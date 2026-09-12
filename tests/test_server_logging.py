"""v1.24.0 #8 — narrow asyncio ConnectionResetError [WinError 10054] log filter.

On Windows ProactorEventLoop, benign HTTP-connection teardown raises
ConnectionResetError inside ``_call_connection_lost``; the default asyncio handler
logs it with a traceback, spamming server.log. The filter suppresses ONLY that
benign teardown noise — any other asyncio error must pass through untouched.
"""

from __future__ import annotations

import logging
import pathlib
import sys
from unittest.mock import patch

import pytest

from rlm_tools_bsl import server
from rlm_tools_bsl.server import (
    _AsyncioConnResetFilter,
    _install_asyncio_conn_reset_filter,
)


class _BenignWinReset(ConnectionResetError):
    """Portable stand-in for the Windows teardown error.

    Setting ``.winerror`` directly is non-portable: the 4-arg OSError form only
    populates ``winerror`` on Windows, and the base ``winerror`` getset-descriptor
    is not writable. A plain class attribute resolves ahead of that descriptor in
    the MRO on every platform, so the filter sees ``winerror == 10054`` on Linux/macOS CI too.
    """

    winerror = 10054


def _call_connection_lost(exc):
    # Named exactly like the asyncio teardown callback so the traceback carries it.
    raise exc


def _record_for(exc, msg="Fatal error on transport"):
    try:
        _call_connection_lost(exc)
    except BaseException:  # noqa: BLE001
        exc_info = sys.exc_info()
    return logging.LogRecord("asyncio", logging.ERROR, __file__, 0, msg, None, exc_info)


def test_suppresses_benign_winerror_teardown():
    # winerror=10054 via a portable subclass (see _BenignWinReset) — errno unset.
    exc = _BenignWinReset("An existing connection was forcibly closed")
    assert getattr(exc, "winerror", None) == 10054  # portable across OSes
    rec = _record_for(exc)
    assert _AsyncioConnResetFilter().filter(rec) is False


def test_suppresses_benign_errno_teardown():
    exc = ConnectionResetError(10054, "An existing connection was forcibly closed")
    rec = _record_for(exc)
    assert _AsyncioConnResetFilter().filter(rec) is False


def test_passes_conn_reset_with_other_errno():
    exc = ConnectionResetError(99, "some other reset")
    rec = _record_for(exc)
    assert _AsyncioConnResetFilter().filter(rec) is True


def test_passes_other_asyncio_error():
    # Same teardown frame but a different exception type → must NOT be suppressed.
    exc = ValueError("totally different problem")
    rec = _record_for(exc)
    assert _AsyncioConnResetFilter().filter(rec) is True


def test_passes_conn_reset_without_teardown_frame():
    # Right errno, but not the _call_connection_lost teardown path → keep it.
    exc = ConnectionResetError(10054, "reset elsewhere")
    try:
        raise exc
    except BaseException:  # noqa: BLE001
        exc_info = sys.exc_info()
    rec = logging.LogRecord("asyncio", logging.ERROR, __file__, 0, "msg", None, exc_info)
    assert _AsyncioConnResetFilter().filter(rec) is True


def test_record_without_exc_info_passes():
    rec = logging.LogRecord("asyncio", logging.INFO, __file__, 0, "plain message", None, None)
    assert _AsyncioConnResetFilter().filter(rec) is True


def test_install_is_idempotent():
    asyncio_logger = logging.getLogger("asyncio")
    before = [f for f in asyncio_logger.filters if isinstance(f, _AsyncioConnResetFilter)]
    for f in before:
        asyncio_logger.removeFilter(f)
    try:
        _install_asyncio_conn_reset_filter()
        _install_asyncio_conn_reset_filter()
        ours = [f for f in asyncio_logger.filters if isinstance(f, _AsyncioConnResetFilter)]
        assert len(ours) == 1
    finally:
        for f in [f for f in asyncio_logger.filters if isinstance(f, _AsyncioConnResetFilter)]:
            asyncio_logger.removeFilter(f)


# ---------------------------------------------------------------------------
# v1.35.2 (#36): стартовый снимок окружения. Под stdio server.log не ведётся
# вовсе, поэтому единственный способ увидеть фактические корни — одна строка
# INFO, печатаемая для ОБОИХ транспортов.
# ---------------------------------------------------------------------------


def _startup_lines(caplog):
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("startup: ")]


@pytest.fixture(autouse=True)
def _isolate_sandbox_backend_lifecycle(monkeypatch):
    """Synthetic main() lifecycles must not leak shutdown state to neighbours.

    main() заканчивается общим shutdown, который закрывает реестр backend-ов
    (`_sandbox_registry_accepting = False`). Без восстановления КАЖДЫЙ следующий
    `rlm_start` в том же процессе получал бы controlled error — тот же приём уже
    стоит в tests/test_server.py и tests/test_server_main.py.
    """
    monkeypatch.setattr(server, "_sandbox_registry_accepting", True)


def _run_main(argv):
    """Синтетический прогон main() без побочных эффектов на соседей.

    `build_session_manager_from_env` подменяется, чтобы глобальный менеджер
    сессий остался прежним; `_prepare_stdio_transport` — чтобы тест не уводил
    реальные дескрипторы 0/1 (под `pytest -s` они настоящие).

    Обработчики, добавленные на root-логгер, снимаются здесь же: под
    `streamable-http` main() зовёт настоящую `_setup_file_logging()`, а она
    вешает `RotatingFileHandler` и НЕ снимает — иначе все последующие тесты
    сессии дописывали бы записи в файл этого теста и держали дескриптор
    открытым до выхода процесса. Снимаются ТОЛЬКО добавленные: обработчик
    caplog уже стоит к моменту снимка и переживает уборку.
    """
    root_logger = logging.getLogger()
    before = list(root_logger.handlers)
    try:
        with (
            patch.object(server.mcp, "run"),
            patch.object(sys, "argv", argv),
            patch.object(server, "build_session_manager_from_env", lambda: server.session_manager),
            patch.object(server, "_prepare_stdio_transport", lambda: (None, None)),
            patch("rlm_tools_bsl.cache.cleanup_stale_cache", return_value={"disabled": True, "roots": []}),
        ):
            server.main()
    finally:
        for handler in list(root_logger.handlers):
            if handler not in before:
                root_logger.removeHandler(handler)
                try:
                    handler.close()
                except Exception:
                    pass


@pytest.fixture
def _restore_mcp_settings():
    host, port = server.mcp.settings.host, server.mcp.settings.port
    yield
    server.mcp.settings.host, server.mcp.settings.port = host, port


@pytest.mark.parametrize(
    "argv",
    [
        ["rlm-tools-bsl"],
        ["rlm-tools-bsl", "--transport", "streamable-http"],
    ],
    ids=["stdio", "streamable-http"],
)
def test_startup_line_present_for_both_transports(caplog, argv, _restore_mcp_settings):
    """Строка обязана быть ровно одна и нести корни, режим и стратегию.

    Читаем через caplog, а НЕ из файла: main() под streamable-http вешает
    RotatingFileHandler на root-логгер и не снимает его.
    """
    with caplog.at_level(logging.INFO, logger="rlm_tools_bsl.server"):
        _run_main(argv)

    lines = _startup_lines(caplog)
    assert len(lines) == 1, lines
    line = lines[0]
    for key in ("version=", "transport=", "sandbox_mode=", "strategy_mode=", "index_root=", "cache_root=", "log="):
        assert key in line, (key, line)


def test_startup_line_does_not_leak_secrets(caplog, monkeypatch, _restore_mcp_settings):
    """В строке — только ИМЕНА переменных и два производных корня, без секретов."""
    monkeypatch.setenv("RLM_LLM_API_KEY", "super-secret-llm-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "super-secret-anthropic-key")
    monkeypatch.setenv("RLM_LLM_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("RLM_LLM_MODEL", "test/model")

    with caplog.at_level(logging.INFO, logger="rlm_tools_bsl.server"):
        _run_main(["rlm-tools-bsl"])

    lines = _startup_lines(caplog)
    assert len(lines) == 1, lines
    assert "super-secret-llm-key" not in lines[0]
    assert "super-secret-anthropic-key" not in lines[0]


def test_startup_line_distinguishes_unset_from_blank(caplog, monkeypatch, _restore_mcp_settings):
    """ "Не задана" и "задана пустой" — РАЗНЫЕ состояния: второе тенит .env."""
    monkeypatch.delenv("RLM_CONFIG_FILE", raising=False)
    monkeypatch.setenv("RLM_INDEX_DIR", "   ")

    with caplog.at_level(logging.INFO, logger="rlm_tools_bsl.server"):
        server._log_effective_env("stdio")

    lines = _startup_lines(caplog)
    assert len(lines) == 1, lines
    assert "RLM_CONFIG_FILE=not set" in lines[0], lines[0]
    assert "RLM_INDEX_DIR=(blank)" in lines[0], lines[0]
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("RLM_INDEX_DIR" in w for w in warnings), warnings


def test_startup_line_log_target_follows_transport(caplog, monkeypatch, tmp_path, _restore_mcp_settings):
    config_file = tmp_path / "cfg" / "service.json"
    config_file.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("RLM_CONFIG_FILE", str(config_file))

    with caplog.at_level(logging.INFO, logger="rlm_tools_bsl.server"):
        server._log_effective_env("stdio")
    assert "log=stderr" in _startup_lines(caplog)[0]

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="rlm_tools_bsl.server"):
        server._log_effective_env("streamable-http")
    assert f"log={config_file.parent / 'logs' / 'server.log'}" in _startup_lines(caplog)[0]


# ---------------------------------------------------------------------------
# Шаг 6.8: tripwire на ребро server <-> service.
# ---------------------------------------------------------------------------


def test_server_log_path_matches_service_config_dir(monkeypatch, tmp_path):
    """Правило "logs рядом с service.json" одинаково у server и service.

    ТОЛЬКО при ЗАДАННОЙ абсолютной RLM_CONFIG_FILE. Состояние "не задана"
    порядкозависимо: service.CONFIG_FILE вычисляется через Path.home() при
    ИМПОРТЕ модуля, а autouse-фикстура подменяет Path.home в каждом тесте — под
    pytest-randomly сравнение стало бы флейком. Относительное значение тоже не
    сравнивается наивно: service._config_path() абсолютизирует, а
    _setup_file_logging — нет.

    Честная граница: tripwire запирает 2 ребра из 3 — литеральные выражения в
    _service_win.py он не исполняет. Полное слияние — в бэклоге.
    """
    from rlm_tools_bsl import service

    config_file = tmp_path / "svc" / "service.json"
    config_file.parent.mkdir(parents=True, exist_ok=True)
    assert config_file.is_absolute()
    monkeypatch.setenv("RLM_CONFIG_FILE", str(config_file))

    assert server._server_log_path() == service._config_path().parent / "logs" / "server.log"


def test_setup_file_logging_uses_server_log_path(monkeypatch, tmp_path):
    """_setup_file_logging обязана брать путь из ЕДИНОЙ функции, а не из копии правила."""
    config_file = tmp_path / "cfg2" / "service.json"
    config_file.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("RLM_CONFIG_FILE", str(config_file))

    expected = server._server_log_path()
    root_logger = logging.getLogger()
    before = list(root_logger.handlers)
    try:
        server._setup_file_logging()
        assert expected.exists(), f"{expected} не создан"
        assert expected.parent.is_dir()
    finally:
        for h in list(root_logger.handlers):
            if h not in before:
                root_logger.removeHandler(h)
                try:
                    h.close()
                except Exception:
                    pass


def test_server_log_path_default_without_config_file(monkeypatch):
    """Без RLM_CONFIG_FILE — ~/.config/rlm-tools-bsl/logs/server.log (home пропатчен)."""
    monkeypatch.delenv("RLM_CONFIG_FILE", raising=False)
    assert server._server_log_path() == pathlib.Path.home() / ".config" / "rlm-tools-bsl" / "logs" / "server.log"
