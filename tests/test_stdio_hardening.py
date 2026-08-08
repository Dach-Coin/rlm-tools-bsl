"""Разводка стандартных дескрипторов для stdio-транспорта (v1.32.1).

Проверки, которые трогают fd 0/1, вынесены в дочерний процесс: внутри pytest
подмена стандартных дескрипторов сломала бы capture, а главное — состояние
«fd 0 это pipe, на котором висит блокирующее чтение» в самом pytest не
воспроизводится.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
import textwrap

import pytest

from rlm_tools_bsl._stdio_hardening import harden_stdio_for_children, is_enabled

REPO_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")


def _child_env(**overrides: str) -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = REPO_SRC + os.pathsep + env.get("PYTHONPATH", "")
    env.pop("RLM_STDIO_HARDENING", None)
    env.update(overrides)
    return env


# --- чистые ветки, без хирургии над дескрипторами ---------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, True),
        ("", True),
        ("1", True),
        ("yes", True),
        ("0", False),
        ("false", False),
        ("FALSE", False),
        (" off ", False),
        ("no", False),
    ],
)
def test_is_enabled_reads_env(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("RLM_STDIO_HARDENING", raising=False)
    else:
        monkeypatch.setenv("RLM_STDIO_HARDENING", raw)
    assert is_enabled() is expected


def test_disabled_via_env_touches_nothing(monkeypatch):
    monkeypatch.setenv("RLM_STDIO_HARDENING", "0")
    stdin_before, stdout_before = sys.stdin, sys.stdout

    result = harden_stdio_for_children()

    assert result.applied is False
    assert "RLM_STDIO_HARDENING" in result.detail
    assert (sys.stdin, sys.stdout) == (stdin_before, stdout_before)


def test_skipped_when_streams_are_not_on_std_fds(monkeypatch):
    """Транспорт, которому подсунули не-файловые потоки, разводить нечего."""
    monkeypatch.delenv("RLM_STDIO_HARDENING", raising=False)
    monkeypatch.setattr(sys, "stdin", io.StringIO())
    monkeypatch.setattr(sys, "stdout", io.StringIO())

    result = harden_stdio_for_children()

    assert result.applied is False
    assert "стандартных дескрипторах" in result.detail
    assert isinstance(sys.stdout, io.StringIO)


def test_fastmcp_exposes_low_level_entry_point():
    """Растяжка: пропажа этой точки входа молча уронит изоляцию в запасной режим."""
    from rlm_tools_bsl.server import mcp

    low_level = getattr(mcp, "_mcp_server", None)
    assert callable(getattr(low_level, "run", None))
    assert callable(getattr(low_level, "create_initialization_options", None))


def test_prepare_stdio_transport_falls_back_when_hardening_disabled(monkeypatch):
    """С отключённой разводкой транспорт обязан подниматься штатным путём.

    Иначе он получил бы `wire_stdin=None` и упал бы на старте.
    """
    monkeypatch.setenv("RLM_STDIO_HARDENING", "0")
    from rlm_tools_bsl import server

    restore, runner = server._prepare_stdio_transport()

    assert runner is None
    restore()  # безопасный no-op


# --- поведение на настоящих дескрипторах ------------------------------------


_DIVERSION_CHILD = textwrap.dedent(
    """
    import os, subprocess, sys
    from rlm_tools_bsl._stdio_hardening import harden_stdio_for_children

    swap = sys.argv[1] == "swap"
    h = harden_stdio_for_children(swap_sys_streams=swap)
    assert h.applied, h.detail
    assert h.wire_stdin.fileno() > 2, h.wire_stdin.fileno()
    assert h.wire_stdout.fileno() > 2, h.wire_stdout.fileno()

    # Постороннее raw-письмо в fd 1 обязано уйти в stderr, а не в протокол.
    os.write(1, b"STRAY_RAW\\n")
    # Вывод дочернего процесса — тоже (наследует уведённые дескрипторы).
    subprocess.run([sys.executable, "-c", "print('STRAY_CHILD')"], check=True)
    # fd 0 уведён в devnull: посторонний код и дети читают EOF...
    assert os.read(0, 16) == b"", "fd 0 не уведён в devnull"

    if swap:
        # Запасной путь: транспорт берёт потоки из sys, туда же пишет print().
        assert sys.stdout is h.wire_stdout
        assert sys.stdin is h.wire_stdin
    else:
        # Предпочтительный путь: sys-потоки остались на отводах, и посторонний
        # print()/чтение sys.stdin до протокола не дотягиваются.
        assert sys.stdout.buffer.fileno() == 1, sys.stdout.buffer.fileno()
        assert sys.stdin.buffer.fileno() == 0, sys.stdin.buffer.fileno()
        print("STRAY_PRINT", flush=True)
        assert sys.stdin.readline() == "", "sys.stdin читает провод вместо отвода"

    # Провод в обе стороны жив: это и есть протокол.
    h.wire_stdout.write("WIRE\\n")
    h.wire_stdout.flush()
    assert h.wire_stdin.readline() == "PING\\n", "провод stdin потерян"

    h.restore()
    sys.stdout.write("RESTORED\\n")
    sys.stdout.flush()
    os.write(1, b"RAW_AFTER_RESTORE\\n")
    """
)


@pytest.mark.parametrize("mode", ["explicit", "swap"])
def test_diverts_children_and_restores(tmp_path, mode):
    script = tmp_path / f"diversion_child_{mode}.py"
    script.write_text(_DIVERSION_CHILD, encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, str(script), mode],
        input=b"PING\n",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_child_env(),
        timeout=60,
    )
    out = proc.stdout.decode("utf-8", "replace")
    err = proc.stderr.decode("utf-8", "replace")

    assert proc.returncode == 0, f"stdout={out!r} stderr={err!r}"
    assert "WIRE" in out
    for stray in ("STRAY_RAW", "STRAY_CHILD"):
        assert stray not in out, f"{stray} попал в протокол: {out!r}"
        assert stray in err, f"{stray} не дошёл до stderr: {err!r}"
    if mode == "explicit":
        assert "STRAY_PRINT" not in out, f"посторонний print() попал в протокол: {out!r}"
        assert "STRAY_PRINT" in err
    # После restore обе записи снова идут в протокольный stdout.
    assert "RESTORED" in out
    assert "RAW_AFTER_RESTORE" in out


_ROLLBACK_CHILD = textwrap.dedent(
    """
    import os, sys
    import rlm_tools_bsl._stdio_hardening as sh

    mode = sys.argv[1]
    real_dup, real_dup2 = os.dup, os.dup2
    calls = {"n": 0}

    if mode == "dup":
        # Провод stdin снят, провод stdout снять не удалось.
        def fake_dup(fd):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("инъекция отказа os.dup")
            return real_dup(fd)
        os.dup = fake_dup
    else:
        # fd 0 уже уведён, отвод fd 1 сорвался — самый неприятный момент.
        def fake_dup2(src, dst, **kw):
            if dst == 1 and calls["n"] == 0:
                calls["n"] += 1
                raise OSError("инъекция отказа os.dup2")
            return real_dup2(src, dst, **kw)
        os.dup2 = fake_dup2

    # Номер, который выдаёт следующий свободный дескриптор: после отката он
    # обязан остаться прежним — иначе провод остался незакрытым.
    free_fd_before = real_dup(0)
    os.close(free_fd_before)

    streams_before = (sys.stdin, sys.stdout)
    result = sh.harden_stdio_for_children()
    assert result.applied is False, result.detail
    assert (sys.stdin, sys.stdout) == streams_before, "потоки подменены при неудачной разводке"

    os.dup, os.dup2 = real_dup, real_dup2

    free_fd_after = os.dup(0)
    os.close(free_fd_after)
    assert free_fd_after == free_fd_before, f"утечка дескриптора: {free_fd_before} -> {free_fd_after}"

    # Дескрипторы целы: протокол работает в обе стороны.
    assert sys.stdout.buffer.fileno() == 1, sys.stdout.buffer.fileno()
    sys.stdout.write("ALIVE\\n")
    sys.stdout.flush()
    assert sys.stdin.readline() == "PING\\n", "провод stdin потерян при откате"

    # И честная разводка после отката проходит — протокол идёт по проводу.
    second = sh.harden_stdio_for_children()
    assert second.applied is True, second.detail
    second.wire_stdout.write("HARDENED\\n")
    second.wire_stdout.flush()
    second.restore()
    """
)


@pytest.mark.parametrize("mode", ["dup", "dup2"])
def test_failed_diversion_rolls_back_without_leaking(tmp_path, mode):
    """Заявленный best-effort: сбой разводки не оставляет ни полусостояния, ни утечки."""
    script = tmp_path / f"rollback_child_{mode}.py"
    script.write_text(_ROLLBACK_CHILD, encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, str(script), mode],
        input=b"PING\n",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_child_env(),
        timeout=60,
    )
    out = proc.stdout.decode("utf-8", "replace")
    err = proc.stderr.decode("utf-8", "replace")

    assert proc.returncode == 0, f"stdout={out!r} stderr={err!r}"
    assert "ALIVE" in out
    assert "HARDENED" in out


_RESTORE_RETRY_CHILD = textwrap.dedent(
    """
    import os, sys
    import rlm_tools_bsl._stdio_hardening as sh

    h = sh.harden_stdio_for_children()
    assert h.applied, h.detail

    real_dup2 = os.dup2
    budget = {"failures": 1}

    def flaky_dup2(src, dst, **kw):
        if dst == 0 and budget["failures"]:
            budget["failures"] -= 1
            raise OSError("инъекция отказа восстановления fd 0")
        return real_dup2(src, dst, **kw)

    os.dup2 = flaky_dup2
    h.restore()          # fd 0 вернуть не удалось
    os.dup2 = real_dup2
    assert os.read(0, 16) == b"", "fd 0 считается восстановленным, хотя dup2 отказал"

    h.restore()          # повторная попытка обязана доделать работу
    assert os.read(0, 16) == b"TAIL\\n", "повторный restore не вернул fd 0 на провод"

    h.wire_stdout.write("RETRY_OK\\n")
    h.wire_stdout.flush()
    """
)


def test_failed_restore_can_be_retried(tmp_path):
    """Разовый отказ `dup2` не имеет права навсегда пометить restore выполненным."""
    script = tmp_path / "restore_retry_child.py"
    script.write_text(_RESTORE_RETRY_CHILD, encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, str(script)],
        input=b"TAIL\n",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_child_env(),
        timeout=60,
    )
    out = proc.stdout.decode("utf-8", "replace")
    err = proc.stderr.decode("utf-8", "replace")

    assert proc.returncode == 0, f"stdout={out!r} stderr={err!r}"
    assert "RETRY_OK" in out


_HANG_REPRO_CHILD = textwrap.dedent(
    """
    import multiprocessing, os, sys, threading, time

    def worker_main(conn):
        conn.send({"ready": True})
        time.sleep(30)

    def hold_stdin():
        try:
            sys.stdin.buffer.readline()   # родитель никогда не пишет
        except Exception:
            pass

    if __name__ == "__main__":
        mode = sys.argv[1]
        budget = float(sys.argv[2])
        if mode == "with":
            from rlm_tools_bsl._stdio_hardening import harden_stdio_for_children
            h = harden_stdio_for_children()
            assert h.applied, h.detail
        # Висящее блокирующее чтение на протокольной трубе — то самое состояние,
        # в котором stdio-транспорт держит fd 0 весь сеанс.
        threading.Thread(target=hold_stdin, daemon=True).start()
        time.sleep(0.5)

        ctx = multiprocessing.get_context("spawn")
        parent, child = ctx.Pipe(duplex=True)
        proc = ctx.Process(target=worker_main, args=(child,), daemon=True)
        t0 = time.monotonic()
        proc.start()
        child.close()
        ready = False
        while time.monotonic() - t0 < budget:
            if parent.poll(0.25):
                parent.recv()
                ready = True
                break
        print("RESULT=" + ("READY" if ready else "HANG"), file=sys.stderr, flush=True)
        try:
            proc.kill()
        except Exception:
            pass
        # Штатный выход невозможен: поток-читатель навсегда заблокирован в
        # readline() и на финализации интерпретатора даёт фатальную ошибку
        # (_enter_buffered_busy). Отчёт уже отправлен — выходим без финализации.
        os._exit(0)
    """
)


def _run_hang_repro(tmp_path, mode: str, budget: float) -> str:
    """Запустить репро так, как это делает MCP-клиент: труба stdin открыта и пуста.

    Именно `Popen` без `communicate()`: `subprocess.run(stdin=PIPE)` закрыл бы
    трубу сразу, чтение в ребёнке вернуло бы EOF — и висящего чтения, которое и
    порождает баг, не возникло бы вовсе.
    """
    script = tmp_path / f"hang_repro_{mode}.py"
    script.write_text(_HANG_REPRO_CHILD, encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, str(script), mode, str(budget)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_child_env(),
    )
    try:
        proc.wait(timeout=budget + 60)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=30)
        raise
    finally:
        # stdout/stderr репро — единицы байт, переполнение труб исключено.
        err = proc.stderr.read().decode("utf-8", "replace")
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            stream.close()

    assert proc.returncode == 0, f"repro-ребёнок упал: rc={proc.returncode} stderr={err!r}"
    assert "RESULT=" in err, f"репро не отчитался: {err!r}"
    return err.rsplit("RESULT=", 1)[1].split()[0]


def test_spawn_under_pending_stdin_read_does_not_hang(tmp_path):
    """Регресс issue #25: с разводкой дочерний Python стартует и под висящим чтением."""
    assert _run_hang_repro(tmp_path, "with", 20.0) == "READY"


@pytest.mark.skipif(sys.platform != "win32", reason="сериализация синхронного pipe — поведение Windows")
def test_repro_control_hangs_without_hardening(tmp_path):
    """Контроль: без разводки ребёнок виснет — иначе позитивный тест вакуумный."""
    result = _run_hang_repro(tmp_path, "without", 8.0)
    if result == "READY":
        pytest.skip("платформа больше не воспроизводит gh-78961: позитивный тест перестал быть доказательным")
    assert result == "HANG"
