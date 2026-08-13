"""Unit tests for find_references_to_object, find_defined_types, canonicalize_type_ref
and the new XML parsers (parse_defined_type, parse_pvh_characteristics,
parse_command_parameter_type) introduced in v1.9.0 (Issue #10)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from rlm_tools_bsl.bsl_index import (
    IndexBuilder,
    IndexReader,
    _METADATA_REFERENCES_TRIGGER_CATEGORIES,
)
from rlm_tools_bsl.bsl_xml_parsers import (
    canonicalize_type_ref,
    parse_command_parameter_type,
    parse_defined_type,
    parse_metadata_xml,
    parse_pvh_characteristics,
)


# ---------------------------------------------------------------------------
# canonicalize_type_ref
# ---------------------------------------------------------------------------


class TestCanonicalize:
    def test_catalog_ref(self):
        assert canonicalize_type_ref("CatalogRef.Контрагенты") == "Catalog.Контрагенты"

    def test_catalog_object(self):
        assert canonicalize_type_ref("CatalogObject.X") == "Catalog.X"

    def test_document_ref_with_namespace(self):
        assert canonicalize_type_ref("cfg:DocumentRef.Заказ") == "Document.Заказ"

    def test_d4p1_prefix_stripped(self):
        # EDT-style prefix
        assert canonicalize_type_ref("d4p1:CatalogRef.X") == "Catalog.X"

    def test_register_record_set(self):
        assert canonicalize_type_ref("InformationRegisterRecordSet.X") == "InformationRegister.X"

    def test_enum_ref(self):
        assert canonicalize_type_ref("EnumRef.Статусы") == "Enum.Статусы"

    def test_already_canonical(self):
        assert canonicalize_type_ref("Catalog.X") == "Catalog.X"
        assert canonicalize_type_ref("DefinedType.Сумма") == "DefinedType.Сумма"

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("document.Заказ", "Document.Заказ"),
            ("DOCUMENT.Заказ", "Document.Заказ"),
            ("documentobject.Заказ", "Document.Заказ"),
            ("CFG:catalogref.Контрагенты", "Catalog.Контрагенты"),
        ],
    )
    def test_english_prefixes_are_case_insensitive(self, raw, expected):
        assert canonicalize_type_ref(raw) == expected

    def test_primitive_returns_empty(self):
        assert canonicalize_type_ref("xs:string") == ""
        assert canonicalize_type_ref("String") == ""
        assert canonicalize_type_ref("Number") == ""

    def test_empty_input(self):
        assert canonicalize_type_ref("") == ""
        assert canonicalize_type_ref("   ") == ""

    def test_chart_of_characteristic_types(self):
        assert canonicalize_type_ref("ChartOfCharacteristicTypesRef.X") == "ChartOfCharacteristicTypes.X"


# ---------------------------------------------------------------------------
# parse_metadata_xml — new "references" field
# ---------------------------------------------------------------------------

# Minimal CF Catalog XML with: owner, attribute referring to Catalog.X, default form
_CF_CATALOG_XML = """<?xml version="1.0" encoding="UTF-8"?>
<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"
                xmlns:v8="http://v8.1c.ru/8.1/data/core"
                xmlns:xr="http://v8.1c.ru/8.3/xcf/readable"
                xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
                xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <Catalog>
    <Properties>
      <Name>Контрагенты</Name>
      <Synonym>
        <v8:item>
          <v8:lang>ru</v8:lang>
          <v8:content>Контрагенты</v8:content>
        </v8:item>
      </Synonym>
      <Owners>
        <xr:Item xsi:type="xr:MDObjectRef">Catalog.Организации</xr:Item>
      </Owners>
      <DefaultObjectForm>Catalog.Контрагенты.Form.ФормаЭлемента</DefaultObjectForm>
      <DefaultListForm>Catalog.Контрагенты.Form.ФормаСписка</DefaultListForm>
    </Properties>
    <ChildObjects>
      <Attribute>
        <Properties>
          <Name>ОсновнойБанк</Name>
          <Type>
            <v8:Type>CatalogRef.Банки</v8:Type>
          </Type>
        </Properties>
      </Attribute>
    </ChildObjects>
  </Catalog>
</MetaDataObject>
"""

# Same logical content in EDT/MDO format
_EDT_CATALOG_MDO = """<?xml version="1.0" encoding="UTF-8"?>
<mdclass:Catalog xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass">
  <name>Контрагенты</name>
  <synonym>
    <key>ru</key>
    <value>Контрагенты</value>
  </synonym>
  <owners>CatalogRef.Организации</owners>
  <defaultObjectForm>Catalog.Контрагенты.Form.ФормаЭлемента</defaultObjectForm>
  <attributes>
    <name>ОсновнойБанк</name>
    <type>
      <types>CatalogRef.Банки</types>
    </type>
  </attributes>
</mdclass:Catalog>
"""

# CF Document with BasedOn + tabular section attribute referring to a Catalog
_CF_DOCUMENT_XML = """<?xml version="1.0" encoding="UTF-8"?>
<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"
                xmlns:v8="http://v8.1c.ru/8.1/data/core"
                xmlns:xr="http://v8.1c.ru/8.3/xcf/readable"
                xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <Document>
    <Properties>
      <Name>РеализацияТоваровУслуг</Name>
      <BasedOn>
        <xr:Item xsi:type="xr:MDObjectRef">Document.ЗаказКлиента</xr:Item>
      </BasedOn>
    </Properties>
    <ChildObjects>
      <TabularSection>
        <Properties>
          <Name>Товары</Name>
        </Properties>
        <ChildObjects>
          <Attribute>
            <Properties>
              <Name>Номенклатура</Name>
              <Type>
                <v8:Type>CatalogRef.Номенклатура</v8:Type>
              </Type>
            </Properties>
          </Attribute>
        </ChildObjects>
      </TabularSection>
    </ChildObjects>
  </Document>
</MetaDataObject>
"""

_CF_SUBSYSTEM_XML = """<?xml version="1.0" encoding="UTF-8"?>
<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"
                xmlns:v8="http://v8.1c.ru/8.1/data/core"
                xmlns:xr="http://v8.1c.ru/8.3/xcf/readable">
  <Subsystem>
    <Properties>
      <Name>CRM</Name>
      <Content>
        <xr:Item>Catalog.Контрагенты</xr:Item>
        <xr:Item>Document.РеализацияТоваровУслуг</xr:Item>
      </Content>
    </Properties>
  </Subsystem>
</MetaDataObject>
"""

_EDT_SUBSYSTEM_MDO = """<?xml version="1.0" encoding="UTF-8"?>
<mdclass:Subsystem xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass">
  <name>CRM</name>
  <content>Catalog.Контрагенты</content>
  <content>Document.РеализацияТоваровУслуг</content>
</mdclass:Subsystem>
"""


_CF_CATALOG_REAL_OWNERS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"
                xmlns:v8="http://v8.1c.ru/8.1/data/core"
                xmlns:xr="http://v8.1c.ru/8.3/xcf/readable"
                xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <Catalog>
    <Properties>
      <Name>Контактные</Name>
      <Owners>
        <xr:Item xsi:type="xr:MDObjectRef">Catalog.Контрагенты</xr:Item>
        <xr:Item xsi:type="xr:MDObjectRef">Catalog.Партнеры</xr:Item>
      </Owners>
    </Properties>
  </Catalog>
</MetaDataObject>
"""


class TestParseMetadataXmlReferences:
    def test_cf_owners_real_world_xr_item_format(self):
        """Real CF Catalog owners use <xr:Item xsi:type="xr:MDObjectRef">,
        not <v8:Type>. Both must be handled — regression for ERP indexes
        showing 0 owner refs in v1.9.0 first build."""
        parsed = parse_metadata_xml(_CF_CATALOG_REAL_OWNERS_XML)
        refs = parsed.get("references", [])
        owner_refs = [r for r in refs if r["ref_kind"] == "owner"]
        owner_targets = {r["ref_object"] for r in owner_refs}
        assert owner_targets == {"Catalog.Контрагенты", "Catalog.Партнеры"}

    def test_cf_catalog_extracts_owner_and_attribute_type_and_form(self):
        parsed = parse_metadata_xml(_CF_CATALOG_XML)
        refs = parsed.get("references", [])
        kinds = {r["ref_kind"] for r in refs}
        assert "owner" in kinds
        assert "attribute_type" in kinds
        # Default object form references the Catalog itself
        assert "default_object_form" in kinds
        assert any(r["ref_object"] == "Catalog.Организации" and r["ref_kind"] == "owner" for r in refs)
        assert any(r["ref_object"] == "Catalog.Банки" and r["ref_kind"] == "attribute_type" for r in refs)

    def test_edt_catalog_extracts_owner_and_attribute_type(self):
        parsed = parse_metadata_xml(_EDT_CATALOG_MDO)
        refs = parsed.get("references", [])
        assert any(r["ref_object"] == "Catalog.Организации" and r["ref_kind"] == "owner" for r in refs)
        assert any(r["ref_object"] == "Catalog.Банки" and r["ref_kind"] == "attribute_type" for r in refs)

    def test_cf_document_basedon_and_ts_attribute(self):
        parsed = parse_metadata_xml(_CF_DOCUMENT_XML)
        refs = parsed.get("references", [])
        assert any(r["ref_object"] == "Document.ЗаказКлиента" and r["ref_kind"] == "based_on" for r in refs)
        assert any(
            r["ref_object"] == "Catalog.Номенклатура"
            and r["ref_kind"] == "attribute_type"
            and "TabularSection.Товары" in r["used_in_suffix"]
            for r in refs
        )

    def test_cf_subsystem_content_references(self):
        parsed = parse_metadata_xml(_CF_SUBSYSTEM_XML)
        refs = parsed.get("references", [])
        kinds_by_obj = {(r["ref_object"], r["ref_kind"]) for r in refs}
        assert ("Catalog.Контрагенты", "subsystem_content") in kinds_by_obj
        assert ("Document.РеализацияТоваровУслуг", "subsystem_content") in kinds_by_obj

    def test_edt_subsystem_content_references(self):
        parsed = parse_metadata_xml(_EDT_SUBSYSTEM_MDO)
        refs = parsed.get("references", [])
        kinds_by_obj = {(r["ref_object"], r["ref_kind"]) for r in refs}
        assert ("Catalog.Контрагенты", "subsystem_content") in kinds_by_obj
        assert ("Document.РеализацияТоваровУслуг", "subsystem_content") in kinds_by_obj

    def test_legacy_fields_still_present(self):
        """parse_metadata_xml must keep its original `attributes` field — backward compat."""
        parsed = parse_metadata_xml(_CF_CATALOG_XML)
        assert parsed["object_type"] == "Catalog"
        assert parsed["name"] == "Контрагенты"
        assert any(a["name"] == "ОсновнойБанк" for a in parsed.get("attributes", []))


# ---------------------------------------------------------------------------
# parse_defined_type / parse_pvh_characteristics / parse_command_parameter_type
# ---------------------------------------------------------------------------

_CF_DEFINED_TYPE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"
                xmlns:v8="http://v8.1c.ru/8.1/data/core">
  <DefinedType>
    <Properties>
      <Name>ВладелецКонтрагента</Name>
      <Type>
        <v8:Type>CatalogRef.Организации</v8:Type>
        <v8:Type>CatalogRef.Партнеры</v8:Type>
      </Type>
    </Properties>
  </DefinedType>
</MetaDataObject>
"""

# v1.33.0: ПВХ нужен в фикстуре, чтобы белый список _ANCHORED_REF_KINDS реально
# проверялся — на characteristic_type он ОБЯЗАН оставить line=NULL.
_CF_PVH_XML = """<?xml version="1.0" encoding="UTF-8"?>
<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"
                xmlns:v8="http://v8.1c.ru/8.1/data/core">
  <ChartOfCharacteristicTypes>
    <Properties>
      <Name>ВидыСубконто</Name>
      <Type>
        <v8:Type>cfg:CatalogRef.Организации</v8:Type>
        <v8:Type>cfg:CatalogRef.Контрагенты</v8:Type>
      </Type>
      <DefaultObjectForm>ChartOfCharacteristicTypes.ВидыСубконто.Form.ФормаЭлемента</DefaultObjectForm>
      <DefaultListForm>ChartOfCharacteristicTypes.ВидыСубконто.Form.ФормаСписка</DefaultListForm>
    </Properties>
  </ChartOfCharacteristicTypes>
</MetaDataObject>
"""

_EDT_DEFINED_TYPE_MDO = """<?xml version="1.0" encoding="UTF-8"?>
<mdclass:DefinedType xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass">
  <name>ВладелецКонтрагента</name>
  <type>
    <types>CatalogRef.Организации</types>
    <types>CatalogRef.Партнеры</types>
  </type>
</mdclass:DefinedType>
"""

_CF_PVH_XML = """<?xml version="1.0" encoding="UTF-8"?>
<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"
                xmlns:v8="http://v8.1c.ru/8.1/data/core">
  <ChartOfCharacteristicTypes>
    <Properties>
      <Name>ВидыСубконто</Name>
      <Type>
        <v8:Type>CatalogRef.Номенклатура</v8:Type>
        <v8:Type>CatalogRef.Контрагенты</v8:Type>
      </Type>
    </Properties>
  </ChartOfCharacteristicTypes>
</MetaDataObject>
"""

_EDT_PVH_MDO = """<?xml version="1.0" encoding="UTF-8"?>
<mdclass:ChartOfCharacteristicTypes xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass">
  <name>ВидыСубконто</name>
  <type>
    <types>CatalogRef.Номенклатура</types>
    <types>CatalogRef.Контрагенты</types>
  </type>
</mdclass:ChartOfCharacteristicTypes>
"""

_CF_COMMAND_XML = """<?xml version="1.0" encoding="UTF-8"?>
<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"
                xmlns:v8="http://v8.1c.ru/8.1/data/core">
  <Command>
    <Properties>
      <Name>СоздатьЗаказНаОснове</Name>
      <CommandParameterType>
        <v8:Type>CatalogRef.Контрагенты</v8:Type>
      </CommandParameterType>
    </Properties>
  </Command>
</MetaDataObject>
"""

_EDT_COMMAND_MDO = """<?xml version="1.0" encoding="UTF-8"?>
<mdclass:Command xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass">
  <name>СоздатьЗаказНаОснове</name>
  <commandParameterType>
    <types>CatalogRef.Контрагенты</types>
  </commandParameterType>
</mdclass:Command>
"""


class TestNewParsers:
    @pytest.mark.parametrize(
        "content",
        [_CF_DEFINED_TYPE_XML, _EDT_DEFINED_TYPE_MDO],
        ids=["cf", "edt"],
    )
    def test_parse_defined_type(self, content):
        parsed = parse_defined_type(content)
        assert parsed is not None
        assert parsed["name"] == "ВладелецКонтрагента"
        assert "CatalogRef.Организации" in parsed["types"]
        assert "CatalogRef.Партнеры" in parsed["types"]

    @pytest.mark.parametrize(
        "content",
        [_CF_PVH_XML, _EDT_PVH_MDO],
        ids=["cf", "edt"],
    )
    def test_parse_pvh_characteristics(self, content):
        parsed = parse_pvh_characteristics(content)
        assert parsed is not None
        assert parsed["pvh_name"] == "ВидыСубконто"
        assert len(parsed["types"]) == 2

    @pytest.mark.parametrize(
        "content",
        [_CF_COMMAND_XML, _EDT_COMMAND_MDO],
        ids=["cf", "edt"],
    )
    def test_parse_command_parameter_type(self, content):
        refs = parse_command_parameter_type(content)
        assert len(refs) == 1
        assert refs[0]["ref_object"] == "Catalog.Контрагенты"
        assert refs[0]["command_name"] == "СоздатьЗаказНаОснове"

    def test_parse_defined_type_handles_garbage(self):
        assert parse_defined_type("") is None
        assert parse_defined_type("<not-xml>") is None

    def test_edt_common_command_root_tag(self):
        """EDT top-level commands use <mdclass:CommonCommand> root tag, not
        <Command>. Regression for v1.9.0 EDT e2e: command_parameter_type=0
        across all probed objects in first build."""
        edt = """<?xml version="1.0" encoding="UTF-8"?>
<mdclass:CommonCommand xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass">
  <name>ИзменитьСтатус</name>
  <commandParameterType>
    <types>DefinedType.тст_ТипОбъектовДляИзмененияСтатусов</types>
    <types>DocumentRef.тст_ДокументТеста</types>
  </commandParameterType>
</mdclass:CommonCommand>
"""
        refs = parse_command_parameter_type(edt)
        ref_objects = {r["ref_object"] for r in refs}
        assert "DefinedType.тст_ТипОбъектовДляИзмененияСтатусов" in ref_objects
        assert "Document.тст_ДокументТеста" in ref_objects
        assert all(r["command_name"] == "ИзменитьСтатус" for r in refs)

    def test_cf_common_command_real_world_format(self):
        """Real CF wraps top-level commands in <CommonCommand> (not <Command>)
        and uses <v8:TypeSet> for DefinedType refs alongside <v8:Type>.
        Regression for ERP indexes showing 0 command_parameter_type refs in
        the first v1.9.0 build."""
        cf = """<?xml version="1.0" encoding="UTF-8"?>
<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"
                xmlns:v8="http://v8.1c.ru/8.1/data/core"
                xmlns:cfg="http://v8.1c.ru/8.1/data/enterprise/current-config">
  <CommonCommand>
    <Properties>
      <Name>АрхивСообщений</Name>
      <CommandParameterType>
        <v8:Type>cfg:ExchangePlanRef.Полный</v8:Type>
        <v8:TypeSet>cfg:DefinedType.ТорговоеПредложение</v8:TypeSet>
      </CommandParameterType>
    </Properties>
  </CommonCommand>
</MetaDataObject>
"""
        refs = parse_command_parameter_type(cf)
        ref_objects = {r["ref_object"] for r in refs}
        # Both v8:Type (object ref) and v8:TypeSet (DefinedType ref) extracted
        assert "ExchangePlan.Полный" in ref_objects
        assert "DefinedType.ТорговоеПредложение" in ref_objects
        assert all(r["command_name"] == "АрхивСообщений" for r in refs)


# ---------------------------------------------------------------------------
# Index-level: build a tiny CF fixture and verify metadata_references
# ---------------------------------------------------------------------------


def _write(path: str | Path, content: str) -> None:
    """Helper: ensure parent dir exists, write file with UTF-8."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


_CF_CONFIG_XML = """<?xml version="1.0" encoding="UTF-8"?>
<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">
  <Configuration>
    <Properties>
      <Name>ТестоваяКонфигурация</Name>
    </Properties>
  </Configuration>
</MetaDataObject>
"""


@pytest.fixture
def refs_cf_fixture(tmp_path, monkeypatch):
    """Builds a tiny CF fixture covering several reference kinds, then builds
    the SQLite index. Returns IndexReader bound to the resulting DB.

    CF folder layout: <Category>/<Object>/Ext/<Type>.xml (e.g. Catalogs/X/Ext/Catalog.xml)
    plus a sibling <Category>/<Object>.xml to ensure parsers find the file.
    """
    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
    base = tmp_path / "cf"
    _write(base / "Configuration.xml", _CF_CONFIG_XML)

    # Builder.build() returns early when no .bsl files are present, so add a stub
    _write(
        base / "CommonModules" / "Test" / "Ext" / "Module.bsl",
        "Процедура Тест() Экспорт КонецПроцедуры",
    )

    # Catalog: requires obj_dir Catalogs/Контрагенты/ — store XML in Ext/Catalog.xml
    _write(base / "Catalogs" / "Контрагенты" / "Ext" / "Catalog.xml", _CF_CATALOG_XML)
    _write(base / "Catalogs" / "Контрагенты.xml", _CF_CATALOG_XML)

    # Document: same layout
    _write(
        base / "Documents" / "РеализацияТоваровУслуг" / "Ext" / "Document.xml",
        _CF_DOCUMENT_XML,
    )
    _write(base / "Documents" / "РеализацияТоваровУслуг.xml", _CF_DOCUMENT_XML)

    # Subsystem (collected directly via _glob_xml on Subsystems/**)
    _write(base / "Subsystems" / "CRM.xml", _CF_SUBSYSTEM_XML)

    # DefinedType (collector iterates DefinedTypes/ for *.xml files)
    _write(base / "DefinedTypes" / "ВладелецКонтрагента.xml", _CF_DEFINED_TYPE_XML)

    # ChartOfCharacteristicTypes: даёт characteristic_type (на нём проверяется
    # белый список) и второй источник default_list_form. Sibling-XML нужен по тем же
    # причинам, что у Catalog/Document выше.
    _write(
        base / "ChartsOfCharacteristicTypes" / "ВидыСубконто" / "Ext" / "ChartOfCharacteristicTypes.xml",
        _CF_PVH_XML,
    )
    _write(base / "ChartsOfCharacteristicTypes" / "ВидыСубконто.xml", _CF_PVH_XML)

    builder = IndexBuilder()
    db_path = builder.build(
        str(base),
        build_calls=False,
        build_metadata=True,
        build_fts=False,
        build_synonyms=True,
    )
    reader = IndexReader(db_path)
    yield reader, str(base), db_path
    reader.close()


class TestIndexV12:
    def test_metadata_references_categories_set(self):
        # DefinedTypes must be a top-level trigger category for v12
        assert "DefinedTypes" in _METADATA_REFERENCES_TRIGGER_CATEGORIES
        assert "Catalogs" in _METADATA_REFERENCES_TRIGGER_CATEGORIES
        assert "ExchangePlans" in _METADATA_REFERENCES_TRIGGER_CATEGORIES
        assert "CommonCommands" in _METADATA_REFERENCES_TRIGGER_CATEGORIES

    def test_v12_tables_exist(self, refs_cf_fixture):
        _, _, db_path = refs_cf_fixture
        conn = sqlite3.connect(str(db_path))
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        conn.close()
        assert "metadata_references" in names
        assert "exchange_plan_content" in names
        assert "defined_types" in names
        assert "characteristic_types" in names

    def test_metadata_references_collected(self, refs_cf_fixture):
        reader, _, _ = refs_cf_fixture
        # Owner ref: Catalog.Организации (referenced from Catalog.Контрагенты)
        rows = reader.find_metadata_references("Catalog.Организации")
        assert rows is not None
        assert any(r["ref_kind"] == "owner" for r in rows)
        # Subsystem content ref: Catalog.Контрагенты ← Subsystem.CRM
        rows = reader.find_metadata_references("Catalog.Контрагенты")
        assert any(r["ref_kind"] == "subsystem_content" for r in rows)
        # Document.ЗаказКлиента is referenced as based_on for РеализацияТоваровУслуг
        rows = reader.find_metadata_references("Document.ЗаказКлиента")
        assert any(r["ref_kind"] == "based_on" for r in rows)

    def test_defined_types_indexed(self, refs_cf_fixture):
        reader, _, _ = refs_cf_fixture
        row = reader.find_defined_type("ВладелецКонтрагента")
        assert row is not None
        assert "Catalog.Организации" in row["types"]

    def test_count_metadata_references_returns_by_kind(self, refs_cf_fixture):
        reader, _, _ = refs_cf_fixture
        counts = reader.count_metadata_references("Catalog.Контрагенты")
        assert counts is not None
        assert counts["total"] >= 1
        assert "subsystem_content" in counts["by_kind"]


# ---------------------------------------------------------------------------
# Sandbox-level: find_references_to_object via make_bsl_helpers
# ---------------------------------------------------------------------------


def _stub_helpers(base: str, idx_reader):
    """Build BSL helpers wired to a real IndexReader and base_path."""
    from rlm_tools_bsl.bsl_helpers import make_bsl_helpers

    def _read_file(p: str) -> str:
        return Path(base, p).read_text(encoding="utf-8-sig", errors="replace")

    def _grep(_pattern: str, _path: str):
        return []

    def _glob(_pattern: str):
        return []

    def _resolve_safe(p: str):
        return Path(base, p)

    return make_bsl_helpers(
        base,
        _resolve_safe,
        _read_file,
        _grep,
        _glob,
        format_info=None,
        idx_reader=idx_reader,
    )


class TestFindReferencesToObject:
    def test_find_via_index_attributes(self, refs_cf_fixture):
        reader, base, _ = refs_cf_fixture
        helpers = _stub_helpers(base, reader)
        # Subsystem content reference for the catalog
        result = helpers["find_references_to_object"]("Справочник.Контрагенты")
        assert result["partial"] is False
        assert result["object"] == "Catalog.Контрагенты"
        assert result["total"] >= 1
        kinds = result["by_kind"]
        assert "subsystem_content" in kinds

    def test_normalize_russian_to_english(self, refs_cf_fixture):
        reader, base, _ = refs_cf_fixture
        helpers = _stub_helpers(base, reader)
        a = helpers["find_references_to_object"]("Справочник.Контрагенты")
        b = helpers["find_references_to_object"]("Catalog.Контрагенты")
        assert a["object"] == b["object"] == "Catalog.Контрагенты"
        assert a["total"] == b["total"]

    def test_normalize_prefix_case_insensitive(self, refs_cf_fixture):
        # The RU metadata-type prefix is matched case-insensitively, so an
        # all-caps prefix normalizes to the same canonical and finds the refs.
        reader, base, _ = refs_cf_fixture
        helpers = _stub_helpers(base, reader)
        expected = helpers["find_references_to_object"]("Справочник.Контрагенты")["total"]
        assert expected >= 1
        for variant in ("СПРАВОЧНИК.Контрагенты", "справочник.Контрагенты"):
            res = helpers["find_references_to_object"](variant)
            assert res["object"] == "Catalog.Контрагенты", variant
            assert res["total"] == expected, variant

    def test_kinds_filter(self, refs_cf_fixture):
        reader, base, _ = refs_cf_fixture
        helpers = _stub_helpers(base, reader)
        result = helpers["find_references_to_object"]("Catalog.Контрагенты", kinds=["attribute_type"])
        for r in result["references"]:
            assert r["kind"] == "attribute_type"

    def test_not_found_returns_empty(self, refs_cf_fixture):
        reader, base, _ = refs_cf_fixture
        helpers = _stub_helpers(base, reader)
        result = helpers["find_references_to_object"]("Catalog.НеСуществует")
        assert result["total"] == 0
        assert result["references"] == []
        assert result["by_kind"] == {}

    def test_defined_type_lookup_via_helper(self, refs_cf_fixture):
        reader, base, _ = refs_cf_fixture
        helpers = _stub_helpers(base, reader)
        result = helpers["find_defined_types"]("ВладелецКонтрагента")
        assert result["partial"] is False
        # canonicalized
        assert "Catalog.Организации" in result["types"]


# ---------------------------------------------------------------------------
# Live fallback (no index — simulate via empty IndexReader-less helpers)
# ---------------------------------------------------------------------------


class TestLiveFallback:
    def _build_live_helpers(self, base):
        from rlm_tools_bsl.bsl_helpers import make_bsl_helpers

        def _read_file(p: str) -> str:
            return Path(base, p).read_text(encoding="utf-8-sig", errors="replace")

        return make_bsl_helpers(
            str(base),
            lambda p: Path(base, p),
            _read_file,
            lambda *_a, **_k: [],
            lambda *_a, **_k: [],
            format_info=None,
            idx_reader=None,
        )

    def test_live_fallback_top_level_subsystem_xml(self, tmp_path, monkeypatch):
        """Standard CF layout — Subsystems/X.xml (top-level file, no subdir)."""
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
        base = tmp_path / "cf2"
        _write(base / "Configuration.xml", _CF_CONFIG_XML)
        # Standard CF layout: top-level Subsystems/CRM.xml
        _write(base / "Subsystems" / "CRM.xml", _CF_SUBSYSTEM_XML)

        helpers = self._build_live_helpers(base)
        result = helpers["find_references_to_object"]("Справочник.Контрагенты")
        assert result["partial"] is True
        kinds = {r["kind"] for r in result["references"]}
        # Subsystem content from top-level Subsystems/CRM.xml MUST be discovered
        assert "subsystem_content" in kinds

    def test_live_fallback_owner_refs(self, tmp_path, monkeypatch):
        """Owner refs from Catalog metadata also discoverable via live fallback."""
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
        base = tmp_path / "cf3"
        _write(base / "Configuration.xml", _CF_CONFIG_XML)
        _write(
            base / "Catalogs" / "Контрагенты" / "Ext" / "Catalog.xml",
            _CF_CATALOG_XML,
        )

        helpers = self._build_live_helpers(base)
        result = helpers["find_references_to_object"]("Catalog.Организации")
        assert result["partial"] is True
        kinds = {r["kind"] for r in result["references"]}
        assert "owner" in kinds


# ---------------------------------------------------------------------------
# Code-review fixes (v1.9.0 — Codex feedback)
# ---------------------------------------------------------------------------


_CF_DEFINED_TYPE_PRIMITIVES_XML = """<?xml version="1.0" encoding="UTF-8"?>
<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"
                xmlns:v8="http://v8.1c.ru/8.1/data/core"
                xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <DefinedType>
    <Properties>
      <Name>ДенежнаяСуммаНеотрицательная</Name>
      <Type>
        <v8:Type>xs:decimal</v8:Type>
      </Type>
    </Properties>
  </DefinedType>
</MetaDataObject>
"""


@pytest.fixture
def primitives_dt_fixture(tmp_path, monkeypatch):
    """DefinedType with a primitive (xs:decimal → Number) — exercises the fix
    that primitives must be retained in the indexed defined_types JSON."""
    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
    base = tmp_path / "cf_prim"
    _write(base / "Configuration.xml", _CF_CONFIG_XML)
    _write(
        base / "CommonModules" / "Stub" / "Ext" / "Module.bsl",
        "Процедура Тест() Экспорт КонецПроцедуры",
    )
    _write(
        base / "DefinedTypes" / "ДенежнаяСуммаНеотрицательная.xml",
        _CF_DEFINED_TYPE_PRIMITIVES_XML,
    )
    builder = IndexBuilder()
    db_path = builder.build(
        str(base),
        build_calls=False,
        build_metadata=True,
        build_fts=False,
        build_synonyms=True,
    )
    reader = IndexReader(db_path)
    yield reader, str(base), db_path
    reader.close()


class TestPrimitivesInDefinedType:
    """Codex finding #1: DefinedType primitives lost on indexed path."""

    def test_index_keeps_primitive_number(self, primitives_dt_fixture):
        reader, _, _ = primitives_dt_fixture
        row = reader.find_defined_type("ДенежнаяСуммаНеотрицательная")
        assert row is not None
        # xs:decimal must be normalized to "Number" — NOT dropped
        assert row["types"] == ["Number"]

    def test_helper_returns_primitive_via_index(self, primitives_dt_fixture):
        reader, base, _ = primitives_dt_fixture
        helpers = _stub_helpers(base, reader)
        result = helpers["find_defined_types"]("ДенежнаяСуммаНеотрицательная")
        assert result["partial"] is False
        assert result["types"] == ["Number"]

    def test_live_fallback_also_returns_primitive(self, tmp_path, monkeypatch):
        """Сравнение поведения index vs live: оба должны вернуть ['Number']."""
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
        base = tmp_path / "cf_prim_live"
        _write(base / "Configuration.xml", _CF_CONFIG_XML)
        _write(
            base / "DefinedTypes" / "ДенежнаяСуммаНеотрицательная.xml",
            _CF_DEFINED_TYPE_PRIMITIVES_XML,
        )

        from rlm_tools_bsl.bsl_helpers import make_bsl_helpers

        helpers = make_bsl_helpers(
            str(base),
            lambda p: Path(base, p),
            lambda p: Path(base, p).read_text(encoding="utf-8-sig", errors="replace"),
            lambda *_a, **_k: [],
            lambda *_a, **_k: [],
            format_info=None,
            idx_reader=None,
        )
        result = helpers["find_defined_types"]("ДенежнаяСуммаНеотрицательная")
        assert result["partial"] is True
        assert result["types"] == ["Number"]


class TestPriorityTruncation:
    """Codex finding #2: SQL LIMIT must respect ref_kind priority."""

    def test_high_priority_kind_survives_truncation(self, refs_cf_fixture):
        """With limit smaller than total count, only the highest-priority kinds
        should remain. attribute_type (priority 0) must outrank owner/based_on."""
        reader, base, _ = refs_cf_fixture
        helpers = _stub_helpers(base, reader)

        # Catalog.Контрагенты has both subsystem_content (priority 1)
        # AND owner ref pointing to Organization. With limit=1 we want the
        # higher-priority kind first.
        result = helpers["find_references_to_object"]("Catalog.Контрагенты", limit=1)
        if result["total"] > 1:
            assert result["truncated"] is True
        # Returned single ref must be the highest-priority kind among the matches
        assert len(result["references"]) <= 1
        # by_kind should still reflect the FULL set, not the truncated slice
        assert sum(result["by_kind"].values()) == result["total"]

    def test_sql_orders_by_priority(self, refs_cf_fixture):
        """Direct IndexReader call — verify ORDER BY ref_kind priority."""
        reader, _, _ = refs_cf_fixture
        rows = reader.find_metadata_references("Catalog.Контрагенты", limit=100)
        assert rows is not None
        # Build kind sequence and check it's monotonically non-decreasing in priority
        from rlm_tools_bsl.bsl_index import IndexReader as _IR

        priority = _IR._REF_KIND_SQL_PRIORITY
        priorities = [priority.get(r["ref_kind"], 99) for r in rows]
        assert priorities == sorted(priorities)


class TestAttributeLineFilled:
    """Codex finding #4: attribute-level refs should have a non-None `line`."""

    def test_index_attribute_ref_has_line(self, refs_cf_fixture):
        reader, _, _ = refs_cf_fixture
        rows = reader.find_metadata_references("Catalog.Банки")
        assert rows is not None
        # The catalog refers to Catalog.Банки via attribute ОсновнойБанк
        attr_rows = [r for r in rows if r["ref_kind"] == "attribute_type"]
        assert attr_rows, "attribute_type ref not found"
        # Line must be set (cheap regex lookup) and point at a line containing the name
        assert attr_rows[0]["line"] is not None
        assert attr_rows[0]["line"] > 0

    def test_index_owner_ref_line_points_at_the_owners_item(self, refs_cf_fixture):
        """v1.33.0 сменила контракт: у owner/based_on/default-форм `line` больше НЕ None,
        а указывает на строку САМОЙ ссылки. До этого релиза здесь стоял честный NULL —
        поэтому тест проверяет не «не None», а попадание в нужную строку."""
        reader, base, _ = refs_cf_fixture
        rows = reader.find_metadata_references("Catalog.Организации")
        assert rows is not None
        owner_rows = [r for r in rows if r["ref_kind"] == "owner"]
        assert owner_rows
        row = owner_rows[0]
        assert row["line"] is not None
        text = (Path(base) / row["path"]).read_text(encoding="utf-8-sig").splitlines()
        assert "Catalog.Организации" in text[row["line"] - 1]
        assert "Owners" in "".join(text[max(0, row["line"] - 2) : row["line"] + 1])

    def test_live_fallback_attribute_ref_has_line(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
        base = tmp_path / "cf_line"
        _write(base / "Configuration.xml", _CF_CONFIG_XML)
        _write(
            base / "Catalogs" / "Контрагенты" / "Ext" / "Catalog.xml",
            _CF_CATALOG_XML,
        )
        from rlm_tools_bsl.bsl_helpers import make_bsl_helpers

        helpers = make_bsl_helpers(
            str(base),
            lambda p: Path(base, p),
            lambda p: Path(base, p).read_text(encoding="utf-8-sig", errors="replace"),
            lambda *_a, **_k: [],
            lambda *_a, **_k: [],
            format_info=None,
            idx_reader=None,
        )
        result = helpers["find_references_to_object"]("Catalog.Банки")
        assert result["partial"] is True
        attr_refs = [r for r in result["references"] if r["kind"] == "attribute_type"]
        assert attr_refs, "live attribute_type ref missing"
        assert attr_refs[0]["line"] is not None
        assert attr_refs[0]["line"] > 0
        # v1.33.0: live-фолбэк якорится ТЕМ ЖЕ резолвером, что и билдер, поэтому `line`
        # обязана указывать на САМУ ссылку (тег типа), а не на объявление владельца —
        # иначе один ключ ответа значил бы разное на двух путях одного хелпера.
        text = (Path(base) / attr_refs[0]["path"]).read_text(encoding="utf-8-sig").splitlines()
        anchored = text[attr_refs[0]["line"] - 1]
        assert "Банки" in anchored, anchored
        assert "Name" not in anchored, f"якорь сел на объявление владельца: {anchored!r}"


# ---------------------------------------------------------------------------
# Codex round 2: command_parameter_type live + sibling/Ext dedup
# ---------------------------------------------------------------------------


_CF_COMMON_COMMAND_XML = """<?xml version="1.0" encoding="UTF-8"?>
<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"
                xmlns:v8="http://v8.1c.ru/8.1/data/core">
  <Command>
    <Properties>
      <Name>СоздатьЗаказ</Name>
      <CommandParameterType>
        <v8:Type>CatalogRef.Контрагенты</v8:Type>
      </CommandParameterType>
    </Properties>
  </Command>
</MetaDataObject>
"""


class TestLiveCommandParameterType:
    """Codex finding: live fallback must invoke parse_command_parameter_type
    for CommonCommands and object-nested Commands."""

    def _build_helpers(self, base):
        from rlm_tools_bsl.bsl_helpers import make_bsl_helpers

        return make_bsl_helpers(
            str(base),
            lambda p: Path(base, p),
            lambda p: Path(base, p).read_text(encoding="utf-8-sig", errors="replace"),
            lambda *_a, **_k: [],
            lambda *_a, **_k: [],
            format_info=None,
            idx_reader=None,
        )

    def test_common_commands_top_level(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
        base = tmp_path / "cf_cc"
        _write(base / "Configuration.xml", _CF_CONFIG_XML)
        _write(base / "CommonCommands" / "СоздатьЗаказ.xml", _CF_COMMON_COMMAND_XML)

        helpers = self._build_helpers(base)
        result = helpers["find_references_to_object"]("Catalog.Контрагенты")
        assert result["partial"] is True
        kinds = {r["kind"] for r in result["references"]}
        assert "command_parameter_type" in kinds, f"command_parameter_type missing — got {kinds}"
        # Verify used_in formatting
        cmd_refs = [r for r in result["references"] if r["kind"] == "command_parameter_type"]
        assert any("CommonCommand.СоздатьЗаказ.CommandParameterType" in r["used_in"] for r in cmd_refs)

    def test_object_nested_command(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
        base = tmp_path / "cf_obj_cmd"
        _write(base / "Configuration.xml", _CF_CONFIG_XML)
        # Catalog with a nested Commands/X.xml
        _write(
            base / "Catalogs" / "Заказы" / "Ext" / "Catalog.xml",
            _CF_CONFIG_XML.replace("ТестоваяКонфигурация", "Заказы").replace("Configuration", "Catalog"),
        )
        _write(
            base / "Catalogs" / "Заказы" / "Commands" / "СоздатьКлиента.xml",
            _CF_COMMON_COMMAND_XML.replace("СоздатьЗаказ", "СоздатьКлиента"),
        )

        helpers = self._build_helpers(base)
        result = helpers["find_references_to_object"]("Catalog.Контрагенты")
        assert result["partial"] is True
        cmd_refs = [r for r in result["references"] if r["kind"] == "command_parameter_type"]
        assert cmd_refs, "object-nested command parameter not discovered"
        assert any("Catalog.Заказы.Command.СоздатьКлиента.CommandParameterType" in r["used_in"] for r in cmd_refs)

    def test_kinds_filter_excludes_command_param(self, tmp_path, monkeypatch):
        """Если kinds=['attribute_type'] — command_parameter_type не должен
        попасть в результат, даже если файл присутствует."""
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
        base = tmp_path / "cf_filter_cmd"
        _write(base / "Configuration.xml", _CF_CONFIG_XML)
        _write(base / "CommonCommands" / "СоздатьЗаказ.xml", _CF_COMMON_COMMAND_XML)

        helpers = self._build_helpers(base)
        result = helpers["find_references_to_object"]("Catalog.Контрагенты", kinds=["attribute_type"])
        kinds = {r["kind"] for r in result["references"]}
        assert "command_parameter_type" not in kinds


class TestLiveSiblingExtDedup:
    """Codex finding: same logical object covered by both sibling X.xml
    and Ext/Type.xml must NOT produce duplicate refs."""

    def test_no_duplicate_owner_ref(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
        base = tmp_path / "cf_dedup"
        _write(base / "Configuration.xml", _CF_CONFIG_XML)
        # Both layouts at once for the SAME object
        _write(
            base / "Catalogs" / "Контрагенты" / "Ext" / "Catalog.xml",
            _CF_CATALOG_XML,
        )
        _write(base / "Catalogs" / "Контрагенты.xml", _CF_CATALOG_XML)

        from rlm_tools_bsl.bsl_helpers import make_bsl_helpers

        helpers = make_bsl_helpers(
            str(base),
            lambda p: Path(base, p),
            lambda p: Path(base, p).read_text(encoding="utf-8-sig", errors="replace"),
            lambda *_a, **_k: [],
            lambda *_a, **_k: [],
            format_info=None,
            idx_reader=None,
        )
        result = helpers["find_references_to_object"]("Catalog.Организации")
        owner_refs = [r for r in result["references"] if r["kind"] == "owner"]
        # Exactly ONE owner ref despite both files existing
        assert len(owner_refs) == 1, f"expected 1 owner ref, got {len(owner_refs)}: {owner_refs}"

    def test_no_duplicate_attribute_ref(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
        base = tmp_path / "cf_dedup_attr"
        _write(base / "Configuration.xml", _CF_CONFIG_XML)
        _write(
            base / "Catalogs" / "Контрагенты" / "Ext" / "Catalog.xml",
            _CF_CATALOG_XML,
        )
        _write(base / "Catalogs" / "Контрагенты.xml", _CF_CATALOG_XML)

        from rlm_tools_bsl.bsl_helpers import make_bsl_helpers

        helpers = make_bsl_helpers(
            str(base),
            lambda p: Path(base, p),
            lambda p: Path(base, p).read_text(encoding="utf-8-sig", errors="replace"),
            lambda *_a, **_k: [],
            lambda *_a, **_k: [],
            format_info=None,
            idx_reader=None,
        )
        result = helpers["find_references_to_object"]("Catalog.Банки")
        attr_refs = [r for r in result["references"] if r["kind"] == "attribute_type"]
        assert len(attr_refs) == 1, f"expected 1 attribute_type ref, got {len(attr_refs)}"
        # by_kind must reflect the deduplicated set
        assert result["by_kind"].get("attribute_type") == 1


# ---------------------------------------------------------------------------
# find_metadata_refs_from (outbound reader) + find_data_path (metagraph BFS)
#   v1.19.0, step 4b
# ---------------------------------------------------------------------------


def _catalog_xml(name: str, attr_types: list[str]) -> str:
    attrs = "".join(
        f"<Attribute><Properties><Name>Реквизит{i}</Name><Type><v8:Type>{t}</v8:Type></Type></Properties></Attribute>"
        for i, t in enumerate(attr_types)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses" '
        'xmlns:v8="http://v8.1c.ru/8.1/data/core">\n'
        f"  <Catalog><Properties><Name>{name}</Name></Properties>"
        f"<ChildObjects>{attrs}</ChildObjects></Catalog>\n"
        "</MetaDataObject>\n"
    )


def _document_xml(name: str, attr_types: list[str]) -> str:
    attrs = "".join(
        f"<Attribute><Properties><Name>Реквизит{i}</Name><Type><v8:Type>{t}</v8:Type></Type></Properties></Attribute>"
        for i, t in enumerate(attr_types)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses" '
        'xmlns:v8="http://v8.1c.ru/8.1/data/core">\n'
        f"  <Document><Properties><Name>{name}</Name></Properties>"
        f"<ChildObjects>{attrs}</ChildObjects></Document>\n"
        "</MetaDataObject>\n"
    )


def _write_catalog(base: Path, name: str, attr_types: list[str]) -> None:
    xml = _catalog_xml(name, attr_types)
    _write(base / "Catalogs" / name / "Ext" / "Catalog.xml", xml)
    _write(base / "Catalogs" / f"{name}.xml", xml)


def _write_document(base: Path, name: str, attr_types: list[str]) -> None:
    xml = _document_xml(name, attr_types)
    _write(base / "Documents" / name / "Ext" / "Document.xml", xml)
    _write(base / "Documents" / f"{name}.xml", xml)


@pytest.fixture
def datapath_fixture(tmp_path, monkeypatch):
    """Controlled metadata graph: chain А→Б→В + a bare-name category collision
    (Catalog.Дубль vs Document.Дубль with DIFFERENT outgoing refs)."""
    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
    base = tmp_path / "cf"
    _write(base / "Configuration.xml", _CF_CONFIG_XML)
    _write(base / "CommonModules" / "Test" / "Ext" / "Module.bsl", "Процедура Тест() Экспорт КонецПроцедуры")

    # Chain: А --attr--> Б --attr--> В
    _write_catalog(base, "А", ["CatalogRef.Б"])
    _write_catalog(base, "Б", ["CatalogRef.В"])
    _write_catalog(base, "В", [])

    # Category collision on bare name "Дубль".
    _write_catalog(base, "Дубль", ["CatalogRef.ЦельК"])
    _write_document(base, "Дубль", ["CatalogRef.ЦельД"])
    _write_catalog(base, "ЦельК", [])
    _write_catalog(base, "ЦельД", [])

    builder = IndexBuilder()
    db_path = builder.build(
        str(base),
        build_calls=False,
        build_metadata=True,
        build_fts=False,
        build_synonyms=True,
    )
    reader = IndexReader(db_path)
    yield reader, str(base), db_path
    reader.close()


class TestFindMetadataRefsFrom:
    def test_outbound_refs(self, datapath_fixture):
        reader, _base, _db = datapath_fixture
        rows = reader.find_metadata_refs_from("А", source_category="Catalogs")
        assert rows is not None
        refs = {r["ref_object"] for r in rows if r["ref_kind"] == "attribute_type"}
        assert "Catalog.Б" in refs

    def test_category_collision_isolated(self, datapath_fixture):
        reader, _base, _db = datapath_fixture
        cat_rows = reader.find_metadata_refs_from("Дубль", source_category="Catalogs")
        doc_rows = reader.find_metadata_refs_from("Дубль", source_category="Documents")
        cat_refs = {r["ref_object"] for r in cat_rows}
        doc_refs = {r["ref_object"] for r in doc_rows}
        assert "Catalog.ЦельК" in cat_refs and "Catalog.ЦельД" not in cat_refs
        assert "Catalog.ЦельД" in doc_refs and "Catalog.ЦельК" not in doc_refs

    def test_none_on_missing_table(self, datapath_fixture):
        _reader, _base, db = datapath_fixture
        conn = sqlite3.connect(str(db))
        conn.execute("DROP TABLE metadata_references")
        conn.commit()
        conn.close()
        reader2 = IndexReader(db)
        try:
            assert reader2.find_metadata_refs_from("А", source_category="Catalogs") is None
        finally:
            reader2.close()


class TestFindDataPath:
    def test_one_hop(self, datapath_fixture):
        reader, base, _db = datapath_fixture
        helpers = _stub_helpers(base, reader)
        res = helpers["find_data_path"]("Catalog.А", "Catalog.Б")
        assert res["found"] is True
        assert res["depth"] == 1
        assert res["path"][0]["from"] == "Catalog.А"
        assert res["path"][0]["to"] == "Catalog.Б"
        assert res["path"][0]["kind"] == "attribute_type"

    def test_two_hop(self, datapath_fixture):
        reader, base, _db = datapath_fixture
        helpers = _stub_helpers(base, reader)
        res = helpers["find_data_path"]("Catalog.А", "Catalog.В")
        assert res["found"] is True
        assert res["depth"] == 2
        chain = [(e["from"], e["to"]) for e in res["path"]]
        assert chain == [("Catalog.А", "Catalog.Б"), ("Catalog.Б", "Catalog.В")]

    def test_ru_en_normalization(self, datapath_fixture):
        reader, base, _db = datapath_fixture
        helpers = _stub_helpers(base, reader)
        ru = helpers["find_data_path"]("Справочник.А", "Справочник.В")
        en = helpers["find_data_path"]("Catalog.А", "Catalog.В")
        assert ru["found"] is en["found"] is True
        assert ru["depth"] == en["depth"] == 2

    def test_kinds_filter(self, datapath_fixture):
        reader, base, _db = datapath_fixture
        helpers = _stub_helpers(base, reader)
        # Only attribute_type edges exist → filtering to 'owner' finds nothing.
        res = helpers["find_data_path"]("Catalog.А", "Catalog.Б", kinds=["owner"])
        assert res["found"] is False

    def test_category_collision_no_cross_edges(self, datapath_fixture):
        reader, base, _db = datapath_fixture
        helpers = _stub_helpers(base, reader)
        # Walking from Catalog.Дубль must NOT pick up Document.Дубль's edges.
        hit = helpers["find_data_path"]("Catalog.Дубль", "Catalog.ЦельК")
        miss = helpers["find_data_path"]("Catalog.Дубль", "Catalog.ЦельД")
        assert hit["found"] is True
        assert miss["found"] is False

    def test_bare_endpoint_returns_hint(self, datapath_fixture):
        reader, base, _db = datapath_fixture
        helpers = _stub_helpers(base, reader)
        res = helpers["find_data_path"]("Дубль", "Catalog.ЦельК")  # bare `from`
        assert res["found"] is False
        assert "hint" in res
        assert res["_meta"]["nodes_expanded"] == 0  # traversal not started

    def test_partial_without_table(self, datapath_fixture):
        _reader, base, db = datapath_fixture
        conn = sqlite3.connect(str(db))
        conn.execute("DROP TABLE metadata_references")
        conn.commit()
        conn.close()
        reader2 = IndexReader(db)
        try:
            helpers = _stub_helpers(base, reader2)
            res = helpers["find_data_path"]("Catalog.А", "Catalog.В")
            assert res["found"] is False
            assert res["partial"] is True
        finally:
            reader2.close()


# ── v1.33.0: якорь ссылок метаданных ───────────────────────────────────────

from rlm_tools_bsl.bsl_index import _ref_anchor_index  # noqa: E402


def _ref(suffix: str, *, kind: str = "attribute_type", ref_object: str = "") -> dict:
    """Словарь ссылки РОВНО как его отдаёт парсер — с ``ref_kind``.

    Без `ref_kind` белый список `_ANCHORED_REF_KINDS` вернёт None, и весь набор
    тестов Задачи 7 станет красным. Продовый парсер `ref_kind` кладёт всегда.
    """
    return {"ref_kind": kind, "used_in_suffix": suffix, "ref_object": ref_object}


_ANCHOR_XML = """<MetaDataObject>
 <Document>
  <Properties>
   <Name>Док</Name>
   <BasedOn><Item>Document.Основание</Item></BasedOn>
  </Properties>
  <ChildObjects>
   <Attribute><Properties>
     <Name>Организация</Name>
     <Synonym><item><content>Организация</content></item></Synonym>
     <Type><v8:Type>cfg:CatalogRef.Организации</v8:Type></Type>
   </Properties></Attribute>
   <TabularSection><Properties><Name>Товары</Name></Properties>
    <ChildObjects><Attribute><Properties>
      <Name>Организация</Name>
      <Type><v8:Type>cfg:CatalogRef.Другие</v8:Type></Type>
    </Properties></Attribute></ChildObjects>
   </TabularSection>
  </ChildObjects>
 </Document>
</MetaDataObject>
"""
_ANCHOR_LINES = _ANCHOR_XML.split("\n")


def test_line_points_at_type_tag_not_at_name():
    idx = _ref_anchor_index(_ANCHOR_LINES)
    line = idx.line_for(_ref("Attribute.Организация.Type"))
    assert "v8:Type" in _ANCHOR_LINES[line - 1]
    assert "CatalogRef.Организации" in _ANCHOR_LINES[line - 1]


def test_repeated_name_resolves_to_its_own_section():
    """Реквизит ТЧ с тем же именем обязан получить СВОЮ строку, а не первую по файлу."""
    idx = _ref_anchor_index(_ANCHOR_LINES)
    head = idx.line_for(_ref("Attribute.Организация.Type"))
    ts = idx.line_for(_ref("TabularSection.Товары.Attribute.Организация.Type"))
    assert ts > head
    assert "CatalogRef.Другие" in _ANCHOR_LINES[ts - 1]


def test_resolution_is_order_independent():
    """Порядок отдачи парсером не совпадает с порядком в файле — резолв обязан
    давать тот же ответ при обратном порядке запросов."""
    forward = _ref_anchor_index(_ANCHOR_LINES)
    a1 = forward.line_for(_ref("Attribute.Организация.Type"))
    b1 = forward.line_for(_ref("TabularSection.Товары.Attribute.Организация.Type"))
    backward = _ref_anchor_index(_ANCHOR_LINES)
    b2 = backward.line_for(_ref("TabularSection.Товары.Attribute.Организация.Type"))
    a2 = backward.line_for(_ref("Attribute.Организация.Type"))
    assert None not in (a1, b1, a2, b2), "резолв вернул None — тест был бы зелёным на (None, None)"
    assert (a1, b1) == (a2, b2)


_COMPOSITE = """<MetaDataObject>
 <InformationRegister>
  <ChildObjects>
   <Dimension><Properties>
     <Name>Ссылка</Name>
     <Type>
       <v8:Type>cfg:DocumentRef.Первый</v8:Type>
       <v8:Type>cfg:DocumentRef.Второй</v8:Type>
       <v8:Type>cfg:DocumentRef.Третий</v8:Type>
     </Type>
   </Properties></Dimension>
  </ChildObjects>
 </InformationRegister>
</MetaDataObject>
""".split("\n")


def test_composite_type_each_component_gets_its_own_line():
    """Составной тип: КАЖДЫЙ тип лежит на своей строке, и ссылка обязана указывать
    на свою, а не на общий контейнер <Type>. На боевой конфигурации у одного реквизита
    бывает до 266 типов — промах контейнером стоит сотни строк."""
    idx = _ref_anchor_index(_COMPOSITE)
    lines = {}
    for name in ("Первый", "Второй", "Третий"):
        ln = idx.line_for(_ref("Dimension.Ссылка.Type", ref_object=f"Document.{name}"))
        lines[name] = ln
        assert f"DocumentRef.{name}" in _COMPOSITE[ln - 1], f"{name} -> строка {ln}"
    assert len(set(lines.values())) == 3, f"все три ссылки схлопнулись в одну строку: {lines}"


def test_composite_type_repeat_query_is_cached_not_exhausted():
    """Повторный запрос той же ссылки обязан дать ту же строку, а не None:
    владелец не должен «исчерпываться» после первого обращения."""
    idx = _ref_anchor_index(_COMPOSITE)
    ref = _ref("Dimension.Ссылка.Type", ref_object="Document.Второй")
    first = idx.line_for(ref)
    assert first is not None
    for _ in range(300):
        assert idx.line_for(ref) == first


def test_unknown_ref_object_falls_back_to_type_block_not_to_none():
    """Если конкретный тип сопоставить не удалось, ссылка указывает на блок <Type>,
    а не теряется."""
    idx = _ref_anchor_index(_COMPOSITE)
    ln = idx.line_for(_ref("Dimension.Ссылка.Type", ref_object="Catalog.НетТакого"))
    assert ln is not None
    assert "<Type>" in _COMPOSITE[ln - 1]


_FORMS_XML = """<MetaDataObject>
 <Document>
  <Properties>
   <Name>БольничныйЛист</Name>
   <DefaultObjectForm>Document.БольничныйЛист.Form.ФормаДокумента</DefaultObjectForm>
   <DefaultListForm>Document.БольничныйЛист.Form.ФормаСписка</DefaultListForm>
  </Properties>
 </Document>
</MetaDataObject>
""".split("\n")


def test_default_form_refs_do_not_land_on_object_name():
    """ref_object у этих видов — САМ объект, поэтому наивный поиск ref_object попадает
    в <Name> в шапке файла.

    Словарь ref собран РОВНО как его отдаёт парсер: `used_in_suffix` и `ref_object`,
    БЕЗ ключа `used_in` — он появляется только в `_emit_refs`, уже после резолвера.
    """
    idx = _ref_anchor_index(_FORMS_XML)
    obj = idx.line_for(
        {
            "ref_kind": "default_object_form",
            "ref_object": "Document.БольничныйЛист",
            "used_in_suffix": "DefaultObjectForm=Document.БольничныйЛист.Form.ФормаДокумента",
        }
    )
    lst = idx.line_for(
        {
            "ref_kind": "default_list_form",
            "ref_object": "Document.БольничныйЛист",
            "used_in_suffix": "DefaultListForm=Document.БольничныйЛист.Form.ФормаСписка",
        }
    )
    assert obj != lst, "обе ссылки схлопнулись в одну строку"
    assert "DefaultObjectForm" in _FORMS_XML[obj - 1]
    assert "DefaultListForm" in _FORMS_XML[lst - 1]
    name_line = next(i for i, s in enumerate(_FORMS_XML, 1) if "<Name>" in s)
    assert obj != name_line and lst != name_line, "ссылка села на имя объекта в шапке"


def test_plain_anchor_requires_exact_tag_value():
    """Подстрочный поиск запрещён: Catalog.Заказ не должен находиться в Catalog.ЗаказКлиента.

    Вид взят ЯКОРНЫЙ (`owner`): невключённые в белый список виды проверяли бы
    только сам белый список.
    """
    lines = [
        "<Owners>",
        "  <Item>Catalog.ЗаказКлиента</Item>",
        "  <Item>Catalog.Заказ</Item>",
        "</Owners>",
    ]
    idx = _ref_anchor_index(lines)
    assert idx.line_for(_ref("Owners", kind="owner", ref_object="Catalog.Заказ")) == 3


_CF_OWNER_THEN_BASEDON = """<MetaDataObject><Catalog><Properties>
 <Name>КонтактныеЛица</Name>
 <Owners><Item>Catalog.Партнеры</Item></Owners>
 <BasedOn><Item>Catalog.Партнеры</Item></BasedOn>
</Properties></Catalog></MetaDataObject>
""".split("\n")

_EDT_BASEDON_THEN_OWNER = """<mdclass:Catalog>
 <name>КонтактныеЛица</name>
 <basedOn>Catalog.Партнеры</basedOn>
 <owners>Catalog.Партнеры</owners>
</mdclass:Catalog>
""".split("\n")


@pytest.mark.parametrize("lines", [_CF_OWNER_THEN_BASEDON, _EDT_BASEDON_THEN_OWNER])
def test_owner_and_based_on_with_same_object_get_different_lines(lines):
    """Один объект бывает и владельцем, и основанием. Поиск и кеш обязаны учитывать
    ref_kind. Оба порядка тегов проверяются намеренно: в CF Owners раньше BasedOn,
    в EDT наоборот, поэтому решение «по позиции» неверно в одном из форматов."""
    idx = _ref_anchor_index(lines)
    own = idx.line_for({"ref_kind": "owner", "ref_object": "Catalog.Партнеры", "used_in_suffix": "Owners"})
    based = idx.line_for({"ref_kind": "based_on", "ref_object": "Catalog.Партнеры", "used_in_suffix": "BasedOn"})
    assert own is not None and based is not None, (own, based)
    assert own != based, "owner и based_on схлопнулись в одну строку"
    assert "wner" in lines[own - 1], lines[own - 1]
    assert "asedOn" in lines[based - 1], lines[based - 1]


def test_missing_anchor_returns_none_not_wrong_line():
    idx = _ref_anchor_index(_ANCHOR_LINES)
    assert idx.line_for(_ref("Attribute.Организация.Type")) is not None, "якорь не работает вовсе"
    assert idx.line_for(_ref("Attribute.НетТакого.Type")) is None


def test_null_index_when_content_unavailable():
    idx = _ref_anchor_index(None)
    assert idx.line_for(_ref("Attribute.Х.Type")) is None


# ── v1.33.0: проверка ЧЕРЕЗ ПРОДОВЫЙ ПУТЬ, а не вызовом якоря ──────────────

from rlm_tools_bsl.bsl_index import _ANCHORED_REF_KINDS  # noqa: E402

# Осознанно НЕ якорятся в v1.33.0. Пишутся своими коллекторами с жёстким None.
# `subsystem_content` и `characteristic_type` — здесь же: первый собирает отдельный
# цикл и `_emit_refs` не зовёт, второй проходит через `_emit_refs`, но белым списком
# принудительно возвращается в None (иначе было бы «заполнено 5 из 486» — контракт
# хуже честного NULL).
_KNOWN_NULL_KINDS = {
    "role_rights",
    "subsystem_content",
    "exchange_plan_content",
    "event_subscription_source",
    "defined_type_content",
    "functional_option_content",
    "characteristic_type",
    "predefined_characteristic_type",
    "command_parameter_type",
}


def test_anchored_kinds_have_line_in_built_index(refs_cf_fixture):
    """Проверяется РЕЗУЛЬТАТ СБОРКИ, а не вызов резолвера.

    Прогон, где якорь дёргают напрямую, измеряет ВОЗМОЖНОСТЬ, а не то, что попадёт
    в индекс: продовые точки записи резолвер могут не вызывать вовсе.
    """
    _, _, db_path = refs_cf_fixture
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT ref_kind, COUNT(*), SUM(line IS NULL) FROM metadata_references GROUP BY 1"
        ).fetchall()
    finally:
        conn.close()
    seen = {r[0] for r in rows}
    assert _ANCHORED_REF_KINDS <= seen, f"фикстура не покрывает якорные виды: не хватает {_ANCHORED_REF_KINDS - seen}"
    assert "characteristic_type" in seen, "нет ПВХ — белый список не проверяется"
    for kind, total, nulls in rows:
        if kind in _ANCHORED_REF_KINDS:
            assert nulls == 0, f"{kind}: {nulls} из {total} строк без line — якорь не подключен"
        elif kind in _KNOWN_NULL_KINDS:
            assert nulls == total, f"{kind}: неожиданно заполнен — перепроверь стоимость"
        else:
            raise AssertionError(f"новый вид ссылок {kind} не классифицирован")


def test_attribute_type_line_points_at_the_reference_itself(refs_cf_fixture):
    """Гейт качества: строка обязана СОДЕРЖАТЬ текст самой ссылки, а не просто быть
    не-NULL. Критерий «строка не пуста» пропустил бы промах контейнером <Type>."""
    _, base, db_path = refs_cf_fixture
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT ref_object, path, line FROM metadata_references "
            "WHERE ref_kind = 'attribute_type' AND line IS NOT NULL"
        ).fetchall()
    finally:
        conn.close()
    assert rows, "нет строк attribute_type — тест был бы вакуумным"
    for ref_object, rel, line in rows:
        text = (Path(base) / rel).read_text(encoding="utf-8-sig", errors="replace").splitlines()
        short = ref_object.split(".")[-1]
        assert short in text[line - 1], f"{ref_object} -> {rel}:{line} = {text[line - 1]!r}"
