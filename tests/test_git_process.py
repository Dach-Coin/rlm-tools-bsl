"""Контракт ``_git_process.run_git`` и Windows-ветка (файловый capture + creation-time Job).

Тесты разделены на три группы:

* платформо-независимый контракт (POSIX-seam, tripwire по callsite, логи);
* Windows-ветка: quoting/audit/handles/Job и достижимый descendant-сценарий;
* служба: приватный capture-каталог, маркер и деградация без него.

Ключевая проверка группы descendant — РАЗНИЦА между двумя ветками:
с creation-time Job после timeout умирает всё дерево, без Job caller всё равно
возвращается вовремя, но потомок остаётся жив (и держит capture-файл).
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from rlm_tools_bsl import _git_process as gp

WINDOWS = sys.platform == "win32"
windows_only = pytest.mark.skipif(not WINDOWS, reason="Windows-ветка runner-а")

# Ребёнок печатает в оба потока и завершается заданным кодом. Пишем в
# ``buffer`` явным UTF-8: текстовые потоки дочернего Python кодируют кириллицу
# ANSI-кодировкой, а контракт runner-а (как и прежний ``encoding="utf-8"``)
# рассчитан на UTF-8, который git и выдаёт.
_ECHO = (
    "import sys\n"
    "sys.stdout.buffer.write({out!r}.encode('utf-8'))\n"
    "sys.stderr.buffer.write({err!r}.encode('utf-8'))\n"
    "sys.exit({rc})\n"
)

# Ребёнок порождает потомка, оба наследуют capture, и оба долго спят.
# PID'ы кладутся в файл — по timeout capture не читается, взять их оттуда нельзя.
_SPAWN_DESCENDANT = (
    "import json, os, subprocess, sys, time\n"
    "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
    "open({marker!r}, 'w').write(json.dumps([os.getpid(), p.pid]))\n"
    "time.sleep(60)\n"
)


# ---------------------------------------------------------------------------
# WinAPI-помощники тестов
# ---------------------------------------------------------------------------
if WINDOWS:
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _k32.GetExitCodeProcess.restype = wintypes.BOOL
    _k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _k32.TerminateProcess.restype = wintypes.BOOL
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.CloseHandle.restype = wintypes.BOOL
    _k32.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _k32.QueryInformationJobObject.restype = wintypes.BOOL

    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _PROCESS_TERMINATE = 0x0001
    _STILL_ACTIVE = 259
    _JobObjectBasicProcessIdList = 3
    _JobObjectExtendedLimitInformation = 9

    class _PID_LIST(ctypes.Structure):
        _fields_ = [
            ("NumberOfAssignedProcesses", wintypes.DWORD),
            ("NumberOfProcessIdsInList", wintypes.DWORD),
            ("ProcessIdList", ctypes.c_size_t * 128),
        ]

    def pid_alive(pid: int) -> bool:
        handle = _k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not _k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == _STILL_ACTIVE
        finally:
            _k32.CloseHandle(handle)

    def hard_kill(pid: int) -> None:
        handle = _k32.OpenProcess(_PROCESS_TERMINATE, False, pid)
        if handle:
            _k32.TerminateProcess(handle, 1)
            _k32.CloseHandle(handle)

    def job_pids(job_handle: int) -> list[int]:
        info = _PID_LIST()
        returned = wintypes.DWORD()
        ok = _k32.QueryInformationJobObject(
            wintypes.HANDLE(job_handle),
            _JobObjectBasicProcessIdList,
            ctypes.byref(info),
            ctypes.sizeof(info),
            ctypes.byref(returned),
        )
        if not ok:
            return []
        return [int(info.ProcessIdList[i]) for i in range(info.NumberOfProcessIdsInList)]

    def job_limits(job_handle: int) -> tuple[int, int]:
        info = gp._JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        returned = wintypes.DWORD()
        ok = _k32.QueryInformationJobObject(
            wintypes.HANDLE(job_handle),
            _JobObjectExtendedLimitInformation,
            ctypes.byref(info),
            ctypes.sizeof(info),
            ctypes.byref(returned),
        )
        assert ok, "QueryInformationJobObject(ExtendedLimit) failed"
        return (
            int(info.BasicLimitInformation.LimitFlags),
            int(info.BasicLimitInformation.ActiveProcessLimit),
        )

    def wait_all_dead(pids: list[int], seconds: float = 10.0) -> list[bool]:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and any(pid_alive(p) for p in pids):
            time.sleep(0.05)
        return [pid_alive(p) for p in pids]


def _capture_spy(monkeypatch) -> list:
    """Записать все открытые capture-файлы, не меняя поведения."""
    opened: list = []
    real = gp._open_capture

    def spy(capture_dir, stream):
        handle = real(capture_dir, stream)
        opened.append(handle)
        return handle

    monkeypatch.setattr(gp, "_open_capture", spy)
    return opened


def _create_spy(monkeypatch) -> dict:
    """Счётчики обеих creator-веток + собранные PID/Job."""
    calls = {"in_job": 0, "no_job": 0, "pids": [], "jobs": []}
    real_in_job = gp._create_process_in_job
    real_no_job = gp._create_process_without_job

    def in_job(argv, command_line, child, job_handle):
        calls["in_job"] += 1
        calls["jobs"].append(job_handle)
        proc = real_in_job(argv, command_line, child, job_handle)
        calls["pids"].append(proc.pid)
        return proc

    def no_job(argv, command_line, child):
        calls["no_job"] += 1
        proc = real_no_job(argv, command_line, child)
        calls["pids"].append(proc.pid)
        return proc

    monkeypatch.setattr(gp, "_create_process_in_job", in_job)
    monkeypatch.setattr(gp, "_create_process_without_job", no_job)
    return calls


def _force_no_job(monkeypatch) -> None:
    """Доуспешный отказ Job-ветки: единственный путь — `_create_process_without_job`."""

    def boom(*_args, **_kwargs):
        raise OSError("forced: creation-time Job unavailable")

    monkeypatch.setattr(gp, "_create_process_in_job", boom)


# ---------------------------------------------------------------------------
# Контракт runner-а
# ---------------------------------------------------------------------------
class TestRunnerContract:
    def test_happy_path_separates_streams_and_rc(self):
        code = _ECHO.format(out="СТДАУТ\nsecond", err="СТДЕРР", rc=7)
        r = gp.run_git([sys.executable, "-c", code], timeout=30)
        assert r.returncode == 7
        assert r.args == [sys.executable, "-c", code]
        assert "СТДАУТ" in r.stdout
        assert "second" in r.stdout
        assert r.stderr == "СТДЕРР"
        assert "СТДЕРР" not in r.stdout

    def test_newline_parity_with_text_mode(self):
        """`\\r\\n` и одиночный `\\r` → `\\n` — как при прежнем ``text=True``."""
        code = "import sys; sys.stdout.buffer.write(b'a\\r\\nb\\rc\\n')"
        ours = gp.run_git([sys.executable, "-c", code], timeout=30)
        stdlib = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        assert ours.stdout == stdlib.stdout == "a\nb\nc\n"

    def test_invalid_utf8_is_replaced_not_raised(self):
        code = "import sys; sys.stdout.buffer.write(b'ok\\xff\\xfetail')"
        r = gp.run_git([sys.executable, "-c", code], timeout=30)
        assert r.returncode == 0
        assert r.stdout.startswith("ok")
        assert r.stdout.endswith("tail")
        assert "�" in r.stdout

    def test_unicode_spaces_quotes_and_empty_arg_survive_quoting(self):
        args = ["значение с пробелом", '"кавычки"', "", "a\\b"]
        code = "import json,sys; sys.stdout.buffer.write(json.dumps(sys.argv[1:]).encode('utf-8'))"
        r = gp.run_git([sys.executable, "-c", code, *args], timeout=30)
        assert r.returncode == 0, r.stderr
        assert json.loads(r.stdout) == args

    def test_timeout_raises_without_partial_output(self):
        with pytest.raises(subprocess.TimeoutExpired) as excinfo:
            gp.run_git([sys.executable, "-c", "import time; time.sleep(30)"], timeout=1.0)
        assert excinfo.value.stdout is None
        assert excinfo.value.stderr is None

    @windows_only
    def test_windows_stdin_is_nul(self):
        code = "import sys; sys.stdout.write('STDIN=%r' % sys.stdin.read())"
        r = gp.run_git([sys.executable, "-c", code], timeout=30)
        assert r.stdout == "STDIN=''"

    @pytest.mark.skipif(WINDOWS, reason="POSIX-ветка")
    def test_posix_delegates_to_subprocess_run_with_unchanged_kwargs(self, monkeypatch):
        seen: dict = {}

        def spy(argv, **kwargs):
            seen["argv"] = argv
            seen["kwargs"] = kwargs
            return subprocess.CompletedProcess(argv, 0, "", "")

        monkeypatch.setattr(gp, "_run_posix", spy)
        gp.run_git(["git", "status"], timeout=12.5)
        assert seen["argv"] == ["git", "status"]
        assert seen["kwargs"] == {
            "capture_output": True,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "timeout": 12.5,
        }
        # stdin / process group / creationflags не трогаются вовсе.
        for forbidden in ("stdin", "start_new_session", "preexec_fn", "creationflags"):
            assert forbidden not in seen["kwargs"]


# ---------------------------------------------------------------------------
# Windows: подготовка capture, handles, audit, Job
# ---------------------------------------------------------------------------
@windows_only
class TestWindowsSetup:
    def test_missing_capture_dir_raises_oserror_without_spawning(self, tmp_path, monkeypatch):
        monkeypatch.setattr(gp, "_git_capture_dir", lambda: tmp_path / "нет-такого")
        calls = _create_spy(monkeypatch)
        with pytest.raises(OSError):
            gp.run_git([sys.executable, "-c", "pass"], timeout=10)
        assert calls["in_job"] == 0 and calls["no_job"] == 0

    @pytest.mark.parametrize("fail_on", [1, 2])
    def test_capture_open_failure_closes_what_was_opened(self, monkeypatch, fail_on):
        opened: list = []
        real = gp._open_capture
        state = {"n": 0}

        def flaky(capture_dir, stream):
            state["n"] += 1
            if state["n"] == fail_on:
                raise OSError("forced capture failure")
            handle = real(capture_dir, stream)
            opened.append(handle)
            return handle

        monkeypatch.setattr(gp, "_open_capture", flaky)
        calls = _create_spy(monkeypatch)

        with pytest.raises(OSError, match="forced capture failure"):
            gp.run_git([sys.executable, "-c", "pass"], timeout=10)

        assert len(opened) == fail_on - 1
        assert all(f.closed for f in opened), "уже открытый capture обязан быть закрыт"
        assert calls["in_job"] == 0 and calls["no_job"] == 0, "fallback на PIPE запрещён, spawn не делается"

    def test_child_handle_failure_closes_both_capture_files(self, monkeypatch):
        opened = _capture_spy(monkeypatch)
        calls = _create_spy(monkeypatch)

        def boom(*_args, **_kwargs):
            raise OSError("forced handle failure")

        monkeypatch.setattr(gp, "_ChildStdio", boom)
        with pytest.raises(OSError, match="forced handle failure"):
            gp.run_git([sys.executable, "-c", "pass"], timeout=10)

        assert len(opened) == 2
        assert all(f.closed for f in opened)
        assert calls["in_job"] == 0 and calls["no_job"] == 0

    def test_child_handles_are_inheritable_duplicates_same_access(self, monkeypatch):
        seen: list[tuple] = []
        real_dup = gp._kernel32.DuplicateHandle

        def spy(src_proc, src, dst_proc, target, access, inherit, options):
            seen.append((access, bool(inherit), options))
            return real_dup(src_proc, src, dst_proc, target, access, inherit, options)

        monkeypatch.setattr(gp._kernel32, "DuplicateHandle", spy)
        r = gp.run_git([sys.executable, "-c", "print('ok')"], timeout=30)
        assert r.returncode == 0
        # ровно три: NUL-stdin, stdout, stderr
        assert len(seen) == 3
        for access, inherit, options in seen:
            assert inherit is True
            assert options == gp._DUPLICATE_SAME_ACCESS
            assert access == 0  # игнорируется при DUPLICATE_SAME_ACCESS

    def test_single_audit_event_and_no_retry_on_success(self, monkeypatch):
        events: list[tuple] = []
        real_audit = gp.sys.audit

        def spy(event, *args):
            if event == "subprocess.Popen":
                events.append(args)
                return None
            return real_audit(event, *args)

        monkeypatch.setattr(gp.sys, "audit", spy)
        calls = _create_spy(monkeypatch)
        r = gp.run_git([sys.executable, "-c", "print('ok')"], timeout=30)

        assert r.returncode == 0
        assert len(events) == 1, "audit шлётся ровно один раз за вызов, а не на каждый create"
        assert events[0][0] is None
        assert "-c" in events[0][1]
        assert calls["in_job"] == 1 and calls["no_job"] == 0, "успешный create не повторяется"

    def test_each_create_gets_a_fresh_command_buffer(self, monkeypatch):
        """CreateProcessW вправе писать в lpCommandLine — буфер не переиспользуется."""
        buffers: list[int] = []
        real_create = gp._kernel32.CreateProcessW

        def spy(app, cmd, *rest):
            buffers.append(id(cmd))
            return real_create(app, cmd, *rest)

        monkeypatch.setattr(gp._kernel32, "CreateProcessW", spy)
        _force_no_job(monkeypatch)  # чтобы Job-ветка не мешала счёту
        gp.run_git([sys.executable, "-c", "pass"], timeout=30)
        gp.run_git([sys.executable, "-c", "pass"], timeout=30)
        assert len(buffers) == 2

    def test_missing_executable_surfaces_first_winerror(self, monkeypatch):
        calls = _create_spy(monkeypatch)
        missing = str(Path(os.environ["SystemDrive"] + "\\") / "нет-такого-git" / "git.exe")
        with pytest.raises(OSError, match="CreateProcessW failed"):
            gp.run_git([missing, "rev-parse"], timeout=10)
        # один Job-запуск + ровно один no-Job fallback
        assert calls["in_job"] == 1
        assert calls["no_job"] == 1


@windows_only
class TestJobFaultInjection:
    def test_job_creation_failure_falls_back_exactly_once(self, monkeypatch):
        calls = _create_spy(monkeypatch)

        def boom():
            raise OSError("forced CreateJobObjectW failure")

        monkeypatch.setattr(gp, "_WindowsKillJob", boom)
        r = gp.run_git([sys.executable, "-c", "print('ok')"], timeout=30)
        assert r.returncode == 0
        assert calls["in_job"] == 0
        assert calls["no_job"] == 1

    def test_create_in_job_failure_falls_back_exactly_once(self, monkeypatch):
        calls = _create_spy(monkeypatch)
        monkeypatch.setattr(gp, "_create_process_in_job", lambda *a, **kw: (_ for _ in ()).throw(OSError("forced")))
        r = gp.run_git([sys.executable, "-c", "print('ok')"], timeout=30)
        assert r.returncode == 0
        assert calls["no_job"] == 1

    def test_no_job_timeout_is_still_bounded(self, monkeypatch):
        _force_no_job(monkeypatch)
        started = time.monotonic()
        with pytest.raises(subprocess.TimeoutExpired):
            gp.run_git([sys.executable, "-c", "import time; time.sleep(30)"], timeout=1.0)
        assert time.monotonic() - started < 10.0


# ---------------------------------------------------------------------------
# Достижимый descendant-сценарий: две ветки дают РАЗНЫЕ гарантии
# ---------------------------------------------------------------------------
@windows_only
class TestDescendantScenario:
    @staticmethod
    def _child_code(marker: Path) -> str:
        return _SPAWN_DESCENDANT.format(marker=str(marker))

    @staticmethod
    def _read_pids(marker: Path, seconds: float = 15.0) -> list[int]:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                return json.loads(marker.read_text())
            except (OSError, ValueError):
                time.sleep(0.05)
        raise AssertionError("ребёнок не записал PID'ы")

    def test_job_branch_kills_whole_tree(self, tmp_path, monkeypatch):
        marker = tmp_path / "pids.json"
        snapshot: dict = {}
        real_terminate = gp._WindowsKillJob.terminate

        def spy_terminate(self):
            # снимок ДО завершения: дерево ещё живо, membership наблюдаемо
            snapshot["pids"] = job_pids(self._handle)
            return real_terminate(self)

        monkeypatch.setattr(gp._WindowsKillJob, "terminate", spy_terminate)
        opened = _capture_spy(monkeypatch)

        pids: list[int] = []
        try:
            started = time.monotonic()
            with pytest.raises(subprocess.TimeoutExpired):
                gp.run_git([sys.executable, "-c", self._child_code(marker)], timeout=3.0)
            elapsed = time.monotonic() - started
            pids = self._read_pids(marker)

            assert elapsed < 10.0, "caller не ждёт EOF от потомка"
            assert set(pids) <= set(snapshot["pids"]), "root и потомок обязаны быть в creation-time Job"
            assert wait_all_dead(pids) == [False, False], "Job обязан снести всё дерево"
            for handle in opened:
                assert not Path(handle.name).exists(), "auto-delete сработал после закрытия последнего handle"
        finally:
            for pid in pids:
                hard_kill(pid)

    def test_no_job_branch_returns_bounded_but_leaves_descendant(self, tmp_path, monkeypatch):
        marker = tmp_path / "pids.json"
        _force_no_job(monkeypatch)
        opened = _capture_spy(monkeypatch)

        pids: list[int] = []
        try:
            started = time.monotonic()
            with pytest.raises(subprocess.TimeoutExpired):
                gp.run_git([sys.executable, "-c", self._child_code(marker)], timeout=3.0)
            elapsed = time.monotonic() - started
            pids = self._read_pids(marker)
            root_pid, desc_pid = pids

            # Гарантия №1 (файлы) есть и здесь: caller вернулся вовремя...
            assert elapsed < 10.0
            assert not pid_alive(root_pid)
            # ...а гарантии №2 (tree kill) нет — это и есть цена отказа Job.
            assert pid_alive(desc_pid), "без Job потомок остаётся жив — деградация заявлена честно"
        finally:
            for pid in pids:
                hard_kill(pid)
            # capture исчезает только после закрытия ЕГО handle осиротевшим потомком
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline and any(Path(h.name).exists() for h in opened):
                time.sleep(0.05)

    def test_parallel_calls_do_not_share_capture_or_serialize(self, monkeypatch):
        opened = _capture_spy(monkeypatch)
        barrier = threading.Barrier(2)
        results: dict[str, subprocess.CompletedProcess] = {}

        def worker(token: str) -> None:
            code = _ECHO.format(out=f"OUT-{token}", err=f"ERR-{token}", rc=0)
            barrier.wait(timeout=30)
            results[token] = gp.run_git([sys.executable, "-c", code], timeout=60)

        threads = [threading.Thread(target=worker, args=(t,)) for t in ("альфа", "бета")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=90)

        assert set(results) == {"альфа", "бета"}
        for token, res in results.items():
            assert res.stdout == f"OUT-{token}"
            assert res.stderr == f"ERR-{token}"
        names = [h.name for h in opened]
        assert len(names) == 4
        assert len(set(names)) == 4, "общего capture-файла нет, имена уникальны"


@windows_only
class TestRealGitForWindowsGate:
    """Обязательный gate: реальный `Git\\cmd\\git.exe` — launcher, а не исполняющий Git."""

    @staticmethod
    def _git_launcher() -> str:
        for var in ("ProgramFiles", "ProgramFiles(x86)", "LocalAppData"):
            root = os.environ.get(var)
            if not root:
                continue
            candidate = Path(root) / "Git" / "cmd" / "git.exe"
            if not candidate.is_file():
                candidate = Path(root) / "Programs" / "Git" / "cmd" / "git.exe"
            if candidate.is_file():
                return str(candidate)
        pytest.skip("Git for Windows не установлен (shutil.which доказательством не считается)")

    def test_launcher_tree_joins_job_and_dies_on_timeout(self, monkeypatch):
        launcher = self._git_launcher()
        snapshot: dict = {}
        real_terminate = gp._WindowsKillJob.terminate

        def spy_terminate(self):
            snapshot["pids"] = job_pids(self._handle)
            return real_terminate(self)

        monkeypatch.setattr(gp._WindowsKillJob, "terminate", spy_terminate)

        started = time.monotonic()
        with pytest.raises(subprocess.TimeoutExpired):
            gp.run_git([launcher, "-c", "alias.rlmslow=!sleep 60", "rlmslow"], timeout=4.0)
        elapsed = time.monotonic() - started

        pids = snapshot.get("pids", [])
        try:
            assert elapsed < 12.0
            assert len(pids) >= 2, (
                f"ожидалась цепочка cmd\\git.exe -> mingw64\\bin\\git.exe -> shell, увидено PID: {pids}"
            )
            assert wait_all_dead(pids) == [False] * len(pids)
        finally:
            for pid in pids:
                hard_kill(pid)


@windows_only
class TestNestedWithSandboxJob:
    """Git входит и во внешний sandbox Job, и во внутренний Git Job одновременно."""

    def test_inner_job_does_not_weaken_outer_sandbox_job(self, tmp_path):
        from rlm_tools_bsl import sandbox_process

        go = tmp_path / "go"
        marker = tmp_path / "pids.json"
        done = tmp_path / "done"
        child_code = _SPAWN_DESCENDANT.format(marker=str(marker))
        worker_code = (
            "import subprocess, sys, time, pathlib\n"
            "sys.path.insert(0, %r)\n"
            "from rlm_tools_bsl import _git_process as gp\n"
            "while not pathlib.Path(%r).exists():\n"
            "    time.sleep(0.05)\n"
            "try:\n"
            "    gp.run_git([sys.executable, '-c', %r], timeout=3.0)\n"
            "except subprocess.TimeoutExpired:\n"
            "    pass\n"
            "pathlib.Path(%r).write_text('done')\n"
            "time.sleep(30)\n"
        ) % (str(Path(__file__).resolve().parents[1] / "src"), str(go), child_code, str(done))

        outer = sandbox_process._WindowsJob(memory_mb=0, max_processes=16)
        limits_before = job_limits(outer._handle)
        worker = subprocess.Popen([sys.executable, "-c", worker_code])
        neighbour = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        git_pids: list[int] = []
        try:
            outer.assign(worker.pid)
            outer.assign(neighbour.pid)
            go.write_text("go")

            # Пока git жив — union PID'ов внешнего Job. Git root обязан там быть:
            # вложенный Job не выводит процесс из внешнего.
            seen: set[int] = set()
            deadline = time.monotonic() + 20.0
            while time.monotonic() < deadline and not done.exists():
                seen.update(job_pids(outer._handle))
                time.sleep(0.05)
            assert done.exists(), "worker не завершил свой run_git"

            git_pids = json.loads(marker.read_text())
            assert set(git_pids) <= seen, "git root/потомок обязаны состоять и во ВНЕШНЕМ Job"
            assert wait_all_dead(git_pids) == [False, False], "внутренний Job снёс дерево git"

            # Внешний Job не тронут: worker и соседняя сессия живы, limits прежние.
            assert worker.poll() is None
            assert neighbour.poll() is None
            assert job_limits(outer._handle) == limits_before
        finally:
            for pid in git_pids:
                hard_kill(pid)
            outer.close()
            for proc in (worker, neighbour):
                proc.kill()
                proc.wait(timeout=10)


# ---------------------------------------------------------------------------
# Capture-каталог службы и маркер
# ---------------------------------------------------------------------------
class TestServiceCaptureDir:
    def test_user_mode_uses_system_temp(self, monkeypatch):
        monkeypatch.delenv("RLM_UNDER_SERVICE", raising=False)
        import tempfile as _tempfile

        assert gp._git_capture_dir() == Path(_tempfile.gettempdir())

    def test_service_mode_requires_exact_marker(self, tmp_path, monkeypatch):
        config = tmp_path / "cfg" / "service.json"
        config.parent.mkdir()
        expected = config.parent / gp.SERVICE_CAPTURE_DIRNAME
        monkeypatch.setenv("RLM_UNDER_SERVICE", "1")
        monkeypatch.setenv("RLM_CONFIG_FILE", str(config))

        monkeypatch.setenv(gp.GIT_CAPTURE_DIR_ENV, str(expected))
        assert gp._git_capture_dir() == expected

        monkeypatch.setenv(gp.GIT_CAPTURE_DIR_ENV, str(tmp_path / "чужой"))
        with pytest.raises(OSError, match="does not match"):
            gp._git_capture_dir()

    @pytest.mark.parametrize(
        ("config_file", "marker", "match"),
        [
            (None, "abs", "RLM_CONFIG_FILE"),
            ("relative", "abs", "RLM_CONFIG_FILE"),
            ("abs", None, "capture marker"),
            ("abs", "relative", "capture marker"),
        ],
    )
    def test_service_mode_rejects_missing_or_relative(self, tmp_path, monkeypatch, config_file, marker, match):
        monkeypatch.setenv("RLM_UNDER_SERVICE", "1")
        abs_config = tmp_path / "cfg" / "service.json"
        values = {
            None: None,
            "relative": os.path.join("cfg", "service.json"),
            "abs": str(abs_config),
        }
        marker_values = {
            None: None,
            "relative": gp.SERVICE_CAPTURE_DIRNAME,
            "abs": str(abs_config.parent / gp.SERVICE_CAPTURE_DIRNAME),
        }
        for name, value in (("RLM_CONFIG_FILE", values[config_file]), (gp.GIT_CAPTURE_DIR_ENV, marker_values[marker])):
            if value is None:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, value)
        with pytest.raises(OSError, match=match):
            gp._git_capture_dir()

    @windows_only
    def test_unavailable_capture_disables_git_fast_path_only(self, tmp_path, monkeypatch):
        """Отказ capture = Git-ускорение выключено; исключение наружу не выходит."""
        from rlm_tools_bsl import bsl_index

        monkeypatch.setenv("RLM_UNDER_SERVICE", "1")
        monkeypatch.delenv(gp.GIT_CAPTURE_DIR_ENV, raising=False)
        monkeypatch.setenv("RLM_CONFIG_FILE", str(tmp_path / "cfg" / "service.json"))
        monkeypatch.setattr(bsl_index, "_git_exe", sys.executable)

        assert bsl_index._git_available(str(tmp_path)) is False

        err: dict = {}
        assert bsl_index._git_grep(str(tmp_path), "что-нибудь", err=err) is None
        assert err["kind"] == "spawn_failed"


# ---------------------------------------------------------------------------
# Tripwire по callsite и гигиена логов
# ---------------------------------------------------------------------------
class TestCallsitesAndLogs:
    def test_all_git_callsites_go_through_run_git(self):
        source = (Path(__file__).resolve().parents[1] / "src" / "rlm_tools_bsl" / "bsl_index.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)
        direct_runs = 0
        run_git_calls = 0
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "run":
                if isinstance(func.value, ast.Name) and func.value.id == "subprocess":
                    direct_runs += 1
            elif isinstance(func, ast.Name) and func.id == "run_git":
                run_git_calls += 1
        assert direct_runs == 0, "прямых Git-вызовов subprocess.run в bsl_index не осталось"
        assert run_git_calls == 8, f"ожидалось 8 Git-callsite через run_git, найдено {run_git_calls}"

    def test_git_grep_log_does_not_leak_pattern_path_or_stderr(self, tmp_path, monkeypatch, caplog):
        from rlm_tools_bsl import bsl_index

        secret_pattern = "СЕКРЕТНЫЙ_ПАТТЕРН"
        monkeypatch.setattr(bsl_index, "_git_exe", sys.executable)

        def boom(args, *, timeout):
            raise subprocess.TimeoutExpired(cmd=list(args), timeout=timeout)

        monkeypatch.setattr(bsl_index, "run_git", boom)
        err: dict = {}
        with caplog.at_level("INFO", logger=bsl_index.logger.name):
            assert bsl_index._git_grep(str(tmp_path), secret_pattern, err=err) is None

        text = "\n".join(rec.getMessage() for rec in caplog.records)
        assert secret_pattern not in text
        assert str(tmp_path) not in text
        assert "TimeoutExpired" in text
        # agent-facing диагностика не обеднела
        assert err["kind"] == "timeout"
        assert "TimeoutExpired" in err["detail"]
