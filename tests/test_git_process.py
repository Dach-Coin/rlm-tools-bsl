"""Tests for the bounded Git subprocess runner."""

import importlib.util
import os
import subprocess
import sys
import time

import pytest

from _process_test_utils import pid_alive, wait_until
from rlm_tools_bsl import _git_process as git_process_mod
from rlm_tools_bsl._git_process import run_git


def test_git_process_module_exists():
    assert importlib.util.find_spec("rlm_tools_bsl._git_process") is not None


def test_run_git_captures_unicode_and_return_code():
    result = run_git(
        [
            sys.executable,
            "-c",
            "import sys; "
            "sys.stdout.reconfigure(encoding='utf-8'); "
            "sys.stderr.reconfigure(encoding='utf-8'); "
            "print('Привет'); print('ошибка', file=sys.stderr); raise SystemExit(7)",
        ],
        timeout=5,
    )

    assert result.returncode == 7
    assert result.stdout.strip() == "Привет"
    assert result.stderr.strip() == "ошибка"


def test_run_git_supplies_eof_to_stdin():
    started = time.monotonic()

    result = run_git(
        [sys.executable, "-c", "import sys; print(len(sys.stdin.read()))"],
        timeout=5,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "0"
    assert time.monotonic() - started < 5


def test_run_git_timeout_is_bounded():
    started = time.monotonic()

    with pytest.raises(subprocess.TimeoutExpired):
        run_git([sys.executable, "-c", "import time; time.sleep(60)"], timeout=0.2)

    assert time.monotonic() - started < 10


@pytest.mark.skipif(os.name != "nt", reason="Windows process-tree regression")
def test_run_git_timeout_terminates_descendants_holding_pipes(tmp_path):
    parent_pid_file = tmp_path / "parent.pid"
    child_pid_file = tmp_path / "child.pid"
    script = tmp_path / "spawn_descendant.py"
    script.write_text(
        "\n".join(
            [
                "import os",
                "import subprocess",
                "import sys",
                "import time",
                "from pathlib import Path",
                "parent_pid_file, child_pid_file = map(Path, sys.argv[1:])",
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])",
                "parent_pid_file.write_text(str(os.getpid()), encoding='ascii')",
                "child_pid_file.write_text(str(child.pid), encoding='ascii')",
                "print('ready', flush=True)",
                "time.sleep(60)",
            ]
        ),
        encoding="utf-8",
    )
    started = time.monotonic()

    with pytest.raises(subprocess.TimeoutExpired):
        run_git([sys.executable, str(script), str(parent_pid_file), str(child_pid_file)], timeout=1)

    elapsed = time.monotonic() - started
    assert elapsed < 10
    assert parent_pid_file.exists()
    assert child_pid_file.exists()
    parent_pid = int(parent_pid_file.read_text(encoding="ascii"))
    child_pid = int(child_pid_file.read_text(encoding="ascii"))
    assert wait_until(lambda: not pid_alive(parent_pid))
    assert wait_until(lambda: not pid_alive(child_pid))


@pytest.mark.skipif(os.name != "nt", reason="Windows taskkill fallback")
def test_run_git_preserves_timeout_when_taskkill_fails(monkeypatch, tmp_path):
    pid_file = tmp_path / "process.pid"
    script = tmp_path / "sleep.py"
    script.write_text(
        "\n".join(
            [
                "import os",
                "import sys",
                "import time",
                "from pathlib import Path",
                "Path(sys.argv[1]).write_text(str(os.getpid()), encoding='ascii')",
                "time.sleep(60)",
            ]
        ),
        encoding="utf-8",
    )

    def fail_taskkill(*args, **kwargs):
        raise OSError("taskkill unavailable")

    monkeypatch.setattr(git_process_mod.subprocess, "run", fail_taskkill)
    started = time.monotonic()

    with pytest.raises(subprocess.TimeoutExpired) as raised:
        run_git([sys.executable, str(script), str(pid_file)], timeout=3)

    assert raised.value.timeout == 3
    assert time.monotonic() - started < 10
    assert pid_file.exists()
    pid = int(pid_file.read_text(encoding="ascii"))
    assert wait_until(lambda: not pid_alive(pid))
