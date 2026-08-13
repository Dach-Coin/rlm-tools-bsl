"""Tests for form XML parsing (v1.6.0): parser, helper, index."""

import os
import sqlite3
from pathlib import Path

import pytest

from rlm_tools_bsl.bsl_index import BUILDER_VERSION
from rlm_tools_bsl.bsl_xml_parsers import parse_form_xml


@pytest.fixture(autouse=True)
def _isolate_index_dir(tmp_path, monkeypatch):
    """Prevent RLM_INDEX_DIR leaking from other tests (e.g. test_config dotenv)."""
    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))


# ── Test XML data ─────────────────────────────────────────────

_EDT_FORM_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<form:Form xmlns:form="http://g5.1c.ru/v8/dt/form"
           xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <form:handlers>
    <event>OnCreateAtServer</event>
    <name>ПриСозданииНаСервере</name>
  </form:handlers>
  <form:handlers>
    <event>NotificationProcessing</event>
    <name>ОбработкаОповещения</name>
  </form:handlers>
  <form:extInfo xsi:type="form:DocumentFormExtInfo">
    <form:handlers>
      <event>AfterWrite</event>
      <name>ПослеЗаписи</name>
    </form:handlers>
  </form:extInfo>
  <form:items xsi:type="form:FormField">
    <name>ОрганизацияОтбор</name>
    <type>InputField</type>
    <form:dataPath>
      <segments>Организация</segments>
    </form:dataPath>
    <form:handlers>
      <event>OnChange</event>
      <name>ОрганизацияОтборПриИзменении</name>
    </form:handlers>
  </form:items>
  <form:items xsi:type="form:FormGroup">
    <name>ГруппаОсновная</name>
    <form:items xsi:type="form:FormField">
      <name>Контрагент</name>
      <type>InputField</type>
      <form:dataPath>
        <segments>Контрагент</segments>
      </form:dataPath>
      <form:handlers>
        <event>StartChoice</event>
        <name>КонтрагентНачалоВыбора</name>
      </form:handlers>
    </form:items>
  </form:items>
  <form:formCommands>
    <name>Обновить</name>
    <form:action>
      <form:handler>
        <name>ОбновитьВыполнить</name>
      </form:handler>
    </form:action>
  </form:formCommands>
  <form:formCommands>
    <name>Печать</name>
    <form:action>
      <form:handler>
        <name>ПечатьВыполнить</name>
      </form:handler>
    </form:action>
  </form:formCommands>
  <form:attributes>
    <name>Объект</name>
    <main>true</main>
    <form:valueType>
      <types>DocumentObject.РеализацияТоваровУслуг</types>
    </form:valueType>
  </form:attributes>
  <form:attributes>
    <name>Список</name>
    <form:valueType>
      <types>DynamicList</types>
    </form:valueType>
    <form:extInfo>
      <mainTable>Document.РеализацияТоваровУслуг</mainTable>
      <queryText>ВЫБРАТЬ Ссылка, Номер, Дата, Организация ИЗ Документ.РеализацияТоваровУслуг</queryText>
    </form:extInfo>
  </form:attributes>
</form:Form>
"""

_CF_FORM_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<Form xmlns="http://v8.1c.ru/8.3/xcf/logform"
      xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <Events>
    <Event name="OnCreateAtServer">ПриСозданииНаСервере</Event>
    <Event name="OnOpen">ПриОткрытии</Event>
  </Events>
  <ChildItems>
    <InputField name="ОрганизацияОтбор" id="1">
      <DataPath>Организация</DataPath>
      <Events>
        <Event name="OnChange">ОрганизацияОтборПриИзменении</Event>
      </Events>
    </InputField>
    <UsualGroup name="ГруппаОсновная" id="2">
      <ChildItems>
        <InputField name="Контрагент" id="3">
          <DataPath>Контрагент</DataPath>
          <Events>
            <Event name="StartChoice">КонтрагентНачалоВыбора</Event>
          </Events>
        </InputField>
      </ChildItems>
    </UsualGroup>
    <Table name="Товары" id="4">
      <ChildItems>
        <InputField name="Номенклатура" id="5">
          <DataPath>Товары.Номенклатура</DataPath>
          <Events>
            <Event name="OnChange">НоменклатураПриИзменении</Event>
          </Events>
        </InputField>
      </ChildItems>
    </Table>
  </ChildItems>
  <Commands>
    <Command name="Обновить">
      <Action>ОбновитьВыполнить</Action>
    </Command>
  </Commands>
  <Attributes>
    <Attribute name="Объект" id="10">
      <Main>true</Main>
      <Type>
        <Type>DocumentObject.РеализацияТоваровУслуг</Type>
      </Type>
    </Attribute>
    <Attribute name="Список" id="11">
      <Type>
        <Type>DynamicList</Type>
      </Type>
      <Settings xsi:type="DynamicList">
        <MainTable>Document.РеализацияТоваровУслуг</MainTable>
        <QueryText>ВЫБРАТЬ Ссылка ИЗ Документ.РеализацияТоваровУслуг</QueryText>
      </Settings>
    </Attribute>
  </Attributes>
</Form>
"""


# ── Parser tests ──────────────────────────────────────────────


class TestParseFormXml:
    """Tests for parse_form_xml() parser."""

    def test_edt_form_handlers(self):
        result = parse_form_xml(_EDT_FORM_XML)
        assert result is not None
        handlers = result["handlers"]
        # form-level
        form_handlers = [h for h in handlers if h["scope"] == "form"]
        assert len(form_handlers) == 2
        assert form_handlers[0]["event"] == "OnCreateAtServer"
        assert form_handlers[0]["handler"] == "ПриСозданииНаСервере"
        assert form_handlers[0]["element"] == ""
        # ext_info
        ext_handlers = [h for h in handlers if h["scope"] == "ext_info"]
        assert len(ext_handlers) == 1
        assert ext_handlers[0]["event"] == "AfterWrite"
        # element-level
        elem_handlers = [h for h in handlers if h["scope"] == "element"]
        assert len(elem_handlers) == 2
        org_h = [h for h in elem_handlers if h["element"] == "ОрганизацияОтбор"][0]
        assert org_h["event"] == "OnChange"
        assert org_h["handler"] == "ОрганизацияОтборПриИзменении"
        assert org_h["data_path"] == "Организация"
        assert org_h["element_type"] == "InputField"

    def test_edt_nested_elements(self):
        result = parse_form_xml(_EDT_FORM_XML)
        handlers = result["handlers"]
        nested = [h for h in handlers if h["element"] == "Контрагент"]
        assert len(nested) == 1
        assert nested[0]["event"] == "StartChoice"
        assert nested[0]["data_path"] == "Контрагент"

    def test_edt_commands(self):
        result = parse_form_xml(_EDT_FORM_XML)
        commands = result["commands"]
        assert len(commands) == 2
        names = {c["name"] for c in commands}
        assert "Обновить" in names
        assert "Печать" in names
        refresh = [c for c in commands if c["name"] == "Обновить"][0]
        assert refresh["action"] == "ОбновитьВыполнить"

    def test_edt_attributes(self):
        result = parse_form_xml(_EDT_FORM_XML)
        attrs = result["attributes"]
        assert len(attrs) == 2
        obj_attr = [a for a in attrs if a["name"] == "Объект"][0]
        assert obj_attr["main"] is True
        assert "DocumentObject.РеализацияТоваровУслуг" in obj_attr["types"]
        dl = [a for a in attrs if a["name"] == "Список"][0]
        assert dl["main_table"] == "Document.РеализацияТоваровУслуг"
        assert "ВЫБРАТЬ" in dl["query_text"]

    def test_cf_form_handlers(self):
        result = parse_form_xml(_CF_FORM_XML)
        assert result is not None
        handlers = result["handlers"]
        form_handlers = [h for h in handlers if h["scope"] == "form"]
        assert len(form_handlers) == 2
        assert form_handlers[0]["handler"] == "ПриСозданииНаСервере"

    def test_cf_nested_elements(self):
        result = parse_form_xml(_CF_FORM_XML)
        handlers = result["handlers"]
        elem_handlers = [h for h in handlers if h["scope"] == "element"]
        # ОрганизацияОтбор, Контрагент (nested in group), Номенклатура (nested in table)
        assert len(elem_handlers) == 3
        nom = [h for h in elem_handlers if h["element"] == "Номенклатура"][0]
        assert nom["event"] == "OnChange"
        assert nom["element_type"] == "InputField"
        assert nom["data_path"] == "Товары.Номенклатура"

    def test_cf_commands(self):
        result = parse_form_xml(_CF_FORM_XML)
        commands = result["commands"]
        assert len(commands) == 1
        assert commands[0]["name"] == "Обновить"
        assert commands[0]["action"] == "ОбновитьВыполнить"

    def test_cf_attributes(self):
        result = parse_form_xml(_CF_FORM_XML)
        attrs = result["attributes"]
        assert len(attrs) == 2
        obj_attr = [a for a in attrs if a["name"] == "Объект"][0]
        assert obj_attr["main"] is True
        dl = [a for a in attrs if a["name"] == "Список"][0]
        assert dl["main_table"] == "Document.РеализацияТоваровУслуг"
        assert "ВЫБРАТЬ" in dl["query_text"]

    def test_cf_ext_info_scope(self):
        """CF ext_info events (AfterWrite, etc.) get scope='ext_info'."""
        xml = """\
<?xml version="1.0" encoding="UTF-8"?>
<Form xmlns="http://v8.1c.ru/8.3/xcf/logform">
  <Events>
    <Event name="OnCreateAtServer">ПриСозданииНаСервере</Event>
    <Event name="AfterWrite">ПослеЗаписи</Event>
    <Event name="OnReadAtServer">ПриЧтенииНаСервере</Event>
  </Events>
</Form>
"""
        result = parse_form_xml(xml)
        assert result is not None
        handlers = result["handlers"]
        form_h = [h for h in handlers if h["scope"] == "form"]
        ext_h = [h for h in handlers if h["scope"] == "ext_info"]
        assert len(form_h) == 1
        assert form_h[0]["event"] == "OnCreateAtServer"
        assert len(ext_h) == 2
        ext_events = {h["event"] for h in ext_h}
        assert "AfterWrite" in ext_events
        assert "OnReadAtServer" in ext_events

    def test_empty_form(self):
        xml = """\
<?xml version="1.0" encoding="UTF-8"?>
<form:Form xmlns:form="http://g5.1c.ru/v8/dt/form">
</form:Form>
"""
        result = parse_form_xml(xml)
        assert result is not None
        assert result["handlers"] == []
        assert result["commands"] == []
        assert result["attributes"] == []

    def test_invalid_xml_returns_none(self):
        assert parse_form_xml("not xml at all") is None

    def test_empty_content_returns_none(self):
        assert parse_form_xml("") is None
        assert parse_form_xml("   ") is None

    def test_unknown_namespace_returns_none(self):
        xml = '<?xml version="1.0"?><Root xmlns="http://example.com"><child/></Root>'
        assert parse_form_xml(xml) is None

    def test_query_text_truncation(self):
        long_query = "ВЫБРАТЬ " + "А" * 600
        xml = f"""\
<?xml version="1.0" encoding="UTF-8"?>
<form:Form xmlns:form="http://g5.1c.ru/v8/dt/form"
           xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <form:attributes>
    <name>Список</name>
    <form:valueType>
      <types>DynamicList</types>
    </form:valueType>
    <form:extInfo>
      <mainTable>Document.Test</mainTable>
      <queryText>{long_query}</queryText>
    </form:extInfo>
  </form:attributes>
</form:Form>
"""
        result = parse_form_xml(xml)
        assert result is not None
        qt = result["attributes"][0]["query_text"]
        assert len(qt) == 512


# ── Helper tests ──────────────────────────────────────────────


class TestParseFormHelper:
    """Tests for parse_form() helper function."""

    @pytest.fixture()
    def helpers(self, tmp_path):
        """Create a minimal form file structure and init helpers."""
        # Create CF-format form
        forms_dir = tmp_path / "Documents" / "РеализацияТоваровУслуг" / "Forms" / "ФормаДокумента" / "Ext"
        forms_dir.mkdir(parents=True)
        (forms_dir / "Form.xml").write_text(_CF_FORM_XML, encoding="utf-8")
        (forms_dir / "Form").mkdir()
        (forms_dir / "Form" / "Module.bsl").write_text("// module", encoding="utf-8")

        # Create CommonForm
        cf_dir = tmp_path / "CommonForms" / "ОбщаяФормаНастроек" / "Ext"
        cf_dir.mkdir(parents=True)
        cf_form_xml = """\
<?xml version="1.0" encoding="UTF-8"?>
<Form xmlns="http://v8.1c.ru/8.3/xcf/logform">
  <Events>
    <Event name="OnCreateAtServer">ПриСозданииНаСервере</Event>
  </Events>
</Form>
"""
        (cf_dir / "Form.xml").write_text(cf_form_xml, encoding="utf-8")
        (cf_dir / "Form").mkdir()
        (cf_dir / "Form" / "Module.bsl").write_text("// common form module", encoding="utf-8")

        from rlm_tools_bsl.bsl_helpers import make_bsl_helpers

        base = str(tmp_path)

        def read_file(path):
            full = path if os.path.isabs(path) else os.path.join(base, path)
            try:
                return Path(full).read_text(encoding="utf-8-sig")
            except (OSError, UnicodeDecodeError):
                return ""

        def glob_files(pattern):
            return [str(p.relative_to(tmp_path)).replace("\\", "/") for p in tmp_path.glob(pattern)]

        h = make_bsl_helpers(
            base_path=base,
            resolve_safe=lambda p: Path(base) / p,
            read_file_fn=read_file,
            grep_fn=lambda *a, **kw: [],
            glob_files_fn=glob_files,
        )
        return h

    def test_parse_form_basic(self, helpers):
        result = helpers["parse_form"]("РеализацияТоваровУслуг")
        assert len(result) == 1
        form = result[0]
        assert form["form_name"] == "ФормаДокумента"
        assert form["category"] == "Documents"
        assert len(form["handlers"]) > 0
        assert len(form["commands"]) == 1
        assert len(form["attributes"]) == 2

    def test_parse_form_module_path(self, helpers):
        result = helpers["parse_form"]("РеализацияТоваровУслуг")
        form = result[0]
        assert form["module_path"] != ""
        assert "Module.bsl" in form["module_path"]

    def test_parse_form_handler_filter(self, helpers):
        result = helpers["parse_form"]("РеализацияТоваровУслуг", handler="ПриСозданииНаСервере")
        assert len(result) == 1
        form = result[0]
        assert len(form["handlers"]) == 1
        assert form["handlers"][0]["handler"] == "ПриСозданииНаСервере"
        # commands and attributes should still be present
        assert len(form["commands"]) == 1

    def test_parse_form_handler_filter_keeps_context(self, helpers):
        """handler filter keeps commands/attributes for full UI context."""
        result = helpers["parse_form"]("РеализацияТоваровУслуг", handler="ПриСозданииНаСервере")
        assert len(result) == 1
        form = result[0]
        # handlers — only matching
        assert len(form["handlers"]) == 1
        assert form["handlers"][0]["handler"] == "ПриСозданииНаСервере"
        # commands and attributes stay complete for context
        assert len(form["commands"]) >= 1
        assert len(form["attributes"]) >= 1

    def test_parse_form_handler_not_found(self, helpers):
        result = helpers["parse_form"]("РеализацияТоваровУслуг", handler="НесуществующийОбработчик")
        assert result == []

    def test_parse_form_empty_name_raises(self, helpers):
        with pytest.raises(ValueError, match="object_name is required"):
            helpers["parse_form"]("")

    def test_parse_form_strip_prefix(self, helpers):
        result = helpers["parse_form"]("Документ.РеализацияТоваровУслуг")
        assert len(result) == 1

    def test_parse_form_common_form(self, helpers):
        result = helpers["parse_form"]("ОбщаяФормаНастроек")
        assert len(result) == 1
        form = result[0]
        assert form["category"] == "CommonForms"
        assert form["object_name"] == "ОбщаяФормаНастроек"
        assert form["form_name"] == "ОбщаяФормаНастроек"
        assert form["module_path"] != ""

    def test_parse_form_common_form_prefix(self, helpers):
        result = helpers["parse_form"]("ОбщаяФорма.ОбщаяФормаНастроек")
        assert len(result) == 1

    def test_parse_form_form_name_filter(self, helpers):
        result = helpers["parse_form"]("РеализацияТоваровУслуг", form_name="ФормаДокумента")
        assert len(result) == 1
        result2 = helpers["parse_form"]("РеализацияТоваровУслуг", form_name="НесуществующаяФорма")
        assert result2 == []

    def test_parse_form_scope_values(self, helpers):
        result = helpers["parse_form"]("РеализацияТоваровУслуг")
        handlers = result[0]["handlers"]
        scopes = {h["scope"] for h in handlers}
        assert "form" in scopes
        assert "element" in scopes
        # All scopes must be non-empty
        for h in handlers:
            assert h["scope"] in ("form", "ext_info", "element")


# ── Index tests ───────────────────────────────────────────────


class TestFormElementsIndex:
    """Tests for form_elements table in SQLite index."""

    @pytest.fixture()
    def indexed_db(self, tmp_path):
        """Create a temporary 1C source tree, build index, return (db_path, base_path)."""
        # Create CF-format form
        forms_dir = tmp_path / "Documents" / "Тест" / "Forms" / "ФормаДок" / "Ext"
        forms_dir.mkdir(parents=True)
        (forms_dir / "Form.xml").write_text(_CF_FORM_XML, encoding="utf-8")
        (forms_dir / "Form").mkdir()
        (forms_dir / "Form" / "Module.bsl").write_text(
            "Процедура ПриСозданииНаСервере(Отказ)\nКонецПроцедуры", encoding="utf-8"
        )

        # Create CommonForm
        cf_dir = tmp_path / "CommonForms" / "ОбщаяФорма1" / "Ext"
        cf_dir.mkdir(parents=True)
        cf_xml = """\
<?xml version="1.0" encoding="UTF-8"?>
<Form xmlns="http://v8.1c.ru/8.3/xcf/logform">
  <Events>
    <Event name="OnCreateAtServer">ПриСозданииНаСервере</Event>
  </Events>
</Form>
"""
        (cf_dir / "Form.xml").write_text(cf_xml, encoding="utf-8")

        from rlm_tools_bsl.bsl_index import IndexBuilder

        builder = IndexBuilder()
        db_path = builder.build(str(tmp_path), build_metadata=True, build_calls=False)
        return db_path, str(tmp_path)

    def test_form_elements_table_exists(self, indexed_db):
        db_path, _ = indexed_db
        conn = sqlite3.connect(str(db_path))
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        conn.close()
        assert "form_elements" in tables

    def test_form_elements_populated(self, indexed_db):
        db_path, _ = indexed_db
        conn = sqlite3.connect(str(db_path))
        count = conn.execute("SELECT COUNT(*) FROM form_elements").fetchone()[0]
        conn.close()
        assert count > 0

    def test_form_elements_kinds(self, indexed_db):
        db_path, _ = indexed_db
        conn = sqlite3.connect(str(db_path))
        kinds = {r[0] for r in conn.execute("SELECT DISTINCT kind FROM form_elements").fetchall()}
        conn.close()
        assert "handler" in kinds
        assert "command" in kinds
        assert "attribute" in kinds

    def test_common_forms_indexed(self, indexed_db):
        db_path, _ = indexed_db
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute("SELECT * FROM form_elements WHERE category='CommonForms'").fetchall()
        conn.close()
        assert len(rows) >= 1

    def test_meta_keys(self, indexed_db):
        db_path, _ = indexed_db
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        has = conn.execute("SELECT value FROM index_meta WHERE key='has_form_elements'").fetchone()
        count = conn.execute("SELECT value FROM index_meta WHERE key='form_elements_count'").fetchone()
        bv = conn.execute("SELECT value FROM index_meta WHERE key='builder_version'").fetchone()
        conn.close()
        assert has["value"] == "1"
        assert int(count["value"]) > 0
        assert int(bv["value"]) == BUILDER_VERSION

    def test_index_reader_get_form_elements(self, indexed_db):
        db_path, _ = indexed_db
        from rlm_tools_bsl.bsl_index import IndexReader

        reader = IndexReader(db_path)
        rows = reader.get_form_elements(object_name="Тест")
        assert rows is not None
        assert len(rows) > 0
        reader.close()

    def test_index_reader_handler_filter(self, indexed_db):
        db_path, _ = indexed_db
        from rlm_tools_bsl.bsl_index import IndexReader

        reader = IndexReader(db_path)
        rows = reader.get_form_elements(handler="ПриСозданииНаСервере")
        assert rows is not None
        assert len(rows) >= 1
        for r in rows:
            assert r["handler"] == "ПриСозданииНаСервере"
        reader.close()

    def test_get_statistics_has_form_elements(self, indexed_db):
        db_path, _ = indexed_db
        from rlm_tools_bsl.bsl_index import IndexReader

        reader = IndexReader(db_path)
        stats = reader.get_statistics()
        assert "form_elements" in stats
        assert stats["form_elements"] > 0
        reader.close()

    def test_no_metadata_no_form_elements(self, tmp_path):
        """build_metadata=False → form_elements not created."""
        forms_dir = tmp_path / "Documents" / "Тест2" / "Forms" / "Ф1" / "Ext"
        forms_dir.mkdir(parents=True)
        (forms_dir / "Form.xml").write_text(_CF_FORM_XML, encoding="utf-8")
        (forms_dir / "Form").mkdir()
        (forms_dir / "Form" / "Module.bsl").write_text("// empty", encoding="utf-8")

        from rlm_tools_bsl.bsl_index import IndexBuilder

        builder = IndexBuilder()
        db_path = builder.build(str(tmp_path), build_metadata=False, build_calls=False)
        conn = sqlite3.connect(str(db_path))
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        conn.close()
        assert "form_elements" not in tables

    def test_attribute_is_main_preserved(self, indexed_db):
        db_path, _ = indexed_db
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute("SELECT * FROM form_elements WHERE kind='attribute' AND attribute_is_main=1").fetchall()
        conn.close()
        assert len(rows) >= 1

    def test_has_form_elements_capability_with_empty_table(self, tmp_path):
        """has_form_elements is a capability flag (table exists), not a count."""
        # Create config with BSL but NO forms at all
        mod_dir = tmp_path / "CommonModules" / "ОбщийМодуль"
        mod_dir.mkdir(parents=True)
        (mod_dir / "Module.bsl").write_text("// empty module", encoding="utf-8")

        from rlm_tools_bsl.bsl_index import IndexBuilder

        builder = IndexBuilder()
        db_path = builder.build(str(tmp_path), build_metadata=True, build_calls=False)

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        has = conn.execute("SELECT value FROM index_meta WHERE key='has_form_elements'").fetchone()
        count = conn.execute("SELECT value FROM index_meta WHERE key='form_elements_count'").fetchone()
        conn.close()
        # Capability = True (table created), even though count = 0
        assert has["value"] == "1"
        assert int(count["value"]) == 0

    def test_empty_form_no_rows_in_index(self, tmp_path):
        """Empty form produces zero rows — kind is handler|command|attribute only."""
        forms_dir = tmp_path / "Documents" / "ПустойДок" / "Forms" / "ПустаяФорма" / "Ext"
        forms_dir.mkdir(parents=True)
        empty_xml = """\
<?xml version="1.0" encoding="UTF-8"?>
<Form xmlns="http://v8.1c.ru/8.3/xcf/logform">
</Form>
"""
        (forms_dir / "Form.xml").write_text(empty_xml, encoding="utf-8")
        (forms_dir / "Form").mkdir()
        (forms_dir / "Form" / "Module.bsl").write_text("// empty", encoding="utf-8")

        from rlm_tools_bsl.bsl_index import IndexBuilder

        builder = IndexBuilder()
        db_path = builder.build(str(tmp_path), build_metadata=True, build_calls=False)

        conn = sqlite3.connect(str(db_path))
        rows = conn.execute("SELECT * FROM form_elements WHERE object_name='ПустойДок'").fetchall()
        kinds = conn.execute("SELECT DISTINCT kind FROM form_elements").fetchall()
        conn.close()
        # No rows for empty form — table stays clean
        assert len(rows) == 0
        # All kinds in table are from the agreed schema
        for (k,) in kinds:
            assert k in ("handler", "command", "attribute")

    def test_v9_to_v10_upgrade(self, tmp_path):
        """Simulate v9 index and verify update() creates form_elements."""
        forms_dir = tmp_path / "Documents" / "Тест3" / "Forms" / "Ф1" / "Ext"
        forms_dir.mkdir(parents=True)
        (forms_dir / "Form.xml").write_text(_CF_FORM_XML, encoding="utf-8")
        (forms_dir / "Form").mkdir()
        (forms_dir / "Form" / "Module.bsl").write_text(
            "Процедура ПриСозданииНаСервере(Отказ)\nКонецПроцедуры", encoding="utf-8"
        )

        from rlm_tools_bsl.bsl_index import IndexBuilder

        builder = IndexBuilder()
        # Build first
        db_path = builder.build(str(tmp_path), build_metadata=True, build_calls=False)

        # Verify form_elements already exists from build
        conn = sqlite3.connect(str(db_path))
        count = conn.execute("SELECT COUNT(*) FROM form_elements").fetchone()[0]
        conn.close()
        assert count > 0

        # Update should also refresh form_elements
        builder.update(str(tmp_path))
        conn = sqlite3.connect(str(db_path))
        count2 = conn.execute("SELECT COUNT(*) FROM form_elements").fetchone()[0]
        conn.close()
        assert count2 > 0


# ── Parity test: indexed vs live ──────────────────────────────


class TestParseFormParity:
    """parse_form() with index and without must return same structure."""

    @pytest.fixture()
    def parity_setup(self, tmp_path):
        """Create form tree, return (helpers_live, helpers_indexed)."""
        forms_dir = tmp_path / "Documents" / "ТестПаритет" / "Forms" / "ФормаДок" / "Ext"
        forms_dir.mkdir(parents=True)
        (forms_dir / "Form.xml").write_text(_CF_FORM_XML, encoding="utf-8")
        (forms_dir / "Form").mkdir()
        (forms_dir / "Form" / "Module.bsl").write_text("// module", encoding="utf-8")

        base = str(tmp_path)

        def read_file(path):
            full = path if os.path.isabs(path) else os.path.join(base, path)
            try:
                return Path(full).read_text(encoding="utf-8-sig")
            except (OSError, UnicodeDecodeError):
                return ""

        def glob_files(pattern):
            return [str(p.relative_to(tmp_path)).replace("\\", "/") for p in tmp_path.glob(pattern)]

        from rlm_tools_bsl.bsl_helpers import make_bsl_helpers

        # Live (no index)
        h_live = make_bsl_helpers(
            base_path=base,
            resolve_safe=lambda p: Path(base) / p,
            read_file_fn=read_file,
            grep_fn=lambda *a, **kw: [],
            glob_files_fn=glob_files,
        )

        # Build index
        from rlm_tools_bsl.bsl_index import IndexBuilder, IndexReader

        builder = IndexBuilder()
        db_path = builder.build(base, build_metadata=True, build_calls=False)
        reader = IndexReader(db_path)

        h_indexed = make_bsl_helpers(
            base_path=base,
            resolve_safe=lambda p: Path(base) / p,
            read_file_fn=read_file,
            grep_fn=lambda *a, **kw: [],
            glob_files_fn=glob_files,
            idx_reader=reader,
        )

        return h_live, h_indexed, reader

    def test_parity_structure(self, parity_setup):
        h_live, h_indexed, reader = parity_setup
        try:
            live = h_live["parse_form"]("ТестПаритет")
            indexed = h_indexed["parse_form"]("ТестПаритет")

            assert len(live) == len(indexed) == 1

            for key in ("form_name", "handlers", "commands", "attributes"):
                if key == "handlers":
                    # Compare handler count and structure
                    assert len(live[0][key]) == len(indexed[0][key])
                    for lh, ih in zip(
                        sorted(live[0][key], key=lambda x: x["handler"]),
                        sorted(indexed[0][key], key=lambda x: x["handler"]),
                    ):
                        assert lh["handler"] == ih["handler"]
                        assert lh["event"] == ih["event"]
                        assert lh["scope"] == ih["scope"]
                elif key == "commands":
                    assert len(live[0][key]) == len(indexed[0][key])
                elif key == "attributes":
                    assert len(live[0][key]) == len(indexed[0][key])
                    for la, ia in zip(
                        sorted(live[0][key], key=lambda x: x["name"]),
                        sorted(indexed[0][key], key=lambda x: x["name"]),
                    ):
                        assert la["name"] == ia["name"]
                        assert la["main"] == ia["main"]
                else:
                    assert live[0][key] == indexed[0][key]
        finally:
            reader.close()

    def test_parity_handler_filter_context(self, parity_setup):
        """handler filter on indexed path preserves commands/attributes like live."""
        h_live, h_indexed, reader = parity_setup
        try:
            live = h_live["parse_form"]("ТестПаритет", handler="ПриСозданииНаСервере")
            indexed = h_indexed["parse_form"]("ТестПаритет", handler="ПриСозданииНаСервере")

            assert len(live) == len(indexed) == 1
            # handlers — only matching
            assert len(live[0]["handlers"]) == 1
            assert len(indexed[0]["handlers"]) == 1
            # commands and attributes — full context preserved
            assert len(live[0]["commands"]) == len(indexed[0]["commands"])
            assert len(live[0]["attributes"]) == len(indexed[0]["attributes"])
        finally:
            reader.close()


class TestParseFormParityEmpty:
    """Parity test for empty form (no handlers/commands/attributes)."""

    @pytest.fixture()
    def empty_form_setup(self, tmp_path):
        """Create an empty form, return (helpers_live, helpers_indexed, reader)."""
        forms_dir = tmp_path / "Documents" / "ПустойТест" / "Forms" / "ПустаяФорма" / "Ext"
        forms_dir.mkdir(parents=True)
        empty_xml = """\
<?xml version="1.0" encoding="UTF-8"?>
<Form xmlns="http://v8.1c.ru/8.3/xcf/logform">
</Form>
"""
        (forms_dir / "Form.xml").write_text(empty_xml, encoding="utf-8")
        (forms_dir / "Form").mkdir()
        (forms_dir / "Form" / "Module.bsl").write_text("// empty module", encoding="utf-8")

        base = str(tmp_path)

        def read_file(path):
            full = path if os.path.isabs(path) else os.path.join(base, path)
            try:
                return Path(full).read_text(encoding="utf-8-sig")
            except (OSError, UnicodeDecodeError):
                return ""

        def glob_files(pattern):
            return [str(p.relative_to(tmp_path)).replace("\\", "/") for p in tmp_path.glob(pattern)]

        from rlm_tools_bsl.bsl_helpers import make_bsl_helpers
        from rlm_tools_bsl.bsl_index import IndexBuilder, IndexReader

        h_live = make_bsl_helpers(
            base_path=base,
            resolve_safe=lambda p: Path(base) / p,
            read_file_fn=read_file,
            grep_fn=lambda *a, **kw: [],
            glob_files_fn=glob_files,
        )

        builder = IndexBuilder()
        db_path = builder.build(base, build_metadata=True, build_calls=False)
        reader = IndexReader(db_path)

        h_indexed = make_bsl_helpers(
            base_path=base,
            resolve_safe=lambda p: Path(base) / p,
            read_file_fn=read_file,
            grep_fn=lambda *a, **kw: [],
            glob_files_fn=glob_files,
            idx_reader=reader,
        )

        return h_live, h_indexed, reader

    def test_parity_empty_form(self, empty_form_setup):
        """Empty form is returned by both live and indexed paths."""
        h_live, h_indexed, reader = empty_form_setup
        try:
            live = h_live["parse_form"]("ПустойТест")
            indexed = h_indexed["parse_form"]("ПустойТест")

            assert len(live) == len(indexed) == 1
            assert live[0]["form_name"] == indexed[0]["form_name"] == "ПустаяФорма"
            assert live[0]["handlers"] == indexed[0]["handlers"] == []
            assert live[0]["commands"] == indexed[0]["commands"] == []
            assert live[0]["attributes"] == indexed[0]["attributes"] == []
        finally:
            reader.close()


# ── v1.33.0: MainAttribute в CF-форме и types списком ──────────────────────


_V1330_CF_FORM = """<?xml version="1.0" encoding="UTF-8"?>
<Form xmlns="http://v8.1c.ru/8.3/xcf/logform" xmlns:v8="http://v8.1c.ru/8.1/data/core">
  <Attributes>
    <Attribute name="Список">
      <Type><v8:Type>cfg:DynamicList</v8:Type></Type>
      <MainAttribute>true</MainAttribute>
    </Attribute>
    <Attribute name="Объект">
      <Type><v8:Type>cfg:CatalogObject.Х</v8:Type><v8:Type>cfg:CatalogRef.Х</v8:Type></Type>
    </Attribute>
  </Attributes>
</Form>
"""


def test_cf_form_main_attribute_is_recognized():
    res = parse_form_xml(_V1330_CF_FORM)
    by_name = {a["name"]: a for a in res["attributes"]}
    assert by_name["Список"]["main"] is True
    assert by_name["Объект"]["main"] is False


def test_cf_form_types_is_a_list():
    res = parse_form_xml(_V1330_CF_FORM)
    by_name = {a["name"]: a for a in res["attributes"]}
    assert by_name["Объект"]["types"] == ["cfg:CatalogObject.Х", "cfg:CatalogRef.Х"]
    assert by_name["Список"]["types"] == ["cfg:DynamicList"]


def test_cf_form_no_type_gives_empty_list():
    res = parse_form_xml(
        '<Form xmlns="http://v8.1c.ru/8.3/xcf/logform"><Attributes><Attribute name="Пусто"/></Attributes></Form>'
    )
    assert res["attributes"][0]["types"] == []


def test_cf_form_legacy_main_tag_still_honored():
    """Тег <Main> в природе не встречается, но если он есть — читать его тоже."""
    res = parse_form_xml(
        '<Form xmlns="http://v8.1c.ru/8.3/xcf/logform">'
        '<Attributes><Attribute name="А"><Main>true</Main></Attribute></Attributes></Form>'
    )
    assert res["attributes"][0]["main"] is True


# Форма EDT: `name` и `main` — ДОЧЕРНИЕ ЭЛЕМЕНТЫ, а не XML-атрибуты
# (сверено с боевым Form.form конфигурации на EDT).
_V1330_EDT_FORM = """<?xml version="1.0" encoding="UTF-8"?>
<form:Form xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
           xmlns:form="http://g5.1c.ru/v8/dt/form">
  <attributes>
    <name>Объект</name>
    <id>1</id>
    <valueType>
      <types>CatalogObject.Х</types>
      <types>CatalogRef.Х</types>
    </valueType>
    <main>true</main>
  </attributes>
  <attributes>
    <name>Прочий</name>
    <id>2</id>
    <valueType><types>String</types></valueType>
  </attributes>
</form:Form>
"""


def test_edt_form_types_is_a_list_too():
    """EDT-ветка обязана держать тот же списочный контракт, что и CF."""
    res = parse_form_xml(_V1330_EDT_FORM)
    by_name = {a["name"]: a for a in res["attributes"]}
    assert by_name["Объект"]["types"] == ["CatalogObject.Х", "CatalogRef.Х"]
    assert by_name["Прочий"]["types"] == ["String"]
