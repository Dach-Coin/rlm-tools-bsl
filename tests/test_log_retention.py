"""v1.27.0 — time-based server.log retention.

On startup the server/service drops log entries older than RLM_LOG_RETENTION_DAYS
(default 20) so server.log doesn't grow unbounded. The log is append-only and
chronological, so a single cut point suffices; mixed formats (timestamped
RotatingFileHandler lines + non-timestamped INFO:name:msg stderr-redirect lines)
are grouped with the block they sit in.
"""

from __future__ import annotations

from datetime import datetime

from rlm_tools_bsl.log_retention import (
    DEFAULT_RETENTION_DAYS,
    log_retention_days,
    purge_log_older_than,
)

# cutoff at 20 days back = 2026-06-15 12:00:00
NOW = datetime(2026, 7, 5, 12, 0, 0)

_OLD_1 = "2026-06-01 10:00:00 INFO x: old rlm_start\n"
_OLD_STDERR = "INFO:rlm_tools_bsl.server:rlm_execute old redirect line\n"  # no timestamp
_OLD_2 = "2026-06-10 09:30:00 INFO x: old rlm_end\n"
_RECENT_1 = "2026-07-04 08:00:00 INFO x: recent rlm_start\n"  # first line >= cutoff
_RECENT_STDERR = "INFO:rlm_tools_bsl.server:rlm_execute recent redirect line\n"
_RECENT_2 = "2026-07-05 09:00:00 INFO x: recent rlm_end\n"


def _write(path, lines):
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write("".join(lines))


def _read(path):
    with open(path, encoding="utf-8", newline="") as f:
        return f.read()


def test_mixed_cuts_at_first_recent_line(tmp_path):
    p = tmp_path / "server.log"
    _write(p, [_OLD_1, _OLD_STDERR, _OLD_2, _RECENT_1, _RECENT_STDERR, _RECENT_2])
    stats = purge_log_older_than(p, days=20, now=NOW)
    assert stats["status"] == "purged"
    assert stats["removed_lines"] == 3
    assert stats["kept_lines"] == 3
    assert _read(p) == "".join([_RECENT_1, _RECENT_STDERR, _RECENT_2])


def test_recent_nontimestamped_line_survives(tmp_path):
    p = tmp_path / "server.log"
    _write(p, [_OLD_1, _RECENT_1, _RECENT_STDERR])
    purge_log_older_than(p, days=20, now=NOW)
    content = _read(p)
    assert _RECENT_STDERR in content
    assert _OLD_1 not in content


def test_all_fresh_is_noop(tmp_path):
    p = tmp_path / "server.log"
    content = [_RECENT_1, _RECENT_STDERR, _RECENT_2]
    _write(p, content)
    stats = purge_log_older_than(p, days=20, now=NOW)
    assert stats["status"] == "fresh"
    assert stats["removed_lines"] == 0
    assert _read(p) == "".join(content)


def test_all_old_truncates_to_empty(tmp_path):
    p = tmp_path / "server.log"
    _write(p, [_OLD_1, _OLD_STDERR, _OLD_2])
    stats = purge_log_older_than(p, days=20, now=NOW)
    assert stats["status"] == "purged"
    assert stats["removed_lines"] == 3
    assert stats["kept_lines"] == 0
    assert _read(p) == ""


def test_undatable_left_untouched(tmp_path):
    p = tmp_path / "server.log"
    content = ["INFO:mod:line one\n", "INFO:mod:line two\n"]
    _write(p, content)
    stats = purge_log_older_than(p, days=20, now=NOW)
    assert stats["status"] == "undatable"
    assert _read(p) == "".join(content)


def test_boundary_exactly_at_cutoff_is_kept(tmp_path):
    p = tmp_path / "server.log"
    at_cutoff = "2026-06-15 12:00:00 INFO x: exactly at cutoff\n"
    just_before = "2026-06-15 11:59:59 INFO x: one second too old\n"
    _write(p, [just_before, at_cutoff])
    stats = purge_log_older_than(p, days=20, now=NOW)
    assert stats["status"] == "purged"
    assert stats["removed_lines"] == 1
    assert _read(p) == at_cutoff


def test_missing_file(tmp_path):
    stats = purge_log_older_than(tmp_path / "nope.log", days=20, now=NOW)
    assert stats["status"] == "missing"


def test_empty_file(tmp_path):
    p = tmp_path / "server.log"
    _write(p, [])
    stats = purge_log_older_than(p, days=20, now=NOW)
    assert stats["status"] == "missing"


def test_disabled_when_days_nonpositive(tmp_path):
    p = tmp_path / "server.log"
    content = [_OLD_1, _RECENT_1]
    _write(p, content)
    stats = purge_log_older_than(p, days=0, now=NOW)
    assert stats["status"] == "disabled"
    assert _read(p) == "".join(content)  # untouched


def test_crlf_endings_preserved(tmp_path):
    p = tmp_path / "server.log"
    old = "2026-06-01 10:00:00 INFO x: old\r\n"
    recent = "2026-07-04 08:00:00 INFO x: recent\r\n"
    with open(p, "w", encoding="utf-8", newline="") as f:
        f.write(old + recent)
    stats = purge_log_older_than(p, days=20, now=NOW)
    assert stats["status"] == "purged"
    with open(p, "rb") as f:
        raw = f.read()
    assert raw == recent.encode("utf-8")  # CRLF intact, old block gone


def test_iso_t_separator_timestamp(tmp_path):
    # Some handlers emit 'YYYY-MM-DDTHH:MM:SS' — must parse too.
    p = tmp_path / "server.log"
    old = "2026-06-01T10:00:00 INFO x: old\n"
    recent = "2026-07-04T08:00:00 INFO x: recent\n"
    _write(p, [old, recent])
    stats = purge_log_older_than(p, days=20, now=NOW)
    assert stats["status"] == "purged"
    assert _read(p) == recent


def test_watchdog_recent_line_survives_when_handler_lines_all_old(tmp_path):
    # Critical Windows case: every RotatingFileHandler line is old, but a recent
    # "[watchdog ...]" line (timestamp not at line start) must be recognized as recent
    # and NOT truncated away.
    p = tmp_path / "server.log"
    watchdog_recent = "[watchdog 2026-07-04 08:00:00] Health check OK\n"
    _write(p, [_OLD_1, _OLD_2, watchdog_recent])
    stats = purge_log_older_than(p, days=20, now=NOW)
    assert stats["status"] == "purged"
    assert stats["removed_lines"] == 2
    assert _read(p) == watchdog_recent


def test_watchdog_only_log_is_datable(tmp_path):
    p = tmp_path / "server.log"
    old = "[watchdog 2026-06-01 10:00:00] Starting subprocess\n"
    recent = "[watchdog 2026-07-04 08:00:00] Health check OK\n"
    _write(p, [old, recent])
    stats = purge_log_older_than(p, days=20, now=NOW)
    assert stats["status"] == "purged"  # not 'undatable'
    assert stats["removed_lines"] == 1
    assert _read(p) == recent


def test_verbatim_bytes_preserved_for_legacy_mixed_encoding(tmp_path):
    # Pre-1.27 mixed-encoding log: the kept tail carries raw non-UTF-8 (cp1251) bytes that
    # must survive verbatim, NOT be replaced with U+FFFD (0xEF 0xBF 0xBD).
    p = tmp_path / "server.log"
    old = b"2026-06-01 10:00:00 INFO x: old\n"
    recent = b"2026-07-04 08:00:00 INFO x: cp1251 \xe0\xe1\xe2\n"  # raw bytes, invalid UTF-8
    with open(p, "wb") as f:
        f.write(old + recent)
    stats = purge_log_older_than(p, days=20, now=NOW)
    assert stats["status"] == "purged"
    with open(p, "rb") as f:
        raw = f.read()
    assert raw == recent  # exact bytes preserved
    assert b"\xef\xbf\xbd" not in raw  # no replacement char introduced


def test_log_retention_days_env(monkeypatch):
    monkeypatch.delenv("RLM_LOG_RETENTION_DAYS", raising=False)
    assert log_retention_days() == DEFAULT_RETENTION_DAYS == 20
    monkeypatch.setenv("RLM_LOG_RETENTION_DAYS", "7")
    assert log_retention_days() == 7
    monkeypatch.setenv("RLM_LOG_RETENTION_DAYS", "0")
    assert log_retention_days() == 0  # disables purge
    monkeypatch.setenv("RLM_LOG_RETENTION_DAYS", "  ")
    assert log_retention_days() == 20  # blank → default
    monkeypatch.setenv("RLM_LOG_RETENTION_DAYS", "abc")
    assert log_retention_days() == 20  # invalid → default
