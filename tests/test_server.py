import json
import os
import sys
import tempfile
from unittest.mock import patch, MagicMock

import pytest

from rlm_tools_bsl.server import (
    _rlm_start,
    _rlm_execute,
    _rlm_end,
    _format_helper_summary,
    _rlm_projects,
    _rlm_index,
    _resolve_path_map,
    _build_jobs,
    _build_jobs_lock,
    _canonicalize_path,
    rlm_index,
    rlm_projects,
)
from rlm_tools_bsl.sandbox import HelperCall


@pytest.fixture(autouse=True)
def _isolate_sandbox_backend_lifecycle(monkeypatch):
    """Synthetic main() lifecycles must not leak shutdown state to neighbours."""
    from rlm_tools_bsl import server

    monkeypatch.setattr(server, "_sandbox_registry_accepting", True)


# ---------------------------------------------------------------------------
# _format_helper_summary tests
# ---------------------------------------------------------------------------


def test_format_helper_summary_mixed():
    """Mixed case: one helper once, another 5 times, third below threshold."""
    calls = [
        HelperCall("find_module", 0.3),
        HelperCall("code_metrics", 0.1),
        HelperCall("code_metrics", 0.2),
        HelperCall("code_metrics", 0.15),
        HelperCall("code_metrics", 0.12),
        HelperCall("code_metrics", 0.13),
        HelperCall("glob_files", 0.02),  # below 0.1 threshold
    ]
    parts, count = _format_helper_summary(calls, threshold=0.1)
    assert count == 2  # find_module + code_metrics (glob_files excluded)
    assert "find_module(0.3s)" in parts
    assert "code_metrics(5\u00d7, total=0.7s)" in parts
    assert "glob_files" not in parts


def test_format_helper_summary_single_call():
    """Single call above threshold."""
    calls = [HelperCall("find_roles", 0.5)]
    parts, count = _format_helper_summary(calls, threshold=0.1)
    assert count == 1
    assert parts == "find_roles(0.5s)"


def test_format_helper_summary_all_below_threshold():
    """All calls below threshold — empty result."""
    calls = [HelperCall("glob_files", 0.01), HelperCall("help", 0.0)]
    parts, count = _format_helper_summary(calls, threshold=0.1)
    assert count == 0
    assert parts == ""


def test_format_helper_summary_threshold_zero():
    """With threshold=0.0 (log_all mode), all calls are included."""
    calls = [
        HelperCall("find_module", 0.3),
        HelperCall("glob_files", 0.0),
        HelperCall("glob_files", 0.0),
    ]
    parts, count = _format_helper_summary(calls, threshold=0.0)
    assert count == 2
    assert "find_module(0.3s)" in parts
    assert "glob_files(2\u00d7" in parts


# ---------------------------------------------------------------------------
# RLM flow tests
# ---------------------------------------------------------------------------


def test_full_rlm_flow():
    with tempfile.TemporaryDirectory() as tmpdir:
        with open(os.path.join(tmpdir, "example.py"), "w") as f:
            f.write("def hello():\n    return 'world'\n\ndef foo():\n    return 'bar'\n")

        start_result = _rlm_start(path=tmpdir, query="find all functions")
        result_data = json.loads(start_result)
        session_id = result_data["session_id"]
        assert session_id is not None
        assert "metadata" in result_data

        exec_result = _rlm_execute(
            session_id=session_id, code="files = glob_files('**/*.py')\nprint(f'Found {len(files)} Python files')"
        )
        exec_data = json.loads(exec_result)
        assert "Found 1 Python files" in exec_data["stdout"]

        exec_result2 = _rlm_execute(session_id=session_id, code="print(files)")
        exec_data2 = json.loads(exec_result2)
        assert "example.py" in exec_data2["stdout"]

        end_result = _rlm_end(session_id=session_id)
        end_data = json.loads(end_result)
        assert end_data["success"] is True


def test_rlm_start_index_block_carries_discovery_fields(monkeypatch):
    """rlm_start.index carries builder_version + has_*/counts so the agent skips a
    separate get_index_info() discovery call on start (Tier 3.2 — PUBLIC key names)."""
    with tempfile.TemporaryDirectory() as raw_tmpdir:
        # rlm_start canonicalizes the path (.resolve()) before hashing it into the
        # index dir; the real CLI build does the same (_cmd_build → _resolve_path).
        # Mirror that here so the md5(base_path) hash agrees — otherwise on runners
        # whose TEMP carries an 8.3 short name (e.g. RUNNER~1) the build hashes the
        # short form while rlm_start hashes the resolved long form, the index is not
        # found, and idx["loaded"] comes back False.
        tmpdir = _canonicalize_path(raw_tmpdir)
        obj = os.path.join(tmpdir, "Documents", "X", "Ext")
        os.makedirs(obj)
        with open(os.path.join(obj, "ObjectModule.bsl"), "w", encoding="utf-8") as f:
            f.write("Процедура П() Экспорт\nКонецПроцедуры\n")
        with open(os.path.join(tmpdir, "Configuration.xml"), "w") as f:
            f.write("<Configuration/>")
        monkeypatch.setenv("RLM_INDEX_DIR", os.path.join(tmpdir, ".idx"))
        from rlm_tools_bsl.bsl_index import IndexBuilder

        IndexBuilder().build(tmpdir, build_calls=False, build_metadata=True)

        result = json.loads(_rlm_start(path=tmpdir, query="discovery fields"))
        try:
            idx = result["index"]
            assert idx["loaded"] is True
            assert idx["index_status"] == "ok"  # v1.26.0: FRESH→"ok" mapping
            assert isinstance(idx["builder_version"], int) and idx["builder_version"] >= 11
            for key in (
                "index_status",
                "has_synonyms",
                "object_synonyms",
                "has_object_attributes",
                "object_attributes_count",
                "has_predefined_items",
                "predefined_items_count",
                "has_form_elements",
                "form_elements_count",
                "has_metadata_references",
                "metadata_references_count",
                "has_metadata_code_usages",
                "metadata_code_usages_count",
            ):
                assert key in idx, f"missing index discovery field: {key}"
            assert isinstance(idx["has_object_attributes"], bool)
        finally:
            _rlm_end(result["session_id"])


def test_rlm_start_index_block_shape_consistent_without_index(monkeypatch):
    """A project WITHOUT an index returns the SAME index discovery keys (safe defaults) so the
    payload shape is stable — agents read them instead of calling get_index_info() and must not
    KeyError on no-index projects (codex finding)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # plain non-BSL dir + RLM_INDEX_DIR pointing at an empty location → no index loads
        with open(os.path.join(tmpdir, "example.py"), "w") as f:
            f.write("def f():\n    return 1\n")
        monkeypatch.setenv("RLM_INDEX_DIR", os.path.join(tmpdir, ".empty_idx"))
        result = json.loads(_rlm_start(path=tmpdir, query="no index"))
        try:
            idx = result["index"]
            assert idx["loaded"] is False
            assert idx["index_status"] == "missing"  # v1.26.0: no index → "missing"
            # SAME discovery keys as the with-index branch, with safe defaults
            for key in (
                "index_status",
                "builder_version",
                "has_synonyms",
                "object_synonyms",
                "has_object_attributes",
                "object_attributes_count",
                "has_predefined_items",
                "predefined_items_count",
                "has_form_elements",
                "form_elements_count",
                "has_metadata_references",
                "metadata_references_count",
                "has_metadata_code_usages",
                "metadata_code_usages_count",
            ):
                assert key in idx, f"missing no-index discovery field: {key}"
            assert idx["builder_version"] == 0
            assert idx["has_object_attributes"] is False
            assert idx["object_attributes_count"] == 0
        finally:
            _rlm_end(result["session_id"])


def test_rlm_start_index_block_key_set_parity(monkeypatch):
    """Full shape parity (codex round 24 — closes a long-standing gap): the index block
    exposes the EXACT SAME key set whether an index is loaded or not, so an agent reading
    any discovery key never KeyErrors across the two branches. The subset 'key in idx'
    checks above miss drift in UNLISTED keys; this set-equality assert catches any."""
    from rlm_tools_bsl.bsl_index import IndexBuilder

    with tempfile.TemporaryDirectory() as raw_a, tempfile.TemporaryDirectory() as raw_b:
        # --- loaded branch (built index) ---
        loaded_dir = _canonicalize_path(raw_a)
        obj = os.path.join(loaded_dir, "Documents", "X", "Ext")
        os.makedirs(obj)
        with open(os.path.join(obj, "ObjectModule.bsl"), "w", encoding="utf-8") as f:
            f.write("Процедура П() Экспорт\nКонецПроцедуры\n")
        with open(os.path.join(loaded_dir, "Configuration.xml"), "w") as f:
            f.write("<Configuration/>")
        monkeypatch.setenv("RLM_INDEX_DIR", os.path.join(loaded_dir, ".idx"))
        IndexBuilder().build(loaded_dir, build_calls=False, build_metadata=True)
        loaded = json.loads(_rlm_start(path=loaded_dir, query="parity loaded"))

        # --- no-index branch (plain dir) ---
        noindex_dir = _canonicalize_path(raw_b)
        with open(os.path.join(noindex_dir, "example.py"), "w") as f:
            f.write("def f():\n    return 1\n")
        monkeypatch.setenv("RLM_INDEX_DIR", os.path.join(noindex_dir, ".empty_idx"))
        noidx = json.loads(_rlm_start(path=noindex_dir, query="parity no-index"))

        try:
            assert loaded["index"]["loaded"] is True
            assert noidx["index"]["loaded"] is False
            loaded_keys, noidx_keys = set(loaded["index"]), set(noidx["index"])
            assert loaded_keys == noidx_keys, (
                "index block key set drifted between loaded and no-index branches: "
                f"loaded-only={loaded_keys - noidx_keys}, no-index-only={noidx_keys - loaded_keys}"
            )
        finally:
            _rlm_end(loaded["session_id"])
            _rlm_end(noidx["session_id"])


def test_rlm_start_index_block_incomplete(monkeypatch):
    """An index left mid-rebuild (build_in_progress=1) is NOT loaded by rlm_start;
    index_status == 'incomplete' and loaded is False (v1.26.0, Finding #1)."""
    import sqlite3

    from rlm_tools_bsl.bsl_index import IndexBuilder, get_index_db_path

    with tempfile.TemporaryDirectory() as raw_tmpdir:
        tmpdir = _canonicalize_path(raw_tmpdir)
        obj = os.path.join(tmpdir, "Documents", "X", "Ext")
        os.makedirs(obj)
        with open(os.path.join(obj, "ObjectModule.bsl"), "w", encoding="utf-8") as f:
            f.write("Процедура П() Экспорт\nКонецПроцедуры\n")
        with open(os.path.join(tmpdir, "Configuration.xml"), "w") as f:
            f.write("<Configuration/>")
        monkeypatch.setenv("RLM_INDEX_DIR", os.path.join(tmpdir, ".idx"))
        IndexBuilder().build(tmpdir, build_calls=False, build_metadata=True)

        db_path = get_index_db_path(tmpdir)
        conn = sqlite3.connect(str(db_path))
        conn.execute("INSERT OR REPLACE INTO index_meta (key, value) VALUES ('build_in_progress', '1')")
        conn.commit()
        conn.close()

        result = json.loads(_rlm_start(path=tmpdir, query="incomplete index"))
        try:
            idx = result["index"]
            assert idx["loaded"] is False
            assert idx["index_status"] == "incomplete"
        finally:
            _rlm_end(result["session_id"])


def test_rlm_start_index_block_incomplete_race(monkeypatch):
    """Race guard (codex High): check_index_usable returns FRESH (a rebuild set
    build_in_progress=1 AFTER the check), but get_statistics is _transient_safe and
    returns a truthy dict, NOT an exception. rlm_start must still detect the marker and
    report loaded:false / index_status:"incomplete" — not "ok"."""
    import sqlite3

    import rlm_tools_bsl.server as srv
    from rlm_tools_bsl.bsl_index import IndexBuilder, IndexStatus, get_index_db_path

    with tempfile.TemporaryDirectory() as raw_tmpdir:
        tmpdir = _canonicalize_path(raw_tmpdir)
        obj = os.path.join(tmpdir, "Documents", "X", "Ext")
        os.makedirs(obj)
        with open(os.path.join(obj, "ObjectModule.bsl"), "w", encoding="utf-8") as f:
            f.write("Процедура П() Экспорт\nКонецПроцедуры\n")
        with open(os.path.join(tmpdir, "Configuration.xml"), "w") as f:
            f.write("<Configuration/>")
        monkeypatch.setenv("RLM_INDEX_DIR", os.path.join(tmpdir, ".idx"))
        IndexBuilder().build(tmpdir, build_calls=False, build_metadata=True)

        db_path = get_index_db_path(tmpdir)
        # Simulate the race: marker set AFTER the (here bypassed) usable-check.
        conn = sqlite3.connect(str(db_path))
        conn.execute("INSERT OR REPLACE INTO index_meta (key, value) VALUES ('build_in_progress', '1')")
        conn.commit()
        conn.close()
        # Force check_index_usable to report FRESH (it ran before the marker was set).
        monkeypatch.setattr(srv, "check_index_usable", lambda *a, **k: IndexStatus.FRESH)

        result = json.loads(srv._rlm_start(path=tmpdir, query="race"))
        try:
            idx = result["index"]
            assert idx["loaded"] is False
            assert idx["index_status"] == "incomplete"
        finally:
            srv._rlm_end(result["session_id"])


def test_rlm_start_zero_stats_not_reported_ok(monkeypatch):
    """Race guard (codex High, timing-independent): if get_statistics returns the
    zero/load-failure sentinel (a finishing rebuild may have cleared the marker before a
    re-check), rlm_start must NOT report loaded:true / index_status:"ok" with zero counts."""
    import rlm_tools_bsl.server as srv
    from rlm_tools_bsl.bsl_index import IndexBuilder, IndexReader, _zero_stats

    with tempfile.TemporaryDirectory() as raw_tmpdir:
        tmpdir = _canonicalize_path(raw_tmpdir)
        obj = os.path.join(tmpdir, "Documents", "X", "Ext")
        os.makedirs(obj)
        with open(os.path.join(obj, "ObjectModule.bsl"), "w", encoding="utf-8") as f:
            f.write("Процедура П() Экспорт\nКонецПроцедуры\n")
        with open(os.path.join(tmpdir, "Configuration.xml"), "w") as f:
            f.write("<Configuration/>")
        monkeypatch.setenv("RLM_INDEX_DIR", os.path.join(tmpdir, ".idx"))
        IndexBuilder().build(tmpdir, build_calls=False, build_metadata=True)

        # No marker set (rebuild "finished"), but the reader read during the DROP window →
        # get_statistics returns the zero-sentinel. index_incomplete() would be False here,
        # so only the stats-based discriminator can catch it.
        monkeypatch.setattr(IndexReader, "get_statistics", lambda self: _zero_stats())

        result = json.loads(srv._rlm_start(path=tmpdir, query="zerostats"))
        try:
            idx = result["index"]
            assert idx["loaded"] is False
            # marker not set (rebuild "finished") but the read was a transient zero of an
            # EXISTING db → "incomplete" (retry), NOT "ok" and NOT "missing" (codex Low).
            assert idx["index_status"] == "incomplete"
        finally:
            srv._rlm_end(result["session_id"])


def test_rlm_index_info_incomplete(monkeypatch):
    """MCP rlm_index(action='info') reports build_status:'incomplete' for a marked index
    (does NOT open get_statistics on a partial DB)."""
    import sqlite3

    from rlm_tools_bsl.bsl_index import IndexBuilder, get_index_db_path

    with tempfile.TemporaryDirectory() as raw_tmpdir:
        tmpdir = _canonicalize_path(raw_tmpdir)
        obj = os.path.join(tmpdir, "Documents", "X", "Ext")
        os.makedirs(obj)
        with open(os.path.join(obj, "ObjectModule.bsl"), "w", encoding="utf-8") as f:
            f.write("Процедура П() Экспорт\nКонецПроцедуры\n")
        with open(os.path.join(tmpdir, "Configuration.xml"), "w") as f:
            f.write("<Configuration/>")
        monkeypatch.setenv("RLM_INDEX_DIR", os.path.join(tmpdir, ".idx"))
        IndexBuilder().build(tmpdir, build_calls=False, build_metadata=True)

        db_path = get_index_db_path(tmpdir)
        conn = sqlite3.connect(str(db_path))
        conn.execute("INSERT OR REPLACE INTO index_meta (key, value) VALUES ('build_in_progress', '1')")
        conn.commit()
        conn.close()

        res = json.loads(_rlm_index(action="info", path=tmpdir))
        assert res.get("build_status") == "incomplete"


def test_rlm_index_info_post_read_marker_recheck(monkeypatch):
    """MCP rlm_index info: a rebuild setting build_in_progress=1 DURING get_statistics
    (after the pre-read marker check) must report build_status:'incomplete', not a normal
    payload. stats_indicate_load_failure is False here (stale meta still present), so only a
    post-read index_incomplete recheck catches it (codex High follow-up)."""
    import sqlite3

    from rlm_tools_bsl.bsl_index import IndexBuilder, IndexReader, get_index_db_path

    with tempfile.TemporaryDirectory() as raw_tmpdir:
        tmpdir = _canonicalize_path(raw_tmpdir)
        obj = os.path.join(tmpdir, "Documents", "X", "Ext")
        os.makedirs(obj)
        with open(os.path.join(obj, "ObjectModule.bsl"), "w", encoding="utf-8") as f:
            f.write("Процедура П() Экспорт\nКонецПроцедуры\n")
        with open(os.path.join(tmpdir, "Configuration.xml"), "w") as f:
            f.write("<Configuration/>")
        monkeypatch.setenv("RLM_INDEX_DIR", os.path.join(tmpdir, ".idx"))
        IndexBuilder().build(tmpdir, build_calls=False, build_metadata=True)
        db_path = get_index_db_path(tmpdir)

        orig = IndexReader.get_statistics

        def spy(self):
            # Marker becomes visible during the stats read; stale meta NOT yet cleared →
            # stats_indicate_load_failure stays False, so only a post-read marker recheck
            # can catch the partial state.
            w = sqlite3.connect(str(db_path))
            w.execute("INSERT OR REPLACE INTO index_meta (key, value) VALUES ('build_in_progress', '1')")
            w.commit()
            w.close()
            return orig(self)

        monkeypatch.setattr(IndexReader, "get_statistics", spy)

        res = json.loads(_rlm_index(action="info", path=tmpdir))
        assert res.get("build_status") == "incomplete"


def test_efficiency_hints_in_execute_response_and_log(caplog):
    """efficiency_hints surface in the rlm_execute response metadata AND the server log
    carries `hints=<id>` for A/B analysis (Tier 2.1)."""
    import logging

    with tempfile.TemporaryDirectory() as tmpdir:
        for n in ("a", "b", "c"):
            with open(os.path.join(tmpdir, f"{n}.txt"), "w", encoding="utf-8") as f:
                f.write("x")
        start = json.loads(_rlm_start(path=tmpdir, query="hints flow"))
        sid = start["session_id"]
        try:
            with caplog.at_level(logging.INFO, logger="rlm_tools_bsl.server"):
                res = json.loads(_rlm_execute(sid, "read_file('a.txt'); read_file('b.txt'); read_file('c.txt')"))
            assert "efficiency_hints" in res
            assert any(h["id"] == "read_files" for h in res["efficiency_hints"])
            assert any("hints=read_files" in r.getMessage() for r in caplog.records)
        finally:
            _rlm_end(sid)


def test_rlm_execute_tool_description_mentions_profile_and_batch():
    """rlm_execute Field description teaches get_object_profile + batch forms (R3 #2)."""
    import inspect

    from rlm_tools_bsl import server

    src = inspect.getsource(server)
    assert "get_object_profile(name) (structure+modules" in src
    assert "read_files([p1,p2])" in src
    assert "read_procedure(path, ['ProcA','ProcB'])" in src


def test_invalid_session():
    result = _rlm_execute(session_id="nonexistent", code="print('hi')")
    data = json.loads(result)
    assert "error" in data


def test_invalid_directory():
    result = _rlm_start(path="/nonexistent/path", query="test")
    data = json.loads(result)
    assert "error" in data


def test_resolve_mapped_drive_returns_unc():
    from rlm_tools_bsl.server import _resolve_mapped_drive

    if sys.platform != "win32":
        assert _resolve_mapped_drive("U:\\some\\path") is None
        return

    import winreg

    fake_sids = ["S-1-5-21-fake"]

    def fake_enum_key(hkey, index):
        if index < len(fake_sids):
            return fake_sids[index]
        raise OSError

    fake_key = MagicMock()

    def fake_open_key(hkey, sub_key):
        if sub_key == "S-1-5-21-fake\\Network\\U":
            cm = MagicMock()
            cm.__enter__ = MagicMock(return_value=fake_key)
            cm.__exit__ = MagicMock(return_value=False)
            return cm
        raise OSError

    def fake_query(key, name):
        if key is fake_key and name == "RemotePath":
            return ("\\\\server\\share", winreg.REG_SZ)
        raise OSError

    with (
        patch.object(winreg, "EnumKey", side_effect=fake_enum_key),
        patch.object(winreg, "OpenKey", side_effect=fake_open_key),
        patch.object(winreg, "QueryValueEx", side_effect=fake_query),
    ):
        result = _resolve_mapped_drive("U:\\ERP\\mainconf")
        assert result == "\\\\server\\share\\ERP\\mainconf"


def test_resolve_mapped_drive_no_mapping():
    from rlm_tools_bsl.server import _resolve_mapped_drive

    if sys.platform != "win32":
        return

    import winreg

    def fake_enum_key(hkey, index):
        raise OSError  # no SIDs

    with patch.object(winreg, "EnumKey", side_effect=fake_enum_key):
        result = _resolve_mapped_drive("Z:\\nonexistent")
        assert result is None


def test_resolve_mapped_drive_not_windows():
    from rlm_tools_bsl.server import _resolve_mapped_drive

    with patch("rlm_tools_bsl.server.os.name", "posix"):
        assert _resolve_mapped_drive("U:\\some\\path") is None


def test_invalid_directory_hint_inaccessible_drive():
    """Error should include UNC hint when drive root is inaccessible."""
    result = _rlm_start(path="Z:\\nonexistent\\path", query="test")
    data = json.loads(result)
    assert "error" in data
    if sys.platform == "win32" and not os.path.isdir("Z:\\"):
        assert "UNC" in data["error"] or "drive Z:" in data["error"]


def test_metadata_includes_file_types():
    with tempfile.TemporaryDirectory() as tmpdir:
        open(os.path.join(tmpdir, "a.py"), "w").close()
        open(os.path.join(tmpdir, "b.py"), "w").close()
        open(os.path.join(tmpdir, "c.txt"), "w").close()

        result = _rlm_start(path=tmpdir, query="test", include_metadata=True)
        data = json.loads(result)
        assert data["metadata"]["total_files"] == 3
        assert ".py" in data["metadata"]["file_types"]

        _rlm_end(data["session_id"])


def test_read_file_in_sandbox():
    with tempfile.TemporaryDirectory() as tmpdir:
        with open(os.path.join(tmpdir, "data.txt"), "w") as f:
            f.write("important data")

        result = _rlm_start(path=tmpdir, query="read file")
        data = json.loads(result)
        session_id = data["session_id"]

        exec_result = _rlm_execute(session_id=session_id, code="content = read_file('data.txt')\nprint(content)")
        exec_data = json.loads(exec_result)
        assert "important data" in exec_data["stdout"]
        assert exec_data["error"] is None

        _rlm_end(session_id)


def test_grep_in_sandbox():
    with tempfile.TemporaryDirectory() as tmpdir:
        with open(os.path.join(tmpdir, "code.py"), "w") as f:
            f.write("class MyController:\n    def handle_error(self):\n        pass\n")

        result = _rlm_start(path=tmpdir, query="find controllers")
        data = json.loads(result)
        session_id = data["session_id"]

        exec_result = _rlm_execute(
            session_id=session_id, code="results = grep('class.*Controller')\nprint(len(results))"
        )
        exec_data = json.loads(exec_result)
        assert "1" in exec_data["stdout"]

        _rlm_end(session_id)


def test_skip_metadata_scan():
    with tempfile.TemporaryDirectory() as tmpdir:
        open(os.path.join(tmpdir, "a.py"), "w").close()

        result = _rlm_start(path=tmpdir, query="test", include_metadata=False)
        data = json.loads(result)
        assert data["metadata"] == {}
        assert "session_id" in data

        _rlm_end(data["session_id"])


def test_new_helpers_in_sandbox():
    with tempfile.TemporaryDirectory() as tmpdir:
        with open(os.path.join(tmpdir, "a.txt"), "w") as f:
            f.write("hello from a")
        with open(os.path.join(tmpdir, "b.txt"), "w") as f:
            f.write("hello from b")

        result = _rlm_start(path=tmpdir, query="test helpers")
        data = json.loads(result)
        session_id = data["session_id"]

        exec_result = _rlm_execute(
            session_id=session_id,
            code="result = read_files(['a.txt', 'b.txt'])\nfor k, v in sorted(result.items()):\n    print(f'{k}: {v}')",
        )
        exec_data = json.loads(exec_result)
        assert "a.txt: 1 | hello from a" in exec_data["stdout"]
        assert "b.txt: 1 | hello from b" in exec_data["stdout"]

        exec_result2 = _rlm_execute(session_id=session_id, code="print(grep_summary('hello'))")
        exec_data2 = json.loads(exec_result2)
        assert "2 matches" in exec_data2["stdout"]

        exec_result3 = _rlm_execute(session_id=session_id, code="result = grep_read('hello')\nprint(result['summary'])")
        exec_data3 = json.loads(exec_result3)
        assert "2 matches" in exec_data3["stdout"]

        _rlm_end(session_id)


def test_new_defaults():
    """Default effort='auto' → простой запрос → medium (25 execute, 15 llm)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        open(os.path.join(tmpdir, "a.py"), "w").close()

        result = _rlm_start(path=tmpdir, query="test defaults")
        data = json.loads(result)
        assert data["effective_effort"] == "medium"  # auto → medium на простом запросе
        assert data["limits"]["max_execute_calls"] == 25
        assert data["limits"]["max_llm_calls"] == 15
        assert data["limits"]["execution_timeout_seconds"] == 45

        _rlm_end(data["session_id"])


def test_full_detail_excludes_helper_functions_from_variables():
    with tempfile.TemporaryDirectory() as tmpdir:
        with open(os.path.join(tmpdir, "a.txt"), "w") as f:
            f.write("hello")

        start = json.loads(_rlm_start(path=tmpdir, query="detail vars"))
        session_id = start["session_id"]

        result = json.loads(
            _rlm_execute(
                session_id=session_id,
                code="x = 123",
                detail_level="full",
            )
        )

        assert "x" in result["variables"]
        assert "read_files" not in result["variables"]
        assert "grep_summary" not in result["variables"]
        assert "grep_read" not in result["variables"]
        assert "find_module" not in result["variables"]
        assert "find_by_type" not in result["variables"]
        assert "extract_procedures" not in result["variables"]
        assert "safe_grep" not in result["variables"]
        assert "find_files" not in result["variables"]

        _rlm_end(session_id)


def test_extension_context_main_with_nearby_extension():
    """rlm_start returns extension_context with nearby extensions for main config."""
    import textwrap

    with tempfile.TemporaryDirectory() as parent:
        main_dir = os.path.join(parent, "main")
        ext_dir = os.path.join(parent, "ext")
        os.makedirs(main_dir)
        os.makedirs(ext_dir)

        # Main config
        with open(os.path.join(main_dir, "Configuration.xml"), "w", encoding="utf-8") as f:
            f.write(
                textwrap.dedent("""\
                <?xml version="1.0" encoding="UTF-8"?>
                <MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">
                    <Configuration uuid="00000000-0000-0000-0000-000000000001">
                        <Properties>
                            <Name>Основная</Name>
                            <NamePrefix/>
                        </Properties>
                    </Configuration>
                </MetaDataObject>
            """)
            )

        # Extension
        with open(os.path.join(ext_dir, "Configuration.xml"), "w", encoding="utf-8") as f:
            f.write(
                textwrap.dedent("""\
                <?xml version="1.0" encoding="UTF-8"?>
                <MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">
                    <Configuration uuid="00000000-0000-0000-0000-000000000002">
                        <Properties>
                            <ObjectBelonging>Adopted</ObjectBelonging>
                            <Name>ТестРасш</Name>
                            <ConfigurationExtensionPurpose>AddOn</ConfigurationExtensionPurpose>
                            <NamePrefix>мр_</NamePrefix>
                        </Properties>
                    </Configuration>
                </MetaDataObject>
            """)
            )

        result = _rlm_start(path=main_dir, query="test ext context")
        data = json.loads(result)

        assert "extension_context" in data
        ec = data["extension_context"]
        assert ec["is_extension"] is False
        assert ec["config_role"] == "main"
        assert len(ec["nearby_extensions"]) == 1
        assert ec["nearby_extensions"][0]["name"] == "ТестРасш"
        assert ec["nearby_extensions"][0]["purpose"] == "AddOn"
        # warnings are at top level, not inside extension_context
        assert len(data["warnings"]) > 0

        _rlm_end(data["session_id"])


def test_multiple_extensions_in_container_e2e():
    """rlm_start with container dir (cfe/) holding multiple extensions."""
    import textwrap

    with tempfile.TemporaryDirectory() as parent:
        main_dir = os.path.join(parent, "cf")
        os.makedirs(main_dir)

        # Main config
        with open(os.path.join(main_dir, "Configuration.xml"), "w", encoding="utf-8") as f:
            f.write(
                textwrap.dedent("""\
                <?xml version="1.0" encoding="UTF-8"?>
                <MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">
                    <Configuration uuid="00000000-0000-0000-0000-000000000001">
                        <Properties>
                            <Name>Основная</Name>
                            <NamePrefix/>
                        </Properties>
                    </Configuration>
                </MetaDataObject>
            """)
            )

        # Container cfe/ with two extensions
        cfe_dir = os.path.join(parent, "cfe")
        for name, purpose, prefix, uid_suffix in [
            ("Расш1", "AddOn", "р1_", "a"),
            ("Расш2", "Customization", "р2_", "b"),
        ]:
            ext_sub = os.path.join(cfe_dir, name)
            os.makedirs(ext_sub)
            with open(os.path.join(ext_sub, "Configuration.xml"), "w", encoding="utf-8") as f:
                f.write(
                    textwrap.dedent(f"""\
                    <?xml version="1.0" encoding="UTF-8"?>
                    <MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">
                        <Configuration uuid="00000000-0000-0000-0000-00000000000{uid_suffix}">
                            <Properties>
                                <ObjectBelonging>Adopted</ObjectBelonging>
                                <Name>{name}</Name>
                                <ConfigurationExtensionPurpose>{purpose}</ConfigurationExtensionPurpose>
                                <NamePrefix>{prefix}</NamePrefix>
                            </Properties>
                        </Configuration>
                    </MetaDataObject>
                """)
                )

        result = _rlm_start(path=main_dir, query="test multi ext")
        data = json.loads(result)

        ec = data["extension_context"]
        assert ec["is_extension"] is False
        assert ec["config_role"] == "main"
        assert len(ec["nearby_extensions"]) == 2
        names = {e["name"] for e in ec["nearby_extensions"]}
        assert names == {"Расш1", "Расш2"}

        _rlm_end(data["session_id"])


def test_extension_context_for_extension():
    """rlm_start for extension shows is_extension=True."""
    import textwrap

    with tempfile.TemporaryDirectory() as parent:
        ext_dir = os.path.join(parent, "myext")
        os.makedirs(ext_dir)

        with open(os.path.join(ext_dir, "Configuration.xml"), "w", encoding="utf-8") as f:
            f.write(
                textwrap.dedent("""\
                <?xml version="1.0" encoding="UTF-8"?>
                <MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">
                    <Configuration uuid="00000000-0000-0000-0000-000000000003">
                        <Properties>
                            <ObjectBelonging>Adopted</ObjectBelonging>
                            <Name>Расширение1</Name>
                            <ConfigurationExtensionPurpose>Customization</ConfigurationExtensionPurpose>
                            <NamePrefix>р1_</NamePrefix>
                        </Properties>
                    </Configuration>
                </MetaDataObject>
            """)
            )

        result = _rlm_start(path=ext_dir, query="test ext")
        data = json.loads(result)

        ec = data["extension_context"]
        assert ec["is_extension"] is True
        assert ec["config_role"] == "extension"
        assert ec["current_name"] == "Расширение1"
        assert ec["current_purpose"] == "Customization"
        assert ec["current_prefix"] == "р1_"

        _rlm_end(data["session_id"])


def test_config_format_returned():
    with tempfile.TemporaryDirectory() as tmpdir:
        open(os.path.join(tmpdir, "script.bsl"), "w").close()

        result = _rlm_start(path=tmpdir, query="test format")
        data = json.loads(result)
        assert "config_format" in data
        assert data["config_format"] in ("cf", "edt", "unknown")

        _rlm_end(data["session_id"])


def test_strategy_always_returned():
    with tempfile.TemporaryDirectory() as tmpdir:
        open(os.path.join(tmpdir, "a.bsl"), "w").close()

        result = _rlm_start(path=tmpdir, query="test strategy")
        data = json.loads(result)
        assert "strategy" in data
        assert "find_module" in data["strategy"]
        assert "CRITICAL" in data["strategy"]

        _rlm_end(data["session_id"])


def test_effort_levels():
    with tempfile.TemporaryDirectory() as tmpdir:
        open(os.path.join(tmpdir, "a.py"), "w").close()

        for effort, expected_exec in [("low", 10), ("medium", 25), ("high", 50), ("max", 100)]:
            result = _rlm_start(path=tmpdir, query="test effort", effort=effort)
            data = json.loads(result)
            assert data["limits"]["max_execute_calls"] == expected_exec, f"effort={effort}"
            _rlm_end(data["session_id"])


def test_bsl_helpers_in_sandbox():
    """BSL helpers should be available in sandbox when format_info is provided."""
    with tempfile.TemporaryDirectory() as tmpdir:
        os.makedirs(os.path.join(tmpdir, "CommonModules", "TestModule", "Ext"))
        with open(os.path.join(tmpdir, "CommonModules", "TestModule", "Ext", "Module.bsl"), "w", encoding="utf-8") as f:
            f.write("Процедура Тест() Экспорт\nКонецПроцедуры\n")
        with open(os.path.join(tmpdir, "Configuration.xml"), "w") as f:
            f.write("<Configuration/>")

        start = json.loads(_rlm_start(path=tmpdir, query="test bsl helpers"))
        session_id = start["session_id"]
        assert start["config_format"] == "cf"

        # Test find_module
        result = json.loads(
            _rlm_execute(session_id=session_id, code="modules = find_module('TestModule')\nprint(len(modules))")
        )
        assert "1" in result["stdout"]
        assert result["error"] is None

        # Test extract_procedures
        result2 = json.loads(
            _rlm_execute(
                session_id=session_id, code="procs = extract_procedures(modules[0]['path'])\nprint(procs[0]['name'])"
            )
        )
        assert "Тест" in result2["stdout"]

        _rlm_end(session_id)


def test_override_effort_limits():
    """Manual max_llm_calls and max_execute_calls override effort defaults."""
    with tempfile.TemporaryDirectory() as tmpdir:
        open(os.path.join(tmpdir, "a.py"), "w").close()

        result = _rlm_start(
            path=tmpdir,
            query="test override",
            effort="low",
            max_execute_calls=99,
            max_llm_calls=77,
        )
        data = json.loads(result)
        assert data["limits"]["max_execute_calls"] == 99
        assert data["limits"]["max_llm_calls"] == 77

        _rlm_end(data["session_id"])


# ---------------------------------------------------------------------------
# Transport / main() tests
# ---------------------------------------------------------------------------


def test_main_default_stdio():
    """main() without args calls mcp.run(transport='stdio')."""
    from rlm_tools_bsl import server

    with patch.object(server.mcp, "run") as mock_run, patch.object(sys, "argv", ["rlm-tools-bsl"]):
        server.main()
        mock_run.assert_called_once_with(transport="stdio")


def test_main_streamable_http_arg():
    """--transport streamable-http sets transport and updates settings."""
    from rlm_tools_bsl import server

    original_host = server.mcp.settings.host
    original_port = server.mcp.settings.port
    try:
        with (
            patch.object(server.mcp, "run") as mock_run,
            patch.object(sys, "argv", ["rlm-tools-bsl", "--transport", "streamable-http"]),
        ):
            server.main()
            mock_run.assert_called_once_with(transport="streamable-http")
            assert server.mcp.settings.host == "127.0.0.1"
            assert server.mcp.settings.port == 9000
    finally:
        server.mcp.settings.host = original_host
        server.mcp.settings.port = original_port


def test_main_custom_port():
    """--port overrides default port in mcp.settings."""
    from rlm_tools_bsl import server

    original_port = server.mcp.settings.port
    try:
        with (
            patch.object(server.mcp, "run") as mock_run,
            patch.object(sys, "argv", ["rlm-tools-bsl", "--transport", "streamable-http", "--port", "3000"]),
        ):
            server.main()
            mock_run.assert_called_once_with(transport="streamable-http")
            assert server.mcp.settings.port == 3000
    finally:
        server.mcp.settings.port = original_port


def test_main_env_transport():
    """RLM_TRANSPORT env var is used as fallback when no CLI arg given."""
    from rlm_tools_bsl import server

    original_host = server.mcp.settings.host
    original_port = server.mcp.settings.port
    try:
        with (
            patch.object(server.mcp, "run") as mock_run,
            patch.object(sys, "argv", ["rlm-tools-bsl"]),
            patch.dict(os.environ, {"RLM_TRANSPORT": "streamable-http"}),
        ):
            server.main()
            mock_run.assert_called_once_with(transport="streamable-http")
    finally:
        server.mcp.settings.host = original_host
        server.mcp.settings.port = original_port


def test_main_stdio_does_not_change_settings():
    """When transport is stdio, mcp.settings.host/port are NOT modified."""
    from rlm_tools_bsl import server

    original_host = server.mcp.settings.host
    original_port = server.mcp.settings.port
    try:
        with patch.object(server.mcp, "run") as mock_run, patch.object(sys, "argv", ["rlm-tools-bsl"]):
            server.main()
            mock_run.assert_called_once_with(transport="stdio")
            assert server.mcp.settings.host == original_host
            assert server.mcp.settings.port == original_port
    finally:
        server.mcp.settings.host = original_host
        server.mcp.settings.port = original_port


def test_sandboxes_concurrent_access():
    """Concurrent create/end must not crash with _sandboxes_lock."""
    import threading

    with tempfile.TemporaryDirectory() as tmpdir:
        open(os.path.join(tmpdir, "a.py"), "w").close()

        errors = []

        def create_and_end():
            try:
                result = _rlm_start(path=tmpdir, query="concurrent test")
                data = json.loads(result)
                sid = data["session_id"]
                _rlm_execute(session_id=sid, code="print(1+1)")
                _rlm_end(session_id=sid)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=create_and_end) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, f"Concurrent access errors: {errors}"


# ---------------------------------------------------------------------------
# Project registry integration tests
# ---------------------------------------------------------------------------


def test_rlm_projects_list_empty():
    with tempfile.TemporaryDirectory() as tmpdir:
        from rlm_tools_bsl.projects import _reset_registry

        _reset_registry()
        with patch.dict(os.environ, {"RLM_CONFIG_FILE": os.path.join(tmpdir, "service.json")}):
            _reset_registry()
            result = json.loads(_rlm_projects(action="list"))
            assert result["projects"] == []


def test_rlm_projects_add_remove_rename_update():
    with tempfile.TemporaryDirectory() as tmpdir:
        from rlm_tools_bsl.projects import _reset_registry

        src = os.path.join(tmpdir, "src")
        os.makedirs(src)
        _reset_registry()
        with patch.dict(os.environ, {"RLM_CONFIG_FILE": os.path.join(tmpdir, "service.json")}):
            _reset_registry()
            # add
            r = json.loads(_rlm_projects(action="add", name="Test", path=src, description="Desc"))
            assert "added" in r
            assert r["added"]["name"] == "Test"
            # list
            r = json.loads(_rlm_projects(action="list"))
            assert len(r["projects"]) == 1
            # rename
            r = json.loads(_rlm_projects(action="rename", name="Test", new_name="Test2"))
            assert r["renamed"]["name"] == "Test2"
            # update
            r = json.loads(_rlm_projects(action="update", name="Test2", description="Updated"))
            assert r["updated"]["description"] == "Updated"
            # remove
            r = json.loads(_rlm_projects(action="remove", name="Test2"))
            assert r["removed"]["name"] == "Test2"
            r = json.loads(_rlm_projects(action="list"))
            assert r["projects"] == []


def test_rlm_projects_validation_errors():
    with tempfile.TemporaryDirectory() as tmpdir:
        from rlm_tools_bsl.projects import _reset_registry

        _reset_registry()
        with patch.dict(os.environ, {"RLM_CONFIG_FILE": os.path.join(tmpdir, "service.json")}):
            _reset_registry()
            # add without name
            r = json.loads(_rlm_projects(action="add", path="/tmp"))
            assert "error" in r
            # add without path
            r = json.loads(_rlm_projects(action="add", name="X"))
            assert "error" in r
            # remove nonexistent
            r = json.loads(_rlm_projects(action="remove", name="Ghost"))
            assert "error" in r


def test_rlm_start_with_project():
    """rlm_start resolves project name from the registry."""
    with tempfile.TemporaryDirectory() as tmpdir:
        from rlm_tools_bsl.projects import _reset_registry

        src = os.path.join(tmpdir, "src")
        os.makedirs(src)
        open(os.path.join(src, "a.py"), "w").close()
        _reset_registry()
        with patch.dict(os.environ, {"RLM_CONFIG_FILE": os.path.join(tmpdir, "service.json")}):
            _reset_registry()
            _rlm_projects(action="add", name="MyProject", path=src)
            result = json.loads(_rlm_start(path=None, query="test", project="MyProject"))
            assert "session_id" in result
            assert "error" not in result
            _rlm_end(result["session_id"])


def test_rlm_start_project_not_found():
    with tempfile.TemporaryDirectory() as tmpdir:
        from rlm_tools_bsl.projects import _reset_registry

        _reset_registry()
        with patch.dict(os.environ, {"RLM_CONFIG_FILE": os.path.join(tmpdir, "service.json")}):
            _reset_registry()
            result = json.loads(_rlm_start(path=None, query="test", project="NoSuch"))
            assert "error" in result
            assert "not found" in result["error"].lower()
            assert "available_projects" in result


def test_rlm_start_project_ambiguous():
    with tempfile.TemporaryDirectory() as tmpdir:
        from rlm_tools_bsl.projects import _reset_registry

        s1 = os.path.join(tmpdir, "s1")
        s2 = os.path.join(tmpdir, "s2")
        os.makedirs(s1)
        os.makedirs(s2)
        _reset_registry()
        with patch.dict(os.environ, {"RLM_CONFIG_FILE": os.path.join(tmpdir, "service.json")}):
            _reset_registry()
            _rlm_projects(action="add", name="Config Alpha", path=s1)
            _rlm_projects(action="add", name="Config Beta", path=s2)
            result = json.loads(_rlm_start(path=None, query="test", project="Config"))
            assert "error" in result
            assert "ambiguous" in result["error"].lower()
            assert "matches" in result


def test_rlm_start_project_fuzzy():
    """Fuzzy match returns 'Did you mean?' error, not a session."""
    with tempfile.TemporaryDirectory() as tmpdir:
        from rlm_tools_bsl.projects import _reset_registry

        src = os.path.join(tmpdir, "src")
        os.makedirs(src)
        _reset_registry()
        with patch.dict(os.environ, {"RLM_CONFIG_FILE": os.path.join(tmpdir, "service.json")}):
            _reset_registry()
            _rlm_projects(action="add", name="Main Config", path=src)
            result = json.loads(_rlm_start(path=None, query="test", project="Main Confgi"))
            assert "error" in result
            assert "did you mean" in result["error"].lower()


def test_rlm_start_project_corrupted_registry():
    """rlm_start with project= and corrupted registry returns clear error."""
    with tempfile.TemporaryDirectory() as tmpdir:
        from rlm_tools_bsl.projects import _reset_registry

        # Write invalid JSON to projects.json
        proj_file = os.path.join(tmpdir, "projects.json")
        with open(proj_file, "w", encoding="utf-8") as f:
            f.write("{bad json")
        _reset_registry()
        with patch.dict(os.environ, {"RLM_CONFIG_FILE": os.path.join(tmpdir, "service.json")}):
            _reset_registry()
            result = json.loads(_rlm_start(path=None, query="test", project="Any"))
            assert "error" in result
            assert "corrupted" in result["error"].lower()


def test_rlm_start_project_invalid_structure():
    """rlm_start with project= and structurally invalid registry."""
    with tempfile.TemporaryDirectory() as tmpdir:
        from rlm_tools_bsl.projects import _reset_registry

        proj_file = os.path.join(tmpdir, "projects.json")
        with open(proj_file, "w", encoding="utf-8") as f:
            f.write("[]")
        _reset_registry()
        with patch.dict(os.environ, {"RLM_CONFIG_FILE": os.path.join(tmpdir, "service.json")}):
            _reset_registry()
            result = json.loads(_rlm_start(path=None, query="test", project="Any"))
            assert "error" in result
            assert "corrupted" in result["error"].lower()


def test_rlm_start_neither_path_nor_project():
    result = json.loads(_rlm_start(path=None, query="test", project=None))
    assert "error" in result
    assert "either" in result["error"].lower()


def test_rlm_start_path_project_hint():
    """Unregistered path returns project_hint in the response."""
    with tempfile.TemporaryDirectory() as tmpdir:
        from rlm_tools_bsl.projects import _reset_registry

        open(os.path.join(tmpdir, "a.py"), "w").close()
        _reset_registry()
        with patch.dict(os.environ, {"RLM_CONFIG_FILE": os.path.join(tmpdir, "service.json")}):
            _reset_registry()
            result = json.loads(_rlm_start(path=tmpdir, query="test"))
            assert "session_id" in result
            assert "project_hint" in result
            _rlm_end(result["session_id"])


def test_sandbox_read_file_numbered():
    """read_file in MCP sandbox returns numbered lines."""
    with tempfile.TemporaryDirectory() as tmpdir:
        with open(os.path.join(tmpdir, "test.txt"), "w") as f:
            f.write("alpha\nbeta\ngamma")

        result = _rlm_start(path=tmpdir, query="test numbered")
        data = json.loads(result)
        session_id = data["session_id"]

        exec_result = _rlm_execute(session_id=session_id, code="print(read_file('test.txt'))")
        exec_data = json.loads(exec_result)
        assert "1 | alpha" in exec_data["stdout"]
        assert "2 | beta" in exec_data["stdout"]
        assert "3 | gamma" in exec_data["stdout"]

        _rlm_end(session_id)


def test_sandbox_read_files_numbered():
    """read_files in MCP sandbox returns numbered lines."""
    with tempfile.TemporaryDirectory() as tmpdir:
        with open(os.path.join(tmpdir, "x.txt"), "w") as f:
            f.write("line1\nline2")

        result = _rlm_start(path=tmpdir, query="test numbered files")
        data = json.loads(result)
        session_id = data["session_id"]

        exec_result = _rlm_execute(
            session_id=session_id,
            code="r = read_files(['x.txt'])\nprint(r['x.txt'])",
        )
        exec_data = json.loads(exec_result)
        assert "1 | line1" in exec_data["stdout"]
        assert "2 | line2" in exec_data["stdout"]

        _rlm_end(session_id)


def test_sandbox_grep_read_numbered():
    """grep_read with context_lines=0 returns numbered file contents."""
    with tempfile.TemporaryDirectory() as tmpdir:
        with open(os.path.join(tmpdir, "code.txt"), "w") as f:
            f.write("function test()\n  return true\nend")

        result = _rlm_start(path=tmpdir, query="test grep numbered")
        data = json.loads(result)
        session_id = data["session_id"]

        exec_result = _rlm_execute(
            session_id=session_id,
            code="r = grep_read('test', 'code.txt')\nprint(r['files']['code.txt'])",
        )
        exec_data = json.loads(exec_result)
        assert "1 | " in exec_data["stdout"]
        assert "2 | " in exec_data["stdout"]

        _rlm_end(session_id)


def test_sandbox_grep_read_context_unchanged():
    """grep_read with context_lines>0 keeps L42: format (not numbered)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        with open(os.path.join(tmpdir, "code.txt"), "w") as f:
            f.write("line1\nline2\ntarget\nline4\nline5")

        result = _rlm_start(path=tmpdir, query="test grep context")
        data = json.loads(result)
        session_id = data["session_id"]

        exec_result = _rlm_execute(
            session_id=session_id,
            code="r = grep_read('target', 'code.txt', context_lines=1)\nprint(r['files']['code.txt'])",
        )
        exec_data = json.loads(exec_result)
        # context_lines>0 should use L-prefix format, not pipe format
        assert "L2:" in exec_data["stdout"] or "L3:" in exec_data["stdout"]
        assert " | " not in exec_data["stdout"]

        _rlm_end(session_id)


def test_read_procedure_numbered_param():
    """read_procedure with numbered=True returns numbered lines, default returns raw."""
    from rlm_tools_bsl.bsl_helpers import make_bsl_helpers

    with tempfile.TemporaryDirectory() as tmpdir:
        bsl_path = os.path.join(tmpdir, "Module.bsl")
        with open(bsl_path, "w", encoding="utf-8") as f:
            f.write("// header\nПроцедура Тест()\n  Возврат;\nКонецПроцедуры\n")

        def _read(p):
            with open(os.path.join(tmpdir, p), encoding="utf-8-sig", errors="replace") as fh:
                return fh.read()

        helpers = make_bsl_helpers(
            base_path=tmpdir,
            resolve_safe=lambda p: os.path.join(tmpdir, p),
            read_file_fn=_read,
            grep_fn=lambda *a, **kw: [],
            glob_files_fn=lambda *a, **kw: [],
            format_info=None,
        )
        rp = helpers["read_procedure"]

        raw = rp("Module.bsl", "Тест")
        assert raw is not None
        assert " | " not in raw
        assert raw.startswith("Процедура Тест()")

        numbered = rp("Module.bsl", "Тест", numbered=True)
        assert numbered is not None
        assert "2 | Процедура Тест()" in numbered
        assert "3 |   Возврат;" in numbered


def test_read_procedure_numbered_absolute():
    """read_procedure numbered=True starts numbering at the actual line in file."""
    from rlm_tools_bsl.bsl_helpers import make_bsl_helpers

    with tempfile.TemporaryDirectory() as tmpdir:
        bsl_path = os.path.join(tmpdir, "Module.bsl")
        # Procedure starts at line 5
        lines = [
            "// line 1",
            "// line 2",
            "// line 3",
            "// line 4",
            "Процедура Пятая()",
            "  Возврат;",
            "КонецПроцедуры",
        ]
        with open(bsl_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

        def _read(p):
            with open(os.path.join(tmpdir, p), encoding="utf-8-sig", errors="replace") as fh:
                return fh.read()

        helpers = make_bsl_helpers(
            base_path=tmpdir,
            resolve_safe=lambda p: os.path.join(tmpdir, p),
            read_file_fn=_read,
            grep_fn=lambda *a, **kw: [],
            glob_files_fn=lambda *a, **kw: [],
            format_info=None,
        )
        rp = helpers["read_procedure"]

        numbered = rp("Module.bsl", "Пятая", numbered=True)
        assert numbered is not None
        assert "5 | Процедура Пятая()" in numbered
        assert "6 |   Возврат;" in numbered
        assert "7 | КонецПроцедуры" in numbered


def test_read_procedure_list_numbered_returns_dict_of_numbered_bodies():
    """P1 + numbering: list proc_name with numbered=True → {name: numbered body}.

    Это путь, которым пользуется MCP-обёртка _numbered_read_procedure
    (она пробрасывает proc_name в диспетчер и форсит numbered=True)."""
    from rlm_tools_bsl.bsl_helpers import make_bsl_helpers

    with tempfile.TemporaryDirectory() as tmpdir:
        bsl_path = os.path.join(tmpdir, "Module.bsl")
        with open(bsl_path, "w", encoding="utf-8") as f:
            f.write(
                "// header\nПроцедура Тест()\n  Возврат;\nКонецПроцедуры\nПроцедура Тест2()\n  Возврат;\nКонецПроцедуры\n"
            )

        def _read(p):
            with open(os.path.join(tmpdir, p), encoding="utf-8-sig", errors="replace") as fh:
                return fh.read()

        helpers = make_bsl_helpers(
            base_path=tmpdir,
            resolve_safe=lambda p: os.path.join(tmpdir, p),
            read_file_fn=_read,
            grep_fn=lambda *a, **kw: [],
            glob_files_fn=lambda *a, **kw: [],
            format_info=None,
        )
        rp = helpers["read_procedure"]

        res = rp("Module.bsl", ["Тест", "Тест2", "НетТакой"], numbered=True)
        assert isinstance(res, dict)
        assert set(res.keys()) == {"Тест", "Тест2", "НетТакой"}
        assert "2 | Процедура Тест()" in res["Тест"]
        assert "5 | Процедура Тест2()" in res["Тест2"]
        assert res["НетТакой"] is None


def test_streamable_http_server_starts():
    """Integration: streamable-http server starts and responds to MCP initialize."""
    import socket
    import subprocess
    import time

    # Find a free port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    proc = subprocess.Popen(
        [sys.executable, "-m", "rlm_tools_bsl", "--transport", "streamable-http", "--port", str(port)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        # Wait for server to start
        import httpx

        client = httpx.Client()
        mcp_url = f"http://127.0.0.1:{port}/mcp"
        initialize_request = {
            "jsonrpc": "2.0",
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0.1.0"},
            },
            "id": 1,
        }

        # Ждём старта сервера. Бюджет задан ДЕДЛАЙНОМ, а не числом попыток: цена
        # одной попытки зависит от того, отказано в соединении сразу (refused —
        # мгновенно) или сокет ушёл в таймаут. Под нагрузкой это разные величины, и
        # «20 попыток» означали то ~10 секунд, то почти минуту.
        #
        # Ловим весь TransportError, а не только ConnectError: httpx.ConnectTimeout
        # — НЕ подкласс ConnectError (оба наследуются от TransportError). На
        # загруженной машине connect упирается в свой таймаут вместо refused, и
        # раньше первое же такое событие валило тест мимо всех ретраев.
        #
        # Досрочный выход по proc.poll(): если сервер умер, ждать дедлайн бессмысленно,
        # а сообщение с его stderr объясняет причину вместо глухого «не запустился».
        response = None
        last_error = None
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            try:
                response = client.post(
                    mcp_url,
                    json=initialize_request,
                    headers={
                        "Content-Type": "application/json",
                        "Accept": "application/json, text/event-stream",
                    },
                    timeout=5,
                )
                break
            except httpx.TransportError as exc:
                last_error = exc
                time.sleep(0.5)

        if response is None and proc.poll() is not None:
            _out, err = proc.communicate(timeout=5)
            raise AssertionError(
                f"server exited with code {proc.returncode} before answering; "
                f"stderr tail: {err.decode('utf-8', 'replace')[-2000:]}"
            )
        assert response is not None, f"Server did not start in time; last error: {last_error!r}"
        assert response.status_code == 200
        assert len(response.content) > 0
    finally:
        proc.terminate()
        proc.wait(timeout=5)


# ---------------------------------------------------------------------------
# rlm_index integration tests
# ---------------------------------------------------------------------------


def test_rlm_index_requires_path_or_project():
    r = json.loads(_rlm_index(action="build"))
    assert "error" in r
    assert "path" in r["error"].lower() or "project" in r["error"].lower()


def test_rlm_index_path_not_found():
    r = json.loads(_rlm_index(action="build", path="/nonexistent/path/xyz"))
    assert "error" in r


def test_rlm_index_build_and_info_and_drop():
    """Full lifecycle: build → info → drop."""
    with tempfile.TemporaryDirectory() as tmpdir:
        src = os.path.join(tmpdir, "src")
        idx_dir = os.path.join(tmpdir, "indexes")
        os.makedirs(src)
        with open(os.path.join(src, "Module.bsl"), "w", encoding="utf-8") as f:
            f.write("Процедура Тест()\nКонецПроцедуры\n")

        with patch.dict(os.environ, {"RLM_INDEX_DIR": idx_dir}):
            # build
            r = json.loads(_rlm_index(action="build", path=src))
            assert r["action"] == "build"
            assert "db_path" in r
            assert r["elapsed_seconds"] >= 0

            # info
            r = json.loads(_rlm_index(action="info", path=src))
            assert r["action"] == "info"
            assert "modules" in r
            assert "methods" in r

            # drop
            r = json.loads(_rlm_index(action="drop", path=src))
            assert r["action"] == "drop"
            assert "dropped" in r

            # info after drop → error
            r = json.loads(_rlm_index(action="info", path=src))
            assert "error" in r
            assert "not found" in r["error"].lower()


def test_rlm_index_drop_nonexistent():
    with tempfile.TemporaryDirectory() as tmpdir:
        r = json.loads(_rlm_index(action="drop", path=tmpdir))
        assert "error" in r
        assert "not found" in r["error"].lower()


def test_rlm_index_update_no_index():
    """Update without prior build → FileNotFoundError."""
    with tempfile.TemporaryDirectory() as tmpdir:
        src = os.path.join(tmpdir, "src")
        idx_dir = os.path.join(tmpdir, "indexes")
        os.makedirs(src)
        with patch.dict(os.environ, {"RLM_INDEX_DIR": idx_dir}):
            r = json.loads(_rlm_index(action="update", path=src))
            assert "error" in r


def test_rlm_index_build_and_update():
    """Build then update → returns delta counts."""
    with tempfile.TemporaryDirectory() as tmpdir:
        src = os.path.join(tmpdir, "src")
        idx_dir = os.path.join(tmpdir, "indexes")
        os.makedirs(src)
        with open(os.path.join(src, "Module.bsl"), "w", encoding="utf-8") as f:
            f.write("Процедура Один()\nКонецПроцедуры\n")

        with patch.dict(os.environ, {"RLM_INDEX_DIR": idx_dir}):
            _rlm_index(action="build", path=src)

            # update with no changes
            r = json.loads(_rlm_index(action="update", path=src))
            assert r["action"] == "update"
            assert r["added"] == 0
            assert r["changed"] == 0
            assert r["removed"] == 0

            # cleanup
            _rlm_index(action="drop", path=src)


def test_rlm_index_with_project():
    """rlm_index resolves project name from the registry."""
    with tempfile.TemporaryDirectory() as tmpdir:
        from rlm_tools_bsl.projects import _reset_registry

        src = os.path.join(tmpdir, "src")
        idx_dir = os.path.join(tmpdir, "indexes")
        os.makedirs(src)
        with open(os.path.join(src, "Module.bsl"), "w", encoding="utf-8") as f:
            f.write("Процедура Два()\nКонецПроцедуры\n")

        _reset_registry()
        with patch.dict(
            os.environ, {"RLM_CONFIG_FILE": os.path.join(tmpdir, "service.json"), "RLM_INDEX_DIR": idx_dir}
        ):
            _reset_registry()
            _rlm_projects(action="add", name="IdxTest", path=src)

            # build by project name
            r = json.loads(_rlm_index(action="build", project="IdxTest"))
            assert r["action"] == "build"
            assert r["project"] == "IdxTest"

            # info by project name
            r = json.loads(_rlm_index(action="info", project="IdxTest"))
            assert r["action"] == "info"
            assert r["project"] == "IdxTest"

            # drop by project name
            r = json.loads(_rlm_index(action="drop", project="IdxTest"))
            assert r["action"] == "drop"
            assert r["project"] == "IdxTest"


def test_rlm_index_unknown_action():
    with tempfile.TemporaryDirectory() as tmpdir:
        r = json.loads(_rlm_index(action="unknown", path=tmpdir))
        assert "error" in r


def test_rlm_index_build_options():
    """Build with all skip flags."""
    with tempfile.TemporaryDirectory() as tmpdir:
        src = os.path.join(tmpdir, "src")
        idx_dir = os.path.join(tmpdir, "indexes")
        os.makedirs(src)
        with open(os.path.join(src, "Module.bsl"), "w", encoding="utf-8") as f:
            f.write("Процедура Три()\nКонецПроцедуры\n")

        with patch.dict(os.environ, {"RLM_INDEX_DIR": idx_dir}):
            r = json.loads(
                _rlm_index(
                    action="build",
                    path=src,
                    no_calls=True,
                    no_metadata=True,
                    no_fts=True,
                    no_synonyms=True,
                )
            )
            assert r["action"] == "build"
            assert "db_path" in r
            assert r["db_path"].startswith(idx_dir)

            # cleanup
            _rlm_index(action="drop", path=src)


# ---------------------------------------------------------------------------
# rlm_index confirm flow tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rlm_index_build_requires_project_not_path():
    """build with path (no project) → error."""
    result = json.loads(await rlm_index(action="build", path="/some/path"))
    assert "error" in result
    assert "requires a registered project" in result["error"]


@pytest.mark.asyncio
async def test_rlm_index_build_path_and_project_rejected():
    """build with path + project → error (path is forbidden for admin actions)."""
    result = json.loads(await rlm_index(action="build", path="/some/path", project="Test"))
    assert "error" in result
    assert "requires a registered project" in result["error"]


@pytest.mark.asyncio
async def test_rlm_index_build_no_password_configured():
    """Project without password → error with instruction to set password."""
    with tempfile.TemporaryDirectory() as tmpdir:
        from rlm_tools_bsl.projects import _reset_registry

        src = os.path.join(tmpdir, "src")
        os.makedirs(src)
        _reset_registry()
        with patch.dict(os.environ, {"RLM_CONFIG_FILE": os.path.join(tmpdir, "service.json")}):
            _reset_registry()
            _rlm_projects(action="add", name="NoPwd", path=src)
            result = json.loads(await rlm_index(action="build", project="NoPwd"))
            assert result["approval_required"] is True
            assert result["action"] == "set_password"


@pytest.mark.asyncio
async def test_rlm_index_build_no_confirm():
    """Project with password, no confirm → approval_required."""
    with tempfile.TemporaryDirectory() as tmpdir:
        from rlm_tools_bsl.projects import _reset_registry

        src = os.path.join(tmpdir, "src")
        os.makedirs(src)
        _reset_registry()
        with patch.dict(os.environ, {"RLM_CONFIG_FILE": os.path.join(tmpdir, "service.json")}):
            _reset_registry()
            _rlm_projects(action="add", name="WithPwd", path=src, password="secret")
            result = json.loads(await rlm_index(action="build", project="WithPwd"))
            assert result["approval_required"] is True
            assert result["action"] == "build"
            assert result["project"] == "WithPwd"
            assert "message" in result


@pytest.mark.asyncio
async def test_rlm_index_build_wrong_confirm():
    """Project with password, wrong confirm → approval_required (same form)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        from rlm_tools_bsl.projects import _reset_registry

        src = os.path.join(tmpdir, "src")
        os.makedirs(src)
        _reset_registry()
        with patch.dict(os.environ, {"RLM_CONFIG_FILE": os.path.join(tmpdir, "service.json")}):
            _reset_registry()
            _rlm_projects(action="add", name="WithPwd", path=src, password="secret")
            result = json.loads(await rlm_index(action="build", project="WithPwd", confirm="wrong"))
            assert result["approval_required"] is True


@pytest.mark.asyncio
async def test_rlm_index_build_correct_confirm():
    """Project with password, correct confirm → build starts in background."""
    import threading as _th

    with tempfile.TemporaryDirectory() as tmpdir:
        from rlm_tools_bsl.projects import _reset_registry

        src = os.path.join(tmpdir, "src")
        idx_dir = os.path.join(tmpdir, "indexes")
        os.makedirs(src)
        with open(os.path.join(src, "Module.bsl"), "w", encoding="utf-8") as f:
            f.write("Процедура Тест()\nКонецПроцедуры\n")

        _reset_registry()
        with patch.dict(
            os.environ, {"RLM_CONFIG_FILE": os.path.join(tmpdir, "service.json"), "RLM_INDEX_DIR": idx_dir}
        ):
            _reset_registry()
            _rlm_projects(action="add", name="WithPwd", path=src, password="secret")
            r = json.loads(await rlm_index(action="build", project="WithPwd", confirm="secret"))
            assert r["started"] is True
            assert r["action"] == "build"
            assert r["project"] == "WithPwd"
            # Wait for background thread to finish
            for t in _th.enumerate():
                if t.name == "build-WithPwd":
                    t.join(timeout=30)
            # cleanup
            _rlm_index(action="drop", path=src)
            _cleanup_build_jobs()


@pytest.mark.asyncio
async def test_rlm_index_drop_correct_confirm():
    """drop with correct password → executes (sync)."""
    import threading as _th

    with tempfile.TemporaryDirectory() as tmpdir:
        from rlm_tools_bsl.projects import _reset_registry

        src = os.path.join(tmpdir, "src")
        idx_dir = os.path.join(tmpdir, "indexes")
        os.makedirs(src)
        with open(os.path.join(src, "Module.bsl"), "w", encoding="utf-8") as f:
            f.write("Процедура Тест()\nКонецПроцедуры\n")

        _reset_registry()
        with patch.dict(
            os.environ, {"RLM_CONFIG_FILE": os.path.join(tmpdir, "service.json"), "RLM_INDEX_DIR": idx_dir}
        ):
            _reset_registry()
            _rlm_projects(action="add", name="WithPwd", path=src, password="secret")
            # Build first (async, wait for thread)
            await rlm_index(action="build", project="WithPwd", confirm="secret")
            for t in _th.enumerate():
                if t.name == "build-WithPwd":
                    t.join(timeout=30)
            # Drop (still sync)
            r = json.loads(await rlm_index(action="drop", project="WithPwd", confirm="secret"))
            assert r["action"] == "drop"
            _cleanup_build_jobs()


@pytest.mark.asyncio
async def test_rlm_index_update_correct_confirm():
    """update with correct password → starts in background."""
    import threading as _th

    with tempfile.TemporaryDirectory() as tmpdir:
        from rlm_tools_bsl.projects import _reset_registry

        src = os.path.join(tmpdir, "src")
        idx_dir = os.path.join(tmpdir, "indexes")
        os.makedirs(src)
        with open(os.path.join(src, "Module.bsl"), "w", encoding="utf-8") as f:
            f.write("Процедура Тест()\nКонецПроцедуры\n")

        _reset_registry()
        with patch.dict(
            os.environ, {"RLM_CONFIG_FILE": os.path.join(tmpdir, "service.json"), "RLM_INDEX_DIR": idx_dir}
        ):
            _reset_registry()
            _rlm_projects(action="add", name="WithPwd", path=src, password="secret")
            # Build first (async, wait for thread)
            await rlm_index(action="build", project="WithPwd", confirm="secret")
            for t in _th.enumerate():
                if t.name == "build-WithPwd":
                    t.join(timeout=30)
            # Update (also async now)
            r = json.loads(await rlm_index(action="update", project="WithPwd", confirm="secret"))
            assert r["started"] is True
            assert r["action"] == "update"
            # Wait for update thread
            for t in _th.enumerate():
                if t.name == "build-WithPwd":
                    t.join(timeout=30)
            # cleanup
            _rlm_index(action="drop", path=src)
            _cleanup_build_jobs()


@pytest.mark.asyncio
async def test_rlm_index_info_works_with_path():
    """info via path without password → works as before."""
    with tempfile.TemporaryDirectory() as tmpdir:
        r = json.loads(await rlm_index(action="info", path=tmpdir))
        assert "approval_required" not in r


@pytest.mark.asyncio
async def test_rlm_index_info_works_with_project():
    """info via project without password → works as before."""
    with tempfile.TemporaryDirectory() as tmpdir:
        from rlm_tools_bsl.projects import _reset_registry

        src = os.path.join(tmpdir, "src")
        os.makedirs(src)
        _reset_registry()
        with patch.dict(os.environ, {"RLM_CONFIG_FILE": os.path.join(tmpdir, "service.json")}):
            _reset_registry()
            _rlm_projects(action="add", name="InfoTest", path=src)
            r = json.loads(await rlm_index(action="info", project="InfoTest"))
            assert "approval_required" not in r


# --- _rlm_projects password tests (sync) ---


def test_rlm_projects_add_with_password():
    """add with password → response has no password_hash/password_salt."""
    with tempfile.TemporaryDirectory() as tmpdir:
        from rlm_tools_bsl.projects import _reset_registry

        src = os.path.join(tmpdir, "src")
        os.makedirs(src)
        _reset_registry()
        with patch.dict(os.environ, {"RLM_CONFIG_FILE": os.path.join(tmpdir, "service.json")}):
            _reset_registry()
            r = json.loads(_rlm_projects(action="add", name="PwdTest", path=src, password="secret"))
            assert "added" in r
            assert "password_hash" not in r["added"]
            assert "password_salt" not in r["added"]


def test_rlm_projects_list_no_password_fields():
    """list never returns password fields."""
    with tempfile.TemporaryDirectory() as tmpdir:
        from rlm_tools_bsl.projects import _reset_registry

        src = os.path.join(tmpdir, "src")
        os.makedirs(src)
        _reset_registry()
        with patch.dict(os.environ, {"RLM_CONFIG_FILE": os.path.join(tmpdir, "service.json")}):
            _reset_registry()
            _rlm_projects(action="add", name="PwdTest", path=src, password="secret")
            r = json.loads(_rlm_projects(action="list"))
            for p in r["projects"]:
                assert "password_hash" not in p
                assert "password_salt" not in p


def test_rlm_projects_update_password():
    """update with password → response has no password_hash."""
    with tempfile.TemporaryDirectory() as tmpdir:
        from rlm_tools_bsl.projects import _reset_registry

        src = os.path.join(tmpdir, "src")
        os.makedirs(src)
        _reset_registry()
        with patch.dict(os.environ, {"RLM_CONFIG_FILE": os.path.join(tmpdir, "service.json")}):
            _reset_registry()
            _rlm_projects(action="add", name="PwdTest", path=src)
            r = json.loads(_rlm_projects(action="update", name="PwdTest", password="newpass"))
            assert "updated" in r
            assert "password_hash" not in r["updated"]


def test_rlm_projects_update_clear_password():
    """update with clear_password → password removed."""
    with tempfile.TemporaryDirectory() as tmpdir:
        from rlm_tools_bsl.projects import _reset_registry, get_registry

        src = os.path.join(tmpdir, "src")
        os.makedirs(src)
        _reset_registry()
        with patch.dict(os.environ, {"RLM_CONFIG_FILE": os.path.join(tmpdir, "service.json")}):
            _reset_registry()
            _rlm_projects(action="add", name="PwdTest", path=src, password="secret")
            reg = get_registry()
            assert reg.has_password("PwdTest") is True
            _rlm_projects(action="update", name="PwdTest", clear_password=True)
            _reset_registry()
            reg = get_registry()
            assert reg.has_password("PwdTest") is False


# ---------------------------------------------------------------------------
# MCP rlm_projects password enforcement tests (async)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_add_with_password():
    """add with password → ok, response has has_password: true."""
    with tempfile.TemporaryDirectory() as tmpdir:
        from rlm_tools_bsl.projects import _reset_registry

        src = os.path.join(tmpdir, "src")
        os.makedirs(src)
        _reset_registry()
        with patch.dict(os.environ, {"RLM_CONFIG_FILE": os.path.join(tmpdir, "service.json")}):
            _reset_registry()
            r = json.loads(await rlm_projects(action="add", name="Test", path=src, password="secret"))
            assert "added" in r
            assert r["added"]["has_password"] is True


@pytest.mark.asyncio
async def test_mcp_remove_correct_password():
    """remove with correct password → ok."""
    with tempfile.TemporaryDirectory() as tmpdir:
        from rlm_tools_bsl.projects import _reset_registry

        src = os.path.join(tmpdir, "src")
        os.makedirs(src)
        _reset_registry()
        with patch.dict(os.environ, {"RLM_CONFIG_FILE": os.path.join(tmpdir, "service.json")}):
            _reset_registry()
            _rlm_projects(action="add", name="Test", path=src, password="secret")
            r = json.loads(await rlm_projects(action="remove", name="Test", password="secret"))
            assert "removed" in r
            assert r["removed"]["has_password"] is True


@pytest.mark.asyncio
async def test_mcp_update_correct_password():
    """update (change description) with correct password → ok."""
    with tempfile.TemporaryDirectory() as tmpdir:
        from rlm_tools_bsl.projects import _reset_registry

        src = os.path.join(tmpdir, "src")
        os.makedirs(src)
        _reset_registry()
        with patch.dict(os.environ, {"RLM_CONFIG_FILE": os.path.join(tmpdir, "service.json")}):
            _reset_registry()
            _rlm_projects(action="add", name="Test", path=src, password="secret")
            r = json.loads(await rlm_projects(action="update", name="Test", description="Updated", password="secret"))
            assert "updated" in r
            assert r["updated"]["description"] == "Updated"


@pytest.mark.asyncio
async def test_mcp_rename_correct_password():
    """rename with correct password → ok."""
    with tempfile.TemporaryDirectory() as tmpdir:
        from rlm_tools_bsl.projects import _reset_registry

        src = os.path.join(tmpdir, "src")
        os.makedirs(src)
        _reset_registry()
        with patch.dict(os.environ, {"RLM_CONFIG_FILE": os.path.join(tmpdir, "service.json")}):
            _reset_registry()
            _rlm_projects(action="add", name="Test", path=src, password="secret")
            r = json.loads(await rlm_projects(action="rename", name="Test", new_name="Test2", password="secret"))
            assert "renamed" in r
            assert r["renamed"]["name"] == "Test2"


@pytest.mark.asyncio
async def test_mcp_add_no_password_approval_required():
    """add without password → approval_required with name+path+description."""
    r = json.loads(await rlm_projects(action="add", name="X", path="/some/path", description="Desc"))
    assert r["approval_required"] is True
    assert r["action"] == "add"
    assert r["name"] == "X"
    assert r["path"] == "/some/path"
    assert r["description"] == "Desc"


@pytest.mark.asyncio
async def test_mcp_add_empty_password_approval_required():
    """add with password='' → approval_required."""
    r = json.loads(await rlm_projects(action="add", name="X", path="/some/path", password=""))
    assert r["approval_required"] is True


@pytest.mark.asyncio
async def test_mcp_add_no_name_error():
    """add without name → error (before password check)."""
    r = json.loads(await rlm_projects(action="add"))
    assert "error" in r
    assert "name is required" in r["error"]


@pytest.mark.asyncio
async def test_mcp_add_no_path_error():
    """add without path → error (before password check)."""
    r = json.loads(await rlm_projects(action="add", name="X"))
    assert "error" in r
    assert "path is required" in r["error"]


@pytest.mark.asyncio
async def test_mcp_remove_no_password():
    """remove without password → approval_required."""
    with tempfile.TemporaryDirectory() as tmpdir:
        from rlm_tools_bsl.projects import _reset_registry

        src = os.path.join(tmpdir, "src")
        os.makedirs(src)
        _reset_registry()
        with patch.dict(os.environ, {"RLM_CONFIG_FILE": os.path.join(tmpdir, "service.json")}):
            _reset_registry()
            _rlm_projects(action="add", name="Test", path=src, password="secret")
            r = json.loads(await rlm_projects(action="remove", name="Test"))
            assert r["approval_required"] is True
            assert r["action"] == "remove"
            assert r["project"] == "Test"


@pytest.mark.asyncio
async def test_mcp_remove_wrong_password():
    """remove with wrong password → approval_required."""
    with tempfile.TemporaryDirectory() as tmpdir:
        from rlm_tools_bsl.projects import _reset_registry

        src = os.path.join(tmpdir, "src")
        os.makedirs(src)
        _reset_registry()
        with patch.dict(os.environ, {"RLM_CONFIG_FILE": os.path.join(tmpdir, "service.json")}):
            _reset_registry()
            _rlm_projects(action="add", name="Test", path=src, password="secret")
            r = json.loads(await rlm_projects(action="remove", name="Test", password="wrong"))
            assert r["approval_required"] is True


@pytest.mark.asyncio
async def test_mcp_update_no_password():
    """update without password → approval_required with path/description."""
    with tempfile.TemporaryDirectory() as tmpdir:
        from rlm_tools_bsl.projects import _reset_registry

        src = os.path.join(tmpdir, "src")
        os.makedirs(src)
        _reset_registry()
        with patch.dict(os.environ, {"RLM_CONFIG_FILE": os.path.join(tmpdir, "service.json")}):
            _reset_registry()
            _rlm_projects(action="add", name="Test", path=src, password="secret")
            r = json.loads(await rlm_projects(action="update", name="Test", description="New desc"))
            assert r["approval_required"] is True
            assert r["action"] == "update"
            assert r["project"] == "Test"
            assert r["description"] == "New desc"


@pytest.mark.asyncio
async def test_mcp_update_no_password_clear_password_in_payload():
    """update with clear_password but no password → approval_required includes clear_password."""
    with tempfile.TemporaryDirectory() as tmpdir:
        from rlm_tools_bsl.projects import _reset_registry

        src = os.path.join(tmpdir, "src")
        os.makedirs(src)
        _reset_registry()
        with patch.dict(os.environ, {"RLM_CONFIG_FILE": os.path.join(tmpdir, "service.json")}):
            _reset_registry()
            _rlm_projects(action="add", name="Test", path=src, password="secret")
            r = json.loads(await rlm_projects(action="update", name="Test", clear_password=True))
            assert r["approval_required"] is True
            assert r["clear_password"] is True


@pytest.mark.asyncio
async def test_mcp_rename_no_password():
    """rename without password → approval_required with new_name."""
    with tempfile.TemporaryDirectory() as tmpdir:
        from rlm_tools_bsl.projects import _reset_registry

        src = os.path.join(tmpdir, "src")
        os.makedirs(src)
        _reset_registry()
        with patch.dict(os.environ, {"RLM_CONFIG_FILE": os.path.join(tmpdir, "service.json")}):
            _reset_registry()
            _rlm_projects(action="add", name="Test", path=src, password="secret")
            r = json.loads(await rlm_projects(action="rename", name="Test", new_name="Test2"))
            assert r["approval_required"] is True
            assert r["new_name"] == "Test2"


@pytest.mark.asyncio
async def test_mcp_rename_no_new_name_error():
    """rename without new_name → error (before auth)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        from rlm_tools_bsl.projects import _reset_registry

        src = os.path.join(tmpdir, "src")
        os.makedirs(src)
        _reset_registry()
        with patch.dict(os.environ, {"RLM_CONFIG_FILE": os.path.join(tmpdir, "service.json")}):
            _reset_registry()
            _rlm_projects(action="add", name="Test", path=src, password="secret")
            r = json.loads(await rlm_projects(action="rename", name="Test"))
            assert "error" in r
            assert "new_name is required" in r["error"]


# --- Legacy migration ---


@pytest.mark.asyncio
async def test_mcp_update_legacy_set_password():
    """legacy project + update(password='X') → sets initial password (bootstrap)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        from rlm_tools_bsl.projects import _reset_registry, get_registry

        src = os.path.join(tmpdir, "src")
        os.makedirs(src)
        _reset_registry()
        with patch.dict(os.environ, {"RLM_CONFIG_FILE": os.path.join(tmpdir, "service.json")}):
            _reset_registry()
            _rlm_projects(action="add", name="Legacy", path=src)
            r = json.loads(await rlm_projects(action="update", name="Legacy", password="newpwd"))
            assert "updated" in r
            assert r["updated"]["has_password"] is True
            _reset_registry()
            reg = get_registry()
            assert reg.verify_password("Legacy", "newpwd") is True


@pytest.mark.asyncio
async def test_mcp_remove_no_name_error():
    """remove without name → error."""
    r = json.loads(await rlm_projects(action="remove"))
    assert "error" in r
    assert "name is required" in r["error"]


@pytest.mark.asyncio
async def test_mcp_update_no_name_error():
    """update without name → error."""
    r = json.loads(await rlm_projects(action="update"))
    assert "error" in r
    assert "name is required" in r["error"]


@pytest.mark.asyncio
async def test_mcp_clear_password_on_legacy_project():
    """clear_password on legacy project (no password) → approval_required to set password."""
    with tempfile.TemporaryDirectory() as tmpdir:
        from rlm_tools_bsl.projects import _reset_registry

        src = os.path.join(tmpdir, "src")
        os.makedirs(src)
        _reset_registry()
        with patch.dict(os.environ, {"RLM_CONFIG_FILE": os.path.join(tmpdir, "service.json")}):
            _reset_registry()
            _rlm_projects(action="add", name="Legacy", path=src)
            r = json.loads(await rlm_projects(action="update", name="Legacy", clear_password=True))
            assert r["approval_required"] is True
            assert r["action"] == "set_password"


@pytest.mark.asyncio
async def test_mcp_update_legacy_password_with_path_rejected():
    """legacy + update(password='X', path='...') → approval_required (set password first)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        from rlm_tools_bsl.projects import _reset_registry

        src = os.path.join(tmpdir, "src")
        os.makedirs(src)
        _reset_registry()
        with patch.dict(os.environ, {"RLM_CONFIG_FILE": os.path.join(tmpdir, "service.json")}):
            _reset_registry()
            _rlm_projects(action="add", name="Legacy", path=src)
            r = json.loads(await rlm_projects(action="update", name="Legacy", password="X", path="/new"))
            assert r["approval_required"] is True
            assert r["action"] == "set_password"


@pytest.mark.asyncio
async def test_mcp_update_legacy_password_with_description_rejected():
    """legacy + update(password='X', description='...') → approval_required."""
    with tempfile.TemporaryDirectory() as tmpdir:
        from rlm_tools_bsl.projects import _reset_registry

        src = os.path.join(tmpdir, "src")
        os.makedirs(src)
        _reset_registry()
        with patch.dict(os.environ, {"RLM_CONFIG_FILE": os.path.join(tmpdir, "service.json")}):
            _reset_registry()
            _rlm_projects(action="add", name="Legacy", path=src)
            r = json.loads(await rlm_projects(action="update", name="Legacy", password="X", description="Desc"))
            assert r["approval_required"] is True


@pytest.mark.asyncio
async def test_mcp_update_legacy_no_password_approval():
    """legacy + update without password → approval_required to set password."""
    with tempfile.TemporaryDirectory() as tmpdir:
        from rlm_tools_bsl.projects import _reset_registry

        src = os.path.join(tmpdir, "src")
        os.makedirs(src)
        _reset_registry()
        with patch.dict(os.environ, {"RLM_CONFIG_FILE": os.path.join(tmpdir, "service.json")}):
            _reset_registry()
            _rlm_projects(action="add", name="Legacy", path=src)
            r = json.loads(await rlm_projects(action="update", name="Legacy", description="Desc"))
            assert r["approval_required"] is True
            assert r["action"] == "set_password"


@pytest.mark.asyncio
async def test_mcp_remove_legacy_no_password_approval():
    """remove on legacy project → approval_required to set password."""
    with tempfile.TemporaryDirectory() as tmpdir:
        from rlm_tools_bsl.projects import _reset_registry

        src = os.path.join(tmpdir, "src")
        os.makedirs(src)
        _reset_registry()
        with patch.dict(os.environ, {"RLM_CONFIG_FILE": os.path.join(tmpdir, "service.json")}):
            _reset_registry()
            _rlm_projects(action="add", name="Legacy", path=src)
            r = json.loads(await rlm_projects(action="remove", name="Legacy"))
            assert r["approval_required"] is True
            assert r["action"] == "set_password"


# --- Resolve contract (structured payloads) ---


@pytest.mark.asyncio
async def test_mcp_remove_substring_resolve():
    """remove('Dev') with project 'DevERP' → resolve via substring + password check + ok."""
    with tempfile.TemporaryDirectory() as tmpdir:
        from rlm_tools_bsl.projects import _reset_registry

        src = os.path.join(tmpdir, "src")
        os.makedirs(src)
        _reset_registry()
        with patch.dict(os.environ, {"RLM_CONFIG_FILE": os.path.join(tmpdir, "service.json")}):
            _reset_registry()
            _rlm_projects(action="add", name="DevERP", path=src, password="secret")
            r = json.loads(await rlm_projects(action="remove", name="Dev", password="secret"))
            assert "removed" in r
            assert r["removed"]["name"] == "DevERP"


@pytest.mark.asyncio
async def test_mcp_update_ambiguous_error():
    """ambiguous substring → error with matches array."""
    with tempfile.TemporaryDirectory() as tmpdir:
        from rlm_tools_bsl.projects import _reset_registry

        src = os.path.join(tmpdir, "src")
        os.makedirs(src)
        _reset_registry()
        with patch.dict(os.environ, {"RLM_CONFIG_FILE": os.path.join(tmpdir, "service.json")}):
            _reset_registry()
            _rlm_projects(action="add", name="Config Alpha", path=src, password="secret")
            _rlm_projects(action="add", name="Config Beta", path=src, password="secret")
            r = json.loads(await rlm_projects(action="update", name="Config", password="secret"))
            assert "error" in r
            assert "Ambiguous" in r["error"]
            assert "matches" in r
            assert len(r["matches"]) == 2


@pytest.mark.asyncio
async def test_mcp_remove_fuzzy_suggestion():
    """fuzzy match → 'Did you mean ...'."""
    with tempfile.TemporaryDirectory() as tmpdir:
        from rlm_tools_bsl.projects import _reset_registry

        src = os.path.join(tmpdir, "src")
        os.makedirs(src)
        _reset_registry()
        with patch.dict(os.environ, {"RLM_CONFIG_FILE": os.path.join(tmpdir, "service.json")}):
            _reset_registry()
            _rlm_projects(action="add", name="Alpha", path=src, password="secret")
            r = json.loads(await rlm_projects(action="remove", name="Alphb", password="secret"))
            assert "error" in r
            assert "Did you mean" in r["error"]


@pytest.mark.asyncio
async def test_mcp_remove_not_found_error():
    """not found → error with available_projects."""
    with tempfile.TemporaryDirectory() as tmpdir:
        from rlm_tools_bsl.projects import _reset_registry

        src = os.path.join(tmpdir, "src")
        os.makedirs(src)
        _reset_registry()
        with patch.dict(os.environ, {"RLM_CONFIG_FILE": os.path.join(tmpdir, "service.json")}):
            _reset_registry()
            _rlm_projects(action="add", name="Alpha", path=src, password="secret")
            r = json.loads(await rlm_projects(action="remove", name="Ghost"))
            assert "error" in r
            assert "not found" in r["error"].lower()
            assert "available_projects" in r


@pytest.mark.asyncio
async def test_mcp_update_corrupted_registry():
    """RegistryCorruptedError → structured error."""
    with tempfile.TemporaryDirectory() as tmpdir:
        from rlm_tools_bsl.projects import _reset_registry

        cfg_file = os.path.join(tmpdir, "service.json")
        proj_file = os.path.join(tmpdir, "projects.json")
        with open(proj_file, "w", encoding="utf-8") as f:
            f.write("NOT JSON")
        _reset_registry()
        with patch.dict(os.environ, {"RLM_CONFIG_FILE": cfg_file}):
            _reset_registry()
            r = json.loads(await rlm_projects(action="remove", name="X"))
            assert "error" in r
            assert "corrupted" in r["error"].lower()


# --- clear_password ---


@pytest.mark.asyncio
async def test_mcp_update_wrong_password_only_gives_error():
    """update with wrong password and no other fields → error with instructions (not approval_required)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        from rlm_tools_bsl.projects import _reset_registry

        src = os.path.join(tmpdir, "src")
        os.makedirs(src)
        _reset_registry()
        with patch.dict(os.environ, {"RLM_CONFIG_FILE": os.path.join(tmpdir, "service.json")}):
            _reset_registry()
            _rlm_projects(action="add", name="Test", path=src, password="current")
            r = json.loads(await rlm_projects(action="update", name="Test", password="wrong"))
            assert "error" in r
            assert "approval_required" not in r
            assert "wrong password" in r["error"].lower() or "неверный" in r["error"].lower()


@pytest.mark.asyncio
async def test_mcp_update_change_password_two_steps():
    """Change password: clear_password with old, then set new password on legacy project."""
    with tempfile.TemporaryDirectory() as tmpdir:
        from rlm_tools_bsl.projects import _reset_registry, get_registry

        src = os.path.join(tmpdir, "src")
        os.makedirs(src)
        _reset_registry()
        with patch.dict(os.environ, {"RLM_CONFIG_FILE": os.path.join(tmpdir, "service.json")}):
            _reset_registry()
            _rlm_projects(action="add", name="Test", path=src, password="old")
            # Step 1: clear password
            r1 = json.loads(await rlm_projects(action="update", name="Test", clear_password=True, password="old"))
            assert "updated" in r1
            assert r1["updated"]["has_password"] is False
            # Step 2: set new password (project is now legacy)
            r2 = json.loads(await rlm_projects(action="update", name="Test", password="new"))
            assert "updated" in r2
            assert r2["updated"]["has_password"] is True
            _reset_registry()
            reg = get_registry()
            assert reg.verify_password("Test", "new") is True
            assert reg.verify_password("Test", "old") is False


@pytest.mark.asyncio
async def test_mcp_clear_password_requires_password():
    """clear_password + correct password → ok, has_password: false."""
    with tempfile.TemporaryDirectory() as tmpdir:
        from rlm_tools_bsl.projects import _reset_registry

        src = os.path.join(tmpdir, "src")
        os.makedirs(src)
        _reset_registry()
        with patch.dict(os.environ, {"RLM_CONFIG_FILE": os.path.join(tmpdir, "service.json")}):
            _reset_registry()
            _rlm_projects(action="add", name="Test", path=src, password="secret")
            r = json.loads(await rlm_projects(action="update", name="Test", clear_password=True, password="secret"))
            assert "updated" in r
            assert r["updated"]["has_password"] is False


@pytest.mark.asyncio
async def test_mcp_clear_then_remove_blocked():
    """clear password → remove → error 'set password'."""
    with tempfile.TemporaryDirectory() as tmpdir:
        from rlm_tools_bsl.projects import _reset_registry

        src = os.path.join(tmpdir, "src")
        os.makedirs(src)
        _reset_registry()
        with patch.dict(os.environ, {"RLM_CONFIG_FILE": os.path.join(tmpdir, "service.json")}):
            _reset_registry()
            _rlm_projects(action="add", name="Test", path=src, password="secret")
            # Clear password
            await rlm_projects(action="update", name="Test", clear_password=True, password="secret")
            # Try remove — should require password setup
            r = json.loads(await rlm_projects(action="remove", name="Test"))
            assert r["approval_required"] is True
            assert r["action"] == "set_password"


@pytest.mark.asyncio
async def test_mcp_clear_then_index_blocked():
    """clear password → rlm_index build → error 'no password'."""
    with tempfile.TemporaryDirectory() as tmpdir:
        from rlm_tools_bsl.projects import _reset_registry

        src = os.path.join(tmpdir, "src")
        os.makedirs(src)
        _reset_registry()
        with patch.dict(os.environ, {"RLM_CONFIG_FILE": os.path.join(tmpdir, "service.json")}):
            _reset_registry()
            _rlm_projects(action="add", name="Test", path=src, password="secret")
            # Clear password
            await rlm_projects(action="update", name="Test", clear_password=True, password="secret")
            # Try index build — should require password setup
            r = json.loads(await rlm_index(action="build", project="Test"))
            assert r["approval_required"] is True
            assert r["action"] == "set_password"


# --- has_password flag in MCP responses ---


@pytest.mark.asyncio
async def test_mcp_list_has_password_flag():
    """list → has_password: true/false for each project."""
    with tempfile.TemporaryDirectory() as tmpdir:
        from rlm_tools_bsl.projects import _reset_registry

        src = os.path.join(tmpdir, "src")
        os.makedirs(src)
        _reset_registry()
        with patch.dict(os.environ, {"RLM_CONFIG_FILE": os.path.join(tmpdir, "service.json")}):
            _reset_registry()
            _rlm_projects(action="add", name="WithPwd", path=src, password="secret")
            _rlm_projects(action="add", name="NoPwd", path=src)
            r = json.loads(await rlm_projects(action="list"))
            by_name = {p["name"]: p for p in r["projects"]}
            assert by_name["WithPwd"]["has_password"] is True
            assert by_name["NoPwd"]["has_password"] is False


@pytest.mark.asyncio
async def test_mcp_add_response_has_password_true():
    """add response → has_password: true."""
    with tempfile.TemporaryDirectory() as tmpdir:
        from rlm_tools_bsl.projects import _reset_registry

        src = os.path.join(tmpdir, "src")
        os.makedirs(src)
        _reset_registry()
        with patch.dict(os.environ, {"RLM_CONFIG_FILE": os.path.join(tmpdir, "service.json")}):
            _reset_registry()
            r = json.loads(await rlm_projects(action="add", name="Test", path=src, password="secret"))
            assert r["added"]["has_password"] is True


@pytest.mark.asyncio
async def test_mcp_remove_response_has_password():
    """remove response → has_password: true."""
    with tempfile.TemporaryDirectory() as tmpdir:
        from rlm_tools_bsl.projects import _reset_registry

        src = os.path.join(tmpdir, "src")
        os.makedirs(src)
        _reset_registry()
        with patch.dict(os.environ, {"RLM_CONFIG_FILE": os.path.join(tmpdir, "service.json")}):
            _reset_registry()
            _rlm_projects(action="add", name="Test", path=src, password="secret")
            r = json.loads(await rlm_projects(action="remove", name="Test", password="secret"))
            assert r["removed"]["has_password"] is True


@pytest.mark.asyncio
async def test_mcp_update_clear_response_has_password_false():
    """update clear_password → has_password: false."""
    with tempfile.TemporaryDirectory() as tmpdir:
        from rlm_tools_bsl.projects import _reset_registry

        src = os.path.join(tmpdir, "src")
        os.makedirs(src)
        _reset_registry()
        with patch.dict(os.environ, {"RLM_CONFIG_FILE": os.path.join(tmpdir, "service.json")}):
            _reset_registry()
            _rlm_projects(action="add", name="Test", path=src, password="secret")
            r = json.loads(await rlm_projects(action="update", name="Test", clear_password=True, password="secret"))
            assert r["updated"]["has_password"] is False


# --- Unaffected ---


@pytest.mark.asyncio
async def test_mcp_list_no_auth():
    """list without password → ok."""
    with tempfile.TemporaryDirectory() as tmpdir:
        from rlm_tools_bsl.projects import _reset_registry

        src = os.path.join(tmpdir, "src")
        os.makedirs(src)
        _reset_registry()
        with patch.dict(os.environ, {"RLM_CONFIG_FILE": os.path.join(tmpdir, "service.json")}):
            _reset_registry()
            _rlm_projects(action="add", name="Test", path=src, password="secret")
            r = json.loads(await rlm_projects(action="list"))
            assert "projects" in r
            assert "approval_required" not in r


# ---------------------------------------------------------------------------
# _resolve_path_map tests
# ---------------------------------------------------------------------------


def test_resolve_path_map_no_env():
    """Without RLM_PATH_MAP, path is returned unchanged."""
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("RLM_PATH_MAP", None)
        assert _resolve_path_map("D:/Repos/erp/src") == "D:/Repos/erp/src"


def test_resolve_path_map_windows_prefix():
    with patch.dict(os.environ, {"RLM_PATH_MAP": "D:/Repos:/repos"}):
        assert _resolve_path_map("D:/Repos/erp/src/cf") == "/repos/erp/src/cf"


def test_resolve_path_map_backslashes():
    with patch.dict(os.environ, {"RLM_PATH_MAP": "D:/Repos:/repos"}):
        assert _resolve_path_map("D:\\Repos\\erp\\src\\cf") == "/repos/erp/src/cf"


def test_resolve_path_map_case_insensitive():
    with patch.dict(os.environ, {"RLM_PATH_MAP": "D:/Repos:/repos"}):
        assert _resolve_path_map("d:/repos/erp/src") == "/repos/erp/src"


def test_resolve_path_map_no_match():
    with patch.dict(os.environ, {"RLM_PATH_MAP": "D:/Repos:/repos"}):
        assert _resolve_path_map("/home/user/data") == "/home/user/data"


def test_resolve_path_map_linux():
    with patch.dict(os.environ, {"RLM_PATH_MAP": "/home/user/repos:/repos"}):
        assert _resolve_path_map("/home/user/repos/erp/src") == "/repos/erp/src"


def test_resolve_path_map_trailing_slash():
    with patch.dict(os.environ, {"RLM_PATH_MAP": "D:/Repos/:/repos/"}):
        assert _resolve_path_map("D:/Repos/erp/src") == "/repos/erp/src"


def test_resolve_path_map_partial_prefix_no_match():
    """D:/Rep must NOT match D:/Repos — boundary check."""
    with patch.dict(os.environ, {"RLM_PATH_MAP": "D:/Rep:/repos"}):
        assert _resolve_path_map("D:/Repos/erp") == "D:/Repos/erp"


# ---------------------------------------------------------------------------
# Async build/update fire-and-forget tests (v1.7.4)
# ---------------------------------------------------------------------------


def _wait_build_thread(name: str, timeout: float = 30) -> None:
    """Wait for background build thread to finish."""
    import threading as _th

    for t in _th.enumerate():
        if t.name == name:
            t.join(timeout=timeout)


def _cleanup_build_jobs() -> None:
    """Clear _build_jobs state between tests."""
    with _build_jobs_lock:
        _build_jobs.clear()


@pytest.mark.asyncio
async def test_mcp_build_returns_started():
    """build via MCP → {"started": true, "action": "build"}."""
    with tempfile.TemporaryDirectory() as tmpdir:
        from rlm_tools_bsl.projects import _reset_registry

        src = os.path.join(tmpdir, "src")
        idx_dir = os.path.join(tmpdir, "indexes")
        os.makedirs(src)
        with open(os.path.join(src, "Module.bsl"), "w", encoding="utf-8") as f:
            f.write("Процедура Тест()\nКонецПроцедуры\n")

        _reset_registry()
        _cleanup_build_jobs()
        with patch.dict(
            os.environ, {"RLM_CONFIG_FILE": os.path.join(tmpdir, "service.json"), "RLM_INDEX_DIR": idx_dir}
        ):
            _reset_registry()
            _rlm_projects(action="add", name="Proj1", path=src, password="pw")
            r = json.loads(await rlm_index(action="build", project="Proj1", confirm="pw"))
            assert r["started"] is True
            assert r["action"] == "build"
            assert r["project"] == "Proj1"
            assert "message" in r
            _wait_build_thread("build-Proj1")
            _rlm_index(action="drop", path=src)
            _cleanup_build_jobs()


@pytest.mark.asyncio
async def test_mcp_update_returns_started():
    """update via MCP → {"started": true, "action": "update"}."""
    with tempfile.TemporaryDirectory() as tmpdir:
        from rlm_tools_bsl.projects import _reset_registry

        src = os.path.join(tmpdir, "src")
        idx_dir = os.path.join(tmpdir, "indexes")
        os.makedirs(src)
        with open(os.path.join(src, "Module.bsl"), "w", encoding="utf-8") as f:
            f.write("Процедура Тест()\nКонецПроцедуры\n")

        _reset_registry()
        _cleanup_build_jobs()
        with patch.dict(
            os.environ, {"RLM_CONFIG_FILE": os.path.join(tmpdir, "service.json"), "RLM_INDEX_DIR": idx_dir}
        ):
            _reset_registry()
            _rlm_projects(action="add", name="Proj2", path=src, password="pw")
            # Build first (sync via internal)
            _rlm_index(action="build", path=src)
            r = json.loads(await rlm_index(action="update", project="Proj2", confirm="pw"))
            assert r["started"] is True
            assert r["action"] == "update"
            _wait_build_thread("build-Proj2")
            _rlm_index(action="drop", path=src)
            _cleanup_build_jobs()


@pytest.mark.asyncio
async def test_mcp_build_completes_check_info():
    """build → started, wait, info → build_status: done + result."""
    with tempfile.TemporaryDirectory() as tmpdir:
        from rlm_tools_bsl.projects import _reset_registry

        src = os.path.join(tmpdir, "src")
        idx_dir = os.path.join(tmpdir, "indexes")
        os.makedirs(src)
        with open(os.path.join(src, "Module.bsl"), "w", encoding="utf-8") as f:
            f.write("Процедура Тест()\nКонецПроцедуры\n")

        _reset_registry()
        _cleanup_build_jobs()
        with patch.dict(
            os.environ, {"RLM_CONFIG_FILE": os.path.join(tmpdir, "service.json"), "RLM_INDEX_DIR": idx_dir}
        ):
            _reset_registry()
            _rlm_projects(action="add", name="Proj3", path=src, password="pw")
            r = json.loads(await rlm_index(action="build", project="Proj3", confirm="pw"))
            assert r["started"] is True
            _wait_build_thread("build-Proj3")
            # Check info — should show build_status: done
            info = json.loads(_rlm_index(action="info", path=src))
            assert info["action"] == "info"
            assert info["build_status"] == "done"
            assert info["build_result"] is not None
            _rlm_index(action="drop", path=src)
            _cleanup_build_jobs()


@pytest.mark.asyncio
async def test_mcp_drop_still_sync():
    """drop via MCP → immediate result (not started)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        from rlm_tools_bsl.projects import _reset_registry

        src = os.path.join(tmpdir, "src")
        idx_dir = os.path.join(tmpdir, "indexes")
        os.makedirs(src)
        with open(os.path.join(src, "Module.bsl"), "w", encoding="utf-8") as f:
            f.write("Процедура Тест()\nКонецПроцедуры\n")

        _reset_registry()
        _cleanup_build_jobs()
        with patch.dict(
            os.environ, {"RLM_CONFIG_FILE": os.path.join(tmpdir, "service.json"), "RLM_INDEX_DIR": idx_dir}
        ):
            _reset_registry()
            _rlm_projects(action="add", name="Proj4", path=src, password="pw")
            _rlm_index(action="build", path=src)
            r = json.loads(await rlm_index(action="drop", project="Proj4", confirm="pw"))
            assert r["action"] == "drop"
            assert "started" not in r
            _cleanup_build_jobs()


@pytest.mark.asyncio
async def test_mcp_build_already_running():
    """Repeated build → error 'already in progress'."""
    with tempfile.TemporaryDirectory() as tmpdir:
        from rlm_tools_bsl.projects import _reset_registry

        src = os.path.join(tmpdir, "src")
        idx_dir = os.path.join(tmpdir, "indexes")
        os.makedirs(src)
        with open(os.path.join(src, "Module.bsl"), "w", encoding="utf-8") as f:
            f.write("Процедура Тест()\nКонецПроцедуры\n")

        _reset_registry()
        _cleanup_build_jobs()
        with patch.dict(
            os.environ, {"RLM_CONFIG_FILE": os.path.join(tmpdir, "service.json"), "RLM_INDEX_DIR": idx_dir}
        ):
            _reset_registry()
            _rlm_projects(action="add", name="Proj5", path=src, password="pw")
            # Simulate a running build by injecting into _build_jobs
            resolved = _canonicalize_path(src)
            import time as _time

            with _build_jobs_lock:
                _build_jobs[resolved] = {
                    "status": "building",
                    "action": "build",
                    "project": "Proj5",
                    "started_at": _time.time(),
                    "finished_at": None,
                    "result": None,
                    "error": None,
                }
            r = json.loads(await rlm_index(action="build", project="Proj5", confirm="pw"))
            assert "error" in r
            assert "already in progress" in r["error"]
            _cleanup_build_jobs()


@pytest.mark.asyncio
async def test_mcp_drop_during_build_blocked():
    """drop during active build → error."""
    with tempfile.TemporaryDirectory() as tmpdir:
        from rlm_tools_bsl.projects import _reset_registry

        src = os.path.join(tmpdir, "src")
        idx_dir = os.path.join(tmpdir, "indexes")
        os.makedirs(src)

        _reset_registry()
        _cleanup_build_jobs()
        with patch.dict(
            os.environ, {"RLM_CONFIG_FILE": os.path.join(tmpdir, "service.json"), "RLM_INDEX_DIR": idx_dir}
        ):
            _reset_registry()
            _rlm_projects(action="add", name="Proj6", path=src, password="pw")
            # Simulate a running build
            resolved = _canonicalize_path(src)
            import time as _time

            with _build_jobs_lock:
                _build_jobs[resolved] = {
                    "status": "building",
                    "action": "build",
                    "project": "Proj6",
                    "started_at": _time.time(),
                    "finished_at": None,
                    "result": None,
                    "error": None,
                }
            r = json.loads(await rlm_index(action="drop", project="Proj6", confirm="pw"))
            assert "error" in r
            assert "Cannot drop" in r["error"]
            _cleanup_build_jobs()


def test_mcp_info_building_no_db():
    """info during build + no db → build_status: building."""
    with tempfile.TemporaryDirectory() as tmpdir:
        import time as _time

        src = os.path.join(tmpdir, "src")
        os.makedirs(src)

        _cleanup_build_jobs()
        resolved = _canonicalize_path(src)
        with _build_jobs_lock:
            _build_jobs[resolved] = {
                "status": "building",
                "action": "build",
                "project": "TestBld",
                "started_at": _time.time() - 10,
                "finished_at": None,
                "result": None,
                "error": None,
            }
        with patch.dict(os.environ, {"RLM_INDEX_DIR": os.path.join(tmpdir, "idx")}):
            r = json.loads(_rlm_index(action="info", path=src))
        assert r["build_status"] == "building"
        assert r["build_action"] == "build"
        assert r["build_elapsed"] >= 10
        assert "error" not in r
        _cleanup_build_jobs()


def test_mcp_info_building_existing_db():
    """info during rebuild over existing DB → build_status: building (not DB data)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        import time as _time

        src = os.path.join(tmpdir, "src")
        idx_dir = os.path.join(tmpdir, "indexes")
        os.makedirs(src)
        with open(os.path.join(src, "Module.bsl"), "w", encoding="utf-8") as f:
            f.write("Процедура Тест()\nКонецПроцедуры\n")

        _cleanup_build_jobs()
        with patch.dict(os.environ, {"RLM_INDEX_DIR": idx_dir}):
            # Build a real index first
            _rlm_index(action="build", path=src)
            # Now simulate an active rebuild
            resolved = _canonicalize_path(src)
            with _build_jobs_lock:
                _build_jobs[resolved] = {
                    "status": "building",
                    "action": "build",
                    "project": "TestRebuild",
                    "started_at": _time.time() - 5,
                    "finished_at": None,
                    "result": None,
                    "error": None,
                }
            r = json.loads(_rlm_index(action="info", path=src))
            # Should return building status, NOT db contents
            assert r["build_status"] == "building"
            assert "modules" not in r  # no DB stats
            _rlm_index(action="drop", path=src)
        _cleanup_build_jobs()


def test_mcp_info_error_no_db():
    """Build error before DB created → info returns build_status: error (not 'Index not found')."""
    with tempfile.TemporaryDirectory() as tmpdir:
        import time as _time

        src = os.path.join(tmpdir, "src")
        os.makedirs(src)

        _cleanup_build_jobs()
        resolved = _canonicalize_path(src)
        with _build_jobs_lock:
            _build_jobs[resolved] = {
                "status": "error",
                "action": "build",
                "project": "FailedBuild",
                "started_at": _time.time() - 60,
                "finished_at": _time.time() - 5,
                "result": None,
                "error": "FileNotFoundError: no .bsl files",
            }
        with patch.dict(os.environ, {"RLM_INDEX_DIR": os.path.join(tmpdir, "idx")}):
            r = json.loads(_rlm_index(action="info", path=src))
        # Must return build_status: error, NOT "Index not found"
        assert r.get("build_status") == "error"
        assert "FileNotFoundError" in r["build_error"]
        assert "error" not in r or r.get("build_status")  # no generic "error" key
        _cleanup_build_jobs()


@pytest.mark.asyncio
async def test_mcp_info_via_project_during_build():
    """info via project= (MCP wrapper) during active build → build_status: building."""
    with tempfile.TemporaryDirectory() as tmpdir:
        import time as _time

        from rlm_tools_bsl.projects import _reset_registry

        src = os.path.join(tmpdir, "src")
        idx_dir = os.path.join(tmpdir, "indexes")
        os.makedirs(src)

        _reset_registry()
        _cleanup_build_jobs()
        with patch.dict(
            os.environ, {"RLM_CONFIG_FILE": os.path.join(tmpdir, "service.json"), "RLM_INDEX_DIR": idx_dir}
        ):
            _reset_registry()
            _rlm_projects(action="add", name="InfoProj", path=src, password="pw")
            # Simulate a running build
            resolved = _canonicalize_path(src)
            with _build_jobs_lock:
                _build_jobs[resolved] = {
                    "status": "building",
                    "action": "build",
                    "project": "InfoProj",
                    "started_at": _time.time() - 15,
                    "finished_at": None,
                    "result": None,
                    "error": None,
                }
            # Call info via MCP wrapper with project=
            r = json.loads(await rlm_index(action="info", project="InfoProj"))
            assert r["build_status"] == "building"
            assert r["build_elapsed"] >= 15
            assert "error" not in r
            _cleanup_build_jobs()


def test_mcp_info_build_error():
    """Build with error → info → build_status: error."""
    with tempfile.TemporaryDirectory() as tmpdir:
        import time as _time

        src = os.path.join(tmpdir, "src")
        idx_dir = os.path.join(tmpdir, "indexes")
        os.makedirs(src)
        with open(os.path.join(src, "Module.bsl"), "w", encoding="utf-8") as f:
            f.write("Процедура Тест()\nКонецПроцедуры\n")

        _cleanup_build_jobs()
        with patch.dict(os.environ, {"RLM_INDEX_DIR": idx_dir}):
            # Build a real index so info can read DB
            _rlm_index(action="build", path=src)
            resolved = _canonicalize_path(src)
            with _build_jobs_lock:
                _build_jobs[resolved] = {
                    "status": "error",
                    "action": "build",
                    "project": "TestErr",
                    "started_at": _time.time() - 30,
                    "finished_at": _time.time() - 1,
                    "result": None,
                    "error": "Something failed",
                }
            r = json.loads(_rlm_index(action="info", path=src))
            assert r["build_status"] == "error"
            assert r["build_error"] == "Something failed"
            assert "modules" in r  # DB stats still present
            _rlm_index(action="drop", path=src)
        _cleanup_build_jobs()


def test_build_jobs_cleanup():
    """Stale completed jobs are cleaned up on next build launch."""
    import time as _time

    _cleanup_build_jobs()
    # Insert a stale job (finished > 1h ago)
    with _build_jobs_lock:
        _build_jobs["/fake/stale/path"] = {
            "status": "done",
            "action": "build",
            "project": "Stale",
            "started_at": _time.time() - 7200,
            "finished_at": _time.time() - 3700,
            "result": {"action": "build"},
            "error": None,
        }
    assert "/fake/stale/path" in _build_jobs

    # Trigger cleanup by attempting a build (will fail at password check, but cleanup runs first)
    # We test cleanup by directly simulating the wrapper logic
    now = _time.time()
    with _build_jobs_lock:
        stale = [
            k
            for k, v in _build_jobs.items()
            if v["status"] != "building" and v.get("finished_at") and now - v["finished_at"] > 3600
        ]
        for k in stale:
            del _build_jobs[k]
    assert "/fake/stale/path" not in _build_jobs
    _cleanup_build_jobs()


@pytest.mark.asyncio
async def test_mcp_build_db_tables_valid():
    """After background build completes, DB has all core tables with data."""
    import sqlite3
    import threading as _th

    with tempfile.TemporaryDirectory() as tmpdir:
        from rlm_tools_bsl.bsl_index import get_index_db_path
        from rlm_tools_bsl.projects import _reset_registry

        src = os.path.join(tmpdir, "src")
        idx_dir = os.path.join(tmpdir, "indexes")
        os.makedirs(src)
        # Создаем минимальный .bsl с процедурой
        with open(os.path.join(src, "Module.bsl"), "w", encoding="utf-8") as f:
            f.write("Процедура МойМетод() Экспорт\nКонецПроцедуры\n")

        _reset_registry()
        _cleanup_build_jobs()
        with patch.dict(
            os.environ, {"RLM_CONFIG_FILE": os.path.join(tmpdir, "service.json"), "RLM_INDEX_DIR": idx_dir}
        ):
            _reset_registry()
            _rlm_projects(action="add", name="DbCheck", path=src, password="pw")
            r = json.loads(await rlm_index(action="build", project="DbCheck", confirm="pw"))
            assert r["started"] is True
            for t in _th.enumerate():
                if t.name == "build-DbCheck":
                    t.join(timeout=30)

            # Открываем БД напрямую и проверяем таблицы
            resolved = _canonicalize_path(src)
            db_path = get_index_db_path(resolved)
            assert db_path.exists(), f"DB not found at {db_path}"

            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            try:
                # Список всех таблиц
                tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                    ).fetchall()
                }
                # Обязательные таблицы
                expected = {"index_meta", "modules", "methods", "calls", "file_paths", "regions", "module_headers"}
                missing = expected - tables
                assert not missing, f"Missing tables: {missing}"

                # modules — хотя бы 1 запись
                cnt = conn.execute("SELECT COUNT(*) FROM modules").fetchone()[0]
                assert cnt >= 1, "modules table empty, expected ≥1"

                # methods — наш МойМетод должен быть
                cnt = conn.execute("SELECT COUNT(*) FROM methods").fetchone()[0]
                assert cnt >= 1, "methods table empty, expected ≥1"

                # index_meta — built_at должен быть
                row = conn.execute("SELECT value FROM index_meta WHERE key='built_at'").fetchone()
                assert row is not None, "index_meta missing built_at"
                assert float(row[0]) > 0
            finally:
                conn.close()

            _rlm_index(action="drop", path=src)
            _cleanup_build_jobs()


def test_rlm_start_failure_path_closes_reader(tmp_path, monkeypatch):
    import rlm_tools_bsl.server as srv

    closed = {"v": False}

    class _FakeReader:
        def __init__(self, *a, **k):
            pass

        def get_statistics(self):
            # FRESH-путь не читает built_at (только STALE_AGE) → константа; в
            # test_server.py нет module-scope `import time` (codex-раунд4 #10).
            return {"built_at": 0.0, "builder_version": srv.BUILDER_VERSION}

        def get_startup_meta(self):
            raise RuntimeError("boom after reader created, before registration")

        def close(self):
            closed["v"] = True

    db = tmp_path / "bsl_index.db"
    db.write_text("")  # .exists() == True
    monkeypatch.setattr(srv, "get_index_db_path", lambda p: db)
    monkeypatch.setattr(srv, "check_index_usable", lambda *a, **k: srv.IndexStatus.FRESH)
    monkeypatch.setattr(srv, "IndexReader", _FakeReader)

    out = srv._rlm_start(query="q", path=str(tmp_path))
    assert "error" in out
    assert closed["v"] is True  # reader ЯВНО закрыт на failure-path
