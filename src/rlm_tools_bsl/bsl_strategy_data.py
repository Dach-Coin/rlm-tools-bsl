"""Structured strategy data for the slim-mode `rlm_help` MCP tool.

Leaf module — imports only stdlib. Holds the canonical text of strategy
sections plus a structured form of the disambiguation block. The slim
strategy header points the agent at `rlm_help(section=...)` /
`rlm_help(helpers=[...], section='disambiguation')`; the dispatcher reads
from the dictionaries below.

The legacy ("full") strategy in `bsl_knowledge._build_full_strategy`
continues to produce its own copies of the same content directly from
`_STRATEGY_HEADER` / `_STRATEGY_IO_SECTION`. A regression test in
`tests/test_strategy_data.py` verifies the data here did not drift away
from those literals.
"""

from __future__ import annotations


STRATEGY_SECTIONS: dict[str, str] = {
    "critical": """\
== CRITICAL ==
Large configs have 23,000+ files. grep on broad paths WILL timeout. ALWAYS:
  1. find_module('name') → get file paths first
  2. Then read_file(path) or grep(pattern, path=specific_file)
If a helper returns an error, read the HINT at the end — it tells you what to do next.""",
    "workflow": """\
== WORKFLOW ==
BEFORE YOU START: check rlm_start response — warnings, extension_context, detected_custom_prefixes.

Step 0 — UNDERSTAND: decode the business question
  If a "BUSINESS RECIPE" section appears below — follow it. It was auto-selected by your query.
  No recipe? → analyze_subsystem('ПодсистемаИмя') for domain overview, then proceed to Step 1.

Step 1 — DISCOVER: find what you need
  search(query)                          → BROAD first pass: methods + objects + regions + headers + attributes + predefined
  find_module('name') or find_by_type('Documents', 'name') → get file paths
  search_objects('бизнес-имя')           → precise: find 1C OBJECTS by Russian synonym
  search_methods('substring')            → precise: find METHODS by code name (FTS)
  search_regions('имя')                  → precise: find code regions
  search_module_headers('текст')         → precise: find modules by header
  NOTE: search_regions/search_module_headers молча усекаются по limit — для census бери count_only=True: тот же scope, что и выдача (с CFE +total_main/total_extensions)
  NOTE: search() = broad first pass; specialized helpers = precise follow-up when you need specific fields
  parse_object_xml(path) → attributes, tabular sections, dimensions, resources
  find_attributes('ИмяРеквизита')        → INSTANT: attribute name → type(s)
  find_predefined('ИмяПредопределённого') → INSTANT: predefined item → type(s)
  find_references_to_object('Справочник.Имя') → все места использования объекта (analogue of "Найти ссылки → В свойствах")
  find_defined_types('Имя')              → раскрытие ОпределяемогоТипа в список реальных типов
  parse_form(object_name) → form handlers, commands, attributes (for UI/form analysis tasks)

Step 2 — READ: understand the code
  extract_procedures(path) → list all procedures with lines
  read_procedure(path, 'ProcName') → str | None. None = имя неточное или у объекта только XML — звони extract_procedures(path).
  find_exports(path) → exported API of a module

Step 3 — TRACE: follow the call chains
  find_callers_context(proc, module_hint) → who calls this procedure (1 уровень + контекст вызова)
  find_call_hierarchy(name, direction='callers', depth=2, module_hint='') → транзитивные вызывающие 2-3 уровня в одном вызове (вместо итерации find_callers_context). depth=1 → используй find_callers_context. Для одноимённого объектного метода передай module_hint='Документ.X' (exact-режим, точные рёбра).
  safe_grep(pattern, name_hint) → search code patterns
  find_event_subscriptions(object_name) → what fires on write/post

Step 4 — ANALYZE: get the full picture
  get_object_full_structure(name) → INSTANT: метаданные + ТЧ + реквизиты + предопределённые + раскрытые перечисления + список форм. Используй ВМЕСТО parse_object_xml + find_attributes + find_predefined.
  analyze_object(name) → metadata + all modules + procedures
  analyze_document_flow(doc_name) → subscriptions + register movements + jobs
  find_custom_modifications(object_name) → find non-standard code by prefix
  find_register_movements(doc_name) → Posting/CFE-фильтрованные кандидаты (main — снимок индекса; сначала is_postable; при пустом code_registers смотри posting_handler_present + hint)
  CAUTION: analyze_document_flow and analyze_object scan many files — on large configs (10K+)
  they may be slow (>60s). Prefer calling individual helpers separately if timeout occurs.

Step 5 — EXTENSIONS: check if behavior is modified
  get_overrides('ObjectName') → overrides=срез 200. Агрегаты by_annotation/by_object_top/by_extension_top=dict{имя:N}/unique_* полны iff partial=False; иначе lower bound, см. _meta. target_method_line=None валидно
  read_procedure(path, name, include_overrides=True) → original + extension body
  extract_procedures includes overridden_by field
  NOTE: extension files are OUTSIDE the sandbox: read_file/grep/glob_files on '../' paths raise PermissionError.
  BUT: if find_module returned a path starting with '../' — it is an extension file; pass it directly
  to high-level BSL helpers (read_procedure, extract_procedures, parse_object_xml, find_attributes,
  find_predefined, search). They read extensions internally. For overrides use get_overrides/find_ext_overrides.""",
    "performance": """\
== STEP 4 EXTENDED (по перформансу) ==

INSTANT (индексный путь, OK для batch 5-10 в одном rlm_execute):
  find_register_writers(reg_name)        → статические reverse-кандидаты
  find_register_movements(doc_name)      → Posting/CFE-фильтрованные кандидаты; main-строки — снимок индекса
  find_event_subscriptions(obj)          → подписки на события (event_filter + limit опционально)
  find_scheduled_jobs(name='')           → регламентные задания
  find_roles(obj_name)                   → роли с правами на объект
  find_defined_types(name)               → раскрытие ОпределяемогоТипа
  find_enum_values(enum_name)            → INSTANT с индексом; LIVE fallback на чтение Enum.xml без индекса
  get_object_full_structure(name)        → агрегат: реквизиты + ТЧ + предопределённые + перечисления + формы

HYBRID (часть из индекса, часть live — ОДИН вызов в batch, не больше 2-3):
  find_functional_options(obj_name[, limit=10]) → xml_options из индекса; code_options через safe_grep (live, всегда); limit= — per-bucket cap, спасает от обрыва по max_output_chars

LIVE (читают тела процедур / parse XML — медленно, особенно без индекса):
  find_based_on_documents(doc_name)      → read_procedure(ОбработкаЗаполнения, ДобавитьКомандыСозданияНаОсновании) — НЕ batch массово
  find_print_forms(obj_name)             → read_procedure(ДобавитьКомандыПечати) — медленно на CommonModules
  analyze_object(name)                   → читает ВСЕ модули объекта
  analyze_document_flow(doc)             → объединяет subscriptions + registers + jobs + based_on + print

CAUTION: на конфигах 10K+ файлов analyze_* могут быть >60с. Батчь LIVE-хелперы по одному; INSTANT — по 5-10.""",
    "batching": """\
== BATCHING & OUTPUT ==
ОБЗОР ОБЪЕКТА ЗА 1 ВЫЗОВ — Step 0 полного анализа объекта (вместо ~10 одиночных хелперов):
  get_object_profile(name) → compact roll-up по секциям (структура+модули+регистры+подписки+роли+ФО), items=top-N без тел.
  Ровно нужное: sections=['structure','roles']; тяжёлое — только include_flow=True / include_code_usages=True.
БАТЧ-ФОРМА по умолчанию — передавай LIST перегруженным хелперам (резолв объекта 1 раз, dict по имени):
  read_procedure(path, ['Проц1','Проц2']) · find_callers_context(['A','B'], hint) · find_enum_values(['E1','E2']) · read_files([p1,p2,p3])
AGGREGATE-FIRST (не после): get_object_modules(name) → код-скелет; get_object_full_structure(name) → метаданные.
  Звать individual-хелпер, а ПОТОМ тот же агрегат — двойной фетч; бери агрегат сразу. Батчи 3-5 связанных операций за вызов.
If output is truncated (ends with '... [output truncated]'), split into smaller calls.
Print only summaries (counts, first N items) — never dump raw data.
Ответ может нести 'duplicates' (тот же хелпер с теми же args дважды — переиспользуй переменную, они живут между rlm_execute)
  и 'efficiency_hints' (подсказки по батчингу/агрегатам — следуй им).
Не зови get_index_info на старте — builder_version/has_*/counts уже в ответе rlm_start (поле index); вызов на старте = пустая трата execute (нужен лишь для has_regions/has_module_headers/extension_overrides).

Call help('keyword') for code recipes — e.g. help('exports'), help('movements'), help('flow')""",
    "io": """\
File I/O:
  read_file(path), read_files(paths)       → str / dict (numbered in MCP session)
  grep(pattern, path), grep_summary(pattern), grep_read(pattern, path)
  glob_files(pattern), tree(path, max_depth=3), find_files(name)
  NOTE: For BSL modules prefer find_module()/find_by_type() over glob_files()
  NOTE: tree('.') on large configs produces too much output — use tree('SubDir') or find_files()
LLM (if available):
  llm_query(prompt, context='')            → str (keep context <3000 chars; '[EMPTY]/[ERROR]' prefix or '[TRUNCATED]' tail = incomplete answer)
  llm_query_batched(prompts, context)      → [str]""",
}


# Disambiguation pairs in structured form — projected from the
# "== DISAMBIGUATION ==" block of `_STRATEGY_HEADER`. The slim builder
# does NOT inline these; agents fetch them via
# `rlm_help(section='disambiguation')`, optionally narrowing with
# `rlm_help(helpers=['name1','name2'], section='disambiguation')`.
DISAMBIGUATION_PAIRS: list[dict] = [
    {
        "pair": ("get_object_full_structure", "analyze_object"),
        "summary": "structure-only vs structure+modules",
        "when_a": "ТОЛЬКО metadata (attrs, ТЧ, predefined, enums, forms list). INSTANT с индексом.",
        "when_b": "metadata + modules + procedures_count + exports. Тяжелее, читает все модули.",
        "rule": "Сначала get_object_full_structure; analyze_object — только если нужны процедуры.",
        "tags": ["structure", "metadata", "composite"],
    },
    {
        "pair": ("find_call_hierarchy", "find_callers_context"),
        "summary": "multi-level tree vs single level + context",
        "when_a": "N уровней (1-3) дерево БЕЗ контекста строк. Один вызов вместо итерации. module_hint включает exact-режим для одноимённых объектных методов (точные рёбра по callee_key, _meta.root_exact/exact_rows).",
        "when_b": "1 уровень callers + контекст вызова (line/text). Быстрее.",
        "rule": "Для одного уровня используй find_callers_context; для глубины >=2 — find_call_hierarchy (с module_hint, если корень — неуникальный объектный метод).",
        "tags": ["callers", "trace"],
    },
    {
        "pair": ("find_callers", "find_callers_context"),
        "summary": "compact first page vs full API with pagination",
        "when_a": "COMPACT FIRST PAGE: тонкая обёртка, default limit=20, без _meta/has_more, плоский [{file, line, text}]. Quick view; если callers > max_files — остаток молча отбрасывается.",
        "when_b": "ПОЛНЫЙ API: caller_name, object_name, category, is_export + _meta с total_callers/has_more и пагинация (offset/limit).",
        "rule": "Под капотом — один и тот же поиск (find_callers вызывает find_callers_context). Бери find_callers для быстрого «где зовётся»; для аудита/полного списка — find_callers_context.",
        "tags": ["callers", "trace"],
    },
    {
        "pair": ("find_register_movements", "analyze_document_flow"),
        "summary": "registers only vs registers+subscriptions+jobs+based_on",
        "when_a": "только регистры, признак is_postable.",
        "when_b": "подписки + регистры + задания + ввод на основании.",
        "rule": "Если документ непроводимый (is_postable=False) — analyze_document_flow всё равно даёт подписки.",
        "tags": ["posting", "registers", "document"],
    },
    {
        "pair": ("parse_object_xml", "find_attributes"),
        "summary": "live XML (slow, full) vs index (instant, flat)",
        "when_a": "читает XML напрямую, видит синонимы ТЧ и подробные типы. SLOW без индекса.",
        "when_b": "flat-список из индекса, INSTANT, но синонимов ТЧ нет.",
        "rule": "Для «карточки объекта» используй get_object_full_structure (выбирает оптимальный путь). Каноничные ключи атрибутов разные (find_attributes → r['attr_name'], get_object_full_structure → a['name']), но записи толерантны: «чужой» алиас тоже работает.",
        "tags": ["metadata", "xml", "attributes"],
    },
    {
        "pair": ("parse_object_xml", "find_roles"),
        "summary": "raw Roles XML vs normalized rights-per-object",
        "when_a": "parse_object_xml('Roles/X') — не подходит для анализа прав: отдаёт сырую XML без нормализации право→объект.",
        "when_b": "find_roles(object_name) — нормализованный список ролей с правами на объект.",
        "rule": "Для прав доступа к объекту → ВСЕГДА find_roles, не parse_object_xml.",
        "tags": ["roles", "rights"],
    },
    {
        "pair": ("find_register_movements", "find_register_writers"),
        "summary": "document → Posting/CFE-filtered candidates vs register → static candidates",
        "when_a": "документ → какие регистры пишет (есть is_postable).",
        "when_b": "регистр → статические ссылки документов (runtime_filtered=False).",
        "rule": "find_register_movements применяет Posting/CFE; main-строка остается снимком и после изменения кода требует проверки живого модуля.",
        "tags": ["registers", "document"],
    },
    {
        "pair": ("search", "search_methods"),
        "summary": "broad first pass vs typed precise follow-up",
        "when_a": "search() — broad-first, отдаёт unified [{source_type, text, path, path_kind, detail}].",
        "when_b": "search_X() — точная типизация: поля специфичны (для search_methods → is_export, rank; для search_objects → category, synonym).",
        "rule": "Используй search для discovery; search_X — когда нужны типизированные поля для batch обработки. (Тэги покрывают и search_objects, и search_regions, и search_module_headers — фильтр rlm_help(helpers=['search_objects']) даст эту же запись.)",
        "tags": ["search", "discovery"],
    },
    {
        "pair": ("find_references_to_object", "find_code_usages"),
        "summary": "metadata-XML references vs in-code usages",
        "when_a": "find_references_to_object — ДЕКЛАРАТИВНЫЕ ссылки из метаданных-XML: типы реквизитов, владелец, основание ввода, подсистемы, права, ФО, ПВХ, DefinedType. Код модулей НЕ сканирует.",
        "when_b": 'find_code_usages — ОБРАЩЕНИЯ В КОДЕ: Документы.X (manager), "ДокументСсылка.X" (ref_type), запросы Документ.X.ТЧ (query, member=имя ТЧ). Метаданные-XML НЕ сканирует.',
        "rule": "Это РАЗНЫЕ слои. «Где объявлен/связан» → find_references_to_object. «Где используется в коде» → find_code_usages. Нужны оба — find_references_to_object(obj, include_code=True). Доступ к реквизитам через локальные переменные и код расширений — вне охвата find_code_usages.",
        "tags": ["references", "code", "usages"],
    },
    {
        "pair": ("get_object_modules", "analyze_object"),
        "summary": "лёгкий индексный скелет vs тяжёлый разбор тел",
        "when_a": "get_object_modules — все модули объекта + дерево #Область + агрегаты + флаги перехватов. Дёшев на индексном пути: НЕ читает тела (extract_procedures) и НЕ парсит XML. include_methods=False (дефолт) — только области.",
        "when_b": "analyze_object — читает ВСЕ тела процедур каждого модуля + parse_object_xml метаданных. Тяжёлый (на 10K+ конфигах >60с).",
        "rule": "Сначала get_object_modules (карта кода объекта). analyze_object — только когда реально нужны тела всех процедур сразу; иначе ныряй точечно read_procedure(path, name).",
        "tags": ["modules", "skeleton", "composite", "code"],
    },
    {
        "pair": ("get_object_modules", "get_object_full_structure"),
        "summary": "код-side скелет vs metadata-side структура (композируются)",
        "when_a": "get_object_modules — КОД: модули, области, методы/экспорты, перехваты.",
        "when_b": "get_object_full_structure — МЕТАДАННЫЕ: реквизиты, ТЧ, измерения/ресурсы, предопределённые, раскрытые перечисления, формы.",
        "rule": "Разные стороны объекта, дополняют друг друга. Нужен код → get_object_modules; нужны реквизиты/ТЧ → get_object_full_structure; нужно и то и то → зови оба (каждый дёшев на индексе).",
        "tags": ["modules", "structure", "metadata", "composite"],
    },
]
