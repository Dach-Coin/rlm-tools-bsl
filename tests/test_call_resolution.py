"""Tests for the call-graph fix (Phase A) and callee resolver (Phase B).

Phase A — _extract_calls_from_body now uses the multi-line-aware _scan_module,
so query-language functions inside multi-line string literals no longer leak in
as false call edges.

Phase B — each call edge carries a stable callee_key ("<rel_path>::<method>")
for the two safe resolution tiers (local + common-exported); IndexReader.get_callers
and find_call_hierarchy expose an exact (resolved) mode with explicit
exact/fallback markers in _meta.
"""

import re
import sqlite3

import pytest

from rlm_tools_bsl.bsl_index import (
    BUILDER_VERSION,
    IndexBuilder,
    IndexReader,
    _callee_match_clause,
    _callee_short_expr,
    _make_callee_key,
    _normalize_module_hint,
)
from rlm_tools_bsl.bsl_helpers import make_bsl_helpers
from rlm_tools_bsl.format_detector import detect_format
from rlm_tools_bsl.helpers import make_helpers


# ---------------------------------------------------------------------------
# Fixture sources
# ---------------------------------------------------------------------------

COMMON_MODULE_BSL = """\
Процедура ОбщийМетод() Экспорт
    Сообщить("ok");
КонецПроцедуры

Функция УникальныйЭкспортныйМетод() Экспорт
    Возврат 1;
КонецФункции

Процедура ЛокальныйХелпер()
    ВнутренняяЛогика();
КонецПроцедуры

Процедура ВнутренняяЛогика()
    Сообщить("common-local");
КонецПроцедуры
"""

COMMON_MODULE2_BSL = """\
Процедура ОбщаяТочка() Экспорт
    Сообщить("tp");
КонецПроцедуры
"""

NAKLADNAYA_OBJECT_BSL = """\
Процедура ОбработкаПроведения(Отказ, Режим)
    ОбщийМодуль.ОбщийМетод();
    Справочники.Контрагенты.НайтиПоКоду("123");
    Контрагент.ПолучитьОбъект();
    ВспомогательныйМетод();
КонецПроцедуры

Процедура ВспомогательныйМетод()
    ОбщийМодуль.УникальныйЭкспортныйМетод();
    ВнутренняяЛогика();
КонецПроцедуры

Процедура ВнутренняяЛогика()
    Сообщить("doc-local");
КонецПроцедуры
"""

ZAKAZ_OBJECT_BSL = """\
Процедура ПередЗаписью(Отказ)
    ОбщийМодуль2.ОбщаяТочка();
    ОбщийМетод();
КонецПроцедуры

Процедура ОбщийМетод()
    Сообщить("order-local");
КонецПроцедуры
"""

SCHET_OBJECT_BSL = """\
Процедура ПередЗаписью(Отказ)
    ОбщийМодуль2.ОбщаяТочка();
КонецПроцедуры
"""

QUERY_MODULE_BSL = """\
Процедура СоБольшимЗапросом()
    Запрос = Новый Запрос;
    Запрос.Текст =
    "ВЫБРАТЬ
    |   ЕСТЬNULL(Т.Сумма, 0) КАК Сумма,
    |   ВЫРАЗИТЬ(Т.Цена КАК ЧИСЛО(15, 2)) КАК Цена,
    |   СУММА(Т.Количество) КАК Количество
    |ИЗ
    |   Документ.Накладная КАК Т";
    РеальныйВызовПослеЗапроса();
КонецПроцедуры

Процедура РеальныйВызовПослеЗапроса()
    Сообщить("real");
КонецПроцедуры

Процедура ТестЭкранирования()
    Текст = "Это ""кавычки"" внутри";
    Путь = "http://x/ВызовВнутриСтроки(";
    РеальныйПослеСтрок();
КонецПроцедуры

Процедура РеальныйПослеСтрок()
    Сообщить("after");
КонецПроцедуры
"""


PROJECT_FILES = {
    "CommonModules/ОбщийМодуль/Ext/Module.bsl": COMMON_MODULE_BSL,
    "CommonModules/ОбщийМодуль2/Ext/Module.bsl": COMMON_MODULE2_BSL,
    "CommonModules/Запросы/Ext/Module.bsl": QUERY_MODULE_BSL,
    "Documents/Накладная/Ext/ObjectModule.bsl": NAKLADNAYA_OBJECT_BSL,
    "Documents/Заказ/Ext/ObjectModule.bsl": ZAKAZ_OBJECT_BSL,
    "Documents/Счет/Ext/ObjectModule.bsl": SCHET_OBJECT_BSL,
}

OBSHIY_PATH = "CommonModules/ОбщийМодуль/Ext/Module.bsl"
OBSHIY2_PATH = "CommonModules/ОбщийМодуль2/Ext/Module.bsl"
NAKLADNAYA_PATH = "Documents/Накладная/Ext/ObjectModule.bsl"
ZAKAZ_PATH = "Documents/Заказ/Ext/ObjectModule.bsl"
QUERY_PATH = "CommonModules/Запросы/Ext/Module.bsl"


def _write_project(root, files):
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8-sig")
    (root / "Configuration.xml").write_text("<Configuration/>", encoding="utf-8")


@pytest.fixture
def project(tmp_path):
    _write_project(tmp_path, PROJECT_FILES)
    return tmp_path


@pytest.fixture
def built(project):
    """Build a full index with calls; return (db_path, base_path)."""
    db_path = IndexBuilder().build(str(project), build_calls=True)
    return db_path, str(project)


def _conn(db_path):
    c = sqlite3.connect(str(db_path))
    c.row_factory = sqlite3.Row
    return c


def _callees(db_path):
    with _conn(db_path) as c:
        return {r["callee_name"] for r in c.execute("SELECT DISTINCT callee_name FROM calls")}


def _make_indexed_helpers(base_path, db_path):
    reader = IndexReader(db_path)
    helpers, resolve_safe = make_helpers(str(base_path))
    fmt = detect_format(str(base_path))
    bsl = make_bsl_helpers(
        base_path=str(base_path),
        resolve_safe=resolve_safe,
        read_file_fn=helpers["read_file"],
        grep_fn=helpers["grep"],
        glob_files_fn=helpers["glob_files"],
        format_info=fmt,
        idx_reader=reader,
        # Fresh index → a 0-caller result is authoritative (no FS fallback that
        # would erase the exact/fallback _meta). Mirrors server.py for FRESH.
        idx_zero_callers_authoritative=True,
    )
    return bsl, reader


# ===========================================================================
# Phase A — multi-line-aware call extraction
# ===========================================================================


class TestPhaseAFalseEdges:
    def test_query_functions_not_extracted(self, built):
        db_path, _ = built
        callees = _callees(db_path)
        for fake in ("ЕСТЬNULL", "ВЫРАЗИТЬ", "ЧИСЛО", "СУММА", "ВызовВнутриСтроки"):
            assert fake not in callees, f"query-language token leaked as a call: {fake}"

    def test_real_calls_around_query_present(self, built):
        db_path, _ = built
        callees = _callees(db_path)
        assert "РеальныйВызовПослеЗапроса" in callees
        assert "РеальныйПослеСтрок" in callees

    def test_string_escape_and_inline_comment(self, built):
        # "" escape and a // inside a string literal must not break scanning:
        # ВызовВнутриСтроки( lives inside a string → no edge; the real call after
        # the strings is still captured.
        db_path, _ = built
        callees = _callees(db_path)
        assert "ВызовВнутриСтроки" not in callees
        assert "РеальныйПослеСтрок" in callees

    def test_call_line_number_after_multiline_string(self, built):
        db_path, _ = built
        # Expected 1-based line of the real call inside СоБольшимЗапросом.
        lines = QUERY_MODULE_BSL.splitlines()
        expected = next(i + 1 for i, ln in enumerate(lines) if "РеальныйВызовПослеЗапроса();" in ln)
        with _conn(db_path) as c:
            row = c.execute(
                "SELECT line FROM calls WHERE callee_name = ?",
                ("РеальныйВызовПослеЗапроса",),
            ).fetchone()
        assert row is not None
        assert row["line"] == expected


# ===========================================================================
# Phase B — build-time resolver (callee_key values)
# ===========================================================================


def _edge_key(db_path, caller_rel, callee_name):
    """callee_key for a specific edge (caller module rel_path + callee_name)."""
    with _conn(db_path) as c:
        row = c.execute(
            "SELECT c.callee_key FROM calls c "
            "JOIN methods m ON m.id = c.caller_id "
            "JOIN modules mod ON mod.id = m.module_id "
            "WHERE mod.rel_path = ? AND c.callee_name = ?",
            (caller_rel, callee_name),
        ).fetchone()
    return row["callee_key"] if row else "<<missing>>"


class TestPhaseBResolver:
    def test_common_exported_resolved(self, built):
        db_path, _ = built
        key = _edge_key(db_path, NAKLADNAYA_PATH, "ОбщийМодуль.ОбщийМетод")
        assert key == _make_callee_key(OBSHIY_PATH, "ОбщийМетод")

    def test_local_bare_resolves_to_own_module(self, built):
        db_path, _ = built
        # Both modules call a same-named ВнутренняяЛогика() — each resolves to its
        # OWN module, not to each other.
        nak = _edge_key(db_path, NAKLADNAYA_PATH, "ВнутренняяЛогика")
        com = _edge_key(db_path, OBSHIY_PATH, "ВнутренняяЛогика")
        assert nak == _make_callee_key(NAKLADNAYA_PATH, "ВнутренняяЛогика")
        assert com == _make_callee_key(OBSHIY_PATH, "ВнутренняяЛогика")
        assert nak != com

    def test_object_manager_qualified_is_null(self, built):
        db_path, _ = built
        # Справочники.Контрагенты.НайтиПоКоду → regex keeps last pair
        # "Контрагенты.НайтиПоКоду"; Контрагент.ПолучитьОбъект → variable method.
        # Both are intentionally NOT resolved.
        assert _edge_key(db_path, NAKLADNAYA_PATH, "Контрагенты.НайтиПоКоду") is None
        assert _edge_key(db_path, NAKLADNAYA_PATH, "Контрагент.ПолучитьОбъект") is None

    def test_platform_global_noise_filtered_out(self, built):
        db_path, _ = built
        # Сообщить() is a reserved platform global — as of the noise filter it is
        # dropped at extraction, so it never reaches the calls table at all (it
        # used to land as an unresolved NULL edge). _edge_key returns the missing
        # sentinel, NOT None. The dedicated coverage lives in
        # tests/test_calls_noise_filter.py.
        assert "Сообщить" not in _callees(db_path)
        assert _edge_key(db_path, OBSHIY_PATH, "Сообщить") == "<<missing>>"

    def test_resolution_stats_written(self, built):
        db_path, _ = built
        with _conn(db_path) as c:
            total = c.execute("SELECT value FROM index_meta WHERE key='calls_total'").fetchone()
            resolved = c.execute("SELECT value FROM index_meta WHERE key='calls_resolved'").fetchone()
            db_total = c.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
            db_resolved = c.execute("SELECT COUNT(callee_key) FROM calls").fetchone()[0]
        assert total is not None and int(total["value"]) == db_total
        assert resolved is not None and int(resolved["value"]) == db_resolved
        assert db_resolved > 0


# ===========================================================================
# Phase B — get_callers exact mode
# ===========================================================================


class TestGetCallersExact:
    def test_unique_common_exported_exact(self, built):
        db_path, _ = built
        reader = IndexReader(db_path)
        try:
            res = reader.get_callers("УникальныйЭкспортныйМетод")
            meta = res["_meta"]
            assert meta["exact_available"] is True
            assert meta["target_exact"] is True
            assert meta["exact_rows"] == 1
            assert meta["fallback_rows"] == 0
            files = [c["file"] for c in res["callers"]]
            assert files == [NAKLADNAYA_PATH]
        finally:
            reader.close()

    def test_ambiguous_namesake_no_hint_falls_back(self, built):
        db_path, _ = built
        reader = IndexReader(db_path)
        try:
            # ОбщийМетод exists TWICE: exported in CommonModules/ОбщийМодуль AND a
            # local object method in Documents/Заказ. Globally ambiguous → no-hint
            # must NOT go exact, else the local method's resolved callers are
            # silently dropped. Fallback keeps recall: BOTH callers are visible.
            res = reader.get_callers("ОбщийМетод")
            meta = res["_meta"]
            assert meta["target_exact"] is False
            files = [c["file"] for c in res["callers"]]
            assert NAKLADNAYA_PATH in files  # caller of common ОбщийМетод
            assert ZAKAZ_PATH in files  # caller of local Заказ.ОбщийМетод
        finally:
            reader.close()

    def test_collision_excluded_with_hint(self, built):
        db_path, _ = built
        reader = IndexReader(db_path)
        try:
            # With a module_hint pinning the common module, exact mode resolves the
            # target precisely and drops the same-named local edge from Заказ.
            res = reader.get_callers("ОбщийМетод", module_hint="ОбщийМодуль")
            meta = res["_meta"]
            assert meta["target_exact"] is True
            assert meta["exact_rows"] == 1
            files = [c["file"] for c in res["callers"]]
            assert NAKLADNAYA_PATH in files
            assert ZAKAZ_PATH not in files
        finally:
            reader.close()

    def test_unresolved_target_falls_back(self, built):
        db_path, _ = built
        reader = IndexReader(db_path)
        try:
            # НайтиПоКоду is defined nowhere → no exact resolution → name fallback
            # still finds the qualified edge (recall preserved).
            res = reader.get_callers("НайтиПоКоду")
            meta = res["_meta"]
            assert meta["exact_available"] is True
            assert meta["target_exact"] is False
            assert meta["exact_rows"] == 0
            assert meta["fallback_rows"] >= 1
        finally:
            reader.close()

    def test_module_hint_forms_resolve(self, built):
        db_path, _ = built
        reader = IndexReader(db_path)
        try:
            for hint in (NAKLADNAYA_PATH, "Документ.Накладная", "Document.Накладная", "Накладная"):
                res = reader.get_callers("ОбработкаПроведения", module_hint=hint)
                assert res["_meta"]["target_exact"] is True, f"hint failed: {hint}"
        finally:
            reader.close()

    def test_bare_hint_with_null_object_name_no_crash(self, tmp_path):
        # Root application modules have object_name = NULL. A bare module_hint
        # applies py_lower(mod.object_name) across all modules → must not raise
        # "user-defined function raised exception" on the NULL rows.
        files = {
            "Ext/ManagedApplicationModule.bsl": "Процедура ПриНачалеРаботы()\n    Тест();\nКонецПроцедуры\n",
            "CommonModules/М/Ext/Module.bsl": "Процедура Тест() Экспорт\nКонецПроцедуры\n",
        }
        _write_project(tmp_path, files)
        db_path = IndexBuilder().build(str(tmp_path), build_calls=True)
        with _conn(db_path) as c:
            nulls = c.execute("SELECT COUNT(*) FROM modules WHERE object_name IS NULL").fetchone()[0]
        assert nulls >= 1, "fixture must contain a NULL-object_name module"
        reader = IndexReader(db_path)
        try:
            res = reader.get_callers("Тест", module_hint="М")  # bare hint
            assert res is not None
            assert res["_meta"]["target_exact"] is True
        finally:
            reader.close()


# ===========================================================================
# Phase B — module_hint normalization (module-level, no nested-import)
# ===========================================================================


class TestNormalizeModuleHint:
    def test_rel_path_form(self):
        assert _normalize_module_hint("Documents/Накладная/Ext/ObjectModule.bsl") == (
            "Documents/Накладная/Ext/ObjectModule.bsl",
            None,
            None,
        )

    def test_backslash_rel_path_normalized(self):
        rel, cat, obj = _normalize_module_hint("Documents\\Накладная\\Ext\\ObjectModule.bsl")
        assert rel == "Documents/Накладная/Ext/ObjectModule.bsl"
        assert cat is None and obj is None

    def test_ru_prefix(self):
        assert _normalize_module_hint("Документ.Накладная") == (None, "Documents", "Накладная")

    def test_en_prefix(self):
        assert _normalize_module_hint("Document.Накладная") == (None, "Documents", "Накладная")

    def test_bare_object_name(self):
        assert _normalize_module_hint("Накладная") == (None, None, "Накладная")

    def test_empty(self):
        assert _normalize_module_hint("") == (None, None, None)


# ===========================================================================
# Phase B — find_call_hierarchy: hint, propagation, visited-by-target, shape
# ===========================================================================


class TestCallHierarchy:
    def test_shape_and_root_exact(self, built):
        db_path, base = built
        bsl, reader = _make_indexed_helpers(base, db_path)
        try:
            res = bsl["find_call_hierarchy"]("УникальныйЭкспортныйМетод", depth=2)
            assert "error" not in res
            meta = res["_meta"]
            assert meta["exact_available"] is True
            assert meta["root_exact"] is True
            # node shape
            for node in res["tree"]:
                assert set(node) >= {"name", "target_hint", "target_key", "meta", "callers"}
                nm = node["meta"]
                assert set(nm) >= {"exact_rows", "fallback_rows", "exact_available", "target_exact"}
        finally:
            reader.close()

    def test_propagation_keeps_exact_on_depth(self, built):
        db_path, base = built
        bsl, reader = _make_indexed_helpers(base, db_path)
        try:
            res = bsl["find_call_hierarchy"]("УникальныйЭкспортныйМетод", depth=2)
            # L2 reaches ВспомогательныйМетод (non-exported object method) via the
            # caller's rel_path → exact propagation, not name fallback.
            node = next(n for n in res["tree"] if n["name"] == "ВспомогательныйМетод")
            assert node["meta"]["target_exact"] is True
            assert node["target_key"].endswith("::вспомогательныйметод")
        finally:
            reader.close()

    def test_root_without_hint_not_exact_for_object_method(self, built):
        db_path, base = built
        bsl, reader = _make_indexed_helpers(base, db_path)
        try:
            res = bsl["find_call_hierarchy"]("ОбработкаПроведения", depth=1)
            assert res["_meta"]["exact_available"] is True
            assert res["_meta"]["root_exact"] is False
        finally:
            reader.close()

    def test_root_with_hint_exact_for_object_method(self, built):
        db_path, base = built
        bsl, reader = _make_indexed_helpers(base, db_path)
        try:
            res = bsl["find_call_hierarchy"]("ОбработкаПроведения", depth=1, module_hint="Документ.Накладная")
            assert res["_meta"]["root_exact"] is True
        finally:
            reader.close()

    def test_visited_by_target_keeps_namesakes(self, built):
        db_path, base = built
        bsl, reader = _make_indexed_helpers(base, db_path)
        try:
            # ОбщаяТочка is called by ПередЗаписью in BOTH Заказ and Счет. With
            # exact propagation these two same-named callers become two DISTINCT
            # L2 targets (different rel_path) and must both survive the visited set.
            res = bsl["find_call_hierarchy"]("ОбщаяТочка", depth=2)
            pz_nodes = [n for n in res["tree"] if n["name"] == "ПередЗаписью"]
            assert len(pz_nodes) == 2
            keys = {n["target_key"] for n in pz_nodes}
            assert len(keys) == 2  # distinct rel_path::method identities
        finally:
            reader.close()


# ===========================================================================
# Phase B — v13 read-without-rebuild + version migration
# ===========================================================================


V13_SCHEMA = """\
CREATE TABLE index_meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE modules (id INTEGER PRIMARY KEY, rel_path TEXT UNIQUE NOT NULL,
    category TEXT, object_name TEXT, module_type TEXT, form_name TEXT,
    is_form INTEGER DEFAULT 0, mtime REAL, size INTEGER);
CREATE TABLE methods (id INTEGER PRIMARY KEY, module_id INTEGER NOT NULL,
    name TEXT NOT NULL, type TEXT NOT NULL, is_export INTEGER DEFAULT 0,
    params TEXT, line INTEGER, end_line INTEGER, loc INTEGER);
CREATE TABLE calls (id INTEGER PRIMARY KEY, caller_id INTEGER NOT NULL,
    callee_name TEXT NOT NULL, line INTEGER);
"""


def test_v13_read_without_rebuild(tmp_path):
    """A v13 DB (no callee_key) read directly → name-based fallback, no crash."""
    db = tmp_path / "v13.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(V13_SCHEMA)
    conn.execute("INSERT INTO index_meta (key, value) VALUES ('builder_version', '13')")
    conn.execute(
        "INSERT INTO modules (id, rel_path, category, object_name, module_type) "
        "VALUES (1, 'CommonModules/М/Ext/Module.bsl', 'CommonModules', 'М', 'Module')"
    )
    conn.execute(
        "INSERT INTO methods (id, module_id, name, type, is_export, line, end_line) "
        "VALUES (1, 1, 'Вызывающий', 'procedure', 0, 1, 3)"
    )
    conn.execute("INSERT INTO calls (id, caller_id, callee_name, line) VALUES (1, 1, 'Целевой', 2)")
    conn.commit()
    conn.close()

    reader = IndexReader(db)
    try:
        assert reader._has_callee_key is False
        res = reader.get_callers("Целевой")
        meta = res["_meta"]
        assert meta["exact_available"] is False
        assert meta["exact_rows"] == 0
        assert meta["fallback_rows"] == meta["total_callers"] >= 1
    finally:
        reader.close()


def test_incremental_key_survives_target_rowid_change(built):
    """Codex finding #1: changing the TARGET module re-creates its methods with
    NEW rowids, but the stable rel_path-based callee_key keeps incoming edges
    (from unchanged callers) resolvable in exact mode."""
    import pathlib

    db_path, base = built
    target_abs = pathlib.Path(base) / OBSHIY_PATH

    # Edit the target module body (method preserved) → re-processed on update,
    # its methods are deleted and re-inserted (rowids re-issued). The point of
    # the stable rel_path-based key: incoming edges from unchanged callers do
    # NOT reference rowid, so they survive this.
    target_abs.write_text(
        COMMON_MODULE_BSL.replace(
            "Функция УникальныйЭкспортныйМетод() Экспорт",
            "// touched\nФункция УникальныйЭкспортныйМетод() Экспорт",
        ),
        encoding="utf-8-sig",
    )
    result = IndexBuilder().update(base)
    assert result.get("changed", 0) >= 1, "target module should be re-processed"

    # Incoming edge from the UNCHANGED caller still resolves in exact mode.
    reader = IndexReader(db_path)
    try:
        res = reader.get_callers("УникальныйЭкспортныйМетод")
        assert res["_meta"]["target_exact"] is True
        assert res["_meta"]["exact_rows"] == 1
        assert [c["file"] for c in res["callers"]] == [NAKLADNAYA_PATH]
    finally:
        reader.close()


def test_v13_migration_forces_rebuild(built):
    """Downgrade a built index to a v13-shaped DB, then update() → full rebuild
    that recreates the schema with callee_key populated."""
    db_path, base = built
    # Downgrade: drop callee_key column + set builder_version=13.
    conn = sqlite3.connect(str(db_path))
    conn.execute("ALTER TABLE calls RENAME TO calls_old")
    conn.execute(
        "CREATE TABLE calls (id INTEGER PRIMARY KEY, caller_id INTEGER NOT NULL, "
        "callee_name TEXT NOT NULL, line INTEGER)"
    )
    conn.execute(
        "INSERT INTO calls (id, caller_id, callee_name, line) SELECT id, caller_id, callee_name, line FROM calls_old"
    )
    conn.execute("DROP TABLE calls_old")
    conn.execute("UPDATE index_meta SET value='13' WHERE key='builder_version'")
    conn.commit()
    conn.close()

    result = IndexBuilder().update(base)
    assert result.get("git_fast_path") is False
    assert "rebuild_reason" in result

    conn = sqlite3.connect(str(db_path))
    cols = {r[1] for r in conn.execute("PRAGMA table_info(calls)")}
    resolved = conn.execute("SELECT COUNT(callee_key) FROM calls").fetchone()[0]
    version = conn.execute("SELECT value FROM index_meta WHERE key='builder_version'").fetchone()[0]
    conn.close()
    assert "callee_key" in cols
    assert resolved > 0
    assert int(version) == BUILDER_VERSION


# ===========================================================================
# Help / strategy layer — module_hint must reach the agent
# ===========================================================================


def _registry(base):
    helpers, resolve_safe = make_helpers(str(base))
    fmt = detect_format(str(base))
    bsl = make_bsl_helpers(
        base_path=str(base),
        resolve_safe=resolve_safe,
        read_file_fn=helpers["read_file"],
        grep_fn=helpers["grep"],
        glob_files_fn=helpers["glob_files"],
        format_info=fmt,
    )
    return bsl["_registry"]


def test_help_sig_mentions_hint_and_meta(project):
    reg = _registry(project)
    entry = reg["find_call_hierarchy"]
    sig = entry["sig"]
    assert "module_hint" in sig
    assert "exact_available" in sig
    assert "target_key" in sig
    assert "include_triggers" in sig
    recipe = entry["recipe"]
    assert "module_hint" in recipe
    assert "root_exact" in recipe
    assert "include_triggers" in recipe


def test_help_topic_recipe_reflects_hint(project):
    from rlm_tools_bsl.bsl_knowledge import _get_topic_recipe

    rec = _get_topic_recipe("иерархия вызовов", format="full")
    assert rec is not None
    blob = " ".join(rec["steps"]) + " " + rec.get("code_hint", "")
    assert "module_hint" in blob
    assert "exact" in blob.lower()


@pytest.mark.parametrize("mode", ["slim", "full"])
def test_strategy_hint_tip_consistent_across_modes(monkeypatch, mode):
    # slim and full INDEX-TIPS must agree: hint is not for speed but for precision
    # (disambiguation of same-named object methods). Regression for the slim branch
    # that kept the old "no need to limit scope with hint".
    from rlm_tools_bsl.bsl_knowledge import get_strategy

    monkeypatch.setenv("RLM_STRATEGY_MODE", mode)
    idx_stats = {"methods": 100, "calls": 200, "has_fts": True}
    s = get_strategy("medium", None, idx_stats=idx_stats)
    assert "no need to limit scope with hint" not in s
    assert "module_hint" in s
    assert "ТОЧНОСТЬ" in s


# Платформенные обработчики модуля объекта: их вызывает ПЛАТФОРМА, а не BSL, поэтому в графе
# ВЫЗОВОВ входящего ребра у них нет и callers всегда 0. Трассировать их call-хелперами нельзя.
_PLATFORM_HANDLERS = "ОбработкаПроведения|ПередЗаписью|ПриЗаписи|ОбработкаЗаполнения"
# Ловим именно ВЫЗОВ хелпера на обработчике. Просто `not in` тут не годится: рецепты обязаны
# УПОМИНАТЬ find_call_hierarchy — в предупреждении «не зови его на обработчике».
_TRACES_PLATFORM_HANDLER = re.compile(rf"find_call(?:_hierarchy|ers_context)\(\s*['\"](?:{_PLATFORM_HANDLERS})")


@pytest.mark.parametrize("fmt", ["compact", "full"])
def test_posting_recipe_never_traces_the_platform_handler(fmt):
    """Прежний тест (test_posting_recipe_uses_module_hint) требовал в рецепте строку
    find_call_hierarchy('ОбработкаПроведения', module_hint=...) — «канонический одноимённый
    объектный метод». Посылка оказалась ЛОЖНОЙ: обработчик зовёт ПЛАТФОРМА, у него callers=0
    ВСЕГДА, и module_hint его не «оживит». Тест цементировал тупиковый совет, а e2e v1.28.0
    показал, как агент по нему упирается в ноль. Здесь пинится НОВЫЙ маршрут.

    module_hint без покрытия не остался: он пинится в test_help_sig_mentions_hint_and_meta
    (registry), test_help_topic_recipe_reflects_hint (топик «иерархия вызовов») и
    test_strategy_hint_tip_consistent_across_modes (обе копии стратегии)."""
    from rlm_tools_bsl.bsl_knowledge import _get_topic_recipe

    rec = _get_topic_recipe("проведение", format=fmt)
    assert rec is not None
    blob = " ".join(rec["steps"]) + " " + rec.get("code_hint", "")

    m = _TRACES_PLATFORM_HANDLER.search(blob)
    assert not m, f"рецепт «проведение»/{fmt} снова зовёт call-хелпер на обработчике: {m.group(0) if m else ''}"

    # Рабочий маршрут вместо тупика. Он живёт в result['hint']: тело обработчика разбирает СЕРВЕР
    # (агент не может — CFE-модуль ему не прочитать, а получателя слева от точки не разрешить),
    # поэтому рецепт обязан вести ИМЕННО ТУДА, а не пересказывать развилку своими словами.
    assert "hint" in blob, f"рецепт «проведение»/{fmt} не ведет в result['hint'] с разбором тела: {blob}"
    # Корень, а не словоформа: тексты говорят и «ПЛАТФОРМА», и «вызов от ПЛАТФОРМЫ».
    assert "ПЛАТФОРМ" in blob, f"рецепт «проведение»/{fmt} не объясняет, почему callers=0 — это норма"


@pytest.mark.parametrize("fmt", ["compact", "full"])
def test_posting_recipe_does_not_leave_the_receiver_to_the_agent(fmt):
    """Точка НЕ доказывает модуль: слева бывает переменная или РЕКВИЗИТ документа, а при наличии
    общего модуля-однофамильца find_definition отработает УСПЕШНО и молча вернёт ЧУЖОЕ тело —
    отказ, который выглядит как ответ.

    Разрешить получателя агент не может В ПРИНЦИПЕ: текстовые маркеры не видят реквизит, а
    «подтверждение по category» тавтологично (module_hint 'ОбщийМодуль.X' сам же и фильтрует по
    этой категории в SQL). Поэтому разрешает СЕРВЕР, а рецепт обязан: назвать возможные виды
    получателя, назвать цену ошибки и запретить тавтологическое «подтверждение»."""
    from rlm_tools_bsl.bsl_knowledge import _get_topic_recipe

    rec = _get_topic_recipe("проведение", format=fmt)
    blob = " ".join(rec["steps"]) + " " + rec.get("code_hint", "")
    up = blob.upper()
    assert "ПЕРЕМЕННАЯ" in up, f"рецепт «проведение»/{fmt} не называет вид получателя «переменная»: {blob}"
    assert "РЕКВИЗИТ" in up, (
        f"рецепт «проведение»/{fmt} не называет РЕКВИЗИТ — а это тот случай, где текстовая эвристика "
        f"бессильна и агент молча читал чужое тело: {blob}"
    )
    assert "ЧУЖОЕ" in up, f"рецепт «проведение»/{fmt} не называет ЦЕНУ ошибки (молча чужое тело): {blob}"
    # Тавтология названа тавтологией: иначе агент «подтвердит» чужой модуль проверкой, которая
    # истинна по построению, и получит ложную уверенность вместо ответа.
    assert "ТАВТОЛОГИ" in up or "ВСЕГДА ИСТИННА" in up, (
        f"рецепт «проведение»/{fmt} не запрещает подтверждение по category (оно круговое): {blob}"
    )


@pytest.mark.parametrize("fmt", ["compact", "full"])
def test_call_hierarchy_recipe_example_is_not_a_platform_handler(fmt):
    """Рецепт темы «иерархия вызовов» показывал find_call_hierarchy('ОбработкаПроведения', ...)
    как КАНОНИЧЕСКИЙ пример module_hint. Пример заведомо пустой (callers=0) и прямо противоречит
    рецепту «проведение», который запрещает так делать. Пример обязан быть на методе, который
    РЕАЛЬНО зовут из BSL."""
    from rlm_tools_bsl.bsl_knowledge import _get_topic_recipe

    rec = _get_topic_recipe("иерархия вызовов", format=fmt)
    assert rec is not None
    blob = " ".join(rec["steps"]) + " " + rec.get("code_hint", "")

    m = _TRACES_PLATFORM_HANDLER.search(blob)
    assert not m, f"пример в «иерархия вызовов»/{fmt} вернёт callers=0: {m.group(0) if m else ''}"
    assert "ПЛАТФОРМ" in blob, f"«иерархия вызовов»/{fmt} не предупреждает про платформенные обработчики"


# Целый ВЫЗОВ call-хелпера на платформенном обработчике (а не просто упоминание имени).
_CALL_ON_HANDLER = re.compile(rf"find_call(?:_hierarchy|ers_context|ers)\(\s*['\"](?:{_PLATFORM_HANDLERS})[^)]*\)")


def test_no_registry_recipe_traces_a_platform_handler_without_triggers(project):
    """Сплошной гейт по ВСЕМ рецептам реестра — а не только по тем, что мы вспомнили руками.
    Именно ручной выбор «радиуса» уже дважды пропускал этот дефект: тупиковый совет нашёлся и в
    рецепте find_definition («дальше: find_callers_context('ОбработкаПроведения')» → вернёт 0).

    Инвариант с исключением, а не запрет: единственный ОСМЫСЛЕННЫЙ вызов call-хелпера на
    платформенном обработчике — `find_call_hierarchy(..., include_triggers=True)`. Он честно
    вернёт callers=0, но ПОКАЖЕТ CFE-перехват обработчика расширением, а это ровно та причина,
    по которой на обработчик вообще стоит смотреть. Всё остальное (голый find_call_hierarchy,
    любой find_callers_context / find_callers — они include_triggers не поддерживают в принципе)
    гарантированно отдаст пусто и научит агента читать это как «мёртвый код»."""
    reg = _registry(project)
    offenders = []
    for name, entry in reg.items():
        text = f"{entry.get('sig') or ''} {entry.get('recipe') or ''}"
        for call in _CALL_ON_HANDLER.findall(text):
            if "include_triggers=True" not in call:
                offenders.append(f"{name}: {call}")
    assert not offenders, "рецепты зовут call-хелпер на платформенном обработчике (вернёт 0):\n" + "\n".join(offenders)


def _agent_facing_texts(project, monkeypatch) -> dict[str, str]:
    """ВСЕ тексты, которые уходят агенту: registry (sig+recipe), топик-рецепты (обе формы), обе
    стратегии (обязательно с idx_stats — без индекса блок INDEX TIPS не рендерится вовсе)."""
    from rlm_tools_bsl.bsl_knowledge import _BUSINESS_RECIPES, _get_topic_recipe, get_strategy

    texts: dict[str, str] = {}
    for name, entry in _registry(project).items():
        texts[f"registry:{name}"] = f"{entry.get('sig') or ''}\n{entry.get('recipe') or ''}"
    for topic in _BUSINESS_RECIPES:
        for fmt in ("compact", "full"):
            rec = _get_topic_recipe(topic, format=fmt) or {}
            texts[f"recipe:{topic}/{fmt}"] = " ".join(rec.get("steps", [])) + " " + (rec.get("code_hint") or "")
    idx_stats = {"methods": 100, "calls": 200, "has_fts": True}
    for mode in ("slim", "full"):
        monkeypatch.setenv("RLM_STRATEGY_MODE", mode)
        texts[f"strategy:{mode}"] = get_strategy("medium", None, idx_stats=idx_stats, query="")
    return texts


# Плейсхолдер ВНУТРИ вызова: `read_procedure(<файл делегата>, ...)`. Угловые скобки в прозе
# законны (`Движения.<Регистр>`), а в аргументах — это псевдокод, который агент скопирует дословно.
_PSEUDOCODE_IN_CALL = re.compile(r"\w\(\s*<")


def test_no_agent_facing_text_puts_pseudocode_inside_a_call(project, monkeypatch):
    """`read_procedure(<файл делегата>, ...)` — невалидный Python в тексте, который агент копирует
    ДОСЛОВНО. Первый раз это нашли в hint и починили — а та же строка преспокойно осталась в
    рецепте find_register_movements, потому что гейт проверял ТОЛЬКО hint. Гейт обязан быть
    сплошным, иначе он ловит ровно то место, где мы уже посмотрели."""
    offenders = [
        f"{where}: ...{text[max(0, m.start() - 40) : m.end() + 40]}..."
        for where, text in _agent_facing_texts(project, monkeypatch).items()
        if (m := _PSEUDOCODE_IN_CALL.search(text))
    ]
    assert not offenders, "псевдокод в аргументах вызова (агент скопирует и получит SyntaxError):\n" + "\n".join(
        offenders
    )


def test_contract_docs_do_not_overclaim_zero_callers():
    """Документация дрейфовала ДВАЖДЫ за релиз, и оба раза мимо гейтов — они покрывали только код.
    HELPERS.md описывал маршрут, которого в hint уже нет; в ARCHITECTURE.md абсолютное «callers
    пусты» я повторил в той самой строке, которую в тот момент и правил.

    Инвариант тот же, что и для agent-facing текстов: если контрактный документ говорит про callers
    рядом с платформенным обработчиком, он обязан оговорить, что ЯВНЫЕ BSL-вызовы находятся.

    Список файлов ЯВНЫЙ, а не docs/*.md, и вот почему:
      * full_analysis_prompt.md — это e2e-ХАРНЕСС. По методике сравнительных прогонов он обязан
        оставаться идентичным прошлому релизу, иначе метрики релиз-к-релизу несопоставимы. Там
        find_call_hierarchy('ОбработкаПроведения') — механическое упражнение хелпера, а не совет.
      * INDEXING.md — не контрактный документ (там ОбработкаПроведенияДокументов лишь в примере
        строки индекса), гейт дал бы ложное срабатывание."""
    from pathlib import Path

    docs = Path(__file__).resolve().parents[1] / "docs"
    handler_re = re.compile(_PLATFORM_HANDLERS)
    zero_callers_re = re.compile(r"callers\s*=\s*0|пуст\w*\s+callers", re.IGNORECASE)
    offenders = []
    for name in ("HELPERS.md", "ARCHITECTURE.md"):
        lines = (docs / name).read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines):
            if not handler_re.search(line):
                continue
            # HELPERS хранит контракт каждого хелпера в одной bullet-строке; ARCHITECTURE
            # переносит NB на три строки. Локальное окно покрывает обе формы, но не позволяет
            # постороннему «явно» за сотню строк завизировать неверное утверждение здесь.
            context = line if name == "HELPERS.md" else "\n".join(lines[max(0, index - 1) : index + 3])
            if zero_callers_re.search(context) and "явн" not in context.lower():
                offenders.append(f"{name}:{index + 1}")
    assert not offenders, (
        "документ говорит про callers у платформенного обработчика, но не оговаривает, что явные "
        "BSL-вызовы находятся — снова противоречит продукту:\n" + "\n".join(offenders)
    )


def test_platform_handler_texts_do_not_overclaim_zero_callers(project, monkeypatch):
    """«У платформенного обработчика callers=0 ВСЕГДА / он вообще не трассируется» — это СИЛЬНЕЕ
    фактического контракта, и в таком виде совет ВРЕДЕН.

    В граф вызовов не попадает именно ПЛАТФОРМЕННЫЙ вход. Но find_call_hierarchy не исключает
    методы по имени: если BSL-код где-то ЯВНО зовёт ОбработкаПроведения(...) (скажем, из другого
    метода того же ObjectModule), билдер пишет обычное call-ребро и хелпер его вернёт. Текст,
    объявляющий такие методы нетрассируемыми, заставит агента ПРОИГНОРИРОВАТЬ реальное ребро —
    то есть чинить одну ложь другой.

    Инвариант: текст, объясняющий «callers=0 — норма» рядом с платформенным обработчиком, обязан
    тут же оговорить, что ЯВНЫЕ BSL-вызовы всё же находятся. Проверяются ВСЕ agent-facing
    поверхности разом (registry sig+recipe, все топик-рецепты в обеих формах, обе стратегии) —
    выборочный обход по памяти в этом релизе промахивался трижды."""
    handler_re = re.compile(_PLATFORM_HANDLERS)
    offenders = [
        where
        for where, text in _agent_facing_texts(project, monkeypatch).items()
        if "callers=0" in text and handler_re.search(text) and "явн" not in text.lower()
    ]
    assert not offenders, (
        "текст объявляет callers=0 у платформенного обработчика, но НЕ оговаривает, что явные "
        "BSL-вызовы всё же находятся (агент проигнорирует реальное ребро):\n" + "\n".join(offenders)
    )
    absolute_zero = [
        where
        for where, text in _agent_facing_texts(project, monkeypatch).items()
        if handler_re.search(text) and "их и так 0" in text.lower()
    ]
    assert not absolute_zero, (
        "локальная абсолютная формулировка противоречит оговорке про явные BSL-вызовы:\n" + "\n".join(absolute_zero)
    )


def _fs_helpers(base):
    helpers, resolve_safe = make_helpers(str(base))
    fmt = detect_format(str(base))
    return make_bsl_helpers(
        base_path=str(base),
        resolve_safe=resolve_safe,
        read_file_fn=helpers["read_file"],
        grep_fn=helpers["grep"],
        glob_files_fn=helpers["glob_files"],
        format_info=fmt,
    )  # no idx_reader → FS fallback


def test_fs_fallback_meta_contract_has_target_exact(project):
    # No index → find_callers_context uses the FS scan. Its _meta must still
    # carry the full contract the registry sig promises, incl. target_exact.
    bsl = _fs_helpers(project)
    res = bsl["find_callers_context"]("ОбщаяТочка")
    meta = res["_meta"]
    for k in (
        "total_callers",
        "returned",
        "offset",
        "has_more",
        "exact_available",
        "target_exact",
        "exact_rows",
        "fallback_rows",
    ):
        assert k in meta, f"FS-fallback _meta missing '{k}'"
    assert meta["exact_available"] is False
    assert meta["target_exact"] is False


# ===========================================================================
# v1.16.0 perf fix — idx_calls_callee_short expression index + node-budget guard
# ===========================================================================

SCHET_PATH = "Documents/Счет/Ext/ObjectModule.bsl"


def _indexes(db_path):
    with _conn(db_path) as c:
        return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='index'")}


def _plan_blob(conn, sql, params):
    rows = conn.execute(f"EXPLAIN QUERY PLAN {sql}", params).fetchall()
    return " ".join(r["detail"] for r in rows)


class TestCalleeShortIndex:
    def test_index_present_after_build(self, built):
        # Built via the normal executescript(_SCHEMA_SQL) site.
        db_path, _ = built
        assert "idx_calls_callee_short" in _indexes(db_path)

    def test_index_present_after_empty_repo_build(self, tmp_path):
        # The empty-repo branch (total_files == 0) has its OWN executescript site;
        # the index lives in _SCHEMA_SQL so both are covered (Codex round-4).
        (tmp_path / "Configuration.xml").write_text("<Configuration/>", encoding="utf-8")
        db_path = IndexBuilder().build(str(tmp_path), build_calls=True)
        assert "idx_calls_callee_short" in _indexes(db_path)

    def test_index_recreated_on_noop_update(self, built):
        # Drop the index but keep builder_version=14, then update() with NO file
        # changes. A no-op update is what proves the ensure sits OUTSIDE the
        # `if bsl_changed:` delta block (Codex round-3) — it must heal anyway.
        db_path, base = built
        conn = sqlite3.connect(str(db_path))
        conn.execute("DROP INDEX IF EXISTS idx_calls_callee_short")
        conn.commit()
        conn.close()
        assert "idx_calls_callee_short" not in _indexes(db_path)

        IndexBuilder().update(base)
        assert "idx_calls_callee_short" in _indexes(db_path)

    def test_explain_uses_short_index(self, built):
        # MANDATORY guard (Codex finding 2): the query MUST use the expression
        # index, not a scan. Built from the SAME helper get_callers uses, so a
        # drift (or a dropped COLLATE NOCASE) flips this red instead of silently
        # regressing prod into a full scan.
        db_path, _ = built
        with _conn(db_path) as c:
            # bare no-hint COUNT clause (unaliased column)
            sql, param = _callee_match_clause("ОбщийМетод", "callee_name")
            blob = _plan_blob(c, f"SELECT COUNT(*) FROM calls WHERE ({sql})", [param])
            assert "idx_calls_callee_short" in blob, blob

            # exact-mode WHERE → MULTI-INDEX OR over callee_key + short index
            msql, mparam = _callee_match_clause("ОбщийМетод", "c.callee_name")
            where = f"WHERE (c.callee_key = ? OR (c.callee_key IS NULL AND {msql}))"
            blob2 = _plan_blob(c, f"SELECT c.id FROM calls c {where}", ["k", mparam])
            assert "idx_calls_callee_short" in blob2, blob2
            assert "idx_calls_callee_key" in blob2, blob2

    def test_collate_nocase_in_clause(self):
        # The COLLATE NOCASE is load-bearing (planner ignores the case-insensitive
        # expression index without it). Assert the helper keeps emitting it.
        sql, _ = _callee_match_clause("Имя", "callee_name")
        assert "COLLATE NOCASE" in sql
        assert _callee_short_expr("callee_name") in sql

    def test_bare_suffix_recall_parity(self, built):
        # Bare name must still match qualified "X.name" edges (the suffix equality
        # replaces the old LIKE '%.name'): same recall, now index-backed.
        db_path, _ = built
        reader = IndexReader(db_path)
        try:
            # НайтиПоКоду appears ONLY as Справочники.Контрагенты.НайтиПоКоду
            # (regex keeps "Контрагенты.НайтиПоКоду") — qualified-only edge.
            res = reader.get_callers("НайтиПоКоду")
            assert res["_meta"]["total_callers"] >= 1
            assert any(c["file"] == NAKLADNAYA_PATH for c in res["callers"])
            # ОбщаяТочка is qualified-called (ОбщийМодуль2.ОбщаяТочка) from BOTH docs.
            res2 = reader.get_callers("ОбщаяТочка")
            files = {c["file"] for c in res2["callers"]}
            assert ZAKAZ_PATH in files and SCHET_PATH in files
        finally:
            reader.close()

    def test_dotted_input_recall_and_index(self, built):
        # Dotted input (Codex finding 1) matches the FULL callee_name (legacy
        # parity); a suffix rewrite would return nothing → recall regression.
        db_path, _ = built
        reader = IndexReader(db_path)
        try:
            res = reader.get_callers("ОбщийМодуль.ОбщийМетод")
            assert any(c["file"] == NAKLADNAYA_PATH for c in res["callers"])
        finally:
            reader.close()
        # dotted branch hits the full-name index, NOT the short one.
        with _conn(db_path) as c:
            sql, param = _callee_match_clause("ОбщийМодуль.ОбщийМетод", "callee_name")
            blob = _plan_blob(c, f"SELECT COUNT(*) FROM calls WHERE ({sql})", [param])
            assert "idx_calls_callee (" in blob, blob
            assert "idx_calls_callee_short" not in blob, blob

    def test_callee_name_at_most_one_dot(self, built):
        # Invariant the suffix-parity relies on (Codex finding 3): the extractor
        # regex (\\w+)\\.(\\w+) yields callee_name with 0 or 1 dot. A future change
        # adding multi-dot names would break suffix matching — catch it here.
        db_path, _ = built
        with _conn(db_path) as c:
            multidot = c.execute(
                "SELECT COUNT(*) FROM calls WHERE (length(callee_name) - length(replace(callee_name, '.', ''))) >= 2"
            ).fetchone()[0]
        assert multidot == 0


class TestHierarchyNodeBudget:
    def test_node_budget_meta_present(self, built):
        # Contract: the fields exist on a normal (sub-cap) tree.
        db_path, base = built
        bsl, reader = _make_indexed_helpers(base, db_path)
        try:
            res = bsl["find_call_hierarchy"]("УникальныйЭкспортныйМетод", depth=2)
            from rlm_tools_bsl.bsl_helpers import _HIERARCHY_VISITED_CAP

            meta = res["_meta"]
            assert meta["node_budget_exceeded"] is False
            assert meta["visited_cap"] == _HIERARCHY_VISITED_CAP
        finally:
            reader.close()

    def test_node_budget_guard_fires(self, built, monkeypatch):
        # With a tiny cap the BFS stops early, marks the partial tree honestly,
        # and stays connected (level-ordered) — never raises.
        import rlm_tools_bsl.bsl_helpers as bh

        monkeypatch.setattr(bh, "_HIERARCHY_VISITED_CAP", 2)
        db_path, base = built
        bsl, reader = _make_indexed_helpers(base, db_path)
        try:
            res = bsl["find_call_hierarchy"]("ОбщаяТочка", depth=3)
            meta = res["_meta"]
            assert meta["node_budget_exceeded"] is True
            assert meta["visited_cap"] == 2
            assert res["visited"] >= 2
            assert len(res["tree"]) >= 1  # partial but non-empty
        finally:
            reader.close()


# ===========================================================================
# v1.16.0 perf fix (добор) — idx_meth_module + cheap calls-emptiness check
# ===========================================================================

# The resolver query shape from IndexReader._resolve_target_key (rel_path branch).
# py_lower is a UDF registered ONLY on IndexReader._conn — run EXPLAIN through the
# reader connection, NOT the raw _conn() helper (which lacks it).
_RESOLVER_QUERY = (
    "SELECT DISTINCT mod.rel_path FROM methods m JOIN modules mod ON mod.id = m.module_id "
    "WHERE mod.rel_path = ? AND py_lower(m.name) = py_lower(?)"
)


class TestMethModuleIndex:
    def test_index_present_after_build(self, built):
        db_path, _ = built
        assert "idx_meth_module" in _indexes(db_path)

    def test_index_present_after_empty_repo_build(self, tmp_path):
        # Empty-repo branch (total_files == 0) — separate executescript site.
        (tmp_path / "Configuration.xml").write_text("<Configuration/>", encoding="utf-8")
        db_path = IndexBuilder().build(str(tmp_path), build_calls=True)
        assert "idx_meth_module" in _indexes(db_path)

    def test_index_recreated_on_noop_update(self, built):
        # Drop the index, keep version 14, update() with NO file changes → self-heal
        # recreates it (proves ensure is outside the `if bsl_changed:` delta block).
        db_path, base = built
        conn = sqlite3.connect(str(db_path))
        conn.execute("DROP INDEX IF EXISTS idx_meth_module")
        conn.commit()
        conn.close()
        assert "idx_meth_module" not in _indexes(db_path)

        IndexBuilder().update(base)
        assert "idx_meth_module" in _indexes(db_path)

    def test_resolver_explain_uses_index(self, built):
        # Perf guard: _resolve_target_key's rel_path query must hit idx_meth_module
        # (SEARCH), not SCAN all methods. Run via reader._conn (has py_lower).
        db_path, _ = built
        reader = IndexReader(db_path)
        try:
            rows = reader._conn.execute(
                "EXPLAIN QUERY PLAN " + _RESOLVER_QUERY,
                (NAKLADNAYA_PATH, "ОбработкаПроведения"),
            ).fetchall()
            blob = " ".join(r["detail"] for r in rows)
            assert "idx_meth_module" in blob, blob
            assert "SCAN m" not in blob, blob  # methods must NOT be table-scanned
        finally:
            reader.close()

    def test_resolve_target_key_correct(self, built):
        # Correctness unchanged by the index: all hint forms still resolve the
        # object-method target to its module's stable key.
        db_path, _ = built
        reader = IndexReader(db_path)
        try:
            expected = _make_callee_key(NAKLADNAYA_PATH, "ОбработкаПроведения")
            for hint in (NAKLADNAYA_PATH, "Документ.Накладная", "Document.Накладная", "Накладная"):
                assert reader._resolve_target_key("ОбработкаПроведения", hint) == expected, hint
        finally:
            reader.close()


class TestCallsEmptinessCheck:
    def _traced(self, db_path):
        """Run has_calls + get_callers on a reader, return executed SQL list."""
        reader = IndexReader(db_path)
        seen: list[str] = []
        reader._conn.set_trace_callback(seen.append)
        try:
            _ = reader.has_calls
            reader.get_callers("ОбщийМетод")  # ambiguous → fallback mode (exercises both)
        finally:
            reader._conn.set_trace_callback(None)
            reader.close()
        return seen

    @staticmethod
    def _is_unfiltered_count_calls(sql: str) -> bool:
        s = " ".join(sql.split()).upper()
        return "COUNT(*)" in s and "FROM CALLS" in s and "WHERE" not in s and "JOIN" not in s

    def test_no_unfiltered_count_on_calls(self, built):
        # Perf guard (replaces a behavior-only test that would pass on the old
        # COUNT(*) code too): neither has_calls nor get_callers may run an
        # unfiltered `COUNT(*) FROM calls` (full ~3.7M scan). Filtered counts
        # (WHERE callee_key=? / no-hint WHERE (...)) are fine and not matched.
        db_path, _ = built
        offenders = [s for s in self._traced(db_path) if self._is_unfiltered_count_calls(s)]
        assert not offenders, f"unfiltered COUNT(*) FROM calls still executed: {offenders}"

    def test_emptiness_check_uses_limit1(self, built):
        # The replacement existence probe is actually emitted.
        sqls = [" ".join(s.split()).upper() for s in self._traced(built[0])]
        assert any("SELECT 1 FROM CALLS LIMIT 1" in s for s in sqls)

    def test_empty_calls_behavior(self, tmp_path):
        # calls table present but empty (no .bsl) → has_calls False, get_callers None.
        (tmp_path / "Configuration.xml").write_text("<Configuration/>", encoding="utf-8")
        db_path = IndexBuilder().build(str(tmp_path), build_calls=True)
        reader = IndexReader(db_path)
        try:
            assert reader.has_calls is False
            assert reader.get_callers("Любой") is None
        finally:
            reader.close()

    def test_has_calls_true_on_built(self, built):
        db_path, _ = built
        reader = IndexReader(db_path)
        try:
            assert reader.has_calls is True
            assert reader.get_callers("ОбщийМетод") is not None
        finally:
            reader.close()


# ===========================================================================
# callee_key drift fix — incremental re-resolve of qualified callers keeps the
# strict  update ≡ build  invariant (H1); batch-narrowed lookups (H2).
# ===========================================================================

# Minimal project: a common module target + a non-common (Document) caller that
# references it via qualified calls. The caller is NEVER edited in the drift
# scenarios, so its stored callee_key can only stay correct via the re-resolve.
_CM_TARGET = (
    "Функция Создать() Экспорт\n    Возврат 1;\nКонецФункции\n\n"
    "Функция Прочитать() Экспорт\n    Возврат 2;\nКонецФункции\n"
)
_CALLER = "Процедура Обработка()\n    ЦелевойМодуль.Создать();\n    ЦелевойМодуль.Прочитать();\nКонецПроцедуры\n"
_DRIFT_BASE = {
    "CommonModules/ЦелевойМодуль/Ext/Module.bsl": _CM_TARGET,
    "Documents/Потребитель/Ext/ObjectModule.bsl": _CALLER,
}


def _edge_tuples(db_path):
    """Per-edge identity (caller_path, caller_name, callee_name, line, callee_key).

    All fields are rel_path-based / textual, so the multiset is comparable across
    two independently built DBs — a mis-bound key with a coinciding value is
    caught (unlike a bare callee_key multiset)."""
    with _conn(db_path) as c:
        return sorted(
            (r["cp"], r["cn"], r["callee_name"], r["line"], r["callee_key"])
            for r in c.execute(
                "SELECT mod.rel_path AS cp, m.name AS cn, cl.callee_name AS callee_name, "
                "cl.line AS line, cl.callee_key AS callee_key "
                "FROM calls cl JOIN methods m ON cl.caller_id=m.id "
                "JOIN modules mod ON m.module_id=mod.id"
            )
        )


def _fresh_build(tmp_path_factory, files, name="fresh"):
    d = tmp_path_factory.mktemp(name)
    _write_project(d, files)
    return IndexBuilder().build(str(d), build_calls=True)


def _apply_changes(root, files_old, files_new):
    for rel, content in files_new.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8-sig")
    for rel in set(files_old) - set(files_new):
        (root / rel).unlink()


def _assert_update_equals_build(tmp_path_factory, files_a, files_b):
    """Build A, mutate on disk to B, incremental update; the per-edge tuples must
    equal a fresh full build of B (strict update ≡ build)."""
    da = tmp_path_factory.mktemp("upd")
    _write_project(da, files_a)
    db_a = IndexBuilder().build(str(da), build_calls=True)
    _apply_changes(da, files_a, files_b)
    IndexBuilder().update(str(da))
    db_b = _fresh_build(tmp_path_factory, files_b, "fresh")
    assert _edge_tuples(db_a) == _edge_tuples(db_b)
    return db_a


class TestCalleeKeyDriftReresolve:
    def test_rename_export_method_orphans_unchanged_caller(self, tmp_path_factory):
        files_b = dict(_DRIFT_BASE)
        files_b["CommonModules/ЦелевойМодуль/Ext/Module.bsl"] = _CM_TARGET.replace(
            "Функция Создать()", "Функция Создать_v2()"
        )
        db = self._roundtrip(tmp_path_factory, _DRIFT_BASE, files_b)
        # The Создать edge from the unchanged caller is now orphan (NULL).
        with _conn(db) as c:
            row = c.execute("SELECT callee_key FROM calls WHERE callee_name='ЦелевойМодуль.Создать'").fetchone()
            assert row is not None and row["callee_key"] is None
            row2 = c.execute("SELECT callee_key FROM calls WHERE callee_name='ЦелевойМодуль.Прочитать'").fetchone()
            assert row2["callee_key"] is not None  # untouched target still resolves

    def test_add_common_module_resolves_prior_reference(self, tmp_path_factory):
        # Caller references a module that does not exist yet → NULL; adding it heals.
        files_a = {
            "Documents/Потребитель/Ext/ObjectModule.bsl": ("Процедура П()\n    НовыйОбщий.Старт();\nКонецПроцедуры\n"),
        }
        files_b = dict(files_a)
        files_b["CommonModules/НовыйОбщий/Ext/Module.bsl"] = "Функция Старт() Экспорт\n    Возврат 1;\nКонецФункции\n"
        db = self._roundtrip(tmp_path_factory, files_a, files_b)
        with _conn(db) as c:
            row = c.execute("SELECT callee_key FROM calls WHERE callee_name='НовыйОбщий.Старт'").fetchone()
            assert row is not None and row["callee_key"] is not None  # stale_null healed

    def test_export_flip_orphans_caller(self, tmp_path_factory):
        files_b = dict(_DRIFT_BASE)
        files_b["CommonModules/ЦелевойМодуль/Ext/Module.bsl"] = _CM_TARGET.replace(
            "Функция Создать() Экспорт", "Функция Создать()"
        )
        db = self._roundtrip(tmp_path_factory, _DRIFT_BASE, files_b)
        with _conn(db) as c:
            row = c.execute("SELECT callee_key FROM calls WHERE callee_name='ЦелевойМодуль.Создать'").fetchone()
            assert row["callee_key"] is None  # non-exported common method → unresolved

    def test_delete_common_module_orphans_caller(self, tmp_path_factory):
        files_b = {"Documents/Потребитель/Ext/ObjectModule.bsl": _CALLER}
        db = self._roundtrip(tmp_path_factory, _DRIFT_BASE, files_b)
        with _conn(db) as c:
            for name in ("ЦелевойМодуль.Создать", "ЦелевойМодуль.Прочитать"):
                row = c.execute("SELECT callee_key FROM calls WHERE callee_name=?", (name,)).fetchone()
                assert row["callee_key"] is None

    def test_rename_common_module_itself(self, tmp_path_factory):
        # Path changes → old removed + new added; caller still names the old module.
        files_b = {
            "CommonModules/ЦелевойМодульНов/Ext/Module.bsl": _CM_TARGET,
            "Documents/Потребитель/Ext/ObjectModule.bsl": _CALLER,
        }
        db = self._roundtrip(tmp_path_factory, _DRIFT_BASE, files_b)
        with _conn(db) as c:
            row = c.execute("SELECT callee_key FROM calls WHERE callee_name='ЦелевойМодуль.Создать'").fetchone()
            assert row["callee_key"] is None  # old module gone → orphan

    def _roundtrip(self, tmp_path_factory, files_a, files_b):
        return _assert_update_equals_build(tmp_path_factory, files_a, files_b)


class TestQualifiedReresolveOnceMigration:
    def test_legacy_drift_healed_on_next_update(self, tmp_path_factory):
        d = tmp_path_factory.mktemp("legacy")
        _write_project(d, _DRIFT_BASE)
        db = IndexBuilder().build(str(d), build_calls=True)
        fresh = _edge_tuples(db)  # correct reference (build sets the flag)

        # Simulate a legacy index: corrupt a resolved key + clear the one-time flag.
        with _conn(db) as c:
            c.execute("UPDATE calls SET callee_key='LEGACY/STALE::создать' WHERE callee_name='ЦелевойМодуль.Создать'")
            c.execute("DELETE FROM index_meta WHERE key='qualified_callers_reresolved'")
            c.commit()

        # A no-op update (no file changes) must run the one-time GLOBAL migration.
        IndexBuilder().update(str(d))
        assert _edge_tuples(db) == fresh
        with _conn(db) as c:
            flag = c.execute("SELECT value FROM index_meta WHERE key='qualified_callers_reresolved'").fetchone()
            assert flag is not None and flag["value"] == "1"

    def test_fresh_build_sets_flag(self, tmp_path_factory):
        db = _fresh_build(tmp_path_factory, _DRIFT_BASE, "flagcheck")
        with _conn(db) as c:
            flag = c.execute("SELECT value FROM index_meta WHERE key='qualified_callers_reresolved'").fetchone()
            assert flag is not None and flag["value"] == "1"

    def test_migration_is_idempotent(self, tmp_path_factory):
        # Once the flag is set, the one-time global migration no longer fires:
        # a manual corruption survives an unrelated no-op update.
        d = tmp_path_factory.mktemp("idem")
        _write_project(d, _DRIFT_BASE)
        db = IndexBuilder().build(str(d), build_calls=True)
        with _conn(db) as c:
            c.execute("UPDATE calls SET callee_key='STALE::x' WHERE callee_name='ЦелевойМодуль.Создать'")
            c.commit()
        IndexBuilder().update(str(d))  # flag already '1' → migration skipped
        with _conn(db) as c:
            row = c.execute("SELECT callee_key FROM calls WHERE callee_name='ЦелевойМодуль.Создать'").fetchone()
            assert row["callee_key"] == "STALE::x"  # NOT healed → proves one-time


class TestReresolveRegressionAndUnit:
    def test_empty_set_is_noop(self, built):
        db_path, _ = built
        with sqlite3.connect(str(db_path)) as c:
            assert IndexBuilder._reresolve_qualified_callers(c, set()) == 0

    def test_noncommon_change_keeps_invariant(self, tmp_path_factory):
        # Editing only a non-common (Document) module must not perturb other edges;
        # with the flag already set the global migration does not run either.
        files_b = dict(_DRIFT_BASE)
        files_b["Documents/Потребитель/Ext/ObjectModule.bsl"] = (
            "Процедура Обработка()\n"
            "    ЦелевойМодуль.Создать();\n"
            "    ЦелевойМодуль.Прочитать();\n"
            "    // comment-only addition\n"
            "КонецПроцедуры\n"
        )
        _assert_update_equals_build(tmp_path_factory, _DRIFT_BASE, files_b)

    def test_build_common_exported_mapping(self, built):
        db_path, _ = built
        from rlm_tools_bsl.bsl_index import _build_common_exported

        with sqlite3.connect(str(db_path)) as c:
            mapping = _build_common_exported(c)
        # exported method of a common module is present (casefolded key)
        assert (("общиймодуль", "общийметод")) in mapping
        assert mapping[("общиймодуль", "общийметод")] == OBSHIY_PATH
        # a non-exported / non-common method is absent
        assert ("общиймодуль", "внутренняялогика") not in mapping

    def test_batch_methods_query_uses_index(self, built):
        # H2: the batch-narrowed methods_by_module query must hit idx_meth_module,
        # not full-scan all methods.
        db_path, _ = built
        with _conn(db_path) as c:
            mid = c.execute("SELECT id FROM modules LIMIT 1").fetchone()[0]
            blob = _plan_blob(
                c,
                "SELECT id, module_id FROM methods WHERE module_id IN (?) ORDER BY module_id, line",
                [mid],
            )
            assert "idx_meth_module" in blob, blob
            assert "SCAN methods" not in blob, blob
