"""ProcessSandboxBackend (v1.29.0): процесс-на-сессию, авторитетный parent deadline.

Родительская сторона процессной изоляции песочницы:

* spawn worker (``sandbox_worker.sandbox_worker_main``) через
  ``multiprocessing.get_context("spawn")`` на всех ОС;
* доверенные bootstrap-handles (pipe, shared stdout buffer, shared LLM quota
  counter) передаются только при создании процесса; runtime-сообщения — только
  UTF-8 JSON bytes (``_sandbox_protocol``);
* hard timeout: deadline контролирует родитель, по истечении — kill process
  tree (Windows Job Object / POSIX process group), частичный stdout читается из
  shared buffer (§10);
* crash/timeout → state ``dead`` + lazy restart на следующем execute с
  обязательным ``sandbox_state``-маркером (§10.5-10.6);
* LLM quota — межпроцессный aligned 32-bit counter, резервирование ДО provider
  call; после kill parent читает raw value без старого lock (§12.2).

Windows: невозможность создать/назначить Job Object — controlled ошибка
``rlm_start``; weak-режима и молчаливого fallback в inline нет (§23.9, §23.2).
"""

from __future__ import annotations

import ctypes
import itertools
import logging
import math
import multiprocessing
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from multiprocessing.sharedctypes import RawArray, RawValue

from rlm_tools_bsl._sandbox_protocol import (
    WORKER_FATAL_REQUEST_ID as _WORKER_FATAL_REQUEST_ID,
    SandboxProtocolError,
    bounded_text,
    decode_frame,
    encode_frame,
    make_message,
    validate_message,
)
from rlm_tools_bsl.sandbox import TRUNCATION_MARKER, HelperCall
from rlm_tools_bsl.sandbox_backend import (
    BackendExecutionResult,
    CloseReport,
    SandboxClosedError,
    SandboxStartupError,
)
from rlm_tools_bsl.sandbox_worker import sandbox_worker_main

logger = logging.getLogger(__name__)

# Отдельные маркеры неполноты: усечение по лимиту (TRUNCATION_MARKER) и
# аварийное завершение — это разные причины (§6.4).
TIMEOUT_PARTIAL_MARKER = "\n... [execution terminated after timeout; partial output]"
CRASH_PARTIAL_MARKER = "\n... [execution terminated; partial output]"

# Bounded enum причин смерти worker (уходит в sandbox_state.reason).
_RESET_REASONS = ("timeout", "crash_or_resource_limit", "protocol_error", "worker_error")

_POLL_SLICE_SECONDS = 0.25


class _IpcSendTimeout(TimeoutError):
    """Parent-side IPC send did not complete before the operation deadline."""


# Разумный потолок seq в helper_calls: JSON-integer не ограничен по длине, а
# бесконечно большое значение не имеет смысла и раздувает ответ.
_MAX_SEQ = 2**31 - 1


def format_info_to_payload(format_info) -> dict | None:
    """FormatInfo → JSON-safe dict для init payload (enum через .value, §7.4)."""
    if format_info is None:
        return None
    return {
        "primary_format": format_info.primary_format.value,
        "root_path": format_info.root_path,
        "bsl_file_count": int(format_info.bsl_file_count),
        "has_configuration_xml": bool(format_info.has_configuration_xml),
        "metadata_categories_found": list(format_info.metadata_categories_found or []),
    }


@dataclass
class ProcessBackendConfig:
    """Конфигурация worker-сессии. Только примитивы/списки — уходит в init frame."""

    base_path: str
    max_output_chars: int = 15_000
    execution_timeout_seconds: int = 45
    format_info_payload: dict | None = None
    db_path: str | None = None
    index_expected: bool = False
    idx_zero_callers_authoritative: bool = False
    extension_paths: list[str] = field(default_factory=list)
    max_llm_calls: int = 50
    llm_calls_used: int = 0
    # Только для Python test API (dotted "module:factory") — прокладывает fake
    # provider в spawn child, где parent monkeypatch не действует (§18.7).
    # Production-код _rlm_start эти поля никогда не заполняет.
    test_llm_provider: str | None = None
    test_init_delay_seconds: float = 0.0
    start_timeout_seconds: int = 60
    kill_grace_seconds: int = 1
    ipc_max_bytes: int = 4 * 1024 * 1024
    max_code_chars: int = 1_000_000
    memory_mb: int = 1024
    max_processes: int = 16

    @classmethod
    def from_env(cls, **overrides) -> "ProcessBackendConfig":
        from rlm_tools_bsl import _sandbox_config as sc

        defaults = dict(
            start_timeout_seconds=sc.start_timeout_seconds(),
            kill_grace_seconds=sc.kill_grace_seconds(),
            ipc_max_bytes=sc.ipc_max_bytes(),
            max_code_chars=sc.max_code_chars(),
            memory_mb=sc.memory_mb(),
            max_processes=sc.max_processes(),
        )
        defaults.update(overrides)
        return cls(**defaults)


# ---------------------------------------------------------------------------
# Windows Job Object (ctypes, без новых зависимостей)
# ---------------------------------------------------------------------------

if sys.platform == "win32":  # pragma: no cover - платформенная ветка
    from ctypes import wintypes

    # ABI-декларации ОБЯЗАТЕЛЬНЫ: по умолчанию ctypes считает restype равным
    # c_int (32 бита), а HANDLE на x64 pointer-sized — вернувшийся handle
    # усекался бы/некорректно расширялся, и Job Object молча не работал бы.
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.TerminateJobObject.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            (n, ctypes.c_ulonglong)
            for n in (
                "ReadOperationCount",
                "WriteOperationCount",
                "OtherOperationCount",
                "ReadTransferCount",
                "WriteTransferCount",
                "OtherTransferCount",
            )
        ]

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", ctypes.c_ulong),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", ctypes.c_ulong),
            # ULONG_PTR — pointer-sized, НЕ c_ulong: иначе на x64 съедет разметка
            # хвоста структуры и лимиты применятся не к тем полям.
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", ctypes.c_ulong),
            ("SchedulingClass", ctypes.c_ulong),
        ]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    _JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x100
    _JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x8
    _JobObjectExtendedLimitInformation = 9
    _PROCESS_SET_QUOTA = 0x0100
    _PROCESS_TERMINATE = 0x0001

    class _WindowsJob:
        """Job Object: KILL_ON_JOB_CLOSE обязателен; memory/active-process —
        по конфигурации. Закрытие handle или TerminateJobObject уничтожает
        worker вместе с descendants (§10.3)."""

        def __init__(self, memory_mb: int, max_processes: int):
            kernel32 = _kernel32
            self._kernel32 = kernel32
            self._lock = threading.Lock()
            self._handle = kernel32.CreateJobObjectW(None, None)
            if not self._handle:
                raise OSError(f"CreateJobObjectW failed: {ctypes.WinError(ctypes.get_last_error())}")
            info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            flags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if memory_mb > 0:
                flags |= _JOB_OBJECT_LIMIT_PROCESS_MEMORY
                info.ProcessMemoryLimit = memory_mb * 1024 * 1024
            if max_processes > 0:
                flags |= _JOB_OBJECT_LIMIT_ACTIVE_PROCESS
                info.BasicLimitInformation.ActiveProcessLimit = max_processes
            info.BasicLimitInformation.LimitFlags = flags
            ok = kernel32.SetInformationJobObject(
                self._handle, _JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info)
            )
            if not ok:
                err = ctypes.WinError(ctypes.get_last_error())
                self.close(kill=False)
                raise OSError(f"SetInformationJobObject failed: {err}")

        def assign(self, pid: int) -> None:
            proc_handle = self._kernel32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
            if not proc_handle:
                raise OSError(f"OpenProcess({pid}) failed: {ctypes.WinError(ctypes.get_last_error())}")
            try:
                ok = self._kernel32.AssignProcessToJobObject(self._handle, proc_handle)
                if not ok:
                    raise OSError(f"AssignProcessToJobObject failed: {ctypes.WinError(ctypes.get_last_error())}")
            finally:
                self._kernel32.CloseHandle(proc_handle)

        def terminate(self) -> None:
            """Поднимает OSError при FALSE от WinAPI.

            Молчаливое игнорирование результата означало бы, что _kill_tree_raw
            считает дерево убитым и НЕ переходит к fallback, хотя worker жив.
            """
            with self._lock:
                if self._handle:
                    if not self._kernel32.TerminateJobObject(self._handle, 1):
                        raise OSError(f"TerminateJobObject failed: {ctypes.WinError(ctypes.get_last_error())}")

        def close(self, kill: bool = True) -> tuple[bool, bool]:
            """Закрыть Job; вернуть ``(tree_confirmed, handle_closed)``.

            Успешный CloseHandle с KILL_ON_JOB_CLOSE подтверждает tree-wide
            очистку, но ошибка CloseHandle не должна терять единственный handle:
            он остаётся в объекте для следующей попытки reaper-а.
            """
            with self._lock:
                handle = self._handle
                if not handle:
                    return False, True
                terminated = False
                try:
                    if kill:
                        terminated = bool(self._kernel32.TerminateJobObject(handle, 1))
                        if not terminated:
                            logger.warning("TerminateJobObject failed: %s", ctypes.WinError(ctypes.get_last_error()))
                finally:
                    # CloseHandle выполняется ВСЕГДА: иначе неудачный terminate утёк бы
                    # handle и отменил KILL_ON_JOB_CLOSE.
                    closed_ok = bool(self._kernel32.CloseHandle(handle))
                    if not closed_ok:
                        logger.warning("CloseHandle(job) failed: %s", ctypes.WinError(ctypes.get_last_error()))
                    else:
                        self._handle = None
                return terminated or (kill and closed_ok), closed_ok

else:
    _WindowsJob = None  # type: ignore[assignment]


class ProcessSandboxBackend:
    """Backend одной сессии: один долгоживущий worker-процесс (§5.3)."""

    mode = "process"

    def __init__(self, config: ProcessBackendConfig, *, startup_register=None, startup_unregister=None):
        self._cfg = config
        self._state_lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._state = "starting"  # starting|alive|executing|dead|closing|closed
        self._finalized = False
        self._close_reason: str | None = None
        self.generation = 0
        self.last_reset_reason: str | None = None
        self.last_start_elapsed: float | None = None
        self._llm_used = max(0, int(config.llm_calls_used))
        self._quota_corruption_logged = False
        # Сохраняемое между вызовами состояние tree-wide очистки. Target нужен,
        # чтобы успешный kill НОВОГО поколения не подтвердил ошибочно очистку
        # старого process group после lazy restart.
        self._tree_cleanup_unconfirmed = False
        self._tree_cleanup_target = None
        self._tree_cleanup_confirmed_target = None
        # Пока холодный restart идёт вне _close_lock, reaper не имеет права
        # финализировать старый runtime: иначе новое поколение станет бесхозным.
        self._restart_in_progress = False
        self._request_ids = itertools.count(1)
        self._pending_restarted_marker: dict | None = None
        # Last protocol-validated namespace snapshot.  Parent-side rejections do
        # not execute code and must preserve the public ``variables`` contract.
        self._variables_snapshot: list[str] = []
        # runtime handles текущего поколения
        self._proc = None
        self._conn = None
        self._job = None
        self._out_buf = None
        self._out_published = None
        self._out_truncated = None
        self._quota_value = None
        # metadata из init_ok
        self._registry_snapshot: dict[str, dict] = {}
        self._detected_prefixes: list[str] = []
        self._prefixes_source = "none"
        self._index_loaded = False
        self.index_warning: str | None = None
        self._has_llm_tools = False
        tracked = False
        if startup_register is not None:
            if not startup_register(self):
                with self._state_lock:
                    self._state = "closed"
                self._finalized = True
                raise SandboxClosedError("sandbox registration was revoked before worker startup")
            tracked = True
        try:
            self._start_worker()
            # Once an initializing backend is lifecycle-visible, shutdown may
            # revoke it before init_ok is published. Never overwrite closing
            # with alive in that race.
            with self._close_lock:
                with self._state_lock:
                    revoked = self._state in ("closing", "closed") or self._finalized
                    if not revoked:
                        self._state = "alive"
                if revoked:
                    self._destroy_current_generation()
                    raise SandboxClosedError("sandbox backend was closed during worker startup")
        except Exception:
            if tracked and startup_unregister is not None:
                startup_unregister(self)
            raise

    # -- metadata -----------------------------------------------------------

    @property
    def registry_snapshot(self) -> dict[str, dict]:
        return {name: {**entry, "kw": list(entry.get("kw") or [])} for name, entry in self._registry_snapshot.items()}

    @property
    def registry_names(self) -> tuple[str, ...]:
        return tuple(self._registry_snapshot.keys())

    @property
    def detected_prefixes(self) -> list[str]:
        return list(self._detected_prefixes)

    @property
    def prefixes_source(self) -> str:
        return self._prefixes_source

    @property
    def extension_paths(self) -> list[str]:
        return list(self._cfg.extension_paths)

    @property
    def extension_paths_count(self) -> int:
        return len(self._cfg.extension_paths)

    @property
    def has_llm_tools(self) -> bool:
        return self._has_llm_tools

    @property
    def index_loaded(self) -> bool:
        return self._index_loaded

    @property
    def state(self) -> str:
        with self._state_lock:
            return self._state

    @property
    def worker_pid(self) -> int | None:
        proc = self._proc
        return proc.pid if proc is not None else None

    @property
    def llm_calls_used(self) -> int:
        return self._sync_llm_quota()

    def _sync_llm_quota(self) -> int:
        """Монотонная синхронизация из shared counter (§12.2.5-12.2.6).

        Raw aligned read БЕЗ старого lock — worker мог погибнуть внутри critical
        section. Значение вне 0..max — повреждение состояния, не разрешение
        дополнительной квоты (§12.2.8): clamp + warning.
        """
        qv = self._quota_value
        if qv is not None:
            raw = int(qv.value)
            if raw < 0 or raw > self._cfg.max_llm_calls:
                if not self._quota_corruption_logged:
                    self._quota_corruption_logged = True
                    logger.warning(
                        "sandbox llm quota counter corrupt: raw=%d (max=%d) — clamping", raw, self._cfg.max_llm_calls
                    )
                raw = min(max(raw, 0), self._cfg.max_llm_calls)
            if raw > self._llm_used:
                self._llm_used = raw
        return self._llm_used

    # -- worker startup -----------------------------------------------------

    def _send_bytes_with_deadline(self, conn, payload: bytes, deadline: float) -> None:
        """Send one frame without letting a full pipe bypass the parent deadline."""
        with self._state_lock:
            if self._state in ("closing", "closed"):
                raise SandboxClosedError(f"sandbox backend is {self._state} (reason: {self._close_reason})")
        if time.monotonic() >= deadline:
            raise _IpcSendTimeout("sandbox IPC send deadline exceeded")

        completed = threading.Event()
        send_errors: list[Exception] = []

        def send() -> None:
            try:
                conn.send_bytes(payload)
            except Exception as exc:  # propagated in the lifecycle-owning thread
                send_errors.append(exc)
            finally:
                completed.set()

        threading.Thread(target=send, name="rlm-sandbox-ipc-send", daemon=True).start()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _IpcSendTimeout("sandbox IPC send deadline exceeded")
            if completed.wait(min(_POLL_SLICE_SECONDS, remaining)):
                with self._state_lock:
                    if self._state in ("closing", "closed"):
                        raise SandboxClosedError(f"sandbox backend is {self._state} (reason: {self._close_reason})")
                if send_errors:
                    raise send_errors[0]
                return
            with self._state_lock:
                if self._state in ("closing", "closed"):
                    raise SandboxClosedError(f"sandbox backend is {self._state} (reason: {self._close_reason})")

    def _start_worker(self) -> None:
        """Запуск/перезапуск поколения worker. Поднимает SandboxStartupError,
        гарантированно не оставляя процесса/handles при неуспехе (§8.1-8.2)."""
        cfg = self._cfg
        with self._state_lock:
            if self._state in ("closing", "closed"):
                raise SandboxClosedError(f"sandbox backend is {self._state} (reason: {self._close_reason})")
        gen = self.generation + 1
        t0 = time.monotonic()
        deadline = t0 + cfg.start_timeout_seconds
        ctx = multiprocessing.get_context("spawn")
        parent_conn, child_conn = ctx.Pipe(duplex=True)
        capacity = cfg.max_output_chars * 4 + 64
        out_buf = RawArray(ctypes.c_char, capacity)
        out_published = RawValue(ctypes.c_uint32, 0)
        out_truncated = RawValue(ctypes.c_uint8, 0)
        out_lock = ctx.Lock()
        # Новый counter И новый lock на каждое поколение: lock аварийно
        # завершённого поколения мог остаться захваченным (§12.2.7).
        quota_value = RawValue(ctypes.c_int32, self._llm_used)
        quota_lock = ctx.Lock()
        proc = ctx.Process(
            target=sandbox_worker_main,
            args=(
                child_conn,
                out_buf,
                out_published,
                out_truncated,
                out_lock,
                quota_value,
                quota_lock,
                {"ipc_max_bytes": cfg.ipc_max_bytes, "generation": gen},
            ),
            # daemon — только страховочный пояс к явному shutdown-циклу (§13.6);
            # mp-daemon не мешает subprocess-детям вроде git.
            daemon=True,
            name=f"rlm-sandbox-worker-gen{gen}",
        )
        job = None
        try:
            proc.start()
            child_conn.close()
            # Make the starting runtime visible before any potentially slow
            # init work. request_close()/shutdown can now kill it immediately.
            with self._state_lock:
                self._proc = proc
                self._conn = parent_conn
                self._out_buf = out_buf
                self._out_published = out_published
                self._out_truncated = out_truncated
                self._quota_value = quota_value
                revoked = self._state in ("closing", "closed")
            if revoked:
                raise SandboxClosedError(f"sandbox backend is {self._state} (reason: {self._close_reason})")
            if _WindowsJob is not None:
                # Назначение в Job строго ДО отправки init: до этого worker
                # блокирован на recv init и пользовательских команд не принимает
                # (§8.1.4/§10.3). Неуспех — controlled ошибка, не weak mode.
                try:
                    job = _WindowsJob(cfg.memory_mb, cfg.max_processes)
                    job.assign(proc.pid)
                except OSError as exc:
                    raise SandboxStartupError(
                        f"Windows Job Object unavailable for sandbox worker: {exc}. "
                        "Process isolation requires a Job Object; not falling back to a weaker mode."
                    ) from exc
                with self._state_lock:
                    self._job = job
                    revoked = self._state in ("closing", "closed")
                if revoked:
                    raise SandboxClosedError(f"sandbox backend is {self._state} (reason: {self._close_reason})")

            request_id = next(self._request_ids)
            init_payload = {
                "base_path": cfg.base_path,
                "max_output_chars": cfg.max_output_chars,
                "format_info": cfg.format_info_payload,
                "db_path": cfg.db_path,
                "index_expected": cfg.index_expected,
                "idx_zero_callers_authoritative": cfg.idx_zero_callers_authoritative,
                "extension_paths": list(cfg.extension_paths),
                "execution_timeout_seconds": 0,
                "max_llm_calls": cfg.max_llm_calls,
                "llm_calls_used": self._llm_used,
                "memory_mb": cfg.memory_mb,
                "test_llm_provider": cfg.test_llm_provider,
                "test_init_delay_seconds": cfg.test_init_delay_seconds,
            }
            init_frame = encode_frame(make_message("init", request_id, gen, init_payload), cfg.ipc_max_bytes)
            self._send_bytes_with_deadline(parent_conn, init_frame, deadline)
            payload = self._wait_init_response(parent_conn, proc, deadline, request_id, gen)
        except SandboxClosedError:
            self._cleanup_failed_start(proc, parent_conn, job)
            self._discard_failed_start_runtime(proc, parent_conn, job)
            raise
        except SandboxStartupError:
            self._cleanup_failed_start(proc, parent_conn, job)
            self._discard_failed_start_runtime(proc, parent_conn, job)
            raise
        except _IpcSendTimeout:
            self._cleanup_failed_start(proc, parent_conn, job)
            self._discard_failed_start_runtime(proc, parent_conn, job)
            raise SandboxStartupError("sandbox worker init timed out") from None
        except SandboxProtocolError as exc:
            self._cleanup_failed_start(proc, parent_conn, job)
            self._discard_failed_start_runtime(proc, parent_conn, job)
            raise SandboxStartupError(f"sandbox worker init protocol error: {exc}") from None
        except Exception as exc:
            self._cleanup_failed_start(proc, parent_conn, job)
            self._discard_failed_start_runtime(proc, parent_conn, job)
            raise SandboxStartupError(f"sandbox worker start failed: {type(exc).__name__}: {exc}") from None

        # Runtime was made lifecycle-visible immediately after spawn; init_ok
        # only promotes its generation and metadata to executable state.
        self.generation = gen
        self.last_start_elapsed = time.monotonic() - t0

        self._registry_snapshot = payload.get("registry_snapshot") or {}
        self._detected_prefixes = list(payload.get("detected_prefixes") or [])
        self._prefixes_source = payload.get("prefixes_source") or "none"
        self._index_loaded = bool(payload.get("index_loaded"))
        self.index_warning = payload.get("index_warning")
        self._has_llm_tools = bool(payload.get("has_llm_tools"))
        logger.info(
            "sandbox worker gen=%d pid=%s started in %.1fs (index_loaded=%s llm=%s python=%s group=%s memlimit=%s)",
            gen,
            payload.get("pid"),
            self.last_start_elapsed,
            self._index_loaded,
            self._has_llm_tools,
            payload.get("python_version"),
            payload.get("process_group_detail"),
            payload.get("memory_limit_detail"),
        )
        if cfg.memory_mb > 0 and not payload.get("memory_limit_applied"):
            # Молча работать «без потолка», пока оператор считает, что он есть,
            # нельзя: суммарный бюджет памяти сервера перестаёт держаться (§11.2).
            logger.warning(
                "sandbox worker gen=%d: RLM_SANDBOX_MEMORY_MB=%d requested but NOT enforced (%s)",
                gen,
                cfg.memory_mb,
                payload.get("memory_limit_detail"),
            )

    def _wait_init_response(self, conn, proc, deadline: float, request_id: int, gen: int) -> dict:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SandboxStartupError(
                    f"sandbox worker init timed out after {self._cfg.start_timeout_seconds}s (RLM_SANDBOX_START_TIMEOUT_SECONDS)"
                )
            if not proc.is_alive() and not conn.poll(0):
                raise SandboxStartupError(f"sandbox worker exited during init (exitcode={proc.exitcode})")
            if not conn.poll(min(_POLL_SLICE_SECONDS, remaining)):
                continue
            try:
                raw = conn.recv_bytes(maxlength=self._cfg.ipc_max_bytes)
            except (EOFError, OSError):
                raise SandboxStartupError(f"sandbox worker closed IPC during init (exitcode={proc.exitcode})") from None
            msg = decode_frame(raw, self._cfg.ipc_max_bytes)
            msg_type, payload = validate_message(
                msg,
                allowed_types={"init_ok", "init_error", "worker_error"},
                expected_request_id=None,
                expected_generation=gen,
            )
            # Те же правила, что и в execute-протоколе: init_ok/init_error обязаны
            # нести id текущей команды, worker_error — его либо сигнальный
            # WORKER_FATAL_REQUEST_ID. Иначе stale/битый фрейм сошёл бы за
            # легитимный отказ инициализации.
            got_id = msg.get("request_id")
            if msg_type in ("init_ok", "init_error"):
                if got_id != request_id:
                    raise SandboxProtocolError(f"{msg_type} request_id mismatch: got {got_id}, expected {request_id}")
            elif got_id not in (request_id, _WORKER_FATAL_REQUEST_ID):
                raise SandboxProtocolError(
                    f"worker_error request_id invalid: got {got_id}, "
                    f"expected {request_id} or {_WORKER_FATAL_REQUEST_ID}"
                )
            if msg_type == "init_ok":
                return payload
            raise SandboxStartupError(f"sandbox worker init failed: {bounded_text(str(payload.get('error')))}")

    def _cleanup_failed_start(self, proc, conn, job) -> None:
        try:
            conn.close()
        except Exception:
            pass
        tree_signalled = False
        try:
            if job is not None:
                tree_signalled, _handle_closed = job.close(kill=True)
            elif os.name == "posix" and proc.pid:
                # Worker creates its own process group before reading init.  A
                # startup timeout/error may therefore happen after init code has
                # spawned descendants; killing only the root would orphan them.
                import signal as _signal

                try:
                    os.killpg(proc.pid, _signal.SIGKILL)
                    tree_signalled = True
                except ProcessLookupError:
                    # The group may not exist yet (worker has not reached
                    # setsid), in which case root-only termination is safe.
                    pass
                except (PermissionError, OSError) as exc:
                    logger.warning("failed-start killpg(%s) failed: %s", proc.pid, exc)
            if not tree_signalled and proc.is_alive():
                # Do not touch published-generation cleanup state: ``proc`` and
                # ``job`` are local startup handles until init succeeds.
                proc.terminate()
        except Exception:
            logger.warning("failed-start termination failed", exc_info=True)
        try:
            proc.join(max(1.0, float(self._cfg.kill_grace_seconds)))
            if proc.is_alive():
                if os.name == "posix" and proc.pid:
                    try:
                        import signal as _signal

                        os.killpg(proc.pid, _signal.SIGKILL)
                    except (ProcessLookupError, PermissionError, OSError):
                        pass
                proc.kill()
                proc.join(1.0)
        except Exception:
            pass
        # request_close() can race with Windows Job assignment: it sees the
        # already-published Process but not yet the local Job, so its root-only
        # fallback records this exact generation as unconfirmed.  A successful
        # close of the matching startup Job is the missing tree-wide proof.
        if tree_signalled and self._tree_cleanup_target is proc:
            self._mark_tree_cleanup_confirmed(proc)

    def _discard_failed_start_runtime(self, proc, conn, job) -> None:
        """Detach handles cleaned by the failed-start path without hiding a survivor."""
        try:
            root_alive = proc.is_alive()
        except Exception:
            root_alive = True
        job_handle_open = job is not None and bool(getattr(job, "_handle", None))
        with self._state_lock:
            if self._conn is conn:
                self._conn = None
            if self._job is job and not job_handle_open:
                self._job = None
            if self._proc is proc and not root_alive and not job_handle_open:
                self._proc = None

    # -- execute ------------------------------------------------------------

    def execute(self, code: str) -> BackendExecutionResult:
        cfg = self._cfg
        with self._state_lock:
            if self._state in ("closing", "closed"):
                raise SandboxClosedError(f"sandbox backend is {self._state} (reason: {self._close_reason})")
            need_restart = self._state == "dead"
            variables_snapshot = list(self._variables_snapshot)

        if need_restart:
            # Коротко синхронизируем подготовку старого runtime с reaper-ом.
            # Сам холодный spawn по-прежнему идёт БЕЗ _close_lock, но флаг не
            # даёт reaper-у финализировать backend и потерять новое поколение.
            with self._close_lock:
                with self._state_lock:
                    if self._state in ("closing", "closed") or self._finalized:
                        raise SandboxClosedError(f"sandbox backend is {self._state} (reason: {self._close_reason})")
                    self._restart_in_progress = True
                    self._state = "starting"
                try:
                    self._prepare_previous_generation_for_restart()
                except Exception:
                    with self._state_lock:
                        self._restart_in_progress = False
                        if self._state == "starting":
                            self._state = "dead"
                    raise
            # Lazy restart (§10.5): холодный старт оплачивает СЛЕДУЮЩИЙ execute.
            # Идёт ВНЕ _close_lock — иначе холодный старт (до start_timeout секунд)
            # заблокировал бы singleton-reaper.
            try:
                self._start_worker()
            except Exception:
                with self._state_lock:
                    self._restart_in_progress = False
                    closing = self._state in ("closing", "closed")
                    if not closing:
                        self._state = "dead"
                if closing:
                    raise SandboxClosedError(
                        f"sandbox backend was closed during restart (reason: {self._close_reason})"
                    ) from None
                raise
            # Гонка §9.3: пока шёл холодный старт, rlm_end/TTL могли отозвать backend,
            # а reaper — финализировать СТАРОЕ мёртвое поколение (_finalized=True,
            # _proc=None). Только что опубликованное новое поколение осталось бы
            # бесхозным: повторный finish_close коротко замкнулся бы на _finalized.
            # Поэтому под _close_lock атомарно проверяем отзыв и, если он был,
            # убиваем поднятое поколение здесь же.
            with self._close_lock:
                with self._state_lock:
                    self._restart_in_progress = False
                    revoked = self._state in ("closing", "closed") or self._finalized
                    if not revoked:
                        self._state = "alive"
                if revoked:
                    self._destroy_current_generation()
                    raise SandboxClosedError(
                        f"sandbox backend was closed during restart (reason: {self._close_reason})"
                    )
                self._pending_restarted_marker = {
                    "status": "restarted",
                    "reason": f"previous_{self.last_reset_reason}",
                    "state_lost": True,
                    "generation": self.generation,
                }

        with self._state_lock:
            if self._state in ("closing", "closed"):
                raise SandboxClosedError(f"sandbox backend is {self._state} (reason: {self._close_reason})")
            if len(code) > cfg.max_code_chars:
                # Reject in parent before IPC (§11.4), but only after honoring a
                # pending lazy restart.  As with an oversized IPC frame, this
                # response owns and consumes the restarted-generation marker.
                marker, self._pending_restarted_marker = self._pending_restarted_marker, None
                return BackendExecutionResult(
                    stdout="",
                    error=(
                        f"CodeTooLargeError: code length {len(code)} exceeds "
                        f"RLM_SANDBOX_MAX_CODE_CHARS={cfg.max_code_chars}"
                    ),
                    variables=variables_snapshot,
                    sandbox_state=marker,
                    generation=self.generation,
                )
            self._state = "executing"
        try:
            return self._execute_ipc(code)
        finally:
            with self._state_lock:
                if self._state == "executing":
                    self._state = "alive"

    def _execute_ipc(self, code: str) -> BackendExecutionResult:
        cfg = self._cfg
        conn = self._conn
        gen = self.generation
        request_id = next(self._request_ids)
        # Обнуление header shared buffer перед каждым execute — parent (§6.3).
        self._out_published.value = 0
        self._out_truncated.value = 0
        deadline = time.monotonic() + cfg.execution_timeout_seconds
        try:
            execute_frame = encode_frame(make_message("execute", request_id, gen, {"code": code}), cfg.ipc_max_bytes)
            self._send_bytes_with_deadline(conn, execute_frame, deadline)
        except _IpcSendTimeout:
            return self._handle_hard_timeout()
        except SandboxClosedError:
            raise
        except (OSError, ValueError, SandboxProtocolError) as exc:
            if isinstance(exc, SandboxProtocolError):
                # code сам по себе больше IPC-frame лимита — controlled error, worker жив.
                marker, self._pending_restarted_marker = self._pending_restarted_marker, None
                return BackendExecutionResult(
                    stdout="",
                    error=f"CodeTooLargeError: execute frame exceeds RLM_SANDBOX_IPC_MAX_BYTES={cfg.ipc_max_bytes}",
                    variables=list(self._variables_snapshot),
                    sandbox_state=marker,
                    generation=gen,
                )
            return self._handle_worker_loss("crash_or_resource_limit", f"IPC send failed: {type(exc).__name__}")

        while True:
            with self._state_lock:
                if self._state in ("closing", "closed"):
                    raise SandboxClosedError(f"sandbox backend is {self._state} (reason: {self._close_reason})")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return self._handle_hard_timeout()
            try:
                has_data = conn.poll(min(_POLL_SLICE_SECONDS, remaining))
            except (OSError, ValueError):
                with self._state_lock:
                    closing = self._state in ("closing", "closed")
                if closing:
                    raise SandboxClosedError(f"sandbox backend closed (reason: {self._close_reason})") from None
                return self._handle_worker_loss("crash_or_resource_limit", "IPC poll failed")
            if not has_data:
                continue
            try:
                raw = conn.recv_bytes(maxlength=cfg.ipc_max_bytes)
            except (EOFError, OSError):
                with self._state_lock:
                    closing = self._state in ("closing", "closed")
                if closing:
                    raise SandboxClosedError(f"sandbox backend closed (reason: {self._close_reason})") from None
                exitcode = self._proc.exitcode if self._proc is not None else None
                return self._handle_worker_loss(
                    "crash_or_resource_limit", f"worker exited unexpectedly (exitcode={exitcode})"
                )
            try:
                msg = decode_frame(raw, cfg.ipc_max_bytes)
                # request_id НЕ фиксируем на этом шаге: аварийный worker_error из
                # внешнего обработчика worker приходит с request_id=-1, и жёсткая
                # сверка исказила бы причину смерти в worker_error → protocol_error.
                msg_type, payload = validate_message(
                    msg,
                    allowed_types={"execute_result", "worker_error"},
                    expected_request_id=None,
                    expected_generation=gen,
                )
                got_id = msg.get("request_id")
                if msg_type == "execute_result":
                    if got_id != request_id:
                        raise SandboxProtocolError(
                            f"execute_result request_id mismatch: got {got_id}, expected {request_id}"
                        )
                elif got_id not in (request_id, _WORKER_FATAL_REQUEST_ID):
                    # Допустимы РОВНО два значения: id текущей команды либо
                    # сигнальный -1 из внешнего аварийного обработчика worker.
                    # Любое другое — protocol violation, а не «обычная авария»:
                    # иначе stale/битый фрейм молча сойдёт за крах worker.
                    raise SandboxProtocolError(
                        f"worker_error request_id invalid: got {got_id}, "
                        f"expected {request_id} or {_WORKER_FATAL_REQUEST_ID}"
                    )
            except SandboxProtocolError as exc:
                return self._handle_worker_loss("protocol_error", f"SandboxProtocolError: {exc}")
            if msg_type == "worker_error":
                return self._handle_worker_loss("worker_error", bounded_text(str(payload.get("error"))))
            try:
                return self._build_result(payload, gen)
            except SandboxProtocolError as exc:
                return self._handle_worker_loss("protocol_error", f"SandboxProtocolError: {exc}")

    def _build_result(self, payload: dict, gen: int) -> BackendExecutionResult:
        stdout, truncated = self._read_shared_stdout()
        if truncated:
            stdout += TRUNCATION_MARKER
        error = payload.get("error")
        if error is not None and not isinstance(error, str):
            raise SandboxProtocolError("execute_result.error is not a string")
        variables = payload.get("variables")
        if not isinstance(variables, list) or not all(isinstance(v, str) for v in variables):
            raise SandboxProtocolError("execute_result.variables is not a list of strings")
        raw_calls = payload.get("helper_calls")
        if raw_calls is None:
            raw_calls = []
        elif not isinstance(raw_calls, list):
            raise SandboxProtocolError("execute_result.helper_calls is not a list")
        helper_calls: list[HelperCall] = []
        for entry in raw_calls:
            if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
                raise SandboxProtocolError("execute_result.helper_calls entry malformed")
            # Строгая схема, а не приведение типов: float("1.5")/int(True) прошли
            # бы молча, дробный seq усёкся бы, NaN уехал бы в ответ. Проверяем
            # ИСХОДНЫЕ типы и конечность значений (bool исключаем явно — в Python
            # он подкласс int).
            raw_elapsed = entry.get("elapsed", 0.0)
            raw_seq = entry.get("seq", 0)
            if isinstance(raw_elapsed, bool) or not isinstance(raw_elapsed, (int, float)):
                raise SandboxProtocolError(f"helper_calls.elapsed must be a number, got {type(raw_elapsed).__name__}")
            try:
                # JSON-integer может быть произвольной длины: math.isfinite/float()
                # на 10**400 бросают OverflowError мимо SandboxProtocolError, и
                # worker не был бы классифицирован как нарушивший протокол.
                if not math.isfinite(raw_elapsed):
                    raise SandboxProtocolError("helper_calls.elapsed must be finite")
                elapsed = float(raw_elapsed)
            except OverflowError:
                raise SandboxProtocolError("helper_calls.elapsed out of representable range") from None
            if isinstance(raw_seq, bool) or not isinstance(raw_seq, int):
                raise SandboxProtocolError(f"helper_calls.seq must be an integer, got {type(raw_seq).__name__}")
            if not (0 <= raw_seq <= _MAX_SEQ):
                raise SandboxProtocolError(f"helper_calls.seq out of range: {raw_seq}")
            seq = raw_seq
            # duplicate_of: строго None либо неотрицательный bounded int. Прежняя
            # проверка пропускала bool (подкласс int), а любое иное неверное
            # значение молча превращала в None, пряча нарушение протокола.
            raw_dup = entry.get("duplicate_of")
            if raw_dup is None:
                duplicate_of = None
            elif isinstance(raw_dup, bool) or not isinstance(raw_dup, int):
                raise SandboxProtocolError(
                    f"helper_calls.duplicate_of must be null or an integer, got {type(raw_dup).__name__}"
                )
            elif not (0 <= raw_dup <= _MAX_SEQ):
                raise SandboxProtocolError(f"helper_calls.duplicate_of out of range: {raw_dup}")
            else:
                duplicate_of = raw_dup
            helper_calls.append(HelperCall(name=entry["name"], elapsed=elapsed, seq=seq, duplicate_of=duplicate_of))
        hints = payload.get("efficiency_hints")
        if hints is not None:
            if not isinstance(hints, list):
                raise SandboxProtocolError("execute_result.efficiency_hints is not a list")
            # Элементы тоже валидируем: completion-log делает h["id"], а response
            # сериализуется в JSON — мусорный элемент уронил бы уже сам ответ.
            for hint in hints:
                if not isinstance(hint, dict) or not isinstance(hint.get("id"), str):
                    raise SandboxProtocolError("execute_result.efficiency_hints entry malformed")
        self._variables_snapshot = list(variables)
        marker, self._pending_restarted_marker = self._pending_restarted_marker, None
        return BackendExecutionResult(
            stdout=stdout,
            error=error,
            variables=variables,
            helper_calls=helper_calls,
            efficiency_hints=hints,
            sandbox_state=marker,
            generation=gen,
        )

    # -- смерть worker: timeout/crash/protocol ------------------------------

    def _handle_hard_timeout(self) -> BackendExecutionResult:
        cfg = self._cfg
        self._terminate_current_worker(reason="timeout")
        stdout, truncated = self._read_shared_stdout()
        if truncated:
            stdout += TRUNCATION_MARKER
        stdout += TIMEOUT_PARTIAL_MARKER
        error = (
            f"TimeoutError: Execution timed out after {cfg.execution_timeout_seconds} seconds — "
            "sandbox worker was hard-killed by the server. All session variables and caches are lost; "
            "the next rlm_execute starts a fresh sandbox (previous partial stdout is included above)."
        )
        return BackendExecutionResult(
            stdout=stdout,
            error=error,
            variables=[],
            helper_calls=[],
            efficiency_hints=None,
            sandbox_state={
                "status": "terminated",
                "reason": "timeout",
                "state_lost": True,
                "restart": "on_next_execute",
            },
            generation=self.generation,
        )

    def _handle_worker_loss(self, reason: str, detail: str) -> BackendExecutionResult:
        self._terminate_current_worker(reason=reason)
        stdout, truncated = self._read_shared_stdout()
        if truncated:
            stdout += TRUNCATION_MARKER
        if stdout:
            stdout += CRASH_PARTIAL_MARKER
        if reason == "protocol_error":
            error = (
                f"SandboxProtocolError: sandbox worker violated the IPC protocol and was terminated "
                f"({bounded_text(detail, 500)}). Session variables are lost; next rlm_execute starts a fresh sandbox."
            )
        else:
            error = (
                f"SandboxCrashedError: sandbox worker terminated unexpectedly ({bounded_text(detail, 500)}). "
                "Possible cause: crash or resource limit (memory). Session variables are lost; "
                "next rlm_execute starts a fresh sandbox."
            )
        return BackendExecutionResult(
            stdout=stdout,
            error=error,
            variables=[],
            helper_calls=[],
            efficiency_hints=None,
            sandbox_state={
                "status": "terminated",
                "reason": reason,
                "state_lost": True,
                "restart": "on_next_execute",
            },
            generation=self.generation,
        )

    def _terminate_current_worker(self, reason: str) -> None:
        """Kill tree текущего поколения + перевод в dead (lazy restart)."""
        if reason not in _RESET_REASONS:
            reason = "crash_or_resource_limit"
        # Ответ этого вызова несёт собственный terminated-маркер, а следующий
        # restart соберёт свежий restarted-маркер. Не снятый здесь pending-маркер
        # дожил бы до ответа ЧУЖОГО поколения и соврал бы про generation (§10.6).
        self._pending_restarted_marker = None
        self._variables_snapshot = []
        proc = self._proc
        if proc is not None:
            if not self._kill_tree_raw(proc):
                # Флаг переживёт до finish_close: закрывать backend как успешный
                # при неподтверждённой очистке дерева нельзя.
                logger.warning("terminate worker: tree-wide kill НЕ подтверждён (pid=%s)", proc.pid)
            try:
                proc.join(max(1.0, float(self._cfg.kill_grace_seconds) + 1.0))
            except Exception:
                pass
        # После kill+join читаем raw counter (без старого lock — §12.2.6).
        self._sync_llm_quota()
        self._close_runtime_handles()
        with self._state_lock:
            if self._state not in ("closing", "closed"):
                self._state = "dead"
        self.last_reset_reason = reason
        logger.warning("sandbox worker gen=%d terminated (reason=%s)", self.generation, reason)

    def force_abort(self) -> bool:
        """Неблокирующее уничтожение дерева worker (§13.6, после общего deadline).

        НЕ берёт ``_close_lock`` блокирующе: им может владеть reaper со своим
        собственным deadline, и ожидание вывело бы shutdown далеко за общий
        бюджет.

        True возвращается ТОЛЬКО если выполнены оба условия: (1) подтверждена
        именно tree-wide операция (Job Object или killpg), а не убийство одного
        корневого процесса — иначе descendants могли пережить отказ WinAPI/killpg,
        и (2) cleanup действительно финализирован. Приравнивать «корень мёртв» к
        «дерева нет» нельзя: reaper снял бы backend с учёта при живых потомках.
        """
        proc = self._proc
        if proc is not None:
            try:
                # Убить дерево можно и без lifecycle lock: это revoke, handles
                # освобождает только владелец lock ниже.
                self._kill_tree_raw(proc, wait=False)
            except Exception:  # noqa: BLE001 — teardown не должен падать
                logger.warning("force_abort: kill tree failed", exc_info=True)
        # Даже если terminate не подтвердился, закрытие Windows Job под lock-ом
        # может подтвердить KILL_ON_JOB_CLOSE. Поэтому не выходим раньше времени.
        if not self._close_lock.acquire(blocking=False):
            return False  # владелец замка доделает сам
        try:
            return self._finish_close_locked(time.monotonic() - 1.0, no_wait=True).closed
        except Exception:  # noqa: BLE001
            logger.warning("force_abort: finalize failed", exc_info=True)
            return False
        finally:
            self._close_lock.release()

    def _mark_tree_cleanup_confirmed(self, proc) -> None:
        """Подтвердить cleanup только для указанного runtime-поколения."""
        target = self._tree_cleanup_target
        if target is not None and target is not proc:
            # Подтверждение нового поколения не имеет права скрыть unresolved
            # process group старого. Такой interleaving нарушает restart guard.
            logger.error(
                "tree cleanup confirmation belongs to another generation (pid=%s, unresolved_pid=%s)",
                getattr(proc, "pid", None),
                getattr(target, "pid", None),
            )
            return
        self._tree_cleanup_unconfirmed = False
        self._tree_cleanup_target = None
        self._tree_cleanup_confirmed_target = proc

    def _mark_tree_cleanup_unconfirmed(self, proc) -> None:
        """Сохранить конкретное поколение, для которого tree-kill не доказан."""
        target = self._tree_cleanup_target
        if target is not None and target is not proc:
            logger.error(
                "multiple unresolved worker generations (pid=%s, previous_pid=%s)",
                getattr(proc, "pid", None),
                getattr(target, "pid", None),
            )
            return
        self._tree_cleanup_unconfirmed = True
        self._tree_cleanup_target = proc
        self._tree_cleanup_confirmed_target = None

    def _kill_tree_raw(self, proc, wait: bool = True) -> bool:
        """Уничтожение дерева: Job Object (Windows) / process group (POSIX) /
        terminate+kill fallback.

        Возвращает True, только если отработала ПОДТВЕРЖДЁННАЯ tree-wide
        операция. Fallback убивает лишь корневой процесс, поэтому даёт False:
        вызывающий не имеет права считать дерево уничтоженным.

        ``wait=False`` запрещает любые join внутри fallback — нужен force-пути
        shutdown, который не имеет права добавлять время поверх общего deadline.
        """
        # Job принадлежит только опубликованному self._proc. Локальный proc из
        # failed startup нельзя случайно сопоставить с Job старого поколения.
        job = self._job if proc is self._proc else None
        if job is not None:
            try:
                job.terminate()
                self._mark_tree_cleanup_confirmed(proc)
                return True  # Job Object убивает всё дерево разом
            except Exception as exc:  # noqa: BLE001
                # terminate() теперь поднимает OSError при FALSE от WinAPI —
                # значит дерево НЕ убито и нужно идти в fallback, а не выходить.
                logger.warning("kill tree: Job Object terminate failed (%s) — переходим к fallback", exc)
        pid = proc.pid
        if os.name == "posix" and pid:
            import signal as _signal

            try:
                os.killpg(pid, _signal.SIGKILL)
                self._mark_tree_cleanup_confirmed(proc)
                return True  # своя process group = всё дерево
            except ProcessLookupError:
                self._mark_tree_cleanup_confirmed(proc)
                return True  # группы уже нет — дерево мертво
            except (PermissionError, OSError) as exc:
                logger.warning("kill tree: killpg(%s) failed (%s) — переходим к fallback", pid, exc)
        # Tree-wide операция уже была подтверждена для этого поколения, но root
        # ещё может кратко выглядеть alive до reap. Не понижаем подтверждение до
        # fallback-состояния только из-за повторного вызова после CloseHandle.
        already_confirmed = self._tree_cleanup_confirmed_target is proc
        try:
            proc.terminate()
            if wait:
                proc.join(0.5)
                if proc.is_alive():
                    proc.kill()
            else:
                # Без ожидания: сразу самый жёсткий сигнал, join не делаем.
                proc.kill()
        except Exception:
            logger.warning("kill tree: terminate/kill fallback failed for pid=%s", pid, exc_info=True)
        if already_confirmed:
            return True
        # Fallback достаёт ТОЛЬКО корневой процесс: судьба descendants неизвестна.
        self._mark_tree_cleanup_unconfirmed(proc)
        return False

    def _confirm_tree_cleanup(self) -> bool:
        """Повторная ПОДТВЕРЖДАЮЩАЯ tree-wide операция без fallback и без ожиданий.

        Нужна потому, что после первой неудачной попытки корневой процесс уже
        мёртв: обычная kill-ветка на повторе не выполнится, и без этой проверки
        backend закрылся бы как успешный при живых потомках.
        """
        if not self._tree_cleanup_unconfirmed:
            return True
        proc = self._tree_cleanup_target
        if proc is None:
            logger.error("tree cleanup marked unconfirmed without a target process")
            return False
        job = self._job if proc is self._proc else None
        if job is not None:
            try:
                job.terminate()
                self._mark_tree_cleanup_confirmed(proc)
                return True
            except Exception as exc:  # noqa: BLE001
                logger.warning("confirm tree cleanup: Job terminate failed (%s)", exc)
                return False
        if os.name == "posix" and proc.pid:
            import signal as _signal

            try:
                os.killpg(proc.pid, _signal.SIGKILL)
            except ProcessLookupError:
                pass  # группы уже нет — дерево мертво
            except (PermissionError, OSError) as exc:
                logger.warning("confirm tree cleanup: killpg failed (%s)", exc)
                return False
            self._mark_tree_cleanup_confirmed(proc)
            return True
        return False

    def _prepare_previous_generation_for_restart(self) -> None:
        """Не дать lazy restart перезаписать незакрытый runtime поколения N."""
        proc = self._proc
        if self._tree_cleanup_unconfirmed:
            self._confirm_tree_cleanup()
        handles_closed = self._close_runtime_handles()
        root_alive = proc is not None and proc.is_alive()
        if root_alive or self._tree_cleanup_unconfirmed or not handles_closed:
            raise SandboxStartupError(
                "previous sandbox worker cleanup is incomplete; lazy restart deferred to the next execute"
            )
        self._proc = None
        self._tree_cleanup_confirmed_target = None

    def _destroy_current_generation(self) -> None:
        """Убить и забыть текущее поколение. Вызывается ТОЛЬКО под ``_close_lock``.

        Нужен для гонки «restart ↔ teardown»: поколение, поднятое уже после
        финализации backend, никто больше не реапит, поэтому хоронить его обязан
        сам restart-путь."""
        proc = self._proc
        if proc is not None:
            self._kill_tree_raw(proc)
            try:
                proc.join(max(1.0, float(self._cfg.kill_grace_seconds)))
            except Exception:
                pass
        if self._tree_cleanup_unconfirmed:
            self._confirm_tree_cleanup()
        handles_closed = self._close_runtime_handles()
        root_alive = proc is not None and proc.is_alive()
        if root_alive or self._tree_cleanup_unconfirmed or not handles_closed:
            # _restart_in_progress удерживал backend в pending reaper-а. Не
            # финализируем его: следующий проход повторит tree/handle cleanup.
            logger.error("restart teardown incomplete — backend remains pending for reaper retry")
            with self._state_lock:
                self._state = "closing"
            self._finalized = False
            return
        self._proc = None
        self._tree_cleanup_confirmed_target = None
        with self._state_lock:
            self._state = "closed"
        self._finalized = True

    def _close_runtime_handles(self) -> bool:
        """Закрыть IPC/Job handles; False означает, что Job надо повторить."""
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        handles_closed = True
        job = self._job
        if job is not None:
            try:
                tree_confirmed, handle_closed = job.close(kill=True)
                job_proc = self._proc if self._proc is not None else self._tree_cleanup_target
                if tree_confirmed and job_proc is not None:
                    # Успешное закрытие Job — самостоятельное подтверждение
                    # tree-wide очистки: KILL_ON_JOB_CLOSE убивает дерево.
                    self._mark_tree_cleanup_confirmed(job_proc)
                if handle_closed:
                    self._job = None
                else:
                    handles_closed = False
            except Exception:
                logger.warning("close job handle failed", exc_info=True)
                handles_closed = False
        return handles_closed

    def _read_shared_stdout(self) -> tuple[str, bool]:
        """Частичный/полный stdout из shared buffer: единственный авторитет (§23.4).

        Публикация writer-а — bytes до счётчика, поэтому опубликованный префикс
        всегда целые UTF-8 символы; clamp к mapping обязателен (§6.3).
        """
        buf = self._out_buf
        published = self._out_published
        if buf is None or published is None:
            return "", False
        n = int(published.value)
        n = max(0, min(n, len(buf)))
        data = bytes(buf[:n])
        text = data.decode("utf-8", errors="replace")
        truncated = bool(self._out_truncated.value) if self._out_truncated is not None else False
        return text, truncated

    # -- lifecycle ----------------------------------------------------------

    def request_close(self, reason: str) -> None:
        """Мгновенный идемпотентный revoke (§9.3): executing → немедленный kill
        tree + закрытие канала (будит заблокированный execute); idle → только
        неблокирующая инициация graceful shutdown. Никаких join/ожиданий."""
        with self._state_lock:
            if self._state in ("closing", "closed"):
                return
            prev_state = self._state
            restart_in_progress = self._restart_in_progress
            self._state = "closing"
            self._close_reason = reason
        if prev_state in ("executing", "starting") or restart_in_progress:
            proc = self._proc
            if proc is not None:
                self._kill_tree_raw(proc, wait=False)
            conn = self._conn
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
        elif prev_state in ("alive", "dead"):
            conn = self._conn
            if conn is not None and prev_state == "alive":
                try:
                    conn.send_bytes(
                        encode_frame(
                            make_message("shutdown", next(self._request_ids), self.generation),
                            self._cfg.ipc_max_bytes,
                        )
                    )
                except Exception:
                    pass

    def finish_close(self, deadline: float) -> CloseReport:
        """Bounded завершение: короткий graceful для idle, затем kill tree и
        закрытие handles. Вызывается reaper-ом (или inline из pre-registration
        startup failure — §13.3)."""
        with self._close_lock:
            return self._finish_close_locked(deadline)

    def _finish_close_locked(self, deadline: float, no_wait: bool = False) -> CloseReport:
        """Тело закрытия. Вызывающий ОБЯЗАН держать ``_close_lock``.

        ``no_wait=True`` полностью запрещает join — используется force-путём
        shutdown, где даже 0.1с на backend суммируются в N × 0.1с поверх уже
        истёкшего общего deadline.
        """
        if self._finalized:
            return CloseReport(closed=True)
        with self._state_lock:
            if self._restart_in_progress:
                return CloseReport(
                    closed=False,
                    residual=True,
                    errors=["lazy restart is still publishing its worker generation"],
                )
        # Истёкший общий deadline автоматически означает zero-wait force path.
        # Иначе последовательный shutdown снова получил бы N × 0.1/0.5 секунды.
        no_wait = no_wait or time.monotonic() >= deadline
        report = CloseReport(closed=False)
        proc = self._proc
        if proc is not None and proc.is_alive():
            if not no_wait:
                budget = deadline - time.monotonic()
                grace = max(0.0, min(budget, float(self._cfg.kill_grace_seconds)))
                try:
                    proc.join(grace)
                except Exception:
                    pass
            if proc.is_alive():
                report.forced = True
                self._kill_tree_raw(proc, wait=not no_wait)
                try:
                    proc.join(0.0 if no_wait else max(0.1, min(max(deadline - time.monotonic(), 0.0), 2.0)))
                except Exception:
                    pass
        # POSIX process group может пережить graceful exit своего leader-а:
        # worker уже мёртв, а запущенный им background descendant всё ещё жив.
        # Windows эквивалентно дочищается ниже через KILL_ON_JOB_CLOSE.
        if (
            proc is not None
            and os.name == "posix"
            and not proc.is_alive()
            and self._tree_cleanup_confirmed_target is not proc
        ):
            self._kill_tree_raw(proc, wait=False)
        self._sync_llm_quota()
        # Флаг СОХРАНЯЕТСЯ между вызовами: после первой неудачной попытки корень
        # уже мёртв, kill-ветка выше не выполнится, и локальная переменная снова
        # оказалась бы True — backend закрылся бы при живых потомках.
        if self._tree_cleanup_unconfirmed:
            self._confirm_tree_cleanup()
        handles_closed = self._close_runtime_handles()
        if proc is not None and proc.is_alive():
            report.residual = True
            report.errors.append(f"worker pid={proc.pid} still alive after force kill")
            return report
        if self._tree_cleanup_unconfirmed:
            # Корень мёртв, но tree-wide операция так и не подтвердилась:
            # descendants могли выжить. Не закрываем backend — оставляем reaper-у,
            # иначе утечка процессов ушла бы из-под учёта.
            report.residual = True
            report.errors.append("tree-wide kill not confirmed — descendants may have survived")
            return report
        if not handles_closed:
            report.residual = True
            report.errors.append("Windows Job handle close failed — cleanup will be retried")
            return report
        self._proc = None
        self._tree_cleanup_confirmed_target = None
        with self._state_lock:
            self._state = "closed"
        self._finalized = True
        report.closed = True
        return report
