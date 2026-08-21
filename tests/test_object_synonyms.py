"""Tests for v1.4.1 Object Synonyms feature.

Tests: collector, IndexReader.search_objects(), helpers, strategy,
CLI --no-synonyms flag, incremental update migration, Cyrillic case-insensitive.
"""

import sqlite3

import pytest

from rlm_tools_bsl.bsl_index import (
    BUILDER_VERSION,
    IndexBuilder,
    IndexReader,
    _CATEGORY_RU,
    _SYNONYM_CATEGORIES,
    _collect_object_synonyms,
)


# ---------------------------------------------------------------------------
# XML fixtures (CF and EDT formats)
# ---------------------------------------------------------------------------

_CF_DOCUMENT_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"
                xmlns:mdclass="http://v8.1c.ru/8.3/MDClasses"
                xmlns:v8="http://v8.1c.ru/8.1/data/core">
<Document>
  <Properties>
    <Name>АвансовыйОтчет</Name>
    <Synonym>
      <v8:item>
        <v8:lang>ru</v8:lang>
        <v8:content>Авансовый отчет</v8:content>
      </v8:item>
    </Synonym>
  </Properties>
</Document>
</MetaDataObject>
"""

_CF_COMMON_MODULE_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"
                xmlns:mdclass="http://v8.1c.ru/8.3/MDClasses"
                xmlns:v8="http://v8.1c.ru/8.1/data/core">
<CommonModule>
  <Properties>
    <Name>РасчетСебестоимости</Name>
    <Synonym>
      <v8:item>
        <v8:lang>ru</v8:lang>
        <v8:content>Расчет себестоимости</v8:content>
      </v8:item>
    </Synonym>
  </Properties>
</CommonModule>
</MetaDataObject>
"""

_CF_REGISTER_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"
                xmlns:mdclass="http://v8.1c.ru/8.3/MDClasses"
                xmlns:v8="http://v8.1c.ru/8.1/data/core">
<InformationRegister>
  <Properties>
    <Name>КурсыВалют</Name>
    <Synonym>
      <v8:item>
        <v8:lang>ru</v8:lang>
        <v8:content>Курсы валют</v8:content>
      </v8:item>
    </Synonym>
  </Properties>
</InformationRegister>
</MetaDataObject>
"""

_EDT_DOCUMENT_MDO = """\
<?xml version="1.0" encoding="UTF-8"?>
<mdclass:Document xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass"
                  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <name>РеализацияТоваров</name>
  <synonym>
    <key>ru</key>
    <value>Реализация товаров и услуг</value>
  </synonym>
</mdclass:Document>
"""

_EDT_CATALOG_MDO = """\
<?xml version="1.0" encoding="UTF-8"?>
<mdclass:Catalog xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass">
  <name>Контрагенты</name>
  <synonym>
    <key>ru</key>
    <value>Контрагенты</value>
  </synonym>
</mdclass:Catalog>
"""

_CF_NO_SYNONYM_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"
                xmlns:mdclass="http://v8.1c.ru/8.3/MDClasses">
<CommonModule>
  <Properties>
    <Name>ПустойМодуль</Name>
  </Properties>
</CommonModule>
</MetaDataObject>
"""


# ---------------------------------------------------------------------------
# Helpers: create test fixture with metadata files
# ---------------------------------------------------------------------------


def _make_synonym_fixture_cf(tmp_path):
    """Create CF-format project with metadata files that have synonyms."""
    # Documents/АвансовыйОтчет/Ext/ObjectModule.bsl + metadata XML
    doc_dir = tmp_path / "Documents" / "АвансовыйОтчет"
    ext_dir = doc_dir / "Ext"
    ext_dir.mkdir(parents=True)
    (ext_dir / "ObjectModule.bsl").write_text("Процедура Тест() Экспорт\nКонецПроцедуры\n", encoding="utf-8-sig")
    (ext_dir / "Document.xml").write_text(_CF_DOCUMENT_XML, encoding="utf-8")

    # CommonModules/РасчетСебестоимости/Ext/Module.bsl + metadata XML
    cm_dir = tmp_path / "CommonModules" / "РасчетСебестоимости" / "Ext"
    cm_dir.mkdir(parents=True)
    (cm_dir / "Module.bsl").write_text("Процедура Рассчитать() Экспорт\nКонецПроцедуры\n", encoding="utf-8-sig")
    (cm_dir / "Module.xml").write_text(_CF_COMMON_MODULE_XML, encoding="utf-8")

    # InformationRegisters/КурсыВалют/Ext/RecordSetModule.bsl + XML
    reg_dir = tmp_path / "InformationRegisters" / "КурсыВалют" / "Ext"
    reg_dir.mkdir(parents=True)
    (reg_dir / "RecordSetModule.bsl").write_text(
        "Процедура ПриЗаписи(Отказ) Экспорт\nКонецПроцедуры\n", encoding="utf-8-sig"
    )
    (reg_dir / "InformationRegister.xml").write_text(_CF_REGISTER_XML, encoding="utf-8")

    # CommonModules/ПустойМодуль — without synonym (edge case)
    empty_dir = tmp_path / "CommonModules" / "ПустойМодуль" / "Ext"
    empty_dir.mkdir(parents=True)
    (empty_dir / "Module.bsl").write_text("Процедура Пусто()\nКонецПроцедуры\n", encoding="utf-8-sig")
    (empty_dir / "Module.xml").write_text(_CF_NO_SYNONYM_XML, encoding="utf-8")

    return tmp_path


def _make_synonym_fixture_edt(tmp_path):
    """Create EDT-format project with .mdo files."""
    # Documents/РеализацияТоваров/РеализацияТоваров.mdo
    doc_dir = tmp_path / "Documents" / "РеализацияТоваров"
    doc_dir.mkdir(parents=True)
    (doc_dir / "РеализацияТоваров.mdo").write_text(_EDT_DOCUMENT_MDO, encoding="utf-8")
    # Need at least one .bsl file for the builder
    ext = doc_dir / "Ext"
    ext.mkdir()
    (ext / "ObjectModule.bsl").write_text("Процедура Тест() Экспорт\nКонецПроцедуры\n", encoding="utf-8-sig")

    # Catalogs/Контрагенты/Контрагенты.mdo
    cat_dir = tmp_path / "Catalogs" / "Контрагенты"
    cat_dir.mkdir(parents=True)
    (cat_dir / "Контрагенты.mdo").write_text(_EDT_CATALOG_MDO, encoding="utf-8")
    ext2 = cat_dir / "Ext"
    ext2.mkdir()
    (ext2 / "ManagerModule.bsl").write_text("Функция Тест() Экспорт\nВозврат 1;\nКонецФункции\n", encoding="utf-8-sig")

    return tmp_path


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def cf_project(tmp_path):
    return _make_synonym_fixture_cf(tmp_path)


@pytest.fixture
def edt_project(tmp_path):
    return _make_synonym_fixture_edt(tmp_path)


@pytest.fixture
def built_cf_index(cf_project, monkeypatch):
    monkeypatch.setenv("RLM_INDEX_DIR", str(cf_project / ".idx"))
    builder = IndexBuilder()
    db_path = builder.build(str(cf_project), build_calls=False, build_fts=False)
    return db_path, str(cf_project)


@pytest.fixture
def built_edt_index(edt_project, monkeypatch):
    monkeypatch.setenv("RLM_INDEX_DIR", str(edt_project / ".idx"))
    builder = IndexBuilder()
    db_path = builder.build(str(edt_project), build_calls=False, build_fts=False)
    return db_path, str(edt_project)


# ---------------------------------------------------------------------------
# _CATEGORY_RU and _SYNONYM_CATEGORIES
# ---------------------------------------------------------------------------


class TestCategoryMapping:
    def test_category_ru_has_all_expected(self):
        assert "CommonModules" in _CATEGORY_RU
        assert "Documents" in _CATEGORY_RU
        assert "Catalogs" in _CATEGORY_RU
        assert "InformationRegisters" in _CATEGORY_RU
        assert "EventSubscriptions" in _CATEGORY_RU
        assert "ScheduledJobs" in _CATEGORY_RU
        assert "FunctionalOptions" in _CATEGORY_RU

    def test_synonym_categories_matches_keys(self):
        assert _SYNONYM_CATEGORIES == frozenset(_CATEGORY_RU.keys())

    def test_category_ru_at_least_32(self):
        assert len(_CATEGORY_RU) >= 32

    def test_russian_labels(self):
        assert _CATEGORY_RU["Documents"] == "Документ"
        assert _CATEGORY_RU["CommonModules"] == "Общий модуль"
        assert _CATEGORY_RU["InformationRegisters"] == "Регистр сведений"


# ---------------------------------------------------------------------------
# _collect_object_synonyms
# ---------------------------------------------------------------------------


class TestCollector:
    def test_collector_cf_finds_synonyms(self, cf_project):
        results = _collect_object_synonyms(str(cf_project))
        names = {r[0] for r in results}
        assert "АвансовыйОтчет" in names
        assert "РасчетСебестоимости" in names
        assert "КурсыВалют" in names

    def test_collector_cf_skips_empty_synonym(self, cf_project):
        results = _collect_object_synonyms(str(cf_project))
        names = {r[0] for r in results}
        assert "ПустойМодуль" not in names

    def test_collector_cf_category_prefix(self, cf_project):
        results = _collect_object_synonyms(str(cf_project))
        by_name = {r[0]: r for r in results}
        # Documents → "Документ: ..."
        assert by_name["АвансовыйОтчет"][2].startswith("Документ: ")
        # CommonModules → "Общий модуль: ..."
        assert by_name["РасчетСебестоимости"][2].startswith("Общий модуль: ")
        # InformationRegisters → "Регистр сведений: ..."
        assert by_name["КурсыВалют"][2].startswith("Регистр сведений: ")

    def test_collector_edt_finds_synonyms(self, edt_project):
        results = _collect_object_synonyms(str(edt_project))
        names = {r[0] for r in results}
        assert "РеализацияТоваров" in names
        assert "Контрагенты" in names

    def test_collector_edt_category_prefix(self, edt_project):
        results = _collect_object_synonyms(str(edt_project))
        by_name = {r[0]: r for r in results}
        assert by_name["РеализацияТоваров"][2] == "Документ: Реализация товаров и услуг"
        assert by_name["Контрагенты"][2] == "Справочник: Контрагенты"

    def test_collector_returns_tuples_with_4_elements(self, cf_project):
        results = _collect_object_synonyms(str(cf_project))
        for r in results:
            assert len(r) == 4
            assert r[0]  # object_name
            assert r[1]  # category
            assert r[2]  # synonym (prefixed)
            assert r[3]  # file path

    def test_collector_category_diversity(self, cf_project):
        results = _collect_object_synonyms(str(cf_project))
        categories = {r[1] for r in results}
        assert "Documents" in categories
        assert "CommonModules" in categories
        assert "InformationRegisters" in categories

    def test_collector_empty_dir(self, tmp_path):
        results = _collect_object_synonyms(str(tmp_path))
        assert results == []

    def test_collector_skips_malformed_xml_does_not_crash(self, tmp_path):
        """BUG-3 (round 13 e2e): один битый/пустой/обрезанный XML НЕ должен
        ронять весь _collect_object_synonyms — раньше rlm-bsl-index update
        падал с xml.etree.ElementTree.ParseError, прерывая всю переиндексацию.

        Сейчас: парсер заворачивает Exception в warning и продолжает обход.
        """
        cat_dir = tmp_path / "Documents"
        cat_dir.mkdir()

        # Битый XML 1: полностью пустой файл (воспроизводит реальный кейс с одного из проектов)
        empty = cat_dir / "БитыйПустой"
        empty.mkdir()
        (empty / "БитыйПустой.mdo").write_text("", encoding="utf-8")

        # Битый XML 2: обрезанный
        truncated = cat_dir / "БитыйОбрезанный"
        truncated.mkdir()
        (truncated / "БитыйОбрезанный.mdo").write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n<mdclass:Document', encoding="utf-8"
        )

        # Корректный документ — должен пройти
        good = cat_dir / "ХорошийДокумент"
        good.mkdir()
        (good / "ХорошийДокумент.mdo").write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<mdclass:Document xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass">'
            "<name>ХорошийДокумент</name>"
            "<synonym><key>ru</key><value>Хороший</value></synonym>"
            "</mdclass:Document>",
            encoding="utf-8",
        )

        # Не должно бросить исключение
        results = _collect_object_synonyms(str(tmp_path))
        names = {r[0] for r in results}
        # Корректный документ найден; битые пропущены
        assert "ХорошийДокумент" in names
        assert "БитыйПустой" not in names
        assert "БитыйОбрезанный" not in names

    def test_collector_cf_sibling_only_layout(self, tmp_path):
        """CF sibling-only layout: Category/<Name>.xml без объектного подкаталога.

        Регрессионный тест (v1.9.3 finding на DO3): EventSubscriptions и другие
        категории в CF могут лежать прямо в категории файлами без подкаталога
        Name/. Старый collector итерировал только директории и пропускал такие
        объекты (на DO3: 229 в event_subscriptions, 0 в object_synonyms). Фикс:
        второй проход по plain .xml в cat_dir.
        """
        cat_dir = tmp_path / "Documents"
        cat_dir.mkdir()
        # Чистый sibling-only layout — НЕТ подкаталога ОрфанДокумент/
        (cat_dir / "ОрфанДокумент.xml").write_text(_CF_DOCUMENT_XML, encoding="utf-8-sig")

        results = _collect_object_synonyms(str(tmp_path))
        names = {r[0] for r in results}
        # _CF_DOCUMENT_XML содержит <Name>АвансовыйОтчет</Name> внутри XML,
        # collector использует stem файла как object_name → "ОрфанДокумент"
        assert "ОрфанДокумент" in names

        by_name = {r[0]: r for r in results}
        assert by_name["ОрфанДокумент"][1] == "Documents"
        assert by_name["ОрфанДокумент"][2].startswith("Документ: ")
        assert by_name["ОрфанДокумент"][3] == "Documents/ОрфанДокумент.xml"

    def test_collector_subsystems_sibling_only_recursive(self, tmp_path):
        """Codex Round 3: _collect_subsystems_recursive имел тот же sibling-only баг.

        Subsystems могут быть вложенными: Subsystems/Parent/Subsystems/Child/...
        Plain .xml на любом уровне иерархии ранее пропускался.
        """
        # Структура:
        #   Subsystems/Корневая.xml                  (sibling-only top-level)
        #   Subsystems/Контейнер/                    (dir с Subsystems/ внутри)
        #     Subsystems/Вложенная.xml               (sibling-only nested)
        sub_root = tmp_path / "Subsystems"
        sub_root.mkdir()
        (sub_root / "Корневая.xml").write_text(_CF_DOCUMENT_XML, encoding="utf-8-sig")

        container = sub_root / "Контейнер"
        container.mkdir()
        nested = container / "Subsystems"
        nested.mkdir()
        (nested / "Вложенная.xml").write_text(_CF_DOCUMENT_XML, encoding="utf-8-sig")

        results = _collect_object_synonyms(str(tmp_path))
        names = {r[0] for r in results}
        assert "Корневая" in names, f"top-level sibling-only Subsystem missed: {names}"
        assert "Вложенная" in names, f"nested sibling-only Subsystem missed: {names}"

    def test_collector_cf_sibling_no_double_count(self, tmp_path):
        """Если объект уже найден через подкаталог + sibling.xml, второй проход
        не должен добавить дубликат."""
        cat_dir = tmp_path / "Documents"
        obj_dir = cat_dir / "Контрагенты"
        # Подкаталог + sibling .xml одновременно — типичный CF dual layout
        obj_dir.mkdir(parents=True)
        (cat_dir / "Контрагенты.xml").write_text(_CF_DOCUMENT_XML, encoding="utf-8-sig")

        results = _collect_object_synonyms(str(tmp_path))
        kontr_rows = [r for r in results if r[0] == "Контрагенты"]
        assert len(kontr_rows) == 1, f"expected 1 row, got {len(kontr_rows)}: {kontr_rows}"


# ---------------------------------------------------------------------------
# IndexBuilder with synonyms
# ---------------------------------------------------------------------------


class TestBuildSynonyms:
    def test_build_creates_object_synonyms_table(self, built_cf_index):
        db_path, _ = built_cf_index
        conn = sqlite3.connect(str(db_path))
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        conn.close()
        assert "object_synonyms" in tables

    def test_build_populates_synonyms(self, built_cf_index):
        db_path, _ = built_cf_index
        conn = sqlite3.connect(str(db_path))
        count = conn.execute("SELECT COUNT(*) FROM object_synonyms").fetchone()[0]
        conn.close()
        assert count == 3  # АвансовыйОтчет, РасчетСебестоимости, КурсыВалют

    def test_build_has_synonyms_meta(self, built_cf_index):
        db_path, _ = built_cf_index
        conn = sqlite3.connect(str(db_path))
        row = conn.execute("SELECT value FROM index_meta WHERE key='has_synonyms'").fetchone()
        conn.close()
        assert row[0] == "1"

    def test_build_no_synonyms_flag(self, cf_project, monkeypatch):
        monkeypatch.setenv("RLM_INDEX_DIR", str(cf_project / ".idx_no_syn"))
        builder = IndexBuilder()
        db_path = builder.build(
            str(cf_project),
            build_calls=False,
            build_fts=False,
            build_synonyms=False,
        )
        conn = sqlite3.connect(str(db_path))
        count = conn.execute("SELECT COUNT(*) FROM object_synonyms").fetchone()[0]
        meta = conn.execute("SELECT value FROM index_meta WHERE key='has_synonyms'").fetchone()
        conn.close()
        assert count == 0
        assert meta[0] == "0"

    def test_builder_version_matches_constant(self, built_cf_index):
        db_path, _ = built_cf_index
        conn = sqlite3.connect(str(db_path))
        row = conn.execute("SELECT value FROM index_meta WHERE key='builder_version'").fetchone()
        conn.close()
        assert int(row[0]) == BUILDER_VERSION


# ---------------------------------------------------------------------------
# IndexReader.search_objects
# ---------------------------------------------------------------------------


class TestSearchObjects:
    def test_search_by_object_name(self, built_cf_index):
        db_path, _ = built_cf_index
        reader = IndexReader(db_path)
        results = reader.search_objects("АвансовыйОтчет")
        reader.close()
        assert len(results) >= 1
        assert results[0]["object_name"] == "АвансовыйОтчет"

    def test_search_by_synonym(self, built_cf_index):
        db_path, _ = built_cf_index
        reader = IndexReader(db_path)
        results = reader.search_objects("Авансовый отчет")
        reader.close()
        assert len(results) >= 1
        names = [r["object_name"] for r in results]
        assert "АвансовыйОтчет" in names

    def test_search_cyrillic_case_insensitive(self, built_cf_index):
        """CRITICAL: py_lower UDF must handle Cyrillic case."""
        db_path, _ = built_cf_index
        reader = IndexReader(db_path)
        results = reader.search_objects("расчет себестоимости")
        reader.close()
        assert len(results) >= 1
        names = [r["object_name"] for r in results]
        assert "РасчетСебестоимости" in names

    def test_search_objects_escapes_like_wildcards(self, built_cf_index):
        # '_' должен искаться ЛИТЕРАЛЬНО, а не как SQL single-char wildcard.
        # Без escape LIKE '%_%' матчит ВСЁ → ранжирование (else: rank=3) вернёт
        # произвольные объекты без '_'. С escape — только литеральные '_'.
        db_path, _ = built_cf_index
        reader = IndexReader(db_path)
        try:
            results = reader.search_objects("_")
            for r in results:
                assert "_" in r["object_name"] or "_" in (r["synonym"] or ""), r
        finally:
            reader.close()

    def test_search_by_category(self, built_cf_index):
        db_path, _ = built_cf_index
        reader = IndexReader(db_path)
        results = reader.search_objects("общий модуль")
        reader.close()
        assert len(results) >= 1
        # All results should be CommonModules
        for r in results:
            assert r["category"] == "CommonModules"

    def test_search_empty_query(self, built_cf_index):
        db_path, _ = built_cf_index
        reader = IndexReader(db_path)
        results = reader.search_objects("")
        reader.close()
        assert len(results) == 3  # all objects

    def test_search_no_match(self, built_cf_index):
        db_path, _ = built_cf_index
        reader = IndexReader(db_path)
        results = reader.search_objects("несуществующий")
        reader.close()
        assert results == []

    def test_search_returns_dict_keys(self, built_cf_index):
        db_path, _ = built_cf_index
        reader = IndexReader(db_path)
        results = reader.search_objects("Аванс")
        reader.close()
        assert len(results) >= 1
        r = results[0]
        assert "object_name" in r
        assert "category" in r
        assert "synonym" in r
        assert "file" in r

    def test_search_missing_table_returns_none(self, tmp_path, monkeypatch):
        """search_objects() on DB without table returns None."""
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / ".idx_empty"))
        db_path = tmp_path / ".idx_empty" / "test.db"
        db_path.parent.mkdir(parents=True)
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE index_meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.close()
        reader = IndexReader(db_path)
        result = reader.search_objects("test")
        reader.close()
        assert result is None

    def test_ranking_exact_name_first(self, built_cf_index):
        db_path, _ = built_cf_index
        reader = IndexReader(db_path)
        results = reader.search_objects("АвансовыйОтчет")
        reader.close()
        assert results[0]["object_name"] == "АвансовыйОтчет"

    def test_ranking_category_match(self, built_cf_index):
        """Category prefix match should rank lower than synonym match."""
        db_path, _ = built_cf_index
        reader = IndexReader(db_path)
        # "регистр" matches category prefix "Регистр сведений: ..."
        results = reader.search_objects("регистр")
        reader.close()
        assert len(results) >= 1


# ---------------------------------------------------------------------------
# IndexReader.get_statistics includes object_synonyms
# ---------------------------------------------------------------------------


class TestStatistics:
    def test_statistics_has_object_synonyms(self, built_cf_index):
        db_path, _ = built_cf_index
        reader = IndexReader(db_path)
        stats = reader.get_statistics()
        reader.close()
        assert "object_synonyms" in stats
        assert stats["object_synonyms"] == 3


# ---------------------------------------------------------------------------
# Helper search_objects() and get_index_info()
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_search_objects_helper(self, built_cf_index):
        from rlm_tools_bsl.bsl_helpers import make_bsl_helpers
        from rlm_tools_bsl.format_detector import detect_format

        db_path, base_path = built_cf_index
        reader = IndexReader(db_path)
        format_info = detect_format(base_path)
        bsl = make_bsl_helpers(
            base_path=base_path,
            resolve_safe=lambda p: __import__("pathlib").Path(base_path) / p,
            read_file_fn=lambda p: open(p, encoding="utf-8-sig").read(),
            grep_fn=lambda pat, path="": [],
            glob_files_fn=lambda pat: [],
            format_info=format_info,
            idx_reader=reader,
        )
        results = bsl["search_objects"]("Аванс")
        reader.close()
        assert len(results) >= 1
        assert results[0]["object_name"] == "АвансовыйОтчет"

    def test_search_objects_no_index(self):
        from rlm_tools_bsl.bsl_helpers import make_bsl_helpers

        bsl = make_bsl_helpers(
            base_path="/nonexistent",
            resolve_safe=lambda p: __import__("pathlib").Path(p),
            read_file_fn=lambda p: "",
            grep_fn=lambda pat, path="": [],
            glob_files_fn=lambda pat: [],
        )
        assert bsl["search_objects"]("test") == []

    def test_get_index_info_helper(self, built_cf_index):
        from rlm_tools_bsl.bsl_helpers import make_bsl_helpers
        from rlm_tools_bsl.format_detector import detect_format

        db_path, base_path = built_cf_index
        reader = IndexReader(db_path)
        format_info = detect_format(base_path)
        bsl = make_bsl_helpers(
            base_path=base_path,
            resolve_safe=lambda p: __import__("pathlib").Path(base_path) / p,
            read_file_fn=lambda p: open(p, encoding="utf-8-sig").read(),
            grep_fn=lambda pat, path="": [],
            glob_files_fn=lambda pat: [],
            format_info=format_info,
            idx_reader=reader,
        )
        info = bsl["get_index_info"]()
        reader.close()
        assert info["status"] == "ok"
        assert info["builder_version"] == BUILDER_VERSION
        assert info["has_synonyms"] is True

    def test_get_index_info_no_index(self):
        from rlm_tools_bsl.bsl_helpers import make_bsl_helpers

        bsl = make_bsl_helpers(
            base_path="/nonexistent",
            resolve_safe=lambda p: __import__("pathlib").Path(p),
            read_file_fn=lambda p: "",
            grep_fn=lambda pat, path="": [],
            glob_files_fn=lambda pat: [],
        )
        assert bsl["get_index_info"]()["status"] == "no_index"

    def test_helper_registered(self, built_cf_index):
        from rlm_tools_bsl.bsl_helpers import make_bsl_helpers
        from rlm_tools_bsl.format_detector import detect_format

        db_path, base_path = built_cf_index
        reader = IndexReader(db_path)
        format_info = detect_format(base_path)
        bsl = make_bsl_helpers(
            base_path=base_path,
            resolve_safe=lambda p: __import__("pathlib").Path(base_path) / p,
            read_file_fn=lambda p: open(p, encoding="utf-8-sig").read(),
            grep_fn=lambda pat, path="": [],
            glob_files_fn=lambda pat: [],
            format_info=format_info,
            idx_reader=reader,
        )
        reg = bsl["_registry"]
        reader.close()
        assert "search_objects" in reg
        assert "get_index_info" in reg
        assert reg["search_objects"]["cat"] == "discovery"
        assert reg["get_index_info"]["cat"] == "discovery"


# ---------------------------------------------------------------------------
# Strategy mentions search_objects
# ---------------------------------------------------------------------------


class TestStrategy:
    def test_strategy_mentions_search_objects(self):
        from rlm_tools_bsl.bsl_knowledge import get_strategy
        from rlm_tools_bsl.format_detector import FormatInfo, SourceFormat

        fi = FormatInfo(SourceFormat.CF, "/test", 10, True, ["Documents"])
        strategy = get_strategy(
            "high",
            fi,
            idx_stats={"methods": 100, "calls": 50, "object_synonyms": 500, "builder_version": "7"},
        )
        assert "search_objects" in strategy

    def test_strategy_shows_synonym_count(self):
        from rlm_tools_bsl.bsl_knowledge import get_strategy
        from rlm_tools_bsl.format_detector import FormatInfo, SourceFormat

        fi = FormatInfo(SourceFormat.CF, "/test", 10, True, ["Documents"])
        strategy = get_strategy(
            "high",
            fi,
            idx_stats={"methods": 100, "calls": 50, "object_synonyms": 1234, "builder_version": "7"},
        )
        assert "1234 synonyms" in strategy

    def test_workflow_has_search_objects(self):
        from rlm_tools_bsl.bsl_knowledge import _STRATEGY_HEADER

        assert "search_objects" in _STRATEGY_HEADER


# ---------------------------------------------------------------------------
# EDT format build
# ---------------------------------------------------------------------------


class TestEdtBuild:
    def test_edt_build_synonyms(self, built_edt_index):
        db_path, _ = built_edt_index
        reader = IndexReader(db_path)
        results = reader.search_objects("Реализация")
        reader.close()
        assert len(results) >= 1
        assert results[0]["object_name"] == "РеализацияТоваров"
        assert results[0]["synonym"] == "Документ: Реализация товаров и услуг"

    def test_edt_catalog_synonym(self, built_edt_index):
        db_path, _ = built_edt_index
        reader = IndexReader(db_path)
        results = reader.search_objects("контрагент")
        reader.close()
        assert len(results) >= 1
        assert results[0]["category"] == "Catalogs"


# ---------------------------------------------------------------------------
# Incremental update with synonyms
# ---------------------------------------------------------------------------


class TestIncrementalUpdate:
    def test_update_creates_synonyms_on_v6_index(self, cf_project, monkeypatch):
        """Simulate v6 index (no has_synonyms key) → update should build synonyms."""
        monkeypatch.setenv("RLM_INDEX_DIR", str(cf_project / ".idx_v6"))
        builder = IndexBuilder()
        # Build without synonyms to simulate v6
        db_path = builder.build(
            str(cf_project),
            build_calls=False,
            build_fts=False,
            build_synonyms=False,
        )
        # Remove has_synonyms key to simulate v6 index
        conn = sqlite3.connect(str(db_path))
        conn.execute("DELETE FROM index_meta WHERE key='has_synonyms'")
        conn.commit()
        conn.close()

        # Update should build synonyms (default=True when key missing)
        builder.update(str(cf_project))

        conn = sqlite3.connect(str(db_path))
        count = conn.execute("SELECT COUNT(*) FROM object_synonyms").fetchone()[0]
        conn.close()
        assert count == 3  # synonyms built

    def test_update_respects_no_synonyms(self, cf_project, monkeypatch):
        """Build with --no-synonyms → update should NOT build synonyms."""
        monkeypatch.setenv("RLM_INDEX_DIR", str(cf_project / ".idx_no_syn"))
        builder = IndexBuilder()
        db_path = builder.build(
            str(cf_project),
            build_calls=False,
            build_fts=False,
            build_synonyms=False,
        )

        builder.update(str(cf_project))

        conn = sqlite3.connect(str(db_path))
        count = conn.execute("SELECT COUNT(*) FROM object_synonyms").fetchone()[0]
        conn.close()
        assert count == 0  # no synonyms built

    def test_update_bumps_builder_version(self, cf_project, monkeypatch):
        """update() on v6 index must bump builder_version to 7."""
        monkeypatch.setenv("RLM_INDEX_DIR", str(cf_project / ".idx_v6ver"))
        builder = IndexBuilder()
        db_path = builder.build(
            str(cf_project),
            build_calls=False,
            build_fts=False,
            build_synonyms=False,
        )
        # Simulate v6 index: remove has_synonyms, set version=6
        conn = sqlite3.connect(str(db_path))
        conn.execute("DELETE FROM index_meta WHERE key='has_synonyms'")
        conn.execute("INSERT OR REPLACE INTO index_meta (key, value) VALUES ('builder_version', '6')")
        conn.execute("INSERT OR REPLACE INTO index_meta (key, value) VALUES ('version', '6')")
        conn.commit()
        conn.close()

        builder.update(str(cf_project))

        conn = sqlite3.connect(str(db_path))
        ver = conn.execute("SELECT value FROM index_meta WHERE key='builder_version'").fetchone()[0]
        conn.close()
        assert int(ver) == BUILDER_VERSION


# ---------------------------------------------------------------------------
# v7 → v8 migration: regions & module_headers
# ---------------------------------------------------------------------------


class TestV7toV8Migration:
    def test_update_v7_index_creates_regions_and_headers(self, cf_project, monkeypatch):
        """update() on v7 index must trigger full rebuild with regions/module_headers."""
        monkeypatch.setenv("RLM_INDEX_DIR", str(cf_project / ".idx_v7to8"))
        builder = IndexBuilder()
        db_path = builder.build(
            str(cf_project),
            build_calls=False,
            build_fts=False,
            build_synonyms=True,
        )
        # Simulate v7 index: set version=7, drop regions/module_headers tables
        conn = sqlite3.connect(str(db_path))
        conn.execute("DROP TABLE IF EXISTS regions")
        conn.execute("DROP TABLE IF EXISTS module_headers")
        conn.execute("INSERT OR REPLACE INTO index_meta (key, value) VALUES ('builder_version', '7')")
        conn.execute("INSERT OR REPLACE INTO index_meta (key, value) VALUES ('version', '7')")
        conn.commit()
        conn.close()

        result = builder.update(str(cf_project))

        # Should have done a full rebuild
        assert result["added"] > 0

        conn = sqlite3.connect(str(db_path))
        ver = conn.execute("SELECT value FROM index_meta WHERE key='builder_version'").fetchone()[0]
        assert int(ver) == BUILDER_VERSION
        # regions and module_headers tables must exist (no OperationalError)
        regions_count = conn.execute("SELECT COUNT(*) FROM regions").fetchone()[0]
        headers_count = conn.execute("SELECT COUNT(*) FROM module_headers").fetchone()[0]
        conn.close()
        # cf_project fixture has no #Область and no header comments,
        # so counts are 0 — but tables must exist after migration
        assert isinstance(regions_count, int)
        assert isinstance(headers_count, int)

    def test_update_v7_preserves_build_flags(self, cf_project, monkeypatch):
        """v7→v8 migration must preserve build flags (no FTS → still no FTS)."""
        monkeypatch.setenv("RLM_INDEX_DIR", str(cf_project / ".idx_v7flags"))
        builder = IndexBuilder()
        db_path = builder.build(
            str(cf_project),
            build_calls=False,
            build_fts=False,
            build_synonyms=False,
        )
        # Simulate v7 index
        conn = sqlite3.connect(str(db_path))
        conn.execute("INSERT OR REPLACE INTO index_meta (key, value) VALUES ('builder_version', '7')")
        conn.commit()
        conn.close()

        builder.update(str(cf_project))

        conn = sqlite3.connect(str(db_path))
        # FTS should still be disabled
        has_fts = conn.execute("SELECT value FROM index_meta WHERE key='has_fts'").fetchone()
        assert has_fts is None or has_fts[0] != "1"
        conn.close()


# ---------------------------------------------------------------------------
# Fix: exact match must never be lost in over-fetch (Finding 1)
# ---------------------------------------------------------------------------


class TestExactMatchGuarantee:
    def test_exact_match_not_lost_with_many_results(self, tmp_path, monkeypatch):
        """Object named exactly "Тест" must be rank 0 among 210 "Тест*" LIKE hits.

        Reproduces the original bug: query "Тест" matches Тест0..Тест209
        (210 rows via LIKE '%Тест%') plus the exact object "Тест".
        With the old LIMIT limit*3 in SQL, "Тест" could be outside the
        over-fetch window and never reach Python ranking.
        """
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / ".idx_many"))

        # Create 210 objects "Тест0".."Тест209" — all match LIKE '%Тест%'
        for i in range(210):
            obj_dir = tmp_path / "Documents" / f"Тест{i}" / "Ext"
            obj_dir.mkdir(parents=True)
            (obj_dir / "ObjectModule.bsl").write_text("Процедура Т() Экспорт\nКонецПроцедуры\n", encoding="utf-8-sig")
            xml = (
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses" '
                'xmlns:mdclass="http://v8.1c.ru/8.3/MDClasses" '
                'xmlns:v8="http://v8.1c.ru/8.1/data/core">\n'
                "<Document><Properties>"
                f"<Name>Тест{i}</Name>"
                "<Synonym><v8:item><v8:lang>ru</v8:lang>"
                f"<v8:content>Тестовый документ {i}</v8:content>"
                "</v8:item></Synonym>"
                "</Properties></Document></MetaDataObject>"
            )
            (obj_dir / "Document.xml").write_text(xml, encoding="utf-8")

        # Add one object named exactly "Тест" — the exact match target
        exact_dir = tmp_path / "CommonModules" / "Тест" / "Ext"
        exact_dir.mkdir(parents=True)
        (exact_dir / "Module.bsl").write_text("Процедура Т() Экспорт\nКонецПроцедуры\n", encoding="utf-8-sig")
        exact_xml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses" '
            'xmlns:mdclass="http://v8.1c.ru/8.3/MDClasses" '
            'xmlns:v8="http://v8.1c.ru/8.1/data/core">\n'
            "<CommonModule><Properties>"
            "<Name>Тест</Name>"
            "<Synonym><v8:item><v8:lang>ru</v8:lang>"
            "<v8:content>Тест</v8:content>"
            "</v8:item></Synonym>"
            "</Properties></CommonModule></MetaDataObject>"
        )
        (exact_dir / "Module.xml").write_text(exact_xml, encoding="utf-8")

        builder = IndexBuilder()
        db_path = builder.build(str(tmp_path), build_calls=False, build_fts=False)
        reader = IndexReader(db_path)

        # Query "Тест" matches all 211 objects; exact "Тест" must be first
        results = reader.search_objects("Тест", limit=50)
        reader.close()

        assert len(results) == 50  # limited by limit param
        assert results[0]["object_name"] == "Тест"  # exact match = rank 0


# ---------------------------------------------------------------------------
# Fix: all 6 recipes start with search_objects (Finding 3)
# ---------------------------------------------------------------------------


class TestRecipesSearchObjects:
    def test_all_recipes_start_with_search_objects(self):
        from rlm_tools_bsl.bsl_knowledge import _BUSINESS_RECIPES

        # Some recipes use specialized helpers instead of search_objects.
        # v1.10.0 adds: перечисления (find_enum_values), ввод на основании (find_based_on_documents),
        # структура объекта (get_object_full_structure).
        # v1.11.0+ adds: иерархия вызовов (find_call_hierarchy), расширения (get_overrides).
        # v1.19.0+ adds: достижимость (find_path), путь данных (find_data_path).
        # v1.34.0 adds: история (git_log / git_commit / git_pickaxe).
        _NO_SEARCH_OBJECTS = {
            "тип реквизита",
            "ссылки",
            "перечисления",
            "ввод на основании",
            "структура объекта",
            "иерархия вызовов",
            "расширения",
            "достижимость",
            "путь данных",
            "история",
        }
        for domain, recipe in _BUSINESS_RECIPES.items():
            if domain in _NO_SEARCH_OBJECTS:
                continue
            for level in ("compact", "full"):
                steps = recipe.get(level, [])
                assert any("search_objects" in s for s in steps), f"Recipe '{domain}' {level} missing search_objects"
                # First step must contain search_objects
                assert "search_objects" in steps[0], f"Recipe '{domain}' {level}: search_objects not first step"
