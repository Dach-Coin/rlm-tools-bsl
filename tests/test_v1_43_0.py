"""v1.43.0 — ответ хелпера не молчит о том, что он видел.

Релиз read-time: схема индекса и BUILDER_VERSION не тронуты. Приватные фикстуры других
файлов не импортируются (каталог tests не пакет) — деревья собираются здесь же.
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import subprocess
import textwrap
from pathlib import Path

import pytest

from rlm_tools_bsl.bsl_helpers import make_bsl_helpers
from rlm_tools_bsl.bsl_index import IndexBuilder, IndexReader
from rlm_tools_bsl.bsl_xml_parsers import parse_metadata_xml
from rlm_tools_bsl.format_detector import detect_format
from rlm_tools_bsl.helpers import make_helpers
from rlm_tools_bsl.sandbox import Sandbox

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
CF_DESCRIPTOR = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">\n'
    '  <Configuration uuid="00000000-0000-0000-0000-000000000001">\n'
    "    <Properties><Name>Тест</Name></Properties>\n"
    "  </Configuration>\n"
    "</MetaDataObject>\n"
)
EDT_DESCRIPTOR = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<mdclass:Configuration xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass" name="Тест"/>\n'
)
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


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _bsl_for(base_path, idx_reader=None, **kw):
    helpers, resolve_safe = make_helpers(str(base_path), idx_reader)
    return make_bsl_helpers(
        base_path=str(base_path),
        resolve_safe=resolve_safe,
        read_file_fn=helpers["read_file"],
        grep_fn=helpers["grep"],
        glob_files_fn=helpers["glob_files"],
        format_info=detect_format(str(base_path)),
        idx_reader=idx_reader,
        **kw,
    )


def _reader_for(root: Path, monkeypatch, **build_kw) -> IndexReader:
    monkeypatch.setenv("RLM_INDEX_DIR", str(root.parent / (root.name + "_idx")))
    return IndexReader(IndexBuilder().build(str(root), **build_kw))


# ═══════════════ Задача 1 (Д3) — find_definition списком ═══════════════


@pytest.fixture
def defs_tree(tmp_path):
    root = tmp_path / "cfg"
    _write(root / "Configuration.xml", CF_DESCRIPTOR)
    _write(
        root / "CommonModules" / "Общий" / "Ext" / "Module.bsl",
        "Процедура Первая() Экспорт\nКонецПроцедуры\n\nФункция Вторая() Экспорт\nКонецФункции\n",
    )
    return root


def test_find_definition_accepts_a_list_of_names(defs_tree):
    bsl = _bsl_for(defs_tree)
    res = bsl["find_definition"](["Первая", "Вторая", "Нет"])
    assert list(res) == ["Первая", "Вторая", "Нет"]
    assert res["Первая"]["total"] == 1 and res["Вторая"]["total"] == 1
    assert res["Нет"]["total"] == 0


def test_find_definition_list_item_error_is_isolated(defs_tree):
    bsl = _bsl_for(defs_tree)
    res = bsl["find_definition"](["Первая", ""])
    assert res["Первая"]["total"] == 1
    assert res[""] == {"error": "empty name", "hint": res[""]["hint"]}


def test_find_definition_string_answer_is_unchanged(defs_tree):
    bsl = _bsl_for(defs_tree)
    single = bsl["find_definition"]("Первая")
    assert single == bsl["find_definition"](["Первая"])["Первая"]


def _traceback_from(frame_file: str, attr: str) -> str:
    return (
        "Traceback (most recent call last):\n"
        '  File "<rlm-sandbox>", line 1, in <module>\n'
        f'  File "{frame_file}", line 10, in helper\n'
        f"AttributeError: 'list' object has no attribute '{attr}'\n"
    )


def test_hint_names_the_argument_when_a_helper_got_a_list():
    err = _traceback_from("C:/x/rlm_tools_bsl/bsl_helpers.py", "strip")
    out = Sandbox._add_error_hints(err, "find_module(['А','Б'])")
    assert "передан СПИСОК" in out
    assert "результат — СПИСОК" not in out


def test_hint_keeps_the_result_reading_for_agent_code():
    err = (
        "Traceback (most recent call last):\n"
        '  File "<rlm-sandbox>", line 1, in <module>\n'
        "AttributeError: 'list' object has no attribute 'get'\n"
    )
    out = Sandbox._add_error_hints(err, "r = search_methods('X'); r.get('a')")
    assert "результат — СПИСОК" in out


# ═══════════════ Задачи 2–3 (Д2, Д1) — перехваты ═══════════════

_EXT_CATALOG = textwrap.dedent("""\
    &После("ПриЗаписи")
    Процедура др_ПриЗаписи(Отказ)
        Сообщить("расширение");
    КонецПроцедуры

    &Перед("ПередЗаписью")
    Процедура др_ПередЗаписью(Отказ)
    КонецПроцедуры
""")
_EXT_DOCUMENT = textwrap.dedent("""\
    &Перед("ПередЗаписью")
    Процедура др_ДокПередЗаписью(Отказ)
    КонецПроцедуры
""")
_EXT_FORM = textwrap.dedent("""\
    &После("ПриОткрытии")
    Процедура др_ПриОткрытии(Отказ)
    КонецПроцедуры
""")


@pytest.fixture
def ov_tree(tmp_path):
    """Основная + расширение.

    Заказ — омонимы Справочник/Документ, перехвачены оба. У ФормаА справочника в основной
    конфигурации модуля НЕТ (перехват её обработчика сборщик привязывает к первой по rel_path
    форме — ФормаБ). Склад — у справочника нет модуля объекта, у документа есть: перехват
    модуля объекта СПРАВОЧНИКА сборщик привязывает к модулю объекта ДОКУМЕНТА."""
    cf = tmp_path / "src" / "cf"
    cfe = tmp_path / "src" / "cfe" / "Доработки"
    _write(cf / "Configuration.xml", CF_MAIN_DESCRIPTOR)
    _write(cf / "Catalogs" / "Заказ" / "Ext" / "ObjectModule.bsl", "Процедура ПередЗаписью(Отказ)\nКонецПроцедуры\n")
    _write(cf / "Documents" / "Заказ" / "Ext" / "ObjectModule.bsl", "Процедура ПередЗаписью(Отказ)\nКонецПроцедуры\n")
    _write(
        cf / "Catalogs" / "Заказ" / "Forms" / "ФормаБ" / "Ext" / "Form" / "Module.bsl",
        "Процедура ПриОткрытии(Отказ)\nКонецПроцедуры\n",
    )
    _write(cf / "Documents" / "Склад" / "Ext" / "ObjectModule.bsl", "Процедура ПриЗаписи(Отказ)\nКонецПроцедуры\n")
    _write(cf / "Catalogs" / "Склад" / "Ext" / "ManagerModule.bsl", "// менеджер\n")
    _write(cfe / "Configuration.xml", CF_EXT_DESCRIPTOR)
    _write(cfe / "Catalogs" / "Заказ" / "Ext" / "ObjectModule.bsl", _EXT_CATALOG)
    _write(cfe / "Documents" / "Заказ" / "Ext" / "ObjectModule.bsl", _EXT_DOCUMENT)
    _write(cfe / "Catalogs" / "Заказ" / "Forms" / "ФормаА" / "Ext" / "Form" / "Module.bsl", _EXT_FORM)
    _write(
        cfe / "Catalogs" / "Склад" / "Ext" / "ObjectModule.bsl",
        '&После("ПриЗаписи")\nПроцедура др_СкладПриЗаписи(Отказ)\nКонецПроцедуры\n',
    )
    return cf, cfe


@pytest.fixture(params=["live", "index"])
def ov_bsl(request, ov_tree, monkeypatch):
    cf, cfe = ov_tree
    reader = None
    if request.param == "index":
        reader = _reader_for(cf, monkeypatch, build_calls=False, build_metadata=False, build_fts=False)
    # extension_paths — как у сервера: rlm_start передаёт найденные соседние расширения в фабрику, и
    # без них модули расширений не попадают в каталог сессии (тест жил бы в непроизводственной конфигурации).
    yield _bsl_for(cf, reader, extension_paths=[str(cfe)]), cf, cfe
    if reader is not None:
        reader.close()


def _ov_key(r):
    return (r["target_method"], r["extension_method"])


@pytest.mark.parametrize(
    "typed",
    [
        "Документ.Заказ",
        "документ.Заказ",
        "Document.Заказ",
        "ДокументСсылка.Заказ",
        "Documents/Заказ",
        "Документы/Заказ",
    ],
)
def test_get_overrides_typed_name_filters_by_category(ov_bsl, typed):
    bsl, _cf, _cfe = ov_bsl
    rows = bsl["get_overrides"](typed)["overrides"]
    assert [_ov_key(r) for r in rows] == [("ПередЗаписью", "др_ДокПередЗаписью")]


def test_get_overrides_bare_name_keeps_all_homonyms(ov_bsl):
    bsl, _cf, _cfe = ov_bsl
    keys = sorted(_ov_key(r) for r in bsl["get_overrides"]("Заказ")["overrides"])
    assert keys == sorted(
        [
            ("ПередЗаписью", "др_ДокПередЗаписью"),
            ("ПриЗаписи", "др_ПриЗаписи"),
            ("ПередЗаписью", "др_ПередЗаписью"),
            ("ПриОткрытии", "др_ПриОткрытии"),
        ]
    )


def test_refusal_guard_understands_russian_plural_path(tmp_path, monkeypatch):
    root = tmp_path / "cfg"
    _write(root / "Configuration.xml", CF_DESCRIPTOR)
    _write(
        root / "Documents" / "Заказ.xml",
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"><Document><Properties><Name>Заказ</Name>'
        "</Properties></Document></MetaDataObject>",
    )
    _write(root / "Documents" / "Заказ" / "Ext" / "ObjectModule.bsl", "Процедура П()\nКонецПроцедуры\n")
    reader = _reader_for(root, monkeypatch, build_calls=False, build_metadata=True)
    try:
        with pytest.raises(ValueError, match="object_name="):
            _bsl_for(root, reader)["find_attributes"]("Документы/Заказ")
    finally:
        reader.close()


def test_object_name_resolvers_honour_the_type_prefix(ov_bsl):
    """Омоним Справочник.Склад (модуль менеджера) / Документ.Склад (модуль объекта)."""
    bsl, _cf, _cfe = ov_bsl
    mods = bsl["get_object_modules"]("Документ.Склад")
    assert mods["category"] == "Documents"
    assert [m["module_type"] for m in mods["modules"]] == ["ObjectModule"]
    assert bsl["get_module_outline"]("Справочник.Склад")["category"] == "Catalogs"
    assert bsl["get_module_outline"]("Документ.Склад")["category"] == "Documents"
    assert bsl["get_object_modules"]("Склад")["category"] == "Catalogs"  # голое имя — прежний выбор
    assert "error" in bsl["get_object_modules"]("Отчет.Склад")  # категория задана, объекта в ней нет


def test_typed_name_that_looks_like_a_file_is_still_a_name(tmp_path):
    """`Документ.XML` — объект с техническим именем XML, а не файл `Документ.XML`."""
    root = tmp_path / "cfg"
    _write(root / "Configuration.xml", CF_DESCRIPTOR)
    _write(root / "Documents" / "XML" / "Ext" / "ObjectModule.bsl", "Процедура П()\nКонецПроцедуры\n")
    out = _bsl_for(root)["get_module_outline"]("Документ.XML")
    assert out["category"] == "Documents" and out["path"] == "Documents/XML/Ext/ObjectModule.bsl"


def _synonym_tree(root: Path) -> None:
    def descriptor(kind: str, name: str) -> str:
        return (
            '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses" xmlns:v8="http://v8.1c.ru/8.1/data/core">'
            f"<{kind}><Properties><Name>{name}</Name><Synonym><v8:item><v8:lang>ru</v8:lang>"
            f"<v8:content>ОбщийСиноним</v8:content></v8:item></Synonym></Properties></{kind}></MetaDataObject>"
        )

    _write(root / "Configuration.xml", CF_DESCRIPTOR)
    _write(root / "Catalogs" / "Альфа.xml", descriptor("Catalog", "Альфа"))
    _write(root / "Catalogs" / "Альфа" / "Ext" / "ObjectModule.bsl", "Процедура А()\nКонецПроцедуры\n")
    _write(root / "Documents" / "Бета.xml", descriptor("Document", "Бета"))
    _write(root / "Documents" / "Бета" / "Ext" / "ObjectModule.bsl", "Процедура Б()\nКонецПроцедуры\n")


@pytest.mark.parametrize("crowd", [0, 50], ids=["single", "saturated"])
def test_typed_synonym_resolves_within_its_category(tmp_path, monkeypatch, crowd):
    """`crowd` справочников с тем же синонимом стоят в выдаче поиска РАНЬШЕ документа: при потолке 50
    документ ушёл бы за срез, и ответ был бы ложным «не найден»."""
    root = tmp_path / "cfg"
    _synonym_tree(root)
    for i in range(crowd):
        _write(
            root / "Catalogs" / f"Толпа{i:02d}.xml",
            '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses" xmlns:v8="http://v8.1c.ru/8.1/data/core">'
            f"<Catalog><Properties><Name>Толпа{i:02d}</Name><Synonym><v8:item><v8:lang>ru</v8:lang>"
            f"<v8:content>ОбщийСиноним {i:02d}</v8:content></v8:item></Synonym></Properties></Catalog>"
            "</MetaDataObject>",
        )
    reader = _reader_for(root, monkeypatch, build_calls=False, build_metadata=True)
    try:
        res = _bsl_for(root, reader)["get_object_modules"]("Документ.ОбщийСиноним")
        assert (res.get("category"), res.get("object_name")) == ("Documents", "Бета")
    finally:
        reader.close()


def test_find_ext_overrides_typed_name(ov_tree):
    cf, cfe = ov_tree
    res = _bsl_for(cf)["find_ext_overrides"](str(cfe), "Справочник.Заказ")
    assert sorted(_ov_key(r) for r in res["overrides"]) == sorted(
        [("ПриЗаписи", "др_ПриЗаписи"), ("ПередЗаписью", "др_ПередЗаписью"), ("ПриОткрытии", "др_ПриОткрытии")]
    )


_CATALOG_MAIN = "Catalogs/Заказ/Ext/ObjectModule.bsl"


def test_override_of_method_absent_in_base_module_is_returned(ov_bsl):
    bsl, _cf, _cfe = ov_bsl
    text = bsl["read_procedure"](_CATALOG_MAIN, "ПриЗаписи", include_overrides=True)
    assert text is not None
    assert "в базовом модуле не объявлен" in text
    assert '=== Перехвачен &После в расширении "Доработки" ===' in text
    assert "Процедура др_ПриЗаписи(Отказ)" in text


def test_absent_method_without_include_overrides_is_still_none(ov_bsl):
    bsl, _cf, _cfe = ov_bsl
    assert bsl["read_procedure"](_CATALOG_MAIN, "ПриЗаписи") is None


def test_absent_method_without_overrides_is_still_none(ov_bsl):
    bsl, _cf, _cfe = ov_bsl
    assert bsl["read_procedure"](_CATALOG_MAIN, "НетТакого", include_overrides=True) is None


def test_list_form_carries_the_override_text(ov_bsl):
    bsl, _cf, _cfe = ov_bsl
    res = bsl["read_procedure"](_CATALOG_MAIN, ["ПриЗаписи", "НетТакого"], include_overrides=True)
    assert "др_ПриЗаписи" in res["ПриЗаписи"] and res["НетТакого"] is None


def test_existing_method_override_section_on_both_branches(ov_bsl):
    """Без индекса перехваты раньше не находились вовсе (карта только из ридера)."""
    bsl, _cf, _cfe = ov_bsl
    text = bsl["read_procedure"](_CATALOG_MAIN, "ПередЗаписью", include_overrides=True)
    assert text.startswith("Процедура ПередЗаписью(Отказ)")
    assert '=== Перехвачен &Перед в расширении "Доработки" ===' in text
    assert "Процедура др_ПередЗаписью(Отказ)" in text
    assert "др_ДокПередЗаписью" not in text  # омоним-документ не прилипает


def test_form_override_does_not_stick_to_another_form(ov_bsl):
    """У ФормаА нет модуля в основной конфигурации: сборщик привязал её перехват к ФормаБ."""
    bsl, _cf, _cfe = ov_bsl
    path_b = "Catalogs/Заказ/Forms/ФормаБ/Ext/Form/Module.bsl"
    text = bsl["read_procedure"](path_b, "ПриОткрытии", include_overrides=True)
    assert text.strip().startswith("Процедура ПриОткрытии(Отказ)")
    assert "Перехвачен" not in text
    assert not any(p.get("overridden_by") for p in bsl["extract_procedures"](path_b))


_DOC_SKLAD = "Documents/Склад/Ext/ObjectModule.bsl"


def test_catalog_override_does_not_stick_to_homonym_document(ov_bsl):
    """У Справочник.Склад нет модуля объекта: сборщик привязал его перехват к Документ.Склад."""
    bsl, _cf, _cfe = ov_bsl
    assert not any(p.get("overridden_by") for p in bsl["extract_procedures"](_DOC_SKLAD))
    assert "Перехвачен" not in bsl["read_procedure"](_DOC_SKLAD, "ПриЗаписи", include_overrides=True)
    res = bsl["get_object_modules"]("Документ.Склад")
    assert res["category"] == "Documents"  # потребитель — именно документ (Задача 2)
    assert [m["overrides"]["count"] for m in res["modules"] if m["module_type"] == "ObjectModule"] == [0]
    row = bsl["get_overrides"]("Справочник.Склад")["overrides"][0]
    assert (row["extension_method"], row["source_path"], row["target_method_line"]) == ("др_СкладПриЗаписи", "", None)


@pytest.mark.parametrize("with_index", [False, True], ids=["live", "index"])
def test_standalone_extension_session_is_not_its_own_target(ov_tree, monkeypatch, with_index):
    _cf, cfe = ov_tree
    reader = (
        _reader_for(cfe, monkeypatch, build_calls=False, build_metadata=False, build_fts=False) if with_index else None
    )
    try:
        bsl = _bsl_for(cfe, reader)
        own = "Catalogs/Заказ/Ext/ObjectModule.bsl"
        assert bsl["read_procedure"](own, "ПриЗаписи", include_overrides=True) is None
        assert not any(p.get("overridden_by") for p in bsl["extract_procedures"](own))
    finally:
        if reader:
            reader.close()


EDT_EXT_DESCRIPTOR = textwrap.dedent("""\
    <?xml version="1.0" encoding="UTF-8"?>
    <mdclass:Configuration xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
                           xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass"
                           xmlns:mdclassExtension="http://g5.1c.ru/v8/dt/metadata/mdclass/extension"
                           uuid="00000000-0000-0000-0000-000000000004">
        <name>ЕДТ</name>
        <objectBelonging>Adopted</objectBelonging>
        <extension xsi:type="mdclassExtension:ConfigurationExtension"><defaultRunMode>Checked</defaultRunMode></extension>
        <namePrefix>ед_</namePrefix>
        <configurationExtensionPurpose>Customization</configurationExtensionPurpose>
    </mdclass:Configuration>
""")


@pytest.mark.parametrize("with_index", [False, True], ids=["live", "index"])
def test_override_reaches_the_right_form_across_layouts(tmp_path, monkeypatch, with_index):
    """CF-основная, EDT-расширение: пути разной раскладки, сборщик привязал перехват формы Б к форме А.
    Мало отбросить чужую привязку — правильная форма обязана перехват ПОЛУЧИТЬ."""
    cf, ext = tmp_path / "src" / "cf", tmp_path / "src" / "cfe" / "ЕДТ"
    _write(cf / "Configuration.xml", CF_MAIN_DESCRIPTOR)
    for form in ("ФормаА", "ФормаБ"):
        _write(
            cf / "Catalogs" / "Заказ" / "Forms" / form / "Ext" / "Form" / "Module.bsl",
            "Процедура ПриОткрытии(Отказ)\nКонецПроцедуры\n",
        )
    _write(ext / "Configuration" / "Configuration.mdo", EDT_EXT_DESCRIPTOR)
    _write(
        ext / "Catalogs" / "Заказ" / "Forms" / "ФормаБ" / "Module.bsl",
        '&После("ПриОткрытии")\nПроцедура ед_ПриОткрытии(Отказ)\nКонецПроцедуры\n',
    )
    reader = (
        _reader_for(cf, monkeypatch, build_calls=False, build_metadata=False, build_fts=False) if with_index else None
    )
    try:
        bsl = _bsl_for(cf, reader, extension_paths=[str(ext)])
        form_a = "Catalogs/Заказ/Forms/ФормаА/Ext/Form/Module.bsl"
        form_b = "Catalogs/Заказ/Forms/ФормаБ/Ext/Form/Module.bsl"
        assert "ед_ПриОткрытии" not in bsl["read_procedure"](form_a, "ПриОткрытии", include_overrides=True)
        assert "ед_ПриОткрытии" in bsl["read_procedure"](form_b, "ПриОткрытии", include_overrides=True)
        if with_index:  # overridden_by — поле индексной ветки (HELPERS.md: «при наличии индекса v9+»)
            [proc] = bsl["extract_procedures"](form_b)
            assert [o["extension_method"] for o in proc["overridden_by"]] == ["ед_ПриОткрытии"]
    finally:
        if reader:
            reader.close()


def test_extension_module_in_any_path_form_is_not_a_target(ov_bsl):
    """Абсолютный и неканонический путь к модулю РАСШИРЕНИЯ — тот же модуль расширения: его собственный
    перехват не выдаётся за перехват основной конфигурации. Модуль основной конфигурации в абсолютной
    форме получает ровно то же, что в канонической."""
    bsl, cf, cfe = ov_bsl
    ext_abs = str(cfe / "Catalogs" / "Заказ" / "Ext" / "ObjectModule.bsl")
    ext_detour = "../cf/../cfe/Доработки/Catalogs/Заказ/Ext/ObjectModule.bsl"
    ext_deep = "../../src/cfe/Доработки/Catalogs/Заказ/Ext/ObjectModule.bsl"  # ведущие .. другой длины
    for p in (ext_abs, ext_detour, ext_deep):
        assert bsl["read_procedure"](p, "ПриЗаписи", include_overrides=True) is None
        assert not any(x.get("overridden_by") for x in bsl["extract_procedures"](p))
    canonical = bsl["read_procedure"](_CATALOG_MAIN, "ПриЗаписи", include_overrides=True)
    main_abs = str(cf / "Catalogs" / "Заказ" / "Ext" / "ObjectModule.bsl")
    main_via_ext = "../cfe/Доработки/../../cf/Catalogs/Заказ/Ext/ObjectModule.bsl"  # через расширение — в основную
    for p in (main_abs, main_via_ext):
        assert bsl["read_procedure"](p, "ПриЗаписи", include_overrides=True) == canonical


@pytest.mark.skipif(os.name != "nt", reason="обратная косая черта — разделитель только на Windows")
def test_backslash_path_finds_the_same_overrides(ov_bsl):
    bsl, _cf, _cfe = ov_bsl
    fwd = bsl["read_procedure"](_CATALOG_MAIN, "ПриЗаписи", include_overrides=True)
    back = bsl["read_procedure"](_CATALOG_MAIN.replace("/", "\\"), "ПриЗаписи", include_overrides=True)
    assert back == fwd and fwd is not None


@pytest.mark.parametrize("with_index", [False, True], ids=["live", "index"])
def test_configuration_level_module_keeps_its_override(tmp_path, monkeypatch, with_index):
    """Модуль уровня конфигурации (модуль сеанса) — без категории и объекта: идентичность у него —
    тип модуля. Перехват его метода на индексе был виден и до релиза (привязка по пути); карта по
    идентичности не имеет права его потерять, а без индекса он теперь находится живым обходом."""
    cf = tmp_path / "src" / "cf"
    cfe = tmp_path / "src" / "cfe" / "Доработки"
    _write(cf / "Configuration.xml", CF_MAIN_DESCRIPTOR)
    _write(
        cf / "Ext" / "SessionModule.bsl",
        "Процедура УстановкаПараметровСеанса(ТребуемыеПараметры)\nКонецПроцедуры\n",
    )
    _write(cf / "CommonModules" / "Общий" / "Ext" / "Module.bsl", "Процедура П() Экспорт\nКонецПроцедуры\n")
    _write(cfe / "Configuration.xml", CF_EXT_DESCRIPTOR)
    _write(
        cfe / "Ext" / "SessionModule.bsl",
        '&После("УстановкаПараметровСеанса")\nПроцедура др_УстановкаПараметровСеанса(ТребуемыеПараметры)\n'
        "КонецПроцедуры\n",
    )
    reader = (
        _reader_for(cf, monkeypatch, build_calls=False, build_metadata=False, build_fts=False) if with_index else None
    )
    try:
        bsl = _bsl_for(cf, reader, extension_paths=[str(cfe)])
        path = "Ext/SessionModule.bsl"
        text = bsl["read_procedure"](path, "УстановкаПараметровСеанса", include_overrides=True)
        assert "Процедура др_УстановкаПараметровСеанса(ТребуемыеПараметры)" in text
        if with_index:
            [proc] = bsl["extract_procedures"](path)
            assert [o["extension_method"] for o in proc["overridden_by"]] == ["др_УстановкаПараметровСеанса"]
    finally:
        if reader:
            reader.close()


# ═══════════════ Задача 4 (Д4) — posting в EDT ═══════════════

_EDT_DOC = '<mdclass:Document xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass"><name>Заказ</name>{extra}</mdclass:Document>'


@pytest.mark.parametrize(
    "extra, expected",
    [("", "Allow"), ("<posting>Deny</posting>", "Deny"), ("<posting>Allow</posting>", "Allow")],
)
def test_edt_document_posting_default_is_allow(extra, expected):
    assert parse_metadata_xml(_EDT_DOC.format(extra=extra))["posting"] == expected


def test_edt_document_posting_attribute_still_wins():
    xml = _EDT_DOC.replace("<mdclass:Document ", '<mdclass:Document posting="Deny" ').format(extra="")
    assert parse_metadata_xml(xml)["posting"] == "Deny"


def test_cf_document_without_posting_still_has_no_key():
    xml = (
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"><Document><Properties>'
        "<Name>Заказ</Name></Properties></Document></MetaDataObject>"
    )
    assert "posting" not in parse_metadata_xml(xml)


def test_edt_document_without_tag_reads_as_postable(tmp_path):
    root = tmp_path / "edt"
    _write(root / "Configuration" / "Configuration.mdo", EDT_DESCRIPTOR)
    _write(root / "Documents" / "Заказ" / "Заказ.mdo", _EDT_DOC.format(extra=""))
    _write(root / "Documents" / "Заказ" / "ObjectModule.bsl", "Процедура Тест()\nКонецПроцедуры\n")
    bsl = _bsl_for(root)
    assert bsl["get_object_full_structure"]("Заказ")["posting"] == "Allow"
    rm = bsl["find_register_movements"]("Заказ")  # результат пуст → posting публикуется
    assert rm.get("posting") == "Allow" and "is_postable" not in rm


# ═══════════════ Задача 5 (Д6) — состав по видам ═══════════════


def _declared_tree(root: Path, refs: list[str]) -> None:
    folders = {
        "AccumulationRegister": "AccumulationRegisters",
        "InformationRegister": "InformationRegisters",
        "AccountingRegister": "AccountingRegisters",
    }
    for ref in refs:
        kind, short = ref.split(".", 1)
        (root / folders[kind] / short).mkdir(parents=True, exist_ok=True)
    items = "".join(f"<xr:Item>{r}</xr:Item>" for r in refs)
    _write(root / "Configuration.xml", CF_DESCRIPTOR)
    _write(
        root / "Documents" / "Заказ.xml",
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses" xmlns:xr="http://v8.1c.ru/8.3/xcf/readable">'
        f"<Document><Properties><Name>Заказ</Name><RegisterRecords>{items}</RegisterRecords></Properties>"
        "</Document></MetaDataObject>",
    )
    _write(
        root / "Documents" / "Заказ" / "Ext" / "ObjectModule.bsl",
        "Процедура ОбработкаПроведения(Отказ, Режим)\nКонецПроцедуры\n",
    )


@pytest.mark.parametrize("with_index", [False, True], ids=["live", "index"])
def test_declared_by_kind_survives_truncation(tmp_path, monkeypatch, with_index):
    from rlm_tools_bsl.bsl_helpers import _DECLARED_REGISTERS_PAGE

    refs = [f"AccumulationRegister.Н{i:02d}" for i in range(_DECLARED_REGISTERS_PAGE)]
    refs += ["InformationRegister.С1", "InformationRegister.С2", "AccountingRegister.Бух"]
    root = tmp_path / "cfg"
    _declared_tree(root, refs)
    reader = _reader_for(root, monkeypatch, build_calls=False, build_metadata=True) if with_index else None
    try:
        res = _bsl_for(root, reader)["find_register_movements"]("Заказ")
        assert res["declared_truncated"] is True
        assert res["declared_by_kind"] == {
            "AccountingRegister": 1,
            "AccumulationRegister": 40,
            "InformationRegister": 2,
        }
        assert sum(res["declared_by_kind"].values()) == res["declared_total"]
        assert "parse_object_xml('Documents/Заказ')['register_records']" in res["_meta"]["declared_route"]
    finally:
        if reader:
            reader.close()


def test_declared_by_kind_without_truncation_has_no_route(tmp_path):
    root = tmp_path / "cfg"
    _declared_tree(root, ["InformationRegister.С1"])
    res = _bsl_for(root)["find_register_movements"]("Заказ")
    assert res["declared_by_kind"] == {"InformationRegister": 1}
    assert "declared_route" not in res["_meta"]


# ═══════════════ Задача 6 (Д5) — печатные формы ═══════════════


def _pf_proc(body: str, params: str = "КомандыПечати") -> str:
    return f"Процедура ДобавитьКомандыПечати({params}) Экспорт\n{body}\nКонецПроцедуры\n"


def _pf_cmd(var: str, ident: str, title: str, coll: str = "КомандыПечати") -> str:
    return (
        f'    {var} = {coll}.Добавить();\n    {var}.Идентификатор = "{ident}";\n'
        f"    {var}.Представление = НСтр(\"ru = '{title}'\");\n"
    )


@pytest.fixture
def pf_tree(tmp_path):
    root = tmp_path / "cfg"
    _write(root / "Configuration.xml", CF_DESCRIPTOR)
    _write(
        root / "Documents" / "Тест" / "Ext" / "ManagerModule.bsl",
        _pf_proc(
            _pf_cmd("К", "Накладная", "Накладная") + "    ТестЛокализация.ДобавитьКомандыПечати(КомандыПечати);\n"
            "    Другие.ДобавитьКомандыПечати(КомандыОтчета);\n    Нет.ДобавитьКомандыПечати(КомандыПечати);\n"
        ),
    )
    _write(
        root / "CommonModules" / "ТестЛокализация" / "Ext" / "Module.bsl",
        _pf_proc(
            _pf_cmd("К", "ТОРГ12", "ТОРГ-12")
            + _pf_cmd("К", "Накладная", "Чужая накладная")
            + "    Документы.Перевозка.ДобавитьКомандыПечати(КомандыПечати, 23);\n"
        ),
    )
    _write(root / "CommonModules" / "Другие" / "Ext" / "Module.bsl", _pf_proc(_pf_cmd("К", "Отчет", "Отчет")))
    _write(
        root / "Documents" / "Перевозка" / "Ext" / "ManagerModule.bsl",
        _pf_proc(
            _pf_cmd("К", "ТТН", "ТТН") + "    ТестЛокализация.ДобавитьКомандыПечати(КомандыПечати);\n",
            params="КомандыПечати, Порядок = Неопределено",
        ),
    )
    _write(root / "Documents" / "ТестДлинный" / "Ext" / "ManagerModule.bsl", _pf_proc(_pf_cmd("К", "Чужая", "Чужая")))
    return root


def test_print_forms_expand_nested_delegates(pf_tree):
    res = _bsl_for(pf_tree)["find_print_forms"]("Тест")
    rows = {r["name"]: r for r in res["print_forms"]}
    assert list(rows) == ["Накладная", "ТОРГ12", "ТТН"]
    assert rows["Накладная"]["presentation"] == "Накладная"  # своя побеждает делегата
    assert rows["ТОРГ12"]["file"] == "CommonModules/ТестЛокализация/Ext/Module.bsl"
    assert rows["ТТН"]["file"] == "Documents/Перевозка/Ext/ManagerModule.bsl"
    status = {(d["call"], d["depth"]): d["status"] for d in res["_meta"]["delegates_expanded"]}
    assert status == {
        ("ТестЛокализация.ДобавитьКомандыПечати", 1): "expanded",
        ("Документы.Перевозка.ДобавитьКомандыПечати", 2): "expanded",
        ("ТестЛокализация.ДобавитьКомандыПечати", 3): "cycle",
        ("Другие.ДобавитьКомандыПечати", 1): "other_list",
        ("Нет.ДобавитьКомандыПечати", 1): "module_not_found",
    }
    assert res["_meta"]["delegates"] == [
        "ТестЛокализация.ДобавитьКомандыПечати",
        "Другие.ДобавитьКомандыПечати",
        "Нет.ДобавитьКомандыПечати",
    ]


def test_print_forms_do_not_mix_documents_with_longer_names(pf_tree):
    res = _bsl_for(pf_tree)["find_print_forms"]("Тест")
    assert all("ТестДлинный" not in r["file"] for r in res["print_forms"])


def test_print_forms_without_delegates_keep_the_old_meta(pf_tree):
    res = _bsl_for(pf_tree)["find_print_forms"]("ТестДлинный")
    assert res["_meta"] == {"delegates": []}
    assert [r["name"] for r in res["print_forms"]] == ["Чужая"]


def test_print_forms_fragment_still_uses_substring_route(pf_tree):
    """Точного объекта нет — прежний подстрочный маршрут (поведение фрагмента не меняется)."""
    names = {r["name"] for r in _bsl_for(pf_tree)["find_print_forms"]("Длинный")["print_forms"]}
    assert names == {"Чужая"}


@pytest.mark.parametrize("arg", ["Пустой", "Documents/Пустой", "Документ.Пустой", "БезМенеджера"])
def test_existing_object_without_manager_gets_no_foreign_commands(pf_tree, arg):
    """Объект есть (описатель без модулей / модуль объекта без менеджера) — подстрочного маршрута нет."""
    _write(
        pf_tree / "Documents" / "Пустой.xml",
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"><Document><Properties><Name>Пустой</Name>'
        "</Properties></Document></MetaDataObject>",
    )
    _write(pf_tree / "Documents" / "ПустойДлинный" / "Ext" / "ManagerModule.bsl", _pf_proc(_pf_cmd("К", "Чужая2", "Ч")))
    _write(pf_tree / "Documents" / "БезМенеджера" / "Ext" / "ObjectModule.bsl", "Процедура П()\nКонецПроцедуры\n")
    _write(
        pf_tree / "Documents" / "БезМенеджераДлинный" / "Ext" / "ManagerModule.bsl",
        _pf_proc(_pf_cmd("К", "Чужая3", "Ч")),
    )
    assert _bsl_for(pf_tree)["find_print_forms"](arg)["print_forms"] == []


@pytest.fixture
def pf_ext_tree(tmp_path):
    """Основная + два расширения. Доработки добавляют ДобавитьКомандыПечати в ЗАИМСТВОВАННЫЙ
    общий модуль и новый документ, который зовёт его; Чужое — та же процедура в своей копии."""
    cf = tmp_path / "src" / "cf"
    _write(cf / "Configuration.xml", CF_MAIN_DESCRIPTOR)
    _write(cf / "CommonModules" / "Локализация" / "Ext" / "Module.bsl", "Процедура Прочее() Экспорт\nКонецПроцедуры\n")
    _write(
        cf / "Documents" / "Осн" / "Ext" / "ManagerModule.bsl",
        _pf_proc(_pf_cmd("К", "Основная", "Основная") + "    Локализация.ДобавитьКомандыПечати(КомандыПечати);\n"),
    )
    for ext, ident in (("Доработки", "ИзРасширения"), ("Чужое", "ИзЧужого")):
        root = tmp_path / "src" / "cfe" / ext
        _write(root / "Configuration.xml", CF_EXT_DESCRIPTOR.replace("Доработки", ext))
        _write(root / "CommonModules" / "Локализация" / "Ext" / "Module.bsl", _pf_proc(_pf_cmd("К", ident, ident)))
    _write(
        tmp_path / "src" / "cfe" / "Доработки" / "Documents" / "ДокРасш" / "Ext" / "ManagerModule.bsl",
        _pf_proc(_pf_cmd("К", "Своя", "Своя") + "    Локализация.ДобавитьКомандыПечати(КомандыПечати);\n"),
    )
    return cf


def _pf_ext_bsl(cf: Path):
    """Корни ОБОИХ расширений — в фабрику, как это делает сервер: без них модули расширений не
    попадают в каталог сессии, и запрет «чужого» расширения проверялся бы на пустом месте."""
    cfe = cf.parent / "cfe"
    return _bsl_for(cf, extension_paths=[str(cfe / "Доработки"), str(cfe / "Чужое")])


@pytest.mark.parametrize("with_index", [False, True], ids=["live", "index"])
@pytest.mark.parametrize("arg", ["Заказ", "Документ.Заказ"])
def test_document_flow_print_forms_stay_within_documents(tmp_path, monkeypatch, with_index, arg):
    """Композит документа передаёт категорию: у документа нет модуля менеджера, у справочника-омонима есть."""
    root = tmp_path / "cfg"
    _write(root / "Configuration.xml", CF_DESCRIPTOR)
    _write(root / "Documents" / "Заказ" / "Ext" / "ObjectModule.bsl", "Процедура П()\nКонецПроцедуры\n")
    _write(root / "Catalogs" / "Заказ" / "Ext" / "ManagerModule.bsl", _pf_proc(_pf_cmd("К", "ЧужаяКаталожная", "Ч")))
    reader = _reader_for(root, monkeypatch, build_calls=False, build_metadata=True) if with_index else None
    try:
        flow = _bsl_for(root, reader)["analyze_document_flow"](arg)
        assert flow["print_forms"]["print_forms"] == []
    finally:
        if reader:
            reader.close()


def test_extension_xml_only_object_is_an_existing_object(pf_ext_tree):
    """XML-only объект расширения (документ без модулей) существует — подстрочного маршрута нет."""
    cfe = pf_ext_tree.parent / "cfe" / "Доработки"
    _write(
        cfe / "Documents" / "др_Пустой.xml",
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"><Document><Properties><Name>др_Пустой</Name>'
        "</Properties></Document></MetaDataObject>",
    )
    _write(
        pf_ext_tree / "Documents" / "др_ПустойДлинный" / "Ext" / "ManagerModule.bsl",
        _pf_proc(_pf_cmd("К", "Чужая4", "Ч")),
    )
    assert _pf_ext_bsl(pf_ext_tree)["find_print_forms"]("Documents/др_Пустой")["print_forms"] == []


def test_extension_delegate_resolves_into_own_extension(pf_ext_tree):
    res = _pf_ext_bsl(pf_ext_tree)["find_print_forms"]("ДокРасш")
    rows = {r["name"]: r["file"] for r in res["print_forms"]}
    assert list(rows) == ["Своя", "ИзРасширения"]
    assert rows["ИзРасширения"].startswith("../cfe/Доработки/") and "ИзЧужого" not in rows


def test_main_delegate_never_resolves_into_an_extension(pf_ext_tree):
    res = _pf_ext_bsl(pf_ext_tree)["find_print_forms"]("Осн")
    assert [r["name"] for r in res["print_forms"]] == ["Основная"]
    [entry] = res["_meta"]["delegates_expanded"]
    assert (entry["module"], entry["status"]) == ("CommonModules/Локализация/Ext/Module.bsl", "procedure_not_found")


# ═══════════════ Задача 7 (Д7) — тексты запросов ═══════════════


def _queries(tmp_path, body: str) -> list[dict]:
    root = tmp_path / "cfg"
    _write(root / "Configuration.xml", CF_DESCRIPTOR)
    _write(root / "Documents" / "Т" / "Ext" / "ManagerModule.bsl", body)
    return _bsl_for(root)["extract_queries"]("Documents/Т/Ext/ManagerModule.bsl")


def test_query_in_variable_with_other_name_next_line(tmp_path):
    q = _queries(
        tmp_path,
        'Процедура П()\n    ТекстЗапросаДанных =\n    "ВЫБРАТЬ\n    |    Т.Ссылка\n    |ИЗ\n'
        '    |    Документ.Заказ КАК Т";\nКонецПроцедуры\n',
    )
    assert [(x["procedure"], x["line"], x["tables"]) for x in q] == [("П", 3, ["Документ.Заказ"])]


def test_query_in_variable_same_line_and_other_query_object(tmp_path):
    q = _queries(
        tmp_path,
        'Процедура П()\n    ТекстПродажа = "ВЫБРАТЬ * ИЗ Документ.Продажа КАК П";\n'
        '    ЗапросПоСкладам.Текст = "ВЫБРАТЬ * ИЗ Справочник.Склады КАК С";\nКонецПроцедуры\n',
    )
    assert [(x["line"], x["tables"]) for x in q] == [(2, ["Документ.Продажа"]), (3, ["Справочник.Склады"])]


def test_query_returned_from_function(tmp_path):
    q = _queries(
        tmp_path,
        'Функция ТекстЗапросаТоваров()\n    Возврат "ВЫБРАТЬ\n    |    Т.Номенклатура\n    |ИЗ\n'
        '    |    РегистрНакопления.Товары КАК Т";\nКонецФункции\n',
    )
    assert [(x["procedure"], x["tables"]) for x in q] == [("ТекстЗапросаТоваров", ["РегистрНакопления.Товары"])]


def test_fragments_and_concatenation_give_no_record(tmp_path):
    q = _queries(
        tmp_path,
        "Процедура П()\n"
        '    Т = СтрЗаменить(Т, "ВЫБРАТЬ", "ВЫБРАТЬ РАЗРЕШЕННЫЕ");\n'
        '    Т2 = "ВЫБРАТЬ * ИЗ Документ.Заказ КАК З" + Условие;\n'
        '    Т3 = Т2 + "ВЫБРАТЬ * ИЗ Документ.Заказ КАК З";\n'
        '    // Т4 = "ВЫБРАТЬ * ИЗ Документ.Заказ КАК З";\nКонецПроцедуры\n',
    )
    assert q == []


def test_old_forms_are_not_duplicated(tmp_path):
    q = _queries(
        tmp_path,
        'Процедура П()\n    Запрос.Текст = "ВЫБРАТЬ * ИЗ Документ.А КАК А";\n'
        '    ТекстЗапроса =\n        "ВЫБРАТЬ * ИЗ Документ.Б КАК Б";\n'
        '    З = Новый Запрос("ВЫБРАТЬ * ИЗ Документ.В КАК В");\nКонецПроцедуры\n',
    )
    assert [x["tables"] for x in q] == [["Документ.А"], ["Документ.Б"], ["Документ.В"]]
    assert q[0]["text_preview"] == 'ВЫБРАТЬ * ИЗ Документ.А КАК А";'  # исторический хвост сохранён


def test_concatenation_rule_differs_only_for_the_legacy_one_line_form(tmp_path):
    """Объявленная совместимость: однострочное `Запрос.Текст = "…" + Хвост` даёт запись с хвостом
    (как до релиза); та же склейка у переменной другого имени — новая форма — записи не даёт."""
    q = _queries(
        tmp_path,
        'Процедура П()\n    Запрос.Текст = "ВЫБРАТЬ * ИЗ Документ.А КАК А" + Хвост;\n'
        '    ТекстДругой = "ВЫБРАТЬ * ИЗ Документ.Б КАК Б" + Хвост;\nКонецПроцедуры\n',
    )
    assert [x["tables"] for x in q] == [["Документ.А"]]
    assert q[0]["text_preview"].endswith('" + Хвост;')


# ═══════════════ Задача 8 (Д8) — события формы CF ═══════════════

_CF_FORM = """<?xml version="1.0" encoding="UTF-8"?>
<Form xmlns="http://v8.1c.ru/8.3/xcf/logform">
  <Events>
    <Event name="OnCreateAtServer">ПриСозданииНаСервере</Event>
    <Event name="BeforeClose">ПередЗакрытием</Event>
    <Event name="OnClose">ПриЗакрытии</Event>
    <Event name="OnWriteAtServer">ПриЗаписиНаСервере</Event>
    <Event name="OnLoadUserSettingsAtServer">ПриЗагрузкеПользовательскихНастроекНаСервере</Event>
    <Event name="AfterWrite">ПослеЗаписи</Event>
  </Events>
</Form>
"""
_EXPECTED_SCOPE = {
    "OnCreateAtServer": "form",
    "BeforeClose": "form",
    "OnClose": "form",
    "OnWriteAtServer": "ext_info",
    "OnLoadUserSettingsAtServer": "ext_info",
    "AfterWrite": "ext_info",
}


def _cf_form_tree(tmp_path) -> Path:
    root = tmp_path / "cfg"
    _write(root / "Configuration.xml", CF_DESCRIPTOR)
    d = root / "Documents" / "Тест" / "Forms" / "ФормаДокумента" / "Ext"
    _write(d / "Form.xml", _CF_FORM)
    _write(d / "Form" / "Module.bsl", "// модуль\n")
    return root


def _scopes(forms) -> dict:
    return {h["event"]: h["scope"] for h in forms[0]["handlers"] if not h["element"]}


@pytest.mark.parametrize("with_index", [False, True], ids=["live", "index"])
def test_cf_form_events_are_classified_by_the_edt_set(tmp_path, monkeypatch, with_index):
    root = _cf_form_tree(tmp_path)
    reader = _reader_for(root, monkeypatch, build_calls=False, build_metadata=True) if with_index else None
    try:
        assert _scopes(_bsl_for(root, reader)["parse_form"]("Тест")) == _EXPECTED_SCOPE
    finally:
        if reader:
            reader.close()


def test_old_index_scope_is_normalized_on_read(tmp_path, monkeypatch):
    """Индекс, собранный прежним разборщиком: строки со старым scope читаются верно."""
    root = _cf_form_tree(tmp_path)
    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
    db = IndexBuilder().build(str(root), build_calls=False, build_metadata=True)
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE form_elements SET scope='ext_info' WHERE event IN ('BeforeClose','OnClose')")
    conn.execute(
        "UPDATE form_elements SET scope='form' WHERE event IN ('OnWriteAtServer','OnLoadUserSettingsAtServer')"
    )
    conn.commit()
    conn.close()
    reader = IndexReader(db)
    try:
        assert _scopes(_bsl_for(root, reader)["parse_form"]("Тест")) == _EXPECTED_SCOPE
    finally:
        reader.close()


# ═══════════════ Задача 9 (Д10) — файлы вне навигационного индекса ═══════════════


@pytest.fixture
def edt_files(tmp_path, monkeypatch):
    root = tmp_path / "edt"
    _write(root / "Configuration" / "Configuration.mdo", EDT_DESCRIPTOR)
    _write(
        root / "Roles" / "Р" / "Р.mdo",
        '<mdclass:Role xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass"><name>Р</name></mdclass:Role>',
    )
    _write(root / "Roles" / "Р" / "Rights.rights", "<Rights><right>Read</right>\n<right>Insert</right></Rights>\n")
    _write(root / "Documents" / "Д" / "Forms" / "Ф" / "Form.form", "<form:Form/>\n")
    _write(root / "Documents" / "Д" / "ObjectModule.bsl", "Процедура П()\nКонецПроцедуры\n")
    reader = _reader_for(root, monkeypatch, build_calls=False, build_metadata=False)
    yield root, reader
    reader.close()


@pytest.mark.parametrize(
    "mask",
    [
        "**/*.rights",
        "Roles/**/*.rights",
        "**/Roles/**/*.rights",
        "Roles/*/Rights.rights",
        "**/Rights.rights",
        "Roles/Р/Rights.rights",
        "**/*.form",
        "**/*.RIGHTS",
    ],
)
def test_glob_with_foreign_extension_answers_like_the_filesystem(edt_files, mask):
    root, reader = edt_files
    with_index = make_helpers(str(root), reader)[0]["glob_files"](mask)
    without = make_helpers(str(root))[0]["glob_files"](mask)
    assert with_index == without
    assert reader.glob_files(mask) is None


def test_nav_extensions_still_served_by_the_index(edt_files):
    _root, reader = edt_files
    assert reader.glob_files("**/*.mdo") == ["Configuration/Configuration.mdo", "Roles/Р/Р.mdo"]
    assert reader.glob_files("**/*.bsl") == ["Documents/Д/ObjectModule.bsl"]


def test_count_matches_finds_rights(edt_files):
    root, reader = edt_files
    res = _bsl_for(root, reader)["count_matches"]("<right>", file_types="rights", path="Roles")
    assert (res["files_matched"], res["occurrences"], res["_meta"]["candidates_total"]) == (1, 2, 1)
    assert res["source"] == "live"  # перечислено обходом файлов, а не индексом


def test_count_matches_source_names_the_mixed_route(edt_files):
    root, reader = edt_files
    bsl = _bsl_for(root, reader)
    assert bsl["count_matches"]("Процедура", file_types="bsl")["source"] == "index"
    assert bsl["count_matches"]("Процедура", file_types="bsl,rights")["source"] == "index+live"


@pytest.mark.parametrize("with_index, expected", [(False, "live"), (True, "index+live")], ids=["live", "index"])
def test_count_matches_source_counts_extension_modules(ov_tree, monkeypatch, with_index, expected):
    """Сессия основной с соседним расширением: модули расширения перечисляются живьём."""
    cf, cfe = ov_tree
    reader = (
        _reader_for(cf, monkeypatch, build_calls=False, build_metadata=False, build_fts=False) if with_index else None
    )
    try:
        res = _bsl_for(cf, reader, extension_paths=[str(cfe)])["count_matches"]("Процедура")
        assert res["extensions_included"] is True
        assert res["source"] == expected
    finally:
        if reader:
            reader.close()


# ═══════════════ Задача 10 (Д9) — регистр в count_matches ═══════════════

_CASES = (
    "#Если ВебКлиент Тогда\n#Если Вебклиент Тогда\n#Если ВЕБКЛИЕНТ Тогда\nЁлка\nёлка\nЕлка\nselect\nSELECT\n"
    'Сообщить("İ");\n'
)


@pytest.fixture
def cm_tree(tmp_path):
    root = tmp_path / "cfg"
    _write(root / "Configuration.xml", CF_DESCRIPTOR)
    _write(root / "CommonModules" / "М" / "Ext" / "Module.bsl", _CASES)
    return root


@pytest.mark.parametrize(
    "pattern, default, sensitive",
    [("ВебКлиент", 3, 1), ("Ёлка", 2, 1), ("ёлка", 2, 1), ("select", 2, 1)],
)
def test_count_matches_is_case_insensitive_by_default(cm_tree, pattern, default, sensitive):
    bsl = _bsl_for(cm_tree)
    res = bsl["count_matches"](pattern)
    assert res["occurrences"] == default and res["_meta"]["ignore_case"] is True
    assert bsl["count_matches"](pattern, ignore_case=False)["occurrences"] == sensitive


def test_regex_mode_keeps_ignorecase_semantics(cm_tree):
    assert _bsl_for(cm_tree)["count_matches"](r"веб\w+", regex=True)["occurrences"] == 3


@pytest.mark.skipif(shutil.which("git") is None, reason="git недоступен")
def test_git_and_python_branches_agree(cm_tree, monkeypatch):
    import rlm_tools_bsl.bsl_index as bi

    subprocess.run(["git", "init", "-q"], cwd=cm_tree, check=True)
    subprocess.run(["git", "add", "-A"], cwd=cm_tree, check=True)
    calls: list[dict] = []
    real = bi._git_grep_counts

    def spy(*a, **kw):
        calls.append(kw)
        return real(*a, **kw)

    monkeypatch.setattr(bi, "_git_grep_counts", spy)
    with_git = _bsl_for(cm_tree)
    monkeypatch.setattr(bi, "_git_available", lambda *a, **kw: False)
    without_git = _bsl_for(cm_tree)
    for pattern in ("select", "ВебКлиент", "i"):  # "i" — свёртка ТЕКСТОМ: "İ".lower() содержит i
        assert with_git["count_matches"](pattern)["occurrences"] == without_git["count_matches"](pattern)["occurrences"]
    assert calls == []  # без учёта регистра git не зовётся вовсе
    assert with_git["count_matches"]("ВебКлиент", ignore_case=False)["occurrences"] == 1  # git-ветка, как раньше
    assert calls


# ═══════════════ Задача 11 (Д11) — регистр по имени ═══════════════

_DELEGATE_DOC = textwrap.dedent("""\
    Процедура ОбработкаПроведения(Отказ, РежимПроведения)
        ПроведениеСервер.ОтразитьДвиженияПоРегистру(ДополнительныеСвойства, Движения, Отказ, "ИсходящийНДС");
        Если Истина Тогда
            ПроведениеСервер.ОтразитьДвиженияПоРегистру(ДополнительныеСвойства, Движения, Отказ,
                // перенос аргумента
                "ВходящийНДС");
        КонецЕсли;
        ПроведениеСервер.ОтразитьДвиженияПоРегистру(ДополнительныеСвойства, Движения, Отказ, ИмяРегистра);
        // ПроведениеСервер.ОтразитьДвиженияПоРегистру(ДополнительныеСвойства, Движения, Отказ, "Призрак");
        ПроведениеСервер.ОтразитьДвиженияПоРегистру(ДополнительныеСвойства, Движения, Отказ, "НетТакого");
    КонецПроцедуры
""")


def _by_name_tree(tmp_path) -> Path:
    root = tmp_path / "cfg"
    _write(root / "Configuration.xml", CF_DESCRIPTOR)
    for reg in ("ИсходящийНДС", "ВходящийНДС"):
        (root / "AccumulationRegisters" / reg).mkdir(parents=True)
    _write(root / "Documents" / "Перенос" / "Ext" / "ObjectModule.bsl", _DELEGATE_DOC)
    _write(
        root / "Documents" / "Обычный" / "Ext" / "ObjectModule.bsl",
        "Процедура ОбработкаПроведения(Отказ, Режим)\n    Движения.ИсходящийНДС.Записать();\nКонецПроцедуры\n",
    )
    _write(
        root / "CommonModules" / "ПроведениеСервер" / "Ext" / "Module.bsl",
        "Процедура ОтразитьДвиженияПоРегистру(ДополнительныеСвойства, Движения, Отказ, ИмяТаблицы) Экспорт\n"
        "    Движения[ИмяТаблицы].Записывать = Истина;\nКонецПроцедуры\n",
    )
    return root


@pytest.mark.parametrize("with_index", [False, True], ids=["live", "index"])
def test_register_by_name_delegate_rows(tmp_path, monkeypatch, with_index):
    root = _by_name_tree(tmp_path)
    reader = _reader_for(root, monkeypatch, build_calls=False, build_metadata=True) if with_index else None
    try:
        res = _bsl_for(root, reader)["find_register_movements"]("Перенос")
        assert sorted(r["name"] for r in res["code_registers"]) == ["ВходящийНДС", "ИсходящийНДС"]
        assert "posting_handler_present" not in res  # движения есть — сигнала «прямых нет» быть не должно
        if with_index:
            kinds = sorted((u["kind"], u["evidence"]) for u in res["unresolved"])
            assert kinds == [("dynamic_name", "ИмяРегистра"), ("unknown_register", "НетТакого")]
        else:
            assert res["unresolved"] == [] and res["_meta"]["unresolved_reason"] == "index_required"
    finally:
        if reader:
            reader.close()


@pytest.mark.parametrize("with_index", [False, True], ids=["live", "index"])
def test_plain_movements_answer_is_unchanged(tmp_path, monkeypatch, with_index):
    root = _by_name_tree(tmp_path)
    reader = _reader_for(root, monkeypatch, build_calls=False, build_metadata=True) if with_index else None
    try:
        res = _bsl_for(root, reader)["find_register_movements"]("Обычный")
        assert [r["name"] for r in res["code_registers"]] == ["ИсходящийНДС"]
        assert res["unresolved"] == []
    finally:
        if reader:
            reader.close()


@pytest.mark.parametrize("with_index", [False, True], ids=["live", "index"])
def test_delegate_in_extension_posting_interceptor_adds_the_movement(tmp_path, monkeypatch, with_index):
    """Делегат в перехвате обработчика расширения: движение ДОБАВЛЯЕТСЯ, а не только гасит сигнал."""
    cf = tmp_path / "src" / "cf"
    cfe = tmp_path / "src" / "cfe" / "Доработки"
    _write(cf / "Configuration.xml", CF_MAIN_DESCRIPTOR)
    (cf / "AccumulationRegisters" / "ИсходящийНДС").mkdir(parents=True)
    _write(
        cf / "Documents" / "Перенос" / "Ext" / "ObjectModule.bsl",
        "Процедура ОбработкаПроведения(Отказ, Режим)\nКонецПроцедуры\n",
    )
    _write(
        cf / "Documents" / "Обычный" / "Ext" / "ObjectModule.bsl",  # таблица движений индекса не пуста
        "Процедура ОбработкаПроведения(Отказ, Режим)\n    Движения.ИсходящийНДС.Записать();\nКонецПроцедуры\n",
    )
    _write(
        cf / "CommonModules" / "ПроведениеСервер" / "Ext" / "Module.bsl",
        "Процедура ОтразитьДвиженияПоРегистру(ДополнительныеСвойства, Движения, Отказ, ИмяТаблицы) Экспорт\n"
        "КонецПроцедуры\n",
    )
    _write(cfe / "Configuration.xml", CF_EXT_DESCRIPTOR)
    _write(
        cfe / "Documents" / "Перенос" / "Ext" / "ObjectModule.bsl",
        '&После("ОбработкаПроведения")\nПроцедура др_ОбработкаПроведения(Отказ, Режим)\n'
        '    ПроведениеСервер.ОтразитьДвиженияПоРегистру(ДополнительныеСвойства, Движения, Отказ, "ИсходящийНДС");\n'
        "КонецПроцедуры\n",
    )
    reader = _reader_for(cf, monkeypatch, build_calls=False, build_metadata=True) if with_index else None
    try:
        res = _bsl_for(cf, reader, extension_paths=[str(cfe)])["find_register_movements"]("Перенос")
        rows = [(r["name"], r["file"]) for r in res["code_registers"]]
        assert rows == [("ИсходящийНДС", "../cfe/Доработки/Documents/Перенос/Ext/ObjectModule.bsl")]
        assert "posting_handler_present" not in res
    finally:
        if reader:
            reader.close()


# ═══════════════ Ревью: стыки, найденные после реализации ═══════════════


def _ov_pair_tree(tmp_path, modules: dict[str, tuple[str, str]]) -> tuple[Path, Path]:
    """Основная + расширение; ``modules``: путь → (тело в основной, тело в расширении | "")."""
    cf = tmp_path / "src" / "cf"
    cfe = tmp_path / "src" / "cfe" / "Доработки"
    _write(cf / "Configuration.xml", CF_MAIN_DESCRIPTOR)
    _write(cfe / "Configuration.xml", CF_EXT_DESCRIPTOR)
    for rel, (main_body, ext_body) in modules.items():
        _write(cf / rel, main_body)
        if ext_body:
            _write(cfe / rel, ext_body)
    return cf, cfe


def _override_methods(bsl, rel: str, method: str) -> tuple[bool, list[str]]:
    text = bsl["read_procedure"](rel, method, include_overrides=True) or ""
    by = [o["extension_method"] for p in bsl["extract_procedures"](rel) for o in (p.get("overridden_by") or [])]
    return "Перехвачен" in text, by


@pytest.mark.parametrize("with_index", [False, True], ids=["live", "index"])
def test_module_outside_known_categories_keeps_its_override(tmp_path, monkeypatch, with_index):
    """Модуль вне словаря категорий (последовательность, сервис интеграции): у `parse_bsl_path` нет ни
    категории, ни объекта, но путь модуля однозначен — перехват не теряется (на индексе его видела и 1.42)."""
    seq = "Sequences/Посл/Ext/RecordSetModule.bsl"
    svc = "IntegrationServices/Сервис/Ext/Module.bsl"
    cf, cfe = _ov_pair_tree(
        tmp_path,
        {
            seq: (
                "Процедура ПередЗаписью(Отказ, Замещение)\nКонецПроцедуры\n",
                '&После("ПередЗаписью")\nПроцедура др_ПослПередЗаписью(Отказ, Замещение)\nКонецПроцедуры\n',
            ),
            svc: (
                "Процедура ОбработкаПолучения(Сообщение)\nКонецПроцедуры\n",
                '&После("ОбработкаПолучения")\nПроцедура др_ОбработкаПолучения(Сообщение)\nКонецПроцедуры\n',
            ),
        },
    )
    reader = (
        _reader_for(cf, monkeypatch, build_calls=False, build_metadata=False, build_fts=False) if with_index else None
    )
    try:
        bsl = _bsl_for(cf, reader, extension_paths=[str(cfe)])
        assert _override_methods(bsl, seq, "ПередЗаписью")[0]
        assert _override_methods(bsl, svc, "ОбработкаПолучения")[0]
        if with_index:
            assert _override_methods(bsl, seq, "ПередЗаписью")[1] == ["др_ПослПередЗаписью"]
            assert _override_methods(bsl, svc, "ОбработкаПолучения")[1] == ["др_ОбработкаПолучения"]
    finally:
        if reader:
            reader.close()


@pytest.mark.parametrize("with_index", [False, True], ids=["live", "index"])
def test_external_data_source_tables_do_not_share_overrides(tmp_path, monkeypatch, with_index):
    """Таблицы одного внешнего источника различает только сегмент `Tables/<Имя>`: перехват модуля таблицы А
    не приписывается модулю таблицы Б."""
    tab_a = "ExternalDataSources/Источник/Tables/ТабА/Ext/ObjectModule.bsl"
    tab_b = "ExternalDataSources/Источник/Tables/ТабБ/Ext/ObjectModule.bsl"
    body = "Процедура ПередЗаписью(Отказ)\nКонецПроцедуры\n"
    cf, cfe = _ov_pair_tree(
        tmp_path,
        {
            tab_a: (body, '&После("ПередЗаписью")\nПроцедура др_ТабАПередЗаписью(Отказ)\nКонецПроцедуры\n'),
            tab_b: (body, ""),
        },
    )
    reader = (
        _reader_for(cf, monkeypatch, build_calls=False, build_metadata=False, build_fts=False) if with_index else None
    )
    try:
        bsl = _bsl_for(cf, reader, extension_paths=[str(cfe)])
        assert _override_methods(bsl, tab_a, "ПередЗаписью")[0]
        assert _override_methods(bsl, tab_b, "ПередЗаписью") == (False, [])
    finally:
        if reader:
            reader.close()


def test_transient_reader_failure_does_not_poison_the_override_map(ov_tree, monkeypatch):
    """Окно пересборки на месте: ридер один раз отдаёт None (так `_transient_safe` гасит `database is
    locked`). Пустая карта не кешируется — следующий вызов видит перехваты, как и до релиза."""
    cf, cfe = ov_tree
    reader = _reader_for(cf, monkeypatch, build_calls=False, build_metadata=False, build_fts=False)
    try:
        real = reader.get_extension_overrides
        calls = {"n": 0}

        def flaky(*a, **kw):
            calls["n"] += 1
            return None if calls["n"] == 1 else real(*a, **kw)

        reader.get_extension_overrides = flaky  # instance attr shadows method
        bsl = _bsl_for(cf, reader, extension_paths=[str(cfe)])
        bsl["read_procedure"](_CATALOG_MAIN, "ПередЗаписью", include_overrides=True)  # первый вызов — в окне сбоя
        assert "Перехвачен" in bsl["read_procedure"](_CATALOG_MAIN, "ПриЗаписи", include_overrides=True)
        other = "Documents/Заказ/Ext/ObjectModule.bsl"
        assert [
            o["extension_method"] for p in bsl["extract_procedures"](other) for o in p.get("overridden_by") or []
        ] == ["др_ДокПередЗаписью"]
        assert calls["n"] >= 2
    finally:
        reader.close()


_EDT_ADOPTED_DOC = (
    '<mdclass:Document xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass"><name>Заказ</name>'
    "<objectBelonging>Adopted</objectBelonging>{extra}</mdclass:Document>"
)


@pytest.mark.parametrize("extra, expected", [("", None), ("<posting>Deny</posting>", "Deny")])
def test_edt_adopted_document_posting_is_not_assumed(extra, expected):
    """Заимствованный объект расширения EDT наследует проведение из основной конфигурации: тега нет —
    значение не читалось, умолчание `Allow` к нему не применяется."""
    assert parse_metadata_xml(_EDT_ADOPTED_DOC.format(extra=extra)).get("posting") == expected


def test_ui_strings_starting_with_select_are_not_queries(tmp_path):
    q = _queries(
        tmp_path,
        "Процедура П()\n"
        '    Сообщить("Выбрать из списка");\n'
        '    Кнопка.Заголовок = "Выбрать как основной";\n'
        '    Подсказка = "Select from the list";\n'
        '    ТекстОтбора = "Выбрать Ссылка Из Перечисление.Виды Где Ссылка = &Значение";\n'
        "КонецПроцедуры\n",
    )
    # Мешаный регистр — запрос, раз источник доказан. v1.44.0: `tables` знает все виды источников
    # запроса — прежде здесь стоял `[]`, потому что таблицы `Перечисление.X` не распознавались.
    assert [(x["line"], x["tables"]) for x in q] == [(5, ["Перечисление.Виды"])]
    assert q[0]["text_preview"].startswith("Выбрать Ссылка Из Перечисление.Виды")


def test_typed_name_absent_in_its_category_is_a_named_error(ov_bsl):
    """Объект есть, но в другой категории: ошибка с категориями, а не ошибка файловой системы."""
    bsl, _cf, _cfe = ov_bsl
    for helper in ("get_module_outline", "extract_procedures"):
        with pytest.raises(ValueError, match="Catalogs"):
            bsl[helper]("Отчет.Склад")


# ═══════════════ Ревью codex ═══════════════

# Модули, названные словом раскладки выгрузки: (путь Конфигуратора, путь EDT).
_LAYOUT_WORD_MODULES = (
    ("Catalogs/Ext/Ext/ObjectModule.bsl", "Catalogs/Ext/ObjectModule.bsl"),
    ("Catalogs/Ext/Forms/Form/Ext/Form/Module.bsl", "Catalogs/Ext/Forms/Form/Module.bsl"),
    ("Catalogs/Заказ/Forms/Ext/Ext/Form/Module.bsl", "Catalogs/Заказ/Forms/Ext/Module.bsl"),
    ("Catalogs/Заказ/Commands/Ext/Ext/CommandModule.bsl", "Catalogs/Заказ/Commands/Ext/CommandModule.bsl"),
    ("CommonForms/Ext/Ext/Form/Module.bsl", "CommonForms/Ext/Module.bsl"),
    ("CommonModules/Ext/Ext/Module.bsl", "CommonModules/Ext/Module.bsl"),
    ("CommonCommands/Ext/Ext/CommandModule.bsl", "CommonCommands/Ext/CommandModule.bsl"),
    ("Catalogs/Configuration/Ext/ObjectModule.bsl", "Catalogs/Configuration/ObjectModule.bsl"),
)


@pytest.mark.parametrize("with_index", [False, True], ids=["live", "index"])
@pytest.mark.parametrize("main_fmt, ext_fmt", [("cf", "edt"), ("edt", "cf")], ids=["cf-edt", "edt-cf"])
def test_layout_word_as_a_name_stays_in_the_module_identity(tmp_path, monkeypatch, main_fmt, ext_fmt, with_index):
    """Объект, форма, команда, общие форма, модуль и команда с именем `Ext` (`Form`, `Configuration`): элемент
    раскладки выгрузки судится по ПОЛОЖЕНИЮ в пути, а не по имени. Перехват доходит до своего модуля при любой
    паре раскладок (на индексе его видела и 1.42), соседняя форма того же объекта его не получает."""
    pick = {"cf": 0, "edt": 1}
    cf = tmp_path / "src" / "cf"
    cfe = tmp_path / "src" / "cfe" / "Доработки"
    if main_fmt == "cf":
        _write(cf / "Configuration.xml", CF_MAIN_DESCRIPTOR)
    else:
        _write(cf / "Configuration" / "Configuration.mdo", EDT_DESCRIPTOR)
    if ext_fmt == "cf":
        _write(cfe / "Configuration.xml", CF_EXT_DESCRIPTOR)
    else:
        _write(cfe / "Configuration" / "Configuration.mdo", EDT_EXT_DESCRIPTOR)
    expected = {}
    for i, paths in enumerate(_LAYOUT_WORD_MODULES, 1):
        main_rel = paths[pick[main_fmt]]
        _write(cf / main_rel, "Процедура П() Экспорт\nКонецПроцедуры\n")
        _write(cfe / paths[pick[ext_fmt]], f'&После("П")\nПроцедура Пер{i}()\nКонецПроцедуры\n')
        expected[main_rel] = f"Пер{i}"
    neighbour = ("Catalogs/Заказ/Forms/ФормаА/Ext/Form/Module.bsl", "Catalogs/Заказ/Forms/ФормаА/Module.bsl")
    neighbour = neighbour[pick[main_fmt]]
    _write(cf / neighbour, "Процедура П() Экспорт\nКонецПроцедуры\n")
    reader = (
        _reader_for(cf, monkeypatch, build_calls=False, build_metadata=False, build_fts=False) if with_index else None
    )
    try:
        bsl = _bsl_for(cf, reader, extension_paths=[str(cfe)])
        seen = {}
        for rel in expected:
            text = bsl["read_procedure"](rel, "П", include_overrides=True) or ""
            seen[rel] = [m for m in expected.values() if f"Процедура {m}()" in text]
        assert seen == {rel: [m] for rel, m in expected.items()}
        assert _override_methods(bsl, neighbour, "П") == (False, [])
        if with_index:
            by = {rel: _override_methods(bsl, rel, "П")[1] for rel in expected}
            assert by == {rel: [m] for rel, m in expected.items()}
            object_module = _LAYOUT_WORD_MODULES[0][pick[main_fmt]]
            rows = bsl["get_overrides"]("Справочник.Ext")["overrides"]
            [row] = [r for r in rows if r["extension_method"] == "Пер1"]
            assert (row["source_path"], row["target_method_line"]) == (object_module, 1)
            modules = bsl["get_object_modules"]("Справочник.Ext")["modules"]  # и модули расширения — `../cfe/…`
            assert {m["path"]: m["overrides"]["count"] for m in modules if not m["path"].startswith("../")} == {
                object_module: 1,
                _LAYOUT_WORD_MODULES[1][pick[main_fmt]]: 1,
            }
    finally:
        if reader:
            reader.close()


def _pf_dag_tree(tmp_path, manager_calls: list[str], modules: dict[str, tuple[str, list[str]]]) -> Path:
    """Документ X; ``modules``: общий модуль → (своя команда | "", вызываемые им общие модули)."""
    root = tmp_path / "cfg"
    _write(root / "Configuration.xml", CF_DESCRIPTOR)

    def calls(names):
        return "".join(f"    {n}.ДобавитьКомандыПечати(КомандыПечати);\n" for n in names)

    _write(root / "Documents" / "X" / "Ext" / "ManagerModule.bsl", _pf_proc(calls(manager_calls)))
    for name, (cmd, callees) in modules.items():
        own = _pf_cmd("К", cmd, cmd) if cmd else ""
        _write(root / "CommonModules" / name / "Ext" / "Module.bsl", _pf_proc(own + calls(callees)))
    return root


def test_print_form_delegate_reached_by_a_shorter_path_is_expanded_again(tmp_path):
    """Документ зовёт А и Д; А → Б → Д → Е. Первым обход доходит до Д на третьем переходе и упирается в
    предел у Е; прямой вызов Д — первый переход, Е на нём второй. Повторный заход — не цикл."""
    root = _pf_dag_tree(tmp_path, ["А", "Д"], {"А": ("", ["Б"]), "Б": ("", ["Д"]), "Д": ("Д", ["Е"]), "Е": ("Е", [])})
    res = _bsl_for(root)["find_print_forms"]("Документ.X")
    assert [r["name"] for r in res["print_forms"]] == ["Д", "Е"]
    entries = [(d["call"], d["depth"], d["status"]) for d in res["_meta"]["delegates_expanded"]]
    assert ("Е.ДобавитьКомандыПечати", 2, "expanded") in entries
    assert ("Д.ДобавитьКомандыПечати", 1, "expanded") in entries
    assert all(status != "cycle" for _call, _depth, status in entries)


def test_print_form_delegate_called_twice_is_not_a_cycle(tmp_path):
    """Ромб: А и Б оба зовут В на одном переходе. Второй вызов — уже раскрытый делегат (новых строк нет),
    а не цикл; настоящий цикл — вызов процедуры со стека обхода."""
    root = _pf_dag_tree(
        tmp_path, ["А", "Б"], {"А": ("А", ["В"]), "Б": ("Б", ["В"]), "В": ("В", ["Г"]), "Г": ("Г", ["В"])}
    )
    res = _bsl_for(root)["find_print_forms"]("Документ.X")
    assert [r["name"] for r in res["print_forms"]] == ["А", "В", "Г", "Б"]
    entries = [(d["call"], d["depth"], d["status"], d["commands"]) for d in res["_meta"]["delegates_expanded"]]
    assert entries == [
        ("А.ДобавитьКомандыПечати", 1, "expanded", 1),
        ("В.ДобавитьКомандыПечати", 2, "expanded", 1),
        ("Г.ДобавитьКомандыПечати", 3, "expanded", 1),
        ("В.ДобавитьКомандыПечати", 4, "depth_limit", 0),
        ("Б.ДобавитьКомандыПечати", 1, "expanded", 1),
        ("В.ДобавитьКомандыПечати", 2, "expanded", 0),
    ]


_CI_TEXT = (
    'Сообщить("ı");\n'  # турецкая i без точки
    'Сообщить("İx");\n'  # İ — строчная из двух символов
    'Сообщить("ΟΣ");\n'  # финальная сигма: lower() даёт ς
    'Сообщить("ſ");\n'  # длинная s
    'Сообщить("Неопределенᲂ");\n'  # ᲂ — узкое «о» (U+1C82), регистровая пара «О»
    'Сообщить("ВЫБРАТЬ");\n'
)


@pytest.mark.parametrize("pattern", ["i", "ix", "Σ", "σ", "ΟΣ", "S", "Неопределено", "выбрать", "İ"])
def test_count_matches_ignore_case_counts_like_the_regex_engine(tmp_path, pattern):
    """Литерал без учёта регистра считается так же, как `re.IGNORECASE` — до релиза и у `regex=True`:
    `lower()` не сворачивает `ı`, `ſ`, финальную сигму и исторические варианты кириллицы."""
    import re

    root = tmp_path / "cfg"
    _write(root / "Configuration.xml", CF_DESCRIPTOR)
    _write(root / "CommonModules" / "М" / "Ext" / "Module.bsl", _CI_TEXT)
    expected = sum(1 for line in _CI_TEXT.splitlines() if re.search(re.escape(pattern), line, re.IGNORECASE))
    bsl = _bsl_for(root)
    assert bsl["count_matches"](pattern)["occurrences"] == expected
    assert bsl["count_matches"](re.escape(pattern), regex=True)["occurrences"] == expected


@pytest.mark.parametrize("with_index", [False, True], ids=["live", "index"])
def test_delegate_in_extension_reports_unresolved_names(tmp_path, monkeypatch, with_index):
    """Делегат «регистр по имени» в перехвате обработчика расширения: вычисляемое имя и литерал вне
    каталога регистров — строки `unresolved` с файлом модуля расширения, как у основной конфигурации
    (канал объявлен доступным — пустой список был бы ложным «всё разрешилось»)."""
    cf = tmp_path / "src" / "cf"
    cfe = tmp_path / "src" / "cfe" / "Доработки"
    _write(cf / "Configuration.xml", CF_MAIN_DESCRIPTOR)
    (cf / "AccumulationRegisters" / "ИсходящийНДС").mkdir(parents=True)
    _write(
        cf / "Documents" / "Перенос" / "Ext" / "ObjectModule.bsl",
        "Процедура ОбработкаПроведения(Отказ, Режим)\nКонецПроцедуры\n",
    )
    _write(
        cf / "Documents" / "Обычный" / "Ext" / "ObjectModule.bsl",  # таблица движений индекса не пуста
        "Процедура ОбработкаПроведения(Отказ, Режим)\n    Движения.ИсходящийНДС.Записать();\nКонецПроцедуры\n",
    )
    _write(cfe / "Configuration.xml", CF_EXT_DESCRIPTOR)
    ext_module = cfe / "Documents" / "Перенос" / "Ext" / "ObjectModule.bsl"
    _write(
        ext_module,
        '&После("ОбработкаПроведения")\nПроцедура др_ОбработкаПроведения(Отказ, Режим)\n'
        '    ПроведениеСервер.ОтразитьДвиженияПоРегистру(Свойства, Движения, Отказ, "ИсходящийНДС");\n'
        "    ПроведениеСервер.ОтразитьДвиженияПоРегистру(Свойства, Движения, Отказ, ИмяРегистра);\n"
        '    ПроведениеСервер.ОтразитьДвиженияПоРегистру(Свойства, Движения, Отказ, "НетТакого");\n'
        "КонецПроцедуры\n",
    )
    reader = _reader_for(cf, monkeypatch, build_calls=False, build_metadata=True) if with_index else None
    try:
        bsl = _bsl_for(cf, reader, extension_paths=[str(cfe)])
        res = bsl["find_register_movements"]("Перенос")
        ext_rel = "../cfe/Доработки/Documents/Перенос/Ext/ObjectModule.bsl"
        assert [(r["name"], r["file"]) for r in res["code_registers"]] == [("ИсходящийНДС", ext_rel)]
        if with_index:
            assert sorted((u["kind"], u["evidence"], u["file"]) for u in res["unresolved"]) == [
                ("dynamic_name", "ИмяРегистра", ext_rel),
                ("unknown_register", "НетТакого", ext_rel),
            ]
            profile = bsl["get_object_profile"]("Документ.Перенос", sections=["registers"])
            section = profile["sections"]["registers"]  # компактный контракт: строк unresolved там нет
            assert [i["register"] for i in section["items"]] == ["ИсходящийНДС"]
            assert section["summary"]["code_registers"] == 1
        else:
            assert res["unresolved"] == [] and res["_meta"]["unresolved_reason"] == "index_required"
    finally:
        if reader:
            reader.close()


def test_transient_reader_failure_does_not_stick_in_the_procedures_cache(ov_tree, monkeypatch):
    """Модуль, разобранный в окне сбоя чтения перехватов, не остаётся без `overridden_by` до конца сессии:
    разбор кешируется, а перехваты дописываются, как только карта перехватов прочитается."""
    cf, cfe = ov_tree
    reader = _reader_for(cf, monkeypatch, build_calls=False, build_metadata=False, build_fts=False)
    try:
        real = reader.get_extension_overrides
        calls = {"n": 0}

        def flaky(*a, **kw):
            calls["n"] += 1
            return None if calls["n"] == 1 else real(*a, **kw)

        reader.get_extension_overrides = flaky  # instance attr shadows method
        bsl = _bsl_for(cf, reader, extension_paths=[str(cfe)])

        def overridden_by():
            procs = bsl["extract_procedures"](_CATALOG_MAIN)
            return [o["extension_method"] for p in procs for o in p.get("overridden_by") or []]

        assert overridden_by() == []  # первый разбор — в окне сбоя
        assert overridden_by() == ["др_ПередЗаписью"]
        assert overridden_by() == ["др_ПередЗаписью"]  # дописано один раз, а не на каждый вызов
        assert calls["n"] == 2
    finally:
        reader.close()


def _described(kind: str, name: str, synonym: str) -> str:
    return (
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses" xmlns:v8="http://v8.1c.ru/8.1/data/core">'
        f"<{kind}><Properties><Name>{name}</Name><Synonym><v8:item><v8:lang>ru</v8:lang>"
        f"<v8:content>{synonym}</v8:content></v8:item></Synonym></Properties></{kind}></MetaDataObject>"
    )


def test_typed_name_is_not_resolved_by_a_substring(tmp_path, monkeypatch):
    """Типизированное имя: синонимный маршрут принимает только ТОЧНОЕ совпадение имени или синонима в своей
    категории — подстрока (`Склад` в `СкладАрхив`, `Архив` в синониме «Склад архив») — другой объект."""
    root = tmp_path / "cfg"
    _write(root / "Configuration.xml", CF_DESCRIPTOR)
    for cat, kind, name, synonym in (
        ("Catalogs", "Catalog", "Склад", "Склад"),
        ("Documents", "Document", "СкладАрхив", "Склад архив"),
    ):
        _write(root / cat / f"{name}.xml", _described(kind, name, synonym))
        _write(root / cat / name / "Ext" / "ObjectModule.bsl", "Процедура П()\nКонецПроцедуры\n")
    reader = _reader_for(root, monkeypatch, build_calls=False, build_metadata=True)
    try:
        bsl = _bsl_for(root, reader)
        for arg in ("Документ.Склад", "Документ.Архив"):
            assert "error" in bsl["get_object_modules"](arg), arg
        for arg in ("Документ.Склад архив", "документ.СКЛАД АРХИВ"):  # точный синоним — объект найден
            res = bsl["get_object_modules"](arg)
            assert (res.get("category"), res.get("object_name")) == ("Documents", "СкладАрхив"), arg
    finally:
        reader.close()


@pytest.mark.parametrize("with_index", [False, True], ids=["live", "index"])
def test_overrides_of_one_method_spelled_differently_are_all_kept(tmp_path, monkeypatch, with_index):
    """`&Перед("ПриЗаписи")` и `&После("ПРИЗАПИСИ")` — два перехвата ОДНОГО метода (1С регистронезависима):
    ни один не теряется, а в счёте перехваченных методов модуля метод один."""
    rel = "Catalogs/X/Ext/ObjectModule.bsl"
    cf, cfe = _ov_pair_tree(
        tmp_path,
        {
            rel: (
                "Процедура ПриЗаписи(Отказ)\nКонецПроцедуры\n",
                '&Перед("ПриЗаписи")\nПроцедура др_Первая(Отказ)\nКонецПроцедуры\n'
                '&После("ПРИЗАПИСИ")\nПроцедура др_Вторая(Отказ)\nКонецПроцедуры\n',
            )
        },
    )
    reader = (
        _reader_for(cf, monkeypatch, build_calls=False, build_metadata=False, build_fts=False) if with_index else None
    )
    try:
        bsl = _bsl_for(cf, reader, extension_paths=[str(cfe)])
        text = bsl["read_procedure"](rel, "ПриЗаписи", include_overrides=True)
        assert "Процедура др_Первая(Отказ)" in text and "Процедура др_Вторая(Отказ)" in text
        if with_index:
            [proc] = bsl["extract_procedures"](rel)
            assert sorted(o["extension_method"] for o in proc["overridden_by"]) == ["др_Вторая", "др_Первая"]
            modules = bsl["get_object_modules"]("Справочник.X")["modules"]
            [module] = [m for m in modules if not m["path"].startswith("../")]
            assert module["overrides"]["count"] == 1
    finally:
        if reader:
            reader.close()


@pytest.mark.parametrize("drop_nav", [False, True], ids=["nav_index", "no_nav_table"])
def test_count_matches_source_names_the_actual_enumeration(tmp_path, monkeypatch, drop_nav):
    """`source` — фактический маршрут перечисления: ридер маску не отдал (нет таблицы навигации, окно
    пересборки) — кандидаты пришли обходом файлов, и это `live`, а не `index`."""
    root = tmp_path / "cfg"
    _write(root / "Configuration.xml", CF_DESCRIPTOR)
    _write(root / "CommonModules" / "М" / "Ext" / "Module.bsl", "Процедура П()\nКонецПроцедуры\n")
    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
    db = IndexBuilder().build(str(root), build_calls=False, build_metadata=False)
    if drop_nav:
        conn = sqlite3.connect(db)
        conn.execute("DROP TABLE file_paths")
        conn.commit()
        conn.close()
    reader = IndexReader(db)
    try:
        res = _bsl_for(root, reader)["count_matches"]("Процедура")
        assert (res["files_matched"], res["source"]) == (1, "live" if drop_nav else "index")
    finally:
        reader.close()


# ═══════════════ Этап 1 e2e: находки Д-1, Д-2 ═══════════════


@pytest.mark.parametrize(
    "file_types, path, expected",
    [
        ("bsl,rights", "", "index+live"),
        ("bsl,rights", "Documents", "index"),
        ("bsl,rights", "Roles", "live"),
        ("bsl", "Нет", "index"),
    ],
    ids=["no_path", "only_index_files", "only_fs_files", "empty_scope"],
)
def test_count_matches_source_names_only_files_in_the_path_scope(edt_files, file_types, path, expected):
    """`source` — происхождение файлов, попавших в область `path`: живая часть, которую фильтр пути отбросил,
    ответ «смешанным» не делает. Пустая область — маршрут перечисления масок."""
    root, reader = edt_files
    res = _bsl_for(root, reader)["count_matches"]("Процедура", file_types=file_types, path=path)
    assert res["source"] == expected


def test_count_matches_source_ignores_extension_modules_outside_the_path(ov_tree, monkeypatch):
    """Модули соседних расширений лежат вне относительного поддерева текущего корня (`../cfe/…`): при `path`
    живой части в ответе нет."""
    cf, cfe = ov_tree
    reader = _reader_for(cf, monkeypatch, build_calls=False, build_metadata=False, build_fts=False)
    try:
        bsl = _bsl_for(cf, reader, extension_paths=[str(cfe)])
        res = bsl["count_matches"]("Процедура", path="Catalogs")
        assert (res["source"], res["extensions_included"]) == ("index", False)
        assert bsl["count_matches"]("Процедура")["source"] == "index+live"
    finally:
        reader.close()


@pytest.mark.parametrize("with_index", [False, True], ids=["live", "index"])
@pytest.mark.parametrize(
    "rel, method, ext_body, expected, absent",
    [
        (
            "CommonModules/Общий/Ext/Module.bsl",
            "Удаленный",
            '&Вместо("Удаленный")\nФункция др_Удаленный()\n    Возврат 1;\nКонецФункции\n',
            "версии основной конфигурации и расширения разошлись",
            "событие платформы",
        ),
        (
            "Catalogs/Товар/Ext/ObjectModule.bsl",
            "ПриЗаписи",
            '&После("ПриЗаписи")\nПроцедура др_ПриЗаписи(Отказ)\nКонецПроцедуры\n',
            "событие платформы",
            "версии основной конфигурации и расширения разошлись",
        ),
    ],
    ids=["common_module", "object_module"],
)
def test_absent_method_note_matches_the_module_kind(
    tmp_path, monkeypatch, with_index, rel, method, ext_body, expected, absent
):
    """У общего модуля событий платформы нет: перехват метода, которого в базовом модуле нет, там означает, что
    версии основной конфигурации и расширения разошлись. У модуля объекта пояснение прежнее — событие платформы."""
    cf, cfe = _ov_pair_tree(tmp_path, {rel: ("Процедура П() Экспорт\nКонецПроцедуры\n", ext_body)})
    reader = (
        _reader_for(cf, monkeypatch, build_calls=False, build_metadata=False, build_fts=False) if with_index else None
    )
    try:
        text = _bsl_for(cf, reader, extension_paths=[str(cfe)])["read_procedure"](rel, method, include_overrides=True)
        assert text.startswith(f"// Метод {method} в базовом модуле не объявлен")
        assert expected in text and absent not in text
    finally:
        if reader:
            reader.close()


def test_override_help_explains_absent_target_line_like_the_note(tmp_path):
    """Справка о `target_method_line=None` (рецепт `help('override')` и тема «расширения» `rlm_help`) объясняет то
    же, что пометка `read_procedure`: у общего модуля событий платформы нет — там это расхождение версий основной
    конфигурации и расширения. Раньше справка называла любую такую строку событием платформы. С v1.47.0 тема
    «расширения» говорит это короче (бюджет полной формы рецепта) — проверяется смысл: у общего модуля — расхождение
    версий."""
    import json

    from rlm_tools_bsl.bsl_knowledge import _get_topic_recipe

    _write(tmp_path / "Configuration.xml", CF_DESCRIPTOR)
    recipe = _bsl_for(tmp_path)["help"]("override")
    topic = json.dumps(_get_topic_recipe("расширения", format="full"), ensure_ascii=False)
    for text in (recipe, topic):
        flat = " ".join(text.replace("#", " ").split())
        assert "target_method_line=None" in flat
        assert "у общего модуля" in flat and "расхождение версий" in flat, flat


_EVENT_FILTER_RE = re.compile(r"event_filter\s*=\s*(?:\[([^\]]*)\]|\(([^)]*)\)|(['\"])(.*?)\3)")


def _event_filter_values(text: str) -> list[str]:
    """Значения фильтра во всех формах записи: список, кортеж, одна строка; пробелы вокруг `=` допустимы."""
    values: list[str] = []
    for m in _EVENT_FILTER_RE.finditer(text):
        if m.group(4) is not None:
            values.append(m.group(4))
        else:
            values += re.findall(r"['\"]([^'\"]+)['\"]", m.group(1) if m.group(1) is not None else m.group(2))
    return values


@pytest.mark.parametrize(
    "text, expected",
    [
        ("event_filter=['BeforeWrite','ПередЗаписью']", ["BeforeWrite", "ПередЗаписью"]),
        ('event_filter=["ПередЗаписью"]', ["ПередЗаписью"]),
        ("event_filter = [\n    'ПередЗаписью',\n]", ["ПередЗаписью"]),
        ("event_filter='ПередЗаписью'", ["ПередЗаписью"]),
        ('event_filter="ПередЗаписью"', ["ПередЗаписью"]),
        ("event_filter=('ПередЗаписью',)", ["ПередЗаписью"]),
        ("event_filter=None, event_filter=event_filter, event_filter = [event_filter]", []),
    ],
    ids=["list", "double_quotes", "spaces_and_newlines", "string", "string_double", "tuple", "not_a_literal"],
)
def test_event_filter_extractor_sees_every_form(text, expected):
    """Сквозная проверка советов слепа, если не видит какую-то форму записи фильтра: одна строка допустима по
    контракту `find_event_subscriptions`, пробелы вокруг `=` и кортеж — обычный Python."""
    assert _event_filter_values(text) == expected


def test_posting_recipe_event_filter_finds_before_write_subscription(tmp_path):
    """Рецепт «проведение» советовал `event_filter=['OnWrite','Posting','ПередЗаписью']`: в поле `event` подписки —
    английское имя события, как в XML, и подписка `BeforeWrite` при таком фильтре молча терялась."""
    from rlm_tools_bsl.bsl_knowledge import _get_topic_recipe

    _write(tmp_path / "Configuration.xml", CF_DESCRIPTOR)
    for name, event in (("СубПередЗаписью", "BeforeWrite"), ("СубПриЗаписи", "OnWrite")):
        _write(
            tmp_path / "EventSubscriptions" / name / "EventSubscription.xml",
            "<MetaDataObject xmlns:md='http://v8.1c.ru/8.3/MDClasses' xmlns:v8='http://v8.1c.ru/8.1/data/core'>"
            f"<md:EventSubscription><md:Properties><md:Name>{name}</md:Name><md:Event>{event}</md:Event>"
            "<md:Handler>CommonModule.МойМодуль.Обработчик</md:Handler></md:Properties></md:EventSubscription>"
            "</MetaDataObject>",
        )
    values = _event_filter_values("\n".join(_get_topic_recipe("проведение", format="full")["steps"]))
    assert values
    rows = _bsl_for(tmp_path)["find_event_subscriptions"]("", event_filter=values)
    assert {r["event"] for r in rows} == {"BeforeWrite", "OnWrite"}, (values, rows)


@pytest.mark.parametrize(
    "rel, has_examples",
    [
        ("src/rlm_tools_bsl/bsl_knowledge.py", True),
        ("src/rlm_tools_bsl/bsl_strategy_data.py", False),  # вторая копия стратегии: примеров пока нет
        ("src/rlm_tools_bsl/bsl_helpers.py", True),
        ("docs/HELPERS.md", True),
        ("docs/full_analysis_prompt.md", True),
    ],
)
def test_recommended_event_filters_name_events_as_stored(rel, has_examples):
    """Фильтр ищет подстроку поля `event`, а там английское имя события, как в XML подписки: русское имя обработчика
    (`ПередЗаписью`) не совпадает никогда. Советы и примеры обязаны называть события так, как они хранятся."""
    values = _event_filter_values((Path(__file__).resolve().parents[1] / rel).read_text(encoding="utf-8"))
    if has_examples:
        assert values, rel
    assert not [v for v in values if not re.fullmatch(r"[A-Za-z]+", v)], (rel, values)


@pytest.mark.parametrize("with_index", [False, True], ids=["live", "index"])
@pytest.mark.parametrize(
    "arg, category",
    [
        ("ОбщийМодуль.Склад", "CommonModules"),
        ("общиймодуль.Склад", "CommonModules"),
        ("CommonModule.Склад", "CommonModules"),
        ("ОбщиеМодули/Склад", "CommonModules"),
        ("ОбщаяФорма.Склад", "CommonForms"),
        ("ОбщиеФормы/Склад", "CommonForms"),
    ],
)
def test_typed_name_of_a_kind_without_reference_type(tmp_path, monkeypatch, with_index, arg, category):
    """Полное имя вида метаданных без ссылочного типа (`ОбщийМодуль.X` — так его отдает
    `Метаданные.ОбщиеМодули.X.ПолноеИмя()`) и русское множественное `ОбщиеМодули/X` — префикс категории,
    как `Документ.X`: на омониме выбирается объект этой категории, а не первый по пути."""
    body = "Процедура П() Экспорт\nКонецПроцедуры\n"
    cf, cfe = _ov_pair_tree(
        tmp_path,
        {
            "Catalogs/Склад/Ext/ManagerModule.bsl": (body, ""),
            "Documents/Склад/Ext/ObjectModule.bsl": (body, ""),
            "CommonModules/Склад/Ext/Module.bsl": (body, '&После("П")\nПроцедура др_П()\nКонецПроцедуры\n'),
            "CommonForms/Склад/Ext/Form/Module.bsl": (body, ""),
        },
    )
    reader = (
        _reader_for(cf, monkeypatch, build_calls=False, build_metadata=False, build_fts=False) if with_index else None
    )
    try:
        bsl = _bsl_for(cf, reader, extension_paths=[str(cfe)])
        assert bsl["get_object_modules"](arg).get("category") == category
        assert bsl["get_overrides"](arg)["total"] == (1 if category == "CommonModules" else 0)
        if "/" not in arg:  # `Категория/Имя` модульные хелперы читают как путь — прежний контракт
            assert bsl["get_module_outline"](arg)["category"] == category
    finally:
        if reader:
            reader.close()
