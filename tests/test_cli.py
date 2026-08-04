"""Tests for CLI commands (rlm-bsl-index index build/update/info/drop)."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from argparse import Namespace
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


_CF_MAIN_XML_FOR_CLI = textwrap.dedent("""\
    <?xml version="1.0" encoding="UTF-8"?>
    <MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">
        <Configuration uuid="00000000-0000-0000-0000-000000000001">
            <Properties>
                <Name>Main</Name>
                <NamePrefix/>
            </Properties>
        </Configuration>
    </MetaDataObject>
""")


def test_cli_resolve_path_normalizes_container(tmp_path):
    """Codex Medium: CLI must cf-normalize container paths (src → src/cf)."""
    from rlm_tools_bsl.cli import _resolve_path

    src = tmp_path / "src"
    cf = src / "cf"
    cf.mkdir(parents=True)
    (cf / "Configuration.xml").write_text(_CF_MAIN_XML_FOR_CLI, encoding="utf-8")

    resolved = _resolve_path(str(src))
    assert Path(resolved) == cf.resolve()


def test_cli_resolve_path_fails_fast_on_ambiguous_main(tmp_path, capsys):
    """Multiple MAIN without cf tie-breaker → CLI exits non-zero with candidate list."""
    from rlm_tools_bsl.cli import _resolve_path

    src = tmp_path / "src"
    for name in ("main1", "main2"):
        sub = src / name
        sub.mkdir(parents=True)
        (sub / "Configuration.xml").write_text(_CF_MAIN_XML_FOR_CLI, encoding="utf-8")

    with pytest.raises(SystemExit) as excinfo:
        _resolve_path(str(src))
    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert "multiple main configurations" in err.lower()


def test_cli_resolve_path_passes_through_cf_root(tmp_path):
    """Direct cf-root path is unchanged (backward compatibility)."""
    from rlm_tools_bsl.cli import _resolve_path

    cf = tmp_path / "cf"
    cf.mkdir()
    (cf / "Configuration.xml").write_text(_CF_MAIN_XML_FOR_CLI, encoding="utf-8")

    resolved = _resolve_path(str(cf))
    assert Path(resolved) == cf.resolve()


@pytest.fixture()
def cli_bsl_project(tmp_path, monkeypatch):
    """Create a minimal BSL project and set RLM_INDEX_DIR."""
    # CF-format structure. Валидный дескриптор обязателен с v1.32.0: без него
    # `index build` упрётся в гейт чужого формата, а не в свой предмет.
    (tmp_path / "Configuration.xml").write_text(_CF_MAIN_XML_FOR_CLI, encoding="utf-8")
    mod_dir = tmp_path / "CommonModules" / "TestModule" / "Ext"
    mod_dir.mkdir(parents=True)
    (mod_dir / "Module.bsl").write_text(
        textwrap.dedent("""\
            Процедура ТестоваяПроцедура() Экспорт
                Возврат;
            КонецПроцедуры

            Функция ТестоваяФункция(Параметр)
                Возврат Параметр;
            КонецФункции
        """),
        encoding="utf-8-sig",
    )
    idx_dir = tmp_path / "_index"
    idx_dir.mkdir()
    monkeypatch.setenv("RLM_INDEX_DIR", str(idx_dir))
    return tmp_path, idx_dir


def _run_cli(*args: str, env_override: dict | None = None) -> subprocess.CompletedProcess:
    """Run CLI command and return result."""
    env = os.environ.copy()
    if env_override:
        env.update(env_override)
    return subprocess.run(
        [sys.executable, "-m", "rlm_tools_bsl.cli", *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


class TestCliBuild:
    def test_cli_build(self, cli_bsl_project):
        project_path, idx_dir = cli_bsl_project
        result = _run_cli(
            "index",
            "build",
            str(project_path),
            env_override={"RLM_INDEX_DIR": str(idx_dir)},
        )
        assert result.returncode == 0
        assert "Индекс построен" in result.stdout or "methods" in result.stdout.lower()
        # DB file should exist
        db_files = list(idx_dir.rglob("bsl_index.db"))
        assert len(db_files) == 1

    def test_cli_build_no_calls(self, cli_bsl_project):
        project_path, idx_dir = cli_bsl_project
        result = _run_cli(
            "index",
            "build",
            "--no-calls",
            str(project_path),
            env_override={"RLM_INDEX_DIR": str(idx_dir)},
        )
        assert result.returncode == 0
        db_files = list(idx_dir.rglob("bsl_index.db"))
        assert len(db_files) == 1


class TestCliUpdate:
    def test_cli_update(self, cli_bsl_project):
        project_path, idx_dir = cli_bsl_project
        # First build
        _run_cli(
            "index",
            "build",
            str(project_path),
            env_override={"RLM_INDEX_DIR": str(idx_dir)},
        )
        # Then update
        result = _run_cli(
            "index",
            "update",
            str(project_path),
            env_override={"RLM_INDEX_DIR": str(idx_dir)},
        )
        assert result.returncode == 0


class TestCliInfo:
    def test_cli_info(self, cli_bsl_project):
        project_path, idx_dir = cli_bsl_project
        # Build first
        _run_cli(
            "index",
            "build",
            str(project_path),
            env_override={"RLM_INDEX_DIR": str(idx_dir)},
        )
        result = _run_cli(
            "index",
            "info",
            str(project_path),
            env_override={"RLM_INDEX_DIR": str(idx_dir)},
        )
        assert result.returncode == 0
        # Should contain stats
        out = result.stdout.lower()
        assert "модул" in out or "метод" in out or "module" in out or "method" in out


class TestCliDrop:
    def test_cli_drop(self, cli_bsl_project):
        project_path, idx_dir = cli_bsl_project
        # Build first
        _run_cli(
            "index",
            "build",
            str(project_path),
            env_override={"RLM_INDEX_DIR": str(idx_dir)},
        )
        db_files = list(idx_dir.rglob("bsl_index.db"))
        assert len(db_files) == 1
        # Drop
        result = _run_cli(
            "index",
            "drop",
            str(project_path),
            env_override={"RLM_INDEX_DIR": str(idx_dir)},
        )
        assert result.returncode == 0
        db_files = list(idx_dir.rglob("bsl_index.db"))
        assert len(db_files) == 0


# ---------------------------------------------------------------------------
# Direct (in-process) tests — these DO count towards coverage.
# The subprocess-based tests above verify end-to-end CLI behaviour but
# coverage collectors only see this parent process.
# ---------------------------------------------------------------------------


# --- _fmt_size / _fmt_age ---------------------------------------------------


def test_fmt_size_bytes():
    from rlm_tools_bsl.cli import _fmt_size

    assert _fmt_size(0) == "0 B"
    assert _fmt_size(512) == "512 B"
    assert _fmt_size(1023) == "1023 B"


def test_fmt_size_kb():
    from rlm_tools_bsl.cli import _fmt_size

    assert _fmt_size(1024) == "1.0 KB"
    assert _fmt_size(2048) == "2.0 KB"
    assert _fmt_size(1024 * 1024 - 1) == "1024.0 KB"


def test_fmt_size_mb():
    from rlm_tools_bsl.cli import _fmt_size

    assert _fmt_size(1024 * 1024) == "1.0 MB"
    assert _fmt_size(10 * 1024 * 1024) == "10.0 MB"


def test_fmt_age_seconds():
    from rlm_tools_bsl.cli import _fmt_age

    assert _fmt_age(0) == "0s ago"
    assert _fmt_age(30) == "30s ago"
    assert _fmt_age(59.4) == "59s ago"


def test_fmt_age_minutes():
    from rlm_tools_bsl.cli import _fmt_age

    assert _fmt_age(60) == "1m ago"
    assert _fmt_age(3599) == "60m ago"


def test_fmt_age_hours():
    from rlm_tools_bsl.cli import _fmt_age

    assert _fmt_age(3600) == "1.0h ago"
    assert _fmt_age(86399) == "24.0h ago"


def test_fmt_age_days():
    from rlm_tools_bsl.cli import _fmt_age

    assert _fmt_age(86400) == "1.0d ago"
    assert _fmt_age(7 * 86400) == "7.0d ago"


# --- _resolve_path additional coverage --------------------------------------


_EDT_MAIN_MDO = textwrap.dedent("""\
    <?xml version="1.0" encoding="UTF-8"?>
    <mdclass:Configuration xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass"
                           uuid="00000000-0000-0000-0000-000000000003">
        <name>Main</name>
    </mdclass:Configuration>
""")


def test_cli_resolve_path_edt_container(tmp_path):
    """CLI must resolve an EDT container to the src-level root."""
    from rlm_tools_bsl.cli import _resolve_path

    src = tmp_path / "src"
    (src / "Configuration").mkdir(parents=True)
    (src / "Configuration" / "Configuration.mdo").write_text(_EDT_MAIN_MDO, encoding="utf-8")

    resolved = _resolve_path(str(tmp_path))
    assert Path(resolved) == src.resolve()


def test_cli_resolve_path_missing_dir(tmp_path, capsys):
    """Nonexistent path → exit 1 with a clear message."""
    from rlm_tools_bsl.cli import _resolve_path

    missing = tmp_path / "does_not_exist"
    with pytest.raises(SystemExit) as excinfo:
        _resolve_path(str(missing))
    assert excinfo.value.code == 1
    assert "directory not found" in capsys.readouterr().err.lower()


# --- _cmd_* direct calls (builders mocked) ---------------------------------


def _make_cmd_args(**overrides) -> Namespace:
    """Build argparse.Namespace for any _cmd_* with sensible defaults."""
    defaults = {
        "path": None,
        "no_calls": False,
        "no_metadata": False,
        "no_fts": False,
        "no_synonyms": False,
    }
    defaults.update(overrides)
    return Namespace(**defaults)


@pytest.fixture()
def _cli_cf_project(tmp_path, monkeypatch):
    """Minimal CF-root project with a Configuration.xml so _resolve_path accepts it."""
    (tmp_path / "Configuration.xml").write_text(_CF_MAIN_XML_FOR_CLI, encoding="utf-8")
    idx_dir = tmp_path / "_index"
    idx_dir.mkdir()
    monkeypatch.setenv("RLM_INDEX_DIR", str(idx_dir))
    return tmp_path, idx_dir


def _mock_db(tmp_path, db_size: int = 1234) -> MagicMock:
    """Return a MagicMock emulating a Path to bsl_index.db."""
    db = MagicMock(spec=Path)
    real_db = tmp_path / "bsl_index.db"
    real_db.write_bytes(b"x" * db_size)
    db.exists.return_value = True
    db.stat.return_value = real_db.stat()
    db.__str__.return_value = str(real_db)
    db.parent = real_db.parent
    db.unlink = real_db.unlink
    return db


def _stats_fixture() -> dict:
    return {
        "builder_version": 12,
        "config_name": "TestCfg",
        "config_version": "1.0",
        "source_format": "cf",
        "modules": 10,
        "methods": 50,
        "calls": 300,
        "exports": 20,
        "event_subscriptions": 1,
        "scheduled_jobs": 0,
        "functional_options": 2,
        "object_synonyms": 15,
        "object_attributes": 100,
        "predefined_items": 5,
        "metadata_references": 50,
        "exchange_plan_content": 0,
        "defined_types": 0,
        "characteristic_types": 0,
        "file_paths": 0,
        "has_metadata": True,
        "has_fts": True,
        "has_synonyms": True,
        "git_accelerated": False,
        "git_head_commit": None,
        "built_at": 1000.0,
    }


def test_cmd_build_happy_path(_cli_cf_project, capsys):
    """_cmd_build calls IndexBuilder, prints stats, does not exit."""
    from rlm_tools_bsl import cli

    project_path, _ = _cli_cf_project
    args = _make_cmd_args(path=str(project_path))

    with (
        patch("rlm_tools_bsl.bsl_index.IndexBuilder") as BuilderMock,
        patch("rlm_tools_bsl.bsl_index.IndexReader") as ReaderMock,
    ):
        builder = BuilderMock.return_value
        builder.build.return_value = _mock_db(project_path)
        reader = ReaderMock.return_value
        reader.get_statistics.return_value = _stats_fixture()

        cli._cmd_build(args)

    out = capsys.readouterr().out
    assert "Index built" in out
    assert "Modules" in out
    assert "Methods" in out
    builder.build.assert_called_once()


def test_cmd_build_runtime_error(_cli_cf_project, capsys):
    """IndexBuilder.build raising RuntimeError → exit 1."""
    from rlm_tools_bsl import cli

    project_path, _ = _cli_cf_project
    args = _make_cmd_args(path=str(project_path))

    with patch("rlm_tools_bsl.bsl_index.IndexBuilder") as BuilderMock:
        BuilderMock.return_value.build.side_effect = RuntimeError("bsl parser exploded")
        with pytest.raises(SystemExit) as excinfo:
            cli._cmd_build(args)

    assert excinfo.value.code == 1
    assert "bsl parser exploded" in capsys.readouterr().err


def test_cmd_update_happy_path(_cli_cf_project, capsys):
    from rlm_tools_bsl import cli

    project_path, _ = _cli_cf_project
    args = _make_cmd_args(path=str(project_path))

    fake_db = _mock_db(project_path)

    with (
        patch("rlm_tools_bsl.bsl_index.IndexBuilder") as BuilderMock,
        patch("rlm_tools_bsl.bsl_index.get_index_db_path", return_value=fake_db),
        patch("rlm_tools_bsl.bsl_index.IndexReader") as ReaderMock,
    ):
        BuilderMock.return_value.update.return_value = {"added": 2, "changed": 1, "removed": 0}
        ReaderMock.return_value.get_statistics.return_value = _stats_fixture()

        cli._cmd_update(args)

    out = capsys.readouterr().out
    assert "Updated in" in out
    assert "Added:   2" in out
    assert "Changed: 1" in out
    assert "Removed: 0" in out


def test_cmd_update_no_index(_cli_cf_project, capsys):
    """Update without a pre-built index → exit 1 with hint."""
    from rlm_tools_bsl import cli

    project_path, _ = _cli_cf_project
    args = _make_cmd_args(path=str(project_path))

    fake_db = MagicMock(spec=Path)
    fake_db.exists.return_value = False

    with patch("rlm_tools_bsl.bsl_index.get_index_db_path", return_value=fake_db):
        with pytest.raises(SystemExit) as excinfo:
            cli._cmd_update(args)

    assert excinfo.value.code == 1
    assert "index not found" in capsys.readouterr().err.lower()


def test_cmd_update_runtime_error(_cli_cf_project, capsys):
    from rlm_tools_bsl import cli

    project_path, _ = _cli_cf_project
    args = _make_cmd_args(path=str(project_path))
    fake_db = _mock_db(project_path)

    with (
        patch("rlm_tools_bsl.bsl_index.get_index_db_path", return_value=fake_db),
        patch("rlm_tools_bsl.bsl_index.IndexBuilder") as BuilderMock,
    ):
        BuilderMock.return_value.update.side_effect = RuntimeError("update exploded")
        with pytest.raises(SystemExit) as excinfo:
            cli._cmd_update(args)

    assert excinfo.value.code == 1
    assert "update exploded" in capsys.readouterr().err


def test_cmd_info_happy_path(_cli_cf_project, capsys):
    from rlm_tools_bsl import cli
    from rlm_tools_bsl.bsl_index import IndexStatus

    project_path, _ = _cli_cf_project
    args = _make_cmd_args(path=str(project_path))
    fake_db = _mock_db(project_path)

    with (
        patch("rlm_tools_bsl.bsl_index.get_index_db_path", return_value=fake_db),
        patch("rlm_tools_bsl.bsl_index.IndexReader") as ReaderMock,
        patch("rlm_tools_bsl.bsl_index.check_index_strict", return_value=IndexStatus.FRESH),
    ):
        ReaderMock.return_value.get_statistics.return_value = _stats_fixture()
        cli._cmd_info(args)

    out = capsys.readouterr().out
    assert "Index:" in out
    assert "fresh" in out
    assert "Modules:" in out


def test_cmd_info_missing(_cli_cf_project, capsys):
    """info on a project without an index → prints 'Index not found' and exits 0."""
    from rlm_tools_bsl import cli

    project_path, _ = _cli_cf_project
    args = _make_cmd_args(path=str(project_path))
    fake_db = MagicMock(spec=Path)
    fake_db.exists.return_value = False
    fake_db.__str__.return_value = str(project_path / "bsl_index.db")

    with patch("rlm_tools_bsl.bsl_index.get_index_db_path", return_value=fake_db):
        with pytest.raises(SystemExit) as excinfo:
            cli._cmd_info(args)

    assert excinfo.value.code == 0
    assert "Index not found" in capsys.readouterr().out


def _build_real_cli_index(tmp_path, monkeypatch):
    """Build a real (tiny) index under tmp_path and return its db_path."""
    from rlm_tools_bsl.bsl_index import IndexBuilder, get_index_db_path

    (tmp_path / "Configuration.xml").write_text(_CF_MAIN_XML_FOR_CLI, encoding="utf-8")
    obj = tmp_path / "Documents" / "X" / "Ext"
    obj.mkdir(parents=True)
    (obj / "ObjectModule.bsl").write_text("Процедура П() Экспорт\nКонецПроцедуры\n", encoding="utf-8")
    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "_idx"))
    IndexBuilder().build(str(tmp_path), build_calls=False, build_metadata=True)
    return get_index_db_path(str(tmp_path))


def _marker_setting_get_statistics(db_path):
    """Spy: set build_in_progress=1 DURING the stats read (race window opens), then return
    real stats. Models a rebuild starting after the pre-read marker check; stale meta stays
    present so stats_indicate_load_failure is False — only a post-read recheck catches it."""
    import sqlite3

    from rlm_tools_bsl.bsl_index import IndexReader

    orig = IndexReader.get_statistics

    def spy(self):
        w = sqlite3.connect(str(db_path))
        w.execute("INSERT OR REPLACE INTO index_meta (key, value) VALUES ('build_in_progress', '1')")
        w.commit()
        w.close()
        return orig(self)

    return spy


def test_cmd_info_post_read_marker_recheck(tmp_path, monkeypatch, capsys):
    """CLI 'index info': a concurrent rebuild that marks the DB DURING get_statistics (after
    the pre-read check) must print 'incomplete', not normal stats (codex High follow-up)."""
    from rlm_tools_bsl import cli
    from rlm_tools_bsl.bsl_index import IndexReader

    db_path = _build_real_cli_index(tmp_path, monkeypatch)
    monkeypatch.setattr(IndexReader, "get_statistics", _marker_setting_get_statistics(db_path))

    cli._cmd_info(_make_cmd_args(path=str(tmp_path)))
    out = capsys.readouterr().out
    assert "incomplete" in out.lower()
    assert "Modules:" not in out


def test_cmd_build_post_read_marker_recheck(tmp_path, monkeypatch, capsys):
    """CLI 'index build' post-read: a marker appearing DURING the read-back get_statistics
    must print 'incomplete', not a success stats block (codex High follow-up)."""
    from rlm_tools_bsl import cli
    from rlm_tools_bsl.bsl_index import IndexReader

    db_path = _build_real_cli_index(tmp_path, monkeypatch)
    monkeypatch.setattr(IndexReader, "get_statistics", _marker_setting_get_statistics(db_path))

    cli._cmd_build(_make_cmd_args(path=str(tmp_path)))
    out = capsys.readouterr().out
    assert "incomplete" in out.lower()
    assert "Modules:" not in out


def test_cmd_update_post_read_marker_recheck(tmp_path, monkeypatch, capsys):
    """CLI 'index update' post-read: a marker appearing DURING the read-back get_statistics
    must print 'incomplete', not a normal update summary (codex High follow-up)."""
    from rlm_tools_bsl import cli
    from rlm_tools_bsl.bsl_index import IndexReader

    db_path = _build_real_cli_index(tmp_path, monkeypatch)
    monkeypatch.setattr(IndexReader, "get_statistics", _marker_setting_get_statistics(db_path))

    cli._cmd_update(_make_cmd_args(path=str(tmp_path)))
    out = capsys.readouterr().out
    assert "incomplete" in out.lower()


def test_cmd_drop_happy_path(_cli_cf_project, capsys, tmp_path):
    from rlm_tools_bsl import cli

    project_path, _ = _cli_cf_project
    args = _make_cmd_args(path=str(project_path))

    db_parent = tmp_path / "idx-hash"
    db_parent.mkdir()
    real_db = db_parent / "bsl_index.db"
    real_db.write_bytes(b"x" * 2048)

    with patch("rlm_tools_bsl.bsl_index.get_index_db_path", return_value=real_db):
        cli._cmd_drop(args)

    out = capsys.readouterr().out
    assert "Index dropped" in out
    assert not real_db.exists()
    # Parent removed since it became empty
    assert not db_parent.exists()


def test_cmd_drop_missing(_cli_cf_project, capsys):
    """drop on a project without an index prints a message and returns (exit 0)."""
    from rlm_tools_bsl import cli

    project_path, _ = _cli_cf_project
    args = _make_cmd_args(path=str(project_path))
    fake_db = MagicMock(spec=Path)
    fake_db.exists.return_value = False

    with patch("rlm_tools_bsl.bsl_index.get_index_db_path", return_value=fake_db):
        cli._cmd_drop(args)  # no SystemExit

    assert "Index not found, nothing to drop." in capsys.readouterr().out


# --- main() -----------------------------------------------------------------


def test_main_no_args_prints_help(monkeypatch, capsys):
    """Bare `rlm-bsl-index` prints help and exits 0."""
    from rlm_tools_bsl import cli

    monkeypatch.setattr(sys, "argv", ["rlm-bsl-index"])
    with pytest.raises(SystemExit) as excinfo:
        cli.main()
    assert excinfo.value.code == 0
    assert "usage:" in capsys.readouterr().out.lower()


def test_main_index_no_subcommand_prints_index_help(monkeypatch, capsys):
    """`rlm-bsl-index index` without a subcommand prints index help and exits 0."""
    from rlm_tools_bsl import cli

    monkeypatch.setattr(sys, "argv", ["rlm-bsl-index", "index"])
    with pytest.raises(SystemExit) as excinfo:
        cli.main()
    assert excinfo.value.code == 0
    out = capsys.readouterr().out.lower()
    # argparse emits subcommand names in the help output
    assert "build" in out and "update" in out and "info" in out and "drop" in out


def test_main_version_flag(monkeypatch, capsys):
    """`--version` prints package version and exits 0."""
    from rlm_tools_bsl import cli

    monkeypatch.setattr(sys, "argv", ["rlm-bsl-index", "--version"])
    with pytest.raises(SystemExit) as excinfo:
        cli.main()
    assert excinfo.value.code == 0
    assert "rlm-bsl-index" in capsys.readouterr().out.lower()


def test_main_dispatches_build(_cli_cf_project, monkeypatch, capsys):
    """`main()` routes `index build <path>` through _cmd_build."""
    from rlm_tools_bsl import cli

    project_path, _ = _cli_cf_project

    with (
        patch("rlm_tools_bsl.bsl_index.IndexBuilder") as BuilderMock,
        patch("rlm_tools_bsl.bsl_index.IndexReader") as ReaderMock,
    ):
        BuilderMock.return_value.build.return_value = _mock_db(project_path)
        ReaderMock.return_value.get_statistics.return_value = _stats_fixture()

        monkeypatch.setattr(sys, "argv", ["rlm-bsl-index", "index", "build", str(project_path)])
        # Keep load_project_env as no-op so we don't touch real env
        with patch("rlm_tools_bsl._config.load_project_env", lambda: None):
            cli.main()

    assert "Index built" in capsys.readouterr().out


# --- Гейт неподдерживаемых форматов у `index build` (v1.32.0) --------------


@pytest.fixture()
def _cli_foreign_with_bsl(tmp_path, monkeypatch):
    """Чужой формат, но .bsl есть: build возможен только с подтверждением."""
    (tmp_path / "Configuration.json").write_text('{"foreign": true}', encoding="utf-8")
    (tmp_path / "Module.bsl").write_text("// code", encoding="utf-8")
    idx_dir = tmp_path / "_index"
    idx_dir.mkdir()
    monkeypatch.setenv("RLM_INDEX_DIR", str(idx_dir))
    return tmp_path


@pytest.fixture()
def _cli_foreign_no_bsl(tmp_path, monkeypatch):
    """Чужой формат без единого .bsl: строить нечего, отказ безусловный."""
    (tmp_path / "Configuration.json").write_text('{"foreign": true}', encoding="utf-8")
    (tmp_path / "notes.txt").write_text("alpha\n", encoding="utf-8")
    idx_dir = tmp_path / "_index"
    idx_dir.mkdir()
    monkeypatch.setenv("RLM_INDEX_DIR", str(idx_dir))
    return tmp_path


def test_cli_build_refuses_foreign_without_bsl(_cli_foreign_no_bsl, capsys):
    """Отказ приходит из _cmd_build — гейт реально подключён к сборке."""
    from rlm_tools_bsl.cli import _cmd_build

    with pytest.raises(SystemExit) as excinfo:
        _cmd_build(_make_cmd_args(path=str(_cli_foreign_no_bsl)))
    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert ".bsl" in err
    assert not list((_cli_foreign_no_bsl / "_index").rglob("*.db"))


def test_cli_build_survives_tty_that_cannot_answer(_cli_foreign_with_bsl, monkeypatch, capsys):
    """isatty()=True ещё не значит, что есть кому отвечать.

    Git Bash/mintty, docker без -i, CI с псевдо-tty дают терминал, чтение из
    которого сразу упирается в EOF. Пользователь должен получить указание на
    флаг, а НЕ трейсбек EOFError (воспроизведено на боевом CLI 1.32.0)."""
    from rlm_tools_bsl.cli import _gate_unsupported_format

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    def _eof(*_a):
        raise EOFError("EOF when reading a line")

    monkeypatch.setattr("builtins.input", _eof)

    with pytest.raises(SystemExit) as excinfo:
        _gate_unsupported_format(str(_cli_foreign_with_bsl), allow=False)
    assert excinfo.value.code == 1
    captured = capsys.readouterr()
    assert "--allow-unsupported-format" in captured.err
    assert "EOFError" not in captured.err and "Traceback" not in captured.err


def test_cli_build_treats_interrupt_at_prompt_as_cancel(_cli_foreign_with_bsl, monkeypatch, capsys):
    """Ctrl+C на вопросе — это отказ, а не трейсбек KeyboardInterrupt."""
    from rlm_tools_bsl.cli import _gate_unsupported_format

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    def _interrupt(*_a):
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", _interrupt)

    with pytest.raises(SystemExit) as excinfo:
        _gate_unsupported_format(str(_cli_foreign_with_bsl), allow=False)
    assert excinfo.value.code == 1
    assert "Отменено." in capsys.readouterr().out


def test_cli_build_aborts_when_user_declines(_cli_foreign_with_bsl, monkeypatch, capsys):
    from rlm_tools_bsl.cli import _cmd_build

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *_a: "n")

    with pytest.raises(SystemExit) as excinfo:
        _cmd_build(_make_cmd_args(path=str(_cli_foreign_with_bsl)))
    assert excinfo.value.code == 1
    out = capsys.readouterr().out
    assert "ВНИМАНИЕ" in out
    assert "Отменено." in out
    assert not list((_cli_foreign_with_bsl / "_index").rglob("*.db"))


@pytest.mark.parametrize("answer", ["y", "Y", "yes", "да", "ДА"])
def test_cli_build_proceeds_when_user_confirms(_cli_foreign_with_bsl, monkeypatch, capsys, answer):
    from rlm_tools_bsl.cli import _gate_unsupported_format

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *_a: answer)

    _gate_unsupported_format(str(_cli_foreign_with_bsl), allow=False)  # не бросает
    assert "ВНИМАНИЕ" in capsys.readouterr().out


def test_cli_build_non_tty_requires_flag(_cli_foreign_with_bsl, monkeypatch, capsys):
    """Без tty вопрос задать некому — нужен явный флаг, а не молчаливая сборка."""
    from rlm_tools_bsl.cli import _gate_unsupported_format

    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    def _no_input(*_a):
        raise AssertionError("input() must not be called without a tty")

    monkeypatch.setattr("builtins.input", _no_input)

    with pytest.raises(SystemExit) as excinfo:
        _gate_unsupported_format(str(_cli_foreign_with_bsl), allow=False)
    assert excinfo.value.code == 1
    assert "--allow-unsupported-format" in capsys.readouterr().err


def test_cli_build_flag_skips_prompt(_cli_foreign_with_bsl, monkeypatch, capsys):
    from rlm_tools_bsl.cli import _gate_unsupported_format

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    def _no_input(*_a):
        raise AssertionError("input() must not be called with --allow-unsupported-format")

    monkeypatch.setattr("builtins.input", _no_input)

    _gate_unsupported_format(str(_cli_foreign_with_bsl), allow=True)  # не бросает
    assert "ВНИМАНИЕ" in capsys.readouterr().out


def test_cli_build_gate_is_noop_on_supported_cf(_cli_cf_project, monkeypatch, capsys):
    """На нашем CF гейт молчит: ни вопроса, ни предупреждения."""
    from rlm_tools_bsl.cli import _cmd_build

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    def _no_input(*_a):
        raise AssertionError("input() must not be called on a supported CF tree")

    monkeypatch.setattr("builtins.input", _no_input)

    project_path, _ = _cli_cf_project
    with (
        patch("rlm_tools_bsl.bsl_index.IndexBuilder") as BuilderMock,
        patch("rlm_tools_bsl.bsl_index.IndexReader") as ReaderMock,
    ):
        BuilderMock.return_value.build.return_value = _mock_db(project_path)
        ReaderMock.return_value.get_statistics.return_value = _stats_fixture()
        _cmd_build(_make_cmd_args(path=str(project_path)))

    out = capsys.readouterr().out
    assert "ВНИМАНИЕ" not in out
    assert "Index built" in out


def test_cli_parser_exposes_flag_only_on_build(monkeypatch):
    """Флаг живёт только у build; update/info/drop его не принимают."""
    from rlm_tools_bsl import cli

    captured = {}

    def _fake_build(args):
        captured["allow"] = args.allow_unsupported_format

    monkeypatch.setattr(cli, "_cmd_build", _fake_build)
    monkeypatch.setattr("rlm_tools_bsl._config.load_project_env", lambda: None)

    monkeypatch.setattr(sys, "argv", ["rlm-bsl-index", "index", "build", "X", "--allow-unsupported-format"])
    cli.main()
    assert captured["allow"] is True

    monkeypatch.setattr(sys, "argv", ["rlm-bsl-index", "index", "build", "X"])
    cli.main()
    assert captured["allow"] is False

    for other in ("update", "info", "drop"):
        monkeypatch.setattr(sys, "argv", ["rlm-bsl-index", "index", other, "X", "--allow-unsupported-format"])
        with pytest.raises(SystemExit) as excinfo:
            cli.main()
        assert excinfo.value.code == 2
