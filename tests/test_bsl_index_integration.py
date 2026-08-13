"""Integration tests: BSL helpers accelerated by SQLite index (Stage 2).

Tests verify that when IndexReader is provided to make_bsl_helpers:
- extract_procedures, find_exports, find_callers_context use the index
- find_event_subscriptions, find_scheduled_jobs, find_functional_options use the index
- search_methods works via FTS5
- Without index: helpers fall back to live parsing (existing behavior)
- Strategy text includes INDEX section when index is loaded
"""

import json
import os
import sqlite3

import pytest

from rlm_tools_bsl.bsl_index import (
    IndexBuilder,
    IndexReader,
)
from rlm_tools_bsl.bsl_helpers import make_bsl_helpers
from rlm_tools_bsl.bsl_knowledge import get_strategy
from rlm_tools_bsl.format_detector import detect_format
from rlm_tools_bsl.helpers import make_helpers


# ---------------------------------------------------------------------------
# BSL fixtures
# ---------------------------------------------------------------------------

COMMON_MODULE_BSL = """\
Процедура ЗаполнитьТабличнуюЧасть(ДокументОбъект, ИмяТабличнойЧасти) Экспорт
    Для Каждого Строка Из ДокументОбъект[ИмяТабличнойЧасти] Цикл
        Строка.Количество = 1;
    КонецЦикла;
КонецПроцедуры

Функция ПолучитьДатуСеанса() Экспорт
    Возврат ТекущаяДатаСеанса();
КонецФункции

Процедура ВычислитьИтоги(ТаблицаЗначений)
    Результат = Новый Массив;
КонецПроцедуры
"""

OBJECT_MODULE_BSL = """\
Процедура ОбработкаЗаполнения(ДанныеЗаполнения, СтандартнаяОбработка) Экспорт
    МойМодуль.ЗаполнитьТабличнуюЧасть(ЭтотОбъект, "Товары");
КонецПроцедуры

Процедура ПередЗаписью(Отказ)
    ПроверитьЗаполнение();
КонецПроцедуры
"""

EVENT_SUBSCRIPTION_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"
                xmlns:v8="http://v8.1c.ru/8.1/data/core"
                xmlns:xr="http://v8.1c.ru/8.3/xcf/readable">
<EventSubscription>
<Properties>
<Name>ПриЗаписиДокумента</Name>
<Synonym><v8:item><v8:content>При записи документа</v8:content></v8:item></Synonym>
<Source>
<v8:Type>cfg:DocumentObject.ТестовыйДокумент</v8:Type>
</Source>
<Handler>CommonModule.МойМодуль.ПриЗаписиДокументаОбработка</Handler>
<Event>OnWrite</Event>
</Properties>
</EventSubscription>
</MetaDataObject>
"""

SCHEDULED_JOB_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses" xmlns:v8="http://v8.1c.ru/8.1/data/core">
<ScheduledJob>
<Properties>
<Name>ОбновлениеКурсовВалют</Name>
<Synonym><v8:item><v8:content>Обновление курсов валют</v8:content></v8:item></Synonym>
<MethodName>CommonModule.КурсыВалют.ОбновитьКурсы</MethodName>
<Use>true</Use>
<Predefined>true</Predefined>
<RestartCountOnFailure>3</RestartCountOnFailure>
<RestartIntervalOnFailure>60</RestartIntervalOnFailure>
</Properties>
</ScheduledJob>
</MetaDataObject>
"""

FUNCTIONAL_OPTION_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"
                xmlns:v8="http://v8.1c.ru/8.1/data/core"
                xmlns:xr="http://v8.1c.ru/8.3/xcf/readable">
<FunctionalOption>
<Properties>
<Name>ИспользоватьВалюту</Name>
<Synonym><v8:item><v8:content>Использовать валюту</v8:content></v8:item></Synonym>
<Location>Constant.ИспользоватьВалюту</Location>
<Content>
<xr:Object>Catalog.Валюты</xr:Object>
<xr:Object>Document.ТестовыйДокумент</xr:Object>
</Content>
</Properties>
</FunctionalOption>
</MetaDataObject>
"""


@pytest.fixture
def tmp_bsl_project(tmp_path):
    """Create a CF-format project with BSL files + metadata XMLs."""
    # CommonModules/МойМодуль/Ext/Module.bsl
    cm_dir = tmp_path / "CommonModules" / "МойМодуль" / "Ext"
    cm_dir.mkdir(parents=True)
    (cm_dir / "Module.bsl").write_text(COMMON_MODULE_BSL, encoding="utf-8-sig")

    # Documents/ТестовыйДокумент/Ext/ObjectModule.bsl
    doc_dir = tmp_path / "Documents" / "ТестовыйДокумент" / "Ext"
    doc_dir.mkdir(parents=True)
    (doc_dir / "ObjectModule.bsl").write_text(OBJECT_MODULE_BSL, encoding="utf-8-sig")

    # EventSubscriptions/ПриЗаписиДокумента/Ext/EventSubscription.xml
    es_dir = tmp_path / "EventSubscriptions" / "ПриЗаписиДокумента" / "Ext"
    es_dir.mkdir(parents=True)
    (es_dir / "EventSubscription.xml").write_text(EVENT_SUBSCRIPTION_XML, encoding="utf-8")

    # ScheduledJobs/ОбновлениеКурсовВалют/Ext/ScheduledJob.xml
    sj_dir = tmp_path / "ScheduledJobs" / "ОбновлениеКурсовВалют" / "Ext"
    sj_dir.mkdir(parents=True)
    (sj_dir / "ScheduledJob.xml").write_text(SCHEDULED_JOB_XML, encoding="utf-8")

    # FunctionalOptions/ИспользоватьВалюту/Ext/FunctionalOption.xml
    fo_dir = tmp_path / "FunctionalOptions" / "ИспользоватьВалюту" / "Ext"
    fo_dir.mkdir(parents=True)
    (fo_dir / "FunctionalOption.xml").write_text(FUNCTIONAL_OPTION_XML, encoding="utf-8")

    # Configuration.xml (minimal, for format detection)
    (tmp_path / "Configuration.xml").write_text("<Configuration/>", encoding="utf-8")

    return tmp_path


@pytest.fixture
def built_index(tmp_bsl_project, monkeypatch):
    """Build a full index (calls + metadata + FTS) and return IndexReader."""
    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_bsl_project / ".index"))
    builder = IndexBuilder()
    db_path = builder.build(
        str(tmp_bsl_project),
        build_calls=True,
        build_metadata=True,
        build_fts=True,
    )
    reader = IndexReader(db_path)
    yield reader
    reader.close()


def _make_helpers(base_path, idx_reader=None):
    """Build bsl helpers dict with optional IndexReader."""
    helpers, resolve_safe = make_helpers(str(base_path))
    format_info = detect_format(str(base_path))
    bsl = make_bsl_helpers(
        base_path=str(base_path),
        resolve_safe=resolve_safe,
        read_file_fn=helpers["read_file"],
        grep_fn=helpers["grep"],
        glob_files_fn=helpers["glob_files"],
        format_info=format_info,
        idx_reader=idx_reader,
    )
    return bsl


# =====================================================================
# Tests WITH index — fast path
# =====================================================================


class TestWithIndex:
    def test_extract_procedures_from_index(self, tmp_bsl_project, built_index):
        bsl = _make_helpers(tmp_bsl_project, built_index)
        procs = bsl["extract_procedures"]("CommonModules/МойМодуль/Ext/Module.bsl")
        assert len(procs) == 3
        names = [p["name"] for p in procs]
        assert "ЗаполнитьТабличнуюЧасть" in names
        assert "ПолучитьДатуСеанса" in names
        assert "ВычислитьИтоги" in names

    def test_extract_procedures_export_flag(self, tmp_bsl_project, built_index):
        bsl = _make_helpers(tmp_bsl_project, built_index)
        procs = bsl["extract_procedures"]("CommonModules/МойМодуль/Ext/Module.bsl")
        by_name = {p["name"]: p for p in procs}
        assert by_name["ЗаполнитьТабличнуюЧасть"]["is_export"] is True
        assert by_name["ВычислитьИтоги"]["is_export"] is False

    def test_find_exports_from_index(self, tmp_bsl_project, built_index):
        bsl = _make_helpers(tmp_bsl_project, built_index)
        exports = bsl["find_exports"]("CommonModules/МойМодуль/Ext/Module.bsl")
        names = [e["name"] for e in exports]
        assert "ЗаполнитьТабличнуюЧасть" in names
        assert "ПолучитьДатуСеанса" in names
        assert "ВычислитьИтоги" not in names

    def test_find_callers_context_from_index(self, tmp_bsl_project, built_index):
        bsl = _make_helpers(tmp_bsl_project, built_index)
        result = bsl["find_callers_context"]("ЗаполнитьТабличнуюЧасть")
        assert "callers" in result
        assert "_meta" in result
        callers = result["callers"]
        assert len(callers) >= 1
        # Should find ObjectModule calling ЗаполнитьТабличнуюЧасть
        caller_files = [c["file"] for c in callers]
        assert any("ObjectModule.bsl" in f for f in caller_files)

    def test_find_event_subscriptions_from_index(self, tmp_bsl_project, built_index):
        bsl = _make_helpers(tmp_bsl_project, built_index)
        # All subscriptions
        all_subs = bsl["find_event_subscriptions"]()
        assert len(all_subs) >= 1
        assert all_subs[0]["name"] == "ПриЗаписиДокумента"
        # Filtered by object
        filtered = bsl["find_event_subscriptions"]("ТестовыйДокумент")
        assert len(filtered) >= 1

    def test_find_scheduled_jobs_from_index(self, tmp_bsl_project, built_index):
        bsl = _make_helpers(tmp_bsl_project, built_index)
        all_jobs = bsl["find_scheduled_jobs"]()
        assert len(all_jobs) >= 1
        assert all_jobs[0]["name"] == "ОбновлениеКурсовВалют"
        assert all_jobs[0]["use"] is True
        # Filter by name
        filtered = bsl["find_scheduled_jobs"]("Курс")
        assert len(filtered) >= 1

    def test_find_functional_options_from_index(self, tmp_bsl_project, built_index):
        bsl = _make_helpers(tmp_bsl_project, built_index)
        result = bsl["find_functional_options"]("ТестовыйДокумент")
        assert "xml_options" in result
        xml_opts = result["xml_options"]
        assert len(xml_opts) >= 1
        assert xml_opts[0]["name"] == "ИспользоватьВалюту"

    def test_search_methods_fts(self, tmp_bsl_project, built_index):
        bsl = _make_helpers(tmp_bsl_project, built_index)
        results = bsl["search_methods"]("Заполнить")
        assert len(results) >= 1
        names = [r["name"] for r in results]
        assert "ЗаполнитьТабличнуюЧасть" in names

    def test_search_methods_empty_query(self, tmp_bsl_project, built_index):
        bsl = _make_helpers(tmp_bsl_project, built_index)
        results = bsl["search_methods"]("")
        assert results == []

    def test_search_methods_no_match(self, tmp_bsl_project, built_index):
        bsl = _make_helpers(tmp_bsl_project, built_index)
        results = bsl["search_methods"]("НесуществующееИмяМетода12345")
        assert results == []


# =====================================================================
# Tests WITHOUT index — fallback
# =====================================================================


class TestWithoutIndex:
    def test_extract_procedures_fallback(self, tmp_bsl_project):
        bsl = _make_helpers(tmp_bsl_project, idx_reader=None)
        procs = bsl["extract_procedures"]("CommonModules/МойМодуль/Ext/Module.bsl")
        assert len(procs) == 3
        names = [p["name"] for p in procs]
        assert "ЗаполнитьТабличнуюЧасть" in names

    def test_find_exports_fallback(self, tmp_bsl_project):
        bsl = _make_helpers(tmp_bsl_project, idx_reader=None)
        exports = bsl["find_exports"]("CommonModules/МойМодуль/Ext/Module.bsl")
        assert len(exports) == 2

    def test_find_callers_context_fallback(self, tmp_bsl_project):
        bsl = _make_helpers(tmp_bsl_project, idx_reader=None)
        result = bsl["find_callers_context"]("ЗаполнитьТабличнуюЧасть")
        assert "callers" in result

    def test_find_event_subscriptions_fallback(self, tmp_bsl_project):
        bsl = _make_helpers(tmp_bsl_project, idx_reader=None)
        subs = bsl["find_event_subscriptions"]()
        assert len(subs) >= 1

    def test_find_scheduled_jobs_fallback(self, tmp_bsl_project):
        bsl = _make_helpers(tmp_bsl_project, idx_reader=None)
        jobs = bsl["find_scheduled_jobs"]()
        assert len(jobs) >= 1

    def test_find_functional_options_fallback(self, tmp_bsl_project):
        bsl = _make_helpers(tmp_bsl_project, idx_reader=None)
        result = bsl["find_functional_options"]("ТестовыйДокумент")
        assert "xml_options" in result

    def test_search_methods_without_index(self, tmp_bsl_project):
        bsl = _make_helpers(tmp_bsl_project, idx_reader=None)
        results = bsl["search_methods"]("Заполнить")
        assert results == []  # no index = empty


# =====================================================================
# IndexReader new methods (unit tests)
# =====================================================================


class TestIndexReaderNewMethods:
    def test_get_event_subscriptions_all(self, built_index):
        result = built_index.get_event_subscriptions()
        assert result is not None
        assert len(result) >= 1
        assert result[0]["name"] == "ПриЗаписиДокумента"

    def test_get_event_subscriptions_filtered(self, built_index):
        result = built_index.get_event_subscriptions("ТестовыйДокумент")
        assert result is not None
        assert len(result) >= 1
        # Filtered result includes source_types
        assert "source_types" in result[0]

    def test_get_event_subscriptions_no_match(self, built_index):
        # Our subscription has specific source_types (DocumentObject.ТестовыйДокумент),
        # so filtering by a non-matching object should return empty.
        result = built_index.get_event_subscriptions("НесуществующийОбъект")
        assert result is not None
        assert len(result) == 0

    def test_get_scheduled_jobs_all(self, built_index):
        result = built_index.get_scheduled_jobs()
        assert result is not None
        assert len(result) >= 1
        job = result[0]
        assert job["name"] == "ОбновлениеКурсовВалют"
        assert job["use"] is True
        assert job["predefined"] is True

    def test_get_scheduled_jobs_filtered(self, built_index):
        result = built_index.get_scheduled_jobs("Курс")
        assert result is not None
        assert len(result) >= 1

    def test_get_scheduled_jobs_no_match(self, built_index):
        result = built_index.get_scheduled_jobs("НетТакогоЗадания")
        assert result is not None
        assert len(result) == 0

    def test_get_scheduled_jobs_cyrillic_case_insensitive(self, built_index):
        # 'курс' (нижний регистр) должен найти 'ОбновлениеКурсовВалют' (py_lower);
        # COLLATE NOCASE фолдит только ASCII → 'курс' не матчил 'Курс'.
        result = built_index.get_scheduled_jobs("курс")
        assert result is not None and len(result) >= 1

    def test_get_functional_options_all(self, built_index):
        result = built_index.get_functional_options()
        assert result is not None
        assert len(result) >= 1
        assert result[0]["name"] == "ИспользоватьВалюту"

    def test_get_functional_options_filtered(self, built_index):
        result = built_index.get_functional_options("ТестовыйДокумент")
        assert result is not None
        assert len(result) >= 1

    def test_get_functional_options_no_match(self, built_index):
        result = built_index.get_functional_options("НесуществующийОбъект")
        assert result is not None
        assert len(result) == 0


# =====================================================================
# Strategy text with index
# =====================================================================


class TestStrategyWithIndex:
    def test_strategy_includes_index_section(self):
        idx_stats = {
            "methods": 500,
            "calls": 10000,
            "has_fts": True,
            "config_name": "ТестоваяКонфигурация",
            "config_version": "3.0.1",
        }
        strategy = get_strategy("high", None, idx_stats=idx_stats)
        assert "== INDEX ==" in strategy
        assert "500 methods" in strategy
        assert "10000 call edges" in strategy
        assert "ТестоваяКонфигурация" in strategy
        assert "search_methods" in strategy

    def test_strategy_includes_warnings(self):
        idx_stats = {"methods": 100, "calls": 200, "has_fts": False}
        idx_warnings = ["Index is 10 days old — verify critical findings"]
        strategy = get_strategy(
            "medium",
            None,
            idx_stats=idx_stats,
            idx_warnings=idx_warnings,
        )
        assert "WARNING:" in strategy
        assert "10 days old" in strategy

    def test_strategy_no_fts_no_fts_line_in_index_section(self):
        idx_stats = {"methods": 100, "calls": 200, "has_fts": False}
        strategy = get_strategy("medium", None, idx_stats=idx_stats)
        assert "== INDEX ==" in strategy
        # FTS-specific line should NOT appear in INDEX section
        assert "full-text search by method name" not in strategy

    def test_strategy_without_index(self):
        strategy = get_strategy("medium", None)
        assert "== INDEX ==" in strategy
        assert "No pre-built index" in strategy
        assert "NEVER call rlm_index" in strategy

    def test_strategy_quick_check_note(self):
        """Strategy includes quick check note when index is loaded."""
        idx_stats = {"methods": 100, "calls": 200, "has_fts": False}
        strategy = get_strategy("medium", None, idx_stats=idx_stats)
        assert "quick check" in strategy


def test_index_state_from_sqlite(tmp_bsl_project, built_index):
    """With idx_reader, _ensure_index() loads from SQLite, not glob."""
    bsl = _make_helpers(tmp_bsl_project, idx_reader=built_index)
    # find_module triggers _ensure_index internally
    modules = bsl["find_module"]("ТестовыйДокумент")
    assert len(modules) >= 1
    assert modules[0]["object_name"] == "ТестовыйДокумент"


def test_detected_prefixes_in_index(tmp_bsl_project, monkeypatch):
    """Detected prefixes are stored in index_meta and retrievable."""
    import tempfile

    # Create a project with objects that have a common prefix (3+ for threshold)
    with tempfile.TemporaryDirectory() as tmpdir:
        for name in ["тст_Справочник1", "тст_Справочник2", "тст_Справочник3"]:
            cat_dir = os.path.join(tmpdir, "Catalogs", name, "Ext")
            os.makedirs(cat_dir)
            with open(os.path.join(cat_dir, "ObjectModule.bsl"), "w", encoding="utf-8") as f:
                f.write("Процедура Тест()\nКонецПроцедуры\n")
        with open(os.path.join(tmpdir, "Configuration.xml"), "w") as f:
            f.write("<Configuration/>")

        monkeypatch.setenv("RLM_INDEX_DIR", os.path.join(tmpdir, ".index"))

        builder = IndexBuilder()
        db_path = builder.build(tmpdir)
        reader = IndexReader(db_path)

        prefixes = reader.get_detected_prefixes()
        assert "тст" in prefixes
        reader.close()


RIGHTS_EDT_CONTENT = """\
<?xml version="1.0" encoding="UTF-8"?>
<Rights xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns="http://v8.1c.ru/8.3/roles" xsi:type="Rights">
  <object>
    <name>Catalog.ТестСправочник</name>
    <right>
      <name>Read</name>
      <value>true</value>
    </right>
    <right>
      <name>Update</name>
      <value>true</value>
    </right>
    <right>
      <name>Delete</name>
      <value>false</value>
    </right>
  </object>
</Rights>
"""


def test_role_rights_build_and_query(tmp_path, monkeypatch):
    """role_rights table is built from .rights files and queryable."""
    # Create BSL + rights structure
    cm_dir = tmp_path / "CommonModules" / "Модуль" / "Ext"
    cm_dir.mkdir(parents=True)
    (cm_dir / "Module.bsl").write_text("Процедура Тест()\nКонецПроцедуры\n", encoding="utf-8-sig")

    role_dir = tmp_path / "Roles" / "ТестоваяРоль"
    role_dir.mkdir(parents=True)
    (role_dir / "ТестоваяРоль.rights").write_text(RIGHTS_EDT_CONTENT, encoding="utf-8")

    (tmp_path / "Configuration.xml").write_text("<Configuration/>", encoding="utf-8")

    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / ".index"))

    builder = IndexBuilder()
    db_path = builder.build(str(tmp_path))
    reader = IndexReader(db_path)

    # Check statistics
    stats = reader.get_statistics()
    assert stats["role_rights"] > 0

    # Query roles for ТестСправочник
    roles = reader.get_roles("ТестСправочник")
    assert roles is not None
    assert len(roles) == 1
    assert roles[0]["role_name"] == "ТестоваяРоль"
    assert "Read" in roles[0]["rights"]
    assert "Update" in roles[0]["rights"]
    # Delete was false, should NOT be in rights
    assert "Delete" not in roles[0]["rights"]

    reader.close()


def test_find_roles_fast_path(tmp_path, monkeypatch):
    """find_roles() uses index fast path when available."""
    cm_dir = tmp_path / "CommonModules" / "Модуль" / "Ext"
    cm_dir.mkdir(parents=True)
    (cm_dir / "Module.bsl").write_text("Процедура Тест()\nКонецПроцедуры\n", encoding="utf-8-sig")

    role_dir = tmp_path / "Roles" / "АдминРоль"
    role_dir.mkdir(parents=True)
    (role_dir / "АдминРоль.rights").write_text(RIGHTS_EDT_CONTENT, encoding="utf-8")

    (tmp_path / "Configuration.xml").write_text("<Configuration/>", encoding="utf-8")

    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / ".index"))

    builder = IndexBuilder()
    db_path = builder.build(str(tmp_path))
    reader = IndexReader(db_path)

    bsl = _make_helpers(tmp_path, idx_reader=reader)
    result = bsl["find_roles"]("ТестСправочник")

    assert result["object"] == "ТестСправочник"
    assert len(result["roles"]) >= 1
    assert result["roles"][0]["role_name"] == "АдминРоль"

    reader.close()


DOCUMENT_OBJECT_MODULE = """\
Процедура ОбработкаПроведения(Отказ, РежимПроведения)
    Движения.ТоварыНаСкладах.Записать = Истина;
    Движения.ВзаиморасчетыСКлиентами.Записать = Истина;
КонецПроцедуры
"""

DOCUMENT_MANAGER_MODULE = """\
Процедура ЗарегистрироватьУчетныеМеханизмы(Менеджер)
    МеханизмыДокумента.Добавить("ТоварыНаСкладах");
    МеханизмыДокумента.Добавить("ВзаиморасчетыСКлиентами");
КонецПроцедуры

Функция ТекстЗапросаТаблицаТоварыНаСкладах()
    Возврат "";
КонецФункции
"""


def test_register_movements_build(tmp_path, monkeypatch):
    """register_movements extracted in-band from Document modules during build."""
    doc_dir = tmp_path / "Documents" / "Реализация" / "Ext"
    doc_dir.mkdir(parents=True)
    (doc_dir / "ObjectModule.bsl").write_text(DOCUMENT_OBJECT_MODULE, encoding="utf-8-sig")
    (doc_dir / "ManagerModule.bsl").write_text(DOCUMENT_MANAGER_MODULE, encoding="utf-8-sig")

    (tmp_path / "Configuration.xml").write_text("<Configuration/>", encoding="utf-8")

    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / ".index"))

    builder = IndexBuilder()
    db_path = builder.build(str(tmp_path))
    reader = IndexReader(db_path)

    stats = reader.get_statistics()
    assert stats["register_movements"] > 0

    movements = reader.get_register_movements("Реализация")
    assert movements is not None
    reg_names = [m["register_name"] for m in movements]
    assert "ТоварыНаСкладах" in reg_names
    assert "ВзаиморасчетыСКлиентами" in reg_names

    # Check register writers (reverse lookup)
    writers = reader.get_register_writers("ТоварыНаСкладах")
    assert writers is not None
    assert any(w["document_name"] == "Реализация" for w in writers)

    reader.close()


def test_find_register_movements_fast_path(tmp_path, monkeypatch):
    """find_register_movements uses index fast path when available."""
    doc_dir = tmp_path / "Documents" / "Реализация" / "Ext"
    doc_dir.mkdir(parents=True)
    (doc_dir / "ObjectModule.bsl").write_text(DOCUMENT_OBJECT_MODULE, encoding="utf-8-sig")

    (tmp_path / "Configuration.xml").write_text("<Configuration/>", encoding="utf-8")

    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / ".index"))

    builder = IndexBuilder()
    db_path = builder.build(str(tmp_path))
    reader = IndexReader(db_path)

    bsl = _make_helpers(tmp_path, idx_reader=reader)
    result = bsl["find_register_movements"]("Реализация")

    assert result["document"] == "Реализация"
    assert len(result["code_registers"]) >= 2
    reg_names = [r["name"] for r in result["code_registers"]]
    assert "ТоварыНаСкладах" in reg_names

    reader.close()


# ---------------------------------------------------------------------------
# Index vs FS parity fixes (v1.3.2 bugfixes)
# ---------------------------------------------------------------------------


def test_role_rights_full_object_name_stored(tmp_path, monkeypatch):
    """Builder stores full object name (Catalog.ТестСправочник), reader finds by substring."""
    cm_dir = tmp_path / "CommonModules" / "Модуль" / "Ext"
    cm_dir.mkdir(parents=True)
    (cm_dir / "Module.bsl").write_text("Процедура Тест()\nКонецПроцедуры\n", encoding="utf-8-sig")

    role_dir = tmp_path / "Roles" / "ТестРоль"
    role_dir.mkdir(parents=True)
    (role_dir / "ТестРоль.rights").write_text(RIGHTS_EDT_CONTENT, encoding="utf-8")

    (tmp_path / "Configuration.xml").write_text("<Configuration/>", encoding="utf-8")
    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / ".index"))

    builder = IndexBuilder()
    db_path = builder.build(str(tmp_path))
    reader = IndexReader(db_path)

    # Full name stored in DB
    rows = reader._conn.execute("SELECT object_name FROM role_rights WHERE role_name = 'ТестРоль'").fetchall()
    obj_names = [r["object_name"] for r in rows]
    assert any("Catalog.ТестСправочник" in n for n in obj_names), f"Expected full name, got {obj_names}"

    # Reader finds by short name via LIKE
    roles = reader.get_roles("ТестСправочник")
    assert roles is not None
    assert len(roles) >= 1

    reader.close()


def test_register_movements_code_filter(tmp_path, monkeypatch):
    """code_registers in fast path contains only source='code', not erp_mechanism."""
    doc_dir = tmp_path / "Documents" / "ТестДок" / "Ext"
    doc_dir.mkdir(parents=True)
    (doc_dir / "ObjectModule.bsl").write_text(DOCUMENT_OBJECT_MODULE, encoding="utf-8-sig")
    (doc_dir / "ManagerModule.bsl").write_text(DOCUMENT_MANAGER_MODULE, encoding="utf-8-sig")

    (tmp_path / "Configuration.xml").write_text("<Configuration/>", encoding="utf-8")
    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / ".index"))

    builder = IndexBuilder()
    db_path = builder.build(str(tmp_path))
    reader = IndexReader(db_path)

    bsl = _make_helpers(tmp_path, idx_reader=reader)
    result = bsl["find_register_movements"]("ТестДок")

    # code_registers must only contain source='code'
    for r in result["code_registers"]:
        assert r["source"] == "code", f"code_registers has non-code source: {r}"

    # erp_mechanisms are separate
    assert len(result["erp_mechanisms"]) >= 2

    reader.close()


_MANAGER_MODULE_WITH_CALLS = """\
Процедура ЗарегистрироватьУчетныеМеханизмы(Менеджер)
    МеханизмыДокумента.Добавить("РегистрА");
КонецПроцедуры

Функция ТекстЗапросаТаблицаРегистрА()
    Возврат "";
КонецФункции

Процедура ОбработатьДанные()
    Текст = ТекстЗапросаТаблицаФейк();
КонецПроцедуры
"""


def test_manager_table_only_definitions(tmp_path, monkeypatch):
    """manager_tables regex matches only Функция/Процедура definitions, not calls."""
    doc_dir = tmp_path / "Documents" / "МТДок" / "Ext"
    doc_dir.mkdir(parents=True)
    (doc_dir / "ObjectModule.bsl").write_text("Процедура Проведение()\nКонецПроцедуры\n", encoding="utf-8-sig")
    (doc_dir / "ManagerModule.bsl").write_text(_MANAGER_MODULE_WITH_CALLS, encoding="utf-8-sig")

    (tmp_path / "Configuration.xml").write_text("<Configuration/>", encoding="utf-8")
    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / ".index"))

    builder = IndexBuilder()
    db_path = builder.build(str(tmp_path))
    reader = IndexReader(db_path)

    movements = reader.get_register_movements("МТДок")
    mgr_tables = [m for m in movements if m["source"] == "manager_table"]
    mgr_names = [m["register_name"] for m in mgr_tables]

    # Only definition (РегистрА) should be found, not call (Фейк)
    assert "РегистрА" in mgr_names
    assert "Фейк" not in mgr_names, f"Call-site match leaked into manager_tables: {mgr_names}"

    reader.close()


_MANAGER_MODULE_ADAPTED = """\
Процедура ЗарегистрироватьУчетныеМеханизмы(Менеджер)
    МеханизмыДокумента.Добавить("РегистрБ");
КонецПроцедуры

Функция АдаптированныйТекстЗапросаДвиженийПоРегистру(ИмяДокумента, ИмяТаблицы)
    Если ИмяТаблицы = "Продажи" Тогда
        ИмяРегистра = "НакоплениеПродажи";
    ИначеЕсли ИмяТаблицы = "Расчеты" Тогда
        ИмяРегистра = "ВзаиморасчетыСКонтрагентами";
    КонецЕсли;
    Возврат "";
КонецФункции
"""


def test_adapted_registers_build(tmp_path, monkeypatch):
    """Builder extracts adapted registers from АдаптированныйТекстЗапросаДвиженийПоРегистру."""
    doc_dir = tmp_path / "Documents" / "АдДок" / "Ext"
    doc_dir.mkdir(parents=True)
    (doc_dir / "ObjectModule.bsl").write_text("Процедура Проведение()\nКонецПроцедуры\n", encoding="utf-8-sig")
    (doc_dir / "ManagerModule.bsl").write_text(_MANAGER_MODULE_ADAPTED, encoding="utf-8-sig")

    (tmp_path / "Configuration.xml").write_text("<Configuration/>", encoding="utf-8")
    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / ".index"))

    builder = IndexBuilder()
    db_path = builder.build(str(tmp_path))
    reader = IndexReader(db_path)

    movements = reader.get_register_movements("АдДок")
    adapted = [m for m in movements if m["source"] == "adapted"]
    adapted_names = [m["register_name"] for m in adapted]

    assert "НакоплениеПродажи" in adapted_names
    assert "ВзаиморасчетыСКонтрагентами" in adapted_names
    assert len(adapted) == 2

    reader.close()


def test_register_movements_fast_path_parity(tmp_path, monkeypatch):
    """Fast-path (index) result has same structure as FS path for find_register_movements."""
    doc_dir = tmp_path / "Documents" / "ПаритетДок" / "Ext"
    doc_dir.mkdir(parents=True)
    (doc_dir / "ObjectModule.bsl").write_text(DOCUMENT_OBJECT_MODULE, encoding="utf-8-sig")
    (doc_dir / "ManagerModule.bsl").write_text(_MANAGER_MODULE_ADAPTED, encoding="utf-8-sig")

    (tmp_path / "Configuration.xml").write_text("<Configuration/>", encoding="utf-8")
    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / ".index"))

    builder = IndexBuilder()
    db_path = builder.build(str(tmp_path))
    reader = IndexReader(db_path)

    bsl = _make_helpers(tmp_path, idx_reader=reader)
    result = bsl["find_register_movements"]("ПаритетДок")

    # code_registers: only from ObjectModule (source=code)
    code_names = [r["name"] for r in result["code_registers"]]
    assert "ТоварыНаСкладах" in code_names
    assert "ВзаиморасчетыСКлиентами" in code_names
    for r in result["code_registers"]:
        assert r["source"] == "code"

    # erp_mechanisms from ManagerModule
    assert "РегистрБ" in result["erp_mechanisms"]

    # adapted_registers from ManagerModule
    assert "НакоплениеПродажи" in result["adapted_registers"]
    assert "ВзаиморасчетыСКонтрагентами" in result["adapted_registers"]

    reader.close()


def test_update_refreshes_role_rights(tmp_path, monkeypatch):
    """index update refreshes role_rights table when .rights files change."""
    cm_dir = tmp_path / "CommonModules" / "Модуль" / "Ext"
    cm_dir.mkdir(parents=True)
    (cm_dir / "Module.bsl").write_text("Процедура Тест()\nКонецПроцедуры\n", encoding="utf-8-sig")

    role_dir = tmp_path / "Roles" / "РольА"
    role_dir.mkdir(parents=True)
    (role_dir / "РольА.rights").write_text(RIGHTS_EDT_CONTENT, encoding="utf-8")

    (tmp_path / "Configuration.xml").write_text("<Configuration/>", encoding="utf-8")
    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / ".index"))

    builder = IndexBuilder()
    db_path = builder.build(str(tmp_path))

    # Verify initial state
    reader = IndexReader(db_path)
    roles = reader.get_roles("ТестСправочник")
    assert roles is not None
    assert len(roles) >= 1
    assert roles[0]["role_name"] == "РольА"
    reader.close()

    # Add a second role
    role_dir_b = tmp_path / "Roles" / "РольБ"
    role_dir_b.mkdir(parents=True)
    rights_b = RIGHTS_EDT_CONTENT.replace("Catalog.ТестСправочник", "Document.ТестДокумент")
    (role_dir_b / "РольБ.rights").write_text(rights_b, encoding="utf-8")

    # Run update
    builder.update(str(tmp_path))

    # Verify new role appeared
    reader = IndexReader(db_path)
    roles_doc = reader.get_roles("ТестДокумент")
    assert roles_doc is not None
    role_names = [r["role_name"] for r in roles_doc]
    assert "РольБ" in role_names

    # Original role still there
    roles_cat = reader.get_roles("ТестСправочник")
    assert roles_cat is not None
    assert any(r["role_name"] == "РольА" for r in roles_cat)

    reader.close()


# ---------------------------------------------------------------------------
# Stabilization: migration of integration tables in update()
# ---------------------------------------------------------------------------

HTTP_SERVICE_CF_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">
<HTTPService>
<Properties><Name>TestAPI</Name><RootURL>api/v1</RootURL></Properties>
<ChildObjects>
<URLTemplate>
<Properties><Name>users</Name><Template>/users</Template></Properties>
<ChildObjects>
<Method><Properties><Name>GET</Name><HTTPMethod>GET</HTTPMethod>
<Handler>МодульСервиса.ОбработатьGET</Handler></Properties></Method>
</ChildObjects>
</URLTemplate>
</ChildObjects>
</HTTPService>
</MetaDataObject>
"""


def test_update_creates_integration_tables_for_old_index(tmp_path, monkeypatch):
    """update() creates http_services/web_services/xdto_packages for pre-v6 indexes."""
    cm_dir = tmp_path / "CommonModules" / "Модуль" / "Ext"
    cm_dir.mkdir(parents=True)
    (cm_dir / "Module.bsl").write_text("Процедура Тест()\nКонецПроцедуры\n", encoding="utf-8-sig")
    (tmp_path / "Configuration.xml").write_text("<Configuration/>", encoding="utf-8")
    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / ".index"))

    builder = IndexBuilder()
    db_path = builder.build(str(tmp_path), build_metadata=True)

    # Simulate old index: drop integration tables
    conn = sqlite3.connect(str(db_path))
    conn.execute("DROP TABLE IF EXISTS http_services")
    conn.execute("DROP TABLE IF EXISTS web_services")
    conn.execute("DROP TABLE IF EXISTS xdto_packages")
    conn.commit()
    conn.close()

    # Add HTTP service fixture
    hs_dir = tmp_path / "HTTPServices" / "TestAPI"
    hs_dir.mkdir(parents=True)
    (hs_dir / "TestAPI.xml").write_text(HTTP_SERVICE_CF_XML, encoding="utf-8")

    # Run update — should NOT crash, should create tables
    builder.update(str(tmp_path))

    # Verify tables exist
    conn = sqlite3.connect(str(db_path))
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert "http_services" in tables
    assert "web_services" in tables
    assert "xdto_packages" in tables


def test_update_refreshes_detected_prefixes(tmp_path, monkeypatch):
    """index update recalculates detected_prefixes when objects change."""
    # Build with 3 objects sharing prefix "тст_"
    for name in ["тст_Справочник1", "тст_Справочник2", "тст_Справочник3"]:
        cat_dir = tmp_path / "Catalogs" / name / "Ext"
        cat_dir.mkdir(parents=True)
        (cat_dir / "ObjectModule.bsl").write_text("Процедура Тест()\nКонецПроцедуры\n", encoding="utf-8-sig")
    (tmp_path / "Configuration.xml").write_text("<Configuration/>", encoding="utf-8")
    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / ".index"))

    builder = IndexBuilder()
    db_path = builder.build(str(tmp_path))

    # Verify initial prefix detected
    conn = sqlite3.connect(str(db_path))
    row = conn.execute("SELECT value FROM index_meta WHERE key = 'detected_prefixes'").fetchone()
    conn.close()
    initial_prefixes = json.loads(row[0]) if row else []
    assert any("тст" in p for p in initial_prefixes)

    # Add 3 objects with new prefix "абв_"
    for name in ["абв_Документ1", "абв_Документ2", "абв_Документ3"]:
        doc_dir = tmp_path / "Documents" / name / "Ext"
        doc_dir.mkdir(parents=True)
        (doc_dir / "ObjectModule.bsl").write_text("Процедура Тест2()\nКонецПроцедуры\n", encoding="utf-8-sig")

    builder.update(str(tmp_path))

    conn = sqlite3.connect(str(db_path))
    row = conn.execute("SELECT value FROM index_meta WHERE key = 'detected_prefixes'").fetchone()
    conn.close()
    updated_prefixes = json.loads(row[0])
    assert any("тст" in p for p in updated_prefixes)
    assert any("абв" in p for p in updated_prefixes)


# ── v1.33.0: миграция 14 -> 15 и поведение устаревшего/недостроенного индекса ──


def test_v14_index_is_force_rebuilt_not_scanned(tmp_path, monkeypatch):
    """Порог обязан ехать вместе с BUILDER_VERSION: иначе нетронутые модули
    сохранят призраков из v14 (дельта полного скана считается по mtime/size)."""
    from rlm_tools_bsl.bsl_index import BUILDER_VERSION

    obj = tmp_path / "Documents" / "Д" / "Ext"
    obj.mkdir(parents=True)
    src = obj / "ObjectModule.bsl"
    src.write_text(
        "// Процедура Призрак() Экспорт\n// КонецПроцедуры\nПроцедура Настоящая() Экспорт\nКонецПроцедуры\n",
        encoding="utf-8",
    )
    (tmp_path / "Configuration.xml").write_text(
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"><Configuration>'
        "<Properties><Name>Т</Name></Properties></Configuration></MetaDataObject>",
        encoding="utf-8",
    )
    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / ".idx"))
    b = IndexBuilder()
    db_path = b.build(str(tmp_path), build_calls=False, build_metadata=True)

    # искусственно состариваем индекс до v14 и подсаживаем призрака
    con = sqlite3.connect(db_path)
    con.execute("UPDATE index_meta SET value='14' WHERE key='builder_version'")
    con.execute(
        "INSERT INTO methods (module_id, name, type, line, end_line, is_export, params) "
        "SELECT id, 'Призрак', 'Процедура', 1, 2, 1, '' FROM modules LIMIT 1"
    )
    con.commit()
    con.close()

    res = b.update(str(tmp_path))
    assert "rebuild_reason" in res, "v14 -> v15 обязан идти через принудительную пересборку"

    con = sqlite3.connect(db_path)
    names = {r[0] for r in con.execute("SELECT name FROM methods")}
    ver = con.execute("SELECT value FROM index_meta WHERE key='builder_version'").fetchone()[0]
    con.close()
    assert "Призрак" not in names, "призрак пережил миграцию — порог пересборки не поднят"
    assert int(ver) == BUILDER_VERSION


def test_stale_index_still_serves_with_warning(tmp_path, monkeypatch):
    """Ошибку PR #21 не повторять: устаревший индекс обязан ОТВЕЧАТЬ, а не закрываться."""
    from rlm_tools_bsl.bsl_index import BUILDER_VERSION
    from rlm_tools_bsl.server import _rlm_end, _rlm_start

    obj = tmp_path / "Documents" / "Д" / "Ext"
    obj.mkdir(parents=True)
    (obj / "ObjectModule.bsl").write_text("Процедура Настоящая() Экспорт\nКонецПроцедуры\n", encoding="utf-8")
    (tmp_path / "Configuration.xml").write_text(
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"><Configuration>'
        "<Properties><Name>Т</Name></Properties></Configuration></MetaDataObject>",
        encoding="utf-8",
    )
    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / ".idx"))
    db_path = IndexBuilder().build(str(tmp_path), build_calls=False, build_metadata=True)

    # состарить индекс до v14, НЕ ломая его содержимое
    con = sqlite3.connect(db_path)
    con.execute("UPDATE index_meta SET value='14' WHERE key='builder_version'")
    con.commit()
    con.close()

    raw = _rlm_start(path=str(tmp_path), query="")
    try:
        data = json.loads(raw)
        assert data["index"]["loaded"] is True, "ридер закрылся на несовпадении версии (PR #21)"
        assert data["index"]["builder_version"] < BUILDER_VERSION
        assert data["index"]["methods"] >= 1, "устаревший индекс перестал отдавать данные"
        assert any("ересобер" in w or "ересборк" in w for w in data["index"]["warnings"]), data["index"]["warnings"]
    finally:
        # сессия держит ОТКРЫТЫЙ IndexReader — без закрытия течёт SQLite handle,
        # а на Windows временный каталог не удаляется (WinError 32).
        _rlm_end(json.loads(raw)["session_id"])


def test_interrupted_v15_rebuild_is_detected_and_retryable(tmp_bsl_project, tmp_path, monkeypatch):
    """Пересборка in-place: падение в середине оставляет ДЕТЕКТИРУЕМОЕ состояние,
    а повторный запуск доводит её до конца."""
    from rlm_tools_bsl.bsl_index import BUILDER_VERSION, index_incomplete

    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / ".idx"))
    # Дерево ОБЯЗАНО содержать хотя бы один .bsl: при пустом наборе build() уходит в
    # ранний `return db_path` ДО `_bulk_insert`, инъекция отказа не срабатывает
    # и `pytest.raises` падает, ничего не проверив.
    root = tmp_bsl_project
    db_path = IndexBuilder().build(str(root), build_calls=False, build_metadata=True)

    # Воспроизводим реальный release-путь: update() видит предыдущую версию и запускает
    # обязательную полную пересборку v15, а не прямой build() текущей версии.
    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "UPDATE index_meta SET value=? WHERE key='builder_version'",
            (str(BUILDER_VERSION - 1),),
        )
        con.commit()
    finally:
        con.close()

    real_bulk = IndexBuilder._bulk_insert

    def _explode(self, conn, results, build_calls):
        raise RuntimeError("инъекция отказа посреди пересборки")

    # context(), а не setattr+undo: undo откатил бы и RLM_INDEX_DIR, и повтор
    # ушёл бы в ДРУГУЮ базу — тест бы прошёл, ничего не проверив.
    with monkeypatch.context() as mp:
        mp.setattr(IndexBuilder, "_bulk_insert", _explode)
        with pytest.raises(RuntimeError):
            IndexBuilder().update(str(root))
        assert index_incomplete(db_path), "незавершённая сборка обязана детектироваться"

    assert IndexBuilder._bulk_insert is real_bulk
    delta = IndexBuilder().update(str(root))
    assert delta.get("rebuild_reason") == "incomplete prior build", delta
    assert not index_incomplete(db_path), "повторная сборка обязана снять маркер"


def test_incomplete_index_is_reported_by_public_start(tmp_bsl_project, tmp_path, monkeypatch):
    """Заявленное предупреждение обязано быть проверено ЧЕРЕЗ ПУБЛИЧНЫЙ СТАРТ,
    а не выведено из наличия маркера в базе."""
    from rlm_tools_bsl.bsl_index import index_incomplete
    from rlm_tools_bsl.server import _rlm_end, _rlm_start

    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / ".idx"))
    root = tmp_bsl_project  # пустое дерево не годится: см. ранний return в build()
    db_path = IndexBuilder().build(str(root), build_calls=False, build_metadata=True)

    def _explode(self, conn, results, build_calls):
        raise RuntimeError("инъекция отказа посреди пересборки")

    with monkeypatch.context() as mp:
        mp.setattr(IndexBuilder, "_bulk_insert", _explode)
        with pytest.raises(RuntimeError):
            IndexBuilder().build(str(root), build_calls=False, build_metadata=True)
    assert index_incomplete(db_path)

    # _rlm_start РЕГИСТРИРУЕТ сессию и может открыть ридер — закрывать обязательно,
    # иначе на Windows временный каталог не удалится (WinError 32) и потечёт handle.
    raw = _rlm_start(path=str(root), query="")
    try:
        data = json.loads(raw)
        assert data["index"]["index_status"] == "incomplete", data["index"]
        # Предупреждения ОБ ИНДЕКСЕ живут в index.warnings; верхнеуровневый warnings
        # собирается только из _session_warnings и индексных сообщений не содержит.
        assert any("ересбор" in w or "езавершен" in w for w in data["index"]["warnings"]), data["index"]
    finally:
        _rlm_end(json.loads(raw)["session_id"])
