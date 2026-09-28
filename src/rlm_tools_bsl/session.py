import logging
import os
import uuid
import time
import threading
from dataclasses import dataclass, field

_logger = logging.getLogger(__name__)


@dataclass
class Session:
    session_id: str
    path: str
    query: str
    _last_reported_vars: set = field(default_factory=set)
    created_at: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)
    max_output_chars: int = 15_000
    max_llm_calls: int = 50
    llm_calls_used: int = 0
    max_execute_calls: int = 50
    execute_calls: int = 0
    total_in_chars: int = 0
    total_out_chars: int = 0
    # v1.29.0 §9.1: сериализация двух rlm_execute ОДНОЙ сессии. Держится только
    # исполняющим путём _rlm_execute; rlm_end/TTL-eviction/shutdown его НИКОГДА
    # не ждут (иначе teardown ждал бы пользовательский timeout до 300с). v1.41.0:
    # rlm_end после отцепления сессии пробует взять его БЕЗ ожидания — только чтобы
    # пометить итог журнала in_flight=1, если execute ещё идёт.
    # Никогда не сериализуется в sandbox worker.
    execution_lock: threading.RLock = field(default_factory=threading.RLock, repr=False, compare=False)
    # v1.41.0 — состояние выдачи подписей хелперов. Меняют его только _rlm_start (до
    # публикации сессии) и _rlm_execute (под execution_lock); справка rlm_help
    # состояния сессии не трогает, поэтому отдельный замок не нужен.
    #   registry_view — представление реестра сессии: имена из реестра воркера,
    #     пересечённые с каталогом родителя, записи (тексты подписей) — из каталога;
    #     пустое в generic-сессии (BSL-хелперов нет);
    #   registry_generation — поколение воркера, для которого строилось представление;
    #   catalog_all_at_start — весь каталог выдан на старте (full, RLM_CATALOG_MODE=all,
    #     выбор «весь каталог»): способ выдачи фиксируется на весь срок сессии;
    #   helper_domains — выбранные и попутно догруженные домены;
    #   delivered_helpers — имена, чьи подписи агент уже получил (старт или signatures);
    #   outside_helpers — вызовы хелперов, чья подпись до вызова не выдавалась;
    #   outside_unknown_executes — ответы без истории вызовов (hard timeout/авария);
    #   domains_added — сколько rlm_execute догрузили хотя бы один новый домен.
    registry_view: dict = field(default_factory=dict, repr=False, compare=False)
    registry_generation: int = 0
    catalog_all_at_start: bool = False
    helper_domains: list = field(default_factory=list)
    delivered_helpers: set = field(default_factory=set, repr=False, compare=False)
    outside_helpers: list = field(default_factory=list)
    outside_unknown_executes: int = 0
    domains_added: int = 0


class SessionManager:
    def __init__(
        self,
        max_sessions: int = 5,
        timeout_minutes: int | None = None,
        timeout_idle_minutes: int = 10,
        timeout_active_minutes: int = 30,
    ):
        self._sessions: dict[str, Session] = {}
        self._max_sessions = max_sessions
        if timeout_minutes is not None:
            # Backward compat: single value overrides both
            self._timeout_idle = timeout_minutes * 60
            self._timeout_active = timeout_minutes * 60
        else:
            self._timeout_idle = timeout_idle_minutes * 60
            self._timeout_active = timeout_active_minutes * 60
        self._lock = threading.Lock()
        # Опциональный хук: вызывается ВНЕ замка для каждого эвикченного sid.
        self.on_evict = None

    def _fire_on_evict(self, evicted):
        cb = self.on_evict
        if cb is None:
            return
        for sid in evicted:
            try:
                cb(sid)
            except Exception:
                _logger.warning("on_evict callback failed for %s", sid, exc_info=True)

    def create(
        self,
        path: str,
        query: str,
        max_output_chars: int = 15_000,
        max_llm_calls: int = 50,
        max_execute_calls: int = 50,
    ) -> str:
        with self._lock:
            evicted = self._cleanup_expired_locked()
            at_capacity = len(self._sessions) >= self._max_sessions
            session_id = None
            if not at_capacity:
                session_id = uuid.uuid4().hex[:12]
                self._sessions[session_id] = Session(
                    session_id=session_id,
                    path=path,
                    query=query,
                    max_output_chars=max_output_chars,
                    max_llm_calls=max_llm_calls,
                    max_execute_calls=max_execute_calls,
                )
        self._fire_on_evict(evicted)
        if at_capacity:
            raise RuntimeError(f"Cannot create session: max sessions ({self._max_sessions}) reached")
        return session_id

    def get(self, session_id: str) -> Session | None:
        with self._lock:
            evicted = self._cleanup_expired_locked()
            session = self._sessions.get(session_id)
            if session:
                session.last_used = time.time()
        self._fire_on_evict(evicted)
        return session

    def _run_if_current(self, session_id: str, expected: Session, callback) -> bool:
        """Run a short callback only while *expected* is still registered.

        The callback runs under the manager lock so eviction cannot complete in
        the gap between the identity check and publication of a related resource.
        It must remain non-blocking and return a truthy value on success.
        """
        with self._lock:
            if self._sessions.get(session_id) is not expected:
                return False
            return bool(callback())

    def end(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    def cleanup_expired(self) -> list[str]:
        with self._lock:
            evicted = self._cleanup_expired_locked()
        self._fire_on_evict(evicted)
        return evicted

    def _cleanup_expired_locked(self) -> list[str]:
        now = time.time()
        expired: list[str] = []
        for sid, s in self._sessions.items():
            timeout = self._timeout_idle if s.execute_calls == 0 else self._timeout_active
            if now - s.last_used > timeout:
                expired.append(sid)
        for sid in expired:
            s = self._sessions.pop(sid)
            _logger.info(
                "session %s evicted (idle %.0fs, calls=%d)",
                sid,
                now - s.last_used,
                s.execute_calls,
            )
        return expired


def build_session_manager_from_env() -> SessionManager:
    """Create SessionManager from environment variables."""
    timeout = os.environ.get("RLM_SESSION_TIMEOUT")
    return SessionManager(
        max_sessions=int(os.environ.get("RLM_MAX_SESSIONS", "5")),
        timeout_minutes=int(timeout) if timeout else None,
        timeout_idle_minutes=int(os.environ.get("RLM_SESSION_TIMEOUT_IDLE", "10")),
        timeout_active_minutes=int(os.environ.get("RLM_SESSION_TIMEOUT_ACTIVE", "30")),
    )
