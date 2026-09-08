"""Restart policy for the Windows service watchdog.

Leaf module on purpose: no win32, no sockets, no file I/O and no clock of its
own.  The watchdog loop in `_service_win.py` stays thin glue around it, and the
whole timeline — hours of it — is exercised in tests without sleeping.

Why this exists.  The watchdog used to decide two things inline, and both were
wrong in a way that only showed up after a long uptime:

  * ONE unanswered `/health` probe terminated the server.  The endpoint is a
    bare `JSONResponse` — it cannot break, it can only be late, and under a
    heavy helper (or a cold disk, or an antivirus sweep) being late by the
    10-second probe timeout is ordinary.  So a single hiccup killed a healthy
    server together with whatever work was in flight.
  * The restart budget was a LIFETIME counter.  It was initialised once, before
    the loop, and never decayed, so it was not a rate limit at all: any six
    hiccups over the entire life of the service — an hour or a month apart —
    exhausted it.  A service left running long enough was guaranteed to die.

Hence: kill only on SUSTAINED silence, count only starts that FAILED TO PROVE
themselves, and never give up — back off instead.  Giving up is the one outcome
an always-on service cannot recover from on its own, while the causes we
actually see (a port still held by the previous process, an index disk that
appears late in boot) clear themselves given time.

What clears the budget is proven health, never elapsed time.  A draft of this
module used a sliding time window and review found three separate faults in it:
the 900 s backoff outlived the 600 s window, so the cap held exactly once
before the budget drained during its own pause and dropped back to 5 s; a
server that had actually served for minutes was punished while one that merely
sat there was forgiven; and a server killed inside its readiness window ran a
~130 s cycle that never filled the window, so it was killed at a flat 5 s
forever.  Counting consecutive starts that never proved healthy has none of
those edges, and needs no window at all.
"""

# Consecutive missed probes before the server is considered wedged.  Probes are
# 30 s apart, so this is ~90-120 s of silence — long enough to outlast a busy
# helper, short enough to catch a real hang well inside a working session.
DEFAULT_FAILURES_BEFORE_KILL = 3
# A freshly started server has this long to answer its FIRST probe before the
# rule above applies at all.  The reported incident began 77 s after boot, which
# is exactly where the first probe lands: the server had not finished starting.
DEFAULT_READINESS_SECONDS = 120.0
# Failed starts in a row before the wait between restarts starts growing.
DEFAULT_MAX_RESTARTS = 5
# A start counts as PROVEN once the server has answered probes over at least
# this long.  Anything shorter is within reach of a crash loop, and a server
# that cannot stay up for five minutes is exactly what backoff is for.
DEFAULT_HEALTHY_RESET_SECONDS = 300.0
DEFAULT_RESTART_DELAY_SECONDS = 5.0
# Applied in order once the budget is spent, then the last one repeats forever.
DEFAULT_BACKOFF_SECONDS = (60.0, 120.0, 300.0, 900.0)


class RestartPolicy:
    """Decides when to kill a wedged server and how long to wait before restarting.

    All times are caller-supplied seconds from an arbitrary origin; the watchdog
    passes `time.monotonic()`, which no clock change can move backwards.
    """

    def __init__(
        self,
        *,
        failures_before_kill: int = DEFAULT_FAILURES_BEFORE_KILL,
        readiness_seconds: float = DEFAULT_READINESS_SECONDS,
        max_restarts: int = DEFAULT_MAX_RESTARTS,
        healthy_reset_seconds: float = DEFAULT_HEALTHY_RESET_SECONDS,
        restart_delay_seconds: float = DEFAULT_RESTART_DELAY_SECONDS,
        backoff_seconds: tuple[float, ...] = DEFAULT_BACKOFF_SECONDS,
    ) -> None:
        self._failures_before_kill = failures_before_kill
        self._readiness_seconds = readiness_seconds
        self._max_restarts = max_restarts
        self._healthy_reset_seconds = healthy_reset_seconds
        self._restart_delay_seconds = restart_delay_seconds
        self._backoff_seconds = backoff_seconds
        self._failed_starts = 0
        self._backoff_step = 0
        self._backing_off = False
        # Per-process state, reset by process_started().
        self._started_at: float | None = None
        self._ready = False
        self._consecutive_failures = 0
        self._first_ok_at: float | None = None
        self._last_ok_at: float | None = None

    # -- the running process ------------------------------------------------

    def process_started(self, now: float) -> None:
        """A new server process is up; its readiness window starts now."""
        self._started_at = now
        self._ready = False
        self._consecutive_failures = 0
        self._first_ok_at = None
        self._last_ok_at = None

    def probe_succeeded(self, now: float) -> None:
        """The server answered.  It is up, and its failure streak is over."""
        self._ready = True
        self._consecutive_failures = 0
        if self._first_ok_at is None:
            self._first_ok_at = now
        self._last_ok_at = now

    def probe_failed(self, now: float) -> bool:
        """Record a missed probe.  True means the process should be terminated.

        The streak is counted during the readiness window too, so a server that
        never answers is killed on the first probe past the deadline rather than
        being granted a fresh count.
        """
        self._consecutive_failures += 1
        if not self._ready and self._started_at is not None and now - self._started_at < self._readiness_seconds:
            return False
        return self._consecutive_failures >= self._failures_before_kill

    @property
    def consecutive_failures(self) -> int:
        """Missed probes since the last answer — for the log line, not decisions."""
        return self._consecutive_failures

    # -- restarting ---------------------------------------------------------

    def note_restart(self, now: float) -> float:
        """Record that the server died and return the wait before starting it again.

        Never returns "give up": once the budget is spent the wait grows along
        `backoff_seconds` and then holds at its last value for as long as the
        server keeps failing, so a genuinely broken one is retried rarely instead
        of abandoned.  `now` is accepted for symmetry with the rest of the API;
        the decision rests on what the process proved, not on when it died.
        """
        if self._healthy_span() >= self._healthy_reset_seconds:
            # A start that proved itself is not a failed one: it CLEARS the budget
            # rather than spending a unit of it, so all five quick retries remain
            # available for whatever comes next.
            self._failed_starts = 0
            self._backoff_step = 0
            self._backing_off = False
            return self._restart_delay_seconds

        self._failed_starts += 1

        if self._failed_starts <= self._max_restarts:
            self._backing_off = False
            return self._restart_delay_seconds

        self._backing_off = True
        delay = self._backoff_seconds[min(self._backoff_step, len(self._backoff_seconds) - 1)]
        self._backoff_step += 1
        return delay

    @property
    def failed_starts(self) -> int:
        """Starts in a row that never proved healthy — for the log line."""
        return self._failed_starts

    @property
    def backing_off(self) -> bool:
        """Whether the last `note_restart` returned a backoff rather than the usual wait."""
        return self._backing_off

    def _healthy_span(self) -> float:
        """How long the process that just died had been answering probes."""
        if self._first_ok_at is None or self._last_ok_at is None:
            return 0.0
        return self._last_ok_at - self._first_ok_at
