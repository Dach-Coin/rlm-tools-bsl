"""v1.37.0 — «одно понятие — одно представление».

Релиз read-time: `BUILDER_VERSION` = 15, схема SQLite, формат данных и выход
сборщика не тронуты, пересборка индексов не требуется.
"""

from __future__ import annotations

import os

import pytest

from rlm_tools_bsl.helpers import make_helpers

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


# ---------------------------------------------------------------------------
# Задача 1. Одно представление относительного пути
# ---------------------------------------------------------------------------
#
# Инвариант «ни одного `\` на любой ОС» НЕВЕРЕН: на POSIX обратный слэш — легальный
# символ ИМЕНИ файла, и `as_posix()` его не убирает. Поэтому инварианта три, и они
# разделены по платформам.
def _tree_with_nested_module(tmp_path):
    obj = tmp_path / "Documents" / "Заказ" / "Ext"
    obj.mkdir(parents=True)
    (obj / "ObjectModule.bsl").write_text("Процедура П() Экспорт\nКонецПроцедуры\n", encoding="utf-8")
    return obj / "ObjectModule.bsl"


def test_generic_helpers_publish_posix_relative_paths(tmp_path):
    """Путь из контролируемых сегментов (без литерального `\\`) разделён `/`."""
    _tree_with_nested_module(tmp_path)
    helpers, _ = make_helpers(str(tmp_path))

    globbed = helpers["glob_files"]("**/*.bsl")
    assert globbed == ["Documents/Заказ/Ext/ObjectModule.bsl"], globbed

    found = helpers["find_files"]("ObjectModule")
    assert found == ["Documents/Заказ/Ext/ObjectModule.bsl"], found

    grepped = helpers["grep"]("Процедура")
    assert [m["file"] for m in grepped] == ["Documents/Заказ/Ext/ObjectModule.bsl"], grepped

    # grep_summary / grep_read отдают тот же `file` транзитом (у grep_read он —
    # КЛЮЧ словарей `matches` и `files`, поэтому расхождение там ещё заметнее).
    assert "Documents/Заказ/Ext/ObjectModule.bsl" in helpers["grep_summary"]("Процедура")
    read = helpers["grep_read"]("Процедура")
    assert list(read["matches"]) == ["Documents/Заказ/Ext/ObjectModule.bsl"], read
    assert list(read["files"]) == ["Documents/Заказ/Ext/ObjectModule.bsl"], read

    # tree: метка корня — тот же POSIX.
    assert helpers["tree"]("Documents/Заказ").splitlines()[0] == "Documents/Заказ"


@pytest.mark.skipif(os.name != "nt", reason="разделитель `\\` появляется только на Windows")
def test_no_native_separator_leaks_on_windows(tmp_path):
    """На Windows в выдаче generic-хелперов нет ни одного `\\`."""
    _tree_with_nested_module(tmp_path)
    helpers, _ = make_helpers(str(tmp_path))

    published = [
        *helpers["glob_files"]("**/*.bsl"),
        *helpers["find_files"]("ObjectModule"),
        *[m["file"] for m in helpers["grep"]("Процедура")],
        *helpers["grep_read"]("Процедура")["matches"],
        *helpers["grep_read"]("Процедура")["files"],
        helpers["grep_summary"]("Процедура"),
        helpers["tree"]("Documents/Заказ"),
    ]
    for value in published:
        assert "\\" not in value, f"нативный разделитель утёк наружу: {value!r}"


@pytest.mark.skipif(os.name == "nt", reason="`\\` в имени файла на Windows недопустим")
def test_backslash_inside_a_posix_file_name_is_preserved(tmp_path):
    """На POSIX `\\` — символ ИМЕНИ, а не разделитель: слепой replace его бы съел."""
    (tmp_path / "Ext").mkdir()
    weird = tmp_path / "Ext" / "A\\B.bsl"
    weird.write_text("Процедура П() Экспорт\nКонецПроцедуры\n", encoding="utf-8")
    helpers, _ = make_helpers(str(tmp_path))

    assert helpers["glob_files"]("**/*.bsl") == ["Ext/A\\B.bsl"]
    assert [m["file"] for m in helpers["grep"]("Процедура")] == ["Ext/A\\B.bsl"]


def test_disk_cache_hash_matches_between_glob_and_saved_rows(tmp_path):
    """Пре-существующий дефект: `paths_hash` считался от РАЗНЫХ представлений.

    `_build_main_rows_from_glob` кормил `load_index(bsl_paths=...)` путями от
    `glob_files`, а `save_index` хэшировал `info.relative_path` (POSIX). На Windows
    хэши не совпадали НИКОГДА — дисковый кеш живого каталога не попадал ни разу и
    перезаписывался каждый старт; на Linux `os.sep == "/"`, и CI этого не видел.
    """
    from rlm_tools_bsl.cache import _paths_hash
    from rlm_tools_bsl.format_detector import parse_bsl_path

    _tree_with_nested_module(tmp_path)
    helpers, _ = make_helpers(str(tmp_path))

    globbed = helpers["glob_files"]("**/*.bsl")
    saved = [parse_bsl_path(p, str(tmp_path)).relative_path for p in globbed]
    assert _paths_hash(globbed) == _paths_hash(saved), (
        f"хэши расходятся: glob={globbed} vs saved={saved} — дисковый кеш не попадёт"
    )


def test_disk_cache_round_trip_hits_on_the_second_start(tmp_path, monkeypatch):
    """Полный круг: записанный кеш ПОПАДАЕТ по тем же путям, которыми его ищут."""
    from rlm_tools_bsl.cache import load_index, save_index
    from rlm_tools_bsl.format_detector import parse_bsl_path

    # `_cache_base()` уважает RLM_CONFIG_FILE — уводим кеш в tmp, чтобы не
    # трогать пользовательский каталог.
    monkeypatch.setenv("RLM_CONFIG_FILE", str(tmp_path / "cfg" / "config.json"))
    _tree_with_nested_module(tmp_path)
    helpers, _ = make_helpers(str(tmp_path))

    globbed = helpers["glob_files"]("**/*.bsl")
    rows = [(info.relative_path, info) for info in (parse_bsl_path(p, str(tmp_path)) for p in globbed)]
    save_index(str(tmp_path), len(globbed), rows)

    loaded = load_index(str(tmp_path), len(globbed), bsl_paths=globbed)
    assert loaded is not None, "дисковый кеш промахнулся по paths_hash — это и был дефект"
    assert [p for p, _ in loaded] == [p for p, _ in rows]


def test_cache_version_bumped_for_the_now_reachable_disk_cache():
    """Кеш начал попадать → стал достижим файл, записанный ПРЕЖНИМ парсером.

    В файле лежит сама классификация (`c/o/m/f/cmd/fe`), а сверяются только версия,
    счётчик и хэш путей — поэтому бамп обязателен, и его цена ровно один промах.
    """
    from rlm_tools_bsl.cache import CACHE_VERSION

    assert CACHE_VERSION >= 2


@pytest.mark.skipif(os.name != "nt", reason="needle с `\\` — проблема только Windows-агента")
def test_find_files_accepts_a_backslash_needle_on_windows(tmp_path):
    """FS-ветка стала POSIX; индексная искала по POSIX `rel_path` и `\\` не матчила НИКОГДА."""
    _tree_with_nested_module(tmp_path)
    helpers, _ = make_helpers(str(tmp_path))

    assert helpers["find_files"]("Заказ\\Ext") == helpers["find_files"]("Заказ/Ext")
    assert helpers["find_files"]("Заказ\\Ext") == ["Documents/Заказ/Ext/ObjectModule.bsl"]


@pytest.mark.skipif(os.name != "nt", reason="needle с `\\` — проблема только Windows-агента")
def test_find_files_backslash_needle_also_hits_the_indexed_branch(tmp_path, monkeypatch):
    """Нормализация стоит ДО индексной ветки, а не только в FS-fallback.

    Иначе `\\`-needle просто уходил бы в обход дерева там, где индекс ответил бы
    сразу, — то есть «работает» ценой молчаливой потери быстрого пути.
    """
    from rlm_tools_bsl.bsl_index import IndexBuilder, IndexReader

    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
    _tree_with_nested_module(tmp_path)
    (tmp_path / "Configuration.xml").write_text("<Configuration/>", encoding="utf-8")
    db_path = IndexBuilder().build(str(tmp_path), build_calls=False, build_metadata=True)
    reader = IndexReader(db_path)
    try:
        helpers, _ = make_helpers(str(tmp_path), idx_reader=reader)
        by_slash = helpers["find_files"]("Заказ/Ext")
        assert by_slash, "предусловие: индексная ветка обязана находить POSIX-needle"
        assert helpers["find_files"]("Заказ\\Ext") == by_slash
        assert all("\\" not in f for f in by_slash), by_slash
    finally:
        reader.close()


def test_both_copies_of_the_generic_signatures_declare_the_separator():
    """Контракт объявляется в ДВУХ независимых копиях — tripwire на расхождение.

    `server.available_functions` уходит агенту на старте и лежит в бюджете;
    `sandbox._GENERIC_HELPER_SIGNATURES` питает kwarg-хинты ошибок и в payload не
    входит. Копии уже разъезжались в формулировках — здесь закрыт именно шов.
    """
    from rlm_tools_bsl.sandbox import _GENERIC_HELPER_SIGNATURES

    marker = "# пути через /"
    publishing = ("grep", "grep_summary", "grep_read", "glob_files", "find_files")
    for name in publishing:
        assert marker in _GENERIC_HELPER_SIGNATURES[name]["sig"], f"sandbox-копия {name} не объявляет разделитель"
    # `tree` отдаёт ТЕКСТ дерева, а не список путей: фраза там вводила бы в заблуждение.
    assert marker not in _GENERIC_HELPER_SIGNATURES["tree"]["sig"]

    # ...и та же граница объявлена в доке, где живёт подробность (в sig она намеренно
    # сжата до 16 символов — это бюджетируемый текст).
    import pathlib

    doc = pathlib.Path(__file__).resolve().parents[1].joinpath("docs", "HELPERS.md").read_text(encoding="utf-8")
    contract = doc.split("## Стандартные (из rlm-tools)", 1)[1].split("\n## ", 1)[0]
    for phrase in ("через `/`", "POSIX", "tree"):
        assert phrase in contract, f"docs/HELPERS.md: раздел generic-хелперов не называет {phrase!r}"

    import inspect

    import rlm_tools_bsl.server as server

    source = inspect.getsource(server)
    for name in publishing:
        line = next(ln for ln in source.splitlines() if ln.strip().startswith(f'"{name}(') and "->" in ln)
        assert marker in line, f"server-копия {name} не объявляет разделитель: {line.strip()}"


# ---------------------------------------------------------------------------
# Задача 8. get_subsystems_for_object — детерминизм и согласованные поля
# ---------------------------------------------------------------------------


def _subsystem_content_reader(tmp_path, rows):
    """Ридер поверх рукописной `subsystem_content` — состав задаётся построчно."""
    import sqlite3

    from rlm_tools_bsl.bsl_index import IndexBuilder, IndexReader

    src = tmp_path / "cfg"
    (src / "CommonModules" / "Пустой" / "Ext").mkdir(parents=True)
    (src / "CommonModules" / "Пустой" / "Ext" / "Module.bsl").write_text(
        "Процедура П() Экспорт\nКонецПроцедуры\n", encoding="utf-8"
    )
    (src / "Configuration.xml").write_text("<Configuration/>", encoding="utf-8")
    db_path = IndexBuilder().build(str(src), build_calls=False, build_metadata=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("DELETE FROM subsystem_content")
    conn.executemany(
        "INSERT INTO subsystem_content (subsystem_name, subsystem_synonym, object_ref, file) VALUES (?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()
    return IndexReader(str(db_path))


def test_file_and_synonym_come_from_one_and_the_same_row(tmp_path, monkeypatch):
    """Прежде группировка перезаписывала `file` И `synonym` ПОСЛЕДНЕЙ строкой при
    `SELECT` без `ORDER BY` — выходила комбинация, которой нет ни в одном исходнике."""
    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
    reader = _subsystem_content_reader(
        tmp_path,
        [
            ("Продажи", "Синоним B", "Document.Заказ", "Subsystems/Продажи/Ext/Subsystem.xml"),
            ("Продажи", "Синоним A", "Document.Заказ", "Subsystems/Продажи.xml"),
        ],
    )
    try:
        found = reader.get_subsystems_for_object("Заказ")
        assert len(found) == 1, found
        row = found[0]
        # Победитель — первый по нормализованному пути, и синоним обязан быть ЕГО.
        assert row["file"] == "Subsystems/Продажи.xml", row
        assert row["synonym"] == "Синоним A", row
        # Полный состав файлов — новый аддитивный ключ.
        assert row["files"] == [
            "Subsystems/Продажи.xml",
            "Subsystems/Продажи/Ext/Subsystem.xml",
        ], row
    finally:
        reader.close()


def test_matched_refs_keep_multiplicity_and_gain_a_stable_order(tmp_path, monkeypatch):
    """Кратность СОХРАНЯЕТСЯ: один и тот же ref из двух XML — это две записи.

    Дедуп сменил бы кратность у внешнего потребителя, который их считает; добавлен
    только детерминированный порядок.
    """
    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
    rows = [
        ("Продажи", "С", "Document.Заказ", "Subsystems/Продажи/Ext/Subsystem.xml"),
        ("Продажи", "С", "Document.Заказ", "Subsystems/Продажи.xml"),
        ("Продажи", "С", "Catalog.Заказчики", "Subsystems/Продажи.xml"),
    ]
    reader = _subsystem_content_reader(tmp_path, rows)
    try:
        first = reader.get_subsystems_for_object("Заказ")[0]["matched_refs"]
        second = reader.get_subsystems_for_object("Заказ")[0]["matched_refs"]
        assert first.count("Document.Заказ") == 2, first
        assert sorted(first) == sorted(["Document.Заказ", "Document.Заказ", "Catalog.Заказчики"])
        assert first == second, (first, second)
    finally:
        reader.close()


def test_group_order_is_stable(tmp_path, monkeypatch):
    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
    reader = _subsystem_content_reader(
        tmp_path,
        [
            ("Финансы", "Ф", "Document.Заказ", "Subsystems/Финансы.xml"),
            ("Продажи", "П", "Document.Заказ", "Subsystems/Продажи.xml"),
            ("Закупки", "З", "Document.Заказ", "Subsystems/Закупки.xml"),
        ],
    )
    try:
        names = [r["name"] for r in reader.get_subsystems_for_object("Заказ")]
        assert names == sorted(names, key=str.lower), names
        assert names == [r["name"] for r in reader.get_subsystems_for_object("Заказ")]
    finally:
        reader.close()


def test_transient_and_non_transient_operational_errors_are_told_apart(tmp_path, monkeypatch):
    """Широкий `except sqlite3.OperationalError → None` глушил и то, что
    `@_transient_safe` обязан пере-бросить."""
    import sqlite3

    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
    reader = _subsystem_content_reader(tmp_path, [("Продажи", "П", "Document.Заказ", "Subsystems/Продажи.xml")])

    class _Raising:
        def __init__(self, msg):
            self.msg = msg

        def execute(self, *a, **kw):
            raise sqlite3.OperationalError(self.msg)

    try:
        real_conn = reader._conn

        # Транзиентное «таблицы нет» по-прежнему даёт None — контракт докстринга цел.
        reader._conn = _Raising("no such table: subsystem_content")
        assert reader.get_subsystems_for_object("Заказ") is None

        # Не-транзиентное больше НЕ выдаётся за «таблицы нет».
        reader._conn = _Raising("no such column: subsystem_synonym")
        with pytest.raises(sqlite3.OperationalError):
            reader.get_subsystems_for_object("Заказ")

        reader._conn = real_conn
    finally:
        reader.close()


# ---------------------------------------------------------------------------
# Задача 7. form_name у общих форм + приватный raw-matcher
# ---------------------------------------------------------------------------
#
# В пути общей формы (`CommonForms/X/Ext/Form/Module.bsl`) нет сегмента `Forms`, а
# классификатор ищет именно его — поэтому `find_by_type('CommonForms')` отдавал
# `form_name=None` ВСЕГДА, хотя `parse_form` то же имя заполняет уже сегодня.
# Чинить в `parse_bsl_path` нельзя: там `is_form_module = form_name is not None`,
# то есть изменилась бы колонка `modules.is_form` — ВЫХОД сборщика.


_COMMON_FORM_MODULE = "Процедура Обработать(Парам) Экспорт\nКонецПроцедуры\n"
_CALLER_MODULE = "Процедура Вызвать()\n    Обработать(1);\nКонецПроцедуры\n"


def _common_form_tree(tmp_path):
    form = tmp_path / "CommonForms" / "ОбщаяФорма" / "Ext" / "Form"
    form.mkdir(parents=True)
    (form / "Module.bsl").write_text(_COMMON_FORM_MODULE, encoding="utf-8")
    caller = tmp_path / "CommonModules" / "Вызывающий" / "Ext"
    caller.mkdir(parents=True)
    (caller / "Module.bsl").write_text(_CALLER_MODULE, encoding="utf-8")
    (tmp_path / "Configuration.xml").write_text("<Configuration/>", encoding="utf-8")
    return str(tmp_path)


def _bsl_no_index(base):
    from rlm_tools_bsl.bsl_helpers import make_bsl_helpers
    from rlm_tools_bsl.format_detector import detect_format
    from rlm_tools_bsl.helpers import make_helpers

    helpers, resolve_safe = make_helpers(base)
    return make_bsl_helpers(
        base_path=base,
        resolve_safe=resolve_safe,
        read_file_fn=helpers["read_file"],
        grep_fn=helpers["grep"],
        glob_files_fn=helpers["glob_files"],
        format_info=detect_format(base),
        idx_reader=None,
    )


def test_common_form_gets_its_form_name_in_every_agent_facing_output(tmp_path):
    """Четыре выдачи, а не три: `_build_module_entries` питает ДВА хелпера."""
    base = _common_form_tree(tmp_path)
    bsl = _bsl_no_index(base)

    by_type = bsl["find_by_type"]("CommonForms")
    assert by_type and all(r["form_name"] == "ОбщаяФорма" for r in by_type), by_type

    mods = bsl["find_module"]("ОбщаяФорма")
    assert mods and all(r["form_name"] == "ОбщаяФорма" for r in mods), mods

    obj = bsl["analyze_object"]("ОбщаяФорма")
    assert obj["modules"], obj
    assert all(m.get("form_name") == "ОбщаяФорма" for m in obj["modules"]), obj["modules"]

    got = bsl["get_object_modules"]("ОбщаяФорма")
    assert got["modules"] and all(m["form_name"] == "ОбщаяФорма" for m in got["modules"]), got


def test_common_form_form_name_in_the_profile_modules_section(tmp_path, monkeypatch):
    """Граница проекции у двух потребителей `_build_module_entries`.

    Функция одна на двоих, но секция профиля проецирует ФИКСИРОВАННЫЙ набор
    ключей и `form_name` не отдаёт вовсе, а `get_object_modules` — отдаёт.
    """
    from rlm_tools_bsl.bsl_helpers import make_bsl_helpers
    from rlm_tools_bsl.bsl_index import IndexBuilder, IndexReader
    from rlm_tools_bsl.format_detector import detect_format
    from rlm_tools_bsl.helpers import make_helpers

    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
    base = _common_form_tree(tmp_path)
    db_path = IndexBuilder().build(base, build_calls=False, build_metadata=True)
    reader = IndexReader(db_path)
    try:
        helpers, resolve_safe = make_helpers(base, idx_reader=reader)
        bsl = make_bsl_helpers(
            base_path=base,
            resolve_safe=resolve_safe,
            read_file_fn=helpers["read_file"],
            grep_fn=helpers["grep"],
            glob_files_fn=helpers["glob_files"],
            format_info=detect_format(base),
            idx_reader=reader,
        )
        prof = bsl["get_object_profile"]("ОбщаяФорма", sections=["modules"])
        items = (prof.get("sections", {}).get("modules") or {}).get("items") or []
        assert items, prof
        # Секция профиля строится из тех же `_build_module_entries`, но проецирует
        # ФИКСИРОВАННЫЙ набор ключей, в который `form_name` не входит вовсе, —
        # значит аgent-facing выдача здесь НЕ меняется. Закрепляем именно это:
        # иначе следующая правка добавит ключ в секцию молча, без бюджета и без CHANGELOG.
        assert all("form_name" not in m for m in items), items
        # А вот `get_object_modules`, использующий ТЕ ЖЕ entries, ключ публикует —
        # и на индексном маршруте тоже.
        got = bsl["get_object_modules"]("ОбщаяФорма")
        assert got["modules"] and all(m["form_name"] == "ОбщаяФорма" for m in got["modules"]), got
    finally:
        reader.close()


def test_find_custom_modifications_projects_form_name_too(tmp_path):
    """Ассерт СТРОГИЙ и с предусловием: условная проверка, допускающая None, прошла
    бы и БЕЗ проекции, и на пустом списке — то есть не доказывала бы ничего."""
    base = _common_form_tree(tmp_path)
    bsl = _bsl_no_index(base)
    res = bsl["find_custom_modifications"]("ОбщаяФорма", custom_prefixes=["Обработать"])
    mods = res["modifications"]
    assert mods, f"фикстура обязана дать доработку, иначе проверка вакуумна: {res}"
    for row in mods:
        assert row["form_name"] == "ОбщаяФорма", row


def test_exported_common_form_procedure_is_still_found_from_another_module(tmp_path):
    """РЕГРЕСС, который проекция могла внести молча.

    `find_callers_context` читает `form_name` как УПРАВЛЯЮЩЕЕ условие: непустое
    значение сужает поиск до ОДНОГО файла. С публичной проекцией экспортная
    процедура общей формы перестала бы находиться внешними вызывающими — и в
    выдаче это выглядело бы как честный ноль.
    """
    base = _common_form_tree(tmp_path)
    bsl = _bsl_no_index(base)
    res = bsl["find_callers_context"]("Обработать", "ОбщаяФорма")
    files = {c["file"] for c in res["callers"]}
    assert any("Вызывающий" in f for f in files), res


def test_raw_matcher_stays_raw_and_the_public_one_projects(tmp_path):
    """Проекция идёт на КОПИЯХ: сырые строки каталога не мутируются."""
    from rlm_tools_bsl.format_detector import parse_bsl_path

    base = _common_form_tree(tmp_path)
    info = parse_bsl_path(str(tmp_path / "CommonForms" / "ОбщаяФорма" / "Ext" / "Form" / "Module.bsl"), base)
    assert info.form_name is None, "сырая классификация не меняется — это вход сборщика"
    assert info.is_form_module is False, "modules.is_form — ВЫХОД сборщика, его трогать нельзя"

    bsl = _bsl_no_index(base)
    assert bsl["find_module"]("ОбщаяФорма")[0]["form_name"] == "ОбщаяФорма"
    # Повторный вызов отдаёт то же — значит первая проекция не записалась в каталог.
    assert bsl["find_module"]("ОбщаяФорма")[0]["form_name"] == "ОбщаяФорма"


def test_find_module_sig_declares_form_name():
    """Заполнить ключ и не назвать его — ровно «ключ, которого никто не читает».

    `find_by_type` наследует подпись через `-> same`, и правка ЕЁ подписи сделала бы
    `-> same` неправдой, поэтому ключ объявляется ТОЛЬКО у `find_module`.
    """
    from rlm_tools_bsl.bsl_helpers import build_helper_metadata_snapshot

    snap = build_helper_metadata_snapshot()
    assert "form_name" in snap["find_module"]["sig"], snap["find_module"]["sig"]
    assert "owner" in snap["find_module"]["sig"], snap["find_module"]["sig"]
    assert "-> same" in snap["find_by_type"]["sig"], snap["find_by_type"]["sig"]


# ---------------------------------------------------------------------------
# Задача 6.1. Пары «одно понятие — два множества»
# ---------------------------------------------------------------------------


def test_via_is_unconditional_in_both_direct_buckets(tmp_path):
    """`via` — БЕЗУСЛОВНЫЙ ключ каждой строки обеих ПРЯМЫХ корзин.

    Правило «отсутствие поля означает direct» жило только в докстринге, то есть
    было ключом, которого никто не читает: агент, встретивший строку без `via`,
    не отличал «прямой обход» от «поле забыли».
    """
    base = tmp_path / "cfg"
    mgr = base / "Documents" / "Источник" / "Ext"
    mgr.mkdir(parents=True)
    (mgr / "ManagerModule.bsl").write_text(
        "Процедура ДобавитьКомандыСозданияНаОсновании(Команды) Экспорт\n"
        '    Команды.Добавить("Документ.Цель");\n'
        "КонецПроцедуры\n",
        encoding="utf-8",
    )
    (mgr / "ObjectModule.bsl").write_text(
        "Процедура ОбработкаЗаполнения(Основание, Данные)\n"
        '    Если ТипЗнч(Основание) = Тип("ДокументСсылка.Прочий") Тогда\n'
        "    КонецЕсли;\n"
        "КонецПроцедуры\n",
        encoding="utf-8",
    )
    (base / "Configuration.xml").write_text("<Configuration/>", encoding="utf-8")

    bsl = _bsl_no_index(str(base))
    res = bsl["find_based_on_documents"]("Источник")
    rows = (res["can_create_from_here"] or []) + (res["can_be_created_from"] or [])
    assert rows, res
    for row in rows:
        assert "via" in row, row
        assert row["via"] in ("direct", "metadata", "back_scan"), row
    # У ПРЯМЫХ строк значение именно 'direct' — не пустое и не отсутствующее.
    assert any(r["via"] == "direct" for r in rows), rows


def test_functional_options_meta_is_unconditional_and_names_three_sources(tmp_path, monkeypatch):
    """Источник ответа СМЕШАННЫЙ по построению: XML из SQLite, code — из живого дерева.

    Один `source="index"|"live"` был бы ложью, а `"mixed"` не объясняет, почему
    профиль вернул typed-опции, а direct — ещё и code-строки.
    """
    from rlm_tools_bsl.bsl_helpers import make_bsl_helpers
    from rlm_tools_bsl.bsl_index import IndexBuilder, IndexReader
    from rlm_tools_bsl.format_detector import detect_format
    from rlm_tools_bsl.helpers import make_helpers

    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
    base = tmp_path / "cfg"
    fo = base / "FunctionalOptions" / "ТестОпция" / "Ext"
    fo.mkdir(parents=True)
    (base / "FunctionalOptions" / "ТестОпция.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">\n'
        '  <FunctionalOption uuid="1"><Properties><Name>ТестОпция</Name>\n'
        '  <Content><xr:Item xmlns:xr="http://v8.1c.ru/8.3/xcf/readable">'
        "<xr:Field>Document.Док</xr:Field></xr:Item></Content>\n"
        "  </Properties></FunctionalOption>\n"
        "</MetaDataObject>\n",
        encoding="utf-8",
    )
    doc = base / "Documents" / "Док" / "Ext"
    doc.mkdir(parents=True)
    (doc / "ObjectModule.bsl").write_text(
        'Процедура П() Экспорт\n    Х = ПолучитьФункциональнуюОпцию("ТестОпция");\nКонецПроцедуры\n',
        encoding="utf-8",
    )
    (base / "Configuration.xml").write_text("<Configuration/>", encoding="utf-8")

    db_path = IndexBuilder().build(str(base), build_calls=False, build_metadata=True)
    reader = IndexReader(db_path)
    try:
        helpers, resolve_safe = make_helpers(str(base), idx_reader=reader)
        bsl = make_bsl_helpers(
            base_path=str(base),
            resolve_safe=resolve_safe,
            read_file_fn=helpers["read_file"],
            grep_fn=helpers["grep"],
            glob_files_fn=helpers["glob_files"],
            format_info=detect_format(str(base)),
            idx_reader=reader,
        )
        res = bsl["find_functional_options"]("Док")
        meta = res["_meta"]
        assert meta["xml_source"] in ("index", "live"), meta
        assert meta["code_source"] == "live", meta
        # `source` ОБЯЗАТЕЛЕН: `test_coverage_map_matches_reality` читает его
        # через `res.get("source") or res["_meta"]["source"]`.
        assert meta["source"] in ("index", "live", "index+live"), meta
        if meta["xml_source"] == "index":
            assert meta["source"] == "index+live", meta

        # И на пагинированной ветке форма та же.
        paged = bsl["find_functional_options"]("Док", limit=1)
        assert set(paged["_meta"]) == set(meta), (sorted(paged["_meta"]), sorted(meta))
    finally:
        reader.close()


def test_four_new_pairs_are_synced_in_all_three_points():
    """ТРИ точки синхронизации, а не две: структурный список, full-блок и счётчик.

    Имя пары — ВСЕГДА два ХЕЛПЕРА: парсер tripwire берёт первый ASCII-идентификатор
    слева и справа от ' vs ', а фильтр `rlm_help(helpers=[...])` ищет по именам
    хелперов — записать пару через `ref_kind` значило бы дать зелёный тест ценой
    поломки конвенции.
    """
    from rlm_tools_bsl.bsl_helpers import build_helper_metadata_snapshot
    from rlm_tools_bsl.bsl_strategy_data import DISAMBIGUATION_PAIRS

    new_pairs = {
        ("find_references_to_object", "find_based_on_documents"),
        ("find_references_to_object", "find_functional_options"),
        ("get_object_profile", "find_functional_options"),
        ("find_references_to_object", "find_event_subscriptions"),
    }
    present = {tuple(e["pair"]) for e in DISAMBIGUATION_PAIRS}
    assert new_pairs <= present, sorted(new_pairs - present)
    assert len(DISAMBIGUATION_PAIRS) == 16

    # Оба элемента КАЖДОЙ пары обязаны быть именами реальных хелперов, иначе
    # `rlm_help(helpers=[...], section='disambiguation')` перестанет её находить.
    snap = build_helper_metadata_snapshot()
    for entry in DISAMBIGUATION_PAIRS:
        for name in entry["pair"]:
            assert name in snap, f"{name} — не хелпер, пара не найдётся фильтром rlm_help"


def test_the_merged_based_on_block_is_not_duplicated():
    """Старый блок `find_based_on_documents — прямой обход + back_scan` СЛИТ с новой
    парой, а не оставлен рядом: одно понятие описывается ОДИН раз."""
    from rlm_tools_bsl.bsl_knowledge import _STRATEGY_HEADER

    block = _STRATEGY_HEADER.split("== DISAMBIGUATION ==", 1)[1].split("\n== ", 1)[0]
    assert "find_based_on_documents(doc_name) — прямой обход + back_scan" not in block
    assert "find_references_to_object → based_on vs find_based_on_documents:" in block


def test_disambiguation_block_did_not_grow_past_the_lever():
    """Рычаг ОБЯЗАН был освободить место ДО добавления пар.

    Задача — единственная, которая пробивает гард текста стратегии: её прибавка не
    помещается ни в одну full-ячейку без предварительного сжатия. Замер до релиза:
    9 639 символов на 17 заголовков.
    """
    from rlm_tools_bsl.bsl_knowledge import _STRATEGY_HEADER

    block = "== DISAMBIGUATION ==" + _STRATEGY_HEADER.split("== DISAMBIGUATION ==", 1)[1].split("\n== ", 1)[0]
    assert len(block) <= 9639, f"блок вырос до {len(block)} при 9639 до релиза — рычаг не сработал"


# ---------------------------------------------------------------------------
# Задача 6.3. Три сигнала свежести — МАТРИЦА, а не иерархия
# ---------------------------------------------------------------------------


def test_freshness_matrix_is_declared_in_both_copies():
    """Ни один сигнал не главнее других — и раньше об этом не говорил НИ ОДИН
    agent-facing текст: `index_status` не был упомянут в них ни разу.

    Три точки: секция `coverage` (slim, через rlm_help), встроенная выжимка
    `== COVERAGE (кратко) ==` (full) и docs/HELPERS.md.
    """
    from rlm_tools_bsl.bsl_knowledge import _STRATEGY_HEADER
    from rlm_tools_bsl.bsl_strategy_data import STRATEGY_SECTIONS

    section = STRATEGY_SECTIONS["coverage"]
    brief = _STRATEGY_HEADER.split("== COVERAGE (кратко) ==", 1)[1].split("\n== ", 1)[0]

    for text, where in ((section, "STRATEGY_SECTIONS['coverage']"), (brief, "== COVERAGE (кратко) ==")):
        for marker in ("index_status", "stale_content", "stale_age", "index update"):
            assert marker in text, f"{where}: нет {marker!r}"
        # `ok` полноты НЕ доказывает — самое частое ложное прочтение.
        assert "НЕ доказывает" in text or "не доказывает" in text.casefold(), where
        # `disabled` — основание ПЕРЕСОБРАТЬ, а не признак устаревания.
        assert "disabled" in text, where


def test_freshness_matrix_is_in_the_docs_too():
    import pathlib

    doc = pathlib.Path(__file__).resolve().parents[1].joinpath("docs", "HELPERS.md").read_text(encoding="utf-8")
    for marker in ("index_status", "stale_content", "stale_age", "МАТРИЦУ, а не иерархию"):
        assert marker in doc, marker


def test_coverage_section_has_a_sync_row_in_module_map():
    """Таблица синхронизации строки для `coverage` НЕ содержала, хотя ключ в
    `STRATEGY_SECTIONS` есть и релиз правит ОБЕ копии."""
    import pathlib

    doc = pathlib.Path(__file__).resolve().parents[1].joinpath("docs", "MODULE_MAP.md").read_text(encoding="utf-8")
    assert "== COVERAGE (кратко) ==" in doc
    assert 'STRATEGY_SECTIONS["coverage"]' in doc


def test_module_map_pair_count_matches_the_data():
    """Снимок «Пары DISAMBIGUATION» в доке застолблён тестом — обновляем оба."""
    import pathlib

    from rlm_tools_bsl.bsl_strategy_data import DISAMBIGUATION_PAIRS

    doc = pathlib.Path(__file__).resolve().parents[1].joinpath("docs", "MODULE_MAP.md").read_text(encoding="utf-8")
    assert f"| Пары DISAMBIGUATION | **{len(DISAMBIGUATION_PAIRS)}** |" in doc


def test_coverage_section_is_not_inlined_into_slim(monkeypatch):
    """Бюджет: секция `coverage` в slim не инлайнится (там только имя секции),
    поэтому пункт 9 базовой slim-ячейке бесплатен — платит только зеркало в full."""
    import os
    import tempfile

    from rlm_tools_bsl.bsl_helpers import build_helper_metadata_snapshot
    from rlm_tools_bsl.bsl_knowledge import get_strategy
    from rlm_tools_bsl.format_detector import detect_format

    monkeypatch.setenv("RLM_STRATEGY_MODE", "slim")
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "Configuration.xml"), "w") as f:
            f.write("<Configuration/>")
        fmt = detect_format(d)
    text = get_strategy("high", fmt, registry=build_helper_metadata_snapshot(), query="")
    assert "stale_content" not in text, "секция coverage начала инлайниться в slim — это бюджет"


# ---------------------------------------------------------------------------
# Ревью codex (P2). Объектная форма module_hint — НЕ маршрут точного перехода
# ---------------------------------------------------------------------------
#
# ТОЧНАЯ форма hint одна — `rel_path`. `'Документ.X'`/`'Document.X'` и голое имя
# объекта задают лишь ОБЪЕКТ, а модулей у объекта несколько: обработчик бывает
# объявлен и в `ObjectModule`, и в модуле формы, а индексная MAIN-сессия домешивает
# объявления соседних CFE. Тогда `total>1` и `_meta.unique=False`.
#
# Задача 5 объявила этот контракт СЛОВАМИ, но ни одного гарда под него не заводила, и
# ГОТОВЫЕ примеры продолжали подавать объектную форму как основной маршрут: за
# `find_definition('ОбработкаПроведения', 'Документ.X')` шла строка
# `read_procedure(d['definitions'][0]['file'], …)`, то есть чтение тела ЧУЖОГО модуля.
# Гардов три: поведение (оно первично), готовые примеры реестра, тексты доков.
_COMMON_MODULE_PREFIXES = ("ОбщийМодуль.", "CommonModule.")


def _object_qualified_hint(literal):
    """Литерал вида `Категория.Объект`.

    Общий модуль исключён сознательно: у него модуль РОВНО один, поэтому и голое
    имя, и `ОбщийМодуль.X` там точны по построению (см. П4 плана).
    """
    return "." in literal and not literal.startswith(_COMMON_MODULE_PREFIXES)


def _runnable_lines(text):
    """Строки, которые агент КОПИРУЕТ и исполняет, а не читает как пояснение."""
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if line and not line.startswith("#"):
            yield line


def test_no_ready_example_passes_an_object_qualified_hint():
    """Ни один готовый пример реестра не подаёт объектную форму hint как точную."""
    import re

    from rlm_tools_bsl.bsl_helpers import build_helper_metadata_snapshot
    from rlm_tools_bsl.bsl_knowledge import _BUSINESS_RECIPES

    by_keyword = re.compile(r"(?:module_hint|from_hint|to_hint)=\s*'([^']*)'")
    positional = re.compile(r"find_definition\(\s*'[^']*'\s*,\s*'([^']*)'")
    placeholder = re.compile(r"(?:module_hint|from_hint|to_hint)=\s*<")

    surfaces = []
    for name, meta in build_helper_metadata_snapshot().items():
        surfaces += [(f"{name}.{field}", meta.get(field)) for field in ("sig", "recipe")]
    # Домены `_BUSINESS_RECIPES` — ТАКАЯ ЖЕ agent-facing поверхность: `compact` инлайнится
    # в slim, `full` в full, `code_hint` уезжает готовым кодом. Первый круг правки их не
    # смотрел — и маршрут домена «достижимость» остался с объектной формой hint.
    for domain, recipe in _BUSINESS_RECIPES.items():
        for form, value in recipe.items():
            parts = value if isinstance(value, (list, tuple)) else [value]
            surfaces.append((f"_BUSINESS_RECIPES[{domain}].{form}", "\n".join(parts)))

    offenders = []
    for where, text in surfaces:
        for line in _runnable_lines(text):
            for match in (*by_keyword.finditer(line), *positional.finditer(line)):
                if _object_qualified_hint(match.group(1)):
                    offenders.append(f"{where}: {line}")
            # Плейсхолдер `<rel_path>` семантику исправляет, но МАРШРУТА не даёт и
            # Python'ом не является: план и CHANGELOG обещают исполнимую форму
            # `find_module('X')[i]['path']` (или присваивание `om = …` выше вызова).
            for match in placeholder.finditer(line):
                offenders.append(f"{where} (плейсхолдер вместо маршрута): {line}")
    assert not offenders, (
        "объектная форма hint в ГОТОВОМ примере: она сужает лишь до ОБЪЕКТА, а агент берёт "
        "definitions[0] и читает ЧУЖОЙ модуль. Точную форму бери из find_module(...)['path']: " + "; ".join(offenders)
    )


def test_hint_form_contract_is_spelled_out_where_the_hint_is_offered():
    """Рецепт, предлагающий hint, обязан назвать точную форму И условие объектной."""
    from rlm_tools_bsl.bsl_helpers import build_helper_metadata_snapshot

    snap = build_helper_metadata_snapshot()
    for helper in ("find_definition", "find_call_hierarchy"):
        text = (snap[helper].get("recipe") or "") + (snap[helper].get("sig") or "")
        assert "rel_path" in text, f"{helper}: точная форма не названа"
        assert "уникальн" in text, f"{helper}: не сказано условие уникальности имени внутри объекта"
        assert "ОБЪЕКТ" in text, f"{helper}: не сказано, что объектная форма задаёт лишь ОБЪЕКТ"


def test_docs_do_not_promise_that_the_object_form_pins_one_module():
    """Пользовательская дока и сценарий приёмки не обещают «ровно один» от объектной формы."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]

    helpers_doc = root.joinpath("docs", "HELPERS.md").read_text(encoding="utf-8")
    assert "чтобы запинить один" not in helpers_doc, (
        "вернулось обещание «запинить один» на все три формы hint: один модуль пинит только rel_path"
    )
    bullet = next(ln for ln in helpers_doc.splitlines() if ln.startswith("- `find_definition(name"))
    low = bullet.lower()
    assert "`rel_path` — точная форма" in low, bullet[:200]
    assert "только когда имя внутри объекта уникально" in low, bullet[:200]

    prompt = root.joinpath("docs", "full_analysis_prompt.md").read_text(encoding="utf-8").splitlines()
    start = next(i for i, ln in enumerate(prompt) if ln.strip().startswith("- Сужение module_hint:"))
    end = next(i for i in range(start + 1, len(prompt)) if prompt[i].strip().startswith("- "))
    scenario = "\n".join(prompt[start:end])
    assert "rel_path" in scenario, scenario
    assert "module_type='ObjectModule'" in scenario, scenario
    assert "НЕ дефект" in scenario, (
        "сценарий приёмки снова требует unique=True от объектной формы — агент отчитается о "
        "ложном дефекте на здоровой конфигурации"
    )


def test_docs_never_assign_an_object_level_hint_in_any_scenario():
    """ВСЕ доки, а не один блок: ни один сценарий не передаёт hint уровня ОБЪЕКТА.

    Первый круг правки покрыл раздел про `find_definition` и пропустил сценарии
    иерархии вызовов, перехватов CFE и достижимости — там hint так и оставался
    объектным, то есть приёмка шла по ПРОИЗВОЛЬНОМУ модулю объекта.
    """
    import pathlib
    import re

    docs = pathlib.Path(__file__).resolve().parents[1].joinpath("docs")
    assignment = re.compile(r"(?:module_hint|from_hint|to_hint)=([^,)\n]*)")
    offenders = []
    for doc in sorted(docs.glob("*.md")):
        for num, line in enumerate(doc.read_text(encoding="utf-8").splitlines(), 1):
            for match in assignment.finditer(line):
                value = match.group(1).strip()
                quoted = re.fullmatch(r"'([^']*)'", value)
                if quoted and _object_qualified_hint(quoted.group(1)):
                    offenders.append(f"{doc.name}:{num} (литерал): {line.strip()[:100]}")
                elif value.startswith("<"):
                    # Любой плейсхолдер целиком вместо значения: и `<этот объект>`
                    # (уровень ОБЪЕКТА), и `<rel_path>` (маршрута получения не даёт).
                    offenders.append(f"{doc.name}:{num} (плейсхолдер): {line.strip()[:100]}")
    assert not offenders, (
        "hint уровня ОБЪЕКТА в сценарии документа: модуль он не пинит, и проверка пойдёт по "
        "произвольному модулю объекта. " + "; ".join(offenders)
    )


def test_expected_results_table_conditions_the_object_form():
    """Таблица ожиданий не требует `unique=True` от объектной формы БЕЗ условия.

    Отдельным тестом, потому что противоречие было ВНУТРИ одного файла: сценарий уже
    называл `unique=False` нормой, а таблица ожиданий ниже по-прежнему требовала
    `total=1`/`unique=True` от `'Документ.X'`. Агент приёмки читает таблицу.
    """
    import pathlib
    import re

    doc = pathlib.Path(__file__).resolve().parents[1].joinpath("docs", "full_analysis_prompt.md")
    object_form = re.compile(r"find_definition\([^)]*,\s*'([^']*)'")
    bad = []
    for num, line in enumerate(doc.read_text(encoding="utf-8").splitlines(), 1):
        hits = [m.group(1) for m in object_form.finditer(line)]
        if any(_object_qualified_hint(h) for h in hits) and "unique=True" in line and "уникальн" not in line:
            bad.append(f"{num}: {line.strip()[:120]}")
    assert not bad, (
        "объектная форма и `unique=True` в одной строке без условия уникальности — приёмка "
        "отчитается о ложном дефекте на здоровой конфигурации: " + "; ".join(bad)
    )


# Проза и Python-docstring — ТРЕТЬЯ публичная поверхность того же контракта. Рецепты и
# сценарии круги 2–4 починили, а `docs/HELPERS.md` и `find_path.__doc__` продолжали
# утверждать, что все три формы hint «пинят одноимённые методы к модулю». Фактически
# `_resolve_target_key` точен ТОЛЬКО при `len(rel_paths) == 1`, а ambiguity guard `find_path`
# гаснет от ЛЮБОГО непустого hint на своём конце — воспроизведено зондом на документе,
# где обработчик объявлен и в `ObjectModule`, и в модуле формы:
#   без hint -> error + 2 кандидата; 'Документ.X' -> found=True, to_exact=False,
#   precision='heuristic'; голое имя объекта -> found=False; rel_path -> to_exact=True.
# Разные ответы на ОДИН вопрос, и отличить их можно только по error/to_exact/precision.
_PINS_ONE_MODULE_CLAIMS = (
    "пинят одноимённые методы к модулю",
    "пинит одноимённые методы к модулю",
    "pin a same-named method to one module",
    "чтобы запинить один",
)


def test_public_prose_never_claims_the_object_form_pins_one_module():
    """Запрещённое утверждение — в доках И в docstring'ах кода.

    `CHANGELOG.md` в область НЕ входит сознательно: его записи описывают УЖЕ выпущенные
    релизы (v1.19.0/v1.20.0), а журнал выпущенного не переписывается.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    surfaces = {p.name: p.read_text(encoding="utf-8") for p in sorted(root.joinpath("docs").glob("*.md"))}
    for module in ("bsl_helpers.py", "bsl_index.py", "bsl_knowledge.py", "bsl_strategy_data.py"):
        surfaces[module] = root.joinpath("src", "rlm_tools_bsl", module).read_text(encoding="utf-8")

    offenders = [
        f"{name}: «{claim}»" for name, text in surfaces.items() for claim in _PINS_ONE_MODULE_CLAIMS if claim in text
    ]
    assert not offenders, (
        "публичная поверхность снова обещает, что объектная форма hint пинит ОДИН модуль; "
        "точен только rel_path (_resolve_target_key требует ровно один rel_path): " + "; ".join(offenders)
    )


def test_public_prose_states_the_uniqueness_condition_for_every_hint_helper():
    """Условие точности обязано стоять РЯДОМ с перечислением форм.

    Проверка «слово есть в пункте» здесь бессмысленна: пункт `find_call_hierarchy` —
    3 килобайта, и «уникально» в нём есть про СОВСЕМ другое (экспортный метод общего
    модуля уникален во всей БД). Негативный контроль это показал дважды: сперва на
    проверке по всему пункту, потом на окне в 600 символов — оно дотягивалось до той же
    соседней фразы. Поэтому окно — ПЕРВОЕ ПРЕДЛОЖЕНИЕ после якоря, которым пункт вводит
    формы hint. Пропал якорь — тест падает, и это правильно: формулировку контракта
    нельзя менять молча. Точка внутри предложения (номер версии) окно сузит, то есть
    ошибётся в СТОРОНУ падения, а не молчания.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    helpers_doc = root.joinpath("docs", "HELPERS.md").read_text(encoding="utf-8").splitlines()
    anchors = {
        "- `find_definition(name": "сужай `module_hint`",
        "- `find_call_hierarchy(name": "Формы hint:",
        "- `find_path(from_name": "`from_hint`/`to_hint`:",
    }
    for prefix, anchor in anchors.items():
        bullet = next((ln for ln in helpers_doc if ln.startswith(prefix)), None)
        assert bullet is not None, f"пункт {prefix!r} исчез из docs/HELPERS.md"
        assert anchor in bullet, (
            f"{prefix}: исчез якорь {anchor!r} — контракт форм hint переформулирован, перечитайте его"
        )
        tail = bullet[bullet.index(anchor) :]
        stop = tail.find(". ")
        window = tail[: stop + 1] if stop > 0 else tail[:600]
        assert "уникальн" in window.lower(), f"{prefix}: рядом с формами не названо условие уникальности"
        assert "rel_path" in window, f"{prefix}: рядом с формами не названа точная форма"

    source = root.joinpath("src", "rlm_tools_bsl", "bsl_helpers.py").read_text(encoding="utf-8")
    start = source.index("from_hint / to_hint:")
    block = source[start : start + 1200]
    assert "unique inside it" in block, "docstring find_path не называет условие уникальности"
    assert "ambiguity guard" in block, (
        "docstring find_path не предупреждает, что непустой hint гасит ambiguity guard: "
        "неразрешающийся объектный hint отдаёт НЕдоказанный путь без error"
    )


# Пункт может СОГЛАСОВАННО объяснять формы в одном абзаце и тут же СОВЕТОВАТЬ «передай
# module_hint» в другом — и агент прочитает совет, а не оговорку. Так и было: пункт
# `find_call_hierarchy` в docs/HELPERS.md обещал «передай module_hint — он привязывает
# КОРЕНЬ к одному модулю» на 300 символов раньше, чем объяснял, что это верно ТОЛЬКО для
# rel_path. Два несовместимых контракта в ОДНОМ пункте — ровно то, против чего заявлен
# релиз. Гарды кругов 2–5 это пропускали: список запрещённых фраз такой формулировки не
# знал, а окно условия начиналось с ПОЗДНЕГО якоря «Формы hint:».
#
# Поэтому правило не про фразу, а про КЛАСС: у каждого совета передать hint в его
# собственном предложении обязана стоять конкретная форма. В доках предложения настоящие,
# и правило строгое; в исходниках рецепт — питоновские литералы с `\n`, поэтому граница
# предложения там шире и правило мягче (рецепты дополнительно закрыты правилами готовых
# примеров).
# Глаголы ОБОИХ языков: публичный Python-docstring написан по-английски, и гард круга 6
# (только русские «передай|передайте|дай») пропускал его целиком.
_HINT_ADVICE = r"(?:передай|передайте|дай|pass|provide|supply|add|give)[^.]{0,40}(?:module_hint|from_hint|to_hint)"
# `candidates` — тоже КОНКРЕТНЫЙ маршрут: у ambiguity guard `find_path` в кандидатах
# лежит ключ `file`, и это тот же rel_path.
_CONCRETE_ROUTE = ("find_module(", "rel_path", "['path']", "candidates")


def _claim_window(text, start, end):
    """Окно одного утверждения вокруг [start, end).

    Границы ДВЕ, и обе выведены из негативных контролей, а не из вкуса:

    * конец предложения — точка со ЛЮБЫМ пробельным символом (`\\.\\s`). Просто `. ` не
      годится: в docstring предложение часто кончается точкой и ПЕРЕВОДОМ строки;
    * ЛИТЕРАЛЬНЫЙ `\\n` внутри питоновского литерала — это конец строки РЕЦЕПТА, который
      увидит агент. Без него окно уезжало в соседние строки рецепта, где точная форма
      уже названа, и правило молчало на сломанном тексте.

    А вот НАСТОЯЩИЙ перевод строки границей НЕ является: в docstring предложение
    переносится по строкам, и строчное окно давало ложное срабатывание — обрезало
    `candidates` на следующей строке у верного совета `find_path`.
    """
    import re

    # Точка с запятой ограничивает окно ТОЛЬКО СПРАВА, и это выведено из контролей:
    # обещание не имеет права занимать уточнение из СЛЕДУЮЩЕЙ клаузы («module_hint
    # привязывает…; точная форма — rel_path…» — ровно тот случай), но опереться на
    # предыдущую клаузу может («…(rel_path always does); from_hint pins … the same
    # way»). Симметричная граница давала ЛОЖНЫЕ срабатывания на верном тексте.
    sentence_end = re.compile(r"\.\s")
    clause_end = re.compile(r"(?:\.\s|;)")
    lookback_from = max(0, start - 2000)
    left = 0
    for match in sentence_end.finditer(text, lookback_from, start):
        left = max(left, match.end())
    literal_nl = text.rfind("\\n", lookback_from, start)
    if literal_nl >= 0:
        left = max(left, literal_nl + 2)

    right = len(text)
    ahead = clause_end.search(text, end, end + 2000)
    if ahead is not None:
        right = min(right, ahead.start() + 1)
    literal_nl = text.find("\\n", end, end + 2000)
    if literal_nl >= 0:
        right = min(right, literal_nl)
    return text[left:right]


def test_every_advice_to_pass_a_hint_names_the_form():
    """Совет «передай module_hint» без названной формы — снова отправляет к объектной."""
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[1]
    surfaces = {p.name: p.read_text(encoding="utf-8") for p in sorted(root.joinpath("docs").glob("*.md"))}
    for module in ("bsl_helpers.py", "bsl_index.py", "bsl_knowledge.py", "bsl_strategy_data.py"):
        surfaces[module] = root.joinpath("src", "rlm_tools_bsl", module).read_text(encoding="utf-8")

    advice = re.compile(_HINT_ADVICE, re.IGNORECASE)
    offenders = []
    for name, text in surfaces.items():
        for match in advice.finditer(text):
            sentence = _claim_window(text, match.start(), match.end())
            if not any(marker in sentence for marker in _CONCRETE_ROUTE):
                offenders.append(f"{name}: …{sentence.strip()[:90]}…")
    assert not offenders, (
        "совет передать module_hint БЕЗ формы: агент подставит объектную, которая модуль не пинит. "
        "Назовите маршрут (find_module('X')[i]['path'] или уже полученный om['path']): " + "; ".join(offenders)
    )


# Третий класс утверждения, помимо «готового примера» и «совета передать»: ОБЕЩАНИЕ, у
# которого подлежащее — сам параметр. «module_hint привязывает корень к ОДНОМУ модулю →
# exact» — законченная фраза, и она ложна для двух форм из трёх. Такая формулировка жила в
# поле `when_a` пары DISAMBIGUATION (уходит агенту через rlm_help(section='disambiguation'))
# и в полном strategy-тексте; гарды кругов 6–7 её не видели — там нет ни готового вызова,
# ни глагола совета. Заодно этот гард нашёл место, которого не назвал никто: в docstring
# `find_path` осталось «to_hint enables the exact-mode root» без условия разрешимости.
_HINT_EXACTNESS_PROMISE = (
    r"(?:module_hint|from_hint|to_hint)[^.;" + chr(92) + "n]{0,40}"
    r"(?:привязыва|привяжет|пинит|пинят|binds|pins|включа" + chr(92) + r"w* exact|"
    r"→ exact|enables the exact|switches the root)"
)


def test_no_standalone_promise_that_a_hint_alone_gives_exactness():
    """Обещание точности обязано иметь подлежащим ТОЧНУЮ форму, а не любой hint."""
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[1]
    surfaces = {p.name: p.read_text(encoding="utf-8") for p in sorted(root.joinpath("docs").glob("*.md"))}
    for module in ("bsl_helpers.py", "bsl_index.py", "bsl_knowledge.py", "bsl_strategy_data.py"):
        surfaces[module] = root.joinpath("src", "rlm_tools_bsl", module).read_text(encoding="utf-8")

    promise = re.compile(_HINT_EXACTNESS_PROMISE, re.IGNORECASE)
    offenders = []
    for name, text in surfaces.items():
        for match in promise.finditer(text):
            window = _claim_window(text, match.start(), match.end())
            if not any(marker in window for marker in _CONCRETE_ROUTE):
                offenders.append(f"{name}: …{window.strip()[:90]}…")
    assert not offenders, (
        "обещание точности с подлежащим «любой hint»: у объектных форм exact включается ТОЛЬКО "
        "при уникальном внутри объекта имени. Сделайте подлежащим rel_path: " + "; ".join(offenders)
    )


# Ревью нашло разрыв ИНОГО рода, чем прежние восемь кругов: не ложное утверждение о
# контракте, а протухший ПЕРЕЧЕНЬ ключей. `get_overrides` отдаёт новый агрегат
# `unique_object_methods` и новый ключ строки `extension_file`, а список в его docstring
# остался прежним; у `find_references_to_object` перечень не знал про `_meta` вовсе, то
# есть и про `kinds_requested`/`kinds_applied`. Форму такого разрыва ловит только сверка
# ПЕРЕЧНЯ с живым вызовом — ни одно текстовое правило его не видит.
def _release_docstrings(*names):
    """Docstring'и вложенных функций `make_bsl_helpers` по именам (самый длинный)."""
    import ast
    import pathlib

    src = pathlib.Path(__file__).resolve().parents[1].joinpath("src", "rlm_tools_bsl", "bsl_helpers.py")
    tree = ast.parse(src.read_text(encoding="utf-8"))
    found: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in names:
            doc = ast.get_docstring(node) or ""
            if len(doc) > len(found.get(node.name, "")):
                found[node.name] = doc
    return found


def _shape_env(tmp_path, monkeypatch):
    """Дерево + индекс + одна строка перехвата: хватает и overrides, и references."""
    import sqlite3

    from rlm_tools_bsl.bsl_helpers import make_bsl_helpers
    from rlm_tools_bsl.bsl_index import IndexBuilder, IndexReader
    from rlm_tools_bsl.format_detector import detect_format
    from rlm_tools_bsl.helpers import make_helpers

    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
    (tmp_path / "Configuration.xml").write_text("<Configuration/>", encoding="utf-8")
    doc_ext = tmp_path / "Documents" / "Заказ" / "Ext"
    doc_ext.mkdir(parents=True)
    (doc_ext / "ObjectModule.bsl").write_text(
        "Процедура ОбработкаПроведения(Отказ, Режим)\n\tДвижения.Продажи.Записывать = Истина;\nКонецПроцедуры\n",
        encoding="utf-8",
    )
    cat_ext = tmp_path / "Catalogs" / "Номенклатура" / "Ext"
    cat_ext.mkdir(parents=True)
    (cat_ext / "ObjectModule.bsl").write_text("Процедура П()\nКонецПроцедуры\n", encoding="utf-8")

    db_path = IndexBuilder().build(str(tmp_path), build_calls=False, build_metadata=True)
    conn = sqlite3.connect(str(db_path))
    # ext_module_path — NOT NULL без DEFAULT (см. схему), пропуск дал бы IntegrityError.
    conn.execute(
        "INSERT INTO extension_overrides (object_name, target_method, annotation, extension_name,"
        " extension_method, extension_root, source_path, ext_module_path)"
        " VALUES ('Документ.Заказ', 'ОбработкаПроведения', '&Вместо', 'ТестРасш',"
        " 'ext_ОбработкаПроведения', 'root', 'Documents/Заказ/Ext/ObjectModule.bsl',"
        " 'Documents/Заказ/Ext/ObjectModule.bsl')"
    )
    conn.commit()
    conn.close()

    reader = IndexReader(str(db_path))
    helpers, resolve_safe = make_helpers(str(tmp_path), idx_reader=reader)
    bsl = make_bsl_helpers(
        base_path=str(tmp_path),
        resolve_safe=resolve_safe,
        read_file_fn=helpers["read_file"],
        grep_fn=helpers["grep"],
        glob_files_fn=helpers["glob_files"],
        format_info=detect_format(str(tmp_path)),
        idx_reader=reader,
    )
    return bsl, reader


def test_docstring_shape_lists_cover_every_returned_key(tmp_path, monkeypatch):
    """Перечень ключей в docstring сверяется с ЖИВЫМ вызовом, а не читается глазами.

    Берём только те хелперы, чей docstring ЯВНО перечисляет форму ответа: там пропуск
    ключа — протухшее утверждение, а не «дока не детализирует». Строки (`overrides[i]`)
    перечнем не покрыты ни у кого, поэтому по строке проверяется ровно одно обещание
    релиза: ключ `extension_file` назван и реально есть.
    """
    docs = _release_docstrings("get_overrides", "find_references_to_object")
    bsl, reader = _shape_env(tmp_path, monkeypatch)
    try:
        overrides = bsl["get_overrides"]()
        references = bsl["find_references_to_object"]("Справочник.Номенклатура")
        for name, data in (("get_overrides", overrides), ("find_references_to_object", references)):
            doc = docs[name]
            assert doc, f"{name}: docstring исчез"
            missing = sorted(k for k in data if k not in doc)
            assert not missing, f"{name}: ключи ответа НЕ названы в перечне docstring: {missing}"
            meta = data.get("_meta")
            if isinstance(meta, dict):
                missing_meta = sorted(k for k in meta if k not in doc)
                assert not missing_meta, f"{name}: ключи _meta НЕ названы в docstring: {missing_meta}"

        rows = overrides["overrides"]
        assert rows, "фикстура деградировала: строк перехвата нет, проверка стала бы ложной"
        assert "extension_file" in rows[0], "релиз обещал ключ extension_file в КАЖДОЙ строке"
        assert "extension_file" in docs["get_overrides"], (
            "docstring get_overrides не называет extension_file — обещание «ключ есть всегда» живёт только в CHANGELOG"
        )
    finally:
        reader.close()


# Ревью чистым агентом (Fable) нашло то, что девять кругов пропустили: строка INDEX TIPS —
# самый видимый текст релиза, она инлайнится в КАЖДЫЙ старт с индексом — называла точной
# формой `find_module('X')[0]['path']`, тогда как остальные 21 место релиза и сам CHANGELOG
# обещают `[i]['path']`. А `[0]` — ПРОИЗВОЛЬНЫЙ и вдобавок НЕСТАБИЛЬНЫЙ модуль:
# `get_all_modules()` идёт без `ORDER BY`, и на ОДНОМ дереве три пересборки индекса дали
# два разных порядка (`ObjectModule, Module, ManagerModule` и `ManagerModule, ObjectModule,
# Module`) — то есть `[0]` меняется без единой правки кода. Измеренная цена совета с `[0]`,
# когда им оказался ManagerModule: `find_definition(метод, [0])` → total=0 (с ObjectModule —
# 1); `find_call_hierarchy`/`find_callers_context` → 1 вызывающий вместо 3, и теряются
# именно КВАЛИФИЦИРОВАННЫЕ вызовы (`Док.Метод()`, `Документы.X.Метод()`) — из чужого модуля
# метод объекта иначе и не зовут. Механизм: hint, не разрешившийся в ключ, оставляет
# name-fallback `callee_name LIKE '<hint>.%' OR callee_name = proc_name`, и rel_path
# ManagerModule не совпадает ни с чем. То есть «точная форма» давала молчаливый ноль и
# молчаливую недостачу рёбер.
#
# Прежние гарды этого не видели: `_CONCRETE_ROUTE` считает маршрут названным при ЛЮБОМ
# `find_module(`. Здесь правило именно про индекс: `[0]` законен только с фильтром
# `module_type=` в том же вызове.
_ARBITRARY_MODULE_PICK = r"find_module\(([^)]*)\)\[0\]\["


def test_no_agent_facing_text_picks_an_arbitrary_module_by_index_zero():
    """`find_module(...)[0]` без `module_type=` — это произвольный модуль объекта."""
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[1]
    surfaces = {p.name: p.read_text(encoding="utf-8") for p in sorted(root.joinpath("docs").glob("*.md"))}
    for module in ("bsl_helpers.py", "bsl_index.py", "bsl_knowledge.py", "bsl_strategy_data.py"):
        surfaces[module] = root.joinpath("src", "rlm_tools_bsl", module).read_text(encoding="utf-8")

    pick = re.compile(_ARBITRARY_MODULE_PICK)
    offenders = []
    for name, text in surfaces.items():
        for match in pick.finditer(text):
            if "module_type" not in match.group(1):
                line = text[: match.start()].count("\n") + 1
                offenders.append(f"{name}:{line}")
    assert not offenders, (
        "find_module(...)[0] без module_type= в agent-facing тексте: порядок строк не задан "
        "(запрос без ORDER BY), поэтому [0] — произвольный и нестабильный между пересборками "
        "модуль; на ManagerModule документа find_definition отдаёт 0, а иерархия вызовов "
        "теряет квалифицированные рёбра. Ставьте [i] либо module_type='ObjectModule': " + "; ".join(offenders)
    )


def _object_with_two_declaring_modules(tmp_path):
    """Документ, где обработчик объявлен И в ObjectModule, И в модуле формы.

    Это не искусственный случай: `ПередЗаписью`/`ПриЗаписи` — обработчики И объекта,
    И формы, и сам сценарий приёмки предлагает их как замену `ОбработкаПроведения`.
    """
    doc = tmp_path / "Documents" / "ПродажаТоваров"
    (doc / "Ext").mkdir(parents=True)
    (doc / "Ext" / "ObjectModule.bsl").write_text(
        "Процедура ПередЗаписью(Отказ)\n\tСообщить(1);\nКонецПроцедуры\n", encoding="utf-8"
    )
    form = doc / "Forms" / "ФормаДокумента" / "Ext" / "Form"
    form.mkdir(parents=True)
    (form / "Module.bsl").write_text(
        "&НаКлиенте\nПроцедура ПередЗаписью(Отказ)\n\tСообщить(2);\nКонецПроцедуры\n", encoding="utf-8"
    )
    (tmp_path / "Configuration.xml").write_text("<Configuration/>", encoding="utf-8")
    return str(tmp_path)


def test_object_form_hint_does_not_pin_one_module(tmp_path, monkeypatch):
    """Негативный контроль под тексты: объектная форма НЕ пинит один, rel_path — пинит.

    Если резолвинг когда-нибудь начнёт пинить один и на объектной форме, упадёт
    ИМЕННО этот тест — и тогда тексты доков надо будет менять обратно, осознанно.
    """
    from rlm_tools_bsl.bsl_helpers import make_bsl_helpers
    from rlm_tools_bsl.bsl_index import IndexBuilder, IndexReader
    from rlm_tools_bsl.format_detector import detect_format
    from rlm_tools_bsl.helpers import make_helpers

    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
    base = _object_with_two_declaring_modules(tmp_path)
    db_path = IndexBuilder().build(base, build_calls=False, build_metadata=True)
    reader = IndexReader(db_path)
    try:
        helpers, resolve_safe = make_helpers(base, idx_reader=reader)
        bsl = make_bsl_helpers(
            base_path=base,
            resolve_safe=resolve_safe,
            read_file_fn=helpers["read_file"],
            grep_fn=helpers["grep"],
            glob_files_fn=helpers["glob_files"],
            format_info=detect_format(base),
            idx_reader=reader,
        )
        object_modules = [m for m in bsl["find_module"]("ПродажаТоваров") if m["module_type"] == "ObjectModule"]
        assert len(object_modules) == 1, object_modules

        for hint in ("Документ.ПродажаТоваров", "ПродажаТоваров"):
            d = bsl["find_definition"]("ПередЗаписью", hint)
            assert d["total"] == 2, (hint, d)
            assert d["_meta"]["hint_applied"] is True, (hint, d["_meta"])
            assert d["_meta"]["unique"] is False, (hint, d["_meta"])

        exact = bsl["find_definition"]("ПередЗаписью", object_modules[0]["path"])
        assert exact["total"] == 1, exact
        assert exact["_meta"]["unique"] is True, exact["_meta"]
    finally:
        reader.close()
