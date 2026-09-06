"""Behavioural contracts for the four service update scripts.

The POSIX scripts run against fake uv, systemctl and service commands, so the
test exercises their real control flow without touching an actual service.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _write_executable(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.mark.skipif(os.name == "nt", reason="behavioural shell test runs on POSIX CI")
@pytest.mark.parametrize("script_name", ["simple-install.sh", "simple-install-from-pip.sh"])
def test_shell_upgrade_discovers_custom_config_and_preserves_saved_values(tmp_path, script_name):
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash is unavailable")

    home = tmp_path / "home"
    fake_bin = tmp_path / "bin"
    config = tmp_path / "custom % $ config" / "service.json"
    env_file = "keys.env"
    expected_env_file = config.parent / env_file
    calls = tmp_path / "calls.bin"
    unit = home / ".config" / "systemd" / "user" / "rlm-tools-bsl.service"
    config.parent.mkdir(parents=True)
    unit.parent.mkdir(parents=True)
    config.write_text(
        json.dumps({"host": "0.0.0.0", "port": 3456, "env_file": env_file, "exe_path": None}),
        encoding="utf-8",
    )
    encoded_config = str(config).replace("%", "%%").replace("$", "$$")
    unit.write_text(
        f'ExecStart=/usr/bin/env "RLM_CONFIG_FILE={encoded_config}" '
        '"/opt/rlm-tools-bsl" --transport streamable-http --host "0.0.0.0" --port 3456\n',
        encoding="utf-8",
    )

    fake_bin.mkdir()
    _write_executable(fake_bin / "python3", f'exec {shlex.quote(sys.executable)} "$@"\n')
    _write_executable(
        fake_bin / "uv",
        'if [ "$1" = tool ] && [ "$2" = dir ]; then printf "%s\\n" "$FAKE_BIN"; fi\nexit 0\n',
    )
    _write_executable(
        fake_bin / "rlm-tools-bsl",
        '{ printf "CALL\\0"; printf "CONFIG=%s\\0" "$RLM_CONFIG_FILE"; '
        'for arg in "$@"; do printf "ARG=%s\\0" "$arg"; done; } >> "$CALL_LOG"\n'
        'if [ "$1" = "--version" ]; then printf "rlm-tools-bsl 1.35.0\\n"; fi\n'
        "exit 0\n",
    )
    for name, body in {
        "systemctl": "exit 0\n",
        "sleep": "exit 0\n",
        "curl": 'printf "200"\n',
    }.items():
        _write_executable(fake_bin / name, body)

    env = os.environ.copy()
    for name in ("RLM_CONFIG_FILE", "RLM_HOST", "RLM_PORT", "RLM_NO_ENV"):
        env.pop(name, None)
    env.update(
        HOME=str(home),
        PATH=str(fake_bin) + os.pathsep + env.get("PATH", ""),
        FAKE_BIN=str(fake_bin),
        CALL_LOG=str(calls),
    )

    result = subprocess.run(
        [bash],
        input=(ROOT / script_name).read_text(encoding="utf-8"),
        text=True,
        cwd=tmp_path,
        env=env,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    fields = calls.read_bytes().split(b"\0")
    calls_seen: list[list[str]] = []
    current: list[str] = []
    for raw in fields:
        value = raw.decode()
        if value == "CALL":
            if current:
                calls_seen.append(current)
            current = []
        elif value:
            current.append(value)
    if current:
        calls_seen.append(current)

    install_call = next(call for call in calls_seen if "ARG=install" in call)
    assert f"CONFIG={config}" in install_call
    assert install_call == [
        f"CONFIG={config}",
        "ARG=service",
        "ARG=install",
        "ARG=--host",
        "ARG=0.0.0.0",
        "ARG=--port",
        "ARG=3456",
        "ARG=--env",
        f"ARG={expected_env_file}",
    ]

    # An updater must reject a path that systemd would read differently BEFORE it
    # invokes the old service command, because that command unregisters the service.
    calls.unlink()
    config.write_text(
        json.dumps({"host": "0.0.0.0", "port": 3456, "env_file": str(expected_env_file) + "\n"}),
        encoding="utf-8",
    )
    rejected = subprocess.run(
        [bash],
        input=(ROOT / script_name).read_text(encoding="utf-8"),
        text=True,
        cwd=tmp_path,
        env=env,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert rejected.returncode != 0
    assert "cannot be represented safely" in rejected.stdout
    assert not calls.exists(), "preflight must not stop or unregister the service"

    # EnvironmentFile= treats these as glob expressions. A configured literal path
    # must be rejected before the updater invokes even its first service command.
    for glob_path in ("key-*.env", "key-?.env", "[prod]/key.env"):
        config.write_text(
            json.dumps({"host": "0.0.0.0", "port": 3456, "env_file": str(config.parent / glob_path)}),
            encoding="utf-8",
        )
        rejected_glob = subprocess.run(
            [bash],
            input=(ROOT / script_name).read_text(encoding="utf-8"),
            text=True,
            cwd=tmp_path,
            env=env,
            capture_output=True,
            timeout=20,
            check=False,
        )

        assert rejected_glob.returncode != 0
        assert "cannot be represented safely" in rejected_glob.stdout
        assert not calls.exists(), "glob preflight must not stop or unregister the service"

    # Up to 1.34.0 the Linux unit did not record RLM_CONFIG_FILE. If such a service
    # used a custom config and the updater starts from a fresh shell, guessing the
    # default path would reset it. Stop before touching the service instead.
    unit.write_text(
        "ExecStart=/opt/rlm-tools-bsl --transport streamable-http "
        "--host 0.0.0.0 --port 3456\n# EnvironmentFile not configured\n",
        encoding="utf-8",
    )

    # The ordinary legacy/default-path upgrade remains automatic: the existing file
    # makes the path unambiguous even though the old unit could not name it.
    default_config = home / ".config" / "rlm-tools-bsl" / "service.json"
    default_config.parent.mkdir(parents=True)
    default_config.write_text(
        json.dumps({"host": "127.0.0.1", "port": 9000, "env_file": None, "exe_path": None}),
        encoding="utf-8",
    )
    stale_default = subprocess.run(
        [bash],
        input=(ROOT / script_name).read_text(encoding="utf-8"),
        text=True,
        cwd=tmp_path,
        env=env,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert stale_default.returncode != 0
    assert "does not match that unit" in stale_default.stdout
    assert not calls.exists(), "a stale default config must not replace the active settings"

    default_config.write_text(
        json.dumps({"host": "0.0.0.0", "port": 3456, "env_file": None, "exe_path": None}),
        encoding="utf-8",
    )
    legacy_default = subprocess.run(
        [bash],
        input=(ROOT / script_name).read_text(encoding="utf-8"),
        text=True,
        cwd=tmp_path,
        env=env,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert legacy_default.returncode == 0, legacy_default.stdout + legacy_default.stderr
    assert b"ARG=install\0" in calls.read_bytes()
    calls.unlink()
    default_config.unlink()

    legacy = subprocess.run(
        [bash],
        input=(ROOT / script_name).read_text(encoding="utf-8"),
        text=True,
        cwd=tmp_path,
        env=env,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert legacy.returncode != 0
    assert "did not record RLM_CONFIG_FILE" in legacy.stdout
    assert not calls.exists(), "an ambiguous legacy unit must be left untouched"

    # A quoted empty shell expansion is an explicit input error, not permission to
    # clear the saved .env setting.
    unit.unlink()
    blank_env = subprocess.run(
        [bash, "-s", "--", "   "],
        input=(ROOT / script_name).read_text(encoding="utf-8"),
        text=True,
        cwd=tmp_path,
        env=env,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert blank_env.returncode != 0
    assert "empty .env path" in blank_env.stdout
    assert not calls.exists(), "invalid input must be rejected before service commands"


@pytest.mark.parametrize("script_name", ["simple-install.ps1", "simple-install-from-pip.ps1"])
def test_powershell_upgrade_reuses_registry_config_path(script_name):
    text = (ROOT / script_name).read_text(encoding="utf-8")

    assert "function Get-InstalledServiceConfigPath" in text
    assert r"HKLM:\SYSTEM\CurrentControlSet\Services\rlm-tools-bsl" in text
    assert "$env:RLM_CONFIG_FILE = $configFile" in text
    assert "IsPathRooted([string]$savedEnvFile)" in text
    assert 'Write-Warning "Logs:         $configLogFile"' in text
    assert '$PSBoundParameters.ContainsKey("EnvFile")' in text
    assert "IsNullOrWhiteSpace($EnvFile)" in text


@pytest.mark.parametrize(
    "script_name",
    ["simple-install.sh", "simple-install-from-pip.sh", "simple-install.ps1", "simple-install-from-pip.ps1"],
)
def test_fresh_install_message_keeps_dotenv_fallback_explicit(script_name):
    text = (ROOT / script_name).read_text(encoding="utf-8")

    assert "normal user/CWD .env fallbacks remain enabled." in text
    assert "No .env found - service will start without it" not in text


@pytest.mark.parametrize("script_name", ["simple-install.sh", "simple-install-from-pip.sh"])
def test_shell_checks_config_destination_before_service_commands(script_name):
    text = (ROOT / script_name).read_text(encoding="utf-8")
    call = "assert_config_destination_writable"
    stop_call = text.index("rlm-tools-bsl service stop")

    assert text.count(call) == 3  # definition, early preflight, last-moment recheck
    assert text.rindex(call) < stop_call


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell registry-reader contract")
@pytest.mark.parametrize("script_name", ["simple-install.ps1", "simple-install-from-pip.ps1"])
def test_powershell_registry_reader_is_case_insensitive(script_name):
    powershell = shutil.which("powershell.exe")
    if not powershell:
        pytest.skip("Windows PowerShell is unavailable")
    text = (ROOT / script_name).read_text(encoding="utf-8")
    start = text.index("function Get-InstalledServiceConfigPath")
    end = text.index("$configWasExplicit", start)
    function_text = text[start:end]
    driver = (
        "$script:fakeKey = New-Object PSObject\n"
        "$script:fakeKey | Add-Member -MemberType ScriptMethod -Name GetValue -Value "
        "{ param($name) @('PYTHONPATH=x', 'rlm_config_file=relative\\service.json') }\n"
        "function Get-Item { return $script:fakeKey }\n"
        + function_text
        + "Write-Output (Get-InstalledServiceConfigPath)\n"
    )

    result = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", driver],
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == r"relative\service.json"


@pytest.mark.skipif(os.name != "nt", reason="Windows file-sharing semantics")
@pytest.mark.parametrize("script_name", ["simple-install.ps1", "simple-install-from-pip.ps1"])
def test_powershell_rejects_locked_config_before_service_commands(tmp_path, script_name):
    powershell = shutil.which("powershell.exe")
    if not powershell:
        pytest.skip("Windows PowerShell is unavailable")

    text = (ROOT / script_name).read_text(encoding="utf-8")
    start = text.index("function Assert-ConfigReplaceable")
    end = text.index("function Copy-FileWithDacl", start)
    function_text = text[start:end]
    call = "Assert-ConfigReplaceable $configFile"
    stop_call = text.index("& rlm-tools-bsl service stop")
    assert text.count(call) == 2
    assert text.index(call, end) < stop_call
    assert text.rindex(call) < stop_call

    config = tmp_path / "service.json"
    config.write_text('{"host":"127.0.0.1","port":9000}', encoding="utf-8")
    quoted_config = str(config).replace("'", "''")
    driver = (
        '$ErrorActionPreference = "Stop"\n'
        + function_text
        + f"$configFile = '{quoted_config}'\n"
        + "$lock = [System.IO.File]::Open($configFile, "
        + "[System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::Read)\n"
        + "try { Assert-ConfigReplaceable $configFile } finally { $lock.Dispose() }\n"
    )

    locked = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", driver],
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    output = locked.stdout + locked.stderr
    assert locked.returncode != 0
    assert "cannot be safely replaced" in output
    assert "Close any editor or other program" in output

    read_only = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            '$ErrorActionPreference = "Stop"\n'
            + function_text
            + f"$configFile = '{quoted_config}'\n"
            + "$item = Get-Item -LiteralPath $configFile\n"
            + "$item.IsReadOnly = $true\n"
            + "try { Assert-ConfigReplaceable $configFile } "
            + "finally { (Get-Item -LiteralPath $configFile).IsReadOnly = $false }\n",
        ],
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert read_only.returncode != 0
    assert "is read-only and cannot be safely replaced" in read_only.stdout + read_only.stderr

    unlocked = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            '$ErrorActionPreference = "Stop"\n'
            + function_text
            + f"Assert-ConfigReplaceable '{quoted_config}'\n"
            + 'Write-Output "replaceable"\n',
        ],
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert unlocked.returncode == 0, unlocked.stderr
    assert unlocked.stdout.strip() == "replaceable"
    assert list(tmp_path.rglob("service.json.partial.*")) == []

    missing_config = tmp_path / "new-config-dir" / "service.json"
    quoted_missing = str(missing_config).replace("'", "''")
    missing = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            '$ErrorActionPreference = "Stop"\n'
            + function_text
            + f"Assert-ConfigReplaceable '{quoted_missing}'\n"
            + 'Write-Output "creatable"\n',
        ],
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert missing.returncode == 0, missing.stderr
    assert missing.stdout.strip() == "creatable"
    assert missing_config.parent.is_dir()
    assert not missing_config.exists()
    assert list(missing_config.parent.glob("service.json.partial.*")) == []


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell path semantics")
@pytest.mark.parametrize("script_name", ["simple-install.ps1", "simple-install-from-pip.ps1"])
def test_powershell_saved_relative_env_is_config_relative(tmp_path, script_name):
    powershell = shutil.which("powershell.exe")
    if not powershell:
        pytest.skip("Windows PowerShell is unavailable")
    text = (ROOT / script_name).read_text(encoding="utf-8")
    start = text.index("if ($savedHost -isnot [string])")
    end = text.index("$savedPort = ConvertTo-PortNumber", start)
    normalisation = text[start:end]
    config = tmp_path / "config" / "service.json"
    driver = (
        '$savedHost = "host"\n'
        '$savedEnvFile = ".env"\n'
        f"$configFile = '{str(config).replace(chr(39), chr(39) * 2)}'\n"
        + normalisation
        + "Write-Output $savedEnvFile\n"
    )

    result = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", driver],
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()) == config.parent / ".env"


def test_diagnostic_normalises_legacy_relative_registry_path():
    text = (ROOT / "diagnose-service-win.ps1").read_text(encoding="utf-8")

    assert "[System.IO.Path]::IsPathRooted($fromRegistry)" in text
    assert "Join-Path ([System.Environment]::SystemDirectory) $fromRegistry" in text
