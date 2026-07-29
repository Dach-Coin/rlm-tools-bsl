"""Tests for the help(task) sandbox helper."""

import tempfile

from test_bsl_helpers import _make_bsl_fixture


def _get_help(tmpdir=None):
    """Get the help function from a BSL fixture."""
    if tmpdir is None:
        with tempfile.TemporaryDirectory() as td:
            bsl, _ = _make_bsl_fixture(td)
            return bsl["help"]
    bsl, _ = _make_bsl_fixture(tmpdir)
    return bsl["help"]


def test_help_no_args_returns_all_recipes():
    with tempfile.TemporaryDirectory() as td:
        bsl_help = _get_help(td)
        result = bsl_help()
        assert "Available recipes" in result
        assert "help('find_exports')" in result
        assert "help('find_callers_context')" in result
        assert "help('parse_object_xml')" in result
        assert "help('safe_grep')" in result
        assert "help('read_procedure')" in result


def test_help_exports():
    with tempfile.TemporaryDirectory() as td:
        bsl_help = _get_help(td)
        result = bsl_help("exports")
        assert "find_exports" in result
        assert "print(" in result


def test_help_exports_russian():
    with tempfile.TemporaryDirectory() as td:
        bsl_help = _get_help(td)
        result = bsl_help("экспорт")
        assert "find_exports" in result


def test_help_callers():
    with tempfile.TemporaryDirectory() as td:
        bsl_help = _get_help(td)
        result = bsl_help("call graph")
        assert "find_callers_context" in result


def test_help_callers_russian():
    with tempfile.TemporaryDirectory() as td:
        bsl_help = _get_help(td)
        result = bsl_help("граф вызовов")
        assert "find_callers_context" in result


def test_help_metadata():
    with tempfile.TemporaryDirectory() as td:
        bsl_help = _get_help(td)
        result = bsl_help("метаданные")
        assert "parse_object_xml" in result


def test_help_search():
    with tempfile.TemporaryDirectory() as td:
        bsl_help = _get_help(td)
        result = bsl_help("поиск")
        assert "safe_grep" in result


def test_help_read():
    with tempfile.TemporaryDirectory() as td:
        bsl_help = _get_help(td)
        result = bsl_help("read")
        assert "read_procedure" in result


def test_help_unknown_falls_back_to_all():
    with tempfile.TemporaryDirectory() as td:
        bsl_help = _get_help(td)
        result = bsl_help("xyzzy_gibberish_12345")
        assert "Available recipes" in result


# ── v1.30.0: agent-facing контракты в sig/recipe ──────────────


def test_safe_grep_sig_names_max_files_cap_in_both_modes():
    """RO-5: срез max_files действует и с name_hint — sig обязан говорить это прямо."""
    from rlm_tools_bsl.bsl_helpers import build_helper_metadata_snapshot

    sig = build_helper_metadata_snapshot()["safe_grep"]["sig"]
    assert "max_files" in sig
    # Оговорка обязана покрывать ОБА режима: срез действует всегда, hint лишь меняет,
    # что режется. Формулировка "только без name_hint" была бы неполной (широкий hint
    # точно так же даёт ложный []).
    assert "ВСЕГДА" in sig and "hint" in sig
    assert "не доказывает отсутствие" in sig


def test_help_safe_grep_gives_full_route_and_denies_proof_of_absence():
    with tempfile.TemporaryDirectory() as td:
        result = _get_help(td)("safe_grep")
        assert "НЕ доказывает отсутствие" in result
        assert "git_search" in result
        assert "find_module" in result


def test_help_overrides_says_aggregates_are_dicts_and_none_line_is_valid():
    with tempfile.TemporaryDirectory() as td:
        result = _get_help(td)("перехват")
        assert "target_method_line" in result
        assert ".items()" in result
