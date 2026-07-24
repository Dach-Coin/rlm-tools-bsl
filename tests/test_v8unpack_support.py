import json
import sqlite3
from argparse import Namespace
from pathlib import Path

import pytest

from rlm_tools_bsl import cache
from rlm_tools_bsl.cli import _cmd_info
from rlm_tools_bsl.bsl_helpers import make_bsl_helpers
from rlm_tools_bsl.bsl_index import BUILDER_VERSION, IndexBuilder, IndexReader
from rlm_tools_bsl.format_detector import SourceFormat, detect_format, parse_bsl_path
from rlm_tools_bsl.helpers import make_helpers
from rlm_tools_bsl.server import _rlm_end, _rlm_start


@pytest.fixture
def v8unpack_source(tmp_path):
    (tmp_path / "Configuration.json").write_text(
        "\ufeff" + json.dumps({"v8unpack": "1.2.9", "name": "Тест"}),
        encoding="utf-8",
    )
    files = {
        "CommonModule/Общий/CommonModule.obj.bsl": "Процедура Вызвать() Экспорт\nКонецПроцедуры",
        "Catalog/Товары/Catalog.obj.bsl": (
            "Процедура ПередЗаписью(Отказ)\n"
            "    Общий.Вызвать();\n"
            "КонецПроцедуры"
        ),
        "Document/Заказ/Document.obj.bsl": (
            "Процедура ОбработкаПроведения(Отказ, Режим)\n"
            "    Движения.Цены.Записать();\n"
            "КонецПроцедуры"
        ),
        "Document/Заказ/Document.mgr.bsl": "Функция Найти()\nКонецФункции",
        "InformationRegister/Цены/InformationRegister.obj.bsl": "Процедура ПриЗаписи(Отказ)\nКонецПроцедуры",
        "Document/Заказ/DocumentForm/ФормаДокумента/DocumentForm.obj.bsl": "Процедура Открыть()\nКонецПроцедуры",
        "Catalog/Товары/CatalogCommand/Печать/CatalogCommand.obj.bsl": "Процедура Выполнить()\nКонецПроцедуры",
        "Sequences/Расчеты/Sequences.obj.bsl": "",
        "Configuration.802.bsl": "",
        "Configuration.app.bsl": "",
        "Configuration.seance.bsl": "",
        "Configuration.con.bsl": "",
    }
    for relative_path, content in files.items():
        path = tmp_path / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8-sig")
    return tmp_path


def test_detect_v8unpack(v8unpack_source):
    info = detect_format(str(v8unpack_source))
    assert info.primary_format == SourceFormat.V8UNPACK
    assert info.format_label == "v8unpack"
    assert "Catalogs" in info.metadata_categories_found
    assert "CommonModules" in info.metadata_categories_found


@pytest.mark.parametrize(
    "configuration",
    [
        {},
        [],
        {"v8unpack": ""},
        {"v8unpack": 129},
    ],
)
def test_v8unpack_marker_must_be_nonempty_string(tmp_path, configuration):
    (tmp_path / "Configuration.json").write_text(json.dumps(configuration), encoding="utf-8")
    module = tmp_path / "Catalog" / "Товары" / "Catalog.obj.bsl"
    module.parent.mkdir(parents=True)
    module.write_text("", encoding="utf-8")
    assert detect_format(str(tmp_path)).primary_format == SourceFormat.UNKNOWN


def test_malformed_v8unpack_descriptor_is_unknown(tmp_path):
    (tmp_path / "Configuration.json").write_text("{", encoding="utf-8")
    module = tmp_path / "Catalog" / "Товары" / "Catalog.obj.bsl"
    module.parent.mkdir(parents=True)
    module.write_text("", encoding="utf-8")
    assert detect_format(str(tmp_path)).primary_format == SourceFormat.UNKNOWN


def test_unreadable_v8unpack_descriptor_is_unknown(tmp_path, monkeypatch):
    descriptor = tmp_path / "Configuration.json"
    descriptor.write_text(json.dumps({"v8unpack": "1.2.9"}), encoding="utf-8")
    module = tmp_path / "Catalog" / "Товары" / "Catalog.obj.bsl"
    module.parent.mkdir(parents=True)
    module.write_text("", encoding="utf-8")
    original_open = Path.open

    def denied_open(path, *args, **kwargs):
        if path == descriptor:
            raise PermissionError("denied")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", denied_open)
    assert detect_format(str(tmp_path)).primary_format == SourceFormat.UNKNOWN


@pytest.mark.parametrize("existing_format", ["cf", "edt"])
def test_cf_and_edt_take_priority_over_v8unpack(tmp_path, existing_format):
    (tmp_path / "Configuration.json").write_text(json.dumps({"v8unpack": "1.2.9"}), encoding="utf-8")
    v8_module = tmp_path / "Catalog" / "Товары" / "Catalog.obj.bsl"
    v8_module.parent.mkdir(parents=True)
    v8_module.write_text("", encoding="utf-8")
    if existing_format == "cf":
        (tmp_path / "Configuration.xml").write_text("<Configuration/>", encoding="utf-8")
        (tmp_path / "Ext").mkdir()
        expected = SourceFormat.CF
    else:
        edt = tmp_path / "Configuration" / "Configuration.mdo"
        edt.parent.mkdir()
        edt.write_text("<mdo/>", encoding="utf-8")
        expected = SourceFormat.EDT
    assert detect_format(str(tmp_path)).primary_format == expected


def test_detector_does_not_open_object_json(v8unpack_source, monkeypatch):
    object_json = v8unpack_source / "Catalog" / "Товары" / "Catalog.json"
    object_json.write_text("{broken", encoding="utf-8")
    original_open = Path.open
    opened_json = []

    def tracked_open(path, *args, **kwargs):
        if path.suffix.lower() == ".json":
            opened_json.append(path)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", tracked_open)
    assert detect_format(str(v8unpack_source)).primary_format == SourceFormat.V8UNPACK
    assert opened_json == [v8unpack_source / "Configuration.json"]


@pytest.mark.parametrize(
    ("source_category", "canonical_category"),
    [
        ("AccountingRegister", "AccountingRegisters"),
        ("AccumulationRegister", "AccumulationRegisters"),
        ("BusinessProcess", "BusinessProcesses"),
        ("CalculationRegister", "CalculationRegisters"),
        ("Catalog", "Catalogs"),
        ("ChartOfAccounts", "ChartsOfAccounts"),
        ("ChartOfCalculationTypes", "ChartsOfCalculationTypes"),
        ("ChartOfCharacteristicType", "ChartsOfCharacteristicTypes"),
        ("CommonCommand", "CommonCommands"),
        ("CommonForm", "CommonForms"),
        ("CommonModule", "CommonModules"),
        ("Constant", "Constants"),
        ("DataProcessor", "DataProcessors"),
        ("Document", "Documents"),
        ("DocumentJournal", "DocumentJournals"),
        ("Enum", "Enums"),
        ("ExchangePlan", "ExchangePlans"),
        ("ExternalDataSource", "ExternalDataSources"),
        ("FilterCriterion", "FilterCriteria"),
        ("HTTPService", "HTTPServices"),
        ("InformationRegister", "InformationRegisters"),
        ("Report", "Reports"),
        ("SettingsStorage", "SettingsStorages"),
        ("Task", "Tasks"),
        ("WebService", "WebServices"),
        ("Sequences", "Sequences"),
    ],
)
def test_v8unpack_category_mapping(tmp_path, source_category, canonical_category):
    info = parse_bsl_path(
        str(tmp_path / source_category / "Объект" / f"{source_category}.obj.bsl"),
        str(tmp_path),
    )
    assert info.category == canonical_category
    assert info.object_name == "Объект"


@pytest.mark.parametrize(
    ("relative_path", "module_type"),
    [
        ("CommonModule/Общий/CommonModule.obj.bsl", "Module"),
        ("CommonCommand/Обновить/CommonCommand.obj.bsl", "CommandModule"),
        ("Catalog/Товары/Catalog.obj.bsl", "ObjectModule"),
        ("Document/Заказ/Document.mgr.bsl", "ManagerModule"),
        ("InformationRegister/Цены/InformationRegister.obj.bsl", "RecordSetModule"),
        ("AccumulationRegister/Остатки/AccumulationRegister.obj.bsl", "RecordSetModule"),
        ("AccountingRegister/Хозрасчетный/AccountingRegister.obj.bsl", "RecordSetModule"),
        ("CalculationRegister/Начисления/CalculationRegister.obj.bsl", "RecordSetModule"),
        ("Sequences/Расчеты/Sequences.obj.bsl", "RecordSetModule"),
        ("Constant/ИспользоватьОбмен/Constant.obj.bsl", "ValueManagerModule"),
        ("Enum/ВидыОпераций/Enum.obj.bsl", "ManagerModule"),
        ("FilterCriterion/Отбор/FilterCriterion.obj.bsl", "ManagerModule"),
        ("DocumentJournal/Документы/DocumentJournal.obj.bsl", "ManagerModule"),
        ("HTTPService/Интеграция/HTTPService.obj.bsl", "Module"),
        ("WebService/Интеграция/WebService.obj.bsl", "Module"),
        ("Configuration.802.bsl", "ManagedApplicationModule"),
        ("Configuration.app.bsl", "OrdinaryApplicationModule"),
        ("Configuration.seance.bsl", "SessionModule"),
        ("Configuration.con.bsl", "ExternalConnectionModule"),
    ],
)
def test_v8unpack_module_types(tmp_path, relative_path, module_type):
    assert parse_bsl_path(str(tmp_path / relative_path), str(tmp_path)).module_type == module_type


def test_v8unpack_form_command_and_unknown_module(tmp_path):
    form = parse_bsl_path(
        str(tmp_path / "Document/Заказ/DocumentForm/ФормаДокумента/DocumentForm.obj.bsl"),
        str(tmp_path),
    )
    assert (form.category, form.object_name, form.form_name, form.module_type, form.is_form_module) == (
        "Documents",
        "Заказ",
        "ФормаДокумента",
        "Module",
        True,
    )

    generic_form = parse_bsl_path(
        str(tmp_path / "DataProcessor/Обработка/Form/Форма/Form.obj.bsl"),
        str(tmp_path),
    )
    assert (generic_form.object_name, generic_form.form_name, generic_form.module_type) == (
        "Обработка",
        "Форма",
        "Module",
    )

    common_form = parse_bsl_path(
        str(tmp_path / "CommonForm/Настройка/CommonForm.obj.bsl"),
        str(tmp_path),
    )
    assert (common_form.category, common_form.object_name, common_form.form_name, common_form.module_type) == (
        "CommonForms",
        "Настройка",
        "Настройка",
        "Module",
    )

    command = parse_bsl_path(
        str(tmp_path / "Catalog/Товары/CatalogCommand/Печать/CatalogCommand.obj.bsl"),
        str(tmp_path),
    )
    assert (command.object_name, command.command_name, command.module_type) == (
        "Товары",
        "Печать",
        "CommandModule",
    )

    unknown = parse_bsl_path(
        str(tmp_path / "Catalog/Товары/Unexpected.bsl"),
        str(tmp_path),
    )
    assert (unknown.category, unknown.object_name, unknown.module_type) == ("Catalogs", "Товары", None)


@pytest.mark.parametrize(
    "relative_path",
    [
        "Catalog/Товары/CatalogForm/Форма/Unexpected.bsl",
        "CommonForm/Настройка/Unexpected.bsl",
        "Catalog/Товары/CatalogCommand/Печать/Unexpected.bsl",
        "CommonCommand/Обновить/Unexpected.bsl",
    ],
)
def test_v8unpack_nested_unknown_filename_has_no_guessed_role(tmp_path, relative_path):
    info = parse_bsl_path(str(tmp_path / relative_path), str(tmp_path))
    assert info.module_type is None
    assert info.form_name is None
    assert info.command_name is None


def _make_bsl_helpers(base_path, reader):
    helpers, resolve_safe = make_helpers(str(base_path))
    return make_bsl_helpers(
        base_path=str(base_path),
        resolve_safe=resolve_safe,
        read_file_fn=helpers["read_file"],
        grep_fn=helpers["grep"],
        glob_files_fn=helpers["glob_files"],
        format_info=detect_format(str(base_path)),
        idx_reader=reader,
        register_git_search="never",
    )


def test_v8unpack_index_integration(v8unpack_source, monkeypatch, capsys):
    monkeypatch.setenv("RLM_INDEX_DIR", str(v8unpack_source / ".index"))
    db_path = IndexBuilder().build(
        str(v8unpack_source),
        build_calls=True,
        build_metadata=False,
        build_fts=False,
        build_synonyms=False,
    )
    reader = IndexReader(db_path)
    try:
        startup = reader.get_startup_meta()
        assert startup["source_format"] == "v8unpack"
        _cmd_info(Namespace(path=str(v8unpack_source)))
        assert "Format:   v8unpack" in capsys.readouterr().out

        conn = sqlite3.connect(db_path)
        rows = {
            row[0]: row[1:]
            for row in conn.execute(
                "SELECT rel_path, category, object_name, module_type, form_name FROM modules"
            )
        }
        conn.close()
        assert rows["CommonModule/Общий/CommonModule.obj.bsl"][:3] == (
            "CommonModules",
            "Общий",
            "Module",
        )
        assert rows["InformationRegister/Цены/InformationRegister.obj.bsl"][:3] == (
            "InformationRegisters",
            "Цены",
            "RecordSetModule",
        )
        assert rows["Document/Заказ/DocumentForm/ФормаДокумента/DocumentForm.obj.bsl"][3] == (
            "ФормаДокумента"
        )
        assert rows["Catalog/Товары/CatalogCommand/Печать/CatalogCommand.obj.bsl"][:3] == (
            "Catalogs",
            "Товары",
            "CommandModule",
        )

        bsl = _make_bsl_helpers(v8unpack_source, reader)
        definition = bsl["find_definition"]("Вызвать", "Общий")
        assert definition["definitions"][0]["file"] == "CommonModule/Общий/CommonModule.obj.bsl"
        callers = reader.get_callers("Вызвать")
        assert callers["_meta"]["exact_rows"] == 1
        hierarchy = bsl["find_call_hierarchy"]("Вызвать", depth=1)
        assert hierarchy["_meta"]["root_exact"] is True
        movements = reader.get_register_movements("Заказ")
        assert any(row["register_name"] == "Цены" for row in movements)
    finally:
        reader.close()


def test_update_rebuilds_previous_index_coordinates(v8unpack_source, monkeypatch):
    monkeypatch.setenv("RLM_INDEX_DIR", str(v8unpack_source / ".index"))
    builder = IndexBuilder()
    db_path = builder.build(
        str(v8unpack_source),
        build_calls=False,
        build_metadata=False,
        build_fts=False,
        build_synonyms=False,
    )
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE modules SET category=NULL, object_name=NULL, module_type=NULL "
        "WHERE rel_path='CommonModule/Общий/CommonModule.obj.bsl'"
    )
    conn.execute(
        "UPDATE index_meta SET value=? WHERE key='builder_version'",
        (str(BUILDER_VERSION - 1),),
    )
    conn.commit()
    conn.close()

    result = builder.update(str(v8unpack_source))
    assert result["rebuild_reason"] == f"schema upgrade v{BUILDER_VERSION - 1}->{BUILDER_VERSION}"
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT category, object_name, module_type FROM modules "
        "WHERE rel_path='CommonModule/Общий/CommonModule.obj.bsl'"
    ).fetchone()
    source_format = conn.execute(
        "SELECT value FROM index_meta WHERE key='source_format'"
    ).fetchone()[0]
    conn.close()
    assert row == ("CommonModules", "Общий", "Module")
    assert source_format == "v8unpack"


def test_old_file_cache_version_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "_cache_base", lambda: tmp_path / ".cache")
    base = str(tmp_path / "source")
    cache_dir = cache._cache_dir(base)
    cache_dir.mkdir(parents=True)
    (cache_dir / "file_index.json").write_text(
        json.dumps({"version": cache.CACHE_VERSION - 1, "bsl_count": 0, "entries": []}),
        encoding="utf-8",
    )
    assert cache.load_index(base, 0, []) is None


def test_rlm_start_warns_and_detects_live_format_for_old_index(v8unpack_source, monkeypatch):
    monkeypatch.setenv("RLM_INDEX_DIR", str(v8unpack_source / ".index"))
    db_path = IndexBuilder().build(
        str(v8unpack_source),
        build_calls=False,
        build_metadata=False,
        build_fts=False,
        build_synonyms=False,
    )
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE index_meta SET value=? WHERE key='builder_version'",
        (str(BUILDER_VERSION - 1),),
    )
    conn.execute("UPDATE index_meta SET value='unknown' WHERE key='source_format'")
    conn.commit()
    conn.close()

    result = json.loads(_rlm_start(path=str(v8unpack_source), query="v8unpack"))
    try:
        assert result["config_format"] == "v8unpack"
        assert any("current v" in warning for warning in result["index"]["warnings"])
        assert result["index"]["loaded"] is False
        assert result["index"]["index_status"] == "outdated"
        assert "index update" in result["index"]["warnings"][0]
    finally:
        _rlm_end(result["session_id"])
