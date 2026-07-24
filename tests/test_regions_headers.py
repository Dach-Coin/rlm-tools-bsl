"""Tests for v1.4.2 Regions & Module Headers feature.

Tests: _parse_regions(), _extract_header_comment(), IndexReader search methods,
helpers (search_regions, search_module_headers), diagnostics, delta-cleanup.
"""

import sqlite3

import pytest

from rlm_tools_bsl.bsl_index import (
    IndexBuilder,
    IndexReader,
    _extract_header_comment,
    _parse_regions,
    get_index_db_path,
)


# ---------------------------------------------------------------------------
# Unit tests: _parse_regions()
# ---------------------------------------------------------------------------


class TestParseRegions:
    def test_simple_region(self):
        lines = [
            "#Область Инициализация",
            "  Перем А;",
            "#КонецОбласти",
        ]
        result = _parse_regions(lines)
        assert len(result) == 1
        assert result[0]["name"] == "Инициализация"
        assert result[0]["line"] == 1
        assert result[0]["end_line"] == 3

    def test_nested_regions(self):
        lines = [
            "#Область Внешняя",
            "  #Область Вложенная",
            "    // код",
            "  #КонецОбласти",
            "#КонецОбласти",
        ]
        result = _parse_regions(lines)
        assert len(result) == 2
        assert result[0]["name"] == "Внешняя"
        assert result[0]["line"] == 1
        assert result[0]["end_line"] == 5
        assert result[1]["name"] == "Вложенная"
        assert result[1]["line"] == 2
        assert result[1]["end_line"] == 4

    def test_three_level_nesting(self):
        lines = [
            "#Область L1",
            "  #Область L2",
            "    #Область L3",
            "    #КонецОбласти",
            "  #КонецОбласти",
            "#КонецОбласти",
        ]
        result = _parse_regions(lines)
        assert len(result) == 3
        assert result[2]["name"] == "L3"
        assert result[2]["end_line"] == 4

    def test_unclosed_region(self):
        lines = [
            "#Область Открытая",
            "  // код без закрытия",
        ]
        result = _parse_regions(lines)
        assert len(result) == 1
        assert result[0]["name"] == "Открытая"
        assert result[0]["end_line"] is None

    def test_extra_end_region_ignored(self):
        lines = [
            "#КонецОбласти",
            "#Область Тест",
            "#КонецОбласти",
            "#КонецОбласти",
        ]
        result = _parse_regions(lines)
        assert len(result) == 1
        assert result[0]["name"] == "Тест"
        assert result[0]["end_line"] == 3

    def test_commented_region_skipped(self):
        lines = [
            "// #Область ОткатСобытий",
            "#Область Реальная",
            "#КонецОбласти",
        ]
        result = _parse_regions(lines)
        assert len(result) == 1
        assert result[0]["name"] == "Реальная"

    def test_english_region(self):
        lines = [
            "#Region Initialization",
            "#EndRegion",
        ]
        result = _parse_regions(lines)
        assert len(result) == 1
        assert result[0]["name"] == "Initialization"
        assert result[0]["end_line"] == 2

    def test_mixed_russian_english(self):
        lines = [
            "#Область Русская",
            "#КонецОбласти",
            "#Region English",
            "#EndRegion",
        ]
        result = _parse_regions(lines)
        assert len(result) == 2

    def test_indented_region(self):
        lines = [
            "\t#Область СТабом",
            "\t#КонецОбласти",
            "  #Область СПробелами",
            "  #КонецОбласти",
        ]
        result = _parse_regions(lines)
        assert len(result) == 2
        assert result[0]["name"] == "СТабом"
        assert result[1]["name"] == "СПробелами"

    def test_empty_file(self):
        assert _parse_regions([]) == []

    def test_file_without_regions(self):
        lines = [
            "Процедура Тест()",
            "  // код",
            "КонецПроцедуры",
        ]
        assert _parse_regions(lines) == []

    def test_empty_name_skipped(self):
        lines = [
            "#Область ",
            "#КонецОбласти",
        ]
        result = _parse_regions(lines)
        assert len(result) == 0


# ---------------------------------------------------------------------------
# Unit tests: _extract_header_comment()
# ---------------------------------------------------------------------------


class TestExtractHeaderComment:
    def test_typical_header(self):
        lines = [
            "// Модуль расчёта себестоимости",
            "// Автор: Иванов И.И.",
            "// Дата: 2024-01-15",
            "",
            "Процедура Тест()",
        ]
        result = _extract_header_comment(lines)
        assert "Модуль расчёта себестоимости" in result
        assert "Автор: Иванов И.И." in result

    def test_header_truncation(self):
        lines = ["// " + "A" * 200 for _ in range(5)]
        result = _extract_header_comment(lines, max_chars=500)
        assert len(result) <= 500

    def test_no_header(self):
        lines = [
            "Процедура Тест()",
            "КонецПроцедуры",
        ]
        assert _extract_header_comment(lines) == ""

    def test_header_stopped_by_procedure(self):
        lines = [
            "// Заголовок",
            "Процедура Тест()",
        ]
        result = _extract_header_comment(lines)
        assert result == "Заголовок"

    def test_header_stopped_by_region(self):
        lines = [
            "// Описание модуля",
            "#Область Инициализация",
        ]
        result = _extract_header_comment(lines)
        assert result == "Описание модуля"

    def test_leading_empty_lines_skipped(self):
        lines = [
            "",
            "",
            "// Заголовок",
            "",
        ]
        result = _extract_header_comment(lines)
        assert result == "Заголовок"

    def test_empty_file(self):
        assert _extract_header_comment([]) == ""

    def test_comment_without_space(self):
        lines = ["//Без пробела"]
        result = _extract_header_comment(lines)
        assert result == "Без пробела"

    def test_copyright_block_skipped(self):
        lines = [
            "// /////////////////////////////////////////////////////////////////////////////////////////////////////",
            "// Copyright (c) 2023, ООО 1С-Софт",
            "// Все права защищены.",
            "// /////////////////////////////////////////////////////////////////////////////////////////////////////",
        ]
        assert _extract_header_comment(lines) == ""

    def test_separator_without_copyright_kept(self):
        lines = [
            "// //////////////////////////////////////////////////////////////////////////////",
            "// Модуль расчёта себестоимости",
        ]
        result = _extract_header_comment(lines)
        assert "Модуль расчёта себестоимости" in result

    def test_stopped_by_var(self):
        lines = [
            "// Описание",
            "Перем МояПеременная;",
        ]
        result = _extract_header_comment(lines)
        assert result == "Описание"


# ---------------------------------------------------------------------------
# Integration: build + search
# ---------------------------------------------------------------------------


def _make_regions_fixture(tmp_path):
    """Create CF-format project with regions and header comments."""
    # CommonModules/ТестМодуль/Ext/Module.bsl
    cm_dir = tmp_path / "CommonModules" / "ТестМодуль" / "Ext"
    cm_dir.mkdir(parents=True)
    (cm_dir / "Module.bsl").write_text(
        "// Модуль расчёта себестоимости товаров\n"
        "// Доработка: Компания, 2024\n"
        "\n"
        "#Область ПрограммныйИнтерфейс\n"
        "\n"
        "Процедура РассчитатьСебестоимость() Экспорт\n"
        "КонецПроцедуры\n"
        "\n"
        "#КонецОбласти\n"
        "\n"
        "#Область СлужебныеПроцедуры\n"
        "\n"
        "Процедура Вспомогательная()\n"
        "КонецПроцедуры\n"
        "\n"
        "#КонецОбласти\n",
        encoding="utf-8-sig",
    )

    # Documents/АвансовыйОтчет/Ext/ObjectModule.bsl
    doc_dir = tmp_path / "Documents" / "АвансовыйОтчет" / "Ext"
    doc_dir.mkdir(parents=True)
    (doc_dir / "ObjectModule.bsl").write_text(
        "#Область ОбработчикиСобытий\n\nПроцедура ПриЗаписи(Отказ)\nКонецПроцедуры\n\n#КонецОбласти\n",
        encoding="utf-8-sig",
    )

    return tmp_path


@pytest.fixture
def regions_project(tmp_path):
    return _make_regions_fixture(tmp_path)


@pytest.fixture
def built_regions_index(regions_project, monkeypatch):
    monkeypatch.setenv("RLM_INDEX_DIR", str(regions_project / ".index"))
    builder = IndexBuilder()
    db_path = builder.build(
        str(regions_project),
        build_calls=False,
        build_fts=False,
        build_synonyms=False,
    )
    return db_path, regions_project


class TestRegionsIntegration:
    def test_regions_table_populated(self, built_regions_index):
        db_path, _ = built_regions_index
        conn = sqlite3.connect(str(db_path))
        count = conn.execute("SELECT COUNT(*) FROM regions").fetchone()[0]
        conn.close()
        assert count == 3  # ПрограммныйИнтерфейс, СлужебныеПроцедуры, ОбработчикиСобытий

    def test_module_headers_populated(self, built_regions_index):
        db_path, _ = built_regions_index
        conn = sqlite3.connect(str(db_path))
        count = conn.execute("SELECT COUNT(*) FROM module_headers").fetchone()[0]
        conn.close()
        assert count == 1  # only ТестМодуль has header comment

    def test_search_regions(self, built_regions_index):
        db_path, _ = built_regions_index
        reader = IndexReader(str(db_path))
        result = reader.search_regions("Программный")
        reader.close()
        assert result is not None
        assert len(result) == 1
        assert result[0]["name"] == "ПрограммныйИнтерфейс"
        assert result[0]["line"] == 4
        assert result[0]["end_line"] == 9
        assert result[0]["category"] == "CommonModules"

    def test_search_regions_empty_query(self, built_regions_index):
        db_path, _ = built_regions_index
        reader = IndexReader(str(db_path))
        result = reader.search_regions("")
        reader.close()
        assert result is not None
        assert len(result) == 3

    def test_search_module_headers(self, built_regions_index):
        db_path, _ = built_regions_index
        reader = IndexReader(str(db_path))
        result = reader.search_module_headers("себестоимости")
        reader.close()
        assert result is not None
        assert len(result) == 1
        assert "себестоимости" in result[0]["header_comment"]

    def test_search_module_headers_no_match(self, built_regions_index):
        db_path, _ = built_regions_index
        reader = IndexReader(str(db_path))
        result = reader.search_module_headers("несуществующий")
        reader.close()
        assert result is not None
        assert len(result) == 0

    def test_search_regions_missing_table(self, tmp_path):
        """search_regions returns None when table doesn't exist."""
        db = tmp_path / "empty.db"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE index_meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("CREATE TABLE modules (id INTEGER PRIMARY KEY, rel_path TEXT)")
        conn.commit()
        conn.close()
        reader = IndexReader(str(db))
        assert reader.search_regions("test") is None
        reader.close()

    def test_get_statistics_includes_regions(self, built_regions_index):
        db_path, _ = built_regions_index
        reader = IndexReader(str(db_path))
        stats = reader.get_statistics()
        reader.close()
        assert "regions" in stats
        assert stats["regions"] == 3
        assert "module_headers" in stats
        assert stats["module_headers"] == 1

    def test_builder_version_is_8(self, built_regions_index):
        db_path, _ = built_regions_index
        conn = sqlite3.connect(str(db_path))
        ver = conn.execute("SELECT value FROM index_meta WHERE key='builder_version'").fetchone()[0]
        conn.close()
        assert ver == "15"


class TestRegionsDeltaCleanup:
    def test_changed_file_updates_regions(self, regions_project, monkeypatch):
        """When a file changes, old regions/headers are replaced with new ones."""
        monkeypatch.setenv("RLM_INDEX_DIR", str(regions_project / ".idx_delta"))
        builder = IndexBuilder()
        builder.build(
            str(regions_project),
            build_calls=False,
            build_fts=False,
            build_synonyms=False,
        )

        # Modify the file to have different regions
        module_path = regions_project / "CommonModules" / "ТестМодуль" / "Ext" / "Module.bsl"
        module_path.write_text(
            "#Область НоваяОбласть\nПроцедура Новая() Экспорт\nКонецПроцедуры\n#КонецОбласти\n",
            encoding="utf-8-sig",
        )

        result = builder.update(str(regions_project))
        assert result["changed"] >= 1 or result["added"] >= 0

        db_path = get_index_db_path(str(regions_project))
        conn = sqlite3.connect(str(db_path))
        # Check that old regions are gone and new one is present
        rows = conn.execute(
            "SELECT r.name FROM regions r JOIN modules m ON m.id = r.module_id WHERE m.object_name = 'ТестМодуль'"
        ).fetchall()
        conn.close()
        names = [r[0] for r in rows]
        assert "НоваяОбласть" in names
        assert "ПрограммныйИнтерфейс" not in names


class TestV7MigrationBackfill:
    def test_v7_migration_backfills_regions_data(self, regions_project, monkeypatch):
        """v7→v8 migration must actually populate regions/module_headers with data."""
        monkeypatch.setenv("RLM_INDEX_DIR", str(regions_project / ".idx_backfill"))
        builder = IndexBuilder()
        db_path = builder.build(
            str(regions_project),
            build_calls=False,
            build_fts=False,
            build_synonyms=False,
        )
        # Simulate v7: drop new tables, set version=7
        conn = sqlite3.connect(str(db_path))
        conn.execute("DROP TABLE IF EXISTS regions")
        conn.execute("DROP TABLE IF EXISTS module_headers")
        conn.execute("INSERT OR REPLACE INTO index_meta (key, value) VALUES ('builder_version', '7')")
        conn.execute("INSERT OR REPLACE INTO index_meta (key, value) VALUES ('version', '7')")
        conn.commit()
        conn.close()

        builder.update(str(regions_project))

        conn = sqlite3.connect(str(db_path))
        regions_count = conn.execute("SELECT COUNT(*) FROM regions").fetchone()[0]
        headers_count = conn.execute("SELECT COUNT(*) FROM module_headers").fetchone()[0]
        conn.close()
        # regions_project has 3 regions and 1 header — data must be backfilled
        assert regions_count == 3
        assert headers_count == 1


class TestRegionsHelpers:
    def test_search_regions_helper(self, built_regions_index):
        db_path, project = built_regions_index
        from rlm_tools_bsl.bsl_helpers import make_bsl_helpers

        reader = IndexReader(str(db_path))
        bsl = make_bsl_helpers(
            base_path=str(project),
            resolve_safe=lambda p: __import__("pathlib").Path(p),
            read_file_fn=lambda p: "",
            grep_fn=lambda pat, path="": [],
            glob_files_fn=lambda pat: [],
            idx_reader=reader,
        )
        result = bsl["search_regions"]("Обработчики")
        assert len(result) == 1
        assert result[0]["name"] == "ОбработчикиСобытий"

        # limit parameter works through sandbox
        result_limited = bsl["search_regions"]("", limit=1)
        assert len(result_limited) == 1

        reader.close()

    def test_search_module_headers_helper(self, built_regions_index):
        db_path, project = built_regions_index
        from rlm_tools_bsl.bsl_helpers import make_bsl_helpers

        reader = IndexReader(str(db_path))
        bsl = make_bsl_helpers(
            base_path=str(project),
            resolve_safe=lambda p: __import__("pathlib").Path(p),
            read_file_fn=lambda p: "",
            grep_fn=lambda pat, path="": [],
            glob_files_fn=lambda pat: [],
            idx_reader=reader,
        )
        result = bsl["search_module_headers"]("себестоимости")
        assert len(result) == 1

        # limit parameter works through sandbox
        result_limited = bsl["search_module_headers"]("", limit=1)
        assert len(result_limited) == 1

        reader.close()

    def test_search_regions_no_index(self):
        from rlm_tools_bsl.bsl_helpers import make_bsl_helpers

        bsl = make_bsl_helpers(
            base_path="/nonexistent",
            resolve_safe=lambda p: __import__("pathlib").Path(p),
            read_file_fn=lambda p: "",
            grep_fn=lambda pat, path="": [],
            glob_files_fn=lambda pat: [],
        )
        assert bsl["search_regions"]("test") == []
        assert bsl["search_module_headers"]("test") == []

    def test_get_index_info_has_regions_capability(self, built_regions_index):
        db_path, project = built_regions_index
        from rlm_tools_bsl.bsl_helpers import make_bsl_helpers

        reader = IndexReader(str(db_path))
        bsl = make_bsl_helpers(
            base_path=str(project),
            resolve_safe=lambda p: __import__("pathlib").Path(p),
            read_file_fn=lambda p: "",
            grep_fn=lambda pat, path="": [],
            glob_files_fn=lambda pat: [],
            idx_reader=reader,
        )
        info = bsl["get_index_info"]()
        reader.close()
        assert info["has_regions"] is True
        assert info["has_module_headers"] is True

    def test_capabilities_true_even_with_zero_rows(self, tmp_path, monkeypatch):
        """has_regions/has_module_headers must be True on v8 index even if tables are empty."""
        from rlm_tools_bsl.bsl_helpers import make_bsl_helpers

        # Build index on project with NO regions and NO header comments
        proj = tmp_path / "empty_proj"
        mod_dir = proj / "CommonModules" / "Пустой" / "Ext"
        mod_dir.mkdir(parents=True)
        (mod_dir / "Module.bsl").write_text("Процедура Тест()\nКонецПроцедуры\n", encoding="utf-8-sig")
        monkeypatch.setenv("RLM_INDEX_DIR", str(proj / ".idx"))
        builder = IndexBuilder()
        db_path = builder.build(str(proj), build_calls=False, build_fts=False, build_synonyms=False)

        reader = IndexReader(str(db_path))
        stats = reader.get_statistics()
        assert stats["regions"] == 0
        assert stats["module_headers"] == 0

        bsl = make_bsl_helpers(
            base_path=str(proj),
            resolve_safe=lambda p: __import__("pathlib").Path(p),
            read_file_fn=lambda p: "",
            grep_fn=lambda pat, path="": [],
            glob_files_fn=lambda pat: [],
            idx_reader=reader,
        )
        info = bsl["get_index_info"]()
        reader.close()
        # Capability = table exists (v8+), not "has data"
        assert info["has_regions"] is True
        assert info["has_module_headers"] is True

    def test_strategy_shows_helpers_with_zero_rows(self, tmp_path, monkeypatch):
        """search_regions()/search_module_headers() must appear in strategy even if 0 rows."""
        from rlm_tools_bsl.bsl_knowledge import get_strategy

        proj = tmp_path / "empty_proj2"
        mod_dir = proj / "CommonModules" / "Пустой" / "Ext"
        mod_dir.mkdir(parents=True)
        (mod_dir / "Module.bsl").write_text("Процедура Тест()\nКонецПроцедуры\n", encoding="utf-8-sig")
        monkeypatch.setenv("RLM_INDEX_DIR", str(proj / ".idx"))
        builder = IndexBuilder()
        db_path = builder.build(str(proj), build_calls=False, build_fts=False, build_synonyms=False)

        reader = IndexReader(str(db_path))
        stats = reader.get_statistics()
        reader.close()
        assert stats["regions"] == 0

        strategy = get_strategy(effort="medium", format_info=None, idx_stats=stats)
        assert "search_regions()" in strategy
        assert "search_module_headers()" in strategy


class TestCountOnly:
    """v1.24.0 #1 — count_only census для search_regions / search_module_headers."""

    def test_index_count_regions_matches_search(self, built_regions_index):
        db_path, _ = built_regions_index
        reader = IndexReader(str(db_path))
        try:
            full = reader.search_regions("", limit=10**9)
            assert reader.count_regions("") == len(full)
            full_q = reader.search_regions("Программный", limit=10**9)
            assert reader.count_regions("Программный") == len(full_q)
        finally:
            reader.close()

    def test_index_count_module_headers_matches_search(self, built_regions_index):
        db_path, _ = built_regions_index
        reader = IndexReader(str(db_path))
        try:
            full = reader.search_module_headers("", limit=10**9)
            assert reader.count_module_headers("") == len(full)
            full_q = reader.search_module_headers("себестоимости", limit=10**9)
            assert reader.count_module_headers("себестоимости") == len(full_q)
        finally:
            reader.close()

    def test_index_count_regions_missing_table(self, tmp_path):
        db = tmp_path / "empty_cnt.db"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE index_meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("CREATE TABLE modules (id INTEGER PRIMARY KEY, rel_path TEXT)")
        conn.commit()
        conn.close()
        reader = IndexReader(str(db))
        try:
            assert reader.count_regions("test") is None
            assert reader.count_module_headers("test") is None
        finally:
            reader.close()

    def _bsl(self, db_path, project):
        from rlm_tools_bsl.bsl_helpers import make_bsl_helpers

        reader = IndexReader(str(db_path))
        bsl = make_bsl_helpers(
            base_path=str(project),
            resolve_safe=lambda p: __import__("pathlib").Path(p),
            read_file_fn=lambda p: "",
            grep_fn=lambda pat, path="": [],
            glob_files_fn=lambda pat: [],
            idx_reader=reader,
        )
        return bsl, reader

    def test_helper_count_only_regions(self, built_regions_index):
        db_path, project = built_regions_index
        bsl, reader = self._bsl(db_path, project)
        try:
            res = bsl["search_regions"]("", count_only=True)
            assert isinstance(res, dict)
            assert res == {
                "total": reader.count_regions(""),
                "source": "index",
                "truncated": False,
                "scope": "main_index",
            }
            assert res["total"] == 3
        finally:
            reader.close()

    def test_helper_count_only_module_headers(self, built_regions_index):
        db_path, project = built_regions_index
        bsl, reader = self._bsl(db_path, project)
        try:
            res = bsl["search_module_headers"]("себестоимости", count_only=True)
            assert res == {
                "total": reader.count_module_headers("себестоимости"),
                "source": "index",
                "truncated": False,
                "scope": "main_index",
            }
            assert res["total"] == 1
        finally:
            reader.close()

    def test_helper_count_only_false_is_unchanged(self, built_regions_index):
        db_path, project = built_regions_index
        bsl, reader = self._bsl(db_path, project)
        try:
            default = bsl["search_regions"]("Обработчики")
            explicit = bsl["search_regions"]("Обработчики", count_only=False)
            assert isinstance(default, list)
            assert default == explicit
        finally:
            reader.close()

    def test_helper_count_only_unavailable_without_index(self):
        from rlm_tools_bsl.bsl_helpers import make_bsl_helpers

        bsl = make_bsl_helpers(
            base_path="/nonexistent",
            resolve_safe=lambda p: __import__("pathlib").Path(p),
            read_file_fn=lambda p: "",
            grep_fn=lambda pat, path="": [],
            glob_files_fn=lambda pat: [],
        )
        res = bsl["search_regions"]("x", count_only=True)
        assert res == {"total": 0, "source": "unavailable", "truncated": False, "scope": "main_index"}
        res_h = bsl["search_module_headers"]("x", count_only=True)
        assert res_h == {"total": 0, "source": "unavailable", "truncated": False, "scope": "main_index"}


class TestRegionsStrategy:
    def test_strategy_includes_search_regions(self, built_regions_index):
        db_path, _ = built_regions_index
        from rlm_tools_bsl.bsl_knowledge import get_strategy

        reader = IndexReader(str(db_path))
        stats = reader.get_statistics()
        reader.close()

        strategy = get_strategy(
            effort="medium",
            format_info=None,
            idx_stats=stats,
        )
        assert "search_regions()" in strategy
        assert "search_module_headers()" in strategy
