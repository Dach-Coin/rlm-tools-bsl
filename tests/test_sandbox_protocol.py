"""v1.29.0 этап 3: IPC codec/schema (_sandbox_protocol) — codec не принимает
произвольные/огромные сообщения (§17 этап 3)."""

import json

import pytest

from rlm_tools_bsl._sandbox_protocol import (
    MAX_ERROR_TEXT_CHARS,
    PARENT_TO_WORKER_TYPES,
    PROTOCOL_VERSION,
    WORKER_TO_PARENT_TYPES,
    SandboxProtocolError,
    bounded_text,
    decode_frame,
    encode_frame,
    make_message,
    validate_message,
)

MAX = 1024 * 1024


def _frame(**overrides):
    msg = make_message("execute", 7, 3, {"code": "print(1)"})
    msg.update(overrides)
    return msg


def test_unicode_roundtrip():
    payload = {"code": "print('кириллица 🎉')", "note": "ё/emoji"}
    msg = make_message("execute", 1, 1, payload)
    data = encode_frame(msg, MAX)
    decoded = decode_frame(data, MAX)
    assert decoded == msg
    t, p = validate_message(decoded, allowed_types={"execute"}, expected_request_id=1, expected_generation=1)
    assert t == "execute" and p == payload


def test_max_size_boundary_accepted():
    msg = make_message("execute", 1, 1, {"code": "x"})
    base_len = len(json.dumps(msg, ensure_ascii=False).encode("utf-8"))
    pad = MAX - base_len
    msg["payload"]["code"] = "x" * (pad + 1)  # ровно MAX bytes итог
    data = encode_frame(msg, MAX)
    assert len(data) == MAX
    assert decode_frame(data, MAX)["payload"]["code"].startswith("x")


def test_oversized_frame_rejected_on_encode_and_decode():
    msg = make_message("execute", 1, 1, {"code": "x" * MAX})
    with pytest.raises(SandboxProtocolError, match="too large"):
        encode_frame(msg, MAX)
    big = b"a" * (MAX + 1)
    with pytest.raises(SandboxProtocolError, match="too large"):
        decode_frame(big, MAX)


def test_invalid_utf8_rejected():
    with pytest.raises(SandboxProtocolError, match="UTF-8"):
        decode_frame(b"\xff\xfe{}", MAX)


def test_invalid_json_rejected():
    with pytest.raises(SandboxProtocolError, match="JSON"):
        decode_frame(b"{not json", MAX)


def test_json_list_instead_of_object_rejected():
    with pytest.raises(SandboxProtocolError, match="not a JSON object"):
        decode_frame(b"[1, 2, 3]", MAX)


def test_non_bytes_rejected():
    with pytest.raises(SandboxProtocolError, match="not bytes"):
        decode_frame("string", MAX)  # type: ignore[arg-type]


@pytest.mark.parametrize("missing", ["protocol_version", "type", "request_id", "generation"])
def test_missing_required_field_rejected(missing):
    msg = _frame()
    del msg[missing]
    with pytest.raises(SandboxProtocolError):
        validate_message(msg, allowed_types={"execute"}, expected_request_id=7, expected_generation=3)


def test_wrong_version_rejected():
    msg = _frame(protocol_version=PROTOCOL_VERSION + 1)
    with pytest.raises(SandboxProtocolError, match="protocol_version"):
        validate_message(msg, allowed_types={"execute"})


def test_unknown_type_rejected():
    msg = _frame(type="drop_tables")
    with pytest.raises(SandboxProtocolError, match="unknown message type"):
        validate_message(msg, allowed_types={"execute"})


def test_known_but_unexpected_type_for_state_rejected():
    # init — валидный тип протокола, но недопустим в состоянии command-loop.
    msg = _frame(type="init")
    with pytest.raises(SandboxProtocolError, match="unexpected message type"):
        validate_message(msg, allowed_types={"execute", "shutdown", "ping"})


def test_wrong_request_id_rejected():
    with pytest.raises(SandboxProtocolError, match="request_id mismatch"):
        validate_message(_frame(), allowed_types={"execute"}, expected_request_id=8)


def test_wrong_generation_rejected():
    with pytest.raises(SandboxProtocolError, match="generation mismatch"):
        validate_message(_frame(), allowed_types={"execute"}, expected_generation=4)


def test_non_int_request_id_and_generation_rejected():
    with pytest.raises(SandboxProtocolError, match="request_id"):
        validate_message(_frame(request_id="7"), allowed_types={"execute"})
    with pytest.raises(SandboxProtocolError, match="generation"):
        validate_message(_frame(generation=None), allowed_types={"execute"})


@pytest.mark.parametrize("field", ["protocol_version", "request_id", "generation"])
def test_bool_is_not_accepted_as_protocol_integer(field):
    msg = _frame(request_id=1, generation=1)
    msg[field] = True
    with pytest.raises(SandboxProtocolError):
        validate_message(msg, allowed_types={"execute"}, expected_request_id=1, expected_generation=1)


def test_payload_must_be_object():
    msg = _frame(payload=[1, 2])
    with pytest.raises(SandboxProtocolError, match="payload"):
        validate_message(msg, allowed_types={"execute"})


def test_too_deep_structure_rejected():
    deep: dict = {"x": 1}
    for _ in range(40):
        deep = {"n": deep}
    frame = json.dumps(make_message("execute", 1, 1, deep), ensure_ascii=False).encode("utf-8")
    with pytest.raises(SandboxProtocolError, match="deeper"):
        decode_frame(frame, MAX)


def test_error_text_cap():
    long_text = "э" * (MAX_ERROR_TEXT_CHARS + 500)
    bounded = bounded_text(long_text)
    assert len(bounded) <= MAX_ERROR_TEXT_CHARS + len("… [truncated]")
    assert bounded.endswith("… [truncated]")
    assert bounded_text("short") == "short"


def test_allowlists_are_disjoint_and_complete():
    assert PARENT_TO_WORKER_TYPES == {"init", "execute", "shutdown", "ping"}
    assert WORKER_TO_PARENT_TYPES == {"init_ok", "init_error", "execute_result", "worker_error", "shutdown_ok", "pong"}
    assert not (PARENT_TO_WORKER_TYPES & WORKER_TO_PARENT_TYPES)


# ---------------------------------------------------------------------------
# Строгая схема: json допускает NaN/Infinity, но это не валидный JSON
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_decode_rejects_non_json_constants(literal):
    raw = (
        '{"protocol_version":1,"type":"execute_result","request_id":1,"generation":1,"payload":{"x":%s}}' % literal
    ).encode("utf-8")
    with pytest.raises(SandboxProtocolError, match="non-JSON constant"):
        decode_frame(raw, 1_000_000)


def test_decode_accepts_normal_floats():
    raw = b'{"protocol_version":1,"type":"execute_result","request_id":1,"generation":1,"payload":{"x":1.5}}'
    assert decode_frame(raw, 1_000_000)["payload"]["x"] == 1.5
