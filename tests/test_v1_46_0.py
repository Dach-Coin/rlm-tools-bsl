"""v1.46.0 — неверный вопрос — не пустой ответ.

Релиз read-time: схема индекса и BUILDER_VERSION не тронуты. Приватные фикстуры других
файлов не импортируются (каталог tests не пакет) — деревья собираются здесь же.

Тесты «обе версии» проходят и на HEAD `b160fa5`: прогон против выгрузки HEAD —
``PYTHONPATH=<выгрузка>/src uv run pytest tests/test_v1_46_0.py …``.
"""

from __future__ import annotations

import json
import os
import textwrap
from pathlib import Path
from typing import Callable

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
CF_EXT_DESCRIPTOR = textwrap.dedent("""\
    <?xml version="1.0" encoding="UTF-8"?>
    <MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses" xmlns:v8="http://v8.1c.ru/8.1/data/core">
        <Configuration uuid="00000000-0000-0000-0000-000000000002">
            <Properties>
                <ObjectBelonging>Adopted</ObjectBelonging>
                <Name>Расш1</Name>
                <ConfigurationExtensionPurpose>Customization</ConfigurationExtensionPurpose>
                <NamePrefix>Расш1_</NamePrefix>
            </Properties>
        </Configuration>
    </MetaDataObject>
""")
EDT_DESCRIPTOR = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<mdclass:Configuration xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass" name="Тест"/>\n'
)
EDT_EXT_DESCRIPTOR = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<mdclass:Configuration xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
    'xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass" '
    'xmlns:mdclassExtension="http://g5.1c.ru/v8/dt/metadata/mdclass/extension" '
    'uuid="00000000-0000-0000-0000-000000000002">'
    "<name>Расш1</name><objectBelonging>Adopted</objectBelonging>"
    '<extension xsi:type="mdclassExtension:ConfigurationExtension"><defaultRunMode>Checked</defaultRunMode></extension>'
    "<namePrefix>Расш1_</namePrefix><configurationExtensionPurpose>Customization</configurationExtensionPurpose>"
    "</mdclass:Configuration>\n"
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _cf_doc_tree(root: Path, spec: dict[str, str]) -> Path:
    """Дерево выгрузки Конфигуратора: описатель конфигурации + файлы «путь → текст»."""
    _write(root / "Configuration.xml", CF_DESCRIPTOR)
    for rel, text in spec.items():
        _write(root / rel, text)
    return root


def _edt_doc_tree(root: Path, spec: dict[str, str]) -> Path:
    """Дерево проекта EDT: описатель конфигурации + файлы «путь → текст»."""
    _write(root / "Configuration" / "Configuration.mdo", EDT_DESCRIPTOR)
    for rel, text in spec.items():
        _write(root / rel, text)
    return root


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
    """Индекс во временном каталоге рядом с деревом (флаги сборки — build_kw)."""
    monkeypatch.setenv("RLM_INDEX_DIR", str(root.parent / (root.name + "_idx")))
    return IndexReader(IndexBuilder().build(str(root), **build_kw))


@pytest.fixture
def page_one(monkeypatch):
    import rlm_tools_bsl.bsl_helpers as bh

    monkeypatch.setattr(bh, "_FIND_PATH_NODE_LIMIT", 1)
    monkeypatch.setattr(bh, "_DATA_PATH_REFS_PAGE", 1, raising=False)


# ═══════════════ Задача 0 — каркас: описатели объектов и стандартный мир ═══════════════

_CF_MD_NS = (
    'xmlns="http://v8.1c.ru/8.3/MDClasses" xmlns:cfg="http://v8.1c.ru/8.1/data/enterprise/current-config" '
    'xmlns:v8="http://v8.1c.ru/8.1/data/core" xmlns:xr="http://v8.1c.ru/8.3/xcf/readable" '
    'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'
)
_EDT_MD_NS = 'xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass"'


def _cf_md(kind: str, name: str, children: str = "", props: str = "") -> str:
    """Описатель объекта выгрузки Конфигуратора: ``props`` — внутри ``<Properties>``, ``children`` — после."""
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>\n<MetaDataObject {_CF_MD_NS}><{kind}><Properties>'
        f"<Name>{name}</Name>{props}</Properties>{children}</{kind}></MetaDataObject>"
    )


def _edt_md(kind: str, name: str, body: str = "") -> str:
    """Описатель объекта проекта EDT (``.mdo``)."""
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>\n<mdclass:{kind} {_EDT_MD_NS}><name>{name}</name>{body}'
        f"</mdclass:{kind}>\n"
    )


# Каталог категории → тег описателя.
_KIND_OF = {
    "Documents": "Document",
    "Catalogs": "Catalog",
    "CommonModules": "CommonModule",
    "AccumulationRegisters": "AccumulationRegister",
    "InformationRegisters": "InformationRegister",
    "Subsystems": "Subsystem",
    "CommonPictures": "CommonPicture",
    "Enums": "Enum",
    "EventSubscriptions": "EventSubscription",
    "Roles": "Role",
    "Reports": "Report",
    "DataProcessors": "DataProcessor",
    "CommandGroups": "CommandGroup",
    "CommonForms": "CommonForm",
    "DocumentJournals": "DocumentJournal",
    "DefinedTypes": "DefinedType",
    "ExchangePlans": "ExchangePlan",
    "Tasks": "Task",
    "StyleItems": "StyleItem",
}


def _cf_syn(text: str) -> str:
    return f"<Synonym><v8:item><v8:lang>ru</v8:lang><v8:content>{text}</v8:content></v8:item></Synonym>"


def _edt_syn(text: str) -> str:
    return f"<synonym><key>ru</key><value>{text}</value></synonym>"


def _edt_rel(cf_rel: str) -> str:
    """Путь файла объекта в раскладке EDT по пути CF (относительно каталога объекта): модули без ``Ext``,
    модуль формы — ``Forms/<Ф>/Module.bsl``, права роли — ``Rights.rights``."""
    if cf_rel.endswith("/Ext/Form/Module.bsl"):
        return cf_rel[: -len("/Ext/Form/Module.bsl")] + "/Module.bsl"
    if cf_rel.endswith("/Ext/Form.xml"):
        return cf_rel[: -len("/Ext/Form.xml")] + "/Form.form"
    if cf_rel == "Ext/Rights.xml":
        return "Rights.rights"
    return cf_rel[len("Ext/") :] if cf_rel.startswith("Ext/") else cf_rel


def _obj_file(fmt: str, category: str, name: str, cf_rel: str) -> str:
    """Путь файла объекта от корня конфигурации в раскладке ``fmt`` (``cf_rel`` — путь CF внутри объекта)."""
    return f"{category}/{name}/{cf_rel if fmt == 'cf' else _edt_rel(cf_rel)}"


def _descriptor_rel(fmt: str, category: str, name: str) -> str:
    return f"{category}/{name}.xml" if fmt == "cf" else f"{category}/{name}/{name}.mdo"


def _objects_tree(
    root: Path,
    fmt: str,
    objects: list[tuple[str, str, dict, dict]],
    descriptor: str | None = None,
) -> Path:
    """Объекты в раскладке CF/EDT: ``(каталог категории, имя, {путь CF внутри объекта: текст | (cf, edt)},
    доп. XML)``; доп. XML — ``{'cf_props', 'cf_children', 'edt'}`` (любые ключи необязательны). Вложенная
    подсистема — имя ``'Верх/Вложенная'``. ``descriptor`` — описатель конфигурации (по умолчанию — основной)."""
    if fmt == "cf":
        _write(root / "Configuration.xml", descriptor or CF_DESCRIPTOR)
    else:
        _write(root / "Configuration" / "Configuration.mdo", descriptor or EDT_DESCRIPTOR)
    for category, name, files, extra in objects:
        kind = _KIND_OF[category]
        *parents, own = name.split("/")
        obj_dir = root / category
        for parent in parents:
            obj_dir = obj_dir / parent / category  # вложенная подсистема: Subsystems/Верх/Subsystems/…
        if fmt == "cf":
            _write(
                obj_dir / f"{own}.xml",
                _cf_md(kind, own, extra.get("cf_children", ""), extra.get("cf_props", "")),
            )
            (obj_dir / own).mkdir(parents=True, exist_ok=True)
        else:
            _write(obj_dir / own / f"{own}.mdo", _edt_md(kind, own, extra.get("edt", "")))
        for cf_rel, text in files.items():
            body = text if isinstance(text, str) else text[0 if fmt == "cf" else 1]
            if body is not None:  # None — файла в этой раскладке нет
                _write(obj_dir / own / (cf_rel if fmt == "cf" else _edt_rel(cf_rel)), body)
    return root


def _ext_tree(root: Path, fmt: str, main: list, exts: dict[str, list]) -> tuple[Path, dict[str, Path]]:
    """Основная конфигурация в ``root/cf`` и расширения в ``root/cfe/<Имя>`` (раскладка «рядом с основной»)."""
    cf = _objects_tree(root / "cf", fmt, main)
    roots: dict[str, Path] = {}
    for name, objects in exts.items():
        descriptor = (CF_EXT_DESCRIPTOR if fmt == "cf" else EDT_EXT_DESCRIPTOR).replace("Расш1", name)
        roots[name] = _objects_tree(root / "cfe" / name, fmt, objects, descriptor)
    return cf, roots


def _strip_times(obj):
    """Копия ответа без ``elapsed_ms``/``total_elapsed_ms`` на любой глубине."""
    if isinstance(obj, dict):
        return {k: _strip_times(v) for k, v in obj.items() if k not in ("elapsed_ms", "total_elapsed_ms")}
    if isinstance(obj, list):
        return [_strip_times(v) for v in obj]
    if isinstance(obj, tuple):
        return tuple(_strip_times(v) for v in obj)
    return obj


def _without(obj, keys):
    """Копия ответа без ключей ``keys`` верхнего уровня и в ``_meta``."""
    if not isinstance(obj, dict):
        return obj
    out = {k: v for k, v in obj.items() if k not in keys}
    if isinstance(out.get("_meta"), dict):
        out["_meta"] = {k: v for k, v in out["_meta"].items() if k not in keys}
    return out


PROC = "Процедура П() Экспорт\nКонецПроцедуры\n"

_CF_FORM_LIST = (
    '<Form xmlns="http://v8.1c.ru/8.3/xcf/logform" xmlns:v8="http://v8.1c.ru/8.1/data/core">'
    '<Attributes><Attribute name="Список"><Type><v8:Type>cfg:DynamicList</v8:Type></Type>'
    "<MainAttribute>true</MainAttribute></Attribute></Attributes></Form>"
)
_EDT_FORM_LIST = (
    '<form:Form xmlns:form="http://g5.1c.ru/v8/dt/form"><form:attributes><name>Список</name>'
    "<main>true</main><form:valueType><types>DynamicList</types></form:valueType>"
    "</form:attributes></form:Form>"
)
_RIGHTS_FLAGGED = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<Rights xmlns="http://v8.1c.ru/8.2/roles">\n'
    "<setForNewObjects>true</setForNewObjects>\n"
    "<setForAttributesByDefault>true</setForAttributesByDefault>\n"
    "</Rights>\n"
)
_DOC_OBJECT_MODULE = (
    "Процедура ОбработкаПроведения(Отказ, Режим)\n"
    "\tДвижения.Рег.Записывать = Истина;\n"
    "КонецПроцедуры\n\n"
    "Процедура ОбработкаЗаполнения(ДанныеЗаполнения, ТекстЗаполнения, СтандартнаяОбработка)\n"
    '\tЕсли ТипЗнч(ДанныеЗаполнения) = Тип("СправочникСсылка.Спр") Тогда\n'
    "\tКонецЕсли;\n"
    "КонецПроцедуры\n\n"
    "Процедура др_Доработка() Экспорт\n"
    "КонецПроцедуры\n"
)
_DOC_MANAGER_MODULE = (
    "Процедура ДобавитьКомандыПечати(КомандыПечати) Экспорт\n"
    "\tКомандаПечати = КомандыПечати.Добавить();\n"
    '\tКомандаПечати.Идентификатор = "Накладная";\n'
    "\tКомандаПечати.Представление = НСтр(\"ru = 'Накладная'\");\n"
    "КонецПроцедуры\n"
)
_EXT_DOC_MODULE = (
    '&После("ОбработкаПроведения")\nПроцедура Расш1_ОбработкаПроведенияПосле(Отказ, Режим)\nКонецПроцедуры\n'
)

# Стандартный мир (Задача 0): документ `Док` (модули объекта и менеджера, форма с динамическим списком,
# реквизит типа `Спр`, запись в `Рег`), справочник `Спр` (модуль объекта), справочник `Пустой` (только
# описатель), общие модули `ОбщМод`, `ОбщМод2`, `ВерхУровень`, `ОбработчикКартинка` (подстроки имён `Верх` и
# `Картинка`), регистр накопления `Рег`, подсистема `Верх` с вложенной `Вложенная`, общая картинка `Картинка`,
# перечисление `ПерБезМодуля` (только описатели), подписка на набор `DocumentObject`, роль `Роль1` с флагом
# «для новых объектов».
_WORLD_MAIN = [
    (
        "Documents",
        "Док",
        {
            "Ext/ObjectModule.bsl": _DOC_OBJECT_MODULE,
            "Ext/ManagerModule.bsl": _DOC_MANAGER_MODULE,
            "Forms/ФормаСписка/Ext/Form.xml": (_CF_FORM_LIST, _EDT_FORM_LIST),
            "Forms/ФормаСписка/Ext/Form/Module.bsl": "Процедура ПриСозданииНаСервере(Отказ, СтандартнаяОбработка)\n"
            "КонецПроцедуры\n",
            "Templates/Макет.xml": (
                _cf_md("Template", "Макет", props="<TemplateType>SpreadsheetDocument</TemplateType>"),
                None,
            ),
            "Templates/Макет/Ext/Template.xml": ("<document/>", None),
        },
        {
            "cf_props": _cf_syn("Тестовый документ")
            + '<Posting>Allow</Posting><RegisterRecords><xr:Item xsi:type="xr:MDObjectRef">'
            "AccumulationRegister.Рег</xr:Item></RegisterRecords>"
            '<BasedOn><xr:Item xsi:type="xr:MDObjectRef">Catalog.Спр</xr:Item></BasedOn>',
            "cf_children": "<ChildObjects><Attribute><Properties><Name>Реквизит</Name><Type>"
            "<v8:Type>cfg:CatalogRef.Спр</v8:Type></Type></Properties></Attribute>"
            "<TabularSection><Properties><Name>Товары</Name></Properties><ChildObjects><Attribute><Properties>"
            "<Name>Количество</Name></Properties></Attribute></ChildObjects></TabularSection>"
            "<Form>ФормаСписка</Form><Template>Макет</Template></ChildObjects>",
            "edt": _edt_syn("Тестовый документ") + "<posting>Allow</posting>"
            "<registerRecords>AccumulationRegister.Рег</registerRecords>"
            "<attributes><name>Реквизит</name><type><types>CatalogRef.Спр</types></type></attributes>"
            "<tabularSections><name>Товары</name><attributes><name>Количество</name></attributes>"
            "</tabularSections><forms><name>ФормаСписка</name></forms><basedOn>Catalog.Спр</basedOn>"
            "<templates><name>Макет</name><templateType>SpreadsheetDocument</templateType></templates>",
        },
    ),
    (
        "Catalogs",
        "Спр",
        {"Ext/ObjectModule.bsl": PROC},
        {
            "cf_props": _cf_syn("Тестовый справочник"),
            "cf_children": "<ChildObjects><Attribute><Properties><Name>РеквизитСпр</Name></Properties></Attribute>"
            "</ChildObjects>",
            "edt": _edt_syn("Тестовый справочник") + "<attributes><name>РеквизитСпр</name></attributes>",
        },
    ),
    ("Catalogs", "Пустой", {}, {}),
    (
        "CommonModules",
        "ОбщМод",
        {"Ext/Module.bsl": "Процедура ОбщаяПроцедура() Экспорт\nКонецПроцедуры\n"},
        {"cf_props": _cf_syn("Общий модуль тест"), "edt": _edt_syn("Общий модуль тест")},
    ),
    ("CommonModules", "ОбщМод2", {"Ext/Module.bsl": "Процедура Вторая() Экспорт\nКонецПроцедуры\n"}, {}),
    ("CommonModules", "ВерхУровень", {"Ext/Module.bsl": "Процедура Верхняя() Экспорт\nКонецПроцедуры\n"}, {}),
    (
        "CommonModules",
        "ОбработчикКартинка",
        {"Ext/Module.bsl": "Процедура Картиночная() Экспорт\nКонецПроцедуры\n"},
        {},
    ),
    (
        "AccumulationRegisters",
        "Рег",
        {},
        {"cf_props": _cf_syn("Регистр тест"), "edt": _edt_syn("Регистр тест")},
    ),
    (
        "Subsystems",
        "Верх",
        {},
        {
            "cf_props": '<Content><xr:Item xsi:type="xr:MDObjectRef">Document.Док</xr:Item></Content>',
            "cf_children": "<ChildObjects><Subsystem>Вложенная</Subsystem></ChildObjects>",
            "edt": "<content>Document.Док</content><subsystems>Вложенная</subsystems>",
        },
    ),
    (
        "Subsystems",
        "Верх/Вложенная",
        {},
        {
            "cf_props": '<Content><xr:Item xsi:type="xr:MDObjectRef">Catalog.Спр</xr:Item></Content>',
            "edt": "<content>Catalog.Спр</content>",
        },
    ),
    ("CommonPictures", "Картинка", {}, {}),
    (
        "Enums",
        "ПерБезМодуля",
        {},
        {
            "cf_children": "<ChildObjects><EnumValue><Properties><Name>Значение1</Name></Properties>"
            "</EnumValue></ChildObjects>",
            "edt": "<enumValues><name>Значение1</name></enumValues>",
        },
    ),
    (
        "EventSubscriptions",
        "ПодпНабор",
        {},
        {
            "cf_props": "<Source><v8:TypeSet>cfg:DocumentObject</v8:TypeSet></Source><Event>BeforeWrite</Event>"
            "<Handler>CommonModule.ОбщМод.ОбщаяПроцедура</Handler>",
            "edt": "<source><types>DocumentObject</types></source><event>BeforeWrite</event>"
            "<handler>CommonModule.ОбщМод.ОбщаяПроцедура</handler>",
        },
    ),
    ("Roles", "Роль1", {"Ext/Rights.xml": _RIGHTS_FLAGGED}, {}),
]
# Расширение `Расш1`: собственный справочник `Расш1_Спр` (модуль объекта), заимствованный `Док`
# (модуль объекта с перехватом обработки проведения), подписка на набор `CatalogObject` и роль с флагом
# «для новых объектов» (сессия на расширении видит только их).
_WORLD_EXT = [
    ("Catalogs", "Расш1_Спр", {"Ext/ObjectModule.bsl": PROC}, {}),
    (
        "Documents",
        "Док",
        {"Ext/ObjectModule.bsl": _EXT_DOC_MODULE},
        {"cf_props": "<ObjectBelonging>Adopted</ObjectBelonging>", "edt": "<objectBelonging>Adopted</objectBelonging>"},
    ),
    (
        "EventSubscriptions",
        "Расш1_ПодпНабор",
        {},
        {
            "cf_props": "<Source><v8:TypeSet>cfg:CatalogObject</v8:TypeSet></Source><Event>BeforeWrite</Event>"
            "<Handler>CommonModule.Расш1_Мод.П</Handler>",
            "edt": "<source><types>CatalogObject</types></source><event>BeforeWrite</event>"
            "<handler>CommonModule.Расш1_Мод.П</handler>",
        },
    ),
    ("Roles", "Расш1_Роль", {"Ext/Rights.xml": _RIGHTS_FLAGGED}, {}),
]


class World(dict):
    """Словарь хелперов сессии плюс раскладка: ``fmt``, ``route``, ``main_root``, ``ext_root``, ``ext_path``
    (путь расширения `Расш1` для ``find_ext_overrides``), ``base`` (корень сессии)."""

    fmt: str
    route: str
    main_root: Path
    ext_root: Path
    base: Path
    reader: IndexReader | None

    @property
    def ext_path(self) -> str:
        return str(self.ext_root)

    @property
    def is_ext_session(self) -> bool:
        return self.route.startswith("ext")


# Варианты мира: формат × маршрут. `nometa` — индекс, собранный с `build_metadata=False`; `ext_session` —
# хелперы, открытые на самом `cfe/Расш1` (роль `extension`), корень основной не передан.
WORLD_MAIN = ["cf-index", "cf-nometa", "cf-live", "edt-index", "edt-nometa", "edt-live"]
WORLD_EXT = ["cf-ext_session", "edt-ext_session"]


def _build_world(base: Path, variant: str) -> World:
    fmt, route = variant.split("-", 1)
    if route == "ext_alone":
        # Только `cfe/Расш1`: основной конфигурации рядом нет (Задача 4).
        ext_root = _objects_tree(
            base / "cfe" / "Расш1", fmt, _WORLD_EXT, CF_EXT_DESCRIPTOR if fmt == "cf" else EDT_EXT_DESCRIPTOR
        )
        world = World(
            _bsl_for(
                ext_root,
                None,
                current_config_role="extension",
                current_config_name="Расш1",
                current_config_root=str(ext_root),
            )
        )
        world.fmt, world.route = fmt, route
        world.main_root, world.ext_root, world.base, world.reader = base / "cf", ext_root, ext_root, None
        return world
    cf, exts = _ext_tree(base, fmt, _WORLD_MAIN, {"Расш1": _WORLD_EXT})
    ext_root = exts["Расш1"]
    reader = None
    if route in ("index", "nometa"):
        mp = pytest.MonkeyPatch()
        mp.setenv("RLM_INDEX_DIR", str(base / f"idx_{route}"))
        try:
            reader = IndexReader(IndexBuilder().build(str(cf), build_metadata=(route == "index")))
        finally:
            mp.undo()
    if route in ("ext_session", "ext_with_main"):
        # `ext_with_main` — как у сервера (Задача 4): корень основной рядом передан в сессию на расширении.
        main_kw = {"main_config_root": str(cf)} if route == "ext_with_main" else {}
        helpers = _bsl_for(
            ext_root,
            None,
            current_config_role="extension",
            current_config_name="Расш1",
            current_config_root=str(ext_root),
            **main_kw,
        )
        session_base = ext_root
    else:
        helpers = _bsl_for(
            cf,
            reader,
            idx_zero_callers_authoritative=reader is not None,
            extension_paths=[str(ext_root)],
            current_config_role="main",
            current_config_name="Тест",
            current_config_root=str(cf),
            extension_name_by_root={str(ext_root): "Расш1"},
        )
        session_base = cf
    world = World(helpers)
    world.fmt, world.route = fmt, route
    world.main_root, world.ext_root, world.base, world.reader = cf, ext_root, session_base, reader
    return world


@pytest.fixture(scope="module", params=WORLD_MAIN)
def world(request, tmp_path_factory):
    built = _build_world(tmp_path_factory.mktemp("world"), request.param)
    yield built
    if built.reader is not None:
        built.reader.close()


_FCM_PREFIXES = ["др_"]

# Как передать объект хелперу: первым аргументом либо `object_name=`/`owner=`; у `find_ext_overrides` — путь
# расширения `Расш1` и `object_name=`; у `find_data_path` — конец `Справочник.Спр`.
ARG_CALLS: dict[str, Callable[[dict, object], object]] = {
    **{
        helper: (lambda h: lambda bsl, value: bsl[h](value))(helper)
        for helper in (
            "get_object_full_structure",
            "get_object_modules",
            "get_object_profile",
            "analyze_object",
            "parse_form",
            "find_print_forms",
            "find_event_subscriptions",
            "find_based_on_documents",
            "find_register_movements",
            "analyze_document_flow",
            "find_references_to_object",
            "find_code_usages",
            "find_functional_options",
            "find_roles",
            "get_overrides",
            "find_enum_values",
            "find_register_writers",
            "find_exchange_plan_content",
            "find_defined_types",
            "extract_procedures",
            "get_module_outline",
        )
    },
    "find_custom_modifications": lambda bsl, value: bsl["find_custom_modifications"](
        value, custom_prefixes=_FCM_PREFIXES
    ),
    "find_templates": lambda bsl, value: bsl["find_templates"](owner=value),
    "find_attributes": lambda bsl, value: bsl["find_attributes"](object_name=value),
    "find_predefined": lambda bsl, value: bsl["find_predefined"](object_name=value),
    "find_ext_overrides": lambda bsl, value: bsl["find_ext_overrides"](bsl.ext_path, object_name=value),
    "find_data_path": lambda bsl, value: bsl["find_data_path"](value, "Справочник.Спр"),
}


def _call(bsl, helper: str, value):
    """Исход вызова хелпера с объектом ``value``: ответ либо ``('raises', тип, текст)``."""
    try:
        return ARG_CALLS[helper](bsl, value)
    except Exception as exc:  # noqa: BLE001 — исход сравнивается целиком
        return ("raises", type(exc), str(exc))


def _dump(obj) -> str:
    return json.dumps(_strip_times(obj), ensure_ascii=False, sort_keys=True, default=str)


@pytest.mark.parametrize("world", WORLD_MAIN + WORLD_EXT, indirect=True)
def test_world_builds_both_layouts(world):
    paths = [m["path"] for m in world["find_module"]("Док")]
    own = _obj_file(world.fmt, "Documents", "Док", "Ext/ObjectModule.bsl")
    assert own in paths, paths
    ext = world["detect_extensions"]()
    if world.is_ext_session:
        assert ext["config_role"] == "extension" and ext["config_name"] == "Расш1", ext
    else:
        assert [e["name"] for e in ext["nearby_extensions"]] == ["Расш1"], ext
        assert any(p.startswith("../cfe/Расш1/") for p in paths), paths  # модуль заимствованного `Док`


# ═══════════════ Задача 1 (Д1) — множественное число категории с точкой — ссылка ═══════════════

DOC_FORMS = ["Documents.Док", "Документы.Док", "documents.Док", "ДОКУМЕНТЫ.Док", "Doc uments.Док"]
ECHO = {"object_filter", "kinds_requested"}  # поля-эхо аргумента сравнением не судятся
DOC_ARG_HELPERS = [
    "get_object_full_structure",
    "get_object_modules",
    "get_object_profile",
    "analyze_object",
    "parse_form",
    "find_print_forms",
    "find_event_subscriptions",
    "find_based_on_documents",
    "find_register_movements",
    "analyze_document_flow",
    "find_references_to_object",
    "find_code_usages",
    "find_functional_options",
    "find_roles",
    "find_custom_modifications",
    "get_overrides",
    "find_templates",
    "find_attributes",
]
# Канонический ответ этих хелперов на `Документ.Док` непуст в каждом варианте мира — сравнение не вакуумно.
_NONEMPTY_CANON = {
    "parse_form": lambda res: len(res) > 0,
    "get_object_modules": lambda res: len(res.get("modules", [])) > 0,
    "find_event_subscriptions": lambda res: len(res) > 0,
    "find_register_movements": lambda res: len(res.get("code_registers", [])) > 0,
    "find_based_on_documents": lambda res: len(res.get("can_be_created_from", [])) > 0,
}


def _answer(bsl, helper: str, value, canonical: str):
    """Исход вызова без полей времени и эха; в тексте исключения аргумент заменён меткой — исход формы и
    канонической ссылки сравнимы и на отказе."""
    out = _call(bsl, helper, value)
    if isinstance(out, tuple) and out and out[0] == "raises":
        text = out[2].replace(repr(value.strip()), "<ARG>").replace(value.strip(), "<ARG>")
        return ("raises", out[1], text)
    return _without(_strip_times(out), ECHO)


def _assert_same_answer(bsl, helper: str, form: str, canonical: str):
    canon = _answer(bsl, helper, canonical, canonical)
    assert _answer(bsl, helper, form, form) == canon


class TestPluralDotPrefix:
    @pytest.mark.parametrize("form", DOC_FORMS)
    @pytest.mark.parametrize("helper", DOC_ARG_HELPERS)
    def test_plural_dot_doc_answers_as_singular(self, world, helper, form):
        if helper in _NONEMPTY_CANON:
            assert _NONEMPTY_CANON[helper](ARG_CALLS[helper](world, "Документ.Док"))
        _assert_same_answer(world, helper, form, "Документ.Док")

    @pytest.mark.parametrize("form", ["Catalogs.Спр", "Справочники.Спр", "catalogs.Спр"])
    @pytest.mark.parametrize("helper", DOC_ARG_HELPERS)
    def test_plural_dot_catalog_answers_as_singular(self, world, helper, form):
        _assert_same_answer(world, helper, form, "Справочник.Спр")

    @pytest.mark.parametrize("form", ["CommonModules.ОбщМод", "ОбщиеМодули.ОбщМод", "commonmodules.ОбщМод"])
    @pytest.mark.parametrize("helper", ["get_object_modules", "extract_procedures"])
    def test_common_module_folder_dot_answers_as_kind_name(self, world, helper, form):
        _assert_same_answer(world, helper, form, "ОбщийМодуль.ОбщМод")
        if helper == "extract_procedures":
            assert ARG_CALLS[helper](world, form)  # процедуры модуля, а не FileNotFoundError

    @pytest.mark.parametrize("form", ["AccumulationRegisters.Рег", "РегистрыНакопления.Рег"])
    def test_register_plural_dot(self, world, form):
        assert world["find_register_writers"]("РегистрНакопления.Рег")["writers"]
        _assert_same_answer(world, "find_register_writers", form, "РегистрНакопления.Рег")

    @pytest.mark.parametrize("form", ["Documents.Док", "Документы.Док"])
    def test_data_path_plural_dot(self, world, form):
        _assert_same_answer(world, "find_data_path", form, "Документ.Док")

    @pytest.mark.parametrize(
        "helper,raw", [("get_object_modules", "Документы."), ("find_event_subscriptions", "Documents.")]
    )
    def test_plural_dot_without_name_is_refused(self, world, helper, raw):
        with pytest.raises(ValueError, match=f"^{helper}: аргумент без имени объекта"):
            world[helper](raw)

    @pytest.mark.parametrize("raw", ["Documents.Док.ТабличнаяЧасть", "Метаданные.Документы.Док"])
    def test_member_reference_keeps_previous_parse(self, world, raw):
        """Обе версии: ссылка на член объекта и путь `Метаданные.…` ссылкой не являются — прежний разбор."""
        assert "error" in world["get_object_modules"](raw)
        assert world["parse_form"](raw) == []
        with pytest.raises(FileNotFoundError):
            world["extract_procedures"](raw)

    def test_singular_headless_kind_is_not_a_ref(self, world):
        """Обе версии: английское единственное число вида без ссылочного типа (`Subsystem.X`) — не ссылка:
        `find_references_to_object` ищет каноническую `Subsystem.Верх` как есть, структура — прежний «не найден»."""
        refs = world["find_references_to_object"]("Subsystem.Верх")
        assert refs["object"] == "Subsystem.Верх"
        assert world["get_object_full_structure"]("Subsystem.Верх") == world["get_object_full_structure"](
            "НетТакогоSubsystemВерх"
        ) | {"error": "Объект 'Subsystem.Верх' не найден"}

    @pytest.mark.parametrize(
        "dotted,path",
        [("Subsystems.Верх", "Subsystems/Верх"), ("CommonModules.ОбщМод", "CommonModules/ОбщМод")],
    )
    @pytest.mark.parametrize(
        "helper",
        [
            "get_object_full_structure",
            "get_object_modules",
            "get_object_profile",
            "analyze_object",
            "find_references_to_object",
        ],
    )
    def test_folder_name_dot_equals_path(self, world, helper, dotted, path):
        if helper == "find_references_to_object" and dotted.startswith("Subsystems"):
            # У подсистемы головы ссылки нет (`_typed_ref_of` отдаёт обе формы как есть), и нормализатор хелпера
            # разбирает точку и путь по-разному — давнее расхождение формы пути, ответ на точку релиз не меняет.
            pytest.skip("у категории без головы ссылки ответ хелпера по пути — прежний")
        canon = _answer(world, helper, path, path)
        assert _answer(world, helper, dotted, dotted) == canon


# ═══════════════ Задача 2 (Д2) — голое имя категории — отказ ═══════════════

CATEGORY_WORDS = [
    "Documents",
    "Документы",
    "документы",
    "Документ",
    "Перечисление",
    "Справочники",
    "Catalogs",
    "ОбщиеМодули",
    " Documents ",
]
OBJECT_GUARD_HELPERS = [
    "get_object_full_structure",
    "get_object_modules",
    "get_object_profile",
    "analyze_object",
    "find_print_forms",
    "find_based_on_documents",
    "find_register_movements",
    "analyze_document_flow",
    "find_references_to_object",
    "find_code_usages",
    "find_roles",
    "find_custom_modifications",
    "find_enum_values",
    "find_register_writers",
    "find_exchange_plan_content",
    "find_defined_types",
    "find_event_subscriptions",
    "find_functional_options",
]


def _unguarded(bsl: World) -> World:
    """Те же хелперы без стража публичной границы (функции замыкания): их ответ на вход, который страж
    пропускает, и есть ответ хелпера."""
    raw = World({name: getattr(fn, "__wrapped__", fn) for name, fn in bsl.items()})
    raw.__dict__.update(bsl.__dict__)
    return raw


def _same_as_unguarded(bsl: World, helper: str, value) -> None:
    got = _call(bsl, helper, value)
    assert not (isinstance(got, tuple) and got and got[0] == "raises" and "имя категории" in got[2]), got
    assert _dump(got) == _dump(_call(_unguarded(bsl), helper, value))


_LIKE_CATEGORY = [
    ("Reports", "Задачи", {"Ext/ObjectModule.bsl": PROC}, {}),
    ("Documents", "Задача", {"Ext/ObjectModule.bsl": PROC}, {}),
    ("CommandGroups", "Отчеты", {}, {}),
    (
        "EventSubscriptions",
        "ПодпЗадача",
        {},
        {
            "cf_props": "<Source><v8:Type>cfg:DocumentObject.Задача</v8:Type></Source><Event>BeforeWrite</Event>"
            "<Handler>CommonModule.ОбщМод.П</Handler>",
            "edt": "<source><types>DocumentObject.Задача</types></source><event>BeforeWrite</event>"
            "<handler>CommonModule.ОбщМод.П</handler>",
        },
    ),
]


@pytest.fixture(scope="module", params=["cf-index", "cf-live", "edt-index", "edt-live"])
def like_category(request, tmp_path_factory):
    """Объекты с именами-словами категорий: отчёт `Задачи` (с модулем), документ `Задача`, группа команд `Отчеты`
    (только описатель) и подписка на `Задача`."""
    fmt, route = request.param.split("-")
    base = tmp_path_factory.mktemp("like_category")
    root = _objects_tree(base / "cf", fmt, _LIKE_CATEGORY)
    reader = None
    if route == "index":
        mp = pytest.MonkeyPatch()
        mp.setenv("RLM_INDEX_DIR", str(base / "idx"))
        try:
            reader = IndexReader(IndexBuilder().build(str(root)))
        finally:
            mp.undo()
    yield _bsl_for(root, reader, current_config_role="main", current_config_name="Тест", current_config_root=str(root))
    if reader is not None:
        reader.close()


class TestBareCategoryWord:
    @pytest.mark.parametrize("word", CATEGORY_WORDS)
    @pytest.mark.parametrize("helper", OBJECT_GUARD_HELPERS)
    def test_bare_category_word_is_refused(self, world, helper, word):
        with pytest.raises(ValueError, match=f"^{helper}: '{word}' — имя категории"):
            _call_raising(world, helper, word)

    @pytest.mark.parametrize("world", WORLD_EXT, indirect=True)
    @pytest.mark.parametrize("word", ["Documents", "Перечисление", "Справочники"])
    @pytest.mark.parametrize("helper", OBJECT_GUARD_HELPERS)
    def test_extension_session_answers_as_before(self, world, helper, word):
        """Сессия на расширении без корня основной: существование объектов основной не проверить — ответ прежний
        (тот же, что у функции без стража)."""
        _same_as_unguarded(world, helper, word)

    def test_enum_list_refuses_category_word_per_element(self, world):
        res = world["find_enum_values"](["Documents", "ПерБезМодуля"])
        assert "имя категории" in res["Documents"]["error"], res
        assert res["ПерБезМодуля"] == world["find_enum_values"]("ПерБезМодуля")

    def test_object_named_like_category_is_answered(self, like_category):
        bsl = like_category
        assert bsl["get_object_modules"]("Задачи")["category"] == "Reports"
        assert bsl["get_object_full_structure"]("Задача")["category"] == "Documents"
        assert [s["name"] for s in bsl["find_event_subscriptions"]("Задача")] == ["ПодпЗадача"]
        assert isinstance(bsl["get_object_modules"]("Отчеты"), dict)  # отказа «имя категории» нет
        res = bsl["find_enum_values"](["Отчеты"])
        assert "имя категории" not in str(res["Отчеты"]), res

    def test_internal_callers_unaffected(self, world):
        """Обе версии: внутренние потребители (search, поиск реквизитов по имени) на слове категории не падают."""
        assert isinstance(world["search"]("Documents"), list)
        assert isinstance(world["find_attributes"](name="Documents"), list)


def _call_raising(bsl, helper: str, value):
    return ARG_CALLS[helper](bsl, value)


# ═══════════════ Задача 3 (Д3, П2) — шлюз существования объекта ═══════════════

GATE_HELPERS = [
    "find_print_forms",
    "find_event_subscriptions",
    "find_based_on_documents",
    "find_register_movements",
    "analyze_document_flow",
    "find_references_to_object",
    "find_code_usages",
    "find_roles",
    "find_functional_options",
    "find_register_writers",
    "find_exchange_plan_content",
    "find_defined_types",
    "get_overrides",
    "find_ext_overrides",
    "find_attributes",
    "find_predefined",
    "find_templates",
    "parse_form",
]
# Ссылки на несуществующий объект — в категориях хелпера.
_MISSING_REFS = {
    "find_register_writers": ["РегистрНакопления.Нет", "AccumulationRegisters/Нет"],
    "find_exchange_plan_content": ["ПланОбмена.Нет", "ExchangePlans/Нет"],
    "find_defined_types": ["ОпределяемыйТип.Нет", "DefinedTypes/Нет"],
}
_MISSING_DEFAULT = ["Документ.Нет", "Documents/Нет", "Справочник.Нет"]


def _missing_cases():
    for helper in GATE_HELPERS:
        for ref in _MISSING_REFS.get(helper, _MISSING_DEFAULT):
            yield helper, ref


class TestMissingObject:
    @pytest.mark.parametrize("helper,ref", list(_missing_cases()))
    def test_ref_to_missing_object_is_refused(self, world, helper, ref):
        with pytest.raises(ValueError, match=f"^{helper}: объекта .* нет — ни в основной конфигурации"):
            _call_raising(world, helper, ref)

    @pytest.mark.parametrize("ref", ["Документ.Док", "документ.док", "Справочник.Пустой", "Справочник.Расш1_Спр"])
    @pytest.mark.parametrize("helper", GATE_HELPERS)
    def test_ref_to_existing_object_passes(self, world, helper, ref):
        """Обе версии: ссылка на существующий объект (в том числе без модулей и только в расширении) — без отказа,
        ответ — тот же, что у функции без стража."""
        got = _call(world, helper, ref)
        assert not (isinstance(got, tuple) and "объекта" in got[2] and "нет —" in got[2]), got
        assert _dump(got) == _dump(_call(_unguarded(world), helper, ref))

    def test_nested_subsystem_ref_passes(self, world):
        """Обе версии: вложенная подсистема путём существует (описатели подсистем — рекурсивно)."""
        res = world["find_references_to_object"]("Subsystems/Вложенная")
        assert res == _unguarded(world)["find_references_to_object"]("Subsystems/Вложенная")
        assert "error" not in world["find_data_path"]("Подсистема.Вложенная", "Справочник.Спр")

    def test_data_path_missing_endpoint_is_structural_error(self, world):
        res = world["find_data_path"]("Подсистема.Нет", "Справочник.Спр", max_depth=99)
        assert res["found"] is False and res["path"] is None and res["depth"] == 0 and res["partial"] is False
        assert res["from"] == "Subsystem.Нет" and "нет —" in res["error"], res
        assert "search_objects('Нет')" in res["hint"], res
        assert res["_meta"]["max_depth"] == 8  # ограничение глубины — до проверки концов
        assert "error" in world["find_data_path"]("Документ.Док", "Справочник.Нет")

    def test_outline_missing_name_raises_like_extract(self, world):
        for name in ("НетТакогоОбъекта", "Документ.Нет"):
            with pytest.raises(FileNotFoundError) as extract_exc:
                world["extract_procedures"](name)
            with pytest.raises(FileNotFoundError) as outline_exc:
                world["get_module_outline"](name)
            assert str(outline_exc.value) == str(extract_exc.value)

    def test_filter_helpers_refuse_category_word_and_missing_ref(self, world):
        for helper in (
            "parse_form",
            "find_attributes",
            "find_predefined",
            "find_templates",
            "get_overrides",
            "find_ext_overrides",
        ):
            for value in ("Documents", "Справочник.Нет"):
                with pytest.raises(ValueError, match=f"^{helper}:"):
                    _call_raising(world, helper, value)

    @pytest.mark.parametrize(
        "helper", ["find_attributes", "find_predefined", "find_templates", "get_overrides", "find_ext_overrides"]
    )
    def test_filter_helpers_empty_value_unchanged(self, world, helper):
        """Обе версии: пустое значение фильтра — «без фильтра», ответ прежний."""
        assert _dump(_call(world, helper, "")) == _dump(_call(_unguarded(world), helper, ""))

    @pytest.mark.parametrize("world", WORLD_EXT, indirect=True)
    def test_extension_session_has_no_gate(self, world):
        """Сессия на расширении без корня основной: ни шлюза, ни правил не-ссылки — ответ прежний."""
        for helper, value in (
            ("find_code_usages", "Справочник.Спр"),
            ("find_code_usages", "Справочник.Нет"),
            ("find_event_subscriptions", "Нет"),
            ("find_roles", "Нет"),
            ("find_references_to_object", "Documents"),
        ):
            _same_as_unguarded(world, helper, value)

    def test_member_reference_keeps_flag_roles(self, world):
        """Обе версии: ссылка на член `Document.X.Attribute.Z` судится по объекту `X` — роли по флагу прежние."""
        res = world["find_roles"]("Document.Док.Attribute.Реквизит")
        assert [r["role_name"] for r in res["roles"] if r["granted_via"] == "set_for_new_objects"] == ["Роль1"]

    def test_bare_missing_name_has_no_set_rows(self, world):
        for value in ("Нет", "До"):
            rows = world["find_event_subscriptions"](value)
            assert not [r for r in rows if r.get("scope") == "set"], rows
        assert [r["name"] for r in world["find_event_subscriptions"]("Док") if r.get("scope") == "set"] == ["ПодпНабор"]
        flow = world["analyze_document_flow"]("Нет")
        assert not [r for r in flow["event_subscriptions"] if r.get("scope") == "set"], flow["event_subscriptions"]

    def test_bare_missing_name_has_no_flag_roles(self, world):
        assert world["find_roles"]("Нет")["roles"] == []
        assert [r["role_name"] for r in world["find_roles"]("Док")["roles"]] == ["Роль1"]

    def test_overrides_missing_bare_name_has_hint(self, world):
        for res in (world["get_overrides"]("Нет"), world["find_ext_overrides"](world.ext_path, "Нет")):
            assert res["total"] == 0 and "search_objects('Нет')" in res["hint"], res
        for res in (world["get_overrides"]("Пустой"), world["find_ext_overrides"](world.ext_path, "Пустой")):
            assert res["total"] == 0 and "hint" not in res, res

    # Формы существующих объектов, которые разбор ссылкой не считает (ревью 1.46.0): голова вида без ссылочного типа
    # (`Role.X`, `Subsystem.X` — их отдаёт сам инструмент, `find_data_path(...)['from']`), русское единственное число,
    # каталог категории с точкой, путь категории без модулей и путь описателя, гибрид, путь объекта расширения.
    _EXISTING_FORMS = [
        "Subsystem.Верх",
        "Role.Роль1",
        "Подсистема.Верх",
        "Роль.Роль1",
        "CommonPictures.Картинка",
        "ОбщаяКартинка.Картинка",
        "CommonPictures/Картинка",
        "CommonPictures/Картинка.xml",
        "EventSubscriptions/ПодпНабор",
        "EventSubscription.ПодпНабор",
        "Документ.Catalogs/Спр",
        "Документ.Documents/Док",
        "../cfe/Расш1/Catalogs/Расш1_Спр.xml",
        # повторный разделитель и `.` — путь, который разрешает и `parse_object_xml` (ревью, круг 2)
        "CommonPictures//Картинка",
        "CommonPictures/./Картинка",
        "CommonPictures///Картинка.xml",
        # хвост после первой точки называет существующий объект — любое толкование строки (объявленный дизайн)
        "НетТакого.Спр",
    ]

    @pytest.mark.parametrize("form", _EXISTING_FORMS)
    def test_existing_object_in_any_form_keeps_flag_roles(self, world, form):
        """Обе версии: форме соответствует существующий объект — роли по флагу и перехваты без подсказки «объекта
        нет»: правило не-ссылки — только для имени, которому не соответствует ни один объект."""
        roles = world["find_roles"](form)["roles"]
        assert [r["role_name"] for r in roles if r["granted_via"] == "set_for_new_objects"] == ["Роль1"], roles
        for res in (world["get_overrides"](form), world["find_ext_overrides"](world.ext_path, form)):
            assert "hint" not in res, res

    @pytest.mark.parametrize(
        "form",
        [
            "CommonPictures/Картинка",
            "CommonPictures.Картинка",
            "EventSubscriptions/ПодпНабор",
            "Документ.Catalogs/Спр",
            "Документ.Documents/Док",
        ],
    )
    def test_existing_object_in_any_form_keeps_set_rows(self, world, form):
        """Обе версии: строки подписок на наборы у формы, которой соответствует существующий объект, — прежние."""
        rows = world["find_event_subscriptions"](form)
        assert [r["name"] for r in rows if r.get("scope") == "set"] == ["ПодпНабор"], rows

    @pytest.mark.parametrize("route", ["index", "live"])
    @pytest.mark.parametrize("fmt", ["cf", "edt"])
    def test_extension_named_like_a_category_keeps_its_object(self, tmp_path, monkeypatch, fmt, route):
        """Обе версии: имя расширения совпало с каталогом категории (`cfe/Reports`) — путь модуля, который отдаёт сам
        хелпер, и путь описателя объекта этого расширения судятся по объекту (`Catalogs/Доп_Спр`), а не по паре
        `Reports/Catalogs`: ответ прежний (ревью, круг 2)."""
        monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
        cf, exts = _ext_tree(
            tmp_path, fmt, _WORLD_MAIN, {"Reports": [("Catalogs", "Доп_Спр", {"Ext/ObjectModule.bsl": PROC}, {})]}
        )
        ext_root = exts["Reports"]
        reader = _reader_for(cf, monkeypatch) if route == "index" else None
        try:
            bsl = _bsl_for(
                cf,
                reader,
                extension_paths=[str(ext_root)],
                current_config_role="main",
                current_config_name="Тест",
                current_config_root=str(cf),
                extension_name_by_root={str(ext_root): "Reports"},
            )
            module_path = bsl["get_object_modules"]("Catalog.Доп_Спр")["modules"][0]["path"]
            descriptor = "Доп_Спр.xml" if fmt == "cf" else "Доп_Спр/Доп_Спр.mdo"
            for form in (module_path, f"../cfe/Reports/Catalogs/{descriptor}"):
                roles = bsl["find_roles"](form)["roles"]
                assert [r["role_name"] for r in roles if r["granted_via"] == "set_for_new_objects"] == ["Роль1"], form
                rows = bsl["find_event_subscriptions"](form)
                assert [r["name"] for r in rows if r.get("scope") == "set"] == ["ПодпНабор"], (form, rows)
                for res in (bsl["get_overrides"](form), bsl["find_ext_overrides"](str(ext_root), form)):
                    assert "hint" not in res, (form, res)
        finally:
            if reader is not None:
                reader.close()

    @pytest.mark.parametrize(
        "form",
        [
            "Role.НетТакой",
            "CommonPictures/НетТакой",
            "ОбщаяКартинка.НетТакой",
            "CommonPictures//НетТакой",
            "Documents/ /",
        ],
    )
    def test_missing_object_in_any_form_gets_the_rule(self, world, form):
        """Формы без объекта ни при одном толковании — правило не-ссылки действует: без ролей по флагу, с `hint`."""
        assert world["find_roles"](form)["roles"] == []
        assert "search_objects(" in world["get_overrides"](form)["hint"]

    def test_composites_unchanged(self, world):
        """Обе версии: композиты на существующей ссылке отвечают как без стража."""
        assert _dump(world["analyze_document_flow"]("Документ.Док")) == _dump(
            _unguarded(world)["analyze_document_flow"]("Документ.Док")
        )
        assert _dump(world["get_object_profile"]("Документ.Док", include_flow=True)) == _dump(
            _unguarded(world)["get_object_profile"]("Документ.Док", include_flow=True)
        )


# ═══════════════ Задача 4 (П11) — корень основной конфигурации в сессии на расширении ═══════════════

WORLD_EXT_MAIN = ["cf-ext_with_main", "edt-ext_with_main"]
WORLD_EXT_ALONE = ["cf-ext_alone", "edt-ext_alone"]


class TestMainRootInExtensionSession:
    @pytest.mark.parametrize("world", WORLD_EXT_MAIN, indirect=True)
    def test_extension_session_gate_uses_main_root(self, world, tmp_path):
        plain = _build_world(tmp_path, f"{world.fmt}-ext_session")  # та же сессия без корня основной
        # Объект основной, расширением не заимствованный, — существует: отказа нет, ответ прежний.
        assert _dump(world["find_code_usages"]("Справочник.Спр")) == _dump(plain["find_code_usages"]("Справочник.Спр"))
        with pytest.raises(ValueError, match="ни в этом расширении, ни в основной"):
            world["find_code_usages"]("Справочник.Нет")
        with pytest.raises(ValueError, match="имя категории"):
            world["find_references_to_object"]("Documents")
        assert plain["find_event_subscriptions"]("Нет"), "фикстура: в сессии без корня основной строка набора есть"
        assert not [r for r in world["find_event_subscriptions"]("Нет") if r.get("scope") == "set"]
        # `Спр` есть в основной — правило не-ссылки не срабатывает, роли по флагу прежние.
        assert world["find_roles"]("Спр") == plain["find_roles"]("Спр")
        assert [r["role_name"] for r in plain["find_roles"]("Спр")["roles"]] == ["Расш1_Роль"]
        assert world["find_roles"]("Нет")["roles"] == []

    @pytest.mark.parametrize("world", WORLD_EXT_ALONE, indirect=True)
    def test_extension_without_main_has_no_gate(self, world):
        """Обе версии: основной рядом нет — ни шлюза, ни правил не-ссылки, ответ прежний."""
        for helper, value in (
            ("find_code_usages", "Справочник.Спр"),
            ("find_code_usages", "Справочник.Нет"),
            ("find_references_to_object", "Documents"),
            ("find_event_subscriptions", "Нет"),
            ("find_roles", "Спр"),
            ("find_roles", "Нет"),
        ):
            _same_as_unguarded(world, helper, value)
        assert world["find_event_subscriptions"]("Нет"), "строка набора на месте"


@pytest.fixture(params=["inline", "process"])
def any_mode(request, monkeypatch):
    """Оба режима песочницы (копия фикстуры `tests/test_sandbox_process_server.py`)."""
    monkeypatch.setenv("RLM_SANDBOX_MODE", request.param)
    return request.param


def test_main_root_is_wired_end_to_end(any_mode, tmp_path):
    from rlm_tools_bsl.server import _rlm_end, _rlm_execute, _rlm_start

    root = tmp_path / "src"
    cf, exts = _ext_tree(root, "cf", _WORLD_MAIN, {"Расш1": _WORLD_EXT})

    def _session(path):
        resp = json.loads(_rlm_start(path=str(path), query="v1.46.0 main root"))
        assert "error" not in resp, resp
        return resp["session_id"]

    sid = _session(exts["Расш1"])
    try:
        res = json.loads(_rlm_execute(sid, "find_code_usages('Справочник.Нет')"))
        assert "ни в этом расширении, ни в основной" in (res["error"] or ""), res
        res = json.loads(_rlm_execute(sid, "print(find_code_usages('Справочник.Спр')['total'])"))
        assert res["error"] is None, res
    finally:
        _rlm_end(sid)
    sid = _session(cf)
    try:
        res = json.loads(_rlm_execute(sid, "find_code_usages('Справочник.Нет')"))
        assert "ни в основной конфигурации, ни в соседних расширениях" in (res["error"] or ""), res
    finally:
        _rlm_end(sid)


# ═══════════════ Задача 5 (Д4, П1) — неизвестное значение фильтра — отказ ═══════════════

_REF_KINDS = [
    "attribute_type",
    "subsystem_content",
    "exchange_plan_content",
    "functional_option_content",
    "event_subscription_source",
    "role_rights",
    "defined_type_content",
    "characteristic_type",
    "owner",
    "based_on",
    "choice_parameter_link",
    "link_by_type",
    "main_form",
    "list_form",
    "default_object_form",
    "default_list_form",
    "command_parameter_type",
    "predefined_characteristic_type",
]
_TEMPLATE_TYPES = [
    "SpreadsheetDocument",
    "TextDocument",
    "BinaryData",
    "ActiveDocument",
    "HTMLDocument",
    "GeographicalSchema",
    "GraphicalSchema",
    "DataCompositionSchema",
    "DataCompositionAppearanceTemplate",
    "AddIn",
]
_SUBSCRIPTION_EVENTS = [
    "BeforeWrite",
    "OnWrite",
    "BeforeDelete",
    "Posting",
    "UndoPosting",
    "Filling",
    "FillCheckProcessing",
    "OnCopy",
    "OnSetNewCode",
    "OnSetNewNumber",
    "ChoiceDataGetProcessing",
    "FormGetProcessing",
    "PresentationGetProcessing",
    "PresentationFieldsGetProcessing",
    "OnSendDataToMaster",
    "OnSendDataToSlave",
    "OnSendNodeDataToSlave",
    "OnReceiveDataFromMaster",
    "OnReceiveDataFromSlave",
    "BeforeExecute",
    "OnExecute",
]
# Флаг → колонка строки ответа (у значений ReturnValuesReuse колонки нет).
_CM_FLAGS = {
    "global": "global",
    "global_": "global",
    "server": "server",
    "server_call": "server_call",
    "servercall": "server_call",
    "privileged": "privileged",
    "external_connection": "external_connection",
    "externalconnection": "external_connection",
    "client_managed": "client_managed",
    "clientmanagedapplication": "client_managed",
    "client_ordinary": "client_ordinary",
    "clientordinaryapplication": "client_ordinary",
    "Privileged": "privileged",
    "DontUse": None,
    "DuringSession": None,
    "DuringRequest": None,
    "DONTUSE": None,
}
_UNKNOWN_FILTER_CASES = {
    "ref_kinds": (
        "find_references_to_object",
        "kinds",
        lambda b: b["find_references_to_object"]("Справочник.Спр", kinds=["bogus"]),
    ),
    "ref_kinds_partial": (
        "find_references_to_object",
        "kinds",
        lambda b: b["find_references_to_object"]("Справочник.Спр", kinds=["attribute_type", "bogus"]),
    ),
    "data_path_kinds": (
        "find_data_path",
        "kinds",
        lambda b: b["find_data_path"]("Документ.Док", "Справочник.Спр", kinds=["bogus"]),
    ),
    "code_kind": ("find_code_usages", "kind", lambda b: b["find_code_usages"]("Справочник.Спр", kind="bogus")),
    "regions_group_by": ("search_regions", "group_by", lambda b: b["search_regions"]("", group_by="bogus")),
    "profile_sections": (
        "get_object_profile",
        "sections",
        lambda b: b["get_object_profile"]("Документ.Док", sections=["bogus"]),
    ),
    "profile_sections_partial": (
        "get_object_profile",
        "sections",
        lambda b: b["get_object_profile"]("Документ.Док", sections=["structure", "bogus"]),
    ),
    "audit_reasons": ("find_unresolved_calls", "reasons", lambda b: b["find_unresolved_calls"](reasons=["bogus"])),
    "cm_flag": ("find_common_modules", "flag", lambda b: b["find_common_modules"](flag="bogus")),
    "template_type": ("find_templates", "template_type", lambda b: b["find_templates"](template_type="bogus")),
    "attr_kind": (
        "find_attributes",
        "kind",
        lambda b: b["find_attributes"](object_name="Документ.Док", kind="bogus"),
    ),
    "attr_kind_column": (
        "find_attributes",
        "kind",
        lambda b: b["find_attributes"](object_name="Документ.Док", kind="column"),
    ),
    "event_filter_ru": (
        "find_event_subscriptions",
        "event_filter",
        lambda b: b["find_event_subscriptions"]("Документ.Док", event_filter=["ПередЗаписью"]),
    ),
    "event_filter_str": (
        "find_event_subscriptions",
        "event_filter",
        lambda b: b["find_event_subscriptions"]("", event_filter="ПередЗаписью"),
    ),
}


class TestClosedFilterValues:
    @pytest.mark.parametrize("case", list(_UNKNOWN_FILTER_CASES))
    def test_unknown_filter_value_is_refused(self, world, case):
        helper, param, call = _UNKNOWN_FILTER_CASES[case]
        with pytest.raises(ValueError, match=f"^{helper}: {param} —") as exc:
            call(world)
        if param == "event_filter":
            assert "английское имя события" in str(exc.value) and "BeforeWrite" in str(exc.value)

    def test_code_usages_empty_kind_unchanged(self, world):
        """Обе версии: пустое значение `kind` любого вида (`''`, `[]`) — без фильтра, строки как у `kind=None`."""
        plain = world["find_code_usages"]("Справочник.Спр")["usages"]
        for kind in ("", []):
            assert world["find_code_usages"]("Справочник.Спр", kind=kind)["usages"] == plain, kind

    def test_known_ref_kinds_unchanged(self, world):
        """Обе версии: каждый известный вид — та же выборка, что вид в полном ответе; без предупреждения."""
        full = world["find_references_to_object"]("Документ.Док")
        for kind in _REF_KINDS:
            res = world["find_references_to_object"]("Документ.Док", kinds=[kind])
            assert "arg_warning" not in res["_meta"], (kind, res["_meta"])
            if res["_meta"]["source"] == "index":
                assert res["total"] == full["by_kind"].get(kind, 0), (kind, res)

    def test_known_data_path_kinds_unchanged(self, world):
        """Обе версии: известный вид не отказывает и находит то же ребро, что без фильтра."""
        plain = world["find_data_path"]("Документ.Док", "Справочник.Спр")
        res = world["find_data_path"]("Документ.Док", "Справочник.Спр", kinds=["attribute_type"])
        assert res["found"] == plain["found"] and "error" not in res

    def test_known_code_kinds_unchanged(self, world):
        full = world["find_code_usages"]("Справочник.Спр")
        for kind in ("manager", "ref_type", "query"):
            res = world["find_code_usages"]("Справочник.Спр", kind=kind)
            assert "arg_warning" not in res["_meta"]
            if res["_meta"].get("kind_filter_applied"):
                assert res["total"] == full["by_kind"].get(kind, 0)

    def test_code_usages_kind_case_folded(self, world):
        assert _dump(world["find_code_usages"]("Справочник.Спр", kind="Manager")) == _dump(
            world["find_code_usages"]("Справочник.Спр", kind="manager")
        )

    def test_known_group_by_and_sections_unchanged(self, world):
        """Обе версии: известное значение группировки и секции (псевдонимы, регистр) — без предупреждения."""
        for group_by in ("name", "category"):
            res = world["search_regions"]("", group_by=group_by)
            assert res["group_by"] == group_by and "arg_warning" not in res.get("_meta", {})
        aliases = (
            "structure",
            "attrs",
            "реквизиты",
            "modules",
            "код",
            "registers",
            "подписки",
            "roles",
            "fo",
            "flow",
            "code_usages",
            "Структура",
        )
        for alias in aliases:
            res = world["get_object_profile"]("Документ.Док", sections=[alias])
            assert "arg_warning" not in res["_meta"] and len(res["sections"]) == 1, (alias, res["_meta"])

    def test_known_reasons_unchanged(self, world):
        reasons = (
            "method_missing",
            "not_exported",
            "object_missing",
            "module_missing",
            "ambiguous_module",
            "target_only_in_extension",
            "target_in_other_extension",
            "unsupported_name_form",
            "dynamic_name",
            "receiver_unknown",
            " Method_Missing ",
        )
        for reason in reasons:
            res = world["find_unresolved_calls"](reasons=[reason])
            assert "arg_warning" not in res["_meta"], (reason, res["_meta"])

    def test_known_flags_and_template_types_unchanged(self, world):
        """Обе версии: известный флаг и тип макета (в том числе в другом регистре) — выборка как у фильтра по
        полному списку."""
        modules = world["find_common_modules"](limit=1000)["modules"]
        # Индекс без метаданных свойств общих модулей не хранит — там список пуст на обеих версиях.
        assert modules or world.route == "nometa", "фикстура: общие модули есть"
        for flag, column in _CM_FLAGS.items():
            res = world["find_common_modules"](flag=flag, limit=1000)
            if column:
                expected = [m for m in modules if m[column]]
            else:
                expected = [m for m in modules if (m["return_values_reuse"] or "").lower() == flag.lower()]
            assert res["modules"] == expected, (flag, res)
        templates = world["find_templates"](limit=1000)["templates"]
        for template_type in (*_TEMPLATE_TYPES, "spreadsheetdocument"):
            res = world["find_templates"](template_type=template_type, limit=1000)
            assert res["templates"] == [
                t for t in templates if (t["template_type"] or "").lower() == template_type.lower()
            ], template_type

    def test_known_attr_kinds_unchanged(self, world):
        rows = world["find_attributes"](object_name="Документ.Док")
        assert rows, "фикстура: реквизиты есть"
        for kind in ("attribute", "dimension", "resource", "ts_attribute", "Dimension", "TS_Attribute"):
            got = world["find_attributes"](object_name="Документ.Док", kind=kind)
            assert got == [r for r in rows if r["attr_kind"] == kind.lower()], kind

    def test_known_events_unchanged(self, world):
        rows = world["find_event_subscriptions"]("")
        assert rows, "фикстура: подписки есть"
        for event in (*_SUBSCRIPTION_EVENTS, "beforewrite", "Write", ""):
            got = world["find_event_subscriptions"]("", event_filter=[event])
            assert got == [r for r in rows if event.lower() in (r.get("event") or "").lower()], event

    def test_event_filter_platform_event_absent_in_config_is_empty(self, world):
        assert world["find_event_subscriptions"]("", event_filter=["UndoPosting"]) == []


# ═══════════════ Задача 6 (Д5) — порядок каталога модулей на индексе ═══════════════

_AMORT_DOCS = ("Акт", "Возврат", "Заказ", "Перемещение", "Счет")


def _amort_tree(root: Path) -> Path:
    """CF: несколько документов и омоним `АмортизацияОС` — документ (модули объекта и менеджера) и регистр
    накопления (модуль набора записей)."""
    objects = [
        ("Documents", name, {"Ext/ObjectModule.bsl": PROC, "Ext/ManagerModule.bsl": PROC}, {}) for name in _AMORT_DOCS
    ]
    objects += [
        (
            "Documents",
            "АмортизацияОС",
            {
                "Ext/ObjectModule.bsl": "Процедура ОбработкаПроведения(Отказ, Режим)\nКонецПроцедуры\n",
                "Ext/ManagerModule.bsl": PROC,
            },
            {"cf_props": "<Posting>Allow</Posting>"},
        ),
        (
            "AccumulationRegisters",
            "АмортизацияОС",
            {"Ext/RecordSetModule.bsl": "Процедура ПередЗаписью(Отказ, Замещение)\nКонецПроцедуры\n"},
            {},
        ),
    ]
    return _objects_tree(root, "cf", objects)


@pytest.fixture
def amort_tree(tmp_path):
    return _amort_tree(tmp_path / "cf")


def _touch_module(path: Path, extra: str) -> None:
    path.write_text(path.read_text(encoding="utf-8") + extra, encoding="utf-8")


class TestCatalogOrder:
    def test_catalog_is_path_ordered_and_update_equals_build(self, amort_tree, monkeypatch):
        root = amort_tree
        reader = _reader_for(root, monkeypatch)
        bsl = _bsl_for(root, reader)
        built = [r["path"] for r in bsl["find_by_type"]("Documents", limit=1000)]
        built_category = bsl["analyze_object"]("АмортизацияОС")["category"]
        reader.close()
        # Правка модулей первого по пути документа и обоих модулей омонима: `update` переписывает их строки.
        _touch_module(root / "Documents" / "Акт" / "Ext" / "ObjectModule.bsl", "// правка\n")
        _touch_module(root / "Documents" / "АмортизацияОС" / "Ext" / "ObjectModule.bsl", "// правка\n")
        _touch_module(root / "AccumulationRegisters" / "АмортизацияОС" / "Ext" / "RecordSetModule.bsl", "// правка\n")
        IndexBuilder().update(str(root))
        reader = IndexReader(_db_path(root))
        try:
            bsl = _bsl_for(root, reader)
            updated = [r["path"] for r in bsl["find_by_type"]("Documents", limit=1000)]
            assert updated == built == sorted(built), (built, updated)
            assert bsl["analyze_object"]("АмортизацияОС")["category"] == built_category
            assert [m["rel_path"] for m in reader.get_all_modules()] == sorted(
                m["rel_path"] for m in reader.get_all_modules()
            )
        finally:
            reader.close()


def _db_path(root: Path) -> str:
    from rlm_tools_bsl.bsl_index import get_index_db_path

    return str(get_index_db_path(str(root)))


# ═══════════════ Задача 7 (Д17 по П7, Д20 по П10) — о спрошенном объекте ═══════════════

_MODULELESS_ARGS = {
    "Роль1": ("Roles", "Роль1"),
    "Roles/Роль1": ("Roles", "Роль1"),
    "Верх": ("Subsystems", "Верх"),
    "Subsystems/Верх": ("Subsystems", "Верх"),
    "Вложенная": ("Subsystems", "Вложенная"),
    "Картинка": ("CommonPictures", "Картинка"),
    "ПерБезМодуля": ("Enums", "ПерБезМодуля"),
    "Перечисление.ПерБезМодуля": ("Enums", "ПерБезМодуля"),
    "Пустой": ("Catalogs", "Пустой"),
    "Справочник.Пустой": ("Catalogs", "Пустой"),
}


def _fcm(bsl, value):
    return bsl["find_custom_modifications"](value, custom_prefixes=_FCM_PREFIXES)


class TestAskedObject:
    @pytest.mark.parametrize("arg", list(_MODULELESS_ARGS))
    def test_moduleless_object_is_answered(self, world, arg):
        category, name = _MODULELESS_ARGS[arg]
        res = world["analyze_object"](arg)
        assert (res.get("category"), res.get("name"), res.get("modules")) == (category, name, []), res
        assert "hint" not in res and "error" not in res
        fcm = _fcm(world, arg)
        assert fcm.get("modules_analyzed") == 0 and fcm.get("modifications") == [], fcm
        assert fcm.get("object_name") == name and "hint" not in fcm and "error" not in fcm

    def test_nested_subsystem_metadata_is_read(self, world):
        assert world["analyze_object"]("Вложенная")["metadata"], "описатель вложенной подсистемы разобран"

    def test_fragment_answers_one_object_with_hint(self, world):
        res = world["analyze_object"]("ОбщМо")
        name = res["name"]
        assert name in ("ОбщМод", "ОбщМод2") and res["category"] == "CommonModules", res
        assert res["modules"] and all(m["path"].startswith(f"CommonModules/{name}/") for m in res["modules"])
        assert f"CommonModules/{name}" in res["hint"] and "search_objects('ОбщМо')" in res["hint"], res
        fcm = _fcm(world, "ОбщМо")
        assert fcm["modules_analyzed"] == 1 and f"CommonModules/{name}" in fcm["hint"], fcm
        if world.reader is not None:
            # Порядок каталога на индексе — порядок пути (Задача 6): первым идёт `ОбщМод`; совет исполним.
            assert name == "ОбщМод"
            assert "ОбщМод" in [r["object_name"] for r in world["search_objects"]("ОбщМо")]

    @pytest.mark.parametrize("route", ["live", "index"])
    def test_homonym_modules_of_one_object(self, amort_tree, monkeypatch, route):
        reader = _reader_for(amort_tree, monkeypatch) if route == "index" else None
        try:
            bsl = _bsl_for(amort_tree, reader)
            res = bsl["analyze_object"]("АмортизацияОС")
            category = res["category"]
            assert res["modules"] and all(m["path"].startswith(f"{category}/") for m in res["modules"]), res
            expected = len([m for m in bsl["find_module"]("АмортизацияОС") if m["category"] == category])
            assert len(res["modules"]) == expected
            assert _fcm(bsl, "АмортизацияОС")["modules_analyzed"] == expected
        finally:
            if reader is not None:
                reader.close()

    @pytest.mark.parametrize("world", WORLD_EXT_MAIN, indirect=True)
    def test_main_only_object_in_extension_session(self, world, tmp_path):
        for res in (world["analyze_object"]("Спр"), _fcm(world, "Спр"), world["get_object_modules"]("Спр")):
            assert "основной конфигурации" in res.get("error", "") and "в этом расширении его нет" in res["error"], res
        plain = _build_world(tmp_path, f"{world.fmt}-ext_session")
        assert _dump(world["analyze_object"]("Док")) == _dump(plain["analyze_object"]("Док"))

    def test_object_modules_names_moduleless_object(self, world):
        res = world["get_object_modules"]("Роль1")
        assert "нет модулей" in res["error"] and "parse_object_xml('Roles/Роль1" in res["error"], res
        route = res["error"].split("parse_object_xml(", 1)[1].split(")", 1)[0]
        parsed = world["parse_object_xml"](json.loads(route.replace("'", '"')))
        assert parsed.get("name") == "Роль1", parsed
        assert (
            world["get_object_modules"]("НетТакого")["error"] == "Объект 'НетТакого' не найден (нет модулей в индексе)"
        )

    def test_object_modules_exact_moduleless_beats_substring(self, world):
        res = world["get_object_modules"]("Картинка")
        assert "У объекта CommonPictures/Картинка нет модулей" in res.get("error", ""), res

    def test_custom_modifications_without_prefixes_keeps_previous_answer(self, world):
        """Обе версии: без обнаруженных префиксов и без custom_prefixes — прежний отказ (у любого объекта)."""
        for value in ("Док", "Роль1"):
            res = world["find_custom_modifications"](value)
            assert res == {"error": "Нетиповые префиксы не обнаружены. Укажите custom_prefixes вручную."}, res

    def test_objects_with_modules_unchanged(self, world):
        """Обе версии: объект с модулями — модули этого объекта, без подсказки."""
        own = sorted(m["path"] for m in world["find_module"]("Док") if m["object_name"] == "Док")
        for arg in ("Док", "Документ.Док"):
            res = world["analyze_object"](arg)
            assert (res["name"], res["category"]) == ("Док", "Documents") and "hint" not in res
            assert sorted(m["path"] for m in res["modules"]) == own
        fcm = _fcm(world, "Док")
        assert fcm["modules_analyzed"] == len(own) and "hint" not in fcm
        assert [p["name"] for m in fcm["modifications"] for p in m["custom_procedures"]] == ["др_Доработка"]
        gom = world["get_object_modules"]("Док")
        assert gom["category"] == "Documents" and sorted(m["path"] for m in gom["modules"]) == own

    @pytest.mark.parametrize("arg", ["  Док  ", " Пустой", "Роль1 "])
    def test_padded_bare_name_keeps_previous_answer(self, world, arg):
        """Обе версии: голое имя с пробелами по краям — не-ссылка (v1.44.0), ответ прежний «не найден». Карточка
        объекта ищется по имени как передано, как и точные модули: иначе объект с модулями (`Док`) получал ответ
        «объект без модулей» (`modules: []`)."""
        assert world["analyze_object"](arg) == {"error": f"Объект '{arg}' не найден"}
        assert _fcm(world, arg) == {"error": f"Объект '{arg}' не найден"}


# ═══════════════ Задача 8 (Д6) — find_data_path: усечение узла последнего уровня ═══════════════


def _dp_catalog(name: str, attr_types: list[str]) -> str:
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


def _dp_tree(root: Path) -> Path:
    """Копия `_dp_tree` из `tests/test_v1_45_0.py`: А → (Б, В); Б → Г; `Изолированный` — без ссылок."""
    spec = {"CommonModules/Т/Ext/Module.bsl": "Процедура Т() Экспорт\nКонецПроцедуры\n"}
    for name, refs in (
        ("А", ["CatalogRef.Б", "CatalogRef.В"]),
        ("Б", ["CatalogRef.Г"]),
        ("В", []),
        ("Г", []),
        ("Изолированный", []),
    ):
        spec[f"Catalogs/{name}.xml"] = _dp_catalog(name, refs)
        spec[f"Catalogs/{name}/Ext/Catalog.xml"] = _dp_catalog(name, refs)
    return _cf_doc_tree(root, spec)


class TestDataPathLastLevel:
    def test_last_level_truncation_is_conclusive(self, tmp_path, monkeypatch, page_one):
        root = _dp_tree(tmp_path / "cf")
        bsl = _bsl_for(root, _reader_for(root, monkeypatch))
        res = bsl["find_data_path"]("Справочник.А", "Справочник.Изолированный", max_depth=1)
        assert res["found"] is False and res["_meta"]["budget_exceeded"] is False, res
        assert "hint" not in res and "truncated_nodes" not in res["_meta"], res

    def test_deeper_truncation_stays_inconclusive(self, tmp_path, monkeypatch, page_one):
        """Обе версии: усечённый узел не последнего уровня — промах неокончательный."""
        root = _dp_tree(tmp_path / "cf")
        bsl = _bsl_for(root, _reader_for(root, monkeypatch))
        res = bsl["find_data_path"]("Справочник.А", "Справочник.Изолированный", max_depth=2)
        assert res["found"] is False and res["_meta"]["budget_exceeded"] is True and "hint" in res, res

    def test_direct_ref_beyond_page_still_found(self, tmp_path, monkeypatch, page_one):
        """Обе версии: прямая ссылка за страницей узла последнего уровня находится точечной проверкой."""
        root = _dp_tree(tmp_path / "cf")
        reader = _reader_for(root, monkeypatch)
        bsl = _bsl_for(root, reader)
        first = reader.find_metadata_refs_from("А", source_category="Catalogs", limit=1)[0]["ref_object"]
        other = "Catalog.В" if first == "Catalog.Б" else "Catalog.Б"
        res = bsl["find_data_path"]("Справочник.А", other.replace("Catalog.", "Справочник."), max_depth=1)
        assert res["found"] is True and res["path"][0]["to"] == other, res


# ═══════════════ Задача 9 (Д7) — find_register_writers: строки без повторов ═══════════════

_WRITERS_OBJECTS = [
    (
        "Documents",
        "Д",
        {
            "Ext/ObjectModule.bsl": (
                "Процедура ОбработкаПроведения(Отказ, Режим)\n"
                "\tДвижения.Рег.Записывать = Истина;\n"
                "\tДвижения.Рег.Очистить();\n"
                "\tДвижения.Рег.Загрузить(Таблица);\n"
                "КонецПроцедуры\n"
            ),
            "Ext/ManagerModule.bsl": (
                "Процедура ДобавитьДвижения() Экспорт\n"
                "\tНабор = РегистрыНакопления.Рег.СоздатьНаборЗаписей();\n"
                "\tНабор.Записать();\n"
                "КонецПроцедуры\n"
            ),
        },
        {"cf_props": "<Posting>Allow</Posting>"},
    ),
    ("AccumulationRegisters", "Рег", {}, {}),
]


class TestRegisterWritersDedup:
    def test_index_rows_are_distinct_pairs(self, tmp_path, monkeypatch):
        root = _objects_tree(tmp_path / "cf", "cf", _WRITERS_OBJECTS)
        reader = _reader_for(root, monkeypatch)
        try:
            res = _bsl_for(root, reader)["find_register_writers"]("Рег")
            # Порядок — порядок первого появления в выдаче ридера (вставка сборщика), поэтому сравнение без порядка.
            assert sorted(res["writers"], key=lambda w: w["source"]) == [
                {"document": "Д", "source": "code", "file": "Documents/Д/Ext/ObjectModule.bsl"},
                {"document": "Д", "source": "manager_code", "file": "Documents/Д/Ext/ManagerModule.bsl"},
            ], res
            assert res["total_writers"] == 2 and res["total_documents_scanned"] is None, res
        finally:
            reader.close()

    def test_live_branch_unchanged(self, tmp_path):
        """Обе версии: без индекса — прежний ответ (документ, файл, строки вхождений)."""
        root = _objects_tree(tmp_path / "cf", "cf", _WRITERS_OBJECTS)
        res = _bsl_for(root)["find_register_writers"]("Рег")
        assert res["writers"] == [{"document": "Д", "file": "Documents/Д/Ext/ObjectModule.bsl", "lines": [2, 3, 4]}]
        assert res["total_writers"] == 1, res


# ═══════════════ Задача 10 (Д8, П6) — count_matches: обход поддерева и память перечисления ═══════════════


def _count_tree(root: Path, fmt: str) -> tuple[Path, str, str]:
    """Файлы вне навигационного индекса: в CF — `.txt`, в EDT — формы `.form`. Возвращает (корень, расширение,
    путь одного файла под `Documents/Док`)."""
    if fmt == "cf":
        file_rel = "Documents/Док/Ext/Справка.txt"
        spec = {
            "Documents/Док/Ext/ObjectModule.bsl": PROC,
            file_rel: "DynamicList\nтекст\nDynamicList\n",
            "Catalogs/Спр/Ext/Справка.txt": "DynamicList\n",
        }
        return _cf_doc_tree(root, spec), "txt", file_rel
    file_rel = "Documents/Док/Forms/Ф/Form.form"
    spec = {
        "Documents/Док/ObjectModule.bsl": PROC,
        file_rel: "<form>DynamicList</form>\n<x>DynamicList</x>\n",
        "Catalogs/Спр/Forms/Ф/Form.form": "<form>DynamicList</form>\n",
    }
    return _edt_doc_tree(root, spec), "form", file_rel


@pytest.fixture(params=["cf-live", "cf-index", "edt-live", "edt-index"])
def count_env(request, tmp_path, monkeypatch):
    fmt, route = request.param.split("-")
    root, ext, file_rel = _count_tree(tmp_path / fmt, fmt)
    reader = _reader_for(root, monkeypatch) if route == "index" else None
    sink: dict = {}
    make_helpers(str(root), reader, _private_io=sink)
    yield root, reader, sink["glob_files_fs"], ext, file_rel
    if reader is not None:
        reader.close()


def _count_bsl(root, reader, fs_glob):
    return _bsl_for(root, reader, glob_files_fs_fn=fs_glob)["count_matches"]


class TestCountMatchesEnumeration:
    def test_path_narrows_fs_enumeration(self, count_env):
        root, reader, fs_glob, ext, _file = count_env
        calls: list[str] = []

        def spy(mask):
            calls.append(mask)
            return fs_glob(mask)

        count = _count_bsl(root, reader, spy)
        # Прежний алгоритм: полное перечисление маски `**/*.ext`, затем фильтр пути.
        full = _count_bsl(root, reader, lambda mask: fs_glob(f"**/*.{ext}"))
        res = count("DynamicList", file_types=ext, path="Documents/Док")
        assert calls == [f"Documents/Док/**/*.{ext}"], calls
        assert _dump(res) == _dump(full("DynamicList", file_types=ext, path="Documents/Док"))
        assert res["occurrences"] == 2 and res["files_matched"] == 1, res
        res_case = count("DynamicList", file_types=ext, path="documents/док")
        assert _dump(res_case) == _dump(full("DynamicList", file_types=ext, path="documents/док"))

    def test_path_to_file_keeps_previous_answer(self, count_env):
        """Обе версии: путь на сам файл — один файл в счёте."""
        root, reader, fs_glob, ext, file_rel = count_env
        res = _count_bsl(root, reader, fs_glob)("DynamicList", file_types=ext, path=file_rel)
        assert res["files_scanned"] == 1 and res["files_matched"] == 1 and res["occurrences"] == 2, res

    def test_fs_enumeration_memoized(self, count_env):
        root, reader, fs_glob, ext, _file = count_env
        calls: list[str] = []

        def spy(mask):
            calls.append(mask)
            return fs_glob(mask)

        count = _count_bsl(root, reader, spy)
        first = count("DynamicList", file_types=ext)
        second = count("текст|form", file_types=ext, regex=True)
        assert calls == [f"**/*.{ext}"], calls
        assert first["files_scanned"] == second["files_scanned"] == 2, (first, second)


# ═══════════════ Задача 11 (Д9) — синоним без префикса вида ═══════════════


def _syn(text: str) -> dict:
    return {"cf_props": _cf_syn(text), "edt": _edt_syn(text)}


_SYN_MAIN = [
    ("Documents", "Док", {"Ext/ObjectModule.bsl": PROC}, _syn("Тестовый документ")),
    ("Catalogs", "Спр", {"Ext/ObjectModule.bsl": PROC}, _syn("Тестовый справочник")),
    ("Reports", "Продажи", {"Ext/ObjectModule.bsl": PROC}, _syn("Отчет: продажи")),
]
_SYN_EXT = [("Catalogs", "Расш1_Спр", {"Ext/ObjectModule.bsl": PROC}, _syn("Тестовый справочник расширения"))]


@pytest.fixture(params=["cf-index", "cf-live", "edt-index", "edt-live"])
def syn_world(request, tmp_path, monkeypatch):
    fmt, route = request.param.split("-")
    cf, exts = _ext_tree(tmp_path / "src", fmt, _SYN_MAIN, {"Расш1": _SYN_EXT})
    reader = _reader_for(cf, monkeypatch) if route == "index" else None
    bsl = _bsl_for(
        cf,
        reader,
        extension_paths=[str(exts["Расш1"])],
        current_config_role="main",
        current_config_name="Тест",
        current_config_root=str(cf),
        extension_name_by_root={str(exts["Расш1"]): "Расш1"},
    )
    yield bsl, route
    if reader is not None:
        reader.close()


_RU_PREFIXES = ("Документ: ", "Справочник: ", "Отчёт: ")


class TestSynonymWithoutKindPrefix:
    def test_search_objects_synonym_without_kind_prefix(self, syn_world):
        bsl, route = syn_world
        rows = {r["object_name"]: r["synonym"] for r in bsl["search_objects"]("Тестов")}
        expected = {"Расш1_Спр": "Тестовый справочник расширения"}
        if route == "index":
            expected.update({"Док": "Тестовый документ", "Спр": "Тестовый справочник"})
        assert rows == expected, rows
        texts = [r["text"] for r in bsl["search"]("Тестов", scope="objects")]
        assert sorted(texts) == sorted(expected.values()), texts
        assert not any(t.startswith(_RU_PREFIXES) for t in texts)

    def test_full_structure_synonym_without_kind_prefix(self, syn_world):
        bsl, _route = syn_world
        assert bsl["get_object_full_structure"]("Документ.Док")["synonym"] == "Тестовый документ"

    def test_synonym_with_colon_is_kept_whole(self, syn_world):
        bsl, route = syn_world
        if route != "index":
            pytest.skip("синонимы основной конфигурации — только из индекса")
        rows = bsl["search_objects"]("продажи")
        assert [(r["object_name"], r["synonym"]) for r in rows] == [("Продажи", "Отчет: продажи")], rows

    def test_kind_query_finds_by_stored_form(self, syn_world):
        """Обе версии: совет рецепта исполним — запрос по виду находит объекты вида по хранимой форме."""
        bsl, route = syn_world
        if route != "index":
            pytest.skip("синонимы основной конфигурации — только из индекса")
        names = {r["object_name"] for r in bsl["search_objects"]("Документ:")}
        assert names == {"Док"}, names

    def test_search_objects_order_unchanged(self, syn_world):
        """Обе версии: ранжирование — по хранимой форме, порядок строк прежний."""
        bsl, route = syn_world
        names = [r["object_name"] for r in bsl["search_objects"]("Тестов")]
        assert names == (["Расш1_Спр", "Спр", "Док"] if route == "index" else ["Расш1_Спр"]), names

    def test_object_modules_by_synonym_with_colon(self, syn_world):
        """Обе версии: синоним с `': '` внутри сравнивается целиком — модули объекта находятся."""
        bsl, route = syn_world
        if route != "index":
            pytest.skip("синонимный маршрут — по индексу")
        res = bsl["get_object_modules"]("Отчет.Отчет: продажи")
        assert (res.get("category"), res.get("object_name")) == ("Reports", "Продажи"), res


# ═══════════════ Задача 12 (Д10; Д18 по П8) — back_scan: строка упоминания, раскладка EDT ═══════════════

_BACK_SCAN_SOURCE = (
    "// Модуль объекта\n"
    "\n"
    "Процедура Прочая()\n"
    "КонецПроцедуры\n"
    "\n"
    "Процедура ОбработкаЗаполнения(ДанныеЗаполнения, СтандартнаяОбработка)\n"
    "\tА = 1;\n"
    '\tЕсли ТипЗнч(ДанныеЗаполнения) = Тип("ДокументСсылка.Цель") Тогда\n'
    "\t\tА = 2;\n"
    "\tКонецЕсли;\n"
    "КонецПроцедуры\n"
)
_BACK_SCAN_LINE = 8  # строка `Тип("ДокументСсылка.Цель")`; объявление процедуры — 6
_BACK_SCAN_OBJECTS = [
    ("Documents", "Цель", {"Ext/ObjectModule.bsl": PROC}, {}),  # без ДобавитьКомандыСозданияНаОсновании
    ("Documents", "Источник", {"Ext/ObjectModule.bsl": _BACK_SCAN_SOURCE}, {}),
    ("Documents", "Третий", {"Ext/ObjectModule.bsl": PROC}, {}),
]


@pytest.fixture(params=["cf-index", "cf-live", "edt-index", "edt-live"])
def back_scan_env(request, tmp_path, monkeypatch):
    fmt, route = request.param.split("-")
    root = _objects_tree(tmp_path / "src", fmt, _BACK_SCAN_OBJECTS)
    reader = _reader_for(root, monkeypatch) if route == "index" else None
    yield _bsl_for(root, reader), root, fmt
    if reader is not None:
        reader.close()


def _back_scan_rows(bsl, name: str) -> list[dict]:
    return [r for r in bsl["find_based_on_documents"](name)["can_create_from_here"] if r["via"] == "back_scan"]


class TestBackScan:
    def test_back_scan_line_is_the_mention(self, back_scan_env):
        """Д10: `line` — строка упоминания `ДокументСсылка.<имя>`, а не начало процедуры; Д18 (П8): и на EDT."""
        bsl, root, fmt = back_scan_env
        rows = _back_scan_rows(bsl, "Цель")
        file = _obj_file(fmt, "Documents", "Источник", "Ext/ObjectModule.bsl")
        assert [(r["document"], r["category"], r["file"], r["line"]) for r in rows] == [
            ("Источник", "Documents", file, _BACK_SCAN_LINE)
        ], rows
        assert "ДокументСсылка.Цель" in (root / file).read_text(encoding="utf-8").splitlines()[rows[0]["line"] - 1]

    def test_back_scan_cf_rows_unchanged(self, back_scan_env):
        """Обе версии: на CF состав строк обратного скана прежний — Д10 меняет только `line`."""
        bsl, _root, fmt = back_scan_env
        if fmt != "cf":
            pytest.skip("на EDT обратного скана до v1.46.0 не было (Д18)")
        rows = _back_scan_rows(bsl, "Цель")
        assert [(r["document"], r["category"], r["file"]) for r in rows] == [
            ("Источник", "Documents", "Documents/Источник/Ext/ObjectModule.bsl")
        ], rows

    def test_no_mention_no_back_scan(self, back_scan_env):
        """Обе версии: документ, на который никто не ссылается, строк обратного скана не получает."""
        bsl, _root, _fmt = back_scan_env
        assert _back_scan_rows(bsl, "Третий") == []


# ═══════════════ Задачи 2–3, доп.: шлюз видит все раскладки описателя, которые знает сборщик ═══════════════


def _cf_ext_layout_tree(root: Path) -> Path:
    """CF: описатели в запасной раскладке сборщика (`_iter_metadata_xml_files`) — `Категория/Имя/Ext/<Тип>.xml`
    без `Имя.xml` рядом; у `ТолькоПредопределенные` в `Ext` — только `Predefined.xml` (описателем не бывает)."""
    _write(root / "Configuration.xml", CF_DESCRIPTOR)
    _write(root / "Catalogs" / "Контрагенты" / "Ext" / "Catalog.xml", _cf_md("Catalog", "Контрагенты"))
    _write(root / "Subsystems" / "Верх" / "Ext" / "Subsystem.xml", _cf_md("Subsystem", "Верх"))
    nested = root / "Subsystems" / "Верх" / "Subsystems" / "Вложенная" / "Ext" / "Subsystem.xml"
    _write(nested, _cf_md("Subsystem", "Вложенная"))
    _write(root / "Catalogs" / "ТолькоПредопределенные" / "Ext" / "Predefined.xml", "<PredefinedData/>")
    _write(root / "CommonModules" / "ОбщМод" / "Ext" / "Module.bsl", PROC)
    # смешанное дерево: описатель EDT у корня с `Configuration.xml`
    _write(root / "Documents" / "ДокМдо" / "ДокМдо.mdo", _edt_md("Document", "ДокМдо"))
    return root


@pytest.fixture(params=["live", "index"])
def cf_ext_layout(request, tmp_path, monkeypatch):
    root = _cf_ext_layout_tree(tmp_path / "src")
    reader = _reader_for(root, monkeypatch) if request.param == "index" else None
    yield _bsl_for(root, reader)
    if reader is not None:
        reader.close()


class TestGateBuilderLayouts:
    @pytest.mark.parametrize(
        "helper,ref",
        [
            ("find_code_usages", "Справочник.Контрагенты"),
            ("find_references_to_object", "Справочник.Контрагенты"),
            ("find_roles", "Справочник.Контрагенты"),
            ("find_references_to_object", "Subsystems/Вложенная"),
            ("find_code_usages", "Документ.ДокМдо"),
        ],
    )
    def test_cf_ext_layout_object_passes_the_gate(self, cf_ext_layout, helper, ref):
        """Обе версии: объект, описанный в раскладке `Имя/Ext/<Тип>.xml`, существует — шлюз (Д3) его не отвергает."""
        assert isinstance(cf_ext_layout[helper](ref), dict)

    def test_predefined_xml_is_not_a_descriptor(self, cf_ext_layout):
        """`Ext/Predefined.xml` описателем объекта не бывает (как у сборщика): маршрут `parse_object_xml` на него не
        ведёт."""
        res = cf_ext_layout["get_object_modules"]("ТолькоПредопределенные")
        assert "Predefined.xml" not in str(res.get("error")), res

    def test_cf_ext_layout_object_card(self, cf_ext_layout):
        """Объект без модулей в этой раскладке — ответ о нём (Д20), путь описателя — исполним."""
        res = cf_ext_layout["analyze_object"]("Контрагенты")
        assert (res.get("category"), res.get("name"), res.get("modules")) == ("Catalogs", "Контрагенты", []), res
        err = cf_ext_layout["get_object_modules"]("Контрагенты")["error"]
        assert "parse_object_xml('Catalogs/Контрагенты/Ext/Catalog.xml')" in err, err


# ═══════════════ Задача 13 (Д11) — префикс конфигурации и заголовок блока расширений ═══════════════

_EXT2_OBJECTS = [("Catalogs", "Расш2_Спр", {"Ext/ObjectModule.bsl": PROC}, {})]


def _start_session(path) -> tuple[str, dict]:
    from rlm_tools_bsl.server import _rlm_start

    resp = json.loads(_rlm_start(path=str(path), query="v1.46.0 extensions"))
    assert "error" not in resp, resp
    return resp["session_id"], resp


def _session_config_prefix(sid: str):
    from rlm_tools_bsl.server import _rlm_execute

    res = json.loads(_rlm_execute(sid, "print(repr(detect_extensions()['config_prefix']))"))
    assert res["error"] is None, res
    return res["stdout"].strip()


class TestExtensionPrefixAndHeader:
    def test_config_prefix_matches_start(self, tmp_path, monkeypatch):
        """Д11: префикс текущей конфигурации — одно значение в двух представлениях: нет префикса — `None` и у
        `detect_extensions()['config_prefix']` (прежде `''`), и у `extension_context.current_prefix` старта."""
        from rlm_tools_bsl.server import _rlm_end

        monkeypatch.setenv("RLM_SANDBOX_MODE", "inline")
        cf, exts = _ext_tree(tmp_path / "src", "cf", _WORLD_MAIN, {"Расш1": _WORLD_EXT})
        for path, expected in ((cf, None), (exts["Расш1"], "Расш1_")):
            sid, resp = _start_session(path)
            try:
                assert resp["extension_context"]["current_prefix"] == expected, resp["extension_context"]
                assert _session_config_prefix(sid) == repr(expected)
            finally:
                _rlm_end(sid)

    def test_one_extension_header_is_singular(self, tmp_path, monkeypatch):
        """Д11: одно расширение — «1 EXTENSION DETECTED» (прежде «1 EXTENSIONS»), в slim и в full."""
        from rlm_tools_bsl.bsl_knowledge import _extension_strategy
        from rlm_tools_bsl.extension_detector import detect_extension_context

        monkeypatch.delenv("RLM_EXT_LIST_CAP", raising=False)
        cf, _exts = _ext_tree(tmp_path / "src", "cf", _WORLD_MAIN, {"Расш1": _WORLD_EXT})
        ctx = detect_extension_context(str(cf))
        for slim in (True, False):
            text = _extension_strategy(ctx, {}, slim=slim)
            assert "CRITICAL — 1 EXTENSION DETECTED" in text and "EXTENSIONS DETECTED" not in text, text

    def test_two_extensions_header_unchanged(self, tmp_path, monkeypatch):
        """Обе версии: два расширения и больше — прежний текст «N EXTENSIONS DETECTED»."""
        from rlm_tools_bsl.bsl_knowledge import _extension_strategy
        from rlm_tools_bsl.extension_detector import detect_extension_context

        monkeypatch.delenv("RLM_EXT_LIST_CAP", raising=False)
        cf, _exts = _ext_tree(tmp_path / "src", "cf", _WORLD_MAIN, {"Расш1": _WORLD_EXT, "Расш2": _EXT2_OBJECTS})
        ctx = detect_extension_context(str(cf))
        for slim in (True, False):
            assert "CRITICAL — 2 EXTENSIONS DETECTED" in _extension_strategy(ctx, {}, slim=slim)


# ═══════════════ Задача 14 (Д12, П5) — подсказка проведения без «объявления» платформенных методов ═══════════════

_PP_HANDLER = (
    "Процедура ОбработкаПроведения(Отказ, Режим)\n"
    '\tДополнительныеСвойства.Вставить("Ключ", 1);\n'
    '\tДополнительныеСвойства.Свойство("Ключ");\n'
    '\tЭтотОбъект.ДополнительныеСвойства.Вставить("Ключ2", 2);\n'
    '\tThisObject.AdditionalProperties.Insert("Key", 3);\n'
    "\tТ = Товары.Выгрузить();\n"
    "\tН = ЭтотОбъект.Товары.Количество();\n"
    "\tМодульПроведения.ОтразитьДвижения(ЭтотОбъект);\n"
    "КонецПроцедуры\n"
)
_PP_KEEP_HANDLER = (
    "Процедура ОбработкаПроведения(Отказ, Режим)\n"
    "\tСвойства = ПолучитьСвойства();\n"
    '\tСвойства.Вставить("Ключ", 1);\n'
    "\tТовары.Записать();\n"
    '\tСервис.ДополнительныеСвойства.Вставить("Ключ", 1);\n'
    "КонецПроцедуры\n"
)
_PP_SHADOW_HANDLER = (
    "Процедура ОбработкаПроведения(ДополнительныеСвойства, Режим)\n"
    '\tДополнительныеСвойства.Вставить("Ключ", 1);\n'
    "КонецПроцедуры\n"
)
_PP_TS = {
    "cf_children": "<ChildObjects><TabularSection><Properties><Name>Товары</Name></Properties></TabularSection>"
    "</ChildObjects>",
    "edt": "<tabularSections><name>Товары</name></tabularSections>",
}
_PP_ATTR = {
    "cf_children": "<ChildObjects><Attribute><Properties><Name>ДополнительныеСвойства</Name><Type>"
    "<v8:Type>xs:string</v8:Type></Type></Properties></Attribute></ChildObjects>",
    "edt": "<attributes><name>ДополнительныеСвойства</name><type><types>String</types></type></attributes>",
}


def _pp_objects(handler: str, extra: dict) -> list:
    return [
        ("Documents", "Док", {"Ext/ObjectModule.bsl": handler}, extra),
        # служебный документ: таблица движений индекса непуста (иначе индексный маршрут не отвечает)
        (
            "Documents",
            "С",
            {
                "Ext/ObjectModule.bsl": "Процедура ОбработкаПроведения(Отказ, Режим)\n\tДвижения.РегС.Записывать = Истина;\nКонецПроцедуры\n"
            },
            {},
        ),
        (
            "CommonModules",
            "МодульПроведения",
            {"Ext/Module.bsl": "Процедура ОтразитьДвижения(Объект) Экспорт\nКонецПроцедуры\n"},
            {},
        ),
    ]


@pytest.fixture(params=["cf-live", "cf-index", "edt-live", "edt-index"])
def posting_env(request, tmp_path, monkeypatch):
    fmt, route = request.param.split("-")
    readers = []

    def build(handler: str, extra: dict = _PP_TS):
        root = _objects_tree(tmp_path / f"src{len(readers)}", fmt, _pp_objects(handler, extra))
        reader = _reader_for(root, monkeypatch) if route == "index" else None
        readers.append(reader)
        return _bsl_for(root, reader)["find_register_movements"]("Док")

    yield build
    for reader in readers:
        if reader is not None:
            reader.close()


def _pp_delegates(res: dict) -> list[tuple[str, str, str]]:
    return [(d["receiver"], d["method"], d["kind"]) for d in res["_meta"]["delegates"]]


class TestPostingPlatformReceivers:
    def test_platform_collection_methods_are_not_delegates(self, posting_env):
        """Д12 (П5): методы Структуры у `ДополнительныеСвойства` и методы табличной части документа — одним именем и
        через `ЭтотОбъект.`/`ThisObject.` — не делегаты: их нет в `_meta.delegates`, и подсказка не ищет их объявление."""
        res = posting_env(_PP_HANDLER)
        assert _pp_delegates(res) == [("МодульПроведения", "ОтразитьДвижения", "common_module")], res["_meta"]
        for method in ("Вставить", "Свойство", "Insert", "Выгрузить", "Количество"):
            assert f"find_definition({method!r}" not in res["hint"], (method, res["hint"])

    def test_other_receivers_keep_previous_answer(self, posting_env):
        """Обе версии: переменная, `Записать` у табличной части и чужая цепочка с тем же последним звеном — прежние
        делегаты."""
        res = posting_env(_PP_KEEP_HANDLER)
        assert _pp_delegates(res) == [
            ("Свойства", "Вставить", "variable"),
            ("Товары", "Записать", "attribute"),
            ("Сервис.ДополнительныеСвойства", "Вставить", "unknown"),
        ], res["_meta"]

    def test_shadowed_property_keeps_previous_answer(self, posting_env):
        """Обе версии: параметр с именем `ДополнительныеСвойства` и реквизит с таким именем — прежний делегат."""
        assert _pp_delegates(posting_env(_PP_SHADOW_HANDLER)) == [("ДополнительныеСвойства", "Вставить", "variable")]
        attr_handler = (
            'Процедура ОбработкаПроведения(Отказ, Режим)\n\tДополнительныеСвойства.Вставить("К", 1);\nКонецПроцедуры\n'
        )
        res = posting_env(attr_handler, _PP_ATTR)
        assert _pp_delegates(res) == [("ДополнительныеСвойства", "Вставить", "attribute")], res["_meta"]


# ═══════════════ Задача 15 (Д13, Д14, Д15) — тексты: запрос формы, единицы модулей, точность цели ═══════════════

_LONG_QUERY = "ВЫБРАТЬ\n" + ",\n".join(f"\tДок.Поле{i} КАК Поле{i}" for i in range(60)) + "\nИЗ\n\tДокумент.Док КАК Док"
_CF_FORM_QUERY = (
    '<Form xmlns="http://v8.1c.ru/8.3/xcf/logform" xmlns:v8="http://v8.1c.ru/8.1/data/core" '
    'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
    '<Attributes><Attribute name="Список"><Type><v8:Type>cfg:DynamicList</v8:Type></Type>'
    '<MainAttribute>true</MainAttribute><Settings xsi:type="DynamicList"><MainTable>Document.Док</MainTable>'
    f"<QueryText>{_LONG_QUERY}</QueryText></Settings></Attribute></Attributes></Form>"
)
_EDT_FORM_QUERY = (
    '<form:Form xmlns:form="http://g5.1c.ru/v8/dt/form"><form:attributes><name>Список</name>'
    "<main>true</main><form:valueType><types>DynamicList</types></form:valueType>"
    f"<extInfo><mainTable>Document.Док</mainTable><queryText>{_LONG_QUERY}</queryText></extInfo>"
    "</form:attributes></form:Form>"
)
_CALLS_TARGET = "Процедура ЗаполнитьДокумент() Экспорт\nКонецПроцедуры\n"
_CALLS_LOCAL = "\nПроцедура Другая()\n\tЗаполнитьДокумент();\nКонецПроцедуры\n"
_CALLS_SERVICE = "Процедура Заполнить(Док) Экспорт\n\tДок.ЗаполнитьДокумент();\nКонецПроцедуры\n"


def _calls_bsl(tmp_path, monkeypatch, *, local_call: bool):
    """Экспорт модуля объекта `РеализацияТоваровУслуг`, вызываемый через переменную из общего модуля (ребро по
    имени); `local_call` — ещё и вызов без точки в своём модуле (точное ребро)."""
    om = _CALLS_TARGET + (_CALLS_LOCAL if local_call else "")
    root = _objects_tree(
        tmp_path / ("src_local" if local_call else "src"),
        "cf",
        [
            ("Documents", "РеализацияТоваровУслуг", {"Ext/ObjectModule.bsl": om}, {}),
            ("CommonModules", "Сервис", {"Ext/Module.bsl": _CALLS_SERVICE}, {}),
        ],
    )
    reader = _reader_for(root, monkeypatch)
    return _bsl_for(root, reader), reader, "Documents/РеализацияТоваровУслуг/Ext/ObjectModule.bsl"


class TestTexts1460:
    @pytest.mark.parametrize("fmt", ["cf", "edt"])
    @pytest.mark.parametrize("route", ["live", "index"])
    def test_form_query_route_is_executable(self, tmp_path, monkeypatch, fmt, route):
        """Д13: полный текст запроса динамического списка — `read_file(f['file'])` (рецепт), а не
        `extract_queries(module_path)` (в модуле формы запроса нет)."""
        from rlm_tools_bsl.bsl_helpers import build_helper_metadata_snapshot

        form = {"Forms/ФормаСписка/Ext/Form.xml": (_CF_FORM_QUERY, _EDT_FORM_QUERY)}
        root = _objects_tree(tmp_path / "src", fmt, [("Documents", "Док", form, {})])
        reader = _reader_for(root, monkeypatch) if route == "index" else None
        try:
            forms = _bsl_for(root, reader)["parse_form"]("Документ.Док")
            attr = next(a for a in forms[0]["attributes"] if a.get("query_text"))
            assert len(attr["query_text"]) == 512 and _LONG_QUERY.startswith(attr["query_text"]), attr
            helpers, _safe = make_helpers(str(root), reader)
            assert _LONG_QUERY in helpers["read_file"](forms[0]["file"])
        finally:
            if reader is not None:
                reader.close()
        recipe = build_helper_metadata_snapshot()["parse_form"]["recipe"]
        assert "extract_queries(module_path)" not in recipe and "read_file(f['file'])" in recipe, recipe

    def test_object_modules_sig_names_units(self):
        """Д14 (П4): подпись называет единицы — перехваты в МЕТОДАХ, верхний index_used — все модули из индекса."""
        from rlm_tools_bsl.bsl_helpers import build_helper_metadata_snapshot

        sig = build_helper_metadata_snapshot()["get_object_modules"]["sig"]
        assert "overrides:{count=методов," in sig and "_meta:{index_used=все из индекса," in sig, sig
        assert len(sig) == 506, len(sig)

    def test_callers_exact_target_name_rows_hint(self, tmp_path, monkeypatch):
        """Д15: цель разрешена точно, а точных рёбер нет — `_meta.hint` (строки — вызовы по имени); при точных
        рёбрах подсказки нет."""
        bsl, reader, path = _calls_bsl(tmp_path, monkeypatch, local_call=False)
        try:
            meta = bsl["find_callers_context"]("ЗаполнитьДокумент", path)["_meta"]
            assert meta["target_exact"] and meta["exact_rows"] == 0 and meta["fallback_rows"] > 0, meta
            assert "edge_exact=False" in meta.get("hint", ""), meta
        finally:
            reader.close()
        bsl, reader, path = _calls_bsl(tmp_path, monkeypatch, local_call=True)
        try:
            meta = bsl["find_callers_context"]("ЗаполнитьДокумент", path)["_meta"]
            assert meta["exact_rows"] > 0 and "hint" not in meta, meta
        finally:
            reader.close()

    def test_hierarchy_recipe_marks_nodes_by_edges(self, tmp_path, monkeypatch):
        """Д15: `code_hint` темы «иерархия вызовов» метит узел по РЁБРАМ, а не по точности цели: узел без точных рёбер
        не помечен «точно» (прежде `[EXACT]` по `target_exact`)."""
        import contextlib
        import io

        from rlm_tools_bsl.bsl_knowledge import _BUSINESS_RECIPES

        bsl, reader, _path = _calls_bsl(tmp_path, monkeypatch, local_call=False)
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                exec(_BUSINESS_RECIPES["иерархия вызовов"]["code_hint"], dict(bsl))  # noqa: S102
        finally:
            reader.close()
        node_line = next(line for line in out.getvalue().splitlines() if line.startswith("ЗаполнитьДокумент ["))
        assert "ребра: по имени" in node_line and "EXACT" not in node_line, out.getvalue()


# ═══════════════ Задача 16 (Д16, П3 — вариант V1) — сверка состава категорий на старте ═══════════════

_DRIFT_OBJECTS = [
    ("Documents", "Док", {"Ext/ObjectModule.bsl": PROC}, {}),
    ("EventSubscriptions", "Подписка", {}, {}),
]


@pytest.fixture
def drift_env(tmp_path, monkeypatch):
    """CF с индексом; `RLM_INDEX_DIR` остаётся тем, что выставил `_reader_for` (старт найдёт тот же индекс). Окружающий
    git отсечён: временный каталог внутри рабочего дерева git увёл бы `index update` на git-быстрый путь."""
    monkeypatch.setenv("RLM_SANDBOX_MODE", "inline")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    root = _objects_tree(tmp_path / "src", "cf", _DRIFT_OBJECTS)
    reader = _reader_for(root, monkeypatch)
    built_at = float(reader.get_statistics()["built_at"])
    reader.close()
    return root, built_at


def _start_index_warnings(root) -> list[str]:
    from rlm_tools_bsl.server import _rlm_end

    sid, resp = _start_session(root)
    _rlm_end(sid)
    assert resp["index"]["index_status"] == "ok", resp["index"]
    return resp["index"]["warnings"]


def _touch_dir(path: Path, when: float) -> None:
    os.utime(path, (when, when))


def _drift_warnings(warnings: list[str]) -> list[str]:
    return [w for w in warnings if "состав категорий" in w]


class TestStartCategoryDrift:
    def test_added_object_is_warned(self, drift_env):
        """Д16: объект добавлен после сборки — каталог категории моложе `built_at`: предупреждение называет категорию."""
        root, built_at = drift_env
        _write(root / "Documents" / "Новый.xml", _cf_md("Document", "Новый"))
        (root / "Documents" / "Новый").mkdir()
        _touch_dir(root / "Documents", built_at + 100)
        drift = _drift_warnings(_start_index_warnings(root))
        assert len(drift) == 1 and "Documents" in drift[0] and "index update" in drift[0], drift
        assert "вложенную подсистему" in drift[0], drift  # граница проверки названа

    def test_category_outside_metadata_categories(self, drift_env):
        """Д16: подписки (их нет в `METADATA_CATEGORIES`) — тоже категория индекса."""
        root, built_at = drift_env
        _write(root / "EventSubscriptions" / "Новая.xml", _cf_md("EventSubscription", "Новая"))
        _touch_dir(root / "EventSubscriptions", built_at + 100)
        drift = _drift_warnings(_start_index_warnings(root))
        assert len(drift) == 1 and "EventSubscriptions" in drift[0] and "Documents" not in drift[0], drift

    def test_no_change_no_warning(self, drift_env):
        """Обе версии: без изменений состава — предупреждения нет."""
        root, _built_at = drift_env
        assert _drift_warnings(_start_index_warnings(root)) == []

    def test_module_edit_is_not_category_drift(self, drift_env):
        """Обе версии: правка содержимого модуля — не изменение состава категории (её ловит выборочная сверка)."""
        root, built_at = drift_env
        module = root / "Documents" / "Док" / "Ext" / "ObjectModule.bsl"
        module.write_text(PROC + "// правка\n", encoding="utf-8")
        _touch_dir(module, built_at + 100)
        _touch_dir(root / "Documents", built_at - 100)
        assert _drift_warnings(_start_index_warnings(root)) == []

    def test_check_does_not_list_directories(self, drift_env, monkeypatch):
        """Сверка — только `stat` каталогов по известным именам, без листинга."""
        from rlm_tools_bsl import bsl_index

        root, built_at = drift_env
        _touch_dir(root / "Documents", built_at + 100)
        calls = []
        real_scandir = os.scandir
        monkeypatch.setattr(os, "scandir", lambda *a, **k: calls.append(a) or real_scandir(*a, **k))
        assert bsl_index.categories_changed_since(str(root), built_at) == ["Documents"]
        assert calls == []

    @pytest.mark.parametrize("change", ["added_role", "removed_subscription"])
    def test_update_without_bsl_delta_clears_the_warning(self, drift_env, change):
        """Д16: `index update` полным сканом (без git) снимает предупреждение и тогда, когда изменились только
        описатели — объект без модулей. `built_at` — момент сверки индекса с исходниками, как у git-быстрого пути и
        сборки; прежде полный скан переписывал его только при изменённых `.bsl`, и предупреждение повторялось на
        каждом старте после `update`."""
        import sqlite3

        from rlm_tools_bsl.bsl_index import get_index_db_path

        root, built_at = drift_env
        # Индекс собран «100 с назад», каталоги категорий — старше него. Время каталога изменённой категории —
        # настоящее: utime в будущее предупреждение не снял бы и верный `update`.
        con = sqlite3.connect(get_index_db_path(str(root)))
        try:
            with con:
                con.execute("UPDATE index_meta SET value = ? WHERE key = 'built_at'", (str(built_at - 100),))
        finally:
            con.close()
        for category in ("Documents", "EventSubscriptions"):
            _touch_dir(root / category, built_at - 200)
        if change == "added_role":
            _write(root / "Roles" / "НоваяРоль.xml", _cf_md("Role", "НоваяРоль"))
            category = "Roles"
        else:
            (root / "EventSubscriptions" / "Подписка.xml").unlink()
            category = "EventSubscriptions"
        drift = _drift_warnings(_start_index_warnings(root))
        assert len(drift) == 1 and category in drift[0], drift  # предусловие: до update предупреждение есть
        res = IndexBuilder().update(str(root))
        assert res["git_fast_path"] is False and (res["added"], res["changed"], res["removed"]) == (0, 0, 0), res
        assert _drift_warnings(_start_index_warnings(root)) == []


# ═══════════════ Задача 17 (Д19, П9) — find_enum_values: точное имя первым ═══════════════


def _enum_extra(values: list[str], synonym: str = "") -> dict:
    return {
        "cf_props": _cf_syn(synonym) if synonym else "",
        "cf_children": "<ChildObjects>"
        + "".join(f"<EnumValue><Properties><Name>{v}</Name></Properties></EnumValue>" for v in values)
        + "</ChildObjects>",
        "edt": (_edt_syn(synonym) if synonym else "")
        + "".join(f"<enumValues><name>{v}</name></enumValues>" for v in values),
    }


_ENUM_RATES = ["НДС20", "НДС10", "БезНДС"]
_ENUM_OBJECTS = [
    ("Enums", "СтавкиНДС", {}, _enum_extra(_ENUM_RATES, "Ставки НДС")),
    ("Enums", "СпособыЗаполненияСтавкиНДС", {}, _enum_extra(["ИзКарточки", "Вручную"], "Способы заполнения ставки")),
    ("Enums", "ВариантыМетка", {}, _enum_extra(["Первый"])),
    ("CommonPictures", "Метка", {}, {}),
    ("Subsystems", "Верх", {}, {}),
    ("Subsystems", "Верх/Вложенная", {}, {}),
    ("Reports", "Задачи", {"Ext/ObjectModule.bsl": PROC}, {}),
    (
        "Documents",
        "Док",
        {"Ext/ObjectModule.bsl": PROC},
        {
            "cf_children": "<ChildObjects><Attribute><Properties><Name>Ставка</Name><Type>"
            "<v8:Type>cfg:EnumRef.СтавкиНДС</v8:Type></Type></Properties></Attribute></ChildObjects>",
            "edt": "<attributes><name>Ставка</name><type><types>EnumRef.СтавкиНДС</types></type></attributes>",
        },
    ),
]


@pytest.fixture(params=["cf-index", "cf-live", "edt-index", "edt-live"])
def enum_env(request, tmp_path, monkeypatch):
    fmt, route = request.param.split("-")
    root = _objects_tree(tmp_path / "src", fmt, _ENUM_OBJECTS)
    reader = _reader_for(root, monkeypatch) if route == "index" else None
    yield _bsl_for(root, reader), route
    if reader is not None:
        reader.close()


def _homonym_enum_tree(root: Path) -> Path:
    """Документ `Счет` с модулем (без реквизитов и синонима), перечисление `Счет` без синонима и перечисление
    `ВидыСчет`, строка которого в таблице `enum_values` — раньше."""
    return _objects_tree(
        root,
        "cf",
        [
            ("Documents", "Счет", {"Ext/ObjectModule.bsl": PROC}, {}),
            ("Enums", "Счет", {}, _enum_extra(["А"])),
            ("Enums", "ВидыСчет", {}, _enum_extra(["Б"])),
        ],
    )


def _case_sensitive_glob(glob_fn):
    """glob_files, различающий регистр, как ``pathlib.glob`` на Linux: на Windows маска регистр не различает, и без
    обёртки ветка регистрозависимой ФС там не исполнялась бы (регистр тест задаёт сам, а не берёт у ФС)."""
    import re

    def _rx(pattern: str):
        out, i = [], 0
        while i < len(pattern):
            if pattern.startswith("**/", i):
                out.append("(?:.*/)?")
                i += 3
                continue
            ch = pattern[i]
            out.append("[^/]*" if ch == "*" else "[^/]" if ch == "?" else re.escape(ch))
            i += 1
        return re.compile("".join(out) + r"\Z")

    return lambda pattern: [f for f in glob_fn(pattern) if _rx(pattern).match(f)]


def _reorder_enum_rows(db_path: str, first: str) -> None:
    import sqlite3

    conn = sqlite3.connect(db_path)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(enum_values)") if r[1] != "id"]
    rows = conn.execute(f"SELECT {', '.join(cols)} FROM enum_values").fetchall()
    rows.sort(key=lambda r: r[cols.index("name")] != first)
    conn.execute("DELETE FROM enum_values")
    conn.executemany(f"INSERT INTO enum_values ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", rows)
    conn.commit()
    conn.close()


class TestEnumExactName:
    def test_exact_enum_name_wins(self, enum_env):
        """Д19 (П9): точное имя (без учёта регистра) — первым; прежде — первое, чьё имя СОДЕРЖИТ запрос."""
        bsl, _fmt = enum_env
        for arg in ("СтавкиНДС", "ставкиндс", "Перечисление.СтавкиНДС"):
            res = bsl["find_enum_values"](arg)
            assert res.get("name") == "СтавкиНДС" and [v["name"] for v in res["values"]] == _ENUM_RATES, (arg, res)
            assert "hint" not in res, res

    @pytest.mark.parametrize("fmt", ["cf", "edt"])
    def test_exact_enum_name_in_other_case_on_case_sensitive_fs(self, tmp_path, fmt):
        """Д19: без индекса маска имени файла на регистрозависимой ФС (Linux) имя в другом регистре не находит —
        точное имя берётся из описателей каталога Enums без учёта регистра (прежде — «не найдено»)."""
        root = _objects_tree(tmp_path / "src", fmt, _ENUM_OBJECTS)
        glob_fn = _case_sensitive_glob(make_helpers(str(root), None)[0]["glob_files"])
        ext = "xml" if fmt == "cf" else "mdo"
        # предусловие: обёртка различает регистр, а файл точного написания видит
        assert glob_fn(f"**/Enums/**/*СтавкиНДС*.{ext}") and not glob_fn(f"**/Enums/**/*ставкиндс*.{ext}")
        bsl = _bsl_for(root, None, glob_wrap=_case_sensitive_glob)
        for arg in ("ставкиндс", "СТАВКИНДС", "Перечисление.ставкиндс"):
            res = bsl["find_enum_values"](arg)
            assert res.get("name") == "СтавкиНДС" and [v["name"] for v in res["values"]] == _ENUM_RATES, (arg, res)
            assert res["file"] == _descriptor_rel(fmt, "Enums", "СтавкиНДС") and "hint" not in res, res

    def test_typed_enum_fragment_not_found(self, enum_env):
        """Д19: ссылка `Перечисление.X` — только точное имя; фрагмент в ссылке — `{error}`."""
        bsl, _fmt = enum_env
        assert "error" in bsl["find_enum_values"]("Перечисление.Ставки")

    def test_bare_fragment_keeps_choice_with_hint(self, enum_env):
        """Д19: голый фрагмент — прежний подстрочный выбор и верхний `hint` с маршрутом к точному имени."""
        bsl, route = enum_env
        res = bsl["find_enum_values"]("Ставки")
        assert res.get("name") in ("СпособыЗаполненияСтавкиНДС", "СтавкиНДС"), res
        assert res["name"] in res["hint"] and "search_objects('Ставки')" in res["hint"], res
        if route == "index":  # совет исполним: синонимы основной конфигурации ищутся по индексу
            assert "СтавкиНДС" in {r["object_name"] for r in bsl["search_objects"]("Ставки")}

    def test_bare_name_of_other_category_is_refused(self, enum_env):
        """Д19: голое имя объекта другой категории (отчёт `Задачи`) — отказ, как у ссылки другой категории."""
        bsl, _fmt = enum_env
        with pytest.raises(ValueError, match="^find_enum_values: .*Reports"):
            bsl["find_enum_values"]("Задачи")
        many = bsl["find_enum_values"](["Задачи", "СтавкиНДС"])
        assert "error" in many["Задачи"] and many["СтавкиНДС"]["name"] == "СтавкиНДС", many

    def test_bare_name_of_descriptor_only_object_is_refused(self, enum_env):
        """Д19: объект без модулей и синонима (общая картинка `Метка`) прежний союз не видит — отказ по описателю
        (прежде — перечисление `ВариантыМетка`)."""
        bsl, _fmt = enum_env
        with pytest.raises(ValueError, match="CommonPictures"):
            bsl["find_enum_values"]("Метка")
        assert "error" in bsl["find_enum_values"](["Метка"])["Метка"]

    def test_other_category_refusal_route_is_executable(self, enum_env):
        """Д19: маршрут отказа — путь найденного описателя: у вложенной подсистемы `parse_object_xml` из текста
        отказа отвечает объектом (прежний маршрут `Subsystems/<Имя>` вложенную не находил)."""
        import re as _re

        bsl, _fmt = enum_env
        with pytest.raises(ValueError, match="Subsystems") as ei:
            bsl["find_enum_values"]("Вложенная")
        route = _re.search(r"parse_object_xml\('([^']+)'\)", str(ei.value)).group(1)
        parsed = bsl["parse_object_xml"](route)
        assert parsed.get("name") == "Вложенная" and "error" not in parsed, (route, parsed)

    def test_full_structure_expands_exact_enum(self, enum_env):
        """Д19: раскрытие типа реквизита — значения ИМЕННО перечисления типа (прежде — первого по подстроке)."""
        bsl, route = enum_env
        expanded = bsl["get_object_full_structure"]("Документ.Док")["enum_values_for_typed_refs"]
        if route == "live" and "EnumRef.СтавкиНДС" not in expanded:
            # Давнее (и на HEAD), вне Д19: на живом маршруте CF тип реквизита несёт префикс `cfg:`
            # (`cfg:EnumRef.X`), `_is_enum_ref` его не узнаёт — раскрытия нет вовсе. Кандидат в бэклог.
            assert expanded == {}, expanded
            return
        assert [v["name"] for v in expanded["EnumRef.СтавкиНДС"]] == _ENUM_RATES, expanded

    def test_cascade_homonym_enum_without_synonym(self, tmp_path, monkeypatch):
        """Д19 (объявлено): каскад на омониме «перечисление без синонима и реквизитов + документ с модулями» выбирает
        перечисление (прежде — по порядку строк таблицы); выбор `get_object_modules` — по модулям, прежний."""
        root = _homonym_enum_tree(tmp_path / "src")
        reader = _reader_for(root, monkeypatch)
        db = str(reader._db_path) if hasattr(reader, "_db_path") else None
        reader.close()
        from rlm_tools_bsl.bsl_index import get_index_db_path

        _reorder_enum_rows(db or str(get_index_db_path(str(root))), "ВидыСчет")
        reader = IndexReader(db or get_index_db_path(str(root)))
        try:
            bsl = _bsl_for(root, reader)
            assert bsl["get_object_full_structure"]("Счет")["category"] == "Enums"
            assert bsl["get_object_profile"]("Счет")["category"] == "Enums"
            assert bsl["get_object_modules"]("Счет")["category"] == "Documents"
        finally:
            reader.close()
