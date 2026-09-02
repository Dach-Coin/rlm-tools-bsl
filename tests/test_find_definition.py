"""Tests for find_definition (v1.20.0) — the forward go-to-definition primitive.

Covers the ``IndexReader.get_definitions`` reader (NOCASE index seek, Cyrillic
``py_lower`` slow-fallback, exact ``total`` via COUNT on truncation, ``None`` on
``OperationalError``) and the ``find_definition`` helper (index path, the three
``module_hint`` forms, live fallback, ``_meta`` semantics).
"""

import os
import textwrap

import pytest

from rlm_tools_bsl.bsl_index import IndexBuilder, IndexReader
from rlm_tools_bsl.bsl_helpers import make_bsl_helpers
from rlm_tools_bsl.format_detector import detect_format
from rlm_tools_bsl.helpers import make_helpers


_CONFIG_XML = textwrap.dedent("""\
    <?xml version="1.0" encoding="UTF-8"?>
    <MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">
        <Configuration><Properties><Name>Конф</Name><NamePrefix/></Properties></Configuration>
    </MetaDataObject>
""")

_CM_BSL = textwrap.dedent("""\
    Функция ПересчитатьИтоги(Знач Дата, Пересчитать) Экспорт
        Возврат Истина;
    КонецФункции

    Процедура ВнутренняяПроцедура()
        // не экспорт
    КонецПроцедуры
""")

_DOC_BSL = textwrap.dedent("""\
    Процедура ОбработкаПроведения(Отказ, Режим)
        // проведение
    КонецПроцедуры
""")


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _make_project(tmp_path, n_docs=3):
    cf = os.path.join(tmp_path, "src", "cf")
    _write(os.path.join(cf, "Configuration.xml"), _CONFIG_XML)
    _write(os.path.join(cf, "CommonModules", "Расчёты", "Ext", "Module.bsl"), _CM_BSL)
    for i in range(1, n_docs + 1):
        _write(os.path.join(cf, "Documents", f"Док{i}", "Ext", "ObjectModule.bsl"), _DOC_BSL)
    return cf


def _make_bsl(base, **kwargs):
    helpers, resolve_safe = make_helpers(str(base))
    format_info = detect_format(str(base))
    return make_bsl_helpers(
        base_path=str(base),
        resolve_safe=resolve_safe,
        read_file_fn=helpers["read_file"],
        grep_fn=helpers["grep"],
        glob_files_fn=helpers["glob_files"],
        format_info=format_info,
        **kwargs,
    )


@pytest.fixture
def built(tmp_path):
    """Index-backed helpers over a 3-document fixture."""
    cf = _make_project(tmp_path, n_docs=3)
    db_path = IndexBuilder().build(cf, build_calls=True, build_metadata=True, build_fts=True)
    reader = IndexReader(db_path)
    bsl = _make_bsl(cf, idx_reader=reader)
    yield bsl, reader, cf
    reader.close()


@pytest.fixture
def live(tmp_path):
    """Helpers WITHOUT an index (live fallback path)."""
    cf = _make_project(tmp_path, n_docs=2)
    return _make_bsl(cf)


# ---------------------------------------------------------------------------
# Index path
# ---------------------------------------------------------------------------


def test_unique_export_no_hint(built):
    bsl, _r, _cf = built
    d = bsl["find_definition"]("ПересчитатьИтоги")
    assert d["total"] == 1
    assert d["_meta"]["index_used"] is True
    assert d["_meta"]["unique"] is True
    assert d["_meta"]["hint_applied"] is False
    assert d["_meta"]["slow_fallback"] is False
    defn = d["definitions"][0]
    assert defn["file"].endswith("Module.bsl")
    assert defn["type"] == "Функция"  # type passes through the source keyword verbatim
    assert defn["is_export"] is True
    assert defn["category"] == "CommonModules"
    assert defn["object_name"] == "Расчёты"


def test_params_are_list_str(built):
    """params contract = list[str] (v1.18.0)."""
    bsl, _r, _cf = built
    d = bsl["find_definition"]("ПересчитатьИтоги")
    assert d["definitions"][0]["params"] == ["Дата", "Пересчитать"]


def test_ambiguous_no_hint_sorted(built):
    bsl, _r, _cf = built
    d = bsl["find_definition"]("ОбработкаПроведения")
    assert d["total"] == 3
    assert d["_meta"]["unique"] is False
    files = [x["file"] for x in d["definitions"]]
    assert files == sorted(files)  # ORDER BY mod.rel_path → deterministic


def test_hint_category_object_narrows(built):
    bsl, _r, _cf = built
    d = bsl["find_definition"]("ОбработкаПроведения", "Документ.Док2")
    assert d["total"] == 1
    assert d["_meta"]["hint_applied"] is True
    assert d["_meta"]["unique"] is True
    assert "Док2" in d["definitions"][0]["file"]


def test_hint_rel_path(built):
    bsl, _r, _cf = built
    mods = bsl["find_module"]("Док1")
    rel = next(m["path"] for m in mods if m["path"].endswith("ObjectModule.bsl"))
    d = bsl["find_definition"]("ОбработкаПроведения", rel)
    assert d["total"] == 1
    assert d["definitions"][0]["file"] == rel
    assert d["_meta"]["hint_applied"] is True


def test_nonexistent_name_total_zero_not_error(built):
    bsl, _r, _cf = built
    d = bsl["find_definition"]("НетТакогоМетода")
    assert d["total"] == 0
    assert d["definitions"] == []
    assert "error" not in d
    assert d["_meta"]["index_used"] is True


def test_empty_name_error_dict(built):
    bsl, _r, _cf = built
    d = bsl["find_definition"]("")
    assert "error" in d and "hint" in d
    d2 = bsl["find_definition"]("   ")
    assert "error" in d2


def test_truncated_exact_total_via_count(built):
    """Codex #5: list can't carry total; on truncation total is exact (COUNT)."""
    bsl, _r, _cf = built
    d = bsl["find_definition"]("ОбработкаПроведения", limit=2)
    assert d["truncated"] is True
    assert len(d["definitions"]) == 2
    assert d["total"] == 3  # exact, not limit+1


def test_cyrillic_lowercase_slow_fallback(built):
    """Codex #3: lowercase Cyrillic input → py_lower rescan finds PascalCase.

    With the old ``name != casefold()`` gate this would return total=0."""
    bsl, _r, _cf = built
    d = bsl["find_definition"]("пересчитатьитоги")
    assert d["total"] == 1
    assert d["_meta"]["slow_fallback"] is True
    assert d["definitions"][0]["object_name"] == "Расчёты"


def test_canonical_pascalcase_no_slow_fallback(built):
    bsl, _r, _cf = built
    d = bsl["find_definition"]("ПересчитатьИтоги")
    assert d["total"] == 1
    assert d["_meta"]["slow_fallback"] is False  # NOCASE byte-equal seek


def test_hint_applied_true_even_when_no_match(built):
    """R2-2: hint_applied = 'filter applied to query', not 'hint changed count'."""
    bsl, _r, _cf = built
    d = bsl["find_definition"]("ПересчитатьИтоги", "Документ.НетТакого")
    assert d["_meta"]["hint_applied"] is True
    assert d["total"] == 0  # ПересчитатьИтоги lives in a CommonModule, not Документ.НетТакого


def test_determinism(built):
    bsl, _r, _cf = built
    assert bsl["find_definition"]("ОбработкаПроведения") == bsl["find_definition"]("ОбработкаПроведения")


def test_reader_get_definitions_contract(built):
    """Direct reader contract: dict with rows/total/truncated/slow_fallback."""
    _bsl, reader, _cf = built
    res = reader.get_definitions("ОбработкаПроведения", limit=2)
    assert set(res.keys()) == {"rows", "total", "truncated", "slow_fallback"}
    assert res["total"] == 3 and res["truncated"] is True and len(res["rows"]) == 2
    # Empty (valid) result is a dict, NOT None. ASCII miss → no rescan paid.
    empty = reader.get_definitions("NoSuchMethodZZZ")
    assert empty == {"rows": [], "total": 0, "truncated": False, "slow_fallback": False}


def test_cyrillic_miss_still_flags_slow_fallback(built):
    """A MISSING Cyrillic name still pays the py_lower SCAN → slow_fallback=True
    (the flag marks the rescan being paid, not whether it found a row)."""
    bsl, reader, _cf = built
    d = bsl["find_definition"]("неттакогометода")
    assert d["total"] == 0
    assert d["definitions"] == []
    assert d["_meta"]["slow_fallback"] is True
    # reader-level contract too
    res = reader.get_definitions("неттакогометода")
    assert res == {"rows": [], "total": 0, "truncated": False, "slow_fallback": True}


# ---------------------------------------------------------------------------
# Broken core index (reader → None) → fallback, NOT silent false-negative
# ---------------------------------------------------------------------------


def test_broken_core_index_routes_to_fallback(built, monkeypatch):
    """R2-3: OperationalError → reader returns None → helper falls back, never
    a silent ``index_used=True, total=0``."""
    bsl, reader, _cf = built
    monkeypatch.setattr(reader, "get_definitions", lambda *a, **k: None)
    # Hint giving an object → live fallback resolves it.
    d = bsl["find_definition"]("ОбработкаПроведения", "Документ.Док1")
    assert d["_meta"]["index_used"] is False
    assert d["total"] == 1
    # v1.34.0: ветка `res is None` (сломанные core-таблицы) уходит в тот же
    # status-aware live-путь, что и «индекса нет вовсе»: ключа `error` больше нет,
    # выполняется живой поиск объявления.
    d2 = bsl["find_definition"]("ОбработкаПроведения")
    assert "error" not in d2
    assert d2["_meta"]["index_used"] is False
    assert d2["_meta"]["source"] == "live"
    assert d2["_meta"]["slow_fallback"] is False
    assert d2["total"] >= 1
    assert any(row["file"].endswith(".bsl") for row in d2["definitions"])


# ---------------------------------------------------------------------------
# Live fallback (no index)
# ---------------------------------------------------------------------------


def test_fallback_rel_path(live):
    """Codex #6: rel_path hint → direct extract_procedures(rel_path). Metadata
    fields are filled structurally (parse_bsl_path), matching the index path."""
    d = live["find_definition"]("ОбработкаПроведения", "Documents/Док1/Ext/ObjectModule.bsl")
    assert d["_meta"]["index_used"] is False
    assert d["total"] == 1
    defn = d["definitions"][0]
    assert defn["file"] == "Documents/Док1/Ext/ObjectModule.bsl"
    assert defn["params"] == ["Отказ", "Режим"]
    assert defn["category"] == "Documents"
    assert defn["object_name"] == "Док1"
    assert defn["module_type"] == "ObjectModule"


def test_fallback_object_name(live):
    d = live["find_definition"]("ОбработкаПроведения", "Док1")
    assert d["_meta"]["index_used"] is False
    assert d["total"] >= 1
    assert all("Док1" in x["file"] for x in d["definitions"])


def test_fallback_no_hint_runs_a_live_declaration_scan(live):
    """v1.34.0: без индекса и без hint хелпер больше НЕ отвечает
    ``{'error': 'no index'}`` — он выполняет живой поиск объявления по BSL-каталогу.

    Это НЕ additive-изменение: ключ `error` с этого маршрута ИСЧЕЗАЕТ, и код агента
    вида ``if res.get('error'): ...`` меняет ветку."""
    d = live["find_definition"]("ОбработкаПроведения")
    assert "error" not in d
    assert d["_meta"]["index_used"] is False
    assert d["_meta"]["source"] == "live"
    # Старая семантика поля сохранена: slow_fallback — только кириллический
    # py_lower-rescan индексного ридера, на live-ветке всегда False.
    assert d["_meta"]["slow_fallback"] is False
    assert d["total"] == len(d["definitions"]) >= 1
    assert isinstance(d["partial"], bool)
    assert d["_meta"]["total_exact"] is not d["partial"]


def test_fallback_missing_name_is_not_an_error(live):
    """Заведомо отсутствующее имя — пустая выдача, а не ошибка."""
    d = live["find_definition"]("ЗаведомоОтсутствующийМетод")
    assert "error" not in d
    assert d["definitions"] == [] and d["total"] == 0
