"""Tests for extension_detector module."""

import builtins
import os
import tempfile
import textwrap

import pytest

from rlm_tools_bsl.extension_detector import (
    ConfigRole,
    detect_extension_context,
    find_extension_overrides,
    _detect_single,
)


# ---------------------------------------------------------------------------
# Helpers to create minimal Configuration XML
# ---------------------------------------------------------------------------

_CF_MAIN_XML = textwrap.dedent("""\
    <?xml version="1.0" encoding="UTF-8"?>
    <MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"
                    xmlns:v8="http://v8.1c.ru/8.1/data/core">
        <Configuration uuid="00000000-0000-0000-0000-000000000001">
            <Properties>
                <Name>МояКонфигурация</Name>
                <NamePrefix/>
                <ConfigurationExtensionCompatibilityMode>Version8_3_24</ConfigurationExtensionCompatibilityMode>
            </Properties>
        </Configuration>
    </MetaDataObject>
""")


def _cf_extension_xml(name="ТестовоеРасширение", purpose="Customization", prefix="мр_"):
    return textwrap.dedent(f"""\
        <?xml version="1.0" encoding="UTF-8"?>
        <MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"
                        xmlns:v8="http://v8.1c.ru/8.1/data/core">
            <Configuration uuid="00000000-0000-0000-0000-000000000002">
                <Properties>
                    <ObjectBelonging>Adopted</ObjectBelonging>
                    <Name>{name}</Name>
                    <ConfigurationExtensionPurpose>{purpose}</ConfigurationExtensionPurpose>
                    <NamePrefix>{prefix}</NamePrefix>
                </Properties>
            </Configuration>
        </MetaDataObject>
    """)


_EDT_MAIN_MDO = textwrap.dedent("""\
    <?xml version="1.0" encoding="UTF-8"?>
    <mdclass:Configuration xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass"
                           uuid="00000000-0000-0000-0000-000000000003">
        <name>МояЕДТКонфигурация</name>
        <defaultRunMode>ManagedApplication</defaultRunMode>
    </mdclass:Configuration>
""")


def _edt_extension_mdo(name="ТестовоеЕДТРасширение", purpose="AddOn", prefix="тст_"):
    return textwrap.dedent(f"""\
        <?xml version="1.0" encoding="UTF-8"?>
        <mdclass:Configuration xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
                               xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass"
                               xmlns:mdclassExtension="http://g5.1c.ru/v8/dt/metadata/mdclass/extension"
                               uuid="00000000-0000-0000-0000-000000000004">
            <name>{name}</name>
            <objectBelonging>Adopted</objectBelonging>
            <extension xsi:type="mdclassExtension:ConfigurationExtension">
                <defaultRunMode>Checked</defaultRunMode>
            </extension>
            <namePrefix>{prefix}</namePrefix>
            <configurationExtensionPurpose>{purpose}</configurationExtensionPurpose>
        </mdclass:Configuration>
    """)


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


# ---------------------------------------------------------------------------
# Tests: _detect_single
# ---------------------------------------------------------------------------


def test_detect_main_cf():
    with tempfile.TemporaryDirectory() as d:
        _write(os.path.join(d, "Configuration.xml"), _CF_MAIN_XML)
        info = _detect_single(d)
        assert info is not None
        assert info.role == ConfigRole.MAIN
        assert info.name == "МояКонфигурация"
        assert info.source_format == "cf"
        assert info.purpose == ""
        assert info.name_prefix == ""


def test_detect_extension_cf():
    with tempfile.TemporaryDirectory() as d:
        _write(os.path.join(d, "Configuration.xml"), _cf_extension_xml("ТестовоеРасширение", "AddOn", "мр_"))
        info = _detect_single(d)
        assert info is not None
        assert info.role == ConfigRole.EXTENSION
        assert info.name == "ТестовоеРасширение"
        assert info.purpose == "AddOn"
        assert info.name_prefix == "мр_"
        assert info.source_format == "cf"


def test_detect_main_edt():
    with tempfile.TemporaryDirectory() as d:
        _write(os.path.join(d, "Configuration", "Configuration.mdo"), _EDT_MAIN_MDO)
        info = _detect_single(d)
        assert info is not None
        assert info.role == ConfigRole.MAIN
        assert info.name == "МояЕДТКонфигурация"
        assert info.source_format == "edt"


def test_detect_extension_edt():
    with tempfile.TemporaryDirectory() as d:
        _write(
            os.path.join(d, "Configuration", "Configuration.mdo"),
            _edt_extension_mdo("ТестовоеРасширение", "Customization", "тст_"),
        )
        info = _detect_single(d)
        assert info is not None
        assert info.role == ConfigRole.EXTENSION
        assert info.name == "ТестовоеРасширение"
        assert info.purpose == "Customization"
        assert info.name_prefix == "тст_"
        assert info.source_format == "edt"


def test_cfe_wrapper_dir():
    """Extension inside a wrapper subdirectory (e.g. cfe/MyExt/Configuration.xml)."""
    with tempfile.TemporaryDirectory() as d:
        wrapper = os.path.join(d, "ТестовоеРасширение")
        _write(os.path.join(wrapper, "Configuration.xml"), _cf_extension_xml("Расш1", "Fix", "р1_"))
        info = _detect_single(d)
        assert info is not None
        assert info.role == ConfigRole.EXTENSION
        assert info.name == "Расш1"
        assert info.purpose == "Fix"
        # path should point to the wrapper, not the parent
        assert info.path == wrapper


def test_empty_dir():
    with tempfile.TemporaryDirectory() as d:
        info = _detect_single(d)
        assert info is None


# ---------------------------------------------------------------------------
# Tests: detect_extension_context
# ---------------------------------------------------------------------------


def test_nearby_extensions():
    """Main config sees nearby extension."""
    with tempfile.TemporaryDirectory() as parent:
        main_dir = os.path.join(parent, "main")
        ext_dir = os.path.join(parent, "доработки")
        _write(os.path.join(main_dir, "Configuration.xml"), _CF_MAIN_XML)
        _write(os.path.join(ext_dir, "Configuration.xml"), _cf_extension_xml("Расш", "Customization", "р_"))

        ctx = detect_extension_context(main_dir)
        assert ctx.current.role == ConfigRole.MAIN
        assert len(ctx.nearby_extensions) == 1
        assert ctx.nearby_extensions[0].name == "Расш"
        assert ctx.nearby_extensions[0].purpose == "Customization"
        assert len(ctx.warnings) > 0
        assert "Расш" in ctx.warnings[0]


def test_nearby_main_from_extension():
    """Extension sees nearby main config."""
    with tempfile.TemporaryDirectory() as parent:
        main_dir = os.path.join(parent, "основная")
        ext_dir = os.path.join(parent, "расширение")
        _write(os.path.join(main_dir, "Configuration.xml"), _CF_MAIN_XML)
        _write(os.path.join(ext_dir, "Configuration.xml"), _cf_extension_xml("Расш", "AddOn", "р_"))

        ctx = detect_extension_context(ext_dir)
        assert ctx.current.role == ConfigRole.EXTENSION
        assert ctx.nearby_main is not None
        assert ctx.nearby_main.role == ConfigRole.MAIN
        assert ctx.nearby_main.name == "МояКонфигурация"
        assert len(ctx.warnings) >= 2  # extension warning + main found


def test_multiple_extensions():
    """All nearby extensions are found."""
    with tempfile.TemporaryDirectory() as parent:
        main_dir = os.path.join(parent, "конфа")
        _write(os.path.join(main_dir, "Configuration.xml"), _CF_MAIN_XML)

        for i, (name, purpose, prefix) in enumerate(
            [
                ("Расш1", "AddOn", "р1_"),
                ("Расш2", "Customization", "р2_"),
                ("Расш3", "Fix", "р3_"),
            ]
        ):
            ext_dir = os.path.join(parent, f"ext{i}")
            _write(os.path.join(ext_dir, "Configuration.xml"), _cf_extension_xml(name, purpose, prefix))

        ctx = detect_extension_context(main_dir)
        assert ctx.current.role == ConfigRole.MAIN
        assert len(ctx.nearby_extensions) == 3
        names = {e.name for e in ctx.nearby_extensions}
        assert names == {"Расш1", "Расш2", "Расш3"}


def test_empty_dir_context():
    with tempfile.TemporaryDirectory() as d:
        ctx = detect_extension_context(d)
        assert ctx.current.role == ConfigRole.UNKNOWN
        assert ctx.nearby_extensions == []
        assert ctx.nearby_main is None
        assert ctx.warnings == []


def test_warnings_main_with_extensions():
    with tempfile.TemporaryDirectory() as parent:
        main_dir = os.path.join(parent, "main")
        ext_dir = os.path.join(parent, "ext")
        _write(os.path.join(main_dir, "Configuration.xml"), _CF_MAIN_XML)
        _write(os.path.join(ext_dir, "Configuration.xml"), _cf_extension_xml("Тест", "AddOn", "т_"))

        ctx = detect_extension_context(main_dir)
        assert any("Extensions detected" in w for w in ctx.warnings)
        assert any("ChangeAndValidate" in w for w in ctx.warnings)


def test_warnings_extension_standalone():
    """Extension without nearby main config."""
    with tempfile.TemporaryDirectory() as parent:
        ext_dir = os.path.join(parent, "ext")
        _write(os.path.join(ext_dir, "Configuration.xml"), _cf_extension_xml("Одиночка", "Customization", "о_"))

        ctx = detect_extension_context(ext_dir)
        assert ctx.current.role == ConfigRole.EXTENSION
        assert ctx.nearby_main is None
        assert any("EXTENSION" in w for w in ctx.warnings)


def test_multiple_extensions_in_container():
    """Container dir (e.g. cfe/) with several extensions — all must be found."""
    with tempfile.TemporaryDirectory() as parent:
        # Main config: parent/cf/Configuration.xml
        main_dir = os.path.join(parent, "cf")
        _write(os.path.join(main_dir, "Configuration.xml"), _CF_MAIN_XML)

        # Container: parent/cfe/ with two extensions
        cfe_dir = os.path.join(parent, "cfe")
        _write(
            os.path.join(cfe_dir, "Ext1", "Configuration.xml"),
            _cf_extension_xml("Расш1", "AddOn", "р1_"),
        )
        _write(
            os.path.join(cfe_dir, "Ext2", "Configuration.xml"),
            _cf_extension_xml("Расш2", "Customization", "р2_"),
        )

        ctx = detect_extension_context(main_dir)
        assert ctx.current.role == ConfigRole.MAIN
        assert len(ctx.nearby_extensions) == 2
        names = {e.name for e in ctx.nearby_extensions}
        assert names == {"Расш1", "Расш2"}


# ---------------------------------------------------------------------------
# Tests: find_extension_overrides
# ---------------------------------------------------------------------------

_BSL_WITH_ANNOTATIONS = textwrap.dedent("""\
    &Перед("ОбработкаПолученияФормы")
    Процедура мр_ОбработкаПолученияФормы(ВидФормы, Параметры, ВыбраннаяФорма, ДопИнформация, СтандартнаяОбработка)
        // ...
    КонецПроцедуры

    &После("ПриЗаписи")
    Процедура мр_ПриЗаписи(Отказ)
        // ...
    КонецПроцедуры

    &Вместо("ИспользованиеЭлементов")
    Функция мр_ИспользованиеЭлементов(ПереданныйОбъект)
        Результат = ПродолжитьВызов(ПереданныйОбъект);
        Возврат Результат;
    КонецФункции

    &ИзменениеИКонтроль("ПередЗаписью")
    Процедура мр_ПередЗаписью(Отказ)
        // ...
    КонецПроцедуры
""")


def test_find_overrides_all():
    """All 4 annotation types are detected."""
    with tempfile.TemporaryDirectory() as d:
        bsl_path = os.path.join(d, "Documents", "РеализацияТоваровУслуг", "Ext", "ManagerModule.bsl")
        _write(bsl_path, _BSL_WITH_ANNOTATIONS)

        overrides = find_extension_overrides(d)
        assert len(overrides) == 4

        annotations = {o["annotation"] for o in overrides}
        assert annotations == {"Перед", "После", "Вместо", "ИзменениеИКонтроль"}

        targets = {o["target_method"] for o in overrides}
        assert "ОбработкаПолученияФормы" in targets
        assert "ПриЗаписи" in targets
        assert "ИспользованиеЭлементов" in targets
        assert "ПередЗаписью" in targets

        # Check extension method names are captured
        ext_methods = {o["extension_method"] for o in overrides}
        assert "мр_ОбработкаПолученияФормы" in ext_methods
        assert "мр_ПередЗаписью" in ext_methods


def test_find_overrides_accepts_case_insensitive_bsl_suffix(tmp_path):
    bsl_path = tmp_path / "Documents" / "Док" / "Ext" / "ObjectModule.BSL"
    _write(str(bsl_path), '&Вместо("ОбработкаПроведения")\nПроцедура Замена()\nКонецПроцедуры\n')
    diagnostics = {}

    overrides = find_extension_overrides(str(tmp_path), diagnostics=diagnostics)

    assert [row["target_method"] for row in overrides] == ["ОбработкаПроведения"]
    assert overrides[0]["module_type"] == "ObjectModule"
    assert diagnostics["complete"] is True
    assert diagnostics["candidate_files"] == 1
    assert diagnostics["files_scanned"] == 1


def test_find_overrides_by_object():
    """Filtering by object_name works."""
    with tempfile.TemporaryDirectory() as d:
        bsl1 = os.path.join(d, "Documents", "Док1", "Ext", "ObjectModule.bsl")
        bsl2 = os.path.join(d, "Documents", "Док2", "Ext", "ObjectModule.bsl")
        _write(bsl1, '&Перед("Метод1")\nПроцедура р_Метод1()\nКонецПроцедуры\n')
        _write(bsl2, '&После("Метод2")\nПроцедура р_Метод2()\nКонецПроцедуры\n')

        all_overrides = find_extension_overrides(d)
        assert len(all_overrides) == 2

        doc1_overrides = find_extension_overrides(d, object_name="Док1")
        assert len(doc1_overrides) == 1
        assert doc1_overrides[0]["target_method"] == "Метод1"
        assert doc1_overrides[0]["object_name"] == "Док1"

        doc2_overrides = find_extension_overrides(d, object_name="Док2")
        assert len(doc2_overrides) == 1
        assert doc2_overrides[0]["target_method"] == "Метод2"


def test_find_overrides_reports_unreadable_module(monkeypatch, tmp_path):
    """Rows from readable modules remain useful, but a failed BSL read must make
    scan completeness observable to aggregate callers."""
    readable = tmp_path / "Documents" / "Док1" / "Ext" / "ObjectModule.bsl"
    unreadable = tmp_path / "Documents" / "Док2" / "Ext" / "ObjectModule.bsl"
    _write(str(readable), '&Перед("Метод1")\nПроцедура р_Метод1()\nКонецПроцедуры\n')
    _write(str(unreadable), '&После("Метод2")\nПроцедура р_Метод2()\nКонецПроцедуры\n')

    real_open = builtins.open

    def _selective_open(path, *args, **kwargs):
        if os.path.normcase(os.fspath(path)) == os.path.normcase(str(unreadable)):
            raise PermissionError("module became unreadable")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", _selective_open)
    diagnostics = {}
    overrides = find_extension_overrides(str(tmp_path), diagnostics=diagnostics)

    assert [row["target_method"] for row in overrides] == ["Метод1"], overrides
    assert diagnostics["complete"] is False, diagnostics
    assert diagnostics["candidate_files"] == 2, diagnostics
    assert diagnostics["files_scanned"] == 1, diagnostics
    assert diagnostics["unreadable_files"] == ["Documents/Док2/Ext/ObjectModule.bsl"], diagnostics


def test_all_purposes():
    """All three purpose values are correctly detected."""
    for purpose in ("AddOn", "Customization", "Fix"):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "Configuration.xml"), _cf_extension_xml("Тест", purpose, "т_"))
            info = _detect_single(d)
            assert info is not None
            assert info.purpose == purpose


def test_annotation_izmenenieikontrol():
    """&ИзменениеИКонтроль annotation is correctly parsed."""
    with tempfile.TemporaryDirectory() as d:
        bsl = os.path.join(d, "CommonModules", "МойМодуль", "Ext", "Module.bsl")
        _write(
            bsl,
            textwrap.dedent("""\
            &ИзменениеИКонтроль("СтароеИмя")
            Процедура мр_СтароеИмя()
                // контроль
            КонецПроцедуры
        """),
        )

        overrides = find_extension_overrides(d)
        assert len(overrides) == 1
        assert overrides[0]["annotation"] == "ИзменениеИКонтроль"
        assert overrides[0]["target_method"] == "СтароеИмя"
        assert overrides[0]["extension_method"] == "мр_СтароеИмя"
        assert overrides[0]["object_name"] == "МойМодуль"
        assert overrides[0]["module_type"] == "Module"


# ---------------------------------------------------------------------------
# Tests: незнакомая кодировка в XML-декларации дескриптора
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("parser_name", "filename", "declared_encoding", "body"),
    [
        ("_parse_config_xml", "Configuration.xml", "x-invalid", "<MetaDataObject/>"),
        ("_parse_config_mdo", "Configuration.mdo", "x-bogus", "<mdclass:Configuration/>"),
    ],
)
def test_unknown_xml_encoding_yields_no_config_instead_of_crash(
    tmp_path, parser_name, filename, declared_encoding, body
):
    """Оба парсера дескриптора обязаны вернуть None, а не бросить исключение.

    ElementTree на незнакомой кодировке бросает LookupError, а НЕ ParseError.
    Раньше он уходил наружу и валил весь `detect_extension_context` — то есть
    и `rlm_start`, даже когда битый файл лежал у СОСЕДА (см. парный
    end-to-end тест в tests/test_foreign_format_gate.py).

    Файл лежит на два уровня ниже tmp_path: скан соседей достаёт
    `сосед/Configuration.xml` и `сосед/обёртка/Configuration.mdo`, поэтому
    более мелкое вложение сделало бы фикстуру видимой для чужих тестов.
    """
    import rlm_tools_bsl.extension_detector as ed

    target = tmp_path / "case" / "cfg" / filename
    target.parent.mkdir(parents=True)
    target.write_text(f'<?xml version="1.0" encoding="{declared_encoding}"?>{body}', encoding="utf-8")

    parser = getattr(ed, parser_name)
    assert parser(str(target), str(target.parent)) is None


def test_poisoned_edt_neighbour_does_not_break_extension_scan(tmp_path):
    """EDT-ветка скана соседей (`_scan_for_mdo` → `_parse_config_mdo`).

    Раскладка ровно как у настоящего соседа-EDT: `сосед/обёртка/Configuration.mdo`.
    Битый дескриптор соседа обязан быть просто не найден, а живая конфигурация
    рядом — по-прежнему найдена (иначе тест прошёл бы на «ничего не сканируем»).
    """
    parent = tmp_path / "container"

    broken = parent / "СломанныйСосед" / "cfg"
    broken.mkdir(parents=True)
    (broken / "Configuration.mdo").write_text(
        '<?xml version="1.0" encoding="x-bogus"?><mdclass:Configuration/>', encoding="utf-8"
    )

    healthy = parent / "ЖивойСосед" / "cfg"
    healthy.mkdir(parents=True)
    (healthy / "Configuration.mdo").write_text(_edt_extension_mdo(name="ЖивоеРасширение"), encoding="utf-8")

    own = parent / "Основная"
    _write(str(own / "Configuration" / "Configuration.mdo"), _EDT_MAIN_MDO)

    ctx = detect_extension_context(str(own))

    assert ctx.current.role == ConfigRole.MAIN
    names = {e.name for e in ctx.nearby_extensions}
    assert "ЖивоеРасширение" in names, "живой EDT-сосед потерян — тест перестал стеречь скан"
    assert not any(e.path.endswith("СломанныйСосед") for e in ctx.nearby_extensions)
