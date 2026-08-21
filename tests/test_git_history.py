"""Tests for git commit-history helpers (v1.34.0).

Covers ``_git_log`` / ``_git_commit`` (path scoping to base_path, sanitisation,
pickaxe, name-status/numstat, rev guard) and helper-level wiring (registration
gating, error-dicts, snapshot / routing).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rlm_tools_bsl.bsl_index as bsl_index_mod
from rlm_tools_bsl.bsl_index import (
    _git_commit,
    _git_log,
    _sanitize_git_rev,
    _sanitize_git_text_filter,
)
from rlm_tools_bsl.bsl_helpers import (
    build_helper_metadata_snapshot,
    make_bsl_helpers,
)
from rlm_tools_bsl.format_detector import detect_format
from rlm_tools_bsl.helpers import make_helpers

MARKER = "HISTTOKEN"


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=check,
    )


def _git_init(root: Path) -> None:
    _git(root, "init")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@test.com")


def _make_history_repo(tmp_path: Path) -> Path:
    """CF-style project under ``tmp_path/src``; git root is ``tmp_path``.

    Three commits:
      1. initial module (subject ``initial``, body mentions gitsync author)
      2. add Form.xml
      3. replace MARKER in the module (pickaxe target)
    Plus a root-only README commit that must NOT leak into base-scoped log.
    """
    base = tmp_path / "src"
    cm = base / "CommonModules" / "МойМодуль" / "Ext"
    cm.mkdir(parents=True)
    (cm / "Module.bsl").write_text(
        f'Процедура Тест() Экспорт\n    Значение = "{MARKER}";\nКонецПроцедуры\n',
        encoding="utf-8",
    )
    (base / "Configuration.xml").write_text("<Configuration/>", encoding="utf-8")

    _git_init(tmp_path)
    _git(tmp_path, "add", ".")
    _git(
        tmp_path,
        "commit",
        "-m",
        "initial\n\nАвтор хранилища: Иванов\nКомментарий: первая выгрузка",
    )

    (tmp_path / "README.md").write_text("repo root only\n", encoding="utf-8")
    _git(tmp_path, "add", "README.md")
    _git(tmp_path, "commit", "-m", "docs: readme at git root")

    form = base / "Documents" / "ТестовыйДокумент" / "Ext"
    form.mkdir(parents=True)
    (form / "Form.xml").write_text(f'<Form name="{MARKER}"/>\n', encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "add form xml")

    (cm / "Module.bsl").write_text(
        'Процедура Тест() Экспорт\n    Значение = "CHANGED";\nКонецПроцедуры\n',
        encoding="utf-8",
    )
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "replace marker in module")

    return base


def _make_bsl(base: Path, **kwargs) -> dict:
    helpers, resolve_safe = make_helpers(str(base))
    format_info = detect_format(str(base))
    return make_bsl_helpers(
        base_path=str(base),
        resolve_safe=resolve_safe,
        read_file_fn=helpers["read_file"],
        grep_fn=helpers["grep"],
        glob_files_fn=helpers["glob_files"],
        format_info=format_info,
        **kwargs,
    )


@pytest.fixture
def repo(tmp_path):
    return _make_history_repo(tmp_path)


# ---------------------------------------------------------------------------
# Sanitisers
# ---------------------------------------------------------------------------


def test_sanitize_git_rev():
    assert _sanitize_git_rev("HEAD") == "HEAD"
    assert _sanitize_git_rev("HEAD~3") == "HEAD~3"
    assert _sanitize_git_rev("HEAD^1") == "HEAD^1"
    assert _sanitize_git_rev("abc1234") == "abc1234"
    assert _sanitize_git_rev("deadbeef" * 5) == "deadbeef" * 5  # 40 hex
    for bad in ("", "main", "origin/master", "HEAD~1000", "-HEAD", "abc\n1234", "HEAD;rm"):
        assert _sanitize_git_rev(bad) is None, bad


def test_sanitize_git_text_filter():
    assert _sanitize_git_text_filter("", 80) == ""
    assert _sanitize_git_text_filter("2 weeks ago", 80) == "2 weeks ago"
    assert _sanitize_git_text_filter("Иванов", 80) == "Иванов"
    assert _sanitize_git_text_filter("a\nb", 80) is None
    assert _sanitize_git_text_filter("x" * 81, 80) is None


# ---------------------------------------------------------------------------
# _git_log core
# ---------------------------------------------------------------------------


def test_git_log_lists_config_commits_not_root_only(repo):
    log = _git_log(str(repo), limit=20)
    assert log is not None
    subjects = [c["subject"] for c in log]
    assert "initial" in subjects
    assert "add form xml" in subjects
    assert "replace marker in module" in subjects
    assert "docs: readme at git root" not in subjects


def test_git_log_path_filter(repo):
    log = _git_log(str(repo), path="Documents")
    subjects = [c["subject"] for c in log]
    assert "add form xml" in subjects
    assert "replace marker in module" not in subjects
    assert "initial" not in subjects


def test_git_log_grep_commit_message(repo):
    log = _git_log(str(repo), grep="Иванов")
    assert len(log) == 1
    assert log[0]["subject"] == "initial"
    assert "Иванов" in log[0]["body"]


def test_git_log_malformed_path_is_none(repo):
    assert _git_log(str(repo), path=":/") is None
    assert _git_log(str(repo), path="CommonModules*") is None


def test_git_log_limit(repo):
    log = _git_log(str(repo), limit=1)
    assert len(log) == 1
    assert log[0]["subject"] == "replace marker in module"


def test_git_pickaxe_finds_add_and_remove(repo):
    hits = _git_log(str(repo), pickaxe=MARKER, path="CommonModules")
    assert hits is not None
    subjects = {c["subject"] for c in hits}
    assert "initial" in subjects
    assert "replace marker in module" in subjects
    assert "add form xml" not in subjects


# ---------------------------------------------------------------------------
# _git_commit core
# ---------------------------------------------------------------------------


def test_git_commit_head_names_are_base_relative(repo):
    info = _git_commit(str(repo), "HEAD")
    assert info is not None
    assert info["subject"] == "replace marker in module"
    files = {f["file"] for f in info["files"]}
    assert any(f.endswith("Module.bsl") for f in files)
    assert all(not f.startswith("src/") for f in files)
    assert info["files"][0]["status"] in {"M", "A"}


def test_git_commit_stat_mode(repo):
    info = _git_commit(str(repo), "HEAD", mode="stat")
    assert info is not None
    assert info["mode"] == "stat"
    assert info["files"]
    rec = info["files"][0]
    assert "added" in rec and "deleted" in rec
    assert rec["file"].endswith("Module.bsl")


def test_git_commit_path_filter(repo):
    # The form-adding commit is HEAD~1
    info = _git_commit(str(repo), "HEAD~1", path="Documents")
    assert info is not None
    files = [f["file"] for f in info["files"]]
    assert files
    assert all(f.startswith("Documents/") for f in files)


def test_git_commit_bad_rev_is_none(repo):
    assert _git_commit(str(repo), "not-a-rev") is None
    assert _git_commit(str(repo), "main") is None


def test_git_commit_does_not_list_root_readme(repo):
    """HEAD~2 is the root-only README commit — scoped to base it has no files."""
    info = _git_commit(str(repo), "HEAD~2")
    assert info is not None
    assert info["subject"] == "docs: readme at git root"
    assert info["files"] == []


# ---------------------------------------------------------------------------
# Helper-level contract
# ---------------------------------------------------------------------------


def test_helpers_registered_on_git_repo(repo):
    bsl = _make_bsl(repo)
    for name in ("git_search", "git_log", "git_commit", "git_pickaxe"):
        assert name in bsl["_registry"]


def test_git_log_helper_returns_commits(repo):
    bsl = _make_bsl(repo)
    log = bsl["git_log"](limit=5)
    assert isinstance(log, list) and log
    assert "error" not in log[0]
    assert {"sha", "short", "date", "author", "subject", "body"} <= set(log[0])


def test_git_log_helper_bad_path(repo):
    bsl = _make_bsl(repo)
    out = bsl["git_log"](path=":/")
    assert out[0]["error"]


def test_git_commit_helper_head(repo):
    bsl = _make_bsl(repo)
    info = bsl["git_commit"]("HEAD")
    assert "error" not in info
    assert info["files"]
    assert info["mode"] == "names"


def test_git_commit_helper_invalid_rev(repo):
    bsl = _make_bsl(repo)
    err = bsl["git_commit"]("main")
    assert err["error"] == "invalid revision"
    empty = bsl["git_commit"]("")
    assert empty["error"] == "empty revision"
    bad_mode = bsl["git_commit"]("HEAD", mode="patch")
    assert bad_mode["error"] == "invalid mode"


def test_git_pickaxe_helper(repo):
    bsl = _make_bsl(repo)
    hits = bsl["git_pickaxe"](MARKER, path="CommonModules")
    assert isinstance(hits, list) and hits
    assert "error" not in hits[0]
    empty = bsl["git_pickaxe"]("")
    assert empty[0]["error"] == "empty pattern"


def test_no_git_excludes_history_helpers(tmp_path):
    base = tmp_path / "src"
    base.mkdir()
    (base / "Configuration.xml").write_text("<Configuration/>", encoding="utf-8")
    bsl = _make_bsl(base)
    for name in ("git_search", "git_log", "git_commit", "git_pickaxe"):
        assert name not in bsl["_registry"]


def test_register_never_excludes_history(repo):
    bsl = _make_bsl(repo, register_git_search="never")
    for name in ("git_search", "git_log", "git_commit", "git_pickaxe"):
        assert name not in bsl["_registry"]


def test_snapshot_documents_history_helpers():
    snap = build_helper_metadata_snapshot()
    for name in ("git_log", "git_commit", "git_pickaxe"):
        assert name in snap
        assert snap[name]["cat"] == "navigation"
        assert snap[name]["recipe"]


def test_routing_mentions_history_when_registered():
    from rlm_tools_bsl.bsl_knowledge import _git_search_routing

    assert _git_search_routing(None) == ""
    note = _git_search_routing({"git_search": {}})
    assert "FULL-TEXT SEARCH" in note and "git_search" in note
    assert "git_log" not in note
    full = _git_search_routing({"git_search": {}, "git_log": {}})
    assert "git_log" in full and "git_pickaxe" in full
    assert "git_search = current snapshot" in full


def test_git_log_error_dict_on_timeout(repo, monkeypatch):
    monkeypatch.setattr(bsl_index_mod, "_git_log", lambda *a, **k: None)
    bsl = _make_bsl(repo)
    out = bsl["git_log"]()
    assert out[0]["error"]
