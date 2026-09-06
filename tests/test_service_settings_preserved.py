"""Issue #33: re-registering the service must not reset host/port/.env.

Every update path runs ``service uninstall`` and then ``service install``. The
server preserves omitted settings, while current install scripts additionally pass
saved values explicitly so an unexpectedly old package cannot reset them.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import types
import pathlib
from pathlib import Path
from unittest.mock import patch

import pytest

from rlm_tools_bsl.service import (
    SavedConfigError,
    load_config,
    read_saved_config,
    resolve_install_settings,
)

SAVED = {"host": "0.0.0.0", "port": 3000, "env_file": "/srv/rlm/.env", "exe_path": None}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    # RLM_CONFIG_FILE is deliberately NOT dropped: conftest points it at a fake home
    # and that isolation must stay. Tests that need a specific config set it themselves.
    for var in ("RLM_HOST", "RLM_PORT"):
        monkeypatch.delenv(var, raising=False)


def _write_config(tmp_path: Path, payload) -> Path:
    cfg = tmp_path / "service.json"
    cfg.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
    return cfg


class TestResolveInstallSettings:
    def test_omitted_flags_keep_saved_settings(self):
        assert resolve_install_settings(saved=SAVED, environ={}) == ("0.0.0.0", 3000, "/srv/rlm/.env")

    def test_explicit_flags_win_over_saved(self):
        assert resolve_install_settings(
            cli_host="10.0.0.5", cli_port=1234, cli_env="/other/.env", saved=SAVED, environ={}
        ) == ("10.0.0.5", 1234, "/other/.env")

    def test_env_used_only_when_nothing_saved(self):
        env = {"RLM_HOST": "192.168.1.7", "RLM_PORT": "3010"}
        assert resolve_install_settings(saved=None, environ=env) == ("192.168.1.7", 3010, None)
        # A saved value is a deliberate choice and outranks the ambient environment.
        assert resolve_install_settings(saved=SAVED, environ=env)[:2] == ("0.0.0.0", 3000)

    def test_fresh_install_falls_back_to_defaults(self):
        assert resolve_install_settings(saved=None, environ={}) == ("127.0.0.1", 9000, None)

    def test_no_env_drops_saved_env_file(self):
        assert resolve_install_settings(drop_env=True, saved=SAVED, environ={})[2] is None

    @pytest.mark.parametrize(
        "saved",
        [
            {},
            {"host": "  ", "port": "not-a-port"},
            {"host": "0.0.0.0"},
            {"host": "0.0.0.0", "port": 70000},
            {"host": "0.0.0.0", "port": 3000.7},
            {"port": 3000},
        ],
        ids=["empty", "blank-and-garbage", "no-port", "port-out-of-range", "fractional-port", "no-host"],
    )
    def test_an_existing_config_may_not_fall_through_to_defaults(self, saved):
        """A config of `{}` is still a config: an installation exists. Quietly answering
        127.0.0.1:9000 for whatever it fails to state is issue #33 all over again -- the
        service moves and nobody is told. Only a FRESH install may fall through."""
        with pytest.raises(ValueError):
            resolve_install_settings(saved=saved, environ={"RLM_HOST": "1.2.3.4", "RLM_PORT": "5555"})

    def test_explicit_flags_still_rescue_a_broken_config(self):
        assert resolve_install_settings(cli_host="1.2.3.4", cli_port=8080, saved={}, environ={}) == (
            "1.2.3.4",
            8080,
            None,
        )

    def test_a_fresh_install_still_falls_through(self):
        assert resolve_install_settings(saved=None, environ={"RLM_PORT": "3010"})[1] == 3010

    @pytest.mark.parametrize("value", ["not-a-number", "70000", "3000.7"])
    def test_invalid_env_port_is_an_error_on_a_fresh_install(self, value):
        """An invalid RLM_PORT must not turn into a successful install on port 9000."""
        with pytest.raises(ValueError, match="RLM_PORT"):
            resolve_install_settings(saved=None, environ={"RLM_PORT": value})

    def test_invalid_explicit_port_is_an_error_not_a_fallback(self):
        """Silently falling back would hide the typo behind a service that looks fine."""
        with pytest.raises(ValueError):
            resolve_install_settings(cli_port=70000, saved=SAVED, environ={})

    def test_env_path_is_kept_verbatim(self):
        """A POSIX filename may legally start or end with a space."""
        saved = {"host": "0.0.0.0", "port": 3000, "env_file": " /srv/x .env "}
        assert resolve_install_settings(saved=saved, environ={})[2] == " /srv/x .env "

    def test_blank_explicit_env_is_an_error_not_no_env(self):
        """Only --no-env may clear a saved path; an empty shell expansion is a typo."""
        with pytest.raises(ValueError, match="--env"):
            resolve_install_settings(cli_env="   ", saved=SAVED, environ={})

    def test_a_whole_number_float_port_is_accepted(self):
        assert resolve_install_settings(saved={"host": "0.0.0.0", "port": 3000.0}, environ={})[1] == 3000


class TestReadSavedConfig:
    def test_absent_config_is_none_not_defaults(self, monkeypatch, tmp_path):
        """`load_config` invents defaults; the install path must not confuse those
        with a user who deliberately picked 127.0.0.1."""
        monkeypatch.setenv("RLM_CONFIG_FILE", str(tmp_path / "service.json"))
        assert read_saved_config() is None
        assert load_config()["host"] == "127.0.0.1"

    def test_broken_config_is_an_error_not_a_silent_reset(self, monkeypatch, tmp_path):
        """The file exists, so its contents ARE the user's settings. Reporting "nothing
        saved" here would overwrite exactly what we failed to read."""
        monkeypatch.setenv("RLM_CONFIG_FILE", str(_write_config(tmp_path, "{not json")))
        with pytest.raises(SavedConfigError):
            read_saved_config()

    def test_non_utf8_config_is_an_error(self, monkeypatch, tmp_path):
        cfg = tmp_path / "service.json"
        cfg.write_bytes(bytes([0xFF, 0xFE, 0x7B, 0x7D]))
        monkeypatch.setenv("RLM_CONFIG_FILE", str(cfg))
        with pytest.raises(SavedConfigError):
            read_saved_config()

    def test_json_that_is_not_an_object_is_an_error(self, monkeypatch, tmp_path):
        monkeypatch.setenv("RLM_CONFIG_FILE", str(_write_config(tmp_path, "[]")))
        with pytest.raises(SavedConfigError):
            read_saved_config()

    def test_tilde_is_not_expanded(self, monkeypatch, tmp_path):
        """Every other consumer of RLM_CONFIG_FILE takes the value literally; expanding
        `~` only here would split one setting into two different files."""
        from rlm_tools_bsl._config import _env_file_from_service_json
        from rlm_tools_bsl.service import _config_path

        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("RLM_CONFIG_FILE", "~/custom/service.json")
        assert _config_path() == tmp_path / "~" / "custom" / "service.json"
        # ... and the other consumer agrees that there is nothing there.
        assert _env_file_from_service_json() is None

    def test_relative_config_path_is_made_absolute(self, monkeypatch, tmp_path):
        """The Windows service registry gets this path and the SCM starts the service
        from a different working directory."""
        from rlm_tools_bsl.service import _config_path

        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("RLM_CONFIG_FILE", "cfg/service.json")
        assert _config_path().is_absolute()
        assert _config_path() == tmp_path / "cfg" / "service.json"

    def test_saved_config_is_returned(self, monkeypatch, tmp_path):
        monkeypatch.setenv("RLM_CONFIG_FILE", str(_write_config(tmp_path, SAVED)))
        assert read_saved_config() == SAVED


def _fake_platform_module(monkeypatch, name: str) -> dict:
    """Stand in for `_service_linux` / `_service_win` so both branches of the
    facade can be driven on any host (and without pywin32)."""
    calls: dict = {}
    mod = types.ModuleType(name)
    mod.install = lambda **kw: calls.update(install=kw)
    mod.uninstall = lambda **kw: calls.update(uninstall=kw)
    mod.start = mod.stop = mod.status = lambda: None
    monkeypatch.setitem(sys.modules, name, mod)
    return calls


@pytest.mark.parametrize(
    ("platform", "module"),
    [("linux", "rlm_tools_bsl._service_linux"), ("win32", "rlm_tools_bsl._service_win")],
)
class TestHandleServiceCommand:
    def test_install_without_flags_reuses_saved_settings(self, monkeypatch, tmp_path, platform, module):
        monkeypatch.setattr(sys, "platform", platform)
        calls = _fake_platform_module(monkeypatch, module)
        saved = {**SAVED, "env_file": "legacy.env"}
        monkeypatch.setenv("RLM_CONFIG_FILE", str(_write_config(tmp_path, saved)))

        from rlm_tools_bsl.service import handle_service_command

        args = argparse.Namespace(service_action="install", host=None, port=None, env=None, no_env=False)
        handle_service_command(args)

        assert calls["install"] == {
            "host": "0.0.0.0",
            "port": 3000,
            "env_file": str(tmp_path / "legacy.env"),
            "no_env": False,
        }

    def test_install_flags_still_win(self, monkeypatch, tmp_path, platform, module):
        monkeypatch.setattr(sys, "platform", platform)
        calls = _fake_platform_module(monkeypatch, module)
        monkeypatch.setenv("RLM_CONFIG_FILE", str(_write_config(tmp_path, SAVED)))

        from rlm_tools_bsl.service import handle_service_command

        args = argparse.Namespace(service_action="install", host="127.0.0.1", port=9100, env=None, no_env=True)
        handle_service_command(args)

        assert calls["install"] == {"host": "127.0.0.1", "port": 9100, "env_file": None, "no_env": True}

    def test_legacy_null_env_keeps_runtime_fallbacks(self, monkeypatch, tmp_path, platform, module):
        """Pre-1.35.0 null did not mean --no-env; it still allowed the user/CWD fallback."""
        monkeypatch.setattr(sys, "platform", platform)
        calls = _fake_platform_module(monkeypatch, module)
        saved = {**SAVED, "env_file": None}
        monkeypatch.setenv("RLM_CONFIG_FILE", str(_write_config(tmp_path, saved)))

        from rlm_tools_bsl.service import handle_service_command

        handle_service_command(
            argparse.Namespace(service_action="install", host=None, port=None, env=None, no_env=False)
        )

        assert calls["install"]["env_file"] is None
        assert calls["install"]["no_env"] is False

    def test_saved_explicit_no_env_is_preserved(self, monkeypatch, tmp_path, platform, module):
        monkeypatch.setattr(sys, "platform", platform)
        calls = _fake_platform_module(monkeypatch, module)
        saved = {**SAVED, "env_file": None, "no_env": True}
        monkeypatch.setenv("RLM_CONFIG_FILE", str(_write_config(tmp_path, saved)))

        from rlm_tools_bsl.service import handle_service_command

        handle_service_command(
            argparse.Namespace(service_action="install", host=None, port=None, env=None, no_env=False)
        )

        assert calls["install"]["env_file"] is None
        assert calls["install"]["no_env"] is True

    def test_explicit_env_replaces_saved_no_env(self, monkeypatch, tmp_path, platform, module):
        monkeypatch.setattr(sys, "platform", platform)
        calls = _fake_platform_module(monkeypatch, module)
        saved = {**SAVED, "env_file": None, "no_env": True}
        monkeypatch.setenv("RLM_CONFIG_FILE", str(_write_config(tmp_path, saved)))

        from rlm_tools_bsl.service import handle_service_command

        handle_service_command(
            argparse.Namespace(service_action="install", host=None, port=None, env="new.env", no_env=False)
        )

        assert calls["install"]["env_file"] == "new.env"
        assert calls["install"]["no_env"] is False

    def test_unusable_config_stops_the_install(self, monkeypatch, tmp_path, platform, module):
        """Without this the installer would write defaults over settings it could not
        read -- issue #33 with extra steps."""
        monkeypatch.setattr(sys, "platform", platform)
        calls = _fake_platform_module(monkeypatch, module)
        cfg = _write_config(tmp_path, "{not json")
        monkeypatch.setenv("RLM_CONFIG_FILE", str(cfg))

        from rlm_tools_bsl.service import handle_service_command

        args = argparse.Namespace(service_action="install", host=None, port=None, env=None, no_env=False)
        with pytest.raises(SystemExit) as exc:
            handle_service_command(args)

        assert exc.value.code == 1
        assert "install" not in calls
        assert cfg.read_text(encoding="utf-8") == "{not json", "the unreadable file must be left as-is"

    def test_unusable_config_is_tolerated_when_everything_is_explicit(self, monkeypatch, tmp_path, platform, module):
        monkeypatch.setattr(sys, "platform", platform)
        calls = _fake_platform_module(monkeypatch, module)
        monkeypatch.setenv("RLM_CONFIG_FILE", str(_write_config(tmp_path, "{not json")))

        from rlm_tools_bsl.service import handle_service_command

        args = argparse.Namespace(service_action="install", host="0.0.0.0", port=3000, env=None, no_env=True)
        handle_service_command(args)

        assert calls["install"] == {"host": "0.0.0.0", "port": 3000, "env_file": None, "no_env": True}

    def test_unreadable_config_cannot_bypass_rollback_snapshot(self, monkeypatch, tmp_path, platform, module, capsys):
        monkeypatch.setattr(sys, "platform", platform)
        calls = _fake_platform_module(monkeypatch, module)
        cfg = _write_config(tmp_path, SAVED)
        monkeypatch.setenv("RLM_CONFIG_FILE", str(cfg))
        original_read_text = pathlib.Path.read_text

        def _deny_config_read(path, *args, **kwargs):
            if path == cfg:
                raise PermissionError(13, "permission denied", str(path))
            return original_read_text(path, *args, **kwargs)

        monkeypatch.setattr(pathlib.Path, "read_text", _deny_config_read)
        args = argparse.Namespace(service_action="install", host="0.0.0.0", port=3000, env=None, no_env=True)

        with pytest.raises(SystemExit) as exc:
            from rlm_tools_bsl.service import handle_service_command

            handle_service_command(args)

        assert exc.value.code == 1
        assert "install" not in calls
        assert "безопасного отката" in capsys.readouterr().out

    def test_empty_explicit_host_stops_the_install(self, monkeypatch, tmp_path, platform, module):
        """`--host ""` used to count as "explicitly specified", pass the broken-config
        guard, and then quietly resolve to 127.0.0.1."""
        monkeypatch.setattr(sys, "platform", platform)
        calls = _fake_platform_module(monkeypatch, module)
        cfg = _write_config(tmp_path, "{not json")
        monkeypatch.setenv("RLM_CONFIG_FILE", str(cfg))

        from rlm_tools_bsl.service import handle_service_command

        args = argparse.Namespace(service_action="install", host="", port=3000, env=None, no_env=True)
        with pytest.raises(SystemExit):
            handle_service_command(args)

        assert "install" not in calls
        assert cfg.read_text(encoding="utf-8") == "{not json"

    def test_invalid_explicit_port_stops_the_install(self, monkeypatch, tmp_path, platform, module):
        monkeypatch.setattr(sys, "platform", platform)
        calls = _fake_platform_module(monkeypatch, module)
        monkeypatch.setenv("RLM_CONFIG_FILE", str(_write_config(tmp_path, SAVED)))

        from rlm_tools_bsl.service import handle_service_command

        args = argparse.Namespace(service_action="install", host=None, port=70000, env=None, no_env=False)
        with pytest.raises(SystemExit):
            handle_service_command(args)
        assert "install" not in calls

    def test_uninstall_forwards_purge(self, monkeypatch, platform, module):
        monkeypatch.setattr(sys, "platform", platform)
        calls = _fake_platform_module(monkeypatch, module)

        from rlm_tools_bsl.service import handle_service_command

        handle_service_command(argparse.Namespace(service_action="uninstall", purge=True))
        assert calls["uninstall"] == {"purge": True}

    def test_uninstall_keeps_config_by_default(self, monkeypatch, platform, module):
        monkeypatch.setattr(sys, "platform", platform)
        calls = _fake_platform_module(monkeypatch, module)

        from rlm_tools_bsl.service import handle_service_command

        handle_service_command(argparse.Namespace(service_action="uninstall"))
        assert calls["uninstall"] == {"purge": False}


class TestLinuxUninstall:
    """`_service_linux` is pure stdlib, so this runs on any host."""

    def _prepare(self, monkeypatch, tmp_path):
        from rlm_tools_bsl import _service_linux

        monkeypatch.setattr(_service_linux, "subprocess", types.SimpleNamespace(run=lambda *a, **kw: None))
        monkeypatch.setattr(_service_linux, "_unit_path", lambda: tmp_path / "unit" / "rlm-tools-bsl.service")
        cfg = _write_config(tmp_path, SAVED)
        monkeypatch.setenv("RLM_CONFIG_FILE", str(cfg))
        # The pre-fix code deleted the module CONSTANT, which ignores RLM_CONFIG_FILE
        # and is computed at import time from the real home. Point it at a decoy: a
        # regression to `CONFIG_FILE.unlink()` then fails here instead of silently
        # eating the developer's own ~/.config/rlm-tools-bsl/service.json.
        decoy = tmp_path / "home-service.json"
        decoy.write_text("{}", encoding="utf-8")
        monkeypatch.setattr("rlm_tools_bsl.service.CONFIG_FILE", decoy)
        # Both spellings, because the pre-fix module bound the constant into its OWN
        # namespace (`from ... import CONFIG_FILE`), where patching the service module
        # does not reach it -- and a run against that code then deletes the real file.
        monkeypatch.setattr(_service_linux, "CONFIG_FILE", decoy, raising=False)
        return _service_linux, cfg, decoy

    def test_config_survives_plain_uninstall(self, monkeypatch, tmp_path):
        service_linux, cfg, decoy = self._prepare(monkeypatch, tmp_path)

        service_linux.uninstall()

        assert cfg.exists(), "service.json must survive the uninstall half of an upgrade"
        assert json.loads(cfg.read_text(encoding="utf-8")) == SAVED
        assert decoy.exists(), "uninstall must not delete the default-path config either"

    def test_purge_removes_config(self, monkeypatch, tmp_path):
        service_linux, cfg, _decoy = self._prepare(monkeypatch, tmp_path)

        service_linux.uninstall(purge=True)

        assert not cfg.exists()

    def test_purge_honours_config_override(self, monkeypatch, tmp_path):
        """The overridden path is purged, not the hardcoded ~/.config one."""
        service_linux, cfg, decoy = self._prepare(monkeypatch, tmp_path)

        service_linux.uninstall(purge=True)

        assert not cfg.exists()
        assert decoy.exists()


class TestLinuxInstall:
    """A repeat install is the documented way to change host/port."""

    def _prepare(self, monkeypatch, tmp_path, is_active: bool):
        from rlm_tools_bsl import _service_linux

        calls: list[list[str]] = []

        def _run(cmd, **kwargs):
            calls.append(list(cmd))
            code = 0 if (cmd[-2:-1] == ["--quiet"] or "is-active" not in cmd) else 1
            if "is-active" in cmd:
                code = 0 if is_active else 3
            return types.SimpleNamespace(returncode=code)

        monkeypatch.setattr(_service_linux, "subprocess", types.SimpleNamespace(run=_run))
        monkeypatch.setattr(_service_linux, "_unit_path", lambda: tmp_path / "unit" / "rlm-tools-bsl.service")
        monkeypatch.setenv("RLM_CONFIG_FILE", str(tmp_path / "service.json"))
        return _service_linux, calls

    def test_running_service_is_restarted_with_the_new_settings(self, monkeypatch, tmp_path):
        service_linux, calls = self._prepare(monkeypatch, tmp_path, is_active=True)

        service_linux.install(host="0.0.0.0", port=3000, env_file=None, no_env=True)

        assert ["systemctl", "--user", "restart", "rlm-tools-bsl"] in calls
        unit = (tmp_path / "unit" / "rlm-tools-bsl.service").read_text(encoding="utf-8")
        # Quoted because every ExecStart argument is: an exe path with a space in it would
        # otherwise be split into two.
        assert '--host "0.0.0.0" --port 3000' in unit
        assert '"_RLM_SERVICE_NO_ENV=1"' in unit

    def test_legacy_null_env_does_not_disable_fallbacks(self, monkeypatch, tmp_path):
        service_linux, _calls = self._prepare(monkeypatch, tmp_path, is_active=False)

        service_linux.install(host="0.0.0.0", port=3000, env_file=None)

        unit = (tmp_path / "unit" / "rlm-tools-bsl.service").read_text(encoding="utf-8")
        assert '"_RLM_SERVICE_NO_ENV=1"' not in unit

    def test_stopped_service_is_not_started_by_install(self, monkeypatch, tmp_path):
        service_linux, calls = self._prepare(monkeypatch, tmp_path, is_active=False)

        service_linux.install(host="0.0.0.0", port=3000, env_file=None)

        assert not any("restart" in c for c in calls)

    def test_unit_carries_the_config_path(self, monkeypatch, tmp_path):
        """`systemctl --user` does not inherit the caller's environment, so without this
        the service would keep projects.json, logs, cache and index next to the DEFAULT
        config instead of the one just written.

        It has to sit on the ExecStart command line rather than in `Environment=`:
        systemd applies `EnvironmentFile=` last, so a .env that happens to define
        RLM_CONFIG_FILE would otherwise point the service at a different config."""
        service_linux, _calls = self._prepare(monkeypatch, tmp_path, is_active=False)

        service_linux.install(host="127.0.0.1", port=9000, env_file="/srv/rlm/.env")

        unit = (tmp_path / "unit" / "rlm-tools-bsl.service").read_text(encoding="utf-8")
        exec_line = next(line for line in unit.splitlines() if line.startswith("ExecStart="))
        expected = str(tmp_path / "service.json").replace(chr(92), chr(92) * 2)
        assert f'/usr/bin/env "RLM_CONFIG_FILE={expected}"' in exec_line
        assert "EnvironmentFile=-/srv/rlm/.env" in unit
        assert "Environment=" not in unit.replace("EnvironmentFile=", ""), "one source of truth only"

    def test_relative_env_path_is_pinned_to_install_cwd(self, monkeypatch, tmp_path):
        service_linux, _calls = self._prepare(monkeypatch, tmp_path, is_active=False)
        install_cwd = tmp_path / "installer"
        install_cwd.mkdir()
        monkeypatch.chdir(install_cwd)

        service_linux.install(host="127.0.0.1", port=9000, env_file=".env")

        expected = str(install_cwd / ".env")
        unit = (tmp_path / "unit" / "rlm-tools-bsl.service").read_text(encoding="utf-8")
        config = json.loads((tmp_path / "service.json").read_text(encoding="utf-8"))
        assert f"EnvironmentFile=-{expected}" in unit
        assert config["env_file"] == expected

    def test_default_config_path_does_not_split_service_and_cli_index_roots(self, monkeypatch, tmp_path):
        """At the default path RLM_CONFIG_FILE must stay unset in the child: setting it
        moves only the service index from ~/.cache into ~/.config/.../index."""
        from rlm_tools_bsl import _service_linux

        inactive = types.SimpleNamespace(returncode=3)
        monkeypatch.setattr(_service_linux, "subprocess", types.SimpleNamespace(run=lambda *a, **kw: inactive))
        monkeypatch.setattr(_service_linux, "_unit_path", lambda: tmp_path / "unit" / "rlm-tools-bsl.service")
        monkeypatch.setattr(_service_linux, "save_config", lambda *a, **kw: None)
        monkeypatch.delenv("RLM_CONFIG_FILE", raising=False)
        monkeypatch.setattr("rlm_tools_bsl.service.CONFIG_FILE", tmp_path / "default" / "service.json")

        _service_linux.install(host="127.0.0.1", port=9000, env_file="/srv/rlm/.env")

        unit = (tmp_path / "unit" / "rlm-tools-bsl.service").read_text(encoding="utf-8")
        exec_line = next(line for line in unit.splitlines() if line.startswith("ExecStart="))
        assert "-u RLM_CONFIG_FILE" in exec_line
        assert '"RLM_CONFIG_FILE=' not in exec_line

    def test_config_path_is_escaped_for_systemd(self, monkeypatch, tmp_path):
        """systemd applies C-style escaping inside quotes too, so a literal backslash in
        the path would reach the process as something else entirely."""
        from rlm_tools_bsl._service_linux import _systemd_escape

        assert _systemd_escape(chr(92) + "n") == chr(92) * 2 + "n"
        assert _systemd_escape('a"b') == "a" + chr(92) + '"b'
        assert _systemd_escape("/plain/path.json") == "/plain/path.json"
        # %u would otherwise be expanded to the user name and point the service elsewhere.
        assert _systemd_escape("/srv/%u/service.json") == "/srv/%%u/service.json"

    def test_specifiers_are_escaped_everywhere_they_expand(self, monkeypatch, tmp_path):
        """ExecStart and EnvironmentFile both expand %-specifiers."""
        from rlm_tools_bsl import _service_linux

        inactive = types.SimpleNamespace(returncode=3)
        monkeypatch.setattr(_service_linux, "subprocess", types.SimpleNamespace(run=lambda *a, **kw: inactive))
        monkeypatch.setattr(_service_linux, "_unit_path", lambda: tmp_path / "unit" / "rlm-tools-bsl.service")
        monkeypatch.setattr(_service_linux, "_exe_path", lambda: "/opt/%h/bin/rlm-tools-bsl")
        monkeypatch.setenv("RLM_CONFIG_FILE", str(tmp_path / "service.json"))

        _service_linux.install(host="127.0.0.1", port=9000, env_file="/srv/%u/.env")

        unit = (tmp_path / "unit" / "rlm-tools-bsl.service").read_text(encoding="utf-8")
        assert "EnvironmentFile=-/srv/%%u/.env" in unit
        assert '"/opt/%%h/bin/rlm-tools-bsl"' in unit

    def test_env_paths_systemd_would_read_differently_are_rejected(self, monkeypatch, tmp_path, capsys):
        """systemd strips the value of a directive, continues the line after an ODD number
        of backslashes and ends it at a newline. Installing without EnvironmentFile would
        apply import-time settings too late, so an unrepresentable path must be rejected
        before either config file changes. Inner and EVEN trailing backslashes are fine."""
        from rlm_tools_bsl import _service_linux

        inactive = types.SimpleNamespace(returncode=3)

        def _install(env_file):
            monkeypatch.setattr(_service_linux, "subprocess", types.SimpleNamespace(run=lambda *a, **kw: inactive))
            monkeypatch.setattr(_service_linux, "_unit_path", lambda: tmp_path / "unit" / "rlm-tools-bsl.service")
            monkeypatch.setenv("RLM_CONFIG_FILE", str(tmp_path / "service.json"))
            _service_linux.install(host="127.0.0.1", port=9000, env_file=env_file)
            unit = (tmp_path / "unit" / "rlm-tools-bsl.service").read_text(encoding="utf-8")
            return capsys.readouterr().out, unit

        backslash = chr(92)
        passed_through = [
            "/srv/x/.env",
            "/srv/team" + backslash + "name/.env",
            "/srv/x" + backslash * 2,
        ]
        left_out = [
            "/srv/x" + backslash,
            "/srv/x" + backslash * 3,
            "/srv/key.env  ",
            "/srv/team" + chr(10) + "name.env",
            "/srv/key-*.env",
            "/srv/key-?.env",
            "/srv/[prod]/key.env",
        ]

        for env_file in passed_through:
            out, unit = _install(env_file)
            assert "ОШИБКА" not in out, env_file
            assert f"EnvironmentFile=-{env_file}" in unit, env_file

        for env_file in left_out:
            unit_path = tmp_path / "unit" / "rlm-tools-bsl.service"
            config_path = tmp_path / "service.json"
            old_unit = unit_path.read_bytes()
            old_config = config_path.read_bytes()
            with pytest.raises(SystemExit) as exc:
                _install(env_file)
            assert exc.value.code == 1
            assert unit_path.read_bytes() == old_unit, env_file
            assert config_path.read_bytes() == old_config, env_file
            assert "ОШИБКА" in capsys.readouterr().out, env_file

    def test_dollar_and_spaces_survive_in_exec_start(self, monkeypatch, tmp_path):
        """ExecStart expands $VAR and splits on unquoted whitespace: a literal path must
        come out of both unchanged."""
        from rlm_tools_bsl import _service_linux

        inactive = types.SimpleNamespace(returncode=3)
        monkeypatch.setattr(_service_linux, "subprocess", types.SimpleNamespace(run=lambda *a, **kw: inactive))
        monkeypatch.setattr(_service_linux, "_unit_path", lambda: tmp_path / "unit" / "rlm-tools-bsl.service")
        monkeypatch.setattr(_service_linux, "_exe_path", lambda: "/opt/RLM Tools/bin/rlm-tools-bsl")
        # Inside tmp_path on purpose: install() really writes the config, and a path
        # of its own would create directories outside the test sandbox.
        monkeypatch.setenv("RLM_CONFIG_FILE", str(tmp_path / "${USER}" / "service.json"))

        _service_linux.install(host="127.0.0.1", port=9000, env_file=None)

        unit = (tmp_path / "unit" / "rlm-tools-bsl.service").read_text(encoding="utf-8")
        exec_line = next(line for line in unit.splitlines() if line.startswith("ExecStart="))
        # The path itself is platform-normalised (a POSIX-looking path gets a drive on
        # Windows); what matters is that the specifier is neutralised, not the shape.
        assert "$${USER}" in exec_line, "a literal $ has to be doubled or systemd expands it"
        assert '"/opt/RLM Tools/bin/rlm-tools-bsl"' in exec_line

    @pytest.mark.parametrize("failed_action", ["daemon-reload", "enable", "restart"])
    def test_failed_systemd_step_restores_previous_install(self, monkeypatch, tmp_path, capsys, failed_action):
        """A repeat install is one transaction across config, unit and systemd state."""
        from rlm_tools_bsl import _service_linux

        calls = []
        failed_once = False

        def _run(cmd, **kwargs):
            nonlocal failed_once
            calls.append(list(cmd))
            if "is-active" in cmd or "is-enabled" in cmd:
                return types.SimpleNamespace(returncode=0)
            if failed_action in cmd and not failed_once:
                failed_once = True
                if kwargs.get("check"):
                    raise RuntimeError(f"{failed_action} failed")
                return types.SimpleNamespace(returncode=1)
            return types.SimpleNamespace(returncode=0)

        monkeypatch.setattr(_service_linux, "subprocess", types.SimpleNamespace(run=_run))
        unit = tmp_path / "unit" / "rlm-tools-bsl.service"
        unit.parent.mkdir()
        old_unit = b"[Service]\nExecStart=/old/server --port 8123\n"
        unit.write_bytes(old_unit)
        cfg = tmp_path / "service.json"
        old_config = b'{"host":"10.0.0.7","port":8123,"env_file":null}\n'
        cfg.write_bytes(old_config)
        monkeypatch.setattr(_service_linux, "_unit_path", lambda: unit)
        monkeypatch.setenv("RLM_CONFIG_FILE", str(cfg))

        with pytest.raises(SystemExit) as exc:
            _service_linux.install(host="0.0.0.0", port=3000, env_file=None)

        assert exc.value.code == 1, "automation must be able to tell this from success"
        assert cfg.read_bytes() == old_config
        assert unit.read_bytes() == old_unit
        out = capsys.readouterr().out
        assert "перезапущена с новыми параметрами" not in out
        assert "ОШИБКА" in out and "восстановлены" in out
        assert sum(failed_action in call for call in calls) >= 2, "the rollback must retry the old state"


class TestPurgeRemovesInstallerBackup:
    """An interrupted install leaves a sidecar copy of service.json. If `--purge` left it
    behind, the next run of any install script would resurrect the settings the user
    just asked to be gone."""

    def test_linux_purge_removes_backup(self, monkeypatch, tmp_path):
        from rlm_tools_bsl import _service_linux
        from rlm_tools_bsl.service import backup_path

        monkeypatch.setattr(_service_linux, "subprocess", types.SimpleNamespace(run=lambda *a, **kw: None))
        monkeypatch.setattr(_service_linux, "_unit_path", lambda: tmp_path / "unit" / "rlm-tools-bsl.service")
        cfg = _write_config(tmp_path, SAVED)
        backup = backup_path(cfg)
        backup.write_text("{}", encoding="utf-8")
        monkeypatch.setenv("RLM_CONFIG_FILE", str(cfg))
        monkeypatch.setattr("rlm_tools_bsl.service.CONFIG_FILE", tmp_path / "home-service.json")
        monkeypatch.setattr(_service_linux, "CONFIG_FILE", tmp_path / "home-service.json", raising=False)

        _service_linux.uninstall(purge=True)

        assert not cfg.exists()
        assert not backup.exists()

    def test_purge_removes_staging_leftovers(self, monkeypatch, tmp_path):
        """An interrupted install can leave `<name>.partial.<pid>` files holding the very
        settings that were purged."""
        from rlm_tools_bsl import _service_linux
        from rlm_tools_bsl.service import backup_path

        monkeypatch.setattr(_service_linux, "subprocess", types.SimpleNamespace(run=lambda *a, **kw: None))
        monkeypatch.setattr(_service_linux, "_unit_path", lambda: tmp_path / "unit" / "rlm-tools-bsl.service")
        cfg = _write_config(tmp_path, SAVED)
        monkeypatch.setenv("RLM_CONFIG_FILE", str(cfg))
        leftovers = [
            backup_path(cfg),
            cfg.with_name(cfg.name + ".partial.4242"),
            cfg.with_name(cfg.name + ".rlm-backup.partial.4242"),
            cfg.with_name(cfg.name + ".new.4242"),
        ]
        for leftover in leftovers:
            leftover.write_text("{}", encoding="utf-8")

        _service_linux.uninstall(purge=True)

        assert not cfg.exists()
        assert [leftover for leftover in leftovers if leftover.exists()] == []

    def test_plain_uninstall_keeps_both(self, monkeypatch, tmp_path):
        from rlm_tools_bsl import _service_linux
        from rlm_tools_bsl.service import backup_path

        monkeypatch.setattr(_service_linux, "subprocess", types.SimpleNamespace(run=lambda *a, **kw: None))
        monkeypatch.setattr(_service_linux, "_unit_path", lambda: tmp_path / "unit" / "rlm-tools-bsl.service")
        cfg = _write_config(tmp_path, SAVED)
        backup = backup_path(cfg)
        backup.write_text("{}", encoding="utf-8")
        monkeypatch.setenv("RLM_CONFIG_FILE", str(cfg))

        _service_linux.uninstall()

        assert cfg.exists() and backup.exists()


class TestInstallerLeftovers:
    """These paths get DELETED by `--purge`, so a wrong match is destructive."""

    def test_glob_metacharacters_in_the_name_do_not_widen_the_match(self, tmp_path):
        """`service[1].json` is a legal file name; as a glob pattern it would match
        somebody else's `service1.json.partial.42` and miss its own."""
        from rlm_tools_bsl.service import installer_leftovers

        cfg = tmp_path / "service[1].json"
        cfg.write_text("{}", encoding="utf-8")
        mine = tmp_path / "service[1].json.partial.42"
        mine.write_text("{}", encoding="utf-8")
        foreign = tmp_path / "service1.json.partial.42"
        foreign.write_text("{}", encoding="utf-8")

        found = installer_leftovers(cfg)

        assert mine in found
        assert foreign not in found

    def test_a_non_pid_suffix_is_not_ours(self, tmp_path):
        """`service.json.partial.manual-copy` is a file somebody made by hand. Ours always
        end in the PID that wrote them, and everything in this list gets deleted."""
        from rlm_tools_bsl.service import installer_leftovers

        cfg = tmp_path / "service.json"
        cfg.write_text("{}", encoding="utf-8")
        theirs = [
            tmp_path / "service.json.partial.manual-copy",
            tmp_path / "service.json.new.notes",
            tmp_path / "service.json.partial.",
        ]
        ours = tmp_path / "service.json.partial.4242"
        for path in [*theirs, ours]:
            path.write_text("{}", encoding="utf-8")

        found = installer_leftovers(cfg)

        assert ours in found
        assert [path for path in theirs if path in found] == []

    def test_the_rescue_copy_is_purged_too(self, tmp_path):
        """It is deliberately kept across upgrades, so `--purge` is the only thing that
        removes it -- and it holds the same settings as the config."""
        from rlm_tools_bsl.service import installer_leftovers, rescue_path

        cfg = tmp_path / "service.json"
        cfg.write_text("{}", encoding="utf-8")

        assert rescue_path(cfg) in installer_leftovers(cfg)

    def test_the_symlink_note_is_purged_too(self, tmp_path):
        """The note an install script leaves when the config is a symlink points at a
        path of the user's; it has no business surviving a purge."""
        from rlm_tools_bsl.service import LINKTARGET_SUFFIX, installer_leftovers

        cfg = tmp_path / "service.json"
        cfg.write_text("{}", encoding="utf-8")

        assert cfg.with_name(cfg.name + LINKTARGET_SUFFIX) in installer_leftovers(cfg)

    def test_staging_files_of_a_failed_save_are_included(self, tmp_path):
        """A save killed between write and replace leaves `<name>.new.<pid>` holding the
        same settings."""
        from rlm_tools_bsl.service import backup_path, installer_leftovers

        cfg = tmp_path / "service.json"
        cfg.write_text("{}", encoding="utf-8")
        staged = tmp_path / "service.json.new.4242"
        staged.write_text("{}", encoding="utf-8")

        found = installer_leftovers(cfg)

        assert staged in found
        assert backup_path(cfg) in found
        assert cfg not in found, "the config itself is removed separately, last"


class TestSaveConfigIsAtomic:
    """A direct `service install` has no installer backup to fall back on, so a failed
    write must not destroy the settings it was replacing."""

    def test_previous_config_survives_a_failed_write(self, monkeypatch, tmp_path):
        from rlm_tools_bsl import service

        cfg = _write_config(tmp_path, SAVED)
        monkeypatch.setenv("RLM_CONFIG_FILE", str(cfg))

        real_write = pathlib.Path.write_text

        def _explode(self, *args, **kwargs):
            if self.name.startswith("service.json.new."):
                raise OSError(28, "No space left on device")
            return real_write(self, *args, **kwargs)

        monkeypatch.setattr(pathlib.Path, "write_text", _explode)

        with pytest.raises(OSError):
            service.save_config("127.0.0.1", 9000, None)

        assert json.loads(cfg.read_text(encoding="utf-8")) == SAVED
        assert list(tmp_path.glob("service.json.new.*")) == [], "no staging file left behind"

    def test_symlinked_config_stays_a_symlink(self, monkeypatch, tmp_path):
        """Replacing the LINK would turn it into a regular file and leave whatever it
        pointed at stale."""
        from rlm_tools_bsl import service

        target = tmp_path / "real" / "service.json"
        target.parent.mkdir()
        target.write_text(json.dumps(SAVED), encoding="utf-8")
        link = tmp_path / "service.json"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            pytest.skip("creating symlinks is not permitted here")
        monkeypatch.setenv("RLM_CONFIG_FILE", str(link))

        service.save_config("0.0.0.0", 3000, None)

        assert link.is_symlink(), "the link must survive the write"
        assert json.loads(target.read_text(encoding="utf-8"))["port"] == 3000
        assert list((tmp_path / "real").glob("*.new.*")) == []

    def test_a_symlink_is_followed_even_where_it_cannot_be_created(self, monkeypatch, tmp_path):
        """Same guarantee as above, checked without OS symlink privileges: the staging
        file and the replace must land on the TARGET, not on the link."""
        import os

        from rlm_tools_bsl import service

        target = tmp_path / "real" / "service.json"
        target.parent.mkdir()
        target.write_text(json.dumps(SAVED), encoding="utf-8")
        link = tmp_path / "service.json"
        link.write_text("stand-in for a link", encoding="utf-8")
        monkeypatch.setenv("RLM_CONFIG_FILE", str(link))
        monkeypatch.setattr(pathlib.Path, "is_symlink", lambda self: self == link)
        monkeypatch.setattr(os.path, "realpath", lambda path, **kw: str(target))

        service.save_config("0.0.0.0", 3000, None)

        assert json.loads(target.read_text(encoding="utf-8"))["port"] == 3000
        assert link.read_text(encoding="utf-8") == "stand-in for a link", "the link is untouched"
        assert list(tmp_path.glob("*.new.*")) == [] and list((tmp_path / "real").glob("*.new.*")) == []

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
    def test_existing_permissions_are_kept(self, monkeypatch, tmp_path):
        import os
        import stat

        cfg = _write_config(tmp_path, SAVED)
        os.chmod(cfg, 0o600)
        monkeypatch.setenv("RLM_CONFIG_FILE", str(cfg))

        from rlm_tools_bsl import service

        service.save_config("0.0.0.0", 3000, None)

        assert stat.S_IMODE(cfg.stat().st_mode) == 0o600

    def test_successful_write_leaves_no_staging_file(self, monkeypatch, tmp_path):
        from rlm_tools_bsl import service

        cfg = tmp_path / "service.json"
        monkeypatch.setenv("RLM_CONFIG_FILE", str(cfg))

        service.save_config("0.0.0.0", 3000, "/srv/rlm/.env")

        assert json.loads(cfg.read_text(encoding="utf-8"))["port"] == 3000
        assert list(tmp_path.glob("service.json.new.*")) == []

    def test_explicit_no_env_is_persisted(self, monkeypatch, tmp_path):
        from rlm_tools_bsl import service

        cfg = tmp_path / "service.json"
        monkeypatch.setenv("RLM_CONFIG_FILE", str(cfg))

        service.save_config("0.0.0.0", 3000, None, no_env=True)

        assert json.loads(cfg.read_text(encoding="utf-8"))["no_env"] is True


class TestWindowsDacl:
    """`os.chmod` does not touch ACLs: without carrying the DACL over, an atomic replace
    would hand a hardened config the directory's inherited rights instead."""

    def test_dacl_is_copied_from_the_file_being_replaced(self, monkeypatch, tmp_path):
        calls = {}
        fake = types.ModuleType("win32security")
        fake.DACL_SECURITY_INFORMATION = 4

        class _Descriptor:
            def __init__(self, path):
                self.path = path

            def GetSecurityDescriptorDacl(self):
                return f"dacl-of-{self.path}"

            def SetSecurityDescriptorDacl(self, present, dacl, defaulted):
                calls["set_dacl"] = dacl

        fake.GetFileSecurity = lambda path, info: _Descriptor(pathlib.Path(path).name)
        fake.SetFileSecurity = lambda path, info, sd: calls.update(applied_to=pathlib.Path(path).name)
        monkeypatch.setitem(sys.modules, "win32security", fake)
        monkeypatch.setattr(sys, "platform", "win32")

        from rlm_tools_bsl import service

        source = tmp_path / "service.json"
        source.write_text("{}", encoding="utf-8")
        destination = tmp_path / "service.json.new.1"
        destination.write_text("{}", encoding="utf-8")

        service._copy_windows_dacl(source, destination)

        assert calls["set_dacl"] == "dacl-of-service.json"
        assert calls["applied_to"] == "service.json.new.1"

    def test_missing_pywin32_is_not_an_error(self, monkeypatch, tmp_path):
        """The save must not fail on a machine without the `service` extra."""
        monkeypatch.setitem(sys.modules, "win32security", None)
        monkeypatch.setattr(sys, "platform", "win32")

        from rlm_tools_bsl import service

        cfg = _write_config(tmp_path, SAVED)
        monkeypatch.setenv("RLM_CONFIG_FILE", str(cfg))

        service.save_config("0.0.0.0", 3000, None)  # must not raise

        assert json.loads(cfg.read_text(encoding="utf-8"))["port"] == 3000


class TestArgparse:
    """The CLI must express "not specified" as None, otherwise the merge above
    can never tell an explicit 127.0.0.1 from an omitted flag."""

    def _parse(self, monkeypatch, argv):
        from rlm_tools_bsl import server

        captured = {}
        with (
            patch("rlm_tools_bsl.service.handle_service_command", lambda args: captured.update(args=args)),
            # main() loads the .env BEFORE argparse; unpatched it would leak the
            # developer's real environment into os.environ for the whole session.
            patch("rlm_tools_bsl._config.load_project_env", lambda: None),
        ):
            monkeypatch.setattr("sys.argv", ["rlm-tools-bsl", *argv])
            server.main()
        return captured["args"]

    def test_install_without_flags_has_no_values(self, monkeypatch):
        args = self._parse(monkeypatch, ["service", "install"])
        assert (args.host, args.port, args.env, args.no_env) == (None, None, None, False)

    def test_install_flags_are_parsed(self, monkeypatch):
        args = self._parse(monkeypatch, ["service", "install", "--host", "0.0.0.0", "--port", "3000"])
        assert (args.host, args.port) == ("0.0.0.0", 3000)

    def test_no_env_flag(self, monkeypatch):
        assert self._parse(monkeypatch, ["service", "install", "--no-env"]).no_env is True

    def test_invalid_env_port_does_not_break_the_parser(self, monkeypatch):
        """The default is computed while the parser is BUILT, so a bad RLM_PORT used to
        abort every invocation -- `--help` and `--version` included."""
        monkeypatch.setenv("RLM_PORT", "not-a-number")

        args = self._parse(monkeypatch, ["service", "install"])

        assert args.port is None, "the service sub-parser has its own default"

    def test_uninstall_purge_defaults_to_false(self, monkeypatch):
        assert self._parse(monkeypatch, ["service", "uninstall"]).purge is False
        assert self._parse(monkeypatch, ["service", "uninstall", "--purge"]).purge is True

    def test_env_and_no_env_are_mutually_exclusive(self, monkeypatch):
        """Both flags answer the same question; accepting the pair would make one
        of them silently win."""
        with pytest.raises(SystemExit) as exc:
            self._parse(monkeypatch, ["service", "install", "--env", "/x/.env", "--no-env"])
        assert exc.value.code == 2

    def test_dotenv_cannot_redirect_a_service_command_config(self, monkeypatch):
        """The service config is selected before .env loading, not by a value inside it."""
        from rlm_tools_bsl import server

        monkeypatch.delenv("RLM_CONFIG_FILE", raising=False)
        observed = {}

        def _load_env():
            os.environ["RLM_CONFIG_FILE"] = "redirected/service.json"

        with (
            patch(
                "rlm_tools_bsl.service.handle_service_command",
                lambda args: observed.update(config=os.environ.get("RLM_CONFIG_FILE")),
            ),
            patch("rlm_tools_bsl._config.load_project_env", _load_env),
        ):
            monkeypatch.setattr("sys.argv", ["rlm-tools-bsl", "service", "install"])
            server.main()

        assert observed["config"] is None
        assert "RLM_CONFIG_FILE" not in os.environ
