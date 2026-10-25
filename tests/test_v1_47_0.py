"""v1.47.0 — индекс не пропускает написанное.

Релиз с бампом поколения индекса (BUILDER_VERSION 17 → 18). Приватные фикстуры других файлов не
импортируются (каталог tests не пакет) — деревья собираются здесь же.

Тесты «обе версии» проходят и на HEAD `0a84975`: прогон против выгрузки HEAD —
``PYTHONPATH=<выгрузка>/src uv run pytest tests/test_v1_47_0.py …``.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import textwrap
import types
from pathlib import Path
from typing import Callable

import pytest

import rlm_tools_bsl.bsl_index as BI
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


def _strip_times(obj):
    """Копия ответа без ``elapsed_ms``/``total_elapsed_ms`` на любой глубине."""
    if isinstance(obj, dict):
        return {k: _strip_times(v) for k, v in obj.items() if k not in ("elapsed_ms", "total_elapsed_ms")}
    if isinstance(obj, list):
        return [_strip_times(v) for v in obj]
    if isinstance(obj, tuple):
        return tuple(_strip_times(v) for v in obj)
    return obj


# ═══════════════ Задача 0 — каркас: выборки, update ≡ build, стандартный мир world18 ═══════════════


def _rows(db_path: str, sql: str, params: tuple = ()) -> list[tuple]:
    """Отсортированные строки выборки; ключ устойчив к ``None`` (как у ``_edge_tuples_v17``)."""
    con = sqlite3.connect(str(db_path))
    try:
        rows = [tuple(r) for r in con.execute(sql, params)]
    finally:
        con.close()
    return sorted(rows, key=lambda row: tuple((value is not None, value) for value in row))


def _write_tree(root: Path, files: dict[str, str]) -> None:
    """Дерево «путь → текст» от корня основной конфигурации; ``../cfe/<Имя>/…`` — расширение рядом."""
    for rel, text in files.items():
        _write(Path(os.path.normpath(root / rel)), text)


def _apply_changes(root: Path, files_a: dict[str, str], files_b: dict[str, str]) -> list[Path]:
    """Превратить на диске дерево A в B; mtime правленых файлов сдвигается — полный скан `update`
    сравнивает mtime с допуском в секунду, а правка тех же размеров иначе осталась бы незамеченной."""
    touched: list[Path] = []
    for rel in set(files_a) - set(files_b):
        Path(os.path.normpath(root / rel)).unlink()
    for rel, text in files_b.items():
        if files_a.get(rel) == text:
            continue
        path = Path(os.path.normpath(root / rel))
        _write(path, text)
        touched.append(path)
    for path in touched:
        st = path.stat()
        os.utime(path, (st.st_atime + 10, st.st_mtime + 10))
    return touched


def _changed_categories(files_a: dict[str, str], files_b: dict[str, str]) -> set[str]:
    """Категории основной конфигурации, чьи файлы правка трогает (первый сегмент пути)."""
    keys = {rel for rel in set(files_a) | set(files_b) if files_a.get(rel) != files_b.get(rel)}
    return {rel.split("/", 1)[0] for rel in keys if not rel.startswith("../")}


def _assert_update_equals_build(
    tmp_path_factory,
    monkeypatch,
    write: Callable[[Path, dict[str, str]], None],
    files_a: dict[str, str],
    files_b: dict[str, str],
    queries: dict[str, str],
    *,
    route: str = "full",
    expect_pointwise: tuple[str, str] | None = None,
    check_route: bool = True,
) -> str:
    """Собрать A, превратить на диске в B, ``update`` — каждая выборка ``queries`` равна свежей сборке B.

    ``route='full'`` — каталог вне git (полный скан); ``route='git'`` — ``git init``, коммит A, B —
    незакоммиченные правки, утверждается ``git_fast_path``. Маршрут git-пути утверждается шпионами
    ``_refresh_object``/``_refresh_global_object``: ``expect_pointwise=(категория, объект)`` — такой вызов
    обязан быть (точечный путь Group A/B), ``None`` — шпион не вызван ни для одной правленой категории
    (bulk-fallback); ``check_route=False`` — маршрут не утверждается (правка формы: CF-форма — ещё и XML
    объекта, EDT-форма — нет). Возвращает путь БД после ``update``."""
    assert route in ("full", "git"), route
    repo = tmp_path_factory.mktemp(f"upd_{route}") / "repo"
    base = repo / "cf"
    write(base, files_a)
    if route == "git":
        from test_git_delta import _git_init

        _git_init(repo)
    db = IndexBuilder().build(str(base), build_calls=True)
    _apply_changes(base, files_a, files_b)

    calls: list[tuple[str, str]] = []
    real_object, real_global = BI._refresh_object, BI._refresh_global_object

    def spy_object(conn, base_path, category, object_name, opt):
        calls.append((category, object_name))
        return real_object(conn, base_path, category, object_name, opt)

    def spy_global(conn, base_path, category, object_name, opt):
        calls.append((category, object_name))
        return real_global(conn, base_path, category, object_name, opt)

    with monkeypatch.context() as m:
        m.setattr(BI, "_refresh_object", spy_object)
        m.setattr(BI, "_refresh_global_object", spy_global)
        res = IndexBuilder().update(str(base))
    if route == "git":
        assert res["git_fast_path"] is True, res
        if not check_route:
            pass
        elif expect_pointwise is not None:
            assert expect_pointwise in calls, (expect_pointwise, calls)
        else:
            touched = _changed_categories(files_a, files_b)
            assert not [c for c in calls if c[0] in touched], (touched, calls)
    else:
        assert res["git_fast_path"] is False, res

    fresh = tmp_path_factory.mktemp(f"fresh_{route}") / "repo" / "cf"
    write(fresh, files_b)
    db_fresh = IndexBuilder().build(str(fresh), build_calls=True)
    for name, sql in queries.items():
        assert _rows(db, sql) == _rows(db_fresh, sql), name
    return str(db)


# ── Описатели world18: многострочный XML, как в выгрузках (якорь `line` ссылок — строка тега) ──


def _cf_syn(text: str) -> str:
    return f"<Synonym><v8:item><v8:lang>ru</v8:lang><v8:content>{text}</v8:content></v8:item></Synonym>"


def _edt_syn(text: str) -> str:
    return f"<synonym><key>ru</key><value>{text}</value></synonym>"


def _cf_type(types=(), sets=()) -> str:
    inner = [f"<v8:Type>{t}</v8:Type>" for t in types] + [f"<v8:TypeSet>{t}</v8:TypeSet>" for t in sets]
    return "\n".join(["<Type>", *inner, "</Type>"]) if inner else "<Type/>"


def _cf_child(tag: str, name: str, types=(), sets=(), extra=()) -> str:
    """Реквизит/измерение/ресурс/реквизит адресации описателя CF."""
    return "\n".join(
        [f"<{tag}>", "<Properties>", f"<Name>{name}</Name>", "<Synonym/>", _cf_type(types, sets), *extra]
        + ["</Properties>", f"</{tag}>"]
    )


def _cf_command(name: str, types=(), sets=()) -> str:
    """Команда объекта в описателе владельца CF: ``<ChildObjects><Command><Properties><CommandParameterType>``."""
    inner = [f"<v8:Type>{t}</v8:Type>" for t in types] + [f"<v8:TypeSet>{t}</v8:TypeSet>" for t in sets]
    param = (
        "\n".join(["<CommandParameterType>", *inner, "</CommandParameterType>"]) if inner else "<CommandParameterType/>"
    )
    return "\n".join(
        ["<Command>", "<Properties>", f"<Name>{name}</Name>", "<Synonym/>", param, "</Properties>", "</Command>"]
    )


def _cf_obj(kind: str, name: str, *, synonym: str = "", props=(), children=()) -> str:
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f"<MetaDataObject {_CF_MD_NS}>",
        f"<{kind}>",
        "<Properties>",
        f"<Name>{name}</Name>",
        _cf_syn(synonym) if synonym else "<Synonym/>",
        *props,
        "</Properties>",
    ]
    if children:
        lines += ["<ChildObjects>", *children, "</ChildObjects>"]
    lines += [f"</{kind}>", "</MetaDataObject>"]
    return "\n".join(lines) + "\n"


def _edt_typed(tag: str, name: str, types=()) -> str:
    inner = [f"<types>{t}</types>" for t in types]
    return "\n".join([f"<{tag}>", f"<name>{name}</name>", "<type>", *inner, "</type>", f"</{tag}>"])


def _edt_command(name: str, types=()) -> str:
    inner = [f"<types>{t}</types>" for t in types]
    param = "\n".join(["<commandParameterType>", *inner, "</commandParameterType>"]) if inner else ""
    return "\n".join(["<commands>", f"<name>{name}</name>", param, "</commands>"])


def _edt_obj(kind: str, name: str, *, synonym: str = "", body=()) -> str:
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<mdclass:{kind} {_EDT_MD_NS} xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">',
        f"<name>{name}</name>",
    ]
    if synonym:
        lines.append(_edt_syn(synonym))
    lines += [*body, f"</mdclass:{kind}>"]
    return "\n".join(lines) + "\n"


def _cf_ref_items(tag: str, refs) -> str:
    items = [f'<xr:Item xsi:type="xr:MDObjectRef">{r}</xr:Item>' for r in refs]
    return "\n".join([f"<{tag}>", *items, f"</{tag}>"])


# Модули world18. Модуль объекта `Док`: квалифицированные вызовы, разорванные переносом или пробелом
# (Задача 12), своя одноимённая процедура `Экспорт1`, делегат движений «регистр по имени» (Задача 13).
_DOC_OBJECT_MODULE = """\
Процедура ОбработкаПроведения(Отказ, Режим)
	ОбщМод.
		Экспорт1(1);
	ОбщМод. Экспорт1(2);
	ОбщМод
		.Экспорт1(3);
	ОбщМод.Экспорт1
		(4);
	ПроведениеСервер.ОтразитьДвиженияПоРегистру(ДополнительныеСвойства, Движения, Отказ, "Рег");
КонецПроцедуры

Процедура Экспорт1(Параметр)
КонецПроцедуры
"""
# Модуль менеджера `Док`: обращения в коде (Задача 11), таблицы движений БГУ (Задача 13), механизмы
# ERP своим литералом и через делегат общего модуля (Задача 14).
_DOC_MANAGER_MODULE = """\
Процедура ИнициализироватьДанныеДокумента(ДокументСсылка, ДополнительныеСвойства) Экспорт
	Т = Неопределено;
	ТаблицыДляДвижений = ДополнительныеСвойства.ТаблицыДляДвижений;
	ТаблицыЗапроса = Новый Массив;
	ТаблицыДляДвижений.Вставить("Рег", Т);
	ТаблицыЗапроса.Добавить("Рег2");
	ТаблицыДляДвижений.Вставить("ТаблицаНеРегистр", Т);
КонецПроцедуры

Процедура Прочее() Экспорт
	Д = Документы.Док.СоздатьДокумент();
	Имя = Метаданные.Документы.Док.ПолноеИмя();
	ОткрытьФорму("Документ.Док.Форма.ФормаДокумента");
	ПолучитьФорму("Справочник.Спр.ФормаВыбора");
	Текст = "ВЫБРАТЬ * ИЗ Документ.Док.Товары";
КонецПроцедуры

Процедура ЗарегистрироватьУчетныеМеханизмы(МеханизмыДокумента) Экспорт
	МеханизмыДокумента.Добавить("Мех1");
	ДокЛокализация.ЗарегистрироватьУчетныеМеханизмы(МеханизмыДокумента);
КонецПроцедуры
"""
_DOC_FORM_MODULE = """\
&НаСервере
Процедура ПриСозданииНаСервере(Отказ, СтандартнаяОбработка)
КонецПроцедуры

&НаКлиенте
Процедура ПриОткрытии(Отказ)
КонецПроцедуры

&НаКлиенте
Процедура Команда1(Команда)
КонецПроцедуры
"""
_COMMAND_MODULE = (
    "&НаКлиенте\nПроцедура ОбработкаКоманды(ПараметрКоманды, ПараметрыВыполненияКоманды)\nКонецПроцедуры\n"
)
_PROC = "Процедура П() Экспорт\nКонецПроцедуры\n"
# Модуль формы расширения: три обработчика `callType` без аннотаций, перехват аннотацией, вызовы с
# переносом после точки и с пробелом перед точкой (живой слой вызовов расширений, Задачи 12 и 18).
_EXT_FORM_MODULE = """\
&НаСервере
Процедура Расш1_ПриСозданииПосле(Отказ, СтандартнаяОбработка)
КонецПроцедуры

&НаКлиенте
Процедура Расш1_Команда1Вместо(Команда)
КонецПроцедуры

&НаКлиенте
Процедура Расш1_НетВБазеПеред(Команда)
КонецПроцедуры

&НаКлиенте
&После("ПриОткрытии")
Процедура Расш1_ПриОткрытииПосле(Отказ)
	ОбщМод.
		Экспорт1(5);
КонецПроцедуры

&НаКлиенте
Процедура Расш1_ПробелПередТочкой()
	ОбщМод .Экспорт1(6);
КонецПроцедуры

&НаКлиенте
Процедура Расш1_СуммаПриИзмененииПеред(Элемент)
КонецПроцедуры

&НаКлиенте
Процедура Расш1_ПолеПриИзмененииПосле(Элемент)
КонецПроцедуры

&НаКлиенте
Процедура Расш1_СуммаКомандаВместо(Команда)
КонецПроцедуры
"""

# Текст запроса динамического списка: объект в комментарии и в литерале запроса ссылкой не считается.
_LIST_QUERY = """\
ВЫБРАТЬ
	С.Ссылка КАК Ссылка,
	"Документ.Док" КАК Вид // Документ.Док
ИЗ
	Справочник.Спр КАК С
		ЛЕВОЕ СОЕДИНЕНИЕ РегистрНакопления.Рег.Остатки() КАК О
		ПО ИСТИНА"""

_CF_FORM_NS = (
    'xmlns="http://v8.1c.ru/8.3/xcf/logform" xmlns:v8="http://v8.1c.ru/8.1/data/core" '
    'xmlns:cfg="http://v8.1c.ru/8.1/data/enterprise/current-config" '
    'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'
)


def _cf_doc_form() -> str:
    return "\n".join(
        [
            '<?xml version="1.0" encoding="UTF-8"?>',
            f'<Form {_CF_FORM_NS} version="2.20">',
            "\t<Events>",
            '\t\t<Event name="OnCreateAtServer">ПриСозданииНаСервере</Event>',
            '\t\t<Event name="OnOpen">ПриОткрытии</Event>',
            "\t</Events>",
            "\t<ChildItems>",
            '\t\t<InputField name="Сумма" id="1">',
            "\t\t\t<DataPath>Сумма</DataPath>",
            "\t\t</InputField>",
            "\t</ChildItems>",
            "\t<Attributes>",
            '\t\t<Attribute name="Объект" id="1">',
            "\t\t\t<Type>",
            "\t\t\t\t<v8:Type>cfg:DocumentObject.Док</v8:Type>",
            "\t\t\t</Type>",
            "\t\t\t<MainAttribute>true</MainAttribute>",
            "\t\t</Attribute>",
            '\t\t<Attribute name="Сумма" id="2">',
            "\t\t\t<Type>",
            "\t\t\t\t<v8:TypeSet>cfg:DefinedType.ДенежнаяСумма</v8:TypeSet>",
            "\t\t\t</Type>",
            "\t\t</Attribute>",
            '\t\t<Attribute name="Таблица" id="3">',
            "\t\t\t<Type>",
            "\t\t\t\t<v8:Type>v8:ValueTable</v8:Type>",
            "\t\t\t</Type>",
            "\t\t\t<Columns>",
            '\t\t\t\t<Column name="Спр" id="1">',
            "\t\t\t\t\t<Type>",
            "\t\t\t\t\t\t<v8:Type>cfg:CatalogRef.Спр</v8:Type>",
            "\t\t\t\t\t</Type>",
            "\t\t\t\t</Column>",
            "\t\t\t</Columns>",
            "\t\t</Attribute>",
            '\t\t<Attribute name="Список" id="4">',
            "\t\t\t<Type>",
            "\t\t\t\t<v8:Type>cfg:DynamicList</v8:Type>",
            "\t\t\t</Type>",
            '\t\t\t<Settings xsi:type="DynamicList">',
            "\t\t\t\t<ManualQuery>true</ManualQuery>",
            f"\t\t\t\t<QueryText>{_LIST_QUERY}</QueryText>",
            "\t\t\t\t<MainTable>Catalog.Спр</MainTable>",
            "\t\t\t</Settings>",
            "\t\t</Attribute>",
            '\t\t<Attribute name="Произвольный" id="5">',
            "\t\t\t<Type/>",
            "\t\t</Attribute>",
            "\t</Attributes>",
            "\t<Parameters>",
            '\t\t<Parameter name="Основание">',
            "\t\t\t<Type>",
            "\t\t\t\t<v8:Type>cfg:DocumentRef.Док</v8:Type>",
            "\t\t\t</Type>",
            "\t\t</Parameter>",
            "\t</Parameters>",
            "\t<Commands>",
            '\t\t<Command name="Команда1" id="1">',
            "\t\t\t<Action>Команда1</Action>",
            "\t\t</Command>",
            "\t</Commands>",
            "</Form>",
        ]
    )


def _edt_doc_form() -> str:
    return "\n".join(
        [
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<form:Form xmlns:form="http://g5.1c.ru/v8/dt/form" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">',
            '  <items xsi:type="form:FormField">',
            "    <name>Сумма</name>",
            '    <dataPath xsi:type="form:DataPath">',
            "      <segments>Сумма</segments>",
            "    </dataPath>",
            "  </items>",
            "  <handlers>",
            "    <event>OnCreateAtServer</event>",
            "    <name>ПриСозданииНаСервере</name>",
            "  </handlers>",
            "  <handlers>",
            "    <event>OnOpen</event>",
            "    <name>ПриОткрытии</name>",
            "  </handlers>",
            "  <attributes>",
            "    <name>Объект</name>",
            "    <valueType>",
            "      <types>DocumentObject.Док</types>",
            "    </valueType>",
            "    <main>true</main>",
            "  </attributes>",
            "  <attributes>",
            "    <name>Сумма</name>",
            "    <valueType>",
            "      <types>DefinedType.ДенежнаяСумма</types>",
            "    </valueType>",
            "  </attributes>",
            "  <attributes>",
            "    <name>Таблица</name>",
            "    <valueType>",
            "      <types>ValueTable</types>",
            "    </valueType>",
            "    <columns>",
            "      <name>Спр</name>",
            "      <valueType>",
            "        <types>CatalogRef.Спр</types>",
            "      </valueType>",
            "    </columns>",
            "  </attributes>",
            "  <attributes>",
            "    <name>Список</name>",
            "    <valueType>",
            "      <types>DynamicList</types>",
            "    </valueType>",
            '    <extInfo xsi:type="form:DynamicListExtInfo">',
            f"      <queryText>{_LIST_QUERY}</queryText>",
            "      <mainTable>Catalog.Спр</mainTable>",
            "    </extInfo>",
            "  </attributes>",
            "  <parameters>",
            "    <name>Основание</name>",
            "    <valueType>",
            "      <types>DocumentRef.Док</types>",
            "    </valueType>",
            "  </parameters>",
            "  <formCommands>",
            "    <name>Команда1</name>",
            '    <action xsi:type="form:FormCommandHandlerContainer">',
            "      <handler>",
            "        <name>Команда1</name>",
            "      </handler>",
            "    </action>",
            "  </formCommands>",
            "</form:Form>",
        ]
    )


def _small_form(cf: bool, attr: str, ref_type: str) -> str:
    """Форма с одним реквизитом ссылочного типа (CF ``Ext/Form.xml`` либо EDT ``Form.form``)."""
    if cf:
        return "\n".join(
            [
                '<?xml version="1.0" encoding="UTF-8"?>',
                f'<Form {_CF_FORM_NS} version="2.20">',
                "\t<Attributes>",
                f'\t\t<Attribute name="{attr}" id="1">',
                f"\t\t\t<Type>\n\t\t\t\t<v8:Type>cfg:{ref_type}</v8:Type>\n\t\t\t</Type>",
                "\t\t</Attribute>",
                "\t</Attributes>",
                "</Form>",
            ]
        )
    return "\n".join(
        [
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<form:Form xmlns:form="http://g5.1c.ru/v8/dt/form" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">',
            "  <attributes>",
            f"    <name>{attr}</name>",
            f"    <valueType>\n      <types>{ref_type}</types>\n    </valueType>",
            "  </attributes>",
            "</form:Form>",
        ]
    )


def _typeset_form(cf: bool, attr: str, token: str) -> str:
    """Форма с одним реквизитом, чей тип — набор (CF ``<v8:TypeSet>`` с префиксом ``cfg:``, EDT ``<types>``)."""
    if cf:
        return "\n".join(
            [
                '<?xml version="1.0" encoding="UTF-8"?>',
                f'<Form {_CF_FORM_NS} version="2.20">',
                "\t<Attributes>",
                f'\t\t<Attribute name="{attr}" id="1">',
                f"\t\t\t<Type>\n\t\t\t\t<v8:TypeSet>{token}</v8:TypeSet>\n\t\t\t</Type>",
                "\t\t</Attribute>",
                "\t</Attributes>",
                "</Form>",
            ]
        )
    return "\n".join(
        [
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<form:Form xmlns:form="http://g5.1c.ru/v8/dt/form" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">',
            "  <attributes>",
            f"    <name>{attr}</name>",
            f"    <valueType>\n      <types>{token}</types>\n    </valueType>",
            "  </attributes>",
            "</form:Form>",
        ]
    )


def _cf_ext_doc_form() -> str:
    """Форма расширения `Расш1` (заимствованная `Документ.Док.ФормаДокумента`): события и действия команд
    через `callType`; `<BaseForm>` — копия базовой формы, как в выгрузках. Перехват — только у перенятого: событие
    формы, команда `Команда1`, поле `Сумма` (в основной форме у него обработчика нет — цель пуста). Свои элементы
    расширения — команда `НетВБазе`, поле `Расш1_Поле` и команда `Сумма` (одноимённая перенятому полю; сверка по
    виду узла) — платформа пишет с `callType`, но перехватывать им нечего."""
    return "\n".join(
        [
            '<?xml version="1.0" encoding="UTF-8"?>',
            f'<Form {_CF_FORM_NS} version="2.20">',
            "\t<Events>",
            '\t\t<Event name="OnCreateAtServer" callType="After">Расш1_ПриСозданииПосле</Event>',
            "\t</Events>",
            "\t<ChildItems>",
            '\t\t<InputField name="Сумма" id="1">',
            "\t\t\t<Events>",
            '\t\t\t\t<Event name="OnChange" callType="Before">Расш1_СуммаПриИзмененииПеред</Event>',
            "\t\t\t</Events>",
            "\t\t</InputField>",
            '\t\t<InputField name="Расш1_Поле" id="1000001">',
            "\t\t\t<Events>",
            '\t\t\t\t<Event name="OnChange" callType="After">Расш1_ПолеПриИзмененииПосле</Event>',
            "\t\t\t</Events>",
            "\t\t</InputField>",
            "\t</ChildItems>",
            "\t<Commands>",
            '\t\t<Command name="Команда1" id="1">',
            '\t\t\t<Action callType="Override">Расш1_Команда1Вместо</Action>',
            "\t\t</Command>",
            '\t\t<Command name="НетВБазе" id="1000000">',
            '\t\t\t<Action callType="Before">Расш1_НетВБазеПеред</Action>',
            "\t\t</Command>",
            '\t\t<Command name="Сумма" id="1000002">',
            '\t\t\t<Action callType="Override">Расш1_СуммаКомандаВместо</Action>',
            "\t\t</Command>",
            "\t</Commands>",
            '\t<BaseForm version="2.20">',
            "\t\t<Events>",
            '\t\t\t<Event name="OnCreateAtServer">ПриСозданииНаСервере</Event>',
            "\t\t</Events>",
            "\t\t<ChildItems>",
            '\t\t\t<InputField name="Сумма" id="1"/>',
            "\t\t</ChildItems>",
            "\t\t<Commands>",
            '\t\t\t<Command name="Команда1" id="1">',
            "\t\t\t\t<Action>Команда1</Action>",
            "\t\t\t</Command>",
            "\t\t</Commands>",
            "\t</BaseForm>",
            "</Form>",
        ]
    )


BALLAST = ("Балласт1", "Балласт2", "Балласт3")


def _world18_files(fmt: str) -> dict[str, str]:
    """Стандартное дерево world18 «путь от корня основной → текст» (у CF ещё `../cfe/Расш1/…`)."""
    cf = fmt == "cf"
    files: dict[str, str] = {}

    def obj(category: str, name: str, *, kind: str, synonym: str = "", cf_props=(), cf_children=(), edt=()):
        if cf:
            files[f"{category}/{name}.xml"] = _cf_obj(kind, name, synonym=synonym, props=cf_props, children=cf_children)
        else:
            files[f"{category}/{name}/{name}.mdo"] = _edt_obj(kind, name, synonym=synonym, body=edt)

    def module(category: str, name: str, file: str, text: str):
        """Модуль объекта: ``file`` — имя файла модуля (CF кладёт его в ``Ext/``)."""
        files[f"{category}/{name}/{'Ext/' if cf else ''}{file}"] = text

    if cf:
        files["Configuration.xml"] = CF_DESCRIPTOR
    else:
        files["Configuration/Configuration.mdo"] = EDT_DESCRIPTOR

    # ── документ `Док` ──
    obj(
        "Documents",
        "Док",
        kind="Document",
        synonym="Документ тест",
        cf_props=[
            "<Posting>Allow</Posting>",
            _cf_ref_items("RegisterRecords", ["AccumulationRegister.Рег", "AccumulationRegister.Рег2"]),
        ],
        cf_children=[
            _cf_child("Attribute", "Сумма", sets=["cfg:DefinedType.ДенежнаяСумма"]),
            _cf_child("Attribute", "Счет", sets=["cfg:ChartOfAccountsRef"]),
            _cf_child("Attribute", "Основание", types=["cfg:DocumentRef.Док"]),
            "\n".join(
                [
                    "<TabularSection>",
                    "<Properties>",
                    "<Name>Товары</Name>",
                    "<Synonym/>",
                    "</Properties>",
                    "<ChildObjects>",
                    _cf_child("Attribute", "Количество", types=["xs:decimal"]),
                    "</ChildObjects>",
                    "</TabularSection>",
                ]
            ),
            "<Form>ФормаДокумента</Form>",
            _cf_command("Заполнить", ["cfg:CatalogRef.БезСинонима"]),
        ],
        edt=[
            _edt_typed("attributes", "Сумма", ["DefinedType.ДенежнаяСумма"]),
            _edt_typed("attributes", "Счет", ["ChartOfAccountsRef"]),
            _edt_typed("attributes", "Основание", ["DocumentRef.Док"]),
            "\n".join(
                [
                    "<tabularSections>",
                    "<name>Товары</name>",
                    _edt_typed("attributes", "Количество", ["Number"]),
                    "</tabularSections>",
                ]
            ),
            "<forms>\n<name>ФормаДокумента</name>\n</forms>",
            _edt_command("Заполнить", ["CatalogRef.БезСинонима"]),
            "<registerRecords>AccumulationRegister.Рег</registerRecords>",
            "<registerRecords>AccumulationRegister.Рег2</registerRecords>",
            "<posting>Allow</posting>",
        ],
    )
    module("Documents", "Док", "ObjectModule.bsl", _DOC_OBJECT_MODULE)
    module("Documents", "Док", "ManagerModule.bsl", _DOC_MANAGER_MODULE)
    if cf:
        files["Documents/Док/Forms/ФормаДокумента/Ext/Form.xml"] = _cf_doc_form()
        files["Documents/Док/Forms/ФормаДокумента/Ext/Form/Module.bsl"] = _DOC_FORM_MODULE
        files["Documents/Док/Commands/Заполнить/Ext/CommandModule.bsl"] = _COMMAND_MODULE
    else:
        files["Documents/Док/Forms/ФормаДокумента/Form.form"] = _edt_doc_form()
        files["Documents/Док/Forms/ФормаДокумента/Module.bsl"] = _DOC_FORM_MODULE
        files["Documents/Док/Commands/Заполнить/CommandModule.bsl"] = _COMMAND_MODULE

    # ── общие модули ──
    for name, text in (
        ("ОбщМод", "Процедура Экспорт1(Параметр) Экспорт\nКонецПроцедуры\n"),
        (
            "ДокЛокализация",
            "Процедура ЗарегистрироватьУчетныеМеханизмы(МеханизмыДокумента) Экспорт\n"
            '\tМеханизмыДокумента.Добавить("Мех2");\nКонецПроцедуры\n',
        ),
        (
            "ПроведениеСервер",
            "Процедура ОтразитьДвиженияПоРегистру(ДополнительныеСвойства, Движения, Отказ, ИмяТаблицы) Экспорт\n"
            "КонецПроцедуры\n",
        ),
    ):
        obj(
            "CommonModules",
            name,
            kind="CommonModule",
            cf_props=["<Server>true</Server>"],
            edt=["<server>true</server>"],
        )
        module("CommonModules", name, "Module.bsl", text)

    # ── определяемые типы ──
    obj(
        "DefinedTypes",
        "ДенежнаяСумма",
        kind="DefinedType",
        cf_props=[_cf_type(["xs:decimal"])],
        edt=["<type>\n<types>Number</types>\n</type>"],
    )
    obj(
        "DefinedTypes",
        "ПредметОбъект",
        kind="DefinedType",
        cf_props=[_cf_type(["cfg:DocumentObject.Док"])],
        edt=["<type>\n<types>DocumentObject.Док</types>\n</type>"],
    )

    # ── справочники ──
    obj(
        "Catalogs",
        "Спр",
        kind="Catalog",
        synonym="Справочник тест",
        cf_children=[_cf_child("Attribute", "Реквизит1", types=["xs:string"])],
        edt=[_edt_typed("attributes", "Реквизит1", ["String"])],
    )
    module("Catalogs", "Спр", "ObjectModule.bsl", _PROC)
    obj("Catalogs", "БезСинонима", kind="Catalog")  # пустой синоним, без модулей: в CF — только описатель

    # ── регистры накопления, перечисление ──
    for name in ("Рег", "Рег2"):
        obj(
            "AccumulationRegisters",
            name,
            kind="AccumulationRegister",
            synonym=f"Регистр {name}",
            cf_children=[_cf_child("Resource", "Количество", types=["xs:decimal"])],
            edt=[_edt_typed("resources", "Количество", ["Number"])],
        )
    obj(
        "Enums",
        "Виды",
        kind="Enum",
        cf_children=[
            "<EnumValue>\n<Properties>\n<Name>Первый</Name>\n</Properties>\n</EnumValue>",
            "<EnumValue>\n<Properties>\n<Name>Второй</Name>\n</Properties>\n</EnumValue>",
        ],
        edt=["<enumValues>\n<name>Первый</name>\n</enumValues>", "<enumValues>\n<name>Второй</name>\n</enumValues>"],
    )

    # ── обработка `Обр` с командой `Открыть` (CF — в описателе владельца, EDT — `<commands>`) ──
    obj(
        "DataProcessors",
        "Обр",
        kind="DataProcessor",
        cf_children=[
            _cf_child("Attribute", "Ссылка", types=["cfg:CatalogRef.Спр"]),
            _cf_child("Attribute", "Вид", types=["cfg:EnumRef.Виды"]),
            _cf_command("Открыть", ["cfg:DocumentRef.Док"]),
        ],
        edt=[
            _edt_typed("attributes", "Ссылка", ["CatalogRef.Спр"]),
            _edt_typed("attributes", "Вид", ["EnumRef.Виды"]),
            _edt_command("Открыть", ["DocumentRef.Док"]),
        ],
    )
    module("DataProcessors", "Обр", "ObjectModule.bsl", _PROC)
    files[f"DataProcessors/Обр/Commands/Открыть/{'Ext/' if cf else ''}CommandModule.bsl"] = _COMMAND_MODULE
    # форма обработки (Задача 10: строки форм переживают bulk-fallback категории)
    files[f"DataProcessors/Обр/Forms/Форма/{'Ext/Form.xml' if cf else 'Form.form'}"] = _small_form(
        cf, "Выбор", "CatalogRef.БезСинонима"
    )

    # ── бизнес-процесс, задача с реквизитом адресации, константа ──
    obj(
        "BusinessProcesses",
        "БП",
        kind="BusinessProcess",
        cf_props=["<Task>Task.Зад</Task>"],
        cf_children=[_cf_child("Attribute", "Предмет", types=["cfg:DocumentRef.Док"])],
        edt=[_edt_typed("attributes", "Предмет", ["DocumentRef.Док"]), "<task>Task.Зад</task>"],
    )
    obj(
        "Tasks",
        "Зад",
        kind="Task",
        cf_children=[_cf_child("AddressingAttribute", "Исполнитель", types=["cfg:CatalogRef.Спр"])],
        edt=[_edt_typed("addressingAttributes", "Исполнитель", ["CatalogRef.Спр"])],
    )
    obj(
        "Constants",
        "Конст",
        kind="Constant",
        cf_props=[_cf_type(sets=["cfg:DefinedType.ДенежнаяСумма"])],
        edt=["<type>\n<types>DefinedType.ДенежнаяСумма</types>\n</type>"],
    )

    # ── регистр бухгалтерии, план счетов, ПВХ ──
    obj(
        "AccountingRegisters",
        "РБ",
        kind="AccountingRegister",
        cf_props=["<ChartOfAccounts>ChartOfAccounts.ПС</ChartOfAccounts>"],
        cf_children=[_cf_child("Resource", "Сумма", types=["xs:decimal"])],
        edt=["<chartOfAccounts>ChartOfAccounts.ПС</chartOfAccounts>", _edt_typed("resources", "Сумма", ["Number"])],
    )
    obj(
        "ChartsOfAccounts",
        "ПС",
        kind="ChartOfAccounts",
        cf_props=["<ExtDimensionTypes>ChartOfCharacteristicTypes.ВидыСубконто</ExtDimensionTypes>"],
        cf_children=[_cf_child("Attribute", "Подразделение", types=["cfg:CatalogRef.Спр"])],
        edt=[
            "<extDimensionTypes>ChartOfCharacteristicTypes.ВидыСубконто</extDimensionTypes>",
            _edt_typed("attributes", "Подразделение", ["CatalogRef.Спр"]),
        ],
    )
    obj(
        "ChartsOfCharacteristicTypes",
        "ВидыСубконто",
        kind="ChartOfCharacteristicTypes",
        cf_props=[_cf_type(["xs:string"])],
        edt=["<type>\n<types>String</types>\n</type>"],
    )

    # ── регистр сведений без каталога (CF — только описатель) ──
    obj(
        "InformationRegisters",
        "БезКаталога",
        kind="InformationRegister",
        cf_children=[_cf_child("Dimension", "Контрагент", types=["cfg:CatalogRef.Спр"])],
        edt=[_edt_typed("dimensions", "Контрагент", ["CatalogRef.Спр"])],
    )

    # ── журнал и критерий отбора ──
    obj(
        "DocumentJournals",
        "Жур",
        kind="DocumentJournal",
        cf_props=[_cf_ref_items("RegisteredDocuments", ["Document.Док"])],
        cf_children=[_cf_command("Печать", ["cfg:DocumentRef.Док"])],
        edt=["<registeredDocuments>Document.Док</registeredDocuments>", _edt_command("Печать", ["DocumentRef.Док"])],
    )
    files[f"DocumentJournals/Жур/Commands/Печать/{'Ext/' if cf else ''}CommandModule.bsl"] = _COMMAND_MODULE
    obj(
        "FilterCriteria",
        "Крит",
        kind="FilterCriterion",
        cf_props=[
            _cf_type(["cfg:DocumentRef.Док"]),
            _cf_ref_items("Content", ["Document.Док.Attribute.Основание", "Document.Док.Attribute.Сумма"]),
        ],
        edt=[
            "<type>\n<types>DocumentRef.Док</types>\n</type>",
            "<content>Document.Док.Attribute.Основание</content>",
            "<content>Document.Док.Attribute.Сумма</content>",
        ],
    )

    # ── подписки и регламентные задания ──
    def subscription(name: str, source: str, event: str):
        obj(
            "EventSubscriptions",
            name,
            kind="EventSubscription",
            cf_props=[
                f"<Source>\n<v8:TypeSet>cfg:{source}</v8:TypeSet>\n</Source>"
                if source.startswith("DefinedType.")
                else f"<Source>\n<v8:Type>cfg:{source}</v8:Type>\n</Source>",
                f"<Event>{event}</Event>",
                "<Handler>CommonModule.ОбщМод.Экспорт1</Handler>",
            ],
            edt=[
                f"<source>\n<types>{source}</types>\n</source>",
                f"<event>{event}</event>",
                "<handler>CommonModule.ОбщМод.Экспорт1</handler>",
            ],
        )

    subscription("ПодпНабор", "DefinedType.ДенежнаяСумма", "OnWrite")
    subscription("ПодпОбъект", "DefinedType.ПредметОбъект", "BeforeWrite")

    def job(name: str, use: bool | None):
        cf_use = [f"<Use>{'true' if use else 'false'}</Use>"]
        edt_use = ["<use>true</use>"] if use else []
        obj(
            "ScheduledJobs",
            name,
            kind="ScheduledJob",
            cf_props=[
                "<MethodName>CommonModule.ОбщМод.Экспорт1</MethodName>",
                *cf_use,
                "<Predefined>true</Predefined>",
            ],
            edt=["<methodName>CommonModule.ОбщМод.Экспорт1</methodName>", *edt_use, "<predefined>true</predefined>"],
        )

    job("ЗаданиеВыкл", False)  # EDT: узла <use> нет (значение по умолчанию false не пишется)
    job("ЗаданиеВкл", True)

    # ── балласт порога точечного пути: по три учтённых объекта в каждой правимой категории ──
    for name in BALLAST:
        for category, kind, file in (
            ("Documents", "Document", "ObjectModule.bsl"),
            ("Catalogs", "Catalog", "ObjectModule.bsl"),
            ("InformationRegisters", "InformationRegister", "RecordSetModule.bsl"),
            ("AccountingRegisters", "AccountingRegister", "RecordSetModule.bsl"),
            ("ChartsOfAccounts", "ChartOfAccounts", "ObjectModule.bsl"),
        ):
            obj(
                category,
                name,
                kind=kind,
                cf_children=[_cf_child("Attribute", "Поле", types=["xs:string"])],
                edt=[_edt_typed("attributes", "Поле", ["String"])],
            )
            module(category, name, file, _PROC)  # каталог объекта есть в обоих форматах
        subscription(f"Подп{name}", f"CatalogObject.{name}", "BeforeWrite")
        job(f"Задание{name}", True)

    # ── расширение `Расш1` рядом (только CF) ──
    if cf:
        ext = "../cfe/Расш1/"
        files[ext + "Configuration.xml"] = CF_EXT_DESCRIPTOR
        files[ext + "Documents/Док.xml"] = _cf_obj(
            "Document",
            "Док",
            props=["<ObjectBelonging>Adopted</ObjectBelonging>"],
            children=["<Form>ФормаДокумента</Form>"],
        )
        files[ext + "Documents/Док/Forms/ФормаДокумента/Ext/Form.xml"] = _cf_ext_doc_form()
        files[ext + "Documents/Док/Forms/ФормаДокумента/Ext/Form/Module.bsl"] = _EXT_FORM_MODULE
    return files


class World(dict):
    """Мир world18: ``files`` (дерево «путь → текст»), ``write`` (писатель дерева), ``root`` (корень основной),
    ``ext_root`` (корень `Расш1` у CF), ``fmt``, ``db`` (путь индекса), ``idx`` (хелперы на индексе), ``live``
    (хелперы без индекса), ``reader`` (ридер индекса)."""


def _world18_helpers(root: Path, ext_root: Path | None, reader: IndexReader | None):
    ext_kw: dict = {}
    if ext_root is not None:
        ext_kw = {
            "extension_paths": [str(ext_root)],
            "extension_name_by_root": {str(ext_root): "Расш1"},
        }
    return _bsl_for(
        root,
        reader,
        idx_zero_callers_authoritative=reader is not None,
        current_config_role="main",
        current_config_name="Тест",
        current_config_root=str(root),
        **ext_kw,
    )


def _build_world18(base: Path, fmt: str, files: dict[str, str] | None = None) -> World:
    """Мир world18 в ``base``; ``files`` — своё дерево вместо стандартного (вариант мира для отдельного класса)."""
    files = _world18_files(fmt) if files is None else files
    root = base / "cf"
    _write_tree(root, files)
    ext_root = base / "cfe" / "Расш1" if fmt == "cf" else None
    mp = pytest.MonkeyPatch()
    mp.setenv("RLM_INDEX_DIR", str(base / "idx"))
    try:
        db = IndexBuilder().build(str(root), build_calls=True)
    finally:
        mp.undo()
    reader = IndexReader(db)
    return World(
        files=files,
        write=_write_tree,
        root=root,
        ext_root=ext_root,
        fmt=fmt,
        db=str(db),
        reader=reader,
        idx=_world18_helpers(root, ext_root, reader),
        live=_world18_helpers(root, ext_root, None),
    )


@pytest.fixture(scope="module")
def worlds18(tmp_path_factory):
    """Оба мира — для сравнения CF с EDT в одном тесте (``world18`` берёт свой отсюда)."""
    built = {fmt: _build_world18(tmp_path_factory.mktemp(f"world18_{fmt}"), fmt) for fmt in ("cf", "edt")}
    yield built
    for world in built.values():
        world["reader"].close()


@pytest.fixture(scope="module", params=["cf", "edt"])
def world18(request, worlds18):
    return worlds18[request.param]


def _module_path(fmt: str, category: str, name: str, file: str) -> str:
    return f"{category}/{name}/{'Ext/' if fmt == 'cf' else ''}{file}"


# Категории точечных тестов: знаменатель порога `_POINTWISE_MAX_RATIO` — объекты со строками таблицы.
_POINTWISE_CATEGORIES = (
    "Documents",
    "Catalogs",
    "InformationRegisters",
    "AccountingRegisters",
    "ScheduledJobs",
    "EventSubscriptions",
)


def test_world18_builds_both_layouts(world18):
    paths = [m["path"] for m in world18["idx"]["find_module"]("Док")]
    assert _module_path(world18["fmt"], "Documents", "Док", "ObjectModule.bsl") in paths, paths
    assert Path(world18["db"]).is_file()
    if world18["fmt"] == "cf":
        ext = world18["idx"]["detect_extensions"]()
        assert [e["name"] for e in ext["nearby_extensions"]] == ["Расш1"], ext
    con = sqlite3.connect(world18["db"])
    try:
        for category in _POINTWISE_CATEGORIES:
            assert BI._category_object_count(con, category) >= 3, category
        # у планов счетов знаменатель — предопределённые; при нуле относительный порог не действует
        assert BI._category_object_count(con, "ChartsOfAccounts") == 0
    finally:
        con.close()


def _db_copy(src: str, dst: Path) -> Path:
    """Копия индекса через API резервного копирования SQLite (WAL-файлы не теряются)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    s, d = sqlite3.connect(src), sqlite3.connect(str(dst))
    try:
        s.backup(d)
    finally:
        s.close()
        d.close()
    return dst


# ═══════════════ Задача 1 — поколение индекса v18 ═══════════════

_V18_OVERRIDE_COLUMNS = {"form_item", "form_event"}


def _downgrade_to_v17(db_path) -> None:
    """Индекс прежнего поколения: ``builder_version``/``version`` = 17 и ``extension_overrides`` без колонок v18
    (таблица пересоздаётся прежним составом — SQLite без ``DROP COLUMN`` на старых сборках)."""
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


def _v17_copy(world: World, dst_dir: Path) -> IndexReader:
    """Ридер на копии индекса мира, понижённой до v17 (сам мир не трогается)."""
    copy = _db_copy(world["db"], dst_dir / "bsl_index_v17.db")
    _downgrade_to_v17(copy)
    return IndexReader(copy)


_GEN_FILES = {
    "CommonModules/ОбщийМодульА.xml": _cf_obj("CommonModule", "ОбщийМодульА"),
    "CommonModules/ОбщийМодульА/Ext/Module.bsl": "Функция Метод1() Экспорт\n    Возврат 1;\nКонецФункции\n",
    "Documents/Заказ/Ext/ObjectModule.bsl": "Процедура П()\n    ОбщийМодульА.Метод1();\nКонецПроцедуры\n",
}


class TestIndexGeneration18:
    def test_builder_version_is_18(self):
        assert BI.BUILDER_VERSION == 18

    def test_old_index_gaps_last_row_is_18(self):
        threshold, text = BI.OLD_INDEX_GAPS[-1]
        assert threshold == BI.BUILDER_VERSION == 18
        assert text.strip()
        for word in ("callType", "форм", "журнал", "Метаданные", "переносом", "БГУ", "EDT"):
            assert word in text, word
        assert "ё" not in text

    def test_update_on_v17_index_forces_full_rebuild(self, tmp_path):
        base = _cf_doc_tree(tmp_path / "cf", _GEN_FILES)
        db = IndexBuilder().build(str(base), build_calls=True)
        _downgrade_to_v17(db)
        res = IndexBuilder().update(str(base))
        assert res["rebuild_reason"] == "schema upgrade v17->18", res
        con = sqlite3.connect(str(db))
        try:
            cols = {r[1] for r in con.execute("PRAGMA table_info(extension_overrides)")}
            version = con.execute("SELECT value FROM index_meta WHERE key='builder_version'").fetchone()[0]
        finally:
            con.close()
        assert _V18_OVERRIDE_COLUMNS <= cols, cols
        assert version == "18"

    def test_start_warns_on_v17_index(self, tmp_path):
        import json

        from rlm_tools_bsl.server import _rlm_end, _rlm_start

        base = _cf_doc_tree(tmp_path / "cf", _GEN_FILES)
        _downgrade_to_v17(IndexBuilder().build(str(base), build_calls=True))
        data = json.loads(_rlm_start(path=str(base), query="", domains=[]))
        try:
            assert data["source_support"] == "supported", data
            assert data["index"]["loaded"] is True
            text18 = dict(BI.OLD_INDEX_GAPS)[18]
            warns = [w for w in data["index"]["warnings"] if "сборщиком v17" in w]
            assert len(warns) == 1, data["index"]["warnings"]
            assert text18 in warns[0]
            # для v17 недостача v17 уже закрыта — её текст здесь ложен
            assert dict(BI.OLD_INDEX_GAPS)[17] not in warns[0]
        finally:
            _rlm_end(data["session_id"])

    def test_full_scan_update_recreates_overrides_table(self, tmp_path):
        base = _cf_doc_tree(tmp_path / "cf", _GEN_FILES)
        db = IndexBuilder().build(str(base), build_calls=True)
        con = sqlite3.connect(str(db))
        try:
            con.execute("DROP TABLE extension_overrides")
            con.commit()
        finally:
            con.close()
        res = IndexBuilder().update(str(base))
        assert res["git_fast_path"] is False and "rebuild_reason" not in res, res
        con = sqlite3.connect(str(db))
        try:
            cols = {r[1] for r in con.execute("PRAGMA table_info(extension_overrides)")}
        finally:
            con.close()
        assert _V18_OVERRIDE_COLUMNS <= cols, cols


# ═══════════════ Задача 2 — состояние регламентного задания EDT (Д15) ═══════════════

_SJ_MDO = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    f"<mdclass:ScheduledJob {_EDT_MD_NS}>\n"
    "<name>Задание</name>\n"
    "<methodName>CommonModule.М.П</methodName>\n"
    "{use}"
    "<predefined>true</predefined>\n"
    "</mdclass:ScheduledJob>\n"
)


def _jobs_use(helpers) -> dict[str, bool]:
    return {j["name"]: j["use"] for j in helpers["find_scheduled_jobs"]() if not j["name"].startswith("ЗаданиеБалласт")}


class TestScheduledJobUseEdt:
    def test_mdo_without_use_is_disabled(self):
        from rlm_tools_bsl.bsl_xml_parsers import parse_scheduled_job_xml

        assert parse_scheduled_job_xml(_SJ_MDO.replace("{use}", ""))["use"] is False

    def test_mdo_use_true_is_enabled(self):
        from rlm_tools_bsl.bsl_xml_parsers import parse_scheduled_job_xml

        assert parse_scheduled_job_xml(_SJ_MDO.replace("{use}", "<use>true</use>\n"))["use"] is True

    @pytest.mark.parametrize("world18", ["edt"], indirect=True)
    def test_index_and_live_agree(self, world18):
        expected = {"ЗаданиеВыкл": False, "ЗаданиеВкл": True}
        assert _jobs_use(world18["idx"]) == expected
        assert _jobs_use(world18["live"]) == expected

    def test_cf_unchanged(self):
        """Обе версии: CF пишет `<Use>` всегда, `<Use>false</Use>` — `False`."""
        from rlm_tools_bsl.bsl_xml_parsers import parse_scheduled_job_xml

        text = _cf_obj(
            "ScheduledJob",
            "Задание",
            props=["<MethodName>CommonModule.М.П</MethodName>", "<Use>false</Use>", "<Predefined>true</Predefined>"],
        )
        assert parse_scheduled_job_xml(text)["use"] is False

    @pytest.mark.parametrize("route", ["full", "git"])
    @pytest.mark.parametrize("world18", ["edt"], indirect=True)
    def test_update_equals_build(self, world18, route, tmp_path_factory, monkeypatch):
        files_b = dict(world18["files"])
        rel = "ScheduledJobs/ЗаданиеВкл/ЗаданиеВкл.mdo"
        assert "<use>true</use>" in files_b[rel]
        files_b[rel] = files_b[rel].replace("<use>true</use>\n", "")
        db = _assert_update_equals_build(
            tmp_path_factory,
            monkeypatch,
            world18["write"],
            world18["files"],
            files_b,
            {"scheduled_jobs": "SELECT name, use FROM scheduled_jobs"},
            route=route,
            expect_pointwise=("ScheduledJobs", "ЗаданиеВкл"),
        )
        assert _rows(db, "SELECT use FROM scheduled_jobs WHERE name = 'ЗаданиеВкл'") == [(0,)]


# ═══════════════ Задача 3 — объект без синонима (Д16) ═══════════════


class TestObjectWithoutSynonym:
    def test_search_by_technical_name(self, world18):
        rows = world18["idx"]["search_objects"]("БезСинонима")
        assert rows, rows
        first = rows[0]
        assert (first["object_name"], first["category"], first["synonym"]) == ("БезСинонима", "Catalogs", ""), first

    def test_search_by_category_word(self, world18):
        rows = world18["idx"]["search_objects"]("справочник", limit=500)
        assert ("БезСинонима", "Catalogs") in {(r["object_name"], r["category"]) for r in rows}, rows

    def test_count_only_counts_it(self, world18):
        count = world18["idx"]["search_objects"]("БезСинонима", count_only=True)
        assert count["total"] == len(world18["idx"]["search_objects"]("БезСинонима", limit=10**6)) >= 1, count

    @pytest.mark.parametrize("helpers", ["idx", "live"])
    def test_object_full_structure_synonym_empty(self, world18, helpers):
        """Обе версии: пустой синоним в структуре — `None`; новая строка индекса ответа не меняет."""
        res = world18[helpers]["get_object_full_structure"]("Справочник.БезСинонима")
        assert res.get("object_name") == "БезСинонима", res
        assert res["synonym"] is None, res

    @pytest.mark.parametrize("route", ["full", "git"])
    def test_update_equals_build(self, world18, route, tmp_path_factory, monkeypatch):
        files_b = dict(world18["files"])
        cf = world18["fmt"] == "cf"
        rel = "Catalogs/БезСинонима.xml" if cf else "Catalogs/БезСинонима/БезСинонима.mdo"
        if cf:
            files_b[rel] = files_b[rel].replace(
                "</Properties>\n",
                "</Properties>\n<ChildObjects>\n" + _cf_child("Attribute", "Добавлено") + "\n</ChildObjects>\n",
            )
        else:
            files_b[rel] = files_b[rel].replace(
                "</mdclass:Catalog>", _edt_typed("attributes", "Добавлено", ["String"]) + "\n</mdclass:Catalog>"
            )
        assert files_b[rel] != world18["files"][rel]
        db = _assert_update_equals_build(
            tmp_path_factory,
            monkeypatch,
            world18["write"],
            world18["files"],
            files_b,
            {"object_synonyms": "SELECT object_name, category, synonym, file FROM object_synonyms"},
            route=route,
            expect_pointwise=("Catalogs", "БезСинонима"),
        )
        assert _rows(db, "SELECT category, synonym, file FROM object_synonyms WHERE object_name = 'БезСинонима'") == [
            ("Catalogs", "Справочник: ", rel)
        ]

    def test_extension_object_without_synonym_live(self, tmp_path):
        files = {
            "Configuration.xml": CF_DESCRIPTOR,
            "Catalogs/Спр.xml": _cf_obj("Catalog", "Спр", synonym="Справочник тест"),
            "Catalogs/Спр/Ext/ObjectModule.bsl": _PROC,
            "../cfe/Расш1/Configuration.xml": CF_EXT_DESCRIPTOR,
            "../cfe/Расш1/Catalogs/Расш1_БезСин.xml": _cf_obj("Catalog", "Расш1_БезСин"),
        }
        root = tmp_path / "cf"
        _write_tree(root, files)
        live = _world18_helpers(root, tmp_path / "cfe" / "Расш1", None)
        rows = [r for r in live["search_objects"]("Расш1_БезСин") if r["object_name"] == "Расш1_БезСин"]
        assert len(rows) == 1, rows
        assert (rows[0]["category"], rows[0]["synonym"], rows[0]["owner"]) == ("Catalogs", "", "extension:Расш1")


# ═══════════════ Задача 4 — тип-набор выгрузки Конфигуратора (Д2, Д17) ═══════════════


def _attr_types(helpers, object_name: str) -> dict[str, list]:
    return {a["attr_name"]: a["attr_type"] for a in helpers["find_attributes"](object_name=object_name)}


def _refs(helpers, ref: str, kinds=None) -> set[tuple[str, str]]:
    res = helpers["find_references_to_object"](ref, kinds=kinds) if kinds else helpers["find_references_to_object"](ref)
    return {(r["kind"], r["used_in"]) for r in res["references"]}


def _form_attr_types(helpers, object_name: str, form: str = "ФормаДокумента") -> dict[str, list]:
    return {
        a["name"]: a["types"]
        for f in helpers["parse_form"](object_name)
        if f.get("form_name") == form
        for a in f.get("attributes", [])
    }


def _json_list(value):
    import json

    return json.loads(value) if isinstance(value, str) else value


class TestCfTypeSet:
    @pytest.mark.parametrize("helpers", ["idx", "live"])
    def test_attribute_typeset_defined_type(self, world18, helpers):
        assert _attr_types(world18[helpers], "Документ.Док")["Сумма"] == ["DefinedType.ДенежнаяСумма"]

    @pytest.mark.parametrize("helpers", ["idx", "live"])
    def test_attribute_typeset_without_name(self, world18, helpers):
        assert _attr_types(world18[helpers], "Документ.Док")["Счет"] == ["ChartOfAccountsRef"]

    def test_references_to_defined_type(self, worlds18):
        by_fmt = {fmt: _refs(w["idx"], "ОпределяемыйТип.ДенежнаяСумма") for fmt, w in worlds18.items()}
        assert ("attribute_type", "Document.Док.Attribute.Сумма.Type") in by_fmt["cf"], by_fmt["cf"]
        assert by_fmt["cf"] == by_fmt["edt"]

    @pytest.mark.parametrize("world18", ["cf"], indirect=True)
    def test_typeset_ref_line_points_to_token(self, world18):
        lines = world18["files"]["Documents/Док.xml"].splitlines()
        token = lines.index("<v8:TypeSet>cfg:DefinedType.ДенежнаяСумма</v8:TypeSet>") + 1
        rows = world18["idx"]["find_references_to_object"]("ОпределяемыйТип.ДенежнаяСумма")["references"]
        line = [r["line"] for r in rows if r["used_in"] == "Document.Док.Attribute.Сумма.Type"]
        assert line == [token], (line, token)

    @pytest.mark.parametrize("helpers", ["idx", "live"])
    def test_constant_value_type(self, world18, helpers):
        res = world18[helpers]["get_object_full_structure"]("Константа.Конст")
        assert _json_list(res["value_type"]) == ["DefinedType.ДенежнаяСумма"], res

    @pytest.mark.parametrize("helpers", ["idx", "live"])
    @pytest.mark.parametrize("world18", ["cf"], indirect=True)
    def test_form_attribute_types_typeset(self, world18, helpers):
        assert _form_attr_types(world18[helpers], "Документ.Док")["Сумма"] == ["cfg:DefinedType.ДенежнаяСумма"]

    @pytest.mark.parametrize("helpers", ["idx", "live"])
    @pytest.mark.parametrize("world18", ["cf"], indirect=True)
    def test_form_attribute_without_type_stays_empty(self, world18, helpers):
        """Обе версии: `[]` — только у реквизита без типа (`<Type/>`)."""
        assert _form_attr_types(world18[helpers], "Документ.Док")["Произвольный"] == []

    def test_mixed_type_and_typeset_keeps_order(self):
        from rlm_tools_bsl.bsl_xml_parsers import parse_metadata_xml

        text = _cf_obj(
            "Catalog",
            "Смесь",
            children=[_cf_child("Attribute", "Р", types=["xs:string"], sets=["cfg:DefinedType.X"])],
        )
        attr = parse_metadata_xml(text)["attributes"][0]
        assert attr["type"] == "xs:string, cfg:DefinedType.X", attr

    # ── Задача 19: тип-набор «значения характеристик ПВХ» (`Characteristic.X`) — токен, ссылки на ПВХ нет ──

    @pytest.fixture(scope="class", params=["cf", "edt"])
    def char_tree(self, request, tmp_path_factory):
        cf = request.param == "cf"
        root = tmp_path_factory.mktemp(f"characteristic_{request.param}") / "cf"
        token = "Characteristic.ВидыСубконто"
        mod = "Ext/" if cf else ""
        if cf:
            files = {
                "Configuration.xml": CF_DESCRIPTOR,
                "Catalogs/Спр.xml": _cf_obj("Catalog", "Спр"),
                "ChartsOfCharacteristicTypes/ВидыСубконто.xml": _cf_obj(
                    "ChartOfCharacteristicTypes", "ВидыСубконто", props=[_cf_type(["cfg:CatalogRef.Спр"])]
                ),
                "Documents/Док.xml": _cf_obj(
                    "Document", "Док", children=[_cf_child("Attribute", "Аналитика", sets=[f"cfg:{token}"])]
                ),
                "Documents/Док/Forms/ФормаДокумента/Ext/Form.xml": _typeset_form(True, "Аналитика", f"cfg:{token}"),
            }
        else:
            files = {
                "Configuration/Configuration.mdo": EDT_DESCRIPTOR,
                "Catalogs/Спр/Спр.mdo": _edt_obj("Catalog", "Спр"),
                "ChartsOfCharacteristicTypes/ВидыСубконто/ВидыСубконто.mdo": _edt_obj(
                    "ChartOfCharacteristicTypes",
                    "ВидыСубконто",
                    body=["<type>\n<types>CatalogRef.Спр</types>\n</type>"],
                ),
                "Documents/Док/Док.mdo": _edt_obj(
                    "Document", "Док", body=[_edt_typed("attributes", "Аналитика", [token])]
                ),
                "Documents/Док/Forms/ФормаДокумента/Form.form": _typeset_form(False, "Аналитика", token),
            }
        files[f"Catalogs/Спр/{mod}ObjectModule.bsl"] = _PROC
        files[f"Documents/Док/{mod}ObjectModule.bsl"] = _PROC
        _write_tree(root, files)
        mp = pytest.MonkeyPatch()
        mp.setenv("RLM_INDEX_DIR", str(root.parent / "idx"))
        try:
            reader = IndexReader(IndexBuilder().build(str(root)))
        finally:
            mp.undo()
        yield {"cf": cf, "idx": _bsl_for(root, reader), "live": _bsl_for(root)}
        reader.close()

    @pytest.mark.parametrize("helpers", ["idx", "live"])
    def test_characteristic_typeset_attr_type(self, char_tree, helpers):
        """CF прежде — `[]`; EDT (обе версии) — тот же токен."""
        assert _attr_types(char_tree[helpers], "Документ.Док")["Аналитика"] == ["Characteristic.ВидыСубконто"]

    @pytest.mark.parametrize("helpers", ["idx", "live"])
    def test_characteristic_typeset_form_types(self, char_tree, helpers):
        expected = ["cfg:Characteristic.ВидыСубконто"] if char_tree["cf"] else ["Characteristic.ВидыСубконто"]
        assert _form_attr_types(char_tree[helpers], "Документ.Док")["Аналитика"] == expected

    @pytest.mark.parametrize("helpers", ["idx", "live"])
    def test_characteristic_typeset_gives_no_ref_boundary(self, char_tree, helpers):
        """Обе версии, граница: реквизит хранит значения типов ПВХ, а не ссылку на него, — строки на ПВХ нет."""
        refs = _refs(char_tree[helpers], "ПланВидовХарактеристик.ВидыСубконто")
        assert not [r for r in refs if ".Аналитика." in r[1]], refs

    @pytest.mark.parametrize("route", ["full", "git"])
    def test_update_equals_build(self, world18, route, tmp_path_factory, monkeypatch):
        files_b = dict(world18["files"])
        cf = world18["fmt"] == "cf"
        rel = "Documents/Док.xml" if cf else "Documents/Док/Док.mdo"
        old, new = (
            ("cfg:DefinedType.ДенежнаяСумма", "cfg:DefinedType.ПредметОбъект")
            if cf
            else ("<types>DefinedType.ДенежнаяСумма</types>", "<types>DefinedType.ПредметОбъект</types>")
        )
        assert files_b[rel].count(old) == 1
        files_b[rel] = files_b[rel].replace(old, new)
        db = _assert_update_equals_build(
            tmp_path_factory,
            monkeypatch,
            world18["write"],
            world18["files"],
            files_b,
            {
                "object_attributes": "SELECT object_name, category, attr_name, attr_synonym, attr_type, attr_kind, "
                "ts_name, source_file FROM object_attributes WHERE object_name = 'Док'",
                "metadata_references": "SELECT source_object, source_category, ref_object, ref_kind, used_in, path, "
                "line FROM metadata_references WHERE source_object = 'Док'",
            },
            route=route,
            expect_pointwise=("Documents", "Док"),
        )
        assert _rows(
            db, "SELECT attr_type FROM object_attributes WHERE object_name = 'Док' AND attr_name = 'Сумма'"
        ) == [('["DefinedType.ПредметОбъект"]',)]


# ═══════════════ Задача 5 — объект выгрузки Конфигуратора без каталога (Д18) ═══════════════

_SIBLING_ONLY_REF = "InformationRegister.БезКаталога.Dimension.Контрагент.Type"


class TestDescriptorWithoutDirectory:
    @pytest.mark.parametrize("world18", ["cf"], indirect=True)
    def test_sibling_only_register_references(self, world18):
        assert not (world18["root"] / "InformationRegisters" / "БезКаталога").exists()  # только описатель
        assert ("attribute_type", _SIBLING_ONLY_REF) in _refs(world18["idx"], "Справочник.Спр")

    @pytest.mark.parametrize("world18", ["cf"], indirect=True)
    def test_sibling_only_attribute_by_name(self, world18):
        rows = world18["idx"]["find_attributes"](name="Контрагент")
        assert ("БезКаталога", "InformationRegisters", "dimension") in {
            (r["object_name"], r["category"], r["attr_kind"]) for r in rows
        }, rows

    @pytest.mark.parametrize("world18", ["cf"], indirect=True)
    def test_full_structure_from_index(self, world18):
        idx = world18["idx"]["get_object_full_structure"]("РегистрСведений.БезКаталога")
        live = world18["live"]["get_object_full_structure"]("РегистрСведений.БезКаталога")
        assert idx["_meta"]["index_used"] is True, idx["_meta"]
        assert [(d["name"], d["type"]) for d in idx["dimensions"]] == [("Контрагент", ["CatalogRef.Спр"])], idx
        # живой разбор CF — сырая форма типа; прочий состав ответа совпадает
        assert [(d["name"], d["type"]) for d in live["dimensions"]] == [("Контрагент", ["cfg:CatalogRef.Спр"])], live
        same = {k for k in idx if k not in ("_meta", "dimensions")}
        assert {k: idx[k] for k in same} == {k: live[k] for k in same}

    @pytest.mark.parametrize("route", ["full", "git"])
    @pytest.mark.parametrize("world18", ["cf"], indirect=True)
    def test_pointwise_update_equals_build_for_sibling_only(self, world18, route, tmp_path_factory, monkeypatch):
        files_b = dict(world18["files"])
        rel = "InformationRegisters/БезКаталога.xml"
        files_b[rel] = files_b[rel].replace(
            "</ChildObjects>",
            _cf_child("Resource", "Сумма", types=["cfg:CatalogRef.БезСинонима"]) + "\n</ChildObjects>",
        )
        assert files_b[rel] != world18["files"][rel]
        db = _assert_update_equals_build(
            tmp_path_factory,
            monkeypatch,
            world18["write"],
            world18["files"],
            files_b,
            {
                "object_attributes": "SELECT object_name, category, attr_name, attr_type, attr_kind, source_file "
                "FROM object_attributes WHERE object_name = 'БезКаталога'",
                "metadata_references": "SELECT source_object, source_category, ref_object, ref_kind, used_in, path, "
                "line FROM metadata_references WHERE source_object = 'БезКаталога'",
            },
            route=route,
            expect_pointwise=("InformationRegisters", "БезКаталога"),
        )
        assert _rows(db, "SELECT attr_name FROM object_attributes WHERE object_name = 'БезКаталога'") == [
            ("Контрагент",),
            ("Сумма",),
        ]

    @pytest.mark.parametrize("helpers", ["idx", "live"])
    def test_by_object_name_unchanged(self, world18, helpers):
        """Обе версии: ответ по имени объекта — те же строки (на HEAD — живым откатом)."""
        rows = world18[helpers]["find_attributes"](object_name="РегистрСведений.БезКаталога")
        assert [(r["attr_name"], r["attr_kind"], r["attr_type"]) for r in rows] == [
            ("Контрагент", "dimension", ["CatalogRef.Спр"])
        ], rows

    @pytest.mark.parametrize("world18", ["edt"], indirect=True)
    def test_edt_unchanged(self, world18):
        """Обе версии: EDT — объект всегда в своём каталоге, ответы прежние."""
        assert ("attribute_type", _SIBLING_ONLY_REF) in _refs(world18["idx"], "Справочник.Спр")
        rows = world18["idx"]["find_attributes"](name="Контрагент")
        assert ("БезКаталога", "InformationRegisters") in {(r["object_name"], r["category"]) for r in rows}


# ═══════════════ Задача 6 — реквизиты и ссылки описателей вне шести категорий (Д3) ═══════════════

_NEW_ATTR_HEADS = (
    "DataProcessor.",
    "Report.",
    "BusinessProcess.",
    "Task.",
    "ExchangePlan.",
    "ChartOfAccounts.",
    "ChartOfCalculationTypes.",
    "CalculationRegister.",
    "Constant.",
)


def _attr_key(rows) -> list[tuple]:
    return sorted((r["attr_name"], r["attr_kind"], tuple(r["attr_type"]), r["ts_name"] or "") for r in rows)


def _edit(files: dict[str, str], rel: str, old: str, new: str) -> dict[str, str]:
    """Копия дерева с одной правкой ровно одного вхождения ``old`` в файле ``rel``."""
    assert files[rel].count(old) == 1, (rel, old)
    out = dict(files)
    out[rel] = files[rel].replace(old, new)
    return out


def _descriptor(world: World, category: str, name: str) -> str:
    return f"{category}/{name}.xml" if world["fmt"] == "cf" else f"{category}/{name}/{name}.mdo"


def _type_token(world: World, ref_type: str) -> str:
    return f"cfg:{ref_type}" if world["fmt"] == "cf" else f"<types>{ref_type}</types>"


def _object_queries(name: str) -> dict[str, str]:
    return {
        "object_attributes": "SELECT object_name, category, attr_name, attr_synonym, attr_type, attr_kind, ts_name, "
        f"source_file FROM object_attributes WHERE object_name = '{name}'",
        "metadata_references": "SELECT source_object, source_category, ref_object, ref_kind, used_in, path, line "
        f"FROM metadata_references WHERE source_object = '{name}'",
    }


class TestAttrsAndRefsOutsideSixCategories:
    def test_data_processor_attribute_ref(self, world18):
        assert ("attribute_type", "DataProcessor.Обр.Attribute.Ссылка.Type") in _refs(world18["idx"], "Справочник.Спр")

    def test_task_addressing_attribute_ref(self, world18):
        assert ("attribute_type", "Task.Зад.AddressingAttribute.Исполнитель.Type") in _refs(
            world18["idx"], "Справочник.Спр"
        )

    def test_constant_type_ref(self, world18):
        assert ("attribute_type", "Constant.Конст.Type") in _refs(world18["idx"], "ОпределяемыйТип.ДенежнаяСумма")

    def test_index_equals_live_refs(self, world18):
        seen = 0
        for ref in ("Справочник.Спр", "Документ.Док", "ОпределяемыйТип.ДенежнаяСумма", "Перечисление.Виды"):
            idx, live = (
                {r for r in _refs(world18[h], ref, kinds=["attribute_type"]) if r[1].startswith(_NEW_ATTR_HEADS)}
                for h in ("idx", "live")
            )
            assert idx == live, (ref, idx, live)
            seen += len(idx)
        assert seen >= 6, seen

    def test_find_attributes_by_name_finds_new_categories(self, world18):
        rows = world18["idx"]["find_attributes"](name="Ссылка", category="DataProcessors")
        assert ("Обр", "DataProcessors", "attribute") in {
            (r["object_name"], r["category"], r["attr_kind"]) for r in rows
        }

    @pytest.mark.parametrize("obj", ["Обработка.Обр", "Задача.Зад", "БизнесПроцесс.БП"])
    def test_find_attributes_object_index_equals_live(self, world18, obj):
        idx = world18["idx"]["find_attributes"](object_name=obj)
        assert idx, obj
        assert _attr_key(idx) == _attr_key(world18["live"]["find_attributes"](object_name=obj))

    @pytest.mark.parametrize("helpers", ["idx", "live"])
    def test_addressing_attribute_kind(self, world18, helpers):
        rows = world18[helpers]["find_attributes"](object_name="Задача.Зад", kind="addressing_attribute")
        assert [(r["attr_name"], r["attr_type"], r["ts_name"]) for r in rows] == [
            ("Исполнитель", ["CatalogRef.Спр"], None)
        ]

    def test_full_structure_addressing_key(self, world18):
        expected = [{"name": "Исполнитель", "synonym": "", "type": ["CatalogRef.Спр"]}]
        for helpers in ("idx", "live"):
            res = world18[helpers]["get_object_full_structure"]("Задача.Зад")
            assert [dict(a) for a in res["addressing_attributes"]] == expected, (helpers, res)
            assert "addressing_attributes" not in world18[helpers]["get_object_full_structure"]("Документ.Док")

    @pytest.mark.parametrize("world18", ["cf"], indirect=True)
    def test_full_structure_new_category_type_is_index_form(self, world18):
        idx = world18["idx"]["get_object_full_structure"]("Обработка.Обр")
        live = world18["live"]["get_object_full_structure"]("Обработка.Обр")
        assert idx["_meta"]["index_used"] is True, idx["_meta"]
        assert {a["name"]: a["type"] for a in idx["attributes"]}["Ссылка"] == ["CatalogRef.Спр"]
        assert {a["name"]: a["type"] for a in live["attributes"]}["Ссылка"] == ["cfg:CatalogRef.Спр"]

    @pytest.mark.parametrize("world18", ["cf"], indirect=True)
    def test_full_structure_enum_values_new_category(self, world18):
        idx = world18["idx"]["get_object_full_structure"]("Обработка.Обр")
        assert [v["name"] for v in idx["enum_values_for_typed_refs"]["EnumRef.Виды"]] == ["Первый", "Второй"]
        # без индекса — как на HEAD: `cfg:EnumRef.Виды` раскрытие не узнаёт
        assert world18["live"]["get_object_full_structure"]("Обработка.Обр")["enum_values_for_typed_refs"] == {}

    def test_addressing_ref_has_line(self, world18):
        lines = world18["files"][_descriptor(world18, "Tasks", "Зад")].splitlines()
        token = "<v8:Type>cfg:CatalogRef.Спр</v8:Type>" if world18["fmt"] == "cf" else "<types>CatalogRef.Спр</types>"
        rows = world18["idx"]["find_references_to_object"]("Справочник.Спр")["references"]
        got = [r["line"] for r in rows if r["used_in"] == "Task.Зад.AddressingAttribute.Исполнитель.Type"]
        assert got == [lines.index(token) + 1], got

    @pytest.mark.parametrize("helpers", ["idx", "live"])
    def test_unknown_kind_still_rejected(self, world18, helpers):
        """Обе версии: вид вне закрытого набора — отказ."""
        with pytest.raises(ValueError):
            world18[helpers]["find_attributes"](object_name="Задача.Зад", kind="column")

    @pytest.mark.parametrize("route", ["full", "git"])
    def test_chart_of_accounts_pointwise_equals_build(self, world18, route, tmp_path_factory, monkeypatch):
        rel = _descriptor(world18, "ChartsOfAccounts", "ПС")
        old, new = _type_token(world18, "CatalogRef.Спр"), _type_token(world18, "CatalogRef.БезСинонима")
        files_b = _edit(world18["files"], rel, old, new)
        db = _assert_update_equals_build(
            tmp_path_factory,
            monkeypatch,
            world18["write"],
            world18["files"],
            files_b,
            _object_queries("ПС"),
            route=route,
            expect_pointwise=("ChartsOfAccounts", "ПС"),
        )
        sql = "SELECT ref_object, used_in FROM metadata_references WHERE ref_kind = ? AND source_object = ?"
        assert _rows(db, sql, ("attribute_type", "ПС")) == [
            ("Catalog.БезСинонима", "ChartOfAccounts.ПС.Attribute.Подразделение.Type")
        ]

    @pytest.mark.parametrize("route", ["full", "git"])
    def test_data_processor_bulk_equals_build(self, world18, route, tmp_path_factory, monkeypatch):
        rel = _descriptor(world18, "DataProcessors", "Обр")
        old, new = _type_token(world18, "CatalogRef.Спр"), _type_token(world18, "CatalogRef.БезСинонима")
        files_b = _edit(world18["files"], rel, old, new)
        db = _assert_update_equals_build(
            tmp_path_factory,
            monkeypatch,
            world18["write"],
            world18["files"],
            files_b,
            _object_queries("Обр"),
            route=route,
            expect_pointwise=None,
        )
        sql = "SELECT attr_type FROM object_attributes WHERE object_name = ? AND attr_name = ?"
        assert _rows(db, sql, ("Обр", "Ссылка")) == [('["CatalogRef.БезСинонима"]',)]

    def test_based_on_not_duplicated(self, tmp_path, monkeypatch):
        """Обе версии: бизнес-процесс с вводом на основании документа — одна строка `based_on`."""
        root = _edt_doc_tree(
            tmp_path / "src",
            {
                "Documents/Заказ/Заказ.mdo": _edt_md("Document", "Заказ"),
                "Documents/Заказ/ObjectModule.bsl": _PROC,
                "BusinessProcesses/Задание/Задание.mdo": _edt_md(
                    "BusinessProcess",
                    "Задание",
                    "<basedOn>Document.Заказ</basedOn><attributes><name>Р</name><type><types>String</types></type>"
                    "</attributes>",
                ),
            },
        )
        reader = _reader_for(root, monkeypatch)
        try:
            rows = reader.find_metadata_references("Document.Заказ", kinds=["based_on"])
        finally:
            reader.close()
        assert [r["used_in"] for r in rows] == ["BusinessProcess.Задание.BasedOn"], rows

    def test_every_attr_category_is_ref_trigger(self):
        for category in [*BI._ATTR_CATEGORIES, "Constants"]:
            assert category in BI._METADATA_REFERENCES_TRIGGER_CATEGORIES, category

    def test_constant_refs_bulk_equals_build(self, world18, tmp_path_factory, monkeypatch):
        rel = _descriptor(world18, "Constants", "Конст")
        files_b = _edit(world18["files"], rel, "DefinedType.ДенежнаяСумма", "DefinedType.ПредметОбъект")
        db = _assert_update_equals_build(
            tmp_path_factory,
            monkeypatch,
            world18["write"],
            world18["files"],
            files_b,
            {
                "metadata_references": "SELECT source_object, ref_object, ref_kind, used_in, path, line "
                "FROM metadata_references WHERE source_category = 'Constants'",
                "constants": "SELECT name, synonym, value_type, file FROM constants",
            },
            route="git",
            expect_pointwise=None,
        )
        sql = "SELECT ref_object, used_in FROM metadata_references WHERE source_category = ?"
        assert _rows(db, sql, ("Constants",)) == [("DefinedType.ПредметОбъект", "Constant.Конст.Type")]


# ═══════════════ Задача 7 — команды объектов: CF, журналы и критерии, живой маршрут (Д4) ═══════════════


def _command_refs(helpers, ref: str) -> set[str]:
    return {used for kind, used in _refs(helpers, ref) if kind == "command_parameter_type"}


def _command_query(name: str) -> dict[str, str]:
    return {
        "commands": "SELECT source_object, source_category, ref_object, used_in, path, line FROM metadata_references "
        f"WHERE ref_kind = 'command_parameter_type' AND source_object = '{name}'"
    }


class TestObjectCommands:
    def test_cf_object_command_ref(self, world18):
        """На EDT — та же строка «как прежде» (индекс v17 её уже отдавал)."""
        assert "DataProcessor.Обр.Command.Открыть.CommandParameterType" in _command_refs(world18["idx"], "Документ.Док")

    def test_no_duplicate_with_commands_xml(self, world18, tmp_path, monkeypatch):
        files = dict(world18["files"])
        if world18["fmt"] == "cf":
            files["DataProcessors/Обр/Commands/Открыть.xml"] = _cf_obj(
                "Command",
                "Открыть",
                props=["<CommandParameterType>\n<v8:Type>cfg:DocumentRef.Док</v8:Type>\n</CommandParameterType>"],
            )
        else:
            files["DataProcessors/Обр/Commands/Открыть/Открыть.mdo"] = _edt_obj(
                "Command",
                "Открыть",
                body=["<commandParameterType>\n<types>DocumentRef.Док</types>\n</commandParameterType>"],
            )
        root = tmp_path / "cf"
        _write_tree(root, files)
        reader = _reader_for(root, monkeypatch)
        try:
            ext_root = tmp_path / "cfe" / "Расш1" if world18["fmt"] == "cf" else None
            for helpers in (_world18_helpers(root, ext_root, reader), _world18_helpers(root, ext_root, None)):
                rows = [
                    r
                    for r in helpers["find_references_to_object"]("Документ.Док")["references"]
                    if r["used_in"] == "DataProcessor.Обр.Command.Открыть.CommandParameterType"
                ]
                assert len(rows) == 1, rows
        finally:
            reader.close()

    def test_journal_command_ref(self, world18):
        assert "DocumentJournal.Жур.Command.Печать.CommandParameterType" in _command_refs(
            world18["idx"], "Документ.Док"
        )

    @pytest.mark.parametrize("ref", ["Документ.Док", "Справочник.БезСинонима"])
    def test_live_equals_index_object_commands(self, world18, ref):
        idx = _command_refs(world18["idx"], ref)
        assert idx, ref
        assert _command_refs(world18["live"], ref) == idx

    @pytest.mark.parametrize("route", ["full", "git"])
    def test_document_owner_pointwise_equals_build(self, world18, route, tmp_path_factory, monkeypatch):
        rel = _descriptor(world18, "Documents", "Док")
        files_b = _edit(
            world18["files"],
            rel,
            _type_token(world18, "CatalogRef.БезСинонима"),
            _type_token(world18, "CatalogRef.Спр"),
        )
        db = _assert_update_equals_build(
            tmp_path_factory,
            monkeypatch,
            world18["write"],
            world18["files"],
            files_b,
            _command_query("Док"),
            route=route,
            expect_pointwise=("Documents", "Док"),
        )
        sql = "SELECT ref_object, used_in FROM metadata_references WHERE ref_kind = ? AND source_object = ?"
        assert _rows(db, sql, ("command_parameter_type", "Док")) == [
            ("Catalog.Спр", "Document.Док.Command.Заполнить.CommandParameterType")
        ]

    @pytest.mark.parametrize("route", ["full", "git"])
    def test_data_processor_bulk_equals_build(self, world18, route, tmp_path_factory, monkeypatch):
        rel = _descriptor(world18, "DataProcessors", "Обр")
        files_b = _edit(
            world18["files"], rel, _type_token(world18, "DocumentRef.Док"), _type_token(world18, "CatalogRef.Спр")
        )
        db = _assert_update_equals_build(
            tmp_path_factory,
            monkeypatch,
            world18["write"],
            world18["files"],
            files_b,
            _command_query("Обр"),
            route=route,
            expect_pointwise=None,
        )
        sql = "SELECT ref_object FROM metadata_references WHERE ref_kind = ? AND source_object = ?"
        assert _rows(db, sql, ("command_parameter_type", "Обр")) == [("Catalog.Спр",)]

    def test_journal_bulk_equals_build(self, world18, tmp_path_factory, monkeypatch):
        rel = _descriptor(world18, "DocumentJournals", "Жур")
        files_b = _edit(
            world18["files"], rel, _type_token(world18, "DocumentRef.Док"), _type_token(world18, "CatalogRef.Спр")
        )
        db = _assert_update_equals_build(
            tmp_path_factory,
            monkeypatch,
            world18["write"],
            world18["files"],
            files_b,
            _command_query("Жур"),
            route="git",
            expect_pointwise=None,
        )
        sql = "SELECT ref_object, used_in FROM metadata_references WHERE ref_kind = ? AND source_object = ?"
        assert _rows(db, sql, ("command_parameter_type", "Жур")) == [
            ("Catalog.Спр", "DocumentJournal.Жур.Command.Печать.CommandParameterType")
        ]

    def test_command_hosts_are_ref_trigger_categories(self):
        """Цикл команд сборки гейтится `_ref_allowed(category)`: категория вне триггеров bulk-fallback не сотрёт."""
        assert set(BI._CMD_HOST_CATS) <= BI._METADATA_REFERENCES_TRIGGER_CATEGORIES
        assert {"DocumentJournals", "FilterCriteria"} <= set(BI._CMD_HOST_CATS)
        assert BI._CATEGORY_TO_TYPE_PREFIX["FilterCriteria"] == "FilterCriterion"

    def test_cf_parser_reads_type_and_type_set(self):
        from rlm_tools_bsl.bsl_xml_parsers import parse_object_commands_parameter_types

        text = _cf_obj(
            "Catalog",
            "Спр",
            children=[
                _cf_command("Первая", ["cfg:DocumentRef.Док"], ["cfg:DefinedType.ОпрТип"]),
                _cf_command("БезТипа"),
            ],
        )
        assert parse_object_commands_parameter_types(text) == [
            {"command_name": "Первая", "ref_object": "Document.Док"},
            {"command_name": "Первая", "ref_object": "DefinedType.ОпрТип"},
        ]


# ═══════════════ Задача 8 — журналы, критерии отбора, связующие свойства; виды без строк (Д6, Д8) ═══════════════

_NEW_REF_KINDS_V18 = ("document_journal_content", "filter_criterion_type", "filter_criterion_content", "linked_object")
_DEAD_REF_KINDS = ("main_form", "list_form", "choice_parameter_link", "link_by_type")


def _linked(text: str) -> list[tuple[str, str]]:
    from rlm_tools_bsl.bsl_xml_parsers import parse_metadata_xml

    refs = parse_metadata_xml(text).get("references", [])
    return [(r["ref_object"], r["used_in_suffix"]) for r in refs if r["ref_kind"] == "linked_object"]


class TestNewRefKinds:
    @pytest.mark.parametrize("helpers", ["idx", "live"])
    def test_journal_registers_document(self, world18, helpers):
        refs = _refs(world18[helpers], "Документ.Док")
        assert ("document_journal_content", "DocumentJournal.Жур.RegisteredDocuments") in refs

    @pytest.mark.parametrize("helpers", ["idx", "live"])
    def test_filter_criterion_type_and_content(self, world18, helpers):
        """Ровно две строки: тип и ОДНА строка состава, хотя в составе два реквизита `Док`."""
        res = world18[helpers]["find_references_to_object"]("Документ.Док")
        rows = sorted(
            (r["kind"], r["used_in"]) for r in res["references"] if r["used_in"].startswith("FilterCriterion.")
        )
        assert rows == [
            ("filter_criterion_content", "FilterCriterion.Крит.Content"),
            ("filter_criterion_type", "FilterCriterion.Крит.Type"),
        ]

    @pytest.mark.parametrize("helpers", ["idx", "live"])
    def test_accounting_register_links_chart_of_accounts(self, world18, helpers):
        assert ("linked_object", "AccountingRegister.РБ.ChartOfAccounts") in _refs(world18[helpers], "ПланСчетов.ПС")

    @pytest.mark.parametrize("helpers", ["idx", "live"])
    def test_chart_of_accounts_ext_dimension_types(self, world18, helpers):
        refs = _refs(world18[helpers], "ПланВидовХарактеристик.ВидыСубконто")
        assert ("linked_object", "ChartOfAccounts.ПС.ExtDimensionTypes") in refs

    @pytest.mark.parametrize("helpers", ["idx", "live"])
    def test_business_process_task(self, world18, helpers):
        assert ("linked_object", "BusinessProcess.БП.Task") in _refs(world18[helpers], "Задача.Зад")

    @pytest.mark.parametrize("helpers", ["idx", "live"])
    @pytest.mark.parametrize(
        "kind, ref",
        [
            ("document_journal_content", "Документ.Док"),
            ("filter_criterion_type", "Документ.Док"),
            ("filter_criterion_content", "Документ.Док"),
            ("linked_object", "ПланСчетов.ПС"),
        ],
    )
    def test_kinds_filter_accepts_new_kinds(self, world18, helpers, kind, ref):
        res = world18[helpers]["find_references_to_object"](ref, kinds=[kind])
        assert res["references"] and {r["kind"] for r in res["references"]} == {kind}, res
        assert res["_meta"]["kinds_applied"] == [kind]
        assert all(r["line"] is None for r in res["references"])

    def test_find_data_path_walks_linked_object(self, world18):
        res = world18["idx"]["find_data_path"]("РегистрБухгалтерии.РБ", "ПланСчетов.ПС")
        assert res["found"] is True, res
        assert [step["kind"] for step in res["path"]] == ["linked_object"]

    def test_kinds_unavailable_on_v17(self, world18, tmp_path):
        reader = _v17_copy(world18, tmp_path)
        try:
            helpers = _world18_helpers(world18["root"], world18["ext_root"], reader)
            res = helpers["find_references_to_object"]("Документ.Док")
            assert res["_meta"]["source"] == "index"
            assert set(_NEW_REF_KINDS_V18) <= set(res["_meta"]["kinds_unavailable"]), res["_meta"]
            assert res["_meta"]["kinds_unavailable"] == sorted(res["_meta"]["kinds_unavailable"])
            res = helpers["find_references_to_object"]("Документ.Док", kinds=["owner", "linked_object"])
            assert res["_meta"]["kinds_unavailable"] == ["linked_object"]
            res = helpers["find_references_to_object"]("Документ.Док", kinds=["owner"])
            assert res["_meta"]["kinds_unavailable"] == []
        finally:
            reader.close()
        for helpers in ("idx", "live"):
            res = world18[helpers]["find_references_to_object"]("Документ.Док")
            assert res["_meta"]["kinds_unavailable"] == [], helpers
        # Ключ есть всегда — и в раннем структурном ответе (голое имя, которого нет).
        res = world18["idx"]["find_references_to_object"]("НетТакогоОбъекта")
        assert res["_meta"]["kinds_unavailable"] == []

    def test_dead_kinds_documented(self, world18):
        import inspect

        doc = inspect.getdoc(world18["idx"]["find_references_to_object"])
        assert "не производятся" in doc
        for kind in _DEAD_REF_KINDS:
            assert kind in doc, kind
        for kind in _NEW_REF_KINDS_V18:
            assert kind in doc, kind
        helpers_md = (Path(__file__).resolve().parents[1] / "docs" / "HELPERS.md").read_text(encoding="utf-8")
        (bullet,) = [ln for ln in helpers_md.splitlines() if ln.startswith("- `find_references_to_object(")]
        assert "не производятся" in bullet
        for kind in _DEAD_REF_KINDS + _NEW_REF_KINDS_V18:
            assert f"`{kind}`" in bullet, kind
        assert "18 видов" not in bullet

    def test_filter_criteria_bulk_equals_build(self, world18, tmp_path_factory, monkeypatch):
        rel = _descriptor(world18, "FilterCriteria", "Крит")
        files_b = _edit(
            world18["files"], rel, _type_token(world18, "DocumentRef.Док"), _type_token(world18, "CatalogRef.Спр")
        )
        db = _assert_update_equals_build(
            tmp_path_factory,
            monkeypatch,
            world18["write"],
            world18["files"],
            files_b,
            _object_queries("Крит"),
            route="git",
            expect_pointwise=None,
        )
        sql = "SELECT ref_kind, ref_object, used_in FROM metadata_references WHERE source_object = ?"
        assert _rows(db, sql, ("Крит",)) == [
            ("filter_criterion_content", "Document.Док", "FilterCriterion.Крит.Content"),
            ("filter_criterion_type", "Catalog.Спр", "FilterCriterion.Крит.Type"),
        ]

    @pytest.mark.parametrize(
        "category, name, old, new, pointwise",
        [
            ("AccountingRegisters", "РБ", "ChartOfAccounts.ПС", "ChartOfAccounts.ПС2", True),
            ("BusinessProcesses", "БП", "Task.Зад", "Task.Зад2", False),
            ("DocumentJournals", "Жур", "Document.Док", "Document.Док2", False),
        ],
    )
    def test_linked_and_journal_update_equals_build(
        self, world18, category, name, old, new, pointwise, tmp_path_factory, monkeypatch
    ):
        files_b = _edit(world18["files"], _descriptor(world18, category, name), old, new)
        db = _assert_update_equals_build(
            tmp_path_factory,
            monkeypatch,
            world18["write"],
            world18["files"],
            files_b,
            _object_queries(name),
            route="git",
            expect_pointwise=(category, name) if pointwise else None,
        )
        sql = "SELECT ref_object FROM metadata_references WHERE source_object = ? AND ref_kind IN (?, ?)"
        assert _rows(db, sql, (name, "linked_object", "document_journal_content")) == [(new,)]

    @pytest.mark.parametrize("fmt", ["cf", "edt"])
    def test_calculation_links_parser(self, fmt):
        """Регистр расчёта (план видов расчёта, график) и план видов расчёта (базовые виды) — закрытый перечень."""
        if fmt == "cf":
            reg = _cf_obj(
                "CalculationRegister",
                "РР",
                props=[
                    "<ChartOfCalculationTypes>ChartOfCalculationTypes.ПВР</ChartOfCalculationTypes>",
                    "<Schedule>InformationRegister.График</Schedule>",
                ],
            )
            chart = _cf_obj(
                "ChartOfCalculationTypes",
                "ПВР",
                props=[
                    _cf_ref_items("BaseCalculationTypes", ["ChartOfCalculationTypes.ПВР", "ChartOfCalculationTypes.Б"])
                ],
            )
        else:
            reg = _edt_obj(
                "CalculationRegister",
                "РР",
                body=[
                    "<schedule>InformationRegister.График</schedule>",
                    "<chartOfCalculationTypes>ChartOfCalculationTypes.ПВР</chartOfCalculationTypes>",
                ],
            )
            chart = _edt_obj(
                "ChartOfCalculationTypes",
                "ПВР",
                body=[
                    "<baseCalculationTypes>ChartOfCalculationTypes.ПВР</baseCalculationTypes>",
                    "<baseCalculationTypes>ChartOfCalculationTypes.Б</baseCalculationTypes>",
                ],
            )
        assert sorted(_linked(reg)) == [
            ("ChartOfCalculationTypes.ПВР", "ChartOfCalculationTypes"),
            ("InformationRegister.График", "Schedule"),
        ]
        assert _linked(chart) == [
            ("ChartOfCalculationTypes.ПВР", "BaseCalculationTypes"),
            ("ChartOfCalculationTypes.Б", "BaseCalculationTypes"),
        ]

    def test_edt_ext_dimension_types_direct_child_only(self):
        """`extDimensionTypes` в EDT бывает и вложенным (`producedTypes`, табличные части) — свойство только у корня."""
        text = _edt_obj(
            "ChartOfAccounts",
            "ПС",
            body=[
                '<producedTypes>\n<extDimensionTypes typeId="1" valueTypeId="2"/>\n</producedTypes>',
                "<extDimensionTypes>ChartOfCharacteristicTypes.ВидыСубконто</extDimensionTypes>",
                "<standardTabularSections>\n<extDimensionTypes>X</extDimensionTypes>\n</standardTabularSections>",
            ],
        )
        assert _linked(text) == [("ChartOfCharacteristicTypes.ВидыСубконто", "ExtDimensionTypes")]

    @pytest.mark.parametrize("fmt", ["cf", "edt"])
    def test_filter_content_one_row_per_object(self, fmt):
        from rlm_tools_bsl.bsl_xml_parsers import parse_metadata_xml

        items = [
            "Document.Д1.Attribute.А",
            "Document.Д1.TabularSection.Т.Attribute.Б",
            "Document.Д2.Attribute.В",
            "Document.Д1.Attribute.Г",
        ]
        if fmt == "cf":
            text = _cf_obj(
                "FilterCriterion", "К", props=[_cf_type(["cfg:DocumentRef.Д1"]), _cf_ref_items("Content", items)]
            )
        else:
            text = _edt_obj(
                "FilterCriterion",
                "К",
                body=["<type>\n<types>DocumentRef.Д1</types>\n</type>", *[f"<content>{i}</content>" for i in items]],
            )
        refs = [(r["ref_kind"], r["ref_object"], r["used_in_suffix"]) for r in parse_metadata_xml(text)["references"]]
        assert refs == [
            ("filter_criterion_type", "Document.Д1", "Type"),
            ("filter_criterion_content", "Document.Д1", "Content"),
            ("filter_criterion_content", "Document.Д2", "Content"),
        ]

    def test_reader_priority_is_the_only_map(self):
        """Одна карта приоритетов: хелперы строят свою из карты ридера (прежде — ручная копия)."""
        from rlm_tools_bsl.bsl_index import IndexReader

        priority = IndexReader._REF_KIND_SQL_PRIORITY
        assert [priority[k] for k in _NEW_REF_KINDS_V18] == [18, 19, 20, 21]
        assert sorted(priority.values()) == list(range(len(priority)))


# ═══════════════ Задача 9 — подписка на определяемый тип (Д5) ═══════════════


class TestDefinedTypeSubscriptionRef:
    def test_defined_type_finds_subscription(self, world18):
        refs = _refs(world18["idx"], "ОпределяемыйТип.ДенежнаяСумма")
        assert ("event_subscription_source", "EventSubscription.ПодпНабор.Source") in refs

    def test_member_expansion_unchanged(self, world18):
        """Обе версии: член определяемого типа — строка раскрытия на чтении, как на HEAD."""
        res = world18["idx"]["find_references_to_object"]("Документ.Док")
        refs = {(r["kind"], r["used_in"]) for r in res["references"]}
        assert ("event_subscription_source", "EventSubscription.ПодпОбъект.SourceTypeSet") in refs
        assert ("event_subscription_source", "EventSubscription.ПодпОбъект.Source") not in refs
        assert res["_meta"]["expanded_on_read"] == {"event_subscription_source": 1}

    def test_defined_type_with_members_has_index_row(self, world18):
        refs = _refs(world18["idx"], "ОпределяемыйТип.ПредметОбъект")
        assert ("event_subscription_source", "EventSubscription.ПодпОбъект.Source") in refs

    def test_platform_set_gives_no_row(self):
        """Бесточечный платформенный набор (`DocumentObject`) строк не даёт — раскрытие на чтении не меняется."""
        rows = BI._event_subscription_source_rows(
            {
                "name": "П",
                "source_types": ["DocumentObject.Док"],
                "source_type_sets": ["DocumentObject", "DefinedType.Х"],
            },
            "EventSubscriptions/П.xml",
        )
        assert [r[2] for r in rows] == ["Document.Док", "DefinedType.Х"]

    @pytest.mark.parametrize("route", ["full", "git"])
    def test_subscription_pointwise_equals_build(self, world18, route, tmp_path_factory, monkeypatch):
        rel = _descriptor(world18, "EventSubscriptions", "ПодпНабор")
        files_b = _edit(world18["files"], rel, "DefinedType.ДенежнаяСумма", "DefinedType.ПредметОбъект")
        queries = {
            "metadata_references": "SELECT source_object, source_category, ref_object, ref_kind, used_in, path, line "
            "FROM metadata_references WHERE source_category = 'EventSubscriptions'",
            "event_subscriptions": "SELECT name, source_types, source_type_sets, file FROM event_subscriptions",
        }
        db = _assert_update_equals_build(
            tmp_path_factory,
            monkeypatch,
            world18["write"],
            world18["files"],
            files_b,
            queries,
            route=route,
            expect_pointwise=("EventSubscriptions", "ПодпНабор"),
        )
        sql = "SELECT ref_object, used_in FROM metadata_references WHERE source_object = ?"
        assert _rows(db, sql, ("ПодпНабор",)) == [("DefinedType.ПредметОбъект", "EventSubscription.ПодпНабор.Source")]


# ═══════════════ Задача 10 — ссылки из форм и текстов их запросов (Д7) ═══════════════

_FORM_KINDS = ("form_attribute_type", "form_main_table", "form_query")
_DOC_FORM = "Document.Док.Form.ФормаДокумента"


def _form_refs(helpers, ref: str) -> set[tuple[str, str]]:
    return {(kind, used) for kind, used in _refs(helpers, ref) if kind in _FORM_KINDS}


def _form_rows_query(name: str) -> dict[str, str]:
    kinds = ", ".join(f"'{k}'" for k in _FORM_KINDS)
    return {
        "form_refs": "SELECT source_object, source_category, ref_object, ref_kind, used_in, path, line "
        f"FROM metadata_references WHERE ref_kind IN ({kinds})",
        "object_refs": "SELECT source_object, source_category, ref_object, ref_kind, used_in, path, line "
        f"FROM metadata_references WHERE source_object = '{name}'",
    }


def _parse_refs(text: str) -> tuple[list[tuple[str, str, str]], list[tuple[str, str]]]:
    from rlm_tools_bsl.bsl_xml_parsers import _parse_form_root, parse_form_type_refs

    root, fmt = _parse_form_root(text)
    refs, texts = parse_form_type_refs(root, fmt)
    return [(r["kind"], r["ref_object"], r["used_in_suffix"]) for r in refs], texts


class TestFormRefs:
    def test_form_attribute_column_parameter_main_table(self, world18):
        spr = _form_refs(world18["idx"], "Справочник.Спр")
        assert ("form_attribute_type", f"{_DOC_FORM}.Attribute.Таблица.Column.Спр.Type") in spr
        assert ("form_main_table", f"{_DOC_FORM}.Attribute.Список.MainTable") in spr
        doc = _form_refs(world18["idx"], "Документ.Док")
        assert ("form_attribute_type", f"{_DOC_FORM}.Parameter.Основание.Type") in doc
        assert ("form_attribute_type", f"{_DOC_FORM}.Attribute.Объект.Type") in doc

    def test_cf_equals_edt(self, worlds18):
        for ref in ("Справочник.Спр", "Документ.Док", "РегистрНакопления.Рег", "Справочник.БезСинонима"):
            by_fmt = {fmt: _form_refs(w["idx"], ref) for fmt, w in worlds18.items()}
            assert by_fmt["cf"] == by_fmt["edt"], (ref, by_fmt)
            assert by_fmt["cf"], ref

    @pytest.mark.parametrize("route", ["full", "git"])
    def test_descriptor_pointwise_keeps_form_refs(self, world18, route, tmp_path_factory, monkeypatch):
        rel = _descriptor(world18, "Documents", "Док")
        files_b = _edit(
            world18["files"],
            rel,
            _type_token(world18, "CatalogRef.БезСинонима"),
            _type_token(world18, "CatalogRef.Спр"),
        )
        db = _assert_update_equals_build(
            tmp_path_factory,
            monkeypatch,
            world18["write"],
            world18["files"],
            files_b,
            _form_rows_query("Док"),
            route=route,
            expect_pointwise=("Documents", "Док"),
        )
        sql = "SELECT used_in FROM metadata_references WHERE source_object = ? AND ref_kind = ?"
        assert (f"{_DOC_FORM}.Attribute.Список.MainTable",) in _rows(db, sql, ("Док", "form_main_table"))

    @pytest.mark.parametrize("route", ["full", "git"])
    def test_bulk_category_keeps_form_refs(self, world18, route, tmp_path_factory, monkeypatch):
        rel = _descriptor(world18, "DataProcessors", "Обр")
        files_b = _edit(
            world18["files"], rel, _type_token(world18, "DocumentRef.Док"), _type_token(world18, "CatalogRef.Спр")
        )
        db = _assert_update_equals_build(
            tmp_path_factory,
            monkeypatch,
            world18["write"],
            world18["files"],
            files_b,
            _form_rows_query("Обр"),
            route=route,
            expect_pointwise=None,
        )
        sql = "SELECT ref_object, used_in FROM metadata_references WHERE source_object = ? AND ref_kind = ?"
        assert _rows(db, sql, ("Обр", "form_attribute_type")) == [
            ("Catalog.БезСинонима", "DataProcessor.Обр.Form.Форма.Attribute.Выбор.Type")
        ]

    @pytest.mark.parametrize("route", ["full", "git"])
    def test_form_change_refreshes_form_refs(self, world18, route, tmp_path_factory, monkeypatch):
        cf = world18["fmt"] == "cf"
        rel = f"Documents/Док/Forms/ФормаДокумента/{'Ext/Form.xml' if cf else 'Form.form'}"
        old = "<v8:Type>cfg:CatalogRef.Спр</v8:Type>" if cf else "<types>CatalogRef.Спр</types>"
        new = old.replace("CatalogRef.Спр", "CatalogRef.БезСинонима")
        files_b = _edit(world18["files"], rel, old, new)
        db = _assert_update_equals_build(
            tmp_path_factory,
            monkeypatch,
            world18["write"],
            world18["files"],
            files_b,
            _form_rows_query("Док"),
            route=route,
            check_route=False,
        )
        sql = "SELECT ref_object FROM metadata_references WHERE used_in = ?"
        assert _rows(db, sql, (f"{_DOC_FORM}.Attribute.Таблица.Column.Спр.Type",)) == [("Catalog.БезСинонима",)]

    def test_no_metadata_build(self, world18, tmp_path, monkeypatch):
        root = tmp_path / "cf"
        world18["write"](root, world18["files"])
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
        db = IndexBuilder().build(str(root), build_metadata=False)
        con = sqlite3.connect(db)
        try:
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "metadata_references" in tables:
                kinds = ", ".join(f"'{k}'" for k in _FORM_KINDS)
                n = con.execute(f"SELECT COUNT(*) FROM metadata_references WHERE ref_kind IN ({kinds})").fetchone()[0]
                assert n == 0
            if "form_elements" in tables:
                assert con.execute("SELECT COUNT(*) FROM form_elements").fetchone()[0] == 0
        finally:
            con.close()

    def test_kinds_unavailable_on_v17_includes_form_kinds(self, world18, tmp_path):
        reader = _v17_copy(world18, tmp_path)
        try:
            helpers = _world18_helpers(world18["root"], world18["ext_root"], reader)
            res = helpers["find_references_to_object"]("Справочник.Спр")
            assert set(_FORM_KINDS) <= set(res["_meta"]["kinds_unavailable"]), res["_meta"]
        finally:
            reader.close()

    @pytest.mark.parametrize("fmt", ["cf", "edt"])
    def test_one_row_per_ref_and_suffix(self, fmt):
        if fmt == "cf":
            text = "\n".join(
                [
                    f'<Form {_CF_FORM_NS} version="2.20">',
                    "<Attributes>",
                    '<Attribute name="Т" id="1">',
                    "<Type><v8:Type>cfg:CatalogRef.Спр</v8:Type><v8:Type>cfg:CatalogRef.Спр</v8:Type></Type>",
                    '<Columns><Column name="К" id="1">',
                    "<Type><v8:Type>cfg:CatalogRef.Спр</v8:Type></Type>",
                    "</Column></Columns>",
                    "</Attribute>",
                    "</Attributes>",
                    "</Form>",
                ]
            )
        else:
            text = "\n".join(
                [
                    '<form:Form xmlns:form="http://g5.1c.ru/v8/dt/form">',
                    "<attributes><name>Т</name>",
                    "<valueType><types>CatalogRef.Спр</types><types>CatalogRef.Спр</types></valueType>",
                    "<columns><name>К</name><valueType><types>CatalogRef.Спр</types></valueType></columns>",
                    "</attributes>",
                    "</form:Form>",
                ]
            )
        refs, _texts = _parse_refs(text)
        assert refs == [
            ("form_attribute_type", "Catalog.Спр", "Attribute.Т.Type"),
            ("form_attribute_type", "Catalog.Спр", "Attribute.Т.Column.К.Type"),
        ]

    @pytest.mark.parametrize("fmt", ["cf", "edt"])
    def test_settings_and_additional_columns(self, fmt):
        """Типы на любой глубине реквизита: настройки списка значений, параметры динамического списка,
        дополнительные колонки — суффикс реквизита или ближайшей колонки."""
        if fmt == "cf":
            text = "\n".join(
                [
                    f'<Form {_CF_FORM_NS} xmlns:dcssch="http://v8.1c.ru/8.1/data-composition-system/schema" '
                    'version="2.20">',
                    "<Attributes>",
                    '<Attribute name="Список" id="1">',
                    "<Type><v8:Type>v8:ValueListType</v8:Type></Type>",
                    '<Settings xsi:type="v8:TypeDescription"><v8:Type>cfg:DocumentRef.Д1</v8:Type></Settings>',
                    "</Attribute>",
                    '<Attribute name="Объект" id="2">',
                    "<Type><v8:Type>cfg:DocumentObject.Д2</v8:Type></Type>",
                    '<Columns><AdditionalColumns table="Объект.Товары"><Column name="Вид" id="1">',
                    "<Type><v8:Type>cfg:EnumRef.Виды</v8:Type></Type>",
                    "</Column></AdditionalColumns></Columns>",
                    "</Attribute>",
                    '<Attribute name="ДС" id="3">',
                    "<Type><v8:Type>cfg:DynamicList</v8:Type></Type>",
                    '<Settings xsi:type="DynamicList"><MainTable>InformationRegister.Рег.SliceLast</MainTable>',
                    "<Parameters><dcssch:parameter><dcssch:valueType><v8:Type>cfg:CatalogRef.Ф</v8:Type>"
                    "</dcssch:valueType></dcssch:parameter></Parameters>",
                    "</Settings>",
                    "</Attribute>",
                    "</Attributes>",
                    "</Form>",
                ]
            )
        else:
            text = "\n".join(
                [
                    '<form:Form xmlns:form="http://g5.1c.ru/v8/dt/form" '
                    'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">',
                    "<attributes><name>Список</name><valueType><types>ValueList</types></valueType>",
                    "<extInfo><itemValueType><types>DocumentRef.Д1</types></itemValueType></extInfo></attributes>",
                    "<attributes><name>Объект</name><valueType><types>DocumentObject.Д2</types></valueType>",
                    "<additionalColumns><columns><name>Вид</name><valueType><types>EnumRef.Виды</types></valueType>"
                    "</columns></additionalColumns></attributes>",
                    "<attributes><name>ДС</name><valueType><types>DynamicList</types></valueType>",
                    '<extInfo xsi:type="form:DynamicListExtInfo"><mainTable>InformationRegister.Рег.SliceLast'
                    "</mainTable>",
                    "<parameters><valueType><types>CatalogRef.Ф</types></valueType></parameters>",
                    "</extInfo></attributes>",
                    "</form:Form>",
                ]
            )
        refs, _texts = _parse_refs(text)
        assert sorted(refs) == sorted(
            [
                ("form_attribute_type", "Document.Д1", "Attribute.Список.Type"),
                ("form_attribute_type", "Document.Д2", "Attribute.Объект.Type"),
                ("form_attribute_type", "Enum.Виды", "Attribute.Объект.Column.Вид.Type"),
                ("form_attribute_type", "Catalog.Ф", "Attribute.ДС.Type"),
                ("form_main_table", "InformationRegister.Рег", "Attribute.ДС.MainTable"),
            ]
        )

    @pytest.mark.parametrize("helpers", ["idx"])
    def test_form_query_text_refs(self, world18, helpers):
        query_row = ("form_query", f"{_DOC_FORM}.Attribute.Список.QueryText")
        assert query_row in _form_refs(world18[helpers], "Справочник.Спр")
        assert query_row in _form_refs(world18[helpers], "РегистрНакопления.Рег")
        assert query_row not in _form_refs(world18[helpers], "Документ.Док")

    def test_query_text_refs_masks_comments_and_literals(self):
        assert BI._query_text_refs(_LIST_QUERY) == ["Catalog.Спр", "AccumulationRegister.Рег"]

    @pytest.mark.parametrize("fmt", ["cf", "edt"])
    def test_long_query_text_not_truncated(self, fmt):
        query = (
            "ВЫБРАТЬ\n\t"
            + ",\n\t".join(f"Т.Поле{i} КАК Поле{i}" for i in range(60))
            + "\nИЗ\n\tСправочник.Далёкий КАК Т"
        )
        assert query.index("Справочник.Далёкий") > 512
        if fmt == "cf":
            text = (
                f'<Form {_CF_FORM_NS} version="2.20"><Attributes><Attribute name="С" id="1">'
                '<Type><v8:Type>cfg:DynamicList</v8:Type></Type><Settings xsi:type="DynamicList">'
                f"<QueryText>{query}</QueryText></Settings></Attribute></Attributes></Form>"
            )
        else:
            text = (
                '<form:Form xmlns:form="http://g5.1c.ru/v8/dt/form" '
                'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"><attributes><name>С</name>'
                '<extInfo xsi:type="form:DynamicListExtInfo">'
                f"<queryText>{query}</queryText></extInfo></attributes></form:Form>"
            )
        _refs_, texts = _parse_refs(text)
        assert texts == [("Attribute.С.QueryText", query)]
        assert BI._query_text_refs(texts[0][1]) == ["Catalog.Далёкий"]

    def test_find_data_path_walks_form_kinds(self, world18):
        res = world18["idx"]["find_data_path"]("Документ.Док", "Справочник.Спр")
        assert res["found"] is True, res
        assert res["path"][0]["kind"] in _FORM_KINDS, res["path"]

    def test_truncated_page_orders_new_kinds(self, world18):
        res = world18["idx"]["find_references_to_object"](
            "Документ.Док", kinds=["filter_criterion_type", "form_attribute_type"], limit=1
        )
        assert res["truncated"] is True
        assert [r["kind"] for r in res["references"]] == ["filter_criterion_type"]

    def test_live_route_names_form_kinds_unsupported(self, world18):
        res = world18["live"]["find_references_to_object"]("Справочник.Спр")
        assert set(_FORM_KINDS) <= set(res["_meta"]["unsupported_kinds"])
        assert not _form_refs(world18["live"], "Справочник.Спр")

    def test_common_form_used_in(self, tmp_path, monkeypatch):
        for fmt in ("cf", "edt"):
            root = tmp_path / fmt / "cf"
            files = {
                "Configuration.xml" if fmt == "cf" else "Configuration/Configuration.mdo": CF_DESCRIPTOR
                if fmt == "cf"
                else EDT_DESCRIPTOR,
                f"CommonForms/ОбщФорма/{'Ext/Form.xml' if fmt == 'cf' else 'Form.form'}": _small_form(
                    fmt == "cf", "Реквизит", "CatalogRef.Спр"
                ),
                # дерево без единого модуля даёт пустой индекс без метаданных (давнее правило сборки)
                f"CommonForms/ОбщФорма/{'Ext/Form/' if fmt == 'cf' else ''}Module.bsl": _PROC,
            }
            _write_tree(root, files)
            reader = _reader_for(root, monkeypatch)
            try:
                rows = [
                    (r["source_object"], r["source_category"], r["used_in"])
                    for r in reader.find_metadata_references("Catalog.Спр")
                ]
            finally:
                reader.close()
            assert rows == [("ОбщФорма", "CommonForms", "CommonForm.ОбщФорма.Attribute.Реквизит.Type")], fmt

    @staticmethod
    def _settings_storage_tree(root: Path, cf: bool) -> None:
        """Хранилище настроек `Хран` с формой, чей реквизит ссылается на справочник `Спр` (он тоже в дереве)."""
        form_dir = "SettingsStorages/Хран/Forms/Форма"
        descriptor, descriptor_text = (
            ("Configuration.xml", CF_DESCRIPTOR) if cf else ("Configuration/Configuration.mdo", EDT_DESCRIPTOR)
        )
        _write_tree(
            root,
            {
                descriptor: descriptor_text,
                f"{form_dir}/{'Ext/Form.xml' if cf else 'Form.form'}": _small_form(cf, "Реквизит", "CatalogRef.Спр"),
                # дерево без единого модуля даёт пустой индекс без метаданных (давнее правило сборки)
                f"{form_dir}/{'Ext/Form/' if cf else ''}Module.bsl": _PROC,
                ("Catalogs/Спр.xml" if cf else "Catalogs/Спр/Спр.mdo"): (
                    _cf_obj("Catalog", "Спр") if cf else _edt_obj("Catalog", "Спр")
                ),
                f"Catalogs/Спр/{'Ext/' if cf else ''}ObjectModule.bsl": _PROC,
            },
        )

    def test_settings_storage_form_used_in_is_canonical(self, tmp_path, monkeypatch):
        """Ревью v1.47.0 (M6): голова `used_in` у формы хранилища настроек — тип (`SettingsStorage`), как у прочих
        владельцев форм, а не имя папки (`SettingsStorages`): категории не было в `_CATEGORY_TO_TYPE_PREFIX`."""
        for fmt in ("cf", "edt"):
            root = tmp_path / fmt / "cf"
            self._settings_storage_tree(root, fmt == "cf")
            reader = _reader_for(root, monkeypatch)
            try:
                rows = [
                    (r["source_object"], r["source_category"], r["used_in"])
                    for r in reader.find_metadata_references("Catalog.Спр")
                ]
            finally:
                reader.close()
            expected = [("Хран", "SettingsStorages", "SettingsStorage.Хран.Form.Форма.Attribute.Реквизит.Type")]
            assert rows == expected, fmt

    def test_data_path_endpoint_settings_storage(self, tmp_path, monkeypatch):
        """Ревью v1.47.0 (M6): тот же тип в общей карте — `find_data_path` принимает конец «хранилище настроек»
        (прежде — ложный отказ «у концов нет распознанного префикса типа») и проходит по ссылке его формы."""
        for fmt in ("cf", "edt"):
            root = tmp_path / fmt / "cf"
            self._settings_storage_tree(root, fmt == "cf")
            reader = _reader_for(root, monkeypatch)
            try:
                bsl = _bsl_for(root, reader)
                there = bsl["find_data_path"]("ХранилищеНастроек.Хран", "Справочник.Спр")
                back = bsl["find_data_path"]("Справочник.Спр", "SettingsStorage.Хран")
            finally:
                reader.close()
            assert there["found"] is True, (fmt, there)
            assert [(e["kind"], e["used_in"]) for e in there["path"]] == [
                ("form_attribute_type", "SettingsStorage.Хран.Form.Форма.Attribute.Реквизит.Type")
            ], fmt
            # v1.47.0 (Задача 17): прежде здесь стояло `back.get("error") is None` — тихое «не найдено» после обхода;
            # ссылок на хранилища настроек граф не хранит, и конец-цель такого вида — структурный ответ.
            assert back["found"] is False and "в графе ссылок" in back["error"], (fmt, back)


# ═══════════════ Задача 11 — обращения в коде: `metadata`, `form`, член менеджера (Д9–Д11) ═══════════════

_CODE_USAGES_SQL = (
    "SELECT m.rel_path, u.object_ref, u.object_ref_key, u.member_path, u.usage_kind, u.line "
    "FROM metadata_code_usages u JOIN modules m ON m.id = u.module_id"
)


def _x(*lines: str) -> list[tuple]:
    return BI._extract_code_usages(list(lines))


class TestCodeUsagesV18:
    def test_metadata_collection_reference(self):
        rows = _x("Имя = Метаданные.Справочники.Спр.ПолноеИмя();")
        assert ("Catalog.Спр", "ПолноеИмя", "metadata", 1) in rows
        assert not [r for r in rows if r[2] == "manager"], rows

    def test_metadata_english_without_member(self):
        assert _x("М = Metadata.Catalogs.Спр;") == [("Catalog.Спр", None, "metadata", 1)]

    def test_metadata_with_spaces(self):
        assert ("Catalog.Спр", "ПолноеИмя", "metadata", 1) in _x("Имя = Метаданные . Справочники . Спр . ПолноеИмя();")

    def test_metadata_in_comment_or_string_ignored(self):
        assert _x("// Метаданные.Справочники.Спр.ПолноеИмя()") == []
        rows = _x('Сообщить("Метаданные.Справочники.Спр");')
        assert not [r for r in rows if r[2] in ("metadata", "manager")], rows

    def test_metadata_collection_without_manager_ignored(self):
        assert _x("Т = Метаданные.ОпределяемыеТипы.Сумма.Тип;") == []

    # Ревью v1.47.0 (M1): метод коллекции объектов метаданных (`Найти`, `Содержит`, `Количество`, `Получить`,
    # `Индекс` и английские) — не объект: прежде `Метаданные.Справочники.Найти(Имя)` давал строку `Catalog.Найти`.
    @pytest.mark.parametrize(
        "line",
        [
            "Если Метаданные.Справочники.Содержит(МД) Тогда",
            "ОМ = Метаданные.Документы.Найти(Имя);",
            "Н = Метаданные.Справочники.Количество();",
            "Д = Метаданные.Документы.Получить(0);",
            "И = Метаданные.Справочники.Индекс(МД);",
            "Н = Metadata.Catalogs.Find (Name);",
            "Ф = Metadata.Catalogs.Contains(MD) Or Metadata.Catalogs.IndexOf(MD) > Metadata.Catalogs.Count();",
            "Г = Metadata.Catalogs.Get(0);",
            "Если Метаданные.Справочники.НАЙТИ(Имя) <> Неопределено Тогда",
            "П = Метаданные.Справочники.Найти(Имя).ПолноеИмя();",
        ],
    )
    def test_metadata_collection_method_is_not_object(self, line):
        assert _x(line) == [], line

    def test_metadata_object_named_like_collection_method_kept(self):
        """Без скобки это свойство коллекции — объект с таким именем."""
        assert _x("П = Метаданные.Справочники.Количество.ПолноеИмя();") == [
            ("Catalog.Количество", "ПолноеИмя", "metadata", 1)
        ]

    # Ревью v1.47.0 (M8): регистр слова «Метаданные» — любой, как у имени коллекции.
    @pytest.mark.parametrize(
        ("line", "member"),
        [
            ("Х = MetaData.Catalogs.Спр.FullName();", "FullName"),
            ("Х = МетаДанные.Справочники.Спр.ПолноеИмя();", "ПолноеИмя"),
            ("Х = METADATA.catalogs.Спр.FullName();", "FullName"),
        ],
    )
    def test_metadata_head_any_case(self, line, member):
        assert _x(line) == [("Catalog.Спр", member, "metadata", 1)]

    def test_metadata_chain_member_through_space_is_not_head(self):
        """Как у `manager`: перед головой точка через пробел — член цепочки, а не глобальный контекст."""
        assert _x("Х = Параметры. Метаданные.Справочники.Спр;") == []

    def test_manager_member_is_method(self):
        assert ("Document.Док", "СоздатьДокумент", "manager", 1) in _x("Д = Документы.Док.СоздатьДокумент();")

    def test_manager_member_property(self):
        assert ("Enum.Е", "Значение1", "manager", 1) in _x("З = Перечисления.Е.Значение1;")

    def test_manager_without_member(self):
        assert ("Document.Док", None, "manager", 1) in _x("Т = Тип(Документы.Док);")

    def test_manager_member_with_spaces(self):
        assert ("Document.Док", "Создать", "manager", 1) in _x("Д = Документы . Док . Создать();")

    # Решение владельца после ревью v1.47.0: `Коллекция.X(` — вызов, а не объект (ни менеджер объекта, ни объект
    # метаданных не вызываются). Прежде метод менеджера коллекции и метод переменной с именем коллекции давали
    # строки `ExchangePlan.ГлавныйУзел`, `Catalog.ТипВсеСсылки`, `Document.Добавить`.
    @pytest.mark.parametrize(
        "line",
        [
            "Узел = ПланыОбмена.ГлавныйУзел();",
            "ПланыОбмена.ЗарегистрироватьИзменения(Узел, Данные);",
            "Узел = ExchangePlans.MasterNode ();",
            "Т = Справочники.ТипВсеСсылки();",
            "Т = Catalogs.AllRefsType();",
            "Т = БизнесПроцессы.ТипВсеСсылкиТочекМаршрутаБизнесПроцессов();",
            'Н = Константы.СоздатьНабор("Конст");',
            "Документы.Добавить(Док);",
        ],
    )
    def test_manager_collection_call_is_not_object(self, line):
        assert not [r for r in _x(line) if r[2] == "manager"], _x(line)

    def test_manager_object_with_member_call_kept(self):
        """Скобка после члена, а не после имени объекта, — объект (`Коллекция.X.Метод()`)."""
        assert _x("Узел = ПланыОбмена.Полный.ЭтотУзел();") == [("ExchangePlan.Полный", "ЭтотУзел", "manager", 1)]

    def test_metadata_call_is_not_object(self):
        """То же правило у `metadata`: скобка сразу за X — вызов, а не объект метаданных."""
        assert _x("Х = Метаданные.Документы.ПолучитьПоИмени(Имя);") == []

    def test_form_path_four_segments(self):
        assert _x('ОткрытьФорму("Документ.Док.Форма.ФормаДокумента");') == [
            ("Document.Док", "ФормаДокумента", "form", 1)
        ]

    def test_default_form_three_segments(self):
        assert _x('Ф = ПолучитьФорму("Обработка.Обр.Форма");') == [("DataProcessor.Обр", "Форма", "form", 1)]

    def test_standard_form(self):
        assert _x('ОткрытьФорму("Справочник.Спр.ФормаВыбора");') == [("Catalog.Спр", "ФормаВыбора", "form", 1)]

    def test_standard_form_english_any_case(self):
        assert _x('OpenForm("Catalog.Спр.choiceform");') == [("Catalog.Спр", "choiceform", "form", 1)]

    def test_tabular_section_named_like_form_stays_query(self):
        assert _x('Т = "ВЫБРАТЬ * ИЗ Документ.Док.ФормаОплаты";') == [("Document.Док", "ФормаОплаты", "query", 1)]

    def test_query_unchanged(self):
        """Обе версии."""
        assert _x('Т = "ВЫБРАТЬ * ИЗ Документ.Док.Товары";') == [("Document.Док", "Товары", "query", 1)]

    def test_four_segment_query_path_unchanged(self):
        """Обе версии: четвёртый сегмент не читается."""
        assert _x('П = "Документ.Док.Товары.Номенклатура";') == [("Document.Док", "Товары", "query", 1)]

    def test_chain_member_named_like_collection_is_not_manager(self):
        for line in ("Н = Форма.Объект.Отчеты.Количество();", "Н = Форма.Объект. Отчеты.Количество();"):
            rows = _x(line)
            assert not [r for r in rows if r[2] == "manager"], (line, rows)

    def test_kind_filter_new_values(self, world18):
        find = world18["idx"]["find_code_usages"]
        res = find("Документ.Док", kind="form")
        assert [(u["kind"], u["member"]) for u in res["usages"]] == [("form", "ФормаДокумента")]
        res = find("Документ.Док", kind="metadata")
        assert [(u["kind"], u["member"]) for u in res["usages"]] == [("metadata", "ПолноеИмя")]
        res = find("Документ.Док", kind="manager")
        assert "СоздатьДокумент" in {u["member"] for u in res["usages"]}
        res = find("Документ.Док", kind="query")
        assert ("query", "Товары") in {(u["kind"], u["member"]) for u in res["usages"]}
        assert "Форма" not in {u["member"] for u in res["usages"]}
        assert find("Справочник.Спр", kind="form")["usages"][0]["member"] == "ФормаВыбора"
        assert res["_meta"]["kinds_unavailable"] == []

    def test_kinds_unavailable_on_v17(self, world18, tmp_path):
        reader = _v17_copy(world18, tmp_path)
        try:
            helpers = _world18_helpers(world18["root"], world18["ext_root"], reader)
            res = helpers["find_code_usages"]("Документ.Док")
            assert res["_meta"]["source"] == "index"
            assert res["_meta"]["kinds_unavailable"] == ["form", "metadata"]
            assert "hint" not in res
            res = helpers["find_code_usages"]("Документ.Док", kind="form")
            assert res["_meta"]["kinds_unavailable"] == ["form"]
            assert res["partial"] is True
            assert "index update" in res["hint"]
            res = helpers["find_code_usages"]("Документ.Док", kind="manager")
            assert res["_meta"]["kinds_unavailable"] == []
            assert "hint" not in res
        finally:
            reader.close()
        res = world18["live"]["find_code_usages"]("Документ.Док")
        assert res["_meta"]["kinds_unavailable"] == []

    @pytest.mark.parametrize("route", ["full", "git"])
    def test_update_equals_build(self, world18, route, tmp_path_factory, monkeypatch):
        rel = f"Documents/Док/{'Ext/' if world18['fmt'] == 'cf' else ''}ManagerModule.bsl"
        files_b = _edit(world18["files"], rel, "Метаданные.Документы.Док.ПолноеИмя()", "Метаданные.Справочники.Спр.Имя")
        db = _assert_update_equals_build(
            tmp_path_factory,
            monkeypatch,
            world18["write"],
            world18["files"],
            files_b,
            {"metadata_code_usages": _CODE_USAGES_SQL},
            route=route,
            check_route=False,
        )
        sql = _CODE_USAGES_SQL + " WHERE u.usage_kind = 'metadata'"
        assert [r[1:5] for r in _rows(db, sql)] == [("Catalog.Спр", "catalog.спр", "Имя", "metadata")]


# ═══════════════ Задача 12 — вызов с переносом или пробелом после точки; перенос перед скобкой (Д12) ═══════════════


def _calls(*body: str) -> list[tuple]:
    """Рёбра тела процедуры (строки ``body`` — со второй строки файла; строка ребра — номер в файле)."""
    lines = ["Процедура П()", *body, "КонецПроцедуры"]
    return BI._extract_calls_from_body(lines, 1, len(lines))


_CALLS_SQL = (
    "SELECT m.rel_path, me.name, c.callee_name, c.line, c.callee_key, c.callee_via, c.call_kind "
    "FROM calls c JOIN methods me ON me.id = c.caller_id JOIN modules m ON m.id = me.module_id"
)


class TestQualifiedCallAcrossLineBreak:
    def test_dot_at_end_of_line(self):
        calls = _calls("\tОбщМод.", "\t\tЭкспорт1(1);")
        assert ("ОбщМод.Экспорт1", 3, None, None) in calls
        assert not [c for c in calls if c[0] == "Экспорт1"], calls

    def test_dot_then_space(self):
        calls = _calls("\tОбщМод. Экспорт1(2);")
        assert calls == [("ОбщМод.Экспорт1", 2, None, None)]

    def test_dot_at_start_of_next_line(self):
        calls = _calls("\tОбщМод", "\t\t.Экспорт1(3);")
        assert calls == [("ОбщМод.Экспорт1", 3, None, None)]

    def test_comment_and_blank_lines_between(self):
        calls = _calls("\tОбщМод.", "\t// комментарий", "", "\tЭкспорт1(1);")
        assert calls == [("ОбщМод.Экспорт1", 5, None, None)]

    def test_preprocessor_line_between(self):
        """Директива маской не гасится: точки перед именем нет — прежний голый вызов."""
        calls = _calls("\tОбщМод.", "#Если Сервер Тогда", "\tЭкспорт1(1);", "#КонецЕсли")
        assert calls == [("Экспорт1", 4, None, None)]

    def test_chain_member_across_line_break(self):
        assert _calls("\tОбъект.Товары.", "\t\tДобавить(1);") == [("Товары.Добавить", 3, None, "member")]

    def test_manager_collection_across_line_break(self):
        assert _calls("\tСправочники.Спр.", "\t\tНайтиПоКоду(1);") == [("Спр.НайтиПоКоду", 3, "Catalogs", None)]

    def test_dot_inside_string_is_not_a_call_head(self):
        """Обе версии: точка в литерале погашена маской — голый вызов."""
        calls = _calls('\tСообщить("ОбщМод.");', "\tЭкспорт1(1);")
        assert calls == [("Экспорт1", 3, None, None)]

    def test_same_line_calls_unchanged(self):
        """Обе версии: модуль без переносов — прежние рёбра."""
        calls = _calls(
            "\tОбщМод.Экспорт1(1);",
            "\tА = Справочники.Спр.НайтиПоКоду(1);",
            "\tОбъект.Товары.Добавить();",
            "\tСвояПроцедура(А, Б);",
            "\tСообщить(НСтр(\"ru = 'x'\"));",
            "\tЕсли Истина Тогда Возврат; КонецЕсли;",
            "\tБ = Метод1(Метод2(1)).Член(2);",
        )
        assert calls == [
            ("ОбщМод.Экспорт1", 2, None, None),
            ("Спр.НайтиПоКоду", 3, "Catalogs", None),
            ("Товары.Добавить", 4, None, "member"),
            ("СвояПроцедура", 5, None, None),
            ("Метод1", 8, None, None),
            ("Метод2", 8, None, None),
        ]

    def test_method_of_expression_across_line_break_gives_no_bare_edge(self):
        """`Получить().` ⏎ `Член(` — получатель не идентификатор: ребра нет (прежде — ложное голое)."""
        assert _calls("\tА = Получить().", "\t\tЧлен(1);") == [("Получить", 2, None, None)]

    # ── Задача 18 (сверка индексов v18): написания того же класса, которые правка уже обрабатывает ──

    def test_space_before_dot_same_line(self):
        """`Голова .Метод(` — квалифицированное ребро, как без пробела (прежде ребра не было вовсе)."""
        assert _calls("\tОбщМод .Экспорт1(1);") == [("ОбщМод.Экспорт1", 2, None, None)]

    def test_tab_before_dot_same_line(self):
        assert _calls("\tОбщМод\t.Экспорт1(1);") == [("ОбщМод.Экспорт1", 2, None, None)]

    def test_space_before_dot_chain_member(self):
        assert _calls("\tОбъект.Товары .Добавить(1);") == [("Товары.Добавить", 2, None, "member")]

    def test_space_before_dot_manager(self):
        assert _calls("\tА = Справочники.Спр .НайтиПоКоду(1);") == [("Спр.НайтиПоКоду", 2, "Catalogs", None)]

    def test_indexer_expression_across_line_break_gives_no_bare_edge(self):
        """`…[Имя].` ⏎ `Метод(` — получатель не идентификатор: ребра нет, как после `).` (прежде — ложное голое)."""
        assert _calls("\tФорма.Элементы[Имя].", "\t\tУстановитьДействие(1);") == []
        assert _calls("\tСсылка = Справочники[Имя].", "\t\tНайтиПоКоду(1);") == []

    # ── П12: перенос перед скобкой ──

    def test_paren_on_next_line_qualified(self):
        assert _calls("\tОбщМод.Экспорт1", "\t\t(1);") == [("ОбщМод.Экспорт1", 2, None, None)]

    def test_paren_on_next_line_bare(self):
        assert _calls("\tСвояПроцедура", "\t\t(1);") == [("СвояПроцедура", 2, None, None)]

    def test_paren_on_next_line_manager(self):
        assert _calls("\tСправочники.Спр.НайтиПоКоду", "\t\t(1);") == [("Спр.НайтиПоКоду", 2, "Catalogs", None)]

    def test_paren_on_next_line_dedup(self):
        assert _calls("\tА = ОбщМод.Экспорт1(1) + ОбщМод.Экспорт1", "\t\t(2);") == [("ОбщМод.Экспорт1", 2, None, None)]

    def test_paren_on_next_line_after_comment(self):
        assert _calls("\tСвояПроцедура // вызов", "\t// ещё", "\t\t(1);") == [("СвояПроцедура", 2, None, None)]

    def test_keyword_before_paren_is_not_a_call(self):
        """Обе версии."""
        assert _calls("\tЕсли А И", "\t\t(Б) Тогда", "\tКонецЕсли;") == []
        assert _calls("\tВозврат", "\t\t(А);") == []

    def test_platform_function_before_paren_dropped(self):
        """Обе версии."""
        assert _calls("\tА = НСтр", "\t\t(\"ru='x'\");") == []

    # ── индекс world18 ──

    def test_index_edges_are_qualified_with_exact_key(self, world18):
        rel = f"Documents/Док/{'Ext/' if world18['fmt'] == 'cf' else ''}ObjectModule.bsl"
        sql = _CALLS_SQL + " WHERE m.rel_path = ? AND me.name = 'ОбработкаПроведения'"
        rows = [r for r in _rows(world18["db"], sql, (rel,)) if "Экспорт1" in r[2]]
        assert [r[2:4] for r in rows] == [
            ("ОбщМод.Экспорт1", 3),
            ("ОбщМод.Экспорт1", 4),
            ("ОбщМод.Экспорт1", 6),
            ("ОбщМод.Экспорт1", 7),
        ], rows
        keys = {r[4] for r in rows}
        assert len(keys) == 1 and next(iter(keys)) and "ОбщМод".casefold() in next(iter(keys)).casefold(), keys

    def test_local_namesake_no_false_exact(self, world18):
        rel = f"Documents/Док/{'Ext/' if world18['fmt'] == 'cf' else ''}ObjectModule.bsl"
        res = world18["idx"]["find_callers_context"]("Экспорт1", rel)
        exact = [c for c in res["callers"] if c.get("edge_exact")]
        assert not [c for c in exact if c.get("caller_name") == "ОбработкаПроведения"], res

    def test_extension_layer_sees_qualified(self, world18):
        if world18["fmt"] != "cf":
            pytest.skip("расширение world18 — только в CF")
        res = world18["idx"]["find_callers_context"]("Экспорт1", "CommonModules/ОбщМод/Ext/Module.bsl")
        ext = [c for c in res["callers"] if str(c.get("file", "")).startswith("../cfe/")]
        rows = [(c["caller_name"], c["call_kind"], c["edge_exact"]) for c in ext]
        assert ("Расш1_ПриОткрытииПосле", "call", True) in rows, res

    def test_extension_layer_space_before_dot(self, world18):
        """Задача 18: `ОбщМод .Экспорт1(` в модуле расширения — то же точное ребро живого слоя."""
        if world18["fmt"] != "cf":
            pytest.skip("расширение world18 — только в CF")
        res = world18["idx"]["find_callers_context"]("Экспорт1", "CommonModules/ОбщМод/Ext/Module.bsl")
        rows = [
            (c["caller_name"], c["call_kind"], c["edge_exact"])
            for c in res["callers"]
            if str(c.get("file", "")).startswith("../cfe/")
        ]
        assert ("Расш1_ПробелПередТочкой", "call", True) in rows, res

    @pytest.mark.parametrize("route", ["full", "git"])
    def test_update_equals_build(self, world18, route, tmp_path_factory, monkeypatch):
        rel = f"Documents/Док/{'Ext/' if world18['fmt'] == 'cf' else ''}ObjectModule.bsl"
        files_b = _edit(world18["files"], rel, "\tОбщМод. Экспорт1(2);", "\tОбщМод.\n\t\tЭкспорт1(20);")
        _assert_update_equals_build(
            tmp_path_factory,
            monkeypatch,
            world18["write"],
            world18["files"],
            files_b,
            {"calls": _CALLS_SQL},
            route=route,
            check_route=False,
        )


# ═══════════════ Задача 13 — таблица движений БГУ и делегат модуля объекта (Д13) ═══════════════

_MOVEMENTS_SQL = "SELECT document_name, register_name, source, file, evidence, kind FROM register_movements"


def _doc_module(world: World, module: str) -> str:
    return f"Documents/Док/{'Ext/' if world['fmt'] == 'cf' else ''}{module}.bsl"


def _movements_of(content: str, module: str) -> list[tuple]:
    from rlm_tools_bsl.format_detector import parse_bsl_path

    base = Path("C:/cfg") if os.name == "nt" else Path("/cfg")
    rel = f"Documents/Док/Ext/{module}.bsl"
    info = parse_bsl_path(str(base / rel), str(base))
    return BI._extract_movements(content, info, rel)


class TestBguMovementTables:
    def test_table_name_is_a_movement(self, world18):
        res = world18["idx"]["find_register_movements"]("Документ.Док")
        rel = _doc_module(world18, "ManagerModule")
        rows = {(r["name"], r["source"], r["file"]) for r in res["code_registers"]}
        assert ("Рег", "movement_table", rel) in rows, res["code_registers"]
        assert ("Рег2", "movement_table", rel) in rows, res["code_registers"]

    def test_non_register_table_ignored(self, world18):
        res = world18["idx"]["find_register_movements"]("Документ.Док")
        assert "ТаблицаНеРегистр" not in {r["name"] for r in res["code_registers"]}
        assert "ТаблицаНеРегистр" not in json.dumps(res.get("unresolved") or [], ensure_ascii=False)
        sql = _MOVEMENTS_SQL + " WHERE evidence LIKE '%ТаблицаНеРегистр%'"
        assert _rows(world18["db"], sql) == []

    def test_undeclared_goes_to_undeclared(self, world18, tmp_path, monkeypatch):
        rel = _doc_module(world18, "ManagerModule")
        files = _edit(
            world18["files"],
            rel,
            '\tТаблицыЗапроса.Добавить("Рег2");',
            '\tТаблицыЗапроса.Добавить("Рег2");\n\tТаблицыЗапроса.Добавить("РБ");',
        )
        root = tmp_path / "cf"
        world18["write"](root, files)
        reader = _reader_for(root, monkeypatch)
        try:
            res = _world18_helpers(root, None, reader)["find_register_movements"]("Документ.Док")
        finally:
            reader.close()
        assert ("РБ", "movement_table") in {(r["name"], r["source"]) for r in res["code_registers"]}
        assert "РБ" in res["undeclared_code_registers"]
        assert "Рег2" not in res["undeclared_code_registers"]

    def test_comment_and_string_not_counted(self):
        content = "\n".join(
            [
                "Процедура ИнициализироватьДанныеДокумента(ДополнительныеСвойства) Экспорт",
                '\t// ТаблицыДляДвижений.Вставить("Рег", Т);',
                '\tС = "ТаблицыЗапроса.Добавить(""Рег2"")";',
                '\tТаблицыЗапроса.Добавить("Рег3"); // настоящий',
                "КонецПроцедуры",
            ]
        )
        rows = [r[:2] for r in _movements_of(content, "ManagerModule") if r[1] == "movement_table"]
        assert rows == [("Рег3", "movement_table")]

    def test_find_register_writers_sees_it(self, world18):
        res = world18["idx"]["find_register_writers"]("РегистрНакопления.Рег")
        pairs = {(w["document"], w["source"], w["file"]) for w in res["writers"]}
        assert ("Док", "movement_table", _doc_module(world18, "ManagerModule")) in pairs, res
        assert ("Док", "code", _doc_module(world18, "ObjectModule")) in pairs, res

    def test_profile_registers_summary_counts_it(self, world18):
        reg = world18["idx"]["get_object_profile"]("Документ.Док")["sections"]["registers"]
        items = {(i["register"], i["source"]) for i in reg["items"]}
        assert ("Рег", "movement_table") in items and ("Рег2", "movement_table") in items, reg
        code_like = [i for i in reg["items"] if i["source"] in ("code", "manager_code", "movement_table")]
        assert reg["summary"]["code_registers"] == len(code_like), reg

    @pytest.mark.parametrize("route", ["full", "git"])
    def test_manager_change_equals_build(self, world18, route, tmp_path_factory, monkeypatch):
        files_b = _edit(
            world18["files"],
            _doc_module(world18, "ManagerModule"),
            '\tТаблицыЗапроса.Добавить("Рег2");',
            '\tТаблицыЗапроса.Добавить("Рег");',
        )
        db = _assert_update_equals_build(
            tmp_path_factory,
            monkeypatch,
            world18["write"],
            world18["files"],
            files_b,
            {"register_movements": _MOVEMENTS_SQL},
            route=route,
            check_route=False,
        )
        sql = "SELECT register_name FROM register_movements WHERE source = 'movement_table'"
        assert _rows(db, sql) == [("Рег",), ("Рег",)]

    @pytest.mark.parametrize("route", ["full", "git"])
    def test_descriptor_change_equals_build(self, world18, route, tmp_path_factory, monkeypatch):
        """Правка состава движений без правки модуля: строки по литералу от объявления не зависят."""
        rel = _descriptor(world18, "Documents", "Док")
        old = (
            '<xr:Item xsi:type="xr:MDObjectRef">AccumulationRegister.Рег2</xr:Item>'
            if world18["fmt"] == "cf"
            else "<registerRecords>AccumulationRegister.Рег2</registerRecords>"
        )
        files_b = _edit(world18["files"], rel, old, "")
        db = _assert_update_equals_build(
            tmp_path_factory,
            monkeypatch,
            world18["write"],
            world18["files"],
            files_b,
            {
                "register_movements": _MOVEMENTS_SQL,
                "declared": "SELECT document_ref, register_ref, file FROM declared_register_records",
            },
            route=route,
            check_route=False,
        )
        sql = "SELECT register_name FROM register_movements WHERE source = 'movement_table'"
        assert _rows(db, sql) == [("Рег",), ("Рег2",)]

    def test_object_module_delegate_row_in_index(self, world18):
        sql = "SELECT register_name, source, file FROM register_movements WHERE document_name = ? AND file = ?"
        rel = _doc_module(world18, "ObjectModule")
        assert _rows(world18["db"], sql, ("Док", rel)) == [("Рег", "code", rel)]

    def test_delegate_rows_shape(self):
        content = "\n".join(
            [
                "Процедура ОбработкаПроведения(Отказ, Режим)",
                '\tПроведениеСервер.ОтразитьДвиженияПоРегистру(ДополнительныеСвойства, Движения, Отказ, "Рег");',
                "\tПроведениеСервер.ОтразитьДвиженияПоРегистру(ДополнительныеСвойства, Движения, Отказ, ИмяТаблицы);",
                "КонецПроцедуры",
            ]
        )
        rows = _movements_of(content, "ObjectModule")
        assert [(r[0], r[1], r[4]) for r in rows if r[1] == "unresolved"] == [("", "unresolved", "dynamic_name")]
        assert [r[3] for r in rows if r[1] == "unresolved"] == ["ИмяТаблицы"]
        inserted, _dropped = BI._movement_rows_for_insert(
            [types.SimpleNamespace(movements=rows, info=types.SimpleNamespace(object_name="Док"))],
            frozenset({"рег"}),
        )
        assert [(r[1], r[2], r[5]) for r in inserted] == [("Рег", "code", None), ("", "unresolved", "dynamic_name")]
        unknown, _dropped = BI._movement_rows_for_insert(
            [types.SimpleNamespace(movements=rows, info=types.SimpleNamespace(object_name="Док"))], frozenset()
        )
        assert [(r[1], r[2], r[4], r[5]) for r in unknown] == [
            ("", "unresolved", "Рег", "unknown_register"),
            ("", "unresolved", "ИмяТаблицы", "dynamic_name"),
        ]

    def test_delegate_not_duplicated_on_read(self, world18, tmp_path):
        rel = _doc_module(world18, "ObjectModule")
        res = world18["idx"]["find_register_movements"]("Документ.Док")
        assert [(r["name"], r["source"]) for r in res["code_registers"] if r["file"] == rel] == [("Рег", "code")]
        # Ревью v1.47.0 (M3): настоящий индекс v17 строки делегата не содержит — её пишет только сборщик v18 (прямых
        # `Движения.Рег` в модуле нет). Копия без неё: строку возвращает раскрытие на чтении (v1.43.0), ответ прежний.
        # Прежде копия хранила строку сборщика, и раскрытие дедупилось об неё же — сравнение было тривиальным.
        copy = _db_copy(world18["db"], tmp_path / "bsl_index_v17.db")
        _downgrade_to_v17(copy)
        con = sqlite3.connect(str(copy))
        try:
            deleted = con.execute("DELETE FROM register_movements WHERE source = 'code' AND file = ?", (rel,)).rowcount
            con.commit()
        finally:
            con.close()
        assert deleted == 1
        reader = IndexReader(copy)
        try:
            old = _world18_helpers(world18["root"], world18["ext_root"], reader)["find_register_movements"](
                "Документ.Док"
            )
        finally:
            reader.close()
        assert [(r["name"], r["source"]) for r in old["code_registers"] if r["file"] == rel] == [("Рег", "code")]
        # Состав тот же; порядок — нет: строку раскрытия `_merge_movement_rows` дописывает после строк индекса.
        assert sorted((r["name"], r["source"], r["file"]) for r in old["code_registers"]) == sorted(
            (r["name"], r["source"], r["file"]) for r in res["code_registers"]
        )

    # Ревью v1.47.0 (Codex I-1): прямое `Движения.Рег` и делегат с "Рег" в одном модуле объекта (так у документа БГУ
    # `ВводНачальныхОстатков`), а также два вызова делегата с одним регистром, но разным текстом строки, — по одной
    # строке `code`: улики разных строк разводили их в `SELECT DISTINCT` ридера, и `code_registers` (без улики)
    # отдавал одинаковые строки. Прямое обращение — после делегата: порядок в модуле не важен.
    _DELEGATE_OVERLAP = (
        '\tПроведениеСервер.ОтразитьДвиженияПоРегистру(ДополнительныеСвойства, Движения, Отказ, "Рег");\n'
        "\tДвижения.Рег.Записывать = Истина;\n"
        '\tПроведениеСервер.ОтразитьДвиженияПоРегистру(ДополнительныеСвойства, Движения, Отказ, "Рег2");\n'
        '\tПроведениеСервер.ОтразитьДвиженияПоРегистру(ДополнительныеСвойства, Движения, ОтказПроведения, "РЕГ2");'
    )

    def _overlap_files(self, world: World) -> dict[str, str]:
        return _edit(
            world["files"],
            _doc_module(world, "ObjectModule"),
            '\tПроведениеСервер.ОтразитьДвиженияПоРегистру(ДополнительныеСвойства, Движения, Отказ, "Рег");',
            self._DELEGATE_OVERLAP,
        )

    def test_delegate_row_dedup_against_direct_rows(self):
        rows = _movements_of(
            "Процедура ОбработкаПроведения(Отказ, Режим)\n" + self._DELEGATE_OVERLAP + "\nКонецПроцедуры",
            "ObjectModule",
        )
        inserted, _dropped = BI._movement_rows_for_insert(
            [types.SimpleNamespace(movements=rows, info=types.SimpleNamespace(object_name="Док"))],
            frozenset({"рег", "рег2"}),
        )
        assert [(r[1], r[2], r[4]) for r in inserted] == [
            ("Рег", "code", "Движения.Рег"),
            (
                "Рег2",
                "code",
                'ПроведениеСервер.ОтразитьДвиженияПоРегистру(ДополнительныеСвойства, Движения, Отказ, "Рег2");',
            ),
        ]

    def test_delegate_and_direct_same_register_one_row(self, world18, tmp_path, monkeypatch):
        rel = _doc_module(world18, "ObjectModule")
        root = tmp_path / "cf"
        world18["write"](root, self._overlap_files(world18))
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "cf_idx"))
        db = IndexBuilder().build(str(root))
        reader = IndexReader(db)
        try:
            res = _world18_helpers(root, None, reader)["find_register_movements"]("Документ.Док")
        finally:
            reader.close()
        rows = [(r["name"], r["source"]) for r in res["code_registers"] if r["file"] == rel]
        assert sorted(rows) == [("Рег", "code"), ("Рег2", "code")], res["code_registers"]
        # Индекс прежнего поколения (строк делегата нет — раскрытие на чтении, v1.43.0) отвечает тем же составом.
        copy = _db_copy(str(db), tmp_path / "bsl_index_v17.db")
        _downgrade_to_v17(copy)
        con = sqlite3.connect(str(copy))
        try:
            deleted = con.execute(
                "DELETE FROM register_movements WHERE source = 'code' AND file = ? AND evidence LIKE 'ПроведениеСервер.%'",
                (rel,),
            ).rowcount
            con.commit()
        finally:
            con.close()
        assert deleted == 1  # строка делегата — только у «Рег2»: «Рег» уже есть прямой
        old_reader = IndexReader(copy)
        try:
            old = _world18_helpers(root, None, old_reader)["find_register_movements"]("Документ.Док")
        finally:
            old_reader.close()
        assert sorted((r["name"], r["source"]) for r in old["code_registers"] if r["file"] == rel) == sorted(rows)

    @pytest.mark.parametrize("route", ["full", "git"])
    def test_delegate_and_direct_change_equals_build(self, world18, route, tmp_path_factory, monkeypatch):
        db = _assert_update_equals_build(
            tmp_path_factory,
            monkeypatch,
            world18["write"],
            world18["files"],
            self._overlap_files(world18),
            {"register_movements": _MOVEMENTS_SQL},
            route=route,
            check_route=False,
        )
        sql = "SELECT register_name, evidence FROM register_movements WHERE source = 'code' AND file LIKE '%ObjectModule.bsl'"
        assert _rows(db, sql) == [
            ("Рег", "Движения.Рег"),
            ("Рег2", 'ПроведениеСервер.ОтразитьДвиженияПоРегистру(ДополнительныеСвойства, Движения, Отказ, "Рег2");'),
        ]

    @pytest.mark.parametrize("route", ["full", "git"])
    def test_object_module_change_equals_build(self, world18, route, tmp_path_factory, monkeypatch):
        files_b = _edit(
            world18["files"],
            _doc_module(world18, "ObjectModule"),
            'Движения, Отказ, "Рег");',
            'Движения, Отказ, "Рег2");',
        )
        db = _assert_update_equals_build(
            tmp_path_factory,
            monkeypatch,
            world18["write"],
            world18["files"],
            files_b,
            {"register_movements": _MOVEMENTS_SQL},
            route=route,
            check_route=False,
        )
        sql = "SELECT register_name FROM register_movements WHERE source = 'code' AND file LIKE '%ObjectModule.bsl'"
        assert _rows(db, sql) == [("Рег2",)]

    def test_moved_names_reexported_by_helpers(self):
        import rlm_tools_bsl.bsl_helpers as H

        for name in (
            "_pf_call_args",
            "_PF_ID_LITERAL_RE",
            "_REGISTER_BY_NAME_DELEGATES",
            "_REGISTER_BY_NAME_CALL_RE",
            "_register_by_name_refs",
        ):
            assert getattr(H, name) is getattr(BI, name), name

    def test_table_head_as_structure_property(self):
        """Голова — свойство структуры (`ДополнительныеСвойства.ТаблицыДляДвижений.Вставить`) — так в БГУ чаще всего."""
        content = "\n".join(
            [
                "Процедура ИнициализироватьДанныеДокумента(ДокументСсылка, ДополнительныеСвойства) Экспорт",
                '\tДополнительныеСвойства.ТаблицыДляДвижений.Вставить("Рег", Т);',
                '\tСтруктура.ТаблицыЗапроса . Добавить ( "Рег2" );',
                '\tМоиТаблицыЗапроса.Добавить("Рег3");',
                "КонецПроцедуры",
            ]
        )
        rows = [r[0] for r in _movements_of(content, "ManagerModule") if r[1] == "movement_table"]
        assert rows == ["Рег", "Рег2"]


# ═══════════════ Задача 14 — механизм от делегата (Д14) ═══════════════


def _variant_movements(world: World, tmp_path, monkeypatch, rel: str, old: str, new: str, *, with_index: bool = True):
    files = _edit(world["files"], rel, old, new)
    root = tmp_path / "cf"
    world["write"](root, files)
    reader = _reader_for(root, monkeypatch) if with_index else None
    try:
        return _world18_helpers(root, None, reader)["find_register_movements"]("Документ.Док")
    finally:
        if reader is not None:
            reader.close()


class TestDelegateMechanisms:
    @pytest.mark.parametrize("helpers", ["idx", "live"])
    def test_delegate_mechanism_added(self, world18, helpers):
        res = world18[helpers]["find_register_movements"]("Документ.Док")
        assert res["erp_mechanisms"] == ["Мех1", "Мех2"], res
        cm = f"CommonModules/ДокЛокализация/{'Ext/' if world18['fmt'] == 'cf' else ''}Module.bsl"
        assert res["_meta"]["mechanism_delegates"] == [
            {"module": cm, "procedure": "ЗарегистрироватьУчетныеМеханизмы", "mechanisms": ["Мех2"]}
        ]

    def test_index_v17_also_sees_delegate(self, world18, tmp_path):
        reader = _v17_copy(world18, tmp_path)
        try:
            res = _world18_helpers(world18["root"], world18["ext_root"], reader)["find_register_movements"](
                "Документ.Док"
            )
        finally:
            reader.close()
        assert res["erp_mechanisms"] == ["Мех1", "Мех2"]
        assert res["_meta"]["mechanism_delegates"][0]["mechanisms"] == ["Мех2"]

    def test_delegate_without_mechanisms(self, world18, tmp_path, monkeypatch):
        rel = f"CommonModules/ДокЛокализация/{'Ext/' if world18['fmt'] == 'cf' else ''}Module.bsl"
        res = _variant_movements(world18, tmp_path, monkeypatch, rel, '\tМеханизмыДокумента.Добавить("Мех2");\n', "")
        assert res["erp_mechanisms"] == ["Мех1"]
        assert "mechanism_delegates" not in (res.get("_meta") or {})

    def test_non_common_module_head_ignored(self, world18, tmp_path, monkeypatch):
        res = _variant_movements(
            world18,
            tmp_path,
            monkeypatch,
            _doc_module(world18, "ManagerModule"),
            "\tДокЛокализация.ЗарегистрироватьУчетныеМеханизмы(МеханизмыДокумента);",
            "\tНеОбщийМодуль.ЗарегистрироватьУчетныеМеханизмы(МеханизмыДокумента);",
        )
        assert res["erp_mechanisms"] == ["Мех1"]
        assert "mechanism_delegates" not in (res.get("_meta") or {})

    def test_commented_delegate_call_ignored(self, world18, tmp_path, monkeypatch):
        res = _variant_movements(
            world18,
            tmp_path,
            monkeypatch,
            _doc_module(world18, "ManagerModule"),
            "\tДокЛокализация.ЗарегистрироватьУчетныеМеханизмы(МеханизмыДокумента);",
            "\t// ДокЛокализация.ЗарегистрироватьУчетныеМеханизмы(МеханизмыДокумента);",
        )
        assert res["erp_mechanisms"] == ["Мех1"]
        assert "mechanism_delegates" not in (res.get("_meta") or {})

    def test_other_first_argument_ignored(self, world18, tmp_path, monkeypatch):
        res = _variant_movements(
            world18,
            tmp_path,
            monkeypatch,
            _doc_module(world18, "ManagerModule"),
            "\tДокЛокализация.ЗарегистрироватьУчетныеМеханизмы(МеханизмыДокумента);",
            "\tДокЛокализация.ЗарегистрироватьУчетныеМеханизмы(ДругойСписок);",
        )
        assert res["erp_mechanisms"] == ["Мех1"]

    def test_profile_counts_delegate_mechanisms(self, world18):
        reg = world18["idx"]["get_object_profile"]("Документ.Док")["sections"]["registers"]
        items = {(i["register"], i["source"]) for i in reg["items"]}
        assert {("Мех1", "erp_mechanism"), ("Мех2", "erp_mechanism")} <= items, reg
        assert reg["summary"]["erp_mechanisms"] == 2


# ═══════════════ Задача 15 — перехват события и команды формы расширения (Д1) ═══════════════

_BASE_FORM_MODULE = "Documents/Док/Forms/ФормаДокумента/Ext/Form/Module.bsl"
_OVERRIDES_SQL = (
    "SELECT object_name, source_path, target_method, target_method_line, annotation, extension_name, "
    "extension_purpose, extension_method, ext_module_path, ext_line, form_item, form_event FROM extension_overrides"
)


def _cf_only(world: World) -> None:
    if world["fmt"] != "cf":
        pytest.skip("расширение world18 — только в CF")


def _doc_overrides(helpers) -> list[dict]:
    return helpers["get_overrides"]("Документ.Док", limit=1000)["overrides"]


def _by_method(rows: list[dict], method: str) -> dict:
    (row,) = [r for r in rows if r["extension_method"] == method]
    return row


class TestFormCallTypeOverrides:
    @pytest.mark.parametrize("helpers", ["idx", "live"])
    def test_form_event_after_is_override(self, world18, helpers):
        _cf_only(world18)
        r = _by_method(_doc_overrides(world18[helpers]), "Расш1_ПриСозданииПосле")
        assert (r["annotation"], r["target_method"], r["form_item"], r["form_event"]) == (
            "После",
            "ПриСозданииНаСервере",
            "",
            "OnCreateAtServer",
        ), r
        assert r["source_path"] == _BASE_FORM_MODULE
        assert r["target_method_line"] == 2
        assert r["ext_module_path"] == _BASE_FORM_MODULE
        assert r["ext_line"] == 2, r  # строка объявления метода расширения в модуле формы

    @pytest.mark.parametrize("helpers", ["idx", "live"])
    def test_command_action_override(self, world18, helpers):
        _cf_only(world18)
        r = _by_method(_doc_overrides(world18[helpers]), "Расш1_Команда1Вместо")
        assert (r["annotation"], r["target_method"], r["form_item"], r["form_event"]) == (
            "Вместо",
            "Команда1",
            "Команда1",
            "Action",
        ), r

    @pytest.mark.parametrize("helpers", ["idx", "live"])
    def test_unresolved_target_kept(self, world18, helpers):
        """Перенятое поле `Сумма`: в форме основной конфигурации обработчика `OnChange` у него нет — строка
        остаётся с пустой целью."""
        _cf_only(world18)
        r = _by_method(_doc_overrides(world18[helpers]), "Расш1_СуммаПриИзмененииПеред")
        assert (r["annotation"], r["target_method"], r["target_method_line"], r["form_item"], r["form_event"]) == (
            "Перед",
            "",
            None,
            "Сумма",
            "OnChange",
        ), r

    @pytest.mark.parametrize("helpers", ["idx", "live"])
    @pytest.mark.parametrize(
        "method",
        ["Расш1_НетВБазеПеред", "Расш1_ПолеПриИзмененииПосле", "Расш1_СуммаКомандаВместо"],
        ids=["own_command", "own_element", "own_command_named_like_adopted_element"],
    )
    def test_own_item_not_override(self, world18, helpers, method):
        """Волны e2e 1.47.0: событие или действие команды СВОЕГО элемента формы расширения (имени нет среди узлов
        `<BaseForm>` того же вида) платформа пишет с `callType`, но перехватывать ему нечего — строки нет. Своя
        команда `Сумма` одноимённа перенятому полю `Сумма`: сверка — по виду узла (команда — среди команд)."""
        _cf_only(world18)
        rows = _doc_overrides(world18[helpers])
        assert method not in {r["extension_method"] for r in rows}, rows
        ext_rows = world18["idx"]["find_ext_overrides"](str(world18["ext_root"]), "Док")["overrides"]
        assert method not in {r["extension_method"] for r in ext_rows}, ext_rows

    @pytest.mark.parametrize("helpers", ["idx", "live"])
    def test_annotation_rows_have_null_form_keys(self, world18, helpers):
        _cf_only(world18)
        r = _by_method(_doc_overrides(world18[helpers]), "Расш1_ПриОткрытииПосле")
        assert (r["annotation"], r["target_method"], r["form_item"], r["form_event"]) == (
            "После",
            "ПриОткрытии",
            None,
            None,
        )

    def test_index_equals_live(self, world18):
        _cf_only(world18)

        def keys(helpers):
            return {
                (r["form_item"], r["form_event"], r["extension_method"], r["annotation"], r["target_method"])
                for r in _doc_overrides(world18[helpers])
            }

        assert keys("idx") == keys("live")

    def test_find_ext_overrides_sees_form_rows(self, world18):
        _cf_only(world18)
        res = world18["idx"]["find_ext_overrides"](str(world18["ext_root"]), "Док")
        rows = {(r["extension_method"], r["form_event"], r["target_method"]) for r in res["overrides"]}
        assert ("Расш1_ПриСозданииПосле", "OnCreateAtServer", "ПриСозданииНаСервере") in rows, res
        assert ("Расш1_СуммаПриИзмененииПеред", "OnChange", "") in rows, res

    def test_scan_diagnostics_count_extension_forms(self, world18):
        """Ревью v1.47.0 (M5): форма расширения — кандидат обхода сканера наравне с модулем
        (`scan_diagnostics.candidate_files`/`files_scanned`)."""
        _cf_only(world18)
        from rlm_tools_bsl.extension_detector import find_extension_overrides

        ext = world18["ext_root"]
        n_bsl = sum(1 for _ in ext.rglob("*.bsl"))
        n_forms = sum(1 for p in ext.rglob("Form.xml") if p.parent.name == "Ext")
        diagnostics: dict = {}
        find_extension_overrides(str(ext), diagnostics=diagnostics, main_root=str(world18["root"]))
        assert n_forms >= 1
        got = (diagnostics["candidate_files"], diagnostics["files_scanned"], diagnostics["complete"])
        assert got == (n_bsl + n_forms, n_bsl + n_forms, True), diagnostics

    def test_unreadable_extension_form_marks_scan_partial(self, world18, monkeypatch):
        """Ревью v1.47.0 (M5): нечитаемая форма расширения — неполный обход (перехват в ней мог потеряться):
        `find_ext_overrides` и живой `get_overrides` отвечают `partial=True`, файл назван в диагностике."""
        _cf_only(world18)
        ext = world18["ext_root"]
        form = next(p for p in ext.rglob("Form.xml") if p.parent.name == "Ext")
        real_read_bytes = Path.read_bytes

        def read_bytes(self):
            if self == form:
                raise PermissionError(f"locked: {self}")
            return real_read_bytes(self)

        monkeypatch.setattr(Path, "read_bytes", read_bytes)
        res = world18["idx"]["find_ext_overrides"](str(ext))
        assert res["partial"] is True, res
        assert form.relative_to(ext).as_posix() in res["_meta"]["scan_diagnostics"]["unreadable_files"], res
        assert "Расш1_ПриСозданииПосле" not in {r["extension_method"] for r in res["overrides"]}
        live = world18["live"]["get_overrides"]()
        assert live["partial"] is True, live
        assert [r["extension_root"] for r in live["_meta"]["failed_extension_roots"]] == [str(ext)], live["_meta"]

    @pytest.mark.parametrize("helpers", ["idx"])
    def test_overridden_by_on_base_form_module(self, world18, helpers):
        """Только индекс: без индекса `extract_procedures` `overridden_by` не строит (и прежде); живьём перехват
        формы виден в `get_overrides` и `read_procedure(..., include_overrides=True)`."""
        _cf_only(world18)
        procs = {p["name"]: p for p in world18[helpers]["extract_procedures"](_BASE_FORM_MODULE)}
        rows = procs["ПриСозданииНаСервере"].get("overridden_by") or []
        assert {(r["extension_method"], r["form_item"], r["form_event"]) for r in rows} == {
            ("Расш1_ПриСозданииПосле", "", "OnCreateAtServer")
        }, rows

    @pytest.mark.parametrize("helpers", ["idx"])
    def test_overridden_by_annotation_rows_unchanged(self, world18, helpers):
        """Обе версии: строка `&После("ПриОткрытии")` — прежний набор ключей (только индекс, как прежде)."""
        _cf_only(world18)
        procs = {p["name"]: p for p in world18[helpers]["extract_procedures"](_BASE_FORM_MODULE)}
        (row,) = procs["ПриОткрытии"]["overridden_by"]
        assert set(row) == {
            "annotation",
            "extension_name",
            "extension_method",
            "extension_root",
            "ext_module_path",
            "ext_line",
            "extension_file",
        }

    @pytest.mark.parametrize("helpers", ["idx", "live"])
    def test_read_procedure_include_overrides_body(self, world18, helpers):
        _cf_only(world18)
        text = world18[helpers]["read_procedure"](_BASE_FORM_MODULE, "ПриСозданииНаСервере", include_overrides=True)
        assert "Процедура Расш1_ПриСозданииПосле" in text, text
        assert "OnCreateAtServer" in text, text

    def test_object_modules_override_count(self, world18):
        _cf_only(world18)
        res = world18["idx"]["get_object_modules"]("Документ.Док")
        (entry,) = [m for m in res["modules"] if m["path"] == _BASE_FORM_MODULE]
        assert entry["overrides"]["methods"] == ["Команда1", "ПриОткрытии", "ПриСозданииНаСервере"], entry
        assert entry["overrides"]["count"] == 3
        assert any(r["target_method"] == "" for r in _doc_overrides(world18["idx"]))

    def test_aggregates_count_form_rows(self, world18):
        _cf_only(world18)
        res = world18["idx"]["get_overrides"]("Документ.Док", limit=1000)
        rows = res["overrides"]
        assert res["by_annotation"] == {
            k: v for k, v in sorted(_count_annotations(rows).items(), key=lambda kv: (-kv[1], kv[0]))
        }
        assert res["unique_methods"] == len({r["target_method"].lower() for r in rows if r["target_method"]})
        assert {"После", "Вместо", "Перед"} <= set(res["by_annotation"])

    def test_extension_session_without_main(self, world18, tmp_path):
        _cf_only(world18)
        ext_files = {k[len("../cfe/Расш1/") :]: v for k, v in world18["files"].items() if k.startswith("../cfe/Расш1/")}
        # Глубже двух уровней от каталогов соседних тестов: детектор ищет основную конфигурацию у предков.
        root = tmp_path / "iso" / "x" / "Расш1"
        _write_tree(root, ext_files)
        helpers = _bsl_for(
            root, None, current_config_role="extension", current_config_name="Расш1", current_config_root=str(root)
        )
        rows = helpers["get_overrides"]("Документ.Док", limit=1000)["overrides"]
        r = _by_method(rows, "Расш1_ПриСозданииПосле")
        assert (r["target_method"], r["form_event"]) == ("", "OnCreateAtServer"), r

    def test_start_scan_resolves_form_targets(self, world18, monkeypatch):
        """Ревью v1.47.0 (Codex M-1): сканер старта `server._auto_scan_overrides` передаёт корень основной
        конфигурации, как сборщик и живые хелперы: в сессии на основной — её корень, на расширении — основную рядом.
        Цели формовых перехватов — те же, что в индексе, и подробная сводка стратегии (`RLM_EXT_OVERRIDE_DETAIL`)
        печатает обработчик базовой формы, а не `""`."""
        _cf_only(world18)
        from rlm_tools_bsl.bsl_knowledge import _extension_strategy
        from rlm_tools_bsl.extension_detector import detect_extension_context
        from rlm_tools_bsl.server import _auto_scan_overrides

        expected = {
            "Расш1_ПриСозданииПосле": "ПриСозданииНаСервере",
            "Расш1_Команда1Вместо": "Команда1",
            "Расш1_СуммаПриИзмененииПеред": "",  # у поля в основной форме обработчика нет — цель пуста и в индексе
        }
        indexed = {r["extension_method"]: r["target_method"] for r in _doc_overrides(world18["idx"]) if r["form_event"]}
        assert indexed == expected, indexed
        main_ctx = detect_extension_context(str(world18["root"]))
        ext_ctx = detect_extension_context(str(world18["ext_root"]))
        assert ext_ctx.nearby_main is not None
        for ctx in (main_ctx, ext_ctx):
            scanned = _auto_scan_overrides(ctx)
            rows = [r for rows in scanned.values() for r in rows if r.get("form_event") is not None]
            assert {r["extension_method"]: r["target_method"] for r in rows} == expected, ctx.current.role
        monkeypatch.setenv("RLM_EXT_OVERRIDE_DETAIL", "50")
        text = _extension_strategy(main_ctx, _auto_scan_overrides(main_ctx), slim=True)
        assert '&После("ПриСозданииНаСервере")' in text and '&Вместо("Команда1")' in text, text

    def test_edt_no_rows_boundary(self, tmp_path):
        from rlm_tools_bsl.bsl_xml_parsers import parse_form_event_handlers
        from rlm_tools_bsl.extension_detector import find_extension_overrides

        edt_form = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<form:Form xmlns:form="http://g5.1c.ru/v8/dt/form">\n'
            '  <handlers callType="After">\n    <event>OnCreateAtServer</event>\n    <name>Р_После</name>\n'
            "  </handlers>\n</form:Form>\n"
        )
        assert parse_form_event_handlers(edt_form) == ({}, [])
        # Глубже двух уровней от каталогов соседних тестов: детектор ищет расширения и основную у предков.
        root = tmp_path / "iso" / "x" / "ext"
        _write_tree(
            root,
            {
                "Configuration/Configuration.mdo": EDT_DESCRIPTOR,
                "Documents/Док/Forms/Ф/Form.form": edt_form,
                "Documents/Док/Forms/Ф/Module.bsl": "&НаСервере\nПроцедура Р_После(Отказ)\nКонецПроцедуры\n",
            },
        )
        assert [r for r in find_extension_overrides(str(root)) if r.get("form_event") is not None] == []

    def test_uuid_event_name_kept(self, tmp_path):
        from rlm_tools_bsl.extension_detector import find_extension_overrides

        uuid = "01d80ddd-1234-4abc-8def-0123456789ab"
        form = "\n".join(
            [
                '<?xml version="1.0" encoding="UTF-8"?>',
                f'<Form {_CF_FORM_NS} version="2.20">',
                "\t<Events>",
                f'\t\t<Event name="{uuid}" callType="Override">Расш_Ув</Event>',
                "\t</Events>",
                "</Form>",
            ]
        )
        # Глубже двух уровней от каталогов соседних тестов: детектор ищет расширения и основную у предков.
        root = tmp_path / "iso" / "x" / "ext"
        _write_tree(
            root,
            {
                "Configuration.xml": CF_EXT_DESCRIPTOR,
                "Documents/Док/Forms/Ф/Ext/Form.xml": form,
                "Documents/Док/Forms/Ф/Ext/Form/Module.bsl": "&НаСервере\nПроцедура Расш_Ув()\nКонецПроцедуры\n",
            },
        )
        rows = [r for r in find_extension_overrides(str(root)) if r.get("form_event") is not None]
        assert [(r["form_event"], r["target_method"], r["annotation"], r["line"]) for r in rows] == [
            (uuid, "", "Вместо", 2)
        ]

    def test_form_without_baseform_not_filtered(self, tmp_path):
        """Волны e2e 1.47.0: свой элемент узнаётся по `<BaseForm>` формы расширения; в форме без `<BaseForm>`
        отбора нет — событие элемента остаётся строкой (своих форм с `callType` на корпусе нет)."""
        from rlm_tools_bsl.extension_detector import find_extension_overrides

        form = "\n".join(
            [
                '<?xml version="1.0" encoding="UTF-8"?>',
                f'<Form {_CF_FORM_NS} version="2.20">',
                "\t<ChildItems>",
                '\t\t<InputField name="Поле" id="1">',
                "\t\t\t<Events>",
                '\t\t\t\t<Event name="OnChange" callType="After">Расш_ПолеПосле</Event>',
                "\t\t\t</Events>",
                "\t\t</InputField>",
                "\t</ChildItems>",
                "</Form>",
            ]
        )
        # Глубже двух уровней от каталогов соседних тестов: детектор ищет расширения и основную у предков.
        root = tmp_path / "iso" / "x" / "ext"
        _write_tree(
            root,
            {
                "Configuration.xml": CF_EXT_DESCRIPTOR,
                "Documents/Док/Forms/Ф/Ext/Form.xml": form,
                "Documents/Док/Forms/Ф/Ext/Form/Module.bsl": "&НаКлиенте\nПроцедура Расш_ПолеПосле(Элемент)\nКонецПроцедуры\n",
            },
        )
        rows = [r for r in find_extension_overrides(str(root)) if r.get("form_event") is not None]
        assert [(r["form_item"], r["form_event"], r["extension_method"]) for r in rows] == [
            ("Поле", "OnChange", "Расш_ПолеПосле")
        ]

    def test_parse_form_event_handlers_cf(self):
        from rlm_tools_bsl.bsl_xml_parsers import parse_form_event_handlers

        handlers, intercepts = parse_form_event_handlers(_cf_ext_doc_form())
        assert handlers == {}  # базовая форма внутри <BaseForm> не читается
        # Свои элементы (`Расш1_Поле`, команды `НетВБазе` и `Сумма`) — не перехваты.
        assert [(i["item"], i["event"], i["call_type"], i["handler"]) for i in intercepts] == [
            ("", "OnCreateAtServer", "After", "Расш1_ПриСозданииПосле"),
            ("Сумма", "OnChange", "Before", "Расш1_СуммаПриИзмененииПеред"),
            ("Команда1", "Action", "Override", "Расш1_Команда1Вместо"),
        ]
        assert [i["line"] for i in intercepts] == [4, 9, 20]
        base_handlers, base_intercepts = parse_form_event_handlers(_cf_doc_form())
        assert base_intercepts == []
        assert base_handlers[("", "OnCreateAtServer")] == "ПриСозданииНаСервере"
        assert base_handlers[("Команда1", "Action")] == "Команда1"

    def test_v17_index_rows_have_null_form_keys(self, world18, tmp_path):
        _cf_only(world18)
        reader = _v17_copy(world18, tmp_path)
        try:
            helpers = _world18_helpers(world18["root"], world18["ext_root"], reader)
            rows = helpers["get_overrides"]("Документ.Док", limit=1000)["overrides"]
        finally:
            reader.close()
        assert rows and all(r["form_item"] is None and r["form_event"] is None for r in rows)

    @pytest.mark.parametrize("route", ["full", "git"])
    def test_update_equals_build(self, world18, route, tmp_path_factory, monkeypatch):
        _cf_only(world18)
        files_b = _edit(
            world18["files"],
            "Documents/Док/Forms/ФормаДокумента/Ext/Form.xml",
            '<Event name="OnCreateAtServer">ПриСозданииНаСервере</Event>',
            '<Event name="OnCreateAtServer">ПриСозданииНаСервереНов</Event>',
        )
        db = _assert_update_equals_build(
            tmp_path_factory,
            monkeypatch,
            world18["write"],
            world18["files"],
            files_b,
            {"extension_overrides": _OVERRIDES_SQL},
            route=route,
            check_route=False,
        )
        sql = "SELECT target_method FROM extension_overrides WHERE form_event = 'OnCreateAtServer'"
        assert _rows(db, sql) == [("ПриСозданииНаСервереНов",)]


def _count_annotations(rows: list[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rows:
        out[r["annotation"]] = out.get(r["annotation"], 0) + 1
    return out


# ═══════════════ Волны e2e: секция перехвата без тела говорит почему ═══════════════

_EXT_DOC_FORM_KEY = "../cfe/Расш1/Documents/Док/Forms/ФормаДокумента/Ext/Form.xml"
_EXT_DOC_MODULE_KEY = "../cfe/Расш1/Documents/Док/Forms/ФормаДокумента/Ext/Form/Module.bsl"
_NOTE_UNREAD = "// Тело не прочитано: модуль расширения недоступен из этой сессии"


@pytest.fixture(scope="class")
def world18_dangling(tmp_path_factory):
    """world18 (CF), где форма расширения называет обработчик события `OnOpen`, которого в модуле формы нет."""
    files = _world18_files("cf")
    anchor = '\t\t<Event name="OnCreateAtServer" callType="After">Расш1_ПриСозданииПосле</Event>'
    assert files[_EXT_DOC_FORM_KEY].count(anchor) == 1
    files[_EXT_DOC_FORM_KEY] = files[_EXT_DOC_FORM_KEY].replace(
        anchor, anchor + '\n\t\t<Event name="OnOpen" callType="Before">Расш1_НетТакойПроцедуры</Event>'
    )
    world = _build_world18(tmp_path_factory.mktemp("world18_dangling"), "cf", files=files)
    yield world
    world["reader"].close()


def _patch_ext_module_read(world: World, monkeypatch, edit) -> None:
    """``Path.read_text`` модуля формы расширения — через ``edit(текст)``; ``edit`` может бросить исключение."""
    module = (world["root"] / _EXT_DOC_MODULE_KEY).resolve()
    real = Path.read_text

    def read_text(self, *args, **kwargs):
        text = real(self, *args, **kwargs)
        return edit(text) if self == module else text

    monkeypatch.setattr(Path, "read_text", read_text)


class TestOverrideSectionNotes:
    """Волны e2e 1.47.0: секция перехвата у `read_procedure(..., include_overrides=True)` без тела говорит почему —
    обработчика, названного формой, в модуле нет; метода аннотации в модуле уже нет (индекс старше модуля); модуль не
    прочитан. Прежде секция молча кончалась строкой `// Файл: …`. Секции с телом — без пометок."""

    @pytest.mark.parametrize("helpers", ["idx", "live"])
    def test_dangling_form_handler_row(self, world18_dangling, helpers):
        r = _by_method(_doc_overrides(world18_dangling[helpers]), "Расш1_НетТакойПроцедуры")
        assert (r["annotation"], r["target_method"], r["form_item"], r["form_event"], r["ext_line"], r["line"]) == (
            "Перед",
            "ПриОткрытии",
            "",
            "OnOpen",
            None,
            None,
        ), r

    @pytest.mark.parametrize("helpers", ["idx", "live"])
    def test_dangling_form_handler_section_note(self, world18_dangling, helpers):
        text = world18_dangling[helpers]["read_procedure"](_BASE_FORM_MODULE, "ПриОткрытии", include_overrides=True)
        assert "=== Перехвачен Перед (callType формы: событие OnOpen формы)" in text, text
        assert (
            "// Обработчик Расш1_НетТакойПроцедуры в модуле формы расширения не найден: форма расширения ссылается "
            "на процедуру, которой в модуле нет"
        ) in text, text
        # Секция аннотации того же метода — с телом и без пометок.
        assert "Процедура Расш1_ПриОткрытииПосле" in text, text
        assert text.count("не найден") == 1 and _NOTE_UNREAD not in text, text

    @pytest.mark.parametrize("helpers", ["idx", "live"])
    def test_section_with_body_has_no_note(self, world18_dangling, helpers):
        text = world18_dangling[helpers]["read_procedure"](
            _BASE_FORM_MODULE, "ПриСозданииНаСервере", include_overrides=True
        )
        assert "Процедура Расш1_ПриСозданииПосле" in text, text
        assert "не найден" not in text and _NOTE_UNREAD not in text, text

    def test_stale_annotation_row_note(self, world18_dangling, monkeypatch):
        """Строка аннотации из индекса, а метода в модуле расширения уже нет (модуль правили после сборки)."""
        _patch_ext_module_read(
            world18_dangling,
            monkeypatch,
            lambda text: text.replace("Расш1_ПриОткрытииПосле", "Расш1_ПриОткрытииПереименован"),
        )
        text = world18_dangling["idx"]["read_procedure"](_BASE_FORM_MODULE, "ПриОткрытии", include_overrides=True)
        assert (
            "// Метод Расш1_ПриОткрытииПосле в модуле расширения не найден: модуль изменился после сборки индекса — "
            "обнови индекс"
        ) in text, text

    def test_unreadable_module_note(self, world18_dangling, monkeypatch):
        def locked(text):
            raise PermissionError("locked")

        _patch_ext_module_read(world18_dangling, monkeypatch, locked)
        text = world18_dangling["idx"]["read_procedure"](_BASE_FORM_MODULE, "ПриОткрытии", include_overrides=True)
        assert text.count(_NOTE_UNREAD) == 2, text  # обе секции метода: аннотация и событие формы
        assert "Процедура Расш1_ПриОткрытииПосле" not in text, text

    def test_untrusted_root_note(self, world18_dangling, monkeypatch):
        """Корни расширений не определены (детектор упал) — тела не читаются, секция это говорит."""
        import rlm_tools_bsl.extension_detector as detector

        def broken(*args, **kwargs):
            raise RuntimeError("detector failed")

        monkeypatch.setattr(detector, "detect_extension_context", broken)
        text = world18_dangling["idx"]["read_procedure"](
            _BASE_FORM_MODULE, "ПриСозданииНаСервере", include_overrides=True
        )
        assert _NOTE_UNREAD in text and "Процедура Расш1_ПриСозданииПосле" not in text, text

    def test_unreadable_module_line_none_and_partial(self, world18_dangling, monkeypatch):
        """Ревью Codex (M2): `ext_line=None` — и у обработчика, который в модуле объявлен, если модуль формы
        расширения не прочитан: живой ответ тогда `partial=True`, а секция — «тело не прочитано», не «не найден»."""
        import builtins

        module = (world18_dangling["root"] / _EXT_DOC_MODULE_KEY).resolve()

        def locked(text):
            raise PermissionError("locked")

        _patch_ext_module_read(world18_dangling, monkeypatch, locked)
        real_open = builtins.open

        def guarded_open(file, *args, **kwargs):
            if isinstance(file, (str, os.PathLike)) and Path(file).resolve() == module:
                raise PermissionError("locked")
            return real_open(file, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", guarded_open)
        res = world18_dangling["live"]["get_overrides"]("Документ.Док", limit=1000)
        r = _by_method(res["overrides"], "Расш1_ПриСозданииПосле")
        assert (r["ext_line"], r["line"], res["partial"]) == (None, None, True), (r, res.get("_meta"))
        text = world18_dangling["live"]["read_procedure"](
            _BASE_FORM_MODULE, "ПриСозданииНаСервере", include_overrides=True
        )
        assert _NOTE_UNREAD in text and "не найден" not in text, text

    def test_missing_module_file_note(self, world18_dangling, monkeypatch):
        """Модуля формы расширения нет вовсе (`FileNotFoundError`) — обработчика в нём нет тем более: у строки из
        формы — пометка «не найден»."""

        def gone(text):
            raise FileNotFoundError("gone")

        _patch_ext_module_read(world18_dangling, monkeypatch, gone)
        text = world18_dangling["idx"]["read_procedure"](
            _BASE_FORM_MODULE, "ПриСозданииНаСервере", include_overrides=True
        )
        assert "// Обработчик Расш1_ПриСозданииПосле в модуле формы расширения не найден" in text, text

    @pytest.mark.parametrize("helpers", ["idx", "live"])
    def test_note_with_numbered_lines(self, world18_dangling, helpers):
        """`numbered=True`: тело секции с номерами строк, пометка — отдельной строкой без номера."""
        text = world18_dangling[helpers]["read_procedure"](
            _BASE_FORM_MODULE, "ПриОткрытии", include_overrides=True, numbered=True
        )
        assert "\n// Обработчик Расш1_НетТакойПроцедуры в модуле формы расширения не найден" in text, text
        assert re.search(r"^\s*\d+ \| Процедура Расш1_ПриОткрытииПосле", text, re.M), text


# ═══════════════ Ускорение сборки без изменения состава индекса ═══════════════
# Права — маски от `Roles` корня вместо рекурсивного `**`; `file_paths` — stat из записи каталога; прогрев описателей
# на время полного скана `update`. Каждое — тот же результат, что прежде (сравнение с прежней реализацией).

_RIGHTS_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>\n<Rights xmlns="http://v8.1c.ru/8.2/roles">\n'
    "<setForNewObjects>false</setForNewObjects>\n"
    "<object><name>Catalog.А</name><right><name>Read</name><value>true</value></right></object></Rights>\n"
)


def _rights_files_recursive(base: Path) -> list[tuple[str, str]]:
    """Прежний поиск файлов прав (до v1.47.0) — `(роль, путь от корня)` в его порядке."""
    found = [(f.parent.parent.name, f) for f in base.glob("**/Roles/*/Ext/Rights.xml")]
    found += [(f.parent.name, f) for f in base.glob("**/Roles/*/*.rights")]
    return [(role, f.relative_to(base).as_posix()) for role, f in found]


def _file_paths_os_walk(base_path: str) -> list[tuple]:
    """Прежний `_collect_file_paths` (до v1.47.0): `os.walk` и отдельный `stat` каждого файла."""
    base = Path(base_path)
    rows: list[tuple] = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in BI._SKIP_DIRS_NAV and not d.startswith(".")]
        for fname in filenames:
            if fname.startswith("."):
                continue
            ext = os.path.splitext(fname)[1].lower()
            if ext not in BI._FILE_NAV_EXTENSIONS:
                continue
            full_path = Path(dirpath) / fname
            try:
                st = full_path.stat()
            except OSError:
                continue
            rel = full_path.relative_to(base).as_posix()
            parts = rel.split("/")
            rows.append((rel, ext, "/".join(parts[:-1]), fname, len(parts), st.st_size, st.st_mtime))
    return rows


class TestBuildSpeedWithoutIndexChange:
    def test_rights_from_top_level_roles_without_recursive_glob(self, tmp_path, monkeypatch):
        _write_tree(
            tmp_path,
            {
                "Roles/Р2/Ext/Rights.xml": _RIGHTS_XML,
                "Roles/Р1/Ext/Rights.xml": _RIGHTS_XML,
                "Roles/Р3/Rights.rights": _RIGHTS_XML,
                "Catalogs/А.xml": "<x/>",
            },
        )
        patterns: list[str] = []
        real_glob = Path.glob

        def spy(self, pattern, *a, **kw):
            patterns.append(pattern)
            return real_glob(self, pattern, *a, **kw)

        expected = _rights_files_recursive(tmp_path)
        monkeypatch.setattr(Path, "glob", spy)
        data = BI._collect_role_data(str(tmp_path))
        assert patterns and not [p for p in patterns if p.startswith("**")], patterns
        assert [(role, rel) for role, _flag, rel in data.flags] == expected
        assert len(expected) == 3

    def test_rights_nested_roles_without_top_level(self, tmp_path):
        """Контейнер без `Roles` в корне (конфигурации глубже) — прежний рекурсивный поиск, тот же состав."""
        _write_tree(
            tmp_path,
            {
                "cf/Roles/Р1/Ext/Rights.xml": _RIGHTS_XML,
                "cfe/Расш1/Roles/Р2/Ext/Rights.xml": _RIGHTS_XML,
                "edt/Roles/Р3/Rights.rights": _RIGHTS_XML,
            },
        )
        data = BI._collect_role_data(str(tmp_path))
        expected = _rights_files_recursive(tmp_path)
        assert [(role, rel) for role, _flag, rel in data.flags] == expected
        assert {role for role, _rel in expected} == {"Р1", "Р2", "Р3"}

    def test_file_paths_equal_os_walk(self, tmp_path):
        _write_tree(
            tmp_path,
            {
                "Configuration.xml": CF_DESCRIPTOR,
                "Catalogs/Б.xml": "<x/>",
                "Catalogs/А.xml": "<x/>",
                "Catalogs/А/Ext/ObjectModule.bsl": "// код\n",
                "Catalogs/А/Forms/Ф/Ext/Form.xml": "<Form/>",
                "Catalogs/А/Forms/Ф/Ext/Form/Module.bsl": "",
                "Documents/Д/Д.mdo": "<x/>",
                "Documents/Д/Form.form": "<x/>",
                "Documents/Д/Big.XML": "<x/>" * 50,
                "Documents/Д/readme.txt": "текст",
                "Documents/.hidden.xml": "<x/>",
                ".git/objects/pack.xml": "<x/>",
                "node_modules/dep.xml": "<x/>",
                "A/B/C/D/deep.bsl": "",
            },
        )
        if os.name != "nt":
            os.symlink(tmp_path / "Catalogs", tmp_path / "Ссылка", target_is_directory=True)
        got = BI._collect_file_paths(str(tmp_path))
        assert got == _file_paths_os_walk(str(tmp_path))
        assert ("Documents/Д/Big.XML", ".xml", "Documents/Д", "Big.XML", 3) in [r[:5] for r in got]
        assert not [r for r in got if r[0].startswith((".git", "node_modules", "Ссылка")) or "/." in r[0]]

    def test_file_paths_take_stat_from_directory_entries(self, tmp_path, monkeypatch):
        _write_tree(tmp_path, {"Catalogs/А.xml": "<x/>", "Catalogs/А/Ext/ObjectModule.bsl": ""})
        calls: list[str] = []
        real_stat = Path.stat

        def spy(self, *a, **kw):
            calls.append(str(self))
            return real_stat(self, *a, **kw)

        monkeypatch.setattr(Path, "stat", spy)
        rows = BI._collect_file_paths(str(tmp_path))
        assert [r[0] for r in rows] == ["Catalogs/А.xml", "Catalogs/А/Ext/ObjectModule.bsl"]
        assert calls == []

    def _built_cf_tree(self, tmp_path, monkeypatch) -> Path:
        base = _cf_doc_tree(
            tmp_path / "cf",
            {
                "Catalogs/А.xml": _cf_md("Catalog", "А"),
                "Catalogs/А/Ext/ObjectModule.bsl": "Процедура П()\nКонецПроцедуры\n",
            },
        )
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
        IndexBuilder().build(str(base), build_calls=True)
        return base

    def _spy_prefetch(self, monkeypatch) -> list[str]:
        started: list[str] = []
        real = BI._DescriptorPrefetch

        class Spy(real):
            def __init__(self, base_path):
                started.append(base_path)
                super().__init__(base_path)

        monkeypatch.setattr(BI, "_DescriptorPrefetch", Spy)
        return started

    def test_full_scan_update_prefetches_descriptors(self, tmp_path, monkeypatch):
        import threading

        base = self._built_cf_tree(tmp_path, monkeypatch)
        _apply_changes(
            base,
            {"Catalogs/А/Ext/ObjectModule.bsl": "Процедура П()\nКонецПроцедуры\n"},
            {"Catalogs/А/Ext/ObjectModule.bsl": "Процедура П2()\nКонецПроцедуры\n"},
        )
        started = self._spy_prefetch(monkeypatch)
        res = IndexBuilder().update(str(base))
        assert res["git_fast_path"] is False and res["changed"] == 1, res
        assert started == [str(base)]
        assert not [t for t in threading.enumerate() if t.name.startswith("rlm-prefetch") and t.is_alive()]

    def test_git_fast_path_update_without_prefetch(self, tmp_path, monkeypatch):
        from test_git_delta import _git_init

        repo = tmp_path / "repo"
        base = _cf_doc_tree(
            repo / "cf",
            {
                "Catalogs/А.xml": _cf_md("Catalog", "А"),
                "Catalogs/А/Ext/ObjectModule.bsl": "Процедура П()\nКонецПроцедуры\n",
            },
        )
        _git_init(repo)
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
        IndexBuilder().build(str(base), build_calls=True)
        _apply_changes(
            base,
            {"Catalogs/А/Ext/ObjectModule.bsl": "Процедура П()\nКонецПроцедуры\n"},
            {"Catalogs/А/Ext/ObjectModule.bsl": "Процедура П2()\nКонецПроцедуры\n"},
        )
        started = self._spy_prefetch(monkeypatch)
        res = IndexBuilder().update(str(base))
        assert res["git_fast_path"] is True, res
        assert started == []


# ═══════════════ Задача 17 — объект вида вне графа ссылок: честный ответ (Д19) ═══════════════


def _outside_graph_tree(root: Path, cf: bool) -> None:
    """Справочник `Спр` (с модулем — без модулей индекс пуст) и объекты видов, которых граф ссылок целью не хранит:
    критерий отбора `Крит` (тип — `Спр`), хранилище настроек `Хран`, HTTP-сервис `API`, веб-сервис `ВС`, внешний
    источник данных `ВИД`, общий макет `Макет`."""
    objs = [
        ("Catalogs", "Catalog", "Спр", (), ()),
        (
            "FilterCriteria",
            "FilterCriterion",
            "Крит",
            (_cf_type(["cfg:CatalogRef.Спр"]),),
            ("<type>\n<types>CatalogRef.Спр</types>\n</type>",),
        ),
        ("SettingsStorages", "SettingsStorage", "Хран", (), ()),
        ("HTTPServices", "HTTPService", "API", (), ()),
        ("WebServices", "WebService", "ВС", (), ()),
        ("ExternalDataSources", "ExternalDataSource", "ВИД", (), ()),
        ("CommonTemplates", "CommonTemplate", "Макет", (), ()),
    ]
    files = {"Configuration.xml": CF_DESCRIPTOR} if cf else {"Configuration/Configuration.mdo": EDT_DESCRIPTOR}
    for folder, kind, name, cf_props, edt_body in objs:
        if cf:
            files[f"{folder}/{name}.xml"] = _cf_obj(kind, name, props=cf_props)
        else:
            files[f"{folder}/{name}/{name}.mdo"] = _edt_obj(kind, name, body=edt_body)
    files[f"Catalogs/Спр/{'Ext/' if cf else ''}ObjectModule.bsl"] = _PROC
    _write_tree(root, files)


_OUTSIDE_GRAPH_REFS = [
    "ХранилищеНастроек.Хран",
    "SettingsStorage.Хран",
    "HTTPСервис.API",
    "HTTPService.API",
    "WebСервис.ВС",
    "ВнешнийИсточникДанных.ВИД",
]


class TestTargetOutsideGraph:
    @pytest.fixture(scope="class", params=["cf", "edt"])
    def tree(self, request, tmp_path_factory):
        root = tmp_path_factory.mktemp(f"outside_graph_{request.param}") / "cf"
        _outside_graph_tree(root, request.param == "cf")
        mp = pytest.MonkeyPatch()
        mp.setenv("RLM_INDEX_DIR", str(root.parent / "idx"))
        try:
            reader = IndexReader(IndexBuilder().build(str(root)))
        finally:
            mp.undo()
        yield {"idx": _bsl_for(root, reader), "live": _bsl_for(root)}
        reader.close()

    @pytest.mark.parametrize("helpers", ["idx", "live"])
    def test_criterion_target_signalled(self, tree, helpers):
        res = tree[helpers]["find_references_to_object"]("КритерийОтбора.Крит")
        assert res["object"] == "FilterCriterion.Крит"
        assert res["_meta"]["target_not_indexed"] is True, res["_meta"]
        assert "analyze_subsystem('Крит')" in res["hint"] and "find_roles('Крит')" in res["hint"], res
        assert {r["kind"] for r in res["references"]} <= {"role_rights"}, res

    @pytest.mark.parametrize("helpers", ["idx", "live"])
    @pytest.mark.parametrize("ref", _OUTSIDE_GRAPH_REFS)
    def test_known_kinds_outside_graph_signalled(self, tree, helpers, ref):
        res = tree[helpers]["find_references_to_object"](ref)
        assert res["_meta"]["target_not_indexed"] is True, (ref, res["_meta"])
        assert f"analyze_subsystem('{ref.split('.')[1]}')" in res["hint"], res

    def test_unknown_head_signalled(self, tree):
        """Голова, которой разбор аргумента не знает (`ОбщийМакет`): ссылок на такой объект граф тоже не хранит."""
        res = tree["idx"]["find_references_to_object"]("ОбщийМакет.Макет")
        assert res["_meta"]["target_not_indexed"] is True, res["_meta"]
        assert "analyze_subsystem('Макет')" in res["hint"], res

    @pytest.mark.parametrize("helpers", ["idx", "live"])
    def test_canonical_target_unchanged(self, tree, helpers):
        """Обе версии: у цели из канонических голов ни признака, ни подсказки."""
        res = tree[helpers]["find_references_to_object"]("Справочник.Спр")
        assert "target_not_indexed" not in res["_meta"] and "hint" not in res, res

    @pytest.mark.parametrize("to", ["КритерийОтбора.Крит", "ХранилищеНастроек.Хран"])
    def test_data_path_to_outside_target_structural(self, tree, to):
        """Цель-критерий и цель-хранилище: прежде тихое `found=False` после обхода."""
        res = tree["idx"]["find_data_path"]("Справочник.Спр", to)
        assert res["found"] is False and res["partial"] is False, res
        assert "в графе ссылок" in res["error"], res
        assert f"analyze_subsystem('{to.split('.')[1]}')" in res["hint"], res
        assert res["_meta"]["nodes_expanded"] == 0, res["_meta"]

    @pytest.mark.parametrize("ref", ["HTTPСервис.API", "WebСервис.ВС", "ВнешнийИсточникДанных.ВИД"])
    @pytest.mark.parametrize("side", ["from", "to"])
    def test_data_path_service_named(self, tree, ref, side):
        """Прежде — отказ «укажите префикс», хотя префикс дан."""
        args = (ref, "Справочник.Спр") if side == "from" else ("Справочник.Спр", ref)
        res = tree["idx"]["find_data_path"](*args)
        assert res["found"] is False and "в графе ссылок" in res["error"], res
        assert "префикс" not in res["hint"], res

    def test_data_path_from_criterion_walks(self, tree):
        """Сохранение: у критерия исходящие ссылки в графе есть — конец-источник обходится."""
        res = tree["idx"]["find_data_path"]("КритерийОтбора.Крит", "Справочник.Спр")
        assert res["found"] is True, res
        assert [step["kind"] for step in res["path"]] == ["filter_criterion_type"]

    def test_data_path_unknown_head_unchanged(self, tree):
        """Обе версии: головы, которой хелперы не знают, — прежний отказ."""
        res = tree["idx"]["find_data_path"]("Справочник.Спр", "ОбщийМакет.Макет")
        assert res["error"] == "endpoints must carry a recognized metadata-type prefix", res


# ═══════════════ Issue #42 — find_register_writers: документы, объявившие регистр в движениях ═══════════════


def _declared_writers_tree(root: Path, cf: bool) -> None:
    """Регистры трёх видов и документы с объявленным составом движений (``RegisterRecords``).

    ``ДокКод`` объявил ``РегА`` и пишет его прямым ``Движения.РегА`` в модуле объекта; ``ДокОбщий`` объявил ``РегА`` и
    ``РегС``, а пишет через общий модуль — в коде самого документа записи нет (шаблон общих механизмов проведения
    типовых конфигураций); ``ДокБезКаталога`` без модулей (в CF — только описатель) объявил ``РегБ``; ``ДокПустой``
    движений не объявил; ``РегНикто`` и ``RegLatin`` (имя латиницей) не объявил никто; независимый регистр сведений
    ``RegSolo`` никто не объявил, а пишет его набором записей модуль менеджера ``ДокМенеджер`` (`manager_code`)."""
    files = {"Configuration.xml": CF_DESCRIPTOR} if cf else {"Configuration/Configuration.mdo": EDT_DESCRIPTOR}
    for folder, kind, name in (
        ("AccumulationRegisters", "AccumulationRegister", "РегА"),
        ("InformationRegisters", "InformationRegister", "РегС"),
        ("AccountingRegisters", "AccountingRegister", "РегБ"),
        ("AccumulationRegisters", "AccumulationRegister", "РегНикто"),
        ("AccumulationRegisters", "AccumulationRegister", "RegLatin"),
        ("InformationRegisters", "InformationRegister", "RegSolo"),
    ):
        if cf:
            files[f"{folder}/{name}.xml"] = _cf_obj(kind, name)
        else:
            files[f"{folder}/{name}/{name}.mdo"] = _edt_obj(kind, name)
    posting = "Процедура ОбработкаПроведения(Отказ, Режим)\n\t{}\nКонецПроцедуры\n"
    for name, refs, module in (
        ("ДокКод", ["AccumulationRegister.РегА"], posting.format("Движения.РегА.Записывать = Истина;")),
        (
            "ДокОбщий",
            ["AccumulationRegister.РегА", "InformationRegister.РегС"],
            posting.format("ПроведениеОбщее.Провести(ЭтотОбъект, Отказ);"),
        ),
        ("ДокБезКаталога", ["AccountingRegister.РегБ"], None),
        ("ДокПустой", [], _PROC),
        ("ДокМенеджер", [], _PROC),
    ):
        if cf:
            props = ["<Posting>Allow</Posting>"] + ([_cf_ref_items("RegisterRecords", refs)] if refs else [])
            files[f"Documents/{name}.xml"] = _cf_obj("Document", name, props=props)
        else:
            body = [f"<registerRecords>{ref}</registerRecords>" for ref in refs] + ["<posting>Allow</posting>"]
            files[f"Documents/{name}/{name}.mdo"] = _edt_obj("Document", name, body=body)
        if module is not None:
            files[f"Documents/{name}/{'Ext/' if cf else ''}ObjectModule.bsl"] = module
    files[f"Documents/ДокМенеджер/{'Ext/' if cf else ''}ManagerModule.bsl"] = (
        "Процедура ЗаписатьВРегистр() Экспорт\n"
        "\tНабор = РегистрыСведений.RegSolo.СоздатьНаборЗаписей();\n"
        "\tНабор.Записать();\n"
        "КонецПроцедуры\n"
    )
    common = "Процедура Провести(Объект, Отказ) Экспорт\n\tОбъект.Движения.РегС.Записывать = Истина;\nКонецПроцедуры\n"
    if cf:
        files["CommonModules/ПроведениеОбщее.xml"] = _cf_obj(
            "CommonModule", "ПроведениеОбщее", props=["<Server>true</Server>"]
        )
        files["CommonModules/ПроведениеОбщее/Ext/Module.bsl"] = common
    else:
        files["CommonModules/ПроведениеОбщее/ПроведениеОбщее.mdo"] = _edt_obj(
            "CommonModule", "ПроведениеОбщее", body=["<server>true</server>"]
        )
        files["CommonModules/ПроведениеОбщее/Module.bsl"] = common
    _write_tree(root, files)


# Регистр → документы, объявившие его в движениях (по имени, без учёта регистра букв).
_DECLARED_EXPECTED = {
    "РегА": ["ДокКод", "ДокОбщий"],
    "РегистрСведений.РегС": ["ДокОбщий"],
    "РегистрБухгалтерии.РегБ": ["ДокБезКаталога"],
    "РегНикто": [],
}


class TestRegisterWritersDeclared:
    """Issue #42: `find_register_writers` отдаёт и документы, объявившие регистр в движениях (`declared_documents`), —
    в том числе тех, чья запись идёт через общие механизмы проведения и в коде самого документа не видна."""

    @pytest.fixture(scope="class", params=["cf", "edt"])
    def tree(self, request, tmp_path_factory):
        root = tmp_path_factory.mktemp(f"declared_writers_{request.param}") / "cf"
        _declared_writers_tree(root, request.param == "cf")
        mp = pytest.MonkeyPatch()
        mp.setenv("RLM_INDEX_DIR", str(root.parent / "idx"))
        try:
            reader = IndexReader(IndexBuilder().build(str(root)))
        finally:
            mp.undo()
        yield {"idx": _bsl_for(root, reader), "live": _bsl_for(root), "reader": reader, "fmt": request.param}
        reader.close()

    @pytest.mark.parametrize("helpers", ["idx", "live"])
    @pytest.mark.parametrize("ref", sorted(_DECLARED_EXPECTED))
    def test_declared_documents(self, tree, helpers, ref):
        """Индекс и живьём — один список: объявившие регистр, по имени; документ без каталога (CF) — тоже."""
        res = tree[helpers]["find_register_writers"](ref)
        assert res["declared_documents"] == _DECLARED_EXPECTED[ref], res
        assert res["_meta"]["declared_source"] == ("index" if helpers == "idx" else "live"), res["_meta"]
        assert "declared_unreadable" not in res["_meta"], res["_meta"]

    def test_writers_unchanged(self, tree):
        """Обе версии: `writers`/`total_writers` — только запись, найденная в коде; объявленное в них не попадает."""
        module = f"Documents/ДокКод/{'Ext/' if tree['fmt'] == 'cf' else ''}ObjectModule.bsl"
        idx = tree["idx"]["find_register_writers"]("РегА")
        assert idx["writers"] == [{"document": "ДокКод", "source": "code", "file": module}], idx
        assert idx["total_writers"] == 1 and idx["total_documents_scanned"] is None, idx
        live = tree["live"]["find_register_writers"]("РегА")
        assert live["writers"] == [{"document": "ДокКод", "file": module, "lines": [2]}], live
        assert live["total_writers"] == 1, live
        for helpers in ("idx", "live"):
            res = tree[helpers]["find_register_writers"]("РегистрСведений.РегС")
            assert res["writers"] == [] and res["total_writers"] == 0, res

    @pytest.mark.parametrize("helpers", ["idx", "live"])
    def test_hint_routes_to_declared_when_code_is_silent(self, tree, helpers):
        """Записи в коде нет, а документ регистр объявил: подсказка называет список и исполнимый маршрут."""
        res = tree[helpers]["find_register_writers"]("РегистрСведений.РегС")
        assert "у документов: 1 — declared_documents" in res["hint"], res["hint"]
        assert "find_register_movements('ДокОбщий')" in res["hint"], res["hint"]

    @pytest.mark.parametrize("helpers", ["idx", "live"])
    def test_hint_counts_code_confirmed(self, tree, helpers):
        res = tree[helpers]["find_register_writers"]("РегА")
        assert "у документов: 2 — declared_documents" in res["hint"], res["hint"]
        assert "в коде запись найдена у 1" in res["hint"], res["hint"]

    @pytest.mark.parametrize("helpers", ["idx", "live"])
    def test_hint_when_nobody_declares(self, tree, helpers):
        res = tree[helpers]["find_register_writers"]("РегНикто")
        assert "Ни один документ не объявил регистр в движениях" in res["hint"], res["hint"]

    @pytest.mark.parametrize(
        "form",
        ["РегистрНакопления.РегА", "AccumulationRegisters/РегА", "регистрнакопления.РегА", "AccumulationRegister.РегА"],
    )
    def test_reference_forms_same_answer(self, tree, form):
        for helpers in ("idx", "live"):
            assert tree[helpers]["find_register_writers"](form)["declared_documents"] == ["ДокКод", "ДокОбщий"], form

    def test_index_older_than_v16_answers_live(self, tree, monkeypatch):
        """Индекс до v16 (таблицы объявленного состава нет): тот же список живым обходом описателей."""
        monkeypatch.setattr(IndexReader, "has_declared_composition", property(lambda self: False))
        res = tree["idx"]["find_register_writers"]("РегА")
        assert res["declared_documents"] == ["ДокКод", "ДокОбщий"], res
        assert res["_meta"]["declared_source"] == "live", res["_meta"]

    def test_unavailable_channel_answers_live(self, tree, monkeypatch):
        """Канал объявленного состава недоступен (`None` ридера): тот же список живым обходом."""
        monkeypatch.setattr(tree["reader"], "get_declaring_documents", lambda name: None)
        res = tree["idx"]["find_register_writers"]("РегистрСведений.РегС")
        assert res["declared_documents"] == ["ДокОбщий"] and res["_meta"]["declared_source"] == "live", res

    def test_unreadable_descriptor_named(self, tree, monkeypatch):
        """Живьём: нечитаемый описатель документа — в `_meta.declared_unreadable` и в подсказке, а не тихий пропуск."""
        target = "ДокОбщий.xml" if tree["fmt"] == "cf" else "ДокОбщий.mdo"
        real = Path.read_text

        def fake(self, *args, **kwargs):
            if self.name == target:
                raise OSError("нет доступа")
            return real(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", fake)
        res = tree["live"]["find_register_writers"]("РегистрСведений.РегС")
        assert res["declared_documents"] == [], res
        assert [p for p in res["_meta"]["declared_unreadable"] if p.endswith(target)], res["_meta"]
        assert "описателей документов не прочитано: 1" in res["hint"], res["hint"]
        assert "Ни один документ не объявил" not in res["hint"], res["hint"]

    def test_wrong_case_name_consistent_within_route(self, tree):
        """Имя кириллицей в другом регистре: `declared_documents` сравнивает имя, как `writers` своего маршрута, —
        внутри ответа расхождения нет; на индексе (сравнение кириллицы с учётом регистра) подсказка называет
        написание с диска вместо ложного «никто не объявил»."""
        live = tree["live"]["find_register_writers"]("рега")
        assert live["writers"] and live["declared_documents"] == ["ДокКод", "ДокОбщий"], live
        idx = tree["idx"]["find_register_writers"]("рега")
        assert idx["writers"] == [] and idx["declared_documents"] == [], idx
        assert "find_register_writers('РегА')" in idx["hint"], idx["hint"]
        assert "Ни один документ не объявил" not in idx["hint"], idx["hint"]

    @pytest.mark.parametrize("helpers", ["idx", "live"])
    def test_latin_name_other_case_nobody_declares(self, tree, helpers):
        """Латиницу `COLLATE NOCASE` складывает сам: имя в другом регистре без объявивших — честное «никто не
        объявил», а не совет повторить с написанием с диска (повтор дал бы тот же ноль)."""
        res = tree[helpers]["find_register_writers"]("reglatin")
        assert res["writers"] == [] and res["declared_documents"] == [], res
        assert "Ни один документ не объявил регистр в движениях" in res["hint"], res["hint"]
        assert "повтори" not in res["hint"], res["hint"]

    def test_latin_name_other_case_writer_found_not_called_empty(self, tree):
        """На индексе латиница в другом регистре уже нашла писателя (`manager_code`, `COLLATE NOCASE`), а объявивших нет:
        подсказка объясняет найденную запись, а не называет `writers` пустым с советом повторить (повтор дал бы то же)."""
        res = tree["idx"]["find_register_writers"]("regsolo")
        assert [(w["document"], w["source"]) for w in res["writers"]] == [("ДокМенеджер", "manager_code")], res
        assert res["declared_documents"] == [], res
        assert "Ни один документ не объявил регистр в движениях" in res["hint"], res["hint"]
        assert "строки writers" in res["hint"], res["hint"]
        assert "пусты" not in res["hint"] and "повтори" not in res["hint"], res["hint"]

    def test_signature_recipe_and_docstring_name_the_key(self, tree):
        from rlm_tools_bsl.bsl_helpers import build_helper_metadata_snapshot

        snap = build_helper_metadata_snapshot()["find_register_writers"]
        assert "declared_documents" in snap["sig"] and "declared_documents" in snap["recipe"], snap
        doc = tree["idx"]["find_register_writers"].__doc__ or ""
        assert "declared_documents" in doc and "declared_source" in doc and "declared_unreadable" in doc, doc
