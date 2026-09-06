"""Linux systemd --user service management for rlm-tools-bsl."""

import os
import shutil
import subprocess
from pathlib import Path

from rlm_tools_bsl._config import SERVICE_NO_ENV_VAR
from rlm_tools_bsl.service import (
    _absolute_env_file,
    _config_path,
    _restore_file,
    _snapshot_file,
    _write_file_atomically,
    installer_leftovers,
    save_config,
)

SERVICE_NAME = "rlm-tools-bsl"
BACKSLASH = chr(92)
NEWLINE = chr(10)
CARRIAGE_RETURN = chr(13)


def _get_version() -> str:
    try:
        from importlib.metadata import version

        return version("rlm-tools-bsl")
    except Exception:
        return "?"


def _unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / f"{SERVICE_NAME}.service"


def _systemd_specifiers(value: str) -> str:
    """Neutralise systemd specifier expansion (`%u`, `%h`, ...) in a literal value.

    Applies to every directive that expands specifiers -- ExecStart and EnvironmentFile
    among them -- so a legal path like /srv/%u/service.json keeps its own name instead of
    quietly becoming somebody's user name.
    """
    return value.replace("%", "%%")


def _systemd_escape(value: str) -> str:
    """Escape a value to be placed INSIDE QUOTES on a systemd command line.

    Three separate mechanisms would otherwise rewrite a perfectly legal path:
    C-style escaping applies inside quotes (a backslash followed by n arrives as a
    newline), ExecStart expands environment variables (a literal $ needs $$), and
    specifiers expand everywhere (%u becomes the user name).
    """
    escaped = value.replace(BACKSLASH, BACKSLASH * 2).replace('"', BACKSLASH + '"')
    # A unit file is parsed line by line, so a path holding a real newline (legal on
    # POSIX) would cut ExecStart in half and leave the unit unparseable -- with the old
    # service already gone. C-style escapes are resolved inside quotes, so writing them
    # out is exactly how the character survives.
    for raw, escape in (("\n", BACKSLASH + "n"), ("\r", BACKSLASH + "r"), ("\t", BACKSLASH + "t")):
        escaped = escaped.replace(raw, escape)
    return _systemd_specifiers(escaped).replace("$", "$$")


def _exe_path() -> str:
    return shutil.which("rlm-tools-bsl") or "rlm-tools-bsl"


def _rollback_install(config_snapshot, unit_snapshot, *, was_enabled: bool, was_active: bool) -> list[str]:
    """Best-effort restoration for a failed multi-step systemd installation."""
    failures: list[str] = []
    for label, snapshot in (("service.json", config_snapshot), ("unit-файл", unit_snapshot)):
        try:
            _restore_file(snapshot)
        except Exception as exc:  # noqa: BLE001 - collect every rollback failure
            failures.append(f"{label}: {exc}")

    try:
        reloaded = subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
        if reloaded.returncode != 0:
            failures.append(f"daemon-reload: код {reloaded.returncode}")
    except Exception as exc:  # noqa: BLE001 - rollback must continue with later steps
        reloaded = None
        failures.append(f"daemon-reload: {exc}")

    enable_action = "enable" if was_enabled else "disable"
    try:
        enabled = subprocess.run(["systemctl", "--user", enable_action, SERVICE_NAME], check=False)
        if enabled.returncode != 0:
            failures.append(f"{enable_action}: код {enabled.returncode}")
    except Exception as exc:  # noqa: BLE001 - rollback must report all failures
        failures.append(f"{enable_action}: {exc}")

    # Restart only after the old files are back and the manager has reloaded them. If
    # either step failed, restarting could launch the just-rejected configuration.
    if was_active and not failures and reloaded is not None:
        try:
            restarted = subprocess.run(["systemctl", "--user", "restart", SERVICE_NAME], check=False)
            if restarted.returncode != 0:
                failures.append(f"restart прежней службы: код {restarted.returncode}")
        except Exception as exc:  # noqa: BLE001 - caller prints manual recovery steps
            failures.append(f"restart прежней службы: {exc}")
    return failures


def install(host: str, port: int, env_file: str | None, *, no_env: bool = False) -> None:
    env_file = _absolute_env_file(env_file, posix=True)

    # EnvironmentFile is not a quoted command line (the value runs to end of line), so
    # only specifier expansion has to be neutralised here -- an inner backslash stays
    # part of the path, and doubling it would corrupt a legal POSIX name.
    #
    # systemd reads the value of a directive AFTER stripping it, continues the line when
    # it ends in an ODD number of backslashes, and ends the directive at a newline. If any
    # of that would make it read a path OTHER than the configured one, the directive is
    # left out entirely.
    #
    # Left out rather than written, because handing systemd a neighbouring file is worse
    # than handing it nothing: its variables are already in the process environment by the
    # time the server loads the configured file itself, and `load_dotenv(override=False)`
    # cannot replace them. The path stays in service.json either way.
    env_line = "# EnvironmentFile not configured"
    if env_file:
        trailing_backslashes = len(env_file) - len(env_file.rstrip(BACKSLASH))
        reads_another_path = (
            NEWLINE in env_file
            or CARRIAGE_RETURN in env_file
            or env_file != env_file.strip()
            or trailing_backslashes % 2 == 1
            # EnvironmentFile= accepts glob expressions. A literal POSIX filename
            # containing one of these characters could therefore select a neighbour
            # (or no file at all) before the server gets a chance to load its own path.
            or any(char in env_file for char in "*?[")
        )
        if reads_another_path:
            print(
                f"ОШИБКА: путь к .env ({env_file!r}) нельзя передать службе через unit-файл: "
                "systemd обрезает значение директивы, продолжает строку после нечётного числа "
                "обратных косых черт, обрывает её на переводе строки, а `*`, `?` и `[` "
                "считает wildcard-шаблоном — он прочитал бы другой "
                "путь. Установка не изменена; переименуйте файл и повторите команду."
            )
            raise SystemExit(1)
        else:
            env_line = f"EnvironmentFile=-{_systemd_specifiers(env_file)}"
    exe = _exe_path()
    # RLM_CONFIG_FILE is controlled through `env` on the command line, not with
    # Environment=: systemd applies EnvironmentFile= AFTER Environment=.  For an
    # override, pin the absolute path written by this install.  For the default path,
    # remove a value that a .env may have injected; leaving the variable unset also keeps
    # the service and the ordinary CLI on their shared ~/.cache index root.
    config_env = (
        f'"RLM_CONFIG_FILE={_systemd_escape(str(_config_path()))}"'
        if os.environ.get("RLM_CONFIG_FILE")
        else "-u RLM_CONFIG_FILE"
    )
    no_env_marker = f' "{SERVICE_NO_ENV_VAR}=1"' if no_env else ""
    exec_start = (
        f"/usr/bin/env {config_env}{no_env_marker} "
        f'"{_systemd_escape(exe)}" --transport streamable-http --host "{_systemd_escape(host)}" --port {port}'
    )
    unit = (
        "[Unit]\n"
        f"Description=RLM Tools BSL (MCP HTTP Server) v{_get_version()}\n"
        "After=network.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={exec_start}\n"
        f"{env_line}\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )
    config_snapshot = _snapshot_file(_config_path())
    unit_snapshot = _snapshot_file(_unit_path())
    active = subprocess.run(["systemctl", "--user", "is-active", "--quiet", SERVICE_NAME], check=False)
    enabled = subprocess.run(["systemctl", "--user", "is-enabled", "--quiet", SERVICE_NAME], check=False)

    try:
        save_config(host, port, env_file, no_env=no_env)
        _write_file_atomically(_unit_path(), unit)
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "--user", "enable", SERVICE_NAME], check=True)

        # A repeat install is how one changes host/port on an installed service. Without
        # a restart the unit file would say one thing while the process keeps serving the
        # old address.
        if active.returncode == 0:
            restarted = subprocess.run(["systemctl", "--user", "restart", SERVICE_NAME], check=False)
            if restarted.returncode != 0:
                raise RuntimeError(f"systemctl restart вернул {restarted.returncode}")
            print("Служба перезапущена с новыми параметрами.")
    except Exception as exc:
        failures = _rollback_install(
            config_snapshot,
            unit_snapshot,
            was_enabled=enabled.returncode == 0,
            was_active=active.returncode == 0,
        )
        print(f"ОШИБКА: установка службы не завершена ({exc}).")
        if failures:
            print("Откат выполнен не полностью: " + "; ".join(failures))
            print("Проверьте вручную: rlm-tools-bsl service status")
        else:
            print("Прежние service.json, unit-файл и состояние службы восстановлены.")
        raise SystemExit(1) from None

    print(f"Служба '{SERVICE_NAME}' установлена.")
    print(f"Unit-файл: {_unit_path()}")
    print("Для автозапуска без входа в систему выполните:")
    print(f"  loginctl enable-linger {Path.home().name}")
    print("Запуск: rlm-tools-bsl service start")


def uninstall(purge: bool = False) -> None:
    """Remove the unit.  The config survives unless *purge* is asked for.

    Update scripts call uninstall + install back to back; deleting service.json
    here used to throw away the user's host/port/.env on every upgrade.
    """
    try:
        subprocess.run(["systemctl", "--user", "disable", "--now", SERVICE_NAME], check=False)
    except Exception:
        pass
    _unit_path().unlink(missing_ok=True)
    config = _config_path()
    if purge:
        # Copies FIRST, config last: if removing a copy fails (read-only, locked), the
        # config is still there, and nothing can be resurrected from a half-purge.
        for leftover in installer_leftovers(config):
            leftover.unlink(missing_ok=True)
        config.unlink(missing_ok=True)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    print(f"Служба '{SERVICE_NAME}' удалена.")
    if purge:
        print(f"Конфиг службы удалён: {config}")
    elif config.exists():
        print(f"Настройки сохранены: {config} (удалить вместе со службой: service uninstall --purge)")


def start() -> None:
    subprocess.run(["systemctl", "--user", "start", SERVICE_NAME], check=True)
    print("Служба запущена.")


def stop() -> None:
    subprocess.run(["systemctl", "--user", "stop", SERVICE_NAME], check=True)
    print("Служба остановлена.")


def status() -> None:
    result = subprocess.run(
        ["systemctl", "--user", "status", SERVICE_NAME],
        capture_output=True,
        text=True,
    )
    print(result.stdout or result.stderr)
