"""Time-based retention for ``server.log`` — runs at service/server startup so the
log does not grow without bound.

The log is append-only and chronological, so a single cut point is enough: find the
first line whose leading ``YYYY-MM-DD HH:MM:SS`` timestamp is within the retention
window and keep everything from there on; drop everything before it. Lines without a
recognizable timestamp (the ``INFO:name:message`` records that reach the file via the
Windows service's stderr redirect) are grouped with the block they sit in — kept if
after the cut point, dropped if before.

Two Windows-specific wrinkles are handled here:
  * the service watchdog writes ``[watchdog 2026-07-05 16:24:48] …`` (timestamp NOT at
    the line start), so the timestamp regex accepts an optional ``[watchdog `` prefix —
    otherwise recent watchdog lines could be dropped when every handler-timestamped line
    is already old;
  * legacy logs written before the 1.26.1 encoding fix may hold mixed cp1251/UTF-8
    bytes, so we work on BYTES with an ASCII regex and rewrite the surviving tail
    verbatim — no decode/re-encode that would turn undecodable bytes into the Unicode
    replacement char U+FFFD.

Must be called BEFORE any writer opens ``server.log``: on Windows the service purges
before it opens the file for the child's stderr redirect (the child then skips its own
purge via ``RLM_UNDER_SERVICE``); elsewhere the child purges before its
``RotatingFileHandler`` opens the file. Rewrites atomically (sibling temp +
``os.replace``) and never raises — retention must not block startup.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta

DEFAULT_RETENTION_DAYS = 20

# Leading "2026-07-05 16:24:48" — the RotatingFileHandler asctime prefix (space or 'T'
# separator), optionally behind the service watchdog's "[watchdog " prefix. Byte regex on
# ASCII digits/punctuation so it matches regardless of the rest of the line's encoding
# (legacy mixed cp1251/UTF-8 logs); the non-timestamped basicConfig lines never match.
_TS_RE = re.compile(rb"^(?:\[watchdog )?(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})")


def log_retention_days(default: int = DEFAULT_RETENTION_DAYS) -> int:
    """Retention window in days from ``RLM_LOG_RETENTION_DAYS`` (default 20).

    ``0`` or a negative value disables purging; an unset/blank/invalid value falls back
    to *default*.
    """
    raw = os.environ.get("RLM_LOG_RETENTION_DAYS")
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def _parse_leading_ts(line: bytes) -> datetime | None:
    m = _TS_RE.match(line)
    if not m:
        return None
    try:
        return datetime(*(int(g) for g in m.groups()))
    except (ValueError, TypeError):
        return None


def purge_log_older_than(log_path, days: int = DEFAULT_RETENTION_DAYS, now: datetime | None = None) -> dict:
    """Drop lines older than *days* from *log_path* in place, preserving bytes verbatim.

    Args:
        log_path: path to the log file (``str``/``os.PathLike``).
        days: retention window. ``<= 0`` disables purging (no-op).
        now: reference "now" (defaults to ``datetime.now()``) — injectable for tests.

    Returns a stats dict ``{"status", "removed_lines", "kept_lines", "cutoff"}`` where
    ``status`` is one of:
        ``disabled``  — days <= 0;
        ``missing``   — file absent or empty;
        ``undatable`` — no line carries a recognizable timestamp → left untouched;
        ``fresh``     — every line is within the window → no-op;
        ``purged``    — file rewritten (``removed_lines`` dropped from the head);
        ``error``     — best-effort failure; the file is left untouched.

    Never raises.
    """
    result: dict = {"status": "missing", "removed_lines": 0, "kept_lines": 0, "cutoff": None}
    tmp_path = None
    try:
        if days <= 0:
            result["status"] = "disabled"
            return result

        path = os.fspath(log_path)
        if not os.path.isfile(path) or os.path.getsize(path) == 0:
            return result

        now = now or datetime.now()
        cutoff = now - timedelta(days=days)
        result["cutoff"] = cutoff.strftime("%Y-%m-%d %H:%M:%S")

        # Bytes in, bytes out: the surviving tail is written back untouched (no re-encode),
        # so a legacy mixed-encoding log keeps its exact bytes. keepends preserves CRLF/LF.
        with open(path, "rb") as fh:
            lines = fh.read().splitlines(keepends=True)

        cut = None
        saw_timestamp = False
        for i, line in enumerate(lines):
            ts = _parse_leading_ts(line)
            if ts is None:
                continue
            saw_timestamp = True
            if ts >= cutoff:
                cut = i
                break

        if cut is None:
            if not saw_timestamp:
                # Can't date anything — never nuke a log we can't reason about.
                result["status"] = "undatable"
                result["kept_lines"] = len(lines)
                return result
            # Every dated line is older than the cutoff → the whole log is stale.
            cut = len(lines)

        if cut == 0:
            result["status"] = "fresh"
            result["kept_lines"] = len(lines)
            return result

        kept = lines[cut:]
        tmp_path = path + ".purge.tmp"
        with open(tmp_path, "wb") as fh:
            fh.write(b"".join(kept))
        os.replace(tmp_path, path)
        tmp_path = None

        result["status"] = "purged"
        result["removed_lines"] = cut
        result["kept_lines"] = len(kept)
        return result
    except Exception as exc:  # startup must not fail because retention hiccupped
        result["status"] = "error"
        result["error"] = f"{type(exc).__name__}: {exc}"
        if tmp_path:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        return result
