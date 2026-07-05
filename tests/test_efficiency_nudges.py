"""v1.23.0 — server-side efficiency nudges (session-cumulative, throttled).

Nudges live in the rlm_execute response metadata (never the helper return / stdout)
and target the call-leak classes from the A/B logs: non-batched reads, re-resolves.
"""

from __future__ import annotations

import os
import tempfile

from rlm_tools_bsl.format_detector import detect_format
from rlm_tools_bsl.sandbox import HelperCall, Sandbox


def _txt_sandbox(tmpdir, names=("a", "b", "c")):
    for n in names:
        with open(os.path.join(tmpdir, f"{n}.txt"), "w", encoding="utf-8") as f:
            f.write("CONTENT")
    return Sandbox(base_path=tmpdir)


def _bsl_sandbox(tmpdir):
    obj = os.path.join(tmpdir, "Documents", "Док", "Ext")
    os.makedirs(obj)
    with open(os.path.join(obj, "ObjectModule.bsl"), "w", encoding="utf-8") as f:
        f.write("Процедура П() Экспорт\nКонецПроцедуры\n")
    with open(os.path.join(tmpdir, "Configuration.xml"), "w") as f:
        f.write("<Configuration/>")
    return Sandbox(base_path=tmpdir, format_info=detect_format(tmpdir))


def test_read_file_triggers_read_files_nudge():
    with tempfile.TemporaryDirectory() as tmpdir:
        sb = _txt_sandbox(tmpdir)
        res = sb.execute("read_file('a.txt'); read_file('b.txt'); read_file('c.txt')")
        ids = {h["id"] for h in (res.efficiency_hints or [])}
        assert "read_files" in ids
        h = next(h for h in res.efficiency_hints if h["id"] == "read_files")
        assert h["helper"] == "read_file"
        assert h["count"] >= 3
        assert "read_files([" in h["message"]


def test_batched_read_files_no_nudge():
    """Using the aggregate form (read_files) once → nothing to nudge."""
    with tempfile.TemporaryDirectory() as tmpdir:
        sb = _txt_sandbox(tmpdir)
        res = sb.execute("d = read_files(['a.txt','b.txt','c.txt'])")
        assert not res.efficiency_hints


def test_repeated_find_module_triggers_reuse_var():
    with tempfile.TemporaryDirectory() as tmpdir:
        sb = _bsl_sandbox(tmpdir)
        sb.execute("find_module('Док')")
        res = sb.execute("find_module('Док')")  # same arg fingerprint
        ids = {h["id"] for h in (res.efficiency_hints or [])}
        assert "reuse_var" in ids
        h = next(h for h in res.efficiency_hints if h["id"] == "reuse_var")
        assert h["helper"] == "find_module"


def test_different_find_module_args_do_not_trigger_reuse():
    with tempfile.TemporaryDirectory() as tmpdir:
        sb = _bsl_sandbox(tmpdir)
        sb.execute("find_module('Док')")
        res = sb.execute("find_module('Другое')")  # different fingerprint
        assert not any(h["id"] == "reuse_var" for h in (res.efficiency_hints or []))


def test_nudge_throttled_once_per_session():
    with tempfile.TemporaryDirectory() as tmpdir:
        sb = _bsl_sandbox(tmpdir)
        sb.execute("find_module('Док')")
        r2 = sb.execute("find_module('Док')")
        r3 = sb.execute("find_module('Док')")
        assert any(h["id"] == "reuse_var" for h in (r2.efficiency_hints or []))
        assert not any(h["id"] == "reuse_var" for h in (r3.efficiency_hints or []))


def test_aggregator_is_instance_local():
    """Two sandboxes never share nudge state (no module singleton leak across sessions)."""
    with tempfile.TemporaryDirectory() as t1, tempfile.TemporaryDirectory() as t2:
        sb1 = _txt_sandbox(t1)
        sb2 = _txt_sandbox(t2)
        sb1.execute("read_file('a.txt'); read_file('b.txt'); read_file('c.txt')")
        # sb2 is fresh — one read, no nudge
        res2 = sb2.execute("read_file('a.txt')")
        assert not res2.efficiency_hints


def test_dense_batch_in_one_execute_no_batch_nudge():
    """v1.24.0 #5 — 20 invocations of one helper in ONE execute is perfect batching;
    the batch nudge (which counts rlm_execute ROUND-TRIPS now, not invocations) must
    NOT fire. Regression: agent 06 did this and still got the hint."""
    with tempfile.TemporaryDirectory() as tmpdir:
        sb = _bsl_sandbox(tmpdir)
        code = "\n".join(f"find_module('Mod{i}')" for i in range(20))
        res = sb.execute(code)
        ids = {h["id"] for h in (res.efficiency_hints or [])}
        assert "batch" not in ids, res.efficiency_hints


def test_few_execute_with_one_dense_execute_no_batch_nudge():
    """v1.24.0 #5 — 4 execute total, one of which packs 8 calls. Only 3 sparse
    round-trips < threshold → no batch nudge (agent 08 case: 4 execute, ideal)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        sb = _bsl_sandbox(tmpdir)
        # one dense execute: 8 calls
        sb.execute("\n".join(f"find_module('Dense{i}')" for i in range(8)))
        # three single-call execute
        last = None
        for i in range(3):
            last = sb.execute(f"find_module('Single{i}')")
        ids = {h["id"] for h in (last.efficiency_hints or [])}
        assert "batch" not in ids, last.efficiency_hints


def test_many_sparse_execute_triggers_batch_with_roundtrip_count():
    """v1.24.0 #5 — 8 separate execute, each a single non-aggregate call → batch
    nudge fires, count reflects ROUND-TRIPS (8), not summed invocations."""
    with tempfile.TemporaryDirectory() as tmpdir:
        sb = _bsl_sandbox(tmpdir)
        last = None
        for i in range(8):
            last = sb.execute(f"find_module('Round{i}')")  # distinct args → no reuse_var
        hints = last.efficiency_hints or []
        batch = [h for h in hints if h["id"] == "batch"]
        assert batch, hints
        assert batch[0]["count"] == 8
        assert "round-trip" in batch[0]["trigger"].lower()


def test_zero_helper_execute_does_not_count_as_sparse():
    """v1.24.0 #5 — execute with NO helper calls (pure Python/print) must not
    increment the sparse counter and must not raise AttributeError."""
    with tempfile.TemporaryDirectory() as tmpdir:
        sb = _bsl_sandbox(tmpdir)
        # 8 pure-python executes — would reach threshold if counted as sparse.
        last = None
        for i in range(8):
            last = sb.execute(f"x = {i}; print(x)")
        ids = {h["id"] for h in (last.efficiency_hints or [])}
        assert "batch" not in ids, last.efficiency_hints


def test_hints_in_metadata_not_stdout_and_return_unchanged():
    with tempfile.TemporaryDirectory() as tmpdir:
        sb = _txt_sandbox(tmpdir)
        res = sb.execute("x = read_file('a.txt'); read_file('b.txt'); read_file('c.txt'); print(x)")
        assert res.efficiency_hints  # present in metadata
        # NOT leaked into stdout
        assert "read_files([" not in res.stdout
        assert "HINT" not in res.stdout
        # helper return value unchanged (x is the file content)
        assert "CONTENT" in res.stdout


# ── v1.27.0 — redundant get_index_info() at session start ────────────────────
# get_index_info() runs only with an index, and the gate only inspects HelperCall.seq,
# so these drive _compute_efficiency_hints via a simulated _helper_calls list.


def test_redundant_get_index_info_hint_fires_at_session_start():
    with tempfile.TemporaryDirectory() as tmpdir:
        sb = Sandbox(base_path=tmpdir)
        sb._helper_calls = [HelperCall("get_index_info", 0.0, seq=1)]
        hints = sb._compute_efficiency_hints()
        ids = {h["id"] for h in hints}
        assert "redundant_get_index_info" in ids
        h = next(h for h in hints if h["id"] == "redundant_get_index_info")
        assert h["helper"] == "get_index_info"
        assert "rlm_start.index" in h["message"]


def test_redundant_get_index_info_hint_fires_as_second_call():
    """seq==2 is still a start signal (called 2nd) → nudge fires."""
    with tempfile.TemporaryDirectory() as tmpdir:
        sb = Sandbox(base_path=tmpdir)
        sb._helper_calls = [
            HelperCall("find_module", 0.0, seq=1),
            HelperCall("get_index_info", 0.0, seq=2),
        ]
        ids = {h["id"] for h in sb._compute_efficiency_hints()}
        assert "redundant_get_index_info" in ids


def test_redundant_get_index_info_hint_absent_mid_session():
    """A high-seq mid-session call (e.g. fetching has_regions) must NOT nudge."""
    with tempfile.TemporaryDirectory() as tmpdir:
        sb = Sandbox(base_path=tmpdir)
        sb._helper_calls = [HelperCall("get_index_info", 0.0, seq=5)]
        ids = {h["id"] for h in sb._compute_efficiency_hints()}
        assert "redundant_get_index_info" not in ids


def test_redundant_get_index_info_hint_throttled_once_per_session():
    with tempfile.TemporaryDirectory() as tmpdir:
        sb = Sandbox(base_path=tmpdir)
        sb._helper_calls = [HelperCall("get_index_info", 0.0, seq=1)]
        first = {h["id"] for h in sb._compute_efficiency_hints()}
        sb._helper_calls = [HelperCall("get_index_info", 0.0, seq=2)]
        second = {h["id"] for h in sb._compute_efficiency_hints()}
        assert "redundant_get_index_info" in first
        assert "redundant_get_index_info" not in second


def test_no_get_index_info_no_hint():
    with tempfile.TemporaryDirectory() as tmpdir:
        sb = Sandbox(base_path=tmpdir)
        sb._helper_calls = [HelperCall("find_module", 0.0, seq=1)]
        ids = {h["id"] for h in sb._compute_efficiency_hints()}
        assert "redundant_get_index_info" not in ids
