"""Regression test for `_set_service_environment` in `_service_win.py`.

Guards against a developer "simplifying" the function by inlining the env_vars
list again instead of delegating to `build_service_env_vars` — which is
exactly the path that produced the broken v1.9.3 PYTHONPATH (issue #13).

The test stubs the bare minimum `win32*` + `winreg` modules in `sys.modules`
so `import rlm_tools_bsl._service_win` succeeds on Linux CI without pywin32.
"""

from __future__ import annotations

import pathlib
import shutil
import sys
import types

import pytest


def _install_stubs(monkeypatch):
    """Stub the bare minimum so `import rlm_tools_bsl._service_win` works on Linux.

    Notes:
      - DO NOT stub `socket` — `_service_win` imports `urllib.request`, which
        needs the real stdlib socket module.
      - `win32serviceutil.ServiceFramework` must be a class (used as a base
        class for `RlmWindowsService` at module import time).
      - `servicemanager` is NOT imported by `_service_win` (the
        pythonservice.exe bootstrap problem we're fixing is on the
        pythonservice side, not ours), so no stub needed for it.
    """
    win32service = types.ModuleType("win32service")
    win32event = types.ModuleType("win32event")
    win32serviceutil = types.ModuleType("win32serviceutil")
    win32serviceutil.ServiceFramework = object
    monkeypatch.setitem(sys.modules, "win32service", win32service)
    monkeypatch.setitem(sys.modules, "win32event", win32event)
    monkeypatch.setitem(sys.modules, "win32serviceutil", win32serviceutil)

    captured: dict = {}
    fake_winreg = types.ModuleType("winreg")
    fake_winreg.HKEY_LOCAL_MACHINE = 0
    fake_winreg.KEY_SET_VALUE = 0
    fake_winreg.REG_MULTI_SZ = 7

    class _Key:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    fake_winreg.OpenKeyEx = lambda *a, **kw: _Key()
    fake_winreg.SetValueEx = lambda key, name, _r, typ, val: captured.update(name=name, type=typ, value=val)
    monkeypatch.setitem(sys.modules, "winreg", fake_winreg)
    return captured


def test_set_service_environment_uses_build_helper(monkeypatch, request):
    captured = _install_stubs(monkeypatch)

    sys.modules.pop("rlm_tools_bsl._service_win", None)
    import rlm_tools_bsl

    if hasattr(rlm_tools_bsl, "_service_win"):
        delattr(rlm_tools_bsl, "_service_win")

    def _cleanup():
        sys.modules.pop("rlm_tools_bsl._service_win", None)
        if hasattr(rlm_tools_bsl, "_service_win"):
            delattr(rlm_tools_bsl, "_service_win")

    request.addfinalizer(_cleanup)

    from rlm_tools_bsl import _service_win
    from rlm_tools_bsl._service_env import build_service_env_vars

    _service_win._set_service_environment("svc-X", r"C:\sp", r"C:\cfg")

    assert captured["name"] == "Environment"
    assert captured["type"] == 7
    assert captured["value"] == build_service_env_vars(r"C:\sp", r"C:\cfg")


def test_child_env_keeps_registry_config_path_case_insensitively(monkeypatch, tmp_path, request):
    """A lowercase spelling in .env must not beat the path pinned by the SCM."""
    _install_stubs(monkeypatch)
    _service_win = _import_service_win(monkeypatch, request)

    cfg = tmp_path / "service.json"
    env_file = tmp_path / ".env"
    env_file.write_text("rlm_config_file=redirected/service.json\n", encoding="utf-8")
    monkeypatch.setenv("RLM_CONFIG_FILE", str(cfg))

    env = _service_win._service_child_environment(str(env_file))

    config_entries = [(key, value) for key, value in env.items() if key.upper() == "RLM_CONFIG_FILE"]
    assert config_entries == [("RLM_CONFIG_FILE", str(cfg))]


def test_service_child_loads_legacy_relative_env_from_config_directory(monkeypatch, tmp_path, request):
    _install_stubs(monkeypatch)
    _service_win = _import_service_win(monkeypatch, request)
    cfg = tmp_path / "config" / "service.json"
    cfg.parent.mkdir()
    cfg.write_text('{"host": "127.0.0.1", "port": 9000, "env_file": ".env"}', encoding="utf-8")
    (cfg.parent / ".env").write_text("RLM_RELATIVE_ENV_TEST=loaded\n", encoding="utf-8")
    monkeypatch.setenv("RLM_CONFIG_FILE", str(cfg))
    monkeypatch.chdir(tmp_path)

    loaded = _service_win.load_config()["env_file"]
    env = _service_win._service_child_environment(".env")

    assert loaded == str(cfg.parent / ".env")
    assert env["RLM_RELATIVE_ENV_TEST"] == "loaded"


def test_uninstall_keeps_service_json_unless_purged(monkeypatch, tmp_path, request):
    """Issue #33: on Windows service.json is what the running service reads its
    bind address from, so `service uninstall` (the first half of every upgrade)
    must leave it alone. Only an explicit --purge deletes it."""
    _install_stubs(monkeypatch)
    win32serviceutil = sys.modules["win32serviceutil"]
    win32serviceutil.StopService = lambda name: None
    win32serviceutil.RemoveService = lambda name: None

    sys.modules.pop("rlm_tools_bsl._service_win", None)
    import rlm_tools_bsl

    if hasattr(rlm_tools_bsl, "_service_win"):
        delattr(rlm_tools_bsl, "_service_win")

    def _cleanup():
        sys.modules.pop("rlm_tools_bsl._service_win", None)
        if hasattr(rlm_tools_bsl, "_service_win"):
            delattr(rlm_tools_bsl, "_service_win")

    request.addfinalizer(_cleanup)

    from rlm_tools_bsl import _service_win

    cfg = tmp_path / "service.json"
    cfg.write_text('{"host": "0.0.0.0", "port": 3000, "env_file": null}', encoding="utf-8")
    monkeypatch.setenv("RLM_CONFIG_FILE", str(cfg))

    _service_win.uninstall()
    assert cfg.exists(), "service.json must survive the uninstall half of an upgrade"

    _service_win.uninstall(purge=True)
    assert not cfg.exists()


def test_purge_works_when_service_is_already_gone(monkeypatch, tmp_path, request):
    """After a plain uninstall the service no longer exists, and `--purge` is then the
    only way left to delete the config -- it must not die on ERROR_SERVICE_DOES_NOT_EXIST
    (Linux, where removal is best-effort, has always allowed this)."""
    _install_stubs(monkeypatch)

    class _ServiceError(Exception):
        winerror = 1060  # ERROR_SERVICE_DOES_NOT_EXIST

    def _raise_missing(name):
        raise _ServiceError("The specified service does not exist as an installed service.")

    win32serviceutil = sys.modules["win32serviceutil"]
    win32serviceutil.StopService = _raise_missing
    win32serviceutil.RemoveService = _raise_missing

    sys.modules.pop("rlm_tools_bsl._service_win", None)
    import rlm_tools_bsl

    if hasattr(rlm_tools_bsl, "_service_win"):
        delattr(rlm_tools_bsl, "_service_win")

    def _cleanup():
        sys.modules.pop("rlm_tools_bsl._service_win", None)
        if hasattr(rlm_tools_bsl, "_service_win"):
            delattr(rlm_tools_bsl, "_service_win")

    request.addfinalizer(_cleanup)

    from rlm_tools_bsl import _service_win

    cfg = tmp_path / "service.json"
    cfg.write_text('{"host": "0.0.0.0", "port": 3000, "env_file": null}', encoding="utf-8")
    monkeypatch.setenv("RLM_CONFIG_FILE", str(cfg))

    _service_win.uninstall(purge=True)
    assert not cfg.exists()


def test_other_removal_errors_still_abort(monkeypatch, tmp_path, request):
    """Only "not installed" is forgiven: a real failure (no admin rights, for
    instance) must not silently proceed to delete the settings."""
    _install_stubs(monkeypatch)

    class _AccessDenied(Exception):
        winerror = 5  # ERROR_ACCESS_DENIED

    win32serviceutil = sys.modules["win32serviceutil"]
    win32serviceutil.StopService = lambda name: None

    def _deny(name):
        raise _AccessDenied("Access is denied.")

    win32serviceutil.RemoveService = _deny

    sys.modules.pop("rlm_tools_bsl._service_win", None)
    import rlm_tools_bsl

    if hasattr(rlm_tools_bsl, "_service_win"):
        delattr(rlm_tools_bsl, "_service_win")

    def _cleanup():
        sys.modules.pop("rlm_tools_bsl._service_win", None)
        if hasattr(rlm_tools_bsl, "_service_win"):
            delattr(rlm_tools_bsl, "_service_win")

    request.addfinalizer(_cleanup)

    from rlm_tools_bsl import _service_win

    cfg = tmp_path / "service.json"
    cfg.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("RLM_CONFIG_FILE", str(cfg))

    with pytest.raises(SystemExit):
        _service_win.uninstall(purge=True)
    assert cfg.exists(), "a failed removal must leave the settings alone"


def test_health_probe_host_maps_wildcard_binds(monkeypatch, request):
    """`http://:::3000/health` does not even parse, so a service bound to a wildcard
    would fail every probe and be killed by its own watchdog."""
    _install_stubs(monkeypatch)

    sys.modules.pop("rlm_tools_bsl._service_win", None)
    import rlm_tools_bsl

    if hasattr(rlm_tools_bsl, "_service_win"):
        delattr(rlm_tools_bsl, "_service_win")

    def _cleanup():
        sys.modules.pop("rlm_tools_bsl._service_win", None)
        if hasattr(rlm_tools_bsl, "_service_win"):
            delattr(rlm_tools_bsl, "_service_win")

    request.addfinalizer(_cleanup)

    from urllib.parse import urlsplit

    from rlm_tools_bsl._service_win import health_probe_host

    assert health_probe_host("0.0.0.0") == "127.0.0.1"
    assert health_probe_host("::") == "[::1]"
    assert health_probe_host("[::]") == "[::1]"
    assert health_probe_host("127.0.0.1") == "127.0.0.1"
    assert health_probe_host("example.lan") == "example.lan"
    assert health_probe_host("fe80::1") == "[fe80::1]"

    for bind in ("0.0.0.0", "::", "[::]", "127.0.0.1", "fe80::1", "example.lan"):
        url = f"http://{health_probe_host(bind)}:3000/health"
        assert urlsplit(url).port == 3000, url


def _import_service_win(monkeypatch, request):
    """Fresh import of `_service_win` on top of the stubs, with cleanup."""
    sys.modules.pop("rlm_tools_bsl._service_win", None)
    import rlm_tools_bsl

    if hasattr(rlm_tools_bsl, "_service_win"):
        delattr(rlm_tools_bsl, "_service_win")

    def _cleanup():
        sys.modules.pop("rlm_tools_bsl._service_win", None)
        if hasattr(rlm_tools_bsl, "_service_win"):
            delattr(rlm_tools_bsl, "_service_win")

    request.addfinalizer(_cleanup)
    from rlm_tools_bsl import _service_win

    return _service_win


def test_purge_also_removes_the_installer_backup(monkeypatch, tmp_path, request):
    """The install scripts keep a sidecar copy next to service.json; leaving it behind
    would let the next run resurrect settings the user just purged."""
    _install_stubs(monkeypatch)
    win32serviceutil = sys.modules["win32serviceutil"]
    win32serviceutil.StopService = lambda name: None
    win32serviceutil.RemoveService = lambda name: None

    _service_win = _import_service_win(monkeypatch, request)
    from rlm_tools_bsl.service import backup_path

    cfg = tmp_path / "service.json"
    cfg.write_text("{}", encoding="utf-8")
    backup = backup_path(cfg)
    backup.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("RLM_CONFIG_FILE", str(cfg))

    _service_win.uninstall(purge=True)

    assert not cfg.exists()
    assert not backup.exists()


def test_dead_watchdog_thread_stops_the_service(monkeypatch, tmp_path, request):
    """The thread reports SERVICE_RUNNING before it starts serving. If it dies -- an
    unusable service.json is enough -- the SCM would keep showing a running service with
    no server behind it, so the failure must be logged and the service stopped."""
    _install_stubs(monkeypatch)
    _service_win = _import_service_win(monkeypatch, request)

    stopped = []
    sys.modules["win32event"].SetEvent = lambda handle: stopped.append(handle)

    cfg = tmp_path / "service.json"
    cfg.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("RLM_CONFIG_FILE", str(cfg))

    service = object.__new__(_service_win.RlmWindowsService)
    service._stop_event = "stop-event"

    service._run_server()  # must not raise

    assert stopped == ["stop-event"], "the service has to be told to stop"
    log = (tmp_path / "logs" / "server.log").read_text(encoding="utf-8")
    assert "Fatal:" in log and "Service is stopping" in log


def test_registry_failure_is_raised_not_warned(monkeypatch, request):
    """A service registered without PYTHONPATH / RLM_CONFIG_FILE cannot start at all.
    Swallowing the error left `install` reporting success on exactly that state; it has
    to reach the caller, which rolls the registration back."""
    _install_stubs(monkeypatch)

    def _deny(*args, **kwargs):
        raise OSError("Access is denied.")

    sys.modules["winreg"].OpenKeyEx = _deny

    _service_win = _import_service_win(monkeypatch, request)

    with pytest.raises(RuntimeError):
        _service_win._set_service_environment("svc-X", r"C:\sp", r"C:\cfg")


def test_install_registers_before_writing_config(monkeypatch, request):
    """A failed SCM registration must not modify an existing service.json."""
    _install_stubs(monkeypatch)
    _service_win = _import_service_win(monkeypatch, request)
    win32service = sys.modules["win32service"]
    win32serviceutil = sys.modules["win32serviceutil"]
    win32service.SERVICE_AUTO_START = 2
    events = []

    monkeypatch.setattr(shutil, "which", lambda _name: r"C:\tool\rlm-tools-bsl.exe")
    monkeypatch.setattr(pathlib.Path, "glob", lambda self, pattern: iter(()))
    win32serviceutil.InstallService = lambda **kwargs: events.append("register")
    monkeypatch.setattr(_service_win, "save_config", lambda *a, **kw: events.append("save"))
    monkeypatch.setattr(_service_win, "_set_service_environment", lambda *a, **kw: events.append("registry"))

    _service_win.install("0.0.0.0", 3000, None)

    assert events == ["register", "save", "registry"]


def test_install_rolls_registration_back_when_registry_write_fails(monkeypatch, request):
    _install_stubs(monkeypatch)
    _service_win = _import_service_win(monkeypatch, request)
    win32service = sys.modules["win32service"]
    win32serviceutil = sys.modules["win32serviceutil"]
    win32service.SERVICE_AUTO_START = 2
    events = []

    monkeypatch.setattr(shutil, "which", lambda _name: r"C:\tool\rlm-tools-bsl.exe")
    monkeypatch.setattr(pathlib.Path, "glob", lambda self, pattern: iter(()))
    win32serviceutil.InstallService = lambda **kwargs: events.append("register")
    win32serviceutil.RemoveService = lambda _name: events.append("remove")
    monkeypatch.setattr(_service_win, "save_config", lambda *a, **kw: events.append("save"))

    def _fail_registry(*args, **kwargs):
        events.append("registry")
        raise RuntimeError("registry denied")

    monkeypatch.setattr(_service_win, "_set_service_environment", _fail_registry)

    with pytest.raises(SystemExit) as exc:
        _service_win.install("0.0.0.0", 3000, None)

    assert exc.value.code == 1
    assert events == ["register", "save", "registry", "remove"]


def test_install_restores_previous_config_when_registry_write_fails(monkeypatch, tmp_path, request):
    _install_stubs(monkeypatch)
    _service_win = _import_service_win(monkeypatch, request)
    win32service = sys.modules["win32service"]
    win32serviceutil = sys.modules["win32serviceutil"]
    win32service.SERVICE_AUTO_START = 2
    cfg = tmp_path / "service.json"
    previous = b'{"host":"10.0.0.7","port":8123,"env_file":null}\n'
    cfg.write_bytes(previous)
    monkeypatch.setenv("RLM_CONFIG_FILE", str(cfg))

    monkeypatch.setattr(shutil, "which", lambda _name: r"C:\tool\rlm-tools-bsl.exe")
    monkeypatch.setattr(pathlib.Path, "glob", lambda self, pattern: iter(()))
    win32serviceutil.InstallService = lambda **kwargs: None
    win32serviceutil.RemoveService = lambda _name: None
    monkeypatch.setattr(
        _service_win,
        "_set_service_environment",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("registry denied")),
    )

    with pytest.raises(SystemExit) as exc:
        _service_win.install("0.0.0.0", 3000, None)

    assert exc.value.code == 1
    assert cfg.read_bytes() == previous
    assert not any(p.name.startswith("service.json.new.") for p in tmp_path.iterdir())


def test_watchdog_returning_normally_also_stops_the_service(monkeypatch, tmp_path, request):
    """The thread reports SERVICE_RUNNING and then serves. It also RETURNS normally when
    the child exits cleanly or the restart budget runs out -- without stopping the
    service, the SCM would keep showing a running service with no server behind it."""
    _install_stubs(monkeypatch)
    _service_win = _import_service_win(monkeypatch, request)

    stopped = []
    sys.modules["win32event"].SetEvent = lambda handle: stopped.append(handle)
    monkeypatch.setenv("RLM_CONFIG_FILE", str(tmp_path / "service.json"))

    service = object.__new__(_service_win.RlmWindowsService)
    service._stop_event = "stop-event"
    monkeypatch.setattr(_service_win.RlmWindowsService, "_serve", lambda self: None)

    service._run_server()

    assert stopped == ["stop-event"]
    log = (tmp_path / "logs" / "server.log").read_text(encoding="utf-8")
    assert "Watchdog finished" in log
