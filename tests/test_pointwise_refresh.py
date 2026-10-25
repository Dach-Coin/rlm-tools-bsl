"""Tests for pointwise incremental metadata refresh (v1.9.3).

Covers resolver, dispatcher, group A (Catalogs/Documents/registers/CoA),
group B (EventSubscriptions/ScheduledJobs/XDTOPackages), feature gates,
and equivalence with full rebuild.
"""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from rlm_tools_bsl.bsl_index import (
    IndexBuilder,
    _POINTWISE_ELIGIBLE_GROUP_A,
    _POINTWISE_ELIGIBLE_GROUP_B,
    _POINTWISE_MAX_OBJECTS,
    _PointwiseTelemetry,
    _build_changed_objects,
    _can_use_pointwise,
    _category_object_count,
    _get_optional_tables_state,
    _resolve_object_from_path,
    get_index_db_path,
)


# ---------------------------------------------------------------------------
# Helpers (locally — no shared fixtures needed beyond tmp_path/monkeypatch)
# ---------------------------------------------------------------------------


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
    _git(root, "add", ".")
    _git(root, "commit", "-m", "initial")


CATALOG_MDO_TEMPLATE = """\
<?xml version="1.0" encoding="UTF-8"?>
<mdclass:Catalog xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass">
  <name>{name}</name>
  <synonym>
    <key>ru</key>
    <value>{synonym}</value>
  </synonym>
  <attributes uuid="11111111-1111-1111-1111-111111111111">
    <name>{attr_name}</name>
    <synonym>
      <key>ru</key>
      <value>{attr_synonym}</value>
    </synonym>
    <type>
      <types>String</types>
    </type>
  </attributes>
</mdclass:Catalog>
"""


def _make_catalog(
    base: Path, name: str, *, attr_name: str = "MyAttr", synonym: str = "Тест", attr_synonym: str = "Реквизит"
) -> Path:
    """Create EDT-style catalog: Catalogs/<name>/<name>.mdo."""
    obj_dir = base / "Catalogs" / name
    obj_dir.mkdir(parents=True, exist_ok=True)
    mdo = obj_dir / f"{name}.mdo"
    mdo.write_text(
        CATALOG_MDO_TEMPLATE.format(name=name, synonym=synonym, attr_name=attr_name, attr_synonym=attr_synonym),
        encoding="utf-8-sig",
    )
    return mdo


EVENT_SUBSCRIPTION_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<mdclass:EventSubscription xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass">
  <name>{internal_name}</name>
  <synonym>
    <key>ru</key>
    <value>{synonym}</value>
  </synonym>
  <event>BeforeWrite</event>
  <handler>CommonModule.{module}.{procedure}</handler>
  <source>CatalogRef.Контрагенты</source>
</mdclass:EventSubscription>
"""


def _make_event_subscription(
    base: Path,
    name: str,
    *,
    module: str = "Модуль",
    procedure: str = "Обработчик",
    synonym: str | None = None,
    internal_name: str | None = None,
) -> Path:
    """Create EDT EventSubscription. *internal_name* — value of <name> tag inside XML;
    defaults to folder *name* (typical case). Different value tests row-by-folder mismatch."""
    obj_dir = base / "EventSubscriptions" / name
    obj_dir.mkdir(parents=True, exist_ok=True)
    mdo = obj_dir / f"{name}.mdo"
    mdo.write_text(
        EVENT_SUBSCRIPTION_XML.format(
            internal_name=internal_name or name,
            synonym=synonym or name,
            module=module,
            procedure=procedure,
        ),
        encoding="utf-8-sig",
    )
    return mdo


CHART_OF_ACCOUNTS_MDO = """\
<?xml version="1.0" encoding="UTF-8"?>
<mdclass:ChartOfAccounts xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass">
  <name>{name}</name>
  <synonym>
    <key>ru</key>
    <value>{synonym}</value>
  </synonym>
  <attributes uuid="22222222-2222-2222-2222-222222222222">
    <name>{attr_name}</name>
    <synonym>
      <key>ru</key>
      <value>{attr_synonym}</value>
    </synonym>
    <type>
      <types>CatalogRef.Контрагенты</types>
    </type>
  </attributes>
</mdclass:ChartOfAccounts>
"""


def _make_chart_of_accounts(
    base: Path,
    name: str,
    *,
    attr_name: str = "Связь",
    attr_synonym: str = "Связь",
    synonym: str = "Тестовый план счетов",
) -> Path:
    obj_dir = base / "ChartsOfAccounts" / name
    obj_dir.mkdir(parents=True, exist_ok=True)
    mdo = obj_dir / f"{name}.mdo"
    mdo.write_text(
        CHART_OF_ACCOUNTS_MDO.format(name=name, synonym=synonym, attr_name=attr_name, attr_synonym=attr_synonym),
        encoding="utf-8-sig",
    )
    return mdo


def _build_basic_project(base: Path) -> None:
    """Базовый набор: один CommonModule + один Catalog."""
    cm_dir = base / "CommonModules" / "Модуль" / "Ext"
    cm_dir.mkdir(parents=True)
    (cm_dir / "Module.bsl").write_text("Процедура Тест() Экспорт\nКонецПроцедуры\n", encoding="utf-8-sig")
    _make_catalog(base, "Контрагенты")
    _make_catalog(base, "Номенклатура")


@pytest.fixture
def git_metadata_project(tmp_path, monkeypatch):
    """Git project with metadata + synonyms enabled."""
    root = tmp_path
    base = root / "src"
    base.mkdir(parents=True)
    _build_basic_project(base)
    _git_init(root)
    monkeypatch.setenv("RLM_INDEX_DIR", str(base / ".index"))
    builder = IndexBuilder()
    builder.build(str(base), build_calls=True, build_metadata=True, build_synonyms=True)
    return base


# =====================================================================
# Tier 2: resolver unit tests
# =====================================================================


class TestResolveObjectFromPath:
    def test_edt_layout(self, tmp_path):
        cat, name = _resolve_object_from_path("Catalogs/Контрагенты/Контрагенты.mdo", str(tmp_path))
        assert cat == "Catalogs"
        assert name == "Контрагенты"

    def test_cf_sibling_only(self, tmp_path):
        cat, name = _resolve_object_from_path("Catalogs/Контрагенты.xml", str(tmp_path))
        assert cat == "Catalogs"
        assert name == "Контрагенты"

    def test_cf_ext_layout(self, tmp_path):
        cat, name = _resolve_object_from_path("Catalogs/Контрагенты/Ext/Module.bsl", str(tmp_path))
        assert cat == "Catalogs"
        assert name == "Контрагенты"

    def test_root_file_no_object(self, tmp_path):
        # Configuration.xml в корне — категория None
        cat, name = _resolve_object_from_path("Configuration.xml", str(tmp_path))
        assert cat is None
        assert name is None

    def test_path_outside_categories(self, tmp_path):
        cat, name = _resolve_object_from_path("README.md", str(tmp_path))
        assert cat is None
        assert name is None

    def test_event_subscriptions_cf_sibling(self, tmp_path):
        """Round 5 (DO3 e2e): EventSubscriptions нет в METADATA_CATEGORIES, но есть
        в _CATEGORY_RU — resolver должен это поймать через свой fallback."""
        cat, name = _resolve_object_from_path("EventSubscriptions/АвтоОбновление.xml", str(tmp_path))
        assert cat == "EventSubscriptions"
        assert name == "АвтоОбновление"

    def test_event_subscriptions_edt(self, tmp_path):
        cat, name = _resolve_object_from_path("EventSubscriptions/АвтоОбновление/АвтоОбновление.mdo", str(tmp_path))
        assert cat == "EventSubscriptions"
        assert name == "АвтоОбновление"

    def test_scheduled_jobs_cf_sibling(self, tmp_path):
        cat, name = _resolve_object_from_path("ScheduledJobs/ОчисткаКорзины.xml", str(tmp_path))
        assert cat == "ScheduledJobs"
        assert name == "ОчисткаКорзины"

    def test_defined_types_cf_sibling(self, tmp_path):
        cat, name = _resolve_object_from_path("DefinedTypes/АвторДействия.xml", str(tmp_path))
        assert cat == "DefinedTypes"
        assert name == "АвторДействия"


class TestBuildChangedObjects:
    def test_groups_by_category(self, tmp_path):
        paths = {
            "Catalogs/A/A.mdo",
            "Catalogs/B/B.mdo",
            "Documents/D/D.mdo",
        }
        changed, unresolved = _build_changed_objects(paths, str(tmp_path))
        assert changed == {"Catalogs": {"A", "B"}, "Documents": {"D"}}
        assert unresolved == set()

    def test_cf_sibling_resolved(self, tmp_path):
        paths = {"Catalogs/Контрагенты.xml"}
        changed, unresolved = _build_changed_objects(paths, str(tmp_path))
        assert changed == {"Catalogs": {"Контрагенты"}}
        assert unresolved == set()


# =====================================================================
# Tier 2: _can_use_pointwise + threshold logic
# =====================================================================


class TestCanUsePointwise:
    def test_not_in_whitelist(self, tmp_path):
        conn = sqlite3.connect(":memory:")
        tel = _PointwiseTelemetry()
        # Reports не в whitelist
        assert _can_use_pointwise("Reports", {"Test"}, set(), conn, tel) is False
        assert tel.fallback_categories == 1
        assert tel.reasons.get("not_eligible") == 1

    def test_in_unresolved(self, tmp_path):
        conn = sqlite3.connect(":memory:")
        tel = _PointwiseTelemetry()
        assert _can_use_pointwise("Catalogs", {"Test"}, {"Catalogs"}, conn, tel) is False
        assert tel.reasons.get("unresolved_root") == 1

    def test_no_objects(self):
        conn = sqlite3.connect(":memory:")
        tel = _PointwiseTelemetry()
        assert _can_use_pointwise("Catalogs", set(), set(), conn, tel) is False
        assert tel.reasons.get("no_objects") == 1

    def test_threshold_absolute(self):
        conn = sqlite3.connect(":memory:")
        tel = _PointwiseTelemetry()
        # _POINTWISE_MAX_OBJECTS = 50
        objects = {f"obj{i}" for i in range(_POINTWISE_MAX_OBJECTS)}
        assert _can_use_pointwise("Catalogs", objects, set(), conn, tel) is False
        assert tel.reasons.get("threshold_absolute") == 1

    def test_below_threshold_passes(self):
        conn = sqlite3.connect(":memory:")
        tel = _PointwiseTelemetry()
        # Малый набор + пустая БД (total=0) → relative threshold off, абсолютный не сработал
        assert _can_use_pointwise("Catalogs", {"A", "B", "C"}, set(), conn, tel) is True


class TestCategoryObjectCount:
    def test_missing_table_returns_zero(self):
        conn = sqlite3.connect(":memory:")
        # Таблиц нет — возвращает 0, не падает
        assert _category_object_count(conn, "Catalogs") == 0
        assert _category_object_count(conn, "EventSubscriptions") == 0
        assert _category_object_count(conn, "ChartsOfAccounts") == 0


class TestOptionalTablesState:
    def test_legacy_no_meta_table(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE index_meta (key TEXT PRIMARY KEY, value TEXT)")
        opt = _get_optional_tables_state(conn)
        # Default has_synonyms=True (no row), tables не существуют
        assert opt["has_synonyms"] is True
        assert opt["has_synonyms_table"] is False
        assert opt["has_metadata_references_table"] is False

    def test_full_state(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE index_meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO index_meta VALUES ('has_synonyms', '1')")
        conn.execute("CREATE TABLE object_synonyms (id INTEGER)")
        conn.execute("CREATE TABLE metadata_references (id INTEGER)")
        opt = _get_optional_tables_state(conn)
        assert opt["has_synonyms"] is True
        assert opt["has_synonyms_table"] is True
        assert opt["has_metadata_references_table"] is True


# =====================================================================
# Tier 3: E2E git fast path with pointwise dispatcher
# =====================================================================


class TestPointwiseDispatcherE2E:
    def test_modify_one_catalog_pointwise(self, git_metadata_project):
        """Изменение одного атрибута в одном справочнике → pointwise refresh."""
        base = git_metadata_project
        root = base.parent
        db_path = get_index_db_path(str(base))

        # Изменяем один атрибут в Контрагентах
        _make_catalog(base, "Контрагенты", attr_name="ИНН", attr_synonym="ИНН")
        _git(root, "add", ".")
        _git(root, "commit", "-m", "change one catalog")

        result = IndexBuilder().update(str(base))
        assert result["git_fast_path"] is True

        # Контрагенты обновлён, Номенклатура осталась
        conn = sqlite3.connect(str(db_path))
        try:
            kontrahenty_attr = conn.execute(
                "SELECT attr_name FROM object_attributes WHERE category='Catalogs' AND object_name=?",
                ("Контрагенты",),
            ).fetchall()
            nomenkl_attr = conn.execute(
                "SELECT attr_name FROM object_attributes WHERE category='Catalogs' AND object_name=?",
                ("Номенклатура",),
            ).fetchall()
        finally:
            conn.close()

        assert any(row[0] == "ИНН" for row in kontrahenty_attr)
        assert all(row[0] != "MyAttr" for row in kontrahenty_attr)
        # Номенклатура не пострадала
        assert any(row[0] == "MyAttr" for row in nomenkl_attr)

    def test_delete_catalog_clears_attributes(self, git_metadata_project):
        """Удаление справочника → attributes/synonyms исчезают."""
        base = git_metadata_project
        root = base.parent
        db_path = get_index_db_path(str(base))

        # Удаляем Номенклатуру целиком
        shutil.rmtree(base / "Catalogs" / "Номенклатура")
        _git(root, "add", "-A")
        _git(root, "commit", "-m", "delete Номенклатура")

        result = IndexBuilder().update(str(base))
        assert result["git_fast_path"] is True

        conn = sqlite3.connect(str(db_path))
        try:
            rows = conn.execute(
                "SELECT COUNT(*) FROM object_attributes WHERE category='Catalogs' AND object_name=?",
                ("Номенклатура",),
            ).fetchone()
        finally:
            conn.close()
        assert rows[0] == 0

    def test_add_catalog_pointwise(self, git_metadata_project):
        """Добавление нового справочника → pointwise INSERT."""
        base = git_metadata_project
        root = base.parent
        db_path = get_index_db_path(str(base))

        _make_catalog(base, "НовыйСправочник", attr_name="Поле", attr_synonym="Поле")
        _git(root, "add", ".")
        _git(root, "commit", "-m", "add new catalog")

        IndexBuilder().update(str(base))

        conn = sqlite3.connect(str(db_path))
        try:
            rows = conn.execute(
                "SELECT object_name, attr_name FROM object_attributes WHERE category='Catalogs' AND object_name=?",
                ("НовыйСправочник",),
            ).fetchall()
        finally:
            conn.close()
        assert len(rows) == 1
        assert rows[0][1] == "Поле"

    def test_event_subscription_pointwise(self, tmp_path, monkeypatch):
        """Group B: добавление EventSubscription → pointwise через _refresh_global_object."""
        root = tmp_path
        base = root / "src"
        base.mkdir(parents=True)
        _build_basic_project(base)
        _make_event_subscription(base, "Тест1")
        _git_init(root)
        monkeypatch.setenv("RLM_INDEX_DIR", str(base / ".index"))
        IndexBuilder().build(str(base), build_calls=True, build_metadata=True, build_synonyms=True)

        # Изменяем существующую подписку
        _make_event_subscription(base, "Тест1", module="ДругойМодуль")
        _git(root, "add", ".")
        _git(root, "commit", "-m", "change event subscription")

        IndexBuilder().update(str(base))
        db_path = get_index_db_path(str(base))
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT handler_module FROM event_subscriptions WHERE name=?",
                ("Тест1",),
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row[0] == "ДругойМодуль"


class TestNonWhitelistFallback:
    """Reports / Constants / Subsystems → fallback (категория не в whitelist)."""

    def test_constant_change_falls_back(self, git_metadata_project, tmp_path):
        """Изменение Constants → bulk fallback, но без падения."""
        base = git_metadata_project
        root = base.parent

        const_dir = base / "Constants"
        const_dir.mkdir(parents=True)
        const_xml = const_dir / "Параметр.xml"
        const_xml.write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            "<MetaDataObject>\n"
            "  <Constant>\n"
            "    <Properties><Name>Параметр</Name></Properties>\n"
            "  </Constant>\n"
            "</MetaDataObject>\n",
            encoding="utf-8-sig",
        )
        _git(root, "add", ".")
        _git(root, "commit", "-m", "add Constant")

        result = IndexBuilder().update(str(base))
        assert result["git_fast_path"] is True


class TestEquivalenceWithFullBuild:
    """Tier 5 — критический инвариант: pointwise update эквивалентен fresh full build."""

    def test_attributes_match_after_modify(self, git_metadata_project, tmp_path, monkeypatch):
        base = git_metadata_project
        root = base.parent
        db_path = get_index_db_path(str(base))

        _make_catalog(base, "Контрагенты", attr_name="ИНН", attr_synonym="ИНН")
        _git(root, "add", ".")
        _git(root, "commit", "-m", "change attr")

        IndexBuilder().update(str(base))

        # Параллельно — fresh full build на копии проекта
        twin = tmp_path / "twin"
        # Копируем без .index (чтобы build строил с нуля)
        shutil.copytree(base, twin / "src", ignore=shutil.ignore_patterns(".index"))
        monkeypatch.setenv("RLM_INDEX_DIR", str(twin / "src" / ".index"))
        IndexBuilder().build(str(twin / "src"), build_calls=True, build_metadata=True, build_synonyms=True)
        twin_db = get_index_db_path(str(twin / "src"))

        def _attrs(p):
            conn = sqlite3.connect(str(p))
            try:
                rows = conn.execute(
                    "SELECT object_name, category, attr_name, attr_synonym, attr_type, "
                    "attr_kind, ts_name, source_file FROM object_attributes "
                    "ORDER BY object_name, attr_name"
                ).fetchall()
            finally:
                conn.close()
            return rows

        assert _attrs(db_path) == _attrs(twin_db)

    def test_synonyms_match_after_modify(self, git_metadata_project, tmp_path, monkeypatch):
        base = git_metadata_project
        root = base.parent
        db_path = get_index_db_path(str(base))

        _make_catalog(
            base,
            "Контрагенты",
            attr_name="MyAttr",
            attr_synonym="Реквизит",
            synonym="ОбновлённыйСиноним",
        )
        _git(root, "add", ".")
        _git(root, "commit", "-m", "change synonym")

        IndexBuilder().update(str(base))

        twin = tmp_path / "twin2"
        shutil.copytree(base, twin / "src", ignore=shutil.ignore_patterns(".index"))
        monkeypatch.setenv("RLM_INDEX_DIR", str(twin / "src" / ".index"))
        IndexBuilder().build(str(twin / "src"), build_calls=True, build_metadata=True, build_synonyms=True)
        twin_db = get_index_db_path(str(twin / "src"))

        def _syns(p):
            conn = sqlite3.connect(str(p))
            try:
                rows = conn.execute(
                    "SELECT object_name, category, synonym, file FROM object_synonyms "
                    "ORDER BY category, object_name, synonym"
                ).fetchall()
            finally:
                conn.close()
            return rows

        assert _syns(db_path) == _syns(twin_db)


# =====================================================================
# Tier 4: regression — synonym rescan must NOT fire on pure .command delta
# =====================================================================


class TestCommandTriggerNoSynonymRescan:
    def test_pure_command_delta_skips_synonym_rescan(self, git_metadata_project):
        """Чистая .command-дельта НЕ должна попасть в synonym_changed_categories.

        Защищает от perf-регрессии: при изменении только Commands/<X>/<X>.command
        category-wide DELETE FROM object_synonyms WHERE category=Catalogs не выполняется.
        """
        base = git_metadata_project
        root = base.parent
        db_path = get_index_db_path(str(base))

        # Замеряем текущее количество синонимов в Catalogs
        conn = sqlite3.connect(str(db_path))
        before_synonyms = conn.execute(
            "SELECT object_name, synonym FROM object_synonyms WHERE category='Catalogs' ORDER BY object_name, synonym"
        ).fetchall()
        conn.close()

        # Создаём только .command файл (без .xml/.mdo рядом)
        cmd_dir = base / "CommonCommands" / "ТестКоманда"
        cmd_dir.mkdir(parents=True)
        (cmd_dir / "ТестКоманда.command").write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n<Command><Name>ТестКоманда</Name></Command>\n',
            encoding="utf-8-sig",
        )
        _git(root, "add", ".")
        _git(root, "commit", "-m", "add command file")

        result = IndexBuilder().update(str(base))
        assert result["git_fast_path"] is True

        # Синонимы Catalogs должны остаться нетронутыми
        conn = sqlite3.connect(str(db_path))
        after_synonyms = conn.execute(
            "SELECT object_name, synonym FROM object_synonyms WHERE category='Catalogs' ORDER BY object_name, synonym"
        ).fetchall()
        conn.close()

        assert before_synonyms == after_synonyms


class TestPointwiseEligibility:
    def test_whitelist_groups_documented(self):
        # Sanity: убеждаемся, что constants exposes ожидаемый whitelist.
        assert "Catalogs" in _POINTWISE_ELIGIBLE_GROUP_A
        assert "Documents" in _POINTWISE_ELIGIBLE_GROUP_A
        assert "ChartsOfAccounts" in _POINTWISE_ELIGIBLE_GROUP_A
        # NB: ChartsOfCharacteristicTypes intentionally NOT in whitelist
        assert "ChartsOfCharacteristicTypes" not in _POINTWISE_ELIGIBLE_GROUP_A
        assert "EventSubscriptions" in _POINTWISE_ELIGIBLE_GROUP_B
        assert "ScheduledJobs" in _POINTWISE_ELIGIBLE_GROUP_B
        assert "XDTOPackages" in _POINTWISE_ELIGIBLE_GROUP_B
        # Reports / Constants / Subsystems / Roles НЕ в whitelist — всегда fallback
        for cat in ("Reports", "Constants", "Subsystems", "Roles", "FunctionalOptions"):
            assert cat not in _POINTWISE_ELIGIBLE_GROUP_A
            assert cat not in _POINTWISE_ELIGIBLE_GROUP_B


# =====================================================================
# Codex review: regression tests for High + 2× Medium fixes
# =====================================================================


class TestGroupBSynonymPointwise:
    """High finding: Group B (ES/SJ/XDTO) входит в _SYNONYM_CATEGORIES, но
    pointwise-ветка раньше не обновляла object_synonyms — оставались stale."""

    def test_event_subscription_synonym_updated(self, tmp_path, monkeypatch):
        root = tmp_path
        base = root / "src"
        base.mkdir(parents=True)
        _build_basic_project(base)
        # 3 ES, чтобы pointwise-threshold (1/3 < 0.5) не уронил dispatcher в bulk
        for name in ("Тест1", "Тест2", "Тест3"):
            _make_event_subscription(base, name, synonym=f"СтарыйСиноним{name}")
        _git_init(root)
        monkeypatch.setenv("RLM_INDEX_DIR", str(base / ".index"))
        IndexBuilder().build(str(base), build_calls=True, build_metadata=True, build_synonyms=True)

        # Меняем синоним только у Тест1 — pointwise-сценарий
        _make_event_subscription(base, "Тест1", synonym="НовыйСиноним")
        _git(root, "add", ".")
        _git(root, "commit", "-m", "change ES synonym")

        IndexBuilder().update(str(base))
        db_path = get_index_db_path(str(base))

        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT synonym FROM object_synonyms WHERE category='EventSubscriptions' AND object_name='Тест1'"
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        # _CATEGORY_RU["EventSubscriptions"] = "Подписка на событие"
        assert "НовыйСиноним" in row[0]
        assert "СтарыйСиноним" not in row[0]


class TestGroupBDeletionInternalNameMismatch:
    """Medium finding: при удалении объекта Group B, у которого parsed name
    отличается от folder name, deletion path должен почистить и stale row."""

    def test_event_subscription_deletion_with_name_mismatch(self, tmp_path, monkeypatch):
        root = tmp_path
        base = root / "src"
        base.mkdir(parents=True)
        _build_basic_project(base)
        # Folder = "FolderName", but <name>InternalName</name> in XML
        _make_event_subscription(base, "FolderName", internal_name="InternalName")
        # Дополнительные ES чтобы избежать relative threshold
        _make_event_subscription(base, "Тест2")
        _make_event_subscription(base, "Тест3")
        _git_init(root)
        monkeypatch.setenv("RLM_INDEX_DIR", str(base / ".index"))
        IndexBuilder().build(str(base), build_calls=True, build_metadata=True, build_synonyms=True)

        db_path = get_index_db_path(str(base))
        conn = sqlite3.connect(str(db_path))
        try:
            # Sanity: row.name = parsed internal name (НЕ folder name)
            stored_name_row = conn.execute(
                "SELECT name, file FROM event_subscriptions WHERE file=? OR file=?",
                (
                    "EventSubscriptions/FolderName/FolderName.mdo",
                    "EventSubscriptions/FolderName.xml",
                ),
            ).fetchone()
        finally:
            conn.close()
        assert stored_name_row is not None
        assert stored_name_row[0] == "InternalName"  # row.name == parsed name

        # Удаляем папку целиком
        shutil.rmtree(base / "EventSubscriptions" / "FolderName")
        _git(root, "add", "-A")
        _git(root, "commit", "-m", "delete FolderName")

        IndexBuilder().update(str(base))

        # Stale row не должна остаться — ни по name=InternalName, ни по file=...
        conn = sqlite3.connect(str(db_path))
        try:
            count_by_name = conn.execute(
                "SELECT COUNT(*) FROM event_subscriptions WHERE name='InternalName'",
            ).fetchone()[0]
            count_by_file = conn.execute(
                "SELECT COUNT(*) FROM event_subscriptions WHERE file LIKE 'EventSubscriptions/FolderName%'",
            ).fetchone()[0]
            count_refs = conn.execute(
                "SELECT COUNT(*) FROM metadata_references "
                "WHERE source_category='EventSubscriptions' "
                "AND (source_object='InternalName' OR source_object='FolderName' "
                "     OR path LIKE 'EventSubscriptions/FolderName%')",
            ).fetchone()[0]
        finally:
            conn.close()

        assert count_by_name == 0
        assert count_by_file == 0
        assert count_refs == 0


class TestGroupBDeletionCFExtLayout:
    """Round 4 (Codex): bulk collector через rglob индексирует CF Ext layout
    (`Category/Foo/Ext/*.xml`). После удаления папки pointwise-deletion должен
    почистить и эти rows — раньше LIKE-prefix не было."""

    def test_event_subscription_cf_ext_deletion(self, tmp_path, monkeypatch):
        root = tmp_path
        base = root / "src"
        base.mkdir(parents=True)
        _build_basic_project(base)

        # CF Ext layout: EventSubscriptions/FolderName/Ext/EventSubscription.xml,
        # внутренний <name>InternalName</name> != folder name.
        cf_dir = base / "EventSubscriptions" / "FolderName" / "Ext"
        cf_dir.mkdir(parents=True)
        (cf_dir / "EventSubscription.xml").write_text(
            EVENT_SUBSCRIPTION_XML.format(
                internal_name="InternalName",
                synonym="Подписка",
                module="Модуль",
                procedure="Обработчик",
            ),
            encoding="utf-8-sig",
        )
        # +2 ES чтобы не сработал relative threshold (1/3 < 0.5)
        _make_event_subscription(base, "Тест2")
        _make_event_subscription(base, "Тест3")
        _git_init(root)
        monkeypatch.setenv("RLM_INDEX_DIR", str(base / ".index"))
        IndexBuilder().build(str(base), build_calls=True, build_metadata=True, build_synonyms=True)

        db_path = get_index_db_path(str(base))
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT name, file FROM event_subscriptions WHERE file LIKE 'EventSubscriptions/FolderName/%'"
            ).fetchone()
        finally:
            conn.close()
        # Sanity: bulk собрал CF Ext layout, row.file именно с Ext/*.xml,
        # row.name = parsed internal name (не folder name)
        assert row is not None
        assert row[0] == "InternalName"
        assert row[1] == "EventSubscriptions/FolderName/Ext/EventSubscription.xml"

        # Удаляем папку целиком
        shutil.rmtree(base / "EventSubscriptions" / "FolderName")
        _git(root, "add", "-A")
        _git(root, "commit", "-m", "delete CF Ext FolderName")

        IndexBuilder().update(str(base))

        conn = sqlite3.connect(str(db_path))
        try:
            count_by_name = conn.execute(
                "SELECT COUNT(*) FROM event_subscriptions WHERE name='InternalName'",
            ).fetchone()[0]
            count_by_file = conn.execute(
                "SELECT COUNT(*) FROM event_subscriptions WHERE file LIKE 'EventSubscriptions/FolderName/%'",
            ).fetchone()[0]
            count_refs = conn.execute(
                "SELECT COUNT(*) FROM metadata_references "
                "WHERE source_category='EventSubscriptions' "
                "AND (source_object IN ('InternalName', 'FolderName') "
                "     OR path LIKE 'EventSubscriptions/FolderName/%')",
            ).fetchone()[0]
        finally:
            conn.close()

        assert count_by_name == 0, "stale row by parsed name remained after CF Ext deletion"
        assert count_by_file == 0, "stale row by CF Ext file path remained"
        assert count_refs == 0, "stale metadata_references for CF Ext path remained"


class TestChartOfAccountsRefsParity:
    """Medium finding: точечный путь и полная сборка эмитят ссылки плана счетов ОДИНАКОВО — иначе Tier 5 fail.

    v1.47.0: прежде здесь стояло «full collector НЕ эмитит parsed.references для CoA (CoA не в
    _ATTR_CATEGORIES); pointwise тоже не должен» — теперь план счетов в `_ATTR_CATEGORIES`, и ОБА пути эмитят
    его ссылки (реквизиты, `based_on`, связующие свойства); паритет — тот же инвариант."""

    def test_coa_pointwise_matches_full_build(self, tmp_path, monkeypatch):
        root = tmp_path
        base = root / "src"
        base.mkdir(parents=True)
        _build_basic_project(base)
        # 3 CoA, чтобы пройти relative threshold (модифицируем 1 → 1/3 < 0.5)
        for n in ("План1", "План2", "План3"):
            _make_chart_of_accounts(base, n)
        _git_init(root)
        monkeypatch.setenv("RLM_INDEX_DIR", str(base / ".index"))
        IndexBuilder().build(str(base), build_calls=True, build_metadata=True, build_synonyms=True)

        # Меняем атрибут одного CoA
        _make_chart_of_accounts(base, "План1", attr_name="ОбновлённыйАтрибут")
        _git(root, "add", ".")
        _git(root, "commit", "-m", "change CoA attr")

        IndexBuilder().update(str(base))
        db_path = get_index_db_path(str(base))

        # Twin: fresh full build (без .index)
        twin = tmp_path / "twin_coa"
        shutil.copytree(base, twin / "src", ignore=shutil.ignore_patterns(".index"))
        monkeypatch.setenv("RLM_INDEX_DIR", str(twin / "src" / ".index"))
        IndexBuilder().build(str(twin / "src"), build_calls=True, build_metadata=True, build_synonyms=True)
        twin_db = get_index_db_path(str(twin / "src"))

        def _coa_refs(p):
            conn = sqlite3.connect(str(p))
            try:
                rows = conn.execute(
                    "SELECT source_object, source_category, ref_object, ref_kind, "
                    "used_in, path FROM metadata_references "
                    "WHERE source_category='ChartsOfAccounts' "
                    "ORDER BY source_object, ref_kind, ref_object, used_in"
                ).fetchall()
            finally:
                conn.close()
            return rows

        assert _coa_refs(db_path) == _coa_refs(twin_db)
