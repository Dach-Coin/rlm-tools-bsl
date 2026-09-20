"""v1.38.0 — «объявленный состав».

Релиз про то, что конфигурация о себе ЗАЯВЛЯЕТ: наборы типов у подписок, состав
движений документа, права по флагу роли, свойства общих модулей, константы,
макеты. ``BUILDER_VERSION`` 15 → 16, пересборка индексов обязательна.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from rlm_tools_bsl.bsl_helpers import make_bsl_helpers
from rlm_tools_bsl.bsl_index import BUILDER_VERSION, IndexBuilder, IndexReader
from rlm_tools_bsl.bsl_xml_parsers import (
    classify_subscription_row,
    is_type_set_token,
    parse_event_subscription_xml,
    type_set_category,
)
from rlm_tools_bsl.format_detector import detect_format
from rlm_tools_bsl.helpers import make_helpers

CF_DESCRIPTOR = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">\n'
    '  <Configuration uuid="00000000-0000-0000-0000-000000000001">\n'
    "    <Properties><Name>Тест</Name></Properties>\n"
    "  </Configuration>\n"
    "</MetaDataObject>\n"
)


def _cf_subscription(name: str, source_body: str, event: str = "OnWrite") -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"\n'
        '                xmlns:v8="http://v8.1c.ru/8.1/data/core">\n'
        "<EventSubscription><Properties>\n"
        f"<Name>{name}</Name>\n"
        f"<Source>{source_body}</Source>\n"
        f"<Handler>CommonModule.М.{name}Обработчик</Handler>\n"
        f"<Event>{event}</Event>\n"
        "</Properties></EventSubscription></MetaDataObject>\n"
    )


def _edt_subscription(name: str, types: list[str], event: str = "OnWrite") -> str:
    body = "".join(f"<types>{t}</types>" for t in types)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<mdclass:EventSubscription xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass">\n'
        f"<name>{name}</name>\n"
        f"<source>{body}</source>\n"
        f"<handler>CommonModule.М.{name}Обработчик</handler>\n"
        f"<event>{event}</event>\n"
        "</mdclass:EventSubscription>\n"
    )


# ───────────────────────── Задача 6: наборы типов ─────────────────────────


class TestTypeSetTokens:
    def test_dotless_token_is_a_set(self):
        assert is_type_set_token("DocumentObject") is True
        assert is_type_set_token("AccumulationRegisterRecordSet") is True

    def test_dotted_defined_type_is_a_set_too(self):
        """Гард против правила «набор == токен без точки».

        На EDT 90 боевых источников — ТОЧЕЧНЫЕ ``DefinedType.X``. Правило по одной
        бесточечности оставило бы их в ``source_types``, где они продолжали бы молча
        исчезать, то есть половина задачи не была бы сделана.
        """
        assert is_type_set_token("DefinedType.МестоХраненияФО") is True
        assert is_type_set_token("definedtype.X") is True

    def test_ordinary_typed_source_is_not_a_set(self):
        assert is_type_set_token("DocumentObject.Заказ") is False
        assert is_type_set_token("") is False

    def test_platform_sets_expand_through_the_public_canonicalizer(self):
        assert type_set_category("DocumentObject") == "Document"
        assert type_set_category("ConstantValueManager") == "Constant"
        assert type_set_category("AccumulationRegisterRecordSet") == "AccumulationRegister"
        assert type_set_category("ChartOfCharacteristicTypesManager") == "ChartOfCharacteristicTypes"

    def test_tokens_outside_the_map_are_named_not_silent(self):
        """Ровно три боевых токена не раскрываются — они обязаны быть ОТКАЗОМ."""
        for token in ("SequenceRecordSet", "RecalculationRecordSet", "DocumentJournalManager"):
            assert type_set_category(token) == ""
            verdict = classify_subscription_row([], [token], object_name="Х", target_refs=("Document.Х",))
            assert verdict == ("set", "set_unresolved", [], [token])


class TestSubscriptionSourceParsing:
    def test_cf_typeset_node_is_read(self):
        parsed = parse_event_subscription_xml(_cf_subscription("П", "<v8:TypeSet>cfg:DocumentObject</v8:TypeSet>"))
        assert parsed["source_types"] == []
        assert parsed["source_type_sets"] == ["DocumentObject"]

    def test_cf_both_nodes_are_merged_not_chosen(self):
        """Три боевые CF-подписки несут и перечень, и набор — они ОБЪЕДИНЯЮТСЯ."""
        parsed = parse_event_subscription_xml(
            _cf_subscription(
                "П",
                "<v8:Type>cfg:DocumentObject.Заказ</v8:Type><v8:TypeSet>cfg:CatalogObject</v8:TypeSet>",
            )
        )
        assert parsed["source_types"] == ["DocumentObject.Заказ"]
        assert parsed["source_type_sets"] == ["CatalogObject"]

    def test_cf_dotless_token_inside_type_node_is_a_set(self):
        """12 боевых бесточечных ``*Manager`` лежат в ``<v8:Type>``, а не в TypeSet."""
        parsed = parse_event_subscription_xml(_cf_subscription("П", "<v8:Type>cfg:DocumentManager</v8:Type>"))
        assert parsed["source_types"] == []
        assert parsed["source_type_sets"] == ["DocumentManager"]

    def test_edt_dotless_token_is_a_set(self):
        parsed = parse_event_subscription_xml(_edt_subscription("П", ["DocumentObject"]))
        assert parsed["source_types"] == []
        assert parsed["source_type_sets"] == ["DocumentObject"]

    def test_edt_defined_type_goes_to_sets_not_to_source_types(self):
        parsed = parse_event_subscription_xml(_edt_subscription("П", ["DefinedType.МестоХранения"]))
        assert parsed["source_types"] == []
        assert parsed["source_type_sets"] == ["DefinedType.МестоХранения"]

    def test_edt_ordinary_type_stays_in_source_types(self):
        parsed = parse_event_subscription_xml(_edt_subscription("П", ["DocumentObject.Заказ"]))
        assert parsed["source_types"] == ["DocumentObject.Заказ"]
        assert parsed["source_type_sets"] == []


class TestSubscriptionClassification:
    def test_platform_set_matches_its_category_only(self):
        assert classify_subscription_row([], ["DocumentObject"], object_name="З", target_refs=("Document.З",)) == (
            "set",
            "set",
            [],
            ["DocumentObject"],
        )
        assert classify_subscription_row([], ["DocumentObject"], object_name="З", target_refs=("Catalog.З",)) is None

    def test_defined_type_expands_through_its_members(self):
        members = {"Ф": ["Document.З", "Catalog.К"]}
        verdict = classify_subscription_row(
            [],
            ["DefinedType.Ф"],
            object_name="З",
            target_refs=("Document.З",),
            defined_type_members=members.get,
        )
        assert verdict == ("set", "set", [], ["DefinedType.Ф"])
        assert (
            classify_subscription_row(
                [],
                ["DefinedType.Ф"],
                object_name="Ч",
                target_refs=("Document.Ч",),
                defined_type_members=members.get,
            )
            is None
        )

    def test_unknown_defined_type_is_unresolved_not_a_miss(self):
        verdict = classify_subscription_row(
            [], ["DefinedType.Нет"], object_name="З", target_refs=("Document.З",), defined_type_members=lambda n: None
        )
        assert verdict[1] == "set_unresolved"

    def test_homonym_matches_by_any_candidate_category(self):
        """Гард против ОДНОЗНАЧНОГО резолвера категории.

        У голого имени-омонима категорий несколько, и набор ``CatalogObject``
        обязан совпасть независимо от того, какую из них каскад вернул первой.
        """
        verdict = classify_subscription_row(
            [], ["CatalogObject"], object_name="З", target_refs=("Document.З", "Catalog.З")
        )
        assert verdict == ("set", "set", [], ["CatalogObject"])

    def test_unresolvable_category_still_returns_the_row(self):
        verdict = classify_subscription_row([], ["DocumentObject"], object_name="З", target_refs=())
        assert verdict == ("set", "set_unresolved", [], ["DocumentObject"])

    def test_empty_source_stays_universal(self):
        assert classify_subscription_row([], [], object_name="З", target_refs=("Document.З",)) == (
            "universal",
            "none",
            [],
            [],
        )

    def test_exact_type_wins_over_set(self):
        scope, via, types, sets_ = classify_subscription_row(
            ["DocumentObject.З"], ["DocumentObject"], object_name="З", target_refs=("Document.З",)
        )
        assert (scope, via) == ("exact", "type")
        assert types == ["DocumentObject.З"]
        assert sets_ == ["DocumentObject"]


# ───────────────────────── интеграция: индекс v16 ─────────────────────────


def _build_set_fixture(tmp_path, *, edt: bool = False):
    """Документ + константа + подписки на наборы. Возвращает (bsl, reader)."""
    doc_dir = tmp_path / "Documents" / "Заказ" / "Ext"
    doc_dir.mkdir(parents=True)
    (doc_dir / "ObjectModule.bsl").write_text("Процедура П() Экспорт\nКонецПроцедуры\n", encoding="utf-8")
    (tmp_path / "Documents" / "Заказ.xml").write_text(
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses" '
        'xmlns:v8="http://v8.1c.ru/8.1/data/core"><Document><Properties>'
        "<Name>Заказ</Name></Properties></Document></MetaDataObject>",
        encoding="utf-8",
    )
    (tmp_path / "Constants").mkdir()
    (tmp_path / "Constants" / "ВалютаУчета.xml").write_text(
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses" '
        'xmlns:v8="http://v8.1c.ru/8.1/data/core"><Constant><Properties>'
        "<Name>ВалютаУчета</Name><Type><v8:Type>cfg:CatalogRef.Валюты</v8:Type></Type>"
        "</Properties></Constant></MetaDataObject>",
        encoding="utf-8",
    )
    es = tmp_path / "EventSubscriptions"
    es.mkdir()
    if edt:
        for name, types in (
            ("НаборДокументов", ["DocumentObject"]),
            ("НаборКонстант", ["ConstantValueManager"]),
            ("НеРаскрываемый", ["SequenceRecordSet"]),
            ("Точная", ["DocumentObject.Заказ"]),
            ("Универсальная", []),
        ):
            d = es / name
            d.mkdir()
            (d / f"{name}.mdo").write_text(_edt_subscription(name, types), encoding="utf-8")
    else:
        for name, body in (
            ("НаборДокументов", "<v8:TypeSet>cfg:DocumentObject</v8:TypeSet>"),
            ("НаборКонстант", "<v8:TypeSet>cfg:ConstantValueManager</v8:TypeSet>"),
            ("НеРаскрываемый", "<v8:TypeSet>cfg:SequenceRecordSet</v8:TypeSet>"),
            ("Точная", "<v8:Type>cfg:DocumentObject.Заказ</v8:Type>"),
            ("Универсальная", ""),
        ):
            d = es / name / "Ext"
            d.mkdir(parents=True)
            (d / "EventSubscription.xml").write_text(_cf_subscription(name, body), encoding="utf-8")
    (tmp_path / "Configuration.xml").write_text(CF_DESCRIPTOR, encoding="utf-8")
    return tmp_path


def _bsl_for(base_path, idx_reader=None):
    helpers, resolve_safe = make_helpers(str(base_path))
    return make_bsl_helpers(
        base_path=str(base_path),
        resolve_safe=resolve_safe,
        read_file_fn=helpers["read_file"],
        grep_fn=helpers["grep"],
        glob_files_fn=helpers["glob_files"],
        format_info=detect_format(str(base_path)),
        idx_reader=idx_reader,
    )


def _helpers_for(tmp_path, monkeypatch, *, with_index: bool):
    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / ".idx"))
    reader = None
    if with_index:
        db = IndexBuilder().build(str(tmp_path), build_calls=False, build_metadata=True)
        reader = IndexReader(db)
    return _bsl_for(tmp_path, reader), reader


@pytest.mark.parametrize("edt", [False, True], ids=["cf", "edt"])
@pytest.mark.parametrize("with_index", [False, True], ids=["live", "index"])
def test_set_subscriptions_are_found_on_both_branches(tmp_path, monkeypatch, edt, with_index):
    """Строка-набор перестаёт исчезать (EDT) и перестаёт быть ложно universal (CF).

    Индексная и живая ветки обязаны дать ОДИН И ТОТ ЖЕ набор строк: иначе одна
    конфигурация отвечает по-разному до и после сборки индекса.
    """
    _build_set_fixture(tmp_path, edt=edt)
    bsl, reader = _helpers_for(tmp_path, monkeypatch, with_index=with_index)
    try:
        rows = bsl["find_event_subscriptions"]("Документ.Заказ")
        by_name = {r["name"]: r for r in rows}
        assert by_name["НаборДокументов"]["scope"] == "set"
        assert by_name["НаборДокументов"]["matched_via"] == "set"
        assert by_name["НаборДокументов"]["matched_sets"] == ["DocumentObject"]
        assert by_name["Точная"]["scope"] == "exact"
        assert by_name["Универсальная"]["scope"] == "universal"
        # Набор — доказанное типовое совпадение, и наличие exact его НЕ подавляет.
        assert "НаборДокументов" in by_name
        # Набор не своей категории не притягивается.
        assert "НаборКонстант" not in by_name
        # Токен вне карты назван, а не проглочен.
        assert by_name["НеРаскрываемый"]["matched_via"] == "set_unresolved"
        assert by_name["НеРаскрываемый"]["matched_sets"] == ["SequenceRecordSet"]
    finally:
        if reader:
            reader.close()


@pytest.mark.parametrize("edt", [False, True], ids=["cf", "edt"])
def test_constant_set_resolves_although_constants_have_no_bsl_modules(tmp_path, monkeypatch, edt):
    """Гард против резолвера категорий по ``_index_state``.

    Тот содержит только объекты с BSL-модулями, и ни одна константа в него не
    входит — наборы ``ConstantValueManager`` не раскрылись бы никогда.
    """
    _build_set_fixture(tmp_path, edt=edt)
    bsl, reader = _helpers_for(tmp_path, monkeypatch, with_index=True)
    try:
        rows = bsl["find_event_subscriptions"]("ВалютаУчета")
        by_name = {r["name"]: r for r in rows}
        assert by_name["НаборКонстант"]["scope"] == "set"
        assert by_name["НаборКонстант"]["matched_sets"] == ["ConstantValueManager"]
        assert "НаборДокументов" not in by_name
    finally:
        if reader:
            reader.close()


def test_set_subscriptions_survive_rebuild_and_reach_the_column(tmp_path, monkeypatch):
    _build_set_fixture(tmp_path, edt=False)
    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / ".idx"))
    db = IndexBuilder().build(str(tmp_path), build_calls=False, build_metadata=True)
    con = sqlite3.connect(db)
    try:
        rows = dict(con.execute("SELECT name, source_type_sets FROM event_subscriptions").fetchall())
        assert json.loads(rows["НаборДокументов"]) == ["DocumentObject"]
        assert json.loads(rows["Точная"]) == []
        version = con.execute("SELECT value FROM index_meta WHERE key='builder_version'").fetchone()[0]
        assert int(version) == BUILDER_VERSION == 16
    finally:
        con.close()


def test_profile_summary_counts_set_rows(tmp_path, monkeypatch):
    """Сводка профиля обязана сходиться с числом строк, которые сама вернула."""
    _build_set_fixture(tmp_path, edt=False)
    bsl, reader = _helpers_for(tmp_path, monkeypatch, with_index=True)
    try:
        subs = bsl["get_object_profile"]("Документ.Заказ")["sections"]["subscriptions"]
        summary = subs["summary"]
        assert summary["subscriptions"] == summary["exact"] + summary["set"] + summary["universal"]
        assert summary["set"] >= 1
    finally:
        if reader:
            reader.close()


def test_references_find_set_subscription_without_growing_the_table(tmp_path, monkeypatch):
    """Четвёртый потребитель: раскрытие НА ЧТЕНИИ, строк в таблице не прибавилось."""
    _build_set_fixture(tmp_path, edt=False)
    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / ".idx"))
    db = IndexBuilder().build(str(tmp_path), build_calls=False, build_metadata=True)
    con = sqlite3.connect(db)
    try:
        stored = con.execute(
            "SELECT COUNT(*) FROM metadata_references WHERE ref_kind='event_subscription_source'"
        ).fetchone()[0]
    finally:
        con.close()
    reader = IndexReader(db)
    bsl = _bsl_for(tmp_path, reader)
    try:
        res = bsl["find_references_to_object"]("Документ.Заказ", kinds=["event_subscription_source"])
        used = {r["used_in"] for r in res["references"]}
        assert "EventSubscription.Точная.Source" in used
        assert "EventSubscription.НаборДокументов.SourceTypeSet" in used
        assert res["total"] == len(res["references"])
        assert res["by_kind"]["event_subscription_source"] == res["total"]
        # Таблица ссылок по наборам строк НЕ получила: 608 членов одного
        # определяемого типа дали бы 608 строк на одну подписку.
        assert stored == 1
    finally:
        reader.close()


# ───────── Задача 7: объявленный состав движений; Задача 12: провенанс ─────────


_MANAGER_WITH_INDEXER = """\
Процедура ЗаписатьДвижения() Экспорт
    Набор = РегистрыНакопления[МетаданныеРегистра.Имя].СоздатьНаборЗаписей();
    Набор.Записать();
    Статик = РегистрыСведений["Цены"].СоздатьНаборЗаписей();
    Статик.Записать();
    Нет = РегистрыНакопления.НетТакогоРегистра.СоздатьНаборЗаписей();
    // РегистрыСведений[Призрак].СоздатьНаборЗаписей();
    Менеджер = РегистрыНакопления[ИмяРегистра];
    Мгр = РегистрыСведений[X].СоздатьМенеджерЗаписи();
КонецПроцедуры
"""


def _cf_document_xml(name: str, declared: list[str], templates: tuple[str, ...] = ()) -> str:
    items = "".join(f"<xr:Item>{r}</xr:Item>" for r in declared)
    child = "".join(f"<Template><Properties><Name>{t}</Name></Properties></Template>" for t in templates)
    return (
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses" '
        'xmlns:v8="http://v8.1c.ru/8.1/data/core" xmlns:xr="http://v8.1c.ru/8.3/xcf/readable">'
        f"<Document><Properties><Name>{name}</Name>"
        f"<RegisterRecords>{items}</RegisterRecords>"
        f"</Properties><ChildObjects>{child}</ChildObjects></Document></MetaDataObject>"
    )


def _edt_document_mdo(name: str, declared: list[str]) -> str:
    items = "".join(f"<registerRecords>{r}</registerRecords>" for r in declared)
    return (
        '<mdclass:Document xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass">'
        f"<name>{name}</name>{items}</mdclass:Document>"
    )


def _declared_fixture(tmp_path, *, edt: bool, declared: list[str], many: int = 0):
    if many:
        declared = [f"AccumulationRegister.Р{i}" for i in range(many)]
    folders = {
        "AccumulationRegister": "AccumulationRegisters",
        "InformationRegister": "InformationRegisters",
    }
    for reg_ref in declared:
        cat, short = reg_ref.split(".", 1)
        (tmp_path / folders[cat] / short).mkdir(parents=True, exist_ok=True)
    (tmp_path / "InformationRegisters" / "Цены").mkdir(parents=True, exist_ok=True)
    (tmp_path / "AccumulationRegisters" / "Продажи").mkdir(parents=True, exist_ok=True)
    doc = tmp_path / "Documents" / "Заказ"
    (doc / "Ext").mkdir(parents=True)
    (doc / "Ext" / "ObjectModule.bsl").write_text(
        "Процедура ОбработкаПроведения(Отказ, Режим)\n    Движения.Продажи.Записать();\nКонецПроцедуры\n",
        encoding="utf-8",
    )
    (doc / "Ext" / "ManagerModule.bsl").write_text(_MANAGER_WITH_INDEXER, encoding="utf-8")
    if edt:
        (doc / "Заказ.mdo").write_text(_edt_document_mdo("Заказ", declared), encoding="utf-8")
    else:
        (tmp_path / "Documents" / "Заказ.xml").write_text(_cf_document_xml("Заказ", declared), encoding="utf-8")
    (tmp_path / "Configuration.xml").write_text(CF_DESCRIPTOR, encoding="utf-8")
    return declared


@pytest.mark.parametrize("edt", [False, True], ids=["cf", "edt"])
def test_declared_registers_are_published_on_both_formats(tmp_path, monkeypatch, edt):
    """CF и EDT дают ОДИН И ТОТ ЖЕ объявленный состав."""
    declared = _declared_fixture(
        tmp_path, edt=edt, declared=["AccumulationRegister.Продажи", "InformationRegister.Цены"]
    )
    bsl, reader = _helpers_for(tmp_path, monkeypatch, with_index=True)
    try:
        res = bsl["find_register_movements"]("Заказ")
        assert sorted(res["declared_registers"]) == sorted(declared)
        assert res["declared_total"] == len(declared)
        assert res["declared_truncated"] is False
    finally:
        if reader:
            reader.close()


def test_document_without_declared_composition_gets_zero_not_a_missing_key(tmp_path, monkeypatch):
    _declared_fixture(tmp_path, edt=False, declared=[])
    bsl, reader = _helpers_for(tmp_path, monkeypatch, with_index=True)
    try:
        res = bsl["find_register_movements"]("Заказ")
        assert res["declared_registers"] == []
        assert res["declared_total"] == 0
        assert res["declared_truncated"] is False
    finally:
        if reader:
            reader.close()


def test_declared_registers_page_is_bounded_and_reports_the_full_total(tmp_path, monkeypatch):
    """258 ссылок × ~45 символов выели бы max_output_chars целиком одним ключом."""
    from rlm_tools_bsl.bsl_helpers import _DECLARED_REGISTERS_PAGE

    _declared_fixture(tmp_path, edt=False, declared=[], many=300)
    bsl, reader = _helpers_for(tmp_path, monkeypatch, with_index=True)
    try:
        res = bsl["find_register_movements"]("Заказ")
        assert res["declared_total"] == 300
        assert len(res["declared_registers"]) == _DECLARED_REGISTERS_PAGE
        assert res["declared_truncated"] is True
    finally:
        if reader:
            reader.close()


def test_code_registers_not_declared_are_named_separately(tmp_path, monkeypatch):
    """Диагностика, которую поле открывает бесплатно."""
    _declared_fixture(tmp_path, edt=False, declared=["InformationRegister.Цены"])
    bsl, reader = _helpers_for(tmp_path, monkeypatch, with_index=True)
    try:
        res = bsl["find_register_movements"]("Заказ")
        assert "Продажи" in res["undeclared_code_registers"]
        assert "Цены" not in res["undeclared_code_registers"]
    finally:
        if reader:
            reader.close()


def test_analyze_document_flow_inherits_declared_but_profile_does_not(tmp_path, monkeypatch):
    _declared_fixture(tmp_path, edt=False, declared=["InformationRegister.Цены"])
    bsl, reader = _helpers_for(tmp_path, monkeypatch, with_index=True)
    try:
        flow = bsl["analyze_document_flow"]("Заказ")
        assert "declared_registers" in flow["register_movements"]
        prof = json.dumps(bsl["get_object_profile"]("Заказ"), ensure_ascii=False, default=str)
        assert '"declared_registers"' not in prof, "профиль форму НЕ отращивает (v1.37.0 её заморозил)"
    finally:
        if reader:
            reader.close()


def test_dynamic_indexer_becomes_unresolved_not_silence(tmp_path, monkeypatch):
    _declared_fixture(tmp_path, edt=False, declared=["InformationRegister.Цены"])
    bsl, reader = _helpers_for(tmp_path, monkeypatch, with_index=True)
    try:
        res = bsl["find_register_movements"]("Заказ")
        assert res["_meta"]["unresolved_available"] is True
        kinds = {u["kind"] for u in res["unresolved"]}
        assert "dynamic_name" in kinds, res["unresolved"]
        dyn = [u for u in res["unresolved"] if u["kind"] == "dynamic_name"][0]
        assert "СоздатьНаборЗаписей" in dyn["evidence"]
        # Закомментированная конструкция строки НЕ даёт.
        assert not any("Призрак" in u["evidence"] for u in res["unresolved"])
        # Охват НЕ расширен: СоздатьМенеджерЗаписи и обращение без метода.
        assert not any("СоздатьМенеджерЗаписи" in u["evidence"] for u in res["unresolved"])
    finally:
        if reader:
            reader.close()


def test_static_indexer_literal_becomes_a_normal_manager_code_row(tmp_path, monkeypatch):
    """Гард против чтения имени из ПОЛНОЙ маски: там литерал погашен."""
    _declared_fixture(tmp_path, edt=False, declared=["InformationRegister.Цены"])
    bsl, reader = _helpers_for(tmp_path, monkeypatch, with_index=True)
    try:
        res = bsl["find_register_movements"]("Заказ")
        names = {r["name"] for r in res["code_registers"]}
        assert "Цены" in names, res["code_registers"]
        assert not any(u["kind"] == "dynamic_name" and "Цены" in u["evidence"] for u in res["unresolved"])
    finally:
        if reader:
            reader.close()


def test_literal_outside_the_register_catalog_is_named_not_dropped(tmp_path, monkeypatch):
    _declared_fixture(tmp_path, edt=False, declared=["InformationRegister.Цены"])
    bsl, reader = _helpers_for(tmp_path, monkeypatch, with_index=True)
    try:
        res = bsl["find_register_movements"]("Заказ")
        unknown = [u for u in res["unresolved"] if u["kind"] == "unknown_register"]
        assert unknown, res["unresolved"]
        assert any("НетТакогоРегистра" in u["evidence"] for u in unknown)
        # Движением НЕ становится и в register_name не попадает.
        assert "НетТакогоРегистра" not in {r["name"] for r in res["code_registers"]}
        writers = bsl["find_register_writers"]("НетТакогоРегистра")
        assert writers["writers"] == [], writers
    finally:
        if reader:
            reader.close()


def test_unresolved_rows_do_not_inflate_the_profile_total(tmp_path, monkeypatch):
    _declared_fixture(tmp_path, edt=False, declared=["InformationRegister.Цены"])
    bsl, reader = _helpers_for(tmp_path, monkeypatch, with_index=True)
    try:
        section = bsl["get_object_profile"]("Заказ")["sections"]["registers"]
        sources = {i["source"] for i in section.get("items", [])}
        assert "unresolved" not in sources, section
    finally:
        if reader:
            reader.close()


def test_postability_hint_is_silenced_by_a_non_empty_unresolved(tmp_path, monkeypatch):
    """Подсказка обязана ЗНАТЬ про непустой `unresolved`.

    Голое «движений регистров нет» рядом с ключом, который перечисляет создание
    наборов записей, читается как опровержение собственного ответа."""
    doc = tmp_path / "Documents" / "Заказ"
    (doc / "Ext").mkdir(parents=True)
    (doc / "Ext" / "ObjectModule.bsl").write_text(
        "Процедура ОбработкаПроведения(Отказ, Режим)\nКонецПроцедуры\n", encoding="utf-8"
    )
    (doc / "Ext" / "ManagerModule.bsl").write_text(
        "Процедура З() Экспорт\n    Н = РегистрыНакопления[Имя].СоздатьНаборЗаписей();\nКонецПроцедуры\n",
        encoding="utf-8",
    )
    (tmp_path / "Documents" / "Заказ.xml").write_text(
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses" '
        'xmlns:v8="http://v8.1c.ru/8.1/data/core"><Document><Properties>'
        "<Name>Заказ</Name><Posting>Deny</Posting></Properties></Document></MetaDataObject>",
        encoding="utf-8",
    )
    (tmp_path / "Configuration.xml").write_text(CF_DESCRIPTOR, encoding="utf-8")
    bsl, reader = _helpers_for(tmp_path, monkeypatch, with_index=True)
    try:
        res = bsl["find_register_movements"]("Заказ")
        assert res["unresolved"], res
        # `is_postable=False` ОСТАЁТСЯ: Posting=Deny — факт метаданных, и
        # скрывать его из-за неразрешённой строки было бы потерей контрактного ключа.
        assert res["is_postable"] is False, res
        # А вот ТЕКСТ подсказки обязан назвать неразрешённые обращения.
        assert "unresolved" in res["hint"], res["hint"]
    finally:
        if reader:
            reader.close()


def test_unresolved_channel_is_declared_unavailable_without_an_index(tmp_path, monkeypatch):
    """Пустой список без индекса неотличим от честного нуля — канал объявляется."""
    _declared_fixture(tmp_path, edt=False, declared=["InformationRegister.Цены"])
    bsl, reader = _helpers_for(tmp_path, monkeypatch, with_index=False)
    try:
        res = bsl["find_register_movements"]("Заказ")
        assert res["unresolved"] == []
        assert res["_meta"]["unresolved_available"] is False
        assert res["_meta"]["unresolved_reason"]
        # Объявленный состав живая ветка всё равно отдаёт — паритет с индексом.
        assert res["declared_registers"] == ["InformationRegister.Цены"]
    finally:
        if reader:
            reader.close()


# ───────────────── Задача 4: названный отказ вместо молчаливого нуля ─────────────────


def _module_fixture(tmp_path):
    """Общий модуль + модуль формы документа + ОБЩАЯ форма."""
    cm = tmp_path / "CommonModules" / "МойОбщий" / "Ext"
    cm.mkdir(parents=True)
    (cm / "Module.bsl").write_text("Процедура О() Экспорт\nКонецПроцедуры\n", encoding="utf-8")
    (tmp_path / "CommonModules" / "МойОбщий.xml").write_text(
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses" '
        'xmlns:v8="http://v8.1c.ru/8.1/data/core"><CommonModule><Properties>'
        "<Name>МойОбщий</Name><Server>true</Server><Privileged>true</Privileged>"
        "<ReturnValuesReuse>DuringSession</ReturnValuesReuse>"
        "</Properties></CommonModule></MetaDataObject>",
        encoding="utf-8",
    )
    form = tmp_path / "Documents" / "Заказ" / "Forms" / "ФормаДокумента" / "Ext" / "Form"
    form.mkdir(parents=True)
    (form / "Module.bsl").write_text("Процедура Ф()\nКонецПроцедуры\n", encoding="utf-8")
    common_form = tmp_path / "CommonForms" / "ОбщаяФорма" / "Ext" / "Form"
    common_form.mkdir(parents=True)
    (common_form / "Module.bsl").write_text("Процедура ОФ()\nКонецПроцедуры\n", encoding="utf-8")
    ir = tmp_path / "InformationRegisters" / "Цены" / "Ext"
    ir.mkdir(parents=True)
    (ir / "RecordSetModule.bsl").write_text("Процедура Р()\nКонецПроцедуры\n", encoding="utf-8")
    (tmp_path / "Configuration.xml").write_text(CF_DESCRIPTOR, encoding="utf-8")
    return _bsl_for(tmp_path)


def test_category_valued_module_type_gets_an_executable_refusal(tmp_path):
    """У запроса «общие модули» ЕСТЬ верный маршрут — отказ на него и указывает."""
    bsl = _module_fixture(tmp_path)
    with pytest.raises(ValueError) as exc:
        bsl["find_module"](module_type="CommonModule")
    assert "category='CommonModules'" in str(exc.value)


def test_unknown_filter_value_lists_the_allowed_ones(tmp_path):
    bsl = _module_fixture(tmp_path)
    with pytest.raises(ValueError) as exc:
        bsl["find_module"](module_type="Nonsense")
    assert "ObjectModule" in str(exc.value)
    with pytest.raises(ValueError) as exc:
        bsl["find_module"](category="Nonsense")
    assert "CommonModules" in str(exc.value)
    with pytest.raises(ValueError) as exc:
        bsl["find_by_type"]("Nonsense")
    assert "CommonModules" in str(exc.value)


def test_russian_category_is_normalized_like_find_by_type(tmp_path):
    bsl = _module_fixture(tmp_path)
    rows = bsl["find_module"](category="Регистр сведений")
    assert [r["object_name"] for r in rows] == ["Цены"], rows


def test_common_modules_filter_never_returns_a_form_module(tmp_path):
    """ПРЯМОЙ гард против алиаса `CommonModule` → `Module`.

    `Module` — это module_type не только общего модуля, но и КАЖДОГО модуля формы
    (имя файла у них одно). На боевой конфигурации из 13 569 строк с этим типом
    девять тысяч — формы, а порядок выдачи — прямой проход каталога: алиас вернул
    бы 50 модулей форм и ни одного общего, то есть молчаливый ноль сменился бы
    уверенно НЕВЕРНЫМ ответом.
    """
    bsl = _module_fixture(tmp_path)
    rows = bsl["find_module"](category="CommonModules")
    assert [r["object_name"] for r in rows] == ["МойОбщий"], rows
    assert all("/Forms/" not in r["path"] for r in rows)


def test_valid_category_absent_from_the_tree_is_an_empty_list_not_an_error(tmp_path):
    """Гард против DISTINCT-словаря: словарь СТАТИЧЕСКИЙ."""
    bsl = _module_fixture(tmp_path)
    assert bsl["find_module"](category="BusinessProcesses") == []
    assert bsl["find_by_type"]("BusinessProcesses") == []
    # И безусловные внутренние вызовы find_by_type("Documents") не падают.
    assert "can_create_from_here" in bsl["find_based_on_documents"]("НетТакого")
    assert "print_forms" in bsl["find_print_forms"]("НетТакого")


def test_form_module_is_a_documented_filter_including_common_forms(tmp_path):
    """`FormModule` не существует как ЗНАЧЕНИЕ поля (ноль строк в боевом индексе),

    но его называли оба agent-facing дока. Обещание сделано работающим: это
    ФИЛЬТР «модуль с непустым ПУБЛИЧНЫМ form_name». Публичным — потому что у
    ОБЩЕЙ формы сегмента `Forms` в пути нет и сырой form_name пуст.
    """
    bsl = _module_fixture(tmp_path)
    rows = bsl["find_module"](module_type="FormModule")
    names = {r["object_name"] for r in rows}
    assert "Заказ" in names, rows
    assert "ОбщаяФорма" in names, "модуль ОБЩЕЙ формы обязан войти в выдачу"
    assert "МойОбщий" not in names, "общий модуль формой не является"


# ───────────────── Задача 8: права, выданные флагом setForNewObjects ─────────────────


def _rights_xml(*, flag: bool, entries: str = "") -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Rights xmlns="http://v8.1c.ru/8.2/roles">\n'
        f"<setForNewObjects>{'true' if flag else 'false'}</setForNewObjects>\n"
        "<setForAttributesByDefault>true</setForAttributesByDefault>\n"
        f"{entries}</Rights>\n"
    )


def _right(name: str, value: str) -> str:
    return f"<right><name>{name}</name><value>{value}</value></right>"


def _roles_fixture(tmp_path):
    _seed_one_module(tmp_path)
    doc = tmp_path / "Documents" / "Заказ" / "Ext"
    doc.mkdir(parents=True)
    (doc / "ObjectModule.bsl").write_text("Процедура П()\nКонецПроцедуры\n", encoding="utf-8")
    roles = tmp_path / "Roles"
    # Флагованная роль: явная запись объекта перечисляет ИСКЛЮЧЕНИЯ.
    full = roles / "ПолныеПрава" / "Ext"
    full.mkdir(parents=True)
    (full / "Rights.xml").write_text(
        _rights_xml(
            flag=True,
            entries=(
                f"<object><name>Document.Заказ</name>{_right('Delete', 'false')}</object>"
                f"<object><name>Document.Заказ.Attribute.Сумма</name>{_right('Edit', 'false')}</object>"
            ),
        ),
        encoding="utf-8",
    )
    # Роль с setForAttributesByDefault, но БЕЗ setForNewObjects — покрытия НЕТ.
    attrs = roles / "ТолькоРеквизиты" / "Ext"
    attrs.mkdir(parents=True)
    (attrs / "Rights.xml").write_text(_rights_xml(flag=False), encoding="utf-8")
    # Обычная роль с явно выданным правом.
    plain = roles / "Читатель" / "Ext"
    plain.mkdir(parents=True)
    (plain / "Rights.xml").write_text(
        _rights_xml(flag=False, entries=f"<object><name>Document.Заказ</name>{_right('Read', 'true')}</object>"),
        encoding="utf-8",
    )
    (tmp_path / "Configuration.xml").write_text(CF_DESCRIPTOR, encoding="utf-8")


@pytest.mark.parametrize("with_index", [False, True], ids=["live", "index"])
def test_flag_covered_role_is_visible_with_its_exclusions(tmp_path, monkeypatch, with_index):
    """Роль, раздающая права флагом, исчезала из ответа ЦЕЛИКОМ.

    `parse_rights_xml` возвращает только права со значением true, а у флагованной
    роли явная запись объекта перечисляет ИСКЛЮЧЕНИЯ — все права в ней false.
    """
    _roles_fixture(tmp_path)
    bsl, reader = _helpers_for(tmp_path, monkeypatch, with_index=with_index)
    try:
        res = bsl["find_roles"]("Заказ")
        by_name = {r["role_name"]: r for r in res["roles"]}
        assert res["flags_available"] is True
        assert "ПолныеПрава" in by_name, res
        flagged = by_name["ПолныеПрава"]
        assert flagged["granted_via"] == "set_for_new_objects"
        assert flagged["excluded_rights"] == ["Delete"], flagged
        # Исключение уровня РЕКВИЗИТА покрытие объекта НЕ снимает и в список не идёт.
        assert "Edit" not in flagged["excluded_rights"]
        # Гард против ошибочного прочтения бэклога: setForAttributesByDefault — норма.
        assert "ТолькоРеквизиты" not in by_name, by_name
        assert by_name["Читатель"]["granted_via"] == "explicit"
        assert "ВЕРХНЯЯ оценка" in res["hint"]
    finally:
        if reader:
            reader.close()


def test_flag_coverage_is_consistent_across_all_three_routes(tmp_path, monkeypatch):
    """find_roles, секция профиля и find_references_to_object считают ОДНО."""
    _roles_fixture(tmp_path)
    bsl, reader = _helpers_for(tmp_path, monkeypatch, with_index=True)
    try:
        roles = {r["role_name"] for r in bsl["find_roles"]("Заказ")["roles"]}
        section = bsl["get_object_profile"]("Документ.Заказ")["sections"]["roles"]
        profile_roles = {i["role_name"] for i in section["items"]}
        refs = bsl["find_references_to_object"]("Документ.Заказ", kinds=["role_rights"])
        ref_roles = {r["used_in"].split(".")[1] for r in refs["references"]}
        assert "ПолныеПрава" in roles
        assert "ПолныеПрава" in profile_roles, section
        assert "ПолныеПрава" in ref_roles, refs
        assert section["summary"]["flag_covered"] >= 1
        assert refs["total"] == len(refs["references"])
    finally:
        reader.close()


def test_flag_channel_declares_itself_unavailable_without_an_index(tmp_path, monkeypatch):
    _roles_fixture(tmp_path)
    bsl, reader = _helpers_for(tmp_path, monkeypatch, with_index=False)
    try:
        # Живая ветка флаг ЧИТАЕТ (тем же проходом), поэтому канал доступен.
        assert bsl["find_roles"]("Заказ")["flags_available"] is True
    finally:
        if reader:
            reader.close()


# ───────────────── Задача 11: общие модули, константы, макеты ─────────────────


def _seed_one_module(tmp_path):
    """Хотя бы один .bsl: на дереве без единого модуля сборка идёт путём пустого
    репозитория и метаданные не собирает вовсе — фикстура была бы вакуумной."""
    d = tmp_path / "CommonModules" / "Якорь" / "Ext"
    d.mkdir(parents=True)
    (d / "Module.bsl").write_text("Процедура Я() Экспорт\nКонецПроцедуры\n", encoding="utf-8")


def _props_fixture(tmp_path, *, edt: bool):
    _seed_one_module(tmp_path)
    if edt:
        d = tmp_path / "CommonModules" / "МойОбщий"
        d.mkdir(parents=True)
        (d / "МойОбщий.mdo").write_text(
            '<mdclass:CommonModule xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass">'
            "<name>МойОбщий</name><server>true</server><privileged>true</privileged>"
            "<returnValuesReuse>DuringSession</returnValuesReuse></mdclass:CommonModule>",
            encoding="utf-8",
        )
        c = tmp_path / "Constants" / "Флаг"
        c.mkdir(parents=True)
        (c / "Флаг.mdo").write_text(
            '<mdclass:Constant xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass">'
            "<name>Флаг</name><type><types>Boolean</types></type></mdclass:Constant>",
            encoding="utf-8",
        )
    else:
        (tmp_path / "CommonModules").mkdir(parents=True, exist_ok=True)
        (tmp_path / "CommonModules" / "МойОбщий.xml").write_text(
            '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses" '
            'xmlns:v8="http://v8.1c.ru/8.1/data/core"><CommonModule><Properties>'
            "<Name>МойОбщий</Name><Global>false</Global><Server>true</Server>"
            "<ServerCall>false</ServerCall><Privileged>true</Privileged>"
            "<ExternalConnection>false</ExternalConnection>"
            "<ClientManagedApplication>false</ClientManagedApplication>"
            "<ClientOrdinaryApplication>false</ClientOrdinaryApplication>"
            "<ReturnValuesReuse>DuringSession</ReturnValuesReuse>"
            "</Properties></CommonModule></MetaDataObject>",
            encoding="utf-8",
        )
        (tmp_path / "Constants").mkdir(parents=True, exist_ok=True)
        (tmp_path / "Constants" / "Флаг.xml").write_text(
            '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses" '
            'xmlns:v8="http://v8.1c.ru/8.1/data/core"><Constant><Properties>'
            "<Name>Флаг</Name><Type><v8:Type>xs:boolean</v8:Type></Type>"
            "</Properties></Constant></MetaDataObject>",
            encoding="utf-8",
        )
    (tmp_path / "Configuration.xml").write_text(CF_DESCRIPTOR, encoding="utf-8")


@pytest.mark.parametrize("edt", [False, True], ids=["cf", "edt"])
def test_common_module_flags_are_identical_on_both_formats(tmp_path, monkeypatch, edt):
    """ПРЯМОЙ гард против прочтения «отсутствует = неизвестно».

    EDT опускает ложные флаги ЦЕЛИКОМ, CF выписывает их явно. Прочтение «нет узла
    = неизвестно» дало бы два РАЗНЫХ ответа на одну конфигурацию.
    """
    _props_fixture(tmp_path, edt=edt)
    bsl, reader = _helpers_for(tmp_path, monkeypatch, with_index=True)
    try:
        (row,) = bsl["find_common_modules"]("МойОбщий")["modules"]
        assert row["privileged"] is True and row["server"] is True
        assert row["global"] is False and row["server_call"] is False
        assert row["client_managed"] is False and row["client_ordinary"] is False
        assert row["return_values_reuse"] == "DuringSession"
    finally:
        reader.close()


def test_rare_flags_are_findable_one_by_one(tmp_path, monkeypatch):
    _props_fixture(tmp_path, edt=False)
    bsl, reader = _helpers_for(tmp_path, monkeypatch, with_index=True)
    try:
        assert [m["module_name"] for m in bsl["find_common_modules"](flag="privileged")["modules"]] == ["МойОбщий"]
        assert bsl["find_common_modules"](flag="global")["modules"] == []
        assert [m["module_name"] for m in bsl["find_common_modules"](flag="DuringSession")["modules"]] == ["МойОбщий"]
    finally:
        reader.close()


def test_find_module_rows_did_not_grow_a_props_key(tmp_path, monkeypatch):
    """Ключ `props` отвергнут: find_module/find_by_type работают и БЕЗ индекса."""
    _props_fixture(tmp_path, edt=False)
    cm = tmp_path / "CommonModules" / "МойОбщий" / "Ext"
    cm.mkdir(parents=True)
    (cm / "Module.bsl").write_text("Процедура О() Экспорт\nКонецПроцедуры\n", encoding="utf-8")
    bsl, reader = _helpers_for(tmp_path, monkeypatch, with_index=True)
    try:
        for row in bsl["find_module"]("МойОбщий") + bsl["find_by_type"]("CommonModules"):
            assert "props" not in row, row
    finally:
        reader.close()


def test_common_modules_without_index_are_partial_not_silent(tmp_path, monkeypatch):
    _props_fixture(tmp_path, edt=False)
    bsl, reader = _helpers_for(tmp_path, monkeypatch, with_index=False)
    try:
        res = bsl["find_common_modules"]("МойОбщий")
        assert res["source"] == "live" and res["partial"] is True
        assert [m["module_name"] for m in res["modules"]] == ["МойОбщий"]
    finally:
        if reader:
            reader.close()


@pytest.mark.parametrize("edt", [False, True], ids=["cf", "edt"])
def test_constant_value_type_keeps_the_reference_form(tmp_path, monkeypatch, edt):
    """Гард против канонизации: примитив НЕ становится пустой строкой."""
    _props_fixture(tmp_path, edt=edt)
    bsl, reader = _helpers_for(tmp_path, monkeypatch, with_index=True)
    try:
        res = bsl["get_object_full_structure"]("Константа.Флаг")
        assert json.loads(res["value_type"]) == ["Boolean"], res["value_type"]
    finally:
        reader.close()


def test_reference_typed_constant_matches_the_neighbouring_attribute_form(tmp_path, monkeypatch):
    """`CatalogRef.X`, как у соседнего реквизита, а НЕ канонический `Catalog.X`."""
    (tmp_path / "Constants").mkdir(parents=True, exist_ok=True)
    _seed_one_module(tmp_path)
    (tmp_path / "Constants" / "Валюта.xml").write_text(
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses" '
        'xmlns:v8="http://v8.1c.ru/8.1/data/core"><Constant><Properties>'
        "<Name>Валюта</Name><Type><v8:Type>cfg:CatalogRef.Валюты</v8:Type></Type>"
        "</Properties></Constant></MetaDataObject>",
        encoding="utf-8",
    )
    (tmp_path / "Configuration.xml").write_text(CF_DESCRIPTOR, encoding="utf-8")
    bsl, reader = _helpers_for(tmp_path, monkeypatch, with_index=True)
    try:
        res = bsl["get_object_full_structure"]("Константа.Валюта")
        assert json.loads(res["value_type"]) == ["CatalogRef.Валюты"], res["value_type"]
    finally:
        reader.close()


def _templates_fixture(tmp_path, *, edt: bool):
    _seed_one_module(tmp_path)
    if edt:
        d = tmp_path / "Documents" / "Заказ"
        d.mkdir(parents=True)
        (d / "Заказ.mdo").write_text(
            '<mdclass:Document xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass"><name>Заказ</name>'
            '<templates uuid="a"><name>Печать</name><templateType>TextDocument</templateType></templates>'
            '<templates uuid="b"><name>БезТипа</name></templates>'
            "</mdclass:Document>",
            encoding="utf-8",
        )
        c = tmp_path / "CommonTemplates" / "Общий"
        c.mkdir(parents=True)
        (c / "Общий.mdo").write_text(
            '<mdclass:CommonTemplate xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass">'
            "<name>Общий</name><templateType>DataCompositionAppearanceTemplate</templateType>"
            "</mdclass:CommonTemplate>",
            encoding="utf-8",
        )
    else:
        (tmp_path / "Documents").mkdir(parents=True, exist_ok=True)
        (tmp_path / "Documents" / "Заказ.xml").write_text(
            _cf_document_xml("Заказ", [], templates=("Печать", "БезТипа")), encoding="utf-8"
        )
        t = tmp_path / "Documents" / "Заказ" / "Templates"
        t.mkdir(parents=True)
        (t / "Печать.xml").write_text(
            '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses" '
            'xmlns:v8="http://v8.1c.ru/8.1/data/core"><Template><Properties>'
            "<Name>Печать</Name><TemplateType>TextDocument</TemplateType>"
            "</Properties></Template></MetaDataObject>",
            encoding="utf-8",
        )
        (t / "БезТипа.xml").write_text(
            '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses" '
            'xmlns:v8="http://v8.1c.ru/8.1/data/core"><Template><Properties>'
            "<Name>БезТипа</Name></Properties></Template></MetaDataObject>",
            encoding="utf-8",
        )
        (tmp_path / "CommonTemplates").mkdir(parents=True, exist_ok=True)
        (tmp_path / "CommonTemplates" / "Общий.xml").write_text(
            '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses" '
            'xmlns:v8="http://v8.1c.ru/8.1/data/core"><CommonTemplate><Properties>'
            "<Name>Общий</Name><TemplateType>DataCompositionAppearanceTemplate</TemplateType>"
            "</Properties></CommonTemplate></MetaDataObject>",
            encoding="utf-8",
        )
    (tmp_path / "Configuration.xml").write_text(CF_DESCRIPTOR, encoding="utf-8")


@pytest.mark.parametrize("edt", [False, True], ids=["cf", "edt"])
def test_templates_cover_all_three_layouts(tmp_path, monkeypatch, edt):
    """ТРИ раскладки, а не две: общий макет EDT — корень без вложенного блока."""
    _templates_fixture(tmp_path, edt=edt)
    bsl, reader = _helpers_for(tmp_path, monkeypatch, with_index=True)
    try:
        rows = bsl["find_templates"]()["templates"]
        by_name = {r["name"]: r for r in rows}
        assert by_name["Печать"]["template_type"] == "TextDocument"
        assert by_name["Печать"]["owner_ref"] == "Document.Заказ"
        # Отсутствие узла типа означает SpreadsheetDocument, а не «неизвестно».
        assert by_name["БезТипа"]["template_type"] == "SpreadsheetDocument"
        # Общий макет: владельца нет, неизвестное значение типа хранится КАК ЕСТЬ.
        assert by_name["Общий"]["owner_ref"] == ""
        assert by_name["Общий"]["template_type"] == "DataCompositionAppearanceTemplate"
    finally:
        reader.close()


def test_object_structure_carries_the_template_composition(tmp_path, monkeypatch):
    _templates_fixture(tmp_path, edt=False)
    bsl, reader = _helpers_for(tmp_path, monkeypatch, with_index=True)
    try:
        res = bsl["get_object_full_structure"]("Документ.Заказ")
        names = {t["name"] for t in res["templates"]}
        assert names == {"Печать", "БезТипа"}, res["templates"]
        assert set(res["templates"][0]) == {"name", "synonym", "template_type"}
    finally:
        reader.close()


@pytest.mark.parametrize("edt", [False, True], ids=["cf", "edt"])
def test_parse_object_xml_returns_non_empty_template_names(tmp_path, edt):
    """Гард против нынешнего `['', '']`: EDT-ключ существовал и ВРАЛ."""
    _templates_fixture(tmp_path, edt=edt)
    bsl = _bsl_for(tmp_path)
    meta = bsl["parse_object_xml"]("Documents/Заказ")
    names = [t["name"] for t in meta["templates"]]
    assert sorted(names) == ["БезТипа", "Печать"], meta["templates"]
    if edt:
        assert meta["templates"][0]["template_type"] is not None
    else:
        # На CF живой ответ описатели НЕ открывает — это цена дешёвого агрегата.
        assert all(t["template_type"] is None and t["synonym"] is None for t in meta["templates"])


def test_find_templates_without_index_is_an_explicit_refusal(tmp_path, monkeypatch):
    _templates_fixture(tmp_path, edt=False)
    bsl, reader = _helpers_for(tmp_path, monkeypatch, with_index=False)
    try:
        res = bsl["find_templates"](name="Печать")
        assert res["templates"] == []
        assert res["source"] == "unavailable"
        assert res["partial"] is True
        assert res["_meta"]["reason"] == "index_required"
        assert "индекс" in res["hint"].lower()
    finally:
        if reader:
            reader.close()


# ───────────────── Задача 3: count_matches ─────────────────


def _count_fixture(tmp_path):
    for i, body in enumerate(
        [
            "#Если ВебКлиент Тогда\n    А = 1;\n#КонецЕсли\n#Если ВебКлиент Тогда\n#КонецЕсли\n",
            "Процедура П()\nКонецПроцедуры\n",
            "#Если ВебКлиент Тогда\nКонецЕсли\n",
        ]
    ):
        d = tmp_path / "CommonModules" / f"М{i}" / "Ext"
        d.mkdir(parents=True)
        (d / "Module.bsl").write_text(body, encoding="utf-8")
    d = tmp_path / "Documents" / "Заказ" / "Ext"
    d.mkdir(parents=True)
    (d / "ObjectModule.bsl").write_text("#Если ВебКлиент Тогда\nКонецЕсли\n", encoding="utf-8")
    (tmp_path / "Configuration.xml").write_text(CF_DESCRIPTOR, encoding="utf-8")


def test_count_matches_aggregate_agrees_with_line_by_line_grep(tmp_path, monkeypatch):
    """Числа, а не строки — но те же числа, что даёт построчный проход."""
    _count_fixture(tmp_path)
    bsl, reader = _helpers_for(tmp_path, monkeypatch, with_index=True)
    try:
        res = bsl["count_matches"]("#Если ВебКлиент")
        assert res["files_matched"] == 3, res
        assert res["occurrences"] == 4, res
        assert res["truncated"] is False
        assert res["failed_files"] == 0
        assert {g["key"] for g in res["groups"]} == {"CommonModules", "Documents"}
        top = {t["file"]: t["count"] for t in res["top"]}
        assert top["CommonModules/М0/Ext/Module.bsl"] == 2
    finally:
        reader.close()


def test_count_matches_serves_a_directory_where_grep_refuses(tmp_path, monkeypatch):
    """Отказ grep статический; count_matches обслуживает тот же вопрос."""
    _count_fixture(tmp_path)
    bsl, reader = _helpers_for(tmp_path, monkeypatch, with_index=True)
    try:
        res = bsl["count_matches"]("#Если ВебКлиент", path="CommonModules")
        assert res["files_matched"] == 2, res
        assert all(g["key"] == "CommonModules" for g in res["groups"])
    finally:
        reader.close()


def test_count_matches_rejects_an_absolute_path_by_name(tmp_path, monkeypatch):
    _count_fixture(tmp_path)
    bsl, reader = _helpers_for(tmp_path, monkeypatch, with_index=True)
    try:
        for bad in ("/CommonModules", "C:\\\\x", "../up", "Common*"):
            with pytest.raises(ValueError):
                bsl["count_matches"]("X", path=bad)
    finally:
        reader.close()


def test_grep_refusal_names_the_threshold_and_the_new_route(tmp_path):
    """Текст отказа обязан вести в исполнимый маршрут, а не в тупик."""
    from rlm_tools_bsl.helpers import make_helpers

    big = tmp_path / "big"
    big.mkdir()
    for i in range(5002):
        (big / f"f{i}.bsl").write_text("x", encoding="utf-8")
    helpers, _resolve = make_helpers(str(tmp_path))
    with pytest.raises(ValueError) as exc:
        helpers["grep"]("x", "big")
    text = str(exc.value)
    assert "5000" in text, text
    assert "count_matches" in text, text


# ───────────────── Регрессии ревью v1.38.0 ─────────────────
#
# Шесть находок внешнего ревью staged-диффа. Каждая ниже закрыта тестом,
# воспроизводящим ИМЕННО тот сценарий, которым она была доказана.


def _defined_type_fixture(tmp_path):
    """Документ + определяемый тип + подписка НА ЭТОТ ТИП (точечный токен)."""
    doc = tmp_path / "Documents" / "Заказ" / "Ext"
    doc.mkdir(parents=True)
    (doc / "ObjectModule.bsl").write_text("Процедура П() Экспорт\nКонецПроцедуры\n", encoding="utf-8")
    (tmp_path / "Documents" / "Заказ.xml").write_text(
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses" '
        'xmlns:v8="http://v8.1c.ru/8.1/data/core"><Document><Properties>'
        "<Name>Заказ</Name></Properties></Document></MetaDataObject>",
        encoding="utf-8",
    )
    dt = tmp_path / "DefinedTypes"
    dt.mkdir()
    (dt / "СсылкаНаДокумент.xml").write_text(
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses" '
        'xmlns:v8="http://v8.1c.ru/8.1/data/core"><DefinedType><Properties>'
        "<Name>СсылкаНаДокумент</Name><Type><v8:Type>cfg:DocumentRef.Заказ</v8:Type></Type>"
        "</Properties></DefinedType></MetaDataObject>",
        encoding="utf-8",
    )
    es = tmp_path / "EventSubscriptions" / "ПоОпредТипу" / "Ext"
    es.mkdir(parents=True)
    (es / "EventSubscription.xml").write_text(
        _cf_subscription("ПоОпредТипу", "<v8:TypeSet>cfg:DefinedType.СсылкаНаДокумент</v8:TypeSet>"),
        encoding="utf-8",
    )
    (tmp_path / "Configuration.xml").write_text(CF_DESCRIPTOR, encoding="utf-8")


def test_defined_type_set_does_not_deadlock_the_reader(tmp_path, monkeypatch):
    """Раскрытие ``DefinedType.X`` НЕ вешает ридер насмерть.

    ``get_event_subscriptions`` классифицирует строки ИЗ-ПОД ``self._lock``, а тот —
    плоский ``threading.Lock``, не ``RLock``. Пока ``_defined_type_members`` звал
    ПУБЛИЧНЫЙ ``find_defined_type``, тот брал ТОТ ЖЕ lock второй раз, и поток вис
    НАВСЕГДА. Достижимо любым адресным вопросом при наличии хотя бы одной подписки
    на определяемый тип: на боевых это 90 и 106 источников.

    Вызов идёт в отдельном ДЕМОН-потоке с join-таймаутом: на регрессе тест обязан
    УПАСТЬ, а не подвесить всю сюиту.

    **Закрывать ридер, пока поток жив, НЕЛЬЗЯ.** ``IndexReader.close()`` сам берёт
    тот же ``self._lock``, который удерживает зависший поток, поэтому наивный
    ``finally: reader.close()`` превратил бы падение теста обратно в вечное
    зависание — ровно то, от чего таймаут и защищает. Subprocess с принудительным
    завершением здесь тоже не годится: у проекта записан отказ
    ``subprocess.run(capture_output, timeout)`` на Windows, который после таймаута
    висит НАВСЕГДА. Поэтому ридер закрывается ТОЛЬКО если поток отработал; на
    регрессе соединение остаётся открытым в демон-потоке, и его снимет выход
    интерпретатора.
    """
    import threading

    _defined_type_fixture(tmp_path)
    bsl, reader = _helpers_for(tmp_path, monkeypatch, with_index=True)
    worker = None
    try:
        box: dict = {}

        def _call():
            box["rows"] = bsl["find_event_subscriptions"]("Документ.Заказ")

        worker = threading.Thread(target=_call, daemon=True)
        worker.start()
        worker.join(timeout=30)
        assert not worker.is_alive(), "ридер завис: повторный захват собственного lock"
        by_name = {r["name"]: r for r in box["rows"]}
        assert by_name["ПоОпредТипу"]["scope"] == "set"
        assert by_name["ПоОпредТипу"]["matched_via"] == "set"
        assert by_name["ПоОпредТипу"]["matched_sets"] == ["DefinedType.СсылкаНаДокумент"]
        # Публичный маршрут обязан продолжать брать lock — иначе вызов ИЗВНЕ
        # ридера остался бы без синхронизации.
        assert reader.find_defined_type("СсылкаНаДокумент")["types"] == ["Document.Заказ"]
    finally:
        if worker is None or not worker.is_alive():
            reader.close()


def test_selective_collection_keeps_based_on_for_the_extra_categories(tmp_path):
    """Инкрементальный ``update`` не имеет права СНОСИТЬ ``based_on`` новых категорий.

    ``_insert_metadata_tables_selective`` БЕЗУСЛОВНО делает
    ``DELETE FROM metadata_references WHERE source_category IN (triggered)``. Пока
    цикл ``based_on`` гейтился ещё и по ``collect_attrs_categories``, сбор для
    ``BusinessProcesses`` / ``Tasks`` / ``ChartsOfCalculationTypes`` /
    ``ExchangePlans`` пропускался ВСЕГДА (ни одной из них нет ни в
    ``_ATTR_CATEGORIES``, ни в ``_PREDEFINED_CATEGORIES``), то есть правка объекта
    удаляла его строки и не восстанавливала до полной пересборки.
    """
    from rlm_tools_bsl.bsl_index import _collect_metadata_tables

    doc = tmp_path / "Documents" / "Заказ" / "Ext"
    doc.mkdir(parents=True)
    (doc / "ObjectModule.bsl").write_text("Процедура П() Экспорт\nКонецПроцедуры\n", encoding="utf-8")
    bp = tmp_path / "BusinessProcesses" / "Задание"
    bp.mkdir(parents=True)
    (bp / "Задание.mdo").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<mdclass:BusinessProcess xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass">\n'
        "<name>Задание</name><basedOn>Document.Заказ</basedOn>\n"
        "</mdclass:BusinessProcess>\n",
        encoding="utf-8",
    )
    (tmp_path / "Configuration.xml").write_text(CF_DESCRIPTOR, encoding="utf-8")

    def _bp_rows(tables):
        return [r for r in tables["metadata_references"] if r[1] == "BusinessProcesses" and r[3] == "based_on"]

    full = _bp_rows(_collect_metadata_tables(str(tmp_path)))
    assert len(full) == 1, full
    # Ровно те аргументы, которые считает selective-ветка при правке бизнес-процесса:
    # attrs_cats = changed ∩ (_ATTR_CATEGORIES | _PREDEFINED_CATEGORIES) — БЕЗ него.
    selective = _bp_rows(
        _collect_metadata_tables(
            str(tmp_path),
            collect_attrs_categories={"Documents"},
            collect_metadata_refs_categories={"BusinessProcesses"},
        )
    )
    assert selective == full, "selective-сбор потерял based_on, а DELETE его уже снёс"
    # Гейт ОДИН и он работает: категория вне ref_cats не собирается.
    assert not _bp_rows(
        _collect_metadata_tables(
            str(tmp_path),
            collect_attrs_categories={"Documents"},
            collect_metadata_refs_categories={"Documents"},
        )
    )


def test_count_matches_counts_neighbouring_extensions_like_safe_grep(tmp_path):
    """Охват ``count_matches`` обязан совпадать с зеркалируемым ``safe_grep``.

    ``glob_files_fn`` — generic-резолвер песочницы, и он ТЕКУЩЕГО корня: файлы
    соседних CFE ему невидимы ПО КОНТРАКТУ. Фолбэк на CFE-aware каталог срабатывал
    только при ПОЛНОСТЬЮ пустом перечислении, поэтому в обычной main-конфигурации
    модули расширений не попадали в счёт НИКОГДА, а ответ молчаливо оказывался
    current-root-only при обещанном охвате «как у safe_grep».
    """
    marker = "#Если ВебКлиент Тогда"
    cf = tmp_path / "cf"
    cfe = tmp_path / "cfe" / "Расш"
    main_mod = cf / "CommonModules" / "ГлавныйМодуль" / "Ext"
    main_mod.mkdir(parents=True)
    (main_mod / "Module.bsl").write_text(f"Процедура П()\n{marker}\nКонецЕсли;\nКонецПроцедуры\n", encoding="utf-8")
    (cf / "Configuration.xml").write_text(CF_DESCRIPTOR, encoding="utf-8")
    ext_mod = cfe / "CommonModules" / "РасширенныйМодуль" / "Ext"
    ext_mod.mkdir(parents=True)
    (ext_mod / "Module.bsl").write_text(f"Процедура Р()\n{marker}\nКонецЕсли;\nКонецПроцедуры\n", encoding="utf-8")
    (cfe / "Configuration.xml").write_text(CF_DESCRIPTOR, encoding="utf-8")

    helpers, resolve_safe = make_helpers(str(cf))
    bsl = make_bsl_helpers(
        base_path=str(cf),
        resolve_safe=resolve_safe,
        read_file_fn=helpers["read_file"],
        grep_fn=helpers["grep"],
        glob_files_fn=helpers["glob_files"],
        format_info=detect_format(str(cf)),
        extension_paths=[str(cfe)],
    )
    grep_files = {r["file"].replace("\\", "/") for r in bsl["safe_grep"](marker)["results"]}
    res = bsl["count_matches"](marker)
    count_files = {t["file"].replace("\\", "/") for t in res["top"]}
    assert any(f.startswith("../") for f in grep_files), grep_files
    assert count_files == grep_files, "охват разошёлся с safe_grep"
    assert res["files_matched"] == 2, res
    # Вторая ось охвата обязана это ПОДТВЕРЖДАТЬ, а не молчать.
    assert res["extensions_included"] is True, res


def test_find_templates_total_is_the_full_count_not_the_page(tmp_path, monkeypatch):
    """``total`` — сколько ВСЕГО совпало, а не длина выданной страницы.

    Ридер применяет ``LIMIT`` сразу, поэтому ``total=len(rows)`` отвечал бы
    «макетов 200» на 15 452 макетах боевой конфигурации. Заодно ``truncated``
    считался как ``len(rows) >= limit`` и объявлял усечение там, где совпадений
    ровно ``limit``.
    """
    doc = tmp_path / "Documents" / "Заказ" / "Ext"
    doc.mkdir(parents=True)
    (doc / "ObjectModule.bsl").write_text("Процедура П() Экспорт\nКонецПроцедуры\n", encoding="utf-8")
    tdir = tmp_path / "Documents" / "Заказ" / "Templates"
    tdir.mkdir(parents=True)
    for i in range(5):
        (tdir / f"Макет{i}.xml").write_text(
            '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">'
            f"<Template><Properties><Name>Макет{i}</Name>"
            "<TemplateType>SpreadsheetDocument</TemplateType></Properties></Template></MetaDataObject>",
            encoding="utf-8",
        )
    (tmp_path / "Configuration.xml").write_text(CF_DESCRIPTOR, encoding="utf-8")
    bsl, reader = _helpers_for(tmp_path, monkeypatch, with_index=True)
    try:
        page = bsl["find_templates"](limit=2)
        assert len(page["templates"]) == 2
        assert page["total"] == 5, page
        assert page["truncated"] is True
        # Ровно limit совпадений — усечения НЕТ.
        exact = bsl["find_templates"](limit=5)
        assert exact["total"] == 5 and exact["truncated"] is False, exact
        # Счёт идёт ТЕМ ЖЕ фильтром, что и выдача.
        one = bsl["find_templates"](name="Макет1")
        assert one["total"] == 1 and one["truncated"] is False, one
        # Полный ответ НЕ помечается неполным.
        assert page["partial"] is False and exact["partial"] is False
        # Страница и счёт приходят ОДНИМ запросом — из одного снимка.
        got = reader.get_templates(owner="Document.Заказ", limit=2)
        assert isinstance(got, dict), got
        assert len(got["templates"]) == 2 and got["total"] == 5, got
    finally:
        reader.close()


def test_find_templates_declares_incompleteness_when_the_count_is_unavailable(tmp_path, monkeypatch):
    """Адаптер без полного счёта обязан дать ОБЪЯВЛЕННУЮ неполноту, а не тихий `total`.

    Пока страница и счёт читались двумя независимыми вызовами, недоступность
    второго (транзиентный отказ во время пересборки либо сторонний ридер без
    нового метода) молча подставляла длину СТРАНИЦЫ в `total` и оставляла
    `partial=False`, `truncated=False` — то есть ровно ту ложь «это всё», ради
    которой ключ и переделывался.
    """
    doc = tmp_path / "Documents" / "Заказ" / "Ext"
    doc.mkdir(parents=True)
    (doc / "ObjectModule.bsl").write_text("Процедура П() Экспорт\nКонецПроцедуры\n", encoding="utf-8")
    tdir = tmp_path / "Documents" / "Заказ" / "Templates"
    tdir.mkdir(parents=True)
    for i in range(5):
        (tdir / f"Макет{i}.xml").write_text(
            '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">'
            f"<Template><Properties><Name>Макет{i}</Name>"
            "<TemplateType>SpreadsheetDocument</TemplateType></Properties></Template></MetaDataObject>",
            encoding="utf-8",
        )
    (tmp_path / "Configuration.xml").write_text(CF_DESCRIPTOR, encoding="utf-8")
    bsl, reader = _helpers_for(tmp_path, monkeypatch, with_index=True)
    try:
        # Старый/сторонний адаптер: отдаёт ГОЛЫЙ список без счёта.
        legacy = (reader.get_templates(limit=2) or {})["templates"]
        monkeypatch.setattr(reader, "get_templates", lambda **_kw: legacy, raising=False)
        res = bsl["find_templates"](limit=2)
        assert len(res["templates"]) == 2
        # Неполнота ОБЪЯВЛЕНА, а не спрятана.
        assert res["partial"] is True, res
        assert res["_meta"]["reason"] == "template_total_unavailable", res
        assert "НИЖНЯЯ" in res["hint"], res["hint"]
        # `truncated` КОНСЕРВАТИВЕН: ложное «возможно, не всё» безопасно,
        # ложное «всё» — нет.
        assert res["truncated"] is True, res
    finally:
        reader.close()


def _subsystem_scan_fixture(tmp_path):
    subs = tmp_path / "Subsystems"
    subs.mkdir()
    for name, content in (
        ("СНашим", ["Document.Заказ"]),
        ("БезНашего", ["Catalog.Прочее"]),
        ("ТожеБез", []),
    ):
        items = "".join(f"<xr:Item>{c}</xr:Item>" for c in content)
        block = f"<Content>{items}</Content>" if content else "<Content/>"
        (subs / f"{name}.xml").write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"\n'
            '                xmlns:v8="http://v8.1c.ru/8.1/data/core"\n'
            '                xmlns:xr="http://v8.1c.ru/8.3/xcf/readable">\n'
            f"<Subsystem><Properties><Name>{name}</Name>{block}</Properties></Subsystem></MetaDataObject>\n",
            encoding="utf-8",
        )
    d = tmp_path / "Documents" / "Заказ" / "Ext"
    d.mkdir(parents=True)
    (d / "ObjectModule.bsl").write_text("Процедура П()\nКонецПроцедуры\n", encoding="utf-8")
    (tmp_path / "Configuration.xml").write_text(CF_DESCRIPTOR, encoding="utf-8")


def test_live_scan_files_total_counts_the_whole_enumeration(tmp_path):
    """``files_total`` — размер ПЕРЕЧИСЛЕНИЯ, а не число выживших после префильтра."""
    _subsystem_scan_fixture(tmp_path)
    res = _bsl_for(tmp_path)["analyze_subsystem"]("Заказ")
    scan = res["_meta"]["live_scan"]
    assert scan["files_total"] == 3, scan
    # Префильтр отбросил два файла, но из ПЕРЕЧИСЛЕНИЯ они не исчезли.
    assert scan["files_read"] == 3 and scan["files_parsed"] == 1, scan
    assert scan["truncated"] is False
    assert [s["match"] for s in res["subsystems"]] == ["content"]


def test_live_scan_cap_reports_the_pre_slice_total_and_says_so(tmp_path, monkeypatch):
    """На УПОРЕ ``files_total`` обязан называть исходное число кандидатов.

    Значение считалось ПОСЛЕ среза, поэтому на упоре всегда равнялось потолку —
    то есть ровно там, где число и важно, прятало, сколько кандидатов осталось за
    бортом. Непустой ответ вдобавок читался как полный: про упор в нём не
    говорилось ничего.

    Потолок правится МОНКИПАТЧЕМ модульной константы (она вынесена на уровень
    модуля ровно для этого, как `_DELEGATES_PAGE_*` в Задаче 1): фикстура на
    5001 описатель стоила бы ~32 с на один вызов.
    """
    import rlm_tools_bsl.bsl_helpers as bh

    _subsystem_scan_fixture(tmp_path)
    monkeypatch.setattr(bh, "_SUBSYSTEM_LIVE_SCAN_MAX", 2)
    res = _bsl_for(tmp_path)["analyze_subsystem"]("Заказ")
    scan = res["_meta"]["live_scan"]
    assert scan["truncated"] is True, scan
    # ГЛАВНОЕ: не 2 (потолок), а 3 — сколько кандидатов было ДО среза.
    assert scan["files_total"] == 3, scan
    assert scan["files_read"] == 2, scan
    # Упор назван и в ПРОЗЕ: непустой ответ иначе читается как полный.
    text = res.get("hint") or ""
    assert "НЕПОЛОН" in text and "потолок" in text, text


def test_role_with_both_explicit_right_and_flag_keeps_its_exclusions(tmp_path, monkeypatch):
    """СМЕШАННАЯ роль: и явное право на объект, и ``setForNewObjects``.

    Флагованная строка пропускалась как дубль, и вместе с ней терялись
    ``excluded_rights`` — ровно то, ради чего задача и делалась; ``granted_via``
    при этом говорил ``explicit`` там, где право раздаёт ФЛАГ, а ``flag_covered``
    в профиле занижался до нуля. Такой ``Rights.xml`` валиден: запись объекта у
    флагованной роли может нести и ``true``, и ``false`` одновременно.
    """
    doc = tmp_path / "Documents" / "Заказ" / "Ext"
    doc.mkdir(parents=True)
    (doc / "ObjectModule.bsl").write_text("Процедура П()\nКонецПроцедуры\n", encoding="utf-8")
    r = tmp_path / "Roles" / "ПолныеПрава" / "Ext"
    r.mkdir(parents=True)
    entries = "<object><name>Document.Заказ</name>" + _right("Read", "true") + _right("Delete", "false") + "</object>"
    (r / "Rights.xml").write_text(_rights_xml(flag=True, entries=entries), encoding="utf-8")
    (tmp_path / "Configuration.xml").write_text(CF_DESCRIPTOR, encoding="utf-8")
    bsl, reader = _helpers_for(tmp_path, monkeypatch, with_index=True)
    try:
        rows = bsl["find_roles"]("Заказ")["roles"]
        assert len(rows) == 1, rows
        row = rows[0]
        assert row["role_name"] == "ПолныеПрава"
        # Управляющий механизм — ФЛАГ, и провенанс обязан называть его.
        assert row["granted_via"] == "set_for_new_objects", row
        # Явные права НЕ потеряны слиянием.
        assert row["rights"] == ["Read"], row
        # Исключение восстановлено — оно и есть предмет задачи.
        assert row["excluded_rights"] == ["Delete"], row
        # Тип ключа один на обе формы строки.
        assert isinstance(row["rights_by_object"], list), row
        # Профиль считает ТО ЖЕ САМОЕ.
        section = bsl["get_object_profile"]("Документ.Заказ")["sections"]["roles"]
        assert section["summary"]["flag_covered"] == 1, section
        assert section["items"][0]["excluded_rights"] == ["Delete"], section
    finally:
        reader.close()


def test_find_roles_rows_carry_excluded_rights_unconditionally(tmp_path, monkeypatch):
    """Набор ключей строки один на обе формы: явную и флагованную."""
    _roles_fixture(tmp_path)
    bsl, reader = _helpers_for(tmp_path, monkeypatch, with_index=True)
    try:
        rows = bsl["find_roles"]("Заказ")["roles"]
        assert rows
        for row in rows:
            assert "granted_via" in row and "excluded_rights" in row, row
            assert isinstance(row["excluded_rights"], list), row
        by_name = {r["role_name"]: r for r in rows}
        assert by_name["Читатель"]["granted_via"] == "explicit"
        assert by_name["Читатель"]["excluded_rights"] == []
    finally:
        reader.close()


def _default_reuse_fixture(tmp_path, *, edt: bool):
    """Один общий модуль БЕЗ явного `returnValuesReuse` в EDT и с явным `DontUse` в CF.

    Это и есть настоящая пара «одна конфигурация, две выгрузки»: Конфигуратор
    выписывает дефолт всегда, EDT опускает его целиком.

    Якорный `.bsl` обязателен (см. `_seed_one_module`): на дереве без единого
    модуля сборка идёт путём пустого репозитория и метаданные не собирает вовсе.
    Своей строки в `common_module_props` якорь не даёт — у его каталога нет
    описателя, а `_iter_flat_objects` отдаёт только sibling-XML и `<Имя>/<Имя>.mdo`.
    """
    _seed_one_module(tmp_path)
    if edt:
        d = tmp_path / "CommonModules" / "МодульПоУмолчанию"
        d.mkdir(parents=True)
        (d / "МодульПоУмолчанию.mdo").write_text(
            '<mdclass:CommonModule xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass">'
            "<name>МодульПоУмолчанию</name><server>true</server>"
            "</mdclass:CommonModule>",
            encoding="utf-8",
        )
    else:
        (tmp_path / "CommonModules").mkdir(parents=True, exist_ok=True)
        (tmp_path / "CommonModules" / "МодульПоУмолчанию.xml").write_text(
            '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses" '
            'xmlns:v8="http://v8.1c.ru/8.1/data/core"><CommonModule><Properties>'
            "<Name>МодульПоУмолчанию</Name><Server>true</Server>"
            "<ReturnValuesReuse>DontUse</ReturnValuesReuse>"
            "</Properties></CommonModule></MetaDataObject>",
            encoding="utf-8",
        )
    (tmp_path / "Configuration.xml").write_text(CF_DESCRIPTOR, encoding="utf-8")


@pytest.mark.parametrize("edt", [False, True], ids=["cf", "edt"])
def test_omitted_return_values_reuse_reads_as_dontuse_on_both_formats(tmp_path, monkeypatch, edt):
    """`ReturnValuesReuse` подчиняется ТОМУ ЖЕ правилу, что булевы флаги и тип макета.

    Замер на боевых корнях: CF выписывает узел ВСЕГДА (3918 из 3918), EDT — только
    у 198 из 3309, то есть 3111 модулей опускают дефолт `DontUse`. Пока дефолт не
    разворачивался, одна и та же конфигурация отвечала `DontUse` на CF и ПУСТОЙ
    строкой на EDT, а документированный `find_common_modules(flag='DontUse')` на
    EDT молча возвращал ноль при девяноста четырёх процентах подходящих модулей.

    Прежний гард паритета этого не ловил: он ставит явный `DuringSession` в ОБОИХ
    форматах, то есть ветку опущенного узла не проходит вовсе.
    """
    _default_reuse_fixture(tmp_path, edt=edt)
    bsl, reader = _helpers_for(tmp_path, monkeypatch, with_index=True)
    try:
        (row,) = bsl["find_common_modules"]("МодульПоУмолчанию")["modules"]
        assert row["return_values_reuse"] == "DontUse", row
        # Документированный запрос обязан находить модуль на ОБОИХ форматах.
        found = [m["module_name"] for m in bsl["find_common_modules"](flag="DontUse")["modules"]]
        assert found == ["МодульПоУмолчанию"], found
        # Соседнее значение при этом не притягивается.
        assert bsl["find_common_modules"](flag="DuringSession")["modules"] == []
    finally:
        reader.close()


@pytest.mark.parametrize("edt", [False, True], ids=["cf", "edt"])
def test_default_return_values_reuse_survives_without_an_index(tmp_path, monkeypatch, edt):
    """Живая ветка идёт ТЕМ ЖЕ парсером — дефолт обязан совпасть с индексным."""
    _default_reuse_fixture(tmp_path, edt=edt)
    bsl, reader = _helpers_for(tmp_path, monkeypatch, with_index=False)
    try:
        res = bsl["find_common_modules"]("МодульПоУмолчанию")
        assert res["source"] == "live" and res["partial"] is True
        (row,) = res["modules"]
        assert row["return_values_reuse"] == "DontUse", row
        assert [m["module_name"] for m in bsl["find_common_modules"](flag="DontUse")["modules"]] == [
            "МодульПоУмолчанию"
        ]
    finally:
        if reader:
            reader.close()


def test_parser_normalizes_the_default_in_both_branches():
    """Дефолт применяется и к CF-ветке — гард против возврата расхождения.

    На боевом CF узел есть всегда, поэтому ветка достижима только на неполной
    выгрузке; отдать там пустую строку значило бы вернуть ровно то расхождение
    форматов, которое константа и закрывает.
    """
    from rlm_tools_bsl.bsl_xml_parsers import DEFAULT_RETURN_VALUES_REUSE, parse_common_module_props

    assert DEFAULT_RETURN_VALUES_REUSE == "DontUse"
    cf_without_node = (
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses" '
        'xmlns:v8="http://v8.1c.ru/8.1/data/core"><CommonModule><Properties>'
        "<Name>Голый</Name></Properties></CommonModule></MetaDataObject>"
    )
    edt_without_node = (
        '<mdclass:CommonModule xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass">'
        "<name>Голый</name></mdclass:CommonModule>"
    )
    for xml in (cf_without_node, edt_without_node):
        parsed = parse_common_module_props(xml)
        assert parsed is not None
        assert parsed["return_values_reuse"] == DEFAULT_RETURN_VALUES_REUSE, xml[:60]
        # Булевы флаги продолжают читаться как False — правило одно на все свойства.
        assert parsed["server"] is False and parsed["privileged"] is False
    # Явное значение по-прежнему читается как есть и дефолтом не затирается.
    explicit = parse_common_module_props(
        '<mdclass:CommonModule xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass">'
        "<name>Явный</name><returnValuesReuse>DuringRequest</returnValuesReuse></mdclass:CommonModule>"
    )
    assert explicit["return_values_reuse"] == "DuringRequest"
