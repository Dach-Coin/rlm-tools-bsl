"""v1.44.0 — ответ не выдумывает и не режет молча.

Релиз read-time: схема индекса и BUILDER_VERSION не тронуты. Приватные фикстуры других
файлов не импортируются (каталог tests не пакет) — деревья собираются здесь же.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import rlm_tools_bsl.bsl_helpers as bsl_helpers_module
from rlm_tools_bsl.bsl_helpers import make_bsl_helpers
from rlm_tools_bsl.bsl_index import IndexBuilder, IndexReader
from rlm_tools_bsl.bsl_xml_parsers import parse_metadata_xml
from rlm_tools_bsl.extension_detector import ConfigRole, detect_extension_context
from rlm_tools_bsl.format_detector import detect_format
from rlm_tools_bsl.helpers import make_helpers
from rlm_tools_bsl.sandbox import Sandbox

CF_DESCRIPTOR = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">\n'
    '  <Configuration uuid="00000000-0000-0000-0000-000000000001">\n'
    "    <Properties><Name>Тест</Name></Properties>\n"
    "  </Configuration>\n"
    "</MetaDataObject>\n"
)
CF_MAIN_DESCRIPTOR = textwrap.dedent("""\
    <?xml version="1.0" encoding="UTF-8"?>
    <MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses" xmlns:v8="http://v8.1c.ru/8.1/data/core">
        <Configuration uuid="00000000-0000-0000-0000-000000000001">
            <Properties>
                <Name>ОсновнаяКонфигурация</Name>
                <NamePrefix/>
                <ConfigurationExtensionCompatibilityMode>Version8_3_24</ConfigurationExtensionCompatibilityMode>
            </Properties>
        </Configuration>
    </MetaDataObject>
""")
CF_EXT_DESCRIPTOR = textwrap.dedent("""\
    <?xml version="1.0" encoding="UTF-8"?>
    <MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses" xmlns:v8="http://v8.1c.ru/8.1/data/core">
        <Configuration uuid="00000000-0000-0000-0000-000000000002">
            <Properties>
                <ObjectBelonging>Adopted</ObjectBelonging>
                <Name>Доработки</Name>
                <ConfigurationExtensionPurpose>Customization</ConfigurationExtensionPurpose>
                <NamePrefix>др_</NamePrefix>
            </Properties>
        </Configuration>
    </MetaDataObject>
""")
EDT_DESCRIPTOR = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<mdclass:Configuration xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass" name="Тест"/>\n'
)
CF_FORM_ATTR = (
    '<Form xmlns="http://v8.1c.ru/8.3/xcf/logform" xmlns:v8="http://v8.1c.ru/8.1/data/core">'
    '<Attributes><Attribute name="Список"><Type><v8:Type>cfg:DynamicList</v8:Type></Type>'
    "<MainAttribute>true</MainAttribute></Attribute></Attributes></Form>"
)
EDT_FORM_ATTR = (
    '<form:Form xmlns:form="http://g5.1c.ru/v8/dt/form"><form:attributes><name>Список</name>'
    "<main>true</main><form:valueType><types>DynamicList</types></form:valueType>"
    "</form:attributes></form:Form>"
)
PROC = "Процедура П()\nКонецПроцедуры\n"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _bsl_for(base_path, idx_reader=None, glob_wrap=None, **kw):
    """Хелперы как у песочницы: glob_files — ПРОИЗВОДСТВЕННЫЙ (с индексом отдаёт подсказки)."""
    helpers, resolve_safe = make_helpers(str(base_path), idx_reader)
    glob_fn = helpers["glob_files"] if glob_wrap is None else glob_wrap(helpers["glob_files"])
    return make_bsl_helpers(
        base_path=str(base_path),
        resolve_safe=resolve_safe,
        read_file_fn=helpers["read_file"],
        grep_fn=helpers["grep"],
        glob_files_fn=glob_fn,
        format_info=detect_format(str(base_path)),
        idx_reader=idx_reader,
        **kw,
    )


def _reader_for(root: Path, monkeypatch, **build_kw) -> IndexReader:
    monkeypatch.setenv("RLM_INDEX_DIR", str(root.parent / (root.name + "_idx")))
    return IndexReader(IndexBuilder().build(str(root), **build_kw))


@pytest.fixture(params=[False, True], ids=["live", "index"])
def with_index(request):
    return request.param


# ═══════════════ Задача 1 (Д1) — подсказка glob не файл ═══════════════


@pytest.fixture(params=["cf", "edt"])
def forms_tree(request, tmp_path):
    """У ФормаСписка модуля нет, у ФормаЭлемента — есть; обе раскладки."""
    fmt = request.param
    root = tmp_path / fmt
    if fmt == "cf":
        _write(root / "Configuration.xml", CF_DESCRIPTOR)
        forms = root / "Catalogs" / "Склады" / "Forms"
        _write(forms / "ФормаСписка" / "Ext" / "Form.xml", CF_FORM_ATTR)
        _write(forms / "ФормаЭлемента" / "Ext" / "Form.xml", CF_FORM_ATTR)
        _write(forms / "ФормаЭлемента" / "Ext" / "Form" / "Module.bsl", PROC)
        _write(root / "Catalogs" / "Склады" / "Ext" / "ObjectModule.bsl", PROC)
        missing = "Catalogs/Склады/Forms/ФормаСписка/Ext/Form/Module.bsl"
    else:
        _write(root / "Configuration" / "Configuration.mdo", EDT_DESCRIPTOR)
        forms = root / "Catalogs" / "Склады" / "Forms"
        _write(forms / "ФормаСписка" / "Form.form", EDT_FORM_ATTR)
        _write(forms / "ФормаЭлемента" / "Form.form", EDT_FORM_ATTR)
        _write(forms / "ФормаЭлемента" / "Module.bsl", PROC)
        _write(root / "Catalogs" / "Склады" / "ObjectModule.bsl", PROC)
        missing = "Catalogs/Склады/Forms/ФормаСписка/Module.bsl"
    return root, missing


def _index_for_forms(root, monkeypatch):
    return _reader_for(root, monkeypatch, build_calls=False, build_metadata=True, build_fts=False)


def test_form_without_module_has_empty_module_path_on_both_branches(forms_tree, monkeypatch):
    root, _missing = forms_tree
    reader = _index_for_forms(root, monkeypatch)
    try:
        for bsl in (_bsl_for(root, reader), _bsl_for(root)):
            forms = {f["form_name"]: f for f in bsl["parse_form"]("Склады")}
            assert forms["ФормаСписка"]["module_path"] == ""
            assert forms["ФормаЭлемента"]["module_path"].endswith("Module.bsl")
            # Совет рецепта «события формы» исполним: модуль берётся только у формы с модулем.
            for f in forms.values():
                if f["module_path"]:
                    bsl["extract_procedures"](f["module_path"])
    finally:
        reader.close()


def test_unknown_name_on_index_is_an_empty_list(forms_tree, monkeypatch):
    root, _missing = forms_tree
    reader = _index_for_forms(root, monkeypatch)
    try:
        assert _bsl_for(root, reader)["parse_form"]("НетТакогоОбъекта") == []
    finally:
        reader.close()


@pytest.mark.parametrize("with_metadata", [True, False], ids=["form_elements", "no_metadata"])
def test_index_session_does_not_walk_the_whole_tree(forms_tree, monkeypatch, with_metadata):
    """Широкий обход `**/Forms/*` в сессии с индексом раньше не исполнялся (его перекрывало
    падение на подсказке) и стоил бы 8–15 с на ERP — он не должен появиться. В том числе на
    индексе без таблицы форм (`build_metadata=False` → get_form_elements отдаёт None)."""
    root, _missing = forms_tree
    reader = _reader_for(root, monkeypatch, build_calls=False, build_metadata=with_metadata, build_fts=False)
    seen: list[str] = []

    def spy(glob_fn):
        def _g(pattern):
            seen.append(pattern)
            return glob_fn(pattern)

        return _g

    try:
        assert _bsl_for(root, reader, glob_wrap=spy)["parse_form"]("НетТакогоОбъекта") == []
        assert not [p for p in seen if p.startswith("**/Forms/")], seen
    finally:
        reader.close()


def test_live_session_keeps_the_fragment_fallback(forms_tree):
    """Страж: без индекса широкий отбор подстрокой остаётся (на v1.43.0 тоже зелёный)."""
    root, _missing = forms_tree
    names = {f["form_name"] for f in _bsl_for(root)["parse_form"]("Склад")}
    assert names == {"ФормаСписка", "ФормаЭлемента"}


def test_missing_module_error_names_the_relative_path_and_the_route(forms_tree):
    root, missing = forms_tree
    with pytest.raises(FileNotFoundError) as ei:
        _bsl_for(root)["extract_procedures"](missing)
    msg = str(ei.value)
    assert missing in msg and "find_module" in msg
    assert str(root) not in msg


def test_exact_glob_calls_go_through_the_hint_filter():
    """Растяжка класса: вызов glob_files_fn с маской без `*` — только внутри _glob_hits."""
    tree = ast.parse(inspect.getsource(bsl_helpers_module))
    offenders: list[tuple[str, int]] = []

    class _Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.stack: list[str] = []

        def visit_FunctionDef(self, node):
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Call(self, node):
            if isinstance(node.func, ast.Name) and node.func.id == "glob_files_fn":
                arg = node.args[0] if node.args else None
                literal = None
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    literal = arg.value
                elif isinstance(arg, ast.JoinedStr):
                    literal = "".join(v.value for v in arg.values if isinstance(v, ast.Constant))
                owner = self.stack[-1] if self.stack else "<module>"
                if not (literal and "*" in literal) and owner != "_glob_hits":
                    offenders.append((owner, node.lineno))
            self.generic_visit(node)

    _Visitor().visit(tree)
    assert offenders == [], offenders


def test_hint_predicate_lives_in_one_place():
    """Растяжка класса: литерал "[hint:" в bsl_helpers — ровно один, в _without_glob_hints
    (прежде — три копии: _real_hits, _valid_files с более широким "[", count_matches)."""
    tree = ast.parse(inspect.getsource(bsl_helpers_module))
    owners = []

    class _Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.stack: list[str] = []

        def visit_FunctionDef(self, node):
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        def visit_Constant(self, node):
            if node.value == "[hint:":
                owners.append(self.stack[-1] if self.stack else "<module>")

    _Visitor().visit(tree)
    assert owners == ["_without_glob_hints"], owners


# ═══════════════ Задача 2 (Д2) — parse_form и префикс типа ═══════════════


@pytest.fixture
def homonym_forms(tmp_path):
    """Группы — справочник с двумя формами и общая форма; БезФорм — справочник без форм и общая форма."""
    root = tmp_path / "cf"
    _write(root / "Configuration.xml", CF_DESCRIPTOR)
    for rel in ("Catalogs/Группы/Forms/ФормаСписка", "Catalogs/Группы/Forms/ФормаЭлемента", "CommonForms/Группы"):
        _write(root / rel / "Ext" / "Form.xml", CF_FORM_ATTR)
        _write(root / rel / "Ext" / "Form" / "Module.bsl", PROC)
    _write(root / "Catalogs" / "БезФорм" / "Ext" / "ObjectModule.bsl", PROC)
    _write(root / "CommonForms" / "БезФорм" / "Ext" / "Form.xml", CF_FORM_ATTR)
    _write(root / "CommonForms" / "БезФорм" / "Ext" / "Form" / "Module.bsl", PROC)
    return root


def _forms_bsl(root, monkeypatch, with_index):
    reader = _index_for_forms(root, monkeypatch) if with_index else None
    return _bsl_for(root, reader), reader


_CATALOG_FORMS = {("Catalogs", "ФормаСписка"), ("Catalogs", "ФормаЭлемента")}
_COMMON_FORM = {("CommonForms", "Группы")}


@pytest.mark.parametrize(
    "arg, expected",
    [
        ("Группы", _CATALOG_FORMS | _COMMON_FORM),
        ("Справочник.Группы", _CATALOG_FORMS),
        ("справочник.Группы", _CATALOG_FORMS),
        ("Catalog.Группы", _CATALOG_FORMS),
        ("Catalogs/Группы", _CATALOG_FORMS),
        ("Справочники/Группы", _CATALOG_FORMS),
        ("ОбщаяФорма.Группы", _COMMON_FORM),
        ("CommonForm.Группы", _COMMON_FORM),
    ],
)
def test_parse_form_type_prefix_is_a_category(homonym_forms, monkeypatch, with_index, arg, expected):
    """Префикс типа и путь `Категория/Имя` — категория. Страж: голое `Группы` (обе ветки) и
    `Catalogs/Группы` на живой ветке зелёные и на v1.43.0; остальные варианты — RED на v1.43.0."""
    bsl, reader = _forms_bsl(homonym_forms, monkeypatch, with_index)
    try:
        assert {(f["category"], f["form_name"]) for f in bsl["parse_form"](arg)} == expected
    finally:
        if reader:
            reader.close()


def test_typed_name_absent_in_its_category_names_where_it_is(homonym_forms, monkeypatch, with_index):
    bsl, reader = _forms_bsl(homonym_forms, monkeypatch, with_index)
    try:
        with pytest.raises(ValueError, match="Documents") as ei:
            bsl["parse_form"]("Документ.Группы")
        assert "Catalogs" in str(ei.value) and "CommonForms" in str(ei.value)
    finally:
        if reader:
            reader.close()


def test_typed_object_without_forms_is_an_honest_empty(homonym_forms, monkeypatch, with_index):
    bsl, reader = _forms_bsl(homonym_forms, monkeypatch, with_index)
    try:
        assert bsl["parse_form"]("Справочник.БезФорм") == []
    finally:
        if reader:
            reader.close()


@pytest.fixture(params=["cf", "edt"])
def lone_common_form(request, tmp_path):
    """ОднаФорма — только общая форма, и у неё нет модуля; обе раскладки."""
    fmt = request.param
    root = tmp_path / fmt
    if fmt == "cf":
        _write(root / "Configuration.xml", CF_DESCRIPTOR)
        _write(root / "CommonForms" / "ОднаФорма" / "Ext" / "Form.xml", CF_FORM_ATTR)
        _write(root / "Catalogs" / "Склады" / "Ext" / "ObjectModule.bsl", PROC)
    else:
        _write(root / "Configuration" / "Configuration.mdo", EDT_DESCRIPTOR)
        _write(root / "CommonForms" / "ОднаФорма" / "Form.form", EDT_FORM_ATTR)
        _write(root / "Catalogs" / "Склады" / "ObjectModule.bsl", PROC)
    return root


def test_common_form_without_module_is_named_in_the_refusal(lone_common_form, monkeypatch, with_index):
    """Общей формы без модуля нет ни в модулях сессии, ни в каскаде метаданных (CommonForms в его
    перечне нет); отказ на чужом префиксе всё равно называет CommonForms, а не отвечает пустым списком."""
    bsl, reader = _forms_bsl(lone_common_form, monkeypatch, with_index)
    try:
        with pytest.raises(ValueError, match="CommonForms"):
            bsl["parse_form"]("Документ.ОднаФорма")
        assert {(f["category"], f["form_name"]) for f in bsl["parse_form"]("ОбщаяФорма.ОднаФорма")} == {
            ("CommonForms", "ОднаФорма")
        }
    finally:
        if reader:
            reader.close()


def test_parse_form_none_and_list_are_clear_errors(homonym_forms):
    bsl = _bsl_for(homonym_forms)
    with pytest.raises(ValueError, match="object_name is required"):
        bsl["parse_form"](None)
    with pytest.raises(TypeError, match="строка"):
        bsl["parse_form"](["Группы"])


# ═══════════════ Задача 3 (Д3) — формы и команды в описателе ═══════════════

# Раскладка — как в боевом .mdo: перевод строки и отступ ВНУТРИ <forms>/<commands> перед <name>.
# Именно этот пробельный текст узла прежний разбор брал за имя; компактная запись без
# пробелов дефект не воспроизводит (узел без текста ветка разбора пропускала вовсе).
_EDT_DOC_FORMS = textwrap.dedent("""\
    <?xml version="1.0" encoding="UTF-8"?>
    <mdclass:Document xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass" uuid="0">
      <name>Заказ</name>
      <forms uuid="1">
        <name>ФормаДокумента</name>
        <synonym>
          <key>ru</key>
          <value>Форма документа</value>
        </synonym>
      </forms>
      <forms uuid="2">
        <name>ФормаСписка</name>
      </forms>
      <commands uuid="3">
        <name>Печать</name>
        <group>FormCommandBarImportant</group>
      </commands>
    </mdclass:Document>
""")
_CF_DOC_FORMS = (
    '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"><Document><Properties><Name>Заказ</Name></Properties>'
    "<ChildObjects><Form>ФормаДокумента</Form><Form>ФормаСписка</Form>"
    '<Command uuid="3"><Properties><Name>Печать</Name></Properties></Command></ChildObjects>'
    "</Document></MetaDataObject>"
)


@pytest.mark.parametrize("xml", [_EDT_DOC_FORMS, _CF_DOC_FORMS], ids=["edt", "cf"])
def test_metadata_forms_and_commands_are_names(xml):
    parsed = parse_metadata_xml(xml)
    assert parsed["forms"] == ["ФормаДокумента", "ФормаСписка"]
    assert parsed["commands"] == ["Печать"]


@pytest.mark.parametrize("fmt", ["cf", "edt"])
def test_full_structure_lists_form_names_without_index(tmp_path, fmt):
    root = tmp_path / fmt
    if fmt == "cf":
        _write(root / "Configuration.xml", CF_DESCRIPTOR)
        _write(root / "Documents" / "Заказ.xml", _CF_DOC_FORMS)
        _write(root / "Documents" / "Заказ" / "Ext" / "ObjectModule.bsl", PROC)
    else:
        _write(root / "Configuration" / "Configuration.mdo", EDT_DESCRIPTOR)
        _write(root / "Documents" / "Заказ" / "Заказ.mdo", _EDT_DOC_FORMS)
        _write(root / "Documents" / "Заказ" / "ObjectModule.bsl", PROC)
    bsl = _bsl_for(root)
    assert bsl["get_object_full_structure"]("Документ.Заказ")["forms"] == ["ФормаДокумента", "ФормаСписка"]
    assert bsl["parse_object_xml"]("Documents/Заказ")["commands"] == ["Печать"]


# ═══════════════ Задача 4 (Д4) — вложенность по коду ═══════════════


def test_max_nesting_counts_only_code(tmp_path):
    root = tmp_path / "cf"
    _write(root / "Configuration.xml", CF_DESCRIPTOR)
    body = (
        "#Если Сервер Или ТолстыйКлиентОбычноеПриложение Тогда\n"
        "Процедура Тест()\n"
        '\tЗапрос = Новый Запрос("ВЫБРАТЬ * ИЗ Справочник.Х ДЛЯ ИЗМЕНЕНИЯ");\n'
        '\tТекст = "Если бы да кабы";  // Если в хвостовом комментарии\n'
        '\tЗапрос.Текст = "ВЫБРАТЬ\n'
        "\t|\tТ.Ссылка ДЛЯ ИЗМЕНЕНИЯ\n"
        '\t|ИЗ Справочник.Х КАК Т";\n'
        "\tЕсли А Тогда\n"
        "\t\t#Если Клиент Тогда\n"
        "\t\tПока Б Цикл\n"
        "\t\tКонецЦикла;\n"
        "\t\t#КонецЕсли\n"
        "\tИначеЕсли В Тогда\n"
        "\tКонецЕсли;\n"
        "КонецПроцедуры\n"
        "#КонецЕсли\n"
    )
    _write(root / "CommonModules" / "М" / "Ext" / "Module.bsl", body)
    metrics = _bsl_for(root)["code_metrics"]("CommonModules/М/Ext/Module.bsl")
    assert metrics["max_nesting"] == 2  # Если + Пока; литералы, комментарий и #Если — не уровни
    assert metrics["total_lines"] == 16 and metrics["procedures_count"] == 1


# ═══════════════ Задача 5 (Д5) — виды таблиц запроса ═══════════════


def _queries(tmp_path, body: str) -> list[dict]:
    root = tmp_path / "cf"
    _write(root / "Configuration.xml", CF_DESCRIPTOR)
    _write(root / "CommonModules" / "Запросы" / "Ext" / "Module.bsl", body)
    return _bsl_for(root)["extract_queries"]("CommonModules/Запросы/Ext/Module.bsl")


@pytest.mark.parametrize(
    "source, table",
    [
        ("ПланВидовХарактеристик.ВидыСубконто", "ПланВидовХарактеристик.ВидыСубконто"),
        ("ПланСчетов.Хозрасчетный.ВидыСубконто", "ПланСчетов.Хозрасчетный"),
        ("ПланВидовРасчета.Начисления", "ПланВидовРасчета.Начисления"),
        ("РегистрРасчета.Начисления", "РегистрРасчета.Начисления"),
        ("Константа.ВалютаУчета", "Константа.ВалютаУчета"),
        ("Перечисление.СтатусыЗаказов", "Перечисление.СтатусыЗаказов"),
        ("ЖурналДокументов.Взаимодействия", "ЖурналДокументов.Взаимодействия"),
        ("БизнесПроцесс.Задание", "БизнесПроцесс.Задание"),
        ("Задача.ЗадачаИсполнителя", "Задача.ЗадачаИсполнителя"),
        ("ПланОбмена.Полный", "ПланОбмена.Полный"),
        ("Последовательность.Взаиморасчеты.Границы", "Последовательность.Взаиморасчеты"),
        ("КритерийОтбора.Связи", "КритерийОтбора.Связи"),
        ("ВнешнийИсточникДанных.Склад.Таблица.Остатки", "ВнешнийИсточникДанных.Склад"),
        ("ChartOfAccounts.Main", "ChartOfAccounts.Main"),
        ("Task.Performer", "Task.Performer"),
        ("Sequence.Settlements", "Sequence.Settlements"),
    ],
)
def test_query_tables_cover_every_source_kind(tmp_path, source, table):
    q = _queries(tmp_path, f'Процедура П()\n\tЗапрос.Текст = "ВЫБРАТЬ * ИЗ {source} КАК Т";\nКонецПроцедуры\n')
    assert q[0]["tables"] == [table]


def test_query_tables_follow_the_shared_metadata_forms(tmp_path):
    """Растяжка единого источника: каждый вид из _RU_META_FORMS (кроме отчётов и обработок —
    они не таблицы запроса) распознаётся в обоих языках."""
    from rlm_tools_bsl.bsl_xml_parsers import _RU_META_FORMS

    heads = [
        forms[key]
        for canon, forms in _RU_META_FORMS.items()
        if canon not in ("Report.", "DataProcessor.")
        for key in ("ru_singular", "en_singular")
    ]
    body = (
        "Процедура П()\n"
        + "".join(f'\tЗапрос{i}.Текст = "ВЫБРАТЬ * ИЗ {h}.Объект{i} КАК Т";\n' for i, h in enumerate(heads))
        + "КонецПроцедуры\n"
    )
    got = [q["tables"] for q in _queries(tmp_path, body)]
    assert got == [[f"{h}.Объект{i}"] for i, h in enumerate(heads)]


# ═══════════════ Задача 6 (Д6) — квота search() ═══════════════


@pytest.mark.parametrize(
    "available, quota, limit, expected",
    [
        ([5, 0, 0, 0, 0, 0], 3, 20, [5, 0, 0, 0, 0, 0]),
        ([10, 10, 10, 10, 10, 10], 5, 30, [5, 5, 5, 5, 5, 5]),
        ([10, 1, 0, 0, 0, 0], 3, 8, [7, 1, 0, 0, 0, 0]),
        ([10, 10, 0, 0, 0, 0], 3, 5, [3, 2, 0, 0, 0, 0]),
        ([2, 2], 3, 1, [1, 0]),
        ([0, 0], 3, 30, [0, 0]),
    ],
)
def test_search_quota_allocation(available, quota, limit, expected):
    from rlm_tools_bsl.bsl_helpers import _search_quota_allocation

    assert _search_quota_allocation(available, quota, limit) == expected


def _quota_tree(root: Path) -> None:
    _write(root / "Configuration.xml", CF_DESCRIPTOR)
    procs = "".join(f"Процедура ПоискКвоты{n}() Экспорт\nКонецПроцедуры\n\n" for n in "АБВГД")
    _write(root / "CommonModules" / "ОбщийМодульКвот" / "Ext" / "Module.bsl", procs)


def test_search_gives_free_places_to_a_short_source(tmp_path, monkeypatch):
    root = tmp_path / "cf"
    _quota_tree(root)
    reader = _reader_for(root, monkeypatch, build_calls=False, build_metadata=True)
    try:
        bsl = _bsl_for(root, reader)
        for limit in (5, 10, 20):
            rows = bsl["search"]("ПоискКвоты", limit=limit)
            assert sum(r["source_type"] == "method" for r in rows) == 5, (limit, rows)
        assert len(bsl["search"]("ПоискКвоты", limit=4)) == 4  # недобор добран, но не сверх limit
        assert len(bsl["search"]("ПоискКвоты", scope="methods", limit=2)) == 2  # один источник — как раньше
    finally:
        reader.close()


def test_full_search_page_raises_the_truncation_hint(tmp_path):
    sb = Sandbox(base_path=str(tmp_path))
    sb._note_saturation("search", ("q",), {"limit": 3}, [{}] * 3)
    hint = next(h for h in sb._compute_efficiency_hints() if h["id"] == "list_truncated:search")
    assert "scope" in hint["message"]


def _ext_session(tmp_path, monkeypatch, main_files: dict, ext_files: dict, **build_kw):
    """Основная конфигурация с индексом и соседнее расширение «Доработки» (CF); ключи — пути."""
    cf = tmp_path / "src" / "cf"
    cfe = tmp_path / "src" / "cfe" / "Доработки"
    _write(cf / "Configuration.xml", CF_MAIN_DESCRIPTOR)
    _write(cfe / "Configuration.xml", CF_EXT_DESCRIPTOR)
    for rel, text in main_files.items():
        _write(cf / rel, text)
    for rel, text in ext_files.items():
        _write(cfe / rel, text)
    reader = _reader_for(cf, monkeypatch, build_calls=False, build_metadata=False, **build_kw)
    return _bsl_for(cf, reader, extension_paths=[str(cfe)]), reader


def test_retry_keeps_the_extension_row_of_the_quota_pass(tmp_path, monkeypatch):
    """Страж: на limit=8 квота шапок (3) отдаёт две строки основной и одну расширения. Повтор
    источника с полным limit не должен вытеснить строку расширения строками основной."""
    procs = "".join(f"Процедура ПроцКвота{n}() Экспорт\nКонецПроцедуры\n\n" for n in "АБВГД")
    main = {"CommonModules/МодульМетодов/Ext/Module.bsl": procs}
    main.update({f"CommonModules/Шапка{i}/Ext/Module.bsl": f"// Квота шапка {i}\n\n{PROC}" for i in range(1, 10)})
    ext = {"CommonModules/др_Доработка/Ext/Module.bsl": f"// Квота доработка\n\n{PROC}"}
    bsl, reader = _ext_session(tmp_path, monkeypatch, main, ext)
    try:
        rows = bsl["search"]("Квота", limit=8)
        assert sum(r["source_type"] == "method" for r in rows) == 5
        headers = [r["path"] for r in rows if r["source_type"] == "header"]
        assert len(headers) == 3 and any(p.startswith("../cfe/") for p in headers), headers
    finally:
        reader.close()


def _ext_only_headers(tmp_path, monkeypatch):
    main = {"CommonModules/Основной/Ext/Module.bsl": PROC}
    ext = {f"CommonModules/др_Модуль{i}/Ext/Module.bsl": f"// ШапкаКвоты {i}\n\n{PROC}" for i in range(1, 21)}
    return _ext_session(tmp_path, monkeypatch, main, ext)


def test_extension_headers_fill_the_free_places(tmp_path, monkeypatch):
    """Шапки только в расширении (20): страница добирается до limit, а не до квоты max(1, limit // 5)."""
    bsl, reader = _ext_only_headers(tmp_path, monkeypatch)
    try:
        assert len(bsl["search_module_headers"]("ШапкаКвоты", limit=10)) == 10
        assert len(bsl["search"]("ШапкаКвоты", limit=30)) == 20
        assert bsl["search_module_headers"]("ШапкаКвоты", count_only=True)["total"] == 20
    finally:
        reader.close()


def test_search_retry_does_not_rescan_extensions(tmp_path, monkeypatch):
    """Повтор источника внутри search() берёт живой проход по расширениям из памяти вызова:
    20 модулей — 20 разборов шапки, а не 40. Вне search() память выключена."""
    bsl, reader = _ext_only_headers(tmp_path, monkeypatch)
    calls = []
    real = bsl_helpers_module._extract_header_comment
    monkeypatch.setattr(bsl_helpers_module, "_extract_header_comment", lambda *a, **k: calls.append(1) or real(*a, **k))
    try:
        assert len(bsl["search"]("ШапкаКвоты", limit=30)) == 20
        assert len(calls) == 20
        calls.clear()
        bsl["search_module_headers"]("ШапкаКвоты", limit=30)
        assert len(calls) == 20
    finally:
        reader.close()


@pytest.mark.parametrize("scope", ["attributes", "predefined"])
def test_search_metadata_sources_have_no_hidden_cap(tmp_path, monkeypatch, scope):
    """600 совпадений: search(scope, limit=600) отдаёт 600, а не скрытые 500 (дефолт limit
    у find_attributes/find_predefined)."""
    root = tmp_path / "cf"
    _write(root / "Configuration.xml", CF_DESCRIPTOR)
    _write(root / "Catalogs" / "Склады" / "Ext" / "ObjectModule.bsl", PROC)
    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "cf_idx"))
    db_path = IndexBuilder().build(str(root), build_calls=False, build_metadata=True)
    types = json.dumps(["Строка"], ensure_ascii=False)
    with sqlite3.connect(str(db_path)) as conn:
        if scope == "attributes":
            conn.executemany(
                "INSERT INTO object_attributes (object_name, category, attr_name, attr_synonym, attr_type,"
                " attr_kind, ts_name, source_file) VALUES (?, 'Catalogs', ?, '', ?, 'attribute', '', ?)",
                [(f"Об{i // 10}", f"ЗасевКвоты{i}", types, f"Catalogs/Об{i // 10}.xml") for i in range(600)],
            )
        else:
            conn.executemany(
                "INSERT INTO predefined_items (object_name, category, item_name, item_synonym, item_code,"
                " types_json, is_folder, source_file) VALUES (?, 'Catalogs', ?, '', '', ?, 0, ?)",
                [(f"Об{i // 10}", f"ЗасевКвоты{i}", types, f"Catalogs/Об{i // 10}.xml") for i in range(600)],
            )
    reader = IndexReader(db_path)
    try:
        bsl = _bsl_for(root, reader)
        assert len(bsl["search"]("ЗасевКвоты", scope=scope, limit=600)) == 600
        assert len(bsl["search"]("ЗасевКвоты", limit=3000)) == 600
        assert len(bsl["search"]("ЗасевКвоты", scope=scope, limit=7)) == 7
    finally:
        reader.close()


# ═══════════════ Задача 7 (Д7) — потолок шапки ═══════════════


def test_header_cap_constant_is_the_builder_default():
    from rlm_tools_bsl.bsl_index import HEADER_COMMENT_MAX_CHARS, _extract_header_comment

    assert len(_extract_header_comment(["// " + "х" * 900])) == HEADER_COMMENT_MAX_CHARS == 500


def test_header_at_the_cap_is_marked(tmp_path, monkeypatch):
    root = tmp_path / "cf"
    _write(root / "Configuration.xml", CF_DESCRIPTOR)
    long_header = "\n".join(f"// Шапка, строка {i:02d}: " + "х" * 50 for i in range(12))
    _write(root / "CommonModules" / "Длинный" / "Ext" / "Module.bsl", long_header + "\n\n" + PROC)
    _write(root / "CommonModules" / "Короткий" / "Ext" / "Module.bsl", "// Короткая шапка\n\n" + PROC)
    reader = _reader_for(root, monkeypatch, build_calls=False, build_metadata=False, build_fts=False)
    try:
        rows = {r["object_name"]: r for r in _bsl_for(root, reader)["search_module_headers"]("шапка")}
        assert len(rows["Длинный"]["header_comment"]) == 500 and rows["Длинный"]["header_truncated"] is True
        assert rows["Короткий"]["header_truncated"] is False
        # Совет справки исполним: полный текст модуля основной конфигурации — read_file(module_path).
        read_file = make_helpers(str(root), reader)[0]["read_file"]
        assert "строка 11" not in rows["Длинный"]["header_comment"]
        assert "строка 11" in read_file(rows["Длинный"]["module_path"])
    finally:
        reader.close()


# ═══════════════ Задача 8 (Д8) — справка о перехватах ═══════════════


@pytest.fixture
def ov_index(tmp_path, monkeypatch):
    cf = tmp_path / "src" / "cf"
    cfe = tmp_path / "src" / "cfe" / "Доработки"
    _write(cf / "Configuration.xml", CF_MAIN_DESCRIPTOR)
    _write(cf / "Catalogs" / "Товары" / "Ext" / "ObjectModule.bsl", "Процедура ПриЗаписи(Отказ)\nКонецПроцедуры\n")
    _write(cfe / "Configuration.xml", CF_EXT_DESCRIPTOR)
    _write(
        cfe / "Catalogs" / "Товары" / "Ext" / "ObjectModule.bsl",
        '// служебный комментарий\n&После("ПриЗаписи")\nПроцедура др_ПриЗаписи(Отказ)\nКонецПроцедуры\n',
    )
    reader = _reader_for(cf, monkeypatch, build_calls=False, build_metadata=False, build_fts=False)
    yield _bsl_for(cf, reader, extension_paths=[str(cfe)]), cf, cfe
    reader.close()


def test_extensions_recipe_names_the_real_overridden_by_keys(ov_index):
    from rlm_tools_bsl.bsl_knowledge import _BUSINESS_RECIPES

    bsl, _cf, _cfe = ov_index
    line = next(s for s in _BUSINESS_RECIPES["расширения"]["full"] if "overridden_by" in s)
    documented = {k.strip() for k in re.search(r"\{([^}]*)\}", line).group(1).split(",")}
    [proc] = [p for p in bsl["extract_procedures"]("Catalogs/Товары/Ext/ObjectModule.bsl") if p.get("overridden_by")]
    # v1.47.0: прежде здесь стояло `assert documented == set(proc["overridden_by"][0])`. Рецепт объявляет и
    # необязательные ключи с «?» — они есть только у перехвата из формы расширения (callType), у строки из
    # аннотации их нет (прежний ответ побайтно); сами формовые строки — tests/test_v1_47_0.py.
    required = {k for k in documented if not k.endswith("?")}
    optional = {k.rstrip("?") for k in documented if k.endswith("?")}
    assert required == set(proc["overridden_by"][0])
    assert optional == {"form_item", "form_event"}


def test_ext_line_points_at_the_annotation(ov_index):
    bsl, _cf, cfe = ov_index
    [row] = bsl["get_overrides"]("Товары")["overrides"]
    text = (cfe / "Catalogs" / "Товары" / "Ext" / "ObjectModule.bsl").read_text(encoding="utf-8").splitlines()
    assert text[row["ext_line"] - 1].startswith("&После")
    assert text[row["ext_line"]].startswith("Процедура др_ПриЗаписи")
    recipe = bsl["help"]("get_overrides")
    assert "ext_line" in recipe and "аннотац" in recipe.casefold()


# ═══════════════ Задача 9 (Д9) — причина у модуля расширения ═══════════════


def test_extension_module_is_not_reported_as_missing_index(ov_index):
    bsl, _cf, _cfe = ov_index
    mods = bsl["get_object_modules"]("Справочник.Товары", no_live=True)["modules"]
    ext = [m for m in mods if m["path"].startswith("../")]
    main = [m for m in mods if not m["path"].startswith("../")]
    assert ext and all(m["_meta"]["fallback_reason"] == "extension_module_not_indexed" for m in ext)
    assert all(m["_meta"]["skipped_live"] for m in ext)
    assert all(m["_meta"]["index_used"] for m in main)


def test_profile_names_skipped_extension_modules(ov_index):
    bsl, _cf, _cfe = ov_index
    sec = bsl["get_object_profile"]("Справочник.Товары", sections=["modules"])["sections"]["modules"]
    # v1.45.0 (П1): прежде здесь стояло status='skipped' с reason='extension_modules_skipped_live' — модули
    # соседних расширений секция не читала. Теперь они разбираются живьём, секция полна, причины пропуска нет.
    assert sec["status"] == "ok"
    assert "reason" not in sec["_meta"]
    assert any(i["path"].startswith("../") and i["methods"] > 0 for i in sec["items"])


# ═══════════════ Задача 10 (Д10) — nearby_main ═══════════════


def test_main_config_gets_no_nearby_main(tmp_path):
    own = tmp_path / "repos" / "a" / "src"
    other = tmp_path / "repos" / "b" / "src"
    _write(own / "Configuration.xml", CF_MAIN_DESCRIPTOR)
    _write(other / "Configuration.xml", CF_MAIN_DESCRIPTOR)
    ctx = detect_extension_context(str(own))
    assert ctx.current.role == ConfigRole.MAIN
    assert ctx.nearby_main is None


def test_config_is_never_its_own_neighbour(tmp_path):
    """Рабочее пространство: расширение и основная — проекты рядом. Уровень −2 находит и
    само расширение (через его каталог-проект) — оно не сосед себе; основная остаётся."""
    ext = tmp_path / "ws" / "ext" / "src"
    main = tmp_path / "ws" / "main" / "src"
    _write(ext / "Configuration.xml", CF_EXT_DESCRIPTOR)
    _write(main / "Configuration.xml", CF_MAIN_DESCRIPTOR)
    ctx = detect_extension_context(str(ext))
    assert ctx.current.role == ConfigRole.EXTENSION
    assert ctx.nearby_main is not None and Path(ctx.nearby_main.path).resolve() == main.resolve()
    assert all(Path(e.path).resolve() != ext.resolve() for e in ctx.nearby_extensions)


# ═══════════════ Задача 11 (Д11) — has_* в стартовом блоке ═══════════════

_V16_HAS = {
    "has_declared_register_records",
    "has_role_flags",
    "has_common_module_props",
    "has_constants",
    "has_templates",
}


@pytest.mark.parametrize("with_metadata", [True, False], ids=["metadata", "no_metadata"])
def test_start_index_carries_every_has_key_but_the_v16_tables(tmp_path, monkeypatch, with_metadata):
    from rlm_tools_bsl.server import _canonicalize_path, _rlm_end, _rlm_start

    root = Path(_canonicalize_path(str(tmp_path))) / "cf"
    _write(root / "Configuration.xml", CF_DESCRIPTOR)
    _write(root / "Documents" / "Заказ" / "Ext" / "ObjectModule.bsl", PROC)
    reader = _reader_for(root, monkeypatch, build_calls=False, build_metadata=with_metadata)
    try:
        start = json.loads(_rlm_start(path=str(root), query="has"))
        try:
            idx = start["index"]
            assert idx["loaded"] is True
            for key in ("has_regions", "has_module_headers", "has_extension_overrides"):
                assert idx[key] is True, key
            info = _bsl_for(root, reader)["get_index_info"]()
            has_info = {k for k in info if k.startswith("has_")}
            assert has_info - set(idx) == _V16_HAS
            for key in has_info & set(idx):
                assert idx[key] == info[key], key
            # Правило из текстов стратегии и подсказки: таблицы v16 есть тогда и только тогда,
            # когда builder_version >= 16 и has_object_attributes (оба ключа — в стартовом блоке).
            predicted = idx["builder_version"] >= 16 and idx["has_object_attributes"]
            assert {info[k] for k in _V16_HAS} == {predicted}
        finally:
            _rlm_end(start["session_id"])
    finally:
        reader.close()


def test_index_nudge_texts_name_the_v16_boundary():
    from rlm_tools_bsl.bsl_knowledge import _STRATEGY_HEADER, _render_index_block
    from rlm_tools_bsl.bsl_strategy_data import STRATEGY_SECTIONS
    from rlm_tools_bsl.sandbox import HelperCall

    def batching(text):
        return next(ln for ln in text.splitlines() if "Не зови get_index_info на старте" in ln)

    block = _render_index_block({"builder_version": 17, "methods": 1, "calls": 1}, [])
    index_line = next(ln for ln in block.splitlines() if "get_index_info()" in ln)
    sb = Sandbox(base_path=".")
    sb._helper_calls = [HelperCall("get_index_info", 0.0, seq=1)]
    hint = next(h for h in sb._compute_efficiency_hints() if h["id"] == "redundant_get_index_info")
    assert batching(STRATEGY_SECTIONS["batching"]) == batching(_STRATEGY_HEADER)
    for text in (batching(_STRATEGY_HEADER), index_line, hint["message"]):
        assert "v16" in text and "builder_version" in text and "has_object_attributes" in text, text
    assert "следуют из builder_version (>= 8 / 8 / 9)" not in hint["message"]


# ═══════════════ Задача 12 (Д12) — потолок duplicates ═══════════════


def test_duplicates_field_is_capped(tmp_path):
    from rlm_tools_bsl.server import _rlm_end, _rlm_execute, _rlm_start

    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    start = json.loads(_rlm_start(path=str(tmp_path), query="dup"))
    sid = start["session_id"]
    try:
        many = json.loads(_rlm_execute(sid, "for _ in range(16):\n    read_file('a.txt')"))
        assert len(many["duplicates"]) == 10
        assert many["duplicates_truncated_count"] == 5
        edge = json.loads(_rlm_execute(sid, "for _ in range(10):\n    read_file('a.txt')"))
        assert len(edge["duplicates"]) == 10 and "duplicates_truncated_count" not in edge
        clean = json.loads(_rlm_execute(sid, "x = 1"))
        assert "duplicates" not in clean and "duplicates_truncated_count" not in clean
    finally:
        _rlm_end(sid)


# ═══════════════ Задача 13 (Д13) — основания на голом имени ═══════════════


_PF_MANAGER = (
    "Процедура ДобавитьКомандыПечати(КомандыПечати) Экспорт\n"
    '    К = КомандыПечати.Добавить();\n    К.Идентификатор = "Счет";\n'
    "    К.Представление = НСтр(\"ru = 'Счет'\");\n"
    "КонецПроцедуры\n"
)


def _based_on_index(tmp_path, monkeypatch, extra=()):
    """Договоры — справочник (основание документа Оплата); у Оплаты печатная форма Счет и
    декларативный потомок Возврат."""
    root = tmp_path / "cf"
    _write(root / "Configuration.xml", CF_DESCRIPTOR)
    _write(root / "Catalogs" / "Договоры" / "Ext" / "ObjectModule.bsl", PROC)
    _write(root / "Documents" / "Оплата" / "Ext" / "ObjectModule.bsl", PROC)
    _write(root / "Documents" / "Оплата" / "Ext" / "ManagerModule.bsl", _PF_MANAGER)
    for rel in extra:
        _write(root / rel, PROC)
    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
    db = IndexBuilder().build(str(root), build_calls=False, build_metadata=True, build_fts=False)
    con = sqlite3.connect(str(db))
    con.executemany(
        "INSERT INTO metadata_references (source_object, source_category, ref_object, ref_kind, used_in, path, line)"
        " VALUES (?, 'Documents', ?, 'based_on', 'BasedOn', ?, NULL)",
        [
            ("Оплата", "Catalog.Договоры", "Documents/Оплата.xml"),
            ("Возврат", "Document.Оплата", "Documents/Возврат.xml"),
        ],
    )
    con.commit()
    con.close()
    return root, IndexReader(db)


def test_bare_catalog_name_is_answered_as_the_catalog(tmp_path, monkeypatch):
    root, reader = _based_on_index(tmp_path, monkeypatch)
    try:
        bsl = _bsl_for(root, reader)
        typed = bsl["find_based_on_documents"]("Справочник.Договоры")
        bare = bsl["find_based_on_documents"]("Договоры")
        assert [d["document"] for d in typed["can_create_from_here"]] == ["Оплата"]
        assert [d["document"] for d in bare["can_create_from_here"]] == ["Оплата"]
        assert bare["_meta"]["resolved_ref"] == "Catalog.Договоры"
    finally:
        reader.close()


def test_bare_name_in_several_non_document_categories_is_named(tmp_path, monkeypatch):
    root, reader = _based_on_index(tmp_path, monkeypatch, extra=("BusinessProcesses/Договоры/Ext/ObjectModule.bsl",))
    try:
        with pytest.raises(ValueError) as ei:
            _bsl_for(root, reader)["find_based_on_documents"]("Договоры")
        assert "Catalogs" in str(ei.value) and "BusinessProcesses" in str(ei.value)
    finally:
        reader.close()


def test_document_flow_stays_document_centric(tmp_path, monkeypatch):
    """Страж: композит документный — справочник-омоним в based_on не подмешивается."""
    root, reader = _based_on_index(tmp_path, monkeypatch)
    try:
        flow = _bsl_for(root, reader)["analyze_document_flow"]("Договоры")
        assert flow["based_on"]["can_create_from_here"] == []
    finally:
        reader.close()


@pytest.mark.parametrize(
    "arg",
    [
        "Оплата",
        "Документ.Оплата",
        "Document.Оплата",
        "документ.Оплата",
        "document.Оплата",
        "Documents/Оплата",
        "Документы/Оплата",
    ],
)
def test_document_flow_reads_any_document_prefix(tmp_path, monkeypatch, arg):
    """Префикс типа в любом регистре и путь — тот же документ. Композит срезал префикс
    регистрозависимо: `документ.Оплата` оставался как есть, печатные формы искались у
    `Документ.документ.Оплата` и молча пропадали; путь терял и печатные формы, и основания.
    Страж: `Оплата`, `Документ.Оплата`, `Document.Оплата` зелёные и на v1.43.0."""
    root, reader = _based_on_index(tmp_path, monkeypatch)
    try:
        flow = _bsl_for(root, reader)["analyze_document_flow"](arg)
        assert flow["document"] == "Оплата"
        assert [r["name"] for r in flow["print_forms"]["print_forms"]] == ["Счет"]
        assert [d["document"] for d in flow["based_on"]["can_create_from_here"]] == ["Возврат"]
    finally:
        reader.close()


@pytest.mark.parametrize(
    "arg",
    [
        "Catalogs/Склады/Forms/ФормаСписка",
        "Справочники/Склады/Forms/ФормаСписка",
        "Catalogs/Склады/Forms/формасписка",
        "Catalogs/склады/Forms/ФормаСписка",
        "{file}",
        "{file_lower}",
    ],
)
def test_parse_form_path_deeper_than_category_and_name(forms_tree, monkeypatch, with_index, arg):
    """Путь глубже `Категория/Имя` — путь формы или файла формы, в том числе с другим регистром
    сегментов. До v1.44.0 без индекса его находил широкий отбор подстрокой (на индексе — падение
    на подсказке); префикс-категория этот отбор отключила, и путь давал молчаливый `[]`. Объект —
    первый сегмент (в написании выгрузки), форма — сегмент после `Forms` (без учёта регистра).
    Страж для живой ветки: варианты `Catalogs/…` и пути файла зелёные и на v1.43.0; RED на v1.43.0 —
    индексная ветка и `Справочники/…`."""
    root, _missing = forms_tree
    reader = _index_for_forms(root, monkeypatch) if with_index else None
    try:
        bsl = _bsl_for(root, reader)
        if arg.startswith("{file"):
            real = next(f["file"] for f in bsl["parse_form"]("Склады") if f["form_name"] == "ФормаСписка")
            arg = real if arg == "{file}" else real.replace("ФормаСписка", "формасписка")
        rows = bsl["parse_form"](arg)
        assert {(f["category"], f["object_name"], f["form_name"]) for f in rows} == {
            ("Catalogs", "Склады", "ФормаСписка")
        }
    finally:
        if reader:
            reader.close()


def test_explicit_form_name_keeps_the_exact_match(forms_tree, monkeypatch, with_index):
    """Страж: явный `form_name` сравнивается точно, как и до v1.44.0; без учёта регистра сравнивается
    только имя формы, извлечённое из пути."""
    root, _missing = forms_tree
    reader = _index_for_forms(root, monkeypatch) if with_index else None
    try:
        bsl = _bsl_for(root, reader)
        assert [f["form_name"] for f in bsl["parse_form"]("Справочник.Склады", "ФормаСписка")] == ["ФормаСписка"]
        assert bsl["parse_form"]("Справочник.Склады", "формасписка") == []
    finally:
        if reader:
            reader.close()


# ═══════════════ Ревью: находки, перепроверенные исполнением ═══════════════

_HOMONYM_MODULES = {
    "AccumulationRegisters": "RecordSetModule",
    "Reports": "ObjectModule",
    "DataProcessors": "ObjectModule",
    "Enums": "ManagerModule",
    "CommonModules": "Module",
}


@pytest.mark.parametrize("other", sorted(_HOMONYM_MODULES))
def test_bare_name_ignores_categories_that_are_never_a_basis(tmp_path, monkeypatch, other):
    """Справочник и одноимённый регистр, отчёт, обработка, перечисление или общий модуль (на боевых
    ERP таких имён — десятки: `ДенежныеДокументы`, `НастройкиЭДО`, `Контрагенты`): основанием бывает
    только справочник, и голое имя отвечается им, а не отказом «есть в нескольких категориях»."""
    rel = f"{other}/Договоры/Ext/{_HOMONYM_MODULES[other]}.bsl"
    root, reader = _based_on_index(tmp_path, monkeypatch, extra=(rel,))
    try:
        bare = _bsl_for(root, reader)["find_based_on_documents"]("Договоры")
        assert [d["document"] for d in bare["can_create_from_here"]] == ["Оплата"]
        assert bare["_meta"]["resolved_ref"] == "Catalog.Договоры"
    finally:
        reader.close()


def test_resolved_ref_carries_the_object_spelling(tmp_path, monkeypatch):
    """Каноническая ссылка — в написании объекта, а не аргумента."""
    root, reader = _based_on_index(tmp_path, monkeypatch)
    try:
        bare = _bsl_for(root, reader)["find_based_on_documents"]("договоры")
        assert bare["_meta"]["resolved_ref"] == "Catalog.Договоры"
        assert bare["document"] == "Договоры"
        assert [d["document"] for d in bare["can_create_from_here"]] == ["Оплата"]
    finally:
        reader.close()


@pytest.mark.parametrize("arg", ["Catalogs/склады", "справочник.склады"])
def test_parse_form_names_the_object_as_spelled_in_the_dump(forms_tree, monkeypatch, with_index, arg):
    """Имя объекта в строке формы — из выгрузки, а не из аргумента; объект с префиксом типа ищется без
    учёта регистра на любой ФС (индекс сравнивает имя точно, а ФС на Linux различает регистр).
    Страж: `Catalogs/склады` на живой ветке зелёный и на v1.43.0 (широкий отбор брал имя из пути).
    Голое имя не в том регистре — давняя граница (точное сравнение индекса), здесь не проверяется."""
    root, _missing = forms_tree
    reader = _index_for_forms(root, monkeypatch) if with_index else None
    try:
        rows = _bsl_for(root, reader)["parse_form"](arg)
        assert rows and {f["object_name"] for f in rows} == {"Склады"}
    finally:
        if reader:
            reader.close()


def _cf_object(kind: str, name: str, basis: str = "") -> str:
    """Описатель CF в боевой форме: `<BasedOn>` — `xr:Item` с `xsi:type`, один реквизит."""
    based = f'<BasedOn><xr:Item xsi:type="xr:MDObjectRef">{basis}</xr:Item></BasedOn>' if basis else ""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses" xmlns:xr="http://v8.1c.ru/8.3/xcf/readable" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        f"<{kind}><Properties><Name>{name}</Name>{based}</Properties>"
        "<ChildObjects><Attribute><Properties><Name>Сумма</Name></Properties></Attribute></ChildObjects>"
        f"</{kind}></MetaDataObject>"
    )


def _edt_object(kind: str, name: str, basis: str = "") -> str:
    """Описатель EDT в боевой форме: `<basedOn>` — текст, реквизит — блок `<attributes>`."""
    based = f"<basedOn>{basis}</basedOn>" if basis else ""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<mdclass:{kind} xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass">'
        f"<name>{name}</name>{based}<attributes><name>Сумма</name></attributes></mdclass:{kind}>\n"
    )


def _document_and_catalog_homonym(tmp_path: Path, fmt: str) -> tuple[Path, Path]:
    """Документ `Договоры` без BSL-модулей и справочник `Договоры` с модулем; `ПоДокументу` вводится
    на основании документа, `ПоСправочнику` — справочника. Возвращает корень и описатель документа."""
    root = tmp_path / fmt
    if fmt == "cf":
        _write(root / "Configuration.xml", CF_DESCRIPTOR)
        objects = [
            ("Documents", "Document", "Договоры", ""),
            ("Catalogs", "Catalog", "Договоры", ""),
            ("Documents", "Document", "ПоДокументу", "Document.Договоры"),
            ("Documents", "Document", "ПоСправочнику", "Catalog.Договоры"),
        ]
        for folder, kind, name, basis in objects:
            _write(root / folder / f"{name}.xml", _cf_object(kind, name, basis))
            (root / folder / name).mkdir(parents=True, exist_ok=True)
        _write(root / "Catalogs" / "Договоры" / "Ext" / "ObjectModule.bsl", PROC)
        return root, root / "Documents" / "Договоры.xml"
    _write(root / "Configuration" / "Configuration.mdo", EDT_DESCRIPTOR)
    _write(root / "Documents" / "Договоры" / "Договоры.mdo", _edt_object("Document", "Договоры"))
    _write(root / "Catalogs" / "Договоры" / "Договоры.mdo", _edt_object("Catalog", "Договоры"))
    _write(root / "Catalogs" / "Договоры" / "ObjectModule.bsl", PROC)
    for name, basis in (("ПоДокументу", "Document.Договоры"), ("ПоСправочнику", "Catalog.Договоры")):
        _write(root / "Documents" / name / f"{name}.mdo", _edt_object("Document", name, basis))
    return root, root / "Documents" / "Договоры" / "Договоры.mdo"


@pytest.mark.parametrize("arg", ["Договоры", "договоры"])
@pytest.mark.parametrize("fmt", ["cf", "edt"])
def test_document_known_to_the_index_stays_the_document(tmp_path, monkeypatch, fmt, arg):
    """Документ без BSL-модулей есть в индексе, а описателя на диске нет (выгрузку обновили после
    сборки; `index_status` при этом `ok`) — голое имя остаётся документом, как на v1.43.0. Союз
    `_object_categories`, из которого выбирается категория, документ видит (индекс), а проба описателя
    на диске — нет, и ответ молча уходил к одноимённому справочнику. Тот же разрыв на ФС Linux даёт имя
    в другом регистре и при свежем описателе. Страж: свежий описатель и точное имя — документ и на
    v1.43.0; без индекса убранный документ не виден нигде — ответ по справочнику (замысел v1.44.0)."""
    root, descriptor = _document_and_catalog_homonym(tmp_path, fmt)
    reader = _reader_for(root, monkeypatch, build_calls=False, build_metadata=True, build_fts=False)
    try:
        fresh = _bsl_for(root, reader)["find_based_on_documents"](arg)
        descriptor.rename(descriptor.with_name(descriptor.name + ".bak"))
        stale = _bsl_for(root, reader)["find_based_on_documents"](arg)
        for res in (fresh, stale):
            assert "resolved_ref" not in res.get("_meta", {}), res
            assert [d["document"] for d in res["can_create_from_here"]] == ["ПоДокументу"], res
            assert not res.get("partial"), res
        live = _bsl_for(root)["find_based_on_documents"](arg)
        assert live["_meta"]["resolved_ref"] == "Catalog.Договоры"
    finally:
        reader.close()


# ═══════════════ Документные хелперы: префикс в любом регистре и путь ═══════════════

_CF_MD_NS = (
    'xmlns="http://v8.1c.ru/8.3/MDClasses" xmlns:cfg="http://v8.1c.ru/8.1/data/enterprise/current-config" '
    'xmlns:v8="http://v8.1c.ru/8.1/data/core" xmlns:xr="http://v8.1c.ru/8.3/xcf/readable" '
    'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'
)
_CF_ATTR = "<ChildObjects><Attribute><Properties><Name>Сумма</Name></Properties></Attribute></ChildObjects>"
_POSTING_MODULE = (
    "Процедура ОбработкаПроведения(Отказ, РежимПроведения)\n"
    "\tДвижения.Взаиморасчеты.Записывать = Истина;\n"
    "\tДвижения.Взаиморасчеты.Записать();\n"
    "КонецПроцедуры\n"
    "\n"
    "Процедура др_Проверить() Экспорт\n"
    '\tЕсли ПолучитьФункциональнуюОпцию("ИспользоватьОплату") Тогда\n'
    "\tКонецЕсли;\n"
    "КонецПроцедуры\n"
)
_BASED_ON_MANAGER = (
    "Процедура ДобавитьКомандыСозданияНаОсновании(КомандыСозданияНаОсновании, Параметры) Экспорт\n"
    "\tДокументы.Возврат.ДобавитьКомандуСозданияНаОсновании(КомандыСозданияНаОсновании);\n"
    "КонецПроцедуры\n"
    "\n" + _PF_MANAGER
)
_USAGE_MODULE = "Процедура Создать() Экспорт\n\tДок = Документы.Оплата.СоздатьДокумент();\nКонецПроцедуры\n"
_RIGHTS = (
    '<?xml version="1.0" encoding="UTF-8"?>\n<Rights xmlns="http://v8.1c.ru/8.2/roles" '
    'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:type="Rights">'
    "<setForNewObjects>false</setForNewObjects><setForAttributesByDefault>true</setForAttributesByDefault>"
    "<independentRightsOfChildObjects>false</independentRightsOfChildObjects>"
    "<object><name>Document.Оплата</name><right><name>Read</name><value>true</value></right></object></Rights>"
)
_CF_PREDEFINED = (
    '<?xml version="1.0" encoding="UTF-8"?>\n<PredefinedData xmlns="http://v8.1c.ru/8.3/xcf/predef" '
    'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:type="CatalogPredefinedItems">'
    '<Item id="00000000-0000-0000-0000-000000000011"><Name>Основной</Name><Code>000000001</Code>'
    "<Description>Основной</Description><IsFolder>false</IsFolder></Item></PredefinedData>"
)
_CF_EXCHANGE_CONTENT = (
    '<?xml version="1.0" encoding="UTF-8"?>\n<ExchangePlanContent xmlns="http://v8.1c.ru/8.3/xcf/extrnprops" '
    'xmlns:xr="http://v8.1c.ru/8.3/xcf/readable">'
    "<Item><Metadata>Document.Оплата</Metadata><AutoRecord>Allow</AutoRecord></Item></ExchangePlanContent>"
)
_SUBSCRIPTION_SOURCES = {
    "ПриЗаписиОплаты": ("DocumentObject.Оплата", False),
    "ПриЗаписиВозврата": ("DocumentObject.Возврат", False),
    "ВсеДокументы": ("DocumentObject", True),
    "ПриЗаписиДоговора": ("CatalogObject.Договоры", False),
}


def _cf_md(kind: str, name: str, props: str = "", children: str = "") -> str:
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>\n<MetaDataObject {_CF_MD_NS}><{kind}><Properties>'
        f"<Name>{name}</Name>{props}</Properties>{children}</{kind}></MetaDataObject>"
    )


def _edt_md(kind: str, name: str, body: str = "") -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<mdclass:{kind} xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass"><name>{name}</name>{body}'
        f"</mdclass:{kind}>\n"
    )


def _document_domain_tree(root: Path, fmt: str) -> None:
    """`Оплата` пишет при проведении `Взаиморасчеты` (тот же регистр объявлен), из нее создается
    `Возврат` (команда менеджера и `BasedOn`); `ПоДоговору` вводится на основании справочника
    `Договоры`; подписки — на `Оплату`, на `Возврат`, на набор `DocumentObject` и на справочник.
    У `Оплаты` есть форма, макет, печатная форма, реквизит `др_Комментарий` и процедура `др_Проверить`
    (нетиповые), право роли `Бухгалтер`, функциональная опция, использование в коде общего модуля,
    место в составе плана обмена и в определяемом типе; у `Договоры` — предопределенный элемент;
    перечисление `СтатусыОплаты`. Формы описателей — как в боевых выгрузках."""
    if fmt == "cf":
        item = '<xr:Item xsi:type="xr:MDObjectRef">{}</xr:Item>'.format
        attribute = "<Attribute><Properties><Name>{}</Name></Properties></Attribute>".format
        _write(root / "Configuration.xml", CF_DESCRIPTOR)
        objects = [
            (
                "Documents",
                "Document",
                "Оплата",
                f"<RegisterRecords>{item('AccumulationRegister.Взаиморасчеты')}</RegisterRecords>",
                f"<ChildObjects>{attribute('Сумма')}{attribute('др_Комментарий')}<Form>ФормаДокумента</Form>"
                "<Template>Счет</Template></ChildObjects>",
            ),
            ("Documents", "Document", "Возврат", f"<BasedOn>{item('Document.Оплата')}</BasedOn>", _CF_ATTR),
            ("Documents", "Document", "ПоДоговору", f"<BasedOn>{item('Catalog.Договоры')}</BasedOn>", _CF_ATTR),
            ("Catalogs", "Catalog", "Договоры", "", _CF_ATTR),
            ("AccumulationRegisters", "AccumulationRegister", "Взаиморасчеты", "", _CF_ATTR),
            (
                "Enums",
                "Enum",
                "СтатусыОплаты",
                "",
                "<ChildObjects><EnumValue><Properties><Name>Оплачено</Name></Properties></EnumValue></ChildObjects>",
            ),
            ("ExchangePlans", "ExchangePlan", "Обмен", "", ""),
            (
                "DefinedTypes",
                "DefinedType",
                "ДокументыОплаты",
                "<Type><v8:Type>cfg:DocumentRef.Оплата</v8:Type></Type>",
                "",
            ),
            (
                "FunctionalOptions",
                "FunctionalOption",
                "ИспользоватьОплату",
                "<Location>Constant.ИспользоватьОплату</Location><Content><xr:Object>Document.Оплата</xr:Object></Content>",
                "",
            ),
            ("Roles", "Role", "Бухгалтер", "", ""),
            ("CommonModules", "CommonModule", "Платежи", "", ""),
            ("HTTPServices", "HTTPService", "API", "", ""),
            ("HTTPServices", "HTTPService", "Пустой", "", ""),
            ("CommonTemplates", "CommonTemplate", "Счет", "<TemplateType>SpreadsheetDocument</TemplateType>", ""),
        ]
        for folder, kind, name, props, children in objects:
            _write(root / folder / f"{name}.xml", _cf_md(kind, name, props, children))
            (root / folder / name).mkdir(parents=True, exist_ok=True)
        modules = root / "Documents" / "Оплата" / "Ext"
        form = root / "Documents" / "Оплата" / "Forms" / "ФормаДокумента" / "Ext"
        _write(form / "Form.xml", CF_FORM_ATTR)
        _write(form / "Form" / "Module.bsl", PROC)
        templates = root / "Documents" / "Оплата" / "Templates"
        _write(templates / "Счет.xml", _cf_md("Template", "Счет", "<TemplateType>SpreadsheetDocument</TemplateType>"))
        _write(templates / "Счет" / "Ext" / "Template.xml", "<document/>")
        _write(root / "Catalogs" / "Договоры" / "Ext" / "ObjectModule.bsl", PROC)
        _write(root / "Catalogs" / "Договоры" / "Ext" / "Predefined.xml", _CF_PREDEFINED)
        _write(root / "ExchangePlans" / "Обмен" / "Ext" / "Content.xml", _CF_EXCHANGE_CONTENT)
        _write(root / "Roles" / "Бухгалтер" / "Ext" / "Rights.xml", _RIGHTS)
        _write(root / "CommonModules" / "Платежи" / "Ext" / "Module.bsl", _USAGE_MODULE)
        _write(root / "HTTPServices" / "API" / "Ext" / "Module.bsl", PROC)
        _write(root / "CommonTemplates" / "Счет" / "Ext" / "Template.xml", "<document/>")
        for name, (source, is_set) in _SUBSCRIPTION_SOURCES.items():
            tag = "v8:TypeSet" if is_set else "v8:Type"
            props = (
                f"<Source><{tag}>cfg:{source}</{tag}></Source><Event>BeforeWrite</Event>"
                f"<Handler>CommonModule.События.{name}</Handler>"
            )
            _write(root / "EventSubscriptions" / f"{name}.xml", _cf_md("EventSubscription", name, props))
    else:
        _write(root / "Configuration" / "Configuration.mdo", EDT_DESCRIPTOR)
        attr = "<attributes><name>Сумма</name></attributes>"
        objects = [
            (
                "Documents",
                "Document",
                "Оплата",
                "<registerRecords>AccumulationRegister.Взаиморасчеты</registerRecords>" + attr + "<attributes>"
                "<name>др_Комментарий</name></attributes><forms><name>ФормаДокумента</name></forms><templates>"
                "<name>Счет</name><templateType>SpreadsheetDocument</templateType></templates>",
            ),
            ("Documents", "Document", "Возврат", "<basedOn>Document.Оплата</basedOn>" + attr),
            ("Documents", "Document", "ПоДоговору", "<basedOn>Catalog.Договоры</basedOn>" + attr),
            (
                "Catalogs",
                "Catalog",
                "Договоры",
                attr + '<predefined><items id="00000000-0000-0000-0000-000000000011"><name>Основной</name>'
                "<description>Основной</description></items></predefined>",
            ),
            ("AccumulationRegisters", "AccumulationRegister", "Взаиморасчеты", attr),
            ("Enums", "Enum", "СтатусыОплаты", "<enumValues><name>Оплачено</name></enumValues>"),
            ("ExchangePlans", "ExchangePlan", "Обмен", "<content><mdObject>Document.Оплата</mdObject></content>"),
            ("DefinedTypes", "DefinedType", "ДокументыОплаты", "<type><types>DocumentRef.Оплата</types></type>"),
            (
                "FunctionalOptions",
                "FunctionalOption",
                "ИспользоватьОплату",
                "<location>Constant.ИспользоватьОплату</location><content>Document.Оплата</content>",
            ),
            ("Roles", "Role", "Бухгалтер", ""),
            ("CommonModules", "CommonModule", "Платежи", ""),
            ("HTTPServices", "HTTPService", "API", ""),
            ("HTTPServices", "HTTPService", "Пустой", ""),
            ("CommonTemplates", "CommonTemplate", "Счет", "<templateType>SpreadsheetDocument</templateType>"),
        ]
        for folder, kind, name, body in objects:
            _write(root / folder / name / f"{name}.mdo", _edt_md(kind, name, body))
        modules = root / "Documents" / "Оплата"
        form = root / "Documents" / "Оплата" / "Forms" / "ФормаДокумента"
        _write(form / "Form.form", EDT_FORM_ATTR)
        _write(form / "Module.bsl", PROC)
        _write(root / "Documents" / "Оплата" / "Templates" / "Счет" / "Template.mxlx", "<document/>")
        _write(root / "Catalogs" / "Договоры" / "ObjectModule.bsl", PROC)
        _write(root / "Roles" / "Бухгалтер" / "Rights.rights", _RIGHTS)
        _write(root / "CommonModules" / "Платежи" / "Module.bsl", _USAGE_MODULE)
        _write(root / "HTTPServices" / "API" / "Module.bsl", PROC)
        _write(root / "CommonTemplates" / "Счет" / "Template.mxlx", "<document/>")
        for name, (source, _is_set) in _SUBSCRIPTION_SOURCES.items():
            body = (
                f"<source><types>{source}</types></source><event>BeforeWrite</event>"
                f"<handler>CommonModule.События.{name}</handler>"
            )
            _write(root / "EventSubscriptions" / name / f"{name}.mdo", _edt_md("EventSubscription", name, body))
    _write(modules / "ObjectModule.bsl", _POSTING_MODULE)
    _write(modules / "ManagerModule.bsl", _BASED_ON_MANAGER)


@pytest.fixture(scope="module", params=["cf", "edt"])
def doc_domain(request, tmp_path_factory):
    """Дерево и индекс — один раз на формат (`fmt`, корень, ридер)."""
    fmt = request.param
    root = tmp_path_factory.mktemp(f"docdomain_{fmt}") / fmt
    _document_domain_tree(root, fmt)
    mp = pytest.MonkeyPatch()
    mp.setenv("RLM_INDEX_DIR", str(root.parent / "idx"))
    try:
        reader = IndexReader(IndexBuilder().build(str(root), build_calls=False, build_metadata=True, build_fts=False))
    finally:
        mp.undo()
    yield fmt, root, reader
    reader.close()


def _object_forms(
    fmt: str, folder: str, ru_folder: str, heads: tuple[str, ...], name: str, with_module: bool = True
) -> list[str]:
    """Префикс типа в любом регистре, путь каталога (RU/EN, любой регистр) и пути файлов объекта:
    описатель и (у объекта с модулем) модуль объекта."""
    files = (
        [f"{folder}/{name}.xml", f"{folder}/{name}/Ext/ObjectModule.bsl"]
        if fmt == "cf"
        else [f"{folder}/{name}/{name}.mdo", f"{folder}/{name}/ObjectModule.bsl"]
    )
    if not with_module:
        files = files[:1]
    typed = [f"{head}.{name}" for head in heads] + [f"{head.lower()}.{name}" for head in heads]
    return [*typed, f"{folder}/{name}", f"{ru_folder}/{name}", f"{folder.lower()}/{name}", f"{folder}//{name}", *files]


def _document_forms(fmt: str) -> list[str]:
    return _object_forms(fmt, "Documents", "Документы", ("Документ", "Document", "ДокументСсылка"), "Оплата")


def _catalog_forms(fmt: str) -> list[str]:
    return _object_forms(fmt, "Catalogs", "Справочники", ("Справочник", "Catalog"), "Договоры")


def _movements_view(res: dict) -> tuple:
    return (res["document"], sorted(r["name"] for r in res["code_registers"]), sorted(res["declared_registers"]))


def _subscriptions_view(rows: list[dict]) -> list:
    return sorted((r["name"], r.get("matched_via")) for r in rows)


def _based_on_view(res: dict) -> tuple:
    return (
        res["document"],
        sorted((d.get("category") or "Documents", d["document"]) for d in res["can_create_from_here"]),
        sorted(d["type"] for d in res["can_be_created_from"]),
        (res.get("_meta") or {}).get("resolved_ref"),
        bool(res.get("partial")),
    )


def _structure_view(res: dict) -> tuple:
    return (res.get("category"), res.get("object_name"), sorted(a["name"] for a in res.get("attributes") or []))


def _flow_view(res: dict) -> tuple:
    return (
        res["document"],
        _movements_view(res["register_movements"]),
        sorted(s["name"] for s in res["event_subscriptions"]),
        _based_on_view(res["based_on"]),
    )


_DOC_VIEWS = {
    "find_register_movements": _movements_view,
    "find_event_subscriptions": _subscriptions_view,
    "find_based_on_documents": _based_on_view,
    "get_object_full_structure": _structure_view,
    "analyze_document_flow": _flow_view,
}


@pytest.mark.parametrize("helper", sorted(_DOC_VIEWS))
def test_document_helpers_read_every_form_of_the_document(doc_domain, with_index, helper):
    """Префикс типа в любом регистре (`документ.X`, `document.X`) и путь (`Documents/X`, `Документы/X`,
    `documents/X`, путь описателя или модуля) — тот же документ, что `Документ.X`. На v1.43.0
    `find_register_movements` на этих формах отвечал нулем, `find_event_subscriptions` на пути — всеми
    подписками на наборы, `find_based_on_documents` на пути — нулем, а на пути файла называл документом
    `xml`/`bsl`/`mdo`, `get_object_full_structure('Документы/X')` — «не найден». Страж: `Документ.X`,
    `Document.X`, `ДокументСсылка.X` (и голое имя у документных хелперов) зеленые и на v1.43.0."""
    fmt, root, reader = doc_domain
    bsl = _bsl_for(root, reader if with_index else None)
    view = _DOC_VIEWS[helper]
    canonical = view(bsl[helper]("Документ.Оплата"))
    forms = _document_forms(fmt)
    if helper in ("find_register_movements", "find_based_on_documents", "analyze_document_flow"):
        forms = ["Оплата", *forms]  # голое имя у документного хелпера — тот же документ
    for arg in forms:
        assert view(bsl[helper](arg)) == canonical, arg
    # Равенство не вакуумно: канонический ответ содержит то, что обещает фикстура.
    text = repr(canonical)
    expected = {
        "find_register_movements": ("Взаиморасчеты", "AccumulationRegister.Взаиморасчеты"),
        "find_event_subscriptions": ("ПриЗаписиОплаты", "ВсеДокументы"),
        "find_based_on_documents": ("Возврат",),
        "get_object_full_structure": ("Documents", "Сумма"),
        "analyze_document_flow": ("Взаиморасчеты", "ПриЗаписиОплаты", "Возврат"),
    }[helper]
    assert all(word in text for word in expected), canonical
    assert "ПриЗаписиВозврата" not in text


@pytest.mark.parametrize("helper", ["find_event_subscriptions", "find_based_on_documents", "get_object_full_structure"])
def test_object_helpers_read_paths_of_any_category(doc_domain, with_index, helper):
    """Путь не-документа — как префикс типа: `Catalogs/Договоры` = `Справочник.Договоры` (в том числе
    `Справочники/…`, нижний регистр и пути файлов). На v1.43.0 путь давал ноль (`find_based_on_documents`),
    все подписки на наборы (`find_event_subscriptions`) или «не найден» (`Справочники/…`)."""
    fmt, root, reader = doc_domain
    bsl = _bsl_for(root, reader if with_index else None)
    view = _DOC_VIEWS[helper]
    canonical = view(bsl[helper]("Справочник.Договоры"))
    for arg in _catalog_forms(fmt):
        assert view(bsl[helper](arg)) == canonical, arg
    expected = {
        "find_event_subscriptions": "ПриЗаписиДоговора",
        "find_based_on_documents": "ПоДоговору",
        "get_object_full_structure": "Catalogs",
    }[helper]
    if with_index or helper != "find_based_on_documents":  # основания справочника — только из индекса
        assert expected in repr(canonical), canonical


@pytest.mark.parametrize("helper", ["find_register_movements", "analyze_document_flow"])
@pytest.mark.parametrize(
    "arg",
    [
        "Справочник.Договоры",
        "справочник.Договоры",
        "Catalogs/Договоры",
        "Справочники/Договоры",
        "РегистрНакопления.Взаиморасчеты",
        "AccumulationRegisters/Взаиморасчеты",
    ],
)
def test_non_document_in_a_document_helper_is_named(doc_domain, with_index, helper, arg):
    """Объект другой категории в документном хелпере — названная ошибка с категорией и маршрутом. На
    v1.43.0 префикс молча отбрасывался: ответ по одноименному документу или уверенный ноль."""
    _fmt, root, reader = doc_domain
    with pytest.raises(ValueError) as ei:
        _bsl_for(root, reader if with_index else None)[helper](arg)
    msg = str(ei.value)
    assert "не документ" in msg and ("Catalogs" in msg or "AccumulationRegisters" in msg), msg
    assert "parse_object_xml(" in msg, msg  # маршрут к описателю объекта


def test_bare_name_of_a_non_document_keeps_the_old_answer(doc_domain, with_index):
    """Страж: голое имя не отказывается — документный хелпер ищет по нему документ, как и прежде."""
    _fmt, root, reader = doc_domain
    res = _bsl_for(root, reader if with_index else None)["find_register_movements"]("Договоры")
    assert res["code_registers"] == []


def test_non_string_object_name_still_fails(doc_domain):
    """Страж: не-строка падает, как прежде, а не превращается в пустое имя (на пустом имени
    `get_object_full_structure` отдал бы первый попавшийся объект)."""
    _fmt, root, reader = doc_domain
    bsl = _bsl_for(root, reader)
    for helper in ("get_object_full_structure", "find_register_movements", "analyze_document_flow"):
        with pytest.raises((AttributeError, TypeError)):
            bsl[helper](None)


# Объект перепись-класса: (папка, RU-множественное, головы префикса, имя, есть ли модуль объекта).
_CENSUS_OBJECTS = {
    "doc": ("Documents", "Документы", ("Документ", "Document", "ДокументСсылка"), "Оплата", True),
    "doc_from": ("Documents", "Документы", ("Документ", "Document"), "Возврат", False),
    "cat": ("Catalogs", "Справочники", ("Справочник", "Catalog"), "Договоры", True),
    "enum": ("Enums", "Перечисления", ("Перечисление", "Enum"), "СтатусыОплаты", False),
    "plan": ("ExchangePlans", "ПланыОбмена", ("ПланОбмена", "ExchangePlan"), "Обмен", False),
    "register": (
        "AccumulationRegisters",
        "РегистрыНакопления",
        ("РегистрНакопления", "AccumulationRegister"),
        "Взаиморасчеты",
        False,
    ),
    "dtype": ("DefinedTypes", "ОпределяемыеТипы", ("ОпределяемыйТип", "DefinedType"), "ДокументыОплаты", False),
}


def _names(rows, key="name"):
    return sorted(str(r.get(key)) for r in rows or [])


# Хелпер: (объект, вызов, проекция ответа, что обязан содержать канонический ответ, есть ли ответ без индекса).
_OBJECT_ARG_HELPERS = {
    "analyze_object": (
        "doc",
        lambda b, a: b["analyze_object"](a),
        lambda r: (r.get("category"), _names(r.get("modules"), "path"), r.get("error")),
        "ObjectModule",
        True,
    ),
    "find_attributes": (
        "doc",
        lambda b, a: b["find_attributes"](object_name=a),
        lambda r: sorted((x["category"], x["object_name"], x["attr_name"]) for x in r),
        "др_Комментарий",
        True,
    ),
    "find_predefined": (
        "cat",
        lambda b, a: b["find_predefined"](object_name=a),
        lambda r: sorted((x["category"], x["object_name"], x["item_name"]) for x in r),
        "Основной",
        True,
    ),
    "find_code_usages": (
        "doc",
        lambda b, a: b["find_code_usages"](a),
        lambda r: (r.get("total"), _names(r.get("usages"), "path")),
        "Платежи",
        True,
    ),
    "find_custom_modifications": (
        "doc",
        lambda b, a: b["find_custom_modifications"](a, custom_prefixes=["др_"]),
        lambda r: (_names(r.get("modifications"), "path"), _names(r.get("custom_attributes")), r.get("error")),
        "др_Комментарий",
        True,
    ),
    "find_functional_options": (
        "doc",
        lambda b, a: b["find_functional_options"](a),
        lambda r: (r.get("object"), _names(r.get("xml_options")), _names(r.get("code_options"))),
        "['ИспользоватьОплату'], ['ИспользоватьОплату']",  # обе корзины: XML-состав и код
        True,
    ),
    "find_print_forms": (
        "doc",
        lambda b, a: b["find_print_forms"](a),
        lambda r: (r.get("object"), _names(r.get("print_forms"))),
        "Счет",
        True,
    ),
    "find_references_to_object": (
        "doc",
        lambda b, a: b["find_references_to_object"](a),
        lambda r: (r.get("object"), r.get("total"), sorted(r.get("by_kind") or {})),
        "based_on",
        True,
    ),
    "find_roles": (
        "doc",
        lambda b, a: b["find_roles"](a),
        lambda r: (r.get("object"), _names(r.get("roles"), "role_name")),
        "Бухгалтер",
        True,
    ),
    "find_templates": (
        "doc",
        lambda b, a: b["find_templates"](owner=a),
        lambda r: _names(r.get("templates")),
        "Счет",
        False,
    ),
    "get_object_modules": (
        "doc",
        lambda b, a: b["get_object_modules"](a),
        lambda r: (r.get("category"), r.get("object_name"), _names(r.get("modules"), "path"), r.get("error")),
        "ObjectModule",
        True,
    ),
    "get_object_profile": (
        "doc",
        lambda b, a: b["get_object_profile"](a),
        lambda r: (
            r.get("category"),
            r.get("object_name"),
            r.get("error"),
            (r.get("_meta") or {}).get("identity_source"),
        ),
        "Documents",
        True,
    ),
    "parse_form": (
        "doc",
        lambda b, a: b["parse_form"](a),
        lambda r: sorted((f["category"], f["object_name"], f["form_name"]) for f in r),
        "ФормаДокумента",
        True,
    ),
    "extract_procedures": ("doc", lambda b, a: b["extract_procedures"](a), lambda r: _names(r), "др_Проверить", True),
    "get_module_outline": (
        "doc",
        lambda b, a: b["get_module_outline"](a),
        lambda r: (r.get("path"), _names(r.get("orphan_methods"))),
        "ObjectModule",
        True,
    ),
    "find_data_path": (
        "doc_from",
        lambda b, a: b["find_data_path"](a, "Документ.Оплата"),
        lambda r: (r.get("found"), r.get("depth"), r.get("error")),
        "True",
        False,
    ),
    "find_enum_values": (
        "enum",
        lambda b, a: b["find_enum_values"](a),
        lambda r: (r.get("name"), _names(r.get("values")), r.get("error")),
        "Оплачено",
        True,
    ),
    "find_exchange_plan_content": (
        "plan",
        lambda b, a: b["find_exchange_plan_content"](a),
        lambda r: _names(r, "ref"),
        "Document.Оплата",
        True,
    ),
    "find_register_writers": (
        "register",
        lambda b, a: b["find_register_writers"](a),
        lambda r: (r.get("register"), _names(r.get("writers"), "document")),
        "Оплата",
        True,
    ),
    "find_defined_types": (
        "dtype",
        lambda b, a: b["find_defined_types"](a),
        lambda r: (r.get("name"), sorted(r.get("types") or [])),
        "Document.Оплата",
        True,
    ),
}


@pytest.mark.parametrize("helper", sorted(_OBJECT_ARG_HELPERS))
def test_object_argument_reads_every_form(doc_domain, with_index, helper):
    """Хелпер с именем объекта: префикс типа в любом регистре (`документ.X`, `document.X`), путь (`Documents/X`,
    `Документы/X`, `documents/X`) и путь описателя или модуля объекта — тот же объект, что `Документ.X`. На
    v1.43.0 часть форм давала ноль, «не найден», неполный ответ (`find_roles` — только роли с флагом,
    `find_functional_options` — без кода) или сбой чтения каталога (`extract_procedures`)."""
    fmt, root, reader = doc_domain
    if helper == "find_templates" and not with_index:
        pytest.skip("макеты — только из индекса: без него ответ `unavailable` на любой форме (сравнение вакуумно)")
    obj, call, view, expected, live_ok = _OBJECT_ARG_HELPERS[helper]
    folder, ru_folder, heads, name, with_module = _CENSUS_OBJECTS[obj]
    bsl = _bsl_for(root, reader if with_index else None)
    canonical = view(call(bsl, f"{heads[0]}.{name}"))
    for arg in _object_forms(fmt, folder, ru_folder, heads, name, with_module):
        assert view(call(bsl, arg)) == canonical, arg
    if with_index or live_ok:  # равенство не вакуумно: канонический ответ содержит обещанное фикстурой
        assert expected in repr(canonical), canonical


@pytest.mark.parametrize(
    ("helper", "arg", "category"),
    [
        ("find_enum_values", "Справочник.Договоры", "Catalogs"),
        ("find_enum_values", "Documents/Оплата", "Documents"),
        ("find_register_writers", "Документ.Оплата", "Documents"),
        ("find_register_writers", "Справочники/Договоры", "Catalogs"),
        ("find_exchange_plan_content", "Catalogs/Договоры", "Catalogs"),
        ("find_exchange_plan_content", "перечисление.СтатусыОплаты", "Enums"),
        ("find_defined_types", "Документ.Оплата", "Documents"),
        ("find_defined_types", "Enums/СтатусыОплаты", "Enums"),
    ],
)
def test_foreign_category_in_a_single_category_helper_is_named(doc_domain, with_index, helper, arg, category):
    """Хелпер одной категории на объекте чужой категории — названная ошибка с категорией и маршрутом. На
    v1.43.0 префикс молча отбрасывался, и искался одноименный объект своей категории."""
    _fmt, root, reader = doc_domain
    with pytest.raises(ValueError) as ei:
        _bsl_for(root, reader if with_index else None)[helper](arg)
    msg = str(ei.value)
    assert f"категории {category}" in msg and "parse_object_xml(" in msg, msg


def test_enum_values_list_mode_isolates_a_foreign_category(doc_domain):
    """Страж списочного режима: чужая категория дает `{error}` под своим ключом, батч не падает."""
    _fmt, root, reader = doc_domain
    res = _bsl_for(root, reader)["find_enum_values"](["Справочник.Договоры", "перечисление.СтатусыОплаты"])
    assert "ValueError" in res["Справочник.Договоры"]["error"]
    assert [v["name"] for v in res["перечисление.СтатусыОплаты"]["values"]] == ["Оплачено"]


@pytest.mark.parametrize("helper", ["analyze_object", "find_custom_modifications"])
def test_type_prefix_is_a_strict_category(doc_domain, with_index, helper):
    """Префикс типа — строгая категория: `Справочник.Оплата` (справочника нет, документ есть) — «не найден в
    категории», а не разбор документа. На v1.43.0 префикс отбрасывался, и отвечал документ."""
    _fmt, root, reader = doc_domain
    bsl = _bsl_for(root, reader if with_index else None)
    kwargs = {"custom_prefixes": ["др_"]} if helper == "find_custom_modifications" else {}
    res = bsl[helper]("Справочник.Оплата", **kwargs)
    assert "в категории Catalogs не найден" in res.get("error", ""), res
    assert "error" not in bsl[helper]("Оплата", **kwargs)  # страж: голое имя — прежний выбор


@pytest.fixture(scope="module")
def doc_domain_ext(doc_domain):
    """Расширение рядом с основной конфигурацией (CF): перехват `&После("ОбработкаПроведения")` у `Оплаты`."""
    _fmt, root, _reader = doc_domain
    ext = root.parent / "ext" / "Доработки"
    _write(ext / "Configuration.xml", CF_EXT_DESCRIPTOR)
    _write(
        ext / "Documents" / "Оплата" / "Ext" / "ObjectModule.bsl",
        '&После("ОбработкаПроведения")\nПроцедура др_ОбработкаПроведенияПосле(Отказ, РежимПроведения)\nКонецПроцедуры\n',
    )
    return ext


@pytest.mark.parametrize("helper", ["get_overrides", "find_ext_overrides"])
def test_override_helpers_read_every_form_of_the_object(doc_domain, doc_domain_ext, helper):
    """Перехваты: та же матрица форм, что у остальных хелперов с именем объекта (путь модуля или описателя
    на v1.43.0 давал молчаливый ноль). Ветка — живая: индекс собран до появления расширения."""
    fmt, root, _reader = doc_domain
    bsl = _bsl_for(root, None, extension_paths=[str(doc_domain_ext)])

    def call(arg):
        if helper == "get_overrides":
            return bsl["get_overrides"](arg)
        return bsl["find_ext_overrides"](str(doc_domain_ext), arg)

    def view(res):
        return sorted((o.get("target_method"), o.get("extension_method")) for o in res.get("overrides") or [])

    canonical = view(call("Документ.Оплата"))
    assert canonical == [("ОбработкаПроведения", "др_ОбработкаПроведенияПосле")]
    for arg in _document_forms(fmt):
        assert view(call(arg)) == canonical, arg


# ═══════════════ Ревью Codex (анстейдж класса «имя объекта») ═══════════════


@pytest.mark.parametrize("helper", ["get_object_full_structure", "analyze_object"])
def test_blank_name_finds_nothing(doc_domain, with_index, helper):
    """Имя из одних пробелов — «не найден», как на v1.43.0: разбор формы не превращает его в пустое имя, на
    котором каскад и подстрочный поиск отдавали первый попавшийся объект конфигурации."""
    _fmt, root, reader = doc_domain
    # v1.45.0 (Д8): прежде здесь стояло «не найден» в ответе — на боевых конфигурациях тот же ввод отдавал
    # объект (регистр). Теперь имя из пробелов — отказ публичной границы с именем вызванного хелпера.
    with pytest.raises(ValueError, match=f"^{helper}:"):
        _bsl_for(root, reader if with_index else None)[helper]("  ")


_DTYPE_HELPERS = {
    "analyze_object": lambda b, a: b["analyze_object"](a),
    "get_object_modules": lambda b, a: b["get_object_modules"](a),
    "find_print_forms": lambda b, a: b["find_print_forms"](a),
    "find_custom_modifications": lambda b, a: b["find_custom_modifications"](a, custom_prefixes=["др_"]),
    "parse_form": lambda b, a: b["parse_form"](a),
    "get_object_profile": lambda b, a: b["get_object_profile"](a),
    "find_attributes": lambda b, a: b["find_attributes"](object_name=a),
    "find_predefined": lambda b, a: b["find_predefined"](object_name=a),
    "find_references_to_object": lambda b, a: b["find_references_to_object"](a),
}


def test_defined_type_forms_reach_every_object_helper(doc_domain, with_index):
    """Определяемый тип — категория без модулей, которую знает разбор аргумента, но не словарь категорий
    `find_module`/`find_by_type`: префикс и путь не роняют хелперы, ищущие модули (было `ValueError` «категория
    неизвестна»), а структура находит тип так же, как по голому имени (на v1.43.0 — «не найден»)."""
    fmt, root, reader = doc_domain
    bsl = _bsl_for(root, reader if with_index else None)
    forms = _object_forms(
        fmt, "DefinedTypes", "ОпределяемыеТипы", ("ОпределяемыйТип", "DefinedType"), "ДокументыОплаты", False
    )
    bare = bsl["get_object_full_structure"]("ДокументыОплаты")
    assert bare["category"] == "DefinedTypes"
    for arg in forms:
        res = bsl["get_object_full_structure"](arg)
        assert (res.get("category"), res.get("object_name")) == ("DefinedTypes", "ДокументыОплаты"), (arg, res)
        for helper, call in _DTYPE_HELPERS.items():
            call(bsl, arg)  # без исключения: модульный этап у категории без модулей пуст


@pytest.mark.parametrize(
    ("helper", "arg"),
    [
        ("find_register_movements", "Справочник.Договоры"),
        ("find_register_movements", "HTTPService.API"),
        ("find_register_movements", "РегистрНакопления.Взаиморасчеты"),
        ("find_enum_values", "ОпределяемыйТип.ДокументыОплаты"),
        ("find_enum_values", "CommonTemplates/Счет"),
        ("find_enum_values", "HTTPService.Пустой"),
        ("find_defined_types", "HTTPServices/API"),
    ],
)
def test_named_error_route_is_executable(doc_domain, with_index, helper, arg):
    """Маршрут из текста названной ошибки исполним для любого вида объекта: описатель (`parse_object_xml`)
    читается у всех видов, с индексом и без. Профиль и структура находили не все (`HTTPService.X`, общий макет,
    HTTP-сервис без модуля — «не найден»), поэтому маршрутом к ним быть не могли."""
    _fmt, root, reader = doc_domain
    bsl = _bsl_for(root, reader if with_index else None)
    with pytest.raises(ValueError) as ei:
        bsl[helper](arg)
    calls = re.findall(r"(parse_object_xml|find_register_writers)\('([^']+)'\)", str(ei.value))
    assert calls and calls[0][0] == "parse_object_xml", str(ei.value)
    for fn, call_arg in calls:
        res = bsl[fn](call_arg)
        assert res and not res.get("error"), (fn, call_arg, res)


def test_category_hint_keeps_the_module_normalizer(tmp_path):
    """Подсказка категории в любом регистре — как у прежнего нормализатора `find_module` (`'documents'` —
    документ, а не первый попавшийся омоним), опечатка — прежний `ValueError` «категория неизвестна»; пустым
    поиск модулей становится только у категории без модулей (находка ревью; подсказка `DefinedTypes` — см.
    `test_defined_type_hint_answers_like_any_category`)."""
    root = tmp_path / "cf"
    _write(root / "Configuration.xml", CF_DESCRIPTOR)
    _write(root / "CommonModules" / "Shared" / "Ext" / "Module.bsl", PROC)
    _write(root / "Documents" / "Shared" / "Ext" / "ObjectModule.bsl", PROC)
    bsl = _bsl_for(root)
    assert bsl["get_object_full_structure"]("Shared", category_hint="documents")["category"] == "Documents"
    with pytest.raises(ValueError, match="неизвестна"):
        bsl["get_object_full_structure"]("Shared", category_hint="Typo")


@pytest.mark.parametrize("arg", ["Documents///", "Documents/.xml", "Documents/.mdo"])
def test_path_without_object_name_is_not_parsed(doc_domain, with_index, arg):
    """Путь без имени объекта не разбирается в пустое имя — иначе фильтр объекта снимался, и приходили реквизиты
    всех документов; ответ — как на v1.43.0 (находка ревью)."""
    _fmt, root, reader = doc_domain
    assert _bsl_for(root, reader if with_index else None)["find_attributes"](object_name=arg) == []


def test_named_error_route_uses_the_dump_spelling(doc_domain, with_index):
    """Имя в маршруте — в написании выгрузки, а не аргумента: на ФС Linux `parse_object_xml('Catalogs/договоры')`
    не нашел бы `Договоры.xml`."""
    _fmt, root, reader = doc_domain
    with pytest.raises(ValueError) as ei:
        _bsl_for(root, reader if with_index else None)["find_enum_values"]("справочник.договоры")
    assert "parse_object_xml('Catalogs/Договоры')" in str(ei.value), str(ei.value)


# ═══════════════ Ревью Codex, круг 3: новый разбор меняет ответ только на корректной ссылке ═══════════════


def _route_in(exc: pytest.ExceptionInfo) -> str:
    match = re.search(r"parse_object_xml\('([^']+)'\)", str(exc.value))
    assert match, str(exc.value)
    return match.group(1)


@pytest.mark.parametrize("fmt", ["cf", "edt"])
def test_named_error_route_follows_the_current_root(tmp_path, fmt):
    """Сессия-обертка (base шире текущего корня конфигурации): путь маршрута — от текущего корня; прежде
    `parse_object_xml('Catalogs/Товары')` падал `FileNotFoundError`."""
    wrap = tmp_path / "wrap"
    ext = wrap / "Расширение"
    if fmt == "cf":
        _write(ext / "Configuration.xml", CF_EXT_DESCRIPTOR)
        _write(ext / "Catalogs" / "Товары.xml", _cf_md("Catalog", "Товары"))
        _write(ext / "Catalogs" / "Товары" / "Ext" / "ObjectModule.bsl", PROC)
    else:
        _write(ext / "Configuration" / "Configuration.mdo", EDT_DESCRIPTOR)
        _write(ext / "Catalogs" / "Товары" / "Товары.mdo", _edt_md("Catalog", "Товары"))
        _write(ext / "Catalogs" / "Товары" / "ObjectModule.bsl", PROC)
    bsl = _bsl_for(
        wrap,
        None,
        current_config_role=ConfigRole.EXTENSION,
        current_config_name="Доработки",
        current_config_root=str(ext),
    )
    with pytest.raises(ValueError) as ei:
        bsl["find_enum_values"]("Справочник.Товары")
    route = _route_in(ei)
    assert route == "Расширение/Catalogs/Товары"
    assert bsl["parse_object_xml"](route).get("name") == "Товары"


def test_named_error_route_does_not_depend_on_catalog_warmup(tmp_path):
    """Объект только расширения: маршрут одинаков до и после прогрева каталога сессии (прежде до прогрева
    объекты расширений не были видны, и в маршрут шло написание аргумента) и исполним."""
    main = tmp_path / "src" / "cf"
    cfe = tmp_path / "src" / "cfe" / "Доработки"
    _write(main / "Configuration.xml", CF_MAIN_DESCRIPTOR)
    _write(main / "Catalogs" / "Склад" / "Ext" / "ObjectModule.bsl", PROC)
    _write(cfe / "Configuration.xml", CF_EXT_DESCRIPTOR)
    _write(cfe / "Catalogs" / "ExtCatalogs.xml", _cf_md("Catalog", "ExtCatalogs"))
    (cfe / "Catalogs" / "ExtCatalogs").mkdir(parents=True)
    cold = _bsl_for(main, None, extension_paths=[str(cfe)])
    with pytest.raises(ValueError) as cold_err:
        cold["find_enum_values"]("Catalog.extcatalogs")
    warm = _bsl_for(main, None, extension_paths=[str(cfe)])
    warm["find_module"]("Склад")
    with pytest.raises(ValueError) as warm_err:
        warm["find_enum_values"]("Catalog.extcatalogs")
    assert _route_in(cold_err) == _route_in(warm_err)
    assert cold["parse_object_xml"](_route_in(cold_err)).get("name") == "ExtCatalogs"


def test_malformed_paths_keep_the_previous_answer(doc_domain):
    """Не-ссылка (путь без имени объекта) — прежним разборщиком каждого хелпера, ответ как на v1.43.0:
    `parse_form` — `[]` (а не `ValueError`), `find_print_forms` и `get_object_modules` разбирают `Documents///`
    как прежний `_split_object_arg` (объект `//`), `get_module_outline` считает описатель без имени путем,
    а не именем объекта."""
    _fmt, root, _reader = doc_domain
    bsl = _bsl_for(root)
    assert bsl["parse_form"]("Documents/.xml") == []
    assert bsl["parse_form"]("Documents/.mdo") == []
    # v1.45.0 (Д8): прежде здесь стояло — `find_print_forms` отвечал объектом `//`, `get_object_modules` —
    # «'//' в категории Documents». Путь категории без имени теперь — отказ публичной границы.
    with pytest.raises(ValueError, match="^find_print_forms:"):
        bsl["find_print_forms"]("Documents///")
    with pytest.raises(ValueError, match="^get_object_modules:"):
        bsl["get_object_modules"]("Documents///")
    outline = bsl["get_module_outline"]("Documents/.xml", no_live=True)
    assert outline["_meta"]["resolved_from_name"] is False


# ═══════════════ Ревью Codex, круг 4: корректная ссылка — категория и имя-идентификатор ═══════════════

# Не-ссылка и ответ v1.43.0 на нее. Найдены переписью «хелперы с именем объекта × вырожденные входы» против
# состояния до анстейджа; до правки каждая форма давала чужой ответ.
_NON_REFERENCE_ANSWERS = [
    # префикс без имени: v1.45.0 (Д8) — прежде здесь стояло («document.», 0, 0) — ноль; теперь отказ публичной
    # границы с именем хелпера (обзорный хелпер отказывает префиксу без имени)
    ("find_functional_options", "document.", None, ValueError),
    # префикс не объекта метаданных не снимается (до правки — имя `Продажи`)
    ("find_functional_options", "Подсистема.Продажи", lambda r: r["object"], "Подсистема.Продажи"),
    # сегмент из пробелов — не имя объекта (до правки — вхождения всех документов, все подписки на документы)
    ("find_code_usages", "Documents/ /", lambda r: (r["object"], r["total"]), ("Documents/ /", 0)),
    # v1.46.0: прежде здесь стояло ["ВсеДокументы"] — строка подписки на набор документов (`scope='set'`): сегмент из
    # пробелов — не-ссылка, которой не соответствует ни один объект, и подписки на наборы к ней не раскрываются (Д3).
    ("find_event_subscriptions", "Documents/ /", lambda r: sorted(x["name"] for x in r), []),
    (
        "get_object_full_structure",
        "Documents/ /Оплата",
        lambda r: r.get("error"),
        "Объект 'Documents/ /Оплата' не найден",
    ),
    (
        "get_object_full_structure",
        "Documents/Оплата /",
        lambda r: r.get("error"),
        "Объект 'Documents/Оплата /' не найден",
    ),
    # `/` после префикса с точкой — не путь от категории (до правки — движения документа `Оплата`)
    ("find_register_movements", "документ.Оплата/Ext/ObjectModule.bsl", lambda r: r["code_registers"], []),
    # ссылка на член объекта — не ссылка на объект: без категории и без названной ошибки
    (
        "analyze_object",
        "Документ.Оплата.ТабличнаяЧасть.Товары",
        lambda r: r.get("error"),
        "Объект 'Оплата.ТабличнаяЧасть.Товары' не найден",
    ),
    ("find_register_movements", "Справочник.Договоры.Реквизит.Х", lambda r: r["code_registers"], []),
    (
        "find_enum_values",
        "справочник.Договоры.Х",
        lambda r: r.get("error"),
        "Перечисление 'справочник.Договоры.Х' не найдено",
    ),
    ("find_roles", "документ.Оплата.Реквизит.Сумма", lambda r: r["object"], "документ.Оплата.Реквизит.Сумма"),
    # v1.45.0 (Д8): прежде здесь стояло name == '' (хелпер снимал свой префикс); префикс без имени — отказ
    ("find_defined_types", "DefinedType.", None, ValueError),
]


@pytest.mark.parametrize(
    ("helper", "arg", "view", "expected"),
    _NON_REFERENCE_ANSWERS,
    ids=[f"{helper}:{arg}" for helper, arg, _view, _expected in _NON_REFERENCE_ANSWERS],
)
def test_non_reference_keeps_the_previous_answer(doc_domain, with_index, helper, arg, view, expected):
    """Новый разбор меняет ответ только на корректной ссылке — категория и имя-идентификатор 1С. Иная форма —
    сегмент из пробелов, префикс без имени, `/` после префикса с точкой, ссылка на член объекта — прежним
    разбором места вызова, ответ v1.43.0 (находки ревью и перепись)."""
    _fmt, root, reader = doc_domain
    if expected is ValueError:  # v1.45.0 (Д8): аргумент без имени объекта — отказ публичной границы
        with pytest.raises(ValueError, match=f"^{helper}:"):
            _bsl_for(root, reader if with_index else None)[helper](arg)
        return
    assert view(_bsl_for(root, reader if with_index else None)[helper](arg)) == expected


@pytest.mark.parametrize("helper", ["find_attributes", "find_predefined"])
def test_non_string_object_name_fails_as_before(doc_domain, helper):
    """Не-строка падает тем же исключением, что на v1.43.0: прежний разбор первым проверял `/` в имени
    (`TypeError`), до правки — `AttributeError` из нового разбора."""
    _fmt, root, reader = doc_domain
    with pytest.raises(TypeError):
        _bsl_for(root, reader)[helper](object_name=1)


def test_document_flow_keeps_a_path_outside_the_categories(tmp_path):
    """Путь не от категории (`src/cf/Documents/X` — с префиксом каталога репозитория) — не ссылка: композит
    дописывает к нему `Документ.`, и до правки разбор резал остаток до первого сегмента — печатные формы и
    основания документа `src`. Ответ — прежний: документа с таким именем нет."""
    root = tmp_path / "cf"
    _document_domain_tree(root, "cf")
    _write(root / "Documents" / "src.xml", _cf_md("Document", "src"))
    _write(root / "Documents" / "src" / "Ext" / "ManagerModule.bsl", _PF_MANAGER)
    flow = _bsl_for(root)["analyze_document_flow"]("src/cf/Documents/Оплата")
    assert flow["print_forms"]["object"] == "src/cf/Documents/Оплата"
    assert flow["print_forms"]["print_forms"] == []
    assert flow["based_on"]["document"] == "src/cf/Documents/Оплата"


# ═══════════════ Ревью Codex, круг 5: гибрид — не ссылка, и внутри композита тоже ═══════════════

# Гибрид — префикс с точкой, приклеенный к другой ссылке или к суффиксу описателя: не ссылка, ответ v1.43.0.
# Проекция композита: (document, регистры кода, подписки, объект печатных форм).
_HYBRID_FLOW_ANSWERS = [
    ("Документ.Catalogs/Договоры", ("Договоры", [], ["ВсеДокументы"], "Catalogs/Договоры")),
    ("Документ.Справочник.Договоры", ("Договоры", [], ["ПриЗаписиДоговора"], "Договоры")),
    ("документ.документ.Оплата", ("документ.документ.Оплата", [], ["ВсеДокументы"], "документ.документ.Оплата")),
    ("Document.Documents/Оплата", ("Оплата", [], ["ВсеДокументы"], "Documents/Оплата")),
]


def _flow_brief(res: dict) -> tuple:
    return (
        res["document"],
        sorted(r["name"] for r in res["register_movements"]["code_registers"]),
        sorted(s["name"] for s in res["event_subscriptions"]),
        res["print_forms"]["object"],
    )


@pytest.mark.parametrize(("arg", "expected"), _HYBRID_FLOW_ANSWERS, ids=[arg for arg, _ in _HYBRID_FLOW_ANSWERS])
def test_document_flow_on_a_hybrid_keeps_the_previous_answer(doc_domain, with_index, arg, expected):
    """Внешний аргумент не ссылка — весь композит прежним разбором (`_previous_parsing`): дочерний хелпер не
    разбирает производное имя (`Catalogs/X` из `Документ.Catalogs/X`) как ссылку. До правки — названная
    ошибка `find_register_movements` о справочнике или ответ по другому объекту вместо ответа v1.43.0."""
    _fmt, root, reader = doc_domain
    assert _flow_brief(_bsl_for(root, reader if with_index else None)["analyze_document_flow"](arg)) == expected


def test_previous_parsing_ends_with_the_composite(doc_domain, with_index):
    """Режим прежнего разбора живет только внутри вызова композита: следующий вызов в той же сессии разбирает
    корректную ссылку по-новому."""
    _fmt, root, reader = doc_domain
    bsl = _bsl_for(root, reader if with_index else None)
    bsl["analyze_document_flow"]("Документ.Catalogs/Договоры")
    movements = bsl["find_register_movements"]("документ.Оплата")
    assert sorted(r["name"] for r in movements["code_registers"]) == ["Взаиморасчеты"]


@pytest.mark.parametrize(
    "arg", ["Документ.Оплата.xml", "документ.Оплата.xml", "Document.Оплата.mdo", "Документ.Оплата.XML"]
)
def test_parse_form_keeps_a_suffix_after_a_type_prefix(doc_domain, with_index, arg):
    """Суффикс описателя снимается только у корректной ссылки-пути (`Documents/X.xml`); у префикса с точкой
    (`Документ.X.xml`) — прежний ответ `[]` (до правки — формы документа X)."""
    _fmt, root, reader = doc_domain
    assert _bsl_for(root, reader if with_index else None)["parse_form"](arg) == []


# ═══════════════ Ревью Codex, круг 6: профиль — композит; обход категории без модулей — только из ссылки ═══════════════


@pytest.mark.parametrize(
    ("arg", "handler"),
    [
        ("Документ.Catalogs/Договоры", "CommonModule.События.ВсеДокументы"),
        ("Документ.Справочник.Договоры", "CommonModule.События.ПриЗаписиДоговора"),
    ],
)
def test_profile_flow_on_a_hybrid_keeps_the_previous_answer(doc_domain, arg, handler):
    """Профиль — тоже композит: без индекса секция flow зовет analyze_document_flow с остатком после префикса
    (`Catalogs/Договоры` из `Документ.Catalogs/Договоры`). Аргумент профиля — не ссылка, поэтому секции идут
    прежним разбором (`_previous_parsing`): ответ — как в 1.43.0 (остаток разбирается `_strip_meta_prefix`), а не
    названная ошибка «не документ» (до правки — status='error'). Режим кончается вместе с профилем."""
    _fmt, root, _reader = doc_domain
    bsl = _bsl_for(root)
    flow = bsl["get_object_profile"](arg, include_flow=True)["sections"]["flow"]
    assert flow["status"] == "ok", flow
    assert flow["items"] == [{"event": "BeforeWrite", "handler": handler}], flow
    movements = bsl["find_register_movements"]("документ.Оплата")
    assert sorted(r["name"] for r in movements["code_registers"]) == ["Взаиморасчеты"]


def test_defined_type_hint_answers_like_any_category(doc_domain, with_index):
    """Давнее (так же на v1.43.0): подсказка `category_hint='DefinedTypes'` в любом регистре падала отказом
    «категория неизвестна» — модулей у определяемого типа нет, и поиск модулей каскада его категорию не принимал;
    тем же отказом отвечала секция структуры профиля голого имени определяемого типа (подсказку там передает сам
    профиль). Теперь поиск модулей такой категории пуст, а подсказка работает как любая другая: объекта в
    категории нет — поиск без фильтра."""
    _fmt, root, reader = doc_domain
    bsl = _bsl_for(root, reader if with_index else None)
    for hint in ("DefinedTypes", "definedtypes"):
        res = bsl["get_object_full_structure"]("ДокументыОплаты", category_hint=hint)
        assert (res.get("category"), res.get("object_name")) == ("DefinedTypes", "ДокументыОплаты"), res
        assert bsl["get_object_full_structure"]("Оплата", category_hint=hint)["category"] == "Documents"
    assert bsl["get_object_full_structure"]("ОпределяемыйТип.ДокументыОплаты")["category"] == "DefinedTypes"
    if with_index:
        prof = bsl["get_object_profile"]("ДокументыОплаты")
        assert prof["category"] == "DefinedTypes" and prof["sections"]["structure"]["status"] == "ok", prof


# ═══════════════ Ревью Codex, круг 7: имя-идентификатор по правилам 1С; профиль и виды вне карты префиксов ═══════════════

# Прочие числовые символы Юникода (`²` — No, `Ⅻ` — Nl, `٣` — цифра другой письменности) именем объекта 1С не являются:
# такая форма — не ссылка, ответ — как на стейдже (до правки — названная ошибка о чужой категории).
_UNICODE_NUMBER_ANSWERS = [
    ("find_enum_values", "Catalogs/²", lambda r: r.get("error"), "Перечисление 'Catalogs/²' не найдено"),
    (
        "find_enum_values",
        "Catalogs/Договоры²",
        lambda r: r.get("error"),
        "Перечисление 'Catalogs/Договоры²' не найдено",
    ),
    ("find_enum_values", "Справочник.Договоры٣", lambda r: r.get("error"), "Перечисление 'Договоры٣' не найдено"),
    ("find_register_movements", "Справочник.Ⅻ", lambda r: (r["document"], r["code_registers"]), ("Ⅻ", [])),
]


@pytest.mark.parametrize(
    ("helper", "arg", "view", "expected"),
    _UNICODE_NUMBER_ANSWERS,
    ids=[f"{helper}:{arg}" for helper, arg, _view, _expected in _UNICODE_NUMBER_ANSWERS],
)
def test_unicode_number_is_not_an_object_name(doc_domain, with_index, helper, arg, view, expected):
    """Имя объекта 1С — буква или `_`, дальше буквы, цифры 0–9, `_`. Прежняя проверка (класс «символ слова» из `re`)
    принимала `²`, `½`, `Ⅻ` и цифры других письменностей, и такая строка считалась ссылкой (находка ревью)."""
    _fmt, root, reader = doc_domain
    assert view(_bsl_for(root, reader if with_index else None)[helper](arg)) == expected


def test_ascii_digits_and_underscore_stay_in_the_name(doc_domain, with_index):
    """Страж: цифры 0–9 и `_` — часть идентификатора, такая форма — по-прежнему ссылка (чужая категория в хелпере
    одной категории — названная ошибка)."""
    _fmt, root, reader = doc_domain
    bsl = _bsl_for(root, reader if with_index else None)
    for arg in ("Справочник.Договоры2", "Catalogs/_Договоры", "Справочник.Договоры_2"):
        with pytest.raises(ValueError, match="категории Catalogs"):
            bsl["find_enum_values"](arg)


@pytest.mark.parametrize(
    ("arg", "category", "name"),
    [
        ("DefinedType.ДокументыОплаты", "DefinedTypes", "ДокументыОплаты"),
        ("ОпределяемыйТип.ДокументыОплаты", "DefinedTypes", "ДокументыОплаты"),
        ("DefinedTypes/ДокументыОплаты", "DefinedTypes", "ДокументыОплаты"),
        ("HTTPСервис.API", "HTTPServices", "API"),
    ],
)
def test_profile_reads_kinds_unknown_to_its_prefix_map(doc_domain, arg, category, name):
    """Давнее (так же на v1.43.0): разбор префикса профиля знал только головы своей карты подсказок, и ссылка на
    определяемый тип или HTTP-сервис давала «не найден». Корректная ссылка — категория и имя из ее разбора; секция
    структуры определяемого типа отвечает (см. `test_defined_type_hint_answers_like_any_category`)."""
    _fmt, root, reader = doc_domain
    prof = _bsl_for(root, reader)["get_object_profile"](arg)
    assert (prof.get("category"), prof.get("object_name"), prof["_meta"]["identity_source"]) == (
        category,
        name,
        "index_prefix",
    ), prof
    assert prof["sections"]["structure"]["status"] == "ok", prof["sections"]["structure"]
    live = _bsl_for(root)["get_object_profile"](arg)
    assert (live.get("category"), live.get("object_name"), live["_meta"]["identity_source"]) == (
        category,
        name,
        "input_prefix",
    ), live


def test_profile_common_form_prefix_is_its_category(homonym_forms, monkeypatch):
    """Давнее: `ОбщаяФорма.X` при одноименном справочнике профиль отвечал справочником — префикс срезался, и объект
    искался по имени в любой категории. Страж: `Справочник.X` — справочник."""
    reader = _index_for_forms(homonym_forms, monkeypatch)
    try:
        bsl = _bsl_for(homonym_forms, reader)
        prof = bsl["get_object_profile"]("ОбщаяФорма.Группы")
        assert (prof["category"], prof["_meta"]["identity_source"]) == ("CommonForms", "index_prefix"), prof
        assert bsl["get_object_profile"]("Справочник.Группы")["category"] == "Catalogs"
    finally:
        reader.close()


# ═══════════════ Ревью Codex, круг 8: объект за потолком find_module, маршрут-литерал, порядок, русские головы ═══════════════


@pytest.fixture(scope="module", params=["cf", "edt"])
def cap_domain(request, tmp_path_factory):
    """`_document_domain_tree` и по 51 приманке: документы `А00_Оплата`… и общие модули `А00_Платежи`… — в каталоге
    они идут раньше `Оплата`/`Платежи` (кириллическая `А`), и первые 50 подстрочных совпадений `find_module` точного
    объекта не содержат."""
    fmt = request.param
    root = tmp_path_factory.mktemp(f"capdomain_{fmt}") / fmt
    _document_domain_tree(root, fmt)
    for i in range(51):
        doc, mod = f"А{i:02d}_Оплата", f"А{i:02d}_Платежи"
        if fmt == "cf":
            _write(root / "Documents" / f"{doc}.xml", _cf_md("Document", doc))
            _write(root / "Documents" / doc / "Ext" / "ObjectModule.bsl", PROC)
            _write(root / "CommonModules" / f"{mod}.xml", _cf_md("CommonModule", mod))
            _write(root / "CommonModules" / mod / "Ext" / "Module.bsl", PROC)
        else:
            _write(root / "Documents" / doc / f"{doc}.mdo", _edt_md("Document", doc))
            _write(root / "Documents" / doc / "ObjectModule.bsl", PROC)
            _write(root / "CommonModules" / mod / f"{mod}.mdo", _edt_md("CommonModule", mod))
            _write(root / "CommonModules" / mod / "Module.bsl", PROC)
    mp = pytest.MonkeyPatch()
    mp.setenv("RLM_INDEX_DIR", str(root.parent / "idx"))
    try:
        reader = IndexReader(IndexBuilder().build(str(root), build_calls=False, build_metadata=True, build_fts=False))
    finally:
        mp.undo()
    yield fmt, root, reader
    reader.close()


@pytest.mark.parametrize("arg", ["Документ.Оплата", "документ.Оплата", "Documents/Оплата", "Оплата"])
def test_exact_object_past_the_find_module_cap(cap_domain, with_index, arg):
    """Точное имя искалось на странице из 50 подстрочных совпадений `find_module`: на ссылке — ложный отказ «не найден
    в категории», на голом имени (давнее) — разбор чужих документов. Теперь точные модули берутся полным проходом
    каталога, когда страница уперлась в потолок (находка ревью)."""
    _fmt, root, reader = cap_domain
    bsl = _bsl_for(root, reader if with_index else None)
    res = bsl["analyze_object"](arg)
    assert (res.get("category"), res.get("name")) == ("Documents", "Оплата"), res
    assert {m["path"].split("/")[1] for m in res["modules"]} == {"Оплата"} and len(res["modules"]) == 3
    mods = bsl["find_custom_modifications"](arg, custom_prefixes=["др_"])
    assert "др_Проверить" in repr(mods.get("modifications")), mods
    assert "др_Комментарий" in repr(mods.get("custom_attributes")), mods


@pytest.mark.parametrize("arg", ["ОбщийМодуль.Платежи", "CommonModules/Платежи", "Платежи"])
def test_exact_common_module_past_the_find_module_cap(cap_domain, with_index, arg):
    """Каскад резолва: у общего модуля нет живого прохода по каталогу категории, и точный объект за потолком страницы
    `find_module` терялся — «не найден в категории» или чужой модуль `А00_Платежи` (давнее для `ОбщийМодуль.X` и
    голого имени; путь до правки отвечал верно лишь по совпадению подстроки пути)."""
    _fmt, root, reader = cap_domain
    bsl = _bsl_for(root, reader if with_index else None)
    res = bsl["get_object_full_structure"](arg)
    assert (res.get("category"), res.get("object_name")) == ("CommonModules", "Платежи"), res
    if with_index:
        prof = bsl["get_object_profile"](arg)
        assert (prof.get("category"), prof.get("object_name")) == ("CommonModules", "Платежи"), prof


@pytest.mark.parametrize("fmt", ["cf", "edt"])
def test_named_error_route_is_a_python_literal(tmp_path, fmt):
    """Маршрут из текста ошибки — литерал Python: корень с апострофом (`O'Brien/…`) не ломает вызов (находка ревью:
    `parse_object_xml('O'Brien/Catalogs/Товары')` давал SyntaxError)."""
    wrap = tmp_path / "wrap"
    ext = wrap / "O'Brien"
    if fmt == "cf":
        _write(ext / "Configuration.xml", CF_EXT_DESCRIPTOR)
        _write(ext / "Catalogs" / "Товары.xml", _cf_md("Catalog", "Товары"))
        _write(ext / "Catalogs" / "Товары" / "Ext" / "ObjectModule.bsl", PROC)
    else:
        _write(ext / "Configuration" / "Configuration.mdo", EDT_DESCRIPTOR)
        _write(ext / "Catalogs" / "Товары" / "Товары.mdo", _edt_md("Catalog", "Товары"))
        _write(ext / "Catalogs" / "Товары" / "ObjectModule.bsl", PROC)
    bsl = _bsl_for(
        wrap,
        None,
        current_config_role=ConfigRole.EXTENSION,
        current_config_name="Доработки",
        current_config_root=str(ext),
    )
    with pytest.raises(ValueError) as ei:
        bsl["find_enum_values"]("Справочник.Товары")
    call = re.search(r"parse_object_xml\(.*?\)(?=[;.]|$)", str(ei.value)).group(0)
    node = ast.parse(call, mode="eval").body  # подсказка — синтаксически верный вызов
    assert bsl["parse_object_xml"](ast.literal_eval(node.args[0])).get("name") == "Товары"


def test_extension_rows_order_does_not_depend_on_the_hash_seed(tmp_path):
    """Давнее (этап 1 e2e): порядок категорий у строк расширений шел из обхода множества и зависел от сида хеширования
    процесса — на усеченной странице (`find_attributes`, квота `search()`) от воркера зависело, какие строки расширений
    в нее попадут. Порядок постоянный, набор строк тот же."""
    main = tmp_path / "src" / "cf"
    ext = tmp_path / "src" / "cfe" / "Доработки"
    _write(main / "Configuration.xml", CF_MAIN_DESCRIPTOR)
    _write(main / "CommonModules" / "Общий" / "Ext" / "Module.bsl", PROC)
    _write(ext / "Configuration.xml", CF_EXT_DESCRIPTOR)
    kinds = ("Catalog", "Document", "InformationRegister", "ChartOfCharacteristicTypes", "BusinessProcess")
    folders = ("Catalogs", "Documents", "InformationRegisters", "ChartsOfCharacteristicTypes", "BusinessProcesses")
    for folder, kind in zip(folders, kinds):
        _write(ext / folder / f"др_{kind}.xml", _cf_md(kind, f"др_{kind}", children=_CF_ATTR))
        (ext / folder / f"др_{kind}").mkdir(parents=True, exist_ok=True)
    child = textwrap.dedent(f"""\
        import json
        from rlm_tools_bsl.bsl_helpers import make_bsl_helpers
        from rlm_tools_bsl.format_detector import detect_format
        from rlm_tools_bsl.helpers import make_helpers
        base = {str(main)!r}
        helpers, resolve_safe = make_helpers(base, None)
        bsl = make_bsl_helpers(
            base_path=base, resolve_safe=resolve_safe, read_file_fn=helpers["read_file"], grep_fn=helpers["grep"],
            glob_files_fn=helpers["glob_files"], format_info=detect_format(base), idx_reader=None,
            extension_paths=[{str(ext)!r}],
        )
        rows = bsl["find_attributes"](name="Сумма")
        print(json.dumps([(r["category"], r["object_name"]) for r in rows], ensure_ascii=False))
    """)
    orders = {
        subprocess.run(
            [sys.executable, "-X", "utf8", "-c", child],
            env={**os.environ, "PYTHONHASHSEED": str(seed)},
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        ).stdout
        for seed in range(4)
    }
    assert len(orders) == 1, orders
    assert len(json.loads(orders.pop())) == len(kinds)


@pytest.mark.parametrize(
    "helper", ["find_event_subscriptions", "find_references_to_object", "find_code_usages", "find_functional_options"]
)
def test_russian_head_of_a_nonref_kind_equals_the_english_one(doc_domain, with_index, helper):
    """Русская голова вида без ссылочного типа (`ОбщийМодуль.X`, `HTTPСервис.X`) у хелперов с разбором
    `_normalize_object_ref` — тот же ответ, что английская (`CommonModule.X`): тот разбор русских голов этих видов не
    знает, и прежде такая форма уходила в отбор как есть (`find_event_subscriptions('ОбщийМодуль.X')` отдавал подписку
    на набор `ВсеДокументы`, а `'CommonModule.X'` — ничего)."""
    _fmt, root, reader = doc_domain
    bsl = _bsl_for(root, reader if with_index else None)
    for ru, en in (("ОбщийМодуль.Платежи", "CommonModule.Платежи"), ("HTTPСервис.API", "HTTPService.API")):
        assert bsl[helper](ru) == bsl[helper](en), (ru, en)


def test_closure_names_do_not_shadow_module_names():
    """Растяжка класса: имя внутри `make_bsl_helpers` не перекрывает имя уровня модуля. Перекрытое имя молча меняло
    поведение всех вызовов внутри замыкания: замыкательный `_exact_object_rows` (точные модули по странице
    `find_module`) подменил модульный фильтр строк ридера по объекту и категории, и формы омонимов смешались."""
    tree = ast.parse(inspect.getsource(bsl_helpers_module))
    top: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            top.add(node.name)
        elif isinstance(node, ast.Assign):
            top.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            top.add(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            top.update((a.asname or a.name).split(".")[0] for a in node.names)
    factory = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "make_bsl_helpers")
    inner: set[str] = set()
    for node in factory.body:
        if isinstance(node, ast.FunctionDef):
            inner.add(node.name)
        elif isinstance(node, ast.Assign):
            inner.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            inner.add(node.target.id)
    assert top & inner == set(), sorted(top & inner)


# ═══════════════ Ревью Codex, стейдж: не-ссылка в композите и в parse_form — выражением 1.43.0 ═══════════════

# Ответ v1.43.0 (снят исполнением на HEAD): (document, регистры кода, подписки, объект печатных форм, имя метаданных,
# документы оснований).
_FLOW_NON_REFERENCE_ANSWERS = [
    # v1.45.0 (Д8): прежде здесь стоял ответ по пустому имени (все подписки) — теперь отказ публичной границы
    ("  ", ValueError),
    (
        "document.Справочник.Договоры",
        ("document.Справочник.Договоры", [], ["ВсеДокументы"], "document.Справочник.Договоры", None, []),
    ),
    ("  Оплата  ", ("  Оплата  ", [], ["ВсеДокументы", "ПриЗаписиОплаты"], "  Оплата", None, ["Возврат"])),
]


@pytest.mark.parametrize(
    ("arg", "expected"), _FLOW_NON_REFERENCE_ANSWERS, ids=[arg for arg, _ in _FLOW_NON_REFERENCE_ANSWERS]
)
def test_document_flow_non_reference_keeps_the_previous_answer(doc_domain, with_index, arg, expected):
    """Не-ссылка разбирается выражением 1.43.0 (`_strip_meta_prefix`, без снятия пробелов). До правки пробельный
    ввод становился пустым именем и отвечал первым объектом каталога (метаданные `Договоры`, движения и основания
    чужих документов), `document.Справочник.Договоры` — справочником, а у `  Оплата  ` пропадали основания (находка
    ревью)."""
    _fmt, root, reader = doc_domain
    if expected is ValueError:
        with pytest.raises(ValueError, match="^analyze_document_flow:"):
            _bsl_for(root, reader if with_index else None)["analyze_document_flow"](arg)
        return
    res = _bsl_for(root, reader if with_index else None)["analyze_document_flow"](arg)
    got = (
        res["document"],
        sorted(r["name"] for r in res["register_movements"]["code_registers"]),
        sorted(s["name"] for s in res["event_subscriptions"]),
        res["print_forms"]["object"],
        (res.get("metadata") or {}).get("name"),
        [d["document"] for d in res["based_on"]["can_create_from_here"]],
    )
    assert got == expected


@pytest.mark.parametrize(
    "arg", ["Documents///", "документ.Оплата/Forms/ФормаДокумента", "   ", "  Оплата  ", "Документ.Documents/Оплата"]
)
def test_parse_form_non_reference_keeps_the_previous_answer(doc_domain, with_index, arg):
    """Не-ссылка в `parse_form` — разбором 1.43.0: категория и путь формы — только у корректной ссылки. До правки
    `Documents///` падал распаковкой пустого пути, `документ.Оплата/Forms/…` и `  Оплата  ` находили форму, `   `
    давал `ValueError`, а гибрид `Документ.Documents/Оплата` терял форму, которую 1.43.0 находил широким отбором
    (без индекса; с индексом широкого отбора нет — объявлено, на 1.43.0 индексная ветка здесь падала)."""
    _fmt, root, reader = doc_domain
    if arg in ("Documents///", "   "):
        # v1.45.0 (Д8): прежде здесь стоял пустой ответ — путь категории и пробельное имя без имени объекта
        # теперь отвергает собственная проверка parse_form, как пустую строку
        with pytest.raises(ValueError, match="object_name is required"):
            _bsl_for(root, reader if with_index else None)["parse_form"](arg)
        return
    rows = _bsl_for(root, reader if with_index else None)["parse_form"](arg)
    expected = (
        {("Documents", "Оплата", "ФормаДокумента")} if arg == "Документ.Documents/Оплата" and not with_index else set()
    )
    assert {(f["category"], f["object_name"], f["form_name"]) for f in rows} == expected


# ═══════════════ Issue #40 — идентификатор сессии под именем sid ═══════════════


def _tool_answer(name: str, arguments: dict) -> dict:
    """Ответ тула через `mcp.call_tool` — с проверкой аргументов по схеме, как у клиента MCP."""
    from rlm_tools_bsl.server import mcp

    res = asyncio.run(mcp.call_tool(name, arguments))
    content = res[0] if isinstance(res, tuple) else res
    return json.loads(content[0].text)


def test_session_id_is_accepted_as_sid(tmp_path):
    """Мост режима Cowork у Claude Desktop теряет аргумент `session_id`: `rlm_execute` и `rlm_end` падали на проверке
    аргументов (`session_id Field required`), и сессию нечем было ни использовать, ни закрыть. `sid` — то же значение
    под другим именем; `session_id` работает как прежде, оба имени сразу — только с одним значением."""
    from rlm_tools_bsl.server import _rlm_end, _rlm_start

    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    sid = json.loads(_rlm_start(path=str(tmp_path), query="sid"))["session_id"]
    try:
        assert _tool_answer("rlm_execute", {"sid": sid, "code": "print(1 + 1)"})["stdout"] == "2\n"
        assert _tool_answer("rlm_execute", {"session_id": sid, "code": "print(3)"})["stdout"] == "3\n"
        assert _tool_answer("rlm_execute", {"session_id": sid, "sid": sid, "code": "print(4)"})["stdout"] == "4\n"
        differ = _tool_answer("rlm_execute", {"session_id": sid, "sid": "other", "code": "print(5)"})
        assert "differ" in differ["error"] and "stdout" not in differ
        assert _tool_answer("rlm_end", {"sid": sid}) == {"success": True}
        assert "not found" in _tool_answer("rlm_execute", {"sid": sid, "code": "print(6)"})["error"]
    finally:
        _rlm_end(sid)


def test_conflicting_session_ids_touch_neither_session(tmp_path):
    """Разные значения под `session_id` и `sid` — отказ до сессии: `rlm_execute` не выполняет код ни в одной из двух
    сессий (счётчики вызовов и переменные прежние), `rlm_end` не закрывает ни одну — обе продолжают работать."""
    from rlm_tools_bsl.server import _rlm_end, _rlm_start, session_manager

    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    a = json.loads(_rlm_start(path=str(tmp_path), query="sid A"))["session_id"]
    b = json.loads(_rlm_start(path=str(tmp_path), query="sid B"))["session_id"]
    try:
        assert _tool_answer("rlm_execute", {"sid": a, "code": "state = 10"}).get("error") is None
        calls = (session_manager.get(a).execute_calls, session_manager.get(b).execute_calls)
        assert "differ" in _tool_answer("rlm_execute", {"session_id": a, "sid": b, "code": "state = 999"})["error"]
        assert (session_manager.get(a).execute_calls, session_manager.get(b).execute_calls) == calls
        assert "differ" in _tool_answer("rlm_end", {"session_id": a, "sid": b})["error"]
        assert _tool_answer("rlm_execute", {"sid": a, "code": "print(state)"})["stdout"] == "10\n"
        assert _tool_answer("rlm_execute", {"sid": b, "code": "print('alive')"})["stdout"] == "alive\n"
    finally:
        _rlm_end(a)
        _rlm_end(b)


@pytest.mark.parametrize("tool", ["rlm_execute", "rlm_end"])
def test_missing_session_id_names_sid(tool, caplog):
    """Ни одного имени — ответ-ошибка, как у неизвестной сессии, а не провал проверки аргументов; ошибка подсказывает
    `sid` тому, у кого клиент теряет `session_id`. Отказ виден в логе сервера: до `_rlm_execute` он не доходит."""
    with caplog.at_level(logging.INFO, logger="rlm_tools_bsl.server"):
        answer = _tool_answer(tool, {"code": "print(1)"} if tool == "rlm_execute" else {})
    assert "session_id is required" in answer["error"] and re.search(r"\bsid\b", answer["error"])
    assert f"{tool}: rejected: session_id is required" in caplog.text


def test_session_id_schema_keeps_order_and_required_code():
    """Оба имени в схеме необязательны (обязательность проверяет сервер), `code` по-прежнему обязателен, порядок
    свойств прежний — `session_id` первым."""
    from rlm_tools_bsl.server import mcp

    schemas = {t.name: t.inputSchema for t in asyncio.run(mcp.list_tools())}
    execute, end = schemas["rlm_execute"], schemas["rlm_end"]
    assert list(execute["properties"]) == ["session_id", "sid", "code", "detail_level", "max_new_variables", "domains"]
    assert execute["required"] == ["code"]
    assert list(end["properties"]) == ["session_id", "sid"] and not end.get("required")
    for schema in (execute, end):
        for name in ("session_id", "sid"):
            assert (schema["properties"][name]["type"], schema["properties"][name]["default"]) == ("string", "")
