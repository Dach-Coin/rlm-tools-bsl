"""Disk cache for BSL file index (`file_index.json`) + activity-marker based cleanup.

Layout:

- Cache root resolution (see :func:`_cache_base`):
  * If ``RLM_CONFIG_FILE`` is set → ``dirname(RLM_CONFIG_FILE)/cache/`` (service install).
  * Else → ``~/.cache/rlm-tools-bsl/`` (XDG default).
- Per-project cache: ``<cache_root>/<hash12>/file_index.json``.

BSL indexes (``bsl_index.db``) live elsewhere (see
:func:`rlm_tools_bsl.bsl_index.get_index_dir_root`) and are **not** managed by
this module. They are never auto-cleaned; users manage them via
``rlm_index(action='drop')``.

Activity marker ``last_used.txt`` is written by :func:`touch_project_cache`
into the cache root on every ``rlm_start`` / ``rlm_index build|update|info``;
:func:`cleanup_stale_cache` removes per-project cache subdirectories that
haven't been touched within ``RLM_CACHE_MAX_AGE_DAYS`` (default 14).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import pathlib
import shutil
import threading
import time

from rlm_tools_bsl.format_detector import BslFileInfo

CACHE_VERSION = 1
_disk_lock = threading.Lock()

LAST_USED_MARKER = "last_used.txt"
DEFAULT_MAX_AGE_DAYS = 14

_logger = logging.getLogger(__name__)


def _cache_base() -> pathlib.Path:
    """Return the cache root directory.

    Respects the ``RLM_CONFIG_FILE`` override so a Windows service installed
    with ``--env`` writes cache alongside its logs/config under the installing
    user's home, not under the LocalSystem profile.

    Precedence:
    1. ``RLM_CONFIG_FILE`` set → ``dirname(RLM_CONFIG_FILE)/cache``
    2. Fallback → ``~/.cache/rlm-tools-bsl``
    """
    config_override = os.environ.get("RLM_CONFIG_FILE")
    if config_override:
        return pathlib.Path(config_override).parent / "cache"
    return pathlib.Path.home() / ".cache" / "rlm-tools-bsl"


def _cache_dir(base_path: str) -> pathlib.Path:
    h = hashlib.md5(base_path.encode()).hexdigest()[:12]
    return _cache_base() / h


def _project_hash(base_path: str) -> str:
    return hashlib.md5(base_path.encode()).hexdigest()[:12]


def _entry_to_dict(relative_path: str, info: BslFileInfo) -> dict:
    return {
        "p": relative_path,
        "c": info.category,
        "o": info.object_name,
        "m": info.module_type,
        "f": info.form_name,
        "cmd": info.command_name,
        "fe": info.is_form_module,
    }


def _dict_to_entry(d: dict) -> tuple[str, BslFileInfo]:
    return d["p"], BslFileInfo(
        relative_path=d["p"],
        category=d.get("c"),
        object_name=d.get("o"),
        module_type=d.get("m"),
        form_name=d.get("f"),
        command_name=d.get("cmd"),
        is_form_module=d.get("fe", False),
    )


def _paths_hash(paths: list[str]) -> str:
    """MD5 hash of sorted relative paths for cache invalidation."""
    joined = "\n".join(sorted(paths))
    return hashlib.md5(joined.encode()).hexdigest()


def load_index(
    base_path: str,
    bsl_count: int,
    bsl_paths: list[str] | None = None,
) -> list[tuple[str, BslFileInfo]] | None:
    """Load index from disk if version, bsl_count, and paths_hash match. Returns None on miss."""
    index_file = _cache_dir(base_path) / "file_index.json"
    try:
        with open(index_file, encoding="utf-8") as f:
            data = json.load(f)
        if data.get("version") != CACHE_VERSION:
            return None
        if data.get("bsl_count") != bsl_count:
            return None
        if bsl_paths is not None and "paths_hash" in data:
            if data["paths_hash"] != _paths_hash(bsl_paths):
                return None
        return [_dict_to_entry(e) for e in data["entries"]]
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        return None


def save_index(
    base_path: str,
    bsl_count: int,
    entries: list[tuple[str, BslFileInfo]],
) -> None:
    """Save index to disk. Silently ignores write errors.

    Also stamps the activity marker here, because this is the point where the
    per-project cache directory is first created. Without this, the very first
    ``rlm_start`` on a new project wouldn't leave a marker — ``touch_project_cache``
    is called early in ``rlm_start``, when the directory doesn't yet exist.
    """
    try:
        cache_dir = _cache_dir(base_path)
        cache_dir.mkdir(parents=True, exist_ok=True)
        paths = [p for p, _ in entries]
        data = {
            "version": CACHE_VERSION,
            "base_path": base_path,
            "bsl_count": bsl_count,
            "paths_hash": _paths_hash(paths),
            "saved_at": time.time(),
            "entries": [_entry_to_dict(p, i) for p, i in entries],
        }
        with _disk_lock:
            with open(cache_dir / "file_index.json", "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        try:
            (cache_dir / LAST_USED_MARKER).write_text(
                f"{time.time():.3f}\n{base_path}\n",
                encoding="utf-8",
            )
        except OSError as exc:
            _logger.debug("save_index: marker stamp for %s failed: %s", cache_dir, exc)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Activity marker + automatic cleanup of stale project caches
# ---------------------------------------------------------------------------


def touch_project_cache(base_path: str) -> None:
    """Mark a project's cache as recently used.

    Writes/refreshes ``last_used.txt`` in the project's cache subdirectory
    (``<cache_root>/<hash12>/``) if that directory exists. If the directory
    is not there yet, nothing is done — we don't want to create empty folders
    just for a marker.

    BSL index directories (``RLM_INDEX_DIR``) are **not** touched — they are
    outside the cleanup scope and don't need activity tracking.
    """
    proj_dir = _cache_dir(base_path)
    try:
        if not proj_dir.is_dir():
            return
        marker = proj_dir / LAST_USED_MARKER
        marker.write_text(f"{time.time():.3f}\n{base_path}\n", encoding="utf-8")
    except OSError as exc:
        _logger.debug("touch_project_cache: %s failed: %s", proj_dir, exc)


def purge_project_cache(base_path: str) -> str | None:
    """Remove a project's on-disk cache subdirectory (``<cache_root>/<hash12>/``).

    Used by ``rlm_index(action='drop')`` to complete a project decommission:
    the index DB and its file-listing cache should disappear together, otherwise
    a stale ``file_index.json`` is left orphaned (issue #16). Best-effort — a
    missing directory or an ``OSError`` is swallowed and reported via the return
    value (``None`` = nothing removed).

    Returns the removed directory path as a string, or ``None`` if it did not
    exist / could not be removed.
    """
    proj_dir = _cache_dir(base_path)
    try:
        if not proj_dir.is_dir():
            return None
        shutil.rmtree(proj_dir)
        return str(proj_dir)
    except OSError as exc:
        _logger.debug("purge_project_cache: %s failed: %s", proj_dir, exc)
        return None


def _project_last_used(project_dir: pathlib.Path) -> float:
    """Return the effective "last used" mtime for a project cache directory.

    Preference: ``last_used.txt`` mtime; fallback: directory mtime.
    """
    marker = project_dir / LAST_USED_MARKER
    try:
        if marker.is_file():
            return marker.stat().st_mtime
    except OSError:
        pass
    try:
        return project_dir.stat().st_mtime
    except OSError:
        return 0.0


def _cleanup_root(root: pathlib.Path, max_age_days: int) -> dict:
    """Scan one cache root and remove stale project subdirectories."""
    stats: dict = {"root": str(root), "scanned": 0, "removed": 0, "bytes_freed": 0, "errors": []}
    if not root.is_dir():
        return stats
    cutoff = time.time() - (max_age_days * 86400)

    try:
        entries = list(root.iterdir())
    except OSError as exc:
        stats["errors"].append({"path": str(root), "error": f"list failed: {exc}"})
        return stats

    for entry in entries:
        try:
            if not entry.is_dir():
                continue
        except OSError:
            continue
        # Skip anything that doesn't look like a project hash (12 hex chars)
        name = entry.name
        if len(name) != 12 or any(c not in "0123456789abcdef" for c in name):
            continue

        stats["scanned"] += 1
        last_used = _project_last_used(entry)
        if last_used >= cutoff:
            continue

        # Compute size best-effort, then remove
        size = 0
        try:
            for sub_root, _dirs, files in os.walk(entry):
                for fname in files:
                    fp = os.path.join(sub_root, fname)
                    try:
                        size += os.path.getsize(fp)
                    except OSError:
                        pass
        except OSError:
            pass

        try:
            shutil.rmtree(entry)
            stats["removed"] += 1
            stats["bytes_freed"] += size
        except OSError as exc:
            stats["errors"].append({"path": str(entry), "error": str(exc)})

    return stats


def _touch_registered_projects() -> int:
    """One-shot legacy migration: stamp missing markers for registered projects.

    For each registered project, write ``last_used.txt`` only if it's not
    already there. Protects pre-v1.9.1 caches (without marker, possibly old
    mtime) from being wiped on first upgrade, while still allowing cleanup
    to remove registered-but-forgotten projects on subsequent starts (their
    markers will age out naturally).

    Paths are cf-normalized so a registry entry pointing at a container
    directory maps to the same hash directory the cache was written under.

    Returns the number of migration markers written. Never raises.
    """
    count = 0
    try:
        from rlm_tools_bsl._paths import canonicalize_path
        from rlm_tools_bsl.extension_detector import resolve_config_root
        from rlm_tools_bsl.projects import RegistryCorruptedError, get_registry

        try:
            reg = get_registry()
            projects = reg.list_projects()
        except RegistryCorruptedError as exc:
            _logger.warning("cleanup: cannot read registry (%s) — no protective touch", exc)
            return 0

        cache_root = _cache_base()
        for entry in projects:
            raw = entry.get("path")
            if not raw:
                continue
            try:
                canonical = canonicalize_path(raw)
                effective, _candidates = resolve_config_root(canonical)
                proj_dir = cache_root / _project_hash(effective)
                if not proj_dir.is_dir():
                    continue
                marker = proj_dir / LAST_USED_MARKER
                if marker.exists():
                    continue  # already has a real activity marker — don't override
                try:
                    marker.write_text(f"{time.time():.3f}\n{effective}\n", encoding="utf-8")
                    count += 1
                except OSError as exc:
                    _logger.debug("cleanup: legacy-touch for %s failed: %s", proj_dir, exc)
            except Exception as exc:
                _logger.debug("cleanup: pre-touch for %s failed: %s", raw, exc)
    except Exception as exc:
        _logger.debug("cleanup: _touch_registered_projects failed: %s", exc)
    return count


def cleanup_stale_cache(max_age_days: int | None = None) -> dict:
    """Remove cache subdirectories for projects unused longer than *max_age_days*.

    Scans the cache root only (``_cache_base()``). BSL index directories
    (``RLM_INDEX_DIR`` / :func:`rlm_tools_bsl.bsl_index.get_index_dir_root`)
    are **never** touched — indexes are expensive to rebuild and are managed
    manually via ``rlm_index(action='drop')``.

    Passing ``0`` or a negative value disables cleanup.

    Before scanning, activity markers are refreshed for every registered
    project that doesn't yet have one (one-shot legacy migration for caches
    built before v1.9.1).
    """
    if max_age_days is None:
        try:
            max_age_days = int(os.environ.get("RLM_CACHE_MAX_AGE_DAYS", str(DEFAULT_MAX_AGE_DAYS)))
        except ValueError:
            max_age_days = DEFAULT_MAX_AGE_DAYS

    cache_root = _cache_base()
    aggregate: dict = {
        "disabled": max_age_days <= 0,
        "max_age_days": max_age_days,
        "scanned": 0,
        "removed": 0,
        "bytes_freed": 0,
        "errors": [],
        "cache_root": str(cache_root),
        "legacy_markers_written": 0,
    }
    if max_age_days <= 0:
        return aggregate

    aggregate["legacy_markers_written"] = _touch_registered_projects()

    sub = _cleanup_root(cache_root, max_age_days)
    aggregate["scanned"] = sub["scanned"]
    aggregate["removed"] = sub["removed"]
    aggregate["bytes_freed"] = sub["bytes_freed"]
    aggregate["errors"] = sub["errors"]
    return aggregate
