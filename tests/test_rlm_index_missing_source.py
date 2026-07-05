"""Issue #16: rlm_index drop/info must work after the source dir is deleted.

`drop` (and `info`) act on the cache/index (stored under RLM_INDEX_DIR by a hash
of the effective config-root path), NOT on the live source tree. When a project
is decommissioned and its sources are removed from disk, the index must still be
inspectable and removable — otherwise it can never be cleaned up.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import textwrap
from pathlib import Path

import pytest
from unittest.mock import patch


_CF_MAIN_XML = textwrap.dedent("""\
    <?xml version="1.0" encoding="UTF-8"?>
    <MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">
        <Configuration uuid="00000000-0000-0000-0000-000000000001">
            <Properties>
                <Name>MainCfg</Name>
                <NamePrefix/>
            </Properties>
        </Configuration>
    </MetaDataObject>
""")


def _make_src_proj(root: str) -> str:
    """Container layout: <root>/src/cf/... — effective root is <root>/src/cf."""
    src = os.path.join(root, "src")
    cf = os.path.join(src, "cf")
    os.makedirs(cf)
    with open(os.path.join(cf, "Configuration.xml"), "w", encoding="utf-8") as f:
        f.write(_CF_MAIN_XML)
    with open(os.path.join(cf, "Module.bsl"), "w", encoding="utf-8") as f:
        f.write("Процедура Тест()\nКонецПроцедуры\n")
    return src


_EDT_MAIN_MDO = textwrap.dedent("""\
    <?xml version="1.0" encoding="UTF-8"?>
    <mdclass:Configuration xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass"
                           uuid="00000000-0000-0000-0000-000000000003">
        <name>МояЕДТКонфигурация</name>
        <defaultRunMode>ManagedApplication</defaultRunMode>
    </mdclass:Configuration>
""")


def _make_edt_root(root: str) -> str:
    """Flat EDT root: <root> holds Configuration/Configuration.mdo directly."""
    os.makedirs(os.path.join(root, "Configuration"))
    with open(os.path.join(root, "Configuration", "Configuration.mdo"), "w", encoding="utf-8") as f:
        f.write(_EDT_MAIN_MDO)
    with open(os.path.join(root, "Module.bsl"), "w", encoding="utf-8") as f:
        f.write("Процедура Тест()\nКонецПроцедуры\n")
    return root


def _make_edt_container(root: str, main_name: str = "edtmain") -> tuple[str, str]:
    """Container with a single EDT MAIN in a (non-'cf') direct subdir.

    Returns (container_path, effective_main_path)."""
    container = os.path.join(root, "src")
    main = os.path.join(container, main_name)
    os.makedirs(os.path.join(main, "Configuration"))
    with open(os.path.join(main, "Configuration", "Configuration.mdo"), "w", encoding="utf-8") as f:
        f.write(_EDT_MAIN_MDO)
    with open(os.path.join(main, "Module.bsl"), "w", encoding="utf-8") as f:
        f.write("Процедура Тест()\nКонецПроцедуры\n")
    return container, main


def _make_flat_proj(root: str) -> str:
    """Flat layout: registered path == effective root (has a direct cf)."""
    cf = os.path.join(root, "cf")
    os.makedirs(cf)
    with open(os.path.join(cf, "Configuration.xml"), "w", encoding="utf-8") as f:
        f.write(_CF_MAIN_XML)
    with open(os.path.join(cf, "Module.bsl"), "w", encoding="utf-8") as f:
        f.write("Процедура Тест()\nКонецПроцедуры\n")
    return cf


def test_drop_after_source_deleted_container_layout():
    from rlm_tools_bsl.server import _rlm_index
    from rlm_tools_bsl.bsl_index import get_index_db_path

    with tempfile.TemporaryDirectory() as tmpdir:
        src = _make_src_proj(tmpdir)
        idx_dir = os.path.join(tmpdir, "indexes")

        with patch.dict(os.environ, {"RLM_INDEX_DIR": idx_dir}):
            _rlm_index(action="build", path=src)
            cf_resolved = str((Path(src) / "cf").resolve())
            db_path = get_index_db_path(cf_resolved)
            assert db_path.exists()

            # Decommission: remove the whole source tree.
            shutil.rmtree(src)
            assert not os.path.isdir(src)

            r = json.loads(_rlm_index(action="drop", path=src))
            assert r.get("action") == "drop", f"expected drop, got {r}"
            assert not db_path.exists()


def test_drop_after_source_deleted_flat_layout():
    from rlm_tools_bsl.server import _rlm_index
    from rlm_tools_bsl.bsl_index import get_index_db_path

    with tempfile.TemporaryDirectory() as tmpdir:
        cf = _make_flat_proj(tmpdir)
        idx_dir = os.path.join(tmpdir, "indexes")

        with patch.dict(os.environ, {"RLM_INDEX_DIR": idx_dir}):
            _rlm_index(action="build", path=cf)
            db_path = get_index_db_path(str(Path(cf).resolve()))
            assert db_path.exists()

            shutil.rmtree(cf)
            r = json.loads(_rlm_index(action="drop", path=cf))
            assert r.get("action") == "drop", f"expected drop, got {r}"
            assert not db_path.exists()


def test_drop_after_source_deleted_edt_flat():
    """Format-agnostic: a flat EDT root drops fine after its sources are gone."""
    from rlm_tools_bsl.server import _rlm_index
    from rlm_tools_bsl.bsl_index import get_index_db_path

    with tempfile.TemporaryDirectory() as tmpdir:
        edt = _make_edt_root(os.path.join(tmpdir, "proj"))
        idx_dir = os.path.join(tmpdir, "indexes")

        with patch.dict(os.environ, {"RLM_INDEX_DIR": idx_dir}):
            _rlm_index(action="build", path=edt)
            db_path = get_index_db_path(str(Path(edt).resolve()))
            assert db_path.exists()

            shutil.rmtree(edt)
            r = json.loads(_rlm_index(action="drop", path=edt))
            assert r.get("action") == "drop", f"expected drop, got {r}"
            assert not db_path.exists()


def test_drop_after_source_deleted_edt_container_noncf():
    """Format-agnostic + non-'cf' main name: EDT container with a single MAIN in a
    direct subdir named 'edtmain' drops fine after deletion (single direct-child)."""
    from rlm_tools_bsl.server import _rlm_index
    from rlm_tools_bsl.bsl_index import get_index_db_path

    with tempfile.TemporaryDirectory() as tmpdir:
        container, main = _make_edt_container(tmpdir, main_name="edtmain")
        idx_dir = os.path.join(tmpdir, "indexes")

        with patch.dict(os.environ, {"RLM_INDEX_DIR": idx_dir}):
            r = json.loads(_rlm_index(action="build", path=container))
            # Effective root is the direct-child main dir.
            assert Path(r["path"]) == Path(main).resolve(), f"unexpected effective root: {r}"
            db_path = get_index_db_path(str(Path(main).resolve()))
            assert db_path.exists()

            shutil.rmtree(container)
            r = json.loads(_rlm_index(action="drop", path=container))
            assert r.get("action") == "drop", f"expected drop, got {r}"
            assert r.get("path") == str(Path(main).resolve())
            assert not db_path.exists()


def test_info_after_source_deleted():
    from rlm_tools_bsl.server import _rlm_index

    with tempfile.TemporaryDirectory() as tmpdir:
        src = _make_src_proj(tmpdir)
        idx_dir = os.path.join(tmpdir, "indexes")

        with patch.dict(os.environ, {"RLM_INDEX_DIR": idx_dir}):
            _rlm_index(action="build", path=src)
            shutil.rmtree(src)

            r = json.loads(_rlm_index(action="info", path=src))
            assert r.get("action") == "info", f"expected info, got {r}"
            assert "methods" in r


def test_drop_purges_project_cache():
    """drop removes the file-listing cache dir alongside the index DB."""
    from rlm_tools_bsl.server import _rlm_index
    from rlm_tools_bsl.cache import _cache_dir

    with tempfile.TemporaryDirectory() as tmpdir:
        cf = _make_flat_proj(tmpdir)
        idx_dir = os.path.join(tmpdir, "indexes")
        cfg = os.path.join(tmpdir, "service.json")

        with patch.dict(os.environ, {"RLM_INDEX_DIR": idx_dir, "RLM_CONFIG_FILE": cfg}):
            _rlm_index(action="build", path=cf)
            resolved = str(Path(cf).resolve())
            # Simulate a populated cache dir for this project.
            cache_dir = _cache_dir(resolved)
            cache_dir.mkdir(parents=True, exist_ok=True)
            (cache_dir / "file_index.json").write_text("{}", encoding="utf-8")
            assert cache_dir.is_dir()

            r = json.loads(_rlm_index(action="drop", path=cf))
            assert r.get("action") == "drop", f"expected drop, got {r}"
            assert not cache_dir.is_dir(), "cache dir should be purged on drop"
            assert r.get("dropped_cache") == str(cache_dir)


def test_recovery_ignores_nested_grandchild_index():
    """codex finding 1: a deeper (grandchild) index in the same tree must not
    make the container→cf recovery ambiguous. Only the direct-child cf wins."""
    from rlm_tools_bsl.server import _rlm_index
    from rlm_tools_bsl.bsl_index import get_index_db_path

    with tempfile.TemporaryDirectory() as tmpdir:
        src = _make_src_proj(tmpdir)  # <root>/src/cf (effective for container <root>/src)
        # A separately-built MAIN index deeper in the tree: <root>/src/nested/cf
        nested_cf = os.path.join(src, "nested", "cf")
        os.makedirs(nested_cf)
        with open(os.path.join(nested_cf, "Configuration.xml"), "w", encoding="utf-8") as f:
            f.write(_CF_MAIN_XML)
        with open(os.path.join(nested_cf, "Module.bsl"), "w", encoding="utf-8") as f:
            f.write("Процедура Тест()\nКонецПроцедуры\n")

        idx_dir = os.path.join(tmpdir, "indexes")
        with patch.dict(os.environ, {"RLM_INDEX_DIR": idx_dir}):
            _rlm_index(action="build", path=src)  # → <src>/cf
            _rlm_index(action="build", path=nested_cf)  # → <src>/nested/cf (grandchild)

            cf_resolved = str((Path(src) / "cf").resolve())
            db_path = get_index_db_path(cf_resolved)
            assert db_path.exists()

            shutil.rmtree(src)
            r = json.loads(_rlm_index(action="drop", path=src))
            assert r.get("action") == "drop", f"expected drop (direct-child cf), got {r}"
            assert not db_path.exists()


def test_recovery_prefers_cf_among_multiple_direct_children():
    """codex finding 1: several direct-child MAIN indexes → prefer the 'cf' one
    (mirrors resolve_config_root's cf tie-breaker)."""
    from rlm_tools_bsl.server import _rlm_index
    from rlm_tools_bsl.bsl_index import get_index_db_path

    with tempfile.TemporaryDirectory() as tmpdir:
        container = os.path.join(tmpdir, "cont")
        cf = os.path.join(container, "cf")
        cf_alt = os.path.join(container, "cf_alt")
        for d in (cf, cf_alt):
            os.makedirs(d)
            with open(os.path.join(d, "Configuration.xml"), "w", encoding="utf-8") as f:
                f.write(_CF_MAIN_XML)
            with open(os.path.join(d, "Module.bsl"), "w", encoding="utf-8") as f:
                f.write("Процедура Тест()\nКонецПроцедуры\n")

        idx_dir = os.path.join(tmpdir, "indexes")
        with patch.dict(os.environ, {"RLM_INDEX_DIR": idx_dir}):
            _rlm_index(action="build", path=cf)
            _rlm_index(action="build", path=cf_alt)
            cf_db = get_index_db_path(str(Path(cf).resolve()))
            assert cf_db.exists()

            shutil.rmtree(container)
            r = json.loads(_rlm_index(action="info", path=container))
            # Recovered to the 'cf' child, not cf_alt — assert the exact path so
            # the test actually proves the cf tie-breaker (cf_alt would also have
            # "methods" in its stats).
            assert r.get("action") == "info", f"expected info via cf child, got {r}"
            assert r.get("path") == str(Path(cf).resolve()), f"expected cf child, got {r.get('path')}"
            assert "methods" in r


def test_drop_purges_orphan_cache_when_db_already_gone():
    """codex finding 2: DB already deleted but file_index.json cache remains →
    drop still purges the orphan cache (best-effort), no bare 'Index not found'."""
    from rlm_tools_bsl.server import _rlm_index
    from rlm_tools_bsl.cache import _cache_dir

    with tempfile.TemporaryDirectory() as tmpdir:
        cf = _make_flat_proj(tmpdir)
        idx_dir = os.path.join(tmpdir, "indexes")
        cfg = os.path.join(tmpdir, "service.json")

        with patch.dict(os.environ, {"RLM_INDEX_DIR": idx_dir, "RLM_CONFIG_FILE": cfg}):
            resolved = str(Path(cf).resolve())
            # Orphan cache with no index DB ever/anymore.
            cache_dir = _cache_dir(resolved)
            cache_dir.mkdir(parents=True, exist_ok=True)
            (cache_dir / "file_index.json").write_text("{}", encoding="utf-8")

            r = json.loads(_rlm_index(action="drop", path=cf))
            assert r.get("action") == "drop", f"expected drop, got {r}"
            assert "dropped" not in r  # no DB was removed
            assert r.get("dropped_cache") == str(cache_dir)
            assert not cache_dir.is_dir()


def test_drop_missing_index_still_reports_not_found():
    """No index built + source gone → clean 'Index not found', not a crash."""
    from rlm_tools_bsl.server import _rlm_index

    with tempfile.TemporaryDirectory() as tmpdir:
        src = _make_src_proj(tmpdir)
        idx_dir = os.path.join(tmpdir, "indexes")

        with patch.dict(os.environ, {"RLM_INDEX_DIR": idx_dir}):
            # Never built. Delete source.
            shutil.rmtree(src)
            r = json.loads(_rlm_index(action="drop", path=src))
            assert "error" in r


def test_build_still_requires_existing_source():
    """build must NOT resurrect a deleted dir — recovery is drop/info only."""
    from rlm_tools_bsl.server import _rlm_index

    with tempfile.TemporaryDirectory() as tmpdir:
        src = _make_src_proj(tmpdir)
        idx_dir = os.path.join(tmpdir, "indexes")

        with patch.dict(os.environ, {"RLM_INDEX_DIR": idx_dir}):
            _rlm_index(action="build", path=src)
            shutil.rmtree(src)
            r = json.loads(_rlm_index(action="build", path=src))
            assert "error" in r
            assert "not found" in r["error"].lower()


@pytest.mark.asyncio
async def test_async_wrapper_drop_after_source_deleted():
    """End-to-end: async rlm_index(drop, project=…, confirm=…) after src deletion."""
    import threading as _th

    from rlm_tools_bsl.server import rlm_index, _rlm_projects, _build_jobs, _build_jobs_lock
    from rlm_tools_bsl.projects import _reset_registry
    from rlm_tools_bsl.bsl_index import get_index_db_path

    with tempfile.TemporaryDirectory() as tmpdir:
        src = _make_src_proj(tmpdir)
        idx_dir = os.path.join(tmpdir, "indexes")

        _reset_registry()
        with patch.dict(
            os.environ,
            {"RLM_CONFIG_FILE": os.path.join(tmpdir, "service.json"), "RLM_INDEX_DIR": idx_dir},
        ):
            _reset_registry()
            _rlm_projects(action="add", name="Gone", path=src, password="secret")
            await rlm_index(action="build", project="Gone", confirm="secret")
            for t in _th.enumerate():
                if t.name == "build-Gone":
                    t.join(timeout=30)

            cf_resolved = str((Path(src) / "cf").resolve())
            db_path = get_index_db_path(cf_resolved)
            assert db_path.exists()

            shutil.rmtree(src)
            r = json.loads(await rlm_index(action="drop", project="Gone", confirm="secret"))
            assert r.get("action") == "drop", f"expected drop, got {r}"
            assert not db_path.exists()
            with _build_jobs_lock:
                _build_jobs.clear()
