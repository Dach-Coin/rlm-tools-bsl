import re

import pytest

from rlm_tools_bsl.bsl_knowledge import (
    BSL_PATTERNS,
    EFFORT_LEVELS,
    EffortConfig,
    RLM_START_DESCRIPTION,
    _BUSINESS_RECIPES,
    _RECIPE_ALIASES,
    _match_recipe,
    get_strategy,
)


# --- BSL_PATTERNS ---


def test_all_patterns_compile():
    """All regex patterns must compile without error."""
    for name, pattern in BSL_PATTERNS.items():
        compiled = re.compile(pattern)
        assert compiled is not None, f"Pattern {name} failed to compile"


def test_procedure_def_pattern():
    pattern = re.compile(BSL_PATTERNS["procedure_def"])
    assert pattern.search("Процедура МояПроцедура(Параметр1) Экспорт")
    assert pattern.search("Функция МояФункция()")
    assert not pattern.search("// комментарий")


def test_procedure_end_pattern():
    pattern = re.compile(BSL_PATTERNS["procedure_end"])
    assert pattern.search("КонецПроцедуры")
    assert pattern.search("  КонецФункции")
    assert not pattern.search("Процедура")


def test_module_call_pattern():
    pattern = re.compile(BSL_PATTERNS["module_call"])
    m = pattern.search("ОбщийМодуль.МояФункция(Параметры)")
    assert m is not None
    assert m.group(1) == "ОбщийМодуль"
    assert m.group(2) == "МояФункция"


def test_region_patterns():
    start = re.compile(BSL_PATTERNS["region_start"])
    end = re.compile(BSL_PATTERNS["region_end"])
    m = start.search("#Область ПрограммныйИнтерфейс")
    assert m is not None
    assert m.group(1) == "ПрограммныйИнтерфейс"
    assert end.search("#КонецОбласти")


# --- EFFORT_LEVELS ---


def test_effort_levels_keys():
    assert set(EFFORT_LEVELS.keys()) == {"low", "medium", "high", "max"}


def test_effort_levels_types():
    for name, config in EFFORT_LEVELS.items():
        assert isinstance(config, EffortConfig)
        assert config.max_execute_calls > 0
        assert config.max_llm_calls > 0
        assert config.safe_grep_max_files > 0
        assert len(config.guidance) > 0


def test_effort_levels_ordering():
    """Higher effort levels should have higher limits."""
    levels = ["low", "medium", "high", "max"]
    for i in range(len(levels) - 1):
        a = EFFORT_LEVELS[levels[i]]
        b = EFFORT_LEVELS[levels[i + 1]]
        assert b.max_execute_calls >= a.max_execute_calls


# --- get_strategy ---


def test_strategy_contains_critical_warning():
    text = get_strategy("medium", None)
    assert "CRITICAL" in text
    assert "23,000" in text or "23000" in text or "timeout" in text.lower()


def test_strategy_contains_helper_signatures():
    text = get_strategy("medium", None)
    assert "find_module" in text
    assert "find_by_type" in text
    assert "extract_procedures" in text
    assert "safe_grep" in text
    assert "read_procedure" in text
    assert "find_callers" in text


def test_disambiguation_section_warns_about_keys_and_paths():
    """BUG-5b/7: DISAMBIGUATION в strategy header содержит:
    - предупреждение о различии ключей get_object_full_structure vs find_attributes;
    - guidance по путям parse_object_xml (директория vs файл);
    - указание find_roles для прав вместо parse_object_xml('Roles/X').
    """
    text = get_strategy("medium", None)
    # DISAMBIGUATION секция присутствует
    assert "DISAMBIGUATION" in text
    # BUG-5b: ключи отличаются от find_attributes
    assert "attr_name" in text
    assert "find_attributes" in text
    # BUG-7: предпочтительный путь parse_object_xml — к директории
    assert "Documents/X" in text
    # BUG-7: для ролей — find_roles, не parse_object_xml
    assert "find_roles" in text


def test_strategy_contains_effort_guidance():
    for effort in ["low", "medium", "high", "max"]:
        text = get_strategy(effort, None)
        # At minimum the strategy should mention the effort level or contain some guidance
        assert len(text) > 100


def test_strategy_with_format_info():
    """When format_info is provided, strategy should mention format."""
    from rlm_tools_bsl.format_detector import FormatInfo, SourceFormat

    cf_info = FormatInfo(
        primary_format=SourceFormat.CF,
        root_path="/test",
        bsl_file_count=100,
        has_configuration_xml=True,
        metadata_categories_found=["CommonModules", "Documents"],
    )
    text = get_strategy("medium", cf_info)
    assert "CF" in text or "cf" in text or "Ext" in text


def test_get_strategy_format_hints():
    """Format-specific hints must appear for CF and EDT, not for None."""
    from rlm_tools_bsl.format_detector import FormatInfo, SourceFormat

    cf_info = FormatInfo(
        primary_format=SourceFormat.CF,
        root_path="/test",
        bsl_file_count=100,
        has_configuration_xml=True,
        metadata_categories_found=[],
    )
    cf_text = get_strategy("medium", cf_info)
    assert "FORMAT: CF" in cf_text
    assert "Ext/" in cf_text

    edt_info = FormatInfo(
        primary_format=SourceFormat.EDT,
        root_path="/test",
        bsl_file_count=50,
        has_configuration_xml=False,
        metadata_categories_found=[],
    )
    edt_text = get_strategy("medium", edt_info)
    assert "FORMAT: EDT" in edt_text

    none_text = get_strategy("medium", None)
    assert "FORMAT: CF" not in none_text
    assert "FORMAT: EDT" not in none_text


# --- Descriptions ---


def test_rlm_start_description():
    assert "BSL" in RLM_START_DESCRIPTION
    assert "1C" in RLM_START_DESCRIPTION
    assert "find_module" in RLM_START_DESCRIPTION


# --- Business recipes ---


def test_business_recipes_structure():
    """All domains must have compact and full keys."""
    # v1.11.0+: 12 prior + 'иерархия вызовов' + 'расширения' = 14
    # v1.19.0+: + 'достижимость' + 'путь данных' = 16
    assert len(_BUSINESS_RECIPES) == 16
    short_full_allowed = {
        "тип реквизита": 3,
        "ссылки": 3,
        "перечисления": 4,
        "ввод на основании": 4,
        "структура объекта": 4,
        "иерархия вызовов": 6,
        "расширения": 6,
    }
    for domain, recipe in _BUSINESS_RECIPES.items():
        assert "compact" in recipe, f"{domain}: missing compact"
        assert "full" in recipe, f"{domain}: missing full"
        assert len(recipe["compact"]) >= 2, f"{domain}: compact too short"
        min_full = short_full_allowed.get(domain, 6)
        assert len(recipe["full"]) >= min_full or domain == "интеграция", f"{domain}: full too short"


def test_recipe_aliases_consistency():
    """Each alias must point to an existing _BUSINESS_RECIPES domain."""
    for alias, dom in _RECIPE_ALIASES.items():
        assert dom in _BUSINESS_RECIPES, f"alias '{alias}' points to missing domain '{dom}'"


def test_based_on_recipe_prints_canonical_metadata_ref():
    full_text = "\n".join(_BUSINESS_RECIPES["ввод на основании"]["full"])
    assert 'd.get("ref") or d["document"]' in full_text
    assert 'd.get("category","")+"."' not in full_text


def test_references_recipe_has_subsystem_membership_hint():
    """v1.24.0 #3 — рецепт 'ссылки' должен подсказывать членство в подсистемах через
    kinds=['subsystem_content'] + предупреждать про устаревший индекс (live-проверку)."""
    full_text = " ".join(_BUSINESS_RECIPES["ссылки"]["full"])
    assert "subsystem_content" in full_text
    assert "kinds=['subsystem_content']" in full_text
    # stale-index caveat: подсказка должна вести к live-проверке состава подсистем
    assert "устаревш" in full_text.lower() or "live" in full_text.lower()


def test_reachability_knowledge_mentions_error_contract():
    """v1.25.0 — домен 'достижимость' должен учить проверять error/hint
    (многозначное имя без hint у find_path) ПЕРЕД интерпретацией found/budget_exceeded."""
    recipe = _BUSINESS_RECIPES["достижимость"]
    text = " ".join(recipe["compact"]) + " " + " ".join(recipe["full"])
    assert "error" in text
    assert "hint" in text
    # должно упоминать многозначность / неоднозначность имени
    assert "многознач" in text.lower() or "неоднознач" in text.lower() or "candidates" in text


def test_match_recipe_found():
    assert _match_recipe("Как рассчитывается себестоимость?") == "себестоимость"
    assert _match_recipe("Проведение документа РеализацияТоваров") == "проведение"
    assert _match_recipe("Распределение затрат по номенклатуре") == "распределение"
    assert _match_recipe("Печать товарной накладной") == "печать"
    assert _match_recipe("Права доступа к справочнику") == "права"
    assert _match_recipe("Интеграция с внешними системами") == "интеграция"


def test_match_recipe_aliases():
    assert _match_recipe("обмен данными с сайтом") == "интеграция"
    assert _match_recipe("синхронизация с сайтом") == "интеграция"
    assert _match_recipe("exchange data with external system") == "интеграция"


def test_match_recipe_form_events():
    """'события формы' recipe matches form-related queries."""
    assert _match_recipe("события формы документа") == "события формы"
    assert _match_recipe("обработчики формы справочника") == "события формы"


def test_match_recipe_print_form_not_hijacked():
    """'печатная форма' must NOT match 'события формы' — 'печать' matches first."""
    result = _match_recipe("печатная форма")
    assert result is None or result == "печать"
    # 'печать' domain requires exact substring: "печать" NOT in "печатная форма" (ь≠н)
    # so both None and "печать" are acceptable (depends on substring match)


def test_match_recipe_bare_form_no_match():
    """Bare 'форма' must NOT match any recipe (too broad)."""
    assert _match_recipe("форма ТОРГ-12") is None


def test_match_recipe_not_found():
    # v1.11.0+: 'http' is now an alias of 'интеграция', so HTTP-сервис matches integration
    assert _match_recipe("Найди все HTTP-сервисы") == "интеграция"
    assert _match_recipe("Опиши МЭДО") == "интеграция"
    # Genuinely unmatched queries
    assert _match_recipe("") is None
    assert _match_recipe("Покажи код модуля") is None
    assert _match_recipe("Какие константы есть") is None


def test_match_recipe_case_insensitive():
    assert _match_recipe("СЕБЕСТОИМОСТЬ товаров") == "себестоимость"
    assert _match_recipe("Печать ТОРГ-12") == "печать"


def test_strategy_step0_always_present():
    text = get_strategy("medium", None)
    assert "Step 0" in text
    assert "UNDERSTAND" in text


def test_strategy_compact_recipe_low_effort():
    text = get_strategy("low", None, query="себестоимость")
    assert "BUSINESS RECIPE: себестоимость" in text
    # Extract only the recipe section
    start = text.index("BUSINESS RECIPE: себестоимость")
    rest = text[start:]
    end = rest.index("\n\n") if "\n\n" in rest else len(rest)
    recipe_section = rest[:end]
    # compact has exactly 3 numbered steps
    assert "  1." in recipe_section
    assert "  3." in recipe_section
    assert "find_by_type" in recipe_section
    assert "find_register_writers" in recipe_section
    # full-only items must NOT be in the recipe section
    assert "find_callers_context" not in recipe_section
    assert "analyze_subsystem" not in recipe_section


def test_strategy_compact_recipe_medium_effort():
    text = get_strategy("medium", None, query="себестоимость")
    assert "BUSINESS RECIPE: себестоимость" in text


def test_strategy_full_recipe_high_effort():
    text = get_strategy("high", None, query="себестоимость")
    assert "BUSINESS RECIPE: себестоимость" in text
    assert "find_callers_context" in text
    assert "analyze_subsystem" in text


def test_strategy_full_recipe_max_effort():
    text = get_strategy("max", None, query="себестоимость")
    assert "BUSINESS RECIPE: себестоимость" in text
    assert "ALT:" in text


def test_strategy_no_recipe_without_query():
    text = get_strategy("high", None, query="")
    assert "BUSINESS RECIPE: " not in text
    # Step 0 generic hint still present
    assert "Step 0" in text


def test_strategy_no_recipe_no_match():
    # v1.11.0+: queries genuinely outside the recipe domain (no HTTP/SOAP/integration
    # keywords, no module/code/structure terms, etc.)
    text = get_strategy("high", None, query="Покажи произвольный код")
    assert "BUSINESS RECIPE: " not in text


def test_strategy_recipe_all_domains():
    """Each domain can be matched and injected."""
    for domain in _BUSINESS_RECIPES:
        text = get_strategy("high", None, query=domain)
        assert f"BUSINESS RECIPE: {domain}" in text


def test_integration_recipe_exists():
    assert "интеграция" in _BUSINESS_RECIPES


def test_integration_recipe_compact():
    recipe = _BUSINESS_RECIPES["интеграция"]["compact"]
    assert len(recipe) >= 3
    assert any("find_http_services" in s for s in recipe)


def test_integration_recipe_full():
    recipe = _BUSINESS_RECIPES["интеграция"]["full"]
    assert len(recipe) >= 6
    assert any("find_web_services" in s for s in recipe)
    assert any("find_xdto_packages" in s for s in recipe)
    assert any("find_exchange_plan_content" in s for s in recipe)


def test_integration_recipe_code_hint():
    recipe = _BUSINESS_RECIPES["интеграция"]
    assert "code_hint" in recipe
    assert "find_http_services" in recipe["code_hint"]
    assert "find_exchange_plan_content" in recipe["code_hint"]
    assert "find_scheduled_jobs" in recipe["code_hint"]


def test_integration_strategy_injection():
    text = get_strategy("high", None, query="интеграция с внешними системами")
    assert "BUSINESS RECIPE" in text
    assert "find_http_services" in text


def test_integration_strategy_code_hint_injected():
    text = get_strategy("high", None, query="интеграция с внешними системами")
    assert "Ready-to-use code" in text
    assert "```python" in text


def test_integration_strategy_via_alias():
    text = get_strategy("high", None, query="обмен данными с сайтом")
    assert "BUSINESS RECIPE: интеграция" in text


# --- v1.28.0: рецепты и стратегии обязаны вести к агрегатам get_overrides ---


def test_overrides_recipe_uses_aggregates_not_the_truncated_slice():
    """Рецепт «расширения» учил строить сводку по усечённым 200 строкам — ровно то, что
    чинит v1.28.0. Рецепт обязан направлять на агрегаты."""
    import json

    from rlm_tools_bsl.bsl_knowledge import _get_topic_recipe

    for fmt in ("compact", "full"):
        rec = _get_topic_recipe("расширения", format=fmt)
        text = json.dumps(rec, ensure_ascii=False)
        assert "by_object_top" in text, (fmt, text)
        assert "by_annotation" in text, (fmt, text)
        # старая формулировка, толкавшая к ручной группировке среза, должна уйти
        assert "сводка group-by неполная" not in text, (fmt, text)


def test_both_strategy_copies_carry_overrides_aggregates(monkeypatch):
    """full-стратегия — ОТДЕЛЬНАЯ, встроенная копия (bsl_knowledge), а не bsl_strategy_data.
    При RLM_STRATEGY_MODE=full агент получает ЕЁ, поэтому обновлять надо ОБЕ копии, иначе
    половина пользователей продолжит получать старые инструкции.

    Источники проверяются РАЗДЕЛЬНО и с ПУСТЫМ query: с непустым query в стратегию
    инжектится рецепт «расширения», и by_object_top попал бы в текст ИЗ РЕЦЕПТА — тест был
    бы зелёным даже при старой встроенной full-копии.
    """
    from rlm_tools_bsl.bsl_strategy_data import STRATEGY_SECTIONS

    # 1) slim-источник (bsl_strategy_data) — без всякого рецепта
    slim_text = "\n".join(str(v) for v in STRATEGY_SECTIONS.values())
    assert "by_object_top" in slim_text, "slim-стратегия не упоминает агрегаты get_overrides"

    # 2) встроенная full-копия (bsl_knowledge) — ПУСТОЙ query, чтобы рецепт не инжектился
    monkeypatch.setenv("RLM_STRATEGY_MODE", "full")
    full_text = get_strategy("medium", None, query="")
    assert "by_object_top" in full_text, "встроенная full-стратегия осталась старой (get_overrides)"


def test_posting_recipe_does_not_claim_unpostable_on_zero_code_registers():
    """Рецепт утверждал «code_registers=0 → документ непроводим». Это ЛОЖЬ для документов
    с делегированным проведением. Рецепт обязан вести к проверке posting_handler_present /
    is_postable, а не к выводу о непроводимости."""
    import json

    from rlm_tools_bsl.bsl_knowledge import _get_topic_recipe

    for fmt in ("compact", "full"):
        text = json.dumps(_get_topic_recipe("проведение", format=fmt), ensure_ascii=False)
        assert "posting_handler_present" in text, (fmt, text)


def test_both_strategy_copies_carry_posting_handler_signal(monkeypatch):
    """Парный к test_both_strategy_copies_carry_overrides_aggregates. Проверяем обе
    физические копии стратегии, ПУСТОЙ query."""
    from rlm_tools_bsl.bsl_strategy_data import STRATEGY_SECTIONS

    slim_text = "\n".join(str(v) for v in STRATEGY_SECTIONS.values())
    assert "posting_handler_present" in slim_text, "slim-стратегия не упоминает posting_handler_present"

    monkeypatch.setenv("RLM_STRATEGY_MODE", "full")
    full_text = get_strategy("medium", None, query="")
    assert "posting_handler_present" in full_text, "встроенная full-стратегия осталась старой (движения)"


def test_no_strategy_copy_names_the_platform_handler_without_the_warning(monkeypatch):
    """Инвариант вместо дублирования текста: копия стратегии ВПРАВЕ не упоминать
    ОбработкаПроведения вовсе (slim так и делает — и платит за это 0 токенов), но если упомянула,
    то обязана сказать, что его зовёт ПЛАТФОРМА и callers=0 — это норма.

    Встроенная full-стратегия приводила ОбработкаПроведения как КАНОНИЧЕСКИЙ пример module_hint
    для find_call_hierarchy, то есть учила приёму, который ВСЕГДА возвращает пусто, и прямо
    противоречила рецепту «проведение». Стратегия — самая горячая agent-facing поверхность (её
    видят на КАЖДОМ старте, в отличие от рецептов по запросу), поэтому дефект тут дороже всего.

    Инвариант, а не «обе копии обязаны нести текст»: заставлять slim нести предупреждение о
    том, чего slim не упоминает, — это чистый расход бюджета старта.

    Проверяем ОТРЕНДЕРЕННУЮ стратегию (get_strategy), а не сырые STRATEGY_SECTIONS, и ОБЯЗАТЕЛЬНО
    с idx_stats. Обе оговорки — про грабли, на которые тест уже наступил, пока писался:
      1) первая версия читала для slim сырые STRATEGY_SECTIONS и ПРОПУСТИЛА дефект: блок INDEX TIPS
         живёт в bsl_knowledge и подмешивается в ОБА режима, а в сырых секциях его нет;
      2) вторая версия звала get_strategy БЕЗ idx_stats — а INDEX TIPS рендерятся ТОЛЬКО при
         наличии индекса, поэтому проверка выполнялась вхолостую (в тексте не было ни обработчика,
         ни предупреждения — «зелено» ни о чём). Агент же всегда работает С индексом.
    Пинить надо ровно то, что реально уходит агенту."""
    idx_stats = {"methods": 100, "calls": 200, "has_fts": True}
    for mode in ("slim", "full"):
        monkeypatch.setenv("RLM_STRATEGY_MODE", mode)
        for stats in (None, idx_stats):
            text = get_strategy("medium", None, idx_stats=stats, query="")
            if "ОбработкаПроведения" in text:
                # Корень «ПЛАТФОРМ», а не словоформа: текст говорит «вызов от ПЛАТФОРМЫ».
                assert "ПЛАТФОРМ" in text, (
                    f"{mode}-стратегия (idx_stats={'да' if stats else 'нет'}) называет "
                    "ОбработкаПроведения, но не предупреждает, что его зовёт ПЛАТФОРМА (callers=0 — "
                    "норма, а не мёртвый код). Пример module_hint на платформенном обработчике всегда "
                    "вернёт пусто и противоречит рецепту «проведение»."
                )


def test_functional_options_recipe_mentions_limit_in_both_formats():
    """Алиас «функциональные опции» → топик «права». limit= обязан быть и в compact (дефолт
    rlm_help), и в full — иначе агент упрётся в обрыв по max_output_chars и о параметре не
    узнает (наблюдалось в e2e v1.28.0).

    Ассерт на find_roles — гейт против «лечения» правкой не той строки: compact-строка
    ОБЪЕДИНЁННАЯ («детальнее: find_roles(...) по ролям; find_functional_options(...) по
    опциям»), и подмена её строкой только про ФО выкинула бы маршрут детализации ролей."""
    import json

    from rlm_tools_bsl.bsl_knowledge import _get_topic_recipe

    for fmt in ("compact", "full"):
        text = json.dumps(_get_topic_recipe("функциональные опции", format=fmt), ensure_ascii=False)
        assert "find_functional_options" in text, (fmt, text)
        assert "limit=" in text, (fmt, text)
        assert "find_roles" in text, (fmt, "маршрут по ролям потерян при правке рецепта")


def test_functional_options_limit_visible_in_both_performance_copies(monkeypatch):
    """performance-секция существует в ДВУХ копиях (slim: STRATEGY_SECTIONS, full: встроенная
    в bsl_knowledge). Пропустив одну, мы бы рассказывали про limit= только половине
    пользователей."""
    from rlm_tools_bsl.bsl_strategy_data import STRATEGY_SECTIONS

    assert "limit=" in STRATEGY_SECTIONS["performance"], "slim performance-секция без limit="
    monkeypatch.setenv("RLM_STRATEGY_MODE", "full")
    assert "limit=10" in get_strategy("medium", None, query=""), "full-стратегия без limit="


# --- build_generic_strategy (v1.32.0): сессия без BSL-хелперов ---


def test_generic_strategy_has_only_available_helpers():
    from rlm_tools_bsl.bsl_knowledge import build_generic_strategy

    text = build_generic_strategy("medium")
    assert "read_file" in text and "glob_files" in text
    for absent in ("find_module", "find_by_type", "safe_grep", "rlm_help"):
        assert absent not in text


def test_generic_strategy_unknown_effort_is_medium():
    from rlm_tools_bsl.bsl_knowledge import build_generic_strategy

    text = build_generic_strategy("bogus")
    assert "== EFFORT: medium ==" in text
    assert "bogus" not in text


@pytest.mark.parametrize("effort", ["low", "medium", "high", "max"])
def test_generic_strategy_does_not_advertise_missing_llm(effort):
    from rlm_tools_bsl.bsl_knowledge import build_generic_strategy

    assert "llm_query" not in build_generic_strategy(effort, has_llm_tools=False)
    assert "llm_query" in build_generic_strategy(effort, has_llm_tools=True)
