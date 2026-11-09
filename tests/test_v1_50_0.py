"""v1.50.0 — ссылка ведёт к объекту.

Релиз с бампом поколения индекса (BUILDER_VERSION 18 → 19) плюс read-time. Приватные фикстуры других файлов не
импортируются (каталог tests не пакет) — деревья собираются здесь же, по образцу ``tests/test_v1_47_0.py`` и
``tests/test_v1_49_0.py``.

Тесты сохранения поведения («…_unchanged», границы) сравнивают ответ с ЭТАЛОНОМ кода HEAD `90cc326`, вписанным
литералом: эталон снят кодом выгрузки ``git archive 90cc326 src`` на той же фикстуре (без полей времени).
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import textwrap
from pathlib import Path
from typing import Callable
from xml.sax.saxutils import escape as _xml_escape

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


# ═══════════════ Задача 0 — копии помощников (`tests/test_v1_49_0.py`, `tests/test_v1_47_0.py`) ═══════════════


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


_CF_MD_NS = (
    'xmlns="http://v8.1c.ru/8.3/MDClasses" xmlns:cfg="http://v8.1c.ru/8.1/data/enterprise/current-config" '
    'xmlns:v8="http://v8.1c.ru/8.1/data/core" xmlns:xr="http://v8.1c.ru/8.3/xcf/readable" '
    'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'
)
# Пространства `xsi` и `core` — для `xsi:type` значения заполнения (`core:ReferenceValue`), как в выгрузках EDT.
_EDT_MD_NS = (
    'xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
    'xmlns:core="http://g5.1c.ru/v8/dt/mcore"'
)
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


def _cf_syn(text: str) -> str:
    return f"<Synonym><v8:item><v8:lang>ru</v8:lang><v8:content>{text}</v8:content></v8:item></Synonym>"


def _edt_syn(text: str) -> str:
    return f"<synonym><key>ru</key><value>{text}</value></synonym>"


# Каталог категории → тег описателя (для `_objects_tree`).
_KIND_OF = {
    "Documents": "Document",
    "Catalogs": "Catalog",
    "CommonModules": "CommonModule",
    "Roles": "Role",
    "CommonPictures": "CommonPicture",
}


def _edt_rel(cf_rel: str) -> str:
    """Путь файла объекта в раскладке EDT по пути CF (относительно каталога объекта): модули без ``Ext``,
    права роли — ``Rights.rights``."""
    if cf_rel == "Ext/Rights.xml":
        return "Rights.rights"
    return cf_rel[len("Ext/") :] if cf_rel.startswith("Ext/") else cf_rel


def _objects_tree(
    root: Path,
    fmt: str,
    objects: list[tuple[str, str, dict, dict]],
    descriptor: str | None = None,
    eol: str = "\n",
) -> Path:
    """Объекты в раскладке CF/EDT: ``(каталог категории, имя, {путь CF внутри объекта: текст | (cf, edt)},
    доп. XML)``; доп. XML — ``{'cf_props', 'cf_children', 'edt'}`` (любые ключи необязательны). ``descriptor`` —
    описатель конфигурации (по умолчанию — основной); ``eol`` — окончание строк модулей ``.bsl``."""
    if fmt == "cf":
        _write(root / "Configuration.xml", descriptor or CF_DESCRIPTOR)
    else:
        _write(root / "Configuration" / "Configuration.mdo", descriptor or EDT_DESCRIPTOR)
    for category, name, files, extra in objects:
        kind = _KIND_OF[category]
        *parents, own = name.split("/")
        obj_dir = root / category
        for parent in parents:
            obj_dir = obj_dir / parent / category
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
            if body is not None:
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
    (bulk-fallback); ``check_route=False`` — маршрут не утверждается. Возвращает путь БД после ``update``."""
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


def _rights(objects: dict[str, list[str] | dict[str, bool]], flag: bool = False) -> str:
    """Файл прав роли (одна форма у CF ``Ext/Rights.xml`` и EDT ``Rights.rights``). Права объекта — список (все
    ``true``, как в копии 1.49.0) либо словарь ``{право: bool}`` (``false`` — запрет)."""

    def _items(rights) -> str:
        pairs = rights.items() if isinstance(rights, dict) else ((r, True) for r in rights)
        return "".join(
            f"<right><name>{r}</name><value>{'true' if value else 'false'}</value></right>" for r, value in pairs
        )

    body = "".join(f"<object><name>{obj}</name>{_items(rights)}</object>" for obj, rights in objects.items())
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n<Rights xmlns="http://v8.1c.ru/8.2/roles">'
        f"<setForNewObjects>{'true' if flag else 'false'}</setForNewObjects>"
        f"<setForAttributesByDefault>true</setForAttributesByDefault>{body}</Rights>\n"
    )


# 21 голова вне графа ссылок до v19 (Д7): семь категорий `METADATA_CATEGORIES` без канонической головы и 14
# каталогов без модулей — литерал (тест вывода `_GRAPH_HEADS_SINCE_V19` сверяет с ним).
_D7_HEADS = frozenset(
    {
        "CommonTemplate",
        "XDTOPackage",
        "WebService",
        "HTTPService",
        "FilterCriterion",
        "SettingsStorage",
        "ExternalDataSource",
        "Bot",
        "CommandGroup",
        "CommonAttribute",
        "CommonPicture",
        "DocumentNumerator",
        "FunctionalOptionsParameter",
        "IntegrationService",
        "Interface",
        "Language",
        "Sequence",
        "SessionParameter",
        "StyleItem",
        "Style",
        "WSReference",
    }
)

_V18_OVERRIDE_COLUMNS = {"form_item", "form_event"}


def _downgrade_to_v17(db_path) -> None:
    """Индекс прежнего поколения: ``builder_version``/``version`` = 17 и ``extension_overrides`` без колонок v18
    (копия из ``tests/test_v1_49_0.py``)."""
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


def _downgrade_to_v18(db_path) -> None:
    """Копия индекса в содержимое прежнего поколения: ``version``/``builder_version`` = 18 и удаление строк, которых
    v18 не пишет: видов ``common_attribute_content`` и ``template_query``, строк ``linked_object`` общих реквизитов и
    строк с головой ``ref_object`` (до первой точки) из ``_D7_HEADS``. Одного номера мало: ридер фильтрует цель и вид,
    а не поколение. Строки прав и состава ФО с путём члена эмуляция не восстанавливает (гейтам они не нужны)."""
    con = sqlite3.connect(str(db_path))
    try:
        con.execute("UPDATE index_meta SET value='18' WHERE key IN ('version', 'builder_version')")
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "metadata_references" in tables:
            con.execute(
                "DELETE FROM metadata_references WHERE ref_kind IN ('common_attribute_content', 'template_query')"
            )
            con.execute(
                "DELETE FROM metadata_references WHERE ref_kind = 'linked_object' AND used_in LIKE 'CommonAttribute.%'"
            )
            doomed = [
                (rowid,)
                for rowid, ref in con.execute("SELECT rowid, ref_object FROM metadata_references")
                if (ref or "").split(".", 1)[0] in _D7_HEADS
            ]
            con.executemany("DELETE FROM metadata_references WHERE rowid = ?", doomed)
        con.commit()
    finally:
        con.close()


def _v18_reader(world, dst_dir: Path) -> IndexReader:
    """Ридер на копии индекса мира, понижённой до v18 (сам мир не трогается)."""
    copy = _db_copy(world["db"], dst_dir / "bsl_index_v18.db")
    _downgrade_to_v18(copy)
    return IndexReader(copy)


# ═══════════════ Задача 0 — мир релиза `world50` ═══════════════

PROC = "Процедура П() Экспорт\nКонецПроцедуры\n"
_JOB_MODULE = (
    "Процедура Выполнить() Экспорт\nКонецПроцедуры\n\nПроцедура ПередЗаписью(Источник, Отказ) Экспорт\nКонецПроцедуры\n"
)
_DOC_OBJECT_MODULE = "Процедура ПередЗаписью(Отказ)\nКонецПроцедуры\n"
_EXT_DOC_MODULE = '&После("ПередЗаписью")\nПроцедура Расш1_ПередЗаписью(Отказ)\nКонецПроцедуры\n'
BALLAST = ("Балласт1", "Балласт2", "Балласт3")
_UUID_ITEM = "0b5bb3a4-1c2d-4e5f-8a9b-0c1d2e3f4a5b"

# Состав подсистемы `Продажи`: документ и объекты 21 головы вне прежнего графа, один UUID-элемент.
_SALES_CONTENT = (
    "Document.Док",
    "CommonPicture.Картинка",
    "StyleItem.ЭлементСтиля1",
    "XDTOPackage.Пакет",
    "CommonTemplate.ОбщСКД",
    "SessionParameter.ТекПольз",
    "WebService.ВС",
    "HTTPService.API",
    "FilterCriterion.Крит",
    "CommonAttribute.ОбщРекв",
    "Style.Основной",
    _UUID_ITEM,
)
_FO_MEMBERS = (
    "Document.Док.Attribute.Сумма",
    "Document.Док.TabularSection.Товары.Attribute.Количество",
    "Document.Док.Command.Печать",
    "Subsystem.Продажи.Subsystem.Подчиненная",
    "CommonAttribute.ОбщРекв",
    "FilterCriterion.Крит",
)

# ── макеты СКД: тексты запросов (сырые; в XML — с экранированием `&`, `<`, `>`) ──
_Q_SALES = """\
ВЫБРАТЬ
	Д.Ссылка КАК Ссылка,
	ВЫРАЗИТЬ(Д.Основание КАК Документ.Док) КАК Основание,
	"Документ.Скрытый2" КАК Вид // Справочник.Скрытый
ИЗ
	Документ.Док КАК Д
		ЛЕВОЕ СОЕДИНЕНИЕ РегистрНакопления.Рег.Остатки(&Дата) КАК О
		ПО ИСТИНА
{ГДЕ
	Д.Дата МЕЖДУ &Начало И &Конец}"""
_Q_CATALOG = "ВЫБРАТЬ\n\tС.Ссылка КАК Ссылка\nИЗ\n\tСправочник.Спр КАК С"
_Q_SALES_EN = "SELECT\n\tД.Ссылка AS Ссылка\nFROM\n\tDocument.ДОК AS Д"
_Q_ENUM = "ВЫБРАТЬ\n\tП.Ссылка КАК Ссылка\nИЗ\n\tПеречисление.Статусы КАК П\nГДЕ\n\tП.Порядок < 10"
_Q_CATALOG_EN = "SELECT\n\tС.Ссылка AS Ссылка\nFROM\n\tCatalog.Спр AS С"

_DCS_NS = (
    'xmlns="http://v8.1c.ru/8.1/data-composition-system/schema" '
    'xmlns:dcscom="http://v8.1c.ru/8.1/data-composition-system/common" '
    'xmlns:dcscor="http://v8.1c.ru/8.1/data-composition-system/core" '
    'xmlns:v8="http://v8.1c.ru/8.1/data/core" xmlns:xs="http://www.w3.org/2001/XMLSchema" '
    'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'
)


def _dcs_query(name: str, query: str, tag: str = "dataSet") -> str:
    """Набор-запрос СКД (``tag='item'`` — набор внутри объединения, как в выгрузках)."""
    return (
        f'<{tag} xsi:type="DataSetQuery"><name>{name}</name>'
        f'<field xsi:type="DataSetFieldField"><dataPath>Ссылка</dataPath><field>Ссылка</field></field>'
        f"<dataSource>ИсточникДанных1</dataSource><query>{_xml_escape(query)}</query></{tag}>"
    )


def _dcs_union(name: str, *items: str) -> str:
    return f'<dataSet xsi:type="DataSetUnion"><name>{name}</name>{"".join(items)}</dataSet>'


def _dcs_object(name: str) -> str:
    return (
        f'<dataSet xsi:type="DataSetObject"><name>{name}</name><dataSource>ИсточникДанных1</dataSource>'
        "<objectName>Данные</objectName></dataSet>"
    )


def _dcs(*datasets: str) -> str:
    """Содержимое макета СКД (CF ``Ext/Template.xml``, EDT ``Template.dcs`` — одна форма)."""
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>\n<DataCompositionSchema {_DCS_NS}>\n'
        "\t<dataSource><name>ИсточникДанных1</name><dataSourceType>Local</dataSourceType></dataSource>\n\t"
        + "\n\t".join(datasets)
        + "\n</DataCompositionSchema>\n"
    )


_DCS_MAIN = _dcs(
    _dcs_query("Продажи", _Q_SALES),
    _dcs_union("Объединение", _dcs_query("Справочники", _Q_CATALOG, tag="item")),
    _dcs_query("ПродажиEN", _Q_SALES_EN),
)
_DCS_DOC = _dcs(_dcs_query("НаборДанных1", _Q_CATALOG))
_DCS_PROC = _dcs(_dcs_query("Статусы", _Q_ENUM), _dcs_query("СпрEN", _Q_CATALOG_EN))
_DCS_NO_QUERIES = _dcs(_dcs_object("Внешние"))
_DCS_COMMON = _dcs(_dcs_query("НаборДанных1", _Q_CATALOG))
_SPREADSHEET = '<?xml version="1.0" encoding="UTF-8"?>\n<document xmlns="http://v8.1c.ru/8.2/data/spreadsheet"/>\n'


def _cf_attr(name: str, cf_type: str, extra: str = "") -> str:
    return f"<Attribute><Properties><Name>{name}</Name><Type><v8:Type>{cf_type}</v8:Type></Type>{extra}</Properties></Attribute>"


def _edt_attr(name: str, edt_type: str, extra: str = "") -> str:
    return f"<attributes><name>{name}</name><type><types>{edt_type}</types></type>{extra}</attributes>"


def _cf_list_form(main_table: str) -> str:
    """Форма списка CF: динамический список с основной таблицей (как `tests/test_v1_47_0.py:477–486`)."""
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>\n<Form {_CF_FORM_NS} version="2.20"><Attributes>'
        '<Attribute name="Список" id="1"><Type><v8:Type>cfg:DynamicList</v8:Type></Type>'
        '<MainAttribute>true</MainAttribute><Settings xsi:type="DynamicList"><ManualQuery>false</ManualQuery>'
        f"<MainTable>{main_table}</MainTable></Settings></Attribute></Attributes></Form>"
    )


def _edt_list_form(main_table: str) -> str:
    """Форма списка EDT: динамический список с основной таблицей (как `tests/test_v1_47_0.py:552–561`)."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n<form:Form xmlns:form="http://g5.1c.ru/v8/dt/form" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"><attributes><name>Список</name>'
        "<valueType><types>DynamicList</types></valueType><main>true</main>"
        f'<extInfo xsi:type="form:DynamicListExtInfo"><mainTable>{main_table}</mainTable></extInfo>'
        "</attributes></form:Form>"
    )


def _cf_common_attr(name: str, auto_use: str | None, items, separation: dict[str, str] | None = None) -> str:
    """Описатель общего реквизита CF: ``items`` — ``(объект, Use|DontUse|Auto, условное разделение элемента)``."""
    content = "".join(
        f"<xr:Item><xr:Metadata>{obj}</xr:Metadata><xr:Use>{use}</xr:Use>"
        + (f"<xr:ConditionalSeparation>{cond}</xr:ConditionalSeparation>" if cond else "<xr:ConditionalSeparation/>")
        + "</xr:Item>"
        for obj, use, cond in items
    )
    props = f"<Type><v8:Type>xs:string</v8:Type></Type><Content>{content}</Content>"
    if auto_use is not None:
        props += f"<AutoUse>{auto_use}</AutoUse>"
    if separation:
        props += "<DataSeparation>Separate</DataSeparation>" + "".join(
            f"<{tag}>{value}</{tag}>" for tag, value in separation.items()
        )
    else:
        props += "<DataSeparation>DontUse</DataSeparation>"
    return _cf_md("CommonAttribute", name, props=props)


def _edt_common_attr(name: str, auto_use: str | None, items, separation: dict[str, str] | None = None) -> str:
    """Описатель общего реквизита EDT: ``items`` — ``(объект, use | None — узла нет)``; условного разделения
    элемента в EDT нет (в корпусе непустой формы не встречается)."""
    body = "<type><types>String</types></type>"
    body += "".join(
        f"<content><metadata>{obj}</metadata>" + (f"<use>{use}</use>" if use else "") + "</content>"
        for obj, use in items
    )
    if auto_use is not None:
        body += f"<autoUse>{auto_use}</autoUse>"
    if separation:
        body += "<dataSeparation>Separate</dataSeparation>" + "".join(
            f"<{tag[0].lower()}{tag[1:]}>{value}</{tag[0].lower()}{tag[1:]}>" for tag, value in separation.items()
        )
    else:
        body += "<dataSeparation>DontUse</dataSeparation>"
    return _edt_md("CommonAttribute", name, body)


_SEPARATION = {
    "DataSeparationValue": "SessionParameter.ОбластьЗначение",
    "DataSeparationUse": "SessionParameter.ОбластьИспользование",
    "ConditionalSeparation": "Constant.ИспользоватьРазделение",
}


def _world50_files(fmt: str) -> dict[str, str]:
    """Дерево мира релиза «путь от корня основной → текст» (у CF ещё `../cfe/Расш1/…`)."""
    cf = fmt == "cf"
    files: dict[str, str] = {}

    def obj(category: str, name: str, kind: str, *, cf_props: str = "", cf_children: str = "", edt: str = ""):
        if cf:
            files[f"{category}/{name}.xml"] = _cf_md(kind, name, cf_children, cf_props)
        else:
            files[f"{category}/{name}/{name}.mdo"] = _edt_md(kind, name, edt)

    def put(category: str, name: str, cf_rel: str, edt_rel: str | None, text: str):
        """Файл внутри каталога объекта: ``cf_rel`` — путь CF, ``edt_rel`` — путь EDT (``None`` — нет в EDT)."""
        rel = cf_rel if cf else edt_rel
        if rel is not None:
            files[f"{category}/{name}/{rel}"] = text

    def module(category: str, name: str, file: str, text: str):
        put(category, name, f"Ext/{file}", file, text)

    def template(category: str, owner: str, name: str, ttype: str, content: str):
        """Макет объекта: CF — описатель `Templates/<Имя>.xml` и `Templates/<Имя>/Ext/Template.xml`; EDT — файл
        содержимого `Templates/<Имя>/Template.dcs` (СКД) либо `Template.mxlx` (блок `<templates>` — у владельца)."""
        if cf:
            files[f"{category}/{owner}/Templates/{name}.xml"] = _cf_md(
                "Template", name, props=f"<TemplateType>{ttype}</TemplateType>"
            )
            files[f"{category}/{owner}/Templates/{name}/Ext/Template.xml"] = content
        else:
            ext = "dcs" if ttype == "DataCompositionSchema" else "mxlx"
            files[f"{category}/{owner}/Templates/{name}/Template.{ext}"] = content

    def edt_templates(*pairs: tuple[str, str]) -> str:
        return "".join(f"<templates><name>{n}</name><templateType>{t}</templateType></templates>" for n, t in pairs)

    def common_template(name: str, ttype: str, content: str):
        if cf:
            files[f"CommonTemplates/{name}.xml"] = _cf_md(
                "CommonTemplate", name, props=f"<TemplateType>{ttype}</TemplateType>"
            )
            files[f"CommonTemplates/{name}/Ext/Template.xml"] = content
        else:
            files[f"CommonTemplates/{name}/{name}.mdo"] = _edt_md(
                "CommonTemplate", name, f"<templateType>{ttype}</templateType>"
            )
            ext = "dcs" if ttype == "DataCompositionSchema" else "mxlx"
            files[f"CommonTemplates/{name}/Template.{ext}"] = content

    def rights(role: str, objects: dict):
        put("Roles", role, "Ext/Rights.xml", "Rights.rights", _rights(objects))
        obj("Roles", role, "Role")

    if cf:
        files["Configuration.xml"] = CF_DESCRIPTOR
    else:
        files["Configuration/Configuration.mdo"] = EDT_DESCRIPTOR

    # ── документ `Док`: синоним «Документ продажи», реквизиты, ТЧ, команда, макет СКД ──
    fill_cf = '<FillValue xsi:type="xr:DesignTimeRef">Enum.Статусы.EnumValue.Новый</FillValue>'
    fill_edt = '<fillValue xsi:type="core:ReferenceValue"><value>Enum.Статусы.EnumValue.Новый</value></fillValue>'
    obj(
        "Documents",
        "Док",
        "Document",
        cf_props=_cf_syn("Документ продажи") + "<Posting>Allow</Posting>",
        cf_children="<ChildObjects>"
        + _cf_attr("Сумма", "xs:decimal")
        + _cf_attr("Статус", "cfg:EnumRef.Статусы", fill_cf)
        + "<TabularSection><Properties><Name>Товары</Name></Properties><ChildObjects>"
        + _cf_attr("Количество", "xs:decimal")
        + "</ChildObjects></TabularSection>"
        + "<Command><Properties><Name>Печать</Name></Properties></Command>"
        + "<Template>СхемаДок</Template></ChildObjects>",
        edt=_edt_syn("Документ продажи")
        + "<posting>Allow</posting>"
        + _edt_attr("Сумма", "Number")
        + _edt_attr("Статус", "EnumRef.Статусы", fill_edt)
        + "<tabularSections><name>Товары</name>"
        + _edt_attr("Количество", "Number")
        + "</tabularSections><commands><name>Печать</name></commands>"
        + edt_templates(("СхемаДок", "DataCompositionSchema")),
    )
    module("Documents", "Док", "ObjectModule.bsl", _DOC_OBJECT_MODULE)
    template("Documents", "Док", "СхемаДок", "DataCompositionSchema", _DCS_DOC)

    # ── справочники, документ, перечисление, регистр, константы, отчёт `Роль`, общий модуль `Задание` ──
    obj("Catalogs", "Спр", "Catalog", cf_props=_cf_syn("Справочник тест"), edt=_edt_syn("Справочник тест"))
    module("Catalogs", "Спр", "ObjectModule.bsl", PROC)
    obj("Catalogs", "Скрытый", "Catalog", cf_props=_cf_syn("Спр"), edt=_edt_syn("Спр"))
    obj("Documents", "Скрытый2", "Document", cf_props="<Posting>Allow</Posting>", edt="<posting>Allow</posting>")
    obj(
        "Enums",
        "Статусы",
        "Enum",
        cf_props=_cf_syn("Состояния заявки"),
        cf_children="<ChildObjects>"
        + "".join(f"<EnumValue><Properties><Name>{v}</Name></Properties></EnumValue>" for v in ("Новый", "Закрыт"))
        + "</ChildObjects>",
        edt=_edt_syn("Состояния заявки")
        + "".join(f"<enumValues><name>{v}</name></enumValues>" for v in ("Новый", "Закрыт")),
    )
    obj("AccumulationRegisters", "Рег", "AccumulationRegister")
    for const in ("Конст", "ИспользоватьРазделение"):
        obj(
            "Constants",
            const,
            "Constant",
            cf_props="<Type><v8:Type>xs:boolean</v8:Type></Type>",
            edt="<type><types>Boolean</types></type>",
        )
    obj(
        "DefinedTypes",
        "ОпрТип",
        "DefinedType",
        cf_props="<Type><v8:Type>cfg:CatalogRef.Спр</v8:Type></Type>",
        edt="<type><types>CatalogRef.Спр</types></type>",
    )
    obj("Reports", "Роль", "Report")
    obj("CommonModules", "Задание", "CommonModule", cf_props="<Server>true</Server>", edt="<server>true</server>")
    module("CommonModules", "Задание", "Module.bsl", _JOB_MODULE)

    # ── отчёт `ОтчетСКД`: макет СКД `ОсновнаяСхема` и табличный `Печать` ──
    obj(
        "Reports",
        "ОтчетСКД",
        "Report",
        cf_children="<ChildObjects><Template>ОсновнаяСхема</Template><Template>Печать</Template></ChildObjects>",
        edt=edt_templates(("ОсновнаяСхема", "DataCompositionSchema"), ("Печать", "SpreadsheetDocument")),
    )
    template("Reports", "ОтчетСКД", "ОсновнаяСхема", "DataCompositionSchema", _DCS_MAIN)
    template("Reports", "ОтчетСКД", "Печать", "SpreadsheetDocument", _SPREADSHEET)

    # ── обработка `ОбрСКД`: макеты СКД `Схема` и `БезЗапросов`; общие макеты ──
    obj(
        "DataProcessors",
        "ОбрСКД",
        "DataProcessor",
        cf_children="<ChildObjects><Template>Схема</Template><Template>БезЗапросов</Template></ChildObjects>",
        edt=edt_templates(("Схема", "DataCompositionSchema"), ("БезЗапросов", "DataCompositionSchema")),
    )
    template("DataProcessors", "ОбрСКД", "Схема", "DataCompositionSchema", _DCS_PROC)
    template("DataProcessors", "ОбрСКД", "БезЗапросов", "DataCompositionSchema", _DCS_NO_QUERIES)
    common_template("ОбщСКД", "DataCompositionSchema", _DCS_COMMON)
    common_template("ОбщТабл", "SpreadsheetDocument", _SPREADSHEET)

    # ── подсистема `Продажи` и вложенная `Подчиненная` ──
    if cf:
        items = "".join(f'<xr:Item xsi:type="xr:MDObjectRef">{ref}</xr:Item>' for ref in _SALES_CONTENT)
        files["Subsystems/Продажи.xml"] = _cf_md(
            "Subsystem",
            "Продажи",
            "<ChildObjects><Subsystem>Подчиненная</Subsystem></ChildObjects>",
            f"<Content>{items}</Content>",
        )
        files["Subsystems/Продажи/Subsystems/Подчиненная.xml"] = _cf_md(
            "Subsystem",
            "Подчиненная",
            props='<Content><xr:Item xsi:type="xr:MDObjectRef">Catalog.Спр</xr:Item></Content>',
        )
    else:
        content = "".join(f"<content>{ref}</content>" for ref in _SALES_CONTENT)
        files["Subsystems/Продажи/Продажи.mdo"] = _edt_md(
            "Subsystem", "Продажи", content + "<subsystems>Подчиненная</subsystems>"
        )
        files["Subsystems/Продажи/Subsystems/Подчиненная/Подчиненная.mdo"] = _edt_md(
            "Subsystem", "Подчиненная", "<content>Catalog.Спр</content>"
        )

    # ── объекты 21 головы вне прежнего графа, задание, подписка, язык ──
    obj("CommonPictures", "Картинка", "CommonPicture")
    obj("StyleItems", "ЭлементСтиля1", "StyleItem")
    obj("Styles", "Основной", "Style")
    obj(
        "XDTOPackages",
        "Пакет",
        "XDTOPackage",
        cf_props="<Namespace>urn:test</Namespace>",
        edt="<namespace>urn:test</namespace>",
    )
    for param in ("ТекПольз", "ОбластьЗначение", "ОбластьИспользование"):
        obj("SessionParameters", param, "SessionParameter")
    obj(
        "WebServices", "ВС", "WebService", cf_props="<Namespace>urn:ws</Namespace>", edt="<namespace>urn:ws</namespace>"
    )
    obj("HTTPServices", "API", "HTTPService", cf_props="<RootURL>api</RootURL>", edt="<rootURL>api</rootURL>")
    obj(
        "FilterCriteria",
        "Крит",
        "FilterCriterion",
        cf_props="<Type><v8:Type>cfg:DocumentRef.Док</v8:Type></Type>",
        cf_children="<ChildObjects><Form>ФормаСписка</Form></ChildObjects>",
        edt="<type><types>DocumentRef.Док</types></type><forms><name>ФормаСписка</name></forms>",
    )
    put(
        "FilterCriteria",
        "Крит",
        "Forms/ФормаСписка/Ext/Form.xml",
        "Forms/ФормаСписка/Form.form",
        _cf_list_form("FilterCriterion.Крит") if cf else _edt_list_form("FilterCriterion.Крит"),
    )
    obj(
        "ScheduledJobs",
        "Задание",
        "ScheduledJob",
        cf_props="<MethodName>CommonModule.Задание.Выполнить</MethodName><Use>true</Use>",
        edt="<methodName>CommonModule.Задание.Выполнить</methodName><use>true</use>",
    )
    obj(
        "EventSubscriptions",
        "Подписка",
        "EventSubscription",
        cf_props="<Source><v8:Type>cfg:DocumentObject.Док</v8:Type></Source><Event>BeforeWrite</Event>"
        "<Handler>CommonModule.Задание.ПередЗаписью</Handler>",
        edt="<source><types>DocumentObject.Док</types></source><event>BeforeWrite</event>"
        "<handler>CommonModule.Задание.ПередЗаписью</handler>",
    )
    obj(
        "Languages",
        "Русский",
        "Language",
        cf_props="<LanguageCode>ru</LanguageCode>",
        edt="<languageCode>ru</languageCode>",
    )

    # ── функциональные опции ──
    def fo(name: str, content: tuple[str, ...], location: str = ""):
        cf_content = "".join(f"<xr:Object>{ref}</xr:Object>" for ref in content)
        obj(
            "FunctionalOptions",
            name,
            "FunctionalOption",
            cf_props=(f"<Location>{location}</Location>" if location else "") + f"<Content>{cf_content}</Content>",
            edt=(f"<location>{location}</location>" if location else "")
            + "".join(f"<content>{ref}</content>" for ref in content),
        )

    fo("Опция", ("Document.Док",))
    fo("ОпцияКонст", ("Constant.Конст",), "Constant.Конст")
    fo("ОпцияПоЧленам", _FO_MEMBERS, "Constant.Конст")

    # ── роли ──
    rights(
        "ПолныеПрава",
        {
            "Document.Док": ["Read"],
            "Subsystem.Продажи": ["View"],
            "SessionParameter.ТекПольз": ["Get"],
            "WebService.ВС": ["Use"],
            "Configuration.Конфигурация": ["Administration"],
        },
    )
    rights("ТолькоКоманды", {"Document.Док.Command.Печать": ["View"]})
    rights("ТолькоЗапреты", {"Document.Док": {"Read": False}, "Document.Док.Attribute.Сумма": {"View": False}})
    rights("ВложеннаяПодсистема", {"Subsystem.Продажи.Subsystem.Подчиненная": ["View"]})
    rights("ЗапретыСпр", {"Catalog.Скрытый": {"Read": False, "Insert": False, "Update": False, "Delete": False}})

    # ── общие реквизиты, параметры разделения ──
    if cf:
        files["CommonAttributes/ОбщРекв.xml"] = _cf_common_attr(
            "ОбщРекв", "DontUse", [("Document.Док", "Use", ""), ("Catalog.Спр", "DontUse", "")]
        )
        files["CommonAttributes/ОбластьДанных.xml"] = _cf_common_attr(
            "ОбластьДанных",
            "Use",
            [("Catalog.Спр", "DontUse", ""), ("Catalog.Скрытый", "DontUse", "Constant.Конст")],
            _SEPARATION,
        )
        files["CommonAttributes/АвтоРекв.xml"] = _cf_common_attr("АвтоРекв", "Use", [("Document.Док", "Auto", "")])
    else:
        files["CommonAttributes/ОбщРекв/ОбщРекв.mdo"] = _edt_common_attr(
            "ОбщРекв", "DontUse", [("Document.Док", "Use"), ("Catalog.Спр", "DontUse")]
        )
        files["CommonAttributes/ОбластьДанных/ОбластьДанных.mdo"] = _edt_common_attr(
            "ОбластьДанных", None, [("Catalog.Спр", "DontUse"), ("Catalog.Скрытый", "DontUse")], _SEPARATION
        )
        files["CommonAttributes/АвтоРекв/АвтоРекв.mdo"] = _edt_common_attr("АвтоРекв", "Use", [("Document.Док", None)])

    # ── балласт порога точечного пути: по три объекта в `Documents` и `Catalogs` ──
    for name in BALLAST:
        for category, kind in (("Documents", "Document"), ("Catalogs", "Catalog")):
            obj(
                category,
                name,
                kind,
                cf_children="<ChildObjects>" + _cf_attr("Поле", "xs:string") + "</ChildObjects>",
                edt=_edt_attr("Поле", "String"),
            )
            module(category, name, "ObjectModule.bsl", PROC)

    # ── расширение `Расш1` рядом (только CF): своя общая картинка и заимствованный документ `Док` ──
    if cf:
        ext = "../cfe/Расш1/"
        files[ext + "Configuration.xml"] = CF_EXT_DESCRIPTOR
        files[ext + "CommonPictures/РасшКартинка.xml"] = _cf_md("CommonPicture", "РасшКартинка")
        files[ext + "Documents/Док.xml"] = _cf_md("Document", "Док", props="<ObjectBelonging>Adopted</ObjectBelonging>")
        files[ext + "Documents/Док/Ext/ObjectModule.bsl"] = _EXT_DOC_MODULE
    return files


class World50(dict):
    """Мир релиза: ``files`` (дерево «путь → текст»), ``write`` (писатель дерева), ``root`` (корень основной),
    ``ext_root`` (корень `Расш1` у CF), ``fmt``, ``db`` (путь индекса), ``idx`` (хелперы на индексе), ``live``
    (хелперы без индекса), ``reader`` (ридер индекса)."""


def _world50_helpers(root: Path, ext_root: Path | None, reader: IndexReader | None):
    ext_kw: dict = {}
    if ext_root is not None:
        ext_kw = {"extension_paths": [str(ext_root)], "extension_name_by_root": {str(ext_root): "Расш1"}}
    return _bsl_for(
        root,
        reader,
        idx_zero_callers_authoritative=reader is not None,
        current_config_role="main",
        current_config_name="Тест",
        current_config_root=str(root),
        **ext_kw,
    )


def _build_world50(base: Path, fmt: str, files: dict[str, str] | None = None, **build_kw) -> World50:
    """Мир релиза в ``base``; ``files`` — своё дерево вместо стандартного, ``build_kw`` — опции сборки."""
    files = _world50_files(fmt) if files is None else files
    root = base / "cf"
    _write_tree(root, files)
    ext_root = base / "cfe" / "Расш1" if fmt == "cf" else None
    db = _build_index(root, base / "idx", build_calls=True, **build_kw)
    reader = IndexReader(db)
    return World50(
        files=files,
        write=_write_tree,
        root=root,
        ext_root=ext_root,
        fmt=fmt,
        db=str(db),
        reader=reader,
        idx=_world50_helpers(root, ext_root, reader),
        live=_world50_helpers(root, ext_root, None),
    )


@pytest.fixture(scope="module")
def worlds50(tmp_path_factory):
    """Оба мира (CF, EDT) — ``world50`` берёт свой отсюда."""
    built = {fmt: _build_world50(tmp_path_factory.mktemp(f"world50_{fmt}"), fmt) for fmt in ("cf", "edt")}
    yield built
    for world in built.values():
        world["reader"].close()


@pytest.fixture(scope="module", params=["cf", "edt"])
def world50(request, worlds50):
    return worlds50[request.param]


def _module_path(fmt: str, category: str, name: str, file: str) -> str:
    return f"{category}/{name}/{'Ext/' if fmt == 'cf' else ''}{file}"


def test_world50_builds_both_layouts(world50):
    paths = [m["path"] for m in world50["idx"]["find_module"]("Док")]
    assert _module_path(world50["fmt"], "Documents", "Док", "ObjectModule.bsl") in paths, paths
    assert Path(world50["db"]).is_file()
    if world50["fmt"] == "cf":
        ext = world50["idx"]["detect_extensions"]()
        assert [e["name"] for e in ext["nearby_extensions"]] == ["Расш1"], ext
    dcs = {
        name
        for (name,) in _rows(world50["db"], "SELECT name FROM templates WHERE template_type = 'DataCompositionSchema'")
    }
    assert {"ОсновнаяСхема", "СхемаДок", "Схема", "БезЗапросов", "ОбщСКД"} <= dcs, dcs
    con = sqlite3.connect(world50["db"])
    try:
        for category in ("Documents", "Catalogs"):
            assert BI._category_object_count(con, category) >= 3, category
    finally:
        con.close()


# ═══════════════ Задача 1 — поколение индекса v19 ═══════════════

_GEN_FILES = {
    "Configuration.xml": CF_DESCRIPTOR,
    "CommonModules/ОбщийМодульА.xml": _cf_md("CommonModule", "ОбщийМодульА"),
    "CommonModules/ОбщийМодульА/Ext/Module.bsl": "Функция Метод1() Экспорт\n    Возврат 1;\nКонецФункции\n",
    "Documents/Заказ.xml": _cf_md("Document", "Заказ"),
    "Documents/Заказ/Ext/ObjectModule.bsl": "Процедура П()\n    ОбщийМодульА.Метод1();\nКонецПроцедуры\n",
}


def _set_generation(db_path, version: str) -> None:
    con = sqlite3.connect(str(db_path))
    try:
        con.execute("UPDATE index_meta SET value=? WHERE key IN ('version', 'builder_version')", (version,))
        con.commit()
    finally:
        con.close()


class TestIndexGeneration19:
    def test_builder_version_is_19(self):
        assert BI.BUILDER_VERSION == 19

    def test_old_index_gaps_last_row_is_19(self):
        threshold, text = BI.OLD_INDEX_GAPS[-1]
        assert threshold == BI.BUILDER_VERSION == 19
        assert text.strip() and not text.rstrip().endswith(".")
        for word in ("общих картинок", "ФО", "общих реквизитов", "СКД"):
            assert word in text, word
        assert "ё" not in text

    def test_update_on_v18_index_forces_full_rebuild(self, tmp_path):
        base = tmp_path / "cf"
        _write_tree(base, _GEN_FILES)
        db = IndexBuilder().build(str(base), build_calls=True)
        _set_generation(db, "18")
        res = IndexBuilder().update(str(base))
        assert res["rebuild_reason"] == "schema upgrade v18->19", res
        assert _rows(db, "SELECT value FROM index_meta WHERE key IN ('version', 'builder_version')") == [
            ("19",),
            ("19",),
        ]

    def test_start_warns_on_v18_index(self, tmp_path):
        from rlm_tools_bsl.server import _rlm_end, _rlm_start

        base = tmp_path / "cf"
        _write_tree(base, _GEN_FILES)
        _set_generation(IndexBuilder().build(str(base), build_calls=True), "18")
        data = json.loads(_rlm_start(path=str(base), query="", domains=[]))
        try:
            assert data["index"]["loaded"] is True, data["index"]
            gaps = dict(BI.OLD_INDEX_GAPS)
            warns = [w for w in data["index"]["warnings"] if "сборщиком v18" in w]
            assert len(warns) == 1, data["index"]["warnings"]
            assert gaps[19] in warns[0]
            assert gaps[18] not in warns[0]  # у v18 недостача v18 уже закрыта
        finally:
            _rlm_end(data["session_id"])


# ═══════════════ Задача 2 — таблица видов и канонизатор ссылки на объект ═══════════════

# Каталоги вне `_CATEGORY_RU` — `_DESCRIPTOR_ONLY_CATEGORIES` замыкания хелперов (до 47 категорий существования).
_DESCRIPTOR_ONLY = (
    "Bots",
    "CommandGroups",
    "CommonAttributes",
    "CommonPictures",
    "DocumentNumerators",
    "FunctionalOptionsParameters",
    "IntegrationServices",
    "Interfaces",
    "Languages",
    "Sequences",
    "SessionParameters",
    "StyleItems",
    "Styles",
    "WSReferences",
)
# Прежние карты голов замыкания хелперов (HEAD `90cc326`, литералом: снаружи замыкания они не видны) — виды без
# ссылочного типа и определяемый тип (`_NONREF_KIND_HEADS`), 17 категорий 1.49.0 (`_DESCRIPTOR_KIND_HEADS`),
# четыре прочих (`_OTHER_CATEGORY_HEADS`). Вместе с `CATEGORY_TO_REF_HEAD` — значения `_head_of_category` HEAD.
_PREV_NONREF_HEADS = {
    "CommonModules": "CommonModule",
    "CommonForms": "CommonForm",
    "CommonCommands": "CommonCommand",
    "HTTPServices": "HTTPService",
    "WebServices": "WebService",
    "DocumentJournals": "DocumentJournal",
    "FilterCriteria": "FilterCriterion",
    "SettingsStorages": "SettingsStorage",
    "ExternalDataSources": "ExternalDataSource",
    "DefinedTypes": "DefinedType",
}
_PREV_DESCRIPTOR_HEADS = {
    "EventSubscriptions": "EventSubscription",
    "ScheduledJobs": "ScheduledJob",
    "FunctionalOptions": "FunctionalOption",
    "Bots": "Bot",
    "CommandGroups": "CommandGroup",
    "CommonAttributes": "CommonAttribute",
    "CommonPictures": "CommonPicture",
    "DocumentNumerators": "DocumentNumerator",
    "FunctionalOptionsParameters": "FunctionalOptionsParameter",
    "IntegrationServices": "IntegrationService",
    "Interfaces": "Interface",
    "Languages": "Language",
    "Sequences": "Sequence",
    "SessionParameters": "SessionParameter",
    "StyleItems": "StyleItem",
    "Styles": "Style",
    "WSReferences": "WSReference",
}
_PREV_OTHER_HEADS = {
    "Roles": "Role",
    "Subsystems": "Subsystem",
    "CommonTemplates": "CommonTemplate",
    "XDTOPackages": "XDTOPackage",
}
# Формы голов прежнего разбора видов без ссылочного типа (`_NONREF_KINDS` замыкания хелперов, HEAD).
_PREV_NONREF_FORMS = (
    "ОбщийМодуль",
    "ОбщиеМодули",
    "ОбщаяФорма",
    "ОбщиеФормы",
    "ОбщаяКоманда",
    "ОбщиеКоманды",
    "HTTPСервис",
    "HTTPСервисы",
    "WebСервис",
    "WebСервисы",
    "ЖурналДокументов",
    "ЖурналыДокументов",
    "КритерийОтбора",
    "КритерииОтбора",
    "ХранилищеНастроек",
    "ХранилищаНастроек",
    "ВнешнийИсточникДанных",
    "ВнешниеИсточникиДанных",
    "ОпределяемыйТип",
    "ОпределяемыеТипы",
)

_UUID = "0b5bb3a4-1c2d-4e5f-8a9b-0c1d2e3f4a5b"
_OBJECT_REF_TABLE = [
    ("Catalog.X", "Catalog.X"),
    ("CatalogRef.X", "Catalog.X"),
    ("catalog.X", "Catalog.X"),
    ("cfg:CatalogRef.X", "Catalog.X"),
    ("DocumentObject.X", "Document.X"),
    ("CommonPicture.X", "CommonPicture.X"),
    ("commonpicture.X", "CommonPicture.X"),
    ("Document.X.Attribute.Y", "Document.X"),
    ("Document.X.TabularSection.T.Attribute.Z", "Document.X"),
    ("Catalog.X.Command.K", "Catalog.X"),
    ("InformationRegister.X.Dimension.Y", "InformationRegister.X"),
    ("Task.X.AddressingAttribute.Y", "Task.X"),
    ("Document.X.StandardAttribute.Ref", "Document.X"),
    ("WebService.X.Operation.Y", "WebService.X"),
    ("ExternalDataSource.X.Table.T", "ExternalDataSource.X"),
    ("Subsystem.A.Subsystem.B", "Subsystem.B"),
    ("Subsystem.A.Subsystem.B.Subsystem.C", "Subsystem.C"),
    ("Subsystem.A", "Subsystem.A"),
    ("Configuration.Конф", ""),
    (_UUID, ""),
    ("Неизвестная.X", ""),
    ("Catalog.", ""),
    ("", ""),
]
# Выход `canonicalize_type_ref` HEAD на тех же входах — литералом (снят кодом `90cc326`).
_TYPE_REF_HEAD = {
    "Catalog.X": "Catalog.X",
    "CatalogRef.X": "Catalog.X",
    "catalog.X": "Catalog.X",
    "cfg:CatalogRef.X": "Catalog.X",
    "DocumentObject.X": "Document.X",
    "CommonPicture.X": "",
    "commonpicture.X": "",
    "Document.X.Attribute.Y": "Document.X.Attribute.Y",
    "Document.X.TabularSection.T.Attribute.Z": "Document.X.TabularSection.T.Attribute.Z",
    "Catalog.X.Command.K": "Catalog.X.Command.K",
    "InformationRegister.X.Dimension.Y": "InformationRegister.X.Dimension.Y",
    "Task.X.AddressingAttribute.Y": "Task.X.AddressingAttribute.Y",
    "Document.X.StandardAttribute.Ref": "Document.X.StandardAttribute.Ref",
    "WebService.X.Operation.Y": "",
    "ExternalDataSource.X.Table.T": "",
    "Subsystem.A.Subsystem.B": "Subsystem.A.Subsystem.B",
    "Subsystem.A.Subsystem.B.Subsystem.C": "Subsystem.A.Subsystem.B.Subsystem.C",
    "Subsystem.A": "Subsystem.A",
    "Configuration.Конф": "",
    _UUID: "",
    "Неизвестная.X": "",
    "Catalog.": "Catalog.",
    "": "",
    "Style.color": "",
    "Language.X": "",
}


class TestObjectKinds:
    def test_object_kind_heads_cover_existence_categories(self):
        from rlm_tools_bsl.bsl_xml_parsers import OBJECT_KIND_HEADS

        expected = {*BI._CATEGORY_RU, *_DESCRIPTOR_ONLY}
        assert len(expected) == 47
        assert set(OBJECT_KIND_HEADS) == expected

    def test_object_kind_heads_agree_with_previous_maps(self):
        from rlm_tools_bsl.bsl_xml_parsers import CATEGORY_TO_REF_HEAD, OBJECT_KIND_HEADS

        previous = {**CATEGORY_TO_REF_HEAD, **_PREV_NONREF_HEADS, **_PREV_DESCRIPTOR_HEADS, **_PREV_OTHER_HEADS}
        assert len(previous) == 47
        assert OBJECT_KIND_HEADS == previous

    def test_ru_forms_cover_categories_without_singular_head(self):
        from rlm_tools_bsl.bsl_xml_parsers import _RU_META_FORMS, CATEGORY_TO_REF_HEAD, OBJECT_KIND_RU_FORMS

        prior = {*CATEGORY_TO_REF_HEAD, *_PREV_NONREF_HEADS}
        assert len(prior) == 26
        assert set(OBJECT_KIND_RU_FORMS) == {*BI._CATEGORY_RU, *_DESCRIPTOR_ONLY} - prior
        forms = [f.casefold() for pair in OBJECT_KIND_RU_FORMS.values() for f in pair]
        assert len(forms) == len(set(forms)) == 42
        meta = {f[k].casefold() for f in _RU_META_FORMS.values() for k in ("ru_singular", "ru_plural")}
        assert not set(forms) & meta
        assert not set(forms) & {f.casefold() for f in _PREV_NONREF_FORMS}

    @pytest.mark.parametrize("raw,expected", _OBJECT_REF_TABLE)
    def test_canonicalize_object_ref_table(self, raw, expected):
        from rlm_tools_bsl.bsl_xml_parsers import canonicalize_object_ref

        assert canonicalize_object_ref(raw) == expected

    def test_canonicalize_type_ref_unchanged(self):
        from rlm_tools_bsl.bsl_xml_parsers import canonicalize_type_ref

        assert {raw: canonicalize_type_ref(raw) for raw in _TYPE_REF_HEAD} == _TYPE_REF_HEAD

    def test_head_of_category_unchanged(self):
        """`_head_of_category` замыкания хелперов — `OBJECT_KIND_HEADS.get(category, '')`; значения у всех 47 — как у
        HEAD (объединение прежних карт, литералом)."""
        from rlm_tools_bsl.bsl_xml_parsers import CATEGORY_TO_REF_HEAD, OBJECT_KIND_HEADS

        head = {**CATEGORY_TO_REF_HEAD, **_PREV_NONREF_HEADS, **_PREV_DESCRIPTOR_HEADS, **_PREV_OTHER_HEADS}
        for category in (*BI._CATEGORY_RU, *_DESCRIPTOR_ONLY):
            assert OBJECT_KIND_HEADS.get(category, "") == head[category], category


# ═══════════════ Задача 3 — формы аргумента 21 категории (Д1, Д2; П1) ═══════════════


def _norm_paths(obj, roots: tuple[str, ...]):
    """Копия ответа без полей времени, абсолютные корни мира в строках заменены на ``<ROOT{i}>`` (оба написания
    разделителя) — эталон не зависит от временного каталога прогона."""
    obj = _strip_times(obj)
    subs = []
    for i, root in enumerate(roots):
        for form in {root, root.replace("\\", "/"), root.replace("/", "\\")}:
            subs.append((form, f"<ROOT{i}>"))
    subs.sort(key=lambda pair: -len(pair[0]))

    def walk(v):
        if isinstance(v, str):
            for old, new in subs:
                v = v.replace(old, new)
            return v
        if isinstance(v, dict):
            return {walk(k): walk(x) for k, x in v.items()}
        if isinstance(v, (list, tuple)):
            return [walk(x) for x in v]
        return v

    return walk(obj)


def _outcome(bsl, helper: str, value):
    """Исход вызова хелпера с объектом ``value``: ответ либо ``['raises', имя типа, текст]``."""
    calls = {
        "find_custom_modifications": lambda v: bsl[helper](v, custom_prefixes=["х"]),
        "find_ext_overrides": lambda v: bsl[helper]("../cfe/Расш1", object_name=v),
        "find_attributes": lambda v: bsl[helper](object_name=v),
        "find_predefined": lambda v: bsl[helper](object_name=v),
        "find_templates": lambda v: bsl[helper](owner=v),
        "read_procedure": lambda v: bsl[helper](v, "ПередЗаписью"),
    }
    try:
        res = calls.get(helper, lambda v: bsl[helper](v))(value)
    except Exception as exc:  # noqa: BLE001 — исход сравнивается целиком
        return ["raises", type(exc).__name__, str(exc)]
    if helper == "find_references_to_object" and isinstance(res, dict):
        # строки и `kinds_applied` у `Документ.Док` меняют задачи 4, 6, 7 — сравнивается разбор цели
        return {"object": res.get("object"), "resolved_ref": (res.get("_meta") or {}).get("resolved_ref")}
    return res


def _digest(obj) -> str:
    import hashlib

    dump = json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(dump.encode("utf-8")).hexdigest()[:16]


# Хелперы `_PUBLIC_ARG_GUARDS` (композиты `get_object_profile`, `analyze_document_flow` — среди них).
_GUARDED_HELPERS = (
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
    "get_overrides",
    "find_ext_overrides",
    "find_attributes",
    "find_predefined",
    "find_templates",
    "parse_form",
    "extract_procedures",
    "find_exports",
    "get_module_outline",
    "extract_queries",
    "code_metrics",
    "read_procedure",
)
# Формы 26 прежних категорий: префикс, путь, вид без ссылочного типа, голое имя, гибрид, ссылка на член.
_PRIOR_FORMS = (
    "Документ.Док",
    "Documents/Док",
    "ОбщийМодуль.Задание",
    "Док",
    "Документ.Catalogs/Спр",
    "Документ.Док.ТабличнаяЧасть",
)


def _platform_neutral(obj):
    """Канон исхода для сравнения между ОС: разделитель пути — ``/``, элементы списков — по возрастанию. Порядок
    обхода каталогов у NTFS и ext4 разный (роли и опции мира приходят в разном порядке и у HEAD), а разделитель
    в путях ответа — родной для ОС."""
    if isinstance(obj, str):
        return obj.replace("\\", "/")
    if isinstance(obj, dict):
        return {_platform_neutral(k): _platform_neutral(v) for k, v in obj.items()}
    if isinstance(obj, list):
        items = [_platform_neutral(x) for x in obj]
        return sorted(items, key=lambda x: json.dumps(x, ensure_ascii=False, sort_keys=True, default=str))
    return obj


def _prior_forms_digests(world) -> dict[str, str]:
    """Отпечатки исходов ``_GUARDED_HELPERS`` × ``_PRIOR_FORMS`` мира: ключ ``<маршрут>|<хелпер>``; исход — в каноне
    ``_platform_neutral`` (эталон один для Windows и Linux)."""
    roots = tuple(str(p) for p in (world["root"].parent, world["root"]) if p)
    out = {}
    for route in ("idx", "live"):
        bsl = world[route]
        for helper in _GUARDED_HELPERS:
            outcomes = [_platform_neutral(_norm_paths(_outcome(bsl, helper, value), roots)) for value in _PRIOR_FORMS]
            out[f"{route}|{helper}"] = _digest(outcomes)
    return out


# Эталон HEAD `90cc326` (зонд кодом выгрузки на `world50`, два прогона без расхождений): отпечатки исходов
# `_prior_forms_digests` — ключ «маршрут|хелпер»; канон `_platform_neutral` — тот же код HEAD под WSL дает те же
# отпечатки.
_PRIOR_FORMS_HEAD = {
    "cf": {
        "idx|get_object_full_structure": "55a1154aa8991b25",
        "idx|get_object_modules": "45a400e39b099170",
        "idx|get_object_profile": "1114fec4a61cc755",
        "idx|analyze_object": "f1ac0cb47c40302e",
        "idx|find_print_forms": "5e6493d35ab59dd2",
        "idx|find_based_on_documents": "d22b6403d6054bf8",
        "idx|find_register_movements": "8f16edeb1dc822c8",
        "idx|analyze_document_flow": "94e30e473a6dc6de",
        "idx|find_references_to_object": "845c1f43d7dafba2",
        "idx|find_code_usages": "ef9a82d1062e035a",
        "idx|find_roles": "80d5d8f112991c83",
        "idx|find_custom_modifications": "6c1c2633c3445705",
        "idx|find_enum_values": "f08c64b00750cf2c",
        "idx|find_register_writers": "6604e7cee61aec24",
        "idx|find_exchange_plan_content": "30b8c3865a08627b",
        "idx|find_defined_types": "647fb9e6122199e2",
        "idx|find_event_subscriptions": "c2f664ddeaec99a7",
        "idx|find_functional_options": "7174a09ff2c4ace9",
        "idx|get_overrides": "f222d3ed1935d673",
        "idx|find_ext_overrides": "1438b8e3245dc601",
        "idx|find_attributes": "218a9fe599e5de3b",
        "idx|find_predefined": "4eceefa0f0e75652",
        "idx|find_templates": "b544f73f557f2f48",
        "idx|parse_form": "4eceefa0f0e75652",
        "idx|extract_procedures": "b2877df6762d13f7",
        "idx|find_exports": "b2877df6762d13f7",
        "idx|get_module_outline": "414448fa25211442",
        "idx|extract_queries": "4a7298ae0ffea936",
        "idx|code_metrics": "95650fab72dedb0b",
        "idx|read_procedure": "4a527edcefd45d23",
        "live|get_object_full_structure": "3f704ae169453c84",
        "live|get_object_modules": "11ab98f80d1a2785",
        "live|get_object_profile": "4ac60e707c5f7462",
        "live|analyze_object": "d1f645da20129943",
        "live|find_print_forms": "5e6493d35ab59dd2",
        "live|find_based_on_documents": "a484176923661afb",
        "live|find_register_movements": "907251c4bb20bf45",
        "live|analyze_document_flow": "72c1c703e89e97ca",
        "live|find_references_to_object": "845c1f43d7dafba2",
        "live|find_code_usages": "bf28d668236219fb",
        "live|find_roles": "5e84c9ad3d652ac0",
        "live|find_custom_modifications": "0165b548dadb99f5",
        "live|find_enum_values": "f08c64b00750cf2c",
        "live|find_register_writers": "9a56eddd0671c3db",
        "live|find_exchange_plan_content": "30b8c3865a08627b",
        "live|find_defined_types": "647fb9e6122199e2",
        "live|find_event_subscriptions": "c2f664ddeaec99a7",
        "live|find_functional_options": "6ccd97071c2b31cd",
        "live|get_overrides": "59d5d2e3133570b3",
        "live|find_ext_overrides": "1438b8e3245dc601",
        "live|find_attributes": "218a9fe599e5de3b",
        "live|find_predefined": "4eceefa0f0e75652",
        "live|find_templates": "3799a44c91ba3430",
        "live|parse_form": "4eceefa0f0e75652",
        "live|extract_procedures": "b2877df6762d13f7",
        "live|find_exports": "b2877df6762d13f7",
        "live|get_module_outline": "4da457da78b00923",
        "live|extract_queries": "4a7298ae0ffea936",
        "live|code_metrics": "95650fab72dedb0b",
        "live|read_procedure": "4a527edcefd45d23",
    },
    "edt": {
        "idx|get_object_full_structure": "55a1154aa8991b25",
        "idx|get_object_modules": "d8dc7f386b3115ef",
        "idx|get_object_profile": "27e09833749778d6",
        "idx|analyze_object": "be53b55e8e4f287b",
        "idx|find_print_forms": "5e6493d35ab59dd2",
        "idx|find_based_on_documents": "d22b6403d6054bf8",
        "idx|find_register_movements": "b6762e37a529ac43",
        "idx|analyze_document_flow": "18edd22712694f96",
        "idx|find_references_to_object": "845c1f43d7dafba2",
        "idx|find_code_usages": "ef9a82d1062e035a",
        "idx|find_roles": "878b73b641aa369c",
        "idx|find_custom_modifications": "7027f56dfc01ce26",
        "idx|find_enum_values": "43628b1d0890162a",
        "idx|find_register_writers": "5507ce175c19f071",
        "idx|find_exchange_plan_content": "30b8c3865a08627b",
        "idx|find_defined_types": "647fb9e6122199e2",
        "idx|find_event_subscriptions": "69bfd46400dc1544",
        "idx|find_functional_options": "edf8faf013ce910a",
        "idx|get_overrides": "60a18912142b0884",
        "idx|find_ext_overrides": "1438b8e3245dc601",
        "idx|find_attributes": "667c9e3c1dd515a3",
        "idx|find_predefined": "4eceefa0f0e75652",
        "idx|find_templates": "e6b0f16aba1ada09",
        "idx|parse_form": "4eceefa0f0e75652",
        "idx|extract_procedures": "46e70683b90b4cbb",
        "idx|find_exports": "d8900e02586cb90c",
        "idx|get_module_outline": "f77a67c6a42992de",
        "idx|extract_queries": "4a7298ae0ffea936",
        "idx|code_metrics": "95650fab72dedb0b",
        "idx|read_procedure": "fa638605df3274d4",
        "live|get_object_full_structure": "deac4795ee02eb5b",
        "live|get_object_modules": "e6952e27cae0ee26",
        "live|get_object_profile": "e450261e91487b0d",
        "live|analyze_object": "9ac40af342bf677d",
        "live|find_print_forms": "5e6493d35ab59dd2",
        "live|find_based_on_documents": "a484176923661afb",
        "live|find_register_movements": "89b1bd4742c2c60f",
        "live|analyze_document_flow": "7aaa38de9741fff5",
        "live|find_references_to_object": "845c1f43d7dafba2",
        "live|find_code_usages": "867afd9c6e53af39",
        "live|find_roles": "4cb4557a8da794d3",
        "live|find_custom_modifications": "dbbdf4b94fcdd800",
        "live|find_enum_values": "43628b1d0890162a",
        "live|find_register_writers": "b71ec7ddf1c63f50",
        "live|find_exchange_plan_content": "30b8c3865a08627b",
        "live|find_defined_types": "647fb9e6122199e2",
        "live|find_event_subscriptions": "69bfd46400dc1544",
        "live|find_functional_options": "ed4a28e143609919",
        "live|get_overrides": "f909b6490a779d5d",
        "live|find_ext_overrides": "1438b8e3245dc601",
        "live|find_attributes": "667c9e3c1dd515a3",
        "live|find_predefined": "4eceefa0f0e75652",
        "live|find_templates": "3799a44c91ba3430",
        "live|parse_form": "4eceefa0f0e75652",
        "live|extract_procedures": "46e70683b90b4cbb",
        "live|find_exports": "d8900e02586cb90c",
        "live|get_module_outline": "9b3112b64414e4c8",
        "live|extract_queries": "4a7298ae0ffea936",
        "live|code_metrics": "95650fab72dedb0b",
        "live|read_procedure": "fa638605df3274d4",
    },
}


def test_prior_forms_unchanged(world50):
    """Обе версии: формы 26 прежних категорий у хелперов `_PUBLIC_ARG_GUARDS` и композитов отвечаются, как у HEAD
    (у `find_references_to_object` — разбор цели и отказы)."""
    got = _prior_forms_digests(world50)
    expected = _PRIOR_FORMS_HEAD[world50["fmt"]]
    assert {k: v for k, v in got.items() if v != expected.get(k)} == {}


# Формы 21 категории без головы единственного числа в прежнем разборе → путь каталога того же объекта.
_SINGULAR_FORMS = [
    ("Роль.ПолныеПрава", "Roles/ПолныеПрава"),
    ("Role.ПолныеПрава", "Roles/ПолныеПрава"),
    ("роль.ПолныеПрава", "Roles/ПолныеПрава"),
    ("Роли/ПолныеПрава", "Roles/ПолныеПрава"),
    ("Роли.ПолныеПрава", "Roles/ПолныеПрава"),
    ("Подсистема.Продажи", "Subsystems/Продажи"),
    ("Подсистемы/Продажи", "Subsystems/Продажи"),
    ("ОбщаяКартинка.Картинка", "CommonPictures/Картинка"),
    ("CommonPicture.Картинка", "CommonPictures/Картинка"),
    ("ОбщиеКартинки/Картинка", "CommonPictures/Картинка"),
    ("ПараметрСеанса.ТекПольз", "SessionParameters/ТекПольз"),
    ("ЭлементСтиля.ЭлементСтиля1", "StyleItems/ЭлементСтиля1"),
    ("Стиль.Основной", "Styles/Основной"),
    ("ПакетXDTO.Пакет", "XDTOPackages/Пакет"),
    ("ОбщийМакет.ОбщСКД", "CommonTemplates/ОбщСКД"),
    ("РегламентноеЗадание.Задание", "ScheduledJobs/Задание"),
    ("ПодпискаНаСобытие.Подписка", "EventSubscriptions/Подписка"),
    ("ФункциональнаяОпция.Опция", "FunctionalOptions/Опция"),
    ("Язык.Русский", "Languages/Русский"),
]
_OBJECT_HELPERS = ("get_object_full_structure", "get_object_modules", "analyze_object", "get_object_profile")


def _not_found(res) -> bool:
    return isinstance(res, dict) and "не найден" in str(res.get("error") or "")


class TestKindHeadForms:
    def test_helpers_doc_prior_canonicalization_lists(self):
        """Перечни «хелперы с прежней канонизацией аргумента (…)» в docs/HELPERS.md не числят `find_data_path` без
        оговорки: с v1.50.0 его концы — каноническая ссылка (`Roles/X` → `Role.X`)."""
        import re

        doc = (Path(__file__).resolve().parents[1] / "docs" / "HELPERS.md").read_text(encoding="utf-8")
        lists = re.findall(r"с прежней канонизацией аргумента \((.*?)\) (?:отвечают|получают)", doc)
        assert len(lists) >= 2, lists
        for items in lists:
            assert "`find_data_path`" not in items or "до v1.50.0" in items, items

    @pytest.mark.parametrize("form,path", _SINGULAR_FORMS)
    def test_singular_forms_answer_object(self, world50, form, path):
        for route in ("idx", "live"):
            bsl = world50[route]
            for helper in _OBJECT_HELPERS:
                by_path = _strip_times(bsl[helper](path))
                assert not _not_found(by_path), (route, helper, by_path)
                assert _strip_times(bsl[helper](form)) == by_path, (route, helper)

    @pytest.mark.parametrize(
        "form,path",
        [("Подсистема.Продажи", "Subsystems/Продажи"), ("ПараметрСеанса.ТекПольз", "SessionParameters/ТекПольз")],
    )
    def test_find_roles_singular_equals_path(self, world50, form, path):
        for route in ("idx", "live"):
            bsl = world50[route]
            by_path = _strip_times(bsl["find_roles"](path))
            assert "ПолныеПрава" in {r["role_name"] for r in by_path["roles"]}, (route, by_path)
            assert _strip_times(bsl["find_roles"](form)) == by_path, route

    @pytest.mark.parametrize(
        "form,canon",
        [
            ("Roles/ПолныеПрава", "Role.ПолныеПрава"),
            ("Roles.ПолныеПрава", "Role.ПолныеПрава"),
            ("Роль.ПолныеПрава", "Role.ПолныеПрава"),
            ("Subsystems/Продажи", "Subsystem.Продажи"),
            ("Subsystems.Продажи", "Subsystem.Продажи"),
            ("CommonTemplates/ОбщСКД", "CommonTemplate.ОбщСКД"),
            ("XDTOPackages/Пакет", "XDTOPackage.Пакет"),
        ],
    )
    def test_refs_canonical_for_role_subsystem_template_xdto(self, world50, form, canon):
        for route in ("idx", "live"):
            bsl = world50[route]
            res = _strip_times(bsl["find_references_to_object"](form))
            assert res["object"] == canon, (route, res)
            assert res == _strip_times(bsl["find_references_to_object"](canon)), route

    def test_code_usages_canonical_for_subsystem_path(self, world50):
        for route in ("idx", "live"):
            assert world50[route]["find_code_usages"]("Subsystems/Продажи")["object"] == "Subsystem.Продажи"

    @pytest.mark.parametrize("raw", ["Роль.", "ОбщаяКартинка.", "Роли/"])
    @pytest.mark.parametrize("helper", ["get_object_full_structure", "find_roles"])
    def test_nameless_singular_refused(self, world50, helper, raw):
        with pytest.raises(ValueError, match=f"^{helper}: аргумент без имени объекта"):
            world50["idx"][helper](raw)

    @pytest.mark.parametrize("word", ["ОбщаяКартинка", "ПодпискаНаСобытие", "Подсистемы"])
    @pytest.mark.parametrize("helper", ["get_object_full_structure", "find_event_subscriptions"])
    def test_bare_singular_word_refused(self, world50, helper, word):
        with pytest.raises(ValueError, match=f"^{helper}: '{word}' — имя категории"):
            world50["idx"][helper](word)

    def test_bare_word_of_existing_object_not_refused(self, world50):
        """`Роль` — точное имя отчёта: отказа «имя категории» нет, ответ — отчёт."""
        for route in ("idx", "live"):
            res = world50[route]["get_object_full_structure"]("Роль")
            assert res.get("category") == "Reports" and res.get("object_name") == "Роль", (route, res)

    def test_homonym_job_and_common_module(self, world50):
        for route in ("idx", "live"):
            bsl = world50[route]
            assert bsl["analyze_object"]("РегламентноеЗадание.Задание")["category"] == "ScheduledJobs"
            assert bsl["get_object_modules"]("РегламентноеЗадание.Задание")["error"].startswith(
                "У объекта ScheduledJobs/Задание нет модулей"
            )
            for ref in ("ОбщийМодуль.Задание", "CommonModules/Задание"):
                assert bsl["analyze_object"](ref)["category"] == "CommonModules", ref
                assert bsl["get_object_modules"](ref)["category"] == "CommonModules", ref
            # голое имя — прежний выбор HEAD (эталон: общий модуль у обоих хелперов)
            assert bsl["analyze_object"]("Задание")["category"] == "CommonModules"
            assert bsl["get_object_modules"]("Задание")["category"] == "CommonModules"

    @pytest.mark.parametrize(
        "helper,raw,category",
        [
            ("analyze_document_flow", "ПодпискаНаСобытие.Подписка", "EventSubscriptions"),
            ("find_enum_values", "Роль.ПолныеПрава", "Roles"),
        ],
    )
    def test_single_category_helper_refusal(self, world50, helper, raw, category):
        bsl = world50["idx"]
        with pytest.raises(ValueError, match=f"^{helper}: .* объект категории {category}") as exc:
            bsl[helper](raw)
        route = str(exc.value).split("parse_object_xml(", 1)[1].split(")", 1)[0]
        assert bsl["parse_object_xml"](eval(route))["name"] == raw.split(".", 1)[1]  # noqa: S307 — литерал отказа

    def test_missing_singular_ref_refused(self, world50):
        for route in ("idx", "live"):
            with pytest.raises(ValueError, match="^find_references_to_object: объекта CommonPicture.Нет нет"):
                world50[route]["find_references_to_object"]("ОбщаяКартинка.Нет")

    def test_extension_only_object_singular(self, world50):
        if world50["fmt"] != "cf":
            pytest.skip("расширение `Расш1` в мире — только у CF")
        for route in ("idx", "live"):
            bsl = world50[route]
            by_path = _strip_times(bsl["get_object_full_structure"]("CommonPictures/РасшКартинка"))
            assert not _not_found(by_path) and by_path.get("object_name") == "РасшКартинка", by_path
            assert _strip_times(bsl["get_object_full_structure"]("ОбщаяКартинка.РасшКартинка")) == by_path

    @pytest.mark.parametrize("start", ["Подсистема.Продажи", "Subsystems/Продажи", "Подсистемы/Продажи"])
    def test_data_path_singular_end(self, world50, start):
        """`Подсистема.X` разбор `find_data_path` понимал и прежде (`_normalize_object_ref`); путь каталога и русское
        множественное в пути — концы-ссылки (`_canonical_ref_of`), прежде — отказ префикса."""
        bsl = world50["idx"]
        res = bsl["find_data_path"](start, "Документ.Док")
        assert res["found"] is True and "error" not in res, res
        expected = _strip_times(bsl["find_data_path"]("Subsystem.Продажи", "Документ.Док"))
        assert {k: v for k, v in _strip_times(res).items() if k != "from"} == {
            k: v for k, v in expected.items() if k != "from"
        }


# Хелперы с прежней канонизацией аргумента (`_typed_ref_of`): новые формы 21 категории существующих объектов — их
# собственный разбор прежний (раздел 2 плана); несуществующий объект — отказ шлюза (тест выше).
_LEGACY_PARSING_HELPERS = (
    "find_functional_options",
    "find_event_subscriptions",
    "find_based_on_documents",
    "find_templates",
)
_LEGACY_NEW_FORMS = (
    "Подсистема.Продажи",
    "Subsystems/Продажи",
    "ФункциональнаяОпция.Опция",
    "РегламентноеЗадание.Задание",
    "ОбщаяКартинка.Картинка",
    "ПодпискаНаСобытие.Подписка",
    "Роль.ПолныеПрава",
)


def _legacy_parsing_digests(world) -> dict[str, str]:
    roots = (str(world["root"].parent), str(world["root"]))
    return {
        f"{route}|{helper}": _digest([_norm_paths(_outcome(world[route], helper, v), roots) for v in _LEGACY_NEW_FORMS])
        for route in ("idx", "live")
        for helper in _LEGACY_PARSING_HELPERS
    }


# Эталон HEAD `90cc326` (зонд кодом выгрузки на `world50`): отпечатки `_legacy_parsing_digests`.
_LEGACY_PARSING_HEAD = {
    "cf": {
        "idx|find_functional_options": "89d90e0dcf3cc772",
        "idx|find_event_subscriptions": "112a471e4665f855",
        "idx|find_based_on_documents": "88837594087c6a90",
        "idx|find_templates": "a1edc6ebc13e9e03",
        "live|find_functional_options": "6a2e37ddc4dd9b34",
        "live|find_event_subscriptions": "112a471e4665f855",
        "live|find_based_on_documents": "c3a1097551fd00fd",
        "live|find_templates": "d630ed434eff3065",
    },
    "edt": {
        "idx|find_functional_options": "2710f01126f01611",
        "idx|find_event_subscriptions": "112a471e4665f855",
        "idx|find_based_on_documents": "88837594087c6a90",
        "idx|find_templates": "a1edc6ebc13e9e03",
        "live|find_functional_options": "e749d77418eedbe3",
        "live|find_event_subscriptions": "112a471e4665f855",
        "live|find_based_on_documents": "c3a1097551fd00fd",
        "live|find_templates": "d630ed434eff3065",
    },
}


def test_legacy_parsing_helpers_unchanged(world50):
    """Обе версии: хелперы с прежней канонизацией аргумента на новых формах 21 категории существующих объектов
    отвечают, как HEAD (их собственный разбор прежний — граница П1)."""
    got = _legacy_parsing_digests(world50)
    expected = _LEGACY_PARSING_HEAD[world50["fmt"]]
    assert {k: v for k, v in got.items() if v != expected.get(k)} == {}


# ═══════════════ Задача 4 — источники состава и прав: ссылка на объект любого вида (Д7, Д8; П5, П10) ═══════════════

_REF_COLS = "source_object, source_category, ref_object, ref_kind, used_in, path, line"


def _refs_of(db, kind: str) -> list[tuple]:
    return _rows(db, f"SELECT {_REF_COLS} FROM metadata_references WHERE ref_kind = ?", (kind,))  # noqa: S608


# Строки неизменных видов на `world50`, собранном кодом HEAD `90cc326` (литерал; путь — в раскладке формата).
_UNCHANGED_KIND_ROWS_HEAD = {
    "cf": [
        (
            "Док",
            "Documents",
            "Enum.Статусы",
            "attribute_type",
            "Document.Док.Attribute.Статус.Type",
            "Documents/Док.xml",
            None,
        ),
        (
            "ОпрТип",
            "DefinedTypes",
            "Catalog.Спр",
            "defined_type_content",
            "DefinedType.ОпрТип.Type",
            "DefinedTypes/ОпрТип.xml",
            None,
        ),
        (
            "Подписка",
            "EventSubscriptions",
            "Document.Док",
            "event_subscription_source",
            "EventSubscription.Подписка.Source",
            "EventSubscriptions/Подписка.xml",
            None,
        ),
    ],
    "edt": [
        (
            "Док",
            "Documents",
            "Enum.Статусы",
            "attribute_type",
            "Document.Док.Attribute.Статус.Type",
            "Documents/Док/Док.mdo",
            None,
        ),
        (
            "ОпрТип",
            "DefinedTypes",
            "Catalog.Спр",
            "defined_type_content",
            "DefinedType.ОпрТип.Type",
            "DefinedTypes/ОпрТип/ОпрТип.mdo",
            None,
        ),
        (
            "Подписка",
            "EventSubscriptions",
            "Document.Док",
            "event_subscription_source",
            "EventSubscription.Подписка.Source",
            "EventSubscriptions/Подписка/Подписка.mdo",
            None,
        ),
    ],
}

_NEW_HEAD_TARGETS = (
    "CommonPicture.Картинка",
    "StyleItem.ЭлементСтиля1",
    "XDTOPackage.Пакет",
    "CommonTemplate.ОбщСКД",
    "SessionParameter.ТекПольз",
    "WebService.ВС",
    "HTTPService.API",
    "FilterCriterion.Крит",
    "CommonAttribute.ОбщРекв",
    "Style.Основной",
)


def _pairs(db, kind: str, source: str | None = None) -> set[tuple[str, str]]:
    return {
        (row[4], row[2]) for row in _refs_of(db, kind) if source is None or row[0] == source
    }  # (used_in, ref_object)


class TestObjectTargets:
    def test_subsystem_content_rows_for_new_heads(self, world50):
        rows = {(row[2], row[4]) for row in _refs_of(world50["db"], "subsystem_content")}
        for target in _NEW_HEAD_TARGETS:
            assert (target, "Subsystem.Продажи.Content") in rows, target
        assert not [r for r in rows if _UUID_ITEM in r[0]], rows
        assert ("Catalog.Спр", "Subsystem.Подчиненная.Content") in rows

    def test_role_rights_rows_by_object(self, world50):
        db = world50["db"]
        full = {ref for _used, ref in _pairs(db, "role_rights", "ПолныеПрава")}
        assert full == {"Document.Док", "Subsystem.Продажи", "SessionParameter.ТекПольз", "WebService.ВС"}, full
        assert _pairs(db, "role_rights", "ТолькоКоманды") == {("Role.ТолькоКоманды.Rights", "Document.Док")}
        assert _pairs(db, "role_rights", "ВложеннаяПодсистема") == {
            ("Role.ВложеннаяПодсистема.Rights", "Subsystem.Подчиненная")
        }
        assert _pairs(db, "role_rights", "ТолькоЗапреты") == set()
        assert _pairs(db, "role_rights", "ЗапретыСпр") == set()
        assert not [r for r in _refs_of(db, "role_rights") if r[2].count(".") >= 2 or r[2].startswith("Configuration.")]

    def test_fo_content_rows_by_object(self, world50):
        db = world50["db"]
        members = [r for r in _refs_of(db, "functional_option_content") if r[0] == "ОпцияПоЧленам"]
        assert sorted((r[4], r[2]) for r in members) == sorted(
            [
                ("FunctionalOption.ОпцияПоЧленам.Content", "Document.Док"),
                ("FunctionalOption.ОпцияПоЧленам.Content", "Subsystem.Подчиненная"),
                ("FunctionalOption.ОпцияПоЧленам.Content", "CommonAttribute.ОбщРекв"),
                ("FunctionalOption.ОпцияПоЧленам.Content", "FilterCriterion.Крит"),
                ("FunctionalOption.ОпцияПоЧленам.Location", "Constant.Конст"),
            ]
        ), members
        const = sorted((r[4], r[2]) for r in _refs_of(db, "functional_option_content") if r[0] == "ОпцияКонст")
        assert const == [
            ("FunctionalOption.ОпцияКонст.Content", "Constant.Конст"),
            ("FunctionalOption.ОпцияКонст.Location", "Constant.Конст"),
        ], const
        assert not [r for r in _refs_of(db, "functional_option_content") if r[2].count(".") >= 2]

    def test_form_main_table_new_head(self, world50):
        rows = {(r[0], r[2], r[4]) for r in _refs_of(world50["db"], "form_main_table")}
        assert (
            "Крит",
            "FilterCriterion.Крит",
            "FilterCriterion.Крит.Form.ФормаСписка.Attribute.Список.MainTable",
        ) in rows, rows

    def test_live_subsystem_content_new_heads(self, world50):
        res = world50["live"]["find_references_to_object"]("ОбщаяКартинка.Картинка")
        assert "Subsystem.Продажи.Content" in {r["used_in"] for r in res["references"]}, res

    def test_attribute_types_unchanged(self, world50):
        rows = _rows(
            world50["db"],
            f"SELECT {_REF_COLS} FROM metadata_references WHERE ref_kind IN "  # noqa: S608
            "('attribute_type', 'defined_type_content', 'event_subscription_source')",
        )
        assert rows == _UNCHANGED_KIND_ROWS_HEAD[world50["fmt"]]

    @pytest.mark.parametrize("route", ["full", "git"])
    def test_update_equals_build_subsystem_roles_fo(self, world50, route, tmp_path_factory, monkeypatch):
        files_b = dict(world50["files"])
        cf = world50["fmt"] == "cf"
        sub = "Subsystems/Продажи.xml" if cf else "Subsystems/Продажи/Продажи.mdo"
        rights = "Roles/ТолькоКоманды/Ext/Rights.xml" if cf else "Roles/ТолькоКоманды/Rights.rights"
        fo = "FunctionalOptions/ОпцияПоЧленам.xml" if cf else "FunctionalOptions/ОпцияПоЧленам/ОпцияПоЧленам.mdo"
        files_b[sub] = files_b[sub].replace("CommonPicture.Картинка", "Language.Русский")
        files_b[rights] = _rights({"Document.Док.Command.Печать": ["View"], "SessionParameter.ТекПольз.X": ["Get"]})
        files_b[fo] = files_b[fo].replace("Document.Док.Attribute.Сумма", "Catalog.Спр.Attribute.Поле")
        queries = {
            kind: f"SELECT {_REF_COLS} FROM metadata_references WHERE ref_kind = '{kind}'"  # noqa: S608
            for kind in ("subsystem_content", "role_rights", "functional_option_content")
        }
        db = _assert_update_equals_build(
            tmp_path_factory, monkeypatch, world50["write"], world50["files"], files_b, queries, route=route
        )
        assert ("Language.Русский", "Subsystem.Продажи.Content") in {
            (r[2], r[4]) for r in _refs_of(db, "subsystem_content")
        }
        assert ("Role.ТолькоКоманды.Rights", "SessionParameter.ТекПольз") in _pairs(db, "role_rights", "ТолькоКоманды")


# ═══════════════ Задача 5 — чтение цели нового поколения (Д7; П5, У8) ═══════════════

_OUTSIDE_HINT_HEAD = "граф ссылок не хранит ссылок на объекты вида"
# Эталон HEAD `90cc326` (индекс, собранный кодом HEAD на `world50`, одинаков у CF и EDT; время снято):
# `find_references_to_object('CommonPictures/Картинка')`. Ключи `kinds_applied`/`kinds_unavailable` сравниваются
# отдельно: новые виды вводят задачи 6–7, их значения на матрице поколений закрепляет тест задачи 7.
_PICTURE_V18_HEAD = {
    "object": "CommonPicture.Картинка",
    "references": [],
    "total": 0,
    "truncated": False,
    "partial": False,
    "by_kind": {},
    "_meta": {
        "source": "index",
        "extensions_included": False,
        "unsupported_kinds": [],
        "index_coverage": "build_unproven",
        "kinds_requested": [],
        "expanded_on_read": {},
        "target_not_indexed": True,
    },
    "hint": (
        "граф ссылок не хранит ссылок на объекты вида CommonPicture (вид вне графа либо префикс не распознан): строк, "
        "кроме ролей с флагом «для новых объектов», нет; состав подсистем — analyze_subsystem('Картинка'), права ролей — "
        "find_roles('Картинка')"
    ),
}


def _v18_helpers(world, dst_dir: Path):
    """Хелперы мира на копии индекса v18 (`_downgrade_to_v18`) и её ридер (закрыть в тесте)."""
    reader = _v18_reader(world, dst_dir)
    return _world50_helpers(world["root"], world["ext_root"], reader), reader


@pytest.fixture(scope="module")
def worlds50_nometa(tmp_path_factory):
    """Мир релиза, собранный без метаданных (`build_metadata=False`), — ось «опция сборки» матрицы поколений."""
    built = {
        fmt: _build_world50(tmp_path_factory.mktemp(f"world50_nometa_{fmt}"), fmt, build_metadata=False)
        for fmt in ("cf", "edt")
    }
    yield built
    for world in built.values():
        world["reader"].close()


def _without_kinds(res: dict) -> dict:
    out = _strip_times(res)
    out["_meta"] = {k: v for k, v in out["_meta"].items() if k not in ("kinds_applied", "kinds_unavailable")}
    return out


class TestTargetGeneration:
    @pytest.mark.parametrize("ref", ["ОбщаяКартинка.Картинка", "CommonPictures/Картинка", "ПараметрСеанса.ТекПольз"])
    def test_new_head_target_has_rows_v19(self, world50, ref):
        res = world50["idx"]["find_references_to_object"](ref)
        assert res["references"] and res["total"] > 0, res
        assert "target_not_indexed" not in res["_meta"] and "hint" not in res, res

    def test_new_head_target_flagged_on_v18(self, world50, tmp_path):
        bsl, reader = _v18_helpers(world50, tmp_path)
        try:
            for ref in ("ОбщаяКартинка.Картинка", "CommonPictures/Картинка", "ПараметрСеанса.ТекПольз"):
                res = bsl["find_references_to_object"](ref)
                assert res["_meta"].get("target_not_indexed") is True, (ref, res)
                assert res["hint"].startswith(_OUTSIDE_HINT_HEAD), (ref, res)
                assert [r for r in res["references"] if not r["used_in"].endswith(".SetForNewObjects")] == [], res
            assert _without_kinds(bsl["find_references_to_object"]("CommonPictures/Картинка")) == _PICTURE_V18_HEAD
        finally:
            reader.close()

    def test_graph_heads_since_v19_derived(self):
        from rlm_tools_bsl.bsl_helpers import _GRAPH_HEADS_SINCE_V19

        assert _GRAPH_HEADS_SINCE_V19 == _D7_HEADS
        assert not _GRAPH_HEADS_SINCE_V19 & {
            "Subsystem",
            "Role",
            "FunctionalOption",
            "EventSubscription",
            "ScheduledJob",
        }

    def test_canonical_heads_never_flagged_on_v18(self, world50, tmp_path):
        bsl, reader = _v18_helpers(world50, tmp_path)
        try:
            for ref in ("Подсистема.Продажи", "Роль.ПолныеПрава", "ФункциональнаяОпция.Опция"):
                old = _strip_times(bsl["find_references_to_object"](ref))
                assert "target_not_indexed" not in old["_meta"] and "hint" not in old, (ref, old)
                new = _strip_times(world50["idx"]["find_references_to_object"](ref))
                assert old["references"] == new["references"], ref
            sub = bsl["find_references_to_object"]("Подсистема.Продажи")
            assert "Role.ПолныеПрава.Rights" in {r["used_in"] for r in sub["references"]}, sub
            svc = bsl["find_references_to_object"]("WebСервис.ВС")
            assert svc["_meta"].get("target_not_indexed") is True, svc
        finally:
            reader.close()

    def test_live_route_new_heads_not_flagged(self, world50):
        res = world50["live"]["find_references_to_object"]("ОбщаяКартинка.Картинка")
        assert "Subsystem.Продажи.Content" in {r["used_in"] for r in res["references"]}, res
        assert "target_not_indexed" not in res["_meta"] and "hint" not in res, res
        conf = world50["live"]["find_references_to_object"]("Configuration.Конфигурация")
        assert conf["_meta"].get("target_not_indexed") is True, conf

    @pytest.mark.parametrize("start", ["ОбщийМодуль.X", "ОбщаяФорма.X", "РегламентноеЗадание.Задание"])
    def test_data_path_prior_prefix_gate_unchanged(self, world50, start):
        """Обе версии: канонические головы вне прежней карты узлов — прежний отказ префикса."""
        res = world50["idx"]["find_data_path"](start, "Документ.Док")
        assert res["error"] == "endpoints must carry a recognized metadata-type prefix", res

    def test_unknown_head_still_flagged(self, world50, tmp_path):
        bsl, reader = _v18_helpers(world50, tmp_path)
        try:
            for helpers in (world50["idx"], bsl, world50["live"]):
                res = helpers["find_references_to_object"]("Configuration.Конфигурация")
                assert res["_meta"].get("target_not_indexed") is True, res
                assert res["hint"].startswith(_OUTSIDE_HINT_HEAD), res
        finally:
            reader.close()

    def test_data_path_reaches_new_head_v19(self, world50, tmp_path):
        bsl = world50["idx"]
        res = bsl["find_data_path"]("Подсистема.Продажи", "ОбщаяКартинка.Картинка")
        assert res["found"] is True and [e["kind"] for e in res["path"]] == ["subsystem_content"], res
        missing = bsl["find_data_path"]("Справочник.Спр", "ОбщаяКартинка.Нет")
        assert missing["found"] is False and missing["error"].startswith("объекта CommonPicture.Нет нет"), missing
        old, reader = _v18_helpers(world50, tmp_path)
        try:
            res18 = old["find_data_path"]("Подсистема.Продажи", "ОбщаяКартинка.Картинка")
            assert res18["error"].startswith(
                "конец CommonPicture.Картинка: ссылок на объекты вида CommonPicture в графе ссылок нет"
            ), res18
        finally:
            reader.close()

    def test_data_path_from_side_by_generation(self, world50, tmp_path):
        old, reader = _v18_helpers(world50, tmp_path)
        try:
            svc = old["find_data_path"]("WebСервис.ВС", "Справочник.Спр")
            assert (
                svc["error"]
                == "конец WebService.ВС: объекты вида WebService в графе ссылок не участвуют — путь не строится"
            )
            crit = old["find_data_path"]("КритерийОтбора.Крит", "Документ.Док")
            assert crit["found"] is True and [e["kind"] for e in crit["path"]] == ["filter_criterion_type"], crit
        finally:
            reader.close()
        res = world50["idx"]["find_data_path"]("WebСервис.ВС", "Справочник.Спр")
        assert "error" not in res and res["found"] is False and res["_meta"]["nodes_expanded"] == 1, res

    def test_data_path_new_head_node_expands_own_category(self, world50, monkeypatch):
        reader = world50["reader"]
        seen: list[tuple[str, str | None]] = []
        real = reader.find_metadata_refs_from

        def spy(name, *args, **kwargs):
            seen.append((name, kwargs.get("source_category")))
            return real(name, *args, **kwargs)

        monkeypatch.setattr(reader, "find_metadata_refs_from", spy)
        res = world50["idx"]["find_data_path"]("Подсистема.Продажи", "Справочник.Скрытый")
        assert res["found"] is False, res
        assert ("Картинка", "CommonPictures") in seen, seen

    @pytest.mark.parametrize("meta", ["with_meta", "no_meta"])
    def test_generation_by_metadata_matrix(self, worlds50, worlds50_nometa, meta, tmp_path):
        for fmt in ("cf", "edt"):
            world = (worlds50 if meta == "with_meta" else worlds50_nometa)[fmt]
            old, reader = _v18_helpers(world, tmp_path / fmt)
            try:
                for gen, bsl in (("v19", world["idx"]), ("v18", old)):
                    res = bsl["find_references_to_object"]("ОбщаяКартинка.Картинка")
                    path = bsl["find_data_path"]("Подсистема.Продажи", "ОбщаяКартинка.Картинка")
                    cell = (fmt, meta, gen)
                    if meta == "no_meta":
                        assert res["references"] == [] and res["total"] == 0, (cell, res)
                        assert res["_meta"]["index_coverage"] == "disabled", (cell, res)
                    if gen == "v19":
                        assert "target_not_indexed" not in res["_meta"] and "hint" not in res, (cell, res)
                        if meta == "with_meta":
                            assert res["total"] > 0 and path["found"] is True, (cell, res, path)
                        else:
                            doc = bsl["find_data_path"]("Подсистема.Продажи", "Документ.Док")
                            shape = ("found", "error", "partial")
                            assert sorted(path) == sorted(doc), (cell, path, doc)
                            assert [path.get(k) for k in shape] == [doc.get(k) for k in shape], (cell, path, doc)
                    else:
                        assert res["_meta"].get("target_not_indexed") is True, (cell, res)
                        assert res["hint"].startswith(_OUTSIDE_HINT_HEAD), (cell, res)
                        assert "в графе ссылок нет" in (path.get("error") or ""), (cell, path)
            finally:
                reader.close()


# ═══════════════ Задача 6 — общий реквизит: состав, разбор, ссылки разделения (Д9, Д14, Д15; П9, П12, П13) ═══════════════


def _ca_rel(fmt: str, name: str) -> str:
    return f"CommonAttributes/{name}.xml" if fmt == "cf" else f"CommonAttributes/{name}/{name}.mdo"


def _sub_rel(fmt: str, path: str) -> str:
    """Описатель подсистемы по пути «Продажи» / «Продажи/Подчиненная»."""
    *parents, own = path.split("/")
    prefix = "".join(f"{p}/Subsystems/" for p in parents)
    return f"Subsystems/{prefix}{own}.xml" if fmt == "cf" else f"Subsystems/{prefix}{own}/{own}.mdo"


# Эталон HEAD `90cc326` (разбор `parse_object_xml` кодом HEAD на `world50`, одинаков у CF и EDT).
_SALES_CONTENT_HEAD = list(_SALES_CONTENT)
_NESTED_PARSE_HEAD = {
    "content": ["Catalog.Спр"],
    "references": [{"ref_object": "Catalog.Спр", "ref_kind": "subsystem_content", "used_in_suffix": "Content"}],
}
_CRIT_PARSE_HEAD = {
    "content": None,
    "references": [{"ref_object": "Document.Док", "ref_kind": "filter_criterion_type", "used_in_suffix": "Type"}],
}
_CA_KIND = "common_attribute_content"


def _used_ins(res: dict, kind: str | None = None) -> set[str]:
    return {r["used_in"] for r in res["references"] if kind is None or r["kind"] == kind}


class TestCommonAttributeContent:
    def test_parse_common_attribute_composition(self, world50):
        parse = world50["live"]["parse_object_xml"]
        fmt = world50["fmt"]
        general = parse(_ca_rel(fmt, "ОбщРекв"))
        assert (general["content"], general["content_excluded"], general["auto_use"]) == (
            ["Document.Док"],
            ["Catalog.Спр"],
            "DontUse",
        ), general
        separator = parse(_ca_rel(fmt, "ОбластьДанных"))
        assert (separator["content"], separator["content_excluded"], separator["auto_use"]) == (
            [],
            ["Catalog.Спр", "Catalog.Скрытый"],
            "Use",
        ), separator
        auto = parse(_ca_rel(fmt, "АвтоРекв"))
        assert auto["content"] == ["Document.Док"] and auto["auto_use"] == "Use", auto
        for res in (general, separator, auto):
            assert "" not in res["content"] + res["content_excluded"], res

    def test_subsystem_content_unchanged(self, world50):
        """Обе версии: ветка состава подсистемы и критерия отбора (элемент — текст) — как у HEAD."""
        parse = world50["live"]["parse_object_xml"]
        fmt = world50["fmt"]
        assert parse(_sub_rel(fmt, "Продажи"))["content"] == _SALES_CONTENT_HEAD
        nested = parse(_sub_rel(fmt, "Продажи/Подчиненная"))
        assert {k: nested.get(k) for k in ("content", "references")} == _NESTED_PARSE_HEAD
        crit = parse("FilterCriteria/Крит.xml" if fmt == "cf" else "FilterCriteria/Крит/Крит.mdo")
        assert {k: crit.get(k) for k in ("content", "references")} == _CRIT_PARSE_HEAD

    @pytest.mark.parametrize(
        "target,prop",
        [
            ("ПараметрСеанса.ОбластьЗначение", "DataSeparationValue"),
            ("ПараметрСеанса.ОбластьИспользование", "DataSeparationUse"),
            ("Константа.ИспользоватьРазделение", "ConditionalSeparation"),
        ],
    )
    def test_separation_linked_refs(self, world50, target, prop):
        for route in ("idx", "live"):
            res = world50[route]["find_references_to_object"](target)
            assert f"CommonAttribute.ОбластьДанных.{prop}" in _used_ins(res, "linked_object"), (route, res)

    def test_data_path_common_attribute_edges(self, world50):
        bsl = world50["idx"]
        sep = bsl["find_data_path"]("ОбщийРеквизит.ОбластьДанных", "ПараметрСеанса.ОбластьЗначение")
        assert sep["found"] is True and [e["kind"] for e in sep["path"]] == ["linked_object"], sep
        content = bsl["find_data_path"]("ОбщийРеквизит.ОбщРекв", "Документ.Док")
        assert content["found"] is True and [e["kind"] for e in content["path"]] == [_CA_KIND], content

    def test_item_conditional_separation_ignored(self, world50):
        """Граница: условное разделение ЭЛЕМЕНТА состава (CF `xr:ConditionalSeparation` у `Catalog.Скрытый`) строк не
        даёт; прежние строки `linked_object` — тесты 1.47.0."""
        for route in ("idx", "live"):
            res = world50[route]["find_references_to_object"]("Константа.Конст")
            assert not [u for u in _used_ins(res) if u.startswith("CommonAttribute.ОбластьДанных")], (route, res)

    def test_explicit_use_rows(self, world50):
        doc = world50["idx"]["find_references_to_object"]("Документ.Док", kinds=[_CA_KIND])
        assert _used_ins(doc) == {"CommonAttribute.ОбщРекв.Content", "CommonAttribute.АвтоРекв.Content"}, doc
        cat = world50["idx"]["find_references_to_object"]("Справочник.Спр", kinds=[_CA_KIND])
        assert cat["references"] == [] and cat["total"] == 0, cat

    def test_live_route_same_rows(self, world50):
        idx = world50["idx"]["find_references_to_object"]("Документ.Док", kinds=[_CA_KIND])
        live = world50["live"]["find_references_to_object"]("Документ.Док", kinds=[_CA_KIND])
        assert [(r["used_in"], r["path"], r["kind"]) for r in live["references"]] == [
            (r["used_in"], r["path"], r["kind"]) for r in idx["references"]
        ], live
        assert _CA_KIND in live["_meta"]["kinds_applied"] and _CA_KIND not in live["_meta"]["unsupported_kinds"]

    def test_kind_unavailable_on_v18(self, world50, tmp_path):
        bsl, reader = _v18_helpers(world50, tmp_path)
        try:
            assert _CA_KIND in bsl["find_references_to_object"]("Документ.Док")["_meta"]["kinds_unavailable"]
            res = bsl["find_references_to_object"]("Документ.Док", kinds=[_CA_KIND])
            assert res["references"] == [] and res["_meta"]["kinds_unavailable"] == [_CA_KIND], res
        finally:
            reader.close()

    def test_kinds_unavailable_union_on_v17(self, world50, tmp_path):
        copy = _db_copy(world50["db"], tmp_path / "bsl_index_v17.db")
        _downgrade_to_v17(copy)
        reader = IndexReader(copy)
        try:
            bsl = _world50_helpers(world50["root"], world50["ext_root"], reader)
            unavailable = set(bsl["find_references_to_object"]("Документ.Док")["_meta"]["kinds_unavailable"])
            assert {"document_journal_content", "linked_object", "form_query", _CA_KIND} <= unavailable, unavailable
        finally:
            reader.close()

    def test_kind_accepted(self, world50):
        res = world50["idx"]["find_references_to_object"]("Документ.Док", kinds=[_CA_KIND])
        assert res["_meta"]["kinds_applied"] == [_CA_KIND], res["_meta"]

    @pytest.mark.parametrize("route", ["full", "git"])
    def test_update_equals_build_common_attribute(self, world50, route, tmp_path_factory, monkeypatch):
        rel = _ca_rel(world50["fmt"], "ОбщРекв")
        files_b = dict(world50["files"])
        files_b[rel] = (
            files_b[rel].replace("Catalog.Спр", "Catalog.Скрытый").replace("DontUse</xr:Use>", "Use</xr:Use>")
        )
        if world50["fmt"] == "edt":
            files_b[rel] = files_b[rel].replace("<use>DontUse</use>", "<use>Use</use>")
        query = f"SELECT {_REF_COLS} FROM metadata_references WHERE source_category = 'CommonAttributes'"  # noqa: S608
        db = _assert_update_equals_build(
            tmp_path_factory, monkeypatch, world50["write"], world50["files"], files_b, {"ca": query}, route=route
        )
        assert ("CommonAttribute.ОбщРекв.Content", "Catalog.Скрытый") in {(r[4], r[2]) for r in _rows(db, query)}

    def test_snapshot_rereads_common_attributes(self, world50, tmp_path, monkeypatch):
        """Полный скан со снимком: правка описателя общего реквизита той же длины с прежним временем перечитывается —
        описатель хешируется доменом метаданных."""
        root = tmp_path / "repo" / "cf"
        world50["write"](root, world50["files"])
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
        db = IndexBuilder().build(str(root), build_calls=True)
        path = root / _ca_rel(world50["fmt"], "ОбщРекв")
        st = path.stat()
        text = path.read_text(encoding="utf-8")
        assert "Document.Док" in text
        path.write_bytes(text.replace("Document.Док", "Document.Скр").encode("utf-8"))  # та же длина
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
        res = IndexBuilder().update(str(root))
        assert res["git_fast_path"] is False, res
        used = {(r[4], r[2]) for r in _refs_of(db, _CA_KIND)}
        assert ("CommonAttribute.ОбщРекв.Content", "Document.Скр") in used, used


# ═══════════════ Задача 7 — запросы макетов СКД (Д10; П7, У2, У9) ═══════════════

_TQ = "template_query"
_TQ_SQL = f"SELECT {_REF_COLS} FROM metadata_references WHERE ref_kind = '{_TQ}'"  # noqa: S608
# Эталон HEAD `90cc326`: `kinds_applied` у любой цели (индекс) — 26 видов; v1.50.0 добавляет два.
_HEAD_KINDS_APPLIED = [
    "attribute_type",
    "based_on",
    "characteristic_type",
    "characteristic_value_type",
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
_NEW_KINDS = ["common_attribute_content", _TQ]


def _tq_rows(bsl, ref: str) -> list[tuple[str, str]]:
    res = bsl["find_references_to_object"](ref, kinds=[_TQ])
    return sorted((r["used_in"], r["path"]) for r in res["references"])


def _content_rel(fmt: str, owner_path: str, name: str) -> str:
    """Файл содержимого макета СКД: ``owner_path`` — `Reports/ОтчетСКД` либо `CommonTemplates` (общий макет)."""
    base = f"{owner_path}/Templates/{name}" if owner_path != "CommonTemplates" else f"CommonTemplates/{name}"
    return f"{base}/Ext/Template.xml" if fmt == "cf" else f"{base}/Template.dcs"


class TestTemplateQueries:
    def test_report_template_rows(self, world50):
        bsl, fmt = world50["idx"], world50["fmt"]
        main = ("Report.ОтчетСКД.Template.ОсновнаяСхема.Query", _content_rel(fmt, "Reports/ОтчетСКД", "ОсновнаяСхема"))
        doc = bsl["find_references_to_object"]("Документ.Док", kinds=[_TQ])
        assert doc["total"] == 1 and [(r["used_in"], r["path"]) for r in doc["references"]] == [main], doc
        assert [r[2] for r in _rows(world50["db"], _TQ_SQL) if r[4] == main[0] and r[2].lower() == "document.док"] == [
            "Document.Док"
        ]
        assert _tq_rows(bsl, "РегистрНакопления.Рег") == [main]
        assert _tq_rows(bsl, "Справочник.Спр") == sorted(
            [
                main,
                ("Document.Док.Template.СхемаДок.Query", _content_rel(fmt, "Documents/Док", "СхемаДок")),
                ("DataProcessor.ОбрСКД.Template.Схема.Query", _content_rel(fmt, "DataProcessors/ОбрСКД", "Схема")),
                ("CommonTemplate.ОбщСКД.Query", _content_rel(fmt, "CommonTemplates", "ОбщСКД")),
            ]
        )
        assert _tq_rows(bsl, "Перечисление.Статусы") == [
            ("DataProcessor.ОбрСКД.Template.Схема.Query", _content_rel(fmt, "DataProcessors/ОбрСКД", "Схема"))
        ]

    def test_comments_and_literals_ignored(self, world50):
        for ref in ("Справочник.Скрытый", "Документ.Скрытый2"):
            assert _tq_rows(world50["idx"], ref) == [], ref

    def test_english_query_and_entities(self, world50):
        rows = {
            r[4]: r for r in _rows(world50["db"], _TQ_SQL) if r[4].startswith("DataProcessor.ОбрСКД.Template.Схема")
        }
        refs = {r[2] for r in _rows(world50["db"], _TQ_SQL) if r[4] == "DataProcessor.ОбрСКД.Template.Схема.Query"}
        assert refs == {"Catalog.Спр", "Enum.Статусы"}, (refs, rows)

    def test_spreadsheet_template_ignored(self, world50):
        used = {r[4] for r in _rows(world50["db"], _TQ_SQL)}
        for name in ("Report.ОтчетСКД.Template.Печать.Query", "CommonTemplate.ОбщТабл.Query"):
            assert name not in used
        assert not [u for u in used if "БезЗапросов" in u], used

    def test_kind_live_unsupported(self, world50):
        res = world50["live"]["find_references_to_object"]("Документ.Док", kinds=[_TQ])
        assert res["references"] == [] and res["_meta"]["unsupported_kinds"] == [_TQ], res
        full = world50["live"]["find_references_to_object"]("Документ.Док")
        assert _TQ in full["_meta"]["unsupported_kinds"]

    def test_kind_unavailable_on_v18(self, world50, tmp_path):
        bsl, reader = _v18_helpers(world50, tmp_path)
        try:
            res = bsl["find_references_to_object"]("Документ.Док", kinds=[_TQ])
            assert res["references"] == [] and res["_meta"]["kinds_unavailable"] == [_TQ], res
        finally:
            reader.close()

    def _spy_template_refresh(self, monkeypatch) -> list:
        calls: list = []
        real = BI._refresh_template_queries

        def spy(*args, **kwargs):
            calls.append(args[2] if len(args) > 2 else kwargs.get("paths"))
            return real(*args, **kwargs)

        monkeypatch.setattr(BI, "_refresh_template_queries", spy)
        return calls

    def test_descriptor_edit_keeps_rows(self, world50, tmp_path_factory, monkeypatch):
        cf = world50["fmt"] == "cf"
        calls = self._spy_template_refresh(monkeypatch)
        doc = "Documents/Док.xml" if cf else "Documents/Док/Док.mdo"
        files_b = dict(world50["files"])
        files_b[doc] = files_b[doc].replace("Количество", "Колво")
        _assert_update_equals_build(
            tmp_path_factory,
            monkeypatch,
            world50["write"],
            world50["files"],
            files_b,
            {"tq": _TQ_SQL},
            route="git",
            expect_pointwise=("Documents", "Док"),
        )
        rep = "Reports/ОтчетСКД.xml" if cf else "Reports/ОтчетСКД/ОтчетСКД.mdo"
        files_c = dict(world50["files"])
        files_c[rep] = files_c[rep].replace("<Name>ОтчетСКД</Name>", f"<Name>ОтчетСКД</Name>{_cf_syn('Отчёт')}")
        if not cf:
            files_c[rep] = files_c[rep].replace("<name>ОтчетСКД</name>", f"<name>ОтчетСКД</name>{_edt_syn('Отчёт')}")
        _assert_update_equals_build(
            tmp_path_factory, monkeypatch, world50["write"], world50["files"], files_c, {"tq": _TQ_SQL}, route="git"
        )
        assert calls == [], calls

    def test_bsl_only_delta_skips_templates(self, world50, tmp_path_factory, monkeypatch):
        calls = self._spy_template_refresh(monkeypatch)
        mod = _module_path(world50["fmt"], "Documents", "Док", "ObjectModule.bsl")
        files_b = dict(world50["files"])
        files_b[mod] = files_b[mod] + "\nПроцедура Новая()\nКонецПроцедуры\n"
        db = _assert_update_equals_build(
            tmp_path_factory, monkeypatch, world50["write"], world50["files"], files_b, {"tq": _TQ_SQL}, route="git"
        )
        assert calls == [] and _rows(db, _TQ_SQL), calls

    def test_no_metadata_build_skips_template_domain(self, world50, tmp_path, monkeypatch):
        from rlm_tools_bsl._index_snapshot import load_snapshot

        seen: list[str] = []
        for name in ("_template_query_files", "_template_query_rows"):
            real = getattr(BI, name)

            def spy(*args, _real=real, _name=name, **kwargs):
                seen.append(_name)
                return _real(*args, **kwargs)

            monkeypatch.setattr(BI, name, spy)
        root = tmp_path / "repo" / "cf"
        world50["write"](root, world50["files"])
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
        db = IndexBuilder().build(str(root), build_calls=True, build_metadata=False)
        IndexBuilder().update(str(root))
        assert seen == [], seen
        snap = load_snapshot(db)
        assert snap is not None and snap["domains"].get("template_queries") is None, snap
        con = sqlite3.connect(str(db))
        try:
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "metadata_references" in tables:
                assert (
                    con.execute(f"SELECT COUNT(*) FROM metadata_references WHERE ref_kind='{_TQ}'").fetchone()[0] == 0
                )
        finally:
            con.close()

    def _edits(self, fmt: str) -> dict[str, tuple[dict[str, str], tuple[str, str] | None]]:
        """Правки домена: имя → (дерево B, ожидаемый точечный вызов git-пути)."""
        cf = fmt == "cf"
        a = _world50_files(fmt)
        out: dict[str, tuple[dict[str, str], tuple[str, str] | None]] = {}

        def edited(changes: dict[str, str | None]) -> dict[str, str]:
            b = dict(a)
            for rel, text in changes.items():
                if text is None:
                    b.pop(rel, None)
                else:
                    b[rel] = text
            return b

        main = _content_rel(fmt, "Reports/ОтчетСКД", "ОсновнаяСхема")
        out["report_query"] = (
            edited({main: a[main].replace("Справочник.Спр КАК С", "Справочник.Скрытый КАК С")}),
            None,
        )
        doc = _content_rel(fmt, "Documents/Док", "СхемаДок")
        out["document_query"] = (
            edited({doc: a[doc].replace("Справочник.Спр", "Документ.Скрытый2")}),
            ("Documents", "Док") if cf else None,
        )
        common = _content_rel(fmt, "CommonTemplates", "ОбщСКД")
        out["common_query"] = (edited({common: a[common].replace("Справочник.Спр", "Перечисление.Статусы")}), None)
        proc_dir = "DataProcessors/ОбрСКД/Templates"
        if cf:
            desc = f"{proc_dir}/Схема.xml"
            out["type_to_sheet"] = (
                edited({desc: a[desc].replace("DataCompositionSchema", "SpreadsheetDocument")}),
                None,
            )
            out["delete"] = (edited({desc: None, f"{proc_dir}/Схема/Ext/Template.xml": None}), None)
            out["rename"] = (
                edited(
                    {
                        desc: None,
                        f"{proc_dir}/Схема/Ext/Template.xml": None,
                        f"{proc_dir}/Схема2.xml": a[desc].replace("<Name>Схема</Name>", "<Name>Схема2</Name>"),
                        f"{proc_dir}/Схема2/Ext/Template.xml": a[f"{proc_dir}/Схема/Ext/Template.xml"],
                    }
                ),
                None,
            )
            sheet = "Reports/ОтчетСКД/Templates/Печать.xml"
            out["type_to_dcs"] = (
                edited(
                    {
                        sheet: a[sheet].replace("SpreadsheetDocument", "DataCompositionSchema"),
                        "Reports/ОтчетСКД/Templates/Печать/Ext/Template.xml": _DCS_DOC,
                    }
                ),
                None,
            )
        else:
            dcs = f"{proc_dir}/Схема/Template.dcs"
            mdo = "DataProcessors/ОбрСКД/ОбрСКД.mdo"
            out["type_to_sheet"] = (
                edited(
                    {
                        dcs: None,
                        f"{proc_dir}/Схема/Template.mxlx": _SPREADSHEET,
                        mdo: a[mdo].replace(
                            "<name>Схема</name><templateType>DataCompositionSchema",
                            "<name>Схема</name><templateType>SpreadsheetDocument",
                        ),
                    }
                ),
                None,
            )
            out["delete"] = (edited({dcs: None}), None)
            out["rename"] = (
                edited(
                    {
                        dcs: None,
                        f"{proc_dir}/Схема2/Template.dcs": a[dcs],
                        mdo: a[mdo].replace("<name>Схема</name>", "<name>Схема2</name>"),
                    }
                ),
                None,
            )
            out["type_to_dcs"] = (
                edited(
                    {
                        "Reports/ОтчетСКД/Templates/Печать/Template.mxlx": None,
                        "Reports/ОтчетСКД/Templates/Печать/Template.dcs": _DCS_DOC,
                    }
                ),
                None,
            )
        return out

    @pytest.mark.parametrize(
        "edit", ["report_query", "document_query", "common_query", "type_to_sheet", "delete", "rename", "type_to_dcs"]
    )
    @pytest.mark.parametrize("route", ["full", "git"])
    def test_update_equals_build_template(self, world50, route, edit, tmp_path_factory, monkeypatch):
        files_b, pointwise = self._edits(world50["fmt"])[edit]
        _assert_update_equals_build(
            tmp_path_factory,
            monkeypatch,
            world50["write"],
            world50["files"],
            files_b,
            {"tq": _TQ_SQL},
            route=route,
            expect_pointwise=pointwise,
        )

    @staticmethod
    def _case_renames(fmt: str, what: str) -> tuple[list[tuple[str, str]], dict[str, tuple[str, str]]]:
        """Переименование только регистром: пути ``(было, стало)`` по порядку и правки имени в описателях (путь после
        переименования → (было, стало)) — как пишет выгрузка."""
        cf = fmt == "cf"
        if what in ("template", "document_template"):
            cat, owner, name = (
                ("Reports", "ОтчетСКД", "ОсновнаяСхема") if what == "template" else ("Documents", "Док", "СхемаДок")
            )
            tdir, low = f"{cat}/{owner}/Templates", name.lower()
            if cf:
                renames = [(f"{tdir}/{name}", f"{tdir}/{low}"), (f"{tdir}/{name}.xml", f"{tdir}/{low}.xml")]
                return renames, {f"{tdir}/{low}.xml": (name, low), f"{cat}/{owner}.xml": (name, low)}
            return [(f"{tdir}/{name}", f"{tdir}/{low}")], {f"{cat}/{owner}/{owner}.mdo": (name, low)}
        cat, name = ("Reports", "ОтчетСКД") if what == "owner" else ("CommonTemplates", "ОбщСКД")
        low = name.lower()
        if cf:
            return [(f"{cat}/{name}", f"{cat}/{low}"), (f"{cat}/{name}.xml", f"{cat}/{low}.xml")], {
                f"{cat}/{low}.xml": (name, low)
            }
        return [(f"{cat}/{name}", f"{cat}/{low}"), (f"{cat}/{low}/{name}.mdo", f"{cat}/{low}/{low}.mdo")], {
            f"{cat}/{low}/{low}.mdo": (name, low)
        }

    @staticmethod
    def _git_update_equals_build(world, files, renames, edits, tmp_path, monkeypatch) -> None:
        """Сборка A под git, переименования на диске (через промежуточное имя: смена только регистра) и правки текста,
        ``update`` git-путём; строки ``template_query`` равны свежей сборке того же дерева."""
        from test_git_delta import _git_init

        repo = tmp_path / "repo"
        base = repo / "cf"
        world["write"](base, files)
        _git_init(repo)
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
        db = IndexBuilder().build(str(base), build_calls=True)
        for old, new in renames:
            src = base / old
            temp = src.with_name(src.name + "__case")
            src.rename(temp)
            temp.rename(base / new)
        for rel, (old, new) in edits.items():
            path = base / rel
            path.write_bytes(path.read_bytes().replace(old.encode(), new.encode()))
        res = IndexBuilder().update(str(base))
        assert res["git_fast_path"] is True, res
        fresh = tmp_path / "fresh" / "cf"
        shutil.copytree(base, fresh)
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx_fresh"))
        assert _rows(db, _TQ_SQL) == _rows(IndexBuilder().build(str(fresh), build_calls=True), _TQ_SQL)

    # Смена только регистра владельца или макета: на регистронезависимой ФС (Windows) путь со старым написанием
    # открывает тот же файл, и выборочный ход git-пути записывал строки обоих написаний (`total` больше свежей сборки).
    # Кириллицу Git видит и с `core.ignorecase` — в дельте оба написания; макет документа — точечный путь Group A. На
    # регистрозависимой ФС тест проходит и без защиты.
    @pytest.mark.parametrize("what", ["template", "owner", "common", "document_template"])
    def test_case_only_rename_git(self, world50, what, tmp_path, monkeypatch):
        renames, edits = self._case_renames(world50["fmt"], what)
        self._git_update_equals_build(world50, world50["files"], renames, edits, tmp_path, monkeypatch)

    def test_case_rename_unseen_by_git(self, world50, tmp_path, monkeypatch):
        # Git с `core.ignorecase` смену регистра латиницы не видит: строки старого написания остаются в индексе, а
        # правка ДРУГОГО макета идёт выборочным ходом — он сверяет с диском и их (как шаг форм).
        fmt = world50["fmt"]
        files = {
            rel.replace("ОсновнаяСхема", "MainSchema"): text.replace("ОсновнаяСхема", "MainSchema")
            for rel, text in world50["files"].items()
        }
        tdir = "Reports/ОтчетСКД/Templates"
        renames = [(f"{tdir}/MainSchema", f"{tdir}/mainschema")]
        if fmt == "cf":
            renames.append((f"{tdir}/MainSchema.xml", f"{tdir}/mainschema.xml"))
        edits = {_content_rel(fmt, "CommonTemplates", "ОбщСКД"): ("Справочник.Спр", "Справочник.Скрытый")}
        assert "Справочник.Спр" in files[_content_rel(fmt, "CommonTemplates", "ОбщСКД")]
        self._git_update_equals_build(world50, files, renames, edits, tmp_path, monkeypatch)

    def test_full_scan_snapshot_template_domain(self, world50, tmp_path, monkeypatch):
        calls: list[str] = []
        real = BI._collect_template_queries

        def spy(base_path):
            calls.append(base_path)
            return real(base_path)

        monkeypatch.setattr(BI, "_collect_template_queries", spy)
        root = tmp_path / "repo" / "cf"
        world50["write"](root, world50["files"])
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
        db = IndexBuilder().build(str(root), build_calls=True)
        fmt = world50["fmt"]
        # (а) правка только запроса — домен перечитан
        main = root / _content_rel(fmt, "Reports/ОтчетСКД", "ОсновнаяСхема")
        main.write_bytes(main.read_bytes().replace("Справочник.Спр".encode(), "Справочник.Скр".encode()))
        calls.clear()
        res = IndexBuilder().update(str(root))
        assert res["git_fast_path"] is False and calls, (res, calls)
        assert ("Report.ОтчетСКД.Template.ОсновнаяСхема.Query", "Catalog.Скр") in {
            (r[4], r[2]) for r in _rows(db, _TQ_SQL)
        }
        before = _rows(db, _TQ_SQL)
        # (б) правка только описателя справочника — метаданные перечитаны, строки домена на месте
        cat = root / ("Catalogs/Скрытый.xml" if fmt == "cf" else "Catalogs/Скрытый/Скрытый.mdo")
        cat.write_bytes(cat.read_bytes().replace("Спр".encode(), "Сп2".encode()))
        calls.clear()
        res = IndexBuilder().update(str(root))
        assert res["git_fast_path"] is False and calls == [], (res, calls)
        assert _rows(db, _TQ_SQL) == before
        fresh = tmp_path / "fresh" / "cf"
        shutil.copytree(root, fresh)
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx_fresh"))
        assert _rows(IndexBuilder().build(str(fresh), build_calls=True), _TQ_SQL) == before

    # Порча файла содержимого: обрыв тега (`ParseError`) и объявление кодировки, которой разбор байтами не примет, —
    # неизвестное имя кодека (`LookupError`) и многобайтовая (`ValueError`); прежде два последних роняли сборку.
    _CORRUPTIONS = {
        "broken_tag": ("</DataCompositionSchema>", ""),
        "unknown_encoding": ('encoding="UTF-8"', 'encoding="x-unknown-codec"'),
        "multibyte_encoding": ('encoding="UTF-8"', 'encoding="Shift_JIS"'),
    }

    @pytest.mark.parametrize("corruption", sorted(_CORRUPTIONS))
    def test_malformed_template_skipped(self, world50, corruption, tmp_path, monkeypatch, caplog):
        files = dict(world50["files"])
        bad = _content_rel(world50["fmt"], "DataProcessors/ОбрСКД", "Схема")
        old, new = self._CORRUPTIONS[corruption]
        assert old in files[bad]
        files[bad] = files[bad].replace(old, new, 1)
        root = tmp_path / "repo" / "cf"
        world50["write"](root, files)
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
        with caplog.at_level("WARNING", logger="rlm_tools_bsl"):
            db = IndexBuilder().build(str(root), build_calls=True)
        used = {r[4] for r in _rows(db, _TQ_SQL)}
        assert "DataProcessor.ОбрСКД.Template.Схема.Query" not in used and "CommonTemplate.ОбщСКД.Query" in used
        assert any("template" in rec.getMessage().lower() and "Схема" in rec.getMessage() for rec in caplog.records)

    def test_data_path_common_template_edge(self, world50):
        res = world50["idx"]["find_data_path"]("ОбщийМакет.ОбщСКД", "Справочник.Спр")
        assert res["found"] is True and [e["kind"] for e in res["path"]] == [_TQ], res

    @pytest.mark.parametrize("meta", ["with_meta", "no_meta"])
    def test_generation_matrix_kinds(self, worlds50, worlds50_nometa, meta, tmp_path):
        for fmt in ("cf", "edt"):
            world = (worlds50 if meta == "with_meta" else worlds50_nometa)[fmt]
            old, reader = _v18_helpers(world, tmp_path / fmt)
            try:
                for gen, bsl in (("v19", world["idx"]), ("v18", old)):
                    m = bsl["find_references_to_object"]("ОбщаяКартинка.Картинка")["_meta"]
                    assert m["kinds_applied"] == sorted(_HEAD_KINDS_APPLIED + _NEW_KINDS), (fmt, meta, gen, m)
                    assert m["kinds_unavailable"] == ([] if gen == "v19" else _NEW_KINDS), (fmt, meta, gen, m)
                pic = old["find_references_to_object"]("CommonPictures/Картинка")["_meta"]
                assert pic["kinds_applied"] == sorted(_HEAD_KINDS_APPLIED + _NEW_KINDS)
                assert pic["kinds_unavailable"] == _NEW_KINDS
            finally:
                reader.close()


# ═══════════════ Задача 8 — analyze_object и find_custom_modifications по точному синониму (Д3; П2) ═══════════════

# Эталон HEAD `90cc326` (зонд кодом выгрузки на `world50`, индекс): фрагмент без синонима — прежний ближайший у обоих
# хелперов; поток документа на синониме, ссылке и гибриде — отпечатки ответов.
_FRAGMENT_HEAD = {"cf": "ae8e6d399cd65a55", "edt": "ef5f5733c0993003"}
_FLOW_HEAD = {
    "cf": {
        "Документ продажи": "c5abd6fead80ab95",
        "Документ.Док": "c3590f937ad72faf",
        "Документ.Документ продажи": "c5abd6fead80ab95",
    },
    "edt": {
        "Документ продажи": "0ee65820f18fd9ee",
        "Документ.Док": "26e79186e48840e1",
        "Документ.Документ продажи": "0ee65820f18fd9ee",
    },
}
_SYN_LEAD = "объекта с именем '{raw}' нет; ответ — по точному синониму: {ref}"


class TestAnalyzeObjectSynonym:
    def test_synonym_answers_object(self, world50):
        bsl = world50["idx"]
        hint = _SYN_LEAD.format(raw="Документ продажи", ref="Documents/Док")
        res = bsl["analyze_object"]("Документ продажи")
        assert (res.get("name"), res.get("category")) == ("Док", "Documents"), res
        assert "ObjectModule" in {m["module_type"] for m in res["modules"]}, res
        assert res["hint"].startswith(hint), res
        by_name = _strip_times(bsl["analyze_object"]("Док"))
        assert {k: v for k, v in _strip_times(res).items() if k != "hint"} == by_name, res
        mods = bsl["find_custom_modifications"]("Документ продажи", custom_prefixes=["х"])
        by_name_mods = bsl["find_custom_modifications"]("Док", custom_prefixes=["х"])
        assert mods["object_name"] == "Док" and mods["modules_analyzed"] == by_name_mods["modules_analyzed"] >= 1, mods
        assert mods["hint"].startswith(hint), mods

    def test_synonym_card_keeps_hint(self, world50):
        bsl = world50["idx"]
        hint = _SYN_LEAD.format(raw="Состояния заявки", ref="Enums/Статусы")
        res = bsl["analyze_object"]("Состояния заявки")
        assert (res.get("name"), res.get("category"), res.get("modules")) == ("Статусы", "Enums", []), res
        assert res["hint"].startswith(hint), res
        mods = bsl["find_custom_modifications"]("Состояния заявки", custom_prefixes=["х"])
        assert mods["object_name"] == "Статусы" and mods["hint"].startswith(hint), mods

    def test_exact_name_older_than_synonym(self, world50):
        """Обе версии: точное имя `Спр` (справочник) старше синонима «Спр» справочника `Скрытый`."""
        res = world50["idx"]["analyze_object"]("Спр")
        assert (res.get("name"), res.get("category")) == ("Спр", "Catalogs"), res
        assert "синониму" not in res.get("hint", ""), res

    def test_fragment_unchanged(self, world50):
        """Обе версии: фрагмент без синонима — прежний ближайший, прежняя пометка и прежний `object_name` (ввод)."""
        roots = (str(world50["root"].parent), str(world50["root"]))
        bsl = world50["idx"]
        frag = {
            "analyze_object": _norm_paths(_outcome(bsl, "analyze_object", "Балласт"), roots),
            "find_custom_modifications": _norm_paths(_outcome(bsl, "find_custom_modifications", "Балласт"), roots),
        }
        assert _digest(frag) == _FRAGMENT_HEAD[world50["fmt"]], frag

    def test_flow_unchanged(self, world50):
        """Обе версии: поток документа разрешает синоним сам — ответ прежний, пометка не двоится."""
        roots = (str(world50["root"].parent), str(world50["root"]))
        bsl = world50["idx"]
        got = {
            form: _digest(_norm_paths(_outcome(bsl, "analyze_document_flow", form), roots))
            for form in _FLOW_HEAD[world50["fmt"]]
        }
        assert got == _FLOW_HEAD[world50["fmt"]]
        flow = bsl["analyze_document_flow"]("Документ продажи")
        assert str(flow).count("по точному синониму") == 1, flow


# ═══════════════ Задача 9 — совет «проверьте имя» по фрагменту (Д4; П3, У6) ═══════════════


def _advice_call(text: str) -> str | None:
    """Аргумент совета ``search_objects('<…>')`` из текста подсказки либо ``None``."""
    import re

    m = re.search(r"search_objects\('([^']*)'\)", text)
    return m.group(1) if m else None


class TestNameCheckFragment:
    def test_typo_hint_fragment_found(self, world50):
        bsl = world50["idx"]
        hint = bsl["get_overrides"]("ДокЪ")["hint"]
        assert "Проверьте имя: search_objects('Док')" in hint, hint
        with pytest.raises(ValueError, match=r"проверьте имя: search_objects\('Док'\)") as exc:
            bsl["find_references_to_object"]("Документ.ДокЪ")
        query = _advice_call(str(exc.value))
        assert "Док" in {r["object_name"] for r in bsl["search_objects"](query)}

    def test_full_search_hit_keeps_text(self, world50):
        """Обе версии: имя, по которому поиск что-то находит, — прежний совет."""
        with pytest.raises(ValueError, match=r"проверьте имя: search_objects\('Спр'\)$"):
            world50["idx"]["find_references_to_object"]("Документ.Спр")

    def test_no_index_similar_by_fragment(self, world50):
        hint = world50["live"]["get_overrides"]("СпрЪЪ")["hint"]
        assert "Проверьте имя: похожие на диске — Catalogs/Спр" in hint, hint

    def test_both_empty_text(self, world50):
        hint = world50["idx"]["get_overrides"]("ЯЯЯЯ")["hint"]
        assert hint.endswith("Проверьте имя: похожих имен нет: search_objects('ЯЯЯ') пуст"), hint
        live = world50["live"]["get_overrides"]("ЯЯЯЯ")["hint"]
        assert live.endswith("Проверьте имя: похожих имен на диске нет"), live

    def test_data_path_and_profile_routes(self, world50):
        bsl = world50["idx"]
        res = bsl["find_data_path"]("Документ.ДокЪ", "Справочник.Спр")
        assert res["hint"] == "проверьте имя: search_objects('Док')", res
        assert "Док" in {r["object_name"] for r in bsl["search_objects"](_advice_call(res["hint"]))}
        prof = world50["live"]["get_object_profile"]("Документ.ДокЪ")
        assert "проверьте имя: похожие на диске — Documents/Док" in json.dumps(prof, ensure_ascii=False), prof
        assert "search_objects" not in json.dumps(prof, ensure_ascii=False), prof
        empty = bsl["find_data_path"]("Документ.ДокЪЪЪ", "Справочник.Спр")
        assert empty["hint"] == "проверьте имя: похожих имен нет: search_objects('ДокЪ') пуст", empty


# ═══════════════ Задача 10 — подсказка бюджета и пример рецепта find_data_path (Д5, Д6; П4, У7) ═══════════════

# Эталон HEAD `90cc326` (одинаков у CF и EDT): подсказка промаха на предельной глубине.
_DEPTH_HINT_HEAD = (
    "в пределах max_depth=1 путь не найден: обход дошел до предельной глубины и дальше не раскрывал — повтори с "
    "max_depth=2"
)
_NEW_EXAMPLE = "find_data_path('Документ.АвансовыйОтчет', 'Справочник.СтраныМира')"
# Длины текстов на HEAD (замер задачи 0.3, стейт-протокол): рецепт хелпера и full-рецепт темы «путь данных».
_RECIPE_LEN_HEAD = 1891
_TOPIC_FULL_LEN_HEAD = 789


class TestDataPathHints:
    def test_budget_hint_reverse_first(self, world50, monkeypatch):
        import rlm_tools_bsl.bsl_helpers as BH

        monkeypatch.setattr(BH, "_DATA_PATH_NODE_BUDGET", 1)
        res = world50["idx"]["find_data_path"]("Subsystem.Продажи", "Справочник.Скрытый")
        assert res["_meta"]["budget_exceeded"] is True, res
        hint = res["hint"]
        reverse = hint.find("find_references_to_object('Справочник.Скрытый')")
        assert 0 <= reverse < hint.find("kinds"), hint
        assert "уменьши max_depth" not in hint, hint

    def test_truncated_and_partial_hint_same_tail(self):
        from rlm_tools_bsl.bsl_helpers import _data_path_miss_hint

        tail = "Проверь ссылку обратным поиском: find_references_to_object('Справочник.X'); либо сузь kinds=[...]."
        truncated = _data_path_miss_hint(
            "Справочник.X", [{"object": "Role.Р", "refs_returned": 1000}], False, False, False
        )
        partial = _data_path_miss_hint("Справочник.X", [], False, True, False)
        budget = _data_path_miss_hint("Справочник.X", [], True, False, False)
        for text in (truncated, partial, budget):
            assert text.endswith(tail), text
        assert budget == (
            "found=False НЕ доказывает недостижимость. Обход уперся в бюджет узлов (400). Проверь ссылку обратным "
            "поиском: find_references_to_object('Справочник.X'); либо сузь kinds=[...]."
        )

    def test_depth_hint_unchanged(self, world50):
        res = world50["idx"]["find_data_path"]("Subsystem.Продажи", "Справочник.Скрытый", max_depth=1)
        assert res["hint"] == _DEPTH_HINT_HEAD, res

    def test_recipe_example_pair(self):
        from rlm_tools_bsl import bsl_knowledge as K
        from rlm_tools_bsl.bsl_helpers import build_helper_metadata_snapshot

        recipe = build_helper_metadata_snapshot()["find_data_path"]["recipe"]
        topic = "\n".join(K._BUSINESS_RECIPES["путь данных"]["full"])
        for text in (recipe, topic):
            assert _NEW_EXAMPLE in text, text
            assert "РегистрНакопления.Продажи" not in text and "сузь max_depth" not in text, text

    def test_recipe_lengths_not_grown(self):
        from rlm_tools_bsl import bsl_knowledge as K
        from rlm_tools_bsl.bsl_helpers import build_helper_metadata_snapshot

        assert len(build_helper_metadata_snapshot()["find_data_path"]["recipe"]) <= _RECIPE_LEN_HEAD
        assert sum(map(len, K._BUSINESS_RECIPES["путь данных"]["full"])) <= _TOPIC_FULL_LEN_HEAD

    def test_helpers_doc_names_the_hint_route(self):
        """Статья `find_data_path` в docs/HELPERS.md называет маршрут неокончательного промаха словами подсказки
        (обратный поиск первым, затем `kinds`) и не советует сужать `max_depth` — подсказка этого не делает."""
        from rlm_tools_bsl.bsl_helpers import _data_path_miss_hint

        hint = _data_path_miss_hint("<to>", [], True, False, False)
        tail = hint[hint.index("Проверь ссылку") :].rstrip(".")
        doc = (Path(__file__).resolve().parents[1] / "docs" / "HELPERS.md").read_text(encoding="utf-8")
        article = next(line for line in doc.splitlines() if line.startswith("- `find_data_path(")).replace("`", "")
        assert tail in article, tail
        assert "kinds/max_depth" not in article

    def test_recipe_names_reverse_search_first(self):
        """Рецепт `find_data_path` называет маршрут неокончательного промаха в порядке подсказки (П4): обратный поиск
        первым, затем сужение `kinds` (прежде рецепт ставил сужение первым)."""
        from rlm_tools_bsl.bsl_helpers import build_helper_metadata_snapshot

        recipe = " ".join(build_helper_metadata_snapshot()["find_data_path"]["recipe"].replace("#", " ").split())
        route = recipe[recipe.index("Маршрут:") :]
        assert route.index("обратным поиском") < route.index("сузь kinds"), route[:160]


# ═══════════════ Задача 11 — метаполя на git-пути (Д11; П11) ═══════════════

_META_COUNTS_SQL = (
    "SELECT key, value FROM index_meta WHERE key IN ('object_attributes_count', 'predefined_items_count')"
)


def _meta_counts(db) -> dict[str, str]:
    return dict(_rows(db, _META_COUNTS_SQL))


def _table_counts(db) -> dict[str, str]:
    con = sqlite3.connect(str(db))
    try:
        return {
            "object_attributes_count": str(con.execute("SELECT COUNT(*) FROM object_attributes").fetchone()[0]),
            "predefined_items_count": str(con.execute("SELECT COUNT(*) FROM predefined_items").fetchone()[0]),
        }
    finally:
        con.close()


_CF_PREDEFINED = (
    '<?xml version="1.0" encoding="UTF-8"?>\n<PredefinedData xmlns="http://v8.1c.ru/8.3/MDClasses">'
    '<Item id="00000000-0000-0000-0000-000000000011"><Name>Основной</Name><Code>000000001</Code>'
    "<Description>Основной</Description><IsFolder>false</IsFolder></Item></PredefinedData>\n"
)
_EDT_PREDEFINED = (
    "<predefined><items><name>Основной</name><code>000000001</code><description>Основной</description>"
    "<isFolder>false</isFolder></items></predefined>"
)


class TestMetaCountsGit:
    def _edit_catalog(self, files: dict[str, str], fmt: str) -> dict[str, str]:
        b = dict(files)
        if fmt == "cf":
            rel = "Catalogs/Спр.xml"
            b[rel] = b[rel].replace(
                "</Properties>", "</Properties><ChildObjects>" + _cf_attr("Новый", "xs:string") + "</ChildObjects>", 1
            )
            b["Catalogs/Спр/Ext/Predefined.xml"] = _CF_PREDEFINED
        else:
            rel = "Catalogs/Спр/Спр.mdo"
            b[rel] = b[rel].replace(
                "</mdclass:Catalog>", _edt_attr("Новый", "String") + _EDT_PREDEFINED + "</mdclass:Catalog>"
            )
        return b

    def test_git_update_recounts_meta(self, world50, tmp_path_factory, monkeypatch):
        fmt = world50["fmt"]
        files_b = self._edit_catalog(world50["files"], fmt)
        db = _assert_update_equals_build(
            tmp_path_factory,
            monkeypatch,
            world50["write"],
            world50["files"],
            files_b,
            {"meta": _META_COUNTS_SQL},
            route="git",
            expect_pointwise=("Catalogs", "Спр"),
        )
        assert _meta_counts(db) == _table_counts(db), _meta_counts(db)
        # bulk-fallback: реквизит отчёта (категория вне точечного пути)
        rep = "Reports/ОтчетСКД.xml" if fmt == "cf" else "Reports/ОтчетСКД/ОтчетСКД.mdo"
        files_c = dict(world50["files"])
        if fmt == "cf":
            files_c[rep] = files_c[rep].replace("<ChildObjects>", "<ChildObjects>" + _cf_attr("Период", "xs:string"), 1)
        else:
            files_c[rep] = files_c[rep].replace("<templates>", _edt_attr("Период", "String") + "<templates>", 1)
        db = _assert_update_equals_build(
            tmp_path_factory,
            monkeypatch,
            world50["write"],
            world50["files"],
            files_c,
            {"meta": _META_COUNTS_SQL},
            route="git",
        )
        assert _meta_counts(db) == _table_counts(db), _meta_counts(db)

    def test_bsl_only_update_keeps_meta(self, world50, tmp_path_factory, monkeypatch):
        """Обе версии: правка только `.bsl` метаполей не пишет."""
        from test_git_delta import _git_init

        repo = tmp_path_factory.mktemp("meta_bsl") / "repo"
        base = repo / "cf"
        world50["write"](base, world50["files"])
        _git_init(repo)
        db = IndexBuilder().build(str(base), build_calls=True)
        con = sqlite3.connect(str(db))
        try:
            con.executemany(
                "UPDATE index_meta SET value='999' WHERE key=?",
                [("object_attributes_count",), ("predefined_items_count",)],
            )
            con.commit()
        finally:
            con.close()
        mod = base / _module_path(world50["fmt"], "Documents", "Док", "ObjectModule.bsl")
        mod.write_text(mod.read_text(encoding="utf-8") + "\nПроцедура Новая()\nКонецПроцедуры\n", encoding="utf-8")
        res = IndexBuilder().update(str(base))
        assert res["git_fast_path"] is True, res
        assert _meta_counts(db) == {"object_attributes_count": "999", "predefined_items_count": "999"}


# ═══════════════ Задача 12 — объявленные границы: значение заполнения, роли с одними запретами (Д12, Д13; П6, П8, У5) ═══════════════

_DENIAL_HINT_HEAD = "в ролях этой конфигурации прав на объект нет"
# Эталон HEAD `90cc326` (одинаков у CF и EDT): поле `object` пустого ответа `find_roles`; прочие поля —
# `roles=[]`, `match='substring'`, `case_sensitive` (индекс — False, живьём — True), `flags_available=True`, без `hint`.
_EMPTY_ROLES_HEAD_OBJECT = {
    "Документ.Док.ТабличнаяЧасть": "Док.ТабличнаяЧасть",
    "Документ.Catalogs/Спр": "Catalogs/Спр",
    "ОбщийМодуль.Задание": "Задание",
    "НетТакогоОбъекта": "НетТакогоОбъекта",
}


def _empty_roles_head(arg: str, case_sensitive: bool) -> dict:
    return {
        "object": _EMPTY_ROLES_HEAD_OBJECT[arg],
        "roles": [],
        "match": "substring",
        "case_sensitive": case_sensitive,
        "flags_available": True,
    }


def _extension_alone(world, tmp_path: Path):
    """Сессия на самом расширении без основной рядом (корень основной не передан): CF — `Расш1` мира, EDT — малое
    дерево расширения с заимствованным документом `Док` (ролей в расширении нет)."""
    if world["fmt"] == "cf":
        ext_root = world["ext_root"]
    else:
        ext_root = _objects_tree(
            tmp_path / "cfe" / "Расш1",
            "edt",
            [
                (
                    "Documents",
                    "Док",
                    {"Ext/ObjectModule.bsl": _EXT_DOC_MODULE},
                    {"edt": "<objectBelonging>Adopted</objectBelonging>"},
                )
            ],
            EDT_EXT_DESCRIPTOR,
        )
    return _bsl_for(
        ext_root, None, current_config_role="extension", current_config_name="Расш1", current_config_root=str(ext_root)
    )


class TestDeclaredBoundaries:
    def test_fill_value_no_row(self, world50):
        """Граница: значение заполнения реквизита строкой ссылки не выдаётся; тип реквизита — строка `attribute_type`."""
        res = world50["idx"]["find_references_to_object"]("Перечисление.Статусы")
        used = {r["used_in"] for r in res["references"]}
        assert not [u for u in used if "FillValue" in u or "fillValue" in u], used
        assert "Document.Док.Attribute.Статус.Type" in used, used

    def test_denial_only_role_absent(self, world50):
        for route in ("idx", "live"):
            roles = {r["role_name"] for r in world50[route]["find_roles"]("Документ.Док")["roles"]}
            assert "ТолькоЗапреты" not in roles and "ПолныеПрава" in roles, (route, roles)

    @pytest.mark.parametrize("arg", ["Справочник.Скрытый", "Скрытый"])
    def test_empty_roles_hint(self, world50, arg):
        for route in ("idx", "live"):
            res = world50[route]["find_roles"](arg)
            assert res["roles"] == [] and res.get("hint", "").startswith(_DENIAL_HINT_HEAD), (route, res)

    def test_nonempty_roles_no_new_hint(self, world50):
        for route in ("idx", "live"):
            res = world50[route]["find_roles"]("Документ.Док")
            assert res["roles"] and not res.get("hint", "").startswith(_DENIAL_HINT_HEAD), (route, res)

    @pytest.mark.parametrize("arg", list(_EMPTY_ROLES_HEAD_OBJECT))
    def test_empty_roles_without_boundary_hint(self, world50, arg):
        """Обе версии: путь члена, не-ссылка без объекта, вид без прав в ролях, голое имя без объекта — ответ HEAD."""
        for route, case_sensitive in (("idx", False), ("live", True)):
            assert _strip_times(world50[route]["find_roles"](arg)) == _empty_roles_head(arg, case_sensitive), route

    def test_extension_session_hint(self, world50, tmp_path):
        bsl = _extension_alone(world50, tmp_path)
        assert _strip_times(bsl["find_roles"]("НетТакогоОбъекта")) == _empty_roles_head("НетТакогоОбъекта", True)
        res = bsl["find_roles"]("Док")
        assert res["roles"] == [] and res.get("hint", "").startswith(_DENIAL_HINT_HEAD), res
