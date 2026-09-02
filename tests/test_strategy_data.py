"""Sanity checks for src/rlm_tools_bsl/bsl_strategy_data.py.

These guard against accidental drift when editing the strategy text — the
slim-mode `rlm_help` MCP tool reads sections from this module, so silent
content changes here would break agent guidance.
"""

from __future__ import annotations

from rlm_tools_bsl.bsl_strategy_data import DISAMBIGUATION_PAIRS, STRATEGY_SECTIONS


def test_disambiguation_pairs_count():
    assert len(DISAMBIGUATION_PAIRS) == 12


_PAIR_ALIASES = {
    # Структурированная запись использует search_methods как ПРЕДСТАВИТЕЛЯ семейства
    # search_X; в full-блоке заголовок написан обобщённо.
    "search_X": "search_methods",
}


def _local_pair_headings() -> set[frozenset[str]]:
    """Нормализованные unordered пары из ЗАГОЛОВКОВ блока ``== DISAMBIGUATION ==``.

    Проверяется именно ЛОКАЛЬНАЯ пара, а не «оба имени где-нибудь в блоке»: наивная
    проверка ложно зеленела бы, потому что ``find_references_to_object`` уже
    встречается в другой паре, а ``find_roles`` — рядом с ``parse_object_xml``.
    """
    import re

    from rlm_tools_bsl.bsl_knowledge import _STRATEGY_HEADER

    block = _STRATEGY_HEADER.split("== DISAMBIGUATION ==", 1)[1]
    # Блок кончается на следующем маркере секции.
    for marker in ("\n== ",):
        idx = block.find(marker)
        if idx != -1:
            block = block[:idx]

    pairs: set[frozenset[str]] = set()
    ident_re = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")
    for raw in block.splitlines():
        line = raw.strip()
        # Заголовок пары — строка вида "A ... vs B ...:" на нулевом отступе блока.
        if not line.endswith(":") or " vs " not in line or raw.startswith((" ", "	", "-")):
            continue
        left, _, right = line.partition(" vs ")
        a_m = ident_re.search(left)
        b_m = ident_re.search(right)
        if not a_m or not b_m:
            continue
        a = _PAIR_ALIASES.get(a_m.group(0), a_m.group(0))
        b = _PAIR_ALIASES.get(b_m.group(0), b_m.group(0))
        if a != b:
            pairs.add(frozenset({a, b}))
    return pairs


def test_full_strategy_block_covers_every_structured_pair():
    """Каждая structured pair обязана иметь ОДИН локальный heading в full-блоке.

    Теста синхронности двух копий DISAMBIGUATION раньше НЕ БЫЛО: сверялись только
    счётчики, поэтому пару можно было добавить в slim, поправить два числа — и
    получить полностью зелёный прогон при том, что full уехал без пары. Именно так
    и уехала пре-существующая пара find_references_to_object / find_code_usages.
    Дополнительные full-only пары разрешены.
    """
    headings = _local_pair_headings()
    missing = [tuple(sorted(e["pair"])) for e in DISAMBIGUATION_PAIRS if frozenset(e["pair"]) not in headings]
    assert not missing, f"structured pairs без локального heading в _STRATEGY_HEADER: {missing}"


def test_full_strategy_pair_tripwire_is_not_vacuous():
    """Negative-control: подмена ИМЕНИ в direct heading обязана уронить проверку,
    даже когда оба helper-name остаются в блоке в других парах."""
    headings = _local_pair_headings()
    target = frozenset({"find_roles", "find_references_to_object"})
    assert target in headings
    weakened = {h for h in headings if h != target}
    assert target not in weakened  # именно локальная пара, а не «имена где-то есть»


def test_slim_disambiguation_pointer_count_matches_data():
    """Число в slim-указателе считается ИЗ ДАННЫХ, а не зашито литералом.

    Ассерт по вычисленному числу: литерал протухал бы так же, как протухла «8».
    """
    from rlm_tools_bsl.bsl_knowledge import _SLIM_DISAMBIGUATION_POINTER

    assert f"{len(DISAMBIGUATION_PAIRS)} overlapping helper pairs" in _SLIM_DISAMBIGUATION_POINTER


def test_disambiguation_pair_shape():
    required_keys = {"pair", "summary", "when_a", "when_b", "rule", "tags"}
    for entry in DISAMBIGUATION_PAIRS:
        assert required_keys.issubset(entry.keys()), entry
        a, b = entry["pair"]
        assert isinstance(a, str) and isinstance(b, str)
        assert isinstance(entry["summary"], str) and entry["summary"]
        assert isinstance(entry["rule"], str) and entry["rule"]
        assert isinstance(entry["tags"], list)


def test_strategy_sections_keys():
    # The dispatcher routes section='disambiguation' separately to the
    # structured DISAMBIGUATION_PAIRS list — that key must NOT live in
    # STRATEGY_SECTIONS.
    assert set(STRATEGY_SECTIONS.keys()) == {"workflow", "performance", "batching", "io", "critical", "coverage"}


def test_strategy_sections_values_nonempty():
    for k, v in STRATEGY_SECTIONS.items():
        assert isinstance(v, str) and v.strip(), f"empty STRATEGY_SECTIONS[{k!r}]"


def test_strategy_sections_did_not_drift_from_legacy():
    # Sanity: each section's marker / leading line still appears in the
    # legacy strategy header. Not byte-for-byte — just guards against
    # accidental rename or deletion. _STRATEGY_HEADER + _STRATEGY_IO_SECTION
    # are the source of truth in legacy mode.
    from rlm_tools_bsl.bsl_knowledge import _STRATEGY_HEADER, _STRATEGY_IO_SECTION

    assert "== CRITICAL ==" in STRATEGY_SECTIONS["critical"]
    assert "== CRITICAL ==" in _STRATEGY_HEADER

    assert "== WORKFLOW ==" in STRATEGY_SECTIONS["workflow"]
    assert "Step 0 — UNDERSTAND" in STRATEGY_SECTIONS["workflow"]
    assert "Step 0 — UNDERSTAND" in _STRATEGY_HEADER

    assert "== STEP 4 EXTENDED" in STRATEGY_SECTIONS["performance"]
    assert "== STEP 4 EXTENDED" in _STRATEGY_HEADER

    assert "== BATCHING & OUTPUT ==" in STRATEGY_SECTIONS["batching"]
    assert "== BATCHING & OUTPUT ==" in _STRATEGY_HEADER

    assert "File I/O:" in STRATEGY_SECTIONS["io"]
    assert "File I/O:" in _STRATEGY_IO_SECTION

    # v1.12.0: Step 5 extensions phrasing — must mention high-level helpers
    # in BOTH slim (STRATEGY_SECTIONS["workflow"]) and full (_STRATEGY_HEADER).
    ext_marker_slim = "to high-level BSL helpers"
    assert ext_marker_slim in STRATEGY_SECTIONS["workflow"]
    assert ext_marker_slim in _STRATEGY_HEADER
    # And the explicit PermissionError warning remains.
    assert "PermissionError" in STRATEGY_SECTIONS["workflow"]
    assert "PermissionError" in _STRATEGY_HEADER


# v1.12.0 extension visibility — explicit checks per round-6 review.


def test_strategy_sections_slim_mentions_extension_helpers():
    section = STRATEGY_SECTIONS["workflow"]
    for needle in ("read_procedure", "extract_procedures", "parse_object_xml", "find_predefined"):
        assert needle in section


def test_strategy_text_full_mentions_extension_helpers():
    from rlm_tools_bsl.bsl_knowledge import _STRATEGY_HEADER

    for needle in ("read_procedure", "extract_procedures", "parse_object_xml", "find_predefined"):
        assert needle in _STRATEGY_HEADER


def test_rlm_help_topic_extensions_does_not_suggest_read_file_on_ext_paths():
    from rlm_tools_bsl.bsl_knowledge import _BUSINESS_RECIPES

    recipe = _BUSINESS_RECIPES["расширения"]
    full_text = " ".join(recipe["full"])
    compact_text = " ".join(recipe["compact"])
    # No "read_file('../...')" suggestion.
    assert "read_file('../" not in full_text
    assert "read_file('../" not in compact_text
    # Helpers mentioned explicitly somewhere.
    assert "read_procedure" in full_text + compact_text
    assert "extract_procedures" in full_text + compact_text


# ── v1.23.0 tripwires: batch-default + truncation marker (slim + full) ──────


def test_batching_leads_with_get_object_profile_slim_and_full():
    from rlm_tools_bsl.bsl_knowledge import _STRATEGY_HEADER

    for text in (STRATEGY_SECTIONS["batching"], _STRATEGY_HEADER):
        assert "get_object_profile" in text
        # the batch (list) form of the overloaded reader is shown, not just the single form
        assert "read_procedure(path, ['" in text
        # read_files batch form promoted
        assert "read_files([" in text


def test_truncation_marker_matches_sandbox_slim_and_full():
    """Strategy references the ACTUAL marker the sandbox writes — '... [output truncated]' (R2 #4)."""
    from rlm_tools_bsl.bsl_knowledge import _STRATEGY_HEADER

    for text in (STRATEGY_SECTIONS["batching"], _STRATEGY_HEADER):
        assert "... [output truncated]" in text


def test_no_get_index_info_on_start_note_slim_and_full():
    from rlm_tools_bsl.bsl_knowledge import _STRATEGY_HEADER

    for text in (STRATEGY_SECTIONS["batching"], _STRATEGY_HEADER):
        assert "get_index_info" in text  # the "не зови ... на старте" note


def test_extension_critical_block_mentions_new_phrasing():
    from types import SimpleNamespace

    from rlm_tools_bsl.bsl_knowledge import _extension_strategy
    from rlm_tools_bsl.extension_detector import ConfigRole

    ctx = SimpleNamespace(
        current=SimpleNamespace(role=ConfigRole.MAIN, name="MainCfg", purpose="", name_prefix=""),
        nearby_extensions=[
            SimpleNamespace(name="ExtAddOn", name_prefix="ext_", path="/tmp/cfe/ExtAddOn"),
        ],
        nearby_main=None,
    )
    text = _extension_strategy(ctx, {})
    assert "PermissionError" in text
    assert "read_procedure" in text and "extract_procedures" in text


# ── v1.27.0 — get_index_info() nudge: strengthened BATCHING copies + co-location ──


def _batching_nudge_line(text):
    for ln in text.splitlines():
        if "Не зови get_index_info на старте" in ln:
            return ln
    return None


def test_get_index_info_nudge_strengthened_and_synced_slim_and_full():
    """Both BATCHING copies call out the wasted execute AND stay byte-identical."""
    from rlm_tools_bsl.bsl_knowledge import _STRATEGY_HEADER

    slim = _batching_nudge_line(STRATEGY_SECTIONS["batching"])
    full = _batching_nudge_line(_STRATEGY_HEADER)
    assert slim is not None and full is not None
    assert slim == full  # no drift between slim and full copies
    assert "пустая трата execute" in slim


def test_index_block_colocates_get_index_info_nudge():
    """The dynamic INDEX block carries the "don't call get_index_info() on start" nudge
    right next to the data it duplicates (rlm_start.index)."""
    from rlm_tools_bsl.bsl_knowledge import _render_index_block

    block = _render_index_block({"builder_version": 14, "methods": 10, "calls": 5}, [])
    assert "== INDEX ==" in block
    assert "get_index_info" in block
    assert "rlm_start.index" in block


def test_count_only_contract_synced_in_both_strategy_copies():
    """v1.30.0: count_only стал CFE-aware. Обе копии NOTE (slim STRATEGY_SECTIONS и
    встроенная full _STRATEGY_HEADER) обязаны описывать НОВЫЙ контракт — иначе код
    считает расширения, а половина агентов продолжает читать «index-side {total}».
    """
    from rlm_tools_bsl.bsl_knowledge import _STRATEGY_HEADER

    for text in (STRATEGY_SECTIONS["workflow"], _STRATEGY_HEADER):
        assert "count_only=True" in text
        assert "тот же scope" in text
        assert "total_extensions" in text
        assert "index-side {total}" not in text  # старое обещание ушло
