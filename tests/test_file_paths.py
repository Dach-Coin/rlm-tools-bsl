"""Tests for file_paths navigation index (v5).

Tests cover:
- _can_index_glob pattern dispatcher
- IndexBuilder file_paths population
- IndexReader glob_files, tree_paths, find_files_indexed
- helpers.py integration (indexed vs FS equivalence)
- hint behavior, ranking, lifecycle
"""

import sqlite3

import pytest

from rlm_tools_bsl.bsl_index import (
    BUILDER_VERSION,
    IndexBuilder,
    IndexReader,
    _can_index_glob,
    _collect_file_paths,
)
from rlm_tools_bsl.helpers import make_helpers


# ---------------------------------------------------------------------------
# _can_index_glob dispatcher tests
# ---------------------------------------------------------------------------


class TestCanIndexGlob:
    def test_extension_pattern(self):
        result = _can_index_glob("**/*.mdo")
        assert result == ("by_extension", {"ext": ".mdo"})

    def test_extension_pattern_xml(self):
        result = _can_index_glob("**/*.xml")
        assert result == ("by_extension", {"ext": ".xml"})

    def test_extension_pattern_bsl(self):
        result = _can_index_glob("**/*.bsl")
        assert result == ("by_extension", {"ext": ".bsl"})

    def test_under_prefix(self):
        result = _can_index_glob("Documents/**")
        assert result == ("under_prefix", {"prefix": "Documents"})

    def test_under_prefix_star(self):
        result = _can_index_glob("Documents/**/*")
        assert result == ("under_prefix", {"prefix": "Documents"})

    def test_nested_prefix(self):
        result = _can_index_glob("Documents/MyDoc/**")
        assert result == ("under_prefix", {"prefix": "Documents/MyDoc"})

    def test_dir_file_pattern(self):
        result = _can_index_glob("Documents/*/ManagerModule.bsl")
        assert result == ("dir_file", {"dir": "Documents", "file": "ManagerModule.bsl"})

    def test_exact_path(self):
        result = _can_index_glob("Documents/MyDoc/ObjectModule.bsl")
        assert result == ("exact", {"path": "Documents/MyDoc/ObjectModule.bsl"})

    def test_name_wildcard_with_ext(self):
        result = _can_index_glob("**/Configuration.mdo")
        assert result == ("name_wildcard", {"name_prefix": "Configuration", "ext": ".mdo"})

    def test_name_wildcard_any_ext(self):
        result = _can_index_glob("**/MyFile.*")
        assert result == ("name_wildcard", {"name_prefix": "MyFile", "ext": ""})

    def test_unsupported_complex(self):
        assert _can_index_glob("**/Dir*/*.xml") is None

    def test_unsupported_star_star_star(self):
        assert _can_index_glob("**/*") is None

    def test_unsupported_multiple_wildcards(self):
        assert _can_index_glob("*/*/File.bsl") is None

    def test_empty_pattern(self):
        assert _can_index_glob("") is None

    def test_backslash_normalization(self):
        result = _can_index_glob("Documents\\**")
        assert result == ("under_prefix", {"prefix": "Documents"})

    # --- under_prefix_ext pattern tests ---

    def test_under_prefix_ext_xml(self):
        result = _can_index_glob("**/EventSubscriptions/**/*.xml")
        assert result == ("under_prefix_ext", {"dir_name": "EventSubscriptions", "ext": ".xml"})

    def test_under_prefix_ext_mdo(self):
        result = _can_index_glob("**/FunctionalOptions/**/*.mdo")
        assert result == ("under_prefix_ext", {"dir_name": "FunctionalOptions", "ext": ".mdo"})

    def test_under_prefix_ext_bsl(self):
        result = _can_index_glob("**/Dir/**/*.bsl")
        assert result == ("under_prefix_ext", {"dir_name": "Dir", "ext": ".bsl"})

    def test_under_prefix_ext_single_star_not_matched(self):
        """**/Dir/*/*.ext (single star) should NOT match under_prefix_ext."""
        assert _can_index_glob("**/Dir/*/*.ext") is None

    # --- prefix_recursive_ext pattern tests ---

    def test_prefix_recursive_ext_mdo(self):
        result = _can_index_glob("Subsystems/**/*.mdo")
        assert result == ("prefix_recursive_ext", {"prefix": "Subsystems", "ext": ".mdo"})

    def test_prefix_recursive_ext_deep(self):
        result = _can_index_glob("Documents/Foo/Forms/**/*.bsl")
        assert result == ("prefix_recursive_ext", {"prefix": "Documents/Foo/Forms", "ext": ".bsl"})

    def test_prefix_recursive_ext_backslash(self):
        result = _can_index_glob("Subsystems\\**\\*.mdo")
        assert result == ("prefix_recursive_ext", {"prefix": "Subsystems", "ext": ".mdo"})

    def test_prefix_recursive_ext_no_ext(self):
        """Subsystems/**/* — no extension, should NOT match prefix_recursive_ext."""
        assert _can_index_glob("Subsystems/**/*") == ("under_prefix", {"prefix": "Subsystems"})


# ---------------------------------------------------------------------------
# _collect_file_paths tests
# ---------------------------------------------------------------------------


class TestCollectFilePaths:
    def test_collects_bsl_mdo_xml(self, tmp_path):
        (tmp_path / "Module.bsl").write_text("// code", encoding="utf-8")
        (tmp_path / "Config.mdo").write_text("<xml/>", encoding="utf-8")
        (tmp_path / "Data.xml").write_text("<xml/>", encoding="utf-8")
        (tmp_path / "readme.txt").write_text("text", encoding="utf-8")  # skipped

        rows = _collect_file_paths(str(tmp_path))
        extensions = {r[1] for r in rows}
        assert extensions == {".bsl", ".mdo", ".xml"}
        assert len(rows) == 3

    def test_posix_paths(self, tmp_path):
        subdir = tmp_path / "Documents" / "MyDoc"
        subdir.mkdir(parents=True)
        (subdir / "ObjectModule.bsl").write_text("// code", encoding="utf-8")

        rows = _collect_file_paths(str(tmp_path))
        rel_paths = [r[0] for r in rows]
        assert "Documents/MyDoc/ObjectModule.bsl" in rel_paths

    def test_depth_calculation(self, tmp_path):
        subdir = tmp_path / "A" / "B"
        subdir.mkdir(parents=True)
        (subdir / "file.bsl").write_text("", encoding="utf-8")

        rows = _collect_file_paths(str(tmp_path))
        # A/B/file.bsl = 3 segments = depth 3
        assert rows[0][4] == 3

    def test_skips_hidden_dirs(self, tmp_path):
        hidden = tmp_path / ".git" / "objects"
        hidden.mkdir(parents=True)
        (hidden / "pack.xml").write_text("", encoding="utf-8")
        (tmp_path / "visible.bsl").write_text("", encoding="utf-8")

        rows = _collect_file_paths(str(tmp_path))
        assert len(rows) == 1
        assert rows[0][0] == "visible.bsl"

    def test_skips_skip_dirs(self, tmp_path):
        skip = tmp_path / "node_modules"
        skip.mkdir()
        (skip / "dep.xml").write_text("", encoding="utf-8")
        (tmp_path / "main.bsl").write_text("", encoding="utf-8")

        rows = _collect_file_paths(str(tmp_path))
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# Builder + Reader integration tests
# ---------------------------------------------------------------------------


def _make_test_fixture(tmp_path):
    """Create a realistic fixture with .bsl/.mdo/.xml files."""
    # BSL modules
    docs = tmp_path / "Documents" / "ПоступлениеТоваров"
    docs.mkdir(parents=True)
    (docs / "ObjectModule.bsl").write_text(
        "Процедура ОбработкаПроведения(Отказ)\nКонецПроцедуры\n",
        encoding="utf-8",
    )
    (docs / "ManagerModule.bsl").write_text(
        "Функция ПолучитьДанные() Экспорт\n  Возврат 1;\nКонецФункции\n",
        encoding="utf-8",
    )

    # MDO files
    (docs / "ПоступлениеТоваров.mdo").write_text("<xml/>", encoding="utf-8")

    # Common modules
    common = tmp_path / "CommonModules" / "ОбщийМодуль"
    common.mkdir(parents=True)
    (common / "Module.bsl").write_text(
        "Процедура Тест() Экспорт\nКонецПроцедуры\n",
        encoding="utf-8",
    )

    # Enums with MDO
    enum_dir = tmp_path / "Enums" / "ВидыОпераций"
    enum_dir.mkdir(parents=True)
    (enum_dir / "ВидыОпераций.mdo").write_text("<xml/>", encoding="utf-8")

    # Subsystems with nested MDO
    sub = tmp_path / "Subsystems" / "Бухгалтерия"
    sub.mkdir(parents=True)
    (sub / "Бухгалтерия.mdo").write_text("<xml/>", encoding="utf-8")
    sub2 = tmp_path / "Subsystems" / "Бухгалтерия" / "Subsystems" / "Расчёты"
    sub2.mkdir(parents=True)
    (sub2 / "Расчёты.mdo").write_text("<xml/>", encoding="utf-8")

    # XML files
    (tmp_path / "Configuration").mkdir(exist_ok=True)
    (tmp_path / "Configuration" / "Configuration.mdo").write_text(
        "<root><name>TestConfig</name></root>",
        encoding="utf-8",
    )

    return tmp_path


class TestBuilderFilePaths:
    def test_build_populates_file_paths(self, tmp_path, monkeypatch):
        _make_test_fixture(tmp_path)
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))

        builder = IndexBuilder()
        db_path = builder.build(str(tmp_path), build_calls=False, build_metadata=False, build_fts=False)

        reader = IndexReader(db_path)
        stats = reader.get_statistics()
        reader.close()

        # Should have: 3 bsl + 3 mdo + 0 xml (Configuration.mdo counts as mdo)
        assert stats["file_paths"] > 0

    def test_builder_version_is_10(self, tmp_path, monkeypatch):
        _make_test_fixture(tmp_path)
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))

        builder = IndexBuilder()
        db_path = builder.build(str(tmp_path), build_calls=False, build_metadata=False, build_fts=False)

        conn = sqlite3.connect(str(db_path))
        row = conn.execute("SELECT value FROM index_meta WHERE key='builder_version'").fetchone()
        conn.close()
        assert row[0] == "15"

    def test_file_paths_count_in_meta(self, tmp_path, monkeypatch):
        _make_test_fixture(tmp_path)
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))

        builder = IndexBuilder()
        db_path = builder.build(str(tmp_path), build_calls=False, build_metadata=False, build_fts=False)

        conn = sqlite3.connect(str(db_path))
        row = conn.execute("SELECT value FROM index_meta WHERE key='file_paths_count'").fetchone()
        conn.close()
        assert int(row[0]) > 0


class TestReaderGlobFiles:
    @pytest.fixture
    def reader(self, tmp_path, monkeypatch):
        _make_test_fixture(tmp_path)
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
        builder = IndexBuilder()
        db_path = builder.build(str(tmp_path), build_calls=False, build_metadata=False, build_fts=False)
        r = IndexReader(db_path)
        yield r
        r.close()

    def test_glob_by_extension_mdo(self, reader):
        result = reader.glob_files("**/*.mdo")
        assert result is not None
        assert all(p.endswith(".mdo") for p in result)
        assert len(result) >= 2  # at least ПоступлениеТоваров.mdo, ВидыОпераций.mdo, Configuration.mdo

    def test_glob_by_extension_bsl(self, reader):
        result = reader.glob_files("**/*.bsl")
        assert result is not None
        assert len(result) >= 3

    def test_glob_under_prefix(self, reader):
        result = reader.glob_files("Documents/**")
        assert result is not None
        assert all(p.startswith("Documents/") for p in result)

    def test_glob_dir_file(self, reader):
        result = reader.glob_files("Documents/*/ObjectModule.bsl")
        assert result is not None
        assert len(result) >= 1
        assert all("ObjectModule.bsl" in p for p in result)

    def test_glob_exact_path(self, reader):
        result = reader.glob_files("CommonModules/ОбщийМодуль/Module.bsl")
        assert result is not None
        assert len(result) == 1

    def test_glob_unsupported_returns_none(self, reader):
        result = reader.glob_files("**/Dir*/*.xml")
        assert result is None

    def test_glob_no_match_returns_empty(self, reader):
        result = reader.glob_files("**/*.nonexistent")
        assert result is not None
        assert result == []

    def test_glob_name_wildcard(self, reader):
        result = reader.glob_files("**/Configuration.mdo")
        assert result is not None
        assert len(result) >= 1
        assert all("Configuration.mdo" in p for p in result)

    def test_glob_prefix_recursive_ext(self, reader):
        result = reader.glob_files("Subsystems/**/*.mdo")
        assert result is not None
        assert len(result) >= 2
        assert all(p.startswith("Subsystems/") for p in result)
        assert all(p.endswith(".mdo") for p in result)

    def test_results_sorted(self, reader):
        result = reader.glob_files("**/*.bsl")
        assert result is not None
        assert result == sorted(result)


class TestReaderTreePaths:
    @pytest.fixture
    def reader(self, tmp_path, monkeypatch):
        _make_test_fixture(tmp_path)
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
        builder = IndexBuilder()
        db_path = builder.build(str(tmp_path), build_calls=False, build_metadata=False, build_fts=False)
        r = IndexReader(db_path)
        yield r
        r.close()

    def test_tree_root(self, reader):
        result = reader.tree_paths("", 3)
        assert result is not None
        assert len(result) > 0

    def test_tree_subdir(self, reader):
        result = reader.tree_paths("Documents", 3)
        assert result is not None
        assert all(p.startswith("Documents/") for p in result)

    def test_tree_max_depth(self, reader):
        result = reader.tree_paths("", 1)
        assert result is not None
        # depth 1 means single-level files only
        for p in result:
            assert p.count("/") == 0


class TestReaderFindFiles:
    @pytest.fixture
    def reader(self, tmp_path, monkeypatch):
        _make_test_fixture(tmp_path)
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
        builder = IndexBuilder()
        db_path = builder.build(str(tmp_path), build_calls=False, build_metadata=False, build_fts=False)
        r = IndexReader(db_path)
        yield r
        r.close()

    def test_find_by_name(self, reader):
        result = reader.find_files_indexed("ObjectModule")
        assert result is not None
        assert len(result) >= 1
        assert all("ObjectModule" in p for p in result)

    def test_find_case_insensitive(self, reader):
        result = reader.find_files_indexed("objectmodule")
        assert result is not None
        assert len(result) >= 1

    def test_find_ranking_exact_first(self, reader):
        """Exact filename match should come before substring matches."""
        result = reader.find_files_indexed("Module.bsl")
        assert result is not None
        assert len(result) >= 1
        # First result should be the exact filename match
        first = result[0].split("/")[-1]
        assert first.lower() == "module.bsl"

    def test_find_limit(self, reader):
        result = reader.find_files_indexed(".", limit=2)
        assert result is not None
        assert len(result) <= 2

    def test_find_no_match(self, reader):
        result = reader.find_files_indexed("nonexistentfile12345")
        assert result is not None
        assert result == []

    def test_find_cyrillic(self, reader):
        """Cyrillic names must work (SQLite LOWER() is ASCII-only)."""
        result = reader.find_files_indexed("ПоступлениеТоваров")
        assert result is not None
        assert len(result) >= 1
        assert any("ПоступлениеТоваров" in p for p in result)

    def test_find_empty_name(self, reader):
        result = reader.find_files_indexed("")
        assert result is None


def test_find_files_indexed_cyrillic_case_insensitive(tmp_path, monkeypatch):
    _make_test_fixture(tmp_path)
    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
    db_path = IndexBuilder().build(str(tmp_path), build_calls=False, build_metadata=False, build_fts=False)
    reader = IndexReader(db_path)
    try:
        hits = reader.find_files_indexed("поступлениетоваров")
        assert any("ПоступлениеТоваров" in p for p in hits), hits
    finally:
        reader.close()


def test_find_files_indexed_escapes_like_wildcards(tmp_path, monkeypatch):
    _make_test_fixture(tmp_path)
    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
    db_path = IndexBuilder().build(str(tmp_path), build_calls=False, build_metadata=False, build_fts=False)
    reader = IndexReader(db_path)
    try:
        # '%' и '_' должны искаться ЛИТЕРАЛЬНО (не как SQL-wildcards) → нет match-all.
        # В фикстуре нет путей с '%' или '_', значит результат пуст, а не «все файлы».
        assert reader.find_files_indexed("%") == []
        assert reader.find_files_indexed("_") == []
        # регресс: обычный кириллический поиск с РАЗНЫМ регистром не затронут escape'ом
        assert any("ПоступлениеТоваров" in p for p in reader.find_files_indexed("поступлениеТОВАРОВ"))
    finally:
        reader.close()


# ---------------------------------------------------------------------------
# helpers.py integration tests (indexed vs FS)
# ---------------------------------------------------------------------------


class TestHelpersIntegration:
    """Test that indexed path in helpers.py works correctly."""

    @pytest.fixture
    def indexed_env(self, tmp_path, monkeypatch):
        """Create environment with both FS files and an index."""
        _make_test_fixture(tmp_path)
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))

        builder = IndexBuilder()
        db_path = builder.build(str(tmp_path), build_calls=False, build_metadata=False, build_fts=False)
        reader = IndexReader(db_path)

        helpers_indexed, _ = make_helpers(str(tmp_path), idx_reader=reader)
        helpers_fs, _ = make_helpers(str(tmp_path), idx_reader=None)

        yield {
            "indexed": helpers_indexed,
            "fs": helpers_fs,
            "reader": reader,
            "path": tmp_path,
        }
        reader.close()

    def test_glob_indexed_returns_results(self, indexed_env):
        result = indexed_env["indexed"]["glob_files"]("**/*.mdo")
        assert len(result) >= 2

    def test_glob_unsupported_falls_back(self, indexed_env):
        """Unsupported patterns should fallback to FS and still work."""
        # Create a file that matches a complex pattern
        subdir = indexed_env["path"] / "Special"
        subdir.mkdir(exist_ok=True)
        (subdir / "test.py").write_text("", encoding="utf-8")

        result = indexed_env["indexed"]["glob_files"]("Special/*.py")
        # This should fallback to FS since it doesn't match whitelist
        # (3-part pattern with * in middle is dir_file, but Special/*.py is 2-part)
        # Actually Special/*.py doesn't match any whitelist pattern, so FS fallback
        assert isinstance(result, list)

    def test_find_files_indexed(self, indexed_env):
        result = indexed_env["indexed"]["find_files"]("ObjectModule")
        assert len(result) >= 1

    def test_tree_indexed(self, indexed_env):
        result = indexed_env["indexed"]["tree"]("Documents", 3)
        assert "ПоступлениеТоваров" in result
        assert "ObjectModule.bsl" in result

    def test_glob_hint_on_dir_pattern(self, indexed_env):
        """Pattern matching a known dir but no files should return hint."""
        result = indexed_env["indexed"]["glob_files"]("Documents")
        assert len(result) == 1
        assert result[0].startswith("[hint:")

    def test_glob_under_prefix_ext_contract(self, indexed_env):
        """under_prefix_ext pattern: indexed result == FS result."""
        pattern = "**/Ext/**/*.bsl"
        indexed_result = indexed_env["indexed"]["glob_files"](pattern)
        fs_result = indexed_env["fs"]["glob_files"](pattern)
        assert sorted(indexed_result) == sorted(fs_result)

    def test_glob_prefix_recursive_ext_contract(self, indexed_env):
        """prefix_recursive_ext pattern: indexed result == FS result."""
        pattern = "Subsystems/**/*.mdo"
        indexed_result = indexed_env["indexed"]["glob_files"](pattern)
        fs_result = indexed_env["fs"]["glob_files"](pattern)
        assert sorted(indexed_result) == sorted(fs_result)


# ---------------------------------------------------------------------------
# Update lifecycle tests
# ---------------------------------------------------------------------------


class TestUpdateFilePaths:
    def test_update_refreshes_file_paths(self, tmp_path, monkeypatch):
        _make_test_fixture(tmp_path)
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))

        builder = IndexBuilder()
        db_path = builder.build(str(tmp_path), build_calls=False, build_metadata=False, build_fts=False)

        # Check initial count
        reader = IndexReader(db_path)
        initial_count = reader.get_statistics()["file_paths"]
        reader.close()

        # Add a new file
        (tmp_path / "NewFile.bsl").write_text("// new", encoding="utf-8")

        # Update
        builder.update(str(tmp_path))

        reader = IndexReader(db_path)
        new_count = reader.get_statistics()["file_paths"]
        reader.close()

        assert new_count == initial_count + 1


# ---------------------------------------------------------------------------
# Version bump test
# ---------------------------------------------------------------------------


class TestVersionBump:
    def test_builder_version_is_10(self):
        assert BUILDER_VERSION == 15
