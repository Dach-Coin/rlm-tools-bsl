"""v1.48.0 — ускорение сборки индексов; содержимое индекса прежнее.

Разделы: журнал Windows-службы — один писатель ``server.log`` (``_service_logging``); исполнитель сборки —
процессы, общий бюджет CPU, упорядоченная выдача, spool (``_index_workers``).
"""

from __future__ import annotations

import logging
import multiprocessing
import os
import re
import subprocess
import sys
import textwrap
import time
import types
from logging.handlers import RotatingFileHandler
from pathlib import Path

import _index_worker_tasks as TASKS
import pytest

from rlm_tools_bsl import _index_workers as IW
from rlm_tools_bsl import _service_logging as SL

# ═══════════════ Журнал службы: один писатель server.log ═══════════════

_LEADING_TS = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d ")
_CLOCK = "2026-10-27 10:00:00"


def _all_log_lines(path: Path) -> list[str]:
    """Строки журнала в хронологическом порядке: самая старая копия ротации первой, активный файл последним."""
    backups = sorted(path.parent.glob(path.name + ".*"), key=lambda p: -int(p.suffix[1:]))
    lines: list[str] = []
    for part in [*backups, path]:
        lines.extend(part.read_text(encoding="utf-8").splitlines())
    return lines


class _Chunks:
    """Поток вывода ребёнка: ``read1`` отдаёт заранее заданные порции, затем EOF."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)
        self.closed = False

    def read1(self, _size: int) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""

    def close(self) -> None:
        self.closed = True


def _pumped(tmp_path: Path, chunks: list[bytes], **pump_kw) -> list[str]:
    sink = SL.ServiceLogSink(tmp_path / "server.log", clock=lambda: _CLOCK)
    pump = SL.ChildOutputPump(_Chunks(chunks), sink, **pump_kw)
    pump.start()
    assert pump.finish(timeout=10)
    sink.close()
    return (tmp_path / "server.log").read_text(encoding="utf-8").splitlines()


class TestServiceLogSink:
    def test_watchdog_line_carries_prefix_and_time(self, tmp_path):
        sink = SL.ServiceLogSink(tmp_path / "server.log", clock=lambda: _CLOCK)
        sink.watchdog("Starting subprocess: %s", "rlm-tools-bsl.exe")
        sink.close()
        text = (tmp_path / "server.log").read_text(encoding="utf-8")
        assert text == f"[watchdog {_CLOCK}] Starting subprocess: rlm-tools-bsl.exe\n"

    def test_child_line_with_time_is_kept_verbatim(self, tmp_path):
        line = "2026-10-01 09:00:00 INFO rlm_tools_bsl.server: rlm_start: session=abc"
        sink = SL.ServiceLogSink(tmp_path / "server.log", clock=lambda: _CLOCK)
        sink.write_line(line)
        sink.close()
        assert (tmp_path / "server.log").read_text(encoding="utf-8") == line + "\n"

    def test_raw_child_line_gets_time_of_receipt(self, tmp_path):
        """Сырой вывод ребёнка (строки uvicorn, трассировка аварии) — со временем приёма в начале строки."""
        sink = SL.ServiceLogSink(tmp_path / "server.log", clock=lambda: _CLOCK)
        sink.write_line('INFO:     127.0.0.1:61087 - "POST /mcp HTTP/1.1" 200 OK')
        sink.close()
        text = (tmp_path / "server.log").read_text(encoding="utf-8")
        assert text == f'{_CLOCK} INFO:     127.0.0.1:61087 - "POST /mcp HTTP/1.1" 200 OK\n'

    def test_closed_sink_drops_late_lines_silently(self, tmp_path):
        sink = SL.ServiceLogSink(tmp_path / "server.log", clock=lambda: _CLOCK)
        sink.close()
        sink.write_line("late line")
        sink.watchdog("late event")
        assert (tmp_path / "server.log").read_text(encoding="utf-8") == ""

    def test_rotation_keeps_every_event_exactly_once(self, tmp_path):
        """60 пронумерованных событий при пороге 3 КБ: копия ротации создана, каждый номер — ровно один раз."""
        path = tmp_path / "server.log"
        sink = SL.ServiceLogSink(path, max_bytes=3000, clock=lambda: _CLOCK)
        for i in range(60):
            if i % 15 == 0:
                sink.watchdog("Health check OK (event %d)", i)
            sink.write_line(f"2026-10-27 10:00:{i % 60:02d} INFO child: record {i:03d} payload {'x' * 20}")
        sink.close()
        assert (tmp_path / "server.log.1").exists()
        lines = _all_log_lines(path)
        numbers = [int(m.group(1)) for line in lines for m in [re.search(r"record (\d+)", line)] if m]
        assert numbers == list(range(60))
        assert all(_LEADING_TS.match(line) or line.startswith("[watchdog ") for line in lines)
        assert not [line for line in lines if "Logging error" in line or "PermissionError" in line]


class TestChildOutputPump:
    def test_utf8_char_split_between_reads_and_crlf(self, tmp_path):
        first = "2026-10-27 09:59:59 INFO a: Юникод ⏎".encode()
        lines = _pumped(tmp_path, [first[:-2], first[-2:] + b"\r", b"\nraw line\r\n"])
        assert lines == ["2026-10-27 09:59:59 INFO a: Юникод ⏎", f"{_CLOCK} raw line"]

    def test_tail_without_newline_is_written_at_eof(self, tmp_path):
        lines = _pumped(tmp_path, [b"2026-10-27 09:00:00 INFO a: one\nTraceback (most recent", b" call last):"])
        assert lines == ["2026-10-27 09:00:00 INFO a: one", f"{_CLOCK} Traceback (most recent call last):"]

    def test_unfinished_line_is_bounded(self, tmp_path):
        lines = _pumped(tmp_path, [b"x" * 25], partial_max=10)
        assert lines == [f"{_CLOCK} {'x' * 10}", f"{_CLOCK} {'x' * 10}", f"{_CLOCK} {'x' * 5}"]

    def test_finish_closes_the_stream_after_eof(self, tmp_path):
        sink = SL.ServiceLogSink(tmp_path / "server.log", clock=lambda: _CLOCK)
        stream = _Chunks([b"a\n"])
        pump = SL.ChildOutputPump(stream, sink)
        pump.start()
        assert pump.finish(timeout=10)
        sink.close()
        assert stream.closed


_STAND_CHILD = textwrap.dedent(
    """\
    import logging, sys
    from rlm_tools_bsl._service_logging import LOG_DATEFMT, LOG_FORMAT
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    log = logging.getLogger("child")
    for i in range(60):
        log.info("record %03d payload %s", i, "x" * 20)
        if i == 30:
            print('INFO:     127.0.0.1:1 - "POST /mcp HTTP/1.1" 200 OK', flush=True)
    try:
        1 / 0
    except ZeroDivisionError:
        log.exception("boom Юникод ⏎")
    sys.stdout.write("Б" * 200000)
    sys.stdout.flush()
    """
)


def _child_env() -> dict:
    env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    src = str(Path(SL.__file__).resolve().parents[1])
    env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
    return env


def test_stand_real_child_through_pipe(tmp_path):
    """Стенд плана: настоящий ребёнок пишет в PIPE записи с временем, сырую строку, трассировку, Юникод и
    хвост больше ёмкости PIPE без перевода строки; порог ротации 3 КБ."""
    script = tmp_path / "child.py"
    script.write_text(_STAND_CHILD, encoding="utf-8")
    path = tmp_path / "server.log"
    sink = SL.ServiceLogSink(path, max_bytes=3000, backup_count=100)
    sink.watchdog("Starting subprocess: %s", "child.py")
    proc = subprocess.Popen(
        [sys.executable, str(script)], env=_child_env(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    )
    pump = SL.start_output_pump(proc, sink)
    assert proc.wait(timeout=120) == 0
    assert pump is not None and pump.finish(timeout=30)
    sink.watchdog("Process exited (code=%s)", proc.returncode)
    sink.close()

    lines = _all_log_lines(path)
    assert (tmp_path / "server.log.1").exists()
    numbers = [int(m.group(1)) for line in lines for m in [re.search(r"record (\d+) payload", line)] if m]
    assert numbers == list(range(60))
    assert all(_LEADING_TS.match(line) or line.startswith("[watchdog ") for line in lines), [
        line[:80] for line in lines if not (_LEADING_TS.match(line) or line.startswith("[watchdog "))
    ]
    assert any(line.endswith('INFO:     127.0.0.1:1 - "POST /mcp HTTP/1.1" 200 OK') for line in lines)
    assert any("ERROR child: boom Юникод ⏎" in line for line in lines)
    assert any(line.endswith("Traceback (most recent call last):") for line in lines)
    assert any(line.endswith("ZeroDivisionError: division by zero") for line in lines)
    assert sum(line.count("Б") for line in lines) == 200000
    assert lines[0].startswith("[watchdog ") and lines[-1].startswith("[watchdog ")
    assert not [line for line in lines if "Logging error" in line or "PermissionError" in line]


class TestServerUnderService:
    def _restore(self, before_handlers, before_formatters):
        root = logging.getLogger()
        for handler in list(root.handlers):
            if handler not in before_handlers:
                root.removeHandler(handler)
                handler.close()
        for handler, formatter in before_formatters.items():
            handler.setFormatter(formatter)

    def test_child_writes_timestamped_stderr_and_never_opens_server_log(self, monkeypatch, tmp_path):
        from rlm_tools_bsl import server

        config_file = tmp_path / "svc" / "service.json"
        config_file.parent.mkdir(parents=True)
        monkeypatch.setenv("RLM_CONFIG_FILE", str(config_file))
        monkeypatch.setenv("RLM_UNDER_SERVICE", "1")
        root = logging.getLogger()
        before = list(root.handlers)
        formatters = {h: h.formatter for h in before}
        try:
            server._setup_file_logging()
            after_first = list(root.handlers)
            server._setup_file_logging()
            assert root.handlers == after_first, "повторный вызов добавил обработчик"
            # Только добавленные этим вызовом: файловые обработчики, оставленные раньше в том же процессе другими
            # тестами (`server.main()` под streamable-http вешает RotatingFileHandler и не снимает), его не касаются.
            assert not [h for h in root.handlers if isinstance(h, RotatingFileHandler) and h not in before]
            assert not server._server_log_path().exists()
            stamped = [
                h
                for h in root.handlers
                if type(h) is logging.StreamHandler
                and h.formatter is not None
                and h.formatter._fmt == SL.LOG_FORMAT
                and h.formatter.datefmt == SL.LOG_DATEFMT
            ]
            assert len(stamped) == 1, root.handlers
            added = [h for h in root.handlers if h not in before]
            assert len(added) <= 1 and all(type(h) is logging.StreamHandler for h in added), added
        finally:
            self._restore(before, formatters)

    def test_plain_http_keeps_its_own_rotating_handler(self, monkeypatch, tmp_path):
        from rlm_tools_bsl import server

        config_file = tmp_path / "plain" / "service.json"
        config_file.parent.mkdir(parents=True)
        monkeypatch.setenv("RLM_CONFIG_FILE", str(config_file))
        monkeypatch.delenv("RLM_UNDER_SERVICE", raising=False)
        root = logging.getLogger()
        before = list(root.handlers)
        formatters = {h: h.formatter for h in before}
        try:
            server._setup_file_logging()
            added = [h for h in root.handlers if h not in before]
            assert [type(h) for h in added] == [RotatingFileHandler]
            assert added[0].formatter._fmt == SL.LOG_FORMAT
            assert server._server_log_path().exists()
        finally:
            self._restore(before, formatters)


def test_watchdog_reads_child_output_through_a_pipe(monkeypatch, tmp_path, request):
    """Служба отдаёт ребёнку PIPE, а не дескриптор server.log; вывод ребёнка доходит до журнала со временем."""
    from test_service_win_integration import _import_service_win, _install_stubs

    _install_stubs(monkeypatch)
    win32event = sys.modules["win32event"]
    win32event.WAIT_TIMEOUT = 258
    mod = _import_service_win(monkeypatch, request)

    clock = types.SimpleNamespace(now=0.0, five_second_waits=0)
    seen: dict = {}
    real_popen = subprocess.Popen

    def popen(argv, env=None, stdout=None, stderr=None):
        seen.update(stdout=stdout, stderr=stderr)
        child = "import sys; print('INFO:     child raw line', flush=True); sys.exit(3)"
        return real_popen([sys.executable, "-c", child], env=_child_env(), stdout=stdout, stderr=stderr)

    def wait_for(_handle, ms):
        # Первая пятисекундная пауза — окно готовности, вторая — пауза перед перезапуском: там служба и
        # останавливается. Часы фиктивные, но настоящему ребёнку даём время дожить до выхода.
        if ms == 5000:
            clock.five_second_waits += 1
            if clock.five_second_waits == 2:
                return 0
        clock.now += ms / 1000.0
        time.sleep(0.02)
        return 258

    win32event.WaitForSingleObject = wait_for
    monkeypatch.setattr(mod, "time", types.SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr(
        mod,
        "subprocess",
        types.SimpleNamespace(
            Popen=popen,
            PIPE=subprocess.PIPE,
            STDOUT=subprocess.STDOUT,
            TimeoutExpired=subprocess.TimeoutExpired,
        ),
    )
    monkeypatch.setattr(mod, "_check_health", lambda url: True)
    monkeypatch.setattr(mod, "_config_path", lambda: tmp_path / "service.json")
    monkeypatch.setattr(
        mod,
        "load_config",
        lambda: {"host": "127.0.0.1", "port": 9000, "env_file": None, "exe_path": "rlm-tools-bsl"},
    )
    service = object.__new__(mod.RlmWindowsService)
    service._stop_event = "stop-event"
    service._git_capture_dir = None
    service._git_capture_error = "TestError"
    service._git_capture_swept = 0
    service._stopping = False
    service._serve()

    assert seen == {"stdout": subprocess.PIPE, "stderr": subprocess.STDOUT}
    lines = (tmp_path / "logs" / "server.log").read_text(encoding="utf-8").splitlines()
    assert any(_LEADING_TS.match(line) and line.endswith("INFO:     child raw line") for line in lines), lines
    assert any(line.startswith("[watchdog ") and "Git capture unavailable" in line for line in lines), lines
    exited = [i for i, line in enumerate(lines) if "Process exited (code=3" in line]
    raw = [i for i, line in enumerate(lines) if line.endswith("child raw line")]
    assert exited and raw and raw[0] < exited[0], lines


# ═══════════════ Исполнитель сборки: лимит CPU, пул процессов, порядок, spool ═══════════════


class TestWorkerLimit:
    def test_auto_is_capped(self, monkeypatch):
        monkeypatch.setattr(IW, "available_cpus", lambda: 16)
        assert IW.resolve_index_workers("auto")[0] == IW.AUTO_WORKERS_CAP
        assert IW.resolve_index_workers("")[0] == IW.AUTO_WORKERS_CAP
        assert IW.resolve_index_workers(None)[0] in range(1, IW.AUTO_WORKERS_CAP + 1)

    def test_explicit_value_is_limited_by_available_cpu(self, monkeypatch):
        monkeypatch.setattr(IW, "available_cpus", lambda: 4)
        assert IW.resolve_index_workers("1")[0] == 1
        assert IW.resolve_index_workers("3")[0] == 3
        count, source = IW.resolve_index_workers("12")
        assert count == 4 and "limited" in source

    @pytest.mark.parametrize("raw", ["0", "-2", "много", "2.5"])
    def test_invalid_value_warns_and_falls_back_to_auto(self, monkeypatch, caplog, raw):
        monkeypatch.setattr(IW, "available_cpus", lambda: 2)
        with caplog.at_level(logging.WARNING, logger="rlm_tools_bsl._index_workers"):
            count, source = IW.resolve_index_workers(raw)
        assert count == 2 and "auto" in source
        assert IW.WORKERS_ENV in caplog.text

    def test_cgroup_v2_quota(self, tmp_path):
        (tmp_path / "cpu.max").write_text("150000 100000\n", encoding="ascii")
        assert IW._cgroup_cpu_quota(str(tmp_path)) == 1.5
        (tmp_path / "cpu.max").write_text("max 100000\n", encoding="ascii")
        assert IW._cgroup_cpu_quota(str(tmp_path)) is None

    def test_cgroup_v1_quota(self, tmp_path):
        cpu = tmp_path / "cpu"
        cpu.mkdir()
        (cpu / "cpu.cfs_quota_us").write_text("200000\n", encoding="ascii")
        (cpu / "cpu.cfs_period_us").write_text("100000\n", encoding="ascii")
        assert IW._cgroup_cpu_quota(str(tmp_path)) == 2.0
        (cpu / "cpu.cfs_quota_us").write_text("-1\n", encoding="ascii")
        assert IW._cgroup_cpu_quota(str(tmp_path)) is None

    def test_fractional_quota_gives_at_least_one_worker(self, monkeypatch):
        monkeypatch.setattr(IW, "_affinity_cpus", lambda: 8)
        monkeypatch.setattr(IW, "_cgroup_cpu_quota", lambda root="/sys/fs/cgroup": 0.5)
        assert IW.available_cpus() == 1
        monkeypatch.setattr(IW, "_cgroup_cpu_quota", lambda root="/sys/fs/cgroup": 2.7)
        assert IW.available_cpus() == 2
        monkeypatch.setattr(IW, "_cgroup_cpu_quota", lambda root="/sys/fs/cgroup": None)
        assert IW.available_cpus() == 8


def test_bsl_batches_keep_order_and_bound_size():
    paths = [f"m{i:05d}.bsl" for i in range(10_000)]
    batches = IW.bsl_batches(paths, 8)
    assert [p for b in batches for p in b] == paths
    assert max(len(b) for b in batches) <= 64 and len(batches) >= 8 * 8
    assert IW.bsl_batches([], 4) == []
    assert [p for b in IW.bsl_batches(paths[:5], 4) for p in b] == paths[:5]


class TestSequentialPool:
    def test_map_ordered_runs_inline_in_order(self):
        done: list[int] = []
        with IW.IndexTaskPool(1) as pool:
            assert not pool.parallel
            blobs = list(pool.map_ordered(TASKS.echo, [(i,) for i in range(10)], window=3, on_done=done.append))
        import pickle

        assert [pickle.loads(b) for b in blobs] == list(range(10))
        assert sum(done) == 10

    def test_task_exception_propagates(self):
        with pytest.raises(ValueError, match="bad"):
            with IW.IndexTaskPool(1) as pool:
                pool.result(pool.submit(TASKS.boom, "bad"))


def _children_alive() -> list:
    return [p for p in multiprocessing.active_children() if p.is_alive()]


class TestProcessPool:
    """Настоящий spawn: порядок выдачи, перенос предупреждений, ошибки задачи и смерть воркера."""

    def test_order_survives_a_slow_first_task_and_workers_are_real(self):
        before = {p.pid for p in _children_alive()}
        done: list[int] = []
        with IW.IndexTaskPool(2, label="test") as pool:
            items = [(0, 0.8)] + [(i, 0.0) for i in range(1, 12)]
            got = [IW.pickle.loads(b) for b in pool.map_ordered(TASKS.echo, items, window=6, on_done=done.append)]
            pids = {pool.result(pool.submit(TASKS.worker_pid)) for _ in range(4)}
            assert pool.parallel and pool.worker_module and pool.worker_module.endswith("bsl_index.py")
        assert got == list(range(12))
        assert sum(done) == 12
        assert os.getpid() not in pids
        assert not [p for p in _children_alive() if p.pid not in before]

    def test_worker_warning_reaches_coordinator_log(self, caplog):
        with caplog.at_level(logging.WARNING, logger="rlm_tools_bsl"):
            with IW.IndexTaskPool(2, label="test") as pool:
                assert pool.result(pool.submit(TASKS.warn_and_return, 42)) == 42
        records = [r for r in caplog.records if r.getMessage() == "worker warning 42"]
        assert len(records) == 1 and records[0].name == "rlm_tools_bsl.bsl_index"

    def test_task_exception_keeps_its_type(self):
        before = {p.pid for p in _children_alive()}
        with pytest.raises(ValueError, match="parser failed"):
            with IW.IndexTaskPool(2, label="test") as pool:
                pool.result(pool.submit(TASKS.boom, "parser failed"))
        assert not [p for p in _children_alive() if p.pid not in before]

    def test_dead_worker_fails_the_operation(self):
        before = {p.pid for p in _children_alive()}
        with pytest.raises(IW.IndexWorkerError):
            with IW.IndexTaskPool(2, label="test") as pool:
                pool.result(pool.submit(TASKS.die))
        assert not [p for p in _children_alive() if p.pid not in before]

    def test_start_failure_warns_once_and_runs_sequentially(self, monkeypatch, caplog):
        def refuse(*_a, **_kw):
            raise PermissionError("no pipes in this sandbox")

        monkeypatch.setattr(IW, "ProcessPoolExecutor", refuse)
        with caplog.at_level(logging.WARNING, logger="rlm_tools_bsl._index_workers"):
            with IW.IndexTaskPool(4, label="test") as pool:
                got = [IW.pickle.loads(b) for b in pool.map_ordered(TASKS.echo, [(i,) for i in range(5)], window=2)]
                assert not pool.parallel
        assert got == list(range(5))
        assert caplog.text.count("cannot start worker processes") == 1


class TestResultSpool:
    def test_round_trip_twice_and_cleanup(self, tmp_path):
        import pickle

        with IW.ResultSpool(tmp_path) as spool:
            for i in range(3):
                spool.append(pickle.dumps([i, "строка"]))
            assert spool.path.parent == tmp_path and spool.path.name.startswith(".bsl_index.spool-")
            assert list(spool) == [[0, "строка"], [1, "строка"], [2, "строка"]]
            assert list(spool) == [[0, "строка"], [1, "строка"], [2, "строка"]]
            path = spool.path
        assert not path.exists()

    def test_wrong_format_is_refused(self, tmp_path):
        spool = IW.ResultSpool(tmp_path)
        try:
            spool.append(b"x")
            spool._file.seek(0)
            spool._file.write(b"OTHER FORMAT 99\n\n")
            with pytest.raises(RuntimeError, match="wrong format"):
                list(spool)
        finally:
            spool.close()


_UNGUARDED_SCRIPT = textwrap.dedent(
    """\
    import pickle, sys
    sys.path.insert(0, sys.argv[2])
    with open(sys.argv[1], "a", encoding="utf-8") as fh:
        fh.write("top-level run\\n")
    import _index_worker_tasks as TASKS
    from rlm_tools_bsl._index_workers import IndexTaskPool
    with IndexTaskPool(2, label="script") as pool:
        pids = {pool.result(pool.submit(TASKS.worker_pid)) for _ in range(4)}
        print("parallel" if pool.parallel else "sequential", len(pids))
    """
)


def test_spawned_workers_do_not_rerun_an_unguarded_script(tmp_path):
    """Скрипт без ``if __name__ == '__main__'`` (встраивание, инструменты замеров): spawn-ребёнок не исполняет его
    верхний уровень — иначе каждый воркер повторил бы побочные эффекты координатора (в A/B — попытку собрать тот
    же индекс)."""
    script = tmp_path / "unguarded.py"
    script.write_text(_UNGUARDED_SCRIPT, encoding="utf-8")
    marker = tmp_path / "marker.txt"
    tests_dir = str(Path(__file__).resolve().parent)
    out = subprocess.run(
        [sys.executable, str(script), str(marker), tests_dir],
        env=_child_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
    )
    assert out.returncode == 0, out.stderr[-3000:]
    assert out.stdout.split()[0] == "parallel", out.stdout + out.stderr[-2000:]
    assert marker.read_text(encoding="utf-8").splitlines() == ["top-level run"]


def test_frozen_interpreter_stays_sequential(monkeypatch, caplog):
    """Frozen-обёртка (PyInstaller/Nuitka) без ``freeze_support`` перезапустила бы в ребёнке всё приложение."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    with caplog.at_level(logging.INFO, logger="rlm_tools_bsl._index_workers"):
        with IW.IndexTaskPool(4, label="test") as pool:
            assert pool.result(pool.submit(TASKS.worker_pid)) == os.getpid()
            assert not pool.parallel
    assert "frozen" in caplog.text


# ═══════════════ Сборка: процессы ≡ последовательно, сбои ═══════════════


def _content(db_path) -> dict[str, list]:
    """Содержимое индекса без суррогатных id: внешние ключи — естественными (как обычный gate сверки 1.48.0)."""
    import sqlite3

    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        mod = dict(con.execute("SELECT id, rel_path FROM modules"))
        meth = {i: (mod.get(m), n, ln) for i, m, n, ln in con.execute("SELECT id, module_id, name, line FROM methods")}
        out: dict[str, list] = {}
        tables = [
            r[0]
            for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                "AND name NOT LIKE 'methods_fts_%'"
            )
        ]
        for table in tables:
            if table == "index_meta":
                rows = [r for r in con.execute("SELECT key, value FROM index_meta") if r[0] != "built_at"]
            elif table == "methods_fts":
                rows = [
                    (meth.get(r), n, o) for r, n, o in con.execute("SELECT rowid, name, object_name FROM methods_fts")
                ]
            else:
                cols = [c[1] for c in con.execute(f'PRAGMA table_info("{table}")')]
                rows = []
                for row in con.execute(f'SELECT * FROM "{table}"'):  # noqa: S608
                    d = dict(zip(cols, row))
                    d.pop("id", None)
                    for key, mapping in (("module_id", mod), ("source_module_id", mod), ("caller_id", meth)):
                        if d.get(key) is not None:
                            d[key] = mapping.get(d[key])
                    rows.append(tuple(sorted(d.items())))
            out[table] = sorted(rows, key=repr)
        return out
    finally:
        con.close()


def _world_root(tmp_path: Path, fmt: str) -> Path:
    import test_v1_47_0 as W

    root = tmp_path / f"world_{fmt}" / "cf"
    W._write_tree(root, W._world18_files(fmt))
    return root


def _build_with(monkeypatch, root: Path, index_dir: Path, workers: str, **build_kw) -> Path:
    from rlm_tools_bsl.bsl_index import IndexBuilder

    monkeypatch.setenv("RLM_INDEX_DIR", str(index_dir))
    monkeypatch.setenv("RLM_INDEX_WORKERS", workers)
    return IndexBuilder().build(str(root), **build_kw)


@pytest.mark.parametrize("fmt", ["cf", "edt"])
def test_build_in_processes_equals_sequential_build(tmp_path, monkeypatch, caplog, fmt):
    """Тот же мир world18 (CF — с расширением рядом): разбор BSL и коллекторы описателей в двух процессах дают
    то же содержимое индекса, что всё последовательно в координаторе."""
    import rlm_tools_bsl.bsl_index as BI

    root = _world_root(tmp_path, fmt)
    monkeypatch.setattr(BI, "_PARALLEL_MIN_BSL_FILES", 0)
    monkeypatch.setattr(IW, "available_cpus", lambda: 4)
    seq = _build_with(monkeypatch, root, tmp_path / "seq", "1")
    with caplog.at_level(logging.INFO, logger="rlm_tools_bsl"):
        par = _build_with(monkeypatch, root, tmp_path / "par", "2")
    assert "build workers: 2 processes" in caplog.text
    assert _content(seq) == _content(par)
    assert not list(par.parent.glob(".bsl_index.spool-*"))


def test_failed_process_start_builds_the_same_index_sequentially(tmp_path, monkeypatch, caplog):
    import rlm_tools_bsl.bsl_index as BI

    root = _world_root(tmp_path, "cf")
    monkeypatch.setattr(BI, "_PARALLEL_MIN_BSL_FILES", 0)
    monkeypatch.setattr(IW, "available_cpus", lambda: 4)
    seq = _build_with(monkeypatch, root, tmp_path / "seq", "1")

    def refuse(*_a, **_kw):
        raise OSError("CreatePipe refused")

    monkeypatch.setattr(IW, "ProcessPoolExecutor", refuse)
    with caplog.at_level(logging.WARNING, logger="rlm_tools_bsl"):
        fallback = _build_with(monkeypatch, root, tmp_path / "fallback", "4")
    assert caplog.text.count("cannot start worker processes") == 1
    assert _content(seq) == _content(fallback)


def test_parser_failure_keeps_the_previous_index(tmp_path, monkeypatch):
    """Сбой разбора — ошибка сборки ДО очистки схемы: прежний индекс цел и пригоден, spool убран."""
    import rlm_tools_bsl.bsl_index as BI

    root = _world_root(tmp_path, "edt")
    db = _build_with(monkeypatch, root, tmp_path / "idx", "1")
    before = _content(db)

    def broken(*_a, **_kw):
        raise ValueError("parser bug")

    monkeypatch.setattr(BI, "_process_single_file", broken)
    with pytest.raises(ValueError, match="parser bug"):
        BI.IndexBuilder().build(str(root))
    assert not BI.index_incomplete(db)
    assert _content(db) == before
    assert not list(db.parent.glob(".bsl_index.spool-*"))


def test_build_in_processes_from_a_background_thread(tmp_path, monkeypatch):
    """Фоновая job MCP (`rlm_index build`) собирает из НЕ главного потока: spawn-пул работает и там."""
    import threading

    import rlm_tools_bsl.bsl_index as BI

    root = _world_root(tmp_path, "cf")
    monkeypatch.setattr(BI, "_PARALLEL_MIN_BSL_FILES", 0)
    monkeypatch.setattr(IW, "available_cpus", lambda: 4)
    seq = _build_with(monkeypatch, root, tmp_path / "seq", "1")
    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "bg"))
    monkeypatch.setenv("RLM_INDEX_WORKERS", "2")
    outcome: dict = {}

    def job():
        try:
            outcome["db"] = BI.IndexBuilder().build(str(root))
        except BaseException as exc:  # noqa: BLE001
            outcome["error"] = exc

    thread = threading.Thread(target=job, name="rlm-index-job")
    thread.start()
    thread.join(timeout=300)
    assert not thread.is_alive() and "error" not in outcome, outcome
    assert _content(seq) == _content(outcome["db"])


# ═══════════════ Выборочный Git-путь: формы, права, навигация ═══════════════


def _rights_xml(*, flag: bool = False, entries: str = "") -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Rights xmlns="http://v8.1c.ru/8.2/roles">\n'
        f"<setForNewObjects>{'true' if flag else 'false'}</setForNewObjects>\n"
        "<setForAttributesByDefault>true</setForAttributesByDefault>\n"
        f"{entries}</Rights>\n"
    )


def _right_entry(obj: str, *rights: tuple[str, str]) -> str:
    body = "".join(f"<right><name>{n}</name><value>{v}</value></right>" for n, v in rights)
    return f"<object><name>{obj}</name>{body}</object>"


def _world48_files(fmt: str) -> dict[str, str]:
    """world18 + общая форма, вторая форма документа и роли (у CF — `Roles/<Р>/Ext/Rights.xml`, у EDT —
    `Roles/<Р>/Rights.rights`): мир выборочных форм и прав."""
    import test_v1_47_0 as W

    cf = fmt == "cf"
    files = dict(W._world18_files(fmt))
    form_file = "Ext/Form.xml" if cf else "Form.form"
    files[f"Documents/Док/Forms/ФормаСписка/{form_file}"] = W._small_form(cf, "Список", "CatalogRef.Номенклатура")
    files[f"CommonForms/ОбщаяФорма/{form_file}"] = W._small_form(cf, "Отбор", "DocumentRef.Док")
    if cf:
        files["CommonForms/ОбщаяФорма.xml"] = W._cf_md("CommonForm", "ОбщаяФорма")
    else:
        files["CommonForms/ОбщаяФорма/ОбщаяФорма.mdo"] = W._edt_md("CommonForm", "ОбщаяФорма")
    roles = {
        "РольА": _rights_xml(entries=_right_entry("Document.Док", ("Read", "true"), ("Update", "true"))),
        "РольБ": _rights_xml(flag=True, entries=_right_entry("Catalog.Номенклатура", ("Delete", "false"))),
        "РольВ": _rights_xml(entries=_right_entry("Catalog.Номенклатура", ("Read", "true"))),
    }
    for name, text in roles.items():
        if cf:
            files[f"Roles/{name}.xml"] = W._cf_md("Role", name)
            files[f"Roles/{name}/Ext/Rights.xml"] = text
        else:
            files[f"Roles/{name}/{name}.mdo"] = W._edt_md("Role", name)
            files[f"Roles/{name}/Rights.rights"] = text
    return files


_FORM_SQL = (
    "SELECT object_name, category, form_name, kind, scope, element_name, element_type, event, handler, data_path, "
    "main_table, attribute_is_main, extra_json, file FROM form_elements"
)
_REF_SQL = "SELECT source_object, source_category, ref_object, ref_kind, used_in, path, line FROM metadata_references"
_ROLE_SQLS = {
    "role_rights": "SELECT role_name, object_name, right_name, file FROM role_rights",
    "role_flags": "SELECT role_name, set_for_new_objects, file FROM role_flags",
    "role_exclusions": "SELECT role_name, object_name, right_name, file FROM role_exclusions",
}
_NAV_SQL = "SELECT rel_path, extension, dir_path, filename, depth, size FROM file_paths"
_META_COUNTS_SQL = (
    "SELECT key, value FROM index_meta WHERE key IN ('form_elements_count', 'has_form_elements', 'file_paths_count')"
)


def _queries_48() -> dict[str, str]:
    return {
        "form_elements": _FORM_SQL,
        "metadata_references": _REF_SQL,
        "file_paths": _NAV_SQL,
        "meta_counts": _META_COUNTS_SQL,
        **_ROLE_SQLS,
    }


class _ParseCounter:
    """Сколько форм и файлов прав РЕАЛЬНО разобрано во время ``update`` (разбор форм — ``_parse_form_root``,
    прав — ``parse_rights_for_index``; коллекторы берут их из ``bsl_xml_parsers`` в момент вызова)."""

    def __init__(self, monkeypatch) -> None:
        from rlm_tools_bsl import bsl_xml_parsers as XP

        self.forms = 0
        self.rights = 0
        real_form, real_rights = XP._parse_form_root, XP.parse_rights_for_index

        def form(text):
            self.forms += 1
            return real_form(text)

        def rights(text):
            self.rights += 1
            return real_rights(text)

        monkeypatch.setattr(XP, "_parse_form_root", form)
        monkeypatch.setattr(XP, "parse_rights_for_index", rights)


def _git_update_vs_build(
    tmp_path_factory, monkeypatch, fmt: str, files_b: dict[str, str], *, files_a=None, gitignore: str = ""
):
    """Мир world48 под git: сборка A, незакоммиченный переход в B, ``update`` (git-быстрый путь) — и чистая сборка B.
    Возвращает ``(db_update, db_fresh, update_result, counter)``."""
    import test_v1_47_0 as W
    from test_git_delta import _git_init

    from rlm_tools_bsl.bsl_index import IndexBuilder

    files_a = _world48_files(fmt) if files_a is None else files_a
    repo = tmp_path_factory.mktemp(f"g48_{fmt}") / "repo"
    base = repo / "cf"
    W._write_tree(base, files_a)
    if gitignore:
        (repo / ".gitignore").write_text(gitignore, encoding="utf-8")
    _git_init(repo)
    monkeypatch.setenv("RLM_INDEX_DIR", str(repo.parent / "idx"))
    db = IndexBuilder().build(str(base), build_calls=True)
    W._apply_changes(base, files_a, files_b)
    with monkeypatch.context() as m:
        counter = _ParseCounter(m)
        res = IndexBuilder().update(str(base))
    assert res["git_fast_path"] is True, res
    fresh = tmp_path_factory.mktemp(f"f48_{fmt}") / "repo" / "cf"
    W._write_tree(fresh, files_b)
    monkeypatch.setenv("RLM_INDEX_DIR", str(fresh.parent.parent / "idx"))
    db_fresh = IndexBuilder().build(str(fresh), build_calls=True)
    return db, db_fresh, res, counter


def _assert_same(db, db_fresh, names=None) -> None:
    import test_v1_47_0 as W

    for name, sql in _queries_48().items():
        if names is None or name in names:
            assert W._rows(db, sql) == W._rows(db_fresh, sql), name


def _form_rel(fmt: str, form_dir: str) -> str:
    return f"{form_dir}/{'Ext/Form.xml' if fmt == 'cf' else 'Form.form'}"


@pytest.mark.parametrize("fmt", ["cf", "edt"])
class TestSelectiveForms:
    def test_one_form_edit_parses_one_form(self, tmp_path_factory, monkeypatch, fmt):
        import test_v1_47_0 as W

        files_b = _world48_files(fmt)
        files_b[_form_rel(fmt, "Documents/Док/Forms/ФормаСписка")] = W._small_form(
            fmt == "cf", "Список", "DocumentRef.Док"
        )
        db, fresh, _res, counter = _git_update_vs_build(tmp_path_factory, monkeypatch, fmt, files_b)
        _assert_same(db, fresh)
        assert counter.forms == 1

    def test_add_delete_and_rename_forms(self, tmp_path_factory, monkeypatch, fmt):
        import test_v1_47_0 as W

        files_b = _world48_files(fmt)
        cf = fmt == "cf"
        # новая форма, удалённая общая форма, переименованная форма списка
        files_b[_form_rel(fmt, "DataProcessors/Обр/Forms/Вторая")] = W._small_form(cf, "Р", "CatalogRef.Номенклатура")
        del files_b[_form_rel(fmt, "CommonForms/ОбщаяФорма")]
        text = files_b.pop(_form_rel(fmt, "Documents/Док/Forms/ФормаСписка"))
        files_b[_form_rel(fmt, "Documents/Док/Forms/ФормаСпискаНовая")] = text
        db, fresh, _res, counter = _git_update_vs_build(tmp_path_factory, monkeypatch, fmt, files_b)
        _assert_same(db, fresh)
        assert counter.forms == 2  # новая и переименованная; удалённую читать нечего

    def test_common_form_edit(self, tmp_path_factory, monkeypatch, fmt):
        import test_v1_47_0 as W

        files_b = _world48_files(fmt)
        files_b[_form_rel(fmt, "CommonForms/ОбщаяФорма")] = W._small_form(
            fmt == "cf", "Отбор", "CatalogRef.Номенклатура"
        )
        db, fresh, _res, counter = _git_update_vs_build(tmp_path_factory, monkeypatch, fmt, files_b)
        _assert_same(db, fresh)
        assert counter.forms == 1

    def test_priority_switch_between_form_files(self, tmp_path_factory, monkeypatch, fmt):
        """`Form.form` и `Ext/Form.xml` в одном каталоге формы: канонический — `Form.form`; появление второго файла
        у CF (`Form.form`) меняет канонический файл каталога, у EDT (`Ext/Form.xml`) — нет."""
        import test_v1_47_0 as W

        files_b = _world48_files(fmt)
        form_dir = "Documents/Док/Forms/ФормаСписка"
        if fmt == "cf":
            files_b[f"{form_dir}/Form.form"] = W._small_form(False, "ИзEDT", "CatalogRef.Номенклатура")
        else:
            files_b[f"{form_dir}/Ext/Form.xml"] = W._small_form(True, "ИзCF", "CatalogRef.Номенклатура")
        db, fresh, _res, counter = _git_update_vs_build(tmp_path_factory, monkeypatch, fmt, files_b)
        _assert_same(db, fresh)
        assert counter.forms == 1

    def test_unrecognized_form_path_falls_back_to_all_forms(self, tmp_path_factory, monkeypatch, fmt):
        files_b = _world48_files(fmt)
        files_b["Documents/Док/Forms/ФормаСписка/Черновик.form"] = "<not-a-form/>"
        db, fresh, _res, counter = _git_update_vs_build(tmp_path_factory, monkeypatch, fmt, files_b)
        _assert_same(db, fresh)
        assert counter.forms == 4  # прежний полный проход: все четыре формы мира


@pytest.mark.parametrize("fmt", ["cf", "edt"])
class TestSelectiveRights:
    @staticmethod
    def _rights_rel(fmt: str, role: str) -> str:
        return f"Roles/{role}/Ext/Rights.xml" if fmt == "cf" else f"Roles/{role}/Rights.rights"

    def test_one_role_edit_parses_one_file_and_keeps_all_role_refs(self, tmp_path_factory, monkeypatch, fmt):
        files_b = _world48_files(fmt)
        files_b[self._rights_rel(fmt, "РольА")] = _rights_xml(
            entries=_right_entry("Document.Док", ("Read", "true"))
            + _right_entry("Catalog.Номенклатура", ("Read", "true"))
        )
        db, fresh, _res, counter = _git_update_vs_build(tmp_path_factory, monkeypatch, fmt, files_b)
        _assert_same(db, fresh)
        assert counter.rights == 1

    def test_add_and_delete_roles(self, tmp_path_factory, monkeypatch, fmt):
        import test_v1_47_0 as W

        files_b = _world48_files(fmt)
        for rel in [r for r in files_b if r.startswith("Roles/РольВ")]:
            del files_b[rel]
        files_b[self._rights_rel(fmt, "РольГ")] = _rights_xml(
            flag=True, entries=_right_entry("Document.Док", ("Post", "false"))
        )
        if fmt == "cf":
            files_b["Roles/РольГ.xml"] = W._cf_md("Role", "РольГ")
        else:
            files_b["Roles/РольГ/РольГ.mdo"] = W._edt_md("Role", "РольГ")
        db, fresh, _res, counter = _git_update_vs_build(tmp_path_factory, monkeypatch, fmt, files_b)
        _assert_same(db, fresh)
        assert counter.rights == 1

    def test_role_descriptor_edit_reads_no_rights_file(self, tmp_path_factory, monkeypatch, fmt):
        import test_v1_47_0 as W

        files_b = _world48_files(fmt)
        if fmt == "cf":
            files_b["Roles/РольА.xml"] = W._cf_md("Role", "РольА", props=W._cf_syn("Роль А"))
        else:
            files_b["Roles/РольА/РольА.mdo"] = W._edt_md("Role", "РольА", W._edt_syn("Роль А"))
        db, fresh, _res, counter = _git_update_vs_build(tmp_path_factory, monkeypatch, fmt, files_b)
        _assert_same(db, fresh)
        assert counter.rights == 0

    def test_all_roles_gone_falls_back_to_the_full_pass(self, tmp_path_factory, monkeypatch, fmt):
        files_b = {rel: text for rel, text in _world48_files(fmt).items() if not rel.startswith("Roles")}
        db, fresh, _res, _counter = _git_update_vs_build(tmp_path_factory, monkeypatch, fmt, files_b)
        _assert_same(db, fresh)


def test_navigation_rows_follow_a_metadata_change(tmp_path_factory, monkeypatch):
    import test_v1_47_0 as W

    files_b = _world48_files("cf")
    files_b["Catalogs/Новый.xml"] = W._cf_md("Catalog", "Новый")
    del files_b["CommonForms/ОбщаяФорма.xml"]
    db, fresh, _res, _counter = _git_update_vs_build(tmp_path_factory, monkeypatch, "cf", files_b)
    _assert_same(db, fresh, names={"file_paths", "meta_counts"})


def test_form_outside_the_git_delta_is_the_declared_boundary(tmp_path_factory, monkeypatch):
    """Граница Git-пути (план 4.5/6.2): форма из ``.gitignore`` правится вместе с tracked-формой — 1.47.0 перечитывал
    все формы и побочно видел обе; 1.48.0 перечитывает только форму из Git-дельты. Разность с чистой сборкой — только
    в строках этого файла (у них — состояние ДО update) и в производном ``form_elements_count``; прочие строки — Δ0."""
    import test_v1_47_0 as W

    hidden = "Documents/Док/Forms/Скрытая/Ext/Form.xml"
    files_a = _world48_files("cf")
    files_a[hidden] = W._small_form(True, "Скрыт", "CatalogRef.Номенклатура")
    files_b = dict(files_a)
    files_b[hidden] = W._small_form(True, "СкрытНовый", "DocumentRef.Док")
    files_b[_form_rel("cf", "Documents/Док/Forms/ФормаСписка")] = W._small_form(True, "Список", "DocumentRef.Док")

    db, fresh, _res, counter = _git_update_vs_build(
        tmp_path_factory, monkeypatch, "cf", files_b, files_a=files_a, gitignore="cf/Documents/Док/Forms/Скрытая/\n"
    )
    assert counter.forms == 1
    rows_update = W._rows(db, _FORM_SQL)
    rows_fresh = W._rows(fresh, _FORM_SQL)
    assert [r for r in rows_update if r[-1] != hidden] == [r for r in rows_fresh if r[-1] != hidden]
    # строки скрытой формы — прежние (состояние A), у чистой сборки — новые
    assert {r[5] for r in rows_update if r[-1] == hidden} == {"Скрыт"}
    assert {r[5] for r in rows_fresh if r[-1] == hidden} == {"СкрытНовый"}
    refs_update = [r for r in W._rows(db, _REF_SQL) if r[5] != hidden]
    assert refs_update == [r for r in W._rows(fresh, _REF_SQL) if r[5] != hidden]


# ═══════════════ Постоянные расходы update: Level-1, dirty-снимок, ANALYZE ═══════════════


def _edge_tree(root: Path) -> Path:
    """Дерево на границах shallow-обхода: Ext на глубинах 1–4, скрытые каталоги, .BSL в верхнем регистре, .mdo и
    Configuration.xml на разных глубинах, пустые каталоги, файл на глубине 4."""
    files = {
        "Configuration.xml": "<x/>",
        "Catalogs/А/Ext/ObjectModule.bsl": "",
        "Catalogs/А/Forms/Ф/Ext/Form/Module.bsl": "",
        "Catalogs/Б/Б.mdo": "<x/>",
        "Documents/Д/Ext/Module.BSL": "",
        "Documents/Д/Sub/Deep/Ext/Module.bsl": "",
        ".hidden/Ext/x.bsl": "",
        "CommonModules/М/Module.bsl": "",
        "Lvl1/Lvl2/Lvl3/Configuration.xml": "<x/>",
        "Lvl1/Lvl2/Lvl3/x.mdo": "<x/>",
    }
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    for empty in ("Roles", "Catalogs/В/Ext", "Lvl1/Lvl2/Lvl3/Ext", "Lvl1/Lvl2/Ext"):
        (root / empty).mkdir(parents=True, exist_ok=True)
    return root


@pytest.mark.parametrize("variant", ["edge", "cf", "edt", "empty_ext_depth3", "no_ext"])
def test_parallel_shallow_walk_equals_detect_format(tmp_path, variant):
    from rlm_tools_bsl.format_detector import _detect_format_listing, detect_format

    if variant == "edge":
        root = _edge_tree(tmp_path / "edge")
    elif variant in ("cf", "edt"):
        root = _world_root(tmp_path, variant)
    elif variant == "empty_ext_depth3":
        root = _world_root(tmp_path, "edt")
        (root / "Catalogs" / "Номенклатура" / "Forms" / "Ext").mkdir(parents=True, exist_ok=True)
    else:
        root = tmp_path / "plain"
        (root / "Catalogs" / "Х").mkdir(parents=True)
        (root / "Catalogs" / "Х" / "Х.mdo").write_text("<x/>", encoding="utf-8")
    assert _detect_format_listing(str(root)) == detect_format(str(root)), variant


@pytest.mark.skipif(sys.platform == "win32", reason="symlink без прав администратора на Windows не создать")
def test_parallel_shallow_walk_does_not_descend_symlinked_dirs(tmp_path):
    from rlm_tools_bsl.format_detector import _detect_format_listing, detect_format

    root = _edge_tree(tmp_path / "edge")
    target = tmp_path / "outside"
    (target / "Ext").mkdir(parents=True)
    (target / "Ext" / "m.bsl").write_text("", encoding="utf-8")
    os.symlink(target, root / "Catalogs" / "Ссылка", target_is_directory=True)
    assert _detect_format_listing(str(root)) == detect_format(str(root))


class _StatementTrace:
    """SQL всех соединений, открытых через ``sqlite3.connect`` за время теста."""

    def __init__(self, monkeypatch) -> None:
        import sqlite3

        self.statements: list[str] = []
        real = sqlite3.connect

        def connect(*args, **kwargs):
            conn = real(*args, **kwargs)
            conn.set_trace_callback(self.statements.append)
            return conn

        monkeypatch.setattr(sqlite3, "connect", connect)


def _git_world_built(tmp_path_factory, monkeypatch, *, index_inside_repo: bool = False):
    import test_v1_47_0 as W
    from test_git_delta import _git_init

    from rlm_tools_bsl.bsl_index import IndexBuilder

    repo = tmp_path_factory.mktemp("g48c") / "repo"
    base = repo / "cf"
    files = _world48_files("cf")
    W._write_tree(base, files)
    _git_init(repo)
    index_dir = (repo / "cf" / ".idx") if index_inside_repo else (repo.parent / "idx")
    monkeypatch.setenv("RLM_INDEX_DIR", str(index_dir))
    db = IndexBuilder().build(str(base), build_calls=True)
    return base, files, db


def _meta_value(db, key: str):
    import sqlite3

    con = sqlite3.connect(str(db))
    try:
        row = con.execute("SELECT value FROM index_meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None
    finally:
        con.close()


@pytest.mark.parametrize("inside", [False, True])
def test_git_update_reuses_the_dirty_snapshot_taken_at_start(tmp_path_factory, monkeypatch, inside):
    """Рабочие команды Git (staged/unstaged/untracked) уже выполнены в начале git-быстрого пути; при неизменных
    исходниках конечный снимок — тот же набор. Каталог индекса ВНУТРИ рабочего дерева — прежний повторный вызов
    (файлы БД сами стали бы untracked)."""
    import json

    import test_v1_47_0 as W

    import rlm_tools_bsl.bsl_index as BI

    base, files, db = _git_world_built(tmp_path_factory, monkeypatch, index_inside_repo=inside)
    files_b = dict(files)
    bsl = next(rel for rel in files if rel.endswith(".bsl"))
    files_b[bsl] = files[bsl] + "\n// правка\n"
    files_b["Catalogs/Черновик.xml"] = W._cf_md("Catalog", "Черновик")  # untracked
    W._apply_changes(base, files, files_b)
    calls: list[str] = []
    real = BI._git_current_dirty

    def spy(base_path, prefix):
        calls.append(base_path)
        return real(base_path, prefix)

    monkeypatch.setattr(BI, "_git_current_dirty", spy)
    res = BI.IndexBuilder().update(str(base))
    assert res["git_fast_path"] is True
    assert (len(calls) == 1) is inside
    stored = set(json.loads(_meta_value(db, "git_dirty_paths")))
    if not inside:  # внутри дерева файлы самой БД меняются после снимка — сверять не с чем
        assert stored == real(str(base), "cf").paths
    assert {bsl, "Catalogs/Черновик.xml"} <= stored
    assert _meta_value(db, "git_dirty_unreliable") == "0"


def test_git_noop_update_skips_analyze(tmp_path_factory, monkeypatch):
    import rlm_tools_bsl.bsl_index as BI

    base, files, db = _git_world_built(tmp_path_factory, monkeypatch)
    trace = _StatementTrace(monkeypatch)
    res = BI.IndexBuilder().update(str(base))
    assert res["git_fast_path"] is True and (res["added"], res["changed"], res["removed"]) == (0, 0, 0)
    assert not [s for s in trace.statements if s.strip().upper() == "ANALYZE"]


def test_git_update_with_changes_keeps_analyze(tmp_path_factory, monkeypatch):
    import test_v1_47_0 as W

    import rlm_tools_bsl.bsl_index as BI

    base, files, db = _git_world_built(tmp_path_factory, monkeypatch)
    files_b = dict(files)
    bsl = next(rel for rel in files if rel.endswith(".bsl"))
    files_b[bsl] = files[bsl] + "\n// правка\n"
    W._apply_changes(base, files, files_b)
    trace = _StatementTrace(monkeypatch)
    res = BI.IndexBuilder().update(str(base))
    assert res["git_fast_path"] is True and res["changed"] == 1
    assert [s for s in trace.statements if s.strip().upper() == "ANALYZE"]


# ═══════════════ Полный скан: снимок входов с хешами ═══════════════


class _CollectorCounter:
    """Сколько раз полный скан звал коллекторы доменов описателей (в координаторе — последовательный режим)."""

    def __init__(self, monkeypatch) -> None:
        import rlm_tools_bsl.bsl_index as BI
        from rlm_tools_bsl import extension_detector as ED

        self.calls: dict[str, int] = {}
        for name in (
            "_collect_metadata_tables",
            "_collect_form_elements",
            "_collect_object_synonyms",
            "_rights_batch_data",
        ):
            real = getattr(BI, name)

            def spy(*args, _real=real, _name=name, **kwargs):
                self.calls[_name] = self.calls.get(_name, 0) + 1
                return _real(*args, **kwargs)

            monkeypatch.setattr(BI, name, spy)
        real_ext = ED.find_extension_overrides

        def spy_ext(*args, **kwargs):
            self.calls["find_extension_overrides"] = self.calls.get("find_extension_overrides", 0) + 1
            return real_ext(*args, **kwargs)

        monkeypatch.setattr(ED, "find_extension_overrides", spy_ext)


def _full_scan_world(tmp_path_factory, monkeypatch, fmt: str = "cf", extra: dict[str, str] | None = None):
    """Мир world48 вне git (полный скан), собранный; у CF — расширение `Расш1` рядом; ``extra`` — файлы сверх мира."""
    import test_v1_47_0 as W

    from rlm_tools_bsl.bsl_index import IndexBuilder

    root = tmp_path_factory.mktemp(f"fs48_{fmt}") / "repo"
    base = root / "cf"
    files = {**_world48_files(fmt), **(extra or {})}
    W._write_tree(base, files)
    monkeypatch.setenv("RLM_INDEX_DIR", str(root.parent / "idx"))
    db = IndexBuilder().build(str(base), build_calls=True)
    return base, files, db


def _same_bytes_edit(path: Path, old: str, new: str) -> None:
    """Правка равной длины с прежним mtime: размер и время файла те же — 1.47.0 перечитывал такой описатель
    безусловно, 1.48.0 видит правку только по хешу содержимого."""
    text = path.read_text(encoding="utf-8")
    assert old in text and len(old.encode("utf-8")) == len(new.encode("utf-8")), (old, new)
    st = path.stat()
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))


def _fresh_build(tmp_path_factory, monkeypatch, files: dict[str, str], tree_root: Path):
    """Чистая сборка дерева в том же виде, что на диске (``tree_root`` — копия дерева)."""
    import shutil

    from rlm_tools_bsl.bsl_index import IndexBuilder

    fresh = tmp_path_factory.mktemp("fs48_fresh") / "repo"
    shutil.copytree(tree_root.parent, fresh)
    monkeypatch.setenv("RLM_INDEX_DIR", str(fresh.parent / "idx"))
    return IndexBuilder().build(str(fresh / tree_root.name), build_calls=True)


def _update_queries_equal(db, fresh) -> None:
    import test_v1_47_0 as W

    queries = dict(_queries_48())
    queries["object_attributes"] = (
        "SELECT object_name, category, attr_name, attr_synonym, attr_type, attr_kind, ts_name, source_file "
        "FROM object_attributes"
    )
    queries["object_synonyms"] = "SELECT object_name, category, synonym, file FROM object_synonyms"
    queries["extension_overrides"] = (
        "SELECT object_name, source_path, target_method, target_method_line, annotation, extension_name, "
        "extension_method, ext_module_path, ext_line, form_item, form_event FROM extension_overrides"
    )
    for name, sql in queries.items():
        if name == "file_paths":
            continue  # другое дерево — другие mtime; навигацию сверяет A/B-инструмент
        assert W._rows(db, sql) == W._rows(fresh, sql), name


class TestInputSnapshot:
    def test_build_publishes_a_snapshot_bound_to_the_index(self, tmp_path_factory, monkeypatch):
        from rlm_tools_bsl import _index_snapshot as SNAP

        _base, _files, db = _full_scan_world(tmp_path_factory, monkeypatch)
        snap = SNAP.load_snapshot(db)
        assert snap is not None
        assert snap["built_at"] == _meta_value(db, "built_at")
        assert set(snap["domains"]) >= {"descriptors", "forms", "rights", "synonyms", "overrides"}

    def test_bsl_only_full_scan_skips_every_descriptor_domain(self, tmp_path_factory, monkeypatch):
        import test_v1_47_0 as W

        from rlm_tools_bsl.bsl_index import IndexBuilder

        base, files, db = _full_scan_world(tmp_path_factory, monkeypatch)
        files_b = dict(files)
        bsl = next(rel for rel in files if rel.endswith(".bsl") and "CommonModules" in rel)
        files_b[bsl] = files[bsl] + "\nПроцедура Новая() Экспорт\nКонецПроцедуры\n"
        W._apply_changes(base, files, files_b)
        with monkeypatch.context() as m:
            counter = _CollectorCounter(m)
            res = IndexBuilder().update(str(base))
        assert res["git_fast_path"] is False and res["changed"] == 1
        # перехваты зависят от модулей основной — при BSL-дельте перечитываются, как прежде
        assert counter.calls == {"find_extension_overrides": 1}
        _update_queries_equal(db, _fresh_build(tmp_path_factory, monkeypatch, files_b, base))

    def test_noop_full_scan_reads_no_collector_and_republishes(self, tmp_path_factory, monkeypatch):
        from rlm_tools_bsl import _index_snapshot as SNAP
        from rlm_tools_bsl.bsl_index import IndexBuilder

        base, files, db = _full_scan_world(tmp_path_factory, monkeypatch)
        with monkeypatch.context() as m:
            counter = _CollectorCounter(m)
            IndexBuilder().update(str(base))
        assert counter.calls == {}
        assert SNAP.load_snapshot(db)["built_at"] == _meta_value(db, "built_at")

    @pytest.mark.parametrize("target", ["form", "rights", "descriptor", "extension"])
    def test_same_size_same_mtime_edit_is_seen_by_hash(self, tmp_path_factory, monkeypatch, target):
        """Правка равной длины с прежним mtime — Δ0 с чистой сборкой: хеш содержимого доказывает изменение."""
        from rlm_tools_bsl.bsl_index import IndexBuilder

        base, files, db = _full_scan_world(tmp_path_factory, monkeypatch)
        if target == "form":
            _same_bytes_edit(base / "Documents/Док/Forms/ФормаСписка/Ext/Form.xml", "Номенклатура", "Номенклатурб")
            expect = "_collect_form_elements"
        elif target == "rights":
            _same_bytes_edit(base / "Roles/РольВ/Ext/Rights.xml", "Номенклатура", "Номенклатурб")
            expect = "_rights_batch_data"
        elif target == "descriptor":
            _same_bytes_edit(base / "CommonForms/ОбщаяФорма.xml", "ОбщаяФорма", "ОбщаяФормб")
            expect = "_collect_metadata_tables"
        else:
            ext_form = base.parent / "cfe/Расш1/Documents/Док/Forms/ФормаДокумента/Ext/Form.xml"
            _same_bytes_edit(ext_form, "Расш1_ПриСозданииПосле<", "Расш1_ПриСозданииПослХ<")
            expect = "find_extension_overrides"
        with monkeypatch.context() as m:
            counter = _CollectorCounter(m)
            IndexBuilder().update(str(base))
        assert expect in counter.calls, counter.calls
        _update_queries_equal(db, _fresh_build(tmp_path_factory, monkeypatch, files, base))

    @pytest.mark.parametrize("damage", ["missing", "corrupt", "foreign_built_at"])
    def test_unusable_snapshot_means_the_full_old_scan(self, tmp_path_factory, monkeypatch, damage):
        import gzip
        import json

        from rlm_tools_bsl import _index_snapshot as SNAP
        from rlm_tools_bsl.bsl_index import IndexBuilder

        base, files, db = _full_scan_world(tmp_path_factory, monkeypatch)
        path = SNAP.snapshot_path(db)
        if damage == "missing":
            path.unlink()
        elif damage == "corrupt":
            path.write_bytes(b"not gzip")
        else:
            snap = SNAP.load_snapshot(db)
            snap["built_at"] = "1.0"
            path.write_bytes(gzip.compress(json.dumps(snap).encode("utf-8")))
        with monkeypatch.context() as m:
            counter = _CollectorCounter(m)
            IndexBuilder().update(str(base))
        assert {"_collect_metadata_tables", "_collect_form_elements", "_collect_object_synonyms"} <= set(counter.calls)
        assert SNAP.load_snapshot(db)["built_at"] == _meta_value(db, "built_at")

    def test_git_update_removes_the_snapshot(self, tmp_path_factory, monkeypatch):
        import test_v1_47_0 as W

        from rlm_tools_bsl import _index_snapshot as SNAP
        from rlm_tools_bsl.bsl_index import IndexBuilder

        base, files, db = _git_world_built(tmp_path_factory, monkeypatch)
        assert SNAP.load_snapshot(db) is not None
        files_b = dict(files)
        files_b[_form_rel("cf", "Documents/Док/Forms/ФормаСписка")] = W._small_form(True, "Список", "DocumentRef.Док")
        W._apply_changes(base, files, files_b)
        assert IndexBuilder().update(str(base))["git_fast_path"] is True
        assert SNAP.load_snapshot(db) is None and not SNAP.snapshot_path(db).exists()

    def test_snapshot_that_cannot_be_removed_fails_before_any_write(self, tmp_path_factory, monkeypatch):
        from rlm_tools_bsl import _index_snapshot as SNAP
        from rlm_tools_bsl.bsl_index import IndexBuilder

        base, files, db = _full_scan_world(tmp_path_factory, monkeypatch)
        before = _content(db)
        built_at = _meta_value(db, "built_at")
        real_unlink = Path.unlink

        def locked(self, *args, **kwargs):
            if self.name == SNAP.SNAPSHOT_NAME:
                raise PermissionError(13, "locked")
            return real_unlink(self, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", locked)
        with pytest.raises(RuntimeError, match="snapshot"):
            IndexBuilder().update(str(base))
        monkeypatch.setattr(Path, "unlink", real_unlink)
        assert _meta_value(db, "built_at") == built_at
        assert _content(db) == before

    def test_interrupted_scan_then_revert_to_snapshot_sources(self, tmp_path_factory, monkeypatch):
        """План 4.5: полный скан фиксирует часть изменений раньше ``built_at``; прерванный update и возврат исходника
        к версии снимка — повторный update обязан исправить строки, а не принять их «неизменными» по хешу."""
        import test_v1_47_0 as W

        import rlm_tools_bsl.bsl_index as BI
        from rlm_tools_bsl.bsl_index import IndexBuilder

        base, files, db = _full_scan_world(tmp_path_factory, monkeypatch)
        files_b = dict(files)
        rights_rel = "Roles/РольА/Ext/Rights.xml"
        files_b[rights_rel] = _rights_xml(entries=_right_entry("Catalog.Номенклатура", ("Read", "true")))
        W._apply_changes(base, files, files_b)

        def boom(*_a, **_kw):
            raise RuntimeError("interrupted after partial commit")

        with monkeypatch.context() as m:
            m.setattr(BI, "_collect_extension_overrides", boom)
            with pytest.raises(RuntimeError, match="interrupted"):
                IndexBuilder().update(str(base))
        W._apply_changes(base, files_b, files)
        IndexBuilder().update(str(base))
        _update_queries_equal(db, _fresh_build(tmp_path_factory, monkeypatch, files, base))

    @pytest.mark.parametrize(
        "rel", ["CommonTemplates/Template.xml", "Documents/Док/Templates/Template.xml", "Catalogs/Template.xml"]
    )
    def test_descriptor_named_template_is_seen_by_hash(self, tmp_path_factory, monkeypatch, rel):
        """Описатель с именем файла `Template.xml` — общий макет, макет объекта, объект с именем `Template` — вход
        коллектора метаданных, а не содержимое макета: его правка перечитывает метаданные, итог — как у чистой
        сборки."""
        import test_v1_47_0 as W

        from rlm_tools_bsl.bsl_index import IndexBuilder

        if rel == "Catalogs/Template.xml":
            text = W._cf_obj(
                "Catalog",
                "Template",
                children=[W._cf_child("Attribute", "Ссылка", types=["cfg:CatalogRef.Номенклатура"])],
            )
            old, new = "CatalogRef.Номенклатура", "DocumentRef.Док"
        else:
            kind = "CommonTemplate" if rel.startswith("Common") else "Template"
            text = W._cf_md(kind, "Template", props="<TemplateType>SpreadsheetDocument</TemplateType>")
            old, new = "SpreadsheetDocument", "TextDocument"
        base, files, db = _full_scan_world(tmp_path_factory, monkeypatch, extra={rel: text})
        files_b = dict(files)
        files_b[rel] = text.replace(old, new)
        W._apply_changes(base, files, files_b)
        with monkeypatch.context() as m:
            counter = _CollectorCounter(m)
            res = IndexBuilder().update(str(base))
        assert res["git_fast_path"] is False
        assert "_collect_metadata_tables" in counter.calls, counter.calls
        fresh = _fresh_build(tmp_path_factory, monkeypatch, files_b, base)
        _update_queries_equal(db, fresh)
        templates = "SELECT owner_ref, name, synonym, template_type, file FROM templates"
        assert W._rows(db, templates) == W._rows(fresh, templates)

    def test_metadata_rescan_keeps_form_and_role_refs_of_skipped_domains(self, tmp_path_factory, monkeypatch):
        """Перечитаны только метаданные (DELETE всех строк metadata_references) — ссылки форм и прав, чьи домены
        пропущены, возвращаются из сохранённых строк и итоговой role_rights."""
        import test_v1_47_0 as W

        from rlm_tools_bsl.bsl_index import IndexBuilder

        base, files, db = _full_scan_world(tmp_path_factory, monkeypatch)
        files_b = dict(files)
        files_b["Catalogs/Новый.xml"] = W._cf_md("Catalog", "Новый")
        W._apply_changes(base, files, files_b)
        with monkeypatch.context() as m:
            counter = _CollectorCounter(m)
            IndexBuilder().update(str(base))
        assert "_collect_metadata_tables" in counter.calls
        assert "_collect_form_elements" not in counter.calls and "_rights_batch_data" not in counter.calls
        _update_queries_equal(db, _fresh_build(tmp_path_factory, monkeypatch, files_b, base))

    @pytest.mark.parametrize("change", ["empty_ext_dir", "root_roles_dir_removed"])
    def test_discovery_directories_are_part_of_the_inputs(self, tmp_path_factory, monkeypatch, change):
        import shutil

        from rlm_tools_bsl.bsl_index import IndexBuilder

        base, files, db = _full_scan_world(tmp_path_factory, monkeypatch, fmt="edt")
        if change == "empty_ext_dir":
            (base / "Catalogs" / "Номенклатура" / "Ext").mkdir(parents=True, exist_ok=True)
            files_now = files
        else:
            shutil.rmtree(base / "Roles")
            files_now = {rel: text for rel, text in files.items() if not rel.startswith("Roles")}
        IndexBuilder().update(str(base))
        fresh = _fresh_build(tmp_path_factory, monkeypatch, files_now, base)
        _update_queries_equal(db, fresh)
        assert _meta_value(db, "source_format") == _meta_value(fresh, "source_format")


def test_cli_drop_removes_the_input_snapshot_with_the_index(tmp_path_factory, monkeypatch, capsys):
    from rlm_tools_bsl import _index_snapshot as SNAP
    from rlm_tools_bsl import cli

    base, _files, db = _full_scan_world(tmp_path_factory, monkeypatch)
    assert SNAP.snapshot_path(db).exists()
    monkeypatch.setattr(sys, "argv", ["rlm-bsl-index", "index", "drop", str(base)])
    cli.main()
    assert not db.exists() and not SNAP.snapshot_path(db).exists()
    assert not db.parent.exists()


def test_mcp_drop_removes_the_input_snapshot_with_the_index(tmp_path_factory, monkeypatch):
    import json

    from rlm_tools_bsl import _index_snapshot as SNAP
    from rlm_tools_bsl.server import _rlm_index

    base, _files, db = _full_scan_world(tmp_path_factory, monkeypatch)
    out = json.loads(_rlm_index(action="drop", path=str(base)))
    assert out.get("action") == "drop", out
    assert not db.exists() and not SNAP.snapshot_path(db).exists()


def test_glob_xml_categories_match_the_metadata_collector():
    """Снимок хеширует ``Template.xml`` в поддеревьях, которые `_collect_metadata_tables` читает целиком
    (`_glob_xml`): новая такая категория без правки списка оставила бы её чтения вне снимка."""
    import inspect

    import rlm_tools_bsl.bsl_index as BI

    source = inspect.getsource(BI._collect_metadata_tables)
    called = set(re.findall(r'_glob_xml\("([A-Za-z]+)"\)', source))
    assert called == set(BI._GLOB_XML_CATEGORIES)
    assert "rglob" in source and source.count("def _glob_xml") == 1


@pytest.mark.parametrize(
    ("rel", "hashed"),
    [
        # содержимое макетов: коллекторы его не читают (кроме объекта без описателя — `_payloads_read_as_descriptor`)
        ("Documents/Док/Templates/Макет/Ext/Template.xml", False),
        ("CommonTemplates/Макет/Ext/Template.xml", False),
        ("Documents/Док/Templates/Template/Ext/Template.xml", False),
        # описатели с именем файла `Template.xml`: макет, общий макет, объект, команда, форма с именем `Template`
        ("Documents/Док/Templates/Template.xml", True),
        ("CommonTemplates/Template.xml", True),
        ("Catalogs/Template.xml", True),
        ("Documents/Док/Commands/Template.xml", True),
        ("CommonCommands/Template.xml", True),
        ("DefinedTypes/Template.xml", True),
        ("Documents/Док/Forms/Template.xml", True),
        # поддеревья, которые коллектор читает целиком
        ("Subsystems/Продажи/Ext/Template.xml", True),
    ],
)
def test_snapshot_hashes_every_descriptor_but_template_content(rel, hashed):
    import rlm_tools_bsl.bsl_index as BI

    assert BI._descriptor_hash_rule(rel) is hashed


# ═══════════════ CPU: кеш канонизации, общие маски модуля ═══════════════


_CANON_INPUTS = [
    "",
    " ",
    "cfg:CatalogRef.Контрагенты",
    "CatalogRef.Контрагенты",
    "catalogref.Контрагенты",
    "DocumentObject.Заказ",
    "document.Заказ",
    "Document.Заказ.TabularSection.Товары",
    "EnumRef.СтавкиНДС",
    "DefinedType.Сумма",
    "xs:string",
    "String",
    "d4p1:DocumentRef.Заказ",
    "  cfg:InformationRegisterRecordSet.Цены  ",
    "Справочник.Номенклатура",
    "Catalog",
    ".X",
    "cfg:.X",
]


@pytest.mark.parametrize("fold_case", [True, False])
def test_canonicalization_cache_returns_the_uncached_answer(fold_case):
    from rlm_tools_bsl import bsl_xml_parsers as XP

    XP._canonicalize_type_ref_cached.cache_clear()
    for text in _CANON_INPUTS * 2:
        assert XP.canonicalize_type_ref(text, fold_case=fold_case) == XP._canonicalize_type_ref_impl(
            text, fold_case=fold_case
        ), text
    info = XP._canonicalize_type_ref_cached.cache_info()
    assert info.hits >= len(_CANON_INPUTS) and info.currsize <= XP._CANONICALIZE_CACHE_SIZE


def test_canonicalization_cache_keeps_the_contract_for_non_strings():
    from rlm_tools_bsl import bsl_xml_parsers as XP

    assert XP.canonicalize_type_ref(None) == ""
    assert XP.canonicalize_type_ref([]) == ""
    with pytest.raises(AttributeError):
        XP.canonicalize_type_ref(["Catalog.X"])
    with pytest.raises(AttributeError):
        XP.canonicalize_type_ref(12)


_DOC_OBJECT_MODULE = """\
// Движения.ЗакомментированныйРегистр
Процедура ОбработкаПроведения(Отказ, Режим)
    Движения.ТоварыНаСкладах.Записывать = Истина;
    Текст = "Движения.ВСтроке";
    ПроведениеСервер.ОтразитьДвиженияПоРегистру(ДополнительныеСвойства, Движения, "Продажи");
    ПроведениеСервер.ОтразитьДвиженияПоРегистру(ДополнительныеСвойства, Движения, ИмяРегистра);
    Запрос = "ВЫБРАТЬ
    |// не комментарий
    |Движения.Внутри";
КонецПроцедуры
"""
_DOC_MANAGER_MODULE = """\
Функция ТекстЗапросаТаблицаТоварыНаСкладах()
КонецФункции
Процедура ДобавитьМеханизмы(МеханизмыДокумента) Экспорт
    МеханизмыДокумента.Добавить("Взаиморасчеты");
    // МеханизмыДокумента.Добавить("Закомментирован");
    Набор = РегистрыНакопления.Продажи.СоздатьНаборЗаписей();
    Набор2 = РегистрыНакопления[Имя].СоздатьНаборЗаписей();
    ТаблицыДляДвижений.Вставить("ТоварыНаСкладах", Т);
КонецПроцедуры
"""


@pytest.mark.parametrize(
    "module_type,text",
    [("ObjectModule", _DOC_OBJECT_MODULE), ("ManagerModule", _DOC_MANAGER_MODULE)],
    ids=["object", "manager"],
)
def test_movements_with_ready_masks_equal_movements_from_scratch(module_type, text):
    import rlm_tools_bsl.bsl_index as BI
    from rlm_tools_bsl.bsl_knowledge import mask_comments_and_strings
    from rlm_tools_bsl.format_detector import BslFileInfo

    rel = f"Documents/Док/Ext/{module_type}.bsl"
    info = BslFileInfo(rel, "Documents", "Док", module_type, None, None, False)
    lines = text.splitlines()
    masked = mask_comments_and_strings(lines)
    comments_only = "\n".join(mask_comments_and_strings(lines, keep_string_content=True))
    masks = ("\n".join(masked), comments_only)
    plain = BI._extract_movements(text, info, rel)
    assert plain and BI._extract_movements(text, info, rel, masked=masked) == plain
    assert BI._extract_movements(text, info, rel, masked=masked, comments_only=comments_only) == plain
    if module_type == "ObjectModule":
        assert BI._register_by_name_refs(text, masks=masks) == BI._register_by_name_refs(text)


def test_failed_build_leaves_no_input_walk_running(tmp_path, monkeypatch):
    """Сборка упала — фоновая опись для снимка остановлена и дождана (файлы ею не открыты), снимка нет."""
    import threading

    import rlm_tools_bsl.bsl_index as BI
    from rlm_tools_bsl import _index_snapshot as SNAP

    root = _world_root(tmp_path, "cf")

    def broken(*_a, **_kw):
        raise ValueError("parser bug")

    monkeypatch.setattr(BI, "_process_single_file", broken)
    monkeypatch.setattr(BI, "_large_export", lambda _base_path: True)  # фоновая опись — у крупной выгрузки
    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
    with pytest.raises(ValueError, match="parser bug"):
        BI.IndexBuilder().build(str(root))
    assert not [t for t in threading.enumerate() if t.name.startswith("rlm-index-inputs") and t.is_alive()]
    assert SNAP.load_snapshot(BI.get_index_db_path(str(root))) is None


# ═══════════════ Находки ревью: пул на no-op, пакет воркеров, общий бюджет, Roles, отпечаток кода ═══════════════


def test_noop_full_scan_starts_no_process_pool(tmp_path_factory, monkeypatch):
    """План 6.4: no-op не запускает дорогой пул процессов — при пропущенных доменах и пустой BSL-дельте остаются две
    дешёвые задачи (навигация, уровень конфигурации), их пул не оправдывает."""
    import rlm_tools_bsl.bsl_index as BI

    base, _files, _db = _full_scan_world(tmp_path_factory, monkeypatch)
    monkeypatch.setattr(BI, "_PARALLEL_MIN_BSL_FILES", 0)
    monkeypatch.setattr(IW, "available_cpus", lambda: 4)
    monkeypatch.setenv("RLM_INDEX_WORKERS", "2")
    started: list[int] = []
    real = IW.ProcessPoolExecutor

    def spy(*args, **kwargs):
        started.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(IW, "ProcessPoolExecutor", spy)
    BI.IndexBuilder().update(str(base))
    assert started == []


def test_full_scan_with_collectors_to_run_still_uses_processes(tmp_path_factory, monkeypatch):
    import rlm_tools_bsl.bsl_index as BI
    from rlm_tools_bsl import _index_snapshot as SNAP

    base, _files, db = _full_scan_world(tmp_path_factory, monkeypatch)
    SNAP.snapshot_path(db).unlink()  # без снимка коллекторы перечитывают все домены
    monkeypatch.setattr(BI, "_PARALLEL_MIN_BSL_FILES", 0)
    monkeypatch.setattr(IW, "available_cpus", lambda: 4)
    monkeypatch.setenv("RLM_INDEX_WORKERS", "2")
    started: list[int] = []
    real = IW.ProcessPoolExecutor

    def spy(*args, **kwargs):
        started.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(IW, "ProcessPoolExecutor", spy)
    BI.IndexBuilder().update(str(base))
    assert started == [1]


def test_workers_with_another_package_copy_are_not_used(monkeypatch, caplog):
    """План, этапы 2 и 6: пакет воркера обязан быть тем же, что у координатора; иначе — последовательно."""
    monkeypatch.setattr(IW, "_coordinator_module", lambda: str(Path("Z:/other/copy/rlm_tools_bsl/bsl_index.py")))
    with caplog.at_level(logging.WARNING, logger="rlm_tools_bsl._index_workers"):
        with IW.IndexTaskPool(2, label="test") as pool:
            assert pool.result(pool.submit(TASKS.worker_pid)) == os.getpid()
            assert not pool.parallel
    assert "differs" in caplog.text


class TestSharedWorkerBudget:
    """План 4.1: сборки разных индексов в одном процессе делят бюджет процессов, а не берут по полному пулу."""

    def test_second_pool_gets_what_is_left(self, monkeypatch):
        monkeypatch.setattr(IW, "available_cpus", lambda: 4)
        monkeypatch.setenv("RLM_INDEX_WORKERS", "4")
        first = IW.IndexTaskPool(4, label="a")
        second = IW.IndexTaskPool(4, label="b")
        try:
            assert first.workers == 4 and first.parallel
            assert second.workers == 1 and not second.parallel
        finally:
            second.close()
            first.close()
        third = IW.IndexTaskPool(4, label="c")
        try:
            assert third.workers == 4
        finally:
            third.close()

    def test_budget_is_split_not_exceeded(self, monkeypatch):
        monkeypatch.setattr(IW, "available_cpus", lambda: 6)
        monkeypatch.setenv("RLM_INDEX_WORKERS", "6")
        first = IW.IndexTaskPool(4, label="a")
        second = IW.IndexTaskPool(4, label="b")
        try:
            assert (first.workers, second.workers) == (4, 2)
        finally:
            first.close()
            second.close()

    def test_fallback_returns_the_budget(self, monkeypatch):
        monkeypatch.setattr(IW, "available_cpus", lambda: 4)
        monkeypatch.setenv("RLM_INDEX_WORKERS", "4")

        def refuse(*_a, **_kw):
            raise OSError("no pipes")

        monkeypatch.setattr(IW, "ProcessPoolExecutor", refuse)
        pool = IW.IndexTaskPool(4, label="a")
        pool.result(pool.submit(TASKS.worker_pid))
        assert not pool.parallel
        other = IW.IndexTaskPool(4, label="b")
        try:
            assert other.workers == 4
        finally:
            other.close()
            pool.close()


def test_main_hiding_from_two_threads_restores_main(monkeypatch):
    """Две операции в одном сервере запускают процессы из разных потоков: первое окно скрытия закрывается раньше
    второго — после обоих ``__main__`` такой, как был (вторая не сохраняет уже скрытые атрибуты)."""
    import threading

    main = sys.modules["__main__"]
    spec = types.SimpleNamespace(name="coordinator")
    monkeypatch.setattr(main, "__file__", "coordinator.py", raising=False)
    monkeypatch.setattr(main, "__spec__", spec, raising=False)
    first_in, second_in, first_out = threading.Event(), threading.Event(), threading.Event()

    def first() -> None:
        with IW._without_parent_main():
            first_in.set()
            second_in.wait(timeout=0.5)  # окна по очереди — второе войдёт только после выхода первого
        first_out.set()

    def second() -> None:
        first_in.wait(timeout=10)
        with IW._without_parent_main():
            second_in.set()
            first_out.wait(timeout=10)

    threads = [threading.Thread(target=first), threading.Thread(target=second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert main.__dict__.get("__file__") == "coordinator.py"
    assert main.__dict__.get("__spec__") is spec


def test_code_fingerprint_without_sources_is_unique_per_process(monkeypatch):
    """Пакет без ``.py`` (сборка из ``.pyc``, zip): отпечаток не константа — снимок другого кода не подходит."""
    import hashlib

    from rlm_tools_bsl import _index_snapshot as SNAP

    assert SNAP._sources_fingerprint(Path(SNAP.__file__).resolve().parent) is not None
    assert SNAP._sources_fingerprint(Path(SNAP.__file__).resolve().parent / "__no_such_dir__") is None
    monkeypatch.setattr(SNAP, "_sources_fingerprint", lambda _package: None)
    monkeypatch.setattr(SNAP, "_code_fingerprint", None)
    first = SNAP.code_fingerprint()
    assert SNAP.code_fingerprint() == first  # в одном процессе — один
    monkeypatch.setattr(SNAP, "_code_fingerprint", None)  # как в новом процессе
    second = SNAP.code_fingerprint()
    assert first and second and first != second
    assert hashlib.sha256().hexdigest() not in (first, second)


def _case_insensitive_fs(directory: Path) -> bool:
    probe = directory / "CaseProbe"
    probe.write_text("", encoding="utf-8")
    try:
        return (directory / "caseprobe").exists()
    finally:
        probe.unlink()


def _rename_case(path: Path, new_name: str) -> None:
    temp = path.with_name(path.name + "_case_tmp")
    path.rename(temp)
    temp.rename(path.with_name(new_name))


_ROLE_QUERIES = {"role_rights", "role_flags", "role_exclusions", "metadata_references"}


def _case_world(
    tmp_path_factory, monkeypatch, fmt: str = "cf", extra: dict[str, str] | None = None
) -> tuple[Path, dict[str, str]]:
    """Мир world48 (и ``extra``) под Git, `core.ignorecase` — переименование каталога только регистром ЛАТИНИЦЫ Git
    не видит; смену регистра кириллицы он видит сам (новое написание — untracked, оба пути приходят в дельту)."""
    import test_v1_47_0 as W
    from test_git_delta import _git, _git_init

    files = {**_world48_files(fmt), **(extra or {})}
    repo = tmp_path_factory.mktemp("case") / "repo"
    if not _case_insensitive_fs(repo.parent):
        pytest.skip("регистр имён различает сама файловая система: сценарий только для Windows/macOS")
    base = repo / "cf"
    W._write_tree(base, files)
    _git_init(repo)
    _git(repo, "config", "core.ignorecase", "true")
    monkeypatch.setenv("RLM_INDEX_DIR", str(repo.parent / "idx"))
    return base, files


def _edit_rights(rights: Path) -> None:
    rights.write_text(_rights_xml(entries=_right_entry("Catalog.Номенклатура", ("Read", "true"))), encoding="utf-8")
    st = rights.stat()
    os.utime(rights, (st.st_atime + 10, st.st_mtime + 10))


def _latin_role(fmt: str) -> dict[str, str]:
    """Роль с латиницей в имени (права — не те, что пишет ``_edit_rights``)."""
    import test_v1_47_0 as W

    text = _rights_xml(entries=_right_entry("Document.Док", ("Read", "true")))
    if fmt == "cf":
        return {"Roles/РольERP.xml": W._cf_md("Role", "РольERP"), "Roles/РольERP/Ext/Rights.xml": text}
    return {"Roles/РольERP/РольERP.mdo": W._edt_md("Role", "РольERP"), "Roles/РольERP/Rights.rights": text}


def _rename_between_operations(base: Path, renamed: str, other: str, when: str, build) -> Path:
    """Переименовать каталог ``renamed`` только регистром (в ``other``): ``restored`` — до сборки и обратно после неё
    (строки индекса — со старым написанием, диск и Git снова совпадают), ``after`` — после сборки (строки — с написанием
    Git, на диске — новое). Возвращает путь БД сборки."""
    head, _, name = renamed.rpartition("/")
    if when == "restored":
        _rename_case(base / renamed, other)
    db = build()
    if when == "restored":
        _rename_case(base / head / other, name)
    else:
        _rename_case(base / renamed, other)
    return db


def _write_edit(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    st = path.stat()
    os.utime(path, (st.st_atime + 10, st.st_mtime + 10))


@pytest.mark.parametrize("fmt", ["cf", "edt"])
@pytest.mark.parametrize("when", ["restored", "after"])
@pytest.mark.parametrize(
    ("renamed", "other", "edited"),
    [
        ("Roles/РольERP", "РольErp", "РольERP"),
        ("Roles/РольERP", "РольErp", "РольА"),
        ("Roles/РольERP", "РольErp", "РольГ"),
        ("Roles/РольА", "рольА", "РольА"),
        ("Roles/РольА", "рольА", "РольБ"),
    ],
)
def test_rights_rows_spelled_differently_fall_back(tmp_path_factory, monkeypatch, fmt, when, renamed, other, edited):
    """Каталог роли переименован только регистром между операциями (имя роли сменили регистром), а правится эта роль,
    другая роль или добавляется новая (`РольГ`): прежний полный проход пишет пути (и `role_name`) с написанием диска —
    итог как у чистой сборки. Регистр латиницы Git не видит вовсе; кириллицы — видит (новое написание — untracked).
    Каталоги, которые называет платформа (`Roles`, `Ext`), регистр не меняют."""
    import test_v1_47_0 as W

    from rlm_tools_bsl.bsl_index import IndexBuilder

    base, files = _case_world(tmp_path_factory, monkeypatch, fmt, extra=_latin_role(fmt))
    db = _rename_between_operations(
        base, renamed, other, when, lambda: IndexBuilder().build(str(base), build_calls=True)
    )
    rights = base / TestSelectiveRights._rights_rel(fmt, edited)
    if rights.exists():
        _edit_rights(rights)
    else:
        _write_edit(rights, _rights_xml(entries=_right_entry("Document.Док", ("Post", "true"))))
        if fmt == "cf":
            _write_edit(base / f"Roles/{edited}.xml", W._cf_md("Role", edited))
        else:
            _write_edit(base / f"Roles/{edited}/{edited}.mdo", W._edt_md("Role", edited))
    res = IndexBuilder().update(str(base))
    assert res["git_fast_path"] is True, res
    _assert_same(db, _fresh_build(tmp_path_factory, monkeypatch, files, base), names=_ROLE_QUERIES)


def _latin_forms(fmt: str) -> dict[str, str]:
    """Формы с латиницей в имени каталога формы и объекта (у CF — с описателями объектов)."""
    import test_v1_47_0 as W

    cf = fmt == "cf"
    files = {
        _form_rel(fmt, "Documents/Док/Forms/ФормаERP"): W._small_form(cf, "ЕРП", "CatalogRef.Номенклатура"),
        _form_rel(fmt, "CommonForms/ФормаERP"): W._small_form(cf, "ОбщЕРП", "CatalogRef.Номенклатура"),
        _form_rel(fmt, "DataProcessors/ОбработкаERP/Forms/Форма"): W._small_form(cf, "Обр", "CatalogRef.Номенклатура"),
    }
    for category, kind, name in (
        ("CommonForms", "CommonForm", "ФормаERP"),
        ("DataProcessors", "DataProcessor", "ОбработкаERP"),
    ):
        if cf:
            files[f"{category}/{name}.xml"] = W._cf_md(kind, name)
        else:
            files[f"{category}/{name}/{name}.mdo"] = W._edt_md(kind, name)
    return files


_FORM_REF_SQL = _REF_SQL + " WHERE ref_kind IN ('form_attribute_type', 'form_main_table', 'form_query')"


@pytest.mark.parametrize("fmt", ["cf", "edt"])
@pytest.mark.parametrize("when", ["restored", "after"])
@pytest.mark.parametrize(
    ("renamed", "other", "edited"),
    [
        ("Documents/Док/Forms/ФормаERP", "ФормаErp", "Documents/Док/Forms/ФормаERP"),
        ("Documents/Док/Forms/ФормаERP", "ФормаErp", "Documents/Док/Forms/ФормаСписка"),
        ("CommonForms/ФормаERP", "ФормаErp", "CommonForms/ФормаERP"),
        ("CommonForms/ФормаERP", "ФормаErp", "CommonForms/ОбщаяФорма"),
        ("DataProcessors/ОбработкаERP", "ОбработкаErp", "DataProcessors/ОбработкаERP/Forms/Форма"),
        ("DataProcessors/ОбработкаERP", "ОбработкаErp", "DataProcessors/ОбработкаERP/Forms/Новая"),
        ("DataProcessors/ОбработкаERP", "ОбработкаErp", "Documents/Док/Forms/ФормаСписка"),
        ("Documents/Док/Forms/ФормаСписка", "формаСписка", "Documents/Док/Forms/ФормаСписка"),
        ("Documents/Док/Forms/ФормаСписка", "формаСписка", "CommonForms/ОбщаяФорма"),
    ],
)
def test_form_rows_spelled_differently_fall_back(tmp_path_factory, monkeypatch, fmt, when, renamed, other, edited):
    """Каталог формы или объекта переименован только регистром между операциями, а правится эта форма, другая форма
    или у переименованного объекта появляется новая (`Новая`): прежний полный проход пишет пути с написанием диска —
    формы и их ссылки как у чистой сборки. Регистр латиницы Git не видит вовсе; кириллицы — видит (новое написание —
    untracked)."""
    import test_v1_47_0 as W

    from rlm_tools_bsl.bsl_index import IndexBuilder

    base, files = _case_world(tmp_path_factory, monkeypatch, fmt, extra=_latin_forms(fmt))
    db = _rename_between_operations(
        base, renamed, other, when, lambda: IndexBuilder().build(str(base), build_calls=True)
    )
    _write_edit(base / _form_rel(fmt, edited), W._small_form(fmt == "cf", "Новый", "DocumentRef.Док"))
    res = IndexBuilder().update(str(base))
    assert res["git_fast_path"] is True, res
    fresh = _fresh_build(tmp_path_factory, monkeypatch, files, base)
    for sql in (_FORM_SQL, _FORM_REF_SQL, _META_COUNTS_SQL):
        assert W._rows(db, sql) == W._rows(fresh, sql), sql


# ═══════════════ Малая операция: без фоновых потоков, опись без пула, детект — один на операцию ═══════════════


class _ThreadStarts:
    """Имена потоков, запущенных за время подмены ``threading.Thread.start``."""

    def __init__(self, monkeypatch) -> None:
        import threading

        self.names: list[str] = []
        real = threading.Thread.start

        def spy(thread, *args, **kwargs):
            self.names.append(thread.name)
            return real(thread, *args, **kwargs)

        monkeypatch.setattr(threading.Thread, "start", spy)

    def with_prefix(self, *prefixes: str) -> list[str]:
        return [name for name in self.names if name.startswith(prefixes)]


def _add_export_procedure(base: Path, files: dict[str, str]) -> dict[str, str]:
    """BSL-правка общего модуля мира на диске (полный скан увидит её по размеру); новый набор файлов."""
    import test_v1_47_0 as W

    files_b = dict(files)
    bsl = next(rel for rel in files if rel.endswith(".bsl") and "CommonModules" in rel)
    files_b[bsl] = files[bsl] + "\nПроцедура Новая() Экспорт\nКонецПроцедуры\n"
    W._apply_changes(base, files, files_b)
    return files_b


@pytest.mark.parametrize("fmt", ["cf", "edt"])
def test_small_build_runs_no_background_threads_and_publishes_the_same_snapshot(tmp_path, monkeypatch, fmt):
    """Выгрузка не крупнее порога параллельного разбора: ни прогрева описателей, ни фоновой описи снимка — коллекторы
    читают описатели сразу, а фоновые потоки лишь спорили с ними за GIL (маленькая сборка шла почти вдвое дольше
    1.47.0). Дайджесты снимка — те же, что у фонового расчёта крупной выгрузки."""
    import rlm_tools_bsl.bsl_index as BI
    from rlm_tools_bsl import _index_snapshot as SNAP

    root = _world_root(tmp_path, fmt)
    with monkeypatch.context() as m:
        starts = _ThreadStarts(m)
        small = _build_with(m, root, tmp_path / "small", "1")
    assert starts.with_prefix("rlm-prefetch", "rlm-index-inputs") == []
    monkeypatch.setattr(BI, "_large_export", lambda _base_path: True)
    with monkeypatch.context() as m:
        starts = _ThreadStarts(m)
        large = _build_with(m, root, tmp_path / "large", "1")
    assert {"rlm-prefetch-walk", "rlm-index-inputs-walk"} <= set(starts.names)
    assert SNAP.load_snapshot(small)["domains"] == SNAP.load_snapshot(large)["domains"]


def test_small_full_scan_runs_no_prefetch(tmp_path_factory, monkeypatch):
    """Полный скан малой выгрузки не прогревает описатели в фоне: опись снимка и коллекторы читают их сразу сами."""
    from rlm_tools_bsl.bsl_index import IndexBuilder

    base, files, _db = _full_scan_world(tmp_path_factory, monkeypatch)
    _add_export_procedure(base, files)
    with monkeypatch.context() as m:
        starts = _ThreadStarts(m)
        res = IndexBuilder().update(str(base))
    assert res["git_fast_path"] is False and res["changed"] == 1, res
    assert starts.with_prefix("rlm-prefetch") == []


def test_large_export_counts_bsl_only_up_to_the_threshold(tmp_path, monkeypatch):
    """Крупная выгрузка — `.bsl` больше порога параллельного разбора. Счёт — тем же `rglob`, что обнаружение, и
    обрывается на первом файле сверх порога: у крупной выгрузки это доли секунды, а не полный обход."""
    import test_v1_47_0 as W

    import rlm_tools_bsl.bsl_index as BI

    monkeypatch.setattr(BI, "_PARALLEL_MIN_BSL_FILES", 3)
    for i in range(3):
        W._write(tmp_path / "CommonModules" / f"М{i}" / "Ext" / "Module.bsl", "")
    assert BI._large_export(str(tmp_path)) is False
    for i in range(3, 10):
        W._write(tmp_path / "CommonModules" / f"М{i}" / "Ext" / "Module.bsl", "")
    taken: list[Path] = []
    real = Path.rglob

    def spy(self, pattern, *args, **kwargs):
        for path in real(self, pattern, *args, **kwargs):
            taken.append(path)
            yield path

    monkeypatch.setattr(Path, "rglob", spy)
    assert BI._large_export(str(tmp_path)) is True
    assert len(taken) == 4


def test_inventory_hashes_a_small_tree_without_a_thread_pool(tmp_path, monkeypatch):
    """Опись малого дерева хеширует в вызывающем потоке: пул на каждое дерево (у каждого расширения рядом — свой)
    стоил дороже самого чтения. Крупное дерево — пулом по ходу обхода. Опись и хеши от способа не зависят."""
    import test_v1_47_0 as W

    from rlm_tools_bsl import _index_snapshot as SNAP

    limit = SNAP._INLINE_HASH_LIMIT
    for name, count in (("small", limit), ("large", limit + 1)):
        for i in range(count):
            W._write(tmp_path / name / "Catalogs" / f"К{i}.xml", f"<x{i}/>")
        W._write(tmp_path / name / "Catalogs" / "К0" / "Ext" / "ObjectModule.bsl", "")

    def inventory(name: str) -> tuple:
        inv = SNAP.inventory_tree(tmp_path / name, hash_rule=lambda rel: rel.endswith(".xml"))
        assert inv.usable
        return inv.dirs, inv.files, inv.hashes

    with monkeypatch.context() as m:
        starts = _ThreadStarts(m)
        small = inventory("small")
        assert starts.with_prefix("rlm-index-inputs") == []
        large = inventory("large")
        assert starts.with_prefix("rlm-index-inputs")
    assert len(small[2]) == limit and len(large[2]) == limit + 1
    monkeypatch.setattr(SNAP, "_INLINE_HASH_LIMIT", 0)
    assert inventory("small") == small
    monkeypatch.setattr(SNAP, "_INLINE_HASH_LIMIT", limit + 10)
    assert inventory("large") == large


@pytest.mark.parametrize("large", [False, True], ids=["small", "large"])
def test_build_and_full_scan_detect_nearby_configurations_once(tmp_path_factory, monkeypatch, large):
    """Детект соседних конфигураций перебирает каталоги рядом с корнем; перехваты и дайджест перехватов для снимка
    берут один результат на операцию. Прежде их было два: в общем каталоге фикстур тестов, где соседей тысячи, сборка
    шла в разы дольше 1.47.0."""
    import rlm_tools_bsl.bsl_index as BI
    from rlm_tools_bsl import extension_detector as ED
    from rlm_tools_bsl.bsl_index import IndexBuilder

    calls: list[str] = []
    real = ED.detect_extension_context

    def spy(path):
        calls.append(path)
        return real(path)

    monkeypatch.setattr(ED, "detect_extension_context", spy)
    monkeypatch.setattr(BI, "_large_export", lambda _base_path: large)
    base, files, _db = _full_scan_world(tmp_path_factory, monkeypatch)
    assert calls == [str(base)]
    calls.clear()
    _add_export_procedure(base, files)
    res = IndexBuilder().update(str(base))
    assert res["git_fast_path"] is False and res["changed"] == 1, res
    assert calls == [str(base)]


def test_detection_failure_skips_overrides_with_one_warning(tmp_path, monkeypatch, caplog):
    """Детект упал — перехваты пропущены с одним предупреждением, как прежде; дайджест перехватов снимка пуст (полный
    скан перечитает их)."""
    from rlm_tools_bsl import _index_snapshot as SNAP
    from rlm_tools_bsl import extension_detector as ED

    root = _world_root(tmp_path, "cf")

    def broken(_path):
        raise RuntimeError("detector failed")

    monkeypatch.setattr(ED, "detect_extension_context", broken)
    with caplog.at_level(logging.WARNING, logger="rlm_tools_bsl"):
        db = _build_with(monkeypatch, root, tmp_path / "idx", "1")
    assert caplog.text.count("Extension detection failed") == 1
    assert _meta_value(db, "extension_overrides_count") == "0"
    assert SNAP.load_snapshot(db)["domains"]["overrides"] is None
