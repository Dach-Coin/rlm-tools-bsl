"""v1.49.0 — ответ называет то, что есть.

Релиз read-time: схема индекса и BUILDER_VERSION не тронуты. Приватные фикстуры других файлов не импортируются
(каталог tests не пакет) — деревья собираются здесь же, по образцу ``tests/test_v1_46_0.py``.

Тесты «обе версии» проходят и на HEAD `b03e854`: прогон против выгрузки HEAD —
``PYTHONPATH=<выгрузка>/src uv run pytest tests/test_v1_49_0.py …``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import textwrap
from pathlib import Path

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


def _write(path: Path, text: str, eol: str = "\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.replace("\n", eol).encode("utf-8"))


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


def _build_index(root: Path, idx_dir: Path, **build_kw) -> Path:
    """Сборка индекса дерева ``root`` в ``idx_dir`` (без monkeypatch теста — для фикстур уровня модуля)."""
    mp = pytest.MonkeyPatch()
    mp.setenv("RLM_INDEX_DIR", str(idx_dir))
    try:
        return IndexBuilder().build(str(root), **build_kw)
    finally:
        mp.undo()


def _db_copy(src, dst: Path) -> Path:
    """Копия индекса через API резервного копирования SQLite (WAL-файлы не теряются)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    s, d = sqlite3.connect(str(src)), sqlite3.connect(str(dst))
    try:
        s.backup(d)
    finally:
        s.close()
        d.close()
    return dst


_V18_OVERRIDE_COLUMNS = {"form_item", "form_event"}


def _downgrade_to_v17(db_path) -> None:
    """Индекс прежнего поколения: ``builder_version``/``version`` = 17 и ``extension_overrides`` без колонок v18
    (копия из ``tests/test_v1_47_0.py``)."""
    con = sqlite3.connect(str(db_path))
    try:
        cols = [r[1] for r in con.execute("PRAGMA table_info(extension_overrides)")]
        keep = [c for c in cols if c not in _V18_OVERRIDE_COLUMNS]
        if len(keep) != len(cols):
            con.executescript(
                f"CREATE TABLE eo_old AS SELECT {', '.join(keep)} FROM extension_overrides;"
                "DROP TABLE extension_overrides;"
                "ALTER TABLE eo_old RENAME TO extension_overrides;"
            )
        con.execute("UPDATE index_meta SET value='17' WHERE key IN ('version', 'builder_version')")
        con.commit()
    finally:
        con.close()


_CF_MD_NS = (
    'xmlns="http://v8.1c.ru/8.3/MDClasses" xmlns:cfg="http://v8.1c.ru/8.1/data/enterprise/current-config" '
    'xmlns:v8="http://v8.1c.ru/8.1/data/core" xmlns:xr="http://v8.1c.ru/8.3/xcf/readable" '
    'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'
)
_EDT_MD_NS = 'xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass"'
_CF_FORM_NS = (
    'xmlns="http://v8.1c.ru/8.3/xcf/logform" xmlns:v8="http://v8.1c.ru/8.1/data/core" '
    'xmlns:cfg="http://v8.1c.ru/8.1/data/enterprise/current-config" '
    'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'
)


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
    "Subsystems": "Subsystem",
    "Enums": "Enum",
    "ChartsOfCharacteristicTypes": "ChartOfCharacteristicTypes",
    "DefinedTypes": "DefinedType",
    "Roles": "Role",
    "CommonCommands": "CommonCommand",
    "CommonForms": "CommonForm",
    # 17 категорий вне прежнего разбора аргумента
    "EventSubscriptions": "EventSubscription",
    "ScheduledJobs": "ScheduledJob",
    "FunctionalOptions": "FunctionalOption",
    "CommonPictures": "CommonPicture",
    "Styles": "Style",
    "StyleItems": "StyleItem",
    "Languages": "Language",
    "SessionParameters": "SessionParameter",
    "CommonAttributes": "CommonAttribute",
    "Sequences": "Sequence",
    "DocumentNumerators": "DocumentNumerator",
    "CommandGroups": "CommandGroup",
    "Interfaces": "Interface",
    "Bots": "Bot",
    "IntegrationServices": "IntegrationService",
    "FunctionalOptionsParameters": "FunctionalOptionsParameter",
    "WSReferences": "WSReference",
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
    if cf_rel == "Ext/Form/Module.bsl":  # модуль общей формы
        return "Module.bsl"
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
    eol: str = "\n",
) -> Path:
    """Объекты в раскладке CF/EDT: ``(каталог категории, имя, {путь CF внутри объекта: текст | (cf, edt)},
    доп. XML)``; доп. XML — ``{'cf_props', 'cf_children', 'edt'}`` (любые ключи необязательны). Вложенная
    подсистема — имя ``'Верх/Вложенная'``. ``descriptor`` — описатель конфигурации (по умолчанию — основной);
    ``eol`` — окончание строк модулей ``.bsl``."""
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
                rel = cf_rel if fmt == "cf" else _edt_rel(cf_rel)
                _write(obj_dir / own / rel, body, eol if rel.endswith(".bsl") else "\n")
    return root


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


def _call(bsl, helper: str, value):
    """Исход вызова хелпера с объектом ``value``: ответ либо ``('raises', тип, текст)``."""
    try:
        if helper == "find_custom_modifications":
            return bsl[helper](value, custom_prefixes=["х"])
        return bsl[helper](value)
    except Exception as exc:  # noqa: BLE001 — исход сравнивается целиком
        return ("raises", type(exc), str(exc))


def _dump(obj) -> str:
    return json.dumps(_strip_times(obj), ensure_ascii=False, sort_keys=True, default=str)


# ═══════════════ Задача 0 — мир релиза ═══════════════

PROC = "Процедура П() Экспорт\nКонецПроцедуры\n"
CHAR = "Characteristic.ВидыАналитик"


def _cf_attr(name: str, types=(), sets=()) -> str:
    inner = "".join(f"<v8:Type>{t}</v8:Type>" for t in types) + "".join(f"<v8:TypeSet>{t}</v8:TypeSet>" for t in sets)
    return f"<Attribute><Properties><Name>{name}</Name><Type>{inner}</Type></Properties></Attribute>"


def _edt_attr(tag: str, name: str, types=()) -> str:
    inner = "".join(f"<types>{t}</types>" for t in types)
    return f"<{tag}><name>{name}</name><type>{inner}</type></{tag}>"


def _rights(objects: dict[str, list[str]], flag: bool = False) -> str:
    """Файл прав роли (одна форма у CF ``Ext/Rights.xml`` и EDT ``Rights.rights``)."""
    body = "".join(
        f"<object><name>{obj}</name>"
        + "".join(f"<right><name>{r}</name><value>true</value></right>" for r in rights)
        + "</object>"
        for obj, rights in objects.items()
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n<Rights xmlns="http://v8.1c.ru/8.2/roles">'
        f"<setForNewObjects>{'true' if flag else 'false'}</setForNewObjects>"
        f"<setForAttributesByDefault>true</setForAttributesByDefault>{body}</Rights>\n"
    )


def _cf_form(attr: str, cf_type: str) -> str:
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>\n<Form {_CF_FORM_NS} version="2.20"><Attributes>'
        f'<Attribute name="{attr}" id="1"><Type>{cf_type}</Type></Attribute></Attributes></Form>'
    )


def _edt_form(attr: str, token: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n<form:Form xmlns:form="http://g5.1c.ru/v8/dt/form" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        f"<attributes><name>{attr}</name><valueType><types>{token}</types></valueType></attributes></form:Form>"
    )


_DOC_OBJECT_MODULE = (
    "Процедура ПередЗаписью(Отказ)\n"
    "КонецПроцедуры\n\n"
    "Процедура ОбработкаПроведения(Отказ, Режим)\n"
    "\tДвижения.Остатки.Записывать = Истина;\n"
    "КонецПроцедуры\n"
)
_PRINT_MODULE = (
    "Процедура ДобавитьКомандыПечати(КомандыПечати) Экспорт\n"
    "\tКомандаПечати = КомандыПечати.Добавить();\n"
    '\tКомандаПечати.Идентификатор = "Накладная";\n'
    "\tКомандаПечати.Представление = НСтр(\"ru = 'Накладная'\");\n"
    "КонецПроцедуры\n"
)
# Цепочка вызовов общего модуля `ОбщМод`: А1 → А2 → А3 → А4 → А5 → А6 (5 рёбер).
_CHAIN_MODULE = "\n".join(
    f"Процедура А{i}() Экспорт\n" + (f"\tА{i + 1}();\n" if i < 6 else "") + "КонецПроцедуры\n" for i in range(1, 7)
)
# Общий модуль `ОбщМод2`: цикл `Ц1 ⇄ Ц2` и цепочка `Б1 → … → Б10` (9 рёбер — длиннее предела глубины 8).
_SECOND_MODULE = (
    "Процедура Вторая() Экспорт\nКонецПроцедуры\n\n"
    "Процедура Ц1() Экспорт\n\tЦ2();\nКонецПроцедуры\n\n"
    "Процедура Ц2() Экспорт\n\tЦ1();\nКонецПроцедуры\n\n"
    + "\n".join(
        f"Процедура Б{i}() Экспорт\n" + (f"\tБ{i + 1}();\n" if i < 10 else "") + "КонецПроцедуры\n"
        for i in range(1, 11)
    )
)
# Перехват модуля объекта `Док` в расширении: строка аннотации, строка комментария, объявление.
_EXT_DOC_MODULE = '&После("ПередЗаписью")\n// Перехват записи\nПроцедура Расш1_ПередЗаписью(Отказ)\nКонецПроцедуры\n'
_ENUM_OPS = [f"Операция{i:02d}" for i in range(1, 61)]  # 60 значений — больше предела раскрытия
_ENUM_RATES = ["БезНДС", "НДС10", "НДС20"]


def _enum_extra(values: list[str]) -> dict:
    return {
        "cf_children": "<ChildObjects>"
        + "".join(f"<EnumValue><Properties><Name>{v}</Name></Properties></EnumValue>" for v in values)
        + "</ChildObjects>",
        "edt": "".join(f"<enumValues><name>{v}</name></enumValues>" for v in values),
    }


# 17 категорий вне прежнего разбора аргумента — по одному объекту.
EXTRA_OBJECTS = [
    ("EventSubscriptions", "ПодпискаДок"),
    ("ScheduledJobs", "Задание1"),
    ("FunctionalOptions", "ФО1"),
    ("CommonPictures", "Картинка"),
    ("Styles", "Стиль1"),
    ("StyleItems", "Элемент1"),
    ("Languages", "Русский"),
    ("SessionParameters", "ТекущийПользователь"),
    ("CommonAttributes", "ОбщРекв"),
    ("Sequences", "Посл1"),
    ("DocumentNumerators", "Нум1"),
    ("CommandGroups", "Группа1"),
    ("Interfaces", "Интерфейс1"),
    ("Bots", "Бот1"),
    ("IntegrationServices", "Сервис1"),
    ("FunctionalOptionsParameters", "ПарФО1"),
    ("WSReferences", "WS1"),
]
_EXTRA_XML = {
    ("EventSubscriptions", "ПодпискаДок"): {
        "cf_props": "<Source><v8:Type>cfg:DocumentObject.Док</v8:Type></Source><Event>BeforeWrite</Event>"
        "<Handler>CommonModule.ОбщМод.А1</Handler>",
        "edt": "<source><types>DocumentObject.Док</types></source><event>BeforeWrite</event>"
        "<handler>CommonModule.ОбщМод.А1</handler>",
    },
    ("CommonPictures", "Картинка"): {"cf_props": _cf_syn("Картинка теста"), "edt": _edt_syn("Картинка теста")},
}

_WORLD49_MAIN = [
    (
        "Documents",
        "Док",
        {
            "Ext/ObjectModule.bsl": _DOC_OBJECT_MODULE,
            "Ext/ManagerModule.bsl": _PRINT_MODULE,
            "Forms/ФормаДокумента/Ext/Form.xml": (
                _cf_form("РеквизитАналитики", f"<v8:TypeSet>cfg:{CHAR}</v8:TypeSet>"),
                _edt_form("РеквизитАналитики", CHAR),
            ),
            "Forms/ФормаДокумента/Ext/Form/Module.bsl": "Процедура ПриСозданииНаСервере(Отказ, СтандартнаяОбработка)\n"
            "КонецПроцедуры\n",
        },
        {
            "cf_props": '<Posting>Allow</Posting><RegisterRecords><xr:Item xsi:type="xr:MDObjectRef">'
            "AccumulationRegister.Остатки</xr:Item></RegisterRecords>",
            "cf_children": "<ChildObjects>"
            + _cf_attr("Статья", sets=[f"cfg:{CHAR}"])
            + _cf_attr("Вид", types=["cfg:EnumRef.ВидыОпераций"])
            + _cf_attr("Ставка", types=["cfg:EnumRef.СтавкиНДС"])
            + _cf_attr("Спр", types=["cfg:CatalogRef.Спр1"])
            + "<TabularSection><Properties><Name>Товары</Name></Properties><ChildObjects>"
            + _cf_attr("Аналитика", sets=[f"cfg:{CHAR}"])
            + "</ChildObjects></TabularSection><Form>ФормаДокумента</Form></ChildObjects>",
            "edt": "<posting>Allow</posting><registerRecords>AccumulationRegister.Остатки</registerRecords>"
            + _edt_attr("attributes", "Статья", [CHAR])
            + _edt_attr("attributes", "Вид", ["EnumRef.ВидыОпераций"])
            + _edt_attr("attributes", "Ставка", ["EnumRef.СтавкиНДС"])
            + _edt_attr("attributes", "Спр", ["CatalogRef.Спр1"])
            + "<tabularSections><name>Товары</name>"
            + _edt_attr("attributes", "Аналитика", [CHAR])
            + "</tabularSections><forms><name>ФормаДокумента</name></forms>",
        },
    ),
    (
        "Catalogs",
        "ДокВложения",
        {"Ext/ObjectModule.bsl": PROC},
        {
            "cf_children": "<ChildObjects>" + _cf_attr("Ставка", types=["cfg:EnumRef.СтавкиНДС"]) + "</ChildObjects>",
            "edt": _edt_attr("attributes", "Ставка", ["EnumRef.СтавкиНДС"]),
        },
    ),
    ("Catalogs", "Док", {}, {}),  # справочник-омоним документа — только описатель
    (
        "Catalogs",
        "Спр1",
        {"Ext/ObjectModule.bsl": PROC},
        {
            "cf_children": "<ChildObjects>" + _cf_attr("Ссылка2", types=["cfg:CatalogRef.Спр2"]) + "</ChildObjects>",
            "edt": _edt_attr("attributes", "Ссылка2", ["CatalogRef.Спр2"]),
        },
    ),
    ("Catalogs", "Спр2", {}, {}),
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
            "cf_props": '<Content><xr:Item xsi:type="xr:MDObjectRef">Catalog.Спр1</xr:Item></Content>',
            "edt": "<content>Catalog.Спр1</content>",
        },
    ),
    ("Enums", "ВидыОпераций", {}, _enum_extra(_ENUM_OPS)),
    ("Enums", "СтавкиНДС", {}, _enum_extra(_ENUM_RATES)),
    (
        "ChartsOfCharacteristicTypes",
        "ВидыАналитик",
        {},
        {
            "cf_props": "<Type><v8:Type>cfg:CatalogRef.Спр2</v8:Type></Type>",
            "edt": "<type><types>CatalogRef.Спр2</types></type>",
        },
    ),
    ("AccumulationRegisters", "Остатки", {}, {}),
    (
        "DefinedTypes",
        "ОпрТип",
        {},
        {
            "cf_props": "<Type><v8:Type>cfg:CatalogRef.Спр1</v8:Type></Type>",
            "edt": "<type><types>CatalogRef.Спр1</types></type>",
        },
    ),
    ("CommonModules", "ОбщМод", {"Ext/Module.bsl": _CHAIN_MODULE}, {}),
    ("CommonModules", "ОбщМод2", {"Ext/Module.bsl": _SECOND_MODULE}, {}),
    ("CommonModules", "Стиль1", {"Ext/Module.bsl": "Процедура СтильПроц() Экспорт\nКонецПроцедуры\n"}, {}),
    ("CommonCommands", "ТекОрг", {}, {}),
    ("SessionParameters", "ТекОрг", {}, {}),
    ("CommonForms", "ТекОрг", {}, {}),
    ("CommonForms", "ОбщФорма", {}, {}),  # общая форма без модуля
    ("CommonPictures", "Спр1", {}, {}),  # омоним справочника `Спр1`
    ("Roles", "ПолныеПрава", {"Ext/Rights.xml": _rights({}, flag=True)}, {}),
    (
        "Roles",
        "Менеджер",
        {
            "Ext/Rights.xml": _rights(
                {
                    "Catalog.ДокВложения": ["Read", "Insert"],
                    "Document.Док": ["Read", "Update", "Posting"],
                    "Document.Док.Command.Печать": ["View"],
                    "Catalog.Док": ["Read"],
                }
            )
        },
        {},
    ),
    ("Roles", "ЧтецВложений", {"Ext/Rights.xml": _rights({"Catalog.ДокВложения": ["Read"]})}, {}),
    (
        "Roles",
        "БазовыеПрава",
        {
            "Ext/Rights.xml": _rights(
                {"CommonCommand.ТекОрг": ["View"], "SessionParameter.ТекОрг": ["Get"], "CommonForm.ТекОрг": ["View"]}
            )
        },
        {},
    ),
    *[(cat, name, {}, _EXTRA_XML.get((cat, name), {})) for cat, name in EXTRA_OBJECTS],
]
# Расширение `Расш1`: собственная общая картинка (с синонимом) и заимствованный `Док` с перехватом записи.
_WORLD49_EXT = [
    (
        "CommonPictures",
        "КартинкаРасш",
        {},
        {"cf_props": _cf_syn("Картинка расширения"), "edt": _edt_syn("Картинка расширения")},
    ),
    (
        "Documents",
        "Док",
        {"Ext/ObjectModule.bsl": _EXT_DOC_MODULE},
        {"cf_props": "<ObjectBelonging>Adopted</ObjectBelonging>", "edt": "<objectBelonging>Adopted</objectBelonging>"},
    ),
]


class World49(dict):
    """Словарь хелперов сессии плюс раскладка: ``fmt``, ``route``, ``root`` (корень основной), ``ext_root``,
    ``reader``, ``db`` (путь индекса или ``None``)."""

    fmt: str
    route: str
    root: Path
    ext_root: Path
    reader: IndexReader | None
    db: Path | None


def _world49_helpers(root: Path, ext_root: Path, reader: IndexReader | None) -> dict:
    return _bsl_for(
        root,
        reader,
        idx_zero_callers_authoritative=reader is not None,
        extension_paths=[str(ext_root)],
        current_config_role="main",
        current_config_name="Тест",
        current_config_root=str(root),
        extension_name_by_root={str(ext_root): "Расш1"},
    )


def _build_world49(base: Path, variant: str, after_build=None) -> World49:
    """Мир релиза в ``base``: основная конфигурация ``base/cf`` и расширение ``base/cfe/Расш1``. ``after_build(root,
    fmt)`` — правка дерева после сборки индекса и до открытия сессии (модуль, добавленный после сборки)."""
    fmt, route = variant.split("-", 1)
    root = _objects_tree(base / "cf", fmt, _WORLD49_MAIN)
    ext_root = _objects_tree(
        base / "cfe" / "Расш1", fmt, _WORLD49_EXT, CF_EXT_DESCRIPTOR if fmt == "cf" else EDT_EXT_DESCRIPTOR
    )
    db = _build_index(root, base / "idx") if route == "index" else None
    if after_build is not None:
        after_build(root, fmt)
    reader = IndexReader(db) if db is not None else None
    world = World49(_world49_helpers(root, ext_root, reader))
    world.fmt, world.route, world.root, world.ext_root, world.reader, world.db = fmt, route, root, ext_root, reader, db
    return world


def _add_after_build(root: Path, fmt: str) -> list[str]:
    """Модули, добавленные после сборки индекса: модуль менеджера `Спр1`, форма `НоваяФорма` с модулем у `Док` и
    модуль общей формы `ОбщФорма`. Возвращает их пути от корня."""
    cf = fmt == "cf"
    rels = [
        "Catalogs/Спр1/Ext/ManagerModule.bsl" if cf else "Catalogs/Спр1/ManagerModule.bsl",
        "Documents/Док/Forms/НоваяФорма/Ext/Form/Module.bsl" if cf else "Documents/Док/Forms/НоваяФорма/Module.bsl",
        "CommonForms/ОбщФорма/Ext/Form/Module.bsl" if cf else "CommonForms/ОбщФорма/Module.bsl",
    ]
    _write(root / rels[0], _PRINT_MODULE)
    _write(root / rels[1], "Процедура НоваяПроцедура() Экспорт\nКонецПроцедуры\n")
    _write(root / rels[2], "Процедура ПроцедураОбщейФормы() Экспорт\nКонецПроцедуры\n")
    if cf:
        _write(root / "Documents/Док/Forms/НоваяФорма.xml", _cf_md("Form", "НоваяФорма"))
        _write(root / "Documents/Док/Forms/НоваяФорма/Ext/Form.xml", _cf_form("Р", "<v8:Type>xs:string</v8:Type>"))
    else:
        _write(root / "Documents/Док/Forms/НоваяФорма/Form.form", _edt_form("Р", "String"))
    return rels


WORLD49 = ["cf-index", "cf-live", "edt-index", "edt-live"]


@pytest.fixture(scope="module", params=WORLD49)
def world49(request, tmp_path_factory):
    """Мир релиза: ``(bsl, root, route, fmt)``; ``bsl`` — :class:`World49` (атрибуты ``reader``, ``db``…)."""
    built = _build_world49(tmp_path_factory.mktemp("world49"), request.param)
    yield built, built.root, built.route, built.fmt
    if built.reader is not None:
        built.reader.close()


def test_world49_builds_both_layouts(world49):
    bsl, root, route, fmt = world49
    paths = [m["path"] for m in bsl["find_module"]("Док")]
    assert _obj_file(fmt, "Documents", "Док", "Ext/ObjectModule.bsl") in paths, paths
    ext = bsl["detect_extensions"]()
    assert [e["name"] for e in ext["nearby_extensions"]] == ["Расш1"], ext
    pic = bsl["parse_object_xml"]("CommonPictures/Картинка")
    assert pic.get("name") == "Картинка", pic
    assert (bsl.reader is not None) == (route == "index")
    assert os.path.isdir(root / "CommonPictures")


# ═══════════════ Задача 1 (Д2, П2) — путь и каталог с точкой для 17 категорий ═══════════════

# Формы ссылки: путь, каталог с точкой, путь каталога в нижнем регистре, каталог с пробелом и точкой.
_EXTRA_FORMS = ["{f}/{n}", "{f}.{n}", "{lf}/{n}", "{sf}.{n}"]


def _extra_arg(form: str, folder: str, name: str) -> str:
    return form.format(f=folder, n=name, lf=folder.lower(), sf=folder[:3] + " " + folder[3:])


class TestDescriptorCategoryRefs:
    @pytest.mark.parametrize("folder,name", EXTRA_OBJECTS)
    @pytest.mark.parametrize("form", _EXTRA_FORMS)
    def test_path_and_dot_pick_the_category_object(self, world49, folder, name, form):
        bsl = world49[0]
        arg = _extra_arg(form, folder, name)
        card = bsl["analyze_object"](arg)
        assert card.get("category") == folder and card["modules"] == [], card
        mods = bsl["get_object_modules"](arg)
        assert mods["error"].startswith(f"У объекта {folder}/{name} нет модулей"), mods
        if name != "Стиль1":  # у объекта без омонима — тот же ответ, что у голого имени
            assert _strip_times(mods) == _strip_times(bsl["get_object_modules"](name))

    def test_homonym_bare_name_keeps_previous_answer(self, world49):
        """Обе версии: голое `Стиль1` (омоним — общий модуль с модулем) по-прежнему отвечает общим модулем; ссылка
        `Styles/Стиль1` выбирает объект своей категории."""
        bsl = world49[0]
        assert bsl["get_object_modules"]("Стиль1")["category"] == "CommonModules"
        assert bsl["get_object_modules"]("Styles/Стиль1")["error"].startswith("У объекта Styles/Стиль1 нет модулей")

    @pytest.mark.parametrize("folder,name", EXTRA_OBJECTS)
    @pytest.mark.parametrize(
        "helper",
        [
            "analyze_object",
            "find_custom_modifications",
            "find_print_forms",
            "get_object_full_structure",
            "get_object_profile",
        ],
    )
    def test_no_value_error_for_extra_categories(self, world49, folder, name, helper):
        got = _call(world49[0], helper, f"{folder}/{name}")
        assert not (isinstance(got, tuple) and got and got[0] == "raises" and "неизвест" in got[2]), got

    def test_references_get_canonical_head(self, world49):
        res = world49[0]["find_references_to_object"]("CommonPictures/Картинка")
        assert res["object"] == "CommonPicture.Картинка", res
        # v1.50.0: прежде здесь стоял `target_not_indexed is True` — с v19 граф хранит цели общих картинок (состав
        # подсистем, права ролей), и живой разбор состава их видит: признак — только у индекса прежнего поколения.
        assert "target_not_indexed" not in res["_meta"] and "hint" not in res, res
        assert world49[0]["find_code_usages"]("CommonPictures.Картинка")["object"] == "CommonPicture.Картинка"

    @pytest.mark.parametrize(
        "helper,raw", [("analyze_object", "CommonPictures/"), ("get_object_modules", "CommonPictures.")]
    )
    def test_nameless_extra_category_is_refused(self, world49, helper, raw):
        with pytest.raises(ValueError, match=f"^{helper}: аргумент без имени объекта"):
            world49[0][helper](raw)

    def test_missing_extra_category_object_is_refused(self, world49):
        with pytest.raises(ValueError, match="^find_references_to_object: объекта CommonPicture.Нет нет"):
            world49[0]["find_references_to_object"]("CommonPictures/Нет")

    def test_single_category_helper_refuses_extra_category(self, world49):
        bsl = world49[0]
        with pytest.raises(ValueError, match="^find_enum_values: .* объект категории CommonPictures") as exc:
            bsl["find_enum_values"]("CommonPictures/Картинка")
        route = str(exc.value).split("parse_object_xml(", 1)[1].split(")", 1)[0]
        assert bsl["parse_object_xml"](eval(route))["name"] == "Картинка"  # noqa: S307 — литерал из текста отказа

    # ── границы (обе версии) ──

    def test_singular_head_keeps_previous_parse(self, world49):
        # v1.50.0: прежде здесь стояло «не найден» у `ОбщаяКартинка.Картинка` и `Подсистема.Верх` — единственное число
        # 21 категории без головы в прежнем разборе — ссылка (П1): ответ тот же, что у пути каталога.
        bsl = world49[0]
        assert bsl["analyze_object"]("ОбщаяКартинка.Картинка") == bsl["analyze_object"]("CommonPictures/Картинка")
        assert bsl["analyze_object"]("Подсистема.Верх") == bsl["analyze_object"]("Subsystems/Верх")
        assert bsl["analyze_object"]("Подсистема.Верх")["category"] == "Subsystems"

    def test_ru_plural_path_keeps_previous_parse(self, world49):
        # v1.50.0: прежде здесь стояло «не найден» — русское множественное в пути 21 категории — ссылка (П1).
        bsl = world49[0]
        assert bsl["analyze_object"]("ОбщиеКартинки/Картинка") == bsl["analyze_object"]("CommonPictures/Картинка")
        assert bsl["analyze_object"]("ОбщиеКартинки/Картинка")["category"] == "CommonPictures"

    def test_bare_extra_category_word_unchanged(self, world49):
        bsl = world49[0]
        assert bsl["analyze_object"]("CommonPictures") == {"error": "Объект 'CommonPictures' не найден"}
        assert bsl["get_object_full_structure"]("Styles") == {
            "error": "Объект 'Styles' не найден",
            "_meta": {"index_used": False, "fallback_reason": "object_not_found", "ts_synonyms_available": False},
        }

    def test_typed_ref_consumers_unchanged(self, world49):
        bsl = world49[0]
        assert bsl["find_event_subscriptions"]("CommonPictures/Картинка") == []
        assert bsl["find_event_subscriptions"]("EventSubscriptions/ПодпискаДок") == []

    def test_parse_form_extra_category_ref(self, world49):
        """Ссылка 17 категорий у `parse_form` — строгая категория (путь и точка одинаково): объект без форм — `[]`, в том
        числе при омониме другой категории (`Стиль1` — и общий модуль); объекта нет — отказ шлюза."""
        bsl = world49[0]
        for arg in ("Styles.Стиль1", "Styles/Стиль1", "CommonPictures/Картинка", "CommonPictures.Картинка"):
            assert bsl["parse_form"](arg) == [], arg
        for arg in ("CommonPictures/Нет", "CommonPictures.Нет"):
            with pytest.raises(ValueError, match="^parse_form: объекта CommonPicture.Нет нет"):
                bsl["parse_form"](arg)


# ═══════════════ Задача 2 (Д3, П3) — карточка объекта без модулей в структуре и профиле ═══════════════

_CARD_REASON = "category_without_attributes_filled_via_live_xml"


def _profile_digest(prof: dict) -> dict:
    """Идентичность и статусы/сводки секций профиля (без полей времени и строк)."""
    return {
        "object_name": prof.get("object_name"),
        "category": prof.get("category"),
        "identity": (prof.get("_meta") or {}).get("identity_source"),
        "sections": {k: [v.get("status"), v.get("summary"), v.get("total")] for k, v in prof["sections"].items()},
    }


# Ответ HEAD `get_object_profile('Справочник.Спр1')` (индексный маршрут; живьём все секции unavailable).
_SPR1_PROFILE_INDEX = {
    "object_name": "Спр1",
    "category": "Catalogs",
    "identity": "index_prefix",
    "sections": {
        "structure": [
            "ok",
            {
                "posting": None,
                "attributes": 1,
                "tabular_sections": 0,
                "dimensions": 0,
                "resources": 0,
                "predefined_items": 0,
                "forms": 0,
            },
            1,
        ],
        "modules": ["ok", {"modules": 1, "methods": 1, "exports": 1, "overrides": 0, "extension_modules": 0}, 1],
        "registers": ["skipped", {}, 0],
        "subscriptions": ["empty", {"subscriptions": 0, "exact": 0, "set": 0, "universal": 0}, 0],
        "roles": ["ok", {"roles": 1, "flag_covered": 1}, 1],
        "functional_options": ["empty", {"functional_options": 0}, 0],
    },
}


class TestDescriptorOnlyCard:
    @pytest.mark.parametrize("arg", ["Картинка", "CommonPictures/Картинка", "CommonPictures.Картинка"])
    def test_full_structure_card_for_descriptor_only(self, world49, arg):
        res = world49[0]["get_object_full_structure"](arg)
        assert res.get("category") == "CommonPictures" and res["object_name"] == "Картинка", res
        assert res["synonym"] == "Картинка теста" and res["attributes"] == [], res
        assert res["_meta"]["index_used"] is False and res["_meta"]["fallback_reason"] == _CARD_REASON, res["_meta"]

    def test_profile_card_for_descriptor_only(self, world49):
        bsl, _root, route, _fmt = world49
        prof = bsl["get_object_profile"]("Картинка")
        if route == "live":
            # граница: без индекса профиль голого имени по-прежнему требует префикс
            assert prof["error"] == "no_index_identity_unresolved", prof
            return
        assert prof["category"] == "CommonPictures" and prof["object_name"] == "Картинка", prof
        assert prof["_meta"]["identity_source"] == "descriptor", prof["_meta"]
        assert prof["sections"]["structure"]["status"] == "ok", prof["sections"]["structure"]
        assert prof["sections"]["modules"]["status"] == "empty", prof["sections"]["modules"]

    @pytest.mark.parametrize("name", ["Задание1", "ПодпискаДок", "ФО1"])
    def test_profile_structure_of_job_subscription_fo(self, world49, name):
        """Секция structure профиля регламентного задания, подписки и ФО: прежде `status='error'` («category=…
        неизвестна» — поиск модулей не знал их категорий), теперь — структура объекта."""
        bsl, _root, route, _fmt = world49
        if route == "live":
            pytest.skip("без индекса профиль голого имени требует префикс")
        sec = bsl["get_object_profile"](name, sections=["structure"])["sections"]["structure"]
        assert sec["status"] == "ok", sec

    def test_profile_card_has_no_flag_roles(self, world49):
        bsl, _root, route, _fmt = world49
        if route == "live":
            pytest.skip("без индекса секция roles недоступна")
        assert bsl["get_object_profile"]("Картинка")["sections"]["roles"]["summary"]["flag_covered"] == 0

    def test_extension_object_card(self, world49):
        res = world49[0]["get_object_full_structure"]("КартинкаРасш")
        assert res.get("category") == "CommonPictures" and res["object_name"] == "КартинкаРасш", res
        assert res["synonym"] == "Картинка расширения", res
        assert not str(res["_meta"].get("fallback_reason") or "").startswith("parse_failed"), res["_meta"]

    def test_profile_card_beats_other_category_homonym(self, world49):
        """Стиль `Стиль1` и общий модуль `Стиль1` с модулем: ссылка стиля — карточка стиля, а не омоним."""
        bsl, _root, route, _fmt = world49
        for arg in ("Styles/Стиль1", "Styles.Стиль1"):
            prof = bsl["get_object_profile"](arg)
            assert prof["category"] == "Styles" and prof["object_name"] == "Стиль1", prof
            if route == "index":
                assert prof["_meta"]["identity_source"] == "descriptor", prof["_meta"]
                assert prof["sections"]["modules"]["status"] == "empty", prof["sections"]["modules"]
            else:
                assert prof["_meta"]["identity_source"] == "input_prefix", prof["_meta"]
        st = bsl["get_object_full_structure"]("Styles/Стиль1")
        assert st["category"] == "Styles" and st["_meta"]["fallback_reason"] == _CARD_REASON, st
        # обе версии: префикс общего модуля и голое имя — общий модуль, как на HEAD
        prof = bsl["get_object_profile"]("ОбщийМодуль.Стиль1")
        assert prof["category"] == "CommonModules", prof
        assert prof["_meta"]["identity_source"] == ("index_prefix" if route == "index" else "input_prefix")
        bare = bsl["get_object_profile"]("Стиль1")
        if route == "index":
            assert bare["category"] == "CommonModules" and bare["_meta"]["identity_source"] == "index", bare
        else:
            assert bare["error"] == "no_index_identity_unresolved", bare

    def test_profile_structure_keeps_card_identity(self, world49):
        """Общая картинка `Спр1` при справочнике `Спр1` с реквизитом: структура карточки не берётся у справочника."""
        bsl, _root, route, _fmt = world49
        st = bsl["get_object_full_structure"]("Спр1", category_hint="CommonPictures")
        assert st["category"] == "CommonPictures" and st["attributes"] == [], st
        assert st["_meta"]["fallback_reason"] == _CARD_REASON, st["_meta"]
        prof = bsl["get_object_profile"]("CommonPictures/Спр1", sections=["structure"])
        assert prof["category"] == "CommonPictures", prof
        if route == "index":
            sec = prof["sections"]["structure"]
            assert sec["status"] == "ok" and sec["_meta"].get("identity_match") is not False, sec
            assert sec["summary"]["attributes"] == 0, sec
            # обе версии: справочник — префиксом и голым именем
            assert bsl["get_object_profile"]("Справочник.Спр1")["category"] == "Catalogs"
            bare = bsl["get_object_profile"]("Спр1")
            assert bare["category"] == "Catalogs" and bare["_meta"]["identity_source"] == "index", bare

    def test_fragment_keeps_previous_answer(self, world49):
        assert world49[0]["get_object_full_structure"]("Картин") == {
            "error": "Объект 'Картин' не найден",
            "_meta": {"index_used": False, "fallback_reason": "object_not_found", "ts_synonyms_available": False},
        }

    def test_resolved_objects_unchanged(self, world49):
        bsl, _root, route, _fmt = world49
        res = _without(_strip_times(bsl["get_object_full_structure"]("Справочник.Спр2")), {"enum_values_truncated"})
        assert res == {
            "object_name": "Спр2",
            "category": "Catalogs",
            "synonym": None,
            "posting": None,
            "attributes": [],
            "tabular_sections": [],
            "dimensions": [],
            "resources": [],
            "predefined_items": [],
            "enum_values_for_typed_refs": {},
            "forms": [],
            "templates": [],
            "value_type": None,
            "_meta": {
                "index_used": False,
                "fallback_reason": "index_empty_for_object"
                if route == "index"
                else "index_unavailable_or_table_missing",
                "ts_synonyms_available": False,
            },
        }
        prof = _profile_digest(bsl["get_object_profile"]("Справочник.Спр1"))
        if route == "index":
            assert prof == _SPR1_PROFILE_INDEX
        else:
            assert prof["identity"] == "input_prefix" and {v[0] for v in prof["sections"].values()} == {"unavailable"}


# ═══════════════ Задача 3 (Д10, П6) — роли «для новых объектов» только у видов с правами ═══════════════

# Перепись `role_rights` 12 боевых индексов (раздел 3 плана, Д10): головы видов, на которые в ролях бывают права.
_PLATFORM_RIGHT_HEADS = {
    "AccountingRegister",
    "AccumulationRegister",
    "BusinessProcess",
    "CalculationRegister",
    "Catalog",
    "ChartOfAccounts",
    "ChartOfCalculationTypes",
    "ChartOfCharacteristicTypes",
    "CommonAttribute",
    "CommonCommand",
    "CommonForm",
    "Constant",
    "DataProcessor",
    "Document",
    "DocumentJournal",
    "ExchangePlan",
    "ExternalDataSource",
    "FilterCriterion",
    "HTTPService",
    "InformationRegister",
    "IntegrationService",
    "Interface",
    "Report",
    "Sequence",
    "SessionParameter",
    "Subsystem",
    "Task",
    "WebService",
}


def _flag_roles(res: dict) -> list[str]:
    return [r["role_name"] for r in res["roles"] if r.get("granted_via") == "set_for_new_objects"]


class TestFlagRolesByKind:
    @pytest.mark.parametrize(
        "ref",
        [
            "ОпределяемыйТип.ОпрТип",
            "ОбщийМодуль.ОбщМод",
            "Перечисление.СтавкиНДС",
            "CommonPictures/Картинка",
            # v1.50.0: прежде здесь стояло «единственное число RU/EN — не ссылка, но объект строки — общая картинка»;
            # с v1.50.0 это ссылка на общую картинку (П1) — вид без прав, строк по флагу по-прежнему нет
            "ОбщаяКартинка.Картинка",
            "CommonPicture.Картинка",
        ],
    )
    def test_no_flag_rows_on_kinds_without_rights(self, world49, ref):
        res = world49[0]["find_references_to_object"](ref)
        assert res["by_kind"].get("role_rights", 0) == 0, res
        assert "role_rights" not in res["_meta"]["expanded_on_read"], res["_meta"]

    @pytest.mark.parametrize("arg", ["ОбщМод", "Картинка", "Role.ПолныеПрава", "ОбщийМодуль.ОбщМод", "Styles/Стиль1"])
    def test_find_roles_without_flag_rows_on_kinds_without_rights(self, world49, arg):
        res = world49[0]["find_roles"](arg)
        assert _flag_roles(res) == [], res

    def test_profile_roles_section_without_flag_rows(self, world49):
        bsl, _root, route, _fmt = world49
        if route == "live":
            pytest.skip("без индекса секция roles недоступна")
        for arg in ("ОбщийМодуль.ОбщМод", "Перечисление.СтавкиНДС"):
            assert bsl["get_object_profile"](arg)["sections"]["roles"]["summary"]["flag_covered"] == 0, arg

    @pytest.mark.parametrize(
        "arg", ["Документ.Док", "Справочник.Спр1", "Подсистема.Верх", "SessionParameters/ТекущийПользователь"]
    )
    def test_flag_rows_kept_on_kinds_with_rights(self, world49, arg):
        """Обе версии: виды с правами (документ, справочник, подсистема, параметр сеанса) — роль по флагу есть."""
        bsl, _root, route, _fmt = world49
        assert _flag_roles(bsl["find_roles"](arg)) == ["ПолныеПрава"], arg
        if route == "index" and arg != "Подсистема.Верх":
            refs = bsl["find_references_to_object"](arg)
            assert "Role.ПолныеПрава.SetForNewObjects" in {r["used_in"] for r in refs["references"]}, refs

    @pytest.mark.parametrize(
        "ref",
        [
            "ПараметрСеанса.ТекущийПользователь",
            "ОбщийРеквизит.ОбщРекв",
            "Последовательность.Посл1",
            "Интерфейс.Интерфейс1",
            "СервисИнтеграции.Сервис1",
        ],
    )
    def test_flag_rows_kept_for_singular_of_kinds_with_rights(self, world49, ref):
        """Обе версии: роль по флагу у вида с правами — у `find_references_to_object`, как у `find_roles` (ревью: сырая
        голова `canonical` роняла такие строки). v1.50.0: прежде здесь стояло «русское единственное число вида с
        правами ссылкой не канонизируется (граница разбора)» — с v1.50.0 оно ссылка (П1), ответ тот же."""
        bsl, _root, route, _fmt = world49
        assert _flag_roles(bsl["find_roles"](ref)) == ["ПолныеПрава"], ref
        if route == "index":
            refs = bsl["find_references_to_object"](ref)
            assert refs["by_kind"].get("role_rights") == 1, refs
            assert "Role.ПолныеПрава.SetForNewObjects" in {r["used_in"] for r in refs["references"]}, refs

    def test_stale_index_homonym_keeps_flag_role(self, tmp_path, monkeypatch):
        """Обе версии: документ `X` остался только в индексе (описатель удалён после сборки, до сессии), на диске —
        общий модуль `X`: голое имя — роль по флагу, как у ссылки и профиля (союз источников существования; ревью
        Codex: дешёвые источники с видом без прав отбрасывали вид с правами из индекса)."""
        root = _objects_tree(
            tmp_path / "cf",
            "cf",
            [
                (
                    "Documents",
                    "X",
                    {},
                    {"cf_children": "<ChildObjects>" + _cf_attr("A", ["xs:string"]) + "</ChildObjects>"},
                ),
                ("CommonModules", "X", {"Ext/Module.bsl": PROC}, {}),
                ("Roles", "ПолныеПрава", {"Ext/Rights.xml": _rights({}, flag=True)}, {}),
            ],
        )
        reader = _reader_for(root, monkeypatch)
        try:
            (root / "Documents" / "X.xml").unlink()
            # Удаление сразу после сборки укладывается в допуск дрейфа (2 с) — время каталога сдвигается явно.
            when = os.stat(root / "Documents").st_mtime + 60
            os.utime(root / "Documents", (when, when))
            bsl = _bsl_for(root, reader)
            assert _flag_roles(bsl["find_roles"]("Документ.X")) == ["ПолныеПрава"]
            assert _flag_roles(bsl["find_roles"]("X")) == ["ПолныеПрава"]
            assert bsl["get_object_profile"]("X", sections=["roles"])["sections"]["roles"]["summary"]["flag_covered"]
        finally:
            reader.close()

    def test_fresh_index_skips_index_union(self, world49, monkeypatch):
        """Свежий индекс (каталоги категорий после сборки не менялись): имя вида без прав не платит каскадом союза с
        индексом — союз нужен, только если индекс может знать объект, которого на диске нет (решение владельца)."""
        bsl, _root, route, _fmt = world49
        if route != "index":
            pytest.skip("союз с индексом — только на индексе")
        asked: list = []
        real = bsl.reader.get_object_attributes

        def spy(*args, **kwargs):
            asked.append(kwargs.get("object_name"))
            return real(*args, **kwargs)

        monkeypatch.setattr(bsl.reader, "get_object_attributes", spy)
        fresh = _world49_helpers(bsl.root, bsl.ext_root, bsl.reader)
        assert _flag_roles(fresh["find_roles"]("ОбщМод")) == []
        assert "ОбщМод" not in asked, asked

    def test_role_right_heads_match_platform_list(self):
        from rlm_tools_bsl.bsl_helpers import _ROLE_RIGHT_HEADS, _kind_has_role_rights

        assert set(_ROLE_RIGHT_HEADS) == _PLATFORM_RIGHT_HEADS and len(_ROLE_RIGHT_HEADS) == 28
        assert _kind_has_role_rights("Document") and not _kind_has_role_rights("CommonModule")
        assert not _kind_has_role_rights("Bot") and not _kind_has_role_rights("Configuration")


# ═══════════════ Задача 4 (Д9, П5) — вид ссылки characteristic_value_type ═══════════════

_CVT = "characteristic_value_type"
# v1.50.0: виды, на которые растёт закрытый набор `kinds` (индекс — `kinds_applied`; живьём — поддержанные и
# `unsupported_kinds`); у индекса до v19 они — в `kinds_unavailable` (объединение поколений).
_V150_INDEX_KINDS = {"common_attribute_content", "template_query"}
_V150_LIVE_KINDS = {"common_attribute_content"}
_V150_LIVE_UNSUPPORTED = {"template_query"}
_CVT_ATTR_ROWS = {
    "Document.Док.Attribute.Статья.Type",
    "Document.Док.TabularSection.Товары.Attribute.Аналитика.Type",
}
_CVT_FORM_ROW = "Document.Док.Form.ФормаДокумента.Attribute.РеквизитАналитики.Type"
# Ответ HEAD `find_references_to_object` при `kinds=None`: применённые виды (индекс и живьём), неподдержанные живьём,
# виды v18 (недоступны на индексе v17).
_HEAD_APPLIED_INDEX = [
    "attribute_type",
    "based_on",
    "characteristic_type",
    "choice_parameter_link",
    "command_parameter_type",
    "default_list_form",
    "default_object_form",
    "defined_type_content",
    "document_journal_content",
    "event_subscription_source",
    "exchange_plan_content",
    "filter_criterion_content",
    "filter_criterion_type",
    "form_attribute_type",
    "form_main_table",
    "form_query",
    "functional_option_content",
    "link_by_type",
    "linked_object",
    "list_form",
    "main_form",
    "owner",
    "predefined_characteristic_type",
    "role_rights",
    "subsystem_content",
]
_HEAD_APPLIED_LIVE = [
    "attribute_type",
    "based_on",
    "characteristic_type",
    "command_parameter_type",
    "default_list_form",
    "default_object_form",
    "defined_type_content",
    "document_journal_content",
    "exchange_plan_content",
    "filter_criterion_content",
    "filter_criterion_type",
    "linked_object",
    "list_form",
    "main_form",
    "owner",
    "subsystem_content",
]
_HEAD_UNSUPPORTED_LIVE = [
    "choice_parameter_link",
    "event_subscription_source",
    "form_attribute_type",
    "form_main_table",
    "form_query",
    "functional_option_content",
    "link_by_type",
    "predefined_characteristic_type",
    "role_rights",
]
_HEAD_V18_KINDS = [
    "document_journal_content",
    "filter_criterion_content",
    "filter_criterion_type",
    "form_attribute_type",
    "form_main_table",
    "form_query",
    "linked_object",
]


class TestCharacteristicValueRefs:
    @pytest.mark.parametrize("ref", ["ПланВидовХарактеристик.ВидыАналитик", "ПланВидовХарактеристик.видыаналитик"])
    def test_pvh_value_type_refs(self, world49, ref):
        bsl, _root, route, _fmt = world49
        res = bsl["find_references_to_object"](ref)
        rows = [r for r in res["references"] if r["kind"] == _CVT]
        expected = _CVT_ATTR_ROWS | ({_CVT_FORM_ROW} if route == "index" else set())
        assert {r["used_in"] for r in rows} == expected, res
        assert all(r["line"] is None for r in rows), rows
        assert res["by_kind"][_CVT] == len(rows) == len(expected)
        assert res["_meta"]["expanded_on_read"].get(_CVT) == len(rows), res["_meta"]
        assert _CVT in res["_meta"]["kinds_applied"], res["_meta"]
        only = bsl["find_references_to_object"](ref, kinds=[_CVT])
        assert {r["kind"] for r in only["references"]} == {_CVT}, only
        assert {r["used_in"] for r in only["references"]} == expected
        assert only["total"] == len(expected) and only["_meta"]["kinds_applied"] == [_CVT]

    def test_pvh_value_type_paths(self, world49):
        bsl, _root, route, fmt = world49
        res = bsl["find_references_to_object"]("ПланВидовХарактеристик.ВидыАналитик", kinds=[_CVT])
        paths = {r["used_in"]: r["path"] for r in res["references"]}
        descriptor = _descriptor_rel(fmt, "Documents", "Док")
        assert paths["Document.Док.Attribute.Статья.Type"] == descriptor, paths
        if route == "index":
            form = "Documents/Док/Forms/ФормаДокумента/" + ("Ext/Form.xml" if fmt == "cf" else "Form.form")
            assert paths[_CVT_FORM_ROW] == form, paths

    def test_data_path_refuses_value_type_kind(self, world49):
        with pytest.raises(ValueError, match="^find_data_path: вид characteristic_value_type"):
            world49[0]["find_data_path"]("Документ.Док", "ПланВидовХарактеристик.ВидыАналитик", kinds=[_CVT])
        with pytest.raises(ValueError, match="^find_data_path: вид characteristic_value_type"):
            world49[0]["find_data_path"]("Документ.Док", "Справочник.Спр2", kinds=["attribute_type", _CVT])

    def test_other_targets_unchanged(self, world49, tmp_path):
        """У цели не-ПВХ ответ HEAD во всех полях, кроме `_meta.kinds_applied` (+`characteristic_value_type`)."""
        bsl, _root, route, fmt = world49
        res = bsl["find_references_to_object"]("Справочник.Спр2")
        rows = [
            ("Catalog.Спр1.Attribute.Ссылка2.Type", _descriptor_rel(fmt, "Catalogs", "Спр1"), "attribute_type"),
            (
                "ChartOfCharacteristicTypes.ВидыАналитик.Type",
                _descriptor_rel(fmt, "ChartsOfCharacteristicTypes", "ВидыАналитик"),
                "characteristic_type",
            ),
        ]
        if route == "index":
            rights = _obj_file(fmt, "Roles", "ПолныеПрава", "Ext/Rights.xml")
            rows.insert(1, ("Role.ПолныеПрава.SetForNewObjects", rights, "role_rights"))
        assert [(r["used_in"], r["path"], r["kind"]) for r in res["references"]] == rows, res
        assert all(r["line"] is None for r in res["references"])
        assert res["total"] == len(rows) and res["truncated"] is False and res["partial"] is (route == "live")
        assert res["by_kind"] == {kind: 1 for _u, _p, kind in rows}
        meta = res["_meta"]
        assert meta["expanded_on_read"] == ({"role_rights": 1} if route == "index" else {}), meta
        assert meta["kinds_unavailable"] == [] and meta["kinds_requested"] == []
        # v1.50.0: прежде здесь вычитался только `characteristic_value_type` — набор видов вырос ещё на виды v1.50.0.
        new_kinds = _V150_INDEX_KINDS if route == "index" else _V150_LIVE_KINDS
        assert sorted(set(meta["kinds_applied"]) - {_CVT} - new_kinds) == (
            _HEAD_APPLIED_INDEX if route == "index" else _HEAD_APPLIED_LIVE
        )
        assert new_kinds <= set(meta["kinds_applied"])
        assert _CVT in meta["kinds_applied"] and meta["kinds_applied"] == sorted(meta["kinds_applied"])
        assert meta["unsupported_kinds"] == (
            [] if route == "index" else sorted({*_HEAD_UNSUPPORTED_LIVE, *_V150_LIVE_UNSUPPORTED})
        )
        if route != "index":
            return
        # Индекс прежнего поколения: те же строки, вид — в `kinds_unavailable` (как прочие виды v18).
        reader = IndexReader(_downgrade_copy(bsl.db, tmp_path))
        try:
            helpers = _world49_helpers(bsl.root, bsl.ext_root, reader)
            old = helpers["find_references_to_object"]("Справочник.Спр2")
            assert [(r["used_in"], r["path"], r["kind"]) for r in old["references"]] == rows, old
            # v1.50.0: прежде здесь стояли только виды v18 — у индекса до v18 нет и видов v19 (объединение поколений).
            assert old["_meta"]["kinds_unavailable"] == sorted({*_HEAD_V18_KINDS, _CVT, *_V150_INDEX_KINDS}), old[
                "_meta"
            ]
        finally:
            reader.close()

    def test_old_generation_index_gives_no_rows_of_unavailable_kind(self, world49, tmp_path):
        """Индекс прежнего поколения (v17): вид объявлен недоступным (`kinds_unavailable`) — и строк его в том же ответе
        нет (ревью: раскрытие на чтении шло мимо поколения — строки и признак «индекс вид не производит» вместе)."""
        bsl, _root, route, _fmt = world49
        if route != "index":
            pytest.skip("поколение — свойство индекса")
        target = "ПланВидовХарактеристик.ВидыАналитик"
        assert bsl["find_references_to_object"](target)["by_kind"].get(_CVT), "на v18 строки вида есть"
        reader = IndexReader(_downgrade_copy(bsl.db, tmp_path))
        try:
            old = _world49_helpers(bsl.root, bsl.ext_root, reader)["find_references_to_object"](target)
        finally:
            reader.close()
        assert _CVT in old["_meta"]["kinds_unavailable"], old["_meta"]
        assert _CVT not in old["by_kind"] and all(r["kind"] != _CVT for r in old["references"]), old
        assert _CVT not in old["_meta"]["expanded_on_read"], old["_meta"]

    def test_reader_method_is_transient_safe(self):
        """Новый прямой вызов ридера из хелперов — метод с `@_transient_safe` (AST-гард 1.43.0)."""
        assert hasattr(IndexReader.find_characteristic_value_refs, "__wrapped__")


def _downgrade_copy(db, dst_dir: Path) -> Path:
    copy = _db_copy(db, dst_dir / "bsl_index_v17.db")
    _downgrade_to_v17(copy)
    return copy


# ═══════════════ Задача 5 (Д4, П4) — find_roles: object и rights — одна пара ═══════════════


def _explicit_role(res: dict, role: str) -> dict:
    rows = [r for r in res["roles"] if r["role_name"] == role and r.get("granted_via") != "set_for_new_objects"]
    assert len(rows) == 1, res
    return rows[0]


class TestRolesObjectRightsPair:
    def test_rights_belong_to_named_object(self, world49):
        res = world49[0]["find_roles"]("Документ.Док")
        mgr = _explicit_role(res, "Менеджер")
        assert mgr["object"] == "Document.Док" and mgr["rights"] == ["Read", "Update", "Posting"], mgr
        assert {d["object"] for d in mgr["rights_by_object"]} == {
            "Catalog.Док",
            "Catalog.ДокВложения",
            "Document.Док",
            "Document.Док.Command.Печать",
        }, mgr
        reader = _explicit_role(res, "ЧтецВложений")  # точного объекта среди совпадений нет — первое совпадение
        assert reader["object"] == "Catalog.ДокВложения" and reader["rights"] == ["Read"], reader

    def test_typed_query_prefers_category(self, world49):
        bsl = world49[0]
        mgr = _explicit_role(bsl["find_roles"]("Справочник.Док"), "Менеджер")
        assert mgr["object"] == "Catalog.Док" and mgr["rights"] == ["Read"], mgr
        assert _explicit_role(bsl["find_roles"]("Документ.Док"), "Менеджер")["object"] == "Document.Док"
        bare = _explicit_role(bsl["find_roles"]("Док"), "Менеджер")  # первый точный по (casefold, имя)
        assert bare["object"] == "Catalog.Док" and bare["rights"] == ["Read"], bare

    def test_full_rights_independent_of_sample(self, world49):
        res = world49[0]["find_roles"]("Документ.Док", details_limit=1)
        mgr = _explicit_role(res, "Менеджер")
        assert mgr["details_truncated"] is True, mgr
        assert mgr["object"] == "Document.Док" and mgr["rights"] == ["Read", "Update", "Posting"], mgr
        flagged = [r for r in res["roles"] if r.get("granted_via") == "set_for_new_objects"]
        assert flagged and all(r["rights"] == [] for r in flagged), flagged

    @pytest.mark.parametrize(
        "arg,obj,rights",
        [
            ("SessionParameters/ТекОрг", "SessionParameter.ТекОрг", ["Get"]),
            ("ОбщаяФорма.ТекОрг", "CommonForm.ТекОрг", ["View"]),
            ("ОбщаяКоманда.ТекОрг", "CommonCommand.ТекОрг", ["View"]),
        ],
    )
    def test_nonref_kind_heads_pick_their_object(self, world49, arg, obj, rights):
        row = _explicit_role(world49[0]["find_roles"](arg), "БазовыеПрава")
        assert row["object"] == obj and row["rights"] == rights, row

    def test_single_object_roles_unchanged(self, world49):
        """Роль с правами на один объект: на индексе — ответ HEAD; живьём `object` — сам объект (прежде — строка
        запроса), `rights` прежние."""
        bsl, _root, route, fmt = world49
        row = _explicit_role(bsl["find_roles"]("ДокВложения"), "ЧтецВложений")
        assert row == {
            "role_name": "ЧтецВложений",
            "object": "Catalog.ДокВложения",
            "rights": ["Read"],
            "file": _obj_file(fmt, "Roles", "ЧтецВложений", "Ext/Rights.xml"),
            "matched_objects": ["Catalog.ДокВложения"],
            "rights_by_object": [{"object": "Catalog.ДокВложения", "rights": ["Read"]}],
            "details_truncated": False,
            "granted_via": "explicit",
            "excluded_rights": [],
        }, row


# ═══════════════ Задача 6 (Д5, Д6; У1, У2) — раскрытие перечислений: префикс CF и предел ═══════════════


def _is_enum_type(t) -> bool:
    """Тестовая копия правила: хвост после `:` начинается с `EnumRef.`, `ПеречислениеСсылка.` или `Enum.`."""
    return isinstance(t, str) and t.rsplit(":", 1)[-1].startswith(("EnumRef.", "ПеречислениеСсылка.", "Enum."))


def _attr_enum_types(res: dict) -> set[str]:
    types = {t for group in ("attributes", "dimensions", "resources") for a in res[group] for t in a["type"]}
    types |= {t for ts in res["tabular_sections"] for c in ts["columns"] for t in c["type"]}
    return {t for t in types if _is_enum_type(t)}


class TestEnumExpansion:
    def test_cf_live_expands_namespaced_enum(self, world49):
        bsl, _root, route, fmt = world49
        evs = bsl["get_object_full_structure"]("Документ.Док")["enum_values_for_typed_refs"]
        key = "cfg:EnumRef.СтавкиНДС" if (fmt, route) == ("cf", "live") else "EnumRef.СтавкиНДС"
        assert [v["name"] for v in evs[key]] == _ENUM_RATES, evs

    def test_expansion_keys_join_attribute_types(self, world49):
        res = world49[0]["get_object_full_structure"]("Документ.Док")
        assert _attr_enum_types(res) == set(res["enum_values_for_typed_refs"]), res
        assert len(res["enum_values_for_typed_refs"]) == 2

    def test_enum_values_capped_at_50(self, world49):
        bsl = world49[0]
        res = bsl["get_object_full_structure"]("Документ.Док")
        evs = res["enum_values_for_typed_refs"]
        (key,) = [k for k in evs if k.endswith("EnumRef.ВидыОпераций")]
        assert [v["name"] for v in evs[key]] == _ENUM_OPS[:50], evs[key]
        assert res["_meta"]["enum_values_truncated"] == {key: 60}, res["_meta"]
        assert "find_enum_values(" in res["hint"] and f"{key} — 60" in res["hint"], res["hint"]
        call = re.search(r"find_enum_values\('([^']+)'\)", res["hint"]).group(1)
        assert len(bsl["find_enum_values"](call)["values"]) == 60

    def test_small_enums_unchanged(self, world49):
        """Обе версии: объект без перечислений больше 50 — раскрытие как на HEAD (кроме живого CF), без `hint`."""
        bsl, _root, route, fmt = world49
        res = bsl["get_object_full_structure"]("Справочник.ДокВложения")
        assert "hint" not in res, res
        if (fmt, route) != ("cf", "live"):
            assert res["enum_values_for_typed_refs"] == {
                "EnumRef.СтавкиНДС": [{"name": v, "synonym": ""} for v in _ENUM_RATES]
            }, res

    def test_truncation_key_always_present(self, world49):
        bsl = world49[0]
        assert bsl["get_object_full_structure"]("Справочник.ДокВложения")["_meta"]["enum_values_truncated"] == {}
        assert bsl["get_object_full_structure"]("Картинка")["_meta"]["enum_values_truncated"] == {}
        assert "enum_values_truncated" not in bsl["get_object_full_structure"]("НетТакого")["_meta"]


# ═══════════════ Задача 7 (Д8) — find_enum_values без индекса: фрагмент в другом регистре ═══════════════


def _case_sensitive_glob(glob_fn):
    """Эмуляция регистрозависимой ФС (Linux): у маски `…*<фрагмент>*.xml|.mdo` остаются только пути, чьё имя файла
    содержит фрагмент в точном регистре; прочие маски — как есть."""

    def wrapped(pattern, *args, **kwargs):
        res = glob_fn(pattern, *args, **kwargs)
        m = re.search(r"\*([^*/]+)\*\.(?:xml|mdo)$", pattern)
        if m is None or not isinstance(res, list):
            return res
        return [p for p in res if m.group(1) in p.rsplit("/", 1)[-1]]

    return wrapped


def _live_helpers(world: World49, glob_wrap=None) -> dict:
    return _bsl_for(
        world.root,
        None,
        glob_wrap=glob_wrap,
        extension_paths=[str(world.ext_root)],
        current_config_role="main",
        current_config_name="Тест",
        current_config_root=str(world.root),
        extension_name_by_root={str(world.ext_root): "Расш1"},
    )


class TestEnumFragmentCase:
    @pytest.mark.parametrize("fragment", ["ставки", "СТАВКИ"])
    def test_fragment_other_case_on_case_sensitive_fs(self, world49, fragment):
        bsl = world49[0]
        if bsl.route != "live":
            pytest.skip("ветка без индекса")
        helpers = _live_helpers(bsl, glob_wrap=_case_sensitive_glob)
        res = helpers["find_enum_values"](fragment)
        assert res.get("name") == "СтавкиНДС", res
        assert "hint" in res and "ближайшее по подстроке" in res["hint"], res
        assert res["file"] == _descriptor_rel(bsl.fmt, "Enums", "СтавкиНДС"), res

    def test_fragment_other_case_native_fs(self, world49):
        """Без обёртки: на Windows маска находит сама, на Linux — новая ветка; ответ один."""
        bsl = world49[0]
        res = bsl["find_enum_values"]("ставки")
        assert res.get("name") == "СтавкиНДС" and "hint" in res, res

    def test_mask_hit_unchanged(self, world49):
        """Обе версии: маска нашла — ответ прежний. v1.49.0 (Н6): прежде здесь и на живом маршруте стоял хвост
        «точное имя — search_objects('Ставки')» — без индекса неисполним; теперь там прочие перечисления с подстрокой
        на диске (у мира — других нет), с индексом — прежний совет."""
        bsl, _root, route, fmt = world49
        tail = "точное имя — search_objects('Ставки')" if route == "index" else "других похожих имен на диске нет"
        assert bsl["find_enum_values"]("Ставки") == {
            "name": "СтавкиНДС",
            "synonym": "",
            "values": [{"name": v, "synonym": ""} for v in _ENUM_RATES],
            "file": _descriptor_rel(fmt, "Enums", "СтавкиНДС"),
            "hint": f"перечисления с именем 'Ставки' нет; ответ — ближайшее по подстроке: СтавкиНДС; {tail}",
        }

    def test_missing_fragment_still_not_found(self, world49):
        bsl = world49[0]
        if bsl.route != "live":
            pytest.skip("ветка без индекса")
        helpers = _live_helpers(bsl, glob_wrap=_case_sensitive_glob)
        assert helpers["find_enum_values"]("нетакого") == {"error": "Перечисление 'нетакого' не найдено"}


# ═══════════════ Задача 8 (Д11, У4) — find_register_writers: имя без учёта регистра на индексе ═══════════════

_WRITERS_HINT_HEAD = (
    "Статические ссылки из кода/индекса: CFE-замены и Posting=Deny здесь не применяются. "
    "find_register_movements(document) применяет эти фильтры, но main-строки там остаются снимком индекса; "
    "после изменения main-кода проверь живой файл кандидата."
)


class TestRegisterNameCase:
    @pytest.mark.parametrize("spelling", ["остатки", "ОСТАТКИ"])
    def test_cyrillic_other_case_finds_writers(self, world49, spelling):
        bsl = world49[0]
        base = bsl["find_register_writers"]("Остатки")
        res = bsl["find_register_writers"](spelling)
        assert base["writers"] and base["declared_documents"] == ["Док"], base
        assert res["writers"] == base["writers"] and res["declared_documents"] == base["declared_documents"], res
        assert "с учетом регистра" not in res["hint"], res["hint"]

    def test_noise_name_unchanged(self, world49):
        """Обе версии: имя-шум (`Записать`) — ответ прежний."""
        bsl, _root, route, _fmt = world49
        res = _strip_times(bsl["find_register_writers"]("Записать"))
        assert res == {
            "register": "Записать",
            "writers": [],
            "total_documents_scanned": None if route == "index" else 2,
            "total_writers": 0,
            "declared_documents": [],
            "runtime_filtered": False,
            "hint": _WRITERS_HINT_HEAD + " Ни один документ не объявил регистр в движениях (RegisterRecords): "
            "документы-регистраторы его не пишут.",
            "_meta": {"declared_source": route},
        }, res

    def test_live_route_unchanged(self, world49):
        """Обе версии: без индекса имя и прежде сравнивалось без учёта регистра."""
        bsl, _root, route, fmt = world49
        if route != "live":
            pytest.skip("живой маршрут")
        res = bsl["find_register_writers"]("остатки")
        assert res["writers"] == [
            {"document": "Док", "file": _obj_file(fmt, "Documents", "Док", "Ext/ObjectModule.bsl"), "lines": [5]}
        ], res
        assert res["declared_documents"] == ["Док"] and res["_meta"] == {"declared_source": "live"}, res


# ═══════════════ Задача 9 (Д12, П7) — промах на предельной глубине ═══════════════


def _module_rel(fmt: str, name: str) -> str:
    return _obj_file(fmt, "CommonModules", name, "Ext/Module.bsl")


class TestDepthLimitMiss:
    def test_find_path_miss_at_depth_has_hint(self, world49):
        bsl, _root, route, fmt = world49
        if route != "index":
            pytest.skip("граф вызовов — индекс")
        mod = _module_rel(fmt, "ОбщМод")
        res = bsl["find_path"]("А1", "А6", max_depth=4, from_hint=mod, to_hint=mod)
        assert res["found"] is False and res["_meta"]["budget_exceeded"] is False, res
        assert "max_depth=4" in res["hint"] and "max_depth=5" in res["hint"], res["hint"]
        assert "truncated_nodes" not in res["_meta"], res["_meta"]
        ok = bsl["find_path"]("А1", "А6", max_depth=5, from_hint=mod, to_hint=mod)
        assert ok["found"] is True and ok["depth"] == 5, ok

    def test_find_path_clamp_named(self, world49):
        bsl, _root, route, fmt = world49
        if route != "index":
            pytest.skip("граф вызовов — индекс")
        mod = _module_rel(fmt, "ОбщМод2")
        res = bsl["find_path"]("Б1", "Б10", max_depth=9, from_hint=mod, to_hint=mod)
        assert res["found"] is False and res["_meta"]["max_depth"] == 8, res
        assert res["_meta"]["budget_exceeded"] is False, res["_meta"]
        assert "max_depth=8 — предел" in res["hint"] and "(запрошено 9, урезано до 8)" in res["hint"], res["hint"]
        in_limit = bsl["find_path"]("Б1", "Б10", max_depth=8, from_hint=mod, to_hint=mod)
        assert "max_depth=8 — предел" in in_limit["hint"] and "урезано" not in in_limit["hint"], in_limit

    def test_find_data_path_miss_at_depth_has_hint(self, world49):
        bsl, _root, route, _fmt = world49
        if route != "index":
            pytest.skip("граф метаданных — индекс")
        res = bsl["find_data_path"]("Документ.Док", "Справочник.Спр2", max_depth=1)
        assert res["found"] is False and res["_meta"]["budget_exceeded"] is False, res
        assert "max_depth=2" in res["hint"] and "truncated_nodes" not in res["_meta"], res
        ok = bsl["find_data_path"]("Документ.Док", "Справочник.Спр2", max_depth=2)
        assert ok["found"] is True and ok["depth"] == 2, ok

    def test_absent_end_is_named(self, world49):
        """Конца нет среди методов индекса и расширений — подсказка называет его вместо подсказки глубины (решение
        владельца по ревью fable): повторы с max_depth+1 его не найдут. Совет исполним и находит похожее имя."""
        bsl, _root, route, fmt = world49
        if route != "index":
            pytest.skip("отсутствие метода доказывает авторитетный граф индекса")
        mod = _module_rel(fmt, "ОбщМод")
        res = bsl["find_path"]("ВтораяХ", "А6", max_depth=4, to_hint=mod)
        assert res["found"] is False and res["_meta"]["budget_exceeded"] is False, res
        assert "from='ВтораяХ' не найден" in res["hint"] and "max_depth" not in res["hint"], res["hint"]
        fragment = res["hint"].split("search_methods('")[1].split("'")[0]
        assert "Вторая" in {m["name"] for m in bsl["search_methods"](fragment)}, fragment
        absent_to = bsl["find_path"]("А1", "НетТакогоМетода", max_depth=4, from_hint=mod)
        assert "to='НетТакогоМетода' в графе вызовов нет ни объявления, ни вызывающих" in absent_to["hint"], absent_to
        # Регистр кириллицы не важен: имя есть — прежняя подсказка о глубине.
        assert "max_depth=5" in bsl["find_path"]("а1", "А6", max_depth=4, to_hint=mod)["hint"]

    def test_undeclared_target_with_callers_is_not_absent(self, tmp_path, monkeypatch):
        """Цель без объявления в исходниках (метод внешнего объекта), у которой есть вызывающие, — узел графа: промах на
        глубине остаётся подсказкой о глубине, а не «не найден» (третья перепроверка Codex)."""
        body = (
            "Процедура Начало(Компонента) Экспорт\n\tСередина(Компонента);\nКонецПроцедуры\n\n"
            'Процедура Середина(Компонента) Экспорт\n\tСообщить("Текст");\n\tКомпонента.ВнешняяЦель();\nКонецПроцедуры\n'
        )
        root = _objects_tree(tmp_path / "cf", "cf", [("CommonModules", "Внеш", {"Ext/Module.bsl": body}, {})])
        reader = _reader_for(root, monkeypatch)
        try:
            bsl = _bsl_for(root, reader, idx_zero_callers_authoritative=True)
            miss = bsl["find_path"]("Начало", "ВнешняяЦель", max_depth=1)
            assert miss["found"] is False and "max_depth=2" in miss["hint"] and "to='" not in miss["hint"], miss
            assert bsl["find_path"]("Начало", "ВнешняяЦель", max_depth=2)["found"] is True
        finally:
            reader.close()

    def test_partial_extension_layer_is_not_absence(self, tmp_path, monkeypatch):
        """Слой вызовов расширений неполон (бюджет модулей) — отсутствие метода не доказано: прежняя подсказка о
        неполноте слоя, а не «не найден» (четвёртая перепроверка Codex)."""
        import rlm_tools_bsl.bsl_helpers as bh

        main_mod = "CommonModules/Основа/Ext/Module.bsl"
        # вызов в основной конфигурации нужен: без рёбер граф не авторитетен (`has_calls`), и отсутствие не судится
        main_body = (
            "Процедура КонецЦепочки() Экспорт\nКонецПроцедуры\n\nПроцедура Середина() Экспорт\n\tКонецЦепочки();\n"
            "КонецПроцедуры\n"
        )
        main = _objects_tree(tmp_path / "cf", "cf", [("CommonModules", "Основа", {"Ext/Module.bsl": main_body}, {})])
        ext_body = "Процедура ИсточникРасширения() Экспорт\n\tОснова.КонецЦепочки();\nКонецПроцедуры\n"
        ext = _objects_tree(
            tmp_path / "cfe" / "Расш1",
            "cf",
            [("CommonModules", "Расш1_М", {"Ext/Module.bsl": ext_body}, {})],
            CF_EXT_DESCRIPTOR,
        )
        reader = _reader_for(main, monkeypatch)
        try:
            monkeypatch.setattr(bh, "_AUDIT_EXT_MODULE_BUDGET", 0)  # модуль расширения слоем не прочитан
            bsl = _bsl_for(
                main,
                reader,
                idx_zero_callers_authoritative=True,
                extension_paths=[str(ext)],
                current_config_role="main",
                current_config_name="Тест",
                current_config_root=str(main),
                extension_name_by_root={str(ext): "Расш1"},
            )
            res = bsl["find_path"]("ИсточникРасширения", "КонецЦепочки", max_depth=4, to_hint=main_mod)
            assert res["found"] is False and res["_meta"].get("extension_layer") == "partial", res
            assert "не найден среди методов" not in res.get("hint", ""), res
        finally:
            reader.close()

    def test_extension_session_does_not_claim_absence(self, tmp_path, monkeypatch):
        """Сессия на расширении: её граф не включает основную конфигурацию — процедура основной в нём не найдётся, но
        «не найден» утверждать нельзя (класс находки о неполном слое)."""
        main = _objects_tree(
            tmp_path / "cf",
            "cf",
            [
                (
                    "CommonModules",
                    "Основа",
                    {"Ext/Module.bsl": "Процедура ПроцедураОсновной() Экспорт\nКонецПроцедуры\n"},
                    {},
                )
            ],
        )
        ext_body = (
            "Процедура Начало() Экспорт\n\tКонец();\nКонецПроцедуры\n\nПроцедура Конец() Экспорт\nКонецПроцедуры\n"
        )
        ext = _objects_tree(
            tmp_path / "cfe" / "Расш1",
            "cf",
            [("CommonModules", "Расш1_М", {"Ext/Module.bsl": ext_body}, {})],
            CF_EXT_DESCRIPTOR,
        )
        reader = _reader_for(ext, monkeypatch)
        try:
            bsl = _bsl_for(
                ext,
                reader,
                idx_zero_callers_authoritative=True,
                current_config_role="extension",
                current_config_name="Расш1",
                current_config_root=str(ext),
                main_config_root=str(main),
            )
            res = bsl["find_path"]("ПроцедураОсновной", "Конец", max_depth=2)
            assert res["found"] is False and "не найден среди методов" not in res.get("hint", ""), res
        finally:
            reader.close()

    def test_conclusive_miss_unchanged(self, world49):
        """Обе версии: фронт исчерпан раньше предела глубины — промах окончателен, без `hint`."""
        bsl, _root, route, fmt = world49
        if route != "index":
            pytest.skip("графы — индекс")
        res = bsl["find_path"](
            "Вторая", "А6", max_depth=8, from_hint=_module_rel(fmt, "ОбщМод2"), to_hint=_module_rel(fmt, "ОбщМод")
        )
        assert res["found"] is False and res["_meta"]["budget_exceeded"] is False and "hint" not in res, res
        data = bsl["find_data_path"]("Справочник.Спр2", "Документ.Док", max_depth=4)
        assert data == {
            "found": False,
            "from": "Catalog.Спр2",
            "to": "Document.Док",
            "path": None,
            "depth": 0,
            "partial": False,
            "_meta": {
                "max_depth": 4,
                "nodes_expanded": 1,
                "node_budget": data["_meta"]["node_budget"],
                "budget_exceeded": False,
                "kinds": None,
            },
        }, data

    def test_cycle_at_depth_is_not_a_cut(self, world49):
        """Узел, вернувшийся по циклу на предельную глубину (уже раскрыт у корня), упором в глубину не считается."""
        bsl, _root, route, fmt = world49
        if route != "index":
            pytest.skip("граф вызовов — индекс")
        mod = _module_rel(fmt, "ОбщМод2")
        res = bsl["find_path"]("Вторая", "Ц1", max_depth=2, from_hint=mod, to_hint=mod)
        assert res["found"] is False and res["_meta"]["budget_exceeded"] is False, res
        assert "hint" not in res, res

    def test_depth_limit_hint_text(self):
        """Текст подсказки — по контракту плана (раздел 2, Д12)."""
        import rlm_tools_bsl.bsl_helpers as bh

        assert bh._depth_limit_hint(3, 3).endswith("повтори с max_depth=4")
        assert "max_depth=8 — предел" in bh._depth_limit_hint(8, 8)
        assert bh._depth_limit_hint(8, "12").endswith("(запрошено 12, урезано до 8)")


# ═══════════════ Задача 10 (Д7, У3) — секция перехвата начинается со своей строки ═══════════════

_OV_MAIN_MODULE = "Процедура ПередЗаписью(Отказ)\nКонецПроцедуры\n"
# Модуль расширения: строка аннотации (5), строка комментария (6), объявление (7).
_OV_EXT_MODULE = (
    "// Модуль расширения\n"
    "Процедура Помощник()\n"
    "КонецПроцедуры\n"
    "\n"
    '&После("ПередЗаписью")\n'
    "// Перехват записи\n"
    "Процедура Расш1_ПередЗаписью(Отказ)\n"
    "\tПомощник();\n"
    "КонецПроцедуры\n"
)
_OV_MAIN_FORM_MODULE = "&НаСервере\nПроцедура ПриСозданииНаСервере(Отказ, СтандартнаяОбработка)\nКонецПроцедуры\n"
_OV_EXT_FORM_MODULE = "&НаСервере\nПроцедура Расш1_ПриСозданииПосле(Отказ, СтандартнаяОбработка)\nКонецПроцедуры\n"
_OV_EXT_FORM_XML = (
    f'<?xml version="1.0" encoding="UTF-8"?>\n<Form {_CF_FORM_NS} version="2.20"><Events>'
    '<Event name="OnCreateAtServer" callType="After">Расш1_ПриСозданииПосле</Event></Events>'
    '<BaseForm version="2.20"><Events><Event name="OnCreateAtServer">ПриСозданииНаСервере</Event></Events>'
    "</BaseForm></Form>"
)
_OV_MAIN_FORM_XML = (
    f'<?xml version="1.0" encoding="UTF-8"?>\n<Form {_CF_FORM_NS} version="2.20"><Events>'
    '<Event name="OnCreateAtServer">ПриСозданииНаСервере</Event></Events></Form>'
)


def _override_tree(base: Path, fmt: str, eol: str) -> tuple[Path, Path]:
    """Основная конфигурация с `Док` и расширение `Расш1` с перехватом `&После("ПередЗаписью")`; модуль расширения
    записан с окончанием строк ``eol``. У CF — ещё перехват события формы через `callType`."""
    form_files = {
        "Forms/ФормаДокумента/Ext/Form.xml": (_OV_MAIN_FORM_XML, None),
        "Forms/ФормаДокумента/Ext/Form/Module.bsl": (_OV_MAIN_FORM_MODULE, None),
    }
    main = _objects_tree(
        base / "cf",
        fmt,
        [
            (
                "Documents",
                "Док",
                {"Ext/ObjectModule.bsl": _OV_MAIN_MODULE, **form_files},
                {"cf_children": "<ChildObjects><Form>ФормаДокумента</Form></ChildObjects>"},
            )
        ],
    )
    ext = _objects_tree(
        base / "cfe" / "Расш1",
        fmt,
        [
            (
                "Documents",
                "Док",
                {
                    "Ext/ObjectModule.bsl": _OV_EXT_MODULE,
                    "Forms/ФормаДокумента/Ext/Form.xml": (_OV_EXT_FORM_XML, None),
                    "Forms/ФормаДокумента/Ext/Form/Module.bsl": (_OV_EXT_FORM_MODULE, None),
                },
                {
                    "cf_props": "<ObjectBelonging>Adopted</ObjectBelonging>",
                    "cf_children": "<ChildObjects><Form>ФормаДокумента</Form></ChildObjects>",
                    "edt": "<objectBelonging>Adopted</objectBelonging>",
                },
            )
        ],
        CF_EXT_DESCRIPTOR if fmt == "cf" else EDT_EXT_DESCRIPTOR,
        eol=eol,
    )
    return main, ext


def _override_helpers(main: Path, ext: Path, reader) -> dict:
    return _bsl_for(
        main,
        reader,
        extension_paths=[str(ext)],
        current_config_role="main",
        current_config_name="Тест",
        current_config_root=str(main),
        extension_name_by_root={str(ext): "Расш1"},
    )


def _section_lines(text: str, ext_name: str = "Расш1") -> list[str]:
    """Строки секции перехвата: от `// Файл: …` расширения до конца ответа."""
    lines = text.splitlines()
    i = next(k for k, line in enumerate(lines) if line.startswith("// Файл: ") and ext_name in line)
    return lines[i:]


@pytest.fixture(params=["cf-index", "cf-live", "edt-index", "edt-live"])
def override_env(request, tmp_path, monkeypatch):
    fmt, route = request.param.split("-")
    yield fmt, route, tmp_path, monkeypatch


class TestOverrideSectionLine:
    @pytest.mark.parametrize("eol", ["\n", "\r\n"], ids=["lf", "crlf"])
    def test_section_text_starts_at_annotation(self, override_env, eol):
        fmt, route, tmp_path, monkeypatch = override_env
        main, ext = _override_tree(tmp_path, fmt, eol)
        reader = _reader_for(main, monkeypatch) if route == "index" else None
        try:
            bsl = _override_helpers(main, ext, reader)
            module = _obj_file(fmt, "Documents", "Док", "Ext/ObjectModule.bsl")
            text = bsl["read_procedure"](module, "ПередЗаписью", include_overrides=True, numbered=True)
            sec = _section_lines(text)
            assert sec[0].endswith(":5"), sec
            assert sec[1] == '5 | &После("ПередЗаписью")', sec
            assert sec[2] == "6 | // Перехват записи", sec
            assert sec[3] == "7 | Процедура Расш1_ПередЗаписью(Отказ)", sec
            assert sec[-1] == "9 | КонецПроцедуры", sec
        finally:
            if reader is not None:
                reader.close()

    def test_stale_annotation_line_falls_back(self, override_env):
        """Строка индекса указывает не на аннотацию (модуль изменён после сборки) — текст и ссылка от объявления."""
        fmt, route, tmp_path, monkeypatch = override_env
        if route != "index":
            pytest.skip("строка перехвата — из индекса")
        main, ext = _override_tree(tmp_path, fmt, "\n")
        reader = _reader_for(main, monkeypatch)
        original = IndexReader.get_extension_overrides

        def stale(self, *args, **kwargs):
            rows = original(self, *args, **kwargs)
            return [{**r, "ext_line": 2} if r.get("extension_method") == "Расш1_ПередЗаписью" else r for r in rows]

        monkeypatch.setattr(IndexReader, "get_extension_overrides", stale)
        try:
            bsl = _override_helpers(main, ext, reader)
            module = _obj_file(fmt, "Documents", "Док", "Ext/ObjectModule.bsl")
            sec = _section_lines(bsl["read_procedure"](module, "ПередЗаписью", include_overrides=True, numbered=True))
            assert sec[0].endswith(":7"), sec
            assert sec[1] == "7 | Процедура Расш1_ПередЗаписью(Отказ)", sec
        finally:
            reader.close()

    def test_form_calltype_section_unchanged(self, override_env):
        """Обе версии: перехват из формы (`callType`) — прежняя секция: ссылка и текст от объявления."""
        fmt, route, tmp_path, monkeypatch = override_env
        if fmt != "cf":
            pytest.skip("перехват формы через callType — только CF")
        main, ext = _override_tree(tmp_path, fmt, "\n")
        reader = _reader_for(main, monkeypatch) if route == "index" else None
        try:
            bsl = _override_helpers(main, ext, reader)
            module = "Documents/Док/Forms/ФормаДокумента/Ext/Form/Module.bsl"
            text = bsl["read_procedure"](module, "ПриСозданииНаСервере", include_overrides=True, numbered=True)
            assert "callType формы: событие OnCreateAtServer формы" in text, text
            sec = _section_lines(text)
            assert sec[0].endswith(":2"), sec
            assert sec[1:] == [
                "2 | Процедура Расш1_ПриСозданииПосле(Отказ, СтандартнаяОбработка)",
                "3 | КонецПроцедуры",
            ], sec
        finally:
            if reader is not None:
                reader.close()


# ═══════════════ Задача 11 (Д1, П1) — сверка модулей объекта с диском ═══════════════


@pytest.fixture(params=["cf-index", "edt-index"])
def drifted49(request, tmp_path):
    """Мир релиза на индексе, собранном ДО модулей `_add_after_build` (модуль добавлен до открытия сессии)."""
    added: list[str] = []

    def after(root, fmt):
        added.extend(_add_after_build(root, fmt))

    world = _build_world49(tmp_path, request.param, after_build=after)
    yield world, added
    world.reader.close()


class TestModulesNotIndexed:
    def test_object_modules_name_disk_modules(self, drifted49):
        bsl, added = drifted49
        res = bsl["get_object_modules"]("Документ.Док")
        assert res["_meta"]["modules_not_indexed"] == [added[1]], res["_meta"]
        assert "index update" in res["hint"] and added[1] in res["hint"], res.get("hint")
        call = re.search(r"extract_procedures\('([^']+)'\)", res["hint"]).group(1)
        assert [p["name"] for p in bsl["extract_procedures"](call)] == ["НоваяПроцедура"]

    def test_print_forms_name_disk_manager_module(self, drifted49):
        bsl, added = drifted49
        res = bsl["find_print_forms"]("Справочник.Спр1")
        assert added[0] in res["hint"] and "read_procedure(" in res["hint"], res
        assert "в каталоге модулей сессии нет" not in res["hint"], res["hint"]
        assert res["_meta"]["modules_not_indexed"] == [added[0]], res["_meta"]
        call = re.search(r"read_procedure\('([^']+)', 'ДобавитьКомандыПечати'\)", res["hint"]).group(1)
        assert "ДобавитьКомандыПечати" in bsl["read_procedure"](call, "ДобавитьКомандыПечати")

    def test_analyze_object_and_profile_name_disk_modules(self, drifted49):
        bsl, added = drifted49
        res = bsl["analyze_object"]("Док")
        assert res["_meta"] == {"modules_not_indexed": [added[1]]}, res.get("_meta")
        assert "index update" in res["hint"], res.get("hint")
        sec = bsl["get_object_profile"]("Документ.Док", sections=["modules"])["sections"]["modules"]
        assert sec["_meta"]["modules_not_indexed"] == [added[1]], sec["_meta"]
        assert "index update" in sec["_meta"]["hint"], sec["_meta"]

    def test_common_form_module_added_after_build(self, drifted49):
        bsl, added = drifted49
        res = bsl["get_object_modules"]("ОбщаяФорма.ОбщФорма")
        assert res["_meta"]["modules_not_indexed"] == [added[2]], res
        assert "нет модулей в индексе; на диске есть" in res["error"], res["error"]
        sec = bsl["get_object_profile"]("ОбщаяФорма.ОбщФорма", sections=["modules"])["sections"]["modules"]
        assert sec["_meta"]["modules_not_indexed"] == [added[2]], sec
        assert bsl["analyze_object"]("ОбщФорма")["_meta"] == {"modules_not_indexed": [added[2]]}

    def test_no_disk_drift_unchanged(self, world49):
        """Обе версии: индекс равен диску (и живой каталог) — ключа нет, ответы прежние."""
        bsl = world49[0]
        res = bsl["get_object_modules"]("Документ.Док")
        assert "modules_not_indexed" not in res["_meta"] and "hint" not in res, res
        obj = bsl["analyze_object"]("Док")
        assert "_meta" not in obj and "hint" not in obj, obj
        pf = bsl["find_print_forms"]("Справочник.Спр1")
        assert "modules_not_indexed" not in pf.get("_meta", {}), pf
        assert "Модуля менеджера в каталоге модулей сессии нет." in pf["hint"], pf["hint"]
        card = bsl["get_object_modules"]("ОбщаяФорма.ОбщФорма")
        assert card["error"].startswith("У объекта CommonForms/ОбщФорма нет модулей (сам объект есть"), card

    def test_live_catalog_has_no_check(self, tmp_path):
        """Обе версии: без индекса каталог и так с диска — модуль, добавленный до сессии, просто в ответе."""
        added: list[str] = []
        world = _build_world49(
            tmp_path, "cf-live", after_build=lambda root, fmt: added.extend(_add_after_build(root, fmt))
        )
        res = world["get_object_modules"]("Документ.Док")
        assert added[1] in {m["path"] for m in res["modules"]}, res
        assert "modules_not_indexed" not in res["_meta"] and "hint" not in res, res

    @pytest.mark.parametrize("fmt", ["cf", "edt"])
    def test_nested_subsystems_are_not_walked(self, tmp_path, monkeypatch, fmt):
        """Сверка у подсистемы не обходит дерево вложенных подсистем — модулей там не бывает (ревью: `os.walk` прочих
        подкаталогов проходил `Subsystems/<Имя>/Subsystems/…` на КАЖДОМ `get_object_modules`/`analyze_object`).
        Первый вызов листает дерево и на HEAD (кешируемый разбор описателей) — проверяются повторные."""
        objects = [("Subsystems", "Верх", {}, {})]
        for i in range(5):
            objects += [("Subsystems", f"Верх/В{i}", {}, {}), ("Subsystems", f"Верх/В{i}/Г{i}", {}, {})]
        root = _objects_tree(tmp_path / "cf", fmt, objects)
        reader = _reader_for(root, monkeypatch)
        try:
            bsl = _bsl_for(root, reader)
            for arg in ("Подсистема.Верх", "Верх"):
                bsl["get_object_modules"](arg)  # прогрев кешей сессии
            nested = os.path.normcase(str(root / "Subsystems" / "Верх" / "Subsystems"))
            scans: list[str] = []
            real_scandir = os.scandir

            def spy_scandir(path="."):
                if os.path.normcase(os.fspath(path)).startswith(nested):
                    scans.append(os.fspath(path))
                return real_scandir(path)

            monkeypatch.setattr(os, "scandir", spy_scandir)
            answers = [
                answer
                for arg in ("Подсистема.Верх", "Верх")
                for answer in (
                    bsl["get_object_modules"](arg),
                    bsl["analyze_object"](arg),
                    bsl["get_object_profile"](arg, sections=["modules"]),
                )
            ]
            monkeypatch.undo()
            assert scans == [], scans
            assert not [a for a in answers if "modules_not_indexed" in json.dumps(a, ensure_ascii=False)], answers
        finally:
            reader.close()

    @pytest.mark.parametrize("fmt", ["cf", "edt"])
    def test_disk_check_cost(self, tmp_path, monkeypatch, fmt):
        """Адресный листинг: на объекте с 30 формами, 10 командами и 20 макетами — не больше 4 `os.scandir` (объект,
        `Ext`, `Forms`, `Commands`) и 40 `os.path.isfile` внутри объекта; поддерево `Templates` не листается."""
        files = {"Ext/ObjectModule.bsl": PROC}
        for i in range(30):
            files[f"Forms/Ф{i}/Ext/Form/Module.bsl"] = PROC
        for i in range(10):
            files[f"Commands/К{i}/Ext/CommandModule.bsl"] = (PROC, None)
            files[f"Commands/К{i}/CommandModule.bsl"] = (None, PROC)
        for i in range(20):
            files[f"Templates/М{i}/Ext/Template.xml"] = ("<document/>", "<document/>")
        root = _objects_tree(tmp_path / "cf", fmt, [("Documents", "Большой", files, {})])
        reader = _reader_for(root, monkeypatch)
        try:
            bsl = _bsl_for(root, reader)
            bsl["get_object_modules"]("Документ.Большой")  # каталог прогрет
            obj_dir = os.path.normcase(str(root / "Documents" / "Большой"))
            scans: list[str] = []
            checks: list[str] = []
            real_scandir, real_isfile = os.scandir, os.path.isfile

            def spy_scandir(path="."):
                if os.path.normcase(os.fspath(path)).startswith(obj_dir):
                    scans.append(os.fspath(path))
                return real_scandir(path)

            def spy_isfile(path):
                if os.path.normcase(os.fspath(path)).startswith(obj_dir):
                    checks.append(os.fspath(path))
                return real_isfile(path)

            monkeypatch.setattr(os, "scandir", spy_scandir)
            monkeypatch.setattr(os.path, "isfile", spy_isfile)
            res = bsl["get_object_modules"]("Документ.Большой")
            monkeypatch.undo()
            assert "modules_not_indexed" not in res["_meta"], res["_meta"]
            assert 1 <= len(scans) <= 4, scans
            assert len(checks) <= 40, len(checks)
            assert not [p for p in scans + checks if "Templates" in p], scans
        finally:
            reader.close()


# ═══════════════ Задача 12 (Д13, Д15; У5, У6) — подсказки песочницы и подпись grep_read ═══════════════

_RP_HINT = "read_procedure(path, name) возвращает СТРОКУ"
_GR_HINT = "grep_read возвращает {matches:"
_KEY_HINT = "— форма результата иная"
_SB_MODULE = "Documents/Док/Ext/ObjectModule.bsl"


@pytest.fixture
def hint_sandbox(tmp_path):
    from rlm_tools_bsl.sandbox import Sandbox

    root = tmp_path / "cf"
    _write(root / "Configuration.xml", CF_DESCRIPTOR)
    _write(root / _SB_MODULE, "Процедура А() Экспорт\nКонецПроцедуры\n\nПроцедура Б() Экспорт\nКонецПроцедуры\n")
    return Sandbox(base_path=str(root), format_info=detect_format(str(root)))


def _run_error(sb, code: str) -> str:
    res = sb.execute(code)
    assert res.error, res
    return res.error


class TestSandboxHintSource:
    def test_read_procedure_hint_only_from_its_value(self, hint_sandbox):
        # Д15: `read_procedure` в первой строке, ошибку даёт значение `grep_read` в другой строке.
        err = _run_error(
            hint_sandbox,
            f"t = read_procedure({_SB_MODULE!r}, 'А')\nfor m in grep_read('Процедура')['matches']: m.get('line')",
        )
        assert _RP_HINT not in err and _GR_HINT in err, err
        err = _run_error(hint_sandbox, f"t = read_procedure({_SB_MODULE!r}, 'А')\nt.get('x')")
        assert _RP_HINT in err and _GR_HINT not in err, err

    def test_rebinding_follows_the_last_value(self, hint_sandbox):
        err = _run_error(
            hint_sandbox,
            f"res = read_procedure({_SB_MODULE!r}, 'А')\nres = grep_read('Процедура')\n"
            "for m in res['matches']: m.get('line')",
        )
        assert _GR_HINT in err and _RP_HINT not in err, err
        err = _run_error(
            hint_sandbox, f"res = grep_read('Процедура')\nres = read_procedure({_SB_MODULE!r}, 'А')\nres.get('x')"
        )
        assert _RP_HINT in err and _GR_HINT not in err, err
        err = _run_error(
            hint_sandbox,
            "for m in grep_read('Процедура')['matches']: pass\n"
            f"for m in read_procedure({_SB_MODULE!r}, ['А', 'Б']).values(): m.get('x')",
        )
        assert _RP_HINT in err and _GR_HINT not in err, err

    def test_ambiguous_source_gets_no_helper_hint(self, hint_sandbox):
        err = _run_error(
            hint_sandbox,
            f"if len('x') > 5:\n    r = grep_read('Процедура')\nelse:\n    r = read_procedure({_SB_MODULE!r}, 'А')\n"
            "r.get('x')",
        )
        assert _RP_HINT not in err and _GR_HINT not in err, err

    def test_grep_read_hint_from_previous_call_variable(self, hint_sandbox):
        assert hint_sandbox.execute("res = grep_read('Процедура')").error is None
        err = _run_error(hint_sandbox, "res['matches'][0]")
        assert _GR_HINT in err and _KEY_HINT not in err, err

    def test_previous_rule_without_agent_frame(self):
        """Ошибка без кадра кода агента — прежнее правило (`read_procedure` в коде)."""
        from rlm_tools_bsl.sandbox import Sandbox

        err = "Traceback (most recent call last):\nAttributeError: 'str' object has no attribute 'get'"
        assert _RP_HINT in Sandbox._add_error_hints(err, "x = read_procedure(p, n)\nx.get('a')")

    def test_loop_body_may_not_run(self, hint_sandbox):
        """Тело цикла может не выполниться ни разу: привязка в нём (и цель `for`) после цикла источник не доказывает,
        если до цикла имя привязано иным значением (ревью Codex: `for m in []: res = grep_read(...)` давал подсказку
        grep_read на значении read_procedure)."""
        for loop in ("for m in []:", "while False:"):
            err = _run_error(
                hint_sandbox,
                f"res = read_procedure({_SB_MODULE!r}, 'А')\n{loop}\n    res = grep_read('Процедура')\nres.get('x')",
            )
            assert _GR_HINT not in err and _RP_HINT not in err, err
        err = _run_error(
            hint_sandbox,
            f"m = read_procedure({_SB_MODULE!r}, 'А')\nfor m in grep_read('НетТакогоТекста')['matches']:\n    pass\n"
            "m.get('x')",
        )
        assert _GR_HINT not in err and _RP_HINT not in err, err
        # Ошибка внутри цикла после привязки в его теле — источник этой итерации доказан.
        err = _run_error(
            hint_sandbox,
            f"res = read_procedure({_SB_MODULE!r}, 'А')\nfor p in [1]:\n    res = grep_read('Процедура')\n"
            "    for m in res['matches']: m.get('line')",
        )
        assert _GR_HINT in err and _RP_HINT not in err, err

    def test_unknown_value_before_conditional(self, hint_sandbox):
        """Без привязки до цикла или ветвления в коде вызова значение имени до них неизвестно — переменная прошлого
        вызова (перепроверка Codex); цель `for` действует только внутри тела, а не в `else` цикла."""
        assert hint_sandbox.execute(f"res = read_procedure({_SB_MODULE!r}, 'А')").error is None
        for code in (
            "for m in []:\n    res = grep_read('Процедура')\nres.get('x')",
            "for res in grep_read('НетТакогоТекста')['matches']:\n    pass\nres.get('x')",
            "if len('x') > 5:\n    res = grep_read('Процедура')\nres.get('x')",
        ):
            err = _run_error(hint_sandbox, code)
            assert _GR_HINT not in err, (code, err)
        err = _run_error(
            hint_sandbox,
            f"res = read_procedure({_SB_MODULE!r}, 'А')\nfor res in grep_read('НетТакогоТекста')['matches']:\n"
            "    pass\nelse:\n    res.get('x')",
        )
        assert _GR_HINT not in err, err
        # if/else, обе ветви которого привязывают имя одним хелпером, — источник доказан и без привязки до него.
        err = _run_error(
            hint_sandbox,
            "if len('x') > 5:\n    r = grep_read('Процедура')\nelse:\n    r = grep_read('А')\nr['matches'][0]",
        )
        assert _GR_HINT in err, err

    def test_class_body_binding_is_class_scoped(self, hint_sandbox):
        """Привязка в теле класса — атрибут класса, а не имя модуля; во вложенных функциях (методах) она не видна —
        свободное имя метода разрешается по модулю (вторая перепроверка Codex)."""
        err = _run_error(
            hint_sandbox,
            f"res = read_procedure({_SB_MODULE!r}, 'А')\nclass C:\n    res = grep_read('Процедура')\nres.get('x')",
        )
        assert _RP_HINT in err and _GR_HINT not in err, err
        err = _run_error(
            hint_sandbox,
            f"res = read_procedure({_SB_MODULE!r}, 'А')\nclass C:\n    res = grep_read('Процедура')\n"
            "    def inspect(self):\n        res.get('x')\nC().inspect()",
        )
        # Атрибут класса в методе не виден — ложной подсказки grep_read нет. Подсказки read_procedure тоже нет: внешнее
        # имя метода читается при вызове (общестыковая вычитка Codex), по порядку строк его источник не доказан.
        assert _GR_HINT not in err and _RP_HINT not in err, err
        # Внутри самого тела класса атрибут виден.
        err = _run_error(hint_sandbox, "class C:\n    r = grep_read('Процедура')\n    r['matches'][0]")
        assert _GR_HINT in err, err
        # Значение по умолчанию метода вычисляется в области класса — атрибут класса виден (третья перепроверка Codex).
        err = _run_error(
            hint_sandbox,
            f"res = grep_read('Процедура')\nclass C:\n    res = read_procedure({_SB_MODULE!r}, 'А')\n"
            "    def inspect(self, value=res.get('x')):\n        pass",
        )
        assert _RP_HINT in err and _GR_HINT not in err, err
        # Первый итератор включения вычисляется в области класса — источник цели включения разрешается там, где стоит
        # сам итератор (а не по первой строке включения): однострочная и многострочная формы (четвёртая перепроверка).
        for comprehension in (
            "    result = [m.get('line') for m in res['matches']]",
            "    result = [\n        m.get('line')\n        for m in res['matches']\n    ]",
        ):
            err = _run_error(
                hint_sandbox,
                f"res = read_procedure({_SB_MODULE!r}, 'А')\nclass C:\n    res = grep_read('Процедура')\n{comprehension}",
            )
            assert _GR_HINT in err and _RP_HINT not in err, (comprehension, err)
        # Первый итератор не видит целей своего же включения: `[x for x in x]` — внешний x.
        err = _run_error(hint_sandbox, f"x = read_procedure({_SB_MODULE!r}, 'А')\n[x.get('k') for x in x]")
        assert _GR_HINT not in err, err

    def test_free_name_in_function_is_read_at_call(self, hint_sandbox):
        """Внешнее имя в теле функции (и замыкания) читается при её ВЫЗОВЕ, а не по порядку строк: источник не
        доказан — подсказки конкретного хелпера нет (общестыковая вычитка Codex). Локальное имя — по порядку строк тела."""
        for code in (
            "res = grep_read('Процедура')\ndef inspect():\n    res.get('name')\n"
            f"res = read_procedure({_SB_MODULE!r}, 'А')\ninspect()",
            "def outer():\n    res = grep_read('Процедура')\n    def inspect():\n        res.get('name')\n"
            f"    res = read_procedure({_SB_MODULE!r}, 'А')\n    inspect()\nouter()",
            f"res = grep_read('Процедура')\ncheck = lambda: res.get('name')\nres = read_procedure({_SB_MODULE!r}, 'А')\n"
            "check()",
            # тело генераторного выражения — тоже при переборе (шестая перепроверка Codex)
            f"res = grep_read('Процедура')\ngen = (res.get('name') for _ in [0])\nres = read_procedure({_SB_MODULE!r}, 'А')\n"
            "list(gen)",
            "def outer():\n    res = grep_read('Процедура')\n    gen = (res.get('name') for _ in [0])\n"
            f"    res = read_procedure({_SB_MODULE!r}, 'А')\n    list(gen)\nouter()",
            # тело класса внутри функции исполняется при её вызове (седьмая перепроверка Codex)
            "res = grep_read('Процедура')\ndef inspect():\n    class C:\n        res.get('name')\n"
            f"res = read_procedure({_SB_MODULE!r}, 'А')\ninspect()",
            "def outer():\n    res = grep_read('Процедура')\n    def inspect():\n        class C:\n            res.get('name')\n"
            f"    res = read_procedure({_SB_MODULE!r}, 'А')\n    inspect()\nouter()",
            # класс в классе в функции; включение в теле класса в функции (кадр класса на 3.12, `<listcomp>` на 3.10)
            "res = grep_read('Процедура')\ndef inspect():\n    class A:\n        class B:\n            res.get('name')\n"
            f"res = read_procedure({_SB_MODULE!r}, 'А')\ninspect()",
            "res = grep_read('Процедура')\ndef inspect():\n    class C:\n        xs = [res.get('name') for _ in [0]]\n"
            f"res = read_procedure({_SB_MODULE!r}, 'А')\ninspect()",
        ):
            err = _run_error(hint_sandbox, code)
            assert _GR_HINT not in err, (code, err)
        err = _run_error(hint_sandbox, "def f():\n    r = grep_read('Процедура')\n    r['matches'][0]\nf()")
        assert _GR_HINT in err, err
        # Тело класса читает локальное имя охватывающей функции по порядку строк её тела; класс верхнего уровня
        # исполняется сразу — источник по порядку строк доказан.
        for code in (
            "def inspect():\n    res = grep_read('Процедура')\n    class C:\n        res['matches'][0]\ninspect()",
            f"res = grep_read('Процедура')\nclass C:\n    res['matches'][0]\nres = read_procedure({_SB_MODULE!r}, 'А')",
        ):
            err = _run_error(hint_sandbox, code)
            assert _GR_HINT in err, (code, err)
        # Первый итератор генератора вычисляется сразу, при объявлении: источник по порядку строк доказан.
        err = _run_error(
            hint_sandbox,
            f"res = grep_read('Процедура')\ngen = (r for r in res['matches'][0])\nres = read_procedure({_SB_MODULE!r}, 'А')",
        )
        assert _GR_HINT in err, err

    def test_get_with_default_is_not_transparent(self, hint_sandbox):
        """`.get(ключ, запасное)` может вернуть запасное значение — иной источник (ревью Codex); `.get(ключ)` — та же
        подписка."""
        err = _run_error(
            hint_sandbox, "res = grep_read('Процедура')\ntext = res.get('missing', 'ordinary string')\ntext.get('x')"
        )
        assert _GR_HINT not in err, err
        # `*args` может раскрыться в ключ и запасное значение — число аргументов не доказано (перепроверка Codex).
        err = _run_error(
            hint_sandbox,
            "res = grep_read('Процедура')\ntext = res.get(*['missing', 'ordinary string'])\ntext.get('x')",
        )
        assert _GR_HINT not in err, err
        err = _run_error(hint_sandbox, "res = grep_read('Процедура')\nfor f in res.get('matches'): f.get('line')")
        assert _GR_HINT in err, err

    def test_hint_detection_never_raises(self, hint_sandbox):
        """Распознавание формы значения не зовёт методы объекта агента и не роняет обработку ошибки (ревью Codex:
        подкласс dict с бросающим `__iter__` выносил исключение из execute, переменные сессии терялись)."""
        from rlm_tools_bsl.sandbox import _grep_read_shaped

        setup = "saved = 123\nclass Result(dict):\n    def __iter__(self):\n        raise RuntimeError('нет')\nres = Result()"
        assert hint_sandbox.execute(setup).error is None
        err = _run_error(hint_sandbox, "res[0]")
        assert "KeyError: 0" in err and _GR_HINT not in err, err
        assert hint_sandbox.execute("print(saved)").stdout == "123\n"
        assert _grep_read_shaped({"matches": {}, "files": {}, "summary": ""})
        assert not _grep_read_shaped(hint_sandbox._namespace["res"])
        # Обычный dict с ключами агента: их `__hash__`/`__eq__` распознавание тоже не зовёт (перепроверка Codex).
        keys = (
            "fail = [False]\nclass Key:\n    def __hash__(self):\n        if fail[0]:\n            raise RuntimeError('нет')\n"
            "        return 123\n    def __eq__(self, other):\n        return False\nres = {Key(): 1, Key(): 2, Key(): 3}\n"
            "fail[0] = True"
        )
        assert hint_sandbox.execute(keys).error is None
        err = _run_error(hint_sandbox, "res[0]")
        assert "KeyError: 0" in err and _GR_HINT not in err, err
        assert not _grep_read_shaped(hint_sandbox._namespace["res"])
        assert hint_sandbox.execute("print(saved)").stdout == "123\n"

    def test_hint_failure_keeps_the_traceback(self, hint_sandbox, monkeypatch):
        """Страховка: сбой самой диагностики — ответ с трассировкой без подсказок, а не исключение из execute."""
        from rlm_tools_bsl.sandbox import Sandbox

        def boom(*_a, **_k):
            raise RuntimeError("сбой диагностики")

        monkeypatch.setattr(Sandbox, "_add_error_hints", staticmethod(boom))
        err = _run_error(hint_sandbox, "{}[0]")
        assert "KeyError: 0" in err and "HINT" not in err, err

    def test_process_worker_keeps_state_after_hint_detection(self, tmp_path):
        """Режим процесса: та же ошибка — обычный ответ, воркер жив, переменные на месте (ревью Codex: прежде
        `worker_error`, перезапуск и `state_lost`)."""
        import time

        from rlm_tools_bsl.sandbox_process import ProcessBackendConfig, ProcessSandboxBackend, format_info_to_payload

        root = tmp_path / "cf"
        _write(root / "Configuration.xml", CF_DESCRIPTOR)
        backend = ProcessSandboxBackend(
            ProcessBackendConfig(
                base_path=str(root),
                max_output_chars=10_000,
                execution_timeout_seconds=30,
                format_info_payload=format_info_to_payload(detect_format(str(root))),
                memory_mb=0,
            )
        )
        try:
            for setup in (
                "class Result(dict):\n    def __iter__(self):\n        raise RuntimeError('нет')\nres = Result()",
                "fail = [False]\nclass Key:\n    def __hash__(self):\n        if fail[0]:\n            raise RuntimeError('нет')\n"
                "        return 123\n    def __eq__(self, other):\n        return False\nres = {Key(): 1, Key(): 2, Key(): 3}\n"
                "fail[0] = True",
            ):
                assert backend.execute("saved = 123\n" + setup).error is None
                failed = backend.execute("res[0]")
                assert "KeyError: 0" in (failed.error or "") and not failed.sandbox_state, failed
                assert backend.execute("print(saved)").stdout == "123\n"
        finally:
            backend.request_close("test_done")
            backend.finish_close(time.monotonic() + 10)

    def test_grep_read_signature_copies_equal(self):
        from rlm_tools_bsl.sandbox import _GENERIC_HELPER_SIGNATURES
        from rlm_tools_bsl.server import _FILE_HELPER_SIGNATURES

        (server_sig,) = [s for s in _FILE_HELPER_SIGNATURES if s.startswith("grep_read(")]
        assert server_sig == _GENERIC_HELPER_SIGNATURES["grep_read"]["sig"]
        assert "matches:{" in server_sig and "files:{" in server_sig


class TestIndexNudgeText:
    def test_redundant_get_index_info_names_missing_counters(self, hint_sandbox):
        res = hint_sandbox.execute("get_index_info()")
        (hint,) = [h for h in res.efficiency_hints or [] if h["id"] == "redundant_get_index_info"]
        for word in ("modules", "regions_count", "module_headers_count", "extension_overrides", "content_sample"):
            assert word in hint["message"], hint["message"]
        assert "counts уже" not in hint["message"] and "has_*/counts" not in hint["message"], hint["message"]

    def test_batching_lines_equal_and_name_get_index_info_counters(self):
        from rlm_tools_bsl.bsl_knowledge import _STRATEGY_HEADER
        from rlm_tools_bsl.bsl_strategy_data import STRATEGY_SECTIONS

        full = [line for line in _STRATEGY_HEADER.splitlines() if line.startswith("Не зови get_index_info")]
        slim = [
            line for line in STRATEGY_SECTIONS["batching"].splitlines() if line.startswith("Не зови get_index_info")
        ]
        assert full == slim and len(full) == 1, (full, slim)
        assert "regions_count" in full[0] and "counts уже" not in full[0] and "/counts" not in full[0], full

    def test_index_block_nudge(self):
        from rlm_tools_bsl.bsl_knowledge import _render_index_block

        block = _render_index_block({"methods": 1, "calls": 1, "builder_version": "18"}, None)
        nudge = [line for line in block.splitlines() if "get_index_info" in line]
        assert nudge and "rlm_start.index" in nudge[0] and "counts" not in nudge[0], nudge


# ═══════════════ Задача 13 (Д14) — база unique_* у get_overrides ═══════════════

_SESSION_MAIN = "Процедура УстановкаПараметровСеанса(Параметры)\nКонецПроцедуры\n"
_SESSION_EXT = '&После("УстановкаПараметровСеанса")\nПроцедура Расш1_Установка(Параметры)\nКонецПроцедуры\n'
# Док2: расширение перехватывает событие формы OnOpen, а в основной форме у него обработчика нет — цель пуста.
_EMPTY_TARGET_EXT_FORM_XML = (
    f'<?xml version="1.0" encoding="UTF-8"?>\n<Form {_CF_FORM_NS} version="2.20"><Events>'
    '<Event name="OnOpen" callType="Before">Расш1_ПриОткрытииПеред</Event></Events>'
    '<BaseForm version="2.20"><Events><Event name="OnCreateAtServer">ПриСозданииНаСервере</Event></Events>'
    "</BaseForm></Form>"
)
_EMPTY_TARGET_EXT_FORM_MODULE = "&НаКлиенте\nПроцедура Расш1_ПриОткрытииПеред(Отказ)\nКонецПроцедуры\n"


class TestOverridesUniqueBase:
    def test_recipe_names_unique_base(self, tmp_path):
        from rlm_tools_bsl.bsl_helpers import build_helper_metadata_snapshot

        recipe = build_helper_metadata_snapshot()["get_overrides"]["recipe"]
        assert any("unique_methods" in ln and "config_level_overrides" in ln for ln in recipe.splitlines()), recipe
        assert "130 имён" not in recipe
        # Строка с пустой целью названа: в имена и пары она не входит (агенты пересчитывали с ней).
        doc = _bsl_for(tmp_path)["get_overrides"].__doc__ or ""
        for text in (recipe, doc):
            assert "ВСЕХ строк" not in text and "пустой целью" in text and "target_method=''" in text, text

    @pytest.mark.parametrize("route", ["index", "live"])
    def test_unique_methods_counts_config_level_names(self, tmp_path, monkeypatch, route):
        """Закрепляет поведение: имя метода модуля сеанса входит в unique_methods, но не в строки с объектом."""
        main, ext = _override_tree(tmp_path, "cf", "\n")
        _write(main / "Ext" / "SessionModule.bsl", _SESSION_MAIN)
        _write(ext / "Ext" / "SessionModule.bsl", _SESSION_EXT)
        reader = _reader_for(main, monkeypatch) if route == "index" else None
        try:
            res = _override_helpers(main, ext, reader)["get_overrides"]()
        finally:
            if reader is not None:
                reader.close()
        assert res["config_level_overrides"] == 1, res
        object_names = {r["target_method"].lower() for r in res["overrides"] if r["object_name"]}
        assert object_names and res["unique_methods"] == len(object_names) + 1, res

    @pytest.mark.parametrize("route", ["index", "live"])
    def test_empty_target_row_counts_only_its_object(self, tmp_path, monkeypatch, route):
        """Закрепляет поведение: строка с пустой целью в unique_methods и unique_object_methods не входит, а её объект
        unique_objects считает (формы читаются только у CF)."""
        main, ext = _override_tree(tmp_path, "cf", "\n")
        form = "<ChildObjects><Form>ФормаДокумента</Form></ChildObjects>"
        _objects_tree(
            main,
            "cf",
            [
                (
                    "Documents",
                    "Док2",
                    {
                        "Forms/ФормаДокумента/Ext/Form.xml": _OV_MAIN_FORM_XML,
                        "Forms/ФормаДокумента/Ext/Form/Module.bsl": _OV_MAIN_FORM_MODULE,
                    },
                    {"cf_children": form},
                )
            ],
        )
        _objects_tree(
            ext,
            "cf",
            [
                (
                    "Documents",
                    "Док2",
                    {
                        "Forms/ФормаДокумента/Ext/Form.xml": _EMPTY_TARGET_EXT_FORM_XML,
                        "Forms/ФормаДокумента/Ext/Form/Module.bsl": _EMPTY_TARGET_EXT_FORM_MODULE,
                    },
                    {"cf_props": "<ObjectBelonging>Adopted</ObjectBelonging>", "cf_children": form},
                )
            ],
            CF_EXT_DESCRIPTOR,
        )
        reader = _reader_for(main, monkeypatch) if route == "index" else None
        try:
            res = _override_helpers(main, ext, reader)["get_overrides"]()
        finally:
            if reader is not None:
                reader.close()
        (empty,) = [r for r in res["overrides"] if not r["target_method"]]
        assert empty["object_name"] == "Док2" and empty["form_event"] == "OnOpen", empty
        with_target = [r for r in res["overrides"] if r["target_method"]]
        assert {r["object_name"] for r in with_target} == {"Док"}, res
        names = {r["target_method"].lower() for r in with_target}
        assert res["unique_objects"] == 2, res
        assert res["unique_methods"] == res["unique_object_methods"] == len(names) == 2, res


# ═══════════════ Приёмка, этап 1 e2e (решения владельца 2026-10-31): Н4, Н5, Н6 ═══════════════

_NEAREST_SPR = r"ближайшее по подстроке — (Catalogs/Спр1): {helper}\('\1'\)"


class TestNameWithoutObject:
    """Н5: ответ по объекту — только при точном имени или точном синониме, ссылка — строго в своей категории; иначе
    «не найден», а объект, которым прежде молча отвечал подстрочный ход каскада, — в `hint` исполнимым вызовом."""

    @pytest.mark.parametrize("helper", ["get_object_full_structure", "get_object_modules", "get_object_profile"])
    def test_fragment_is_not_found_with_nearest_route(self, world49, helper):
        bsl = world49[0]
        if helper == "get_object_profile" and bsl.route == "live":
            pytest.skip("без индекса профиль голого имени требует префикс (граница Д3)")
        res = bsl[helper]("Спр")
        assert res.get("error", "").startswith("Объект 'Спр'") and "не найден" in res["error"], res
        assert "object_name" not in res, res
        match = re.search(_NEAREST_SPR.format(helper=helper), res.get("hint") or "")
        assert match, res
        routed = bsl[helper](match.group(1))  # совет исполним: ближайший объект отвечает
        assert routed.get("object_name") == "Спр1" and "error" not in routed, routed

    def test_profile_ref_is_strict_in_its_category(self, world49):
        bsl = world49[0]
        res = bsl["get_object_profile"]("Документ.Спр1")  # документа нет, справочник `Спр1` есть
        assert res.get("error") == "Объект 'Документ.Спр1' не найден", res
        assert res["_meta"]["identity_source"] == "unresolved", res
        if bsl.route == "index":
            assert "объект с этим именем есть в Catalogs: get_object_profile('Catalogs/Спр1')" in res["hint"], res
        else:  # без индекса — проверка описателя на диске, совет — похожие объекты на диске
            assert res["hint"].startswith("проверьте имя: похожие на диске — ") and "Catalogs/Спр1" in res["hint"], res

    def test_profile_hybrid_keeps_previous_answer(self, world49):
        """Обе версии: гибрид — не ссылка с префиксом вида — прежний повтор без категории (решение 1.44.0)."""
        bsl = world49[0]
        if bsl.route == "live":
            pytest.skip("без индекса гибрид — прежняя идентичность из префикса")
        res = bsl["get_object_profile"]("Документ.Catalogs/Спр1")
        assert (res.get("category"), res.get("object_name")) == ("Catalogs", "Спр1"), res

    @pytest.mark.parametrize("helper", ["get_object_full_structure", "get_object_modules"])
    def test_existing_object_unchanged(self, world49, helper):
        """Обе версии: точное имя и ссылка существующего объекта — ответ объектом, без подсказки «не найден»."""
        bsl = world49[0]
        for arg in ("Спр1", "Справочник.Спр1"):
            res = bsl[helper](arg)
            assert res.get("object_name") == "Спр1" and "error" not in res, (arg, res)

    def test_document_flow_keeps_the_mark(self, world49):
        bsl = world49[0]
        res = bsl["analyze_document_flow"]("Спр")
        assert "объекта с именем 'Спр' нет; ответ — ближайший по подстроке: Catalogs/Спр1" in res.get("hint", ""), res
        missing = bsl["analyze_document_flow"]("НетТакогоОбъекта")
        assert missing.get("error") == "Объект 'НетТакогоОбъекта' не найден", missing
        assert "hint" not in bsl["analyze_document_flow"]("Документ.Док"), "у существующего документа подсказки нет"

    @pytest.mark.parametrize("fmt", ["cf", "edt"])
    def test_exact_synonym_resolves_to_its_object(self, tmp_path, monkeypatch, fmt):
        """Точный синоним — объект с этим бизнес-именем; прежде — первый по ранжированию поиска (справочник раньше
        документа при равном ранге), то есть чужой объект."""
        syn = _cf_syn if fmt == "cf" else _edt_syn
        root = _objects_tree(
            tmp_path / fmt,
            fmt,
            [
                (
                    "Documents",
                    "АвансовыйОтчет",
                    {"Ext/ObjectModule.bsl": PROC},
                    {"cf_props": syn("Авансовый отчет"), "edt": syn("Авансовый отчет")},
                ),
                (
                    "Catalogs",
                    "АвансовыйОтчетФайлы",
                    {"Ext/ObjectModule.bsl": PROC},
                    {"cf_props": syn("Авансовый отчет (файлы)"), "edt": syn("Авансовый отчет (файлы)")},
                ),
            ],
        )
        reader = _reader_for(root, monkeypatch)
        try:
            bsl = _bsl_for(root, reader)
            for helper in ("get_object_full_structure", "get_object_modules", "get_object_profile"):
                res = bsl[helper]("Авансовый отчет")
                assert (res.get("category"), res.get("object_name")) == ("Documents", "АвансовыйОтчет"), (helper, res)
        finally:
            reader.close()


class TestNameCheckWithoutSearch:
    """Н6: совет «search_objects('…')» исполним только при поиске по индексу (индекс своей базы с синонимами); без него
    подсказка называет похожие объекты на диске или их отсутствие — во всех пяти местах."""

    @staticmethod
    def _routes(bsl):
        return bsl.route == "index"

    def test_gate_refusal(self, world49):
        bsl = world49[0]
        with pytest.raises(ValueError) as similar:
            bsl["find_roles"]("Справочник.Сп")
        with pytest.raises(ValueError) as none:
            bsl["find_roles"]("Справочник.НетТакого")
        if self._routes(bsl):
            assert str(similar.value).endswith("проверьте имя: search_objects('Сп')"), similar.value
            # v1.50.0: прежде здесь стояло search_objects('НетТакого') — совет, исполнив который получишь пусто; поиск по
            # всему вводу пуст — фрагмент (две трети имени), пуст и он — «похожих имен нет» (П3).
            assert str(none.value).endswith("проверьте имя: похожих имен нет: search_objects('НетТак') пуст"), (
                none.value
            )
            return
        text = str(similar.value)
        assert "проверьте имя: похожие на диске — " in text, text
        refs = text.split("похожие на диске — ", 1)[1].split(", ")
        assert "Catalogs/Спр1" in refs and len(refs) <= 5, refs
        for ref in refs:  # совет исполним: каждое названное — существующий объект
            assert "error" not in bsl["get_object_full_structure"](ref), ref
        assert str(none.value).endswith("проверьте имя: похожих имен на диске нет"), none.value

    def test_overrides_and_data_path(self, world49):
        bsl = world49[0]
        hint = bsl["get_overrides"]("Спр")["hint"]
        path_hint = bsl["find_data_path"]("Справочник.Сп", "Документ.Док")["hint"]
        if self._routes(bsl):
            assert (
                hint.endswith("Проверьте имя: search_objects('Спр')")
                and path_hint == "проверьте имя: search_objects('Сп')"
            )
        else:
            assert "Проверьте имя: похожие на диске — Catalogs/Спр1" in hint, hint
            assert path_hint.startswith("проверьте имя: похожие на диске — Catalogs/Спр1"), path_hint

    def test_nearest_answer_tail(self, world49):
        bsl = world49[0]
        obj = bsl["analyze_object"]("ОбщМо")
        enum = bsl["find_enum_values"]("и")  # подстрока есть у обоих перечислений мира
        if self._routes(bsl):
            assert obj["hint"].endswith("точное имя — search_objects('ОбщМо')"), obj["hint"]
            assert enum["hint"].endswith("точное имя — search_objects('и')"), enum["hint"]
            return
        (other,) = {"ОбщМод", "ОбщМод2"} - {obj["name"]}
        assert obj["hint"].endswith(f"другие похожие на диске — CommonModules/{other}"), obj["hint"]
        (other_enum,) = {"ВидыОпераций", "СтавкиНДС"} - {enum["name"]}
        assert enum["hint"].endswith(f"другие похожие на диске — Enums/{other_enum}"), enum["hint"]
        assert bsl["find_enum_values"](other_enum)["name"] == other_enum  # совет исполним

    def test_index_without_synonyms_uses_disk(self, tmp_path, monkeypatch):
        """Индекс без синонимов: `search_objects` основную конфигурацию не видит — совет тоже с диска."""
        root = _objects_tree(tmp_path / "cf", "cf", _WORLD49_MAIN)
        ext_root = _objects_tree(tmp_path / "cfe" / "Расш1", "cf", _WORLD49_EXT, CF_EXT_DESCRIPTOR)
        reader = _reader_for(root, monkeypatch, build_synonyms=False)
        try:
            bsl = _world49_helpers(root, ext_root, reader)
            assert bsl["search_objects"]("Спр1") == []  # предпосылка: поиск по индексу пуст
            with pytest.raises(ValueError, match="проверьте имя: похожие на диске — Catalogs/Спр1"):
                bsl["find_roles"]("Справочник.Сп")
        finally:
            reader.close()


class TestGrepReadRecordShape:
    def test_signature_and_hint_name_the_record_keys(self, hint_sandbox):
        """Н4: одна форма записи `grep_read` в подписи (обе копии) и подсказке песочницы — ключи настоящей записи."""
        from rlm_tools_bsl.sandbox import _GENERIC_HELPER_SIGNATURES
        from rlm_tools_bsl.server import _FILE_HELPER_SIGNATURES

        res = hint_sandbox.execute("r = grep_read('Процедура')\nprint(sorted(next(iter(r['matches'].values()))[0]))")
        assert res.error is None and res.stdout.strip() == "['file', 'line', 'text']", res
        (server_sig,) = [s for s in _FILE_HELPER_SIGNATURES if s.startswith("grep_read(")]
        assert server_sig == _GENERIC_HELPER_SIGNATURES["grep_read"]["sig"]
        assert "-> {matches:{file:[{file,line,text}]}, files:{file:numbered}, summary}" in server_sig, server_sig
        hint = _run_error(hint_sandbox, "r['matches'][0]")
        assert "grep_read возвращает {matches: {файл: [{file, line, text}]}" in hint, hint


class TestExactSynonymFullSearch:
    """Ревью Codex правок Н5: точный синоним ищется по ПОЛНОМУ поиску — и за первыми 20 подстрочными совпадениями
    ридера (боевая CF: синоним «Обмен данными» подсистемы — 42-я строка), и среди синонимов объектов соседних
    расширений; иначе существующий объект объявлялся отсутствующим."""

    @pytest.mark.parametrize("fmt", ["cf", "edt"])
    def test_exact_synonym_beyond_first_page(self, tmp_path, monkeypatch, fmt):
        syn = _cf_syn if fmt == "cf" else _edt_syn
        rivals = [
            (
                "Catalogs",
                f"ЗаказФайлы{i:02d}",
                {"Ext/ObjectModule.bsl": PROC},
                {"cf_props": syn(f"Заказ (файлы {i:02d})"), "edt": syn(f"Заказ (файлы {i:02d})")},
            )
            for i in range(25)
        ]
        target = (
            "Documents",
            "ЦелевойДок",
            {"Ext/ObjectModule.bsl": PROC},
            {"cf_props": syn("Заказ"), "edt": syn("Заказ")},
        )
        root = _objects_tree(tmp_path / fmt, fmt, [*rivals, target])
        reader = _reader_for(root, monkeypatch)
        try:
            bsl = _bsl_for(root, reader)
            ranked = [(r["category"], r["object_name"]) for r in bsl["search_objects"]("Заказ", limit=1000)]
            assert ranked.index(("Documents", "ЦелевойДок")) >= 20, ranked  # предпосылка: точный синоним за страницей
            for helper in ("get_object_full_structure", "get_object_modules", "get_object_profile"):
                res = bsl[helper]("Заказ")
                assert (res.get("category"), res.get("object_name")) == ("Documents", "ЦелевойДок"), (helper, res)
        finally:
            reader.close()

    @pytest.mark.parametrize("route", ["index", "live"])
    def test_exact_synonym_of_extension_object(self, tmp_path, monkeypatch, route):
        main = _objects_tree(
            tmp_path / "cf",
            "cf",
            [("Catalogs", "Архив", {"Ext/ObjectModule.bsl": PROC}, {"cf_props": _cf_syn("Продажа товаров (архив)")})],
        )
        ext = _objects_tree(
            tmp_path / "cfe" / "Расш1",
            "cf",
            [("Documents", "Продажа", {"Ext/ObjectModule.bsl": PROC}, {"cf_props": _cf_syn("Продажа товаров")})],
            CF_EXT_DESCRIPTOR,
        )
        reader = _reader_for(main, monkeypatch) if route == "index" else None
        try:
            bsl = _world49_helpers(main, ext, reader)
            assert any(r["object_name"] == "Продажа" for r in bsl["search_objects"]("Продажа товаров"))  # предпосылка
            helpers = ("get_object_full_structure", "get_object_modules") + (("get_object_profile",) if reader else ())
            for helper in helpers:
                res = bsl[helper]("Продажа товаров")
                assert (res.get("category"), res.get("object_name")) == ("Documents", "Продажа"), (helper, res)
        finally:
            if reader is not None:
                reader.close()

    @pytest.mark.parametrize("fmt", ["cf", "edt"])
    def test_exact_synonym_of_object_without_modules(self, tmp_path, monkeypatch, fmt):
        """Точный синоним объекта без модулей: `get_object_modules` — «нет модулей» у этого объекта, а не «не найден»
        (боевая CF: синоним «Обмен данными» — подсистема `ОбменДанными`); структура и профиль — объект."""
        syn = _cf_syn if fmt == "cf" else _edt_syn
        root = _objects_tree(
            tmp_path / fmt,
            fmt,
            [
                ("Subsystems", "ОбменДанными", {}, {"cf_props": syn("Обмен данными"), "edt": syn("Обмен данными")}),
                (
                    "CommonModules",
                    "ОбменДаннымиСервер",
                    {"Ext/Module.bsl": PROC},
                    {"cf_props": syn("Обмен данными сервер"), "edt": syn("Обмен данными сервер")},
                ),
            ],
        )
        reader = _reader_for(root, monkeypatch)
        try:
            bsl = _bsl_for(root, reader)
            res = bsl["get_object_modules"]("Обмен данными")
            assert res.get("error", "").startswith("У объекта Subsystems/ОбменДанными нет модулей"), res
            for helper in ("get_object_full_structure", "get_object_profile"):
                got = bsl[helper]("Обмен данными")
                assert (got.get("category"), got.get("object_name")) == ("Subsystems", "ОбменДанными"), (helper, got)
        finally:
            reader.close()

    @pytest.mark.parametrize("fmt", ["cf", "edt"])
    def test_synonym_starting_with_kind_prefix_is_compared_whole(self, tmp_path, monkeypatch, fmt):
        """Повторное ревью Codex: `search_objects` отдаёт синоним уже без префикса вида — второе снятие резало синоним,
        который сам начинается с «Документ: »: полный синоним — «не найден», остаток — чужой объект."""
        syn = _cf_syn if fmt == "cf" else _edt_syn
        root = _objects_tree(
            tmp_path / fmt,
            fmt,
            [
                (
                    "Documents",
                    "АПервый",
                    {"Ext/ObjectModule.bsl": PROC},
                    {"cf_props": syn("Документ: Оплата услуг"), "edt": syn("Документ: Оплата услуг")},
                ),
                (
                    "Documents",
                    "ЯВерный",
                    {"Ext/ObjectModule.bsl": PROC},
                    {"cf_props": syn("Оплата услуг"), "edt": syn("Оплата услуг")},
                ),
            ],
        )
        reader = _reader_for(root, monkeypatch)
        try:
            bsl = _bsl_for(root, reader)
            for query, expected in (("Документ: Оплата услуг", "АПервый"), ("Оплата услуг", "ЯВерный")):
                for helper in ("get_object_full_structure", "get_object_modules", "get_object_profile"):
                    res = bsl[helper](query)
                    got = (res.get("category"), res.get("object_name"))
                    assert got == ("Documents", expected), (helper, query, res)
        finally:
            reader.close()


class TestDocumentFlowMarkScope:
    """Перепись гибридов правок Н5: композит переносит только пометку «объекта … нет; ответ — ближайший» и ошибку
    отсутствующего объекта; у гибрида — не ссылки с префиксом вида — ответ прежний, подсказка о модулях вне индекса у
    существующего объекта не переносится."""

    def test_hybrid_and_existing_object_keep_previous_answer(self, tmp_path):
        bsl = _build_world49(tmp_path, "cf-index", after_build=_add_after_build)
        try:
            for arg in (
                "Документ.Catalogs/Спр1",
                "Документ.Справочник.Спр1",
                "Справочник.Documents/Док",
                "Документ.Catalogs/Нет",
            ):
                res = bsl["analyze_document_flow"](arg)
                assert "error" not in res and "hint" not in res, (arg, res)
            assert "hint" not in bsl["analyze_document_flow"]("Документ.Док")
            res = bsl["analyze_document_flow"]("Спр")  # голое имя без объекта — пометка ближайшего переносится
            assert res["hint"].startswith("объекта с именем 'Спр' нет; ответ — ближайший по подстроке:"), res
        finally:
            bsl.reader.close()


def _flow_synonym_world(root: Path, fmt: str) -> Path:
    syn = _cf_syn if fmt == "cf" else _edt_syn
    return _objects_tree(
        root,
        fmt,
        [
            (
                "Documents",
                "ЗаказПокупателя",
                {"Ext/ObjectModule.bsl": PROC},
                {"cf_props": syn("Заказ покупателя"), "edt": syn("Заказ покупателя")},
            ),
            (
                "Catalogs",
                "Склады",
                {"Ext/ObjectModule.bsl": PROC},
                {"cf_props": syn("Места хранения"), "edt": syn("Места хранения")},
            ),
            (
                "Catalogs",
                "Номенклатура",
                {"Ext/ObjectModule.bsl": PROC},
                {"cf_props": syn("Номенклатура"), "edt": syn("Номенклатура")},
            ),
        ],
    )


class TestDocumentFlowExactSynonym:
    """Живая перепроверка правок Н5 (решение владельца): `analyze_document_flow` по точному синониму документа отвечает
    потоком этого документа с пометкой — как структура, профиль и модули; прежде — «не найден» (голое имя) или тихий
    пустой ответ (`Документ.<синоним>`). Синоним не документа — «не найден» с исполнимым маршрутом к объекту."""

    @pytest.mark.parametrize("fmt", ["cf", "edt"])
    def test_exact_synonym_answers_the_document_flow(self, tmp_path, monkeypatch, fmt):
        root = _flow_synonym_world(tmp_path / fmt, fmt)
        reader = _reader_for(root, monkeypatch)
        try:
            bsl = _bsl_for(root, reader)
            ref = _strip_times(bsl["analyze_document_flow"]("Документ.ЗаказПокупателя"))
            assert "error" not in ref and "hint" not in ref and ref["document"] == "ЗаказПокупателя", ref
            for arg, said in (
                ("Заказ покупателя", "Заказ покупателя"),
                ("заказ ПОКУПАТЕЛЯ", "заказ ПОКУПАТЕЛЯ"),
                ("Документ.Заказ покупателя", "Заказ покупателя"),
            ):
                res = _strip_times(bsl["analyze_document_flow"](arg))
                mark = f"объекта с именем {said!r} нет; ответ — по точному синониму: Documents/ЗаказПокупателя"
                assert res.get("hint") == mark, (arg, res)
                assert _without(res, {"hint"}) == ref, arg  # тот же ответ, что по ссылке на документ
        finally:
            reader.close()

    @pytest.mark.parametrize("fmt", ["cf", "edt"])
    def test_exact_synonym_of_non_document_is_not_found_with_route(self, tmp_path, monkeypatch, fmt):
        root = _flow_synonym_world(tmp_path / fmt, fmt)
        reader = _reader_for(root, monkeypatch)
        try:
            bsl = _bsl_for(root, reader)
            res = bsl["analyze_document_flow"]("Места хранения")
            assert res.get("error") == "Объект 'Места хранения' не найден", res
            route = (
                "точный синоним 'Места хранения' — Catalogs/Склады, не документ: get_object_profile('Catalogs/Склады')"
            )
            assert res.get("hint") == route, res
            got = bsl["get_object_profile"]("Catalogs/Склады", sections=["structure"])  # маршрут исполним
            assert (got.get("category"), got.get("object_name")) == ("Catalogs", "Склады"), got
        finally:
            reader.close()

    @pytest.mark.parametrize("route", ["index", "live"])
    def test_exact_synonym_of_extension_document(self, tmp_path, monkeypatch, route):
        main = _objects_tree(
            tmp_path / "cf",
            "cf",
            [("Catalogs", "Архив", {"Ext/ObjectModule.bsl": PROC}, {"cf_props": _cf_syn("Продажа товаров (архив)")})],
        )
        ext = _objects_tree(
            tmp_path / "cfe" / "Расш1",
            "cf",
            [("Documents", "Продажа", {"Ext/ObjectModule.bsl": PROC}, {"cf_props": _cf_syn("Продажа товаров")})],
            CF_EXT_DESCRIPTOR,
        )
        reader = _reader_for(main, monkeypatch) if route == "index" else None
        try:
            bsl = _world49_helpers(main, ext, reader)
            ref = _strip_times(bsl["analyze_document_flow"]("Документ.Продажа"))
            res = _strip_times(bsl["analyze_document_flow"]("Продажа товаров"))
            mark = "объекта с именем 'Продажа товаров' нет; ответ — по точному синониму: Documents/Продажа"
            assert res.get("hint") == mark, res
            assert _without(res, {"hint"}) == ref
        finally:
            if reader is not None:
                reader.close()

    @pytest.mark.parametrize("fmt", ["cf", "edt"])
    def test_names_references_and_hybrids_keep_previous_answer(self, tmp_path, monkeypatch, fmt):
        """Стражи: точное имя и ссылка документа, голое имя не-документа (точное имя важнее синонима), гибрид и имя
        без объекта — ответ прежний, синонимной пометки нет."""
        root = _flow_synonym_world(tmp_path / fmt, fmt)
        reader = _reader_for(root, monkeypatch)
        try:
            bsl = _bsl_for(root, reader)
            for arg in ("ЗаказПокупателя", "Документ.ЗаказПокупателя", "Documents/ЗаказПокупателя"):
                res = bsl["analyze_document_flow"](arg)
                assert "hint" not in res and "error" not in res and res["document"] == "ЗаказПокупателя", (arg, res)
            res = bsl["analyze_document_flow"]("Номенклатура")
            assert res["document"] == "Номенклатура" and "по точному синониму" not in str(res.get("hint")), res
            res = bsl["analyze_document_flow"]("Документ.Catalogs/Склады")
            assert "error" not in res and "hint" not in res, res
            res = bsl["analyze_document_flow"]("НетТакогоДокумента")
            assert res.get("error") == "Объект 'НетТакогоДокумента' не найден" and "hint" not in res, res
        finally:
            reader.close()

    @pytest.mark.parametrize("fmt", ["cf", "edt"])
    def test_non_document_synonym_with_fragment_match_is_not_found(self, tmp_path, monkeypatch, fmt):
        """Ревью Codex: синоним справочника ('Оплата'), который к тому же фрагмент имени документа (`ОплатаУслуг`), —
        «не найден» с маршрутом, а не поток ближайшего по подстроке документа."""
        syn = _cf_syn if fmt == "cf" else _edt_syn
        root = _objects_tree(
            tmp_path / fmt,
            fmt,
            [
                (
                    "Catalogs",
                    "СпособыРасчетов",
                    {"Ext/ObjectModule.bsl": PROC},
                    {"cf_props": syn("Оплата"), "edt": syn("Оплата")},
                ),
                (
                    "Documents",
                    "ОплатаУслуг",
                    {"Ext/ObjectModule.bsl": PROC},
                    {"cf_props": syn("Оплата услуг"), "edt": syn("Оплата услуг")},
                ),
            ],
        )
        reader = _reader_for(root, monkeypatch)
        try:
            bsl = _bsl_for(root, reader)
            res = _strip_times(bsl["analyze_document_flow"]("Оплата"))
            assert res.get("error") == "Объект 'Оплата' не найден", res
            route = "точный синоним 'Оплата' — Catalogs/СпособыРасчетов, не документ: get_object_profile('Catalogs/СпособыРасчетов')"
            assert res.get("hint") == route, res
            assert res["document"] == "Оплата" and res["metadata"] == {} and res["event_subscriptions"] == [], res
            assert "ОплатаУслуг" not in _dump(res), res  # ни метаданных, ни модулей ближайшего документа
        finally:
            reader.close()


class TestExactNameOverSynonym:
    """Ревью Codex: точное имя объекта, которого каскад не видит (только описатель: общая картинка, вложенная подсистема
    без синонима), старше точного синонима другого объекта — у всех четырех хелперов Н5; прежде с индексом отвечал
    документ с этим синонимом. Ссылка с категорией ищет синоним в своей категории — как прежде."""

    def _world(self, root: Path, fmt: str) -> Path:
        syn = _cf_syn if fmt == "cf" else _edt_syn
        return _objects_tree(
            root,
            fmt,
            [
                ("CommonPictures", "Логотип", {}, {}),
                (
                    "Documents",
                    "Печать",
                    {"Ext/ObjectModule.bsl": PROC},
                    {"cf_props": syn("Логотип"), "edt": syn("Логотип")},
                ),
                ("Subsystems", "Верх", {}, {"cf_props": syn("Верхняя"), "edt": syn("Верхняя")}),
                ("Subsystems", "Верх/Вложенная", {}, {}),
                (
                    "Documents",
                    "Дублер",
                    {"Ext/ObjectModule.bsl": PROC},
                    {"cf_props": syn("Вложенная"), "edt": syn("Вложенная")},
                ),
            ],
        )

    @pytest.mark.parametrize("fmt", ["cf", "edt"])
    def test_exact_descriptor_name_beats_document_synonym(self, tmp_path, monkeypatch, fmt):
        root = self._world(tmp_path / fmt, fmt)
        reader = _reader_for(root, monkeypatch)
        try:
            bsl = _bsl_for(root, reader)
            for arg, want in (
                ("Логотип", ("CommonPictures", "Логотип")),
                ("логотип", ("CommonPictures", "Логотип")),
                ("Вложенная", ("Subsystems", "Вложенная")),
            ):
                for helper in ("get_object_full_structure", "get_object_profile"):
                    got = bsl[helper](arg)
                    assert (got.get("category"), got.get("object_name")) == want, (helper, arg, got)
                mods = bsl["get_object_modules"](arg)
                assert mods.get("error", "").startswith(f"У объекта {want[0]}/{want[1]} нет модулей"), (arg, mods)
                flow = bsl["analyze_document_flow"](arg)
                assert flow["document"] == want[1] and "по точному синониму" not in str(flow.get("hint")), (arg, flow)
            got = bsl["get_object_full_structure"]("Документ.Логотип")  # ссылка: синоним в своей категории
            assert (got.get("category"), got.get("object_name")) == ("Documents", "Печать"), got
        finally:
            reader.close()

    @pytest.mark.parametrize("fmt", ["cf", "edt"])
    def test_index_only_name_beats_synonym_in_any_case(self, tmp_path, fmt):
        """Перепроверка Codex: объект только в индексе (описатель удален после сборки, синонима нет) — ввод в другом
        регистре отвечает как в регистре объекта, а не документом, у которого это имя — синоним."""
        syn = _cf_syn if fmt == "cf" else _edt_syn
        root = _objects_tree(
            tmp_path / fmt,
            fmt,
            [
                ("Catalogs", "ТолькоИндекс", {}, {}),
                (
                    "Documents",
                    "ДублерИндекса",
                    {"Ext/ObjectModule.bsl": PROC},
                    {"cf_props": syn("ТолькоИндекс"), "edt": syn("ТолькоИндекс")},
                ),
                ("EventSubscriptions", "ПодпискаИндекса", {}, {}),
                (
                    "Documents",
                    "ДублерПодписки",
                    {"Ext/ObjectModule.bsl": PROC},
                    {"cf_props": syn("ПодпискаИндекса"), "edt": syn("ПодпискаИндекса")},
                ),
            ],
        )
        db = _build_index(root, tmp_path / "idx")
        for category, name in (("Catalogs", "ТолькоИндекс"), ("EventSubscriptions", "ПодпискаИндекса")):
            (root / category / f"{name}.xml").unlink(missing_ok=True)
            shutil.rmtree(root / category / name, ignore_errors=True)
        reader = IndexReader(str(db))
        try:
            bsl = _bsl_for(root, reader)
            for exact, other in (("ТолькоИндекс", "толькоиндекс"), ("ПодпискаИндекса", "подпискаиндекса")):
                for helper in ("get_object_full_structure", "get_object_profile", "get_object_modules"):
                    want, got = bsl[helper](exact), bsl[helper](other)
                    pair = (got.get("category"), got.get("object_name"))
                    assert pair == (want.get("category"), want.get("object_name")), (helper, other, got)
                    assert "Дублер" not in str(pair), (helper, other, got)
                flow = bsl["analyze_document_flow"](other)
                assert flow["document"] == other and "по точному синониму" not in str(flow.get("hint")), flow
        finally:
            reader.close()

    @pytest.mark.parametrize("fmt", ["cf", "edt"])
    @pytest.mark.parametrize("index_only", ["Documents", "Catalogs"])
    def test_mixed_source_homonym_keeps_category_in_any_case(self, tmp_path, fmt, index_only):
        """Вторая перепроверка Codex: омоним справочника и документа, один из которых остался только в индексе, — ввод
        в другом регистре выбирает ту же категорию, что ввод в регистре объектов (порядок каскада), а не объект без
        описателя и не документ, у которого это имя — синоним."""
        syn = _cf_syn if fmt == "cf" else _edt_syn
        root = _objects_tree(
            tmp_path / fmt,
            fmt,
            [
                ("Catalogs", "Омоним", {}, {}),
                ("Documents", "Омоним", {}, {}),
                (
                    "Documents",
                    "Дублер",
                    {"Ext/ObjectModule.bsl": PROC},
                    {"cf_props": syn("Омоним"), "edt": syn("Омоним")},
                ),
            ],
        )
        db = _build_index(root, tmp_path / "idx")
        (root / index_only / "Омоним.xml").unlink(missing_ok=True)
        shutil.rmtree(root / index_only / "Омоним", ignore_errors=True)
        reader = IndexReader(str(db))
        try:
            bsl = _bsl_for(root, reader)
            for helper in ("get_object_full_structure", "get_object_profile", "get_object_modules"):
                want, got = bsl[helper]("Омоним"), bsl[helper]("омоним")
                pair = (got.get("category"), got.get("object_name"))
                assert pair == (want.get("category"), want.get("object_name")), (helper, index_only, got)
                assert str(got.get("error", "")).replace("омоним", "Омоним") == str(want.get("error", "")), (
                    helper,
                    got,
                )
            for helper in ("get_object_full_structure", "get_object_profile"):
                got = bsl[helper]("омоним")
                assert (got.get("category"), got.get("object_name")) == ("Catalogs", "Омоним"), (
                    helper,
                    index_only,
                    got,
                )
        finally:
            reader.close()

    @pytest.mark.parametrize("fmt", ["cf", "edt"])
    def test_foreign_index_does_not_resolve_name_in_other_base(self, tmp_path, fmt):
        """Третья перепроверка Codex: при индексе ДРУГОЙ базы повтор Pass 2 написанием объекта не берет список файлов
        этого индекса (публичный поиск его строки исключает) — точное имя объекта расширения сессии старше синонима и
        объекта чужой конфигурации."""
        syn = _cf_syn if fmt == "cf" else _edt_syn
        main = _objects_tree(tmp_path / fmt / "cf", fmt, [("CommonModules", "Якорь", {"Ext/Module.bsl": PROC}, {})])
        ext = _objects_tree(
            tmp_path / fmt / "cfe" / "Расш1",
            fmt,
            [
                ("Catalogs", "Омоним", {}, {}),
                (
                    "Documents",
                    "Дублер",
                    {"Ext/ObjectModule.bsl": PROC},
                    {"cf_props": syn("Омоним"), "edt": syn("Омоним")},
                ),
            ],
            CF_EXT_DESCRIPTOR if fmt == "cf" else EDT_EXT_DESCRIPTOR,
        )
        other_descriptor = (
            CF_DESCRIPTOR.replace("Тест", "Чужая").replace("000000000001", "000000000003")
            if fmt == "cf"
            else EDT_DESCRIPTOR.replace('name="Тест"', 'name="Чужая"')
        )
        other = _objects_tree(tmp_path / fmt / "other", fmt, [("Documents", "Омоним", {}, {})], other_descriptor)
        reader = IndexReader(str(_build_index(other, tmp_path / fmt / "idx")))
        try:
            bsl = _world49_helpers(main, ext, reader)
            found = [(r["category"], r["object_name"]) for r in bsl["search_objects"]("омоним", limit=1000)]
            assert ("Documents", "Омоним") not in found, found  # предпосылка: чужая база исключена из поиска
            for helper in ("get_object_full_structure", "get_object_profile"):
                got = bsl[helper]("омоним")
                assert (got.get("category"), got.get("object_name")) == ("Catalogs", "Омоним"), (helper, got)
        finally:
            reader.close()
