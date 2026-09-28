"""Tests for `_build_slim_strategy` (RLM_STRATEGY_MODE=slim).

All tests in this file are pinned to slim mode via the
``strategy_mode_slim`` marker (see tests/conftest.py).
"""

from __future__ import annotations

import pytest

from rlm_tools_bsl.bsl_knowledge import get_strategy
from _strategy_fixtures import _MOCK_REGISTRY, _REALISTIC_IDX_STATS


pytestmark = pytest.mark.strategy_mode_slim


# ── Token / size budget on representative inputs ────────────────────────────
#
# v1.41.0: верхние границы опущены до ceil10(факт × 1,10) — из slim ушли compact index и
# строка INSTANT, HELP переписан, добавлен блок доменов; прежняя полоса «< 9000»
# позволила бы следующей правке молча съесть освобождённые ~3 тыс. символов.


def test_slim_size_baseline():
    s = get_strategy("medium", None)
    # Slim strategy with no registry / no idx_stats is still substantive
    # (HELP block, workflow pointer, batching, no-index INDEX block, effort).
    assert 800 < len(s) <= 4990, f"baseline length out of expected band: {len(s)}"


def test_slim_size_realistic_high():
    s = get_strategy(
        "high",
        None,
        registry=_MOCK_REGISTRY,
        idx_stats=_REALISTIC_IDX_STATS,
    )
    assert 800 < len(s) <= 6670, f"realistic length out of expected band: {len(s)}"


def test_slim_size_with_recipe():
    s = get_strategy(
        "high",
        None,
        registry=_MOCK_REGISTRY,
        idx_stats=_REALISTIC_IDX_STATS,
        query="себестоимость",
    )
    assert 800 < len(s) <= 7390, len(s)


# ── Slim-only markers ───────────────────────────────────────────────────────


def test_slim_contains_help_block():
    s = get_strategy("medium", None)
    assert "== HELP ==" in s
    assert "rlm_help" in s


def test_slim_contains_workflow_overview_but_not_full_steps():
    s = get_strategy("medium", None)
    assert "== WORKFLOW (overview) ==" in s
    # Detailed Step 0..5 lives behind rlm_help(section='workflow') — must NOT
    # be inlined in slim strategy.
    assert "Step 0 — UNDERSTAND" not in s
    assert "Step 4 — ANALYZE" not in s


def test_slim_contains_disambiguation_pointer_not_full_block():
    s = get_strategy("medium", None)
    assert "== DISAMBIGUATION (pointer) ==" in s
    # Full DISAMBIGUATION block has rule lines like this — they must be absent:
    assert "structure-only vs structure+modules" not in s
    # The pointer mentions some pair names so the agent knows what kinds of
    # comparisons exist.
    assert "find_callers_context" in s


def test_slim_contains_helper_domains_block():
    """v1.41.0: compact index (имена по категориям) заменён блоком доменов хелперов —
    подписи выбранных доменов уже в available_functions, имена всех доменов — в описании
    параметра rlm_start.domains."""
    s = get_strategy("medium", None, registry=_MOCK_REGISTRY)
    assert "== HELPERS (compact index" not in s
    assert "== HELPERS (call help" not in s
    assert "== ДОМЕНЫ ХЕЛПЕРОВ ==" in s
    # Без выбора (прямой вызов) — «только ядро»: ключи доменов и маршрут догрузки.
    assert "Загружено только ядро" in s
    assert "документ | структура | код | связи | расширения | поиск | весь каталог" in s
    assert "rlm_execute" in s and "signatures" in s


def test_slim_recipe_is_compact_even_on_high_effort():
    s = get_strategy(
        "high",
        None,
        registry=_MOCK_REGISTRY,
        idx_stats=_REALISTIC_IDX_STATS,
        query="себестоимость",
    )
    assert "== BUSINESS RECIPE: себестоимость ==" in s
    # Slim always uses compact; "find_register_writers" is a step in the
    # 'compact' себестоимость recipe.
    assert "find_register_writers" in s
    # Pointer to full version via rlm_help.
    assert "rlm_help(topic='себестоимость', format='full')" in s


# ── Dynamic blocks remain in place ──────────────────────────────────────────


def test_slim_keeps_index_block():
    s = get_strategy("high", None, registry=_MOCK_REGISTRY, idx_stats=_REALISTIC_IDX_STATS)
    assert "== INDEX ==" in s
    assert "Index v12" in s


def test_slim_keeps_effort_and_format():
    from types import SimpleNamespace

    fmt = SimpleNamespace(format_label="cf")
    s = get_strategy(
        "high",
        fmt,
        detected_prefixes=["рлф"],
        registry=_MOCK_REGISTRY,
    )
    assert "== EFFORT: high ==" in s
    assert "== FORMAT: CF ==" in s
    assert "== CUSTOM PREFIXES: ['рлф'] ==" in s


def test_slim_mentions_in_sandbox_help():
    """Sandbox `help('keyword')` must remain mentioned in slim strategy
    (separate channel from the rlm_help MCP tool — both exist)."""
    s = get_strategy("medium", None, registry=_MOCK_REGISTRY)
    # BATCHING block carries the "Call help('keyword') for code recipes" line.
    assert "help('keyword')" in s or "help('exports')" in s
