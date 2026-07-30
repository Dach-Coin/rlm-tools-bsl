import json
import os
import sys
from unittest.mock import patch, MagicMock

import httpx
import pytest

import rlm_tools_bsl.llm_bridge as _llm_bridge_module
from rlm_tools_bsl.llm_bridge import (
    DEFAULT_MAX_TOKENS,
    make_llm_query,
    make_llm_query_batched,
    _make_openai_query,
    _resolve_extra_body,
    _resolve_max_tokens,
    get_llm_query_fn,
    validate_llm_env,
    warmup_openai_import,
)


@pytest.fixture(autouse=True)
def _isolate_sub_llm_env(monkeypatch):
    """Ни один тест файла не должен видеть RLM_LLM_MAX_TOKENS/EXTRA_BODY с машины.

    Иначе значения разработчика молча меняли бы max_tokens и тело запроса в тестах,
    которые про них ничего не знают.
    """
    monkeypatch.delenv("RLM_LLM_MAX_TOKENS", raising=False)
    monkeypatch.delenv("RLM_LLM_EXTRA_BODY", raising=False)


# ── Existing Anthropic tests (unchanged) ──────────────


def test_llm_query_calls_anthropic():
    mock_client = MagicMock()
    mock_client.messages.create.return_value = MagicMock(content=[MagicMock(text="YES - handles errors properly")])

    query_fn = make_llm_query(client=mock_client, model="claude-haiku-4-5-20251001")
    result = query_fn("Does this handle errors?", context="some code here")

    assert "YES" in result
    mock_client.messages.create.assert_called_once()


def test_llm_query_without_context():
    mock_client = MagicMock()
    mock_client.messages.create.return_value = MagicMock(content=[MagicMock(text="42")])

    query_fn = make_llm_query(client=mock_client, model="claude-haiku-4-5-20251001")
    result = query_fn("What is the answer?")

    assert "42" in result
    call_args = mock_client.messages.create.call_args
    messages = call_args.kwargs.get("messages") or call_args[1].get("messages")
    assert len(messages) == 1
    assert "Context:" not in messages[0]["content"]


def test_llm_query_batched():
    mock_client = MagicMock()
    mock_client.messages.create.return_value = MagicMock(content=[MagicMock(text="answer")])

    query_fn = make_llm_query(client=mock_client, model="claude-haiku-4-5-20251001")
    batch_fn = make_llm_query_batched(query_fn)

    results = batch_fn(["q1", "q2", "q3"])
    assert len(results) == 3
    assert all(r == "answer" for r in results)


# ── OpenAI-compatible provider tests ──────────────────


def _mock_openai_module():
    """Inject a mock 'openai' module into sys.modules for lazy import."""
    mock_module = MagicMock()
    mock_client = MagicMock()
    mock_module.OpenAI.return_value = mock_client
    return mock_module, mock_client


def test_openai_query_calls_openai():
    mock_module, mock_client = _mock_openai_module()
    mock_client.chat.completions.create.return_value = MagicMock(
        choices=[MagicMock(message=MagicMock(content="YES - works"))]
    )

    with patch.dict(sys.modules, {"openai": mock_module}):
        query_fn = _make_openai_query("http://localhost:11434/v1", "test-key", "qwen2.5:7b")
        result = query_fn("Does this work?", context="some code")

    assert "YES" in result
    mock_client.chat.completions.create.assert_called_once()
    call_args = mock_client.chat.completions.create.call_args
    messages = call_args.kwargs.get("messages")
    assert "Context:" in messages[0]["content"]


def test_openai_query_without_context():
    mock_module, mock_client = _mock_openai_module()
    mock_client.chat.completions.create.return_value = MagicMock(choices=[MagicMock(message=MagicMock(content="42"))])

    with patch.dict(sys.modules, {"openai": mock_module}):
        query_fn = _make_openai_query("http://x", "key", "model")
        result = query_fn("What is the answer?")

    assert "42" in result
    call_args = mock_client.chat.completions.create.call_args
    messages = call_args.kwargs.get("messages")
    assert len(messages) == 1
    assert "Context:" not in messages[0]["content"]


def test_openai_empty_response():
    mock_module, mock_client = _mock_openai_module()
    mock_client.chat.completions.create.return_value = MagicMock(choices=[])

    with patch.dict(sys.modules, {"openai": mock_module}):
        query_fn = _make_openai_query("http://x", "key", "model")
        result = query_fn("test")

    assert result == ""


def test_openai_batched():
    mock_module, mock_client = _mock_openai_module()
    mock_client.chat.completions.create.return_value = MagicMock(
        choices=[MagicMock(message=MagicMock(content="answer"))]
    )

    with patch.dict(sys.modules, {"openai": mock_module}):
        query_fn = _make_openai_query("http://x", "key", "model")
        batch_fn = make_llm_query_batched(query_fn)

    results = batch_fn(["q1", "q2", "q3"])
    assert len(results) == 3
    assert all(r == "answer" for r in results)


# ── Provider priority / factory tests ─────────────────


def _clean_llm_env(env_dict):
    """Helper: remove all LLM-related env vars, then set given ones."""
    keys_to_clear = [
        "RLM_LLM_BASE_URL",
        "RLM_LLM_API_KEY",
        "RLM_LLM_MODEL",
        "ANTHROPIC_API_KEY",
        "RLM_SUB_MODEL",
        # Иначе реальные значения с машины разработчика протекли бы в тесты,
        # которые собирают окружение из os.environ.
        "RLM_LLM_MAX_TOKENS",
        "RLM_LLM_EXTRA_BODY",
    ]
    cleaned = {k: v for k, v in os.environ.items() if k not in keys_to_clear}
    cleaned.update(env_dict)
    return cleaned


def test_provider_priority_openai_over_anthropic():
    mock_module, mock_client = _mock_openai_module()
    mock_client.chat.completions.create.return_value = MagicMock(
        choices=[MagicMock(message=MagicMock(content="openai-response"))]
    )
    env = _clean_llm_env(
        {
            "RLM_LLM_BASE_URL": "http://localhost:11434/v1",
            "RLM_LLM_API_KEY": "test",
            "RLM_LLM_MODEL": "qwen2.5:7b",
            "ANTHROPIC_API_KEY": "sk-ant-test",
        }
    )
    with patch.dict(os.environ, env, clear=True), patch.dict(sys.modules, {"openai": mock_module}):
        fn = get_llm_query_fn()
        assert fn is not None
        result = fn("test")
        assert result == "openai-response"
        mock_module.OpenAI.assert_called_once()


def test_provider_fallback_to_anthropic():
    env = _clean_llm_env({"ANTHROPIC_API_KEY": "sk-ant-test"})
    with patch.dict(os.environ, env, clear=True), patch("rlm_tools_bsl.llm_bridge.Anthropic") as MockAnthropic:
        mock_client = MagicMock()
        MockAnthropic.return_value = mock_client
        mock_client.messages.create.return_value = MagicMock(content=[MagicMock(text="anthropic-response")])

        fn = get_llm_query_fn()
        assert fn is not None
        result = fn("test")
        assert result == "anthropic-response"


def test_provider_none_when_no_keys():
    env = _clean_llm_env({})
    with patch.dict(os.environ, env, clear=True):
        fn = get_llm_query_fn()
        assert fn is None


def test_openai_missing_model():
    env = _clean_llm_env({"RLM_LLM_BASE_URL": "http://localhost:11434/v1"})
    with patch.dict(os.environ, env, clear=True):
        fn = get_llm_query_fn()
        assert fn is None


# ── Edge case tests ───────────────────────────────────


def test_anthropic_empty_response():
    mock_client = MagicMock()
    mock_client.messages.create.return_value = MagicMock(content=[])

    query_fn = make_llm_query(client=mock_client, model="claude-haiku-4-5-20251001")
    result = query_fn("test")

    assert result == ""


def test_openai_none_content():
    """content=None → маркер [EMPTY], а не пустая строка (намеренная смена контракта).

    Ответ собран на MagicMock, у которого finish_reason — истинный объект, но НЕ str,
    поэтому ожидается НЕЙТРАЛЬНЫЙ маркер: без разбора типов мок утащил бы ответ в
    reasoning-ветку и подставил бы `<MagicMock ...>` в текст.
    """
    mock_module, mock_client = _mock_openai_module()
    mock_client.chat.completions.create.return_value = MagicMock(choices=[MagicMock(message=MagicMock(content=None))])

    with patch.dict(sys.modules, {"openai": mock_module}):
        query_fn = _make_openai_query("http://x", "key", "model")
        result = query_fn("test")

    assert isinstance(result, str)
    assert result.startswith("[EMPTY]"), result
    assert "MagicMock" not in result, result
    assert "RLM_LLM_MAX_TOKENS" not in result, result


def test_empty_prompt_raises_anthropic():
    mock_client = MagicMock()
    query_fn = make_llm_query(client=mock_client, model="test")

    try:
        query_fn("")
        assert False, "Should have raised ValueError"
    except ValueError:
        pass


def test_empty_prompt_raises_openai():
    mock_module, mock_client = _mock_openai_module()

    with patch.dict(sys.modules, {"openai": mock_module}):
        query_fn = _make_openai_query("http://x", "key", "model")

    try:
        query_fn("")
        assert False, "Should have raised ValueError"
    except ValueError:
        pass


def test_batched_empty_list():
    mock_client = MagicMock()
    query_fn = make_llm_query(client=mock_client, model="test")
    batch_fn = make_llm_query_batched(query_fn)

    results = batch_fn([])
    assert results == []
    mock_client.messages.create.assert_not_called()


def test_batched_handles_error():
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = RuntimeError("API down")

    query_fn = make_llm_query(client=mock_client, model="test")
    batch_fn = make_llm_query_batched(query_fn)

    results = batch_fn(["q1"])
    assert len(results) == 1
    assert "[ERROR]" in results[0]
    assert "API down" in results[0]


# ── Warmup tests ───────────────────────────────────────


def test_warmup_no_crash_without_openai():
    """warmup_openai_import should not raise even when openai is missing."""
    # Reset flag to test fresh
    _llm_bridge_module._openai_warmup_done = False
    try:
        with patch.dict(sys.modules, {"openai": None}):
            # openai mapped to None simulates ImportError
            _llm_bridge_module._openai_warmup_done = False
            warmup_openai_import()
        assert _llm_bridge_module._openai_warmup_done is True
    finally:
        _llm_bridge_module._openai_warmup_done = False


def test_warmup_idempotent():
    """Second call should be a no-op (flag already True)."""
    _llm_bridge_module._openai_warmup_done = False
    try:
        warmup_openai_import()
        assert _llm_bridge_module._openai_warmup_done is True
        # Second call — should not fail
        warmup_openai_import()
        assert _llm_bridge_module._openai_warmup_done is True
    finally:
        _llm_bridge_module._openai_warmup_done = False


def test_warmup_does_not_break_get_llm_query_fn():
    """Warmup should not interfere with get_llm_query_fn without RLM_LLM_MODEL."""
    _llm_bridge_module._openai_warmup_done = False
    try:
        warmup_openai_import()
        env = _clean_llm_env({})
        with patch.dict(os.environ, env, clear=True):
            fn = get_llm_query_fn()
            assert fn is None
    finally:
        _llm_bridge_module._openai_warmup_done = False


# ── v1.30.2: настраиваемые параметры sub-LLM (issue #19) ─

_SENTINEL_API_KEY = "sk-sentinel-KEYLEAK-9f3a"
_SENTINEL_PROMPT = "SENTINEL-PROMPT-LEAK-7b21"


def _sdk_response(payload: dict):
    """Ответ, построенный НАСТОЯЩИМ клиентом из HTTP-тела через MockTransport.

    Ветвление по finish_reason/refusal/reasoning_content нельзя проверять на
    MagicMock: у мока любой атрибут — истинный объект, ветки становятся
    неотличимы, и тест ничего не докажет. Кроме того, только сетевой путь
    воспроизводит нестандартные типы полей: openai SDK строит объекты ответа
    НЕ валидируя, тогда как прямой конструктор
    ``ChatCompletionMessage(content=[...])`` бросает ValidationError — тест,
    написанный «правильно» через конструктор, упал бы на сборке фикстуры и
    навёл бы на ложный вывод «SDK такое не пропускает».
    """
    from openai import OpenAI

    # Ключ и промпт — намеренно РАЗЛИЧИМЫЕ строки: только так assert на их
    # отсутствие в маркере что-то доказывает (на "k"/"q" он был бы бессмысленным).
    client = OpenAI(
        api_key=_SENTINEL_API_KEY,
        base_url="http://x/v1",
        http_client=httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))),
    )
    return client.chat.completions.create(
        model="m", messages=[{"role": "user", "content": _SENTINEL_PROMPT}], max_tokens=16
    )


def _completion(choices, usage=None):
    payload = {"id": "1", "object": "chat.completion", "created": 0, "model": "m", "choices": choices}
    if usage is not None:
        payload["usage"] = usage
    return payload


def _answer(choices, usage=None) -> str:
    return _llm_bridge_module._openai_answer(_sdk_response(_completion(choices, usage)))


# --- max_tokens ---------------------------------------------------------------


def test_openai_default_max_tokens_and_no_extra_body():
    mock_module, mock_client = _mock_openai_module()
    mock_client.chat.completions.create.return_value = MagicMock(choices=[MagicMock(message=MagicMock(content="ok"))])

    with patch.dict(sys.modules, {"openai": mock_module}):
        _make_openai_query("http://x", "key", "model")("test")

    kwargs = mock_client.chat.completions.create.call_args.kwargs
    assert kwargs["max_tokens"] == DEFAULT_MAX_TOKENS == 2048
    assert "extra_body" not in kwargs


def test_openai_max_tokens_from_env(monkeypatch):
    monkeypatch.setenv("RLM_LLM_MAX_TOKENS", "2048")
    mock_module, mock_client = _mock_openai_module()
    mock_client.chat.completions.create.return_value = MagicMock(choices=[MagicMock(message=MagicMock(content="ok"))])

    with patch.dict(sys.modules, {"openai": mock_module}):
        _make_openai_query("http://x", "key", "model")("test")

    assert mock_client.chat.completions.create.call_args.kwargs["max_tokens"] == 2048


def test_anthropic_max_tokens_from_env(monkeypatch):
    """Лимит действует на ОБА провайдера, не только на OpenAI-совместимый."""
    monkeypatch.setenv("RLM_LLM_MAX_TOKENS", "4096")
    mock_client = MagicMock()
    mock_client.messages.create.return_value = MagicMock(content=[MagicMock(text="ok")])

    make_llm_query(client=mock_client, model="m")("test")

    assert mock_client.messages.create.call_args.kwargs["max_tokens"] == 4096


def test_anthropic_default_max_tokens():
    mock_client = MagicMock()
    mock_client.messages.create.return_value = MagicMock(content=[MagicMock(text="ok")])

    make_llm_query(client=mock_client, model="m")("test")

    assert mock_client.messages.create.call_args.kwargs["max_tokens"] == 2048


@pytest.mark.parametrize("raw", ["abc", "0", "-5", "2.5", "1e3", "0x10", "тысяча"])
def test_max_tokens_invalid_falls_back_with_warning(raw):
    value, warning = _resolve_max_tokens(raw)
    assert value == DEFAULT_MAX_TOKENS
    assert warning and "RLM_LLM_MAX_TOKENS" in warning


@pytest.mark.parametrize("raw", [None, "", "   ", "2048", " 2048 "])
def test_max_tokens_valid_or_absent_has_no_warning(raw):
    _value, warning = _resolve_max_tokens(raw)
    assert warning is None


def test_max_tokens_warning_truncates_huge_value():
    value, warning = _resolve_max_tokens("z" * 5000)
    assert value == DEFAULT_MAX_TOKENS
    assert warning is not None
    assert len(warning) < 300, "предупреждение не должно тащить в лог всё значение"
    assert "5000" in warning


# --- extra_body --------------------------------------------------------------


def test_extra_body_passed_to_sdk(monkeypatch):
    monkeypatch.setenv("RLM_LLM_EXTRA_BODY", '{"thinking":{"type":"disabled"}}')
    mock_module, mock_client = _mock_openai_module()
    mock_client.chat.completions.create.return_value = MagicMock(choices=[MagicMock(message=MagicMock(content="ok"))])

    with patch.dict(sys.modules, {"openai": mock_module}):
        _make_openai_query("http://x", "key", "model")("test")

    kwargs = mock_client.chat.completions.create.call_args.kwargs
    assert kwargs["extra_body"] == {"thinking": {"type": "disabled"}}


@pytest.mark.parametrize("raw", ["{not json", "[1,2]", '"строка"', "42", "null", "true"])
def test_extra_body_rejected_with_warning(raw):
    value, warning = _resolve_extra_body(raw)
    assert value is None
    assert warning and "RLM_LLM_EXTRA_BODY" in warning


@pytest.mark.parametrize("raw", [None, "", "   ", "{}"])
def test_extra_body_absent_is_silent(raw):
    value, warning = _resolve_extra_body(raw)
    assert value is None
    assert warning is None


def test_extra_body_reserved_keys_dropped():
    value, warning = _resolve_extra_body('{"max_tokens":99,"Model":"x","thinking":{"type":"disabled"}}')
    assert value == {"thinking": {"type": "disabled"}}
    assert warning and "max_tokens" in warning and "Model" in warning


def test_extra_body_reserved_key_does_not_override_max_tokens(monkeypatch):
    monkeypatch.setenv("RLM_LLM_EXTRA_BODY", '{"max_tokens":99}')
    mock_module, mock_client = _mock_openai_module()
    mock_client.chat.completions.create.return_value = MagicMock(choices=[MagicMock(message=MagicMock(content="ok"))])

    with patch.dict(sys.modules, {"openai": mock_module}):
        _make_openai_query("http://x", "key", "model")("test")

    kwargs = mock_client.chat.completions.create.call_args.kwargs
    assert kwargs["max_tokens"] == DEFAULT_MAX_TOKENS
    assert "extra_body" not in kwargs, "после отбрасывания единственного ключа посылать нечего"


def test_extra_body_infinity_rejected_before_http():
    """1e999 разбирается в inf и роняет httpx (allow_nan=False) — но уже после
    списания квоты. Значение проходит json.loads, поэтому без контрольной
    сериализации регресс не поймать."""
    parsed = json.loads('{"vendor_option": 1e999}')
    assert parsed["vendor_option"] == float("inf"), "предпосылка теста: JSON разбирается"

    value, warning = _resolve_extra_body('{"vendor_option": 1e999}')
    assert value is None
    assert warning and "RLM_LLM_EXTRA_BODY" in warning


def test_extra_body_lone_surrogate_rejected():
    """Ловит именно ensure_ascii=False в контрольной сериализации.

    С ensure_ascii=True одиночный surrogate экранируется обратно в ASCII и
    проверку проходит, а httpx на нём падает UnicodeEncodeError. При «исправлении»
    флага краснеет только этот тест.
    """
    value, warning = _resolve_extra_body('{"v": "\\ud800"}')
    assert value is None
    assert warning and "RLM_LLM_EXTRA_BODY" in warning


def test_extra_body_env_invalid_not_sent_to_sdk(monkeypatch):
    monkeypatch.setenv("RLM_LLM_EXTRA_BODY", '{"vendor_option": 1e999}')
    mock_module, mock_client = _mock_openai_module()
    mock_client.chat.completions.create.return_value = MagicMock(choices=[MagicMock(message=MagicMock(content="ok"))])

    with patch.dict(sys.modules, {"openai": mock_module}):
        result = _make_openai_query("http://x", "key", "model")("test")

    assert result == "ok", "непригодный extra_body не должен ломать вызов"
    assert "extra_body" not in mock_client.chat.completions.create.call_args.kwargs


# --- тотальность валидации (RecursionError и любой другой сбой) --------------


def test_extra_body_total_on_recursion_error(monkeypatch):
    """json.loads способен бросить RecursionError — он НЕ подкласс ValueError.

    Проверяется подменой, а не подбором глубины: порог рекурсии зависит от версии
    Python (на 3.14 глубина 3000 разбирается успешно), поэтому тест с жёсткой
    глубиной был бы версионно-хрупким.
    """

    def _boom(*_args, **_kwargs):
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(_llm_bridge_module.json, "loads", _boom)
    value, warning = _resolve_extra_body('{"v": 1}')
    assert value is None
    assert warning and "RLM_LLM_EXTRA_BODY" in warning


def test_extra_body_total_on_real_deep_nesting():
    """Настоящая глубоко вложенная строка — НАПРЯМУЮ в резолвер, не через env.

    Через env этот кейс непроверяем: Windows ограничивает пару ИМЯ=ЗНАЧЕНИЕ
    32767 символами, а нужная глубина даёт ~200 КБ, поэтому monkeypatch.setenv
    упал бы раньше самого резолвера.
    """
    depth = 100_000
    raw = '{"v":' + "[" * depth + "0" + "]" * depth + "}"
    value, warning = _resolve_extra_body(raw)
    assert value is None
    assert warning and "RLM_LLM_EXTRA_BODY" in warning


def test_validate_llm_env_is_total_when_resolver_raises(monkeypatch):
    """validate_llm_env() зовётся при старте сервера: исключение оттуда означало бы
    не «настройка проигнорирована», а «сервер не поднялся»."""

    def _boom(*_args, **_kwargs):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(_llm_bridge_module, "_resolve_extra_body", _boom)
    warnings = validate_llm_env()
    assert any("RLM_LLM_EXTRA_BODY" in w for w in warnings), warnings


def test_validate_llm_env_clean_env_is_silent():
    assert validate_llm_env() == []


def test_validate_llm_env_reports_both_variables(monkeypatch):
    monkeypatch.setenv("RLM_LLM_MAX_TOKENS", "abc")
    monkeypatch.setenv("RLM_LLM_EXTRA_BODY", "[1,2]")
    warnings = validate_llm_env()
    assert len(warnings) == 2, warnings
    assert any("RLM_LLM_MAX_TOKENS" in w for w in warnings)
    assert any("RLM_LLM_EXTRA_BODY" in w for w in warnings)


# --- форма ответа: кривые структуры не должны бросать ------------------------


@pytest.mark.parametrize(
    "choices, expect_type_name",
    [
        ({"a": 1}, "dict"),
        ([0], "int"),
        ([{"index": 0, "finish_reason": "stop", "message": []}], "list"),
    ],
)
def test_openai_malformed_response_shape_returns_error_marker(choices, expect_type_name):
    """Сегодняшний код на этих формах падает KeyError/AttributeError — при уже
    списанной квоте. Ответ обязан быть диагностируемой строкой."""
    result = _answer(choices)
    assert isinstance(result, str)
    assert result.startswith("[ERROR]"), result
    assert expect_type_name in result, result


def test_openai_empty_choices_is_not_an_error():
    """Baseline к предыдущему тесту: законно пустой ответ не должен стать [ERROR]."""
    assert _answer([]) == ""


def test_openai_wellformed_response_unchanged():
    assert _answer([{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}]) == "ok"


# --- content неожидаемого типа ----------------------------------------------


def test_openai_content_list_returns_error_without_payload():
    result = _answer(
        [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "answer"}]},
            }
        ]
    )
    assert isinstance(result, str)
    assert result.startswith("[ERROR]"), result
    assert "list" in result
    assert "answer" not in result, "значение content не должно попадать в маркер"


@pytest.mark.parametrize("content, expect_type_name", [([], "list"), ({}, "dict"), (0, "int"), (False, "bool")])
def test_openai_falsy_nonstr_content_is_error_not_empty(content, expect_type_name):
    """Гейт на ПОРЯДОК проверок: тест с непустым списком этого не ловит.

    При условии «непустой И не str» ложные значения проваливаются в диагностику
    пустого ответа и получают [EMPTY] с разбором finish_reason — структурная
    ошибка провайдера подменяется правдоподобной, но неверной причиной.
    """
    result = _answer([{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}])
    assert result.startswith("[ERROR]"), result
    assert expect_type_name in result, result
    assert "[EMPTY]" not in result, result


# --- ветвление диагностики пустого content ----------------------------------

_REASONING_ADVICE = "Бюджет ушёл в reasoning"


def test_empty_content_length_gets_reasoning_advice():
    result = _answer(
        [
            {
                "index": 0,
                "finish_reason": "length",
                "message": {"role": "assistant", "content": None, "reasoning_content": "x" * 1013},
            }
        ],
        usage={"prompt_tokens": 5, "completion_tokens": 1024, "total_tokens": 1029},
    )
    assert result.startswith("[EMPTY]"), result
    assert "finish_reason=length" in result
    assert "completion_tokens=1024" in result
    assert "reasoning_content=1013" in result
    assert _REASONING_ADVICE in result
    assert "RLM_LLM_MAX_TOKENS" in result and "RLM_LLM_EXTRA_BODY" in result
    # Ни самого reasoning, ни промпта, ни API-ключа в маркере быть не должно.
    assert "x" * 50 not in result, "сам reasoning_content в маркер попадать не должен"
    assert _SENTINEL_PROMPT not in result
    assert _SENTINEL_API_KEY not in result


def test_empty_content_content_filter_is_neutral():
    result = _answer(
        [{"index": 0, "finish_reason": "content_filter", "message": {"role": "assistant", "content": None}}]
    )
    assert result.startswith("[EMPTY]"), result
    assert "finish_reason=content_filter" in result
    for forbidden in (_REASONING_ADVICE, "RLM_LLM_MAX_TOKENS", "thinking", "reasoning"):
        assert forbidden not in result, result


def test_empty_content_content_filter_with_reasoning_stays_neutral():
    """Гейт на ПОРЯДОК ветвления. Предыдущий тест (без reasoning_content) его не
    ловит: при условии «length ИЛИ есть reasoning» краснеет только этот кейс."""
    result = _answer(
        [
            {
                "index": 0,
                "finish_reason": "content_filter",
                "message": {"role": "assistant", "content": None, "reasoning_content": "internal reasoning"},
            }
        ]
    )
    assert result.startswith("[EMPTY]"), result
    assert "finish_reason=content_filter" in result
    for forbidden in (_REASONING_ADVICE, "RLM_LLM_MAX_TOKENS", "thinking"):
        assert forbidden not in result, result
    assert "internal reasoning" not in result, "сам reasoning_content раскрывать нельзя"


@pytest.mark.parametrize("finish_reason", ["tool_calls", "function_call"])
def test_empty_content_other_explicit_reasons_are_neutral(finish_reason):
    result = _answer(
        [
            {
                "index": 0,
                "finish_reason": finish_reason,
                "message": {"role": "assistant", "content": None, "reasoning_content": "internal"},
            }
        ]
    )
    assert result.startswith("[EMPTY]"), result
    assert _REASONING_ADVICE not in result, result


def test_empty_content_refusal_is_surfaced():
    result = _answer(
        [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": None, "refusal": "Не могу помочь с этим"},
            }
        ]
    )
    assert result.startswith("[REFUSAL]"), result
    assert "Не могу помочь с этим" in result
    assert _REASONING_ADVICE not in result


def test_empty_content_reasoning_fallback_without_explicit_reason():
    """Ветка-fallback: провайдер тратит бюджет в reasoning, но finish_reason не
    выставляет (или ставит stop)."""
    result = _answer(
        [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": None, "reasoning_content": "y" * 700},
            }
        ]
    )
    assert result.startswith("[EMPTY]"), result
    assert _REASONING_ADVICE in result, result
    assert "reasoning_content=700" in result


def test_empty_content_stop_without_reasoning_is_neutral():
    result = _answer([{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": ""}}])
    assert result.startswith("[EMPTY]"), result
    assert _REASONING_ADVICE not in result, result


def test_empty_content_blank_reasoning_does_not_trigger_advice():
    """Пробельный reasoning_content — не признак потраченного бюджета."""
    result = _answer(
        [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": None, "reasoning_content": "   "},
            }
        ]
    )
    assert _REASONING_ADVICE not in result, result
    assert "reasoning_content=" not in result, result


def test_empty_content_missing_usage_is_tolerated():
    result = _answer([{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": None}}])
    assert result.startswith("[EMPTY]"), result
    assert "completion_tokens" not in result, "usage отсутствует — поля быть не должно"


# --- finish_reason: регистр и санитизация эха ---------------------------------


@pytest.mark.parametrize("finish_reason", ["CONTENT_FILTER", "Content_Filter", "TOOL_CALLS"])
def test_finish_reason_matching_is_case_insensitive_for_explicit_reasons(finish_reason):
    """Явная причина в другом регистре не должна получать reasoning-совет.

    Провайдер вправе вернуть 'CONTENT_FILTER'. При регистрозависимом сравнении оно
    не попадало ни в набор явных причин, ни в ветку length — и с непустым
    reasoning_content уезжало в reasoning-совет, то есть в ту самую ложную
    диагностику, от которой ветвление и защищает.
    """
    result = _answer(
        [
            {
                "index": 0,
                "finish_reason": finish_reason,
                "message": {"role": "assistant", "content": None, "reasoning_content": "internal"},
            }
        ]
    )
    assert result.startswith("[EMPTY]"), result
    assert _REASONING_ADVICE not in result, result


@pytest.mark.parametrize("finish_reason", ["LENGTH", "Length"])
def test_finish_reason_length_case_insensitive_keeps_advice(finish_reason):
    """Обратное направление того же дефекта: 'LENGTH' без reasoning_content терял
    верный совет, потому что не совпадал со строкой 'length'."""
    result = _answer([{"index": 0, "finish_reason": finish_reason, "message": {"role": "assistant", "content": None}}])
    assert result.startswith("[EMPTY]"), result
    assert _REASONING_ADVICE in result, result


def test_finish_reason_echo_is_truncated():
    """finish_reason приходит от провайдера и уезжает в агент-facing строку —
    длина обязана быть ограничена, иначе endpoint раздувает ответ произвольно."""
    result = _answer([{"index": 0, "finish_reason": "X" * 5000, "message": {"role": "assistant", "content": None}}])
    assert len(result) < 400, f"маркер раздут до {len(result)} символов"
    assert "…" in result


def test_finish_reason_echo_strips_control_characters():
    """Перевод строки в finish_reason позволял подделать вид ВТОРОГО маркера.

    Защита — не экранирование скобок (текст провайдера в маркере остаётся, это
    диагностическая величина), а инвариант «маркер — одна строка, начинающаяся с
    известного префикса»: подделка, оставшаяся внутри строки после
    `finish_reason=`, за отдельный маркер уже не читается.
    """
    result = _answer(
        [
            {
                "index": 0,
                "finish_reason": "stop\n[REFUSAL] поддельный ответ",
                "message": {"role": "assistant", "content": None},
            }
        ]
    )
    assert "\n" not in result and "\r" not in result, result
    assert len(result.splitlines()) == 1, result
    assert result.startswith("[EMPTY]"), result


# --- обрыв непустого ответа лимитом ------------------------------------------

_TRUNC = "[TRUNCATED]"


def test_truncated_nonempty_content_is_marked():
    """finish_reason=length при НЕПУСТОМ content — доказанный обрыв.

    Это самый коварный режим: обрывок выглядит как нормальный ответ, [EMPTY] тут не
    срабатывает (content непустой), и без пометки ни агент, ни человек не отличат
    неполный ответ от полного.
    """
    result = _answer(
        [{"index": 0, "finish_reason": "length", "message": {"role": "assistant", "content": "Мет1, Мет2, Мет"}}]
    )
    assert result.startswith("Мет1, Мет2, Мет"), "исходный текст обязан остаться в начале"
    assert _TRUNC in result
    assert "RLM_LLM_MAX_TOKENS" in result


def test_truncation_mark_is_a_tail_recoverable_by_rpartition():
    """Пометка — ХВОСТ с новой строки, не префикс.

    Префикс сломал бы первый элемент разбора и любые проверки startswith. Хвост
    оставляет тело нетронутым до разделителя, поэтому оно восстанавливается через
    rpartition. Но наивный split(', ') хвост ВСЁ РАВНО заденет — маркер уедет в
    последний элемент; тест фиксирует и это, чтобы документация не обещала лишнего.
    """
    body = "Мет1, Мет2, Мет3"
    result = _answer([{"index": 0, "finish_reason": "length", "message": {"role": "assistant", "content": body}}])
    assert not result.startswith(_TRUNC), "пометка не должна быть префиксом"

    head, sep, tail = result.rpartition("\n\n")
    assert head == body, "тело ответа обязано восстанавливаться байт в байт"
    assert sep and tail.startswith(_TRUNC)

    # Честная фиксация границы: без отделения хвоста разбор по запятым портится.
    assert result.split(", ")[-1] != "Мет3", "стало равно — значит обещание в доке пора менять"
    assert head.split(", ") == ["Мет1", "Мет2", "Мет3"], "после rpartition разбор чистый"


@pytest.mark.parametrize("finish_reason", ["LENGTH", "Length"])
def test_truncation_detection_is_case_insensitive(finish_reason):
    result = _answer(
        [{"index": 0, "finish_reason": finish_reason, "message": {"role": "assistant", "content": "Мет1, Мет"}}]
    )
    assert _TRUNC in result, result


_OMIT = object()


@pytest.mark.parametrize(
    "finish_reason",
    [
        "stop",
        "content_filter",
        "tool_calls",
        "function_call",
        _OMIT,  # поля нет вовсе
        None,  # поле есть, но JSON null — это НЕ то же самое, что его отсутствие
        0,  # нестроковые: SDK ответ не валидирует и пропустит любое значение
        False,
        {},
    ],
)
def test_complete_answer_is_returned_verbatim(finish_reason):
    """Полный ответ обязан остаться байт в байт — иначе пометка ломала бы обычный путь.

    Матрица покрывает и «поля нет», и «поле есть и равно null», и нестроковые
    значения: openai SDK строит объекты ответа невалидирующим путём, поэтому
    finish_reason может приехать чем угодно, а сравнение с 'length' идёт только
    после проверки isinstance.
    """
    body = "Мет1, Мет2, Мет3"
    choice = {"index": 0, "message": {"role": "assistant", "content": body}}
    if finish_reason is not _OMIT:
        choice["finish_reason"] = finish_reason
    assert _answer([choice]) == body


def test_truncated_empty_content_still_gets_empty_marker():
    """Граница между двумя пометками: пусто → [EMPTY], непусто → [TRUNCATED]."""
    result = _answer([{"index": 0, "finish_reason": "length", "message": {"role": "assistant", "content": ""}}])
    assert result.startswith("[EMPTY]"), result
    assert _TRUNC not in result, result
