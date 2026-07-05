"""v1.26.1: rlm_execute logs the agent's executed code (the query) for later analysis.

The executed code IS the mini-prompt — helper calls with their parameters. It is
appended to the rlm_execute completion log line as ``code=<...>`` with newlines
flattened to ⏎ so the event stays a single, grep-friendly line.
"""

from __future__ import annotations

import os

from unittest.mock import patch

from rlm_tools_bsl.server import _DEFAULT_EXECUTE_CODE_LOG_CAP, _execute_code_log_field


def test_default_on_flattens_newlines():
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("RLM_LOG_EXECUTE_CODE", None)
        field = _execute_code_log_field('x = find_object("ТестовыйСправочник")\nprint(x)')
    assert field == ' code=<x = find_object("ТестовыйСправочник")⏎print(x)>'
    assert "\n" not in field


def test_crlf_normalized():
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("RLM_LOG_EXECUTE_CODE", None)
        field = _execute_code_log_field("a\r\nb\rc")
    assert field == " code=<a⏎b⏎c>"


def test_disabled_values():
    for val in ("0", "false", "off", "no", "FALSE", " Off "):
        with patch.dict(os.environ, {"RLM_LOG_EXECUTE_CODE": val}):
            assert _execute_code_log_field("print(1)") == ""


def test_explicit_on_values_use_default_cap():
    long_code = "a" * (_DEFAULT_EXECUTE_CODE_LOG_CAP + 50)
    for val in ("1", "true", "on", "yes", "all"):
        with patch.dict(os.environ, {"RLM_LOG_EXECUTE_CODE": val}):
            field = _execute_code_log_field(long_code)
        assert field.endswith("…>")
        # code=< + cap chars + …>
        assert len(field) == len(" code=<") + _DEFAULT_EXECUTE_CODE_LOG_CAP + len("…>")


def test_custom_integer_cap():
    with patch.dict(os.environ, {"RLM_LOG_EXECUTE_CODE": "5"}):
        field = _execute_code_log_field("1234567890")
    assert field == " code=<12345…>"


def test_negative_or_zero_int_disables():
    for val in ("-1", "0"):
        with patch.dict(os.environ, {"RLM_LOG_EXECUTE_CODE": val}):
            assert _execute_code_log_field("print(1)") == ""


def test_invalid_value_falls_back_to_default():
    with patch.dict(os.environ, {"RLM_LOG_EXECUTE_CODE": "banana"}):
        field = _execute_code_log_field("print(1)")
    assert field == " code=<print(1)>"


def test_short_code_not_truncated():
    with patch.dict(os.environ, {"RLM_LOG_EXECUTE_CODE": "1000"}):
        field = _execute_code_log_field("find_module('X')")
    assert field == " code=<find_module('X')>"
    assert "…" not in field
