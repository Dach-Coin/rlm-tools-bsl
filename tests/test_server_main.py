"""Smoke tests for main() cleanup-on-startup hook."""

from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _isolate_sandbox_backend_lifecycle(monkeypatch):
    """Each test owns one synthetic server lifecycle."""
    from rlm_tools_bsl import server

    monkeypatch.setattr(server, "_sandbox_registry_accepting", True)


def test_main_calls_cleanup_stale_cache(monkeypatch):
    """cleanup_stale_cache must run once during main() before mcp.run."""
    from rlm_tools_bsl import server

    run_called = {"ok": False}

    def _fake_run(*args, **kwargs):
        run_called["ok"] = True

    with (
        patch("rlm_tools_bsl.cache.cleanup_stale_cache", return_value={"disabled": True, "roots": []}) as spy,
        patch.object(server.mcp, "run", _fake_run),
        patch("rlm_tools_bsl._config.load_project_env", lambda: None),
        patch.object(server, "build_session_manager_from_env", lambda: server.session_manager),
    ):
        monkeypatch.setattr("sys.argv", ["rlm-tools-bsl"])
        server.main()

    assert spy.called
    assert run_called["ok"] is True


def test_main_reopens_registry_for_new_lifecycle(monkeypatch):
    from rlm_tools_bsl import server

    accepting_during_run = []

    with (
        patch.object(
            server.mcp, "run", lambda *args, **kwargs: accepting_during_run.append(server._sandbox_registry_accepting)
        ),
        patch("rlm_tools_bsl.cache.cleanup_stale_cache", return_value={"disabled": True, "roots": []}),
        patch("rlm_tools_bsl._config.load_project_env", lambda: None),
        patch.object(server, "build_session_manager_from_env", lambda: server.session_manager),
    ):
        monkeypatch.setattr(server, "_sandbox_registry_accepting", False)
        monkeypatch.setattr("sys.argv", ["rlm-tools-bsl"])
        server.main()

    assert accepting_during_run == [True]
    assert server._sandbox_registry_accepting is False


def test_version_flag_does_not_trigger_cleanup(monkeypatch, capsys):
    """--version must be fast and silent. It must NOT run cleanup_stale_cache
    (argparse handles --version by printing and exiting before we reach the
    server-startup path)."""
    from rlm_tools_bsl import server

    monkeypatch.setenv("RLM_SANDBOX_MODE", "invalid-for-version")
    with (
        patch("rlm_tools_bsl.cache.cleanup_stale_cache") as spy,
        patch.object(server.mcp, "run", lambda *a, **kw: None),
        patch("rlm_tools_bsl._config.load_project_env", lambda: None),
        patch.object(server, "build_session_manager_from_env", lambda: server.session_manager),
    ):
        monkeypatch.setattr("sys.argv", ["rlm-tools-bsl", "--version"])
        try:
            server.main()
        except SystemExit:
            pass  # argparse exits after printing version

    assert not spy.called, "cleanup_stale_cache must not run for --version"


def test_service_subcommand_does_not_trigger_cleanup(monkeypatch):
    """`rlm-tools-bsl service <...>` commands must not trigger cleanup —
    those are short-lived utilities (install/start/stop/status/uninstall)."""
    from rlm_tools_bsl import server

    monkeypatch.setenv("RLM_SANDBOX_MODE", "invalid-for-service")
    with (
        patch("rlm_tools_bsl.cache.cleanup_stale_cache") as spy,
        patch.object(server.mcp, "run", lambda *a, **kw: None),
        patch("rlm_tools_bsl._config.load_project_env", lambda: None),
        patch.object(server, "build_session_manager_from_env", lambda: server.session_manager),
        patch("rlm_tools_bsl.service.handle_service_command", lambda args: None),
    ):
        monkeypatch.setattr("sys.argv", ["rlm-tools-bsl", "service", "status"])
        server.main()

    assert not spy.called, "cleanup_stale_cache must not run for `service` sub-commands"


def test_main_tolerates_cleanup_exception(monkeypatch):
    """An exception in cleanup_stale_cache must not prevent mcp.run."""
    from rlm_tools_bsl import server

    run_called = {"ok": False}

    def _fake_run(*args, **kwargs):
        run_called["ok"] = True

    with (
        patch("rlm_tools_bsl.cache.cleanup_stale_cache", side_effect=RuntimeError("boom")),
        patch.object(server.mcp, "run", _fake_run),
        patch("rlm_tools_bsl._config.load_project_env", lambda: None),
        patch.object(server, "build_session_manager_from_env", lambda: server.session_manager),
    ):
        monkeypatch.setattr("sys.argv", ["rlm-tools-bsl"])
        server.main()

    assert run_called["ok"] is True
