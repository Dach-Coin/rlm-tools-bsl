"""Tests for v1.12.0 extension visibility from a main-config session.

Covers Issue #14: extension modules and objects must be visible to
``find_module`` / ``find_attributes`` / ``find_predefined`` / ``parse_object_xml``
when the sandbox base is a main config and there are nearby extensions.
"""

from __future__ import annotations

import os
import re
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


def _ext_xml(name="ExtAddOn", purpose="Customization", prefix="ext_"):
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
    Процедура ОбработкаЗаполнения(ДанныеЗаполнения, СтандартнаяОбработка) Экспорт
        // основная логика
    КонецПроцедуры
""")


_EXT_MODULE_BSL = textwrap.dedent("""\
    Процедура ExtOnlyMethod(Параметр) Экспорт
        Возврат Параметр;
    КонецПроцедуры
""")


_EXT_OVERRIDE_BSL = textwrap.dedent("""\
    &После("ОбработкаЗаполнения")
    Процедура ext_ОбработкаЗаполнения(ДанныеЗаполнения, СтандартнаяОбработка)
        // расширенная логика
    КонецПроцедуры
""")


# Catalog XML with attribute + predefined item (CF format)
_EXT_CATALOG_XML = textwrap.dedent("""\
    <?xml version="1.0" encoding="UTF-8"?>
    <MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"
                    xmlns:v8="http://v8.1c.ru/8.1/data/core">
        <Catalog>
            <Properties>
                <Name>ExtCatalog</Name>
                <Synonym><v8:item><v8:lang>ru</v8:lang><v8:content>Расш Каталог</v8:content></v8:item></Synonym>
            </Properties>
            <ChildObjects>
                <Attribute>
                    <Properties>
                        <Name>ExtAttr</Name>
                        <Synonym><v8:item><v8:lang>ru</v8:lang><v8:content>Расш Реквизит</v8:content></v8:item></Synonym>
                        <Type>
                            <v8:Type>xs:string</v8:Type>
                        </Type>
                    </Properties>
                </Attribute>
            </ChildObjects>
        </Catalog>
    </MetaDataObject>
""")


_EXT_PREDEFINED_XML = textwrap.dedent("""\
    <?xml version="1.0" encoding="UTF-8"?>
    <PredefinedData xmlns="http://v8.1c.ru/8.3/MDClasses"
                    xmlns:v8="http://v8.1c.ru/8.1/data/core">
        <Item>
            <Name>PreItem1</Name>
            <Synonym><v8:item><v8:lang>ru</v8:lang><v8:content>Предопределённый 1</v8:content></v8:item></Synonym>
            <Code>001</Code>
            <IsFolder>false</IsFolder>
        </Item>
    </PredefinedData>
""")


# Subsystem with empty <Synonym> — exercises the "metadata-XML pass without synonym filter"
# branch. _collect_object_synonyms drops these; _iter_metadata_xml_files keeps them.
_EXT_SUBSYSTEM_XML = textwrap.dedent("""\
    <?xml version="1.0" encoding="UTF-8"?>
    <MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"
                    xmlns:v8="http://v8.1c.ru/8.1/data/core"
                    xmlns:xr="http://v8.1c.ru/8.3/xcf/readable">
        <Subsystem>
            <Properties>
                <Name>ExtSubsystemNoSyn</Name>
                <Synonym/>
                <Content>
                    <xr:Item>Catalog.ExtCatalog</xr:Item>
                </Content>
            </Properties>
        </Subsystem>
    </MetaDataObject>
""")


# XML-only Catalog (no ObjectModule.bsl) for find_attributes by bare name tests.
_EXT_CATALOG_NO_MODULE_XML = textwrap.dedent("""\
    <?xml version="1.0" encoding="UTF-8"?>
    <MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"
                    xmlns:v8="http://v8.1c.ru/8.1/data/core">
        <Catalog>
            <Properties>
                <Name>ExtCatNoModule</Name>
                <Synonym><v8:item><v8:lang>ru</v8:lang><v8:content>Без модуля</v8:content></v8:item></Synonym>
            </Properties>
            <ChildObjects>
                <Attribute>
                    <Properties>
                        <Name>NoModAttr</Name>
                        <Type><v8:Type>xs:string</v8:Type></Type>
                    </Properties>
                </Attribute>
            </ChildObjects>
        </Catalog>
    </MetaDataObject>
""")


_EXT_CATALOG_NO_MODULE_PREDEFINED = textwrap.dedent("""\
    <?xml version="1.0" encoding="UTF-8"?>
    <PredefinedData xmlns="http://v8.1c.ru/8.3/MDClasses"
                    xmlns:v8="http://v8.1c.ru/8.1/data/core">
        <Item>
            <Name>NoModItem</Name>
            <Code>050</Code>
            <IsFolder>false</IsFolder>
        </Item>
    </PredefinedData>
""")


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _make_main_with_extension(parent_dir):
    """Create src/cf (main) and src/cfe/ExtAddOn (extension) with rich content."""
    cf = os.path.join(parent_dir, "src", "cf")
    cfe = os.path.join(parent_dir, "src", "cfe", "ExtAddOn")

    # Main
    _write(os.path.join(cf, "Configuration.xml"), _CF_MAIN_XML)
    _write(
        os.path.join(cf, "Catalogs", "MainCat", "Ext", "ObjectModule.bsl"),
        _MAIN_MODULE_BSL,
    )
    _write(
        os.path.join(cf, "CommonModules", "MainModule", "Ext", "Module.bsl"),
        "Процедура MainProc() Экспорт\nКонецПроцедуры\n",
    )

    # Extension
    _write(os.path.join(cfe, "Configuration.xml"), _ext_xml())
    _write(
        os.path.join(cfe, "CommonModules", "ExtOnlyModule", "Ext", "Module.bsl"),
        _EXT_MODULE_BSL,
    )
    # Object metadata is a SIBLING Catalogs/<Name>.xml — the real CF/CFE dump
    # layout (verified against real CF, CFE and EDT sources). Ext/ holds only
    # modules + Predefined.xml.
    _write(
        os.path.join(cfe, "Catalogs", "ExtCatalog.xml"),
        _EXT_CATALOG_XML,
    )
    _write(
        os.path.join(cfe, "Catalogs", "ExtCatalog", "Ext", "Predefined.xml"),
        _EXT_PREDEFINED_XML,
    )
    _write(
        os.path.join(cfe, "Catalogs", "ExtCatalog", "Ext", "ObjectModule.bsl"),
        "Процедура ExtCatHandler() Экспорт\nКонецПроцедуры\n",
    )
    # XML-only Subsystem (no <Synonym>) — kept under Ext/ on purpose to exercise
    # the (now order-independent) Ext/*.xml fallback branch of
    # _iter_metadata_xml_files.
    _write(
        os.path.join(cfe, "Subsystems", "ExtSubsystemNoSyn", "Ext", "Subsystem.xml"),
        _EXT_SUBSYSTEM_XML,
    )
    # XML-only Catalog (no .bsl module) — sibling metadata, only Predefined.xml in Ext/.
    _write(
        os.path.join(cfe, "Catalogs", "ExtCatNoModule.xml"),
        _EXT_CATALOG_NO_MODULE_XML,
    )
    _write(
        os.path.join(cfe, "Catalogs", "ExtCatNoModule", "Ext", "Predefined.xml"),
        _EXT_CATALOG_NO_MODULE_PREDEFINED,
    )

    return cf, cfe


@pytest.fixture()
def helpers_with_ext(tmp_path):
    cf, cfe = _make_main_with_extension(tmp_path)
    base = cf
    generic, resolve_safe = make_helpers(base)
    fmt = detect_format(base)
    bsl = make_bsl_helpers(
        base_path=base,
        resolve_safe=resolve_safe,
        read_file_fn=generic["read_file"],
        grep_fn=generic["grep"],
        glob_files_fn=generic["glob_files"],
        format_info=fmt,
        idx_reader=None,
        extension_paths=[cfe],
    )
    return bsl, cf, cfe


@pytest.fixture()
def helpers_with_idx_reader(tmp_path, monkeypatch):
    """Same fixture but with a real IndexReader behind the helpers."""
    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
    cf, cfe = _make_main_with_extension(tmp_path)
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


# ---------------------------------------------------------------------------
# find_module / find_by_type
# ---------------------------------------------------------------------------


class TestFindModuleExtension:
    def test_find_module_returns_extension_module(self, helpers_with_ext):
        bsl, _cf, _cfe = helpers_with_ext
        results = bsl["find_module"]("ExtOnlyModule")
        assert results, "extension module must be discoverable from main session"
        # All matches should map to the extension via '../cfe/...' relative path.
        ext_paths = [r for r in results if r["path"].startswith("../cfe/")]
        assert ext_paths, f"no '../cfe/' results in: {results}"
        assert ext_paths[0]["object_name"] == "ExtOnlyModule"

    def test_find_module_with_idx_reader(self, helpers_with_idx_reader):
        """Extension pass must run AFTER idx_reader.get_all_modules() — closes H1."""
        bsl, _cf, _cfe, _reader = helpers_with_idx_reader
        results = bsl["find_module"]("ExtOnlyModule")
        ext_paths = [r for r in results if r["path"].startswith("../cfe/")]
        assert ext_paths, "extension modules must be visible even when main pass was loaded from idx_reader"

    def test_find_by_type_includes_extension_catalog(self, helpers_with_ext):
        bsl, _cf, _cfe = helpers_with_ext
        results = bsl["find_by_type"]("Catalogs")
        names = {(r["object_name"], r["path"]) for r in results}
        assert any(name == "ExtCatalog" and path.startswith("../cfe/") for name, path in names)

    def test_find_module_order_main_first_then_extensions(self, helpers_with_ext):
        bsl, _cf, _cfe = helpers_with_ext
        # 'Module' substring matches both main MainModule and ext ExtOnlyModule
        results = bsl["find_module"]("Module")
        paths = [r["path"] for r in results]
        # main path appears before any ext path
        main_indices = [i for i, p in enumerate(paths) if not p.startswith("../")]
        ext_indices = [i for i, p in enumerate(paths) if p.startswith("../")]
        if main_indices and ext_indices:
            assert max(main_indices) < min(ext_indices), paths


# ---------------------------------------------------------------------------
# find_attributes / find_predefined for extension objects
# ---------------------------------------------------------------------------


class TestExtensionAttributes:
    def test_find_attributes_for_extension_object(self, helpers_with_ext):
        bsl, _cf, _cfe = helpers_with_ext
        attrs = bsl["find_attributes"](object_name="ExtCatalog")
        assert any(a.get("attr_name") == "ExtAttr" for a in attrs), attrs

    def test_find_attributes_for_xml_only_extension_object_by_bare_name(self, helpers_with_ext):
        """No .bsl module — must still auto-resolve via _extension_metadata_xml (round-4 H)."""
        bsl, _cf, _cfe = helpers_with_ext
        attrs = bsl["find_attributes"](object_name="ExtCatNoModule")
        assert attrs, "XML-only ext object must be resolvable by bare name"
        assert any(a.get("attr_name") == "NoModAttr" for a in attrs), attrs

    def test_find_attributes_for_xml_only_extension_object_case_insensitive(self, helpers_with_ext):
        """Lower-case bare name must still resolve via canonical metadata entry (round-5 M)."""
        bsl, _cf, _cfe = helpers_with_ext
        attrs = bsl["find_attributes"](object_name="extcatnomodule")
        assert attrs, "case-mismatched bare ext name must auto-resolve"
        assert any(a.get("attr_name") == "NoModAttr" for a in attrs), attrs

    def test_find_predefined_for_extension_object(self, helpers_with_ext):
        bsl, _cf, _cfe = helpers_with_ext
        items = bsl["find_predefined"](object_name="ExtCatalog")
        assert items, "ext Predefined.xml must be resolvable"
        assert any(it.get("item_name") == "PreItem1" for it in items)

    def test_find_predefined_for_xml_only_extension_object_by_bare_name(self, helpers_with_ext):
        bsl, _cf, _cfe = helpers_with_ext
        items = bsl["find_predefined"](object_name="ExtCatNoModule")
        assert items, "XML-only ext predefined must be reachable by bare name"
        assert any(it.get("item_name") == "NoModItem" for it in items)

    def test_find_attributes_name_only_scans_extensions(self, helpers_with_ext):
        """codex review round 1 #1 — name-only find_attributes must surface ext attrs."""
        bsl, _cf, _cfe = helpers_with_ext
        rows = bsl["find_attributes"](name="ExtAttr")
        assert rows, "name-only find_attributes must find extension attributes"
        assert any(r.get("attr_name") == "ExtAttr" for r in rows)

    def test_find_predefined_name_only_scans_extensions(self, helpers_with_ext):
        """codex review round 1 #1 — name-only find_predefined must surface ext items."""
        bsl, _cf, _cfe = helpers_with_ext
        items = bsl["find_predefined"](name="PreItem1")
        assert items, "name-only find_predefined must find extension predefined items"
        assert any(it.get("item_name") == "PreItem1" for it in items)

    def test_search_unified_attributes_scope_includes_extension(self, helpers_with_ext):
        bsl, _cf, _cfe = helpers_with_ext
        rows = bsl["search"]("ExtAttr", scope="attributes")
        ext_attrs = [r for r in rows if r["source_type"] == "attribute" and r.get("object_name") == "ExtCatalog"]
        assert ext_attrs, rows

    def test_search_unified_predefined_scope_includes_extension(self, helpers_with_ext):
        bsl, _cf, _cfe = helpers_with_ext
        rows = bsl["search"]("PreItem1", scope="predefined")
        ext_preds = [r for r in rows if r["source_type"] == "predefined" and r.get("object_name") == "ExtCatalog"]
        assert ext_preds, rows

    def test_search_attributes_path_resolves_to_extension_xml(self, helpers_with_ext):
        """codex round 2 #1 — search() must surface the ext XML path so the agent
        can navigate; empty path would weaken the contract."""
        bsl, _cf, _cfe = helpers_with_ext
        rows = bsl["search"]("ExtAttr", scope="attributes")
        ext_rows = [r for r in rows if r["source_type"] == "attribute" and r.get("object_name") == "ExtCatalog"]
        assert ext_rows
        for r in ext_rows:
            assert r["path"], f"empty path on ext attribute hit: {r}"
            assert r["path"].startswith("../cfe/") or "ExtCatalog" in r["path"], r

    def test_search_predefined_path_resolves_to_extension_xml(self, helpers_with_ext):
        bsl, _cf, _cfe = helpers_with_ext
        rows = bsl["search"]("PreItem1", scope="predefined")
        ext_rows = [r for r in rows if r["source_type"] == "predefined" and r.get("object_name") == "ExtCatalog"]
        assert ext_rows
        for r in ext_rows:
            assert r["path"], f"empty path on ext predefined hit: {r}"
            assert r["path"].startswith("../cfe/") or "ExtCatalog" in r["path"], r

    def test_find_attributes_live_rows_have_source_file(self, helpers_with_ext):
        bsl, _cf, _cfe = helpers_with_ext
        rows = bsl["find_attributes"](object_name="ExtCatalog")
        assert rows
        for r in rows:
            assert r.get("source_file"), f"missing source_file on live row: {r}"

    def test_find_predefined_live_rows_have_source_file(self, helpers_with_ext):
        bsl, _cf, _cfe = helpers_with_ext
        items = bsl["find_predefined"](object_name="ExtCatalog")
        assert items
        for it in items:
            assert it.get("source_file"), f"missing source_file on live row: {it}"

    def test_find_attributes_synonym_only_ext_match_wins_over_saturated_main(self, tmp_path, monkeypatch):
        """codex round 6 — ext attribute that matches by attr_synonym (not by
        attr_name) must still be visible when main index returns `limit` rows.
        IndexReader filters on attr_name OR attr_synonym, so rank-merge must
        consider both fields."""
        from rlm_tools_bsl.bsl_helpers import make_bsl_helpers
        from rlm_tools_bsl.bsl_index import IndexBuilder, IndexReader
        from rlm_tools_bsl.format_detector import detect_format
        from rlm_tools_bsl.helpers import make_helpers

        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
        cf = os.path.join(str(tmp_path), "src", "cf")
        cfe = os.path.join(str(tmp_path), "src", "cfe", "ExtSynOnly")

        _write(os.path.join(cf, "Configuration.xml"), _CF_MAIN_XML)
        # Make a catalog with 500 attributes whose ATTR_NAME starts with
        # "unique" — they'll all be rank 1 (prefix on attr_name). Index returns
        # them with limit applied; rank function for main also recognises them
        # as rank 1 ("prefix"). Ext row's attr_name does NOT start with
        # "unique" but attr_synonym IS exactly "unique" → rank 0 via synonym.
        attrs_xml = "\n".join(
            f"""\
                <Attribute>
                    <Properties>
                        <Name>uniqueAttr{i:03d}</Name>
                        <Synonym><v8:item><v8:lang>ru</v8:lang><v8:content>noise {i}</v8:content></v8:item></Synonym>
                        <Type><v8:Type>xs:string</v8:Type></Type>
                    </Properties>
                </Attribute>"""
            for i in range(500)
        )
        _write(
            os.path.join(cf, "Catalogs", "BulkCat", "Ext", "Catalog.xml"),
            textwrap.dedent(f"""\
                <?xml version="1.0" encoding="UTF-8"?>
                <MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"
                                xmlns:v8="http://v8.1c.ru/8.1/data/core">
                    <Catalog>
                        <Properties>
                            <Name>BulkCat</Name>
                        </Properties>
                        <ChildObjects>
                            {attrs_xml}
                        </ChildObjects>
                    </Catalog>
                </MetaDataObject>
            """),
        )

        _write(os.path.join(cfe, "Configuration.xml"), _ext_xml(name="ExtSynOnly"))
        _write(
            os.path.join(cfe, "Catalogs", "ExtCat", "Ext", "Catalog.xml"),
            textwrap.dedent("""\
                <?xml version="1.0" encoding="UTF-8"?>
                <MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"
                                xmlns:v8="http://v8.1c.ru/8.1/data/core">
                    <Catalog>
                        <Properties>
                            <Name>ExtCat</Name>
                        </Properties>
                        <ChildObjects>
                            <Attribute>
                                <Properties>
                                    <Name>SomeCode</Name>
                                    <Synonym><v8:item><v8:lang>ru</v8:lang><v8:content>unique</v8:content></v8:item></Synonym>
                                    <Type><v8:Type>xs:string</v8:Type></Type>
                                </Properties>
                            </Attribute>
                        </ChildObjects>
                    </Catalog>
                </MetaDataObject>
            """),
        )

        builder = IndexBuilder()
        db_path = builder.build(cf, build_calls=False, build_metadata=True, build_fts=False)
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
            rows = bsl["find_attributes"](name="unique", limit=500)
            ext_rows = [r for r in rows if r.get("object_name") == "ExtCat"]
            assert ext_rows, (
                "ext attr matching by attr_synonym must be visible when main "
                "saturates limit; top-5 attr_names: "
                f"{[r.get('attr_name') for r in rows[:5]]}"
            )
            # Strongest: synonym exact match → rank 0 → position 0.
            assert rows[0].get("object_name") == "ExtCat", rows[0]
        finally:
            reader.close()

    def test_parse_object_xml_for_xml_only_extension_object_without_synonym(self, helpers_with_ext):
        """Subsystems/ExtSubsystemNoSyn has empty <Synonym>.

        ``_collect_object_synonyms`` drops it; ``_extension_metadata_xml`` must
        still include it via the independent path-scan (round-3 H1).
        """
        bsl, _cf, _cfe = helpers_with_ext
        parsed = bsl["parse_object_xml"]("Subsystems/ExtSubsystemNoSyn")
        assert parsed, "must return non-empty structure for XML-only ext subsystem"
        assert parsed.get("name") == "ExtSubsystemNoSyn"


# ---------------------------------------------------------------------------
# Sandbox security: multi-root resolver
# ---------------------------------------------------------------------------


class TestExtensionSandboxResolver:
    def test_resolve_safe_rejects_path_traversal_with_extensions(self, helpers_with_ext, tmp_path):
        """Generic read_file still strictly base-only when extensions are configured."""
        bsl, _cf, _cfe = helpers_with_ext
        # Hit the generic resolver through read_file via the make_helpers path.
        # We re-build a small generic helper attached to the same base.
        generic, _resolve_safe = make_helpers(_cf)
        with pytest.raises(PermissionError):
            generic["read_file"]("../cfe/ExtAddOn/Configuration.xml")

    def test_ext_resolve_safe_blocks_outside_roots(self, helpers_with_ext):
        bsl, _cf, _cfe = helpers_with_ext
        # read_procedure goes through _ext_read_file → _ext_resolve_safe
        with pytest.raises(PermissionError):
            bsl["read_procedure"]("../../../etc/passwd", "Foo")
        with pytest.raises(PermissionError):
            bsl["parse_object_xml"]("../../../../foo")

    def test_ext_resolve_safe_accepts_extension_root_paths(self, helpers_with_ext):
        bsl, _cf, _cfe = helpers_with_ext
        # Read a real extension BSL file via the high-level helper.
        body = bsl["read_procedure"]("../cfe/ExtAddOn/CommonModules/ExtOnlyModule/Ext/Module.bsl", "ExtOnlyMethod")
        assert body is not None and "ExtOnlyMethod" in body


# ---------------------------------------------------------------------------
# Regression: main-only sessions are unaffected
# ---------------------------------------------------------------------------


class TestMainOnlyRegression:
    def test_main_without_extensions_keeps_existing_behavior(self, tmp_path):
        cf = os.path.join(str(tmp_path), "src", "cf")
        _write(os.path.join(cf, "Configuration.xml"), _CF_MAIN_XML)
        _write(
            os.path.join(cf, "CommonModules", "MainOnly", "Ext", "Module.bsl"),
            "Процедура MainProc() Экспорт\nКонецПроцедуры\n",
        )
        generic, resolve_safe = make_helpers(cf)
        fmt = detect_format(cf)
        bsl = make_bsl_helpers(
            base_path=cf,
            resolve_safe=resolve_safe,
            read_file_fn=generic["read_file"],
            grep_fn=generic["grep"],
            glob_files_fn=generic["glob_files"],
            format_info=fmt,
            idx_reader=None,
            extension_paths=[],
        )
        # find_module works
        rows = bsl["find_module"]("MainOnly")
        assert rows and all(not r["path"].startswith("../") for r in rows)
        # generic read_file still rejects extension-like paths
        with pytest.raises(PermissionError):
            generic["read_file"]("../cfe/anything")

    def test_extension_with_only_overrides_preserves_override_contracts(self, tmp_path, monkeypatch):
        """Round-7 critical: extension with only overrides (no new objects) must
        leave existing override contracts intact AND add a second find_module
        row for the overridden module.
        """
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
        cf = os.path.join(str(tmp_path), "src", "cf")
        cfe = os.path.join(str(tmp_path), "src", "cfe", "OverridesOnly")

        _write(os.path.join(cf, "Configuration.xml"), _CF_MAIN_XML)
        _write(
            os.path.join(cf, "CommonModules", "TargetModule", "Ext", "Module.bsl"),
            _MAIN_MODULE_BSL,
        )
        _write(os.path.join(cfe, "Configuration.xml"), _ext_xml(name="OverridesOnly", prefix="ov_"))
        _write(
            os.path.join(cfe, "CommonModules", "TargetModule", "Ext", "Module.bsl"),
            _EXT_OVERRIDE_BSL,
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
            # get_overrides still returns the indexed override
            ov = bsl["get_overrides"]()
            assert ov["overrides"], "get_overrides must still report extension overrides"

            # find_module returns BOTH main and ext rows for the same name
            rows = bsl["find_module"]("TargetModule")
            paths = [r["path"] for r in rows]
            main_paths = [p for p in paths if not p.startswith("../")]
            ext_paths = [p for p in paths if p.startswith("../")]
            assert main_paths, paths
            assert ext_paths, paths

            # search_methods: both main and ext methods visible with different module_path
            sm = bsl["search_methods"]("ОбработкаЗаполнения")
            mods = {r["module_path"] for r in sm}
            assert any(not m.startswith("../") for m in mods), mods
            assert any(m.startswith("../") for m in mods), mods
        finally:
            reader.close()


class TestMetadataXmlDiscoveryFallback:
    """Regression: the Ext/*.xml fallback of _iter_metadata_xml_files must be
    order-independent and never pick Predefined.xml as the object metadata
    locator. A GitHub ubuntu-latest readdir-order change once made it pick
    Predefined.xml first → find_attributes returned []. Real CF/CFE use a
    sibling Obj.xml and EDT uses Obj.mdo, but the fallback must be robust too.
    """

    @staticmethod
    def _make_ext_only_catalog(root):
        # Object whose metadata sits inside Ext/ with NO sibling and NO .mdo,
        # alongside Predefined.xml — the exact layout that triggered the bug.
        ext_dir = os.path.join(root, "Catalogs", "Obj", "Ext")
        os.makedirs(ext_dir)
        _write(os.path.join(ext_dir, "Catalog.xml"), _EXT_CATALOG_XML)
        _write(os.path.join(ext_dir, "Predefined.xml"), _EXT_PREDEFINED_XML)

    def test_fallback_picks_object_xml_not_predefined(self, tmp_path):
        from rlm_tools_bsl.bsl_index import _iter_metadata_xml_files

        self._make_ext_only_catalog(str(tmp_path))
        locators = _iter_metadata_xml_files(str(tmp_path))
        obj = [(c, o, rel) for (c, o, rel) in locators if c == "Catalogs" and o == "Obj"]
        assert len(obj) == 1, locators
        rel = obj[0][2]
        assert rel.endswith("Catalog.xml"), rel
        assert "Predefined.xml" not in rel

    def test_fallback_order_independent_reverse_iterdir(self, tmp_path, monkeypatch):
        """Force reverse iterdir (Predefined.xml first) — the CI failure condition."""
        import pathlib

        from rlm_tools_bsl.bsl_index import _iter_metadata_xml_files

        self._make_ext_only_catalog(str(tmp_path))
        _orig = pathlib.Path.iterdir
        monkeypatch.setattr(
            pathlib.Path,
            "iterdir",
            lambda self: iter(sorted(_orig(self), key=lambda p: p.name, reverse=True)),
        )
        locators = _iter_metadata_xml_files(str(tmp_path))
        rel = next(rel for (c, o, rel) in locators if c == "Catalogs" and o == "Obj")
        assert rel.endswith("Catalog.xml"), rel


def test_profile_modules_carry_main_cfe_provenance(helpers_with_idx_reader):
    """get_object_profile tags each module with main/CFE provenance (is_extension /
    source_root / extension_name) so a main+extension profile is not ambiguous (codex finding)."""
    bsl, cf, cfe, reader = helpers_with_idx_reader

    # extension-only object → its module is flagged as coming from the extension
    ext = bsl["get_object_profile"]("ExtCatalog")
    assert ext["_meta"]["extension_visibility"] == "main_with_nearby_extensions"
    mods = ext["sections"]["modules"]
    om = next(i for i in mods["items"] if i["path"].endswith("ObjectModule.bsl"))
    assert om["is_extension"] is True
    assert om["extension_name"] == "ExtAddOn"
    assert om["source_root"] and "ExtAddOn" in om["source_root"]
    assert mods["summary"]["extension_modules"] >= 1
    assert mods["_meta"]["extension_visibility"] == "main_with_nearby_extensions"

    # main object → its module is NOT an extension, and the ext count is 0
    main = bsl["get_object_profile"]("MainCat")
    mmods = main["sections"]["modules"]
    mom = next(i for i in mmods["items"] if i["path"].endswith("ObjectModule.bsl"))
    assert mom["is_extension"] is False
    assert mom["extension_name"] is None
    assert mom["source_root"] is None
    assert mmods["summary"]["extension_modules"] == 0


def test_profile_extension_name_is_metadata_name_not_folder(helpers_with_idx_reader, monkeypatch):
    """extension_name is the REAL configured name (extension_detector parses Configuration.xml/.mdo),
    NOT the folder basename — guards the dir-vs-metadata divergence (codex finding)."""
    import types

    import rlm_tools_bsl.extension_detector as _ed

    bsl, cf, cfe, reader = helpers_with_idx_reader

    # The extension folder is 'ExtAddOn'; force its configured metadata name to differ.
    def _fake_ctx(_base):
        return types.SimpleNamespace(nearby_extensions=[types.SimpleNamespace(path=cfe, name="РеальноеИмяРасширения")])

    monkeypatch.setattr(_ed, "detect_extension_context", _fake_ctx)

    p = bsl["get_object_profile"]("ExtCatalog")
    om = next(i for i in p["sections"]["modules"]["items"] if i["is_extension"])
    assert om["extension_name"] == "РеальноеИмяРасширения"  # metadata name, NOT folder 'ExtAddOn'


# ---------------------------------------------------------------------------
# v1.36.0 — analyze_subsystem: граница текущего корня названа, а не замаскирована
# ---------------------------------------------------------------------------
def test_analyze_subsystem_live_declares_current_root_scope(helpers_with_ext):
    bsl, _cf, _cfe = helpers_with_ext
    # Предусловие: XML-only подсистема расширения реально поддержана общим
    # extension-resolver и существует в статичном дереве.
    assert bsl["parse_object_xml"]("Subsystems/ExtSubsystemNoSyn")["name"] == "ExtSubsystemNoSyn"
    res = bsl["analyze_subsystem"]("ExtSubsystemNoSyn")
    assert "error" in res
    assert res["_meta"]["extensions_included"] is False
    assert "расширен" in res["hint"].lower()


def test_analyze_subsystem_index_declares_current_root_scope(helpers_with_idx_reader):
    bsl, _cf, _cfe, _reader = helpers_with_idx_reader
    res = bsl["analyze_subsystem"]("ExtSubsystemNoSyn")
    assert "error" in res
    assert res["_meta"]["source"] == "index"
    assert res["_meta"]["extensions_included"] is False
    assert "текущ" in res["hint"].lower()


@pytest.mark.parametrize("with_index", [False, True])
def test_analyze_subsystem_direct_cfe_reads_its_current_root(tmp_path, monkeypatch, with_index):
    """R75: current root может быть CFE; это не превращает ответ в main-only.

    Никакого merge здесь нет: читается только текущий CFE, nearby main/другие CFE
    остаются за границей. Проверяются обе реально поддержанные ветки.
    """
    _cf, cfe = _make_main_with_extension(tmp_path)
    reader = None
    try:
        if with_index:
            monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx-cfe"))
            db = IndexBuilder().build(cfe, build_calls=False, build_metadata=True, build_fts=False)
            reader = IndexReader(db)
        generic, resolve_safe = make_helpers(cfe, idx_reader=reader)
        bsl = make_bsl_helpers(
            base_path=cfe,
            resolve_safe=resolve_safe,
            read_file_fn=generic["read_file"],
            grep_fn=generic["grep"],
            glob_files_fn=generic["glob_files"],
            format_info=detect_format(cfe),
            idx_reader=reader,
            extension_paths=[],
            current_config_role="extension",
            current_config_name="ExtAddOn",
            current_config_root=cfe,
        )
        res = bsl["analyze_subsystem"]("ExtSubsystemNoSyn")
        (row,) = res["subsystems"]
        assert row["raw_content"] == ["Catalog.ExtCatalog"]
        assert res["_meta"]["source"] == ("index" if with_index else "live")
        assert res["_meta"]["extensions_included"] is False
        assert "текущ" in res["hint"].lower()
        assert "расширен" in res["hint"].lower()
    finally:
        if reader is not None:
            reader.close()


@pytest.mark.parametrize("with_index", [False, True])
def test_analyze_subsystem_empty_cfe_ext_layout_is_found(tmp_path, monkeypatch, with_index):
    """R84: CF-Ext layout остаётся достижимым при пустом Content.

    Дерево статично во время чтения: Content заменяется ДО build/make_helpers.
    Это current-root CFE, а не overlay с main.
    """
    _cf, cfe = _make_main_with_extension(tmp_path)
    rel = "Subsystems/ExtSubsystemNoSyn/Ext/Subsystem.xml"
    empty_xml = re.sub(r"<Content>.*?</Content>", "<Content/>", _EXT_SUBSYSTEM_XML, flags=re.DOTALL)
    assert "<Content/>" in empty_xml and "xr:Item" not in empty_xml, "фикстура пустого состава не собралась"
    _write(os.path.join(cfe, *rel.split("/")), empty_xml)
    reader = None
    try:
        if with_index:
            monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx-cfe-empty"))
            db = IndexBuilder().build(cfe, build_calls=False, build_metadata=True, build_fts=False)
            reader = IndexReader(db)
            with reader._lock:
                n_content = reader._conn.execute(
                    "SELECT COUNT(*) FROM subsystem_content WHERE subsystem_name='ExtSubsystemNoSyn'"
                ).fetchone()[0]
                n_synonyms = reader._conn.execute(
                    "SELECT COUNT(*) FROM object_synonyms "
                    "WHERE category='Subsystems' AND object_name='ExtSubsystemNoSyn'"
                ).fetchone()[0]
            assert (n_content, n_synonyms) == (0, 0), (
                "предусловие: единственным индексным источником обязан быть file_paths"
            )
            lookup = reader.get_subsystem_lookup("ExtSubsystemNoSyn")
            assert lookup["direct"] == []
            assert lookup["direct_candidates"] == [rel]

        generic, resolve_safe = make_helpers(cfe, idx_reader=reader)
        bsl = make_bsl_helpers(
            base_path=cfe,
            resolve_safe=resolve_safe,
            read_file_fn=generic["read_file"],
            grep_fn=generic["grep"],
            glob_files_fn=generic["glob_files"],
            format_info=detect_format(cfe),
            idx_reader=reader,
            extension_paths=[],
            current_config_role="extension",
            current_config_name="ExtAddOn",
            current_config_root=cfe,
        )
        res = bsl["analyze_subsystem"]("ExtSubsystemNoSyn")
        (row,) = res["subsystems"]
        assert row["name"] == "ExtSubsystemNoSyn" and row["match"] == "name"
        assert row["total_objects"] == row["objects_returned"] == 0
        assert row["raw_content"] == [] and row["content_truncated"] is False
        assert res["_meta"]["source"] == ("index" if with_index else "live")
        assert res["_meta"]["extensions_included"] is False
    finally:
        if reader is not None:
            reader.close()
