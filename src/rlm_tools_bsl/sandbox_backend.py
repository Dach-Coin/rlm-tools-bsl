"""Backend-слой песочницы (v1.29.0): интерфейс, inline-реализация, reaper.

``server.py`` больше не работает с ``Sandbox``/``_namespace`` напрямую — только
с backend-объектом, который предоставляет execute + JSON-safe metadata
(registry snapshot без ``fn``, detected prefixes, has_llm_tools, состояние).
Две реализации:

* ``InlineSandboxBackend`` (здесь) — обёртка над текущим ``Sandbox`` в том же
  процессе; для unit-тестов, диагностики и аварийного ручного fallback
  (``RLM_SANDBOX_MODE=inline``). Hard-kill НЕ гарантирует.
* ``ProcessSandboxBackend`` (``sandbox_process.py``) — процесс на сессию,
  production-цель релиза.

Lifecycle двухфазный (§5.2/§9.4 плана): ``request_close(reason)`` — мгновенный
идемпотентный revoke без ожиданий; ``finish_close(deadline)`` — bounded
graceful/join/force-kill, обычно только из ``SandboxBackendReaper`` (исключение
— pre-registration init failure в ``_rlm_start``). Teardown-пути никогда не
берут session execution lock.
"""

from __future__ import annotations

import heapq
import itertools
import logging
import queue
import threading
import time
from dataclasses import dataclass, field

from rlm_tools_bsl.sandbox import HelperCall, Sandbox

logger = logging.getLogger(__name__)


class SandboxClosedError(RuntimeError):
    """Backend отозван (rlm_end/TTL/shutdown) — execute невозможен."""


class SandboxStartupError(RuntimeError):
    """Worker не удалось запустить/инициализировать (включая lazy restart)."""


@dataclass
class BackendExecutionResult:
    """Результат execute, отвязанный от runtime-объектов worker.

    ``stdout`` уже содержит маркеры усечения/аварийного завершения;
    ``sandbox_state`` — machine-readable маркер terminated/restarted (§10.6)
    либо None для обычных ответов.
    """

    stdout: str
    error: str | None
    variables: list[str]
    helper_calls: list[HelperCall] = field(default_factory=list)
    efficiency_hints: list[dict] | None = None
    sandbox_state: dict | None = None
    generation: int = 1
    # WARNING+ пакета, испущенные в worker (v1.34.0). ОБЯЗАТЕЛЬНО с
    # ``default_factory``: результат конструируют и inline-backend, и аварийные
    # ветки process-backend, и они об этом поле знать не должны — поле без дефолта
    # либо уронило бы inline, либо потребовало правки всех конструкторов.
    # В public JSON-ответ агенту НЕ копируется: это диагностика оператора.
    log_records: list[str] = field(default_factory=list)


@dataclass
class CloseReport:
    closed: bool
    forced: bool = False
    residual: bool = False  # True → cleanup не доведён, backend возвращается в reaper
    errors: list[str] = field(default_factory=list)


class LlmQuota:
    """Внутрипроцессный LLM-quota counter (inline mode).

    Семантика зеркалит прежний ``_reserve_llm_calls``: single резервирует 1,
    batch — весь N атомарно (all-or-nothing); резерв при provider error не
    возвращается. Process mode использует межпроцессный аналог в
    ``sandbox_process.py`` — shared counter, переживающий kill.
    """

    def __init__(self, max_calls: int, used: int = 0):
        self._lock = threading.Lock()
        self._max = max_calls
        self._used = used

    def reserve(self, count: int) -> None:
        if count < 1:
            raise ValueError("count must be >= 1")
        with self._lock:
            if self._used + count > self._max:
                raise RuntimeError(f"LLM call limit exceeded: {self._used} + {count} > {self._max}")
            self._used += count

    @property
    def used(self) -> int:
        with self._lock:
            return self._used


class InlineSandboxBackend:
    """Однопроцессная обёртка над ``Sandbox``: прежнее runtime-поведение + новый
    metadata/lifecycle контракт. Владеет переданным ``idx_reader`` и закрывает
    его в ``finish_close`` (§14.5)."""

    mode = "inline"

    def __init__(
        self,
        sandbox: Sandbox,
        idx_reader=None,
        *,
        max_llm_calls: int = 50,
        llm_calls_used: int = 0,
        install_llm_tools: bool = True,
    ):
        self._sandbox = sandbox
        self._idx_reader = idx_reader
        self._state_lock = threading.Lock()
        self._state = "alive"  # alive | executing | closing | closed
        # Отдельный флаг: request_close() затирает "executing" на "closing", после
        # чего по _state уже нельзя понять, крутится ли пользовательский код.
        # finish_close() обязан это знать (см. его докстроку).
        self._executing = False
        self._close_reason: str | None = None
        self._close_lock = threading.Lock()
        self.generation = 1
        self.last_reset_reason: str | None = None
        self._quota = LlmQuota(max_llm_calls, llm_calls_used)
        self._has_llm_tools = self._install_llm_tools() if install_llm_tools else False
        # Может поднять RuntimeError (helper вне каталога) — это init failure
        # сессии by design, не повод собирать альтернативную схему (§7.5).
        self._registry_snapshot: dict[str, dict] = sandbox.registry_metadata_snapshot()
        self._detected_prefixes, self._prefixes_source = self._compute_prefixes()

    # -- metadata -----------------------------------------------------------

    @property
    def registry_snapshot(self) -> dict[str, dict]:
        # Копия на каждый доступ: потребитель не может испортить кеш backend.
        return {name: {**entry, "kw": list(entry["kw"])} for name, entry in self._registry_snapshot.items()}

    @property
    def registry_names(self) -> tuple[str, ...]:
        # Вычисляемое представление ключей snapshot — не второй источник истины (§5.2).
        return tuple(self._registry_snapshot.keys())

    @property
    def startup_log_records(self) -> list[str]:
        """Inline-режим МОСТ НЕ ИСПОЛЬЗУЕТ: логгер здесь и так родительский, и
        двойной записи быть не должно. Свойство существует ради единого
        server-контракта и всегда пусто."""
        return []

    @property
    def detected_prefixes(self) -> list[str]:
        return list(self._detected_prefixes)

    @property
    def prefixes_source(self) -> str:
        return self._prefixes_source

    @property
    def extension_paths(self) -> list[str]:
        return list(self._sandbox._extension_paths)

    @property
    def extension_paths_count(self) -> int:
        return len(self._sandbox._extension_paths)

    @property
    def has_llm_tools(self) -> bool:
        return self._has_llm_tools

    @property
    def llm_calls_used(self) -> int:
        return self._quota.used

    @property
    def index_loaded(self) -> bool:
        return self._idx_reader is not None

    @property
    def state(self) -> str:
        with self._state_lock:
            return self._state

    @property
    def worker_pid(self) -> int | None:
        return None

    # -- execute ------------------------------------------------------------

    def execute(self, code: str) -> BackendExecutionResult:
        with self._state_lock:
            if self._state in ("closing", "closed"):
                raise SandboxClosedError(f"sandbox backend is {self._state} (reason: {self._close_reason})")
            self._state = "executing"
            self._executing = True
        try:
            result = self._sandbox.execute(code)
        finally:
            with self._state_lock:
                self._executing = False
                if self._state == "executing":
                    self._state = "alive"
        return BackendExecutionResult(
            stdout=result.stdout,
            error=result.error,
            variables=result.variables,
            helper_calls=list(result.helper_calls or []),
            efficiency_hints=result.efficiency_hints,
            sandbox_state=None,
            generation=self.generation,
        )

    # -- lifecycle ----------------------------------------------------------

    def request_close(self, reason: str) -> None:
        """Идемпотентный неблокирующий revoke. Inline не может прервать уже
        запущенный exec (hard-kill гарантируется только в process mode) — новый
        execute после revoke получает ``SandboxClosedError``."""
        with self._state_lock:
            if self._state in ("closing", "closed"):
                return
            self._state = "closing"
            self._close_reason = reason

    def finish_close(self, deadline: float) -> CloseReport:
        """Bounded закрытие inline-backend.

        Inline не умеет hard-kill, поэтому пользовательский код может держать
        внутренний lock ``IndexReader`` (его ``close()`` — это
        ``with self._lock: conn.close()``). Закрыть reader прямо из reaper-потока
        значило бы: (а) подвесить ЕДИНСТВЕННЫЙ reaper на весь пользовательский
        execute, остановив уборку всех остальных сессий, и (б) в другом
        interleaving закрыть соединение под работающим кодом. Поэтому пока
        execute активен возвращаем ``residual`` — reaper повторит позже; а на
        исчерпании deadline отдаём закрытие отдельному daemon-потоку, чтобы
        handle всё-таки освободился, но reaper не блокировался.
        """
        with self._close_lock:
            return self._finish_close_locked(deadline)

    def force_abort(self) -> bool:
        """Неблокирующая попытка добить backend (server shutdown после общего deadline).

        НИКОГДА не ждёт ``_close_lock``: если им уже владеет reaper, второй
        синхронный ``finish_close`` встал бы в очередь за чужим 15-секундным
        deadline и вышел бы далеко за общий бюджет остановки сервера.
        Возвращает True, только если уборка действительно доведена.
        """
        if not self._close_lock.acquire(blocking=False):
            return False
        try:
            return self._finish_close_locked(time.monotonic() - 1.0).closed
        except Exception:  # noqa: BLE001 — teardown не должен падать
            logger.warning("inline backend: force_abort failed", exc_info=True)
            return False
        finally:
            self._close_lock.release()

    def _finish_close_locked(self, deadline: float) -> CloseReport:
        """Тело закрытия. Вызывающий ОБЯЗАН держать ``_close_lock``."""
        with self._state_lock:
            if self._state == "closed":
                return CloseReport(closed=True)
            executing = self._executing
        if executing and time.monotonic() < deadline:
            return CloseReport(closed=False, residual=True)
        report = CloseReport(closed=True)
        reader, self._idx_reader = self._idx_reader, None
        if reader is not None:
            if executing:
                # Deadline исчерпан, код всё ещё крутится: отцепляем закрытие,
                # чтобы не заблокировать reaper. Поток закроет соединение, как
                # только helper отпустит lock reader-а.
                report.forced = True
                report.errors.append("idx_reader.close deferred to detached thread (execute still running)")
                threading.Thread(
                    target=self._close_reader_detached, args=(reader,), name="inline-reader-close", daemon=True
                ).start()
            else:
                try:
                    reader.close()
                except Exception as exc:  # noqa: BLE001 — teardown не должен падать
                    report.errors.append(f"idx_reader.close: {type(exc).__name__}: {exc}")
        with self._state_lock:
            self._state = "closed"
        return report

    @staticmethod
    def _close_reader_detached(reader) -> None:
        try:
            reader.close()
        except Exception:  # noqa: BLE001 — teardown не должен падать
            logger.warning("inline backend: deferred idx_reader.close failed", exc_info=True)

    # -- internals ----------------------------------------------------------

    def _compute_prefixes(self) -> tuple[list[str], str]:
        """Fast-path из индекса, затем fallback-скан песочницы (перенос логики
        из ``_rlm_start`` — §13.2: server больше не читает ``_namespace``)."""
        if self._idx_reader is not None:
            try:
                prefixes = self._idx_reader.get_detected_prefixes()
                if prefixes:
                    return list(prefixes), "index"
            except Exception:
                pass
        prefix_fn = self._sandbox._namespace.get("_detected_prefixes")
        if callable(prefix_fn):
            try:
                prefixes = prefix_fn()
                if prefixes:
                    return list(prefixes), "fallback"
            except Exception:
                pass
        return [], "none"

    def _install_llm_tools(self) -> bool:
        """Прежний eager-контракт inline: client создаётся здесь; неуспех →
        helpers отсутствуют (parity с v1.28, тесты стерегут)."""
        try:
            from rlm_tools_bsl.llm_bridge import get_llm_query_fn, make_llm_query_batched

            base_llm_query = get_llm_query_fn()
            if base_llm_query is None:
                logger.info("llm_query not available (no LLM provider configured)")
                return False
            base_llm_query_batched = make_llm_query_batched(base_llm_query)
            quota = self._quota

            def llm_query(prompt: str, context: str = "") -> str:
                quota.reserve(1)
                return base_llm_query(prompt, context)

            def llm_query_batched(prompts: list[str], context: str = "") -> list[str]:
                if not prompts:
                    return []
                quota.reserve(len(prompts))
                return base_llm_query_batched(prompts, context)

            self._sandbox._namespace["llm_query"] = llm_query
            self._sandbox._namespace["llm_query_batched"] = llm_query_batched
            return True
        except Exception as e:
            logger.warning(f"Could not initialize llm_query: {e}")
            return False


# Sentinel остановки reaper-потока (используется stop(), см. её докстроку).
_REAPER_STOP = object()


class SandboxBackendReaper:
    """Единственный владелец завершающей фазы lifecycle (§9.4).

    Неблокирующая FIFO-очередь + pending-set + ОДИН daemon-thread: eviction/
    ``rlm_end`` только снимают backend из registries, зовут ``request_close``
    и кладут его сюда; ожидание graceful/join/force-kill происходит здесь, вне
    caller request. ``enqueue`` никогда не ждёт capacity и не теряет backend
    молча; повторная постановка того же объекта подавляется pending-set.
    """

    # Bounded время finish_close на один backend внутри reaper-thread.
    _PER_BACKEND_DEADLINE_SECONDS = 15.0
    # Общий бюджет повторов на один backend. Должен переживать самый долгий
    # пользовательский execute (schema-потолок rlm_start — 300с), иначе inline
    # residual сдался бы раньше, чем код отпустит lock IndexReader.
    _RESIDUAL_BUDGET_SECONDS = 360.0
    _RESIDUAL_BACKOFF_START = 0.2
    _RESIDUAL_BACKOFF_MAX = 2.0
    # После исчерпания бюджета backend не выбрасывается, а переходит на редкие
    # force-повторы: живой worker обязан оставаться в pending_count().
    _SLOW_RETRY_SECONDS = 30.0

    def __init__(self):
        self._queue: queue.SimpleQueue = queue.SimpleQueue()
        # id(backend) -> backend: держим сам объект, чтобы server shutdown мог
        # ДОБИТЬ оставшиеся деревья, а не только подождать их (§13.6).
        self._pending: dict[int, object] = {}
        # id(backend) -> (первое попадание в residual, текущий backoff)
        self._retry_state: dict[int, tuple[float, float]] = {}
        self._pending_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._thread_lock = threading.Lock()
        self._stopped = False

    def enqueue(self, backend) -> None:
        with self._pending_lock:
            if id(backend) in self._pending:
                return
            self._pending[id(backend)] = backend
        self._ensure_thread()
        self._queue.put(backend)

    def pending_count(self) -> int:
        """Диагностика (§9.4.5): устойчивый рост = lifecycle bug."""
        with self._pending_lock:
            return len(self._pending)

    def force_abort_pending(self) -> tuple[int, int]:
        """Неблокирующе добить всё, что осталось в очереди (§13.6, после общего deadline).

        Использует ``force_abort()``, а НЕ ``finish_close()``: последний ждал бы
        ``_close_lock``, которым мог владеть reaper со своим собственным
        15-секундным deadline, и тогда shutdown вышел бы далеко за общий бюджет.

        Из ``_pending`` удаляются ТОЛЬКО подтверждённо закрытые. Живой worker
        обязан оставаться виден в ``pending_count()``, иначе диагностика
        отрапортует успешный drain при фактической утечке процесса.
        Возвращает ``(закрыто, осталось)``.
        """
        with self._pending_lock:
            backends = list(self._pending.items())
        closed = 0
        for key, backend in backends:
            aborted = False
            try:
                aborted = bool(backend.force_abort())
            except Exception:
                logger.warning("shutdown: force_abort failed for %s", getattr(backend, "mode", "?"), exc_info=True)
            if aborted:
                closed += 1
                with self._pending_lock:
                    self._pending.pop(key, None)
                    self._retry_state.pop(key, None)
            else:
                logger.warning(
                    "shutdown: %s backend не удалось добить — остаётся в pending (возможная утечка процесса)",
                    getattr(backend, "mode", "?"),
                )
        return closed, self.pending_count()

    def drain(self, deadline: float) -> bool:
        """Дождаться опустошения очереди, но не дольше ОБЩЕГО deadline (§9.4.6).

        Используется только server shutdown: backends, снятые эвикцией
        непосредственно перед остановкой, должны быть добиты в пределах того же
        единого бюджета, а не оставлены на семантику daemon-процессов.
        Возвращает True, если к deadline ничего не осталось.
        """
        while self.pending_count() > 0:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)
        return True

    def stop(self, timeout: float = 5.0) -> None:
        """Остановить reaper-поток.

        Нужен ТЕСТАМ: локальный reaper с незакрываемым fake-ом иначе повторял бы
        попытки и писал в лог до конца всего pytest-процесса. Production-синглтон
        в ``server.py`` живёт всё время работы сервера и не останавливается.
        """
        with self._thread_lock:
            self._stopped = True
            thread = self._thread
            self._thread = None
        if thread is None or not thread.is_alive():
            return
        self._queue.put(_REAPER_STOP)
        thread.join(timeout)

    def _ensure_thread(self) -> None:
        with self._thread_lock:
            if self._stopped:
                return
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._run, name="sandbox-reaper", daemon=True)
            self._thread.start()

    def _next_backend(self, deferred: list):
        """Взять следующий backend: готовый отложенный либо новый из очереди.

        Отложенные повторы живут в min-heap по сроку, а ожидание делается
        ``get(timeout=...)``. Простой ``sleep(backoff)`` блокировал бы
        ЕДИНСТВЕННЫЙ consumer: несколько residual-backend-ов давали бы
        N × 30 секунд задержки для всех штатно закрывающихся сессий позади них.
        """
        while True:
            now = time.monotonic()
            if deferred and deferred[0][0] <= now:
                return heapq.heappop(deferred)[2]
            timeout = None if not deferred else max(0.0, deferred[0][0] - now)
            try:
                return self._queue.get(timeout=timeout)
            except queue.Empty:
                continue  # настал срок отложенного — заберём его на следующем витке

    def _run(self) -> None:
        deferred: list[tuple[float, int, object]] = []
        tiebreak = itertools.count()
        while True:
            backend = self._next_backend(deferred)
            if backend is _REAPER_STOP:
                return
            key = id(backend)
            with self._pending_lock:
                started, backoff = self._retry_state.setdefault(key, (time.monotonic(), self._RESIDUAL_BACKOFF_START))
            # Бюджет повторов АБСОЛЮТНЫЙ и переживает retry. Иначе каждый повтор
            # выдавал бы свежие PER_BACKEND секунды, backend вечно возвращал бы
            # residual, а force-ветка finish_close не вызывалась бы никогда —
            # по истечении бюджета его просто забывали с открытым reader.
            give_up_at = started + self._RESIDUAL_BUDGET_SECONDS
            now = time.monotonic()
            final_attempt = now >= give_up_at
            if final_attempt:
                # Заведомо истёкший deadline: finish_close ОБЯЗАН довести очистку
                # (force-kill дерева / detached close reader), а не просить повтор.
                deadline = now - 1.0
            else:
                deadline = min(now + self._PER_BACKEND_DEADLINE_SECONDS, give_up_at)
            try:
                report = backend.finish_close(deadline)
            except Exception:
                logger.warning(
                    "reaper: finish_close failed for %s backend%s",
                    getattr(backend, "mode", "?"),
                    "" if final_attempt else " — will retry",
                    exc_info=True,
                )
                # Исключение НЕ повод забыть backend: оно могло прилететь до kill
                # дерева/закрытия handles, и тогда worker остался бы жить. Инвариант
                # плана §9.4.5 — «queue не может молча потерять backend».
                report = CloseReport(closed=False, residual=True, errors=["finish_close raised"])
            requeue = False
            with self._pending_lock:
                if report.residual:
                    # Backend НЕ удаляется из _pending, пока не закрыт — даже после
                    # исчерпания бюджета. Иначе живой worker исчезал бы из
                    # pending_count(), и диагностика рапортовала бы успешный drain
                    # при фактической утечке процесса. После бюджета переходим на
                    # редкие force-повторы вместо отказа.
                    if final_attempt:
                        logger.error(
                            "reaper: %s backend cleanup still residual after force attempt "
                            "— оставляем в pending, редкий повтор каждые %.0fs (возможная утечка процесса)",
                            getattr(backend, "mode", "?"),
                            self._SLOW_RETRY_SECONDS,
                        )
                        backoff = self._SLOW_RETRY_SECONDS
                        self._retry_state[key] = (started, self._SLOW_RETRY_SECONDS)
                    else:
                        backoff = min(backoff * 2, self._RESIDUAL_BACKOFF_MAX)
                        self._retry_state[key] = (started, backoff)
                    requeue = True
                else:
                    self._pending.pop(key, None)
                    self._retry_state.pop(key, None)
            if report.errors:
                logger.warning("reaper: finish_close errors: %s", "; ".join(report.errors))
            if requeue:
                # Откладываем срок, а НЕ спим: новые backend-ы должны приниматься
                # немедленно, не дожидаясь чужого backoff.
                heapq.heappush(deferred, (time.monotonic() + backoff, next(tiebreak), backend))
