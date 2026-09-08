"""Restart policy of the Windows service watchdog.

Regression tests for the incident where the service stopped itself after six
health-probe failures spread over 67 minutes (issue: watchdog exhausted its
restart budget).  Two defects made that inevitable:

  * a SINGLE unanswered probe terminated the server, so one 10-second hiccup
    under load killed a perfectly healthy process together with whatever work
    was in flight;
  * the restart budget was a LIFETIME counter that never decayed, so any six
    hiccups — an hour or a month apart — stopped the service for good.

The budget is cleared by PROVEN HEALTH, never by the passage of time.  An
earlier draft used a sliding time window instead, and review found three ways
that was wrong: the 900 s backoff outlived the 600 s window, so the cap held
exactly once and then fell back to 5 s; a server that had served for minutes
was punished while one that merely waited was forgiven; and a server killed
inside its readiness window cycled at a flat 5 s forever, never reaching
backoff at all.  Every one of those has a test below.

The policy is a leaf: no win32, no sockets, no clock of its own.  Time is
passed in, so whole hours are exercised without sleeping.
"""

from __future__ import annotations

from rlm_tools_bsl._service_watchdog import RestartPolicy


def _started_and_ready(now: float = 0.0) -> RestartPolicy:
    """A policy whose process is up and has answered at least one probe."""
    policy = RestartPolicy()
    policy.process_started(now)
    policy.probe_succeeded(now)
    return policy


# ---------------------------------------------------------------------------
# Killing a running server


def test_single_failed_probe_does_not_kill_a_running_server():
    """The incident itself: one missed probe used to terminate the process."""
    policy = _started_and_ready()

    assert policy.probe_failed(35.0) is False


def test_third_consecutive_failure_kills_the_server():
    """Sustained silence still has to be caught — that is what the probe is for."""
    policy = _started_and_ready()

    assert policy.probe_failed(35.0) is False
    assert policy.probe_failed(65.0) is False
    assert policy.probe_failed(95.0) is True


def test_one_good_probe_clears_the_failure_streak():
    """Isolated hiccups must not accumulate across minutes of healthy service."""
    policy = _started_and_ready()

    policy.probe_failed(35.0)
    policy.probe_failed(65.0)
    policy.probe_succeeded(95.0)

    assert policy.probe_failed(125.0) is False


# ---------------------------------------------------------------------------
# Starting up


def test_startup_is_not_killed_while_the_server_is_still_coming_up():
    """A cold-booted machine needs more than the 35 s to the first probe.

    The reported incident began 77 seconds after boot, which is where the very
    first probe lands — the server had simply not finished starting.
    """
    policy = RestartPolicy()
    policy.process_started(0.0)

    assert policy.probe_failed(35.0) is False
    assert policy.probe_failed(65.0) is False
    assert policy.probe_failed(95.0) is False


def test_a_server_that_never_answers_is_killed_after_the_readiness_window():
    policy = RestartPolicy()
    policy.process_started(0.0)

    policy.probe_failed(35.0)
    policy.probe_failed(65.0)
    policy.probe_failed(95.0)

    assert policy.probe_failed(125.0) is True


def test_readiness_grace_ends_at_the_first_answer_not_at_the_deadline():
    """Once a server has answered, it is up: later silence is a wedge, not a slow start."""
    policy = RestartPolicy()
    policy.process_started(0.0)
    policy.probe_succeeded(0.0)

    assert policy.probe_failed(35.0) is False
    assert policy.probe_failed(65.0) is False
    assert policy.probe_failed(95.0) is True


def test_a_restarted_process_gets_a_full_readiness_window_of_its_own():
    """Three misses, not one: with a single probe the test passes even when the
    readiness flag is never reset, because the streak counter alone carries it."""
    policy = _started_and_ready()
    policy.probe_failed(35.0)
    policy.probe_failed(65.0)
    assert policy.probe_failed(95.0) is True

    policy.note_restart(95.0)
    policy.process_started(100.0)

    assert policy.probe_failed(135.0) is False
    assert policy.probe_failed(165.0) is False
    assert policy.probe_failed(195.0) is False


# ---------------------------------------------------------------------------
# The restart budget


def test_a_long_healthy_run_before_each_restart_never_reaches_backoff():
    """THE defect: restarts an hour apart used to accumulate until the service died.

    Twenty of them, each after an hour of answered probes, must all stay ordinary.
    """
    policy = RestartPolicy()
    t = 0.0

    for _ in range(20):
        policy.process_started(t)
        policy.probe_succeeded(t + 35.0)
        policy.probe_succeeded(t + 3600.0)
        assert policy.note_restart(t + 3630.0) == 5.0
        t += 3635.0


def test_a_burst_of_restarts_backs_off_instead_of_giving_up():
    """Five failed starts in a row means the server is genuinely broken.

    The service must slow down rather than stop: whatever broke (a busy port, an
    index disk that appears late in boot) may well clear on its own.
    """
    policy = RestartPolicy()

    for i in range(5):
        policy.process_started(i * 10.0)
        assert policy.note_restart(i * 10.0 + 5.0) == 5.0

    policy.process_started(60.0)
    assert policy.note_restart(65.0) == 60.0


def test_backoff_holds_at_its_cap_on_a_timeline_the_loop_can_actually_produce():
    """Regression: the cap used to hold exactly once.

    With a time-window budget, the 900 s pause outlived the 600 s window, so the
    budget drained during the very pause it had just imposed and the next restart
    was back to 5 s — a sawtooth that gave a dead server ten starts per half hour.
    """
    policy = RestartPolicy()
    t = 0.0
    delays = []

    for _ in range(12):
        policy.process_started(t)
        delay = policy.note_restart(t + 5.0)  # dies at once, never answers a probe
        delays.append(delay)
        t += 5.0 + delay

    assert delays == [5.0, 5.0, 5.0, 5.0, 5.0, 60.0, 120.0, 300.0, 900.0, 900.0, 900.0, 900.0]


def test_a_server_killed_inside_its_readiness_window_still_reaches_backoff():
    """Regression: this cycle is ~130 s, so a time-window budget never filled up and
    a server too slow to answer was killed at a flat 5 s for as long as it existed."""
    policy = RestartPolicy()
    t = 0.0
    delays = []

    for _ in range(8):
        policy.process_started(t)
        for probe in (35.0, 65.0, 95.0, 125.0):
            policy.probe_failed(t + probe)
        delay = policy.note_restart(t + 125.0)
        delays.append(delay)
        t += 125.0 + delay

    assert delays[-1] > 5.0, delays


def test_proven_health_clears_the_budget():
    """Recovery must be automatic, and it must be EARNED — no operator, no reinstall."""
    policy = RestartPolicy()
    t = 0.0
    for _ in range(6):
        policy.process_started(t)
        delay = policy.note_restart(t + 5.0)
        t += 5.0 + delay
    assert delay > 5.0

    policy.process_started(t)
    policy.probe_succeeded(t + 35.0)
    policy.probe_succeeded(t + 400.0)

    assert policy.note_restart(t + 430.0) == 5.0


def test_a_brief_healthy_spell_does_not_clear_the_budget():
    """Answering for half a minute is not proof: a crash loop can do that much."""
    policy = RestartPolicy()
    t = 0.0
    for _ in range(6):
        policy.process_started(t)
        delay = policy.note_restart(t + 5.0)
        t += 5.0 + delay

    policy.process_started(t)
    policy.probe_succeeded(t + 35.0)
    policy.probe_succeeded(t + 65.0)

    assert policy.note_restart(t + 95.0) > 5.0


def test_waiting_alone_never_clears_the_budget():
    """The inverse of the rule: time served is not health proven."""
    policy = RestartPolicy()
    t = 0.0
    for _ in range(6):
        policy.process_started(t)
        delay = policy.note_restart(t + 5.0)
        t += 5.0 + delay

    policy.process_started(t + 86400.0)

    assert policy.note_restart(t + 86405.0) > 5.0


def test_policy_never_reports_that_it_has_given_up():
    """There is no state in which the watchdog stops trying."""
    policy = RestartPolicy()
    t = 0.0

    for _ in range(50):
        policy.process_started(t)
        delay = policy.note_restart(t + 5.0)
        assert delay > 0.0
        t += 5.0 + delay


def test_backing_off_is_reported_so_the_log_can_say_why():
    policy = RestartPolicy()
    t = 0.0

    policy.process_started(t)
    policy.note_restart(t + 5.0)
    assert policy.backing_off is False
    assert policy.failed_starts == 1

    for _ in range(5):
        t += 10.0
        policy.process_started(t)
        policy.note_restart(t + 5.0)

    assert policy.backing_off is True
    assert policy.failed_starts == 6


def _spend_the_budget(policy: RestartPolicy, rounds: int = 6, start: float = 0.0) -> float:
    """Run `rounds` starts that never prove healthy; return the last delay."""
    t = start
    delay = 0.0
    for _ in range(rounds):
        policy.process_started(t)
        delay = policy.note_restart(t + 5.0)
        t += 5.0 + delay
    return delay


def test_recovering_from_the_cap_starts_the_backoff_over():
    """Without clearing the step too, a server that recovered and then hit trouble
    again would jump straight back to a 15-minute pause instead of a 60-second one."""
    policy = RestartPolicy()
    assert _spend_the_budget(policy, rounds=9) == 900.0

    policy.process_started(10_000.0)
    policy.probe_succeeded(10_035.0)
    policy.probe_succeeded(10_400.0)
    assert policy.note_restart(10_430.0) == 5.0

    # Six, not five: the healthy start above CLEARED the budget rather than spending
    # a unit of it, so all five quick retries come first and the sixth backs off.
    assert _spend_the_budget(policy, rounds=6, start=11_000.0) == 60.0


def test_health_proven_exactly_at_the_threshold_counts():
    policy = RestartPolicy()
    assert _spend_the_budget(policy) > 5.0

    policy.process_started(10_000.0)
    policy.probe_succeeded(10_035.0)
    policy.probe_succeeded(10_335.0)  # exactly five minutes of answered probes

    assert policy.note_restart(10_400.0) == 5.0


def test_the_healthy_span_is_measured_from_the_first_answer_not_from_the_start():
    """A server that took 35 s to come up and then answered for 290 s has NOT proven
    five minutes of health, even though 325 s passed since it was launched."""
    policy = RestartPolicy()
    assert _spend_the_budget(policy) > 5.0

    policy.process_started(10_000.0)
    policy.probe_succeeded(10_035.0)
    policy.probe_succeeded(10_325.0)

    assert policy.note_restart(10_330.0) > 5.0


def test_a_healthy_spell_ends_at_the_last_answer_not_at_the_death():
    """A server that answered for 210 s and then wedged spends another 90-120 s being
    missed before it is killed. Counting that silence as health would let a server
    that never manages five good minutes clear the budget over and over."""
    policy = RestartPolicy()
    assert _spend_the_budget(policy) > 5.0

    policy.process_started(10_000.0)
    policy.probe_succeeded(10_035.0)
    policy.probe_succeeded(10_245.0)

    assert policy.note_restart(10_345.0) > 5.0


def test_a_proven_healthy_process_is_not_counted_as_a_failed_start():
    """The counter is defined as consecutive starts that never proved healthy, so the
    death of one that DID prove itself must not spend a unit of the budget — the five
    quick retries have to be there in full for whatever comes next."""
    policy = RestartPolicy()
    assert _spend_the_budget(policy) > 5.0

    policy.process_started(10_000.0)
    policy.probe_succeeded(10_035.0)
    policy.probe_succeeded(10_400.0)

    assert policy.note_restart(10_430.0) == 5.0
    assert policy.failed_starts == 0
