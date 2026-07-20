import json
import os
import re
import sqlite3
import tempfile

import pytest

from rlm_tools_bsl.helpers import make_helpers
from rlm_tools_bsl.format_detector import detect_format
from rlm_tools_bsl.bsl_helpers import (
    make_bsl_helpers,
    parse_metadata_xml,
    parse_event_subscription_xml,
    parse_scheduled_job_xml,
    parse_enum_xml,
    parse_functional_option_xml,
    parse_rights_xml,
)
from rlm_tools_bsl.bsl_xml_parsers import normalize_type_string, parse_predefined_items


BSL_CODE = """\
#Область ПрограммныйИнтерфейс

Процедура ЗаполнитьДанные(Параметр1, Параметр2) Экспорт
    // тело процедуры
    Сообщить("Начало заполнения");
КонецПроцедуры

Функция ПолучитьСумму(Сумма1, Сумма2) Экспорт
    Возврат Сумма1 + Сумма2;
КонецФункции

Процедура ВнутренняяПроцедура()
    // внутренняя
КонецПроцедуры

#КонецОбласти
"""

BSL_CALLER_CODE = """\
Процедура ОбработкаЗаполнения() Экспорт
    МойМодуль.ЗаполнитьДанные(1, 2);
КонецПроцедуры

Процедура НеВызывает()
    // ЗаполнитьДанные(1, 2);
    //ЗаполнитьДанные(1, 2);
    Сообщить("ЗаполнитьДанные");
КонецПроцедуры
"""


def _create_cf_fixture(tmpdir):
    """Create a CF-style structure with BSL files."""
    # CommonModules/МойМодуль/Ext/Module.bsl
    mod_dir = os.path.join(tmpdir, "CommonModules", "МойМодуль", "Ext")
    os.makedirs(mod_dir)
    with open(os.path.join(mod_dir, "Module.bsl"), "w", encoding="utf-8") as f:
        f.write(BSL_CODE)

    # Documents/АвансовыйОтчет/Ext/ObjectModule.bsl
    doc_dir = os.path.join(tmpdir, "Documents", "АвансовыйОтчет", "Ext")
    os.makedirs(doc_dir)
    with open(os.path.join(doc_dir, "ObjectModule.bsl"), "w", encoding="utf-8") as f:
        f.write(BSL_CALLER_CODE)

    # Documents/АвансовыйОтчет/Forms/ФормаДокумента/Ext/Form/Module.bsl
    form_dir = os.path.join(tmpdir, "Documents", "АвансовыйОтчет", "Forms", "ФормаДокумента", "Ext", "Form")
    os.makedirs(form_dir)
    with open(os.path.join(form_dir, "Module.bsl"), "w", encoding="utf-8") as f:
        f.write("// form module code\n")

    # Configuration.xml
    with open(os.path.join(tmpdir, "Configuration.xml"), "w") as f:
        f.write("<Configuration/>")


def _make_bsl_fixture(tmpdir):
    """Create fixture and return bsl_helpers dict."""
    _create_cf_fixture(tmpdir)
    helpers, resolve_safe = make_helpers(tmpdir)
    format_info = detect_format(tmpdir)
    bsl = make_bsl_helpers(
        base_path=tmpdir,
        resolve_safe=resolve_safe,
        read_file_fn=helpers["read_file"],
        grep_fn=helpers["grep"],
        glob_files_fn=helpers["glob_files"],
        format_info=format_info,
    )
    return bsl, helpers


# --- Finding #3 (v1.26.0): multiline literals → no false callers in FS-fallback ---


def _make_cold_bsl_for_callers(tmpdir, modules):
    """Cold (no-index) BSL session с заданными CommonModule-исходниками → форсит
    find_callers FS-fallback. modules: {object_name: bsl_source}."""
    for obj_name, src in modules.items():
        mod_dir = os.path.join(tmpdir, "CommonModules", obj_name, "Ext")
        os.makedirs(mod_dir)
        with open(os.path.join(mod_dir, "Module.bsl"), "w", encoding="utf-8") as f:
            f.write(src)
    with open(os.path.join(tmpdir, "Configuration.xml"), "w") as f:
        f.write("<Configuration/>")
    helpers, resolve_safe = make_helpers(tmpdir)
    format_info = detect_format(tmpdir)
    return make_bsl_helpers(
        base_path=tmpdir,
        resolve_safe=resolve_safe,
        read_file_fn=helpers["read_file"],
        grep_fn=helpers["grep"],
        glob_files_fn=helpers["glob_files"],
        format_info=format_info,
    )


# Текст «вызова» ЦелеваяПроцедура(...) спрятан ВНУТРИ многострочного строкового
# литерала запроса (строки с | — продолжение строки, открытой `"ВЫБРАТЬ`).
_MULTILINE_LITERAL_MODULE = """\
Процедура СформироватьЗапрос() Экспорт
\tЗапрос = Новый Запрос;
\tЗапрос.Текст = "ВЫБРАТЬ
\t|\tТаблица.Ссылка
\t|ИЗ
\t|\tСправочник.Номенклатура КАК Таблица
\t|ГДЕ
\t|\tЦелеваяПроцедура(Таблица.Ссылка)";
\tРезультат = Запрос.Выполнить();
КонецПроцедуры
"""

# Настоящий вызов в коде — на ПЕРВОЙ строке тела процедуры (регресс + off-by-one guard).
_REAL_CALL_MODULE = """\
Процедура ВызватьЦелевую() Экспорт
\tЦелеваяПроцедура(Параметр);
КонецПроцедуры
"""


def test_fs_fallback_ignores_multiline_literal():
    """Finding #3: текст «вызова» внутри многострочного строкового литерала
    (запроса) НЕ должен давать ложного caller на FS-fallback (multiline-aware
    _scan_module видит, что строка — содержимое литерала, а не код)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl = _make_cold_bsl_for_callers(tmpdir, {"МодульСЗапросом": _MULTILINE_LITERAL_MODULE})
        results = bsl["find_callers"]("ЦелеваяПроцедура")
        assert results == []


def test_fs_fallback_finds_real_call():
    """Регресс-якорь + off-by-one guard: настоящий вызов на первой строке тела
    процедуры по-прежнему находится (scan_dict[line_idx + 1], не [line_idx])."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl = _make_cold_bsl_for_callers(tmpdir, {"МодульСВызовом": _REAL_CALL_MODULE})
        results = bsl["find_callers"]("ЦелеваяПроцедура")
        assert len(results) >= 1


def test_helper_on_partial_index_degrades(tmp_path, monkeypatch):
    """Finding #1d (helper-level, not only reader-level): a hot-path helper
    (extract_procedures) on a reader whose tables were dropped mid-rebuild does NOT
    crash — it degrades to the live fallback (parses the .bsl file)."""
    from rlm_tools_bsl.bsl_index import IndexBuilder, IndexReader, get_index_db_path

    tmpdir = str(tmp_path)
    _create_cf_fixture(tmpdir)
    monkeypatch.setenv("RLM_INDEX_DIR", os.path.join(tmpdir, ".idx"))
    IndexBuilder().build(tmpdir, build_calls=False)
    db_path = get_index_db_path(tmpdir)
    reader = IndexReader(db_path)
    try:
        # simulate mid-rebuild: drop core tables under the open reader
        w = sqlite3.connect(str(db_path))
        w.execute("DROP TABLE IF EXISTS methods")
        w.execute("DROP TABLE IF EXISTS modules")
        w.commit()
        w.close()

        helpers, resolve_safe = make_helpers(tmpdir)
        format_info = detect_format(tmpdir)
        bsl = make_bsl_helpers(
            base_path=tmpdir,
            resolve_safe=resolve_safe,
            read_file_fn=helpers["read_file"],
            grep_fn=helpers["grep"],
            glob_files_fn=helpers["glob_files"],
            format_info=format_info,
            idx_reader=reader,
        )
        # idx_reader.get_methods_by_path → None sentinel → live fallback (no crash).
        procs = bsl["extract_procedures"]("CommonModules/МойМодуль/Ext/Module.bsl")
        assert isinstance(procs, list)
        assert "ЗаполнитьДанные" in [p["name"] for p in procs]
    finally:
        reader.close()


# --- find_module ---


def test_find_module_by_name(bsl_env):
    results = bsl_env.bsl["find_module"]("МойМодуль")
    assert len(results) >= 1
    assert any(r["object_name"] == "МойМодуль" for r in results)


def test_find_module_by_path_fragment(bsl_env):
    results = bsl_env.bsl["find_module"]("АвансовыйОтчет")
    assert len(results) >= 1


def test_find_module_case_insensitive(bsl_env):
    results = bsl_env.bsl["find_module"]("моймодуль")
    assert len(results) >= 1


def test_find_module_no_results(bsl_env):
    results = bsl_env.bsl["find_module"]("НесуществующийМодуль")
    assert len(results) == 0


# --- find_by_type ---


def test_find_by_type(bsl_env):
    results = bsl_env.bsl["find_by_type"]("Documents")
    assert len(results) >= 1
    assert all(r["category"] == "Documents" for r in results)


def test_find_by_type_with_name(bsl_env):
    results = bsl_env.bsl["find_by_type"]("CommonModules", "МойМодуль")
    assert len(results) >= 1


# --- extract_procedures ---


def test_extract_procedures(bsl_env):
    # Find the module path first
    modules = bsl_env.bsl["find_module"]("МойМодуль")
    assert len(modules) >= 1
    path = modules[0]["path"]

    procs = bsl_env.bsl["extract_procedures"](path)
    assert len(procs) == 3
    names = [p["name"] for p in procs]
    assert "ЗаполнитьДанные" in names
    assert "ПолучитьСумму" in names
    assert "ВнутренняяПроцедура" in names


def test_extract_procedures_export_flag(bsl_env):
    modules = bsl_env.bsl["find_module"]("МойМодуль")
    path = modules[0]["path"]

    procs = bsl_env.bsl["extract_procedures"](path)
    by_name = {p["name"]: p for p in procs}
    assert by_name["ЗаполнитьДанные"]["is_export"] is True
    assert by_name["ПолучитьСумму"]["is_export"] is True
    assert by_name["ВнутренняяПроцедура"]["is_export"] is False


def test_extract_procedures_has_end_line(bsl_env):
    modules = bsl_env.bsl["find_module"]("МойМодуль")
    path = modules[0]["path"]

    procs = bsl_env.bsl["extract_procedures"](path)
    for p in procs:
        assert p["end_line"] is not None
        assert p["end_line"] > p["line"]


# --- find_exports ---


def test_find_exports(bsl_env):
    modules = bsl_env.bsl["find_module"]("МойМодуль")
    path = modules[0]["path"]

    exports = bsl_env.bsl["find_exports"](path)
    assert len(exports) == 2
    names = [e["name"] for e in exports]
    assert "ЗаполнитьДанные" in names
    assert "ПолучитьСумму" in names
    assert "ВнутренняяПроцедура" not in names


# --- safe_grep ---


def test_safe_grep_with_hint(bsl_env):
    results = bsl_env.bsl["safe_grep"]("ЗаполнитьДанные", name_hint="АвансовыйОтчет")
    assert len(results) >= 1


def test_safe_grep_rejects_catastrophic(bsl_env):
    """Finding #2 (v1.26.0): safe_grep отклоняет catastrophic-паттерны ValueError'ом."""
    for pat in ("(a+)+b", "(a*)*", r"(\d+)+$", "((ab)+)+"):
        with pytest.raises(ValueError):
            bsl_env.bsl["safe_grep"](pat)


def test_safe_grep_bad_pattern_no_index_warmup(bsl_env):
    """Guard — самое первое действие safe_grep: на сессии без индекса bad-pattern
    мгновенно даёт ValueError (не [], не прогрев индекса, не зависание; _grep_one
    глотает Exception, поэтому полагаться на guard внутри grep_fn нельзя)."""
    with pytest.raises(ValueError):
        bsl_env.bsl["safe_grep"]("(a+)+b", name_hint="АвансовыйОтчет")


def test_safe_grep_invalid_regex_clean_error(tmp_path):
    """#5 (v1.28.0): синтаксически битый regex → чистый ValueError с подсказкой
    (не сырой ``re.error`` traceback), и валидация происходит ДО прогрева индекса
    (как catastrophic-guard, а не после — на исходной строке 1134). Spy на warmup-I/O
    (``glob_files_fn('**/*.bsl')``) подтверждает, что ``_ensure_index`` не отработал."""
    tmpdir = str(tmp_path)
    _create_cf_fixture(tmpdir)
    helpers, resolve_safe = make_helpers(tmpdir)
    format_info = detect_format(tmpdir)

    glob_calls: list[str] = []
    real_glob = helpers["glob_files"]

    def _spy_glob(pattern, *a, **k):
        glob_calls.append(pattern)
        return real_glob(pattern, *a, **k)

    bsl = make_bsl_helpers(
        base_path=tmpdir,
        resolve_safe=resolve_safe,
        read_file_fn=helpers["read_file"],
        grep_fn=helpers["grep"],
        glob_files_fn=_spy_glob,
        format_info=format_info,
    )
    glob_calls.clear()  # изолируем ассерт на сам вызов safe_grep

    # "(" — синтаксически некорректный (unbalanced), но НЕ catastrophic-nesting.
    with pytest.raises(ValueError) as ei:
        bsl["safe_grep"]("(", name_hint="АвансовыйОтчет")
    msg = str(ei.value)
    assert "regex" in msg.lower()  # обёрнутое сообщение, не голое "missing ), unterminated subpattern"
    assert glob_calls == []  # валидация ДО _ensure_index-прогрева (index не грелся)


def test_safe_grep_literal_unaffected(bsl_env):
    """Литералы (в т.ч. git fast-path) guard'ом не затронуты."""
    results = bsl_env.bsl["safe_grep"]("ЗаполнитьДанные", name_hint="АвансовыйОтчет")
    assert len(results) >= 1


def test_safe_grep_without_hint(bsl_env):
    results = bsl_env.bsl["safe_grep"]("Процедура")
    assert len(results) >= 1


def test_safe_grep_parallel_order(bsl_env):
    """Parallel safe_grep returns results sorted by (file, line)."""
    results = bsl_env.bsl["safe_grep"]("Процедура", max_files=50)
    assert len(results) >= 1
    # Verify sort order: (file, line) ascending
    for i in range(1, len(results)):
        prev = (results[i - 1].get("file", ""), results[i - 1].get("line", 0))
        curr = (results[i].get("file", ""), results[i].get("line", 0))
        assert prev <= curr, f"Order violation: {prev} > {curr}"


# --- read_procedure ---


def test_read_procedure(bsl_env):
    modules = bsl_env.bsl["find_module"]("МойМодуль")
    path = modules[0]["path"]

    body = bsl_env.bsl["read_procedure"](path, "ЗаполнитьДанные")
    assert body is not None
    assert "ЗаполнитьДанные" in body
    assert "КонецПроцедуры" in body


def test_read_procedure_not_found(bsl_env):
    modules = bsl_env.bsl["find_module"]("МойМодуль")
    path = modules[0]["path"]

    body = bsl_env.bsl["read_procedure"](path, "НесуществующаяПроцедура")
    assert body is None


# --- find_callers ---


def test_find_callers(bsl_env):
    results = bsl_env.bsl["find_callers"]("ЗаполнитьДанные")
    assert len(results) >= 1
    # Should find the call in АвансовыйОтчет
    assert any("АвансовыйОтчет" in r.get("file", "") for r in results)


def test_find_callers_with_hint(bsl_env):
    results = bsl_env.bsl["find_callers"]("ЗаполнитьДанные", module_hint="АвансовыйОтчет")
    assert len(results) >= 1


# --- parse_metadata_xml / parse_object_xml ---

CATALOG_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"
    xmlns:v8="http://v8.1c.ru/8.1/data/core"
    xmlns:xr="http://v8.1c.ru/8.3/xcf/readable">
<Catalog>
  <Properties>
    <Name>ВидыСпецодежды</Name>
    <Synonym>
      <v8:item>
        <v8:lang>ru</v8:lang>
        <v8:content>Виды спецодежды</v8:content>
      </v8:item>
    </Synonym>
  </Properties>
  <Attribute>
    <Properties>
      <Name>Безразмерный</Name>
      <Synonym>
        <v8:item>
          <v8:lang>ru</v8:lang>
          <v8:content>Безразмерный</v8:content>
        </v8:item>
      </Synonym>
      <Type>
        <v8:Type>xs:boolean</v8:Type>
      </Type>
    </Properties>
  </Attribute>
  <TabularSection>
    <Properties>
      <Name>Размеры</Name>
      <Synonym>
        <v8:item>
          <v8:lang>ru</v8:lang>
          <v8:content>Размеры</v8:content>
        </v8:item>
      </Synonym>
    </Properties>
    <Attribute>
      <Properties>
        <Name>Размер</Name>
        <Synonym>
          <v8:item>
            <v8:lang>ru</v8:lang>
            <v8:content>Размер</v8:content>
          </v8:item>
        </Synonym>
        <Type>
          <v8:Type>xs:string</v8:Type>
        </Type>
      </Properties>
    </Attribute>
  </TabularSection>
</Catalog>
</MetaDataObject>
"""

SUBSYSTEM_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"
    xmlns:v8="http://v8.1c.ru/8.1/data/core"
    xmlns:xr="http://v8.1c.ru/8.3/xcf/readable">
<Subsystem>
  <Properties>
    <Name>Спецодежда</Name>
    <Synonym>
      <v8:item>
        <v8:lang>ru</v8:lang>
        <v8:content>Спецодежда (ктн)</v8:content>
      </v8:item>
    </Synonym>
    <Content>
      <xr:Item>Catalog.ктнВидыСпецодежды</xr:Item>
      <xr:Item>Document.ктнЗаявкаНаВыдачуСпецодежды</xr:Item>
    </Content>
  </Properties>
</Subsystem>
</MetaDataObject>
"""

REGISTER_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"
    xmlns:v8="http://v8.1c.ru/8.1/data/core">
<AccumulationRegister>
  <Properties>
    <Name>ЗаказыНаВыдачу</Name>
    <Synonym>
      <v8:item>
        <v8:lang>ru</v8:lang>
        <v8:content>Заказы на выдачу спецодежды</v8:content>
      </v8:item>
    </Synonym>
  </Properties>
  <Dimension>
    <Properties>
      <Name>ВидСпецодежды</Name>
      <Synonym>
        <v8:item>
          <v8:lang>ru</v8:lang>
          <v8:content>Вид спецодежды</v8:content>
        </v8:item>
      </Synonym>
      <Type>
        <v8:Type>CatalogRef.ктнВидыСпецодежды</v8:Type>
      </Type>
    </Properties>
  </Dimension>
  <Resource>
    <Properties>
      <Name>Количество</Name>
      <Synonym>
        <v8:item>
          <v8:lang>ru</v8:lang>
          <v8:content>Количество</v8:content>
        </v8:item>
      </Synonym>
      <Type>
        <v8:Type>xs:decimal</v8:Type>
      </Type>
    </Properties>
  </Resource>
</AccumulationRegister>
</MetaDataObject>
"""


# --- MDO format test data ---

MDO_DOCUMENT_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<mdclass:Document xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
    xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass"
    xmlns:core="http://g5.1c.ru/v8/dt/mcore"
    uuid="abcd1234-0000-0000-0000-000000000001">
  <name>ЗаявкаНаВыдачу</name>
  <synonym>
    <key>ru</key>
    <value>Заявка на выдачу спецодежды</value>
  </synonym>
  <attributes uuid="abcd1234-0000-0000-0000-000000000002">
    <name>ФизЛицо</name>
    <synonym>
      <key>ru</key>
      <value>Физическое лицо</value>
    </synonym>
    <type>
      <types>CatalogRef.ФизическиеЛица</types>
    </type>
  </attributes>
  <attributes uuid="abcd1234-0000-0000-0000-000000000003">
    <name>Организация</name>
    <synonym>
      <key>ru</key>
      <value>Организация</value>
    </synonym>
    <type>
      <types>CatalogRef.Организации</types>
    </type>
  </attributes>
  <tabularSections uuid="abcd1234-0000-0000-0000-000000000010">
    <name>ВидыСпецодежды</name>
    <synonym>
      <key>ru</key>
      <value>Виды спецодежды</value>
    </synonym>
    <attributes uuid="abcd1234-0000-0000-0000-000000000011">
      <name>ВидСпецодежды</name>
      <synonym>
        <key>ru</key>
        <value>Вид спецодежды</value>
      </synonym>
      <type>
        <types>CatalogRef.ВидыСпецодежды</types>
      </type>
    </attributes>
    <attributes uuid="abcd1234-0000-0000-0000-000000000012">
      <name>Количество</name>
      <type>
        <types>Number</types>
      </type>
    </attributes>
  </tabularSections>
  <forms>ФормаДокумента</forms>
  <forms>ФормаСписка</forms>
  <commands>Печать</commands>
</mdclass:Document>
"""

MDO_REGISTER_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<mdclass:AccumulationRegister xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass"
    uuid="abcd1234-0000-0000-0000-000000000020">
  <name>ТоварыНаСкладах</name>
  <synonym>
    <key>ru</key>
    <value>Товары на складах</value>
  </synonym>
  <dimensions uuid="abcd1234-0000-0000-0000-000000000021">
    <name>Номенклатура</name>
    <synonym>
      <key>ru</key>
      <value>Номенклатура</value>
    </synonym>
    <type>
      <types>CatalogRef.Номенклатура</types>
    </type>
  </dimensions>
  <dimensions uuid="abcd1234-0000-0000-0000-000000000022">
    <name>Склад</name>
    <type>
      <types>CatalogRef.Склады</types>
    </type>
  </dimensions>
  <resources uuid="abcd1234-0000-0000-0000-000000000023">
    <name>Количество</name>
    <type>
      <types>Number</types>
    </type>
  </resources>
</mdclass:AccumulationRegister>
"""

MDO_SUBSYSTEM_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<mdclass:Subsystem xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass"
    uuid="abcd1234-0000-0000-0000-000000000030">
  <name>Спецодежда</name>
  <synonym>
    <key>ru</key>
    <value>Спецодежда</value>
  </synonym>
  <content>Catalog.ВидыСпецодежды</content>
  <content>Document.ЗаявкаНаВыдачу</content>
  <content>AccumulationRegister.ТоварыНаСкладах</content>
</mdclass:Subsystem>
"""


def test_parse_catalog_xml():
    result = parse_metadata_xml(CATALOG_XML)
    assert result["object_type"] == "Catalog"
    assert result["name"] == "ВидыСпецодежды"
    assert result["synonym"] == "Виды спецодежды"
    assert len(result["attributes"]) == 1
    assert result["attributes"][0]["name"] == "Безразмерный"
    assert result["attributes"][0]["type"] == "xs:boolean"
    assert len(result["tabular_sections"]) == 1
    ts = result["tabular_sections"][0]
    assert ts["name"] == "Размеры"
    assert len(ts["attributes"]) == 1
    assert ts["attributes"][0]["name"] == "Размер"


def test_parse_subsystem_xml():
    result = parse_metadata_xml(SUBSYSTEM_XML)
    assert result["object_type"] == "Subsystem"
    assert result["name"] == "Спецодежда"
    assert "content" in result
    assert "Catalog.ктнВидыСпецодежды" in result["content"]
    assert "Document.ктнЗаявкаНаВыдачуСпецодежды" in result["content"]


def test_parse_register_xml():
    result = parse_metadata_xml(REGISTER_XML)
    assert result["object_type"] == "AccumulationRegister"
    assert result["name"] == "ЗаказыНаВыдачу"
    assert len(result["dimensions"]) == 1
    assert result["dimensions"][0]["name"] == "ВидСпецодежды"
    assert len(result["resources"]) == 1
    assert result["resources"][0]["name"] == "Количество"


# --- Finding #5 (v1.26.0): parse_metadata_xml error contract (dict | None) ---


def test_parse_metadata_xml_malformed_returns_none():
    """Битый / пустой / whitespace / не-XML контент → None (контракт сиблинга
    parse_form_xml), а не ET.ParseError."""
    assert parse_metadata_xml("<MetaDataObject><Catalog><unclosed>") is None
    assert parse_metadata_xml("") is None
    assert parse_metadata_xml("   \n\t ") is None
    assert parse_metadata_xml("not xml at all") is None


def test_parse_metadata_xml_nonparse_error_propagates(monkeypatch):
    """Контракт «только ParseError→None»: не-ParseError НЕ проглатывается в None,
    а пробрасывается к callsite-обёрткам (защита от ошибочного `except Exception`)."""
    import rlm_tools_bsl.bsl_xml_parsers as bxp

    def _boom(_content):
        raise ValueError("non-parse failure")

    monkeypatch.setattr(bxp.ET, "fromstring", _boom)
    with pytest.raises(ValueError):
        bxp.parse_metadata_xml(CATALOG_XML)


def test_parse_metadata_xml_valid_unchanged():
    """Регресс-якорь: валидный CF-XML по-прежнему парсится в dict."""
    result = parse_metadata_xml(CATALOG_XML)
    assert isinstance(result, dict)
    assert result["name"] == "ВидыСпецодежды"


def test_parse_object_xml_via_sandbox():
    """Test parse_object_xml as registered in sandbox helpers."""
    with tempfile.TemporaryDirectory() as tmpdir:
        _create_cf_fixture(tmpdir)
        # Write a metadata XML file
        xml_dir = os.path.join(tmpdir, "Catalogs", "ВидыСО")
        os.makedirs(xml_dir)
        # Write the XML at the catalog level
        with open(os.path.join(xml_dir, "ВидыСО.xml"), "w", encoding="utf-8") as f:
            f.write(CATALOG_XML)

        helpers, resolve_safe = make_helpers(tmpdir)
        format_info = detect_format(tmpdir)
        bsl = make_bsl_helpers(
            base_path=tmpdir,
            resolve_safe=resolve_safe,
            read_file_fn=helpers["read_file"],
            grep_fn=helpers["grep"],
            glob_files_fn=helpers["glob_files"],
            format_info=format_info,
        )
        # Use relative path
        result = bsl["parse_object_xml"]("Catalogs/ВидыСО/ВидыСО.xml")
        assert result["object_type"] == "Catalog"
        assert result["name"] == "ВидыСпецодежды"


def test_parse_object_xml_directory_path():
    """Test parse_object_xml with a directory path (auto-resolves to XML)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        _create_cf_fixture(tmpdir)
        # Create CF-style Document metadata: Documents/TestDoc/Ext/Document.xml
        doc_xml_dir = os.path.join(tmpdir, "Documents", "АвансовыйОтчет", "Ext")
        os.makedirs(doc_xml_dir, exist_ok=True)
        with open(os.path.join(doc_xml_dir, "Document.xml"), "w", encoding="utf-8") as f:
            f.write(CATALOG_XML)  # reuse catalog XML, structure is similar enough

        helpers, resolve_safe = make_helpers(tmpdir)
        format_info = detect_format(tmpdir)
        bsl = make_bsl_helpers(
            base_path=tmpdir,
            resolve_safe=resolve_safe,
            read_file_fn=helpers["read_file"],
            grep_fn=helpers["grep"],
            glob_files_fn=helpers["glob_files"],
            format_info=format_info,
        )
        # Pass directory path — should auto-resolve to Ext/Document.xml
        result = bsl["parse_object_xml"]("Documents/АвансовыйОтчет")
        assert "name" in result
        assert result["name"] == "ВидыСпецодежды"  # from CATALOG_XML fixture


def test_parse_object_xml_malformed_raises():
    """Finding #5 п.4: agent-facing parse_object_xml на битом metadata XML
    даёт ИСКЛЮЧЕНИЕ (а не None) — иначе консьюмеры (analyze_object,
    find_custom_modifications) молча получат None вместо ловимого исключения."""
    with tempfile.TemporaryDirectory() as tmpdir:
        _create_cf_fixture(tmpdir)
        xml_dir = os.path.join(tmpdir, "Catalogs", "БитыйОбъект")
        os.makedirs(xml_dir)
        with open(os.path.join(xml_dir, "БитыйОбъект.xml"), "w", encoding="utf-8") as f:
            f.write("<MetaDataObject><Catalog><unclosed>")

        helpers, resolve_safe = make_helpers(tmpdir)
        format_info = detect_format(tmpdir)
        bsl = make_bsl_helpers(
            base_path=tmpdir,
            resolve_safe=resolve_safe,
            read_file_fn=helpers["read_file"],
            grep_fn=helpers["grep"],
            glob_files_fn=helpers["glob_files"],
            format_info=format_info,
        )
        with pytest.raises(ValueError):
            bsl["parse_object_xml"]("Catalogs/БитыйОбъект/БитыйОбъект.xml")


# --- MDO format tests ---


def test_parse_mdo_document():
    result = parse_metadata_xml(MDO_DOCUMENT_XML)
    assert result["object_type"] == "Document"
    assert result["name"] == "ЗаявкаНаВыдачу"
    assert result["synonym"] == "Заявка на выдачу спецодежды"
    # Attributes
    assert len(result["attributes"]) == 2
    assert result["attributes"][0]["name"] == "ФизЛицо"
    assert result["attributes"][0]["synonym"] == "Физическое лицо"
    assert result["attributes"][0]["type"] == "CatalogRef.ФизическиеЛица"
    assert result["attributes"][1]["name"] == "Организация"
    # Tabular section
    assert len(result["tabular_sections"]) == 1
    ts = result["tabular_sections"][0]
    assert ts["name"] == "ВидыСпецодежды"
    assert ts["synonym"] == "Виды спецодежды"
    assert len(ts["attributes"]) == 2
    assert ts["attributes"][0]["name"] == "ВидСпецодежды"
    assert ts["attributes"][1]["name"] == "Количество"
    # Forms and commands
    assert result["forms"] == ["ФормаДокумента", "ФормаСписка"]
    assert result["commands"] == ["Печать"]


def test_parse_mdo_register():
    result = parse_metadata_xml(MDO_REGISTER_XML)
    assert result["object_type"] == "AccumulationRegister"
    assert result["name"] == "ТоварыНаСкладах"
    assert result["synonym"] == "Товары на складах"
    assert len(result["dimensions"]) == 2
    assert result["dimensions"][0]["name"] == "Номенклатура"
    assert result["dimensions"][0]["type"] == "CatalogRef.Номенклатура"
    assert result["dimensions"][1]["name"] == "Склад"
    assert len(result["resources"]) == 1
    assert result["resources"][0]["name"] == "Количество"


def test_parse_mdo_subsystem():
    result = parse_metadata_xml(MDO_SUBSYSTEM_XML)
    assert result["object_type"] == "Subsystem"
    assert result["name"] == "Спецодежда"
    assert len(result["content"]) == 3
    assert "Catalog.ВидыСпецодежды" in result["content"]
    assert "Document.ЗаявкаНаВыдачу" in result["content"]
    assert "AccumulationRegister.ТоварыНаСкладах" in result["content"]


# --- find_callers_context ---


def test_find_callers_context_basic(bsl_env):
    """Basic: finds caller with all required fields."""
    result = bsl_env.bsl["find_callers_context"]("ЗаполнитьДанные")
    callers = result["callers"]
    assert len(callers) >= 1
    c = callers[0]
    # All required fields present
    assert "file" in c
    assert "caller_name" in c
    assert "caller_is_export" in c
    assert "line" in c
    assert "context" in c
    assert "object_name" in c
    assert "category" in c
    assert "module_type" in c
    # Caller is ОбработкаЗаполнения
    assert c["caller_name"] == "ОбработкаЗаполнения"


def test_find_callers_context_with_hint(bsl_env):
    """With module_hint: determines export scope, finds caller across files."""
    result = bsl_env.bsl["find_callers_context"]("ЗаполнитьДанные", module_hint="МойМодуль")
    callers = result["callers"]
    assert len(callers) >= 1
    assert any(c["caller_name"] == "ОбработкаЗаполнения" for c in callers)


def test_find_callers_context_no_callers(bsl_env):
    """Internal procedure with no callers returns empty list."""
    result = bsl_env.bsl["find_callers_context"]("ВнутренняяПроцедура")
    assert result["callers"] == []


def test_find_callers_context_ignores_comments(bsl_env):
    """Calls in comments (// with and without space) should be ignored."""
    result = bsl_env.bsl["find_callers_context"]("ЗаполнитьДанные")
    caller_names = [c["caller_name"] for c in result["callers"]]
    assert "НеВызывает" not in caller_names


def test_find_callers_context_ignores_strings(bsl_env):
    """Calls inside string literals should be ignored."""
    result = bsl_env.bsl["find_callers_context"]("ЗаполнитьДанные")
    caller_names = [c["caller_name"] for c in result["callers"]]
    # НеВызывает has the name only in a string literal (after comment lines are stripped)
    assert "НеВызывает" not in caller_names


def test_find_callers_context_caller_metadata(bsl_env):
    """Verify caller metadata: category, object_name, module_type."""
    result = bsl_env.bsl["find_callers_context"]("ЗаполнитьДанные")
    callers = result["callers"]
    c = next(c for c in callers if c["caller_name"] == "ОбработкаЗаполнения")
    assert c["category"] == "Documents"
    assert c["object_name"] == "АвансовыйОтчет"
    assert c["module_type"] == "ObjectModule"


def test_find_callers_context_qualified_call(bsl_env):
    """Qualified call МойМодуль.ЗаполнитьДанные() is found by proc name alone."""
    result = bsl_env.bsl["find_callers_context"]("ЗаполнитьДанные")
    callers = result["callers"]
    # The call is МойМодуль.ЗаполнитьДанные(1, 2) — should be found
    assert any("МойМодуль.ЗаполнитьДанные" in c["context"] for c in callers)


def test_find_callers_context_meta(bsl_env):
    """Result contains _meta with total_callers, returned, offset, has_more."""
    result = bsl_env.bsl["find_callers_context"]("ЗаполнитьДанные")
    meta = result["_meta"]
    assert "total_callers" in meta
    assert "returned" in meta
    assert "offset" in meta
    assert "has_more" in meta
    assert meta["has_more"] is False  # small fixture, all scanned


def test_find_callers_context_pagination(bsl_env):
    """Pagination: limit=1 → has_more=True, offset=1 → next batch."""
    # First page: limit=1
    result1 = bsl_env.bsl["find_callers_context"]("ЗаполнитьДанные", limit=1)
    meta1 = result1["_meta"]
    assert "has_more" in meta1
    assert "total_callers" in meta1
    assert "returned" in meta1
    assert "offset" in meta1
    if meta1["has_more"]:
        # Second page
        result2 = bsl_env.bsl["find_callers_context"]("ЗаполнитьДанные", offset=1, limit=1)
        assert result2["_meta"]["returned"] >= 0


# --- Composite helpers ---


SUBSYSTEM_CF_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"
                xmlns:v8="http://v8.1c.ru/8.1/data/core"
                xmlns:xr="http://v8.1c.ru/8.3/xcf/readable">
<Subsystem>
<Properties>
<Name>ктнСпецодежда</Name>
<Synonym><v8:item><v8:lang>ru</v8:lang><v8:content>Спецодежда</v8:content></v8:item></Synonym>
<Content>
<xr:Item>Catalog.ктнВидыСпецодежды</xr:Item>
<xr:Item>Document.ВнутреннееПотребление</xr:Item>
<xr:Item>Document.ктнЗаявкаНаВыдачуСпецодежды</xr:Item>
</Content>
</Properties>
</Subsystem>
</MetaDataObject>
"""


def _make_subsystem_fixture(tmpdir):
    """Create fixture with a subsystem XML."""
    # Add subsystem XML to existing fixture
    sub_dir = os.path.join(
        tmpdir,
        "Subsystems",
        "Администрирование",
        "Subsystems",
        "ктнСпецодежда",
    )
    os.makedirs(sub_dir, exist_ok=True)
    with open(os.path.join(sub_dir, "ктнСпецодежда.xml"), "w", encoding="utf-8") as f:
        f.write(SUBSYSTEM_CF_XML)
    # Now create the rest of the fixture (BSL files, Configuration.xml)
    helpers, resolve_safe = make_helpers(tmpdir)
    # Create CF structure manually (avoid _create_cf_fixture which fails on existing dirs)
    mod_dir = os.path.join(tmpdir, "CommonModules", "МойМодуль", "Ext")
    os.makedirs(mod_dir, exist_ok=True)
    with open(os.path.join(mod_dir, "Module.bsl"), "w", encoding="utf-8") as f:
        f.write(BSL_CODE)
    doc_dir = os.path.join(tmpdir, "Documents", "АвансовыйОтчет", "Ext")
    os.makedirs(doc_dir, exist_ok=True)
    with open(os.path.join(doc_dir, "ObjectModule.bsl"), "w", encoding="utf-8") as f:
        f.write(BSL_CALLER_CODE)
    with open(os.path.join(tmpdir, "Configuration.xml"), "w") as f:
        f.write("<Configuration/>")
    format_info = detect_format(tmpdir)
    bsl = make_bsl_helpers(
        base_path=tmpdir,
        resolve_safe=resolve_safe,
        read_file_fn=helpers["read_file"],
        grep_fn=helpers["grep"],
        glob_files_fn=helpers["glob_files"],
        format_info=format_info,
    )
    return bsl, helpers


def test_analyze_subsystem_found():
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_subsystem_fixture(tmpdir)
        result = bsl["analyze_subsystem"]("Спецодежда")
        assert result["subsystems_found"] >= 1
        sub = result["subsystems"][0]
        assert sub["synonym"] == "Спецодежда"
        assert len(sub["custom_objects"]) >= 1
        custom_names = [o["name"] for o in sub["custom_objects"]]
        assert "ктнВидыСпецодежды" in custom_names
        standard_names = [o["name"] for o in sub["standard_objects"]]
        assert "ВнутреннееПотребление" in standard_names


def test_analyze_subsystem_not_found(bsl_env):
    result = bsl_env.bsl["analyze_subsystem"]("НесуществующаяПодсистема")
    assert "error" in result


BSL_CUSTOM_CODE = """\
#Область ктнДоработки

Процедура ктнОбработкаСпецодежды() Экспорт
    // нетиповая процедура
КонецПроцедуры

Процедура ТиповаяПроцедура()
    // типовая
КонецПроцедуры

#КонецОбласти
"""


def test_find_custom_modifications():
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create object with custom code
        doc_dir = os.path.join(tmpdir, "Documents", "ТестДок", "Ext")
        os.makedirs(doc_dir)
        with open(os.path.join(doc_dir, "ObjectModule.bsl"), "w", encoding="utf-8") as f:
            f.write(BSL_CUSTOM_CODE)
        with open(os.path.join(tmpdir, "Configuration.xml"), "w") as f:
            f.write("<Configuration/>")

        helpers, resolve_safe = make_helpers(tmpdir)
        format_info = detect_format(tmpdir)
        bsl = make_bsl_helpers(
            base_path=tmpdir,
            resolve_safe=resolve_safe,
            read_file_fn=helpers["read_file"],
            grep_fn=helpers["grep"],
            glob_files_fn=helpers["glob_files"],
            format_info=format_info,
        )

        result = bsl["find_custom_modifications"]("ТестДок", custom_prefixes=["ктн"])
        assert result["modules_analyzed"] >= 1
        assert len(result["modifications"]) >= 1
        mod = result["modifications"][0]
        custom_proc_names = [p["name"] for p in mod["custom_procedures"]]
        assert "ктнОбработкаСпецодежды" in custom_proc_names
        assert "ТиповаяПроцедура" not in custom_proc_names
        region_names = [r["name"] for r in mod["custom_regions"]]
        assert "ктнДоработки" in region_names
        # Check prefix_source and prefixes_used in response
        assert result["prefix_source"] == "user"
        assert result["prefixes_used"] == ["ктн"]


def test_find_custom_modifications_parse_error():
    """parse_object_xml failure returns diagnostic parse_error field."""
    with tempfile.TemporaryDirectory() as tmpdir:
        doc_dir = os.path.join(tmpdir, "Documents", "ТестДок", "Ext")
        os.makedirs(doc_dir)
        with open(os.path.join(doc_dir, "ObjectModule.bsl"), "w", encoding="utf-8") as f:
            f.write("Процедура тст_Тест()\nКонецПроцедуры\n")
        # Write invalid XML so parse_object_xml fails
        with open(os.path.join(doc_dir, "Document.xml"), "w") as f:
            f.write("NOT-XML{{{{")
        with open(os.path.join(tmpdir, "Configuration.xml"), "w") as f:
            f.write("<Configuration/>")

        helpers, resolve_safe = make_helpers(tmpdir)
        format_info = detect_format(tmpdir)
        bsl = make_bsl_helpers(
            base_path=tmpdir,
            resolve_safe=resolve_safe,
            read_file_fn=helpers["read_file"],
            grep_fn=helpers["grep"],
            glob_files_fn=helpers["glob_files"],
            format_info=format_info,
        )

        result = bsl["find_custom_modifications"]("ТестДок", custom_prefixes=["тст"])
        assert "parse_error" in result
        assert result["modules_analyzed"] >= 1


def test_resolve_object_xml_edt_mdo():
    """_resolve_object_xml finds EDT-pattern {path}/{Name}.mdo."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # EDT structure: Documents/ТестДок/ТестДок.mdo
        doc_dir = os.path.join(tmpdir, "Documents", "ТестДок")
        os.makedirs(doc_dir)
        mdo_path = os.path.join(doc_dir, "ТестДок.mdo")
        with open(mdo_path, "w", encoding="utf-8") as f:
            f.write(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<mdclass:Document xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass"'
                ' uuid="00000000-0000-0000-0000-000000000001">\n'
                "  <name>ТестДок</name>\n"
                "</mdclass:Document>\n"
            )
        # Also create a BSL file so find_module works
        bsl_dir = os.path.join(doc_dir)
        with open(os.path.join(bsl_dir, "ObjectModule.bsl"), "w", encoding="utf-8") as f:
            f.write("Процедура Тест()\nКонецПроцедуры\n")
        with open(os.path.join(tmpdir, "Configuration.mdo"), "w", encoding="utf-8") as f:
            f.write(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<mdclass:Configuration xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass"/>\n'
            )

        helpers, resolve_safe = make_helpers(tmpdir)
        format_info = detect_format(tmpdir)
        bsl = make_bsl_helpers(
            base_path=tmpdir,
            resolve_safe=resolve_safe,
            read_file_fn=helpers["read_file"],
            grep_fn=helpers["grep"],
            glob_files_fn=helpers["glob_files"],
            format_info=format_info,
        )

        # _resolve_object_xml is internal, test through parse_object_xml
        result = bsl["parse_object_xml"]("Documents/ТестДок")
        # Should resolve to ТестДок.mdo and attempt to parse
        assert isinstance(result, dict)


def test_find_custom_modifications_extension_prefix_threshold():
    """Extension mode uses threshold=1 for prefix detection."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create objects with a prefix that appears only once
        cat_dir = os.path.join(tmpdir, "Catalogs", "тст_Справочник", "Ext")
        os.makedirs(cat_dir)
        with open(os.path.join(cat_dir, "ObjectModule.bsl"), "w", encoding="utf-8") as f:
            f.write("Процедура тст_Метод()\nКонецПроцедуры\n")
        with open(os.path.join(tmpdir, "Configuration.xml"), "w") as f:
            f.write("<Configuration/>")

        helpers, resolve_safe = make_helpers(tmpdir)
        format_info = detect_format(tmpdir)

        # Without idx_reader (config_role unknown), threshold=3 → prefix "тст" won't be detected
        bsl = make_bsl_helpers(
            base_path=tmpdir,
            resolve_safe=resolve_safe,
            read_file_fn=helpers["read_file"],
            grep_fn=helpers["grep"],
            glob_files_fn=helpers["glob_files"],
            format_info=format_info,
        )
        auto_prefixes = bsl["_detected_prefixes"]()
        # Only 1 object with prefix тст → below threshold 3 → not detected
        assert "тст" not in auto_prefixes


def test_analyze_object(bsl_env):
    result = bsl_env.bsl["analyze_object"]("МойМодуль")
    assert result["name"] == "МойМодуль"
    assert result["category"] == "CommonModules"
    assert len(result["modules"]) >= 1
    mod = result["modules"][0]
    assert mod["procedures_count"] == 3
    assert mod["exports_count"] == 2


# === EventSubscription / ScheduledJob XML parsers ===

EVENT_SUB_CF_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"
    xmlns:v8="http://v8.1c.ru/8.1/data/core"
    xmlns:cfg="http://v8.1c.ru/8.1/data/enterprise/current-config">
<EventSubscription uuid="ba1f402d-0000-0000-0000-000000000001">
  <Properties>
    <Name>ЗаписатьВерсиюДокумента</Name>
    <Synonym>
      <v8:item><v8:lang>ru</v8:lang><v8:content>Записать версию документа</v8:content></v8:item>
    </Synonym>
    <Source>
      <v8:Type>cfg:DocumentObject.АвансовыйОтчет</v8:Type>
      <v8:Type>cfg:DocumentObject.ЗаказКлиента</v8:Type>
    </Source>
    <Event>BeforeWrite</Event>
    <Handler>CommonModule.ВерсионированиеОбъектовСобытия.ЗаписатьВерсиюДокумента</Handler>
  </Properties>
</EventSubscription>
</MetaDataObject>
"""

EVENT_SUB_MDO_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<mdclass:EventSubscription xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass"
    uuid="7ce50cee-0000-0000-0000-000000000001">
  <name>тст_ЗаписатьВерсиюДокумента</name>
  <synonym>
    <key>ru</key>
    <value>Записать версию документа</value>
  </synonym>
  <source>
    <types>DocumentObject.АвансовыйОтчет</types>
    <types>DocumentObject.СчетФактураВыданный</types>
  </source>
  <event>BeforeWrite</event>
  <handler>CommonModule.ВерсионированиеОбъектовСобытия.ЗаписатьВерсиюДокумента</handler>
</mdclass:EventSubscription>
"""

SCHEDULED_JOB_CF_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"
    xmlns:v8="http://v8.1c.ru/8.1/data/core">
<ScheduledJob uuid="c7ffd8ab-0000-0000-0000-000000000001">
  <Properties>
    <Name>ЗагрузкаКурсовВалют</Name>
    <Synonym>
      <v8:item><v8:lang>ru</v8:lang><v8:content>Загрузка курсов валют</v8:content></v8:item>
    </Synonym>
    <MethodName>CommonModule.РаботаСКурсамиВалют.ЗагрузитьАктуальныйКурс</MethodName>
    <Use>false</Use>
    <Predefined>true</Predefined>
    <RestartCountOnFailure>10</RestartCountOnFailure>
    <RestartIntervalOnFailure>600</RestartIntervalOnFailure>
  </Properties>
</ScheduledJob>
</MetaDataObject>
"""

SCHEDULED_JOB_MDO_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<mdclass:ScheduledJob xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass"
    uuid="f3be2107-0000-0000-0000-000000000001">
  <name>ext_ОтправкаПодтверждения</name>
  <synonym>
    <key>ru</key>
    <value>Отправка подтверждения поставки</value>
  </synonym>
  <methodName>CommonModule.ext_РегламентныеЗадания.ОтправкаПодтверждения</methodName>
  <predefined>true</predefined>
  <restartCountOnFailure>3</restartCountOnFailure>
  <restartIntervalOnFailure>10</restartIntervalOnFailure>
</mdclass:ScheduledJob>
"""


ENUM_CF_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses" xmlns:v8="http://v8.1c.ru/8.1/data/core" xmlns:xr="http://v8.1c.ru/8.3/xcf/readable">
  <Enum><Properties><Name>СтатусыЗаказов</Name><Synonym><v8:item><v8:lang>ru</v8:lang><v8:content>Статусы заказов</v8:content></v8:item></Synonym></Properties>
  <ChildObjects>
    <EnumValue><Properties><Name>Новый</Name><Synonym><v8:item><v8:lang>ru</v8:lang><v8:content>Новый</v8:content></v8:item></Synonym></Properties></EnumValue>
    <EnumValue><Properties><Name>ВРаботе</Name><Synonym><v8:item><v8:lang>ru</v8:lang><v8:content>В работе</v8:content></v8:item></Synonym></Properties></EnumValue>
    <EnumValue><Properties><Name>Закрыт</Name><Synonym><v8:item><v8:lang>ru</v8:lang><v8:content>Закрыт</v8:content></v8:item></Synonym></Properties></EnumValue>
  </ChildObjects></Enum>
</MetaDataObject>
"""

ENUM_MDO_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<mdclass:Enum xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass">
  <name>ВажностьПроблемы</name>
  <enumValues><name>Предупреждение</name></enumValues>
  <enumValues><name>Ошибка</name></enumValues>
</mdclass:Enum>
"""

FUNCTIONAL_OPTION_CF_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses" xmlns:v8="http://v8.1c.ru/8.1/data/core" xmlns:xr="http://v8.1c.ru/8.3/xcf/readable">
  <FunctionalOption><Properties><Name>ИспользоватьСерии</Name>
    <Synonym><v8:item><v8:lang>ru</v8:lang><v8:content>Использовать серии</v8:content></v8:item></Synonym>
    <Location>Constant.ИспользоватьСерии</Location>
    <Content><xr:Object>Document.ПриобретениеТоваров</xr:Object><xr:Object>Catalog.СерииНоменклатуры</xr:Object></Content>
  </Properties></FunctionalOption>
</MetaDataObject>
"""

FUNCTIONAL_OPTION_MDO_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<mdclass:FunctionalOption xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass">
  <name>ВестиСведенияДляДекларацийПоАлкогольнойПродукции</name>
  <location>Constant.ВестиСведенияДляДекларацийПоАлкогольнойПродукции</location>
  <content>Document.ext_ЗаявлениеОВыдачеФСМ</content>
  <content>Document.ext_НакладнаяНаВыдачуФСМ</content>
</mdclass:FunctionalOption>
"""

RIGHTS_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<Rights xmlns="http://v8.1c.ru/8.2/roles" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:type="Rights">
  <setForNewObjects>false</setForNewObjects>
  <setForAttributesByDefault>true</setForAttributesByDefault>
  <independentRightsOfChildObjects>false</independentRightsOfChildObjects>
  <object><name>Document.ПриобретениеТоваров</name>
    <right><name>Read</name><value>true</value></right>
    <right><name>Update</name><value>true</value></right>
    <right><name>View</name><value>false</value></right>
  </object>
  <object><name>Catalog.Номенклатура</name>
    <right><name>Read</name><value>true</value></right>
  </object>
</Rights>
"""


# === Enum / FunctionalOption / Rights XML parser tests ===


def test_parse_enum_xml_cf():
    result = parse_enum_xml(ENUM_CF_XML)
    assert result is not None
    assert result["name"] == "СтатусыЗаказов"
    assert result["synonym"] == "Статусы заказов"
    assert len(result["values"]) == 3
    assert result["values"][0]["name"] == "Новый"
    assert result["values"][0]["synonym"] == "Новый"
    assert result["values"][1]["name"] == "ВРаботе"
    assert result["values"][1]["synonym"] == "В работе"
    assert result["values"][2]["name"] == "Закрыт"
    assert result["values"][2]["synonym"] == "Закрыт"


def test_parse_enum_xml_edt():
    result = parse_enum_xml(ENUM_MDO_XML)
    assert result is not None
    assert result["name"] == "ВажностьПроблемы"
    assert len(result["values"]) == 2
    assert result["values"][0]["name"] == "Предупреждение"
    assert result["values"][1]["name"] == "Ошибка"


def test_parse_functional_option_xml_cf():
    result = parse_functional_option_xml(FUNCTIONAL_OPTION_CF_XML)
    assert result is not None
    assert result["name"] == "ИспользоватьСерии"
    assert result["synonym"] == "Использовать серии"
    assert result["location"] == "Constant.ИспользоватьСерии"
    assert len(result["content"]) == 2
    assert "Document.ПриобретениеТоваров" in result["content"]
    assert "Catalog.СерииНоменклатуры" in result["content"]


def test_parse_functional_option_xml_edt():
    result = parse_functional_option_xml(FUNCTIONAL_OPTION_MDO_XML)
    assert result is not None
    assert result["name"] == "ВестиСведенияДляДекларацийПоАлкогольнойПродукции"
    assert result["location"] == "Constant.ВестиСведенияДляДекларацийПоАлкогольнойПродукции"
    assert len(result["content"]) == 2
    assert "Document.ext_ЗаявлениеОВыдачеФСМ" in result["content"]
    assert "Document.ext_НакладнаяНаВыдачуФСМ" in result["content"]


def test_parse_rights_xml():
    result = parse_rights_xml(RIGHTS_XML)
    assert len(result) == 2
    doc = next(r for r in result if r["object"] == "Document.ПриобретениеТоваров")
    assert "Read" in doc["rights"]
    assert "Update" in doc["rights"]
    assert "View" not in doc["rights"]  # value=false excluded
    cat = next(r for r in result if r["object"] == "Catalog.Номенклатура")
    assert cat["rights"] == ["Read"]


def test_parse_rights_xml_filter():
    result = parse_rights_xml(RIGHTS_XML, "ПриобретениеТоваров")
    assert len(result) == 1
    assert result[0]["object"] == "Document.ПриобретениеТоваров"
    assert "Read" in result[0]["rights"]


# === Integration tests for find_enum_values, find_functional_options, find_roles ===


def test_find_enum_values():
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_full_fixture(tmpdir)
        # Add Enum fixture file
        enum_dir = os.path.join(tmpdir, "Enums", "СтатусыЗаказов")
        os.makedirs(enum_dir)
        with open(os.path.join(enum_dir, "СтатусыЗаказов.xml"), "w", encoding="utf-8") as f:
            f.write(ENUM_CF_XML)
        result = bsl["find_enum_values"]("СтатусыЗаказов")
        assert "error" not in result
        assert result["name"] == "СтатусыЗаказов"
        assert len(result["values"]) == 3
        assert "file" in result


def test_find_enum_values_not_found():
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_full_fixture(tmpdir)
        result = bsl["find_enum_values"]("НесуществующееПеречисление")
        assert "error" in result


def test_find_functional_options():
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_full_fixture(tmpdir)
        # Add FunctionalOption fixture file
        fo_dir = os.path.join(tmpdir, "FunctionalOptions")
        os.makedirs(fo_dir)
        with open(os.path.join(fo_dir, "ИспользоватьСерии.xml"), "w", encoding="utf-8") as f:
            f.write(FUNCTIONAL_OPTION_CF_XML)
        result = bsl["find_functional_options"]("ПриобретениеТоваров")
        assert result["object"] == "ПриобретениеТоваров"
        assert len(result["xml_options"]) >= 1
        assert result["xml_options"][0]["name"] == "ИспользоватьСерии"


def test_find_functional_options_limit_per_bucket(tmp_path):
    """#6 (v1.28.0): ``limit`` — per-bucket cap (``xml_options`` и ``code_options``
    режутся КАЖДЫЙ независимо до N), НЕ global-cap. Дефолт (``limit=None``) — прежний
    shape ``{object, xml_options, code_options}`` без пагинационных полей."""
    tmpdir = str(tmp_path)
    # 1 code-опция: ПолучитьФункциональнуюОпцию в модуле объекта (ct=1).
    doc_dir = os.path.join(tmpdir, "Documents", "ПриобретениеТоваров", "Ext")
    os.makedirs(doc_dir)
    with open(os.path.join(doc_dir, "ObjectModule.bsl"), "w", encoding="utf-8") as f:
        f.write(
            "Процедура ПередЗаписью(Отказ) Экспорт\n"
            '    Если ПолучитьФункциональнуюОпцию("ОпцияКод") Тогда\n'
            "        Возврат;\n"
            "    КонецЕсли;\n"
            "КонецПроцедуры\n"
        )
    # 1 xml-опция: FO с Document.ПриобретениеТоваров в content (xt=1).
    os.makedirs(os.path.join(tmpdir, "FunctionalOptions"))
    with open(os.path.join(tmpdir, "FunctionalOptions", "ИспользоватьСерии.xml"), "w", encoding="utf-8") as f:
        f.write(FUNCTIONAL_OPTION_CF_XML)
    with open(os.path.join(tmpdir, "Configuration.xml"), "w") as f:
        f.write("<Configuration/>")

    helpers, resolve_safe = make_helpers(tmpdir)
    format_info = detect_format(tmpdir)
    bsl = make_bsl_helpers(
        base_path=tmpdir,
        resolve_safe=resolve_safe,
        read_file_fn=helpers["read_file"],
        grep_fn=helpers["grep"],
        glob_files_fn=helpers["glob_files"],
        format_info=format_info,
    )

    # Дефолт (limit=None) — прежний контракт байт-в-байт, без пагинационных полей.
    default = bsl["find_functional_options"]("ПриобретениеТоваров")
    assert set(default.keys()) == {"object", "xml_options", "code_options"}
    xt, ct = len(default["xml_options"]), len(default["code_options"])
    assert xt == 1 and ct == 1, (xt, ct)  # оба бакета непусты → тест различит per-bucket от global

    # limit=1 → по 1 из КАЖДОГО бакета (per-bucket), НЕ 1 суммарно (global-cap дал бы returned=1).
    limited = bsl["find_functional_options"]("ПриобретениеТоваров", limit=1)
    assert set(limited.keys()) == {"object", "xml_options", "code_options", "total", "returned", "has_more"}
    assert len(limited["xml_options"]) == 1
    assert len(limited["code_options"]) == 1
    assert limited["returned"] == 2  # global-cap дал бы 1
    assert limited["total"] == 2
    assert limited["has_more"] is False  # каждый бакет ровно в пределах limit


def test_find_functional_options_limit_truncates_and_flags_has_more(tmp_path):
    """#6: при limit=0 оба бакета усечены до пустых, total сохранён, has_more=True."""
    tmpdir = str(tmp_path)
    doc_dir = os.path.join(tmpdir, "Documents", "ПриобретениеТоваров", "Ext")
    os.makedirs(doc_dir)
    with open(os.path.join(doc_dir, "ObjectModule.bsl"), "w", encoding="utf-8") as f:
        f.write('Процедура П() Экспорт\n    ПолучитьФункциональнуюОпцию("ОпцияКод");\nКонецПроцедуры\n')
    os.makedirs(os.path.join(tmpdir, "FunctionalOptions"))
    with open(os.path.join(tmpdir, "FunctionalOptions", "ИспользоватьСерии.xml"), "w", encoding="utf-8") as f:
        f.write(FUNCTIONAL_OPTION_CF_XML)
    with open(os.path.join(tmpdir, "Configuration.xml"), "w") as f:
        f.write("<Configuration/>")
    helpers, resolve_safe = make_helpers(tmpdir)
    format_info = detect_format(tmpdir)
    bsl = make_bsl_helpers(
        base_path=tmpdir,
        resolve_safe=resolve_safe,
        read_file_fn=helpers["read_file"],
        grep_fn=helpers["grep"],
        glob_files_fn=helpers["glob_files"],
        format_info=format_info,
    )
    limited = bsl["find_functional_options"]("ПриобретениеТоваров", limit=0)
    assert limited["xml_options"] == [] and limited["code_options"] == []
    assert limited["returned"] == 0
    assert limited["total"] == 2  # полный счёт сохранён
    assert limited["has_more"] is True


def test_find_functional_options_total_covers_all_matching_modules(tmp_path):
    """Code total is exhaustive, not bounded by safe_grep=20 or find_module=50."""
    tmpdir = str(tmp_path)
    for i in range(55):
        module_dir = os.path.join(tmpdir, "CommonModules", f"ПриобретениеТоваров{i:02d}", "Ext")
        os.makedirs(module_dir)
        with open(os.path.join(module_dir, "Module.bsl"), "w", encoding="utf-8") as f:
            f.write(
                "Процедура ПроверитьОпцию() Экспорт\n"
                f'    ПолучитьФункциональнуюОпцию("Опция{i:02d}");\n'
                "КонецПроцедуры\n"
            )
    with open(os.path.join(tmpdir, "Configuration.xml"), "w") as f:
        f.write("<Configuration/>")

    helpers, resolve_safe = make_helpers(tmpdir)
    bsl = make_bsl_helpers(
        base_path=tmpdir,
        resolve_safe=resolve_safe,
        read_file_fn=helpers["read_file"],
        grep_fn=helpers["grep"],
        glob_files_fn=helpers["glob_files"],
        format_info=detect_format(tmpdir),
    )

    default = bsl["find_functional_options"]("ПриобретениеТоваров")
    assert len(default["code_options"]) == 55
    limited = bsl["find_functional_options"]("ПриобретениеТоваров", limit=1)
    assert limited["total"] == 55
    assert limited["returned"] == 1
    assert limited["has_more"] is True


def test_find_functional_options_sees_module_newer_than_sqlite_snapshot(tmp_path):
    """The code scan uses the session's current immutable tree, not SQLite's file list."""
    tmpdir = str(tmp_path)
    old_dir = os.path.join(tmpdir, "CommonModules", "СтарыйМодуль", "Ext")
    os.makedirs(old_dir)
    with open(os.path.join(old_dir, "Module.bsl"), "w", encoding="utf-8") as f:
        f.write("Процедура Пустая() Экспорт\nКонецПроцедуры\n")
    with open(os.path.join(tmpdir, "Configuration.xml"), "w") as f:
        f.write("<Configuration/>")

    from rlm_tools_bsl.bsl_index import IndexBuilder, IndexReader

    db = IndexBuilder().build(tmpdir, build_calls=False, build_metadata=True)
    reader = IndexReader(str(db))
    fresh_name = "ПриобретениеТоваровНоваяЛогика"
    fresh_dir = os.path.join(tmpdir, "CommonModules", fresh_name, "Ext")
    os.makedirs(fresh_dir)
    with open(os.path.join(fresh_dir, "Module.bsl"), "w", encoding="utf-8") as f:
        f.write(
            "Процедура ПроверитьОпцию() Экспорт\n"
            '    получитьФУНКЦИОНАЛЬНУЮопцию("ОпцияПослеИндекса");\n'
            "КонецПроцедуры\n"
        )

    # Production passes this same stale reader to generic glob_files too.  The BSL
    # live catalog must bypass both SQLite-backed module-list surfaces.
    helpers, resolve_safe = make_helpers(tmpdir, idx_reader=reader)
    bsl = make_bsl_helpers(
        base_path=tmpdir,
        resolve_safe=resolve_safe,
        read_file_fn=helpers["read_file"],
        grep_fn=helpers["grep"],
        glob_files_fn=helpers["glob_files"],
        format_info=detect_format(tmpdir),
        idx_reader=reader,
    )
    try:
        assert not any(row["object_name"] == fresh_name for row in reader.get_all_modules())
        result = bsl["find_functional_options"]("ПриобретениеТоваров")
        assert [row["option_name"] for row in result["code_options"]] == ["ОпцияПослеИндекса"]
    finally:
        reader.close()


def test_find_roles():
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_full_fixture(tmpdir)
        # Add Role/Rights fixture file
        role_dir = os.path.join(tmpdir, "Roles", "Менеджер", "Ext")
        os.makedirs(role_dir)
        with open(os.path.join(role_dir, "Rights.xml"), "w", encoding="utf-8") as f:
            f.write(RIGHTS_XML)
        result = bsl["find_roles"]("ПриобретениеТоваров")
        assert result["object"] == "ПриобретениеТоваров"
        assert len(result["roles"]) >= 1
        role = result["roles"][0]
        assert role["role_name"] == "Менеджер"
        assert "Read" in role["rights"]
        assert "file" in role
        # Fix 5: fallback must include "object" in each role item
        assert "object" in role
        assert role["object"] == "ПриобретениеТоваров"


def test_find_roles_not_found():
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_full_fixture(tmpdir)
        result = bsl["find_roles"]("НесуществующийОбъект")
        assert len(result["roles"]) == 0


def test_parse_cf_event_subscription():
    result = parse_event_subscription_xml(EVENT_SUB_CF_XML)
    assert result is not None
    assert result["name"] == "ЗаписатьВерсиюДокумента"
    assert result["synonym"] == "Записать версию документа"
    assert result["event"] == "BeforeWrite"
    assert result["handler"] == "CommonModule.ВерсионированиеОбъектовСобытия.ЗаписатьВерсиюДокумента"
    assert len(result["source_types"]) == 2
    assert "DocumentObject.АвансовыйОтчет" in result["source_types"]
    assert "DocumentObject.ЗаказКлиента" in result["source_types"]


def test_parse_mdo_event_subscription():
    result = parse_event_subscription_xml(EVENT_SUB_MDO_XML)
    assert result is not None
    assert result["name"] == "тст_ЗаписатьВерсиюДокумента"
    assert result["synonym"] == "Записать версию документа"
    assert result["event"] == "BeforeWrite"
    assert len(result["source_types"]) == 2
    assert "DocumentObject.АвансовыйОтчет" in result["source_types"]


def test_parse_cf_scheduled_job():
    result = parse_scheduled_job_xml(SCHEDULED_JOB_CF_XML)
    assert result is not None
    assert result["name"] == "ЗагрузкаКурсовВалют"
    assert result["synonym"] == "Загрузка курсов валют"
    assert result["method_name"] == "CommonModule.РаботаСКурсамиВалют.ЗагрузитьАктуальныйКурс"
    assert result["use"] is False
    assert result["predefined"] is True
    assert result["restart_on_failure"]["count"] == 10
    assert result["restart_on_failure"]["interval"] == 600


def test_parse_mdo_scheduled_job():
    result = parse_scheduled_job_xml(SCHEDULED_JOB_MDO_XML)
    assert result is not None
    assert result["name"] == "ext_ОтправкаПодтверждения"
    assert result["synonym"] == "Отправка подтверждения поставки"
    assert result["method_name"] == "CommonModule.ext_РегламентныеЗадания.ОтправкаПодтверждения"
    assert result["use"] is True  # EDT default
    assert result["predefined"] is True
    assert result["restart_on_failure"]["count"] == 3


# === Integration tests for new helpers ===

BSL_DOC_WITH_MOVEMENTS = """\
Процедура ОбработкаПроведения(Отказ) Экспорт
    Движения.ТоварыНаСкладах.Записать = Истина;
    Движения.ТоварыНаСкладах.Очистить();
    Движения.РасчетыСПоставщиками.Записать = Истина;
КонецПроцедуры
"""

BSL_DOC_OBJECT_FULL = """\
Процедура ОбработкаЗаполнения(ДанныеЗаполнения) Экспорт
    Если ТипЗнч(ДанныеЗаполнения) = Тип("ДокументСсылка.ЗаказПоставщику") Тогда
        ЗаполнитьНаОсновании(ДанныеЗаполнения);
    ИначеЕсли ТипЗнч(ДанныеЗаполнения) = Тип("СправочникСсылка.ДоговорыКонтрагентов") Тогда
        ЗаполнитьПоДоговору(ДанныеЗаполнения);
    КонецЕсли;
КонецПроцедуры

Процедура ОбработкаПроведения(Отказ) Экспорт
    Движения.ТоварыНаСкладах.Записать = Истина;
    Движения.ТоварыНаСкладах.Очистить();
    Движения.РасчетыСПоставщиками.Записать = Истина;
КонецПроцедуры
"""

BSL_DOC_MANAGER = """\
Процедура ДобавитьКомандыСозданияНаОсновании(КомандыСозданияНаОсновании, Параметры) Экспорт
    Документы.ВозвратТоваров.ДобавитьКомандуСоздатьНаОсновании(КомандыСозданияНаОсновании);
    Документы.СписаниеТоваров.ДобавитьКомандуСоздатьНаОсновании(КомандыСозданияНаОсновании);
КонецПроцедуры

Процедура ДобавитьКомандыПечати(КомандыПечати) Экспорт
    УправлениеПечатью.ДобавитьКомандуПечати(КомандыПечати, "Накладная", НСтр("ru = 'Товарная накладная'"));
    УправлениеПечатью.ДобавитьКомандуПечати(КомандыПечати, "СчетНаОплату", НСтр("ru = 'Счет на оплату'"));
КонецПроцедуры
"""

BSL_DOC_ERP_MANAGER = """\
Процедура ЗарегистрироватьУчетныеМеханизмы(МеханизмыДокумента) Экспорт
    МеханизмыДокумента.Добавить("Взаиморасчеты");
    МеханизмыДокумента.Добавить("Продажи");
    МеханизмыДокумента.Добавить("СебестоимостьИПартионныйУчет");
КонецПроцедуры

Функция АдаптированныйТекстЗапросаДвиженийПоРегистру(ИмяРегистра) Экспорт
    Если ИмяРегистра = "ЗаказыКлиентов" Тогда
        Возврат "";
    ИначеЕсли ИмяРегистра = "РеестрДокументов" Тогда
        Возврат "";
    КонецЕсли;
КонецФункции

Функция ТекстЗапросаТаблицаТовары() Экспорт
    Возврат "";
КонецФункции

Функция ТекстЗапросаТаблицаВидыЗапасов() Экспорт
    Возврат "";
КонецФункции
"""

BSL_DOC_ERP_OBJECT = """\
Процедура ОбработкаПроведения(Отказ, РежимПроведения)
    ПроведениеДокументов.ОбработкаПроведенияДокумента(ЭтотОбъект, Отказ);
КонецПроцедуры
"""


def _make_full_fixture(tmpdir):
    """Create fixture with event subscriptions, scheduled jobs, and document with movements."""
    # BSL modules
    mod_dir = os.path.join(tmpdir, "CommonModules", "МойМодуль", "Ext")
    os.makedirs(mod_dir)
    with open(os.path.join(mod_dir, "Module.bsl"), "w", encoding="utf-8") as f:
        f.write(BSL_CODE)

    # Document with register movements + ОбработкаЗаполнения
    doc_dir = os.path.join(tmpdir, "Documents", "ПриобретениеТоваров", "Ext")
    os.makedirs(doc_dir)
    with open(os.path.join(doc_dir, "ObjectModule.bsl"), "w", encoding="utf-8") as f:
        f.write(BSL_DOC_OBJECT_FULL)

    # Add ManagerModule
    with open(os.path.join(doc_dir, "ManagerModule.bsl"), "w", encoding="utf-8") as f:
        f.write(BSL_DOC_MANAGER)

    # EventSubscription
    sub_dir = os.path.join(tmpdir, "EventSubscriptions")
    os.makedirs(sub_dir)
    with open(os.path.join(sub_dir, "ЗаписатьВерсию.xml"), "w", encoding="utf-8") as f:
        f.write(EVENT_SUB_CF_XML)

    # ScheduledJob
    job_dir = os.path.join(tmpdir, "ScheduledJobs")
    os.makedirs(job_dir)
    with open(os.path.join(job_dir, "ЗагрузкаКурсов.xml"), "w", encoding="utf-8") as f:
        f.write(SCHEDULED_JOB_CF_XML)

    # Configuration.xml
    with open(os.path.join(tmpdir, "Configuration.xml"), "w") as f:
        f.write("<Configuration/>")

    helpers, resolve_safe = make_helpers(tmpdir)
    format_info = detect_format(tmpdir)
    bsl = make_bsl_helpers(
        base_path=tmpdir,
        resolve_safe=resolve_safe,
        read_file_fn=helpers["read_file"],
        grep_fn=helpers["grep"],
        glob_files_fn=helpers["glob_files"],
        format_info=format_info,
    )
    return bsl, helpers


def test_find_event_subscriptions_all():
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_full_fixture(tmpdir)
        result = bsl["find_event_subscriptions"]()
        assert len(result) >= 1
        sub = result[0]
        assert sub["name"] == "ЗаписатьВерсиюДокумента"
        assert sub["event"] == "BeforeWrite"
        assert sub["handler_module"] == "ВерсионированиеОбъектовСобытия"
        assert sub["handler_procedure"] == "ЗаписатьВерсиюДокумента"
        # Without filter, source_types should be excluded
        assert "source_types" not in sub


def test_find_event_subscriptions_filtered():
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_full_fixture(tmpdir)
        result = bsl["find_event_subscriptions"]("АвансовыйОтчет")
        assert len(result) >= 1
        # With filter, source_types should be included
        assert "source_types" in result[0]


def test_find_event_subscriptions_no_match():
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_full_fixture(tmpdir)
        result = bsl["find_event_subscriptions"]("НесуществующийДокумент")
        assert len(result) == 0


def test_find_scheduled_jobs_all():
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_full_fixture(tmpdir)
        result = bsl["find_scheduled_jobs"]()
        assert len(result) >= 1
        job = result[0]
        assert job["name"] == "ЗагрузкаКурсовВалют"
        assert job["handler_module"] == "РаботаСКурсамиВалют"
        assert job["handler_procedure"] == "ЗагрузитьАктуальныйКурс"
        assert job["use"] is False


def test_find_scheduled_jobs_filtered():
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_full_fixture(tmpdir)
        result = bsl["find_scheduled_jobs"]("Курс")
        assert len(result) >= 1
        assert result[0]["name"] == "ЗагрузкаКурсовВалют"


def test_find_scheduled_jobs_no_match():
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_full_fixture(tmpdir)
        result = bsl["find_scheduled_jobs"]("НесуществующееЗадание")
        assert len(result) == 0


def test_find_register_movements():
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_full_fixture(tmpdir)
        result = bsl["find_register_movements"]("ПриобретениеТоваров")
        assert result["document"] == "ПриобретениеТоваров"
        assert len(result["code_registers"]) == 2
        reg_names = [r["name"] for r in result["code_registers"]]
        assert "ТоварыНаСкладах" in reg_names
        assert "РасчетыСПоставщиками" in reg_names
        # ТоварыНаСкладах appears on 2 lines
        товары = next(r for r in result["code_registers"] if r["name"] == "ТоварыНаСкладах")
        assert len(товары["lines"]) == 2


def test_find_register_movements_not_found():
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_full_fixture(tmpdir)
        result = bsl["find_register_movements"]("НесуществующийДок")
        assert "error" in result


def test_find_register_writers():
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_full_fixture(tmpdir)
        result = bsl["find_register_writers"]("ТоварыНаСкладах")
        assert result["register"] == "ТоварыНаСкладах"
        assert result["total_writers"] >= 1
        writers = result["writers"]
        assert any(w["document"] == "ПриобретениеТоваров" for w in writers)


def test_find_register_writers_no_match():
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_full_fixture(tmpdir)
        result = bsl["find_register_writers"]("НесуществующийРегистр")
        assert result["total_writers"] == 0


def test_analyze_document_flow():
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_full_fixture(tmpdir)
        result = bsl["analyze_document_flow"]("ПриобретениеТоваров")
        assert "metadata" in result
        assert "event_subscriptions" in result
        assert "register_movements" in result
        assert "related_scheduled_jobs" in result
        # Should find register movements
        regs = result["register_movements"].get("code_registers", [])
        assert len(regs) >= 1


def test_help_subscriptions():
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_full_fixture(tmpdir)
        text = bsl["help"]("подписки")
        assert "find_event_subscriptions" in text


def test_help_movements():
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_full_fixture(tmpdir)
        text = bsl["help"]("движения")
        assert "find_register_movements" in text


def test_help_jobs():
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_full_fixture(tmpdir)
        text = bsl["help"]("регламентные задания")
        assert "find_scheduled_jobs" in text


def test_help_flow():
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_full_fixture(tmpdir)
        text = bsl["help"]("как работает документ")
        assert "analyze_document_flow" in text


# === Task 5: find_based_on_documents ===


def test_find_based_on_documents():
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_full_fixture(tmpdir)
        result = bsl["find_based_on_documents"]("ПриобретениеТоваров")
        assert len(result["can_create_from_here"]) >= 2
        names = [d["document"] for d in result["can_create_from_here"]]
        assert "ВозвратТоваров" in names
        assert "СписаниеТоваров" in names
        assert len(result["can_be_created_from"]) >= 1
        types = [d["type"] for d in result["can_be_created_from"]]
        assert "ДокументСсылка.ЗаказПоставщику" in types


@pytest.mark.parametrize("with_index", [False, True], ids=["fs_glob", "index_backed_glob"])
def test_find_based_on_documents_exact_name_excludes_prefix_homonym(with_index):
    """Точное имя документа не смешивает direct-связи префиксного соседа и не
    позволяет чужому direct-hit отключить правильный back_scan."""
    with tempfile.TemporaryDirectory() as tmpdir:
        prefix_dir = os.path.join(tmpdir, "Documents", "ЗаказКлиента", "Ext")
        related_dir = os.path.join(tmpdir, "Documents", "Реализация", "Ext")
        for path in (prefix_dir, related_dir):
            os.makedirs(path)

        # Точный документ существует только как metadata: отсутствие собственных BSL-модулей
        # не должно превращать его имя в fragment-запрос по соседним документам.
        with open(os.path.join(tmpdir, "Documents", "Заказ.xml"), "w", encoding="utf-8") as f:
            f.write(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">\n'
                '  <Document uuid="u-order"><Properties><Name>Заказ</Name></Properties></Document>\n'
                "</MetaDataObject>\n"
            )
        with open(os.path.join(prefix_dir, "ManagerModule.bsl"), "w", encoding="utf-8") as f:
            f.write(
                "Процедура ДобавитьКомандыСозданияНаОсновании(Команды)\n"
                "    Документы.ЧужойАкт.ДобавитьКомандуСозданияНаОсновании(Команды);\n"
                "КонецПроцедуры\n"
            )
        with open(os.path.join(related_dir, "ObjectModule.bsl"), "w", encoding="utf-8") as f:
            f.write(
                "Процедура ОбработкаЗаполнения(ДанныеЗаполнения, СтандартнаяОбработка)\n"
                '    Если ТипЗнч(ДанныеЗаполнения) = Тип("ДокументСсылка.Заказ") Тогда\n'
                "        Возврат;\n"
                "    КонецЕсли;\n"
                "КонецПроцедуры\n"
            )
        with open(os.path.join(tmpdir, "Configuration.xml"), "w", encoding="utf-8") as f:
            f.write("<Configuration/>")

        reader = None
        if with_index:
            from rlm_tools_bsl.bsl_index import IndexBuilder, IndexReader

            db_path = IndexBuilder().build(tmpdir, build_calls=False, build_metadata=True)
            reader = IndexReader(str(db_path))
        helpers, resolve_safe = make_helpers(tmpdir, idx_reader=reader)
        bsl = make_bsl_helpers(
            base_path=tmpdir,
            resolve_safe=resolve_safe,
            read_file_fn=helpers["read_file"],
            grep_fn=helpers["grep"],
            glob_files_fn=helpers["glob_files"],
            format_info=detect_format(tmpdir),
            idx_reader=reader,
        )
        try:
            result = bsl["find_based_on_documents"]("Заказ")
            assert "ЧужойАкт" not in {row["document"] for row in result["can_create_from_here"]}
            assert [(row["document"], row.get("via")) for row in result["can_create_from_here"]] == [
                ("Реализация", "back_scan")
            ]

            fragment_result = bsl["find_based_on_documents"]("ЗаказКл")
            assert "ЧужойАкт" in {row["document"] for row in fragment_result["can_create_from_here"]}, fragment_result
        finally:
            if reader is not None:
                reader.close()


def test_find_based_on_documents_no_manager():
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_full_fixture(tmpdir)
        result = bsl["find_based_on_documents"]("НесуществующийДок")
        assert len(result["can_create_from_here"]) == 0
        assert len(result["can_be_created_from"]) == 0


# --- #3 (v1.28.0): union с декларативным <BasedOn> из metadata_references ---


def _make_based_on_index_fixture(tmpdir, *, with_zadacha=False):
    """Document ВходящееПисьмо (без ДобавитьКомандыСозданияНаОсновании → direct пуст) +
    опц. Document Задача c ОбработкаЗаполнения, ссылающейся на ДокументСсылка.ВходящееПисьмо
    (back_scan) + индекс с metadata. Возвращает (bsl, reader, db_path) — db_path для
    прямого seed metadata_references (декларативные <BasedOn> проще засеять, чем XML)."""
    vp = os.path.join(tmpdir, "Documents", "ВходящееПисьмо", "Ext")
    os.makedirs(vp)
    with open(os.path.join(vp, "ObjectModule.bsl"), "w", encoding="utf-8") as f:
        f.write("Процедура ПередЗаписью(Отказ)\nКонецПроцедуры\n")
    if with_zadacha:
        zp = os.path.join(tmpdir, "Documents", "Задача", "Ext")
        os.makedirs(zp)
        with open(os.path.join(zp, "ObjectModule.bsl"), "w", encoding="utf-8") as f:
            f.write(
                "Процедура ОбработкаЗаполнения(ДанныеЗаполнения, СтандартнаяОбработка)\n"
                '    Если ТипЗнч(ДанныеЗаполнения) = Тип("ДокументСсылка.ВходящееПисьмо") Тогда\n'
                "        Возврат;\n"
                "    КонецЕсли;\n"
                "КонецПроцедуры\n"
            )
    with open(os.path.join(tmpdir, "Configuration.xml"), "w") as f:
        f.write("<Configuration/>")
    helpers, resolve_safe = make_helpers(tmpdir)
    format_info = detect_format(tmpdir)
    from rlm_tools_bsl.bsl_index import IndexBuilder, IndexReader

    db_path = IndexBuilder().build(tmpdir, build_calls=False, build_metadata=True)
    reader = IndexReader(str(db_path))
    bsl = make_bsl_helpers(
        base_path=tmpdir,
        resolve_safe=resolve_safe,
        read_file_fn=helpers["read_file"],
        grep_fn=helpers["grep"],
        glob_files_fn=helpers["glob_files"],
        format_info=format_info,
        idx_reader=reader,
    )
    return bsl, reader, db_path


def _seed_based_on(db_path, rows):
    """rows: list of (source_object, source_category, ref_object) — декларативные
    <BasedOn>: source может создаваться НА ОСНОВАНИИ ref_object."""
    conn = sqlite3.connect(str(db_path))
    conn.executemany(
        "INSERT INTO metadata_references (source_object, source_category, ref_object, "
        "ref_kind, used_in, path, line) VALUES (?, ?, ?, 'based_on', 'BasedOn', ?, NULL)",
        [(so, sc, ro, f"{sc}/{so}/Ext/{so}.xml") for so, sc, ro in rows],
    )
    conn.commit()
    conn.close()


def test_find_based_on_documents_catalog_via_metadata():
    """#3: Catalog-основание (декларативный <BasedOn>, только в metadata_references)
    попадает в can_create_from_here с category и via='metadata'. FS-скан Documents/*
    его не видит (сканит только Documents)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader, db_path = _make_based_on_index_fixture(tmpdir)
        try:
            _seed_based_on(db_path, [("ЗаявкаНаОплату", "Catalogs", "Document.ВходящееПисьмо")])
            result = bsl["find_based_on_documents"]("ВходящееПисьмо")
            hits = {(d["document"], d.get("category"), d.get("via")) for d in result["can_create_from_here"]}
            assert ("ЗаявкаНаОплату", "Catalogs", "metadata") in hits, result["can_create_from_here"]
            help_text = bsl["help"]("ввод на основании")
            assert "ref = d.get('ref') or d['document']" in help_text, help_text
            assert "cat + '.'" not in help_text, help_text
        finally:
            reader.close()


def test_find_based_on_documents_backscan_preserved_with_metadata():
    """#3: metadata-union АДДИТИВЕН и идёт ПОСЛЕ back_scan — back_scan-хит (Задача через
    ОбработкаЗаполнения) сохраняется РЯДОМ с metadata-хитом (Catalog). Ловит регресс
    порядка (metadata раньше back_scan сделала бы can_create_from_here непустым и
    back_scan бы не отработал)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader, db_path = _make_based_on_index_fixture(tmpdir, with_zadacha=True)
        try:
            _seed_based_on(db_path, [("ЗаявкаНаОплату", "Catalogs", "Document.ВходящееПисьмо")])
            result = bsl["find_based_on_documents"]("ВходящееПисьмо")
            by_name = {d["document"]: d for d in result["can_create_from_here"]}
            assert "Задача" in by_name and by_name["Задача"].get("via") == "back_scan", result["can_create_from_here"]
            assert "ЗаявкаНаОплату" in by_name and by_name["ЗаявкаНаОплату"].get("via") == "metadata"
        finally:
            reader.close()


def test_find_based_on_documents_homonym_not_collapsed():
    """#3: Document.X и Catalog.X (омонимы-основания) НЕ схлопываются — дедуп по
    (source_category, source_object), не по голому имени."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader, db_path = _make_based_on_index_fixture(tmpdir)
        try:
            _seed_based_on(
                db_path,
                [
                    ("Дубль", "Documents", "Document.ВходящееПисьмо"),
                    ("Дубль", "Catalogs", "Document.ВходящееПисьмо"),
                ],
            )
            result = bsl["find_based_on_documents"]("ВходящееПисьмо")
            cats = {d.get("category") for d in result["can_create_from_here"] if d["document"] == "Дубль"}
            assert cats == {"Documents", "Catalogs"}, result["can_create_from_here"]
        finally:
            reader.close()


def _make_homonym_based_on_fixture(tmpdir):
    """Document Контрагент (ОДНОИМЁННЫЙ с Catalog Контрагент) с полным document-specific
    следом: ManagerModule.ДобавитьКомандыСозданияНаОсновании → ЗаказКлиента (direct),
    ObjectModule.ОбработкаЗаполнения → Тип("ДокументСсылка.Основание") (can_be_created_from),
    плюс Documents/Задача с обратной ссылкой ДокументСсылка.Контрагент (back_scan)."""
    kp = os.path.join(tmpdir, "Documents", "Контрагент", "Ext")
    os.makedirs(kp)
    with open(os.path.join(kp, "ManagerModule.bsl"), "w", encoding="utf-8") as f:
        f.write(
            "Процедура ДобавитьКомандыСозданияНаОсновании(КомандыСоздания)\n"
            "    Документы.ЗаказКлиента.ДобавитьКомандуСозданияНаОсновании(КомандыСоздания);\n"
            "КонецПроцедуры\n"
        )
    with open(os.path.join(kp, "ObjectModule.bsl"), "w", encoding="utf-8") as f:
        f.write(
            "Процедура ОбработкаЗаполнения(ДанныеЗаполнения, СтандартнаяОбработка)\n"
            '    Если ТипЗнч(ДанныеЗаполнения) = Тип("ДокументСсылка.Основание") Тогда\n'
            "        Возврат;\n"
            "    КонецЕсли;\n"
            "КонецПроцедуры\n"
        )
    zp = os.path.join(tmpdir, "Documents", "Задача", "Ext")
    os.makedirs(zp)
    with open(os.path.join(zp, "ObjectModule.bsl"), "w", encoding="utf-8") as f:
        f.write(
            "Процедура ОбработкаЗаполнения(ДанныеЗаполнения, СтандартнаяОбработка)\n"
            '    Если ТипЗнч(ДанныеЗаполнения) = Тип("ДокументСсылка.Контрагент") Тогда\n'
            "        Возврат;\n"
            "    КонецЕсли;\n"
            "КонецПроцедуры\n"
        )
    with open(os.path.join(tmpdir, "Configuration.xml"), "w") as f:
        f.write("<Configuration/>")
    helpers, resolve_safe = make_helpers(tmpdir)
    format_info = detect_format(tmpdir)
    from rlm_tools_bsl.bsl_index import IndexBuilder, IndexReader

    db_path = IndexBuilder().build(tmpdir, build_calls=False, build_metadata=True)
    reader = IndexReader(str(db_path))
    bsl = make_bsl_helpers(
        base_path=tmpdir,
        resolve_safe=resolve_safe,
        read_file_fn=helpers["read_file"],
        grep_fn=helpers["grep"],
        glob_files_fn=helpers["glob_files"],
        format_info=format_info,
        idx_reader=reader,
    )
    return bsl, reader, db_path


def test_find_based_on_documents_typed_catalog_input_skips_document_scan():
    """#3 (code-review P1): явный вход `Справочник.X` при ОДНОИМЁННОМ `Document.X` НЕ
    подмешивает следы документа. Категория определяется ДО FS-обхода; document-specific
    ветки (find_by_type('Documents'), ManagerModule/ObjectModule самого документа,
    back_scan по `ДокументСсылка.X`) работают только для bare/Document.*-входов.
    Иначе именно тот сценарий омонимов, ради которого делалась category-aware
    канонизация, возвращает ложные связи документа."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader, db_path = _make_homonym_based_on_fixture(tmpdir)
        try:
            _seed_based_on(db_path, [("ЗаявкаКлиента", "Documents", "Catalog.Контрагент")])
            result = bsl["find_based_on_documents"]("Справочник.Контрагент")
            hits = {(d["document"], d.get("via")) for d in result["can_create_from_here"]}
            assert hits == {("ЗаявкаКлиента", "metadata")}, result["can_create_from_here"]
            # ЗаказКлиента — direct-скан ManagerModule ДОКУМЕНТА-омонима; Задача — back_scan
            # по ДокументСсылка.Контрагент. Ни того, ни другого для входа-справочника быть не должно.
            assert result["can_be_created_from"] == [], result["can_be_created_from"]
        finally:
            reader.close()


def test_find_based_on_documents_document_input_unaffected_by_homonym_gate():
    """#3 (code-review P1): гейт по категории НЕ ломает документо-центричный дефолт —
    bare-имя (и явный `Документ.X`) по-прежнему проходят полный FS-обход."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader, db_path = _make_homonym_based_on_fixture(tmpdir)
        try:
            _seed_based_on(db_path, [("ЗаявкаКлиента", "Documents", "Catalog.Контрагент")])
            for inp in (
                "Контрагент",
                "Документ.Контрагент",
                "Document.Контрагент",
                "document.Контрагент",
                "DOCUMENT.Контрагент",
                "documentref.Контрагент",
            ):
                result = bsl["find_based_on_documents"](inp)
                hits = {(d["document"], d.get("via")) for d in result["can_create_from_here"]}
                # direct-скан ManagerModule документа отработал (via отсутствует → None)
                assert ("ЗаказКлиента", None) in hits, (inp, result["can_create_from_here"])
                # …и Catalog-основание (ref=Catalog.Контрагент) НЕ подмешалось
                assert "ЗаявкаКлиента" not in {d["document"] for d in result["can_create_from_here"]}
                assert any(d["type"] == "ДокументСсылка.Основание" for d in result["can_be_created_from"]), result[
                    "can_be_created_from"
                ]
        finally:
            reader.close()


def test_find_based_on_documents_runtime_ru_prefixes_are_document_input():
    """#3 (code-review P2): русские RUNTIME-формы документа (`ДокументСсылка.X`,
    `ДокументОбъект.X`) и регистронезависимый `документ.X` — это документо-вход.

    `_strip_meta_prefix` их исторически принимал (полный обход), поэтому category-гейт
    ОБЯЗАН канонизировать их в `Document.X`, иначе он молча выключает direct/back_scan
    для ранее рабочих аргументов. `документ.X` дополнительно проверяет, что короткое имя
    берётся из canonical suffix, а не из регистрозависимого `_strip_meta_prefix`
    (иначе в FS-поиск ушло бы `документ.Контрагент` целиком)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader, db_path = _make_homonym_based_on_fixture(tmpdir)
        try:
            _seed_based_on(db_path, [("ЗаявкаКлиента", "Documents", "Catalog.Контрагент")])
            for inp in ("ДокументСсылка.Контрагент", "ДокументОбъект.Контрагент", "документ.Контрагент"):
                result = bsl["find_based_on_documents"](inp)
                assert result["document"] == "Контрагент", (inp, result["document"])
                docs = {d["document"] for d in result["can_create_from_here"]}
                assert "ЗаказКлиента" in docs, (inp, result["can_create_from_here"])  # direct-скан отработал
                assert "ЗаявкаКлиента" not in docs, inp  # Catalog-основание не подмешалось
                assert any(d["type"] == "ДокументСсылка.Основание" for d in result["can_be_created_from"]), inp
        finally:
            reader.close()


def test_find_based_on_documents_runtime_ru_catalog_prefix_is_not_document():
    """#3 (code-review P2): зеркально — `СправочникСсылка.X`/`СправочникОбъект.X`
    канонизируются в `Catalog.X` и НЕ тянут за собой обход документа-омонима."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader, db_path = _make_homonym_based_on_fixture(tmpdir)
        try:
            _seed_based_on(db_path, [("ЗаявкаКлиента", "Documents", "Catalog.Контрагент")])
            for inp in ("СправочникСсылка.Контрагент", "СправочникОбъект.Контрагент"):
                result = bsl["find_based_on_documents"](inp)
                hits = {(d["document"], d.get("via")) for d in result["can_create_from_here"]}
                assert hits == {("ЗаявкаКлиента", "metadata")}, (inp, result["can_create_from_here"])
                assert result["can_be_created_from"] == [], inp
        finally:
            reader.close()


def test_find_based_on_documents_metadata_ref_canonical():
    """#3 (code-review): metadata-запись несёт CANONICAL `ref` для ЛЮБОЙ категории-
    источника, а не только Documents/Catalogs. Tasks/BusinessProcesses тоже поддерживают
    Ввод на основании → ref = Task.X / BusinessProcess.X (singular canonical), НЕ folder-
    форма Tasks.X / BusinessProcesses.X (иначе ref нельзя скормить canonical-ref хелперам)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader, db_path = _make_based_on_index_fixture(tmpdir)
        try:
            _seed_based_on(
                db_path,
                [
                    ("ЗаявкаНаОплату", "Catalogs", "Document.ВходящееПисьмо"),
                    ("СозданиеЗадачи", "Tasks", "Document.ВходящееПисьмо"),
                    ("Согласование", "BusinessProcesses", "Document.ВходящееПисьмо"),
                ],
            )
            result = bsl["find_based_on_documents"]("ВходящееПисьмо")
            refs = {d["document"]: d.get("ref") for d in result["can_create_from_here"] if d.get("via") == "metadata"}
            assert refs["ЗаявкаНаОплату"] == "Catalog.ЗаявкаНаОплату"
            assert refs["СозданиеЗадачи"] == "Task.СозданиеЗадачи"  # НЕ "Tasks.СозданиеЗадачи"
            assert refs["Согласование"] == "BusinessProcess.Согласование"  # НЕ "BusinessProcesses.Согласование"
        finally:
            reader.close()


# === Task 6: find_print_forms ===


def test_find_print_forms():
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_full_fixture(tmpdir)
        result = bsl["find_print_forms"]("ПриобретениеТоваров")
        assert len(result["print_forms"]) >= 2
        names = [p["name"] for p in result["print_forms"]]
        assert "Накладная" in names
        assert "СчетНаОплату" in names
        # Check presentation
        nakl = next(p for p in result["print_forms"] if p["name"] == "Накладная")
        assert nakl["presentation"] == "Товарная накладная"


def test_find_print_forms_not_found():
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_full_fixture(tmpdir)
        result = bsl["find_print_forms"]("НесуществующийДок")
        assert len(result["print_forms"]) == 0


# === Task 7: find_register_movements ERP framework fallback ===


def test_find_register_movements_erp_framework():
    with tempfile.TemporaryDirectory() as tmpdir:
        doc_dir = os.path.join(tmpdir, "Documents", "РеализацияТоваров", "Ext")
        os.makedirs(doc_dir)
        with open(os.path.join(doc_dir, "ObjectModule.bsl"), "w", encoding="utf-8") as f:
            f.write(BSL_DOC_ERP_OBJECT)
        with open(os.path.join(doc_dir, "ManagerModule.bsl"), "w", encoding="utf-8") as f:
            f.write(BSL_DOC_ERP_MANAGER)
        with open(os.path.join(tmpdir, "Configuration.xml"), "w") as f:
            f.write("<Configuration/>")

        from rlm_tools_bsl.helpers import make_helpers
        from rlm_tools_bsl.format_detector import detect_format

        helpers, resolve_safe = make_helpers(tmpdir)
        format_info = detect_format(tmpdir)
        bsl = make_bsl_helpers(
            base_path=tmpdir,
            resolve_safe=resolve_safe,
            read_file_fn=helpers["read_file"],
            grep_fn=helpers["grep"],
            glob_files_fn=helpers["glob_files"],
            format_info=format_info,
        )

        result = bsl["find_register_movements"]("РеализацияТоваров")
        assert len(result["code_registers"]) == 0  # No direct Движения.X
        assert len(result["erp_mechanisms"]) == 3
        assert "Взаиморасчеты" in result["erp_mechanisms"]
        assert "Продажи" in result["erp_mechanisms"]
        assert len(result["manager_tables"]) >= 2
        assert "Товары" in result["manager_tables"]
        assert "ВидыЗапасов" in result["manager_tables"]
        assert "ЗаказыКлиентов" in result["adapted_registers"]
        assert "РеестрДокументов" in result["adapted_registers"]


# === Task 8: help recipes for new helpers ===


def test_help_based_on():
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_full_fixture(tmpdir)
        text = bsl["help"]("ввод на основании")
        assert "find_based_on_documents" in text


def test_help_print_forms():
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_full_fixture(tmpdir)
        text = bsl["help"]("печатные формы")
        assert "find_print_forms" in text


def test_help_enum():
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_full_fixture(tmpdir)
        text = bsl["help"]("значения перечисления")
        assert "find_enum_values" in text


def test_help_roles():
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_full_fixture(tmpdir)
        text = bsl["help"]("права доступа")
        assert "find_roles" in text


def test_help_functional_options():
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_full_fixture(tmpdir)
        text = bsl["help"]("functional options")
        assert "find_functional_options" in text


# === Auto-strip metadata type prefix ===


def test_strip_meta_prefix_find_module(bsl_env):
    # With prefix
    r1 = bsl_env.bsl["find_module"]("Документ.МойМодуль")
    # Without prefix
    r2 = bsl_env.bsl["find_module"]("МойМодуль")
    assert len(r1) == len(r2)
    assert r1[0]["object_name"] == r2[0]["object_name"]


def test_find_module_optional_filters(bsl_env):
    """v1.19.0 tolerant contract: find_module accepts optional module_type/category
    filters (instead of raising on the kwarg) and applies them case-insensitively."""
    fm = bsl_env.bsl["find_module"]
    all_mods = fm("МойМодуль")
    assert all_mods, "fixture must yield at least one module"
    mt = all_mods[0]["module_type"]
    cat = all_mods[0]["category"]

    # Filter by the actual module_type → every row matches it.
    filtered = fm("МойМодуль", module_type=mt)
    assert filtered
    assert all(m["module_type"] == mt for m in filtered)
    # Case-insensitive.
    assert len(fm("МойМодуль", module_type=mt.upper())) == len(filtered)
    # Nonexistent type → empty (no error).
    assert fm("МойМодуль", module_type="НесуществующийТип") == []

    # Category filter likewise.
    by_cat = fm("МойМодуль", category=cat)
    assert by_cat and all(m["category"] == cat for m in by_cat)
    assert fm("МойМодуль", category="НетТакойКатегории") == []

    # Filter-only call WITHOUT a positional name must NOT raise (Codex finding):
    # find_module(module_type=...) is the exact agent guess. name is optional.
    no_name = fm(module_type=mt)
    assert no_name and all(m["module_type"] == mt for m in no_name)
    assert fm(category=cat) and all(m["category"] == cat for m in fm(category=cat))
    # No args at all → browse (bounded), never a TypeError.
    assert isinstance(fm(), list)


def test_strip_meta_prefix_find_register_movements():
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_full_fixture(tmpdir)
        r1 = bsl["find_register_movements"]("Документ.ПриобретениеТоваров")
        r2 = bsl["find_register_movements"]("ПриобретениеТоваров")
        assert r1["document"] == r2["document"]
        assert len(r1["code_registers"]) == len(r2["code_registers"])


def test_strip_meta_prefix_find_enum_values():
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_bsl_fixture(tmpdir)
        # Create enum fixture
        enum_dir = os.path.join(tmpdir, "Enums", "СтатусыЗаказов")
        os.makedirs(enum_dir)
        with open(os.path.join(enum_dir, "СтатусыЗаказов.xml"), "w", encoding="utf-8") as f:
            f.write(ENUM_CF_XML)
        r1 = bsl["find_enum_values"]("Перечисление.СтатусыЗаказов")
        r2 = bsl["find_enum_values"]("СтатусыЗаказов")
        assert r1["name"] == r2["name"]


# === source_count=0 subscriptions (catch-all) ===

EVENT_SUB_CATCHALL_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"
    xmlns:v8="http://v8.1c.ru/8.1/data/core"
    xmlns:cfg="http://v8.1c.ru/8.1/data/enterprise/current-config">
<EventSubscription uuid="ca1f402d-0000-0000-0000-000000000002">
  <Properties>
    <Name>ктнПередЗаписьюДокумента</Name>
    <Synonym>
      <v8:item><v8:lang>ru</v8:lang><v8:content>Перед записью документа</v8:content></v8:item>
    </Synonym>
    <Source/>
    <Event>BeforeWrite</Event>
    <Handler>CommonModule.ктнПроведение.ПередЗаписьюДокумента</Handler>
  </Properties>
</EventSubscription>
</MetaDataObject>
"""


def test_find_event_subscriptions_catchall():
    """Subscriptions with source_count=0 should match any object name filter."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create base fixture
        mod_dir = os.path.join(tmpdir, "CommonModules", "МойМодуль", "Ext")
        os.makedirs(mod_dir)
        with open(os.path.join(mod_dir, "Module.bsl"), "w", encoding="utf-8") as f:
            f.write(BSL_CODE)
        sub_dir = os.path.join(tmpdir, "EventSubscriptions")
        os.makedirs(sub_dir)
        # Normal subscription with specific sources
        with open(os.path.join(sub_dir, "ЗаписатьВерсию.xml"), "w", encoding="utf-8") as f:
            f.write(EVENT_SUB_CF_XML)
        # Catch-all subscription (empty Source)
        with open(os.path.join(sub_dir, "ктнПередЗаписью.xml"), "w", encoding="utf-8") as f:
            f.write(EVENT_SUB_CATCHALL_XML)
        with open(os.path.join(tmpdir, "Configuration.xml"), "w") as f:
            f.write("<Configuration/>")

        helpers, resolve_safe = make_helpers(tmpdir)
        format_info = detect_format(tmpdir)
        bsl = make_bsl_helpers(
            base_path=tmpdir,
            resolve_safe=resolve_safe,
            read_file_fn=helpers["read_file"],
            grep_fn=helpers["grep"],
            glob_files_fn=helpers["glob_files"],
            format_info=format_info,
        )

        # Filter by a specific object — should return BOTH the matching sub AND the catch-all
        result = bsl["find_event_subscriptions"]("АвансовыйОтчет")
        names = [s["name"] for s in result]
        assert "ЗаписатьВерсиюДокумента" in names  # has АвансовыйОтчет in sources
        assert "ктнПередЗаписьюДокумента" in names  # catch-all, source_count=0

        # Filter by non-existing object — should still return catch-all
        result2 = bsl["find_event_subscriptions"]("НесуществующийОбъект")
        names2 = [s["name"] for s in result2]
        assert "ктнПередЗаписьюДокумента" in names2
        assert "ЗаписатьВерсиюДокумента" not in names2


# === v1.28.0: exact-с-фолбэком + category-aware типизированный вход ===


def _make_subs_env(tmpdir, specs, *, with_index=False):
    """specs: list[(name, source_types)] → (bsl, reader|None). XML-подписки на диске.

    Форма XML — как у боевой CF-выгрузки (см. EVENT_SUB_CF_XML): типы источника лежат
    в <v8:Type>cfg:DocumentObject.X</v8:Type>. Голый <Type> (без namespace v8) парсер НЕ
    видит (`source_el.findall("v8:Type", ns)`), и source_types вышел бы ПУСТЫМ — то есть
    каждая подписка стала бы universal-catch-all, и тест проверял бы не то, что заявлено.
    """
    sub_dir = os.path.join(tmpdir, "EventSubscriptions")
    os.makedirs(sub_dir, exist_ok=True)
    for name, types in specs:
        types_xml = "".join(f"<v8:Type>cfg:{t}</v8:Type>" for t in types)
        with open(os.path.join(sub_dir, f"{name}.xml"), "w", encoding="utf-8") as f:
            f.write(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"\n'
                '    xmlns:v8="http://v8.1c.ru/8.1/data/core"\n'
                '    xmlns:cfg="http://v8.1c.ru/8.1/data/enterprise/current-config">\n'
                f'  <EventSubscription uuid="u-{name}">\n'
                f"    <Properties><Name>{name}</Name>\n"
                f"      <Source>{types_xml}</Source>\n"
                "      <Event>ПередЗаписью</Event>\n"
                "      <Handler>ОбщийМодуль.Обработчик</Handler>\n"
                "    </Properties>\n"
                "  </EventSubscription>\n"
                "</MetaDataObject>\n"
            )
    with open(os.path.join(tmpdir, "Configuration.xml"), "w") as f:
        f.write("<Configuration/>")
    helpers, resolve_safe = make_helpers(tmpdir)
    reader = None
    if with_index:
        from rlm_tools_bsl.bsl_index import IndexBuilder, IndexReader

        db = IndexBuilder().build(tmpdir, build_calls=False, build_metadata=True)
        reader = IndexReader(str(db))
    bsl = make_bsl_helpers(
        base_path=tmpdir,
        resolve_safe=resolve_safe,
        read_file_fn=helpers["read_file"],
        grep_fn=helpers["grep"],
        glob_files_fn=helpers["glob_files"],
        format_info=detect_format(tmpdir),
        idx_reader=reader,
    )
    return bsl, reader


def test_find_event_subscriptions_live_exact_with_fallback():
    """Live-ветка (без idx_reader) матчит как reader: точное совпадение по именной части,
    длинный омоним не подмешивается, universal всегда, фрагмент — через фолбэк."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_subs_env(
            tmpdir,
            [
                ("ПодпискаТочная", ["DocumentObject.РеализацияТоваровУслуг"]),
                ("ПодпискаОмоним", ["CatalogObject.РеализацияТоваровУслугПрисоединенныеФайлы"]),
                ("ПодпискаUniversal", []),
            ],
        )
        rows = bsl["find_event_subscriptions"]("РеализацияТоваровУслуг")
        names = {r["name"] for r in rows}
        assert "ПодпискаОмоним" not in names, rows
        assert names == {"ПодпискаТочная", "ПодпискаUniversal"}
        assert {r["name"]: r["scope"] for r in rows}["ПодпискаТочная"] == "exact"

        # Фрагмент 'Реализация' — подстрока ОБОИХ имён (и точного объекта, и омонима),
        # точных совпадений нет → partial-фолбэк возвращает ОБА.
        frag = bsl["find_event_subscriptions"]("Реализация")
        assert {r["name"] for r in frag} == {"ПодпискаТочная", "ПодпискаОмоним", "ПодпискаUniversal"}, frag
        assert {r["scope"] for r in frag if r["name"] != "ПодпискаUniversal"} == {"partial"}, frag


@pytest.mark.parametrize("with_index", [False, True])
def test_find_event_subscriptions_typed_input_is_category_aware(with_index):
    """Явный префикс (Документ.X) → canonical-матчинг: Catalog.X-омоним не подмешивается,
    и хелпер сходится с get_object_profile (тот всегда canonical).

    Параметризация обязательна: canonical-ветка живёт В ДВУХ местах — в reader и в
    live-фолбэке. Тест только с индексом оставил бы вторую непроверенной, и конфигурация
    без индекса ответила бы иначе."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_subs_env(
            tmpdir,
            [
                ("ПодпискаДок", ["DocumentObject.Дубль"]),
                ("ПодпискаСпр", ["CatalogObject.Дубль"]),
            ],
            with_index=with_index,
        )
        try:
            for inp in (
                "Документ.Дубль",
                "Document.Дубль",
                "document.Дубль",
                "DOCUMENT.Дубль",
                "documentobject.Дубль",
            ):
                typed = bsl["find_event_subscriptions"](inp)
                assert [r["name"] for r in typed] == ["ПодпискаДок"], (inp, typed)
            bare = bsl["find_event_subscriptions"]("Дубль")
            assert {r["name"] for r in bare} == {"ПодпискаДок", "ПодпискаСпр"}  # category-blind
        finally:
            if reader:
                reader.close()


# === custom_only parameter ===


def test_find_event_subscriptions_custom_only():
    """custom_only=True should filter by auto-detected prefixes."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create fixture with both standard and custom subscriptions
        mod_dir = os.path.join(tmpdir, "CommonModules", "ктнМодуль", "Ext")
        os.makedirs(mod_dir)
        with open(os.path.join(mod_dir, "Module.bsl"), "w", encoding="utf-8") as f:
            f.write(BSL_CODE)
        # Need 3+ objects with "ктн" prefix for auto-detect threshold
        for name in ["ктнМодуль2", "ктнМодуль3"]:
            d = os.path.join(tmpdir, "CommonModules", name, "Ext")
            os.makedirs(d)
            with open(os.path.join(d, "Module.bsl"), "w", encoding="utf-8") as f:
                f.write("// stub\n")

        sub_dir = os.path.join(tmpdir, "EventSubscriptions")
        os.makedirs(sub_dir)
        with open(os.path.join(sub_dir, "ЗаписатьВерсию.xml"), "w", encoding="utf-8") as f:
            f.write(EVENT_SUB_CF_XML)
        with open(os.path.join(sub_dir, "ктнПередЗаписью.xml"), "w", encoding="utf-8") as f:
            f.write(EVENT_SUB_CATCHALL_XML)
        with open(os.path.join(tmpdir, "Configuration.xml"), "w") as f:
            f.write("<Configuration/>")

        helpers, resolve_safe = make_helpers(tmpdir)
        format_info = detect_format(tmpdir)
        bsl = make_bsl_helpers(
            base_path=tmpdir,
            resolve_safe=resolve_safe,
            read_file_fn=helpers["read_file"],
            grep_fn=helpers["grep"],
            glob_files_fn=helpers["glob_files"],
            format_info=format_info,
        )

        # Without custom_only — should return both
        all_subs = bsl["find_event_subscriptions"]()
        assert len(all_subs) == 2

        # With custom_only — should return only "ктн" prefixed
        custom_subs = bsl["find_event_subscriptions"]("", custom_only=True)
        assert len(custom_subs) == 1
        assert custom_subs[0]["name"] == "ктнПередЗаписьюДокумента"


# ── extract_queries tests ─────────────────────────────────────


def test_extract_queries_basic():
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_bsl_fixture(tmpdir)
        mod_dir = os.path.join(tmpdir, "Documents", "ТестовыйДокумент", "Ext")
        os.makedirs(mod_dir, exist_ok=True)
        bsl_path = os.path.join(mod_dir, "ObjectModule.bsl")
        with open(bsl_path, "w", encoding="utf-8-sig") as f:
            f.write(
                "Процедура ОбработкаПроведения(Отказ)\n"
                "    Запрос = Новый Запрос;\n"
                '    Запрос.Текст = "ВЫБРАТЬ\n'
                "    |    Т.Ссылка\n"
                "    |ИЗ\n"
                "    |    РегистрНакопления.ТоварыНаСкладах КАК Т\n"
                "    |    СОЕДИНЕНИЕ Справочник.Номенклатура КАК Н\n"
                '    |    ПО Т.Номенклатура = Н.Ссылка";\n'
                "КонецПроцедуры\n"
            )
        rel_path = os.path.relpath(bsl_path, tmpdir).replace("\\", "/")
        queries = bsl["extract_queries"](rel_path)
        assert len(queries) >= 1
        q = queries[0]
        assert q["procedure"] == "ОбработкаПроведения"
        assert "РегистрНакопления.ТоварыНаСкладах" in q["tables"]
        assert "Справочник.Номенклатура" in q["tables"]
        assert "text_preview" in q


def test_extract_queries_no_queries():
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_bsl_fixture(tmpdir)
        modules = bsl["find_module"]("МойМодуль")
        assert modules
        queries = bsl["extract_queries"](modules[0]["path"])
        assert queries == []


# ── code_metrics tests ────────────────────────────────────────


def test_code_metrics_basic():
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_bsl_fixture(tmpdir)
        mod_dir = os.path.join(tmpdir, "CommonModules", "МетрикиТест", "Ext")
        os.makedirs(mod_dir, exist_ok=True)
        bsl_path = os.path.join(mod_dir, "Module.bsl")
        with open(bsl_path, "w", encoding="utf-8-sig") as f:
            f.write(
                "// Комментарий\n"
                "\n"
                "Процедура Тест1() Экспорт\n"
                "    Если Истина Тогда\n"
                "        Для Каждого Элемент Из Список Цикл\n"
                "            Сообщить(Элемент);\n"
                "        КонецЦикла;\n"
                "    КонецЕсли;\n"
                "КонецПроцедуры\n"
                "\n"
                "Функция Тест2()\n"
                "    Возврат 1;\n"
                "КонецФункции\n"
            )
        rel_path = os.path.relpath(bsl_path, tmpdir).replace("\\", "/")
        m = bsl["code_metrics"](rel_path)
        assert m["total_lines"] == 13
        assert m["comment_lines"] == 1
        assert m["empty_lines"] == 2
        assert m["code_lines"] == 10
        assert m["procedures_count"] == 2
        assert m["exports_count"] == 1
        assert m["max_nesting"] == 2  # Если + Для
        assert m["avg_proc_size"] > 0


# ---------------------------------------------------------------------------
# find_callers_context: idx_zero_callers_authoritative tests
# ---------------------------------------------------------------------------


def _make_bsl_fixture_authoritative(tmpdir, authoritative=False):
    """Create fixture with idx_zero_callers_authoritative param."""
    _create_cf_fixture(tmpdir)
    helpers, resolve_safe = make_helpers(tmpdir)
    format_info = detect_format(tmpdir)
    bsl = make_bsl_helpers(
        base_path=tmpdir,
        resolve_safe=resolve_safe,
        read_file_fn=helpers["read_file"],
        grep_fn=helpers["grep"],
        glob_files_fn=helpers["glob_files"],
        format_info=format_info,
        idx_zero_callers_authoritative=authoritative,
    )
    return bsl, helpers


def test_authoritative_true_index_hit():
    """authoritative=True + index returns callers -> result without fallback."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_bsl_fixture_authoritative(tmpdir, authoritative=True)
        # Replace idx_reader in the closure — not possible directly,
        # so we test via the FS path: ЗаполнитьДанные has callers
        result = bsl["find_callers_context"]("ЗаполнитьДанные")
        assert len(result["callers"]) > 0
        assert "fallback_skipped" not in result["_meta"]


def test_authoritative_true_zero_callers():
    """authoritative=True + index returns 0 callers -> fallback skipped."""
    from unittest.mock import MagicMock
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        _create_cf_fixture(tmpdir)
        helpers, resolve_safe = make_helpers(tmpdir)
        format_info = detect_format(tmpdir)

        # Create a mock idx_reader that returns 0 callers
        mock_idx = MagicMock()
        mock_idx.has_calls = True
        mock_idx.get_callers.return_value = {
            "callers": [],
            "_meta": {"total_callers": 0, "returned": 0, "offset": 0, "has_more": False},
        }
        mock_idx.get_all_modules.return_value = []
        mock_idx.get_methods_by_path.return_value = None

        bsl = make_bsl_helpers(
            base_path=tmpdir,
            resolve_safe=resolve_safe,
            read_file_fn=helpers["read_file"],
            grep_fn=helpers["grep"],
            glob_files_fn=helpers["glob_files"],
            format_info=format_info,
            idx_reader=mock_idx,
            idx_zero_callers_authoritative=True,
        )

        result = bsl["find_callers_context"]("НесуществующаяФункция")
        assert result["_meta"]["fallback_skipped"] is True
        assert "hint" in result["_meta"]
        assert "safe_grep" in result["_meta"]["hint"]
        assert len(result["callers"]) == 0


def test_authoritative_false_zero_callers_fallback():
    """authoritative=False + index returns 0 callers -> fallback performed."""
    from unittest.mock import MagicMock
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        _create_cf_fixture(tmpdir)
        helpers, resolve_safe = make_helpers(tmpdir)
        format_info = detect_format(tmpdir)

        mock_idx = MagicMock()
        mock_idx.has_calls = True
        mock_idx.get_callers.return_value = {
            "callers": [],
            "_meta": {"total_callers": 0, "returned": 0, "offset": 0, "has_more": False},
        }
        mock_idx.get_all_modules.return_value = []
        mock_idx.get_methods_by_path.return_value = None

        bsl = make_bsl_helpers(
            base_path=tmpdir,
            resolve_safe=resolve_safe,
            read_file_fn=helpers["read_file"],
            grep_fn=helpers["grep"],
            glob_files_fn=helpers["glob_files"],
            format_info=format_info,
            idx_reader=mock_idx,
            idx_zero_callers_authoritative=False,  # not authoritative
        )

        result = bsl["find_callers_context"]("ЗаполнитьДанные")
        # Fallback should have been performed (no fallback_skipped flag)
        assert "fallback_skipped" not in result["_meta"]


def test_authoritative_parity_known_call():
    """Parity test: known qualified call returns same result from both paths."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        # Create fixture once, build two helpers with different authoritative flag
        _create_cf_fixture(tmpdir)
        helpers, resolve_safe = make_helpers(tmpdir)
        format_info = detect_format(tmpdir)

        bsl_fs = make_bsl_helpers(
            base_path=tmpdir,
            resolve_safe=resolve_safe,
            read_file_fn=helpers["read_file"],
            grep_fn=helpers["grep"],
            glob_files_fn=helpers["glob_files"],
            format_info=format_info,
            idx_zero_callers_authoritative=False,
        )
        bsl_auth = make_bsl_helpers(
            base_path=tmpdir,
            resolve_safe=resolve_safe,
            read_file_fn=helpers["read_file"],
            grep_fn=helpers["grep"],
            glob_files_fn=helpers["glob_files"],
            format_info=format_info,
            idx_zero_callers_authoritative=True,
        )

        result_fs = bsl_fs["find_callers_context"]("ЗаполнитьДанные")
        result_auth = bsl_auth["find_callers_context"]("ЗаполнитьДанные")

        # Both should find the same callers (FS path for both, no idx_reader)
        assert len(result_fs["callers"]) == len(result_auth["callers"])
        fs_callers = {c["caller_name"] for c in result_fs["callers"]}
        auth_callers = {c["caller_name"] for c in result_auth["callers"]}
        assert fs_callers == auth_callers


# --- Fix 2: find_xdto_packages fallback without Package.xdto ---


_XDTO_EDT_MDO = """\
<?xml version="1.0" encoding="UTF-8"?>
<mdclass:XDTOPackage xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass"
                     name="TestPackage">
  <name>TestPackage</name>
  <namespace>http://example.com/test</namespace>
</mdclass:XDTOPackage>
"""


def test_find_xdto_packages_no_package_xdto():
    """find_xdto_packages must not crash when .mdo exists without Package.xdto."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_full_fixture(tmpdir)
        # Create EDT-style XDTO package without Package.xdto
        xdto_dir = os.path.join(tmpdir, "XDTOPackages", "TestPackage")
        os.makedirs(xdto_dir)
        with open(os.path.join(xdto_dir, "TestPackage.mdo"), "w", encoding="utf-8") as f:
            f.write(_XDTO_EDT_MDO)
        # No Package.xdto — must not crash
        result = bsl["find_xdto_packages"]("")
        # Should return the package without types
        assert len(result) >= 1
        pkg = next(p for p in result if p["name"] == "TestPackage")
        assert pkg["namespace"] == "http://example.com/test"
        assert "types" not in pkg or pkg.get("types") == []


# ──────────────────────────────────────────────────────────────────────────
# Tests for v1.7.0: object_attributes, predefined_items, normalize_type_string
# ──────────────────────────────────────────────────────────────────────────

CF_DOC_WITH_TS_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"
    xmlns:v8="http://v8.1c.ru/8.1/data/core">
  <Document>
    <Properties>
      <Name>ДокСТЧ</Name>
      <Synonym><v8:item><v8:lang>ru</v8:lang><v8:content>Док с ТЧ</v8:content></v8:item></Synonym>
    </Properties>
    <ChildObjects>
      <TabularSection>
        <Properties>
          <Name>Товары</Name>
          <Synonym><v8:item><v8:lang>ru</v8:lang><v8:content>Товары</v8:content></v8:item></Synonym>
        </Properties>
        <ChildObjects>
          <Attribute>
            <Properties>
              <Name>Номенклатура</Name>
              <Synonym><v8:item><v8:lang>ru</v8:lang><v8:content>Номенклатура</v8:content></v8:item></Synonym>
              <Type><v8:Type xmlns:d4p1="http://v8.1c.ru/8.1/data/enterprise/current-config">d4p1:CatalogRef.Номенклатура</v8:Type></Type>
            </Properties>
          </Attribute>
          <Attribute>
            <Properties>
              <Name>Количество</Name>
              <Synonym><v8:item><v8:lang>ru</v8:lang><v8:content>Количество</v8:content></v8:item></Synonym>
              <Type><v8:Type>xs:decimal</v8:Type></Type>
            </Properties>
          </Attribute>
        </ChildObjects>
      </TabularSection>
    </ChildObjects>
  </Document>
</MetaDataObject>
"""


class TestCfTabularSectionAttributes:
    def test_ts_attributes_parsed(self):
        result = parse_metadata_xml(CF_DOC_WITH_TS_XML)
        assert "tabular_sections" in result
        ts = result["tabular_sections"][0]
        assert ts["name"] == "Товары"
        assert len(ts["attributes"]) == 2
        assert ts["attributes"][0]["name"] == "Номенклатура"
        assert ts["attributes"][1]["name"] == "Количество"


class TestNormalizeTypeString:
    def test_single_xs_string(self):
        assert normalize_type_string("xs:string") == '["String"]'

    def test_single_xs_decimal(self):
        assert normalize_type_string("xs:decimal") == '["Number"]'

    def test_single_xs_boolean(self):
        assert normalize_type_string("xs:boolean") == '["Boolean"]'

    def test_single_xs_datetime(self):
        assert normalize_type_string("xs:dateTime") == '["DateTime"]'

    def test_cfg_catalog_ref(self):
        assert normalize_type_string("cfg:CatalogRef.Организации") == '["CatalogRef.Организации"]'

    def test_d4p1_catalog_ref(self):
        assert normalize_type_string("d4p1:CatalogRef.Номенклатура") == '["CatalogRef.Номенклатура"]'

    def test_edt_no_prefix(self):
        assert normalize_type_string("CatalogRef.Номенклатура") == '["CatalogRef.Номенклатура"]'

    def test_composite_types(self):
        result = normalize_type_string("cfg:CatalogRef.X, cfg:CatalogRef.Y")
        assert result == '["CatalogRef.X", "CatalogRef.Y"]'

    def test_empty_string(self):
        assert normalize_type_string("") == "[]"

    def test_single_xs_base64binary(self):
        assert normalize_type_string("xs:base64Binary") == '["ValueStorage"]'

    def test_mixed_prefixes(self):
        result = normalize_type_string("xs:string, cfg:CatalogRef.X, d4p1:DocumentRef.Y")
        assert result == '["String", "CatalogRef.X", "DocumentRef.Y"]'


PREDEFINED_CF_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<ChartOfCharacteristicTypes xmlns="http://v8.1c.ru/8.3/MDClasses"
    xmlns:v8="http://v8.1c.ru/8.1/data/core">
<PredefinedData>
<Item id="aaa">
    <Name>РеализуемыеАктивы</Name>
    <Code>00055</Code>
    <Description>Реализуемые активы</Description>
    <Type>
        <v8:Type xmlns:d4p1="http://v8.1c.ru/8.1/data/enterprise/current-config">d4p1:CatalogRef.Номенклатура</v8:Type>
        <v8:Type xmlns:d4p1="http://v8.1c.ru/8.1/data/enterprise/current-config">d4p1:CatalogRef.Контрагенты</v8:Type>
    </Type>
    <IsFolder>false</IsFolder>
</Item>
<Item id="bbb">
    <Name>ВидыДеятельности</Name>
    <Code>00010</Code>
    <Description>Виды деятельности</Description>
    <Type>
        <v8:Type xmlns:d4p1="http://v8.1c.ru/8.1/data/enterprise/current-config">d4p1:CatalogRef.ВидыДеятельности</v8:Type>
    </Type>
    <IsFolder>true</IsFolder>
</Item>
</PredefinedData>
</ChartOfCharacteristicTypes>
"""

PREDEFINED_EDT_MDO = """\
<?xml version="1.0" encoding="UTF-8"?>
<mdclass:ChartOfCharacteristicTypes xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass">
  <name>ВидыСубконтоХозрасчетные</name>
  <predefined>
    <items id="aaa">
      <name>РеализуемыеАктивы</name>
      <description>Реализуемые активы</description>
      <code>00055</code>
      <type>
        <types>CatalogRef.Номенклатура</types>
        <types>CatalogRef.Контрагенты</types>
      </type>
    </items>
    <items id="bbb">
      <name>ВидыДеятельности</name>
      <description>Виды деятельности</description>
      <code>00010</code>
      <type>
        <types>CatalogRef.ВидыДеятельности</types>
      </type>
      <isFolder>true</isFolder>
    </items>
  </predefined>
</mdclass:ChartOfCharacteristicTypes>
"""


class TestParsePredefinedItems:
    def test_cf_format(self):
        result = parse_predefined_items(PREDEFINED_CF_XML)
        assert result is not None
        assert len(result) == 2
        r0 = result[0]
        assert r0["name"] == "РеализуемыеАктивы"
        assert r0["synonym"] == "Реализуемые активы"
        assert r0["code"] == "00055"
        assert "CatalogRef.Номенклатура" in r0["types"]
        assert "CatalogRef.Контрагенты" in r0["types"]
        assert r0["is_folder"] is False
        r1 = result[1]
        assert r1["name"] == "ВидыДеятельности"
        assert r1["is_folder"] is True

    def test_edt_format(self):
        result = parse_predefined_items(PREDEFINED_EDT_MDO)
        assert result is not None
        assert len(result) == 2
        r0 = result[0]
        assert r0["name"] == "РеализуемыеАктивы"
        assert r0["synonym"] == "Реализуемые активы"
        assert r0["code"] == "00055"
        assert "CatalogRef.Номенклатура" in r0["types"]
        assert "CatalogRef.Контрагенты" in r0["types"]
        assert r0["is_folder"] is False

    def test_empty_xml(self):
        result = parse_predefined_items("<root/>")
        assert result is None or result == []

    def test_invalid_xml(self):
        result = parse_predefined_items("not xml")
        assert result is None


# ──────────────────────────────────────────────────────────────────────────
# Tests for IndexReader, helpers, and search integration (v1.7.0)
# ──────────────────────────────────────────────────────────────────────────

ATTR_DOC_CF_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"
    xmlns:v8="http://v8.1c.ru/8.1/data/core">
  <Document>
    <Properties>
      <Name>ТестДок</Name>
      <Synonym><v8:item><v8:lang>ru</v8:lang><v8:content>Тест документ</v8:content></v8:item></Synonym>
    </Properties>
    <ChildObjects>
      <Attribute>
        <Properties>
          <Name>Организация</Name>
          <Synonym><v8:item><v8:lang>ru</v8:lang><v8:content>Организация</v8:content></v8:item></Synonym>
          <Type><v8:Type xmlns:d4p1="http://v8.1c.ru/8.1/data/enterprise/current-config">d4p1:CatalogRef.Организации</v8:Type></Type>
        </Properties>
      </Attribute>
      <Attribute>
        <Properties>
          <Name>Сумма</Name>
          <Synonym><v8:item><v8:lang>ru</v8:lang><v8:content>Сумма</v8:content></v8:item></Synonym>
          <Type><v8:Type>xs:decimal</v8:Type></Type>
        </Properties>
      </Attribute>
    </ChildObjects>
  </Document>
</MetaDataObject>
"""


def _make_indexed_fixture(tmpdir):
    """Create fixture with indexed attributes + predefined items."""
    from rlm_tools_bsl.bsl_index import IndexBuilder, IndexReader

    # BSL module (required for index build)
    mod_dir = os.path.join(tmpdir, "CommonModules", "МойМодуль", "Ext")
    os.makedirs(mod_dir)
    with open(os.path.join(mod_dir, "Module.bsl"), "w", encoding="utf-8") as f:
        f.write(BSL_CODE)

    # Document with attributes
    doc_dir = os.path.join(tmpdir, "Documents", "ТестДок", "Ext")
    os.makedirs(doc_dir)
    with open(os.path.join(doc_dir, "Document.xml"), "w", encoding="utf-8") as f:
        f.write(ATTR_DOC_CF_XML)
    with open(os.path.join(doc_dir, "ObjectModule.bsl"), "w", encoding="utf-8") as f:
        f.write("// пусто")

    # Predefined items
    pvh_dir = os.path.join(tmpdir, "ChartsOfCharacteristicTypes", "ВидыСубконто", "Ext")
    os.makedirs(pvh_dir)
    with open(os.path.join(pvh_dir, "Predefined.xml"), "w", encoding="utf-8") as f:
        f.write(PREDEFINED_CF_XML)
    # Need a metadata XML too for attribute scanning
    with open(os.path.join(pvh_dir, "ChartOfCharacteristicTypes.xml"), "w", encoding="utf-8") as f:
        f.write("""\
<?xml version="1.0" encoding="UTF-8"?>
<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"
    xmlns:v8="http://v8.1c.ru/8.1/data/core">
  <ChartOfCharacteristicTypes>
    <Properties>
      <Name>ВидыСубконто</Name>
      <Synonym><v8:item><v8:lang>ru</v8:lang><v8:content>Виды субконто</v8:content></v8:item></Synonym>
    </Properties>
  </ChartOfCharacteristicTypes>
</MetaDataObject>
""")

    with open(os.path.join(tmpdir, "Configuration.xml"), "w") as f:
        f.write("<Configuration/>")

    # Build index
    builder = IndexBuilder()
    db_path = builder.build(tmpdir, build_calls=False, build_metadata=True)

    reader = IndexReader(str(db_path))
    helpers, resolve_safe = make_helpers(tmpdir)
    format_info = detect_format(tmpdir)
    bsl = make_bsl_helpers(
        base_path=tmpdir,
        resolve_safe=resolve_safe,
        read_file_fn=helpers["read_file"],
        grep_fn=helpers["grep"],
        glob_files_fn=helpers["glob_files"],
        format_info=format_info,
        idx_reader=reader,
    )
    return bsl, reader


class TestIndexReaderObjectAttributes:
    def test_get_object_attributes_by_name(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bsl, reader = _make_indexed_fixture(tmpdir)
            results = reader.get_object_attributes(attr_name="Организация")
            assert results is not None
            assert len(results) >= 1
            assert any(r["attr_name"] == "Организация" for r in results)
            assert all(isinstance(r["attr_type"], list) for r in results)
            reader.close()

    def test_get_object_attributes_by_object(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bsl, reader = _make_indexed_fixture(tmpdir)
            results = reader.get_object_attributes(object_name="ТестДок")
            assert results is not None
            assert len(results) == 2  # Организация + Сумма
            reader.close()

    def test_get_object_attributes_by_kind(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bsl, reader = _make_indexed_fixture(tmpdir)
            results = reader.get_object_attributes(kind="attribute")
            assert results is not None
            assert len(results) >= 2
            assert all(r["attr_kind"] == "attribute" for r in results)
            reader.close()

    def test_get_object_attributes_kind_case_insensitive(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bsl, reader = _make_indexed_fixture(tmpdir)
            results = reader.get_object_attributes(kind="Attribute")
            assert results is not None
            assert len(results) >= 2
            reader.close()

    def test_get_object_attributes_not_found(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bsl, reader = _make_indexed_fixture(tmpdir)
            results = reader.get_object_attributes(attr_name="НесуществующийРеквизит")
            assert results is not None
            assert results == []
            reader.close()


class TestIndexReaderPredefinedItems:
    def test_get_predefined_items_by_name(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bsl, reader = _make_indexed_fixture(tmpdir)
            results = reader.get_predefined_items(item_name="РеализуемыеАктивы")
            assert results is not None
            assert len(results) >= 1
            r0 = results[0]
            assert r0["item_name"] == "РеализуемыеАктивы"
            assert isinstance(r0["types"], list)
            assert "CatalogRef.Номенклатура" in r0["types"]
            reader.close()

    def test_get_predefined_items_by_object(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bsl, reader = _make_indexed_fixture(tmpdir)
            results = reader.get_predefined_items(object_name="ВидыСубконто")
            assert results is not None
            assert len(results) == 2  # РеализуемыеАктивы + ВидыДеятельности
            reader.close()

    def test_get_predefined_items_not_found(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bsl, reader = _make_indexed_fixture(tmpdir)
            results = reader.get_predefined_items(item_name="НесуществующийЭлемент")
            assert results is not None
            assert results == []
            reader.close()


class TestFindAttributesHelper:
    def test_find_attributes_by_name(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bsl, reader = _make_indexed_fixture(tmpdir)
            results = bsl["find_attributes"](name="Организация")
            assert isinstance(results, list)
            assert len(results) >= 1
            assert any(r["attr_name"] == "Организация" for r in results)
            reader.close()

    def test_find_attributes_by_object(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bsl, reader = _make_indexed_fixture(tmpdir)
            results = bsl["find_attributes"](object_name="ТестДок")
            assert isinstance(results, list)
            assert len(results) == 2
            reader.close()

    def test_find_attributes_not_found(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bsl, reader = _make_indexed_fixture(tmpdir)
            results = bsl["find_attributes"](name="НесуществующийРеквизит")
            assert results == []
            reader.close()


class TestFindPredefinedHelper:
    def test_find_predefined_by_name(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bsl, reader = _make_indexed_fixture(tmpdir)
            results = bsl["find_predefined"](name="РеализуемыеАктивы")
            assert isinstance(results, list)
            assert len(results) >= 1
            assert results[0]["item_name"] == "РеализуемыеАктивы"
            reader.close()

    def test_find_predefined_not_found(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bsl, reader = _make_indexed_fixture(tmpdir)
            results = bsl["find_predefined"](name="НесуществующийЭлемент")
            assert results == []
            reader.close()


class TestSearchNewScopes:
    def test_search_scope_attributes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bsl, reader = _make_indexed_fixture(tmpdir)
            results = bsl["search"]("Организация", scope="attributes")
            assert isinstance(results, list)
            assert len(results) >= 1
            assert all(r["source_type"] == "attribute" for r in results)
            reader.close()

    def test_search_scope_predefined(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bsl, reader = _make_indexed_fixture(tmpdir)
            results = bsl["search"]("Реализуемые", scope="predefined")
            assert isinstance(results, list)
            assert len(results) >= 1
            assert all(r["source_type"] == "predefined" for r in results)
            reader.close()

    def test_search_all_includes_new_types(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bsl, reader = _make_indexed_fixture(tmpdir)
            results = bsl["search"]("Организация", scope="all")
            source_types = {r["source_type"] for r in results}
            assert "attribute" in source_types
            reader.close()


class TestGetIndexInfoNewFields:
    def test_has_new_capability_flags(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bsl, reader = _make_indexed_fixture(tmpdir)
            info = bsl["get_index_info"]()
            assert info["status"] == "ok"
            assert info["has_object_attributes"] is True
            assert info["has_predefined_items"] is True
            assert info["object_attributes_count"] >= 2
            assert info["predefined_items_count"] >= 2
            reader.close()

    def test_post_read_marker_recheck(self, monkeypatch):
        """get_index_info: a rebuild that sets build_in_progress=1 AFTER the pre-read
        check but DURING get_statistics must NOT be reported as status:'ok'. In that
        window stale meta is not yet cleared (built_at/builder_version still present), so
        stats_indicate_load_failure stays False — only a post-read index_incomplete
        recheck catches it, mirroring rlm_start (codex High follow-up)."""
        import sqlite3

        from rlm_tools_bsl.bsl_index import IndexReader, get_index_db_path

        with tempfile.TemporaryDirectory() as tmpdir:
            bsl, reader = _make_indexed_fixture(tmpdir)
            db_path = get_index_db_path(tmpdir)

            orig = IndexReader.get_statistics

            def spy(self):
                # The rebuild's marker becomes visible during the stats read (window opens).
                w = sqlite3.connect(str(db_path))
                w.execute("INSERT OR REPLACE INTO index_meta (key, value) VALUES ('build_in_progress', '1')")
                w.commit()
                w.close()
                return orig(self)

            monkeypatch.setattr(IndexReader, "get_statistics", spy)
            try:
                info = bsl["get_index_info"]()
            finally:
                reader.close()
            assert info["status"] == "incomplete"


class TestSearchSynonymPath:
    def test_search_predefined_by_synonym(self):
        """search(scope='predefined') finds items by synonym (Реализуемые активы)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            bsl, reader = _make_indexed_fixture(tmpdir)
            results = bsl["search"]("Реализуемые активы", scope="predefined")
            assert isinstance(results, list)
            assert len(results) >= 1
            assert any("Реализуемые" in r["text"] for r in results)
            reader.close()

    def test_search_attribute_by_synonym(self):
        """search(scope='attributes') finds by synonym when attr_name != attr_synonym."""
        with tempfile.TemporaryDirectory() as tmpdir:
            bsl, reader = _make_indexed_fixture(tmpdir)
            # Организация has same name and synonym — search by synonym
            results = bsl["search"]("Организация", scope="attributes")
            assert isinstance(results, list)
            assert len(results) >= 1
            reader.close()


class TestFallbackContract:
    def test_find_attributes_fallback_normalizes_types(self):
        """Fallback path returns normalized types matching index contract."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create fixture WITHOUT index — only XML files
            doc_dir = os.path.join(tmpdir, "Documents", "ТестДок", "Ext")
            os.makedirs(doc_dir)
            with open(os.path.join(doc_dir, "Document.xml"), "w", encoding="utf-8") as f:
                f.write(ATTR_DOC_CF_XML)

            with open(os.path.join(tmpdir, "Configuration.xml"), "w") as f:
                f.write("<Configuration/>")

            helpers, resolve_safe = make_helpers(tmpdir)
            format_info = detect_format(tmpdir)
            bsl = make_bsl_helpers(
                base_path=tmpdir,
                resolve_safe=resolve_safe,
                read_file_fn=helpers["read_file"],
                grep_fn=helpers["grep"],
                glob_files_fn=helpers["glob_files"],
                format_info=format_info,
                # No idx_reader — force fallback path
            )
            results = bsl["find_attributes"](object_name="Documents/ТестДок")
            assert len(results) == 2
            # Types must be normalized lists, not raw XML strings
            org = next(r for r in results if r["attr_name"] == "Организация")
            assert isinstance(org["attr_type"], list)
            assert "CatalogRef.Организации" in org["attr_type"]
            # Category must be filled from path
            assert org["category"] == "Documents"
            # Сумма type must be normalized
            summ = next(r for r in results if r["attr_name"] == "Сумма")
            assert "Number" in summ["attr_type"]


# ============================================================
# P1 — list-перегрузка read_procedure / find_callers_context / find_enum_values
# Каждый: первый «целевой» аргумент list → dict-by-name (изоляция ошибок
# поэлементно); str → прежний контракт байт-в-байт.
# ============================================================


def test_read_procedure_list_returns_dict_by_name(bsl_env):
    """list имён → {name: body}; модуль резолвится один раз (мемоизация)."""
    path = bsl_env.bsl["find_module"]("МойМодуль")[0]["path"]
    res = bsl_env.bsl["read_procedure"](path, ["ЗаполнитьДанные", "ПолучитьСумму"])
    assert isinstance(res, dict)
    assert set(res.keys()) == {"ЗаполнитьДанные", "ПолучитьСумму"}
    assert "ЗаполнитьДанные" in res["ЗаполнитьДанные"]
    assert "ПолучитьСумму" in res["ПолучитьСумму"]


def test_read_procedure_list_isolates_bad_name(bsl_env):
    """Один валид + один отсутствующий: dict с телом-str и None, без падения батча."""
    path = bsl_env.bsl["find_module"]("МойМодуль")[0]["path"]
    res = bsl_env.bsl["read_procedure"](path, ["ЗаполнитьДанные", "НетТакойМетод"])
    assert isinstance(res, dict)
    assert isinstance(res["ЗаполнитьДанные"], str)
    assert res["НетТакойМетод"] is None


def test_read_procedure_str_mode_unchanged(bsl_env):
    """str proc_name → str|None (НЕ dict) — прежний контракт."""
    path = bsl_env.bsl["find_module"]("МойМодуль")[0]["path"]
    body = bsl_env.bsl["read_procedure"](path, "ЗаполнитьДанные")
    assert isinstance(body, str)
    assert "ЗаполнитьДанные" in body
    none_body = bsl_env.bsl["read_procedure"](path, "НетТакойМетод")
    assert none_body is None


def test_find_callers_context_list_returns_dict_by_name(bsl_env):
    """list имён → {name: {callers, _meta}} с общим module_hint/offset/limit."""
    res = bsl_env.bsl["find_callers_context"](["ЗаполнитьДанные", "ПолучитьСумму"])
    assert isinstance(res, dict)
    assert set(res.keys()) == {"ЗаполнитьДанные", "ПолучитьСумму"}
    for name in ("ЗаполнитьДанные", "ПолучитьСумму"):
        assert "callers" in res[name]
        assert "_meta" in res[name]
    assert any(c["caller_name"] == "ОбработкаЗаполнения" for c in res["ЗаполнитьДанные"]["callers"])


def test_find_callers_context_list_isolates_no_callers(bsl_env):
    """list с именем без вызывающих → его запись — валидный пустой результат, не крэш."""
    res = bsl_env.bsl["find_callers_context"](["ЗаполнитьДанные", "ВнутренняяПроцедура"])
    assert res["ВнутренняяПроцедура"]["callers"] == []
    assert len(res["ЗаполнитьДанные"]["callers"]) >= 1


def test_find_callers_context_str_mode_unchanged(bsl_env):
    """str proc_name → {callers, _meta} (НЕ keyed by name) — прежний контракт."""
    res = bsl_env.bsl["find_callers_context"]("ЗаполнитьДанные")
    assert "callers" in res and "_meta" in res
    assert any(c["caller_name"] == "ОбработкаЗаполнения" for c in res["callers"])


def test_find_enum_values_list_returns_dict_by_name():
    """list → {name: {...}|{error}}; изоляция: валид + не найдено."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_full_fixture(tmpdir)
        enum_dir = os.path.join(tmpdir, "Enums", "СтатусыЗаказов")
        os.makedirs(enum_dir)
        with open(os.path.join(enum_dir, "СтатусыЗаказов.xml"), "w", encoding="utf-8") as f:
            f.write(ENUM_CF_XML)
        res = bsl["find_enum_values"](["СтатусыЗаказов", "НетТакогоПеречисления"])
        assert isinstance(res, dict)
        assert set(res.keys()) == {"СтатусыЗаказов", "НетТакогоПеречисления"}
        assert res["СтатусыЗаказов"]["name"] == "СтатусыЗаказов"
        assert "error" not in res["СтатусыЗаказов"]
        assert "error" in res["НетТакогоПеречисления"]


def test_find_enum_values_str_mode_unchanged():
    """str enum_name → один dict с name/values (НЕ keyed by name) — прежний контракт."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_full_fixture(tmpdir)
        enum_dir = os.path.join(tmpdir, "Enums", "СтатусыЗаказов")
        os.makedirs(enum_dir)
        with open(os.path.join(enum_dir, "СтатусыЗаказов.xml"), "w", encoding="utf-8") as f:
            f.write(ENUM_CF_XML)
        res = bsl["find_enum_values"]("СтатусыЗаказов")
        assert res["name"] == "СтатусыЗаказов"
        assert "values" in res


# ============================================================
# P2 — дешёвый агрегат get_object_modules(name, include_methods=False)
# ============================================================

_MANAGER_BSL = "Функция ПолучитьПоИдентификатору(Ид) Экспорт\n    Возврат Ид;\nКонецФункции\n"


def _make_object_modules_fixture(tmpdir, *, with_index=True):
    """Document РеализацияТоваров: ObjectModule (3 метода, 1 #Область, 2 экспорта) +
    ManagerModule (1 метод-экспорт, без области). Returns (bsl, reader|None)."""
    obj = os.path.join(tmpdir, "Documents", "РеализацияТоваров", "Ext")
    os.makedirs(obj)
    with open(os.path.join(obj, "ObjectModule.bsl"), "w", encoding="utf-8") as f:
        f.write(BSL_CODE)
    with open(os.path.join(obj, "ManagerModule.bsl"), "w", encoding="utf-8") as f:
        f.write(_MANAGER_BSL)
    with open(os.path.join(tmpdir, "Configuration.xml"), "w") as f:
        f.write("<Configuration/>")
    helpers, resolve_safe = make_helpers(tmpdir)
    format_info = detect_format(tmpdir)
    reader = None
    kw = {}
    if with_index:
        from rlm_tools_bsl.bsl_index import IndexBuilder, IndexReader

        db_path = IndexBuilder().build(tmpdir, build_calls=False, build_metadata=True)
        reader = IndexReader(str(db_path))
        kw["idx_reader"] = reader
    bsl = make_bsl_helpers(
        base_path=tmpdir,
        resolve_safe=resolve_safe,
        read_file_fn=helpers["read_file"],
        grep_fn=helpers["grep"],
        glob_files_fn=helpers["glob_files"],
        format_info=format_info,
        **kw,
    )
    return bsl, reader


def test_get_object_modules_index_path():
    """Индексный путь: идентичность объекта, все модули, per-module index_used=True, roll-up."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_object_modules_fixture(tmpdir, with_index=True)
        try:
            res = bsl["get_object_modules"]("РеализацияТоваров")
            assert "error" not in res
            assert res["object_name"] == "РеализацияТоваров"
            assert res["category"] == "Documents"
            paths = {m["path"] for m in res["modules"]}
            assert any(p.endswith("ObjectModule.bsl") for p in paths)
            assert any(p.endswith("ManagerModule.bsl") for p in paths)
            # Дешёвый индексный путь доказан per-module (без extract_procedures).
            for m in res["modules"]:
                assert m["_meta"]["index_used"] is True
            # roll-up: 3 (ObjectModule) + 1 (ManagerModule) методов, 2+1 экспортов.
            assert res["totals"]["modules"] == 2
            assert res["totals"]["methods"] == 4
            assert res["totals"]["exports"] == 3
            assert res["_meta"]["index_used"] is True
        finally:
            if reader:
                reader.close()


def test_get_object_modules_include_methods_false_vs_true():
    """include_methods=False → область без листовых методов; True → методы есть. totals одинаковы."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_object_modules_fixture(tmpdir, with_index=True)
        try:
            skel = bsl["get_object_modules"]("РеализацияТоваров", include_methods=False)
            full = bsl["get_object_modules"]("РеализацияТоваров", include_methods=True)

            def _om_outline(res):
                om = next(m for m in res["modules"] if m["path"].endswith("ObjectModule.bsl"))
                return om["outline"]

            for r in _om_outline(skel):
                assert "methods" not in r
            assert any("methods" in r and r["methods"] for r in _om_outline(full))
            assert skel["totals"]["methods"] == full["totals"]["methods"] == 4
        finally:
            if reader:
                reader.close()


def test_get_object_modules_not_found():
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_object_modules_fixture(tmpdir, with_index=True)
        try:
            res = bsl["get_object_modules"]("НетТакогоОбъекта")
            assert "error" in res
            assert "_meta" in res
        finally:
            if reader:
                reader.close()


def test_get_object_modules_override_flags():
    """Флаги перехватов на модуль через get_overrides_for_path + roll-up overrides."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_object_modules_fixture(tmpdir, with_index=True)
        try:

            def _fake_overrides(rel_path):
                if rel_path.endswith("ObjectModule.bsl"):
                    return {"ЗаполнитьДанные": [{"extension_name": "Расш", "annotation": "Вместо"}]}
                return {}

            reader.get_overrides_for_path = _fake_overrides  # instance attr shadows method
            res = bsl["get_object_modules"]("РеализацияТоваров")
            om = next(m for m in res["modules"] if m["path"].endswith("ObjectModule.bsl"))
            assert om["overrides"]["count"] == 1
            assert "ЗаполнитьДанные" in om["overrides"]["methods"]
            mm = next(m for m in res["modules"] if m["path"].endswith("ManagerModule.bsl"))
            assert mm["overrides"]["count"] == 0
            assert res["totals"]["overrides"] == 1
        finally:
            if reader:
                reader.close()


def test_get_object_modules_exact_enumeration_not_capped():
    """Имя-подстрока многих путей: прямой exact-скан собирает ВСЕ свои модули и НЕ
    обрезается капом 50 (в отличие от find_module). Live-режим."""
    with tempfile.TemporaryDirectory() as tmpdir:
        obj = os.path.join(tmpdir, "Documents", "Док", "Ext")
        os.makedirs(obj)
        with open(os.path.join(obj, "ObjectModule.bsl"), "w", encoding="utf-8") as f:
            f.write(BSL_CODE)
        with open(os.path.join(obj, "ManagerModule.bsl"), "w", encoding="utf-8") as f:
            f.write(_MANAGER_BSL)
        # 60 «декоев», object_name которых СОДЕРЖИТ «Док» подстрокой → find_module
        # их substring-матчит и упирается в кап 50.
        for i in range(60):
            d = os.path.join(tmpdir, "Documents", f"Док_Декой{i:02d}", "Ext")
            os.makedirs(d)
            with open(os.path.join(d, "ObjectModule.bsl"), "w", encoding="utf-8") as f:
                f.write("Процедура П() Экспорт\nКонецПроцедуры\n")
        with open(os.path.join(tmpdir, "Configuration.xml"), "w") as f:
            f.write("<Configuration/>")
        helpers, resolve_safe = make_helpers(tmpdir)
        format_info = detect_format(tmpdir)
        bsl = make_bsl_helpers(
            base_path=tmpdir,
            resolve_safe=resolve_safe,
            read_file_fn=helpers["read_file"],
            grep_fn=helpers["grep"],
            glob_files_fn=helpers["glob_files"],
            format_info=format_info,  # live, без индекса
        )
        # Кап реально срабатывает на частом имени-подстроке.
        assert len(bsl["find_module"]("Док")) == 50
        # get_object_modules — exact scan → ТОЛЬКО 2 собственных модуля «Док».
        res = bsl["get_object_modules"]("Док")
        assert res["object_name"] == "Док"
        assert len(res["modules"]) == 2
        assert all("/Док/" in m["path"] for m in res["modules"])  # ни одного декоя


def test_get_object_modules_does_not_parse_object_xml(monkeypatch):
    """get_object_modules НИКОГДА не зовёт parse_object_xml/parse_metadata_xml (в отличие от analyze_object)."""
    import rlm_tools_bsl.bsl_helpers as bh

    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_object_modules_fixture(tmpdir, with_index=True)
        try:
            calls = []
            real = bh.parse_metadata_xml

            def _spy(*a, **k):
                calls.append(1)
                return real(*a, **k)

            monkeypatch.setattr(bh, "parse_metadata_xml", _spy)
            res = bsl["get_object_modules"]("РеализацияТоваров")
            assert "error" not in res
            assert calls == []
        finally:
            if reader:
                reader.close()


def test_get_object_modules_stale_index_fallback_reason(monkeypatch):
    """Честный per-module live-fallback: module-row есть, methods пуст → index_used=False + fallback_reason."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_object_modules_fixture(tmpdir, with_index=True)
        try:
            real = reader.get_outline_data

            def _stale(rel_path):
                if rel_path.endswith("ManagerModule.bsl"):
                    return {
                        "module": {
                            "category": "Documents",
                            "object_name": "РеализацияТоваров",
                            "module_type": "ManagerModule",
                        },
                        "regions": [],
                        "methods": [],
                    }
                return real(rel_path)

            monkeypatch.setattr(reader, "get_outline_data", _stale)
            res = bsl["get_object_modules"]("РеализацияТоваров")
            mm = next(m for m in res["modules"] if m["path"].endswith("ManagerModule.bsl"))
            assert mm["_meta"]["index_used"] is False
            assert mm["_meta"]["fallback_reason"] == "index_empty_for_module"
            om = next(m for m in res["modules"] if m["path"].endswith("ObjectModule.bsl"))
            assert om["_meta"]["index_used"] is True
        finally:
            if reader:
                reader.close()


def test_get_module_outline_no_live_skips_stale_read(monkeypatch):
    """no_live=True: stale module → skipped marker (empty outline, skipped_live), NO live parse.
    no_live=False on the same module → live read populates the outline (proves the contrast)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_object_modules_fixture(tmpdir, with_index=True)
        try:
            real = reader.get_outline_data

            def _stale(rel_path):
                if rel_path.endswith("ManagerModule.bsl"):
                    return {
                        "module": {
                            "category": "Documents",
                            "object_name": "РеализацияТоваров",
                            "module_type": "ManagerModule",
                        },
                        "regions": [],
                        "methods": [],
                    }
                return real(rel_path)

            monkeypatch.setattr(reader, "get_outline_data", _stale)
            # Find the ManagerModule rel path.
            mods = bsl["find_module"]("РеализацияТоваров")
            mgr = next(m["path"] for m in mods if m["path"].endswith("ManagerModule.bsl"))

            skipped = bsl["get_module_outline"](mgr, no_live=True)
            assert skipped["_meta"]["skipped_live"] is True
            assert skipped["_meta"]["index_used"] is False
            assert skipped["_meta"]["fallback_reason"] == "index_empty_for_module"
            assert skipped["outline"] == []
            assert skipped["totals"]["methods"] == 0
            # Identity still filled structurally (no body read needed).
            assert skipped["object_name"] == "РеализацияТоваров"

            live = bsl["get_module_outline"](mgr, no_live=False)
            assert live["_meta"].get("skipped_live") is not True
            assert live["totals"]["methods"] == 1  # ManagerModule has 1 export — live read happened
        finally:
            if reader:
                reader.close()


def test_get_object_modules_no_live_propagates(monkeypatch):
    """get_object_modules(no_live=True): stale module marked skipped_live, top-level
    _meta.modules_skipped_live=True, and NO live parse (methods stay 0 for that module)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_object_modules_fixture(tmpdir, with_index=True)
        try:
            real = reader.get_outline_data

            def _stale(rel_path):
                if rel_path.endswith("ManagerModule.bsl"):
                    return {
                        "module": {
                            "category": "Documents",
                            "object_name": "РеализацияТоваров",
                            "module_type": "ManagerModule",
                        },
                        "regions": [],
                        "methods": [],
                    }
                return real(rel_path)

            monkeypatch.setattr(reader, "get_outline_data", _stale)
            res = bsl["get_object_modules"]("РеализацияТоваров", no_live=True)
            mm = next(m for m in res["modules"] if m["path"].endswith("ManagerModule.bsl"))
            assert mm["_meta"]["skipped_live"] is True
            assert mm["totals"]["methods"] == 0  # not read
            om = next(m for m in res["modules"] if m["path"].endswith("ObjectModule.bsl"))
            assert om["_meta"]["index_used"] is True  # healthy module unaffected
            assert res["_meta"]["modules_skipped_live"] is True
        finally:
            if reader:
                reader.close()


def test_find_functional_options_include_code_gate():
    """include_code=False → XML-only, the safe_grep code scan is skipped (code_options empty)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        mod_dir = os.path.join(tmpdir, "CommonModules", "Скидки", "Ext")
        os.makedirs(mod_dir)
        with open(os.path.join(mod_dir, "Module.bsl"), "w", encoding="utf-8") as f:
            f.write(
                "Функция ИспользуютсяСкидки() Экспорт\n"
                '    Возврат ПолучитьФункциональнуюОпцию("ИспользоватьСкидки");\n'
                "КонецФункции\n"
            )
        with open(os.path.join(tmpdir, "Configuration.xml"), "w") as f:
            f.write("<Configuration/>")
        helpers, resolve_safe = make_helpers(tmpdir)
        format_info = detect_format(tmpdir)
        bsl = make_bsl_helpers(
            base_path=tmpdir,
            resolve_safe=resolve_safe,
            read_file_fn=helpers["read_file"],
            grep_fn=helpers["grep"],
            glob_files_fn=helpers["glob_files"],
            format_info=format_info,
        )
        with_code = bsl["find_functional_options"]("Скидки", include_code=True)
        assert any(c["option_name"] == "ИспользоватьСкидки" for c in with_code["code_options"])
        xml_only = bsl["find_functional_options"]("Скидки", include_code=False)
        assert xml_only["code_options"] == []


# ============================================================
# v1.23.0 — get_object_profile (one-shot compact object aggregate)
# ============================================================

_PROFILE_STATUS_ENUM = {"ok", "empty", "error", "unavailable", "skipped"}
_PROFILE_SOURCE_ENUM = {"index", "live", "mixed", "unknown"}


def _seed_profile_meta(db_path):
    """Inject roles/subscriptions/FO/register_movements rows (+ a non-Document object)
    so the profile data sections are populated, including a *Доп prefix-collision sibling."""
    conn = sqlite3.connect(str(db_path))
    conn.executemany(
        "INSERT INTO role_rights (role_name, object_name, right_name, file) VALUES (?, ?, ?, ?)",
        [
            ("РольПродажи", "Document.РеализацияТоваров", "Read", "Roles/РольПродажи/Ext/Rights.xml"),
            ("РольПродажи", "Document.РеализацияТоваров", "Update", "Roles/РольПродажи/Ext/Rights.xml"),
            ("РольДоп", "Document.РеализацияТоваровДоп", "Read", "Roles/РольДоп/Ext/Rights.xml"),  # collision
        ],
    )
    conn.executemany(
        "INSERT INTO event_subscriptions (name, synonym, event, handler_module, "
        "handler_procedure, source_types, source_count, file) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                "ПодпискаРеализация",
                "",
                "BeforeWrite",
                "ОМ",
                "Обр",
                json.dumps(["DocumentObject.РеализацияТоваров"], ensure_ascii=False),
                1,
                "es1.xml",
            ),
            (
                "ПодпискаДоп",
                "",
                "OnWrite",
                "ОМ",
                "Обр2",
                json.dumps(["DocumentObject.РеализацияТоваровДоп"], ensure_ascii=False),
                1,
                "es2.xml",
            ),
            # #2 (v1.28.0): universal catch-all (пустой source_types) — применяется к
            # ЛЮБОМУ источнику, поэтому попадает в профиль РеализацияТоваров (scope=universal).
            (
                "ПодпискаUniversal",
                "",
                "OnWrite",
                "ОМ",
                "ОбрU",
                json.dumps([], ensure_ascii=False),
                0,
                "esU.xml",
            ),
        ],
    )
    conn.executemany(
        "INSERT INTO functional_options (name, synonym, location, content, file) VALUES (?, ?, ?, ?, ?)",
        [
            ("ФО_Реализация", "", "", json.dumps(["Document.РеализацияТоваров"], ensure_ascii=False), "fo1.xml"),
            ("ФО_Доп", "", "", json.dumps(["Document.РеализацияТоваровДоп"], ensure_ascii=False), "fo2.xml"),
        ],
    )
    conn.executemany(
        "INSERT INTO register_movements (document_name, register_name, source, file) VALUES (?, ?, ?, ?)",
        [
            ("РеализацияТоваров", "Продажи", "code", "om.bsl"),
            ("РеализацияТоваров", "Себестоимость", "code", "om.bsl"),
            ("РеализацияТоваров", "МеханизмУУ", "erp_mechanism", "mm.bsl"),
            ("РеализацияТоваров", "ТаблицаX", "manager_table", "mm.bsl"),
            ("РеализацияТоваров", "АдаптРег", "adapted", "mm.bsl"),
        ],
    )
    conn.executemany(
        "INSERT INTO object_attributes (object_name, category, attr_name, attr_synonym, "
        "attr_type, attr_kind, ts_name, source_file) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("Контрагенты", "Catalogs", "ИНН", "ИНН", json.dumps(["String"]), "attribute", None, "cat.xml"),
            ("Контрагенты", "Catalogs", "КПП", "КПП", json.dumps(["String"]), "attribute", None, "cat.xml"),
        ],
    )
    conn.commit()
    conn.close()


def _make_profile_fixture(tmpdir, *, with_index=True, glob_counter=None, read_counter=None):
    """Document РеализацияТоваров (ObjectModule+ManagerModule) + seeded metadata rows.
    Optional glob/read counters prove the no-index path triggers no glob/live."""
    obj = os.path.join(tmpdir, "Documents", "РеализацияТоваров", "Ext")
    os.makedirs(obj)
    with open(os.path.join(obj, "ObjectModule.bsl"), "w", encoding="utf-8") as f:
        f.write(BSL_CODE)
    with open(os.path.join(obj, "ManagerModule.bsl"), "w", encoding="utf-8") as f:
        f.write(_MANAGER_BSL)
    with open(os.path.join(tmpdir, "Configuration.xml"), "w") as f:
        f.write("<Configuration/>")
    helpers, resolve_safe = make_helpers(tmpdir)
    format_info = detect_format(tmpdir)
    reader = None
    kw = {}
    if with_index:
        from rlm_tools_bsl.bsl_index import IndexBuilder, IndexReader

        db_path = IndexBuilder().build(tmpdir, build_calls=False, build_metadata=True)
        _seed_profile_meta(db_path)
        reader = IndexReader(str(db_path))
        kw["idx_reader"] = reader

    read_fn = helpers["read_file"]
    glob_fn = helpers["glob_files"]
    if read_counter is not None:
        _r = read_fn

        def read_fn(p, _r=_r):  # noqa: F811
            read_counter.append(p)
            return _r(p)

    if glob_counter is not None:
        _g = glob_fn

        def glob_fn(pat, _g=_g):  # noqa: F811
            glob_counter.append(pat)
            return _g(pat)

    bsl = make_bsl_helpers(
        base_path=tmpdir,
        resolve_safe=resolve_safe,
        read_file_fn=read_fn,
        grep_fn=helpers["grep"],
        glob_files_fn=glob_fn,
        format_info=format_info,
        **kw,
    )
    return bsl, reader


def _assert_section_shape(sec):
    """Every section conforms to the unified contract."""
    assert sec["status"] in _PROFILE_STATUS_ENUM, sec["status"]
    assert isinstance(sec["summary"], dict)
    assert isinstance(sec["items"], list)
    assert isinstance(sec["total"], int)
    assert isinstance(sec["returned"], int)
    assert isinstance(sec["has_more"], bool)
    assert sec["_meta"]["source"] in _PROFILE_SOURCE_ENUM, sec["_meta"]["source"]
    assert isinstance(sec["_meta"]["elapsed_ms"], (int, float))


def test_profile_default_sections_full_shape():
    """Default profile: top-level + every section conforms; _meta tracing present."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_profile_fixture(tmpdir, with_index=True)
        try:
            p = bsl["get_object_profile"]("РеализацияТоваров")
            assert "error" not in p
            assert p["object_name"] == "РеализацияТоваров"
            assert p["category"] == "Documents"
            # default section set, no heavy flow/code_usages
            assert set(p["sections"]) == {
                "structure",
                "modules",
                "registers",
                "subscriptions",
                "roles",
                "functional_options",
            }
            for sec in p["sections"].values():
                _assert_section_shape(sec)
            # _meta tracing (R4 #1,#2)
            meta = p["_meta"]
            assert meta["identity_source"] == "index"
            assert meta["extension_visibility"] == "standalone"
            assert isinstance(meta["total_elapsed_ms"], (int, float))
            names = {s["name"] for s in meta["sections"]}
            assert names == set(p["sections"])
            for s in meta["sections"]:
                assert set(s) >= {"name", "elapsed_ms", "source", "status", "items_count", "truncated"}
        finally:
            if reader:
                reader.close()


def test_profile_exact_ref_no_collision():
    """roles/subscriptions/functional_options match РеализацияТоваров EXACTLY, never *Доп.
    Подписки: exact (ПодпискаРеализация) + universal catch-all (ПодпискаUniversal), но
    именованный *Доп-collision (ПодпискаДоп) НЕ протекает (#2, v1.28.0)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_profile_fixture(tmpdir, with_index=True)
        try:
            p = bsl["get_object_profile"]("РеализацияТоваров")
            roles = p["sections"]["roles"]
            assert roles["status"] == "ok"
            assert {r["role_name"] for r in roles["items"]} == {"РольПродажи"}
            subs = p["sections"]["subscriptions"]
            assert {s["name"] for s in subs["items"]} == {"ПодпискаРеализация", "ПодпискаUniversal"}
            assert "ПодпискаДоп" not in {s["name"] for s in subs["items"]}  # named collision не протекает
            fo = p["sections"]["functional_options"]
            assert {f["name"] for f in fo["items"]} == {"ФО_Реализация"}
        finally:
            if reader:
                reader.close()


def test_profile_subscriptions_summary_split_and_exact_first():
    """#2 (v1.28.0): subscriptions.summary раскладывает exact/universal; exact-first
    сортировка гарантирует, что явные подписки видны в items даже при малом limit."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_profile_fixture(tmpdir, with_index=True)
        try:
            subs = bsl["get_object_profile"]("РеализацияТоваров")["sections"]["subscriptions"]
            assert subs["status"] == "ok"
            # summary раскладывает: 1 exact (ПодпискаРеализация) + 1 universal (ПодпискаUniversal).
            assert subs["summary"] == {"subscriptions": 2, "exact": 1, "universal": 1}
            assert subs["total"] == 2
            # каждый item несёт scope.
            assert {i["scope"] for i in subs["items"]} == {"exact", "universal"}
            # При limit=1 exact-first сортировка → показан именно exact, не universal-хвост.
            subs1 = bsl["get_object_profile"]("РеализацияТоваров", limit=1)["sections"]["subscriptions"]
            assert subs1["returned"] == 1
            assert subs1["has_more"] is True
            assert subs1["items"][0]["name"] == "ПодпискаРеализация"
            assert subs1["items"][0]["scope"] == "exact"
            # но summary остаётся полным (счёт по всем rows, не по усечённым items).
            assert subs1["summary"] == {"subscriptions": 2, "exact": 1, "universal": 1}
        finally:
            if reader:
                reader.close()


def test_profile_registers_summary_domain_counters_not_flattened():
    """registers.summary keeps code/erp/manager/adapted counters separate (R5 #6)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_profile_fixture(tmpdir, with_index=True)
        try:
            reg = bsl["get_object_profile"]("РеализацияТоваров")["sections"]["registers"]
            assert reg["status"] == "ok"
            assert reg["summary"] == {
                "code_registers": 2,
                "erp_mechanisms": 1,
                "manager_tables": 1,
                "adapted_registers": 1,
            }
            # total/returned/has_more self-consistent (all about the ONE main list)
            assert reg["total"] == 5
            assert reg["returned"] == 5
            assert reg["has_more"] is False
            # items = all movement targets with a source label; breakdown lives in summary
            assert {i["register"] for i in reg["items"]} == {
                "Продажи",
                "Себестоимость",
                "МеханизмУУ",
                "ТаблицаX",
                "АдаптРег",
            }
            assert {i["source"] for i in reg["items"]} == {"code", "erp_mechanism", "manager_table", "adapted"}
        finally:
            if reader:
                reader.close()


def test_profile_no_index_all_sections_unavailable_no_glob():
    """No index → identity from input prefix, ALL data sections 'unavailable', NO glob/read (R10 #1, R11 #1)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        globs, reads = [], []
        bsl, _ = _make_profile_fixture(tmpdir, with_index=False, glob_counter=globs, read_counter=reads)
        globs.clear()
        reads.clear()
        p = bsl["get_object_profile"]("Документ.РеализацияТоваров")
        assert p["object_name"] == "РеализацияТоваров"
        assert p["category"] == "Documents"
        assert p["_meta"]["identity_source"] == "input_prefix"
        for name, sec in p["sections"].items():
            assert sec["status"] == "unavailable", name
            assert sec["_meta"]["reason"] == "no_index"
        # The compact no-index path must NOT touch the filesystem.
        assert globs == [], f"unexpected glob: {globs}"
        assert reads == [], f"unexpected read: {reads}"


def test_profile_no_index_bare_name_errors_no_glob():
    """No index + bare name (no type prefix) → {error:'no_index_identity_unresolved'}, NO glob."""
    with tempfile.TemporaryDirectory() as tmpdir:
        globs = []
        bsl, _ = _make_profile_fixture(tmpdir, with_index=False, glob_counter=globs)
        globs.clear()
        p = bsl["get_object_profile"]("РеализацияТоваров")
        assert p["error"] == "no_index_identity_unresolved"
        assert "hint" in p
        assert globs == []


def test_profile_per_section_isolation():
    """A broken section (component raises) → status 'error', neighbors 'ok', profile still valid (R2 #2, R4 #4)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_profile_fixture(tmpdir, with_index=True)
        try:

            def _boom(*a, **k):
                raise RuntimeError("boom")

            reader.get_roles_exact = _boom  # shadow → roles section raises
            p = bsl["get_object_profile"]("РеализацияТоваров")
            assert "error" not in p  # profile still valid
            assert p["sections"]["roles"]["status"] == "error"
            assert "boom" in p["sections"]["roles"]["_meta"]["error"]
            # neighbors unaffected
            assert p["sections"]["subscriptions"]["status"] in {"ok", "empty"}
            assert p["sections"]["registers"]["status"] == "ok"
        finally:
            if reader:
                reader.close()


def test_profile_not_found_top_level_error():
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_profile_fixture(tmpdir, with_index=True)
        try:
            p = bsl["get_object_profile"]("НетТакогоОбъекта")
            assert "error" in p
            assert p["_meta"]["identity_source"] == "unresolved"
        finally:
            if reader:
                reader.close()


def test_profile_non_document_modules_empty_registers_skipped():
    """Catalog (no BSL modules): modules 'empty', registers 'skipped' (not a Document), structure 'ok'."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_profile_fixture(tmpdir, with_index=True)
        try:
            p = bsl["get_object_profile"]("Справочник.Контрагенты")
            assert p["object_name"] == "Контрагенты"
            assert p["category"] == "Catalogs"
            assert p["sections"]["modules"]["status"] == "empty"
            assert p["sections"]["registers"]["status"] == "skipped"
            assert p["sections"]["registers"]["_meta"]["reason"] == "not_a_document"
            structure = p["sections"]["structure"]
            assert structure["status"] == "ok"
            assert structure["summary"]["attributes"] == 2
        finally:
            if reader:
                reader.close()


def test_profile_sections_arg_takes_exactly_requested():
    """sections=[...] (with aliases) runs ONLY the requested sections."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_profile_fixture(tmpdir, with_index=True)
        try:
            p = bsl["get_object_profile"]("РеализацияТоваров", sections=["structure", "права"])
            assert set(p["sections"]) == {"structure", "roles"}  # 'права' → roles alias
        finally:
            if reader:
                reader.close()


def test_profile_include_flow_adds_heavy_section_only_on_flag():
    """flow/code_usages sections appear ONLY under include_flow/include_code_usages (heavy gated)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_profile_fixture(tmpdir, with_index=True)
        try:
            default = bsl["get_object_profile"]("РеализацияТоваров")
            assert "flow" not in default["sections"]
            assert "code_usages" not in default["sections"]
            withflow = bsl["get_object_profile"]("РеализацияТоваров", include_flow=True)
            assert "flow" in withflow["sections"]
            assert withflow["sections"]["flow"]["_meta"]["source"] == "mixed"
        finally:
            if reader:
                reader.close()


def test_profile_items_carry_no_procedure_bodies():
    """Compact output contract (R4 #3): section items expose only names/paths/counts, no bodies."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_profile_fixture(tmpdir, with_index=True)
        try:
            p = bsl["get_object_profile"]("РеализацияТоваров")
            for sec in p["sections"].values():
                for item in sec["items"]:
                    # no procedure bodies / large snippets (a small 'source' label is fine)
                    assert "body" not in item and "text" not in item
                    # items are small flat dicts (names/paths/counts only)
                    for v in item.values():
                        assert not (isinstance(v, str) and len(v) > 300)
        finally:
            if reader:
                reader.close()


def _make_homonym_profile_fixture(tmpdir):
    """Document.Заказ AND Catalog.Заказ — both have an ObjectModule (a true homonym)."""
    for cat in ("Documents", "Catalogs"):
        d = os.path.join(tmpdir, cat, "Заказ", "Ext")
        os.makedirs(d)
        with open(os.path.join(d, "ObjectModule.bsl"), "w", encoding="utf-8") as f:
            f.write(BSL_CODE)
    with open(os.path.join(tmpdir, "Configuration.xml"), "w") as f:
        f.write("<Configuration/>")
    helpers, resolve_safe = make_helpers(tmpdir)
    format_info = detect_format(tmpdir)
    from rlm_tools_bsl.bsl_index import IndexBuilder, IndexReader

    db_path = IndexBuilder().build(tmpdir, build_calls=False, build_metadata=True)
    reader = IndexReader(str(db_path))
    bsl = make_bsl_helpers(
        base_path=tmpdir,
        resolve_safe=resolve_safe,
        read_file_fn=helpers["read_file"],
        grep_fn=helpers["grep"],
        glob_files_fn=helpers["glob_files"],
        format_info=format_info,
        idx_reader=reader,
    )
    return bsl, reader


def test_profile_homonym_modules_category_aware():
    """Homonym Document.Заказ / Catalog.Заказ: modules section carries ONLY the resolved
    category's module (category-aware adapter, not a merge of both) (R2 #1, R5 #1)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_homonym_profile_fixture(tmpdir)
        try:
            p = bsl["get_object_profile"]("Заказ")
            cat = p["category"]
            assert cat in {"Documents", "Catalogs"}
            mods = p["sections"]["modules"]
            assert mods["status"] == "ok"
            # every module path belongs to the ONE resolved category (no homonym merge)
            folder = cat + "/"
            for item in mods["items"]:
                assert folder in item["path"].replace("\\", "/"), (cat, item["path"])
            assert mods["total"] == 1  # exactly that object's single module
        finally:
            if reader:
                reader.close()


def test_profile_prefix_disambiguates_homonym():
    """Explicit type prefix routes identity (and ALL sections) to the requested category on a
    cross-category homonym — in the INDEXED path, not just no-index (codex finding 1)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_homonym_profile_fixture(tmpdir)
        try:
            doc = bsl["get_object_profile"]("Документ.Заказ")
            assert doc["category"] == "Documents"
            assert doc["_meta"]["identity_source"] == "index_prefix"
            assert doc["sections"]["modules"]["items"]
            for item in doc["sections"]["modules"]["items"]:
                assert "Documents/" in item["path"].replace("\\", "/")
            # structure stays on the SAME homonym (category_hint) — no identity_match drift
            assert "identity_match" not in doc["sections"]["structure"]["_meta"]

            cat = bsl["get_object_profile"]("Справочник.Заказ")
            assert cat["category"] == "Catalogs"
            for item in cat["sections"]["modules"]["items"]:
                assert "Catalogs/" in item["path"].replace("\\", "/")
        finally:
            if reader:
                reader.close()


def test_profile_prefix_missing_type_falls_back_honestly():
    """Explicit prefix for a type that does NOT contain the object → fall back to the object's
    REAL category with identity_source='index' (NOT a misleading 'index_prefix'). Guards the
    Pass-3 close-match leak when prefer_category is set (codex finding 5)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # ONLY Catalog.Заказ exists — there is NO Document.Заказ.
        d = os.path.join(tmpdir, "Catalogs", "Заказ", "Ext")
        os.makedirs(d)
        with open(os.path.join(d, "ObjectModule.bsl"), "w", encoding="utf-8") as f:
            f.write(BSL_CODE)
        with open(os.path.join(tmpdir, "Configuration.xml"), "w") as f:
            f.write("<Configuration/>")
        helpers, resolve_safe = make_helpers(tmpdir)
        format_info = detect_format(tmpdir)
        from rlm_tools_bsl.bsl_index import IndexBuilder, IndexReader

        db_path = IndexBuilder().build(tmpdir, build_calls=False, build_metadata=True)
        reader = IndexReader(str(db_path))
        bsl = make_bsl_helpers(
            base_path=tmpdir,
            resolve_safe=resolve_safe,
            read_file_fn=helpers["read_file"],
            grep_fn=helpers["grep"],
            glob_files_fn=helpers["glob_files"],
            format_info=format_info,
            idx_reader=reader,
        )
        try:
            p = bsl["get_object_profile"]("Документ.Заказ")
            # the object exists only as a Catalog → resolved to its real category, honestly labelled
            assert p["category"] == "Catalogs"
            assert p["object_name"] == "Заказ"
            assert p["_meta"]["identity_source"] == "index", "must NOT claim index_prefix on a fallback"
        finally:
            reader.close()


def test_profile_prefix_case_insensitive():
    """The type prefix is recognised case-insensitively AND `bare` is derived from the suffix,
    so 'document.X' / 'DOCUMENT.X' behave exactly like 'Document.X' / 'Документ.X' (finding 6)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_homonym_profile_fixture(tmpdir)
        try:
            for variant in ("Документ.Заказ", "document.Заказ", "DOCUMENT.Заказ", "Document.Заказ"):
                p = bsl["get_object_profile"](variant)
                assert "error" not in p, (variant, p)
                assert p["category"] == "Documents", variant
                assert p["object_name"] == "Заказ", variant
                assert p["_meta"]["identity_source"] == "index_prefix", variant
        finally:
            if reader:
                reader.close()


def test_profile_modules_skipped_status_on_stale(monkeypatch):
    """no_live modules section that hits a stale module → status 'skipped' (NOT 'ok'): the
    zero methods/exports are not authoritative; section flagged for a full live re-read (finding 2)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_profile_fixture(tmpdir, with_index=True)
        try:
            real = reader.get_outline_data

            def _stale(rel_path):
                if rel_path.endswith("ObjectModule.bsl"):
                    return {
                        "module": {
                            "category": "Documents",
                            "object_name": "РеализацияТоваров",
                            "module_type": "ObjectModule",
                        },
                        "regions": [],
                        "methods": [],
                    }
                return real(rel_path)

            monkeypatch.setattr(reader, "get_outline_data", _stale)
            mods = bsl["get_object_profile"]("РеализацияТоваров")["sections"]["modules"]
            assert mods["status"] == "skipped"
            assert mods["_meta"]["modules_skipped_live"] is True
            assert mods["_meta"]["reason"] == "stale_modules_skipped_live"
            om = next(i for i in mods["items"] if i["path"].endswith("ObjectModule.bsl"))
            assert om["skipped_live"] is True
            assert om["methods"] == 0  # not read — flagged, not silently authoritative
        finally:
            if reader:
                reader.close()


def test_profile_code_usages_surfaces_live_fallback(monkeypatch):
    """code_usages reflects find_code_usages' live grep fallback (partial=True → source 'live',
    not 'index') so the hidden live cost is visible (codex finding 3)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_profile_fixture(tmpdir, with_index=True)
        try:

            def _boom(*a, **k):
                raise RuntimeError("no v13 metadata_code_usages table")

            monkeypatch.setattr(reader, "find_code_usages", _boom)
            monkeypatch.setattr(reader, "count_code_usages", _boom)
            p = bsl["get_object_profile"]("РеализацияТоваров", include_code_usages=True)
            cu = p["sections"]["code_usages"]
            assert cu["_meta"]["source"] == "live"
            assert cu["_meta"]["partial"] is True
            assert cu["summary"]["partial"] is True
            # the per-section _meta source surfaced into the top-level trace
            trace = next(s for s in p["_meta"]["sections"] if s["name"] == "code_usages")
            assert trace["source"] == "live"
        finally:
            if reader:
                reader.close()


def test_profile_trace_truncated_matches_has_more():
    """A section sliced by `limit` (has_more=True) reports _meta.truncated=True AND the
    top-level trace truncated=True — the trace must not lie about truncation (codex finding)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_profile_fixture(tmpdir, with_index=True)
        try:
            p = bsl["get_object_profile"]("РеализацияТоваров", limit=2)
            reg = p["sections"]["registers"]  # 5 movements, limit 2 → truncated preview
            assert reg["has_more"] is True
            assert reg["_meta"]["truncated"] is True  # section _meta honest
            reg_trace = next(s for s in p["_meta"]["sections"] if s["name"] == "registers")
            assert reg_trace["truncated"] is True  # trace agrees with has_more

            subs = p["sections"]["subscriptions"]  # only 1 match → not truncated
            assert subs["has_more"] is False
            assert subs["_meta"]["truncated"] is False
            subs_trace = next(s for s in p["_meta"]["sections"] if s["name"] == "subscriptions")
            assert subs_trace["truncated"] is False
        finally:
            if reader:
                reader.close()


# ============================================================
# P3 — extract_procedures / get_module_outline принимают имя ИЛИ путь
# Единое правило (category, module_type): (CommonModules/CommonForms, Module)
# → (*, ObjectModule) → (*, ManagerModule) → первый по стабильной сортировке.
# ============================================================


def _make_ambiguous_object_fixture(tmpdir):
    """Document «Многоформ» с ДВУМЯ формами и БЕЗ ObjectModule → обе строки
    (Documents, Module) одного ранга → неоднозначность. Live-режим."""
    for fn in ("Ф1", "Ф2"):
        d = os.path.join(tmpdir, "Documents", "Многоформ", "Forms", fn, "Ext", "Form")
        os.makedirs(d)
        with open(os.path.join(d, "Module.bsl"), "w", encoding="utf-8") as f:
            f.write("Процедура ПриОткрытии()\nКонецПроцедуры\n")
    with open(os.path.join(tmpdir, "Configuration.xml"), "w") as f:
        f.write("<Configuration/>")
    helpers, resolve_safe = make_helpers(tmpdir)
    format_info = detect_format(tmpdir)
    return make_bsl_helpers(
        base_path=tmpdir,
        resolve_safe=resolve_safe,
        read_file_fn=helpers["read_file"],
        grep_fn=helpers["grep"],
        glob_files_fn=helpers["glob_files"],
        format_info=format_info,
    )


def test_extract_procedures_by_name_picks_object_module(bsl_env):
    """Имя Document → (*, ObjectModule) по единому правилу; результат == по пути."""
    by_name = bsl_env.bsl["extract_procedures"]("АвансовыйОтчет")
    names = {p["name"] for p in by_name}
    assert "ОбработкаЗаполнения" in names  # ObjectModule выбран (у формы методов нет)
    om_path = next(
        m["path"] for m in bsl_env.bsl["find_module"]("АвансовыйОтчет") if m["path"].endswith("ObjectModule.bsl")
    )
    by_path = bsl_env.bsl["extract_procedures"](om_path)
    assert {p["name"] for p in by_path} == names


def test_extract_procedures_by_name_common_module(bsl_env):
    """Имя общего модуля → (CommonModules, Module) ранг 0."""
    procs = bsl_env.bsl["extract_procedures"]("МойМодуль")
    assert {p["name"] for p in procs} == {"ЗаполнитьДанные", "ПолучитьСумму", "ВнутренняяПроцедура"}


def test_extract_procedures_path_mode_unchanged(bsl_env):
    """Путь (есть '/') — прежний контракт, детект имени не ложно-срабатывает."""
    path = bsl_env.bsl["find_module"]("МойМодуль")[0]["path"]
    assert "/" in path
    procs = bsl_env.bsl["extract_procedures"](path)
    assert len(procs) == 3


def test_extract_procedures_ambiguous_name_raises():
    """Неоднозначное имя → ValueError (НЕ [] — пустой список неотличим от «нет процедур»)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl = _make_ambiguous_object_fixture(tmpdir)
        with pytest.raises(ValueError):
            bsl["extract_procedures"]("Многоформ")


def test_get_module_outline_by_name_merges_meta(bsl_env):
    """Имя → _meta МЕРЖИТ resolver-ключи в существующий _meta (index_used/fallback_reason НЕ затёрты)."""
    by_name = bsl_env.bsl["get_module_outline"]("МойМодуль")
    meta = by_name["_meta"]
    # старый контракт _meta цел
    assert "index_used" in meta and "fallback_reason" in meta
    # resolver-ключи домержены
    assert meta["resolved_from_name"] is True
    assert meta["chosen_module"].endswith("Module.bsl")
    assert meta["ambiguous"] is False
    assert "candidates" in meta
    # name-режим == path-режим того же модуля
    path = meta["chosen_module"]
    by_path = bsl_env.bsl["get_module_outline"](path)
    assert by_path["_meta"]["resolved_from_name"] is False
    assert "index_used" in by_path["_meta"] and "fallback_reason" in by_path["_meta"]
    assert by_name["totals"] == by_path["totals"]


def test_get_module_outline_ambiguous_name_flags_meta():
    """Неоднозначное имя → прозрачный авто-выбор: _meta.ambiguous=True + кандидаты, НЕ ошибка."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl = _make_ambiguous_object_fixture(tmpdir)
        o = bsl["get_module_outline"]("Многоформ")
        assert o["_meta"]["resolved_from_name"] is True
        assert o["_meta"]["ambiguous"] is True
        assert len(o["_meta"]["candidates"]) == 2
        assert "totals" in o  # валидный outline (авто-выбор первого по стабильной сортировке)
        assert "index_used" in o["_meta"] and "fallback_reason" in o["_meta"]


# ============================================================
# Codex review fixes (dense-batching): findings 1/2/3
# ============================================================


def test_resolve_module_arg_strips_meta_prefix_extract_procedures(bsl_env):
    """Finding 2: имя с префиксом типа (Документ.X) резолвится так же, как bare-имя."""
    bare = {p["name"] for p in bsl_env.bsl["extract_procedures"]("АвансовыйОтчет")}
    prefixed = {p["name"] for p in bsl_env.bsl["extract_procedures"]("Документ.АвансовыйОтчет")}
    assert prefixed == bare
    assert "ОбработкаЗаполнения" in prefixed


def test_resolve_module_arg_strips_meta_prefix_get_module_outline(bsl_env):
    """Finding 2: get_module_outline('Документ.X') резолвит ObjectModule, не уходит битым путём."""
    o = bsl_env.bsl["get_module_outline"]("Документ.АвансовыйОтчет")
    assert "error" not in o
    assert o["_meta"]["resolved_from_name"] is True
    assert o["_meta"]["chosen_module"].endswith("ObjectModule.bsl")
    # bare и prefixed дают один и тот же выбранный модуль
    bare = bsl_env.bsl["get_module_outline"]("АвансовыйОтчет")
    assert bare["_meta"]["chosen_module"] == o["_meta"]["chosen_module"]


def test_single_or_map_isolates_raising_item():
    """Finding 3: исключение скаляра на одном элементе батча → {error} под его ключом,
    валидный сосед всё равно резолвится (батч не оборван)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_full_fixture(tmpdir)
        enum_dir = os.path.join(tmpdir, "Enums", "СтатусыЗаказов")
        os.makedirs(enum_dir)
        with open(os.path.join(enum_dir, "СтатусыЗаказов.xml"), "w", encoding="utf-8") as f:
            f.write(ENUM_CF_XML)
        # 123 (int) → _strip_meta_prefix(123) бросает AttributeError в скалярном ядре.
        res = bsl["find_enum_values"](["СтатусыЗаказов", 123])
        assert isinstance(res, dict)
        assert res["СтатусыЗаказов"]["name"] == "СтатусыЗаказов"  # сосед уцелел
        assert "error" in res["123"]  # битый элемент изолирован, батч не упал


def test_get_object_modules_collision_category_deterministic():
    """Finding 1: одноимённые объекты в разных категориях → выбор детерминирован
    (rel-path сорт), модули ровно ОДНОЙ категории — get_all_modules() без ORDER BY
    больше не делает результат порядок-зависимым."""
    with tempfile.TemporaryDirectory() as tmpdir:
        for cat in ("Catalogs", "Documents"):
            obj = os.path.join(tmpdir, cat, "Омоним", "Ext")
            os.makedirs(obj)
            with open(os.path.join(obj, "ObjectModule.bsl"), "w", encoding="utf-8") as f:
                f.write("Процедура П() Экспорт\nКонецПроцедуры\n")
        with open(os.path.join(tmpdir, "Configuration.xml"), "w") as f:
            f.write("<Configuration/>")
        helpers, resolve_safe = make_helpers(tmpdir)
        format_info = detect_format(tmpdir)
        bsl = make_bsl_helpers(
            base_path=tmpdir,
            resolve_safe=resolve_safe,
            read_file_fn=helpers["read_file"],
            grep_fn=helpers["grep"],
            glob_files_fn=helpers["glob_files"],
            format_info=format_info,  # live, без индекса
        )
        res = bsl["get_object_modules"]("Омоним")
        assert "error" not in res
        # Catalogs/... < Documents/... по rel-path → первая категория Catalogs.
        assert res["category"] == "Catalogs"
        assert res["modules"]  # непусто
        # ровно одна категория: все модули из Catalogs, ни одного из Documents
        assert all(m["path"].startswith("Catalogs/") for m in res["modules"])
        assert not any("Documents" in m["path"] for m in res["modules"])


# ===========================================================================
# v1.28.0 — делегированное проведение: posting_handler_present в ОБОИХ маршрутах
# ===========================================================================

_DELEGATED = (
    "Процедура ОбработкаПроведения(Отказ, РежимПроведения)\n"
    "    ОбщийМодульУчета.ОтразитьВУчете(ЭтотОбъект, Отказ);\n"
    "КонецПроцедуры\n"
)
_WITH_MOVEMENTS = (
    "Процедура ОбработкаПроведения(Отказ, РежимПроведения)\n"
    "    Движения.ТоварыНаСкладах.Записывать = Истина;\n"
    "КонецПроцедуры\n"
)
# Сигнал posting_handler_present утверждает РОВНО две вещи: обработчик есть, прямых Движения.X
# нет. Про ФОРМУ тела он не знает ничего — а значит все тела ниже его законно выставляют, и hint
# обязан вести к ответу в КАЖДОМ из них, а не только при квалифицированном делегате.
_HANDLER_WRITES_SETS = (  # ветка (A): пишет сам обработчик, делегата НЕТ вовсе
    "Процедура ОбработкаПроведения(Отказ, РежимПроведения)\n"
    "    НаборЗаписей = РегистрыНакопления.ВзаиморасчетыСКонтрагентами.СоздатьНаборЗаписей();\n"
    "    НаборЗаписей.Записать();\n"
    "КонецПроцедуры\n"
)
_DELEGATED_LOCAL = (  # ветка (B): вызов БЕЗ точки — метод в ЭТОМ ЖЕ модуле, find_definition не нужен
    "Процедура ОбработкаПроведения(Отказ, РежимПроведения)\n"
    "    ЗаписатьДвиженияНабором(Отказ);\n"
    "КонецПроцедуры\n"
    "\n"
    "Процедура ЗаписатьДвиженияНабором(Отказ)\n"
    "    НаборЗаписей = РегистрыНакопления.ВзаиморасчетыСКонтрагентами.СоздатьНаборЗаписей();\n"
    "    НаборЗаписей.Записать();\n"
    "КонецПроцедуры\n"
)
# Ветка (B), но вызов БЕЗ точки НЕ означает «метод тут же»: он бывает экспортным методом
# ГЛОБАЛЬНОГО общего модуля (или методом глобального контекста). В модуле объекта его НЕТ, и
# read_procedure(путь_объекта, ...) вернёт пусто — маршрут обязан вести дальше, а не обрываться.
# Заодно набор пишется РегистрыСведений (а не Накопления): наборы есть у ВСЕХ видов регистров.
_GLOBAL_DELEGATE_NAME = "ЗаписатьДвиженияГлобально"
_DELEGATED_GLOBAL = (
    f"Процедура ОбработкаПроведения(Отказ, РежимПроведения)\n    {_GLOBAL_DELEGATE_NAME}(Отказ);\nКонецПроцедуры\n"
)
# Ветка (C), но слева от точки — ПЕРЕМЕННАЯ, а не общий модуль. Точка в вызове НЕ доказывает, что
# получатель — модуль: `Приемник.Метод()` законно вызывается и на объекте/переменной. Если в
# конфигурации есть общий модуль-однофамилец с тем же методом, find_definition отработает УСПЕШНО
# и молча отдаст ЧУЖОЕ тело — это отказ хуже падения, потому что выглядит как ответ.
_VAR_RECEIVER = "СервисПроведения"
_VAR_DELEGATE_METHOD = "ЗаписатьДвижения"
_DELEGATED_VIA_VARIABLE = (
    "Процедура ОбработкаПроведения(Отказ, РежимПроведения)\n"
    f"    {_VAR_RECEIVER} = ПолучитьСервисПроведения();\n"
    f"    {_VAR_RECEIVER}.{_VAR_DELEGATE_METHOD}(ЭтотОбъект, Отказ);\n"
    "КонецПроцедуры\n"
)
# Формы объявления получателя, КАЖДАЯ из которых законна в BSL. Первая версия (C1) искала две
# точные подстроки ("Имя =" / "Перем Имя") в ТЕЛЕ процедуры — то есть была регистрозависимой,
# требовала конкретных пробелов и не видела модульных переменных. На всех формах, кроме первой,
# она возвращала False и снова уводила агента в чужой общий модуль.
_VAR_RECEIVER_FORMS = {
    # (a) каноничная — единственная, которую ловила первая версия
    "assign_spaced": (
        "Процедура ОбработкаПроведения(Отказ, РежимПроведения)\n"
        f"    {_VAR_RECEIVER} = ПолучитьСервисПроведения();\n"
        f"    {_VAR_RECEIVER}.{_VAR_DELEGATE_METHOD}(ЭтотОбъект, Отказ);\n"
        "КонецПроцедуры\n"
    ),
    # (b) BSL РЕГИСТРОНЕЗАВИСИМ: это ОДНА переменная, а подстрока "СервисПроведения =" не встречается
    "assign_lowercase_no_space": (
        "Процедура ОбработкаПроведения(Отказ, РежимПроведения)\n"
        f"    {_VAR_RECEIVER.lower()}=ПолучитьСервисПроведения();\n"
        f"    {_VAR_RECEIVER}.{_VAR_DELEGATE_METHOD}(ЭтотОбъект, Отказ);\n"
        "КонецПроцедуры\n"
    ),
    # (c) переменная МОДУЛЯ, объявленная СПИСКОМ и ВНЕ тела процедуры: в теле обработчика её
    #     объявления нет вовсе — значит проверять надо весь модуль, а не тело
    "module_level_perem_list": (
        f"Перем Прочее, {_VAR_RECEIVER};\n\n"
        "Процедура ОбработкаПроведения(Отказ, РежимПроведения)\n"
        f"    {_VAR_RECEIVER}.{_VAR_DELEGATE_METHOD}(ЭтотОбъект, Отказ);\n"
        "КонецПроцедуры\n"
        "\n"
        "Процедура ПриКопировании(ОбъектКопирования)\n"
        f"    {_VAR_RECEIVER} = ПолучитьСервисПроведения();\n"
        "КонецПроцедуры\n"
    ),
    # (d) английский диалект BSL: Var семантически эквивалентен Перем и обязан
    #     затенять одноимённый общий модуль без вспомогательного присваивания
    "module_level_var": (
        f"Var {_VAR_RECEIVER};\n\n"
        "Procedure ОбработкаПроведения(Отказ, РежимПроведения)\n"
        f"    {_VAR_RECEIVER}.{_VAR_DELEGATE_METHOD}(ЭтотОбъект, Отказ);\n"
        "EndProcedure\n"
    ),
}


def _make_posting_env(
    tmpdir,
    docs,
    *,
    posting=None,
    reads=None,
    manager_tables=None,
    manager_modules=None,
    with_movements_doc=True,
    homonym_delegate=False,
    cross_category_delegate=False,
    global_delegate=False,
    variable_receiver_delegate=False,
    attribute_receiver=False,
    extra_common_modules=None,
    post_index_common_modules=None,
    post_index_object_modules=None,
    index_backed_glob=False,
    ext_docs=None,
    ext_attribute_receiver=False,
    extra_extension_paths=None,
    git=False,
    no_index=False,
):
    """docs: {имя_документа: текст ObjectModule.bsl} → (bsl, reader).

    База конфигурации — ПОДКАТАЛОГ `cf` внутри tmpdir, а расширение (CFE) — соседний `cfe`,
    то есть ВНЕ песочницы: только так воспроизводится боевая топология, где ObjectModule
    расширения виден хелперам как `../cfe/.../ObjectModule.bsl`, а generic read_file на него
    отвечает PermissionError. Пока фикстура клала всё в одну папку, CFE-путь не возникал —
    и дефект «hint зовёт read_file на путь расширения» тесты пропускали.

    reads: если передан список — в него пишется каждый путь, прочитанный через read_file_fn
    (шпион для гейта чтений профиля).

    ВАЖНО: всегда добавляем служебный документ С движениями — IndexReader.get_register_movements
    возвращает None при ГЛОБАЛЬНО пустой таблице, и профиль тогда уходит в _unavailable ДО
    обогащения. На реальной конфигурации таблица непуста, фикстура обязана это воспроизводить.

    posting: {документ: "Deny"|"Allow"} → Posting в XML документа.
    manager_tables: {документ: [имена]} → ManagerModule с ТекстЗапросаТаблицаX (result["manager_tables"] непуст).
    manager_modules: {документ: текст} → произвольный ManagerModule.bsl (для экспортных делегатов).
    homonym_delegate: ВТОРОЙ общий модуль с ОДНОИМЁННЫМ ОтразитьВУчете (одноимённые методы в 1С — норма).
    cross_category_delegate: СПРАВОЧНИК, названный ТОЧНО как общий модуль (имена уникальны лишь ВНУТРИ категории).
    global_delegate: ГЛОБАЛЬНЫЙ общий модуль — его экспортный метод зовут БЕЗ точки.
    variable_receiver_delegate: общий модуль-ОДНОФАМИЛЕЦ переменной-получателя (ловушка «чужое тело»).
    attribute_receiver: РЕКВИЗИТ документа с именем получателя — вариант ловушки БЕЗ единого маркера
                        присваивания в модуле: текстовая эвристика его не видит в принципе.
    ext_docs: {документ: текст ObjectModule.bsl} → модуль в CFE-РАСШИРЕНИИ (вне песочницы).
    post_index_common_modules: общие модули, созданные ПОСЛЕ SQLite build, но ДО helper-сессии;
                               воспроизводят штатный stale-снимок при неизменном дереве сессии.
    post_index_object_modules: main ObjectModule, созданные ПОСЛЕ SQLite build, но ДО helper-сессии.
    index_backed_glob: передать BSL-фабрике production-like generic glob из того же SQLite;
                       stale-регрессия обязана обходить и этот второй снимок.
    extra_extension_paths: дополнительные сконфигурированные roots расширений (в т.ч. недоступные).
    git: детерминированно регистрировать git_search (True) либо исключать его (False), независимо
         от того, не оказался ли системный temp случайно внутри чужого git-worktree.
    no_index: не строить индекс (idx_reader=None).
    """
    base = os.path.join(tmpdir, "cf")
    ext_root = os.path.join(tmpdir, "cfe", "ExtPosting")
    os.makedirs(base, exist_ok=True)

    if with_movements_doc:
        docs = {**docs, "СлужебныйДокСДвижениями": _WITH_MOVEMENTS}
    # Делегат из _DELEGATED существует ПО-НАСТОЯЩЕМУ: без него маршрут из hint нельзя пройти до
    # конца, а тест, исполняющий только первый шаг, снова завизировал бы полу-рабочий совет.
    cm = os.path.join(base, "CommonModules", "ОбщийМодульУчета", "Ext")
    os.makedirs(cm, exist_ok=True)
    # Регистр НАМЕРЕННО не тот, что в _WITH_MOVEMENTS: тот пишется прямым `Движения.X` из
    # служебного документа, и find_register_writers его НАШЁЛ БЫ — тест перестал бы доказывать,
    # что записи наборами он не видит.
    with open(os.path.join(cm, "Module.bsl"), "w", encoding="utf-8") as f:
        f.write(
            "Процедура ОтразитьВУчете(Объект, Отказ) Экспорт\n"
            "    НаборЗаписей = РегистрыНакопления.ВзаиморасчетыСКонтрагентами.СоздатьНаборЗаписей();\n"
            "    НаборЗаписей.Записать();\n"
            "КонецПроцедуры\n"
        )
    for extra_name, extra_body in (extra_common_modules or {}).items():
        # Произвольный общий модуль — например, с экспортным методом, названным КАК платформенный
        # (Записать/Выполнить): боевой паттерн «ПроведениеДокументов.Записать(...)».
        ecm = os.path.join(base, "CommonModules", extra_name, "Ext")
        os.makedirs(ecm, exist_ok=True)
        with open(os.path.join(ecm, "Module.bsl"), "w", encoding="utf-8") as f:
            f.write(extra_body)
    if homonym_delegate:
        cm2 = os.path.join(base, "CommonModules", "ОбщийМодульДругой", "Ext")
        os.makedirs(cm2, exist_ok=True)
        with open(os.path.join(cm2, "Module.bsl"), "w", encoding="utf-8") as f:
            f.write(
                "Процедура ОтразитьВУчете(Объект, Отказ) Экспорт\n"
                "    // ЧУЖОЕ ТЕЛО: этот модуль документ НЕ зовет.\n"
                "КонецПроцедуры\n"
            )
    if global_delegate:
        gm = os.path.join(base, "CommonModules", "ГлобальныйМодульУчета", "Ext")
        os.makedirs(gm, exist_ok=True)
        with open(os.path.join(gm, "Module.bsl"), "w", encoding="utf-8") as f:
            f.write(
                f"Процедура {_GLOBAL_DELEGATE_NAME}(Отказ) Экспорт\n"
                "    НаборЗаписей = РегистрыСведений.СостоянияДокументов.СоздатьНаборЗаписей();\n"
                "    НаборЗаписей.Записать();\n"
                "КонецПроцедуры\n"
            )
    if variable_receiver_delegate:
        vm = os.path.join(base, "CommonModules", _VAR_RECEIVER, "Ext")
        os.makedirs(vm, exist_ok=True)
        with open(os.path.join(vm, "Module.bsl"), "w", encoding="utf-8") as f:
            f.write(
                f"Процедура {_VAR_DELEGATE_METHOD}(Объект, Отказ) Экспорт\n"
                "    // ЧУЖОЕ ТЕЛО: одноименный ОБЩИЙ МОДУЛЬ. Документ зовет метод на ПЕРЕМЕННОЙ\n"
                "    // (или на реквизите), а не на этом модуле.\n"
                "КонецПроцедуры\n"
            )
    if cross_category_delegate:
        cat = os.path.join(base, "Catalogs", "ОбщийМодульУчета", "Ext")
        os.makedirs(cat, exist_ok=True)
        with open(os.path.join(cat, "ObjectModule.bsl"), "w", encoding="utf-8") as f:
            f.write(
                "Процедура ОтразитьВУчете(Объект, Отказ) Экспорт\n"
                "    // ЧУЖОЕ ТЕЛО: одноименный СПРАВОЧНИК, а не общий модуль.\n"
                "КонецПроцедуры\n"
            )
        with open(os.path.join(base, "Catalogs", "ОбщийМодульУчета.xml"), "w", encoding="utf-8") as f:
            f.write(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">\n'
                '  <Catalog uuid="u-cat"><Properties><Name>ОбщийМодульУчета</Name></Properties></Catalog>\n'
                "</MetaDataObject>\n"
            )

    attr_xml = ""
    if attribute_receiver:
        attr_xml = (
            "  <Attribute><Properties><Name>"
            + _VAR_RECEIVER
            + '</Name><Type><v8:Type xmlns:v8="http://v8.1c.ru/8.1/data/core">xs:string</v8:Type></Type>'
            "</Properties></Attribute>\n"
        )

    for doc, body in docs.items():
        ext = os.path.join(base, "Documents", doc, "Ext")
        os.makedirs(ext, exist_ok=True)
        if doc not in (post_index_object_modules or {}):
            with open(os.path.join(ext, "ObjectModule.bsl"), "w", encoding="utf-8") as f:
                f.write(body)
        tables = (manager_tables or {}).get(doc) or []
        manager_body = (manager_modules or {}).get(doc) or ""
        if tables or manager_body:
            with open(os.path.join(ext, "ManagerModule.bsl"), "w", encoding="utf-8") as f:
                f.write(manager_body)
                if manager_body and tables:
                    f.write("\n")
                for t in tables:
                    f.write("Функция ТекстЗапросаТаблица" + t + '()\n    Возврат "";\nКонецФункции\n\n')
        post = (posting or {}).get(doc)
        post_xml = f"<Posting>{post}</Posting>" if post else ""
        with open(os.path.join(base, "Documents", f"{doc}.xml"), "w", encoding="utf-8") as f:
            f.write(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">\n'
                f'  <Document uuid="u-{doc}">\n'
                f"    <Properties><Name>{doc}</Name>{post_xml}</Properties>\n"
                f"{attr_xml if doc in (docs.keys() - {'СлужебныйДокСДвижениями'}) else ''}"
                "  </Document>\n"
                "</MetaDataObject>\n"
            )
    with open(os.path.join(base, "Configuration.xml"), "w") as f:
        f.write("<Configuration/>")

    extension_paths = []
    if ext_docs or ext_attribute_receiver:
        os.makedirs(ext_root, exist_ok=True)
        with open(os.path.join(ext_root, "Configuration.xml"), "w") as f:
            f.write("<Configuration/>")
        for doc, body in (ext_docs or {}).items():
            ext_dir = os.path.join(ext_root, "Documents", doc, "Ext")
            os.makedirs(ext_dir, exist_ok=True)
            with open(os.path.join(ext_dir, "ObjectModule.bsl"), "w", encoding="utf-8") as f:
                f.write(body)
        if ext_attribute_receiver:
            # РАСШИРЕНИЕ добавляет документу реквизит с именем получателя. Ни индекс (main-only),
            # ни живой XML ОСНОВНОЙ конфигурации этого реквизита не видят — его видно только в
            # метаданных самого расширения. Layout зависит от формата дампа: CF — sibling
            # Documents/<Имя>.xml, EDT — Documents/<Имя>/<Имя>.mdo. Оба поддержаны штатным
            # резолвером — фикстура обязана уметь оба, иначе тесты пиняют лишь один диалект.
            if ext_attribute_receiver == "corrupt":
                # Метаданные расширения СУЩЕСТВУЮТ (локатор их увидит), но НЕ разбираются:
                # состояние «прочитать не удалось» — полноту live-проверки оно обязано ломать.
                os.makedirs(os.path.join(ext_root, "Documents"), exist_ok=True)
                with open(os.path.join(ext_root, "Documents", "ТестДок.xml"), "w", encoding="utf-8") as f:
                    f.write("<оборванный-xml без закрытия")
            elif ext_attribute_receiver == "mdo":
                mdo_dir = os.path.join(ext_root, "Documents", "ТестДок")
                os.makedirs(mdo_dir, exist_ok=True)
                with open(os.path.join(mdo_dir, "ТестДок.mdo"), "w", encoding="utf-8") as f:
                    f.write(
                        '<?xml version="1.0" encoding="UTF-8"?>\n'
                        '<mdclass:Document xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass">'
                        "<name>ТестДок</name>"
                        f"<attributes><name>{_VAR_RECEIVER}</name></attributes>"
                        "</mdclass:Document>\n"
                    )
            else:
                os.makedirs(os.path.join(ext_root, "Documents"), exist_ok=True)
                with open(os.path.join(ext_root, "Documents", "ТестДок.xml"), "w", encoding="utf-8") as f:
                    f.write(
                        '<?xml version="1.0" encoding="UTF-8"?>\n'
                        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">\n'
                        '  <Document uuid="u-ext-doc">\n'
                        "    <Properties><Name>ТестДок</Name></Properties>\n"
                        f"  <Attribute><Properties><Name>{_VAR_RECEIVER}</Name></Properties></Attribute>\n"
                        "  </Document>\n"
                        "</MetaDataObject>\n"
                    )
        extension_paths = [ext_root]
    extension_paths.extend(extra_extension_paths or [])

    if git:
        # git_search в развилках hint — терминальный маршрут; чтобы тест мог ИСПОЛНИТЬ его,
        # а не только скомпилировать, база должна быть git-рабочим деревом.
        import subprocess

        for args in (["init", "-q"], ["add", "."]):
            subprocess.run(["git", "-C", base, *args], check=True, capture_output=True)

    helpers, resolve_safe = make_helpers(base)
    from rlm_tools_bsl.bsl_index import IndexBuilder, IndexReader

    if no_index:
        reader = None
    else:
        db = IndexBuilder().build(base, build_calls=False, build_metadata=True)
        reader = IndexReader(str(db))

    for extra_name, extra_body in (post_index_common_modules or {}).items():
        ecm = os.path.join(base, "CommonModules", extra_name, "Ext")
        os.makedirs(ecm, exist_ok=True)
        with open(os.path.join(ecm, "Module.bsl"), "w", encoding="utf-8") as f:
            f.write(extra_body)

    for doc, body in (post_index_object_modules or {}).items():
        object_module_dir = os.path.join(base, "Documents", doc, "Ext")
        os.makedirs(object_module_dir, exist_ok=True)
        with open(os.path.join(object_module_dir, "ObjectModule.bsl"), "w", encoding="utf-8") as f:
            f.write(body)

    if index_backed_glob:
        helpers, resolve_safe = make_helpers(base, idx_reader=reader)

    read_fn = helpers["read_file"]
    if reads is not None:

        def read_fn(path, _orig=helpers["read_file"], _sink=reads):  # noqa: F811
            _sink.append(path)
            return _orig(path)

    bsl = make_bsl_helpers(
        base_path=base,
        resolve_safe=resolve_safe,
        read_file_fn=read_fn,
        grep_fn=helpers["grep"],
        glob_files_fn=helpers["glob_files"],
        format_info=detect_format(base),
        idx_reader=reader,
        extension_paths=extension_paths,
        register_git_search="force" if git else "never",
    )
    # Шаги hint исполняются в ПЕСОЧНИЦЕ, где рядом с bsl-хелперами живут и базовые. `read_file`
    # там отдаёт строки С НОМЕРАМИ (sandbox._numbered_read_file) и ЗАПРЕЩАЕТ выход за базу —
    # кладём в namespace ровно это, чтобы тест проверял ту же среду, что видит агент.
    from rlm_tools_bsl._format import number_lines

    bsl["read_file"] = lambda path, _rf=read_fn: number_lines(_rf(path))
    return bsl, reader


def test_find_register_movements_flags_posting_handler_without_movements():
    """Есть ОбработкаПроведения, но ни одного `Движения.X` → честный сигнал + hint."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(tmpdir, {"ТестДок": _DELEGATED})
        try:
            res = bsl["find_register_movements"]("ТестДок")
            assert res["code_registers"] == []
            assert res.get("posting_handler_present") is True, res
            assert "ОбработкаПроведения" in res.get("hint", "")
            # Путь в hint — ТОЧНЫЙ rel_path, а не 'Документ.ТестДок': при main+CFE-омонимах
            # имя резолвится неоднозначно.
            hint = res["hint"].replace("\\", "/")
            assert "Documents/ТестДок/Ext/ObjectModule.bsl" in hint, hint
            assert "'Документ.ТестДок'" not in hint, hint
        finally:
            reader.close()


_DELEGATE = "ОтразитьВУчете"
_DELEGATE_MODULE = "ОбщийМодульУчета"


def _hint_steps(hint):
    """Исполнимые шаги hint, ВЫРЕЗАННЫЕ из его текста → {номер: код}.

    Шаги нумерованы подряд: «(N) код -> пояснение». ПЛЕЙСХОЛДЕРОВ БОЛЬШЕ НЕТ — имена делегата,
    модуля и путь подставил СЕРВЕР, потому что только он может их разрешить (агент не прочитает
    CFE-модуль и не отличит переменную от общего модуля). Тест исполняет ровно тот текст, который
    агент копирует: псевдокод здесь = SyntaxError = красный тест.
    """
    pairs = re.findall(r"\((\d+)\)\s*(.+?)\s*->", hint)
    labels = [lbl for lbl, _ in pairs]
    assert len(labels) == len(set(labels)), f"номер шага встречается дважды и перезапишет код: {labels}"
    steps = dict(pairs)
    for label, src in steps.items():
        assert "<" not in src, f"шаг ({label}) hint — псевдокод, а не Python: {src!r}"
        # Ни один шаг не имеет права звать generic read_file: handler_path может указывать в CFE,
        # а песочница туда не пускает (PermissionError). Это гейт против возврата старого маршрута.
        assert "read_file(" not in src, f"шаг ({label}) зовет read_file — на CFE-пути это PermissionError: {src!r}"
    return steps


_DECL_SAFE_GREP_RE = re.compile(r"safe_grep\('\(\?i\)\^(?:\\.|[^'])*', max_files=\d+, _result_cap=\d+\)")


def _decl_search_fragments(hint):
    """Executable declaration-search calls, including prose branches without a step number."""
    return _DECL_SAFE_GREP_RE.findall(hint)


def test_posting_hint_route_executes_end_to_end_and_names_the_delegate():
    """Hint обязан вести в РАБОТАЮЩИЙ маршрут, а тест — ИСПОЛНЯТЬ его, а не искать подстроку.

    Сервер сам разобрал тело: назвал делегата (получатель разрешён как ОБЩИЙ МОДУЛЬ) и выдал шаги.
    Тест вырезает шаги ИЗ ТЕКСТА hint и исполняет их — то есть проверяет ровно то, что агент
    скопирует. Маршрут доводится ДО КОНЦА, включая последний шаг: `find_register_writers` записи
    наборами НЕ находит, и hint обязан об этом предупреждать, а не обещать несуществующее.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(tmpdir, {"ТестДок": _DELEGATED})
        try:
            res = bsl["find_register_movements"]("ТестДок")
            hint = res["hint"]

            # Сервер НАЗВАЛ делегата и КЕМ является получатель — агенту не надо этого угадывать.
            assert f"{_DELEGATE_MODULE}.{_DELEGATE}" in hint, hint
            assert "ОБЩИЙ МОДУЛЬ" in hint, hint
            # ...и объяснил, почему callers=0 у обработчика — это норма, но без перебора.
            assert "ПЛАТФОРМ" in hint and "мертвый код" in hint, hint
            assert "ЯВНЫЙ вызов" in hint, hint

            code = _hint_steps(hint)
            ns = dict(bsl)
            exec(compile(code["1"], "<hint:1>", "exec"), ns)  # noqa: S102 → body = read_procedure(...)
            body = ns["body"]
            assert f"{_DELEGATE_MODULE}.{_DELEGATE}" in body, body
            assert "Движения." not in body

            exec(compile(code["2"], "<hint:2>", "exec"), ns)  # noqa: S102 → d = find_definition(...)
            assert ns["d"]["total"] >= 1, ns["d"]

            dbody = eval(compile(code["3"], "<hint:3>", "eval"), ns)  # noqa: S307
            assert dbody and "СоздатьНаборЗаписей" in dbody, dbody  # ЧЕМ он пишет

            # РАЗВИЛКА: тут набор записей — и find_register_writers его НЕ находит. Контроль: тот же
            # хелпер НАХОДИТ прямого писателя, значит ноль ниже — не «хелпер сломан», а слепота к наборам.
            assert bsl["find_register_writers"]("ТоварыНаСкладах")["total_writers"] >= 1
            assert bsl["find_register_writers"]("ВзаиморасчетыСКонтрагентами")["total_writers"] == 0
            assert "НЕ НАЙДЕТ" in hint, "hint не говорит, что find_register_writers наборы не найдет"
            # Стенд НЕ под git → git_search в песочнице НЕ зарегистрирован, и hint не имеет права
            # его советовать (дословное копирование дало бы NameError); живой фолбэк — safe_grep.
            assert "git_search(" not in hint, f"hint советует незарегистрированный git_search: {hint}"
            assert "safe_grep" in hint, hint
        finally:
            reader.close()


def test_posting_hint_does_not_offer_the_tautological_category_confirmation():
    """Подтверждать делегата через `definitions[0]['category'] == 'CommonModules'` НЕЛЬЗЯ.

    module_hint='ОбщийМодуль.X' уже добавляет в SQL `mod.category = 'CommonModules'`
    (bsl_index._normalize_module_hint + WHERE), поэтому проверка истинна ПО ПОСТРОЕНИЮ: она
    подтверждает не получателя, а собственный фильтр запроса. Агент, прочитавший тело
    одноимённого общего модуля, считал бы его «подтверждённым» — круговая порука.

    Тест ПРЕДЪЯВЛЯЕТ тавтологию: гоняет ту самую проверку на заведомо ЧУЖОМ модуле и показывает,
    что она True. Значит hint обязан её запрещать и опираться на разбор сервера."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(tmpdir, {"ТестДок": _DELEGATED_VIA_VARIABLE}, variable_receiver_delegate=True)
        try:
            # ЧУЖОЙ модуль (получатель — переменная), но «подтверждение по category» его одобряет:
            d = bsl["find_definition"](_VAR_DELEGATE_METHOD, f"ОбщийМодуль.{_VAR_RECEIVER}")
            assert d["definitions"], d
            assert d["definitions"][0].get("category") == "CommonModules", "фикстура не воспроизвела тавтологию"
            assert "ЧУЖОЕ ТЕЛО" in bsl["read_procedure"](d["definitions"][0]["file"], _VAR_DELEGATE_METHOD)

            hint = bsl["find_register_movements"]("ТестДок")["hint"]
            assert "d['definitions'][0]['category']" not in hint, hint
            assert "['category'] == 'CommonModules'" not in hint, hint
        finally:
            reader.close()

    # А там, где делегат НАСТОЯЩИЙ, hint обязан ПРЯМО предупредить, что такая проверка бессмысленна:
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(tmpdir, {"ТестДок": _DELEGATED})
        try:
            hint = bsl["find_register_movements"]("ТестДок")["hint"]
            assert "ВСЕГДА" in hint and "истинна" in hint, f"hint не называет проверку category тавтологией: {hint}"
        finally:
            reader.close()


_RECEIVER_CASES = {
    # (kwargs фикстуры, тело обработчика, ожидаемая метка получателя)
    "variable_assign_spaced": (
        {"variable_receiver_delegate": True},
        _VAR_RECEIVER_FORMS["assign_spaced"],
        "ПЕРЕМЕННАЯ",
    ),
    # BSL РЕГИСТРОНЕЗАВИСИМ и пробелы необязательны: 'сервиспроведения=' объявляет ТУ ЖЕ переменную
    "variable_lowercase_no_space": (
        {"variable_receiver_delegate": True},
        _VAR_RECEIVER_FORMS["assign_lowercase_no_space"],
        "ПЕРЕМЕННАЯ",
    ),
    # объявление Перем СПИСКОМ и ВНЕ тела процедуры — в теле обработчика его нет вовсе
    "variable_module_level_perem": (
        {"variable_receiver_delegate": True},
        _VAR_RECEIVER_FORMS["module_level_perem_list"],
        "ПЕРЕМЕННАЯ",
    ),
    "variable_module_level_var": (
        {"variable_receiver_delegate": True},
        _VAR_RECEIVER_FORMS["module_level_var"],
        "ПЕРЕМЕННАЯ",
    ),
    # РЕКВИЗИТ документа: маркеров присваивания НЕТ НИ ОДНОГО — текстовая эвристика бессильна
    # в принципе, и старый гейт пропускал этот случай целиком.
    "attribute": (
        {"variable_receiver_delegate": True, "attribute_receiver": True},
        (
            "Процедура ОбработкаПроведения(Отказ, РежимПроведения)\n"
            f"    {_VAR_RECEIVER}.{_VAR_DELEGATE_METHOD}(ЭтотОбъект);\n"
            "КонецПроцедуры\n"
        ),
        "РЕКВИЗИТ",
    ),
    # получатель, которого нет НИГДЕ: ни переменная, ни реквизит, ни общий модуль
    "unknown": (
        {},
        (
            "Процедура ОбработкаПроведения(Отказ, РежимПроведения)\n"
            "    НеизвестныйПолучатель.ЗаписатьДвижения(ЭтотОбъект);\n"
            "КонецПроцедуры\n"
        ),
        "НЕ ОПОЗНАН",
    ),
}


@pytest.mark.parametrize("case", sorted(_RECEIVER_CASES))
def test_posting_hint_resolves_the_receiver_instead_of_guessing(case):
    """Получателя слева от точки РАЗРЕШАЕТ сервер — у него есть живой модуль, _index_state и индекс
    реквизитов. Агент разрешить его не может В ПРИНЦИПЕ: текстовая эвристика не видит реквизит
    (маркеров присваивания нет вовсе), а «подтверждение по category» тавтологично.

    Для КАЖДОГО не-модульного получателя hint обязан: назвать, ЧТО это; НЕ предлагать
    find_definition по 'ОбщийМодуль.<получатель>'; увести в поиск по дереву. Там, где существует
    общий модуль-однофамилец, — назвать ЛОВУШКУ явно.
    """
    kwargs, body, label = _RECEIVER_CASES[case]
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(tmpdir, {"ТестДок": body}, **kwargs)
        try:
            res = bsl["find_register_movements"]("ТестДок")
            assert res.get("posting_handler_present") is True, res  # сигнал законен: форму тела он не знает
            hint = res["hint"]

            assert label in hint, f"({case}) hint не назвал вид получателя: {hint}"
            # ГЛАВНОЕ: ни один ИСПОЛНИМЫЙ ШАГ не ведёт в одноимённый общий модуль. Проверяем именно
            # шаги, а не весь текст: назвать опасный вызов В ПРЕДУПРЕЖДЕНИИ («find_definition(...)
            # отдал бы ЧУЖОЕ тело — не ходи туда») не только можно, но и нужно — это и есть ловушка.
            steps = _hint_steps(hint)
            forbidden = f"find_definition('{_VAR_DELEGATE_METHOD}', 'ОбщийМодуль.{_VAR_RECEIVER}')"
            offending = [f"({k}) {v}" for k, v in steps.items() if forbidden in v]
            assert not offending, f"({case}) ШАГ ведет в ЧУЖОЙ общий модуль: {offending}"
            # Стенд НЕ под git: маршрут по дереву обязан строиться из зарегистрированных хелперов
            # (find_definition без module-hint), а не из недоступного git_search (NameError).
            assert "git_search(" not in hint, f"({case}) hint советует незарегистрированный git_search: {hint}"
            assert "find_definition(" in hint, f"({case}) hint не дает маршрут по дереву: {hint}"

            if kwargs.get("variable_receiver_delegate"):
                # ...и ЛОВУШКА названа: одноимённый модуль существует, find_definition вернул бы ЕГО тело.
                assert "ЛОВУШКА" in hint, f"({case}) hint не предупреждает про модуль-однофамильца: {hint}"
                d = bsl["find_definition"](_VAR_DELEGATE_METHOD, f"ОбщийМодуль.{_VAR_RECEIVER}")
                foreign = bsl["read_procedure"](d["definitions"][0]["file"], _VAR_DELEGATE_METHOD)
                assert "ЧУЖОЕ ТЕЛО" in foreign  # предъявляем опасность, от которой защищаемся

            # Шаги, которые hint всё же даёт, обязаны быть исполнимы.
            for label_, src in _hint_steps(hint).items():
                compile(src, f"<hint:{label_}>", "exec")
        finally:
            reader.close()


def test_posting_hint_does_not_mistake_a_comparison_for_an_assignment():
    """В BSL `=` — это И присваивание, И СРАВНЕНИЕ, и на этом легко соврать В ОБЕ стороны.

    `Если ОбщийМодульУчета = Неопределено Тогда` — СРАВНЕНИЕ. Наивный маркер «есть X =» объявил бы
    НАСТОЯЩИЙ общий модуль «переменной» и увёл бы агента от рабочего делегата — то есть повторил бы
    грех релиза (утверждать больше, чем знаешь), только в другую сторону. Обратная крайность —
    игнорировать такой `=` — молча подсунула бы ЧУЖОЕ тело, если получатель всё-таки переменная.

    Честный ответ — РАЗВИЛКА: сервер говорит «общий модуль с таким именем есть, но ВОЗМОЖНО ЗАТЕНЕН»
    и даёт ОБА маршрута, оставляя решение телу. А присваивание в позиции ОПЕРАТОРА (начало строки,
    после ';', после `Тогда`) сравнением быть не может — там сервер отвечает уверенно.
    """
    ambiguous = (
        "Процедура ОбработкаПроведения(Отказ, РежимПроведения)\n"
        f"    Если {_DELEGATE_MODULE} = Неопределено Тогда\n"
        "        Возврат;\n"
        "    КонецЕсли;\n"
        f"    {_DELEGATE_MODULE}.{_DELEGATE}(ЭтотОбъект, Отказ);\n"
        "КонецПроцедуры\n"
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(tmpdir, {"ТестДок": ambiguous})
        try:
            hint = bsl["find_register_movements"]("ТестДок")["hint"]
            assert "ВОЗМОЖНО ЗАТЕНЕН" in hint, f"сравнение принято за присваивание (или наоборот): {hint}"
            # ОБА маршрута названы, решение оставлено телу — а не выдумано.
            # Не-git стенд: оба маршрута развилки обязаны строиться из зарегистрированных
            # хелперов — git_search здесь просто нет в песочнице.
            assert "find_definition(" in hint and "git_search(" not in hint, hint
            for label, src in _hint_steps(hint).items():
                compile(src, f"<hint:{label}>", "exec")
        finally:
            reader.close()

    # КОНТРОЛЬ: присваивание после `Тогда` сравнением быть не может — сервер отвечает уверенно.
    assigned_in_branch = (
        "Процедура ОбработкаПроведения(Отказ, РежимПроведения)\n"
        f"    Если {_VAR_RECEIVER} = Неопределено Тогда {_VAR_RECEIVER} = ПолучитьСервис(); КонецЕсли;\n"
        f"    {_VAR_RECEIVER}.{_VAR_DELEGATE_METHOD}(ЭтотОбъект);\n"
        "КонецПроцедуры\n"
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(tmpdir, {"ТестДок": assigned_in_branch}, variable_receiver_delegate=True)
        try:
            hint = bsl["find_register_movements"]("ТестДок")["hint"]
            assert "ПЕРЕМЕННАЯ" in hint, f"присваивание в ветке Если не распознано: {hint}"
            assert "ЛОВУШКА" in hint, hint  # одноимённый общий модуль есть — ловушка обязана быть названа
        finally:
            reader.close()


def test_posting_hint_works_when_the_handler_lives_only_in_a_cfe_extension():
    """Обработчик бывает ТОЛЬКО в CFE-расширении — и это ровно тот случай, ради которого сигнал
    делался (делегированное проведение из расширения).

    Путь такого модуля — `../cfe/...`, то есть ВНЕ песочницы: generic read_file на него бросает
    PermissionError (тест это ПРЕДЪЯВЛЯЕТ). Прежний hint велел агенту звать read_file(path) —
    маршрут обрывался исключением именно там, где был нужен. Теперь модуль читает СЕРВЕР
    (_ext_read_file), а в hint уходят факты и только ext-safe шаги.
    """
    main_without_handler = "Процедура ПередЗаписью(Отказ)\nКонецПроцедуры\n"
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(
            tmpdir,
            {"ТестДок": main_without_handler},
            ext_docs={"ТестДок": _DELEGATED},
        )
        try:
            res = bsl["find_register_movements"]("ТестДок")
            assert res.get("posting_handler_present") is True, res
            hint = res["hint"]
            path = re.search(r"read_procedure\('([^']+)'", hint).group(1)
            assert path.startswith("../"), f"фикстура не воспроизвела CFE-путь: {path}"

            # ПРЕДЪЯВЛЯЕМ ОПАСНОСТЬ: то, что советовал прежний hint, здесь ПАДАЕТ.
            with pytest.raises(PermissionError):
                bsl["read_file"](path)

            # А маршрут из hint — работает: read_procedure ext-aware.
            code = _hint_steps(hint)  # он же гейтит отсутствие read_file( в шагах
            ns = dict(bsl)
            exec(compile(code["1"], "<hint:1>", "exec"), ns)  # noqa: S102
            assert "ОбработкаПроведения" in ns["body"], ns["body"]
            exec(compile(code["2"], "<hint:2>", "exec"), ns)  # noqa: S102
            dbody = eval(compile(code["3"], "<hint:3>", "eval"), ns)  # noqa: S307
            assert dbody and "СоздатьНаборЗаписей" in dbody, dbody
        finally:
            reader.close()


def test_posting_hint_includes_a_cfe_posting_interceptor():
    """Точное ОбработкаПроведения в main не исчерпывает runtime-цепочку: &После в CFE тоже
    выполняется и может писать регистр набором. Hint обязан разобрать оба entrypoint и дать
    ext-safe маршрут к произвольно названной процедуре расширения.
    """
    main = "Процедура ОбработкаПроведения(Отказ, РежимПроведения)\nКонецПроцедуры\n"
    extension = (
        '&После("ОбработкаПроведения")\n'
        "Процедура ДополнитьПроведение(Отказ, РежимПроведения)\n"
        "    Набор = РегистрыСведений.СледCFE.СоздатьНаборЗаписей();\n"
        "    Набор.Записать();\n"
        "КонецПроцедуры\n"
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(
            tmpdir,
            {"ТестДок": main},
            ext_docs={"ТестДок": extension},
        )
        try:
            hint = bsl["find_register_movements"]("ТестДок")["hint"]
            assert "РегистрыСведений.СледCFE" in hint, f"запись из &После потеряна: {hint}"
            assert 'CFE-перехвата &После("ОбработкаПроведения")' in hint, hint
            assert "судя по коду, движений он не пишет" not in hint, hint

            cfe_steps = [
                src for src in _hint_steps(hint).values() if "read_procedure" in src and "ДополнитьПроведение" in src
            ]
            assert len(cfe_steps) == 1, hint
            ns = dict(bsl)
            exec(compile(cfe_steps[0], "<hint:cfe-posting>", "exec"), ns)  # noqa: S102
            assert "СледCFE" in ns["cfe_body_1"], ns["cfe_body_1"]

            profile_hint = (
                bsl["get_object_profile"]("ТестДок", sections=["registers"])["sections"]["registers"].get("hint") or ""
            )
            assert "РегистрыСведений.СледCFE" in profile_hint, f"профиль потерял &После: {profile_hint}"
        finally:
            reader.close()


def test_indexed_register_movements_merge_direct_cfe_rows():
    """The SQLite snapshot covers only the main config. Direct ``Движения.X``
    from an adjacent CFE must augment the indexed result instead of merely
    suppressing posting_handler_present and leaving code_registers empty."""
    main = "Процедура ОбработкаПроведения(Отказ, РежимПроведения)\nКонецПроцедуры\n"
    extension = (
        '&После("ОбработкаПроведения")\n'
        "Процедура ДополнитьПроведение(Отказ, РежимПроведения)\n"
        "    Движения . ПрямойРегистрCFE.Записывать = Истина;\n"
        "КонецПроцедуры\n"
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(
            tmpdir,
            {"ТестДок": main},
            ext_docs={"ТестДок": extension},
        )
        try:
            result = bsl["find_register_movements"]("ТестДок")
            cfe_rows = [r for r in result["code_registers"] if r["name"] == "ПрямойРегистрCFE"]
            assert len(cfe_rows) == 1, result
            assert cfe_rows[0]["source"] == "code"
            assert cfe_rows[0]["file"] in result["modules_scanned"]
            assert "posting_handler_present" not in result, result

            section = bsl["get_object_profile"]("ТестДок", sections=["registers"])["sections"]["registers"]
            assert {i["register"] for i in section["items"]} >= {"ПрямойРегистрCFE"}, section
            assert section["summary"]["code_registers"] >= 1
            assert section["_meta"]["source"] == "mixed"
            assert section["_meta"]["extension_modules_scanned"] == 1
        finally:
            reader.close()


@pytest.mark.parametrize("no_index", [False, True], ids=["indexed", "live"])
def test_cfe_instead_suppresses_main_posting_movements(no_index):
    """A CFE replacement without an explicit continuation makes the main
    posting handler unreachable.  CFE movements remain active and the main
    snapshot stays available as explicitly suppressed provenance."""
    extension = (
        '&Вместо("ОбработкаПроведения")\n'
        "Процедура ЗаменитьПроведение(Отказ, РежимПроведения)\n"
        "    // ПродолжитьВызов(Отказ, РежимПроведения);\n"
        '    ЛожныйСигнал = "ProceedWithCall(Отказ, РежимПроведения)";\n'
        "    Сервис . ProceedWithCall(Отказ, РежимПроведения);\n"
        "    ProceedWithCall(Отказ, РежимПроведения);\n"
        "    Движения.РегистрТолькоCFE.Записывать = Истина;\n"
        "КонецПроцедуры\n"
        "Процедура ProceedWithCall(Отказ, РежимПроведения)\n"
        "КонецПроцедуры\n"
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(
            tmpdir,
            {"ТестДок": _WITH_MOVEMENTS},
            ext_docs={"ТестДок": extension},
            no_index=no_index,
        )
        try:
            result = bsl["find_register_movements"]("ТестДок")
            assert {row["name"] for row in result["code_registers"]} == {"РегистрТолькоCFE"}, result
            assert {row["name"] for row in result["suppressed_main_code_registers"]} == {"ТоварыНаСкладах"}, result
            replacement = result["_meta"]["cfe_posting_replacement"]
            assert replacement["main_handler_continuation_visible"] is False, replacement
            assert replacement["interceptors"][0]["annotation"] == "Вместо", replacement
            assert "suppressed_main_code_registers" in result["hint"], result["hint"]
            assert "хотя бы в одной точной процедуре замены не найден" in result["hint"], result["hint"]

            reverse = bsl["find_register_writers"]("ТоварыНаСкладах")
            assert any(row["document"] == "ТестДок" for row in reverse["writers"]), reverse
            assert reverse["runtime_filtered"] is False, reverse
            assert "find_register_movements(document)" in reverse["hint"], reverse

            if reader is not None:
                section = bsl["get_object_profile"]("ТестДок", sections=["registers"])["sections"]["registers"]
                assert section["items"] == [{"register": "РегистрТолькоCFE", "source": "code"}], section
                assert section["summary"]["code_registers"] == 1, section
                assert section["summary"]["main_code_registers_suppressed_by_cfe"] == 1, section
                assert section["_meta"]["cfe_posting_replacement"]["main_handler_continuation_visible"] is False, (
                    section
                )
        finally:
            if reader is not None:
                reader.close()


def test_cfe_instead_without_movements_reports_handler_but_not_main_movements():
    """A replacement can delegate dynamically even when it has no direct
    movements.  The helper must expose that handler while keeping suppressed
    main rows out of the active movement list."""
    extension = (
        '&Вместо("ОбработкаПроведения")\n'
        "Процедура ЗаменитьПроведение(Отказ, РежимПроведения)\n"
        "    ОбщийМодульУчета.ОтразитьВУчете(ЭтотОбъект, Отказ);\n"
        "КонецПроцедуры\n"
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(
            tmpdir,
            {"ТестДок": _WITH_MOVEMENTS},
            ext_docs={"ТестДок": extension},
        )
        try:
            result = bsl["find_register_movements"]("ТестДок")
            assert result["code_registers"] == [], result
            assert [row["name"] for row in result["suppressed_main_code_registers"]] == ["ТоварыНаСкладах"], result
            assert result["posting_handler_present"] is True, result
            assert "CFE-ЗАМЕНА" in result["hint"], result["hint"]

            section = bsl["get_object_profile"]("ТестДок", sections=["registers"])["sections"]["registers"]
            assert section["items"] == [], section
            assert section["summary"]["code_registers"] == 0, section
            assert section["summary"]["main_code_registers_suppressed_by_cfe"] == 1, section
            assert section["summary"]["posting_handler_present"] is True, section
            assert "CFE-ЗАМЕНА" in section["hint"], section["hint"]
        finally:
            reader.close()


@pytest.mark.parametrize("continue_call", ["ПродолжитьВызов", "ProceedWithCall"])
def test_cfe_instead_with_visible_continue_keeps_main_posting_movements(continue_call):
    """An explicit continuation in the exact replacement procedure preserves
    possible execution of the main posting handler."""
    extension = (
        '&Вместо("ОбработкаПроведения")\n'
        "Процедура ЗаменитьПроведение(Отказ, РежимПроведения)\n"
        f"    {continue_call}(Отказ, РежимПроведения);\n"
        "КонецПроцедуры\n"
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(
            tmpdir,
            {"ТестДок": _WITH_MOVEMENTS},
            ext_docs={"ТестДок": extension},
        )
        try:
            result = bsl["find_register_movements"]("ТестДок")
            assert [row["name"] for row in result["code_registers"]] == ["ТоварыНаСкладах"], result
            assert "suppressed_main_code_registers" not in result, result
            assert result["_meta"]["cfe_posting_replacement"]["main_handler_continuation_visible"] is True, result

            section = bsl["get_object_profile"]("ТестДок", sections=["registers"])["sections"]["registers"]
            assert section["summary"]["code_registers"] == 1, section
            assert "main_code_registers_suppressed_by_cfe" not in section["summary"], section
            assert section["_meta"]["cfe_posting_replacement"]["main_handler_continuation_visible"] is True, section
        finally:
            reader.close()


@pytest.mark.parametrize("no_index", [False, True], ids=["indexed", "live"])
def test_cfe_instead_suppresses_only_handler_local_main_movements(no_index):
    """Index provenance is module-wide.  A replacement may still call another
    main-module procedure, so only movements confined to the replaced handler
    are suppressible."""
    main = (
        "Процедура ОбработкаПроведения(Отказ, РежимПроведения)\n"
        "    Движения.ТолькоВMainHandler.Записывать = Истина;\n"
        "КонецПроцедуры\n"
        "Процедура ЗаписатьДополнительно()\n"
        "    Движения.ИзMainHelper.Записывать = Истина;\n"
        "КонецПроцедуры\n"
    )
    extension = (
        '&Вместо("ОбработкаПроведения")\n'
        "Процедура ЗаменитьПроведение(Отказ, РежимПроведения)\n"
        "    ЗаписатьДополнительно();\n"
        "КонецПроцедуры\n"
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(
            tmpdir,
            {"ТестДок": main},
            ext_docs={"ТестДок": extension},
            no_index=no_index,
        )
        try:
            result = bsl["find_register_movements"]("ТестДок")
            assert {row["name"] for row in result["code_registers"]} == {"ИзMainHelper"}, result
            assert {row["name"] for row in result["suppressed_main_code_registers"]} == {"ТолькоВMainHandler"}, result

            if reader is not None:
                section = bsl["get_object_profile"]("ТестДок", sections=["registers"])["sections"]["registers"]
                assert section["items"] == [{"register": "ИзMainHelper", "source": "code"}], section
                assert section["summary"]["main_code_registers_suppressed_by_cfe"] == 1, section
        finally:
            if reader is not None:
                reader.close()


def test_all_cfe_replacements_must_continue_main_handler():
    """One blocking replacement cuts the lower call chain even when another
    configured extension contains a direct continuation."""
    continuing = (
        '&Вместо("ОбработкаПроведения")\n'
        "Процедура ПродолжающаяЗамена(Отказ, РежимПроведения)\n"
        "    ПродолжитьВызов(Отказ, РежимПроведения);\n"
        "КонецПроцедуры\n"
    )
    blocking = '&Вместо("ОбработкаПроведения")\nПроцедура БлокирующаяЗамена(Отказ, РежимПроведения)\nКонецПроцедуры\n'
    with tempfile.TemporaryDirectory() as tmpdir:
        second_root = os.path.join(tmpdir, "cfe-second")
        second_module = os.path.join(second_root, "Documents", "ТестДок", "Ext")
        os.makedirs(second_module)
        with open(os.path.join(second_root, "Configuration.xml"), "w") as f:
            f.write("<Configuration/>")
        with open(os.path.join(second_module, "ObjectModule.bsl"), "w", encoding="utf-8") as f:
            f.write(blocking)

        bsl, reader = _make_posting_env(
            tmpdir,
            {"ТестДок": _WITH_MOVEMENTS},
            ext_docs={"ТестДок": continuing},
            extra_extension_paths=[second_root],
        )
        try:
            result = bsl["find_register_movements"]("ТестДок")
            assert result["code_registers"] == [], result
            assert [row["name"] for row in result["suppressed_main_code_registers"]] == ["ТоварыНаСкладах"], result
            replacement = result["_meta"]["cfe_posting_replacement"]
            assert replacement["main_handler_continuation_visible"] is False, replacement
            assert {row["continues_main"] for row in replacement["interceptors"]} == {False, True}
            assert "хотя бы в одной точной процедуре замены" in result["hint"], result["hint"]
        finally:
            reader.close()


def test_live_exact_document_cfe_is_not_lost_behind_fuzzy_cap():
    """The no-capability path must collect every exact main/CFE ObjectModule even
    when 50 earlier main modules contain the requested document name."""
    blocking = '&Вместо("ОбработкаПроведения")\nПроцедура БлокирующаяЗамена(Отказ, РежимПроведения)\nКонецПроцедуры\n'
    homonyms = {f"ТестДокКопия{i:02d}": "Процедура Служебная()\nКонецПроцедуры\n" for i in range(50)}
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(
            tmpdir,
            {"ТестДок": _WITH_MOVEMENTS, **homonyms},
            ext_docs={"ТестДок": blocking},
            no_index=True,
        )
        try:
            result = bsl["find_register_movements"]("ТестДок")
            assert result["code_registers"] == [], result
            assert [row["name"] for row in result["suppressed_main_code_registers"]] == ["ТоварыНаСкладах"], result
            assert result["_meta"]["cfe_posting_replacement"]["main_handler_continuation_visible"] is False
        finally:
            if reader is not None:
                reader.close()


def test_cfe_instead_with_visible_continue_keeps_main_delegates_in_hint():
    """The movement list and the deep posting hint use the same continuation
    decision: a visible continuation keeps main-handler delegates possible."""
    extension = (
        '&Вместо("ОбработкаПроведения")\n'
        "Процедура ЗаменитьПроведение(Отказ, РежимПроведения)\n"
        "    ПродолжитьВызов(Отказ, РежимПроведения);\n"
        "КонецПроцедуры\n"
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(
            tmpdir,
            {"ТестДок": _DELEGATED},
            ext_docs={"ТестДок": extension},
        )
        try:
            result = bsl["find_register_movements"]("ТестДок")
            assert result["code_registers"] == [], result
            assert result["posting_handler_present"] is True, result
            assert "ОбщийМодульУчета.ОтразитьВУчете" in result["hint"], result["hint"]
            assert "main-handler включен в возможные ФАКТЫ" in result["hint"], result["hint"]
            assert "во всех точных процедурах замены" in result["hint"], result["hint"]

            section = bsl["get_object_profile"]("ТестДок", sections=["registers"])["sections"]["registers"]
            assert section["summary"]["posting_handler_present"] is True, section
            assert "ОбщийМодульУчета.ОтразитьВУчете" in section["hint"], section["hint"]
        finally:
            reader.close()


def test_find_register_movements_finds_post_build_main_handler():
    """Main ObjectModule, созданный после build, входит в точный live-сигнал даже
    когда generic glob и список modules читаются из stale SQLite."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(
            tmpdir,
            {"ТестДок": ""},
            post_index_object_modules={"ТестДок": _DELEGATED},
            index_backed_glob=True,
        )
        try:
            assert not any(
                row["object_name"] == "ТестДок" and row["module_type"] == "ObjectModule"
                for row in reader.get_all_modules()
            )
            result = bsl["find_register_movements"]("тестдок")
            assert result["code_registers"] == [], result
            assert result.get("posting_handler_present") is True, result
            hint = result.get("hint", "").replace("\\", "/")
            assert "Documents/ТестДок/Ext/ObjectModule.bsl" in hint, hint
            assert "ОбщийМодульУчета.ОтразитьВУчете" in hint, hint

            # Compact-профиль сохраняет свой index-prefilter: новый module-row не
            # превращает дешёвую секцию в дополнительный live-анализ обработчика.
            section = bsl["get_object_profile"]("тестдок", sections=["registers"])["sections"]["registers"]
            assert "posting_handler_present" not in section["summary"], section
        finally:
            reader.close()


def test_profile_deduplicates_same_main_and_cfe_register_without_losing_helper_provenance():
    """Profile items omit ``file`` and therefore must collapse main/CFE rows that
    would otherwise be indistinguishable. The detailed helper keeps both origins."""
    extension = (
        '&После("ОбработкаПроведения")\n'
        "Процедура ДополнитьПроведение(Отказ, РежимПроведения)\n"
        "    Движения.ТоварыНаСкладах.Записывать = Истина;\n"
        "КонецПроцедуры\n"
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(
            tmpdir,
            {"ТестДок": _WITH_MOVEMENTS},
            ext_docs={"ТестДок": extension},
        )
        try:
            helper_rows = [
                row
                for row in bsl["find_register_movements"]("ТестДок")["code_registers"]
                if row["name"] == "ТоварыНаСкладах"
            ]
            assert len(helper_rows) == 2, helper_rows
            assert len({row["file"] for row in helper_rows}) == 2, helper_rows

            section = bsl["get_object_profile"]("ТестДок", sections=["registers"])["sections"]["registers"]
            matching = [item for item in section["items"] if item["register"] == "ТоварыНаСкладах"]
            assert matching == [{"register": "ТоварыНаСкладах", "source": "code"}], section
            assert section["summary"]["code_registers"] == 1, section
            assert section["total"] == 1, section
        finally:
            reader.close()


@pytest.mark.parametrize(
    "with_movements_doc",
    [True, False],
    ids=["main_table_has_other_rows", "main_table_globally_empty"],
)
def test_indexed_register_movements_marks_unreadable_cfe_module_partial(with_movements_doc):
    """An indexed main ``[]`` must not look complete when an exact CFE
    ObjectModule was discovered but could not be read afterwards."""
    main = "Процедура ОбработкаПроведения(Отказ, РежимПроведения)\nКонецПроцедуры\n"
    extension = "Движения.СкрытыйНечитаемыйРегистр.Записывать = Истина;\n"
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(
            tmpdir,
            {"ТестДок": main},
            ext_docs={"ТестДок": extension},
            with_movements_doc=with_movements_doc,
        )
        try:
            # Build the live extension locator first, then reproduce a configured
            # local CFE file becoming unreadable between discovery and the read.
            modules = bsl["find_module"]("ТестДок")
            ext_path = next(
                m["path"] for m in modules if m.get("module_type") == "ObjectModule" and m["path"].startswith("../")
            )
            os.unlink(os.path.join(tmpdir, "cfe", "ExtPosting", "Documents", "ТестДок", "Ext", "ObjectModule.bsl"))

            result = bsl["find_register_movements"]("ТестДок")
            assert result["code_registers"] == [], result
            assert result["partial"] is True, result
            assert ext_path not in result["modules_scanned"], result
            assert result["_meta"]["extension_modules_unreadable"] == [ext_path], result

            section = bsl["get_object_profile"]("ТестДок", sections=["registers"])["sections"]["registers"]
            assert section["status"] == "unavailable", section
            assert section["_meta"]["partial"] is True, section
            assert section["_meta"]["extension_modules_unreadable"] == [ext_path], section
            assert section["_meta"].get("extension_modules_scanned", 0) == 0, section
        finally:
            reader.close()


def test_profile_keeps_known_cfe_movements_when_main_table_is_globally_empty():
    """A missing/empty main movement capability makes the section partial, but it
    must not erase positive live CFE facts that are independently known."""
    main = "Процедура ОбработкаПроведения(Отказ, РежимПроведения)\nКонецПроцедуры\n"
    extension = "Движения.ЕдинственныйРегистрCFE.Записывать = Истина;\n"
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(
            tmpdir,
            {"ТестДок": main},
            ext_docs={"ТестДок": extension},
            with_movements_doc=False,
        )
        try:
            helper_result = bsl["find_register_movements"]("ТестДок")
            assert {row["name"] for row in helper_result["code_registers"]} == {"ЕдинственныйРегистрCFE"}

            section = bsl["get_object_profile"]("ТестДок", sections=["registers"])["sections"]["registers"]
            assert section["status"] == "unavailable", section
            assert section["items"] == [{"register": "ЕдинственныйРегистрCFE", "source": "code"}]
            assert section["summary"]["code_registers"] == 1
            assert section["_meta"]["partial"] is True
            assert section["_meta"]["reason"] == "main_index_capability_missing"
        finally:
            reader.close()


def test_profile_deduplicates_same_cfe_movement_when_main_capability_is_empty():
    """Two CFE provenance rows collapse after compact profile drops their file paths."""
    main = "Процедура ОбработкаПроведения(Отказ, РежимПроведения)\nКонецПроцедуры\n"
    extension = "Движения.ОдинРегистрИзДвухCFE.Записывать = Истина;\n"
    with tempfile.TemporaryDirectory() as tmpdir:
        second_root = os.path.join(tmpdir, "cfe-second")
        second_module = os.path.join(second_root, "Documents", "ТестДок", "Ext")
        os.makedirs(second_module)
        with open(os.path.join(second_root, "Configuration.xml"), "w") as f:
            f.write("<Configuration/>")
        with open(os.path.join(second_module, "ObjectModule.bsl"), "w", encoding="utf-8") as f:
            f.write(extension)

        bsl, reader = _make_posting_env(
            tmpdir,
            {"ТестДок": main},
            ext_docs={"ТестДок": extension},
            extra_extension_paths=[second_root],
            with_movements_doc=False,
        )
        try:
            helper_result = bsl["find_register_movements"]("ТестДок")
            helper_rows = [row for row in helper_result["code_registers"] if row["name"] == "ОдинРегистрИзДвухCFE"]
            # Globally-empty reader capability falls through to the legacy live helper,
            # which already collapses same-named register rows across modules.
            assert len(helper_rows) == 1

            section = bsl["get_object_profile"]("ТестДок", sections=["registers"])["sections"]["registers"]
            assert section["items"] == [{"register": "ОдинРегистрИзДвухCFE", "source": "code"}]
            assert section["summary"]["code_registers"] == 1
            assert section["total"] == 1 and section["returned"] == 1
            assert section["_meta"]["extension_modules_scanned"] == 2  # оба CFE фактически прочитаны
        finally:
            reader.close()


def test_posting_hint_finds_cfe_interceptor_after_many_comments():
    """Допустимые пустые/comment/directive-строки не имеют синтаксического потолка в шесть строк.

    Старое окно ``pos + 7`` не доходило до процедуры и затем ложно писало, что пустой main-handler
    движений не делает, хотя &После записывал регистр набором.
    """
    main = "Процедура ОбработкаПроведения(Отказ, РежимПроведения)\nКонецПроцедуры\n"
    spacer = "".join(f"// служебный комментарий {i}\n" for i in range(8))
    extension = (
        '&После("ОбработкаПроведения")\n' + spacer + "Процедура ДополнитьПроведение(Отказ, РежимПроведения)\n"
        "    Набор = РегистрыСведений.СледCFEПослеКомментариев.СоздатьНаборЗаписей();\n"
        "    Набор.Записать();\n"
        "КонецПроцедуры\n"
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(
            tmpdir,
            {"ТестДок": main},
            ext_docs={"ТестДок": extension},
        )
        try:
            hint = bsl["find_register_movements"]("ТестДок")["hint"]
            assert "РегистрыСведений.СледCFEПослеКомментариев" in hint, hint
            assert "судя по коду, движений он не пишет" not in hint, hint
        finally:
            reader.close()


def test_posting_hint_route_survives_real_index_states():
    """ВАЛИДНЫЙ Python != РАБОТАЮЩИЙ маршрут: шаги обязаны переживать реальные состояния индекса.

    (а) ОМОНИМ: одноимённые методы в 1С — норма; без категорийного module_hint `[0]` вернул бы
        тело ЧУЖОГО модуля (порядок строк не гарантирован — это монетка).
    (б) ДЕЛЕГАТ НОВЕЕ ИНДЕКСА: definitions=[] → голый [0] дал бы IndexError; guard отдаёт None.
    (в) ИНДЕКСА НЕТ: с категорийным hint find_definition читает модуль живьём и маршрут работает.
    """
    # (а) омоним
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(tmpdir, {"ТестДок": _DELEGATED}, homonym_delegate=True)
        try:
            code = _hint_steps(bsl["find_register_movements"]("ТестДок")["hint"])
            ns = dict(bsl)
            exec(compile(code["1"], "<hint:1>", "exec"), ns)  # noqa: S102
            exec(compile(code["2"], "<hint:2>", "exec"), ns)  # noqa: S102
            assert bsl["find_definition"](_DELEGATE)["total"] >= 2  # без hint кандидатов ДВОЕ
            assert ns["d"]["total"] == 1, f"шаг (2) не сузил омонимы категорийным hint: {ns['d']}"
            dbody = eval(compile(code["3"], "<hint:3>", "eval"), ns)  # noqa: S307
            assert "СоздатьНаборЗаписей" in dbody and "ЧУЖОЕ ТЕЛО" not in dbody, dbody
        finally:
            reader.close()

    # (б) делегата нет в индексе: guard в шаге (3) отдаёт None вместо IndexError
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(tmpdir, {"ТестДок": _DELEGATED})
        try:
            hint = bsl["find_register_movements"]("ТестДок")["hint"]
            code = _hint_steps(hint)
            ns = dict(bsl)
            ns["d"] = {"definitions": [], "total": 0}  # состояние «делегат новее индекса»
            assert eval(compile(code["3"], "<hint:3>", "eval"), ns) is None  # noqa: S307
            # ...и hint говорит, что делать дальше — маршрутом из ЗАРЕГИСТРИРОВАННЫХ хелперов:
            # стенд не под git, модуль-получатель известен -> safe_grep прямо по нему.
            assert "safe_grep" in hint, hint
            assert "git_search(" not in hint, f"hint советует незарегистрированный git_search: {hint}"
        finally:
            reader.close()

    # (в) индекса нет вовсе
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_posting_env(tmpdir, {"ТестДок": _DELEGATED}, no_index=True)
        no_hint = bsl["find_definition"](_DELEGATE)
        assert "definitions" not in no_hint and "error" in no_hint, no_hint  # контракт no-index

        code = _hint_steps(bsl["find_register_movements"]("ТестДок")["hint"])
        ns = dict(bsl)
        exec(compile(code["2"], "<hint:2>", "exec"), ns)  # noqa: S102
        assert ns["d"].get("definitions"), f"категорийный hint без индекса не сработал: {ns['d']}"
        dbody = eval(compile(code["3"], "<hint:3>", "eval"), ns)  # noqa: S307
        assert dbody and "СоздатьНаборЗаписей" in dbody, dbody


def test_posting_hint_module_hint_is_category_aware():
    """module_hint в шаге find_definition ОБЯЗАН нести КАТЕГОРИЮ ('ОбщийМодуль.X').

    Голый hint фильтрует ТОЛЬКО по object_name (_normalize_module_hint → (None, None, name)), а
    имена в 1С уникальны лишь ВНУТРИ категории: Справочник.ОбщийМодульУчета и
    ОбщийМодуль.ОбщийМодульУчета сосуществуют законно и оба могут объявлять один метод."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(tmpdir, {"ТестДок": _DELEGATED}, cross_category_delegate=True)
        try:
            bare = bsl["find_definition"](_DELEGATE, _DELEGATE_MODULE)
            assert bare["total"] >= 2, f"фикстура не воспроизвела кросс-категорийную коллизию: {bare}"

            code = _hint_steps(bsl["find_register_movements"]("ТестДок")["hint"])
            assert "ОбщийМодуль." in code["2"], f"шаг (2) потерял категорийный префикс: {code['2']!r}"
            ns = dict(bsl)
            exec(compile(code["2"], "<hint:2>", "exec"), ns)  # noqa: S102
            assert ns["d"]["total"] == 1, f"категорийный hint не отсёк однофамильца: {ns['d']}"
            dbody = eval(compile(code["3"], "<hint:3>", "eval"), ns)  # noqa: S307
            assert "СоздатьНаборЗаписей" in dbody and "ЧУЖОЕ ТЕЛО" not in dbody, dbody
        finally:
            reader.close()


@pytest.mark.parametrize("kind", ["Накопления", "Сведений", "Бухгалтерии", "Расчета"])
def test_posting_hint_names_record_sets_of_every_register_kind(kind):
    """Наборы записей есть у ВСЕХ видов регистров, не только у РегистрыНакопления. Сервер обязан
    назвать регистр прямо в hint — тогда агенту вообще некуда идти дальше."""
    body = (
        "Процедура ОбработкаПроведения(Отказ, РежимПроведения)\n"
        f"    НаборЗаписей = Регистры{kind}.ТестовыйРегистр.СоздатьНаборЗаписей();\n"
        "    НаборЗаписей.Записать();\n"
        "КонецПроцедуры\n"
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(tmpdir, {"ТестДок": body})
        try:
            res = bsl["find_register_movements"]("ТестДок")
            assert res["code_registers"] == []
            assert res.get("posting_handler_present") is True, res
            hint = res["hint"]
            assert f"Регистры{kind}.ТестовыйРегистр" in hint, f"сервер не назвал регистр: {hint}"
            assert "НЕ НАЙДЕТ" in hint, "hint не предупреждает, что find_register_writers наборы не найдет"
            # `НаборЗаписей.Записать()` — это НЕ делегат, и разбор не имеет права выдавать его за такового.
            assert "НаборЗаписей.Записать" not in hint, f"служебный вызов выдан за делегата: {hint}"
        finally:
            reader.close()


def test_posting_hint_no_git_register_search_covers_all_bsl_candidates():
    """No-git fallback не должен молча оставаться на default `safe_grep(max_files=20)`.

    Писатель регистра после первых двадцати BSL-кандидатов обязан находиться маршрутом,
    который hint рекомендует для поиска остальных писателей.
    """
    register_name = "РедкийРегистрПослеДвадцати"
    body = (
        "Процедура ОбработкаПроведения(Отказ, РежимПроведения)\n"
        f"    Набор = РегистрыСведений.{register_name}.СоздатьНаборЗаписей();\n"
        "    Набор.Записать();\n"
        "КонецПроцедуры\n"
    )
    modules = {f"ДополнительныйМодуль{i:02d}": "Процедура Служебная() Экспорт\nКонецПроцедуры\n" for i in range(30)}
    modules["ЯПоследнийПисатель"] = (
        "Процедура ЗаписатьРедкийРегистр() Экспорт\n"
        f"    Набор = РегистрыСведений.{register_name}.СоздатьНаборЗаписей();\n"
        "КонецПроцедуры\n"
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(tmpdir, {"ТестДок": body}, extra_common_modules=modules)
        try:
            hint = bsl["find_register_movements"]("ТестДок")["hint"]
            matches = re.findall(r"safe_grep\('ИмяРегистра'(?:, max_files=(\d+))?\)", hint)
            assert matches, f"no-git маршрут поиска писателей пропал: {hint}"
            assert all(matches), f"в hint остался второй bare safe_grep с default cap=20: {hint}"
            limit = int(matches[0])
            assert limit > 20, f"маршрут остался на старом потолке: {hint}"
            hits = bsl["safe_grep"](register_name, max_files=limit)
            assert any("ЯПоследнийПисатель" in hit["file"] for hit in hits), hits
        finally:
            reader.close()


def test_posting_hint_platform_globals_do_not_consume_detailed_routes():
    """Reserved platform calls must not push the only real global delegate past the route cap."""
    body = (
        "Процедура ОбработкаПроведения(Отказ, РежимПроведения)\n"
        "    НСтр(\"ru = 'Текст'\");\n"
        '    СокрЛП(" Текст ");\n'
        "    ЗаполнитьЗначенияСвойств(ЭтотОбъект, Источник);\n"
        "    ТекущаяДатаСеанса();\n"
        '    ПредопределенноеЗначение("Перечисление.Тест.Значение");\n'
        "    НачатьТранзакцию();\n"
        "    NStr(\"en = 'Text'\");\n"
        '    TrimAll(" Text ");\n'
        "    FillPropertyValues(ThisObject, Source);\n"
        "    SessionDate();\n"
        '    PredefinedValue("Enum.Test.Value");\n'
        "    BeginTransaction();\n"
        f"    {_GLOBAL_DELEGATE_NAME}(Отказ);\n"
        "КонецПроцедуры\n"
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(tmpdir, {"ТестДок": body}, global_delegate=True)
        try:
            hint = bsl["find_register_movements"]("ТестДок")["hint"]
            assert f"ВЫЗОВ БЕЗ ТОЧКИ {_GLOBAL_DELEGATE_NAME}" in hint, hint
            for platform_name in (
                "НСтр",
                "СокрЛП",
                "ЗаполнитьЗначенияСвойств",
                "ТекущаяДатаСеанса",
                "ПредопределенноеЗначение",
                "НачатьТранзакцию",
                "NStr",
                "TrimAll",
                "FillPropertyValues",
                "SessionDate",
                "PredefinedValue",
                "BeginTransaction",
            ):
                assert f"ВЫЗОВ БЕЗ ТОЧКИ {platform_name}" not in hint, hint
        finally:
            reader.close()


def test_posting_hint_live_declaration_route_handles_cyrillic_case_difference():
    """The live route covers Cyrillic casing and a module newer than SQLite.

    The file is created before the helper session, so the source tree remains immutable
    throughout analysis; only the optional SQLite acceleration snapshot is stale.
    """
    method_call = "записатьдвижениясрегистром"
    method_decl = "ЗаписатьДвиженияСРегистром"
    body = (
        "Процедура ОбработкаПроведения(Отказ, РежимПроведения)\n"
        f"    НеизвестныйПолучатель.{method_call}(ЭтотОбъект);\n"
        "КонецПроцедуры\n"
    )
    target = f"Процедура {method_decl}(Объект) Экспорт\nКонецПроцедуры\n"
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(
            tmpdir,
            {"ТестДок": body},
            post_index_common_modules={"ЦелевойМодуль": target},
            index_backed_glob=True,
            git=True,
        )
        try:
            assert not any(row["object_name"] == "ЦелевойМодуль" for row in reader.get_all_modules())
            hint = bsl["find_register_movements"]("ТестДок")["hint"]
            live_steps = _decl_search_fragments(hint)
            assert len(live_steps) == 1, hint
            assert "(?i)" in live_steps[0] and "max_files=" in live_steps[0]
            hits = eval(compile(live_steps[0], "<hint:live-decl>", "eval"), dict(bsl))  # noqa: S307
            assert any(method_decl in hit.get("text", "") for hit in hits), hits
            assert "ЛЮБОЙ регистр" not in hint
        finally:
            reader.close()


def test_posting_hint_live_declaration_route_caps_common_method_results():
    """A common declaration cannot make the generated route unbounded.

    The final sentinel says the live catalog search stopped early; it is not a
    declaration candidate itself.
    """
    method = "ЧастыйМетодПроведения"
    body = (
        "Процедура ОбработкаПроведения(Отказ, РежимПроведения)\n"
        f"    НеизвестныйПолучатель.{method}(ЭтотОбъект);\n"
        "КонецПроцедуры\n"
    )
    declaration = f"Процедура {method}(Объект) Экспорт\nКонецПроцедуры\n"
    common_modules = {f"ЧастыйМодуль{i:02d}": declaration for i in range(70)}

    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(
            tmpdir,
            {"ТестДок": body},
            extra_common_modules=common_modules,
            git=True,
        )
        try:
            hint = bsl["find_register_movements"]("ТестДок")["hint"]
            live_steps = _decl_search_fragments(hint)
            assert len(live_steps) == 1, hint
            hits = eval(compile(live_steps[0], "<hint:capped-decl>", "eval"), dict(bsl))  # noqa: S307
            assert hits[-1] == {"_truncated": True, "shown": 50}, hits[-1]
            candidates = [hit for hit in hits if not hit.get("_truncated")]
            assert len(candidates) == 50
            assert all(method in hit["text"] for hit in candidates)
            assert "_truncated" in hint and "досрочно" in hint
        finally:
            reader.close()


def test_posting_hint_keeps_a_module_delegate_with_a_platform_sounding_name():
    """Шум по ОДНОМУ имени метода теряет настоящих делегатов — и рождает ложное «движений не пишет».

    `ПроведениеДокументов.Записать(ЭтотОбъект, Отказ)` — боевой паттерн: общий модуль с экспортным
    методом `Записать`. Фильтр, выбрасывающий вызов по имени метода ДО разрешения получателя,
    удалял его; других вызовов нет — и hint заявлял «движений не пишет», хотя делегат ЕСТЬ и пишет.
    Шум обязан быть ПАРНЫМ (вид получателя, метод): у РАЗРЕШЁННОГО общего модуля никакое имя метода
    шумом не является; у переменной `Записать()` — почти наверняка платформенный метод набора."""
    body = (
        "Процедура ОбработкаПроведения(Отказ, РежимПроведения)\n"
        "    ПроведениеДокументов.Записать(ЭтотОбъект, Отказ);\n"
        "КонецПроцедуры\n"
    )
    delegate = (
        "Процедура Записать(Объект, Отказ) Экспорт\n"
        "    НаборЗаписей = РегистрыНакопления.ВзаиморасчетыСКонтрагентами.СоздатьНаборЗаписей();\n"
        "    НаборЗаписей.Записать();\n"
        "КонецПроцедуры\n"
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(
            tmpdir, {"ТестДок": body}, extra_common_modules={"ПроведениеДокументов": delegate}
        )
        try:
            res = bsl["find_register_movements"]("ТестДок")
            assert res.get("posting_handler_present") is True, res
            hint = res["hint"]
            assert "не пишет" not in hint, f"единственный делегат выброшен как шум: {hint}"
            assert "ДЕЛЕГАТ" in hint and "ПроведениеДокументов" in hint, hint

            # Маршрут доводится до конца: тело делегата читается по шагам из hint.
            code = _hint_steps(hint)
            ns = dict(bsl)
            exec(compile(code["2"], "<hint:2>", "exec"), ns)  # noqa: S102
            assert ns["d"]["total"] >= 1, ns["d"]
            dbody = eval(compile(code["3"], "<hint:3>", "eval"), ns)  # noqa: S307
            assert dbody and "СоздатьНаборЗаписей" in dbody, dbody
        finally:
            reader.close()


def test_posting_hint_names_a_register_written_via_record_manager():
    """`СоздатьМенеджерЗаписи()` — второй платформенный способ записи, симметричный набору.

    `Менеджер = РегистрыСведений.X.СоздатьМенеджерЗаписи(); Менеджер.Записать();` — регистр X
    назван прямо в обработчике, а `Менеджер.Записать()` (переменная + платформенный метод) —
    не делегат. Раньше оба вызова выбрасывались как шум, record_sets был пуст, и hint заявлял
    «движений не пишет» ровно там, где запись видна в двух строках кода."""
    body = (
        "Процедура ОбработкаПроведения(Отказ, РежимПроведения)\n"
        "    Менеджер = РегистрыСведений.СостоянияДокументов.СоздатьМенеджерЗаписи();\n"
        "    Менеджер.Записать();\n"
        "КонецПроцедуры\n"
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(tmpdir, {"ТестДок": body})
        try:
            hint = bsl["find_register_movements"]("ТестДок")["hint"]
            assert "РегистрыСведений.СостоянияДокументов" in hint, f"регистр менеджера записи не назван: {hint}"
            assert "не пишет" not in hint, hint
            assert "Менеджер.Записать" not in hint, f"платформенный вызов выдан за делегата: {hint}"

            profile_hint = (
                bsl["get_object_profile"]("ТестДок", sections=["registers"])["sections"]["registers"].get("hint") or ""
            )
            assert "MANAGER-ВЫЗОВ" not in profile_hint, (
                "штатная фабрика регистра не требует чтения ManagerModule и не должна становиться развилкой: "
                + profile_hint
            )
        finally:
            reader.close()


def test_posting_hint_keeps_an_exported_manager_module_delegate():
    """Платформенное пространство `Документы` не делает каждый manager-вызов шумом.

    Пользовательский экспорт из ManagerModule вызывается той же цепочкой
    `Документы.X.Метод()`, что и платформенный `НайтиПоНомеру`. Различаем их по живому
    экспортному объявлению и даём точный маршрут к телу менеджерного модуля.
    """
    body = (
        "Процедура ОбработкаПроведения(Отказ, РежимПроведения)\n"
        "    Документы.СлужебныйДок.СформироватьДвижения(ЭтотОбъект, Отказ);\n"
        "КонецПроцедуры\n"
    )
    manager_body = (
        "Процедура СформироватьДвижения(Объект, Отказ) Экспорт\n"
        "    Набор = РегистрыНакопления.МенеджерныйРегистр.СоздатьНаборЗаписей();\n"
        "    Набор.Записать();\n"
        "КонецПроцедуры\n"
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(
            tmpdir,
            {
                "ТестДок": body,
                "СлужебныйДок": "Процедура ПередЗаписью(Отказ)\nКонецПроцедуры\n",
            },
            manager_modules={"СлужебныйДок": manager_body},
        )
        try:
            hint = bsl["find_register_movements"]("ТестДок")["hint"]
            assert "ДЕЛЕГАТ:" in hint and "МОДУЛЯ МЕНЕДЖЕРА" in hint, hint
            assert "Документы.СлужебныйДок.СформироватьДвижения" in hint, hint
            manager_steps = [src for src in _hint_steps(hint).values() if "СформироватьДвижения" in src]
            assert manager_steps, hint
            manager_result = eval(compile(manager_steps[-1], "<hint:manager>", "eval"), dict(bsl))  # noqa: S307
            assert "МенеджерныйРегистр" in manager_result, manager_result
        finally:
            reader.close()


@pytest.mark.parametrize("manager_location", ["main", "adjacent_extension"])
def test_posting_hint_finds_manager_module_created_after_index_build(manager_location):
    """Новый ManagerModule отсутствует в снимке, но лежит по точному штатному CF-пути.

    Невозможность найти его в _index_state нельзя схлопывать с платформенным manager-вызовом:
    иначе единственный делегат исчезает и hint ложно пишет «движений не пишет».
    """
    body = (
        "Процедура ОбработкаПроведения(Отказ, РежимПроведения)\n"
        "    Документы.СлужебныйДок.СформироватьДвижения(ЭтотОбъект, Отказ);\n"
        "КонецПроцедуры\n"
    )
    manager_body = (
        "Процедура СформироватьДвижения(Объект, Отказ) Экспорт\n"
        "    Набор = РегистрыНакопления.СвежийМенеджерныйРегистр.СоздатьНаборЗаписей();\n"
        "    Набор.Записать();\n"
        "КонецПроцедуры\n"
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(
            tmpdir,
            {
                "ТестДок": body,
                "СлужебныйДок": "Процедура ПередЗаписью(Отказ)\nКонецПроцедуры\n",
            },
            # Реальная топология: исходники расширения — соседний с основной конфигурацией
            # каталог. Пустой модуль нужен только для регистрации этого known root.
            ext_docs={"ТехническийДокРасширения": ""} if manager_location == "adjacent_extension" else None,
        )
        try:
            if manager_location == "adjacent_extension":
                # Фиксируем lazy extension-index ДО появления модуля: иначе живой initial scan
                # сам добавит файл и тест не воспроизведёт stale-состояние.
                bsl["find_module"]("ОбщийМодульУчета")
                manager_root = os.path.join(tmpdir, "cfe", "ExtPosting")
            else:
                # Main SQLite уже собран внутри _make_posting_env.
                manager_root = os.path.join(tmpdir, "cf")
            manager_path = os.path.join(manager_root, "Documents", "СлужебныйДок", "Ext", "ManagerModule.bsl")
            os.makedirs(os.path.dirname(manager_path), exist_ok=True)
            with open(manager_path, "w", encoding="utf-8") as f:
                f.write(manager_body)

            hint = bsl["find_register_movements"]("ТестДок")["hint"]
            assert "ДЕЛЕГАТ:" in hint and "МОДУЛЯ МЕНЕДЖЕРА" in hint, hint
            assert "СвежийМенеджерныйРегистр" in eval(
                compile(
                    next(src for src in _hint_steps(hint).values() if "СформироватьДвижения" in src),
                    "<hint:stale-manager>",
                    "eval",
                ),
                dict(bsl),
            )
            assert "судя по коду, движений он не пишет" not in hint, hint
        finally:
            reader.close()


def test_profile_manager_call_does_not_open_manager_modules():
    """Compact registers-профиль читает только ObjectModule целевого документа.

    Manager-вызов при этом не пропадает как platform noise: без live-проверки он остаётся
    честной развилкой, которую полный find_register_movements сможет разрешить.
    """
    reads: list[str] = []
    body = (
        "Процедура ОбработкаПроведения(Отказ, РежимПроведения)\n"
        "    Документы.СлужебныйДок.СформироватьДвижения(ЭтотОбъект, Отказ);\n"
        "КонецПроцедуры\n"
    )
    manager_body = "Процедура СформироватьДвижения(Объект, Отказ) Экспорт\nКонецПроцедуры\n"
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(
            tmpdir,
            {
                "ТестДок": body,
                "СлужебныйДок": "Процедура ПередЗаписью(Отказ)\nКонецПроцедуры\n",
            },
            manager_modules={"СлужебныйДок": manager_body},
            reads=reads,
        )
        try:
            reads.clear()
            sec = bsl["get_object_profile"]("ТестДок", sections=["registers"])["sections"]["registers"]
            hint = sec.get("hint") or ""
            bsl_reads = [p.replace("\\", "/") for p in reads if p.endswith(".bsl")]
            assert bsl_reads and all("Documents/ТестДок/" in p for p in bsl_reads), bsl_reads
            assert not any(p.endswith("ManagerModule.bsl") for p in bsl_reads), bsl_reads
            assert "MANAGER-ВЫЗОВ" in hint and "find_register_movements" in hint, hint
            assert "судя по коду, движений он не пишет" not in hint, hint
        finally:
            reader.close()


def test_posting_hint_does_not_swallow_this_object_local_call():
    """`ЭтотОбъект.Метод()` — вызов СВОЕГО метода, а не шум.

    Получатель `ЭтотОбъект` лежал в _RECEIVER_NOISE, и вызов выбрасывался целиком — вместе с
    локальным методом, который пишет движения. Метод, объявленный в ЭТОМ модуле, обязан уходить
    в ЛОКАЛЬНЫЙ маршрут (read_procedure); платформенные методы объекта (Записать/Проверить) —
    единственное, что тут законно молчит."""
    body = (
        "Процедура ОбработкаПроведения(Отказ, РежимПроведения)\n"
        "    ЭтотОбъект.ЗаписатьДвиженияНабором(Отказ);\n"
        "КонецПроцедуры\n"
        "\n"
        "Процедура ЗаписатьДвиженияНабором(Отказ)\n"
        "    НаборЗаписей = РегистрыНакопления.ВзаиморасчетыСКонтрагентами.СоздатьНаборЗаписей();\n"
        "    НаборЗаписей.Записать();\n"
        "КонецПроцедуры\n"
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(tmpdir, {"ТестДок": body})
        try:
            hint = bsl["find_register_movements"]("ТестДок")["hint"]
            assert "не пишет" not in hint, f"локальный метод за ЭтотОбъект. потерян: {hint}"
            assert "ЛОКАЛЬНЫЙ ВЫЗОВ" in hint and "ЗаписатьДвиженияНабором" in hint, hint
            code = _hint_steps(hint)
            ns = dict(bsl)
            local_body = eval(compile(code["2"], "<hint:2>", "eval"), ns)  # noqa: S307
            assert local_body and "СоздатьНаборЗаписей" in local_body, local_body
        finally:
            reader.close()

    # Контроль: ЧИСТО платформенный ЭтотОбъект.Записать() не рождает ни делегата, ни локального вызова.
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(
            tmpdir,
            {
                "ТестДок": "Процедура ОбработкаПроведения(Отказ, РежимПроведения)\n    ЭтотОбъект.Записать();\nКонецПроцедуры\n"
            },
        )
        try:
            hint = bsl["find_register_movements"]("ТестДок")["hint"]
            # 'ДЕЛЕГАТ:' — метка НАЙДЕННОГО делегата; слово «ДЕЛЕГАТА» законно живёт в общем хвосте.
            assert "ЛОКАЛЬНЫЙ ВЫЗОВ" not in hint and "ДЕЛЕГАТ:" not in hint, hint
        finally:
            reader.close()


def test_posting_hint_without_index_resolves_the_attribute_via_live_xml():
    """БЕЗ индекса реквизит снова принимался за одноимённый общий модуль — исходный дефект целиком.

    idx_reader is None → индекс реквизитов недоступен; маркеров переменной нет; общий модуль-
    однофамилец найден в _index_state → анализатор объявлял получателя ОБЩИМ МОДУЛЕМ, а
    find_definition(..., 'ОбщийМодуль.X') без индекса читает модуль ЖИВЬЁМ — и агент получал чужое
    тело как «разрешённого сервером» делегата.

    Маршрут хелпера и так живой (postability читается из XML) — реквизиты обязаны браться оттуда же."""
    body = (
        "Процедура ОбработкаПроведения(Отказ, РежимПроведения)\n"
        f"    {_VAR_RECEIVER}.{_VAR_DELEGATE_METHOD}(ЭтотОбъект);\n"
        "КонецПроцедуры\n"
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_posting_env(
            tmpdir,
            {"ТестДок": body},
            attribute_receiver=True,
            variable_receiver_delegate=True,  # одноимённый общий модуль существует
            no_index=True,
        )
        res = bsl["find_register_movements"]("ТестДок")
        assert res.get("posting_handler_present") is True, res
        hint = res["hint"]
        assert "РЕКВИЗИТ" in hint, f"без индекса реквизит не разрешён живым XML: {hint}"
        assert "ЛОВУШКА" in hint, hint
        forbidden = f"find_definition('{_VAR_DELEGATE_METHOD}', 'ОбщийМодуль.{_VAR_RECEIVER}')"
        offending = [f"({k}) {v}" for k, v in _hint_steps(hint).items() if forbidden in v]
        assert not offending, f"ШАГ снова ведет в ЧУЖОЙ общий модуль: {offending}"


def test_profile_hint_gives_a_fork_not_a_fact_when_attribute_info_is_unavailable(monkeypatch):
    """Профиль live-XML не читает (контракт), реквизиты берёт из индекса. Если индекс реквизитов
    НЕДОСТУПЕН (таблицы нет / ридер упал) — «получатель = ОБЩИЙ МОДУЛЬ» превращается из факта в
    ДОГАДКУ, и заявлять её нельзя: реквизит с тем же именем затенил бы модуль. Честный ответ —
    развилка с исполнимой проверкой метаданных (get_object_full_structure работает у агента и
    живьём), а не удобный «факт»."""
    body = (
        "Процедура ОбработкаПроведения(Отказ, РежимПроведения)\n"
        f"    {_VAR_RECEIVER}.{_VAR_DELEGATE_METHOD}(ЭтотОбъект);\n"
        "КонецПроцедуры\n"
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(
            tmpdir, {"ТестДок": body}, attribute_receiver=True, variable_receiver_delegate=True
        )
        try:
            monkeypatch.setattr(reader, "get_object_attributes", lambda *a, **k: None)
            sec = bsl["get_object_profile"]("ТестДок", sections=["registers"])["sections"]["registers"]
            assert sec["summary"].get("posting_handler_present") is True, sec
            hint = sec.get("hint") or ""
            assert "НЕ ПРОВЕРЕНЫ" in hint, f"недоступные реквизиты выданы за проверенные: {hint}"
            assert "ДЕЛЕГАТ:" not in hint, f"догадка подана как факт «ОБЩИЙ МОДУЛЬ»: {hint}"
            assert "get_object_full_structure" in hint, f"развилка не даёт исполнимой проверки: {hint}"
            for label, src in _hint_steps(hint).items():
                compile(src, f"<hint:{label}>", "exec")
        finally:
            reader.close()


def test_posting_hint_record_set_does_not_cancel_a_coexisting_delegate():
    """Набор записей НЕ отменяет делегата: обработчик законно пишет один регистр набором и
    делегирует остальные движения. Фраза «делегата нет, идти дальше некуда» при этом — ложь,
    противоречащая соседнему абзацу того же hint (который делегата показывает)."""
    body = (
        "Процедура ОбработкаПроведения(Отказ, РежимПроведения)\n"
        "    Набор = РегистрыСведений.СостоянияДокументов.СоздатьНаборЗаписей();\n"
        "    Набор.Записать();\n"
        f"    {_DELEGATE_MODULE}.{_DELEGATE}(ЭтотОбъект, Отказ);\n"
        "КонецПроцедуры\n"
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(tmpdir, {"ТестДок": body})
        try:
            hint = bsl["find_register_movements"]("ТестДок")["hint"]
            assert "РегистрыСведений.СостоянияДокументов" in hint, hint
            assert "ДЕЛЕГАТ" in hint and _DELEGATE_MODULE in hint, f"делегат рядом с набором потерян: {hint}"
            assert "идти дальше некуда" not in hint, f"hint сам себе противоречит: {hint}"
        finally:
            reader.close()

    # Контроль: когда набор — ЕДИНСТВЕННОЕ, что есть в теле, «идти дальше некуда» — правда, и она остаётся.
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(tmpdir, {"ТестДок": _HANDLER_WRITES_SETS})
        try:
            assert "идти дальше некуда" in bsl["find_register_movements"]("ТестДок")["hint"]
        finally:
            reader.close()


def test_posting_hint_reports_a_noise_named_method_on_an_untraced_variable():
    """Статически отличить `МенеджерЗаписи.Записать()` от пользовательского `Сервис.Записать()`
    по паре (переменная, имя метода) НЕВОЗМОЖНО — и выбрасывать вызов по этой паре значит снова
    выдавать эвристику за отрицательный факт («движений не пишет»).

    Шум обоснован только ИСТОЧНИКОМ получателя: если переменная присвоена из платформенной
    фабрики (`Регистры<Тип>.X.Создать(НаборЗаписей|МенеджерЗаписи)` / `Новый ...`) — её
    `Записать()` платформенный, а регистр уже назван. НЕПРОСЛЕЖЕННЫЙ источник -> вызов ОБЯЗАН
    быть показан: он может оказаться единственным настоящим делегатом."""
    body = (
        "Процедура ОбработкаПроведения(Отказ, РежимПроведения)\n"
        "    Сервис = ПолучитьСервисПроведения();\n"
        "    Сервис.Записать(ЭтотОбъект);\n"
        "КонецПроцедуры\n"
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(tmpdir, {"ТестДок": body})
        try:
            hint = bsl["find_register_movements"]("ТестДок")["hint"]
            assert "не пишет" not in hint, f"непрослеженный вызов выброшен как шум: {hint}"
            assert "Сервис.Записать" in hint and "ПЕРЕМЕННАЯ" in hint, hint
            # Имя метода платформенное — hint обязан оговорить двусмысленность, а не молчать.
            assert "платформенн" in hint.lower(), f"hint не предупреждает о платформенном имени метода: {hint}"
        finally:
            reader.close()

    # КОНТРОЛЬ обоснованного шума: источник ПРОСЛЕЖЕН до платформенной фабрики — вызов законно
    # скрыт (регистр уже назван). Имя переменной произвольное: старый фильтр резал по имени
    # МЕТОДА у любой переменной, новый обязан резать только по ИСТОЧНИКУ.
    traced = (
        "Процедура ОбработкаПроведения(Отказ, РежимПроведения)\n"
        "    Набор = РегистрыНакопления.ТестовыйРегистр.СоздатьНаборЗаписей();\n"
        "    Набор.Записать();\n"
        "КонецПроцедуры\n"
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(tmpdir, {"ТестДок": traced})
        try:
            hint = bsl["find_register_movements"]("ТестДок")["hint"]
            assert "РегистрыНакопления.ТестовыйРегистр" in hint, hint
            assert "Набор.Записать" not in hint, f"прослеженный платформенный вызов показан как делегат: {hint}"
        finally:
            reader.close()


def test_posting_hint_object_named_variable_is_not_noise():
    """`Объект` в ObjectModule — НЕ предопределённое имя (это форменная сущность), а обычная
    переменная. Безусловный receiver-noise выбрасывал `Объект.ОтразитьДвижения()` ещё ДО
    _shadowing — вместе с единственным делегатом."""
    body = (
        "Процедура ОбработкаПроведения(Отказ, РежимПроведения)\n"
        "    Объект = ПолучитьСервис();\n"
        "    Объект.ОтразитьДвижения(ЭтотОбъект);\n"
        "КонецПроцедуры\n"
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(tmpdir, {"ТестДок": body})
        try:
            hint = bsl["find_register_movements"]("ТестДок")["hint"]
            assert "не пишет" not in hint, f"вызов на переменной 'Объект' выброшен как шум: {hint}"
            assert "ОтразитьДвижения" in hint and "ПЕРЕМЕННАЯ" in hint, hint
        finally:
            reader.close()


def test_posting_hint_helper_heals_a_stale_attribute_index_via_live_xml():
    """Успешный ответ индекса — НЕ доказательство полноты (контракт HELPERS.md: `index_used` —
    об источнике, не о полноте). Реквизит, добавленный в XML ПОСЛЕ сборки индекса, из
    get_object_attributes не виден; прежний код ставил attrs_known=True и объявлял получателя
    ОБЩИМ МОДУЛЕМ — агент снова уходил в чужое тело. Маршрут хелпера живой, поэтому реквизиты
    он обязан сверять по живому XML ВСЕГДА, а не только когда индекс отказал."""
    body = (
        "Процедура ОбработкаПроведения(Отказ, РежимПроведения)\n"
        f"    {_VAR_RECEIVER}.{_VAR_DELEGATE_METHOD}(ЭтотОбъект);\n"
        "КонецПроцедуры\n"
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        # Индекс строится по XML БЕЗ реквизита...
        bsl, reader = _make_posting_env(tmpdir, {"ТестДок": body}, variable_receiver_delegate=True)
        try:
            # ...а ПОСЛЕ сборки реквизит появляется в живом XML (типовая правка конфигурации).
            doc_xml = os.path.join(tmpdir, "cf", "Documents", "ТестДок.xml")
            with open(doc_xml, "w", encoding="utf-8") as f:
                f.write(
                    '<?xml version="1.0" encoding="UTF-8"?>\n'
                    '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">\n'
                    '  <Document uuid="u-ТестДок">\n'
                    "    <Properties><Name>ТестДок</Name></Properties>\n"
                    f"  <Attribute><Properties><Name>{_VAR_RECEIVER}</Name></Properties></Attribute>\n"
                    "  </Document>\n"
                    "</MetaDataObject>\n"
                )
            # Предусловие stale-состояния: индекс реквизита НЕ знает.
            rows = reader.get_object_attributes(object_name="ТестДок", category="Documents") or []
            assert not any((r.get("attr_name") or "").casefold() == _VAR_RECEIVER.casefold() for r in rows), rows

            hint = bsl["find_register_movements"]("ТестДок")["hint"]
            assert "РЕКВИЗИТ" in hint, f"устаревший индекс выдан за полное знание о реквизитах: {hint}"
            forbidden = f"find_definition('{_VAR_DELEGATE_METHOD}', 'ОбщийМодуль.{_VAR_RECEIVER}')"
            offending = [f"({k}) {v}" for k, v in _hint_steps(hint).items() if forbidden in v]
            assert not offending, f"ШАГ снова ведет в ЧУЖОЙ общий модуль: {offending}"
        finally:
            reader.close()


def test_profile_hint_does_not_treat_a_stale_deleted_attribute_as_live_proof():
    """Индекс может ОПЕРЕЖАТЬ XML: удаленный реквизит остается в снимке до пересборки.

    Положительный ответ get_object_full_structure(index_used=True) поэтому не доказывает, что
    получатель в живом коде — реквизит. Профиль должен оставить развилку, а live-хелпер —
    разрешить одноименный общий модуль после удаления реквизита из XML.
    """
    body = (
        "Процедура ОбработкаПроведения(Отказ, РежимПроведения)\n"
        f"    {_VAR_RECEIVER}.{_VAR_DELEGATE_METHOD}(ЭтотОбъект);\n"
        "КонецПроцедуры\n"
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(
            tmpdir,
            {"ТестДок": body},
            variable_receiver_delegate=True,
            attribute_receiver=True,
        )
        try:
            # Индекс уже запомнил реквизит; теперь удаляем его только из живого XML.
            doc_xml = os.path.join(tmpdir, "cf", "Documents", "ТестДок.xml")
            with open(doc_xml, "w", encoding="utf-8") as f:
                f.write(
                    '<?xml version="1.0" encoding="UTF-8"?>\n'
                    '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">\n'
                    '  <Document uuid="u-ТестДок">\n'
                    "    <Properties><Name>ТестДок</Name></Properties>\n"
                    "  </Document>\n"
                    "</MetaDataObject>\n"
                )

            profile_hint = (
                bsl["get_object_profile"]("ТестДок", sections=["registers"])["sections"]["registers"].get("hint") or ""
            )
            snapshot_steps = [src for src in _hint_steps(profile_hint).values() if "get_object_full_structure" in src]
            assert len(snapshot_steps) == 1, profile_hint
            ns = dict(bsl)
            exec(compile(snapshot_steps[0], "<hint:stale-positive>", "exec"), ns)  # noqa: S102
            stale_attrs = ns["s"].get("attributes") or []
            assert any(
                (a.get("name") or a.get("attr_name") or "").casefold() == _VAR_RECEIVER.casefold() for a in stale_attrs
            ), ns["s"]
            assert "и наличие, и отсутствие" in profile_hint, profile_hint
            assert "-> это РЕКВИЗИТ" not in profile_hint, profile_hint

            live_hint = bsl["find_register_movements"]("ТестДок")["hint"]
            assert "ДЕЛЕГАТ:" in live_hint and "ОБЩИЙ МОДУЛЬ" in live_hint, live_hint
        finally:
            reader.close()


def test_posting_hint_sees_an_attribute_added_by_a_cfe_extension():
    """Реквизит, ДОБАВЛЕННЫЙ РАСШИРЕНИЕМ, не виден ни в индексе (он main-only), ни в живом XML
    основной конфигурации — «не реквизит» по этим двум источникам не доказано. Хелпер обязан
    сверяться и с XML расширений (они уже доступны через ext-aware чтение)."""
    body = (
        "Процедура ОбработкаПроведения(Отказ, РежимПроведения)\n"
        f"    {_VAR_RECEIVER}.{_VAR_DELEGATE_METHOD}(ЭтотОбъект);\n"
        "КонецПроцедуры\n"
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(
            tmpdir,
            {"ТестДок": body},
            variable_receiver_delegate=True,  # одноимённый общий модуль существует
            ext_attribute_receiver=True,  # а реквизит добавлен ТОЛЬКО расширением
        )
        try:
            hint = bsl["find_register_movements"]("ТестДок")["hint"]
            assert "РЕКВИЗИТ" in hint, f"реквизит из расширения не увиден: {hint}"
            forbidden = f"find_definition('{_VAR_DELEGATE_METHOD}', 'ОбщийМодуль.{_VAR_RECEIVER}')"
            offending = [f"({k}) {v}" for k, v in _hint_steps(hint).items() if forbidden in v]
            assert not offending, f"ШАГ ведет в ЧУЖОЙ общий модуль мимо реквизита расширения: {offending}"
        finally:
            reader.close()


def test_module_fact_requires_a_live_attribute_check():
    """ФАКТ «получатель — общий модуль» разрешён только при LIVE-проверке реквизитов.

    Прежняя редакция давала профилю тот же факт с оговоркой «индекс может отставать» — но
    оговорка не чинит классификацию: на stale-индексе с реквизитом, добавленным после сборки,
    сам факт уже ложен, и агент уходит в чужое тело С ПРЕДУПРЕЖДЕНИЕМ в кармане. Успешный
    SQL-запрос не доказывает полноту снимка, поэтому index-источник обязан давать РАЗВИЛКУ
    (module_unverified), как и отсутствие источника. Это структурный потолок: сильное
    утверждение ⇔ сильный источник, никаких «фактов со звёздочкой»."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(tmpdir, {"ТестДок": _DELEGATED})
        try:
            # Хелпер (live-источник, включая расширения) — факт, и источник назван.
            helper_hint = bsl["find_register_movements"]("ТестДок")["hint"]
            assert "ДЕЛЕГАТ:" in helper_hint, helper_hint
            assert "ЖИВОМУ XML" in helper_hint, f"хелпер не называет живой источник проверки: {helper_hint}"

            # Профиль (index-источник) — РАЗВИЛКА, а не факт: причина названа, шаг исполним.
            sec = bsl["get_object_profile"]("ТестДок", sections=["registers"])["sections"]["registers"]
            profile_hint = sec.get("hint") or ""
            assert "ДЕЛЕГАТ:" not in profile_hint, f"index-источник снова выдан за факт: {profile_hint}"
            assert "НЕ ПРОВЕРЕНЫ" in profile_hint, profile_hint
            assert "ПО ИНДЕКСУ" in profile_hint, f"развилка не называет причину (индекс): {profile_hint}"
            assert "отстава" in profile_hint.lower(), profile_hint
            assert "get_object_full_structure" in profile_hint, f"развилка без исполнимой проверки: {profile_hint}"
            for label, src in _hint_steps(profile_hint).items():
                compile(src, f"<hint:{label}>", "exec")
        finally:
            reader.close()


def test_module_unverified_fork_does_not_conclude_module_from_absence():
    """Развилка не имеет права выводить «нет среди реквизитов → это ОБЩИЙ МОДУЛЬ» из проверки,
    которая сама не live: get_object_full_structure при живом индексе читает ЕГО ЖЕ (его
    собственный контракт: index_used — об источнике, не о полноте) — на stale-снимке рекомендация
    замыкала круг и снова отправляла агента в чужое тело, только теперь двумя шагами.

    Snapshot может и отставать, и опережать live XML, поэтому ни наличие, ни отсутствие
    реквизита не классифицирует получателя. Ветка обязана вести в git_search по дереву
    (он найдёт объявление метода И в общем модуле, если получатель был им — безопасно в
    обоих мирах), а профиль дополнительно — в
    find_register_movements: единственную перепроверку, сверяющую реквизиты живьём."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(tmpdir, {"ТестДок": _DELEGATED})
        try:
            sec = bsl["get_object_profile"]("ТестДок", sections=["registers"])["sections"]["registers"]
            hint = sec.get("hint") or ""
            assert "нет -> это ОБЩИЙ МОДУЛЬ" not in hint, f"отсутствие снова выведено в факт модуля: {hint}"
            assert "НЕ ДОКАЗЫВАЕТ" in hint, f"асимметрия вывода не названа: {hint}"
            assert "find_register_movements('ТестДок')" in hint, f"профиль не даёт live-перепроверку: {hint}"
            for label, src in _hint_steps(hint).items():
                compile(src, f"<hint:{label}>", "exec")
        finally:
            reader.close()

    # END-TO-END на stale-состоянии: реквизит добавлен в XML ПОСЛЕ сборки индекса. Профиль честно
    # даёт развилку, а рекомендованная им live-перепроверка ДОВОДИТ ДО ПРАВДЫ (РЕКВИЗИТ) — маршрут
    # из развилки больше не замыкается на тот же stale-источник.
    body = (
        "Процедура ОбработкаПроведения(Отказ, РежимПроведения)\n"
        f"    {_VAR_RECEIVER}.{_VAR_DELEGATE_METHOD}(ЭтотОбъект);\n"
        "КонецПроцедуры\n"
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(tmpdir, {"ТестДок": body}, variable_receiver_delegate=True)
        try:
            doc_xml = os.path.join(tmpdir, "cf", "Documents", "ТестДок.xml")
            with open(doc_xml, "w", encoding="utf-8") as f:
                f.write(
                    '<?xml version="1.0" encoding="UTF-8"?>\n'
                    '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">\n'
                    '  <Document uuid="u-ТестДок">\n'
                    "    <Properties><Name>ТестДок</Name></Properties>\n"
                    f"  <Attribute><Properties><Name>{_VAR_RECEIVER}</Name></Properties></Attribute>\n"
                    "  </Document>\n"
                    "</MetaDataObject>\n"
                )
            profile_hint = (
                bsl["get_object_profile"]("ТестДок", sections=["registers"])["sections"]["registers"].get("hint") or ""
            )
            assert "ДЕЛЕГАТ:" not in profile_hint, profile_hint  # развилка, не факт
            # Следуем рекомендации развилки — и она разрешает получателя ПРАВИЛЬНО.
            helper_hint = bsl["find_register_movements"]("ТестДок")["hint"]
            assert "РЕКВИЗИТ" in helper_hint, f"live-перепроверка не довела до правды: {helper_hint}"
        finally:
            reader.close()


def test_posting_hint_unreadable_extension_metadata_blocks_the_module_fact():
    """Полнота live-проверки обязана ОТСЛЕЖИВАТЬСЯ: нечитаемые метаданные расширения — это
    «проверил не всё», а не «проверил». Раньше parse-ошибка глоталась (`except: continue`),
    attrs_source оставался 'live', и факт «общий модуль (сверено... включая XML расширений)»
    утверждал проверку, которой НЕ БЫЛО — реквизит, добавленный расширением, терялся, и агент
    получал уверенный маршрут в чужое тело."""
    body = (
        "Процедура ОбработкаПроведения(Отказ, РежимПроведения)\n"
        f"    {_VAR_RECEIVER}.{_VAR_DELEGATE_METHOD}(ЭтотОбъект);\n"
        "КонецПроцедуры\n"
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(
            tmpdir,
            {"ТестДок": body},
            variable_receiver_delegate=True,
            ext_attribute_receiver="corrupt",
        )
        try:
            hint = bsl["find_register_movements"]("ТестДок")["hint"]
            assert "ДЕЛЕГАТ:" not in hint, f"непроверенное расширение не помешало факту: {hint}"
            assert "НЕ ПРОВЕРЕНЫ" in hint, hint
            assert "расширен" in hint.lower(), f"причина (метаданные расширения) не названа: {hint}"
            forbidden = f"find_definition('{_VAR_DELEGATE_METHOD}', 'ОбщийМодуль.{_VAR_RECEIVER}')"
            offending = [f"({k}) {v}" for k, v in _hint_steps(hint).items() if forbidden in v]
            assert not offending, f"ШАГ ведет в возможно чужой модуль при непроверенном расширении: {offending}"
        finally:
            reader.close()


def test_cross_drive_extension_locator_failure_blocks_the_module_fact(monkeypatch):
    """`os.path.relpath` между разными дисками (Windows: база на D:, расширение на E:) бросает
    ValueError — и локатор метаданных расширения МОЛЧА выпадал, НЕ выставляя
    _ext_metadata_scan_failed. Проверка «включая XML расширений» при этом считалась ПОЛНОЙ:
    реквизит из расширения терялся, и получатель снова объявлялся общим модулем.

    «Не смогли выразить путь» — то же самое «не смогли посмотреть», что и отказ перечисления:
    полнота обязана ломаться. Кросс-дисковую топологию воспроизводим фейком relpath, падающим
    ровно для путей расширения — контракт extension_paths абсолютные пути с другого диска
    допускает."""
    body = (
        "Процедура ОбработкаПроведения(Отказ, РежимПроведения)\n"
        f"    {_VAR_RECEIVER}.{_VAR_DELEGATE_METHOD}(ЭтотОбъект);\n"
        "КонецПроцедуры\n"
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(
            tmpdir,
            {"ТестДок": body},
            variable_receiver_delegate=True,
            ext_attribute_receiver=True,  # валидные метаданные расширения С реквизитом
        )
        try:
            ext_root = os.path.realpath(os.path.join(tmpdir, "cfe"))
            real_relpath = os.path.relpath

            def _cross_drive_relpath(path, start=os.curdir):
                if os.path.realpath(str(path)).startswith(ext_root):
                    raise ValueError("path is on mount 'E:', start on mount 'D:'")
                return real_relpath(path, start)

            # Патч ДО первого вызова хелпера: _ensure_index ленив, загрузчик расширений ещё не бегал.
            monkeypatch.setattr(os.path, "relpath", _cross_drive_relpath)
            hint = bsl["find_register_movements"]("ТестДок")["hint"]
            assert "ДЕЛЕГАТ:" not in hint, f"невыразимый локатор не сломал полноту проверки: {hint}"
            assert "НЕ ПРОВЕРЕНЫ" in hint, hint
            assert "расширен" in hint.lower(), f"причина (метаданные расширений) не названа: {hint}"
        finally:
            reader.close()


def test_missing_extension_root_blocks_the_module_fact():
    """Сконфигурированный root расширения, который исчез до ленивого обхода, — это неполная
    проверка, а не пустое расширение. Иначе live-классификатор уверенно назовет получателя общим
    модулем, хотя недоступное расширение могло добавить документу одноименный реквизит."""
    body = (
        "Процедура ОбработкаПроведения(Отказ, РежимПроведения)\n"
        f"    {_VAR_RECEIVER}.{_VAR_DELEGATE_METHOD}(ЭтотОбъект);\n"
        "КонецПроцедуры\n"
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        missing_root = os.path.join(tmpdir, "cfe-that-disappeared")
        bsl, reader = _make_posting_env(
            tmpdir,
            {"ТестДок": body},
            variable_receiver_delegate=True,
            extra_extension_paths=[missing_root],
        )
        try:
            hint = bsl["find_register_movements"]("ТестДок")["hint"]
            assert "ДЕЛЕГАТ:" not in hint, f"недоступный root не сломал полноту live-проверки: {hint}"
            assert "НЕ ПРОВЕРЕНЫ" in hint, hint
            assert "расширен" in hint.lower(), f"причина неполноты не названа: {hint}"
        finally:
            reader.close()


def test_fork_declaration_search_covers_functions_and_is_executable():
    """Развилка ищет и процедуры, и функции в русском/английском BSL.

    Тест исполняет live Python-regex маршрут на git-дереве: он не зависит от POSIX ERE
    case-folding и находит функцию по всему известному BSL-каталогу."""
    delegate_fn = f"Функция {_VAR_DELEGATE_METHOD}(Объект) Экспорт\n    Возврат Истина;\nКонецФункции\n"
    body = (
        "Процедура ОбработкаПроведения(Отказ, РежимПроведения)\n"
        f"    {_VAR_RECEIVER}.{_VAR_DELEGATE_METHOD}(ЭтотОбъект);\n"
        "КонецПроцедуры\n"
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(
            tmpdir,
            {"ТестДок": body},
            extra_common_modules={_VAR_RECEIVER: delegate_fn},
            ext_attribute_receiver="corrupt",  # live_partial -> развилка module_unverified
            git=True,
        )
        try:
            hint = bsl["find_register_movements"]("ТестДок")["hint"]
            assert "ДЕЛЕГАТ:" not in hint, hint  # предусловие: это развилка
            # Live-маршрут исполняется дословно и находит Function с любым регистром/
            # пробельным разделителем, не полагаясь на кириллический git grep -iE.
            live_steps = _decl_search_fragments(hint)
            assert len(live_steps) == 1, hint
            fn_hits = eval(compile(live_steps[0], "<hint:decl-search>", "eval"), dict(bsl))  # noqa: S307
            assert any("CommonModules" in hit.get("file", "") for hit in fn_hits), fn_hits
        finally:
            reader.close()

    # Охват ОСТАЛЬНЫХ веток тем же инвариантом: shadow_risk и step-3 факта (helper),
    # module_unverified (профиль).
    shadow_body = (
        "Процедура ОбработкаПроведения(Отказ, РежимПроведения)\n"
        f"    Если {_VAR_RECEIVER} = Неопределено Тогда\n"
        "        Возврат;\n"
        "    КонецЕсли;\n"
        f"    {_VAR_RECEIVER}.{_VAR_DELEGATE_METHOD}(ЭтотОбъект);\n"
        "КонецПроцедуры\n"
    )
    for case, body2, kwargs, route in (
        ("shadow_risk", shadow_body, {"extra_common_modules": {_VAR_RECEIVER: delegate_fn}}, "helper"),
        ("fact_step3", _DELEGATED, {}, "helper"),
        ("profile_fork", _DELEGATED, {}, "profile"),
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            bsl, reader = _make_posting_env(tmpdir, {"ТестДок": body2}, **kwargs)
            try:
                if route == "profile":
                    hint = (
                        bsl["get_object_profile"]("ТестДок", sections=["registers"])["sections"]["registers"].get(
                            "hint"
                        )
                        or ""
                    )
                else:
                    hint = bsl["find_register_movements"]("ТестДок")["hint"]
                assert "(Процедура|Функция|Procedure|Function)[[:space:]]+" not in hint
            finally:
                reader.close()


def test_fork_declaration_search_fragments_are_executable_and_cover_english():
    """Каждый live declaration-search из hint исполняется и покрывает English/case/spacing.

    Объявление намеренно lowercase и с тремя пробелами. Маршрут обязан найти его точным
    Python-regex по полному BSL-каталогу; POSIX ERE `git grep -iE` для кириллицы ненадёжен."""
    delegate_en = f"function   {_VAR_DELEGATE_METHOD}(Объект) export\n    Возврат Истина;\nКонецФункции\n"
    body = (
        "Процедура ОбработкаПроведения(Отказ, РежимПроведения)\n"
        f"    {_VAR_RECEIVER}.{_VAR_DELEGATE_METHOD}(ЭтотОбъект);\n"
        "КонецПроцедуры\n"
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(
            tmpdir,
            {"ТестДок": body},
            extra_common_modules={_VAR_RECEIVER: delegate_en},
            ext_attribute_receiver="corrupt",  # live_partial -> развилка module_unverified
            git=True,
        )
        try:
            hint = bsl["find_register_movements"]("ТестДок")["hint"]
            fragments = _decl_search_fragments(hint)
            assert fragments, f"в развилке нет live-поиска объявления: {hint}"
            assert all("(?i)" in frag and "max_files=" in frag for frag in fragments)
            results = [eval(compile(frag, "<hint:safe_grep>", "eval"), dict(bsl)) for frag in fragments]  # noqa: S307

            found = [
                hit
                for res in results
                if isinstance(res, list)
                for hit in res
                if isinstance(hit, dict) and "CommonModules" in str(hit.get("file", ""))
            ]
            assert found, f"live-поиск не нашел Function-делегата: {fragments}"
        finally:
            reader.close()

    # Остальные ветки (shadow_risk / step-3 факта / профильная развилка / вызов без точки):
    # канонический regex-вид обязан быть везде, где hint советует искать объявление.
    delegate_ru = f"Функция {_VAR_DELEGATE_METHOD}(Объект) Экспорт\n    Возврат Истина;\nКонецФункции\n"
    shadow_body = (
        "Процедура ОбработкаПроведения(Отказ, РежимПроведения)\n"
        f"    Если {_VAR_RECEIVER} = Неопределено Тогда\n"
        "        Возврат;\n"
        "    КонецЕсли;\n"
        f"    {_VAR_RECEIVER}.{_VAR_DELEGATE_METHOD}(ЭтотОбъект);\n"
        "КонецПроцедуры\n"
    )
    global_body = (
        "Процедура ОбработкаПроведения(Отказ, РежимПроведения)\n    ЗаписатьДвиженияГлобально(Отказ);\nКонецПроцедуры\n"
    )
    for case, body2, kwargs, route in (
        ("shadow_risk", shadow_body, {"extra_common_modules": {_VAR_RECEIVER: delegate_ru}}, "helper"),
        ("fact_step3", _DELEGATED, {}, "helper"),
        ("profile_fork", _DELEGATED, {}, "profile"),
        ("global_call", global_body, {}, "helper"),
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            bsl, reader = _make_posting_env(tmpdir, {"ТестДок": body2}, **kwargs)
            try:
                if route == "profile":
                    hint = (
                        bsl["get_object_profile"]("ТестДок", sections=["registers"])["sections"]["registers"].get(
                            "hint"
                        )
                        or ""
                    )
                else:
                    hint = bsl["find_register_movements"]("ТестДок")["hint"]
                # Эти стенды НЕ под git: совет незарегистрированного git_search запрещён,
                # маршрут обязан строиться из find_definition/safe_grep. Полный live-вариант
                # safe_grep на git-стенде закреплён первой частью этого теста.
                assert "git_search(" not in hint, f"({case}) hint советует незарегистрированный git_search: {hint}"
                assert "find_definition(" in hint or "safe_grep(" in hint, (
                    f"({case}) нет исполнимого маршрута поиска объявления: {hint}"
                )
            finally:
                reader.close()

    # Рецепт «проведение» ведёт к сгенерированному runtime-hint, где известен полный размер каталога.
    from rlm_tools_bsl.bsl_knowledge import _get_topic_recipe

    blob = " ".join(_get_topic_recipe("проведение", format="full")["steps"])
    assert "' / '" not in blob, f"рецепт советует делить строки: {blob}"
    assert "result['hint']" in blob and "live safe_grep" in blob, blob


def test_posting_hint_platform_trace_is_scoped_to_the_handler_and_order_safe():
    """`_platform_sourced` обязан судить по ТЕЛУ ОБРАБОТЧИКА, и только когда возразить нечем.

    Поиск «любого платформенного присваивания по всему модулю» давал два ложных подавления:
    (а) присваивание в ДРУГОЙ процедуре — там локальная переменная другой области видимости —
    навсегда маркировало имя как «платформенное» и в обработчике; (б) переприсваивание внутри
    обработчика (платформа -> пользовательское) игнорировалось: ранний платформенный источник
    побеждал, хотя в точке вызова значение уже другое. Оба случая кончались ложным «движений
    не пишет». Порядок внутри тела мы НЕ анализируем (это dataflow): подавление законно только
    когда ВСЕ присваивания имени в теле — платформенные фабрики; любое возражение -> показать."""
    # (а) платформенное присваивание в ЧУЖОЙ процедуре, пользовательское — в обработчике
    other_proc = (
        "Процедура Служебная()\n"
        "    Сервис = РегистрыСведений.СостоянияДокументов.СоздатьНаборЗаписей();\n"
        "КонецПроцедуры\n"
        "\n"
        "Процедура ОбработкаПроведения(Отказ, РежимПроведения)\n"
        "    Сервис = ПолучитьСервисПроведения();\n"
        "    Сервис.Записать(ЭтотОбъект);\n"
        "КонецПроцедуры\n"
    )
    # (б) переприсваивание в САМОМ обработчике: в точке вызова это уже не набор записей
    reassigned = (
        "Процедура ОбработкаПроведения(Отказ, РежимПроведения)\n"
        "    Сервис = РегистрыСведений.СостоянияДокументов.СоздатьНаборЗаписей();\n"
        "    Сервис = ПолучитьСервисПроведения();\n"
        "    Сервис.Записать(ЭтотОбъект);\n"
        "КонецПроцедуры\n"
    )
    for case, body in (("other_proc", other_proc), ("reassigned", reassigned)):
        with tempfile.TemporaryDirectory() as tmpdir:
            bsl, reader = _make_posting_env(tmpdir, {"ТестДок": body})
            try:
                hint = bsl["find_register_movements"]("ТестДок")["hint"]
                assert "не пишет" not in hint, f"({case}) вызов подавлен по чужому/устаревшему присваиванию: {hint}"
                assert "Сервис.Записать" in hint, (case, hint)
            finally:
                reader.close()


def test_posting_hint_record_set_creation_alone_is_not_claimed_as_a_write():
    """СоздатьНаборЗаписей()/СоздатьМенеджерЗаписи() НИЧЕГО не записывают до вызова Записать().

    Прежний разбор объявлял ЗАПИСЬЮ само создание: на `Набор = ...СоздатьНаборЗаписей();
    Набор.Прочитать();` регистр попадал в «ЗАПИСЬ РЕГИСТРОВ ПРЯМО В ОБРАБОТЧИКЕ»,
    `Набор.Прочитать()` подавлялся как платформенный шум, и финал «делегата нет, идти дальше
    некуда» закрывал трассировку на ЧТЕНИИ (Codex HIGH, v1.28). Регистр обязан быть назван —
    но честным абзацем «создан, Записать() в теле не видно», без факта записи и без финала."""
    body_read = (
        "Процедура ОбработкаПроведения(Отказ, РежимПроведения)\n"
        "    Набор = РегистрыСведений.СостоянияДокументов.СоздатьНаборЗаписей();\n"
        "    Набор.Прочитать();\n"
        "КонецПроцедуры\n"
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(tmpdir, {"ТестДок": body_read})
        try:
            hint = bsl["find_register_movements"]("ТестДок")["hint"]
            assert "ЗАПИСЬ РЕГИСТРОВ ПРЯМО В ОБРАБОТЧИКЕ" not in hint, f"чтение выдано за запись: {hint}"
            assert "идти дальше некуда" not in hint, f"ложный финал на чтении набора: {hint}"
            assert "РегистрыСведений.СостоянияДокументов" in hint, f"регистр не назван вовсе: {hint}"
            assert "СОЗДАНИЕ — ЕЩЕ НЕ ЗАПИСЬ" in hint, hint
        finally:
            reader.close()

    # Смешанное тело: один набор ЗАПИСАН, второй только создан — статусы НЕ смешиваются,
    # а созданный-без-записи не даёт финала «идти дальше некуда» рядом с записанным.
    body_mixed = (
        "Процедура ОбработкаПроведения(Отказ, РежимПроведения)\n"
        "    Чтение = РегистрыСведений.СостоянияДокументов.СоздатьНаборЗаписей();\n"
        "    Чтение.Прочитать();\n"
        "    Запись = РегистрыНакопления.ТестовыйРегистр.СоздатьНаборЗаписей();\n"
        "    Запись.Записать();\n"
        "КонецПроцедуры\n"
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(tmpdir, {"ТестДок": body_mixed})
        try:
            hint = bsl["find_register_movements"]("ТестДок")["hint"]
            assert "ЗАПИСЬ РЕГИСТРОВ ПРЯМО В ОБРАБОТЧИКЕ" in hint, hint
            written_part = hint.split("СОЗДАН, НО")[0]
            assert "РегистрыНакопления.ТестовыйРегистр" in written_part, hint
            assert "РегистрыСведений.СостоянияДокументов" in hint.split("СОЗДАН, НО")[1], hint
            assert "идти дальше некуда" not in hint, f"созданный набор не остановил финал: {hint}"
        finally:
            reader.close()

    # Запись ЦЕПОЧКОЙ сразу за фабрикой — законный written без переменной.
    body_chained = (
        "Процедура ОбработкаПроведения(Отказ, РежимПроведения)\n"
        "    РегистрыСведений.СостоянияДокументов.СоздатьМенеджерЗаписи().Записать();\n"
        "КонецПроцедуры\n"
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(tmpdir, {"ТестДок": body_chained})
        try:
            hint = bsl["find_register_movements"]("ТестДок")["hint"]
            assert "ЗАПИСЬ РЕГИСТРОВ ПРЯМО В ОБРАБОТЧИКЕ" in hint, hint
            assert "РегистрыСведений.СостоянияДокументов" in hint, hint
        finally:
            reader.close()


def test_posting_hint_property_chain_receiver_is_reported_not_swallowed():
    """Цепочка свойств (`ЭтотОбъект.Реквизит.Метод()`, `А.Б.Метод()`) раньше выпадала из разбора
    ЦЕЛИКОМ — вопреки обещанию «неразрешённое помечено НЕ ОПОЗНАН» она не попадала даже туда:
    все списки фактов пустели, и hint заявлял «движений он не пишет», пряча единственного
    настоящего делегата (Codex HIGH, v1.28)."""
    body = (
        "Процедура ОбработкаПроведения(Отказ, РежимПроведения)\n"
        f"    ЭтотОбъект.{_VAR_RECEIVER}.ОтразитьДвижения(Отказ);\n"
        "КонецПроцедуры\n"
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(tmpdir, {"ТестДок": body})
        try:
            hint = bsl["find_register_movements"]("ТестДок")["hint"]
            assert "не пишет" not in hint, f"цепочка свойств выпала из анализа целиком: {hint}"
            assert f"ЭтотОбъект.{_VAR_RECEIVER}.ОтразитьДвижения" in hint, hint
            assert "НЕ ОПОЗНАН" in hint, hint
            # Стенд не под git: маршрут объявления — find_definition (git_search не зарегистрирован).
            assert "find_definition(" in hint and "git_search(" not in hint, hint
            for label, src in _hint_steps(hint).items():
                compile(src, f"<hint:{label}>", "exec")
        finally:
            reader.close()

    # Ловушка модуля-однофамильца у цепочки проверяется по ПОСЛЕДНЕМУ звену — именно его агент
    # подставил бы в find_definition и молча получил бы чужое тело.
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(tmpdir, {"ТестДок": body}, variable_receiver_delegate=True)
        try:
            hint = bsl["find_register_movements"]("ТестДок")["hint"]
            assert "ЛОВУШКА" in hint, f"однофамилец последнего звена не назван ловушкой: {hint}"
            assert f"'ОбщийМодуль.{_VAR_RECEIVER}'" in hint, hint
            assert f"'ОбщийМодуль.ЭтотОбъект.{_VAR_RECEIVER}'" not in hint, (
                f"module_ref собран из ВСЕЙ цепочки, а не из звена: {hint}"
            )
        finally:
            reader.close()

    # `ЭтотОбъект.X.М()` с X-реквизитом, подтверждённым живой проверкой, — доказуемый РЕКВИЗИТ.
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(tmpdir, {"ТестДок": body}, attribute_receiver=True)
        try:
            hint = bsl["find_register_movements"]("ТестДок")["hint"]
            assert "РЕКВИЗИТ ДОКУМЕНТА" in hint, f"живой реквизит за ЭтотОбъект. не распознан: {hint}"
        finally:
            reader.close()

    # Контроль шума: голова цепочки — платформенное пространство имён, менеджерный вызов
    # делегатом не является (поведение прежнее: такие вызовы и раньше не показывались).
    noise_body = (
        "Процедура ОбработкаПроведения(Отказ, РежимПроведения)\n"
        "    Док = Документы.ЗаказКлиента.НайтиПоНомеру(Номер);\n"
        "КонецПроцедуры\n"
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(tmpdir, {"ТестДок": noise_body})
        try:
            hint = bsl["find_register_movements"]("ТестДок")["hint"]
            assert "НайтиПоНомеру" not in hint, f"менеджерный вызов выдан за делегата: {hint}"
        finally:
            reader.close()


def test_posting_hint_shadowing_ignores_locals_of_other_procedures():
    """Локальная переменная ЧУЖОЙ процедуры — другая область видимости: присваивание в
    `Служебная()` не затеняет общий модуль в `ОбработкаПроведения`. Межпроцедурный поиск
    маркеров объявлял получателя «переменной», и точный маршрут find_definition по модулю
    подменялся широким поиском (Codex MED, v1.28). Модульные переменные (Перем до процедур)
    видимы обработчику ЗАКОННО — их покрывает control-фикстура module_level_perem_list."""
    delegate_fn = f"Процедура {_VAR_DELEGATE_METHOD}(Объект) Экспорт\nКонецПроцедуры\n"
    body = (
        "Процедура Служебная()\n"
        f"    {_VAR_RECEIVER} = ПолучитьСервис();\n"
        "КонецПроцедуры\n"
        "\n"
        "Процедура ОбработкаПроведения(Отказ, РежимПроведения)\n"
        f"    {_VAR_RECEIVER}.{_VAR_DELEGATE_METHOD}(ЭтотОбъект);\n"
        "КонецПроцедуры\n"
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(tmpdir, {"ТестДок": body}, extra_common_modules={_VAR_RECEIVER: delegate_fn})
        try:
            hint = bsl["find_register_movements"]("ТестДок")["hint"]
            assert "ДЕЛЕГАТ:" in hint and "ОБЩИЙ МОДУЛЬ" in hint, (
                f"локал чужой процедуры затенил рабочий модуль: {hint}"
            )
            assert "ПЕРЕМЕННАЯ (или параметр)" not in hint, hint
        finally:
            reader.close()


def test_posting_hint_for_each_variable_shadows_a_homonymous_common_module():
    """Переменная `Для Каждого X Из ...` затеняет одноименный общий модуль так же, как параметр
    или присваивание. У нее нет `X =`, поэтому прежний разбор объявлял вызов на элементе коллекции
    вызовом общего модуля и выдавал исполнимый, но ведущий в чужое тело find_definition."""
    receiver = "Проводка"
    method = "Отразить"
    body = (
        "Процедура ОбработкаПроведения(Отказ, РежимПроведения)\n"
        f"    Для Каждого {receiver} Из Проводки Цикл\n"
        f"        {receiver}.{method}();\n"
        "    КонецЦикла;\n"
        "КонецПроцедуры\n"
    )
    foreign_module = f"Процедура {method}() Экспорт\n    // ЧУЖОЕ ТЕЛО\nКонецПроцедуры\n"
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(
            tmpdir,
            {"ТестДок": body},
            extra_common_modules={receiver: foreign_module},
        )
        try:
            hint = bsl["find_register_movements"]("ТестДок")["hint"]
            assert "ПЕРЕМЕННАЯ (или параметр)" in hint, f"loop-переменная выдана за общий модуль: {hint}"
            forbidden = f"find_definition('{method}', 'ОбщийМодуль.{receiver}')"
            offending = [f"({k}) {v}" for k, v in _hint_steps(hint).items() if forbidden in v]
            assert not offending, f"исполнимый шаг ведет в чужой одноименный модуль: {offending}"
        finally:
            reader.close()


def test_posting_hint_does_not_treat_common_module_prefix_as_register_namespace():
    """Только четыре точных платформенных namespace `Регистры<Тип>` являются шумом.
    Пользовательский общий модуль `РегистрыПроведения` с тем же префиксом — обычный делегат."""
    receiver = "РегистрыПроведения"
    method = "ОтразитьДвижения"
    body = (
        f"Процедура ОбработкаПроведения(Отказ, РежимПроведения)\n    {receiver}.{method}(ЭтотОбъект);\nКонецПроцедуры\n"
    )
    delegate = f"Процедура {method}(Объект) Экспорт\nКонецПроцедуры\n"
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(
            tmpdir,
            {"ТестДок": body},
            extra_common_modules={receiver: delegate},
        )
        try:
            hint = bsl["find_register_movements"]("ТестДок")["hint"]
            assert f"ДЕЛЕГАТ: {receiver}.{method}" in hint, f"общий модуль поглощен register-шумом: {hint}"
            assert "ОБЩИЙ МОДУЛЬ" in hint, hint
        finally:
            reader.close()


def test_posting_hint_local_execute_method_wins_over_dotless_noise():
    """Локальная процедура законно называется `Выполнить`. Если dotless-шум проверяется раньше
    списка объявлений модуля, единственный делегат исчезает и hint ложно говорит, что обработчик
    движений не пишет."""
    body = (
        "Процедура ОбработкаПроведения(Отказ, РежимПроведения)\n"
        "    Выполнить();\n"
        "КонецПроцедуры\n"
        "\n"
        "Процедура Выполнить()\n"
        "    Набор = РегистрыСведений.СостоянияДокументов.СоздатьНаборЗаписей();\n"
        "    Набор.Записать();\n"
        "КонецПроцедуры\n"
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(tmpdir, {"ТестДок": body})
        try:
            hint = bsl["find_register_movements"]("ТестДок")["hint"]
            assert "ЛОКАЛЬНЫЙ ВЫЗОВ Выполнить" in hint, f"локальный метод поглощен dotless-шумом: {hint}"
            local_steps = [src for src in _hint_steps(hint).values() if "read_procedure" in src and "Выполнить" in src]
            assert len(local_steps) == 1, f"нет исполнимого маршрута в локальный метод: {hint}"
            local_body = eval(compile(local_steps[0], "<hint:local-execute>", "eval"), dict(bsl))  # noqa: S307
            assert local_body and "СоздатьНаборЗаписей" in local_body, local_body
        finally:
            reader.close()


def test_posting_hint_marks_builtin_execute_as_dynamic_and_inconclusive():
    """Встроенное Выполнить может скрывать запись регистра целиком в строке или переменной.
    Строки static-анализ намеренно вырезает, поэтому такой вызов запрещает отрицательный вывод;
    одноименная локальная процедура по-прежнему проверяется отдельным контрольным тестом выше.
    """
    body = (
        "Процедура ОбработкаПроведения(Отказ, РежимПроведения)\n"
        '    Выполнить("Набор = РегистрыСведений.ДинамическийСлед.СоздатьНаборЗаписей(); '
        'Набор.Записать();");\n'
        "КонецПроцедуры\n"
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(tmpdir, {"ТестДок": body})
        try:
            hint = bsl["find_register_movements"]("ТестДок")["hint"]
            assert "ДИНАМИЧЕСКОЕ ВЫПОЛНЕНИЕ" in hint, hint
            assert "отрицательный вывод о движениях ЗАПРЕЩЕН" in hint, hint
            assert "судя по коду, движений он не пишет" not in hint, hint
            assert "ВЫЗОВ БЕЗ ТОЧКИ Выполнить" not in hint, hint
        finally:
            reader.close()


def test_posting_hint_is_bounded_and_lists_calls_beyond_the_route_budget():
    """На каждый вызов hint строит крупный текстовый маршрут, а stdout песочницы обрезается на
    ~15К символов: без потолка длинный обработчик терял бы ХВОСТ hint — последние делегаты и
    финальные инструкции — МОЛЧА (Codex LOW, v1.28). Развернутых маршрутов не больше лимита,
    но КАЖДЫЙ вызов назван поимённо, и финальный блок инструкций доживает до конца."""
    lines = []
    for i in range(20):
        lines.append(f"    Сервис{i:02d} = Настройка{i:02d}(Отказ);\n")
        lines.append(f"    Сервис{i:02d}.Метод{i:02d}(Отказ);\n")
    body = "Процедура ОбработкаПроведения(Отказ, РежимПроведения)\n" + "".join(lines) + "КонецПроцедуры\n"
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(tmpdir, {"ТестДок": body})
        try:
            hint = bsl["find_register_movements"]("ТестДок")["hint"]
            assert len(hint) < 14000, f"hint не влезает в stdout-лимит песочницы (15000): {len(hint)}"
            for i in range(20):
                assert f"Сервис{i:02d}.Метод{i:02d}" in hint, f"вызов #{i} пропал молча"
                assert f"Настройка{i:02d}" in hint, f"вызов без точки #{i} пропал молча"
            assert "ЕЩЕ ВЫЗОВЫ ИЗ ОБРАБОТЧИКА" in hint, hint
            assert "Трассируй им ДЕЛЕГАТА, а не обработчик." in hint, "финальные инструкции обрезаны"
        finally:
            reader.close()


def test_posting_hint_paginates_every_overflow_call_name():
    """A bounded hint cannot inline an unbounded call list, but it must expose an
    exact continuation route instead of replacing the tail with an anonymous count."""
    count = 50
    receivers = [f"Сервис{i:02d}" for i in range(count)]
    body = (
        "Перем "
        + ", ".join(receivers)
        + ";\n\nПроцедура ОбработкаПроведения(Отказ, РежимПроведения)\n"
        + "".join(f"    {receiver}.Метод{i:02d}();\n" for i, receiver in enumerate(receivers))
        + "КонецПроцедуры\n"
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(tmpdir, {"ТестДок": body})
        try:
            first = bsl["find_register_movements"]("ТестДок")["hint"]
            assert "find_register_movements('ТестДок', posting_calls_offset=40)" in first, first
            second = bsl["find_register_movements"]("ТестДок", posting_calls_offset=40)["hint"]
            combined = first + "\n" + second
            for i, receiver in enumerate(receivers):
                assert f"{receiver}.Метод{i:02d}" in combined, f"вызов #{i} недоступен ни на одной странице"
            assert "и еще" not in combined
            assert len(first) < 14000 and len(second) < 14000
            assert "Трассируй им ДЕЛЕГАТА, а не обработчик." in second, "финальный tail потерян на странице"
        finally:
            reader.close()


def test_posting_hint_paginates_record_set_names_without_losing_tail():
    """Record-set facts share the same bounded compact pager as overflow calls.
    Every exact register name remains reachable and every page keeps the final warning."""
    count = 140
    register_names = [f"ОченьДлинноеИмяРегистраПроведения{i:03d}" for i in range(count)]
    statements = []
    for i, name in enumerate(register_names):
        statements.append(f"    Набор{i:03d} = РегистрыСведений.{name}.СоздатьНаборЗаписей();\n")
        statements.append(f"    Набор{i:03d}.Записать();\n")
    body = "Процедура ОбработкаПроведения(Отказ, РежимПроведения)\n" + "".join(statements) + "КонецПроцедуры\n"

    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(tmpdir, {"ТестДок": body})
        try:
            offset = 0
            seen_offsets: set[int] = set()
            pages: list[str] = []
            while offset not in seen_offsets:
                seen_offsets.add(offset)
                hint = bsl["find_register_movements"]("ТестДок", posting_calls_offset=offset)["hint"]
                pages.append(hint)
                assert len(hint) < 14000, f"compact page exceeds stdout budget: {len(hint)}"
                assert "Трассируй им ДЕЛЕГАТА, а не обработчик." in hint, "финальный tail потерян"
                match = re.search(
                    r"следующая страница: find_register_movements\('ТестДок', posting_calls_offset=(\d+)\)",
                    hint,
                )
                if match is None:
                    break
                offset = int(match.group(1))
            else:  # pragma: no cover - protects the test itself from a cyclic continuation
                pytest.fail(f"cyclic posting pager: {seen_offsets}")

            combined = "\n".join(pages)
            assert len(pages) > 1, "fixture did not cross the compact page budget"
            for name in register_names:
                assert f"РегистрыСведений.{name}" in combined, f"register fact lost: {name}"
        finally:
            reader.close()


def test_posting_hint_only_recommends_registered_helpers():
    """git_search регистрируется ТОЛЬКО когда исходники под git (`register_git_search='auto'`).
    Безусловный совет git_search(...) на не-git конфигурации — NameError ровно на fallback-пути:
    хелпера просто НЕТ в namespace песочницы (Codex HIGH, v1.28). Терминальные маршруты hint
    обязаны строиться из ЗАРЕГИСТРИРОВАННЫХ хелперов, а когда исчерпывающего маршрута нет —
    честно называть ограничение. Тест ИСПОЛНЯЕТ каждый нумерованный шаг в том же namespace,
    который получает агент."""
    body = (
        "Процедура ОбработкаПроведения(Отказ, РежимПроведения)\n"
        "    Сервис = ПолучитьСервис();\n"
        "    Сервис.ОтразитьДвижения(ЭтотОбъект);\n"
        "КонецПроцедуры\n"
    )
    # (а) НЕ под git, индекс есть: маршрут — find_definition БЕЗ module-hint; git_search не
    # упоминается вовсе, и каждый шаг исполняется без NameError.
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(tmpdir, {"ТестДок": body})
        try:
            assert "git_search" not in bsl, "предусловие: не-git стенд не регистрирует git_search"
            hint = bsl["find_register_movements"]("ТестДок")["hint"]
            assert "git_search(" not in hint, f"hint советует незарегистрированный хелпер: {hint}"
            assert "find_definition('ОтразитьДвижения')" in hint, hint
            ns = dict(bsl)
            for label, src in sorted(_hint_steps(hint).items()):
                exec(compile(src, f"<hint:{label}>", "exec"), ns)  # noqa: S102
        finally:
            reader.close()

    # (б) НЕ под git и БЕЗ индекса: исчерпывающего маршрута нет — hint называет ограничение и
    # даёт живой safe_grep-маршрут вместо нумерованного шага с несуществующим хелпером.
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_posting_env(tmpdir, {"ТестДок": body}, no_index=True)
        hint = bsl["find_register_movements"]("ТестДок")["hint"]
        assert "git_search(" not in hint, hint
        assert "find_module" in hint and "safe_grep(" in hint, f"нет честного ограничения с живым маршрутом: {hint}"
        ns = dict(bsl)
        for label, src in sorted(_hint_steps(hint).items()):
            exec(compile(src, f"<hint:{label}>", "exec"), ns)  # noqa: S102

    # (в) Под git поиск объявления всё равно live/Python: git grep -iE ненадёжен для кириллицы.
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(tmpdir, {"ТестДок": body}, git=True)
        try:
            hint = bsl["find_register_movements"]("ТестДок")["hint"]
            assert "safe_grep('(?i)^" in hint and "ОтразитьДвижения" in hint, hint
            assert "ЛЮБОЙ регистр" not in hint
        finally:
            reader.close()


def test_register_movements_recipe_names_the_no_git_route():
    """Статический registry-рецепт не должен противоречить capability-aware runtime-hint."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(tmpdir, {"ТестДок": _DELEGATED})
        try:
            recipe = bsl["_registry"]["find_register_movements"]["recipe"]
            assert "уведет в git_search" not in recipe, recipe
            assert "точный live safe_grep" in recipe and "find_definition" in recipe, recipe
        finally:
            reader.close()


def test_stale_index_attribute_deleted_from_xml_does_not_shadow_the_module():
    """Индекс может не только ОТСТАВАТЬ от XML, но и ОПЕРЕЖАТЬ его: реквизит удалён из XML без
    пересборки. Смешивание index- и live-имён в одном наборе выдавало удалённый реквизит за
    live-факт «РЕКВИЗИТ ДОКУМЕНТА» — раньше ветки common_module — и уводило от настоящего
    модуля-делегата (Codex MED, v1.28). Имена из индекса не дают НИКАКОГО факта: live-маршрут
    хелпера верит только живому XML, профиль (live запрещён) даёт развилку, а не «РЕКВИЗИТ»."""
    body = (
        "Процедура ОбработкаПроведения(Отказ, РежимПроведения)\n"
        f"    {_VAR_RECEIVER}.{_VAR_DELEGATE_METHOD}(ЭтотОбъект);\n"
        "КонецПроцедуры\n"
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(
            tmpdir,
            {"ТестДок": body},
            attribute_receiver=True,  # реквизит существует НА МОМЕНТ СБОРКИ индекса
            variable_receiver_delegate=True,  # и существует одноимённый общий модуль
        )
        try:
            # Предусловие: индекс ЗНАЕТ реквизит (иначе тест не про stale-опережение).
            rows = reader.get_object_attributes(object_name="ТестДок", category="Documents") or []
            assert any((r.get("attr_name") or "").casefold() == _VAR_RECEIVER.casefold() for r in rows), rows
            # Реквизит УДАЛЯЕТСЯ из живого XML; пересборки индекса нет.
            doc_xml = os.path.join(tmpdir, "cf", "Documents", "ТестДок.xml")
            with open(doc_xml, "w", encoding="utf-8") as f:
                f.write(
                    '<?xml version="1.0" encoding="UTF-8"?>\n'
                    '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">\n'
                    '  <Document uuid="u-ТестДок">\n'
                    "    <Properties><Name>ТестДок</Name></Properties>\n"
                    "  </Document>\n"
                    "</MetaDataObject>\n"
                )

            # ХЕЛПЕР (live-маршрут): удалённый реквизит не затеняет модуль — честный live-факт.
            hint = bsl["find_register_movements"]("ТестДок")["hint"]
            assert "РЕКВИЗИТ" not in hint, f"удалённый реквизит из stale-индекса выдан за live-факт: {hint}"
            assert "ДЕЛЕГАТ:" in hint and "ОБЩИЙ МОДУЛЬ" in hint, hint

            # ПРОФИЛЬ (live запрещён контрактом): index-позитив — НЕ факт, а развилка с причиной.
            sec = bsl["get_object_profile"]("ТестДок", sections=["registers"])["sections"]["registers"]
            phint = sec.get("hint") or ""
            assert "РЕКВИЗИТ ДОКУМЕНТА" not in phint, f"профиль выдал index-строку за факт: {phint}"
            assert "НЕ ПРОВЕРЕНЫ" in phint and "ИНДЕКС" in phint.upper(), phint
        finally:
            reader.close()


def test_stale_deleted_common_module_is_not_reported_as_a_live_fact():
    """SQLite может помнить общий модуль, файл которого уже удалён без пересборки.

    Такой снимок не доказывает существование получателя и не должен вести агента в точный
    `find_definition` по заведомо отсутствующему модулю; fallback ищет объявления живьём.
    """
    receiver = "УдаленныйСервисПроведения"
    method = "СформироватьДвижения"
    body = (
        f"Процедура ОбработкаПроведения(Отказ, РежимПроведения)\n    {receiver}.{method}(ЭтотОбъект);\nКонецПроцедуры\n"
    )
    module_body = f"Процедура {method}(Объект) Экспорт\nКонецПроцедуры\n"
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(
            tmpdir,
            {"ТестДок": body},
            extra_common_modules={receiver: module_body},
        )
        try:
            stale_path = os.path.join(tmpdir, "cf", "CommonModules", receiver, "Ext", "Module.bsl")
            os.remove(stale_path)

            hint = bsl["find_register_movements"]("ТестДок")["hint"]
            assert f"получатель '{receiver}' это ОБЩИЙ МОДУЛЬ" not in hint, hint
            assert "ИНДЕКС ПОМНИТ" in hint and "ЖИВЬЕМ не читается" in hint, hint
            assert f"find_definition('{method}', 'ОбщийМодуль.{receiver}')" not in hint, hint
            assert "safe_grep(" in hint, f"нет live-маршрута вместо stale definition: {hint}"
            live_steps = [src for src in _hint_steps(hint).values() if src.startswith("safe_grep(")]
            assert live_steps, hint
            result = eval(compile(live_steps[0], "<hint:live-search>", "eval"), dict(bsl))  # noqa: S307
            assert result == [], result
        finally:
            reader.close()


def test_record_set_variable_reuse_does_not_mark_the_read_register_as_written():
    """Переменная набора переиспользуется ЗАКОННО: ранний `Набор.Записать()` относится к первому
    регистру, а не ко всем последующим фабрикам на том же имени. Поиск Записать() по ВСЕМУ телу
    помечал записанными оба регистра (Codex MED, v1.28); теперь запись ищется только на участке
    «от фабрики до следующего присваивания той же переменной»."""
    body = (
        "Процедура ОбработкаПроведения(Отказ, РежимПроведения)\n"
        "    Набор = РегистрыСведений.РегистрАльфа.СоздатьНаборЗаписей();\n"
        "    Набор.Записать();\n"
        "    Набор = РегистрыСведений.РегистрБета.СоздатьНаборЗаписей();\n"
        "    Набор.Прочитать();\n"
        "КонецПроцедуры\n"
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(tmpdir, {"ТестДок": body})
        try:
            hint = bsl["find_register_movements"]("ТестДок")["hint"]
            written_part, created_part = hint.split("СОЗДАН, НО", 1)
            assert "РегистрыСведений.РегистрАльфа" in written_part, hint
            assert "РегистрыСведений.РегистрБета" in created_part, hint
            assert "РегистрыСведений.РегистрБета" not in written_part, (
                f"ранний Записать() приписан следующему регистру на той же переменной: {hint}"
            )
        finally:
            reader.close()

    # Обратный порядок: Записать() ДО создания — не запись созданного ниже набора.
    reversed_body = (
        "Процедура ОбработкаПроведения(Отказ, РежимПроведения)\n"
        "    Набор = РегистрыСведений.РегистрАльфа.СоздатьНаборЗаписей();\n"
        "    Набор.Прочитать();\n"
        "    Набор = РегистрыСведений.РегистрБета.СоздатьНаборЗаписей();\n"
        "    Набор.Записать();\n"
        "КонецПроцедуры\n"
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(tmpdir, {"ТестДок": reversed_body})
        try:
            hint = bsl["find_register_movements"]("ТестДок")["hint"]
            written_part, created_part = hint.split("СОЗДАН, НО", 1)
            assert "РегистрыСведений.РегистрБета" in written_part, hint
            assert "РегистрыСведений.РегистрАльфа" in created_part, hint
        finally:
            reader.close()


def test_record_set_factory_allows_spaces_around_the_first_platform_dot():
    """BSL допускает пробелы вокруг точки и между `РегистрыСведений` и именем регистра.
    Dotted-call parser это уже поддерживает, а более строгий record-set parser терял запись и
    вместо готового факта показывал `Менеджер.Записать()` как неразрешенный делегат."""
    body = (
        "Процедура ОбработкаПроведения(Отказ, РежимПроведения)\n"
        "    Менеджер = РегистрыСведений . СостоянияДокументов . СоздатьМенеджерЗаписи();\n"
        "    Менеджер.Записать();\n"
        "КонецПроцедуры\n"
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(tmpdir, {"ТестДок": body})
        try:
            hint = bsl["find_register_movements"]("ТестДок")["hint"]
            assert "ЗАПИСЬ РЕГИСТРОВ ПРЯМО В ОБРАБОТЧИКЕ" in hint, hint
            assert "РегистрыСведений.СостоянияДокументов" in hint, hint
            assert "ВЫЗОВ Менеджер.Записать" not in hint, f"платформенная запись выдана за делегата: {hint}"
        finally:
            reader.close()


def test_spaced_dot_call_is_one_delegate_not_also_a_global_call():
    """Dotted-регулярка терпит пробелы вокруг точки (`Модуль . Метод()`), а dotless-регулярка
    запрещает точку только ВПЛОТНУЮ перед именем — то же `Метод(` матчилось ещё и как «вызов без
    точки», и hint рядом с правильным маршрутом по модулю печатал ложное «экспортный метод
    ГЛОБАЛЬНОГО общего модуля» с несуженным find_definition (Codex MED, v1.28)."""
    for spacing in (
        "ОбщийМодульУчета . ОтразитьВУчете",
        "ОбщийМодульУчета.  ОтразитьВУчете",
        "ОбщийМодульУчета  .ОтразитьВУчете",
    ):
        body = f"Процедура ОбработкаПроведения(Отказ, РежимПроведения)\n    {spacing}(ЭтотОбъект, Отказ);\nКонецПроцедуры\n"
        with tempfile.TemporaryDirectory() as tmpdir:
            bsl, reader = _make_posting_env(tmpdir, {"ТестДок": body})
            try:
                hint = bsl["find_register_movements"]("ТестДок")["hint"]
                assert "ДЕЛЕГАТ:" in hint and "ОбщийМодульУчета" in hint, (spacing, hint)
                assert "ВЫЗОВ БЕЗ ТОЧКИ" not in hint, (
                    f"({spacing!r}) имя метода dotted-вызова продублировано как вызов без точки: {hint}"
                )
            finally:
                reader.close()


def test_profile_global_empty_table_applies_cfe_replacement_to_main_english_alias():
    """The rows=None profile branch applies the same CFE filter as the detailed helper."""
    main = (
        "Procedure ОбработкаПроведения(Cancel, Mode)\n"
        "    RegisterRecords.MainEnglishRegister.Write = True;\n"
        "EndProcedure\n"
    )
    extension = (
        '&Вместо("ОбработкаПроведения")\n'
        "Процедура ЗаменитьПроведение(Отказ, РежимПроведения)\n"
        "    Движения.РегистрТолькоCFE.Записывать = Истина;\n"
        "КонецПроцедуры\n"
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(
            tmpdir,
            {"ТестДок": main},
            ext_docs={"ТестДок": extension},
            with_movements_doc=False,
        )
        try:
            helper_result = bsl["find_register_movements"]("ТестДок")
            assert {row["name"] for row in helper_result["code_registers"]} == {"РегистрТолькоCFE"}
            assert {row["name"] for row in helper_result["suppressed_main_code_registers"]} == {"MainEnglishRegister"}

            section = bsl["get_object_profile"]("ТестДок", sections=["registers"])["sections"]["registers"]
            assert section["status"] == "unavailable", section
            assert section["items"] == [{"register": "РегистрТолькоCFE", "source": "code"}], section
            assert section["summary"]["code_registers"] == 1, section
            assert section["summary"]["main_code_registers_suppressed_by_cfe"] == 1, section
            assert section["_meta"]["cfe_posting_replacement"]["main_handler_continuation_visible"] is False, section
        finally:
            reader.close()


@pytest.mark.parametrize(
    "with_movements_doc",
    [False, True],
    ids=["main_table_globally_empty", "main_table_has_other_rows"],
)
def test_cfe_replacement_suppresses_post_build_main_english_alias(with_movements_doc):
    """A main ObjectModule absent from the snapshot still uses the live CFE suppression path."""
    main = (
        "Procedure ОбработкаПроведения(Cancel, Mode)\n    RegisterRecords.PostBuildMain.Write = True;\nEndProcedure\n"
    )
    extension = (
        '&Вместо("ОбработкаПроведения")\n'
        "Процедура ЗаменитьПроведение(Отказ, РежимПроведения)\n"
        "    Движения.PostBuildCFE.Записывать = Истина;\n"
        "КонецПроцедуры\n"
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(
            tmpdir,
            {"ТестДок": ""},
            ext_docs={"ТестДок": extension},
            post_index_object_modules={"ТестДок": main},
            with_movements_doc=with_movements_doc,
        )
        try:
            assert not any(
                row["object_name"] == "ТестДок" and row["module_type"] == "ObjectModule"
                for row in reader.get_all_modules()
            )
            helper_result = bsl["find_register_movements"]("тестдок")
            assert {row["name"] for row in helper_result["code_registers"]} == {"PostBuildCFE"}
            if with_movements_doc:
                assert {row["name"] for row in helper_result["suppressed_main_code_registers"]} == {"PostBuildMain"}

            section = bsl["get_object_profile"]("тестдок", sections=["registers"])["sections"]["registers"]
            assert section["items"] == [{"register": "PostBuildCFE", "source": "code"}], section
            assert section["summary"]["main_code_registers_suppressed_by_cfe"] == 1, section
        finally:
            reader.close()


@pytest.mark.parametrize(
    "constructor",
    ['Новый Структура("Ссылка", Ссылка)', 'New Structure("Ref", Ссылка)'],
    ids=["ru", "en"],
)
def test_posting_hint_does_not_treat_new_type_constructor_as_dotless_call(constructor):
    body = (
        "Процедура ОбработкаПроведения(Отказ, РежимПроведения)\n"
        f"    Данные = {constructor};\n"
        "    ОбщийМодульУчета.ОтразитьВУчете(Данные, Отказ);\n"
        "КонецПроцедуры\n"
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(tmpdir, {"ТестДок": body})
        try:
            hint = bsl["find_register_movements"]("ТестДок")["hint"]
            assert "ДЕЛЕГАТ:" in hint and "ОбщийМодульУчета.ОтразитьВУчете" in hint, hint
            type_name = "Структура" if constructor.startswith("Новый") else "Structure"
            assert f"ВЫЗОВ БЕЗ ТОЧКИ {type_name}" not in hint, hint
            assert f"find_definition('{type_name}')" not in hint, hint
        finally:
            reader.close()


def test_posting_hint_sees_an_attribute_added_by_an_edt_extension():
    """EDT-расширение хранит метаданные как Documents/<Имя>/<Имя>.mdo — этот layout поддержан
    штатным резолвером (_resolve_object_xml), и проверка реквизитов расширений обязана идти
    через него, а не через захардкоженный Documents/<Имя>.xml: иначе реквизит EDT-расширения
    невидим, и вызов на нём снова объявляется вызовом одноимённого общего модуля."""
    body = (
        "Процедура ОбработкаПроведения(Отказ, РежимПроведения)\n"
        f"    {_VAR_RECEIVER}.{_VAR_DELEGATE_METHOD}(ЭтотОбъект);\n"
        "КонецПроцедуры\n"
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(
            tmpdir,
            {"ТестДок": body},
            variable_receiver_delegate=True,
            ext_attribute_receiver="mdo",
        )
        try:
            hint = bsl["find_register_movements"]("ТестДок")["hint"]
            assert "РЕКВИЗИТ" in hint, f"реквизит EDT-расширения (.mdo) не увиден: {hint}"
            forbidden = f"find_definition('{_VAR_DELEGATE_METHOD}', 'ОбщийМодуль.{_VAR_RECEIVER}')"
            offending = [f"({k}) {v}" for k, v in _hint_steps(hint).items() if forbidden in v]
            assert not offending, f"ШАГ ведет в ЧУЖОЙ общий модуль мимо mdo-реквизита: {offending}"
        finally:
            reader.close()


def test_posting_hint_separates_local_calls_from_global_ones():
    """Вызов БЕЗ точки: если метод объявлен в ЭТОМ модуле — маршрут короткий (read_procedure).
    Если НЕ объявлен — это экспортный метод ГЛОБАЛЬНОГО общего модуля или глобального контекста,
    и «метод тут же» было бы ложью (прежний hint именно так и утверждал)."""
    # локальный
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(tmpdir, {"ТестДок": _DELEGATED_LOCAL})
        try:
            hint = bsl["find_register_movements"]("ТестДок")["hint"]
            assert "ЛОКАЛЬНЫЙ ВЫЗОВ" in hint, hint
            code = _hint_steps(hint)
            ns = dict(bsl)
            local_body = eval(compile(code["2"], "<hint:2>", "eval"), ns)  # noqa: S307
            assert local_body and "СоздатьНаборЗаписей" in local_body, local_body
        finally:
            reader.close()

    # глобальный: метода в модуле объекта НЕТ
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(tmpdir, {"ТестДок": _DELEGATED_GLOBAL}, global_delegate=True)
        try:
            hint = bsl["find_register_movements"]("ТестДок")["hint"]
            assert "БЕЗ ТОЧКИ" in hint and "ГЛОБАЛЬНОГО" in hint, hint
            assert "не пишет" not in hint, "разбор потерял единственный вызов и объявил, что движений нет"
            code = _hint_steps(hint)
            ns = dict(bsl)
            exec(compile(code["2"], "<hint:2>", "exec"), ns)  # noqa: S102 → d = find_definition('Имя')
            cands = [x for x in ns["d"].get("definitions", []) if x.get("category") == "CommonModules"]
            assert cands, f"объявление глобального делегата не нашлось: {ns['d']}"
            gbody = bsl["read_procedure"](cands[0]["file"], _GLOBAL_DELEGATE_NAME)
            assert "РегистрыСведений" in gbody, gbody  # наборы есть не только у РегистрыНакопления
        finally:
            reader.close()


def test_posting_hint_ignores_commented_out_code():
    """Разбор идёт по коду с ВЫРЕЗАННЫМИ комментариями и строками (_live_code_only).

    Иначе — обе ошибки сразу: закомментированное `// СервисПроведения = ...` объявило бы
    НАСТОЯЩИЙ общий модуль «переменной» (и увело бы от рабочего маршрута), а закомментированный
    вызов родил бы делегата, которого в коде нет."""
    body = (
        "Процедура ОбработкаПроведения(Отказ, РежимПроведения)\n"
        f"    // {_DELEGATE_MODULE} = ПолучитьСервис();  // это КОММЕНТАРИЙ, а не присваивание\n"
        f"    // ФиктивныйМодуль.ФиктивныйМетод(ЭтотОбъект);\n"
        f"    {_DELEGATE_MODULE}.{_DELEGATE}(ЭтотОбъект, Отказ);\n"
        "КонецПроцедуры\n"
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(tmpdir, {"ТестДок": body})
        try:
            hint = bsl["find_register_movements"]("ТестДок")["hint"]
            assert "ОБЩИЙ МОДУЛЬ" in hint, f"комментарий превратил модуль в переменную: {hint}"
            assert "ФиктивныйМетод" not in hint, f"закомментированный вызов стал делегатом: {hint}"
            code = _hint_steps(hint)
            ns = dict(bsl)
            exec(compile(code["2"], "<hint:2>", "exec"), ns)  # noqa: S102
            dbody = eval(compile(code["3"], "<hint:3>", "eval"), ns)  # noqa: S307
            assert dbody and "СоздатьНаборЗаписей" in dbody, dbody
        finally:
            reader.close()


def test_posting_hint_says_plainly_when_the_handler_writes_nothing():
    """Пустой обработчик — ЗАКОННЫЙ исход, а не ошибка: сигнал утверждает ровно две вещи и про
    форму тела не знает ничего. Разбор обязан сказать это прямо, а не выдумывать делегата."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(
            tmpdir,
            {"ТестДок": "Процедура ОбработкаПроведения(Отказ, РежимПроведения)\nКонецПроцедуры\n"},
        )
        try:
            res = bsl["find_register_movements"]("ТестДок")
            assert res.get("posting_handler_present") is True, res
            hint = res["hint"]
            assert "не пишет" in hint and "ЗАКОННЫЙ" in hint, hint
            # 'ДЕЛЕГАТ:' — метка НАЙДЕННОГО делегата (слово «ДЕЛЕГАТА» есть и в общем хвосте).
            assert "ДЕЛЕГАТ:" not in hint, f"разбор выдумал делегата на пустом теле: {hint}"
            assert "ВЫЗОВ" not in hint, f"разбор выдумал вызов на пустом теле: {hint}"
        finally:
            reader.close()


def test_profile_registers_section_carries_posting_handler_signal():
    """ДЕФОЛТНЫЙ маршрут агента — get_object_profile, он читает reader напрямую.
    Сигнал обязан быть и там, иначе e2e-сбой сохраняется на основном пути."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(tmpdir, {"ТестДок": _DELEGATED})
        try:
            sec = bsl["get_object_profile"]("ТестДок", sections=["registers"])["sections"]["registers"]
            # status == "empty" (не "ok"!): контракт секции — "empty" if total == 0.
            # Ключевое — что это НЕ "unavailable" (ранний return).
            assert sec["status"] == "empty", sec
            assert sec["summary"]["code_registers"] == 0
            assert sec["summary"].get("posting_handler_present") is True, sec
            assert "ОбработкаПроведения" in (sec.get("hint") or ""), sec
        finally:
            reader.close()


def test_profile_registers_reads_at_most_the_one_candidate_module():
    """Профиль ПОДТВЕРЖДАЕТ обработчика по живому модулю — иначе он врал бы: билдер кладёт
    закомментированную `// Процедура ОбработкаПроведения()` в таблицу methods (неякорный
    .search() по сырой строке), и index-only профиль выставил бы ложный сигнал, разойдясь с
    find_register_movements НА ОДНОМ И ТОМ ЖЕ свежем индексе.

    Чтение гейтится ДВАЖДЫ (code_registers==0 И индекс указал на модуль), поэтому контракт
    секции — не «ноль чтений», а «ноль чтений на общем пути; открываются ТОЛЬКО
    модули-кандидаты ЭТОГО документа» (у типового документа кандидат один — main; при main+CFE
    их может быть несколько, и цикл идёт по ним, пока не подтвердится).

    Шпион ставится ИНЪЕКЦИЕЙ read_file_fn: `_ext_read_file` — вложенная функция
    make_bsl_helpers, monkeypatch по module-level имени дал бы AttributeError и всё равно
    не подменил бы уже созданную замыкание-ссылку."""
    with tempfile.TemporaryDirectory() as tmpdir:
        reads: list[str] = []
        bsl, reader = _make_posting_env(tmpdir, {"ТестДок": _DELEGATED}, reads=reads)
        try:
            reads.clear()  # отбросить чтения, сделанные при сборке индекса/фикстуры
            sec = bsl["get_object_profile"]("ТестДок", sections=["registers"])["sections"]["registers"]
            assert sec["summary"].get("posting_handler_present") is True, sec
            bsl_reads = [p.replace("\\", "/") for p in reads if p.endswith(".bsl")]
            # читаются ТОЛЬКО ObjectModule'ы самого документа — чужих модулей секция не трогает
            assert bsl_reads, "профиль обязан подтвердить обработчика по живому модулю"
            assert all("Documents/ТестДок/" in p for p in bsl_reads), bsl_reads
            assert len(bsl_reads) == 1, f"у документа один кандидат — лишние чтения: {bsl_reads}"
        finally:
            reader.close()


def test_profile_registers_does_not_read_files_when_no_handler_in_index():
    """Общий путь — НОЛЬ чтений. Документ с реальными движениями: сигнала быть не может,
    значит и открывать модуль незачем (дешёвый index-отсев отрабатывает раньше)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        reads: list[str] = []
        bsl, reader = _make_posting_env(tmpdir, {"ТестДок": _WITH_MOVEMENTS}, reads=reads)
        try:
            reads.clear()
            sec = bsl["get_object_profile"]("ТестДок", sections=["registers"])["sections"]["registers"]
            assert sec["summary"]["code_registers"] == 1, sec
            assert "posting_handler_present" not in sec["summary"], sec
            assert [p for p in reads if p.endswith(".bsl")] == [], f"профиль читал модули зря: {reads}"
        finally:
            reader.close()


def test_profile_registers_no_false_signal_for_commented_out_handler():
    """КЛЮЧЕВОЙ кейс расхождения: билдер индексирует закомментированную процедуру как метод,
    поэтому index-only профиль выставлял ложный posting_handler_present=True — на СВЕЖЕМ
    индексе, при том что find_register_movements корректно молчал. Оба маршрута обязаны
    отвечать ОДИНАКОВО."""
    body = "// Процедура ОбработкаПроведения(Отказ, РежимПроведения)\n// КонецПроцедуры\n"
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(tmpdir, {"ТестДок": body})
        try:
            sec = bsl["get_object_profile"]("ТестДок", sections=["registers"])["sections"]["registers"]
            assert "posting_handler_present" not in sec["summary"], sec
            assert not (sec.get("hint") or ""), sec
            # и хелпер — так же (никакого расхождения маршрутов)
            res = bsl["find_register_movements"]("ТестДок")
            assert "posting_handler_present" not in res, res
        finally:
            reader.close()


def test_profile_signal_on_globally_empty_movements_table():
    """Ветка `rows is None` (таблица register_movements ГЛОБАЛЬНО пуста) реализуется
    отдельно — значит обязана и тестироваться. Проверяем ровно её контракт:
      * status остаётся "unavailable" (мы НЕ вправе заявлять, что движений 0);
      * code_registers в summary НЕ появляется;
      * сигнал лежит В SUMMARY (туда ведут рецепты), а не в top-level;
      * подтверждение обработчика читает РОВНО один модуль (см.
        test_profile_registers_reads_at_most_the_one_candidate_module).
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        reads: list[str] = []
        bsl, reader = _make_posting_env(tmpdir, {"ТестДок": _DELEGATED}, reads=reads, with_movements_doc=False)
        try:
            reads.clear()
            sec = bsl["get_object_profile"]("ТестДок", sections=["registers"])["sections"]["registers"]
            assert sec["status"] == "unavailable", sec
            assert sec["summary"].get("posting_handler_present") is True, sec
            assert "code_registers" not in sec["summary"], sec  # 0 регистров НЕ заявляем
            assert "ОбработкаПроведения" in (sec.get("hint") or ""), sec
            assert len([p for p in reads if p.endswith(".bsl")]) == 1, f"лишние чтения: {reads}"
        finally:
            reader.close()


def test_posting_handler_signal_not_faked_by_homonym_document():
    """find_by_type матчит ПОДСТРОКОЙ — 'ТестДок' находит и 'ТестДокАрхив'.
    Обработчик соседа НЕ должен выставлять сигнал нашему документу."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(
            tmpdir,
            {
                "ТестДок": "Процедура ПередЗаписью(Отказ)\nКонецПроцедуры\n",  # обработчика НЕТ
                "ТестДокАрхив": _DELEGATED,  # обработчик есть у ОМОНИМА
            },
        )
        try:
            res = bsl["find_register_movements"]("ТестДок")
            assert "posting_handler_present" not in res, res
        finally:
            reader.close()


def test_posting_deny_hint_wins_over_handler_signal_in_helper():
    """У find_register_movements есть live-XML, поэтому Posting=Deny там ПРИОРИТЕТНЕЕ
    handler-сигнала (движений нет В ПРИНЦИПЕ). В профиле такого обещания НЕТ — posting
    в индексе отсутствует, а live-XML нарушил бы no-live контракт."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(tmpdir, {"ТестДок": _DELEGATED}, posting={"ТестДок": "Deny"})
        try:
            res = bsl["find_register_movements"]("ТестДок")
            assert res.get("is_postable") is False, res
            assert "непроводим" in res["hint"], res  # hint от Posting=Deny, не handler-нудж
            assert "posting_handler_present" not in res, res
        finally:
            reader.close()


def test_posting_deny_wins_even_when_manager_tables_not_empty():
    """ГЛАВНЫЙ кейс: _maybe_add_postability_hint гейтится ПОЛНОЙ пустотой результата
    (code_registers И erp_mechanisms И manager_tables И adapted_registers). Наш сценарий
    допускает НЕПУСТЫЕ manager_tables — именно так выглядит боевой документ. Значит для
    Deny-документа с manager_tables старый hint НЕ выставится, и handler-сигнал соврал бы
    вопреки обещанному приоритету Deny. Постановка обязана проверяться в самом
    _maybe_add_posting_handler_hint."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(
            tmpdir,
            {"ТестДок": _DELEGATED},
            posting={"ТестДок": "Deny"},
            manager_tables={"ТестДок": ["ТаблицаОдин"]},  # → manager_tables != []
        )
        try:
            res = bsl["find_register_movements"]("ТестДок")
            assert res["manager_tables"], res  # предусловие кейса: список НЕ пуст
            assert res["code_registers"] == []
            assert res.get("is_postable") is False, res
            assert "posting_handler_present" not in res, res  # сигнал НЕ выставлен
            assert "непроводим" in res["hint"], res
        finally:
            reader.close()


def test_posting_deny_wins_when_direct_code_movements_are_present():
    """Direct rows remain useful static provenance, but Deny must mark them as
    unreachable before an agent presents them as runtime posting movements."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(
            tmpdir,
            {"ТестДок": _WITH_MOVEMENTS},
            posting={"ТестДок": "Deny"},
        )
        try:
            res = bsl["find_register_movements"]("ТестДок")
            assert [row["name"] for row in res["code_registers"]] == ["ТоварыНаСкладах"], res
            assert res.get("posting") == "Deny", res
            assert res.get("is_postable") is False, res
            assert "статические ссылки" in res["hint"], res
            assert "posting_handler_present" not in res, res
        finally:
            reader.close()


def test_no_posting_handler_signal_when_movements_found():
    """Нудж не шумит, когда движения реально найдены."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(tmpdir, {"ТестДок": _WITH_MOVEMENTS})
        try:
            res = bsl["find_register_movements"]("ТестДок")
            assert [r["name"] for r in res["code_registers"]] == ["ТоварыНаСкладах"]
            assert "posting_handler_present" not in res
        finally:
            reader.close()


def test_spaced_direct_movement_never_becomes_false_empty_handler_signal():
    """BSL permits whitespace around ``.``; the no-rebuild live guard must not
    inherit the builder regex's known blind spot and claim that the handler writes nothing."""
    body = (
        "Процедура ОбработкаПроведения(Отказ, РежимПроведения)\n"
        "    Движения . ТоварыНаСкладах.Записывать = Истина;\n"
        "КонецПроцедуры\n"
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(tmpdir, {"ТестДок": body}, with_movements_doc=False)
        try:
            res = bsl["find_register_movements"]("ТестДок")
            assert [row["name"] for row in res["code_registers"]] == ["ТоварыНаСкладах"], res
            assert "posting_handler_present" not in res, res
            assert "судя по коду, движений он не пишет" not in (res.get("hint") or ""), res

            section = bsl["get_object_profile"]("ТестДок", sections=["registers"])["sections"]["registers"]
            assert "posting_handler_present" not in section["summary"], section
            assert "судя по коду, движений он не пишет" not in (section.get("hint") or ""), section
        finally:
            reader.close()


def test_register_movements_reg_contract_mentions_posting_handler_signal():
    """Бизнес-рецепт и стратегии обновлены, а зарегистрированный контракт самого хелпера —
    нет. Агент, спросивший rlm_help(helpers=['find_register_movements']), о новом сигнале не
    узнает. Сигнатура И recipe в _reg обязаны его нести (условной формулировкой).

    Проверяем ЧЕРЕЗ РЕЕСТР, а не через help(): sandbox-`help(task)` возвращает ТОЛЬКО recipe,
    поэтому тест на help() остался бы зелёным даже с нетронутой сигнатурой — а именно `sig`
    уходит в rlm_start.available_functions и в rlm_help."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(tmpdir, {"ТестДок": _DELEGATED})
        try:
            entry = bsl["_registry"]["find_register_movements"]
            assert "posting_handler_present" in entry["sig"], entry["sig"]
            assert "posting_handler_present" in entry["recipe"], entry["recipe"]
            assert "hint" in entry["sig"], entry["sig"]
            assert "наличие реквизита\n      #     доказуемо" not in entry["recipe"], entry["recipe"]
            assert "И наличие" in entry["recipe"] and "И отсутствие" in entry["recipe"], entry["recipe"]
            assert entry["recipe"].index("is_postable") < entry["recipe"].index("for r in result['code_registers']")
        finally:
            reader.close()


def test_no_posting_handler_signal_when_index_stale_but_file_has_movements():
    """Флаг утверждает КОНЪЮНКЦИЮ: «обработчик есть» И «прямых Движения.X нет». Половины
    брались из РАЗНЫХ источников: «нет движений» — из ИНДЕКСА (снимок, rebuild opt-in),
    «обработчик есть» — по факту ЖИВОГО модуля. На отставшем индексе мы бы прочитали ровно
    тот файл, где движения ЕСТЬ, и уверенно заявили агенту, что их нет ("обращений
    `Движения.<Регистр>` в ObjectModule нет") — да ещё и услали трассировать несуществующее
    делегирование. Молчание лучше ложного утверждения."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(tmpdir, {"ТестДок": _DELEGATED})
        try:
            # Индекс собран по версии БЕЗ движений; теперь файл их получает (индекс отстал).
            obj = os.path.join(tmpdir, "cf", "Documents", "ТестДок", "Ext", "ObjectModule.bsl")
            with open(obj, "w", encoding="utf-8") as f:
                f.write(_WITH_MOVEMENTS)
            res = bsl["find_register_movements"]("ТестДок")
            assert res["code_registers"] == []  # индекс — снимок, он отстал: это его контракт
            assert "posting_handler_present" not in res, res  # но ЛГАТЬ про модуль мы не вправе
            assert "hint" not in res, res
        finally:
            reader.close()


def test_no_posting_handler_signal_on_cyrillic_case_mismatch():
    """Индекс ищет документ через SQL `COLLATE NOCASE`, который сворачивает ТОЛЬКО ASCII
    (в bsl_index это уже зафиксировано: "COLLATE NOCASE doesn't work for Cyrillic"), а
    _find_posting_handler_module сравнивает через .casefold() — Unicode. На lowercase-вводе
    половина «нет движений» оказывалась пустой (SQL не нашёл документ), а половина
    «обработчик есть» — истинной, и флаг лгал на СВЕЖЕМ индексе."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(tmpdir, {"РеализацияТоваровУслуг": _WITH_MOVEMENTS})
        try:
            exact = bsl["find_register_movements"]("РеализацияТоваровУслуг")
            assert [r["name"] for r in exact["code_registers"]] == ["ТоварыНаСкладах"]
            assert "posting_handler_present" not in exact, exact

            lower = bsl["find_register_movements"]("реализациятоваровуслуг")
            # Движения у документа ЕСТЬ — сигнал не имеет права появиться ни при каком регистре.
            assert "posting_handler_present" not in lower, lower
        finally:
            reader.close()


def test_find_event_subscriptions_bare_name_tolerates_padding():
    """Типизированная ветка звала _normalize_object_ref(object_name.strip()), а голая —
    _strip_meta_prefix(object_name) БЕЗ strip: ' Реализация ' не матчился ни точно, ни
    подстрокой. Обе ветки обязаны вести себя одинаково."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_subs_env(
            tmpdir,
            [
                ("ПодпискаТочная", ["DocumentObject.РеализацияТоваровУслуг"]),
                ("ПодпискаUniversal", []),
            ],
        )
        # universal-подписки включаются ВСЕГДА, в обеих ветках — это контракт, а не артефакт.
        padded = bsl["find_event_subscriptions"](" РеализацияТоваровУслуг ")
        assert {r["name"] for r in padded} == {"ПодпискаТочная", "ПодпискаUniversal"}, padded
        typed = bsl["find_event_subscriptions"](" Документ.РеализацияТоваровУслуг ")
        assert {r["name"] for r in typed} == {"ПодпискаТочная", "ПодпискаUniversal"}, typed
        assert {r["name"]: r["scope"] for r in typed}["ПодпискаТочная"] == "exact", typed


def test_find_event_subscriptions_unrecognized_prefix_does_not_poison_ref():
    """`_normalize_object_ref` при неудаче канонизации отдаёт вход ВЕРБАТИМ, поэтому одной
    проверки "." мало: любой dotted-ввод уходил в category-aware ветку, а у неё НЕТ
    partial-фолбэка → выдача схлопывалась до одних universal.

    'РегламентноеЗадание.' выбран НЕ случайно: это ровно зазор между двумя таблицами
    префиксов — он ЕСТЬ в _META_TYPE_PREFIXES (его срезает _strip_meta_prefix, и до v1.28.0
    такой ввод работал), но его НЕТ в _RU_META_PREFIXES, поэтому canonicalize_type_ref его не
    канонизирует. Без гейта распознанности это был бы прямой регресс."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_subs_env(tmpdir, [("ПодпискаПартии", ["DocumentObject.Партии"])])
        bare = bsl["find_event_subscriptions"]("Партии")
        assert [r["name"] for r in bare] == ["ПодпискаПартии"], bare
        # Нераспознанный префикс не должен обнулять выдачу — падаем в обычный матчинг по имени.
        prefixed = bsl["find_event_subscriptions"]("РегламентноеЗадание.Партии")
        assert [r["name"] for r in prefixed] == ["ПодпискаПартии"], prefixed


def test_no_posting_handler_signal_when_handler_deleted_from_live_module():
    """ОБРАТНАЯ асимметрия источников. extract_procedures при live-fill только ДОБАВЛЯЕТ
    пропущенные индексом методы и НЕ убирает исчезнувшие, поэтому на отставшем индексе
    половина «обработчик есть» приходила бы из снимка, хотя из живого модуля процедуру уже
    удалили — и hint услал бы агента трассировать несуществующую ОбработкуПроведения.
    Обе половины конъюнкции обязаны читаться по ОДНОМУ телу модуля."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(tmpdir, {"ТестДок": _DELEGATED})
        try:
            # Индекс помнит ОбработкаПроведения; из живого модуля её удалили.
            obj = os.path.join(tmpdir, "cf", "Documents", "ТестДок", "Ext", "ObjectModule.bsl")
            with open(obj, "w", encoding="utf-8") as f:
                f.write("Процедура ПередЗаписью(Отказ)\nКонецПроцедуры\n")
            res = bsl["find_register_movements"]("ТестДок")
            assert res["code_registers"] == []
            assert "posting_handler_present" not in res, res
            assert "hint" not in res, res
        finally:
            reader.close()


def test_posting_handler_signal_recognizes_english_bsl_keywords():
    """1С поддерживает английский синтаксис, и системный парсер (BSL_PATTERNS['procedure_def'])
    принимает Procedure/Function наравне с Процедура/Функция. Подтверждение живого модуля обязано
    идти ТЕМ ЖЕ парсером: своя регулярка на Процедура|Функция дала бы false-negative — обработчик
    нашёлся бы в индексе, но перепроверка его отвергла бы, и сигнал молча пропал."""
    body = (
        "Procedure ОбработкаПроведения(Отказ, РежимПроведения)\n"
        "    ОбщийМодульУчета.ОтразитьВУчете(ЭтотОбъект, Отказ);\n"
        "EndProcedure\n"
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(tmpdir, {"ТестДок": body})
        try:
            res = bsl["find_register_movements"]("ТестДок")
            assert res["code_registers"] == []
            assert res.get("posting_handler_present") is True, res
            assert "ОбработкаПроведения" in res.get("hint", "")
        finally:
            reader.close()


def test_posting_handler_signal_survives_multiline_signature():
    """Multiline-сигнатуру индекс может пропустить — её склеивает _merge_proc_continuations
    внутри системного парсера. Живое подтверждение обязано её видеть (self-healing сохранён)."""
    body = (
        "Процедура ОбработкаПроведения(Отказ,\n"
        "        РежимПроведения)\n"
        "    ОбщийМодульУчета.ОтразитьВУчете(ЭтотОбъект, Отказ);\n"
        "КонецПроцедуры\n"
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(tmpdir, {"ТестДок": body})
        try:
            res = bsl["find_register_movements"]("ТестДок")
            assert res.get("posting_handler_present") is True, res
        finally:
            reader.close()


def test_posting_hint_multiline_parameter_shadows_a_homonymous_common_module():
    """Параметр на строке-продолжении имеет тот же приоритет, что и на первой строке.

    Иначе одноимённый живой общий модуль ошибочно объявляется фактическим получателем,
    хотя BSL разрешает имя в пользу параметра обработчика.
    """
    receiver = "СервисПроведения"
    method = "СформироватьДвижения"
    body = (
        "Процедура ОбработкаПроведения(Отказ,\n"
        f"        {receiver})\n"
        f"    {receiver}.{method}(ЭтотОбъект);\n"
        "КонецПроцедуры\n"
    )
    homonym = f"Процедура {method}(Объект) Экспорт\nКонецПроцедуры\n"
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(
            tmpdir,
            {"ТестДок": body},
            extra_common_modules={receiver: homonym},
        )
        try:
            hint = bsl["find_register_movements"]("ТестДок")["hint"]
            assert "ПЕРЕМЕННАЯ (или параметр)" in hint, hint
            assert "ЛОВУШКА" in hint, f"одноимённый модуль должен остаться только предупреждением: {hint}"
            assert f"получатель '{receiver}' это ОБЩИЙ МОДУЛЬ" not in hint, hint
        finally:
            reader.close()


def test_no_posting_handler_signal_for_commented_out_declaration():
    """Общий парсер методов применяет BSL_PATTERNS['procedure_def'] неякорным .search() к СЫРОЙ
    строке, поэтому `// Процедура ОбработкаПроведения()` он считает процедурой (и билдер кладёт
    её в таблицу methods). Опираться на это в новом сигнале НЕЛЬЗЯ: обработчика нет, а флаг
    заявил бы его наличие. Перепроверка идёт по коду с вырезанными комментариями."""
    body = "// Процедура ОбработкаПроведения(Отказ, РежимПроведения)\n// КонецПроцедуры\n"
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(tmpdir, {"ТестДок": body})
        try:
            res = bsl["find_register_movements"]("ТестДок")
            assert "posting_handler_present" not in res, res
            assert "hint" not in res, res
        finally:
            reader.close()


def test_no_posting_handler_signal_for_declaration_inside_string_literal():
    """Текст объявления внутри строкового литерала (например, в тексте запроса или сообщения)
    — тоже НЕ объявление. _scan_module вырезает строковые литералы наравне с комментариями."""
    body = 'Процедура ПередЗаписью(Отказ)\n    Т = "Процедура ОбработкаПроведения(Отказ)";\nКонецПроцедуры\n'
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(tmpdir, {"ТестДок": body})
        try:
            res = bsl["find_register_movements"]("ТестДок")
            assert "posting_handler_present" not in res, res
        finally:
            reader.close()


def test_commented_out_movement_does_not_suppress_posting_handler_signal():
    """Зеркальный кейс: `// Движения.СтарыйРегистр` — НЕ прямое обращение и глушить сигнал не
    имеет права. Индекс собран ДО появления комментария (его дописали позже), поэтому
    code_registers пуст и перепроверка реально отрабатывает; по СЫРОМУ тексту она приняла бы
    и комментарий, и текст внутри строкового литерала за движение — и отняла бы у агента верный
    нудж (обработчик есть, реальных обращений нет).

    NB: если бы индекс собирался УЖЕ с этим комментарием, билдер положил бы «СтарыйРегистр» в
    register_movements как настоящий регистр (он матчит по сырому content) — это ОТДЕЛЬНЫЙ
    пре-существующий дефект билдера, лечится только пересборкой с бампом BUILDER_VERSION."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(tmpdir, {"ТестДок": _DELEGATED})
        try:
            obj = os.path.join(tmpdir, "cf", "Documents", "ТестДок", "Ext", "ObjectModule.bsl")
            with open(obj, "w", encoding="utf-8") as f:
                f.write(
                    "Процедура ОбработкаПроведения(Отказ, РежимПроведения)\n"
                    "    // Движения.СтарыйРегистр.Записывать = Истина;\n"
                    '    Т = "Движения.РегистрИзТекстаЗапроса";\n'
                    "    ОбщийМодульУчета.ОтразитьВУчете(ЭтотОбъект, Отказ);\n"
                    "КонецПроцедуры\n"
                )
            res = bsl["find_register_movements"]("ТестДок")
            assert res["code_registers"] == []  # индекс собран ДО комментария
            assert res.get("posting_handler_present") is True, res
            assert "ОбработкаПроведения" in res.get("hint", "")
        finally:
            reader.close()


def test_profile_no_false_signal_when_index_stale_but_file_has_movements():
    """HIGH (4-й раунд): профиль подтверждал живьём ТОЛЬКО обработчика, а «движений нет»
    по-прежнему брал из индекса — и конъюнкция снова разъезжалась по источникам.

    Состояние: индекс собран по модулю с ОбработкаПроведения и БЕЗ движений; затем в живой
    файл дописали Движения.ТоварыНаСкладах. rows из индекса пусты → code_registers=0, живая
    проверка подтверждает обработчика → профиль выставлял posting_handler_present=True с
    текстом «прямых Движения.X нет», хотя они в файле ЕСТЬ. find_register_movements на том же
    состоянии молчит. Оба маршрута обязаны отвечать ОДИНАКОВО."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(tmpdir, {"ТестДок": _DELEGATED})
        try:
            obj = os.path.join(tmpdir, "cf", "Documents", "ТестДок", "Ext", "ObjectModule.bsl")
            with open(obj, "w", encoding="utf-8") as f:
                f.write(_WITH_MOVEMENTS)  # движения дописаны ПОСЛЕ сборки индекса
            sec = bsl["get_object_profile"]("ТестДок", sections=["registers"])["sections"]["registers"]
            assert sec["summary"]["code_registers"] == 0, sec  # индекс отстал — это его контракт
            assert "posting_handler_present" not in sec["summary"], sec  # но ЛГАТЬ нельзя
            assert not (sec.get("hint") or ""), sec
            # и хелпер — так же: никакого расхождения маршрутов
            res = bsl["find_register_movements"]("ТестДок")
            assert "posting_handler_present" not in res, res
        finally:
            reader.close()


def test_find_based_on_documents_metadata_union_is_not_silently_capped():
    """Ридер по умолчанию отдаёт не больше 1000 строк (limit=1000 — контракт agent-facing
    выдачи). Metadata-union внутри find_based_on_documents ВНУТРЕННИЙ и обязан быть ПОЛНЫМ:
    на дефолте хвост оснований молча исчез бы из can_create_from_here, а тихое усечение
    читается агентом как «это всё». Лимит берётся по фактическому счёту."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader, db_path = _make_based_on_index_fixture(tmpdir)
        try:
            n = 1200  # ЗАВЕДОМО больше дефолтного лимита ридера
            _seed_based_on(
                db_path,
                [(f"Основание{i:04d}", "Catalogs", "Document.ВходящееПисьмо") for i in range(n)],
            )
            result = bsl["find_based_on_documents"]("ВходящееПисьмо")
            via_meta = {d["document"] for d in result["can_create_from_here"] if d.get("via") == "metadata"}
            assert len(via_meta) == n, f"хвост усечён: получено {len(via_meta)} из {n}"
            assert "Основание1199" in via_meta, "потеряна последняя строка — сработал дефолтный cap"
        finally:
            reader.close()


def test_find_based_on_documents_unknown_count_marks_fallback_cap_partial(monkeypatch):
    """Без authoritative count строка на границе fallback-лимита не доказывает полноту."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader, db_path = _make_based_on_index_fixture(tmpdir)
        try:
            _seed_based_on(
                db_path,
                [(f"Основание{i:04d}", "Catalogs", "Document.ВходящееПисьмо") for i in range(1200)],
            )
            monkeypatch.setattr(reader, "count_metadata_references", lambda *a, **k: None)

            result = bsl["find_based_on_documents"]("ВходящееПисьмо")
            via_meta = [d for d in result["can_create_from_here"] if d.get("via") == "metadata"]
            assert len(via_meta) == 1000
            assert result["partial"] is True
            assert result["_meta"]["reason"] == "metadata_references_incomplete"
        finally:
            reader.close()


@pytest.mark.parametrize("with_index", [False, True])
def test_event_subscription_dotless_typed_prefix_is_the_empty_overview(with_index):
    """Typed prefix without an object behaves like the empty query in any letter case."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_subs_env(
            tmpdir,
            [
                ("ПодпискаДок", ["DocumentObject.ДокА"]),
                ("ПодпискаСпр", ["CatalogObject.СпрА"]),
                ("ПодпискаОбщая", []),
            ],
            with_index=with_index,
        )
        try:
            for object_name in ("Документ.", "документ.", "Document.", "DOCUMENT."):
                rows = bsl["find_event_subscriptions"](object_name)
                assert {row["name"] for row in rows} == {"ПодпискаДок", "ПодпискаСпр", "ПодпискаОбщая"}
                assert all("scope" not in row and "source_types" not in row for row in rows)
        finally:
            if reader:
                reader.close()


def test_typed_non_document_based_on_without_metadata_index_is_partial():
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_full_fixture(tmpdir)
        result = bsl["find_based_on_documents"]("Справочник.Контрагент")
        assert result["partial"] is True
        assert result["_meta"]["reason"] == "metadata_references_unavailable"
        assert "пересобери индекс" in result["hint"]


@pytest.mark.parametrize("failure", ["missing", "error"])
def test_typed_non_document_based_on_old_or_failed_reader_is_partial(monkeypatch, failure):
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader, _db_path = _make_based_on_index_fixture(tmpdir)
        try:
            if failure == "missing":
                monkeypatch.setattr(reader, "find_metadata_references", lambda *a, **k: None)
            else:

                def fail(*_args, **_kwargs):
                    raise RuntimeError("reader failed")

                monkeypatch.setattr(reader, "find_metadata_references", fail)
            result = bsl["find_based_on_documents"]("Справочник.Контрагент")
            assert result["partial"] is True
            assert result["_meta"]["reason"] == "metadata_references_unavailable"
        finally:
            reader.close()


def test_empty_functional_option_overview_keeps_twenty_module_budget(tmp_path):
    for i in range(30):
        path = tmp_path / "CommonModules" / f"Модуль{i:02d}" / "Ext" / "Module.bsl"
        path.parent.mkdir(parents=True)
        path.write_text(
            f'Процедура П() Экспорт\n    ПолучитьФункциональнуюОпцию("Опция{i:02d}");\nКонецПроцедуры\n',
            encoding="utf-8",
        )
    (tmp_path / "Configuration.xml").write_text("<Configuration/>", encoding="utf-8")
    helpers, resolve_safe = make_helpers(str(tmp_path))
    bsl = make_bsl_helpers(
        base_path=str(tmp_path),
        resolve_safe=resolve_safe,
        read_file_fn=helpers["read_file"],
        grep_fn=helpers["grep"],
        glob_files_fn=helpers["glob_files"],
        format_info=detect_format(str(tmp_path)),
    )
    result = bsl["find_functional_options"]("")
    assert len(result["code_options"]) == 20

    page = bsl["find_functional_options"]("", limit=50)
    assert len(page["code_options"]) == 20
    assert page["total"] == 20  # lower bound over the scanned code slice
    assert page["has_more"] is False  # no more rows inside that known slice
    assert page["partial"] is True
    assert page["_meta"] == {
        "reason": "code_scan_budget",
        "code_modules_scanned": 20,
        "code_modules_total": 30,
        "total_scope": "all_xml_plus_scanned_code",
        "hint": "Пустой обзор проверяет первые 20 BSL-модулей; укажи object_name для полного code-скана.",
    }


def test_english_register_records_and_record_factory_are_recognized():
    direct = "Procedure ОбработкаПроведения(Cancel, Mode)\n    RegisterRecords.Sales.Add();\nEndProcedure\n"
    factory = (
        "Procedure ОбработкаПроведения(Cancel, Mode)\n"
        "    Set = InformationRegisters.Prices.CreateRecordSet();\n"
        "    Set.Write();\n"
        "EndProcedure\n"
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(tmpdir, {"EnglishDirect": direct, "EnglishFactory": factory})
        try:
            direct_result = bsl["find_register_movements"]("EnglishDirect")
            assert [row["name"] for row in direct_result["code_registers"]] == ["Sales"]
            assert "posting_handler_present" not in direct_result
            profile = bsl["get_object_profile"]("EnglishDirect", sections=["registers"])
            profile_registers = profile["sections"]["registers"]
            assert profile_registers["items"] == [{"register": "Sales", "source": "code"}]
            assert profile_registers["_meta"]["source"] == "mixed"

            factory_result = bsl["find_register_movements"]("EnglishFactory")
            assert factory_result["posting_handler_present"] is True
            assert "InformationRegisters.Prices" in factory_result["hint"]
            assert "Set.Write" not in factory_result["hint"]
        finally:
            reader.close()


def test_boolean_keywords_and_raise_are_not_dotless_global_calls():
    body = (
        "Процедура ОбработкаПроведения(Отказ, РежимПроведения)\n"
        "    И(А); ИЛИ(Б); НЕ(В); And(A); Or(B); ВызватьИсключение(Текст); Raise(Text);\n"
        "КонецПроцедуры\n"
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(tmpdir, {"ТестДок": body})
        try:
            hint = bsl["find_register_movements"]("ТестДок")["hint"]
            for name in ("И", "ИЛИ", "НЕ", "And", "Or", "ВызватьИсключение", "Raise"):
                assert f"ВЫЗОВ БЕЗ ТОЧКИ {name}" not in hint
        finally:
            reader.close()


def test_common_module_created_after_index_is_resolved_by_exact_live_probe():
    receiver = "СвежийСервисПроведения"
    call_receiver = receiver.lower()
    method = "СформироватьДвижения"
    body = (
        "Процедура ОбработкаПроведения(Отказ, РежимПроведения)\n"
        f"    {call_receiver}.{method}(ЭтотОбъект);\n"
        "КонецПроцедуры\n"
    )
    module = f"Процедура {method}(Объект) Экспорт\nКонецПроцедуры\n"
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(
            tmpdir,
            {"ТестДок": body},
            post_index_common_modules={receiver: module},
            index_backed_glob=True,
        )
        try:
            assert not any(row["object_name"] == receiver for row in reader.get_all_modules())
            hint = bsl["find_register_movements"]("ТестДок")["hint"]
            assert f"получатель '{call_receiver}' это ОБЩИЙ МОДУЛЬ" in hint
        finally:
            reader.close()


def test_profile_compact_pager_restarts_in_full_route_without_losing_names():
    names = [f"Регистр{i:02d}" for i in range(45)]
    body = (
        "Процедура ОбработкаПроведения(Отказ, РежимПроведения)\n"
        + "".join(
            f"    Набор{i:02d} = РегистрыСведений.{name}.СоздатьНаборЗаписей();\n    Набор{i:02d}.Записать();\n"
            for i, name in enumerate(names)
        )
        + "КонецПроцедуры\n"
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(tmpdir, {"ТестДок": body})
        try:
            section = bsl["get_object_profile"]("ТестДок", sections=["registers"])["sections"]["registers"]
            profile_hint = section["hint"]
            first_offset_match = re.search(r"posting_calls_offset=(\d+)", profile_hint)
            assert first_offset_match, profile_hint
            first_offset = int(first_offset_match.group(1))
            assert first_offset == 0
            assert "posting_calls_offset=40" not in profile_hint

            first_page = bsl["find_register_movements"]("ТестДок", posting_calls_offset=first_offset)["hint"]
            next_offset_match = re.search(r"posting_calls_offset=(\d+)", first_page)
            assert next_offset_match, first_page
            second_page = bsl["find_register_movements"](
                "ТестДок", posting_calls_offset=int(next_offset_match.group(1))
            )["hint"]
            pages = [first_page, second_page]
            combined = "\n".join(pages)
            assert all(f"РегистрыСведений.{name}" in combined for name in names)
        finally:
            reader.close()


def test_no_git_register_route_counts_post_index_live_catalog():
    register_name = "РегистрИзСвежегоМодуля"
    body = (
        "Процедура ОбработкаПроведения(Отказ, РежимПроведения)\n"
        f"    Набор = РегистрыСведений.{register_name}.СоздатьНаборЗаписей();\n"
        "    Набор.Записать();\n"
        "КонецПроцедуры\n"
    )
    fresh_module = (
        "Процедура Писатель() Экспорт\n"
        f"    Набор = РегистрыСведений.{register_name}.СоздатьНаборЗаписей();\n"
        "КонецПроцедуры\n"
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_posting_env(
            tmpdir,
            {"ТестДок": body},
            post_index_common_modules={"СвежийПисатель": fresh_module},
            index_backed_glob=True,
        )
        try:
            hint = bsl["find_register_movements"]("ТестДок")["hint"]
            match = re.search(r"safe_grep\('ИмяРегистра', max_files=(\d+)\)", hint)
            assert match, hint
            hits = bsl["safe_grep"](register_name, max_files=int(match.group(1)))
            assert any("СвежийПисатель" in row["file"] for row in hits), hits
        finally:
            reader.close()
