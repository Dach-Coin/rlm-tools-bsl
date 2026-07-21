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
    # не берут (иначе teardown ждал бы пользовательский timeout до 300с).
    # Никогда не сериализуется в sandbox worker.
    execution_lock: threading.RLock = field(default_factory=threading.RLock, repr=False, compare=False)


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
