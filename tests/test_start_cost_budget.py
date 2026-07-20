"""v1.23.0 — start-cost budget guard for rlm_start.

The strategy / recipe / tool-description edits in this release are formulation
REPLACEMENTS (not bulk additions); the new get_object_profile signature + the
extended rlm_start.index fields are the only intended growth. This test pins the
whole-strategy payload (slim AND full, with/without a business recipe) to the
v1.23.0 baselines and fails if a future edit grows any case by more than ~5%.

Baselines are deterministic: the strategy is built from the FROZEN helper-metadata
snapshot (build_helper_metadata_snapshot force-registers git_search), so the numbers
do not depend on git availability or the live registry.
"""

from __future__ import annotations

import json
import os
import tempfile

import pytest

from rlm_tools_bsl.bsl_helpers import build_helper_metadata_snapshot
from rlm_tools_bsl.bsl_knowledge import get_strategy
from rlm_tools_bsl.format_detector import detect_format

# Baselines (chars), measured with the frozen snapshot registry.
#
# v1.28.0 re-baseline (INTENTIONAL — per this test's own guidance). The v1.23.0 numbers had
# been eaten to 99.2–99.7% of ceiling by v1.28.0 release A, i.e. the +5% guard was already
# exhausted BEFORE this change and could no longer absorb any new contract text. The growth
# here is the agent-facing contract of the v1.28.0 fixes — without it the fixes are invisible
# to the agent (aggregate keys nobody reads == the bug we just fixed):
#   * find_event_subscriptions  → scope=exact|partial|universal, category-aware 'Документ.X'
#   * get_overrides             → by_annotation / by_object_top / by_extension_top / unique_*
#   * find_register_movements   → posting_handler_present + hint
#   * find_functional_options   → limit= (per-bucket cap)
# The prose was trimmed first (helper `sig` strings shrank 388/319/568 → 328/256/320 chars, and
# the long explanations moved from the BUDGETED `sig` into the unbudgeted `recipe`, which
# rlm_help serves on demand). What remains is irreducible without deleting the key names.
#
# NB the DEFAULT start path did not grow at all: slim/"" is byte-for-byte what release A
# emitted (7146) — Step 4/5 + performance strategy lines live in sections that slim serves via
# rlm_help, not inline. Growth is confined to full mode and to a query-matched recipe, i.e. it
# is paid only when the agent actually asked about that topic.
# Re-baselining to the measured values restores a real +5% margin for the next edit.
_BASELINES = {
    ("slim", ""): 7146,
    ("slim", "проведение"): 7990,
    ("full", ""): 30063,
    ("full", "проведение"): 31555,
}
# Whole rlm_start payload baselines (strategy + available_functions + index +
# extension_context) for a fixed minimal INDEXED config — the plan's real target.
# v1.26.0 re-baseline (intentional growth, per this test's own guidance): the new
# index.index_status machine-contract key (+~22 chars) and the find_files "instant on
# index-hit, else FS-fallback" hint update. Restores the +5% margin (the v1.23.0
# baseline sat at ~99% of ceiling, so the documented order-dependent extension-leak
# flakiness could tip it once the margin shrank).
# v1.28.0 re-baseline, same reasoning as _BASELINES above (see there). This payload also
# carries available_functions, i.e. every helper `sig` — after the sig trim it was back under
# the old ceiling on its own, but at 98–99% of it; re-baselining restores the +5% margin so the
# next edit trips the guard on its own merits rather than on inherited saturation.
_PAYLOAD_BASELINES = {"slim": 20691, "full": 43482}
_DRIFT = 1.05  # allow ≤5% growth before failing

_IDX_STATS = {
    "methods": 1000,
    "calls": 500,
    "config_name": "X",
    "config_version": "1.0",
    "has_fts": True,
    "object_synonyms": 10,
    "builder_version": "14",
    "has_metadata": True,
}


@pytest.fixture(scope="module")
def _fmt_info():
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "Configuration.xml"), "w") as f:
            f.write("<Configuration/>")
        yield detect_format(d)


@pytest.mark.parametrize("mode,query", list(_BASELINES))
def test_strategy_payload_within_budget(_fmt_info, monkeypatch, mode, query):
    monkeypatch.setenv("RLM_STRATEGY_MODE", mode)
    snap = build_helper_metadata_snapshot()
    text = get_strategy("high", _fmt_info, registry=snap, idx_stats=_IDX_STATS, query=query)
    baseline = _BASELINES[(mode, query)]
    ceiling = int(baseline * _DRIFT)
    assert len(text) <= ceiling, (
        f"{mode}/{query or '(none)'} strategy grew to {len(text)} chars "
        f"(> {ceiling} = baseline {baseline} +5%). Trim, or re-baseline intentionally."
    )


def test_get_object_profile_signature_stays_compact():
    """The new sig appears in available_functions + the strategy helpers table — keep it lean."""
    snap = build_helper_metadata_snapshot()
    sig = snap["get_object_profile"]["sig"]
    assert len(sig) <= 525, f"get_object_profile sig is {len(sig)} chars — trim to stay in budget"


def test_helper_snapshot_count_locked():
    """Adding/removing a registered helper is an intentional change — update this number."""
    assert len(build_helper_metadata_snapshot()) == 53


@pytest.mark.parametrize("mode", ["slim", "full"])
def test_full_rlm_start_payload_within_budget(monkeypatch, tmp_path, mode):
    """The WHOLE rlm_start payload (strategy + available_functions + index + extension_context),
    not just the strategy, stays within +5% of the v1.23.0 baseline — so a future edit cannot
    silently balloon available_functions or the index block (R7 #4/#5)."""
    import rlm_tools_bsl.extension_detector as _ed
    from rlm_tools_bsl.bsl_index import IndexBuilder
    from rlm_tools_bsl.server import _rlm_end, _rlm_start

    obj = tmp_path / "Documents" / "БюджетТест" / "Ext"
    obj.mkdir(parents=True)
    (obj / "ObjectModule.bsl").write_text("Процедура П() Экспорт\nКонецПроцедуры\n", encoding="utf-8")
    (tmp_path / "Configuration.xml").write_text("<Configuration/>", encoding="utf-8")
    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / ".idx"))
    monkeypatch.setenv("RLM_STRATEGY_MODE", mode)
    IndexBuilder().build(str(tmp_path), build_calls=False, build_metadata=True)

    # The baseline is the NO-extension start cost. detect_extension_context scans sibling /
    # grandparent dirs for extensions, so under pytest's shared tmp tree it can pick up OTHER
    # tests' extension fixtures and inject the "EXTENSIONS DETECTED" block — making the budget
    # ordering-dependent. Force a clean context (real current role, no nearby extensions).
    _real_single = _ed._detect_single

    def _clean_ctx(p):
        cur = _real_single(p) or _ed.ExtensionInfo(path=p, role=_ed.ConfigRole.UNKNOWN)
        return _ed.ExtensionContext(current=cur, nearby_extensions=[], nearby_main=None, warnings=[])

    monkeypatch.setattr("rlm_tools_bsl.server.detect_extension_context", _clean_ctx)

    raw = _rlm_start(path=str(tmp_path), query="")
    data = json.loads(raw)
    try:
        assert not data["extension_context"]["nearby_extensions"], "budget config must be extension-free"
        ceiling = int(_PAYLOAD_BASELINES[mode] * _DRIFT)
        assert len(raw) <= ceiling, (
            f"{mode} rlm_start payload {len(raw)} > {ceiling} (+5% of {_PAYLOAD_BASELINES[mode]}). "
            "available_functions / index / strategy grew — trim or re-baseline intentionally."
        )
        # the new aggregate signature lives on available_functions — confirm it is present
        assert any("get_object_profile(name" in s for s in data["available_functions"])
        # index discovery keys present so the agent skips get_index_info() on start
        assert data["index"]["loaded"] is True
        assert "has_object_attributes" in data["index"]
    finally:
        _rlm_end(data["session_id"])
