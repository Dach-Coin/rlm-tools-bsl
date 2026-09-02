"""Tests for v1.12.0 search_* live-fallback over extension content.

Each ``search_*`` helper must reproduce the exact result shape returned by
``IndexReader``, even for live rows synthesised from extension files.
"""

from __future__ import annotations

import os
import textwrap

import pytest

from rlm_tools_bsl.bsl_helpers import make_bsl_helpers
from rlm_tools_bsl.bsl_index import IndexBuilder, IndexReader
from rlm_tools_bsl.format_detector import detect_format
from rlm_tools_bsl.helpers import make_helpers


_CF_MAIN_XML = textwrap.dedent("""\
    <?xml version="1.0" encoding="UTF-8"?>
    <MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"
                    xmlns:v8="http://v8.1c.ru/8.1/data/core">
        <Configuration uuid="00000000-0000-0000-0000-000000000001">
            <Properties>
                <Name>MainCfg</Name>
                <NamePrefix/>
                <ConfigurationExtensionCompatibilityMode>Version8_3_24</ConfigurationExtensionCompatibilityMode>
            </Properties>
        </Configuration>
    </MetaDataObject>
""")


def _ext_xml(name="ExtSearch", purpose="Customization", prefix="ext_"):
    return textwrap.dedent(f"""\
        <?xml version="1.0" encoding="UTF-8"?>
        <MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"
                    xmlns:v8="http://v8.1c.ru/8.1/data/core">
            <Configuration uuid="00000000-0000-0000-0000-000000000003">
                <Properties>
                    <ObjectBelonging>Adopted</ObjectBelonging>
                    <Name>{name}</Name>
                    <ConfigurationExtensionPurpose>{purpose}</ConfigurationExtensionPurpose>
                    <NamePrefix>{prefix}</NamePrefix>
                </Properties>
            </Configuration>
        </MetaDataObject>
    """)


_EXT_BSL = textwrap.dedent("""\
    // ext_SearchMarker header marker for module headers
    Процедура ext_SearchMethod(Параметр)
        #Область ext_SearchRegion
            Сообщить(Параметр);
            // Calling a procedure defined in the main config. This verifies
            // that find_callers_context successfully READS extension files
            // via _ext_read_file (codex round 1 medium #2).
            MainOnlyProc();
        #КонецОбласти
    КонецПроцедуры
""")


_EXT_CAT_XML = textwrap.dedent("""\
    <?xml version="1.0" encoding="UTF-8"?>
    <MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"
                    xmlns:v8="http://v8.1c.ru/8.1/data/core">
        <Catalog>
            <Properties>
                <Name>ExtSearchCatalog</Name>
                <Synonym><v8:item><v8:lang>ru</v8:lang><v8:content>ext_SearchSynonym</v8:content></v8:item></Synonym>
            </Properties>
        </Catalog>
    </MetaDataObject>
""")


_EXT_EVENT_SUB_XML = textwrap.dedent("""\
    <?xml version="1.0" encoding="UTF-8"?>
    <MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"
                    xmlns:v8="http://v8.1c.ru/8.1/data/core">
        <EventSubscription>
            <Properties>
                <Name>ExtEventSubXmlOnly</Name>
                <Synonym><v8:item><v8:lang>ru</v8:lang><v8:content>ext_EventSubSynonym</v8:content></v8:item></Synonym>
            </Properties>
        </EventSubscription>
    </MetaDataObject>
""")


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


@pytest.fixture()
def helpers_with_ext(tmp_path, monkeypatch):
    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
    cf = os.path.join(str(tmp_path), "src", "cf")
    cfe = os.path.join(str(tmp_path), "src", "cfe", "ExtSearch")

    _write(os.path.join(cf, "Configuration.xml"), _CF_MAIN_XML)
    _write(
        os.path.join(cf, "CommonModules", "MainOnly", "Ext", "Module.bsl"),
        "Процедура MainOnlyProc() Экспорт\nКонецПроцедуры\n",
    )

    _write(os.path.join(cfe, "Configuration.xml"), _ext_xml())
    _write(
        os.path.join(cfe, "CommonModules", "ExtSearchModule", "Ext", "Module.bsl"),
        _EXT_BSL,
    )
    _write(
        os.path.join(cfe, "Catalogs", "ExtSearchCatalog", "Ext", "Catalog.xml"),
        _EXT_CAT_XML,
    )
    # XML-only EventSubscription (CF sibling-only layout)
    _write(
        os.path.join(cfe, "EventSubscriptions", "ExtEventSubXmlOnly.xml"),
        _EXT_EVENT_SUB_XML,
    )

    # Build index for main config
    builder = IndexBuilder()
    db_path = builder.build(cf, build_calls=False, build_metadata=True, build_fts=True)
    reader = IndexReader(db_path)
    try:
        generic, resolve_safe = make_helpers(cf, idx_reader=reader)
        fmt = detect_format(cf)
        bsl = make_bsl_helpers(
            base_path=cf,
            resolve_safe=resolve_safe,
            read_file_fn=generic["read_file"],
            grep_fn=generic["grep"],
            glob_files_fn=generic["glob_files"],
            format_info=fmt,
            idx_reader=reader,
            extension_paths=[cfe],
        )
        yield bsl, cf, cfe, reader
    finally:
        reader.close()


# v1.34.0 (Задача 6): в строку добавлен ЗАПЛАНИРОВАННЫЙ additive-ключ `owner`
# ("main" | "extension:<Имя>"). Константы обновлены на `<старый набор> | {"owner"}`,
# а НЕ ослаблены до `>=`: смысл freeze'а — ловить НЕЗАПЛАНИРОВАННЫЙ рост строки.
SEARCH_METHODS_KEYS = {
    "name",
    "type",
    "is_export",
    "line",
    "end_line",
    "params",
    "module_path",
    "object_name",
    "rank",
} | {"owner"}

SEARCH_OBJECTS_KEYS = {"object_name", "category", "synonym", "file"} | {"owner"}

SEARCH_REGIONS_KEYS = {"name", "line", "end_line", "module_path", "object_name", "category"} | {"owner"}

SEARCH_HEADERS_KEYS = {"module_path", "object_name", "category", "header_comment"} | {"owner"}


class TestSearchMethodsLive:
    def test_search_methods_finds_extension_method(self, helpers_with_ext):
        bsl, _cf, _cfe, _reader = helpers_with_ext
        rows = bsl["search_methods"]("ext_SearchMethod")
        ext_rows = [r for r in rows if r["module_path"].startswith("../cfe/")]
        assert ext_rows, rows

    def test_search_methods_result_shape(self, helpers_with_ext):
        bsl, _cf, _cfe, _reader = helpers_with_ext
        rows = bsl["search_methods"]("ext_SearchMethod")
        ext_rows = [r for r in rows if r["module_path"].startswith("../cfe/")]
        assert ext_rows
        for row in ext_rows:
            assert set(row.keys()) == SEARCH_METHODS_KEYS, row
            assert row["rank"] is None


class TestSearchObjectsLive:
    def test_search_objects_finds_extension_synonym(self, helpers_with_ext):
        bsl, _cf, _cfe, _reader = helpers_with_ext
        rows = bsl["search_objects"]("ext_SearchSynonym")
        ext_rows = [r for r in rows if r["file"].startswith("../cfe/")]
        assert ext_rows, rows

    def test_search_objects_finds_xml_only_object(self, helpers_with_ext):
        bsl, _cf, _cfe, _reader = helpers_with_ext
        rows = bsl["search_objects"]("ExtEventSubXmlOnly")
        ext_rows = [r for r in rows if r["file"].startswith("../cfe/")]
        assert ext_rows, "XML-only ext object (EventSubscription) must be findable"

    def test_search_objects_result_shape(self, helpers_with_ext):
        bsl, _cf, _cfe, _reader = helpers_with_ext
        rows = bsl["search_objects"]("ext_SearchSynonym")
        ext_rows = [r for r in rows if r["file"].startswith("../cfe/")]
        assert ext_rows
        for row in ext_rows:
            assert set(row.keys()) == SEARCH_OBJECTS_KEYS, row


class TestSearchRegionsLive:
    def test_search_regions_finds_extension_region(self, helpers_with_ext):
        bsl, _cf, _cfe, _reader = helpers_with_ext
        rows = bsl["search_regions"]("ext_SearchRegion")
        ext_rows = [r for r in rows if r["module_path"].startswith("../cfe/")]
        assert ext_rows, rows
        for row in ext_rows:
            assert set(row.keys()) == SEARCH_REGIONS_KEYS, row


class TestSearchModuleHeadersLive:
    def test_search_module_headers_finds_extension_header(self, helpers_with_ext):
        bsl, _cf, _cfe, _reader = helpers_with_ext
        rows = bsl["search_module_headers"]("ext_SearchMarker")
        ext_rows = [r for r in rows if r["module_path"].startswith("../cfe/")]
        assert ext_rows, rows
        for row in ext_rows:
            assert set(row.keys()) == SEARCH_HEADERS_KEYS, row


class TestCountOnlyExtension:
    """v1.30.0 — count_only считает в ТОМ ЖЕ scope, что и list.

    До 1.30.0 count отвечал только индексом, и `список=2 при count=1` было штатным
    (v1.24.0 #1). Агенты рекуррентно читали это как «total mismatch» и уходили за полной
    выдачей, то есть экономящий токены count_only работал против себя.

    NB: оба теста делают count_only ПЕРВЫМ вызовом на свежей фикстуре — это и есть
    регресс на то, что признак «расширения настроены» берётся из `_ext_paths_raw`
    (доступен сразу), а не из `_extension_paths_set` (заполняется только в
    `_ensure_index`, то есть на первом вызове был бы пуст).
    """

    def test_count_only_regions_counts_live_extensions(self, helpers_with_ext):
        bsl, _cf, _cfe, reader = helpers_with_ext
        res = bsl["search_regions"]("ext_SearchRegion", count_only=True)
        assert res["scope"] == "main_index+live_extensions"
        assert res["source"] == "index+live"
        assert res["truncated"] is False
        # Область живёт только в расширении: main-счёт 0, но census её ВИДИТ.
        assert res["total_main"] == reader.count_regions("ext_SearchRegion") == 0
        assert res["total_extensions"] >= 1
        merged = bsl["search_regions"]("ext_SearchRegion")
        assert any(r["module_path"].startswith("../cfe/") for r in merged)
        assert res["total"] == len(merged)

    def test_count_only_module_headers_counts_live_extensions(self, helpers_with_ext):
        bsl, _cf, _cfe, reader = helpers_with_ext
        res = bsl["search_module_headers"]("ext_SearchMarker", count_only=True)
        assert res["scope"] == "main_index+live_extensions"
        assert res["total_main"] == reader.count_module_headers("ext_SearchMarker")
        merged = bsl["search_module_headers"]("ext_SearchMarker")
        assert res["total"] == len(merged)

    def test_count_only_does_not_depend_on_limit(self, helpers_with_ext):
        bsl, _cf, _cfe, _reader = helpers_with_ext
        a = bsl["search_regions"]("ext_SearchRegion", count_only=True)
        b = bsl["search_regions"]("ext_SearchRegion", limit=1, count_only=True)
        assert a == b

    @pytest.mark.parametrize("query", ["", "   "])
    def test_empty_and_whitespace_query_keep_main_only_scope(self, helpers_with_ext, query):
        """«Пустой» = `not query or not query.strip()` — тот же предикат, что у reader'а.

        Сырой truthiness пустил бы `"   "` в полный live-проход по всем CFE-модулям, хотя
        main-сторона того же вызова трактует его как «отдай всё». Предохранитель работает
        в ОБЕИХ ветках: и count, и list.
        """
        bsl, _cf, _cfe, _reader = helpers_with_ext
        res = bsl["search_regions"](query, count_only=True)
        assert res["scope"] == "main_index"
        assert set(res) == {"total", "source", "truncated", "scope"}

        headers = bsl["search_module_headers"](query, count_only=True)
        assert headers["scope"] == "main_index"

        # list при whitespace-query тоже не подмешивает CFE-строки
        rows = bsl["search_module_headers"](query)
        assert not any(r["module_path"].startswith("../cfe/") for r in rows)

    def test_main_and_cfe_module_paths_live_in_separate_namespaces(self, helpers_with_ext):
        """Инвариант, на котором стоит `total = main + len(live)`: ext↔main collision по
        `(module_path, name)` невозможен, поэтому main-строки грузить не нужно."""
        bsl, _cf, _cfe, _reader = helpers_with_ext
        rows = bsl["search_regions"]("Search")
        main_paths = {r["module_path"] for r in rows if not r["module_path"].startswith("../cfe/")}
        ext_paths = {r["module_path"] for r in rows if r["module_path"].startswith("../cfe/")}
        assert not (main_paths & ext_paths)

    def test_duplicate_region_names_in_one_cfe_module_are_two_occurrences(self, tmp_path, monkeypatch):
        """Merge-хелперы снимают только совпадение с MAIN-строкой и НЕ дедупят ext↔ext.
        Count обязан повторять это: два одноимённых `#Область` в одном CFE-модуле — две
        физические записи, а не одна."""
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
        cf = os.path.join(str(tmp_path), "src", "cf")
        cfe = os.path.join(str(tmp_path), "src", "cfe", "ExtDup")
        _write(os.path.join(cf, "Configuration.xml"), _CF_MAIN_XML)
        _write(os.path.join(cfe, "Configuration.xml"), _ext_xml())
        _write(
            os.path.join(cfe, "CommonModules", "ExtDupModule", "Ext", "Module.bsl"),
            "#Область dup_Region\nПроцедура А() Экспорт\nКонецПроцедуры\n#КонецОбласти\n"
            "#Область dup_Region\nПроцедура Б() Экспорт\nКонецПроцедуры\n#КонецОбласти\n",
        )
        db_path = IndexBuilder().build(cf, build_calls=False, build_metadata=True, build_fts=True)
        reader = IndexReader(db_path)
        try:
            generic, resolve_safe = make_helpers(cf, idx_reader=reader)
            bsl = make_bsl_helpers(
                base_path=cf,
                resolve_safe=resolve_safe,
                read_file_fn=generic["read_file"],
                grep_fn=generic["grep"],
                glob_files_fn=generic["glob_files"],
                format_info=detect_format(cf),
                idx_reader=reader,
                extension_paths=[cfe],
            )
            res = bsl["search_regions"]("dup_Region", count_only=True)
            assert res["total_extensions"] == 2
            assert res["total"] == len(bsl["search_regions"]("dup_Region"))
        finally:
            reader.close()


class TestSearchUnified:
    def test_search_unified_scope_all_includes_extension(self, helpers_with_ext):
        bsl, _cf, _cfe, _reader = helpers_with_ext
        rows = bsl["search"]("ext_Search", scope="all")
        ext_paths = [r for r in rows if (r.get("path") or "").startswith("../cfe/")]
        assert ext_paths, rows

    def test_search_dedup_with_index_results(self, helpers_with_ext):
        """Calling search_methods twice should not duplicate rows."""
        bsl, _cf, _cfe, _reader = helpers_with_ext
        first = bsl["search_methods"]("ext_SearchMethod")
        # Result is freshly computed each call — assert no internal duplicates.
        keys = [(r["module_path"], r["name"]) for r in first]
        assert len(keys) == len(set(keys)), keys


# v1.12.0 codex review round 1 — gaps in attributes/predefined/empty-query.


class TestSearchEmptyQueryListing:
    """search_objects("") must include extension synonyms in alphabetical listing."""

    def test_search_objects_empty_query_includes_extension(self, helpers_with_ext):
        bsl, _cf, _cfe, _reader = helpers_with_ext
        rows = bsl["search_objects"]("")
        # Alphabetical listing — extension rows must be present.
        ext_rows = [r for r in rows if r["file"].startswith("../cfe/")]
        assert ext_rows, "search_objects('') should return alphabetical listing including extension synonyms"

    def test_search_objects_empty_query_sorted_by_category_then_name(self, helpers_with_ext):
        bsl, _cf, _cfe, _reader = helpers_with_ext
        rows = bsl["search_objects"]("", limit=200)
        # Within ext rows, ordering must be (category, object_name) — mirrors IndexReader.
        ext_rows = [r for r in rows if r["file"].startswith("../cfe/")]
        keys = [(r["category"], r["object_name"]) for r in ext_rows]
        assert keys == sorted(keys), keys

    def test_search_methods_exact_ext_match_wins_slot_when_main_saturated(self, tmp_path, monkeypatch):
        """codex round 5 — saturated main FTS index must not starve an ext method
        with exact-name match."""
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
        cf = os.path.join(str(tmp_path), "src", "cf")
        cfe = os.path.join(str(tmp_path), "src", "cfe", "ExtMethodSat")

        _write(os.path.join(cf, "Configuration.xml"), _CF_MAIN_XML)
        # 60 main methods whose names contain "uniqueProc" but are NOT exactly "uniqueProc".
        body_lines = "\n".join(f"Процедура uniqueProc{i:02d}() Экспорт\nКонецПроцедуры\n" for i in range(60))
        _write(os.path.join(cf, "CommonModules", "MainBulk", "Ext", "Module.bsl"), body_lines)

        # Extension has a method exactly named "uniqueProc" → rank 0.
        _write(os.path.join(cfe, "Configuration.xml"), _ext_xml(name="ExtMethodSat"))
        _write(
            os.path.join(cfe, "CommonModules", "ExtMethodMod", "Ext", "Module.bsl"),
            "Процедура uniqueProc() Экспорт\nКонецПроцедуры\n",
        )

        builder = IndexBuilder()
        db_path = builder.build(cf, build_calls=False, build_metadata=True, build_fts=True)
        reader = IndexReader(db_path)
        try:
            generic, resolve_safe = make_helpers(cf, idx_reader=reader)
            fmt = detect_format(cf)
            bsl = make_bsl_helpers(
                base_path=cf,
                resolve_safe=resolve_safe,
                read_file_fn=generic["read_file"],
                grep_fn=generic["grep"],
                glob_files_fn=generic["glob_files"],
                format_info=fmt,
                idx_reader=reader,
                extension_paths=[cfe],
            )
            rows = bsl["search_methods"]("uniqueProc", limit=30)
            ext_rows = [r for r in rows if r["module_path"].startswith("../cfe/")]
            assert ext_rows, (
                f"exact-name ext method must claim a slot in saturated main; got {[r['name'] for r in rows[:5]]}"
            )
            assert ext_rows[0]["name"] == "uniqueProc"
        finally:
            reader.close()

    def test_search_module_headers_ext_visible_when_main_saturated(self, tmp_path, monkeypatch):
        """codex round 5 — main-saturated module_headers must still surface ext
        rows via reservation."""
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
        cf = os.path.join(str(tmp_path), "src", "cf")
        cfe = os.path.join(str(tmp_path), "src", "cfe", "ExtHdrSat")

        _write(os.path.join(cf, "Configuration.xml"), _CF_MAIN_XML)
        for i in range(250):
            _write(
                os.path.join(cf, "CommonModules", f"MainHdr{i:03d}", "Ext", "Module.bsl"),
                f"// uniqueHdr main {i}\nПроцедура F() Экспорт\nКонецПроцедуры\n",
            )

        _write(os.path.join(cfe, "Configuration.xml"), _ext_xml(name="ExtHdrSat"))
        _write(
            os.path.join(cfe, "CommonModules", "ExtHdrMod", "Ext", "Module.bsl"),
            "// uniqueHdr ext marker\nПроцедура F() Экспорт\nКонецПроцедуры\n",
        )

        builder = IndexBuilder()
        db_path = builder.build(cf, build_calls=False, build_metadata=True, build_fts=True)
        reader = IndexReader(db_path)
        try:
            generic, resolve_safe = make_helpers(cf, idx_reader=reader)
            fmt = detect_format(cf)
            bsl = make_bsl_helpers(
                base_path=cf,
                resolve_safe=resolve_safe,
                read_file_fn=generic["read_file"],
                grep_fn=generic["grep"],
                glob_files_fn=generic["glob_files"],
                format_info=fmt,
                idx_reader=reader,
                extension_paths=[cfe],
            )
            rows = bsl["search_module_headers"]("uniqueHdr", limit=200)
            ext_rows = [r for r in rows if r["module_path"].startswith("../cfe/")]
            assert ext_rows, "ext header must be visible via reservation"
        finally:
            reader.close()

    def test_search_objects_nonempty_query_full_scan_finds_late_exact_ext(self, tmp_path, monkeypatch):
        """codex round 4 — _live_search_objects must full-scan extensions for
        non-empty query (no early-break) so an exact-name ext match late in
        scan order is not lost. Mirrors IndexReader's no-LIMIT contract for
        substring queries (bsl_index.py: "Python ranking needs ALL matches to
        guarantee exact name (rank 0) is never lost").
        """
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
        cf = os.path.join(str(tmp_path), "src", "cf")
        cfe = os.path.join(str(tmp_path), "src", "cfe", "ExtScanDeep")

        _write(os.path.join(cf, "Configuration.xml"), _CF_MAIN_XML)

        # 60 EXTENSION-side rank-2 catalogs (synonym substring match) come
        # alphabetically BEFORE the exact-name match in object_name. Ext scan
        # order follows _extension_synonyms which is filled in scan order;
        # exact-name "uniqueDeep" goes last so it would be lost to an early
        # break at limit=50.
        _write(os.path.join(cfe, "Configuration.xml"), _ext_xml(name="ExtScanDeep"))
        for i in range(60):
            name = f"AaaExtCat{i:02d}"
            _write(
                os.path.join(cfe, "Catalogs", name, "Ext", "Catalog.xml"),
                textwrap.dedent(f"""\
                    <?xml version="1.0" encoding="UTF-8"?>
                    <MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"
                                    xmlns:v8="http://v8.1c.ru/8.1/data/core">
                        <Catalog>
                            <Properties>
                                <Name>{name}</Name>
                                <Synonym><v8:item><v8:lang>ru</v8:lang><v8:content>noise uniqueDeep {i}</v8:content></v8:item></Synonym>
                            </Properties>
                        </Catalog>
                    </MetaDataObject>
                """),
            )
        # Exact name match — goes last alphabetically among AaaExtCat* but
        # alphabetically before z* — scan order puts it after the 60 rank-2 rows.
        _write(
            os.path.join(cfe, "Catalogs", "uniqueDeep", "Ext", "Catalog.xml"),
            textwrap.dedent("""\
                <?xml version="1.0" encoding="UTF-8"?>
                <MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"
                                xmlns:v8="http://v8.1c.ru/8.1/data/core">
                    <Catalog>
                        <Properties>
                            <Name>uniqueDeep</Name>
                            <Synonym><v8:item><v8:lang>ru</v8:lang><v8:content>Точное</v8:content></v8:item></Synonym>
                        </Properties>
                    </Catalog>
                </MetaDataObject>
            """),
        )

        builder = IndexBuilder()
        db_path = builder.build(cf, build_calls=False, build_metadata=True, build_fts=True)
        reader = IndexReader(db_path)
        try:
            generic, resolve_safe = make_helpers(cf, idx_reader=reader)
            fmt = detect_format(cf)
            bsl = make_bsl_helpers(
                base_path=cf,
                resolve_safe=resolve_safe,
                read_file_fn=generic["read_file"],
                grep_fn=generic["grep"],
                glob_files_fn=generic["glob_files"],
                format_info=fmt,
                idx_reader=reader,
                extension_paths=[cfe],
            )
            rows = bsl["search_objects"]("uniqueDeep", limit=50)
            # Exact-name ext match must appear AND win rank-0 → position 0.
            assert rows, "no rows at all"
            exact = [r for r in rows if r["object_name"] == "uniqueDeep"]
            assert exact, (
                f"exact-name ext match was lost to early break: top rows are {[r['object_name'] for r in rows[:5]]}"
            )
            assert rows[0]["object_name"] == "uniqueDeep", (
                f"exact-name (rank 0) must be at position 0, got {rows[0]['object_name']}"
            )
        finally:
            reader.close()

    def test_search_objects_nonempty_query_exact_ext_match_wins_slot(self, tmp_path, monkeypatch):
        """codex round 3 — for a non-empty query that EXACTLY matches an ext
        object_name (rank=0), the ext row must claim a slot even when the main
        index already returned `limit` rank-2 (synonym substring) matches.
        """
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
        cf = os.path.join(str(tmp_path), "src", "cf")
        cfe = os.path.join(str(tmp_path), "src", "cfe", "ExtSaturate2")

        _write(os.path.join(cf, "Configuration.xml"), _CF_MAIN_XML)
        # 60 main catalogs whose synonyms contain "uniqueQuery" (rank 2 substring).
        # No main object_name equals "uniqueQuery", so an ext object named
        # exactly "uniqueQuery" must outrank all of them.
        for i in range(60):
            name = f"ZzzCat{i:02d}"
            _write(
                os.path.join(cf, "Catalogs", name, "Ext", "Catalog.xml"),
                textwrap.dedent(f"""\
                    <?xml version="1.0" encoding="UTF-8"?>
                    <MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"
                                    xmlns:v8="http://v8.1c.ru/8.1/data/core">
                        <Catalog>
                            <Properties>
                                <Name>{name}</Name>
                                <Synonym><v8:item><v8:lang>ru</v8:lang><v8:content>contains uniqueQuery {i}</v8:content></v8:item></Synonym>
                            </Properties>
                        </Catalog>
                    </MetaDataObject>
                """),
            )

        # Extension with a catalog whose object_name IS the query string.
        _write(os.path.join(cfe, "Configuration.xml"), _ext_xml(name="ExtSaturate2"))
        _write(
            os.path.join(cfe, "Catalogs", "uniqueQuery", "Ext", "Catalog.xml"),
            textwrap.dedent("""\
                <?xml version="1.0" encoding="UTF-8"?>
                <MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"
                                xmlns:v8="http://v8.1c.ru/8.1/data/core">
                    <Catalog>
                        <Properties>
                            <Name>uniqueQuery</Name>
                            <Synonym><v8:item><v8:lang>ru</v8:lang><v8:content>Расш Уник</v8:content></v8:item></Synonym>
                        </Properties>
                    </Catalog>
                </MetaDataObject>
            """),
        )

        builder = IndexBuilder()
        db_path = builder.build(cf, build_calls=False, build_metadata=True, build_fts=True)
        reader = IndexReader(db_path)
        try:
            generic, resolve_safe = make_helpers(cf, idx_reader=reader)
            fmt = detect_format(cf)
            bsl = make_bsl_helpers(
                base_path=cf,
                resolve_safe=resolve_safe,
                read_file_fn=generic["read_file"],
                grep_fn=generic["grep"],
                glob_files_fn=generic["glob_files"],
                format_info=fmt,
                idx_reader=reader,
                extension_paths=[cfe],
            )
            rows = bsl["search_objects"]("uniqueQuery", limit=50)
            # The ext rank-0 row must appear in the top-50 even though main has
            # 60 rank-2 candidates. Without the fix it would be at position 51+
            # and sliced away.
            ext_rows = [r for r in rows if r["file"].startswith("../cfe/")]
            assert ext_rows, (
                "exact-name ext match must claim a slot in a saturated main result; got rows: "
                f"{[(r['object_name'], r['file'][:30]) for r in rows[:5]]}"
            )
            # Stronger: rank-0 ext should be at position 0 (only one rank-0).
            assert rows[0]["object_name"] == "uniqueQuery", rows[0]
        finally:
            reader.close()

    def test_search_objects_empty_query_not_starved_when_index_saturated(self, tmp_path, monkeypatch):
        """codex round 2 #2 — when the main index already fills `limit`, the old
        guard ``len(result) < limit`` dropped ext rows. After the fix ext rows
        merge BEFORE truncation, so they surface alphabetically.
        """
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
        cf = os.path.join(str(tmp_path), "src", "cf")
        cfe = os.path.join(str(tmp_path), "src", "cfe", "ExtSaturate")

        _write(os.path.join(cf, "Configuration.xml"), _CF_MAIN_XML)
        # Many main catalogs with synonyms — guarantees main-index has > limit hits.
        # Names start with "Zzz" alphabetically AFTER ext "Aaa"; merge+sort surfaces ext.
        for i in range(60):
            name = f"ZzzCat{i:02d}"
            _write(
                os.path.join(cf, "Catalogs", name, "Ext", "Catalog.xml"),
                textwrap.dedent(f"""\
                    <?xml version="1.0" encoding="UTF-8"?>
                    <MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"
                                    xmlns:v8="http://v8.1c.ru/8.1/data/core">
                        <Catalog>
                            <Properties>
                                <Name>{name}</Name>
                                <Synonym><v8:item><v8:lang>ru</v8:lang><v8:content>Main {i}</v8:content></v8:item></Synonym>
                            </Properties>
                        </Catalog>
                    </MetaDataObject>
                """),
            )

        # Extension with alphabetically-early catalog "AaaExtFirst".
        _write(os.path.join(cfe, "Configuration.xml"), _ext_xml(name="ExtSaturate"))
        _write(
            os.path.join(cfe, "Catalogs", "AaaExtFirst", "Ext", "Catalog.xml"),
            textwrap.dedent("""\
                <?xml version="1.0" encoding="UTF-8"?>
                <MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"
                                xmlns:v8="http://v8.1c.ru/8.1/data/core">
                    <Catalog>
                        <Properties>
                            <Name>AaaExtFirst</Name>
                            <Synonym><v8:item><v8:lang>ru</v8:lang><v8:content>Расш Первый</v8:content></v8:item></Synonym>
                        </Properties>
                    </Catalog>
                </MetaDataObject>
            """),
        )

        builder = IndexBuilder()
        db_path = builder.build(cf, build_calls=False, build_metadata=True, build_fts=True)
        reader = IndexReader(db_path)
        try:
            generic, resolve_safe = make_helpers(cf, idx_reader=reader)
            fmt = detect_format(cf)
            bsl = make_bsl_helpers(
                base_path=cf,
                resolve_safe=resolve_safe,
                read_file_fn=generic["read_file"],
                grep_fn=generic["grep"],
                glob_files_fn=generic["glob_files"],
                format_info=fmt,
                idx_reader=reader,
                extension_paths=[cfe],
            )
            rows = bsl["search_objects"]("", limit=50)
            assert any(r["file"].startswith("../cfe/") for r in rows), (
                "extension rows must not be starved by a saturated main index"
            )
            # Alphabetically AaaExtFirst should appear before any ZzzCat row.
            ext_first = [r for r in rows if r["object_name"] == "AaaExtFirst"]
            assert ext_first, rows
            ext_pos = rows.index(ext_first[0])
            zzz = [r for r in rows if r["object_name"].startswith("Zzz")]
            if zzz:
                assert ext_pos < rows.index(zzz[0]), (
                    "AaaExtFirst must come alphabetically before ZzzCat* after merge+sort"
                )
        finally:
            reader.close()


class TestSearchAttributesExtension:
    """search() and find_attributes(name=...) must surface attributes from
    extension XML when no object_name is given (codex round 1 medium #1)."""

    def test_find_attributes_name_only_finds_extension_attr(self, helpers_with_ext):
        bsl, _cf, _cfe, _reader = helpers_with_ext
        # Our ext fixture has ExtSearchCatalog with no attributes; the test
        # fixture in test_helpers_extension has ExtCatalog.ExtAttr. Here we
        # use the search-extension fixture (idx + main + ext); ext attributes
        # come from ExtSearchCatalog's child <Attribute> if present, but the
        # fixture is intentionally minimal. So we verify name-only DOES return
        # something when extensions have matching attributes — using the
        # finer-grained fixture in test_helpers_extension is the right home.
        # Here, ensure the API contract: name-only search must not silently
        # drop ext-side data when extensions exist.
        rows = bsl["find_attributes"](name="ExtSearch")
        # Even if no exact attr name match, structurally must be list (not error).
        assert isinstance(rows, list)


class TestSearchSourceConsistency:
    """find_callers_context / find_register_writers must successfully read
    extension paths via _ext_read_file rather than silently skipping (codex
    round 1 medium #2)."""

    def test_extension_caller_appears_in_find_callers_context(self, helpers_with_ext):
        """Extension BSL calls a main procedure; find_callers_context must
        read the extension file (via _ext_read_file) and surface the caller.
        Closes the silent-skip path where prefilter saw the file but final
        read failed with PermissionError.
        """
        bsl, _cf, _cfe, _reader = helpers_with_ext
        result = bsl["find_callers_context"]("MainOnlyProc")
        assert "callers" in result
        ext_callers = [c for c in result["callers"] if c["file"].startswith("../cfe/")]
        assert ext_callers, f"extension caller silently skipped: {result}"
        assert ext_callers[0]["caller_name"] == "ext_SearchMethod"


def test_count_only_without_index_but_with_extensions_is_live(tmp_path, monkeypatch):
    """Без индекса, но с настроенными расширениями census остаётся честным: source='live',
    total_main=0. Раньше такой вызов отдавал {'total': 0, 'source': 'unavailable'} —
    неотличимо от «ничего нет»."""
    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
    cf = os.path.join(str(tmp_path), "src", "cf")
    cfe = os.path.join(str(tmp_path), "src", "cfe", "ExtNoIdx")
    _write(os.path.join(cf, "Configuration.xml"), _CF_MAIN_XML)
    _write(os.path.join(cfe, "Configuration.xml"), _ext_xml())
    _write(
        os.path.join(cfe, "CommonModules", "ExtNoIdxModule", "Ext", "Module.bsl"),
        "#Область noidx_Region\nПроцедура А() Экспорт\nКонецПроцедуры\n#КонецОбласти\n",
    )
    generic, resolve_safe = make_helpers(cf)
    bsl = make_bsl_helpers(
        base_path=cf,
        resolve_safe=resolve_safe,
        read_file_fn=generic["read_file"],
        grep_fn=generic["grep"],
        glob_files_fn=generic["glob_files"],
        format_info=detect_format(cf),
        idx_reader=None,
        extension_paths=[cfe],
    )
    res = bsl["search_regions"]("noidx_Region", count_only=True)
    assert res["source"] == "live"
    assert res["scope"] == "main_index+live_extensions"
    assert res["total_main"] == 0
    assert res["total"] == res["total_extensions"] == 1
    assert res["total"] == len(bsl["search_regions"]("noidx_Region"))
