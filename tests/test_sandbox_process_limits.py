"""v1.29.0 этап 9: resource limits — env-валидация (fail-fast, без fallback),
memory ceiling, code/IPC limits, поведение соседних workers."""

import sys
import time
from unittest.mock import patch

import pytest

from _process_test_utils import make_cf_project, pid_alive, wait_until
from rlm_tools_bsl import _sandbox_config as sc
from rlm_tools_bsl._sandbox_config import SandboxConfigError
from rlm_tools_bsl.format_detector import detect_format
from rlm_tools_bsl.sandbox_process import ProcessBackendConfig, ProcessSandboxBackend, format_info_to_payload


# ---------------------------------------------------------------------------
# env parsing / fail-fast
# ---------------------------------------------------------------------------


def test_mode_default_is_process(monkeypatch):
    """Production default — процессная изоляция. Тест снимает и env-переменную,
    и pin из conftest: иначе он проверял бы pin, а не реальный default."""
    monkeypatch.delenv("RLM_SANDBOX_MODE", raising=False)
    assert sc.DEFAULT_SANDBOX_MODE == "process"
    assert sc.get_sandbox_mode() == "process"


@pytest.mark.parametrize(
    "value,expected",
    [("process", "process"), ("inline", "inline"), (" Process ", "process"), ("INLINE", "inline")],
)
def test_mode_accepts_only_known_values(monkeypatch, value, expected):
    # Ожидаемые значения заданы литералами, а не повтором выражения нормализации
    # из реализации — иначе тест подтверждал бы сам себя.
    monkeypatch.setenv("RLM_SANDBOX_MODE", value)
    assert sc.get_sandbox_mode() == expected


@pytest.mark.parametrize("value", ["", "  ", "porcess", "processs", "0", "true"])
def test_mode_invalid_or_empty_fails_fast(monkeypatch, value):
    monkeypatch.setenv("RLM_SANDBOX_MODE", value)
    with pytest.raises(SandboxConfigError, match="RLM_SANDBOX_MODE"):
        sc.get_sandbox_mode()


def test_invalid_mode_fails_rlm_start(monkeypatch, tmp_path):
    from rlm_tools_bsl.server import _rlm_start
    import json

    monkeypatch.setenv("RLM_SANDBOX_MODE", "typo-mode")
    resp = json.loads(_rlm_start(path=str(tmp_path), query="x"))
    assert "Sandbox configuration error" in resp["error"]


def test_invalid_mode_fails_server_main(monkeypatch):
    from rlm_tools_bsl import server

    monkeypatch.setenv("RLM_SANDBOX_MODE", "bogus")
    with patch.object(server.mcp, "run") as mock_run, patch.object(sys, "argv", ["rlm-tools-bsl"]):
        with pytest.raises(SystemExit, match="invalid sandbox configuration"):
            server.main()
        mock_run.assert_not_called()


def test_shutdown_deadline_default_and_bounds(monkeypatch):
    monkeypatch.delenv("RLM_SANDBOX_SHUTDOWN_DEADLINE_SECONDS", raising=False)
    assert sc.shutdown_deadline_seconds() == 10
    for ok in ("1", "60"):
        monkeypatch.setenv("RLM_SANDBOX_SHUTDOWN_DEADLINE_SECONDS", ok)
        assert sc.shutdown_deadline_seconds() == int(ok)
    for bad in ("0", "61", "-5", "ten", "1.5"):
        monkeypatch.setenv("RLM_SANDBOX_SHUTDOWN_DEADLINE_SECONDS", bad)
        with pytest.raises(SandboxConfigError):
            sc.shutdown_deadline_seconds()


def test_numeric_envs_validation(monkeypatch):
    cases = [
        ("RLM_SANDBOX_START_TIMEOUT_SECONDS", sc.start_timeout_seconds, 60, ["0", "-1", "abc"]),
        ("RLM_SANDBOX_MEMORY_MB", sc.memory_mb, 1024, ["-1", "5", "x"]),
        ("RLM_SANDBOX_IPC_MAX_BYTES", sc.ipc_max_bytes, 4 * 1024 * 1024, ["0", "1000"]),
        ("RLM_SANDBOX_MAX_CODE_CHARS", sc.max_code_chars, 1_000_000, ["0", "-9"]),
        ("RLM_SANDBOX_MAX_PROCESSES", sc.max_processes, 16, ["-1", "99999"]),
        ("RLM_SANDBOX_KILL_GRACE_SECONDS", sc.kill_grace_seconds, 1, ["-1", "61"]),
    ]
    for env, fn, default, bad_values in cases:
        monkeypatch.delenv(env, raising=False)
        assert fn() == default, env
        for bad in bad_values:
            monkeypatch.setenv(env, bad)
            with pytest.raises(SandboxConfigError):
                fn()
        monkeypatch.delenv(env, raising=False)


def test_zero_semantics(monkeypatch):
    # 0 = «лимит отключён» только там, где это документировано
    monkeypatch.setenv("RLM_SANDBOX_MEMORY_MB", "0")
    assert sc.memory_mb() == 0
    monkeypatch.setenv("RLM_SANDBOX_MAX_PROCESSES", "0")
    assert sc.max_processes() == 0
    monkeypatch.setenv("RLM_SANDBOX_KILL_GRACE_SECONDS", "0")
    assert sc.kill_grace_seconds() == 0


def test_validate_sandbox_env_snapshot(monkeypatch):
    monkeypatch.delenv("RLM_SANDBOX_MODE", raising=False)
    snap = sc.validate_sandbox_env()
    assert set(snap.keys()) == {
        "mode",
        "start_timeout_seconds",
        "kill_grace_seconds",
        "shutdown_deadline_seconds",
        "memory_mb",
        "ipc_max_bytes",
        "max_code_chars",
        "max_processes",
    }


# ---------------------------------------------------------------------------
# process-level limit behaviour
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def cf_project(tmp_path_factory):
    return make_cf_project(tmp_path_factory.mktemp("cf_limits"))


def _make_backend(project, **overrides):
    overrides.setdefault("memory_mb", 0)
    return ProcessSandboxBackend(
        ProcessBackendConfig(
            base_path=project,
            max_output_chars=10_000,
            execution_timeout_seconds=30,
            format_info_payload=format_info_to_payload(detect_format(project)),
            **overrides,
        )
    )


def _close(b):
    b.request_close("test_done")
    b.finish_close(time.monotonic() + 10)


def test_memory_limit_kills_only_worker(cf_project):
    """Аллокация сверх ceiling валит/ограничивает ТОЛЬКО свой worker; parent и
    соседний worker живы. ОС не даёт надёжного признака OOM — допустимы clean
    MemoryError либо controlled crash_or_resource_limit (§11.2)."""
    b_limited = _make_backend(cf_project, memory_mb=256)
    b_neighbor = _make_backend(cf_project)
    try:
        neighbor_pid = b_neighbor.worker_pid
        r = b_limited.execute("data = bytearray(800 * 1024 * 1024)\nprint('allocated')")
        assert r.error is not None, "800MB при ceiling 256MB не должны пройти"
        assert (
            "MemoryError" in r.error
            or "SandboxCrashedError" in r.error
            or "TimeoutError" in r.error  # страничная возня под лимитом может упереться в deadline
        ), r.error
        # сосед жив и работает
        assert pid_alive(neighbor_pid)
        assert b_neighbor.execute("print('neighbor ok')").stdout == "neighbor ok\n"
        # limited-backend восстановим (или жив, если был clean MemoryError)
        r2 = b_limited.execute("print('recovered')")
        assert r2.error is None and r2.stdout == "recovered\n"
    finally:
        _close(b_limited)
        _close(b_neighbor)


def test_memory_limit_zero_disables(cf_project):
    b = _make_backend(cf_project, memory_mb=0)
    try:
        r = b.execute("data = bytearray(50 * 1024 * 1024)\nprint(len(data))")
        assert r.error is None
    finally:
        _close(b)


@pytest.mark.parametrize(
    "soft,hard,requested_mb,effective_mb",
    [
        (512, -1, 1_024, 512),
        (256, 512, 1_024, 256),
        (-1, -1, 1_024, 1_024),
        (512, 1_024, 128, 128),
    ],
)
def test_posix_memory_limit_never_raises_inherited_ceiling(monkeypatch, soft, hard, requested_mb, effective_mb):
    from rlm_tools_bsl import sandbox_worker

    mib = 1024 * 1024
    applied_limits = []

    class FakeResource:
        RLIMIT_AS = 9
        RLIM_INFINITY = -1

        @staticmethod
        def getrlimit(resource_id):
            assert resource_id == FakeResource.RLIMIT_AS
            return soft * mib if soft >= 0 else soft, hard * mib if hard >= 0 else hard

        @staticmethod
        def setrlimit(resource_id, limits):
            applied_limits.append((resource_id, limits))

    class FakeOs:
        name = "posix"

    monkeypatch.setattr(sandbox_worker, "os", FakeOs)
    monkeypatch.setitem(sys.modules, "resource", FakeResource)

    applied, detail = sandbox_worker._apply_memory_limit(requested_mb)

    effective_bytes = effective_mb * mib
    assert applied is True
    assert applied_limits == [(FakeResource.RLIMIT_AS, (effective_bytes, effective_bytes))]
    assert str(effective_bytes) in detail


def test_code_too_large_rejected_in_parent(cf_project):
    b = _make_backend(cf_project, max_code_chars=1_000)
    try:
        pid = b.worker_pid
        assert b.execute("kept_before_code_limit = 1").error is None
        r = b.execute("# " + "x" * 2_000)
        assert r.error is not None and "CodeTooLargeError" in r.error
        assert "kept_before_code_limit" in r.variables
        assert pid_alive(pid), "отклонение до IPC не трогает worker"
        assert b.execute("print('ok')").stdout == "ok\n"
    finally:
        _close(b)


def test_execute_frame_over_ipc_limit_controlled(cf_project):
    """code < max_code_chars, но frame > RLM_SANDBOX_IPC_MAX_BYTES → controlled
    ошибка в parent до отправки, worker жив."""
    b = _make_backend(cf_project, ipc_max_bytes=256 * 1024, max_code_chars=1_000_000)
    try:
        assert b.execute("kept_before_frame_limit = 1").error is None
        r = b.execute("# " + "y" * (512 * 1024))
        assert r.error is not None and "CodeTooLargeError" in r.error
        assert "kept_before_frame_limit" in r.variables
        assert b.execute("print('ok')").stdout == "ok\n"
    finally:
        _close(b)


def test_execute_frame_over_ipc_limit_consumes_restarted_marker(cf_project):
    b = _make_backend(cf_project, ipc_max_bytes=256 * 1024, max_code_chars=1_000_000)
    try:
        b._proc.terminate()
        b._proc.join(10)
        terminated = b.execute("print('observe crash')")
        assert terminated.sandbox_state is not None
        assert terminated.sandbox_state["status"] == "terminated"

        restarted = b.execute("# " + "y" * (512 * 1024))
        assert restarted.error is not None and "CodeTooLargeError" in restarted.error
        assert restarted.sandbox_state is not None
        assert restarted.sandbox_state["status"] == "restarted"
        assert restarted.sandbox_state["generation"] == 2
        assert b.execute("print('ok')").sandbox_state is None
    finally:
        _close(b)


def test_max_code_limit_consumes_restarted_marker(cf_project):
    b = _make_backend(cf_project, max_code_chars=1_000)
    try:
        b._proc.terminate()
        b._proc.join(10)
        terminated = b.execute("print('observe crash')")
        assert terminated.sandbox_state is not None
        assert terminated.sandbox_state["status"] == "terminated"

        restarted = b.execute("# " + "x" * 2_000)
        assert restarted.error is not None and "CodeTooLargeError" in restarted.error
        assert restarted.sandbox_state is not None
        assert restarted.sandbox_state["status"] == "restarted"
        assert restarted.sandbox_state["generation"] == 2
        assert b.execute("print('ok')").sandbox_state is None
    finally:
        _close(b)


def test_large_user_error_is_bounded_only_by_ipc_frame(cf_project):
    b = _make_backend(cf_project, ipc_max_bytes=256 * 1024)
    try:
        r = b.execute("raise ValueError('z' * 300_000)")
        assert r.error is not None and "ValueError" in r.error
        assert r.error.endswith("… [truncated]")
        assert b.execute("print('worker alive')").stdout == "worker alive\n"
    finally:
        _close(b)


def test_large_user_error_with_lone_surrogate_keeps_worker_alive(cf_project):
    b = _make_backend(cf_project, ipc_max_bytes=256 * 1024)
    try:
        r = b.execute("raise ValueError('a' * 70_000 + chr(0xD800))")
        assert r.error is not None and "ValueError" in r.error
        assert "\ud800" not in r.error
        assert "\ufffd" in r.error
        assert b.execute("print('worker alive')").stdout == "worker alive\n"
    finally:
        _close(b)


def test_oversized_worker_result_kills_worker_controlled(cf_project):
    """Ответ worker больше IPC-лимита: recv_bytes(maxlength) делает канал
    нечитаемым → worker убивается controlled, следующий execute — новое поколение."""
    b = _make_backend(cf_project, ipc_max_bytes=256 * 1024)
    try:
        # маленький code, но result с тысячами helper_calls раздувается сверх 256KB
        code = "for _i in range(6000):\n    read_file('Configuration.xml')\nprint('done')"
        r = b.execute(code)
        assert r.error is not None
        assert "SandboxCrashedError" in r.error or "SandboxProtocolError" in r.error
        assert r.sandbox_state is not None and r.sandbox_state["status"] == "terminated"
        r2 = b.execute("print('fresh generation')")
        assert r2.error is None and b.generation == 2
    finally:
        _close(b)


def test_normal_heavy_helpers_fit_default_budget(cf_project):
    """Обычные тяжёлые операции укладываются в defaults (§17.9)."""
    b = _make_backend(cf_project, memory_mb=1024)
    try:
        r = b.execute(
            "files = glob_files('**/*.bsl')\n"
            "bodies = read_files(files)\n"
            "print(len(files), sum(len(v) for v in bodies.values()) > 0)"
        )
        assert r.error is None, r.error
        assert r.stdout.strip().endswith("True")
    finally:
        _close(b)


def test_worker_dead_after_close_no_pid_leak(cf_project):
    pids = []
    for _ in range(3):
        b = _make_backend(cf_project)
        pids.append(b.worker_pid)
        _close(b)
    assert wait_until(lambda: not any(pid_alive(p) for p in pids), timeout=15)
