"""Tests for extension_overrides indexing (v1.5.0, Level-8).

Covers: schema, collector, build/update, IndexReader, helpers enrichment.
"""

import os
import sqlite3
import textwrap

import pytest

from rlm_tools_bsl.bsl_index import IndexBuilder, IndexReader


# ---------------------------------------------------------------------------
# Helpers to create test fixtures
# ---------------------------------------------------------------------------

_CF_MAIN_XML = textwrap.dedent("""\
    <?xml version="1.0" encoding="UTF-8"?>
    <MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"
                    xmlns:v8="http://v8.1c.ru/8.1/data/core">
        <Configuration uuid="00000000-0000-0000-0000-000000000001">
            <Properties>
                <Name>ОсновнаяКонфигурация</Name>
                <NamePrefix/>
                <ConfigurationExtensionCompatibilityMode>Version8_3_24</ConfigurationExtensionCompatibilityMode>
            </Properties>
        </Configuration>
    </MetaDataObject>
""")


def _cf_extension_xml(name="ТестовоеРасширение", purpose="Customization", prefix="мр_"):
    return textwrap.dedent(f"""\
        <?xml version="1.0" encoding="UTF-8"?>
        <MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"
                        xmlns:v8="http://v8.1c.ru/8.1/data/core">
            <Configuration uuid="00000000-0000-0000-0000-000000000002">
                <Properties>
                    <ObjectBelonging>Adopted</ObjectBelonging>
                    <Name>{name}</Name>
                    <ConfigurationExtensionPurpose>{purpose}</ConfigurationExtensionPurpose>
                    <NamePrefix>{prefix}</NamePrefix>
                </Properties>
            </Configuration>
        </MetaDataObject>
    """)


_MAIN_MODULE_BSL = textwrap.dedent("""\
    Процедура ОбработкаЗаполнения(ДанныеЗаполнения, СтандартнаяОбработка)
        // основная логика
    КонецПроцедуры

    Процедура ПередЗаписью(Отказ)
        // валидация
    КонецПроцедуры
""")


_EXT_MODULE_BSL = textwrap.dedent("""\
    &После("ОбработкаЗаполнения")
    Процедура мр_ОбработкаЗаполнения(ДанныеЗаполнения, СтандартнаяОбработка)
        // расширенная логика
    КонецПроцедуры

    &Вместо("ПередЗаписью")
    Процедура мр_ПередЗаписью(Отказ)
        // замена
    КонецПроцедуры
""")


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _make_main_with_extension(parent_dir):
    """Create src/cf/ (main) + src/cfe/ТестовоеРасширение/ (extension)."""
    cf = os.path.join(parent_dir, "src", "cf")
    cfe = os.path.join(parent_dir, "src", "cfe", "ТестовоеРасширение")

    # Main config
    _write(os.path.join(cf, "Configuration.xml"), _CF_MAIN_XML)
    _write(
        os.path.join(cf, "Catalogs", "Номенклатура", "Ext", "ObjectModule.bsl"),
        _MAIN_MODULE_BSL,
    )

    # Extension
    _write(os.path.join(cfe, "Configuration.xml"), _cf_extension_xml())
    _write(
        os.path.join(cfe, "Catalogs", "Номенклатура", "Ext", "ObjectModule.bsl"),
        _EXT_MODULE_BSL,
    )

    return cf, cfe


def _make_extension_only(parent_dir):
    """Create standalone extension directory."""
    ext_dir = os.path.join(parent_dir, "ext")
    _write(os.path.join(ext_dir, "Configuration.xml"), _cf_extension_xml())
    _write(
        os.path.join(ext_dir, "Catalogs", "Номенклатура", "Ext", "ObjectModule.bsl"),
        _EXT_MODULE_BSL,
    )
    return ext_dir


# ---------------------------------------------------------------------------
# Schema and version
# ---------------------------------------------------------------------------


class TestSchema:
    def test_extension_overrides_table_created(self, tmp_path, monkeypatch):
        """Build creates extension_overrides table in schema."""
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
        cf = str(tmp_path / "cf")
        _write(os.path.join(cf, "Configuration.xml"), _CF_MAIN_XML)
        _write(os.path.join(cf, "CommonModules", "Test", "Ext", "Module.bsl"), "")

        builder = IndexBuilder()
        db_path = builder.build(cf, build_calls=False, build_metadata=False, build_fts=False)

        conn = sqlite3.connect(str(db_path))
        # Table must exist
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        conn.close()
        assert "extension_overrides" in tables


# ---------------------------------------------------------------------------
# Build with extensions (main config)
# ---------------------------------------------------------------------------


class TestBuildMainWithExtensions:
    def test_build_populates_overrides(self, tmp_path, monkeypatch):
        """Build main config with nearby extension populates extension_overrides."""
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
        cf, cfe = _make_main_with_extension(tmp_path)

        builder = IndexBuilder()
        db_path = builder.build(cf, build_calls=False, build_metadata=False, build_fts=False)

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM extension_overrides").fetchall()
        conn.close()

        assert len(rows) == 2

        # Check override data
        overrides = [dict(r) for r in rows]
        by_method = {ov["target_method"]: ov for ov in overrides}

        assert "ОбработкаЗаполнения" in by_method
        assert "ПередЗаписью" in by_method

        ov1 = by_method["ОбработкаЗаполнения"]
        assert ov1["annotation"] == "После"
        assert ov1["extension_name"] == "ТестовоеРасширение"
        assert ov1["extension_method"] == "мр_ОбработкаЗаполнения"
        assert ov1["object_name"] == "Номенклатура"
        assert ov1["extension_root"] == cfe
        assert ov1["ext_module_path"] == "Catalogs/Номенклатура/Ext/ObjectModule.bsl"

        ov2 = by_method["ПередЗаписью"]
        assert ov2["annotation"] == "Вместо"

    def test_source_module_linked(self, tmp_path, monkeypatch):
        """Overrides link to source module via source_module_id and source_path."""
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
        cf, _cfe = _make_main_with_extension(tmp_path)

        builder = IndexBuilder()
        db_path = builder.build(cf, build_calls=False, build_metadata=False, build_fts=False)

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM extension_overrides").fetchall()

        for r in rows:
            assert r["source_module_id"] is not None
            assert r["source_path"] != ""
            assert "Номенклатура" in r["source_path"]

        conn.close()

    def test_target_method_line_populated(self, tmp_path, monkeypatch):
        """target_method_line is populated from methods table."""
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
        cf, _cfe = _make_main_with_extension(tmp_path)

        builder = IndexBuilder()
        db_path = builder.build(cf, build_calls=False, build_metadata=False, build_fts=False)

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM extension_overrides").fetchall()

        for r in rows:
            assert r["target_method_line"] is not None
            assert r["target_method_line"] > 0

        conn.close()

    def test_meta_written(self, tmp_path, monkeypatch):
        """has_extension_overrides and extension_overrides_count in meta."""
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
        cf, _cfe = _make_main_with_extension(tmp_path)

        builder = IndexBuilder()
        db_path = builder.build(cf, build_calls=False, build_metadata=False, build_fts=False)

        conn = sqlite3.connect(str(db_path))
        has = conn.execute("SELECT value FROM index_meta WHERE key='has_extension_overrides'").fetchone()[0]
        count = conn.execute("SELECT value FROM index_meta WHERE key='extension_overrides_count'").fetchone()[0]
        conn.close()

        assert has == "1"
        assert int(count) == 2

    def test_no_extensions_meta_zero(self, tmp_path, monkeypatch):
        """Config without extensions writes meta with 0."""
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
        cf = str(tmp_path / "cf")
        _write(os.path.join(cf, "Configuration.xml"), _CF_MAIN_XML)
        _write(os.path.join(cf, "CommonModules", "Test", "Ext", "Module.bsl"), "")

        builder = IndexBuilder()
        db_path = builder.build(cf, build_calls=False, build_metadata=False, build_fts=False)

        conn = sqlite3.connect(str(db_path))
        has = conn.execute("SELECT value FROM index_meta WHERE key='has_extension_overrides'").fetchone()[0]
        count = conn.execute("SELECT value FROM index_meta WHERE key='extension_overrides_count'").fetchone()[0]
        conn.close()

        assert has == "0"
        assert count == "0"


# ---------------------------------------------------------------------------
# Build extension-only (no main config)
# ---------------------------------------------------------------------------


class TestBuildExtensionOnly:
    def test_extension_build_no_source_link(self, tmp_path, monkeypatch):
        """Building an extension without main config: source_module_id=NULL."""
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
        ext_dir = _make_extension_only(tmp_path)

        builder = IndexBuilder()
        db_path = builder.build(ext_dir, build_calls=False, build_metadata=False, build_fts=False)

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM extension_overrides").fetchall()
        conn.close()

        assert len(rows) == 2
        for r in rows:
            assert r["source_module_id"] is None
            assert r["source_path"] == ""
            assert r["target_method_line"] is None


class TestLiveOverridesContract:
    """get_overrides() LIVE fallback must carry extension_name in EVERY branch.

    Regression: a session opened directly ON an extension (role=EXTENSION) used the
    raw find_extension_overrides rows, which lack extension_name → the `расширения`
    recipe's `{o['extension_name'] for o in ov}` raised KeyError (confirmed live).
    """

    def test_extension_session_live_overrides_have_extension_name(self, tmp_path):
        from rlm_tools_bsl.bsl_helpers import make_bsl_helpers
        from rlm_tools_bsl.format_detector import detect_format
        from rlm_tools_bsl.helpers import make_helpers

        ext_dir = _make_extension_only(tmp_path)
        helpers, resolve_safe = make_helpers(str(ext_dir))
        fmt = detect_format(str(ext_dir))
        bsl = make_bsl_helpers(
            base_path=str(ext_dir),
            resolve_safe=resolve_safe,
            read_file_fn=helpers["read_file"],
            grep_fn=helpers["grep"],
            glob_files_fn=helpers["glob_files"],
            format_info=fmt,
            idx_reader=None,  # force the live fallback → EXTENSION-role branch
        )
        res = bsl["get_overrides"]()
        assert res["source"] == "live"
        assert res["overrides"], "expected live overrides from the extension"
        for ov in res["overrides"]:
            assert ov.get("extension_name"), ov
            assert "extension_root" in ov
        # The расширения recipe's set-comprehension must not raise.
        names = {o["extension_name"] for o in res["overrides"]}
        assert names == {"ТестовоеРасширение"}


class _FakeOverridesReader:
    """Minimal idx_reader stub: only get_extension_overrides is exercised by get_overrides."""

    def __init__(self, rows):
        self._rows = rows

    def get_extension_overrides(self, object_name="", method_name=""):
        return list(self._rows)


def _make_bsl_with_reader(idx_reader):
    from rlm_tools_bsl.bsl_helpers import make_bsl_helpers

    return make_bsl_helpers(
        base_path="/nonexistent",
        resolve_safe=lambda p: __import__("pathlib").Path(p),
        read_file_fn=lambda p: "",
        grep_fn=lambda pat, path="": [],
        glob_files_fn=lambda pat: [],
        idx_reader=idx_reader,
    )


class TestGetOverridesTruncated:
    """v1.24.0 #6 — truncated-флаг + cap=200 во ВСЕХ ветках get_overrides + find_ext_overrides."""

    def test_index_branch_truncated_over_200(self):
        rows = [{"object_name": "Об", "target_method": f"M{i}", "extension_name": "E"} for i in range(250)]
        bsl = _make_bsl_with_reader(_FakeOverridesReader(rows))
        res = bsl["get_overrides"]()
        assert res["source"] == "index"
        assert res["partial"] is False
        assert res["total"] == 250
        assert len(res["overrides"]) == 200
        assert res["truncated"] is True

    def test_index_branch_not_truncated_under_200(self):
        rows = [{"object_name": "Об", "target_method": f"M{i}", "extension_name": "E"} for i in range(50)]
        bsl = _make_bsl_with_reader(_FakeOverridesReader(rows))
        res = bsl["get_overrides"]()
        assert res["total"] == 50
        assert len(res["overrides"]) == 50
        assert res["truncated"] is False

    def test_unavailable_branch_has_truncated_false(self, monkeypatch):
        import rlm_tools_bsl.extension_detector as ed

        def _boom(_path):
            raise RuntimeError("no context")

        monkeypatch.setattr(ed, "detect_extension_context", _boom)
        # idx_reader None → skip index branch, then _det raises → unavailable branch.
        bsl = _make_bsl_with_reader(None)
        res = bsl["get_overrides"]()
        assert res["source"] == "unavailable"
        assert res["partial"] is True
        assert res["truncated"] is False

    def test_live_branch_truncated_over_200(self, monkeypatch):
        import rlm_tools_bsl.extension_detector as ed
        from rlm_tools_bsl.extension_detector import ConfigRole

        big = [{"target_method": f"M{i}", "annotation": "После"} for i in range(250)]

        class _Cur:
            role = ConfigRole.EXTENSION
            name = "РасшБольшое"
            path = "/ext/path"

        class _Ctx:
            current = _Cur()
            nearby_extensions = []

        monkeypatch.setattr(ed, "detect_extension_context", lambda _p: _Ctx())

        def _find(_p, _o=None, diagnostics=None):
            if diagnostics is not None:
                diagnostics.update({"complete": True})
            return list(big)

        monkeypatch.setattr(ed, "find_extension_overrides", _find)
        bsl = _make_bsl_with_reader(None)
        res = bsl["get_overrides"]()
        assert res["source"] == "live"
        assert res["total"] == 250
        assert len(res["overrides"]) == 200
        assert res["truncated"] is True
        assert res["partial"] is False

    def test_live_main_scan_failure_is_explicitly_partial(self, monkeypatch):
        import rlm_tools_bsl.extension_detector as ed
        from rlm_tools_bsl.extension_detector import ConfigRole

        class _Cur:
            role = ConfigRole.MAIN

        class _Ext:
            def __init__(self, name, path):
                self.name = name
                self.path = path

        class _Ctx:
            current = _Cur()
            nearby_extensions = [_Ext("РасшA", "/ext/a"), _Ext("РасшB", "/ext/b")]

        def _find(path, _object=None, diagnostics=None):
            if path == "/ext/b":
                raise PermissionError("denied")
            diagnostics.update(
                {
                    "root": path,
                    "root_available": True,
                    "complete": True,
                    "candidate_files": 1,
                    "files_scanned": 1,
                    "unreadable_files": [],
                    "walk_errors": [],
                }
            )
            return [{"object_name": "Док", "target_method": "ПриЗаписи", "annotation": "После"}]

        monkeypatch.setattr(ed, "detect_extension_context", lambda _p: _Ctx())
        monkeypatch.setattr(ed, "find_extension_overrides", _find)
        res = _make_bsl_with_reader(None)["get_overrides"]()

        assert res["source"] == "live", res
        assert res["partial"] is True, res
        assert res["total"] == 1, res
        assert res["unique_extensions"] == 1, res
        assert res["by_extension_top"] == {"РасшA": 1}, res
        assert res["_meta"]["failed_extension_roots"] == [
            {
                "extension_name": "РасшB",
                "extension_root": "/ext/b",
                "error": "PermissionError",
                "message": "denied",
            }
        ], res

    def test_live_main_all_missing_roots_is_unavailable(self, monkeypatch):
        import rlm_tools_bsl.extension_detector as ed
        from rlm_tools_bsl.extension_detector import ConfigRole

        class _Cur:
            role = ConfigRole.MAIN

        class _Ext:
            name = "ИсчезнувшееРасширение"
            path = "/ext/missing"

        class _Ctx:
            current = _Cur()
            nearby_extensions = [_Ext()]

        def _find(path, _object=None, diagnostics=None):
            diagnostics.update(
                {
                    "root": path,
                    "root_available": False,
                    "complete": False,
                    "candidate_files": 0,
                    "files_scanned": 0,
                    "unreadable_files": [],
                    "walk_errors": [],
                }
            )
            return []

        monkeypatch.setattr(ed, "detect_extension_context", lambda _p: _Ctx())
        monkeypatch.setattr(ed, "find_extension_overrides", _find)
        res = _make_bsl_with_reader(None)["get_overrides"]()

        assert res["source"] == "unavailable", res
        assert res["partial"] is True, res
        assert res["total"] == 0, res
        assert res["_meta"]["failed_extension_roots"][0]["diagnostics"]["root_available"] is False

    def test_live_unknown_context_without_extensions_is_unavailable(self, monkeypatch):
        import rlm_tools_bsl.extension_detector as ed
        from rlm_tools_bsl.extension_detector import ConfigRole

        class _Cur:
            role = ConfigRole.UNKNOWN
            name = ""
            path = "/incomplete/current"

        class _Ctx:
            current = _Cur()
            nearby_extensions = []

        monkeypatch.setattr(ed, "detect_extension_context", lambda _p: _Ctx())
        res = _make_bsl_with_reader(None)["get_overrides"]()

        assert res["source"] == "unavailable", res
        assert res["partial"] is True, res
        assert res["total"] == 0, res
        assert res["_meta"]["failed_extension_roots"] == [
            {
                "extension_name": "",
                "extension_root": "/incomplete/current",
                "error": "UnknownConfigRole",
                "message": "Current configuration root could not be classified as main or extension",
            }
        ], res

    def test_live_unknown_context_scans_known_extensions_as_partial_lower_bound(self, monkeypatch):
        import rlm_tools_bsl.extension_detector as ed
        from rlm_tools_bsl.extension_detector import ConfigRole

        class _Cur:
            role = ConfigRole.UNKNOWN
            name = ""
            path = "/incomplete/current"

        class _Ext:
            name = "РаспознанноеРасширение"
            path = "/ext/known"

        class _Ctx:
            current = _Cur()
            nearby_extensions = [_Ext()]

        def _find(path, _object=None, diagnostics=None):
            assert path == "/ext/known"
            diagnostics.update({"complete": True, "files_scanned": 1})
            return [{"object_name": "Док", "target_method": "ПриЗаписи", "annotation": "После"}]

        monkeypatch.setattr(ed, "detect_extension_context", lambda _p: _Ctx())
        monkeypatch.setattr(ed, "find_extension_overrides", _find)
        res = _make_bsl_with_reader(None)["get_overrides"]()

        assert res["source"] == "live", res
        assert res["partial"] is True, res
        assert res["total"] == 1, res
        assert res["overrides"][0]["extension_name"] == "РаспознанноеРасширение", res
        assert res["overrides"][0]["extension_root"] == "/ext/known", res
        assert res["_meta"]["failed_extension_roots"][0]["error"] == "UnknownConfigRole", res

    def test_find_ext_overrides_truncated(self, monkeypatch):
        import rlm_tools_bsl.extension_detector as ed

        big = [{"target_method": f"M{i}", "annotation": "Перед"} for i in range(250)]

        def _find(_p, _o=None, diagnostics=None):
            if diagnostics is not None:
                diagnostics.update({"complete": True})
            return list(big)

        monkeypatch.setattr(ed, "find_extension_overrides", _find)
        bsl = _make_bsl_with_reader(None)
        res = bsl["find_ext_overrides"]("/ext/path")
        assert res["total"] == 250
        assert len(res["overrides"]) == 200
        assert res["truncated"] is True
        assert res["partial"] is False

    def test_find_ext_overrides_not_truncated(self, monkeypatch):
        import rlm_tools_bsl.extension_detector as ed

        small = [{"target_method": f"M{i}", "annotation": "Перед"} for i in range(5)]

        def _find(_p, _o=None, diagnostics=None):
            if diagnostics is not None:
                diagnostics.update({"complete": False, "unreadable_files": ["Module.bsl"]})
            return list(small)

        monkeypatch.setattr(ed, "find_extension_overrides", _find)
        bsl = _make_bsl_with_reader(None)
        res = bsl["find_ext_overrides"]("/ext/path")
        assert res["total"] == 5
        assert res["truncated"] is False
        assert res["partial"] is True
        assert res["_meta"]["scan_diagnostics"]["unreadable_files"] == ["Module.bsl"]


# ---------------------------------------------------------------------------
# IndexReader methods
# ---------------------------------------------------------------------------


class TestIndexReaderOverrides:
    @pytest.fixture()
    def reader(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
        cf, _cfe = _make_main_with_extension(tmp_path)
        builder = IndexBuilder()
        db_path = builder.build(cf, build_calls=False, build_metadata=False, build_fts=False)
        r = IndexReader(db_path)
        yield r
        r.close()

    def test_get_extension_overrides_all(self, reader):
        result = reader.get_extension_overrides()
        assert result is not None
        assert len(result) == 2

    def test_get_extension_overrides_by_object(self, reader):
        result = reader.get_extension_overrides(object_name="Номенклатура")
        assert result is not None
        assert len(result) == 2

    def test_get_extension_overrides_by_method(self, reader):
        result = reader.get_extension_overrides(method_name="ОбработкаЗаполнения")
        assert result is not None
        assert len(result) == 1
        assert result[0]["annotation"] == "После"

    def test_get_extension_overrides_empty_filter(self, reader):
        result = reader.get_extension_overrides(object_name="НесуществующийОбъект")
        assert result is not None
        assert len(result) == 0

    def test_get_overrides_for_path(self, reader):
        result = reader.get_overrides_for_path("Catalogs/Номенклатура/Ext/ObjectModule.bsl")
        assert isinstance(result, dict)
        assert "ОбработкаЗаполнения" in result
        assert "ПередЗаписью" in result
        assert len(result["ОбработкаЗаполнения"]) == 1

    def test_get_overrides_for_path_no_match(self, reader):
        result = reader.get_overrides_for_path("NonExistent/Path.bsl")
        assert result == {}

    def test_get_extension_overrides_grouped(self, reader, tmp_path):
        cf = str(tmp_path / "src" / "cf")
        result = reader.get_extension_overrides_grouped(base_path=cf)
        assert result is not None
        assert len(result) > 0
        # All overrides grouped under extension_root key
        total = sum(len(v) for v in result.values())
        assert total == 2

    def test_statistics_includes_overrides(self, reader):
        stats = reader.get_statistics()
        assert "extension_overrides" in stats
        assert stats["extension_overrides"] == 2


# ---------------------------------------------------------------------------
# Backward compatibility: pre-v9 index
# ---------------------------------------------------------------------------


class TestBackwardCompatibility:
    def test_reader_methods_return_none_on_missing_table(self, tmp_path, monkeypatch):
        """Pre-v9 index without extension_overrides table: methods return None/{}."""
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
        cf = str(tmp_path / "cf")
        _write(os.path.join(cf, "Configuration.xml"), _CF_MAIN_XML)
        _write(os.path.join(cf, "CommonModules", "Test", "Ext", "Module.bsl"), "")

        builder = IndexBuilder()
        db_path = builder.build(cf, build_calls=False, build_metadata=False, build_fts=False)

        # Simulate pre-v9: drop the table
        conn = sqlite3.connect(str(db_path))
        conn.execute("DROP TABLE IF EXISTS extension_overrides")
        conn.commit()
        conn.close()

        reader = IndexReader(db_path)
        assert reader.get_extension_overrides() is None
        assert reader.get_overrides_for_path("any/path.bsl") == {}
        assert reader.get_extension_overrides_grouped() is None
        reader.close()


# ---------------------------------------------------------------------------
# Update (incremental)
# ---------------------------------------------------------------------------


class TestUpdateOverrides:
    def test_update_refreshes_overrides(self, tmp_path, monkeypatch):
        """update() refreshes extension_overrides table."""
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
        cf, cfe = _make_main_with_extension(tmp_path)

        builder = IndexBuilder()
        builder.build(cf, build_calls=False, build_metadata=False, build_fts=False)

        # Update (no file changes, but overrides re-scanned)
        builder.update(cf)

        # Verify overrides still present
        from rlm_tools_bsl.bsl_index import get_index_db_path

        db_path = get_index_db_path(cf)
        conn = sqlite3.connect(str(db_path))
        count = conn.execute("SELECT COUNT(*) FROM extension_overrides").fetchone()[0]
        conn.close()
        assert count == 2

    def test_update_soft_upgrade_from_v8(self, tmp_path, monkeypatch):
        """update() creates extension_overrides table on v8 index."""
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
        cf = str(tmp_path / "cf")
        _write(os.path.join(cf, "Configuration.xml"), _CF_MAIN_XML)
        _write(os.path.join(cf, "CommonModules", "Test", "Ext", "Module.bsl"), "")

        builder = IndexBuilder()
        db_path = builder.build(cf, build_calls=False, build_metadata=False, build_fts=False)

        # Simulate v8: drop table
        conn = sqlite3.connect(str(db_path))
        conn.execute("DROP TABLE IF EXISTS extension_overrides")
        conn.commit()
        conn.close()

        # update() should re-create it
        builder.update(cf)

        conn = sqlite3.connect(str(db_path))
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        conn.close()
        assert "extension_overrides" in tables


# ---------------------------------------------------------------------------
# Self-mapping: extension grouped key
# ---------------------------------------------------------------------------


class TestSelfMapping:
    def test_extension_grouped_self_key(self, tmp_path, monkeypatch):
        """For extension config, extension_root == base_path -> key 'self'."""
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
        ext_dir = _make_extension_only(tmp_path)

        builder = IndexBuilder()
        db_path = builder.build(ext_dir, build_calls=False, build_metadata=False, build_fts=False)

        reader = IndexReader(db_path)
        grouped = reader.get_extension_overrides_grouped(base_path=ext_dir)
        reader.close()

        assert grouped is not None
        assert "self" in grouped
        assert len(grouped["self"]) == 2


# ---------------------------------------------------------------------------
# Fix 1: case-insensitive Cyrillic matching
# ---------------------------------------------------------------------------


_EXT_MODULE_LOWERCASE = textwrap.dedent("""\
    &После("обработказаполнения")
    Процедура мр_ОбработкаЗаполнения(ДанныеЗаполнения, СтандартнаяОбработка)
        // расширенная логика
    КонецПроцедуры
""")


class TestCaseInsensitiveCyrillic:
    def test_method_line_found_with_case_mismatch(self, tmp_path, monkeypatch):
        """target_method_line resolved even when annotation has different case."""
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
        cf = os.path.join(str(tmp_path), "src", "cf")
        cfe = os.path.join(str(tmp_path), "src", "cfe", "ТестовоеРасширение")

        _write(os.path.join(cf, "Configuration.xml"), _CF_MAIN_XML)
        _write(
            os.path.join(cf, "Catalogs", "Номенклатура", "Ext", "ObjectModule.bsl"),
            _MAIN_MODULE_BSL,
        )
        _write(os.path.join(cfe, "Configuration.xml"), _cf_extension_xml())
        _write(
            os.path.join(cfe, "Catalogs", "Номенклатура", "Ext", "ObjectModule.bsl"),
            _EXT_MODULE_LOWERCASE,
        )

        builder = IndexBuilder()
        db_path = builder.build(cf, build_calls=False, build_metadata=False, build_fts=False)

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM extension_overrides").fetchall()
        conn.close()

        assert len(rows) == 1
        # target_method_line must be found despite case mismatch
        assert rows[0]["target_method_line"] is not None
        assert rows[0]["target_method_line"] > 0

    def test_reader_filter_case_insensitive(self, tmp_path, monkeypatch):
        """get_extension_overrides filters case-insensitively for Cyrillic."""
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
        cf, _cfe = _make_main_with_extension(tmp_path)
        builder = IndexBuilder()
        db_path = builder.build(cf, build_calls=False, build_metadata=False, build_fts=False)

        reader = IndexReader(db_path)
        # Query with different case
        result = reader.get_extension_overrides(method_name="обработказаполнения")
        reader.close()

        assert result is not None
        assert len(result) == 1
        assert result[0]["target_method"] == "ОбработкаЗаполнения"

    def test_overrides_for_path_case_insensitive(self, tmp_path, monkeypatch):
        """get_overrides_for_path groups are matched case-insensitively by extract_procedures."""
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
        cf = os.path.join(str(tmp_path), "src", "cf")
        cfe = os.path.join(str(tmp_path), "src", "cfe", "ТестовоеРасширение")

        _write(os.path.join(cf, "Configuration.xml"), _CF_MAIN_XML)
        _write(
            os.path.join(cf, "Catalogs", "Номенклатура", "Ext", "ObjectModule.bsl"),
            _MAIN_MODULE_BSL,
        )
        _write(os.path.join(cfe, "Configuration.xml"), _cf_extension_xml())
        _write(
            os.path.join(cfe, "Catalogs", "Номенклатура", "Ext", "ObjectModule.bsl"),
            _EXT_MODULE_LOWERCASE,
        )

        builder = IndexBuilder()
        db_path = builder.build(cf, build_calls=False, build_metadata=False, build_fts=False)

        reader = IndexReader(db_path)
        result = reader.get_overrides_for_path("Catalogs/Номенклатура/Ext/ObjectModule.bsl")
        reader.close()

        # Key in the dict is "обработказаполнения" (from annotation), but
        # extract_procedures uses .lower() matching, so it should still work
        assert len(result) == 1
        # The single key should contain the override
        all_overrides = [ov for ovs in result.values() for ov in ovs]
        assert len(all_overrides) == 1


_EXT_PLAIN_BSL = textwrap.dedent("""\
    Процедура мр_ОбычныйМетод() Экспорт
        // никаких &После/&Вместо — нет перехватов
    КонецПроцедуры
""")


def _bsl_for(base_path, reader):
    from rlm_tools_bsl.bsl_helpers import make_bsl_helpers
    from rlm_tools_bsl.format_detector import detect_format
    from rlm_tools_bsl.helpers import make_helpers

    helpers, resolve_safe = make_helpers(base_path, idx_reader=reader)
    fmt = detect_format(base_path)
    return make_bsl_helpers(
        base_path=base_path,
        resolve_safe=resolve_safe,
        read_file_fn=helpers["read_file"],
        grep_fn=helpers["grep"],
        glob_files_fn=helpers["glob_files"],
        format_info=fmt,
        idx_reader=reader,
    )


class TestCountOverridesByExtensionRoot:
    """v1.24.0 #7 — IndexReader.count_overrides_by_extension_root + detect_extensions.overrides_count."""

    def test_index_reader_groups_by_root(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
        cf, cfe = _make_main_with_extension(tmp_path)
        builder = IndexBuilder()
        db_path = builder.build(cf, build_calls=False, build_metadata=False, build_fts=False)
        reader = IndexReader(db_path)
        try:
            counts = reader.count_overrides_by_extension_root()
            assert counts is not None
            assert counts.get(cfe) == 2
        finally:
            reader.close()

    def test_index_reader_none_when_table_missing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
        cf = str(tmp_path / "cf")
        _write(os.path.join(cf, "Configuration.xml"), _CF_MAIN_XML)
        _write(os.path.join(cf, "CommonModules", "Test", "Ext", "Module.bsl"), "")
        builder = IndexBuilder()
        db_path = builder.build(cf, build_calls=False, build_metadata=False, build_fts=False)
        conn = sqlite3.connect(str(db_path))
        conn.execute("DROP TABLE IF EXISTS extension_overrides")
        conn.commit()
        conn.close()
        reader = IndexReader(db_path)
        try:
            assert reader.count_overrides_by_extension_root() is None
        finally:
            reader.close()

    def test_detect_main_session_reports_count(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
        cf, cfe = _make_main_with_extension(tmp_path)
        builder = IndexBuilder()
        db_path = builder.build(cf, build_calls=False, build_metadata=False, build_fts=False)
        reader = IndexReader(db_path)
        try:
            bsl = _bsl_for(cf, reader)
            ctx = bsl["detect_extensions"]()
            assert ctx["config_role"] == "main"
            nearby = ctx["nearby_extensions"]
            assert nearby, ctx
            # match by normalized path, NOT index i
            import os as _os

            def _norm(p):
                return _os.path.normcase(_os.path.normpath(_os.path.abspath(p)))

            by_path = {_norm(e["path"]): e for e in nearby}
            assert by_path[_norm(cfe)]["overrides_count"] == 2
        finally:
            reader.close()

    def test_detect_main_session_zero_when_no_overrides(self, tmp_path, monkeypatch):
        """MAIN + nearby extension WITHOUT overrides + index built → 0 (known zero), not None."""
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
        cf = os.path.join(str(tmp_path), "src", "cf")
        cfe = os.path.join(str(tmp_path), "src", "cfe", "ПустоеРасширение")
        _write(os.path.join(cf, "Configuration.xml"), _CF_MAIN_XML)
        _write(os.path.join(cf, "Catalogs", "Номенклатура", "Ext", "ObjectModule.bsl"), _MAIN_MODULE_BSL)
        _write(os.path.join(cfe, "Configuration.xml"), _cf_extension_xml(name="ПустоеРасширение"))
        _write(os.path.join(cfe, "CommonModules", "мр_Модуль", "Ext", "Module.bsl"), _EXT_PLAIN_BSL)
        builder = IndexBuilder()
        db_path = builder.build(cf, build_calls=False, build_metadata=False, build_fts=False)
        reader = IndexReader(db_path)
        try:
            bsl = _bsl_for(cf, reader)
            ctx = bsl["detect_extensions"]()
            assert ctx["config_role"] == "main"
            nearby = ctx["nearby_extensions"]
            assert nearby, ctx
            assert all(e["overrides_count"] == 0 for e in nearby), nearby
        finally:
            reader.close()

    def test_detect_no_index_none(self, tmp_path):
        cf, _cfe = _make_main_with_extension(tmp_path)
        bsl = _bsl_for(cf, None)
        ctx = bsl["detect_extensions"]()
        nearby = ctx["nearby_extensions"]
        assert nearby, ctx
        assert all(e["overrides_count"] is None for e in nearby), nearby

    def test_detect_extension_session_sibling_is_none(self, tmp_path, monkeypatch):
        """EXTENSION session: index covers only current.path; a sibling extension is
        NOT covered → its overrides_count must be None (unknown), NOT a false 0."""
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
        ext1 = os.path.join(str(tmp_path), "src", "cfe", "Расширение1")
        ext2 = os.path.join(str(tmp_path), "src", "cfe", "Расширение2")
        # ext1 has real overrides (needs a main object to override → still records rows)
        _write(os.path.join(ext1, "Configuration.xml"), _cf_extension_xml(name="Расширение1"))
        _write(os.path.join(ext1, "Catalogs", "Номенклатура", "Ext", "ObjectModule.bsl"), _EXT_MODULE_BSL)
        # ext2 sibling, plain
        _write(os.path.join(ext2, "Configuration.xml"), _cf_extension_xml(name="Расширение2"))
        _write(os.path.join(ext2, "CommonModules", "мр_Модуль", "Ext", "Module.bsl"), _EXT_PLAIN_BSL)
        builder = IndexBuilder()
        db_path = builder.build(ext1, build_calls=False, build_metadata=False, build_fts=False)
        reader = IndexReader(db_path)
        try:
            bsl = _bsl_for(ext1, reader)
            ctx = bsl["detect_extensions"]()
            assert ctx["config_role"] == "extension"
            nearby = ctx["nearby_extensions"]
            # the sibling ext2 must be among nearby and report None (not covered by index)
            sib = [e for e in nearby if "Расширение2" in e["path"]]
            assert sib, ctx
            assert all(e["overrides_count"] is None for e in sib), sib
        finally:
            reader.close()


# ---------------------------------------------------------------------------
# Fix 3: early-exit build without BSL writes meta keys
# ---------------------------------------------------------------------------


class TestEarlyExitMeta:
    def test_build_no_bsl_writes_override_meta(self, tmp_path, monkeypatch):
        """Build with no .bsl files still writes has_extension_overrides meta."""
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
        cf = str(tmp_path / "cf")
        # Only Configuration.xml, no BSL files
        _write(os.path.join(cf, "Configuration.xml"), _CF_MAIN_XML)

        builder = IndexBuilder()
        db_path = builder.build(cf, build_calls=False, build_metadata=False, build_fts=False)

        conn = sqlite3.connect(str(db_path))
        has = conn.execute("SELECT value FROM index_meta WHERE key='has_extension_overrides'").fetchone()
        count = conn.execute("SELECT value FROM index_meta WHERE key='extension_overrides_count'").fetchone()
        conn.close()

        assert has is not None, "has_extension_overrides must be in meta"
        assert has[0] == "0"
        assert count is not None, "extension_overrides_count must be in meta"
        assert count[0] == "0"


# ===========================================================================
# v1.28.0 — агрегаты get_overrides по ПОЛНОМУ набору + единый shape
# ===========================================================================


def _make_overrides_env(tmp_path, rows):
    """rows: list[(object_name, target_method, annotation, extension_name)] → (bsl, reader)."""
    from rlm_tools_bsl.bsl_helpers import make_bsl_helpers
    from rlm_tools_bsl.format_detector import detect_format
    from rlm_tools_bsl.helpers import make_helpers

    tmp_path.mkdir(parents=True, exist_ok=True)  # фикстуру зовут и с подкаталогом (tmp_path/"reversed")
    (tmp_path / "Configuration.xml").write_text("<Configuration/>", encoding="utf-8")
    db = IndexBuilder().build(str(tmp_path), build_calls=False, build_metadata=True)
    conn = sqlite3.connect(str(db))
    # ext_module_path — NOT NULL без DEFAULT (см. схему), пропуск дал бы IntegrityError.
    conn.executemany(
        "INSERT INTO extension_overrides (object_name, target_method, annotation, "
        "extension_name, extension_method, extension_root, source_path, ext_module_path) "
        "VALUES (?, ?, ?, ?, 'ext_' || ?, 'root', 'p.bsl', 'ext.bsl')",
        [(o, m, a, e, m) for o, m, a, e in rows],
    )
    conn.commit()
    conn.close()
    reader = IndexReader(str(db))
    helpers, resolve_safe = make_helpers(str(tmp_path))
    bsl = make_bsl_helpers(
        base_path=str(tmp_path),
        resolve_safe=resolve_safe,
        read_file_fn=helpers["read_file"],
        grep_fn=helpers["grep"],
        glob_files_fn=helpers["glob_files"],
        format_info=detect_format(str(tmp_path)),
        idx_reader=reader,
    )
    return bsl, reader


def test_get_overrides_aggregates_cover_rows_invisible_in_the_slice(tmp_path):
    """Агрегаты обязаны видеть строку, которой НЕТ в срезе 200.

    Имя 'ЯРедкийОбъект' выбрано намеренно: после сортировки среза по (object, ...) он
    лексикографически ПОСЛЕДНИЙ (Я > Ш) и в первые 200 не попадает. Если бы агрегаты
    считались по срезу (старое поведение) — его бы для агента не существовало.
    """
    rows = [("ШумныйОбъект", f"Метод{i:03d}", "После", "ExtA") for i in range(210)]
    rows += [("ЯРедкийОбъект", f"Метод{i:03d}", "Вместо", "ExtB") for i in range(5)]
    bsl, reader = _make_overrides_env(tmp_path, rows)
    try:
        res = bsl["get_overrides"]()
        assert res["total"] == 215
        assert res["truncated"] is True
        assert len(res["overrides"]) == 200
        # Ключевое: строки НЕТ в срезе, но она ЕСТЬ в агрегате
        assert "ЯРедкийОбъект" not in {o["object_name"] for o in res["overrides"]}
        assert res["by_object_top"]["ЯРедкийОбъект"] == 5
        assert res["by_object_top"]["ШумныйОбъект"] == 210
        assert res["by_annotation"] == {"После": 210, "Вместо": 5}
        assert res["by_extension_top"] == {"ExtA": 210, "ExtB": 5}
        assert res["unique_objects"] == 2
        assert res["unique_extensions"] == 2
        # unique_methods имеет ЗАЯВЛЕННУЮ семантику (уникальные target_method,
        # case-insensitive). Метод000..Метод209 у ШумныйОбъект + Метод000..Метод004 у
        # ЯРедкийОбъект (имена ПЕРЕСЕКАЮТСЯ) → ровно 210.
        assert res["unique_methods"] == 210, res["unique_methods"]
        # Срез детерминирован между вызовами
        again = bsl["get_overrides"]()
        assert [(o["object_name"], o["target_method"]) for o in res["overrides"]] == [
            (o["object_name"], o["target_method"]) for o in again["overrides"]
        ]
    finally:
        reader.close()


def test_get_overrides_aggregates_are_case_insensitive(tmp_path):
    """Имена объектов/расширений в 1С регистронезависимы, и фильтры reader'а сравнивают
    через .lower(). Агрегаты обязаны следовать той же семантике, иначе «Объект» и «объект»
    дадут два элемента by_object_top и unique_objects=2 — агрегат разойдётся с фильтрами
    того же API."""
    rows = [
        ("Номенклатура", "ПередЗаписью", "После", "ExtA"),
        ("номенклатура", "ПриЗаписи", "После", "extA"),
    ]
    bsl, reader = _make_overrides_env(tmp_path, rows)
    try:
        res = bsl["get_overrides"]()
        assert res["unique_objects"] == 1, res["by_object_top"]
        assert res["unique_extensions"] == 1, res["by_extension_top"]
        assert sum(res["by_object_top"].values()) == 2
        assert len(res["by_object_top"]) == 1  # одна запись, а не две
        # Сверка с фильтром того же API: он тоже регистронезависим
        assert bsl["get_overrides"]("НОМЕНКЛАТУРА")["total"] == 2
    finally:
        reader.close()

    # display-name ДЕТЕРМИНИРОВАН: обратный порядок вставки даёт тот же ключ.
    bsl2, reader2 = _make_overrides_env(tmp_path / "reversed", list(reversed(rows)))
    try:
        assert list(bsl2["get_overrides"]()["by_object_top"]) == list(res["by_object_top"]), (
            "ключ by_object_top зависит от порядка выдачи SQLite"
        )
    finally:
        reader2.close()


def test_get_overrides_unavailable_has_same_shape(tmp_path, monkeypatch):
    """Три ветки (index/live/unavailable) — ОДИН shape, иначе агент, написавший
    res['by_object_top'], падает на конфигурации без расширений."""
    import rlm_tools_bsl.extension_detector as ed

    def _boom(_path):
        raise RuntimeError("boom")

    monkeypatch.setattr(ed, "detect_extension_context", _boom)
    bsl = _make_bsl_with_reader(None)  # без idx_reader → live-ветка → _det падает
    res = bsl["get_overrides"]()
    assert res["source"] == "unavailable"
    assert res["partial"] is True
    for key in ("by_annotation", "by_object_top", "by_extension_top"):
        assert res[key] == {}, key
    for key in ("unique_objects", "unique_methods", "unique_extensions", "total"):
        assert res[key] == 0, key
    assert res["overrides"] == [] and res["truncated"] is False


# ── v1.30.0 (пакет 5): единый additive shape перехватов ───────

_UNIFIED_OVERRIDE_KEYS = {
    "object_name",
    "target_method",
    "annotation",
    "extension_name",
    "extension_method",
    "extension_root",
    "ext_module_path",
    "ext_line",
    "module_path",
    "module_type",
    "line",
    "source_path",
    "source_module_id",
    "target_method_line",
}


def test_override_shape_is_unified_across_index_and_find_ext(tmp_path, monkeypatch):
    """Индексная строка несла ext_module_path/ext_line/source_*, а live-строка
    find_ext_overrides — module_path/line/module_type. Код агента, переиспользующий одну
    обработку на обеих ветках, падал. Теперь обязательный набор ключей одинаков, алиасы
    достроены в ОБЕ стороны, старые поля на месте."""
    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
    cf, cfe = _make_main_with_extension(str(tmp_path))
    db_path = IndexBuilder().build(cf, build_calls=False, build_metadata=True)
    reader = IndexReader(db_path)
    try:
        bsl = _bsl_for(cf, reader)
        indexed = bsl["get_overrides"]()
        live = bsl["find_ext_overrides"](cfe)
        assert indexed["overrides"], indexed
        assert live["overrides"], live

        for row in indexed["overrides"] + live["overrides"]:
            missing = _UNIFIED_OVERRIDE_KEYS - set(row)
            assert not missing, sorted(missing)
            assert row["module_path"] == row["ext_module_path"]
            assert row["line"] == row["ext_line"]
            assert row["module_type"] == "ObjectModule"
        # исторические индексные поля не потеряны
        assert "id" in indexed["overrides"][0]
        assert "extension_purpose" in indexed["overrides"][0]
    finally:
        reader.close()


class _ManyOverridesReader:
    """Reader-заглушка: отдаёт заданные СЫРЫЕ строки extension_overrides."""

    def __init__(self, rows):
        self._rows = rows

    def get_extension_overrides(self, object_name="", method_name=""):
        return [dict(r) for r in self._rows]


def _raw_override(i):
    return {
        "id": i,
        "object_name": "Док%02d" % (i % 7),
        "source_path": "Documents/Док/Ext/ObjectModule.bsl",
        "source_module_id": i,
        "target_method": "Метод%02d" % (i % 5),
        "target_method_line": None,
        "annotation": "После",
        "extension_name": "Расш",
        "extension_purpose": "Customization",
        "extension_method": "",
        "extension_root": "/ext",
        "ext_module_path": "Documents/Док/Ext/ObjectModule.bsl",
        "ext_line": i,
    }


def test_cap_200_slice_and_order_survive_additive_normalization(tmp_path):
    """У `_sort_key` последний элемент — json.dumps ВСЕЙ строки, поэтому новые ключи
    сдвинули бы tie-break, а с ним и СОСТАВ среза cap=200. Ключ обязан считаться по
    НЕТРОНУТОЙ строке (до нормализации), иначе часть перехватов молча уезжает из
    видимых агенту 200."""
    import json as _json

    from rlm_tools_bsl.bsl_helpers import make_bsl_helpers
    from rlm_tools_bsl.format_detector import detect_format
    from rlm_tools_bsl.helpers import make_helpers

    rows = [_raw_override(i) for i in range(260)]
    tmpdir = str(tmp_path)
    (tmp_path / "Configuration.xml").write_text("<Configuration/>", encoding="utf-8")
    helpers, resolve_safe = make_helpers(tmpdir)
    bsl = make_bsl_helpers(
        base_path=tmpdir,
        resolve_safe=resolve_safe,
        read_file_fn=helpers["read_file"],
        grep_fn=helpers["grep"],
        glob_files_fn=helpers["glob_files"],
        format_info=detect_format(tmpdir),
        idx_reader=_ManyOverridesReader(rows),
    )
    res = bsl["get_overrides"]()
    assert res["total"] == 260 and res["truncated"] is True
    assert len(res["overrides"]) == 200

    def legacy_key(r):
        return (
            (r.get("object_name") or "").lower(),
            (r.get("target_method") or "").lower(),
            (r.get("extension_name") or "").lower(),
            (r.get("annotation") or "").lower(),
            (r.get("extension_method") or "").lower(),
            (r.get("source_path") or "").lower(),
            _json.dumps(r, sort_keys=True, ensure_ascii=False, default=str),
        )

    expected_ids = [r["id"] for r in sorted(rows, key=legacy_key)[:200]]
    assert [r["id"] for r in res["overrides"]] == expected_ids
    # служебный sort-token наружу не протекает
    assert all("_sort_token" not in r for r in res["overrides"])


def test_find_ext_overrides_keeps_legacy_walk_order_slice(tmp_path):
    """У find_ext_overrides сортировки среза не было: строки шли в порядке os.walk и
    резались `[:200]`. Нормализация обязана применяться ПОСЛЕ среза — иначе на
    расширении с >200 перехватами поменялся бы САМ НАБОР возвращаемых строк."""
    from rlm_tools_bsl.extension_detector import find_extension_overrides

    cfe = os.path.join(str(tmp_path), "cfe", "БольшоеРасширение")
    _write(os.path.join(cfe, "Configuration.xml"), _cf_extension_xml(name="БольшоеРасширение"))
    body = "\n".join('&После("Метод%03d")\nПроцедура мр_П%03d()\nКонецПроцедуры\n' % (i, i) for i in range(210))
    _write(os.path.join(cfe, "Catalogs", "Номенклатура", "Ext", "ObjectModule.bsl"), body)

    cf = os.path.join(str(tmp_path), "cf")
    _write(os.path.join(cf, "Configuration.xml"), _CF_MAIN_XML)
    bsl = _bsl_for(cf, None)

    raw = find_extension_overrides(cfe, None, diagnostics={})
    res = bsl["find_ext_overrides"](cfe)
    assert res["total"] == len(raw) > 200
    assert res["truncated"] is True
    assert [(r["target_method"], r["line"]) for r in res["overrides"]] == [
        (r["target_method"], r["line"]) for r in raw[:200]
    ]
    assert _UNIFIED_OVERRIDE_KEYS <= set(res["overrides"][0])


def test_find_ext_overrides_carries_extension_provenance(tmp_path, monkeypatch):
    """Единый shape обязан быть совместим СЕМАНТИЧЕСКИ, а не только по набору ключей.

    Сырые live-строки не несут ни `extension_name`, ни `extension_root`. Если просто
    добить их пустыми значениями, ключ появится, но выдача по нескольким расширениям
    схлопнется под одним пустым именем — тогда как `get_overrides` для тех же строк даёт
    настоящее имя из метаданных расширения.
    """
    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
    cf, cfe = _make_main_with_extension(str(tmp_path))
    db_path = IndexBuilder().build(cf, build_calls=False, build_metadata=True)
    reader = IndexReader(db_path)
    try:
        bsl = _bsl_for(cf, reader)
        live_rows = bsl["find_ext_overrides"](cfe)["overrides"]
        index_rows = bsl["get_overrides"]()["overrides"]
        assert live_rows and index_rows

        index_names = {r["extension_name"] for r in index_rows}
        assert index_names == {"ТестовоеРасширение"}
        for row in live_rows:
            assert row["extension_name"] == "ТестовоеРасширение"
            assert row["extension_root"] == cfe
    finally:
        reader.close()


# ── v1.33.0: read-time разбор CFE в паритете с билдером ─────────────────────
#
# АДРЕС ФИКСА. Правка лежит в `_read_procedure_one` (bsl_helpers). `get_overrides`
# этот код НЕ вызывает и отдаёт ключ `overrides` (не `items`), поля `extension_body`
# в нём нет вовсе — тест через него был бы вакуумно зелёным. Единственный публичный
# путь: `read_procedure(..., include_overrides=True)`.
#
# ЧТО ИМЕННО ДИСКРИМИНИРУЕТ МАСКУ. Закомментированное объявление `// Процедура X(...)`
# на этой точке отсекается уже ЯКОРЕМ в `procedure_def`, поэтому ghost-тест по
# комментарию зелен и БЕЗ маски — он ничего не доказывает. Маска здесь нужна для
# двух других вещей, они и проверяются:
#   1) многострочная сигнатура перехвата (30 из 799 живых перехватов на боевых);
#   2) фальшивый `КонецПроцедуры` ВНУТРИ строкового литерала, который обрывает тело.

_MAIN_REL = "Catalogs/Номенклатура/Ext/ObjectModule.bsl"
_MAIN_BSL = "Процедура ПередЗаписью(Отказ)\n    // основная логика\nКонецПроцедуры\n"

_EXT_MULTILINE = (
    '&Вместо("ПередЗаписью")\n'
    "Процедура мр_ПередЗаписью(Отказ,\n"
    "    ДопПараметр) Экспорт\n"
    "    // МАРКЕР_НАСТОЯЩЕГО_ТЕЛА\n"
    "КонецПроцедуры\n"
)

_EXT_FAKE_END = (
    '&Вместо("ПередЗаписью")\n'
    "Процедура мр_ПередЗаписью(Отказ) Экспорт\n"
    '    Т = "\n'
    "КонецПроцедуры\n"
    '    ";\n'
    "    // МАРКЕР_НАСТОЯЩЕГО_ТЕЛА\n"
    "КонецПроцедуры\n"
)

_EXT_SINGLE_LINE = (
    '&Вместо("ПередЗаписью")\nПроцедура мр_ПередЗаписью(Отказ) Экспорт\n    // МАРКЕР_НАСТОЯЩЕГО_ТЕЛА\nКонецПроцедуры\n'
)


def _ext_env(tmp_path, ext_bsl, main_bsl=_MAIN_BSL):
    """Дерево main+CFE поверх существующих хелперов файла.

    `_make_main_with_extension` и `_write` уже есть; `_bsl_for` возвращает ОДИН dict —
    распаковывать его в `(bsl, reader)` нельзя.
    """
    cf, cfe = _make_main_with_extension(tmp_path)
    _write(os.path.join(cf, "Catalogs", "Номенклатура", "Ext", "ObjectModule.bsl"), main_bsl)
    _write(os.path.join(cfe, "Catalogs", "Номенклатура", "Ext", "ObjectModule.bsl"), ext_bsl)
    db_path = IndexBuilder().build(cf, build_calls=False, build_metadata=False, build_fts=False)
    return cf, cfe, IndexReader(db_path)


def test_multiline_interceptor_body_is_found(tmp_path, monkeypatch):
    """Полный `procedure_def` требует закрывающую ')', которой на первой строке
    многострочной сигнатуры нет. Построчный поиск теряет такие тела целиком —
    на боевых расширениях это 30 перехватов из 799."""
    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
    cf, _cfe, reader = _ext_env(tmp_path, _EXT_MULTILINE)
    try:
        bsl = _bsl_for(cf, reader)
        text = bsl["read_procedure"](_MAIN_REL, "ПередЗаписью", include_overrides=True)
        assert text, "основная процедура не прочиталась — тест ничего не проверяет"
        assert "МАРКЕР_НАСТОЯЩЕГО_ТЕЛА" in text, f"тело многострочного перехвата не найдено: {text!r}"
        assert "ДопПараметр" in text, "тело начато не с первой строки сигнатуры"
    finally:
        reader.close()


def test_body_not_truncated_by_end_inside_string_literal(tmp_path, monkeypatch):
    """`КонецПроцедуры` внутри строкового литерала не должен обрывать тело.
    Это и есть то, ради чего поиск конца переведён на маску."""
    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
    cf, _cfe, reader = _ext_env(tmp_path, _EXT_FAKE_END)
    try:
        bsl = _bsl_for(cf, reader)
        text = bsl["read_procedure"](_MAIN_REL, "ПередЗаписью", include_overrides=True)
        assert text, "основная процедура не прочиталась"
        assert "МАРКЕР_НАСТОЯЩЕГО_ТЕЛА" in text, "тело оборвано фальшивым КонецПроцедуры из литерала"
    finally:
        reader.close()


def test_single_line_interceptor_still_found(tmp_path, monkeypatch):
    """Регресс-гард: обычный однострочный перехват обязан работать как раньше."""
    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
    cf, _cfe, reader = _ext_env(tmp_path, _EXT_SINGLE_LINE)
    try:
        bsl = _bsl_for(cf, reader)
        text = bsl["read_procedure"](_MAIN_REL, "ПередЗаписью", include_overrides=True)
        assert "МАРКЕР_НАСТОЯЩЕГО_ТЕЛА" in text
    finally:
        reader.close()


def test_extract_procedures_read_time_ignores_ghosts(tmp_path, monkeypatch):
    """read-time разбор модуля РАСШИРЕНИЯ обязан вести себя как билдер.

    `extract_procedures` — публичная обёртка над вложенной `_parse_procedures`,
    которую правит Шаг 4; импортом та недостижима.
    """
    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
    ghost = (
        "// Пример:\n"
        "//   Процедура Призрак(А) Экспорт\n"
        "//   КонецПроцедуры\n"
        'Шаблон = "Процедура ПризракИзСтроки(Б) Экспорт";\n'
        'Процедура Настоящая(Знач В = ", ", Г) Экспорт\n'
        "КонецПроцедуры\n"
    )
    cf, cfe, reader = _ext_env(tmp_path, ghost)
    try:
        bsl = _bsl_for(cfe, None)  # разбор идёт по дереву РАСШИРЕНИЯ, индекс не нужен
        procs = bsl["extract_procedures"]("Catalogs/Номенклатура/Ext/ObjectModule.bsl")
        assert procs, "модуль не разобрался — тест ничего не проверяет"
        assert [p["name"] for p in procs] == ["Настоящая"], procs
        assert isinstance(procs[0]["params"], list), procs[0]
        assert procs[0]["params"] == ["В", "Г"], procs[0]
    finally:
        reader.close()
