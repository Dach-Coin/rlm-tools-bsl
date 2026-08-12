"""Bounded execution of Git subprocesses."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence
from os import PathLike
from pathlib import Path
from typing import IO

_TASKKILL_TIMEOUT_SECONDS = 5
_POST_TIMEOUT_DRAIN_SECONDS = 2
_POST_TIMEOUT_WAIT_SECONDS = 2


def _taskkill_tree(pid: int) -> None:
    """Best-effort termination of a Windows process tree."""
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    taskkill = system_root / "System32" / "taskkill.exe"
    try:
        subprocess.run(
            [str(taskkill), "/PID", str(pid), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=_TASKKILL_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass


def _kill_direct_process(process: subprocess.Popen[str]) -> None:
    """Best-effort fallback for the immediate child process."""
    if process.poll() is not None:
        return
    try:
        process.kill()
    except OSError:
        pass


def _close_pipe(pipe: IO[str] | None) -> None:
    if pipe is None:
        return
    try:
        pipe.close()
    except (OSError, ValueError):
        pass


def _drain_after_timeout(process: subprocess.Popen[str]) -> None:
    """Collect pipes only while collection remains bounded."""
    try:
        process.communicate(timeout=_POST_TIMEOUT_DRAIN_SECONDS)
        return
    except (OSError, ValueError, subprocess.TimeoutExpired):
        _close_pipe(process.stdout)
        _close_pipe(process.stderr)

    try:
        process.wait(timeout=_POST_TIMEOUT_WAIT_SECONDS)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        _kill_direct_process(process)


def run_git(
    args: Sequence[str | PathLike[str]],
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    """Run Git command *args* with a bounded timeout."""
    command = list(args)
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            _taskkill_tree(process.pid)
        _kill_direct_process(process)
        _drain_after_timeout(process)
        raise

    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
