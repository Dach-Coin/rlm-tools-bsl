"""v1.36.0 — «честный состав и исполнимый упор».

Тесты сгруппированы по задачам релиза. Общий принцип: проверяется не «ключ есть»,
а СМЫСЛ контракта — что именно ответ обещает и чего он НЕ обещает.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import types

import pytest

from rlm_tools_bsl.bsl_helpers import make_bsl_helpers
from rlm_tools_bsl.bsl_index import IndexBuilder, IndexReader
from rlm_tools_bsl.format_detector import detect_format
from rlm_tools_bsl.helpers import make_helpers


# ---------------------------------------------------------------------------
# Фикстуры
# ---------------------------------------------------------------------------
def _write(path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


_CF_DESCRIPTOR = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">\n'
    '  <Configuration uuid="00000000-0000-0000-0000-000000000001">\n'
    "    <Properties><Name>Тест</Name></Properties>\n"
    "  </Configuration>\n"
    "</MetaDataObject>\n"
)


def _subsystem_xml(name: str, content: list[str], synonym: str = "") -> str:
    """Валидный CF-XML подсистемы, включая пустой ``<Content/>``.

    Раскладка — как в реальной выгрузке Конфигуратора: состав лежит в
    ``<Properties><Content><xr:Item>…</xr:Item></Content></Properties>``.
    """
    if synonym:
        syn = (
            "            <Synonym><v8:item><v8:lang>ru</v8:lang>"
            f"<v8:content>{synonym}</v8:content></v8:item></Synonym>\n"
        )
    else:
        syn = "            <Synonym/>\n"
    if content:
        items = "".join(f"                <xr:Item>{c}</xr:Item>\n" for c in content)
        block = f"            <Content>\n{items}            </Content>\n"
    else:
        block = "            <Content/>\n"
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"\n'
        '                xmlns:v8="http://v8.1c.ru/8.1/data/core"\n'
        '                xmlns:xr="http://v8.1c.ru/8.3/xcf/readable">\n'
        "    <Subsystem>\n"
        "        <Properties>\n"
        f"            <Name>{name}</Name>\n"
        f"{syn}"
        f"{block}"
        "        </Properties>\n"
        "    </Subsystem>\n"
        "</MetaDataObject>\n"
    )


def _make_bsl(root, *, idx_reader=None, extension_paths=None, private_io=False, **kw):
    """Прямая фабрика. ``private_io=True`` эмулирует production-песочницу:
    именно она передаёт публичные status-aware каналы.

    Возвращает ОБЪЕДИНЁННЫЙ словарь ``{**generic, **bsl}`` (поправка R45):
    ``make_bsl_helpers`` отдаёт только BSL-хелперы, а generic ``grep`` /
    ``glob_files`` / ``read_file`` живут в словаре ``make_helpers``. Ключи двух
    словарей не пересекаются (``grep`` != ``safe_grep``), объединение безопасно.

    FS-only ``glob_files_fs`` передаётся ВСЕГДА (R105): он нужен не для нового
    режима, а чтобы live-ветка при подключённом ридере доказуемо перечисляла
    текущий корень, а не ``file_paths`` этого ридера. В agent-facing словарь
    содержимое sink не попадает.
    """
    sink: dict = {}
    helpers, resolve_safe = make_helpers(str(root), idx_reader=idx_reader, _private_io=sink)
    extra = {}
    if private_io:
        extra["grep_status_fn"] = sink["grep_with_status"]
        extra["catalog_scan_fn"] = sink["scan_bsl_catalog_status"]
    bsl = make_bsl_helpers(
        base_path=str(root),
        resolve_safe=resolve_safe,
        read_file_fn=helpers["read_file"],
        grep_fn=helpers["grep"],
        glob_files_fn=helpers["glob_files"],
        glob_files_fs_fn=sink["glob_files_fs"],
        format_info=detect_format(str(root)),
        idx_reader=idx_reader,
        extension_paths=extension_paths,
        **extra,
        **kw,
    )
    return {**helpers, **bsl}


_FIXTURE_MODULE_BSL = "Процедура Фикстура() Экспорт\nКонецПроцедуры\n"


def _build_reader(root):
    """Построить индекс и открыть ридер.

    Перед build гарантируется один нейтральный BSL-модуль: при ``total_files == 0``
    билдер идёт по ранней ветке и НЕ собирает metadata-таблицы даже при
    ``build_metadata=True``.
    """
    if not any(root.rglob("*.bsl")):
        _write(root / "CommonModules" / "Фикстура" / "Ext" / "Module.bsl", _FIXTURE_MODULE_BSL)
    db = IndexBuilder().build(str(root), build_calls=False, build_metadata=True)
    return IndexReader(db)


def _ns(root, reader, **extra):
    return types.SimpleNamespace(root=root, reader=reader, **extra)


@pytest.fixture
def cf_indexed(tmp_path, monkeypatch):
    """Подсистема с составом из объекта-однофамильца и объекта с чужим именем."""
    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
    root = tmp_path / "cf"
    _write(root / "Configuration.xml", _CF_DESCRIPTOR)
    _write(
        root / "Subsystems" / "ПодсистемаА.xml",
        _subsystem_xml("ПодсистемаА", ["CommonModule.ПодсистемаАСервер", "Document.ЧужоеИмя"]),
    )
    _write(root / "CommonModules" / "ПодсистемаАСервер" / "Ext" / "Module.bsl", _FIXTURE_MODULE_BSL)
    _write(root / "Documents" / "ЧужоеИмя" / "Ext" / "ObjectModule.bsl", _FIXTURE_MODULE_BSL)
    reader = _build_reader(root)
    try:
        yield _ns(root, reader)
    finally:
        reader.close()


@pytest.fixture
def built_index_env(cf_indexed):
    """Алиас ``cf_indexed`` — то же дерево под именем, которым его читают reader-тесты."""
    return cf_indexed


@pytest.fixture
def cf_big_subsystem(tmp_path, monkeypatch):
    """Подсистема `Большая` с 25 элементами состава."""
    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
    root = tmp_path / "cf"
    _write(root / "Configuration.xml", _CF_DESCRIPTOR)
    _write(
        root / "Subsystems" / "Большая.xml",
        _subsystem_xml("Большая", [f"Document.Объект{i:02d}" for i in range(25)]),
    )
    reader = _build_reader(root)
    try:
        yield _ns(root, reader)
    finally:
        reader.close()


@pytest.fixture
def cf_many_subsystem_matches(tmp_path, monkeypatch):
    """Точная `Маркер` (4 объекта, ни один не содержит `Маркер`) + 12 подсистем по 30.

    В каждой из 12 ровно одна ссылка содержит `Маркер`. Каждый состав меньше
    default ``limit=200``, но суммарно 364 — больше него: фикстура доказывает, что
    бюджет общий для ответа, direct-строка расходует его первой, а обратные строки
    не разворачивают весь состав.
    """
    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
    root = tmp_path / "cf"
    _write(root / "Configuration.xml", _CF_DESCRIPTOR)
    _write(
        root / "Subsystems" / "Маркер.xml",
        _subsystem_xml("Маркер", [f"Document.Прямой{i}" for i in range(4)]),
    )
    for n in range(12):
        content = [f"Document.Обычный{n}_{i:02d}" for i in range(29)]
        content.append(f"Document.МаркерВходящий{n}")
        _write(root / "Subsystems" / f"Иная{n:02d}.xml", _subsystem_xml(f"Иная{n:02d}", content))
    reader = _build_reader(root)
    try:
        yield _ns(root, reader)
    finally:
        reader.close()


@pytest.fixture
def cf_subsystem_output_budget(tmp_path, monkeypatch):
    """Два независимых репро JSON-упора в ОДНОМ статичном дереве.

    * `Широкая` — 200 refs обычной длины, ни один не содержит `Широкая`:
      одна строка уже тяжелее 15K из-за `raw_content` + classified-проекции;
    * 100 подсистем `ОбратнаяNNN` с одной ссылкой `Document.МаркерNNN`:
      оболочки чистых reverse-строк тяжелее 15K даже при `limit=1`.
    """
    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
    root = tmp_path / "cf"
    _write(root / "Configuration.xml", _CF_DESCRIPTOR)
    _write(
        root / "Subsystems" / "Широкая.xml",
        _subsystem_xml("Широкая", [f"Document.ЭлементСостава{i:03d}" for i in range(200)]),
    )
    for n in range(100):
        _write(
            root / "Subsystems" / f"Обратная{n:03d}.xml",
            _subsystem_xml(f"Обратная{n:03d}", [f"Document.Маркер{n:03d}"]),
        )
    reader = _build_reader(root)
    try:
        yield _ns(root, reader)
    finally:
        reader.close()


@pytest.fixture
def cf_case_variants(tmp_path, monkeypatch):
    """`Почта` и `почта` — два файла в РАЗНЫХ каталогах (R47).

    В одном каталоге на Windows это ОДИН путь, и второй файл перезаписал бы первый.
    Вложенная подсистема — штатная CF-раскладка: билдер обходит `Subsystems` через
    `rglob`.
    """
    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
    root = tmp_path / "cf"
    _write(root / "Configuration.xml", _CF_DESCRIPTOR)
    _write(root / "Subsystems" / "Почта.xml", _subsystem_xml("Почта", ["Document.ПисьмоВерхнее"]))
    _write(
        root / "Subsystems" / "Родитель" / "Subsystems" / "почта.xml",
        _subsystem_xml("почта", ["Document.ПисьмоНижнее"]),
    )
    reader = _build_reader(root)
    try:
        yield _ns(root, reader)
    finally:
        reader.close()


@pytest.fixture
def cf_name_fragment(tmp_path, monkeypatch):
    """`Почта` и `ВстроеннаяПочта` в РАЗНЫХ файлах; `Почта` НЕ входит в состав второй."""
    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
    root = tmp_path / "cf"
    _write(root / "Configuration.xml", _CF_DESCRIPTOR)
    _write(root / "Subsystems" / "Почта.xml", _subsystem_xml("Почта", ["Document.Письмо"]))
    _write(
        root / "Subsystems" / "ВстроеннаяПочта.xml",
        _subsystem_xml("ВстроеннаяПочта", ["Document.ВходящееПисьмо"]),
    )
    reader = _build_reader(root)
    try:
        yield _ns(root, reader)
    finally:
        reader.close()


@pytest.fixture
def cf_synonym(tmp_path, monkeypatch):
    """`<Name>ктнСпецодежда</Name>`, `<Synonym>Спецодежда</Synonym>`, непустой Content."""
    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
    root = tmp_path / "cf"
    _write(root / "Configuration.xml", _CF_DESCRIPTOR)
    _write(
        root / "Subsystems" / "ктнСпецодежда.xml",
        _subsystem_xml("ктнСпецодежда", ["Catalog.Номенклатура"], synonym="Спецодежда"),
    )
    reader = _build_reader(root)
    try:
        yield _ns(root, reader)
    finally:
        reader.close()


@pytest.fixture
def cf_empty_subsystem(tmp_path, monkeypatch):
    """`<Name>Пустая</Name>`, `<Synonym>Пустая подсистема</Synonym>`, `<Content/>`.

    Предусловие: 0 строк в `subsystem_content`, XML — в `file_paths`, синоним — в
    `object_synonyms`. Синоним, которого нет в basename файла, задаёт отрицательную
    границу: релиз не добавляет глобальный поиск по произвольному бизнес-синониму.
    """
    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
    root = tmp_path / "cf"
    _write(root / "Configuration.xml", _CF_DESCRIPTOR)
    _write(
        root / "Subsystems" / "Пустая.xml",
        _subsystem_xml("Пустая", [], synonym="Пустая подсистема"),
    )
    reader = _build_reader(root)
    try:
        yield _ns(root, reader)
    finally:
        reader.close()


@pytest.fixture
def cf_edt_empty_subsystem(tmp_path, monkeypatch):
    """EDT-раскладка: `Subsystems/ЕДТПустая/ЕДТПустая.mdo`, `<Content/>`, БЕЗ Synonym.

    Обязательна (поправка V2): предикат кандидатов по `dir_path='Subsystems'` на
    этой раскладке даёт НОЛЬ, а CF-фикстура регресс не ловит. Пустой синоним тоже
    не случаен: строка в `object_synonyms` появляется ТОЛЬКО при непустом
    `<Synonym>`, то есть второй источник кандидатов здесь молчит.
    """
    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
    root = tmp_path / "edt"
    _write(root / "Configuration" / "Configuration.mdo", _EDT_CONFIG_MDO)
    _write(
        root / "Subsystems" / "ЕДТПустая" / "ЕДТПустая.mdo",
        _edt_subsystem_mdo("ЕДТПустая", []),
    )
    reader = _build_reader(root)
    try:
        yield _ns(root, reader)
    finally:
        reader.close()


_EDT_CONFIG_MDO = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<mdclass:Configuration xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass"\n'
    '    uuid="00000000-0000-0000-0000-000000000002" name="ТестEDT">\n'
    "</mdclass:Configuration>\n"
)


def _edt_subsystem_mdo(name: str, content: list[str], synonym: str = "") -> str:
    """`.mdo` подсистемы: `<name>` и `<content>` — ПРЯМЫЕ дочерние элементы корня."""
    syn = f"  <synonym><key>ru</key><value>{synonym}</value></synonym>\n" if synonym else ""
    body = "".join(f"  <content>{c}</content>\n" for c in content)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<mdclass:Subsystem xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass"\n'
        '    uuid="00000000-0000-0000-0000-0000000000aa">\n'
        f"  <name>{name}</name>\n"
        f"{syn}{body}"
        "</mdclass:Subsystem>\n"
    )


@pytest.fixture
def cf_edt_and_cf(tmp_path, monkeypatch):
    """Одно дерево с четырьмя раскладками сразу — для теста F1/R113."""
    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
    root = tmp_path / "mix"
    _write(root / "Configuration.xml", _CF_DESCRIPTOR)
    _write(
        root / "Subsystems" / "ВерхнегоУровня.xml",
        _subsystem_xml("ВерхнегоУровня", ["Document.Верхний"]),
    )
    _write(
        root / "Subsystems" / "Родитель" / "Subsystems" / "Вложенная.xml",
        _subsystem_xml("Вложенная", ["Document.Вложенный"]),
    )
    _write(
        root / "Subsystems" / "ЕДТПодсистема" / "ЕДТПодсистема.mdo",
        _edt_subsystem_mdo("ЕДТПодсистема", ["Document.ЕДТОбъект"]),
    )
    reader = _build_reader(root)
    try:
        yield _ns(root, reader)
    finally:
        reader.close()


@pytest.fixture
def cf_foreign_subsystem_index(tmp_path, monkeypatch):
    """Два статичных корня: reader построен от B, `.root` указывает на A."""
    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
    root_a = tmp_path / "A"
    root_b = tmp_path / "B"
    for root, only in ((root_a, "ТолькоА"), (root_b, "ТолькоБ")):
        _write(root / "Configuration.xml", _CF_DESCRIPTOR)
        _write(root / "Subsystems" / f"{only}.xml", _subsystem_xml(only, [f"Document.{only}Объект"]))
    reader = _build_reader(root_b)
    try:
        yield types.SimpleNamespace(root=root_a, reader=reader, foreign_root=root_b)
    finally:
        reader.close()


def _wrapper_tree(wrap):
    """Поддержанный wrapper-вход: base — контейнер, current root — `MyExt` внутри него.

    Рядом лежит `Other` — ВТОРАЯ конфигурация того же контейнера с ОДНОИМЁННОЙ
    подсистемой. Она и вскрывает подмену домена: её состав к текущему корню не
    относится ни в каком смысле.
    """
    _write(wrap / "MyExt" / "Configuration.xml", _CF_DESCRIPTOR)
    _write(wrap / "MyExt" / "Subsystems" / "Почта.xml", _subsystem_xml("Почта", ["Document.СвойОбъект"]))
    _write(wrap / "MyExt" / "Documents" / "СвойОбъект" / "Ext" / "ObjectModule.bsl", _FIXTURE_MODULE_BSL)
    _write(wrap / "Other" / "Configuration.xml", _CF_DESCRIPTOR)
    _write(wrap / "Other" / "Subsystems" / "Почта.xml", _subsystem_xml("Почта", ["Document.ЧужойОбъект"]))
    _write(wrap / "Other" / "Documents" / "ЧужойОбъект" / "Ext" / "ObjectModule.bsl", _FIXTURE_MODULE_BSL)


_WRAPPER_KW = {
    "current_config_role": "extension",
    "current_config_name": "MyExt",
}


@pytest.fixture
def cf_wrapper_indexed(tmp_path, monkeypatch):
    """Wrapper-вход С индексом: ридер построен от КОНТЕЙНЕРА, шире current root."""
    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
    wrap = tmp_path / "wrap"
    _wrapper_tree(wrap)
    reader = _build_reader(wrap)
    try:
        yield _ns(wrap, reader, current_root=wrap / "MyExt")
    finally:
        reader.close()


@pytest.fixture
def cf_wrapper_live(tmp_path):
    """Тот же wrapper БЕЗ индекса: живая ветка обязана видеть текущий корень."""
    wrap = tmp_path / "wrap"
    _wrapper_tree(wrap)
    return _ns(wrap, None, current_root=wrap / "MyExt")


@pytest.fixture
def edt_pure(tmp_path, monkeypatch):
    """ЧИСТО EDT-дерево: и подсистема, и BSL-модуль в EDT-раскладке.

    Отдельная фикстура нужна потому, что `_build_reader` дописывает модуль в
    CF-раскладке (`CommonModules/X/Ext/Module.bsl`), а смешанное дерево
    `detect_format` честно объявляет `unknown` — и тест ветки EDT стал бы
    вакуумным, проверяя дефолтную CF-ветку.
    """
    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
    root = tmp_path / "edt"
    _write(root / "Configuration" / "Configuration.mdo", _EDT_CONFIG_MDO)
    _write(root / "CommonModules" / "ЕДТМодуль" / "Module.bsl", _FIXTURE_MODULE_BSL)
    _write(
        root / "Subsystems" / "ЕДТСостав" / "ЕДТСостав.mdo",
        _edt_subsystem_mdo("ЕДТСостав", ["Document.ЕДТОбъект"]),
    )
    assert detect_format(str(root)).primary_format.value == "edt", "фикстура обязана быть чистым EDT"
    reader = _build_reader(root)
    try:
        yield _ns(root, reader)
    finally:
        reader.close()


@pytest.fixture
def git_wrapper_output_budget(tmp_path, monkeypatch):
    """Wrapper ПОД GIT и с упором вывода: только здесь виден git-маршрут из hint.

    Прежний wrapper-тест этого дефекта увидеть не мог по двум причинам сразу:
    его дерево не было git work-tree (значит `git_search` не регистрировался
    вовсе), и ответ помещался в бюджет (значит output-hint не выдавался).
    """
    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
    wrap = tmp_path / "wrap"
    _wrapper_tree(wrap)
    # 200 refs: одна строка тяжелее бюджета вывода — hint с маршрутами добора выйдет наружу.
    _write(
        wrap / "MyExt" / "Subsystems" / "Широкая.xml",
        _subsystem_xml("Широкая", [f"Document.ЭлементСостава{i:03d}" for i in range(200)]),
    )
    _git_init(wrap)
    reader = _build_reader(wrap)
    try:
        yield _ns(wrap, reader, current_root=wrap / "MyExt")
    finally:
        reader.close()


@pytest.fixture
def cf_two_branch_filling(tmp_path):
    """`Тип("ДокументСсылка.ДваждыПроверяемый")` в ДВУХ ветках `Если`."""
    root = tmp_path / "cf"
    _write(root / "Configuration.xml", _CF_DESCRIPTOR)
    _write(
        root / "Documents" / "ЦелевойДок" / "Ext" / "ObjectModule.bsl",
        "Процедура ОбработкаЗаполнения(ДанныеЗаполнения, СтандартнаяОбработка)\n"
        '    Если ТипЗнч(ДанныеЗаполнения) = Тип("ДокументСсылка.ДваждыПроверяемый") Тогда\n'
        "        Возврат;\n"
        '    ИначеЕсли ТипЗнч(ДанныеЗаполнения) = Тип("ДокументСсылка.Однажды") Тогда\n'
        "        Возврат;\n"
        "    КонецЕсли;\n"
        '    Если ТипЗнч(ДанныеЗаполнения) = Тип("ДокументСсылка.ДваждыПроверяемый") Тогда\n'
        "        Возврат;\n"
        "    КонецЕсли;\n"
        "КонецПроцедуры\n",
    )
    return types.SimpleNamespace(root=root)


@pytest.fixture
def cf_dup_manager_commands(tmp_path):
    """`Документы.Цель.ДобавитьКомандуСозданияНаОсновании` вызвано дважды."""
    root = tmp_path / "cf"
    _write(root / "Configuration.xml", _CF_DESCRIPTOR)
    _write(
        root / "Documents" / "Источник" / "Ext" / "ManagerModule.bsl",
        "Процедура ДобавитьКомандыСозданияНаОсновании(КомандыСоздания) Экспорт\n"
        "    Документы.Цель.ДобавитьКомандуСозданияНаОсновании(КомандыСоздания);\n"
        "    Если Ложь Тогда\n"
        "        Документы.Цель.ДобавитьКомандуСозданияНаОсновании(КомандыСоздания);\n"
        "    КонецЕсли;\n"
        "КонецПроцедуры\n",
    )
    return types.SimpleNamespace(root=root)


_BASED_ON_DOC_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"\n'
    '                xmlns:v8="http://v8.1c.ru/8.1/data/core"\n'
    '                xmlns:xr="http://v8.1c.ru/8.3/xcf/readable">\n'
    "    <Document>\n"
    "        <Properties>\n"
    "            <Name>Цель</Name>\n"
    "            <Synonym/>\n"
    "            <BasedOn>\n"
    "                <xr:Item>Document.Источник</xr:Item>\n"
    "            </BasedOn>\n"
    "        </Properties>\n"
    "    </Document>\n"
    "</MetaDataObject>\n"
)


@pytest.fixture
def cf_indexed_basedon(tmp_path, monkeypatch):
    """`cf_dup_manager_commands` + `<BasedOn>` у `Цель` + собранный индекс."""
    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
    root = tmp_path / "cf"
    _write(root / "Configuration.xml", _CF_DESCRIPTOR)
    _write(
        root / "Documents" / "Источник" / "Ext" / "ManagerModule.bsl",
        "Процедура ДобавитьКомандыСозданияНаОсновании(КомандыСоздания) Экспорт\n"
        "    Документы.Цель.ДобавитьКомандуСозданияНаОсновании(КомандыСоздания);\n"
        "    Если Ложь Тогда\n"
        "        Документы.Цель.ДобавитьКомандуСозданияНаОсновании(КомандыСоздания);\n"
        "    КонецЕсли;\n"
        "КонецПроцедуры\n",
    )
    _write(root / "Documents" / "Цель" / "Ext" / "Document.xml", _BASED_ON_DOC_XML)
    _write(root / "Documents" / "Цель" / "Ext" / "ObjectModule.bsl", _FIXTURE_MODULE_BSL)
    reader = _build_reader(root)
    try:
        yield _ns(root, reader)
    finally:
        reader.close()


_CFE_DESCRIPTOR = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">\n'
    '  <Configuration uuid="00000000-0000-0000-0000-000000000003">\n'
    "    <Properties><Name>РасширениеТест</Name>\n"
    "      <ConfigurationExtensionPurpose>Customization</ConfigurationExtensionPurpose>\n"
    "      <NamePrefix>ртт</NamePrefix>\n"
    "    </Properties>\n"
    "  </Configuration>\n"
    "</MetaDataObject>\n"
)

_OBJECT_MODULE_BOTH = (
    "Процедура ОбработкаЗаполнения(ДанныеЗаполнения, СтандартнаяОбработка)\n"
    '    Если ТипЗнч(ДанныеЗаполнения) = Тип("ДокументСсылка.Общий") Тогда\n'
    "        Возврат;\n"
    "    КонецЕсли;\n"
    "КонецПроцедуры\n"
)

_MANAGER_MODULE_BOTH = (
    "Процедура ДобавитьКомандыСозданияНаОсновании(КомандыСоздания) Экспорт\n"
    "    Документы.ОбщаяЦель.ДобавитьКомандуСозданияНаОсновании(КомандыСоздания);\n"
    "КонецПроцедуры\n"
)


@pytest.fixture
def cf_base_and_cfe_same_type(tmp_path):
    """base + CFE: у ОДНОГО документа по два модуля каждого прямого вида.

    Доказывает, что два статичных файла реально попали в один helper-вызов, но одна
    логическая связь возвращается ОДНОЙ строкой: `file` — provenance первого
    вхождения, а не identity отношения.
    """
    root = tmp_path / "src" / "cf"
    cfe = tmp_path / "src" / "cfe" / "ExtAddOn"
    _write(root / "Configuration.xml", _CF_DESCRIPTOR)
    _write(cfe / "Configuration.xml", _CFE_DESCRIPTOR)
    for base in (root, cfe):
        _write(base / "Documents" / "ЦелевойДок" / "Ext" / "ObjectModule.bsl", _OBJECT_MODULE_BOTH)
        _write(base / "Documents" / "ЦелевойДок" / "Ext" / "ManagerModule.bsl", _MANAGER_MODULE_BOTH)
    return types.SimpleNamespace(root=root, cfe=cfe)


_THREE_PROCS_NO_REGIONS = (
    "Процедура А() Экспорт\nКонецПроцедуры\n\n"
    "Процедура Б()\nКонецПроцедуры\n\n"
    "Функция В() Экспорт\n    Возврат 1;\nКонецФункции\n"
)

_THREE_PROCS_MIXED_REGIONS = (
    "#Область ПрограммныйИнтерфейс\n\n"
    "Процедура А() Экспорт\nКонецПроцедуры\n\n"
    "Процедура Б()\nКонецПроцедуры\n\n"
    "#КонецОбласти\n\n"
    "Функция В() Экспорт\n    Возврат 1;\nКонецФункции\n"
)


@pytest.fixture
def cf_no_regions(tmp_path, monkeypatch):
    """ManagerModule с тремя процедурами и БЕЗ единой `#Область`."""
    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
    root = tmp_path / "cf"
    _write(root / "Configuration.xml", _CF_DESCRIPTOR)
    _write(root / "Documents" / "ТестДок" / "Ext" / "ManagerModule.bsl", _THREE_PROCS_NO_REGIONS)
    reader = _build_reader(root)
    try:
        yield _ns(root, reader)
    finally:
        reader.close()


_NESTED_REGIONS = (
    "#Область Внешняя\n\n"
    "Процедура ВнешнийМетод() Экспорт\nКонецПроцедуры\n\n"
    "#Область Внутренняя\n\n"
    "Процедура ВнутреннийМетод() Экспорт\nКонецПроцедуры\n\n"
    "#КонецОбласти\n\n"
    "#КонецОбласти\n\n"
    "Процедура МетодВнеОбластей() Экспорт\nКонецПроцедуры\n"
)


@pytest.fixture
def cf_nested_regions(tmp_path, monkeypatch):
    """`#Область` ВНУТРИ `#Область`: метод вложенной лежит в `children`, не в
    `methods` корня. Плоский обход outline его теряет."""
    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
    root = tmp_path / "cf"
    _write(root / "Configuration.xml", _CF_DESCRIPTOR)
    _write(root / "Documents" / "ТестДок" / "Ext" / "ManagerModule.bsl", _NESTED_REGIONS)
    reader = _build_reader(root)
    try:
        yield _ns(root, reader)
    finally:
        reader.close()


@pytest.fixture
def cf_mixed_regions(tmp_path, monkeypatch):
    """Тот же документ: две процедуры внутри `#Область`, одна вне."""
    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
    root = tmp_path / "cf"
    _write(root / "Configuration.xml", _CF_DESCRIPTOR)
    _write(root / "Documents" / "ТестДок" / "Ext" / "ManagerModule.bsl", _THREE_PROCS_MIXED_REGIONS)
    reader = _build_reader(root)
    try:
        yield _ns(root, reader)
    finally:
        reader.close()


def _functional_option_xml(name: str, content: list[str], synonym: str = "", location: str = "") -> str:
    """CF-XML функциональной опции: состав — `<Content><xr:Object>…</xr:Object></Content>`."""
    syn = (
        f"            <Synonym><v8:item><v8:lang>ru</v8:lang><v8:content>{synonym}</v8:content></v8:item></Synonym>\n"
        if synonym
        else "            <Synonym/>\n"
    )
    loc = f"            <Location>{location}</Location>\n" if location else ""
    items = "".join(f"                <xr:Object>{c}</xr:Object>\n" for c in content)
    block = f"            <Content>\n{items}            </Content>\n" if content else "            <Content/>\n"
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"\n'
        '                xmlns:v8="http://v8.1c.ru/8.1/data/core"\n'
        '                xmlns:xr="http://v8.1c.ru/8.3/xcf/readable">\n'
        "    <FunctionalOption>\n"
        "        <Properties>\n"
        f"            <Name>{name}</Name>\n"
        f"{syn}{loc}{block}"
        "        </Properties>\n"
        "    </FunctionalOption>\n"
        "</MetaDataObject>\n"
    )


@pytest.fixture
def cf_fo(tmp_path, monkeypatch):
    """ФО `ОпцияА` c `<Content>Document.ТестДок</Content>` + code-корзина у `ТестДок`."""
    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
    root = tmp_path / "cf"
    _write(root / "Configuration.xml", _CF_DESCRIPTOR)
    _write(
        root / "FunctionalOptions" / "ОпцияА.xml",
        _functional_option_xml("ОпцияА", ["Document.ТестДок"]),
    )
    _write(
        root / "Documents" / "ТестДок" / "Ext" / "ObjectModule.bsl",
        "Процедура Проверить() Экспорт\n"
        '    Если ПолучитьФункциональнуюОпцию("ОпцияБ") Тогда\n'
        "        Возврат;\n"
        "    КонецЕсли;\n"
        "КонецПроцедуры\n",
    )
    reader = _build_reader(root)
    try:
        yield _ns(root, reader)
    finally:
        reader.close()


# cf_fo_fat подбирает ВЕС строки, а не только их число: тест требует, чтобы
# каждый рычаг ПООДИНОЧКЕ бюджета не спасал, а пара — спасала. Пусть S — вес
# slim-строки (без content), N=60 опций, limit=50:
#   60*S > 15000  (include_content=False в одиночку НЕ спасает)  => S > 250
#   50*S <= 15000 (пара спасает)                                 => S <= 300
# Отсюда длина имени/синонима/расположения; content держит вес полной строки
# заведомо выше бюджета при любом срезе. Фикстура статична, числа детерминированы.
_FO_FAT_PAD = "Раздел"


def _fo_fat_name(n: int) -> str:
    return f"ОпцияОбзорнаяНомер{n:02d}{_FO_FAT_PAD * 4}"


@pytest.fixture
def cf_fo_fat(tmp_path, monkeypatch):
    """60 ФО (строго больше `limit=50`), у каждой `<Content>` из 40 элементов.

    Размеры подобраны так, что полный ответ и каждый одиночный рычаг тяжелее
    15 000 символов, а комбинация — легче; тест это ассертит.
    """
    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
    root = tmp_path / "cf"
    _write(root / "Configuration.xml", _CF_DESCRIPTOR)
    for n in range(60):
        name = _fo_fat_name(n)
        _write(
            root / "FunctionalOptions" / f"{name}.xml",
            _functional_option_xml(
                name,
                [f"Document.ОбъектСоставаОпции{n:02d}Номер{i:02d}" for i in range(40)],
                synonym=f"Бизнес-имя опции номер {n:02d} {_FO_FAT_PAD * 3}",
                location=f"Константа.ХранилищеЗначенияОпции{n:02d}{_FO_FAT_PAD * 2}",
            ),
        )
    reader = _build_reader(root)
    try:
        yield _ns(root, reader)
    finally:
        reader.close()


def _git_init(root):
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)


requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git недоступен")


@pytest.fixture
def git_repo_small(tmp_path):
    root = tmp_path / "repo"
    _write(root / "Configuration.xml", _CF_DESCRIPTOR)
    _write(
        root / "CommonModules" / "Модуль" / "Ext" / "Module.bsl",
        "Процедура Первая() Экспорт\nКонецПроцедуры\n\nПроцедура Вторая() Экспорт\nКонецПроцедуры\n",
    )
    _git_init(root)
    return types.SimpleNamespace(root=root)


@pytest.fixture
def git_repo_many_hits(tmp_path):
    """Два модуля, в каждом РОВНО 60 строк со словом `Если`.

    60 > пофайлового потолка 50; 2x50=100 > `max_results=30`. Обычный пробел без
    `]` делает на тех же статичных файлах наблюдаемым расхождение POSIX ERE
    `[[:space:]]` и Python `re`.
    """
    root = tmp_path / "repo"
    _write(root / "Configuration.xml", _CF_DESCRIPTOR)
    for n in (1, 2):
        body = "".join(f"    Если Условие{i:02d} Тогда КонецЕсли;\n" for i in range(60))
        _write(
            root / "CommonModules" / f"Модуль{n}" / "Ext" / "Module.bsl",
            f"Процедура Тело{n}() Экспорт\n{body}КонецПроцедуры\n",
        )
    _git_init(root)
    return types.SimpleNamespace(root=root)


@pytest.fixture
def git_repo_paren_hits(tmp_path):
    """Модуль с 60 строками, содержащими ЛИТЕРАЛЬНОЕ `Если(`."""
    root = tmp_path / "repo"
    _write(root / "Configuration.xml", _CF_DESCRIPTOR)
    body = "".join(f"    // Если({i:02d});\n" for i in range(60))
    _write(
        root / "CommonModules" / "Скобки" / "Ext" / "Module.bsl",
        f"Процедура Тело() Экспорт\n{body}КонецПроцедуры\n",
    )
    _git_init(root)
    return types.SimpleNamespace(root=root)


@pytest.fixture
def git_repo_ignore_case_hits(tmp_path):
    """Один модуль с 60 строками, попеременно `Marker` / `marker` (ASCII)."""
    root = tmp_path / "repo"
    _write(root / "Configuration.xml", _CF_DESCRIPTOR)
    body = "".join(f"    // {'Marker' if i % 2 == 0 else 'marker'} {i:02d}\n" for i in range(60))
    _write(
        root / "CommonModules" / "Регистр" / "Ext" / "Module.bsl",
        f"Процедура Тело() Экспорт\n{body}КонецПроцедуры\n",
    )
    _git_init(root)
    return types.SimpleNamespace(root=root)


@pytest.fixture
def git_repo_text_blocked_extension(tmp_path):
    """Tracked UTF-8 `manual.pdf` c 60 текстовыми строками `Marker` и без NUL.

    Для Git `-I` это текст и `git_search` достигает пофайлового потолка; generic
    `grep` заранее отбрасывает `.pdf` по `_BINARY_EXTENSIONS`.
    """
    root = tmp_path / "repo"
    _write(root / "Configuration.xml", _CF_DESCRIPTOR)
    _write(root / "CommonModules" / "Пустой" / "Ext" / "Module.bsl", _FIXTURE_MODULE_BSL)
    _write(root / "manual.pdf", "".join(f"Marker line {i:02d}\n" for i in range(60)))
    _git_init(root)
    return types.SimpleNamespace(root=root)


@pytest.fixture
def git_repo_many_files(tmp_path):
    """20 модулей, в каждом РОВНО одна строка со словом `Маркер`."""
    root = tmp_path / "repo"
    _write(root / "Configuration.xml", _CF_DESCRIPTOR)
    for n in range(20):
        _write(
            root / "CommonModules" / f"Модуль{n:02d}" / "Ext" / "Module.bsl",
            f"Процедура Тело{n:02d}() Экспорт\n    // Маркер {n:02d}\nКонецПроцедуры\n",
        )
    _git_init(root)
    return types.SimpleNamespace(root=root)


@pytest.fixture
def deep_tree(tmp_path):
    """200 каталогов глубины 4, в листьях `.bsl`."""
    root = tmp_path / "deep"
    _write(root / "Configuration.xml", _CF_DESCRIPTOR)
    for n in range(200):
        leaf = root / "CommonModules" / f"Г{n % 10}" / f"У{n % 20}" / f"Модуль{n:03d}" / "Ext"
        _write(leaf / "Module.bsl", _FIXTURE_MODULE_BSL)
    return types.SimpleNamespace(root=root)


@pytest.fixture
def tree_with_skips(tmp_path):
    """Дерево, где `.bsl` лежат и в `.git/`, `node_modules/`, `.hidden/`."""
    root = tmp_path / "skips"
    _write(root / "Configuration.xml", _CF_DESCRIPTOR)
    _write(root / "CommonModules" / "Обычный" / "Ext" / "Module.bsl", _FIXTURE_MODULE_BSL)
    for skipped in (".git", "node_modules", ".hidden"):
        _write(root / skipped / "Спрятанный" / "Ext" / "Module.bsl", _FIXTURE_MODULE_BSL)
    return types.SimpleNamespace(root=root)


@pytest.fixture
def cf_many_modules(tmp_path, monkeypatch):
    """300 общих модулей — чтобы прогрев был измерим.

    Отдаёт `.root` / `.reader` / `.format_info` (R48): без ридера
    `_ensure_live_bsl_catalog` идёт glob-каноном и `scan_bsl_tree` не зовёт вовсе;
    без `format_info` `Sandbox` BSL-фабрику не вызывает.
    """
    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
    root = tmp_path / "cf"
    _write(root / "Configuration.xml", _CF_DESCRIPTOR)
    for n in range(300):
        _write(
            root / "CommonModules" / f"Модуль{n:03d}" / "Ext" / "Module.bsl",
            _FIXTURE_MODULE_BSL,
        )
    reader = _build_reader(root)
    try:
        yield _ns(root, reader, format_info=detect_format(str(root)))
    finally:
        reader.close()


@pytest.fixture
def cf_with_extensions(tmp_path, monkeypatch):
    """main + одно расширение рядом, чтобы metadata/synonym-фаза была достижима.

    Тест ОБЯЗАН передать `extension_paths=[str(.cfe)]`: `_iter_metadata_xml_files`
    зовётся ТОЛЬКО в фазе расширений, для прямой фабрики расширения не
    обнаруживаются автоматически (R49).
    """
    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
    root = tmp_path / "src" / "cf"
    cfe = tmp_path / "src" / "cfe" / "ExtAddOn"
    _write(root / "Configuration.xml", _CF_DESCRIPTOR)
    _write(root / "CommonModules" / "Основной" / "Ext" / "Module.bsl", _FIXTURE_MODULE_BSL)
    _write(cfe / "Configuration.xml", _CFE_DESCRIPTOR)
    _write(cfe / "CommonModules" / "Расширенный" / "Ext" / "Module.bsl", _FIXTURE_MODULE_BSL)
    reader = _build_reader(root)
    try:
        yield _ns(root, reader, cfe=cfe)
    finally:
        reader.close()


@pytest.fixture
def tree_with_unreadable_dir(tmp_path, monkeypatch):
    """Каталог, перечисление которого падает.

    На Windows `os.chmod` доступ не отбирает, поэтому отказ моделируется
    инъекцией `OSError` в `os.scandir` на КОНКРЕТНОМ пути. Это и есть тот самый
    класс молча выпавшего каталога, ради которого scanner вообще считает ошибки.
    """
    root = tmp_path / "unreadable"
    _write(root / "Configuration.xml", _CF_DESCRIPTOR)
    _write(root / "CommonModules" / "Видимый" / "Ext" / "Module.bsl", _FIXTURE_MODULE_BSL)
    blocked = root / "CommonModules" / "Закрытый"
    _write(blocked / "Ext" / "Module.bsl", _FIXTURE_MODULE_BSL)

    real_scandir = os.scandir
    target = os.path.normcase(os.fspath(blocked))

    def _blocked_scandir(path):
        if os.path.normcase(os.fspath(path)) == target:
            raise PermissionError(13, "Permission denied", str(path))
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", _blocked_scandir)
    return root


def _make_dir_redirect(link: "os.PathLike[str] | str", target: "os.PathLike[str] | str") -> bool:
    """Создать каталог-редирект платформенным способом. False — не удалось."""
    link, target = os.fspath(link), os.fspath(target)
    if os.name == "nt":
        proc = subprocess.run(
            ["cmd", "/c", "mklink", "/J", link, target],
            capture_output=True,
            text=True,
        )
        return proc.returncode == 0
    try:
        os.symlink(target, link, target_is_directory=True)
    except OSError:
        return False
    return True


@pytest.fixture
def tree_with_self_junction(tmp_path):
    """Каталог `AliasLoop`, перенаправленный на саму `.root` (цикл ВНУТРИ корня)."""
    root = tmp_path / "loop"
    _write(root / "Configuration.xml", _CF_DESCRIPTOR)
    _write(root / "CommonModules" / "Модуль" / "Ext" / "Module.bsl", _FIXTURE_MODULE_BSL)
    if not _make_dir_redirect(root / "AliasLoop", root):
        pytest.skip("каталог-редирект создать не удалось")
    return types.SimpleNamespace(root=root)


@pytest.fixture
def tree_with_external_redirect(tmp_path):
    """Внутри `.root` каталог `OutsideAlias` ведёт в ЗАВЕДОМО внешний каталог."""
    root = tmp_path / "inside"
    outside = tmp_path / "outside"
    _write(root / "Configuration.xml", _CF_DESCRIPTOR)
    _write(root / "CommonModules" / "Модуль" / "Ext" / "Module.bsl", _FIXTURE_MODULE_BSL)
    leak = outside / "Leak.bsl"
    _write(leak, _FIXTURE_MODULE_BSL)
    if not _make_dir_redirect(root / "OutsideAlias", outside):
        pytest.skip("каталог-редирект создать не удалось")
    return types.SimpleNamespace(root=root, outside_file=leak)


@pytest.fixture
def real_sandbox():
    """Фабрика настоящего ``Sandbox`` с ``format_info``.

    Без ``format_info`` конструктор BSL-фабрику НЕ вызывает вовсе, и тест
    проверял бы generic-песочницу без единого BSL-хелпера. Защитный finalizer
    делает ``join(timeout=60)`` потоку прогрева, если тест передал Sandbox
    lifecycle-владельцу либо явно позвал приватный start-hook: поток ходит по
    tmp_path, а pytest начинает удалять дерево сразу после теста.
    """
    from rlm_tools_bsl.sandbox import Sandbox

    made: list = []

    def _factory(root, **kw):
        kw.setdefault("format_info", detect_format(str(root)))
        sb = Sandbox(base_path=str(root), **kw)
        made.append(sb)
        return sb

    yield _factory
    for sb in made:
        thread = getattr(sb, "_prewarm_thread", None)
        if thread is not None:
            thread.join(timeout=60)


@pytest.fixture(scope="module")
def prewarm_env_seen_at_module_setup():
    return os.environ.get("RLM_PREWARM_LIVE_CATALOG")


def test_suite_prewarm_pin_precedes_module_fixtures(prewarm_env_seen_at_module_setup):
    """Session-pin обязан встать ДО любых module-scoped backend-фикстур.

    Иначе защита process-сюиты (где `backend` имеет module scope) недостижима, и
    фоновый обход tmp-дерева ломает уборку на Windows.
    """
    assert prewarm_env_seen_at_module_setup == "0"


# ---------------------------------------------------------------------------
# Smoke: каждая фикстура, обещающая `.reader`, реально строит индекс
# ---------------------------------------------------------------------------
_READER_FIXTURES = [
    "cf_indexed",
    "built_index_env",
    "cf_big_subsystem",
    "cf_many_subsystem_matches",
    "cf_subsystem_output_budget",
    "cf_case_variants",
    "cf_name_fragment",
    "cf_synonym",
    "cf_empty_subsystem",
    "cf_edt_empty_subsystem",
    "cf_edt_and_cf",
    "cf_foreign_subsystem_index",
    "cf_indexed_basedon",
    "cf_no_regions",
    "cf_mixed_regions",
    "cf_fo",
    "cf_fo_fat",
    "cf_many_modules",
    "cf_with_extensions",
]


@pytest.mark.parametrize("fixture_name", _READER_FIXTURES)
def test_reader_fixtures_smoke(request, fixture_name):
    """Фикстура не считается выполненной, если имя лишь перечислено в плане.

    Smoke одновременно импортирует файл, резолвит фикстуру, реально строит индекс
    и выполняет generator-teardown (закрытие ридера).
    """
    ns = request.getfixturevalue(fixture_name)
    assert ns.root.exists(), f"{fixture_name}: дерево не создано"
    assert ns.reader is not None, f"{fixture_name}: ридер не построен"
    caps = ns.reader.get_build_capabilities()
    assert caps is not None and caps["has_metadata"] is True


# ---------------------------------------------------------------------------
# Задача 0. Освобождённый slim-бюджет подписей
# ---------------------------------------------------------------------------
# Порог двигается ТОЛЬКО после exact-теста соответствующей подписи и ровно на
# заранее известную дельту:
#   Task 0: 11928   Task 1: 12289 (+361)   Task 3: 12265 (-24)   Task 4: 12306 (+41)
_SIG_SUM_BUDGET_WITHOUT_GIT = 12306


def test_sig_budget_headroom_freed_for_release():
    """Задача 0: до объявления новых ключей в бюджете обязан появиться запас.

    Считается сумма подписей БЕЗ `git_search` — то есть ровно то множество,
    которое попадает в бьющую ячейку (slim / query='' / non-git whole payload).
    `build_helper_metadata_snapshot()` регистрирует `git_search` ПРИНУДИТЕЛЬНО
    (`register_git_search="force"`), а в non-git-сессии его в
    `available_functions` нет вовсе — поэтому включать его в эту сумму значит
    считать не ту величину и получить ложный отказ на Задаче 5, чья подпись
    `git_search` живёт в ДРУГОМ бюджете.
    """
    from rlm_tools_bsl.bsl_helpers import build_helper_metadata_snapshot

    snap = build_helper_metadata_snapshot()
    total = sum(len(v["sig"]) for k, v in snap.items() if k != "git_search")
    # 12737 - 409 = 12328 — измеренный факт v1.35.2 без git_search.
    # Задача 0 обязана снять минимум 400.
    assert total <= _SIG_SUM_BUDGET_WITHOUT_GIT, (
        f"сумма sig без git_search = {total}; порог {_SIG_SUM_BUDGET_WITHOUT_GIT} "
        f"(Задача 0 освободила >= 400 символов до объявления новых ключей)"
    )


def test_trimmed_sigs_did_not_lose_a_single_key_name():
    """Задача 0 режет ПРОЗУ вокруг ключей, а не сами ключи.

    Любой ключ/параметр, исчезнувший из `sig`, — ошибка, а не экономия: агент
    выбирает вызов именно по именам.
    """
    from rlm_tools_bsl.bsl_helpers import build_helper_metadata_snapshot

    snap = build_helper_metadata_snapshot()
    required = {
        "find_functional_options": [
            "xml_options",
            "code_options",
            "xml_total",
            "code_total",
            "returned",
            "has_more",
            "limit",
            "include_code",
        ],
        "find_by_type": ["meta_type", "count_only", "unique_objects", "extensions_included"],
        "parse_form": ["handlers", "commands", "attributes", "element", "types", "main_table"],
        "find_callers_context": [
            "callers",
            "total_callers",
            "exact_available",
            "target_exact",
            "exact_rows",
            "fallback_rows",
        ],
    }
    for helper, keys in required.items():
        sig = snap[helper]["sig"]
        for key in keys:
            assert key in sig, f"{helper}: ключ {key!r} пропал из sig"


def test_moved_prose_landed_in_the_unbudgeted_recipe():
    """Перенос — это ПЕРЕНОС: пояснение обязано появиться в `recipe`.

    Иначе «экономия» бюджета оказалась бы простым удалением знания, которое
    агенту всё ещё нужно (`recipe` отдаёт `rlm_help(helpers=[...])` по запросу и
    в стартовый payload он не входит).
    """
    from rlm_tools_bsl.bsl_helpers import build_helper_metadata_snapshot

    snap = build_helper_metadata_snapshot()
    assert "20 BSL-модул" in snap["find_functional_options"]["recipe"]
    assert "partial=True" in snap["find_functional_options"]["recipe"]
    assert "InformationRegisters" in snap["find_by_type"]["recipe"]
    assert "element_name" in snap["parse_form"]["recipe"]
    assert "attr_type" in snap["parse_form"]["recipe"]
    assert "exact_rows" in snap["find_callers_context"]["recipe"]
    assert "fallback_rows" in snap["find_callers_context"]["recipe"]


def test_analyze_subsystem_registered_sig_exact():
    from rlm_tools_bsl.bsl_helpers import build_helper_metadata_snapshot

    sig = build_helper_metadata_snapshot()["analyze_subsystem"]["sig"]
    assert len(sig) == 427
    for marker in (
        "subsystems_found",
        "content_truncated",
        "reverse_lookup_supported",
        "extensions_included",
        "direct=row-full",
        "total_objects=row-total",
        "found>len(subsystems)=>14K.",
    ):
        assert marker in sig


# ---------------------------------------------------------------------------
# Задача 1. analyze_subsystem — одна форма ответа и честный состав
# ---------------------------------------------------------------------------
def test_lookup_direct_returns_entire_stored_row_composition(built_index_env):
    """Прямой вопрос возвращает весь сохранённый состав найденной строки."""
    reader = built_index_env.reader
    found = reader.get_subsystem_lookup("ПодсистемаА")
    assert found is not None, "таблица subsystem_content должна быть в индексе"
    assert len(found["direct"]) == 1 and found["containing"] == []
    row = found["direct"][0]
    assert row["name"] == "ПодсистемаА" and row["matched_by"] == "name"
    # Состав ЭТОЙ СТРОКИ полный: и объект, чьё имя совпадает с именем подсистемы, и тот,
    # чьё имя с ним ничего общего не имеет — вторые и терялись до релиза.
    # Это не утверждение, что collector разобрал каждый subsystem XML корня.
    assert set(row["content"]) == {"CommonModule.ПодсистемаАСервер", "Document.ЧужоеИмя"}
    # Регистронезависимость — str.lower(), как у py_lower.
    assert reader.get_subsystem_lookup("подсистемаа")["direct"][0]["name"] == "ПодсистемаА"
    # Несуществующее имя — пустые группы И кандидаты при включённом synonym-каталоге, а не
    # None: None означает «нужных таблиц нет / транзиентный сбой».
    missing = reader.get_subsystem_lookup("НетТакой")
    assert missing == {
        "direct": [],
        "containing": [],
        "direct_candidates": [],
        "synonym_candidates_supported": True,
    }


def test_lookup_refuses_to_publish_rows_of_an_unfinished_inplace_rebuild(built_index_env):
    """`has_metadata` переживает in-place пересборку, а таблицы — нет.

    `_begin_inplace_rebuild` СОХРАНЯЕТ build-опции, ставит `build_in_progress=1` и
    опустошает схему; окно видно открытому RO-ридеру (ради этого пересборка и
    делается in-place). Без маркерного гейта частично заполненная таблица уехала
    бы наружу как полный состав — `_transient_safe` тут не помогает, SQL успешен.
    """
    import sqlite3

    reader = built_index_env.reader
    full = reader.get_subsystem_lookup("ПодсистемаА")["direct"][0]["content"]
    assert len(full) == 2, "предусловие: до пересборки состав полный"

    writer = sqlite3.connect(str(reader._db_path))
    try:
        writer.execute("INSERT OR REPLACE INTO index_meta (key, value) VALUES ('build_in_progress','1')")
        writer.execute("DELETE FROM subsystem_content WHERE object_ref LIKE 'Document.%'")
        writer.commit()
    finally:
        writer.close()

    assert reader.get_subsystem_lookup("ПодсистемаА") is None, "частичный состав выдан за полный"


def test_analyze_subsystem_goes_live_instead_of_publishing_a_rebuild_window(built_index_env):
    """Отказ ридера обязан приводить к ЖИВОМУ ответу, а не к «не найдено».

    Проверяется не только `source`: живая ветка читает реальный XML, поэтому
    состав возвращается ПОЛНЫЙ — то есть маркерный гейт не теряет ответ, он
    меняет маршрут.
    """
    import sqlite3

    reader = built_index_env.reader
    writer = sqlite3.connect(str(reader._db_path))
    try:
        writer.execute("INSERT OR REPLACE INTO index_meta (key, value) VALUES ('build_in_progress','1')")
        writer.execute("DELETE FROM subsystem_content WHERE object_ref LIKE 'Document.%'")
        writer.commit()
    finally:
        writer.close()

    bsl = _make_bsl(built_index_env.root, idx_reader=reader)
    res = bsl["analyze_subsystem"]("ПодсистемаА")
    assert res["_meta"]["source"] == "live"
    assert res["_meta"]["reverse_lookup_supported"] is False
    (row,) = res["subsystems"]
    assert row["total_objects"] == 2
    assert set(row["raw_content"]) == {"CommonModule.ПодсистемаАСервер", "Document.ЧужоеИмя"}


def test_lookup_containing_is_the_reverse_question_with_full_content(built_index_env):
    """Обратный вопрос ТЕМ ЖЕ проходом — и у его строк состав тоже ПОЛНЫЙ."""
    found = built_index_env.reader.get_subsystem_lookup("ЧужоеИмя")
    assert found["direct"] == []
    (row,) = found["containing"]
    assert row["matched_refs"] == ["Document.ЧужоеИмя"]
    assert len(row["content"]) == 2, "состав containing-строки обязан быть полным, не matched_refs"


def test_lookup_matches_synonym_as_direct(cf_synonym):
    """R52/R93: сохранить прежний basename-достижимый synonym-match.

    `<Name>ктнСпецодежда</Name>`, `<Synonym>Спецодежда</Synonym>` находится по
    `Спецодежда`, потому что query уже входит в `ктнСпецодежда.xml`.
    """
    found = cf_synonym.reader.get_subsystem_lookup("спецодежда")
    assert [r["matched_by"] for r in found["direct"]] == ["synonym"]
    assert found["direct"][0]["name"] == "ктнСпецодежда"


def test_lookup_empty_subsystem_is_a_candidate_not_a_fake_absence(cf_empty_subsystem):
    """Пустой Content не создаёт строк subsystem_content, но XML подсистемы есть."""
    reader = cf_empty_subsystem.reader
    with reader._lock:
        n = reader._conn.execute(
            "SELECT COUNT(*) FROM subsystem_content WHERE subsystem_name=?", ("Пустая",)
        ).fetchone()[0]
    assert n == 0, "предусловие: дефект представимости не воспроизведён"
    found = reader.get_subsystem_lookup("Пустая")
    assert found["direct"] == []
    assert found["direct_candidates"] == ["Subsystems/Пустая.xml"]
    assert found["synonym_candidates_supported"] is True
    # Это синоним, которого нет в basename файла. Старый query-bearing glob его
    # не находил; read-time bugfix не должен превращаться в новый глобальный поиск.
    by_new_global_synonym = reader.get_subsystem_lookup("Пустая подсистема")
    assert by_new_global_synonym["direct"] == []
    assert by_new_global_synonym["direct_candidates"] == []


def test_lookup_edt_empty_without_synonym_comes_from_file_paths(cf_edt_empty_subsystem):
    """V2/R76: EDT `.mdo` без Content и Synonym имеет ровно один источник-кандидат.

    Smoke build этой фикстуры недостаточен: он проходит и при сломанном
    структурном предикате. Здесь проверяется сам reader -> helper стык.
    """
    reader = cf_edt_empty_subsystem.reader
    with reader._lock:
        n_content = reader._conn.execute(
            "SELECT COUNT(*) FROM subsystem_content WHERE subsystem_name=?",
            ("ЕДТПустая",),
        ).fetchone()[0]
        n_synonyms = reader._conn.execute(
            "SELECT COUNT(*) FROM object_synonyms WHERE category='Subsystems' AND object_name=?",
            ("ЕДТПустая",),
        ).fetchone()[0]
    assert (n_content, n_synonyms) == (0, 0), "фикстура обязана оставить только file_paths"
    found = reader.get_subsystem_lookup("ЕДТПустая")
    assert found["direct"] == []
    assert found["direct_candidates"] == ["Subsystems/ЕДТПустая/ЕДТПустая.mdo"]

    bsl = _make_bsl(cf_edt_empty_subsystem.root, idx_reader=reader)
    res = bsl["analyze_subsystem"]("ЕДТПустая")
    (row,) = res["subsystems"]
    assert res["_meta"]["source"] == "index"
    assert row["name"] == "ЕДТПустая" and row["match"] == "name"
    assert row["total_objects"] == row["objects_returned"] == 0
    assert row["raw_content"] == [] and row["content_truncated"] is False


def test_case_variants_of_one_name_in_two_files_are_both_returned(cf_case_variants):
    """`Почта` (файл A) и `почта` (файл B) — ОБЕ строки, при любом регистре запроса.

    Именно здесь ломался двухступенчатый предикат: `COLLATE NOCASE` на кириллице
    возвращал ОДНУ строку, имя засчитывалось найденным, добор не выполнялся —
    половина состава исчезала молча.
    """
    reader = cf_case_variants.reader
    # ПРЕДУСЛОВИЕ фикстуры: два РАЗНЫХ файла реально попали в индекс (в одном
    # каталоге на Windows они схлопнулись бы в один путь — R47).
    with reader._lock:
        n_files = reader._conn.execute("SELECT COUNT(DISTINCT file) FROM subsystem_content").fetchone()[0]
    assert n_files == 2, "фикстура вырождена: в индексе не два файла"
    for q in ("Почта", "почта", "ПОЧТА"):
        files = sorted(r["file"] for r in reader.get_subsystem_lookup(q)["direct"])
        assert len(files) == 2, f"запрос {q!r} вернул {files}, а файлов два"


class TestAnalyzeSubsystemSingleShape:
    def test_index_branch_returns_full_composition_not_matched_refs(self, cf_indexed):
        bsl = _make_bsl(cf_indexed.root, idx_reader=cf_indexed.reader)
        res = bsl["analyze_subsystem"]("ПодсистемаА")
        rows = [s for s in res["subsystems"] if s["match"] == "name"]
        assert len(rows) == 1
        row = rows[0]
        # Главное утверждение релиза: total_objects — ПОЛНЫЙ состав.
        assert row["total_objects"] == 2
        assert row["raw_content"] == ["CommonModule.ПодсистемаАСервер", "Document.ЧужоеИмя"]
        # И объект, чьё имя не содержит имени подсистемы, В СОСТАВЕ.
        assert "Document.ЧужоеИмя" in row["raw_content"]

    def test_both_branches_expose_identical_key_set(self, cf_indexed):
        """Форма СВЕДЕНА: индексная и живая ветки дают один набор ключей —
        на ПРЯМОМ вопросе, который умеют обе."""
        indexed = _make_bsl(cf_indexed.root, idx_reader=cf_indexed.reader)
        live = _make_bsl(cf_indexed.root)  # без ридера — живая ветка
        a = indexed["analyze_subsystem"]("ПодсистемаА")["subsystems"][0]
        b = live["analyze_subsystem"]("ПодсистемаА")["subsystems"][0]
        assert set(a) == set(b)
        assert {
            "match",
            "total_objects",
            "custom_objects",
            "standard_objects",
            "raw_content",
            "objects_returned",
            "content_truncated",
            "matched_refs",
        } <= set(a)

    def test_live_match_name_is_exact_not_substring(self, cf_name_fragment):
        """Фикстура: подсистемы `Почта` и `ВстроеннаяПочта`, причём `Почта` НЕ входит
        в состав `ВстроеннаяПочта`. Запрос-фрагмент `Почта` обязан дать на live
        ровно ОДНУ строку match='name' — иначе семантика match разъезжается с
        индексной веткой (проверено на боевом ДО3: там таких кандидатов 4)."""
        live = _make_bsl(cf_name_fragment.root)
        names = [s["name"] for s in live["analyze_subsystem"]("Почта")["subsystems"]]
        assert names == ["Почта"], f"подстрочные кандидаты просочились как match='name': {names}"
        # А по ТОЧНОМУ имени длинная подсистема по-прежнему находится.
        assert live["analyze_subsystem"]("ВстроеннаяПочта")["subsystems"][0]["name"] == "ВстроеннаяПочта"

    def test_live_name_match_is_case_insensitive_on_any_fs(self, cf_name_fragment):
        """Запрос в НИЖНЕМ регистре обязан найти подсистему `Почта`.

        На case-sensitive ФС (Linux в CI) паттерн `*почта*` не совпал бы с файлом
        `Почта.xml`, и post-filter по meta['name'] исполнять было бы не над чем.
        Тест значим именно на Linux; на Windows он проходит и ДО фикса — поэтому
        локальный зелёный прогон здесь ничего не доказывает, нужен CI.
        """
        live = _make_bsl(cf_name_fragment.root)
        res = live["analyze_subsystem"]("почта")
        assert [s["name"] for s in res["subsystems"]] == ["Почта"]

    def test_live_case_fix_does_not_parse_every_subsystem_xml(self, cf_subsystem_output_budget, monkeypatch):
        """R93: case-insensitive candidates are prefiltered before XML parsing.

        Статичная фикстура содержит точную `Широкая` и 100 нерелевантных
        `ОбратнаяNNN`. Перечислить структурные пути допустимо; разобрать 101 XML
        на один точный запрос — уже новая цена и скрытое расширение synonym-search.
        """
        import rlm_tools_bsl.bsl_helpers as BH

        live = _make_bsl(cf_subsystem_output_budget.root)
        parsed: list[str] = []
        real = BH.parse_metadata_xml

        def _record(text):
            parsed.append(text)
            return real(text)

        monkeypatch.setattr(BH, "parse_metadata_xml", _record)
        res = live["analyze_subsystem"]("широкая")
        assert [s["name"] for s in res["subsystems"]] == ["Широкая"]
        assert len(parsed) == 1, f"разобрано {len(parsed)} XML вместо одного кандидата"

    def test_live_and_index_agree_on_match_name_for_a_fragment(self, cf_name_fragment):
        """Тот же запрос-фрагмент на ОБЕИХ ветках даёт один набор match='name'."""
        live = _make_bsl(cf_name_fragment.root)
        idx = _make_bsl(cf_name_fragment.root, idx_reader=cf_name_fragment.reader)
        a = sorted(s["name"] for s in live["analyze_subsystem"]("Почта")["subsystems"] if s["match"] == "name")
        b = sorted(s["name"] for s in idx["analyze_subsystem"]("Почта")["subsystems"] if s["match"] == "name")
        assert a == b == ["Почта"]

    def test_live_branch_declares_that_it_cannot_do_reverse_lookup(self, cf_indexed):
        """Живая ветка ищет XML подсистемы ПО ИМЕНИ ФАЙЛА, поэтому запрос по имени
        входящего объекта не найдёт ничего НИКОГДА. Это обязано быть ОБЪЯВЛЕНО,
        а не выглядеть как «объект ни в одну подсистему не входит»."""
        live = _make_bsl(cf_indexed.root)  # без ридера
        direct = live["analyze_subsystem"]("ПодсистемаА")
        assert direct["_meta"]["reverse_lookup_supported"] is False
        assert all(s["match"] == "name" for s in direct["subsystems"])
        reverse = live["analyze_subsystem"]("ЧужоеИмя")
        # Пустой ответ на обратный вопрос СОПРОВОЖДЁН объяснением, а не молчит,
        # и _meta есть даже на нём (иначе агент не отличит «нет» от «не искали»).
        assert "error" in reverse
        assert reverse["_meta"]["reverse_lookup_supported"] is False
        # Текст проверяется ПО СМЫСЛУ, а не по факту наличия: прежний hint обещал
        # обратный поиск и на живой ветке, то есть подтверждал ложный вывод.
        assert "не поддержан" in reverse["hint"]
        assert "match='content'" not in reverse["hint"], "hint живой ветки не имеет права обещать то, чего она не умеет"
        assert "rlm_index build" in reverse["hint"], "нужен исполнимый выход"

    def test_index_empty_hint_does_not_turn_has_metadata_into_coverage(self, cf_indexed):
        """Пустая таблица — ноль среди разобранных строк, не сертификат всех XML."""
        bsl = _make_bsl(cf_indexed.root, idx_reader=cf_indexed.reader)
        res = bsl["analyze_subsystem"]("НетТакойПодсистемыИлиОбъекта")
        assert "error" in res and res["_meta"]["source"] == "index"
        assert "успешно разобран" in res["hint"]
        assert "has_metadata" in res["hint"] and "сертификат" in res["hint"]

    def test_index_answer_makes_exactly_one_reader_call(self, cf_indexed, monkeypatch):
        """R51: один вызов ридера на весь ответ — не три прохода по таблице.

        R132: утверждение именно про reader API. Точечное чтение XML для
        `direct_candidates` этим тестом не запрещено и запрещаться не должно —
        пустой `<Content>` иначе недостижим; на ЭТОЙ фикстуре кандидатов нет,
        поэтому XML не читается вовсе, что и проверяет предпоследний ассерт.
        """
        reader = cf_indexed.reader
        calls: list[str] = []
        real = reader.get_subsystem_lookup
        monkeypatch.setattr(reader, "get_subsystem_lookup", lambda q: (calls.append(q), real(q))[1])
        monkeypatch.setattr(
            reader,
            "get_subsystems_for_object",
            lambda *a, **k: pytest.fail("analyze_subsystem не должен звать старый метод"),
        )
        import rlm_tools_bsl.bsl_helpers as BH

        parsed: list[int] = []
        real_parse = BH.parse_metadata_xml
        monkeypatch.setattr(BH, "parse_metadata_xml", lambda text: (parsed.append(1), real_parse(text))[1])
        bsl = _make_bsl(cf_indexed.root, idx_reader=reader)
        res = bsl["analyze_subsystem"]("ЧужоеИмя")
        assert calls == ["ЧужоеИмя"]
        assert parsed == [], "кандидатов нет — XML читать не за чем"
        assert res["_meta"]["source"] == "index" and "partial" not in res

    def test_reader_unavailable_falls_back_to_live(self, cf_indexed, monkeypatch):
        """None от ридера (нет таблицы / транзиентный сбой) -> live, объявленно."""
        reader = cf_indexed.reader
        monkeypatch.setattr(reader, "get_subsystem_lookup", lambda q: None)
        bsl = _make_bsl(cf_indexed.root, idx_reader=reader)
        res = bsl["analyze_subsystem"]("ПодсистемаА")
        assert res["_meta"]["source"] == "live"
        assert res["_meta"]["reverse_lookup_supported"] is False

    def test_synonym_match_on_both_branches(self, cf_synonym):
        """R52: `Спецодежда` при имени `ктнСпецодежда` и синониме `Спецодежда` —
        найдено как match='synonym' и на индексе, и на live; существующий
        tests/test_bsl_helpers.py::test_analyze_subsystem_found остаётся зелёным."""
        for bsl in (
            _make_bsl(cf_synonym.root, idx_reader=cf_synonym.reader),
            _make_bsl(cf_synonym.root),
        ):
            res = bsl["analyze_subsystem"]("Спецодежда")
            (row,) = res["subsystems"]
            assert row["match"] == "synonym" and row["name"] == "ктнСпецодежда"
            assert row["total_objects"] > 0

    def test_empty_subsystem_survives_index_shape(self, cf_empty_subsystem):
        """Статичный XML есть, но subsystem_content не имеет строки для пустого состава."""
        bsl = _make_bsl(cf_empty_subsystem.root, idx_reader=cf_empty_subsystem.reader)
        res = bsl["analyze_subsystem"]("Пустая")
        (row,) = res["subsystems"]
        assert row["name"] == "Пустая" and row["match"] == "name"
        assert row["total_objects"] == row["objects_returned"] == 0
        assert row["raw_content"] == row["custom_objects"] == row["standard_objects"] == []
        assert row["content_truncated"] is False
        assert res["_meta"] == {
            "source": "index",
            "limit": 200,
            "reverse_lookup_supported": True,
            "extensions_included": False,
        }

    def test_arbitrary_synonym_absent_from_filename_is_not_new_search_surface(self, cf_empty_subsystem):
        """R93: обе ветки сохраняют прежнюю candidate-path границу синонима."""
        for bsl in (
            _make_bsl(cf_empty_subsystem.root, idx_reader=cf_empty_subsystem.reader),
            _make_bsl(cf_empty_subsystem.root),
        ):
            res = bsl["analyze_subsystem"]("Пустая подсистема")
            assert "error" in res
            assert res["_meta"]["extensions_included"] is False

    def test_recipe_pattern_no_longer_reads_empty_as_absent(self, cf_indexed):
        """Код ПО РЕЦЕПТУ на проиндексированной конфигурации получает состав."""
        bsl = _make_bsl(cf_indexed.root, idx_reader=cf_indexed.reader)
        res = bsl["analyze_subsystem"]("ПодсистемаА")
        for sub in res["subsystems"]:
            got = len(sub.get("custom_objects", [])) + len(sub.get("standard_objects", []))
            assert got == sub["objects_returned"] > 0

    def test_reverse_question_preserved_and_labelled(self, cf_indexed):
        """Обратный вопрос НЕ потерян — он назван match='content'. ТОЛЬКО индексная
        ветка (на живой он не поддержан, см. отдельный тест выше)."""
        bsl = _make_bsl(cf_indexed.root, idx_reader=cf_indexed.reader)
        res = bsl["analyze_subsystem"]("ЧужоеИмя")
        content_rows = [s for s in res["subsystems"] if s["match"] == "content"]
        assert content_rows, "подсистема, СОДЕРЖАЩАЯ объект, обязана найтись"
        assert content_rows[0]["matched_refs"] == ["Document.ЧужоеИмя"]
        # но total_objects у неё — полный состав, а не длина matched_refs
        assert content_rows[0]["total_objects"] == 2
        assert content_rows[0]["raw_content"] == ["Document.ЧужоеИмя"]
        assert content_rows[0]["objects_returned"] == 1
        assert content_rows[0]["content_truncated"] is True
        assert res["_meta"]["reverse_lookup_supported"] is True

    def test_name_matches_come_first(self, cf_indexed):
        bsl = _make_bsl(cf_indexed.root, idx_reader=cf_indexed.reader)
        res = bsl["analyze_subsystem"]("ПодсистемаА")
        modes = [s["match"] for s in res["subsystems"]]
        assert modes == sorted(modes, key=lambda m: 0 if m == "name" else 1)

    def test_limit_caps_object_lists_and_flags_truncation(self, cf_big_subsystem):
        bsl = _make_bsl(cf_big_subsystem.root, idx_reader=cf_big_subsystem.reader)
        res = bsl["analyze_subsystem"]("Большая", limit=5)
        row = res["subsystems"][0]
        assert row["objects_returned"] == 5
        assert row["content_truncated"] is True
        assert row["total_objects"] > 5, "total_objects НЕ режется limit-ом"
        assert len(row["raw_content"]) == 5

    def test_limit_zero_is_a_valid_shell_page(self, cf_indexed, cf_empty_subsystem):
        """R128: `limit=0` достижим через общий guard и обязан иметь ОДНУ форму.

        Не ошибка и не пустой `subsystems`: строки найдены и перечислены, а
        состав не разворачивается. Вторая фикстура закрывает границу
        `content_truncated` у ДЕЙСТВИТЕЛЬНО пустой подсистемы — иначе реализация,
        ставящая `True` всем подряд, прошла бы тест.
        """
        bsl = _make_bsl(cf_indexed.root, idx_reader=cf_indexed.reader)
        res = bsl["analyze_subsystem"]("ПодсистемаА", limit=0)
        assert res["_meta"]["limit"] == 0
        assert res["subsystems"], "строки обязаны найтись: режется состав, а не выдача"
        (row,) = [s for s in res["subsystems"] if s["match"] == "name"]
        assert row["objects_returned"] == 0
        assert row["raw_content"] == []
        assert row["custom_objects"] == [] and row["standard_objects"] == []
        assert row["matched_refs"] == []
        assert row["total_objects"] == 2, "total_objects limit-ом НЕ режется"
        assert row["content_truncated"] is True

        empty = _make_bsl(cf_empty_subsystem.root, idx_reader=cf_empty_subsystem.reader)
        (empty_row,) = empty["analyze_subsystem"]("Пустая", limit=0)["subsystems"]
        assert empty_row["total_objects"] == empty_row["objects_returned"] == 0
        assert empty_row["content_truncated"] is False

    def test_limit_is_answer_wide_and_reverse_rows_are_compact(self, cf_many_subsystem_matches):
        """R66: direct имеет 4 объекта, 12 обратных составов — по 30.

        Per-row limit сделал бы все content_truncated=False и раздул ответ.
        Один общий бюджет отдаёт direct первым и оставляет обратным строкам
        суммарно один объект, сохраняя все 13 найденных подсистем.
        """
        bsl = _make_bsl(
            cf_many_subsystem_matches.root,
            idx_reader=cf_many_subsystem_matches.reader,
        )
        res = bsl["analyze_subsystem"]("Маркер", limit=5)
        assert res["subsystems_found"] == 13
        direct = res["subsystems"][0]
        reverse = [r for r in res["subsystems"] if r["match"] == "content"]
        assert direct["match"] == "name"
        assert direct["objects_returned"] == 4
        assert direct["content_truncated"] is False
        assert len(reverse) == 12
        assert sum(r["objects_returned"] for r in res["subsystems"]) == 5
        assert sum(r["objects_returned"] for r in reverse) == 1
        assert all(r["raw_content"] == r["matched_refs"] for r in reverse)
        assert all(r["total_objects"] == 30 for r in reverse)
        assert all(r["content_truncated"] is True for r in reverse)

    def test_one_direct_row_is_bounded_by_serialized_output(self, cf_subsystem_output_budget):
        """R73: object limit=200 сам по себе не удерживает дублированные проекции в 15K."""
        bsl = _make_bsl(
            cf_subsystem_output_budget.root,
            idx_reader=cf_subsystem_output_budget.reader,
        )
        res = bsl["analyze_subsystem"]("Широкая")
        (row,) = res["subsystems"]
        assert row["total_objects"] == 200
        assert row["objects_returned"] < 200
        assert row["content_truncated"] is True
        assert "14" in res["hint"] and "размер" in res["hint"].lower()
        assert len(json.dumps(res, ensure_ascii=False)) <= 14_000

    def test_many_reverse_row_shells_are_bounded_too(self, cf_subsystem_output_budget):
        """R73: после исчерпания limit оболочки строк не образуют второй безлимитный канал."""
        bsl = _make_bsl(
            cf_subsystem_output_budget.root,
            idx_reader=cf_subsystem_output_budget.reader,
            register_git_search="never",
        )
        assert "git_search" not in bsl, "предусловие: hint проверяется без git-helper"
        res = bsl["analyze_subsystem"]("Маркер", limit=1)
        assert res["subsystems_found"] == 100
        assert 0 < len(res["subsystems"]) < res["subsystems_found"]
        assert sum(r["objects_returned"] for r in res["subsystems"]) <= 1
        assert "subsystems_found" in res["hint"]
        assert "git_search" not in res["hint"], "нельзя советовать незарегистрированный helper"
        # Маршрут обязан быть ИСПОЛНИМ, а не просто упомянут: `Subsystems/**`
        # до Python 3.13 матчит ТОЛЬКО каталоги, и FS-ветка `glob_files`
        # отдала бы диагностическую строку вместо списка файлов.
        assert "glob_files('Subsystems/**/*.xml')" in res["hint"]
        listed = bsl["glob_files"]("Subsystems/**/*.xml")
        assert listed and all(f.lower().endswith(".xml") for f in listed), listed
        assert len(json.dumps(res, ensure_ascii=False)) <= 14_000

    @requires_git
    def test_output_hint_uses_git_search_only_when_it_is_registered(self, cf_subsystem_output_budget):
        """R92: registered route и текст fallback не расходятся."""
        subprocess.run(
            ["git", "init", "-q"],
            cwd=cf_subsystem_output_budget.root,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "add", "."],
            cwd=cf_subsystem_output_budget.root,
            check=True,
            capture_output=True,
        )

        bsl = _make_bsl(
            cf_subsystem_output_budget.root,
            idx_reader=cf_subsystem_output_budget.reader,
        )
        assert "git_search" in bsl, "предусловие: helper реально зарегистрирован"
        res = bsl["analyze_subsystem"]("Маркер", limit=1)
        assert "git_search('Маркер',path='Subsystems',file_types='xml,mdo',mode='files')" in res["hint"]
        assert "glob_files('Subsystems/**/*.xml')" in res["hint"]
        assert bsl["glob_files"]("Subsystems/**/*.xml"), "альтернативный маршрут обязан исполниться"
        routed = bsl["git_search"]("Маркер", path="Subsystems", file_types="xml,mdo", mode="files")
        assert routed["results"], "готовый маршрут из hint обязан исполниться"
        assert len(json.dumps(res, ensure_ascii=False)) <= 14_000

    @pytest.mark.parametrize("query", ["", "   "])
    def test_empty_query_fails_before_lookup_or_xml(self, cf_indexed, monkeypatch, query):
        """R91: пустой q не может совпасть с достижимым `<Synonym/>`."""
        import rlm_tools_bsl.bsl_helpers as BH

        indexed = _make_bsl(cf_indexed.root, idx_reader=cf_indexed.reader)
        live = _make_bsl(cf_indexed.root)
        monkeypatch.setattr(
            cf_indexed.reader,
            "get_subsystem_lookup",
            lambda *_: pytest.fail("validation обязана быть раньше subsystem lookup"),
        )
        monkeypatch.setattr(
            BH,
            "parse_metadata_xml",
            lambda *_: pytest.fail("validation обязана быть раньше XML"),
        )
        for bsl, source, reverse in ((indexed, "index", True), (live, "live", False)):
            res = bsl["analyze_subsystem"](query)
            assert "error" in res and "непуст" in res["hint"].lower()
            assert res["_meta"]["source"] == source
            assert res["_meta"]["reverse_lookup_supported"] is reverse
            assert len(json.dumps(res, ensure_ascii=False)) <= 14_000

    def test_overlong_query_fails_bounded_without_echo(self, cf_indexed):
        """Сам query не может сделать private output-cap недостижимым и уронить assert."""
        bsl = _make_bsl(cf_indexed.root, idx_reader=cf_indexed.reader)
        huge = "X" * 20_000
        res = bsl["analyze_subsystem"](huge)
        assert "error" in res and huge not in res["error"] and huge not in res["hint"]
        assert res["_meta"]["source"] == "index"
        assert res["_meta"]["reverse_lookup_supported"] is True
        assert len(json.dumps(res, ensure_ascii=False)) <= 14_000

    def test_overlong_query_with_no_metadata_does_not_claim_reverse(self, cf_indexed, tmp_path, monkeypatch):
        """R87: наличие ридера не равно доступности subsystem metadata."""
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx-no-metadata"))
        db = IndexBuilder().build(
            str(cf_indexed.root),
            build_calls=False,
            build_metadata=False,
            build_fts=False,
        )
        reader = IndexReader(db)
        try:
            caps = reader.get_build_capabilities()
            assert caps is not None and caps["has_metadata"] is False
            bsl = _make_bsl(cf_indexed.root, idx_reader=reader)
            res = bsl["analyze_subsystem"]("X" * 20_000)
            assert res["_meta"]["source"] == "live"
            assert res["_meta"]["reverse_lookup_supported"] is False
        finally:
            reader.close()

    @pytest.mark.parametrize("query", ["", "X" * 20_000])
    def test_validation_capability_failure_stays_bounded(self, cf_indexed, monkeypatch, query):
        """Capability I/O не должен превращать validation-error в helper crash.

        Исходники и индекс не меняются во время чтения; инъекция моделирует
        статично повреждённую/недоступную БД либо duck-typed reader. Существующий
        `_read_build_capabilities()` именно для этой helper-границы глотает такой
        отказ и возвращает None.
        """
        import sqlite3

        def _capability_failure():
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(cf_indexed.reader, "get_build_capabilities", _capability_failure)
        bsl = _make_bsl(cf_indexed.root, idx_reader=cf_indexed.reader)
        res = bsl["analyze_subsystem"](query)
        assert "error" in res
        assert res["_meta"]["source"] == "live"
        assert res["_meta"]["reverse_lookup_supported"] is False

    def test_foreign_reader_never_publishes_another_roots_subsystems(self, cf_foreign_subsystem_index, monkeypatch):
        """Два статичных корня: reader B не является данными current-root A.

        Запрет на `glob_files` ридера сужен до ПАТТЕРНОВ ПОДСИСТЕМ намеренно:
        предмет этого теста — каталог кандидатов `analyze_subsystem`. Общий
        BSL-каталог (`**/*.bsl`) под foreign reader — отдельный пре-существующий
        домен `_ensure_index`, который этот релиз не трогает; запрет на любой
        вызов ловил бы именно его и о subsystem-границе не говорил бы ничего.
        """
        fx = cf_foreign_subsystem_index
        monkeypatch.setattr(
            fx.reader,
            "get_subsystem_lookup",
            lambda *_: pytest.fail("foreign reader не должен участвовать в lookup"),
        )
        _real_glob = fx.reader.glob_files

        def _no_subsystem_glob(pattern, *a, **kw):
            if "Subsystems" in str(pattern):
                pytest.fail("foreign reader не должен участвовать в live-перечислении подсистем")
            return _real_glob(pattern, *a, **kw)

        monkeypatch.setattr(fx.reader, "glob_files", _no_subsystem_glob)
        bsl = _make_bsl(fx.root, idx_reader=fx.reader)
        alien = bsl["analyze_subsystem"]("ТолькоБ")
        assert "error" in alien
        assert alien["_meta"]["source"] == "live"
        assert alien["_meta"]["reverse_lookup_supported"] is False
        own = bsl["analyze_subsystem"]("ТолькоА")
        assert own["_meta"]["source"] == "live"
        assert [r["name"] for r in own["subsystems"]] == ["ТолькоА"]

    def test_legacy_reader_without_new_lookup_falls_back(self, cf_indexed, monkeypatch):
        """Старый duck-reader получает настоящий FS-live, а не index-backed glob."""

        class LegacyReader:
            def __init__(self, real):
                self._real = real

            def __getattr__(self, attr):
                if attr == "get_subsystem_lookup":
                    raise AttributeError(attr)
                return getattr(self._real, attr)

        legacy = LegacyReader(cf_indexed.reader)
        monkeypatch.setattr(
            cf_indexed.reader,
            "glob_files",
            lambda *_: pytest.fail("source=live не должен читать file_paths reader-а"),
        )
        bsl = _make_bsl(cf_indexed.root, idx_reader=legacy)
        res = bsl["analyze_subsystem"]("ПодсистемаА")
        assert res["_meta"]["source"] == "live"
        assert [r["name"] for r in res["subsystems"]] == ["ПодсистемаА"]

    def test_analyze_subsystem_no_metadata_fallback_sees_pre_session_stale_file(self, tmp_path, monkeypatch):
        """R113: static during read != index and FS are the same snapshot.

        Индекс строится без metadata, затем ДО создания helper-сессии появляется
        валидный XML подсистемы. Во время самого вызова дерево уже неизменно.
        `source=live` обязан увидеть файл с диска и не обращаться к stale file_paths.
        """
        root = tmp_path / "cf"
        _write(root / "Configuration.xml", _CF_DESCRIPTOR)
        _write(root / "CommonModules" / "Фикстура" / "Ext" / "Module.bsl", _FIXTURE_MODULE_BSL)
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx_no_meta"))
        db = IndexBuilder().build(str(root), build_calls=False, build_metadata=False, build_fts=False)
        reader = IndexReader(db)
        try:
            _write(
                root / "Subsystems" / "ПослеИндекса.xml",
                _subsystem_xml("ПослеИндекса", ["Document.Новый"]),
            )
            monkeypatch.setattr(
                reader,
                "glob_files",
                lambda *_: pytest.fail("live-fallback не должен читать stale file_paths"),
            )
            bsl = _make_bsl(root, idx_reader=reader)
            res = bsl["analyze_subsystem"]("ПослеИндекса")
            assert res["_meta"]["source"] == "live"
            assert res["subsystems"][0]["raw_content"] == ["Document.Новый"]
        finally:
            reader.close()

    def test_fs_only_glob_seam_stays_in_private_io(self, tmp_path):
        """Приватный callback не становится helper-ом и не проходит wrapping."""
        sink = {}
        generic, _ = make_helpers(str(tmp_path), _private_io=sink)
        assert "glob_files_fs" not in generic
        assert callable(sink["glob_files_fs"])

    def test_limit_guard_is_documented_default_not_crash(self, cf_indexed):
        bsl = _make_bsl(cf_indexed.root, idx_reader=cf_indexed.reader)
        res = bsl["analyze_subsystem"]("ПодсистемаА", limit="много")
        assert res["_meta"]["limit"] == 200

    @pytest.mark.parametrize("style", ["dotdot", "absolute"])
    def test_poisoned_candidate_never_escapes_the_sandbox_boundary(self, cf_empty_subsystem, monkeypatch, style):
        """R126: кандидат вне корня отсекается существующей границей, не новым кодом.

        Внешний файл РЕАЛЬНО СОЗДАЁТСЯ и содержит узнаваемый состав. Без этого
        тест был бы вечнозелёным: несуществующий путь даёт `FileNotFoundError`,
        который гасится тем же `except Exception: continue`, и реализация вовсе
        без границы прошла бы проверку.

        Оба пути подобраны так, чтобы ПРОЙТИ структурный prefilter (родитель —
        `Subsystems`, stem равен запросу): иначе тест зеленел бы на prefilter и
        саму границу не проверял. Инъекция статична — подменяется ответ ридера,
        дерево и индекс во время чтения не меняются.
        """
        import rlm_tools_bsl.bsl_helpers as BH

        foreign_root = cf_empty_subsystem.root.parent / "foreign"
        foreign = foreign_root / "Subsystems" / "Пустая.xml"
        _write(foreign, _subsystem_xml("Пустая", ["Document.УтечкаСостава"]))
        assert foreign.is_file(), "предусловие: внешний файл существует и читаем"
        poisoned = "../foreign/Subsystems/Пустая.xml" if style == "dotdot" else str(foreign)

        reader = cf_empty_subsystem.reader
        real = reader.get_subsystem_lookup

        def _poison(query):
            found = real(query)
            assert found is not None
            return {**found, "direct_candidates": [poisoned]}

        monkeypatch.setattr(reader, "get_subsystem_lookup", _poison)
        parsed: list[int] = []
        real_parse = BH.parse_metadata_xml
        monkeypatch.setattr(BH, "parse_metadata_xml", lambda text: (parsed.append(1), real_parse(text))[1])
        bsl = _make_bsl(cf_empty_subsystem.root, idx_reader=reader)
        res = bsl["analyze_subsystem"]("Пустая")
        assert "error" in res, "кандидат вне корня не имеет права стать строкой ответа"
        assert res["_meta"]["source"] == "index"
        assert parsed == [], "файл вне base+extension roots не должен быть прочитан"
        assert "УтечкаСостава" not in json.dumps(res, ensure_ascii=False)

    def test_live_branch_uses_fs_only_glob_with_reader(self, cf_edt_and_cf, monkeypatch):
        """F1/R113: fallback с reader-ом остаётся live и видит обе раскладки.

        Якорные `Subsystems/**/*.ext` нужны и FS-маршруту. Публичный glob здесь
        намеренно запрещён: иначе `_meta.source='live'` скрывал бы file_paths.
        """
        reader = cf_edt_and_cf.reader
        # Reader есть, но subsystem lookup недоступен -> настоящий current-root FS.
        monkeypatch.setattr(reader, "get_subsystem_lookup", lambda q: None)
        monkeypatch.setattr(
            reader,
            "glob_files",
            lambda *_: pytest.fail("source=live не должен использовать indexed glob"),
        )
        bsl = _make_bsl(cf_edt_and_cf.root, idx_reader=reader)
        for query in ("ВерхнегоУровня", "ЕДТПодсистема"):
            res = bsl["analyze_subsystem"](query)
            assert res["_meta"]["source"] == "live"
            assert [s["name"] for s in res["subsystems"]] == [query], (
                f"{query!r} не найдена FS-live веткой при подключённом reader-е"
            )


class TestAnalyzeSubsystemCurrentRootContract:
    """Поддержанный wrapper-вход: ответ описывает ТЕКУЩИЙ корень, и ровно его.

    Контракт двусторонний, поэтому проверяются ОБА направления: чужая соседняя
    конфигурация не подмешивается, а своя — не теряется.
    """

    def test_index_branch_does_not_publish_a_sibling_roots_subsystem(self, cf_wrapper_indexed):
        """Индекс построен от контейнера и знает ОБЕ одноимённые подсистемы.

        Строка соседа означала бы состав ДРУГОЙ конфигурации, выданный как состав
        текущей, — причём вместе с `extensions_included=False`, то есть с явным
        обещанием, что чужие корни не накладывались.
        """
        bsl = _make_bsl(
            cf_wrapper_indexed.root,
            idx_reader=cf_wrapper_indexed.reader,
            current_config_root=str(cf_wrapper_indexed.current_root),
            **_WRAPPER_KW,
        )
        res = bsl["analyze_subsystem"]("Почта")
        assert res["_meta"]["source"] == "index"
        files = [s["file"].replace("\\", "/") for s in res["subsystems"]]
        assert files == ["MyExt/Subsystems/Почта.xml"], files
        assert res["subsystems"][0]["raw_content"] == ["Document.СвойОбъект"]

    def test_index_branch_keeps_the_whole_answer_on_a_direct_root(self, cf_indexed):
        """Гард — no-op там, где current == base: штатный ответ не меняется."""
        bsl = _make_bsl(cf_indexed.root, idx_reader=cf_indexed.reader)
        res = bsl["analyze_subsystem"]("ПодсистемаА")
        assert res["_meta"]["source"] == "index"
        assert [s["name"] for s in res["subsystems"]] == ["ПодсистемаА"]
        assert res["subsystems"][0]["total_objects"] == 2

    def test_live_branch_finds_the_current_roots_subsystem(self, cf_wrapper_live):
        """Живая ветка ищет подсистемы ОТ ТЕКУЩЕГО корня.

        До фикса якорный `Subsystems/**` резолвился от base (контейнера) и
        существующий `MyExt/Subsystems/Почта.xml` объявлялся ненайденным —
        регресс относительно прежнего рекурсивного `**/Subsystems/**`.
        """
        bsl = _make_bsl(
            cf_wrapper_live.root,
            idx_reader=None,
            current_config_root=str(cf_wrapper_live.current_root),
            **_WRAPPER_KW,
        )
        res = bsl["analyze_subsystem"]("Почта")
        assert "error" not in res, res
        assert res["_meta"]["source"] == "live"
        files = [s["file"].replace("\\", "/") for s in res["subsystems"]]
        assert files == ["MyExt/Subsystems/Почта.xml"], files

    def test_empty_answer_hint_is_executable_in_the_same_session(self, cf_wrapper_live):
        """Совет из hint обязан ИСПОЛНИТЬСЯ там, где он выдан.

        На wrapper-входе `glob_files('Subsystems/**/*.xml')` возвращает пусто —
        агент получил бы подтверждение ложного вывода «подсистем нет».
        """
        import re

        bsl = _make_bsl(
            cf_wrapper_live.root,
            idx_reader=None,
            current_config_root=str(cf_wrapper_live.current_root),
            **_WRAPPER_KW,
        )
        res = bsl["analyze_subsystem"]("НетТакой")
        pattern = re.search(r"glob_files\('([^']+)'\)", res["hint"])
        assert pattern is not None, res["hint"]
        listed = bsl["glob_files"](pattern.group(1))
        assert any("MyExt" in f and "Почта" in f for f in listed), (pattern.group(1), listed)

    def test_hint_leads_with_the_detected_dump_format(self, edt_pure, cf_indexed):
        """Готовый вызов из `hint` ведётся ОПРЕДЕЛЁННЫМ форматом дампа.

        На EDT-выгрузке подсистем в `.xml` нет вовсе (на боевой — 719 `.mdo`
        против 0 `.xml`), поэтому CF-первая подсказка возвращала бы там ЛОЖНЫЙ
        НОЛЬ — неотличимый от честного «подсистем нет». Идиома та же, что у
        соседнего `_resolve_object_xml`: сначала формат этой сессии.
        Маршрут ИСПОЛНЯЕТСЯ, а не сверяется с текстом.
        """
        import re

        for env, want_ext, other_ext in ((edt_pure, ".mdo", "'*.xml'"), (cf_indexed, ".xml", "'*.mdo'")):
            bsl = _make_bsl(env.root, idx_reader=env.reader)
            res = bsl["analyze_subsystem"]("ЗаведомоНетТакой")
            hint = res["hint"]
            call = re.search(r"glob_files\('([^']+)'\)", hint)
            assert call is not None, hint
            pattern = call.group(1)
            assert pattern.endswith(want_ext), (pattern, want_ext, hint)
            assert other_ext in hint, "второй формат обязан остаться названным: " + hint
            assert bsl["glob_files"](pattern), f"готовый маршрут из hint вернул пусто: {pattern!r}"

    @requires_git
    def test_both_recovery_routes_of_the_output_hint_execute(self, git_wrapper_output_budget):
        """ОБА маршрута добора из output-hint обязаны исполниться в ЭТОЙ сессии.

        `git grep` запускается с `-C base_path`, поэтому pathspec резолвится от
        base: в wrapper-сессии `path='Subsystems'` указывает в несуществующий
        `wrapper/Subsystems` и отдаёт ЛОЖНЫЙ НОЛЬ — неотличимый от честного
        «кандидатов нет». Маршруты поэтому не сверяются с текстом, а ЗАПУСКАЮТСЯ.
        """
        import re

        env = git_wrapper_output_budget
        bsl = _make_bsl(
            env.root,
            idx_reader=env.reader,
            current_config_root=str(env.current_root),
            **_WRAPPER_KW,
        )
        assert "git_search" in bsl, "предусловие: дерево под git и helper зарегистрирован"
        # Без сужения limit: упор обязан быть именно JSON-бюджетом вывода — только
        # он и добавляет маршруты добора (`limit=1` даёт крошечный ответ без hint).
        res = bsl["analyze_subsystem"]("Широкая")
        assert res["subsystems"][0]["content_truncated"] is True, "предусловие: упора вывода нет"
        hint = res["hint"]

        git_call = re.search(
            r"git_search\('([^']*)',path='([^']*)',file_types='([^']*)',mode='([^']*)'\)",
            hint,
        )
        assert git_call is not None, f"git-маршрут пропал из hint: {hint}"
        query, git_path, file_types, mode = git_call.groups()
        routed = bsl["git_search"](query, path=git_path, file_types=file_types, mode=mode)
        assert routed["results"], f"готовый git-маршрут из hint вернул пусто при существующем файле: path={git_path!r}"
        assert all("MyExt/" in r["file"].replace("\\", "/") for r in routed["results"]), routed["results"]

        glob_call = re.search(r"glob_files\('([^']+)'\)", hint)
        assert glob_call is not None, hint
        assert bsl["glob_files"](glob_call.group(1)), "glob-альтернатива обязана исполниться"


# ---------------------------------------------------------------------------
# Задача 2. find_based_on_documents — дедуп обеих прямых веток
# ---------------------------------------------------------------------------
class TestBasedOnDedup:
    def test_can_be_created_from_is_deduplicated(self, cf_two_branch_filling):
        """Тип, проверяемый в ДВУХ ветках ОбработкаЗаполнения, — ОДНА строка."""
        bsl = _make_bsl(cf_two_branch_filling.root)
        res = bsl["find_based_on_documents"]("ЦелевойДок")
        types = [r["type"] for r in res["can_be_created_from"]]
        assert types == sorted(set(types), key=types.index), f"дубли: {types}"
        assert len(types) == 2  # фикстура: 3 совпадения на 2 типа

    def test_key_set_of_a_row_is_unchanged(self, cf_two_branch_filling):
        """Дедуп — это ТОЛЬКО дедуп: новых ключей в строке не появляется."""
        bsl = _make_bsl(cf_two_branch_filling.root)
        res = bsl["find_based_on_documents"]("ЦелевойДок")
        for r in res["can_be_created_from"]:
            assert set(r) == {"type", "file"}

    def test_first_occurrence_order_and_spelling_preserved(self, cf_two_branch_filling):
        """Ключ дедупа считается по lower(), но в ответ уезжает ПЕРВОЕ написание:
        1С регистронезависима, а агент увидит в коде именно исходное написание."""
        bsl = _make_bsl(cf_two_branch_filling.root)
        types = [r["type"] for r in bsl["find_based_on_documents"]("ЦелевойДок")["can_be_created_from"]]
        assert types == ["ДокументСсылка.ДваждыПроверяемый", "ДокументСсылка.Однажды"]

    def test_direct_manager_branch_is_deduplicated_too(self, cf_dup_manager_commands):
        """Прямая ветка ManagerModule лечится ТЕМ ЖЕ дедупом (симметрия половин)."""
        bsl = _make_bsl(cf_dup_manager_commands.root)
        res = bsl["find_based_on_documents"]("Источник")
        keys = [(r["document"], r["file"]) for r in res["can_create_from_here"]]
        assert len(keys) == len(set(keys)) == 1
        assert keys[0][0] == "Цель"

    def test_dedup_is_by_relation_not_file_in_both_direct_branches(self, cf_base_and_cfe_same_type):
        """Одна связь из РАЗНЫХ файлов — ОДНА строка: file лишь provenance.

        Фикстура ОБЯЗАНА быть base+CFE (R15): хелпер отбирает модули ТОЛЬКО
        точного целевого документа, поэтому два РАЗНЫХ документа две строки не
        дадут никогда, и тест на такой фикстуре был бы вечнозелёным. Сначала
        положительно проверяем, что ОБА файла каждого вида действительно попали
        в каталог, а затем требуем одну логическую связь в каждой независимо
        реализованной ветке — иначе реализация, потерявшая CFE целиком, прошла бы
        тест ложнозелёно.
        """
        bsl = _make_bsl(
            cf_base_and_cfe_same_type.root,
            extension_paths=[str(cf_base_and_cfe_same_type.cfe)],
        )
        modules = bsl["find_module"]("ЦелевойДок")
        assert len([m for m in modules if m.get("module_type") == "ObjectModule"]) == 2
        assert len([m for m in modules if m.get("module_type") == "ManagerModule"]) == 2
        res = bsl["find_based_on_documents"]("ЦелевойДок")
        same_types = [r for r in res["can_be_created_from"] if r["type"] == "ДокументСсылка.Общий"]
        same_documents = [r for r in res["can_create_from_here"] if r["document"] == "ОбщаяЦель"]
        assert len(same_types) == 1
        assert len(same_documents) == 1
        assert set(same_types[0]) == {"type", "file"}
        assert set(same_documents[0]) == {"document", "file"}

    def test_metadata_union_seed_still_sees_deduped_direct_rows(self, cf_indexed_basedon):
        """Соседний metadata-дедуп засевается из can_create_from_here — он обязан
        продолжать работать поверх уже дедуплицированных строк."""
        indexed = cf_indexed_basedon.reader.find_metadata_references("Document.Источник", kinds=["based_on"], limit=10)
        assert indexed is not None
        assert any(r.get("source_object") == "Цель" for r in indexed), (
            "предусловие: та же связь реально присутствует в metadata-union"
        )
        bsl = _make_bsl(cf_indexed_basedon.root, idx_reader=cf_indexed_basedon.reader)
        res = bsl["find_based_on_documents"]("Источник")
        keys = [
            ((r.get("category") or "Documents").lower(), r["document"].lower()) for r in res["can_create_from_here"]
        ]
        assert len(keys) == len(set(keys))
        target_rows = [r for r in res["can_create_from_here"] if r["document"].lower() == "цель"]
        assert len(target_rows) == 1
        assert "via" not in target_rows[0], "первой сохраняется direct-строка"

    def test_document_flow_embeds_the_same_deduped_contract(self, cf_two_branch_filling):
        """Непосредственный consumer не должен восстановить дубли либо изменить
        форму строк при композиции ответа."""
        bsl = _make_bsl(cf_two_branch_filling.root)
        direct = bsl["find_based_on_documents"]("ЦелевойДок")
        flow = bsl["analyze_document_flow"]("ЦелевойДок")
        assert flow["based_on"] == direct
        for bucket, identity in (
            (direct["can_be_created_from"], lambda row: row["type"].lower()),
            (
                direct["can_create_from_here"],
                lambda row: ((row.get("category") or "Documents").lower(), row["document"].lower()),
            ),
        ):
            keys = [identity(row) for row in bucket]
            assert len(keys) == len(set(keys))


# ---------------------------------------------------------------------------
# Задача 3. get_object_modules — проекция orphan_methods
# ---------------------------------------------------------------------------
class TestObjectModulesOrphans:
    def test_orphan_methods_projected_when_include_methods(self, cf_no_regions):
        """Модуль БЕЗ #Область: totals.methods > 0, outline пуст — имена обязаны
        приехать в orphan_methods, иначе их негде взять без второго вызова."""
        bsl = _make_bsl(cf_no_regions.root, idx_reader=cf_no_regions.reader)
        om = bsl["get_object_modules"]("ТестДок", include_methods=True)
        mod = next(m for m in om["modules"] if m["module_type"] == "ManagerModule")
        assert mod["outline"] == []
        assert mod["totals"]["methods"] == 3
        assert [x["name"] for x in mod["orphan_methods"]] == ["А", "Б", "В"]

    def test_absent_without_include_methods(self, cf_no_regions):
        bsl = _make_bsl(cf_no_regions.root, idx_reader=cf_no_regions.reader)
        om = bsl["get_object_modules"]("ТестДок", include_methods=False)
        assert all("orphan_methods" not in m for m in om["modules"])

    def test_shape_matches_get_module_outline(self, cf_no_regions):
        """Один источник — _get_module_outline_core; формы обязаны совпасть
        поэлементно, иначе появится третий диалект имени метода."""
        bsl = _make_bsl(cf_no_regions.root, idx_reader=cf_no_regions.reader)
        om = bsl["get_object_modules"]("ТестДок", include_methods=True)
        mod = next(m for m in om["modules"] if m["module_type"] == "ManagerModule")
        direct = bsl["get_module_outline"](mod["path"], include_methods=True)
        assert mod["orphan_methods"] == direct["orphan_methods"]

    def test_sum_of_outline_and_orphans_equals_totals(self, cf_mixed_regions):
        """Инвариант полноты: методы в областях + вне областей = totals.methods.
        Без него «orphan_methods есть» ничего не доказывает."""
        bsl = _make_bsl(cf_mixed_regions.root, idx_reader=cf_mixed_regions.reader)
        om = bsl["get_object_modules"]("ТестДок", include_methods=True)
        assert om["modules"], "фикстура вырождена: модулей нет"
        saw_both = False
        for m in om["modules"]:

            def _count(nodes):
                return sum(len(n.get("methods", [])) + _count(n.get("children", [])) for n in nodes)

            in_regions = _count(m["outline"])
            assert in_regions + len(m["orphan_methods"]) == m["totals"]["methods"]
            if in_regions and m["orphan_methods"]:
                saw_both = True
        assert saw_both, "фикстура обязана иметь модуль и с областью, и с методом вне неё"

    def test_object_profile_payload_unchanged(self, cf_no_regions):
        """Compact-профиль зовёт include_methods=False — он НЕ дорожает."""
        bsl = _make_bsl(cf_no_regions.root, idx_reader=cf_no_regions.reader)
        p = bsl["get_object_profile"]("ТестДок", sections=["modules"])
        items = p["sections"]["modules"]["items"]
        assert all("orphan_methods" not in it for it in items)


def test_object_modules_registered_sig_exact():
    from rlm_tools_bsl.bsl_helpers import build_helper_metadata_snapshot

    sig = build_helper_metadata_snapshot()["get_object_modules"]["sig"]
    assert len(sig) == 467
    assert "orphan_methods?" in sig
    assert "# ДЕШЕВЫЙ КОД-СКЕЛЕТ объекта за 1 вызов" not in sig


def test_object_modules_recipe_walk_is_executed_not_paraphrased(cf_nested_regions):
    """Рецепт из `rlm_help` ИСПОЛНЯЕТСЯ на дереве с ВЛОЖЕННОЙ областью.

    Проверять подстроку бессмысленно: плоский `for r in m['outline'] for x in
    r['methods']` синтаксически безупречен и молча теряет методы вложенных
    областей, после чего рецепт утверждает, что сумма покрывает totals.methods.
    Поэтому из рецепта берётся ИМЕННО блок сбора имён и исполняется.
    """
    import textwrap

    bsl = _make_bsl(cf_nested_regions.root, idx_reader=cf_nested_regions.reader)
    det = bsl["get_object_modules"]("ТестДок", include_methods=True)
    mod = next(m for m in det["modules"] if m["module_type"] == "ManagerModule")
    assert mod["outline"] and mod["outline"][0]["children"], "фикстура вырождена: вложенной области нет"
    assert mod["totals"]["methods"] == 3

    recipe = bsl["_registry"]["get_object_modules"]["recipe"]
    lines = recipe.split("\n")
    start = next(i for i, ln in enumerate(lines) if ln.strip().startswith("for m in det['modules']:"))
    end = next(i for i, ln in enumerate(lines) if "in_regions + outside" in ln)
    block = [ln for ln in lines[start:end] if ln.strip() and not ln.strip().startswith("#")]
    code = textwrap.dedent("\n".join(block))
    code += "\n    seen.append((in_regions, outside, m['totals']['methods']))"

    namespace: dict = {"det": det, "seen": []}
    exec(code, namespace)  # noqa: S102 — исполнение рецепта и есть предмет теста
    assert namespace["seen"], "рецепт не обошёл ни одного модуля"
    for in_regions, outside, total in namespace["seen"]:
        assert len(in_regions) + len(outside) == total, (in_regions, outside, total)
    names = {n for in_regions, outside, _ in namespace["seen"] for n in (*in_regions, *outside)}
    assert {"ВнешнийМетод", "ВнутреннийМетод", "МетодВнеОбластей"} <= names, names


def test_object_modules_docstring_matches_orphan_contract(cf_no_regions):
    import inspect

    bsl = _make_bsl(cf_no_regions.root, idx_reader=cf_no_regions.reader)
    doc = inspect.getdoc(bsl["get_object_modules"])
    assert doc is not None
    assert "orphan_methods" in doc and "include_methods=True" in doc


# ---------------------------------------------------------------------------
# Задача 4. find_functional_options — общий ключ имени и include_content
# ---------------------------------------------------------------------------
class TestFunctionalOptionsShape:
    def test_both_buckets_share_the_name_key(self, cf_fo):
        """Общий цикл по двум корзинам одного ответа не должен спотыкаться."""
        bsl = _make_bsl(cf_fo.root, idx_reader=cf_fo.reader)
        res = bsl["find_functional_options"]("ТестДок")
        assert res["xml_options"] and res["code_options"], "предусловие: обе корзины непусты"
        for row in res["xml_options"] + res["code_options"]:
            assert isinstance(row.get("name"), str) and row["name"]

    def test_option_name_kept_for_backcompat(self, cf_fo):
        bsl = _make_bsl(cf_fo.root, idx_reader=cf_fo.reader)
        for row in bsl["find_functional_options"]("ТестДок")["code_options"]:
            assert row["option_name"] == row["name"]

    def test_include_content_false_drops_content_keeps_size(self, cf_fo_fat):
        bsl = _make_bsl(cf_fo_fat.root, idx_reader=cf_fo_fat.reader)
        full = bsl["find_functional_options"]("", include_code=False)
        slim = bsl["find_functional_options"]("", include_code=False, include_content=False)
        # R129: инвариант ПРОИЗВОДИТЕЛЯ — ключ есть всегда и всегда список.
        # Без него `content_size == 0` было бы неотличимо от «состав недоступен»,
        # а `or []` в проекции молча маскировал бы отсутствующий ключ.
        assert all(isinstance(r.get("content"), list) for r in full["xml_options"])
        assert all("content" in r for r in full["xml_options"])
        assert all("content" not in r for r in slim["xml_options"])
        assert [r["content_size"] for r in slim["xml_options"]] == [len(r["content"]) for r in full["xml_options"]]
        # Проекция убирает только тяжёлый content. Эти старые поля нужны
        # потребителям (recipe ныряет по file) и не должны потеряться.
        stable = {"name", "synonym", "location", "file"}
        for before, after in zip(full["xml_options"], slim["xml_options"]):
            assert {k: before[k] for k in stable} == {k: after[k] for k in stable}

    def test_two_levers_together_fit_the_output_budget(self, cf_fo_fat):
        """Смысл параметра — сделать существующую пагинацию ДОСТАТОЧНОЙ.

        Проверяется именно КОМБИНАЦИЯ и именно против реального max_output_chars:
        на боевой конфигурации (198 ФО) ни include_content=False в одиночку
        (48 452 симв.), ни limit=50 в одиночку (28 613) в 15 000 не влезают —
        влезает только пара (12 231). Ассерт «стало вдвое меньше» это НЕ ловит.
        """
        MAX_OUTPUT_CHARS = 15000
        bsl = _make_bsl(cf_fo_fat.root, idx_reader=cf_fo_fat.reader)

        def sz(**kw):
            return len(json.dumps(bsl["find_functional_options"]("", include_code=False, **kw), ensure_ascii=False))

        # Предусловия: фикстура тяжелее бюджета И опций БОЛЬШЕ, чем limit,
        # иначе limit ничего не режет и тест вечнозелёный (R26).
        assert sz() > MAX_OUTPUT_CHARS, "фикстура cf_fo_fat слишком лёгкая"
        base = bsl["find_functional_options"]("", include_code=False)
        assert base["xml_total"] > 50, "опций должно быть больше limit, иначе limit холостой"
        # ГЛАВНОЕ утверждение R19: КАЖДЫЙ рычаг ПООДИНОЧКЕ бюджета НЕ спасает.
        assert sz(include_content=False) > MAX_OUTPUT_CHARS, "один include_content не спасает"
        assert sz(limit=50) > MAX_OUTPUT_CHARS, "один limit не спасает"
        # И только вместе — спасают.
        assert sz(include_content=False, limit=50) <= MAX_OUTPUT_CHARS

    def test_counts_unaffected_by_include_content(self, cf_fo_fat):
        """Отбор строк НЕ зависит от того, сериализуем ли мы состав."""
        bsl = _make_bsl(cf_fo_fat.root, idx_reader=cf_fo_fat.reader)
        a = bsl["find_functional_options"]("", include_code=False)
        b = bsl["find_functional_options"]("", include_code=False, include_content=False)
        assert (a["xml_total"], a["code_total"]) == (b["xml_total"], b["code_total"])
        assert a["xml_total"] == 60 and a["code_total"] == 0

    def test_live_branch_matches_index_branch_on_new_keys(self, cf_fo):
        """Паритет index/live: новые ключи обязаны быть на ОБОИХ путях."""
        idx = _make_bsl(cf_fo.root, idx_reader=cf_fo.reader)
        live = _make_bsl(cf_fo.root)
        a = idx["find_functional_options"]("ТестДок", include_content=False)
        b = live["find_functional_options"]("ТестДок", include_content=False)
        for bucket in ("xml_options", "code_options"):
            assert a[bucket] and b[bucket], f"предусловие: {bucket} достижима"
            assert set(a[bucket][0]) == set(b[bucket][0])
        assert a["code_options"][0]["name"] == a["code_options"][0]["option_name"]
        assert b["code_options"][0]["name"] == b["code_options"][0]["option_name"]

    def test_legacy_top_level_key_set_unchanged(self, cf_fo):
        bsl = _make_bsl(cf_fo.root, idx_reader=cf_fo.reader)
        res = bsl["find_functional_options"]("ТестДок", include_code=False)
        assert set(res) == {"object", "xml_options", "code_options", "total", "xml_total", "code_total"}

    def test_include_content_false_does_not_poison_the_live_cache(self, cf_fo):
        bsl = _make_bsl(cf_fo.root)  # live-ветка, кеш _ensure_functional_options
        bsl["find_functional_options"]("ТестДок", include_content=False)
        again = bsl["find_functional_options"]("ТестДок", include_content=True)
        assert all("content" in r for r in again["xml_options"])
        # R129: тот же инвариант производителя на ЖИВОЙ ветке.
        assert all(isinstance(r["content"], list) for r in again["xml_options"])

    def test_public_docstring_matches_projection_contract(self, cf_fo):
        import inspect

        bsl = _make_bsl(cf_fo.root, idx_reader=cf_fo.reader)
        doc = inspect.getdoc(bsl["find_functional_options"])
        assert doc is not None
        for marker in (
            "include_content",
            "content_size",
            'code_options[i]["name"]',
        ):
            assert marker in doc


def test_functional_options_registered_sig_exact():
    from rlm_tools_bsl.bsl_helpers import build_helper_metadata_snapshot

    sig = build_helper_metadata_snapshot()["find_functional_options"]["sig"]
    assert len(sig) == 412
    for marker in (
        "include_content=True",
        "synonym,location,file,content?|content_size?",
        "code_options:[{name,option_name,file,line}]",
    ):
        assert marker in sig


# ---------------------------------------------------------------------------
# Задача 5. git_search — пофайловый упор назван машинно
# ---------------------------------------------------------------------------
@requires_git
class TestGitSearchPerFileCap:
    def test_truncated_by_names_the_per_file_cap(self, git_repo_many_hits):
        """Наблюдённый сценарий: max_results поднят, ответ тот же. Причина обязана
        быть НАЗВАНА, иначе агент чинит не то."""
        bsl = _make_bsl(git_repo_many_hits.root)
        res = bsl["git_search"]("Если", file_types="bsl", max_results=5000)
        assert res["truncated"] is True
        assert res["truncated_by"] == "per_file"
        assert res["_meta"]["per_file_cap"] == 50
        assert res["_meta"]["files_capped_count"] >= 1
        # Совет обязан быть ИСПОЛНИМЫМ. Рычага max_per_file нет (снят как
        # ресурсно опасный), сужение path потолок НЕ снимает (проверено живьём:
        # даже на одном файле остаётся 50 из 216) — исполним только выход в grep.
        assert "max_results" in res["hint"], "hint обязан сказать, что max_results НЕ поможет"
        assert "grep(" in res["hint"], "hint обязан дать выход в построчный поиск"
        assert "mode='files'" not in res["hint"], (
            "сужение области потолок не снимает — советовать его как решение нельзя"
        )

    def test_raising_max_results_does_not_change_the_answer(self, git_repo_many_hits):
        """Тот самый наблюдённый симптом — воспроизводится тестом, а не только
        в живом прогоне: 200 и 5000 дают ОДНО И ТО ЖЕ."""
        bsl = _make_bsl(git_repo_many_hits.root)
        a = bsl["git_search"]("Если", file_types="bsl", max_results=200)
        b = bsl["git_search"]("Если", file_types="bsl", max_results=5000)
        assert a["returned"] == b["returned"]
        assert a["truncated_by"] == b["truncated_by"] == "per_file"

    def test_truncated_by_is_permanent_and_none_on_full_answer(self, git_repo_small):
        bsl = _make_bsl(git_repo_small.root)
        res = bsl["git_search"]("Процедура")
        assert res["truncated"] is False
        assert res["truncated_by"] is None
        assert "_meta" not in res and "hint" not in res

    def test_registered_sig_does_not_advertise_narrowing(self):
        """Агент читает `sig` на КАЖДОМ старте, а рецепт — по запросу. Ложный совет
        в подписи опаснее, чем в hint: именно он и есть «инструкция по умолчанию»."""
        from rlm_tools_bsl.bsl_helpers import build_helper_metadata_snapshot

        sig = build_helper_metadata_snapshot()["git_search"]["sig"]
        # Маркер — «ready grep», а НЕ подстрока `grep(`: готовый вызов с круглой
        # скобкой живёт в hint, а sig лишь называет маршрут.
        assert "Ready grep" in sig, "sig обязана назвать РАБОЧИЙ выход"
        # Запрещается ПОЛОЖИТЕЛЬНЫЙ совет, а не слово «сужение»: правильная подпись
        # сама говорит «ни max_results, ни сужение path».
        for bad in ("сужай область", "сузь область", "сузить область"):
            assert bad not in sig, f"sig советует неработающий маршрут: {bad!r}"
        assert "max_results/path do not lift it" in sig, "sig обязана явно отрицать оба неработающих рычага"
        assert len(sig) == 409

    def test_hint_route_is_correct_for_a_metacharacter_pattern(self, git_repo_paren_hits):
        """git_search ищет ЛИТЕРАЛ, grep компилирует Python-regex. Совет, отдающий
        сырой pattern, упал бы на литеральной `(` и расширил бы совпадения для `.`.
        """
        import re as _re

        bsl = _make_bsl(git_repo_paren_hits.root)
        res = bsl["git_search"]("Если(", file_types="bsl", max_results=5000)
        assert res["truncated_by"] == "per_file"
        one = res["_meta"]["files_capped"][0]
        # 1) сырой pattern в grep — это ОШИБКА, значит советовать его нельзя
        with pytest.raises(Exception):
            bsl["grep"]("Если(", one)
        # 2) hint содержит ГОТОВЫЙ вызов с реальным путём, не плейсхолдер.
        escaped = _re.escape("Если(")
        call = f"grep({escaped!r}, {one!r})"
        assert call in res["hint"]
        assert "конкретный/путь" not in res["hint"]
        # Исполняются РОВНО аргументы из проверенной строки call.
        assert len(bsl["grep"](escaped, one)) == 60

    def test_posix_ere_hint_does_not_promise_a_false_full_python_fallback(self, git_repo_many_hits):
        """Статичный достижимый контрпример: POSIX-класс не равен Python `re`.

        В каждом файле 60 строк с пробелом и без `]`: git grep -E видит все 60,
        а Python `grep('[[:space:]]', file)` — ни одной.
        """
        import warnings

        bsl = _make_bsl(git_repo_many_hits.root)
        pattern = "[[:space:]]"
        res = bsl["git_search"](pattern, file_types="bsl", regex=True, max_results=5000)
        assert res["truncated_by"] == "per_file"
        assert "ERE" in res["hint"] and "Python re" in res["hint"]
        assert "Полная выдача" not in res["hint"]
        one = res["_meta"]["files_capped"][0]
        assert f"grep({pattern!r}, {one!r})" not in res["hint"]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", FutureWarning)
            assert bsl["grep"](pattern, one) == []

    def test_narrowing_does_NOT_lift_the_cap(self, git_repo_many_hits):
        """Анти-регресс на ложный совет: сужение path до КОНКРЕТНОГО файла
        оставляет ровно 50 строк."""
        bsl = _make_bsl(git_repo_many_hits.root)
        one = bsl["git_search"]("Если", file_types="bsl", mode="files")["results"][0]["file"]
        narrowed = bsl["git_search"]("Если", path=one, max_results=5000)
        assert narrowed["returned"] == 50 and narrowed["truncated_by"] == "per_file"

    def test_hint_route_actually_returns_everything(self, git_repo_many_hits):
        """Совет обязан РЕАЛЬНО давать полный ответ, иначе это второй отказ
        подряд. Проверяется ИСПОЛНЕНИЕМ совета и сверкой с истинным числом."""
        bsl = _make_bsl(git_repo_many_hits.root)
        capped = bsl["git_search"]("Если", file_types="bsl", max_results=5000)
        one = capped["_meta"]["files_capped"][0]
        # Фикстура: 60 совпадений в файле, пофайловый потолок 50.
        assert capped["truncated_by"] == "per_file"
        call = f"grep({'Если'!r}, {one!r})"
        assert call in capped["hint"]
        full = bsl["grep"]("Если", one)  # ровно аргументы из строки hint
        assert len(full) == 60, "grep обязан отдать ВСЕ совпадения файла"
        assert len(full) > 50, "иначе тест не отличает маршрут от упора"

    def test_ignore_case_hint_does_not_claim_exact_python_fallback(self, git_repo_ignore_case_hits):
        """ASCII-пример может случайно совпасть, но не доказывает равенство
        Unicode case-folding у git -i и Python re. Готовой «полной» команды нет."""
        import re as _re

        bsl = _make_bsl(git_repo_ignore_case_hits.root)
        capped = bsl["git_search"]("marker", ignore_case=True, max_results=5000)
        assert capped["truncated_by"] == "per_file"
        one = capped["_meta"]["files_capped"][0]
        py_pattern = f"(?i:{_re.escape('marker')})"
        call = f"grep({py_pattern!r}, {one!r})"
        assert call not in capped["hint"]
        assert "Полная выдача" not in capped["hint"]
        assert "git -i" in capped["hint"] and "IGNORECASE" in capped["hint"]

    def test_generic_blocked_suffix_has_no_false_ready_route(self, git_repo_text_blocked_extension):
        """Git -I читает текстовый .pdf, generic grep исключает его по suffix.
        Подсказка не имеет права называть заведомо пустой маршрут полным."""
        bsl = _make_bsl(git_repo_text_blocked_extension.root)
        capped = bsl["git_search"]("Marker", max_results=5000)
        assert capped["truncated_by"] == "per_file"
        one = capped["_meta"]["files_capped"][0]
        assert one.endswith(".pdf")
        assert bsl["grep"]("Marker", one) == []
        assert "grep(" not in capped["hint"]
        assert "Полная выдача" not in capped["hint"]
        assert ".pdf" in capped["hint"] and "исключает суффикс" in capped["hint"]

    def test_reported_per_file_cap_is_the_enforced_backend_cap(self, monkeypatch, git_repo_many_hits):
        """Одна константа управляет и backend-ом, и `_meta`: иначе диагностируем
        не тот предел, который реально срезал строки."""
        import collections

        import rlm_tools_bsl.bsl_index as bsl_index

        monkeypatch.setattr(bsl_index, "_GIT_GREP_DEFAULT_MAX_PER_FILE", 7)
        bsl = _make_bsl(git_repo_many_hits.root)
        res = bsl["git_search"]("Если", file_types="bsl", max_results=5000)
        per_file = collections.Counter(row["file"] for row in res["results"])
        assert res["truncated_by"] == "per_file"
        assert res["_meta"]["per_file_cap"] == 7
        assert per_file and max(per_file.values()) == 7

    def test_both_caps_reported_as_both(self, git_repo_many_hits):
        """max_results обязан быть МЕНЬШЕ числа строк, уцелевших после пофайлового
        среза, иначе глобального упора нет и ожидание 'both' недостижимо.
        Фикстура: 2 файла x 60 совпадений; пофайловый потолок 50 -> 100 строк."""
        bsl = _make_bsl(git_repo_many_hits.root)
        res = bsl["git_search"]("Если", file_types="bsl", max_results=30)
        assert res["returned"] == 30
        assert res["truncated_by"] == "both"

    def test_global_only_cap_named_max_results(self, git_repo_many_files):
        """Много файлов по одному совпадению: упор ТОЛЬКО глобальный."""
        bsl = _make_bsl(git_repo_many_files.root)
        res = bsl["git_search"]("Маркер", file_types="bsl", max_results=5)
        assert res["truncated_by"] == "max_results"

    def test_files_mode_never_reports_per_file(self, git_repo_many_hits):
        """mode='files' пофайлового потолка не имеет вовсе (нет -m)."""
        bsl = _make_bsl(git_repo_many_hits.root)
        res = bsl["git_search"]("Если", mode="files", max_results=1)
        assert res["truncated_by"] == "max_results"

    def test_error_branch_carries_truncated_by_none(self, git_repo_small):
        """Форма ошибки ЕДИНАЯ: новый постоянный ключ есть и на аварийном пути."""
        bsl = _make_bsl(git_repo_small.root)
        res = bsl["git_search"]("")
        assert res["error"] and res["truncated_by"] is None and res["results"] == []

    def test_public_signature_did_not_grow_a_lever(self, git_repo_many_hits):
        """R16: рычаг max_per_file СНЯТ как ресурсно опасный. Тест держит решение:
        без него параметр вернётся при следующей правке «за компанию»."""
        import inspect

        bsl = _make_bsl(git_repo_many_hits.root)
        params = inspect.signature(bsl["git_search"]).parameters
        assert "max_per_file" not in params

        doc = inspect.getdoc(bsl["git_search"])
        assert doc is not None and "truncated_by" in doc
        assert '"returned": int, "truncated": bool, "error"' not in doc


# ---------------------------------------------------------------------------
# Задача 6. Тёплый старт живого каталога модулей
# ---------------------------------------------------------------------------
class TestParallelScanEquivalence:
    def test_parallel_and_serial_agree_on_a_real_tree(self, deep_tree, monkeypatch):
        """Состав ОБЯЗАН совпасть побайтно; порядок — нет (канон walk его не обещает)."""
        from rlm_tools_bsl.helpers import scan_bsl_tree

        monkeypatch.setenv("RLM_SCAN_WORKERS", "1")
        serial, e1 = scan_bsl_tree(deep_tree.root)
        monkeypatch.setenv("RLM_SCAN_WORKERS", "8")
        par, e2 = scan_bsl_tree(deep_tree.root)
        assert serial, "фикстура вырождена: модулей нет"
        assert sorted(serial) == sorted(par)
        assert e1 == e2

    def test_skip_dirs_and_dotdirs_respected_in_parallel(self, tree_with_skips, monkeypatch):
        from rlm_tools_bsl.helpers import _SKIP_DIRS, scan_bsl_tree

        monkeypatch.setenv("RLM_SCAN_WORKERS", "8")
        paths, _ = scan_bsl_tree(tree_with_skips.root)
        assert paths, "предусловие: обычный модуль обязан найтись"
        assert not any(seg in _SKIP_DIRS or seg.startswith(".") for p in paths for seg in p.split(os.sep))

    def test_enumeration_errors_counted_in_parallel(self, tree_with_unreadable_dir, monkeypatch):
        """Канал ошибок — не украшение: «успешно пусто» вместо «не дошли» и есть
        тот дефект, ради которого scan_bsl_tree их вообще считает."""
        from rlm_tools_bsl.helpers import scan_bsl_tree

        monkeypatch.setenv("RLM_SCAN_WORKERS", "8")
        _, errors = scan_bsl_tree(tree_with_unreadable_dir)
        assert errors >= 1

    def test_internal_redirect_matches_serial(self, tree_with_self_junction, monkeypatch):
        """Редирект не меняет состав и ошибки относительно прежней serial-ветки."""
        from rlm_tools_bsl.helpers import scan_bsl_tree

        monkeypatch.setenv("RLM_SCAN_WORKERS", "1")
        serial, e1 = scan_bsl_tree(tree_with_self_junction.root)
        monkeypatch.setenv("RLM_SCAN_WORKERS", "8")
        parallel, e2 = scan_bsl_tree(tree_with_self_junction.root)
        snorm = sorted(os.path.normcase(p) for p in serial)
        pnorm = sorted(os.path.normcase(p) for p in parallel)
        assert pnorm == snorm
        assert e2 == e1
        assert len(pnorm) == len(set(pnorm))

    def test_external_redirect_matches_serial_and_stays_excluded(self, tree_with_external_redirect, monkeypatch):
        """Статичная внешняя цель не становится модулем ни в одной ветке обхода."""
        from rlm_tools_bsl.helpers import scan_bsl_tree

        tree = tree_with_external_redirect
        monkeypatch.setenv("RLM_SCAN_WORKERS", "1")
        serial, e1 = scan_bsl_tree(tree.root)
        monkeypatch.setenv("RLM_SCAN_WORKERS", "8")
        parallel, e2 = scan_bsl_tree(tree.root)
        snorm = sorted(os.path.normcase(p) for p in serial)
        pnorm = sorted(os.path.normcase(p) for p in parallel)
        leak = os.path.normcase(str(tree.outside_file.resolve()))
        assert pnorm == snorm
        assert e2 == e1
        assert leak not in pnorm
        assert all(os.path.basename(p) != tree.outside_file.name for p in parallel)

    def test_workers_1_is_the_untouched_serial_branch(self, deep_tree, monkeypatch):
        """RLM_SCAN_WORKERS=1 обязан идти ПРЕЖНИМ кодом, а не параллельным с одним
        воркером: иначе «откат» не является откатом."""
        import rlm_tools_bsl.helpers as H

        called = []
        monkeypatch.setattr(H, "_scan_bsl_tree_parallel", lambda *a, **k: called.append(1) or ([], 0))
        monkeypatch.setenv("RLM_SCAN_WORKERS", "1")
        H.scan_bsl_tree(deep_tree.root)
        assert called == []

    @pytest.mark.parametrize(
        "bad,expect",
        [("", 4), ("0", 4), ("-3", 4), ("не число", 4), ("999999", 32), ("8", 8)],
    )
    def test_workers_env_parsing(self, monkeypatch, bad, expect):
        """Значение > 32 УСЕКАЕТСЯ до 32, а не откатывается к дефолту — иначе
        документация и код разойдутся ровно на этом входе."""
        from rlm_tools_bsl.helpers import _scan_workers

        monkeypatch.setenv("RLM_SCAN_WORKERS", bad)
        assert _scan_workers() == expect

    def test_workers_default_is_four_when_env_is_absent(self, monkeypatch):
        """R98: production-default проверяется без env, а не через явное значение.

        Иначе реализация с ошибочным default, но правильным парсингом всех явно
        заданных значений пройдёт параметризованный тест выше.
        """
        from rlm_tools_bsl.helpers import _scan_workers

        monkeypatch.delenv("RLM_SCAN_WORKERS", raising=False)
        assert _scan_workers() == 4

    @pytest.mark.parametrize("bad", ["", "0", "-3", "не число", "999999"])
    def test_invalid_workers_still_scans(self, deep_tree, monkeypatch, bad):
        import rlm_tools_bsl.helpers as H

        monkeypatch.setenv("RLM_SCAN_WORKERS", bad)
        paths, _ = H.scan_bsl_tree(deep_tree.root)
        assert paths  # не падает и не отдаёт пусто

    @pytest.mark.parametrize("starts_before_failure", [0, 2])
    def test_worker_start_failure_drains_on_caller_without_catalog_loss(
        self, deep_tree, monkeypatch, starts_before_failure
    ):
        """R95/R97: нулевой И частичный start-failure не портят каталог.

        Значение 0 проверяет caller-only fallback; 2 — достижимый стык, когда
        часть worker-ов уже работает с общей очередью, а следующий Thread.start
        упирается в ресурсный лимит. Исходное дерево во время теста не меняется.
        """
        import threading as _th

        import rlm_tools_bsl.helpers as H

        monkeypatch.setenv("RLM_SCAN_WORKERS", "1")
        serial, e1 = H.scan_bsl_tree(deep_tree.root)
        real_start = _th.Thread.start
        scan_starts = 0
        started: list[_th.Thread] = []

        def _fail_scan_workers(self, *a, **k):
            nonlocal scan_starts
            if self.name.startswith("rlm-scan-bsl"):
                if scan_starts >= starts_before_failure:
                    raise RuntimeError("can't start new thread")
                scan_starts += 1
                started.append(self)
            return real_start(self, *a, **k)

        monkeypatch.setattr(_th.Thread, "start", _fail_scan_workers)
        monkeypatch.setenv("RLM_SCAN_WORKERS", "8")
        fallback, e2 = H.scan_bsl_tree(deep_tree.root)
        assert sorted(fallback) == sorted(serial)
        assert e2 == e1
        assert scan_starts == starts_before_failure
        assert all(not th.is_alive() for th in started), "scan worker leaked after fallback"

    def test_unexpected_worker_exception_is_rethrown_without_partial_success(self, deep_tree, monkeypatch):
        """Не-OSError из worker-body не может стать ни зависанием, ни частичным [].

        Ошибка инъецируется только в именованный scan-worker на одном заранее
        выбранном вложенном каталоге; serial-ветка и pytest-cleanup не затронуты.
        """
        import pathlib
        import threading as _th

        import rlm_tools_bsl.helpers as H

        bad_dir = next(p for p in pathlib.Path(deep_tree.root).rglob("*") if p.is_dir())
        real_scandir = os.scandir
        fired = _th.Event()

        def _boom(path):
            if (
                os.path.normcase(os.fspath(path)) == os.path.normcase(os.fspath(bad_dir))
                and _th.current_thread().name.startswith("rlm-scan-bsl")
                and not fired.is_set()
            ):
                fired.set()
                raise RuntimeError("worker-body sentinel")
            return real_scandir(path)

        monkeypatch.setattr(os, "scandir", _boom)
        monkeypatch.setenv("RLM_SCAN_WORKERS", "4")
        outcome = {}

        def _drive():
            try:
                outcome["result"] = H.scan_bsl_tree(deep_tree.root)
            except BaseException as exc:  # noqa: BLE001 — тест ловит ЛЮБОЙ исход
                outcome["error"] = exc

        # daemon только для безопасности КРАСНОЙ реализации: её зависший driver
        # не должен удержать весь pytest-процесс после диагностического assert.
        driver = _th.Thread(target=_drive, name="scan-test-driver", daemon=True)
        driver.start()
        driver.join(timeout=10)
        assert not driver.is_alive(), "worker-body exception left caller hung"
        assert fired.is_set(), "fault injection never reached a scan worker"
        assert "result" not in outcome
        assert isinstance(outcome.get("error"), RuntimeError)
        assert str(outcome["error"]) == "worker-body sentinel"
        assert not any(th.is_alive() and th.name.startswith("rlm-scan-bsl") for th in _th.enumerate())


class TestLiveCatalogPrewarm:
    """Предмет класса — САМ прогрев, поэтому он здесь ВКЛЮЧЁН (см. `_prewarm_on`)."""

    @pytest.fixture(autouse=True)
    def _prewarm_on(self, monkeypatch):
        """Класс-локальная ОТМЕНА сюитной `_prewarm_off_by_default`.

        Сюитная фикстура гасит прогрев везде (иначе каждая reader-backed
        конструкция `Sandbox` тащит фоновый `scandir` по tmp_path и ломает уборку
        на Windows). Но для ЭТОГО класса выключенный прогрев означает, что
        проверять нечего: часть тестов проходила бы вхолостую, а тест с барьером
        падал бы по таймауту. Тесты, которым нужен ВЫКЛЮЧЕННЫЙ прогрев или
        production-default, гасят/удаляют переменную у себя явно.
        """
        monkeypatch.setenv("RLM_PREWARM_LIVE_CATALOG", "1")

    def test_prewarm_produces_identical_catalog(self, cf_many_modules, monkeypatch):
        """Прогрев — это ТА ЖЕ функция построения. Он не имеет права дать другой
        каталог, иначе появится второй источник истины.

        Есть ПОЛОЖИТЕЛЬНОЕ предусловие: прогрев обязан РЕАЛЬНО обойти дерево.
        """
        import rlm_tools_bsl.helpers as H

        # production-конфигурация: ридер (иначе канон glob, а не walk) +
        # private_io (иначе scanner зовётся через alias и патч его не видит).
        cold = _make_bsl(cf_many_modules.root, idx_reader=cf_many_modules.reader, private_io=True)
        a = cold["safe_grep"]("Процедура", max_files=500)

        scans: list[int] = []
        real = H.scan_bsl_tree
        monkeypatch.setattr(H, "scan_bsl_tree", lambda root: (scans.append(1), real(root))[1])
        warm = _make_bsl(cf_many_modules.root, idx_reader=cf_many_modules.reader, private_io=True)
        warm["_prewarm_live_catalog"](block=True)
        assert scans, "прогрев не выполнился — тест вырожден, проверять нечего"
        b = warm["safe_grep"]("Процедура", max_files=500)
        assert a["candidates_total"] == b["candidates_total"]
        assert a["results"] == b["results"]
        assert len(scans) == 1, "safe_grep после прогрева обошёл дерево повторно"

    def test_prewarm_scans_the_tree_exactly_once_under_concurrency(self, cf_many_modules, monkeypatch):
        """Считается ЧИСЛО ОБХОДОВ, а не отсутствие дублей в результате.

        Вариант «дублей путей нет» был бы ложнозелёным: четыре конкурентных
        сканирования, каждое вернувшее одинаковый список, дают такой же
        результат — дедуп по casefold схлопнул бы их и скрыл лишнюю работу.

        `block=True`, а НЕ `block=False`: с `block=False` метод сам спавнит
        daemon-поток и сразу возвращается, поэтому join по НАШИМ потокам дождался
        бы спавнеров, а не обхода.
        """
        import rlm_tools_bsl.helpers as H

        calls: list[int] = []
        entered = threading.Event()
        release = threading.Event()
        real = H.scan_bsl_tree

        def _spy(root):
            calls.append(1)
            entered.set()  # первый обходчик доложил, что вошёл
            release.wait(timeout=30)  # и удерживается, пока входят остальные
            return real(root)

        monkeypatch.setattr(H, "scan_bsl_tree", _spy)
        bsl = _make_bsl(cf_many_modules.root, idx_reader=cf_many_modules.reader, private_io=True)
        threads = [threading.Thread(target=bsl["_prewarm_live_catalog"], args=(True,)) for _ in range(4)]
        threads[0].start()
        assert entered.wait(timeout=30), "первый обход не стартовал"
        for th in threads[1:]:
            th.start()  # входят, пока первый ДЕРЖИТ замок
        release.set()
        for th in threads:
            th.join(timeout=60)
        assert not any(th.is_alive() for th in threads)
        assert calls == [1], f"дерево обойдено {len(calls)} раз(а) вместо одного"

    def test_walk_prewarm_does_not_block_glob_fallback(self, cf_many_modules):
        """R120: разные route-cache не должны сериализоваться общим scan-lock."""
        import threading as _th

        from rlm_tools_bsl.bsl_helpers import make_bsl_helpers
        from rlm_tools_bsl.helpers import make_helpers

        walk_entered = _th.Event()
        glob_entered = _th.Event()
        release_walk = _th.Event()
        sink = {}
        generic, resolve_safe = make_helpers(str(cf_many_modules.root), _private_io=sink)
        real_catalog = sink["scan_bsl_catalog_status"]

        def _catalog(route_canon):
            if route_canon == "walk":
                walk_entered.set()
                release_walk.wait(timeout=10)
            elif route_canon == "glob":
                glob_entered.set()
            return real_catalog(route_canon)

        class _FallbackReader:
            def __init__(self, delegate):
                self._delegate = delegate

            def get_all_modules(self):
                return None  # штатный transient/legacy fallback в glob

            def __getattr__(self, name):
                return getattr(self._delegate, name)

        bsl = make_bsl_helpers(
            base_path=str(cf_many_modules.root),
            resolve_safe=resolve_safe,
            read_file_fn=generic["read_file"],
            grep_fn=generic["grep"],
            glob_files_fn=generic["glob_files"],
            format_info=cf_many_modules.format_info,
            idx_reader=_FallbackReader(cf_many_modules.reader),
            catalog_scan_fn=_catalog,
            register_git_search="never",
        )
        walk = _th.Thread(target=bsl["_prewarm_live_catalog"], args=(True,), name="held-walk")
        glob = _th.Thread(target=lambda: bsl["find_module"]("НетТакого"), name="glob-user")
        walk.start()
        assert walk_entered.wait(timeout=5)
        glob.start()
        try:
            assert glob_entered.wait(timeout=5), "glob waited for unrelated walk"
        finally:
            release_walk.set()
            walk.join(timeout=30)
            glob.join(timeout=30)
        assert not walk.is_alive() and not glob.is_alive()

    def test_async_prewarm_has_one_stable_owned_thread(self, cf_many_modules, monkeypatch):
        """Стык фабрика -> Sandbox lifecycle: повторный async-вызов не имеет
        права заменить handle завершившегося/идущего потока вторым обходом."""
        import rlm_tools_bsl.helpers as H

        entered = threading.Event()
        release = threading.Event()
        calls = []
        real = H.scan_bsl_tree

        def _spy(root):
            calls.append(1)
            entered.set()
            release.wait(timeout=30)
            return real(root)

        monkeypatch.setattr(H, "scan_bsl_tree", _spy)
        bsl = _make_bsl(cf_many_modules.root, idx_reader=cf_many_modules.reader, private_io=True)
        prewarm = bsl["_prewarm_live_catalog"]
        prewarm()
        owned = prewarm.thread()
        try:
            assert owned is not None
            assert entered.wait(timeout=30)
            prewarm()
            assert prewarm.thread() is owned
        finally:
            release.set()
            if owned is not None:
                owned.join(timeout=60)
        assert owned is not None
        assert not owned.is_alive()
        assert calls == [1]

    @pytest.mark.parametrize("off_value", ["0", "false", "FALSE", " no ", "off"])
    def test_prewarm_disabled_by_env(self, cf_many_modules, monkeypatch, off_value):
        """Проверяется, что поток НЕ СОЗДАЁТСЯ, а не что он быстро вернулся."""
        import threading as _th

        monkeypatch.setenv("RLM_PREWARM_LIVE_CATALOG", off_value)
        started: list[str] = []
        real_start = _th.Thread.start
        monkeypatch.setattr(
            _th.Thread,
            "start",
            lambda self, *a, **k: (started.append(self.name), real_start(self, *a, **k))[1],
        )
        bsl = _make_bsl(cf_many_modules.root, idx_reader=cf_many_modules.reader, private_io=True)
        bsl["_prewarm_live_catalog"]()
        assert not any(n == "rlm-prewarm-live-catalog" for n in started)

    def test_default_on_prewarm_skips_no_index_glob_cache(self, cf_many_modules, real_sandbox, monkeypatch):
        """R86: default-on не расширяет фон на no-index glob/cache ветку.

        Источники статичны во время чтения. Проверяется достижимый lifecycle-стык:
        process-worker не join-ит daemon при rlm_end, а save_index пишет общий
        file_index.json. Поэтому в этой ветке поток не должен стартовать вообще.
        """
        import threading as _th
        import time

        import rlm_tools_bsl.bsl_helpers as BH
        from rlm_tools_bsl.sandbox_backend import InlineSandboxBackend

        # Class-autouse ставит "1" для остальных тестов прогрева. Здесь ключ
        # удаляется намеренно: проверяется реальный default при отсутствующей env.
        monkeypatch.delenv("RLM_PREWARM_LIVE_CATALOG", raising=False)
        started: list[str] = []
        writers: list[str] = []
        real_start = _th.Thread.start
        monkeypatch.setattr(
            _th.Thread,
            "start",
            lambda self, *a, **k: (started.append(self.name), real_start(self, *a, **k))[1],
        )
        monkeypatch.setattr(BH, "save_index", lambda *a, **k: writers.append(_th.current_thread().name))

        sb = real_sandbox(cf_many_modules.root, idx_reader=None)
        backend = InlineSandboxBackend(sb, None, install_llm_tools=False)
        assert sb._prewarm_thread is None
        assert "rlm-prewarm-live-catalog" not in started
        # `save_index` на этой ветке вызывается СИНХРОННО из `_compute_prefixes`
        # самого конструктора backend — это прежнее поведение, и запрещать его
        # нечем. Опасность именно в ФОНОВОЙ записи: daemon-поток process-worker
        # при `rlm_end` не join-ится, а общий `file_index.json` он бы дописывал
        # уже после закрытия сессии. Поэтому проверяется ВЛАДЕЛЕЦ записи.
        assert writers, "предусловие: no-index ветка вообще пишет кеш"
        assert all(name != "rlm-prewarm-live-catalog" for name in writers)
        backend.request_close("test")
        assert backend.finish_close(time.monotonic() + 5).closed is True

    def test_prewarm_thread_start_failure_keeps_sandbox_and_lazy_path_usable(
        self, cf_many_modules, real_sandbox, monkeypatch
    ):
        """R95: запрет потоков не ломает init уже существующего владельца."""
        import threading as _th
        import time

        from rlm_tools_bsl.sandbox_backend import InlineSandboxBackend

        real_start = _th.Thread.start

        def _fail_only_prewarm(self, *a, **k):
            if self.name == "rlm-prewarm-live-catalog":
                raise RuntimeError("can't start new thread")
            return real_start(self, *a, **k)

        monkeypatch.setattr(_th.Thread, "start", _fail_only_prewarm)
        sb = real_sandbox(cf_many_modules.root, idx_reader=cf_many_modules.reader)
        # Sandbox только подготовил callable; стартует последний шаг backend init.
        assert sb._prewarm_thread is None
        backend = InlineSandboxBackend(sb, cf_many_modules.reader, install_llm_tools=False)
        assert sb._prewarm_thread is None
        # Та же reader-backed функция остаётся доступна лениво. Scanner-worker-ы
        # имеют другие имена, поэтому тест изолирует именно prewarm-start стык.
        result = sb._namespace["safe_grep"]("Процедура", max_files=500)
        assert result["candidates_total"] > 0
        backend.request_close("test")
        assert backend.finish_close(time.monotonic() + 5).closed is True

    def test_prewarm_body_failure_does_not_poison_catalog_or_hide_error(self, cf_many_modules, monkeypatch):
        """R124: ошибка ТЕЛА фонового потока — не отказ сессии и не потеря ошибки."""
        import rlm_tools_bsl.helpers as H

        real = H.scan_bsl_tree

        def _boom(root):
            raise RuntimeError("prewarm-body sentinel")

        monkeypatch.setattr(H, "scan_bsl_tree", _boom)
        bsl = _make_bsl(cf_many_modules.root, idx_reader=cf_many_modules.reader, private_io=True)
        prewarm = bsl["_prewarm_live_catalog"]
        prewarm()
        owned = prewarm.thread()
        assert owned is not None
        owned.join(timeout=60)
        assert not owned.is_alive(), "ошибка тела не имеет права подвесить поток"
        # Handle остаётся: фон однократен, автоматического повтора нет.
        prewarm()
        assert prewarm.thread() is owned

        # Ошибка не проглочена для ПОТРЕБИТЕЛЯ: тот же маршрут синхронно.
        with pytest.raises(RuntimeError, match="prewarm-body sentinel"):
            bsl["safe_grep"]("Процедура", max_files=500)

        # И ничего не отравлено: без инъекции каталог строится обычным образом.
        monkeypatch.setattr(H, "scan_bsl_tree", real)
        ok = bsl["safe_grep"]("Процедура", max_files=500)
        assert ok["candidates_total"] > 0

    @pytest.mark.parametrize("prewarm_env", ["1", "0"])
    def test_prewarm_is_not_exposed_to_the_agent_namespace(
        self, cf_many_modules, real_sandbox, monkeypatch, prewarm_env
    ):
        """R4: `_wrap_helpers` приватные ключи НЕ фильтрует, значит изъятие обязано
        быть явным — иначе агент зовёт прогрев как хелпер.

        Проверяется при ОБОИХ значениях env: `pop` из словаря хелперов обязан
        происходить БЕЗУСЛОВНО, а гасить прогрев обязан сам
        `_prewarm_live_catalog`.
        """
        monkeypatch.setenv("RLM_PREWARM_LIVE_CATALOG", prewarm_env)
        sb = real_sandbox(cf_many_modules.root, idx_reader=cf_many_modules.reader)
        assert "_prewarm_live_catalog" not in sb._namespace
        out = sb.execute("_prewarm_live_catalog()")
        # Sandbox.execute возвращает ExecutionResult (dataclass), а не dict.
        assert out.error, "имя не должно быть вызываемым из кода агента"

    def test_prewarm_does_not_trigger_metadata_phase(self, cf_with_extensions, monkeypatch):
        """Прогрев греет ТОЛЬКО перечисление BSL-каталога. Он не имеет права
        неявно вызвать общий _ensure_index() и пройти metadata XML/синонимы."""
        import rlm_tools_bsl.bsl_index as BI

        touched = []
        real = BI._iter_metadata_xml_files
        monkeypatch.setattr(
            BI,
            "_iter_metadata_xml_files",
            lambda *a, **k: (touched.append(1), real(*a, **k))[1],
        )
        import rlm_tools_bsl.helpers as H

        scans: list[int] = []
        real_scan = H.scan_bsl_tree
        monkeypatch.setattr(H, "scan_bsl_tree", lambda root: (scans.append(1), real_scan(root))[1])
        # extension_paths ОБЯЗАТЕЛЕН (R49): `_iter_metadata_xml_files` зовётся
        # ТОЛЬКО в фазе расширений, для прямой фабрики они не обнаруживаются сами.
        bsl = _make_bsl(
            cf_with_extensions.root,
            idx_reader=cf_with_extensions.reader,
            private_io=True,
            extension_paths=[str(cf_with_extensions.cfe)],
        )
        bsl["_prewarm_live_catalog"](block=True)
        assert scans, "прогрев не выполнился — тест вырожден"
        assert touched == [], "прогрев не имеет права запускать metadata/synonym фазу"
        # ... а обычный потребитель metadata её по-прежнему запускает.
        bsl["find_attributes"](object_name="ТестДок")
        assert touched, "предусловие: metadata-фаза вообще достижима в этой фикстуре"

    def test_prewarm_starts_only_after_real_inline_backend_owns_sandbox(
        self, cf_many_modules, real_sandbox, monkeypatch
    ):
        """WIRING: ownerless Sandbox молчит, успешный backend запускает default-on."""
        import threading as _th
        import time

        from rlm_tools_bsl.sandbox_backend import InlineSandboxBackend

        # `_prewarm_on` ставит "1" для класса, но этот тест про публичный DEFAULT.
        monkeypatch.delenv("RLM_PREWARM_LIVE_CATALOG", raising=False)
        started: list = []
        real_start = _th.Thread.start

        def _record(self, *a, **k):
            started.append(self)
            return real_start(self, *a, **k)

        monkeypatch.setattr(_th.Thread, "start", _record)
        sb = real_sandbox(cf_many_modules.root, idx_reader=cf_many_modules.reader)
        assert "rlm-prewarm-live-catalog" not in [th.name for th in started]
        backend = InlineSandboxBackend(sb, cf_many_modules.reader, install_llm_tools=False)
        names = [th.name for th in started]
        assert "rlm-prewarm-live-catalog" in names
        # ОБЯЗАТЕЛЬНО дождаться: поток демонический и ходит по tmp_path, а pytest
        # начнёт удалять дерево сразу после теста.
        for th in started:
            if th.name == "rlm-prewarm-live-catalog":
                th.join(timeout=60)
                assert not th.is_alive(), "прогрев не завершился — tmp_path удалять нельзя"
        backend.request_close("test")
        assert backend.finish_close(time.monotonic() + 5).closed is True

    def test_inline_init_failure_never_starts_ownerless_prewarm(self, cf_many_modules, real_sandbox, monkeypatch):
        """Registry init failure достижим после Sandbox(), но до возврата backend."""
        import threading as _th

        from rlm_tools_bsl.sandbox_backend import InlineSandboxBackend

        started: list[str] = []
        real_start = _th.Thread.start

        def _record(self, *a, **k):
            started.append(self.name)
            return real_start(self, *a, **k)

        monkeypatch.setattr(_th.Thread, "start", _record)
        sb = real_sandbox(cf_many_modules.root, idx_reader=cf_many_modules.reader)

        def _registry_failure():
            raise RuntimeError("session helpers missing from metadata catalog")

        monkeypatch.setattr(sb, "registry_metadata_snapshot", _registry_failure)
        with pytest.raises(RuntimeError, match="metadata catalog"):
            InlineSandboxBackend(sb, cf_many_modules.reader, install_llm_tools=False)
        assert sb._prewarm_thread is None
        assert "rlm-prewarm-live-catalog" not in started

    def test_process_worker_starts_prewarm_after_registry_and_prefixes(self):
        """R131: статический tripwire порядка — по AST, а не по тексту.

        Это именно tripwire, а НЕ второе независимое доказательство wiring:
        фактический запуск наблюдают runtime-тесты (реальный worker-процесс с
        индексом и inline-пара, где `_start_owned_prewarm` — общий one-shot hook
        обоих владельцев). Через IPC приватный поток не наблюдаем, и ради теста
        protocol не расширяется.
        """
        import ast
        import inspect
        import textwrap

        from rlm_tools_bsl.sandbox_worker import sandbox_worker_main

        fn = ast.parse(textwrap.dedent(inspect.getsource(sandbox_worker_main))).body[0]

        def _walk(stmts, chain):
            for index, st in enumerate(stmts):
                yield st, stmts, index, chain
                for field in ("body", "orelse", "finalbody"):
                    inner = getattr(st, field, None)
                    if isinstance(inner, list):
                        yield from _walk(inner, (*chain, st))
                for handler in getattr(st, "handlers", []):
                    yield from _walk(handler.body, (*chain, st, handler))

        found = [
            (stmts, index, chain)
            for st, stmts, index, chain in _walk(fn.body, ())
            if ast.unparse(st) == "sandbox._start_owned_prewarm()"
        ]
        assert len(found) == 1, "старт прогрева обязан быть ровно один"
        stmts, start_index, chain = found[0]
        assert not any(isinstance(node, (ast.If, ast.For, ast.While, ast.ExceptHandler)) for node in chain), (
            "прогрев стартует безусловно, а не в ветке условия/обработчика"
        )
        texts = [ast.unparse(st) for st in stmts]
        registry = next(i for i, t in enumerate(texts) if "sandbox.registry_metadata_snapshot()" in t)
        prefixes = next(i for i, t in enumerate(texts) if "_compute_prefixes(sandbox, idx_reader)" in t)
        assert registry < prefixes < start_index, "порядок init нарушен"
        publish = next(st for st, *_ in _walk(fn.body, ()) if ast.unparse(st).startswith("init_ok = make_message("))
        assert publish.lineno > stmts[start_index].lineno

    def test_prewarm_yields_the_same_catalog_under_race(self, cf_many_modules, monkeypatch):
        """ПРОВЕРЯЕТ: при работающем фоновом обходе немедленный live-вызов получает
        ТОТ ЖЕ каталог, что и без прогрева. НЕ проверяет время: поток асинхронный,
        а вызов, пришедший сразу, ШТАТНО ждёт тот же замок. Время меряется на
        боевой конфигурации, где есть настоящее дерево и настоящая пауза.
        """
        monkeypatch.setenv("RLM_PREWARM_LIVE_CATALOG", "1")  # дублирует `_prewarm_on` намеренно
        warm = _make_bsl(cf_many_modules.root, idx_reader=cf_many_modules.reader, private_io=True)
        warm["_prewarm_live_catalog"]()  # фоном, НЕ ждём — нужна именно гонка
        a = warm["safe_grep"]("Процедура", max_files=500)  # встанет на тот же замок
        monkeypatch.setenv("RLM_PREWARM_LIVE_CATALOG", "0")
        cold = _make_bsl(cf_many_modules.root, idx_reader=cf_many_modules.reader, private_io=True)
        b = cold["safe_grep"]("Процедура", max_files=500)
        assert a["candidates_total"] == b["candidates_total"]
        assert a["results"] == b["results"]
        # Гонка закончилась: `safe_grep` выше уже прошёл через тот же замок.


# ---------------------------------------------------------------------------
# Задача 7. Сквозные consumer-контракты (стратегии, рецепты, доки)
# ---------------------------------------------------------------------------
def test_agent_facing_routes_match_subsystem_and_fo_contracts():
    from rlm_tools_bsl import bsl_knowledge as K
    from rlm_tools_bsl.bsl_strategy_data import STRATEGY_SECTIONS

    slim_perf = STRATEGY_SECTIONS["performance"]
    # Пара Step 0 лежит в секции "workflow", а не "critical".
    slim_workflow = STRATEGY_SECTIONS["workflow"]
    full = K._STRATEGY_HEADER
    perf_route = (
        "find_functional_options(obj_name,limit=10) → ФО объекта; широкий "
        "XML-обзор: find_functional_options('',include_code=False,"
        "include_content=False,limit=50), детали состава по file"
    )
    for text in (slim_perf, full):
        assert perf_route in text
    subsystem_route = (
        "No recipe? → analyze_subsystem('Подсистема'); current-root; uncut "
        "known rows:all direct:!content_truncated&"
        "subsystems_found==len(subsystems);live:no reverse"
    )
    # Байтовый гард ЯЧЕЙКИ (R127): «BUSINESS RECIPE? Follow it.» (27) + перевод
    # строки + маршрут (156) = 184 при прежних 186. Держатся ОБЕ строки и их
    # соседство, а не только вторая. Отступ секции в два пробела учтён явно.
    import re

    recipe_line = "BUSINESS RECIPE? Follow it."
    assert (len(recipe_line), len(subsystem_route)) == (27, 156)
    pair = re.compile(re.escape(recipe_line) + r"\n[ \t]*" + re.escape(subsystem_route))
    for text in (slim_workflow, full):
        assert pair.search(text), "пара Step 0 обязана остаться СОСЕДНИМИ строками"
        assert "for domain overview" not in text

    subsystem_lines = [
        line
        for recipe in K._BUSINESS_RECIPES.values()
        for level in ("compact", "full")
        for line in recipe[level]
        if "analyze_subsystem(" in line
    ]
    assert subsystem_lines
    assert all(
        "uncut:all direct:!content_truncated" in line
        and "subsystems_found==len(subsystems)" in line.replace(" ", "")
        and "current-root" in line
        for line in subsystem_lines
    )
    assert all("все объекты" not in line.lower() for line in subsystem_lines)
    assert all("full:" not in line.lower() for line in subsystem_lines)

    rights_full = "\n".join(K._BUSINESS_RECIPES["права"]["full"])
    assert "find_functional_options('',include_code=False,include_content=False,limit=50)" in rights_full

    old_full_lengths = {
        "себестоимость": 612,
        "распределение": 573,
        "печать": 465,
        "права": 1118,
    }
    for domain, ceiling in old_full_lengths.items():
        assert sum(map(len, K._BUSINESS_RECIPES[domain]["full"])) <= ceiling


def test_subsystem_response_completeness_requires_every_direct_row(cf_case_variants):
    """R85: полный row-count не компенсирует усечение второй direct-строки.

    Это тест двух транспортных границ, не coverage всех XML текущего корня.
    """
    bsl = _make_bsl(cf_case_variants.root, idx_reader=cf_case_variants.reader)
    res = bsl["analyze_subsystem"]("Почта", limit=1)
    direct = [r for r in res["subsystems"] if r["match"] in {"name", "synonym"}]
    assert len(direct) == 2, "предусловие: регистровые дубли direct достижимы"
    assert res["subsystems_found"] == len(res["subsystems"])
    assert any(r["content_truncated"] for r in direct)


def test_full_analysis_prompt_and_agent_instructions_match_release_contracts():
    """Доки-потребители не имеют права описывать снятые контракты.

    `docs/AGENT_INSTRUCTIONS.md` ПРЯМО запрещал брать состав из
    `analyze_subsystem`; после Задачи 1 это ложь, и агент по старой инструкции
    продолжал бы читать сырой XML вручную. `docs/full_analysis_prompt.md`
    описывал сбор имён методов без `orphan_methods` и старую точную error-форму
    `git_search`.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "docs"
    agent = (root / "AGENT_INSTRUCTIONS.md").read_text(encoding="utf-8")
    prompt = (root / "full_analysis_prompt.md").read_text(encoding="utf-8")

    assert "content_truncated" in agent
    assert "subsystems_found == len(subsystems)" in agent
    assert "extensions_included" in agent
    assert "reverse_lookup_supported" in agent
    # Снятый запрет: прежняя формулировка «запрещено использовать как источник состава».
    assert "запрещено использовать как источник состава" not in agent

    assert "orphan_methods" in prompt
    assert "truncated_by: None" in prompt
    assert "truncated: False, error" not in prompt


def test_empty_answer_hints_advise_an_executable_glob_route(cf_indexed, cf_name_fragment):
    """Совет из hint обязан ИСПОЛНЯТЬСЯ на ТОЙ ЖЕ ветке, где он выдан.

    `Subsystems/**` до Python 3.13 матчит ТОЛЬКО каталоги, поэтому на FS-маршруте
    (живая ветка — ровно там, где совет и нужен) `glob_files` отдавал бы
    диагностическую строку-заглушку вместо списка файлов.
    """
    indexed = _make_bsl(cf_indexed.root, idx_reader=cf_indexed.reader)
    live = _make_bsl(cf_name_fragment.root)
    for bsl in (indexed, live):
        res = bsl["analyze_subsystem"]("НетТакойПодсистемыИлиОбъекта")
        assert "error" in res
        assert "glob_files('Subsystems/**/*.xml')" in res["hint"]
        listed = bsl["glob_files"]("Subsystems/**/*.xml")
        assert listed, "совет из hint не вернул ни одного файла"
        assert all(f.lower().endswith(".xml") for f in listed), listed
        # А прежний совет на FS-маршруте файлов НЕ даёт — это и был дефект.
        assert "glob_files('Subsystems/**')." not in res["hint"]


# ---------------------------------------------------------------------------
# Подсказка песочницы про импорты (находка приёмо-сдаточного e2e v1.36.0)
# ---------------------------------------------------------------------------
def test_restricted_import_hint_names_the_actual_whitelist(tmp_path, real_sandbox):
    """Подсказка при отказе импорта обязана называть ФАКТИЧЕСКОЕ правило.

    Прежний текст гласил «Only standard library modules are allowed», и это НЕВЕРНО:
    ``time``, ``os``, ``datetime`` — ровно стандартная библиотека, и они заблокированы,
    потому что гейт работает по белому списку. Агент читал подсказку, делал единственный
    возможный из неё вывод и упирался снова, теряя вызов — дефект пойман живым прогоном,
    а не юнитами, и жил с v1.2.0.

    Проверяется И отсутствие ложного утверждения, И наличие ПОЛНОГО состава белого списка:
    строка сверяется с самой константой, поэтому текст не сможет разъехаться с гейтом.
    """
    from rlm_tools_bsl.sandbox import ALLOWED_MODULES

    _write(tmp_path / "Configuration.xml", _CF_DESCRIPTOR)
    sb = real_sandbox(tmp_path)

    res = sb.execute("import time")
    err = res.error or ""

    assert err, "импорт вне белого списка обязан быть отклонён — иначе тест ничего не стережёт"
    assert "standard library modules are allowed" not in err.lower(), (
        "вернулось ложное утверждение: stdlib разрешена НЕ вся, гейт работает по белому списку"
    )
    assert ", ".join(sorted(ALLOWED_MODULES)) in err, "подсказка не называет фактический белый список целиком: " + err

    # Разрешённый модуль по-прежнему проходит — гейт не заужен починкой текста.
    ok = sb.execute("import json; print(json.dumps({'k': 1}))")
    assert ok.error is None, ok.error
    assert '"k"' in ok.stdout
