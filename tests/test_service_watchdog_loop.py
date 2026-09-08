"""The watchdog loop itself, driven end to end on a fake clock.

`test_service_watchdog_policy.py` covers the decisions; this file covers the
WIRING, which is where the decisions can be attached wrongly and no policy test
would notice.  Review found exactly that gap: swapping `_wait_for_stop(delay)`
back for a plain sleep, or dropping `policy.process_started(...)`, left the whole
suite green.  Both are caught here.

Nothing sleeps: the fake `WaitForSingleObject` advances a counter, so an hour of
service life costs microseconds and the timeline is exact rather than flaky.
"""

from __future__ import annotations

import subprocess
import sys
import types

import pytest
from test_service_win_integration import _import_service_win, _install_stubs

WAIT_TIMEOUT = 258
SIGNALLED = 0


class _FakeProc:
    """A child that lives until `dies_at`, then reports `exit_code`."""

    def __init__(self, world, lifetime: float | None, exit_code: int) -> None:
        self._world = world
        self._dies_at = None if lifetime is None else world.now + lifetime
        self._exit_code = exit_code
        self.returncode = None

    def poll(self):
        if self._dies_at is not None and self._world.now >= self._dies_at:
            self.returncode = self._exit_code
        return self.returncode

    def terminate(self):
        self._dies_at = self._world.now
        self._exit_code = 1
        self.returncode = 1
        self._world.terminated += 1

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.terminate()

    def svcstop_kill(self):
        """Killed by SvcStop, not by the watchdog — not a restart decision."""
        self._dies_at = self._world.now
        self._exit_code = 1
        self.returncode = 1


class _World:
    """Fakes the clock, the child process, the stop event and the health probe."""

    def __init__(self, mod, *, healthy, lifetime=None, exit_code=1, stop_at, stop_during_probe=None):
        self.mod = mod
        self.now = 0.0
        self.healthy = healthy  # callable(probe_index) -> bool
        self.lifetime = lifetime  # seconds the child stays alive, None = forever
        self.exit_code = exit_code
        self.stop_at = stop_at
        # Probe ordinal at which SvcStop arrives while the watchdog is INSIDE the
        # probe, where it cannot see the event -- see check_health().
        self.stop_during_probe = stop_during_probe
        self.stopped = False
        self.child: _FakeProc | None = None
        self.launches: list[float] = []
        self.waits: list[int] = []
        self.probes = 0
        self.terminated = 0

    def popen(self, argv, env=None, stdout=None, stderr=None):
        self.launches.append(self.now)
        self.child = _FakeProc(self, self.lifetime, self.exit_code)
        return self.child

    def wait_for(self, handle, ms):
        """Advance the clock, or report the stop event if it falls inside the wait.

        The child is deliberately left ALIVE here: this models the SvcStop whose
        snapshot of `self._proc` was already stale, so the process running now is
        the loop's own to clean up on the way out.
        """
        self.waits.append(ms)
        if self.stopped:
            return SIGNALLED
        dt = ms / 1000.0
        if self.now + dt >= self.stop_at:
            self.now = max(self.now, self.stop_at)
            self.stopped = True
            return SIGNALLED
        self.now += dt
        return WAIT_TIMEOUT

    def check_health(self, url):
        self.probes += 1
        if self.stop_during_probe == self.probes:
            # The other SvcStop: it did catch the child. The loop meets the dead
            # process before it ever looks at the event.
            self.stopped = True
            if self.child is not None:
                self.child.svcstop_kill()
        return self.healthy(self.probes)


def _run(monkeypatch, tmp_path, request, *, stopping=False, **kwargs) -> tuple[_World, str]:
    """Run the real `_serve()` against a fake world; return the world and the log."""
    _install_stubs(monkeypatch)
    win32event = sys.modules["win32event"]
    win32event.WAIT_TIMEOUT = WAIT_TIMEOUT
    mod = _import_service_win(monkeypatch, request)

    world = _World(mod, **kwargs)
    win32event.WaitForSingleObject = world.wait_for
    monkeypatch.setattr(mod, "time", types.SimpleNamespace(monotonic=lambda: world.now))
    monkeypatch.setattr(
        mod,
        "subprocess",
        types.SimpleNamespace(
            Popen=world.popen,
            STDOUT=subprocess.STDOUT,
            TimeoutExpired=subprocess.TimeoutExpired,
        ),
    )
    monkeypatch.setattr(mod, "_check_health", world.check_health)
    monkeypatch.setattr(mod, "_config_path", lambda: tmp_path / "service.json")
    monkeypatch.setattr(
        mod,
        "load_config",
        lambda: {"host": "127.0.0.1", "port": 9000, "env_file": None, "exe_path": "rlm-tools-bsl"},
    )

    service = object.__new__(mod.RlmWindowsService)
    service._stop_event = "stop-event"
    service._git_capture_dir = None
    service._git_capture_error = None
    service._git_capture_swept = 0
    service._stopping = stopping
    service._serve()

    log = (tmp_path / "logs" / "server.log").read_text(encoding="utf-8")
    assert "Fatal" not in log, log
    return world, log


@pytest.fixture
def run_watchdog(monkeypatch, tmp_path, request):
    def _factory(**kwargs):
        return _run(monkeypatch, tmp_path, request, **kwargs)

    return _factory


# ---------------------------------------------------------------------------


def test_isolated_missed_probes_never_restart_the_server(run_watchdog):
    """The incident, replayed through the real loop: two hours of occasional misses.

    Every tenth probe is missed — far more often than the reported machine — and
    the server must survive all of it untouched.
    """
    world, log = run_watchdog(healthy=lambda i: i % 10 != 0, stop_at=7200.0)

    assert world.launches == [0.0]
    assert "terminating process" not in log
    assert "Health check missed" in log
    assert "Health check recovered" in log


def test_a_server_that_served_and_then_went_quiet_is_replaced(run_watchdog):
    """The protection itself must still work — this is what the probe is for.

    Two answered probes make the server ready, so the readiness window no longer
    applies and the third miss in a row ends it.
    """
    world, log = run_watchdog(healthy=lambda i: i <= 2, stop_at=600.0)

    assert world.terminated >= 1
    assert len(world.launches) >= 2
    assert "failed 3 times in a row" in log


def test_the_watchdog_keeps_restarting_far_past_the_old_budget(run_watchdog):
    """The service used to stop for good after six starts. It must never stop now."""
    world, _ = run_watchdog(healthy=lambda i: False, stop_at=7200.0)

    assert len(world.launches) > 6


def test_a_crash_loop_backs_off_and_says_why(run_watchdog):
    world, log = run_watchdog(healthy=lambda i: True, lifetime=0.0, stop_at=4000.0)

    assert len(world.launches) > 6
    assert max(world.waits) >= 60_000
    assert "backing off" in log
    assert "does not give up" in log


def test_stop_during_a_backoff_is_not_waited_out(run_watchdog):
    """A plain sleep here would make `net stop` hang for the whole backoff.

    Five failed starts take 10 s each, so the first 60 s backoff begins at t=55.
    Stopping at t=70 must return then and there, not at t=115.
    """
    world, _ = run_watchdog(healthy=lambda i: True, lifetime=0.0, stop_at=70.0)

    assert world.waits[-1] == 60_000, world.waits[-6:]
    assert world.now == 70.0


def test_each_restarted_process_gets_its_own_readiness_window(run_watchdog):
    """Drops `policy.process_started(...)` from the loop: without it the second and
    later processes are judged against the first one's start time, so a slow restart
    is killed on its third probe instead of being given its readiness window."""
    world, log = run_watchdog(healthy=lambda i: False, stop_at=1000.0)

    kills = log.count("terminating process")
    # 5 s grace + probes at +35/+65/+95 inside the window, killed on the one at +125.
    assert kills >= 2
    for launch, next_launch in zip(world.launches, world.launches[1:]):
        assert next_launch - launch >= 125.0, (launch, next_launch)


def test_the_stop_event_is_manual_reset(monkeypatch, request):
    """SvcDoRun and the watchdog thread both wait on this one event, and SvcStop sets
    it once. An auto-reset event releases exactly ONE waiter, so the watchdog could be
    left sitting out a 15-minute backoff after `net stop` was already accepted."""
    mod = _service_class_with_real_base(monkeypatch, request)

    created: list[tuple] = []
    sys.modules["win32event"].CreateEvent = lambda *args: created.append(args) or "stop-event"

    mod.RlmWindowsService(["rlm-tools-bsl"])

    assert created, "no stop event was created"
    assert created[0][1], f"the stop event must be manual-reset, got CreateEvent{created[0]}"


def test_the_child_is_not_left_running_when_the_loop_stops(run_watchdog):
    """SvcStop terminates the process it can SEE, and the loop can assign a new one
    right after that snapshot. Left alive, the orphan keeps the port and the next
    `net start` cannot bind — which the new policy then retries every 15 minutes."""
    world, _ = run_watchdog(healthy=lambda i: True, stop_at=3.0)

    assert world.terminated == 1


def test_a_stop_delivered_during_a_probe_is_not_logged_as_a_restart(run_watchdog):
    """`net stop` kills the child, and the loop meets the dead process before the
    event — the probe does not check it. Announcing `restarting in 5 s` right before
    the service stops tells the operator the opposite of what happened."""
    _world, log = run_watchdog(healthy=lambda i: True, stop_at=10_000.0, stop_during_probe=2)

    assert "restarting in" not in log


def test_a_failed_log_write_costs_a_line_not_the_service(monkeypatch, request):
    """A full disk must not be able to stop the watchdog.

    `_log_watchdog` writes and flushes a line-buffered file; on ENOSPC that raises,
    the exception escapes the loop, and the thread body stops the service — over a
    diagnostic line, while the server it was watching is perfectly healthy.
    """
    _install_stubs(monkeypatch)
    mod = _import_service_win(monkeypatch, request)

    class _FullDisk:
        def write(self, _text):
            raise OSError(28, "No space left on device")

        def flush(self):
            raise OSError(28, "No space left on device")

    mod._log_watchdog(_FullDisk(), "anything at all")


def test_an_exception_in_the_loop_does_not_leave_the_child_running(monkeypatch, tmp_path, request):
    """The loop has a fourth exit — the exception one — and the thread body stops the
    service on it. Without cleanup there, a crash inside the loop leaves the server
    it had just started alive, holding the port against every later start."""
    _install_stubs(monkeypatch)
    sys.modules["win32event"].SetEvent = lambda *_a: None
    mod = _import_service_win(monkeypatch, request)
    monkeypatch.setattr(mod, "_config_path", lambda: tmp_path / "service.json")

    world = _World(mod, healthy=lambda i: True, stop_at=1e9)
    child = _FakeProc(world, None, 1)

    def _boom(self):
        self._proc = child
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(mod.RlmWindowsService, "_serve", _boom)
    service = object.__new__(mod.RlmWindowsService)
    service._stop_event = "stop-event"

    service._run_server()

    assert child.poll() is not None, "the server process was left running"


def test_a_stop_delivered_during_a_failing_probe_is_not_logged_as_a_miss(run_watchdog):
    """The real `net stop` kills the child WHILE the probe waits, so the probe comes
    back as a failure. Reporting `giving the server more time` moments before the
    service stops is the same wrong reason as the restart line, one notch quieter."""
    _world, log = run_watchdog(healthy=lambda i: i != 2, stop_at=10_000.0, stop_during_probe=2)

    assert "Health check missed" not in log


def test_a_stop_while_the_server_is_healthy_also_cleans_up_the_child(run_watchdog):
    """Pins the cleanup on the tick-loop exit, not just the one after the grace wait."""
    world, _ = run_watchdog(healthy=lambda i: True, stop_at=20.0)

    assert world.terminated == 1


def test_a_requested_stop_is_seen_before_the_event_is_set(monkeypatch, request):
    """SvcStop spends up to 10 s terminating the child BEFORE it sets the event.

    Until then the loop sees no stop at all: it reads the killed child as a failed
    start, logs a restart that is never coming, and can launch one more server.
    """
    _install_stubs(monkeypatch)
    mod = _import_service_win(monkeypatch, request)

    waited = []
    win32event = sys.modules["win32event"]
    win32event.WAIT_TIMEOUT = WAIT_TIMEOUT
    win32event.WaitForSingleObject = lambda handle, ms: waited.append(ms) or WAIT_TIMEOUT

    service = object.__new__(mod.RlmWindowsService)
    service._stop_event = "stop-event"
    service._stopping = True

    assert service._wait_for_stop(900) is True
    assert waited == [], "a requested stop must not be waited on"


def test_svcstop_records_the_intent_before_it_touches_the_process(monkeypatch, request):
    """Ordering is the whole fix, and the SNAPSHOT is the moment that matters.

    The flag has to be up before SvcStop reads `_proc`: only then is that read the
    last word, because the loop checks the same flag under the same lock before it
    publishes anything. Set it after the read and the loop can still slip a new
    server in behind the snapshot.
    """
    _install_stubs(monkeypatch)
    sys.modules["win32event"].SetEvent = lambda *_a: None
    sys.modules["win32service"].SERVICE_STOP_PENDING = 3
    mod = _import_service_win(monkeypatch, request)

    seen = []
    service = object.__new__(mod.RlmWindowsService)
    service._stop_event = "stop-event"
    service._stopping = False

    class _RecordingLock:
        def __enter__(self):
            seen.append(("lock", service._stopping))
            return self

        def __exit__(self, *_exc):
            return False

    service._proc_lock = _RecordingLock()

    class _Proc:
        returncode = None

        def poll(self):
            return None

        def terminate(self):
            seen.append(("terminate", service._stopping))

        def wait(self, timeout=None):
            seen.append(("wait", service._stopping))
            return 0

        def kill(self):
            pass

    service._proc = _Proc()
    service.ReportServiceStatus = lambda _status: seen.append(("report", service._stopping))

    service.SvcStop()

    assert seen == [("report", False), ("lock", True), ("terminate", True), ("wait", True)], seen


def test_no_child_is_launched_once_a_stop_has_been_requested(run_watchdog):
    """A stop delivered while `Popen` runs used to terminate the process SvcStop had
    seen — the previous, already dead one — leaving the brand new server alive and
    holding the port against every later start."""
    world, _log = run_watchdog(healthy=lambda i: True, stop_at=100.0, stopping=True)

    assert world.launches == []


def _service_class_with_real_base(monkeypatch, request):
    """Import `_service_win` on a base class that accepts pywin32's constructor args.

    `_install_stubs` uses bare `object`, which cannot take them — and the point here
    is the constructor itself, so it has to run for real.
    """
    _install_stubs(monkeypatch)

    class _Base:
        def __init__(self, args):
            self.ssh = None

    sys.modules["win32serviceutil"].ServiceFramework = _Base
    win32event = sys.modules["win32event"]
    win32event.WAIT_TIMEOUT = WAIT_TIMEOUT
    win32event.INFINITE = -1
    win32event.CreateEvent = lambda *_a: "stop-event"
    win32event.SetEvent = lambda *_a: None
    win32event.WaitForSingleObject = lambda *_a: SIGNALLED
    sys.modules["win32service"].SERVICE_STOP_PENDING = 3
    sys.modules["win32service"].SERVICE_RUNNING = 4
    return _import_service_win(monkeypatch, request)


def test_a_stop_arriving_before_svcdorun_does_not_crash_the_control_handler(monkeypatch, request):
    """pywin32 registers the control handler in ServiceFramework.__init__ and SvcRun
    reports SERVICE_RUNNING BEFORE calling SvcDoRun. A quick `net start && net stop`
    lands in that window, where SvcStop would reach for state SvcDoRun has not
    created yet — an AttributeError raised inside the SCM's own callback."""
    mod = _service_class_with_real_base(monkeypatch, request)

    service = mod.RlmWindowsService(["rlm-tools-bsl"])
    service.ReportServiceStatus = lambda *_a, **_kw: None

    service.SvcStop()

    assert service._stopping is True


def test_svcdorun_honours_a_stop_that_already_arrived(monkeypatch, request):
    """The stop must survive SvcDoRun: re-initialising the state there would drop it,
    and the service would start a server the SCM has already been told is stopping."""
    mod = _service_class_with_real_base(monkeypatch, request)
    monkeypatch.setattr(mod, "_prepare_git_capture_dir", lambda: None)
    monkeypatch.setattr(mod, "_sweep_git_capture_dir", lambda _dir: 0)

    started = []
    monkeypatch.setattr(mod.RlmWindowsService, "_run_server", lambda self: started.append("served"))

    reported = []
    service = mod.RlmWindowsService(["rlm-tools-bsl"])
    service.ReportServiceStatus = lambda status, *_a, **_kw: reported.append(status)

    service.SvcStop()
    service.SvcDoRun()

    assert service._stopping is True, "SvcDoRun overwrote the accepted stop"
    assert started == [], "a server was started after the stop was accepted"
    assert reported == [3], f"only SvcStop's own STOP_PENDING may be reported, got {reported}"


def test_svcdorun_reports_no_status_of_its_own(monkeypatch, request):
    """pywin32 reports SERVICE_RUNNING in SvcRun immediately BEFORE calling SvcDoRun,
    so a second report here says nothing new — and can land after a stop, painting
    SERVICE_RUNNING over the SERVICE_STOP_PENDING SvcStop has just sent.

    The stop lands while the watchdog thread is being started, which is past the
    `_stopping` check and therefore invisible to it.
    """
    mod = _service_class_with_real_base(monkeypatch, request)
    monkeypatch.setattr(mod, "_prepare_git_capture_dir", lambda: None)
    monkeypatch.setattr(mod, "_sweep_git_capture_dir", lambda _dir: 0)
    monkeypatch.setattr(mod.RlmWindowsService, "_run_server", lambda self: None)

    reported: list[int] = []
    service = mod.RlmWindowsService(["rlm-tools-bsl"])
    service.ReportServiceStatus = lambda status, *_a, **_kw: reported.append(status)

    class _ThreadThatIsStoppedMidStart:
        def __init__(self, target=None, daemon=None):
            self._target = target

        def start(self):
            service.SvcStop()

    real_threading = mod.threading
    monkeypatch.setattr(
        mod,
        "threading",
        types.SimpleNamespace(Thread=_ThreadThatIsStoppedMidStart, Lock=real_threading.Lock),
    )

    service.SvcDoRun()

    assert reported == [3], f"expected only SvcStop's STOP_PENDING, got {reported}"
