"""v1.40.0 — «фильтр значит то, что написано».

Релиз read-time: схема SQLite, ``BUILDER_VERSION`` и сборщик не тронуты. Каждый
тест ниже воспроизводит конкретный дефект и обязан краснеть на откате своей
правки. Приватные фикстуры других файлов не импортируются (каталог ``tests`` не
package) — деревья собираются здесь же.
"""

from __future__ import annotations

import math
import threading
import types

import pytest

from rlm_tools_bsl.bsl_helpers import make_bsl_helpers
from rlm_tools_bsl.bsl_index import IndexBuilder, IndexReader
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


def _write(path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _bsl_for(base_path, idx_reader=None):
    helpers, resolve_safe = make_helpers(str(base_path), idx_reader)
    return make_bsl_helpers(
        base_path=str(base_path),
        resolve_safe=resolve_safe,
        read_file_fn=helpers["read_file"],
        grep_fn=helpers["grep"],
        glob_files_fn=helpers["glob_files"],
        format_info=detect_format(str(base_path)),
        idx_reader=idx_reader,
    )


def _build_reader(root, monkeypatch, *, build_metadata: bool = True) -> IndexReader:
    monkeypatch.setenv("RLM_INDEX_DIR", str(root.parent / (root.name + "_idx")))
    db = IndexBuilder().build(str(root), build_calls=True, build_metadata=build_metadata)
    return IndexReader(db)


# ═══════════════════════ Задача 1 — арг-гарды (Д4, Д5, Д6) ═══════════════════════

_SUBSCRIPTION_NAMES = ("ПодпА", "ПодпБ", "ПодпВ")
_OPTION_NAMES = ("ОпцияА", "ОпцияБ", "ОпцияВ")


def _cf_subscription(name: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"\n'
        '                xmlns:v8="http://v8.1c.ru/8.1/data/core">\n'
        "<EventSubscription><Properties>\n"
        f"<Name>{name}</Name>\n"
        "<Source><v8:Type>cfg:DocumentObject.Заказ</v8:Type></Source>\n"
        f"<Handler>CommonModule.М.{name}Обработчик</Handler>\n"
        "<Event>BeforeWrite</Event>\n"
        "</Properties></EventSubscription></MetaDataObject>\n"
    )


def _cf_functional_option(name: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"\n'
        '                xmlns:xr="http://v8.1c.ru/8.3/xcf/readable">\n'
        "  <FunctionalOption>\n"
        "    <Properties>\n"
        f"      <Name>{name}</Name>\n"
        "      <Content>\n"
        "        <xr:Object>Document.Заказ</xr:Object>\n"
        "      </Content>\n"
        "    </Properties>\n"
        "  </FunctionalOption>\n"
        "</MetaDataObject>\n"
    )


def _task1_tree(root):
    """Документ с тремя подписками и тремя ФО (две из них — ещё и в коде)."""
    _write(root / "Configuration.xml", CF_DESCRIPTOR)
    _write(
        root / "Documents" / "Заказ" / "Ext" / "ObjectModule.bsl",
        "Процедура ПередЗаписью(Отказ)\n"
        '    Если ПолучитьФункциональнуюОпцию("ОпцияА") Тогда\n'
        "        Отказ = Истина;\n"
        "    КонецЕсли;\n"
        '    Если ПолучитьФункциональнуюОпцию("ОпцияБ") Тогда\n'
        "        Отказ = Ложь;\n"
        "    КонецЕсли;\n"
        "    ПроверитьЗаказ();\n"
        "КонецПроцедуры\n"
        "\n"
        "Процедура ПроверитьЗаказ()\n"
        "КонецПроцедуры\n",
    )
    for name in _SUBSCRIPTION_NAMES:
        _write(root / "EventSubscriptions" / name / "Ext" / "EventSubscription.xml", _cf_subscription(name))
    for name in _OPTION_NAMES:
        _write(root / "FunctionalOptions" / name / "Ext" / "FunctionalOption.xml", _cf_functional_option(name))
    return root


@pytest.fixture
def task1_bsl(tmp_path):
    root = _task1_tree(tmp_path / "cfg")
    return _bsl_for(root)


# Вход, на котором «отдать всё» — единственный честный ответ: пять из восьми сегодня
# роняют весь rlm_execute, `-1`/`-0.5` молча дают пустую страницу, `True` — одну строку.
_GARBAGE_LIMITS = [
    pytest.param("abc", id="str"),
    pytest.param([1], id="list"),
    pytest.param(True, id="bool"),
    pytest.param(float("nan"), id="nan"),
    pytest.param(float("inf"), id="+inf"),
    pytest.param(float("-inf"), id="-inf"),
    pytest.param(-1, id="-1"),
    pytest.param(-0.5, id="-0.5"),
]


@pytest.mark.parametrize("limit", _GARBAGE_LIMITS)
def test_family_b_garbage_limit_returns_the_whole_page(task1_bsl, limit):
    subs = task1_bsl["find_event_subscriptions"]("Заказ", limit=limit)
    assert isinstance(subs, dict), "не-None limit обязан дать ПАГИНИРОВАННУЮ форму"
    assert subs["total"] == len(_SUBSCRIPTION_NAMES)
    assert subs["returned"] == subs["total"]
    assert subs["has_more"] is False

    fo = task1_bsl["find_functional_options"]("Заказ", limit=limit)
    assert "returned" in fo and "has_more" in fo
    assert fo["xml_total"] == len(_OPTION_NAMES)
    assert fo["code_total"] >= 1, "фикстура обязана давать непустую code-корзину"
    assert fo["returned"] == fo["xml_total"] + fo["code_total"]
    assert fo["has_more"] is False


def test_family_b_zero_is_an_empty_page(task1_bsl):
    subs = task1_bsl["find_event_subscriptions"]("Заказ", limit=0)
    assert subs["returned"] == 0 and subs["has_more"] is True
    fo = task1_bsl["find_functional_options"]("Заказ", limit=0)
    assert fo["returned"] == 0 and fo["has_more"] is True


def test_family_b_float_is_truncated(task1_bsl):
    subs = task1_bsl["find_event_subscriptions"]("Заказ", limit=1.7)
    assert subs["returned"] == 1 and subs["has_more"] is True


def test_family_b_numeric_string_is_not_a_number(task1_bsl, caplog):
    """Объявленное изменение: `int('1')` больше не судья — строка некорректна."""
    with caplog.at_level("WARNING"):
        subs = task1_bsl["find_event_subscriptions"]("Заказ", limit="1")
    assert subs["returned"] == subs["total"] == len(_SUBSCRIPTION_NAMES)
    messages = [r.getMessage() for r in caplog.records]
    assert any("arg-guard: limit='1' некорректен" in m for m in messages), messages


@pytest.fixture
def task1_indexed(tmp_path, monkeypatch):
    root = _task1_tree(tmp_path / "cfg")
    reader = _build_reader(root, monkeypatch)
    yield _bsl_for(root, reader)
    reader.close()


@pytest.mark.parametrize(("value", "expected_hierarchy", "expected_path"), [(math.inf, 3, 8), (-math.inf, 1, 1)])
def test_depth_infinity_is_clamped_not_raised(task1_indexed, value, expected_hierarchy, expected_path):
    hierarchy = task1_indexed["find_call_hierarchy"]("ПроверитьЗаказ", depth=value)
    assert hierarchy["depth"] == expected_hierarchy

    path = task1_indexed["find_path"]("ПередЗаписью", "ПроверитьЗаказ", max_depth=value)
    assert path["_meta"]["max_depth"] == expected_path

    data = task1_indexed["find_data_path"]("Документ.Заказ", "Справочник.Нет", max_depth=value)
    assert data["_meta"]["max_depth"] == expected_path


@pytest.fixture
def generic_tree(tmp_path):
    """Двенадцать файлов с совпадением — больше дефолта `grep_read(max_files=10)`."""
    root = tmp_path / "gen"
    for i in range(12):
        _write(root / "Dir" / f"f{i:02d}.txt", f"строка\nМаркер номер {i}\nхвост\n")
    helpers, _ = make_helpers(str(root))
    return helpers


@pytest.mark.parametrize("max_depth", [None, "2", float("nan"), float("inf"), -1])
def test_generic_tree_restores_default(generic_tree, max_depth):
    out = generic_tree["tree"](".", max_depth)
    assert isinstance(out, str)
    assert "f00.txt" in out  # дефолт 3 достаёт до файлов на глубине 2


@pytest.mark.parametrize("max_files", [None, -1, "5"])
def test_generic_grep_read_restores_default_max_files(generic_tree, max_files):
    res = generic_tree["grep_read"]("Маркер", ".", max_files)
    assert len(res["files"]) == 10
    assert "showing -" not in res["summary"]
    assert "(showing 10, 2 more)" in res["summary"]


def test_generic_grep_read_none_context_behaves_as_zero(generic_tree):
    baseline = generic_tree["grep_read"]("Маркер", ".", 10, 0)
    res = generic_tree["grep_read"]("Маркер", ".", 10, None)
    assert res["files"] == baseline["files"]


_GRID = [None, "abc", [1], True, False, math.nan, math.inf, -math.inf, -1, -0.5, 0, 0.5, 2.5, 7, 10**30]


@pytest.mark.parametrize("value", _GRID, ids=repr)
def test_every_bound_implementation_agrees_on_the_grid(value):
    """Правило живёт в одном месте: копии, если их поправить поодиночке, разойдутся.

    (а) ``_normalize_role_details_limit`` сохраняет свои тексты, но числа — те же,
    что у общего нормализатора; (б) зеркало насыщения песочницы считает ТОТ ЖЕ
    эффективный лимит, что и сам хелпер.
    """
    from rlm_tools_bsl._arg_guards import coerce_bound
    from rlm_tools_bsl.bsl_index import _ROLE_DETAILS_DEFAULT, _ROLE_DETAILS_MAX, _normalize_role_details_limit
    from rlm_tools_bsl.sandbox import Sandbox

    roles = coerce_bound(value, _ROLE_DETAILS_DEFAULT, "details_limit", "", maximum=_ROLE_DETAILS_MAX)[0]
    assert roles == _normalize_role_details_limit(value)[0]

    default = Sandbox._SATURATING_LIST_HELPERS["search_regions"][1]
    effective = coerce_bound(value, default, "limit", "")[0]
    lengths = {250}
    if 0 < effective <= 250:
        lengths |= {effective - 1, effective}
    for n in sorted(lengths):
        stub = types.SimpleNamespace(
            _SATURATING_LIST_HELPERS=Sandbox._SATURATING_LIST_HELPERS,
            _execute_saturated={},
        )
        Sandbox._note_saturation(stub, "search_regions", (), {"limit": value}, [{}] * n)
        saturated = "search_regions" in stub._execute_saturated
        expected = effective > 0 and n >= effective
        assert saturated is expected, (value, effective, n)


def test_safe_grep_garbage_result_cap_does_not_raise(task1_bsl):
    res = task1_bsl["safe_grep"]("ПроверитьЗаказ", "Заказ", 20, _result_cap="x")
    assert isinstance(res, dict)
    assert res["returned"] >= 1


# ═══════════ Задача 2 — object_name ТОЧНЫЙ, префикс типа — категория (Д7, Д8, Д15, Д16) ═══════════


def _cf_object(tag: str, name: str, children: str = "", synonym: str = "") -> str:
    syn = f"<Synonym><v8:item><v8:lang>ru</v8:lang><v8:content>{synonym}</v8:content></v8:item></Synonym>"
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses" xmlns:v8="http://v8.1c.ru/8.1/data/core">\n'
        f"<{tag}><Properties><Name>{name}</Name>{syn if synonym else ''}</Properties>\n"
        f"<ChildObjects>{children}</ChildObjects></{tag}></MetaDataObject>\n"
    )


def _cf_child(kind: str, name: str) -> str:
    return f"<{kind}><Properties><Name>{name}</Name><Type><v8:Type>xs:string</v8:Type></Type></Properties></{kind}>"


def _cf_predefined(tag: str, *items: str) -> str:
    body = "".join(
        f'<Item id="{i}"><Name>{n}</Name><Code>{i:03d}</Code><Description>{n}</Description>'
        "<IsFolder>false</IsFolder></Item>"
        for i, n in enumerate(items, 1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<{tag} xmlns="http://v8.1c.ru/8.3/MDClasses" xmlns:v8="http://v8.1c.ru/8.1/data/core">'
        f"<PredefinedData>{body}</PredefinedData></{tag}>\n"
    )


_CF_FORM_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<Form xmlns="http://v8.1c.ru/8.3/xcf/logform">\n'
    '  <Events><Event name="OnCreateAtServer">ПриСозданииНаСервере</Event></Events>\n'
    "</Form>\n"
)


def _task2_tree(root):
    """Омонимы в двух категориях, XML-only объекты (без единого .bsl у самих объектов).

    Порядок категорий существенен: и с индексом, и без каскад отдаёт ``Documents``
    раньше ``AccumulationRegisters``, поэтому голое имя разрешается в документ, и только
    префикс может привести к регистру. У пары ``Бета`` порядок с индексом и без разный,
    поэтому тесты зовут ОБА префикса.
    """
    _write(root / "Configuration.xml", CF_DESCRIPTOR)
    # Сборщику нужен хотя бы один модуль; к объектам фикстуры он отношения не имеет.
    _write(root / "CommonModules" / "Служебный" / "Ext" / "Module.bsl", "Процедура Пусто() Экспорт\nКонецПроцедуры\n")
    docs = root / "Documents"
    _write(
        docs / "Альфа" / "Ext" / "Document.xml",
        _cf_object("Document", "Альфа", _cf_child("Attribute", "А1") + _cf_child("Attribute", "А2"), "Альфа док"),
    )
    _write(docs / "Альфа" / "Forms" / "ФормаДокумента" / "Ext" / "Form.xml", _CF_FORM_XML)
    _write(
        docs / "АльфаБета" / "Ext" / "Document.xml", _cf_object("Document", "АльфаБета", _cf_child("Attribute", "Б1"))
    )
    _write(
        docs / "ТолькоДок" / "Ext" / "Document.xml", _cf_object("Document", "ТолькоДок", _cf_child("Attribute", "Т1"))
    )
    reg = root / "AccumulationRegisters" / "Альфа"
    _write(
        reg / "Ext" / "AccumulationRegister.xml",
        _cf_object("AccumulationRegister", "Альфа", _cf_child("Dimension", "Р1")),
    )
    _write(reg / "Forms" / "ФормаСписка" / "Ext" / "Form.xml", _CF_FORM_XML)
    cat = root / "Catalogs" / "Бета" / "Ext"
    _write(cat / "Catalog.xml", _cf_object("Catalog", "Бета", synonym="Бета справочник"))
    _write(cat / "Predefined.xml", _cf_predefined("Catalog", "П1"))
    pvh = root / "ChartsOfCharacteristicTypes" / "Бета" / "Ext"
    _write(pvh / "ChartOfCharacteristicTypes.xml", _cf_object("ChartOfCharacteristicTypes", "Бета", synonym="Бета ПВХ"))
    _write(pvh / "Predefined.xml", _cf_predefined("ChartOfCharacteristicTypes", "П2"))
    gamma = root / "Catalogs" / "БетаГамма" / "Ext"
    _write(gamma / "Catalog.xml", _cf_object("Catalog", "БетаГамма", synonym="Бета гамма"))
    _write(gamma / "Predefined.xml", _cf_predefined("Catalog", "П3"))
    return root


@pytest.fixture(params=[False, True], ids=["live", "index"])
def task2(request, tmp_path, monkeypatch):
    root = _task2_tree(tmp_path / "cfg")
    reader = _build_reader(root, monkeypatch) if request.param else None
    yield types.SimpleNamespace(bsl=_bsl_for(root, reader), reader=reader, root=root, indexed=request.param)
    if reader is not None:
        reader.close()


def _names(rows, key="name") -> set[str]:
    return {r[key] for r in rows}


def test_full_structure_does_not_mix_neighbour_attributes(task2):
    res = task2.bsl["get_object_full_structure"]("Документ.Альфа")
    assert res["category"] == "Documents"
    assert _names(res["attributes"]) == {"А1", "А2"}


def test_full_structure_honors_type_prefix_on_homonym(task2):
    res = task2.bsl["get_object_full_structure"]("РегистрНакопления.Альфа")
    assert res["category"] == "AccumulationRegisters"
    assert _names(res["dimensions"]) == {"Р1"}
    # Голое имя по-прежнему разрешается первым омонимом каскада — документом.
    assert task2.bsl["get_object_full_structure"]("Альфа")["category"] == "Documents"


def test_full_structure_typed_prefix_is_strict(task2):
    res = task2.bsl["get_object_full_structure"]("РегистрНакопления.ТолькоДок")
    assert "error" in res
    assert "не найден в категории AccumulationRegisters" in res["error"]
    # Явный category_hint (его передаёт профиль) сохраняет прежний повтор без фильтра.
    hinted = task2.bsl["get_object_full_structure"]("ТолькоДок", category_hint="AccumulationRegisters")
    assert hinted["category"] == "Documents"


def test_full_structure_predefined_is_exact_and_category_scoped(task2):
    pvh = task2.bsl["get_object_full_structure"]("ПланВидовХарактеристик.Бета")
    assert pvh["category"] == "ChartsOfCharacteristicTypes"
    assert _names(pvh["predefined_items"]) == {"П2"}
    cat = task2.bsl["get_object_full_structure"]("Справочник.Бета")
    assert cat["category"] == "Catalogs"
    assert _names(cat["predefined_items"]) == {"П1"}


def test_full_structure_forms_belong_to_the_category(tmp_path, monkeypatch):
    """Формы омонимов — только в индексной ветке (CF-описатель форм не перечисляет)."""
    root = _task2_tree(tmp_path / "cfg")
    reader = _build_reader(root, monkeypatch)
    try:
        bsl = _bsl_for(root, reader)
        assert bsl["get_object_full_structure"]("РегистрНакопления.Альфа")["forms"] == ["ФормаСписка"]
        assert bsl["get_object_full_structure"]("Документ.Альфа")["forms"] == ["ФормаДокумента"]
    finally:
        reader.close()


def test_exact_object_page_refetches_only_a_full_thinned_page():
    from rlm_tools_bsl._arg_guards import UNBOUNDED_PAGE
    from rlm_tools_bsl.bsl_helpers import _exact_object_page

    foreign = [{"object_name": "Бета", "category": "Catalogs"}] * 3
    own = {"object_name": "Бета", "category": "ChartsOfCharacteristicTypes"}
    calls: list[int] = []

    def fetch(limit):
        calls.append(limit)
        return (foreign + [own])[:limit]

    assert _exact_object_page(fetch, "Бета", "ChartsOfCharacteristicTypes", 3) == [own]
    assert calls == [3, UNBOUNDED_PAGE]

    calls.clear()
    assert _exact_object_page(fetch, "Бета", "ChartsOfCharacteristicTypes", 10) == [own]
    assert calls == [10], "неполная страница добора не требует"

    assert _exact_object_page(lambda _lim: None, "Бета", "", 5) is None


_RAW_META = {
    "builder_version": "16",
    "has_metadata": "1",
    "has_synonyms": "1",
    "has_calls": "0",
    "has_fts": "0",
    "bsl_count": "0",
}


def _raw_index(tmp_path, statements: list[tuple[str, tuple]]) -> IndexReader:
    """БД по _SCHEMA_SQL с прямыми INSERT — ридер-тест без сборки дерева."""
    import sqlite3

    from rlm_tools_bsl.bsl_index import _SCHEMA_SQL

    db = tmp_path / "raw.db"
    conn = sqlite3.connect(db)
    conn.executescript(_SCHEMA_SQL)
    conn.executemany("INSERT INTO index_meta (key, value) VALUES (?, ?)", list(_RAW_META.items()))
    for sql, params in statements:
        conn.execute(sql, params)
    conn.commit()
    conn.close()
    return IndexReader(db)


def _pi_insert(obj: str, cat: str, item: str) -> tuple[str, tuple]:
    return (
        "INSERT INTO predefined_items (object_name, category, item_name, item_synonym, item_code, types_json,"
        " is_folder, source_file) VALUES (?, ?, ?, '', '', '[]', 0, ?)",
        (obj, cat, item, f"{cat}/{obj}/Ext/Predefined.xml"),
    )


def test_find_predefined_refetches_a_page_eaten_by_the_homonym(tmp_path):
    rows = [_pi_insert("Бета", "Catalogs", f"С{i}") for i in range(5)]
    rows.append(_pi_insert("Бета", "ChartsOfCharacteristicTypes", "П2"))
    reader = _raw_index(tmp_path, rows)
    try:
        empty_root = tmp_path / "root"
        empty_root.mkdir()
        bsl = _bsl_for(empty_root, reader)
        got = bsl["find_predefined"](object_name="ПланВидовХарактеристик.Бета", limit=5)
        assert [(r["category"], r["item_name"]) for r in got] == [("ChartsOfCharacteristicTypes", "П2")]
    finally:
        reader.close()


def test_fragment_resolves_to_the_ranked_nearest_object(tmp_path):
    def attr(obj):
        return (
            "INSERT INTO object_attributes (object_name, category, attr_name, attr_synonym, attr_type, attr_kind,"
            " ts_name, source_file) VALUES (?, 'Catalogs', 'Р', '', '[]', 'attribute', NULL, ?)",
            (obj, f"Catalogs/{obj}.xml"),
        )

    def syn(obj):
        return (
            "INSERT INTO object_synonyms (object_name, category, synonym, file) VALUES (?, 'Catalogs', ?, ?)",
            (obj, obj, f"Catalogs/{obj}.xml"),
        )

    reader = _raw_index(
        tmp_path,
        [attr("ГруппыДоступаКонтрагентов"), attr("Контрагенты"), syn("ГруппыДоступаКонтрагентов"), syn("Контрагенты")],
    )
    try:
        empty_root = tmp_path / "root"
        empty_root.mkdir()
        bsl = _bsl_for(empty_root, reader)
        assert bsl["get_object_full_structure"]("Контраге")["object_name"] == "Контрагенты"
    finally:
        reader.close()


def _pairs(rows) -> set[tuple[str, str]]:
    return {(r["category"], r["object_name"]) for r in rows}


def test_find_attributes_bare_name_returns_every_exact_homonym(task2):
    rows = task2.bsl["find_attributes"](object_name="Альфа")
    assert _pairs(rows) == {("Documents", "Альфа"), ("AccumulationRegisters", "Альфа")}


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        ("Документ.Альфа", {("Documents", "Альфа")}),
        ("документ.Альфа", {("Documents", "Альфа")}),
        ("РегистрНакопления.Альфа", {("AccumulationRegisters", "Альфа")}),
    ],
)
def test_find_attributes_typed_prefix_narrows_category(task2, ref, expected):
    assert _pairs(task2.bsl["find_attributes"](object_name=ref)) == expected


def test_find_attributes_index_and_live_agree(tmp_path, monkeypatch):
    root = _task2_tree(tmp_path / "cfg")
    live = _bsl_for(root)
    reader = _build_reader(root, monkeypatch)
    try:
        indexed = _bsl_for(root, reader)
        for ref in ("Альфа", "Документ.Альфа"):
            got_index = {
                (r["category"], r["object_name"], r["attr_name"]) for r in indexed["find_attributes"](object_name=ref)
            }
            got_live = {
                (r["category"], r["object_name"], r["attr_name"]) for r in live["find_attributes"](object_name=ref)
            }
            assert got_index == got_live, ref
            assert got_live, ref
    finally:
        reader.close()


def test_find_predefined_object_name_is_exact(task2):
    bare = task2.bsl["find_predefined"](object_name="Бета")
    assert _names(bare, "item_name") == {"П1", "П2"}
    typed = task2.bsl["find_predefined"](object_name="Справочник.Бета")
    assert _names(typed, "item_name") == {"П1"}


def test_live_branch_finds_xml_only_object(tmp_path):
    root = _task2_tree(tmp_path / "cfg")
    rows = _bsl_for(root)["find_attributes"](object_name="ТолькоДок")
    assert _names(rows, "attr_name") == {"Т1"}


def test_reader_object_name_is_exact(tmp_path, monkeypatch):
    root = _task2_tree(tmp_path / "cfg")
    reader = _build_reader(root, monkeypatch)
    try:
        attrs = reader.get_object_attributes(object_name="Альфа")
        assert {r["object_name"] for r in attrs} == {"Альфа"}
        assert {r["category"] for r in attrs} == {"Documents", "AccumulationRegisters"}
        items = reader.get_predefined_items(object_name="бета")
        assert {r["item_name"] for r in items} == {"П1", "П2"}
    finally:
        reader.close()


# ═══════════════ Задача 3 — `%` и `_` литеральны во всех фильтрах (Д2, Д3) ═══════════════

_T3_NAMES = ("Пре_фикс", "ПреXфикс", "Пре%фикс", "ПреXYфикс")


def test_every_parameterized_like_is_escaped():
    """AST-tripwire: у КАЖДОГО параметризованного `LIKE` в `bsl_index.py` — `ESCAPE`.

    Правило ставится на каждое вхождение `LIKE`, а не на константу целиком: смежные
    литералы парсер склеивает в одну `ast.Constant`, и в константе реквизитов
    (`attr_name` … `OR` `attr_synonym`) `ESCAPE` второго `LIKE` иначе засчитывался бы
    первому. Операнд — текст от `LIKE` до ближайшего `AND`/`OR`/`ORDER`/`LIMIT`/`GROUP`
    либо следующего `LIKE`. Константы без `?` в операнде (`'Subsystems/%'`, `'sqlite_%'`)
    не судятся.
    """
    import ast
    import pathlib
    import re

    import rlm_tools_bsl.bsl_index as bi

    tree = ast.parse(pathlib.Path(bi.__file__).read_text(encoding="utf-8"))
    stop = re.compile(r"\b(?:AND|OR|ORDER|LIMIT|GROUP|LIKE)\b")
    judged, bad = 0, []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        for m in re.finditer(r"\bLIKE\b", node.value):
            rest = node.value[m.end() :]
            end = stop.search(rest)
            operand = rest[: end.start()] if end else rest
            if "?" not in operand:
                continue
            judged += 1
            if "ESCAPE" not in operand:
                bad.append((node.lineno, operand.strip()))
    assert judged >= 35, f"правило стало вакуумным: судимых вхождений {judged}"
    assert bad == [], bad


def _t3_statements() -> list[tuple[str, tuple]]:
    stmts: list[tuple[str, tuple]] = []
    for i, n in enumerate(_T3_NAMES, 1):
        stmts += [
            (
                "INSERT INTO modules (id, rel_path, category, object_name, module_type)"
                " VALUES (?, ?, 'CommonModules', ?, 'Module')",
                (i, f"CommonModules/М{i}/Ext/Module.bsl", f"М{i}"),
            ),
            ("INSERT INTO regions (module_id, name, line, end_line) VALUES (?, ?, 1, 2)", (i, n)),
            ("INSERT INTO module_headers (module_id, header_comment) VALUES (?, ?)", (i, f"Заголовок {n}")),
            (
                "INSERT INTO object_attributes (object_name, category, attr_name, attr_synonym, attr_type,"
                " attr_kind, ts_name, source_file) VALUES ('Док', 'Documents', ?, '', '[]', 'attribute', NULL, 'f')",
                (n,),
            ),
            (
                "INSERT INTO predefined_items (object_name, category, item_name, item_synonym, item_code,"
                " types_json, is_folder, source_file) VALUES ('Спр', 'Catalogs', ?, '', '', '[]', 0, 'f')",
                (n,),
            ),
            ("INSERT INTO scheduled_jobs (name, file) VALUES (?, 'f')", (n,)),
            ("INSERT INTO http_services (name, root_url, templates_json, file) VALUES (?, 'u', '[]', 'f')", (n,)),
            ("INSERT INTO web_services (name, namespace, operations_json, file) VALUES (?, 'ns', '[]', 'f')", (n,)),
            ("INSERT INTO xdto_packages (name, namespace, types_json, file) VALUES (?, 'ns', '[]', 'f')", (n,)),
            (
                "INSERT INTO subsystem_content (subsystem_name, subsystem_synonym, object_ref, file)"
                " VALUES (?, '', ?, ?)",
                (n, f"Catalog.{n}", f"Subsystems/П{i}.xml"),
            ),
            ("INSERT INTO common_module_props (module_name, file) VALUES (?, 'f')", (n,)),
            ("INSERT INTO templates (owner_ref, name, file) VALUES ('', ?, 'f')", (n,)),
        ]
    return stmts


def _strip_header(text: str) -> str:
    return text.removeprefix("Заголовок ")


_T3_READERS = {
    "search_regions": lambda r, q: {x["name"] for x in r.search_regions(q, 100)},
    "group_regions": lambda r, q: {g["key"] for g in r.group_regions(q, "name", 100)["groups"]},
    "search_module_headers": lambda r, q: {_strip_header(x["header_comment"]) for x in r.search_module_headers(q, 100)},
    "get_object_attributes": lambda r, q: {x["attr_name"] for x in r.get_object_attributes(attr_name=q)},
    "get_predefined_items": lambda r, q: {x["item_name"] for x in r.get_predefined_items(item_name=q)},
    "get_scheduled_jobs": lambda r, q: {x["name"] for x in r.get_scheduled_jobs(q)},
    "get_http_services": lambda r, q: {x["name"] for x in r.get_http_services(q)},
    "get_web_services": lambda r, q: {x["name"] for x in r.get_web_services(q)},
    "get_xdto_packages": lambda r, q: {x["name"] for x in r.get_xdto_packages(q)},
    "get_subsystems_for_object": lambda r, q: {x["name"] for x in r.get_subsystems_for_object(q)},
    "get_common_module_props": lambda r, q: {x["module_name"] for x in r.get_common_module_props(name=q)["modules"]},
    "get_templates": lambda r, q: {x["name"] for x in r.get_templates(name=q)["templates"]},
}


@pytest.fixture
def t3_reader(tmp_path):
    reader = _raw_index(tmp_path, _t3_statements())
    yield reader
    reader.close()


@pytest.mark.parametrize("reader_name", sorted(_T3_READERS))
@pytest.mark.parametrize("query", ["Пре_фикс", "Пре%фикс"])
def test_underscore_and_percent_are_literal(t3_reader, reader_name, query):
    assert _T3_READERS[reader_name](t3_reader, query) == {query}


@pytest.mark.parametrize("query", ["Пре_фикс", "Пре%фикс"])
def test_regions_list_count_group_agree_on_metacharacters(t3_reader, query):
    listed = t3_reader.search_regions(query, 10**6)
    assert t3_reader.count_regions(query) == len(listed) == 1
    groups = t3_reader.group_regions(query, "name", 100)["groups"]
    assert sum(g["count"] for g in groups) == t3_reader.count_regions(query)
    headers = t3_reader.search_module_headers(query, 10**6)
    assert t3_reader.count_module_headers(query) == len(headers) == 1


_EXT_DESCRIPTOR = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses" xmlns:v8="http://v8.1c.ru/8.1/data/core">\n'
    '  <Configuration uuid="00000000-0000-0000-0000-000000000003">\n'
    "    <Properties><ObjectBelonging>Adopted</ObjectBelonging><Name>Расш</Name>\n"
    "      <ConfigurationExtensionPurpose>Customization</ConfigurationExtensionPurpose>\n"
    "      <NamePrefix>р_</NamePrefix></Properties>\n"
    "  </Configuration>\n"
    "</MetaDataObject>\n"
)


def _regions_module(*names: str) -> str:
    return "".join(
        f"#Область {n}\nПроцедура П{i}() Экспорт\nКонецПроцедуры\n#КонецОбласти\n" for i, n in enumerate(names)
    )


def test_search_regions_main_and_extension_agree_on_underscore(tmp_path, monkeypatch):
    """Один ответ склеивает main (индекс) и CFE (живой проход) — правило обязано быть одно."""
    cf = tmp_path / "src" / "cf"
    cfe = tmp_path / "src" / "cfe" / "Расш"
    _write(cf / "Configuration.xml", CF_DESCRIPTOR)
    _write(cf / "CommonModules" / "Главный" / "Ext" / "Module.bsl", _regions_module("Пре_фикс", "ПреXфикс"))
    _write(cfe / "Configuration.xml", _EXT_DESCRIPTOR)
    _write(cfe / "CommonModules" / "р_Модуль" / "Ext" / "Module.bsl", _regions_module("Пре_фикс", "ПреXфикс"))
    reader = _build_reader(cf, monkeypatch)
    try:
        generic, resolve_safe = make_helpers(str(cf), idx_reader=reader)
        bsl = make_bsl_helpers(
            base_path=str(cf),
            resolve_safe=resolve_safe,
            read_file_fn=generic["read_file"],
            grep_fn=generic["grep"],
            glob_files_fn=generic["glob_files"],
            format_info=detect_format(str(cf)),
            idx_reader=reader,
            extension_paths=[str(cfe)],
        )
        rows = bsl["search_regions"]("Пре_фикс")
        assert [r["name"] for r in rows] == ["Пре_фикс", "Пре_фикс"], rows
        assert {r["owner"] for r in rows} == {"main", "extension:Расш"}, rows
    finally:
        reader.close()


def _cf_common_module(name: str) -> str:
    return (
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses" '
        'xmlns:v8="http://v8.1c.ru/8.1/data/core"><CommonModule><Properties>'
        f"<Name>{name}</Name><Global>false</Global><Server>true</Server>"
        "<ServerCall>false</ServerCall><Privileged>false</Privileged>"
        "<ExternalConnection>false</ExternalConnection>"
        "<ClientManagedApplication>false</ClientManagedApplication>"
        "<ClientOrdinaryApplication>false</ClientOrdinaryApplication>"
        "<ReturnValuesReuse>DontUse</ReturnValuesReuse>"
        "</Properties></CommonModule></MetaDataObject>"
    )


@pytest.mark.parametrize("with_index", [True, False], ids=["index", "live"])
def test_common_modules_live_page_matches_index_page_with_underscores(tmp_path, monkeypatch, with_index):
    """Паритет-тест v1.39.0 обходил `_` намеренно — теперь обещание дано без оговорки."""
    root = tmp_path / "cfg"
    _write(root / "Configuration.xml", CF_DESCRIPTOR)
    _write(root / "CommonModules" / "Якорь" / "Ext" / "Module.bsl", "Процедура Я() Экспорт\nКонецПроцедуры\n")
    for name in ("Мод_А", "МодXА"):
        _write(root / "CommonModules" / f"{name}.xml", _cf_common_module(name))
    reader = _build_reader(root, monkeypatch) if with_index else None
    try:
        res = _bsl_for(root, reader)["find_common_modules"](name="Мод_А")
        assert res["source"] == ("index" if with_index else "live"), res
        assert [m["module_name"] for m in res["modules"]] == ["Мод_А"], res
        assert res["total"] == 1, res
    finally:
        if reader is not None:
            reader.close()


# ═══════════════ Задача 4 — маска glob_files значит то же, что без индекса (Д9) ═══════════════


def _cf_scheduled_job(name: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses" xmlns:v8="http://v8.1c.ru/8.1/data/core">\n'
        f"<ScheduledJob><Properties><Name>{name}</Name>"
        f"<MethodName>CommonModule.Задания.{name}</MethodName><Use>true</Use></Properties>"
        "</ScheduledJob></MetaDataObject>\n"
    )


def _task4_tree(root):
    """«Чистая» фикстура: только .bsl/.mdo/.xml и ни одного скрытого/служебного каталога.

    ``file_paths`` хранит только эти расширения и пропускает скрытые каталоги и
    ``_SKIP_DIRS_NAV``, а ``pathlib.glob`` — нет; файл другого расширения покрасил бы
    сравнение без вины правки (пре-существующее различие индекса и ФС). Имена одного
    регистра — регистровая семантика ФС платформозависима и в тест не входит.
    """
    _write(root / "Configuration.xml", CF_DESCRIPTOR)
    _write(root / "Catalogs" / "Имя.xml", _cf_object("Catalog", "Имя"))
    _write(root / "Catalogs" / "ИмяДругое.xml", _cf_object("Catalog", "ИмяДругое"))
    _write(root / "Catalogs" / "Имя" / "Ext" / "ObjectModule.bsl", "Процедура А() Экспорт\nКонецПроцедуры\n")
    _write(root / "CommonModules" / "Общий" / "Ext" / "Module.bsl", "Процедура Б() Экспорт\nКонецПроцедуры\n")
    _write(root / "Documents" / "Заказ" / "Ext" / "ObjectModule.bsl", "Процедура В() Экспорт\nКонецПроцедуры\n")
    _write(root / "Documents" / "Имя" / "Имя.mdo", "<xml/>")
    for name in ("ПодпА", "ПодпБ"):
        _write(root / "EventSubscriptions" / f"{name}.xml", _cf_subscription(name))
    for name in ("ЗаданиеА", "ЗаданиеБ"):
        _write(root / "ScheduledJobs" / f"{name}.xml", _cf_scheduled_job(name))
    return root


_GLOB_MASKS = [
    "**/Имя.xml",
    "**/Имя.*",
    "**/EventSubscriptions/**/*.xml",
    "**/Ext/**/*.bsl",
    "Documents/*/ObjectModule.bsl",
    "Documents/*/Имя.mdo",
]


@pytest.fixture
def task4_reader(tmp_path, monkeypatch):
    root = _task4_tree(tmp_path / "cfg")
    reader = _build_reader(root, monkeypatch)
    yield root, reader
    reader.close()


@pytest.mark.parametrize("mask", _GLOB_MASKS)
def test_index_glob_matches_filesystem_glob(task4_reader, mask):
    root, reader = task4_reader
    fs = {p.relative_to(root).as_posix() for p in root.glob(mask) if p.is_file()}
    assert set(reader.glob_files(mask)) == fs, mask


def test_event_subscriptions_live_fallback_sees_top_level_on_no_metadata_index(tmp_path, monkeypatch):
    """Индекс без метаданных: живой фолбэк зовёт index-backed `glob_files` с маской
    `**/X/**/*.xml`, и до правки категория верхнего уровня давала молчаливый ноль."""
    root = _task4_tree(tmp_path / "cfg")
    reader = _build_reader(root, monkeypatch, build_metadata=False)
    try:
        bsl = _bsl_for(root, reader)
        assert _names(bsl["find_event_subscriptions"]()) == {"ПодпА", "ПодпБ"}
        assert _names(bsl["find_scheduled_jobs"]("")) == {"ЗаданиеА", "ЗаданиеБ"}
    finally:
        reader.close()


def test_glob_strategy_for_exact_name():
    from rlm_tools_bsl.bsl_index import _can_index_glob

    assert _can_index_glob("**/Configuration.mdo") == ("name_exact", {"filename": "Configuration.mdo"})


def test_glob_wildcard_extension_requires_the_dot(task4_reader):
    _root, reader = task4_reader
    assert sorted(reader.glob_files("**/Имя.*")) == ["Catalogs/Имя.xml", "Documents/Имя/Имя.mdo"]


def test_glob_and_tree_prefix_underscore_is_literal(tmp_path, monkeypatch):
    root = tmp_path / "cfg"
    _write(root / "Configuration.xml", CF_DESCRIPTOR)
    _write(root / "Каталог_1" / "Своё.xml", "<xml/>")
    _write(root / "КаталогX1" / "Чужое.xml", "<xml/>")
    _write(root / "CommonModules" / "Общий" / "Ext" / "Module.bsl", "Процедура Б() Экспорт\nКонецПроцедуры\n")
    reader = _build_reader(root, monkeypatch)
    try:
        assert reader.glob_files("Каталог_1/**") == ["Каталог_1/Своё.xml"]
        assert reader.tree_paths("Каталог_1", 3) == ["Каталог_1/Своё.xml"]
    finally:
        reader.close()


# ═══════════ Задача 5 — представление печатной формы из СВОЕЙ команды (Д10) ═══════════

_PF_MAIN = '''\
Процедура ДобавитьКомандыПечати(КомандыПечати) Экспорт
    КомандаПечати = КомандыПечати.Добавить();
    КомандаПечати.Идентификатор = "Первая";
    КомандаПечати.Представление =
        ОбщийМодуль.СинонимЧего();
    КомандаПечати.Порядок = 1;
    КомандаПечати = КомандыПечати.Добавить();
    КомандаПечати.Идентификатор = "Вторая";
    НоваяСтрока = ТаблицаНастроек.Добавить();
    НоваяСтрока.Идентификатор = "Служебная";
    КомандаПечати.Представление = НСтр("ru = 'Вторая форма';
        |en = 'Second'");
    КомандаПечати = КомандыПечати.Добавить();
    КомандаПечати.Идентификатор = "Третья";
    // КомандаПечати.Представление = НСтр("ru = 'Закомментировано'");
    // КомандаПечати.Идентификатор = "Призрак";
    // КомандаПечати = КомандыПечати.Добавить(); КомандаПечати.Идентификатор = "Призрак2";
    КомандаПечати.Порядок = 3;
    КомандаПечати = КомандыПечати.Добавить();
    КомандаПечати.Идентификатор = "Четвертая";
    КомандаПечати = Обработки.ПечатьЧего.ДобавитьКомандуПечати(КомандыПечати, "Делегат");
    КомандаПечати.Представление = НСтр("ru = 'Чужое'");
    ДобавитьКомандуПечати(КомандыПечати, "Помощник", НСтр("ru = 'Через ""помощника""'"));
    НеДобавитьКомандуПечати(КомандыПечати, "НеКоманда");
    КомандаПечати = КомандыПечати.Добавить();
    КомандаПечати.Идентификатор = "Акт,Счет";
    КомандаПечати.Представление = НСтр("en = 'Receipt'; ru = 'Чек д''Артаньяна'");
    КомандаПечати = КомандыПечати.Добавить();
    КомандаПечати.Идентификатор = "Литерал";
    КомандаПечати.Представление = "Счет ""Фактура""";
    МодульЛокализация.ДобавитьКомандыПечати(КомандыПечати);
    ВводОстатковЛокализация.ВводОстатковДобавитьКомандыПечати(КомандыПечати);
КонецПроцедуры
'''


def _print_forms(tmp_path, module_text: str, doc: str = "Тест") -> dict:
    root = tmp_path / "cfg"
    _write(root / "Configuration.xml", CF_DESCRIPTOR)
    _write(root / "Documents" / doc / "Ext" / "ManagerModule.bsl", module_text)
    return _bsl_for(root)["find_print_forms"](doc)


def _mini(body: str) -> str:
    return f"Процедура ДобавитьКомандыПечати(КомандыПечати) Экспорт\n{body}\nКонецПроцедуры\n"


@pytest.fixture
def pf_main(tmp_path):
    res = _print_forms(tmp_path, _PF_MAIN)
    return res, {r["name"]: r for r in res["print_forms"]}


def _pres(row) -> tuple:
    return row["presentation"], row["presentation_source"]


def test_print_form_presentation_belongs_to_its_own_command(pf_main):
    _res, rows = pf_main
    assert _pres(rows["Первая"]) == (None, "computed")
    assert _pres(rows["Вторая"]) == ("Вторая форма", "literal")


def test_commented_out_command_is_not_a_row(pf_main):
    _res, rows = pf_main
    assert "Призрак" not in rows
    # Закомментированная строка целиком (начало блока + идентификатор после `;`)
    # без маски комментариев дала бы блок-призрак.
    assert "Призрак2" not in rows
    assert _pres(rows["Третья"]) == (None, "not_set")


def test_foreign_add_does_not_cut_the_command_block(pf_main):
    _res, rows = pf_main
    assert _pres(rows["Вторая"]) == ("Вторая форма", "literal")


def test_foreign_table_row_and_prefixed_helper_are_not_commands(pf_main):
    _res, rows = pf_main
    assert "Служебная" not in rows
    assert "НеКоманда" not in rows


def test_reassigned_variable_ends_the_block(pf_main):
    _res, rows = pf_main
    assert _pres(rows["Четвертая"]) == (None, "not_set")


def test_string_literal_does_not_cut_the_block(tmp_path):
    body = 'К = КомандыПечати.Добавить(); К.Идентификатор = "А"; Текст = "пример; К = X";\nК.Представление = "Верное";'
    rows = {r["name"]: r for r in _print_forms(tmp_path, _mini(body))["print_forms"]}
    assert _pres(rows["А"]) == ("Верное", "literal")


def test_comparison_is_not_a_property_assignment(tmp_path):
    body = (
        "К = КомандыПечати.Добавить();\n"
        'Если К.Идентификатор = "Ложный" Тогда Сообщить(1); КонецЕсли;\n'
        'К.Идентификатор = "Б";\n'
        'Если К.Представление = "Ложное" Тогда Сообщить(2); КонецЕсли;'
    )
    got = _print_forms(tmp_path, _mini(body))["print_forms"]
    assert [(r["name"], *_pres(r)) for r in got] == [("Б", None, "not_set")]


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        pytest.param(
            'К = КомандыПечати.Добавить(); К.Идентификатор = "А";\n'
            "Если Истина И\n"
            '    К.Представление = "Ложное" Тогда КонецЕсли;',
            [("А", None, "not_set")],
            id="a-condition-continues",
        ),
        pytest.param(
            "К = КомандыПечати.Добавить();\n"
            "Если Истина И\n"
            '    К.Идентификатор = "Ложный" Тогда КонецЕсли;\n'
            'К.Идентификатор = "Б";',
            [("Б", None, "not_set")],
            id="b-condition-id",
        ),
        pytest.param(
            'К = КомандыПечати.Добавить(); К.Идентификатор = "В";\n'
            "Если Истина И\n"
            "    К = Неопределено Тогда КонецЕсли;\n"
            'К.Представление = "Своё";',
            [("В", "Своё", "literal")],
            id="c-comparison-keeps-block",
        ),
        pytest.param(
            'К = КомандыПечати.Добавить(); К.Идентификатор = "Г";\n'
            "#Область Хвост\n"
            'К.Представление = "Своё";\n'
            "#КонецОбласти",
            [("Г", "Своё", "literal")],
            id="d-directive-line",
        ),
    ],
)
def test_line_break_inside_condition_is_not_a_statement_start(tmp_path, body, expected):
    got = _print_forms(tmp_path, _mini(body))["print_forms"]
    assert [(r["name"], *_pres(r)) for r in got] == expected


def test_helper_and_delegate_calls(pf_main):
    _res, rows = pf_main
    assert _pres(rows["Помощник"]) == ('Через "помощника"', "literal")
    assert _pres(rows["Делегат"]) == (None, "delegate")
    assert rows["Делегат"]["delegate"] == "Обработки.ПечатьЧего"


def test_list_delegation_is_named(pf_main):
    res, _rows = pf_main
    assert res["_meta"]["delegates"] == [
        "МодульЛокализация.ДобавитьКомандыПечати",
        "ВводОстатковЛокализация.ВводОстатковДобавитьКомандыПечати",
    ]


def test_every_row_carries_every_key(pf_main):
    res, _rows = pf_main
    for row in res["print_forms"]:
        assert set(row) == {"name", "presentation", "presentation_source", "delegate", "file"}, row
    assert [r["name"] for r in res["print_forms"]] == [
        "Первая",
        "Вторая",
        "Третья",
        "Четвертая",
        "Делегат",
        "Помощник",
        "Акт,Счет",
        "Литерал",
    ]


def test_list_identifier_and_ru_not_first(pf_main):
    _res, rows = pf_main
    assert _pres(rows["Акт,Счет"]) == ("Чек д'Артаньяна", "literal")


def test_english_nstr_is_a_literal(tmp_path):
    """`NStr` — английская форма той же функции; без неё представление ложно
    объявлялось вычисляемым (`computed`)."""
    body = 'К = КомандыПечати.Добавить(); К.Идентификатор = "А"; К.Представление = NStr("ru = \'Акт\'");'
    rows = {r["name"]: r for r in _print_forms(tmp_path, _mini(body))["print_forms"]}
    assert _pres(rows["А"]) == ("Акт", "literal")


_PF_NUMBER_FN = '\nФункция ПолучитьНомер()\n    Возврат "№1";\nКонецФункции\n'


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(
            'К = КомандыПечати.Добавить(); К.Идентификатор = "А";\nК.Представление = "Акт " + ПолучитьНомер();',
            id="literal-concat",
        ),
        pytest.param(
            'К = КомандыПечати.Добавить(); К.Идентификатор = "А";\n'
            'К.Представление = НСтр("ru = \'Акт\'") + " №" + ПолучитьНомер();',
            id="nstr-concat",
        ),
        pytest.param(
            'ДобавитьКомандуПечати(КомандыПечати, "А", "Акт " + ПолучитьНомер());',
            id="helper-literal-concat",
        ),
        pytest.param(
            'ДобавитьКомандуПечати(КомандыПечати, "А", НСтр("ru = \'Акт\'") + ПолучитьНомер());',
            id="helper-nstr-concat",
        ),
    ],
)
def test_literal_that_only_starts_the_expression_is_computed(tmp_path, body):
    """`"Акт " + ПолучитьНомер()` — выражение, и литерал в его НАЧАЛЕ значением не
    является: строка отдавала обрезок `"Акт "` с `literal` — часть значения под видом
    всего."""
    rows = {r["name"]: r for r in _print_forms(tmp_path, _mini(body) + _PF_NUMBER_FN)["print_forms"]}
    assert _pres(rows["А"]) == (None, "computed")


@pytest.mark.parametrize(
    "tail",
    [
        pytest.param('Если Истина Тогда\n    К.Представление = "Акт"\nКонецЕсли;', id="before-endif"),
        pytest.param('Если Истина Тогда К.Представление = "Акт" Иначе К.Порядок = 1; КонецЕсли;', id="before-else"),
        pytest.param(
            'Если Ложь Тогда К.Представление = "Акт" ИначеЕсли Истина Тогда К.Порядок = 1; КонецЕсли;',
            id="before-elsif",
        ),
        pytest.param('Для Каждого Х Из Список Цикл\n    К.Представление = "Акт"\nКонецЦикла;', id="before-enddo"),
        pytest.param('Попытка\n    К.Представление = "Акт"\nИсключение\nКонецПопытки;', id="before-except"),
        pytest.param('Попытка\nИсключение\n    К.Представление = "Акт"\nКонецПопытки;', id="before-endtry"),
        pytest.param("If True Then\n    К.Представление = НСтр(\"ru = 'Акт'\")\nEndIf;", id="before-english-endif"),
        pytest.param('К.Представление = "Акт"', id="before-end-procedure"),
        pytest.param('#Область Хвост\nК.Представление = "Акт"\n#КонецОбласти', id="directive-before-end"),
    ],
)
def test_literal_that_ends_the_statement_without_semicolon_is_a_literal(tmp_path, tail):
    """`;` в 1С разделяет инструкции и перед концом блока необязательна: литерал, за
    которым сразу идёт `КонецЕсли`, `Иначе`, `КонецПроцедуры` (строка препроцессора
    между ними не в счёт), — всё выражение, а не его начало."""
    body = 'К = КомандыПечати.Добавить(); К.Идентификатор = "А";\n' + tail
    rows = {r["name"]: r for r in _print_forms(tmp_path, _mini(body))["print_forms"]}
    assert _pres(rows["А"]) == ("Акт", "literal")


def test_doubled_quotes_are_decoded(pf_main):
    _res, rows = pf_main
    assert _pres(rows["Литерал"]) == ('Счет "Фактура"', "literal")


def test_analyze_document_flow_inherits_print_form_shape(tmp_path):
    root = tmp_path / "cfg"
    _write(root / "Configuration.xml", CF_DESCRIPTOR)
    _write(root / "Documents" / "Тест" / "Ext" / "ManagerModule.bsl", _PF_MAIN)
    flow = _bsl_for(root)["analyze_document_flow"]("Тест")
    rows = flow["print_forms"]["print_forms"]
    assert rows and all("presentation_source" in r for r in rows)
    assert "delegates" in flow["print_forms"]["_meta"]


# ═══════════════════════════ Задача 6 — что такое `loc` (Д11) ═══════════════════════════

_LOC_MODULE = """\
// Комментарий уровня модуля
Перем МодульнаяПеременная;

Процедура Первая() Экспорт
    // комментарий внутри метода

    А = 1;
КонецПроцедуры

// Комментарий между методами

Функция Вторая()
    Возврат 1;
КонецФункции

МодульнаяПеременная = 0;
"""


@pytest.mark.parametrize("with_index", [False, True], ids=["live", "index"])
def test_loc_is_the_sum_of_method_spans(tmp_path, monkeypatch, with_index):
    root = tmp_path / "cfg"
    _write(root / "Configuration.xml", CF_DESCRIPTOR)
    path = "CommonModules/Счетчик/Ext/Module.bsl"
    _write(root / path, _LOC_MODULE)
    reader = _build_reader(root, monkeypatch) if with_index else None
    try:
        bsl = _bsl_for(root, reader)
        outline = bsl["get_module_outline"](path)
        assert outline["_meta"]["index_used"] is with_index, outline["_meta"]
        spans = sum(p["end_line"] - p["line"] + 1 for p in bsl["extract_procedures"](path))
        assert outline["totals"]["loc"] == spans == 8
        assert outline["totals"]["loc"] < bsl["code_metrics"](path)["total_lines"]
    finally:
        if reader is not None:
            reader.close()


def test_loc_definition_is_in_both_recipes():
    from rlm_tools_bsl.bsl_helpers import build_helper_metadata_snapshot

    snapshot = build_helper_metadata_snapshot()
    for name in ("get_module_outline", "get_object_modules"):
        recipe = snapshot[name]["recipe"]
        assert "СУММА строк МЕТОДОВ" in recipe, name
        assert "code_metrics" in recipe, name


def test_loc_definition_is_in_both_sigs():
    """Подпись уходит агенту на КАЖДОМ старте, рецепт — только по `rlm_help`: агент
    приёмки, не открывший справку, принял `loc` за число строк файла."""
    from rlm_tools_bsl.bsl_helpers import build_helper_metadata_snapshot

    snapshot = build_helper_metadata_snapshot()
    for name in ("get_module_outline", "get_object_modules"):
        assert "loc=Σ строк методов" in snapshot[name]["sig"], name


# ═══════════════ Задача 7 — нудж get_index_info перестаёт себе противоречить (Д12) ═══════════════


def _get_index_info_nudge_texts() -> dict[str, str]:
    from rlm_tools_bsl.bsl_knowledge import _STRATEGY_HEADER, _render_index_block
    from rlm_tools_bsl.bsl_strategy_data import STRATEGY_SECTIONS
    from rlm_tools_bsl.sandbox import HelperCall, Sandbox

    def batching_line(text: str) -> str:
        return next(ln for ln in text.splitlines() if "Не зови get_index_info на старте" in ln)

    block = _render_index_block({"builder_version": 16, "methods": 10, "calls": 5}, [])
    index_line = next(ln for ln in block.splitlines() if "get_index_info()" in ln)
    sb = Sandbox(base_path=".")
    sb._helper_calls = [HelperCall("get_index_info", 0.0, seq=1)]
    hint = next(h for h in sb._compute_efficiency_hints() if h["id"] == "redundant_get_index_info")
    return {
        "batching_slim": batching_line(STRATEGY_SECTIONS["batching"]),
        "batching_full": batching_line(_STRATEGY_HEADER),
        "index_block": index_line,
        "hint": hint["message"],
    }


def test_get_index_info_nudge_names_where_the_override_count_lives():
    texts = _get_index_info_nudge_texts()
    assert texts["batching_slim"] == texts["batching_full"]
    for where, text in texts.items():
        assert "get_overrides()['total']" in text, where
        assert "нужен лишь" not in text, where  # extension_overrides больше не причина звать
        assert "nearby_extensions" not in text, where  # сумма по списку тихо недосчитала бы


def test_extensions_recipe_does_not_route_to_get_index_info():
    from rlm_tools_bsl.bsl_knowledge import _BUSINESS_RECIPES

    recipe = _BUSINESS_RECIPES["расширения"]
    for form in ("compact", "full"):
        assert not any("get_index_info" in line for line in recipe[form]), form


# ═══════════════════ Задача 9 — общий бюджет потоков обхода дерева (Д1) ═══════════════════


@pytest.fixture
def fresh_ledger(monkeypatch):
    """Свежий журнал на тест: незакрытые backend-ы других тестов могли бы исчерпать
    пул из 256 слотов, и выдача 0 покрасила бы тест в зависимости от порядка запуска."""
    from rlm_tools_bsl import _scan_budget

    ledger = _scan_budget.ScanLedger()
    monkeypatch.setattr(_scan_budget, "_LEDGER", ledger)
    return ledger


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, 16), ("", 16), ("0", 0), ("-1", 16), ("abc", 16), ("999", 512), ("7", 7)],
)
def test_scan_workers_total_parsing(monkeypatch, raw, expected):
    from rlm_tools_bsl._scan_budget import scan_workers_total

    if raw is None:
        monkeypatch.delenv("RLM_SCAN_WORKERS_TOTAL", raising=False)
    else:
        monkeypatch.setenv("RLM_SCAN_WORKERS_TOTAL", raw)
    assert scan_workers_total() == expected


def test_scan_leases_share_one_ledger(fresh_ledger):
    from rlm_tools_bsl._scan_budget import ScanLease

    a = ScanLease(fresh_ledger.array, 1, 5)
    b = ScanLease(fresh_ledger.array, 2, 5)
    assert a.acquire(3) == 3
    assert b.acquire(3) == 2
    a.release(3)
    assert b.acquire(3) == 3
    b.release(5)
    # Одна аренда на пустом журнале при want больше бюджета получает РОВНО бюджет —
    # на этом держится оценка худшего случая строго одновременного старта.
    c = ScanLease(fresh_ledger.array, 3, 5)
    assert c.acquire(31) == 5


class _SimultaneousJournal:
    """Прокси журнала: чтение суммы ждёт, пока ВСЕ участники опубликуют, снимает
    снимок и ждёт второй раз, пока все снимут снимок, — только потом кто-либо
    уменьшает свою заявку. Два барьера: одного недостаточно, быстрый участник успел
    бы отступить до того, как медленный прочитал сумму."""

    def __init__(self, parties: int, size: int = 8):
        self._data = [0] * size
        self._published = threading.Barrier(parties, timeout=10)
        self._snapshotted = threading.Barrier(parties, timeout=10)

    def __getitem__(self, i):
        return self._data[i]

    def __setitem__(self, i, v):
        self._data[i] = v

    def __iter__(self):
        self._published.wait()
        snapshot = list(self._data)
        self._snapshotted.wait()
        return iter(snapshot)


@pytest.mark.parametrize(("parties", "minimum"), [(2, 4), (3, 2)])
def test_simultaneous_claims_use_the_whole_budget(parties, minimum):
    from rlm_tools_bsl._scan_budget import ScanLease

    journal = _SimultaneousJournal(parties)
    leases = [ScanLease(journal, slot, 4) for slot in range(1, parties + 1)]
    granted = [0] * parties

    def claim(i):
        granted[i] = leases[i].acquire(3)

    threads = [threading.Thread(target=claim, args=(i,)) for i in range(parties)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert sum(granted) >= minimum, granted
    # Перебор — только в объявленной границе: каждая заявка не больше min(W−1, бюджет).
    assert all(g <= 3 for g in granted), granted


def _scan_tree(root):
    for i in range(3):
        _write(root / "CommonModules" / f"М{i}" / "Ext" / "Module.bsl", "Процедура П() Экспорт\nКонецПроцедуры\n")
    return root


def test_zero_grant_takes_the_serial_path(tmp_path, monkeypatch, fresh_ledger):
    from rlm_tools_bsl import helpers
    from rlm_tools_bsl._scan_budget import ScanLease

    root = _scan_tree(tmp_path / "cfg")
    monkeypatch.setenv("RLM_SCAN_WORKERS", "4")
    expected = sorted(helpers.scan_bsl_tree(root)[0])
    calls: list[str] = []
    real_serial = helpers._scan_bsl_tree_serial

    def spy(r):
        calls.append("serial")
        return real_serial(r)

    monkeypatch.setattr(helpers, "_scan_bsl_tree_serial", spy)
    lease = ScanLease(fresh_ledger.array, 1, 0)  # бюджет 0 — выдача 0
    paths, errors = helpers.scan_bsl_tree(root, lease=lease)
    assert calls == ["serial"]
    assert sorted(paths) == expected and errors == 0


def test_grant_caps_spawned_threads(tmp_path, monkeypatch, fresh_ledger):
    from rlm_tools_bsl import helpers
    from rlm_tools_bsl._scan_budget import ScanLease

    root = _scan_tree(tmp_path / "cfg")
    monkeypatch.setenv("RLM_SCAN_WORKERS", "4")
    started: list[str] = []
    real_start = threading.Thread.start

    def spy_start(self):
        if self.name.startswith("rlm-scan-bsl-"):
            started.append(self.name)
        return real_start(self)

    monkeypatch.setattr(threading.Thread, "start", spy_start)
    lease = ScanLease(fresh_ledger.array, 1, 1)  # выдано 1 из 3
    helpers.scan_bsl_tree(root, lease=lease)
    assert len(started) == 1, started
    assert fresh_ledger.array[1] == 0, "после обхода удержание снято"


def test_lease_released_when_walk_raises(tmp_path, monkeypatch, fresh_ledger):
    from rlm_tools_bsl import helpers
    from rlm_tools_bsl._scan_budget import ScanLease

    monkeypatch.setenv("RLM_SCAN_WORKERS", "4")

    def boom(root, workers):
        raise RuntimeError("walk failed")

    monkeypatch.setattr(helpers, "_scan_bsl_tree_parallel", boom)
    lease = ScanLease(fresh_ledger.array, 1, 16)
    with pytest.raises(RuntimeError, match="walk failed"):
        helpers.scan_bsl_tree(tmp_path, lease=lease)
    assert fresh_ledger.array[1] == 0


def _inline_backend(root):
    from rlm_tools_bsl.server import _create_session_backend

    backend, _owned = _create_session_backend(
        sandbox_mode="inline",
        resolved=str(root),
        session=types.SimpleNamespace(max_llm_calls=50, llm_calls_used=0, session_id="t"),
        max_output_chars=10_000,
        execution_timeout_seconds=45,
        format_info=None,
        idx_reader=None,
        db_path=None,
        callers_authoritative=False,
        ext_paths_for_sandbox=[],
        registry_epoch=0,
    )
    return backend


def test_inline_sessions_share_one_permanent_slot(tmp_path, fresh_ledger):
    import time as _time

    from rlm_tools_bsl._scan_budget import INLINE_SLOT, LEDGER_SLOTS

    backends = [_inline_backend(tmp_path) for _ in range(2)]
    try:
        lease = fresh_ledger.inline_lease()
        assert all(b._sandbox._scan_lease is lease for b in backends)
    finally:
        for b in backends:
            b.request_close("test")
            b.finish_close(_time.monotonic() + 5)
    slots = [fresh_ledger.allocate() for _ in range(LEDGER_SLOTS - 1)]
    assert None not in slots and INLINE_SLOT not in slots
    assert len(set(slots)) == LEDGER_SLOTS - 1
    assert fresh_ledger.allocate() is None


def test_inline_forced_close_keeps_the_walk_counted(tmp_path, monkeypatch, fresh_ledger):
    """Отцепленный на исчерпанном deadline прогрев продолжает работать — его потоки
    обязаны оставаться в сумме, пока его собственный `finally` их не вернёт."""
    import time as _time

    from rlm_tools_bsl import helpers
    from rlm_tools_bsl._scan_budget import INLINE_SLOT
    from rlm_tools_bsl.sandbox import Sandbox
    from rlm_tools_bsl.sandbox_backend import InlineSandboxBackend

    monkeypatch.setenv("RLM_SCAN_WORKERS", "4")
    monkeypatch.setenv("RLM_SCAN_WORKERS_TOTAL", "4")
    entered = threading.Event()
    release = threading.Event()

    def blocked_parallel(root, workers):
        entered.set()
        release.wait(timeout=30)
        return [], 0

    monkeypatch.setattr(helpers, "_scan_bsl_tree_parallel", blocked_parallel)
    sandbox = Sandbox(base_path=str(tmp_path), max_output_chars=10_000)
    prewarm = threading.Thread(
        target=lambda: helpers.scan_bsl_tree(tmp_path, lease=fresh_ledger.inline_lease()), daemon=True
    )
    sandbox._prewarm_thread = prewarm
    backend = InlineSandboxBackend(sandbox, None, install_llm_tools=False)
    try:
        prewarm.start()
        assert entered.wait(timeout=5)
        assert fresh_ledger.array[INLINE_SLOT] == 3
        backend.request_close("test")
        report = backend.finish_close(_time.monotonic() - 1.0)
        assert report.closed is True and report.forced is True
        assert prewarm.is_alive()
        assert fresh_ledger.array[INLINE_SLOT] == 3, "закрытие не вернуло потоки идущего обхода"
        second = fresh_ledger.inline_lease()
        assert second.acquire(3) == 1
        second.release(1)
    finally:
        release.set()
        prewarm.join(timeout=5)
    assert fresh_ledger.array[INLINE_SLOT] == 0


def _raw_process_config(tmp_path):
    from rlm_tools_bsl.sandbox_process import ProcessBackendConfig

    return ProcessBackendConfig(base_path=str(tmp_path), memory_mb=0)


def test_untracked_process_backend_start_failure_returns_the_slot(tmp_path, monkeypatch, fresh_ledger):
    from rlm_tools_bsl.sandbox_backend import SandboxStartupError
    from rlm_tools_bsl.sandbox_process import ProcessSandboxBackend

    def failing_start(self):
        raise SandboxStartupError("boom")

    monkeypatch.setattr(ProcessSandboxBackend, "_start_worker", failing_start)
    before = len(fresh_ledger._free)
    with pytest.raises(SandboxStartupError):
        ProcessSandboxBackend(_raw_process_config(tmp_path))
    assert len(fresh_ledger._free) == before, "неотслеживаемый backend вернул слот сам"


def test_tracked_process_backend_start_failure_slot_returns_via_finalization(tmp_path, monkeypatch, fresh_ledger):
    import time as _time

    from rlm_tools_bsl.sandbox_backend import SandboxStartupError
    from rlm_tools_bsl.sandbox_process import ProcessSandboxBackend

    def failing_start(self):
        raise SandboxStartupError("boom")

    monkeypatch.setattr(ProcessSandboxBackend, "_start_worker", failing_start)
    before = len(fresh_ledger._free)
    captured = []
    with pytest.raises(SandboxStartupError):
        ProcessSandboxBackend(
            _raw_process_config(tmp_path),
            startup_register=lambda b: captured.append(b) or True,
            startup_unregister=lambda b: None,
        )
    backend = captured[0]
    assert len(fresh_ledger._free) == before - 1, "отслеживаемый: слот вернёт финализация, а не конструктор"
    backend.request_close("test")
    assert backend.finish_close(_time.monotonic() + 5).closed is True
    assert len(fresh_ledger._free) == before
    backend._release_scan_slot()  # повторное освобождение — ничего не делает
    assert len(fresh_ledger._free) == before
