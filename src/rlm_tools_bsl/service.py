"""Service management for rlm-tools-bsl HTTP server (Windows SC / Linux systemd)."""

import json
import os
import pathlib
import sys
from collections.abc import Mapping
from dataclasses import dataclass

CONFIG_DIR = pathlib.Path.home() / ".config" / "rlm-tools-bsl"
CONFIG_FILE = CONFIG_DIR / "service.json"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9000

# The install scripts copy service.json next to itself under this name before they hand
# the service over to `uninstall`, so an interrupted upgrade can be recovered by the
# next run.  Named here because `uninstall --purge` has to remove it too: otherwise a
# leftover copy would resurrect settings the user deliberately purged.
BACKUP_SUFFIX = ".rlm-backup"
# Write-once rescue copy of a config that could not be parsed. The transient backup above
# is refreshed on every run; this one is never overwritten, because it may be the only
# remaining trace of what the user had before the upgrade replaced it.
RESCUE_SUFFIX = ".rlm-unreadable"
# Note left by an install script when the config is a SYMLINK: the legacy uninstall
# deletes the link itself, and a run interrupted right after that has nothing left to
# read the target from.
LINKTARGET_SUFFIX = ".rlm-linktarget"


def backup_path(config: pathlib.Path) -> pathlib.Path:
    """Path of the installer's sidecar copy of *config*."""
    return config.with_name(config.name + BACKUP_SUFFIX)


def rescue_path(config: pathlib.Path) -> pathlib.Path:
    """Path of the write-once copy of an unparseable *config*."""
    return config.with_name(config.name + RESCUE_SUFFIX)


def installer_leftovers(config: pathlib.Path) -> list[pathlib.Path]:
    """Everything an install script or a failed save may have left next to *config*.

    The sidecar copy, the `<name>.partial.<pid>` staging files an interrupted install
    leaves behind, and the `<name>.new.<pid>` file an interrupted save leaves behind --
    each of them holds the same settings as the config itself.

    Matched by string prefix rather than by glob ON PURPOSE: the config name comes from
    the user (RLM_CONFIG_FILE), and a perfectly legal `service[1].json` would make a
    glob pattern match somebody else's `service1.json.partial.42` while missing its own.
    These paths are then deleted, so a wrong match is destructive.
    """
    prefixes = (
        config.name + ".partial.",
        config.name + BACKUP_SUFFIX + ".partial.",
        config.name + ".new.",
    )

    def is_ours(name: str) -> bool:
        # The tail after the prefix is a PID and nothing else. Without that check a
        # user's own `service.json.partial.manual-copy` would be deleted as ours.
        for prefix in prefixes:
            if name.startswith(prefix):
                return name[len(prefix) :].isdigit()
        return False

    found = [backup_path(config), rescue_path(config), config.with_name(config.name + LINKTARGET_SUFFIX)]
    try:
        entries = sorted(config.parent.iterdir())
    except OSError:
        return found
    found.extend(entry for entry in entries if is_ours(entry.name) and entry.is_file())
    return found


def _config_path() -> pathlib.Path:
    """Return the config file path.

    On Windows, the service runs as LocalSystem whose home dir differs from
    the installing user.  The install step writes RLM_CONFIG_FILE into the
    service's registry Environment so load_config() can find it at runtime.
    """
    override = os.environ.get("RLM_CONFIG_FILE")
    if override:
        # Absolute on purpose: this path is written into the Windows service registry,
        # and the SCM starts the service from a different working directory, where a
        # relative path would resolve to a different (or missing) file.
        #
        # Deliberately NOT expanduser(): the shell expands `~` before we ever see the
        # value, and every other consumer of RLM_CONFIG_FILE (_config.py, the install
        # scripts) takes it literally.  Expanding it only here would split one setting
        # into two different files.
        path = pathlib.Path(override)
        return path if path.is_absolute() else (pathlib.Path.cwd() / path)
    return CONFIG_FILE


def _absolute_env_file(
    env_file: str | None,
    *,
    base_dir: pathlib.Path | None = None,
    posix: bool = False,
) -> str | None:
    """Return a stable path for a service process whose CWD is not the install CWD.

    ``posix=True`` lets the Linux backend retain POSIX semantics even when its unit
    rendering is tested on Windows. Tildes stay literal, consistently with
    ``RLM_CONFIG_FILE``.
    """
    if env_file is None:
        return None
    path_for_check = pathlib.PurePosixPath(env_file) if posix else pathlib.Path(env_file)
    if path_for_check.is_absolute():
        return env_file
    return str(((pathlib.Path.cwd() if base_dir is None else base_dir) / env_file).absolute())


@dataclass(frozen=True)
class _FileSnapshot:
    path: pathlib.Path
    target: pathlib.Path
    existed: bool
    contents: bytes | None


def _snapshot_file(path: pathlib.Path) -> _FileSnapshot:
    """Capture exact bytes before a multi-step service installation changes a file."""
    target = pathlib.Path(os.path.realpath(path)) if path.is_symlink() else path
    try:
        contents = target.read_bytes()
    except FileNotFoundError:
        return _FileSnapshot(path=path, target=target, existed=False, contents=None)
    return _FileSnapshot(path=path, target=target, existed=True, contents=contents)


def _write_file_atomically(path: pathlib.Path, payload: str | bytes) -> None:
    """Replace a regular file (or a symlink target) without exposing partial data."""
    path.parent.mkdir(parents=True, exist_ok=True)
    target = pathlib.Path(os.path.realpath(path)) if path.is_symlink() else path
    staged = target.with_name(f"{target.name}.new.{os.getpid()}")
    try:
        if isinstance(payload, bytes):
            staged.write_bytes(payload)
        else:
            staged.write_text(payload, encoding="utf-8")
        try:
            os.chmod(staged, target.stat().st_mode)
        except OSError:
            pass  # no previous file, or a filesystem that does not do modes
        _copy_windows_dacl(target, staged)
        os.replace(staged, target)
    finally:
        staged.unlink(missing_ok=True)


def _restore_file(snapshot: _FileSnapshot) -> None:
    """Restore a snapshot exactly, including the originally-absent state."""
    if snapshot.existed:
        assert snapshot.contents is not None
        _write_file_atomically(snapshot.path, snapshot.contents)
    else:
        # For a dangling symlink, the transaction created its target. Remove the target
        # and leave the link itself in the same dangling state in which we found it.
        snapshot.target.unlink(missing_ok=True)


def save_config(
    host: str,
    port: int,
    env_file: str | None,
    exe_path: str | None = None,
    *,
    no_env: bool = False,
) -> None:
    """Write the service config ATOMICALLY.

    A plain write truncates first, so a failure halfway (full disk, quota) would leave a
    zero-length or half-written config where working settings used to be -- and for a
    direct `service install` there is no installer backup to fall back on.  Staging into
    a sibling file and replacing keeps the previous content until the new one is whole.
    """
    cfg = _config_path()
    payload = json.dumps(
        {"host": host, "port": port, "env_file": env_file, "exe_path": exe_path, "no_env": no_env},
        indent=2,
    )
    _write_file_atomically(cfg, payload)


def _copy_windows_dacl(source: pathlib.Path, destination: pathlib.Path) -> None:
    """Carry the explicit DACL of *source* over to *destination* (Windows only).

    `os.chmod` does not touch ACLs, so without this a hardened `service.json` -- one with
    an explicit ACE letting the LocalSystem service read it, in a directory that does not
    hand that down -- would come back after an atomic replace with the directory's
    inherited rights only.  Best effort by design: on a machine without pywin32 (the
    `service` extra) nothing can be copied, and that is not a reason to fail a save.
    """
    if sys.platform != "win32" or not source.exists():
        return
    try:
        import win32security
    except ImportError:
        return
    try:
        info = win32security.DACL_SECURITY_INFORMATION
        dacl = win32security.GetFileSecurity(str(source), info).GetSecurityDescriptorDacl()
        descriptor = win32security.GetFileSecurity(str(destination), info)
        descriptor.SetSecurityDescriptorDacl(1, dacl, 0)
        win32security.SetFileSecurity(str(destination), info, descriptor)
    except Exception:  # noqa: BLE001 - permissions are best effort, the write is not
        pass


def load_config() -> dict:
    cfg = _config_path()
    if not cfg.exists():
        return {"host": DEFAULT_HOST, "port": DEFAULT_PORT, "env_file": None}
    data = json.loads(cfg.read_text(encoding="utf-8"))
    if isinstance(data, dict) and isinstance(data.get("env_file"), str):
        data["env_file"] = _absolute_env_file(data["env_file"], base_dir=cfg.parent)
    return data


class SavedConfigError(Exception):
    """The config file is there but unusable (unreadable, not UTF-8, not a JSON object)."""

    def __init__(self, path: pathlib.Path, reason: str) -> None:
        super().__init__(f"{path}: {reason}")
        self.path = path
        self.reason = reason


def read_saved_config() -> dict | None:
    """Return the stored config, or ``None`` when there is no installation yet.

    Unlike :func:`load_config` this never invents defaults: the install path has to
    tell "nothing installed" apart from "the user deliberately chose 127.0.0.1".  A
    file that EXISTS but cannot be used is a third case and raises: treating it as
    "nothing saved" would overwrite exactly the settings we failed to read, which is
    the damage this release is about.
    """
    cfg = _config_path()
    try:
        raw = cfg.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as exc:
        raise SavedConfigError(cfg, exc.strerror or str(exc)) from exc
    except ValueError as exc:  # UnicodeDecodeError is a ValueError, not an OSError
        raise SavedConfigError(cfg, f"не читается как UTF-8 ({exc})") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise SavedConfigError(cfg, f"не разбирается как JSON ({exc})") from exc
    if not isinstance(data, dict):
        raise SavedConfigError(cfg, "содержит не объект JSON")
    return data


def _first_str(*candidates: object) -> str | None:
    for value in candidates:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _first_path(*candidates: object) -> str | None:
    """Like :func:`_first_str`, but returns the value VERBATIM.

    A POSIX filename may legally begin or end with a space, so trimming a stored
    ``.env`` path would silently point the service at a file that does not exist.
    """
    for value in candidates:
        if isinstance(value, str) and value.strip():
            return value
    return None


def _first_port(*candidates: object) -> int | None:
    for value in candidates:
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, float) and not value.is_integer():
            continue  # a hand-edited "port": 3000.7 is a mistake, not port 3000
        try:
            port = int(value)
        except (TypeError, ValueError):
            continue
        if 1 <= port <= 65535:
            return port
    return None


def resolve_install_settings(
    cli_host: str | None = None,
    cli_port: int | None = None,
    cli_env: str | None = None,
    drop_env: bool = False,
    saved: dict | None = None,
    environ: Mapping[str, str] | None = None,
) -> tuple[str, int, str | None]:
    """Merge install options: explicit flag → saved config → env → built-in default.

    Re-registering the service without flags is what every update script does;
    it must not silently move a service the user moved to another host/port
    back to 127.0.0.1:9000, nor drop the configured ``.env``.
    """
    env = os.environ if environ is None else environ
    installed = saved is not None
    saved = saved or {}

    # An explicit flag must never lose to a saved value -- not even a nonsensical one:
    # falling through would hide the typo behind a service that looks like it worked.
    if cli_port is not None and _first_port(cli_port) is None:
        raise ValueError(f"недопустимый порт: {cli_port!r} (ожидается целое 1..65535)")
    if cli_env is not None and _first_path(cli_env) is None:
        raise ValueError("пустое значение --env; для отказа от .env используйте --no-env")

    host = _first_str(cli_host, saved.get("host"))
    port = _first_port(cli_port, saved.get("port"))

    # For an EXISTING installation the environment and the built-in defaults are not
    # allowed to fill a gap: a config of `{}` or one with `"port": "oops"` is still a
    # config, and quietly answering 127.0.0.1:9000 there is issue #33 all over again --
    # the service moves and nobody is told. Only a fresh install may fall through.
    if installed:
        for name, value in (("host", host), ("port", port)):
            if value is None:
                raise ValueError(
                    f"в сохранённом конфиге нет пригодного значения {name} "
                    f"({saved.get(name)!r}); задайте --host и --port явно"
                )
    else:
        env_port = env.get("RLM_PORT")
        if cli_port is None and env_port not in (None, "") and _first_port(env_port) is None:
            # RLM_PORT is an explicit first-install choice too.  Silently replacing a
            # typo with 9000 would report a successful service on the wrong endpoint.
            raise ValueError(f"недопустимый RLM_PORT: {env_port!r} (ожидается целое 1..65535)")
        host = host or _first_str(env.get("RLM_HOST")) or DEFAULT_HOST
        port = port or _first_port(env_port) or DEFAULT_PORT

    if drop_env:
        env_file: str | None = None
    elif cli_env is not None:
        env_file = _first_path(cli_env)
    else:
        env_file = _first_path(saved.get("env_file"))

    return host, port, env_file


def handle_service_command(args) -> None:
    if sys.platform == "win32":
        try:
            from rlm_tools_bsl._service_win import (  # type: ignore[import]
                install,
                uninstall,
                start,
                stop,
                status,
            )
        except ImportError:
            print(
                "Ошибка: для управления службой на Windows требуется pywin32.\n"
                "Установите: uv tool install rlm-tools-bsl --extra service\n"
                "  или: pip install pywin32"
            )
            raise SystemExit(1)
    else:
        from rlm_tools_bsl._service_linux import (
            install,
            uninstall,
            start,
            stop,
            status,
        )

    action = args.service_action
    if action == "install":
        cli_host = getattr(args, "host", None)
        cli_port = getattr(args, "port", None)
        cli_env = getattr(args, "env", None)
        drop_env = getattr(args, "no_env", False)
        if cli_host is not None and not cli_host.strip():
            # Otherwise an empty --host would count as "explicitly specified", pass the
            # guard below and let the resolver quietly fall back to 127.0.0.1.
            print("Ошибка: пустое значение --host")
            raise SystemExit(1)
        try:
            saved = read_saved_config()
        except SavedConfigError as exc:
            bytes_unreadable = isinstance(exc.__cause__, OSError)
            if bytes_unreadable:
                # Even a complete set of flags cannot make this safe: both backends
                # snapshot the old bytes before a multi-step registration so they can
                # restore them if a later filesystem/registry/systemd operation fails.
                print(f"Ошибка: {exc}")
                print(
                    "Установка остановлена: прежний конфиг нельзя прочитать для безопасного отката.\n"
                    "Почините файл или права на него; либо удалите настройки явно — "
                    "service uninstall --purge"
                )
                raise SystemExit(1) from None
            # Refuse only when a value would actually have come from that file.
            if cli_host is None or cli_port is None or (cli_env is None and not drop_env):
                print(f"Ошибка: {exc}")
                print(
                    "Установка остановлена, чтобы не затереть настройки, которые не удалось прочитать.\n"
                    "Что сделать: починить содержимое файла; либо задать все параметры явно "
                    "(--host, --port и --env/--no-env); либо удалить конфиг — service uninstall --purge"
                )
                raise SystemExit(1) from None
            print(f"Внимание: {exc}; продолжаю с явно заданными параметрами.")
            saved = None
        try:
            host, port, env_file = resolve_install_settings(
                cli_host=cli_host,
                cli_port=cli_port,
                cli_env=cli_env,
                drop_env=drop_env,
                saved=saved,
            )
        except ValueError as exc:
            print(f"Ошибка: {exc}")
            raise SystemExit(1) from None
        if env_file is not None and cli_env is None:
            # A relative value already stored in service.json has no surviving install
            # CWD. Give it one deterministic legacy interpretation. Explicit --env is
            # normalised by the selected backend against the caller's current CWD.
            env_file = _absolute_env_file(
                env_file,
                base_dir=_config_path().parent,
                posix=sys.platform != "win32",
            )
        # Before `no_env` was persisted, `env_file: null` still allowed the server's
        # user/CWD fallback. Only an explicit --no-env (or its new saved marker) may
        # disable those fallbacks; otherwise an upgrade would change legacy behaviour.
        no_env = drop_env or (
            cli_env is None and env_file is None and saved is not None and saved.get("no_env") is True
        )
        if saved:
            print(f"Прежние настройки найдены: {_config_path()} (значения без явных флагов сохраняются)")
        print(f"Параметры службы: host={host}, port={port}, env_file={env_file or 'не задан'}")
        install(host=host, port=port, env_file=env_file, no_env=no_env)
    elif action == "uninstall":
        uninstall(purge=getattr(args, "purge", False))
    elif action == "start":
        start()
    elif action == "stop":
        stop()
    elif action == "status":
        status()
    else:
        print("Использование: rlm-tools-bsl service {install|start|stop|status|uninstall}")
        raise SystemExit(1)
