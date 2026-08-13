"""v1.30.0: гарды limit/offset-подобных параметров на границе хелперов.

Что доказывают эти тесты и почему фикстура именно такая.

Дефект: `limit=None` (и прочий мусор) уезжал в `LIMIT ? OFFSET ?` ридера, где
SQLite требует целое — получался `IntegrityError: datatype mismatch`, а часть
хелперов падала раньше на арифметике `offset + limit`. Отдельно `limit=-1` в
SQLite означает «без ограничения» и выдавал десятки тысяч строк в песочницу.

ДВА требования к фикстуре, без которых тесты вакуумны:

1. **Индекс должен быть РЕАЛЬНО ПОСТРОЕН.** Дефолтная `bsl_env` собирает хелперы
   без `idx_reader`, а все `IntegrityError` живут в индексном пути — на ней они
   не воспроизводятся вовсе (`find_definition` вернёт `{"error": "no index"}`).
2. **Данных должно быть БОЛЬШЕ дефолтного кэпа.** На фикстуре из трёх модулей
   проверка «`f(q, -1)` не больше `f(q)`» зелёная и ДО фикса (3 <= 3) и не
   доказывает ничего. Поэтому ниже 60 модулей: 240 областей против дефолта 200
   и 60 вызывающих против дефолта 50.
"""

import shutil
import subprocess

import pytest

from rlm_tools_bsl.bsl_index import IndexBuilder, IndexReader

MODULE_COUNT = 60
REGIONS_PER_MODULE = 4  # 60*4 = 240 > дефолт search_regions (200)


def _make_saturating_fixture(root):
    """60 общих модулей: >200 областей, >50 вызывающих одной процедуры."""
    for i in range(MODULE_COUNT):
        mod_dir = root / "CommonModules" / f"Модуль{i:03d}" / "Ext"
        mod_dir.mkdir(parents=True, exist_ok=True)
        body = ["// Служебный модуль подсистемы Тест", ""]
        for r in range(REGIONS_PER_MODULE):
            body += [
                f"#Область Служебные{r}",
                f"Процедура Обработать{i:03d}_{r}() Экспорт",
                "    ЦелеваяПроцедура();",
                "КонецПроцедуры",
                "#КонецОбласти",
                "",
            ]
        (mod_dir / "Module.bsl").write_text("\n".join(body), encoding="utf-8")

    tgt = root / "CommonModules" / "Цель" / "Ext"
    tgt.mkdir(parents=True, exist_ok=True)
    (tgt / "Module.bsl").write_text("Процедура ЦелеваяПроцедура() Экспорт\nКонецПроцедуры\n", encoding="utf-8")

    doc_dir = root / "Documents" / "ТестовыйДокумент" / "Ext"
    doc_dir.mkdir(parents=True, exist_ok=True)
    (doc_dir / "ObjectModule.bsl").write_text(
        "Процедура ОбработкаПроведения(Отказ, Режим)\nКонецПроцедуры\n", encoding="utf-8"
    )
    # Метаданные-XML: без них find_references_to_object и find_functional_options
    # отдают ноль, и любой тест «валидный вид продолжает фильтровать» превращается
    # в вакуумное 0 == 0. Форма тегов — та же, что в tests/test_find_references.py
    # (проверенная на боевых CF-конфигурациях).
    cat_dir = root / "Catalogs" / "ТестовыйСправочник" / "Ext"
    cat_dir.mkdir(parents=True, exist_ok=True)
    (cat_dir / "Catalog.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"\n'
        '                xmlns:v8="http://v8.1c.ru/8.1/data/core"\n'
        '                xmlns:xr="http://v8.1c.ru/8.3/xcf/readable"\n'
        '                xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">\n'
        "  <Catalog>\n"
        "    <Properties>\n"
        "      <Name>ТестовыйСправочник</Name>\n"
        "    </Properties>\n"
        "    <ChildObjects>\n"
        "      <Attribute>\n"
        "        <Properties>\n"
        "          <Name>Основание</Name>\n"
        "          <Type>\n"
        "            <v8:Type>DocumentRef.ТестовыйДокумент</v8:Type>\n"
        "          </Type>\n"
        "        </Properties>\n"
        "      </Attribute>\n"
        "    </ChildObjects>\n"
        "  </Catalog>\n"
        "</MetaDataObject>\n",
        encoding="utf-8",
    )
    fo_dir = root / "FunctionalOptions" / "ТестоваяОпция" / "Ext"
    fo_dir.mkdir(parents=True, exist_ok=True)
    (fo_dir / "FunctionalOption.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"\n'
        '                xmlns:xr="http://v8.1c.ru/8.3/xcf/readable"\n'
        '                xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">\n'
        "  <FunctionalOption>\n"
        "    <Properties>\n"
        "      <Name>ТестоваяОпция</Name>\n"
        "      <Content>\n"
        "        <xr:Object>Document.ТестовыйДокумент</xr:Object>\n"
        "      </Content>\n"
        "    </Properties>\n"
        "  </FunctionalOption>\n"
        "</MetaDataObject>\n",
        encoding="utf-8",
    )
    (root / "Configuration.xml").write_text("<Configuration/>", encoding="utf-8")
    return root


@pytest.fixture
def guarded_bsl(tmp_path, monkeypatch):
    """Хелперы поверх РЕАЛЬНО построенного индекса на насыщенной фикстуре."""
    from rlm_tools_bsl.bsl_helpers import make_bsl_helpers

    project = _make_saturating_fixture(tmp_path)
    monkeypatch.setenv("RLM_INDEX_DIR", str(project / ".index"))
    db_path = IndexBuilder().build(str(project), build_calls=True, build_metadata=True)
    reader = IndexReader(str(db_path))
    bsl = make_bsl_helpers(
        base_path=str(project),
        resolve_safe=lambda p: __import__("pathlib").Path(project) / p,
        read_file_fn=lambda p: (__import__("pathlib").Path(project) / p).read_text(encoding="utf-8"),
        grep_fn=lambda pat, path="": [],
        glob_files_fn=lambda pat: [],
        idx_reader=reader,
    )
    yield bsl
    reader.close()


# ── фикстура обязана быть насыщенной, иначе всё ниже вакуумно ────────


def test_fixture_actually_saturates_caps(guarded_bsl):
    """Мета-тест: если он покраснеет, остальные перестают что-либо доказывать."""
    assert guarded_bsl["search_regions"]("Служебные", limit=10_000).__len__() > 200
    ctx = guarded_bsl["find_callers_context"]("ЦелеваяПроцедура", "", 0, 10_000)
    assert ctx["_meta"]["total_callers"] > 50


# ── 1. не падать на None ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda b: b["find_callers_context"]("ЦелеваяПроцедура", "", 0, None), id="find_callers_context"),
        pytest.param(lambda b: b["find_definition"]("ЦелеваяПроцедура", "", None), id="find_definition"),
        pytest.param(lambda b: b["find_code_usages"]("Документ.ТестовыйДокумент", None, None), id="find_code_usages"),
        pytest.param(lambda b: b["search_regions"]("Служебные", None), id="search_regions"),
        pytest.param(lambda b: b["search_module_headers"]("подсистема", None), id="search_module_headers"),
        pytest.param(lambda b: b["search_methods"]("Обработать", None), id="search_methods"),
        pytest.param(lambda b: b["search_objects"]("Тест", None), id="search_objects"),
        pytest.param(lambda b: b["search"]("Обработать", "all", None), id="search_all"),
        pytest.param(lambda b: b["search"]("Обработать", "methods", None), id="search_methods_scope"),
        pytest.param(lambda b: b["find_attributes"]("Организация", "", "", "", None), id="find_attributes"),
        pytest.param(lambda b: b["find_predefined"]("", "", None), id="find_predefined"),
        pytest.param(lambda b: b["find_callers"]("ЦелеваяПроцедура", "", None), id="find_callers"),
        pytest.param(lambda b: b["safe_grep"]("ЦелеваяПроцедура", "", None), id="safe_grep"),
    ],
)
def test_limit_none_does_not_raise(guarded_bsl, call):
    """Красный до фикса: IntegrityError / TypeError.

    ВНЕ параметризации намеренно оставлены три хелпера, у которых дефект НЕ
    является исключением и такой тест был бы зелёным по конструкции:
    get_object_profile (молча деградирует), find_references_to_object (уходит в
    live-скан), find_register_movements (ветка не достигается). Для них —
    предметные тесты ниже.
    """
    call(guarded_bsl)  # не должно бросить


def test_offset_none_does_not_raise(guarded_bsl):
    guarded_bsl["find_callers_context"]("ЦелеваяПроцедура", "", None, 50)


# ── 2-3. дефолт восстановлен, отрицательное не безлимитно ────────────


def test_limit_none_uses_documented_default(guarded_bsl):
    assert len(guarded_bsl["search_regions"]("Служебные", None)) == 200
    assert len(guarded_bsl["find_callers_context"]("ЦелеваяПроцедура", "", 0, None)["callers"]) == 50


def test_limit_negative_is_not_unbounded(guarded_bsl):
    """Красный до фикса: сегодня `LIMIT -1` = «отдать всё» (240 против 200)."""
    assert len(guarded_bsl["search_regions"]("Служебные", -1)) == 200
    assert len(guarded_bsl["find_callers_context"]("ЦелеваяПроцедура", "", 0, -1)["callers"]) == 50


# ── 4-5. тихие режимы: деградация и live-fallback ────────────────────


def test_get_object_profile_none_does_not_degrade_sections(guarded_bsl):
    """Красный до фикса: `int(None)` в каждой секции ловится посекционно и
    записывается как status='error' — наружу уходит внешне валидный профиль без
    единой заполненной секции, который агент читает как «данных нет»."""
    prof = guarded_bsl["get_object_profile"]("ТестовыйДокумент", limit=None)
    statuses = {k: v.get("status") for k, v in prof["sections"].items()}
    assert "error" not in statuses.values(), statuses


def test_references_none_does_not_fall_back_to_live(guarded_bsl):
    """Красный до фикса: индексный запрос падал, голый `except Exception` его
    глушил, и управление уходило в полный FS-скан → partial=True."""
    res = guarded_bsl["find_references_to_object"]("Документ.ТестовыйДокумент", None, None)
    assert res["partial"] is False


def test_code_usages_none_does_not_fall_back_to_live(guarded_bsl):
    res = guarded_bsl["find_code_usages"]("Документ.ТестовыйДокумент", None, None)
    assert res["partial"] is False


# ── 6. предупреждение в _meta — только у тех, у кого _meta есть ──────


def test_arg_warning_in_meta(guarded_bsl):
    ctx = guarded_bsl["find_callers_context"]("ЦелеваяПроцедура", "", 0, None)
    warn = ctx["_meta"].get("arg_warning", "")
    assert "limit" in warn and "find_callers_context" in warn

    d = guarded_bsl["find_definition"]("ЦелеваяПроцедура", "", None)
    assert "limit" in d["_meta"].get("arg_warning", "")

    prof = guarded_bsl["get_object_profile"]("ТестовыйДокумент", limit=None)
    assert "limit" in prof["_meta"].get("arg_warning", "")

    cu = guarded_bsl["find_code_usages"]("Документ.ТестовыйДокумент", None, None)
    assert "limit" in cu["_meta"].get("arg_warning", "")


def test_valid_args_leave_no_warning(guarded_bsl):
    """Гард не должен шуметь на корректных значениях."""
    ctx = guarded_bsl["find_callers_context"]("ЦелеваяПроцедура", "", 0, 10)
    assert "arg_warning" not in ctx["_meta"]


# ── 7. семейство B: None там ДОКУМЕНТИРОВАН как «без ограничения» ────


def test_family_b_none_still_unlimited(guarded_bsl):
    """Регресс-гард: механическое применение гарда ко всем хелперам с `limit`
    сломало бы публичный контракт этих двух. Зелёный ДО и ПОСЛЕ."""
    subs = guarded_bsl["find_event_subscriptions"]("ТестовыйДокумент", False, None, None)
    assert isinstance(subs, list)  # без limit — плоский список, не пагинированный dict

    fo = guarded_bsl["find_functional_options"]("ТестовыйДокумент", False, None)
    assert set(fo) >= {"xml_options", "code_options"}
    assert "total" not in fo  # limit=None → без per-bucket cap, а не усечение


def test_safe_grep_max_files_none_is_normalized(guarded_bsl, caplog):
    """Тихий класс дефекта: `safe_grep` не падал, а срез `candidates[:max_files]`
    при None означал «весь каталог» — заявленный кэп молча превращался в полный
    обход.

    Проверяем СРАБАТЫВАНИЕ ГАРДА по логу, а не число найденных строк. В этой
    фикстуре `safe_grep` совпадений не возвращает (при заданном idx_reader живой
    каталог собирается обходом ФС, а не из `_index_state`), поэтому ассерт на
    количество был бы вакуумным — зелёным и ДО фикса. Применение самого среза
    `[:max_files]` — существующий код; новым здесь является только нормализация
    значения, её и проверяем.
    """
    with caplog.at_level("WARNING"):
        guarded_bsl["safe_grep"]("ЦелеваяПроцедура", "", None)
    fired = [r.getMessage() for r in caplog.records if "max_files" in r.getMessage()]
    assert fired and "20" in fired[0], caplog.records


def test_safe_grep_result_cap_none_untouched(guarded_bsl, caplog):
    """`_result_cap=None` — второй параметр той же функции, намеренное «без cap»
    для внутренних исчерпывающих сканов. Гард обязан его НЕ трогать: при валидном
    `max_files` по нему не должно быть НИ ОДНОГО предупреждения."""
    with caplog.at_level("WARNING"):
        rows = guarded_bsl["safe_grep"]("ЦелеваяПроцедура", "", 5, _result_cap=None)
    assert isinstance(rows, list)
    assert not [r for r in caplog.records if "_result_cap" in r.getMessage()]


# ── 8. замороженный контракт count_only не должен протечь ────────────


def test_count_only_payload_unchanged(guarded_bsl):
    """Гард пишет предупреждение в лог, а не в payload: четырёхключевой dict
    закреплён byte-for-byte и в docstring, и в тестах."""
    for name in ("search_regions", "search_module_headers"):
        res = guarded_bsl[name]("Служебные", None, count_only=True)
        assert set(res) == {"total", "source", "truncated", "scope"}


# ── 9-10. мусорные типы и list-перегрузка ────────────────────────────


@pytest.mark.parametrize("garbage", ["50", [], {}, float("nan")])
def test_garbage_types_fall_back_to_default(guarded_bsl, garbage):
    assert len(guarded_bsl["search_regions"]("Служебные", garbage)) == 200


def test_float_is_truncated_not_rejected(guarded_bsl):
    """Осознанное решение, а не недосмотр: дробное усекается до целого — ровно
    так же, как это давно делает принятый `int(depth)` в find_call_hierarchy.
    Откат к дефолту здесь был бы менее предсказуем для вызывающего."""
    assert len(guarded_bsl["search_regions"]("Служебные", 2.5)) == 2


def test_bool_is_not_silently_treated_as_int(guarded_bsl):
    """bool — подкласс int: `True` без явной проверки прошёл бы как limit=1."""
    assert len(guarded_bsl["search_regions"]("Служебные", True)) == 200


@pytest.mark.parametrize(
    "call,rows",
    [
        pytest.param(
            lambda b: b["find_callers_context"]("ЦелеваяПроцедура", "", 0, 0), lambda r: r["callers"], id="callers_ctx"
        ),
        pytest.param(
            lambda b: b["find_definition"]("ЦелеваяПроцедура", "", 0), lambda r: r["definitions"], id="find_definition"
        ),
        pytest.param(
            lambda b: b["find_references_to_object"]("Документ.ТестовыйДокумент", None, 0),
            lambda r: r["references"],
            id="references",
        ),
        pytest.param(
            lambda b: b["find_code_usages"]("Документ.ТестовыйДокумент", None, 0),
            lambda r: r["usages"],
            id="code_usages",
        ),
        pytest.param(lambda b: b["find_callers"]("ЦелеваяПроцедура", "", 0), list, id="find_callers"),
        pytest.param(lambda b: b["safe_grep"]("ЦелеваяПроцедура", "", 0), list, id="safe_grep"),
        pytest.param(lambda b: b["search_regions"]("Служебные", 0), list, id="search_regions"),
        pytest.param(lambda b: b["search_methods"]("Обработать", 0), list, id="search_methods"),
    ],
)
def test_zero_is_honoured_everywhere(guarded_bsl, caplog, call, rows):
    """`0` — валидный запрос «ничего не возвращать», а не мусор.

    Регресс из ревью: часть точек была объявлена с `minimum=1`, и нулевой бюджет
    молча подменялся дефолтом — вызывающий получал полную страницу (а `safe_grep`
    ещё и скан) вместо пустоты. Для динамически вычисленного лимита это худший
    вид сюрприза: тихий и противоположный намерению.

    Ключевой ассерт здесь — ОТСУТСТВИЕ предупреждения, а не пустота выдачи.
    Пустота у части хелперов достигается и без данных (фикстура не насыщает
    ссылки/код-обращения, а `grep_fn` заглушен), поэтому сама по себе она ничего
    не доказывала бы. Сработавший гард всегда оставляет след — в `_meta` либо в
    логе, — и именно его отсутствие означает, что `0` дошёл до хелпера как есть.
    """
    with caplog.at_level("WARNING"):
        res = call(guarded_bsl)
    assert rows(res) == []
    meta = res.get("_meta", {}) if isinstance(res, dict) else {}
    assert "arg_warning" not in meta, meta
    assert not [r for r in caplog.records if "arg-guard" in r.getMessage()], caplog.records


@pytest.mark.skipif(not shutil.which("git"), reason="git недоступен")
def test_git_search_max_results_guarded(tmp_path, monkeypatch):
    """`git_search` — opt-in хелпер, он вообще не регистрируется вне git-репозитория,
    поэтому основной фикстурой не покрывается и нуждается в своей.

    Проверяем обе стороны контракта: `max_results=0` проходит без предупреждения
    (валидный запрос «ничего»), а `None` — нормализуется к дефолту 200.
    """
    from rlm_tools_bsl.bsl_helpers import make_bsl_helpers

    project = _make_saturating_fixture(tmp_path)
    for cmd in (["init", "-q"], ["add", "-A"], ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"]):
        subprocess.run(["git", *cmd], cwd=project, check=True, capture_output=True)

    monkeypatch.setenv("RLM_INDEX_DIR", str(project / ".index"))
    db_path = IndexBuilder().build(str(project), build_calls=False, build_metadata=False)
    reader = IndexReader(str(db_path))
    bsl = make_bsl_helpers(
        base_path=str(project),
        resolve_safe=lambda p: __import__("pathlib").Path(project) / p,
        read_file_fn=lambda p: (__import__("pathlib").Path(project) / p).read_text(encoding="utf-8"),
        grep_fn=lambda pat, path="": [],
        glob_files_fn=lambda pat: [],
        idx_reader=reader,
    )
    try:
        assert "git_search" in bsl, "фикстура не под git — тест выродился бы в пустышку"
        # У git_search свой исторический контракт: при усечении он добавляет
        # последним элементом sentinel {_truncated, shown}. Ноль уважён — реальных
        # строк нет, а `shown: 0` это подтверждает.
        zero = bsl["git_search"]("ЦелеваяПроцедура", max_results=0)
        assert [r for r in zero if "_truncated" not in r] == [], zero
        assert zero and zero[-1]["shown"] == 0, zero
        assert len([r for r in bsl["git_search"]("ЦелеваяПроцедура", max_results=None) if "_truncated" not in r]) <= 200
    finally:
        reader.close()


def test_both_bound_warnings_are_reported(guarded_bsl):
    """Регресс из ревью: `_w_off or _w_lim` сохранял только ПЕРВОЕ предупреждение,
    и при двух битых аргументах пользователь узнавал лишь про offset.

    Проверять вхождение слов «offset» и «limit» НЕЛЬЗЯ: оба присутствуют в тексте
    одной лишь offset-ошибки, потому что туда подставляется полная сигнатура
    `find_callers_context(proc_name, module_hint, offset, limit)`. Такой ассерт
    был бы зелёным и до исправления. Считаем именно ДВА независимых вердикта.
    """
    ctx = guarded_bsl["find_callers_context"]("ЦелеваяПроцедура", "", None, None)
    warn = ctx["_meta"].get("arg_warning", "")
    assert "offset ожидался" in warn, warn
    assert "limit ожидался" in warn, warn
    assert warn.count("использован дефолт") == 2, warn


def test_profile_early_return_still_carries_warning(guarded_bsl):
    """Регресс из ревью: ранний возврат по нерезолвящемуся объекту нёс `_meta`,
    но не нёс предупреждение — а документация обещает его всюду, где есть `_meta`."""
    prof = guarded_bsl["get_object_profile"]("ЗаведомоНетТакогоОбъекта", limit=None)
    assert "error" in prof
    assert "limit" in prof["_meta"].get("arg_warning", "")


def test_zero_stays_valid(guarded_bsl):
    """`0` — валидное значение, не мусор: гард не должен подменять его дефолтом."""
    assert guarded_bsl["search_regions"]("Служебные", 0) == []


def test_list_overload_isolation_preserved(guarded_bsl):
    """До фикса общий на батч limit=None ронял ВСЕ элементы в {'error': ...}."""
    res = guarded_bsl["find_callers_context"](["ЦелеваяПроцедура", "Обработать000_0"], "", 0, None)
    assert set(res) == {"ЦелеваяПроцедура", "Обработать000_0"}
    for name, data in res.items():
        assert "error" not in data, (name, data)


# ── v1.33.0: гард имён секций get_object_profile ────────────────────────────


def test_unknown_profile_section_yields_arg_warning(guarded_bsl):
    """Опечатка в имени секции обязана быть заметной, а не давать пустой профиль."""
    res = guarded_bsl["get_object_profile"]("Документ.ТестовыйДокумент", sections=["predefined_items"])
    warn = (res.get("_meta") or {}).get("arg_warning") or ""
    assert "predefined_items" in warn
    assert "structure" in warn, "варнинг обязан перечислить принятые имена"


def test_flow_and_code_usages_are_accepted_as_section_names(guarded_bsl):
    """sections=['flow'] — естественная догадка по имени секции в самом же ответе."""
    res = guarded_bsl["get_object_profile"]("Документ.ТестовыйДокумент", sections=["flow"])
    assert "flow" in res["sections"]
    res = guarded_bsl["get_object_profile"]("Документ.ТестовыйДокумент", sections=["code_usages"])
    assert "code_usages" in res["sections"]


def test_known_section_produces_no_warning(guarded_bsl):
    res = guarded_bsl["get_object_profile"]("Документ.ТестовыйДокумент", sections=["structure"])
    assert not (res.get("_meta") or {}).get("arg_warning")


# ── kinds= у find_references_to_object: тот же класс, что и sections= ─────────
#
# Неизвестный вид ссылки ни с чем не совпадает, и раньше это давало МОЛЧАЛИВЫЙ
# total=0 — ответ, неотличимый от честного «ссылок нет». В e2e-прогоне v1.33.0
# агент на этом построил вывод «документ ни в какие подсистемы не входит»,
# передав человекочитаемое 'Subsystem' вместо ключа 'subsystem_content'.


def test_unknown_ref_kind_yields_arg_warning(guarded_bsl):
    res = guarded_bsl["find_references_to_object"]("Документ.ТестовыйДокумент", kinds=["Subsystem"])
    warn = (res.get("_meta") or {}).get("arg_warning") or ""
    assert "Subsystem" in warn, "варнинг обязан назвать отвергнутый вид"
    assert "subsystem_content" in warn, "варнинг обязан перечислить допустимые виды"
    assert res["total"] == 0


def test_mixed_ref_kinds_keep_filtering_by_valid_ones(guarded_bsl):
    """Валидный вид продолжает фильтровать как прежде — гард только информирует."""
    mixed = guarded_bsl["find_references_to_object"]("Документ.ТестовыйДокумент", kinds=["Subsystem", "attribute_type"])
    only_valid = guarded_bsl["find_references_to_object"]("Документ.ТестовыйДокумент", kinds=["attribute_type"])
    assert only_valid["total"] > 0, "фикстура обязана давать ссылку, иначе сравнение вакуумно (0 == 0)"
    assert mixed["total"] == only_valid["total"]
    assert mixed["by_kind"] == only_valid["by_kind"]
    assert "Subsystem" in ((mixed.get("_meta") or {}).get("arg_warning") or "")


def test_known_ref_kind_produces_no_meta_noise(guarded_bsl):
    """Без предупреждения ключа _meta у хелпера быть не должно — контракт прежний."""
    res = guarded_bsl["find_references_to_object"]("Документ.ТестовыйДокумент", kinds=["attribute_type"])
    assert "_meta" not in res


# ── две корзины find_functional_options ──────────────────────────────────────
#
# `total` — сумма корзин РАЗНОЙ точности (xml — exact, code — подстрочный grep).
# Два прогона подряд (1.30.0 и 1.33.0) агент читал её как «столько ФО у объекта»
# и получал 167 вместо 34. Разбивка обязана лежать в самом ответе.


def test_functional_options_without_limit_keep_legacy_key_set(guarded_bsl):
    """Ветка без limit — прежний контракт: ни total, ни разбивки, только списки."""
    res = guarded_bsl["find_functional_options"]("ТестовыйДокумент", include_code=False)
    assert len(res["xml_options"]) > 0, "фикстура обязана давать ФО, иначе проверка вакуумна"
    assert set(res) == {"object", "xml_options", "code_options"}


def test_functional_options_bucket_totals_match_total_with_limit(guarded_bsl):
    # limit=0 — единственный вход, который РАЗЛИЧАЕТ «полную корзину» и «длину среза»:
    # при limit>=размера корзины оба числа совпадают и тест ничего не доказывает.
    res = guarded_bsl["find_functional_options"]("ТестовыйДокумент", limit=0)
    assert res["total"] > 0, "фикстура обязана давать ФО, иначе проверка вакуумна"
    assert res["total"] == res["xml_total"] + res["code_total"]
    assert res["xml_options"] == [], "срез при limit=0 обязан быть пустым"
    assert res["xml_total"] > 0, "xml_total — ПОЛНАЯ корзина, а не длина среза"
