"""Part 4: дедупликация/усечение списка расширений на старте (threshold-gated top-N).

Покрывает три представления-для-агента (warnings / response-поле / strategy-текст),
которые на extreme-extension конфигах (напр. 155 расш) раздували rlm_start выше
токен-лимита. Внутренний список объектов ExtensionInfo и питание песочницы — НЕ режем.
"""

from __future__ import annotations

import json
import os
import re
from types import SimpleNamespace

import pytest

from rlm_tools_bsl.bsl_knowledge import summarize_extensions_by_overrides
from rlm_tools_bsl.extension_detector import (
    ConfigRole,
    ExtensionInfo,
    _build_warnings,
    _ext_list_cap,
)

from test_strategy_ext_budget import _ctx_main, _overrides_for


def _main_info():
    return ExtensionInfo(path="/tmp/cf", role=ConfigRole.MAIN, name="Основная")


def _ext_infos(n: int):
    return [
        ExtensionInfo(
            path=f"/tmp/cfe/Ext{i:03d}",
            role=ConfigRole.EXTENSION,
            name=f"Расш{i}",
            purpose="Customization",
            name_prefix=f"р{i}_",
        )
        for i in range(n)
    ]


# ── cap-ридер (RLM_EXT_LIST_CAP) ────────────────────────────────────────────


def test_cap_reader_default_is_20(monkeypatch):
    monkeypatch.delenv("RLM_EXT_LIST_CAP", raising=False)
    assert _ext_list_cap() == 20


def test_cap_reader_explicit_positive(monkeypatch):
    monkeypatch.setenv("RLM_EXT_LIST_CAP", "5")
    assert _ext_list_cap() == 5


def test_cap_reader_zero_means_no_limit(monkeypatch):
    monkeypatch.setenv("RLM_EXT_LIST_CAP", "0")
    assert _ext_list_cap() == 0


def test_cap_reader_negative_means_no_limit(monkeypatch):
    """F4: негативное значение тоже «без лимита» (защита от регресса к positive-only)."""
    monkeypatch.setenv("RLM_EXT_LIST_CAP", "-1")
    assert _ext_list_cap() == -1


def test_cap_reader_garbage_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("RLM_EXT_LIST_CAP", "abc")
    assert _ext_list_cap() == 20


def test_cap_reader_empty_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("RLM_EXT_LIST_CAP", "   ")
    assert _ext_list_cap() == 20


# ── summarize_extensions_by_overrides (юнит) ────────────────────────────────


def test_summarize_full_when_total_within_cap():
    ctx = _ctx_main(5)
    ovr = _overrides_for(ctx, per_ext=3)
    shown, total, n_shown = summarize_extensions_by_overrides(ctx.nearby_extensions, ovr, cap=20)
    assert total == 5 and n_shown == 5
    assert [e.name for e in shown] == [e.name for e in ctx.nearby_extensions]  # исходный порядок


def test_summarize_full_at_exact_boundary():
    ctx = _ctx_main(20)
    ovr = _overrides_for(ctx, per_ext=1)
    shown, total, n_shown = summarize_extensions_by_overrides(ctx.nearby_extensions, ovr, cap=20)
    assert total == 20 and n_shown == 20  # total==cap → full, не усекаем


def test_summarize_full_when_cap_zero():
    ctx = _ctx_main(30)
    ovr = _overrides_for(ctx, per_ext=2)
    shown, total, n_shown = summarize_extensions_by_overrides(ctx.nearby_extensions, ovr, cap=0)
    assert total == 30 and n_shown == 30  # cap<=0 → без лимита


def test_summarize_full_when_cap_negative():
    ctx = _ctx_main(30)
    ovr = _overrides_for(ctx, per_ext=2)
    shown, total, n_shown = summarize_extensions_by_overrides(ctx.nearby_extensions, ovr, cap=-1)
    assert total == 30 and n_shown == 30


def test_summarize_truncates_to_cap_sorted_by_overrides_desc():
    ctx = _ctx_main(25)
    # Разное число overrides: Ext{i} получает i overrides → больше у больших i.
    ovr = {
        ext.path: [{"object_name": f"О{j}", "annotation": "Перед", "target_method": f"М{j}"} for j in range(i)]
        for i, ext in enumerate(ctx.nearby_extensions)
    }
    shown, total, n_shown = summarize_extensions_by_overrides(ctx.nearby_extensions, ovr, cap=20)
    assert total == 25 and n_shown == 20
    assert len(shown) == 20
    counts = [len(ovr.get(e.path, [])) for e in shown]
    assert counts == sorted(counts, reverse=True)  # отсортировано по overrides убыв.
    # Расширение с 0 overrides (Ext0) обязано уйти в хвост → не показано.
    assert all(len(ovr.get(e.path, [])) > 0 for e in shown)


def test_summarize_does_not_mutate_input():
    """F8.1: возвращает НОВЫЙ список и не мутирует вход (ловит in-place sort/усечение)."""
    ctx = _ctx_main(25)
    ovr = _overrides_for(ctx, per_ext=4)
    original_paths = [e.path for e in ctx.nearby_extensions]
    shown, _total, _n = summarize_extensions_by_overrides(ctx.nearby_extensions, ovr, cap=20)
    assert shown is not ctx.nearby_extensions
    assert len(ctx.nearby_extensions) == 25  # вход не усечён
    assert [e.path for e in ctx.nearby_extensions] == original_paths  # порядок входа не тронут


def test_summarize_deterministic_with_equal_overrides_and_dup_empty_names():
    """F2: равные overrides + дубль/пустые имена → top-N стабилен по normcase(path),
    не зависит от порядка iterdir(). Прогон дважды на перемешанном входе идентичен."""
    # 25 расширений: одинаковое число overrides (по 5), имена дублируются и пустые.
    base = [
        SimpleNamespace(name=("" if i % 3 == 0 else "Дубль"), name_prefix="", path=f"/tmp/cfe/Z{i:02d}", purpose="")
        for i in range(25)
    ]
    ovr = {e.path: [{"object_name": "О", "annotation": "Перед", "target_method": "М"}] * 5 for e in base}

    shuffled_a = [
        base[k] for k in [7, 0, 22, 3, 14, 1, 19, 5, 11, 24, 2, 8, 17, 6, 20, 4, 23, 9, 13, 10, 16, 21, 12, 18, 15]
    ]
    shuffled_b = list(reversed(base))

    shown_a, _, _ = summarize_extensions_by_overrides(shuffled_a, ovr, cap=20)
    shown_b, _, _ = summarize_extensions_by_overrides(shuffled_b, ovr, cap=20)
    assert [e.path for e in shown_a] == [e.path for e in shown_b]  # порядок входа не влияет
    # И ровно top-20 по полному тай-брейку (name, prefix, normcase(path)) при равных overrides.
    expected = sorted(base, key=lambda e: (e.name or "", e.name_prefix or "", os.path.normcase(e.path)))[:20]
    assert [e.path for e in shown_a] == [e.path for e in expected]


# ── Site 1: _build_warnings (MAIN ветка) ────────────────────────────────────


def test_warnings_truncated_above_cap(monkeypatch):
    """N>cap → один короткий контекст-нейтральный warning со счётчиком и
    указателем detect_extensions(), БЕЗ конкатенации всех путей."""
    monkeypatch.setenv("RLM_EXT_LIST_CAP", "20")
    warnings = _build_warnings(_main_info(), _ext_infos(30), None)
    assert len(warnings) == 1
    w = warnings[0]
    assert "30" in w  # число расширений
    assert "detect_extensions()" in w
    assert "ChangeAndValidate" in w  # аннотации сохранены (контракт test_warnings_main_with_extensions)
    # НЕ должно быть дампа путей расширений.
    assert "/tmp/cfe/Ext" not in w
    assert "р0_" not in w and "Расш0" not in w


def test_warnings_full_at_cap(monkeypatch):
    """N==cap → полный warning с именем конкретного расширения (байт-в-байт как раньше)."""
    monkeypatch.setenv("RLM_EXT_LIST_CAP", "20")
    warnings = _build_warnings(_main_info(), _ext_infos(20), None)
    assert len(warnings) == 1
    w = warnings[0]
    assert "Расш0" in w and "Расш19" in w  # полный список имён
    assert "/tmp/cfe/Ext000" in w  # путь конкретного расширения присутствует
    assert "ChangeAndValidate" in w


def test_warnings_disabled_cap_keeps_full(monkeypatch):
    """RLM_EXT_LIST_CAP=0 → полный warning даже при большом N."""
    monkeypatch.setenv("RLM_EXT_LIST_CAP", "0")
    warnings = _build_warnings(_main_info(), _ext_infos(30), None)
    assert "Расш29" in warnings[0]
    assert "/tmp/cfe/Ext029" in warnings[0]


# ── Site 3: _extension_strategy (MAIN ветка) ────────────────────────────────


def _overrides_by_index(ctx):
    """Ext{i} получает ровно i overrides → детерминированный top-N по overrides."""
    return {
        ext.path: [{"object_name": f"О{j}", "annotation": "Перед", "target_method": f"М{j}"} for j in range(i)]
        for i, ext in enumerate(ctx.nearby_extensions)
    }


def _shown_count_headers(text):
    return set(re.findall(r"(Расш\d+): \d+ overrides", text))


def test_strategy_main_truncates_to_top_n(monkeypatch):
    from rlm_tools_bsl.bsl_knowledge import _extension_strategy

    monkeypatch.setenv("RLM_EXT_LIST_CAP", "20")
    ctx = _ctx_main(25)
    ovr = _overrides_by_index(ctx)  # Ext0=0 … Ext24=24 overrides
    text = _extension_strategy(ctx, ovr)

    # #1 (v1.28.0): по умолчанию (budget==0) заголовок — счётчик total + указатель на
    # nearby_extensions (с маркерами усечения), БЕЗ перечня префиксов, БЕЗ построчных
    # счётчиков и БЕЗ прозы hidden-summary (усечение несёт структурный nearby_extensions).
    assert "EXTENSIONS DETECTED" in text
    assert "25 EXTENSIONS DETECTED" in text  # total (не top-N) в заголовке
    assert "nearby_extensions" in text
    assert "(prefix:" not in text
    assert _shown_count_headers(text) == set()  # без построчных счётчиков
    assert "more extensions" not in text  # без прозы hidden-summary
    assert "get_overrides(" in text


def test_strategy_main_no_truncation_at_cap(monkeypatch):
    """exact boundary N==cap → весь список, без +more и без сводной строки."""
    from rlm_tools_bsl.bsl_knowledge import _extension_strategy

    monkeypatch.setenv("RLM_EXT_LIST_CAP", "20")
    ctx = _ctx_main(20)
    text = _extension_strategy(ctx, _overrides_for(ctx, per_ext=3))
    assert "more (detect_extensions())" not in text
    assert "more extensions" not in text
    assert "detect_extensions()" not in text  # не-усечённая стратегия не зовёт detect_extensions
    # #1: budget==0 → без построчных счётчиков; total=20 в заголовке.
    assert _shown_count_headers(text) == set()
    assert "20 EXTENSIONS DETECTED" in text
    assert "get_overrides(" in text


def test_strategy_main_cap_plus_one(monkeypatch):
    """N==cap+1 → ровно +1 more."""
    from rlm_tools_bsl.bsl_knowledge import _extension_strategy

    monkeypatch.setenv("RLM_EXT_LIST_CAP", "20")
    ctx = _ctx_main(21)
    text = _extension_strategy(ctx, _overrides_for(ctx, per_ext=2))
    # #1: budget==0 → усечение отражено total-счётчиком (21) + структурным nearby_extensions,
    # без прозы "+1 more" и без построчных счётчиков.
    assert "21 EXTENSIONS DETECTED" in text
    assert "more extensions" not in text
    assert _shown_count_headers(text) == set()
    assert "nearby_extensions" in text


def test_strategy_main_disabled_cap_keeps_full(monkeypatch):
    """RLM_EXT_LIST_CAP=0 → полный список расширений в стратегии при N=30."""
    from rlm_tools_bsl.bsl_knowledge import _extension_strategy

    monkeypatch.setenv("RLM_EXT_LIST_CAP", "0")
    ctx = _ctx_main(30)
    text = _extension_strategy(ctx, _overrides_for(ctx, per_ext=2))
    assert "more (detect_extensions())" not in text
    # #1: budget==0 → без построчных счётчиков; cap отключён → total=30 (все) в заголовке.
    assert _shown_count_headers(text) == set()
    assert "30 EXTENSIONS DETECTED" in text


def test_strategy_main_critical_block_preserved_when_truncated(monkeypatch):
    """Блок CRITICAL-инструкций дословно сохранён и при усечении."""
    from rlm_tools_bsl.bsl_knowledge import _extension_strategy

    monkeypatch.setenv("RLM_EXT_LIST_CAP", "20")
    ctx = _ctx_main(25)
    text = _extension_strategy(ctx, _overrides_for(ctx, per_ext=3))
    assert "PermissionError" in text
    assert "read_procedure" in text and "extract_procedures" in text


# ── Site 2: response-поле через _rlm_start ──────────────────────────────────


def _make_main_with_n_extensions(parent, n, override_idx=None):
    """src/cf (main) + src/cfe/Ext{i:03d} (n расширений). override_idx получает
    реальный annotation-bearing модуль (overrides_count>0); остальные пустые."""
    from test_extension_overrides import _CF_MAIN_XML, _EXT_MODULE_BSL, _cf_extension_xml, _write

    cf = os.path.join(parent, "src", "cf")
    _write(os.path.join(cf, "Configuration.xml"), _CF_MAIN_XML)
    for i in range(n):
        ext = os.path.join(parent, "src", "cfe", f"Ext{i:03d}")
        _write(os.path.join(ext, "Configuration.xml"), _cf_extension_xml(f"Расш{i}", "Customization", f"р{i}_"))
        if i == override_idx:
            _write(os.path.join(ext, "Catalogs", "Номенклатура", "Ext", "ObjectModule.bsl"), _EXT_MODULE_BSL)
    return cf


def test_response_truncates_above_cap(tmp_path, monkeypatch):
    from rlm_tools_bsl.server import _rlm_end, _rlm_start

    monkeypatch.setenv("RLM_EXT_LIST_CAP", "20")
    cf = _make_main_with_n_extensions(str(tmp_path), n=25, override_idx=7)
    resp = json.loads(_rlm_start(path=cf, query="расширения"))
    assert "error" not in resp, resp
    sid = resp["session_id"]
    try:
        ec = resp["extension_context"]
        assert len(ec["nearby_extensions"]) == 20
        assert ec["nearby_extensions_truncated"] is True
        assert ec["nearby_extensions_total"] == 25
        assert ec["nearby_extensions_shown"] == 20
        assert "detect_extensions" in ec["extensions_hint"]
        # override-bearing расширение (Ext7, 2 перехвата) ранжируется первым.
        assert ec["nearby_extensions"][0]["overrides_count"] > 0
        assert ec["nearby_extensions"][0]["name"] == "Расш7"
    finally:
        _rlm_end(sid)


def test_response_full_at_cap_no_companion_fields(tmp_path, monkeypatch):
    from rlm_tools_bsl.server import _rlm_end, _rlm_start

    monkeypatch.setenv("RLM_EXT_LIST_CAP", "20")
    cf = _make_main_with_n_extensions(str(tmp_path), n=20)
    resp = json.loads(_rlm_start(path=cf, query="расширения"))
    assert "error" not in resp, resp
    sid = resp["session_id"]
    try:
        ec = resp["extension_context"]
        assert len(ec["nearby_extensions"]) == 20
        assert "nearby_extensions_truncated" not in ec
        assert "nearby_extensions_total" not in ec
        assert "nearby_extensions_shown" not in ec
        assert "extensions_hint" not in ec
    finally:
        _rlm_end(sid)


def test_sandbox_gets_all_extension_paths_above_cap(tmp_path, monkeypatch):
    """F8.2 (white-box): при N>cap песочница получает ВСЕ N путей, не top-N —
    find_module/find_attributes по расширениям вне top-N не должны ломаться."""
    from rlm_tools_bsl.server import _rlm_end, _rlm_start, _sandboxes

    monkeypatch.setenv("RLM_EXT_LIST_CAP", "20")
    cf = _make_main_with_n_extensions(str(tmp_path), n=25)
    resp = json.loads(_rlm_start(path=cf, query="расширения"))
    assert "error" not in resp, resp
    sid = resp["session_id"]
    try:
        backend = _sandboxes[sid]
        assert backend.extension_paths_count == 25  # песочница полна
        assert len(resp["extension_context"]["nearby_extensions"]) == 20  # response усечён
    finally:
        _rlm_end(sid)


@pytest.mark.parametrize("cap_value", ["0", "-1"])
def test_response_disabled_cap_keeps_full(tmp_path, monkeypatch, cap_value):
    """env-disable: RLM_EXT_LIST_CAP=0 и =-1, N=30 → полный список во всех трёх
    представлениях (response без companion-полей, warning/strategy со всеми именами)."""
    from rlm_tools_bsl.server import _rlm_end, _rlm_start

    monkeypatch.setenv("RLM_EXT_LIST_CAP", cap_value)
    cf = _make_main_with_n_extensions(str(tmp_path), n=30)
    resp = json.loads(_rlm_start(path=cf, query="расширения"))
    assert "error" not in resp, resp
    sid = resp["session_id"]
    try:
        ec = resp["extension_context"]
        # Site 2: полный список, без companion-полей.
        assert len(ec["nearby_extensions"]) == 30
        assert "nearby_extensions_truncated" not in ec
        assert "extensions_hint" not in ec
        # Site 1: warning со всеми именами (без короткой сводки).
        assert any("Расш29" in w for w in resp["warnings"])
        assert not any("call detect_extensions() for the complete list" in w for w in resp["warnings"])
        # Site 3: strategy — счётчик total (все 30) + указатель на nearby_extensions,
        # без +more и без перечня имён/префиксов (#1, v1.28.0).
        assert "30 EXTENSIONS DETECTED" in resp["strategy"]
        assert "nearby_extensions" in resp["strategy"]
        assert "more (detect_extensions())" not in resp["strategy"]
    finally:
        _rlm_end(sid)


def test_extension_session_response_not_truncated(tmp_path, monkeypatch):
    """Finding (codex): ветку EXTENSION НЕ трогаем. Сессия, открытая на одиночном
    расширении с >cap соседних расширений, не должна усекаться в response и НЕ
    получать companion-поля — усечение только для current.role == MAIN."""
    from rlm_tools_bsl.server import _rlm_end, _rlm_start

    monkeypatch.setenv("RLM_EXT_LIST_CAP", "20")
    _make_main_with_n_extensions(str(tmp_path), n=25)
    ext0 = os.path.join(str(tmp_path), "src", "cfe", "Ext000")
    resp = json.loads(_rlm_start(path=ext0, query="расширение"))
    assert "error" not in resp, resp
    sid = resp["session_id"]
    try:
        ec = resp["extension_context"]
        assert ec["is_extension"] is True
        assert len(ec["nearby_extensions"]) == 24  # все 24 соседа, не top-20
        assert "nearby_extensions_truncated" not in ec
        assert "nearby_extensions_total" not in ec
        assert "extensions_hint" not in ec
    finally:
        _rlm_end(sid)


# ── Регрессия: малый CF+CFE (N=1) не затронут ───────────────────────────────


def test_small_cf_cfe_unaffected(tmp_path, monkeypatch):
    """N=1: companion-полей нет, warning полный с именем, заголовок без +more
    (формальная фиксация «малые конфиги байт-в-байт прежние»)."""
    from test_extension_overrides import _make_main_with_extension
    from rlm_tools_bsl.server import _rlm_end, _rlm_start

    monkeypatch.delenv("RLM_EXT_LIST_CAP", raising=False)  # дефолт cap=20
    cf, _cfe = _make_main_with_extension(tmp_path)
    resp = json.loads(_rlm_start(path=cf, query="перехваты расширения"))
    assert "error" not in resp, resp
    sid = resp["session_id"]
    try:
        ec = resp["extension_context"]
        assert len(ec["nearby_extensions"]) == 1
        assert "nearby_extensions_truncated" not in ec
        assert "extensions_hint" not in ec
        # warning полный с именем расширения, без короткой сводки.
        assert any("ТестовоеРасширение" in w for w in resp["warnings"])
        assert not any("call detect_extensions() for the complete list" in w for w in resp["warnings"])
        # strategy без +more.
        assert "more (detect_extensions())" not in resp["strategy"]
    finally:
        _rlm_end(sid)
