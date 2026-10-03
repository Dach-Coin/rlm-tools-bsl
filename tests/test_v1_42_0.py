"""v1.42.0 — граф вызовов видит запуск по имени и вызов через менеджер; аудит адресатов.

``BUILDER_VERSION`` 16 → 17, пересборка индексов обязательна. Таблица ``calls`` получает
``callee_via`` (категория коллекции менеджера) и ``call_kind`` (вид ребра); поверх новых
данных — хелпер ``find_unresolved_calls``.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3

import pytest

from rlm_tools_bsl.bsl_index import (
    BUILDER_VERSION,
    OLD_INDEX_GAPS,
    IndexBuilder,
    IndexReader,
)

# Валидный дескриптор CF: заглушка ``<Configuration/>`` дала бы source_support=
# foreign_with_bsl, и rlm_start шёл бы не по той ветке, которую проверяют тесты.
_CF_DESCRIPTOR = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">\n'
    '  <Configuration uuid="00000000-0000-0000-0000-000000000001">\n'
    "    <Properties><Name>Тест</Name></Properties>\n"
    "  </Configuration>\n"
    "</MetaDataObject>\n"
)


def _write_project(root, files):
    """CF-раскладка: файлы + валидный ``Configuration.xml``."""
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8-sig")
    (root / "Configuration.xml").write_text(_CF_DESCRIPTOR, encoding="utf-8")


def _conn(db_path):
    c = sqlite3.connect(str(db_path))
    c.row_factory = sqlite3.Row
    return c


# ── Задача 1: схема v17, порог пересборки, недостачи старого индекса ─────────

SCHEMA_FILES = {
    "CommonModules/ОбщийМодульА/Ext/Module.bsl": "Функция Метод1() Экспорт\n    Возврат 1;\nКонецФункции\n",
    "Documents/Заказ/Ext/ObjectModule.bsl": "Процедура П()\n    ОбщийМодульА.Метод1();\nКонецПроцедуры\n",
}


@pytest.fixture
def built_v17(tmp_path):
    _write_project(tmp_path, SCHEMA_FILES)
    db_path = IndexBuilder().build(str(tmp_path), build_calls=True)
    return db_path, tmp_path


@pytest.fixture
def built_v16_copy(tmp_path):
    """Сборка текущим кодом, затем откат схемы ``calls`` и версии до v16.

    SQLite без ``DROP COLUMN`` на старых сборках, поэтому таблица пересоздаётся
    с прежним составом колонок (прецедент — tests/test_bsl_index_integration.py).
    """
    _write_project(tmp_path, SCHEMA_FILES)
    db_path = IndexBuilder().build(str(tmp_path), build_calls=True)
    con = sqlite3.connect(db_path)
    try:
        con.executescript(
            "CREATE TABLE calls_old AS SELECT id, caller_id, callee_name, line, callee_key FROM calls;"
            "DROP TABLE calls;"
            "ALTER TABLE calls_old RENAME TO calls;"
        )
        con.execute("UPDATE index_meta SET value='16' WHERE key IN ('version', 'builder_version')")
        con.commit()
        cols = {r[1] for r in con.execute("PRAGMA table_info(calls)")}
        assert "call_kind" not in cols and "callee_via" not in cols
    finally:
        con.close()
    return db_path, tmp_path


@pytest.fixture
def built_v17_no_calls(tmp_path):
    """То же дерево, собранное штатным ``--no-calls``: колонки v17 есть, граф выключен."""
    _write_project(tmp_path, SCHEMA_FILES)
    db_path = IndexBuilder().build(str(tmp_path), build_calls=False)
    with _conn(db_path) as c:
        assert c.execute("SELECT value FROM index_meta WHERE key='has_calls'").fetchone()[0] == "0"
        cols = {r[1] for r in c.execute("PRAGMA table_info(calls)")}
        assert {"callee_via", "call_kind"} <= cols
        assert c.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 0
    return db_path, tmp_path


class TestSchemaV17:
    def test_builder_version_and_gap_table(self):
        assert BUILDER_VERSION == 17
        # последняя строка таблицы недостач несёт порог текущей версии: бамп без новой
        # строки ронял бы этот тест, а не выдавал старый текст
        assert OLD_INDEX_GAPS[-1][0] == BUILDER_VERSION
        assert [v for v, _ in OLD_INDEX_GAPS] == sorted(v for v, _ in OLD_INDEX_GAPS)
        assert all("ё" not in text for _, text in OLD_INDEX_GAPS)

    def test_calls_has_v17_columns_and_partial_index(self, built_v17):
        db_path, _ = built_v17
        with sqlite3.connect(db_path) as c:
            cols = {r[1] for r in c.execute("PRAGMA table_info(calls)")}
            idx = {r[1] for r in c.execute("PRAGMA index_list(calls)")}
        assert {"callee_via", "call_kind"} <= cols
        assert "idx_calls_by_name" in idx

    def test_update_on_v16_index_forces_full_rebuild(self, built_v16_copy):
        _db_path, base = built_v16_copy
        res = IndexBuilder().update(str(base))
        assert res["rebuild_reason"] == "schema upgrade v16->17"

    def test_reader_on_v16_index_has_no_call_audit(self, built_v16_copy):
        db_path, _base = built_v16_copy
        r = IndexReader(db_path)
        try:
            assert r.has_call_audit is False
            res = r.get_callers("Метод1", "")
            assert res["callers"], "фикстура обязана давать хотя бы одного вызывающего"
            assert all(c["call_kind"] == "call" for c in res["callers"])
        finally:
            r.close()

    def test_reader_on_v17_index_has_call_audit(self, built_v17):
        db_path, _base = built_v17
        r = IndexReader(db_path)
        try:
            assert r.builder_version == 17
            assert r.has_call_audit is True
        finally:
            r.close()

    def test_v17_no_calls_is_not_auditable(self, built_v17_no_calls):
        db_path, _base = built_v17_no_calls
        r = IndexReader(db_path)
        try:
            assert r.builder_version == 17
            assert r.has_call_audit is False  # схема v17 есть, граф выключен
        finally:
            r.close()

    def test_update_preserves_disabled_calls_option(self, built_v17_no_calls):
        db_path, base = built_v17_no_calls
        IndexBuilder().update(str(base))
        with sqlite3.connect(db_path) as c:
            assert c.execute("SELECT value FROM index_meta WHERE key='has_calls'").fetchone()[0] == "0"
            assert c.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 0

    def test_old_index_warning_lists_gaps(self, built_v16_copy):
        from rlm_tools_bsl.server import _rlm_end, _rlm_start

        _db_path, base = built_v16_copy
        raw = _rlm_start(path=str(base), query="")
        data = json.loads(raw)
        try:
            assert data["source_support"] == "supported", "фикстура обязана идти по поддержанной ветке"
            assert data["index"]["loaded"] is True
            text15 = dict(OLD_INDEX_GAPS)[15]
            text17 = dict(OLD_INDEX_GAPS)[17]
            warns = [w for w in data["index"]["warnings"] if "сборщиком v16" in w]
            assert len(warns) == 1, data["index"]["warnings"]
            assert text17 in warns[0]
            # для v16 недостача v15 уже закрыта — её текст здесь ложен
            assert text15 not in warns[0]
            assert "ё" not in warns[0]
        finally:
            _rlm_end(data["session_id"])


# ── Задача 2: коллекция менеджера и член цепочки ──────────────────────────────

_UNSET = object()


def _edge(db_path, rel, callee_name, kind=_UNSET):
    """``(callee_via, call_kind, callee_key)`` ровно одного ребра модуля ``rel``."""
    sql = (
        "SELECT c.callee_via, c.call_kind, c.callee_key, c.line FROM calls c "
        "JOIN methods m ON m.id = c.caller_id JOIN modules mod ON mod.id = m.module_id "
        "WHERE mod.rel_path = ? AND c.callee_name = ?"
    )
    params: list = [rel, callee_name]
    if kind is not _UNSET:
        sql += " AND c.call_kind IS ?"
        params.append(kind)
    with _conn(db_path) as c:
        rows = [tuple(r) for r in c.execute(sql, params)]
    assert len(rows) == 1, f"{rel}: {callee_name!r} kind={kind!r} -> {rows}"
    return rows[0][:3]


def _edge_line(db_path, rel, callee_name, kind=_UNSET):
    sql = (
        "SELECT c.line FROM calls c JOIN methods m ON m.id = c.caller_id "
        "JOIN modules mod ON mod.id = m.module_id WHERE mod.rel_path = ? AND c.callee_name = ?"
    )
    params: list = [rel, callee_name]
    if kind is not _UNSET:
        sql += " AND c.call_kind IS ?"
        params.append(kind)
    with _conn(db_path) as c:
        rows = [r[0] for r in c.execute(sql, params)]
    assert len(rows) == 1, rows
    return rows[0]


def _make_callee_key(rel_path, method):
    from rlm_tools_bsl.bsl_index import _make_callee_key as mk

    return mk(rel_path, method)


ZAKAZ = "Documents/Заказ/Ext/ObjectModule.bsl"
ZAKAZ_BY_FMT = {"cf": ZAKAZ, "edt": "Documents/Заказ/ObjectModule.bsl"}

OBRABOTAT_BSL = (
    "Процедура Обработать()\n"
    '    А = Справочники.Номенклатура.НайтиПоАртикулу("1");\n'
    '    Б = СПРАВОЧНИКИ.номенклатура.НайтиПоКоду("2");\n'
    "    Справочники.Контрагенты.УстановитьСтатус(1);\n"
    "    Справочники.БезМодуля.ПустаяСсылка();\n"
    "    Объект.Товары.Добавить();\n"
    "    ИмяПоля = Метаданные.Справочники.Номенклатура.ПолноеИмя();\n"
    "    УстановитьСтатус(2);\n"
    "КонецПроцедуры\n"
)
ZAKAZ_OBJECT_BSL = OBRABOTAT_BSL + "\nПроцедура УстановитьСтатус(С) Экспорт\nКонецПроцедуры\n"
MANAGER_BSL = "Функция НайтиПоАртикулу(А) Экспорт\n    Возврат Неопределено;\nКонецФункции\n"
KONTRAGENTY_MANAGER_BSL = "Процедура УстановитьСтатус(С) Экспорт\nКонецПроцедуры\n"

LINE_BARE_SET_STATUS = OBRABOTAT_BSL.splitlines().index("    УстановитьСтатус(2);") + 1
LINE_MGR_SET_STATUS = OBRABOTAT_BSL.splitlines().index("    Справочники.Контрагенты.УстановитьСтатус(1);") + 1

MGR_FILES = {
    "Catalogs/Номенклатура/Ext/ManagerModule.bsl": MANAGER_BSL,
    "Catalogs/Контрагенты/Ext/ManagerModule.bsl": KONTRAGENTY_MANAGER_BSL,
    ZAKAZ: ZAKAZ_OBJECT_BSL,
}

_EDT_DESCRIPTOR = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<mdclass:Configuration xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass" '
    'uuid="00000000-0000-0000-0000-000000000003">\n'
    "    <name>Тест</name>\n"
    "</mdclass:Configuration>\n"
)


def _write_edt_project(root, files):
    """EDT-раскладка: файлы + ``Configuration/Configuration.mdo``."""
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8-sig")
    (root / "Configuration").mkdir(exist_ok=True)
    (root / "Configuration" / "Configuration.mdo").write_text(_EDT_DESCRIPTOR, encoding="utf-8")


WRITERS = {"cf": _write_project, "edt": _write_edt_project}


def _mgr_files(fmt, with_manager):
    files = {ZAKAZ_BY_FMT[fmt]: ZAKAZ_OBJECT_BSL}
    if with_manager:
        rel = {"cf": "Catalogs/Номенклатура/Ext/ManagerModule.bsl", "edt": "Catalogs/Номенклатура/ManagerModule.bsl"}
        files[rel[fmt]] = MANAGER_BSL
    return files


@pytest.fixture
def mgr_built(tmp_path):
    _write_project(tmp_path, MGR_FILES)
    db_path = IndexBuilder().build(str(tmp_path), build_calls=True)
    return db_path, tmp_path


def _apply_changes(root, files_old, files_new):
    for rel, content in files_new.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8-sig")
    for rel in set(files_old) - set(files_new):
        (root / rel).unlink()


def _edge_tuples_v17(db_path):
    """Как ``_edge_tuples`` (tests/test_call_resolution.py) + via и kind: всё текстовое и
    от rel_path, поэтому множества двух НЕЗАВИСИМО собранных баз сравнимы. Ключ сортировки
    несёт отдельный признак ``None``: строки с одинаковыми первыми полями и ``None`` против
    строки в ``call_kind`` иначе уронили бы ``sorted`` с ``TypeError``."""
    with _conn(db_path) as c:
        return sorted(
            [
                (r["cp"], r["cn"], r["callee_name"], r["line"], r["callee_via"], r["call_kind"], r["callee_key"])
                for r in c.execute(
                    "SELECT mod.rel_path AS cp, m.name AS cn, cl.callee_name, cl.line, cl.callee_via, "
                    "cl.call_kind, cl.callee_key FROM calls cl JOIN methods m ON cl.caller_id = m.id "
                    "JOIN modules mod ON m.module_id = mod.id"
                )
            ],
            key=lambda row: tuple((value is not None, value) for value in row),
        )


def _assert_update_equals_build_v17(tmp_path_factory, files_a, files_b, write=_write_project):
    """Собрать A, превратить на диске в B, update; кортежи обязаны совпасть со СВЕЖЕЙ сборкой B."""
    da = tmp_path_factory.mktemp("upd")
    write(da, files_a)
    db_a = IndexBuilder().build(str(da), build_calls=True)
    _apply_changes(da, files_a, files_b)
    IndexBuilder().update(str(da))
    dfresh = tmp_path_factory.mktemp("fresh")
    write(dfresh, files_b)
    db_b = IndexBuilder().build(str(dfresh), build_calls=True)
    assert _edge_tuples_v17(db_a) == _edge_tuples_v17(db_b)
    return db_a


class TestManagerTier:
    def test_manager_call_resolves_to_manager_module(self, mgr_built):
        db, _ = mgr_built
        assert _edge(db, ZAKAZ, "Номенклатура.НайтиПоАртикулу") == (
            "Catalogs",
            None,
            _make_callee_key("Catalogs/Номенклатура/Ext/ManagerModule.bsl", "НайтиПоАртикулу"),
        )

    def test_platform_method_gets_inert_real_key_any_case(self, mgr_built):
        db, _ = mgr_built
        # регистр коллекции и объекта не важен; ключ — реальный путь модуля, метода в нём нет
        assert _edge(db, ZAKAZ, "номенклатура.НайтиПоКоду") == (
            "Catalogs",
            None,
            _make_callee_key("Catalogs/Номенклатура/Ext/ManagerModule.bsl", "НайтиПоКоду"),
        )

    def test_missing_manager_module_gets_synthetic_key_with_leading_slash(self, mgr_built):
        db, _ = mgr_built
        assert _edge(db, ZAKAZ, "БезМодуля.ПустаяСсылка")[2] == "/Catalogs/БезМодуля/ManagerModule.bsl::пустаяссылка"

    def test_metadata_chain_is_not_a_manager_call(self, mgr_built):
        db, _ = mgr_built
        assert _edge(db, ZAKAZ, "Номенклатура.ПолноеИмя") == (None, "member", None)

    def test_other_objects_manager_call_left_exact_mode_heuristics(self, mgr_built):
        db, _ = mgr_built
        r = IndexReader(db)
        try:
            res = r.get_callers("УстановитьСтатус", ZAKAZ)
            # До v17: exact 1 (голый УстановитьСтатус(2)) + fallback 1 (Справочники.Контрагенты.
            # УстановитьСтатус(1) без ключа). После: вызов чужого менеджера из эвристики ушёл.
            assert [c["line"] for c in res["callers"]] == [LINE_BARE_SET_STATUS]
            assert (res["_meta"]["exact_rows"], res["_meta"]["fallback_rows"]) == (1, 0)
            res2 = r.get_callers("УстановитьСтатус", "Catalogs/Контрагенты/Ext/ManagerModule.bsl")
            assert [c["line"] for c in res2["callers"]] == [LINE_MGR_SET_STATUS]
            assert res2["_meta"]["exact_rows"] == 1
        finally:
            r.close()

    def test_metadata_chain_split_across_lines_is_member(self, tmp_path):
        # Точка цепочки на предыдущей строке (в том числе через пустую строку и
        # комментарий) и пробельные символы после первой точки — всё ещё член цепочки.
        body = (
            "Процедура П()\n"
            "    А = Метаданные. Справочники.Номенклатура.ПолноеИмя();\n"
            "    Б = Метаданные.\tСправочники.Номенклатура.Синоним();\n"
            "    В = Метаданные.\n"
            "        Справочники.Номенклатура.Представление();\n"
            "    Г = Метаданные.\n"
            "\n"
            "        // пояснение\n"
            "        Справочники.Номенклатура.ПолучитьИмя();\n"
            "    Д = Объект.\n"
            "        ОбщийМодульА.Метод1();\n"
            "    Е = Справочники.Номенклатура.НайтиПоАртикулу(1);\n"
            "    ОбщийМодульА.Метод1();\n"
            "КонецПроцедуры\n"
        )
        files = {
            "Catalogs/Номенклатура/Ext/ManagerModule.bsl": MANAGER_BSL,
            "CommonModules/ОбщийМодульА/Ext/Module.bsl": "Функция Метод1() Экспорт\nКонецФункции\n",
            ZAKAZ: body,
        }
        _write_project(tmp_path, files)
        db = IndexBuilder().build(str(tmp_path), build_calls=True)
        lines = body.splitlines()
        for name, marker in (
            ("Номенклатура.ПолноеИмя", "ПолноеИмя"),
            ("Номенклатура.Синоним", "Синоним"),
            ("Номенклатура.Представление", "Представление"),
            ("Номенклатура.ПолучитьИмя", "ПолучитьИмя"),
        ):
            assert _edge(db, ZAKAZ, name) == (None, "member", None), name
            expected_line = next(i for i, ln in enumerate(lines, 1) if marker + "(" in ln)
            assert _edge_line(db, ZAKAZ, name) == expected_line, name
        assert _edge(db, ZAKAZ, "ОбщийМодульА.Метод1", kind="member") == (None, "member", None)
        # контроль: настоящий вызов через коллекцию и прямой вызов общего модуля — точные ключи
        assert _edge(db, ZAKAZ, "Номенклатура.НайтиПоАртикулу")[2] == _make_callee_key(
            "Catalogs/Номенклатура/Ext/ManagerModule.bsl", "НайтиПоАртикулу"
        )
        assert _edge(db, ZAKAZ, "ОбщийМодульА.Метод1", kind=None)[2] == _make_callee_key(
            "CommonModules/ОбщийМодульА/Ext/Module.bsl", "Метод1"
        )
        r = IndexReader(db)
        try:
            got = r.get_callers("Метод1", "CommonModules/ОбщийМодульА/Ext/Module.bsl")
            direct_line = next(i for i, ln in enumerate(lines, 1) if ln == "    ОбщийМодульА.Метод1();")
            assert [c["line"] for c in got["callers"]] == [direct_line]
            for name in ("ПолноеИмя", "Синоним", "Представление", "ПолучитьИмя"):
                res = r.get_callers(name, "")
                assert res is None or res["callers"] == [], (name, res)
        finally:
            r.close()


LINE_DIRECT_COMMON_CALL = 3


def _build_member_shadow_project(tmp_path):
    files = {
        "CommonModules/ОбщийМодульА/Ext/Module.bsl": "Функция Метод1() Экспорт\nКонецФункции\n",
        # второй Метод1 — чтобы без hint цель была неоднозначна (name-fallback)
        "CommonModules/ДругойМодуль/Ext/Module.bsl": "Функция Метод1() Экспорт\nКонецФункции\n",
        ZAKAZ: "Процедура П()\n    Объект.ОбщийМодульА.Метод1();\n    ОбщийМодульА.Метод1();\nКонецПроцедуры\n",
    }
    assert files[ZAKAZ].splitlines()[LINE_DIRECT_COMMON_CALL - 1] == "    ОбщийМодульА.Метод1();"
    _write_project(tmp_path, files)
    return IndexBuilder().build(str(tmp_path), build_calls=True)


class TestMemberKind:
    def test_member_call_is_marked_and_never_resolved(self, mgr_built):
        db, _ = mgr_built
        assert _edge(db, ZAKAZ, "Товары.Добавить") == (None, "member", None)

    def test_member_named_like_common_module_is_not_resolved(self, tmp_path):
        # Одно имя ребра — две строки: член цепочки без ключа и прямой вызов с ключом модуля.
        db = _build_member_shadow_project(tmp_path)
        assert _edge(db, ZAKAZ, "ОбщийМодульА.Метод1", kind="member") == (None, "member", None)
        assert _edge(db, ZAKAZ, "ОбщийМодульА.Метод1", kind=None)[2] == _make_callee_key(
            "CommonModules/ОбщийМодульА/Ext/Module.bsl", "Метод1"
        )
        r = IndexReader(db)
        try:
            got = r.get_callers("Метод1", "CommonModules/ОбщийМодульА/Ext/Module.bsl")
            assert [c["line"] for c in got["callers"]] == [LINE_DIRECT_COMMON_CALL]
            assert (got["_meta"]["exact_rows"], got["_meta"]["fallback_rows"]) == (1, 0)
            by_name = r.get_callers("Метод1", "")
            assert by_name["_meta"]["target_exact"] is False
            assert [c["line"] for c in by_name["callers"]] == [LINE_DIRECT_COMMON_CALL]
            assert by_name["_meta"]["total_callers"] == 1
            hinted = r.get_callers("Метод1", "ОбщийМодульА")
            assert all(c["line"] == LINE_DIRECT_COMMON_CALL for c in hinted["callers"])
        finally:
            r.close()


def _write_two_extension_managers(tmp_path):
    base = tmp_path / "exts"
    for ext in ("ExtA", "ExtB"):
        p = base / ext / "Catalogs" / "Номенклатура" / "Ext" / "ManagerModule.bsl"
        p.parent.mkdir(parents=True)
        p.write_text("Процедура Метод() Экспорт\nКонецПроцедуры\n", encoding="utf-8-sig")
    caller = base / "ExtA" / "Documents" / "Заказ" / "Ext" / "ObjectModule.bsl"
    caller.parent.mkdir(parents=True)
    caller.write_text("Процедура П()\n    Справочники.Номенклатура.Метод();\nКонецПроцедуры\n", encoding="utf-8-sig")
    return base


class TestManagerUpdateEqualsBuild:
    @pytest.mark.parametrize("fmt", ["cf", "edt"])
    def test_manager_module_appears_then_disappears(self, tmp_path_factory, fmt):
        mgr_rel = {
            "cf": "Catalogs/Номенклатура/Ext/ManagerModule.bsl",
            "edt": "Catalogs/Номенклатура/ManagerModule.bsl",
        }[fmt]
        caller = ZAKAZ_BY_FMT[fmt]
        without = _mgr_files(fmt, with_manager=False)
        with_mgr = {**without, mgr_rel: MANAGER_BSL}
        synthetic = "/Catalogs/Номенклатура/ManagerModule.bsl::найтипоартикулу"
        # модуль появился → реальный ключ, update ≡ build
        db = _assert_update_equals_build_v17(tmp_path_factory, without, with_mgr, write=WRITERS[fmt])
        assert _edge(db, caller, "Номенклатура.НайтиПоАртикулу")[2] == _make_callee_key(mgr_rel, "НайтиПоАртикулу")
        # на EDT реальный путь отличается от синтетического ТОЛЬКО ведущим «/» — ключи разные
        assert _make_callee_key(mgr_rel, "НайтиПоАртикулу") != synthetic
        # модуль исчез → снова синтетический, update ≡ build
        db = _assert_update_equals_build_v17(tmp_path_factory, with_mgr, without, write=WRITERS[fmt])
        assert _edge(db, caller, "Номенклатура.НайтиПоАртикулу")[2] == synthetic

    def test_second_manager_module_makes_key_synthetic(self, tmp_path):
        base = _write_two_extension_managers(tmp_path)
        db = IndexBuilder().build(str(base), build_calls=True)
        key = _edge(db, "ExtA/Documents/Заказ/Ext/ObjectModule.bsl", "Номенклатура.Метод")[2]
        assert key == "/Catalogs/Номенклатура/ManagerModule.bsl::метод"
        r = IndexReader(db)
        try:
            for mod in (
                "ExtA/Catalogs/Номенклатура/Ext/ManagerModule.bsl",
                "ExtB/Catalogs/Номенклатура/Ext/ManagerModule.bsl",
            ):
                meta = r.get_callers("Метод", mod)["_meta"]
                assert (meta["exact_rows"], meta["fallback_rows"]) == (0, 0)
        finally:
            r.close()

    @pytest.mark.skipif(not shutil.which("git"), reason="git недоступен")
    def test_manager_module_change_on_git_fast_path(self, tmp_path, tmp_path_factory):
        # Вторая ветка update (git fast path) собирает changed_managers так же, как полный скан.
        from test_git_delta import _git, _git_init

        root = tmp_path / "repo"
        base = root / "src"
        without = _mgr_files("cf", with_manager=False)
        mgr_rel = "Catalogs/Номенклатура/Ext/ManagerModule.bsl"
        with_mgr = {**without, mgr_rel: MANAGER_BSL}
        _write_project(base, without)
        _git_init(root)
        db = IndexBuilder().build(str(base), build_calls=True)
        for old, new in ((without, with_mgr), (with_mgr, without)):
            _apply_changes(base, old, new)
            _git(root, "add", "-A")
            _git(root, "commit", "-m", "manager module change")
            res = IndexBuilder().update(str(base))
            assert res["git_fast_path"] is True, res
            fresh = tmp_path_factory.mktemp("fresh_git")
            _write_project(fresh, new)
            assert _edge_tuples_v17(db) == _edge_tuples_v17(IndexBuilder().build(str(fresh), build_calls=True))
        assert _edge(db, ZAKAZ, "Номенклатура.НайтиПоАртикулу")[2] == (
            "/Catalogs/Номенклатура/ManagerModule.bsl::найтипоартикулу"
        )

    def test_second_manager_module_appears_on_update(self, tmp_path_factory):
        # Переход неоднозначности: второй модуль менеджера появился инкрементом.
        one = {
            "ExtA/Catalogs/Номенклатура/Ext/ManagerModule.bsl": "Процедура Метод() Экспорт\nКонецПроцедуры\n",
            "ExtA/Documents/Заказ/Ext/ObjectModule.bsl": (
                "Процедура П()\n    Справочники.Номенклатура.Метод();\nКонецПроцедуры\n"
            ),
        }
        two = {**one, "ExtB/Catalogs/Номенклатура/Ext/ManagerModule.bsl": "Процедура Метод() Экспорт\nКонецПроцедуры\n"}
        db = _assert_update_equals_build_v17(tmp_path_factory, one, two)
        assert _edge(db, "ExtA/Documents/Заказ/Ext/ObjectModule.bsl", "Номенклатура.Метод")[2] == (
            "/Catalogs/Номенклатура/ManagerModule.bsl::метод"
        )
        db = _assert_update_equals_build_v17(tmp_path_factory, two, one)
        assert _edge(db, "ExtA/Documents/Заказ/Ext/ObjectModule.bsl", "Номенклатура.Метод")[2] == _make_callee_key(
            "ExtA/Catalogs/Номенклатура/Ext/ManagerModule.bsl", "Метод"
        )


# ── Задача 3: запуск по имени ─────────────────────────────────────────────────

import rlm_tools_bsl.bsl_index as BI  # noqa: E402

FON_BSL = (
    "Процедура Задача() Экспорт\nКонецПроцедуры\n\n"
    "Процедура Вторая() Экспорт\nКонецПроцедуры\n\n"
    "Процедура Внутренняя()\nКонецПроцедуры\n\n"
    "Процедура Вторая2() Экспорт\nКонецПроцедуры\n"
)
DOK_MANAGER_BSL = "Функция Пересчитать() Экспорт\n    Возврат 1;\nКонецФункции\n"
LONG_TAIL = "Ж" * 2500
ZAPUSKATEL_BSL = (
    "Процедура Литерал() Экспорт\n"
    '    ФоновыеЗадания.Выполнить("Фон.Задача", Параметры);\n'
    "КонецПроцедуры\n"
    "\n"
    "Процедура ЛитералНаСледующейСтроке() Экспорт\n"
    "    фоновыезадания.выполнить(\n"
    "        // что запускаем\n"
    '        "фон.задача",\n'
    "        Ключ);\n"
    "КонецПроцедуры\n"
    "\n"
    "Процедура ЧерезПеременную(Условие) Экспорт\n"
    "    Если Условие Тогда\n"
    '        ИмяМетода = "Фон.Задача";\n'
    "    Иначе\n"
    '        ИмяМетода = "Фон.Вторая";\n'
    "    КонецЕсли;\n"
    "    ДлительныеОперации.ВыполнитьВФоне(ИмяМетода, Параметры, ПараметрыВыполнения);\n"
    "КонецПроцедуры\n"
    "\n"
    "Процедура ПрисвоениеПослеЗапуска(ИмяМетода) Экспорт\n"
    "    ФоновыеЗадания.Выполнить(ИмяМетода);\n"
    '    ИмяМетода = "Фон.Задача";\n'
    "КонецПроцедуры\n"
    "\n"
    "Процедура ПереопределениеПослеЗапуска() Экспорт\n"
    '    ИмяМетода = "Фон.Вторая";\n'
    "    ФоновыеЗадания.Выполнить(ИмяМетода);\n"
    '    ИмяМетода = "Фон.Задача";\n'
    "КонецПроцедуры\n"
    "\n"
    "Процедура Менеджер() Экспорт\n"
    '    ДлительныеОперации.ВыполнитьФункцию(ПараметрыВыполнения, "Документы.Док.Пересчитать", 1);\n'
    "КонецПроцедуры\n"
    "\n"
    "Процедура Параметр(ИмяМетода) Экспорт\n"
    "    ФоновыеЗадания.Выполнить(ИмяМетода);\n"
    "КонецПроцедуры\n"
    "\n"
    "Процедура Выражение() Экспорт\n"
    '    BackgroundJobs.Execute("Фон." + Имя);\n'
    "КонецПроцедуры\n"
    "\n"
    "Процедура ЧетыреСегмента() Экспорт\n"
    '    ДлительныеОперации.ВыполнитьВФоне("Обработка.Х.МодульОбъекта.М", П, ПВ);\n'
    "КонецПроцедуры\n"
    "\n"
    "Процедура ВКомментарии() Экспорт\n"
    '    // ФоновыеЗадания.Выполнить("Фон.Задача");\n'
    '    Текст = "ФоновыеЗадания.Выполнить(""Фон.Задача"")";\n'
    "КонецПроцедуры\n"
    "\n"
    "Процедура Смешанная(Условие) Экспорт\n"
    "    Если Условие Тогда\n"
    '        ИмяМетода = "Фон.Задача";\n'
    "    Иначе\n"
    '        ИмяМетода = "Обработка.Х.МодульОбъекта.М";\n'
    "    КонецЕсли;\n"
    "    ФоновыеЗадания.Выполнить(ИмяМетода);\n"
    "КонецПроцедуры\n"
    "\n"
    "Процедура СтараяОбертка() Экспорт\n"
    '    ДлительныеОперации.ЗапуститьВыполнениеВФоне(УникальныйИдентификатор, "Фон.Задача", Параметры);\n'
    "КонецПроцедуры\n"
    "\n"
    "Процедура ДлинныйХвост() Экспорт\n"
    '    ДлительныеОперации.ВыполнитьВФоне("Фон.Вторая", Новый Структура("П", "' + LONG_TAIL + '"), ПВ);\n'
    "КонецПроцедуры\n"
    "\n"
    "Процедура НеЭкспорт() Экспорт\n"
    '    ФоновыеЗадания.Выполнить("Фон.Внутренняя");\n'
    "КонецПроцедуры\n"
    "\n"
    "Процедура ПрямойВызов() Экспорт\n"
    "    Фон.Вторая();\n"
    "КонецПроцедуры\n"
)


def _line_of(text, needle):
    hits = [i for i, ln in enumerate(text.splitlines(), 1) if needle in ln]
    assert len(hits) == 1, (needle, hits)
    return hits[0]


LINE_LITERAL = _line_of(ZAPUSKATEL_BSL, 'ФоновыеЗадания.Выполнить("Фон.Задача", Параметры);')
LINE_NEXT_CALL = _line_of(ZAPUSKATEL_BSL, "фоновыезадания.выполнить(")
LINE_MGR = _line_of(ZAPUSKATEL_BSL, '"Документы.Док.Пересчитать"')
LINE_NONEXPORT = _line_of(ZAPUSKATEL_BSL, '"Фон.Внутренняя"')
ZAPUSKATEL = "CommonModules/Запускатель/Ext/Module.bsl"
FON = "CommonModules/Фон/Ext/Module.bsl"


def _cm_descriptor(name):
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">\n'
        '  <CommonModule uuid="00000000-0000-0000-0000-000000000011">\n'
        f"    <Properties><Name>{name}</Name><Server>true</Server></Properties>\n"
        "  </CommonModule>\n"
        "</MetaDataObject>\n"
    )


LAUNCH_FILES = {
    FON: FON_BSL,
    ZAPUSKATEL: ZAPUSKATEL_BSL,
    "Documents/Док/Ext/ManagerModule.bsl": DOK_MANAGER_BSL,
    "CommonModules/Фон.xml": _cm_descriptor("Фон"),
    "CommonModules/Запускатель.xml": _cm_descriptor("Запускатель"),
}


@pytest.fixture
def launch_built(tmp_path):
    _write_project(tmp_path, LAUNCH_FILES)
    db_path = IndexBuilder().build(str(tmp_path), build_calls=True)
    return db_path, tmp_path


def _launch_rows(built, caller, module=ZAPUSKATEL, col="c.line"):
    db = built[0]
    with _conn(db) as c:
        rows = c.execute(
            f"SELECT c.callee_name, c.callee_via, c.call_kind, {col} FROM calls c "
            "JOIN methods m ON m.id = c.caller_id JOIN modules mod ON mod.id = m.module_id "
            "WHERE c.call_kind IN ('background', 'background_dynamic') AND mod.rel_path = ? AND m.name = ? "
            "ORDER BY c.line, c.callee_name",
            (module, caller),
        ).fetchall()
    return [tuple(r) for r in rows]


def _launch_keys(built, caller, module=ZAPUSKATEL):
    return [r[3] for r in _launch_rows(built, caller, module, col="c.callee_key")]


_FULL_CTX = (frozenset(), True)


class TestLaunchExtraction:
    def test_literal_same_line(self, launch_built):
        assert _launch_rows(launch_built, "Литерал") == [("Фон.Задача", None, "background", LINE_LITERAL)]

    def test_literal_next_line_with_comment_any_case(self, launch_built):
        # строка ребра — строка запускающего вызова, а не литерала
        assert _launch_rows(launch_built, "ЛитералНаСледующейСтроке") == [
            ("фон.задача", None, "background", LINE_NEXT_CALL)
        ]

    def test_local_variable_all_literal_assignments(self, launch_built):
        rows = _launch_rows(launch_built, "ЧерезПеременную")
        assert sorted(r[0] for r in rows) == ["Фон.Вторая", "Фон.Задача"]
        assert {r[2] for r in rows} == {"background"}

    def test_assignment_after_launch_cannot_supply_its_target(self, launch_built):
        assert [r[:3] for r in _launch_rows(launch_built, "ПрисвоениеПослеЗапуска")] == [
            ("", None, "background_dynamic")
        ]
        assert [r[:3] for r in _launch_rows(launch_built, "ПереопределениеПослеЗапуска")] == [
            ("Фон.Вторая", None, "background")
        ]

    def test_linear_overwrite_does_not_create_impossible_exact_edge(self):
        lines = ['ИмяМетода = "Фон.Задача";', 'ИмяМетода = "Фон.Вторая";', "ФоновыеЗадания.Выполнить(ИмяМетода);"]
        assert BI._extract_launch_edges(lines, 0, receiver_context=_FULL_CTX) == [("Фон.Вторая", 3, None, "background")]

    @pytest.mark.parametrize(
        "rhs, expected",
        [
            ('"Фон.Вторая"', ("Фон.Вторая", 2, None, "background")),
            ("ПолучитьИмя()", ("", 2, None, "background_dynamic")),
        ],
    )
    def test_overwrite_before_launch_on_same_line(self, rhs, expected):
        lines = ['Имя = "Фон.Задача";', f"Имя = {rhs}; ФоновыеЗадания.Выполнить(Имя);"]
        assert BI._extract_launch_edges(lines, 0, receiver_context=_FULL_CTX) == [expected]

    def test_two_launches_with_assignment_between_them_on_same_line(self):
        lines = [
            'Имя = "Фон.Задача";',
            'ФоновыеЗадания.Выполнить(Имя); Имя = "Фон.Вторая"; ФоновыеЗадания.Выполнить(Имя);',
        ]
        assert BI._extract_launch_edges(lines, 0, receiver_context=_FULL_CTX) == [
            ("Фон.Задача", 2, None, "background"),
            ("Фон.Вторая", 2, None, "background"),
        ]

    def test_assignment_after_launch_on_same_line_does_not_change_target(self):
        lines = ['Имя = "Фон.Задача";', 'ФоновыеЗадания.Выполнить(Имя); Имя = "Фон.Вторая";']
        assert BI._extract_launch_edges(lines, 0, receiver_context=_FULL_CTX) == [("Фон.Задача", 2, None, "background")]

    def test_intervening_call_can_overwrite_name_by_reference(self):
        lines = ['Имя = "Фон.Задача";', "ИзменитьИмя(Имя);", "ФоновыеЗадания.Выполнить(Имя);"]
        assert BI._extract_launch_edges(lines, 0, receiver_context=_FULL_CTX) == [("", 3, None, "background_dynamic")]

    def test_other_argument_call_cannot_preserve_old_variable_target(self):
        lines = ['Имя = "Фон.Задача";', "ДлительныеОперации.ВыполнитьФункцию(ИзменитьИмя(Имя), Имя);"]
        assert BI._extract_launch_edges(lines, 0, receiver_context=_FULL_CTX) == [("", 2, None, "background_dynamic")]
        local = BI._extract_launch_edges(lines, 0, receiver_context=_FULL_CTX, target_local=lambda n: True)
        assert local == [("", 2, None, "background_dynamic")]

    # Типовой идиом БСП: между присваиванием имени и запуском — вызов, которому имя НЕ передано.
    # Локальную переменную процедуры такой вызов переписать не может.
    BSP_IDIOM = [
        'Имя = "Фон.Задача";',
        "ПВ = ДлительныеОперации.ПараметрыВыполненияВФоне(УИД);",
        "ДлительныеОперации.ВыполнитьВФоне(Имя, П, ПВ);",
    ]

    def test_unrelated_call_keeps_proven_local_target(self):
        got = BI._extract_launch_edges(self.BSP_IDIOM, 0, receiver_context=_FULL_CTX, target_local=lambda n: True)
        assert got == [("Фон.Задача", 3, None, "background")]

    @pytest.mark.parametrize("target_local", [None, lambda n: False])
    def test_unrelated_call_drops_target_without_proven_locality(self, target_local):
        # параметр, Перем модуля, реквизит или недоказанный контекст: вызов может переписать имя
        got = BI._extract_launch_edges(self.BSP_IDIOM, 0, receiver_context=_FULL_CTX, target_local=target_local)
        assert got == [("", 3, None, "background_dynamic")]

    def test_locality_is_asked_about_the_target_name(self):
        asked = []

        def probe(name_cf):
            asked.append(name_cf)
            return True

        BI._extract_launch_edges(
            ['ИмяМетода = "Фон.Задача";', "Подготовить();", "ФоновыеЗадания.Выполнить(ИмяМетода);"],
            0,
            receiver_context=_FULL_CTX,
            target_local=probe,
        )
        assert set(asked) == {"имяметода"}

    @pytest.mark.parametrize(
        "between",
        [
            "ИзменитьИмя(Имя);",
            "Другое = Обработать(П, имя);",
            "Если Проверить(Имя) Тогда КонецЕсли;",
            'Выполнить("Имя = ""Фон.Вторая""");',
            "Вычислить(Выражение);",
        ],
    )
    def test_call_that_can_reach_local_variable_drops_target(self, between):
        lines = ['Имя = "Фон.Задача";', between, "ФоновыеЗадания.Выполнить(Имя);"]
        got = BI._extract_launch_edges(lines, 0, receiver_context=_FULL_CTX, target_local=lambda n: True)
        assert got == [("", 3, None, "background_dynamic")]

    @pytest.mark.parametrize(
        "between",
        [
            "Результат = Запрос.Выполнить();",
            "Если Проверить(Другое) Тогда КонецЕсли;",
            "Для Каждого Строка Из ПолучитьСписок() Цикл КонецЦикла;",
            "Структура.Вставить(ИмяКлюча, ИмяМетодаДругое);",
        ],
    )
    def test_call_that_cannot_reach_local_variable_keeps_target(self, between):
        lines = ['Имя = "Фон.Задача";', between, "ФоновыеЗадания.Выполнить(Имя);"]
        got = BI._extract_launch_edges(lines, 0, receiver_context=_FULL_CTX, target_local=lambda n: True)
        assert got == [("Фон.Задача", 3, None, "background")]

    def test_unrelated_call_in_other_argument_keeps_local_target(self):
        lines = ['Имя = "Фон.Задача";', "ДлительныеОперации.ВыполнитьВФоне(Имя, П, ДлительныеОперации.Параметры(УИД));"]
        assert BI._extract_launch_edges(lines, 0, receiver_context=_FULL_CTX) == [("", 2, None, "background_dynamic")]
        local = BI._extract_launch_edges(lines, 0, receiver_context=_FULL_CTX, target_local=lambda n: True)
        assert local == [("Фон.Задача", 2, None, "background")]

    # Самая частая форма БСП: функция возвращает результат запуска. Выражение Возврат вычисляется
    # ДО перехода, поэтому запуск в нем достижим с состоянием перед оператором.
    @pytest.mark.parametrize(
        "launch",
        [
            ["Возврат ДлительныеОперации.ВыполнитьВФоне(Имя, П, ПВ);"],
            ["Возврат", "    ДлительныеОперации.ВыполнитьВФоне(Имя, П, ПВ);"],
            ["ВызватьИсключение ФоновыеЗадания.Выполнить(Имя);"],
        ],
    )
    def test_launch_inside_jump_expression_is_reachable(self, launch):
        lines = ['Имя = "Фон.Задача";', *launch]
        got = BI._extract_launch_edges(lines, 0, receiver_context=_FULL_CTX)
        assert got == [("Фон.Задача", len(lines), None, "background")]

    def test_jump_expression_call_still_rewrites_before_launch(self):
        lines = ['Имя = "Фон.Задача";', "Возврат ИзменитьИмя(Имя) + ДлительныеОперации.ВыполнитьВФоне(Имя, П, ПВ);"]
        got = BI._extract_launch_edges(lines, 0, receiver_context=_FULL_CTX, target_local=lambda n: True)
        assert got == [("", 2, None, "background_dynamic")]

    def test_completed_jump_in_branch_does_not_reach_launch(self):
        lines = [
            'Имя = "Фон.Задача";',
            "Если Условие Тогда",
            '    Имя = "Фон.Вторая";',
            "    Возврат;",
            "КонецЕсли;",
            "ФоновыеЗадания.Выполнить(Имя);",
        ]
        assert BI._extract_launch_edges(lines, 0, receiver_context=_FULL_CTX) == [("Фон.Задача", 6, None, "background")]

    def test_elsif_branches_join_into_all_targets(self):
        lines = [
            "Если А Тогда",
            '    Имя = "Фон.Задача";',
            "ИначеЕсли Б Тогда",
            '    Имя = "Фон.Вторая";',
            "Иначе",
            '    Имя = "Фон.Вторая2";',
            "КонецЕсли;",
            "ФоновыеЗадания.Выполнить(Имя);",
        ]
        got = BI._extract_launch_edges(lines, 0, receiver_context=_FULL_CTX)
        assert sorted(r[0] for r in got) == ["Фон.Вторая", "Фон.Вторая2", "Фон.Задача"]
        assert {r[3] for r in got} == {"background"}

    @pytest.mark.parametrize(
        "lines, expected",
        [
            # присваивание до цикла, запуск в теле: значение предыдущей итерации неизвестно
            (
                [
                    'Имя = "Фон.Задача";',
                    "Для Каждого Эл Из Список Цикл",
                    "    ФоновыеЗадания.Выполнить(Имя);",
                    "КонецЦикла;",
                ],
                [("", 3, None, "background_dynamic")],
            ),
            # присваивание в теле перед запуском
            (
                [
                    "Для Каждого Эл Из Список Цикл",
                    '    Имя = "Фон.Задача";',
                    "    ФоновыеЗадания.Выполнить(Имя);",
                    "КонецЦикла;",
                ],
                [("Фон.Задача", 3, None, "background")],
            ),
            # тело переменную не трогало: после цикла — входное значение
            (
                [
                    'Имя = "Фон.Задача";',
                    "Для Каждого Эл Из Список Цикл",
                    "    Счет = Счет + 1;",
                    "КонецЦикла;",
                    "ФоновыеЗадания.Выполнить(Имя);",
                ],
                [("Фон.Задача", 5, None, "background")],
            ),
        ],
    )
    def test_loops(self, lines, expected):
        assert BI._extract_launch_edges(lines, 0, receiver_context=_FULL_CTX) == expected

    # Выражение ЗАВЕРШЕННОГО перехода вычисляется до него: вызов, получивший имя по ссылке, переписывает
    # его и для обработчика Исключение объемлющей Попытки (ВызватьИсключение с вызовом в выражении либо
    # исключение внутри вызова у Возврат).
    @pytest.mark.parametrize("target_local", [None, lambda n: True])
    @pytest.mark.parametrize(
        "jump",
        [
            "    ВызватьИсключение Переписать(Имя);",
            "    Возврат Переписать(Имя);",
            "    ВызватьИсключение Переписать(Имя)",  # последний оператор без ';' перед Исключение
            "    Raise Переписать(Имя);",
        ],
    )
    def test_completed_jump_expression_rewrite_reaches_except_handler(self, jump, target_local):
        lines = [
            'Имя = "Фон.Задача";',
            "Попытка",
            jump,
            "Исключение",
            "    ФоновыеЗадания.Выполнить(Имя);",
            "КонецПопытки;",
        ]
        got = BI._extract_launch_edges(lines, 0, receiver_context=_FULL_CTX, target_local=target_local)
        assert got == [("", 5, None, "background_dynamic")]

    def test_completed_jump_expression_follows_the_locality_rule(self):
        def lines(expr):
            return [
                'Имя = "Фон.Задача";',
                "Попытка",
                f"    ВызватьИсключение {expr};",
                "Исключение",
                "    ФоновыеЗадания.Выполнить(Имя);",
                "КонецПопытки;",
            ]

        # вызов без имени: доказанно локальную переменную не переписывает, недоказанную — может
        local = BI._extract_launch_edges(
            lines("Описание(Другое)"), 0, receiver_context=_FULL_CTX, target_local=lambda n: True
        )
        assert local == [("Фон.Задача", 5, None, "background")]
        assert BI._extract_launch_edges(lines("Описание(Другое)"), 0, receiver_context=_FULL_CTX) == [
            ("", 5, None, "background_dynamic")
        ]
        # выражение без вызова переменную не меняет
        assert BI._extract_launch_edges(lines('"ошибка"'), 0, receiver_context=_FULL_CTX) == [
            ("Фон.Задача", 5, None, "background")
        ]

    # Условие Пока вычисляется на КАЖДОЙ итерации, а тело (после позиции запуска) разбору не видно:
    # значение, присвоенное до цикла, доказывает адресат только первой проверки.
    @pytest.mark.parametrize(
        "lines",
        [
            [
                "Итерация = 0;",
                'Имя = "Фон.Задача";',
                "Пока Итерация < 2 И ФоновыеЗадания.Выполнить(Имя) <> Неопределено Цикл",
                '    Имя = "Фон.Вторая";',
                "    Итерация = Итерация + 1;",
                "КонецЦикла;",
            ],
            [
                "Iteration = 0;",
                'Name = "Фон.Задача";',
                "While Iteration < 2 And BackgroundJobs.Execute(Name) <> Undefined Do",
                '    Name = "Фон.Вторая";',
                "    Iteration = Iteration + 1;",
                "EndDo;",
            ],
        ],
    )
    def test_launch_in_while_condition_is_not_proven_by_entry_value(self, lines):
        got = BI._extract_launch_edges(lines, 0, receiver_context=_FULL_CTX, target_local=lambda n: True)
        assert got == [("", 3, None, "background_dynamic")]

    def test_literal_launch_in_while_condition_keeps_static_edge(self):
        # Правило условия Пока касается адресата через переменную: литерал от итерации не зависит.
        lines = [
            'Пока Итерация < 2 И ФоновыеЗадания.Выполнить("Фон.Задача") <> Неопределено Цикл',
            "    Итерация = Итерация + 1;",
            "КонецЦикла;",
        ]
        assert BI._extract_launch_edges(lines, 0, receiver_context=_FULL_CTX) == [("Фон.Задача", 1, None, "background")]

    def test_launch_in_for_each_collection_is_evaluated_once(self):
        lines = [
            'Имя = "Фон.Задача";',
            "Для Каждого Эл Из ДлительныеОперации.ВыполнитьФункцию(ПВ, Имя) Цикл",
            '    Имя = "Фон.Вторая";',
            "КонецЦикла;",
        ]
        assert BI._extract_launch_edges(lines, 0, receiver_context=_FULL_CTX) == [("Фон.Задача", 2, None, "background")]

    def test_except_branch_starts_from_entry_when_try_body_did_not_touch_name(self):
        lines = [
            'Имя = "Фон.Задача";',
            "Попытка",
            "    Подготовить();",
            "Исключение",
            "    ФоновыеЗадания.Выполнить(Имя);",
            "КонецПопытки;",
        ]
        assert BI._extract_launch_edges(lines, 0, receiver_context=_FULL_CTX) == [("", 5, None, "background_dynamic")]
        local = BI._extract_launch_edges(lines, 0, receiver_context=_FULL_CTX, target_local=lambda n: True)
        assert local == [("Фон.Задача", 5, None, "background")]

    @pytest.mark.parametrize("complete", [True, False])
    def test_shadowed_launcher_is_excluded_even_with_incomplete_other_context(self, complete):
        lines = ['ДлительныеОперации.ВыполнитьВФоне("Фон.Задача");']
        ctx = (frozenset({"длительныеоперации"}), complete)
        assert BI._extract_launch_edges(lines, 0, receiver_context=ctx) == []

    def test_unavailable_launcher_context_never_produces_static_edge(self):
        lines = ['ДлительныеОперации.ВыполнитьВФоне("Фон.Задача");']
        assert BI._extract_launch_edges(lines, 0, receiver_context=None) == [("", 1, None, "background_dynamic")]

    def test_dedup_preserves_both_launcher_heads_for_borrowed_context(self):
        lines = ['ДлительныеОперации.ВыполнитьВФоне("Фон.Задача"); ФоновыеЗадания.Выполнить("Фон.Задача");']
        heads = {}
        edge = ("Фон.Задача", 1, None, "background")
        assert BI._extract_launch_edges(lines, 0, receiver_context=_FULL_CTX, receiver_heads=heads) == [edge]
        assert heads[edge] == {"длительныеоперации", "фоновыезадания"}

    @pytest.mark.parametrize(
        "lines",
        [
            ['Объект. ФоновыеЗадания.Выполнить("Фон.Задача");'],
            ["Объект.", '    ФоновыеЗадания.Выполнить("Фон.Задача");'],
            ["Объект.", "", "    // пояснение", '    ДлительныеОперации.ВыполнитьВФоне("Фон.Задача");'],
        ],
    )
    def test_chain_member_launcher_is_not_a_launch(self, lines):
        assert BI._extract_launch_edges(lines, 0, receiver_context=_FULL_CTX) == []

    def test_three_segment_literal_is_manager_tier(self, launch_built):
        assert _launch_rows(launch_built, "Менеджер") == [("Док.Пересчитать", "Documents", "background", LINE_MGR)]

    @pytest.mark.parametrize("proc", ["Параметр", "Выражение", "ЧетыреСегмента"])
    def test_non_static_name_is_dynamic(self, launch_built, proc):
        assert [r[:3] for r in _launch_rows(launch_built, proc)] == [("", None, "background_dynamic")]

    def test_commented_and_string_launch_ignored(self, launch_built):
        assert _launch_rows(launch_built, "ВКомментарии") == []

    def test_mixed_literals_keep_recognized_edge(self, launch_built):
        # переменной присвоены «Фон.Задача» и четырёхсегментный адрес: ребро по распознанному
        # литералу остаётся, нераспознанный даёт одну строку с вычисляемым адресатом
        assert sorted(r[:3] for r in _launch_rows(launch_built, "Смешанная")) == [
            ("", None, "background_dynamic"),
            ("Фон.Задача", None, "background"),
        ]

    def test_legacy_bsp_launcher(self, launch_built):
        # ЗапуститьВыполнениеВФоне(ИдентификаторФормы, ИмяЭкспортнойПроцедуры, …) — имя во 2-м аргументе
        assert [r[:3] for r in _launch_rows(launch_built, "СтараяОбертка")] == [("Фон.Задача", None, "background")]

    def test_long_argument_tail_does_not_hide_literal(self, launch_built):
        assert [r[:3] for r in _launch_rows(launch_built, "ДлинныйХвост")] == [("Фон.Вторая", None, "background")]


class TestLaunchResolution:
    def test_background_edge_is_exact_caller(self, launch_built):
        db, _ = launch_built
        r = IndexReader(db)
        try:
            res = r.get_callers("Задача", FON)
            kinds = sorted((c["caller_name"], c["call_kind"]) for c in res["callers"])
            assert kinds == [
                ("Литерал", "background"),
                ("ЛитералНаСледующейСтроке", "background"),
                ("Смешанная", "background"),
                ("СтараяОбертка", "background"),
                ("ЧерезПеременную", "background"),
            ]
            assert res["_meta"]["exact_rows"] == 5 and res["_meta"]["fallback_rows"] == 0
            res2 = r.get_callers("Пересчитать", "Documents/Док/Ext/ManagerModule.bsl")
            assert [c["call_kind"] for c in res2["callers"]] == ["background"]
        finally:
            r.close()

    def test_non_exported_target_not_resolved(self, launch_built):
        # уровень общих модулей требует экспорт, ключа нет (как у прямого вызова)
        assert _launch_rows(launch_built, "НеЭкспорт") == [("Фон.Внутренняя", None, "background", LINE_NONEXPORT)]
        assert _launch_keys(launch_built, "НеЭкспорт") == [None]

    def test_launch_key_follows_common_module_rename_on_update(self, tmp_path_factory):
        files_b = {k.replace("CommonModules/Фон/", "CommonModules/Фон2/"): v for k, v in LAUNCH_FILES.items()}
        db = _assert_update_equals_build_v17(tmp_path_factory, LAUNCH_FILES, files_b)  # модуль «Фон» исчез
        assert _launch_keys((db, None), "Литерал") == [None]  # "Фон.Задача" больше некуда вести

    def test_direct_call_and_launch_on_one_line_survive_update(self, tmp_path_factory):
        # Одинаковые первые поля, call_kind None против 'background': оба ребра сохраняются.
        files_a = {FON: FON_BSL, "CommonModules/Пусть/Ext/Module.bsl": "Процедура П() Экспорт\nКонецПроцедуры\n"}
        files_b = {
            **files_a,
            "CommonModules/Пусть/Ext/Module.bsl": (
                'Процедура П() Экспорт\n    Фон.Задача(); ФоновыеЗадания.Выполнить("Фон.Задача");\nКонецПроцедуры\n'
            ),
        }
        db = _assert_update_equals_build_v17(tmp_path_factory, files_a, files_b)
        with _conn(db) as c:
            kinds = sorted(
                (r["call_kind"] or "")
                for r in c.execute("SELECT call_kind FROM calls WHERE callee_name = 'Фон.Задача'")
            )
        assert kinds == ["", "background"]


# Затенение получателя обёртки: параметр (с Знач и значением по умолчанию), локальное
# присваивание (в другом регистре), Перем модуля (английское имя), переменная цикла и
# реквизит формы.
_CF_FORM_WITH_LONG_OPS = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<Form xmlns="http://v8.1c.ru/8.3/xcf/logform">\n'
    "  <Attributes>\n"
    '    <Attribute name="Объект" id="1"><Main>true</Main></Attribute>\n'
    '    <Attribute name="ДлительныеОперации" id="2"/>\n'
    "  </Attributes>\n"
    "</Form>\n"
)
_CF_FORM_PLAIN = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<Form xmlns="http://v8.1c.ru/8.3/xcf/logform">\n'
    "  <Attributes>\n"
    '    <Attribute name="Объект" id="1"><Main>true</Main></Attribute>\n'
    "  </Attributes>\n"
    "</Form>\n"
)
_EDT_FORM_WITH_LONG_OPS = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<form:Form xmlns:form="http://g5.1c.ru/v8/dt/form">\n'
    "  <form:attributes><name>Объект</name><main>true</main></form:attributes>\n"
    "  <form:attributes><name>ДлительныеОперации</name></form:attributes>\n"
    "</form:Form>\n"
)
_EDT_FORM_PLAIN = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<form:Form xmlns:form="http://g5.1c.ru/v8/dt/form">\n'
    "  <form:attributes><name>Объект</name><main>true</main></form:attributes>\n"
    "</form:Form>\n"
)
_FORM_LAUNCH_BSL = (
    '&НаСервере\nПроцедура ЗапускИзФормы()\n    ДлительныеОперации.ВыполнитьВФоне("Фон.Задача");\nКонецПроцедуры\n'
)
TEN = "CommonModules/Тень/Ext/Module.bsl"
TEN_BSL = (
    "Перем BackgroundJobs;\n"
    "\n"
    "Процедура ЧерезПараметр(Знач ДлительныеОперации = Неопределено) Экспорт\n"
    '    ДлительныеОперации.ВыполнитьВФоне("Фон.Задача");\n'
    "КонецПроцедуры\n"
    "\n"
    "Процедура ЧерезПрисваивание() Экспорт\n"
    "    фоновыезадания = Новый Массив;\n"
    '    ФоновыеЗадания.Выполнить("Фон.Задача");\n'
    "КонецПроцедуры\n"
    "\n"
    "Процедура ЧерезПеременнуюМодуля() Экспорт\n"
    '    BackgroundJobs.Execute("Фон.Задача");\n'
    "КонецПроцедуры\n"
    "\n"
    "Процедура ЧерезЦикл(Список) Экспорт\n"
    "    Для Каждого ДлительныеОперации Из Список Цикл\n"
    '        ДлительныеОперации.ВыполнитьВФоне("Фон.Задача");\n'
    "    КонецЦикла;\n"
    "КонецПроцедуры\n"
    "\n"
    "Процедура Контроль() Экспорт\n"
    '    ДлительныеОперации.ВыполнитьВФоне("Фон.Вторая");\n'
    "КонецПроцедуры\n"
)
FORM_CF = "Documents/Д/Forms/ФормаДокумента/Ext/Form/Module.bsl"
FORM_CF_XML = "Documents/Д/Forms/ФормаДокумента/Ext/Form.xml"
FORM_EDT = "Documents/Д/Forms/ФормаДокумента/Module.bsl"
FORM_EDT_XML = "Documents/Д/Forms/ФормаДокумента/Form.form"


class TestLaunchReceiverShadowing:
    def test_shadowed_receivers_produce_no_launch(self, tmp_path):
        files = {
            FON: FON_BSL,
            TEN: TEN_BSL,
            FORM_CF: _FORM_LAUNCH_BSL,
            FORM_CF_XML: _CF_FORM_WITH_LONG_OPS,
        }
        _write_project(tmp_path, files)
        db = IndexBuilder().build(str(tmp_path), build_calls=True)
        built = (db, tmp_path)
        for proc in ("ЧерезПараметр", "ЧерезПрисваивание", "ЧерезПеременнуюМодуля", "ЧерезЦикл"):
            assert _launch_rows(built, proc, TEN) == [], proc
        assert [r[:3] for r in _launch_rows(built, "Контроль", TEN)] == [("Фон.Вторая", None, "background")]
        assert _launch_rows(built, "ЗапускИзФормы", FORM_CF) == []

    def test_form_without_long_ops_attribute_launches(self, tmp_path):
        _write_project(tmp_path, {FON: FON_BSL, FORM_CF: _FORM_LAUNCH_BSL, FORM_CF_XML: _CF_FORM_PLAIN})
        db = IndexBuilder().build(str(tmp_path), build_calls=True)
        assert [r[:3] for r in _launch_rows((db, tmp_path), "ЗапускИзФормы", FORM_CF)] == [
            ("Фон.Задача", None, "background")
        ]

    def test_multiline_module_variable_shadows_receiver(self, tmp_path):
        module = "Перем А,\n\tДлительныеОперации;\n\n" + _FORM_LAUNCH_BSL
        _write_project(tmp_path, {FON: FON_BSL, FORM_CF: module, FORM_CF_XML: _CF_FORM_PLAIN})
        db = IndexBuilder().build(str(tmp_path), build_calls=True)
        assert _launch_rows((db, tmp_path), "ЗапускИзФормы", FORM_CF) == []

    @pytest.mark.parametrize("descriptor", [None, "<Form><broken"])
    def test_unavailable_form_descriptor_gives_dynamic_only(self, tmp_path, descriptor):
        files = {FON: FON_BSL, FORM_CF: _FORM_LAUNCH_BSL}
        if descriptor is not None:
            files[FORM_CF_XML] = descriptor
        _write_project(tmp_path, files)
        db = IndexBuilder().build(str(tmp_path), build_calls=True)
        built = (db, tmp_path)
        assert [r[:3] for r in _launch_rows(built, "ЗапускИзФормы", FORM_CF)] == [("", None, "background_dynamic")]
        assert _launch_keys(built, "ЗапускИзФормы", FORM_CF) == [None]

    def test_call_passing_name_by_reference_drops_old_target(self, tmp_path):
        imena = "CommonModules/Имена/Ext/Module.bsl"
        body = (
            'Процедура ИзменитьИмя(Имя) Экспорт\n    Имя = "Фон.Вторая";\nКонецПроцедуры\n\n'
            'Процедура Запуск() Экспорт\n    Имя = "Фон.Задача";\n    ИзменитьИмя(Имя);\n'
            "    ФоновыеЗадания.Выполнить(Имя);\nКонецПроцедуры\n\n"
            'Процедура Перезапись() Экспорт\n    Имя = "Фон.Задача";\n'
            '    Имя = "Фон.Вторая"; ФоновыеЗадания.Выполнить(Имя);\nКонецПроцедуры\n\n'
            'Процедура КонтрольныйЗапуск() Экспорт\n    ФоновыеЗадания.Выполнить("Фон.Задача");\nКонецПроцедуры\n'
        )
        _write_project(tmp_path, {FON: FON_BSL, imena: body})
        db = IndexBuilder().build(str(tmp_path), build_calls=True)
        built = (db, tmp_path)
        assert [r[:3] for r in _launch_rows(built, "Запуск", imena)] == [("", None, "background_dynamic")]
        assert [r[:3] for r in _launch_rows(built, "Перезапись", imena)] == [("Фон.Вторая", None, "background")]
        assert _launch_keys(built, "Перезапись", imena) == [_make_callee_key(FON, "Вторая")]
        assert _launch_keys(built, "КонтрольныйЗапуск", imena) == [_make_callee_key(FON, "Задача")]


def _desc_files(fmt, shadowed):
    if fmt == "cf":
        return {
            FON: FON_BSL,
            FORM_CF: _FORM_LAUNCH_BSL,
            FORM_CF_XML: _CF_FORM_WITH_LONG_OPS if shadowed else _CF_FORM_PLAIN,
        }
    return {
        "CommonModules/Фон/Module.bsl": FON_BSL,
        FORM_EDT: _FORM_LAUNCH_BSL,
        FORM_EDT_XML: _EDT_FORM_WITH_LONG_OPS if shadowed else _EDT_FORM_PLAIN,
    }


class TestLaunchDescriptorDependence:
    """Меняется только описатель формы, BSL нетронут: update обязан совпасть со сборкой."""

    @pytest.mark.parametrize("fmt", ["cf", "edt"])
    @pytest.mark.parametrize("build_metadata", [True, False])
    def test_full_scan_update_follows_form_attribute(self, tmp_path_factory, fmt, build_metadata):
        mod, xml = (FORM_CF, FORM_CF_XML) if fmt == "cf" else (FORM_EDT, FORM_EDT_XML)
        write = WRITERS[fmt]
        files_a, files_b = _desc_files(fmt, False), _desc_files(fmt, True)
        da = tmp_path_factory.mktemp("desc_upd")
        write(da, files_a)
        db = IndexBuilder().build(str(da), build_calls=True, build_metadata=build_metadata)
        assert [r[2] for r in _launch_rows((db, da), "ЗапускИзФормы", mod)] == ["background"]
        for old, new, expected in ((files_a, files_b, []), (files_b, files_a, ["background"])):
            _apply_changes(da, old, new)
            p = da / xml
            st = p.stat()
            os.utime(p, (st.st_atime + 5, st.st_mtime + 5))
            IndexBuilder().update(str(da))
            dfresh = tmp_path_factory.mktemp("desc_fresh")
            write(dfresh, new)
            fresh = IndexBuilder().build(str(dfresh), build_calls=True, build_metadata=build_metadata)
            assert _edge_tuples_v17(db) == _edge_tuples_v17(fresh)
            assert [r[2] for r in _launch_rows((db, da), "ЗапускИзФормы", mod)] == expected

    @pytest.mark.skipif(not shutil.which("git"), reason="git недоступен")
    @pytest.mark.parametrize("fmt", ["cf", "edt"])
    def test_git_fast_path_update_follows_form_attribute(self, tmp_path, tmp_path_factory, fmt):
        from test_git_delta import _git, _git_init

        mod = FORM_CF if fmt == "cf" else FORM_EDT
        write = WRITERS[fmt]
        root = tmp_path / "repo"
        base = root / "src"
        files_a, files_b = _desc_files(fmt, False), _desc_files(fmt, True)
        write(base, files_a)
        _git_init(root)
        db = IndexBuilder().build(str(base), build_calls=True)
        for old, new, expected in ((files_a, files_b, []), (files_b, files_a, ["background"])):
            _apply_changes(base, old, new)
            _git(root, "add", "-A")
            _git(root, "commit", "-m", "descriptor only")
            res = IndexBuilder().update(str(base))
            assert res["git_fast_path"] is True, res
            dfresh = tmp_path_factory.mktemp("desc_git_fresh")
            write(dfresh, new)
            assert _edge_tuples_v17(db) == _edge_tuples_v17(IndexBuilder().build(str(dfresh), build_calls=True))
            assert [r[2] for r in _launch_rows((db, base), "ЗапускИзФормы", mod)] == expected


# Адресат запуска через локальную переменную и промежуточный вызов (идиом БСП): вызов, которому
# имя не передано, локальную переменную переписать не может. Параметр, Перем модуля и имя
# контекста модуля (реквизит формы/объекта, ТЧ) — не локальны: прежнее консервативное правило.
LOKALNOE = "CommonModules/Локальное/Ext/Module.bsl"
LOKALNOE_BSL = (
    "Процедура ЗапускБСП(УИД) Экспорт\n"
    '    ИмяМетода = "Фон.Задача";\n'
    "    ПараметрыВыполнения = ДлительныеОперации.ПараметрыВыполненияВФоне(УИД);\n"
    "    ДлительныеОперации.ВыполнитьВФоне(ИмяМетода, Неопределено, ПараметрыВыполнения);\n"
    "КонецПроцедуры\n"
    "\n"
    "Процедура ЧерезПараметр(ИмяМетода, УИД) Экспорт\n"
    '    ИмяМетода = "Фон.Задача";\n'
    "    ПараметрыВыполнения = ДлительныеОперации.ПараметрыВыполненияВФоне(УИД);\n"
    "    ДлительныеОперации.ВыполнитьВФоне(ИмяМетода, Неопределено, ПараметрыВыполнения);\n"
    "КонецПроцедуры\n"
    "\n"
    "Процедура ПередачаВВызов() Экспорт\n"
    '    ИмяМетода = "Фон.Задача";\n'
    "    Подготовить(ИмяМетода);\n"
    "    ДлительныеОперации.ВыполнитьВФоне(ИмяМетода);\n"
    "КонецПроцедуры\n"
)
_FORM_IDIOM_BSL = (
    "&НаСервере\nПроцедура ЗапускИзФормы()\n"
    '    ИмяМетода = "Фон.Задача";\n'
    "    ПараметрыВыполнения = ДлительныеОперации.ПараметрыВыполненияВФоне(УникальныйИдентификатор);\n"
    "    ДлительныеОперации.ВыполнитьВФоне(ИмяМетода, Неопределено, ПараметрыВыполнения);\n"
    "КонецПроцедуры\n"
)
_EDT_FORM_WITH_METHOD_NAME = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<form:Form xmlns:form="http://g5.1c.ru/v8/dt/form">\n'
    "  <form:attributes><name>Объект</name><main>true</main></form:attributes>\n"
    "  <form:attributes><name>ИмяМетода</name></form:attributes>\n"
    "</form:Form>\n"
)


def _locality_files(fmt, attr):
    """Форма с идиомом БСП; ``attr`` — у формы есть реквизит ``ИмяМетода``."""
    if fmt == "cf":
        return {
            FON: FON_BSL,
            FORM_CF: _FORM_IDIOM_BSL,
            FORM_CF_XML: _cf_form_xml("ИмяМетода") if attr else _cf_form_xml(),
        }
    return {
        "CommonModules/Фон/Module.bsl": FON_BSL,
        FORM_EDT: _FORM_IDIOM_BSL,
        FORM_EDT_XML: _EDT_FORM_WITH_METHOD_NAME if attr else _EDT_FORM_PLAIN,
    }


class TestLaunchTargetLocality:
    def test_common_module_idiom_is_exact_edge(self, tmp_path):
        _write_project(tmp_path, {FON: FON_BSL, LOKALNOE: LOKALNOE_BSL})
        built = (IndexBuilder().build(str(tmp_path), build_calls=True), tmp_path)
        assert [r[:3] for r in _launch_rows(built, "ЗапускБСП", LOKALNOE)] == [("Фон.Задача", None, "background")]
        assert _launch_keys(built, "ЗапускБСП", LOKALNOE) == [_make_callee_key(FON, "Задача")]
        for proc in ("ЧерезПараметр", "ПередачаВВызов"):
            assert [r[:3] for r in _launch_rows(built, proc, LOKALNOE)] == [("", None, "background_dynamic")], proc

    @pytest.mark.parametrize("fmt", ["cf", "edt"])
    @pytest.mark.parametrize("attr, expected", [(False, "background"), (True, "background_dynamic")])
    def test_form_attribute_named_like_target_is_not_local(self, tmp_path, fmt, attr, expected):
        mod = FORM_CF if fmt == "cf" else FORM_EDT
        WRITERS[fmt](tmp_path, _locality_files(fmt, attr))
        db = IndexBuilder().build(str(tmp_path), build_calls=True)
        assert [r[2] for r in _launch_rows((db, tmp_path), "ЗапускИзФормы", mod)] == [expected]

    def test_module_variable_is_not_local(self, tmp_path):
        files = _locality_files("cf", False)
        files[FORM_CF] = "Перем ИмяМетода;\n\n" + _FORM_IDIOM_BSL
        _write_project(tmp_path, files)
        db = IndexBuilder().build(str(tmp_path), build_calls=True)
        assert [r[2] for r in _launch_rows((db, tmp_path), "ЗапускИзФормы", FORM_CF)] == ["background_dynamic"]

    def test_multiline_module_variable_is_not_local(self, tmp_path):
        # Объявление на нескольких строках: имя со второй строки — тоже переменная модуля, и
        # процедура модуля, вызванная между присваиванием и запуском, может ее переписать.
        files = _locality_files("cf", False)
        files[FORM_CF] = (
            "Перем ДругаяПеременная,\n"
            "\tИмяМетода;\n\n"
            '&НаСервере\nПроцедура Установить()\n\tИмяМетода = "Фон.Вторая";\nКонецПроцедуры\n\n'
            "&НаСервере\nПроцедура ЗапускИзФормы()\n"
            '\tИмяМетода = "Фон.Задача";\n'
            "\tУстановить();\n"
            "\tДлительныеОперации.ВыполнитьВФоне(ИмяМетода);\n"
            "КонецПроцедуры\n"
        )
        _write_project(tmp_path, files)
        db = IndexBuilder().build(str(tmp_path), build_calls=True)
        assert [r[2] for r in _launch_rows((db, tmp_path), "ЗапускИзФормы", FORM_CF)] == ["background_dynamic"]

    def test_multiline_variable_declarations_are_collected(self):
        module = ["Перем А Экспорт,", "\tБ,", "\tВ;", "", "Процедура П()", "\tПерем Г,", "\t\tД;", "КонецПроцедуры"]
        masked = BI.mask_comments_and_strings(module)
        procs = BI._parse_procedures_from_lines(module, masked=masked)
        assert BI._module_var_names(masked, procs) == {"а", "б", "в"}
        assert {"г", "д"} <= BI._procedure_scope_names(masked, procs[0])

    def test_assignment_with_line_break_before_equals_is_in_scope(self):
        # Оператор продолжается через перевод строки (и комментарий) до «=»: это присваивание.
        module = [
            "Процедура П()",
            "    ОМ",
            "        = Новый Структура;",
            "    ДлительныеОперации // пояснение",
            "        = Новый Структура;",
            "КонецПроцедуры",
        ]
        masked = BI.mask_comments_and_strings(module)
        procs = BI._parse_procedures_from_lines(module, masked=masked)
        assert {"ом", "длительныеоперации"} <= BI._procedure_scope_names(masked, procs[0])

    def test_receiver_assigned_with_line_break_before_equals_is_shadowed(self, tmp_path):
        mod = "CommonModules/Перенос/Ext/Module.bsl"
        body = (
            "Процедура Вызовы() Экспорт\n"
            "    ДлительныеОперации\n"
            "        = Новый Структура;\n"
            '    ДлительныеОперации.ВыполнитьВФоне("Фон.Задача");\n'
            "КонецПроцедуры\n"
        )
        _write_project(tmp_path, {FON: FON_BSL, mod: body})
        db = IndexBuilder().build(str(tmp_path), build_calls=True)
        assert _launch_rows((db, tmp_path), "Вызовы", mod) == []

    def test_object_tabular_section_named_like_target_is_not_local(self, tmp_path):
        obj_mod = "Documents/Д/Ext/ObjectModule.bsl"
        body = _FORM_IDIOM_BSL.replace("&НаСервере\n", "")
        for ts, expected in (((), "background"), (("ИмяМетода",), "background_dynamic")):
            root = tmp_path / ("ts" if ts else "plain")
            _write_project(root, {FON: FON_BSL, obj_mod: body, "Documents/Д.xml": _cf_object_xml("Document", "Д", ts)})
            db = IndexBuilder().build(str(root), build_calls=True)
            assert [r[2] for r in _launch_rows((db, root), "ЗапускИзФормы", obj_mod)] == [expected], ts

    @pytest.mark.parametrize("fmt", ["cf", "edt"])
    def test_full_scan_update_follows_attribute_named_like_target(self, tmp_path_factory, fmt):
        mod, xml = (FORM_CF, FORM_CF_XML) if fmt == "cf" else (FORM_EDT, FORM_EDT_XML)
        write = WRITERS[fmt]
        files_a, files_b = _locality_files(fmt, False), _locality_files(fmt, True)
        da = tmp_path_factory.mktemp("loc_upd")
        write(da, files_a)
        db = IndexBuilder().build(str(da), build_calls=True)
        for old, new, expected in ((files_a, files_b, "background_dynamic"), (files_b, files_a, "background")):
            _apply_changes(da, old, new)
            p = da / xml
            st = p.stat()
            os.utime(p, (st.st_atime + 5, st.st_mtime + 5))
            IndexBuilder().update(str(da))
            dfresh = tmp_path_factory.mktemp("loc_fresh")
            write(dfresh, new)
            assert _edge_tuples_v17(db) == _edge_tuples_v17(IndexBuilder().build(str(dfresh), build_calls=True))
            assert [r[2] for r in _launch_rows((db, da), "ЗапускИзФормы", mod)] == [expected]

    def test_multiline_signature_tail_is_not_a_statement(self, tmp_path):
        # Хвост сигнатуры без ';' не склеивается с первым оператором тела.
        mod = "CommonModules/Многострочная/Ext/Module.bsl"
        body = (
            "Функция Начать(Ключ,\n"
            "\tИдентификаторФормы = Неопределено) Экспорт\n"
            '\tИмяФункции = "Фон.Задача";\n'
            "\tПВ = ДлительныеОперации.ПараметрыВыполненияФункции(ИдентификаторФормы);\n"
            "\tВозврат ДлительныеОперации.ВыполнитьФункцию(ПВ, ИмяФункции, Ключ);\n"
            "КонецФункции\n"
        )
        _write_project(tmp_path, {FON: FON_BSL, mod: body})
        built = (IndexBuilder().build(str(tmp_path), build_calls=True), tmp_path)
        assert _launch_keys(built, "Начать", mod) == [_make_callee_key(FON, "Задача")]

    def test_full_scan_update_with_multiline_signature_equals_build(self, tmp_path_factory):
        files_a = _locality_files("cf", False)
        files_a[FORM_CF] = _FORM_IDIOM_BSL.replace(
            "Процедура ЗапускИзФормы()", "Процедура ЗапускИзФормы(\n\tКлюч = Неопределено)"
        )
        files_b = {**files_a, FORM_CF_XML: _cf_form_xml("ИмяМетода")}
        da = tmp_path_factory.mktemp("sig_upd")
        _write_project(da, files_a)
        db = IndexBuilder().build(str(da), build_calls=True)
        assert [r[2] for r in _launch_rows((db, da), "ЗапускИзФормы", FORM_CF)] == ["background"]
        for old, new, expected in ((files_a, files_b, "background_dynamic"), (files_b, files_a, "background")):
            _apply_changes(da, old, new)
            _bump(da / FORM_CF_XML)
            IndexBuilder().update(str(da))
            dfresh = tmp_path_factory.mktemp("sig_fresh")
            _write_project(dfresh, new)
            assert _edge_tuples_v17(db) == _edge_tuples_v17(IndexBuilder().build(str(dfresh), build_calls=True))
            assert [r[2] for r in _launch_rows((db, da), "ЗапускИзФормы", FORM_CF)] == [expected]

    def test_audit_classifies_variable_target_like_the_builder(self, tmp_path):
        main = {
            **AUDIT_MAIN,
            "CommonModules/Проба/Ext/Module.bsl": (
                "Процедура П(УИД) Экспорт\n"
                '    ИмяМетода = "Обработка.Х.МодульОбъекта.М";\n'
                "    ПВ = ДлительныеОперации.ПараметрыВыполненияВФоне(УИД);\n"
                "    ДлительныеОперации.ВыполнитьВФоне(ИмяМетода, Неопределено, ПВ);\n"
                "КонецПроцедуры\n\n"
                "Процедура Н(УИД) Экспорт\n"
                '    ИмяМетода = "НетМодуля.Метод";\n'
                "    ПВ = ДлительныеОперации.ПараметрыВыполненияВФоне(УИД);\n"
                "    ДлительныеОперации.ВыполнитьВФоне(ИмяМетода, Неопределено, ПВ);\n"
                "КонецПроцедуры\n\n"
                # хвост многострочной сигнатуры: повторный разбор аудита гасит его, как сборщик
                "Функция С(Ключ,\n"
                "    УИД = Неопределено) Экспорт\n"
                '    ИмяМетода = "Обработка.Х.МодульОбъекта.М";\n'
                "    Возврат ДлительныеОперации.ВыполнитьВФоне(ИмяМетода, Ключ, УИД);\n"
                "КонецФункции\n"
            ),
        }
        bsl, reader, _cf = _audit_session(tmp_path, main)
        try:
            res = bsl["find_unresolved_calls"](layer="main", path="CommonModules/Проба")
            got = sorted(
                (i["caller"], i["reason"], i["expression"] if i["reason"] == "unsupported_name_form" else i["target"])
                for i in res["issues"]
            )
            assert got == [
                ("Н", "module_missing", "НетМодуля.Метод"),
                ("П", "unsupported_name_form", "Обработка.Х.МодульОбъекта.М"),
                ("С", "unsupported_name_form", "Обработка.Х.МодульОбъекта.М"),
            ]
            assert res["not_checked"]["dynamic_name"] == 0
        finally:
            reader.close()

    def test_extension_layer_keeps_conservative_rule_in_graph_and_audit(self, tmp_path):
        # Контекст заимствованного объекта наследуется от основной конфигурации — живой слой
        # локальность имени не доказывает: и граф, и аудит видят вычисляемое имя.
        ext1 = {
            **AUDIT_EXT1,
            "CommonModules/Расш1_Запуск/Ext/Module.bsl": (
                "Процедура ЗапускБСП(УИД) Экспорт\n"
                '    ИмяМетода = "ОМ.Есть";\n'
                "    ПВ = ДлительныеОперации.ПараметрыВыполненияВФоне(УИД);\n"
                "    ДлительныеОперации.ВыполнитьВФоне(ИмяМетода, Неопределено, ПВ);\n"
                "КонецПроцедуры\n"
            ),
        }
        bsl, reader, _cf = _audit_session(tmp_path, AUDIT_MAIN, ext1, None)
        try:
            res = bsl["find_callers_context"]("Есть", "CommonModules/ОМ/Ext/Module.bsl")
            assert [c for c in res["callers"] if c["file"].endswith("Расш1_Запуск/Ext/Module.bsl")] == []
            dyn = bsl["find_unresolved_calls"](layer="extensions", reasons=["dynamic_name"])
            assert [(i["file"].rsplit("/", 3)[-3], i["reason"]) for i in dyn["issues"]] == [
                ("Расш1_Запуск", "dynamic_name")
            ]
        finally:
            reader.close()


def test_extension_multiline_signature_launch_is_edge_in_graph_and_audit(tmp_path):
    # Живой проход по расширениям и повторный разбор аудита гасят хвост сигнатуры одинаково.
    ext1 = {
        **AUDIT_EXT1,
        "CommonModules/Расш1_Запуск/Ext/Module.bsl": (
            "Функция ЗапускБСП(Ключ,\n"
            "\tУИД = Неопределено) Экспорт\n"
            '\tИмяМетода = "ОМ.Есть";\n'
            "\tВозврат ДлительныеОперации.ВыполнитьВФоне(ИмяМетода, Неопределено, УИД);\n"
            "КонецФункции\n"
        ),
    }
    bsl, reader, _cf = _audit_session(tmp_path, AUDIT_MAIN, ext1, None)
    try:
        res = bsl["find_callers_context"]("Есть", "CommonModules/ОМ/Ext/Module.bsl")
        ext_rows = [c for c in res["callers"] if c["file"].endswith("Расш1_Запуск/Ext/Module.bsl")]
        assert [(c["caller_name"], c["call_kind"]) for c in ext_rows] == [("ЗапускБСП", "background")]
        dyn = bsl["find_unresolved_calls"](layer="extensions", reasons=["dynamic_name"])
        assert [i for i in dyn["issues"] if "Расш1_Запуск" in i["file"]] == []
    finally:
        reader.close()


PEREHOD = "CommonModules/Переход/Ext/Module.bsl"
_REWRITE_FN = 'Функция Переписать(Имя)\n    Имя = "Фон.Вторая";\n    Возврат "ошибка";\nКонецФункции\n'


class TestLaunchFlowGraphAndAudit:
    """Сборка и публичные хелперы: адресат, доказанный только для части путей, — не точное ребро."""

    def _check(self, tmp_path, start_body):
        body = f"Процедура Старт() Экспорт\n{start_body}КонецПроцедуры\n\n{_REWRITE_FN}"
        bsl, reader, _cf = _audit_session(tmp_path, {FON: FON_BSL, PEREHOD: body}, None, None)
        try:
            assert [r["kind"] for r in reader.get_launch_calls()] == ["background_dynamic"]
            p = bsl["find_path"]("Старт", "Задача", from_hint=PEREHOD, to_hint=FON)
            assert p["found"] is False and "error" not in p
            res = bsl["find_unresolved_calls"](layer="main", path=PEREHOD, reasons=["dynamic_name"])
            assert res["not_checked"]["dynamic_name"] == 1 and res["partial"] is False
            assert [(i["caller"], i["reason"]) for i in res["issues"]] == [("Старт", "dynamic_name")]
        finally:
            reader.close()

    def test_rewrite_in_raise_expression_reaches_except_handler(self, tmp_path):
        self._check(
            tmp_path,
            '    Имя = "Фон.Задача";\n'
            "    Попытка\n"
            "        ВызватьИсключение Переписать(Имя);\n"
            "    Исключение\n"
            "        ФоновыеЗадания.Выполнить(Имя);\n"
            "    КонецПопытки;\n",
        )

    def test_launch_in_while_condition_sees_later_iterations(self, tmp_path):
        self._check(
            tmp_path,
            "    Итерация = 0;\n"
            '    Имя = "Фон.Задача";\n'
            "    Пока Итерация < 2 И ФоновыеЗадания.Выполнить(Имя) <> Неопределено Цикл\n"
            '        Имя = "Фон.Вторая";\n'
            "        Итерация = Итерация + 1;\n"
            "    КонецЦикла;\n",
        )


def _scope_files(fmt):
    """Форма с запуском (маркер), форма без маркера, модуль объекта с запуском."""
    launch = '\nПроцедура Запуск() Экспорт\n    ДлительныеОперации.ВыполнитьВФоне("Фон.Задача");\nКонецПроцедуры\n'
    plain_form = "&НаКлиенте\nПроцедура Команда()\nКонецПроцедуры\n"
    if fmt == "cf":
        return {
            FON: FON_BSL,
            FORM_CF: _FORM_LAUNCH_BSL,
            FORM_CF_XML: _CF_FORM_PLAIN,
            "Documents/Д/Forms/ФормаСписка/Ext/Form/Module.bsl": plain_form,
            "Documents/Д/Forms/ФормаСписка/Ext/Form.xml": _CF_FORM_PLAIN,
            "Documents/Д/Ext/ObjectModule.bsl": launch,
            "Documents/Д.xml": _cf_object_xml("Document", "Д"),
        }
    return {
        "CommonModules/Фон/Module.bsl": FON_BSL,
        FORM_EDT: _FORM_LAUNCH_BSL,
        FORM_EDT_XML: _EDT_FORM_PLAIN,
        "Documents/Д/Forms/ФормаСписка/Module.bsl": plain_form,
        "Documents/Д/Forms/ФормаСписка/Form.form": _EDT_FORM_PLAIN,
        "Documents/Д/ObjectModule.bsl": launch,
        "Documents/Д/Д.mdo": _edt_mdo("Document", "Д"),
    }


def _spy_refresh(monkeypatch):
    seen: set[str] = set()
    orig = BI._refresh_launch_rows

    def spy(conn, base_path, rel_paths):
        seen.update(rel_paths)
        return orig(conn, base_path, rel_paths)

    monkeypatch.setattr(BI, "_refresh_launch_rows", spy)
    return seen


def _bump(path):
    st = path.stat()
    os.utime(path, (st.st_atime + 5, st.st_mtime + 5))


class TestLaunchRefreshScope:
    """Полный скан update перечитывает фоновые рёбра только у модулей с маркером запуска и
    только при изменении описателя (снимок ``file_paths``); ``Form.form`` EDT снимка не имеет."""

    @pytest.mark.parametrize(
        "fmt, expected",
        [("cf", set()), ("edt", {FORM_EDT})],
    )
    def test_noop_full_scan_rechecks_only_untracked_marker_forms(self, tmp_path, monkeypatch, fmt, expected):
        WRITERS[fmt](tmp_path, _scope_files(fmt))
        IndexBuilder().build(str(tmp_path), build_calls=True)
        seen = _spy_refresh(monkeypatch)
        IndexBuilder().update(str(tmp_path))
        assert seen == expected

    @pytest.mark.parametrize("fmt", ["cf", "edt"])
    def test_object_descriptor_change_rechecks_its_marker_modules(self, tmp_path, monkeypatch, fmt):
        files = _scope_files(fmt)
        WRITERS[fmt](tmp_path, files)
        IndexBuilder().build(str(tmp_path), build_calls=True)
        if fmt == "cf":
            obj_mod, desc, new = (
                "Documents/Д/Ext/ObjectModule.bsl",
                "Documents/Д.xml",
                _cf_object_xml("Document", "Д", ["ТЧ"]),
            )
        else:
            obj_mod, desc = "Documents/Д/ObjectModule.bsl", "Documents/Д/Д.mdo"
            new = _edt_mdo("Document", "Д", "  <tabularSections><name>ТЧ</name></tabularSections>\n")
        (tmp_path / desc).write_text(new, encoding="utf-8-sig")
        _bump(tmp_path / desc)
        seen = _spy_refresh(monkeypatch)
        IndexBuilder().update(str(tmp_path))
        untracked = {FORM_EDT} if fmt == "edt" else set()
        assert seen == {obj_mod} | untracked

    @pytest.mark.parametrize("fmt", ["cf", "edt"])
    def test_launch_marker_column_update_equals_build(self, tmp_path_factory, fmt):
        files_a = _scope_files(fmt)
        plain_rel = next(r for r in files_a if "ФормаСписка" in r and r.endswith("Module.bsl"))
        files_b = {**files_a, plain_rel: files_a[plain_rel] + _FORM_LAUNCH_BSL}
        da = tmp_path_factory.mktemp("marker_upd")
        WRITERS[fmt](da, files_a)
        db = IndexBuilder().build(str(da), build_calls=True)

        def markers(path):
            with _conn(path) as c:
                return dict(c.execute("SELECT rel_path, launch_marker FROM modules").fetchall())

        before = markers(db)
        assert before[plain_rel] == 0 and before[FORM_CF if fmt == "cf" else FORM_EDT] == 1
        _apply_changes(da, files_a, files_b)
        _bump(da / plain_rel)
        IndexBuilder().update(str(da))
        dfresh = tmp_path_factory.mktemp("marker_fresh")
        WRITERS[fmt](dfresh, files_b)
        fresh = IndexBuilder().build(str(dfresh), build_calls=True)
        assert markers(db) == markers(fresh)
        assert markers(db)[plain_rel] == 1

    @pytest.mark.parametrize("fmt", ["cf", "edt"])
    def test_full_scan_descriptor_update_refreshes_call_stats(self, tmp_path, fmt):
        xml = FORM_CF_XML if fmt == "cf" else FORM_EDT_XML
        files_a, files_b = _desc_files(fmt, False), _desc_files(fmt, True)
        WRITERS[fmt](tmp_path, files_a)
        db = IndexBuilder().build(str(tmp_path), build_calls=True)
        _apply_changes(tmp_path, files_a, files_b)
        _bump(tmp_path / xml)
        IndexBuilder().update(str(tmp_path))
        with _conn(db) as c:
            meta = dict(c.execute("SELECT key, value FROM index_meta").fetchall())
            total, resolved = c.execute("SELECT COUNT(*), COUNT(callee_key) FROM calls").fetchone()
        assert (meta["calls_total"], meta["calls_resolved"]) == (str(total), str(resolved))

    @pytest.mark.skipif(not shutil.which("git"), reason="git недоступен")
    def test_git_fast_path_descriptor_update_refreshes_call_stats(self, tmp_path):
        from test_git_delta import _git, _git_init

        root = tmp_path / "repo"
        base = root / "src"
        files_a, files_b = _desc_files("cf", False), _desc_files("cf", True)
        _write_project(base, files_a)
        _git_init(root)
        db = IndexBuilder().build(str(base), build_calls=True)
        _apply_changes(base, files_a, files_b)
        _git(root, "add", "-A")
        _git(root, "commit", "-m", "descriptor only")
        assert IndexBuilder().update(str(base))["git_fast_path"] is True
        with _conn(db) as c:
            meta = dict(c.execute("SELECT key, value FROM index_meta").fetchall())
            total, resolved = c.execute("SELECT COUNT(*), COUNT(callee_key) FROM calls").fetchone()
        assert (meta["calls_total"], meta["calls_resolved"]) == (str(total), str(resolved))


# ── Задача 4: выдача графа — call_kind и вычисляемые имена ────────────────────


def _make_bsl_for(base, db_path=None):
    """Хелперы над проектом; с ридером — как у сервера на свежем индексе (ноль авторитетен)."""
    from rlm_tools_bsl.bsl_helpers import make_bsl_helpers
    from rlm_tools_bsl.format_detector import detect_format
    from rlm_tools_bsl.helpers import make_helpers

    reader = IndexReader(db_path) if db_path is not None else None
    helpers, resolve_safe = make_helpers(str(base))
    bsl = make_bsl_helpers(
        base_path=str(base),
        resolve_safe=resolve_safe,
        read_file_fn=helpers["read_file"],
        grep_fn=helpers["grep"],
        glob_files_fn=helpers["glob_files"],
        format_info=detect_format(str(base)),
        idx_reader=reader,
        idx_zero_callers_authoritative=reader is not None,
    )
    return bsl, reader


@pytest.fixture
def launch_helpers(launch_built):
    db, base = launch_built
    bsl, reader = _make_bsl_for(base, db)
    yield bsl
    reader.close()


@pytest.fixture
def launch_fs_helpers(tmp_path):
    _write_project(tmp_path, LAUNCH_FILES)
    bsl, _ = _make_bsl_for(tmp_path)
    return bsl


@pytest.fixture
def mgr_helpers(mgr_built):
    db, base = mgr_built
    bsl, reader = _make_bsl_for(base, db)
    yield bsl
    reader.close()


def _plan_blob(conn, sql, params):
    rows = conn.execute(f"EXPLAIN QUERY PLAN {sql}", params).fetchall()
    return " ".join(r["detail"] for r in rows)


class TestGraphOutput:
    def test_find_callers_context_rows_carry_call_kind_on_both_routes(self, launch_helpers, launch_fs_helpers):
        res = launch_helpers["find_callers_context"]("Задача", FON)
        assert {c["call_kind"] for c in res["callers"]} == {"background"}
        mixed = launch_helpers["find_callers_context"]("Вторая", FON)
        assert sorted((c["caller_name"], c["call_kind"]) for c in mixed["callers"]) == [
            ("ДлинныйХвост", "background"),
            ("ПереопределениеПослеЗапуска", "background"),
            ("ПрямойВызов", "call"),
            ("ЧерезПеременную", "background"),
        ]
        # Без индекса запуски не видны (объявленная граница), прямой вызов — с call_kind='call'
        fs = launch_fs_helpers["find_callers_context"]("Вторая", "Фон")
        assert [(c["caller_name"], c["call_kind"]) for c in fs["callers"]] == [("ПрямойВызов", "call")]

    def test_hierarchy_and_path_carry_call_kind(self, launch_helpers):
        tree = launch_helpers["find_call_hierarchy"]("Задача", depth=1, module_hint=FON)
        assert {c["call_kind"] for c in tree["tree"][0]["callers"]} == {"background"}
        p = launch_helpers["find_path"]("Литерал", "Задача", from_hint=ZAPUSKATEL, to_hint=FON)
        assert p["found"] is True
        assert [e["call_kind"] for e in p["path"]] == ["background", None]
        assert p["_meta"]["precision"] == "exact"

    def test_zero_callers_reports_unresolved_launches(self, launch_helpers):
        res = launch_helpers["find_callers_context"]("Вторая2", FON)  # экспорт без вызовов
        assert res["_meta"]["total_callers"] == 0
        # ПрисвоениеПослеЗапуска, Параметр, Выражение, ЧетыреСегмента и нераспознанная ветвь Смешанной
        assert res["_meta"]["unresolved_launches"] == 5
        assert "find_unresolved_calls" in res["_meta"]["hint"]

    def test_unresolved_launch_count_uses_partial_index(self, launch_built):
        db, _ = launch_built
        with _conn(db) as c:
            blob = _plan_blob(
                c,
                "SELECT COUNT(*) FROM calls WHERE call_kind IN ('background', 'background_dynamic') "
                "AND call_kind = 'background_dynamic'",
                [],
            )
        assert "idx_calls_by_name" in blob, blob

    def test_default_output_otherwise_unchanged(self, mgr_helpers):
        # ключи строки: прежние + call_kind, ничего больше
        res = mgr_helpers["find_callers_context"]("НайтиПоАртикулу", "Catalogs/Номенклатура/Ext/ManagerModule.bsl")
        assert set(res["callers"][0]) == {
            "file",
            "caller_name",
            "caller_is_export",
            "line",
            "object_name",
            "category",
            "module_type",
            "edge_exact",
            "call_kind",
        }

    def test_member_and_dynamic_rows_never_carry_a_key(self, launch_built, mgr_built):
        # exact-счёт get_callers идёт по callee_key без фильтра вида — держится на этом инварианте
        for db in (launch_built[0], mgr_built[0]):
            with _conn(db) as c:
                n = c.execute(
                    "SELECT COUNT(*) FROM calls WHERE call_kind IN ('member', 'background_dynamic') "
                    "AND callee_key IS NOT NULL"
                ).fetchone()[0]
            assert n == 0

    def test_empty_page_with_positive_total_is_not_a_zero(self, launch_helpers):
        res = launch_helpers["find_callers_context"]("Задача", FON, 0, 0)
        assert res["callers"] == [] and res["_meta"]["total_callers"] == 5
        assert "No callers found" not in (res["_meta"].get("hint") or "")
        assert "unresolved_launches" not in res["_meta"]

    def test_overwritten_name_gives_no_stale_exact_path(self, tmp_path):
        imena = "CommonModules/Имена/Ext/Module.bsl"
        body = (
            'Процедура Перезапись() Экспорт\n    Имя = "Фон.Задача";\n'
            '    Имя = "Фон.Вторая"; ФоновыеЗадания.Выполнить(Имя);\nКонецПроцедуры\n'
        )
        _write_project(tmp_path, {FON: FON_BSL, imena: body})
        db = IndexBuilder().build(str(tmp_path), build_calls=True)
        bsl, reader = _make_bsl_for(tmp_path, db)
        try:
            stale = bsl["find_path"]("Перезапись", "Задача", from_hint=imena, to_hint=FON)
            assert stale["found"] is False and stale["_meta"]["budget_exceeded"] is False
            live = bsl["find_path"]("Перезапись", "Вторая", from_hint=imena, to_hint=FON)
            assert live["found"] is True and live["_meta"]["precision"] == "exact"
            assert [e["call_kind"] for e in live["path"]] == ["background", None]
        finally:
            reader.close()


class TestUnresolvedLaunchCounter:
    def test_counter_follows_update_in_open_reader(self, tmp_path):
        mod = "CommonModules/Счет/Ext/Module.bsl"
        plain = "Процедура П(Имя) Экспорт\nКонецПроцедуры\n"
        dyn = "Процедура П(Имя) Экспорт\n    ФоновыеЗадания.Выполнить(Имя);\nКонецПроцедуры\n"
        _write_project(tmp_path, {FON: FON_BSL, mod: plain})
        db = IndexBuilder().build(str(tmp_path), build_calls=True)
        r = IndexReader(db)
        try:
            assert r.count_unresolved_launches() == 0
            for content, expected in ((dyn, 1), (plain, 0)):
                p = tmp_path / mod
                p.write_text(content, encoding="utf-8-sig")
                st = p.stat()
                os.utime(p, (st.st_atime + 5, st.st_mtime + 5))
                IndexBuilder().update(str(tmp_path))
                assert r.count_unresolved_launches() == expected
        finally:
            r.close()

    def test_build_in_progress_is_not_cached_as_zero(self, launch_built):
        db, _ = launch_built
        r = IndexReader(db)
        try:
            con = sqlite3.connect(db)
            try:
                con.execute("INSERT OR REPLACE INTO index_meta (key, value) VALUES ('build_in_progress', '1')")
                con.commit()
                assert r.count_unresolved_launches() is None
                con.execute("INSERT OR REPLACE INTO index_meta (key, value) VALUES ('build_in_progress', '0')")
                con.commit()
            finally:
                con.close()
            assert r.count_unresolved_launches() == 5
        finally:
            r.close()

    def test_old_or_no_calls_index_has_no_counter(self, built_v16_copy, tmp_path_factory):
        r = IndexReader(built_v16_copy[0])
        try:
            assert r.count_unresolved_launches() is None
        finally:
            r.close()


# ── Задача 5: аудит адресатов find_unresolved_calls ───────────────────────────


def _layer(common=None, managers=None, objects=(), complete=True):
    """Слой классификатора: common/managers — {имя: {метод: экспорт}}; objects — {(кат, имя)}."""

    def _mods(d, key_fn):
        out = {}
        for name, spec in (d or {}).items():
            paths = (
                spec.get("paths", [f"{name}.bsl"]) if isinstance(spec, dict) and "paths" in spec else [f"{name}.bsl"]
            )
            methods = spec.get("methods") if isinstance(spec, dict) and "paths" in spec else spec
            out[key_fn(name)] = {
                "paths": paths,
                "methods": None if methods is None else {k.casefold(): v for k, v in methods.items()},
            }
        return out

    return {
        "common_modules": _mods(common, lambda n: n.casefold()),
        "manager_modules": _mods(managers, lambda k: (k[0], k[1].casefold())),
        "objects": None if objects is None else {(c, n.casefold()) for c, n in objects},
        "catalog_complete": complete,
    }


def _names_cf(names: str) -> set[str]:
    return {n.casefold() for n in names.split()}


# Методы менеджеров «<Вид>Менеджер.<Имя>» по синтакс-помощнику платформы 8.3 — русские и английские
# имена со страницы менеджера КАЖДОГО вида: состав у видов разный, метод менеджера другого вида
# (СоздатьЭлемент у плана счетов, итоги у регистра расчета) у этого менеджера отсутствует.
_SH_REF = (
    "Выбрать Select ВыбратьПоСсылкам SelectByRefs НайтиПоРеквизиту FindByAttribute "
    "ПолучитьДанныеВыбора GetChoiceData ПолучитьСсылку GetRef ПолучитьФорму GetForm "
    "ПолучитьФормуВыбора GetChoiceForm ПолучитьФормуСписка GetListForm ПустаяСсылка EmptyRef "
)
_SH_TEMPLATE = "ПолучитьМакет GetTemplate "  # у менеджера плана видов расчета его нет
_SH_PREDEFINED = (
    "ПолучитьИнициализациюПредопределенныхДанных GetPredefinedDataInitialization "
    "ПолучитьОбновлениеПредопределенныхДанных GetPredefinedDataUpdate "
    "УстановитьИнициализациюПредопределенныхДанных SetPredefinedDataInitialization "
    "УстановитьОбновлениеПредопределенныхДанных SetPredefinedDataUpdate "
)
_SH_CODE_NAME = "НайтиПоКоду FindByCode НайтиПоНаименованию FindByDescription "
_SH_HIERARCHY = (
    "ВыбратьИерархически SelectHierarchically ПолучитьФормуВыбораГруппы GetFolderChoiceForm "
    "ПолучитьФормуНовогоЭлемента GetNewItemForm ПолучитьФормуНовойГруппы GetNewFolderForm "
    "СоздатьГруппу CreateFolder СоздатьЭлемент CreateItem "
)
_SH_REGISTER = (
    "Выбрать Select ВыбратьПоРегистратору SelectByRecorder ПолучитьМакет GetTemplate "
    "ПолучитьФорму GetForm ПолучитьФормуСписка GetListForm СоздатьКлючЗаписи CreateRecordKey "
    "СоздатьНаборЗаписей CreateRecordSet "
)
# Итоги регистров накопления и бухгалтерии; английское имя ПолучитьМаксимальныйПериодРассчитанныхИтогов
# у этих видов разное — оно в строке вида.
_SH_TOTALS = (
    "ПересчитатьИтоги RecalcTotals ПересчитатьИтогиЗаПериод RecalcTotalsForPeriod "
    "ПересчитатьТекущиеИтоги RecalcPresentTotals ПолучитьИспользованиеИтогов GetTotalsUsing "
    "ПолучитьИспользованиеТекущихИтогов GetPresentTotalsUsing "
    "ПолучитьМинимальныйПериодРассчитанныхИтогов GetMinTotalsPeriod "
    "ПолучитьМаксимальныйПериодРассчитанныхИтогов "
    "ПолучитьРежимРазделенияИтогов GetTotalsSplittingMode УстановитьИспользованиеИтогов SetTotalsUsing "
    "УстановитьИспользованиеТекущихИтогов SetPresentTotalsUsing "
    "УстановитьМаксимальныйПериодРассчитанныхИтогов SetMaxTotalsPeriod "
    "УстановитьМинимальныйИМаксимальныйПериодыРассчитанныхИтогов SetMinAndMaxTotalsPeriods "
    "УстановитьМинимальныйПериодРассчитанныхИтогов SetMinTotalsPeriod "
    "УстановитьРежимРазделенияИтогов SetTotalsSplittingMode "
)
_MANAGER_SYNTAX = {
    "Catalogs": _SH_REF + _SH_TEMPLATE + _SH_PREDEFINED + _SH_CODE_NAME + _SH_HIERARCHY,
    "ChartsOfCharacteristicTypes": _SH_REF + _SH_TEMPLATE + _SH_PREDEFINED + _SH_CODE_NAME + _SH_HIERARCHY,
    "ChartsOfAccounts": _SH_REF
    + _SH_TEMPLATE
    + _SH_PREDEFINED
    + _SH_CODE_NAME
    + "ВыбратьИерархически SelectHierarchically ПолучитьФормуНовогоСчета GetNewAccountForm СоздатьСчет CreateAccount",
    "ChartsOfCalculationTypes": _SH_REF
    + _SH_PREDEFINED
    + _SH_CODE_NAME
    + "ПолучитьФормуНовогоВидаРасчета GetNewCalculationTypeForm СоздатьВидРасчета CreateCalculationType",
    "ExchangePlans": _SH_REF
    + _SH_TEMPLATE
    + _SH_CODE_NAME
    + "ПолучитьФормуНовогоУзла GetNewNodeForm СоздатьУзел CreateNode ЭтотУзел ThisNode",
    "Documents": _SH_REF
    + _SH_TEMPLATE
    + "НайтиПоНомеру FindByNumber ПолучитьФормуНовогоДокумента GetNewDocumentForm СоздатьДокумент CreateDocument",
    "BusinessProcesses": _SH_REF + _SH_TEMPLATE + "НайтиПоНомеру FindByNumber ПолучитьКартуМаршрута GetFlowchart "
    "ПолучитьФормуНовогоБизнесПроцесса GetNewBusinessProcessForm "
    "ПустаяСсылкаНаТочкуМаршрута EmptyRoutePointRef СоздатьБизнесПроцесс CreateBusinessProcess",
    "Tasks": _SH_REF + _SH_TEMPLATE + "НайтиПоНаименованию FindByDescription НайтиПоНомеру FindByNumber "
    "ПолучитьФормуНовойЗадачи GetNewTaskForm СоздатьЗадачу CreateTask",
    "Enums": "Индекс IndexOf Количество Count Получить Get ПолучитьДанныеВыбора GetChoiceData "
    "ПолучитьМакет GetTemplate ПолучитьФорму GetForm ПолучитьФормуВыбора GetChoiceForm "
    "ПолучитьФормуСписка GetListForm ПустаяСсылка EmptyRef",
    "Constants": "Получить Get СоздатьКлючЗначения CreateValueKey СоздатьМенеджерЗначения "
    "CreateValueManager Установить Set",
    "Reports": "ПолучитьМакет GetTemplate ПолучитьФорму GetForm Создать Create",
    "DataProcessors": "ПолучитьМакет GetTemplate ПолучитьФорму GetForm Создать Create",
    "InformationRegisters": _SH_REGISTER
    + "ПересчитатьИтоги RecalcTotals Получить Get ПолучитьИспользованиеИтогов GetTotalsUsing "
    "ПолучитьПервое GetFirst ПолучитьПоследнее GetLast ПолучитьФормуРедактированияЗаписи "
    "GetRecordEditingForm ПустойКлюч EmptyKey СоздатьМенеджерЗаписи CreateRecordManager "
    "СрезПервых SliceFirst СрезПоследних SliceLast УстановитьИспользованиеИтогов SetTotalsUsing",
    "AccumulationRegisters": _SH_REGISTER
    + _SH_TOTALS
    + "GetMaxTotalsPeriod АгрегатыЗаполнены AggregatesIsFilled ОбновитьАгрегаты UpdateAggregates "
    "Обороты Turnovers ОпределитьОптимальныеАгрегаты DetermineOptimalAggregates Остатки Balance "
    "ОчиститьАгрегаты ClearAggregates ПерестроитьИспользованиеАгрегатов RebuildAggregatesUsing "
    "ПолучитьАгрегаты GetAggregates ПолучитьИспользованиеАгрегатов GetAggregatesUsing "
    "ПолучитьРежимАгрегатов GetAggregatesMode УстановитьИспользованиеАгрегатов SetAggregatesUsing "
    "УстановитьРежимАгрегатов SetAggregatesMode",
    "AccountingRegisters": _SH_REGISTER
    + _SH_TOTALS
    + "GetTotalsPeriod Обороты Turnovers ОборотыДтКт DrCrTurnovers Остатки Balance",
    "CalculationRegisters": _SH_REGISTER + "ПолучитьБазу GetBase ПолучитьДанныеГрафика GetScheduleData",
}
# Имена, встреченные в вызовах менеджеров категории в коде сверенных конфигураций без определения в модуле
# менеджера. Все, кроме ПолучитьИмяПредопределенного, есть и в синтакс-помощнике; его вызывают у справочников
# (несколько конфигураций) и у планов видов характеристик (в том числе типовой код).
_MANAGER_OBSERVED = {
    "Catalogs": "пустаяссылка создатьэлемент получитьссылку найтипокоду найтипореквизиту найтипонаименованию "
    "получитьмакет выбрать создатьгруппу получитьформу получитьданныевыбора выбратьиерархически "
    "получитьимяпредопределенного установитьинициализациюпредопределенныхданных",
    "ChartsOfCharacteristicTypes": "пустаяссылка найтипокоду найтипонаименованию найтипореквизиту "
    "получитьссылку создатьэлемент выбрать получитьмакет создатьгруппу получитьимяпредопределенного",
    "ChartsOfAccounts": "найтипокоду пустаяссылка выбратьиерархически создатьсчет получитьмакет",
    "ChartsOfCalculationTypes": "пустаяссылка получитьссылку создатьвидрасчета",
    "ExchangePlans": "получитьмакет этотузел пустаяссылка создатьузел найтипокоду выбрать",
    "Documents": "пустаяссылка создатьдокумент получитьмакет получитьссылку выбрать найтипореквизиту найтипономеру",
    "BusinessProcesses": "создатьбизнеспроцесс пустаяссылка",
    "Tasks": "создатьзадачу пустаяссылка",
    "Enums": "пустаяссылка индекс получить получитьмакет количество получитьданныевыбора",
    "Constants": "получить установить создатьменеджерзначения",
    "Reports": "получитьмакет создать",
    "DataProcessors": "получитьмакет создать",
    "InformationRegisters": "создатьнаборзаписей создатьменеджерзаписи создатьключзаписи срезпоследних выбрать "
    "получитьмакет получить получитьпоследнее выбратьпорегистратору пустойключ "
    "установитьиспользованиеитогов пересчитатьитоги",
    "AccumulationRegisters": "создатьнаборзаписей установитьиспользованиеитогов пересчитатьитоги "
    "получитьмаксимальныйпериодрассчитанныхитогов",
    "AccountingRegisters": "создатьнаборзаписей получитьиспользованиетекущихитогов "
    "получитьмаксимальныйпериодрассчитанныхитогов получитьминимальныйпериодрассчитанныхитогов "
    "установитьиспользованиетекущихитогов установитьминимальныйимаксимальныйпериодырассчитанныхитогов "
    "получитьрежимразделенияитогов",
    "CalculationRegisters": "создатьнаборзаписей",
}


class TestAuditClassify:
    def _cls(self, *args, **kw):
        from rlm_tools_bsl.bsl_helpers import _audit_classify
        from rlm_tools_bsl.bsl_index import PLATFORM_MANAGER_METHODS

        return _audit_classify(*args, platform=PLATFORM_MANAGER_METHODS, **kw)

    def test_module_method_found_not_exported_missing(self):
        layers = {"main": _layer(common={"ОМ": {"Есть": True, "Скрытая": False}})}
        assert self._cls("module", "ОМ", "Есть", None, "main", layers) == (None, None)
        assert self._cls("module", "ом", "есть", None, "main", layers) == (None, None)
        assert self._cls("module", "ОМ", "Скрытая", None, "main", layers) == ("not_exported", "main")
        assert self._cls("module", "ОМ", "Нет", None, "main", layers) == ("method_missing", "main")

    def test_borrowed_module_unites_main_and_own_extension(self):
        layers = {
            "main": _layer(common={"ОМ": {"Есть": True}}),
            "extension:Е": _layer(common={"ОМ": {"Добавленный": True}}),
        }
        # вызывающий из Е видит оба модуля; из main — только свой
        assert self._cls("module", "ОМ", "Добавленный", None, "extension:Е", layers) == (None, None)
        assert self._cls("module", "ОМ", "Добавленный", None, "main", layers) == (
            "target_only_in_extension",
            "extension:Е",
        )

    def test_method_only_in_other_extension(self):
        layers = {
            "main": _layer(common={"ОМ": {"Есть": True}}),
            "extension:Е": _layer(),
            "extension:Ф": _layer(common={"ОМ": {"ТолькоФ": True}}),
        }
        assert self._cls("module", "ОМ", "ТолькоФ", None, "extension:Е", layers) == (
            "target_in_other_extension",
            "extension:Ф",
        )
        assert self._cls("module", "ОМ", "ТолькоФ", None, "main", layers) == ("target_only_in_extension", "extension:Ф")

    def test_launch_to_missing_module(self):
        layers = {"main": _layer(common={"ОМ": {"Есть": True}})}
        assert self._cls("module", "НетМодуля", "М", None, "main", layers, is_launch=True) == ("module_missing", None)

    def test_manager_object_and_methods(self):
        layers = {
            "main": _layer(
                managers={("Catalogs", "Номенклатура"): {"НайтиПоАртикулу": True, "Скрытый": False}},
                objects={("Catalogs", "Номенклатура"), ("Catalogs", "БезМодуля")},
            )
        }
        assert self._cls("manager", "Номенклатура", "НайтиПоАртикулу", "Catalogs", "main", layers) == (None, None)
        assert self._cls("manager", "Номенклатура", "НайтиПоКоду", "Catalogs", "main", layers) == (None, None)
        assert self._cls("manager", "БезМодуля", "ПустаяСсылка", "Catalogs", "main", layers) == (None, None)
        assert self._cls("manager", "Номенклатура", "Скрытый", "Catalogs", "main", layers) == ("not_exported", "main")
        assert self._cls("manager", "Номенклатура", "НетТакого", "Catalogs", "main", layers) == (
            "method_missing",
            "main",
        )
        assert self._cls("manager", "Удаленный", "ПустаяСсылка", "Catalogs", "main", layers) == ("object_missing", None)

    def test_object_only_in_own_or_other_extension(self):
        layers = {
            "main": _layer(objects=set()),
            "extension:Е": _layer(objects={("Catalogs", "СпрЕ")}),
            "extension:Ф": _layer(objects={("Catalogs", "СпрФ")}),
        }
        assert self._cls("manager", "СпрЕ", "ПустаяСсылка", "Catalogs", "extension:Е", layers) == (None, None)
        assert self._cls("manager", "СпрФ", "ПустаяСсылка", "Catalogs", "extension:Е", layers) == (
            "target_in_other_extension",
            "extension:Ф",
        )

    def test_unproven_object_catalog_is_internal(self):
        layers = {"main": _layer(objects=None), "extension:Ф": _layer(objects={("Catalogs", "Х")})}
        # отсутствие объекта в видимом слое недоказуемо — даже когда он найден в другом расширении
        assert self._cls("manager", "Х", "ПустаяСсылка", "Catalogs", "main", layers) == (
            "object_catalog_unproven",
            None,
        )
        assert self._cls("manager", "Y", "ПустаяСсылка", "Catalogs", "main", layers) == (
            "object_catalog_unproven",
            None,
        )

    def test_unproven_invisible_layer_does_not_block_proof(self):
        layers = {"main": _layer(objects=set()), "extension:Ф": _layer(objects=None)}
        assert self._cls("manager", "Y", "ПустаяСсылка", "Catalogs", "main", layers) == ("object_missing", None)

    def test_ambiguous_paths_inside_one_visible_layer(self):
        layers = {
            "main": _layer(
                common={"ОМ": {"paths": ["a/ОМ.bsl", "b/ОМ.bsl"], "methods": {"Есть": True}}},
                managers={("Catalogs", "Н"): {"paths": ["a/М.bsl", "b/М.bsl"], "methods": {"Свой": True}}},
                objects={("Catalogs", "Н")},
            )
        }
        assert self._cls("module", "ОМ", "Есть", None, "main", layers) == ("ambiguous_module", None)
        assert self._cls("manager", "Н", "Свой", "Catalogs", "main", layers) == ("ambiguous_module", None)
        # платформенный метод менеджера от кода модулей не зависит
        assert self._cls("manager", "Н", "ПустаяСсылка", "Catalogs", "main", layers) == (None, None)

    def test_borrowed_copy_in_main_and_extension_is_not_ambiguous(self):
        layers = {
            "main": _layer(common={"ОМ": {"Есть": True}}),
            "extension:Е": _layer(common={"ОМ": {"Свой": True}}),
        }
        assert self._cls("module", "ОМ", "Есть", None, "extension:Е", layers) == (None, None)

    def test_unread_or_incomplete_catalog_never_proves_absence(self):
        layers = {"main": _layer(common={"ОМ": None})}  # модуль есть, методы не прочитаны
        assert self._cls("module", "ОМ", "Нет", None, "main", layers) == ("target_catalog_unproven", None)
        layers = {"main": _layer(common={"ОМ": {"Есть": True}}, complete=False)}
        assert self._cls("module", "ОМ", "Есть", None, "main", layers) == (None, None)  # найденное подтверждается
        assert self._cls("module", "ОМ", "Нет", None, "main", layers) == ("target_catalog_unproven", None)
        layers = {"main": None, "extension:Е": _layer(common={"ОМ": {"Свой": True}})}
        assert self._cls("module", "ОМ", "Свой", None, "extension:Е", layers) == (None, None)
        assert self._cls("module", "ОМ", "Нет", None, "extension:Е", layers) == ("target_catalog_unproven", None)

    def test_platform_whitelist_covers_observed_names(self):
        from rlm_tools_bsl.bsl_index import PLATFORM_MANAGER_METHODS as P

        missing = {cat: sorted(_names_cf(names) - P[cat]) for cat, names in _MANAGER_OBSERVED.items()}
        assert all(not v for v in missing.values()), missing
        # методы форм обычного приложения — платформенные (встречены в коде конфигураций), в белом списке
        for name in ("получитьформувыбора", "получитьформусписка", "получитьформуновогоэлемента"):
            assert name in P["Catalogs"], name
        assert "получитьформуновогодокумента" in P["Documents"]
        assert "получитьформуновогобизнеспроцесса" in P["BusinessProcesses"]

    def test_platform_whitelist_covers_syntax_helper(self):
        # Пропуск дает ложный method_missing: на ДО3 «БизнесПроцессы.Подписание.ПустаяСсылкаНаТочкуМаршрута()».
        from rlm_tools_bsl.bsl_index import PLATFORM_MANAGER_METHODS as P

        missing = {cat: sorted(_names_cf(names) - P[cat]) for cat, names in _MANAGER_SYNTAX.items()}
        assert all(not v for v in missing.values()), {k: v for k, v in missing.items() if v}

    def test_platform_whitelist_has_nothing_beyond_its_manager_kind(self):
        # Лишнее имя не безвредно: метод менеджера ДРУГОГО вида в белом списке прячет настоящий
        # method_missing (ПланыСчетов.X.СоздатьЭлемент()). Категория — синтакс-помощник ее менеджера
        # плюс имена, наблюдаемые в коде этой категории.
        from rlm_tools_bsl.bsl_index import PLATFORM_MANAGER_METHODS as P

        assert set(P) == set(_MANAGER_SYNTAX)
        extra = {
            cat: sorted(P[cat] - _names_cf(_MANAGER_SYNTAX[cat]) - _names_cf(_MANAGER_OBSERVED.get(cat, "")))
            for cat in P
        }
        assert all(not v for v in extra.values()), {k: v for k, v in extra.items() if v}

    @pytest.mark.parametrize(
        "category, method",
        [
            ("ChartsOfAccounts", "СоздатьЭлемент"),
            ("ChartsOfAccounts", "CreateItem"),
            ("ExchangePlans", "СоздатьГруппу"),
            ("ExchangePlans", "CreateFolder"),
            ("ExchangePlans", "УстановитьИнициализациюПредопределенныхДанных"),
            ("ChartsOfCalculationTypes", "ВыбратьИерархически"),
            ("ChartsOfCalculationTypes", "ПолучитьМакет"),
            ("CalculationRegisters", "ПересчитатьИтоги"),
            ("InformationRegisters", "ПересчитатьТекущиеИтоги"),
            ("AccumulationRegisters", "ОстаткиИОбороты"),
            ("AccountingRegisters", "ДвиженияССубконто"),
            ("Constants", "ПолучитьФорму"),
        ],
    )
    def test_platform_method_of_another_manager_kind_is_missing(self, category, method):
        # Метод есть у менеджера другого вида (справочника, регистра накопления) либо это виртуальная
        # таблица запроса: объект есть, модуля менеджера с таким методом нет — настоящий method_missing.
        layers = {"main": _layer(objects={(category, "Х")})}
        assert self._cls("manager", "Х", method, category, "main", layers) == ("method_missing", "main")


def _cf_object_xml(kind, name, ts=()):
    ts_xml = "".join(
        f"<TabularSection><Properties><Name>{t}</Name></Properties><ChildObjects/></TabularSection>" for t in ts
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses" xmlns:v8="http://v8.1c.ru/8.1/data/core">\n'
        f"  <{kind}><Properties><Name>{name}</Name></Properties><ChildObjects>{ts_xml}</ChildObjects></{kind}>\n"
        "</MetaDataObject>\n"
    )


def _cf_predefined_xml(*names):
    items = "".join(
        f"<Item><Name>{n}</Name><Code>{i:03d}</Code><IsFolder>false</IsFolder></Item>" for i, n in enumerate(names, 1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<PredefinedData xmlns="http://v8.1c.ru/8.3/MDClasses" xmlns:v8="http://v8.1c.ru/8.1/data/core">'
        f"{items}</PredefinedData>\n"
    )


def _cf_form_xml(*attrs):
    body = "".join(f'<Attribute name="{a}" id="{i}"/>' for i, a in enumerate(attrs, 2))
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Form xmlns="http://v8.1c.ru/8.3/xcf/logform"><Attributes>'
        f'<Attribute name="Объект" id="1"><Main>true</Main></Attribute>{body}</Attributes></Form>\n'
    )


def _ext_descriptor(name):
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses" xmlns:v8="http://v8.1c.ru/8.1/data/core">\n'
        '  <Configuration uuid="00000000-0000-0000-0000-000000000021"><Properties>'
        f"<ObjectBelonging>Adopted</ObjectBelonging><Name>{name}</Name>"
        "<ConfigurationExtensionPurpose>Customization</ConfigurationExtensionPurpose>"
        f"<NamePrefix>{name}_</NamePrefix></Properties></Configuration>\n"
        "</MetaDataObject>\n"
    )


PROVERKA_BSL = (
    "Процедура Проверка(Параметр1)\n"
    "    ОМ.Есть();\n"
    "    ОМ.Удалена();\n"
    "    УдаленныйМодуль.НетМетода();\n"
    "    ОМ.Скрытая();\n"
    "    Справочники.Номенклатура.НайтиПоАртикулу();\n"
    '    Справочники.Номенклатура.НайтиПоКоду("1");\n'
    "    Справочники.Номенклатура.НетТакого();\n"
    "    Справочники.Удаленный.ПустаяСсылка();\n"
    "    Объект.ОМ.Добавить();\n"
    '    ФоновыеЗадания.Выполнить("НетМодуля.М");\n'
    '    ФоновыеЗадания.Выполнить("ОМ.Удалена");\n'
    "    ФоновыеЗадания.Выполнить(Параметр1);\n"
    "    ОМ.ТолькоВРасш1();\n"
    "КонецПроцедуры\n"
)
AUDIT_MAIN = {
    "CommonModules/ОМ/Ext/Module.bsl": "Процедура Есть() Экспорт\nКонецПроцедуры\n\nПроцедура Скрытая()\nКонецПроцедуры\n",
    "Catalogs/Номенклатура.xml": _cf_object_xml("Catalog", "Номенклатура", ts=["Товары"]),
    "Catalogs/Номенклатура/Ext/Predefined.xml": _cf_predefined_xml("Услуга"),
    "Catalogs/Номенклатура/Ext/ManagerModule.bsl": (
        "Функция НайтиПоАртикулу() Экспорт\n    Возврат Неопределено;\nКонецФункции\n\n"
        "Процедура Заполнить()\n    ОМ = 1;\n    ОМ.Добавить();\n    Услуга.ПолучитьОбъект();\nКонецПроцедуры\n"
    ),
    "Documents/Заказ.xml": _cf_object_xml("Document", "Заказ"),
    "Documents/Заказ/Ext/ObjectModule.bsl": PROVERKA_BSL,
    "Documents/Заказ/Forms/ФормаДокумента/Ext/Form.xml": _cf_form_xml("ОМ"),
    "Documents/Заказ/Forms/ФормаДокумента/Ext/Form/Module.bsl": (
        "&НаКлиенте\nПроцедура Команда()\n    ОМ.Добавить();\nКонецПроцедуры\n"
    ),
}
AUDIT_EXT1 = {
    "Configuration.xml": _ext_descriptor("Расш1"),
    "CommonModules/ОМ/Ext/Module.bsl": "Процедура ТолькоВРасш1() Экспорт\nКонецПроцедуры\n",
    "Catalogs/Расш1_Спр.xml": _cf_object_xml("Catalog", "Расш1_Спр"),
    "Catalogs/Расш1_Спр/Ext/ManagerModule.bsl": "Процедура М() Экспорт\nКонецПроцедуры\n",
    "CommonModules/Расш1_Модуль/Ext/Module.bsl": (
        "Процедура Вызовы() Экспорт\n"
        "    Справочники.Расш1_Спр.М();\n"
        "    Справочники.Расш2_Спр.М();\n"
        "    ОМ.Есть();\n"
        "    #Удаление\n"
        "    ОМ.Удалена();\n"
        "    #КонецУдаления\n"
        "КонецПроцедуры\n"
    ),
}
AUDIT_EXT2 = {
    "Configuration.xml": _ext_descriptor("Расш2"),
    "Catalogs/Расш2_Спр.xml": _cf_object_xml("Catalog", "Расш2_Спр"),
    "Catalogs/Расш2_Спр/Ext/ManagerModule.bsl": "Процедура М() Экспорт\nКонецПроцедуры\n",
    "CommonModules/Услуга/Ext/Module.bsl": "Процедура Прочее() Экспорт\nКонецПроцедуры\n",
}


def _write_tree(root, files):
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8-sig")


def _downgrade_calls_to_v16(db_path):
    con = sqlite3.connect(db_path)
    try:
        con.executescript(
            "CREATE TABLE calls_old AS SELECT id, caller_id, callee_name, line, callee_key FROM calls;"
            "DROP TABLE calls;"
            "ALTER TABLE calls_old RENAME TO calls;"
        )
        con.execute("UPDATE index_meta SET value='16' WHERE key IN ('version', 'builder_version')")
        con.commit()
    finally:
        con.close()


def _audit_session(
    tmp_path,
    main=AUDIT_MAIN,
    ext1=AUDIT_EXT1,
    ext2=AUDIT_EXT2,
    *,
    build_calls=True,
    build_metadata=True,
    downgrade=False,
    with_reader=True,
    role="main",
):
    """Сессия как у сервера: индекс основной конфигурации + соседние расширения."""
    from rlm_tools_bsl.bsl_helpers import make_bsl_helpers
    from rlm_tools_bsl.format_detector import detect_format
    from rlm_tools_bsl.helpers import make_helpers

    cf = tmp_path / "src" / "cf"
    exts = {}
    _write_project(cf, main)
    for name, files in (("Расш1", ext1), ("Расш2", ext2)):
        if files is None:
            continue
        root = tmp_path / "src" / "cfe" / name
        _write_tree(root, files)
        exts[str(root)] = name
    reader = None
    if with_reader:
        db = IndexBuilder().build(str(cf), build_calls=build_calls, build_metadata=build_metadata)
        if downgrade:
            _downgrade_calls_to_v16(db)
        reader = IndexReader(db)
    generic, resolve_safe = make_helpers(str(cf), idx_reader=reader)
    bsl = make_bsl_helpers(
        base_path=str(cf),
        resolve_safe=resolve_safe,
        read_file_fn=generic["read_file"],
        grep_fn=generic["grep"],
        glob_files_fn=generic["glob_files"],
        format_info=detect_format(str(cf)),
        idx_reader=reader,
        idx_zero_callers_authoritative=reader is not None,
        extension_paths=list(exts),
        current_config_role=role,
        current_config_name="Тест",
        current_config_root=str(cf),
        extension_name_by_root=exts,
    )
    return bsl, reader, cf


@pytest.fixture
def audit_helpers(tmp_path):
    bsl, reader, _cf = _audit_session(tmp_path)
    yield bsl
    reader.close()


@pytest.fixture
def audit_helpers_v16(tmp_path):
    bsl, reader, _cf = _audit_session(tmp_path, downgrade=True)
    yield bsl
    reader.close()


@pytest.fixture
def audit_helpers_no_calls(tmp_path):
    bsl, reader, _cf = _audit_session(tmp_path, build_calls=False)
    yield bsl
    reader.close()


@pytest.fixture
def audit_helpers_no_metadata(tmp_path):
    bsl, reader, _cf = _audit_session(tmp_path, build_metadata=False)
    yield bsl
    reader.close()


MAIN_EXPECTED = [
    ("ОМ.Удалена", "method_missing"),
    ("ОМ.Скрытая", "not_exported"),
    ("Catalog.Номенклатура.НетТакого", "method_missing"),
    ("Catalog.Удаленный.ПустаяСсылка", "object_missing"),
    ("НетМодуля.М", "module_missing"),
    ("ОМ.Удалена", "method_missing"),
    ("ОМ.ТолькоВРасш1", "target_only_in_extension"),
]


class TestAuditMain:
    def test_expected_issues_main(self, audit_helpers):
        res = audit_helpers["find_unresolved_calls"](layer="main")
        got = sorted((i["target"], i["reason"]) for i in res["issues"])
        assert got == sorted(MAIN_EXPECTED)
        assert res["not_checked"]["dynamic_name"] == 1
        assert res["not_checked"]["unknown_receiver"] == 1 and res["source"] == "index"
        assert {i["owner"] for i in res["issues"]} == {"main"}
        only_ext = [i for i in res["issues"] if i["reason"] == "target_only_in_extension"]
        assert only_ext[0]["target_owner"] == "extension:Расш1"
        assert res["extensions_included"] is False and res["partial"] is False
        kinds = sorted((i["target"], i["kind"]) for i in res["issues"] if i["target"].startswith("НетМодуля"))
        assert kinds == [("НетМодуля.М", "launch")]

    def test_deleted_module_candidate_is_visible_on_request(self, audit_helpers):
        res = audit_helpers["find_unresolved_calls"](layer="main", reasons=["receiver_unknown"])
        assert [(i["target"], i["reason"], i["kind"]) for i in res["issues"]] == [
            ("УдаленныйМодуль.НетМетода", "receiver_unknown", "receiver_unknown")
        ]
        assert res["issues"][0]["target_owner"] is None
        assert res["issues"][0]["expression"] == "УдаленныйМодуль.НетМетода();"

    def test_dynamic_name_listed_only_on_request(self, audit_helpers):
        res = audit_helpers["find_unresolved_calls"](reasons=["dynamic_name"])
        assert [(i["kind"], i["reason"], i["target"]) for i in res["issues"]] == [("launch", "dynamic_name", "")]

    def test_v16_index_main_layer_unavailable(self, audit_helpers_v16):
        res = audit_helpers_v16["find_unresolved_calls"](layer="main")
        assert res["source"] == "unavailable" and res["partial"] is True
        assert "index_v17_required" in res["_meta"]["reasons"] and "index update" in res["hint"]

    def test_v17_without_calls_main_layer_unavailable(self, audit_helpers_no_calls):
        res = audit_helpers_no_calls["find_unresolved_calls"](layer="main")
        assert res["source"] == "unavailable" and res["partial"] is True
        assert "calls_disabled" in res["_meta"]["reasons"]
        assert "index build" in res["hint"] and "index update" not in res["hint"]
        both = audit_helpers_no_calls["find_unresolved_calls"]()
        assert both["source"] == "live" and both["partial"] is True
        assert any(i["owner"] == "extension:Расш1" for i in both["issues"])

    def test_cf_ext_descriptor_without_bsl_module_is_existing_object(self, tmp_path):
        main = {
            **AUDIT_MAIN,
            "Catalogs/ТолькоОписание/Ext/Catalog.xml": _cf_object_xml("Catalog", "ТолькоОписание"),
            "CommonModules/Проба/Ext/Module.bsl": (
                "Процедура П() Экспорт\n    Справочники.ТолькоОписание.ПустаяСсылка();\nКонецПроцедуры\n"
            ),
        }
        bsl, reader, _cf = _audit_session(tmp_path, main)
        try:
            res = bsl["find_unresolved_calls"](layer="main")
            assert not any(
                i["target"].startswith("Catalog.ТолькоОписание.") and i["reason"] == "object_missing"
                for i in res["issues"]
            )
        finally:
            reader.close()

    def test_method_of_another_manager_kind_is_reported(self, tmp_path):
        # СоздатьЭлемент — метод менеджера справочника, у плана счетов его нет; CreateFolder у плана
        # обмена — тоже. Объекты существуют (описатели), модулей менеджеров нет.
        main = {
            **AUDIT_MAIN,
            "ChartsOfAccounts/Хозрасчетный.xml": _cf_object_xml("ChartOfAccounts", "Хозрасчетный"),
            "ExchangePlans/Обмен.xml": _cf_object_xml("ExchangePlan", "Обмен"),
            "CommonModules/Проба/Ext/Module.bsl": (
                "Процедура П() Экспорт\n"
                "    ПланыСчетов.Хозрасчетный.СоздатьЭлемент();\n"
                "    ExchangePlans.Обмен.CreateFolder();\n"
                "    ПланыСчетов.Хозрасчетный.СоздатьСчет();\n"
                "    ПланыОбмена.Обмен.ЭтотУзел();\n"
                "КонецПроцедуры\n"
            ),
        }
        bsl, reader, _cf = _audit_session(tmp_path, main)
        try:
            res = bsl["find_unresolved_calls"](layer="main", path="CommonModules/Проба")
            assert sorted((i["line"], i["reason"]) for i in res["issues"]) == [
                (2, "method_missing"),
                (3, "method_missing"),
            ]
            assert [i["target"].rsplit(".", 1)[1] for i in res["issues"]] == ["СоздатьЭлемент", "CreateFolder"]
            assert res["checked"]["manager_calls"] == 4 and res["partial"] is False
        finally:
            reader.close()

    def test_two_identical_manager_errors_are_two_issues(self, tmp_path):
        main = {
            **AUDIT_MAIN,
            "CommonModules/Проба/Ext/Module.bsl": (
                "Процедура П() Экспорт\n    Справочники.Удаленный.ПустаяСсылка();\n"
                "    Справочники.Удаленный.ПустаяСсылка();\nКонецПроцедуры\n"
            ),
        }
        bsl, reader, _cf = _audit_session(tmp_path, main)
        try:
            res = bsl["find_unresolved_calls"](layer="main", path="CommonModules/Проба")
            assert [(i["line"], i["reason"]) for i in res["issues"]] == [(2, "object_missing"), (3, "object_missing")]
            assert res["checked"]["manager_calls"] == 2
        finally:
            reader.close()

    def test_path_filter_limits_counters_to_the_caller_file(self, tmp_path):
        main = {
            **AUDIT_MAIN,
            "CommonModules/Проба%_1/Ext/Module.bsl": (
                "Процедура П() Экспорт\n    ОМ.Удалена();\n    Справочники.Удаленный.ПустаяСсылка();\n"
                "    Локальная();\nКонецПроцедуры\n\nПроцедура Локальная()\nКонецПроцедуры\n"
            ),
            "CommonModules/ПробаX1/Ext/Module.bsl": "Процедура П() Экспорт\n    ОМ.Удалена();\nКонецПроцедуры\n",
        }
        bsl, reader, _cf = _audit_session(tmp_path, main)
        try:
            res = bsl["find_unresolved_calls"](layer="main", path="commonmodules/проба%_1")
            assert sorted(i["target"] for i in res["issues"]) == ["Catalog.Удаленный.ПустаяСсылка", "ОМ.Удалена"]
            assert res["checked"] == {"module_calls": 1, "manager_calls": 1, "launches": 0}
            # локальный вызов с ненулевым ключом — голый: в unqualified
            assert res["not_checked"] == {
                "dynamic_name": 0,
                "unqualified": 1,
                "value_methods": 0,
                "unknown_receiver": 0,
            }
            # «%» и «_» — литералы: соседний модуль ПробаX1 сюда не попадает
            assert all(i["file"].startswith("CommonModules/Проба%_1/") for i in res["issues"])
        finally:
            reader.close()

    def test_empty_graph_is_an_answer_not_a_failure(self, tmp_path):
        main = {"CommonModules/Пусто/Ext/Module.bsl": "Процедура П() Экспорт\nКонецПроцедуры\n"}
        bsl, reader, _cf = _audit_session(tmp_path, main, None, None)
        try:
            res = bsl["find_unresolved_calls"]()
            assert res["source"] == "index" and res["partial"] is False
            assert res["checked"] == {"module_calls": 0, "manager_calls": 0, "launches": 0}
            assert all(isinstance(v, int) and v == 0 for v in res["not_checked"].values())
        finally:
            reader.close()

    def test_four_segment_launch_is_unsupported_not_dynamic(self, tmp_path):
        main = {
            **AUDIT_MAIN,
            "CommonModules/Проба/Ext/Module.bsl": (
                'Процедура П() Экспорт\n    ДлительныеОперации.ВыполнитьВФоне("Обработка.Х.МодульОбъекта.М");\n'
                "КонецПроцедуры\n"
            ),
        }
        bsl, reader, _cf = _audit_session(tmp_path, main)
        try:
            res = bsl["find_unresolved_calls"](layer="main", path="CommonModules/Проба")
            assert [(i["reason"], i["expression"]) for i in res["issues"]] == [
                ("unsupported_name_form", "Обработка.Х.МодульОбъекта.М")
            ]
            assert res["not_checked"]["dynamic_name"] == 0
            dyn = bsl["find_unresolved_calls"](layer="main", path="CommonModules/Проба", reasons=["dynamic_name"])
            assert dyn["issues"] == []
        finally:
            reader.close()

    def test_two_copies_of_a_common_module_are_ambiguous(self, tmp_path):
        main = {
            **AUDIT_MAIN,
            # вложенная копия: parse_bsl_path находит категорию в любом компоненте пути. Экспорты
            # копий РАЗНЫЕ: пара ОМ/Есть однозначна, и резолвер ставит вызову ненулевой ключ —
            # неоднозначность модуля аудит обязан увидеть мимо ключа.
            "Копия/CommonModules/ОМ/Ext/Module.bsl": "Процедура Другая() Экспорт\nКонецПроцедуры\n",
            "CommonModules/Проба/Ext/Module.bsl": (
                'Процедура П() Экспорт\n    ОМ.Есть();\n    ФоновыеЗадания.Выполнить("ОМ.Есть");\nКонецПроцедуры\n'
            ),
        }
        bsl, reader, cf = _audit_session(tmp_path, main)
        try:
            with _conn(BI.get_index_db_path(str(cf))) as c:
                keyed = c.execute(
                    "SELECT COUNT(*) FROM calls c JOIN methods m ON m.id = c.caller_id "
                    "JOIN modules mod ON mod.id = m.module_id WHERE mod.rel_path = ? "
                    "AND c.callee_name = 'ОМ.Есть' AND c.callee_key IS NOT NULL",
                    ("CommonModules/Проба/Ext/Module.bsl",),
                ).fetchone()[0]
            assert keyed == 2  # прямой вызов и запуск — оба с ключом
            res = bsl["find_unresolved_calls"](layer="main", path="CommonModules/Проба")
            assert sorted((i["kind"], i["reason"]) for i in res["issues"]) == [
                ("launch", "ambiguous_module"),
                ("module", "ambiguous_module"),
            ]
        finally:
            reader.close()

    def test_closed_modules_are_unproven_not_missing(self, tmp_path):
        # Поставка без исходников: модуль лежит как Module.bin / ManagerModule.bin. Методы такого
        # модуля из выгрузки не видны — отсутствие адресата в нем недоказуемо (ДО3: подсистема КОД).
        main = {
            **AUDIT_MAIN,
            "CommonModules/Закрытый/Ext/Module.bin": "closed",
            "InformationRegisters/РегЗакрытый.xml": _cf_object_xml("InformationRegister", "РегЗакрытый"),
            "InformationRegisters/РегЗакрытый/Ext/ManagerModule.bin": "closed",
            "CommonModules/Проба/Ext/Module.bsl": (
                "Процедура П() Экспорт\n"
                "    Закрытый.Метод();\n"
                "    РегистрыСведений.РегЗакрытый.СвойМетод();\n"
                '    ФоновыеЗадания.Выполнить("Закрытый.Метод");\n'
                "КонецПроцедуры\n"
            ),
        }
        bsl, reader, _cf = _audit_session(tmp_path, main)
        try:
            res = bsl["find_unresolved_calls"](layer="main", path="CommonModules/Проба")
            assert res["issues"] == []
            assert res["_meta"]["targets_unproven"] == 3
            assert res["not_checked"]["unknown_receiver"] == 0  # закрытый модуль — модуль, а не значение
            assert res["partial"] is True and "target_catalog_unproven" in res["_meta"]["reasons"]
            assert ".bin" in res["hint"]
        finally:
            reader.close()


class TestAuditShadowing:
    def test_form_attribute_assignment_predefined_and_chain_not_reported(self, audit_helpers):
        res = audit_helpers["find_unresolved_calls"]()
        exprs = [i["expression"] for i in res["issues"]]
        assert not any("ОМ.Добавить" in e or "Услуга.ПолучитьОбъект" in e for e in exprs)
        # реквизит формы ОМ, присваивание ОМ = 1, предопределённый Услуга; Объект.ОМ.Добавить() —
        # член цепочки и кандидатом не был вовсе, поэтому в счётчик не входит
        assert res["_meta"]["shadowed_excluded"] == 3

    def test_no_metadata_index_still_checks_live_shadow_context(self, audit_helpers_no_metadata):
        res = audit_helpers_no_metadata["find_unresolved_calls"]()
        assert res["source"] in ("index", "index+live")
        assert not any(
            "ОМ.Добавить" in i["expression"] or "Услуга.ПолучитьОбъект" in i["expression"] for i in res["issues"]
        )

    def test_assignment_with_line_break_before_equals_shadows_head(self, tmp_path):
        main = {
            **AUDIT_MAIN,
            "CommonModules/Проба/Ext/Module.bsl": (
                "Процедура П() Экспорт\n"
                "    ОМ\n"
                "        = Новый Структура;\n"
                '    ОМ.Вставить("Ключ", 1);\n'
                "    ДлительныеОперации\n"
                "        // пояснение\n"
                "        = Новый Структура;\n"
                '    ДлительныеОперации.ВыполнитьВФоне("ОМ.Удалена");\n'
                "КонецПроцедуры\n"
            ),
        }
        bsl, reader, _cf = _audit_session(tmp_path, main)
        try:
            res = bsl["find_unresolved_calls"](layer="main", path="CommonModules/Проба")
            assert res["issues"] == [] and res["partial"] is False
            assert res["_meta"]["shadowed_excluded"] == 1  # ОМ.Вставить; затененная обертка — не запуск вовсе
            assert res["checked"]["launches"] == 0
        finally:
            reader.close()

    def test_literal_launch_target_is_not_shadowed(self, tmp_path):
        main = {
            "CommonModules/ОМ/Ext/Module.bsl": "Процедура Есть() Экспорт\nКонецПроцедуры\n",
            "Documents/Заказ.xml": _cf_object_xml("Document", "Заказ"),
            "CommonModules/Проба/Ext/Module.bsl": (
                "Процедура П() Экспорт\n    ОМ = Неопределено;\n    Документы = Неопределено;\n"
                "    ОМ.Удалена();\n    Документы.Заказ.НетТакого();\n"
                '    ФоновыеЗадания.Выполнить("ОМ.Удалена");\n'
                '    ФоновыеЗадания.Выполнить("Документы.Заказ.НетТакого");\nКонецПроцедуры\n'
            ),
        }
        bsl, reader, _cf = _audit_session(tmp_path, main, None, None)
        try:
            res = bsl["find_unresolved_calls"](layer="main")
            assert sorted((i["kind"], i["target"], i["reason"]) for i in res["issues"]) == sorted(
                [("launch", "ОМ.Удалена", "method_missing"), ("launch", "Document.Заказ.НетТакого", "method_missing")]
            )
            assert res["_meta"]["shadowed_excluded"] == 2
        finally:
            reader.close()

    @pytest.mark.parametrize("build_metadata", [True, False])
    def test_same_named_form_of_other_category_does_not_shadow(self, tmp_path, build_metadata):
        # Реквизит ОМ — у формы СПРАВОЧНИКА Х2; одноименная форма ДОКУМЕНТА Х2 его не видит, и ее
        # ОМ.Удалена() — настоящий method_missing (индексные имена контекста — по категории модуля).
        form_doc = "Documents/Х2/Forms/Форма/Ext/Form/Module.bsl"
        main = {
            **AUDIT_MAIN,
            "Catalogs/Х2.xml": _cf_object_xml("Catalog", "Х2"),
            "Catalogs/Х2/Forms/Форма/Ext/Form.xml": _cf_form_xml("ОМ"),
            "Catalogs/Х2/Forms/Форма/Ext/Form/Module.bsl": "&НаКлиенте\nПроцедура К()\nКонецПроцедуры\n",
            "Documents/Х2.xml": _cf_object_xml("Document", "Х2"),
            "Documents/Х2/Forms/Форма/Ext/Form.xml": _cf_form_xml(),
            form_doc: "&НаСервере\nПроцедура Команда()\n    ОМ.Удалена();\nКонецПроцедуры\n",
        }
        bsl, reader, _cf = _audit_session(tmp_path, main, build_metadata=build_metadata)
        try:
            res = bsl["find_unresolved_calls"](layer="main", path="Documents/Х2")
            assert [(i["target"], i["reason"]) for i in res["issues"]] == [("ОМ.Удалена", "method_missing")]
        finally:
            reader.close()

    def test_manager_shadow_follows_its_own_collection_on_shared_line(self, tmp_path):
        # Справочники — параметр (не менеджер), Документы — менеджер: второй вызов той же строки —
        # настоящий object_missing, и затенение первого его не прячет.
        main = {
            **AUDIT_MAIN,
            "CommonModules/Проба/Ext/Module.bsl": (
                "Процедура П(Справочники) Экспорт\n"
                "    Справочники.Нет.Метод(); Документы.Нет.Метод();\n"
                "КонецПроцедуры\n"
            ),
        }
        bsl, reader, _cf = _audit_session(tmp_path, main)
        try:
            res = bsl["find_unresolved_calls"](layer="main", path="CommonModules/Проба")
            assert [(i["target"], i["reason"]) for i in res["issues"]] == [("Document.Нет.Метод", "object_missing")]
            assert res["_meta"]["shadowed_excluded"] == 1
        finally:
            reader.close()

    # Одна категория, два написания коллекции на одной строке: экстрактор отдает ОДНО ребро
    # (дедуп по адресу), и затенено оно, только если затенены ВСЕ его головы. Член цепочки
    # (Метаданные.Справочники.X.М()) менеджером не считается и головой ребра не бывает.
    @pytest.mark.parametrize("layer", ["main", "extensions"])
    @pytest.mark.parametrize(
        "line, expected",
        [
            ("Catalogs.Нет.Метод(); Справочники.Нет.Метод();", [("Catalog.Нет.Метод", "object_missing")]),
            ("Справочники.Нет.Метод(); Catalogs.Нет.Метод();", [("Catalog.Нет.Метод", "object_missing")]),
            # точка цепочки на предыдущей строке: Справочники здесь — член цепочки, а не голова
            ("Метаданные.\n        Справочники.Нет.Метод(); Catalogs.Нет.Метод();", []),
        ],
    )
    def test_shared_line_manager_edge_is_shadowed_only_when_all_heads_are(self, tmp_path, layer, line, expected):
        body = f"Процедура П(Catalogs) Экспорт\n    {line}\nКонецПроцедуры\n"
        if layer == "main":
            main, ext1, needle = {**AUDIT_MAIN, "CommonModules/Проба/Ext/Module.bsl": body}, None, "Проба"
        else:
            main, needle = AUDIT_MAIN, "Расш1_Проба"
            ext1 = {**AUDIT_EXT1, "CommonModules/Расш1_Проба/Ext/Module.bsl": body}
        bsl, reader, _cf = _audit_session(tmp_path, main, ext1, None)
        try:
            res = bsl["find_unresolved_calls"](layer=layer)
            got = [(i["target"], i["reason"]) for i in res["issues"] if needle in i["file"]]
            assert got == expected
            assert res["partial"] is False
        finally:
            reader.close()

    def test_broken_form_descriptor_makes_candidate_unproven(self, tmp_path):
        main = {**AUDIT_MAIN, "Documents/Заказ/Forms/ФормаДокумента/Ext/Form.xml": "<Form><broken"}
        bsl, reader, _cf = _audit_session(tmp_path, main)
        try:
            res = bsl["find_unresolved_calls"](layer="main", reasons=["method_missing"])
            assert not any("ОМ.Добавить" in i["expression"] for i in res["issues"])
            assert res["partial"] is True and "shadow_context_unavailable" in res["_meta"]["reasons"]
            assert res["_meta"]["shadow_context_unavailable"] == 1
        finally:
            reader.close()

    @pytest.mark.parametrize("target", ["ОМ.Есть", "ОМ.Удалена"])
    def test_shadowed_launcher_gives_no_target_issue(self, tmp_path, target):
        main = {
            **AUDIT_MAIN,
            "CommonModules/Проба/Ext/Module.bsl": (
                f'Процедура П(ДлительныеОперации) Экспорт\n    ДлительныеОперации.ВыполнитьВФоне("{target}");\n'
                "КонецПроцедуры\n"
            ),
        }
        bsl, reader, _cf = _audit_session(tmp_path, main)
        try:
            res = bsl["find_unresolved_calls"](layer="main", path="CommonModules/Проба")
            assert res["issues"] == [] and res["partial"] is False
        finally:
            reader.close()

    def test_unavailable_launcher_context_is_partial_not_dynamic(self, tmp_path):
        main = {
            **AUDIT_MAIN,
            # форма без описателя: контекст получателя обёртки недоказуем
            "Documents/Заказ/Forms/БезОписателя/Ext/Form/Module.bsl": (
                '&НаСервере\nПроцедура З()\n    ДлительныеОперации.ВыполнитьВФоне("ОМ.Удалена");\nКонецПроцедуры\n'
            ),
        }
        bsl, reader, _cf = _audit_session(tmp_path, main)
        try:
            path = "Documents/Заказ/Forms/БезОписателя"
            res = bsl["find_unresolved_calls"](layer="main", path=path, reasons=["method_missing"])
            assert res["issues"] == []
            assert res["partial"] is True and "shadow_context_unavailable" in res["_meta"]["reasons"]
            assert res["not_checked"]["dynamic_name"] == 0
        finally:
            reader.close()


class TestAuditExtensions:
    def test_extension_layer(self, audit_helpers):
        res = audit_helpers["find_unresolved_calls"](layer="extensions")
        got = sorted((i["owner"], i["target"], i["reason"], i.get("target_owner")) for i in res["issues"])
        assert got == [("extension:Расш1", "Catalog.Расш2_Спр.М", "target_in_other_extension", "extension:Расш2")]
        assert res["extensions_included"] is True and res["source"] == "live"
        named = audit_helpers["find_unresolved_calls"](layer="расш1")
        assert [i["owner"] for i in named["issues"]] == ["extension:Расш1"]

    def test_extension_session_is_refused_with_route(self, tmp_path):
        bsl, reader, _cf = _audit_session(tmp_path, role="extension")
        try:
            res = bsl["find_unresolved_calls"]()
            assert res["source"] == "unavailable" and "extension_session" in res["_meta"]["reasons"]
        finally:
            reader.close()

    @pytest.mark.parametrize("variant", ["v16", "no_calls"])
    def test_extension_layer_works_without_main_graph(self, tmp_path, variant):
        ext1 = {
            **AUDIT_EXT1,
            "CommonModules/Расш1_Модуль/Ext/Module.bsl": (
                "Процедура Вызовы() Экспорт\n    ОМ.Есть();\n    ОМ.ТолькоВРасш1();\n    ОМ.НетНигде();\nКонецПроцедуры\n"
            ),
        }
        bsl, reader, _cf = _audit_session(
            tmp_path, AUDIT_MAIN, ext1, AUDIT_EXT2, downgrade=variant == "v16", build_calls=variant != "v16"
        )
        try:
            res = bsl["find_unresolved_calls"](layer="extensions")
            assert res["source"] == "live"
            got = sorted((i["target"], i["reason"]) for i in res["issues"])
            if variant == "v16":
                # каталог main v16 неполон (Д5): отсутствие метода в нём не доказано
                assert got == []
                assert "target_catalog_unproven" in res["_meta"]["reasons"]
            else:
                assert got == [("ОМ.НетНигде", "method_missing")]
        finally:
            reader.close()

    def test_without_reader_main_absence_is_unproven(self, tmp_path):
        ext1 = {
            **AUDIT_EXT1,
            "CommonModules/Расш1_Модуль/Ext/Module.bsl": (
                "Процедура Вызовы() Экспорт\n    ОМ.ТолькоВРасш1();\n    ОМ.НетНигде();\n"
                "    Расш1_Свой.Есть();\n    Расш1_Свой.Нет();\nКонецПроцедуры\n"
            ),
            "CommonModules/Расш1_Свой/Ext/Module.bsl": "Процедура Есть() Экспорт\nКонецПроцедуры\n",
        }
        bsl, _reader, _cf = _audit_session(tmp_path, AUDIT_MAIN, ext1, AUDIT_EXT2, with_reader=False)
        res = bsl["find_unresolved_calls"]()
        assert res["source"] == "live" and res["partial"] is True
        assert "main_catalog_unavailable" in res["_meta"]["reasons"]
        assert "target_catalog_unproven" in res["_meta"]["reasons"]
        # найденный адресат подтверждается и без индекса основной конфигурации, а отрицательный
        # вывод — нет: одноимённый модуль main мог бы нести метод (заимствование объединяет их)
        assert res["issues"] == []
        assert res["_meta"]["targets_unproven"] == 2

    def test_main_layer_with_extension_target_is_index_only(self, audit_helpers):
        res = audit_helpers["find_unresolved_calls"](layer="main", reasons=["target_only_in_extension"])
        assert res["source"] == "index" and res["extensions_included"] is False
        assert [i["target"] for i in res["issues"]] == ["ОМ.ТолькоВРасш1"]


class TestAuditContract:
    def test_pagination_and_aggregates_are_from_full_set(self, audit_helpers):
        full = audit_helpers["find_unresolved_calls"](limit=1000)
        p1 = audit_helpers["find_unresolved_calls"](limit=3, offset=0)
        p2 = audit_helpers["find_unresolved_calls"](limit=3, offset=3)
        assert p1["by_reason"] == full["by_reason"] == p2["by_reason"]
        assert p1["issues"] + p2["issues"] == full["issues"][:6]
        assert p1["has_more"] is True and p1["truncated"] is True
        assert full["total"] == len(full["issues"]) == 8

    def test_unknown_reason_and_layer(self, audit_helpers):
        res = audit_helpers["find_unresolved_calls"](reasons=["нет_такой"])
        assert res["issues"] == [] and "method_missing" in res["_meta"]["arg_warning"]
        with pytest.raises(ValueError, match="layer"):
            audit_helpers["find_unresolved_calls"](layer="нет_такого")

    def test_answer_is_json_safe_without_yo(self, audit_helpers):
        text = json.dumps(audit_helpers["find_unresolved_calls"](), ensure_ascii=False)
        assert "ё" not in text  # в фикстуре «ё» нет: любая «ё» пришла из текстов сервера
        for kw in ({"layer": "main"}, {"reasons": ["dynamic_name"]}, {"limit": "x"}):
            assert "ё" not in json.dumps(audit_helpers["find_unresolved_calls"](**kw), ensure_ascii=False)

    def test_ext_pass_is_cached_across_arguments(self, tmp_path, monkeypatch):
        import rlm_tools_bsl.bsl_helpers as bh

        calls = {"n": 0}
        real = bh._extract_calls_from_body

        def spy(*a, **kw):
            calls["n"] += 1
            return real(*a, **kw)

        monkeypatch.setattr(bh, "_extract_calls_from_body", spy)
        bsl, reader, _cf = _audit_session(tmp_path)
        try:
            bsl["find_unresolved_calls"]()
            first = calls["n"]
            assert first > 0
            bsl["find_unresolved_calls"](layer="extensions", reasons=["method_missing"])
            bsl["find_unresolved_calls"](path="../cfe/Расш1")
            assert calls["n"] == first
        finally:
            reader.close()

    def test_open_session_sees_index_update(self, tmp_path):
        bsl, reader, cf = _audit_session(tmp_path)
        try:
            before = bsl["find_unresolved_calls"](layer="main", reasons=["method_missing"])
            assert ("ОМ.Удалена", "module") in {(i["target"], i["kind"]) for i in before["issues"]}
            mod = cf / "CommonModules" / "ОМ" / "Ext" / "Module.bsl"
            mod.write_text(
                AUDIT_MAIN["CommonModules/ОМ/Ext/Module.bsl"] + "\nПроцедура Удалена() Экспорт\nКонецПроцедуры\n",
                encoding="utf-8-sig",
            )
            st = mod.stat()
            os.utime(mod, (st.st_atime + 5, st.st_mtime + 5))
            IndexBuilder().update(str(cf))
            after = bsl["find_unresolved_calls"](layer="main", reasons=["method_missing"])
            assert "ОМ.Удалена" not in {i["target"] for i in after["issues"]}
        finally:
            reader.close()

    def test_open_session_sees_module_closed_after_update(self, tmp_path):
        # Исходник общего модуля заменен поставкой без исходника (Module.bin) и индекс обновлен:
        # та же сессия обязана увидеть закрытый модуль, а не «модуля нет».
        main = {
            **AUDIT_MAIN,
            "CommonModules/Сменный/Ext/Module.bsl": "Процедура Метод() Экспорт\nКонецПроцедуры\n",
            "CommonModules/Проба/Ext/Module.bsl": (
                'Процедура П() Экспорт\n    ФоновыеЗадания.Выполнить("Сменный.Метод");\nКонецПроцедуры\n'
            ),
        }
        bsl, reader, cf = _audit_session(tmp_path, main)
        try:
            before = bsl["find_unresolved_calls"](layer="main", path="CommonModules/Проба")
            assert before["issues"] == [] and before["partial"] is False
            src = cf / "CommonModules" / "Сменный" / "Ext" / "Module.bsl"
            src.unlink()
            (src.parent / "Module.bin").write_bytes(b"closed")
            IndexBuilder().update(str(cf))
            after = bsl["find_unresolved_calls"](layer="main", path="CommonModules/Проба")
            assert after["issues"] == []
            assert after["partial"] is True and "target_catalog_unproven" in after["_meta"]["reasons"]
        finally:
            reader.close()


# ── Задача 6: сигнатура длиннее 20 строк (Д5) ───────────────────────────────────

LONG_SIG = (
    ["Процедура Длинная("]
    + [f"    Параметр{i}{',' if i < 23 else ''}" for i in range(1, 24)]
    + [") Экспорт", "    Возврат;", "КонецПроцедуры", "", "Процедура Следующая()", "КонецПроцедуры"]
)


def _live_helpers_with_module(tmp_path, rel, content):
    """CF-проект без индекса с одним модулем — как фикстура `helpers` в test_parse_multiline_signature."""
    from rlm_tools_bsl.bsl_helpers import make_bsl_helpers
    from rlm_tools_bsl.format_detector import detect_format
    from rlm_tools_bsl.helpers import make_helpers

    cf = tmp_path / "src" / "cf"
    _write_tree(
        cf,
        {
            "Configuration.xml": (
                "<MetaDataObject><Configuration><Properties><Name>X</Name></Properties></Configuration>"
                "</MetaDataObject>"
            ),
            rel: content,
        },
    )
    generic, resolve_safe = make_helpers(str(cf))
    return make_bsl_helpers(
        base_path=str(cf),
        resolve_safe=resolve_safe,
        read_file_fn=generic["read_file"],
        grep_fn=generic["grep"],
        glob_files_fn=generic["glob_files"],
        format_info=detect_format(str(cf)),
        idx_reader=None,
    )


class TestLongSignature:
    def test_24_line_signature_found_by_indexer(self):
        procs = {p["name"]: p for p in BI._parse_procedures_from_lines(LONG_SIG)}
        assert procs["Длинная"]["line"] == 1 and procs["Длинная"]["end_line"] == 27
        assert procs["Длинная"]["is_export"] is True
        assert "Параметр23" in procs["Длинная"]["params"]
        assert "Следующая" in procs

    def test_live_and_index_agree(self, tmp_path):
        rel = "CommonModules/М/Ext/Module.bsl"
        bsl = _live_helpers_with_module(tmp_path, rel, "\n".join(LONG_SIG))
        live = [(p["name"], p["line"], p["end_line"]) for p in bsl["extract_procedures"](rel)]
        idx = [(p["name"], p["line"], p["end_line"]) for p in BI._parse_procedures_from_lines(LONG_SIG)]
        assert live == idx == [("Длинная", 1, 27), ("Следующая", 29, 30)]

    def test_unbalanced_paren_does_not_swallow_next_declaration(self):
        lines = ["Процедура Битая(А,", "    Б", "", "Процедура Целая()", "КонецПроцедуры"]
        names = [p["name"] for p in BI._parse_procedures_from_lines(lines)]
        assert "Целая" in names  # на прежнем коде склейка съедала объявление «Целая»


# ── Задача 7: шапки модулей под лицензией (Д6) ──────────────────────────────────

LICENSE = ["////////////////", "// Copyright (c) 2024, ООО 1С-Софт", "// Все права защищены.", "////////////////"]
HEADER_FILES = {
    "CommonModules/А/Ext/Module.bsl": "// Текст шапки\nПроцедура А()\nКонецПроцедуры\n",
    "CommonModules/Б/Ext/Module.bsl": "\n".join(
        LICENSE
        + ["", '// Подсистема "Бизнес-сеть".', "// ОбщийМодуль.БизнесСеть.", "", "Процедура Б()", "КонецПроцедуры", ""]
    ),
    "CommonModules/В/Ext/Module.bsl": "\n".join(LICENSE + ["", "Процедура В()", "КонецПроцедуры", ""]),
}


@pytest.fixture
def headers_built(tmp_path):
    _write_project(tmp_path, HEADER_FILES)
    db = IndexBuilder().build(str(tmp_path), build_calls=False)
    bsl, reader = _make_bsl_for(tmp_path, db)
    yield bsl
    reader.close()


class TestModuleHeaders:
    def test_license_only(self):
        assert BI._extract_header_comment(LICENSE + ["", "#Область ПрограммныйИнтерфейс"]) == ""

    def test_block_under_license(self):
        lines = LICENSE + ["", '// Подсистема "Бизнес-сеть".', "// ОбщийМодуль.БизнесСеть.", "", "Процедура А()"]
        assert BI._extract_header_comment(lines) == 'Подсистема "Бизнес-сеть".\nОбщийМодуль.БизнесСеть.'

    def test_if_directive_does_not_stop(self):
        lines = ["#Если Сервер Или ТолстыйКлиентОбычноеПриложение Тогда", "", "// Шапка модуля", "Процедура А()"]
        assert BI._extract_header_comment(lines) == "Шапка модуля"

    def test_region_stops_search_after_license(self):
        lines = LICENSE + ["", "#Область Служебные", "// Описание процедуры", "Процедура А()"]
        assert BI._extract_header_comment(lines) == ""

    def test_no_leading_comment(self):
        assert BI._extract_header_comment(["Процедура А()", "КонецПроцедуры"]) is None
        assert BI._extract_header_comment([]) is None

    def test_marker_block_is_not_header(self):
        # ЕРП: лицензия, #Если, маркер условной сборки, затем область — шапки с текстом нет.
        lines = LICENSE + [
            "",
            "#Если Сервер Или ТолстыйКлиентОбычноеПриложение Тогда",
            "",
            "//++ Устарело_Производство21",
        ]
        assert BI._extract_header_comment(lines + ["", "#Область ПрограммныйИнтерфейс"]) == ""
        assert BI._extract_header_comment(["//++ НЕ УТ", "Процедура А()", "КонецПроцедуры"]) is None

    @pytest.mark.parametrize(
        "marker",
        [
            "//++ НЕ УТ",
            "// ++ Локализация",
            "//+++ Доработка; Иванов; 01.07.2024; № 1",
            " // +++ Доработка",
            "//++",
            "//-- НЕ УТ",
            "//--- Доработка",
            "//-----Доработка Иванов 03.02.2025",
        ],
    )
    def test_marker_block_skipped_next_block_taken(self, marker):
        lines = [marker, "", "// Модуль выгрузки платежей", "Процедура А()"]
        assert BI._extract_header_comment(lines) == "Модуль выгрузки платежей"

    def test_leading_marker_lines_dropped_from_block(self):
        lines = ["//+++ Доработка; Иванов; 10.12.2024; № 2", "//", "// Механизм корректировки долей", "Процедура А()"]
        assert BI._extract_header_comment(lines) == "Механизм корректировки долей"

    def test_list_items_inside_header_kept(self):
        # Подпункты `--` и пункты `-`/`+` внутри шапки — текст, а не маркеры: срезается только начало блока.
        block = [
            "// Общие требования:",
            "// - период кратен месяцу",
            "//   -- НачалоПериода",
            "// -- КонецПериода",
            "//+ правка",
        ]
        expected = "Общие требования:\n- период кратен месяцу\n  -- НачалоПериода\n-- КонецПериода\n+ правка"
        assert BI._extract_header_comment(block + ["Процедура А()"]) == expected
        assert BI._extract_header_comment(["// - первый пункт", "Процедура А()"]) == "- первый пункт"
        assert BI._extract_header_comment(["//   -- подпункт", "Процедура А()"]) == "  -- подпункт"
        assert BI._extract_header_comment(["//", "// Описание", "Процедура А()"]) == "\nОписание"

    def test_index_skips_marker_blocks(self, tmp_path):
        files = {
            "CommonModules/Г/Ext/Module.bsl": "\n".join(
                LICENSE + ["", "#Если Сервер Тогда", "//++ НЕ УТ", "Процедура Г()", "КонецПроцедуры", "#КонецЕсли", ""]
            ),
            "CommonModules/Д/Ext/Module.bsl": "//++ НЕ УТ\nПроцедура Д()\nКонецПроцедуры\n//-- НЕ УТ\n",
        }
        _write_project(tmp_path, files)
        db = IndexBuilder().build(str(tmp_path), build_calls=False)
        bsl, reader = _make_bsl_for(tmp_path, db)
        try:
            assert bsl["search_module_headers"]("", count_only=True)["total"] == 1  # Г: лицензия; Д: шапки нет
            assert bsl["search_module_headers"]("НЕ УТ", limit=10) == []
        finally:
            reader.close()

    def test_index_counts_license_only_and_lists_text_first(self, headers_built):
        helpers = headers_built
        assert helpers["search_module_headers"]("", count_only=True)["total"] == 3  # текст, лицензия+блок, лицензия
        rows = helpers["search_module_headers"]("", limit=10)
        assert [bool(r["header_comment"]) for r in rows] == [True, True, False]
        assert helpers["search_module_headers"]("Бизнес-сеть", limit=10)[0]["header_comment"].startswith("Подсистема")


# ── Задача 8: EDT — типы параметров команд объектов (Д7) ───────────────────────

_MDCLASS = 'xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass"'


def _edt_mdo(kind, name, body=""):
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<mdclass:{kind} {_MDCLASS} uuid="00000000-0000-0000-0000-0000000000aa">\n'
        f"  <name>{name}</name>\n{body}</mdclass:{kind}>\n"
    )


def _edt_command(name, *types):
    if not types:
        return f"  <commands>\n    <name>{name}</name>\n    <commandParameterType/>\n  </commands>\n"
    inner = "".join(f"<types>{t}</types>" for t in types)
    return f"  <commands>\n    <name>{name}</name>\n    <commandParameterType>{inner}</commandParameterType>\n  </commands>\n"


MDO_REL = "DataProcessors/Настройка/Настройка.mdo"
MDO_TEXT = _edt_mdo(
    "DataProcessor",
    "Настройка",
    _edt_command("Первая") + _edt_command("Вторая", "CatalogRef.А", "ChartOfCharacteristicTypesRef.Б"),
)
EDT_CMD_FILES = {
    MDO_REL: MDO_TEXT,
    "DataProcessors/Настройка/Commands/Вторая/CommandModule.bsl": (
        "&НаКлиенте\nПроцедура ОбработкаКоманды(ПараметрКоманды, ПараметрыВыполненияКоманды)\nКонецПроцедуры\n"
    ),
    "DataProcessors/БезМодуля/БезМодуля.mdo": _edt_mdo(
        "DataProcessor", "БезМодуля", _edt_command("КомандаБезМодуля", "CatalogRef.А")
    ),
    "Catalogs/А/А.mdo": _edt_mdo("Catalog", "А"),
    "ChartsOfCharacteristicTypes/Б/Б.mdo": _edt_mdo("ChartOfCharacteristicTypes", "Б"),
}
CF_CMD_FILES = {
    "DataProcessors/Настройка.xml": _cf_object_xml("DataProcessor", "Настройка"),
    "DataProcessors/Настройка/Commands/Вторая.xml": (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses" xmlns:v8="http://v8.1c.ru/8.1/data/core" '
        'xmlns:cfg="http://v8.1c.ru/8.1/data/enterprise/current-config">\n'
        '  <Command uuid="00000000-0000-0000-0000-0000000000bb"><Properties><Name>Вторая</Name>'
        "<CommandParameterType><v8:Type>cfg:CatalogRef.А</v8:Type></CommandParameterType>"
        "</Properties></Command>\n"
        "</MetaDataObject>\n"
    ),
    "Catalogs/А.xml": _cf_object_xml("Catalog", "А"),
    # сборка без единого модуля метаданные не пишет — модуль, как в любой выгрузке
    "DataProcessors/Настройка/Commands/Вторая/Ext/CommandModule.bsl": (
        "&НаКлиенте\nПроцедура ОбработкаКоманды(ПараметрКоманды, ПараметрыВыполненияКоманды)\nКонецПроцедуры\n"
    ),
}


def _refs(built, ref_object, kind):
    with _conn(built[0]) as c:
        rows = c.execute(
            "SELECT used_in, path FROM metadata_references WHERE ref_object = ? AND ref_kind = ?",
            (ref_object, kind),
        ).fetchall()
    return sorted((r[0], r[1]) for r in rows)


def _refs_snapshot(db):
    with _conn(db) as c:
        rows = c.execute(
            "SELECT source_object, source_category, ref_object, ref_kind, used_in, path, line FROM metadata_references"
        ).fetchall()
    return sorted((tuple(r) for r in rows), key=lambda t: tuple("" if v is None else str(v) for v in t))


@pytest.fixture
def edt_cmd_built(tmp_path):
    _write_edt_project(tmp_path, EDT_CMD_FILES)
    return IndexBuilder().build(str(tmp_path), build_calls=True), tmp_path


@pytest.fixture
def cf_cmd_built(tmp_path):
    _write_project(tmp_path, CF_CMD_FILES)
    return IndexBuilder().build(str(tmp_path), build_calls=True), tmp_path


def _attr_catalog(name, *commands):
    attrs = "  <attributes>\n    <name>Реквизит</name>\n    <type><types>String</types></type>\n  </attributes>\n"
    return _edt_mdo("Catalog", name, attrs + "".join(commands))


class TestEdtObjectCommands:
    def test_parser(self):
        from rlm_tools_bsl.bsl_xml_parsers import parse_object_commands_parameter_types

        assert parse_object_commands_parameter_types(MDO_TEXT) == [
            {"command_name": "Вторая", "ref_object": "Catalog.А"},
            {"command_name": "Вторая", "ref_object": "ChartOfCharacteristicTypes.Б"},
        ]

    def test_parser_accepts_namespaced_command_tag(self):
        from rlm_tools_bsl.bsl_xml_parsers import parse_object_commands_parameter_types

        mdo = (
            '<mdclass:DataProcessor xmlns:mdclass="urn:edt">'
            "<mdclass:commands><mdclass:name>Вторая</mdclass:name>"
            "<mdclass:commandParameterType><mdclass:types>CatalogRef.А</mdclass:types>"
            "</mdclass:commandParameterType></mdclass:commands></mdclass:DataProcessor>"
        )
        assert parse_object_commands_parameter_types(mdo) == [{"command_name": "Вторая", "ref_object": "Catalog.А"}]

    def test_build_emits_refs(self, edt_cmd_built):
        assert _refs(edt_cmd_built, "Catalog.А", "command_parameter_type") == [
            (
                "DataProcessor.БезМодуля.Command.КомандаБезМодуля.CommandParameterType",
                "DataProcessors/БезМодуля/БезМодуля.mdo",
            ),
            ("DataProcessor.Настройка.Command.Вторая.CommandParameterType", "DataProcessors/Настройка/Настройка.mdo"),
        ]
        assert _refs(edt_cmd_built, "ChartOfCharacteristicTypes.Б", "command_parameter_type") == [
            ("DataProcessor.Настройка.Command.Вторая.CommandParameterType", "DataProcessors/Настройка/Настройка.mdo"),
        ]

    def test_owner_command_without_module_or_commands_dir(self, edt_cmd_built):
        assert (
            "DataProcessor.БезМодуля.Command.КомандаБезМодуля.CommandParameterType",
            "DataProcessors/БезМодуля/БезМодуля.mdo",
        ) in _refs(edt_cmd_built, "Catalog.А", "command_parameter_type")

    def test_update_equals_build_after_owner_mdo_change(self, tmp_path_factory):
        files_b = dict(EDT_CMD_FILES)
        files_b[MDO_REL] = files_b[MDO_REL].replace("<types>CatalogRef.А</types>", "")
        da = tmp_path_factory.mktemp("upd")
        _write_edt_project(da, EDT_CMD_FILES)
        db = IndexBuilder().build(str(da), build_calls=True)
        _apply_changes(da, EDT_CMD_FILES, files_b)
        IndexBuilder().update(str(da))
        dfresh = tmp_path_factory.mktemp("fresh")
        _write_edt_project(dfresh, files_b)
        assert _refs_snapshot(db) == _refs_snapshot(IndexBuilder().build(str(dfresh), build_calls=True))
        assert _refs((db, None), "Catalog.А", "command_parameter_type") == [
            (
                "DataProcessor.БезМодуля.Command.КомандаБезМодуля.CommandParameterType",
                "DataProcessors/БезМодуля/БезМодуля.mdo",
            )
        ]

    def test_update_without_commands_dir_equals_build(self, tmp_path_factory):
        files_b = dict(EDT_CMD_FILES)
        rel = "DataProcessors/БезМодуля/БезМодуля.mdo"
        files_b[rel] = files_b[rel].replace("<types>CatalogRef.А</types>", "")
        da = tmp_path_factory.mktemp("upd-no-command-module")
        _write_edt_project(da, EDT_CMD_FILES)
        db = IndexBuilder().build(str(da), build_calls=True)
        _apply_changes(da, EDT_CMD_FILES, files_b)
        IndexBuilder().update(str(da))
        dfresh = tmp_path_factory.mktemp("fresh-no-command-module")
        _write_edt_project(dfresh, files_b)
        assert _refs_snapshot(db) == _refs_snapshot(IndexBuilder().build(str(dfresh), build_calls=True))

    def test_cf_object_command_file_unchanged(self, cf_cmd_built):
        assert _refs(cf_cmd_built, "Catalog.А", "command_parameter_type") == [
            (
                "DataProcessor.Настройка.Command.Вторая.CommandParameterType",
                "DataProcessors/Настройка/Commands/Вторая.xml",
            )
        ]

    def test_pointwise_update_owner_mdo_equals_build(self, tmp_path, tmp_path_factory, monkeypatch):
        from test_git_delta import _git, _git_init

        root = tmp_path / "repo"
        base = root / "src"
        files_a = {
            "Catalogs/Владелец/Владелец.mdo": _attr_catalog("Владелец", _edt_command("Открыть", "CatalogRef.Цель1")),
            "Catalogs/Второй/Второй.mdo": _attr_catalog("Второй"),
            "Catalogs/Третий/Третий.mdo": _attr_catalog("Третий"),
            "Catalogs/Цель1/Цель1.mdo": _attr_catalog("Цель1"),
            "Catalogs/Цель2/Цель2.mdo": _attr_catalog("Цель2"),
            "CommonModules/М/Module.bsl": "Процедура П() Экспорт\nКонецПроцедуры\n",
        }
        files_b = dict(files_a)
        files_b["Catalogs/Владелец/Владелец.mdo"] = files_a["Catalogs/Владелец/Владелец.mdo"].replace(
            "CatalogRef.Цель1", "CatalogRef.Цель2"
        )
        _write_edt_project(base, files_a)
        _git_init(root)
        db = IndexBuilder().build(str(base), build_calls=True)
        _apply_changes(base, files_a, files_b)
        _git(root, "add", "-A")
        _git(root, "commit", "-m", "owner mdo")
        calls = []
        real = BI._refresh_object

        def spy(conn, base_path, category, object_name, opt):
            calls.append((category, object_name))
            return real(conn, base_path, category, object_name, opt)

        monkeypatch.setattr(BI, "_refresh_object", spy)
        res = IndexBuilder().update(str(base))
        assert res["git_fast_path"] is True, res
        assert ("Catalogs", "Владелец") in calls
        fresh = tmp_path_factory.mktemp("fresh-pw")
        _write_edt_project(fresh, files_b)
        assert _refs_snapshot(db) == _refs_snapshot(IndexBuilder().build(str(fresh), build_calls=True))
        used = "Catalog.Владелец.Command.Открыть.CommandParameterType"
        assert _refs((db, None), "Catalog.Цель1", "command_parameter_type") == []
        assert _refs((db, None), "Catalog.Цель2", "command_parameter_type") == [
            (used, "Catalogs/Владелец/Владелец.mdo")
        ]

    @pytest.mark.parametrize("category,kind", [("DataProcessors", "DataProcessor"), ("Reports", "Report")])
    def test_bulk_fallback_owner_mdo_without_commands_dir(
        self, tmp_path, tmp_path_factory, monkeypatch, category, kind
    ):
        from test_git_delta import _git, _git_init

        root = tmp_path / "repo"
        base = root / "src"
        owner_rel = f"{category}/Владелец/Владелец.mdo"
        files_a = {
            owner_rel: _edt_mdo(kind, "Владелец", _edt_command("Открыть", "CatalogRef.А")),
            "Catalogs/А/А.mdo": _attr_catalog("А"),
            "Catalogs/Б/Б.mdo": _attr_catalog("Б", _edt_command("Своя", "CatalogRef.А")),
            "CommonModules/М/Module.bsl": "Процедура П() Экспорт\nКонецПроцедуры\n",
        }
        files_b = dict(files_a)
        files_b[owner_rel] = files_a[owner_rel].replace("CatalogRef.А", "CatalogRef.Б")
        _write_edt_project(base, files_a)
        _git_init(root)
        db = IndexBuilder().build(str(base), build_calls=True)
        _apply_changes(base, files_a, files_b)
        _git(root, "add", "-A")
        _git(root, "commit", "-m", "owner command type")
        refreshed, collected = [], []
        real_refresh, real_collect = BI._refresh_object, BI._collect_metadata_tables

        def spy_refresh(conn, base_path, cat, object_name, opt):
            refreshed.append(cat)
            return real_refresh(conn, base_path, cat, object_name, opt)

        def spy_collect(base_path, **kw):
            collected.append(kw.get("collect_metadata_refs_categories"))
            return real_collect(base_path, **kw)

        monkeypatch.setattr(BI, "_refresh_object", spy_refresh)
        monkeypatch.setattr(BI, "_collect_metadata_tables", spy_collect)
        res = IndexBuilder().update(str(base))
        assert res["git_fast_path"] is True, res
        assert category not in refreshed  # не точечная категория
        assert {category} in collected  # выборочный коллектор этой категории
        fresh = tmp_path_factory.mktemp("fresh-bulk")
        _write_edt_project(fresh, files_b)
        assert _refs_snapshot(db) == _refs_snapshot(IndexBuilder().build(str(fresh), build_calls=True))
        used = f"{kind}.Владелец.Command.Открыть.CommandParameterType"
        assert _refs((db, None), "Catalog.Б", "command_parameter_type") == [(used, owner_rel)]
        # чужая категория не затронута: команда справочника Б на месте
        assert _refs((db, None), "Catalog.А", "command_parameter_type") == [
            ("Catalog.Б.Command.Своя.CommandParameterType", "Catalogs/Б/Б.mdo")
        ]
        # прямой выборочный коллектор тоже видит команду владельца
        tables = real_collect(str(base), collect_metadata_refs_categories={category})
        assert (
            "Владелец",
            category,
            "Catalog.Б",
            "command_parameter_type",
            used,
            owner_rel,
            None,
        ) in tables["metadata_references"]


# ── Задача 11: слой вызовов расширений в графе (Д8) ─────────────────────────────

import rlm_tools_bsl.bsl_helpers as bsl_helpers  # noqa: E402

OM = "CommonModules/ОМ/Ext/Module.bsl"
GLAVNY = "CommonModules/Главный/Ext/Module.bsl"
ORDER = "Documents/Заказ/Ext/ObjectModule.bsl"
NOM_MGR = "Catalogs/Номенклатура/Ext/ManagerModule.bsl"
EXT1_MOD = "../cfe/Расш1/CommonModules/Расш1_Модуль/Ext/Module.bsl"
EXT1_OM = "../cfe/Расш1/CommonModules/ОМ/Ext/Module.bsl"
EXT1_ORDER = "../cfe/Расш1/Documents/Заказ/Ext/ObjectModule.bsl"
EXT2_MOD = "../cfe/Расш2/CommonModules/Расш2_Модуль/Ext/Module.bsl"

OM_BSL = (
    "Процедура Цель() Экспорт\nКонецПроцедуры\n\n"
    "Процедура Фоновая() Экспорт\nКонецПроцедуры\n\n"
    "Процедура Промежуточная() Экспорт\n    Конечная();\nКонецПроцедуры\n\n"
    "Процедура Конечная() Экспорт\nКонецПроцедуры\n\n"
    "Процедура Удаленная() Экспорт\nКонецПроцедуры\n\n"
    "Процедура ДваИсточника() Экспорт\nКонецПроцедуры\n"
)
GLAVNY_BSL = (
    "Процедура ПервыйВызов() Экспорт\n    ОМ.ДваИсточника();\nКонецПроцедуры\n\n"
    "Процедура ВторойВызов() Экспорт\n    ОМ.ДваИсточника();\nКонецПроцедуры\n\n"
    "Процедура Локальная()\nКонецПроцедуры\n\n"
    "Процедура ВызовНесуществующей()\n    Несуществующая();\nКонецПроцедуры\n\n"
    "Процедура ВызовЛокальной()\n    Локальная();\nКонецПроцедуры\n"
)
ORDER_BSL = "Процедура ПередЗаписью(Отказ)\nКонецПроцедуры\n\nПроцедура ЗаполнитьСтроки()\nКонецПроцедуры\n"
NOM_MGR_BSL = "Функция НайтиПоАртикулу(А) Экспорт\nКонецФункции\n"
EXT1_MOD_BSL = (
    "Процедура Вызовы() Экспорт\n"
    "    ОМ.Цель();\n"
    '    ФоновыеЗадания.Выполнить("ОМ.Фоновая");\n'
    '    Справочники.Номенклатура.НайтиПоАртикулу("1");\n'
    "    ОМ.Промежуточная();\n"
    "    ОМ.ДобавленаВРасш1();\n"
    "    ОМ.ДваИсточника();\n"
    "    ОМ.ДваИсточника();\n"
    "    Локальная();\n"
    "КонецПроцедуры\n\n"
    "Процедура Локальная()\nКонецПроцедуры\n"
)
EXT1_OM_BSL = "Процедура ДобавленаВРасш1() Экспорт\nКонецПроцедуры\n"
EXT1_ORDER_BSL = (
    '&После("ПередЗаписью")\nПроцедура Расш1_ПередЗаписью(Отказ)\n    ЗаполнитьСтроки();\nКонецПроцедуры\n\n'
    '&ИзменениеИКонтроль("ЗаполнитьСтроки")\nПроцедура Расш1_ЗаполнитьСтроки()\n'
    "    #Удаление\n    ОМ.Удаленная();\n    #КонецУдаления\nКонецПроцедуры\n"
)
EXT2_MOD_BSL = (
    "Процедура Локальная()\nКонецПроцедуры\n\nПроцедура Вызов2()\n    Локальная();\n    Вызовы();\nКонецПроцедуры\n"
)
LINE_E1_TSEL = _line_of(EXT1_MOD_BSL, "ОМ.Цель();")
LINE_E1_LOCAL = _line_of(EXT1_MOD_BSL, "    Локальная();")

EXT_GRAPH_MAIN = {
    OM: OM_BSL,
    GLAVNY: GLAVNY_BSL,
    ORDER: ORDER_BSL,
    NOM_MGR: NOM_MGR_BSL,
    "CommonModules/ОМ.xml": _cm_descriptor("ОМ"),
    "CommonModules/Главный.xml": _cm_descriptor("Главный"),
    "Documents/Заказ.xml": _cf_object_xml("Document", "Заказ"),
    "Catalogs/Номенклатура.xml": _cf_object_xml("Catalog", "Номенклатура"),
}
EXT_GRAPH_EXT1 = {
    "CommonModules/Расш1_Модуль/Ext/Module.bsl": EXT1_MOD_BSL,
    "CommonModules/ОМ/Ext/Module.bsl": EXT1_OM_BSL,
    "Documents/Заказ/Ext/ObjectModule.bsl": EXT1_ORDER_BSL,
}
EXT_GRAPH_EXT2 = {"CommonModules/Расш2_Модуль/Ext/Module.bsl": EXT2_MOD_BSL}


def _write_ext_graph(root, main=None, ext1=None, ext2=None):
    cf = root / "src" / "cf"
    _write_project(cf, EXT_GRAPH_MAIN if main is None else main)
    for name, files in (
        ("Расш1", EXT_GRAPH_EXT1 if ext1 is None else ext1),
        ("Расш2", EXT_GRAPH_EXT2 if ext2 is None else ext2),
    ):
        _write_tree(root / "src" / "cfe" / name, {"Configuration.xml": _ext_descriptor(name), **files})
    return cf


def _ext_helpers(built, authoritative=True, with_ext=True):
    from rlm_tools_bsl.format_detector import detect_format
    from rlm_tools_bsl.helpers import make_helpers

    db, cf = built
    reader = IndexReader(db)
    exts = {}
    if with_ext:
        for name in ("Расш1", "Расш2"):
            exts[str(cf.parent / "cfe" / name)] = name
    generic, resolve_safe = make_helpers(str(cf), idx_reader=reader)
    return bsl_helpers.make_bsl_helpers(
        base_path=str(cf),
        resolve_safe=resolve_safe,
        read_file_fn=generic["read_file"],
        grep_fn=generic["grep"],
        glob_files_fn=generic["glob_files"],
        format_info=detect_format(str(cf)),
        idx_reader=reader,
        idx_zero_callers_authoritative=authoritative,
        extension_paths=list(exts),
        current_config_role="main",
        current_config_name="Тест",
        current_config_root=str(cf),
        extension_name_by_root=exts,
    )


def _helpers_without_extensions(built):
    return _ext_helpers(built, with_ext=False)


def _all_calls(db):
    with _conn(db) as c:
        rows = c.execute(
            "SELECT mod.rel_path, c.line, c.callee_name, c.callee_key, c.call_kind FROM calls c "
            "JOIN methods m ON m.id = c.caller_id JOIN modules mod ON mod.id = m.module_id"
        ).fetchall()
    return [tuple(r) for r in rows]


@pytest.fixture
def ext_graph_built(tmp_path):
    cf = _write_ext_graph(tmp_path)
    return IndexBuilder().build(str(cf), build_calls=True), cf


@pytest.fixture
def ext_helpers(ext_graph_built):
    return _ext_helpers(ext_graph_built)


@pytest.fixture
def ext_helpers_not_authoritative(ext_graph_built):
    return _ext_helpers(ext_graph_built, authoritative=False)


@pytest.fixture
def ext_helpers_v16(ext_graph_built):
    _downgrade_calls_to_v16(ext_graph_built[0])
    return _ext_helpers(ext_graph_built)


@pytest.fixture
def ext_graph_empty_main_calls(tmp_path):
    main = {OM: "Процедура Цель() Экспорт\nКонецПроцедуры\n", "CommonModules/ОМ.xml": _cm_descriptor("ОМ")}
    ext1 = {"CommonModules/Расш1_Модуль/Ext/Module.bsl": "Процедура Вызовы() Экспорт\n    ОМ.Цель();\nКонецПроцедуры\n"}
    cf = _write_ext_graph(tmp_path, main, ext1, {})
    db = IndexBuilder().build(str(cf), build_calls=True)
    with _conn(db) as c:
        assert c.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 0
        assert c.execute("SELECT value FROM index_meta WHERE key = 'has_calls'").fetchone()[0] == "1"
    return db, cf


@pytest.fixture
def duplicate_main_identity_built(tmp_path):
    files = {ORDER: ORDER_BSL, "Копия/Documents/Заказ/Ext/ObjectModule.bsl": ORDER_BSL}
    _write_project(tmp_path, files)
    return IndexBuilder().build(str(tmp_path), build_calls=True), tmp_path


class TestExtLayerResolve:
    def test_sql_nocase_folds_ascii_only(self):
        assert bsl_helpers._sql_nocase("AbC") == "abc"
        assert bsl_helpers._sql_nocase("Метод") != bsl_helpers._sql_nocase("метод")

    def test_row_predicate_mirrors_get_callers_sql(self, ext_graph_built):
        # строки calls основной конфигурации через _ext_row_matches — тот же набор (file, line), что get_callers
        db, _ = ext_graph_built
        rows = _all_calls(db)
        r = IndexReader(db)
        try:
            for proc, hint in (("Конечная", OM), ("ДваИсточника", OM), ("Конечная", ""), ("Локальная", "")):
                res = r.get_callers(proc, hint, 0, 1000)
                key = res["_meta"]["target_key"] if res["_meta"]["target_exact"] else None
                got = sorted(
                    (f, ln)
                    for f, ln, cn, k, kind in rows
                    if kind in (None, "background") and bsl_helpers._ext_row_matches(cn, k, proc, key)
                )
                assert got == sorted((c["file"], c["line"]) for c in res["callers"]), (proc, hint)
        finally:
            r.close()

    def test_resolve_order_own_extension_then_main(self):
        maps = {"common_exported": {("ом", "цель"): OM}, "managers": {}, "modules_by_identity": {}}
        ext = {
            "common_modules": {"ом": {"paths": [EXT1_OM], "methods": {"добавленаврасш1": True}}},
            "manager_modules": {},
        }
        caller = {
            "rel": EXT1_ORDER,
            "own_cf": {"расш1_передзаписью"},
            "counterpart": ORDER,
            "counterpart_cf": frozenset({"передзаписью", "заполнитьстроки"}),
        }
        f = bsl_helpers._ext_resolve_callee
        assert f("ОМ.ДобавленаВРасш1", None, None, caller, ext, maps) == _make_callee_key(EXT1_OM, "ДобавленаВРасш1")
        assert f("ОМ.Цель", None, None, caller, ext, maps) == _make_callee_key(OM, "Цель")
        assert f("ЗаполнитьСтроки", None, None, caller, ext, maps) == _make_callee_key(ORDER, "ЗаполнитьСтроки")
        assert f("Товары.Добавить", None, "member", caller, ext, maps) is None
        assert f("", None, "background_dynamic", caller, ext, maps) is None

    def test_ambiguous_main_counterpart_has_no_arbitrary_exact_key(self, duplicate_main_identity_built):
        db, _ = duplicate_main_identity_built
        r = IndexReader(db)
        try:
            maps = r.get_call_resolution_maps()
            assert maps["modules_by_identity"][("Documents", "заказ", "ObjectModule", "")] is None
        finally:
            r.close()

    def test_ambiguous_own_extension_module_has_no_arbitrary_exact_key(self):
        ext = {
            "common_modules": {
                "ом": {
                    "paths": [EXT1_OM, "../cfe/Расш1/copy/CommonModules/ОМ/Ext/Module.bsl"],
                    "methods": {"добавленаврасш1": True},
                }
            },
            "manager_modules": {
                ("Catalogs", "номенклатура"): {
                    "paths": [
                        "../cfe/Расш1/Catalogs/Номенклатура/Ext/ManagerModule.bsl",
                        "../cfe/Расш1/copy/Catalogs/Номенклатура/Ext/ManagerModule.bsl",
                    ],
                    "methods": {"метод": True},
                }
            },
        }
        maps = {"common_exported": {}, "managers": {}, "modules_by_identity": {}}
        caller = {"rel": EXT1_MOD, "own_cf": set(), "counterpart": None, "counterpart_cf": frozenset()}
        f = bsl_helpers._ext_resolve_callee
        assert f("ОМ.ДобавленаВРасш1", None, None, caller, ext, maps) is None
        assert f("Номенклатура.Метод", "Catalogs", None, caller, ext, maps) is None


class TestExtGraphCallers:
    def test_extension_hint_works_on_first_graph_call(self, ext_helpers):
        res = ext_helpers["find_callers_context"]("Локальная", EXT1_MOD)
        assert [(c["file"], c["line"]) for c in res["callers"]] == [(EXT1_MOD, LINE_E1_LOCAL)]

    def test_main_target_called_only_from_extension(self, ext_helpers):
        res = ext_helpers["find_callers_context"]("Цель", OM)
        assert [(c["file"], c["line"], c["caller_name"], c["edge_exact"], c["call_kind"]) for c in res["callers"]] == [
            (EXT1_MOD, LINE_E1_TSEL, "Вызовы", True, "call")
        ]
        m = res["_meta"]
        assert (m["total_callers"], m["exact_rows"], m["fallback_rows"]) == (1, 1, 0)
        assert (m["extension_rows"], m["extension_layer"]) == (1, "complete")

    def test_launch_and_manager_call_from_extension(self, ext_helpers):
        f = ext_helpers["find_callers_context"]
        assert [(c["file"], c["call_kind"]) for c in f("Фоновая", OM)["callers"]] == [(EXT1_MOD, "background")]
        assert [(c["file"], c["edge_exact"]) for c in f("НайтиПоАртикулу", NOM_MGR)["callers"]] == [(EXT1_MOD, True)]

    def test_borrowed_module_bare_call_is_exact_edge_to_main_module(self, ext_helpers):
        res = ext_helpers["find_callers_context"]("ЗаполнитьСтроки", ORDER)
        assert [(c["file"], c["caller_name"], c["edge_exact"]) for c in res["callers"]] == [
            (EXT1_ORDER, "Расш1_ПередЗаписью", True)
        ]

    def test_deleted_block_is_not_an_edge(self, ext_helpers):
        res = ext_helpers["find_callers_context"]("Удаленная", OM)
        assert res["callers"] == [] and res["_meta"]["extension_layer"] == "complete"

    def test_extension_target_is_answered_only_from_its_extension(self, ext_helpers):
        f = ext_helpers["find_callers_context"]
        loc = f("Локальная", EXT1_MOD)  # ни Главный.ВызовЛокальной, ни Расш2.Вызов2
        assert [(c["file"], c["line"]) for c in loc["callers"]] == [(EXT1_MOD, LINE_E1_LOCAL)]
        assert loc["_meta"]["target_key"] == _make_callee_key(EXT1_MOD, "Локальная")
        added = f("ДобавленаВРасш1", EXT1_OM)
        assert [(c["file"], c["edge_exact"]) for c in added["callers"]] == [(EXT1_MOD, True)]
        # «Вызовы()» в Расш2 без ключа: код другого расширения процедуру Расш1 вызвать не может
        none = f("Вызовы", EXT1_MOD)
        assert none["callers"] == [] and none["_meta"]["hint"].startswith("Вызывающих нет")

    def test_missing_declared_extension_target_does_not_get_name_fallback(self, ext_helpers):
        res = ext_helpers["find_callers_context"]("Несуществующая", EXT1_MOD)
        assert res["callers"] == [] and res["_meta"]["target_exact"] is False

    def test_empty_main_calls_still_merges_extension_layer(self, ext_graph_empty_main_calls):
        helpers = _ext_helpers(ext_graph_empty_main_calls)
        res = helpers["find_callers_context"]("Цель", OM)
        assert [(c["file"], c["edge_exact"]) for c in res["callers"]] == [(EXT1_MOD, True)]
        assert res["_meta"]["extension_layer"] == "complete"

    def test_page_spans_index_and_extension_rows(self, ext_helpers):
        f = ext_helpers["find_callers_context"]
        p1, p2 = f("ДваИсточника", OM, 0, 3), f("ДваИсточника", OM, 3, 3)
        assert [c["file"] for c in p1["callers"]] == [GLAVNY, GLAVNY, EXT1_MOD]
        assert [c["file"] for c in p2["callers"]] == [EXT1_MOD]
        assert (p1["_meta"]["total_callers"], p1["_meta"]["extension_rows"]) == (4, 2)
        assert (p1["_meta"]["has_more"], p2["_meta"]["has_more"]) == (True, False)
        m = p1["_meta"]
        assert m["exact_rows"] + m["fallback_rows"] == m["total_callers"]

    def test_name_search_without_hint_includes_extension_rows(self, ext_helpers):
        res = ext_helpers["find_callers_context"]("Локальная")
        assert res["_meta"]["target_exact"] is False
        assert sorted(c["file"] for c in res["callers"]) == sorted([GLAVNY, EXT1_MOD, EXT2_MOD])
        assert not any(c["edge_exact"] for c in res["callers"])

    def test_unresolved_object_hint_does_not_apply_layer(self, ext_helpers):
        res = ext_helpers["find_callers_context"]("Цель", "НетТакогоОбъекта")
        assert res["callers"] == [] and res["_meta"]["extension_layer"] == "not_applied"

    def test_zero_limit_keeps_positive_extension_count(self, ext_helpers):
        res = ext_helpers["find_callers_context"]("Цель", OM, limit=0)
        assert res["callers"] == [] and res["_meta"]["total_callers"] == 1
        assert res["_meta"]["extension_rows"] == 1 and res["_meta"]["has_more"] is True
        assert "No callers found" not in res["_meta"].get("hint", "")


PUSK = "CommonModules/Пуск/Ext/Module.bsl"
EXT1_OWN = "../cfe/Расш1/CommonModules/Расш1_Свое/Ext/Module.bsl"
EXT1_OWN_BSL = (
    "Процедура Цель() Экспорт\nКонецПроцедуры\n\nПроцедура СвоиВызовы() Экспорт\n    Цель();\nКонецПроцедуры\n"
)


class TestExtGraphNamesake:
    """Имя уникально в основной конфигурации, но одноименная процедура объявлена в расширении: без
    подсказки цель неоднозначна — поиск по имени, а не точный ключ main (иначе вызывающие процедуры
    расширения теряются, а промах find_path выглядит окончательным)."""

    def _helpers(self, tmp_path, empty_main_graph=False):
        if empty_main_graph:
            main = {OM: "Процедура Цель() Экспорт\nКонецПроцедуры\n", "CommonModules/ОМ.xml": _cm_descriptor("ОМ")}
            ext1 = {
                "CommonModules/Расш1_Модуль/Ext/Module.bsl": "Процедура Вызовы() Экспорт\n    ОМ.Цель();\nКонецПроцедуры\n"
            }
        else:
            main = {**EXT_GRAPH_MAIN, PUSK: "Процедура Пуск() Экспорт\n    ОМ.Цель();\nКонецПроцедуры\n"}
            ext1 = dict(EXT_GRAPH_EXT1)
        ext1["CommonModules/Расш1_Свое/Ext/Module.bsl"] = EXT1_OWN_BSL
        cf = _write_ext_graph(tmp_path, main, ext1, {})
        return _ext_helpers((IndexBuilder().build(str(cf), build_calls=True), cf))

    @pytest.mark.parametrize("empty_main_graph", [False, True])
    def test_name_search_keeps_callers_of_extension_namesake(self, tmp_path, empty_main_graph):
        res = self._helpers(tmp_path, empty_main_graph)["find_callers_context"]("Цель")
        expected = [(EXT1_MOD, "Вызовы"), (EXT1_OWN, "СвоиВызовы")] + ([] if empty_main_graph else [(PUSK, "Пуск")])
        assert sorted((c["file"], c["caller_name"]) for c in res["callers"]) == sorted(expected)
        m = res["_meta"]
        assert m["target_exact"] is False and not any(c["edge_exact"] for c in res["callers"])
        assert m["extension_rows"] == 2 and m["exact_rows"] + m["fallback_rows"] == m["total_callers"] == len(expected)

    def test_main_hint_keeps_exact_main_target(self, tmp_path):
        res = self._helpers(tmp_path)["find_callers_context"]("Цель", OM)
        assert sorted(c["file"] for c in res["callers"]) == sorted([PUSK, EXT1_MOD])
        assert res["_meta"]["target_exact"] is True and all(c["edge_exact"] for c in res["callers"])

    def test_find_path_without_hint_reports_extension_namesake(self, tmp_path):
        h = self._helpers(tmp_path)
        p = h["find_path"]("СвоиВызовы", "Цель", from_hint=EXT1_OWN)
        assert p["found"] is False and "error" in p and p["_meta"]["ambiguous_arg"] == "to"
        assert {OM, EXT1_OWN} <= {c["file"] for c in p["candidates"]}
        exact = h["find_path"]("СвоиВызовы", "Цель", from_hint=EXT1_OWN, to_hint=EXT1_OWN)
        assert exact["found"] is True and exact["_meta"]["precision"] == "exact"

    def test_find_path_end_without_hint_is_not_pinned_to_main(self, tmp_path):
        # Пустой граф main: защита неоднозначности выключена (строк calls нет), но конец без подсказки
        # все равно не закрепляется за точным ключом main — путь ведет к процедуре расширения.
        p = self._helpers(tmp_path, empty_main_graph=True)["find_path"]("СвоиВызовы", "Цель", from_hint=EXT1_OWN)
        assert p["found"] is True and p["_meta"]["precision"] == "heuristic"
        assert p["_meta"]["to_exact"] is False and p["_meta"]["to_key"] is None


class TestExtGraphTraversal:
    def test_find_path_from_extension_method_is_exact(self, ext_helpers):
        p = ext_helpers["find_path"]("Вызовы", "Конечная", from_hint=EXT1_MOD, to_hint=OM)
        assert p["found"] is True and p["_meta"]["precision"] == "exact"
        assert [(e["name"], e["module_path"], e["call_kind"]) for e in p["path"]] == [
            ("Вызовы", EXT1_MOD, "call"),
            ("Промежуточная", OM, "call"),
            ("Конечная", OM, None),
        ]
        assert p["_meta"]["from_key"] == _make_callee_key(EXT1_MOD, "Вызовы")
        assert p["_meta"]["extension_layer"] == "complete"

    def test_find_path_through_launch_from_extension(self, ext_helpers):
        p = ext_helpers["find_path"]("Вызовы", "Фоновая", from_hint=EXT1_MOD, to_hint=OM)
        assert [e["call_kind"] for e in p["path"]] == ["background", None]

    def test_missing_method_in_explicit_extension_from_hint_is_error(self, ext_helpers):
        p = ext_helpers["find_path"]("Вызов2", "Локальная", from_hint=EXT1_MOD, to_hint=EXT2_MOD)
        assert p["found"] is False and "error" in p
        assert p["_meta"]["nodes_expanded"] == 0
        same = ext_helpers["find_path"]("Вызов2", "Вызов2", from_hint=EXT1_MOD, to_hint=EXT2_MOD)
        assert same["found"] is False and "error" in same  # и тривиальный путь не обходит гард

    def test_hierarchy_descends_into_extension_without_main_triggers(self, tmp_path):
        # Обработчик события формы main назван так же, как процедура расширения: без правила
        # узла расширения get_inbound_edges сравнил бы имя со ВСЕМИ обработчиками форм main.
        form = "Documents/Заказ/Forms/ФормаДокумента"
        main = {
            **EXT_GRAPH_MAIN,
            f"{form}/Ext/Form.xml": (
                '<?xml version="1.0" encoding="UTF-8"?>\n<Form xmlns="http://v8.1c.ru/8.3/xcf/logform">'
                '<Events><Event name="OnOpen">Вызовы</Event></Events></Form>\n'
            ),
            f"{form}/Ext/Form/Module.bsl": "&НаКлиенте\nПроцедура Вызовы(Отказ)\nКонецПроцедуры\n",
        }
        cf = _write_ext_graph(tmp_path, main)
        db = IndexBuilder().build(str(cf), build_calls=True)
        r = IndexReader(db)
        try:
            assert r.get_inbound_edges("Вызовы", module_hint=EXT1_MOD)  # предусловие: ловушка есть
        finally:
            r.close()
        ext_helpers = _ext_helpers((db, cf))
        t = ext_helpers["find_call_hierarchy"]("Конечная", depth=3, module_hint=OM, include_triggers=True)
        nodes = {n["name"]: n for n in t["tree"]}
        assert ("Вызовы", EXT1_MOD) in [(c["caller_name"], c["module_path"]) for c in nodes["Промежуточная"]["callers"]]
        assert nodes["Вызовы"]["triggers"] == []
        assert t["_meta"]["extension_layer"] == "complete"


class TestExtGraphGuards:
    def test_transient_resolution_maps_give_partial_empty_layer(self, ext_graph_built, monkeypatch):
        monkeypatch.setattr(IndexReader, "get_call_resolution_maps", lambda self: None)
        helpers = _ext_helpers(ext_graph_built)
        res = helpers["find_callers_context"]("Цель", OM)
        assert res["callers"] == [] and res["_meta"]["extension_layer"] == "partial"

    def test_call_rows_probe_error_keeps_fs_fallback(self, ext_graph_empty_main_calls, monkeypatch):
        monkeypatch.setattr(IndexReader, "probe_call_rows", lambda self: None)
        helpers = _ext_helpers(ext_graph_empty_main_calls)
        res = helpers["find_callers_context"]("Цель", OM)
        assert "extension_layer" not in res["_meta"]  # отказ чтения не стал авторитетным нулем

    def test_stale_index_zero_keeps_fs_fallback(self, ext_helpers_not_authoritative):
        res = ext_helpers_not_authoritative["find_callers_context"]("Цель", OM)
        assert res["_meta"]["exact_available"] is False and "extension_layer" not in res["_meta"]
        assert [c["file"] for c in res["callers"]] == [EXT1_MOD]  # прежний FS-фолбэк видит расширения по имени

    def test_no_extensions_no_layer(self, ext_graph_built, monkeypatch):
        seen = []
        monkeypatch.setattr(bsl_helpers, "_ext_resolve_callee", lambda *a, **k: seen.append(a))
        res = _helpers_without_extensions(ext_graph_built)["find_callers_context"]("Цель", OM)
        assert "extension_layer" not in res["_meta"] and seen == []

    def test_v16_index_keeps_previous_answer(self, ext_helpers_v16):
        res = ext_helpers_v16["find_callers_context"]("Цель", OM)
        assert res["callers"] == [] and "extension_layer" not in res["_meta"]

    def test_call_rows_probe_distinguishes_empty_from_read_error(self, ext_graph_empty_main_calls):
        db, _ = ext_graph_empty_main_calls
        r = IndexReader(db)
        try:
            assert r.has_call_audit is True and r.probe_call_rows() is False
            # Удаление таблицы во внешнем соединении дает достижимую sqlite3.Error
            # при следующей пробе; has_calls смешивал такой отказ с пустой таблицей.
            with sqlite3.connect(db) as c:
                c.execute("DROP TABLE calls")
            assert r.probe_call_rows() is None
        finally:
            r.close()

    def test_partial_layer_makes_path_miss_inconclusive(self, ext_graph_built, monkeypatch):
        monkeypatch.setattr(bsl_helpers, "_AUDIT_EXT_MODULE_BUDGET", 1)
        helpers = _ext_helpers(ext_graph_built)
        assert helpers["find_callers_context"]("Цель", OM)["_meta"]["extension_layer"] == "partial"
        p = helpers["find_path"]("НетТакого", "Цель", from_hint=OM, to_hint=OM)
        assert p["found"] is False and p["_meta"]["budget_exceeded"] is True

    def test_audit_and_graph_share_one_pass(self, ext_graph_built, monkeypatch):
        n = [0]
        real = bsl_helpers._extract_calls_from_body

        def counting(*a, **k):
            n[0] += 1
            return real(*a, **k)

        monkeypatch.setattr(bsl_helpers, "_extract_calls_from_body", counting)
        helpers = _ext_helpers(ext_graph_built)
        helpers["find_unresolved_calls"]()
        after_audit = n[0]
        helpers["find_callers_context"]("Цель", OM)
        helpers["find_unresolved_calls"](reasons=["method_missing"])
        assert after_audit > 0 and n[0] == after_audit

    # --- чувствительные пробы 11.3 -------------------------------------------------

    def test_main_export_change_flips_edge_exactness_without_new_pass(self, ext_graph_built, monkeypatch):
        db, cf = ext_graph_built
        n = [0]
        real = bsl_helpers._extract_calls_from_body

        def counting(*a, **k):
            n[0] += 1
            return real(*a, **k)

        monkeypatch.setattr(bsl_helpers, "_extract_calls_from_body", counting)
        helpers = _ext_helpers(ext_graph_built)
        before = helpers["find_callers_context"]("Цель", OM)
        assert [c["edge_exact"] for c in before["callers"]] == [True] and before["_meta"]["exact_rows"] == 1
        passes = n[0]
        mod = cf / OM
        mod.write_text(OM_BSL.replace("Процедура Цель() Экспорт", "Процедура Цель()"), encoding="utf-8-sig")
        st = mod.stat()
        os.utime(mod, (st.st_atime + 5, st.st_mtime + 5))
        IndexBuilder().update(str(cf))
        after = helpers["find_callers_context"]("Цель", OM)
        # экспорта больше нет: ключа у ребра расширения нет, остается эвристика по имени
        assert [c["edge_exact"] for c in after["callers"]] == [False] and after["_meta"]["exact_rows"] == 0
        assert n[0] == passes  # фазы 1-2 не перестраивались

    def test_budget_skipped_extension_hint_is_unavailable_not_absent(self, ext_graph_built, monkeypatch):
        monkeypatch.setattr(bsl_helpers, "_AUDIT_EXT_MODULE_BUDGET", 0)
        helpers = _ext_helpers(ext_graph_built)
        res = helpers["find_callers_context"]("Локальная", EXT1_MOD)
        assert res["callers"] == [] and res["_meta"]["extension_layer"] == "partial"
        assert "недоступна" in res["_meta"]["hint"]
        p = helpers["find_path"]("Вызовы", "Конечная", from_hint=EXT1_MOD, to_hint=OM)
        assert p["found"] is False and "error" not in p
        assert p["_meta"]["extension_layer"] == "partial" and p["_meta"]["budget_exceeded"] is True
        assert p["_meta"]["nodes_expanded"] == 0

    def test_unproven_counterpart_procs_give_uncached_partial(self, ext_graph_built, monkeypatch):
        real = IndexReader.get_methods_by_path
        flaky = {"on": True}

        def maybe_none(self, rel_path):
            return None if flaky["on"] and rel_path == ORDER else real(self, rel_path)

        monkeypatch.setattr(IndexReader, "get_methods_by_path", maybe_none)
        helpers = _ext_helpers(ext_graph_built)
        res = helpers["find_callers_context"]("ЗаполнитьСтроки", ORDER)
        assert res["_meta"]["extension_layer"] == "partial"
        assert not any(c["edge_exact"] for c in res["callers"])
        flaky["on"] = False  # следующий вызов повторяет сборку уровня — partial не закешировался
        res2 = helpers["find_callers_context"]("ЗаполнитьСтроки", ORDER)
        assert res2["_meta"]["extension_layer"] == "complete"
        assert [(c["file"], c["edge_exact"]) for c in res2["callers"]] == [(EXT1_ORDER, True)]

    def test_extension_receiver_shadowing_and_two_wrappers(self, tmp_path):
        ext1 = {
            **EXT_GRAPH_EXT1,
            "CommonModules/Расш1_Запуски/Ext/Module.bsl": (
                "Процедура ЧерезПараметр(ФоновыеЗадания) Экспорт\n"
                '    ФоновыеЗадания.Выполнить("ОМ.Фоновая");\nКонецПроцедуры\n\n'
                "Процедура ДвеОбертки(ДлительныеОперации) Экспорт\n"
                '    ФоновыеЗадания.Выполнить("ОМ.Цель"); ДлительныеОперации.ВыполнитьВФоне("ОМ.Цель");\n'
                "КонецПроцедуры\n"
            ),
        }
        cf = _write_ext_graph(tmp_path, None, ext1, None)
        helpers = _ext_helpers((IndexBuilder().build(str(cf), build_calls=True), cf))
        f = helpers["find_callers_context"]
        # параметр ФоновыеЗадания затеняет обертку — запуска нет
        assert [c["caller_name"] for c in f("Фоновая", OM)["callers"] if c["call_kind"] == "background"] == ["Вызовы"]
        # затенена одна из двух оберток одной строки: ребро другой остается
        launches = [c for c in f("Цель", OM)["callers"] if c["call_kind"] == "background"]
        assert [(c["caller_name"], c["edge_exact"]) for c in launches] == [("ДвеОбертки", True)]
        issues = helpers["find_unresolved_calls"](layer="extensions")["issues"]
        assert not any(i["kind"] == "launch" for i in issues)

    def test_borrowed_receiver_follows_main_descriptor_without_new_pass(self, tmp_path, monkeypatch):
        # Заимствованная форма: реквизит ДлительныеОперации появляется только в описателе main.
        form_rel = "Documents/Заказ/Forms/ФормаДокумента/Ext/Form/Module.bsl"
        form_xml = "Documents/Заказ/Forms/ФормаДокумента/Ext/Form.xml"
        main = {**EXT_GRAPH_MAIN, form_rel: "&НаСервере\nПроцедура Свое()\nКонецПроцедуры\n", form_xml: _cf_form_xml()}
        ext1 = {
            **EXT_GRAPH_EXT1,
            form_rel: '&НаСервере\nПроцедура Расш1_Запуск()\n    ДлительныеОперации.ВыполнитьВФоне("ОМ.Фоновая");\nКонецПроцедуры\n',
            form_xml: _cf_form_xml(),
        }
        cf = _write_ext_graph(tmp_path, main, ext1, None)
        n = [0]
        real = bsl_helpers._extract_calls_from_body

        def counting(*a, **k):
            n[0] += 1
            return real(*a, **k)

        monkeypatch.setattr(bsl_helpers, "_extract_calls_from_body", counting)
        helpers = _ext_helpers((IndexBuilder().build(str(cf), build_calls=True), cf))
        ext_form = "../cfe/Расш1/" + form_rel

        def launch_files():
            return [
                c["file"]
                for c in helpers["find_callers_context"]("Фоновая", OM)["callers"]
                if c["call_kind"] == "background"
            ]

        assert ext_form in launch_files()
        passes = n[0]
        for attrs, expected in ((("ДлительныеОперации",), False), ((), True)):
            p = cf / form_xml
            p.write_text(_cf_form_xml(*attrs), encoding="utf-8-sig")
            st = p.stat()
            os.utime(p, (st.st_atime + 5, st.st_mtime + 5))
            IndexBuilder().update(str(cf))
            assert (ext_form in launch_files()) is expected
            audit = helpers["find_unresolved_calls"](layer="extensions", path=ext_form)
            assert not any(i["kind"] == "launch" for i in audit["issues"])
        assert n[0] == passes  # фазы 1-2 не перестраивались


# ── Ускорение полной сборки: тот же индекс быстрее ─────────────────────────────

import threading  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

import rlm_tools_bsl.bsl_xml_parsers as XP  # noqa: E402
from rlm_tools_bsl.bsl_knowledge import mask_comments_and_strings  # noqa: E402

CLEAN_BODY = [
    "Процедура А()",
    '    Т = "Модуль.Метод(";  // В();',
    "    Модуль.Метод(1);",
    "    Б();",
    "КонецПроцедуры",
]
# Тело начинается ВНУТРИ литерала, открытого строкой объявления (битый модуль): маска модуля
# гасит вызов Б(), пересчет по срезу тела — нет. Прежнее поведение — пересчет.
BROKEN_BODY = ['Процедура А() Экспорт Сообщить("начало', "|продолжение", "Б();", "КонецПроцедуры"]


class TestBuildSpeedMasks:
    def test_line_states_mark_lines_inside_literal(self):
        lines = ['А = "раз', "|два", '|три";', "Б();"]
        states: list[bool] = []
        assert mask_comments_and_strings(lines, line_states=states) == mask_comments_and_strings(lines)
        assert states == [False, True, True, False]

    def test_body_mask_taken_from_module_mask(self, monkeypatch):
        states: list[bool] = []
        masked = mask_comments_and_strings(CLEAN_BODY, line_states=states)
        expected = BI._extract_calls_from_body(CLEAN_BODY, 1, len(CLEAN_BODY))

        def forbidden(*a, **k):
            raise AssertionError("маска тела пересчитана")

        monkeypatch.setattr(BI, "mask_comments_and_strings", forbidden)
        got = BI._extract_calls_from_body(CLEAN_BODY, 1, len(CLEAN_BODY), module_mask=(masked, states))
        assert got == expected
        assert [c[0] for c in got] == ["Модуль.Метод", "Б"]

    def test_body_starting_inside_literal_is_remasked(self):
        states: list[bool] = []
        masked = mask_comments_and_strings(BROKEN_BODY, line_states=states)
        assert states[1] is True
        plain = BI._extract_calls_from_body(BROKEN_BODY, 1, len(BROKEN_BODY))
        assert [c[0] for c in plain] == ["Б"]
        assert BI._extract_calls_from_body(BROKEN_BODY, 1, len(BROKEN_BODY), module_mask=(masked, states)) == plain


def _speed_rights_xml(flag: bool) -> str:
    def right(name, value):
        return f"<right><name>{name}</name><value>{value}</value></right>"

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n<Rights xmlns="http://v8.1c.ru/8.2/roles">\n'
        f"<setForNewObjects>{'true' if flag else 'false'}</setForNewObjects>\n"
        f"<object><name>Catalog.А</name>{right('Read', 'true')}{right('Update', 'false')}</object>"
        f"<object><name>Document.Б</name>{right('Read', 'false')}</object></Rights>\n"
    )


class TestBuildSpeedRights:
    @pytest.mark.parametrize("flag", [False, True])
    def test_single_parse_equals_public_parsers(self, flag):
        xml = _speed_rights_xml(flag)
        granted, flagged, denied = XP.parse_rights_for_index(xml)
        meta = XP.parse_rights_meta(xml)
        assert granted == XP.parse_rights_xml(xml) == [{"object": "Catalog.А", "rights": ["Read"]}]
        assert flagged is meta["set_for_new_objects"] is flag
        assert meta["exclusions"] == [
            {"object": "Catalog.А", "rights": ["Update"]},
            {"object": "Document.Б", "rights": ["Read"]},
        ]
        assert denied == (meta["exclusions"] if flag else [])

    def test_broken_xml(self):
        assert XP.parse_rights_for_index("<Rights") == ([], False, [])

    def test_collect_role_data_parses_each_file_once(self, tmp_path, monkeypatch):
        for role, flag in (("Р1", True), ("Р2", False)):
            d = tmp_path / "Roles" / role / "Ext"
            d.mkdir(parents=True)
            (d / "Rights.xml").write_text(_speed_rights_xml(flag), encoding="utf-8")
        n = [0]
        real = XP.ET.fromstring

        def counting(text, *a, **k):
            n[0] += 1
            return real(text, *a, **k)

        monkeypatch.setattr(XP.ET, "fromstring", counting)
        data = BI._collect_role_data(str(tmp_path))
        assert n[0] == 2  # прежде — два разбора на файл
        r1, r2 = "Roles/Р1/Ext/Rights.xml", "Roles/Р2/Ext/Rights.xml"
        assert sorted(data.rights) == [("Р1", "Catalog.А", "Read", r1), ("Р2", "Catalog.А", "Read", r2)]
        assert sorted(data.flags) == [("Р1", 1, r1), ("Р2", 0, r2)]
        assert sorted(data.exclusions) == [("Р1", "Catalog.А", "Update", r1), ("Р1", "Document.Б", "Read", r1)]


class TestBuildSpeedPrefetch:
    def test_descriptor_selection(self, tmp_path):
        wanted = [
            "Configuration.xml",
            "Catalogs/А.xml",
            "Catalogs/А/Forms/Ф/Ext/Form.xml",
            "Catalogs/Б/Б.mdo",
            "Catalogs/Б/Forms/Ф/Form.form",
            "Roles/Р/Ext/Rights.xml",
            "Roles/Р2/Rights.rights",
        ]
        skipped = ["Catalogs/А/Ext/ObjectModule.bsl", "Catalogs/А/Templates/М/Ext/Template.xml", ".git/x.xml"]
        for rel in wanted + skipped:
            (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
            (tmp_path / rel).write_text("<x/>", encoding="utf-8")
        got = sorted(Path(p).relative_to(tmp_path).as_posix() for p in BI._prefetch_descriptor_paths(tmp_path))
        assert got == sorted(wanted)

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO и /dev/zero — только POSIX")
    def test_special_files_are_not_prefetched(self, tmp_path):
        # open() на FIFO без писателя блокируется навсегда, у /dev/zero нет EOF: сборка не вернулась бы.
        (tmp_path / "Catalogs").mkdir()
        (tmp_path / "Catalogs" / "А.xml").write_text("<x/>", encoding="utf-8")
        other = tmp_path / "unrelated"
        other.mkdir()
        os.mkfifo(other / "stream.xml")
        os.symlink(other / "stream.xml", other / "fifo_link.xml")
        os.symlink("/dev/zero", other / "zero.xml")
        os.symlink(tmp_path / "Catalogs" / "А.xml", other / "file_link.xml")
        os.symlink(tmp_path, other / "loop")  # ссылка на каталог: обход в нее не заходит (иначе петля)
        got = sorted(Path(p).relative_to(tmp_path).as_posix() for p in BI._prefetch_descriptor_paths(tmp_path))
        assert got == ["Catalogs/А.xml", "unrelated/file_link.xml"]

    def test_warm_stops_between_chunks(self, monkeypatch):
        reads = [0]

        class Endless:  # источник без EOF; предохранитель — чтобы сломанный код не повесил прогон
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self, n):
                reads[0] += 1
                return b"\0" if reads[0] < 1000 else b""

        monkeypatch.setattr(BI, "open", lambda *a, **k: Endless(), raising=False)
        stop = threading.Event()
        stop.set()
        BI._warm_file("endless.xml", stop)
        assert reads[0] == 1

    def test_prefetch_warms_every_descriptor(self, tmp_path, monkeypatch):
        (tmp_path / "Catalogs").mkdir()
        for i in range(40):
            (tmp_path / "Catalogs" / f"О{i}.xml").write_text("<x/>", encoding="utf-8")
        seen: list[str] = []
        lock = threading.Lock()

        def warm(path, stop=None):
            with lock:
                seen.append(path)

        monkeypatch.setattr(BI, "_warm_file", warm)
        pf = BI._DescriptorPrefetch(str(tmp_path))
        pf._walker.join()
        pf._pool.shutdown(wait=True)  # дождаться очереди, не снимая ее
        pf.close()
        assert len(seen) == len(set(seen)) == 40

    def test_build_leaves_no_prefetch_threads(self, tmp_path, monkeypatch):
        def slow_warm(path, stop=None):
            time.sleep(0.3)  # чтение идет в момент закрытия сборки

        monkeypatch.setattr(BI, "_warm_file", slow_warm)
        files = {"CommonModules/М/Ext/Module.bsl": "Процедура П()\nКонецПроцедуры\n"}
        files.update({f"Catalogs/О{i}.xml": "<x/>" for i in range(40)})
        _write_project(tmp_path, files)
        IndexBuilder().build(str(tmp_path), build_calls=False)
        # После возврата сборки ни один файл прогревом не открыт: временный каталог можно удалять.
        assert not [t for t in threading.enumerate() if t.name.startswith("rlm-prefetch") and t.is_alive()]

    def test_prefetch_failure_does_not_break_build(self, tmp_path, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("обход упал")

        monkeypatch.setattr(BI, "_prefetch_descriptor_paths", boom)
        _write_project(tmp_path, {"CommonModules/М/Ext/Module.bsl": "Процедура П()\nКонецПроцедуры\n"})
        db = IndexBuilder().build(str(tmp_path), build_calls=False)
        c = sqlite3.connect(str(db))
        try:
            assert c.execute("SELECT COUNT(*) FROM methods").fetchone()[0] == 1
        finally:
            c.close()


class TestBuildSpeedCallsIndexes:
    def test_calls_indexes_built_after_insert_with_schema_sql(self, tmp_path, monkeypatch):
        seen = []
        real = IndexBuilder._bulk_insert

        def spy(conn, results, build_calls):
            seen.append(
                conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND tbl_name='calls'").fetchone()[0]
            )
            return real(conn, results, build_calls)

        monkeypatch.setattr(IndexBuilder, "_bulk_insert", staticmethod(spy))
        _write_project(tmp_path, SCHEMA_FILES)
        db = IndexBuilder().build(str(tmp_path), build_calls=True)
        assert seen == [0]  # во время массовой вставки индексов calls нет
        query = "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name='calls' ORDER BY name"
        c = sqlite3.connect(str(db))
        ref = sqlite3.connect(":memory:")
        try:
            built = c.execute(query).fetchall()
            ref.executescript(BI._SCHEMA_SQL)
            assert built == ref.execute(query).fetchall()  # те же индексы с тем же текстом, что у схемы
            assert len(built) == 4
        finally:
            c.close()
            ref.close()


class TestBuildSpeedVacuum:
    def test_threshold(self, tmp_path):
        c = sqlite3.connect(str(tmp_path / "v.db"))
        try:
            c.execute("CREATE TABLE t (x TEXT)")
            c.executemany("INSERT INTO t VALUES (?)", [("x" * 500,)] * 2000)
            c.commit()
            assert IndexBuilder._vacuum_worthwhile(c) is False  # компактный файл
            c.execute("DELETE FROM t")
            c.commit()
            assert IndexBuilder._vacuum_worthwhile(c) is True  # освободилось больше порога
        finally:
            c.close()

    def test_vacuum_only_when_rebuild_frees_space(self, tmp_path, monkeypatch):
        body = "Процедура П()\n" + "    А();\n" * 200 + "КонецПроцедуры\n"
        _write_project(tmp_path, {f"CommonModules/М{i}/Ext/Module.bsl": body for i in range(60)})
        IndexBuilder().build(str(tmp_path))
        decisions: list[bool] = []
        real = IndexBuilder._vacuum_worthwhile

        def spy(conn):
            decisions.append(real(conn))
            return decisions[-1]

        monkeypatch.setattr(IndexBuilder, "_vacuum_worthwhile", staticmethod(spy))
        IndexBuilder().build(str(tmp_path))  # тот же состав поверх: страницы заняты заново
        for i in range(55):
            shutil.rmtree(tmp_path / "CommonModules" / f"М{i}")
        db = IndexBuilder().build(str(tmp_path))  # состав меньше: место освободилось
        assert decisions == [False, True]
        c = sqlite3.connect(str(db))
        try:
            assert c.execute("PRAGMA freelist_count").fetchone()[0] == 0
        finally:
            c.close()


# ── План эвристической ветки точного режима get_callers ────────────────────────

# Статистика sqlite_stat1 таблицы calls боевых индексов v17: (idx_calls_callee_key, idx_calls_callee_short).
# На первых двух (ERP) планировщик без правки шел по idx_calls_callee_key и перебирал все строки без
# ключа; третья — контроль: на ней план был верным и без правки.
_ERP_CALL_STATS = (
    pytest.param(("3715958 10", "3715958 24"), id="erp-10-24"),
    pytest.param(("3170299 11", "3170299 24"), id="erp-11-24"),
    pytest.param(("2698529 11", "2698529 23"), id="control-11-23"),
)


class TestCallersQueryPlan:
    """Эвристическая ветка точного режима ``get_callers`` (ключа нет, совпало короткое имя) идет по
    индексу короткого имени при любой статистике. SQLite без stat4 оценивает ``callee_key IS NULL``
    средним stat1 и не знает, что строк без ключа — половина таблицы: на боевых индексах v17 ERP план
    уходил в ``idx_calls_callee_key`` и стоил ~1 с на запрос, дважды на вызов. Планировщик верит stat1,
    а не размеру таблицы, поэтому маленькая фикстура с вписанной статистикой ERP получает тот же выбор
    индекса, что боевая БД."""

    @pytest.mark.parametrize("stats", _ERP_CALL_STATS)
    def test_exact_mode_fallback_uses_short_name_index(self, tmp_path, stats):
        key_stat, short_stat = stats
        files = dict(SCHEMA_FILES)
        files["Documents/Заказ/Ext/ObjectModule.bsl"] = (
            "Процедура П()\n    ОбщийМодульА.Метод1();\n    Значение.Метод1();\nКонецПроцедуры\n"
        )
        _write_project(tmp_path, files)
        db_path = IndexBuilder().build(str(tmp_path), build_calls=True)
        rows = key_stat.split()[0]
        con = sqlite3.connect(db_path)
        try:
            con.execute("ANALYZE")
            con.execute("DELETE FROM sqlite_stat1 WHERE tbl = 'calls'")
            con.executemany(
                "INSERT INTO sqlite_stat1 (tbl, idx, stat) VALUES ('calls', ?, ?)",
                [
                    ("idx_calls_callee", f"{rows} 12"),
                    ("idx_calls_callee_key", key_stat),
                    ("idx_calls_by_name", "2089 1045"),
                    ("idx_calls_callee_short", short_stat),
                ],
            )
            con.commit()
        finally:
            con.close()
        reader = IndexReader(db_path)
        try:
            statements: list[str] = []
            reader._conn.set_trace_callback(statements.append)
            res = reader.get_callers("Метод1", "CommonModules/ОбщийМодульА/Ext/Module.bsl")
            reader._conn.set_trace_callback(None)
            meta = res["_meta"]
            # точная строка — вызов общего модуля по ключу, эвристическая — метод значения по имени
            assert (meta["target_exact"], meta["exact_rows"], meta["fallback_rows"]) == (True, 1, 1)
            fallback = [s for s in statements if "callee_key IS NULL" in s and s.lstrip().upper().startswith("SELECT")]
            assert len(fallback) == 2  # подсчет и выборка страницы
            for sql in fallback:
                plan = " | ".join(r[-1] for r in reader._conn.execute("EXPLAIN QUERY PLAN " + sql))
                assert "idx_calls_callee_short" in plan, plan
                # idx_calls_callee_key допустим только в ветке «callee_key = ?» выборки
                assert plan.count("idx_calls_callee_key") <= 1, plan
        finally:
            reader.close()


# ── Регрессы e2e этапа 2 ──────────────────────────────────────────────────────
# В-1: диспетчер `<…>.Менеджер.М(` (подключаемые команды БСП) — кандидат по имени, а не член цепочки вне графа.

PECHAT = "CommonModules/УправлениеПечатью/Ext/Module.bsl"
REAL_MGR = "Documents/Реализация/Ext/ManagerModule.bsl"
REAL_OBJ = "Documents/Реализация/Ext/ObjectModule.bsl"
REAL_FORM = "Documents/Реализация/Forms/ФормаДокумента/Ext/Form/Module.bsl"
PECHAT_BSL = (
    "Процедура Диспетчер(СведенияОбОбъекте, Команды) Экспорт\n"
    "    СведенияОбОбъекте.Менеджер.ДобавитьКомандыПечати(Команды);\n"
    "КонецПроцедуры\n\n"
    "Процедура ДиспетчерАнгл(Источник, Команды) Экспорт\n"
    "    Источник.Manager.ДобавитьКомандыПечати(Команды);\n"
    "КонецПроцедуры\n\n"
    "Процедура Прочие(Объект, Команды) Экспорт\n"
    "    Объект.ОМ.ДобавитьКомандыПечати(Команды);\n"
    "    Объект.Товары.Добавить();\n"
    "    Документы.Реализация.ДобавитьКомандыПечати(Команды);\n"
    "    ОМ.ДобавитьКомандыПечати(Команды);\n"
    "КонецПроцедуры\n"
)
LINE_DISPATCH_RU = _line_of(PECHAT_BSL, "СведенияОбОбъекте.Менеджер.ДобавитьКомандыПечати")
LINE_DISPATCH_EN = _line_of(PECHAT_BSL, "Источник.Manager.ДобавитьКомандыПечати")
LINE_CHAIN_OM = _line_of(PECHAT_BSL, "Объект.ОМ.ДобавитьКомандыПечати")
LINE_MGR_DIRECT = _line_of(PECHAT_BSL, "Документы.Реализация.ДобавитьКомандыПечати")
LINE_OM_DIRECT = _line_of(PECHAT_BSL, "    ОМ.ДобавитьКомандыПечати")
_EXPORT_PRINT_BSL = "Процедура ДобавитьКомандыПечати(Команды) Экспорт\nКонецПроцедуры\n"
DISPATCH_FILES = {
    PECHAT: PECHAT_BSL,
    REAL_MGR: _EXPORT_PRINT_BSL,
    REAL_OBJ: _EXPORT_PRINT_BSL,
    REAL_FORM: "&НаСервере\nПроцедура ДобавитьКомандыПечати(Команды)\nКонецПроцедуры\n",
    OM: _EXPORT_PRINT_BSL,
    "CommonModules/УправлениеПечатью.xml": _cm_descriptor("УправлениеПечатью"),
    "CommonModules/ОМ.xml": _cm_descriptor("ОМ"),
}


@pytest.fixture
def dispatch_built(tmp_path):
    _write_project(tmp_path, DISPATCH_FILES)
    return IndexBuilder().build(str(tmp_path), build_calls=True), tmp_path


class TestManagerMemberDispatch:
    """В-1: член цепочки `<…>.Менеджер.М(` — вызов метода менеджера объекта, известного только во время
    выполнения. В граф он возвращается кандидатом по имени (`edge_exact=False`, `call_kind='call'`): в
    режиме по имени всегда, в точном — только у цели в модуле менеджера. Прочие члены цепочки — вне графа."""

    def test_rows_stay_member_rows_without_key(self, dispatch_built):
        db, _ = dispatch_built
        assert _edge(db, PECHAT, "Менеджер.ДобавитьКомандыПечати") == (None, "member", None)
        assert _edge(db, PECHAT, "Manager.ДобавитьКомандыПечати") == (None, "member", None)

    def test_manager_target_gets_dispatcher_as_heuristic_row(self, dispatch_built):
        db, _ = dispatch_built
        r = IndexReader(db)
        try:
            res = r.get_callers("ДобавитьКомандыПечати", REAL_MGR, 0, 100)
            got = sorted((c["line"], c["caller_name"], c["edge_exact"], c["call_kind"]) for c in res["callers"])
            assert got == [
                (LINE_DISPATCH_RU, "Диспетчер", False, "call"),
                (LINE_DISPATCH_EN, "ДиспетчерАнгл", False, "call"),
                (LINE_MGR_DIRECT, "Прочие", True, "call"),
            ]
            m = res["_meta"]
            assert (m["target_exact"], m["total_callers"], m["exact_rows"], m["fallback_rows"]) == (True, 3, 1, 2)
            assert m["returned"] == m["total_callers"] == m["exact_rows"] + m["fallback_rows"]
            # постраничная выдача и счет сходятся
            p1 = r.get_callers("ДобавитьКомандыПечати", REAL_MGR, 0, 2)
            p2 = r.get_callers("ДобавитьКомандыПечати", REAL_MGR, 2, 2)
            assert len(p1["callers"]) + len(p2["callers"]) == 3 and p1["_meta"]["has_more"] is True
        finally:
            r.close()

    @pytest.mark.parametrize("target", [OM, REAL_OBJ, REAL_FORM])
    def test_namesake_outside_manager_module_gets_no_dispatcher(self, dispatch_built, target):
        db, _ = dispatch_built
        r = IndexReader(db)
        try:
            res = r.get_callers("ДобавитьКомандыПечати", target, 0, 100)
            assert res["_meta"]["target_exact"] is True
            lines = [c["line"] for c in res["callers"]]
            assert LINE_DISPATCH_RU not in lines and LINE_DISPATCH_EN not in lines and LINE_CHAIN_OM not in lines
            assert lines == ([LINE_OM_DIRECT] if target == OM else [])
            assert res["_meta"]["total_callers"] == len(lines)
        finally:
            r.close()

    def test_name_mode_without_hint_includes_dispatcher(self, dispatch_built):
        db, _ = dispatch_built
        r = IndexReader(db)
        try:
            res = r.get_callers("ДобавитьКомандыПечати", "", 0, 100)
            assert res["_meta"]["target_exact"] is False
            assert sorted(c["line"] for c in res["callers"]) == sorted(
                [LINE_DISPATCH_RU, LINE_DISPATCH_EN, LINE_MGR_DIRECT, LINE_OM_DIRECT]
            )
            assert res["_meta"]["total_callers"] == 4  # подсчет без JOIN — тот же фильтр вида
            assert not any(c["edge_exact"] for c in res["callers"])
            # прочие члены цепочки по-прежнему вне графа
            other = r.get_callers("Добавить", "", 0, 100)
            assert other["callers"] == [] and other["_meta"]["total_callers"] == 0
        finally:
            r.close()

    def test_name_mode_module_hint_filter_cuts_dispatcher(self, dispatch_built):
        db, _ = dispatch_built
        r = IndexReader(db)
        try:
            # «Менеджер»/«Manager» совпадают с головой диспетчера: фильтр `hint.%` его бы не отсек (ревью
            # Codex, P3) — с подсказкой диспетчер не подмешивается вовсе, как и в слое расширений.
            for hint in ("Реализация", "Документ.Реализация", "Менеджер", "Manager", "МЕНЕДЖЕР"):
                res = r.get_callers("ДобавитьКомандыПечати", hint, 0, 100)
                assert res["_meta"]["target_exact"] is False, hint  # объект неоднозначен или не найден
                lines = [c["line"] for c in res["callers"]]
                assert LINE_DISPATCH_RU not in lines and LINE_DISPATCH_EN not in lines, (hint, lines)
                assert res["_meta"]["total_callers"] == len(lines)
        finally:
            r.close()

    def test_layer_predicate_mirrors_sql(self, dispatch_built):
        db, _ = dispatch_built
        rows = _all_calls(db)
        r = IndexReader(db)
        try:
            for hint in (REAL_MGR, OM, REAL_OBJ, REAL_FORM, ""):
                res = r.get_callers("ДобавитьКомандыПечати", hint, 0, 1000)
                key = res["_meta"]["target_key"] if res["_meta"]["target_exact"] else None
                got = sorted(
                    (f, ln)
                    for f, ln, cn, k, kind in rows
                    if bsl_helpers._ext_row_matches(cn, k, "ДобавитьКомандыПечати", key, kind)
                )
                assert got == sorted((c["file"], c["line"]) for c in res["callers"]), hint
        finally:
            r.close()

    def test_hierarchy_and_path_see_dispatcher_edge(self, dispatch_built):
        db, base = dispatch_built
        bsl, reader = _make_bsl_for(base, db)
        try:
            tree = bsl["find_call_hierarchy"]("ДобавитьКомандыПечати", depth=1, module_hint=REAL_MGR)
            callers = {(c["caller_name"], c["module_path"], c["call_kind"]) for c in tree["tree"][0]["callers"]}
            assert {("Диспетчер", PECHAT, "call"), ("ДиспетчерАнгл", PECHAT, "call")} <= callers
            p = bsl["find_path"]("Диспетчер", "ДобавитьКомандыПечати", from_hint=PECHAT, to_hint=REAL_MGR)
            assert p["found"] is True and p["_meta"]["precision"] == "heuristic"
            assert [(e["name"], e["module_path"], e["call_kind"]) for e in p["path"]] == [
                ("Диспетчер", PECHAT, "call"),
                ("ДобавитьКомандыПечати", REAL_MGR, None),
            ]
            # к тезке в общем модуле члена цепочки нет — путь не найден, промах окончательный
            miss = bsl["find_path"]("Диспетчер", "ДобавитьКомандыПечати", from_hint=PECHAT, to_hint=OM)
            assert miss["found"] is False and miss["_meta"]["budget_exceeded"] is False
        finally:
            reader.close()

    @pytest.mark.parametrize("stats", _ERP_CALL_STATS)
    def test_member_clause_keeps_short_name_index(self, dispatch_built, stats):
        # Урок Д-1: OR в фильтре вида не должен сменить план — эвристика идет по индексу короткого имени.
        db, _ = dispatch_built
        key_stat, short_stat = stats
        rows = key_stat.split()[0]
        con = sqlite3.connect(db)
        try:
            con.execute("ANALYZE")
            con.execute("DELETE FROM sqlite_stat1 WHERE tbl = 'calls'")
            con.executemany(
                "INSERT INTO sqlite_stat1 (tbl, idx, stat) VALUES ('calls', ?, ?)",
                [
                    ("idx_calls_callee", f"{rows} 12"),
                    ("idx_calls_callee_key", key_stat),
                    ("idx_calls_by_name", "2089 1045"),
                    ("idx_calls_callee_short", short_stat),
                ],
            )
            con.commit()
        finally:
            con.close()
        reader = IndexReader(db)
        try:
            statements: list[str] = []
            reader._conn.set_trace_callback(statements.append)
            exact = reader.get_callers("ДобавитьКомандыПечати", REAL_MGR)
            by_name = reader.get_callers("ДобавитьКомандыПечати", "")
            reader._conn.set_trace_callback(None)
            assert (exact["_meta"]["fallback_rows"], by_name["_meta"]["total_callers"]) == (2, 4)
            member_sql = [s for s in statements if "py_casefold(" in s and s.lstrip().upper().startswith("SELECT")]
            assert len(member_sql) == 4  # точный режим и режим по имени: подсчет и страница
            for sql in member_sql:
                plan = " | ".join(r[-1] for r in reader._conn.execute("EXPLAIN QUERY PLAN " + sql))
                assert "idx_calls_callee_short" in plan, plan
                assert plan.count("idx_calls_callee_key") <= 1, plan
        finally:
            reader.close()


EXT1_DISPATCH = "../cfe/Расш1/CommonModules/Расш1_Печать/Ext/Module.bsl"


class TestManagerMemberDispatchFromExtension:
    """В-1 в живом слое расширений (Д8): то же правило, что у индекса."""

    def _helpers(self, tmp_path):
        main = {
            **EXT_GRAPH_MAIN,
            OM: OM_BSL + "\n" + _EXPORT_PRINT_BSL,
            REAL_MGR: _EXPORT_PRINT_BSL,
            "Documents/Реализация.xml": _cf_object_xml("Document", "Реализация"),
        }
        ext1 = {
            "CommonModules/Расш1_Печать/Ext/Module.bsl": (
                "Процедура ДиспетчерРасш(Сведения, Объект, Команды) Экспорт\n"
                "    Сведения.Менеджер.ДобавитьКомандыПечати(Команды);\n"
                "    Объект.ОМ.ДобавитьКомандыПечати(Команды);\n"
                "КонецПроцедуры\n"
            )
        }
        cf = _write_ext_graph(tmp_path, main, ext1, {})
        return _ext_helpers((IndexBuilder().build(str(cf), build_calls=True), cf))

    def test_extension_dispatcher_reaches_manager_target_only(self, tmp_path):
        f = self._helpers(tmp_path)["find_callers_context"]
        mgr = f("ДобавитьКомандыПечати", REAL_MGR)
        assert [(c["file"], c["line"], c["edge_exact"], c["call_kind"]) for c in mgr["callers"]] == [
            (EXT1_DISPATCH, 2, False, "call")
        ]
        m = mgr["_meta"]
        assert (m["extension_rows"], m["extension_layer"]) == (1, "complete")
        assert (m["total_callers"], m["exact_rows"], m["fallback_rows"]) == (1, 0, 1)
        om = f("ДобавитьКомандыПечати", OM)
        assert om["callers"] == [] and om["_meta"]["extension_rows"] == 0
        by_name = f("ДобавитьКомандыПечати")
        assert by_name["_meta"]["target_exact"] is False
        assert [(c["file"], c["line"]) for c in by_name["callers"]] == [(EXT1_DISPATCH, 2)]

    def test_head_named_hint_answers_alike_for_main_and_extension_dispatchers(self, tmp_path):
        # Подсказка, совпавшая с головой диспетчера, цель не разрешает: ни строка индекса, ни строка слоя
        # расширений в ответ не идут — результат не зависит от того, где лежит вызывающий.
        main_dispatch = "CommonModules/Диспетчер/Ext/Module.bsl"
        main = {
            **EXT_GRAPH_MAIN,
            OM: OM_BSL + "\n" + _EXPORT_PRINT_BSL,
            REAL_MGR: _EXPORT_PRINT_BSL,
            "Documents/Реализация.xml": _cf_object_xml("Document", "Реализация"),
            main_dispatch: "Процедура Д(Сведения, Команды) Экспорт\n    Сведения.Менеджер.ДобавитьКомандыПечати(Команды);\n"
            "    Сведения.Manager.ДобавитьКомандыПечати(Команды);\nКонецПроцедуры\n",
        }
        ext1 = {
            "CommonModules/Расш1_Печать/Ext/Module.bsl": "Процедура ДиспетчерРасш(Сведения, Команды) Экспорт\n"
            "    Сведения.Менеджер.ДобавитьКомандыПечати(Команды);\nКонецПроцедуры\n"
        }
        cf = _write_ext_graph(tmp_path, main, ext1, {})
        f = _ext_helpers((IndexBuilder().build(str(cf), build_calls=True), cf))["find_callers_context"]
        for hint in ("Менеджер", "Manager"):
            res = f("ДобавитьКомандыПечати", hint)
            assert res["callers"] == [] and res["_meta"]["total_callers"] == 0, (hint, res["callers"])
        # контроль: без подсказки видны все три диспетчера
        assert len(f("ДобавитьКомандыПечати")["callers"]) == 3


# В-2: `_meta.unresolved_launches` — только у цели, которую можно запустить по имени.

DOK_MGR = "Documents/Док/Ext/ManagerModule.bsl"
DOK_OBJ = "Documents/Док/Ext/ObjectModule.bsl"
DOK_FORM = "Documents/Док/Forms/ФормаДокумента/Ext/Form/Module.bsl"
DP_X_OBJ = "DataProcessors/Х/Ext/ObjectModule.bsl"
PROCHIY = "CommonModules/Прочий/Ext/Module.bsl"
LAUNCH_TARGET_FILES = {
    **LAUNCH_FILES,
    DOK_MGR: DOK_MANAGER_BSL + "\nПроцедура БезВызовов() Экспорт\nКонецПроцедуры\n",
    DOK_OBJ: "Процедура ПриЗаписи(Отказ)\nКонецПроцедуры\n\nПроцедура ЭкспортОбъекта() Экспорт\nКонецПроцедуры\n",
    DOK_FORM: "&НаКлиенте\nПроцедура ПередЗаписью(Отказ, ПараметрыЗаписи)\nКонецПроцедуры\n",
    # адресат литерала "Обработка.Х.МодульОбъекта.М" (ЧетыреСегмента/Смешанная) — запуск БСП есть, ребра нет
    DP_X_OBJ: "Процедура М() Экспорт\nКонецПроцедуры\n",
    PROCHIY: "Процедура Скрытая()\nКонецПроцедуры\n",
    "CommonModules/Прочий.xml": _cm_descriptor("Прочий"),
}
_LAUNCH_TARGET_CASES = [
    pytest.param("Вторая2", FON, True, id="export-common-module"),
    pytest.param("БезВызовов", DOK_MGR, True, id="export-manager-module"),
    pytest.param("М", DP_X_OBJ, True, id="export-data-processor-object-module"),
    pytest.param("ПередЗаписью", DOK_FORM, False, id="form-event-handler"),
    pytest.param("ПриЗаписи", DOK_OBJ, False, id="object-event-handler"),
    pytest.param("ЭкспортОбъекта", DOK_OBJ, False, id="export-document-object-module"),
    pytest.param("Скрытая", PROCHIY, False, id="non-export-common-module"),
]


@pytest.fixture
def launch_target_built(tmp_path):
    _write_project(tmp_path, LAUNCH_TARGET_FILES)
    return IndexBuilder().build(str(tmp_path), build_calls=True), tmp_path


class TestUnresolvedLaunchesOnlyForLaunchableTarget:
    """В-2: запуски без статического адресата называются при нуле вызывающих, только если цель можно
    запустить по имени — экспортный метод общего модуля, модуля менеджера или модуля объекта обработки
    (отчета). У обработчика события и неэкспортного метода это число к нулю отношения не имеет."""

    @pytest.mark.parametrize(("name", "hint", "launchable"), _LAUNCH_TARGET_CASES)
    def test_zero_callers_hint(self, launch_target_built, name, hint, launchable):
        db, base = launch_target_built
        bsl, reader = _make_bsl_for(base, db)
        try:
            res = bsl["find_callers_context"](name, hint)
            m = res["_meta"]
            assert (m["total_callers"], m["target_exact"]) == (0, True)
            assert m["hint"].startswith("No callers found in call index.")
            assert ("unresolved_launches" in m) is launchable
            assert ("find_unresolved_calls" in m["hint"]) is launchable
            if launchable:
                assert m["unresolved_launches"] == 5
        finally:
            reader.close()

    @pytest.mark.parametrize(("name", "hint", "launchable"), _LAUNCH_TARGET_CASES)
    def test_reader_predicate(self, launch_target_built, name, hint, launchable):
        db, _ = launch_target_built
        r = IndexReader(db)
        try:
            assert r.is_launch_target(_make_callee_key(hint, name)) is launchable
        finally:
            r.close()

    def test_reader_predicate_unknown_target(self, launch_target_built):
        db, _ = launch_target_built
        r = IndexReader(db)
        try:
            assert r.is_launch_target(_make_callee_key("CommonModules/Нет/Ext/Module.bsl", "М")) is None
            assert r.is_launch_target("без разделителя") is None
            assert r.is_launch_target(_make_callee_key(FON, "НетТакогоМетода")) is False
        finally:
            r.close()


# В-3: подпись find_unresolved_calls называет все ключи строки issues.


class TestUnresolvedCallsSignature:
    def test_signature_names_every_issue_key(self, audit_helpers):
        from rlm_tools_bsl.bsl_helpers import build_helper_metadata_snapshot

        res = audit_helpers["find_unresolved_calls"]()
        assert res["issues"], "предусловие: в фикстуре аудита есть находки"
        sig = build_helper_metadata_snapshot()["find_unresolved_calls"]["sig"]
        named = [k.strip() for k in sig.split("issues:[{", 1)[1].split("}]", 1)[0].split(",")]
        assert named == list(res["issues"][0])


# В-4: count_only шапок отделяет модули с текстом шапки от модулей «только с лицензией».


class TestModuleHeadersCountWithText:
    def test_empty_query_splits_license_only(self, headers_built):
        res = headers_built["search_module_headers"]("", count_only=True)
        rows = headers_built["search_module_headers"]("", limit=1000)
        assert res == {"total": 3, "with_text": 2, "source": "index", "truncated": False, "scope": "main_index"}
        assert res["total"] == len(rows)
        assert res["with_text"] == sum(1 for r in rows if r["header_comment"])

    def test_query_counts_text_only(self, headers_built):
        res = headers_built["search_module_headers"]("шапки", count_only=True)
        assert (res["total"], res["with_text"]) == (1, 1)

    def test_extension_scope_carries_with_text(self, tmp_path):
        ext1 = {"CommonModules/Расш1_Ш/Ext/Module.bsl": "// Подсистема расширения\nПроцедура Ш()\nКонецПроцедуры\n"}
        cf = _write_ext_graph(tmp_path, dict(HEADER_FILES), ext1, {})
        h = _ext_helpers((IndexBuilder().build(str(cf), build_calls=True), cf))
        res = h["search_module_headers"]("Подсистема", count_only=True)
        assert (res["total"], res["with_text"], res["total_main"], res["total_extensions"]) == (2, 2, 1, 1)
        assert res["scope"] == "main_index+live_extensions"
        empty = h["search_module_headers"]("", count_only=True)
        assert (empty["total"], empty["with_text"], empty["scope"]) == (3, 2, "main_index")

    def test_reader_split_matches_list(self, tmp_path):
        _write_project(tmp_path, HEADER_FILES)
        reader = IndexReader(IndexBuilder().build(str(tmp_path), build_calls=False))
        try:
            full = reader.search_module_headers("", limit=10**9)
            with_text = sum(1 for r in full if r["header_comment"])
            assert reader.count_module_headers_with_text("") == (len(full), with_text) == (3, 2)
            assert reader.count_module_headers("") == 3
        finally:
            reader.close()
