import os
import tempfile
from pathlib import Path

import pytest

from rlm_tools_bsl.format_detector import (
    METADATA_CATEGORIES,
    MODULE_TYPE_MAP,
    SourceFormat,
    detect_format,
    parse_bsl_path,
)


# --- detect_format ---


def test_detect_cf_format():
    """CF format: Configuration.xml + /Ext/ directories with .bsl files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create CF-style structure
        os.makedirs(os.path.join(tmpdir, "CommonModules", "MyModule", "Ext"))
        with open(os.path.join(tmpdir, "CommonModules", "MyModule", "Ext", "Module.bsl"), "w") as f:
            f.write("// code")
        with open(os.path.join(tmpdir, "Configuration.xml"), "w") as f:
            f.write("<Configuration/>")

        info = detect_format(tmpdir)
        assert info.primary_format == SourceFormat.CF
        assert info.has_configuration_xml is True
        assert info.bsl_file_count >= 1
        assert "CommonModules" in info.metadata_categories_found
        assert info.format_label == "cf"


def test_detect_edt_format():
    """EDT format: .mdo files, no /Ext/."""
    with tempfile.TemporaryDirectory() as tmpdir:
        os.makedirs(os.path.join(tmpdir, "CommonModules", "MyModule"))
        with open(os.path.join(tmpdir, "CommonModules", "MyModule", "Module.bsl"), "w") as f:
            f.write("// code")
        with open(os.path.join(tmpdir, "CommonModules", "MyModule", "MyModule.mdo"), "w") as f:
            f.write("<mdo/>")

        info = detect_format(tmpdir)
        assert info.primary_format == SourceFormat.EDT
        assert info.bsl_file_count >= 1
        assert "CommonModules" in info.metadata_categories_found
        assert info.format_label == "edt"


def test_detect_unknown_format():
    """Unknown: just .bsl files without CF/EDT markers."""
    with tempfile.TemporaryDirectory() as tmpdir:
        with open(os.path.join(tmpdir, "script.bsl"), "w") as f:
            f.write("// code")

        info = detect_format(tmpdir)
        assert info.primary_format == SourceFormat.UNKNOWN
        assert info.bsl_file_count >= 1
        assert info.format_label == "unknown"


def test_detect_empty_directory():
    """Empty directory: UNKNOWN with 0 files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        info = detect_format(tmpdir)
        assert info.primary_format == SourceFormat.UNKNOWN
        assert info.bsl_file_count == 0


# --- parse_bsl_path ---


def test_parse_cf_common_module():
    with tempfile.TemporaryDirectory() as base:
        result = parse_bsl_path(
            os.path.join(base, "CommonModules", "MyModule", "Ext", "Module.bsl"),
            base,
        )
        assert result.category == "CommonModules"
        assert result.object_name == "MyModule"
        assert result.module_type == "Module"
        assert result.is_form_module is False
        assert result.form_name is None
        assert result.command_name is None


def test_parse_cf_document_object_module():
    with tempfile.TemporaryDirectory() as base:
        result = parse_bsl_path(
            os.path.join(base, "Documents", "АвансовыйОтчет", "Ext", "ObjectModule.bsl"),
            base,
        )
        assert result.category == "Documents"
        assert result.object_name == "АвансовыйОтчет"
        assert result.module_type == "ObjectModule"
        assert result.is_form_module is False


def test_parse_cf_form_module():
    with tempfile.TemporaryDirectory() as base:
        result = parse_bsl_path(
            os.path.join(base, "Documents", "АвансовыйОтчет", "Forms", "ФормаДокумента", "Ext", "Form", "Module.bsl"),
            base,
        )
        assert result.category == "Documents"
        assert result.object_name == "АвансовыйОтчет"
        assert result.module_type == "Module"
        assert result.form_name == "ФормаДокумента"
        assert result.is_form_module is True


def test_parse_cf_command_module():
    with tempfile.TemporaryDirectory() as base:
        result = parse_bsl_path(
            os.path.join(base, "Catalogs", "Номенклатура", "Commands", "Print", "Ext", "CommandModule.bsl"),
            base,
        )
        assert result.category == "Catalogs"
        assert result.object_name == "Номенклатура"
        assert result.command_name == "Print"
        assert result.module_type == "CommandModule"


def test_parse_edt_common_module():
    with tempfile.TemporaryDirectory() as base:
        result = parse_bsl_path(
            os.path.join(base, "CommonModules", "тст_Интеграция", "Module.bsl"),
            base,
        )
        assert result.category == "CommonModules"
        assert result.object_name == "тст_Интеграция"
        assert result.module_type == "Module"
        assert result.is_form_module is False


def test_parse_edt_form_module():
    with tempfile.TemporaryDirectory() as base:
        result = parse_bsl_path(
            os.path.join(base, "Catalogs", "тст_ВходящиеСообщения", "Forms", "ФормаСписка", "Module.bsl"),
            base,
        )
        assert result.category == "Catalogs"
        assert result.object_name == "тст_ВходящиеСообщения"
        assert result.form_name == "ФормаСписка"
        assert result.is_form_module is True


def test_parse_flat_path():
    """File without standard metadata structure."""
    with tempfile.TemporaryDirectory() as base:
        result = parse_bsl_path(
            os.path.join(base, "scripts", "myfile.bsl"),
            base,
        )
        assert result.category is None
        assert result.object_name is None
        assert result.is_form_module is False
        assert "myfile.bsl" in result.relative_path


def test_parse_register_module():
    with tempfile.TemporaryDirectory() as base:
        result = parse_bsl_path(
            os.path.join(base, "AccumulationRegisters", "ТоварыНаСкладах", "Ext", "RecordSetModule.bsl"),
            base,
        )
        assert result.category == "AccumulationRegisters"
        assert result.object_name == "ТоварыНаСкладах"
        assert result.module_type == "RecordSetModule"


# --- Constants ---


def test_metadata_categories_is_frozenset():
    assert isinstance(METADATA_CATEGORIES, frozenset)
    assert "CommonModules" in METADATA_CATEGORIES
    assert "Documents" in METADATA_CATEGORIES
    assert len(METADATA_CATEGORIES) >= 20


def test_module_type_map_completeness():
    assert "Module.bsl" in MODULE_TYPE_MAP
    assert "ObjectModule.bsl" in MODULE_TYPE_MAP
    assert "ManagerModule.bsl" in MODULE_TYPE_MAP
    assert MODULE_TYPE_MAP["Module.bsl"] == "Module"


# --- classify_source: гейт неподдерживаемых форматов (v1.32.0) ---

CF_DESCRIPTOR = """<?xml version="1.0" encoding="UTF-8"?>
<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">
  <Configuration uuid="00000000-0000-0000-0000-000000000001">
    <Properties><Name>Тест</Name></Properties>
  </Configuration>
</MetaDataObject>
"""

EDT_DESCRIPTOR = """<?xml version="1.0" encoding="UTF-8"?>
<mdclass:Configuration
    xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass"
    name="Тест"/>
"""


def _write_descriptor(root: Path, kind: str) -> None:
    if kind == "cf":
        root.mkdir(parents=True, exist_ok=True)
        (root / "Configuration.xml").write_text(CF_DESCRIPTOR, encoding="utf-8")
    else:
        cfg = root / "Configuration"
        cfg.mkdir(parents=True, exist_ok=True)
        (cfg / "Configuration.mdo").write_text(EDT_DESCRIPTOR, encoding="utf-8")


@pytest.mark.parametrize("kind", ["cf", "edt"])
@pytest.mark.parametrize("parts", [(), ("ExtA",), ("cfe", "ExtA")])
def test_supported_descriptor_layouts(tmp_path, kind, parts):
    from rlm_tools_bsl.format_detector import SourceSupport, classify_source

    root = tmp_path.joinpath(*parts)
    _write_descriptor(root, kind)
    assert classify_source(str(tmp_path)) is SourceSupport.SUPPORTED


def test_nested_edt_cfe_is_supported(tmp_path):
    """Регрессия: base/cfe/ExtA/Configuration/Configuration.mdo — наш EDT-CFE."""
    from rlm_tools_bsl.format_detector import SourceSupport, classify_source

    _write_descriptor(tmp_path / "cfe" / "ExtA", "edt")
    (tmp_path / "cfe" / "ExtA" / "Module.bsl").write_text("// code", encoding="utf-8")
    assert classify_source(str(tmp_path)) is SourceSupport.SUPPORTED


@pytest.mark.parametrize(
    ("relative", "content"),
    [
        ("docs/Configuration.xml", "<settings/>"),
        ("Foo/Configuration/Configuration.mdo", "<root/>"),
        ("Document/X/layout.mdo", "<root/>"),
    ],
)
def test_foreign_descriptor_name_collision_does_not_bypass_gate(tmp_path, relative, content):
    """Имя файла без сигнатуры CF/EDT не легализует чужое дерево."""
    from rlm_tools_bsl.format_detector import SourceSupport, classify_source

    target = tmp_path / relative
    target.parent.mkdir(parents=True)
    target.write_text(content, encoding="utf-8")
    (tmp_path / "Module.bsl").write_text("// code", encoding="utf-8")
    assert classify_source(str(tmp_path)) is SourceSupport.FOREIGN_WITH_BSL


@pytest.mark.parametrize(
    ("relative", "declared_encoding"),
    [
        ("Configuration.xml", "x-invalid"),
        ("Configuration/Configuration.mdo", "x-bogus"),
    ],
)
def test_unknown_xml_encoding_is_not_a_descriptor(tmp_path, relative, declared_encoding):
    """Незнакомая кодировка в XML-декларации не роняет классификатор.

    ElementTree на такой декларации бросает LookupError, а НЕ ParseError.
    Без перехвата исключение уходило наружу и валило оба гейта build
    необработанной ошибкой вместо штатного отказа."""
    from rlm_tools_bsl.format_detector import SourceSupport, classify_source

    target = tmp_path / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        f'<?xml version="1.0" encoding="{declared_encoding}"?><root/>',
        encoding="utf-8",
    )
    (tmp_path / "Module.bsl").write_text("// code", encoding="utf-8")
    assert classify_source(str(tmp_path)) is SourceSupport.FOREIGN_WITH_BSL


def test_invalid_root_configuration_xml_is_not_supported(tmp_path):
    from rlm_tools_bsl.format_detector import SourceSupport, classify_source

    (tmp_path / "Configuration.xml").write_text("<Configuration/>", encoding="utf-8")
    assert classify_source(str(tmp_path)) is SourceSupport.FOREIGN_NO_BSL


def test_foreign_with_and_without_bsl(tmp_path):
    from rlm_tools_bsl.format_detector import SourceSupport, classify_source

    (tmp_path / "Configuration.json").write_text('{"foreign": true}', encoding="utf-8")
    assert classify_source(str(tmp_path)) is SourceSupport.FOREIGN_NO_BSL

    (tmp_path / "Configuration.802.bsl").write_text("// code", encoding="utf-8")
    assert classify_source(str(tmp_path)) is SourceSupport.FOREIGN_WITH_BSL


def test_probe_bsl_finds_root_and_deep_files(tmp_path):
    from rlm_tools_bsl.format_detector import probe_bsl

    deep = tmp_path / "a" / "b" / "c"
    deep.mkdir(parents=True)
    (deep / "Module.bsl").write_text("// code", encoding="utf-8")
    assert probe_bsl(str(tmp_path)) == "found"


def test_probe_bsl_reports_unknown_for_missing_root(tmp_path):
    from rlm_tools_bsl.format_detector import probe_bsl

    assert probe_bsl(str(tmp_path / "missing")) == "unknown"


def test_probe_bsl_matches_indexer_case_semantics(tmp_path):
    from rlm_tools_bsl.format_detector import probe_bsl

    (tmp_path / "Module.BSL").write_text("// code", encoding="utf-8")
    expected = "found" if list(tmp_path.rglob("*.bsl")) else "none"
    assert probe_bsl(str(tmp_path)) == expected


def test_probe_bsl_matches_indexer_hidden_directory_semantics(tmp_path):
    from rlm_tools_bsl.format_detector import probe_bsl

    obj = tmp_path / ".git" / "objects"
    obj.mkdir(parents=True)
    (obj / "stray.bsl").write_text("// code", encoding="utf-8")
    expected = "found" if list(tmp_path.rglob("*.bsl")) else "none"
    assert probe_bsl(str(tmp_path)) == expected


def test_cf_descriptor_early_exit_ignores_tail_after_first_child(tmp_path):
    """Гарантия скорости: дескриптор читается префиксно, хвост файла — нет.

    Битый хвост лежит за пределами первого 16КБ-чанка парсера: реализация
    с полным ET.parse упала бы на нём и потеряла SUPPORTED."""
    from rlm_tools_bsl.format_detector import SourceSupport, classify_source

    prefix = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">\n'
        '  <Configuration uuid="00000000-0000-0000-0000-000000000001">\n'
    )
    filler = "    <Comment>" + "x" * 262_144 + "</Comment>\n"
    (tmp_path / "Configuration.xml").write_text(prefix + filler + "  <<< битый хвост", encoding="utf-8")
    assert classify_source(str(tmp_path)) is SourceSupport.SUPPORTED


def test_descriptor_scan_lists_only_top_two_levels(tmp_path, monkeypatch):
    """Гарантия скорости: листинг каталогов только на глубине 0-1.

    Прямой вызов has_our_format_descriptor: classify_source дальше зовет
    probe_bsl, который законно обходит все дерево через os.walk."""
    import os

    from rlm_tools_bsl.format_detector import has_our_format_descriptor

    deep = tmp_path / "A" / "B" / "C" / "D"
    deep.mkdir(parents=True)
    listed: list[str] = []
    real_scandir = os.scandir

    def counting_scandir(path):
        listed.append(os.fspath(path))
        return real_scandir(path)

    monkeypatch.setattr("rlm_tools_bsl.format_detector.os.scandir", counting_scandir)
    assert has_our_format_descriptor(str(tmp_path)) is False
    max_depth = max(len(Path(p).resolve().relative_to(tmp_path.resolve()).parts) for p in listed)
    assert max_depth <= 1


def test_classify_speed_on_multimegabyte_descriptor(tmp_path):
    """Замер: ранний выход держит классификацию в единицах мс на ~35 МБ дескрипторе.

    Первый проход не меряется: первое открытие свежезаписанного большого файла
    оплачивает антивирус/кеш ФС (замерено ~500 мс на Defender). Полный ET.parse
    такого файла ~1 с — порог 0.2 с отделяет ранний выход с ~200-кратным запасом."""
    import time

    from rlm_tools_bsl.format_detector import SourceSupport, classify_source

    tail = "<ChildObjects>" + "<Language>Ru</Language>" * 1_500_000 + "</ChildObjects>"
    (tmp_path / "Configuration.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">\n'
        '<Configuration uuid="00000000-0000-0000-0000-000000000001">'
        "<Properties><Name>Тест</Name></Properties></Configuration>\n" + tail + "\n</MetaDataObject>\n",
        encoding="utf-8",
    )
    assert classify_source(str(tmp_path)) is SourceSupport.SUPPORTED  # прогрев — не меряем
    t0 = time.perf_counter()
    assert classify_source(str(tmp_path)) is SourceSupport.SUPPORTED
    assert time.perf_counter() - t0 < 0.2
