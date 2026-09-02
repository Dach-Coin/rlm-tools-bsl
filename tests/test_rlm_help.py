"""Tests for ``rlm_help`` MCP-tool dispatcher (mode router + JSON shapes).

Calls ``_rlm_help_dispatch`` directly — no FastMCP / no live session needed.
The tool wrapper in server.py is a thin Annotated/Field shim around it.
"""

from __future__ import annotations

import json

import pytest


@pytest.fixture
def dispatch():
    """Return a callable that invokes _rlm_help_dispatch and returns parsed JSON."""
    from rlm_tools_bsl.server import _rlm_help_dispatch

    def _call(**kwargs) -> dict:
        return json.loads(_rlm_help_dispatch(**kwargs))

    return _call


# ── Mode 1: menu ────────────────────────────────────────────────────────────


def test_menu_no_args(dispatch):
    from rlm_tools_bsl.bsl_helpers import build_helper_metadata_snapshot

    res = dispatch()
    assert res["mode"] == "menu"
    assert res["warnings"] == []
    result = res["result"]
    assert "available_topics" in result
    assert "available_categories" in result
    assert "available_sections" in result
    assert result["helpers_count"] == len(build_helper_metadata_snapshot())
    assert result["helpers_count"] > 30  # there are 45+ helpers as of v1.10.0


# ── Mode 2: topic ───────────────────────────────────────────────────────────


def test_topic_compact(dispatch):
    res = dispatch(topic="проведение")
    assert res["mode"] == "topic"
    assert res["warnings"] == []
    r = res["result"]
    assert r["topic"] == "проведение"
    assert r["format"] == "compact"
    assert r["steps"]
    # v1.23.0: the проведение compact branch leads with the one-call aggregate.
    assert any("get_object_profile" in s for s in r["steps"])


def test_topic_full_with_code(dispatch):
    res = dispatch(topic="ссылки", format="full", include_code=True)
    r = res["result"]
    assert r["format"] == "full"
    assert "code_hint" in r
    assert "find_references_to_object" in r["code_hint"]


def test_topic_full_without_code(dispatch):
    res = dispatch(topic="ссылки", format="full", include_code=False)
    r = res["result"]
    assert "code_hint" not in r


def test_topic_alias_resolves_to_canonical(dispatch):
    # 'обмен' is an alias of 'интеграция' (in _RECIPE_ALIASES)
    res = dispatch(topic="обмен")
    assert res["result"]["topic"] == "интеграция"


def test_topic_call_hierarchy_recipe(dispatch):
    """New v1.11.x domain — find_call_hierarchy has its own recipe."""
    res = dispatch(topic="иерархия вызовов")
    assert res["mode"] == "topic"
    r = res["result"]
    assert r["topic"] == "иерархия вызовов"
    assert r["steps"]
    assert any("find_call_hierarchy" in s for s in r["steps"])


def test_topic_extensions_recipe(dispatch):
    """New v1.11.x domain — get_overrides / extension perехваты have own recipe."""
    res = dispatch(topic="расширения", format="full", include_code=True)
    assert res["mode"] == "topic"
    r = res["result"]
    assert r["topic"] == "расширения"
    assert r["steps"]
    assert any("get_overrides" in s for s in r["steps"])
    assert any("overridden_by" in s for s in r["steps"])
    assert "code_hint" in r and "get_overrides" in r["code_hint"]


@pytest.mark.parametrize(
    ("alias", "canonical"),
    [
        # «иерархия вызовов» — все варианты, которые могут спросить агенты
        ("иерархия", "иерархия вызовов"),
        ("call hierarchy", "иерархия вызовов"),
        ("callers", "иерархия вызовов"),
        ("кто вызывает", "иерархия вызовов"),
        ("цепочка вызовов", "иерархия вызовов"),
        # «интеграция» — расширенные алиасы (HTTP/SOAP/XDTO/REST/МЭДО)
        ("http", "интеграция"),
        ("http-сервис", "интеграция"),
        ("веб-сервис", "интеграция"),
        ("soap", "интеграция"),
        ("rest", "интеграция"),
        ("xdto", "интеграция"),
        ("планы обмена", "интеграция"),
        ("мэдо", "интеграция"),
        # «проведение» — события документа + регистры
        ("движения по регистрам", "проведение"),
        ("регистры", "проведение"),
        ("регистры накопления", "проведение"),
        ("события документа", "проведение"),
        ("обработчики событий", "проведение"),
        ("ПередЗаписью", "проведение"),
        # «права» — RLS + функциональные опции
        ("rls", "права"),
        ("ограничение доступа", "права"),
        ("права доступа", "права"),
        ("функциональные опции", "права"),
        # «структура объекта» — справочники + регистры
        ("структура справочника", "структура объекта"),
        ("структура регистра", "структура объекта"),
        ("табличные части", "структура объекта"),
        ("реквизиты", "структура объекта"),
        # «расширения» — перехваты / overrides / аннотации
        ("перехват", "расширения"),
        ("перехваты", "расширения"),
        ("override", "расширения"),
        ("overrides", "расширения"),
        ("extension", "расширения"),
        ("extensions", "расширения"),
        ("ext_overrides", "расширения"),
        ("аннотации", "расширения"),
        ("&Перед", "расширения"),
        ("&После", "расширения"),
        ("&Вместо", "расширения"),
    ],
)
def test_topic_extended_aliases(dispatch, alias, canonical):
    """Verify all aliases added during v1.11.0 e2e analysis route correctly."""
    res = dispatch(topic=alias)
    assert res["mode"] == "topic"
    assert "error" not in res["result"], f"alias {alias!r} did not resolve"
    assert res["result"]["topic"] == canonical, (
        f"alias {alias!r} → expected {canonical!r}, got {res['result']['topic']!r}"
    )


def test_topic_unknown_returns_suggestions(dispatch):
    res = dispatch(topic="посаждение")  # close to 'проведение' / 'распределение'
    r = res["result"]
    assert r["error"] == "unknown"
    assert isinstance(r["suggestions"], list)


def test_topic_with_helpers_emits_warning(dispatch):
    res = dispatch(topic="проведение", helpers=["foo"])
    assert res["mode"] == "topic"
    assert any("helpers" in w for w in res["warnings"])


# ── Mode 3: section='disambiguation' (structured) ──────────────────────────


def test_disambiguation_full(dispatch):
    res = dispatch(section="disambiguation")
    assert res["mode"] == "disambiguation"
    assert isinstance(res["result"], list)
    assert len(res["result"]) == 12


def test_disambiguation_filter_by_helpers(dispatch):
    res = dispatch(section="disambiguation", helpers=["find_callers", "find_callers_context"])
    assert res["mode"] == "disambiguation"
    pairs = res["result"]
    assert len(pairs) == 1
    a, b = pairs[0]["pair"]
    assert {a, b} == {"find_callers", "find_callers_context"}


def test_disambiguation_with_category_emits_warning(dispatch):
    res = dispatch(section="disambiguation", category="xml")
    assert res["mode"] == "disambiguation"
    assert any("category" in w for w in res["warnings"])


# ── Mode 4: section ─────────────────────────────────────────────────────────


def test_section_workflow(dispatch):
    res = dispatch(section="workflow")
    assert res["mode"] == "section"
    text = res["result"]["text"]
    assert "Step 0 — UNDERSTAND" in text
    assert "Step 5 — EXTENSIONS" in text


def test_section_coverage(dispatch):
    """Легенда осей охвата (v1.34.0) — единственное место, где они описаны целиком."""
    res = dispatch(section="coverage")
    assert res["mode"] == "section"
    text = res["result"]["text"]
    assert "== COVERAGE" in text
    assert "index_coverage" in text and "extensions_included" in text


def test_menu_points_to_the_coverage_section(dispatch):
    """Меню обязано вести к легенде: иначе о ней узнает только тот, кто уже знает."""
    res = dispatch()
    assert "coverage" in res["result"]["available_sections"]
    assert "section='coverage'" in res["result"]["hint"]


def test_section_performance(dispatch):
    res = dispatch(section="performance")
    text = res["result"]["text"]
    assert "INSTANT" in text
    assert "HYBRID" in text
    assert "LIVE" in text


def test_section_io(dispatch):
    res = dispatch(section="io")
    assert "File I/O" in res["result"]["text"]


# ── Mode 5: helpers ─────────────────────────────────────────────────────────


def test_helpers_known(dispatch):
    res = dispatch(helpers=["find_callers_context"])
    assert res["mode"] == "helpers"
    items = res["result"]
    assert len(items) == 1
    h = items[0]
    assert h["name"] == "find_callers_context"
    assert h["sig"]
    assert h["category"] == "code"
    assert "kw" in h
    assert "recipe" in h


def test_helpers_unknown_returns_error_with_suggestions(dispatch):
    res = dispatch(helpers=["nonexistent_helper"])
    items = res["result"]
    assert items[0]["name"] == "nonexistent_helper"
    assert items[0]["error"] == "unknown"
    assert isinstance(items[0]["suggestions"], list)


def test_helpers_with_category_filter(dispatch):
    # parse_form is in xml category; find_module is NOT.
    # Plan contract: result contains ONLY helpers matching category;
    # mismatches are silently dropped from result and recorded in warnings.
    res = dispatch(helpers=["parse_form", "find_module"], category="xml")
    items = res["result"]
    names = [h["name"] for h in items]
    assert names == ["parse_form"]
    # Single matched item is normal helper-details, no error sentinel.
    assert items[0].get("error") is None
    assert items[0]["category"] == "xml"
    # Caller is told about the drop via warnings.
    assert any("find_module" in w and "xml" in w for w in res["warnings"])


# ── Mode 6: category ────────────────────────────────────────────────────────


def test_category_xml(dispatch):
    res = dispatch(category="xml")
    assert res["mode"] == "category"
    helpers = res["result"]["helpers"]
    names = {h["name"] for h in helpers}
    assert "parse_object_xml" in names
    assert "parse_form" in names
    # No recipes leaked in this mode — only name+sig.
    for h in helpers:
        assert set(h.keys()) == {"name", "sig"}


def test_category_unknown(dispatch):
    res = dispatch(category="nonexistent")
    r = res["result"]
    assert r["error"] == "unknown"
    assert "available" in r


# ── Warnings shape ──────────────────────────────────────────────────────────


def test_warnings_always_a_list(dispatch):
    # Every mode must include `warnings: list[str]` (empty when no conflicts).
    for kwargs in (
        {},
        {"topic": "проведение"},
        {"section": "workflow"},
        {"category": "xml"},
        {"helpers": ["find_module"]},
    ):
        res = dispatch(**kwargs)
        assert isinstance(res["warnings"], list)


# ── Smoke: dispatcher works without a live session ─────────────────────────


def test_no_session_required(dispatch):
    # Just exercise once more — entire dispatcher path is pure / static
    # snapshot-backed.
    assert dispatch()["mode"] == "menu"


# ── v1.23.0 tripwires: get_object_profile routing + recipe-first ────────────


def test_get_object_profile_registered():
    from rlm_tools_bsl.bsl_helpers import build_helper_metadata_snapshot

    snap = build_helper_metadata_snapshot()
    assert "get_object_profile" in snap
    assert snap["get_object_profile"]["cat"] == "composite"
    assert "get_object_profile(name" in snap["get_object_profile"]["sig"]


def test_profile_keywords_route_to_get_object_profile():
    """profile/профиль/overview/обзор own get_object_profile, NOT analyze_object (R2 #3)."""
    from rlm_tools_bsl.bsl_helpers import build_helper_metadata_snapshot

    snap = build_helper_metadata_snapshot()
    for kw in ("profile", "профиль", "обзор", "overview"):
        owners = [name for name, meta in snap.items() if kw in (meta.get("kw") or [])]
        assert owners == ["get_object_profile"], (kw, owners)
    assert "profile" not in snap["analyze_object"]["kw"]
    assert "overview" not in snap["analyze_object"]["kw"]


def test_business_recipes_lead_with_get_object_profile():
    """проведение/себестоимость/распределение/права/структура объекта compact branch
    references get_object_profile (R5 #2)."""
    from rlm_tools_bsl.bsl_knowledge import _BUSINESS_RECIPES

    for domain in ("проведение", "себестоимость", "распределение", "права", "структура объекта"):
        compact = " ".join(_BUSINESS_RECIPES[domain]["compact"])
        assert "get_object_profile" in compact, domain
