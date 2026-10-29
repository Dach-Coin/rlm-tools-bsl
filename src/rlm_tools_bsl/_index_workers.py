"""Процессы сборки индекса (v1.48.0): общий бюджет CPU, упорядоченная выдача, временный spool результатов.

Координатор build/update остаётся единственным владельцем SQLite-соединения: воркеры читают неизменные исходники
и возвращают готовые данные. Соединение, обработчики журнала и объект сборщика воркерам не передаются; задачи —
функции уровня модуля, этот модуль ``server`` не импортирует и вложенных пулов процессов не создаёт.

- Пул один на операцию и создаётся лениво, всегда ``spawn``. Задачи всех видов (батчи BSL, коллекторы описателей,
  батчи прав) делят ОДИН бюджет ``RLM_INDEX_WORKERS`` (``auto`` — доступные CPU по affinity и cgroup, не больше
  ``AUTO_WORKERS_CAP``; ``1`` — всё последовательно в координаторе).
- Бюджет общий на процесс: операции над разными индексами в одном сервере берут процессы из него, а не по полному
  пулу каждая; не досталось хотя бы двух — операция идёт последовательно в координаторе.
- Не удалось ЗАПУСТИТЬ процессы (проба старта не ответила) — одно предупреждение и последовательное исполнение тех
  же функций в координаторе: данные те же. Исключение кода задачи или смерть уже работавшего воркера — ошибка
  операции, а не пропущенный файл.
- Предупреждения, которые задача пишет в журнал ``rlm_tools_bsl``, едут обратно вместе с результатом и
  записываются координатором тем же логгером и уровнем: журнал прежний, даже когда работу делал воркер.
- ``ResultSpool`` — временный файл рядом с индексом с порциями результатов текущей операции (собственный формат с
  версией, только для этого запуска): координатор не держит в памяти все ``FileResult`` до записи.
"""

from __future__ import annotations

import collections
import contextlib
import logging
import math
import multiprocessing
import os
import pickle
import signal
import sys
import tempfile
import threading
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import Future, ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path

logger = logging.getLogger(__name__)

WORKERS_ENV = "RLM_INDEX_WORKERS"
# Верхний предел auto: дальше выигрыш упирается в запись координатора, а память дерева процессов растёт.
AUTO_WORKERS_CAP = 8
# Сколько ждать первого ответа воркера (импорт пакета под spawn): не ответил — пул считается незапустившимся.
START_PROBE_TIMEOUT_SECONDS = 120.0

_PICKLE_PROTOCOL = pickle.HIGHEST_PROTOCOL
# Воркер-процесс помнит, что он воркер: коллекторы внутри него не заводят своих пулов потоков (общий бюджет CPU).
_IN_INDEX_WORKER = False


# ── Лимит CPU ──────────────────────────────────────────────────────────────────────────────────────────────────


def _windows_affinity_cpus() -> int | None:
    """Число CPU в маске affinity процесса (Windows, без pywin32)."""
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        process_mask = ctypes.c_size_t()
        system_mask = ctypes.c_size_t()
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.GetProcessAffinityMask.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(ctypes.c_size_t),
            ctypes.POINTER(ctypes.c_size_t),
        ]
        ok = kernel32.GetProcessAffinityMask(
            kernel32.GetCurrentProcess(), ctypes.byref(process_mask), ctypes.byref(system_mask)
        )
        if not ok:
            return None
        count = bin(process_mask.value).count("1")
        return count or None
    except Exception:
        return None


def _affinity_cpus() -> int | None:
    """CPU, доступные процессу: ``os.process_cpu_count`` (3.13+), ``sched_getaffinity`` (POSIX, учитывает
    cpuset), маска affinity (Windows), иначе ``os.cpu_count``."""
    process_cpu_count = getattr(os, "process_cpu_count", None)
    if process_cpu_count is not None:
        try:
            count = process_cpu_count()
            if count:
                return count
        except Exception:
            pass
    if hasattr(os, "sched_getaffinity"):
        try:
            return len(os.sched_getaffinity(0)) or None
        except OSError:
            pass
    if sys.platform == "win32":
        count = _windows_affinity_cpus()
        if count:
            return count
    return os.cpu_count()


def _read_text(path: str) -> str | None:
    try:
        with open(path, encoding="ascii") as fh:
            return fh.read().strip()
    except (OSError, UnicodeDecodeError):
        return None


def _cgroup_cpu_quota(root: str = "/sys/fs/cgroup") -> float | None:
    """Квота CPU контейнера (cgroup v2 ``cpu.max``, v1 ``cpu.cfs_quota_us``/``cpu.cfs_period_us``); ``None`` —
    квоты нет или её не прочитать."""
    v2 = _read_text(os.path.join(root, "cpu.max"))
    if v2:
        parts = v2.split()
        if len(parts) == 2 and parts[0] != "max":
            try:
                quota, period = int(parts[0]), int(parts[1])
            except ValueError:
                return None
            return quota / period if quota > 0 and period > 0 else None
        return None
    for cpu_dir in ("cpu", "cpu,cpuacct", "cpuacct,cpu"):
        quota_text = _read_text(os.path.join(root, cpu_dir, "cpu.cfs_quota_us"))
        period_text = _read_text(os.path.join(root, cpu_dir, "cpu.cfs_period_us"))
        if quota_text is None or period_text is None:
            continue
        try:
            quota, period = int(quota_text), int(period_text)
        except ValueError:
            return None
        return quota / period if quota > 0 and period > 0 else None
    return None


def available_cpus() -> int:
    """CPU, которые действительно доступны процессу: affinity/cpuset и квота cgroup; не меньше одного."""
    count = _affinity_cpus() or 1
    quota = _cgroup_cpu_quota()  # файлов cgroup нет (Windows, macOS) — None
    if quota is not None:
        count = min(count, max(1, math.floor(quota)))
    return max(1, count)


def resolve_index_workers(raw: str | None = None, *, warn: bool = True) -> tuple[int, str]:
    """Число процессов сборки и как оно получено. ``RLM_INDEX_WORKERS``: пусто/``auto`` — доступные CPU, не
    больше ``AUTO_WORKERS_CAP``; целое ``N >= 1`` — ``N``, но не больше доступных CPU; иное — предупреждение
    (``warn``) и ``auto``."""
    if raw is None:
        raw = os.environ.get(WORKERS_ENV)
    available = available_cpus()
    value = (raw or "").strip().lower()
    if value in ("", "auto"):
        return min(available, AUTO_WORKERS_CAP), f"auto (available CPU {available})"
    try:
        requested = int(value)
    except ValueError:
        requested = 0
    if requested < 1:
        if warn:
            logger.warning(
                "%s=%r is not 'auto' or a positive integer; using auto (available CPU %d)", WORKERS_ENV, raw, available
            )
        return min(available, AUTO_WORKERS_CAP), f"auto (invalid {WORKERS_ENV})"
    if requested > available:
        return available, f"{WORKERS_ENV}={requested}, limited to available CPU {available}"
    return requested, f"{WORKERS_ENV}={requested}"


def in_index_worker() -> bool:
    """Код исполняется в воркере сборки (там коллекторы не заводят своих пулов потоков)."""
    return _IN_INDEX_WORKER


# ── Воркер ─────────────────────────────────────────────────────────────────────────────────────────────────────


class _RecordCollector(logging.Handler):
    """Предупреждения задачи — для переноса в журнал координатора."""

    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.records: list[tuple[int, str, str]] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.records.append((record.levelno, record.name, record.getMessage()))
        except Exception:
            pass


def _parent_death_guard(parent_pid: int) -> None:
    """Воркер не переживает координатора: Windows — ожидание дескриптора родителя, POSIX — опрос ``getppid``."""

    def _watch_windows() -> None:
        try:
            import ctypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.restype = ctypes.c_void_p
            handle = kernel32.OpenProcess(0x00100000, False, parent_pid)  # SYNCHRONIZE
            if not handle:
                os._exit(1)
            kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            kernel32.WaitForSingleObject(handle, 0xFFFFFFFF)
        except Exception:
            return
        os._exit(1)

    def _watch_posix() -> None:
        import time

        while True:
            time.sleep(1.0)
            if os.getppid() != parent_pid:
                os._exit(1)

    target = _watch_windows if sys.platform == "win32" else _watch_posix
    threading.Thread(target=target, name="rlm-index-parent-guard", daemon=True).start()


def _worker_init(parent_pid: int) -> None:
    global _IN_INDEX_WORKER
    _IN_INDEX_WORKER = True
    # Ctrl+C получает вся группа процессов консоли — останавливает пул координатор.
    try:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    except (ValueError, OSError):
        pass
    # Обработчиков у корня нет, и без этого предупреждения ушли бы ещё и в stderr (logging.lastResort):
    # в журнал их переносит координатор.
    logging.getLogger().addHandler(logging.NullHandler())
    _parent_death_guard(parent_pid)


def _run_task(fn: Callable, args: tuple) -> tuple[bytes, list[tuple[int, str, str]]]:
    """Исполнить задачу; результат — pickle (координатор может положить его в spool, не разбирая), плюс
    предупреждения задачи."""
    collector = _RecordCollector()
    package_logger = logging.getLogger("rlm_tools_bsl")
    package_logger.addHandler(collector)
    try:
        result = fn(*args)
    finally:
        package_logger.removeHandler(collector)
    return pickle.dumps(result, protocol=_PICKLE_PROTOCOL), collector.records


def _probe() -> str:
    """Проба старта: файл пакета, из которого воркер импортировал сборщик."""
    from rlm_tools_bsl import bsl_index

    return bsl_index.__file__


def _coordinator_module() -> str:
    """Файл сборщика, который исполняет координатор: воркер обязан исполнять тот же."""
    from rlm_tools_bsl import bsl_index

    return bsl_index.__file__


def _same_file(left: str, right: str) -> bool:
    return os.path.normcase(os.path.realpath(left)) == os.path.normcase(os.path.realpath(right))


# Окно скрытия главного модуля — одно на процесс: операции из разных потоков (сборки двух индексов в сервере)
# запускают процессы по очереди. Иначе вторая сохранила бы уже скрытые атрибуты и вернула бы их вместо настоящих.
_MAIN_HIDING_LOCK = threading.RLock()


@contextlib.contextmanager
def _without_parent_main() -> Iterator[None]:
    """На время запуска процессов скрыть у ``__main__`` ``__file__`` и ``__spec__``: по ним ``multiprocessing``
    решает, исполнять ли главный модуль координатора в spawn-ребёнке. Скрипт без ``if __name__ == '__main__'``
    (встраивание, инструменты замеров) иначе исполнился бы в каждом воркере со всеми побочными эффектами. Задачи
    сборки живут в модулях пакета, главный модуль воркеру не нужен."""
    main = sys.modules.get("__main__")
    if main is None:
        yield
        return
    with _MAIN_HIDING_LOCK:
        saved = {attr: main.__dict__[attr] for attr in ("__file__", "__spec__") if attr in main.__dict__}
        try:
            main.__dict__.pop("__file__", None)
            main.__spec__ = None
            yield
        finally:
            main.__dict__.pop("__spec__", None)
            main.__dict__.update(saved)


# ── Пул ────────────────────────────────────────────────────────────────────────────────────────────────────────


class IndexWorkerError(RuntimeError):
    """Воркер сборки умер во время работы: операция прерывается, файлы не пропускаются молча."""


def _done_future(fn: Callable, args: tuple) -> Future:
    future: Future = Future()
    try:
        future.set_result(_run_task_inline(fn, args))
    except BaseException as exc:  # noqa: BLE001 — исключение задачи отдаётся тем же путём, что из процесса
        future.set_exception(exc)
    return future


def _run_task_inline(fn: Callable, args: tuple) -> tuple[bytes, list]:
    # В координаторе предупреждения задачи сразу идут в его журнал — переносить нечего.
    return pickle.dumps(fn(*args), protocol=_PICKLE_PROTOCOL), []


# Процессы, занятые пулами операций этого процесса: бюджет ``RLM_INDEX_WORKERS`` — на процесс, а не на операцию.
_BUDGET_LOCK = threading.Lock()
_budget_in_use = 0


def _reserve_workers(requested: int, label: str) -> int:
    """Сколько процессов достаётся операции из бюджета процесса; меньше двух — последовательно, бюджет не
    занимается (координатор воркером не считается)."""
    global _budget_in_use
    if requested <= 1:
        return 1
    limit, _source = resolve_index_workers(warn=False)
    with _BUDGET_LOCK:
        busy = _budget_in_use
        granted = min(requested, limit - busy)
        if granted > 1:
            _budget_in_use += granted
    if busy and granted < requested:
        logger.info(
            "%s workers: %d of %d processes are taken by other index operations in this process; this one gets %s",
            label,
            busy,
            limit,
            granted if granted > 1 else "none (sequential)",
        )
    return max(1, granted)


def _release_workers(count: int) -> None:
    global _budget_in_use
    if count > 1:
        with _BUDGET_LOCK:
            _budget_in_use = max(0, _budget_in_use - count)


class IndexTaskPool:
    """Пул процессов одной операции сборки. ``workers <= 1`` — все задачи исполняются в координаторе в момент
    постановки. Использовать как контекстный менеджер: на выходе с ошибкой (и ``KeyboardInterrupt``) не начатые
    задачи снимаются, процессы пула завершаются. ``workers`` — сколько просит операция; достаётся не больше
    свободного в бюджете процесса (``_reserve_workers``), бюджет возвращается в ``close()`` и при переходе на
    последовательное исполнение."""

    def __init__(self, workers: int, *, label: str = "index") -> None:
        self._label = label
        self._reserved = _reserve_workers(max(1, workers), label)
        self.workers = self._reserved
        self._executor: ProcessPoolExecutor | None = None
        self._sequential = self.workers <= 1
        self._warned = False
        self.worker_module: str | None = None

    @property
    def parallel(self) -> bool:
        return not self._sequential

    def __enter__(self) -> IndexTaskPool:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close(error=exc_type is not None)

    def _fall_back(self, reason: str) -> None:
        if not self._warned:
            logger.warning(
                "%s workers: cannot start worker processes (%s); continuing sequentially in this process",
                self._label,
                reason,
            )
            self._warned = True
        self._sequential = True
        self._terminate_executor()
        self._release_budget()

    def _release_budget(self) -> None:
        reserved, self._reserved = self._reserved, 1
        _release_workers(reserved)

    def _ensure_executor(self) -> ProcessPoolExecutor | None:
        if self._sequential:
            return None
        if self._executor is not None:
            return self._executor
        if getattr(sys, "frozen", False):
            # Frozen-обёртка без multiprocessing.freeze_support() перезапустила бы в ребёнке всё приложение.
            logger.info("%s workers: frozen interpreter, worker processes are not used", self._label)
            self._sequential = True
            self._release_budget()
            return None
        try:
            executor = ProcessPoolExecutor(
                max_workers=self.workers,
                mp_context=multiprocessing.get_context("spawn"),
                initializer=_worker_init,
                initargs=(os.getpid(),),
            )
        except Exception as exc:  # noqa: BLE001 — любая неудача создания — последовательный путь
            self._fall_back(f"{type(exc).__name__}: {exc}")
            return None
        self._executor = executor
        try:
            # Процессы пула заводятся по одному на постановку задачи, пока занятых не станет `workers`: проба на
            # каждый — все процессы стартуют здесь, в окне без главного модуля, и каждый доказывает, что запустился.
            with _without_parent_main():
                probes = [executor.submit(_probe) for _ in range(self.workers)]
            modules = {probe.result(timeout=START_PROBE_TIMEOUT_SECONDS) for probe in probes}
        except Exception as exc:  # noqa: BLE001 — проба не ответила: процессы не запускаются
            self._fall_back(f"{type(exc).__name__}: {exc}")
            return None
        expected = _coordinator_module()
        foreign = sorted(module for module in modules if not _same_file(module, expected))
        if foreign:
            # Ребёнок импортировал другую копию пакета (иной путь поиска): её разбор не обязан совпадать с этим.
            self._fall_back(f"worker package {foreign[0]} differs from coordinator package {expected}")
            return None
        self.worker_module = expected
        logger.info("%s workers: %d processes (spawn), module %s", self._label, self.workers, self.worker_module)
        return executor

    def submit(self, fn: Callable, *args) -> Future:
        """Поставить задачу; результат — ``result(future)``. Последовательный режим исполняет её сразу."""
        executor = self._ensure_executor()
        if executor is None:
            return _done_future(fn, args)
        try:
            return executor.submit(_run_task, fn, args)
        except BrokenProcessPool as exc:
            raise IndexWorkerError(f"{self._label} worker process died: {exc}") from exc
        except RuntimeError as exc:
            # Пул не может завести ещё один процесс (RuntimeError/OSError из spawn) — задача в координаторе.
            self._fall_back(f"{type(exc).__name__}: {exc}")
            return _done_future(fn, args)
        except OSError as exc:
            self._fall_back(f"{type(exc).__name__}: {exc}")
            return _done_future(fn, args)

    def result_bytes(self, future: Future) -> bytes:
        """Pickle результата задачи; предупреждения задачи записываются в журнал координатора."""
        try:
            blob, records = future.result()
        except BrokenProcessPool as exc:
            raise IndexWorkerError(f"{self._label} worker process died: {exc}") from exc
        for levelno, name, message in records:
            logging.getLogger(name).log(levelno, "%s", message)
        return blob

    def result(self, future: Future):
        return pickle.loads(self.result_bytes(future))

    def map_ordered(
        self,
        fn: Callable,
        items: Iterable[tuple],
        *,
        window: int,
        on_done: Callable[[int], None] | None = None,
        weights: Callable[[tuple], int] | None = None,
    ) -> Iterator[bytes]:
        """Pickle-результаты ``fn(*item)`` в порядке ``items``: в работе не больше ``window`` задач, готовые ждут
        очереди не дольше, чем самая старая незавершённая. ``on_done(weight)`` — по завершении каждой задачи
        (прогресс по фактически сделанной работе, а не по порядку выдачи)."""
        pending: collections.deque[Future] = collections.deque()
        iterator = iter(items)

        def submit_next() -> bool:
            try:
                item = next(iterator)
            except StopIteration:
                return False
            future = self.submit(fn, *item)
            if on_done is not None:
                weight = weights(item) if weights is not None else 1
                future.add_done_callback(lambda _f, w=weight: on_done(w))
            pending.append(future)
            return True

        for _ in range(max(1, window)):
            if not submit_next():
                break
        while pending:
            future = pending.popleft()
            blob = self.result_bytes(future)
            submit_next()
            yield blob

    def _terminate_executor(self) -> None:
        executor = self._executor
        self._executor = None
        if executor is None:
            return
        # Процессы пула и его поток-менеджер — до shutdown: он обнуляет приватные ссылки на них, а публичного способа
        # завершить процессы до 3.14 нет (terminate_workers).
        processes = dict(getattr(executor, "_processes", None) or {})
        manager = getattr(executor, "_executor_manager_thread", None)
        terminate = getattr(executor, "terminate_workers", None)
        if terminate is not None:
            try:
                terminate()
            except Exception:
                pass
        for process in list(processes.values()):
            try:
                if process.is_alive():
                    process.terminate()
            except Exception:
                pass
        try:
            executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
        # Поток-менеджер сам ждёт процессы пула (POSIX: waitpid); пока он не закончил, параллельный join получил бы
        # «жив» у уже мёртвого процесса. Дождаться его — значит дождаться и учёта процессов.
        if manager is not None:
            manager.join(timeout=10)
        for process in list(processes.values()):
            try:
                process.join(timeout=10)
            except Exception:
                pass

    def close(self, *, error: bool = False) -> None:
        try:
            executor = self._executor
            if executor is None:
                return
            if error:
                self._terminate_executor()
                return
            self._executor = None
            executor.shutdown(wait=True)
        finally:
            self._release_budget()


def bsl_batches(paths: list, workers: int) -> list[list]:
    """Батчи путей BSL в исходном порядке: много больше числа воркеров, чтобы крупный модуль не растягивал хвост."""
    if not paths:
        return []
    size = max(8, min(64, math.ceil(len(paths) / (max(1, workers) * 24))))
    return [paths[i : i + size] for i in range(0, len(paths), size)]


# ── Spool ──────────────────────────────────────────────────────────────────────────────────────────────────────


class ResultSpool:
    """Временный файл рядом с индексом: порции результатов текущей операции (pickle каждой порции, длина перед
    порцией). Формат версионирован заголовком; это не кеш и не источник данных для хелперов — файл удаляется в
    ``close()`` и при штатной ошибке операции. Читать можно сколько угодно раз (две фазы записи)."""

    MAGIC = b"RLM-INDEX-SPOOL 1\n"

    def __init__(self, directory: str | Path) -> None:
        fd, path = tempfile.mkstemp(prefix=".bsl_index.spool-", suffix=".tmp", dir=str(directory))
        self.path = Path(path)
        self._file = os.fdopen(fd, "w+b")
        self._file.write(self.MAGIC)
        self.batches = 0
        self.bytes = 0

    def __enter__(self) -> ResultSpool:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def append(self, blob: bytes) -> None:
        self._file.write(len(blob).to_bytes(8, "little"))
        self._file.write(blob)
        self.batches += 1
        self.bytes += len(blob)

    def __iter__(self) -> Iterator[list]:
        self._file.flush()
        with open(self.path, "rb") as fh:
            if fh.read(len(self.MAGIC)) != self.MAGIC:
                raise RuntimeError(f"index spool {self.path}: wrong format")
            for _ in range(self.batches):
                size = int.from_bytes(fh.read(8), "little")
                yield pickle.loads(fh.read(size))

    def close(self) -> None:
        try:
            self._file.close()
        finally:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
