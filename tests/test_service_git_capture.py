"""Приватный capture-каталог службы: подготовка, маркер и честная деградация.

Инварианты, которые здесь застолблены:

* маркер `_RLM_GIT_CAPTURE_DIR` — приватный канал служба→ребёнок; ни `.env`, ни
  унаследованное окружение подменить его не могут;
* отказ подготовки НЕ срывает старт службы: watchdog-поток запускается,
  `SERVICE_RUNNING` сообщается, а в лог уходит одна обезличенная строка;
* сам каталог живёт внутри config-root, не является reparse point и закрыт
  protected DACL.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import types

import pytest

from rlm_tools_bsl._git_process import (
    CAPTURE_FILE_PREFIX,
    CAPTURE_FILE_SUFFIX,
    GIT_CAPTURE_DIR_ENV,
    SERVICE_CAPTURE_DIRNAME,
)

WINDOWS = sys.platform == "win32"
windows_only = pytest.mark.skipif(not WINDOWS, reason="подготовка каталога — Windows-only")


def _install_win32_stubs(monkeypatch) -> None:
    """Минимальные заглушки — тот же набор, что и в test_service_win_integration."""
    win32service = types.ModuleType("win32service")
    win32service.SERVICE_RUNNING = 4
    win32service.SERVICE_STOP_PENDING = 3
    win32event = types.ModuleType("win32event")
    win32event.INFINITE = -1
    win32event.WAIT_TIMEOUT = 258
    win32event.CreateEvent = lambda *a: object()
    win32event.WaitForSingleObject = lambda *a: 0
    win32event.SetEvent = lambda *a: None
    win32serviceutil = types.ModuleType("win32serviceutil")
    win32serviceutil.ServiceFramework = object
    monkeypatch.setitem(sys.modules, "win32service", win32service)
    monkeypatch.setitem(sys.modules, "win32event", win32event)
    monkeypatch.setitem(sys.modules, "win32serviceutil", win32serviceutil)


@pytest.fixture
def service_mod(monkeypatch, request):
    """Свежий импорт `_service_win` (на POSIX — под заглушками win32)."""
    if not WINDOWS:
        _install_win32_stubs(monkeypatch)

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


class _FakeProc:
    """Ребёнок, который «сразу штатно завершился» — restart-цикл делает один проход."""

    returncode = 0

    def poll(self):
        return 0


def _run_server_once(mod, monkeypatch, tmp_path, *, capture_dir, env_file=None, no_env=False, swept=0) -> dict:
    """Прогнать реальный `_run_server` один круг и вернуть env, ушедший ребёнку."""
    captured: dict = {}

    def fake_popen(argv, env=None, stdout=None, stderr=None):
        captured["argv"] = argv
        captured["env"] = dict(env or {})
        return _FakeProc()

    # Подменяем ССЫЛКИ модуля, а не сами stdlib-модули: правка `subprocess.Popen`
    # in-place действует глобально и ломает любой другой запуск процесса в том же
    # тесте (в т.ч. сквозной пробник ниже).
    monkeypatch.setattr(
        mod,
        "subprocess",
        types.SimpleNamespace(
            Popen=fake_popen,
            STDOUT=subprocess.STDOUT,
            TimeoutExpired=subprocess.TimeoutExpired,
        ),
    )
    monkeypatch.setattr(mod, "time", types.SimpleNamespace(sleep=lambda *_a: None))
    monkeypatch.setattr(mod, "_config_path", lambda: tmp_path / "service.json")
    monkeypatch.setattr(
        mod,
        "load_config",
        lambda: {
            "host": "127.0.0.1",
            "port": 9000,
            "env_file": env_file,
            "no_env": no_env,
            "exe_path": "rlm-tools-bsl",
        },
    )

    svc = mod.RlmWindowsService.__new__(mod.RlmWindowsService)
    svc._git_capture_dir = capture_dir
    svc._git_capture_error = None if capture_dir is not None else "OSError"
    svc._git_capture_swept = swept
    svc._run_server()

    captured["log"] = (tmp_path / "logs" / "server.log").read_text(encoding="utf-8")
    return captured


def _neuter_service_loop(mod, monkeypatch) -> None:
    """Обезвредить всё, что SvcDoRun делает помимо подготовки capture-каталога.

    `_run_server` подменяется на no-op, а `win32event`/`win32service` — на
    фальшивые модули: ждать реальное событие в тесте нельзя.
    """
    fake_event = types.ModuleType("win32event")
    fake_event.INFINITE = -1
    fake_event.CreateEvent = lambda *a: "event"
    fake_event.WaitForSingleObject = lambda *a: 0
    fake_service = types.ModuleType("win32service")
    fake_service.SERVICE_RUNNING = 4
    monkeypatch.setattr(mod, "win32event", fake_event)
    monkeypatch.setattr(mod, "win32service", fake_service)
    monkeypatch.setattr(mod.RlmWindowsService, "_run_server", lambda self: None)


def _run_svcdorun(mod):
    """Прогнать реальный SvcDoRun и вернуть экземпляр службы."""
    svc = mod.RlmWindowsService.__new__(mod.RlmWindowsService)
    svc.ReportServiceStatus = lambda _status: None
    svc.SvcDoRun()
    svc._thread.join(timeout=10)
    return svc


def _variants(env: dict, name: str) -> dict:
    """Все регистровые варианты имени — ребёнок на Windows видит их как одно имя."""
    upper = name.upper()
    return {k: v for k, v in env.items() if k.upper() == upper}


class TestMarkerHandoff:
    def test_marker_is_set_only_from_prepared_dir(self, service_mod, monkeypatch, tmp_path):
        prepared = tmp_path / SERVICE_CAPTURE_DIRNAME
        prepared.mkdir()
        result = _run_server_once(service_mod, monkeypatch, tmp_path, capture_dir=prepared)
        assert result["env"][GIT_CAPTURE_DIR_ENV] == str(prepared)

    def test_env_file_and_inherited_value_cannot_substitute_marker(self, service_mod, monkeypatch, tmp_path):
        """`.env` загружается ДО scrub — и всё равно не может подсунуть свой путь."""
        hostile = tmp_path / "чужой-каталог"
        hostile.mkdir()
        env_file = tmp_path / "hostile.env"
        env_file.write_text(f"{GIT_CAPTURE_DIR_ENV}={hostile}\n", encoding="utf-8")
        monkeypatch.setenv(GIT_CAPTURE_DIR_ENV, str(hostile))

        prepared = tmp_path / SERVICE_CAPTURE_DIRNAME
        prepared.mkdir()
        result = _run_server_once(service_mod, monkeypatch, tmp_path, capture_dir=prepared, env_file=str(env_file))
        assert result["env"][GIT_CAPTURE_DIR_ENV] == str(prepared)
        assert str(hostile) not in result["env"][GIT_CAPTURE_DIR_ENV]

    def test_failed_preparation_removes_marker_and_logs_anonymously(self, service_mod, monkeypatch, tmp_path):
        hostile = tmp_path / "чужой-каталог"
        hostile.mkdir()
        monkeypatch.setenv(GIT_CAPTURE_DIR_ENV, str(hostile))

        result = _run_server_once(service_mod, monkeypatch, tmp_path, capture_dir=None)

        assert GIT_CAPTURE_DIR_ENV not in result["env"], "унаследованное значение обязано быть вычищено"
        assert result["argv"][0] == "rlm-tools-bsl", "сервер всё равно запускается"
        assert "Git capture unavailable; Git acceleration disabled (OSError)" in result["log"]
        assert str(hostile) not in result["log"], "в лог не попадают пути"


class TestStaleSweep:
    """Уборка остатков при старте службы.

    Штатно убирать нечего: `FILE_FLAG_DELETE_ON_CLOSE` снимает файлы даже при
    убийстве процессов. Непустой результат означает одно из двух — аварийное
    выключение ОС (дескрипторы не закрылись) либо осиротевший git из
    деградировавшей ветки без Job, переживший остановку службы. Убирать
    приходится нам: приватный каталог службы не чистит больше никто.
    """

    @staticmethod
    def _make_stale(d: pathlib.Path) -> list[pathlib.Path]:
        names = [
            f"{CAPTURE_FILE_PREFIX}stdout-abc123{CAPTURE_FILE_SUFFIX}",
            f"{CAPTURE_FILE_PREFIX}stderr-xyz789{CAPTURE_FILE_SUFFIX}",
        ]
        out = []
        for n in names:
            p = d / n
            p.write_bytes(b"leftover")
            out.append(p)
        return out

    def test_sweeps_only_our_files(self, service_mod, tmp_path):
        d = tmp_path / SERVICE_CAPTURE_DIRNAME
        d.mkdir()
        stale = self._make_stale(d)
        # Посторонние: чужое имя, чужое расширение и подкаталог — не трогаем.
        foreign = d / "чужой-файл.txt"
        foreign.write_bytes(b"x")
        wrong_suffix = d / f"{CAPTURE_FILE_PREFIX}stdout-zzz.log"
        wrong_suffix.write_bytes(b"x")
        subdir = d / f"{CAPTURE_FILE_PREFIX}stdout-dir{CAPTURE_FILE_SUFFIX}"
        subdir.mkdir()

        removed = service_mod._sweep_git_capture_dir(d)

        assert removed == 2
        assert not any(p.exists() for p in stale)
        assert foreign.exists(), "посторонний файл трогать нельзя"
        assert wrong_suffix.exists(), "наш префикс, но чужое расширение — не наш файл"
        assert subdir.is_dir(), "каталог с подходящим именем — не файл, не удаляем"

    def test_empty_dir_is_a_no_op(self, service_mod, tmp_path):
        d = tmp_path / SERVICE_CAPTURE_DIRNAME
        d.mkdir()
        assert service_mod._sweep_git_capture_dir(d) == 0

    def test_missing_dir_does_not_raise(self, service_mod, tmp_path):
        assert service_mod._sweep_git_capture_dir(tmp_path / "нет-такого") == 0

    def test_unremovable_file_does_not_stop_the_rest(self, service_mod, tmp_path, monkeypatch):
        d = tmp_path / SERVICE_CAPTURE_DIRNAME
        d.mkdir()
        stale = self._make_stale(d)
        real_unlink = pathlib.Path.unlink
        first = sorted(stale)[0]

        def flaky(self, *a, **kw):
            if self == first:
                raise OSError("forced: file is locked")
            return real_unlink(self, *a, **kw)

        monkeypatch.setattr(pathlib.Path, "unlink", flaky)
        removed = service_mod._sweep_git_capture_dir(d)

        assert removed == 1, "один файл не поддался, остальные всё равно убраны"
        assert first.exists()

    @windows_only
    def test_preparation_sweeps_leftovers(self, service_mod, monkeypatch, tmp_path):
        """Сквозной путь: подготовка каталога + уборка, как в SvcDoRun."""
        config = tmp_path / "cfg" / "service.json"
        config.parent.mkdir()
        monkeypatch.setattr(service_mod, "_config_path", lambda: config)

        target = service_mod._prepare_git_capture_dir()
        stale = self._make_stale(target)
        assert all(p.exists() for p in stale)

        assert service_mod._sweep_git_capture_dir(target) == 2
        assert not any(p.exists() for p in stale)
        assert service_mod._sweep_git_capture_dir(target) == 0, "повторный проход идемпотентен"

    def test_watchdog_logs_only_when_something_was_swept(self, service_mod, monkeypatch, tmp_path):
        prepared = tmp_path / SERVICE_CAPTURE_DIRNAME
        prepared.mkdir()

        quiet = _run_server_once(service_mod, monkeypatch, tmp_path, capture_dir=prepared, swept=0)
        assert "stale file" not in quiet["log"], "штатный старт молчит"

        noisy = _run_server_once(service_mod, monkeypatch, tmp_path, capture_dir=prepared, swept=3)
        assert "Git capture: removed 3 stale file(s)" in noisy["log"]
        # Причина названа как ДВЕ возможности: приписывать счётчик одному лишь
        # выключению ОС нельзя — осиротевший git даёт тот же след.
        assert "unclean shutdown or an orphaned git process" in noisy["log"]
        assert str(prepared) not in noisy["log"], "путь в лог не уезжает"

    def test_svcdorun_sweeps_the_prepared_dir_and_keeps_the_count(self, service_mod, monkeypatch):
        """Проводка в SvcDoRun: без этого теста вызов уборки можно удалить незаметно."""
        calls: list[str] = []
        prepared = pathlib.Path("C:/prepared") if WINDOWS else pathlib.Path("/prepared")

        def fake_prepare():
            calls.append("prepare")
            return prepared

        def fake_sweep(path):
            calls.append(f"sweep:{path}")
            return 7

        monkeypatch.setattr(service_mod, "_prepare_git_capture_dir", fake_prepare)
        monkeypatch.setattr(service_mod, "_sweep_git_capture_dir", fake_sweep)
        _neuter_service_loop(service_mod, monkeypatch)

        svc = _run_svcdorun(service_mod)

        assert calls == ["prepare", f"sweep:{prepared}"], "уборка идёт ПОСЛЕ подготовки, по её результату"
        assert svc._git_capture_dir == prepared
        assert svc._git_capture_swept == 7

    def test_failed_preparation_never_sweeps(self, service_mod, monkeypatch):
        """По непроверенному каталогу мести нельзя — там может быть что угодно."""
        calls: list[str] = []
        monkeypatch.setattr(
            service_mod,
            "_prepare_git_capture_dir",
            lambda: (_ for _ in ()).throw(OSError("forced DACL failure")),
        )
        monkeypatch.setattr(service_mod, "_sweep_git_capture_dir", lambda p: calls.append("sweep") or 99)
        _neuter_service_loop(service_mod, monkeypatch)

        svc = _run_svcdorun(service_mod)

        assert calls == [], "уборка не имеет права запускаться после провала подготовки"
        assert svc._git_capture_dir is None
        assert svc._git_capture_swept == 0
        assert svc._git_capture_error == "OSError"

    def test_sweep_failure_does_not_invalidate_the_prepared_dir(self, service_mod, monkeypatch):
        """Провал уборки НЕ меняет вердикт о готовности каталога."""
        prepared = pathlib.Path("C:/prepared") if WINDOWS else pathlib.Path("/prepared")
        monkeypatch.setattr(service_mod, "_prepare_git_capture_dir", lambda: prepared)
        monkeypatch.setattr(
            service_mod,
            "_sweep_git_capture_dir",
            lambda p: (_ for _ in ()).throw(RuntimeError("forced non-OSError")),
        )
        _neuter_service_loop(service_mod, monkeypatch)

        svc = _run_svcdorun(service_mod)

        assert svc._git_capture_dir == prepared, "каталог проверен — уборка на это не влияет"
        assert svc._git_capture_swept == 0
        assert svc._git_capture_error is None, "ошибка уборки не маскируется под отказ подготовки"


class TestMarkerCaseInsensitiveScrub:
    """Имена переменных окружения на Windows регистронезависимы, а собираемый env — нет.

    `.env` кладёт ключи ДОСЛОВНО, поэтому `env.pop("_RLM_GIT_CAPTURE_DIR")`
    оставлял бы `_rlm_git_capture_dir`, и дочерний процесс получал бы его уже
    под каноническим именем — мимо mkdir и DACL-проверки.
    """

    CASES = ["_rlm_git_capture_dir", "_Rlm_Git_Capture_Dir", "_RLM_GIT_capture_DIR"]

    @pytest.mark.parametrize("key", CASES)
    def test_lowercase_marker_from_env_file_cannot_survive_failed_preparation(
        self, service_mod, monkeypatch, tmp_path, key
    ):
        # Самый опасный вектор: подготовка ПРОВАЛИЛАСЬ, а подсунутый путь
        # указывает ровно на ожидаемый `<config>/git-capture` — то есть прошёл
        # бы проверку маркера в `_git_capture_dir()` без единой проверки прав.
        looks_legit = tmp_path / SERVICE_CAPTURE_DIRNAME
        looks_legit.mkdir()
        env_file = tmp_path / "hostile.env"
        env_file.write_text(f"{key}={looks_legit}\n", encoding="utf-8")

        result = _run_server_once(service_mod, monkeypatch, tmp_path, capture_dir=None, env_file=str(env_file))

        assert _variants(result["env"], GIT_CAPTURE_DIR_ENV) == {}, (
            "ни один регистровый вариант маркера не имеет права уехать ребёнку"
        )

    @pytest.mark.parametrize("key", CASES)
    def test_lowercase_marker_cannot_shadow_the_prepared_one(self, service_mod, monkeypatch, tmp_path, key):
        hostile = tmp_path / "чужой-каталог"
        hostile.mkdir()
        prepared = tmp_path / SERVICE_CAPTURE_DIRNAME
        prepared.mkdir()
        env_file = tmp_path / "hostile.env"
        env_file.write_text(f"{key}={hostile}\n", encoding="utf-8")

        result = _run_server_once(service_mod, monkeypatch, tmp_path, capture_dir=prepared, env_file=str(env_file))

        assert _variants(result["env"], GIT_CAPTURE_DIR_ENV) == {GIT_CAPTURE_DIR_ENV: str(prepared)}

    @pytest.mark.parametrize("key", ["rlm_under_service", "Rlm_Under_Service"])
    def test_lowercase_under_service_flag_cannot_disable_the_gate(self, service_mod, monkeypatch, tmp_path, key):
        """Второй заслон того же гейта: при `RLM_UNDER_SERVICE != 1` маркер не проверяется вовсе."""
        env_file = tmp_path / "hostile.env"
        env_file.write_text(f"{key}=0\n", encoding="utf-8")
        prepared = tmp_path / SERVICE_CAPTURE_DIRNAME
        prepared.mkdir()

        result = _run_server_once(service_mod, monkeypatch, tmp_path, capture_dir=prepared, env_file=str(env_file))

        assert _variants(result["env"], "RLM_UNDER_SERVICE") == {"RLM_UNDER_SERVICE": "1"}

    def test_no_env_configuration_is_forwarded_to_the_server_child(self, service_mod, monkeypatch, tmp_path):
        result = _run_server_once(service_mod, monkeypatch, tmp_path, capture_dir=None, env_file=None, no_env=True)

        assert _variants(result["env"], "_RLM_SERVICE_NO_ENV") == {"_RLM_SERVICE_NO_ENV": "1"}

    def test_legacy_null_env_does_not_disable_fallbacks(self, service_mod, monkeypatch, tmp_path):
        result = _run_server_once(service_mod, monkeypatch, tmp_path, capture_dir=None, env_file=None, no_env=False)

        assert _variants(result["env"], "_RLM_SERVICE_NO_ENV") == {}

    def test_configured_env_clears_an_inherited_no_env_marker(self, service_mod, monkeypatch, tmp_path):
        env_file = tmp_path / "service.env"
        env_file.write_text("_rlm_service_no_env=1\n", encoding="utf-8")

        result = _run_server_once(service_mod, monkeypatch, tmp_path, capture_dir=None, env_file=str(env_file))

        assert _variants(result["env"], "_RLM_SERVICE_NO_ENV") == {}

    @windows_only
    def test_child_process_really_sees_no_marker(self, service_mod, monkeypatch, tmp_path):
        """Сквозная проверка настоящим ребёнком: dict-ассертов тут мало.

        Именно на этом шаге дефект и виден — регистронезависимость применяет ОС
        при создании процесса, а не Python при сборке словаря.
        """
        looks_legit = tmp_path / SERVICE_CAPTURE_DIRNAME
        looks_legit.mkdir()
        env_file = tmp_path / "hostile.env"
        env_file.write_text(f"_rlm_git_capture_dir={looks_legit}\n", encoding="utf-8")

        result = _run_server_once(service_mod, monkeypatch, tmp_path, capture_dir=None, env_file=str(env_file))

        probe = subprocess.run(
            [sys.executable, "-c", f"import os,sys; sys.stdout.write(repr(os.environ.get({GIT_CAPTURE_DIR_ENV!r})))"],
            env=result["env"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        )
        assert probe.stdout == "None", f"ребёнок всё-таки увидел маркер: {probe.stdout}"


class TestSvcDoRunDegradation:
    def test_service_starts_even_when_capture_preparation_fails(self, service_mod, monkeypatch):
        fake_event = types.ModuleType("win32event")
        fake_event.INFINITE = -1
        fake_event.CreateEvent = lambda *a: "event"
        fake_event.WaitForSingleObject = lambda *a: 0
        fake_service = types.ModuleType("win32service")
        fake_service.SERVICE_RUNNING = 4
        monkeypatch.setattr(service_mod, "win32event", fake_event)
        monkeypatch.setattr(service_mod, "win32service", fake_service)

        def boom():
            raise OSError("forced DACL failure")

        monkeypatch.setattr(service_mod, "_prepare_git_capture_dir", boom)

        started: list[str] = []
        monkeypatch.setattr(service_mod.RlmWindowsService, "_run_server", lambda self: started.append("ran"))

        statuses: list[int] = []
        svc = service_mod.RlmWindowsService.__new__(service_mod.RlmWindowsService)
        svc.ReportServiceStatus = statuses.append
        svc.SvcDoRun()
        svc._thread.join(timeout=10)

        assert svc._git_capture_dir is None
        assert svc._git_capture_error == "OSError"
        assert started == ["ran"], "watchdog-поток стартовал"
        assert statuses == [fake_service.SERVICE_RUNNING], "служба сообщила SERVICE_RUNNING"


@windows_only
class TestPrepareCaptureDir:
    def test_prepares_exact_path_under_config_root(self, service_mod, monkeypatch, tmp_path):
        import win32security

        config = tmp_path / "cfg" / "service.json"
        config.parent.mkdir()
        monkeypatch.setattr(service_mod, "_config_path", lambda: config)

        target = service_mod._prepare_git_capture_dir()

        assert target == config.parent / SERVICE_CAPTURE_DIRNAME
        assert target.is_dir()
        assert not service_mod._is_reparse_point(target)

        # Проверка повторно, «снаружи»: DACL protected и без посторонних принципалов.
        sd = win32security.GetNamedSecurityInfo(
            str(target), win32security.SE_FILE_OBJECT, win32security.DACL_SECURITY_INFORMATION
        )
        control, _rev = sd.GetSecurityDescriptorControl()
        assert control & win32security.SE_DACL_PROTECTED
        dacl = sd.GetSecurityDescriptorDacl()
        allowed = {win32security.ConvertSidToStringSid(s) for s in service_mod._git_capture_allowed_sids()}
        assert dacl.GetAceCount() > 0
        for i in range(dacl.GetAceCount()):
            (_ace_type, ace_flags), _mask, sid = dacl.GetAce(i)
            assert not ace_flags & win32security.INHERITED_ACE
            assert win32security.ConvertSidToStringSid(sid) in allowed

        # Маркер, выданный этим каталогом, принимается рантаймом as is.
        monkeypatch.setenv("RLM_UNDER_SERVICE", "1")
        monkeypatch.setenv("RLM_CONFIG_FILE", str(config))
        monkeypatch.setenv(GIT_CAPTURE_DIR_ENV, str(target))
        from rlm_tools_bsl import _git_process

        assert _git_process._git_capture_dir() == target

    def test_relative_config_path_is_rejected(self, service_mod, monkeypatch):
        monkeypatch.setattr(service_mod, "_config_path", lambda: pathlib.Path("cfg") / "service.json")
        with pytest.raises(OSError, match="not absolute"):
            service_mod._prepare_git_capture_dir()

    def test_reparse_point_is_rejected(self, service_mod, monkeypatch, tmp_path):
        config = tmp_path / "cfg" / "service.json"
        config.parent.mkdir()
        elsewhere = tmp_path / "снаружи"
        elsewhere.mkdir()
        link = config.parent / SERVICE_CAPTURE_DIRNAME
        try:
            os.symlink(str(elsewhere), str(link), target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("создание junction/symlink требует прав (SeCreateSymbolicLinkPrivilege)")
        monkeypatch.setattr(service_mod, "_config_path", lambda: config)
        with pytest.raises(OSError, match="reparse point"):
            service_mod._prepare_git_capture_dir()

    def test_dacl_failure_propagates_and_leaves_no_marker(self, service_mod, monkeypatch, tmp_path):
        config = tmp_path / "cfg" / "service.json"
        config.parent.mkdir()
        monkeypatch.setattr(service_mod, "_config_path", lambda: config)
        monkeypatch.setattr(
            service_mod,
            "_verify_protected_dacl",
            lambda _p: (_ for _ in ()).throw(OSError("git capture directory DACL is not protected")),
        )
        with pytest.raises(OSError, match="not protected"):
            service_mod._prepare_git_capture_dir()

        # SvcDoRun такой отказ проглатывает, маркер не выставляется.
        result = _run_server_once(service_mod, monkeypatch, tmp_path, capture_dir=None)
        assert GIT_CAPTURE_DIR_ENV not in result["env"]
