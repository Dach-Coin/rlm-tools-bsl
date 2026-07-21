"""v1.29.0 этапы 4-6: worker lifecycle, ProcessSandboxBackend execute, hard timeout,
kill tree, crash/restart, LLM shared quota. Реальные spawn-процессы (Win/Linux)."""

import os
import threading
import time
from types import SimpleNamespace

import pytest

from _process_test_utils import make_cf_project, pid_alive, wait_until
from rlm_tools_bsl.format_detector import detect_format
from rlm_tools_bsl._sandbox_protocol import SandboxProtocolError
from rlm_tools_bsl.sandbox import TRUNCATION_MARKER
from rlm_tools_bsl.sandbox_backend import SandboxClosedError, SandboxStartupError
from rlm_tools_bsl.sandbox_process import (
    TIMEOUT_PARTIAL_MARKER,
    ProcessBackendConfig,
    ProcessSandboxBackend,
    format_info_to_payload,
)
from rlm_tools_bsl.sandbox_worker import _detach_stdio

# Catch-all бесконечный цикл: глотает ЛЮБОЕ внедряемое исключение — старый
# thread-timeout такой код переживал, parent hard-kill обязан завершить (§18.3.2).
CATCH_ALL_LOOP = """
print('before-hang')
while True:
    try:
        while True:
            pass
    except BaseException:
        pass
"""


def _make_config(project_path, **overrides):
    overrides.setdefault("max_output_chars", 10_000)
    overrides.setdefault("execution_timeout_seconds", 45)
    overrides.setdefault("start_timeout_seconds", 60)
    overrides.setdefault("kill_grace_seconds", 1)
    # memory limit в функциональных тестах отключён — отдельные limit-тесты
    # включают его явно (test_sandbox_process_limits).
    overrides.setdefault("memory_mb", 0)
    overrides.setdefault("format_info_payload", format_info_to_payload(detect_format(project_path)))
    return ProcessBackendConfig(base_path=project_path, **overrides)


@pytest.fixture(scope="module")
def cf_project(tmp_path_factory):
    return make_cf_project(tmp_path_factory.mktemp("cf_proc"))


@pytest.fixture(scope="module")
def backend(cf_project):
    """Один живой worker на модуль — дорогой spawn амортизируется."""
    b = ProcessSandboxBackend(_make_config(cf_project))
    yield b
    b.request_close("test_module_done")
    b.finish_close(time.monotonic() + 10)


def _close(b):
    b.request_close("test_done")
    b.finish_close(time.monotonic() + 10)


# ---------------------------------------------------------------------------
# init / базовый execute (этапы 4-5)
# ---------------------------------------------------------------------------


def test_init_metadata(backend):
    assert backend.mode == "process"
    assert backend.generation == 1
    assert backend.state == "alive"
    assert backend.index_loaded is False
    assert pid_alive(backend.worker_pid)
    snapshot = backend.registry_snapshot
    assert snapshot and all(set(e.keys()) == {"sig", "cat", "kw", "recipe"} for e in snapshot.values())
    assert backend.registry_names == tuple(snapshot.keys())


def test_registry_snapshot_parity_with_inline(backend, cf_project):
    from rlm_tools_bsl.format_detector import detect_format as df
    from rlm_tools_bsl.sandbox import Sandbox
    from rlm_tools_bsl.sandbox_backend import InlineSandboxBackend

    inline = InlineSandboxBackend(
        Sandbox(base_path=cf_project, max_output_chars=10_000, format_info=df(cf_project)),
        None,
        install_llm_tools=False,
    )
    assert set(backend.registry_snapshot.keys()) == set(inline.registry_snapshot.keys())


def test_execute_namespace_persists_and_unicode(backend):
    r1 = backend.execute("x = 42\nprint('установлено')")
    assert r1.error is None and r1.stdout == "установлено\n"
    assert "x" in r1.variables
    r2 = backend.execute("print(x)")
    assert r2.error is None and r2.stdout == "42\n"
    assert r2.generation == 1 and r2.sandbox_state is None


def test_execute_helper_call_and_duplicates(backend):
    code = "print(len(glob_files('**/*.bsl')))\nprint(len(glob_files('**/*.bsl')))"
    r = backend.execute(code)
    assert r.error is None
    names = [h.name for h in r.helper_calls]
    assert names.count("glob_files") == 2
    dups = [h for h in r.helper_calls if h.duplicate_of is not None]
    assert len(dups) == 1, "второй идентичный вызов помечен duplicate_of"


def test_execute_error_with_hints_and_partial_stdout(backend):
    r = backend.execute("print('partial-out')\nnot_a_helper()")
    assert "partial-out" in r.stdout
    assert r.error is not None and "NameError" in r.error and "HINT" in r.error
    assert r.sandbox_state is None, "обычная ошибка кода не является reset-ом"


def test_large_user_error_preserved_when_it_fits_ipc_frame(backend):
    message = "x" * 100_000
    r = backend.execute("raise ValueError('x' * 100_000)")
    assert r.error is not None
    assert message in r.error
    assert "… [truncated]" not in r.error


def test_error_traceback_has_no_spawn_bootstrap_leak(backend):
    """Регресс: до фикса traceback-кадр кода агента (File "<string>", line 1)
    в process-режиме эхом показывал multiprocessing bootstrap worker-а
    (`from multiprocessing.spawn import spawn_main; spawn_main(parent_pid=..., pipe_handle=...)`),
    т.к. exec использовал дефолтное имя "<string>", совпадающее с co_filename `-c`-bootstrap.
    Компиляция под "<rlm-sandbox>" убирает коллизию — bootstrap-строка не протекает в ошибку."""
    r = backend.execute("raise ValueError('boom-xyz')")
    assert r.error is not None and "ValueError: boom-xyz" in r.error
    assert "spawn_main" not in r.error, r.error
    assert "multiprocessing.spawn" not in r.error, r.error
    assert 'File "<rlm-sandbox>", line 1, in <module>' in r.error
    assert r.sandbox_state is None  # обычная ошибка кода, не крах worker


def test_execute_truncation_marker(backend, cf_project):
    b = ProcessSandboxBackend(_make_config(cf_project, max_output_chars=50))
    try:
        r = b.execute("print('a' * 500)")
        assert r.stdout == "a" * 50 + TRUNCATION_MARKER
    finally:
        _close(b)


def test_child_stdio_detached_from_parent(backend, capfd):
    r = backend.execute("print('LEAK-CHECK-MARKER-123')")
    assert r.stdout == "LEAK-CHECK-MARKER-123\n"
    out, err = capfd.readouterr()
    assert "LEAK-CHECK-MARKER-123" not in out
    assert "LEAK-CHECK-MARKER-123" not in err


def test_detach_stdio_open_failure_is_reported(monkeypatch):
    def fail_open(*_args, **_kwargs):
        raise OSError("no null device")

    monkeypatch.setattr("rlm_tools_bsl.sandbox_worker.os.open", fail_open)
    detached, detail = _detach_stdio()
    assert detached is False
    assert "open(" in detail and "no null device" in detail


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group cleanup")
def test_failed_start_cleanup_kills_process_group(monkeypatch):
    class FakeConnection:
        def close(self):
            pass

    class FakeProcess:
        pid = 424242

        def __init__(self):
            self.alive = True
            self.terminated_root_only = False

        def is_alive(self):
            return self.alive

        def terminate(self):
            self.terminated_root_only = True
            self.alive = False

        def kill(self):
            self.alive = False

        def join(self, _timeout):
            pass

    proc = FakeProcess()
    killed_groups = []

    def kill_group(pid, _signal):
        killed_groups.append(pid)
        proc.alive = False

    monkeypatch.setattr("rlm_tools_bsl.sandbox_process.os.killpg", kill_group)
    backend = ProcessSandboxBackend.__new__(ProcessSandboxBackend)
    backend._cfg = SimpleNamespace(kill_grace_seconds=0)
    # __new__ минует __init__, поэтому tree-cleanup-состояние выставляем вручную,
    # как и соседний test_failed_start_job_confirms_matching_close_race: успешный
    # killpg доводит _cleanup_failed_start до чтения self._tree_cleanup_target.
    backend._tree_cleanup_unconfirmed = False
    backend._tree_cleanup_target = None
    backend._tree_cleanup_confirmed_target = None
    backend._cleanup_failed_start(proc, FakeConnection(), None)

    assert killed_groups == [proc.pid]
    assert proc.terminated_root_only is False


def test_code_too_large_rejected_before_ipc(backend):
    cfg_max = backend._cfg.max_code_chars
    r = backend.execute("#" + "x" * cfg_max)
    assert r.error is not None and "CodeTooLargeError" in r.error
    # worker жив и работает дальше
    assert backend.execute("print('alive')").stdout == "alive\n"


def test_init_with_index(cf_project, monkeypatch, tmp_path):
    from rlm_tools_bsl.bsl_index import IndexBuilder

    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
    db_path = IndexBuilder().build(cf_project, build_calls=True)
    b = ProcessSandboxBackend(_make_config(cf_project, db_path=str(db_path), index_expected=True))
    try:
        assert b.index_loaded is True
        assert b.index_warning is None
        r = b.execute("print(get_index_info()['builder_version'])")
        assert r.error is None and int(r.stdout.strip()) >= 14
    finally:
        _close(b)


def test_missing_db_between_check_and_init(cf_project, tmp_path):
    ghost = tmp_path / "ghost" / "bsl_index.db"
    b = ProcessSandboxBackend(_make_config(cf_project, db_path=str(ghost), index_expected=True))
    try:
        assert b.index_loaded is False
        assert b.index_warning and "live/no-index" in b.index_warning
        assert b.execute("print('live ok')").stdout == "live ok\n"  # live-режим работает
    finally:
        _close(b)


def test_startup_timeout(cf_project):
    t0 = time.monotonic()
    with pytest.raises(SandboxStartupError, match="init timed out"):
        ProcessSandboxBackend(_make_config(cf_project, start_timeout_seconds=1, test_init_delay_seconds=30.0))
    assert time.monotonic() - t0 < 20


def test_extension_paths_full_list_reaches_worker(cf_project, tmp_path):
    ext_paths = []
    for i in range(25):
        d = tmp_path / f"ext{i}"
        d.mkdir()
        ext_paths.append(str(d))
    b = ProcessSandboxBackend(_make_config(cf_project, extension_paths=ext_paths))
    try:
        assert b.extension_paths_count == 25
        assert b.extension_paths == ext_paths
    finally:
        _close(b)


# ---------------------------------------------------------------------------
# lifecycle: shutdown / repeated close / crash / restart
# ---------------------------------------------------------------------------


def test_graceful_close_removes_pid(cf_project):
    b = ProcessSandboxBackend(_make_config(cf_project))
    pid = b.worker_pid
    assert pid_alive(pid)
    b.request_close("rlm_end")
    report = b.finish_close(time.monotonic() + 10)
    assert report.closed
    assert wait_until(lambda: not pid_alive(pid), timeout=10)
    # repeated close безопасен
    b.request_close("again")
    assert b.finish_close(time.monotonic() + 10).closed
    with pytest.raises(SandboxClosedError):
        b.execute("print(1)")


def test_external_kill_gives_controlled_crash_then_restart(cf_project):
    b = ProcessSandboxBackend(_make_config(cf_project))
    try:
        assert b.execute("marker_var = 'gen1'").error is None
        proc = b._proc
        proc.terminate()
        proc.join(10)
        r = b.execute("print(marker_var)")
        assert r.error is not None and "SandboxCrashedError" in r.error
        assert r.sandbox_state == {
            "status": "terminated",
            "reason": "crash_or_resource_limit",
            "state_lost": True,
            "restart": "on_next_execute",
        }
        assert b.state == "dead"
        # lazy restart на следующем execute
        r2 = b.execute("print('marker_var' in dir())")
        assert r2.error is None
        assert r2.stdout.strip() == "False", "переменная старого поколения отсутствует"
        assert r2.generation == 2 and b.generation == 2
        assert r2.sandbox_state == {
            "status": "restarted",
            "reason": "previous_crash_or_resource_limit",
            "state_lost": True,
            "generation": 2,
        }
        # маркер снят после первого ответа нового поколения
        assert b.execute("print('ok')").sandbox_state is None
    finally:
        _close(b)


def test_restarted_marker_survives_user_error(cf_project):
    b = ProcessSandboxBackend(_make_config(cf_project))
    try:
        b._proc.terminate()
        b._proc.join(10)
        b.execute("print(1)")  # crash result, state dead
        r = b.execute("raise ValueError('user error in fresh worker')")
        assert r.error is not None and "ValueError" in r.error
        assert r.sandbox_state is not None and r.sandbox_state["status"] == "restarted"
    finally:
        _close(b)


def test_protocol_violation_kills_worker(cf_project, monkeypatch):
    b = ProcessSandboxBackend(_make_config(cf_project))
    try:
        pid = b.worker_pid
        import rlm_tools_bsl.sandbox_process as sp

        real_validate = sp.validate_message
        calls = {"n": 0}

        def broken_validate(*args, **kwargs):
            if calls["n"] == 0 and kwargs.get("allowed_types") == {"execute_result", "worker_error"}:
                calls["n"] += 1
                raise sp.SandboxProtocolError("simulated malformed frame")
            return real_validate(*args, **kwargs)

        monkeypatch.setattr(sp, "validate_message", broken_validate)
        r = b.execute("print('x')")
        assert r.error is not None and "SandboxProtocolError" in r.error
        assert r.sandbox_state["reason"] == "protocol_error"
        assert wait_until(lambda: not pid_alive(pid), timeout=10)
        monkeypatch.setattr(sp, "validate_message", real_validate)
        assert b.execute("print('recovered')").stdout == "recovered\n"
        assert b.generation == 2
    finally:
        _close(b)


# ---------------------------------------------------------------------------
# hard timeout (этап 6)
# ---------------------------------------------------------------------------


def test_hard_timeout_kills_catch_all_loop(cf_project):
    b = ProcessSandboxBackend(_make_config(cf_project, execution_timeout_seconds=2))
    try:
        assert b.execute("keep = 'will-be-lost'").error is None
        pid = b.worker_pid
        t0 = time.monotonic()
        r = b.execute(CATCH_ALL_LOOP)
        elapsed = time.monotonic() - t0
        assert elapsed < 20, f"hard kill не уложился в tolerance: {elapsed:.1f}s"
        assert r.error is not None and "TimeoutError" in r.error and "2 seconds" in r.error
        assert r.stdout.startswith("before-hang\n"), "частичный stdout до зависания возвращён"
        assert r.stdout.endswith(TIMEOUT_PARTIAL_MARKER)
        assert r.sandbox_state == {
            "status": "terminated",
            "reason": "timeout",
            "state_lost": True,
            "restart": "on_next_execute",
        }
        assert wait_until(lambda: not pid_alive(pid), timeout=10), "PID не должен пережить timeout"
        # следующий execute — новое поколение, переменные потеряны
        r2 = b.execute("print('keep' in dir())")
        assert r2.stdout.strip() == "False"
        assert r2.generation == 2
        assert r2.sandbox_state["status"] == "restarted" and r2.sandbox_state["reason"] == "previous_timeout"
    finally:
        _close(b)


def test_redos_in_child_killed(cf_project):
    b = ProcessSandboxBackend(_make_config(cf_project, execution_timeout_seconds=2))
    try:
        t0 = time.monotonic()
        r = b.execute("import re\nprint('start')\nre.match(r'(a+)+$', 'a'*40 + 'b')\nprint('never')")
        assert time.monotonic() - t0 < 20
        assert r.error is not None and "TimeoutError" in r.error
        assert "start" in r.stdout and "never" not in r.stdout
    finally:
        _close(b)


def test_timeout_of_one_backend_does_not_affect_other(cf_project):
    b_slow = ProcessSandboxBackend(_make_config(cf_project, execution_timeout_seconds=2))
    b_ok = ProcessSandboxBackend(_make_config(cf_project))
    try:
        results = {}

        def run_slow():
            results["slow"] = b_slow.execute(CATCH_ALL_LOOP)

        def run_ok():
            results["ok"] = b_ok.execute("total = sum(range(1000000))\nprint(total)")

        threads = [threading.Thread(target=run_slow), threading.Thread(target=run_ok)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert "TimeoutError" in results["slow"].error
        assert results["ok"].error is None
        assert results["ok"].stdout.strip() == str(sum(range(1000000)))
        assert b_ok.execute("print('still alive')").stdout == "still alive\n"
    finally:
        _close(b_slow)
        _close(b_ok)


def test_kill_during_multibyte_write_no_broken_utf8(cf_project):
    """Kill-during-write (§6.3): вывод — сплошные multibyte/emoji; после hard kill
    прочитанный префикс обязан быть валидным UTF-8 без tofu-байтов."""
    b = ProcessSandboxBackend(_make_config(cf_project, execution_timeout_seconds=2, max_output_chars=100_000))
    try:
        code = "i = 0\nwhile True:\n    print('эмодзи🎉я' * 10)\n    i += 1\n"
        r = b.execute(code)
        assert r.error is not None and "TimeoutError" in r.error
        body = r.stdout.removesuffix(TIMEOUT_PARTIAL_MARKER)
        if body.endswith(TRUNCATION_MARKER):
            body = body.removesuffix(TRUNCATION_MARKER)
        assert "�" not in body, "битые UTF-8 последовательности недопустимы"
        for line in body.splitlines():
            if line:
                assert set(line) <= set("эмодзи🎉я")
    finally:
        _close(b)


# ---------------------------------------------------------------------------
# LLM: lazy init + межпроцессная quota (этап 6, §12.2)
# ---------------------------------------------------------------------------


def _llm_config(project, provider, **overrides):
    overrides.setdefault("test_llm_provider", f"_sandbox_test_providers:{provider}")
    return _make_config(project, **overrides)


def test_llm_echo_and_quota_accounting(cf_project):
    b = ProcessSandboxBackend(_llm_config(cf_project, "echo_provider", max_llm_calls=5))
    try:
        assert b.has_llm_tools is True
        r = b.execute("print(llm_query('a'))\nprint(llm_query('b'))")
        assert r.error is None and r.stdout == "echo:a\necho:b\n"
        assert b.llm_calls_used == 2
    finally:
        _close(b)


def test_llm_quota_limit_enforced_in_worker(cf_project):
    b = ProcessSandboxBackend(_llm_config(cf_project, "echo_provider", max_llm_calls=1))
    try:
        assert b.execute("print(llm_query('one'))").error is None
        r = b.execute("print(llm_query('two'))")
        assert r.error is not None and "LLM call limit exceeded" in r.error
        assert b.llm_calls_used == 1
    finally:
        _close(b)


def test_llm_quota_survives_hard_kill(cf_project, tmp_path, monkeypatch):
    """§12.2 тест (а): provider вошёл в вызов и завис; worker убит БЕЗ
    execute_result — потраченная попытка видна parent через shared counter."""
    signal_file = tmp_path / "llm_signal.txt"
    monkeypatch.setenv("RLM_TEST_LLM_SIGNAL_FILE", str(signal_file))
    b = ProcessSandboxBackend(_llm_config(cf_project, "hang_provider", max_llm_calls=5, execution_timeout_seconds=3))
    try:
        r = b.execute("print(llm_query('hang me'))")
        assert r.error is not None and "TimeoutError" in r.error
        assert signal_file.exists(), "provider фактически вошёл в вызов"
        assert b.llm_calls_used == 1, "резерв quota пережил kill без execute_result"
        # restart наследует потраченный лимит
        r2 = b.execute("print('fresh')")
        assert r2.error is None and b.generation == 2
        assert b.llm_calls_used == 1
    finally:
        _close(b)


def test_llm_batch_all_or_nothing_process(cf_project, tmp_path, monkeypatch):
    calls_file = tmp_path / "llm_calls.txt"
    monkeypatch.setenv("RLM_TEST_LLM_CALLS_FILE", str(calls_file))
    b = ProcessSandboxBackend(_llm_config(cf_project, "counting_provider", max_llm_calls=2))
    try:
        # (в) remaining < len(prompts) → batch целиком отклонён, ноль вызовов
        r = b.execute("print(llm_query_batched(['p1','p2','p3']))")
        assert r.error is not None and "LLM call limit exceeded" in r.error
        assert b.llm_calls_used == 0
        assert not calls_file.exists() or calls_file.read_text(encoding="utf-8") == ""
        # (г) достаточная quota → весь N зарезервирован и выполнен
        r = b.execute("print(sorted(llm_query_batched(['p1','p2'])))")
        assert r.error is None
        assert b.llm_calls_used == 2
        assert sorted(calls_file.read_text(encoding="utf-8").split()) == ["p1", "p2"]
    finally:
        _close(b)


def test_llm_late_client_init_failure_no_quota_spent(cf_project):
    """Probe успешен (helper объявлен), lazy factory падает: первый вызов —
    bounded ошибка, quota не израсходована (§12.1 документированная дивергенция)."""
    b = ProcessSandboxBackend(_llm_config(cf_project, "failing_factory", max_llm_calls=5))
    try:
        assert b.has_llm_tools is True, "helper объявлен по probe ещё на init"
        r = b.execute("print(llm_query('x'))")
        assert r.error is not None and "LLM provider initialization failed" in r.error
        assert b.llm_calls_used == 0
        # worker жив — это user-level ошибка, не crash
        assert b.execute("print('alive')").stdout == "alive\n"
    finally:
        _close(b)


def test_descendant_process_killed_with_tree(cf_project, tmp_path, monkeypatch):
    """§18.3.5: descendant (sys.executable sleep), запущенный внутри worker,
    не переживает hard timeout kill tree."""
    pid_file = tmp_path / "descendant_pid.txt"
    monkeypatch.setenv("RLM_TEST_CHILD_PID_FILE", str(pid_file))
    b = ProcessSandboxBackend(
        _llm_config(cf_project, "spawning_provider", max_llm_calls=5, execution_timeout_seconds=3)
    )
    try:
        r = b.execute("llm_query('spawn and hang')")
        assert r.error is not None and "TimeoutError" in r.error
        assert wait_until(pid_file.exists, timeout=5), "descendant не был запущен"
        descendant_pid = int(pid_file.read_text(encoding="utf-8").strip())
        assert wait_until(lambda: not pid_alive(descendant_pid), timeout=15), (
            f"descendant {descendant_pid} пережил kill tree"
        )
    finally:
        _close(b)


def test_restart_racing_with_teardown_does_not_orphan_worker(cf_project):
    """rlm_end во время медленного init lazy-restart убивает уже поднятый worker."""
    backend = ProcessSandboxBackend(_make_config(cf_project, execution_timeout_seconds=2))
    outcome = {}
    try:
        previous_pid = backend.worker_pid
        backend._terminate_current_worker("crash_or_resource_limit")
        assert backend.state == "dead"
        backend._cfg.test_init_delay_seconds = 30.0

        def do_execute():
            try:
                backend.execute("x = 1")
                outcome["result"] = "returned"
            except SandboxClosedError:
                outcome["result"] = "closed"
            except Exception as exc:  # noqa: BLE001
                outcome["result"] = f"{type(exc).__name__}: {exc}"

        thread = threading.Thread(target=do_execute)
        thread.start()
        assert wait_until(
            lambda: (
                backend.state == "starting"
                and backend.worker_pid is not None
                and backend.worker_pid != previous_pid
                and pid_alive(backend.worker_pid)
            ),
            timeout=20,
        ), "restart не опубликовал запускаемый worker"
        starting_pid = backend.worker_pid
        assert pid_alive(starting_pid)

        backend.request_close("rlm_end")
        thread.join(timeout=20)
        assert not thread.is_alive()
        assert outcome["result"] == "closed", outcome
        assert backend.finish_close(time.monotonic() + 10).closed
        assert wait_until(lambda: not pid_alive(starting_pid), timeout=20), (
            f"worker pid={starting_pid}, отозванный во время init, остался жив"
        )
    finally:
        backend.request_close("test_cleanup")
        backend.finish_close(time.monotonic() + 10)


@pytest.mark.parametrize(
    "entry,expected",
    [
        ({"name": "h", "elapsed": "1.5", "seq": 1}, "elapsed must be a number"),
        ({"name": "h", "elapsed": True, "seq": 1}, "elapsed must be a number"),
        ({"name": "h", "elapsed": float("nan"), "seq": 1}, "elapsed must be finite"),
        ({"name": "h", "elapsed": 1.0, "seq": "2"}, "seq must be an integer"),
        ({"name": "h", "elapsed": 1.0, "seq": 2.7}, "seq must be an integer"),
        ({"name": "h", "elapsed": 1.0, "seq": False}, "seq must be an integer"),
    ],
)
def test_helper_call_schema_is_strict(cf_project, entry, expected):
    """Строгие ТИПЫ, а не приведение: float('1.5') и int(True) прошли бы молча,
    дробный seq усёкся бы, NaN уехал бы в ответ агенту."""
    backend = ProcessSandboxBackend.__new__(ProcessSandboxBackend)
    backend._out_buf = None
    backend._out_published = None
    backend._out_truncated = None
    backend._pending_restarted_marker = None
    payload = {"error": None, "variables": [], "helper_calls": [entry], "efficiency_hints": None}
    with pytest.raises(SandboxProtocolError, match=expected):
        backend._build_result(payload, 1)


def test_efficiency_hint_entry_schema_is_strict(cf_project):
    backend = ProcessSandboxBackend.__new__(ProcessSandboxBackend)
    backend._out_buf = None
    backend._out_published = None
    backend._out_truncated = None
    backend._pending_restarted_marker = None
    payload = {"error": None, "variables": [], "helper_calls": [], "efficiency_hints": ["not-a-dict"]}
    with pytest.raises(SandboxProtocolError, match="efficiency_hints entry malformed"):
        backend._build_result(payload, 1)


@pytest.mark.parametrize("raw_calls", [False, 0, "", {}])
def test_falsey_non_list_helper_calls_are_rejected(cf_project, raw_calls):
    backend = ProcessSandboxBackend.__new__(ProcessSandboxBackend)
    backend._out_buf = None
    backend._out_published = None
    backend._out_truncated = None
    backend._pending_restarted_marker = None
    payload = {"error": None, "variables": [], "helper_calls": raw_calls, "efficiency_hints": None}
    with pytest.raises(SandboxProtocolError, match="helper_calls is not a list"):
        backend._build_result(payload, 1)


def test_failed_job_handle_close_is_retained_for_retry():
    class FakeJob:
        def close(self, kill=True):
            assert kill is True
            return True, False  # дерево убито, но CloseHandle не удался

    proc = object()
    job = FakeJob()
    backend = ProcessSandboxBackend.__new__(ProcessSandboxBackend)
    backend._conn = None
    backend._job = job
    backend._proc = proc
    backend._tree_cleanup_unconfirmed = False
    backend._tree_cleanup_target = None
    backend._tree_cleanup_confirmed_target = None

    assert backend._close_runtime_handles() is False
    assert backend._job is job, "неуспешно закрытый Job потерян и не сможет попасть в retry"
    assert backend._tree_cleanup_confirmed_target is proc


def test_failed_start_job_confirms_matching_close_race():
    class FakeProcess:
        pid = 4242

        def __init__(self):
            self.alive = True

        def is_alive(self):
            return self.alive

        def terminate(self):
            self.alive = False

        def kill(self):
            self.alive = False

        def join(self, timeout):
            pass

    class FakeConnection:
        def close(self):
            pass

    class FakeJob:
        def __init__(self, proc):
            self._proc = proc
            self._handle = object()

        def close(self, kill=True):
            assert kill is True
            self._proc.alive = False
            self._handle = None
            return True, True

    proc = FakeProcess()
    conn = FakeConnection()
    job = FakeJob(proc)
    backend = ProcessSandboxBackend.__new__(ProcessSandboxBackend)
    backend._cfg = SimpleNamespace(kill_grace_seconds=0)
    backend._state_lock = threading.Lock()
    backend._proc = proc
    backend._conn = conn
    backend._job = None  # request_close won the race before Job publication
    backend._tree_cleanup_unconfirmed = True
    backend._tree_cleanup_target = proc
    backend._tree_cleanup_confirmed_target = None

    backend._cleanup_failed_start(proc, conn, job)
    backend._discard_failed_start_runtime(proc, conn, job)

    assert backend._tree_cleanup_unconfirmed is False
    assert backend._tree_cleanup_target is None
    assert backend._tree_cleanup_confirmed_target is proc
    assert backend._proc is None


def test_execute_send_is_bounded_by_execution_deadline():
    release_send = threading.Event()

    class BlockingConnection:
        def send_bytes(self, payload):
            release_send.wait(5)

    backend = ProcessSandboxBackend.__new__(ProcessSandboxBackend)
    backend._cfg = SimpleNamespace(ipc_max_bytes=1024 * 1024, execution_timeout_seconds=0.1)
    backend._conn = BlockingConnection()
    backend.generation = 1
    backend._request_ids = iter([1])
    backend._out_published = SimpleNamespace(value=0)
    backend._out_truncated = SimpleNamespace(value=0)
    backend._state_lock = threading.Lock()
    backend._state = "executing"
    backend._close_reason = None
    timeout_result = object()
    backend._handle_hard_timeout = lambda: timeout_result

    try:
        started = time.monotonic()
        result = backend._execute_ipc("print('blocked send')")
        elapsed = time.monotonic() - started
    finally:
        release_send.set()

    assert result is timeout_result
    assert elapsed < 1.0


def test_request_close_of_executing_worker_uses_zero_wait_kill():
    backend = ProcessSandboxBackend.__new__(ProcessSandboxBackend)
    backend._state_lock = threading.Lock()
    backend._state = "executing"
    backend._close_reason = None
    backend._restart_in_progress = False
    backend._proc = object()
    backend._conn = None
    seen_wait = []

    def fake_kill(proc, wait=True):
        seen_wait.append(wait)

    backend._kill_tree_raw = fake_kill

    backend.request_close("test")

    assert seen_wait == [False]
    assert backend._state == "closing"


def test_graceful_posix_close_sweeps_group_after_root_exit(monkeypatch):
    """Graceful exit process-group leader-а не доказывает смерть descendants."""
    import rlm_tools_bsl.sandbox_process as sp

    backend = ProcessSandboxBackend.__new__(ProcessSandboxBackend)
    backend._finalized = False
    backend._close_lock = threading.Lock()
    backend._state_lock = threading.Lock()
    backend._restart_in_progress = False
    backend._state = "closing"
    backend._cfg = SimpleNamespace(kill_grace_seconds=1)
    proc = SimpleNamespace(pid=4242, is_alive=lambda: False)
    backend._proc = proc
    backend._tree_cleanup_unconfirmed = False
    backend._tree_cleanup_target = None
    backend._tree_cleanup_confirmed_target = None
    backend._sync_llm_quota = lambda: 0
    backend._close_runtime_handles = lambda: True
    kill_calls = []

    def fake_kill(target, wait=True):
        kill_calls.append((target, wait))
        backend._mark_tree_cleanup_confirmed(target)
        return True

    backend._kill_tree_raw = fake_kill
    monkeypatch.setattr(sp, "os", SimpleNamespace(name="posix"))

    report = backend.finish_close(time.monotonic() + 1)

    assert report.closed and not report.residual
    assert kill_calls == [(proc, False)]


def test_unconfirmed_tree_kill_persists_across_retries(cf_project, monkeypatch):
    """Codex P1: _finish_close_locked начинал каждый вызов с tree_confirmed=True.
    После первой неудачной попытки корень уже мёртв, kill-ветка на повторе не
    выполняется — и backend закрывался как успешный при возможно живых потомках.
    Флаг обязан сохраняться между вызовами."""
    backend = ProcessSandboxBackend(_make_config(cf_project))
    saved_job = backend._job
    try:
        # Глушим ВСЕ три источника подтверждения: первичный kill, повторное
        # подтверждение и закрытие Job (на Windows успешный close сам по себе
        # подтверждает очистку через KILL_ON_JOB_CLOSE — это корректно, но тогда
        # проверяемая ветка недостижима).
        backend._job = None
        monkeypatch.setattr(backend, "_kill_tree_raw", lambda proc, wait=True: _fail_tree(backend))
        monkeypatch.setattr(backend, "_confirm_tree_cleanup", lambda: False)

        backend.request_close("test")
        first = backend.finish_close(time.monotonic() - 1.0)
        assert first.residual, "неподтверждённая очистка дерева обязана дать residual"
        assert backend._tree_cleanup_unconfirmed is True

        # Повтор: корень уже не жив, kill-ветка пропускается. Раньше здесь
        # backend закрывался бы как успешный.
        assert not backend._proc.is_alive()
        second = backend.finish_close(time.monotonic() - 1.0)
        assert second.residual, "флаг не пережил повтор — backend закрылся при неподтверждённом дереве"
        assert not second.closed

        # И lazy restart не имеет права перезаписать target новым Process:
        # прежний pgid/Job должен остаться доступен для следующего retry.
        old_target = backend._tree_cleanup_target
        with pytest.raises(SandboxStartupError, match="cleanup is incomplete"):
            backend._prepare_previous_generation_for_restart()
        assert backend._tree_cleanup_target is old_target

        # Как только подтверждение получено — закрытие проходит штатно.
        monkeypatch.setattr(backend, "_confirm_tree_cleanup", lambda: _clear_tree(backend))
        third = backend.finish_close(time.monotonic() - 1.0)
        assert third.closed and not third.residual
    finally:
        # Возвращаем реальный Job, чтобы штатное закрытие освободило handle.
        backend._job = saved_job
        backend._finalized = False
        backend._tree_cleanup_unconfirmed = False
        backend._tree_cleanup_target = None
        backend._tree_cleanup_confirmed_target = None
        backend.request_close("cleanup")
        backend.finish_close(time.monotonic() + 10)


def _fail_tree(backend):
    """Имитация fallback-ветки: корень убит, tree-wide операция не подтверждена."""
    backend._mark_tree_cleanup_unconfirmed(backend._proc)
    if backend._proc is not None:
        backend._proc.kill()
        backend._proc.join(5)
    return False


def _clear_tree(backend):
    backend._mark_tree_cleanup_confirmed(backend._tree_cleanup_target)
    return True
