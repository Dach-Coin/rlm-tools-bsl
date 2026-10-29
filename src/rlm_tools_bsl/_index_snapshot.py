"""Служебный снимок входов сборки (v1.48.0): ``build_inputs.v1.json.gz`` рядом с БД индекса.

Полный скан ``update`` 1.47.0 перечитывал и разбирал все описатели при каждом запуске, даже без единой их правки.
Снимок хранит дайджесты входов доменов описателей (метаданные, формы, права, синонимы, перехваты расширений); полный
скан пропускает домен, только если дайджест его входов, посчитанный ПО СОДЕРЖИМОМУ файлов (хеш), совпал со снимком:
равенства размера и времени для этого мало — правку равной длины с прежним временем 1.47.0 подхватывал.

- Публикуют снимок только полная сборка и полный скан — атомарно (временный файл и ``os.replace``), после успеха
  операции. Неудачная публикация успешную операцию ошибкой не делает.
- Любая пишущая операция (сборка, полный скан, Git ``update``) удаляет файл ДО первой записи в БД: прерванная операция
  оставляет только промах кеша. Удалить не удалось — ошибка до изменений БД.
- Привязка к БД: формат, ``builder_version``, точная метка ``built_at``, корень, опции сборки и отпечаток кода пакета.
  Индекс без снимка (1.47.0) работает как прежде; старый код файл игнорирует.

Модуль — leaf: только stdlib; что входит в домен, решает сборщик (``bsl_index``).
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import stat
import sys
import threading
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

logger = logging.getLogger(__name__)

SNAPSHOT_NAME = "build_inputs.v1.json.gz"
FORMAT = 1
# Хеш файла, который не удалось прочитать: домен с таким входом пропуска не получает.
UNREADABLE = "!unreadable"
_HASH_WORKERS = 16
# Сколько файлов опись хеширует сама, без пула: у малого дерева (небольшая выгрузка, расширение рядом) старт пула и
# передача GIL между его потоками дороже самого чтения. Больше — пул по ходу обхода.
_INLINE_HASH_LIMIT = 64
_READ_CHUNK = 1 << 20
_code_fingerprint: str | None = None
_code_lock = threading.Lock()


def snapshot_path(db_path: str | Path) -> Path:
    return Path(db_path).parent / SNAPSHOT_NAME


def remove_snapshot(db_path: str | Path) -> None:
    """Удалить снимок до первой записи операции. Не удалось — ``RuntimeError``: операция не начинается."""
    path = snapshot_path(db_path)
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise RuntimeError(
            f"Cannot remove index input snapshot {path}: {exc}. The index was not changed; remove the file and retry."
        ) from exc


def discard_snapshot(db_path: str | Path) -> None:
    """Убрать снимок вместе с индексом (``drop``); неудача не мешает удалению индекса."""
    try:
        snapshot_path(db_path).unlink()
    except OSError:
        pass


def load_snapshot(db_path: str | Path) -> dict | None:
    """Снимок как словарь; нет файла, битый или чужой формат — ``None``."""
    path = snapshot_path(db_path)
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return None
    except (OSError, EOFError, ValueError) as exc:
        logger.info("index input snapshot %s unusable (%s)", path, type(exc).__name__)
        return None
    if not isinstance(data, dict) or data.get("format") != FORMAT or not isinstance(data.get("domains"), dict):
        return None
    return data


def write_snapshot(db_path: str | Path, data: dict) -> bool:
    """Атомарно опубликовать снимок. Неудача — предупреждение и ``False``, операция остаётся успешной."""
    path = snapshot_path(db_path)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        with gzip.open(tmp, "wt", encoding="utf-8") as fh:
            json.dump({**data, "format": FORMAT}, fh, ensure_ascii=False, sort_keys=True)
        os.replace(tmp, path)
        return True
    except OSError as exc:
        logger.warning("index input snapshot %s not written: %s", path, exc)
        try:
            tmp.unlink()
        except OSError:
            pass
        return False


def _sources_fingerprint(package: Path) -> str | None:
    """sha256 исходников пакета (имена и содержимое всех ``.py``); исходников нет — ``None``."""
    paths = sorted(package.glob("*.py"))
    if not paths:
        return None
    h = hashlib.sha256()
    for path in paths:
        h.update(path.name.encode("utf-8") + b"\0")
        h.update(path.read_bytes().replace(b"\r\n", b"\n"))
        h.update(b"\0")
    return h.hexdigest()


def code_fingerprint() -> str:
    """Отпечаток кода пакета (все ``.py`` модуля с именами): снимок другого кода не годится — даже без смены
    ``BUILDER_VERSION`` правка коллектора меняет его выход. Пакет без исходников (только ``.pyc``, zip) — отпечаток
    свой у каждого процесса: снимок не переживает перезапуск, но и чужой код его не примет."""
    global _code_fingerprint
    with _code_lock:
        if _code_fingerprint is None:
            sources = _sources_fingerprint(Path(__file__).resolve().parent)
            _code_fingerprint = sources if sources is not None else f"process:{os.urandom(16).hex()}"
        return _code_fingerprint


def digest(parts: Iterable) -> str:
    """Дайджест упорядоченной последовательности JSON-сериализуемых частей."""
    h = hashlib.sha256()
    for part in parts:
        h.update(json.dumps(part, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


def file_hash(path: str | Path) -> str:
    """BLAKE2b содержимого файла (порциями); не прочитан — ``UNREADABLE``."""
    h = hashlib.blake2b(digest_size=16)
    try:
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(_READ_CHUNK)
                if not chunk:
                    break
                h.update(chunk)
    except OSError:
        return UNREADABLE
    return h.hexdigest()


def _is_redirect(entry: os.DirEntry) -> bool:
    """Ссылка на каталог или точка повторной обработки (junction на Windows: ``is_symlink`` для неё ложно)."""
    try:
        if entry.is_symlink():
            return True
        if sys.platform == "win32":
            attrs = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
            return bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    except OSError:
        return True
    return False


class TreeInventory:
    """Опись дерева: все каталоги, все файлы (пути относительно корня, ``/``), хеши выбранных файлов. ``usable`` —
    ложно, если в дереве есть ссылка на каталог/junction или каталог не прочитан: такое дерево не доказывает
    неизменности ни одного домена."""

    def __init__(self) -> None:
        self.dirs: list[str] = []
        self.files: list[str] = []
        self.hashes: dict[str, str] = {}
        self.usable = True


def inventory_tree(
    root: str | Path,
    *,
    hash_rule: Callable[[str], bool],
    skip_file: Callable[[str], bool] | None = None,
    workers: int = _HASH_WORKERS,
    stop: threading.Event | None = None,
) -> TreeInventory:
    """Обойти дерево (со скрытыми каталогами), хешировать файлы ``hash_rule(rel)``: крупное дерево — пулом потоков по
    ходу обхода (чтение заодно прогревает кеш файлов для коллекторов), малое (не больше ``_INLINE_HASH_LIMIT`` файлов к
    хешированию) — в вызывающем потоке после обхода, без пула. ``skip_file(rel)`` — файлы вне описи (BSL основной).
    ``stop`` — прервать (операция упала): опись тогда ``usable=False``."""
    inv = TreeInventory()
    base = os.fspath(root)
    lock = threading.Lock()
    # Файлы к хешированию, пока пула нет: превысили порог — пул заводится и получает их первыми.
    queued: list[tuple[str, str]] = []
    pool: ThreadPoolExecutor | None = None
    slots = threading.BoundedSemaphore(max(1, workers) * 4)

    def _hash(rel: str, path: str) -> None:
        value = file_hash(path)
        with lock:
            inv.hashes[rel] = value

    def _run(rel: str, path: str) -> None:
        try:
            if stop is None or not stop.is_set():
                _hash(rel, path)
        finally:
            slots.release()

    def _submit(rel: str, path: str) -> None:
        nonlocal pool
        if pool is None:
            queued.append((rel, path))
            if len(queued) <= _INLINE_HASH_LIMIT:
                return
            pool = ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="rlm-index-inputs")
            batch = queued[:]
            queued.clear()
        else:
            batch = [(rel, path)]
        for item in batch:
            slots.acquire()
            pool.submit(_run, *item)

    try:
        stack: list[tuple[str, str]] = [(base, "")]
        while stack:
            if stop is not None and stop.is_set():
                inv.usable = False
                break
            top, rel_dir = stack.pop()
            try:
                with os.scandir(top) as it:
                    entries = list(it)
            except OSError:
                inv.usable = False
                continue
            for entry in entries:
                rel = f"{rel_dir}/{entry.name}" if rel_dir else entry.name
                try:
                    is_dir = entry.is_dir()
                except OSError:
                    is_dir = False
                if is_dir:
                    if _is_redirect(entry):
                        inv.usable = False
                        continue
                    inv.dirs.append(rel)
                    stack.append((entry.path, rel))
                    continue
                if skip_file is not None and skip_file(rel):
                    continue
                inv.files.append(rel)
                # Только обычный файл (и ссылка на него): FIFO, сокет или устройство открывать нельзя — open() на FIFO
                # без писателя не вернётся. Такая запись — только путь, коллекторы её тоже не читают (is_file()).
                try:
                    regular = entry.is_file()
                except OSError:
                    regular = False
                if regular and hash_rule(rel):
                    _submit(rel, entry.path)
    finally:
        if pool is not None:
            pool.shutdown(wait=True)
    for rel, path in queued:
        if stop is not None and stop.is_set():
            inv.usable = False
            break
        _hash(rel, path)
    inv.dirs.sort()
    inv.files.sort()
    return inv
