"""IPC-протокол parent ↔ sandbox worker (v1.29.0).

Leaf-модуль (только stdlib). После старта worker все сообщения — UTF-8 JSON
bytes поверх ``Connection.send_bytes``/``recv_bytes(maxlength=...)``; pickle
для runtime-сообщений запрещён (§4.4/§7.1 плана): parent никогда не вызывает
``recv()``/``send()`` с произвольными Python-объектами от уже запущенного
worker. Каждое сообщение несёт protocol version, type из allowlist,
request_id и generation; нарушение любой проверки — ``SandboxProtocolError``,
после которого конкретный worker уничтожается (§7.7).
"""

from __future__ import annotations

import json

PROTOCOL_VERSION = 1

# Allowlist типов по направлениям (§7.3).
PARENT_TO_WORKER_TYPES = frozenset({"init", "execute", "shutdown", "ping"})
WORKER_TO_PARENT_TYPES = frozenset({"init_ok", "init_error", "execute_result", "worker_error", "shutdown_ok", "pong"})

# Cap на тексты ошибок внутри frames — диагностика bounded, огромный traceback/
# payload не путешествует по IPC и не попадает в лог целиком (§7.7/§20).
MAX_ERROR_TEXT_CHARS = 4_000

# request_id, которым worker помечает фатальный ``worker_error`` из внешнего
# обработчика — там id текущей команды может быть ещё/уже неизвестен. Родитель
# принимает от worker_error ТОЛЬКО это значение или id текущего запроса.
WORKER_FATAL_REQUEST_ID = -1

# Защита от патологически глубоких структур в decoded payload.
_MAX_DEPTH = 32


class SandboxProtocolError(RuntimeError):
    """Нарушение IPC-протокола: worker подлежит уничтожению, сессия получает controlled error."""


def bounded_text(text: str, cap: int = MAX_ERROR_TEXT_CHARS) -> str:
    """Обрезать диагностический текст до cap символов с маркером усечения."""
    if not isinstance(text, str):
        text = str(text)
    if len(text) <= cap:
        return text
    return text[:cap] + "… [truncated]"


def make_message(msg_type: str, request_id: int, generation: int, payload: dict | None = None) -> dict:
    msg: dict = {
        "protocol_version": PROTOCOL_VERSION,
        "type": msg_type,
        "request_id": request_id,
        "generation": generation,
    }
    if payload is not None:
        msg["payload"] = payload
    return msg


def _reject_constant(name: str):
    raise SandboxProtocolError(f"frame contains non-JSON constant: {name}")


def _check_depth(obj, depth: int = 0) -> None:
    if depth > _MAX_DEPTH:
        raise SandboxProtocolError(f"frame structure deeper than {_MAX_DEPTH} levels")
    if isinstance(obj, dict):
        for v in obj.values():
            _check_depth(v, depth + 1)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _check_depth(v, depth + 1)


def encode_frame(msg: dict, max_bytes: int) -> bytes:
    """Сериализовать сообщение в UTF-8 JSON bytes с проверкой размера."""
    try:
        # allow_nan=False — симметрично parse_constant в decode_frame: иначе мы
        # сами генерировали бы NaN/Infinity, которые наша же сторона отвергает.
        data = json.dumps(msg, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise SandboxProtocolError(f"frame is not JSON-serializable: {type(exc).__name__}: {exc}") from None
    if len(data) > max_bytes:
        raise SandboxProtocolError(f"frame too large: {len(data)} > {max_bytes} bytes")
    return data


def decode_frame(data, max_bytes: int) -> dict:
    """Разобрать и структурно проверить входящий frame.

    Только длина/UTF-8/JSON/тип-объект/глубина; семантику (type/request_id/
    generation) проверяет ``validate_message`` — вызывающий знает своё
    ожидаемое состояние.
    """
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise SandboxProtocolError(f"frame is not bytes: {type(data).__name__}")
    raw = bytes(data)
    if len(raw) > max_bytes:
        raise SandboxProtocolError(f"frame too large: {len(raw)} > {max_bytes} bytes")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise SandboxProtocolError("frame is not valid UTF-8") from None
    try:
        # parse_constant отсекает NaN/Infinity/-Infinity: стандартный json их
        # принимает, хотя это не валидный JSON, а дальше они уехали бы в ответ
        # агенту и в json.dumps без ошибки.
        obj = json.loads(text, parse_constant=_reject_constant)
    except SandboxProtocolError:
        raise
    except (ValueError, RecursionError):
        raise SandboxProtocolError("frame is not valid JSON") from None
    if not isinstance(obj, dict):
        raise SandboxProtocolError(f"frame is not a JSON object: {type(obj).__name__}")
    _check_depth(obj)
    return obj


def validate_message(
    msg: dict,
    *,
    allowed_types: frozenset[str] | set[str],
    expected_request_id: int | None = None,
    expected_generation: int | None = None,
) -> tuple[str, dict]:
    """Проверить version/type/request_id/generation; вернуть ``(type, payload)``.

    ``allowed_types`` — допустимые типы В ТЕКУЩЕМ СОСТОЯНИИ вызывающего (не весь
    направленный allowlist): например, после ``execute`` parent принимает только
    ``execute_result``/``worker_error``.
    """
    version = msg.get("protocol_version")
    # bool — подкласс int и True == 1, поэтому одной проверки равенства
    # недостаточно: JSON true не должен становиться protocol_version=1.
    if type(version) is not int or version != PROTOCOL_VERSION:
        raise SandboxProtocolError(f"unsupported protocol_version: {version!r} (expected {PROTOCOL_VERSION})")
    msg_type = msg.get("type")
    if not isinstance(msg_type, str) or msg_type not in (PARENT_TO_WORKER_TYPES | WORKER_TO_PARENT_TYPES):
        raise SandboxProtocolError(f"unknown message type: {msg_type!r}")
    if msg_type not in allowed_types:
        raise SandboxProtocolError(f"unexpected message type for current state: {msg_type!r}")
    request_id = msg.get("request_id")
    if type(request_id) is not int:
        raise SandboxProtocolError(f"missing/invalid request_id: {request_id!r}")
    if expected_request_id is not None and request_id != expected_request_id:
        raise SandboxProtocolError(f"request_id mismatch: got {request_id}, expected {expected_request_id}")
    generation = msg.get("generation")
    if type(generation) is not int:
        raise SandboxProtocolError(f"missing/invalid generation: {generation!r}")
    if expected_generation is not None and generation != expected_generation:
        raise SandboxProtocolError(f"generation mismatch: got {generation}, expected {expected_generation}")
    payload = msg.get("payload")
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise SandboxProtocolError(f"payload is not an object: {type(payload).__name__}")
    return msg_type, payload
