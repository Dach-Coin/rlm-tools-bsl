"""Запуск `git` с ограниченным по времени завершением (Windows: файловый capture + creation-time Job).

Проблема, которую решает модуль. `subprocess.run(capture_output=True, timeout=...)`
на Windows даёт две независимые поломки timeout-семантики:

1. **Caller не возвращается по timeout.** CPython после истечения timeout зовёт
   `process.kill()` (на Windows это `TerminateProcess` ТОЛЬКО для root) и затем
   снова блокирующий `communicate()` — уже без лимита. Пока хоть один потомок
   держит унаследованный конец pipe, EOF не приходит, и ожидание длится столько,
   сколько живёт потомок. Замер на synthetic-потомке: `capture_output=True` с
   `timeout=0.2` вернулся через ~1.31 с, файловый capture — через ~0.22 с.
2. **Настоящий Git продолжает работать.** `C:\\Program Files\\Git\\cmd\\git.exe` —
   launcher (~45 KiB), а не исполняющий бинарник. Точечный запуск даёт живое
   дерево `cmd\\git.exe -> mingw64\\bin\\git.exe -> usr\\bin\\sh.exe -> helper`;
   после `TerminateProcess(root)` все три потомка остаются живы, держат
   repo/file handles, жгут CPU и накапливаются от timeout к timeout внутри одной
   долгоживущей sandbox-сессии.

Это не две реализации одной гарантии, а два разных отказа, поэтому нужны оба
механизма:

* **обычные файлы вместо `PIPE`** — ожидание root не зависит от EOF потомков;
* **creation-time Job** (`PROC_THREAD_ATTRIBUTE_JOB_LIST`) — root попадает в Job
  АТОМАРНО при создании, поэтому `TerminateJobObject` / `KILL_ON_JOB_CLOSE`
  сносит всё дерево. Post-spawn `AssignProcessToJobObject` здесь не годится:
  `cmd\\git.exe` создаёт настоящий Git сразу после старта, назначение Job не
  ретроактивно для уже ушедшего потомка.

Внешний Job песочницы (`sandbox_process._WindowsJob`) внутренний не заменяет: он
принадлежит всей сессии, и убивать его на timeout одного `git_search` означало
бы потерять worker. Job'ы вкладываются (nested jobs, Windows 8+), внутренний не
ослабляет limits внешнего — breakaway-флаги не используются.

Деградация честная: если creation-time Job недоступен, Git всё равно
запускается с файловым capture — caller остаётся bounded, но tree-wide kill в
этой ветке не обещается (осиротевший потомок может удерживать имя и дисковые
блоки временного файла до собственного выхода).

POSIX не меняется вовсе: там stdlib убивает process group корректно, а pipe-EOF
проблемы нет — вызов идёт прежним `subprocess.run(capture_output=True, ...)`.

Модуль — leaf: не импортирует `bsl_index`, sandbox и server. Любой отказ
подготовить capture-каталог/файлы/handles выходит наружу обычным `OSError`;
fallback на `PIPE` запрещён — callsite Git трактуют `OSError` как «Git-ускорение
недоступно» и уходят на свои существующие пути (`None` / полный скан /
`spawn_failed`).
"""

from __future__ import annotations

import contextlib
import logging
import os
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = [
    "CAPTURE_FILE_GLOB",
    "CAPTURE_FILE_PREFIX",
    "CAPTURE_FILE_SUFFIX",
    "GIT_CAPTURE_DIR_ENV",
    "SERVICE_CAPTURE_DIRNAME",
    "run_git",
]

#: Приватный service→child маркер каталога capture. НЕ пользовательская
#: настройка: значение выставляет только служба, и `_git_capture_dir()`
#: принимает его лишь при точном совпадении с `<parent RLM_CONFIG_FILE>/git-capture`.
GIT_CAPTURE_DIR_ENV = "_RLM_GIT_CAPTURE_DIR"

#: Имя защищённого подкаталога внутри config-root службы.
SERVICE_CAPTURE_DIRNAME = "git-capture"

#: Имя capture-файла: ``<PREFIX><stream>-<random><SUFFIX>``. ЕДИНЫЙ источник для
#: создания (`_open_capture`) и для уборки остатков при старте службы
#: (`_service_win._sweep_git_capture_dir`) — разъехавшись, они дали бы «уборку»,
#: которая ничего не находит.
CAPTURE_FILE_PREFIX = "rlm-tools-bsl-git-"
CAPTURE_FILE_SUFFIX = ".tmp"
#: Шаблон для перечисления остатков. Совпадает с именами выше по построению.
CAPTURE_FILE_GLOB = f"{CAPTURE_FILE_PREFIX}*{CAPTURE_FILE_SUFFIX}"

#: Seam для POSIX-ветки: тесты подменяют его, чтобы зафиксировать kwargs.
_run_posix = subprocess.run

#: Ограниченное ожидание выхода root после kill/terminate по timeout.
_REAP_AFTER_KILL_SECONDS = 2.0


def _normalize_newlines(text: str) -> str:
    """`\\r\\n` и одиночный `\\r` → `\\n` — как делал прежний ``text=True``."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _decode_capture(data: bytes) -> str:
    """UTF-8 с ``errors="replace"`` — прежнее поведение (cp1251 stderr git под службой)."""
    return _normalize_newlines(data.decode("utf-8", errors="replace"))


def _git_capture_dir() -> Path:
    """Каталог для временных capture-файлов Windows-ветки.

    Обычный CLI / ручной запуск сервера — системный temp.

    Под установленной службой (`RLM_UNDER_SERVICE=1`) — только защищённый
    каталог, подготовленный самой службой: требуются абсолютные
    `RLM_CONFIG_FILE` и `_RLM_GIT_CAPTURE_DIR`, а маркер обязан ТОЧНО совпасть с
    `<parent RLM_CONFIG_FILE>/git-capture`. Частично созданный или не прошедший
    DACL-проверку каталог маркера не получает, поэтому использован быть не может.
    Любое несовпадение — обычный `OSError`.
    """
    if os.environ.get("RLM_UNDER_SERVICE") != "1":
        return Path(tempfile.gettempdir())

    config_file = os.environ.get("RLM_CONFIG_FILE") or ""
    if not config_file or not os.path.isabs(config_file):
        raise OSError("git capture unavailable: RLM_CONFIG_FILE is unset or not absolute")

    marker = os.environ.get(GIT_CAPTURE_DIR_ENV) or ""
    if not marker or not os.path.isabs(marker):
        raise OSError("git capture unavailable: capture marker is unset or not absolute")

    expected = Path(config_file).parent / SERVICE_CAPTURE_DIRNAME
    if os.path.normcase(os.path.normpath(marker)) != os.path.normcase(os.path.normpath(str(expected))):
        raise OSError("git capture unavailable: capture marker does not match the service config root")
    return expected


def run_git(args: Sequence[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
    """Выполнить git-команду и вернуть `CompletedProcess` с раздельными stdout/stderr.

    Контракт (единый для обеих платформ):

    * декодирование UTF-8 с ``errors="replace"``, переводы строк нормализованы;
    * `returncode` возвращается как есть — парсеры и fallback-ветки callsite не меняются;
    * истечение *timeout* поднимает `subprocess.TimeoutExpired` БЕЗ частичного
      вывода (`stdout`/`stderr` у исключения — `None`): callsite его не читают;
    * отказ подготовить capture/handles выходит `OSError`; fallback на `PIPE` не делается.

    На Windows `stdin` ребёнка — `NUL`; на POSIX stdin, process group и весь
    lifecycle stdlib сохранены без изменений.
    """
    argv = [str(a) for a in args]
    if sys.platform != "win32":
        return _run_posix(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    return _run_windows(argv, timeout=timeout)


# ---------------------------------------------------------------------------
# Windows: WinAPI-адаптер одного git-вызова
# ---------------------------------------------------------------------------

if sys.platform == "win32":  # pragma: no cover - платформенная ветка
    import ctypes
    import msvcrt
    from ctypes import wintypes

    # ABI-декларации ОБЯЗАТЕЛЬНЫ: по умолчанию ctypes считает restype равным
    # c_int (32 бита), а HANDLE на x64 pointer-sized — возвращённый handle
    # усекался бы, и всё построенное на нём молча ломалось.
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.TerminateJobObject.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.GetCurrentProcess.argtypes = []
    _kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    _kernel32.DuplicateHandle.argtypes = [
        wintypes.HANDLE,
        wintypes.HANDLE,
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    ]
    _kernel32.DuplicateHandle.restype = wintypes.BOOL
    _kernel32.InitializeProcThreadAttributeList.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    _kernel32.InitializeProcThreadAttributeList.restype = wintypes.BOOL
    _kernel32.UpdateProcThreadAttribute.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    _kernel32.UpdateProcThreadAttribute.restype = wintypes.BOOL
    _kernel32.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
    _kernel32.DeleteProcThreadAttributeList.restype = None
    _kernel32.CreateProcessW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.BOOL,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.LPCWSTR,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    _kernel32.CreateProcessW.restype = wintypes.BOOL
    _kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _kernel32.WaitForSingleObject.restype = wintypes.DWORD
    _kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    _kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.TerminateProcess.restype = wintypes.BOOL

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
            # ULONG_PTR — pointer-sized, НЕ c_ulong: иначе на x64 съедет
            # разметка хвоста структуры.
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

    class _STARTUPINFOW(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("lpReserved", wintypes.LPWSTR),
            ("lpDesktop", wintypes.LPWSTR),
            ("lpTitle", wintypes.LPWSTR),
            ("dwX", wintypes.DWORD),
            ("dwY", wintypes.DWORD),
            ("dwXSize", wintypes.DWORD),
            ("dwYSize", wintypes.DWORD),
            ("dwXCountChars", wintypes.DWORD),
            ("dwYCountChars", wintypes.DWORD),
            ("dwFillAttribute", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("wShowWindow", wintypes.WORD),
            ("cbReserved2", wintypes.WORD),
            ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
            ("hStdInput", wintypes.HANDLE),
            ("hStdOutput", wintypes.HANDLE),
            ("hStdError", wintypes.HANDLE),
        ]

    class _STARTUPINFOEXW(ctypes.Structure):
        _fields_ = [
            ("StartupInfo", _STARTUPINFOW),
            ("lpAttributeList", ctypes.c_void_p),
        ]

    class _PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("hProcess", wintypes.HANDLE),
            ("hThread", wintypes.HANDLE),
            ("dwProcessId", wintypes.DWORD),
            ("dwThreadId", wintypes.DWORD),
        ]

    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    _JobObjectExtendedLimitInformation = 9

    _DUPLICATE_SAME_ACCESS = 0x00000002
    _STARTF_USESTDHANDLES = 0x00000100
    _EXTENDED_STARTUPINFO_PRESENT = 0x00080000
    # ProcThreadAttributeValue(Number, Thread=0, Input=1, Additive=0):
    # HandleList = 2 → 0x00020002, JobList = 13 → 0x0002000D.
    _PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
    _PROC_THREAD_ATTRIBUTE_JOB_LIST = 0x0002000D

    _STILL_ACTIVE = 259
    _WAIT_OBJECT_0 = 0x00000000
    _WAIT_TIMEOUT = 0x00000102
    _WAIT_FAILED = 0xFFFFFFFF
    _INFINITE = 0xFFFFFFFF

    def _win_error(prefix: str) -> OSError:
        err = ctypes.WinError(ctypes.get_last_error())
        return OSError(f"{prefix}: {err}")

    def _close_handle(handle: int | None, what: str) -> bool:
        """Идемпотентное закрытие; неудача логируется без argv/путей."""
        if not handle:
            return True
        if _kernel32.CloseHandle(wintypes.HANDLE(handle)):
            return True
        logger.info("run_git: CloseHandle(%s) failed: %s", what, ctypes.WinError(ctypes.get_last_error()))
        return False

    class _WindowsKillJob:
        """Внутренний Job одного git-вызова: только `KILL_ON_JOB_CLOSE` и terminate.

        Намеренно НЕ переиспользует `sandbox_process._WindowsJob`: тот владеет
        sandbox-worker вместе с его memory/active-process limits и живёт всю
        сессию, а этот — ровно один вызов. UI restrictions и breakaway не
        задаются, поэтому вложение во внешний Job песочницы совместимо и не
        ослабляет его ограничений.
        """

        def __init__(self) -> None:
            handle = _kernel32.CreateJobObjectW(None, None)
            if not handle:
                raise _win_error("CreateJobObjectW failed")
            self._handle: int | None = handle
            info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            ok = _kernel32.SetInformationJobObject(
                wintypes.HANDLE(handle),
                _JobObjectExtendedLimitInformation,
                ctypes.byref(info),
                ctypes.sizeof(info),
            )
            if not ok:
                exc = _win_error("SetInformationJobObject failed")
                self.close()
                raise exc

        @property
        def handle(self) -> int | None:
            return self._handle

        def terminate(self) -> bool:
            """`TerminateJobObject` по всему дереву; `False` — вызывающий обязан убить root сам."""
            if not self._handle:
                return False
            if _kernel32.TerminateJobObject(wintypes.HANDLE(self._handle), 1):
                return True
            logger.info("run_git: TerminateJobObject failed: %s", ctypes.WinError(ctypes.get_last_error()))
            return False

        def close(self) -> None:
            """Идемпотентно. `KILL_ON_JOB_CLOSE` доубивает всё, что ещё назначено Job."""
            if not self._handle:
                return
            if _close_handle(self._handle, "job"):
                self._handle = None

    class _WindowsProcess:
        """Минимальный процесс-объект над `hProcess`: ровно то, что нужно `run_git`."""

        def __init__(self, args: list[str], handle: int, pid: int) -> None:
            self.args = args
            self.pid = pid
            self.returncode: int | None = None
            self._handle: int | None = handle

        def poll(self) -> int | None:
            if self.returncode is not None or not self._handle:
                return self.returncode
            code = wintypes.DWORD()
            if not _kernel32.GetExitCodeProcess(wintypes.HANDLE(self._handle), ctypes.byref(code)):
                raise _win_error("GetExitCodeProcess failed")
            if code.value == _STILL_ACTIVE:
                return None
            self.returncode = int(code.value)
            return self.returncode

        def wait(self, timeout: float | None = None) -> int:
            """Ждать выхода; по истечении *timeout* — `subprocess.TimeoutExpired` без вывода."""
            if self.returncode is not None:
                return self.returncode
            if not self._handle:
                raise ValueError("process handle is already closed")
            ms = _INFINITE if timeout is None else max(0, int(timeout * 1000))
            rc = _kernel32.WaitForSingleObject(wintypes.HANDLE(self._handle), ms)
            if rc == _WAIT_TIMEOUT:
                raise subprocess.TimeoutExpired(self.args, timeout)
            if rc == _WAIT_FAILED:
                raise _win_error("WaitForSingleObject failed")
            if rc != _WAIT_OBJECT_0:
                raise OSError(f"WaitForSingleObject returned unexpected 0x{rc:08X}")
            code = wintypes.DWORD()
            if not _kernel32.GetExitCodeProcess(wintypes.HANDLE(self._handle), ctypes.byref(code)):
                raise _win_error("GetExitCodeProcess failed")
            self.returncode = int(code.value)
            return self.returncode

        def kill(self) -> bool:
            if not self._handle or self.returncode is not None:
                return True
            if _kernel32.TerminateProcess(wintypes.HANDLE(self._handle), 1):
                return True
            logger.info("run_git: TerminateProcess failed: %s", ctypes.WinError(ctypes.get_last_error()))
            return False

        def close(self) -> None:
            if not self._handle:
                return
            if _close_handle(self._handle, "process"):
                self._handle = None

    class _ChildStdio:
        """Три inheritable-хэндла ребёнка: `NUL` на stdin и дубликаты capture-файлов.

        Дубликаты делаются `DuplicateHandle(..., bInheritHandle=TRUE,
        DUPLICATE_SAME_ACCESS)` — file pointer у них ОБЩИЙ с родительскими file
        object. Это безопасно: родитель не делает `seek`/`read`, пока root не
        завершился и Job не закрыт, а по timeout capture не читается вовсе.
        """

        __slots__ = ("stderr", "stdin", "stdout")

        def __init__(self, out_file, err_file) -> None:
            self.stdin: int | None = None
            self.stdout: int | None = None
            self.stderr: int | None = None
            try:
                self.stdin = _open_inheritable_nul()
                self.stdout = _dup_inheritable(msvcrt.get_osfhandle(out_file.fileno()))
                self.stderr = _dup_inheritable(msvcrt.get_osfhandle(err_file.fileno()))
            except BaseException:
                self.close()
                raise

        def handles(self) -> tuple[int, int, int]:
            if not (self.stdin and self.stdout and self.stderr):
                raise OSError("child stdio handles are not available")
            return (self.stdin, self.stdout, self.stderr)

        def close(self) -> None:
            """Идемпотентно: родительские копии больше не нужны сразу после create."""
            for name in ("stdin", "stdout", "stderr"):
                handle = getattr(self, name)
                if handle and _close_handle(handle, f"child {name}"):
                    setattr(self, name, None)

        def __enter__(self) -> _ChildStdio:
            return self

        def __exit__(self, *exc_info) -> None:
            self.close()

    def _dup_inheritable(handle: int) -> int:
        """Inheritable-дубликат уже открытого хэндла родителя."""
        current = _kernel32.GetCurrentProcess()
        target = wintypes.HANDLE()
        ok = _kernel32.DuplicateHandle(
            current,
            wintypes.HANDLE(handle),
            current,
            ctypes.byref(target),
            0,
            True,
            _DUPLICATE_SAME_ACCESS,
        )
        if not ok:
            raise _win_error("DuplicateHandle failed")
        return int(target.value or 0)

    def _open_inheritable_nul() -> int:
        """Read-only `NUL` как stdin ребёнка (прежний inherited stdin не передаётся)."""
        fd = os.open(os.devnull, os.O_RDONLY)
        try:
            return _dup_inheritable(msvcrt.get_osfhandle(fd))
        finally:
            os.close(fd)

    def _open_capture(capture_dir: Path, stream: str):
        """Один auto-delete capture-файл. Имя уникально (`NamedTemporaryFile`), никаких общих файлов."""
        return tempfile.NamedTemporaryFile(  # noqa: SIM115 - владение передаётся вызывающему ExitStack
            mode="w+b",
            dir=str(capture_dir),
            prefix=f"{CAPTURE_FILE_PREFIX}{stream}-",
            suffix=CAPTURE_FILE_SUFFIX,
            delete=True,
        )

    def _create_process_common(
        argv: list[str],
        command_line: str,
        child: _ChildStdio,
        job_handle: int | None,
    ) -> _WindowsProcess:
        """Единственный низкоуровневый `CreateProcessW`. Audit здесь НЕ повторяется."""
        attr_count = 1 if job_handle is None else 2
        size = ctypes.c_size_t(0)
        _kernel32.InitializeProcThreadAttributeList(None, attr_count, 0, ctypes.byref(size))
        if size.value == 0:
            raise _win_error("InitializeProcThreadAttributeList(size) failed")
        buf = ctypes.create_string_buffer(size.value)
        buf_ptr = ctypes.cast(buf, ctypes.c_void_p)
        if not _kernel32.InitializeProcThreadAttributeList(buf_ptr, attr_count, 0, ctypes.byref(size)):
            raise _win_error("InitializeProcThreadAttributeList failed")
        try:
            # backing-массивы обязаны жить до возврата CreateProcessW —
            # UpdateProcThreadAttribute сохраняет УКАЗАТЕЛЬ, а не копию.
            h_stdin, h_stdout, h_stderr = child.handles()
            handle_array = (wintypes.HANDLE * 3)(h_stdin, h_stdout, h_stderr)
            ok = _kernel32.UpdateProcThreadAttribute(
                buf_ptr,
                0,
                _PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
                ctypes.cast(handle_array, ctypes.c_void_p),
                ctypes.sizeof(handle_array),
                None,
                None,
            )
            if not ok:
                raise _win_error("UpdateProcThreadAttribute(HANDLE_LIST) failed")

            job_array = None
            if job_handle is not None:
                job_array = (wintypes.HANDLE * 1)(job_handle)
                ok = _kernel32.UpdateProcThreadAttribute(
                    buf_ptr,
                    0,
                    _PROC_THREAD_ATTRIBUTE_JOB_LIST,
                    ctypes.cast(job_array, ctypes.c_void_p),
                    ctypes.sizeof(job_array),
                    None,
                    None,
                )
                if not ok:
                    raise _win_error("UpdateProcThreadAttribute(JOB_LIST) failed")

            si = _STARTUPINFOEXW()
            si.StartupInfo.cb = ctypes.sizeof(_STARTUPINFOEXW)
            si.StartupInfo.dwFlags = _STARTF_USESTDHANDLES
            si.StartupInfo.hStdInput = h_stdin
            si.StartupInfo.hStdOutput = h_stdout
            si.StartupInfo.hStdError = h_stderr
            si.lpAttributeList = buf_ptr

            pi = _PROCESS_INFORMATION()
            # Каждый create получает СВОЙ mutable buffer: CreateProcessW вправе
            # писать в lpCommandLine, и переиспользование испортило бы retry.
            cmd_buffer = ctypes.create_unicode_buffer(command_line)
            created = _kernel32.CreateProcessW(
                argv[0],
                cmd_buffer,
                None,
                None,
                True,
                _EXTENDED_STARTUPINFO_PRESENT,
                None,
                None,
                ctypes.byref(si),
                ctypes.byref(pi),
            )
            if not created:
                raise _win_error("CreateProcessW failed")
        finally:
            _kernel32.DeleteProcThreadAttributeList(buf_ptr)

        _close_handle(int(pi.hThread or 0), "thread")
        return _WindowsProcess(argv, int(pi.hProcess or 0), int(pi.dwProcessId))

    def _create_process_in_job(
        argv: list[str], command_line: str, child: _ChildStdio, job_handle: int
    ) -> _WindowsProcess:
        """Seam: root создаётся АТОМАРНО внутри Job (`PROC_THREAD_ATTRIBUTE_JOB_LIST`)."""
        return _create_process_common(argv, command_line, child, job_handle)

    def _create_process_without_job(argv: list[str], command_line: str, child: _ChildStdio) -> _WindowsProcess:
        """Seam деградировавшей ветки: caller bounded, tree-wide kill не обещается."""
        return _create_process_common(argv, command_line, child, None)

    def _spawn_root(
        argv: list[str], command_line: str, child: _ChildStdio
    ) -> tuple[_WindowsProcess, _WindowsKillJob | None]:
        """Один Job-запуск; при доуспешном отказе — РОВНО один no-Job запуск."""
        job: _WindowsKillJob | None = None
        try:
            job = _WindowsKillJob()
        except OSError as exc:
            logger.info("run_git: creation-time Job unavailable (%s); tree kill not guaranteed", type(exc).__name__)
            job = None

        if job is not None and job.handle is not None:
            try:
                return _create_process_in_job(argv, command_line, child, job.handle), job
            except OSError as exc:
                # Ошибка ДО появления process handle: Job закрываем и пробуем
                # один раз без него. Успешный create никогда не повторяется.
                logger.info("run_git: create in Job failed (%s); retrying without Job", type(exc).__name__)
                job.close()
                job = None

        return _create_process_without_job(argv, command_line, child), None

    def _terminate_tree(process: _WindowsProcess, job: _WindowsKillJob | None) -> None:
        """Job — целиком; без Job (или при отказе `TerminateJobObject`) — только root."""
        if job is not None and job.terminate():
            return
        process.kill()

    def _run_windows(argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
        command_line = subprocess.list2cmdline(argv)
        capture_dir = _git_capture_dir()

        with contextlib.ExitStack() as stack:
            # Порядок важен: файлы закрываются ПОСЛЕДНИМИ — после Job и process.
            out_file = stack.enter_context(_open_capture(capture_dir, "stdout"))
            err_file = stack.enter_context(_open_capture(capture_dir, "stderr"))
            child = stack.enter_context(_ChildStdio(out_file, err_file))

            # Ровно один совместимый audit event на вызов; низкоуровневые
            # creator-ы его не повторяют, а исключение из hook не маскируется.
            sys.audit("subprocess.Popen", None, command_line, None, None)

            process, job = _spawn_root(argv, command_line, child)
            if job is not None:
                stack.callback(job.close)
            stack.callback(process.close)
            # Родительские копии child-хэндлов больше не нужны: ребёнок и его
            # потомки держат собственные ссылки на те же file objects.
            child.close()

            try:
                returncode = process.wait(timeout)
            except subprocess.TimeoutExpired:
                _terminate_tree(process, job)
                with contextlib.suppress(subprocess.TimeoutExpired, OSError):
                    process.wait(timeout=_REAP_AFTER_KILL_SECONDS)
                raise  # исходный TimeoutExpired, без частичного вывода
            except BaseException:
                _terminate_tree(process, job)
                raise

            # Happy path: Job закрывается ДО чтения capture — поддержанный Git
            # завершает writers до выхода root, поэтому файлы уже полны.
            if job is not None:
                job.close()
            out_file.seek(0)
            stdout = _decode_capture(out_file.read())
            err_file.seek(0)
            stderr = _decode_capture(err_file.read())
            return subprocess.CompletedProcess(argv, returncode, stdout, stderr)
