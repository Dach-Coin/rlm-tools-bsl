"""v1.29.0 этап 7: server-интеграция process mode — полный tool-flow, сериализация
сессии, teardown без ожиданий, shutdown c единым deadline, 5 параллельных rlm_start."""

import json
import os
import threading
import time

import pytest

from _process_test_utils import make_cf_project, pid_alive, wait_until
from rlm_tools_bsl import server
from rlm_tools_bsl.sandbox_backend import BackendExecutionResult, CloseReport
from rlm_tools_bsl.server import _rlm_end, _rlm_execute, _rlm_start
from rlm_tools_bsl.session import Session, SessionManager


@pytest.fixture
def process_mode(monkeypatch):
    """Только для сценариев, которые по смыслу существуют лишь в process mode
    (worker PID, hard-kill таймаута, kill дерева, Job Object)."""
    monkeypatch.setenv("RLM_SANDBOX_MODE", "process")


@pytest.fixture(params=["inline", "process"])
def any_mode(request, monkeypatch):
    """Сценарии, поведение которых обязано СОВПАДАТЬ в обоих режимах.

    Публичный контракт rlm_start/rlm_execute/rlm_end один на оба backend-а,
    поэтому такие тесты гоняются дважды: иначе inline-ветка (аварийный fallback,
    которым оператор реально будет пользоваться) осталась бы без server-level
    покрытия."""
    monkeypatch.setenv("RLM_SANDBOX_MODE", request.param)
    return request.param


@pytest.fixture
def cf_project(tmp_path):
    return make_cf_project(tmp_path / "cf")


def _start(path, **kwargs):
    resp = json.loads(_rlm_start(path=path, query="process-mode test", **kwargs))
    assert "error" not in resp, resp
    return resp


def _backend(sid):
    with server._sandboxes_lock:
        return server._sandboxes.get(sid)


def test_backend_publication_requires_current_session_and_server_epoch(monkeypatch, tmp_path):
    manager = SessionManager()
    monkeypatch.setattr(server, "session_manager", manager)
    monkeypatch.setattr(server, "_sandbox_registry_accepting", True)
    with server._sandboxes_lock:
        epoch = server._sandbox_registry_epoch

    live_sid = manager.create(path=str(tmp_path), query="live")
    live_session = manager.get(live_sid)
    live_backend = object()
    assert server._publish_session_backend(live_sid, live_session, live_backend, epoch)
    with server._sandboxes_lock:
        assert server._sandboxes.pop(live_sid) is live_backend
    manager.end(live_sid)

    evicted_sid = manager.create(path=str(tmp_path), query="evicted")
    evicted_session = manager.get(evicted_sid)
    manager.end(evicted_sid)
    assert not server._publish_session_backend(evicted_sid, evicted_session, object(), epoch)

    stale_sid = manager.create(path=str(tmp_path), query="old epoch")
    stale_session = manager.get(stale_sid)
    assert not server._publish_session_backend(stale_sid, stale_session, object(), epoch - 1)

    shutdown_sid = manager.create(path=str(tmp_path), query="shutdown")
    shutdown_session = manager.get(shutdown_sid)
    monkeypatch.setattr(server, "_sandbox_registry_accepting", False)
    assert not server._publish_session_backend(shutdown_sid, shutdown_session, object(), epoch)
    assert "shutting down" in json.loads(_rlm_start(path=str(tmp_path), query="late start"))["error"]
    with server._sandboxes_lock:
        assert evicted_sid not in server._sandboxes
        assert stale_sid not in server._sandboxes
        assert shutdown_sid not in server._sandboxes


def test_failed_start_backend_is_transferred_to_reaper(monkeypatch):
    class FakeBackend:
        def __init__(self):
            self.close_reasons = []

        def request_close(self, reason):
            self.close_reasons.append(reason)

    class FakeReaper:
        def __init__(self):
            self.enqueued = []

        def enqueue(self, backend):
            self.enqueued.append(backend)

    backend = FakeBackend()
    reaper = FakeReaper()
    monkeypatch.setattr(server, "_reaper", reaper)
    with server._sandboxes_lock:
        server._starting_sandbox_backends[id(backend)] = backend

    assert server._reap_failed_starting_backend(backend) is True
    assert server._reap_failed_starting_backend(backend) is False

    with server._sandboxes_lock:
        assert id(backend) not in server._starting_sandbox_backends
    assert backend.close_reasons == ["start_failure"]
    assert reaper.enqueued == [backend]


def test_failed_publication_keeps_worker_owned_until_reaper_transfer(monkeypatch, tmp_path):
    manager = SessionManager()
    monkeypatch.setattr(server, "session_manager", manager)
    monkeypatch.setattr(server, "_sandbox_registry_accepting", True)

    class FakeBackend:
        mode = "process"

        def __init__(self):
            self.close_reasons = []

        def request_close(self, reason):
            self.close_reasons.append(reason)

    class FakeReaper:
        def __init__(self):
            self.enqueued = []

        def enqueue(self, backend):
            self.enqueued.append(backend)

    backend = FakeBackend()
    reaper = FakeReaper()
    monkeypatch.setattr(server, "_reaper", reaper)
    with server._sandboxes_lock:
        epoch = server._sandbox_registry_epoch
    assert server._track_starting_backend(backend, epoch)

    sid = manager.create(path=str(tmp_path), query="revoked start")
    session = manager.get(sid)
    manager.end(sid)
    assert not server._publish_session_backend(sid, session, backend, epoch)

    with server._sandboxes_lock:
        assert server._starting_sandbox_backends[id(backend)] is backend
    assert server._reap_failed_starting_backend(backend) is True
    assert backend.close_reasons == ["start_failure"]
    assert reaper.enqueued == [backend]


def test_failed_process_backend_does_not_compete_with_shutdown_owner(monkeypatch):
    class FakeBackend:
        mode = "process"

    backend = FakeBackend()
    monkeypatch.setattr(server, "_sandbox_registry_accepting", False)
    with server._sandboxes_lock:
        assert id(backend) not in server._starting_sandbox_backends

    assert server._failed_process_backend_has_lifecycle_owner(backend) is True


def test_process_release_transfers_to_reaper_before_registry_unlock(monkeypatch):
    class FakeBackend:
        mode = "process"

        def __init__(self):
            self.close_reasons = []

        def request_close(self, reason):
            self.close_reasons.append(reason)

    class FakeReaper:
        def __init__(self):
            self.enqueued = []
            self.registry_was_locked = False

        def enqueue(self, backend):
            acquired = server._sandboxes_lock.acquire(blocking=False)
            self.registry_was_locked = not acquired
            if acquired:
                server._sandboxes_lock.release()
            self.enqueued.append(backend)

    backend = FakeBackend()
    reaper = FakeReaper()
    monkeypatch.setattr(server, "_reaper", reaper)
    with server._sandboxes_lock:
        server._sandboxes["atomic-release"] = backend

    server._release_session_resources("atomic-release", "rlm_end")

    with server._sandboxes_lock:
        assert "atomic-release" not in server._sandboxes
    assert backend.close_reasons == ["rlm_end"]
    assert reaper.enqueued == [backend]
    assert reaper.registry_was_locked, "shutdown could pass between registry detach and reaper ownership"


def test_queued_execute_rechecks_session_after_execution_lock(monkeypatch, tmp_path):
    """Queued execute must not run a backend detached while it waited."""
    manager = SessionManager()
    monkeypatch.setattr(server, "session_manager", manager)
    sid = manager.create(path=str(tmp_path), query="queued execute")
    session = manager.get(sid)

    entered = threading.Event()
    proceed = threading.Event()

    class GateLock:
        def __enter__(self):
            entered.set()
            assert proceed.wait(timeout=10), "queued execute was not released"

        def __exit__(self, exc_type, exc, tb):
            return False

    class FakeBackend:
        mode = "inline"

        def __init__(self):
            self.execute_calls = 0

        def execute(self, code):
            self.execute_calls += 1
            raise AssertionError("detached backend must not execute")

        def request_close(self, reason):
            self.close_reason = reason

        def finish_close(self, deadline):
            return CloseReport(closed=True)

    backend = FakeBackend()
    session.execution_lock = GateLock()
    with server._sandboxes_lock:
        server._sandboxes[sid] = backend

    result = {}
    thread = threading.Thread(target=lambda: result.setdefault("value", json.loads(_rlm_execute(sid, "print(1)"))))
    thread.start()
    assert entered.wait(timeout=10), "execute did not reach the session lock"
    manager.end(sid)
    server._release_session_resources(sid, "test_end")
    proceed.set()
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert backend.execute_calls == 0
    assert session.execute_calls == 0
    assert "closed before execution" in result["value"]["error"]


def test_full_flow_start_execute_end(any_mode, cf_project):
    resp = _start(cf_project)
    sid = resp["session_id"]
    pid = None
    try:
        assert resp["index"]["loaded"] is False
        assert resp["available_functions"], "sigs собраны из registry snapshot backend-а"
        assert resp["limits"]["sandbox_mode"] == any_mode, "режим обязан быть виден агенту (§16.2)"
        backend = _backend(sid)
        assert backend.mode == any_mode
        if any_mode == "process":
            pid = backend.worker_pid
            assert pid_alive(pid)

        r1 = json.loads(_rlm_execute(sid, "shared_var = 'из первого вызова'", detail_level="usage"))
        assert r1["error"] is None
        assert r1["usage"]["execute_calls_used"] == 1
        r2 = json.loads(_rlm_execute(sid, "print(shared_var)", detail_level="full"))
        assert r2["error"] is None and r2["stdout"] == "из первого вызова\n"
        assert "shared_var" in r2["variables"]
        assert "sandbox_state" not in r2
    finally:
        end = json.loads(_rlm_end(sid))
        assert end == {"success": True}
    assert _backend(sid) is None
    if pid is not None:
        assert wait_until(lambda: not pid_alive(pid), timeout=15), "worker обязан исчезнуть после rlm_end"
    # repeated end идемпотентен
    assert json.loads(_rlm_end(sid)) == {"success": True}


def test_missing_session_and_backend(any_mode):
    resp = json.loads(_rlm_execute("nonexistent", "print(1)"))
    assert "not found" in resp["error"]


def test_execute_call_limit_with_concurrent_executes(any_mode, cf_project):
    resp = _start(cf_project, max_execute_calls=1)
    sid = resp["session_id"]
    try:
        results = []
        barrier = threading.Barrier(2)

        def run():
            barrier.wait(timeout=10)
            results.append(json.loads(_rlm_execute(sid, "print('win')")))

        threads = [threading.Thread(target=run) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        limit_errors = [r for r in results if r.get("error") and "limit exceeded" in r["error"]]
        successes = [r for r in results if r.get("error") is None]
        assert len(successes) == 1 and len(limit_errors) == 1, results
    finally:
        _rlm_end(sid)


def test_ttl_eviction_kills_worker(process_mode, cf_project):
    resp = _start(cf_project)
    sid = resp["session_id"]
    try:
        pid = _backend(sid).worker_pid
        session = server.session_manager._sessions[sid]
        session.last_used -= 10 * 24 * 3600  # заведомо истёк
        t0 = time.monotonic()
        server._cleanup_expired_resources()
        assert time.monotonic() - t0 < 5, "eviction callback обязан быть bounded (без join в caller)"
        assert _backend(sid) is None
        assert wait_until(lambda: not pid_alive(pid), timeout=15), "reaper обязан добить worker"
        resp = json.loads(_rlm_execute(sid, "print(1)"))
        assert "not found" in resp["error"]
    finally:
        # Провал любого assert выше не должен оставлять сессию/worker в глобальном
        # registry — он session-global и утечка каскадом ломает соседние тесты.
        _rlm_end(sid)


def test_end_during_active_execute_returns_fast(process_mode, cf_project):
    """§9.3: rlm_end не ждёт execution timeout; активный execute получает
    controlled-ответ на удалённой Session, worker убит."""
    resp = _start(cf_project, execution_timeout_seconds=60)
    sid = resp["session_id"]
    exec_result = {}
    thread = None
    try:
        pid = _backend(sid).worker_pid

        def long_execute():
            exec_result["resp"] = json.loads(_rlm_execute(sid, "while True:\n    pass"))

        thread = threading.Thread(target=long_execute)
        thread.start()
        time.sleep(1.5)  # дать execute реально стартовать в worker
        t0 = time.monotonic()
        end_resp = json.loads(_rlm_end(sid))
        end_elapsed = time.monotonic() - t0
        assert end_resp == {"success": True}
        assert end_elapsed < 5, f"rlm_end ждал {end_elapsed:.1f}s — должен вернуться сразу"
        thread.join(timeout=20)
        assert not thread.is_alive(), "активный execute обязан проснуться после kill"
        resp = exec_result["resp"]
        assert resp.get("error"), resp
        assert "closed during execution" in resp["error"] or "TimeoutError" in resp["error"]
        assert wait_until(lambda: not pid_alive(pid), timeout=15)
    finally:
        # Без этого падение любого assert оставило бы сессию, worker и поток,
        # заблокированный на execute до 60 секунд.
        _rlm_end(sid)
        if thread is not None:
            thread.join(timeout=20)


def test_init_failure_cleans_session(any_mode, cf_project, monkeypatch):
    sessions_before = len(server.session_manager._sessions)

    def boom(**kwargs):
        raise RuntimeError("simulated backend init failure")

    monkeypatch.setattr(server, "_create_session_backend", boom)
    resp = json.loads(_rlm_start(path=cf_project, query="init failure"))
    assert "error" in resp and "Session init failed" in resp["error"]
    assert len(server.session_manager._sessions) == sessions_before, "session не утёк"


def test_timeout_sandbox_state_flows_to_response(process_mode, cf_project):
    resp = _start(cf_project, execution_timeout_seconds=2)
    sid = resp["session_id"]
    try:
        assert resp["limits"]["execution_timeout_seconds"] == 2
        r = json.loads(
            _rlm_execute(
                sid, "print('pre')\nwhile True:\n    try:\n        pass\n    except BaseException:\n        pass"
            )
        )
        assert r["error"] and "TimeoutError" in r["error"]
        assert r["stdout"].startswith("pre\n")
        assert r["sandbox_state"]["status"] == "terminated" and r["sandbox_state"]["reason"] == "timeout"
        # usage продолжается, restart маркирован, счётчик execute не сброшен
        r2 = json.loads(_rlm_execute(sid, "print('fresh')", detail_level="usage"))
        assert r2["error"] is None
        assert r2["sandbox_state"]["status"] == "restarted"
        assert r2["usage"]["execute_calls_used"] == 2
    finally:
        _rlm_end(sid)


def test_state_loss_resets_new_variables_snapshot():
    """compact reset не должен смешивать namespace разных worker generations."""

    class FakeBackend:
        mode = "process"
        registry_names = ()
        worker_pid = None

    session = Session(session_id="sid", path=".", query="state reset")
    backend = FakeBackend()

    first = BackendExecutionResult(stdout="", error=None, variables=["same_name"], generation=1)
    first_response = json.loads(server._finish_rlm_execute(session, backend, "", first, "full", 20, time.monotonic()))
    assert first_response["new_variables"] == ["same_name"]

    terminated = BackendExecutionResult(
        stdout="",
        error="TimeoutError",
        variables=[],
        sandbox_state={"status": "terminated", "reason": "timeout", "state_lost": True},
        generation=1,
    )
    server._finish_rlm_execute(session, backend, "", terminated, "compact", 20, time.monotonic())
    assert session._last_reported_vars == set()

    restarted = BackendExecutionResult(
        stdout="",
        error=None,
        variables=["same_name"],
        sandbox_state={"status": "restarted", "reason": "previous_timeout", "state_lost": True},
        generation=2,
    )
    restarted_response = json.loads(
        server._finish_rlm_execute(session, backend, "", restarted, "full", 20, time.monotonic())
    )
    assert restarted_response["new_variables"] == ["same_name"]


def test_shutdown_all_uses_single_shared_deadline(monkeypatch):
    """§13.6: N медленных backend-ов закрываются в ОДИН общий deadline, не N×10."""
    monkeypatch.setattr(server, "_sandbox_registry_accepting", True)
    monkeypatch.setenv("RLM_SANDBOX_SHUTDOWN_DEADLINE_SECONDS", "1")
    seen_deadlines = []
    finished = []
    force_aborted = []

    class SlowFake:
        mode = "fake"

        def request_close(self, reason):
            self.reason = reason

        def finish_close(self, deadline):
            seen_deadlines.append(deadline)
            time.sleep(min(0.8, max(0.0, deadline - time.monotonic())))
            finished.append(self)
            return CloseReport(closed=True)

        def force_abort(self):
            force_aborted.append(self)
            return True

    fakes = [SlowFake() for _ in range(3)]
    with server._sandboxes_lock:
        assert not server._sandboxes, "тест требует пустой registry"
        assert not server._starting_sandbox_backends
        for i, f in enumerate(fakes[:2]):
            server._sandboxes[f"fake-{i}"] = f
        server._starting_sandbox_backends[id(fakes[2])] = fakes[2]
    t0 = time.monotonic()
    server._shutdown_all_sandbox_backends()
    elapsed = time.monotonic() - t0
    assert seen_deadlines
    assert max(seen_deadlines) - min(seen_deadlines) < 0.01, "deadline один на всех"
    assert set(map(id, [*finished, *force_aborted])) == set(map(id, fakes))
    assert not (set(map(id, finished)) & set(map(id, force_aborted)))
    assert elapsed < 2.5, f"shutdown масштабировался как N×deadline: {elapsed:.1f}s"
    assert all(f.reason == "server_shutdown" for f in fakes)
    with server._sandboxes_lock:
        assert not server._sandboxes
        assert not server._starting_sandbox_backends
        assert server._sandbox_registry_accepting is False


def test_shutdown_with_real_idle_and_executing_workers(process_mode, cf_project, monkeypatch):
    monkeypatch.setattr(server, "_sandbox_registry_accepting", True)
    monkeypatch.setenv("RLM_SANDBOX_SHUTDOWN_DEADLINE_SECONDS", "5")
    sid_idle = _start(cf_project)["session_id"]
    sid_busy = _start(cf_project, execution_timeout_seconds=60)["session_id"]
    pid_idle = _backend(sid_idle).worker_pid
    pid_busy = _backend(sid_busy).worker_pid
    busy_thread = threading.Thread(target=lambda: _rlm_execute(sid_busy, "while True:\n    pass"))
    busy_thread.start()
    time.sleep(1.5)
    try:
        t0 = time.monotonic()
        server._shutdown_all_sandbox_backends()
        assert time.monotonic() - t0 < 10
        assert wait_until(lambda: not pid_alive(pid_idle), timeout=10)
        assert wait_until(lambda: not pid_alive(pid_busy), timeout=10)
    finally:
        busy_thread.join(timeout=15)
        server.session_manager.end(sid_idle)
        server.session_manager.end(sid_busy)
    # повторный вызов безопасен
    server._shutdown_all_sandbox_backends()


def test_five_concurrent_rlm_start_functional(process_mode, cf_project, monkeypatch):
    """§18.10: 5 одновременных полных rlm_start при лимите 5 сессий — все успешны,
    workers различны, tagged execute маршрутизируется верно, teardown полный."""
    # §18.10.1: лимит именно ПИНУЕМ, а не «>= 5» — иначе граница capacity,
    # ради которой тест и написан, никогда не проверяется.
    monkeypatch.setattr(server.session_manager, "_max_sessions", 5, raising=False)
    barrier = threading.Barrier(5)
    results = [None] * 5
    errors = []

    def run(i):
        try:
            barrier.wait(timeout=30)
            results[i] = json.loads(_rlm_start(path=cf_project, query=f"concurrent start {i}"))
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(5)]
    t0 = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    startup_elapsed = time.monotonic() - t0

    # sids собираем ДО любых assert: даже частично успешный запуск обязан быть
    # убран в finally, иначе до 5 живых worker-ов переживут падение теста.
    sids = [r["session_id"] for r in results if isinstance(r, dict) and r.get("session_id")]
    pids = []
    try:
        assert not errors, errors
        assert all(r is not None and "error" not in r for r in results), results
        assert len(set(sids)) == 5, "session ids уникальны"
        for sid in sids:
            backend = _backend(sid)
            assert backend is not None and backend.state == "alive" and backend.generation == 1
            pids.append(backend.worker_pid)
        assert len(set(pids)) == 5, "worker PID различны"
        # tagged execute в каждой сессии — ответ привязан к своей сессии
        for n, sid in enumerate(sids):
            r = json.loads(_rlm_execute(sid, f"print('TAG-{n}')"))
            assert r["error"] is None and r["stdout"] == f"TAG-{n}\n"
    finally:
        end_threads = [threading.Thread(target=_rlm_end, args=(sid,)) for sid in sids]
        for t in end_threads:
            t.start()
        for t in end_threads:
            t.join(timeout=30)
    for pid in pids:
        assert wait_until(lambda: not pid_alive(pid), timeout=20)
    # диагностика реального времени старта — без искусственного SLA (§18.10.7)
    print(f"five concurrent rlm_start wall time: {startup_elapsed:.1f}s")


def test_detail_full_excludes_worker_helpers(process_mode, cf_project):
    resp = _start(cf_project)
    sid = resp["session_id"]
    try:
        r = json.loads(_rlm_execute(sid, "my_marker_var = 5", detail_level="full"))
        assert "my_marker_var" in r["new_variables"]
        assert "read_file" not in r["variables"]
        assert "find_module" not in r["variables"]
    finally:
        _rlm_end(sid)


def test_five_concurrent_sessions_do_not_mix_stdout(any_mode, cf_project, monkeypatch, capfd):
    """§18.2: 5 сессий печатают по 100 тегированных строк с CPU-работой между
    ними; execute стартуют ОДНОВРЕМЕННО через barrier.

    Гоняется в ОБОИХ режимах, потому что гонка v1.28 была общей: `redirect_stdout`
    подменял глобальный `sys.stdout` процесса. Чинилась она по-разному —
    в process физическим разделением процессов, в inline глобальным замком, —
    но внешний инвариант один: каждый ответ содержит только свой тег.

    Разница только в перекрытии: в process исполнения обязаны идти параллельно,
    в inline они намеренно сериализованы (цена отказа от гонки), поэтому
    требовать перекрытия там нельзя.
    """
    monkeypatch.setattr(server.session_manager, "_max_sessions", 5, raising=False)
    sids = []
    try:
        for _ in range(5):
            sids.append(_start(cf_project)["session_id"])
        barrier = threading.Barrier(5)
        results = {}
        spans = {}

        def run(n, sid):
            # Нагрузка подобрана так, чтобы execute длился заметно дольше разброса
            # старта потоков — иначе «перекрытие» получалось бы случайно.
            code = (
                "for i in range(100):\n    print('S%d-' + str(i))\n    _burn = sum(j * j for j in range(30000))\n" % n
            )
            barrier.wait(timeout=30)
            t0 = time.monotonic()
            results[n] = json.loads(_rlm_execute(sid, code))
            spans[n] = (t0, time.monotonic())

        threads = [threading.Thread(target=run, args=(n, sid)) for n, sid in enumerate(sids)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=180)

        assert len(results) == 5, f"не все execute завершились: {sorted(results)}"
        for n in range(5):
            r = results[n]
            assert r["error"] is None, r
            lines = r["stdout"].splitlines()
            foreign = [ln for ln in lines if not ln.startswith(f"S{n}-")]
            assert not foreign, f"сессия {n} получила ЧУЖОЙ вывод: {foreign[:5]}"
            assert lines == [f"S{n}-{i}" for i in range(100)], f"сессия {n}: вывод неполон/переставлен"

        if any_mode == "process":
            # Без реального перекрытия process-ветка выродилась бы в
            # последовательный прогон и гонку бы не проверила.
            for n in range(5):
                a0, a1 = spans[n]
                assert any(m != n and spans[m][0] < a1 and a0 < spans[m][1] for m in range(5)), (
                    f"execute сессии {n} ни с кем не перекрылся — гонка не проверена"
                )
        # Для inline временных утверждений НЕТ намеренно: замеряется интервал
        # вызова в родителе, а он включает ожидание на глобальном замке, поэтому
        # интервалы законно перекрываются, хотя сам exec сериализован. Реальный
        # инвариант inline — отсутствие смешивания тегов выше; сериализация
        # проверяется напрямую в test_sandbox_stdout.py.

        out, err = capfd.readouterr()
        for n in range(5):
            assert f"S{n}-" not in out and f"S{n}-" not in err, "вывод песочницы утёк в stdout/stderr процесса"
    finally:
        for sid in sids:
            _rlm_end(sid)


def test_shutdown_force_closes_reaper_pending_even_with_empty_registry(monkeypatch):
    """Codex P1: при пустом registry функция выходила сразу, и backends, попавшие
    в reaper эвикцией прямо перед остановкой, оставались незакрытыми; а если
    registry был непуст, drain() лишь ждал до deadline и никого не добивал."""
    monkeypatch.setattr(server, "_sandbox_registry_accepting", True)
    monkeypatch.setenv("RLM_SANDBOX_SHUTDOWN_DEADLINE_SECONDS", "1")
    forced = []

    class StuckBackend:
        mode = "stuck"

        def request_close(self, reason):
            pass

        def finish_close(self, deadline):
            return CloseReport(closed=False, residual=True)

        def force_abort(self):
            forced.append(self)
            return True

    with server._sandboxes_lock:
        assert not server._sandboxes, "тест требует пустой registry"
    pending = [StuckBackend() for _ in range(2)]
    for b in pending:
        server._reaper.enqueue(b)

    t0 = time.monotonic()
    server._shutdown_all_sandbox_backends()
    elapsed = time.monotonic() - t0

    # Утверждаем про СВОИ fake-и, а не про глобальный счётчик: _reaper —
    # модульный синглтон, и жёсткий pending_count()==0 сделал бы тест зависимым
    # от порядка выполнения соседних тестов.
    assert len(forced) == 2, f"добито {len(forced)} из 2 — очередь reaper проигнорирована"
    assert set(map(id, forced)) == set(map(id, pending))
    assert elapsed < 6, f"shutdown затянулся на {elapsed:.1f}s"


def test_index_handle_released_after_rlm_end(any_mode, tmp_path, monkeypatch):
    """§18.6.6 (Windows-критично): после rlm_end файл индекса обязан стать
    удаляемым/перезаписываемым.

    Регресс из этого трека: teardown ушёл в асинхронный reaper, и открытый
    handle bsl_index.db какое-то время держал файл — на Windows это ломает
    немедленный rebuild/drop сразу после rlm_end (WinError 32).

    Inline обязан освобождать handle СИНХРОННО (процесса нет, ждать нечего);
    process — в пределах bounded времени, потому что там ожидание выхода
    worker в caller-е запрещено (§9.3).
    """
    from rlm_tools_bsl.bsl_index import IndexBuilder

    project = make_cf_project(tmp_path / "cf")
    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
    db_path = IndexBuilder().build(project, build_calls=False, build_metadata=True)

    resp = _start(project)
    assert resp["index"]["loaded"] is True, "тест бессмыслен без открытого индекса"
    _rlm_end(resp["session_id"])

    if any_mode == "inline":
        os.unlink(db_path)  # обязано пройти НЕМЕДЛЕННО
    else:
        assert wait_until(lambda: _can_unlink(db_path), timeout=20), (
            "handle индекса не освобождён — worker не завершился"
        )


def _can_unlink(path) -> bool:
    try:
        os.unlink(path)
        return True
    except PermissionError:
        return False
    except FileNotFoundError:
        return True
