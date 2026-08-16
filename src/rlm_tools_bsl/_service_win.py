"""Windows Service implementation for rlm-tools-bsl using pywin32.

Architecture:
  pythonservice.exe (system Python) imports this module via PYTHONPATH set
  in the service registry.  SvcDoRun spawns rlm-tools-bsl.exe (uv tool env)
  as a child process instead of importing the server directly, so the HTTP
  server always runs in its own isolated Python environment.
"""

import datetime
import os
import pathlib
import stat
import subprocess
import threading
import time
import urllib.error
import urllib.request

import win32event
import win32service
import win32serviceutil

from rlm_tools_bsl._git_process import CAPTURE_FILE_GLOB, GIT_CAPTURE_DIR_ENV, SERVICE_CAPTURE_DIRNAME
from rlm_tools_bsl._service_env import build_service_env_vars
from rlm_tools_bsl.service import _config_path, load_config, save_config

SERVICE_NAME = "rlm-tools-bsl"
SERVICE_DISPLAY = "RLM Tools BSL (MCP HTTP Server)"
_SERVICE_DESC_BASE = "RLM-инструменты для анализа 1C BSL-кода. Предназначены для экономии расхода токенов и контекста при анализе BSL-проектов"


def _get_service_desc() -> str:
    try:
        from importlib.metadata import version

        ver = version("rlm-tools-bsl")
    except Exception:
        ver = "?"
    return f"{_SERVICE_DESC_BASE} (v{ver})"


SERVICE_DESC = _get_service_desc()


class RlmWindowsService(win32serviceutil.ServiceFramework):
    _svc_name_ = SERVICE_NAME
    _svc_display_name_ = SERVICE_DISPLAY
    _svc_description_ = SERVICE_DESC

    # Результат подготовки capture-каталога (см. SvcDoRun). Значения по
    # умолчанию нужны, чтобы _run_server оставался вызываемым сам по себе.
    _git_capture_dir: pathlib.Path | None = None
    _git_capture_error: str | None = None
    _git_capture_swept: int = 0

    def SvcDoRun(self) -> None:
        self._stop_event = win32event.CreateEvent(None, 0, 0, None)
        self._proc: subprocess.Popen | None = None
        # Приватный capture-каталог для git-вызовов сервера. Best-effort: отказ
        # выключает ТОЛЬКО Git-ускорение, а не службу, поэтому watchdog-поток
        # стартует и SERVICE_RUNNING сообщается в любом случае.
        self._git_capture_dir: pathlib.Path | None = None
        self._git_capture_error: str | None = None
        self._git_capture_swept: int = 0
        try:
            self._git_capture_dir = _prepare_git_capture_dir()
        except BaseException as exc:  # noqa: BLE001 - старт службы не срывается ничем
            self._git_capture_error = type(exc).__name__
        else:
            # Уборка — только по ПРОВЕРЕННОМУ каталогу: подметать что-то, чему мы
            # не доверяем (не тот путь, ссылка, чужой DACL), нельзя. Отдельный
            # try обязателен: провал уборки НЕ меняет вердикт о готовности
            # каталога, иначе `_git_capture_error` оказался бы выставлен при
            # живом `_git_capture_dir` — состояние, которого остальной код не
            # ждёт (ошибка записана, но нигде не всплывёт).
            try:
                self._git_capture_swept = _sweep_git_capture_dir(self._git_capture_dir)
            except BaseException:  # noqa: BLE001 - уборка не критична вовсе
                self._git_capture_swept = 0
        self._thread = threading.Thread(target=self._run_server, daemon=True)
        self._thread.start()
        self.ReportServiceStatus(win32service.SERVICE_RUNNING)
        win32event.WaitForSingleObject(self._stop_event, win32event.INFINITE)

    def _run_server(self) -> None:
        cfg = load_config()
        exe = cfg.get("exe_path") or "rlm-tools-bsl"
        host = cfg["host"]
        port = str(cfg["port"])
        health_url = f"http://{host}:{port}/health"

        env = os.environ.copy()
        env_file = cfg.get("env_file")
        if env_file and pathlib.Path(env_file).exists():
            _load_env_file(env_file, env)
        # Force line-buffered stdout/stderr in the subprocess so log records
        # (which go to stderr via basicConfig and get redirected here) appear
        # in server.log immediately, not in 4-8 KB block-buffered chunks.
        env.setdefault("PYTHONUNBUFFERED", "1")
        # Force UTF-8 std streams in the subprocess. We redirect its stderr into
        # server.log below; without this, Windows encodes a redirected stderr
        # with the legacy ANSI code page (cp1251) → Cyrillic (and the rlm_execute
        # `code=<…>` field) become mojibake next to the UTF-8 RotatingFileHandler
        # lines. Belt-and-braces with the reconfigure() in server.main().
        env.setdefault("PYTHONUTF8", "1")
        env.setdefault("PYTHONIOENCODING", "utf-8")
        # The child skips its own log-retention purge (see server._setup_file_logging):
        # we purge here, before opening server.log for the child's stderr redirect, so the
        # child never truncates a file this service holds open.
        #
        # Обе переменные ниже — решения СЛУЖБЫ, а не настройки: вместе они и
        # образуют гейт capture-каталога (`_git_process._git_capture_dir()`
        # требует `RLM_UNDER_SERVICE=1` И точного совпадения маркера). Поэтому
        # выставляются регистронезависимо — см. `_set_service_env_var`.
        _set_service_env_var(env, "RLM_UNDER_SERVICE", "1")
        # Маркер capture-каталога — приватный канал служба→ребёнок. Любое
        # унаследованное значение (в т.ч. из `.env`, загруженного выше)
        # вычищается: подставленный извне путь обошёл бы и mkdir, и
        # DACL-проверку. Выставляем только то, что реально подготовили сами.
        _set_service_env_var(
            env,
            GIT_CAPTURE_DIR_ENV,
            str(self._git_capture_dir) if self._git_capture_dir is not None else None,
        )

        log_dir = _config_path().parent / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "server.log"

        # Time-based retention BEFORE the file is opened below (drop entries older than
        # RLM_LOG_RETENTION_DAYS, default 20). Best-effort — never block service start.
        try:
            from rlm_tools_bsl.log_retention import log_retention_days, purge_log_older_than

            purge_log_older_than(log_path, days=log_retention_days())
        except Exception:
            pass

        if self._git_capture_dir is None:
            # Одна обезличенная строка: имя класса исключения без пути и ACL-деталей.
            try:
                with open(log_path, "a", encoding="utf-8") as f:
                    _log_watchdog(
                        f,
                        "Git capture unavailable; Git acceleration disabled (%s)",
                        self._git_capture_error or "Unknown",
                    )
            except OSError:
                pass
        elif self._git_capture_swept:
            # Штатно тут убирать нечего, поэтому непустой счётчик — сигнал. Но
            # источников у него ДВА, и приписывать его только выключению ОС
            # нельзя: осиротевший git из деградировавшей ветки (без Job) тоже
            # мог пережить остановку службы и оставить своё имя в каталоге.
            try:
                with open(log_path, "a", encoding="utf-8") as f:
                    _log_watchdog(
                        f,
                        "Git capture: removed %d stale file(s) (unclean shutdown or an orphaned git process)",
                        self._git_capture_swept,
                    )
            except OSError:
                pass

        max_restarts = 5
        restart_count = 0

        while restart_count <= max_restarts:
            log_file = open(log_path, "a", encoding="utf-8", buffering=1)
            _log_watchdog(log_file, "Starting subprocess: %s", exe)
            self._proc = subprocess.Popen(
                [exe, "--transport", "streamable-http", "--host", host, "--port", port],
                env=env,
                stdout=log_file,
                stderr=subprocess.STDOUT,
            )

            # Grace period for startup
            time.sleep(5)

            # Health-check loop: check every 30s, poll stop_event every 1s
            health_failed = False
            first_health_ok = True
            while self._proc.poll() is None:
                for _ in range(30):
                    if win32event.WaitForSingleObject(self._stop_event, 1000) != win32event.WAIT_TIMEOUT:
                        log_file.close()
                        return  # SvcStop was called
                    if self._proc.poll() is not None:
                        break
                else:
                    if _check_health(health_url):
                        if first_health_ok:
                            _log_watchdog(log_file, "Health check OK (%s)", health_url)
                            first_health_ok = False
                    else:
                        _log_watchdog(log_file, "Health check failed (%s), terminating process", health_url)
                        self._proc.terminate()
                        try:
                            self._proc.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            self._proc.kill()
                        health_failed = True

                if health_failed:
                    break

            exit_code = self._proc.returncode
            log_file.close()

            if exit_code == 0 and not health_failed:
                break  # clean shutdown

            restart_count += 1
            if restart_count <= max_restarts:
                with open(log_path, "a", encoding="utf-8") as f:
                    _log_watchdog(
                        f,
                        "Process exited (code=%s health_fail=%s), restarting (%d/%d)...",
                        exit_code,
                        health_failed,
                        restart_count,
                        max_restarts,
                    )
                time.sleep(5)

    def SvcStop(self) -> None:
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        proc = self._proc
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        win32event.SetEvent(self._stop_event)


# ---------------------------------------------------------------------------
# Приватный capture-каталог для git-вызовов сервера (v1.33.1)
# ---------------------------------------------------------------------------
# Все pywin32-модули, нужные только для подготовки/проверки DACL, импортируются
# ЛОКАЛЬНО внутри этих Windows-only helper-ов. Top-level import seam модуля
# намеренно не расширяется: существующий Linux-тест импортирует _service_win с
# минимальными заглушками win32event/win32service/win32serviceutil и не должен
# начать требовать service-extra.


def _set_service_env_var(env: dict, name: str, value: str | None) -> None:
    """Выставить (или снять) переменную службы, вычистив ЛЮБОЙ регистровый вариант.

    Имена переменных окружения на Windows регистронезависимы, а собираемый здесь
    `env` — обычный регистрозависимый dict. `os.environ.copy()` даёт ключи уже в
    верхнем регистре, но `_load_env_file` кладёт их из `.env` **дословно**,
    поэтому `env.pop("_RLM_GIT_CAPTURE_DIR")` не убирает `_rlm_git_capture_dir`
    — а ребёнок получит его как раз под каноническим именем. Для приватного
    маркера capture-каталога это означало бы обход и mkdir, и DACL-проверки:
    при неудачной подготовке сервер принял бы непроверенный каталог и снова
    включил Git-ускорение.

    ``value=None`` — переменная снимается целиком (ни один вариант не уезжает).
    """
    upper = name.upper()
    for key in [k for k in env if k.upper() == upper]:
        del env[key]
    if value is not None:
        env[name] = value


def _current_user_sid():
    """SID учётной записи, под которой реально работает служба."""
    import win32api
    import win32security

    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32security.TOKEN_QUERY)
    try:
        return win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    finally:
        token.Close()


def _git_capture_allowed_sids() -> list:
    """Единственный источник истины для «кому можно»: служба, SYSTEM, Administrators.

    Тем же списком и ставится DACL, и проверяется — иначе применение и проверка
    могли бы разъехаться.
    """
    import win32security

    candidates = [
        _current_user_sid(),
        win32security.CreateWellKnownSid(win32security.WinLocalSystemSid),
        win32security.CreateWellKnownSid(win32security.WinBuiltinAdministratorsSid),
    ]
    out: list = []
    seen: set[str] = set()
    for sid in candidates:
        key = win32security.ConvertSidToStringSid(sid)
        if key not in seen:
            seen.add(key)
            out.append(sid)
    return out


def _apply_protected_dacl(path: pathlib.Path) -> None:
    """Protected DACL: наследование от родителя отрезается, ACE — только свои."""
    import ntsecuritycon
    import win32security

    dacl = win32security.ACL()
    inherit = win32security.OBJECT_INHERIT_ACE | win32security.CONTAINER_INHERIT_ACE
    for sid in _git_capture_allowed_sids():
        dacl.AddAccessAllowedAceEx(win32security.ACL_REVISION_DS, inherit, ntsecuritycon.FILE_ALL_ACCESS, sid)
    win32security.SetNamedSecurityInfo(
        str(path),
        win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
        None,
        None,
        dacl,
        None,
    )


def _verify_protected_dacl(path: pathlib.Path) -> None:
    """Перечитать DACL и убедиться, что он именно такой, каким его ставили.

    Проверка отдельным чтением, а не «мы же только что записали»: только так
    видно, что DACL действительно protected и что в нём не осталось
    унаследованных или посторонних ACE.
    """
    import win32security

    sd = win32security.GetNamedSecurityInfo(
        str(path), win32security.SE_FILE_OBJECT, win32security.DACL_SECURITY_INFORMATION
    )
    control, _revision = sd.GetSecurityDescriptorControl()
    if not control & win32security.SE_DACL_PROTECTED:
        raise OSError("git capture directory DACL is not protected")
    dacl = sd.GetSecurityDescriptorDacl()
    if dacl is None:
        raise OSError("git capture directory has no DACL")
    ace_count = dacl.GetAceCount()
    if ace_count == 0:
        raise OSError("git capture directory DACL is empty")
    allowed = {win32security.ConvertSidToStringSid(s) for s in _git_capture_allowed_sids()}
    for i in range(ace_count):
        (_ace_type, ace_flags), _mask, sid = dacl.GetAce(i)
        if ace_flags & win32security.INHERITED_ACE:
            raise OSError("git capture directory DACL carries an inherited ACE")
        if win32security.ConvertSidToStringSid(sid) not in allowed:
            raise OSError("git capture directory DACL grants a foreign principal")


def _is_reparse_point(path: pathlib.Path) -> bool:
    """Junction/symlink увёл бы capture за пределы config-root."""
    attrs = getattr(os.lstat(path), "st_file_attributes", 0)
    return bool(attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _prepare_git_capture_dir() -> pathlib.Path:
    """Создать и проверить `<parent RLM_CONFIG_FILE>/git-capture`.

    Возвращает готовый каталог либо поднимает исключение — половинчатого
    результата не бывает: маркер `_RLM_GIT_CAPTURE_DIR` получает только каталог,
    прошедший ВСЕ проверки, а `_git_process._git_capture_dir()` под службой
    принимает исключительно этот путь. Поэтому частично созданный или не
    прошедший DACL-check каталог использован быть не может в принципе.
    """
    config_file = _config_path()
    if not config_file.is_absolute():
        raise OSError("service config path is not absolute")
    target = config_file.parent / SERVICE_CAPTURE_DIRNAME
    target.mkdir(parents=True, exist_ok=True)
    if not target.is_dir():
        raise OSError("git capture path is not a directory")
    if _is_reparse_point(target):
        raise OSError("git capture path is a reparse point")
    _apply_protected_dacl(target)
    _verify_protected_dacl(target)
    return target


def _sweep_git_capture_dir(path: pathlib.Path) -> int:
    """Убрать capture-файлы, пережившие АВАРИЙНОЕ выключение ОС. Возвращает число убранных.

    Штатно уборка здесь не нужна и не работает: файлы открыты с
    `FILE_FLAG_DELETE_ON_CLOSE`, поэтому ядро удаляет их при закрытии последнего
    дескриптора — в том числе когда процессы просто убивают (остановка службы,
    kill дерева по timeout). Остаются два случая: аварийное выключение самой ОС
    (питание, BSOD, reset — дескрипторы не закрылись вовсе) и осиротевший git из
    деградировавшей ветки без Job, переживший остановку службы.

    Удалить имя у файла, который такой потомок ещё держит, безопасно: Windows
    снимает имя сразу, а сам держатель продолжает читать и писать через свой
    дескриптор, и данные освобождаются при его выходе. Своих читателей у нас
    тут быть не может — `run_git` читает capture только через УЖЕ открытый
    файловый объект и никогда не переоткрывает файл по имени.

    Почему именно здесь. План (§2.2) отверг stale-cleanup **на критическом
    пути** — сканер внутри каждого `run_git` не может иметь честного лимита по
    времени вокруг блокирующих обращений к ФС. Разовый проход при старте службы
    — другое дело: он вне критического пути, случается раз за запуск, каталог
    приватный и маленький, а дочерний сервер в этот момент ещё не поднят, так
    что занятых НАМИ файлов там быть не может.

    Отдельная причина, почему без этого нельзя: системный temp (куда пишет
    ручной запуск и CLI) рано или поздно чистит сама Windows, а приватный
    `<config>/git-capture` не чистит НИЧТО, кроме нас.

    Best-effort и поштучно: неудача на одном файле не мешает остальным и никогда
    не срывает старт службы. Трогаются только файлы нашего шаблона — посторонние
    остаются на месте.
    """
    removed = 0
    try:
        stale = sorted(path.glob(CAPTURE_FILE_GLOB))
    except OSError:
        return 0
    for entry in stale:
        try:
            if entry.is_file():
                entry.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def _load_env_file(path: str, env: dict) -> None:
    """Parse .env file and merge variables into env (no override of existing vars)."""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k = k.strip()
                v = v.strip()
                # Quoted value: extract content between quotes, ignore trailing comment
                for q in ('"', "'"):
                    if v.startswith(q):
                        end = v.find(q, 1)
                        if end > 0:
                            v = v[1:end]
                            break
                else:
                    # Unquoted: strip inline comment only when preceded by space (" #")
                    idx = v.find(" #")
                    if idx >= 0:
                        v = v[:idx].rstrip()
                env.setdefault(k, v)
    except OSError:
        pass


def _check_health(url: str) -> bool:
    """Check if MCP server process is alive via GET /health."""
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return resp.status == 200
    except urllib.error.HTTPError:
        return False
    except Exception:
        return False


def _log_watchdog(f, msg: str, *args) -> None:
    """Write a timestamped watchdog message to the log file."""
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    text = msg % args if args else msg
    f.write(f"[watchdog {ts}] {text}\n")
    f.flush()


def _set_service_environment(service_name: str, site_packages: str, config_file: str) -> None:
    """Set Environment REG_MULTI_SZ value on the service registry key.

    Windows SCM reads the 'Environment' value (REG_MULTI_SZ) directly under
    HKLM\\SYSTEM\\CurrentControlSet\\Services\\<name> and injects those
    variables into the service process environment at start.

    We set:
      PYTHONPATH  — so pythonservice.exe (system Python) can import rlm_tools_bsl
                    AND find pywin32's servicemanager.pyd / win32 helpers
                    (site processing does not run for pythonservice.exe in a
                    uv tool env, so pywin32.pth is not honored — we replicate
                    its effect via build_service_pythonpath)
      RLM_CONFIG_FILE — so load_config() finds the user's service.json
                        (LocalSystem has a different home dir)
    """
    import winreg

    key_path = rf"SYSTEM\CurrentControlSet\Services\{service_name}"
    env_vars = build_service_env_vars(site_packages, config_file)
    try:
        with winreg.OpenKeyEx(winreg.HKEY_LOCAL_MACHINE, key_path, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, "Environment", 0, winreg.REG_MULTI_SZ, env_vars)
        for ev in env_vars:
            print(f"Service env set: {ev}")
    except Exception as exc:
        print(f"Warning: could not set Environment in registry: {exc}")


def install(host: str, port: int, env_file: str | None) -> None:
    import shutil
    import sys

    # Locate rlm-tools-bsl.exe in the current (uv tool) Python environment.
    # We try two strategies: PATH lookup and sibling of sys.executable.
    exe_path: str | None = shutil.which("rlm-tools-bsl")
    if not exe_path:
        candidate = pathlib.Path(sys.executable).parent / "rlm-tools-bsl.exe"
        if candidate.exists():
            exe_path = str(candidate)

    if exe_path:
        print(f"Found rlm-tools-bsl.exe: {exe_path}")
    else:
        print("Warning: rlm-tools-bsl.exe not found in PATH; service may fail to start.")

    # site-packages of the current (uv tool) env — needed for PYTHONPATH in registry
    # _service_win.py lives at  <site-packages>/rlm_tools_bsl/_service_win.py
    site_packages = str(pathlib.Path(__file__).parent.parent)

    # pythonservice.exe needs several DLLs next to it that aren't on the
    # DLL search path in an isolated uv tool environment:
    #   - pywintypes*.dll, pythoncom*.dll  (pywin32, in pywin32_system32/)
    #   - python3.dll, python3XX.dll       (Python runtime, in sys.prefix or exe dir)
    # site_packages = .../Lib/site-packages, pythonservice.exe is at env root (2 levels up)
    svc_dir = pathlib.Path(site_packages).parent.parent
    dlls_to_copy: list[pathlib.Path] = []

    # pywin32 DLLs
    pywin32_sys32 = pathlib.Path(site_packages) / "pywin32_system32"
    if pywin32_sys32.is_dir():
        dlls_to_copy.extend(pywin32_sys32.glob("*.dll"))

    # Python runtime DLLs (python3.dll + python3XX.dll)
    # In venvs/uv tool envs, DLLs are in base_prefix, not prefix
    for py_dir in dict.fromkeys(
        [
            pathlib.Path(sys.base_prefix),
            pathlib.Path(sys.prefix),
            pathlib.Path(sys.executable).resolve().parent,
        ]
    ):
        dlls_to_copy.extend(py_dir.glob("python3*.dll"))

    for dll in dlls_to_copy:
        dest = svc_dir / dll.name
        if not dest.exists():
            shutil.copy2(dll, dest)
            print(f"Copied {dll.name} -> {svc_dir}")

    save_config(host, port, env_file, exe_path=exe_path)
    try:
        win32serviceutil.InstallService(
            pythonClassString="rlm_tools_bsl._service_win.RlmWindowsService",
            serviceName=SERVICE_NAME,
            displayName=SERVICE_DISPLAY,
            description=SERVICE_DESC,
            startType=win32service.SERVICE_AUTO_START,
        )
        # Allow pythonservice.exe (system Python) to find rlm_tools_bsl at runtime
        # and locate the config file (LocalSystem has a different home dir)
        _set_service_environment(SERVICE_NAME, site_packages, str(_config_path()))
        print(f"Service '{SERVICE_NAME}' installed.")
        print("Start with: rlm-tools-bsl service start")
    except Exception as exc:
        print(f"Install error: {exc}")
        print("Make sure you are running as Administrator.")
        raise SystemExit(1)


def uninstall() -> None:
    try:
        win32serviceutil.StopService(SERVICE_NAME)
    except Exception:
        pass
    try:
        win32serviceutil.RemoveService(SERVICE_NAME)
        _config_path().unlink(missing_ok=True)
        print(f"Service '{SERVICE_NAME}' removed.")
    except Exception as exc:
        print(f"Error: {exc}")
        raise SystemExit(1)


def start() -> None:
    win32serviceutil.StartService(SERVICE_NAME)
    print("Service started.")


def stop() -> None:
    win32serviceutil.StopService(SERVICE_NAME)
    print("Service stopped.")


def status() -> None:
    try:
        s = win32serviceutil.QueryServiceStatus(SERVICE_NAME)
        states = {
            win32service.SERVICE_RUNNING: "Running",
            win32service.SERVICE_STOPPED: "Stopped",
            win32service.SERVICE_START_PENDING: "Start Pending",
            win32service.SERVICE_STOP_PENDING: "Stop Pending",
        }
        print(f"Status: {states.get(s[1], str(s[1]))}")
    except Exception:
        print("Service not installed.")
