"""Tests for the v1.14.0 reverse code-usage feature:
metadata_code_usages table, _extract_code_usages, IndexReader.find_code_usages/
count_code_usages, the find_code_usages helper + find_references_to_object(include_code=True),
plus mapping-parity and EDT-layout tripwires.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from rlm_tools_bsl.bsl_index import (
    BUILDER_VERSION,
    IndexBuilder,
    IndexReader,
    _extract_code_usages,
)
from rlm_tools_bsl.bsl_xml_parsers import (
    _CODE_MANAGER_COLLECTIONS,
    _CODE_QUERY_COLLECTIONS,
    _RU_META_FORMS,
    _RU_REFTYPE_TO_CANONICAL,
    canonicalize_type_ref,
)


def _write(path: str | Path, content: str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


_CF_CONFIG_XML = """<?xml version="1.0" encoding="UTF-8"?>
<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">
  <Configuration><Properties><Name>ТестКонфиг</Name></Properties></Configuration>
</MetaDataObject>
"""

# A module exercising all three usage kinds + the source-aware negatives.
_USAGE_MODULE = """\
Процедура Тест() Экспорт
    Док = Документы.ПриобретениеТоваровУслуг.СоздатьДокумент();
    Тип = Тип("ДокументСсылка.ПриобретениеТоваровУслуг");
    Запрос.Текст = "ВЫБРАТЬ * ИЗ Документ.ПриобретениеТоваровУслуг.Товары КАК Т";
    // Документы.ПриобретениеТоваровУслуг в комментарии — НЕ должно ловиться как manager
    Сообщить("Документы.ПриобретениеТоваровУслуг — внутри строки, НЕ manager");
КонецПроцедуры
"""


# ---------------------------------------------------------------------------
# Unit: _extract_code_usages (no index)
# ---------------------------------------------------------------------------
class TestExtractor:
    def test_manager_ru(self):
        rows = _extract_code_usages(["Док = Документы.ПриобретениеТоваровУслуг.Создать();"])
        assert ("Document.ПриобретениеТоваровУслуг", None, "manager", 1) in rows

    def test_manager_en(self):
        rows = _extract_code_usages(["X = Documents.Acme.CreateDocument();"])
        assert ("Document.Acme", None, "manager", 1) in rows

    def test_reftype_ru(self):
        rows = _extract_code_usages(['Т = Тип("СправочникСсылка.Контрагенты");'])
        assert ("Catalog.Контрагенты", None, "ref_type", 1) in rows

    def test_reftype_en_via_canonicalize(self):
        rows = _extract_code_usages(['Т = Тип("DocumentRef.Заказ");'])
        assert ("Document.Заказ", None, "ref_type", 1) in rows

    def test_builder_extraction_keeps_pre_1_28_case_contract(self):
        """Public canonicalization accepts case variants, persisted BUILD does not widen."""
        assert canonicalize_type_ref("documentref.Заказ") == "Document.Заказ"
        exact = _extract_code_usages(['Т = Тип("DocumentRef.Заказ");'])
        lower = _extract_code_usages(['Т = Тип("documentref.Заказ");'])
        assert ("Document.Заказ", None, "ref_type", 1) in exact
        assert ("Document.Заказ", None, "ref_type", 1) not in lower

    def test_query_with_tabular_section_member(self):
        rows = _extract_code_usages(['Текст = "ИЗ Документ.ПриобретениеТоваровУслуг.Товары";'])
        assert ("Document.ПриобретениеТоваровУслуг", "Товары", "query", 1) in rows

    def test_query_without_member(self):
        rows = _extract_code_usages(['Текст = "ИЗ Документ.ПриобретениеТоваровУслуг КАК Д";'])
        assert ("Document.ПриобретениеТоваровУслуг", None, "query", 1) in rows

    def test_query_multiline_literal(self):
        # Realistic multi-line 1C query: the table reference is on a continuation
        # line of a string literal opened earlier (1C marks it with leading '|').
        rows = _extract_code_usages(
            [
                'Запрос.Текст = "ВЫБРАТЬ',
                "|ИЗ Документ.ПриобретениеТоваровУслуг.Товары КАК Т",
                '|ГДЕ Истина";',
            ]
        )
        # The usage must be attributed to the continuation line (2), not dropped.
        assert ("Document.ПриобретениеТоваровУслуг", "Товары", "query", 2) in rows

    def test_query_multiline_en(self):
        rows = _extract_code_usages(['Q.Text = "SELECT', '|FROM Document.Acme.Goods AS G";'])
        assert ("Document.Acme", "Goods", "query", 2) in rows

    def test_escaped_quote_does_not_break_scan(self):
        # "" is an escaped quote inside the string, not a terminator — the manager
        # call after the literal must still be detected.
        rows = _extract_code_usages(['А = "x""y"; Док = Документы.Заказ.Создать();'])
        assert ("Document.Заказ", None, "manager", 1) in rows

    def test_source_aware_manager_not_in_comment(self):
        rows = _extract_code_usages(["// Документы.ПриобретениеТоваровУслуг.Создать();"])
        assert rows == []

    def test_source_aware_manager_not_in_string(self):
        # Plural collection inside a string literal must NOT be a manager usage
        # (and plural is not a query singular either) — so no rows at all.
        rows = _extract_code_usages(['Сообщить("Документы.ПриобретениеТоваровУслуг");'])
        assert rows == []

    def test_source_aware_manager_not_in_multiline_string(self):
        # A plural collection token on a CONTINUATION line of a multi-line string
        # literal must NOT be mis-indexed as a manager usage (the scan tracks
        # string state across lines).
        rows = _extract_code_usages(
            [
                'Текст = "Старт',
                '|Документы.ПриобретениеТоваровУслуг.Создать() внутри строки";',
            ]
        )
        assert all(r[2] != "manager" for r in rows)

    def test_manager_after_closing_string_on_same_line(self):
        rows = _extract_code_usages(['Сообщить("x"); Док = Документы.Заказ.Создать();'])
        assert ("Document.Заказ", None, "manager", 1) in rows

    def test_reftype_not_caught_outside_string(self):
        # ДокументСсылка.X written as bare code (not a literal) must NOT be ref_type.
        rows = _extract_code_usages(["Х = ДокументСсылка.ПриобретениеТоваровУслуг;"])
        assert all(r[2] != "ref_type" for r in rows)

    def test_full_module_all_kinds(self):
        rows = _extract_code_usages(_USAGE_MODULE.splitlines())
        kinds = {r[2] for r in rows}
        assert kinds == {"manager", "ref_type", "query"}
        objects = {r[0] for r in rows}
        assert objects == {"Document.ПриобретениеТоваровУслуг"}
        # member only on the query row
        query = [r for r in rows if r[2] == "query"]
        assert query and query[0][1] == "Товары"


# ---------------------------------------------------------------------------
# Mapping parity (closed-set tripwire)
# ---------------------------------------------------------------------------
class TestMappingParity:
    def test_forms_have_nonempty_singular_plural(self):
        for canonical, forms in _RU_META_FORMS.items():
            assert canonical.endswith("."), canonical
            for key in ("ru_singular", "ru_plural", "en_singular", "en_plural"):
                assert forms.get(key), f"{canonical} missing {key}"

    def test_canonicals_recognized_by_canonicalize(self):
        # Every canonical category must round-trip through the shared
        # canonicalizer (already-canonical passthrough) — keeps _RU_META_FORMS
        # from drifting away from canonicalize_type_ref's known heads.
        for canonical in _RU_META_FORMS:
            sample = canonical + "X"
            assert canonicalize_type_ref(sample) == sample, canonical

    def test_derived_maps_cover_plural_and_singular(self):
        for canonical, forms in _RU_META_FORMS.items():
            assert _CODE_MANAGER_COLLECTIONS[forms["ru_plural"].lower()] == canonical
            assert _CODE_MANAGER_COLLECTIONS[forms["en_plural"].lower()] == canonical
            assert _CODE_QUERY_COLLECTIONS[forms["ru_singular"].lower()] == canonical
            assert _CODE_QUERY_COLLECTIONS[forms["en_singular"].lower()] == canonical

    def test_ru_reftype_map(self):
        assert _RU_REFTYPE_TO_CANONICAL["документссылка."] == "Document."
        assert _RU_REFTYPE_TO_CANONICAL["справочникссылка."] == "Catalog."
        for canonical, forms in _RU_META_FORMS.items():
            for rt in forms.get("reftypes", []):
                assert _RU_REFTYPE_TO_CANONICAL[rt.lower() + "."] == canonical


# ---------------------------------------------------------------------------
# Builder + Reader (CF fixture with a real .bsl module)
# ---------------------------------------------------------------------------
@pytest.fixture
def cf_index(tmp_path, monkeypatch):
    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
    base = tmp_path / "cf"
    _write(base / "Configuration.xml", _CF_CONFIG_XML)
    _write(base / "CommonModules" / "ТестМодуль" / "Ext" / "Module.bsl", _USAGE_MODULE)
    builder = IndexBuilder()
    db_path = builder.build(
        str(base),
        build_calls=False,
        build_metadata=False,  # code-derived table must be built even WITHOUT metadata
        build_fts=False,
        build_synonyms=False,
    )
    reader = IndexReader(db_path)
    yield reader, str(base), db_path, builder
    reader.close()


class TestBuilderReader:
    def test_builder_version_is_14(self):
        assert BUILDER_VERSION == 14

    def test_table_exists(self, cf_index):
        _, _, db_path, _ = cf_index
        conn = sqlite3.connect(str(db_path))
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        assert "metadata_code_usages" in names

    def test_built_without_metadata(self, cf_index):
        # build_metadata=False above — table must still be populated.
        reader, _, _, _ = cf_index
        counts = reader.count_code_usages("Document.ПриобретениеТоваровУслуг")
        assert counts is not None
        assert counts["total"] == 3
        assert counts["by_kind"] == {"manager": 1, "ref_type": 1, "query": 1}

    def test_find_shape_and_join(self, cf_index):
        reader, _, _, _ = cf_index
        rows = reader.find_code_usages("Document.ПриобретениеТоваровУслуг")
        assert rows is not None and len(rows) == 3
        for r in rows:
            assert set(r) == {"path", "object_name", "category", "module_type", "line", "kind", "member"}
            assert r["object_name"] == "ТестМодуль"
            assert r["category"] == "CommonModules"
            assert r["module_type"] == "Module"
        query = [r for r in rows if r["kind"] == "query"]
        assert query and query[0]["member"] == "Товары"

    def test_kind_filter(self, cf_index):
        reader, _, _, _ = cf_index
        rows = reader.find_code_usages("Document.ПриобретениеТоваровУслуг", kind="manager")
        assert rows is not None and len(rows) == 1 and rows[0]["kind"] == "manager"

    def test_case_insensitive_cyrillic(self, cf_index):
        reader, _, _, _ = cf_index
        lower = reader.count_code_usages("document.приобретениетоваровуслуг")
        upper = reader.count_code_usages("Document.ПриобретениеТоваровУслуг")
        assert lower is not None and lower["total"] == upper["total"] == 3

    def test_empty_table_semantics(self, cf_index):
        # Table present, no usages for this object → total 0, NOT None.
        reader, _, _, _ = cf_index
        counts = reader.count_code_usages("Document.НесуществующийДокумент")
        assert counts == {"total": 0, "by_kind": {}}
        rows = reader.find_code_usages("Document.НесуществующийДокумент")
        assert rows == []

    def test_statistics_counts_table(self, cf_index):
        reader, _, _, _ = cf_index
        stats = reader.get_statistics()
        assert stats.get("metadata_code_usages") == 3


# ---------------------------------------------------------------------------
# Incremental update (full-scan fallback on a non-git tmp dir)
# ---------------------------------------------------------------------------
class TestIncremental:
    def test_modify_module_resyncs_usages(self, cf_index):
        reader, base, db_path, builder = cf_index
        assert reader.count_code_usages("Document.ПриобретениеТоваровУслуг")["total"] == 3
        reader.close()  # release the WAL handle before update rewrites the DB

        # Rewrite the module: drop all old usages, add a new (Catalog) one.
        mod = Path(base) / "CommonModules" / "ТестМодуль" / "Ext" / "Module.bsl"
        mod.write_text(
            "Процедура Тест() Экспорт\n    С = Справочники.Контрагенты.НайтиПоКоду(1);\nКонецПроцедуры\n",
            encoding="utf-8",
        )
        builder.update(base)

        # Old usages gone, new one present (DELETE + reINSERT through full-scan path).
        r2 = IndexReader(db_path)
        try:
            assert r2.count_code_usages("Document.ПриобретениеТоваровУслуг")["total"] == 0
            assert r2.count_code_usages("Catalog.Контрагенты")["total"] == 1
        finally:
            r2.close()


# ---------------------------------------------------------------------------
# Helper layer: find_code_usages + include_code
# ---------------------------------------------------------------------------
def _make_helpers(base, idx_reader, grep_fn=None):
    from rlm_tools_bsl.bsl_helpers import make_bsl_helpers

    return make_bsl_helpers(
        str(base),
        lambda p: Path(base, p),
        lambda p: Path(base, p).read_text(encoding="utf-8-sig", errors="replace"),
        grep_fn or (lambda *_a, **_k: []),
        lambda *_a, **_k: [],
        format_info=None,
        idx_reader=idx_reader,
    )


class TestHelper:
    def test_find_code_usages_fast_path(self, cf_index):
        reader, base, _, _ = cf_index
        helpers = _make_helpers(base, reader)
        res = helpers["find_code_usages"]("Документ.ПриобретениеТоваровУслуг")
        assert res["partial"] is False
        assert res["total"] == 3
        assert res["by_kind"] == {"manager": 1, "ref_type": 1, "query": 1}
        assert res["_meta"]["scope"] == "main_config"
        assert res["_meta"]["extensions_included"] is False

    def test_find_code_usages_registered(self, cf_index):
        reader, base, _, _ = cf_index
        helpers = _make_helpers(base, reader)
        assert "find_code_usages" in helpers

    def test_find_code_usages_case_insensitive_prefix(self, cf_index):
        # The metadata-type prefix is normalized case-insensitively (RU and EN),
        # and the object name is matched via lowercased object_ref_key — so all
        # of these variants must return the same usages as the canonical form.
        reader, base, _, _ = cf_index
        helpers = _make_helpers(base, reader)
        expected = helpers["find_code_usages"]("Документ.ПриобретениеТоваровУслуг")["total"]
        assert expected == 3
        for variant in (
            "ДОКУМЕНТ.ПриобретениеТоваровУслуг",  # uppercase RU prefix
            "документ.ПриобретениеТоваровУслуг",  # lowercase RU prefix
            "Document.ПриобретениеТоваровУслуг",  # EN prefix
            "document.приобретениетоваровуслуг",  # all lowercase
        ):
            assert helpers["find_code_usages"](variant)["total"] == expected, variant

    def test_get_index_info_capability(self, cf_index):
        reader, base, _, _ = cf_index
        helpers = _make_helpers(base, reader)
        info = helpers["get_index_info"]()
        assert info["has_metadata_code_usages"] is True
        assert info["metadata_code_usages_count"] == 3

    def test_include_code_adds_keys(self, cf_index):
        reader, base, _, _ = cf_index
        helpers = _make_helpers(base, reader)
        res = helpers["find_references_to_object"]("Документ.ПриобретениеТоваровУслуг", include_code=True)
        for key in ("code_usages", "code_total", "code_by_kind", "code_truncated", "code_partial", "code_meta"):
            assert key in res
        assert res["code_total"] == 3

    def test_include_code_false_omits_keys(self, cf_index):
        reader, base, _, _ = cf_index
        helpers = _make_helpers(base, reader)
        res = helpers["find_references_to_object"]("Документ.ПриобретениеТоваровУслуг")
        assert "code_usages" not in res
        assert "code_total" not in res

    def test_live_fallback_when_no_table(self, tmp_path, monkeypatch):
        # idx_reader=None → reader path skipped → partial=True live fallback.
        base = tmp_path / "nolive"
        _write(base / "Configuration.xml", _CF_CONFIG_XML)
        helpers = _make_helpers(base, None)
        res = helpers["find_code_usages"]("Документ.ПриобретениеТоваровУслуг")
        assert res["partial"] is True
        assert "hint" in res["_meta"]


# ---------------------------------------------------------------------------
# EDT parity — same extraction + shape on the EDT layout (no Ext/, .mdo present)
# ---------------------------------------------------------------------------
class TestEDTParity:
    def test_edt_layout_attribution(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx_edt"))
        base = tmp_path / "edt"
        _write(base / "Configuration.xml", _CF_CONFIG_XML)
        # EDT object module: Documents/<Name>/ObjectModule.bsl (NO Ext/) + a .mdo sibling.
        _write(base / "Documents" / "ЗаказКлиента" / "ЗаказКлиента.mdo", "<mdo/>")
        _write(
            base / "Documents" / "ЗаказКлиента" / "ObjectModule.bsl",
            "Процедура ПриПроведении()\n"
            "    Рег = РегистрыСведений.Цены.СоздатьНаборЗаписей();\n"
            '    Т = Тип("ДокументСсылка.ЗаказКлиента");\n'
            "КонецПроцедуры\n",
        )
        builder = IndexBuilder()
        db_path = builder.build(
            str(base), build_calls=False, build_metadata=False, build_fts=False, build_synonyms=False
        )
        reader = IndexReader(db_path)
        try:
            mgr = reader.find_code_usages("InformationRegister.Цены")
            assert mgr and mgr[0]["kind"] == "manager"
            assert mgr[0]["category"] == "Documents"
            assert mgr[0]["object_name"] == "ЗаказКлиента"
            assert mgr[0]["module_type"] == "ObjectModule"
            ref = reader.find_code_usages("Document.ЗаказКлиента", kind="ref_type")
            assert ref and ref[0]["kind"] == "ref_type"
        finally:
            reader.close()
