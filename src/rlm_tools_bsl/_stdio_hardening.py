"""Разводка стандартных дескрипторов перед спавном дочерних процессов (stdio-транспорт).

Проблема, которую решает модуль (CPython gh-78961). При MCP stdio-транспорте
протокол идёт по fd 0/1, и транспорт всё время сеанса держит на fd 0 висящее
блокирующее чтение. Windows сериализует операции на синхронном pipe и передаёт
дочернему процессу стандартные хэндлы родителя даже при ``bInheritHandles=FALSE``.
Поэтому Python-ребёнок, унаследовавший занятый pipe, зависает **внутри
инициализации интерпретатора** — до первой строки собственного кода: сброс
буферов на старте (``fflush(NULL)``) упирается в pipe, на котором висит чтение
родителя.

Для сервера это означало зависание `rlm_start` в дефолтной конфигурации
(транспорт `stdio` + `RLM_SANDBOX_MODE=process`) на Windows: sandbox-worker
спавнится, но никогда не присылает `init_ok`, и сессия падает по
`RLM_SANDBOX_START_TIMEOUT_SECONDS`. Собственная отвязка stdio внутри worker
(`_detach_stdio`) от этого не спасает: она исполняется уже после инициализации
интерпретатора, до которой дело не доходит.

Лечение — на уровне транспорта, а не места спавна: протокол обслуживается с
приватных дубликатов fd 0/1, а сами fd 0 и 1 на время работы сервера уводятся на
`os.devnull` и на stderr. Дети наследуют «отводы», а не протокольные трубы, —
и это верно для ЛЮБОГО ребёнка (sandbox-worker, `git` из `git_search`), а не
только для того, чей спавн пропатчили. Побочно закрывается класс «посторонний
вывод попал в JSON-RPC поток»: при `swap_sys_streams=False` ни `print()`, ни
raw-`os.write(1, ...)`, ни вывод ребёнка до протокола не дотягиваются — все они
идут в stderr. Оговорка одна: если клиент слил stderr в stdout (`2>&1`), отвод
и есть протокольная труба, и эта гарантия не действует (зависание всё равно
вылечено — его порождает сторона fd 0).

Модуль — leaf (только stdlib) и best-effort: любая неудача откатывается к
исходному состоянию дескрипторов и возвращается как «не применено», но никогда
не срывает старт сервера. Отключение — `RLM_STDIO_HARDENING=0`.

Тот же приём с v2.0.0 делает сам MCP SDK; наш вызов ему не мешает и не
дублируется: после разводки `sys.stdin.buffer` больше не сидит на fd 0, и
`stdio_server()` из v2 такие потоки обслуживает как есть, не претендуя на
дескрипторы повторно.
"""

from __future__ import annotations

import io
import os
import sys
from collections.abc import Callable
from typing import NamedTuple, TextIO

_DISABLED_VALUES = frozenset({"0", "false", "no", "off"})

# Win32 STD_*_HANDLE (winbase.h) — как беззнаковые DWORD.
_STD_HANDLE_IDS = {0: 0xFFFFFFF6, 1: 0xFFFFFFF5, 2: 0xFFFFFFF4}


class StdioHardening(NamedTuple):
    """Результат разводки.

    ``applied`` — были ли реально уведены fd 0/1; ``detail`` — человекочитаемая
    причина для лога; ``restore`` — возврат дескрипторов (и, если потоки
    подменялись, ``sys.stdin``/``sys.stdout``) в исходное состояние; повторный
    вызов доделывает то, что не удалось с первого раза.

    ``wire_stdin``/``wire_stdout`` — текстовые обёртки над проводами; их нужно
    отдать транспорту явно, если ``sys``-потоки НЕ подменялись.
    """

    applied: bool
    detail: str
    restore: Callable[[], None]
    wire_stdin: TextIO | None = None
    wire_stdout: TextIO | None = None


def _noop() -> None:
    return None


def is_enabled() -> bool:
    """Включена ли разводка. Отключают только явные «нет»-значения."""
    raw = os.environ.get("RLM_STDIO_HARDENING")
    if raw is None:
        return True
    return raw.strip().lower() not in _DISABLED_VALUES


def _is_backed_by_fd(stream: TextIO | None, fd: int) -> bool:
    """Сидит ли текстовый поток именно на этом дескрипторе."""
    try:
        return stream.buffer.fileno() == fd  # type: ignore[union-attr]
    except (AttributeError, OSError, ValueError):
        return False


def _rebind_win32_std_handle(fd: int) -> None:
    """Указать Win32-слот стандартного хэндла на текущий OS-хэндл fd.

    Наследование стандартных хэндлов ребёнком читает именно Win32-слот, а не
    таблицу дескрипторов CRT. UCRT синхронизирует слот сам при `dup2` на fd 0/1/2,
    поэтому вызов — страховка на случай другой реализации CRT/embedded-хоста;
    неудача не считается ошибкой разводки.
    """
    if sys.platform != "win32":
        return
    import ctypes
    import msvcrt

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.SetStdHandle.argtypes = (ctypes.c_uint32, ctypes.c_void_p)
    kernel32.SetStdHandle.restype = ctypes.c_int
    handle = msvcrt.get_osfhandle(fd)
    if not kernel32.SetStdHandle(ctypes.c_uint32(_STD_HANDLE_IDS[fd]), ctypes.c_void_p(handle)):
        raise OSError(f"SetStdHandle(fd={fd}) failed: winerror={ctypes.get_last_error()}")


def _rebind_quiet(fd: int) -> None:
    try:
        _rebind_win32_std_handle(fd)
    except OSError:
        pass


def _close_quiet(fd: int | None) -> None:
    if fd is None:
        return
    try:
        os.close(fd)
    except OSError:
        pass


def _open_stdout_diversion() -> int:
    """Куда увести fd 1: предпочтительно на stderr, иначе в devnull.

    Если клиент запустил сервер со stderr, слитым в stdout (`2>&1`), отвод — это
    та же протокольная труба, и посторонняя запись в fd 1 по-прежнему попадёт в
    протокол. Зависание при этом всё равно вылечено: его порождает сторона fd 0,
    а он уводится в devnull безусловно. Поведение совпадает с MCP SDK v2.
    """
    try:
        return os.dup(2)
    except OSError:
        return os.open(os.devnull, os.O_WRONLY)


def _divert(fd: int, open_diversion: Callable[[], int], wire_fd: int) -> None:
    """Увести fd на отвод, сохранив провод в ``wire_fd``.

    При неудаче fd гарантированно возвращается на провод: `dup2` на Windows
    умеет закрыть цель до того, как упасть.
    """
    diversion_fd = open_diversion()
    try:
        os.dup2(diversion_fd, fd)
    except OSError:
        try:
            os.dup2(wire_fd, fd)
        except OSError:
            pass
        _rebind_quiet(fd)
        raise
    finally:
        _close_quiet(diversion_fd)
    _rebind_quiet(fd)


def harden_stdio_for_children(*, swap_sys_streams: bool = False) -> StdioHardening:
    """Увести fd 0 в devnull, fd 1 на stderr; протокол — с приватных дубликатов.

    Вызывать ОДИН раз на старте сервера, только для stdio-транспорта и до
    запуска транспорта.

    ``swap_sys_streams=False`` (предпочтительно): ``sys.stdin``/``sys.stdout``
    остаются на fd 0/1, то есть на отводах, — посторонний `print()` и чтение
    `sys.stdin` физически не могут дотянуться до протокола. Транспорту нужно
    ЯВНО отдать `wire_stdin`/`wire_stdout` из результата.

    ``swap_sys_streams=True`` — запасной путь для транспорта, который умеет
    брать потоки только из ``sys``: обёртки проводов подставляются в
    ``sys.stdin``/``sys.stdout``. Зависание лечится так же, но посторонняя
    запись в ``sys.stdout`` по-прежнему попадает в протокол.
    """
    if not is_enabled():
        return StdioHardening(False, "отключено через RLM_STDIO_HARDENING", _noop)

    stdin_on_fd0 = _is_backed_by_fd(sys.stdin, 0)
    stdout_on_fd1 = _is_backed_by_fd(sys.stdout, 1)
    if not (stdin_on_fd0 and stdout_on_fd1):
        return StdioHardening(
            False,
            f"stdin/stdout не на стандартных дескрипторах (stdin_on_fd0={stdin_on_fd0} stdout_on_fd1={stdout_on_fd1})",
            _noop,
        )

    orig_stdin, orig_stdout = sys.stdin, sys.stdout
    wire_in: int | None = None
    wire_out: int | None = None
    diverted: list[int] = []
    try:
        wire_in = os.dup(0)
        wire_out = os.dup(1)
        if wire_in <= 2 or wire_out <= 2:
            raise OSError(f"дубликат провода попал в стандартный диапазон (wire_in={wire_in} wire_out={wire_out})")

        _divert(0, lambda: os.open(os.devnull, os.O_RDONLY), wire_in)
        diverted.append(0)
        _divert(1, _open_stdout_diversion, wire_out)
        diverted.append(1)

        # closefd=False: провод переживает свою обёртку. Дескриптор нельзя
        # закрывать и переиспользовать, пока на нём может висеть чтение
        # транспортного потока.
        wire_stdin = io.TextIOWrapper(
            io.BufferedReader(io.FileIO(wire_in, "rb", closefd=False)),
            encoding="utf-8",
            errors="replace",
        )
        wire_stdout = io.TextIOWrapper(
            io.BufferedWriter(io.FileIO(wire_out, "wb", closefd=False)),
            encoding="utf-8",
        )
        # Обе обёртки готовы — только теперь подменяем потоки. Иначе сбой на
        # построении второй оставил бы разводку применённой наполовину: транспорт
        # подхватил бы старый sys.stdout, чей fd 1 уже уведён, и все ответы
        # молча уходили бы в stderr.
        if swap_sys_streams:
            sys.stdin, sys.stdout = wire_stdin, wire_stdout
    except Exception as exc:
        for fd, wire in ((0, wire_in), (1, wire_out)):
            if fd in diverted and wire is not None:
                try:
                    os.dup2(wire, fd)
                except OSError:
                    pass
                _rebind_quiet(fd)
        sys.stdin, sys.stdout = orig_stdin, orig_stdout
        _close_quiet(wire_in)
        _close_quiet(wire_out)
        return StdioHardening(False, f"разводка не удалась, потоки не тронуты: {type(exc).__name__}: {exc}", _noop)

    # Восстановление отслеживается ПОФДОВО и снимается с учёта только по факту
    # успеха: иначе разовый сбой `dup2` навсегда пометил бы restore выполненным,
    # и повторная попытка стала бы бесполезной.
    pending = {0: wire_in, 1: wire_out}

    def restore() -> None:
        if not pending:
            return
        for stream in (wire_stdout, sys.stdout):
            try:
                stream.flush()
            except (OSError, ValueError):
                pass
        for fd in list(pending):
            try:
                os.dup2(pending[fd], fd)
            except OSError:
                continue
            _rebind_quiet(fd)
            del pending[fd]
        if not pending and swap_sys_streams:
            sys.stdin, sys.stdout = orig_stdin, orig_stdout
        # Провода намеренно не закрываются: на них может висеть чтение
        # транспортного потока, а закрытие освободило бы номер под переиспользование.

    return StdioHardening(
        True,
        f"fd 0 → devnull, fd 1 → stderr; протокол на fd {wire_in}/{wire_out}; "
        f"sys-потоки {'подменены' if swap_sys_streams else 'оставлены на отводах'}",
        restore,
        wire_stdin,
        wire_stdout,
    )
