"""Comprehensive unit tests for bsl_index module.

Tests IndexBuilder, IndexReader, incremental updates, freshness checks,
and path/env helpers.
"""

import json
import sqlite3
import time

import pytest

from rlm_tools_bsl.bsl_index import (
    IndexBuilder,
    IndexReader,
    IndexStatus,
    _read_index_meta,
    check_index_freshness,
    check_index_strict,
    check_index_usable,
    get_index_db_path,
    get_index_dir,
    index_incomplete,
)
from rlm_tools_bsl.cache import _paths_hash


def _mark_incomplete(db_path, has_flags=None):
    """Simulate a partially-built (in-place) index: set build_in_progress=1.

    has_flags=None → keep existing has_* (e.g. as written by a real build).
    has_flags={...} → DELETE all has_* then set only the provided ones (for the
    recovery-default tests: missing/partial has_* must default to full-feature).
    """
    conn = sqlite3.connect(str(db_path))
    conn.execute("INSERT OR REPLACE INTO index_meta (key, value) VALUES ('build_in_progress', '1')")
    if has_flags is not None:
        for k in ("has_calls", "has_metadata", "has_fts", "has_synonyms"):
            conn.execute("DELETE FROM index_meta WHERE key = ?", (k,))
        for k, v in has_flags.items():
            conn.execute("INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)", (k, "1" if v else "0"))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# BSL source code fixtures — realistic 1C code with Russian identifiers
# ---------------------------------------------------------------------------

COMMON_MODULE_BSL = """\
#Область ПрограммныйИнтерфейс

// Заполняет табличную часть документа по умолчанию.
//
// Параметры:
//  ДокументОбъект - ДокументОбъект - заполняемый документ
//
Процедура ЗаполнитьТабличнуюЧасть(ДокументОбъект, ИмяТабличнойЧасти) Экспорт
    Для Каждого Строка Из ДокументОбъект[ИмяТабличнойЧасти] Цикл
        Строка.Количество = 1;
    КонецЦикла;
    ВычислитьИтоги(ДокументОбъект[ИмяТабличнойЧасти]);
КонецПроцедуры

// Возвращает текущую дату сеанса с учетом часового пояса.
Функция ПолучитьДатуСеанса() Экспорт
    Возврат ТекущаяДатаСеанса();
КонецФункции

// Внутренняя процедура — НЕ экспортная.
Процедура ВычислитьИтоги(ТаблицаЗначений)
    Результат = Новый Массив;
    Для Каждого Строка Из ТаблицаЗначений Цикл
        Результат.Добавить(Строка.Сумма);
    КонецЦикла;
КонецПроцедуры

#КонецОбласти
"""

OBJECT_MODULE_BSL = """\
Процедура ОбработкаЗаполнения(ДанныеЗаполнения, СтандартнаяОбработка) Экспорт
    Если ТипЗнч(ДанныеЗаполнения) = Тип("Структура") Тогда
        МойМодуль.ЗаполнитьТабличнуюЧасть(ЭтотОбъект, "Товары");
    КонецЕсли;
КонецПроцедуры

Процедура ПередЗаписью(Отказ)
    ПроверитьЗаполнение();
    МойМодуль.ВычислитьИтоги(Товары);
КонецПроцедуры

Функция ПолучитьПредставление() Экспорт
    Дата = МойМодуль.ПолучитьДатуСеанса();
    Возврат "Документ от " + Формат(Дата, "ДФ=dd.MM.yyyy");
КонецФункции
"""

MANAGER_MODULE_BSL = """\
Функция ПолучитьФорму(ИмяФормы, Параметры, Владелец) Экспорт
    Возврат ПолучитьОбщуюФорму("ФормаВыбора");
КонецФункции
"""

RECORDSET_MODULE_BSL = """\
Процедура ПриЗаписи(Отказ)
    МойМодуль.ЗаполнитьТабличнуюЧасть(ЭтотОбъект, "Движения");
    ЗаполнитьТабличнуюЧасть(ЭтотОбъект, "Итоги");
КонецПроцедуры
"""

FORM_MODULE_BSL = """\
Процедура ОбработчикФормы()
    ЛокальныйХелпер();
КонецПроцедуры

Процедура ЛокальныйХелпер()
    Сообщить("Привет");
КонецПроцедуры
"""


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_bsl_project(tmp_path):
    """Create a temporary CF-format BSL project structure.

    Structure:
        tmp_path/
          CommonModules/
            МойМодуль/
              Ext/
                Module.bsl
          Documents/
            ТестовыйДокумент/
              Ext/
                ObjectModule.bsl
                ManagerModule.bsl
    """
    # CommonModules/МойМодуль/Ext/Module.bsl
    cm_dir = tmp_path / "CommonModules" / "МойМодуль" / "Ext"
    cm_dir.mkdir(parents=True)
    (cm_dir / "Module.bsl").write_text(COMMON_MODULE_BSL, encoding="utf-8-sig")

    # Documents/ТестовыйДокумент/Ext/ObjectModule.bsl
    doc_dir = tmp_path / "Documents" / "ТестовыйДокумент" / "Ext"
    doc_dir.mkdir(parents=True)
    (doc_dir / "ObjectModule.bsl").write_text(OBJECT_MODULE_BSL, encoding="utf-8-sig")

    # Documents/ТестовыйДокумент/Ext/ManagerModule.bsl
    (doc_dir / "ManagerModule.bsl").write_text(MANAGER_MODULE_BSL, encoding="utf-8-sig")

    # InformationRegisters/МойРегистр/Ext/RecordSetModule.bsl
    reg_dir = tmp_path / "InformationRegisters" / "МойРегистр" / "Ext"
    reg_dir.mkdir(parents=True)
    (reg_dir / "RecordSetModule.bsl").write_text(RECORDSET_MODULE_BSL, encoding="utf-8-sig")

    # Documents/ТестовыйДокумент/Forms/ФормаДокумента/Ext/Form/Module.bsl
    form_dir = tmp_path / "Documents" / "ТестовыйДокумент" / "Forms" / "ФормаДокумента" / "Ext" / "Form"
    form_dir.mkdir(parents=True)
    (form_dir / "Module.bsl").write_text(FORM_MODULE_BSL, encoding="utf-8-sig")

    return tmp_path


@pytest.fixture
def built_index(tmp_bsl_project, monkeypatch):
    """Build a full index (with calls) and return (db_path, base_path)."""
    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_bsl_project / ".index"))
    builder = IndexBuilder()
    db_path = builder.build(str(tmp_bsl_project), build_calls=True)
    return db_path, str(tmp_bsl_project)


@pytest.fixture
def built_index_no_calls(tmp_bsl_project, monkeypatch):
    """Build an index WITHOUT calls and return (db_path, base_path)."""
    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_bsl_project / ".index_nc"))
    builder = IndexBuilder()
    db_path = builder.build(str(tmp_bsl_project), build_calls=False)
    return db_path, str(tmp_bsl_project)


# =====================================================================
# Finding #1a (v1.26.0): _BuildLock thread-aware (owner tid + depth)
# =====================================================================


class TestBuildLock:
    def test_buildlock_nested_reentry(self, tmp_path):
        """Same-thread reentry bumps depth; OS lock + lock-file released ONLY after
        the OUTER release (nested _update_locked → self.build())."""
        from rlm_tools_bsl.bsl_index import _BuildLock, _active_locks

        db = tmp_path / "idx" / "bsl_index.db"
        db.parent.mkdir(parents=True)
        lock_key = str(db.with_suffix(".lock"))

        outer = _BuildLock(db)
        outer.acquire()
        try:
            assert _active_locks[lock_key]._depth == 1
            inner = _BuildLock(db)
            inner.acquire()  # same-thread reentry
            assert _active_locks[lock_key]._depth == 2
            inner.release()
            # depth back to 1 → OS lock STILL held, lock-file STILL present
            assert lock_key in _active_locks
            assert _active_locks[lock_key]._depth == 1
            assert db.with_suffix(".lock").exists()
        finally:
            outer.release()
        # fully released after the outer release
        assert lock_key not in _active_locks

    def test_buildlock_depth_balanced_on_inner_exception(self, tmp_path):
        """Inner work raising (with release in finally — like build()/update()) still
        collapses depth back to the outer level; no OS-lock leak."""
        from rlm_tools_bsl.bsl_index import _BuildLock, _active_locks

        db = tmp_path / "idx" / "bsl_index.db"
        db.parent.mkdir(parents=True)
        lock_key = str(db.with_suffix(".lock"))

        outer = _BuildLock(db)
        outer.acquire()
        try:
            inner = _BuildLock(db)
            inner.acquire()
            try:
                raise ValueError("boom inside inner build")
            except ValueError:
                pass
            finally:
                inner.release()
            assert _active_locks[lock_key]._depth == 1
        finally:
            outer.release()
        assert lock_key not in _active_locks

    def test_buildlock_blocks_second_thread(self, tmp_path):
        """A second THREAD of the same process must NOT enter the critical section —
        it gets RuntimeError (not silent reentry that would corrupt in-place rebuild)."""
        import threading

        from rlm_tools_bsl.bsl_index import _BuildLock

        db = tmp_path / "idx" / "bsl_index.db"
        db.parent.mkdir(parents=True)

        held = threading.Event()
        release_now = threading.Event()
        result: dict = {}

        def holder():
            lock = _BuildLock(db)
            lock.acquire()
            held.set()
            release_now.wait(timeout=5)
            lock.release()

        def second():
            held.wait(timeout=5)
            lock2 = _BuildLock(db)
            try:
                lock2.acquire()
                result["entered"] = True  # MUST NOT happen
                lock2.release()
            except RuntimeError:
                result["blocked"] = True

        t1 = threading.Thread(target=holder)
        t2 = threading.Thread(target=second)
        t1.start()
        t2.start()
        t2.join(timeout=5)
        release_now.set()
        t1.join(timeout=5)

        assert result.get("blocked") is True
        assert "entered" not in result


# =====================================================================
# Finding #1b/#1c (v1.26.0): in-place rebuild + marker + self-heal + M1
# =====================================================================


class TestInPlaceRebuild:
    def test_build_no_unlink_on_db_path(self, tmp_bsl_project, monkeypatch):
        """Anti-regression: a normal rebuild must NEVER unlink the db file (the root of
        the Windows H1 reader clash). Spy distinguishes db_path from the .lock file."""
        import pathlib

        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_bsl_project / ".idx"))
        builder = IndexBuilder()
        db_path = builder.build(str(tmp_bsl_project))

        unlinked: list[str] = []
        real_unlink = pathlib.Path.unlink

        def spy_unlink(self, *a, **k):
            unlinked.append(str(self))
            return real_unlink(self, *a, **k)

        monkeypatch.setattr(pathlib.Path, "unlink", spy_unlink)
        builder.build(str(tmp_bsl_project))  # rebuild — must not unlink db_path
        assert str(db_path) not in unlinked  # (.lock unlink is fine and expected)

    def test_rebuild_under_open_reader(self, tmp_bsl_project, monkeypatch):
        """H1 regress gate (real on the windows CI leg, trivially green on Linux):
        rebuild while an IndexReader is open must NOT crash; a NEW reader sees fresh
        data. Reader opened BEFORE the 2nd build. NOT skipif(win32)."""
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_bsl_project / ".idx"))
        builder = IndexBuilder()
        db_path = builder.build(str(tmp_bsl_project))
        reader = IndexReader(db_path)
        try:
            db_path2 = builder.build(str(tmp_bsl_project))  # in-place under open reader
            assert db_path2 == db_path
            assert db_path.exists()
            new_reader = IndexReader(db_path)
            try:
                assert new_reader.get_statistics()["modules"] >= 1
            finally:
                new_reader.close()
        finally:
            reader.close()

    def test_rebuild_twice_with_fts_under_open_reader(self, tmp_bsl_project, monkeypatch):
        """FTS5 shadow tables must drop/recreate cleanly under an open reader (no
        OperationalError)."""
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_bsl_project / ".idx"))
        builder = IndexBuilder()
        db_path = builder.build(str(tmp_bsl_project), build_fts=True)
        reader = IndexReader(db_path)
        try:
            builder.build(str(tmp_bsl_project), build_fts=True)
            results = reader.search_methods("Заполнить")
            assert isinstance(results, list)
        finally:
            reader.close()

    def test_old_reader_survives_rebuild(self, tmp_bsl_project, monkeypatch):
        """The SAME old reader keeps working after an in-place rebuild (SQLite
        re-prepares after SQLITE_SCHEMA) — statistics, file nav, FTS."""
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_bsl_project / ".idx"))
        builder = IndexBuilder()
        db_path = builder.build(str(tmp_bsl_project), build_fts=True)
        reader = IndexReader(db_path)
        try:
            assert reader.get_statistics()["modules"] >= 1
            builder.build(str(tmp_bsl_project), build_fts=True)  # rebuild under old reader
            assert reader.get_statistics()["modules"] >= 1
            assert reader.find_files_indexed("Module.bsl") is not None
            assert isinstance(reader.search_methods("Заполнить"), list)
        finally:
            reader.close()

    def test_build_conn_closed_on_exception(self, tmp_bsl_project, monkeypatch):
        """M1: an exception mid-build releases the lock (conn closed in finally) — a
        fresh lock acquire on the same db_path does NOT block; the marker is left set."""
        from rlm_tools_bsl.bsl_index import _BuildLock, _active_locks

        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_bsl_project / ".idx"))
        builder = IndexBuilder()
        db_path = builder.build(str(tmp_bsl_project))

        def boom(self, *a, **k):
            raise RuntimeError("injected mid-build failure")

        monkeypatch.setattr(IndexBuilder, "_bulk_insert", boom)
        with pytest.raises(RuntimeError):
            builder.build(str(tmp_bsl_project))

        assert not _active_locks  # lock released by build()'s finally
        # not blocked: a fresh acquire succeeds
        lock = _BuildLock(db_path)
        lock.acquire()
        lock.release()
        # marker left → not usable
        assert index_incomplete(db_path) is True
        assert check_index_usable(db_path, str(tmp_bsl_project)) == IndexStatus.MISSING

    def test_update_releases_lock_on_connect_error(self, built_index, monkeypatch):
        """codex: a connect-time error in update() must NOT leak the _BuildLock (connect
        is inside the try/finally). Otherwise _active_locks stays registered and blocks
        later builds in the same process."""
        import rlm_tools_bsl.bsl_index as bi
        from rlm_tools_bsl.bsl_index import _BuildLock, _active_locks

        db_path, base_path = built_index

        def boom(*a, **k):
            raise sqlite3.OperationalError("injected connect failure")

        monkeypatch.setattr(bi.sqlite3, "connect", boom)
        with pytest.raises(sqlite3.OperationalError):
            IndexBuilder().update(base_path)
        monkeypatch.undo()  # restore real sqlite3.connect

        assert not _active_locks  # lock released despite the connect-time error
        # not blocked: a fresh acquire succeeds
        lock = _BuildLock(db_path)
        lock.acquire()
        lock.release()

    def test_rebuild_clears_stale_rows(self, tmp_bsl_project, monkeypatch):
        """An object removed from disk is gone from the index after rebuild (in-place
        DROP+repopulate, not a stale leftover)."""
        import shutil

        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_bsl_project / ".idx"))
        builder = IndexBuilder()
        db_path = builder.build(str(tmp_bsl_project))
        shutil.rmtree(tmp_bsl_project / "Documents" / "ТестовыйДокумент")
        builder.build(str(tmp_bsl_project))
        conn = sqlite3.connect(str(db_path))
        try:
            n = conn.execute("SELECT COUNT(*) FROM modules WHERE object_name='ТестовыйДокумент'").fetchone()[0]
        finally:
            conn.close()
        assert n == 0

    def test_build_self_heals_corrupt_db(self, tmp_bsl_project, monkeypatch):
        """A corrupt/incompatible file is recreated (not stuck forever): (a) non-SQLite
        garbage; (b) valid SQLite with malformed index_meta (key not PK)."""
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_bsl_project / ".idx"))
        db_path = get_index_db_path(str(tmp_bsl_project))
        db_path.parent.mkdir(parents=True, exist_ok=True)
        builder = IndexBuilder()

        # (a) non-SQLite garbage
        db_path.write_bytes(b"this is not a sqlite database at all")
        builder.build(str(tmp_bsl_project))
        reader = IndexReader(db_path)
        try:
            assert reader.get_statistics()["modules"] >= 1
        finally:
            reader.close()

        # (b) valid SQLite, malformed index_meta (no key PRIMARY KEY)
        conn = sqlite3.connect(str(db_path))
        conn.execute("DROP TABLE IF EXISTS index_meta")
        conn.execute("CREATE TABLE index_meta (foo TEXT, bar TEXT)")
        conn.commit()
        conn.close()
        builder.build(str(tmp_bsl_project))
        reader = IndexReader(db_path)
        try:
            assert reader.get_statistics()["modules"] >= 1
        finally:
            reader.close()

    def test_empty_rebuild_clears_old_rows(self, tmp_bsl_project, monkeypatch):
        """A rebuild after all .bsl files are removed clears modules/methods, refreshes
        file_paths, clears the marker, and reads FRESH (paths_hash of [], not '')."""
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_bsl_project / ".idx"))
        builder = IndexBuilder()
        db_path = builder.build(str(tmp_bsl_project))
        for f in tmp_bsl_project.rglob("*.bsl"):
            f.unlink()
        builder.build(str(tmp_bsl_project))
        conn = sqlite3.connect(str(db_path))
        try:
            assert conn.execute("SELECT COUNT(*) FROM modules").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM methods").fetchone()[0] == 0
        finally:
            conn.close()
        assert index_incomplete(db_path) is False  # marker cleared
        assert check_index_strict(db_path, 0, _paths_hash([]), str(tmp_bsl_project)) == IndexStatus.FRESH

    def test_partial_build_not_usable(self, tmp_bsl_project, monkeypatch):
        """A marked (build_in_progress=1) index reads as MISSING from usable/strict;
        update() does NOT increment it — it recovers via full rebuild."""
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_bsl_project / ".idx"))
        builder = IndexBuilder()
        db_path = builder.build(str(tmp_bsl_project))
        _mark_incomplete(db_path)

        assert index_incomplete(db_path) is True
        assert check_index_usable(db_path, str(tmp_bsl_project)) == IndexStatus.MISSING
        # marker is checked before structural — any count/hash → still MISSING
        assert check_index_strict(db_path, 999, "whatever", str(tmp_bsl_project)) == IndexStatus.MISSING

        delta = builder.update(str(tmp_bsl_project))
        assert delta.get("rebuild_reason") == "incomplete prior build"
        assert index_incomplete(db_path) is False


class TestIncompleteRecovery:
    def test_recovery_preserves_build_options(self, tmp_bsl_project, monkeypatch):
        """Recovery via update() preserves the intended (non-default) build options of
        the interrupted build when all four has_* are present + valid."""
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_bsl_project / ".idx"))
        builder = IndexBuilder()
        db_path = builder.build(str(tmp_bsl_project), build_fts=False, build_metadata=False)
        _mark_incomplete(db_path)  # keeps has_fts=0/has_metadata=0 from the build
        builder.update(str(tmp_bsl_project))
        meta = _read_index_meta(db_path)
        assert meta["has_fts"] == "0"
        assert meta["has_metadata"] == "0"
        assert meta["has_calls"] == "1"
        assert meta["has_synonyms"] == "1"

    def test_recovery_marker_only_defaults_full(self, tmp_bsl_project, monkeypatch):
        """Marker present but NO has_* (legacy/manual partial) → recovery defaults to
        full-feature (all has_* True)."""
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_bsl_project / ".idx"))
        builder = IndexBuilder()
        db_path = builder.build(str(tmp_bsl_project), build_fts=False)
        _mark_incomplete(db_path, has_flags={})  # delete all has_*, set none
        builder.update(str(tmp_bsl_project))
        meta = _read_index_meta(db_path)
        assert meta["has_fts"] == "1"  # defaulted to full (was 0)
        assert meta["has_metadata"] == "1"
        assert meta["has_calls"] == "1"
        assert meta["has_synonyms"] == "1"

    def test_recovery_partial_has_flags_defaults_full(self, tmp_bsl_project, monkeypatch):
        """Marker + only SOME has_* present → atomic validation fails → full-feature."""
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_bsl_project / ".idx"))
        builder = IndexBuilder()
        db_path = builder.build(str(tmp_bsl_project), build_fts=False)
        _mark_incomplete(db_path, has_flags={"has_fts": False, "has_metadata": False})
        builder.update(str(tmp_bsl_project))
        meta = _read_index_meta(db_path)
        assert meta["has_fts"] == "1"  # not all 4 present → defaulted to full
        assert meta["has_metadata"] == "1"


# =====================================================================
# Finding #1d (v1.26.0): defensive reader (_transient_safe + ZERO_STATS + gate)
# =====================================================================


class TestDefensiveReader:
    def test_idx_reader_calls_are_transient_safe(self):
        """Coverage gate (AST visitor, NOT regex): every IndexReader method called as
        ``idx_reader.<m>(...)`` in bsl_helpers.py / server.py must be wrapped with
        @_transient_safe. AST distinguishes a Call from a property access
        (idx_reader.has_calls) and ignores docstrings/comments. Only close() is allowed
        unwrapped."""
        import ast
        import pathlib

        import rlm_tools_bsl.bsl_helpers as bh_mod
        import rlm_tools_bsl.server as srv_mod

        allow = {"close"}  # lifecycle / non-read
        called: set[str] = set()
        for mod in (bh_mod, srv_mod):
            tree = ast.parse(pathlib.Path(mod.__file__).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "idx_reader"
                ):
                    called.add(node.func.attr)

        missing = [
            name
            for name in sorted(called - allow)
            if not getattr(getattr(IndexReader, name, None), "_is_transient_safe", False)
        ]
        assert not missing, f"idx_reader.<m>() not @_transient_safe: {missing}"

    def test_zero_stats_key_parity(self, built_index):
        """ZERO_STATS sentinel carries EXACTLY the same key set as a real
        get_statistics() (consumers do **stats / .get — drift would break them)."""
        from rlm_tools_bsl.bsl_index import _zero_stats

        db_path, _ = built_index
        reader = IndexReader(db_path)
        try:
            real = reader.get_statistics()
        finally:
            reader.close()
        assert set(_zero_stats()) == set(real)

    def test_transient_safe_reraises_real_error(self, built_index):
        """A REAL (engine, not mocked) non-transient OperationalError ('no such column')
        propagates; the transient 'no such table' degrades to the sentinel."""
        from rlm_tools_bsl.bsl_index import _transient_safe

        db_path, _ = built_index
        reader = IndexReader(db_path)
        try:

            @_transient_safe(lambda: "SENTINEL")
            def bad_column(rdr):
                return rdr._conn.execute("SELECT no_such_col FROM modules").fetchone()

            @_transient_safe(lambda: "SENTINEL")
            def missing_table(rdr):
                return rdr._conn.execute("SELECT * FROM no_such_table_xyz").fetchone()

            with pytest.raises(sqlite3.OperationalError):
                bad_column(reader)  # real defect → NOT swallowed
            assert missing_table(reader) == "SENTINEL"  # transient → sentinel
        finally:
            reader.close()

    def test_reader_degrades_on_partial_db(self, built_index):
        """An old reader whose tables are dropped mid-rebuild degrades to sentinels
        (zeros / None) instead of raising OperationalError."""
        db_path, _ = built_index
        reader = IndexReader(db_path)
        try:
            w = sqlite3.connect(str(db_path))
            w.execute("DROP TABLE IF EXISTS methods")
            w.execute("DROP TABLE IF EXISTS modules")
            w.commit()
            w.close()
            stats = reader.get_statistics()  # zeros, not crash
            assert stats["modules"] == 0
            assert stats["methods"] == 0
            # get_methods_by_path → None sentinel (not crash)
            assert reader.get_methods_by_path("CommonModules/МойМодуль/Ext/Module.bsl") is None
        finally:
            reader.close()

    def test_stats_indicate_load_failure(self, built_index):
        """The zero/load-failure discriminator (codex High): ZERO_STATS → True, a real
        index (builder_version + built_at present) → False. Timing-independent guard so a
        truthy ZERO_STATS during a concurrent rebuild is never reported as a loaded 'ok'."""
        from rlm_tools_bsl.bsl_index import _zero_stats, stats_indicate_load_failure

        db_path, _ = built_index
        reader = IndexReader(db_path)
        try:
            real = reader.get_statistics()
        finally:
            reader.close()
        assert stats_indicate_load_failure(_zero_stats()) is True
        assert stats_indicate_load_failure(real) is False


# =====================================================================
# IndexBuilder tests
# =====================================================================


class TestIndexBuilder:
    def test_build_creates_db(self, built_index):
        """build() creates a .db file with all 4 tables (meta + 3 data)."""
        db_path, _ = built_index
        assert db_path.exists(), "Database file should exist after build"

        conn = sqlite3.connect(str(db_path))
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()

        expected_tables = {"index_meta", "modules", "methods", "calls"}
        assert expected_tables.issubset(tables), f"Expected tables {expected_tables}, found {tables}"

    def test_build_no_calls(self, built_index_no_calls):
        """build(build_calls=False) creates DB without calls data."""
        db_path, _ = built_index_no_calls

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row

        # The calls table schema is always created, but should have 0 rows
        row = conn.execute("SELECT COUNT(*) AS cnt FROM calls").fetchone()
        assert row["cnt"] == 0, "calls table should be empty when build_calls=False"

        # Verify meta records it
        meta_row = conn.execute("SELECT value FROM index_meta WHERE key = 'has_calls'").fetchone()
        assert meta_row["value"] == "0"

        conn.close()

    def test_build_modules_complete(self, built_index):
        """All .bsl files from the fixture appear in the modules table."""
        db_path, _ = built_index

        conn = sqlite3.connect(str(db_path))
        rows = conn.execute("SELECT rel_path FROM modules ORDER BY rel_path").fetchall()
        conn.close()

        rel_paths = sorted(r[0] for r in rows)
        assert len(rel_paths) == 5, f"Expected 5 modules, got {len(rel_paths)}"

        # Check that all expected relative paths are present
        assert any("CommonModules" in p and "Module.bsl" in p for p in rel_paths)
        assert any("ObjectModule.bsl" in p for p in rel_paths)
        assert any("ManagerModule.bsl" in p for p in rel_paths)
        assert any("RecordSetModule.bsl" in p for p in rel_paths)
        assert any("Form" in p and "Module.bsl" in p for p in rel_paths)

    def test_build_modules_mtime_and_size(self, built_index):
        """mtime and size fields are stored and are positive numbers."""
        db_path, _ = built_index

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT mtime, size FROM modules").fetchall()
        conn.close()

        assert len(rows) > 0
        for row in rows:
            assert row["mtime"] > 0, "mtime should be a positive number"
            assert row["size"] > 0, "size should be a positive number"

    def test_build_methods_match_fixture(self, built_index):
        """Methods table has all expected procedures/functions from fixture files."""
        db_path, _ = built_index

        conn = sqlite3.connect(str(db_path))
        rows = conn.execute("SELECT name FROM methods ORDER BY name").fetchall()
        conn.close()

        method_names = {r[0] for r in rows}

        # From COMMON_MODULE_BSL
        assert "ЗаполнитьТабличнуюЧасть" in method_names
        assert "ПолучитьДатуСеанса" in method_names
        assert "ВычислитьИтоги" in method_names

        # From OBJECT_MODULE_BSL
        assert "ОбработкаЗаполнения" in method_names
        assert "ПередЗаписью" in method_names
        assert "ПолучитьПредставление" in method_names

        # From MANAGER_MODULE_BSL
        assert "ПолучитьФорму" in method_names

        # From RECORDSET_MODULE_BSL
        assert "ПриЗаписи" in method_names

        # From FORM_MODULE_BSL
        assert "ОбработчикФормы" in method_names
        assert "ЛокальныйХелпер" in method_names

        # Total: 3 + 3 + 1 + 1 + 2 = 10
        assert len(method_names) == 10, f"Expected 10 methods, got {len(method_names)}: {method_names}"

    def test_build_methods_unique_constraint(self, built_index):
        """No duplicates for (module_id, name, line) in the methods table."""
        db_path, _ = built_index

        conn = sqlite3.connect(str(db_path))
        total = conn.execute("SELECT COUNT(*) FROM methods").fetchone()[0]
        unique = conn.execute("SELECT COUNT(*) FROM (SELECT DISTINCT module_id, name, line FROM methods)").fetchone()[0]
        conn.close()

        assert total == unique, "There should be no duplicate (module_id, name, line) entries"

    def test_build_calls_populated(self, built_index):
        """Calls table has entries for the qualified calls in the fixture."""
        db_path, _ = built_index

        conn = sqlite3.connect(str(db_path))
        row = conn.execute("SELECT COUNT(*) AS cnt FROM calls").fetchone()
        conn.close()

        assert row[0] > 0, "calls table should have entries"

    def test_build_calls_excludes_keywords(self, built_index):
        """BSL keywords like 'Если', 'Новый', 'Тип' should NOT appear as callee_name."""
        db_path, _ = built_index

        conn = sqlite3.connect(str(db_path))
        rows = conn.execute("SELECT DISTINCT callee_name FROM calls").fetchall()
        conn.close()

        callee_names = {r[0] for r in rows}
        keywords_that_must_be_absent = {"Если", "Новый", "Тип", "ТипЗнч", "Для", "Каждого"}
        found = callee_names & keywords_that_must_be_absent
        assert not found, f"Keywords should not be in callee_name: {found}"

    def test_build_calls_qualified(self, built_index):
        """Qualified call 'МойМодуль.ЗаполнитьТабличнуюЧасть' found in callee_name."""
        db_path, _ = built_index

        conn = sqlite3.connect(str(db_path))
        rows = conn.execute("SELECT callee_name FROM calls").fetchall()
        conn.close()

        callee_names = {r[0] for r in rows}
        assert "МойМодуль.ЗаполнитьТабличнуюЧасть" in callee_names, (
            f"Expected qualified call not found. All callees: {callee_names}"
        )


# =====================================================================
# IndexReader tests
# =====================================================================


class TestIndexReader:
    def test_reader_get_methods_by_path(self, built_index):
        """get_methods_by_path returns correct list of dicts for a known module."""
        db_path, _ = built_index
        reader = IndexReader(db_path)
        try:
            # Find the rel_path for the common module
            conn = sqlite3.connect(str(db_path))
            rel_paths = [r[0] for r in conn.execute("SELECT rel_path FROM modules").fetchall()]
            conn.close()

            cm_path = [p for p in rel_paths if "CommonModules" in p][0]
            methods = reader.get_methods_by_path(cm_path)

            assert methods is not None, "Should return methods for known path"
            assert isinstance(methods, list)
            assert len(methods) == 3  # ЗаполнитьТабличнуюЧасть, ПолучитьДатуСеанса, ВычислитьИтоги

            names = {m["name"] for m in methods}
            assert "ЗаполнитьТабличнуюЧасть" in names
            assert "ПолучитьДатуСеанса" in names
            assert "ВычислитьИтоги" in names

            # Check dict keys
            for m in methods:
                assert "name" in m
                assert "type" in m
                assert "line" in m
                assert "end_line" in m
                assert "is_export" in m
                assert "params" in m
        finally:
            reader.close()

    def test_reader_get_methods_unknown_path(self, built_index):
        """get_methods_by_path returns None for a path not in the index."""
        db_path, _ = built_index
        reader = IndexReader(db_path)
        try:
            result = reader.get_methods_by_path("НесуществующийМодуль/Ext/Module.bsl")
            assert result is None
        finally:
            reader.close()

    def test_reader_get_callers(self, built_index):
        """get_callers returns dict with 'callers' list and '_meta' dict."""
        db_path, _ = built_index
        reader = IndexReader(db_path)
        try:
            result = reader.get_callers("ЗаполнитьТабличнуюЧасть")

            assert result is not None, "Should find callers for a called method"
            assert "callers" in result
            assert "_meta" in result
            assert isinstance(result["callers"], list)
            assert isinstance(result["_meta"], dict)

            meta = result["_meta"]
            assert "total_callers" in meta
            assert "returned" in meta
            assert "offset" in meta
            assert "has_more" in meta

            # At least one caller (from ObjectModule.bsl)
            assert len(result["callers"]) >= 1

            caller = result["callers"][0]
            assert "file" in caller
            assert "caller_name" in caller
            assert "caller_is_export" in caller
            assert "line" in caller
        finally:
            reader.close()

    def test_reader_get_callers_no_calls_table(self, built_index_no_calls):
        """get_callers returns None when the index was built without calls."""
        db_path, _ = built_index_no_calls
        reader = IndexReader(db_path)
        try:
            result = reader.get_callers("ЗаполнитьТабличнуюЧасть")
            assert result is None
        finally:
            reader.close()

    def test_reader_get_callers_no_hint_meta(self, built_index):
        """get_callers without module_hint: fast COUNT matches actual rows."""
        db_path, _ = built_index
        reader = IndexReader(db_path)
        try:
            result = reader.get_callers("ЗаполнитьТабличнуюЧасть")
            meta = result["_meta"]
            assert meta["total_callers"] >= 1
            assert meta["returned"] == len(result["callers"])
            assert meta["offset"] == 0
            # has_more should be False when all rows fit in default limit
            assert meta["has_more"] is False or meta["total_callers"] > meta["returned"]
        finally:
            reader.close()

    def test_reader_get_callers_with_hint_meta(self, built_index):
        """get_callers with module_hint pointing to callee's module (cross-module)."""
        db_path, _ = built_index
        reader = IndexReader(db_path)
        try:
            # hint=МойМодуль — module where ЗаполнитьТабличнуюЧасть is DEFINED
            result = reader.get_callers("ЗаполнитьТабличнуюЧасть", module_hint="МойМодуль")
            meta = result["_meta"]
            assert meta["total_callers"] >= 1
            assert meta["returned"] == len(result["callers"])
            # Must find cross-module callers (ТестовыйДокумент, МойРегистр)
            caller_objects = {c["object_name"] for c in result["callers"]}
            assert "ТестовыйДокумент" in caller_objects
        finally:
            reader.close()

    def test_reader_get_callers_pagination(self, built_index):
        """get_callers pagination: offset + limit produce consistent _meta."""
        db_path, _ = built_index
        reader = IndexReader(db_path)
        try:
            full = reader.get_callers("ЗаполнитьТабличнуюЧасть", limit=100)
            total = full["_meta"]["total_callers"]
            if total >= 1:
                page = reader.get_callers("ЗаполнитьТабличнуюЧасть", limit=1, offset=0)
                assert page["_meta"]["total_callers"] == total
                assert page["_meta"]["returned"] == 1
                assert page["_meta"]["offset"] == 0
                if total > 1:
                    assert page["_meta"]["has_more"] is True
        finally:
            reader.close()

    def test_reader_get_callers_zero(self, built_index):
        """get_callers for a method nobody calls returns empty list."""
        db_path, _ = built_index
        reader = IndexReader(db_path)
        try:
            result = reader.get_callers("НесуществующаяПроцедура12345")
            assert result is not None
            assert result["callers"] == []
            assert result["_meta"]["total_callers"] == 0
            assert result["_meta"]["has_more"] is False
        finally:
            reader.close()

    def test_reader_get_callers_qualified(self, built_index):
        """get_callers finds qualified calls (МойМодуль.ЗаполнитьТабличнуюЧасть)."""
        db_path, _ = built_index
        reader = IndexReader(db_path)
        try:
            result = reader.get_callers("ЗаполнитьТабличнуюЧасть")
            # Should find the call from ObjectModule (МойМодуль.ЗаполнитьТабличнуюЧасть)
            assert result["_meta"]["total_callers"] >= 1
        finally:
            reader.close()

    def test_reader_get_callers_cross_module(self, built_index):
        """get_callers with hint finds cross-module caller from RecordSetModule."""
        db_path, _ = built_index
        reader = IndexReader(db_path)
        try:
            result = reader.get_callers("ЗаполнитьТабличнуюЧасть", module_hint="МойМодуль")
            caller_objects = {c["object_name"] for c in result["callers"]}
            # RecordSetModule calls МойМодуль.ЗаполнитьТабличнуюЧасть
            assert "МойРегистр" in caller_objects
        finally:
            reader.close()

    def test_reader_get_callers_unqualified_with_hint(self, built_index):
        """get_callers with hint finds unqualified calls too (not filtered out)."""
        db_path, _ = built_index
        reader = IndexReader(db_path)
        try:
            # RecordSetModule has BOTH in same method ПриЗаписи:
            #   line 2: МойМодуль.ЗаполнитьТабличнуюЧасть(...) — qualified
            #   line 3: ЗаполнитьТабличнуюЧасть(...)           — unqualified
            # ObjectModule also has 1 qualified call.
            # Without unqualified support we'd get only qualified edges.
            result = reader.get_callers("ЗаполнитьТабличнуюЧасть", module_hint="МойМодуль")
            # Count call edges from RecordSetModule specifically
            rs_callers = [c for c in result["callers"] if "RecordSetModule" in c["file"]]
            # Must have 2 edges (qualified + unqualified), not just 1
            assert len(rs_callers) == 2, (
                f"Expected 2 call edges from RecordSetModule (qualified + unqualified), got {len(rs_callers)}"
            )
        finally:
            reader.close()

    def test_reader_get_callers_non_export_exact(self, built_index):
        """Non-export method + hint: same-module local call resolves EXACTLY.

        v1.16.0 superseded the old "non-export → same file only" heuristic with
        callee_key resolution. ВычислитьИтоги is a private method of МойМодуль; its
        only real-and-resolvable caller is the same-module local call from
        ЗаполнитьТабличнуюЧасть (exact). The cross-module ``МойМодуль.ВычислитьИтоги``
        in ObjectModule does NOT resolve (qualified calls resolve only for EXPORTED
        common methods) → it stays a NULL-key edge, surfaced as a fallback edge for
        recall and counted separately in _meta.
        """
        db_path, _ = built_index
        reader = IndexReader(db_path)
        try:
            # Without hint, the private method is not a unique exported common
            # method → name-based fallback (cross-module reference IS visible).
            result_no_hint = reader.get_callers("ВычислитьИтоги")
            assert any("ObjectModule" in c["file"] for c in result_no_hint["callers"]), (
                "Sanity check: ObjectModule references ВычислитьИтоги"
            )
            assert result_no_hint["_meta"]["target_exact"] is False

            result = reader.get_callers("ВычислитьИтоги", module_hint="МойМодуль")
            meta = result["_meta"]
            # Exact resolution engaged: the same-module local caller is precise.
            assert meta["target_exact"] is True
            assert meta["exact_rows"] == 1
            exact_caller = next(c for c in result["callers"] if "CommonModules" in c["file"])
            assert exact_caller["caller_name"] == "ЗаполнитьТабличнуюЧасть"
            # The unresolved cross-module reference is kept as a labeled fallback.
            assert meta["fallback_rows"] >= 1
        finally:
            reader.close()

    def test_reader_get_callers_form_module_scoped(self, built_index):
        """Form module method: callers scoped to same form file only."""
        db_path, _ = built_index
        reader = IndexReader(db_path)
        try:
            result = reader.get_callers("ЛокальныйХелпер", module_hint="ТестовыйДокумент")
            # ЛокальныйХелпер is called from ОбработчикФормы in the same form module
            assert result is not None
            assert len(result["callers"]) >= 1, "Expected at least 1 same-form caller for form method"
            for c in result["callers"]:
                assert "Form" in c["file"], f"Form method callers must be from same form, got {c['file']}"
        finally:
            reader.close()

    def test_reader_get_exports(self, built_index):
        """get_exports_by_path returns only exported methods."""
        db_path, _ = built_index
        reader = IndexReader(db_path)
        try:
            conn = sqlite3.connect(str(db_path))
            rel_paths = [r[0] for r in conn.execute("SELECT rel_path FROM modules").fetchall()]
            conn.close()

            cm_path = [p for p in rel_paths if "CommonModules" in p][0]
            exports = reader.get_exports_by_path(cm_path)

            assert exports is not None
            export_names = {e["name"] for e in exports}

            # Only exported methods
            assert "ЗаполнитьТабличнуюЧасть" in export_names
            assert "ПолучитьДатуСеанса" in export_names
            # ВычислитьИтоги is NOT exported
            assert "ВычислитьИтоги" not in export_names
        finally:
            reader.close()

    def test_reader_get_statistics(self, built_index):
        """get_statistics returns dict with all expected keys."""
        db_path, _ = built_index
        reader = IndexReader(db_path)
        try:
            stats = reader.get_statistics()

            assert isinstance(stats, dict)
            for key in ("modules", "methods", "calls", "exports", "built_at"):
                assert key in stats, f"Missing key '{key}' in statistics"

            assert stats["modules"] == 5
            assert stats["methods"] == 10
            assert stats["calls"] > 0
            assert stats["exports"] > 0
            assert stats["built_at"] is not None
            assert stats["built_at"] > 0
        finally:
            reader.close()

    def test_reader_has_calls_true(self, built_index):
        """has_calls property is True when calls table has data."""
        db_path, _ = built_index
        reader = IndexReader(db_path)
        try:
            assert reader.has_calls is True
        finally:
            reader.close()

    def test_reader_has_calls_false(self, built_index_no_calls):
        """has_calls property is False when built without calls."""
        db_path, _ = built_index_no_calls
        reader = IndexReader(db_path)
        try:
            assert reader.has_calls is False
        finally:
            reader.close()

    def test_reader_close(self, built_index):
        """close() does not raise errors and subsequent access fails gracefully."""
        db_path, _ = built_index
        reader = IndexReader(db_path)
        reader.close()
        # Calling close again should not raise
        reader.close()


# =====================================================================
# FTS5 full-text search tests
# =====================================================================


class TestFTS:
    """Tests for FTS5 trigram-based full-text search of methods."""

    @pytest.fixture
    def built_index_fts(self, tmp_bsl_project, monkeypatch):
        """Build an index with FTS enabled."""
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_bsl_project / ".index_fts"))
        builder = IndexBuilder()
        db_path = builder.build(str(tmp_bsl_project), build_calls=True, build_fts=True)
        return db_path, str(tmp_bsl_project)

    @pytest.fixture
    def built_index_no_fts(self, tmp_bsl_project, monkeypatch):
        """Build an index WITHOUT FTS."""
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_bsl_project / ".index_nofts"))
        builder = IndexBuilder()
        db_path = builder.build(str(tmp_bsl_project), build_calls=True, build_fts=False)
        return db_path, str(tmp_bsl_project)

    def test_fts_table_created(self, built_index_fts):
        """FTS virtual table exists after build with build_fts=True."""
        db_path, _ = built_index_fts
        conn = sqlite3.connect(str(db_path))
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        assert "methods_fts" in tables

    def test_fts_search_substring(self, built_index_fts):
        """Search by substring finds matching methods."""
        db_path, _ = built_index_fts
        reader = IndexReader(db_path)
        try:
            results = reader.search_methods("Заполнить")
            names = [r["name"] for r in results]
            assert "ЗаполнитьТабличнуюЧасть" in names
        finally:
            reader.close()

    def test_fts_search_case_insensitive(self, built_index_fts):
        """Trigram search is case-insensitive."""
        db_path, _ = built_index_fts
        reader = IndexReader(db_path)
        try:
            results = reader.search_methods("заполнить")
            names = [r["name"] for r in results]
            assert "ЗаполнитьТабличнуюЧасть" in names
        finally:
            reader.close()

    def test_fts_search_by_object_name(self, built_index_fts):
        """Search matches object_name field too."""
        db_path, _ = built_index_fts
        reader = IndexReader(db_path)
        try:
            results = reader.search_methods("МойМодуль")
            assert len(results) > 0
            assert all(r["object_name"] == "МойМодуль" for r in results)
        finally:
            reader.close()

    def test_fts_search_no_results(self, built_index_fts):
        """Non-existent query returns empty list."""
        db_path, _ = built_index_fts
        reader = IndexReader(db_path)
        try:
            results = reader.search_methods("НесуществующийМетод12345")
            assert results == []
        finally:
            reader.close()

    def test_fts_search_limit(self, built_index_fts):
        """Limit parameter restricts result count."""
        db_path, _ = built_index_fts
        reader = IndexReader(db_path)
        try:
            results = reader.search_methods("Процедур", limit=1)
            assert len(results) <= 1
        finally:
            reader.close()

    def test_fts_search_returns_fields(self, built_index_fts):
        """Each result has all expected fields including rank."""
        db_path, _ = built_index_fts
        reader = IndexReader(db_path)
        try:
            results = reader.search_methods("Заполнить")
            assert len(results) > 0
            r = results[0]
            expected_keys = {
                "name",
                "type",
                "is_export",
                "line",
                "end_line",
                "params",
                "module_path",
                "object_name",
                "rank",
            }
            assert set(r.keys()) == expected_keys
        finally:
            reader.close()

    def test_build_no_fts(self, built_index_no_fts):
        """--no-fts flag skips FTS table creation."""
        db_path, _ = built_index_no_fts
        conn = sqlite3.connect(str(db_path))
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        assert "methods_fts" not in tables

    def test_search_without_fts_table(self, built_index_no_fts):
        """search_methods returns [] when FTS not built."""
        db_path, _ = built_index_no_fts
        reader = IndexReader(db_path)
        try:
            results = reader.search_methods("Заполнить")
            assert results == []
        finally:
            reader.close()

    def test_has_fts_property(self, built_index_fts, built_index_no_fts):
        """has_fts property reflects FTS presence."""
        db_path_fts, _ = built_index_fts
        reader_fts = IndexReader(db_path_fts)
        try:
            assert reader_fts.has_fts is True
        finally:
            reader_fts.close()

        db_path_no, _ = built_index_no_fts
        reader_no = IndexReader(db_path_no)
        try:
            assert reader_no.has_fts is False
        finally:
            reader_no.close()

    def test_fts_search_empty_query(self, built_index_fts):
        """Empty query returns empty list."""
        db_path, _ = built_index_fts
        reader = IndexReader(db_path)
        try:
            assert reader.search_methods("") == []
            assert reader.search_methods("  ") == []
        finally:
            reader.close()


# =====================================================================
# Incremental update tests
# =====================================================================

NEW_FILE_BSL = """\
Процедура НоваяПроцедура() Экспорт
    Сообщить("Новый файл");
КонецПроцедуры
"""

MODIFIED_MODULE_BSL = """\
Функция ПолучитьФорму(ИмяФормы, Параметры, Владелец) Экспорт
    Возврат ПолучитьОбщуюФорму("ФормаВыбора");
КонецФункции

Функция ДополнительнаяФункция() Экспорт
    Возврат 42;
КонецФункции
"""


class TestIncrementalUpdate:
    def test_update_new_file(self, built_index, monkeypatch):
        """Adding a new .bsl file and running update makes it appear in the index."""
        db_path, base_path = built_index

        # Add a new file
        new_dir = tmp_path_from_base(base_path) / "Catalogs" / "НовыйСправочник" / "Ext"
        new_dir.mkdir(parents=True)
        (new_dir / "ObjectModule.bsl").write_text(NEW_FILE_BSL, encoding="utf-8-sig")

        builder = IndexBuilder()
        result = builder.update(base_path)

        assert result["added"] == 1

        # Verify new module in DB
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute("SELECT rel_path FROM modules").fetchall()
        conn.close()

        rel_paths = [r[0] for r in rows]
        assert any("НовыйСправочник" in p for p in rel_paths), f"New module not found. All paths: {rel_paths}"

    def test_update_changed_file(self, built_index, monkeypatch):
        """Modifying an existing .bsl file and running update refreshes methods."""
        db_path, base_path = built_index
        base = tmp_path_from_base(base_path)

        manager_path = base / "Documents" / "ТестовыйДокумент" / "Ext" / "ManagerModule.bsl"

        # Count methods before
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        mod_row = conn.execute("SELECT id FROM modules WHERE rel_path LIKE '%ManagerModule%'").fetchone()
        methods_before = conn.execute(
            "SELECT COUNT(*) AS cnt FROM methods WHERE module_id = ?",
            (mod_row["id"],),
        ).fetchone()["cnt"]
        conn.close()

        assert methods_before == 1, "ManagerModule initially has 1 method"

        # Modify the file — add a second method
        # Ensure mtime changes by waiting briefly and then touching
        time.sleep(0.1)
        manager_path.write_text(MODIFIED_MODULE_BSL, encoding="utf-8-sig")

        builder = IndexBuilder()
        result = builder.update(base_path)

        assert result["changed"] == 1

        # Verify methods refreshed
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        mod_row = conn.execute("SELECT id FROM modules WHERE rel_path LIKE '%ManagerModule%'").fetchone()
        methods_after = conn.execute(
            "SELECT COUNT(*) AS cnt FROM methods WHERE module_id = ?",
            (mod_row["id"],),
        ).fetchone()["cnt"]
        conn.close()

        assert methods_after == 2, f"After modification, ManagerModule should have 2 methods, got {methods_after}"

    def test_update_removed_file(self, built_index, monkeypatch):
        """Deleting a .bsl file and running update removes its methods and calls."""
        db_path, base_path = built_index
        base = tmp_path_from_base(base_path)

        manager_path = base / "Documents" / "ТестовыйДокумент" / "Ext" / "ManagerModule.bsl"
        manager_path.unlink()

        builder = IndexBuilder()
        result = builder.update(base_path)

        assert result["removed"] == 1

        # Verify module removed from DB
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute("SELECT rel_path FROM modules WHERE rel_path LIKE '%ManagerModule%'").fetchall()
        conn.close()

        assert len(rows) == 0, "Removed file should not be in modules table"

    def test_update_unchanged_skipped(self, built_index, monkeypatch):
        """Unchanged files are not re-parsed; delta counts are all zero."""
        db_path, base_path = built_index

        # Get method count before
        conn = sqlite3.connect(str(db_path))
        count_before = conn.execute("SELECT COUNT(*) FROM methods").fetchone()[0]
        conn.close()

        builder = IndexBuilder()
        result = builder.update(base_path)

        assert result["added"] == 0
        assert result["changed"] == 0
        assert result["removed"] == 0

        # Method count unchanged
        conn = sqlite3.connect(str(db_path))
        count_after = conn.execute("SELECT COUNT(*) FROM methods").fetchone()[0]
        conn.close()

        assert count_before == count_after

    def test_update_returns_delta_stats(self, built_index, monkeypatch):
        """update() returns a dict with added/changed/removed keys."""
        _, base_path = built_index

        builder = IndexBuilder()
        result = builder.update(base_path)

        assert isinstance(result, dict)
        assert "added" in result
        assert "changed" in result
        assert "removed" in result
        # Counts are ints; the delta also carries diagnostic fields
        # (git_fast_path: bool, git_fallback_reason: str|None).
        assert all(isinstance(result[k], int) for k in ("added", "changed", "removed"))


# =====================================================================
# Staleness / freshness tests
# =====================================================================


class TestFreshness:
    def test_freshness_fresh(self, built_index):
        """Immediately after build, freshness check returns FRESH."""
        db_path, base_path = built_index
        base = tmp_path_from_base(base_path)

        bsl_files = sorted(base.rglob("*.bsl"))
        rel_paths = [f.relative_to(base).as_posix() for f in bsl_files]
        paths_hash = _paths_hash(rel_paths)

        status = check_index_freshness(
            db_path=db_path,
            current_bsl_count=len(bsl_files),
            current_paths_hash=paths_hash,
            base_path=base_path,
        )
        assert status == IndexStatus.FRESH

    def test_freshness_stale_structure(self, built_index):
        """Adding a file without update -> STALE due to count/hash mismatch."""
        db_path, base_path = built_index
        base = tmp_path_from_base(base_path)

        # Add a new file on disk (but don't update index)
        new_dir = base / "Catalogs" / "ДополнительныйСправочник" / "Ext"
        new_dir.mkdir(parents=True)
        (new_dir / "ObjectModule.bsl").write_text(
            "Процедура Тест() Экспорт\nКонецПроцедуры\n",
            encoding="utf-8-sig",
        )

        bsl_files = sorted(base.rglob("*.bsl"))
        rel_paths = [f.relative_to(base).as_posix() for f in bsl_files]
        paths_hash = _paths_hash(rel_paths)

        status = check_index_freshness(
            db_path=db_path,
            current_bsl_count=len(bsl_files),
            current_paths_hash=paths_hash,
            base_path=base_path,
        )
        assert status == IndexStatus.STALE

    def test_freshness_missing(self, tmp_path):
        """When no database file exists, freshness returns MISSING."""
        non_existent = tmp_path / "does_not_exist.db"
        status = check_index_freshness(
            db_path=non_existent,
            current_bsl_count=5,
            current_paths_hash="abc",
            base_path=str(tmp_path),
        )
        assert status == IndexStatus.MISSING


# =====================================================================
# check_index_usable tests (lightweight, no rglob)
# =====================================================================


class TestCheckIndexUsable:
    def test_usable_fresh(self, built_index):
        """Immediately after build, usable check returns FRESH."""
        db_path, base_path = built_index
        status = check_index_usable(db_path, base_path)
        assert status == IndexStatus.FRESH

    def test_usable_missing(self, tmp_path):
        """Non-existent DB returns MISSING."""
        status = check_index_usable(tmp_path / "no.db", str(tmp_path))
        assert status == IndexStatus.MISSING

    def test_usable_stale_age(self, built_index, monkeypatch):
        """Index older than max_age_days returns STALE_AGE."""
        db_path, base_path = built_index
        monkeypatch.setenv("RLM_INDEX_MAX_AGE_DAYS", "0")
        # Also need to disable skip_sample_hours to not short-circuit to FRESH
        monkeypatch.setenv("RLM_INDEX_SKIP_SAMPLE_HOURS", "0")
        status = check_index_usable(db_path, base_path)
        assert status == IndexStatus.STALE_AGE

    def test_usable_skip_sample_for_young_index(self, built_index, monkeypatch):
        """Index younger than SKIP_SAMPLE_HOURS skips sampling, returns FRESH."""
        db_path, base_path = built_index
        monkeypatch.setenv("RLM_INDEX_SKIP_SAMPLE_HOURS", "9999")
        status = check_index_usable(db_path, base_path)
        assert status == IndexStatus.FRESH

    def test_usable_stale_content(self, built_index, monkeypatch):
        """Modified files trigger STALE_CONTENT."""
        db_path, base_path = built_index
        base = tmp_path_from_base(base_path)
        monkeypatch.setenv("RLM_INDEX_SKIP_SAMPLE_HOURS", "0")
        monkeypatch.setenv("RLM_INDEX_SAMPLE_SIZE", "100")
        monkeypatch.setenv("RLM_INDEX_SAMPLE_THRESHOLD", "1")

        # Modify all BSL files to trigger mtime mismatch
        for bsl in base.rglob("*.bsl"):
            bsl.write_text(bsl.read_text(encoding="utf-8-sig") + "\n// changed", encoding="utf-8-sig")

        status = check_index_usable(db_path, base_path)
        assert status == IndexStatus.STALE_CONTENT

    def test_usable_does_not_check_structure(self, built_index):
        """Usable check does NOT detect added files (no structural check)."""
        db_path, base_path = built_index
        base = tmp_path_from_base(base_path)

        # Add a new file on disk
        new_dir = base / "Catalogs" / "НовыйСправочник" / "Ext"
        new_dir.mkdir(parents=True)
        (new_dir / "ObjectModule.bsl").write_text(
            "Процедура Тест() Экспорт\nКонецПроцедуры\n",
            encoding="utf-8-sig",
        )

        # Usable check should still return FRESH (no structural check)
        status = check_index_usable(db_path, base_path)
        assert status == IndexStatus.FRESH


class TestCheckIndexStrict:
    def test_strict_is_alias(self):
        """check_index_freshness is an alias for check_index_strict."""
        assert check_index_freshness is check_index_strict

    def test_strict_fresh(self, built_index):
        """Strict check returns FRESH when everything matches."""
        db_path, base_path = built_index
        base = tmp_path_from_base(base_path)

        bsl_files = sorted(base.rglob("*.bsl"))
        rel_paths = [f.relative_to(base).as_posix() for f in bsl_files]
        paths_hash = _paths_hash(rel_paths)

        status = check_index_strict(db_path, len(bsl_files), paths_hash, base_path)
        assert status == IndexStatus.FRESH

    def test_strict_stale_structure(self, built_index):
        """Strict check detects added files via structural mismatch."""
        db_path, base_path = built_index
        base = tmp_path_from_base(base_path)

        new_dir = base / "Catalogs" / "ДополнительныйСправочник" / "Ext"
        new_dir.mkdir(parents=True)
        (new_dir / "ObjectModule.bsl").write_text(
            "Процедура Тест() Экспорт\nКонецПроцедуры\n",
            encoding="utf-8-sig",
        )

        bsl_files = sorted(base.rglob("*.bsl"))
        rel_paths = [f.relative_to(base).as_posix() for f in bsl_files]
        paths_hash = _paths_hash(rel_paths)

        status = check_index_strict(db_path, len(bsl_files), paths_hash, base_path)
        assert status == IndexStatus.STALE


class TestBslCountInStats:
    def test_statistics_include_bsl_count(self, built_index):
        """get_statistics() returns bsl_count as int from index_meta."""
        db_path, base_path = built_index
        reader = IndexReader(db_path)
        stats = reader.get_statistics()
        reader.close()

        assert "bsl_count" in stats
        assert isinstance(stats["bsl_count"], int)
        assert stats["bsl_count"] > 0


class TestGetAllModules:
    def test_get_all_modules_returns_all(self, built_index):
        """get_all_modules() returns all modules with correct fields."""
        db_path, base_path = built_index
        reader = IndexReader(db_path)
        modules = reader.get_all_modules()
        reader.close()

        assert len(modules) > 0
        for m in modules:
            assert "rel_path" in m
            assert "category" in m
            assert "object_name" in m
            assert "module_type" in m
            assert "form_name" in m

    def test_get_all_modules_matches_count(self, built_index):
        """get_all_modules() count matches get_statistics().modules."""
        db_path, base_path = built_index
        reader = IndexReader(db_path)
        modules = reader.get_all_modules()
        stats = reader.get_statistics()
        reader.close()

        assert len(modules) == stats["modules"]


# =====================================================================
# ENV / path helper tests
# =====================================================================


class TestPathHelpers:
    def test_index_dir_from_env(self, monkeypatch, tmp_path):
        """RLM_INDEX_DIR env var overrides default index directory."""
        custom_dir = str(tmp_path / "custom_index")
        monkeypatch.setenv("RLM_INDEX_DIR", custom_dir)

        result = get_index_dir("/some/base")
        assert result == tmp_path / "custom_index"

    def test_index_dir_default(self, monkeypatch):
        """Without RLM_INDEX_DIR, uses ~/.cache/rlm-tools-bsl/."""
        monkeypatch.delenv("RLM_INDEX_DIR", raising=False)
        # Этот тест про САМЫЙ последний fallback, поэтому снимаем и вторую
        # ступень precedence, которую conftest выставляет всем тестам ради
        # изоляции spawn-детей (dirname(RLM_CONFIG_FILE)/index).
        monkeypatch.delenv("RLM_CONFIG_FILE", raising=False)

        from pathlib import Path

        result = get_index_dir("/some/base")
        expected = Path.home() / ".cache" / "rlm-tools-bsl"
        assert result == expected

    def test_index_db_path_uses_hash(self, monkeypatch, tmp_path):
        """Index DB path contains an MD5 hash of base_path."""
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path))

        import hashlib

        base = "/my/project/path"
        expected_hash = hashlib.md5(base.encode()).hexdigest()[:12]

        result = get_index_db_path(base)
        assert expected_hash in str(result), f"Expected hash {expected_hash} in path {result}"
        assert result.name == "bsl_index.db"

    def test_get_index_db_path_migrates_old_name(self, monkeypatch, tmp_path):
        """Old method_index.db is automatically renamed to bsl_index.db."""
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path))

        import hashlib

        base = "/migrate/test"
        h = hashlib.md5(base.encode()).hexdigest()[:12]
        index_dir = tmp_path / h
        index_dir.mkdir(parents=True)

        old_db = index_dir / "method_index.db"
        old_lock = index_dir / "method_index.lock"
        old_db.write_text("test")
        old_lock.write_text("")

        result = get_index_db_path(base)
        assert result.name == "bsl_index.db"
        assert (index_dir / "bsl_index.db").exists()
        assert not old_db.exists()
        assert (index_dir / "bsl_index.lock").exists()
        assert not old_lock.exists()

    def test_get_index_db_path_fallback_on_failed_rename(self, monkeypatch, tmp_path):
        """If rename fails, fallback to old method_index.db."""
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path))

        import hashlib
        from pathlib import Path

        base = "/fallback/test"
        h = hashlib.md5(base.encode()).hexdigest()[:12]
        index_dir = tmp_path / h
        index_dir.mkdir(parents=True)

        old_db = index_dir / "method_index.db"
        old_db.write_text("test")

        original_rename = Path.rename

        def failing_rename(self, target):
            if self.name == "method_index.db":
                raise OSError("permission denied")
            return original_rename(self, target)

        monkeypatch.setattr(Path, "rename", failing_rename)

        result = get_index_db_path(base)
        assert result.name == "method_index.db"


# =====================================================================
# get_index_dir_root() precedence + legacy migration (v1.9.2)
# =====================================================================


@pytest.fixture
def _isolated_home(monkeypatch, tmp_path):
    """Redirect ``Path.home()`` and clear RLM_INDEX_DIR / RLM_CONFIG_FILE.

    Critical for migration tests: ``migrate_legacy_index_root`` reads
    ``Path.home() / ".cache" / "rlm-tools-bsl"`` and would otherwise touch the
    developer's real index directory. Patching ``Path.home`` directly is the
    most robust approach (no OS-specific env-var dance).
    """
    import pathlib

    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    monkeypatch.setattr(pathlib.Path, "home", lambda: fake_home)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    monkeypatch.delenv("RLM_INDEX_DIR", raising=False)
    monkeypatch.delenv("RLM_CONFIG_FILE", raising=False)
    return fake_home


class TestIndexDirRootPrecedence:
    """Precedence: RLM_INDEX_DIR > RLM_CONFIG_FILE > home fallback."""

    def test_index_dir_root_respects_rlm_config_file(self, _isolated_home, monkeypatch, tmp_path):
        """Without RLM_INDEX_DIR, RLM_CONFIG_FILE drives the root → dirname/index."""
        from rlm_tools_bsl.bsl_index import get_index_dir_root

        config_file = tmp_path / "service.json"
        config_file.write_text("{}", encoding="utf-8")
        monkeypatch.setenv("RLM_CONFIG_FILE", str(config_file))
        monkeypatch.delenv("RLM_INDEX_DIR", raising=False)

        assert get_index_dir_root() == tmp_path / "index"

    def test_index_dir_root_rlm_index_dir_wins(self, _isolated_home, monkeypatch, tmp_path):
        """When both env are set, RLM_INDEX_DIR (explicit override) wins."""
        from rlm_tools_bsl.bsl_index import get_index_dir_root

        config_file = tmp_path / "service.json"
        config_file.write_text("{}", encoding="utf-8")
        explicit_dir = tmp_path / "explicit_index"
        monkeypatch.setenv("RLM_CONFIG_FILE", str(config_file))
        monkeypatch.setenv("RLM_INDEX_DIR", str(explicit_dir))

        assert get_index_dir_root() == explicit_dir

    def test_index_dir_root_default_when_no_env(self, _isolated_home):
        """Both env unset → ~/.cache/rlm-tools-bsl (home fallback)."""
        from pathlib import Path

        from rlm_tools_bsl.bsl_index import get_index_dir_root

        assert get_index_dir_root() == Path.home() / ".cache" / "rlm-tools-bsl"


class TestIndexDirMigration:
    """One-shot migration of legacy ~/.cache/rlm-tools-bsl/<hash>/ subdirs."""

    @staticmethod
    def _legacy_root(home):
        return home / ".cache" / "rlm-tools-bsl"

    @staticmethod
    def _new_root(tmp_path):
        config_dir = tmp_path / "service-cfg"
        config_dir.mkdir(parents=True, exist_ok=True)
        return config_dir, config_dir / "service.json"

    def test_migrate_legacy_index_root_moves_db_subdirs_only(self, _isolated_home, monkeypatch, tmp_path):
        """Move only subdirs containing bsl_index.db / method_index.db."""
        from rlm_tools_bsl.bsl_index import migrate_legacy_index_root

        legacy = self._legacy_root(_isolated_home)
        legacy.mkdir(parents=True)
        # hash1 has bsl_index.db → migrate
        (legacy / "hash1").mkdir()
        (legacy / "hash1" / "bsl_index.db").write_bytes(b"db1")
        (legacy / "hash1" / "bsl_index.lock").write_bytes(b"")
        # hash2 has method_index.db (legacy name) → migrate
        (legacy / "hash2").mkdir()
        (legacy / "hash2" / "method_index.db").write_bytes(b"db2")
        # hash3 has only file_index.json → NOT a real index, leave alone
        (legacy / "hash3").mkdir()
        (legacy / "hash3" / "file_index.json").write_text("{}")
        # loose file at root → leave alone
        (legacy / "loose_file.txt").write_text("x")

        config_dir, config_file = self._new_root(tmp_path)
        config_file.write_text("{}", encoding="utf-8")
        monkeypatch.setenv("RLM_CONFIG_FILE", str(config_file))

        moved = migrate_legacy_index_root()

        assert moved == 2
        new_root = config_dir / "index"
        assert (new_root / "hash1" / "bsl_index.db").read_bytes() == b"db1"
        assert (new_root / "hash2" / "method_index.db").read_bytes() == b"db2"
        # Source for migrated dirs is gone
        assert not (legacy / "hash1").exists()
        assert not (legacy / "hash2").exists()
        # Non-index leftovers remain
        assert (legacy / "hash3" / "file_index.json").exists()
        assert (legacy / "loose_file.txt").exists()

    def test_migrate_legacy_index_root_idempotent(self, _isolated_home, monkeypatch, tmp_path):
        """Second call returns 0 without error."""
        from rlm_tools_bsl.bsl_index import migrate_legacy_index_root

        legacy = self._legacy_root(_isolated_home)
        legacy.mkdir(parents=True)
        (legacy / "hashX").mkdir()
        (legacy / "hashX" / "bsl_index.db").write_bytes(b"x")

        config_dir, config_file = self._new_root(tmp_path)
        config_file.write_text("{}", encoding="utf-8")
        monkeypatch.setenv("RLM_CONFIG_FILE", str(config_file))

        first = migrate_legacy_index_root()
        second = migrate_legacy_index_root()

        assert first == 1
        assert second == 0
        assert (config_dir / "index" / "hashX" / "bsl_index.db").exists()

    def test_migrate_legacy_index_root_skip_when_target_exists(self, _isolated_home, monkeypatch, tmp_path):
        """If new_root/<hash> already exists, do not overwrite."""
        from rlm_tools_bsl.bsl_index import migrate_legacy_index_root

        legacy = self._legacy_root(_isolated_home)
        legacy.mkdir(parents=True)
        (legacy / "hashY").mkdir()
        (legacy / "hashY" / "bsl_index.db").write_bytes(b"old")

        config_dir, config_file = self._new_root(tmp_path)
        config_file.write_text("{}", encoding="utf-8")
        new_root = config_dir / "index"
        (new_root / "hashY").mkdir(parents=True)
        (new_root / "hashY" / "bsl_index.db").write_bytes(b"new")
        monkeypatch.setenv("RLM_CONFIG_FILE", str(config_file))

        moved = migrate_legacy_index_root()

        assert moved == 0
        # Target untouched (still "new")
        assert (new_root / "hashY" / "bsl_index.db").read_bytes() == b"new"
        # Source remains in legacy (we did not delete it)
        assert (legacy / "hashY" / "bsl_index.db").read_bytes() == b"old"

    def test_migrate_legacy_index_root_noop_when_RLM_INDEX_DIR_set(self, _isolated_home, monkeypatch, tmp_path):
        """If RLM_INDEX_DIR is set the user chose explicitly — do not touch legacy."""
        from rlm_tools_bsl.bsl_index import migrate_legacy_index_root

        legacy = self._legacy_root(_isolated_home)
        legacy.mkdir(parents=True)
        (legacy / "hashZ").mkdir()
        (legacy / "hashZ" / "bsl_index.db").write_bytes(b"z")

        config_dir, config_file = self._new_root(tmp_path)
        config_file.write_text("{}", encoding="utf-8")
        explicit = tmp_path / "explicit_idx"
        monkeypatch.setenv("RLM_CONFIG_FILE", str(config_file))
        monkeypatch.setenv("RLM_INDEX_DIR", str(explicit))

        moved = migrate_legacy_index_root()

        assert moved == 0
        # Legacy untouched
        assert (legacy / "hashZ" / "bsl_index.db").exists()
        # Nothing created at new_root or explicit
        assert not (config_dir / "index").exists()
        assert not explicit.exists()

    def test_migrate_legacy_index_root_noop_when_legacy_eq_new(self, _isolated_home, monkeypatch):
        """Desktop install: legacy_root == new_root → NOOP, no errors."""
        from rlm_tools_bsl.bsl_index import migrate_legacy_index_root

        legacy = self._legacy_root(_isolated_home)
        legacy.mkdir(parents=True)
        (legacy / "hashD").mkdir()
        (legacy / "hashD" / "bsl_index.db").write_bytes(b"d")

        # No RLM_CONFIG_FILE → new_root is also ~/.cache/rlm-tools-bsl
        monkeypatch.delenv("RLM_CONFIG_FILE", raising=False)
        monkeypatch.delenv("RLM_INDEX_DIR", raising=False)

        moved = migrate_legacy_index_root()

        assert moved == 0
        assert (legacy / "hashD" / "bsl_index.db").exists()

    def test_migrate_legacy_index_root_noop_when_legacy_missing(self, _isolated_home, monkeypatch, tmp_path):
        """Fresh install: legacy_root never created → NOOP."""
        from rlm_tools_bsl.bsl_index import migrate_legacy_index_root

        config_dir, config_file = self._new_root(tmp_path)
        config_file.write_text("{}", encoding="utf-8")
        monkeypatch.setenv("RLM_CONFIG_FILE", str(config_file))

        # Legacy root does NOT exist (no .cache/rlm-tools-bsl created)
        legacy = self._legacy_root(_isolated_home)
        assert not legacy.exists()

        moved = migrate_legacy_index_root()

        assert moved == 0


# ---------------------------------------------------------------------------
# Helper for resolving pathlib.Path from base_path string
# ---------------------------------------------------------------------------


def tmp_path_from_base(base_path: str):
    """Convert base_path string back to pathlib.Path."""
    from pathlib import Path

    return Path(base_path)


# ---------------------------------------------------------------------------
# v1.23.0 — exact-ref reader methods (category-aware adapters for
# get_object_profile). Verify Document.X does NOT pull in Document.XYZ.
# ---------------------------------------------------------------------------


def _minimal_index_with_meta(tmp_path, monkeypatch):
    """Build a minimal index (empty config) so the metadata tables exist, then
    return (db_path) ready for direct row inserts."""
    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / ".index_exact"))
    (tmp_path / "Configuration.xml").write_text("<Configuration/>", encoding="utf-8")
    builder = IndexBuilder()
    db_path = builder.build(str(tmp_path), build_calls=False, build_metadata=True)
    return db_path


def _index_with_subscriptions(tmp_path, monkeypatch, subs):
    """subs: list[(name, source_types, event)] → db_path с засеянной event_subscriptions.

    Строится поверх _minimal_index_with_meta — тот пинит RLM_INDEX_DIR в tmp, без чего
    сборка ушла бы в общий индекс-каталог машины.
    """
    db_path = _minimal_index_with_meta(tmp_path, monkeypatch)
    conn = sqlite3.connect(str(db_path))
    conn.executemany(
        "INSERT INTO event_subscriptions (name, synonym, event, handler_module, "
        "handler_procedure, source_types, source_count, file) "
        "VALUES (?, '', ?, 'ОбщийМодуль', 'Обработчик', ?, ?, 'x.xml')",
        [(n, ev, json.dumps(st, ensure_ascii=False), len(st)) for n, st, ev in subs],
    )
    conn.commit()
    conn.close()
    return db_path


class TestExactRefReaders:
    """get_roles_exact / get_event_subscriptions_exact / get_functional_options_exact."""

    def _seed(self, db_path):
        conn = sqlite3.connect(str(db_path))
        # role_rights: object-level grant, a prefix-collision sibling, a member right.
        conn.executemany(
            "INSERT INTO role_rights (role_name, object_name, right_name, file) VALUES (?, ?, ?, ?)",
            [
                ("РольА", "Document.ЗаказПоставщику", "Read", "Roles/РольА/Ext/Rights.xml"),
                ("РольА", "Document.ЗаказПоставщику", "Update", "Roles/РольА/Ext/Rights.xml"),
                ("РольB", "Document.ЗаказПоставщикуРасш", "Read", "Roles/РольB/Ext/Rights.xml"),
                (
                    "РольC",
                    "Document.ЗаказПоставщику.Attribute.Контрагент",
                    "View",
                    "Roles/РольC/Ext/Rights.xml",
                ),
            ],
        )
        # event_subscriptions: source_types are type-FORMS (need canonicalize_type_ref).
        conn.executemany(
            "INSERT INTO event_subscriptions (name, synonym, event, handler_module, "
            "handler_procedure, source_types, source_count, file) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    "ПодпискаЗаказ",
                    "",
                    "BeforeWrite",
                    "ОбщийМодуль",
                    "Обработчик",
                    json.dumps(["DocumentObject.ЗаказПоставщику"], ensure_ascii=False),
                    1,
                    "f1.xml",
                ),
                (
                    "ПодпискаРасш",
                    "",
                    "OnWrite",
                    "ОбщийМодуль",
                    "Обработчик2",
                    json.dumps(["DocumentObject.ЗаказПоставщикуРасш"], ensure_ascii=False),
                    1,
                    "f2.xml",
                ),
                (
                    "ПодпискаКонтр",
                    "",
                    "OnWrite",
                    "ОбщийМодуль",
                    "Обработчик3",
                    json.dumps(["CatalogRef.Контрагенты"], ensure_ascii=False),
                    1,
                    "f3.xml",
                ),
            ],
        )
        # functional_options: content is canonical refs (object-level + member + collision).
        conn.executemany(
            "INSERT INTO functional_options (name, synonym, location, content, file) VALUES (?, ?, ?, ?, ?)",
            [
                ("ФО_Заказ", "", "", json.dumps(["Document.ЗаказПоставщику"], ensure_ascii=False), "fo1.xml"),
                ("ФО_Расш", "", "", json.dumps(["Document.ЗаказПоставщикуРасш"], ensure_ascii=False), "fo2.xml"),
                (
                    "ФО_Член",
                    "",
                    "",
                    json.dumps(["Document.ЗаказПоставщику.Attribute.Сумма"], ensure_ascii=False),
                    "fo3.xml",
                ),
            ],
        )
        conn.commit()
        conn.close()

    def test_get_roles_cyrillic_case_insensitive(self, tmp_path, monkeypatch):
        db_path = _minimal_index_with_meta(tmp_path, monkeypatch)
        self._seed(db_path)
        reader = IndexReader(str(db_path))
        try:
            # нижний регистр кириллицы должен найти PascalCase object_name (py_lower)
            roles = reader.get_roles("заказпоставщику")
            assert roles, "get_roles должен фолдить кириллицу (заказпоставщику → ЗаказПоставщику)"
            assert any(r["role_name"] == "РольА" for r in roles)
        finally:
            reader.close()

    def test_get_roles_exact_no_collision(self, tmp_path, monkeypatch):
        db_path = _minimal_index_with_meta(tmp_path, monkeypatch)
        self._seed(db_path)
        reader = IndexReader(str(db_path))
        try:
            roles = reader.get_roles_exact("Document.ЗаказПоставщику")
            names = {r["role_name"] for r in roles}
            assert names == {"РольА"}, f"Document.X must NOT pull Document.XYZ / member; got {names}"
            role_a = next(r for r in roles if r["role_name"] == "РольА")
            assert set(role_a["rights"]) == {"Read", "Update"}
        finally:
            reader.close()

    def test_get_roles_exact_ascii_prefix_case_insensitive(self, tmp_path, monkeypatch):
        db_path = _minimal_index_with_meta(tmp_path, monkeypatch)
        self._seed(db_path)
        reader = IndexReader(str(db_path))
        try:
            # ASCII prefix folded by COLLATE NOCASE; Cyrillic tail must still match.
            roles = reader.get_roles_exact("document.ЗаказПоставщику")
            assert {r["role_name"] for r in roles} == {"РольА"}
        finally:
            reader.close()

    def test_get_roles_exact_include_members(self, tmp_path, monkeypatch):
        db_path = _minimal_index_with_meta(tmp_path, monkeypatch)
        self._seed(db_path)
        reader = IndexReader(str(db_path))
        try:
            roles = reader.get_roles_exact("Document.ЗаказПоставщику", include_members=True)
            assert {r["role_name"] for r in roles} == {"РольА", "РольC"}
        finally:
            reader.close()

    def test_get_roles_exact_empty_table_none_vs_no_match_empty(self, tmp_path, monkeypatch):
        db_path = _minimal_index_with_meta(tmp_path, monkeypatch)
        reader = IndexReader(str(db_path))
        try:
            # Empty role_rights table → None (caller marks 'unavailable').
            assert reader.get_roles_exact("Document.ЗаказПоставщику") is None
        finally:
            reader.close()
        # Seed, then ask for a ref that has no grant → [] (authoritative empty).
        self._seed(db_path)
        reader = IndexReader(str(db_path))
        try:
            assert reader.get_roles_exact("Document.НесуществующийДок") == []
        finally:
            reader.close()

    def test_get_event_subscriptions_exact_no_collision(self, tmp_path, monkeypatch):
        db_path = _minimal_index_with_meta(tmp_path, monkeypatch)
        self._seed(db_path)
        reader = IndexReader(str(db_path))
        try:
            subs = reader.get_event_subscriptions_exact("Document.ЗаказПоставщику")
            names = {s["name"] for s in subs}
            assert names == {"ПодпискаЗаказ"}, f"type-form must canonicalize exact; got {names}"
            assert subs[0]["event"] == "BeforeWrite"
        finally:
            reader.close()

    def test_get_event_subscriptions_exact_empty_vs_no_match(self, tmp_path, monkeypatch):
        db_path = _minimal_index_with_meta(tmp_path, monkeypatch)
        reader = IndexReader(str(db_path))
        try:
            assert reader.get_event_subscriptions_exact("Document.X") is None
        finally:
            reader.close()
        self._seed(db_path)
        reader = IndexReader(str(db_path))
        try:
            assert reader.get_event_subscriptions_exact("Document.НетТакого") == []
        finally:
            reader.close()

    def test_get_event_subscriptions_exact_includes_universal(self, tmp_path, monkeypatch):
        """#2 (v1.28.0): universal (пустой source_types) подписки-catch-all включаются
        в exact-выборку (паритет с find_event_subscriptions), помечаются scope и идут
        ПОСЛЕ exact (стабильно). Именованное точное сопоставление сохранено — non-matching
        named подписка (Catalog.Контрагенты) в выборку по документу НЕ попадает."""
        db_path = _minimal_index_with_meta(tmp_path, monkeypatch)
        conn = sqlite3.connect(str(db_path))
        conn.executemany(
            "INSERT INTO event_subscriptions (name, synonym, event, handler_module, "
            "handler_procedure, source_types, source_count, file) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                # universal catch-all (пустой source_types) — раньше молча дропалась.
                ("ПодпискаUniversal", "", "OnWrite", "ОМ", "H0", json.dumps([], ensure_ascii=False), 0, "u.xml"),
                # exact named source (type-форма → канонизируется).
                (
                    "ПодпискаЗаказ",
                    "",
                    "BeforeWrite",
                    "ОМ",
                    "H1",
                    json.dumps(["DocumentObject.ЗаказПоставщику"], ensure_ascii=False),
                    1,
                    "f1.xml",
                ),
                # non-matching named source — не должна попасть в выборку по документу.
                (
                    "ПодпискаКонтр",
                    "",
                    "OnWrite",
                    "ОМ",
                    "H2",
                    json.dumps(["CatalogRef.Контрагенты"], ensure_ascii=False),
                    1,
                    "f3.xml",
                ),
            ],
        )
        conn.commit()
        conn.close()
        reader = IndexReader(str(db_path))
        try:
            subs = reader.get_event_subscriptions_exact("Document.ЗаказПоставщику")
            names = [s["name"] for s in subs]
            assert set(names) == {"ПодпискаЗаказ", "ПодпискаUniversal"}
            assert "ПодпискаКонтр" not in names  # именованный non-match не протекает
            by_name = {s["name"]: s for s in subs}
            assert by_name["ПодпискаЗаказ"]["scope"] == "exact"
            assert by_name["ПодпискаUniversal"]["scope"] == "universal"
            # exact ПЕРЕД universal (preview всегда показывает явные первыми).
            assert names.index("ПодпискаЗаказ") < names.index("ПодпискаUniversal")
        finally:
            reader.close()

    def test_get_event_subscriptions_exact_first_no_substring_false_positive(self, tmp_path, monkeypatch):
        """Полное имя объекта НЕ должно цеплять более длинный омоним (ДРУГОЙ объект)."""
        db = _index_with_subscriptions(
            tmp_path,
            monkeypatch,
            [
                ("ПодпискаТочная", ["DocumentObject.РеализацияТоваровУслуг"], "ПередЗаписью"),
                ("ПодпискаОмоним", ["CatalogObject.РеализацияТоваровУслугПрисоединенныеФайлы"], "ПередЗаписью"),
                ("ПодпискаUniversal", [], "ПередЗаписью"),
            ],
        )
        reader = IndexReader(str(db))
        try:
            rows = reader.get_event_subscriptions("РеализацияТоваровУслуг")
            by_name = {r["name"]: r for r in rows}
            assert "ПодпискаОмоним" not in by_name, rows
            assert by_name["ПодпискаТочная"]["scope"] == "exact"
            assert by_name["ПодпискаUniversal"]["scope"] == "universal"
            assert [r["scope"] for r in rows] == ["exact", "universal"]  # exact-первыми
        finally:
            reader.close()

    def test_get_event_subscriptions_partial_fallback_when_no_exact(self, tmp_path, monkeypatch):
        """Фрагмент имени (точных совпадений нет) → substring-фолбэк по именной части."""
        db = _index_with_subscriptions(
            tmp_path,
            monkeypatch,
            [
                ("ПодпискаОмоним", ["CatalogObject.РеализацияТоваровУслугПрисоединенныеФайлы"], "ПередЗаписью"),
                ("ПодпискаДругая", ["DocumentObject.ЗаказКлиента"], "ПередЗаписью"),
            ],
        )
        reader = IndexReader(str(db))
        try:
            rows = reader.get_event_subscriptions("Реализация")
            assert {r["name"] for r in rows} == {"ПодпискаОмоним"}, rows
            assert rows[0]["scope"] == "partial"
        finally:
            reader.close()

    def test_get_event_subscriptions_event_filter_does_not_resurrect_homonym(self, tmp_path, monkeypatch):
        """exact/partial решается по ПОЛНОМУ набору, event_filter — ПОСЛЕ.

        У точного источника события 'ПередЗаписью' НЕТ, у длинного омонима — ЕСТЬ.
        Если фильтровать по событию ДО классификации, exact-набор опустеет, включится
        partial-фолбэк и вернётся чужой объект. Правильный ответ — пусто (у нашего
        объекта нет подписок на это событие)."""
        db = _index_with_subscriptions(
            tmp_path,
            monkeypatch,
            [
                ("ПодпискаТочная", ["DocumentObject.РеализацияТоваровУслуг"], "ПриЗаписи"),
                ("ПодпискаОмоним", ["CatalogObject.РеализацияТоваровУслугПрисоединенныеФайлы"], "ПередЗаписью"),
            ],
        )
        reader = IndexReader(str(db))
        try:
            rows = reader.get_event_subscriptions("РеализацияТоваровУслуг", event_filter=["ПередЗаписью"])
            assert rows == [], rows  # НЕ ПодпискаОмоним
            rows2 = reader.get_event_subscriptions("РеализацияТоваровУслуг", event_filter=["ПриЗаписи"])
            assert [r["name"] for r in rows2] == ["ПодпискаТочная"]
        finally:
            reader.close()

    def test_get_event_subscriptions_object_ref_is_category_aware(self, tmp_path, monkeypatch):
        """Типизированный вход (canonical ref) различает Document.X и Catalog.X-омонимы;
        голое имя — category-blind (обратная совместимость)."""
        db = _index_with_subscriptions(
            tmp_path,
            monkeypatch,
            [
                ("ПодпискаДок", ["DocumentObject.Дубль"], "ПередЗаписью"),
                ("ПодпискаСпр", ["CatalogObject.Дубль"], "ПередЗаписью"),
            ],
        )
        reader = IndexReader(str(db))
        try:
            typed = reader.get_event_subscriptions("Дубль", object_ref="Document.Дубль")
            assert [r["name"] for r in typed] == ["ПодпискаДок"], typed
            bare = reader.get_event_subscriptions("Дубль")
            assert {r["name"] for r in bare} == {"ПодпискаДок", "ПодпискаСпр"}  # category-blind
        finally:
            reader.close()

    def test_get_functional_options_exact_members_and_collision(self, tmp_path, monkeypatch):
        db_path = _minimal_index_with_meta(tmp_path, monkeypatch)
        self._seed(db_path)
        reader = IndexReader(str(db_path))
        try:
            # Default include_members=True → object-level + attribute-scoped, NOT collision.
            fos = reader.get_functional_options_exact("Document.ЗаказПоставщику")
            assert {f["name"] for f in fos} == {"ФО_Заказ", "ФО_Член"}
            # include_members=False → object-level only.
            fos_obj = reader.get_functional_options_exact("Document.ЗаказПоставщику", include_members=False)
            assert {f["name"] for f in fos_obj} == {"ФО_Заказ"}
        finally:
            reader.close()

    def test_get_functional_options_exact_empty_vs_no_match(self, tmp_path, monkeypatch):
        db_path = _minimal_index_with_meta(tmp_path, monkeypatch)
        reader = IndexReader(str(db_path))
        try:
            assert reader.get_functional_options_exact("Document.X") is None
        finally:
            reader.close()
        self._seed(db_path)
        reader = IndexReader(str(db_path))
        try:
            assert reader.get_functional_options_exact("Document.НетТакого") == []
        finally:
            reader.close()


def test_substr_readers_cyrillic_case_insensitive(tmp_path, monkeypatch):
    db_path = _minimal_index_with_meta(tmp_path, monkeypatch)
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO subsystem_content (subsystem_name, subsystem_synonym, object_ref, file) VALUES (?,?,?,?)",
        ("Продажи", "", "Catalog.Контрагенты", "s.xml"),
    )
    conn.execute(
        "INSERT INTO http_services (name, root_url, templates_json, file) VALUES (?,?,?,?)",
        ("ОбменДанными", "/api", "[]", "h.xml"),
    )
    conn.execute(
        "INSERT INTO web_services (name, namespace, operations_json, file) VALUES (?,?,?,?)",
        ("ВебСервисОбмена", "ns", "[]", "w.xml"),
    )
    conn.execute(
        "INSERT INTO xdto_packages (name, namespace, types_json, file) VALUES (?,?,?,?)",
        ("ПакетОбмена", "ns", "[]", "x.xml"),
    )
    conn.commit()
    conn.close()
    reader = IndexReader(str(db_path))
    try:
        # нижний регистр кириллицы → должен найти (py_lower); раньше COLLATE NOCASE миссил
        assert reader.get_subsystems_for_object("контрагенты")  # object_ref Catalog.Контрагенты
        assert reader.get_http_services("обмен")  # ОбменДанными
        assert reader.get_web_services("обмен")  # ВебСервисОбмена
        assert reader.get_xdto_packages("обмен")  # ПакетОбмена
    finally:
        reader.close()


# ── v1.33.0: билдер смотрит на исходник через маску ─────────────────────────

from rlm_tools_bsl.bsl_index import _extract_movements, _parse_procedures_from_lines  # noqa: E402


def _names(src: str):
    return [p["name"] for p in _parse_procedures_from_lines(src.split("\n"))]


def test_bsp_header_example_does_not_create_ghost_and_does_not_eat_real_method():
    src = (
        "// Модуль содержит служебные процедуры.\n"
        "//\n"
        "// Пример использования:\n"
        "//   Процедура ОпределитьНастройки(Настройки) Экспорт\n"
        '//       Настройки.Вставить("Ключ", Значение);\n'
        "//   КонецПроцедуры\n"
        "\n"
        "Процедура НастроитьВарианты(Настройки) Экспорт\n"
        '    Настройки.Вставить("А", 1);\n'
        "КонецПроцедуры\n"
    )
    assert _names(src) == ["НастроитьВарианты"]


def test_module_level_string_literal_does_not_create_ghost():
    src = (
        "Перем Шаблон;\n"
        'Шаблон = "Процедура Призрак(А) Экспорт";\n'
        "\n"
        "Процедура РеальныйМетод(Б) Экспорт\n"
        "    Возврат;\n"
        "КонецПроцедуры\n"
    )
    assert _names(src) == ["РеальныйМетод"]


def test_query_continuation_line_does_not_create_ghost():
    src = (
        "Перем ТекстЗапроса;\n"
        'ТекстЗапроса = "ВЫБРАТЬ *\n'
        "    |Процедура Призрак(Б) Экспорт\n"
        '    |ИЗ Справочник.Х";\n'
        "\n"
        "Процедура РеальныйМетод(В) Экспорт\n"
        "КонецПроцедуры\n"
    )
    assert _names(src) == ["РеальныйМетод"]


def test_string_default_param_is_stored_verbatim_not_masked():
    """params берётся из ОРИГИНАЛА по смещениям — маска не должна съесть дефолт."""
    src = 'Процедура СоЗначением(Знач Адрес = "http://x/y", Знач Разделитель = ", ") Экспорт\nКонецПроцедуры\n'
    (proc,) = _parse_procedures_from_lines(src.split("\n"))
    assert proc["params"] == 'Знач Адрес = "http://x/y", Знач Разделитель = ", "'


def test_async_single_line_function_still_indexed():
    src = "Асинх Функция ЗагрузитьДанные(Ссылка) Экспорт\n    Возврат 1;\nКонецФункции\n"
    assert _names(src) == ["ЗагрузитьДанные"]


def test_async_multiline_function_now_indexed():
    src = "Асинх Функция Длинная(Ссылка,\n    Второй) Экспорт\n    Возврат 1;\nКонецФункции\n"
    (proc,) = _parse_procedures_from_lines(src.split("\n"))
    assert proc["name"] == "Длинная"
    assert proc["params"] == "Ссылка,     Второй"
    assert proc["is_export"] is True


def test_commented_end_procedure_still_does_not_shorten_method():
    src = "Процедура Настоящая()\n    // КонецПроцедуры\n    А = 1;\nКонецПроцедуры\n"
    (proc,) = _parse_procedures_from_lines(src.split("\n"))
    assert proc["end_line"] == 4


# ОБЩАЯ ФАБРИКА — BslFileInfo это dataclass с СЕМЬЮ обязательными полями
# (format_detector.py): relative_path, category, object_name, module_type,
# form_name, command_name, is_form_module. Вызов с тремя полями даёт TypeError
# ДО входа в проверяемый код, то есть тест падает, ничего не проверив.
def _doc_info(module_type: str, object_name: str = "Д", rel: str = "m.bsl"):
    from rlm_tools_bsl.format_detector import BslFileInfo

    return BslFileInfo(
        relative_path=rel,
        category="Documents",
        object_name=object_name,
        module_type=module_type,
        form_name=None,
        command_name=None,
        is_form_module=False,
    )


def test_commented_movement_is_not_extracted():
    info = _doc_info("ObjectModule", rel="x.bsl")
    content = (
        "Процедура ОбработкаПроведения(Отказ, Режим)\n"
        "    // Движения.Призрак.Записать();\n"
        "    Движения.Настоящий.Записывать = Истина;\n"
        "КонецПроцедуры\n"
    )
    regs = {r[0] for r in _extract_movements(content, info, "x.bsl")}
    assert regs == {"Настоящий"}


def test_commented_erp_mechanism_is_not_extracted():
    """Ветка erp_mechanism читает имя ИЗ литерала, поэтому идёт по маске-только-комментарии."""
    info = _doc_info("ManagerModule")
    content = (
        'МеханизмыДокумента.Добавить("Живой");\n'
        '//МеханизмыДокумента.Добавить("Мертвый");\n'
        '// МеханизмыДокумента.Добавить("МертвыйСПробелом");\n'
    )
    rows = _extract_movements(content, info, "m.bsl")
    regs = {r[0] for r in rows if r[1] == "erp_mechanism"}
    assert regs == {"Живой"}


def test_commented_adapted_register_is_not_extracted():
    """Та же ветка для adapted: ИмяРегистра = "X" внутри закомментированного блока."""
    info = _doc_info("ManagerModule")
    content = (
        "Функция АдаптированныйТекстЗапросаДвиженийПоРегистру(П)\n"
        '    ИмяРегистра = "Живой";\n'
        '    // ИмяРегистра = "Мертвый";\n'
        "КонецФункции\n"
    )
    rows = _extract_movements(content, info, "m.bsl")
    regs = {r[0] for r in rows if r[1] == "adapted"}
    assert regs == {"Живой"}


# ── v1.33.0: движения из ManagerModule (source='manager_code') ──────────────

from rlm_tools_bsl.bsl_index import _known_register_names  # noqa: E402


def _mgr(content):
    # _doc_info — фабрика выше: BslFileInfo требует все семь полей
    return _extract_movements(content, _doc_info("ManagerModule"), "m.bsl")


def test_manager_create_recordset_is_extracted():
    rows = _mgr(
        "Процедура Провести() Экспорт\n"
        "    Наб = РегистрыСведений.МойРегистр.СоздатьНаборЗаписей();\n"
        "    Наб.Записать();\n"
        "КонецПроцедуры\n"
    )
    assert ("МойРегистр", "manager_code", "m.bsl") in rows


def test_manager_create_recordset_all_register_kinds():
    for coll, reg in (
        ("РегистрыНакопления", "Р1"),
        ("РегистрыБухгалтерии", "Р2"),
        ("РегистрыРасчета", "Р3"),
        ("РегистрыСведений", "Р4"),
    ):
        rows = _mgr(f"Н = {coll}.{reg}.СоздатьНаборЗаписей();\n")
        assert (reg, "manager_code", "m.bsl") in rows, coll


def test_manager_commented_create_recordset_is_not_extracted():
    rows = _mgr("// Наб = РегистрыСведений.Призрак.СоздатьНаборЗаписей();\n")
    assert not [r for r in rows if r[0] == "Призрак"]


def test_manager_field_access_is_not_a_register():
    """Движения.Количество в менеджере — поле набора, не регистр: source manager_code не выдаётся."""
    rows = _mgr("Для Каждого Стр Из Движения Цикл\n    Стр.Количество = Движения.Количество;\nКонецЦикла;\n")
    assert not [r for r in rows if r[1] == "manager_code"]


def test_known_register_names_scans_four_catalogs(tmp_path):
    for cat, name in (
        ("InformationRegisters", "РС1"),
        ("AccumulationRegisters", "РН1"),
        ("AccountingRegisters", "РБ1"),
        ("CalculationRegisters", "РР1"),
    ):
        (tmp_path / cat / name).mkdir(parents=True)
    assert _known_register_names(tmp_path) == {"рс1", "рн1", "рб1", "рр1"}


def test_manager_rows_filtered_by_register_catalog(tmp_path, monkeypatch):
    """Строка manager_code с именем, которого нет среди регистров, до таблицы не доходит."""
    (tmp_path / "InformationRegisters" / "Настоящий").mkdir(parents=True)
    (tmp_path / "Documents" / "Д" / "Ext").mkdir(parents=True)
    (tmp_path / "Documents" / "Д" / "Ext" / "ManagerModule.bsl").write_text(
        "Н = РегистрыСведений.Настоящий.СоздатьНаборЗаписей();\n"
        "М = РегистрыСведений.НетТакого.СоздатьНаборЗаписей();\n",
        encoding="utf-8",
    )
    (tmp_path / "Configuration.xml").write_text(
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"><Configuration>'
        "<Properties><Name>Т</Name></Properties></Configuration></MetaDataObject>",
        encoding="utf-8",
    )
    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / ".idx"))
    db_path = IndexBuilder().build(str(tmp_path), build_calls=False, build_metadata=True)
    con = sqlite3.connect(db_path)
    regs = {r[0] for r in con.execute("SELECT register_name FROM register_movements WHERE source='manager_code'")}
    con.close()
    assert regs == {"Настоящий"}


def test_manager_recordset_requires_left_boundary():
    """Имя коллекции регистров обязано начинаться на границе идентификатора.

    Без левой границы `МоиРегистрыСведений.Мой.СоздатьНаборЗаписей()` и
    `Обертка.РегистрыСведений.Мой...` давали ложную пару документ-регистр:
    сверка с каталогом регистров это ловит не всегда — имя могло совпасть
    с настоящим регистром.
    """
    assert not [r for r in _mgr("Н = МоиРегистрыСведений.Мой.СоздатьНаборЗаписей();\n") if r[1] == "manager_code"]
    assert not [r for r in _mgr("Н = Обертка.РегистрыСведений.Мой.СоздатьНаборЗаписей();\n") if r[1] == "manager_code"]
    # регресс-гард: законные формы по-прежнему находятся
    assert ("Мой", "manager_code", "m.bsl") in _mgr("    Н = РегистрыСведений.Мой.СоздатьНаборЗаписей();\n")
    assert ("Foo", "manager_code", "m.bsl") in _mgr("X = InformationRegisters.Foo.CreateRecordSet();\n")
