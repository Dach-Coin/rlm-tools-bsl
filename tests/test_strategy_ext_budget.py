"""Часть 1: глобальный бюджет дампа overrides расширений (slim + full)."""

from __future__ import annotations

import json
import re
from types import SimpleNamespace

import pytest

from rlm_tools_bsl.bsl_knowledge import _extension_strategy, get_strategy
from rlm_tools_bsl.extension_detector import ConfigRole
from _strategy_fixtures import _MOCK_REGISTRY, _REALISTIC_IDX_STATS


def _ctx_main(n: int):
    return SimpleNamespace(
        current=SimpleNamespace(role=ConfigRole.MAIN, name="ОсновнаяКонфигурация", purpose="", name_prefix=""),
        nearby_extensions=[
            SimpleNamespace(name=f"Расш{i}", name_prefix=f"р{i}_", path=f"/tmp/cfe/Ext{i}", purpose="")
            for i in range(n)
        ],
        nearby_main=None,
    )


def _overrides_for(ctx, per_ext: int):
    return {
        ext.path: [
            {"object_name": f"Объект{ext.name}_{j}", "annotation": "Перед", "target_method": f"Метод{j}"}
            for j in range(per_ext)
        ]
        for ext in ctx.nearby_extensions
    }


# ── Юнит: прямой вызов _extension_strategy (mode-agnostic) ──────────────────


def test_default_pointer_only_no_counters(monkeypatch):
    """#1 (v1.28.0): по умолчанию (budget==0) ext-блок стратегии — счётчик total +
    указатель на nearby_extensions, БЕЗ перечня имён/префиксов в заголовке и БЕЗ
    построчных счётчиков "РасшN: M overrides" (зеркало nearby_extensions[].overrides_count)."""
    monkeypatch.delenv("RLM_EXT_OVERRIDE_DETAIL", raising=False)
    ctx = _ctx_main(14)
    ovr = _overrides_for(ctx, per_ext=90)  # 14*90 = 1260 перехватов
    text = _extension_strategy(ctx, ovr)

    # Нет построчных счётчиков "РасшN: M overrides" (зеркало nearby_extensions).
    assert re.search(r"Расш\d+: \d+ overrides", text) is None
    # Нет перечня имён/префиксов в заголовке.
    assert "(prefix:" not in text
    assert "Метод0" not in text  # БЕЗ поимённого дампа
    assert "&Перед(" not in text
    # Счётчик всех расширений + указатель на структурный источник + on-demand pointer.
    assert "EXTENSIONS DETECTED" in text
    assert "14 EXTENSIONS DETECTED" in text  # total в заголовке
    assert "nearby_extensions" in text
    assert "get_overrides(" in text
    assert len(text) < 4000, f"ext-блок раздут: {len(text)}"


def test_detail_budget_is_global_not_per_extension(monkeypatch):
    monkeypatch.setenv("RLM_EXT_OVERRIDE_DETAIL", "20")
    ctx = _ctx_main(14)
    ovr = _overrides_for(ctx, per_ext=90)
    text = _extension_strategy(ctx, ovr)

    detail_lines = [ln for ln in text.splitlines() if "&Перед(" in ln]
    assert detail_lines, "детализация должна включиться"
    assert len(detail_lines) <= 20, f"бюджет не глобальный: {len(detail_lines)}"
    assert "get_overrides(" in text


def test_detail_rows_grouped_under_their_extension_header(monkeypatch):
    """Finding (round-3): detail-строки идут ПОД header'ом своего расширения, а не
    собраны глобально/перемешаны. Проверяем порядок структурно: текущее расширение =
    последний встреченный header, и объект каждой detail-строки принадлежит ему.
    (Ловит регресс к flattening — там объект чужого расширения попал бы под header.)"""
    monkeypatch.setenv("RLM_EXT_OVERRIDE_DETAIL", "30")
    ctx = _ctx_main(14)
    ovr = _overrides_for(ctx, per_ext=5)  # 5 объектов/расш → несколько расширений уместятся в бюджет
    text = _extension_strategy(ctx, ovr)

    current = None
    checked = 0
    for ln in text.splitlines():
        m = re.match(r"\s*(Расш\d+): \d+ overrides", ln)
        if m:
            current = m.group(1)
            continue
        if "&Перед(" in ln:
            assert current is not None, "detail-строка раньше любого header'а расширения"
            # object_name = f'Объект{ext.name}_{j}' → имя текущего расширения обязано быть в строке
            assert current in ln, f"detail '{ln.strip()}' не под своим расширением {current}"
            checked += 1
    assert checked >= 10, f"должно проверить несколько расширений, проверено {checked}"
    # round-4: при урезании (14 расш, в бюджет влезло ~6) — ОДИН глобальный pointer,
    # а не молчаливое опускание деталей у остальных расширений.
    assert "get_overrides(" in text
    # Маркера _format_overrides_summary быть не должно — он снят в пользу pointer'а.
    assert "... and more" not in text


def test_detail_budget_global_across_annotation_kinds(monkeypatch):
    """round-8: контракт «до N detail-строк суммарно» защищаем по ВСЕМ аннотациям
    (Перед/После/Вместо/ИзменениеИКонтроль), а не только &Перед. Считаем detail-строки
    regex'ом по любой `&<Аннотация>(`."""
    monkeypatch.setenv("RLM_EXT_OVERRIDE_DETAIL", "10")
    ctx = _ctx_main(3)
    anns = ["Перед", "После", "Вместо"]
    ovr = {
        ext.path: [
            {"object_name": f"Объект{ext.name}_{j}", "annotation": anns[j % 3], "target_method": f"Метод{j}"}
            for j in range(8)  # 3 расш * 8 = 24 объекта, бюджет 10 → урезание
        ]
        for ext in ctx.nearby_extensions
    }
    text = _extension_strategy(ctx, ovr)

    detail = [ln for ln in text.splitlines() if re.search(r"&\w+\(", ln)]
    assert detail, "детализация должна включиться"
    assert len(detail) <= 10, f"глобальный бюджет нарушен: {len(detail)}"
    # присутствуют разные аннотации, не только Перед
    joined = "\n".join(detail)
    assert "&После(" in joined or "&Вместо(" in joined
    assert "get_overrides(" in text  # урезано → pointer


def test_detail_no_pointer_when_everything_fits(monkeypatch):
    """round-4: если весь detail влез в бюджет — omission НЕ помечается ложно."""
    monkeypatch.setenv("RLM_EXT_OVERRIDE_DETAIL", "500")
    ctx = _ctx_main(2)
    ovr = _overrides_for(ctx, per_ext=3)  # 2*3 = 6 объектов, далеко в пределах 500
    text = _extension_strategy(ctx, ovr)
    detail_lines = [ln for ln in text.splitlines() if "&Перед(" in ln]
    assert len(detail_lines) == 6  # всё показано
    assert "... and more" not in text  # нет маркера
    assert "Full per-object override detail on demand" not in text  # и нет pointer'а


def test_main_no_false_pointer_at_exact_boundary(monkeypatch):
    """round-5: бюджет РОВНО равен суммарному числу объектов → ничего не опущено,
    ни маркера, ни pointer'а. Ловит ложный marker _format_overrides_summary."""
    monkeypatch.setenv("RLM_EXT_OVERRIDE_DETAIL", "6")
    ctx = _ctx_main(2)
    ovr = _overrides_for(ctx, per_ext=3)  # 2*3 = 6 == budget (exact boundary)
    text = _extension_strategy(ctx, ovr)
    assert len([ln for ln in text.splitlines() if "&Перед(" in ln]) == 6  # всё показано
    assert "... and more" not in text
    assert "Full per-object override detail on demand" not in text  # НЕТ ложного pointer'а


# ── Юнит: сам _format_overrides_summary (round-5 root-cause) ─────────────────


def test_format_overrides_summary_no_marker_at_exact_boundary():
    from rlm_tools_bsl.bsl_knowledge import _format_overrides_summary

    ovr = [{"object_name": f"О{j}", "annotation": "Перед", "target_method": f"М{j}"} for j in range(3)]
    out = _format_overrides_summary(ovr, max_lines=3)
    assert len(out) == 3
    assert not any("... and more" in ln for ln in out)


def test_format_overrides_summary_marker_only_when_truly_more():
    from rlm_tools_bsl.bsl_knowledge import _format_overrides_summary

    ovr = [{"object_name": f"О{j}", "annotation": "Перед", "target_method": f"М{j}"} for j in range(5)]
    out = _format_overrides_summary(ovr, max_lines=3)
    assert any("... and more" in ln for ln in out)
    assert len([ln for ln in out if "&Перед(" in ln]) == 3  # показаны 3, маркер про остальные


def test_critical_header_preserved(monkeypatch):
    monkeypatch.delenv("RLM_EXT_OVERRIDE_DETAIL", raising=False)
    ctx = _ctx_main(2)
    text = _extension_strategy(ctx, _overrides_for(ctx, per_ext=5))
    assert "EXTENSIONS DETECTED" in text
    assert "PermissionError" in text
    assert "read_procedure" in text and "extract_procedures" in text


# ── Интеграция: полный rlm_start-путь стратегии (Finding 2) ─────────────────


@pytest.mark.strategy_mode_slim
def test_slim_strategy_bounded_with_many_extensions(monkeypatch):
    """get_strategy в slim с 14 расширениями: стратегия ограничена, без дампа."""
    monkeypatch.delenv("RLM_EXT_OVERRIDE_DETAIL", raising=False)
    ctx = _ctx_main(14)
    ovr = _overrides_for(ctx, per_ext=90)
    s = get_strategy(
        "high",
        None,
        None,
        ctx,
        ovr,
        registry=_MOCK_REGISTRY,
        idx_stats=_REALISTIC_IDX_STATS,
        query="продажи",
    )
    assert "Метод0" not in s
    # #1 (v1.28.0): без перечня префиксов и без построчных счётчиков; счётчик total +
    # указатель на nearby_extensions. Имя расширения может законно всплыть в другой прозе
    # (detected_prefixes/CUSTOM PREFIXES), поэтому проверяем ПАТТЕРНЫ, а не голое имя.
    assert re.search(r"Расш\d+: \d+ overrides", s) is None
    assert "(prefix:" not in s
    assert "EXTENSIONS DETECTED" in s
    assert "get_overrides(" in s
    assert "== HELP ==" in s  # slim-маркер на месте
    assert len(s) < 12000, f"slim-стратегия раздута расширениями: {len(s)}"


def test_full_strategy_also_bounded_with_many_extensions(monkeypatch):
    """Finding 1: бюджет применяется и в legacy full-режиме."""
    monkeypatch.setenv("RLM_STRATEGY_MODE", "full")
    monkeypatch.delenv("RLM_EXT_OVERRIDE_DETAIL", raising=False)
    ctx = _ctx_main(14)
    ovr = _overrides_for(ctx, per_ext=90)
    s = get_strategy("high", None, None, ctx, ovr, registry=_MOCK_REGISTRY, idx_stats=_REALISTIC_IDX_STATS)
    assert "Метод0" not in s
    assert "Step 0 — UNDERSTAND" in s  # full-маркер на месте
    assert "get_overrides(" in s


# ── Task 1.2: ветка ConfigRole.EXTENSION ────────────────────────────────────


def test_extension_role_own_overrides_counts_only(monkeypatch):
    monkeypatch.delenv("RLM_EXT_OVERRIDE_DETAIL", raising=False)
    ctx = SimpleNamespace(
        current=SimpleNamespace(
            role=ConfigRole.EXTENSION, name="МоёРасширение", purpose="Доработка", name_prefix="мр_"
        ),
        nearby_extensions=[],
        nearby_main=None,
    )
    ovr = {
        "self": [
            {"object_name": f"Объект{j}", "annotation": "Вместо", "target_method": f"Метод{j}"} for j in range(120)
        ]
    }
    text = _extension_strategy(ctx, ovr)
    assert "МоёРасширение" in text
    assert "120" in text  # счётчик
    assert "Метод0" not in text  # без поимённого дампа
    assert "get_overrides(" in text
    assert len(text) < 2500


def test_extension_no_false_marker_at_exact_boundary(monkeypatch):
    """round-5: budget == числу объектов расширения → всё показано, без ложного '... and more'."""
    monkeypatch.setenv("RLM_EXT_OVERRIDE_DETAIL", "4")
    ctx = SimpleNamespace(
        current=SimpleNamespace(role=ConfigRole.EXTENSION, name="Расш", purpose="", name_prefix="р_"),
        nearby_extensions=[],
        nearby_main=None,
    )
    ovr = {
        "self": [{"object_name": f"Объект{j}", "annotation": "Вместо", "target_method": f"Метод{j}"} for j in range(4)]
    }
    text = _extension_strategy(ctx, ovr)
    assert len([ln for ln in text.splitlines() if "&Вместо(" in ln]) == 4  # все 4 показаны
    assert "... and more" not in text


# ── Task 1.3: e2e на реальной CF+CFE фикстуре (через _rlm_start) ─────────────


def test_e2e_main_with_extension_pointer_no_counts(tmp_path, monkeypatch):
    """e2e: реальные CF main + CFE расширение через _rlm_start — расширение
    детектится, но по умолчанию (#1, v1.28.0) в стратегии ТОЛЬКО счётчик total +
    указатель на nearby_extensions: ни построчных счётчиков overrides, ни поимённого дампа.
    Fixture перехватывает &После("ОбработкаЗаполнения") и &Вместо("ПередЗаписью")."""
    from test_extension_overrides import _make_main_with_extension
    from rlm_tools_bsl.server import _rlm_start, _rlm_end

    monkeypatch.delenv("RLM_EXT_OVERRIDE_DETAIL", raising=False)
    cf, _cfe = _make_main_with_extension(tmp_path)

    resp = json.loads(_rlm_start(path=cf, query="перехваты расширения"))
    assert "error" not in resp, resp
    sid = resp["session_id"]
    try:
        s = resp["strategy"]
        assert "EXTENSIONS DETECTED" in s
        assert "1 EXTENSIONS DETECTED" in s  # total (1 расширение) в заголовке
        assert "nearby_extensions" in s  # указатель на структурный источник
        assert re.search(r": \d+ overrides", s) is None  # НЕТ построчных счётчиков
        # Без поимённого дампа по умолчанию. ВАЖНО: сами имена методов
        # (ОбработкаЗаполнения/ПередЗаписью) встречаются в boilerplate-примерах
        # стратегии, поэтому проверяем именно ДАМП-формат &Аннотация("Метод") —
        # точный сигнал утечки перехватов, а не голое имя.
        assert "&После(" not in s and "&Вместо(" not in s
        assert '&После("ОбработкаЗаполнения")' not in s
        assert '&Вместо("ПередЗаписью")' not in s
        assert "get_overrides(" in s  # pointer на детали
    finally:
        _rlm_end(sid)


def test_e2e_extension_detail_opt_in(tmp_path, monkeypatch):
    """e2e: при RLM_EXT_OVERRIDE_DETAIL>0 реальные перехваты показаны."""
    from test_extension_overrides import _make_main_with_extension
    from rlm_tools_bsl.server import _rlm_start, _rlm_end

    monkeypatch.setenv("RLM_EXT_OVERRIDE_DETAIL", "50")
    cf, _cfe = _make_main_with_extension(tmp_path)

    resp = json.loads(_rlm_start(path=cf, query="перехваты расширения"))
    assert "error" not in resp, resp  # полезная диагностика вместо KeyError при регрессе старта
    sid = resp["session_id"]
    try:
        s = resp["strategy"]
        # дамп-формат показан (точный сигнал, а не голые имена из boilerplate):
        assert '&После("ОбработкаЗаполнения")' in s
        assert '&Вместо("ПередЗаписью")' in s
    finally:
        _rlm_end(sid)
