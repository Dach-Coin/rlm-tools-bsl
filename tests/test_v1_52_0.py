"""v1.52.0 — точная идентичность объекта и достоверные движения регистров."""

from __future__ import annotations

from pathlib import Path

import pytest

from rlm_tools_bsl.bsl_helpers import make_bsl_helpers
from rlm_tools_bsl.bsl_index import IndexBuilder, IndexReader
from rlm_tools_bsl.format_detector import detect_format
from rlm_tools_bsl.helpers import make_helpers


_CF_CONFIG = """<?xml version="1.0" encoding="UTF-8"?>
<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">
  <Configuration uuid="00000000-0000-0000-0000-000000000001">
    <Properties><Name>Тест</Name></Properties>
  </Configuration>
</MetaDataObject>
"""
_CF_EXTENSION = """<?xml version="1.0" encoding="UTF-8"?>
<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">
  <Configuration uuid="00000000-0000-0000-0000-000000000002">
    <Properties>
      <ObjectBelonging>Adopted</ObjectBelonging><Name>Расш1</Name>
      <ConfigurationExtensionPurpose>Customization</ConfigurationExtensionPurpose><NamePrefix>Расш1_</NamePrefix>
    </Properties>
  </Configuration>
</MetaDataObject>
"""


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _config(root: Path, *, extension: bool = False) -> None:
    _write(root / "Configuration.xml", _CF_EXTENSION if extension else _CF_CONFIG)


def _object(root: Path, category: str, kind: str, name: str, properties: str = "") -> None:
    _write(
        root / category / f"{name}.xml",
        f'<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses" '
        f'xmlns:v8="http://v8.1c.ru/8.1/data/core" xmlns:xr="http://v8.1c.ru/8.3/xcf/readable">'
        f"<{kind}><Properties><Name>{name}</Name>{properties}</Properties></{kind}></MetaDataObject>",
    )


def _module(root: Path, category: str, name: str, module_type: str, body: str) -> None:
    _write(root / category / name / "Ext" / f"{module_type}.bsl", body)


def _bsl(
    base: Path,
    reader: IndexReader | None = None,
    *,
    root: Path | None = None,
    role: str = "main",
    extensions=(),
    main_root: Path | None = None,
):
    helpers, resolve_safe = make_helpers(str(base), reader)
    current = root or base
    ext_list = [str(path) for path in extensions]
    return make_bsl_helpers(
        base_path=str(base),
        resolve_safe=resolve_safe,
        read_file_fn=helpers["read_file"],
        grep_fn=helpers["grep"],
        glob_files_fn=helpers["glob_files"],
        format_info=detect_format(str(current)),
        idx_reader=reader,
        extension_paths=ext_list,
        current_config_role=role,
        current_config_name="Расш1" if role == "extension" else "Тест",
        current_config_root=str(current),
        extension_name_by_root={str(path): f"Ext{i}" for i, path in enumerate(ext_list, 1)},
        main_config_root=str(main_root or ""),
        idx_zero_callers_authoritative=True,
    )


def _reader(root: Path, index_dir: Path, *, synonyms: bool = True) -> IndexReader:
    mp = pytest.MonkeyPatch()
    mp.setenv("RLM_INDEX_DIR", str(index_dir))
    try:
        return IndexReader(IndexBuilder().build(str(root), build_calls=False, build_synonyms=synonyms))
    finally:
        mp.undo()


def _movement_config(root: Path) -> None:
    _config(root)
    _object(root, "AccumulationRegisters", "AccumulationRegister", "Продажи")
    _object(root, "Documents", "Document", "Док")
    _module(
        root,
        "Documents",
        "Док",
        "ObjectModule",
        "Процедура ОбработкаПроведения(Отказ, Режим)\n"
        "\tДвижения.Продажи.Добавить();\n"
        "\tДвижения.Сумма = 0;\n"
        "\tДвижения.Период = Дата(2026, 1, 1);\n"
        "КонецПроцедуры\n",
    )
    _module(
        root,
        "Documents",
        "Док",
        "ManagerModule",
        'Функция ТекстЗапросаТаблицаСумма(Запрос)\n\tВозврат "";\nКонецФункции\n',
    )


class TestKnownRegisterMovementRows:
    def test_index_live_profile_and_flow_drop_non_register_code(self, tmp_path):
        root = tmp_path / "cf"
        _movement_config(root)
        reader = _reader(root, tmp_path / "idx")
        try:
            sessions = ((_bsl(root, reader), True), (_bsl(root), False))
            for bsl, indexed in sessions:
                result = bsl["find_register_movements"]("Док")
                assert [r["name"] for r in result["code_registers"]] == ["Продажи"]
                assert result["manager_tables"] == ["Сумма"]
                assert result["undeclared_code_registers"] == ["Продажи"]

                if indexed:
                    profile = bsl["get_object_profile"]("Док", sections=["registers"])
                    section = profile["sections"]["registers"]
                    assert [(r["register"], r["source"]) for r in section["items"]] == [
                        ("Сумма", "manager_table"),
                        ("Продажи", "code"),
                    ]
                    assert section["summary"]["code_registers"] == 1

                flow = bsl["analyze_document_flow"]("Док")
                assert [r["name"] for r in flow["register_movements"]["code_registers"]] == ["Продажи"]

                with pytest.raises(ValueError, match="регистра с именем 'Период' нет"):
                    bsl["find_register_writers"]("Период")
                assert bsl["find_register_writers"]("Продажи")["total_writers"] == 1
        finally:
            reader.close()

    def test_live_reverse_lookup_matches_full_register_name(self, tmp_path):
        root = tmp_path / "cf"
        _config(root)
        for name in ("Продажи", "ПродажиПодытог"):
            _object(root, "AccumulationRegisters", "AccumulationRegister", name)
        _object(root, "Documents", "Document", "ДокПоле")
        _module(
            root,
            "Documents",
            "ДокПоле",
            "ObjectModule",
            "Процедура ОбработкаПроведения(Отказ, Режим)\n\tДвижения.ПродажиПодытог.Добавить();\nКонецПроцедуры\n",
        )
        bsl = _bsl(root)
        assert bsl["find_register_writers"]("Продажи")["writers"] == []
        assert [r["name"] for r in bsl["find_register_movements"]("ДокПоле")["code_registers"]] == ["ПродажиПодытог"]

    def test_wrapper_uses_actual_configuration_root(self, tmp_path):
        base = tmp_path / "container"
        root = base / "ExtWrapped"
        _movement_config(root)
        reader = _reader(base, tmp_path / "idx")
        try:
            for bsl in (_bsl(base, reader, root=root), _bsl(base, root=root)):
                result = bsl["find_register_movements"]("Док")
                assert [r["name"] for r in result["code_registers"]] == ["Продажи"]
        finally:
            reader.close()

    def test_main_and_cfe_rows_use_their_own_code_roots(self, tmp_path):
        main = tmp_path / "main"
        cfe = tmp_path / "cfe" / "Расш1"
        _config(main)
        _config(cfe, extension=True)
        _object(main, "Documents", "Document", "ДокMain")
        _module(
            main,
            "Documents",
            "ДокMain",
            "ObjectModule",
            "Процедура ОбработкаПроведения(Отказ, Режим)\n\tДвижения.РасшРег.Добавить();\nКонецПроцедуры\n",
        )
        _object(cfe, "AccumulationRegisters", "AccumulationRegister", "РасшРег")
        _object(cfe, "Documents", "Document", "ДокCfe")
        _module(
            cfe,
            "Documents",
            "ДокCfe",
            "ObjectModule",
            "Процедура ОбработкаПроведения(Отказ, Режим)\n"
            "\tДвижения.РасшРег.Добавить();\n"
            "\tДвижения.Сумма = 0;\n"
            "КонецПроцедуры\n",
        )
        bsl = _bsl(main, extensions=[cfe])
        assert bsl["find_register_movements"]("ДокMain")["code_registers"] == []
        assert [r["name"] for r in bsl["find_register_movements"]("ДокCfe")["code_registers"]] == ["РасшРег"]

    def test_extension_uses_known_main_but_unknown_main_is_not_a_negative_proof(self, tmp_path):
        main = tmp_path / "main"
        cfe = tmp_path / "cfe" / "Расш1"
        _config(main)
        _config(cfe, extension=True)
        _object(main, "AccumulationRegisters", "AccumulationRegister", "Продажи")
        _object(cfe, "Documents", "Document", "ДокCfe")
        body = (
            "Процедура ОбработкаПроведения(Отказ, Режим)\n"
            "\tДвижения.Продажи.Добавить();\n"
            "\tДвижения.ВнешнийРегистр.Добавить();\n"
            "КонецПроцедуры\n"
        )
        _module(cfe, "Documents", "ДокCfe", "ObjectModule", body)
        with_main = _bsl(cfe, role="extension", main_root=main)
        assert [r["name"] for r in with_main["find_register_movements"]("ДокCfe")["code_registers"]] == ["Продажи"]
        without_main = _bsl(cfe, role="extension")
        assert {r["name"] for r in without_main["find_register_movements"]("ДокCfe")["code_registers"]} == {
            "Продажи",
            "ВнешнийРегистр",
        }


def _edt_config(root: Path) -> None:
    _write(
        root / "Configuration" / "Configuration.mdo",
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<mdclass:Configuration xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass" name="Тест"/>\n',
    )


def _exchange_plan(root: Path, fmt: str, name: str, refs: tuple[str, ...]) -> None:
    if fmt == "cf":
        _object(root, "ExchangePlans", "ExchangePlan", name)
        items = "".join(f"<Item><Metadata>{ref}</Metadata><AutoRecord>Allow</AutoRecord></Item>" for ref in refs)
        _write(
            root / "ExchangePlans" / name / "Ext" / "Content.xml",
            f'<?xml version="1.0" encoding="UTF-8"?>\n<ExchangePlanContent>{items}</ExchangePlanContent>',
        )
        return
    content = "".join(f"<content><mdObject>{ref}</mdObject><autoRecord>Allow</autoRecord></content>" for ref in refs)
    _write(
        root / "ExchangePlans" / name / f"{name}.mdo",
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<mdclass:ExchangePlan xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass">'
        f"<name>{name}</name>{content}</mdclass:ExchangePlan>",
    )


def _exchange_root(root: Path, fmt: str) -> None:
    (_config if fmt == "cf" else _edt_config)(root)
    _exchange_plan(root, fmt, "Полный", ("Catalog.Первый", "Document.Общий"))
    _exchange_plan(root, fmt, "тст_Полный", ("Catalog.Второй",))
    _exchange_plan(root, fmt, "УдалитьПолный", ())


class TestExchangePlanExactIdentity:
    @pytest.mark.parametrize("fmt", ["cf", "edt"])
    def test_exact_names_and_refusals(self, tmp_path, fmt):
        root = tmp_path / fmt
        _exchange_root(root, fmt)
        reader = _reader(root, tmp_path / "idx")
        try:
            for bsl in (_bsl(root), _bsl(root, reader)):
                expected = [
                    {"ref": "Catalog.Первый", "auto_record": True},
                    {"ref": "Document.Общий", "auto_record": True},
                ]
                for arg in ("Полный", "ПОЛНЫЙ", "ПланОбмена.ПОЛНЫЙ", "ExchangePlans/ПОЛНЫЙ"):
                    assert bsl["find_exchange_plan_content"](arg) == expected, arg
                assert bsl["find_exchange_plan_content"]("тст_Полный") == [
                    {"ref": "Catalog.Второй", "auto_record": True}
                ]
                assert bsl["find_exchange_plan_content"]("УдалитьПолный") == []
                for arg in ("Полн", "Ext", "Content"):
                    with pytest.raises(ValueError, match="find_exchange_plan_content") as exc:
                        bsl["find_exchange_plan_content"](arg)
                    assert "проверьте имя:" in str(exc.value)
                with pytest.raises(ValueError, match="маска"):
                    bsl["find_exchange_plan_content"]("*Полный")
        finally:
            reader.close()

    @pytest.mark.parametrize("prefix", ["ExtWrapped", "Ext[Wrapped]"])
    def test_literal_wrapper_path_with_and_without_index(self, tmp_path, prefix):
        base = tmp_path / "container"
        root = base / prefix
        _config(root)
        _exchange_plan(root, "cf", "Обмен", ("Catalog.Спр",))
        reader = _reader(base, tmp_path / "idx")
        try:
            for bsl in (_bsl(base, root=root), _bsl(base, reader, root=root)):
                assert bsl["find_exchange_plan_content"]("Обмен") == [{"ref": "Catalog.Спр", "auto_record": True}]
        finally:
            reader.close()

    def test_other_non_refs_keep_their_legacy_route(self, tmp_path):
        root = tmp_path / "cf"
        _exchange_root(root, "cf")
        for bsl in (_bsl(root), _bsl(root, _reader(root, tmp_path / "idx"))):
            for arg in ("Полный/Ext", "ПланОбмена.Полный/Ext"):
                assert bsl["find_exchange_plan_content"](arg) == [
                    {"ref": "Catalog.Первый", "auto_record": True},
                    {"ref": "Document.Общий", "auto_record": True},
                ]

    @pytest.mark.parametrize("fmt", ["cf", "edt"])
    def test_glob_refusal_gives_an_exact_call_without_synonyms(self, tmp_path, fmt):
        root = tmp_path / fmt
        _exchange_root(root, fmt)
        reader = _reader(root, tmp_path / "idx", synonyms=False)
        try:
            for bsl in (_bsl(root), _bsl(root, reader)):
                for mask in ("*", "[Пп]олный", "*Полный", "НетТакогоПлана?"):
                    with pytest.raises(ValueError, match="маска") as exc:
                        bsl["find_exchange_plan_content"](mask)
                    assert "find_exchange_plan_content('ExchangePlans/Полный')" in str(exc.value)
                assert bsl["find_exchange_plan_content"]("ExchangePlans/Полный") == [
                    {"ref": "Catalog.Первый", "auto_record": True},
                    {"ref": "Document.Общий", "auto_record": True},
                ]
        finally:
            reader.close()

    def test_glob_refusal_in_empty_catalog_gives_a_file_search(self, tmp_path):
        root = tmp_path / "cf"
        _config(root)
        with pytest.raises(ValueError, match="маска") as exc:
            _bsl(root)["find_exchange_plan_content"]("*")
        assert "find_files('ExchangePlans/')" in str(exc.value)
        helpers, _resolve_safe = make_helpers(str(root))
        assert helpers["find_files"]("ExchangePlans/") == []

    def test_other_root_returns_empty_and_refusal_names_its_session(self, tmp_path):
        main = tmp_path / "main"
        cfe = tmp_path / "cfe" / "Расш1"
        _config(main)
        _config(cfe, extension=True)
        _exchange_plan(main, "cf", "Основной", ("Catalog.Main",))
        _exchange_plan(cfe, "cf", "Расш1_Обмен", ("Catalog.Ext",))
        main_bsl = _bsl(main, extensions=[cfe])
        ext_bsl = _bsl(cfe, role="extension", main_root=main)
        assert main_bsl["find_exchange_plan_content"]("Расш1_Обмен") == []
        assert ext_bsl["find_exchange_plan_content"]("Основной") == []
        assert ext_bsl["find_exchange_plan_content"]("Расш1_Обмен") == [{"ref": "Catalog.Ext", "auto_record": True}]
        for arg in ("Расш1_Об", "[Рр]асш1_Об*"):
            with pytest.raises(ValueError) as exc:
                main_bsl["find_exchange_plan_content"](arg)
            text = str(exc.value)
            assert f"rlm_start(path={str(cfe)!r})" in text
            assert "find_exchange_plan_content('ExchangePlans/Расш1_Обмен')" in text


class TestBareObjectRefusal:
    @pytest.mark.parametrize("indexed", [False, True])
    def test_nine_helpers_refuse_missing_bare_name(self, tmp_path, indexed):
        root = tmp_path / "cf"
        _config(root)
        _object(root, "Documents", "Document", "Док")
        _object(root, "Catalogs", "Catalog", "Спр")
        reader = _reader(root, tmp_path / "idx") if indexed else None
        try:
            bsl = _bsl(root, reader)
            missing = "НетТакогоОбъекта"
            calls = {
                "find_print_forms": lambda: bsl["find_print_forms"](missing),
                "find_based_on_documents": lambda: bsl["find_based_on_documents"](missing),
                "find_roles": lambda: bsl["find_roles"](missing),
                "find_event_subscriptions": lambda: bsl["find_event_subscriptions"](missing),
                "find_functional_options": lambda: bsl["find_functional_options"](missing, include_code=False),
                "find_attributes": lambda: bsl["find_attributes"](object_name=missing),
                "find_predefined": lambda: bsl["find_predefined"](object_name=missing),
                "find_templates": lambda: bsl["find_templates"](owner=missing),
                "parse_form": lambda: bsl["parse_form"](missing),
            }
            for helper, call in calls.items():
                with pytest.raises(ValueError, match=helper) as exc:
                    call()
                assert "проверьте имя:" in str(exc.value)
        finally:
            if reader is not None:
                reader.close()

    def test_existing_objects_keep_legal_empty_answers(self, tmp_path):
        root = tmp_path / "cf"
        _config(root)
        _object(root, "Documents", "Document", "Док")
        _object(root, "Catalogs", "Catalog", "Спр")
        _object(root, "Reports", "Report", "Отчет")
        bsl = _bsl(root)
        calls = (
            lambda: bsl["find_print_forms"]("Док"),
            lambda: bsl["find_based_on_documents"]("Док"),
            lambda: bsl["find_roles"]("Док"),
            lambda: bsl["find_event_subscriptions"]("Док"),
            lambda: bsl["find_functional_options"]("Док", include_code=False),
            lambda: bsl["find_attributes"](object_name="Док"),
            lambda: bsl["find_predefined"](object_name="Спр"),
            lambda: bsl["find_templates"](owner="Док"),
            lambda: bsl["parse_form"]("Док"),
            lambda: bsl["parse_form"]("Док", form_name="НетТакойФормы"),
            lambda: bsl["find_print_forms"]("Отчет"),
        )
        for call in calls:
            call()
        for call in (
            lambda: bsl["find_based_on_documents"]("Отчет"),
            lambda: bsl["find_predefined"](object_name="Отчет"),
        ):
            with pytest.raises(ValueError, match="Reports") as exc:
                call()
            assert "parse_object_xml(" in str(exc.value)


def _print_proc(identifier: str) -> str:
    return (
        "Процедура ДобавитьКомандыПечати(КомандыПечати) Экспорт\n"
        "\tК = КомандыПечати.Добавить();\n"
        f'\tК.Идентификатор = "{identifier}";\n'
        f"\tК.Представление = НСтр(\"ru = '{identifier}'\");\n"
        "КонецПроцедуры\n"
    )


def _based_proc(identifier: str) -> str:
    return (
        "Процедура ДобавитьКомандыСозданияНаОсновании(Команды) Экспорт\n"
        f"\tДокументы.{identifier}.ДобавитьКомандуСозданияНаОсновании(Команды);\n"
        "КонецПроцедуры\n"
    )


class TestNoFragmentIdentityMerge:
    def test_print_based_on_and_live_movements_refuse_fragment(self, tmp_path):
        root = tmp_path / "cf"
        _config(root)
        _object(root, "AccumulationRegisters", "AccumulationRegister", "Продажи")
        for name, suffix in (("РеализацияТоваров", "Товары"), ("РеализацияУслуг", "Услуги")):
            _object(root, "Documents", "Document", name)
            _module(root, "Documents", name, "ManagerModule", _print_proc(suffix) + _based_proc(f"Акт{suffix}"))
            _module(
                root,
                "Documents",
                name,
                "ObjectModule",
                "Процедура ОбработкаПроведения(Отказ, Режим)\n\tДвижения.Продажи.Добавить();\nКонецПроцедуры\n",
            )
        bsl = _bsl(root)
        for helper in ("find_print_forms", "find_based_on_documents", "find_register_movements"):
            with pytest.raises(ValueError, match=helper) as exc:
                bsl[helper]("Реализация")
            assert "проверьте имя:" in str(exc.value)
        assert [r["name"] for r in bsl["find_print_forms"]("РеализацияТоваров")["print_forms"]] == ["Товары"]
        assert bsl["find_register_movements"].__wrapped__("Реализация")["code_registers"]

    @pytest.mark.parametrize("indexed", [False, True])
    def test_print_with_exact_manager_module_skips_metadata_scan(self, tmp_path, caplog, indexed):
        root = tmp_path / "cf"
        _config(root)
        _object(root, "Documents", "Document", "Заказ")
        _module(root, "Documents", "Заказ", "ManagerModule", _print_proc("ПечатьЗаказа"))
        reader = _reader(root, tmp_path / "idx") if indexed else None
        try:
            bsl = _bsl(root, reader)
            with caplog.at_level("INFO", logger="rlm_tools_bsl.helpers"):
                result = bsl["find_print_forms"]("Заказ")
            assert [row["name"] for row in result["print_forms"]] == ["ПечатьЗаказа"]
            metadata_globs = [msg for msg in caplog.messages if "glob_files: FS fallback pattern=Enums/Заказ/" in msg]
            assert metadata_globs == []
        finally:
            if reader is not None:
                reader.close()

    def test_exact_document_without_modules_does_not_borrow_neighbor(self, tmp_path):
        root = tmp_path / "cf"
        _config(root)
        _object(root, "AccumulationRegisters", "AccumulationRegister", "Продажи")
        _object(root, "Documents", "Document", "Заказ")
        _object(root, "Documents", "Document", "ЗаказПокупателя")
        _module(
            root,
            "Documents",
            "ЗаказПокупателя",
            "ObjectModule",
            "Процедура ОбработкаПроведения(Отказ, Режим)\n\tДвижения.Продажи.Добавить();\nКонецПроцедуры\n",
        )
        bsl = _bsl(root)
        for arg in ("Заказ", "Документ.Заказ"):
            result = bsl["find_register_movements"](arg)
            assert result["code_registers"] == []
            assert "ObjectModule" in result["error"]

    def test_exact_moduless_object_does_not_borrow_print_forms(self, tmp_path):
        root = tmp_path / "cf"
        _config(root)
        _object(root, "CommonPictures", "CommonPicture", "Бланк")
        _object(root, "Documents", "Document", "БланкЗаказа")
        _module(root, "Documents", "БланкЗаказа", "ManagerModule", _print_proc("Чужая"))
        for bsl in (_bsl(root), _bsl(root, _reader(root, tmp_path / "idx"))):
            for arg in ("Бланк", "CommonPicture.Бланк"):
                assert bsl["find_print_forms"](arg)["print_forms"] == []

    def test_main_only_object_gets_empty_print_and_main_route(self, tmp_path):
        main = tmp_path / "main"
        cfe = tmp_path / "cfe" / "Расш1"
        _config(main)
        _config(cfe, extension=True)
        _object(main, "Documents", "Document", "Заказ")
        _object(cfe, "Documents", "Document", "ЗаказДополнительный")
        _module(cfe, "Documents", "ЗаказДополнительный", "ManagerModule", _print_proc("Чужая"))
        result = _bsl(cfe, role="extension", main_root=main)["find_print_forms"]("Заказ")
        assert result["print_forms"] == []
        assert f"rlm_start(path={str(main)!r})" in result["hint"]


class TestKnownRegisterMovementEdges:
    def test_false_code_field_does_not_suppress_posting_handler(self, tmp_path):
        root = tmp_path / "cf"
        _config(root)
        _object(root, "Documents", "Document", "ДокПоля")
        _module(
            root,
            "Documents",
            "ДокПоля",
            "ObjectModule",
            "Процедура ОбработкаПроведения(Отказ, Режим)\n"
            "\tДвижения.Сумма = 0;\n"
            "\tДвижения.Период = Дата(2026, 1, 1);\n"
            "КонецПроцедуры\n",
        )
        reader = _reader(root, tmp_path / "idx")
        try:
            for bsl in (_bsl(root), _bsl(root, reader)):
                result = bsl["find_register_movements"]("ДокПоля")
                assert result["code_registers"] == []
                assert result["posting_handler_present"] is True
        finally:
            reader.close()

    def test_profile_replaces_false_index_row_with_valid_live_bridge(self, tmp_path):
        root = tmp_path / "cf"
        _config(root)
        _object(root, "AccumulationRegisters", "AccumulationRegister", "Продажи")
        _object(root, "Documents", "Document", "Док")
        _module(
            root,
            "Documents",
            "Док",
            "ObjectModule",
            "Процедура ОбработкаПроведения(Отказ, Режим)\n"
            "\tДвижения.Сумма = 0;\n"
            "\tRegisterRecords.Продажи.Добавить();\n"
            "КонецПроцедуры\n",
        )
        reader = _reader(root, tmp_path / "idx")
        try:
            assert [(r["register_name"], r["source"]) for r in reader.get_register_movements("Док")] == [
                ("Сумма", "code")
            ]
            bsl = _bsl(root, reader)
            assert [r["name"] for r in bsl["find_register_movements"]("Док")["code_registers"]] == ["Продажи"]
            section = bsl["get_object_profile"]("Док", sections=["registers"])["sections"]["registers"]
            assert [(r["register"], r["source"]) for r in section["items"]] == [("Продажи", "code")]
        finally:
            reader.close()

    def test_live_bridge_is_filtered_when_document_has_no_index_rows(self, tmp_path):
        root = tmp_path / "cf"
        _config(root)
        _object(root, "AccumulationRegisters", "AccumulationRegister", "Продажи")
        for name in ("Другой", "Док"):
            _object(root, "Documents", "Document", name)
        _module(
            root,
            "Documents",
            "Другой",
            "ObjectModule",
            "Процедура ОбработкаПроведения(Отказ, Режим)\n\tДвижения.Продажи.Добавить();\nКонецПроцедуры\n",
        )
        _module(
            root,
            "Documents",
            "Док",
            "ObjectModule",
            "Процедура ОбработкаПроведения(Отказ, Режим)\n\tRegisterRecords.Сумма = 0;\nКонецПроцедуры\n",
        )
        reader = _reader(root, tmp_path / "idx")
        try:
            assert reader.get_register_movements("Док") == []
            bsl = _bsl(root, reader)
            assert bsl["find_register_movements"]("Док")["code_registers"] == []
            section = bsl["get_object_profile"]("Док", sections=["registers"])["sections"]["registers"]
            assert all(row["register"] != "Сумма" for row in section["items"])
        finally:
            reader.close()

    def test_main_register_delegate_stays_unresolved_in_extension(self, tmp_path):
        main = tmp_path / "main"
        cfe = tmp_path / "cfe" / "Расш1"
        _config(main)
        _config(cfe, extension=True)
        _object(main, "AccumulationRegisters", "AccumulationRegister", "Продажи")
        _object(cfe, "Documents", "Document", "Док")
        _module(
            cfe,
            "Documents",
            "Док",
            "ObjectModule",
            "Процедура ОбработкаПроведения(Отказ, Режим)\n"
            '\tПроведениеСервер.ОтразитьДвиженияПоРегистру(ДополнительныеСвойства, Движения, Отказ, "Продажи");\n'
            "КонецПроцедуры\n",
        )
        reader = _reader(cfe, tmp_path / "idx")
        try:
            result = _bsl(cfe, reader, role="extension", main_root=main)["find_register_movements"]("Док")
            assert result["code_registers"] == []
            assert [(r["kind"], r["evidence"]) for r in result["unresolved"]] == [("unknown_register", "Продажи")]
        finally:
            reader.close()


def test_exchange_fragment_without_known_main_keeps_previous_positive_answer(tmp_path):
    cfe = tmp_path / "cfe" / "Расш1"
    _config(cfe, extension=True)
    _exchange_plan(cfe, "cf", "Расш1_Обмен", ("Catalog.Ext",))
    assert _bsl(cfe, role="extension")["find_exchange_plan_content"]("Расш1_Об") == [
        {"ref": "Catalog.Ext", "auto_record": True}
    ]


def test_zero_limit_with_positive_total_is_not_an_empty_full_result(tmp_path):
    root = tmp_path / "cf"
    _config(root)
    missing = "НетТакогоОбъекта"
    _write(
        root / "EventSubscriptions" / "Подписка.xml",
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses" xmlns:v8="http://v8.1c.ru/8.1/data/core">'
        "<EventSubscription><Properties><Name>Подписка</Name>"
        f"<Source><v8:Type>cfg:DocumentObject.{missing}</v8:Type></Source>"
        "<Handler>CommonModule.М.Обработчик</Handler><Event>OnWrite</Event>"
        "</Properties></EventSubscription></MetaDataObject>",
    )
    _write(
        root / "FunctionalOptions" / "Опция.xml",
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses" '
        'xmlns:v8="http://v8.1c.ru/8.1/data/core" xmlns:xr="http://v8.1c.ru/8.3/xcf/readable">'
        "<FunctionalOption><Properties><Name>Опция</Name><Location>Constant.Опция</Location>"
        f"<Content><xr:Object>Document.{missing}</xr:Object></Content>"
        "</Properties></FunctionalOption></MetaDataObject>",
    )
    reader = _reader(root, tmp_path / "idx")
    try:
        import sqlite3

        with sqlite3.connect(reader._db_path) as conn:
            conn.execute(
                "INSERT INTO templates (owner_ref, name, synonym, template_type, file) VALUES (?, ?, ?, ?, ?)",
                (missing, "Макет", "Макет", "SpreadsheetDocument", "Documents/Нет/Templates/Макет.xml"),
            )
        bsl = _bsl(root, reader)
        subscriptions = bsl["find_event_subscriptions"](missing, limit=0)
        assert subscriptions["subscriptions"] == [] and subscriptions["total"] == 1
        options = bsl["find_functional_options"](missing, include_code=False, limit=0)
        assert options["xml_options"] == [] and options["total"] == 1
        templates = bsl["find_templates"](owner=missing, limit=0)
        assert templates["templates"] == [] and templates["total"] == 1
    finally:
        reader.close()


def test_exchange_refusal_without_synonyms_uses_disk_and_exact_call(tmp_path):
    root = tmp_path / "cf"
    _exchange_root(root, "cf")
    reader = _reader(root, tmp_path / "idx", synonyms=False)
    try:
        bsl = _bsl(root, reader)
        assert bsl["search_objects"]("Полн") == []
        with pytest.raises(ValueError) as exc:
            bsl["find_exchange_plan_content"]("Полн")
        text = str(exc.value)
        assert "похожие на диске — ExchangePlans/Полный" in text
        assert "find_exchange_plan_content('ExchangePlans/Полный')" in text
        assert bsl["find_exchange_plan_content"]("ExchangePlans/Полный") == [
            {"ref": "Catalog.Первый", "auto_record": True},
            {"ref": "Document.Общий", "auto_record": True},
        ]
    finally:
        reader.close()


def _main_only_neighbor_world(tmp_path: Path, indexed: bool):
    main = tmp_path / "main"
    cfe = tmp_path / "cfe" / "Расш1"
    _config(main)
    _config(cfe, extension=True)
    _object(main, "Documents", "Document", "Заказ")
    _object(cfe, "Documents", "Document", "ЗаказДополнительный")
    _module(
        cfe,
        "Documents",
        "ЗаказДополнительный",
        "ManagerModule",
        _print_proc("ПечатьСоседа") + _based_proc("Акт"),
    )
    reader = _reader(cfe, tmp_path / "idx") if indexed else None
    return main, cfe, reader, _bsl(cfe, reader, role="extension", main_root=main)


@pytest.mark.parametrize("indexed", [False, True])
def test_main_only_based_on_does_not_borrow_neighbor_for_exact_forms(tmp_path, indexed):
    main, _cfe, reader, bsl = _main_only_neighbor_world(tmp_path, indexed)
    try:
        for arg in ("Заказ", "Документ.Заказ", "Documents/Заказ.xml"):
            result = bsl["find_based_on_documents"](arg)
            assert result["can_create_from_here"] == [], arg
            assert result["can_be_created_from"] == [], arg
        assert f"rlm_start(path={str(main)!r})" in bsl["find_print_forms"]("Заказ")["hint"]
    finally:
        if reader is not None:
            reader.close()


@pytest.mark.parametrize("indexed", [False, True])
def test_main_only_print_does_not_borrow_neighbor_for_exact_references(tmp_path, indexed):
    _main, _cfe, reader, bsl = _main_only_neighbor_world(tmp_path, indexed)
    try:
        for arg in ("Заказ", "Документ.Заказ", "Documents/Заказ.xml"):
            assert bsl["find_print_forms"](arg)["print_forms"] == [], arg
    finally:
        if reader is not None:
            reader.close()


def test_index_writers_do_not_resolve_root_per_row(tmp_path, monkeypatch):
    import sqlite3

    root = tmp_path / "cf"
    _config(root)
    _object(root, "AccumulationRegisters", "AccumulationRegister", "Продажи")
    reader = _reader(root, tmp_path / "idx")
    try:
        with sqlite3.connect(reader._db_path) as conn:
            conn.executemany(
                "INSERT INTO register_movements (document_name, register_name, source, file) VALUES (?, ?, ?, ?)",
                [(f"Док{i}", "Продажи", "code", "Documents/Док/Ext/ObjectModule.bsl") for i in range(1200)],
            )
        bsl = _bsl(root, reader)
        assert bsl["find_register_writers"]("Продажи")["total_writers"] == 1200
        original_resolve = Path.resolve
        resolved = []

        def counted_resolve(self, *args, **kwargs):
            resolved.append(str(self))
            return original_resolve(self, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", counted_resolve)
        result = bsl["find_register_writers"]("Продажи")
        assert result["total_writers"] == 1200
        assert len(resolved) < 20, f"Path.resolve() вызван {len(resolved)} раз для 1200 строк"
    finally:
        reader.close()


@pytest.mark.parametrize("indexed", [False, True])
def test_main_only_basis_category_does_not_borrow_document_modules(tmp_path, indexed):
    main = tmp_path / "main"
    cfe = tmp_path / "cfe" / "Расш1"
    _config(main)
    _config(cfe, extension=True)
    _object(main, "Catalogs", "Catalog", "Договоры")
    _object(cfe, "Documents", "Document", "ДоговорыДополнительные")
    _module(cfe, "Documents", "ДоговорыДополнительные", "ManagerModule", _based_proc("Акт"))
    reader = _reader(cfe, tmp_path / "idx") if indexed else None
    try:
        bsl = _bsl(cfe, reader, role="extension", main_root=main)
        for arg in ("Договоры", "Справочник.Договоры"):
            result = bsl["find_based_on_documents"](arg)
            assert result["can_create_from_here"] == [], arg
            assert result["can_be_created_from"] == [], arg
        bare = bsl["find_based_on_documents"]("Договоры")
        assert bare["document"] == "Договоры"
        assert bare["_meta"]["resolved_ref"] == "Catalog.Договоры"
    finally:
        if reader is not None:
            reader.close()


@pytest.mark.parametrize("indexed", [False, True])
@pytest.mark.parametrize("arg", [r"Catalogs\Договоры", "src/Catalogs/Договоры"])
def test_non_object_name_paths_keep_previous_based_on_route(tmp_path, indexed, arg):
    main = tmp_path / "main"
    cfe = tmp_path / "cfe" / "Расш1"
    _config(main)
    _config(cfe, extension=True)
    _object(main, "Catalogs", "Catalog", "Договоры")
    _object(cfe, "Documents", "Document", "ДоговорыДополнительные")
    _module(cfe, "Documents", "ДоговорыДополнительные", "ManagerModule", _based_proc("Акт"))
    reader = _reader(cfe, tmp_path / "idx") if indexed else None
    try:
        result = _bsl(cfe, reader, role="extension", main_root=main)["find_based_on_documents"](arg)
        assert result["document"] == arg
        assert result["can_create_from_here"] == []
        assert result["can_be_created_from"] == []
        assert "resolved_ref" not in result.get("_meta", {})
    finally:
        if reader is not None:
            reader.close()
