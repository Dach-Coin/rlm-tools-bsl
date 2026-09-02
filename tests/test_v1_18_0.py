"""Tests for v1.18.0 — устойчивость контрактов хелперов.

Лечим «хрупкость контрактов»: агент угадывает правдоподобный, но неверный
контракт и получает либо жёсткую ошибку (KeyError/AttributeError/FileNotFound),
либо «тихо пустой» результат, тратя retry. Болезнь одна, лиц несколько:

- Фикс 1: `_AttrRecord` — толерантность ключей атрибутов (name <-> attr_name и т.д.).
- Фикс 2: `_split_params` — `params` строкой превращается в `list[str]` на helper-границе.
- Фикс 3: `find_callers_context` — самокоррекция вместо тихой пустоты (offset/int-guard).
- Фикс 4a: `git_search` пустой паттерн -> `[{error, hint}]` (а не невнятный таймаут).
- Фикс 4b: HINT путей по формату дампа (CF -> Ext/*.xml, EDT -> *.mdo).
"""

from __future__ import annotations

import json

import pytest


# ============================================================================
# Фикс 1 — _AttrRecord: толерантность ключей атрибутов
# ============================================================================


def test_attrrecord_bidirectional_getitem():
    """`['attr_name']` и `['name']` оба работают на одной записи (оба направления)."""
    from rlm_tools_bsl.bsl_knowledge import _AttrRecord

    # Каноничные ключи структуры (get_object_full_structure)
    struct_rec = _AttrRecord({"name": "Контрагент", "synonym": "Контр.", "type": "СправочникСсылка"})
    assert struct_rec["name"] == "Контрагент"
    assert struct_rec["attr_name"] == "Контрагент"  # alias
    assert struct_rec["synonym"] == "Контр."
    assert struct_rec["attr_synonym"] == "Контр."  # alias
    assert struct_rec["type"] == "СправочникСсылка"
    assert struct_rec["attr_type"] == "СправочникСсылка"  # alias

    # Каноничные ключи find_attributes
    attr_rec = _AttrRecord({"attr_name": "Сумма", "attr_synonym": "Сумма док.", "attr_type": "Число"})
    assert attr_rec["attr_name"] == "Сумма"
    assert attr_rec["name"] == "Сумма"  # alias
    assert attr_rec["attr_synonym"] == "Сумма док."
    assert attr_rec["synonym"] == "Сумма док."  # alias
    assert attr_rec["attr_type"] == "Число"
    assert attr_rec["type"] == "Число"  # alias


def test_attrrecord_get_method():
    """`.get()` отдаёт значение по «чужому» ключу и default для отсутствующего."""
    from rlm_tools_bsl.bsl_knowledge import _AttrRecord

    rec = _AttrRecord({"name": "X", "synonym": "Y", "type": "Z"})
    assert rec.get("attr_name") == "X"
    assert rec.get("name") == "X"
    assert rec.get("attr_kind") is None  # нет ни прямого, ни алиаса
    assert rec.get("attr_kind", "Реквизит") == "Реквизит"


def test_attrrecord_contains():
    """`in` истинно и для каноничного ключа, и для алиаса; ложно для чужого."""
    from rlm_tools_bsl.bsl_knowledge import _AttrRecord

    rec = _AttrRecord({"attr_name": "X", "attr_synonym": "Y", "attr_type": "Z"})
    assert "attr_name" in rec
    assert "name" in rec  # alias
    assert "synonym" in rec  # alias
    assert "type" in rec  # alias
    assert "attr_kind" not in rec  # нет алиаса


def test_attrrecord_keys_are_canonical():
    """Итерация / .keys() / len() остаются КАНОНИЧНЫМИ (агент видит реальные имена)."""
    from rlm_tools_bsl.bsl_knowledge import _AttrRecord

    rec = _AttrRecord({"name": "X", "synonym": "Y", "type": "Z"})
    assert set(rec.keys()) == {"name", "synonym", "type"}
    assert len(rec) == 3
    # Алиасы НЕ протекают в итерацию
    assert "attr_name" not in set(rec.keys())


def test_attrrecord_json_serializes_only_real_keys():
    """json.dumps сериализует только реальные ключи -> нулевая цена по токенам."""
    from rlm_tools_bsl.bsl_knowledge import _AttrRecord

    rec = _AttrRecord({"name": "X", "synonym": "Y", "type": "Z"})
    dumped = json.loads(json.dumps(rec, ensure_ascii=False))
    assert dumped == {"name": "X", "synonym": "Y", "type": "Z"}
    assert "attr_name" not in dumped


def test_attrrecord_is_dict_instance():
    """isinstance(..., dict) истинно для подкласса (важно для dedup/rank-merge)."""
    from rlm_tools_bsl.bsl_knowledge import _AttrRecord

    rec = _AttrRecord({"name": "X"})
    assert isinstance(rec, dict)


def test_attrrecord_missing_key_without_alias_raises():
    """Полностью отсутствующий ключ (без алиаса) -> честный KeyError."""
    from rlm_tools_bsl.bsl_knowledge import _AttrRecord

    rec = _AttrRecord({"name": "X"})
    with pytest.raises(KeyError):
        _ = rec["completely_unknown_key"]


def test_attrrecord_missing_alias_target_raises():
    """Если у записи нет ни ключа, ни его алиас-источника -> KeyError (не None)."""
    from rlm_tools_bsl.bsl_knowledge import _AttrRecord

    # Запись без synonym/attr_synonym вообще
    rec = _AttrRecord({"name": "X"})
    with pytest.raises(KeyError):
        _ = rec["attr_synonym"]
    with pytest.raises(KeyError):
        _ = rec["synonym"]


# ---- Фикс 1 интеграция: helper-return-поверхности толерантны ----------------


def _cf_doc_xml(name: str) -> str:
    """CF Document с одним реквизитом и одной ТЧ (колонка) — для on-disk live-пути."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses" '
        'xmlns:v8="http://v8.1c.ru/8.1/data/core">'
        f"<Document><Properties><Name>{name}</Name></Properties>"
        "<ChildObjects>"
        "<Attribute><Properties>"
        "<Name>Контрагент</Name>"
        "<Type><v8:Type>xs:string</v8:Type></Type>"
        "</Properties></Attribute>"
        "<TabularSection><Properties><Name>Товары</Name></Properties>"
        "<ChildObjects>"
        "<Attribute><Properties>"
        "<Name>Номенклатура</Name>"
        "<Type><v8:Type>xs:string</v8:Type></Type>"
        "</Properties></Attribute>"
        "</ChildObjects></TabularSection>"
        "</ChildObjects>"
        "</Document>"
        "</MetaDataObject>"
    )


def _write_cf_doc(base_path, name: str) -> None:
    doc_dir = base_path / "Documents" / name / "Ext"
    doc_dir.mkdir(parents=True)
    (doc_dir / "Document.xml").write_text(_cf_doc_xml(name), encoding="utf-8")


def _assert_both_dialects(rec, canonical_name_value):
    """Запись принимает и каноничный, и «чужой» ключ; ровно один диалект каноничен."""
    from rlm_tools_bsl.bsl_knowledge import _AttrRecord

    assert isinstance(rec, _AttrRecord)
    assert isinstance(rec, dict)
    # доступ работает в обе стороны (через [], .get, in)
    assert rec["name"] == canonical_name_value
    assert rec["attr_name"] == canonical_name_value
    assert rec.get("name") == canonical_name_value
    assert rec.get("attr_name") == canonical_name_value
    assert "name" in rec and "attr_name" in rec
    # ...но в .keys()/итерацию/json протекает РОВНО ОДИН (каноничный) диалект
    keys = set(rec.keys())
    assert ("attr_name" in keys) != ("name" in keys), f"оба диалекта в keys: {keys}"


def test_get_object_full_structure_live_tolerant_keys(bsl_env):
    """Live-XML путь get_object_full_structure: attributes и columns ТЧ толерантны."""
    from test_v1_10_0 import _StubIdxReader, _make_bsl_with_stub_idx

    _write_cf_doc(bsl_env.path, "ЗаказГофсLive")
    bsl = _make_bsl_with_stub_idx(str(bsl_env.path), _StubIdxReader())
    res = bsl["get_object_full_structure"]("ЗаказГофсLive")

    assert "error" not in res
    attr = next(a for a in res["attributes"] if a["name"] == "Контрагент")
    _assert_both_dialects(attr, "Контрагент")
    # каноничный контракт get_object_full_structure — name/synonym/type
    assert "attr_name" not in set(attr.keys())

    ts = next(t for t in res["tabular_sections"] if t["name"] == "Товары")
    col = ts["columns"][0]
    _assert_both_dialects(col, "Номенклатура")


def test_get_object_full_structure_index_tolerant_keys(bsl_env):
    """Index путь get_object_full_structure оборачивает строки из reader."""
    from test_v1_10_0 import _StubIdxReader, _make_bsl_with_stub_idx

    class _RowsStub(_StubIdxReader):
        def get_object_attributes(self, **kwargs):
            return [
                {
                    "object_name": "ЗаказГофсIdx",
                    "category": "Documents",
                    "attr_name": "Контрагент",
                    "attr_synonym": "Контрагент",
                    "attr_type": ["СправочникСсылка.Контрагенты"],
                    "attr_kind": "attribute",
                    "ts_name": None,
                },
            ]

    _write_cf_doc(bsl_env.path, "ЗаказГофсIdx")  # чтобы объект резолвился
    bsl = _make_bsl_with_stub_idx(str(bsl_env.path), _RowsStub())
    res = bsl["get_object_full_structure"]("ЗаказГофсIdx")

    assert "error" not in res
    attr = next(a for a in res["attributes"] if a["name"] == "Контрагент")
    _assert_both_dialects(attr, "Контрагент")
    assert "attr_name" not in set(attr.keys())


def test_find_attributes_index_tolerant_keys(bsl_env):
    """find_attributes index-passthrough оборачивает строки reader (контракт attr_*)."""
    from test_v1_10_0 import _StubIdxReader, _make_bsl_with_stub_idx

    class _RowsStub(_StubIdxReader):
        def get_object_attributes(self, **kwargs):
            return [
                {
                    "object_name": "ЗаказКлиента",
                    "category": "Documents",
                    "attr_name": "Контрагент",
                    "attr_synonym": "Контрагент",
                    "attr_type": ["СправочникСсылка.Контрагенты"],
                    "attr_kind": "attribute",
                    "ts_name": None,
                    "source_file": "Documents/ЗаказКлиента/Ext/Document.xml",
                },
            ]

    bsl = _make_bsl_with_stub_idx(str(bsl_env.path), _RowsStub())
    rows = bsl["find_attributes"](name="Контрагент")

    assert rows, "ожидали хотя бы одну строку из index-passthrough"
    rec = next(r for r in rows if r["attr_name"] == "Контрагент")
    _assert_both_dialects(rec, "Контрагент")
    # каноничный контракт find_attributes — attr_*
    assert "attr_name" in set(rec.keys())


def test_find_attributes_live_tolerant_keys(bsl_env):
    """find_attributes live-XML путь (object_name) оборачивает записи."""
    from test_v1_10_0 import _StubIdxReader, _make_bsl_with_stub_idx

    _write_cf_doc(bsl_env.path, "ЗаказFaLive")
    bsl = _make_bsl_with_stub_idx(str(bsl_env.path), _StubIdxReader())
    rows = bsl["find_attributes"](object_name="Documents/ЗаказFaLive")

    assert rows, "ожидали реквизиты из live XML"
    rec = next(r for r in rows if r["attr_name"] == "Контрагент")
    _assert_both_dialects(rec, "Контрагент")


def test_parse_object_xml_tolerant_keys(bsl_env):
    """parse_object_xml оборачивает attributes и tabular_sections[*].attributes."""
    from test_v1_10_0 import _StubIdxReader, _make_bsl_with_stub_idx

    _write_cf_doc(bsl_env.path, "ЗаказParse")
    bsl = _make_bsl_with_stub_idx(str(bsl_env.path), _StubIdxReader())
    parsed = bsl["parse_object_xml"]("Documents/ЗаказParse")

    attr = next(a for a in parsed["attributes"] if a["name"] == "Контрагент")
    _assert_both_dialects(attr, "Контрагент")

    # У parse_object_xml колонки ТЧ лежат под ключом `attributes` (НЕ `columns`)
    ts = next(t for t in parsed["tabular_sections"] if t["name"] == "Товары")
    col = ts["attributes"][0]
    _assert_both_dialects(col, "Номенклатура")


# ============================================================================
# Фикс 2 — _split_params: params строкой -> list[str] имён параметров
# ============================================================================


def test_split_params_basic_and_znach_prefix():
    """Несколько параметров, префикс `Знач` отбрасывается."""
    from rlm_tools_bsl.bsl_knowledge import _split_params

    assert _split_params("Знач А, Б, С") == ["А", "Б", "С"]
    assert _split_params("А") == ["А"]


def test_split_params_empty():
    from rlm_tools_bsl.bsl_knowledge import _split_params

    assert _split_params("") == []
    assert _split_params("   ") == []
    assert _split_params("\n  \t ") == []


def test_split_params_znach_val_case_insensitive():
    """`Знач`/`ЗНАЧ`/`знач`/`Val` — регистронезависимо (ключевые слова 1С)."""
    from rlm_tools_bsl.bsl_knowledge import _split_params

    assert _split_params("знач х, ЗНАЧ у, Val z, vAl w") == ["х", "у", "z", "w"]


def test_split_params_default_value_dropped():
    """Хвост `= <default>` отбрасывается, остаётся имя."""
    from rlm_tools_bsl.bsl_knowledge import _split_params

    assert _split_params("А = Неопределено") == ["А"]
    assert _split_params("Знач Б = 5, В = Истина") == ["Б", "В"]


def test_split_params_default_string_with_comma():
    """Строковый default с запятой НЕ создаёт фантомный параметр."""
    from rlm_tools_bsl.bsl_knowledge import _split_params

    assert _split_params('Разделитель = ", "') == ["Разделитель"]
    assert _split_params('А, Б = ", ", В') == ["А", "Б", "В"]


def test_split_params_doubled_quotes():
    """Удвоенные кавычки `""` внутри строкового литерала не ломают разбор."""
    from rlm_tools_bsl.bsl_knowledge import _split_params

    assert _split_params('Сообщение = "Скажи ""привет"""') == ["Сообщение"]
    # запятая внутри литерала с экранированной кавычкой — тоже не разделитель
    assert _split_params('Шаблон = "a"", b", Хвост') == ["Шаблон", "Хвост"]


def test_split_params_multiline_signature():
    """Многострочная сигнатура (после merge) корректно разбивается."""
    from rlm_tools_bsl.bsl_knowledge import _split_params

    assert _split_params("А,\n    Б,\n    С") == ["А", "Б", "С"]
    assert _split_params("Знач Таблица,\n  Колонка = Неопределено") == ["Таблица", "Колонка"]


def test_split_params_name_starting_with_znach_preserved():
    """Имя параметра, начинающееся на `Знач` (без границы слова), НЕ режется."""
    from rlm_tools_bsl.bsl_knowledge import _split_params

    assert _split_params("ЗначениеПоля") == ["ЗначениеПоля"]
    assert _split_params("Знач ЗначениеПоля") == ["ЗначениеПоля"]


# ---- Фикс 2 интеграция: params == list на всех агент-поверхностях ------------


class _ParamsStub:
    """Стаб ридера для проверки нормализации params на helper-границе."""

    has_calls = False
    has_fts = True

    def __init__(self, methods=None, search_rows=None):
        self._methods = methods  # None => live-парс; list => index-путь
        self._search = search_rows or []

    def get_methods_by_path(self, path):
        return None if self._methods is None else [dict(m) for m in self._methods]

    def get_overrides_for_path(self, path):
        return None

    def search_methods(self, query, limit=30):
        return [dict(r) for r in self._search]

    def search_objects(self, *a, **k):
        return None

    def get_all_modules(self):
        return []


def _write_common_module(base_path, name: str, body: str) -> str:
    mod_dir = base_path / "CommonModules" / name / "Ext"
    mod_dir.mkdir(parents=True)
    (mod_dir / "Module.bsl").write_text(body, encoding="utf-8")
    return f"CommonModules/{name}/Ext/Module.bsl"


def test_extract_procedures_live_params_is_list(bsl_env):
    """Live-парс (_parse_procedures) отдаёт params списком имён."""
    from test_v1_10_0 import _make_bsl_with_stub_idx

    path = _write_common_module(bsl_env.path, "ТестLive", "Процедура Тест(Знач А, Б = 5) Экспорт\nКонецПроцедуры\n")
    bsl = _make_bsl_with_stub_idx(str(bsl_env.path), _ParamsStub(methods=None))
    procs = bsl["extract_procedures"](path)

    proc = next(p for p in procs if p["name"] == "Тест")
    assert proc["params"] == ["А", "Б"]


def test_extract_procedures_index_params_normalized_to_list(bsl_env):
    """Index-граница (get_methods_by_path) нормализует строковые params в list."""
    from test_v1_10_0 import _make_bsl_with_stub_idx

    path = "CommonModules/ТестIdx/Ext/Module.bsl"  # на диске нет -> live-fill пропущен
    reader = _ParamsStub(
        methods=[
            {"name": "Метод1", "type": "Процедура", "line": 1, "end_line": 3, "is_export": True, "params": "Знач А, Б"},
        ]
    )
    bsl = _make_bsl_with_stub_idx(str(bsl_env.path), reader)
    procs = bsl["extract_procedures"](path)

    proc = next(p for p in procs if p["name"] == "Метод1")
    assert proc["params"] == ["А", "Б"]


def test_search_methods_index_params_normalized_to_list(bsl_env):
    """search_methods нормализует строковые params из FTS-ридера в list."""
    from test_v1_10_0 import _make_bsl_with_stub_idx

    reader = _ParamsStub(
        search_rows=[
            {
                "name": "Провести",
                "type": "Процедура",
                "is_export": True,
                "line": 10,
                "end_line": 20,
                "params": "Знач Документ, Режим = Неопределено",
                "module_path": "CommonModules/М/Ext/Module.bsl",
                "object_name": "М",
                "rank": -1.0,
            }
        ]
    )
    bsl = _make_bsl_with_stub_idx(str(bsl_env.path), reader)
    rows = bsl["search_methods"]("Провести")

    rec = next(r for r in rows if r["name"] == "Провести")
    assert rec["params"] == ["Документ", "Режим"]


def test_search_methods_params_idempotent_for_list_input(bsl_env):
    """Если ридер уже отдал params списком — повторно НЕ разбиваем (идемпотентность)."""
    from test_v1_10_0 import _make_bsl_with_stub_idx

    reader = _ParamsStub(
        search_rows=[
            {
                "name": "УжеСписок",
                "type": "Функция",
                "is_export": False,
                "line": 1,
                "end_line": 2,
                "params": ["X", "Y"],  # уже list
                "module_path": "CommonModules/М/Ext/Module.bsl",
                "object_name": "М",
                "rank": -1.0,
            }
        ]
    )
    bsl = _make_bsl_with_stub_idx(str(bsl_env.path), reader)
    rows = bsl["search_methods"]("УжеСписок")

    rec = next(r for r in rows if r["name"] == "УжеСписок")
    assert rec["params"] == ["X", "Y"]


# ============================================================================
# Фикс 3 — find_callers_context: самокоррекция вместо тихой пустоты
# ============================================================================


def test_find_callers_context_offset_overshoot_hint(bsl_env):
    """offset >= total (индекс): returned=0, но это не «нет вызовов» — даём HINT
    о вероятно перепутанных позиционных аргументах, а НЕ тихую пустоту."""
    from test_v1_10_0 import _make_bsl_with_stub_idx

    class _OvershootStub:
        has_calls = True

        def get_callers(self, proc, module_hint, offset, limit):
            return {
                "callers": [],
                "_meta": {
                    "total_callers": 6,
                    "returned": 0,
                    "offset": offset,
                    "exact_available": True,
                    "target_exact": False,
                    "exact_rows": 0,
                    "fallback_rows": 0,
                },
            }

    bsl = _make_bsl_with_stub_idx(str(bsl_env.path), _OvershootStub())
    res = bsl["find_callers_context"]("Провести", "", 10, 50)

    # Индексный результат сохранён (не выброшен в FS-fallback с total_callers=0)
    assert res["_meta"]["total_callers"] == 6
    assert "hint" in res["_meta"]
    assert "offset" in res["_meta"]["hint"].lower()
    assert "10" in res["_meta"]["hint"] and "6" in res["_meta"]["hint"]


def test_find_callers_context_module_hint_int_no_crash(bsl_env):
    """module_hint=int (агент сдвинул позиционные аргументы): НЕ падаем
    (реальный _normalize_module_hint делает hint.strip()), коэрсим в '' + arg_warning."""
    from test_v1_10_0 import _make_bsl_with_stub_idx

    class _HintStrictStub:
        has_calls = True

        def get_callers(self, proc, module_hint, offset, limit):
            module_hint.strip()  # как _normalize_module_hint — AttributeError на int
            return {
                "callers": [{"file": "CommonModules/М/Ext/Module.bsl", "caller_name": "Вызыв", "line": 3}],
                "_meta": {
                    "total_callers": 1,
                    "returned": 1,
                    "offset": offset,
                    "exact_available": True,
                    "target_exact": False,
                    "exact_rows": 1,
                    "fallback_rows": 0,
                },
            }

    bsl = _make_bsl_with_stub_idx(str(bsl_env.path), _HintStrictStub())
    # module_hint=5 -> без guard упадёт AttributeError внутри ридера
    res = bsl["find_callers_context"]("Провести", 5, 0, 50)

    assert res["callers"], "не должно падать; module_hint=int коэрсится в ''"
    assert "arg_warning" in res["_meta"]
    assert "module_hint" in res["_meta"]["arg_warning"]


def test_find_callers_context_fs_fallback_offset_overshoot_hint(bsl_env):
    """FS-fallback (нет call-индекса): симметричный guard по total_files —
    offset за пределами кандидатов -> HINT, а не тихий пустой результат.

    bsl_env.bsl идёт по FS-пути (call-индекса нет); в дефолтной CF-фикстуре
    `ЗаполнитьДанные` вызывается из `ОбработкаЗаполнения` — кандидаты ЕСТЬ.
    """
    base = bsl_env.bsl["find_callers_context"]("ЗаполнитьДанные", "", 0, 50)
    assert base["callers"], "санити: на offset=0 вызывающие должны быть"

    res = bsl_env.bsl["find_callers_context"]("ЗаполнитьДанные", "", 999, 50)
    assert res["callers"] == []  # страница пуста (offset за пределами)
    assert "hint" in res["_meta"]
    assert "offset" in res["_meta"]["hint"].lower()


# ============================================================================
# Фикс 4a — git_search: пустой паттерн -> [{error, hint}] (list-форма)
# ============================================================================


def test_git_search_empty_pattern_returns_error_hint(bsl_env):
    """Пустой/пробельный паттерн -> внятный {results: [], error, hint} (не таймаут-заглушка).

    git_search регистрируем через register_git_search='force' (без реального
    git-репо) — guard пустого паттерна срабатывает ДО вызова git.
    """
    import os

    from rlm_tools_bsl.bsl_helpers import make_bsl_helpers
    from rlm_tools_bsl.format_detector import detect_format
    from rlm_tools_bsl.helpers import make_helpers

    path = str(bsl_env.path)
    if not os.path.exists(os.path.join(path, "Configuration.xml")):
        with open(os.path.join(path, "Configuration.xml"), "w") as f:
            f.write("<Configuration/>")
    helpers, resolve_safe = make_helpers(path)
    bsl = make_bsl_helpers(
        base_path=path,
        resolve_safe=resolve_safe,
        read_file_fn=helpers["read_file"],
        grep_fn=helpers["grep"],
        glob_files_fn=helpers["glob_files"],
        format_info=detect_format(path),
        register_git_search="force",
    )

    res = bsl["git_search"]("")
    assert isinstance(res, dict), "контракт git_search — словарь (v1.34.0)"
    assert res["results"] == [] and res["returned"] == 0
    assert res["error"] and res["hint"]

    res2 = bsl["git_search"]("   ")
    assert isinstance(res2, dict) and res2["error"] and res2["results"] == []


# ============================================================================
# Фикс 4b — HINT путей по формату дампа (CF -> Ext/*.xml, EDT -> *.mdo)
# ============================================================================


def _bsl_with_format(path: str, source_format):
    """Построить bsl-хелперы с явно заданным форматом дампа (CF/EDT)."""
    import os

    from rlm_tools_bsl.bsl_helpers import make_bsl_helpers
    from rlm_tools_bsl.format_detector import FormatInfo
    from rlm_tools_bsl.helpers import make_helpers

    if not os.path.exists(os.path.join(path, "Configuration.xml")):
        with open(os.path.join(path, "Configuration.xml"), "w") as f:
            f.write("<Configuration/>")
    helpers, resolve_safe = make_helpers(path)
    fi = FormatInfo(
        primary_format=source_format,
        root_path=path,
        bsl_file_count=0,
        has_configuration_xml=True,
        metadata_categories_found=[],
    )
    return make_bsl_helpers(
        base_path=path,
        resolve_safe=resolve_safe,
        read_file_fn=helpers["read_file"],
        grep_fn=helpers["grep"],
        glob_files_fn=helpers["glob_files"],
        format_info=fi,
    )


def test_resolve_object_xml_hint_leads_with_cf_format(bsl_env):
    """CF-дамп: HINT FileNotFoundError ведёт CF-вариантом (Ext/*.xml) первым."""
    from rlm_tools_bsl.format_detector import SourceFormat

    bsl = _bsl_with_format(str(bsl_env.path), SourceFormat.CF)
    with pytest.raises(FileNotFoundError) as ei:
        bsl["parse_object_xml"]("Documents/НетТакогоДок.mdo")
    msg = str(ei.value)
    assert "(CF)" in msg and "(EDT)" in msg
    assert msg.index("(CF)") < msg.index("(EDT)"), f"CF должен идти первым на CF-дампе: {msg}"


def test_resolve_object_xml_hint_leads_with_edt_format(bsl_env):
    """EDT-дамп: HINT FileNotFoundError ведёт EDT-вариантом (*.mdo) первым."""
    from rlm_tools_bsl.format_detector import SourceFormat

    bsl = _bsl_with_format(str(bsl_env.path), SourceFormat.EDT)
    with pytest.raises(FileNotFoundError) as ei:
        bsl["parse_object_xml"]("Documents/НетТакогоДок.mdo")
    msg = str(ei.value)
    assert msg.index("(EDT)") < msg.index("(CF)"), f"EDT должен идти первым на EDT-дампе: {msg}"


def test_xml_candidate_order_cf_prefers_ext_xml(bsl_env):
    """CF-дамп: при наличии И .mdo, И Ext/*.xml резолвер берёт Ext/*.xml первым."""
    from rlm_tools_bsl.format_detector import SourceFormat

    doc = bsl_env.path / "Documents" / "ДваФормата"
    (doc / "Ext").mkdir(parents=True)
    (doc / "Ext" / "Document.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses" '
        'xmlns:v8="http://v8.1c.ru/8.1/data/core">'
        "<Document><Properties><Name>ИзXML</Name></Properties></Document></MetaDataObject>",
        encoding="utf-8",
    )
    (doc / "ДваФормата.mdo").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<mdclass:Document xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass">'
        "<name>ИзMDO</name></mdclass:Document>",
        encoding="utf-8",
    )
    bsl_cf = _bsl_with_format(str(bsl_env.path), SourceFormat.CF)
    assert bsl_cf["parse_object_xml"]("Documents/ДваФормата")["name"] == "ИзXML"


# ---- Фикс 2 follow-up: sandbox-hint про форму params (list[str], не list[dict]) ----
# Внешний бенчмарк v1.18.0 (test07/test08) показал: после смены params на list[str]
# агент, ожидавший list[dict], пишет p['name']/p.get('name') на строке-элементе и
# получает TypeError "string indices must be integers" / AttributeError — без подсказки
# (в отличие от attr_kind). Хинт конвертирует этот retry в самокоррекцию.


def test_sandbox_hint_for_params_str_indices_misuse():
    """TypeError 'string indices must be integers' при доступе p['name'] к элементу
    params (который теперь строка-имя) + params в коде → подсказка про list[str]."""
    from rlm_tools_bsl.sandbox import Sandbox

    error = "Traceback (most recent call last):\n  File ...\nTypeError: string indices must be integers\n"
    code = "for m in extract_procedures(path):\n    for p in m['params']:\n        print(p['name'])"

    out = Sandbox._add_error_hints(error, code)

    assert "list[str]" in out, f"hint про форму params не выдан: {out!r}"
    assert "params" in out


def test_sandbox_hint_for_params_attr_on_str():
    """AttributeError ''str' object has no attribute 'get'' при p.get('name') на
    элементе params + params в коде → та же подсказка про list[str]."""
    from rlm_tools_bsl.sandbox import Sandbox

    error = "Traceback (most recent call last):\nAttributeError: 'str' object has no attribute 'get'\n"
    code = "rows = find_exports(path)\nnames = [p.get('name') for p in rows[0]['params']]"

    out = Sandbox._add_error_hints(error, code)

    assert "list[str]" in out, f"hint про форму params не выдан: {out!r}"


def test_sandbox_params_hint_skipped_without_params_in_code():
    """Тот же TypeError, но в коде нет обращения к params → params-hint НЕ выдаём
    (не лезем в чужой 'string indices' от несвязанного кода)."""
    from rlm_tools_bsl.sandbox import Sandbox

    error = "TypeError: string indices must be integers"
    code = "d = some_dict\nprint(d['a']['b'])"

    out = Sandbox._add_error_hints(error, code)

    assert "list[str]" not in out


# === v1.19.0 tolerant-contract sandbox hints ===


def test_sandbox_hint_safe_grep_wrong_kwarg():
    """safe_grep(path=...) / safe_grep(hint=...) → подсказка про name_hint."""
    from rlm_tools_bsl.sandbox import Sandbox

    error = "TypeError: safe_grep() got an unexpected keyword argument 'path'"
    code = "r = safe_grep('Pattern', path='SomeModule')"
    out = Sandbox._add_error_hints(error, code)
    assert "name_hint" in out


def test_sandbox_hint_dict_sliced_as_list():
    """d[:N] на dict → KeyError: slice(...) → подсказка взять список по ключу."""
    from rlm_tools_bsl.sandbox import Sandbox

    error = "KeyError: slice(None, 10, None)"
    code = "flow = analyze_document_flow('X')\nprint(flow[:10])"
    out = Sandbox._add_error_hints(error, code)
    assert "dict" in out and "[:N]" in out


def test_sandbox_hint_read_procedure_returns_str():
    """read_procedure(...).get() → подсказка, что вернулась строка-тело, не dict."""
    from rlm_tools_bsl.sandbox import Sandbox

    error = "AttributeError: 'str' object has no attribute 'get'"
    code = "body = read_procedure(path, 'M')\nprint(body.get('x'))"
    out = Sandbox._add_error_hints(error, code)
    assert "read_procedure" in out and ("СТРОКУ" in out or "строк" in out)


def test_sandbox_hint_detect_extensions_iterated_as_list():
    """detect_extensions() итерируют как список → подсказка про nearby_extensions."""
    from rlm_tools_bsl.sandbox import Sandbox

    error = "AttributeError: 'str' object has no attribute 'get'"
    code = "exts = detect_extensions()\nfor e in exts:\n    print(e.get('name'))"
    out = Sandbox._add_error_hints(error, code)
    assert "nearby_extensions" in out


def test_sandbox_hint_detect_extensions_indexed_as_list():
    """detect_extensions()[0] на dict → KeyError: 0 → та же подсказка."""
    from rlm_tools_bsl.sandbox import Sandbox

    error = "KeyError: 0"
    code = "first = detect_extensions()[0]"
    out = Sandbox._add_error_hints(error, code)
    assert "nearby_extensions" in out
