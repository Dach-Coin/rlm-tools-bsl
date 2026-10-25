"""v1.45.0 — ни ложного «нет», ни чужого «есть».

Релиз read-time: схема индекса и BUILDER_VERSION не тронуты. Приватные фикстуры других
файлов не импортируются (каталог tests не пакет) — деревья собираются здесь же.
"""

from __future__ import annotations

import json
import os
import sys
import textwrap
from pathlib import Path

import pytest

from rlm_tools_bsl.bsl_helpers import build_helper_metadata_snapshot, make_bsl_helpers
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


# ═══════════════ Задача 0 — запас бюджета подписей (доноры) ═══════════════

# DONOR_FRAGMENTS: пять удалений прозы (find_event_subscriptions, find_unresolved_calls, count_matches,
# find_definition, parse_form) и значение 'callers-reverse' у find_path; правки типов (`:bool`, `dict (`)
# переноса не требуют — у них проверяется только отсутствие в sig
DONOR_FRAGMENTS = [
    (
        "find_event_subscriptions",
        "; полное имя — ТОЧНОЕ совпадение, фрагмент — подстрока; 'Документ.X' -> category-aware",
    ),
    ("find_unresolved_calls", "  # адресат в тексте: Модуль.Метод, Коллекция.X.Метод, запуск по имени"),
    ("count_matches", "; там, где grep отказывает (>5000)"),
    ("find_definition", "; hint по объекту — ТОЧНОЕ имя"),
    ("parse_form", "— сравнение по ЭЛЕМЕНТУ — "),
    ("find_path", "'callers-reverse'"),
]

DONOR_TYPE_FRAGMENTS = [
    ("get_object_full_structure", "index_used:bool"),
    ("get_object_full_structure", "ts_synonyms_available:bool"),
    ("analyze_document_flow", "# dict ("),
]


@pytest.mark.parametrize("helper, fragment", DONOR_FRAGMENTS)
def test_donor_prose_moved_to_recipe(helper, fragment):
    entry = build_helper_metadata_snapshot()[helper]
    assert fragment not in entry["sig"] and fragment.strip("; #").strip() in entry["recipe"]


@pytest.mark.parametrize("helper, fragment", DONOR_TYPE_FRAGMENTS)
def test_donor_type_annotation_dropped_from_sig(helper, fragment):
    assert fragment not in build_helper_metadata_snapshot()[helper]["sig"]


def test_donor_kept_frozen_and_warning_fragments():
    snap = build_helper_metadata_snapshot()
    # хвост posting=None и fallback_reason:str|None — не доноры (гард ложного отрицания)
    assert "posting=None" in snap["get_object_full_structure"]["sig"]
    assert "fallback_reason:str|None" in snap["get_object_full_structure"]["sig"]
    # маркеры гарда типов parse_form остаются
    for marker in ("list[str]", "CF", "EDT", "DynamicList", "rsplit("):
        assert marker in snap["parse_form"]["sig"], marker
    # у find_path ключ direction остаётся в подписи (снято только значение)
    assert "direction" in snap["find_path"]["sig"]


# ═══════════════ Задача 2 (Д3) — обходы графа без повторных запросов ═══════════════

# Цель ← А (зовёт Цель трижды) ← Б (зовёт А дважды) ← В (зовёт Б дважды) ← Корень: каждая строка
# вызывающего — отдельная запись очереди, и до правки повтор пары (имя, подсказка) стоил полного запроса.
_CHAIN_MOD = "CommonModules/М/Ext/Module.bsl"
_CHAIN_TEXT = "\n".join(
    [
        "Процедура Цель() Экспорт",
        "КонецПроцедуры",
        "",
        "Процедура А() Экспорт",
        "\tЦель();",
        "\tЦель();",
        "\tЦель();",
        "КонецПроцедуры",
        "",
        "Процедура Б() Экспорт",
        "\tА();",
        "\tА();",
        "КонецПроцедуры",
        "",
        "Процедура В() Экспорт",
        "\tБ();",
        "\tБ();",
        "КонецПроцедуры",
        "",
        "Процедура Корень() Экспорт",
        "\tВ();",
        "КонецПроцедуры",
        "",
    ]
)


def _path_elem(name, line, kind="call"):
    return {"name": name, "module_path": _CHAIN_MOD, "call_line": line, "call_kind": kind}


def _caller(name, line, level):
    return {
        "caller_name": name,
        "module_path": _CHAIN_MOD,
        "category": "CommonModules",
        "object_name": "М",
        "line": line,
        "is_export": True,
        "level": level,
        "call_kind": "call",
    }


def _chain_meta(max_depth, nodes, precision):
    return {
        "max_depth": max_depth,
        "nodes_expanded": nodes,
        "visited_cap": 2000,
        "budget_exceeded": False,
        "from_key": f"{_CHAIN_MOD}::корень",
        "to_exact": True,
        "to_key": f"{_CHAIN_MOD}::цель",
        "precision": precision,
        "direction": "callers-reverse",
    }


# Ответы HEAD (v1.44.0) на этой фикстуре — память пар обязана их не менять ни на символ.
_PATH_FOUND_HEAD = {
    "found": True,
    "from": "Корень",
    "to": "Цель",
    "path": [
        _path_elem("Корень", 21),
        _path_elem("В", 16),
        _path_elem("Б", 11),
        _path_elem("А", 5),
        {"name": "Цель", "module_path": _CHAIN_MOD, "call_line": None, "call_kind": None},
    ],
    "depth": 4,
    "_meta": _chain_meta(4, 4, "exact"),
}
_PATH_MISS_HEAD = {
    "found": False,
    "from": "Корень",
    "to": "Цель",
    "path": None,
    "depth": 0,
    "_meta": _chain_meta(3, 3, "heuristic"),
}


def _node(name, callers, exact_rows):
    return {
        "name": name,
        "target_hint": _CHAIN_MOD,
        "target_key": f"{_CHAIN_MOD}::{name.casefold()}",
        "meta": {"exact_rows": exact_rows, "fallback_rows": 0, "exact_available": True, "target_exact": True},
        "callers": callers,
    }


_HIERARCHY_HEAD = {
    "root": "Цель",
    "direction": "callers",
    "depth": 3,
    "tree": [
        _node("Цель", [_caller("А", 5, 1), _caller("А", 6, 1), _caller("А", 7, 1)], 3),
        _node("А", [_caller("Б", 11, 2), _caller("Б", 12, 2)], 2),
        _node("Б", [_caller("В", 16, 3), _caller("В", 17, 3)], 2),
    ],
    "visited": 3,
    "truncated_targets": [],
    "_meta": {
        "exact_available": True,
        "root_exact": True,
        "exact_targets": 3,
        "fallback_targets": 0,
        "exact_rows": 7,
        "fallback_rows": 0,
        "node_budget_exceeded": False,
        "visited_cap": 2000,
    },
}


class TestTraversalQueryMemo:
    @pytest.fixture
    def chain(self, tmp_path, monkeypatch):
        root = _cf_doc_tree(tmp_path / "cf", {_CHAIN_MOD: _CHAIN_TEXT})
        reader = _reader_for(root, monkeypatch)
        calls = {"n": 0}
        orig = IndexReader.get_callers

        def counting(self, *args, **kwargs):
            calls["n"] += 1
            return orig(self, *args, **kwargs)

        monkeypatch.setattr(IndexReader, "get_callers", counting)
        bsl = _bsl_for(root, reader, idx_zero_callers_authoritative=True)
        return bsl, calls

    def test_find_path_does_not_requery_duplicate_queue_entries(self, chain):
        bsl, calls = chain
        res = bsl["find_path"]("Корень", "Цель", max_depth=4, from_hint=_CHAIN_MOD, to_hint=_CHAIN_MOD)
        assert res == _PATH_FOUND_HEAD
        # запросов — по числу РАЗНЫХ пар (имя, подсказка); в фикстуре (точные подсказки, без расширений)
        # это nodes_expanded; до правки — по числу строк очереди (7)
        assert calls["n"] == res["_meta"]["nodes_expanded"]

    def test_find_path_conclusive_miss_unchanged_with_fewer_queries(self, chain):
        bsl, calls = chain
        res = bsl["find_path"]("Корень", "Цель", max_depth=3, from_hint=_CHAIN_MOD, to_hint=_CHAIN_MOD)
        assert res == _PATH_MISS_HEAD
        assert calls["n"] == res["_meta"]["nodes_expanded"]  # до правки — 6

    def test_find_call_hierarchy_does_not_requery_duplicate_queue_entries(self, chain):
        bsl, calls = chain
        res = bsl["find_call_hierarchy"]("Цель", depth=3, module_hint=_CHAIN_MOD)
        assert res == _HIERARCHY_HEAD
        assert calls["n"] == res["visited"]  # до правки — 6


# ═══════════════ Задача 1 (Д4, Р1) — авторитетность графа на stale_age по выборочной сверке ═══════════════

_SA_MOD = "CommonModules/М/Ext/Module.bsl"
_SA_TEXT = "\n".join(
    [
        "Процедура НетВызовов() Экспорт",
        "КонецПроцедуры",
        "",
        "Процедура Дубль() Экспорт",
        "КонецПроцедуры",
        "",
        "Процедура Корень() Экспорт",
        "\tДубль();",
        "КонецПроцедуры",
        "",
    ]
)
_SA_SPEC = {
    _SA_MOD: _SA_TEXT,
    "CommonModules/Н/Ext/Module.bsl": "Процедура Дубль() Экспорт\nКонецПроцедуры\n",
    "Documents/Д/Ext/ObjectModule.bsl": "Процедура ОбработкаПроведения(Отказ, Режим)\nКонецПроцедуры\n",
}


def _start_json(root: Path, **kw) -> dict:
    from rlm_tools_bsl.server import _rlm_start

    start = json.loads(_rlm_start(path=str(root), query="", domains=["весь каталог"], **kw))
    assert start.get("session_id"), start
    return start


def _execute_json(start: dict, expr: str):
    """Значение выражения из песочницы сессии (через json — в ответе только stdout)."""
    from rlm_tools_bsl.server import _rlm_execute

    res = json.loads(_rlm_execute(start["session_id"], f"import json\nprint(json.dumps({expr}, ensure_ascii=False))"))
    assert not res.get("error"), res
    return json.loads(res["stdout"])


def _end(start: dict) -> None:
    from rlm_tools_bsl.server import _rlm_end

    _rlm_end(start["session_id"])


def _shift_mtimes(root: Path, seconds: float = 10.0) -> None:
    for p in root.rglob("*.bsl"):
        st = p.stat()
        os.utime(p, (st.st_atime, st.st_mtime + seconds))


class TestStaleAgeAuthority:
    @pytest.fixture
    def stale_root(self, tmp_path, monkeypatch):
        """Индекс фикстуры, объявленный stale_age (порог возраста 0 дней); выборка берёт все модули."""
        from rlm_tools_bsl.server import _canonicalize_path

        monkeypatch.setenv("RLM_SANDBOX_MODE", "inline")
        root = Path(_canonicalize_path(str(tmp_path))) / "cf"
        _cf_doc_tree(root, _SA_SPEC)

        def build(sample: str = "ok", **build_kw):
            _reader_for(root, monkeypatch, **build_kw).close()
            monkeypatch.setenv("RLM_INDEX_MAX_AGE_DAYS", "0")
            monkeypatch.setenv("RLM_INDEX_SAMPLE_SIZE", "50")
            # skipped — порог по умолчанию (30) больше числа модулей фикстуры
            monkeypatch.setenv("RLM_INDEX_SAMPLE_THRESHOLD", "30" if sample == "skipped" else "1")
            if sample == "mismatch":
                _shift_mtimes(root)
            return root

        return build

    def test_stale_age_with_matching_sample_is_authoritative(self, stale_root):
        start = _start_json(stale_root("ok"))
        try:
            assert start["index"]["index_status"] == "stale_age"
            warnings = start["index"]["warnings"]
            assert any("выборочная сверка" in w and "совпадает" in w for w in warnings), warnings
            assert any("граф вызовов авторитетен" in w for w in warnings), warnings
            res = _execute_json(start, f"find_callers_context('НетВызовов', {_SA_MOD!r})")
            assert res["_meta"]["exact_available"] is True and res["_meta"].get("fallback_skipped") is True
        finally:
            _end(start)

    def test_stale_age_with_mismatching_sample_stays_non_authoritative(self, stale_root):
        # mtime сдвинут на 10 с у ВСЕХ модулей фикстуры после сборки: несовпадений больше max(1, n // 5)
        start = _start_json(stale_root("mismatch"))
        try:
            assert start["index"]["index_status"] == "stale_age"
            warnings = start["index"]["warnings"]
            assert any("расходится" in w and "index update" in w for w in warnings), warnings
            assert not any("авторитетен" in w for w in warnings), warnings
            res = _execute_json(start, f"find_callers_context('НетВызовов', {_SA_MOD!r})")
            assert res["_meta"]["exact_available"] is False  # прежний FS-маршрут
        finally:
            _end(start)

    def test_stale_age_below_sample_threshold_is_authoritative(self, stale_root):
        start = _start_json(stale_root("skipped"))
        try:
            warnings = start["index"]["warnings"]
            assert any("не проводится" in w and "граф вызовов авторитетен" in w for w in warnings), warnings
            res = _execute_json(start, f"find_callers_context('НетВызовов', {_SA_MOD!r})")
            assert res["_meta"]["exact_available"] is True and res["_meta"].get("fallback_skipped") is True
        finally:
            _end(start)

    @pytest.mark.parametrize("sample", ["ok", "skipped"])
    def test_stale_age_without_calls_never_claims_authority(self, stale_root, sample):
        start = _start_json(stale_root(sample, build_calls=False))
        try:
            warnings = start["index"]["warnings"]
            assert not any("авторитетен" in w for w in warnings), warnings
            assert any("графа вызовов в индексе нет" in w for w in warnings), warnings
            res = _execute_json(start, f"find_callers_context('НетВызовов', {_SA_MOD!r})")
            assert res["_meta"]["exact_available"] is False  # find_callers_context — FS-маршрут, как прежде
        finally:
            _end(start)

    def test_stale_age_authority_reaches_process_worker(self, stale_root, monkeypatch):
        from rlm_tools_bsl.server import _rlm_execute

        root = stale_root("ok")
        monkeypatch.setenv("RLM_SANDBOX_MODE", "process")
        start = _start_json(root, execution_timeout_seconds=2)
        try:
            expr = f"find_callers_context('НетВызовов', {_SA_MOD!r})"
            res = _execute_json(start, expr)
            assert res["_meta"]["exact_available"] is True and res["_meta"].get("fallback_skipped") is True
            dead = json.loads(_rlm_execute(start["session_id"], "while True:\n    pass"))
            assert dead["sandbox_state"]["status"] == "terminated", dead
            again = json.loads(
                _rlm_execute(start["session_id"], f"import json\nprint(json.dumps({expr}, ensure_ascii=False))")
            )
            assert again["sandbox_state"]["status"] == "restarted", again
            assert json.loads(again["stdout"]) == res  # новое поколение воркера — тот же ответ
        finally:
            _end(start)

    def test_find_path_ambiguity_guard_is_on_for_sampled_stale_age(self, stale_root):
        start = _start_json(stale_root("ok"))
        try:
            res = _execute_json(start, "find_path('Корень', 'Дубль', max_depth=2)")
            assert "error" in res and len(res["candidates"]) == 2
        finally:
            _end(start)

    def test_check_index_usable_unchanged(self, tmp_path, monkeypatch):
        from rlm_tools_bsl.bsl_index import IndexStatus, check_index_usable, get_index_db_path

        root = _cf_doc_tree(tmp_path / "cf", _SA_SPEC)
        _reader_for(root, monkeypatch).close()
        db = get_index_db_path(str(root))
        assert check_index_usable(db, str(root)) is IndexStatus.FRESH  # моложе суток — без выборки
        monkeypatch.setenv("RLM_INDEX_MAX_AGE_DAYS", "0")
        assert check_index_usable(db, str(root)) is IndexStatus.STALE_AGE  # возраст — без выборки, как прежде
        monkeypatch.setenv("RLM_INDEX_MAX_AGE_DAYS", "7")
        monkeypatch.setenv("RLM_INDEX_SKIP_SAMPLE_HOURS", "0")
        monkeypatch.setenv("RLM_INDEX_SAMPLE_THRESHOLD", "1")
        monkeypatch.setenv("RLM_INDEX_SAMPLE_SIZE", "50")
        assert check_index_usable(db, str(root)) is IndexStatus.FRESH
        _shift_mtimes(root)
        assert check_index_usable(db, str(root)) is IndexStatus.STALE_CONTENT
        monkeypatch.setenv("RLM_INDEX_SAMPLE_THRESHOLD", "30")  # модулей меньше порога — выборки нет
        assert check_index_usable(db, str(root)) is IndexStatus.FRESH

    def test_content_sample_status_values(self, tmp_path, monkeypatch):
        from rlm_tools_bsl.bsl_index import content_sample_status, get_index_db_path

        root = _cf_doc_tree(tmp_path / "cf", _SA_SPEC)
        _reader_for(root, monkeypatch).close()
        db = get_index_db_path(str(root))
        monkeypatch.setenv("RLM_INDEX_SAMPLE_SIZE", "50")
        monkeypatch.setenv("RLM_INDEX_SAMPLE_THRESHOLD", "30")
        assert content_sample_status(db, str(root)) == "skipped"
        monkeypatch.setenv("RLM_INDEX_SAMPLE_THRESHOLD", "1")
        assert content_sample_status(db, str(root)) == "ok"
        assert content_sample_status(tmp_path / "нет.db", str(root)) == "unavailable"
        _shift_mtimes(root)
        assert content_sample_status(db, str(root)) == "mismatch"

    def test_file_matches_index_stat_rule(self, tmp_path):
        from rlm_tools_bsl.bsl_index import file_matches_index_stat

        f = tmp_path / "a.bsl"
        f.write_text("x", encoding="utf-8")
        st = f.stat()
        assert file_matches_index_stat(f, st.st_mtime + 0.9, st.st_size) is True
        assert file_matches_index_stat(f, st.st_mtime + 1.5, st.st_size) is False
        assert file_matches_index_stat(f, st.st_mtime, st.st_size + 1) is False
        assert file_matches_index_stat(f, None, st.st_size) is False
        assert file_matches_index_stat(tmp_path / "нет.bsl", st.st_mtime, st.st_size) is False


# ═══════════════ Задача 3 (Д1, Д2) — прямое ребро и усечение в find_path / find_data_path ═══════════════


def _cm_descriptor(name: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">\n'
        '  <CommonModule uuid="00000000-0000-0000-0000-000000000011">\n'
        f"    <Properties><Name>{name}</Name><Server>true</Server></Properties>\n"
        "  </CommonModule>\n"
        "</MetaDataObject>\n"
    )


def _ext_descriptor(name: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses" xmlns:v8="http://v8.1c.ru/8.1/data/core">\n'
        '  <Configuration uuid="00000000-0000-0000-0000-000000000021"><Properties>'
        f"<ObjectBelonging>Adopted</ObjectBelonging><Name>{name}</Name>"
        "<ConfigurationExtensionPurpose>Customization</ConfigurationExtensionPurpose>"
        f"<NamePrefix>{name}_</NamePrefix></Properties></Configuration>\n"
        "</MetaDataObject>\n"
    )


def _ext_tree(root: Path, main: dict[str, str], exts: dict[str, dict[str, str]]) -> Path:
    """Раскладка «рядом с основной»: root/src/cf + root/src/cfe/<Имя>."""
    cf = _cf_doc_tree(root / "src" / "cf", main)
    for name, files in exts.items():
        ext_root = root / "src" / "cfe" / name
        _write(ext_root / "Configuration.xml", _ext_descriptor(name))
        for rel, text in files.items():
            _write(ext_root / rel, text)
    return cf


def _ext_bsl(cf: Path, reader, ext_names, authoritative: bool = True):
    """Хелперы сессии основной конфигурации с соседними расширениями (как у сервера)."""
    exts = {str(cf.parent / "cfe" / name): name for name in ext_names}
    return _bsl_for(
        cf,
        reader,
        idx_zero_callers_authoritative=authoritative,
        extension_paths=list(exts),
        current_config_role="main",
        current_config_name="Тест",
        current_config_root=str(cf),
        extension_name_by_root=exts,
    )


# Цель — экспорт общего модуля М; вызывающие — модули объектов документов А, Б, В (порядок rel_path).
_FP_TARGET = "CommonModules/М/Ext/Module.bsl"
_FP_DOC = "Documents/{}/Ext/ObjectModule.bsl"
_FP_SPEC = {
    _FP_TARGET: "Процедура ОбщаяЦель() Экспорт\nКонецПроцедуры\n",
    "CommonModules/М.xml": _cm_descriptor("М"),
    **{
        _FP_DOC.format(d): "Процедура ОбработкаПроведения(Отказ, Режим)\n\tМ.ОбщаяЦель();\nКонецПроцедуры\n"
        for d in ("А", "Б", "В")
    },
}

# Маршрут файлов: три вызова цели в модуле А и один в модуле Б; страница FS-маршрута — 1 файл, первым идёт А
# (порядок кандидатов задаёт _sorted_glob).
_FR_SPEC = {
    "CommonModules/А/Ext/Module.bsl": (
        "Процедура ВызовА() Экспорт\n\tМ.ОбщаяЦель();\n\tМ.ОбщаяЦель();\n\tМ.ОбщаяЦель();\nКонецПроцедуры\n"
    ),
    "CommonModules/Б/Ext/Module.bsl": "Процедура ВызовБ() Экспорт\n\tМ.ОбщаяЦель();\nКонецПроцедуры\n",
    _FP_TARGET: "Процедура ОбщаяЦель() Экспорт\nКонецПроцедуры\n",
    "CommonModules/А.xml": _cm_descriptor("А"),
    "CommonModules/Б.xml": _cm_descriptor("Б"),
    "CommonModules/М.xml": _cm_descriptor("М"),
}

_HINT_CALL_RE = __import__("re").compile(r"find_callers_context\([^)]*\)")


def _sorted_glob(glob_fn):
    """glob_files с порядком по пути: без индекса кандидаты идут в порядке обхода ФС, а он у каждой ФС свой
    (NTFS — по алфавиту, ext4 — по хешу имени с затравкой ФС)."""
    return lambda pattern: sorted(glob_fn(pattern))


@pytest.fixture
def page_one(monkeypatch):
    import rlm_tools_bsl.bsl_helpers as bh

    monkeypatch.setattr(bh, "_FIND_PATH_NODE_LIMIT", 1)
    monkeypatch.setattr(bh, "_DATA_PATH_REFS_PAGE", 1, raising=False)


class TestFindPathTruncation:
    def test_find_path_finds_direct_edge_beyond_callers_page(self, tmp_path, monkeypatch, page_one):
        root = _cf_doc_tree(tmp_path / "cf", _FP_SPEC)
        bsl = _bsl_for(root, _reader_for(root, monkeypatch))
        path_v = _FP_DOC.format("В")
        res = bsl["find_path"]("ОбработкаПроведения", "ОбщаяЦель", max_depth=1, from_hint=path_v, to_hint=_FP_TARGET)
        assert res["found"] is True and res["depth"] == 1 and res["path"][0]["module_path"] == path_v
        assert "hint" not in res and "truncated_nodes" not in res["_meta"]

    def test_find_path_inconclusive_miss_names_truncated_nodes_and_route(self, tmp_path, monkeypatch, page_one):
        root = _cf_doc_tree(tmp_path / "cf", _FP_SPEC)
        bsl = _bsl_for(root, _reader_for(root, monkeypatch))
        res = bsl["find_path"]("НеСвязан", "ОбщаяЦель", max_depth=2, to_hint=_FP_TARGET)
        assert res["found"] is False and res["_meta"]["budget_exceeded"] is True
        node = res["_meta"]["truncated_nodes"][0]
        assert node["name"] == "ОбщаяЦель" and node["page_unit"] == "rows"
        assert (node["expanded"], node["next_offset"], node["total_callers"]) == (1, 1, 3)
        assert res["_meta"]["truncated_nodes_total"] == 1
        assert "find_callers_context('ОбщаяЦель'" in res["hint"] and "offset=1" in res["hint"]
        assert "limit=2" in res["hint"]

    def test_find_path_direct_edge_from_extension_module(self, tmp_path, monkeypatch, page_one):
        ext_mod = "CommonModules/Расш1_М/Ext/Module.bsl"
        cf = _ext_tree(
            tmp_path,
            _FP_SPEC,
            {"Расш1": {ext_mod: "Процедура ИзРасширения() Экспорт\n\tМ.ОбщаяЦель();\nКонецПроцедуры\n"}},
        )
        bsl = _ext_bsl(cf, _reader_for(cf, monkeypatch), ["Расш1"])
        from_hint = f"../cfe/Расш1/{ext_mod}"
        res = bsl["find_path"]("ИзРасширения", "ОбщаяЦель", max_depth=1, from_hint=from_hint, to_hint=_FP_TARGET)
        assert res["found"] is True and res["path"][0]["module_path"] == from_hint

    def test_find_path_conclusive_miss_has_no_hint(self, tmp_path, monkeypatch):
        root = _cf_doc_tree(tmp_path / "cf", _FP_SPEC)
        bsl = _bsl_for(root, _reader_for(root, monkeypatch), idx_zero_callers_authoritative=True)
        res = bsl["find_path"]("НеСвязан", "ОбщаяЦель", max_depth=2, to_hint=_FP_TARGET)
        assert res["found"] is False and res["_meta"]["budget_exceeded"] is False
        assert "hint" not in res and "truncated_nodes" not in res["_meta"]

    def test_find_path_early_unreadable_extension_module_has_top_hint(self, tmp_path, monkeypatch):
        import rlm_tools_bsl.bsl_helpers as bh

        ext_mod = "CommonModules/Расш1_М/Ext/Module.bsl"
        cf = _ext_tree(
            tmp_path,
            _FP_SPEC,
            {"Расш1": {ext_mod: "Процедура ИзРасширения() Экспорт\n\tМ.ОбщаяЦель();\nКонецПроцедуры\n"}},
        )
        # модуль расширения из подсказки слоем не прочитан (бюджет модулей 0 — тот же маршрут, что отказ чтения)
        monkeypatch.setattr(bh, "_AUDIT_EXT_MODULE_BUDGET", 0)
        bsl = _ext_bsl(cf, _reader_for(cf, monkeypatch), ["Расш1"])
        res = bsl["find_path"](
            "ОбработкаПроведения", "ИзРасширения", from_hint=_FP_DOC.format("А"), to_hint=f"../cfe/Расш1/{ext_mod}"
        )
        assert res["found"] is False and res["_meta"]["budget_exceeded"] is True and res["_meta"]["nodes_expanded"] == 0
        assert res["hint"] and res["_meta"]["hint"]  # верхний — новый, внутренний — прежний
        assert res["hint"].startswith(res["_meta"]["hint"]) and "find_definition('ИзРасширения')" in res["hint"]

    @pytest.mark.parametrize("route", ["rows", "files"])
    def test_find_path_hint_route_executes(self, tmp_path, monkeypatch, page_one, route):
        # совет из hint исполняется и находит строку за страницей
        root = _cf_doc_tree(tmp_path / "cf", _FR_SPEC)
        reader = _reader_for(root, monkeypatch) if route == "rows" else None
        # первая страница маршрута файлов — модуль А (три строки при одном файле) на любой ФС: иначе первым мог
        # оказаться Б, и совет вёл бы к А — верно, но мимо ассерта (так было на ext4 раннера CI)
        bsl = _bsl_for(root, reader, glob_wrap=_sorted_glob if route == "files" else None)
        res = bsl["find_path"]("НеСвязан", "ОбщаяЦель", max_depth=1, to_hint=_FP_TARGET)
        assert res["found"] is False and res["_meta"]["budget_exceeded"] is True
        node = res["_meta"]["truncated_nodes"][0]
        assert node["page_unit"] == route and (node["total_callers"] is None) == (route == "files")
        call = _HINT_CALL_RE.search(res["hint"]).group(0)
        page = eval(call, {"find_callers_context": bsl["find_callers_context"]})  # noqa: S307
        assert "ВызовБ" in {c["caller_name"] for c in page["callers"]}
        if route == "files":
            assert "offset += limit" in res["hint"] and "has_more" in res["hint"]

    def test_find_path_without_calls_adds_only_declared_keys(self, tmp_path, monkeypatch, page_one):
        # --no-calls (маршрут файлов): добора нет; ответ = HEAD, кроме hint и _meta.truncated_nodes(_total)
        root = _cf_doc_tree(tmp_path / "cf", _FR_SPEC)
        bsl = _bsl_for(root, _reader_for(root, monkeypatch, build_calls=False))
        res = bsl["find_path"]("НеСвязан", "ОбщаяЦель", max_depth=1, to_hint=_FP_TARGET)
        assert set(res) == {"found", "from", "to", "path", "depth", "_meta", "hint"}
        meta = {k: v for k, v in res["_meta"].items() if k not in ("truncated_nodes", "truncated_nodes_total")}
        assert meta == {
            "max_depth": 1,
            "nodes_expanded": 1,
            "visited_cap": 2000,
            "budget_exceeded": True,
            "from_key": None,
            "to_exact": True,
            "to_key": f"{_FP_TARGET}::общаяцель",
            "precision": "heuristic",
            "direction": "callers-reverse",
        }
        assert res["_meta"]["truncated_nodes"][0]["page_unit"] == "files"


_DP_SPEC = {
    "CommonModules/Т/Ext/Module.bsl": "Процедура Т() Экспорт\nКонецПроцедуры\n",
}


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
    spec = dict(_DP_SPEC)
    # А → (Б, В); Б → Г: две исходящие ссылки у А, цепочка через Б. v1.46.0: `Изолированный` — существующий
    # объект без ссылок (конец промаха): конец-ссылка на несуществующий объект — отказ шлюза, а не обход.
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


class TestDataPathTruncation:
    def test_find_data_path_truncated_node_is_not_conclusive_and_direct_ref_found(
        self, tmp_path, monkeypatch, page_one
    ):
        root = _dp_tree(tmp_path / "cf")
        reader = _reader_for(root, monkeypatch)
        bsl = _bsl_for(root, reader)
        first = reader.find_metadata_refs_from("А", source_category="Catalogs", limit=1)[0]["ref_object"]
        other = "Catalog.В" if first == "Catalog.Б" else "Catalog.Б"
        res = bsl["find_data_path"]("Справочник.А", other.replace("Catalog.", "Справочник."), max_depth=1)
        assert res["found"] is True and res["path"][0]["to"] == other  # ссылка за страницей узла
        # v1.46.0: прежде здесь стоял конец `Справочник.Нет` — несуществующий объект теперь отвергает шлюз.
        # v1.46.0 (Д6): прежде здесь стоял неокончательный промах (`budget_exceeded=True`, `hint`, `truncated_nodes`)
        # — у узла последнего уровня (`max_depth=1`) дети не раскрывались бы вовсе, и после точечной проверки
        # прямой ссылки промах окончателен.
        res2 = bsl["find_data_path"]("Справочник.А", "Справочник.Изолированный", max_depth=1)
        assert res2["found"] is False and res2["_meta"]["budget_exceeded"] is False and "hint" not in res2
        assert "truncated_nodes" not in res2["_meta"]

    @pytest.mark.parametrize("exit_kind", ["node_budget", "no_index", "refs_table_lost"])
    def test_find_data_path_every_inconclusive_exit_has_hint(self, tmp_path, monkeypatch, exit_kind):
        import rlm_tools_bsl.bsl_helpers as bh

        root = _dp_tree(tmp_path / "cf")
        reader = _reader_for(root, monkeypatch)
        if exit_kind == "node_budget":
            monkeypatch.setattr(bh, "_DATA_PATH_NODE_BUDGET", 1)
        elif exit_kind == "no_index":
            reader = None
        else:
            monkeypatch.setattr(IndexReader, "find_metadata_refs_from", lambda self, *a, **k: None)
        bsl = _bsl_for(root, reader)
        # v1.46.0: прежде здесь стоял конец `Справочник.Нет` — несуществующий объект теперь отвергает шлюз.
        res = bsl["find_data_path"]("Справочник.А", "Справочник.Изолированный", max_depth=3)
        assert res["found"] is False and "hint" in res
        assert res["_meta"]["budget_exceeded"] is True or res["partial"] is True

    def test_find_data_path_conclusive_miss_has_no_hint(self, tmp_path, monkeypatch):
        root = _dp_tree(tmp_path / "cf")
        bsl = _bsl_for(root, _reader_for(root, monkeypatch))
        # v1.46.0: прежде здесь стоял конец `Справочник.Нет` — несуществующий объект теперь отвергает шлюз.
        res = bsl["find_data_path"]("Справочник.А", "Справочник.Изолированный", max_depth=3)
        assert res["found"] is False and res["_meta"]["budget_exceeded"] is False and res["partial"] is False
        assert "hint" not in res and "truncated_nodes" not in res["_meta"]

    def test_find_data_path_without_metadata_references_unchanged(self, tmp_path, monkeypatch):
        root = _dp_tree(tmp_path / "cf")
        bsl = _bsl_for(root, _reader_for(root, monkeypatch, build_metadata=False))
        res = bsl["find_data_path"]("Справочник.А", "Справочник.Г", max_depth=3)
        assert res == {
            "found": False,
            "from": "Catalog.А",
            "to": "Catalog.Г",
            "path": None,
            "depth": 0,
            "partial": False,
            "_meta": {"max_depth": 3, "nodes_expanded": 1, "node_budget": 400, "budget_exceeded": False, "kinds": None},
        }

    def test_reader_ref_object_filter(self, tmp_path, monkeypatch):
        root = _dp_tree(tmp_path / "cf")
        reader = _reader_for(root, monkeypatch)
        rows = reader.find_metadata_refs_from("А", source_category="Catalogs", ref_object="catalog.в")
        assert [r["ref_object"] for r in rows] == ["Catalog.В"]
        assert len(reader.find_metadata_refs_from("А", source_category="Catalogs")) == 2


# ═══════════════ Задача 4 (Д5) — вызывающие неэкспортной процедуры ═══════════════

_NE_FORM_A = "Documents/А/Forms/ФормаДокумента/Ext/Form/Module.bsl"
_NE_FORM_B = "Documents/Б/Forms/ФормаДокумента/Ext/Form/Module.bsl"
_NE_SPEC = {
    _NE_FORM_A: "\n".join(
        [
            "&НаСервере",
            "Процедура ПриСозданииНаСервере(Отказ, СтандартнаяОбработка)",
            "\tСобытияФормКлиент.ПриСозданииНаСервере(ЭтотОбъект);",
            "\tВспомогательнаяПроцедура();",
            "КонецПроцедуры",
            "",
            "&НаСервере",
            "Процедура ВспомогательнаяПроцедура()",
            "\tПриСозданииНаСервере();",
            "КонецПроцедуры",
            "",
        ]
    ),
    "CommonModules/СобытияФормКлиент/Ext/Module.bsl": "Процедура ПриСозданииНаСервере(Форма) Экспорт\nКонецПроцедуры\n",
    "CommonModules/СобытияФормКлиент.xml": _cm_descriptor("СобытияФормКлиент"),
    # МодульХ — переменная: вызов по имени, адресат статически не выводится (строка без ключа)
    _NE_FORM_B: "Процедура Что()\n\tМодульХ = Неопределено;\n\tМодульХ.ПриСозданииНаСервере(ЭтотОбъект);\nКонецПроцедуры\n",
}

_NE_LOCAL_MOD = "CommonModules/Л/Ext/Module.bsl"
_NE_LOCAL_SPEC = {
    _NE_LOCAL_MOD: "\n".join(
        [
            "Процедура Локальная()",
            "КонецПроцедуры",
            "",
            "Процедура Вызов() Экспорт",
            "\tЧужойМодуль.",
            "\t\tЛокальная();",
            "\tЧужойМодуль. Локальная();",
            "\tЛокальная();",
            "КонецПроцедуры",
            "",
        ]
    ),
    "CommonModules/Л.xml": _cm_descriptor("Л"),
}


def _callers(res) -> list[tuple[str, str]]:
    return [(c["file"], c["caller_name"]) for c in res["callers"]]


class TestNonExportCallers:
    @pytest.mark.parametrize("route", ["index", "fs"])
    def test_non_export_target_has_only_own_module_unqualified_callers(self, tmp_path, monkeypatch, route):
        root = _cf_doc_tree(tmp_path / "cf", _NE_SPEC)
        reader = _reader_for(root, monkeypatch) if route == "index" else None
        bsl = _bsl_for(root, reader)
        res = bsl["find_callers_context"]("ПриСозданииНаСервере", _NE_FORM_A)
        assert _callers(res) == [(_NE_FORM_A, "ВспомогательнаяПроцедура")]
        assert res["_meta"].get("non_export_target") is True  # на FS-маршруте тоже
        if route == "index":
            assert res["_meta"]["fallback_rows"] == 0 and res["_meta"]["total_callers"] == 1

    def test_non_export_extension_layer_rows_only_exact(self, tmp_path, monkeypatch):
        ext_form = f"../cfe/Расш1/{_NE_FORM_A}"
        cf = _ext_tree(
            tmp_path,
            _NE_SPEC,
            {
                "Расш1": {
                    # заимствованная форма: голый вызов процедуры расширяемого модуля — точное ребро
                    _NE_FORM_A: '&После("ПриСозданииНаСервере")\nПроцедура Расш1_ПослеСоздания(Отказ, СО)\n'
                    "\tПриСозданииНаСервере();\nКонецПроцедуры\n",
                    # Х — значение: строка слоя без ключа (по имени) у неэкспортной цели не годится
                    "CommonModules/Расш1_М/Ext/Module.bsl": "Процедура Чужое() Экспорт\n\tХ = 1;\n"
                    "\tХ.ПриСозданииНаСервере();\nКонецПроцедуры\n",
                }
            },
        )
        bsl = _ext_bsl(cf, _reader_for(cf, monkeypatch), ["Расш1"])
        res = bsl["find_callers_context"]("ПриСозданииНаСервере", _NE_FORM_A)
        assert _callers(res) == [(_NE_FORM_A, "ВспомогательнаяПроцедура"), (ext_form, "Расш1_ПослеСоздания")]
        assert all(c["edge_exact"] for c in res["callers"])
        assert res["_meta"]["non_export_target"] is True and res["_meta"]["fallback_rows"] == 0

    def test_non_export_target_inside_extension_only_exact(self, tmp_path, monkeypatch):
        ext_mod = "../cfe/Расш1/CommonModules/Расш1_Модуль/Ext/Module.bsl"
        cf = _ext_tree(
            tmp_path,
            _NE_SPEC,
            {
                "Расш1": {
                    "CommonModules/Расш1_Модуль/Ext/Module.bsl": "Процедура Локальная()\nКонецПроцедуры\n\n"
                    "Процедура Свой() Экспорт\n\tЛокальная();\nКонецПроцедуры\n",
                    "CommonModules/Расш1_Другой/Ext/Module.bsl": "Процедура Чужой() Экспорт\n\tПеременная = 1;\n"
                    "\tПеременная.Локальная();\nКонецПроцедуры\n",
                }
            },
        )
        bsl = _ext_bsl(cf, _reader_for(cf, monkeypatch), ["Расш1"])
        res = bsl["find_callers_context"]("Локальная", ext_mod)
        assert [c["file"] for c in res["callers"]] == [ext_mod] and res["_meta"]["non_export_target"] is True
        assert res["_meta"]["fallback_rows"] == 0

    def test_fs_route_skips_qualified_call_broken_by_newline_or_space(self, tmp_path):
        # FS-маршрут: «ЧужойМодуль.» ⏎ «Локальная()» и «ЧужойМодуль. Локальная()» — не вызовы неэкспортной Локальная
        root = _cf_doc_tree(tmp_path / "cf", _NE_LOCAL_SPEC)
        res = _bsl_for(root)["find_callers_context"]("Локальная", _NE_LOCAL_MOD)
        assert [(c["caller_name"], c["line"]) for c in res["callers"]] == [("Вызов", 8)]
        assert res["_meta"]["non_export_target"] is True

    def test_index_route_builder_edge_is_the_declared_boundary(self, tmp_path, monkeypatch):
        # граница Д5 (Р4) снята в v1.47.0: прежде здесь стояло `6 in lines and 8 in lines` — сборщик писал
        # «ЧужойМодуль.» ⏎ «Локальная()» голым вызовом с точным ребром в свой модуль; теперь вызов с переносом
        # или пробелом после точки — квалифицированный (`ЧужойМодуль.Локальная`), и точное ребро — только строка 8
        root = _cf_doc_tree(tmp_path / "cf", _NE_LOCAL_SPEC)
        res = _bsl_for(root, _reader_for(root, monkeypatch))["find_callers_context"]("Локальная", _NE_LOCAL_MOD)
        lines = {c["line"] for c in res["callers"] if c["edge_exact"]}
        assert lines == {8}

    def test_export_object_method_keeps_name_rows(self, tmp_path, monkeypatch):
        # граница: экспорт модуля объекта — строки «по имени» через переменные-объекты, как прежде
        spec = {
            "Documents/В/Ext/ObjectModule.bsl": "Процедура Заполнить() Экспорт\nКонецПроцедуры\n",
            "CommonModules/Ч/Ext/Module.bsl": "Процедура Ч() Экспорт\n\tДокОбъект = 1;\n\tДокОбъект.Заполнить();\nКонецПроцедуры\n",
            "CommonModules/Ч.xml": _cm_descriptor("Ч"),
        }
        root = _cf_doc_tree(tmp_path / "cf", spec)
        res = _bsl_for(root, _reader_for(root, monkeypatch))["find_callers_context"](
            "Заполнить", "Documents/В/Ext/ObjectModule.bsl"
        )
        assert _callers(res) == [("CommonModules/Ч/Ext/Module.bsl", "Ч")]
        assert res["callers"][0]["edge_exact"] is False and "non_export_target" not in res["_meta"]

    @pytest.mark.parametrize("route", ["index", "fs"])
    def test_export_form_method_routes_unchanged(self, tmp_path, monkeypatch, route):
        # граница: экспорт формы — прежние маршруты (индекс — по ключу и имени, FS — свой файл тремя шаблонами)
        spec = dict(_NE_SPEC)
        spec[_NE_FORM_A] = (
            "Процедура Экспортная() Экспорт\nКонецПроцедуры\n\nПроцедура Вызов()\n\tЭтаФорма.Экспортная();\n"
            "\tЭкспортная();\nКонецПроцедуры\n"
        )
        root = _cf_doc_tree(tmp_path / "cf", spec)
        reader = _reader_for(root, monkeypatch) if route == "index" else None
        res = _bsl_for(root, reader)["find_callers_context"]("Экспортная", _NE_FORM_A)
        assert "non_export_target" not in res["_meta"]
        assert sorted(c["line"] for c in res["callers"]) == [5, 6]  # ЭтаФорма.Экспортная() — как прежде

    def test_find_path_ignores_false_callers_of_non_export(self, tmp_path, monkeypatch):
        root = _cf_doc_tree(tmp_path / "cf", _NE_SPEC)
        bsl = _bsl_for(root, _reader_for(root, monkeypatch))
        res = bsl["find_path"]("Что", "ПриСозданииНаСервере", max_depth=2, from_hint=_NE_FORM_B, to_hint=_NE_FORM_A)
        assert res["found"] is False and res["_meta"]["budget_exceeded"] is False

    @pytest.mark.parametrize("variant", ["no_callee_key", "no_calls"])
    def test_non_export_rule_on_index_without_callee_key(self, tmp_path, monkeypatch, variant):
        # индекс без callee_key — именной маршрут с подсказкой-объектом; --no-calls — FS-маршрут: то же правило
        root = _cf_doc_tree(tmp_path / "cf", _NE_SPEC)
        reader = _reader_for(root, monkeypatch, build_calls=(variant != "no_calls"))
        if variant == "no_callee_key":
            reader._has_callee_key = False
        bsl = _bsl_for(root, reader)
        hint = "А" if variant == "no_callee_key" else _NE_FORM_A
        res = bsl["find_callers_context"]("ПриСозданииНаСервере", hint)
        assert _callers(res) == [(_NE_FORM_A, "ВспомогательнаяПроцедура")]
        assert res["_meta"]["non_export_target"] is True


# ═══════════════ Задача 5 (Д6) — find_callers: текст строки на индексе ═══════════════


class TestFindCallersText:
    @pytest.mark.parametrize("route", ["index", "fs"])
    def test_find_callers_text_filled(self, tmp_path, monkeypatch, route):
        root = _cf_doc_tree(tmp_path / "cf", _FR_SPEC)
        reader = _reader_for(root, monkeypatch) if route == "index" else None
        rows = _bsl_for(root, reader)["find_callers"]("ОбщаяЦель", _FP_TARGET)
        assert len(rows) == 4 and all(c["text"] for c in rows)
        lines = {rel: (root / rel).read_text(encoding="utf-8").splitlines() for rel in {c["file"] for c in rows}}
        assert all(c["text"] == lines[c["file"]][c["line"] - 1].rstrip() for c in rows)

    def test_find_callers_text_same_on_both_routes(self, tmp_path, monkeypatch):
        root = _cf_doc_tree(tmp_path / "cf", _FR_SPEC)
        idx = _bsl_for(root, _reader_for(root, monkeypatch))["find_callers"]("ОбщаяЦель", _FP_TARGET)
        fs = _bsl_for(root)["find_callers"]("ОбщаяЦель", _FP_TARGET)
        key = lambda r: (r["file"], r["line"])  # noqa: E731
        assert sorted(idx, key=key) == sorted(fs, key=key)


# ═══════════════ Задача 6 (Д7) — analyze_document_flow: документ, а не омоним ═══════════════

_CF_MD_NS = (
    'xmlns="http://v8.1c.ru/8.3/MDClasses" xmlns:cfg="http://v8.1c.ru/8.1/data/enterprise/current-config" '
    'xmlns:v8="http://v8.1c.ru/8.1/data/core" xmlns:xr="http://v8.1c.ru/8.3/xcf/readable" '
    'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'
)
_CF_ATTR = "<ChildObjects><Attribute><Properties><Name>Сумма</Name></Properties></Attribute></ChildObjects>"
_EDT_ATTR = "<attributes><name>Сумма</name></attributes>"


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


def _md_tree(
    root: Path, fmt: str, objects: list, subscriptions: dict | None = None, modules: dict | None = None
) -> Path:
    """Объекты метаданных обеих раскладок: objects — [(папка, вид, имя, CF-свойства, EDT-тело)];
    subscriptions — {имя: (источник, набор?, событие)}; modules — {(папка, имя, файл): текст}."""
    if fmt == "cf":
        _write(root / "Configuration.xml", CF_DESCRIPTOR)
        for folder, kind, name, props, _body in objects:
            _write(root / folder / f"{name}.xml", _cf_md(kind, name, props, _CF_ATTR))
            (root / folder / name).mkdir(parents=True, exist_ok=True)
        for name, (source, is_set, event) in (subscriptions or {}).items():
            tag = "v8:TypeSet" if is_set else "v8:Type"
            props = (
                f"<Source><{tag}>cfg:{source}</{tag}></Source><Event>{event}</Event>"
                f"<Handler>CommonModule.События.{name}</Handler>"
            )
            _write(root / "EventSubscriptions" / f"{name}.xml", _cf_md("EventSubscription", name, props))
        for (folder, name, file), text in (modules or {}).items():
            _write(root / folder / name / "Ext" / file, text)
    else:
        _write(root / "Configuration" / "Configuration.mdo", EDT_DESCRIPTOR)
        for folder, kind, name, _props, body in objects:
            _write(root / folder / name / f"{name}.mdo", _edt_md(kind, name, body + _EDT_ATTR))
        for name, (source, _is_set, event) in (subscriptions or {}).items():
            body = (
                f"<source><types>{source}</types></source><event>{event}</event>"
                f"<handler>CommonModule.События.{name}</handler>"
            )
            _write(root / "EventSubscriptions" / name / f"{name}.mdo", _edt_md("EventSubscription", name, body))
        for (folder, name, file), text in (modules or {}).items():
            _write(root / folder / name / file, text)
    return root


_AMORT_OBJECTS = [
    ("Documents", "Document", "АмортизацияОС", "<Posting>Allow</Posting>", "<posting>Allow</posting>"),
    ("AccumulationRegisters", "AccumulationRegister", "АмортизацияОС", "", ""),
    ("AccumulationRegisters", "AccumulationRegister", "ТолькоРегистр", "", ""),
]
_AMORT_SUBS = {
    "ПодпДокумент": ("DocumentObject.АмортизацияОС", False, "BeforeWrite"),
    "ПодпРегистр": ("AccumulationRegisterRecordSet.АмортизацияОС", False, "BeforeWrite"),
}
_AMORT_MODULES = {
    ("Documents", "АмортизацияОС", "ObjectModule.bsl"): "Процедура ОбработкаПроведения(Отказ, Режим)\nКонецПроцедуры\n",
    (
        "AccumulationRegisters",
        "АмортизацияОС",
        "RecordSetModule.bsl",
    ): "Процедура ПередЗаписью(Отказ, Замещение)\nКонецПроцедуры\n",
    (
        "AccumulationRegisters",
        "ТолькоРегистр",
        "RecordSetModule.bsl",
    ): "Процедура ПередЗаписью(Отказ, Замещение)\nКонецПроцедуры\n",
}


@pytest.fixture(params=["cf", "edt"])
def amort(request, tmp_path, monkeypatch):
    fmt = request.param
    root = _md_tree(tmp_path / fmt, fmt, _AMORT_OBJECTS, _AMORT_SUBS, _AMORT_MODULES)
    return root, _bsl_for(root, _reader_for(root, monkeypatch))


@pytest.fixture(params=["cf", "edt"])
def amort_register_first(request, tmp_path, monkeypatch):
    """Тот же омоним, но голое имя профиль разрешает в РЕГИСТР: у документа нет строк `object_attributes` (так же
    бывает после `index update`, переписавшего строки документов в конец таблицы)."""
    fmt = request.param
    root = _md_tree(tmp_path / fmt, fmt, _AMORT_OBJECTS, _AMORT_SUBS, _AMORT_MODULES)
    doc = root / ("Documents/АмортизацияОС.xml" if fmt == "cf" else "Documents/АмортизацияОС/АмортизацияОС.mdo")
    attr = _CF_ATTR if fmt == "cf" else _EDT_ATTR
    doc.write_text(doc.read_text(encoding="utf-8").replace(attr, ""), encoding="utf-8")
    return root, _bsl_for(root, _reader_for(root, monkeypatch))


def _sub_names(rows) -> list[str]:
    rows = rows["subscriptions"] if isinstance(rows, dict) else rows
    return sorted(r["name"] for r in rows)


class TestDocumentFlowHomonym:
    @pytest.mark.parametrize("arg", ["АмортизацияОС", "Документ.АмортизацияОС", "Documents/АмортизацияОС"])
    def test_flow_metadata_and_subscriptions_are_documents(self, amort, arg):
        _root, bsl = amort
        res = bsl["analyze_document_flow"](arg)
        assert res["metadata"]["object_type"] == "Document"
        assert _sub_names(res["event_subscriptions"]) == _sub_names(
            bsl["find_event_subscriptions"]("Документ.АмортизацияОС")
        )
        assert "ПодпРегистр" not in _sub_names(res["event_subscriptions"])

    def test_flow_without_document_keeps_previous_answer(self, amort):
        # голое имя только регистра — прежний разбор (метаданные регистра, подписки по имени)
        _root, bsl = amort
        res = bsl["analyze_document_flow"]("ТолькоРегистр")
        assert res["metadata"].get("object_type") == "AccumulationRegister"

    def test_flow_document_ref_without_document_is_not_foreign(self, amort):
        # ссылка 'Документ.X' — категория документа задана явно: при одноимённом регистре без документа
        # метаданные и подписки регистра больше не выдаются за документ (ревью вехи 4, буква плана Задачи 6)
        _root, bsl = amort
        # v1.46.0: прежде здесь стоял ответ с пустыми метаданными (не регистра). Документа `ТолькоРегистр` нет, и
        # ссылку на несуществующий объект шлюз существования (Д3) отвергает — регистр за документ по-прежнему не выдаётся.
        with pytest.raises(ValueError, match="^analyze_document_flow: объекта Document.ТолькоРегистр нет"):
            bsl["analyze_document_flow"]("Документ.ТолькоРегистр")

    def test_flow_hybrid_input_keeps_previous_parsing(self, amort):
        # 'Документ.Catalogs/X' — не ссылка: прежний разбор (производное имя `Catalogs/X`)
        _root, bsl = amort
        res = bsl["analyze_document_flow"]("Документ.Catalogs/АмортизацияОС")
        assert res["document"] == "Catalogs/АмортизацияОС"

    def test_profile_flow_section_is_typed_by_profile_category(self, amort):
        # решение владельца (перепись 6.3): секция flow — поток объекта категории профиля
        _root, bsl = amort
        doc = bsl["get_object_profile"]("Документ.АмортизацияОС", sections=["flow"], include_flow=True)
        assert doc["sections"]["flow"]["status"] == "ok"
        assert [i["handler"] for i in doc["sections"]["flow"]["items"]] == ["CommonModule.События.ПодпДокумент"]
        reg = bsl["get_object_profile"]("РегистрНакопления.АмортизацияОС", sections=["flow"], include_flow=True)
        flow = reg["sections"]["flow"]
        assert flow["status"] == "error" and flow["items"] == []
        assert flow["_meta"]["error"].startswith("ValueError") and "parse_object_xml" in flow["_meta"]["error"]

    @pytest.mark.parametrize("kw", [{"sections": ["flow"]}, {"include_flow": True}], ids=["sections", "include_flow"])
    def test_profile_flow_on_bare_name_follows_profile_category(self, amort_register_first, kw):
        # ревью Codex (K1): голое имя профиль разрешил в регистр — секция flow регистра (status='error' с
        # маршрутом), а не поток документа-омонима (до правки — одна чужая подписка документа, на HEAD — обе)
        _root, bsl = amort_register_first
        prof = bsl["get_object_profile"]("АмортизацияОС", **kw)
        assert prof["category"] == "AccumulationRegisters", prof  # предпосылка: регистр разрешается первым
        flow = prof["sections"]["flow"]
        assert flow["status"] == "error" and flow["items"] == [], flow
        assert "AccumulationRegisters" in flow["_meta"]["error"] and "parse_object_xml" in flow["_meta"]["error"]

    def test_profile_flow_on_bare_non_document_answers_like_its_ref(self, amort):
        # решение владельца — на любом корректном вводе: голое имя регистра без документа — как его ссылка
        _root, bsl = amort

        def view(arg):
            flow = bsl["get_object_profile"](arg, sections=["flow"])["sections"]["flow"]
            return flow["status"], flow["items"], flow["_meta"].get("error")

        assert view("ТолькоРегистр")[0] == "error"
        assert view("ТолькоРегистр") == view("РегистрНакопления.ТолькоРегистр")

    def test_profile_flow_on_bare_document_name_is_the_document(self, amort):
        # сохранение: голое имя, которое профиль разрешил в документ, — поток документа, как у ссылки
        _root, bsl = amort
        prof = bsl["get_object_profile"]("АмортизацияОС", sections=["flow"])
        assert prof["category"] == "Documents", prof
        flow = prof["sections"]["flow"]
        assert flow["status"] == "ok" and [i["handler"] for i in flow["items"]] == ["CommonModule.События.ПодпДокумент"]

    @pytest.mark.parametrize("fmt", ["cf", "edt"])
    @pytest.mark.parametrize(
        ("folder", "kind"),
        [
            ("Subsystems", "Subsystem"),
            ("Roles", "Role"),
            ("CommonTemplates", "CommonTemplate"),
            ("XDTOPackages", "XDTOPackage"),
        ],
    )
    def test_profile_flow_of_category_without_ref_head_is_refused(self, tmp_path, monkeypatch, fmt, folder, kind):
        # ревью Codex (K1, круг 2): у категории без головы ссылки секция flow звала композит голым именем — поток
        # документа-омонима в профиле подсистемы/роли; теперь — отказ с категорией и маршрутом, как у прочих
        # синоним — как в выгрузках: по нему профиль находит объект без реквизитов и модулей (общий макет, XDTO)
        cf_syn = "<Synonym><v8:item><v8:lang>ru</v8:lang><v8:content>Амортизация</v8:content></v8:item></Synonym>"
        edt_syn = "<synonym><key>ru</key><value>Амортизация</value></synonym>"
        objects = [_AMORT_OBJECTS[0], (folder, kind, "АмортизацияОС", cf_syn, edt_syn)]
        subs = {"ПодпДокумент": _AMORT_SUBS["ПодпДокумент"]}
        modules = {k: v for k, v in _AMORT_MODULES.items() if k[0] == "Documents"}
        root = _md_tree(tmp_path / fmt, fmt, objects, subs, modules)
        bsl = _bsl_for(root, _reader_for(root, monkeypatch))
        prof = bsl["get_object_profile"](f"{folder}/АмортизацияОС", sections=["flow"])
        assert prof["category"] == folder, prof
        flow = prof["sections"]["flow"]
        assert flow["status"] == "error" and flow["items"] == [], flow
        error = flow["_meta"]["error"]
        assert f"объект категории {folder}" in error and f"parse_object_xml('{folder}/АмортизацияОС')" in error, error


# ═══════════════ Задача 7 (Д8) — аргумент без имени объекта ═══════════════

NAMELESS = ["", "   ", "\t", "Документ.", "документ.   ", "Справочник.", "Documents/", "Справочники//"]
ONE_OBJECT_HELPERS = [
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
]
PATH_HELPERS = [
    "extract_procedures",
    "find_exports",
    "extract_queries",
    "code_metrics",
    "read_procedure",
    "get_module_outline",
]
OVERVIEW_HELPERS = ["find_event_subscriptions", "find_functional_options"]

_NL_OBJECTS = [
    ("Documents", "Document", "Док", "<Posting>Allow</Posting>", "<posting>Allow</posting>"),
    ("Catalogs", "Catalog", "Спр", "", ""),
    ("AccumulationRegisters", "AccumulationRegister", "Рег", "", ""),
    ("ExchangePlans", "ExchangePlan", "План", "", ""),
]
_NL_MODULES = {
    ("Documents", "Док", "ObjectModule.bsl"): "Процедура ОбработкаПроведения(Отказ, Режим)\nКонецПроцедуры\n",
    ("Catalogs", "Спр", "ObjectModule.bsl"): "Процедура П() Экспорт\nКонецПроцедуры\n",
    ("CommonModules", "Ext", "Module.bsl"): "Процедура ИзОбщегоМодуляExt() Экспорт\nКонецПроцедуры\n",
}
_NL_ENUM_CF = _cf_md(
    "Enum",
    "Перечисление1",
    "",
    "<ChildObjects><EnumValue><Properties><Name>Значение1</Name></Properties></EnumValue></ChildObjects>",
)


def _nameless_tree(root: Path) -> Path:
    """CF: документ, справочник, регистр, план обмена, перечисление и общий модуль `Ext` рядом с корневым
    каталогом `Ext` (модули уровня конфигурации раскладки CF)."""
    _md_tree(root, "cf", _NL_OBJECTS, {"ПодпДок": ("DocumentObject.Док", False, "BeforeWrite")}, _NL_MODULES)
    _write(root / "Enums" / "Перечисление1.xml", _NL_ENUM_CF)
    (root / "Enums" / "Перечисление1").mkdir(parents=True, exist_ok=True)
    _write(root / "Ext" / "SessionModule.bsl", "Процедура УстановкаПараметровСеанса(Параметры)\nКонецПроцедуры\n")
    return root


@pytest.fixture(scope="module")
def nameless_bsl(tmp_path_factory):
    root = _nameless_tree(tmp_path_factory.mktemp("nameless") / "cf")
    mp = pytest.MonkeyPatch()
    mp.setenv("RLM_INDEX_DIR", str(root.parent / "idx"))
    try:
        reader = IndexReader(IndexBuilder().build(str(root)))
        yield _bsl_for(root, reader)
        reader.close()
    finally:
        mp.undo()


def _call(bsl, helper, value):
    if helper == "read_procedure":
        return bsl[helper](value, "П")
    return bsl[helper](value)


def _call_fn(fn, helper, value):
    return fn(value, "П") if helper == "read_procedure" else fn(value)


def _outcome(fn, helper, value):
    try:
        return ("ok", json.dumps(_call_fn(fn, helper, value), ensure_ascii=False, sort_keys=True, default=str))
    except Exception as exc:  # noqa: BLE001
        return ("exc", type(exc).__name__, str(exc))


class TestNamelessObjectArg:
    @pytest.mark.parametrize("helper", ONE_OBJECT_HELPERS)
    @pytest.mark.parametrize("value", NAMELESS)
    def test_one_object_helpers_refuse_nameless_arg(self, nameless_bsl, helper, value):
        with pytest.raises(ValueError, match=f"^{helper}:"):
            _call(nameless_bsl, helper, value)

    def test_refusal_names_the_called_helper_not_the_child(self, nameless_bsl):
        with pytest.raises(ValueError, match="^analyze_document_flow:"):
            nameless_bsl["analyze_document_flow"]("Документ.")  # страж в теле дал бы analyze_object
        with pytest.raises(ValueError, match="^find_exports:"):
            nameless_bsl["find_exports"]("")  # HEAD: PermissionError; страж в теле дал бы extract_procedures

    @pytest.mark.parametrize("helper", ONE_OBJECT_HELPERS + PATH_HELPERS)
    def test_internal_calls_bypass_the_guard(self, nameless_bsl, helper):
        # внутренние вызовы идут к функции замыкания: она на том же вводе НЕ отказывает стражем
        inner = nameless_bsl[helper].__wrapped__
        try:
            _call_fn(inner, helper, "")
        except ValueError as exc:
            assert not str(exc).startswith(f"{helper}:"), exc
        except Exception:  # noqa: BLE001 — прежний ответ функции (не страж)
            pass

    @pytest.mark.parametrize("helper", PATH_HELPERS)
    @pytest.mark.parametrize("value", ["", "   ", "Documents/"])
    def test_path_helpers_refuse_directory(self, nameless_bsl, helper, value):
        with pytest.raises(ValueError, match=f"^{helper}:"):
            _call(nameless_bsl, helper, value)

    @pytest.mark.parametrize("helper", ["extract_procedures", "find_exports", "extract_queries", "code_metrics"])
    def test_path_helpers_keep_named_missing_file_error(self, nameless_bsl, helper):
        # 'Документ.' у путевых — прежний FileNotFoundError с маршрутом find_module
        with pytest.raises(FileNotFoundError):
            _call(nameless_bsl, helper, "Документ.")

    def test_object_level_path_is_not_a_directory_refusal(self, nameless_bsl):
        # путь самого объекта — имя объекта (v1.44.0), ответ прежний
        res = nameless_bsl["extract_procedures"]("Catalogs/Спр")
        assert [p["name"] for p in res] == ["П"]

    def test_module_named_like_a_root_directory_is_not_refused(self, nameless_bsl):
        # CF: CommonModules/Ext/Ext/Module.bsl и корневой каталог Ext (модули уровня конфигурации)
        for helper in ("extract_procedures", "find_exports", "get_module_outline"):
            fn = nameless_bsl[helper]
            assert _outcome(fn, helper, "Ext") == _outcome(fn.__wrapped__, helper, "Ext"), helper
            assert _outcome(fn, helper, "Ext")[0] == "ok", helper
        for helper in ("extract_queries", "code_metrics", "read_procedure"):  # читают путь буквально: каталог Ext
            with pytest.raises(ValueError, match=f"^{helper}:"):
                _call(nameless_bsl, helper, "Ext")

    def test_directory_refusal_covers_extension_roots(self, tmp_path, monkeypatch):
        cf = _ext_tree(
            tmp_path,
            {"CommonModules/М/Ext/Module.bsl": "Процедура П() Экспорт\nКонецПроцедуры\n"},
            {"Р": {"CommonModules/Р_М/Ext/Module.bsl": "Процедура Р() Экспорт\nКонецПроцедуры\n"}},
        )
        bsl = _ext_bsl(cf, _reader_for(cf, monkeypatch), ["Р"])
        with pytest.raises(ValueError, match="^extract_procedures:"):
            bsl["extract_procedures"]("../cfe/Р/CommonModules")
        with pytest.raises(PermissionError):
            bsl["extract_procedures"]("../../")  # вне всех корней — прежний ответ

    def test_overview_helpers_treat_blank_as_empty(self, nameless_bsl):
        # пробелы снимаются ДО разбора, поэтому совпадает и эхо аргумента (`object == ''`)
        bsl = nameless_bsl
        assert bsl["find_functional_options"]("   ", include_code=False) == bsl["find_functional_options"](
            "", include_code=False
        )
        assert bsl["find_event_subscriptions"]("\t") == bsl["find_event_subscriptions"]("")

    @pytest.mark.parametrize("helper", OVERVIEW_HELPERS)
    @pytest.mark.parametrize("value", ["Документ.", "Documents/", "справочник."])
    def test_overview_helpers_refuse_nameless_prefix(self, nameless_bsl, helper, value):
        with pytest.raises(ValueError, match=f"^{helper}:"):
            nameless_bsl[helper](value)

    @pytest.mark.parametrize("helper", ONE_OBJECT_HELPERS + PATH_HELPERS)
    @pytest.mark.parametrize("value", [None, 0])
    def test_non_string_keeps_previous_answer(self, nameless_bsl, helper, value):
        fn = nameless_bsl[helper]
        assert _outcome(fn, helper, value) == _outcome(fn.__wrapped__, helper, value)

    def test_list_overload_isolates_nameless_element(self, nameless_bsl):
        res = nameless_bsl["find_enum_values"](["", "Документ.", "Перечисление1"])
        assert "error" in res[""] and "error" in res["Документ."] and res["Перечисление1"]["values"]
        assert "find_enum_values:" in res[""]["error"]

    @pytest.mark.parametrize("value", ["   ", "Документ.", "Documents/", ""])
    def test_parse_form_keeps_its_own_message(self, nameless_bsl, value):
        with pytest.raises(ValueError, match="object_name is required"):
            nameless_bsl["parse_form"](value)

    def test_named_arguments_are_guarded_too(self, nameless_bsl):
        with pytest.raises(ValueError, match="^get_object_full_structure:"):
            nameless_bsl["get_object_full_structure"](name="Документ.")
        with pytest.raises(ValueError, match="^read_procedure:"):
            nameless_bsl["read_procedure"](path="", proc_name="П")

    def test_names_still_work(self, nameless_bsl):
        assert nameless_bsl["get_object_full_structure"]("Документ.Док")["object_name"] == "Док"
        assert nameless_bsl["find_enum_values"]("Перечисление1")["values"]


# ═══════════════ Задача 8 (Д9) — голое имя в find_references_to_object / find_code_usages ═══════════════


def _typed_md(kind: str, name: str, attr_types: list[str]) -> str:
    attrs = "".join(
        f"<Attribute><Properties><Name>Реквизит{i}</Name><Type><v8:Type>cfg:{t}</v8:Type></Type></Properties></Attribute>"
        for i, t in enumerate(attr_types)
    )
    return _cf_md(kind, name, "", f"<ChildObjects>{attrs}</ChildObjects>")


@pytest.fixture(scope="module")
def bare_refs(tmp_path_factory):
    root = tmp_path_factory.mktemp("bare") / "cf"
    _write(root / "Configuration.xml", CF_DESCRIPTOR)
    for folder, kind, name, types in (
        ("Catalogs", "Catalog", "Валюты", []),
        ("Documents", "Document", "Заказ", ["CatalogRef.Валюты"]),
        ("Catalogs", "Catalog", "Дубль", []),
        ("Documents", "Document", "Дубль", ["CatalogRef.Валюты"]),
    ):
        _write(root / folder / f"{name}.xml", _typed_md(kind, name, types))
        (root / folder / name).mkdir(parents=True, exist_ok=True)
    _write(
        root / "Documents" / "Заказ" / "Ext" / "ObjectModule.bsl",
        'Процедура П()\n\tВ = Справочники.Валюты.НайтиПоКоду("1");\n\tД = Справочники.Дубль.ПустаяСсылка();\nКонецПроцедуры\n',
    )
    mp = pytest.MonkeyPatch()
    mp.setenv("RLM_INDEX_DIR", str(root.parent / "idx"))
    try:
        reader = IndexReader(IndexBuilder().build(str(root)))
        yield _bsl_for(root, reader)
        reader.close()
    finally:
        mp.undo()


class TestBareNameReferences:
    @pytest.mark.parametrize(
        "helper, items", [("find_references_to_object", "references"), ("find_code_usages", "usages")]
    )
    def test_bare_name_resolves_single_category(self, bare_refs, helper, items):
        bare = bare_refs[helper]("Валюты")
        typed = bare_refs[helper]("Справочник.Валюты")
        assert bare[items] and bare[items] == typed[items] and bare["total"] == typed["total"]
        assert bare["_meta"]["resolved_ref"] == "Catalog.Валюты"
        assert "resolved_ref" not in typed["_meta"]

    @pytest.mark.parametrize("helper", ["find_references_to_object", "find_code_usages"])
    def test_bare_homonym_is_refused_with_categories(self, bare_refs, helper):
        with pytest.raises(ValueError, match=f"^{helper}:") as err:
            bare_refs[helper]("Дубль")
        assert "Catalogs" in str(err.value) and "Documents" in str(err.value)

    @pytest.mark.parametrize("helper", ["find_references_to_object", "find_code_usages"])
    def test_bare_missing_name_has_hint(self, bare_refs, helper):
        res = bare_refs[helper]("НетТакогоОбъекта")
        assert res["total"] == 0 and "hint" in res and "Справочник." in res["hint"]

    @pytest.mark.parametrize("helper", ["find_references_to_object", "find_code_usages"])
    @pytest.mark.parametrize("arg", ["Справочник.Валюты", "Catalogs/Валюты", "Catalog.Валюты"])
    def test_prefixed_and_path_forms_unchanged(self, bare_refs, helper, arg):
        res = bare_refs[helper](arg)
        assert "resolved_ref" not in res["_meta"] and "hint" not in res and res["total"] > 0


# ═══════════════ Задача 9 (Д10, Д11, Д12) — перехваты: привязка живых строк, агрегаты, ссылка на файл ═══════════════

_OV_DOC = "Documents/Док/Ext/ObjectModule.bsl"
_OV_MAIN = {
    _OV_DOC: "Процедура ПередЗаписью(Отказ)\nКонецПроцедуры\n\nПроцедура Метод() Экспорт\nКонецПроцедуры\n",
    "CommonModules/ОМ/Ext/Module.bsl": "Процедура Служебная()\nКонецПроцедуры\n\nПроцедура Сделать() Экспорт\nКонецПроцедуры\n",
    "CommonModules/ОМ.xml": _cm_descriptor("ОМ"),
    "Ext/SessionModule.bsl": "Процедура УстановкаПараметровСеанса(Параметры)\nКонецПроцедуры\n",
}
_OV_EXT = {
    _OV_DOC: '&Перед("ПередЗаписью")\nПроцедура Расш1_ПередЗаписью(Отказ)\nКонецПроцедуры\n\n'
    '&После("Метод")\nПроцедура Расш1_Метод()\nКонецПроцедуры\n',
    "CommonModules/ОМ/Ext/Module.bsl": '&Вместо("Сделать")\nПроцедура Расш1_Сделать()\n\tПродолжитьВызов();\nКонецПроцедуры\n',
    "Ext/SessionModule.bsl": '&После("УстановкаПараметровСеанса")\nПроцедура Расш1_Установка(Параметры)\nКонецПроцедуры\n',
}
_OV_EXT_FILE = f"../cfe/Расш1/{_OV_DOC}"
# Набор ключей индексной строки после _normalize_override_row: 15 ключей единого формата + id, extension_purpose.
KEYS_17 = {
    "object_name",
    "target_method",
    "annotation",
    "extension_name",
    "extension_method",
    "extension_root",
    "ext_module_path",
    "ext_line",
    "module_path",
    "module_type",
    "line",
    "source_path",
    "source_module_id",
    "target_method_line",
    "extension_file",
    "id",
    "extension_purpose",
}


@pytest.fixture
def ov_tree(tmp_path, monkeypatch):
    cf = _ext_tree(tmp_path, _OV_MAIN, {"Расш1": _OV_EXT})
    reader = _reader_for(cf, monkeypatch)
    return cf, _ext_bsl(cf, reader, ["Расш1"]), _ext_bsl(cf, None, ["Расш1"])


def _ov_key(r):
    return (r["ext_module_path"], r["target_method"])


class TestLiveOverrideBinding:
    def test_live_and_index_override_rows_agree_on_binding(self, ov_tree):
        cf, bsl_idx, bsl_live = ov_tree
        idx = bsl_idx["get_overrides"](limit=1000)["overrides"]
        live = bsl_live["find_ext_overrides"](str(cf.parent / "cfe" / "Расш1"))["overrides"]
        assert len(idx) == len(live) == 4
        bind = lambda rows: {_ov_key(r): (r["source_path"], r["target_method_line"]) for r in rows}  # noqa: E731
        assert bind(live) == bind(idx)
        assert all(r["source_path"] for r in live)
        assert all(KEYS_17 <= set(r) for r in live)  # id, extension_purpose — тоже
        assert {r["extension_purpose"] for r in live} == {r["extension_purpose"] for r in idx}

    def test_live_get_overrides_binds_page_rows(self, ov_tree):
        _cf, bsl_idx, bsl_live = ov_tree
        idx = {_ov_key(r): r["source_module_id"] for r in bsl_idx["get_overrides"](limit=1000)["overrides"]}
        live = bsl_live["get_overrides"](limit=1000)
        assert live["source"] == "live"
        assert {_ov_key(r): r["source_path"] for r in live["overrides"]} == {
            _ov_key(r): r["source_path"] for r in bsl_idx["get_overrides"](limit=1000)["overrides"]
        }
        assert all(r["source_module_id"] is None for r in live["overrides"])  # без ридера id модуля неизвестен
        assert all(v is not None for v in idx.values())

    def test_binding_does_not_change_slice_order(self, ov_tree):
        from rlm_tools_bsl.extension_detector import find_extension_overrides

        cf, _bsl_idx, bsl_live = ov_tree
        ext_root = str(cf.parent / "cfe" / "Расш1")
        raw = find_extension_overrides(ext_root, None)
        res = bsl_live["find_ext_overrides"](ext_root)["overrides"]
        assert [(r["module_path"], r["target_method"]) for r in raw[:200]] == [
            (r["module_path"], r["target_method"]) for r in res
        ]

    def test_extension_session_leaves_binding_empty(self, ov_tree):
        cf, _bsl_idx, _bsl_live = ov_tree
        ext_root = cf.parent / "cfe" / "Расш1"
        bsl = _bsl_for(
            ext_root,
            None,
            current_config_role="extension",
            current_config_name="Расш1",
            current_config_root=str(ext_root),
        )
        rows = bsl["get_overrides"](limit=1000)["overrides"]
        assert rows and all(r["source_path"] == "" for r in rows)


class TestConfigLevelOverrideAggregates:
    @pytest.mark.parametrize("route", ["index", "live"])
    def test_config_level_rows_counted_separately(self, ov_tree, route):
        _cf, bsl_idx, bsl_live = ov_tree
        res = (bsl_idx if route == "index" else bsl_live)["get_overrides"]()
        assert res["config_level_overrides"] == 1
        assert res["unique_object_methods"] == 3  # Док.ПередЗаписью, Док.Метод, ОМ.Сделать — без модуля сеанса
        assert res["total"] == 4


class TestOverrideSectionHeader:
    @pytest.mark.parametrize("route", ["index", "live"])
    def test_override_section_header_is_executable_path(self, ov_tree, route):
        _cf, bsl_idx, bsl_live = ov_tree
        bsl = bsl_idx if route == "index" else bsl_live
        body = bsl["read_procedure"](_OV_DOC, "ПередЗаписью", include_overrides=True)
        assert f"// Файл: {_OV_EXT_FILE}:" in body
        procs = {p["name"]: p for p in bsl["extract_procedures"](_OV_DOC)}
        if route == "index":
            assert procs["ПередЗаписью"]["overridden_by"][0]["extension_file"] == _OV_EXT_FILE


# ═══════════════ Задача 10 (Д13) — канонический путь модуля в get_module_outline ═══════════════


class TestOutlineCanonicalPath:
    def test_absolute_extension_module_path_routes_as_extension(self, ov_tree):
        cf, bsl_idx, _bsl_live = ov_tree
        abs_path = str((cf.parent / "cfe" / "Расш1" / _OV_DOC).resolve())
        res = bsl_idx["get_module_outline"](abs_path, no_live=True)
        assert res["_meta"]["fallback_reason"] == "extension_module_not_indexed"
        assert res["path"] == _OV_EXT_FILE and res["_meta"]["requested_path"] == abs_path

    @pytest.mark.parametrize(
        "form",
        [
            "absolute",
            # `\` — разделитель пути только на Windows; на POSIX это символ имени файла, и такой путь не адресует
            # модуль ни у одного хелпера песочницы (`_resolve_safe`/`_ext_resolve_safe` разрешают путь как есть)
            pytest.param("backslash", marks=pytest.mark.skipif(sys.platform != "win32", reason="Windows-only: \\")),
        ],
    )
    def test_non_canonical_main_module_path_uses_index(self, ov_tree, form):
        cf, bsl_idx, _bsl_live = ov_tree
        arg = str((cf / _OV_DOC).resolve()) if form == "absolute" else _OV_DOC.replace("/", "\\")
        res = bsl_idx["get_module_outline"](arg)
        assert res["_meta"]["index_used"] is True and res["path"] == _OV_DOC
        assert res["_meta"]["requested_path"] == arg

    def test_canonical_path_answer_unchanged(self, ov_tree):
        _cf, bsl_idx, _bsl_live = ov_tree
        res = bsl_idx["get_module_outline"](_OV_DOC)
        assert res["_meta"]["index_used"] is True and res["path"] == _OV_DOC
        assert "requested_path" not in res["_meta"]
        assert res == bsl_idx["get_module_outline"].__wrapped__(_OV_DOC)

    def test_path_outside_roots_keeps_permission_error(self, ov_tree, tmp_path):
        _cf, bsl_idx, _bsl_live = ov_tree
        outside = tmp_path.parent / "чужой.bsl"
        with pytest.raises(PermissionError):
            bsl_idx["get_module_outline"](str(outside))


# ═══════════════ Задача 11 (Д14, Д15, П1) — модули без процедур и флаги модулей объекта ═══════════════

_ZM_EMPTY = "Documents/Об/Forms/Ф/Ext/Form/Module.bsl"
_ZM_MAIN = {
    "Documents/Об.xml": _cf_md("Document", "Об", "<Posting>Allow</Posting>", _CF_ATTR),
    "Documents/Об/Ext/ObjectModule.bsl": "Процедура А() Экспорт\nКонецПроцедуры\n",
    "Documents/Об/Ext/ManagerModule.bsl": "Процедура Б() Экспорт\nКонецПроцедуры\n",
    _ZM_EMPTY: "// модуль формы без процедур\nПерем Х;\n",
}
_ZM_EXT_OM = "../cfe/Расш1/Documents/Об/Ext/ObjectModule.bsl"
_ZM_EXT = {
    "Documents/Об/Ext/ObjectModule.bsl": '&После("А")\nПроцедура Расш1_А()\nКонецПроцедуры\n',
    "Documents/Об/Ext/ManagerModule.bsl": "Процедура Расш1_Б() Экспорт\nКонецПроцедуры\n",
}


@pytest.fixture
def zm_tree(tmp_path, monkeypatch):
    cf = _ext_tree(tmp_path, _ZM_MAIN, {"Расш1": _ZM_EXT})
    return cf, _ext_bsl(cf, _reader_for(cf, monkeypatch), ["Расш1"])


def _fail_reading(monkeypatch, suffix: str) -> None:
    """Чтение одного файла расширения падает PermissionError (исходники не меняются)."""
    orig = Path.read_text

    def read_text(self, *args, **kwargs):
        if str(self).replace("\\", "/").endswith(suffix):
            raise PermissionError(f"нет доступа: {self}")
        return orig(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)


class TestZeroMethodModules:
    def test_module_without_methods_fresh_on_disk_is_answered_by_index(self, zm_tree):
        _cf, bsl = zm_tree
        o = bsl["get_module_outline"](_ZM_EMPTY)
        assert o["_meta"]["index_used"] is True and o["_meta"]["fallback_reason"] is None
        assert o["totals"]["methods"] == 0

    def test_module_without_methods_changed_on_disk_still_goes_live(self, zm_tree):
        cf, bsl = zm_tree
        p = cf / _ZM_EMPTY
        st = p.stat()
        os.utime(p, (st.st_atime, st.st_mtime + 10))
        o = bsl["get_module_outline"](_ZM_EMPTY)
        assert o["_meta"]["index_used"] is False and o["_meta"]["fallback_reason"] == "index_empty_for_module"

    def test_profile_does_not_report_stale_for_methodless_module(self, zm_tree):
        _cf, bsl = zm_tree
        sec = bsl["get_object_profile"]("Документ.Об", sections=["modules"])["sections"]["modules"]
        assert sec["status"] == "ok", sec
        assert "reason" not in sec["_meta"]


class TestObjectModulesCounters:
    def test_object_modules_counters(self, zm_tree):
        _cf, bsl = zm_tree
        m = bsl["get_object_modules"]("Документ.Об")["_meta"]
        assert (m["modules_index"], m["modules_live"], m["modules_skipped"]) == (3, 2, 0)

    def test_object_modules_counters_no_live(self, zm_tree):
        _cf, bsl = zm_tree
        m = bsl["get_object_modules"]("Документ.Об", no_live=True)["_meta"]
        assert (m["modules_index"], m["modules_live"], m["modules_skipped"]) == (3, 0, 2)

    def test_profile_reads_extension_modules_live(self, zm_tree):
        # П1: модули соседних расширений разбираются живьём — секция полна
        _cf, bsl = zm_tree
        sec = bsl["get_object_profile"]("Документ.Об", sections=["modules"])["sections"]["modules"]
        assert sec["status"] == "ok" and sec["summary"]["extension_modules"] == 2
        assert sec["summary"]["methods"] == 4  # А, Б + Расш1_А, Расш1_Б
        assert all("fallback_reason" not in i for i in sec["items"])

    def test_profile_extension_read_failure_is_not_ok(self, zm_tree, monkeypatch):
        _cf, bsl = zm_tree
        _fail_reading(monkeypatch, "cfe/Расш1/Documents/Об/Ext/ObjectModule.bsl")
        sec = bsl["get_object_profile"]("Документ.Об", sections=["modules"])["sections"]["modules"]
        assert sec["status"] == "skipped" and sec["_meta"]["reason"] == "module_read_failed"
        failed = [i for i in sec["items"] if i.get("fallback_reason")]
        assert [i["path"] for i in failed] == [_ZM_EXT_OM] and failed[0]["fallback_reason"].startswith("parse_failed:")
        main_methods = {i["path"]: i["methods"] for i in sec["items"] if not i["path"].startswith("..")}
        assert main_methods["Documents/Об/Ext/ObjectModule.bsl"] == 1  # данные основной — на месте
        m = bsl["get_object_modules"]("Документ.Об")["_meta"]
        assert m["modules_skipped"] == 1 and m["modules_live"] == 1


# ═══════════════ Задача 12 (Д16, П2) — точный объект первым в find_module / find_by_type ═══════════════

PROC_TEXT = "Процедура П() Экспорт\nКонецПроцедуры\n"
_RK_SPEC = {
    **{f"CommonModules/ЭДОСОбъектом{i}/Ext/Module.bsl": PROC_TEXT for i in (1, 2, 3)},
    **{f"DataProcessors/ЭДО/Commands/Команда{i}/Ext/CommandModule.bsl": PROC_TEXT for i in range(1, 7)},
    **{f"DataProcessors/ЭДО/Forms/Форма{i}/Ext/Form/Module.bsl": PROC_TEXT for i in range(1, 5)},
    "DataProcessors/ЭДО/Ext/ObjectModule.bsl": PROC_TEXT,
    "DataProcessors/ЭДО/Ext/ManagerModule.bsl": PROC_TEXT,
}


@pytest.fixture(params=["live", "index"])
def ranked_bsl(request, tmp_path, monkeypatch):
    root = _cf_doc_tree(tmp_path / "cf", _RK_SPEC)
    reader = _reader_for(root, monkeypatch) if request.param == "index" else None
    return _bsl_for(root, reader)


class TestFindModuleRanking:
    def test_find_module_puts_exact_object_first(self, ranked_bsl):
        rows = ranked_bsl["find_module"]("ЭДО", limit=3)
        # модуль объекта, модуль менеджера, затем остальные модули точного объекта — в порядке каталога; на живом
        # маршруте это порядок обхода ФС (NTFS — по алфавиту, ext4 и /mnt в WSL — иначе), поэтому третий — любой
        assert [(r["object_name"], r["module_type"]) for r in rows[:2]] == [
            ("ЭДО", "ObjectModule"),
            ("ЭДО", "ManagerModule"),
        ]
        assert rows[2]["object_name"] == "ЭДО" and rows[2]["module_type"] in {"CommandModule", "Module"}

    def test_find_by_type_puts_exact_object_first(self, ranked_bsl):
        rows = ranked_bsl["find_by_type"]("DataProcessors", "ЭДО", limit=2)
        assert [r["module_type"] for r in rows] == ["ObjectModule", "ManagerModule"]

    def test_inexact_rows_keep_catalog_order_after_exact(self, ranked_bsl):
        rows = ranked_bsl["find_module"]("ЭДО", limit=100)
        names = [r["object_name"] for r in rows]
        assert len(rows) == 15 and set(names[:12]) == {"ЭДО"}
        # неточные — в порядке прохода каталога (том же, что у выдачи по категории)
        catalog = [r["object_name"] for r in ranked_bsl["find_module"](category="CommonModules", limit=100)]
        assert names[12:] == catalog

    def test_without_name_order_unchanged(self, ranked_bsl):
        # без имени — прежний проход каталога с ранним выходом: страница — префикс полной выдачи
        full = ranked_bsl["find_module"](category="DataProcessors", limit=100)
        assert ranked_bsl["find_module"](category="DataProcessors", limit=3) == full[:3]
        full_t = ranked_bsl["find_by_type"]("DataProcessors", limit=100)
        assert ranked_bsl["find_by_type"]("DataProcessors", limit=3) == full_t[:3]
        assert [r["path"] for r in full] == [r["path"] for r in full_t]

    def test_count_only_unchanged(self, ranked_bsl):
        res = ranked_bsl["find_by_type"]("DataProcessors", "ЭДО", count_only=True)
        assert res["total"] == 12 and res["unique_objects"] == 1

    def test_internal_consumers_keep_previous_category(self, amort):
        # analyze_object на омониме (регистр + документ) — та же категория, что HEAD: внутренние потребители
        # ходят на прежнюю выдачу без ранжирования
        _root, bsl = amort
        # порядок каталога EDT бывает разным между процессами (сид хеширования, давнее поведение) — эталон
        # берётся из прохода каталога этой же сессии (выдача без имени — прежняя)
        catalog = bsl["find_module"](limit=1000)
        first_exact = next(r for r in catalog if r["object_name"] == "АмортизацияОС")
        assert bsl["analyze_object"]("АмортизацияОС")["category"] == first_exact["category"]
        assert bsl["find_module"]("АмортизацияОС")[0]["category"] == first_exact["category"]


# ═══════════════ Задача 13 (Д17, Р3) — имена групп и оговорка category=None ═══════════════

_GK_BODY = "#Область Маркерная\nПроцедура П() Экспорт\n\t// Маркер\nКонецПроцедуры\n#КонецОбласти\n"


@pytest.fixture(params=["cf", "edt"])
def group_tree(request, tmp_path, monkeypatch):
    fmt = request.param
    if fmt == "cf":
        main = {
            "Ext/SessionModule.bsl": _GK_BODY,
            "Sequences/П/Ext/RecordSetModule.bsl": _GK_BODY,
            "IntegrationServices/С/Ext/Module.bsl": _GK_BODY,
        }
        cf = _ext_tree(tmp_path, main, {"Р": {"Documents/Д/Ext/ObjectModule.bsl": _GK_BODY}})
    else:
        cf = _edt_doc_tree(
            tmp_path / "src" / "cf",
            {
                "Configuration/SessionModule.bsl": _GK_BODY,
                "Sequences/П/RecordSetModule.bsl": _GK_BODY,
                "IntegrationServices/С/Module.bsl": _GK_BODY,
            },
        )
        ext_root = tmp_path / "src" / "cfe" / "Р"
        _write(ext_root / "Configuration.xml", _ext_descriptor("Р"))
        _write(ext_root / "Documents" / "Д" / "Ext" / "ObjectModule.bsl", _GK_BODY)
    return _ext_bsl(cf, _reader_for(cf, monkeypatch), ["Р"])


class TestGroupKeys:
    def test_count_matches_group_keys_name_the_group(self, group_tree):
        res = group_tree["count_matches"]("Маркер")
        keys = {g["key"] for g in res["groups"]}
        assert keys == {"Configuration", "Sequences", "IntegrationServices", "Documents"}, res["groups"]
        assert not keys & {"..", "Ext", None}
        assert sum(g["count"] for g in res["groups"]) == res["occurrences"]

    def test_search_regions_category_groups_have_no_none_key(self, group_tree):
        res = group_tree["search_regions"]("", group_by="category")
        keys = {g["key"] for g in res["groups"]}
        assert None not in keys and "Configuration" in keys, res["groups"]
        assert sum(g["count"] for g in res["groups"]) == group_tree["search_regions"]("", count_only=True)["total"]

    def test_search_regions_category_groups_with_extensions(self, group_tree):
        res = group_tree["search_regions"]("Маркер", group_by="category")
        keys = {g["key"] for g in res["groups"]}
        assert None not in keys and {"Configuration", "Documents"} <= keys, res["groups"]
        assert (
            sum(g["count"] for g in res["groups"]) == group_tree["search_regions"]("Маркер", count_only=True)["total"]
        )

    def test_row_category_stays_none(self, group_tree):
        # строки — прежние (Р3: меняются только ключи групп): у модуля уровня конфигурации category=None
        rows = group_tree["search_regions"]("Маркер")
        session = [r for r in rows if r["module_path"].endswith("SessionModule.bsl")]
        assert session and session[0]["category"] is None and session[0]["object_name"] is None, rows
        mods = group_tree["find_module"]("SessionModule")
        assert mods and mods[0]["category"] is None


# ═══════════════ Задача 14 (Д18, Д19) — modules_scanned одного смысла, короткая подсказка ═══════════════

_MS_MAIN_OM = "Documents/Д/Ext/ObjectModule.bsl"
_MS_EXT_OM = "../cfe/Р/Documents/Д/Ext/ObjectModule.bsl"


def _doc_descriptor(name: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">\n'
        f'  <Document uuid="00000000-0000-0000-0000-0000000000d1"><Properties><Name>{name}</Name></Properties>'
        "</Document>\n"
        "</MetaDataObject>\n"
    )


def _ms_tree(root: Path) -> Path:
    return _ext_tree(
        root,
        {
            "Documents/Д.xml": _doc_descriptor("Д"),
            _MS_MAIN_OM: (
                "Процедура ОбработкаПроведения(Отказ, Режим)\n\tДвижения.РегОсн.Записывать = Истина;\nКонецПроцедуры\n"
            ),
        },
        {
            "Р": {
                "Documents/Д/Ext/ObjectModule.bsl": (
                    "Процедура Р_Доп()\n\tДвижения.РегРасш.Записывать = Истина;\nКонецПроцедуры\n"
                ),
            }
        },
    )


class TestModulesScanned:
    def test_modules_scanned_names_main_object_module_on_index(self, tmp_path, monkeypatch):
        cf = _ms_tree(tmp_path)
        res = _ext_bsl(cf, _reader_for(cf, monkeypatch), ["Р"])["find_register_movements"]("Д")
        assert {r["name"] for r in res["code_registers"]} == {"РегОсн", "РегРасш"}, res
        assert res["modules_scanned"][0] == _MS_MAIN_OM, res["modules_scanned"]
        assert res["modules_scanned"] == [_MS_MAIN_OM, _MS_EXT_OM], res["modules_scanned"]

    def test_modules_scanned_same_on_both_routes(self, tmp_path, monkeypatch):
        cf = _ms_tree(tmp_path)
        on_index = _ext_bsl(cf, _reader_for(cf, monkeypatch), ["Р"])["find_register_movements"]("Д")
        live = _ext_bsl(cf, None, ["Р"])["find_register_movements"]("Д")
        assert on_index["modules_scanned"] == live["modules_scanned"] == [_MS_MAIN_OM, _MS_EXT_OM]


# Обработчик без прямых Движения.X: шесть делегатов общих модулей и шесть вызовов без точки.
_PH_MODULES = [f"Учет{i}" for i in range(1, 7)]
_PH_GLOBALS = [f"ГлобПровести{i}" for i in range(1, 7)]
_PH_HANDLER = (
    "Процедура ОбработкаПроведения(Отказ, Режим)\n"
    + "".join(f"\t{m}.Отразить{i}(ЭтотОбъект, Отказ);\n" for i, m in enumerate(_PH_MODULES, 1))
    + "".join(f"\t{g}(Отказ);\n" for g in _PH_GLOBALS)
    + "КонецПроцедуры\n"
)
_PH_SPEC = {
    "Documents/Д.xml": _doc_descriptor("Д"),
    _MS_MAIN_OM: _PH_HANDLER,
    # служебный документ: таблица движений индекса непуста (иначе индексный маршрут не отвечает)
    "Documents/С.xml": _doc_descriptor("С"),
    "Documents/С/Ext/ObjectModule.bsl": (
        "Процедура ОбработкаПроведения(Отказ, Режим)\n\tДвижения.РегС.Записывать = Истина;\nКонецПроцедуры\n"
    ),
    **{f"CommonModules/{m}.xml": _cm_descriptor(m) for m in _PH_MODULES},
    **{
        f"CommonModules/{m}/Ext/Module.bsl": (
            f"Процедура Отразить{i}(Объект, Отказ) Экспорт\n\t// ТЕЛО {m}\nКонецПроцедуры\n"
        )
        for i, m in enumerate(_PH_MODULES, 1)
    },
}


@pytest.fixture
def posting_hint_bsl(tmp_path, monkeypatch):
    cf = _cf_doc_tree(tmp_path / "cf", _PH_SPEC)
    return _bsl_for(cf, _reader_for(cf, monkeypatch))


class TestPostingHintSize:
    def test_posting_hint_explains_each_delegate_kind_once(self, posting_hint_bsl):
        res = posting_hint_bsl["find_register_movements"]("Д")
        hint = res["hint"]
        assert [d["kind"] for d in res["_meta"]["delegates"]] == ["common_module"] * 6, res["_meta"]
        assert hint.count("НЕ ПОДТВЕРЖДАЙ результат проверкой category") == 1, hint
        assert hint.count("category-blind") == 1, hint
        assert hint.count("ГЛОБАЛЬНОГО общего модуля") == 1, hint
        assert hint.count("проверки — как у первого делегата этого вида") == 5, hint
        assert hint.count("проверки — как у первого вызова без точки") == 5, hint
        assert hint.count("find_definition(") >= 12  # шаги каждого делегата и вызова на месте
        for i, m in enumerate(_PH_MODULES, 1):
            assert f"find_definition('Отразить{i}', 'ОбщийМодуль.{m}')" in hint, (m, hint)
        for g in _PH_GLOBALS:
            assert f"find_definition({g!r})" in hint, (g, hint)

    def test_posting_hint_compact_steps_execute(self, posting_hint_bsl):
        """Шаги каждого делегата — и первого, и сжатых следующих — исполнимы и ведут к ЕГО телу."""
        import re

        hint = posting_hint_bsl["find_register_movements"]("Д")["hint"]
        steps = re.findall(r"\((\d+)\)\s*(.+?)\s*->", hint)
        labels = [n for n, _ in steps]
        assert labels == [str(i) for i in range(1, len(labels) + 1)], labels  # нумерация подряд
        ns = dict(posting_hint_bsl)
        bodies = []
        for _n, src in steps:
            if src.startswith(("d = ", "body = ")):
                exec(compile(src, "<hint>", "exec"), ns)  # noqa: S102
            elif src.startswith("read_procedure(d["):
                bodies.append(eval(compile(src, "<hint>", "eval"), ns))  # noqa: S307
            else:
                compile(src, "<hint>", "eval")
        assert [b and b.split("// ТЕЛО ")[1].split()[0] for b in bodies] == _PH_MODULES, bodies

    def test_posting_hint_shorter_than_before(self, posting_hint_bsl):
        hint = posting_hint_bsl["find_register_movements"]("Д")["hint"]
        # одна и та же проза пяти следующих делегатов и пяти вызовов без точки больше не повторяется
        assert hint.count("Guard обязателен") == 1, hint
        assert hint.count("в модуле объекта их нет") == 1, hint

    def test_posting_hint_keeps_per_delegate_facts(self, tmp_path, monkeypatch):
        """Ловушка однофамильца — факт ДЕЛЕГАТА, а не пояснение вида: она остаётся у каждого."""
        # v1.46.0: окружающий git отсечён — временный каталог внутри рабочего дерева git уводил подсказку на маршрут
        # `safe_grep`, и проверка маршрута `find_definition` падала без отношения к коду.
        monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
        handler = (
            "Процедура ОбработкаПроведения(Отказ, Режим)\n"
            + "".join(
                f"\t{m} = Новый Структура;\n\t{m}.Отразить{i}(Отказ);\n" for i, m in enumerate(_PH_MODULES[:3], 1)
            )
            + "КонецПроцедуры\n"
        )
        cf = _cf_doc_tree(tmp_path / "cf", {**_PH_SPEC, _MS_MAIN_OM: handler})
        res = _bsl_for(cf, _reader_for(cf, monkeypatch))["find_register_movements"]("Д")
        hint = res["hint"]
        assert [d["kind"] for d in res["_meta"]["delegates"]] == ["variable"] * 3, res["_meta"]
        assert hint.count("ЛОВУШКА") == 3, hint
        assert hint.count("Тип получателя по имени не восстановить") == 1, hint
        assert hint.count("проверки — как у первого делегата этого вида") == 2, hint
        for i in range(1, 4):
            assert f"find_definition('Отразить{i}') -> объявление" in hint, hint


# ═══════════════ Задача 15 (Д20) — маршрут печати при пустом ответе find_print_forms ═══════════════

_PF_NS = (
    'xmlns="http://v8.1c.ru/8.3/MDClasses" xmlns:xr="http://v8.1c.ru/8.3/xcf/readable" '
    'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'
)
_PF_MDO_NS = 'xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass"'
_PF_HANDLER = "&НаКлиенте\nПроцедура ОбработкаКоманды(ПараметрКоманды, ПараметрыВыполненияКоманды)\nКонецПроцедуры\n"
_PF_PRINT_MANAGER = (
    "Процедура ДобавитьКомандыПечати(КомандыПечати) Экспорт\n"
    "\tКомандаПечати = КомандыПечати.Добавить();\n"
    '\tКомандаПечати.Идентификатор = "Накладная";\n'
    "\tКомандаПечати.Представление = НСтр(\"ru = 'Накладная'\");\n"
    "КонецПроцедуры\n"
)


def _pf_cf_journal(name: str, docs: list[str], commands: list[str]) -> str:
    items = "".join(f'<xr:Item xsi:type="xr:MDObjectRef">Document.{d}</xr:Item>' for d in docs)
    cmds = "".join(f'<Command uuid="c-{c}"><Properties><Name>{c}</Name></Properties></Command>' for c in commands)
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>\n<MetaDataObject {_PF_NS}>\n'
        f'  <DocumentJournal uuid="j-{name}"><Properties><Name>{name}</Name>'
        f"<RegisteredDocuments>{items}</RegisteredDocuments></Properties>\n"
        f'  <ChildObjects><Column uuid="k1"><Properties><Name>Тема</Name></Properties></Column>{cmds}</ChildObjects>\n'
        "  </DocumentJournal>\n</MetaDataObject>\n"
    )


def _pf_cf_doc(name: str, commands: list[str]) -> str:
    cmds = "".join(f'<Command uuid="c-{c}"><Properties><Name>{c}</Name></Properties></Command>' for c in commands)
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>\n<MetaDataObject {_PF_NS}>\n'
        f'  <Document uuid="d-{name}"><Properties><Name>{name}</Name></Properties>'
        f"<ChildObjects>{cmds}</ChildObjects></Document>\n</MetaDataObject>\n"
    )


def _pf_mdo_journal(name: str, docs: list[str], commands: list[str]) -> str:
    regs = "".join(f"<registeredDocuments>Document.{d}</registeredDocuments>" for d in docs)
    cmds = "".join(f'<commands uuid="c-{c}"><name>{c}</name></commands>' for c in commands)
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>\n<mdclass:DocumentJournal {_PF_MDO_NS} uuid="j-{name}">'
        f"<name>{name}</name>{regs}<columns><name>Тема</name></columns>{cmds}</mdclass:DocumentJournal>\n"
    )


def _pf_mdo_doc(name: str, commands: list[str]) -> str:
    cmds = "".join(f'<commands uuid="c-{c}"><name>{c}</name></commands>' for c in commands)
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>\n<mdclass:Document {_PF_MDO_NS} uuid="d-{name}">'
        f"<name>{name}</name>{cmds}</mdclass:Document>\n"
    )


_EDT_EXT_DESCRIPTOR = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<mdclass:Configuration xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
    'xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass" '
    'xmlns:mdclassExtension="http://g5.1c.ru/v8/dt/metadata/mdclass/extension" uuid="00000000-0000-0000-0000-000000000004">'
    "<name>ЕДТ</name><objectBelonging>Adopted</objectBelonging>"
    '<extension xsi:type="mdclassExtension:ConfigurationExtension"><defaultRunMode>Checked</defaultRunMode></extension>'
    "<namePrefix>ед_</namePrefix><configurationExtensionPurpose>Customization</configurationExtensionPurpose>"
    "</mdclass:Configuration>\n"
)


def _pf_tree(root: Path, fmt: str) -> tuple[Path, dict[str, str]]:
    ext = "Ext/" if fmt == "cf" else ""
    journal_cmd = "DocumentJournals/ЭлектроннаяПочта/Commands/{}/" + ext + "CommandModule.bsl"
    paths = {
        "journal": journal_cmd.format("ПечатьПисьма"),
        "journal2": journal_cmd.format("ОткрытьПечатнуюФорму"),
        "object": f"Documents/ВходящееПисьмо/Commands/Ответить/{ext}CommandModule.bsl",
    }
    if fmt == "cf":
        spec = {
            "Documents/ВходящееПисьмо.xml": _pf_cf_doc("ВходящееПисьмо", ["Ответить"]),
            "Documents/СПечатью.xml": _pf_cf_doc("СПечатью", []),
            "DocumentJournals/ЭлектроннаяПочта.xml": _pf_cf_journal(
                "ЭлектроннаяПочта", ["Другой", "ВходящееПисьмо"], ["ПечатьПисьма", "ОткрытьПечатнуюФорму"]
            ),
            "DocumentJournals/Чужой.xml": _pf_cf_journal("Чужой", ["СПечатью"], ["ЧужаяПечать"]),
        }
        tree = _cf_doc_tree
    else:
        spec = {
            "Documents/ВходящееПисьмо/ВходящееПисьмо.mdo": _pf_mdo_doc("ВходящееПисьмо", ["Ответить"]),
            "Documents/СПечатью/СПечатью.mdo": _pf_mdo_doc("СПечатью", []),
            "DocumentJournals/ЭлектроннаяПочта/ЭлектроннаяПочта.mdo": _pf_mdo_journal(
                "ЭлектроннаяПочта", ["Другой", "ВходящееПисьмо"], ["ПечатьПисьма", "ОткрытьПечатнуюФорму"]
            ),
            "DocumentJournals/Чужой/Чужой.mdo": _pf_mdo_journal("Чужой", ["СПечатью"], ["ЧужаяПечать"]),
        }
        tree = _edt_doc_tree
    spec.update(
        {
            f"Documents/ВходящееПисьмо/{ext}ObjectModule.bsl": "Процедура П()\nКонецПроцедуры\n",
            f"Documents/ВходящееПисьмо/{ext}ManagerModule.bsl": "Процедура Прочее() Экспорт\nКонецПроцедуры\n",
            f"Documents/СПечатью/{ext}ManagerModule.bsl": _PF_PRINT_MANAGER,
            paths["journal"]: _PF_HANDLER,
            paths["journal2"]: _PF_HANDLER,
            paths["object"]: _PF_HANDLER,
            f"DocumentJournals/Чужой/Commands/ЧужаяПечать/{ext}CommandModule.bsl": _PF_HANDLER,
        }
    )
    return tree(root / fmt, spec), paths


class TestParseDocumentJournal:
    def test_cf_journal(self):
        from rlm_tools_bsl.bsl_xml_parsers import parse_document_journal

        parsed = parse_document_journal(_pf_cf_journal("Ж", ["А", "Б"], ["К1", "К2"]))
        assert parsed == {"registered": ["Document.А", "Document.Б"], "commands": ["К1", "К2"]}

    def test_edt_journal(self):
        from rlm_tools_bsl.bsl_xml_parsers import parse_document_journal

        parsed = parse_document_journal(_pf_mdo_journal("Ж", ["А"], ["К1"]))
        assert parsed == {"registered": ["Document.А"], "commands": ["К1"]}

    def test_not_a_journal(self):
        from rlm_tools_bsl.bsl_xml_parsers import parse_document_journal

        assert parse_document_journal(_pf_cf_doc("А", ["К"])) is None
        assert parse_document_journal(_pf_mdo_doc("А", ["К"])) is None
        assert parse_document_journal("<оборванный") is None
        assert parse_document_journal("") is None


class TestPrintFormsRoute:
    @pytest.mark.parametrize("fmt", ["cf", "edt"])
    def test_empty_answer_routes_to_journal_command(self, tmp_path, fmt):
        root, paths = _pf_tree(tmp_path, fmt)
        bsl = _bsl_for(root)
        res = bsl["find_print_forms"]("Документ.ВходящееПисьмо")
        assert res["print_forms"] == [], res
        cands = res["_meta"]["command_candidates"]
        assert cands[0] == {
            "kind": "journal_command",
            "owner": "DocumentJournal.ЭлектроннаяПочта",
            "command": "ПечатьПисьма",
            "module_path": paths["journal"],
        }, cands
        assert cands[1]["command"] == "ОткрытьПечатнуюФорму" and cands[1]["module_path"] == paths["journal2"]
        own = {"kind": "object_command", "owner": "Document.ВходящееПисьмо", "command": "Ответить"}
        assert {**own, "module_path": paths["object"]} in cands, cands
        assert all(c["owner"] != "DocumentJournal.Чужой" for c in cands), cands  # журнал другого документа
        assert "ОбработкаКоманды" in bsl["read_procedure"](cands[0]["module_path"], "ОбработкаКоманды")
        hint = res["hint"]
        # в модуле менеджера только `Прочее()` — модуль прочитан, «процедуры нет» о нем истинно
        manager = f"Documents/ВходящееПисьмо/{'Ext/' if fmt == 'cf' else ''}ManagerModule.bsl"
        assert hint.startswith(f"В модуле менеджера ({manager}) процедуры ДобавитьКомандыПечати нет."), hint
        assert "ЭлектроннаяПочта" in hint and paths["journal"] in hint, hint
        assert "find_references_to_object('Документ.ВходящееПисьмо', kinds=['command_parameter_type'])" in hint

    def test_unexpanded_delegate_is_named_first(self, tmp_path):
        """ДобавитьКомандыПечати есть и отдаёт список делегату, которого нет в дереве: подсказка не утверждает
        «команд нет», а первым называет нераскрытого делегата."""
        root = _cf_doc_tree(
            tmp_path / "cf",
            {
                "Documents/Д.xml": _doc_descriptor("Д"),
                "Documents/Д/Ext/ManagerModule.bsl": (
                    "Процедура ДобавитьКомандыПечати(КомандыПечати) Экспорт\n"
                    "\tПечатьЛокализация.ДобавитьКомандыПечати(КомандыПечати);\n"
                    "КонецПроцедуры\n"
                ),
            },
        )
        res = _bsl_for(root)["find_print_forms"]("Документ.Д")
        assert res["print_forms"] == [] and res["_meta"]["delegates_expanded"][0]["status"] == "module_not_found"
        hint = res["hint"]
        for absent in ("процедуры ДобавитьКомандыПечати нет", "в каталоге модулей сессии нет", "команд не нашел"):
            assert absent not in hint, hint
        assert hint.startswith("ДобавитьКомандыПечати передает список делегатам"), hint
        assert "_meta.delegates_expanded" in hint, hint

    @pytest.mark.parametrize("fmt", ["cf", "edt"])
    @pytest.mark.parametrize(
        "registration",
        [
            "\tКомандаПечати = КомандыПечати.Добавить();\n\tКомандаПечати.Идентификатор = ИмяМакета;\n",
            '\tУправлениеПечатью.ДобавитьКомандуПечати(КомандыПечати, ИмяМакета, "Печать");\n',
        ],
        ids=["block", "helper"],
    )
    def test_computed_identifier_is_not_reported_absent(self, tmp_path, fmt, registration):
        """Ревью Codex (K2): идентификатор не литералом разборщик пропускает — пустой разбор подсказка не выдает
        за «команд нет», а ведет к телу ДобавитьКомандыПечати; совет исполним."""
        ext = "Ext/" if fmt == "cf" else ""
        manager = f"Documents/Д/{ext}ManagerModule.bsl"
        body = (
            "Процедура ДобавитьКомандыПечати(КомандыПечати) Экспорт\n"
            '\tИмяМакета = "ПечатнаяФорма";\n' + registration + "КонецПроцедуры\n"
        )
        if fmt == "cf":
            root = _cf_doc_tree(tmp_path / fmt, {"Documents/Д.xml": _pf_cf_doc("Д", []), manager: body})
        else:
            root = _edt_doc_tree(tmp_path / fmt, {"Documents/Д/Д.mdo": _pf_mdo_doc("Д", []), manager: body})
        bsl = _bsl_for(root)
        res = bsl["find_print_forms"]("Документ.Д")
        assert res["print_forms"] == [], res
        hint = res["hint"]
        for absent in ("процедуры ДобавитьКомандыПечати нет", "в каталоге модулей сессии нет", "Команд печати"):
            assert absent not in hint, hint
        assert f"read_procedure({manager!r}, 'ДобавитьКомандыПечати')" in hint, hint
        assert registration.strip() in bsl["read_procedure"](manager, "ДобавитьКомандыПечати")

    @pytest.mark.parametrize("fmt", ["cf", "edt"])
    def test_manager_module_added_after_index_build_is_not_called_absent(self, tmp_path, monkeypatch, fmt):
        """Ревью Codex (K2, круг 2): модуль менеджера добавлен после сборки индекса, до сессии — каталог модулей
        сессии (индекс на старте) его не видит. Подсказка говорит о каталоге, а не «процедуры нет»: отсутствие
        утверждается только о прочитанном модуле."""
        ext = "Ext/" if fmt == "cf" else ""
        if fmt == "cf":
            root = _cf_doc_tree(tmp_path / fmt, {"Documents/Печать.xml": _pf_cf_doc("Печать", [])})
        else:
            root = _edt_doc_tree(tmp_path / fmt, {"Documents/Печать/Печать.mdo": _pf_mdo_doc("Печать", [])})
        _write(root / f"Documents/Печать/{ext}ObjectModule.bsl", "Процедура П()\nКонецПроцедуры\n")
        reader = _reader_for(root, monkeypatch)
        manager = f"Documents/Печать/{ext}ManagerModule.bsl"
        _write(root / manager, _PF_PRINT_MANAGER)
        bsl = _bsl_for(root, reader)
        hint = bsl["find_print_forms"]("Документ.Печать")["hint"]
        assert hint.startswith("Модуля менеджера в каталоге модулей сессии нет."), hint
        assert "процедуры ДобавитьКомандыПечати нет" not in hint and "Процедуры ДобавитьКомандыПечати" not in hint, hint
        assert "Накладная" in bsl["read_procedure"](manager, "ДобавитьКомандыПечати")  # тело на диске есть

    def test_route_on_index(self, tmp_path, monkeypatch):
        root, paths = _pf_tree(tmp_path, "cf")
        res = _bsl_for(root, _reader_for(root, monkeypatch))["find_print_forms"]("ВходящееПисьмо")
        assert res["_meta"]["command_candidates"][0]["module_path"] == paths["journal"], res

    @pytest.mark.parametrize("ext_fmt", ["cf", "edt"])
    @pytest.mark.parametrize("with_index", [False, True], ids=["live", "index"])
    def test_extension_commands_are_candidates(self, tmp_path, monkeypatch, ext_fmt, with_index):
        """Команды объекта — из всех его описателей в сессии (решение владельца 2026-10-17): расширение добавило
        печатную команду заимствованному документу, у собственного документа расширения команда описана только
        там; путь модуля — рядом с описателем в расширении, совет из hint исполним."""
        main = {
            "Documents/Д.xml": _pf_cf_doc("Д", []),
            "Documents/Д/Ext/ManagerModule.bsl": "Процедура Прочее() Экспорт\nКонецПроцедуры\n",
        }
        if ext_fmt == "cf":
            ext, own_doc, ext_cmd = "Расш1", "Расш1_Док", "Расш1_Печать"
            adopted = _pf_cf_doc("Д", [ext_cmd]).replace(
                "<Name>Д</Name>", "<ObjectBelonging>Adopted</ObjectBelonging><Name>Д</Name>", 1
            )
            ext_files = {
                "Documents/Д.xml": adopted,
                f"Documents/Д/Commands/{ext_cmd}/Ext/CommandModule.bsl": _PF_HANDLER,
                f"Documents/{own_doc}.xml": _pf_cf_doc(own_doc, ["Печать"]),
                f"Documents/{own_doc}/Commands/Печать/Ext/CommandModule.bsl": _PF_HANDLER,
            }
            cf = _ext_tree(tmp_path, main, {ext: ext_files})
        else:
            ext, own_doc, ext_cmd = "ЕДТ", "ед_Док", "ед_Печать"
            cf = _cf_doc_tree(tmp_path / "src" / "cf", main)
            adopted = _pf_mdo_doc("Д", [ext_cmd]).replace(
                "<name>Д</name>", "<name>Д</name><objectBelonging>Adopted</objectBelonging>", 1
            )
            for rel, text in {
                "Configuration/Configuration.mdo": _EDT_EXT_DESCRIPTOR,
                "Documents/Д/Д.mdo": adopted,
                f"Documents/Д/Commands/{ext_cmd}/CommandModule.bsl": _PF_HANDLER,
                f"Documents/{own_doc}/{own_doc}.mdo": _pf_mdo_doc(own_doc, ["Печать"]),
                f"Documents/{own_doc}/Commands/Печать/CommandModule.bsl": _PF_HANDLER,
            }.items():
                _write(tmp_path / "src" / "cfe" / ext / rel, text)
        reader = _reader_for(cf, monkeypatch) if with_index else None
        bsl = _ext_bsl(cf, reader, [ext])
        for doc, command in (("Д", ext_cmd), (own_doc, "Печать")):
            res = bsl["find_print_forms"](f"Документ.{doc}")
            assert res["print_forms"] == [], res
            [cand] = res["_meta"]["command_candidates"]
            assert (cand["kind"], cand["owner"], cand["command"]) == ("object_command", f"Document.{doc}", command)
            assert cand["module_path"].startswith(f"../cfe/{ext}/Documents/{doc}/Commands/{command}/"), cand
            assert "самого объекта нет" not in res["hint"], res["hint"]
            assert f"read_procedure({cand['module_path']!r}, 'ОбработкаКоманды')" in res["hint"], res["hint"]
            assert "ОбработкаКоманды" in bsl["read_procedure"](cand["module_path"], "ОбработкаКоманды")

    def test_wrapper_session_finds_object_commands(self, tmp_path):
        # корень сессии — каталог над конфигурацией: описатель объекта ищется от текущего корня (раньше — от
        # корня сессии, и команда объекта терялась, а подсказка все равно называла «самого объекта»)
        cf, paths = _pf_tree(tmp_path / "src", "cf")
        bsl = _bsl_for(
            cf.parent, None, current_config_role="main", current_config_name="Тест", current_config_root=str(cf)
        )
        res = bsl["find_print_forms"]("Документ.ВходящееПисьмо")
        module = "cf/" + paths["object"]
        own = {
            "kind": "object_command",
            "owner": "Document.ВходящееПисьмо",
            "command": "Ответить",
            "module_path": module,
        }
        assert own in res["_meta"]["command_candidates"], res
        assert "ОбработкаКоманды" in bsl["read_procedure"](module, "ОбработкаКоманды")

    def test_journal_only_candidates_do_not_name_the_object(self, tmp_path):
        # кандидаты только из журнала: подсказка не обещает команд «самого объекта»
        root = _cf_doc_tree(
            tmp_path / "cf",
            {
                "Documents/БезКоманд.xml": _pf_cf_doc("БезКоманд", []),
                "Documents/БезКоманд/Ext/ManagerModule.bsl": "Процедура Прочее() Экспорт\nКонецПроцедуры\n",
                "DocumentJournals/Ж.xml": _pf_cf_journal("Ж", ["БезКоманд"], ["ПечатьЖ"]),
                "DocumentJournals/Ж/Commands/ПечатьЖ/Ext/CommandModule.bsl": _PF_HANDLER,
            },
        )
        hint = _bsl_for(root)["find_print_forms"]("Документ.БезКоманд")["hint"]
        assert "Печать может быть командой журналов документов (Ж): _meta.command_candidates (1)." in hint, hint
        assert "самого объекта" not in hint, hint

    @pytest.mark.parametrize("fmt", ["cf", "edt"])
    def test_document_with_print_keeps_answer(self, tmp_path, fmt):
        root, _paths = _pf_tree(tmp_path, fmt)
        res = _bsl_for(root)["find_print_forms"]("СПечатью")
        assert [p["name"] for p in res["print_forms"]] == ["Накладная"], res
        assert "hint" not in res and set(res["_meta"]) == {"delegates"}, res

    @pytest.mark.parametrize("fmt", ["cf", "edt"])
    def test_fragment_keeps_substring_route(self, tmp_path, fmt):
        root, _paths = _pf_tree(tmp_path, fmt)
        bsl = _bsl_for(root)
        res = bsl["find_print_forms"]("СПеч")
        assert [p["name"] for p in res["print_forms"]] == ["Накладная"], res
        assert "hint" not in res and set(res["_meta"]) == {"delegates"}, res
        missing = bsl["find_print_forms"]("НетТакого")
        assert missing == {"object": "НетТакого", "print_forms": [], "_meta": {"delegates": []}}, missing


# ═══════════════ Задача 16 (Д21, Д22, Д23) — наборы подписок, проведение у непроводимого, раскрытие ═══════════════


def _ss_subscription(name: str, source: str, event: str = "BeforeWrite") -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses" xmlns:v8="http://v8.1c.ru/8.1/data/core">\n'
        f"<EventSubscription><Properties><Name>{name}</Name><Source>{source}</Source>"
        f"<Handler>CommonModule.М.{name}</Handler><Event>{event}</Event></Properties></EventSubscription>"
        "</MetaDataObject>\n"
    )


def _ss_object(tag: str, name: str, posting: str = "") -> str:
    post = f"<Posting>{posting}</Posting>" if posting else ""
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>\n<MetaDataObject {_PF_NS}>\n'
        f'  <{tag} uuid="o-{name}"><Properties><Name>{name}</Name>{post}</Properties></{tag}>\n'
        "</MetaDataObject>\n"
    )


_SS_SETS = {
    "ПослНабор": "<v8:TypeSet>cfg:SequenceRecordSet</v8:TypeSet>",
    "ПерерасчНабор": "<v8:TypeSet>cfg:RecalculationRecordSet</v8:TypeSet>",
    "ЖурнНабор": "<v8:TypeSet>cfg:DocumentJournalManager</v8:TypeSet>",
    "СпрНабор": "<v8:TypeSet>cfg:CatalogObject</v8:TypeSet>",
    "ДокЗапись": "<v8:TypeSet>cfg:DocumentObject</v8:TypeSet>",
    "Точная": "<v8:Type>cfg:CatalogObject.Спр</v8:Type>",
}
_SS_BODY = "Процедура П() Экспорт\nКонецПроцедуры\n"


def _ss_tree(root: Path) -> Path:
    spec = {
        "Catalogs/Спр.xml": _ss_object("Catalog", "Спр"),
        "Catalogs/Спр/Ext/ObjectModule.bsl": _SS_BODY,
        "Catalogs/Омоним.xml": _ss_object("Catalog", "Омоним"),
        "Catalogs/Омоним/Ext/ObjectModule.bsl": _SS_BODY,
        "Documents/Док.xml": _ss_object("Document", "Док", "Allow"),
        "Documents/Док/Ext/ObjectModule.bsl": _SS_BODY,
        "Documents/ДокНП.xml": _ss_object("Document", "ДокНП", "Deny"),
        "Documents/ДокНП/Ext/ObjectModule.bsl": _SS_BODY,
        "Documents/ДокБезПроведения.xml": _ss_object("Document", "ДокБезПроведения"),
        "Documents/ДокБезПроведения/Ext/ObjectModule.bsl": _SS_BODY,
        "Documents/Омоним.xml": _ss_object("Document", "Омоним", "Deny"),
        "Documents/Омоним/Ext/ObjectModule.bsl": _SS_BODY,
        "DocumentJournals/Жур.xml": _pf_cf_journal("Жур", ["Док"], []),
        "EventSubscriptions/ДокПроведение.xml": _ss_subscription(
            "ДокПроведение", "<v8:TypeSet>cfg:DocumentObject</v8:TypeSet>", "Posting"
        ),
        "EventSubscriptions/ДокОтмена.xml": _ss_subscription(
            "ДокОтмена", "<v8:TypeSet>cfg:DocumentObject</v8:TypeSet>", "UndoPosting"
        ),
        **{f"EventSubscriptions/{n}.xml": _ss_subscription(n, src) for n, src in _SS_SETS.items()},
    }
    return _cf_doc_tree(root / "cf", spec)


@pytest.fixture
def ss_root(tmp_path):
    return _ss_tree(tmp_path)


@pytest.fixture
def ss_bsl(ss_root, monkeypatch):
    return _bsl_for(ss_root, _reader_for(ss_root, monkeypatch, build_calls=False, build_metadata=True))


class TestSubscriptionSets:
    @pytest.mark.parametrize("ref", ["Справочник.Спр", "Документ.Док"])
    def test_out_of_map_sets_are_not_unresolved(self, ss_bsl, ref):
        rows = ss_bsl["find_event_subscriptions"](ref)
        assert not [r for r in rows if r["matched_via"] == "set_unresolved"], rows
        assert not {"ПослНабор", "ПерерасчНабор", "ЖурнНабор"} & {r["name"] for r in rows}, rows
        refs = ss_bsl["find_references_to_object"](ref, kinds=["event_subscription_source"])
        assert len(rows) == refs["by_kind"]["event_subscription_source"] == refs["total"], (rows, refs)

    def test_journal_manager_set_matches_journal(self, ss_bsl):
        rows = ss_bsl["find_event_subscriptions"]("ЖурналДокументов.Жур")
        row = next(r for r in rows if r["name"] == "ЖурнНабор")
        assert (row["scope"], row["matched_via"], row["matched_sets"]) == ("set", "set", ["DocumentJournalManager"])
        assert all(r["matched_via"] != "set_unresolved" for r in rows), rows

    def test_four_consumers_agree(self, ss_root, ss_bsl):
        live = _bsl_for(ss_root)["find_event_subscriptions"]("Справочник.Спр")
        on_index = ss_bsl["find_event_subscriptions"]("Справочник.Спр")
        profile = ss_bsl["get_object_profile"]("Справочник.Спр", sections=["subscriptions"])["sections"]
        refs = ss_bsl["find_references_to_object"]("Справочник.Спр", kinds=["event_subscription_source"])
        names = {r["name"] for r in on_index}
        assert names == {"СпрНабор", "Точная"}, on_index
        assert {r["name"] for r in live} == names, live
        assert {r["name"] for r in profile["subscriptions"]["items"]} == names, profile
        assert {r["used_in"].split(".")[1] for r in refs["references"]} == names, refs


class TestPostingSubscriptionsOnDeny:
    def test_posting_events_marked_unreachable_on_deny(self, ss_bsl):
        rows = {r["name"]: r for r in ss_bsl["find_event_subscriptions"]("Документ.ДокНП")}
        assert rows["ДокПроведение"]["event_reachable"] is False
        assert rows["ДокОтмена"]["event_reachable"] is False
        assert "event_reachable" not in rows["ДокЗапись"], rows

    @pytest.mark.parametrize("ref", ["Документ.Док", "Документ.ДокБезПроведения", "Омоним"])
    def test_no_mark_when_postable_undetermined_or_homonym(self, ss_bsl, ref):
        rows = ss_bsl["find_event_subscriptions"](ref)
        assert rows and all("event_reachable" not in r for r in rows), rows

    def test_bare_document_name_is_marked(self, ss_bsl):
        rows = {r["name"]: r for r in ss_bsl["find_event_subscriptions"]("ДокНП")}
        assert rows["ДокПроведение"]["event_reachable"] is False, rows

    def test_live_branch_marks_too(self, ss_root):
        rows = {r["name"]: r for r in _bsl_for(ss_root)["find_event_subscriptions"]("Документ.ДокНП")}
        assert rows["ДокПроведение"]["event_reachable"] is False and "event_reachable" not in rows["ДокЗапись"]

    def test_consumers_agree_on_mark(self, ss_bsl):
        def _pairs(rows):
            return sorted((r["name"], r["event"], r.get("event_reachable")) for r in rows)

        subs = ss_bsl["find_event_subscriptions"]("Документ.ДокНП")
        sections = ss_bsl["get_object_profile"]("Документ.ДокНП", sections=["subscriptions", "flow"])["sections"]
        flow = ss_bsl["analyze_document_flow"]("Документ.ДокНП")
        assert ("ДокПроведение", "Posting", False) in _pairs(subs), subs
        assert _pairs(subs) == _pairs(sections["subscriptions"]["items"]) == _pairs(flow["event_subscriptions"])
        assert len(sections["subscriptions"]["items"]) == len(subs) == len(flow["event_subscriptions"])
        marks = sorted((i["event"], i.get("event_reachable")) for i in sections["flow"]["items"])
        assert marks == sorted((r["event"], r.get("event_reachable")) for r in subs), sections["flow"]


class TestReferenceExpansionCounts:
    def test_expanded_on_read_counts_set_rows(self, ss_bsl):
        refs = ss_bsl["find_references_to_object"]("Справочник.Спр")
        assert refs["_meta"]["expanded_on_read"] == {"event_subscription_source": 1}, refs["_meta"]

    def test_expanded_on_read_always_present(self, ss_root, ss_bsl):
        assert ss_bsl["find_references_to_object"]("Справочник.Омоним")["_meta"]["expanded_on_read"] == {
            "event_subscription_source": 1
        }
        live = _bsl_for(ss_root)["find_references_to_object"]("Справочник.Спр")
        assert live["_meta"]["expanded_on_read"] == {}, live["_meta"]
        missing = ss_bsl["find_references_to_object"]("НетТакого")
        assert missing["_meta"]["expanded_on_read"] == {}, missing["_meta"]


# ═══════════════ Задача 17 (Д24, Д25) — суффиксы used_in и kind_filter_applied ═══════════════


def _ui_cf(tag: str, name: str, props: str) -> str:
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>\n<MetaDataObject {_PF_NS}>\n'
        f'  <{tag} uuid="u-{name}"><Properties><Name>{name}</Name>{props}</Properties></{tag}>\n'
        "</MetaDataObject>\n"
    )


def _ui_mdo(tag: str, name: str, body: str) -> str:
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>\n<mdclass:{tag} {_PF_MDO_NS} uuid="u-{name}">'
        f"<name>{name}</name>{body}</mdclass:{tag}>\n"
    )


_UI_MODULE = "Процедура П() Экспорт\n\tС = Справочники.Спр.ПустаяСсылка();\nКонецПроцедуры\n"


def _ui_tree(root: Path, fmt: str) -> Path:
    if fmt == "cf":
        spec = {
            "Catalogs/Спр.xml": _ui_cf(
                "Catalog",
                "Спр",
                "<DefaultObjectForm>Catalog.Спр.Form.ФормаЭлемента</DefaultObjectForm>"
                "<DefaultListForm>Catalog.Спр.Form.ФормаСписка</DefaultListForm>",
            ),
            "Documents/Док.xml": _ui_cf(
                "Document", "Док", '<BasedOn><xr:Item xsi:type="xr:MDObjectRef">Catalog.Спр</xr:Item></BasedOn>'
            ),
            "DataProcessors/Обр.xml": _ui_cf(
                "DataProcessor", "Обр", "<MainForm>DataProcessor.Обр.Form.Форма</MainForm>"
            ),
            "Enums/Пер.xml": _ui_cf("Enum", "Пер", "<ListForm>Enum.Пер.Form.ФормаСписка</ListForm>"),
            "Documents/Док/Ext/ObjectModule.bsl": _UI_MODULE,
            "Catalogs/Спр/Ext/ObjectModule.bsl": _SS_BODY,
            "DataProcessors/Обр/Ext/ObjectModule.bsl": _SS_BODY,
            "Enums/Пер/Ext/ManagerModule.bsl": _SS_BODY,
        }
        return _cf_doc_tree(root / "cf", spec)
    spec = {
        "Catalogs/Спр/Спр.mdo": _ui_mdo(
            "Catalog",
            "Спр",
            "<defaultObjectForm>Catalog.Спр.Form.ФормаЭлемента</defaultObjectForm>"
            "<defaultListForm>Catalog.Спр.Form.ФормаСписка</defaultListForm>",
        ),
        "Documents/Док/Док.mdo": _ui_mdo("Document", "Док", "<basedOn>Catalog.Спр</basedOn>"),
        "DataProcessors/Обр/Обр.mdo": _ui_mdo(
            "DataProcessor", "Обр", "<mainForm>DataProcessor.Обр.Form.Форма</mainForm>"
        ),
        "Enums/Пер/Пер.mdo": _ui_mdo("Enum", "Пер", "<listForm>Enum.Пер.Form.ФормаСписка</listForm>"),
        "Documents/Док/ObjectModule.bsl": _UI_MODULE,
        "Catalogs/Спр/ObjectModule.bsl": _SS_BODY,
        "DataProcessors/Обр/ObjectModule.bsl": _SS_BODY,
        "Enums/Пер/ManagerModule.bsl": _SS_BODY,
    }
    return _edt_doc_tree(root / "edt", spec)


# Индекс хранит ссылки based_on и форм по умолчанию; main_form/list_form обработки и перечисления — только
# разбор описателя (сверяются отдельно, на уровне разборщика).
_UI_CASES = [
    ("Справочник.Спр", ["based_on"]),
    ("Справочник.Спр", ["default_object_form", "default_list_form"]),
]
_UI_EXPECTED = {
    "Document.Док.BasedOn",
    "Catalog.Спр.DefaultObjectForm=Catalog.Спр.Form.ФормаЭлемента",
    "Catalog.Спр.DefaultListForm=Catalog.Спр.Form.ФормаСписка",
}


def _ui_used_in(bsl) -> set[str]:
    out: set[str] = set()
    for ref, kinds in _UI_CASES:
        out |= {r["used_in"] for r in bsl["find_references_to_object"](ref, kinds=kinds)["references"]}
    return out


class TestUsedInSuffixes:
    @pytest.mark.parametrize("with_index", [True, False], ids=["index", "live"])
    def test_cf_and_edt_give_same_used_in(self, tmp_path, monkeypatch, with_index):
        got = {}
        for fmt in ("cf", "edt"):
            root = _ui_tree(tmp_path, fmt)
            reader = _reader_for(root, monkeypatch, build_calls=False, build_metadata=True) if with_index else None
            got[fmt] = _ui_used_in(_bsl_for(root, reader))
        assert got["cf"] == got["edt"] == _UI_EXPECTED, got

    def test_parser_suffixes_match_between_formats(self, tmp_path):
        from rlm_tools_bsl.bsl_xml_parsers import parse_metadata_xml

        def _suffixes(fmt: str) -> set[tuple[str, str]]:
            root = _ui_tree(tmp_path, fmt)
            out: set[tuple[str, str]] = set()
            for f in sorted(root.rglob("*.xml" if fmt == "cf" else "*.mdo")):
                parsed = parse_metadata_xml(f.read_text(encoding="utf-8")) or {}
                out |= {(r["ref_kind"], r["used_in_suffix"]) for r in parsed.get("references") or []}
            return out

        cf, edt = _suffixes("cf"), _suffixes("edt")
        assert cf == edt, (cf, edt)
        assert {k for k, _ in cf} == {"based_on", "default_object_form", "default_list_form", "main_form", "list_form"}
        assert ("main_form", "MainForm=DataProcessor.Обр.Form.Форма") in edt and ("based_on", "BasedOn") in edt

    def test_reader_normalizes_rows_of_an_older_index(self, tmp_path, monkeypatch):
        import sqlite3

        root = _ui_tree(tmp_path, "edt")
        monkeypatch.setenv("RLM_INDEX_DIR", str(root.parent / (root.name + "_idx")))
        db = IndexBuilder().build(str(root), build_calls=False, build_metadata=True)
        conn = sqlite3.connect(db)
        for new, old in (
            (".BasedOn", ".Basedon"),
            (".DefaultObjectForm=", ".defaultObjectForm="),
            (".DefaultListForm=", ".defaultListForm="),
        ):
            conn.execute("UPDATE metadata_references SET used_in = replace(used_in, ?, ?)", (new, old))
        conn.commit()
        assert conn.execute("SELECT count(*) FROM metadata_references WHERE used_in LIKE '%.Basedon'").fetchone()[0]
        conn.close()
        reader = IndexReader(db)
        assert _ui_used_in(_bsl_for(root, reader)) == _UI_EXPECTED
        outbound = reader.find_metadata_refs_from("Док", "Documents")
        assert [r["used_in"] for r in outbound if r["ref_kind"] == "based_on"] == ["Document.Док.BasedOn"], outbound

    def test_normalize_used_in(self):
        from rlm_tools_bsl.bsl_xml_parsers import normalize_used_in

        cases = [
            ("BusinessProcess.Задание.Basedon", "based_on", "BusinessProcess.Задание.BasedOn"),
            ("Basedon", "based_on", "BasedOn"),
            (
                "Catalog.X.defaultObjectForm=Catalog.X.Form.Ф",
                "default_object_form",
                "Catalog.X.DefaultObjectForm=Catalog.X.Form.Ф",
            ),
            ("defaultListForm=Catalog.X.Form.С", "default_list_form", "DefaultListForm=Catalog.X.Form.С"),
            ("Report.Р.mainForm=Report.Р.Form.Ф", "main_form", "Report.Р.MainForm=Report.Р.Form.Ф"),
            ("Enum.П.listForm=Enum.П.Form.С", "list_form", "Enum.П.ListForm=Enum.П.Form.С"),
            ("Catalog.X.Attribute.Y.Type", "attribute_type", "Catalog.X.Attribute.Y.Type"),
            ("Catalog.X.Owners", "owner", "Catalog.X.Owners"),
            ("", "based_on", ""),
        ]
        for raw, kind, expected in cases:
            assert normalize_used_in(raw, kind) == expected, raw
            assert normalize_used_in(expected, kind) == expected, expected  # идемпотентна


class TestKindFilterApplied:
    def test_three_values(self, tmp_path, monkeypatch):
        root = _ui_tree(tmp_path, "cf")
        indexed = _bsl_for(root, _reader_for(root, monkeypatch, build_calls=False, build_metadata=True))
        live = _bsl_for(root)
        on_index = indexed["find_code_usages"]("Справочник.Спр", kind="manager")
        assert on_index["_meta"]["source"] == "index" and on_index["_meta"]["kind_filter_applied"] is True
        assert indexed["find_code_usages"]("Справочник.Спр")["_meta"]["kind_filter_applied"] is None
        live_kind = live["find_code_usages"]("Справочник.Спр", kind="manager")
        assert live_kind["_meta"]["source"] == "live" and live_kind["_meta"]["kind_filter_applied"] is False
        assert live["find_code_usages"]("Справочник.Спр")["_meta"]["kind_filter_applied"] is None


# ═══════════════ Задача 18 (Д26–Д32) — подсказки, меню, подписи и служебные ответы ═══════════════


class TestReadFilesNudge:
    def test_read_files_nudge_counts_only_single_name_reads(self, tmp_path):
        from rlm_tools_bsl.sandbox import Sandbox

        mod = "Documents/Д/Ext/ObjectModule.bsl"
        root = _cf_doc_tree(
            tmp_path / "cf", {mod: "Процедура А() Экспорт\nКонецПроцедуры\nПроцедура Б() Экспорт\nКонецПроцедуры\n"}
        )
        sb = Sandbox(base_path=str(root), format_info=detect_format(str(root)))
        res = sb.execute(
            f"read_procedure({mod!r}, ['А', 'Б'])\nread_procedure({mod!r}, proc_name=['А'])\n"
            f"read_procedure({mod!r}, 'А')\nread_procedure({mod!r}, proc_name='Б')\n"
        )
        assert "read_files" not in {h["id"] for h in res.efficiency_hints or []}, res.efficiency_hints
        res = sb.execute(f"read_procedure({mod!r}, 'Б')")
        hint = next(h for h in res.efficiency_hints or [] if h["id"] == "read_files")
        assert hint["trigger"] == "read_procedure x3 single calls in session" and hint["count"] == 3, hint


class TestHelpMenuTitles:
    def test_every_recipe_starts_with_title_line(self):
        for name, entry in build_helper_metadata_snapshot().items():
            first = (entry["recipe"] or "").split("\n")[0]
            if entry["recipe"]:
                assert (
                    first and not first.lstrip().startswith("#") and first.rstrip().endswith(":") and len(first) <= 130
                ), (name, first)


_SK_FORM = "Catalogs/Спр/Forms/ФормаЭлемента/Ext/Form.xml"
_SK_SPEC = {
    **_OV_MAIN,
    "Catalogs/Спр.xml": _ss_object("Catalog", "Спр"),
    _SK_FORM: (
        '<Form xmlns="http://v8.1c.ru/8.3/xcf/logform" xmlns:v8="http://v8.1c.ru/8.1/data/core">'
        '<Attributes><Attribute name="Объект"><Type><v8:Type>cfg:CatalogObject.Спр</v8:Type></Type>'
        "<MainAttribute>true</MainAttribute></Attribute></Attributes></Form>"
    ),
    "Catalogs/Спр/Forms/ФормаЭлемента/Ext/Form/Module.bsl": "&НаСервере\nПроцедура П()\nКонецПроцедуры\n",
}

# Ключи, которые подпись называет и ответ несёт (Д28 — последние в каждом списке до правки не назывались).
SIG_KEYS = {
    "parse_form": ["form_name", "module_path", "handlers", "commands", "attributes", "category", "object_name", "file"],
    "extract_procedures": ["name", "type", "line", "end_line", "is_export", "params", "overridden_by"],
    "detect_extensions": [
        "config_role",
        "nearby_extensions",
        "nearby_main",
        "warnings",
        "config_name",
        "config_prefix",
    ],
    "find_ext_overrides": ["overrides", "total", "truncated", "partial", "extension_path", "object_filter"],
    "get_index_info": ["status", "builder_version", "config_name", "has_fts", "has_synonyms", "index_status"],
}


class TestSignatureKeys:
    def test_sigs_name_keys_present_in_answers(self, tmp_path, monkeypatch):
        import re

        cf = _ext_tree(tmp_path, _SK_SPEC, {"Расш1": _OV_EXT})
        bsl = _ext_bsl(cf, _reader_for(cf, monkeypatch), ["Расш1"])
        answers = {
            "parse_form": set().union(*(set(f) for f in bsl["parse_form"]("Справочник.Спр"))),
            "extract_procedures": set().union(*(set(p) for p in bsl["extract_procedures"](_OV_DOC))),
            "detect_extensions": set(bsl["detect_extensions"]()),
            "find_ext_overrides": set(bsl["find_ext_overrides"](str(cf.parent / "cfe" / "Расш1"))),
            "get_index_info": set(bsl["get_index_info"]()),
        }
        snap = build_helper_metadata_snapshot()
        for helper, keys in SIG_KEYS.items():
            named = set(re.findall(r"[A-Za-z_]+", snap[helper]["sig"]))
            for key in keys:
                assert key in named, (helper, key, snap[helper]["sig"])
                assert key in answers[helper], (helper, key, sorted(answers[helper]))


class TestIndexInfo:
    def test_index_info_names_freshness_and_counts(self, tmp_path, monkeypatch):
        root = _cf_doc_tree(tmp_path / "cf", {"CommonModules/М/Ext/Module.bsl": _GK_BODY})
        bsl = _bsl_for(root, _reader_for(root, monkeypatch))
        gi = bsl["get_index_info"]()
        assert gi["status"] == "ok" and gi["index_status"] in {"ok", "stale_age", "stale_content"}, gi
        assert gi["regions_count"] == bsl["search_regions"]("", count_only=True)["total"] == 1, gi
        assert gi["module_headers_count"] == 0 and gi["content_sample"] == "not_checked", gi

    @pytest.fixture
    def info_root(self, tmp_path, monkeypatch):
        from rlm_tools_bsl.server import _canonicalize_path

        monkeypatch.setenv("RLM_SANDBOX_MODE", "inline")
        root = Path(_canonicalize_path(str(tmp_path))) / "cf"
        _cf_doc_tree(root, {"CommonModules/М/Ext/Module.bsl": _GK_BODY})

        def make(state: str) -> Path:
            monkeypatch.setenv("RLM_INDEX_DIR", str(root.parent / (root.name + "_idx")))
            if state == "absent":
                return root
            db = IndexBuilder().build(str(root))
            if state == "stale_age":
                monkeypatch.setenv("RLM_INDEX_MAX_AGE_DAYS", "0")
            if state == "incomplete":
                import sqlite3

                conn = sqlite3.connect(db)
                conn.execute("INSERT OR REPLACE INTO index_meta (key, value) VALUES ('build_in_progress', '1')")
                conn.commit()
                conn.close()
            return root

        return make

    @pytest.mark.parametrize(
        "state, label",
        [("absent", "missing"), ("incomplete", "incomplete"), ("fresh", "ok"), ("stale_age", "stale_age")],
    )
    def test_index_info_status_matches_start(self, info_root, state, label):
        start = _start_json(info_root(state))
        try:
            gi = _execute_json(start, "get_index_info()")
            assert start["index"]["index_status"] == gi["index_status"] == label, (start["index"], gi)
            if state in ("absent", "incomplete"):
                assert gi["status"] == "no_index" and gi["content_sample"] == "not_checked", gi
                assert gi["regions_count"] is None and gi["module_headers_count"] is None, gi
            else:
                assert gi["status"] == "ok" and gi["regions_count"] == 1, gi
            if state == "stale_age":
                assert gi["content_sample"] in {"ok", "skipped", "mismatch", "unavailable"}, gi
        finally:
            _end(start)


class TestIndexInfoFollowsStart:
    """Метка свежести и итог выборки — СТАРТА сессии: на частично разошедшемся индексе случайная выборка
    иначе давала бы у get_index_info то одну, то другую метку (находка ревью вехи 5)."""

    @pytest.fixture
    def diverged_root(self, tmp_path, monkeypatch):
        import time

        from rlm_tools_bsl.server import _canonicalize_path

        root = Path(_canonicalize_path(str(tmp_path))) / "cf"
        _cf_doc_tree(
            root,
            {f"CommonModules/М{i}/Ext/Module.bsl": f"Процедура П{i}() Экспорт\nКонецПроцедуры\n" for i in range(60)},
        )
        _reader_for(root, monkeypatch).close()
        for i in range(24):  # до сессии правится часть модулей — индекс не пересобирается
            p = root / f"CommonModules/М{i}/Ext/Module.bsl"
            p.write_text(p.read_text(encoding="utf-8") + "// правка\n", encoding="utf-8")
            os.utime(p, (time.time() + 100, time.time() + 100))
        monkeypatch.setenv("RLM_INDEX_SAMPLE_SIZE", "5")
        monkeypatch.setenv("RLM_INDEX_SAMPLE_THRESHOLD", "1")
        return root

    @pytest.mark.parametrize("mode", ["inline", "process"])
    def test_stale_content_label_is_the_start_label(self, diverged_root, monkeypatch, mode):
        monkeypatch.setenv("RLM_SANDBOX_MODE", mode)
        monkeypatch.setenv("RLM_INDEX_SKIP_SAMPLE_HOURS", "0")  # выборка — как у индекса старше суток
        start = _start_json(diverged_root)
        try:
            labels = _execute_json(start, "[get_index_info()['index_status'] for _ in range(12)]")
            assert set(labels) == {start["index"]["index_status"]}, (start["index"]["index_status"], labels)
        finally:
            _end(start)

    def test_stale_age_content_sample_is_the_start_sample(self, diverged_root, monkeypatch):
        monkeypatch.setenv("RLM_SANDBOX_MODE", "inline")
        monkeypatch.setenv("RLM_INDEX_MAX_AGE_DAYS", "0")
        start = _start_json(diverged_root)
        try:
            samples = _execute_json(start, "[get_index_info()['content_sample'] for _ in range(12)]")
            assert len(set(samples)) == 1, samples
            warned = " ".join(start["index"]["warnings"])
            assert ("расходится" in warned) == (samples[0] == "mismatch"), (warned, samples)
            assert ("совпадает" in warned) == (samples[0] == "ok"), (warned, samples)
        finally:
            _end(start)


class TestStartLimits:
    def test_start_limits_name_max_output_chars(self, tmp_path, monkeypatch):
        from rlm_tools_bsl.server import _canonicalize_path

        monkeypatch.setenv("RLM_SANDBOX_MODE", "inline")
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
        root = _cf_doc_tree(
            Path(_canonicalize_path(str(tmp_path))) / "cf", {"CommonModules/М/Ext/Module.bsl": _GK_BODY}
        )
        start = _start_json(root, max_output_chars=30000)
        try:
            assert start["limits"]["max_output_chars"] == 30000, start["limits"]
        finally:
            _end(start)
