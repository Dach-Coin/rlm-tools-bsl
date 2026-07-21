"""Конфигурация sandbox-инфраструктуры (v1.29.0, процессная изоляция).

Leaf-модуль (только stdlib): разбор и валидация env-переменных `RLM_SANDBOX_*`.
Вызывается из `server.main()` (fail-fast до принятия tool calls) и из
backend-фабрики `_rlm_start`. Невалидное значение любой переменной — это
`SandboxConfigError`, никогда не молчаливый fallback (решение §23.2 плана
v1.29.0): опечатка в `RLM_SANDBOX_MODE` не имеет права незаметно отключить
процессную изоляцию.

Семантика `0` описана per-переменно: у `RLM_SANDBOX_MEMORY_MB` и
`RLM_SANDBOX_MAX_PROCESSES` ноль означает «лимит отключён» (с warning на
стороне вызывающего), у остальных ноль/отрицательное — ошибка конфигурации.
"""

from __future__ import annotations

import os

# v1.29.0: production default — процессная изоляция. "inline" остаётся только
# как явный диагностический/аварийный режим (громкий warning на старте) и
# автоматическим fallback-ом НИКОГДА не становится (§23.1/§23.2).
DEFAULT_SANDBOX_MODE = "process"

_ALLOWED_MODES = ("process", "inline")


class SandboxConfigError(ValueError):
    """Невалидная конфигурация sandbox — fail-fast startup error, не fallback."""


def _int_env(name: str, default: int, *, min_value: int, max_value: int | None = None, allow_zero: bool = False) -> int:
    """Целое из env с жёсткой валидацией.

    Отсутствие/пустая строка → default. Не-целое, значение < min_value
    (кроме разрешённого нуля) или > max_value → SandboxConfigError.
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        val = int(raw.strip())
    except ValueError:
        raise SandboxConfigError(f"{name}={raw!r}: ожидается целое число") from None
    if allow_zero and val == 0:
        return 0
    if val < min_value:
        raise SandboxConfigError(
            f"{name}={val}: должно быть >= {min_value}" + (" (или 0 — отключено)" if allow_zero else "")
        )
    if max_value is not None and val > max_value:
        raise SandboxConfigError(f"{name}={val}: должно быть <= {max_value}")
    return val


def get_sandbox_mode() -> str:
    """Режим песочницы: "process" | "inline".

    Отсутствие переменной → DEFAULT_SANDBOX_MODE. Любое другое значение,
    включая явно заданную пустую строку и опечатки, — SandboxConfigError
    (fail-fast, без fallback к inline — §11.1/§23.2 плана).
    """
    raw = os.environ.get("RLM_SANDBOX_MODE")
    if raw is None:
        return DEFAULT_SANDBOX_MODE
    mode = raw.strip().lower()
    if mode not in _ALLOWED_MODES:
        raise SandboxConfigError(
            f"RLM_SANDBOX_MODE={raw!r}: допустимы только 'process' и 'inline' (без fallback; уберите переменную для default '{DEFAULT_SANDBOX_MODE}')"
        )
    return mode


def start_timeout_seconds() -> int:
    """Timeout инициализации worker (не равен execution timeout). Default 60."""
    return _int_env("RLM_SANDBOX_START_TIMEOUT_SECONDS", 60, min_value=1, max_value=3600)


def kill_grace_seconds() -> int:
    """Короткий grace между terminate и hard kill. Default 1."""
    return _int_env("RLM_SANDBOX_KILL_GRACE_SECONDS", 1, min_value=0, allow_zero=True, max_value=60)


def shutdown_deadline_seconds() -> int:
    """ЕДИНЫЙ deadline остановки всех workers при server shutdown (не per-worker).

    Строго целое 1..60, default 10 (§23.13 плана).
    """
    return _int_env("RLM_SANDBOX_SHUTDOWN_DEADLINE_SECONDS", 10, min_value=1, max_value=60)


def memory_mb() -> int:
    """Per-worker memory ceiling в MB; 0 отключает лимит (caller пишет warning)."""
    return _int_env("RLM_SANDBOX_MEMORY_MB", 1024, min_value=16, allow_zero=True)


def ipc_max_bytes() -> int:
    """Максимальный размер одного IPC JSON frame (bytes).

    Floor 256 KiB: init_ok несёт registry snapshot (53 хелпера с recipe) ~66 KiB —
    меньший лимит сделал бы worker незапускаемым.
    """
    return _int_env("RLM_SANDBOX_IPC_MAX_BYTES", 4 * 1024 * 1024, min_value=256 * 1024)


def max_code_chars() -> int:
    """Максимальная длина code, принимаемая parent до отправки worker."""
    return _int_env("RLM_SANDBOX_MAX_CODE_CHARS", 1_000_000, min_value=1_000)


def max_processes() -> int:
    """Windows Job Object active-process limit; 0 отключает.

    POSIX RLIMIT_NPROC сознательно НЕ применяется (на ряде систем он считается
    per-UID и затронул бы соседние workers — §11.5 плана).
    """
    return _int_env("RLM_SANDBOX_MAX_PROCESSES", 16, min_value=1, allow_zero=True, max_value=1024)


def validate_sandbox_env() -> dict:
    """Разобрать ВСЕ sandbox-переменные разом (fail-fast точка для server.main()).

    Возвращает snapshot значений для стартового лога/диагностики; секретов не
    содержит. Любая невалидная переменная → SandboxConfigError.
    """
    return {
        "mode": get_sandbox_mode(),
        "start_timeout_seconds": start_timeout_seconds(),
        "kill_grace_seconds": kill_grace_seconds(),
        "shutdown_deadline_seconds": shutdown_deadline_seconds(),
        "memory_mb": memory_mb(),
        "ipc_max_bytes": ipc_max_bytes(),
        "max_code_chars": max_code_chars(),
        "max_processes": max_processes(),
    }
