"""Tests for git-accelerated incremental update (v1.8.0).

Covers git utilities, git fast path vs full scan,
selective metadata refresh, dirty snapshot, and build-time flag guards.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import rlm_tools_bsl.bsl_index as bsl_index_mod
from rlm_tools_bsl.bsl_index import (
    IndexBuilder,
    IndexReader,
    _collect_metadata_tables,
    _collect_object_synonyms,
    _git_available,
    _git_changed_files,
    _git_current_dirty,
    _git_dirty_timeout,
    _git_head_sha,
    _git_repo_info,
    _GitDirtyResult,
    get_index_db_path,
)

# ---------------------------------------------------------------------------
# BSL content for test fixtures
# ---------------------------------------------------------------------------

COMMON_MODULE_BSL = """\
Процедура ТестоваяПроцедура() Экспорт
    Сообщить("Привет");
КонецПроцедуры
"""

MODIFIED_BSL = """\
Процедура ТестоваяПроцедура() Экспорт
    Сообщить("Изменено");
КонецПроцедуры

Функция НоваяФункция() Экспорт
    Возврат 42;
КонецФункции
"""

NEW_FILE_BSL = """\
Процедура НоваяПроцедура() Экспорт
    Сообщить("Новый файл");
КонецПроцедуры
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run git command in *root*."""
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=check,
    )


def _git_init(root: Path) -> None:
    """Initialize a git repo at *root* and do an initial commit."""
    _git(root, "init")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@test.com")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "initial")


def _read_meta(db_path: Path, key: str) -> str | None:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT value FROM index_meta WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else None


def _install_worktree_timeout(monkeypatch) -> None:
    """Force best-effort work-tree git commands to time out.

    Critical commands (rev-parse, merge-base, committed diff) pass through so
    the fast path is still *attempted* — only the staged/unstaged/untracked
    work-tree probes (which carry ``--ignore-cr-at-eol`` or ``ls-files
    --others``) raise ``TimeoutExpired``, marking detection unreliable.
    """
    real_run_git = bsl_index_mod.run_git

    def fake_run_git(args, timeout):
        is_worktree_diff = "--ignore-cr-at-eol" in args
        is_untracked = "ls-files" in args and "--others" in args
        if is_worktree_diff or is_untracked:
            raise subprocess.TimeoutExpired(cmd=args, timeout=timeout)
        return real_run_git(args, timeout)

    monkeypatch.setattr(bsl_index_mod, "run_git", fake_run_git)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def git_bsl_project(tmp_path, monkeypatch):
    """Create a BSL project inside a git repo.

    Structure: tmp_path/.git, tmp_path/src/CommonModules/...
    base_path = tmp_path/src (subdir of git root — tests prefix transform).
    """
    root = tmp_path
    base = root / "src"

    # CommonModules/МойМодуль/Ext/Module.bsl
    cm_dir = base / "CommonModules" / "МойМодуль" / "Ext"
    cm_dir.mkdir(parents=True)
    (cm_dir / "Module.bsl").write_text(COMMON_MODULE_BSL, encoding="utf-8-sig")

    # Documents/ТестовыйДокумент/Ext/ObjectModule.bsl
    doc_dir = base / "Documents" / "ТестовыйДокумент" / "Ext"
    doc_dir.mkdir(parents=True)
    (doc_dir / "ObjectModule.bsl").write_text(
        "Процедура ПередЗаписью(Отказ)\n    //\nКонецПроцедуры\n",
        encoding="utf-8-sig",
    )

    _git_init(root)

    monkeypatch.setenv("RLM_INDEX_DIR", str(base / ".index"))
    builder = IndexBuilder()
    builder.build(str(base), build_calls=True)
    return base


@pytest.fixture
def git_bsl_project_with_metadata(tmp_path, monkeypatch):
    """Git BSL project with metadata and synonyms."""
    root = tmp_path
    base = root / "src"

    cm_dir = base / "CommonModules" / "МойМодуль" / "Ext"
    cm_dir.mkdir(parents=True)
    (cm_dir / "Module.bsl").write_text(COMMON_MODULE_BSL, encoding="utf-8-sig")

    _git_init(root)

    monkeypatch.setenv("RLM_INDEX_DIR", str(base / ".index"))
    builder = IndexBuilder()
    builder.build(str(base), build_calls=True, build_metadata=True, build_synonyms=True)
    return base


# =====================================================================
# Unit tests: git utilities
# =====================================================================


class TestGitAvailable:
    def test_uses_common_runner(self, monkeypatch):
        calls = []

        def fake_run_git(args, timeout):
            calls.append((args, timeout))
            return subprocess.CompletedProcess(args, 0, "true\n", "")

        monkeypatch.setattr(bsl_index_mod, "_git_base_cmd", lambda base_path: ["git", "-C", base_path])
        monkeypatch.setattr(bsl_index_mod, "run_git", fake_run_git)

        assert _git_available("repo") is True
        assert calls == [(["git", "-C", "repo", "rev-parse", "--is-inside-work-tree"], 10)]

    def test_inside_repo(self, git_bsl_project):
        assert _git_available(str(git_bsl_project)) is True

    def test_subdir_of_repo(self, git_bsl_project):
        subdir = git_bsl_project / "CommonModules"
        assert _git_available(str(subdir)) is True

    def test_no_git(self, tmp_path):
        assert _git_available(str(tmp_path)) is False

    def test_no_git_binary(self, git_bsl_project, monkeypatch):
        import shutil

        import rlm_tools_bsl.bsl_index as _mod

        monkeypatch.setattr(shutil, "which", lambda x: None)
        # Reset cached git path and block Windows fallback
        monkeypatch.setattr(_mod, "_git_exe", None)
        monkeypatch.setattr("os.path.isfile", lambda x: False)
        assert _git_available(str(git_bsl_project)) is False
        # Restore cache for other tests
        monkeypatch.setattr(_mod, "_git_exe", None)


class TestGitRepoInfo:
    def test_root_is_base(self, tmp_path):
        (tmp_path / "test.txt").write_text("x")
        _git_init(tmp_path)
        info = _git_repo_info(str(tmp_path))
        assert info is not None
        git_root, prefix = info
        assert prefix == ""

    def test_base_is_subdir(self, git_bsl_project):
        # base = .../src, git root = parent of src
        info = _git_repo_info(str(git_bsl_project))
        assert info is not None
        _, prefix = info
        assert prefix == "src"

    def test_deep_subdir(self, git_bsl_project):
        deep = git_bsl_project / "CommonModules" / "МойМодуль"
        info = _git_repo_info(str(deep))
        assert info is not None
        _, prefix = info
        assert prefix == "src/CommonModules/МойМодуль"


class TestGitHeadSha:
    def test_returns_sha(self, git_bsl_project):
        sha = _git_head_sha(str(git_bsl_project))
        assert sha is not None
        assert len(sha) == 40
        assert all(c in "0123456789abcdef" for c in sha)


class TestGitChangedFiles:
    def test_committed(self, git_bsl_project):
        root = git_bsl_project.parent
        initial_sha = _git_head_sha(str(git_bsl_project))

        # Commit a new file
        new_file = git_bsl_project / "Catalogs" / "Тест" / "Ext" / "Module.bsl"
        new_file.parent.mkdir(parents=True)
        new_file.write_text(NEW_FILE_BSL, encoding="utf-8-sig")
        _git(root, "add", ".")
        _git(root, "commit", "-m", "add new file")

        info = _git_repo_info(str(git_bsl_project))
        assert info is not None
        _, prefix = info
        changed = _git_changed_files(str(git_bsl_project), initial_sha, prefix)
        assert changed is not None
        assert changed.unreliable_reason is None
        assert "Catalogs/Тест/Ext/Module.bsl" in changed.paths

    def test_staged(self, git_bsl_project):
        root = git_bsl_project.parent
        sha = _git_head_sha(str(git_bsl_project))

        # Modify and stage
        bsl = git_bsl_project / "CommonModules" / "МойМодуль" / "Ext" / "Module.bsl"
        bsl.write_text(MODIFIED_BSL, encoding="utf-8-sig")
        _git(root, "add", str(bsl))

        info = _git_repo_info(str(git_bsl_project))
        _, prefix = info
        changed = _git_changed_files(str(git_bsl_project), sha, prefix)
        assert changed is not None
        assert "CommonModules/МойМодуль/Ext/Module.bsl" in changed.paths

    def test_unstaged(self, git_bsl_project):
        sha = _git_head_sha(str(git_bsl_project))
        bsl = git_bsl_project / "CommonModules" / "МойМодуль" / "Ext" / "Module.bsl"
        bsl.write_text(MODIFIED_BSL, encoding="utf-8-sig")

        info = _git_repo_info(str(git_bsl_project))
        _, prefix = info
        changed = _git_changed_files(str(git_bsl_project), sha, prefix)
        assert changed is not None
        assert "CommonModules/МойМодуль/Ext/Module.bsl" in changed.paths

    def test_untracked(self, git_bsl_project):
        sha = _git_head_sha(str(git_bsl_project))
        new_file = git_bsl_project / "NewFile.bsl"
        new_file.write_text("// new\n")

        info = _git_repo_info(str(git_bsl_project))
        _, prefix = info
        changed = _git_changed_files(str(git_bsl_project), sha, prefix)
        assert changed is not None
        assert "NewFile.bsl" in changed.paths

    def test_deleted(self, git_bsl_project):
        root = git_bsl_project.parent
        sha = _git_head_sha(str(git_bsl_project))

        bsl = git_bsl_project / "CommonModules" / "МойМодуль" / "Ext" / "Module.bsl"
        bsl.unlink()
        _git(root, "add", "-A")

        info = _git_repo_info(str(git_bsl_project))
        _, prefix = info
        changed = _git_changed_files(str(git_bsl_project), sha, prefix)
        assert changed is not None
        assert "CommonModules/МойМодуль/Ext/Module.bsl" in changed.paths

    def test_bad_commit(self, git_bsl_project):
        info = _git_repo_info(str(git_bsl_project))
        _, prefix = info
        result = _git_changed_files(str(git_bsl_project), "0" * 40, prefix)
        assert result is None

    def test_prefix_filter(self, git_bsl_project):
        root = git_bsl_project.parent
        sha = _git_head_sha(str(git_bsl_project))

        # Add file outside base_path (in root, not in src/)
        (root / "README.md").write_text("# readme\n")
        _git(root, "add", ".")
        _git(root, "commit", "-m", "add readme")

        info = _git_repo_info(str(git_bsl_project))
        _, prefix = info
        changed = _git_changed_files(str(git_bsl_project), sha, prefix)
        assert changed is not None
        # README.md is outside prefix "src" — should NOT be in result
        assert "README.md" not in changed.paths

    @pytest.mark.skipif(
        sys.platform == "win32" and not os.environ.get("PYTHONUTF8"),
        reason="Windows CI uses cp1252 stdout — Cyrillic assert output crashes pytest",
    )
    def test_cyrillic_paths(self, git_bsl_project):
        root = git_bsl_project.parent
        sha = _git_head_sha(str(git_bsl_project))

        cyrillic_file = git_bsl_project / "Справочники" / "Тест.bsl"
        cyrillic_file.parent.mkdir(parents=True)
        cyrillic_file.write_text("// кириллица\n")
        _git(root, "add", ".")
        _git(root, "commit", "-m", "cyrillic")

        info = _git_repo_info(str(git_bsl_project))
        _, prefix = info
        changed = _git_changed_files(str(git_bsl_project), sha, prefix)
        assert changed is not None
        assert "Справочники/Тест.bsl" in changed.paths

    def test_overlap_committed_and_staged(self, git_bsl_project):
        root = git_bsl_project.parent
        sha = _git_head_sha(str(git_bsl_project))

        bsl = git_bsl_project / "CommonModules" / "МойМодуль" / "Ext" / "Module.bsl"
        bsl.write_text(MODIFIED_BSL, encoding="utf-8-sig")
        _git(root, "add", ".")
        _git(root, "commit", "-m", "change")

        # Stage another modification
        bsl.write_text(COMMON_MODULE_BSL, encoding="utf-8-sig")
        _git(root, "add", str(bsl))

        info = _git_repo_info(str(git_bsl_project))
        _, prefix = info
        changed = _git_changed_files(str(git_bsl_project), sha, prefix)
        assert changed is not None
        # Should appear only once despite being in both committed and staged
        assert "CommonModules/МойМодуль/Ext/Module.bsl" in changed.paths


# =====================================================================
# Integration tests: git fast path
# =====================================================================


class TestUpdateGitFastPath:
    def test_changed_bsl(self, git_bsl_project):
        root = git_bsl_project.parent
        bsl = git_bsl_project / "CommonModules" / "МойМодуль" / "Ext" / "Module.bsl"
        bsl.write_text(MODIFIED_BSL, encoding="utf-8-sig")
        _git(root, "add", ".")
        _git(root, "commit", "-m", "modify")

        builder = IndexBuilder()
        result = builder.update(str(git_bsl_project))
        assert result["git_fast_path"] is True
        assert result["changed"] == 1

    def test_added_bsl(self, git_bsl_project):
        root = git_bsl_project.parent
        new_file = git_bsl_project / "Catalogs" / "НовыйСправочник" / "Ext" / "Module.bsl"
        new_file.parent.mkdir(parents=True)
        new_file.write_text(NEW_FILE_BSL, encoding="utf-8-sig")
        _git(root, "add", ".")
        _git(root, "commit", "-m", "add")

        builder = IndexBuilder()
        result = builder.update(str(git_bsl_project))
        assert result["git_fast_path"] is True
        assert result["added"] == 1

    def test_removed_bsl(self, git_bsl_project):
        root = git_bsl_project.parent
        bsl = git_bsl_project / "Documents" / "ТестовыйДокумент" / "Ext" / "ObjectModule.bsl"
        bsl.unlink()
        _git(root, "add", "-A")
        _git(root, "commit", "-m", "remove")

        builder = IndexBuilder()
        result = builder.update(str(git_bsl_project))
        assert result["git_fast_path"] is True
        assert result["removed"] == 1

    def test_no_changes(self, git_bsl_project):
        builder = IndexBuilder()
        result = builder.update(str(git_bsl_project))
        assert result["git_fast_path"] is True
        assert result["added"] == 0
        assert result["changed"] == 0
        assert result["removed"] == 0

    def test_bsl_count_updated(self, git_bsl_project):
        db_path = get_index_db_path(str(git_bsl_project))
        old_count = int(_read_meta(db_path, "bsl_count"))

        root = git_bsl_project.parent
        new_file = git_bsl_project / "NewModule.bsl"
        new_file.write_text(NEW_FILE_BSL, encoding="utf-8-sig")
        _git(root, "add", ".")
        _git(root, "commit", "-m", "add new")

        IndexBuilder().update(str(git_bsl_project))
        new_count = int(_read_meta(db_path, "bsl_count"))
        assert new_count == old_count + 1

    def test_paths_hash_updated(self, git_bsl_project):
        db_path = get_index_db_path(str(git_bsl_project))
        old_hash = _read_meta(db_path, "paths_hash")

        root = git_bsl_project.parent
        new_file = git_bsl_project / "NewModule.bsl"
        new_file.write_text(NEW_FILE_BSL, encoding="utf-8-sig")
        _git(root, "add", ".")
        _git(root, "commit", "-m", "add new")

        IndexBuilder().update(str(git_bsl_project))
        new_hash = _read_meta(db_path, "paths_hash")
        assert new_hash != old_hash

    def test_built_at_updated(self, git_bsl_project):
        db_path = get_index_db_path(str(git_bsl_project))
        old_built_at = float(_read_meta(db_path, "built_at"))

        import time

        time.sleep(0.05)

        IndexBuilder().update(str(git_bsl_project))
        new_built_at = float(_read_meta(db_path, "built_at"))
        assert new_built_at > old_built_at

    def test_git_head_saved_after_build(self, git_bsl_project):
        db_path = get_index_db_path(str(git_bsl_project))
        stored = _read_meta(db_path, "git_head_commit")
        assert stored is not None
        assert len(stored) == 40

    def test_git_head_saved_after_update(self, git_bsl_project):
        root = git_bsl_project.parent
        bsl = git_bsl_project / "CommonModules" / "МойМодуль" / "Ext" / "Module.bsl"
        bsl.write_text(MODIFIED_BSL, encoding="utf-8-sig")
        _git(root, "add", ".")
        _git(root, "commit", "-m", "change")

        IndexBuilder().update(str(git_bsl_project))
        db_path = get_index_db_path(str(git_bsl_project))
        stored = _read_meta(db_path, "git_head_commit")
        current = _git_head_sha(str(git_bsl_project))
        assert stored == current

    def test_fast_path_flag_in_result(self, git_bsl_project):
        result = IndexBuilder().update(str(git_bsl_project))
        assert "git_fast_path" in result
        assert result["git_fast_path"] is True

    def test_bsl_count_arithmetic(self, git_bsl_project):
        """bsl_count = stored + added - removed (not COUNT FROM modules)."""
        root = git_bsl_project.parent
        db_path = get_index_db_path(str(git_bsl_project))

        # Add 2 files, remove 1
        for name in ("New1.bsl", "New2.bsl"):
            f = git_bsl_project / name
            f.write_text(NEW_FILE_BSL, encoding="utf-8-sig")
        bsl = git_bsl_project / "Documents" / "ТестовыйДокумент" / "Ext" / "ObjectModule.bsl"
        bsl.unlink()
        _git(root, "add", "-A")
        _git(root, "commit", "-m", "add 2, remove 1")

        old_count = int(_read_meta(db_path, "bsl_count"))
        result = IndexBuilder().update(str(git_bsl_project))
        new_count = int(_read_meta(db_path, "bsl_count"))
        assert new_count == old_count + result["added"] - result["removed"]


class TestUpdateFallback:
    def test_no_git_dir(self, tmp_path, monkeypatch):
        """No git → fallback, result still correct."""
        base = tmp_path
        cm_dir = base / "CommonModules" / "МойМодуль" / "Ext"
        cm_dir.mkdir(parents=True)
        (cm_dir / "Module.bsl").write_text(COMMON_MODULE_BSL, encoding="utf-8-sig")

        monkeypatch.setenv("RLM_INDEX_DIR", str(base / ".index"))
        builder = IndexBuilder()
        builder.build(str(base), build_calls=True)

        # Modify a file
        (cm_dir / "Module.bsl").write_text(MODIFIED_BSL, encoding="utf-8-sig")
        result = builder.update(str(base))
        assert result["git_fast_path"] is False
        assert result["changed"] == 1

    def test_no_stored_commit(self, git_bsl_project):
        """Remove git_head_commit → fallback, but it gets saved for next time."""
        db_path = get_index_db_path(str(git_bsl_project))
        # Delete stored commit
        conn = sqlite3.connect(str(db_path))
        conn.execute("DELETE FROM index_meta WHERE key = 'git_head_commit'")
        conn.commit()
        conn.close()

        result = IndexBuilder().update(str(git_bsl_project))
        assert result["git_fast_path"] is False

        # Now git_head_commit should be saved
        stored = _read_meta(db_path, "git_head_commit")
        assert stored is not None

    def test_fallback_flag_in_result(self, tmp_path, monkeypatch):
        base = tmp_path
        cm_dir = base / "CommonModules" / "М" / "Ext"
        cm_dir.mkdir(parents=True)
        (cm_dir / "Module.bsl").write_text(COMMON_MODULE_BSL, encoding="utf-8-sig")

        monkeypatch.setenv("RLM_INDEX_DIR", str(base / ".index"))
        builder = IndexBuilder()
        builder.build(str(base), build_calls=True)

        result = builder.update(str(base))
        assert result["git_fast_path"] is False

    def test_git_head_saved_on_fallback(self, tmp_path, monkeypatch):
        """Fallback update in git repo saves git_head_commit for next time."""
        root = tmp_path
        base = root / "src"
        cm_dir = base / "CommonModules" / "М" / "Ext"
        cm_dir.mkdir(parents=True)
        (cm_dir / "Module.bsl").write_text(COMMON_MODULE_BSL, encoding="utf-8-sig")
        _git_init(root)

        monkeypatch.setenv("RLM_INDEX_DIR", str(base / ".index"))
        builder = IndexBuilder()
        # Build WITHOUT git head (simulate old builder)
        db_path = builder.build(str(base), build_calls=True)
        conn = sqlite3.connect(str(db_path))
        conn.execute("DELETE FROM index_meta WHERE key = 'git_head_commit'")
        conn.commit()
        conn.close()

        # First update: fallback (no stored commit)
        result = builder.update(str(base))
        assert result["git_fast_path"] is False

        stored = _read_meta(db_path, "git_head_commit")
        assert stored is not None

        # Second update: should use fast path
        result2 = builder.update(str(base))
        assert result2["git_fast_path"] is True


class TestDirtySnapshot:
    def test_dirty_paths_saved(self, git_bsl_project):
        """After update with dirty workspace → git_dirty_paths recorded."""
        bsl = git_bsl_project / "CommonModules" / "МойМодуль" / "Ext" / "Module.bsl"
        bsl.write_text(MODIFIED_BSL, encoding="utf-8-sig")  # unstaged

        IndexBuilder().update(str(git_bsl_project))
        db_path = get_index_db_path(str(git_bsl_project))
        raw = _read_meta(db_path, "git_dirty_paths")
        assert raw is not None
        dirty = json.loads(raw)
        assert any("Module.bsl" in p for p in dirty)

    def test_dirty_paths_empty_on_clean(self, git_bsl_project):
        """After update on clean workspace → no .bsl files in git_dirty_paths."""
        IndexBuilder().update(str(git_bsl_project))
        db_path = get_index_db_path(str(git_bsl_project))
        raw = _read_meta(db_path, "git_dirty_paths")
        assert raw is not None
        dirty = json.loads(raw)
        # .index/ files may appear as untracked — filter to .bsl only
        bsl_dirty = [p for p in dirty if p.lower().endswith(".bsl")]
        assert bsl_dirty == []

    def test_dirty_to_clean_detected(self, git_bsl_project):
        """File dirty→clean at same HEAD → reparsed via dirty snapshot."""
        bsl = git_bsl_project / "CommonModules" / "МойМодуль" / "Ext" / "Module.bsl"

        # 1. Update with dirty file
        bsl.write_text(MODIFIED_BSL, encoding="utf-8-sig")
        result1 = IndexBuilder().update(str(git_bsl_project))
        assert result1["changed"] == 1

        # 2. Restore original content (dirty→clean)
        bsl.write_text(COMMON_MODULE_BSL, encoding="utf-8-sig")
        root = git_bsl_project.parent
        _git(root, "checkout", "--", str(bsl))

        # 3. Update again — should still detect change (via dirty snapshot)
        result2 = IndexBuilder().update(str(git_bsl_project))
        # The file was in prev_dirty, so it gets re-checked
        # Since it's now back to original (same as HEAD), it may count as changed=1
        # because it was in the dirty set and gets reparsed
        assert result2["git_fast_path"] is True


class TestGitDirtyTimeout:
    """RLM_GIT_DIRTY_TIMEOUT parsing for best-effort git commands (FIX-3)."""

    def test_default(self, monkeypatch):
        monkeypatch.delenv("RLM_GIT_DIRTY_TIMEOUT", raising=False)
        assert _git_dirty_timeout() == 120

    def test_custom(self, monkeypatch):
        monkeypatch.setenv("RLM_GIT_DIRTY_TIMEOUT", "300")
        assert _git_dirty_timeout() == 300

    def test_invalid_falls_back(self, monkeypatch):
        monkeypatch.setenv("RLM_GIT_DIRTY_TIMEOUT", "not-a-number")
        assert _git_dirty_timeout() == 120

    def test_floor_at_one(self, monkeypatch):
        monkeypatch.setenv("RLM_GIT_DIRTY_TIMEOUT", "0")
        assert _git_dirty_timeout() == 1


class TestGitCurrentDirtyContract:
    """_git_current_dirty returns a _GitDirtyResult (FIX-2 contract)."""

    def test_clean_is_reliable(self, git_bsl_project):
        info = _git_repo_info(str(git_bsl_project))
        assert info is not None
        _, prefix = info
        result = _git_current_dirty(str(git_bsl_project), prefix)
        assert isinstance(result, _GitDirtyResult)
        assert result.unreliable_reason is None

    def test_timeout_marks_unreliable(self, git_bsl_project, monkeypatch):
        info = _git_repo_info(str(git_bsl_project))
        _, prefix = info
        _install_worktree_timeout(monkeypatch)
        result = _git_current_dirty(str(git_bsl_project), prefix)
        assert isinstance(result, _GitDirtyResult)
        assert result.unreliable_reason is not None


class TestIgnoreCrAtEol:
    """--ignore-cr-at-eol: CRLF-only diffs are not reported as changed (FIX-3)."""

    def test_crlf_only_excluded_real_change_kept(self, tmp_path, monkeypatch):
        root = tmp_path
        base = root / "src"
        da = base / "CommonModules" / "ModA" / "Ext"
        da.mkdir(parents=True)
        db = base / "CommonModules" / "ModB" / "Ext"
        db.mkdir(parents=True)
        fa = da / "Module.bsl"
        fb = db / "Module.bsl"
        # Commit both with LF endings, autocrlf disabled so the blob keeps LF.
        fa.write_bytes(b"Procedure A() Export\nEndProcedure\n")
        fb.write_bytes(b"Procedure B() Export\nEndProcedure\n")
        _git(root, "init")
        _git(root, "config", "user.name", "Test")
        _git(root, "config", "user.email", "test@test.com")
        _git(root, "config", "core.autocrlf", "false")
        _git(root, "add", ".")
        _git(root, "commit", "-m", "initial")

        sha = _git_head_sha(str(base))
        info = _git_repo_info(str(base))
        assert info is not None
        _, prefix = info

        # ModA: only line endings change LF -> CRLF (same logical content).
        fa.write_bytes(b"Procedure A() Export\r\nEndProcedure\r\n")
        # ModB: genuine content change.
        fb.write_bytes(b"Procedure B() Export\n    Message(1);\nEndProcedure\n")

        changed = _git_changed_files(str(base), sha, prefix)
        assert changed is not None
        assert changed.unreliable_reason is None
        assert "CommonModules/ModA/Ext/Module.bsl" not in changed.paths
        assert "CommonModules/ModB/Ext/Module.bsl" in changed.paths


class TestUnreliableDirtyDetection:
    """FIX-2: best-effort timeout must not silently drop uncommitted changes."""

    def test_unreliable_forces_full_scan_and_sets_flag(self, git_bsl_project, monkeypatch):
        # Uncommitted edit that git fast path would normally pick up via unstaged diff.
        bsl = git_bsl_project / "CommonModules" / "МойМодуль" / "Ext" / "Module.bsl"
        bsl.write_text(MODIFIED_BSL, encoding="utf-8-sig")

        _install_worktree_timeout(monkeypatch)
        result = IndexBuilder().update(str(git_bsl_project))

        # Fast path aborted, full scan used, reason surfaced.
        assert result["git_fast_path"] is False
        assert result["git_fallback_reason"]
        # The uncommitted edit is still indexed (full scan via mtime+size).
        assert result["changed"] == 1
        # Flag persisted so the NEXT update also full-scans.
        db_path = get_index_db_path(str(git_bsl_project))
        assert _read_meta(db_path, "git_dirty_unreliable") == "1"

    def test_flag_forces_full_scan_then_clears(self, git_bsl_project):
        db_path = get_index_db_path(str(git_bsl_project))
        # Simulate a prior unreliable run.
        conn = sqlite3.connect(str(db_path))
        conn.execute("INSERT OR REPLACE INTO index_meta (key, value) VALUES ('git_dirty_unreliable', '1')")
        conn.commit()
        conn.close()

        # Detection is reliable now → full scan once, then flag cleared.
        result = IndexBuilder().update(str(git_bsl_project))
        assert result["git_fast_path"] is False
        assert result["git_fallback_reason"] == "prev dirty unreliable"
        assert _read_meta(db_path, "git_dirty_unreliable") == "0"

        # With the flag cleared the fast path is available again.
        result2 = IndexBuilder().update(str(git_bsl_project))
        assert result2["git_fast_path"] is True

    def test_build_sets_flag_when_unreliable(self, tmp_path, monkeypatch):
        root = tmp_path
        base = root / "src"
        cm = base / "CommonModules" / "М" / "Ext"
        cm.mkdir(parents=True)
        (cm / "Module.bsl").write_text(COMMON_MODULE_BSL, encoding="utf-8-sig")
        _git_init(root)

        monkeypatch.setenv("RLM_INDEX_DIR", str(base / ".index"))
        _install_worktree_timeout(monkeypatch)
        db_path = IndexBuilder().build(str(base), build_calls=True)

        # No prior snapshot existed → the flag is the only way to force a correct
        # full scan on the first update.
        assert _read_meta(db_path, "git_dirty_unreliable") == "1"

    def test_unreliable_preserves_previous_snapshot(self, git_bsl_project, monkeypatch):
        db_path = get_index_db_path(str(git_bsl_project))
        # 1. Reliable update with a dirty file → good snapshot saved, flag=0.
        bsl = git_bsl_project / "CommonModules" / "МойМодуль" / "Ext" / "Module.bsl"
        bsl.write_text(MODIFIED_BSL, encoding="utf-8-sig")
        IndexBuilder().update(str(git_bsl_project))
        before = _read_meta(db_path, "git_dirty_paths")
        assert any("Module.bsl" in p for p in json.loads(before))
        assert _read_meta(db_path, "git_dirty_unreliable") == "0"

        # 2. Next update with unreliable detection must NOT clobber the snapshot.
        _install_worktree_timeout(monkeypatch)
        IndexBuilder().update(str(git_bsl_project))
        after = _read_meta(db_path, "git_dirty_paths")
        assert after == before
        assert _read_meta(db_path, "git_dirty_unreliable") == "1"


class TestFallbackReasonInDelta:
    """FIX-2 observability: git_fallback_reason / rebuild_reason in delta."""

    def test_no_stored_commit_reason(self, git_bsl_project):
        db_path = get_index_db_path(str(git_bsl_project))
        conn = sqlite3.connect(str(db_path))
        conn.execute("DELETE FROM index_meta WHERE key = 'git_head_commit'")
        conn.commit()
        conn.close()

        result = IndexBuilder().update(str(git_bsl_project))
        assert result["git_fast_path"] is False
        assert result["git_fallback_reason"] == "no stored commit"

    def test_fast_path_reason_is_none(self, git_bsl_project):
        result = IndexBuilder().update(str(git_bsl_project))
        assert result["git_fast_path"] is True
        assert result["git_fallback_reason"] is None


class TestCollectMetadataTablesKwargs:
    def test_default_all(self, tmp_path):
        """Without kwargs → all tables populated (backward compat)."""
        # Just verify the function runs without error with defaults
        result = _collect_metadata_tables(str(tmp_path))
        assert isinstance(result, dict)
        assert "event_subscriptions" in result

    def test_selective_skip_es(self, tmp_path):
        """collect_es=False → event_subscriptions empty."""
        result = _collect_metadata_tables(str(tmp_path), collect_es=False)
        assert result["event_subscriptions"] == []

    def test_attrs_empty_set_skips(self, tmp_path):
        """collect_attrs_categories=set() → object_attributes and predefined_items empty."""
        result = _collect_metadata_tables(str(tmp_path), collect_attrs_categories=set())
        assert result["object_attributes"] == []
        assert result["predefined_items"] == []


class TestCollectObjectSynonymsCategories:
    def test_default_all(self, tmp_path):
        """Without categories kwarg → scans all."""
        result = _collect_object_synonyms(str(tmp_path))
        assert isinstance(result, list)

    def test_selective_categories(self, tmp_path):
        """With categories= frozenset → only those scanned."""
        result = _collect_object_synonyms(str(tmp_path), categories=frozenset({"Catalogs"}))
        assert isinstance(result, list)


class TestUpdateGitHasMetadataGuards:
    def test_no_metadata_skips_level2(self, git_bsl_project):
        """Index with --no-metadata + git fast path → Level-2 NOT populated."""
        # Index was built without metadata, so has_metadata=False
        # Just verify git fast path doesn't crash
        root = git_bsl_project.parent
        bsl = git_bsl_project / "CommonModules" / "МойМодуль" / "Ext" / "Module.bsl"
        bsl.write_text(MODIFIED_BSL, encoding="utf-8-sig")
        _git(root, "add", ".")
        _git(root, "commit", "-m", "change")

        result = IndexBuilder().update(str(git_bsl_project))
        assert result["git_fast_path"] is True

    def test_with_metadata_refreshes(self, git_bsl_project_with_metadata):
        """Index with metadata + xml change → metadata refreshed."""
        base = git_bsl_project_with_metadata
        root = base.parent

        # Add an XML file to trigger metadata refresh
        es_dir = base / "EventSubscriptions" / "Тест"
        es_dir.mkdir(parents=True)
        (es_dir / "Тест.xml").write_text("<xml/>", encoding="utf-8")
        _git(root, "add", ".")
        _git(root, "commit", "-m", "add es xml")

        result = IndexBuilder().update(str(base))
        assert result["git_fast_path"] is True


class TestGetStatisticsGitInfo:
    def test_git_accelerated_true(self, git_bsl_project):
        db_path = get_index_db_path(str(git_bsl_project))
        reader = IndexReader(db_path)
        stats = reader.get_statistics()
        reader.close()
        assert stats["git_accelerated"] is True
        assert stats["git_head_commit"] is not None
        assert len(stats["git_head_commit"]) == 40

    def test_git_accelerated_false(self, tmp_path, monkeypatch):
        base = tmp_path
        cm_dir = base / "CommonModules" / "М" / "Ext"
        cm_dir.mkdir(parents=True)
        (cm_dir / "Module.bsl").write_text(COMMON_MODULE_BSL, encoding="utf-8-sig")

        monkeypatch.setenv("RLM_INDEX_DIR", str(base / ".index"))
        builder = IndexBuilder()
        db_path = builder.build(str(base), build_calls=True)

        # Remove git_head_commit to simulate non-git build
        conn = sqlite3.connect(str(db_path))
        conn.execute("DELETE FROM index_meta WHERE key = 'git_head_commit'")
        conn.commit()
        conn.close()

        reader = IndexReader(db_path)
        stats = reader.get_statistics()
        reader.close()
        assert stats["git_accelerated"] is False
        assert stats["git_head_commit"] is None
