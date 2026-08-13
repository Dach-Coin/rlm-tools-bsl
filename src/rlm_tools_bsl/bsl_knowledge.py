from __future__ import annotations

import difflib
import os
import re
from dataclasses import dataclass

from rlm_tools_bsl.bsl_strategy_data import (
    DISAMBIGUATION_PAIRS,
    STRATEGY_SECTIONS,
)


BSL_PATTERNS = {
    # Заякорен на начало строки (v1.33.0): неякорный поиск ловил объявления из
    # комментариев и строковых литералов. Префикс Асинх/Async — non-capturing,
    # нумерация групп 1..4 сохранена, поэтому call-site'ы правки не требуют.
    "procedure_def": r"^\s*(?:(?:Асинх|Async)\s+)?(Процедура|Функция|Procedure|Function)\s+(\w+)\s*\(([^)]*)\)\s*(Экспорт|Export)?",
    "procedure_end": r"^\s*(КонецПроцедуры|КонецФункции|EndProcedure|EndFunction)",
    "export_marker": r"\)\s*(Экспорт|Export)\s*$",
    "module_call": r"(\w+)\.(\w+)\s*\(",
    "region_start": r"#(?:Область|Region)\s+(\w+)",
    "region_end": r"#(?:КонецОбласти|EndRegion)",
    "preprocessor_if": r"#(?:Если|If)\s+.+\s+(?:Тогда|Then)",
    "preprocessor_endif": r"#(?:КонецЕсли|EndIf)",
    "new_structure": r"(?:Новый|New)\s+(?:Структура|Structure)\(",
    "structure_insert": r'\.(?:Вставить|Insert)\(\s*"(\w+)"',
}


class _AttrRecord(dict):
    """Запись атрибута, толерантная к «диалекту» ключей (v1.18.0, Фикс 1).

    Два хелпера исторически отдают атрибуты под разными именами ключей:
    ``get_object_full_structure`` -> ``{name, synonym, type}``, а
    ``find_attributes`` -> ``{attr_name, attr_synonym, attr_type}``. Агент,
    переносящий паттерн с одного на другой, писал ``a['attr_name']`` на
    структуре и получал детерминированный ``KeyError`` (6 из 10 e2e-тестов
    июня 2026). Этот dict-подкласс принимает доступ по «чужому» имени ключа
    прозрачно (через ``[]``, ``.get``, ``in``), но **итерация / .keys() / len()
    остаются каноничными** — агент по-прежнему видит реальные имена и учится,
    а ``json.dumps`` сериализует только реальные ключи (нулевая цена по токенам).

    Алиасинг охватывает только триаду name/synonym/type; служебные ключи вроде
    ``attr_kind`` алиаса не имеют (намеренно — у структурных записей его нет).
    """

    _EQUIV = {
        "name": "attr_name",
        "attr_name": "name",
        "synonym": "attr_synonym",
        "attr_synonym": "synonym",
        "type": "attr_type",
        "attr_type": "type",
    }

    def __missing__(self, key):
        alt = self._EQUIV.get(key)
        if alt is not None and dict.__contains__(self, alt):
            return dict.__getitem__(self, alt)
        raise KeyError(key)

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default

    def __contains__(self, key):
        if dict.__contains__(self, key):
            return True
        alt = self._EQUIV.get(key)
        return alt is not None and dict.__contains__(self, alt)


_PARAM_BYVAL_PREFIX_RE = re.compile(r"(?i)(?:Знач|Val)\s+")


def _split_params(raw: str) -> list[str]:
    """Разбить сырую строку параметров BSL-сигнатуры на список ИМЁН (v1.18.0, Фикс 2).

    ``params`` исторически хранится/возвращается строкой (``"Знач А, Б = 5"``),
    и агент, ожидавший список, получал ``AttributeError`` при итерации. Эта
    функция — единый разборщик, применяемый на helper-границе (не в build-time).

    Возвращает только имена параметров: отбрасывает по-значению-префикс
    ``Знач``/``Val`` (регистронезависимо — ключевые слова 1С) и хвост
    ``= <default>``. Пустая/пробельная сигнатура -> ``[]``.

    Разбор — мини state-machine по top-level запятым, устойчивый к строковым
    литералам с **удвоенными кавычками** (``""``): запятая внутри строкового
    default (``Разделитель = ", "``) НЕ считается разделителем параметров.
    Глубина скобок не нужна — апстрим-регекс ``procedure_def`` захватывает
    ``([^)]*)``, поэтому ``)`` и вложенные ``(...)`` в строку ``params`` не
    попадают; защищаемся только от строк с запятой/кавычками.
    """
    if not raw or not raw.strip():
        return []

    # --- top-level split, уважая строковые литералы с "" эскейпом ---
    tokens: list[str] = []
    buf: list[str] = []
    in_str = False
    i = 0
    n = len(raw)
    while i < n:
        ch = raw[i]
        if in_str:
            if ch == '"':
                if i + 1 < n and raw[i + 1] == '"':
                    buf.append('""')  # экранированная кавычка — остаёмся в строке
                    i += 2
                    continue
                in_str = False
                buf.append(ch)
            else:
                buf.append(ch)
        elif ch == '"':
            in_str = True
            buf.append(ch)
        elif ch == ",":
            tokens.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    tokens.append("".join(buf))

    names: list[str] = []
    for tok in tokens:
        tok = tok.strip()
        if not tok:
            continue
        m = _PARAM_BYVAL_PREFIX_RE.match(tok)
        if m:
            tok = tok[m.end() :]
        # Имя — всё до первого `=` (default отбрасывается). Имя стоит первым и
        # не содержит ни строк, ни `=`, поэтому простого split достаточно.
        tok = tok.split("=", 1)[0].strip()
        if tok:
            names.append(tok)
    return names


def _normalize_method_params(rows: list[dict]) -> list[dict]:
    """In-place: строковый ``params`` метода -> ``list[str]`` (v1.18.0, Фикс 2).

    Применяется на helper-границе (после вызова IndexReader), а НЕ внутри ридера:
    ``idx_reader`` duck-typed, fake/stub-ридеры из тестов отдают записи мимо
    реального IndexReader, и граница покрывает любой ридер. Идемпотентна —
    list-вход (ридер уже отдал список) не трогается.
    """
    for rec in rows:
        if isinstance(rec.get("params"), str):
            rec["params"] = _split_params(rec["params"])
    return rows


# Lightweight prefix detector for multi-line procedure signatures.
# Note: matches the START of a Procedure/Function declaration that opens a
# paren-list. The actual `procedure_def` regex (above) requires a balanced
# `([^)]*)` — too strict for multi-line cases where `)` lives on a later row.
_PROC_DEF_PREFIX_RE = re.compile(
    r"^\s*(?:(?:Асинх|Async)\s+)?(?:Процедура|Функция|Procedure|Function)\s+\w+\s*\(",
    re.IGNORECASE,
)
_PROC_STRING_LITERAL_RE = re.compile(r'"[^"\r\n]*"')


def mask_comments_and_strings(
    lines: list[str],
    *,
    keep_string_content: bool = False,
) -> list[str]:
    """Маска той же длины: текст комментариев (и, по умолчанию, содержимое строковых
    литералов) заменяется пробелами.

    Та же машина состояний, что у ``_scan_module`` в билдере (``""`` — экранированная
    кавычка, ``//`` начинает комментарий только ВНЕ строки), но с сохранением длины:
    смещения групп регекса, посчитанные по маске, применимы к оригиналу. Это и есть
    причина существования отдельной функции — ``_scan_module`` длину не сохраняет,
    поэтому ``params`` через него взять нельзя.

    ``keep_string_content=True`` гасит ТОЛЬКО комментарии. Этот режим нужен регексам,
    которые по замыслу читают имя ИЗ строкового литерала
    (``МеханизмыДокумента.Добавить("X")``, ``ИмяРегистра = "X"``): полная маска убила бы
    сам сигнал, а работа по сырому тексту тащит в индекс закомментированные вызовы.

    Кавычки-ограничители сохраняются всегда: без них ``_count_unquoted_parens`` перестал
    бы видеть границы литерала.
    """
    out: list[str] = []
    in_string = False
    for raw in lines:
        n = len(raw)
        # 1С допускает КОММЕНТАРИЙ между строками-продолжениями многострочного
        # литерала. Такая строка — комментарий, а НЕ содержимое литерала, и её
        # кавычки состояние не меняют. Без этого правила закомментированный конец
        # запроса (`//|  ОбъектРасчетов";`) закрывает литерал в маске, машина
        # состояний рассинхронизируется и гасит ОСТАТОК МОДУЛЯ.
        if in_string and raw.lstrip().startswith("//"):
            out.append(" " * n)
            continue

        # БЫСТРЫЙ ПУТЬ. Машина состояний ниже посимвольная только по необходимости:
        # строка ВНЕ литерала, в которой нет ни кавычки, ни `//`, маской не меняется
        # вовсе и отдаётся как есть (без единой аллокации). На боевых конфигурациях
        # это большинство строк, и именно он окупает маску на сборке ЕРП-масштаба.
        if not in_string:
            q = raw.find('"')
            c = raw.find("//")
            if q < 0:
                if c < 0:
                    out.append(raw)
                else:
                    out.append(raw[:c] + " " * (n - c))
                continue
            if 0 <= c < q:  # комментарий начинается раньше литерала
                out.append(raw[:c] + " " * (n - c))
                continue

        # МЕДЛЕННЫЙ ПУТЬ — та же машина состояний, но прыжками по str.find
        # и срезами вместо символ-за-символом. Семантика идентична посимвольной
        # версии (см. тест эквивалентности на боевых исходниках).
        buf: list[str] = []
        i = 0
        while i < n:
            if in_string:
                j = raw.find('"', i)
                if j < 0:  # литерал продолжается на следующей строке
                    buf.append(raw[i:] if keep_string_content else " " * (n - i))
                    break
                if j > i:
                    buf.append(raw[i:j] if keep_string_content else " " * (j - i))
                if j + 1 < n and raw[j + 1] == '"':
                    # экранированная "" — остаёмся в строке; длина всегда 2 символа
                    buf.append('""' if keep_string_content else "  ")
                    i = j + 2
                else:
                    buf.append('"')
                    in_string = False
                    i = j + 1
            else:
                q = raw.find('"', i)
                c = raw.find("//", i)
                if q < 0 and c < 0:
                    buf.append(raw[i:])
                    break
                if q >= 0 and (c < 0 or q < c):
                    buf.append(raw[i:q])
                    buf.append('"')
                    in_string = True
                    i = q + 1
                else:
                    buf.append(raw[i:c])
                    buf.append(" " * (n - c))  # хвост строки — комментарий
                    break
        out.append("".join(buf))
    return out


_MULTILINE_HARD_CAP_LINES = 20
_MULTILINE_HARD_CAP_CHARS = 2000


def _count_unquoted_parens(line: str) -> tuple[int, int]:
    """Count `(` and `)` in a line, skipping string-literal contents.

    Uses the same single-line string-literal trick (strip ``"..."`` content) so
    that `Знач X = "(не скобка)"` does not unbalance signature merging.
    """
    sanitized = _PROC_STRING_LITERAL_RE.sub("", line)
    return sanitized.count("("), sanitized.count(")")


def _merge_proc_continuations_with_mask(
    lines: list[str],
    masked: list[str],
) -> tuple[list[str], list[str], list[int]]:
    """Склейка многострочных сигнатур с параллельной маской.

    Args:
        lines: исходные строки файла (без завершающего перевода строки).
        masked: результат ``mask_comments_and_strings(lines)`` — посимвольно той же
            длины, что и ``lines``.

    Returns:
        ``(merged_lines, merged_masked, line_map)``; ``line_map[i]`` — **1-based**
        номер исходной строки, с которой начинается ``merged_lines[i]``.
        ``merged_lines[i]`` и ``merged_masked[i]`` посимвольно одинаковой длины,
        поэтому смещения групп регекса, посчитанные по маске, применимы к оригиналу.

    Решение о склейке принимается ПО МАСКЕ, поэтому закомментированное объявление с
    несбалансированной скобкой больше не проглатывает соседей.

    Hard caps: 20 строк / 2000 символов на сигнатуру — защита от разбега на битых
    файлах с несбалансированной `(`.
    """
    merged_lines: list[str] = []
    merged_masked: list[str] = []
    line_map: list[int] = []

    total = len(lines)
    i = 0
    while i < total:
        line = lines[i]
        mline = masked[i]
        if _PROC_DEF_PREFIX_RE.match(mline):
            open_count, close_count = _count_unquoted_parens(mline)
            balance = open_count - close_count
            if balance > 0:
                combined = line
                combined_mask = mline
                start_original = i + 1
                last_index = i
                for j in range(i + 1, min(i + _MULTILINE_HARD_CAP_LINES, total)):
                    combined = combined + " " + lines[j]
                    combined_mask = combined_mask + " " + masked[j]
                    o, c = _count_unquoted_parens(masked[j])
                    balance += o - c
                    last_index = j
                    if balance <= 0:
                        break
                    if len(combined) > _MULTILINE_HARD_CAP_CHARS:
                        break
                # Весь пролёт становится одной логической строкой независимо от того,
                # достигнут ли баланс (выход по hard-cap тоже схлопывается, иначе
                # вызывающий получил бы дубли совпадений сигнатуры).
                merged_lines.append(combined)
                merged_masked.append(combined_mask)
                line_map.append(start_original)
                i = last_index + 1
                continue
        merged_lines.append(line)
        merged_masked.append(mline)
        line_map.append(i + 1)
        i += 1

    return merged_lines, merged_masked, line_map


def _merge_proc_continuations(lines: list[str]) -> tuple[list[str], list[int]]:
    """Обёртка: решение о склейке — по сырым строкам (как до 1.33.0).

    Оставлена для вызовов, которые уже подают очищенный текст (``_live_code_only``)
    либо одиночное объявление обработчика.

    NB: поведение НЕ полностью идентично v14 — ``_PROC_DEF_PREFIX_RE`` теперь знает
    префикс ``Асинх``, поэтому многострочная ``Асинх Функция`` склеивается и здесь.
    Это желаемое изменение (v14 такие объявления не видел вовсе).
    """
    merged, _masked, line_map = _merge_proc_continuations_with_mask(lines, lines)
    return merged, line_map


@dataclass
class EffortConfig:
    max_execute_calls: int
    max_llm_calls: int
    safe_grep_max_files: int
    guidance: str


EFFORT_LEVELS = {
    "low": EffortConfig(
        10, 5, 5, "Quick lookup. Find target module, extract what's needed, stop. Target: 3-5 rlm_execute calls."
    ),
    "medium": EffortConfig(
        25, 15, 10, "Standard analysis. Find modules, trace 1-2 levels of calls, summarize. Target: 10-15 calls."
    ),
    "high": EffortConfig(
        50,
        30,
        20,
        "Deep analysis (RECOMMENDED for multi-aspect tasks). Multi-module trace (3-4 levels), data flow, complete picture. Target: 20-30 calls. Build mermaid diagram.",
    ),
    "max": EffortConfig(
        100,
        50,
        50,
        "Exhaustive mapping. All modules, all call chains, all data flows. Use llm_query() for semantic analysis. Target: 40-50+ calls.",
    ),
}

# Маркеры «сложного» (многоаспектного) запроса → effort=high при effort='auto'.
# casefold-подстроки. НАМЕРЕННО специфичные: общие слова вроде «полный/полное/
# целиком/по всей» исключены — они дают ложный high на простых лукапах («полный
# список реквизитов», «полный текст процедуры», «полное имя объекта»). Это лишь
# ДЕФОЛТ — агент может передать явный тир, админ — RLM_FORCE_EFFORT.
_COMPLEX_QUERY_MARKERS = (
    "жизненн",
    "сквозн",
    "поток данных",
    "механизм",
    "цепочк",
    "иерарх",
    "архитект",
    "интеграц",
    "сценари",
    "взаимосвяз",
    "влияни",
    "влияет",
    "трассир",
    "lifecycle",
    "data flow",
    "end-to-end",
    "end to end",
)


def _auto_effort(query: str) -> str:
    """Глубина по тексту запроса: 'high' для многоаспектных задач, иначе 'medium'.
    Используется когда effort='auto' (дефолт тула) и не задан RLM_FORCE_EFFORT."""
    q = (query or "").casefold()
    return "high" if any(m in q for m in _COMPLEX_QUERY_MARKERS) else "medium"


_STRATEGY_HEADER = """\
You are exploring a 1C BSL codebase via Python sandbox.
Write Python code in rlm_execute. Use print() to output results.

== CRITICAL ==
Large configs have 23,000+ files. grep on broad paths WILL timeout. ALWAYS:
  1. find_module('name') → get file paths first
  2. Then read_file(path) or grep(pattern, path=specific_file)
If a helper returns an error, read the HINT at the end — it tells you what to do next.

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
  NOTE: search_regions/search_module_headers режутся по limit (порядок — не релевантность): census — count_only=True (тот же scope, что и выдача; при CFE +total_main/total_extensions), топ-N — search_regions(group_by='name')
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

CAUTION: на конфигах 10K+ файлов analyze_* могут быть >60с. Батчь LIVE-хелперы по одному; INSTANT — по 5-10.

Step 5 — EXTENSIONS: check if behavior is modified
  get_overrides('ObjectName') → overrides=срез 200. Агрегаты by_annotation/by_object_top/by_extension_top=dict{имя:N}/unique_* полны iff partial=False; иначе lower bound. target_method_line=None валидно
  read_procedure(path, name, include_overrides=True) → original + extension body
  extract_procedures includes overridden_by field
  NOTE: extension files are OUTSIDE the sandbox: read_file/grep/glob_files on '../' paths raise PermissionError.
  BUT: if find_module returned a path starting with '../' — it is an extension file; pass it directly
  to high-level BSL helpers (read_procedure, extract_procedures, parse_object_xml, find_attributes,
  find_predefined, search). They read extensions internally. For overrides use get_overrides/find_ext_overrides.

== DISAMBIGUATION ==
get_object_full_structure(name) vs analyze_object(name):
  - get_object_full_structure → ТОЛЬКО metadata (attrs, ТЧ, predefined, enums, forms list). INSTANT с индексом.
  - analyze_object → metadata + modules + procedures_count + exports. Тяжелее, читает все модули.
  Сначала get_object_full_structure; analyze_object — только если нужны процедуры.

find_call_hierarchy(name, depth=N, module_hint=...) vs find_callers_context(name):
  - find_callers_context → 1 уровень callers + контекст вызова (line/text). Быстрее.
  - find_call_hierarchy → N уровней (1-3) дерево БЕЗ контекста строк. Один вызов вместо итерации.
    + module_hint у hierarchy: для ОДНОИМЕННЫХ объектных методов, которые РЕАЛЬНО зовут из BSL
      (ЗаполнитьДокумент и т.п.), привязывает корень к одному модулю → exact-режим (точные ребра
      по callee_key, без однофамильцев).
      Глубже exact распространяется сам. Доверие к ребрам — в _meta (root_exact/exact_rows/fallback_rows).
    + ПЛАТФОРМЕННЫЕ обработчики (ОбработкаПроведения, ПередЗаписью, ПриЗаписи): вызов от ПЛАТФОРМЫ
      в граф ВЫЗОВОВ не попадает → callers=0 это НОРМА, а не мертвый код (module_hint это не лечит).
      По имени хелпер их НЕ исключает: ЯВНЫЙ вызов обработчика из BSL, если он есть, он ПОКАЖЕТ.
      Но ЧЕМ пишутся движения, так не найти: читай тело и трассируй ДЕЛЕГАТА (rlm_help(topic='проведение')).
  Для одного уровня используй find_callers_context; для глубины >=2 — find_call_hierarchy
  (с module_hint, если корень — неуникальный объектный метод).

find_callers(name) vs find_callers_context(name):
  - find_callers          → COMPACT FIRST PAGE: тонкая обёртка над find_callers_context,
                            default limit=20, без _meta/has_more, плоский [{file, line, text}].
                            Quick view; если callers > max_files — остаток молча отбрасывается.
  - find_callers_context  → ПОЛНЫЙ API: caller_name, object_name, category, is_export
                            + _meta с total_callers/has_more и пагинация (offset/limit).
  Под капотом — один и тот же поиск (find_callers вызывает find_callers_context).
  Бери find_callers для быстрого «где зовётся»; для аудита/полного списка — find_callers_context.

find_register_movements vs analyze_document_flow:
  - find_register_movements → только регистры, признак is_postable.
  - analyze_document_flow → подписки + регистры + задания + ввод на основании.
  Если непроводимый (is_postable=False) — analyze_document_flow всё равно даёт подписки.

parse_object_xml(path) vs find_attributes(object_name=X):
  - parse_object_xml → читает XML напрямую, видит синонимы ТЧ и подробные типы. SLOW без индекса.
  - find_attributes → flat-список из индекса, INSTANT, но синонимов ТЧ нет.
  - Для «карточки объекта» используй get_object_full_structure (выбирает оптимальный путь).

get_object_full_structure(name) ключи vs find_attributes:
  - find_attributes:           [{attr_name, attr_synonym, attr_type, attr_kind}]
  - get_object_full_structure: {attributes:[{name, synonym, type}], dimensions:[{name, synonym, type}],
                                resources:[{name, synonym, type}],
                                tabular_sections:[{name, synonym, columns:[{name, synonym, type}]}]}
  Каноничные ключи разные, но записи ТОЛЕРАНТНЫ (v1.18.0): get_object_full_structure → a['name'],
  find_attributes → r['attr_name']; «чужой» алиас (name↔attr_name, synonym↔attr_synonym, type↔attr_type)
  тоже принимается. Итерируй: for a in s['attributes']: a['name'].
  Для регистров — данные в s['dimensions'] и s['resources'], s['attributes'] пустой.

parse_object_xml(path) — путь к ДИРЕКТОРИИ объекта (не к файлу):
  - 'Documents/X'                         → ПРЕДПОЧТИТЕЛЬНО (auto-resolves to .mdo or Ext/Document.xml).
  - 'Documents/X.mdo'                     → допустимо: авто-нормализация base (v1.10.0 фикс A1).
  - 'Documents/X/X.mdo'                   → допустимо: явный EDT path.
  - 'Documents/X/Ext/Document.xml'        → допустимо: явный CF path.
  - 'Documents/X/Document.xml' (без Ext/) → ошибка, такого файла нет.
  Категория должна соответствовать типу объекта: для Регистра — InformationRegisters/X, не Documents/X.

parse_object_xml для Roles vs find_roles(object_name):
  - parse_object_xml('Roles/X')      → не подходит для анализа прав: отдаёт сырую XML без нормализации право→объект.
  - find_roles(object_name)          → нормализованный список ролей с правами на объект.
  Для прав доступа к объекту → ВСЕГДА find_roles, не parse_object_xml.

find_event_subscriptions(event_filter=...) — list[str], НЕ голая строка:
  - event_filter=['BeforeWrite']         → правильно, один substring-matcher по 'beforewrite'.
  - event_filter='BeforeWrite'           → допустимо: хелпер сам обернёт в [строка] (v1.10.x фикс).
  - event_filter=['BeforeWrite','OnWrite'] → два matcher через OR.
  Если передать голую строку в version <1.10.x — Python итерировал её посимвольно
  (['B','e','f',...]) и фильтр де-факто игнорировался: каждый одно-символьный
  matcher ловил почти все события. Сейчас защита есть, но рекомендуется list[str].

find_based_on_documents(doc_name) — прямой обход + back_scan:
  - Прямой: ManagerModule.ДобавитьКомандыСозданияНаОсновании + ObjectModule.ОбработкаЗаполнения САМОГО документа.
  - Back_scan (lazy fallback): если прямой пуст для can_create_from_here — сканируется
    ОбработкаЗаполнения других Documents и собираются те, кто упомянул ДокументСсылка.<doc_name>.
  - Записи из back_scan помечены via='back_scan' (типичный кейс — Письма в ДО3:
    у них нет ДобавитьКомандыСозданияНаОсновании, но Задача/Поручение могут заполняться от них).
  - Декларативный BasedOn из metadata_references добавляет документы и другие категории.
    Для типизированного не-документа это единственный источник; без доступной таблицы ответ partial=True.

find_register_movements(doc) vs find_register_writers(reg):
  - find_register_movements: документ → какие регистры пишет (есть is_postable).
  - find_register_writers: регистр → статические ссылки документов (runtime_filtered=False).
  find_register_movements применяет Posting/CFE, но main-строки берет из снимка индекса;
  после изменения main-кода проверь живое тело файла кандидата.

search(q, scope='X') vs search_X(q):
  - search() — broad-first, отдаёт unified [{source_type, text, path, path_kind, detail}].
  - search_X() — точная типизация: поля специфичны (для search_methods → is_export, rank; для search_objects → category, synonym).
  - Используй search для discovery; search_X — когда нужны типизированные поля для batch обработки.

read_procedure(path, name) vs read_procedure(path, name, include_overrides=True):
  - Без флага: только оригинальное тело.
  - С include_overrides: оригинал + тело перехвата с маркером "=== Перехвачен &Аннотация ===".
  - Используй с include_overrides когда rlm_start обнаружил расширения (extension_context).

get_object_modules(name) vs analyze_object(name):
  - get_object_modules → все модули объекта + дерево #Область + агрегаты + флаги перехватов.
    Дёшев на индексном пути: НЕ читает тела (extract_procedures) и НЕ парсит XML.
  - analyze_object → читает ВСЕ тела процедур каждого модуля + parse_object_xml. Тяжёлый (>60с на 10K+).
  Сначала get_object_modules (карта кода); analyze_object — только если нужны тела всех процедур сразу.

get_object_modules(name) vs get_object_full_structure(name):
  - get_object_modules → КОД-side: модули, области, методы/экспорты, перехваты.
  - get_object_full_structure → METADATA-side: реквизиты, ТЧ, измерения/ресурсы, предопределённые, перечисления, формы.
  Разные стороны объекта, дополняют друг друга — нужны обе стороны, зови оба (каждый дёшев на индексе).

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

Call help('keyword') for code recipes — e.g. help('exports'), help('movements'), help('flow')
"""

# Category display order and labels for strategy table
_CATEGORY_ORDER = [
    ("discovery", "Module discovery"),
    ("code", "Code analysis"),
    ("xml", "Metadata & XML"),
    ("composite", "Composite analysis"),
    ("business", "Business logic"),
    ("extension", "Extensions"),
    ("navigation", "Navigation"),
]

_BUSINESS_RECIPES: dict[str, dict[str, list[str]]] = {
    "себестоимость": {
        "compact": [
            "search_objects('себестоимость') → объекты по синониму",
            "get_object_profile('ДокИмя') → за 1 вызов: регистры + подписки + структура + модули (вместо find_register_movements/analyze_document_flow по отдельности)",
            "find_register_writers('РегистрСебестоимости') → статические reverse-кандидаты; Posting/CFE проверь через forward, свежесть main-кода — по живому файлу",
            "детали потока → get_object_profile('ДокИмя', include_flow=True)",
        ],
        "full": [
            "search_objects('себестоимость') → документы, регистры, модули по синониму",
            "find_by_type('AccumulationRegisters', 'Себестоимость') → регистры себестоимости",
            "find_register_writers('РегистрИмя') → статические reverse-кандидаты; Posting/CFE проверь через forward, свежесть main-кода — по живому файлу",
            "analyze_document_flow('ДокИмя') → проводки + подписки + задания",
            "search_methods('Себестоимость') → методы расчёта по всей кодовой базе",
            "find_callers_context('РассчитатьСебестоимость') → цепочка вызовов",
            "analyze_subsystem('РасчетСебестоимости') → все объекты домена",
            "ALT: grep('Себестоимость', path=module) если регистр не найден",
        ],
    },
    "проведение": {
        "compact": [
            "search_objects('ДокИмя') → найти документ по бизнес-имени",
            "get_object_profile('ДокИмя') → за 1 вызов: регистры (registers) + подписки (subscriptions) + структура + модули + роли",
            "registers.summary: main_code_registers_suppressed_by_cfe>0 — handler-only main не active; code_registers=0 ≠ непроводимый: смотри posting_handler_present. Posting=Deny определяет только find_register_movements.is_postable. Исполняй hint: сервер назвал регистры и классифицировал получателя (МОДУЛЬ/ПЕРЕМЕННАЯ/РЕКВИЗИТ/НЕ ОПОЗНАН); неподтвержденный МОДУЛЬ молча даст ЧУЖОЕ тело, для НЕ ОПОЗНАН дал tree-search. Проверка category=='CommonModules' — ТАВТОЛОГИЯ: module_hint уже применил этот фильтр. find_call_hierarchy движений не найдет: обработчик зовет ПЛАТФОРМА",
            "поток целиком → get_object_profile('ДокИмя', include_flow=True)",
        ],
        "full": [
            "search_objects('ДокИмя') → найти документ по бизнес-имени",
            "find_register_movements('ДокИмя') → Posting/CFE-фильтрованные кандидаты; main-строки — снимок индекса",
            "сигналы: is_postable=False -> нет движений; suppressed_main_code_registers -> handler-only main не active; posting_handler_present при code_registers=0 -> прямых Движения.X нет, возможны делегаты",
            "ТРАССИРОВКА: исполняй result['hint']: сервер вернул регистр, делегата (получатель может быть НЕ РАЗРЕШЕН), dotless local-global или «не пишет»; CFE через read_file недоступен",
            "ЛОВУШКИ: (1) точка НЕ доказывает модуль: слева бывает ПЕРЕМЕННАЯ/РЕКВИЗИТ; одноименный модуль молча отдаст ЧУЖОЕ тело — верь метке hint. (2) category=='CommonModules' при 'ОбщийМодуль.' — ТАВТОЛОГИЯ: это фильтр module_hint. (3) проверяй d.get('definitions'): пусто = definitions=[], без индекса ключа нет. (4) ПУСТО — исполни live safe_grep-маршрут из hint; _truncated = остановка на 50 кандидатах, без него каталог пройден",
            "НАШЕЛ Движения.X: find_register_writers('Регистр') даст static-кандидатов; Posting/CFE проверь forward, измененный после build main-файл — живьем. Набор записей helper не найдет — ищи регистр через git_search, иначе safe_grep",
            "ОбработкаПроведения зовет ПЛАТФОРМА: callers=0 норма, но ЯВНЫЙ BSL-вызов хелперы покажут. Движения ищи через hint, call-хелперами трассируй ДЕЛЕГАТА; include_triggers — лишь CFE-перехват",
            "analyze_document_flow('ДокИмя') → проводки + подписки + регзадания",
            "find_event_subscriptions('ДокИмя', event_filter=['BeforeWrite','OnWrite','Posting','Проведение','ПередЗаписью','ПриЗаписи']) → подписки документа",
            "ALT: search_methods('Проведение') при нестандартном имени",
        ],
    },
    "распределение": {
        "compact": [
            "search_objects('распределение') → объекты по синониму",
            "get_object_profile('ДокИмя') → за 1 вызов: регистры + подписки + структура + модули",
            "find_register_writers('РегистрИмя') → статические кандидаты; методы → search_methods('Распредел')",
        ],
        "full": [
            "search_objects('распределение') → объекты по синониму",
            "search_methods('Распредел') → все методы распределения",
            "find_by_type('AccumulationRegisters', 'Распредел') → регистры распределения",
            "find_register_writers('РегистрИмя') → статические reverse-кандидаты; Posting/CFE проверь через forward, свежесть main-кода — по живому файлу",
            "analyze_document_flow('ДокИмя') → полный flow документа распределения",
            "analyze_subsystem('РаспределениеЗатрат') → все объекты домена",
            "find_callers_context('Распределить') → цепочка вызовов",
            "ALT: grep('Распредел', path=module) для поиска в конкретных модулях",
        ],
    },
    "печать": {
        "compact": [
            "search_objects('печат') → объекты печати по синониму",
            "find_print_forms('ОбъектИмя') → печатные формы объекта",
            "search_methods('Печат') → методы формирования печати",
        ],
        "full": [
            "search_objects('печат') → объекты печати по синониму",
            "find_print_forms('ОбъектИмя') → все печатные формы объекта",
            "find_module('Печать') → модули подсистемы печати",
            "search_methods('Печат') → методы формирования печатных форм",
            "find_callers_context('СформироватьПечатнуюФорму') → цепочка вызовов",
            "analyze_subsystem('Печать') → все объекты подсистемы печати",
            "find_by_type('CommonModules', 'Печат') → общие модули печати",
            "ALT: grep('ТабличныйДокумент', path=module) для поиска макетов",
        ],
    },
    "права": {
        "compact": [
            "search_objects('ОбъектИмя') → найти объект по бизнес-имени",
            "get_object_profile('ОбъектИмя') → за 1 вызов: роли (roles) + функц.опции (functional_options) + структура",
            "детальнее: find_roles('ОбъектИмя') по ролям; find_functional_options('ОбъектИмя', limit=10) по опциям (limit= ИМЕНОВАННО — per-bucket cap + total/returned/has_more; без него на «жирных» объектах обрыв по max_output_chars)",
        ],
        "full": [
            "search_objects('ОбъектИмя') → найти объект по бизнес-имени",
            "find_roles('ОбъектИмя') → роли с правами на объект (чтение, запись, и т.д.)",
            "find_by_type('Roles') → полный список ролей конфигурации",
            "find_functional_options('ОбъектИмя', limit=10) → функциональные опции; limit= (именованно!) режет xml_options и code_options КАЖДЫЙ до N + total/returned/has_more — без него обрыв по max_output_chars",
            "search_methods('ПравоДоступа') → проверки прав в коде",
            "search_methods('РольДоступна') → программные проверки ролей",
            "analyze_subsystem('УправлениеДоступом') → все объекты подсистемы прав",
            "ALT: grep('ПравоДоступа|РольДоступна', path=module) в конкретных модулях",
        ],
    },
    "интеграция": {
        "compact": [
            "search_objects('обмен') или search_objects('сервис') → объекты интеграции по синониму",
            "find_http_services() → HTTP endpoints (REST API)",
            "find_web_services() → SOAP операции",
        ],
        "full": [
            "search_objects('обмен') или search_objects('сервис') → объекты интеграции по синониму",
            "find_http_services() → HTTP endpoints (REST API)",
            "find_web_services() → SOAP операции",
            "find_xdto_packages() → XDTO контракты данных",
            "plans = find_by_type('ExchangePlans') → получить имена планов обмена",
            "find_exchange_plan_content('КонкретноеИмяПлана') → состав плана (передать реальное имя из шага 4)",
            "all_jobs = find_scheduled_jobs() → затем отфильтровать: [j for j in all_jobs if any(k in j['name'] for k in ('Обмен','Exchange','Синхрониз','Загруз','Выгруз'))]",
        ],
        "code_hint": (
            "# Готовый код для интеграционного анализа (можно вставить в rlm_execute):\n"
            "hs = find_http_services()\n"
            "ws = find_web_services()\n"
            "xdto = find_xdto_packages()\n"
            "plans = find_by_type('ExchangePlans')\n"
            "plan_names = sorted(set(p['object_name'] for p in plans))\n"
            "print(f'HTTP: {len(hs)}, SOAP: {len(ws)}, XDTO: {len(xdto)}, Plans: {len(plan_names)}')\n"
            "for name in plan_names[:3]:\n"
            "    ep = find_exchange_plan_content(name)\n"
            "    print(f'  {name}: {len(ep)} objects')\n"
            "all_jobs = find_scheduled_jobs()\n"
            "kw = ('Обмен','Exchange','Синхрониз','Загруз','Выгруз')\n"
            "ex_jobs = [j for j in all_jobs if any(k in j['name'] for k in kw)]\n"
            "print(f'Exchange jobs: {len(ex_jobs)} of {len(all_jobs)}')"
        ),
    },
    "события формы": {
        "compact": [
            "search_objects('ОбъектИмя') → найти объект по бизнес-имени",
            "forms = parse_form('ОбъектИмя') → список форм с handlers, commands, attributes, module_path",
            "for f in forms: if f['module_path']: extract_procedures(f['module_path'])",
        ],
        "full": [
            "search_objects('ОбъектИмя') → найти объект по бизнес-имени",
            "forms = parse_form('ОбъектИмя') → все формы с handlers/commands/attributes/module_path",
            "parse_form('ОбъектИмя', handler='ПроцИмя') → обратный поиск: к чему привязана процедура",
            "forms_with_code = [f for f in forms if f['module_path']]  # формы с BSL-модулем",
            "for f in forms_with_code: extract_procedures(f['module_path']) → процедуры каждой формы",
            "for f in forms_with_code: read_procedure(f['module_path'], 'ПриСозданииНаСервере') → код инициализации",
            "find_callers_context('ОбработчикИмя') → кто вызывает обработчик",
            "parse_object_xml(path) → метаданные объекта (реквизиты, ТЧ)",
        ],
    },
    "ссылки": {
        "compact": [
            "find_references_to_object('Справочник.Имя') → метаданные-XML ссылки (типы, владелец, подсистемы, права…)",
            "find_code_usages('Документ.Имя') → ОБРАЩЕНИЯ В КОДЕ (Документы.X, \"ДокументСсылка.X\", запросы Документ.X.ТЧ)",
            "Нужны оба сразу — find_references_to_object('Документ.Имя', include_code=True)",
            "Если объект упоминается через DefinedType — find_defined_types('Имя') раскроет составляющие",
        ],
        "full": [
            "res = find_references_to_object('Справочник.ВидыПодарочныхСертификатов')  # метаданные-XML",
            "print(res['by_kind'], res['total'])",
            "Filter by kind: find_references_to_object('Справочник.Х', kinds=['attribute_type'])",
            "Членство в подсистемах: find_references_to_object('Документ.Х', kinds=['subsystem_content']); на устаревшем индексе состав подсистем мог измениться — проверь live (rlm_index build)",
            "code = find_code_usages('Документ.ПриобретениеТоваровУслуг')  # обращения в коде",
            "print(code['by_kind'])  # {'manager':.., 'ref_type':.., 'query':..}; u['member'] = имя ТЧ для query",
            "Filter by kind: find_code_usages('Документ.Х', kind='query') — только обращения к ТЧ/реквизитам в запросах",
            "Полная картина одним вызовом: find_references_to_object('Документ.Х', include_code=True) → + ключи code_*",
            "Если res['partial'] / code['partial'] — индекс старый, запустить rlm_index(action='build')",
            "Если объект упоминается через DefinedType — find_defined_types('ИмяТипа') раскроет составляющие; затем find_references_to_object('DefinedType.Имя') найдёт обратные использования",
            "Аналог конфигуратора 'Найти ссылки' (XML — issue #10; код — v1.14.0). Код расширений вне индекса.",
        ],
        "code_hint": (
            "# 1) Декларативные ссылки из метаданных-XML:\n"
            "res = find_references_to_object('Справочник.ВидыПодарочныхСертификатов')\n"
            "print(f\"meta total={res['total']} partial={res['partial']}\")\n"
            "print('by_kind:', res['by_kind'])\n"
            "# 2) Обращения В КОДЕ (обратный поиск использований):\n"
            "code = find_code_usages('Документ.ПриобретениеТоваровУслуг')\n"
            "print(f\"code total={code['total']} by_kind={code['by_kind']}\")\n"
            "for u in code['usages'][:20]:\n"
            "    tail = f\" .{u['member']}\" if u['member'] else ''\n"
            "    print(f\"  {u['kind']:8s} {u['path']}:{u['line']}{tail}\")\n"
            "# 3) Метаданные + код в одном вызове:\n"
            "full = find_references_to_object('Документ.ПриобретениеТоваровУслуг', include_code=True)\n"
            "print(full['total'], full['code_total'], full['code_by_kind'])\n"
            "# Раскрыть DefinedType и найти обратные использования:\n"
            "dt = find_defined_types('ДенежнаяСумма')\n"
            "refs = find_references_to_object('DefinedType.ДенежнаяСумма')\n"
            "print(dt['types'], refs['by_kind'])"
        ),
    },
    "перечисления": {
        "compact": [
            "find_enum_values('ИмяПеречисления') → значения с синонимами",
            "Если ищется тип статуса/состояния — find_attributes(name='Статус') покажет где используется",
        ],
        "full": [
            "ev = find_enum_values('СтатусыЗаказовКлиентов') → {name, synonym, values:[{name, synonym}]}",
            "find_attributes(name='Статус') → реквизиты типа EnumRef.Статус* для понимания контекста",
            "search_objects('статус') → объекты с синонимом 'статус' (документы, регистры с этим полем)",
            "get_object_full_structure(doc) → s['enum_values_for_typed_refs'] уже раскроет связанные перечисления",
        ],
        "code_hint": (
            "ev = find_enum_values('СтатусыЗаказовКлиентов')\n"
            "print(f\"{ev['name']} ({ev['synonym']}):\")\n"
            "for v in ev['values']:\n"
            "    print(f\"  {v['name']}: {v['synonym']}\")"
        ),
    },
    "ввод на основании": {
        "compact": [
            "find_based_on_documents('ДокИмя') → {can_create_from_here, can_be_created_from}",
            "Двунаправленно: что создаётся ИЗ документа и НА основании чего создаётся документ",
        ],
        "full": [
            "rel = find_based_on_documents('ПриобретениеТоваровУслуг')",
            'for d in rel[\'can_create_from_here\']: print(f\'  -> {d.get("ref") or d["document"]} (via={d.get("via","direct")})\')  # metadata ref канонический: Catalog.X/Document.X',
            "for d in rel['can_be_created_from']: print(f'  <- {d[\"type\"]}')",
            "search_methods('Заполнить') → процедуры заполнения шапки/ТЧ при вводе на основании",
            "find_event_subscriptions('ДокИмя', event_filter=['Filling','ОбработкаЗаполнения']) → подписки на заполнение",
        ],
    },
    "структура объекта": {
        "compact": [
            "get_object_profile('ИмяОбъекта') → за 1 вызов: структура + модули + регистры + подписки + роли + ФО",
            "только метаданные (реквизиты/ТЧ/предопределённые) → get_object_full_structure('ИмяОбъекта'); только код → get_object_modules('ИмяОбъекта')",
        ],
        "full": [
            "s = get_object_full_structure('ИмяОбъекта')",
            "Анализируй s['attributes'] (kind=attribute), s['tabular_sections'][i]['columns'] (kind=ts_attribute)",
            "Для регистров: s['dimensions'] + s['resources']",
            "s['predefined_items'] — предопределённые значения с типами",
            "s['enum_values_for_typed_refs'] — типы EnumRef уже раскрыты в значения",
            "s['forms'] — список form_name; для деталей формы → parse_form(name)",
            "Если нужен код: find_module(name) → modules → extract_procedures(path)",
        ],
        "code_hint": (
            "s = get_object_full_structure('РеализацияТоваровУслуг')\n"
            "print(f\"{s['object_name']}: posting={s.get('posting')}\")\n"
            "print(f\"  attrs={len(s['attributes'])}, ts={len(s['tabular_sections'])}, forms={len(s['forms'])}\")\n"
            "for ts in s['tabular_sections']:\n"
            "    print(f\"  ТЧ {ts['name']}: {len(ts['columns'])} cols\")\n"
            "for ref, vals in s['enum_values_for_typed_refs'].items():\n"
            "    print(f\"  {ref}: {[v['name'] for v in vals]}\")"
        ),
    },
    "тип реквизита": {
        "compact": [
            "find_predefined('ИмяСубконто') — if asking about subconto/predefined",
            "find_attributes('ИмяРеквизита') — if asking about attribute type",
            "Done — types are in the result",
        ],
        "full": [
            "Step 1: find_predefined('Name') or find_attributes('Name')",
            "Step 2: If not found, parse_object_xml('Category/ObjectName') for on-demand parse",
            "Step 3: Report types from result",
        ],
        "code_hint": (
            "# Тип субконто / предопределённого:\n"
            "items = find_predefined('РеализуемыеАктивы')\n"
            "for i in items:\n"
            "    print(i['item_name'], i['types'])\n\n"
            "# Тип реквизита:\n"
            "attrs = find_attributes('Организация')\n"
            "for a in attrs:\n"
            "    print(a['object_name'], a['attr_name'], a['attr_type'])"
        ),
    },
    "иерархия вызовов": {
        "compact": [
            "find_call_hierarchy('ПроцИмя', direction='callers', depth=2) → транзитивные вызывающие на 2 уровня",
            "Одноименный ОБЪЕКТНЫЙ метод, который РЕАЛЬНО зовут из кода (ЗаполнитьДокумент, ОтразитьВУчете) → добавь module_hint='Документ.X' для точности (exact-режим)",
            "ПЛАТФОРМЕННЫЕ обработчики (ОбработкаПроведения, ПередЗаписью, ПриЗаписи, ОбработкаЗаполнения): вызов от ПЛАТФОРМЫ в граф не попадает → callers=0 это НОРМА, а не мертвый код. По имени хелпер их НЕ исключает — ЯВНЫЙ вызов из BSL он ПОКАЖЕТ; но движения так не найти: читай тело и трассируй ДЕЛЕГАТА → рецепт «проведение»",
            "Экспортный метод общего модуля с уникальным во всей БД именем → hint не нужен (exact сам); если root_exact=False — имя неуникально, передай module_hint",
            "Для одного уровня + контекст строк используй find_callers_context('ПроцИмя')",
            "direction='callees' пока не поддерживается (возвращает error-dict с hint)",
        ],
        "full": [
            "tree = find_call_hierarchy('ЗаполнитьДокумент', module_hint='Документ.РеализацияТоваровУслуг', depth=2) → одноименный ОБЪЕКТНЫЙ метод, который РЕАЛЬНО зовут из кода",
            "ПЛАТФОРМЕННЫЕ обработчики модуля объекта (ОбработкаПроведения, ПередЗаписью, ПриЗаписи, ОбработкаЗаполнения): вызов от ПЛАТФОРМЫ в граф ВЫЗОВОВ не попадает → callers=0 это НОРМА, а не мертвый код, и module_hint этого не лечит. НО по имени хелпер обработчики НЕ исключает: если BSL-код где-то ЯВНО зовет ОбработкаПроведения(...), такое ребро в индексе ЕСТЬ и хелпер его ПОКАЖЕТ — не игнорируй его. Просто ЧЕМ пишутся движения, так не найти: трассируй ДЕЛЕГАТА из тела обработчика → рецепт «проведение». include_triggers ребра «зовет платформа» не добавит (такого edge_type нет), но CFE-перехват самого обработчика ПОКАЖЕТ",
            "module_hint привязывает КОРЕНЬ к одному модулю → exact-режим убирает ложные звенья от однофамильцев",
            "Формы hint: rel_path | 'Документ.X'/'Document.X' | голый object_name; глубже обход exact идёт сам (по rel_path caller'а)",
            "Доверие: _meta.root_exact (включился ли exact на корне), _meta.exact_rows/fallback_rows, node['meta'].target_exact, node['target_key']=rel_path::метод",
            "Дерево по узлам: tree['tree'][i] = {name, target_hint, target_key, meta, callers}",
            "tree['truncated_targets'] — список узлов с >200 callers (популярные имена)",
            "tree['visited'] — общее число уникальных узлов в обходе",
            "БЕЗ hint для одноимённого объектного метода exact на корне НЕ включится → _meta.root_exact=False, раскроет всех носителей по имени",
            "_meta.node_budget_exceeded=True → широкий корень без hint упёрся в visited_cap, дерево частичное; hint и точнее, и ограничивает обход",
            "ALT: find_callers_context('ПроцИмя') для одного уровня с контекстом строк и file:line",
            "ALT: find_callers_context('ПроцИмя', module_hint='ОбщегоНазначения') для disambig'а омонимов",
        ],
        "code_hint": (
            "# NB: корень — метод, который РЕАЛЬНО зовут из BSL. На платформенном обработчике\n"
            "# (ОбработкаПроведения и др.) этот пример вернет callers=0 — его зовет ПЛАТФОРМА.\n"
            "tree = find_call_hierarchy('ЗаполнитьДокумент', module_hint='Документ.РеализацияТоваровУслуг', depth=2)\n"
            "m = tree.get('_meta', {})\n"
            "print(f\"root_exact={m.get('root_exact')} exact_rows={m.get('exact_rows')} fallback_rows={m.get('fallback_rows')}\")\n"
            "for node in tree.get('tree', []):\n"
            "    mark = 'EXACT' if node['meta']['target_exact'] else 'name'\n"
            "    print(f\"{node['name']} [{mark}] ({len(node.get('callers', []))} callers)\")\n"
            "    for caller in node.get('callers', [])[:5]:\n"
            "        print(f\"  ← {caller['caller_name']} ({caller['object_name']} {caller['module_path']}:{caller['line']})\")"
        ),
    },
    "расширения": {
        "compact": [
            "get_overrides('ИмяОбъекта') → перехваты объекта из индекса (source='index')",
            "get_overrides() → вся конфигурация: сначала partial. False → агрегаты по ВСЕМ перехватам; True → нижняя оценка, причины в _meta.failed_extension_roots. НЕ группируй overrides — это срез 200",
            "extract_procedures(path) → поле overridden_by у перехваченных методов",
            "read_procedure(path, name, include_overrides=True) → оригинал + тело расширения с аннотацией",
            "find_module/find_attributes/parse_object_xml/search видят объекты ext конфигов; пути начинаются на '../' — передавай их в read_procedure/extract_procedures напрямую",
        ],
        "full": [
            "get_overrides() → перехваты конфигурации. СНАЧАЛА partial: False — total/агрегаты по полному выбранному источнику; True — только по прочитанной части, причины в _meta.failed_extension_roots. overrides = отсортированный срез 200, total/truncated сигналят обрезку; сводку по срезу не строй",
            "get_overrides('ИмяОбъекта') → перехваты одного объекта (метод, аннотация, файл расширения)",
            "by_annotation/by_object_top/by_extension_top — DICT {имя: количество} (top-20 у двух последних): итерируй .items(), срез — list(d.items())[:N]; это НЕ список записей",
            "target_method_line=None — валидно: перехват предопределенного события платформы (ПриЗаписи, ОбработкаПроведения) без текстового объявления в базовом модуле либо строка без source-привязки. Не считай это ошибкой индекса и не перепроверяй",
            "extract_procedures(path) → у перехваченных методов поле overridden_by={ext, annotation, ext_method}",
            "read_procedure(path, name) → ТОЛЬКО оригинал (по умолчанию, без перехватов)",
            "read_procedure(path, name, include_overrides=True) → оригинал + секция «=== Перехвачен &Аннотация в расширении ИмяРасш ===»",
            "ALT live (без индекса): detect_extensions() + find_ext_overrides(ext_path, 'ИмяОбъекта') — на СВЕЖЕМ индексе совпадает с index по количеству; на устаревшем может расходиться (контракт: index = снимок)",
            "get_index_info() → has_extension_overrides, extension_overrides — статистика индекса",
            "Объекты и модули расширения видны из main-сессии: find_module/find_by_type/find_attributes/find_predefined/parse_object_xml/search возвращают пути с префиксом '../'. Передавай эти пути напрямую в read_procedure/extract_procedures — они читают расширение внутренне.",
            "read_file/grep/glob_files на путях с '../' дадут PermissionError (sandbox base-only)",
        ],
        "code_hint": (
            "res = get_overrides()\n"
            "print(f\"total={res['total']} truncated={res['truncated']} partial={res['partial']} source={res['source']}\")\n"
            "if res['partial']:\n"
            "    print('НИЖНЯЯ ОЦЕНКА; недочитанные roots:', res.get('_meta', {}))\n"
            "print('по аннотациям:', res['by_annotation'])\n"
            "print('топ объектов:', list(res['by_object_top'].items())[:5])\n"
            "print('топ расширений:', list(res['by_extension_top'].items())[:5])\n"
            "print(f\"объектов={res['unique_objects']} методов={res['unique_methods']} расширений={res['unique_extensions']}\")\n"
            "# Не группируй res['overrides'] — это срез; агрегаты полны только при partial=False\n"
            "# Детализация топ-объекта (имя берем из АГРЕГАТА, а не из среза):\n"
            "top_obj = next(iter(res['by_object_top']), None)\n"
            "if top_obj:\n"
            "    for o in get_overrides(top_obj)['overrides'][:5]:\n"
            "        print(f\"  {o['annotation']} {o['target_method']} ← {o['extension_name']}\")"
        ),
    },
    "достижимость": {
        "compact": [
            "if 'error' in res (find_path): имя многозначно (определено в >1 модуле) без своего hint → добавь to_hint/from_hint из res['candidates'] (file надёжнее всего); проверяй ПЕРЕД found/budget_exceeded",
            "find_path('МетодА', 'МетодБ') → есть ли путь вызовов from→to (forward)",
            "found=False + _meta.budget_exceeded → обход обрезан, сузь max_depth/дай hint",
            "find_call_hierarchy('Метод', include_triggers=True) → callers + триггеры (подписки/формы/рег.задания/CFE)",
        ],
        "full": [
            "СНАЧАЛА if 'error' in res: find_path для многозначного имени (NOCASE-COUNT>1 = разные модули) БЕЗ своего hint вернёт {error, hint, candidates:[{object_name, category, module_type, file, line}], _meta:{ambiguous, ambiguous_arg}} вместо обхода — добавь to_hint/from_hint (file из candidates) и повтори; проверяй error ПЕРЕД found/budget_exceeded",
            "find_path('НизкоуровневыйМетод', 'ОбработчикUI', max_depth=4) → достижимость по графу ВЫЗОВОВ (forward path from→…→to)",
            "проверь _meta.precision: 'exact' = путь доказан по callee_key; 'heuristic' = по имени (старый индекс/FS/однофамильцы)",
            "одноимённые методы → from_hint/to_hint ('Документ.X' | rel_path | object_name) пинят концы к модулю",
            "call_line элемента пути = строка ВЫЗОВА к СЛЕДУЮЩЕМУ узлу (ребро), НЕ определения; у терминального (to) None",
            "find_call_hierarchy('Метод', module_hint='Документ.X', include_triggers=True) → дерево callers + не-call триггеры на КАЖДОМ узле",
            "_meta.budget_exceeded=True → обход упёрся в visited_cap, found=False НЕ доказывает отсутствие пути",
            "ALT: find_callers_context('Метод') для одного уровня callers с контекстом строк и file:line",
        ],
    },
    "путь данных": {
        "compact": [
            "find_data_path('Документ.X', 'РегистрНакопления.Y') → цепочка ссылок метаданных from→to",
            "endpoints ОБЯЗАНЫ быть с префиксом (Справочник./Документ./…), иначе error+hint",
            "kinds=['attribute_type'] → фильтр по виду ссылки",
        ],
        "full": [
            "find_data_path('Документ.РеализацияТоваровУслуг', 'РегистрНакопления.Продажи') → N-hop путь по графу МЕТАДАННЫХ (атрибуты/владелец/основание/...)",
            "endpoints С ПРЕФИКСОМ для ОБОИХ концов (Справочник.X/Catalog.X, Документ.Y/Document.Y); bare-имя → структурный hint без обхода",
            "каждый элемент path = РЕБРО {from, to, kind, used_in, path, line}",
            "kinds=[...] → ограничить виды ссылок (attribute_type, owner, based_on, subsystem_content, ...)",
            "partial=True → нет таблицы metadata_references (старый индекс) — нужен ребилд",
            "_meta.budget_exceeded=True → обход упёрся в node_budget (~400), сузь max_depth",
            "ALT: find_references_to_object('Справочник.X') → все ВХОДЯЩИЕ ссылки на объект (обратное направление, 1 уровень)",
        ],
    },
}

_RECIPE_ALIASES: dict[str, str] = {
    "обмен": "интеграция",
    "синхрониз": "интеграция",
    "exchange": "интеграция",
    # «интеграция» — http/soap/xdto/rest/мэдо
    "http": "интеграция",
    "http-сервис": "интеграция",
    "http-сервисы": "интеграция",
    "http services": "интеграция",
    "веб-сервис": "интеграция",
    "веб-сервисы": "интеграция",
    "web service": "интеграция",
    "soap": "интеграция",
    "rest": "интеграция",
    "rest api": "интеграция",
    "xdto": "интеграция",
    "планы обмена": "интеграция",
    "мэдо": "интеграция",
    "межведомственный": "интеграция",
    "обработчики формы": "события формы",
    "элементы формы": "события формы",
    "кнопки формы": "события формы",
    "формы": "события формы",
    "form events": "события формы",
    "form handlers": "события формы",
    "события документа": "проведение",
    "обработчики событий": "проведение",
    "передзаписью": "проведение",
    "призаписи": "проведение",
    "document events": "проведение",
    "субконто": "тип реквизита",
    "тип субконто": "тип реквизита",
    "предопределённ": "тип реквизита",
    "предопределенн": "тип реквизита",
    "attribute type": "тип реквизита",
    "references": "ссылки",
    "where used": "ссылки",
    "где используется": "ссылки",
    "найти ссылки": "ссылки",
    "поиск ссылок": "ссылки",
    "в свойствах": "ссылки",
    "вхождения": "ссылки",
    "где используется в коде": "ссылки",
    "использования в коде": "ссылки",
    "обращения к объекту": "ссылки",
    "обращения в коде": "ссылки",
    "code usages": "ссылки",
    "использования тч": "ссылки",
    "тч в запросах": "ссылки",
    # «проведение»
    "движения": "проведение",
    "регистры документа": "проведение",
    "проводки": "проведение",
    "posting": "проведение",
    "register movements": "проведение",
    "как проводится документ": "проведение",
    "движения по регистрам": "проведение",
    "регистры": "проведение",
    "регистры накопления": "проведение",
    "регистры сведений": "проведение",
    "movements": "проведение",
    # «печать»
    "макеты": "печать",
    "печатные формы": "печать",
    "templates": "печать",
    # «права»
    "роли": "права",
    "role": "права",
    "rights": "права",
    "access": "права",
    "rls": "права",
    "restriction": "права",
    "ограничение доступа": "права",
    "access restriction": "права",
    "права доступа": "права",
    "функциональные опции": "права",
    "functional options": "права",
    # «перечисления»
    "enum": "перечисления",
    "значения перечисления": "перечисления",
    "статусы": "перечисления",
    "состояния": "перечисления",
    # «ввод на основании»
    "based on": "ввод на основании",
    "основание": "ввод на основании",
    "can_create_from": "ввод на основании",
    "ввод документов на основании": "ввод на основании",
    # «структура объекта»
    "карточка объекта": "структура объекта",
    "полная структура": "структура объекта",
    "реквизиты документа": "структура объекта",
    "object structure": "структура объекта",
    "структура справочника": "структура объекта",
    "структура регистра": "структура объекта",
    "табличные части": "структура объекта",
    "реквизиты": "структура объекта",
    # «иерархия вызовов»
    "иерархия": "иерархия вызовов",
    "call hierarchy": "иерархия вызовов",
    "callers": "иерархия вызовов",
    "вызывающие": "иерархия вызовов",
    "кто вызывает": "иерархия вызовов",
    "цепочка вызовов": "иерархия вызовов",
    # «расширения»
    "перехват": "расширения",
    "перехваты": "расширения",
    "override": "расширения",
    "overrides": "расширения",
    "extension": "расширения",
    "extensions": "расширения",
    "ext_overrides": "расширения",
    "аннотации": "расширения",
    "&перед": "расширения",
    "&после": "расширения",
    "&вместо": "расширения",
    # «достижимость»
    "reachability": "достижимость",
    "путь вызовов": "достижимость",
    "доходит ли": "достижимость",
    "вызывает ли": "достижимость",
    "find_path": "достижимость",
    # «путь данных»
    "data path": "путь данных",
    "как связаны": "путь данных",
    "граф данных": "путь данных",
    "цепочка ссылок": "путь данных",
    "связь объектов": "путь данных",
    "find_data_path": "путь данных",
}

_STRATEGY_IO_SECTION = """\
File I/O:
  read_file(path), read_files(paths)       → str / dict (numbered in MCP session)
  grep(pattern, path), grep_summary(pattern), grep_read(pattern, path)
  glob_files(pattern), tree(path, max_depth=3), find_files(name)
  NOTE: For BSL modules prefer find_module()/find_by_type() over glob_files()
  NOTE: tree('.') on large configs produces too much output — use tree('SubDir') or find_files()
LLM (if available):
  llm_query(prompt, context='')            → str (keep context <3000 chars; '[EMPTY]/[ERROR]' prefix or '[TRUNCATED]' tail = incomplete answer)
  llm_query_batched(prompts, context)      → [str]"""


def build_helpers_table(registry: dict) -> str:
    """Build the HELPERS section of strategy text from registry."""
    lines = ["== HELPERS (call help('keyword') for usage examples and return formats) =="]
    for cat_key, cat_label in _CATEGORY_ORDER:
        entries = [(name, entry["sig"]) for name, entry in registry.items() if entry["cat"] == cat_key]
        if not entries:
            continue
        lines.append(f"{cat_label}:")
        for _, sig in entries:
            lines.append(f"  {sig}")
    lines.append(_STRATEGY_IO_SECTION)
    return "\n".join(lines)


def _git_search_routing(registry: dict | None) -> str:
    """One routing block for git_search, only when it is in the live registry.

    Presence is derived from the registry (gated by git availability at session
    start) — no extra parameter needed. Disambiguates the three search intents so
    the agent doesn't oscillate between safe_grep and git_search.
    """
    if not registry or "git_search" not in registry:
        return ""
    return (
        "\n== FULL-TEXT SEARCH (git detected) ==\n"
        "Sources are under git → git_search is available: full-text over ALL files,\n"
        "including raw XML/forms/rights/DCS/queries that name-based helpers and the index never see.\n"
        "Pick the tool by intent (avoid tool-oscillation):\n"
        "  - by NAME (object/procedure/attribute) → search / find_module / find_attributes\n"
        "  - inside a KNOWN module → safe_grep(pattern, name_hint)   (scoped, fast)\n"
        "  - ANY substring ANYWHERE, incl. XML/forms/query text → git_search(pattern[, path, file_types])\n"
        "Anti-noise on common tokens: git_search(tok, mode='files') first (which files), or narrow\n"
        "file_types/path, then drill down. Mind max_results / the {'_truncated': True} sentinel."
    )


def _match_recipe(query: str) -> str | None:
    """Match query text against _BUSINESS_RECIPES domain keys and aliases."""
    q = query.lower()
    for domain in _BUSINESS_RECIPES:
        if domain in q:
            return domain
    for alias, domain in _RECIPE_ALIASES.items():
        if alias in q:
            return domain
    return None


def get_strategy(
    effort: str,
    format_info,
    detected_prefixes: list[str] | None = None,
    extension_context=None,
    ext_overrides: dict | None = None,
    registry: dict | None = None,
    idx_stats: dict | None = None,
    idx_warnings: list[str] | None = None,
    query: str = "",
) -> str:
    """Public strategy builder. Routes to slim or full mode based on
    ``RLM_STRATEGY_MODE`` env var.

    Modes:
      - ``slim`` (default, production): condensed strategy + ``rlm_help`` MCP tool.
      - ``full``: legacy strategy (safe fallback for weak models). Byte-for-byte
        with the historical output EXCEPT the shared _extension_strategy() block,
        which is token-bounded in both modes (see _build_full_strategy).

    Unknown values fall back to ``slim``.
    """
    mode = os.environ.get("RLM_STRATEGY_MODE", "slim").lower()
    if mode not in ("slim", "full"):
        mode = "slim"
    builder = _build_full_strategy if mode == "full" else _build_slim_strategy
    return builder(
        effort,
        format_info,
        detected_prefixes,
        extension_context,
        ext_overrides,
        registry,
        idx_stats,
        idx_warnings,
        query,
    )


def get_strategy_mode() -> str:
    """Resolved strategy mode for the current environment ('slim' or 'full')."""
    mode = os.environ.get("RLM_STRATEGY_MODE", "slim").lower()
    return mode if mode in ("slim", "full") else "slim"


# --- Generic mode: чужой формат исходников БЕЗ единого .bsl (v1.32.0) ---
# BSL-хелперы в такой сессии не загружаются, поэтому маршрутная карта не имеет
# права ссылаться ни на один из них. Строится отдельно от slim/full — те
# описывают ИМЕННО BSL-инструментарий и остаются нетронутыми.

_GENERIC_LLM_GUIDANCE_SENTENCE = " Use llm_query() for semantic analysis."

_GENERIC_STRATEGY_BODY = """\
== UNSUPPORTED SOURCE FORMAT - GENERIC MODE ==
This directory is neither a Configurator dump (cf) nor a 1C:EDT project, and no .bsl file was found.
1C/BSL helpers are not loaded. Use only generic file exploration.

== WORKFLOW ==
Write Python in rlm_execute and print() compact results.
  1. tree('.', max_depth=2) or glob_files('**/*') - inspect the tree
  2. grep(pattern, path) / grep_summary(pattern, path) - locate text
  3. read_file(path) / read_files(paths) - read only relevant files

== HELPERS ==
  read_file(path), read_files(paths)
  grep(pattern, path), grep_summary(pattern, path)
  grep_read(pattern, path, max_files=10, context_lines=0)
  glob_files(pattern), tree(path, max_depth=3), find_files(name)"""

_GENERIC_LLM_HELPERS_LINE = "  llm_query(prompt, context=''), llm_query_batched(prompts, context='')"


def build_generic_strategy(effort: str, has_llm_tools: bool = False) -> str:
    """Маршрутная карта сессии без BSL-хелперов (чужой формат, .bsl нет)."""
    effort_key = effort if effort in EFFORT_LEVELS else "medium"
    config = EFFORT_LEVELS[effort_key]
    guidance = config.guidance
    body = _GENERIC_STRATEGY_BODY
    if has_llm_tools:
        body += "\n" + _GENERIC_LLM_HELPERS_LINE
    else:
        guidance = guidance.replace(_GENERIC_LLM_GUIDANCE_SENTENCE, "")
    return "\n".join(
        [
            body,
            f"\n== EFFORT: {effort_key} ==",
            guidance,
            f"Limits: max_execute_calls={config.max_execute_calls}, max_llm_calls={config.max_llm_calls}",
        ]
    )


def _build_full_strategy(
    effort: str,
    format_info,
    detected_prefixes: list[str] | None = None,
    extension_context=None,
    ext_overrides: dict | None = None,
    registry: dict | None = None,
    idx_stats: dict | None = None,
    idx_warnings: list[str] | None = None,
    query: str = "",
) -> str:
    """Legacy ("full") strategy — safe fallback for weak models.

    NB: kept byte-for-byte EXCEPT the shared _extension_strategy() block, which is
    now token-bounded in both modes (per-extension override counts + on-demand
    pointer; detail via RLM_EXT_OVERRIDE_DETAIL). The old per-extension dump cost
    ~64K chars on extension-heavy configs — unacceptable even as a fallback.
    """
    config = EFFORT_LEVELS.get(effort, EFFORT_LEVELS["medium"])

    has_extensions = (
        extension_context is not None
        and extension_context.current.role.value != "unknown"
        and (extension_context.current.role.value == "extension" or extension_context.nearby_extensions)
    )

    parts: list[str] = []

    # --- Extension alert (BEFORE everything else if present) ---
    if has_extensions:
        parts.append(_extension_strategy(extension_context, ext_overrides or {}))

    # --- Base strategy (critical, workflow) ---
    parts.append(_STRATEGY_HEADER)

    # --- Business recipe (dynamic injection based on query) ---
    if query:
        domain = _match_recipe(query)
        if domain:
            level = "compact" if effort in ("low", "medium") else "full"
            recipe = _BUSINESS_RECIPES[domain]
            steps = recipe[level]
            recipe_lines = [f"\n== BUSINESS RECIPE: {domain} =="]
            for i, step in enumerate(steps, 1):
                recipe_lines.append(f"  {i}. {step}")
            code_hint = recipe.get("code_hint")
            if code_hint:
                recipe_lines.append(f"\nReady-to-use code (paste into rlm_execute):\n```python\n{code_hint}\n```")
            parts.append("\n".join(recipe_lines))

    # --- Helpers table (dynamic from registry, or static fallback for IO/LLM) ---
    if registry:
        parts.append(build_helpers_table(registry))
    else:
        parts.append(_STRATEGY_IO_SECTION)

    # --- Full-text search routing (only when git_search is registered) ---
    git_note = _git_search_routing(registry)
    if git_note:
        parts.append(git_note)

    # --- Index status ---
    if idx_stats is not None:
        methods_count = idx_stats.get("methods", 0)
        calls_count = idx_stats.get("calls", 0)
        config_name = idx_stats.get("config_name") or ""
        config_version = idx_stats.get("config_version") or ""
        has_fts = bool(idx_stats.get("has_fts"))

        builder_version = idx_stats.get("builder_version") or "?"
        synonyms_count = idx_stats.get("object_synonyms", 0)

        idx_lines = ["\n== INDEX =="]
        label = f"Index v{builder_version} ({methods_count} methods, {calls_count} call edges"
        if synonyms_count:
            label += f", {synonyms_count} synonyms"
        oa_count = idx_stats.get("object_attributes", 0)
        pi_count = idx_stats.get("predefined_items", 0)
        if oa_count:
            label += f", {oa_count} attributes"
        if pi_count:
            label += f", {pi_count} predefined"
        if config_name:
            label += f", config: {config_name}"
            if config_version:
                label += f" v{config_version}"
        label += ")."
        idx_lines.append(label)

        # Speedup summary
        instant_helpers = ["extract_procedures()", "find_exports()"]
        if calls_count:
            instant_helpers.append("find_callers_context()")
            instant_helpers.append("find_call_hierarchy()")
        instant_helpers.extend(
            [
                "find_event_subscriptions()",
                "find_scheduled_jobs()",
                "find_functional_options()",
            ]
        )
        role_rights_count = idx_stats.get("role_rights", 0)
        if role_rights_count:
            instant_helpers.append("find_roles()")
        register_movements_count = idx_stats.get("register_movements", 0)
        if register_movements_count:
            instant_helpers.extend(["find_register_movements()", "find_register_writers()"])
        file_paths_count = idx_stats.get("file_paths", 0)
        if file_paths_count:
            # find_files() is NOT listed here: it is instant on an index-hit but falls back
            # to an FS-scan on a zero-hit (v1.26.0) — the conditional behaviour is described
            # in the tips below, so it must not appear in the unconditional INSTANT list.
            instant_helpers.extend(["glob_files(indexed)", "tree(indexed)"])
        if synonyms_count:
            instant_helpers.append("search_objects()")
        form_elements_count = idx_stats.get("form_elements", 0)
        if form_elements_count:
            instant_helpers.append("parse_form()")
        bver = int(idx_stats.get("builder_version") or 0)
        if bver >= 8:
            instant_helpers.append("search_regions()")
            instant_helpers.append("search_module_headers()")
        if oa_count:
            instant_helpers.append("find_attributes()")
        if pi_count:
            instant_helpers.append("find_predefined()")
        if oa_count and pi_count:
            instant_helpers.append("get_object_full_structure()")
        instant_helpers.append("search()")
        idx_lines.append(f"INSTANT from index: {', '.join(instant_helpers)}.")

        # FTS/synonym discovery
        if has_fts:
            idx_lines.append(
                "search_methods(query) — full-text search by method name substring. "
                "Use in Step 1 DISCOVER to find methods across the codebase (main FTS: query >= 3 chars)."
            )
        if synonyms_count:
            idx_lines.append(
                f"search_objects(query) — {synonyms_count} object synonyms indexed. "
                "Find 1C objects by Russian business name. Use in Step 1 DISCOVER."
            )

        # Workflow hints
        tips = [
            "INDEX TIPS:",
            "  - find_callers_context() returns instantly — для СКОРОСТИ scope-hint не нужен, ищи по всей кодовой базе.",
            "  - НО module_hint у find_call_hierarchy/find_callers_context — это ТОЧНОСТЬ, не скорость: для одноименных объектных методов, которые РЕАЛЬНО зовут из BSL (ЗаполнитьДокумент, ОтразитьВУчете), hint включает exact-режим (точные ребра по callee_key, без однофамильцев из других модулей); экспортному методу общего модуля hint не нужен, ЕСЛИ его имя уникально во всей БД — иначе (root_exact=False) передай module_hint.",
            "  - ПЛАТФОРМЕННЫЕ обработчики (ОбработкаПроведения, ПередЗаписью): вызов от ПЛАТФОРМЫ в граф не попадает → callers=0 норма, не мертвый код (ЯВНЫЙ BSL-вызов хелпер покажет). Движения ищи не тут: rlm_help(topic='проведение').",
            "  - Batch 5-10 helpers per rlm_execute (index calls are <1ms each).",
            "  - extract_procedures + find_exports + find_callers_context in ONE call is fine.",
            "  - find_attributes() and find_predefined() are INSTANT from index — use for attribute/subconto type questions.",
        ]
        if file_paths_count:
            tips.extend(
                [
                    f"  - File navigation indexed: {file_paths_count} paths (.bsl/.mdo/.xml) — "
                    "glob_files(), tree() instant for supported patterns; find_files() instant on index-hit, else FS-fallback (may scan).",
                    "  - FAST: glob_files('**/*.mdo'), glob_files('Subsystems/**/*.mdo'), glob_files('Documents/**'), tree('Documents'), find_files('name')",
                    "  - SLOW (FS fallback): complex globs with multiple wildcards, glob_files('**/Dir*/*.xml')",
                    "  - For BSL modules: ALWAYS prefer find_module()/find_by_type() over glob_files() — faster and more precise.",
                    "  - NEVER use tree('.') on root of large configs — too much data. Use tree('SubDir') instead.",
                ]
            )
        idx_lines.append("\n".join(tips))

        idx_lines.append(
            "NOTE: Index freshness uses quick check (age + content sampling). "
            "Structural validation (files added/removed) is approximate — "
            "run 'rlm-bsl-index index info' for full check."
        )

        for w in idx_warnings or []:
            idx_lines.append(f"WARNING: {w}")
        parts.append("\n".join(idx_lines))
    else:
        parts.append(
            "\n== INDEX ==\n"
            "No pre-built index. All helpers work via filesystem fallback (slower on large configs).\n"
            "NEVER call rlm_index(action='build') yourself — only the USER decides when to build indexes. "
            "Build runs in background (returns immediately), but requires the project password. Work with what you have.\n"
            "WITHOUT INDEX:\n"
            "  - find_attributes(object_name='X') — WORKS (auto-resolves category via find_module, parses XML live)\n"
            "  - find_predefined(object_name='X') — WORKS (parses Predefined.xml live)\n"
            "  - find_attributes('name') without object_name — EMPTY (cannot scan all files)\n"
            "  - find_predefined('name') without object_name — EMPTY (cannot scan all files)\n"
            "  - search_methods, search_objects, search_regions — EMPTY (require index)\n"
            "  - parse_object_xml(path) — WORKS (always, direct XML read)\n"
            "  - All other helpers — WORK via filesystem (slower but functional)"
        )

    # --- Effort & limits ---
    parts.append(f"\n== EFFORT: {effort} ==")
    parts.append(config.guidance)
    parts.append(
        f"Limits: max_execute_calls={config.max_execute_calls}, "
        f"max_llm_calls={config.max_llm_calls}, "
        f"safe_grep_max_files={config.safe_grep_max_files}"
    )

    # --- Format & paths ---
    if format_info is not None:
        fmt = getattr(format_info, "format_label", None)
        if fmt == "cf":
            parts.append(
                "\n== FORMAT: CF ==\nPaths: CommonModules/Name/Ext/Module.bsl, Documents/Name/Ext/ObjectModule.bsl"
            )
        elif fmt == "edt":
            parts.append("\n== FORMAT: EDT ==\nPaths: CommonModules/Name/Module.bsl, Documents/Name/ObjectModule.bsl")

    # --- Custom prefixes ---
    if detected_prefixes:
        parts.append(
            f"\n== CUSTOM PREFIXES: {detected_prefixes} ==\n"
            "Use these to filter custom objects/subscriptions/roles. "
            "find_custom_modifications() uses them automatically."
        )

    return "\n".join(parts)


# ─────────────────────────────────────────────────────────────────────
#                        SLIM STRATEGY (v1.10.x)
# ─────────────────────────────────────────────────────────────────────

_SLIM_HELP_BLOCK = """\
== HELP ==
This is a slim strategy. For detailed recipes, examples and helper-comparison rules
you MUST call rlm_help(...) BEFORE running rlm_execute on non-trivial queries.
  rlm_help(topic='проведение'|'печать'|'права'|'ссылки'|...)  → готовый план для домена
  rlm_help(category='discovery'|'code'|'xml'|'composite'|'business'|'extension'|'navigation')  → группа хелперов
  rlm_help(helpers=['name1','name2'])  → детальные рецепты для конкретных хелперов
  rlm_help(section='workflow'|'disambiguation'|'performance'|'batching'|'io')  → справочные секции
  rlm_help()  → меню всех topic/category/section
NOTE: in-sandbox help('keyword') (the BSL helper) still works inside rlm_execute for
quick recipe lookup at code-time — that is separate from the rlm_help MCP tool above."""

_SLIM_WORKFLOW_OVERVIEW = """\
== WORKFLOW (overview) ==
Step 0 UNDERSTAND → Step 1 DISCOVER → Step 2 READ → Step 3 TRACE → Step 4 ANALYZE → Step 5 EXTENSIONS.
Full Step 0..5 with helper menu per step → rlm_help(section='workflow').
INSTANT/HYBRID/LIVE breakdown for Step 4 perf → rlm_help(section='performance')."""

_SLIM_DISAMBIGUATION_POINTER = """\
== DISAMBIGUATION (pointer) ==
8 overlapping helper pairs are documented separately. Examples:
  get_object_full_structure vs analyze_object | find_call_hierarchy vs find_callers_context
  find_callers vs find_callers_context | find_register_movements vs analyze_document_flow
  parse_object_xml vs find_attributes | parse_object_xml vs find_roles
  find_register_movements vs find_register_writers | search vs search_methods/search_objects
Get rules, when_a/when_b for any pair → rlm_help(section='disambiguation')
or rlm_help(helpers=['name_a','name_b'], section='disambiguation') for one pair."""


def build_slim_helpers_index(registry: dict) -> str:
    """One line per category with up to 6 helper names, no signatures or descriptions.

    Detailed signatures live in `available_functions` of `rlm_start` and full
    recipes/tags via `rlm_help(category=..., helpers=[...])`.
    """
    if not registry:
        return ""
    lines = ["== HELPERS (compact index — call rlm_help for sigs/recipes) =="]
    for cat_key, cat_label in _CATEGORY_ORDER:
        names = [name for name, entry in registry.items() if entry.get("cat") == cat_key]
        if not names:
            continue
        head = ", ".join(names[:6])
        suffix = f", … ({len(names)} total)" if len(names) > 6 else ""
        lines.append(f"  {cat_label} ({len(names)}): {head}{suffix}")
    return "\n".join(lines)


def _build_slim_strategy(
    effort: str,
    format_info,
    detected_prefixes: list[str] | None = None,
    extension_context=None,
    ext_overrides: dict | None = None,
    registry: dict | None = None,
    idx_stats: dict | None = None,
    idx_warnings: list[str] | None = None,
    query: str = "",
) -> str:
    """Slim strategy — replaces the static workflow/disambiguation/perf walls
    with one-line pointers to the `rlm_help(...)` MCP tool. Token target ~1500-1800.
    """
    config = EFFORT_LEVELS.get(effort, EFFORT_LEVELS["medium"])

    has_extensions = (
        extension_context is not None
        and extension_context.current.role.value != "unknown"
        and (extension_context.current.role.value == "extension" or extension_context.nearby_extensions)
    )

    parts: list[str] = []

    # --- Extension alert (BEFORE everything else if present) ---
    if has_extensions:
        parts.append(_extension_strategy(extension_context, ext_overrides or {}))

    # --- Sandbox preamble + critical block ---
    parts.append(
        "You are exploring a 1C BSL codebase via Python sandbox.\n"
        "Write Python code in rlm_execute. Use print() to output results.\n\n" + STRATEGY_SECTIONS["critical"]
    )

    # --- HELP pointer (slim-only marker) ---
    parts.append(_SLIM_HELP_BLOCK)

    # --- Top-level workflow + perf pointers ---
    parts.append(_SLIM_WORKFLOW_OVERVIEW)

    # --- Auto-routed compact recipe (always 'compact', regardless of effort) ---
    if query:
        domain = _match_recipe(query)
        if domain:
            recipe = _BUSINESS_RECIPES[domain]
            steps = recipe.get("compact") or []
            recipe_lines = [f"\n== BUSINESS RECIPE: {domain} =="]
            for i, step in enumerate(steps, 1):
                recipe_lines.append(f"  {i}. {step}")
            recipe_lines.append(f"Full version + code_hint (if any): rlm_help(topic='{domain}', format='full')")
            parts.append("\n".join(recipe_lines))

    # --- Compact helpers index (categories + names, no signatures) ---
    if registry:
        parts.append(build_slim_helpers_index(registry))

    # --- Full-text search routing (only when git_search is registered) ---
    git_note = _git_search_routing(registry)
    if git_note:
        parts.append(git_note)

    # --- DISAMBIGUATION + STEP 4 perf pointers ---
    parts.append(_SLIM_DISAMBIGUATION_POINTER)

    # --- BATCHING & OUTPUT (kept verbatim — small, contains anti-duplicate rule) ---
    parts.append(STRATEGY_SECTIONS["batching"])

    # --- INDEX (full dynamic block — same as legacy) ---
    parts.append(_render_index_block(idx_stats, idx_warnings))

    # --- Effort & limits ---
    parts.append(f"\n== EFFORT: {effort} ==")
    parts.append(config.guidance)
    parts.append(
        f"Limits: max_execute_calls={config.max_execute_calls}, "
        f"max_llm_calls={config.max_llm_calls}, "
        f"safe_grep_max_files={config.safe_grep_max_files}"
    )

    # --- Format & paths ---
    if format_info is not None:
        fmt = getattr(format_info, "format_label", None)
        if fmt == "cf":
            parts.append(
                "\n== FORMAT: CF ==\nPaths: CommonModules/Name/Ext/Module.bsl, Documents/Name/Ext/ObjectModule.bsl"
            )
        elif fmt == "edt":
            parts.append("\n== FORMAT: EDT ==\nPaths: CommonModules/Name/Module.bsl, Documents/Name/ObjectModule.bsl")

    # --- Custom prefixes ---
    if detected_prefixes:
        parts.append(
            f"\n== CUSTOM PREFIXES: {detected_prefixes} ==\n"
            "Use these to filter custom objects/subscriptions/roles. "
            "find_custom_modifications() uses them automatically."
        )

    return "\n".join(parts)


def _render_index_block(idx_stats: dict | None, idx_warnings: list[str] | None) -> str:
    """Render the dynamic INDEX block. Reused by both full and slim builders.

    Behavior matches the original inline block in `_build_full_strategy` so
    legacy output stays byte-identical when reached via the router.
    """
    if idx_stats is None:
        return (
            "\n== INDEX ==\n"
            "No pre-built index. All helpers work via filesystem fallback (slower on large configs).\n"
            "NEVER call rlm_index(action='build') yourself — only the USER decides when to build indexes. "
            "Build runs in background (returns immediately), but requires the project password. Work with what you have.\n"
            "WITHOUT INDEX:\n"
            "  - find_attributes(object_name='X') — WORKS (auto-resolves category via find_module, parses XML live)\n"
            "  - find_predefined(object_name='X') — WORKS (parses Predefined.xml live)\n"
            "  - find_attributes('name') without object_name — EMPTY (cannot scan all files)\n"
            "  - find_predefined('name') without object_name — EMPTY (cannot scan all files)\n"
            "  - search_methods, search_objects, search_regions — EMPTY (require index)\n"
            "  - parse_object_xml(path) — WORKS (always, direct XML read)\n"
            "  - All other helpers — WORK via filesystem (slower but functional)"
        )

    methods_count = idx_stats.get("methods", 0)
    calls_count = idx_stats.get("calls", 0)
    config_name = idx_stats.get("config_name") or ""
    config_version = idx_stats.get("config_version") or ""
    has_fts = bool(idx_stats.get("has_fts"))

    builder_version = idx_stats.get("builder_version") or "?"
    synonyms_count = idx_stats.get("object_synonyms", 0)

    idx_lines = ["\n== INDEX =="]
    label = f"Index v{builder_version} ({methods_count} methods, {calls_count} call edges"
    if synonyms_count:
        label += f", {synonyms_count} synonyms"
    oa_count = idx_stats.get("object_attributes", 0)
    pi_count = idx_stats.get("predefined_items", 0)
    if oa_count:
        label += f", {oa_count} attributes"
    if pi_count:
        label += f", {pi_count} predefined"
    if config_name:
        label += f", config: {config_name}"
        if config_version:
            label += f" v{config_version}"
    label += ")."
    idx_lines.append(label)
    # Co-located nudge (v1.27.0): the counts/has_*/builder_version below ARE the get_index_info()
    # payload (rlm_start.index carries the same subset), so calling it on start wastes an execute.
    idx_lines.append(
        "⚠️ Эти данные (builder_version/has_*/counts) уже в rlm_start.index — НЕ вызывай "
        "get_index_info() на старте (пустая трата execute); он нужен лишь для "
        "has_regions/has_module_headers/extension_overrides."
    )

    instant_helpers = ["extract_procedures()", "find_exports()"]
    if calls_count:
        instant_helpers.append("find_callers_context()")
        instant_helpers.append("find_call_hierarchy()")
    instant_helpers.extend(
        [
            "find_event_subscriptions()",
            "find_scheduled_jobs()",
            "find_functional_options()",
        ]
    )
    role_rights_count = idx_stats.get("role_rights", 0)
    if role_rights_count:
        instant_helpers.append("find_roles()")
    register_movements_count = idx_stats.get("register_movements", 0)
    if register_movements_count:
        instant_helpers.extend(["find_register_movements()", "find_register_writers()"])
    file_paths_count = idx_stats.get("file_paths", 0)
    if file_paths_count:
        # find_files() is NOT listed here: it is instant on an index-hit but falls back
        # to an FS-scan on a zero-hit (v1.26.0) — the conditional behaviour is described
        # in the tips below, so it must not appear in the unconditional INSTANT list.
        instant_helpers.extend(["glob_files(indexed)", "tree(indexed)"])
    if synonyms_count:
        instant_helpers.append("search_objects()")
    form_elements_count = idx_stats.get("form_elements", 0)
    if form_elements_count:
        instant_helpers.append("parse_form()")
    bver = int(idx_stats.get("builder_version") or 0)
    if bver >= 8:
        instant_helpers.append("search_regions()")
        instant_helpers.append("search_module_headers()")
    if oa_count:
        instant_helpers.append("find_attributes()")
    if pi_count:
        instant_helpers.append("find_predefined()")
    if oa_count and pi_count:
        instant_helpers.append("get_object_full_structure()")
    instant_helpers.append("search()")
    idx_lines.append(f"INSTANT from index: {', '.join(instant_helpers)}.")

    if has_fts:
        idx_lines.append(
            "search_methods(query) — full-text search by method name substring. "
            "Use in Step 1 DISCOVER to find methods across the codebase (main FTS: query >= 3 chars)."
        )
    if synonyms_count:
        idx_lines.append(
            f"search_objects(query) — {synonyms_count} object synonyms indexed. "
            "Find 1C objects by Russian business name. Use in Step 1 DISCOVER."
        )

    tips = [
        "INDEX TIPS:",
        "  - find_callers_context() returns instantly — для СКОРОСТИ scope-hint не нужен, ищи по всей кодовой базе.",
        "  - НО module_hint у find_call_hierarchy/find_callers_context — это ТОЧНОСТЬ, не скорость: для одноименных объектных методов, которые РЕАЛЬНО зовут из BSL (ЗаполнитьДокумент, ОтразитьВУчете), hint включает exact-режим (точные ребра по callee_key, без однофамильцев из других модулей); экспортному методу общего модуля hint не нужен, ЕСЛИ его имя уникально во всей БД — иначе (root_exact=False) передай module_hint.",
        "  - ПЛАТФОРМЕННЫЕ обработчики (ОбработкаПроведения, ПередЗаписью): вызов от ПЛАТФОРМЫ в граф не попадает → callers=0 норма, не мертвый код (ЯВНЫЙ BSL-вызов хелпер покажет). Движения ищи не тут: rlm_help(topic='проведение').",
        "  - Batch 5-10 helpers per rlm_execute (index calls are <1ms each).",
        "  - extract_procedures + find_exports + find_callers_context in ONE call is fine.",
        "  - find_attributes() and find_predefined() are INSTANT from index — use for attribute/subconto type questions.",
    ]
    if file_paths_count:
        tips.extend(
            [
                f"  - File navigation indexed: {file_paths_count} paths (.bsl/.mdo/.xml) — "
                "glob_files(), tree() instant for supported patterns; find_files() instant on index-hit, else FS-fallback (may scan).",
                "  - FAST: glob_files('**/*.mdo'), glob_files('Subsystems/**/*.mdo'), glob_files('Documents/**'), tree('Documents'), find_files('name')",
                "  - SLOW (FS fallback): complex globs with multiple wildcards, glob_files('**/Dir*/*.xml')",
                "  - For BSL modules: ALWAYS prefer find_module()/find_by_type() over glob_files() — faster and more precise.",
                "  - NEVER use tree('.') on root of large configs — too much data. Use tree('SubDir') instead.",
            ]
        )
    idx_lines.append("\n".join(tips))

    idx_lines.append(
        "NOTE: Index freshness uses quick check (age + content sampling). "
        "Structural validation (files added/removed) is approximate — "
        "run 'rlm-bsl-index index info' for full check."
    )

    for w in idx_warnings or []:
        idx_lines.append(f"WARNING: {w}")
    return "\n".join(idx_lines)


# ─────────────────────────────────────────────────────────────────────
#               rlm_help dispatcher helpers (read-only)
# ─────────────────────────────────────────────────────────────────────


def _get_section(name: str) -> str:
    """Return the verbatim text of a strategy section.

    `disambiguation` is intentionally NOT a key here — it is structured data,
    fetched via `_get_disambiguation()` instead. Other unknown names raise.
    """
    if name == "disambiguation":
        raise ValueError("section='disambiguation' is structured data — use _get_disambiguation() instead")
    if name not in STRATEGY_SECTIONS:
        raise KeyError(name)
    return STRATEGY_SECTIONS[name]


def _get_disambiguation(filter_helpers: list[str] | None = None) -> list[dict]:
    """Return DISAMBIGUATION_PAIRS, optionally narrowed by ``filter_helpers``.

    Filter semantics (case-insensitive):
      - 1 helper given  → pairs where at least one leg matches.
      - 2+ helpers given → pairs where BOTH legs are in the filter (lets agents
        pinpoint a single pair via ``helpers=['a','b']`` even when 'a' or 'b'
        appears in several pairs).
    """
    if not filter_helpers:
        return list(DISAMBIGUATION_PAIRS)
    targets = {h.lower() for h in filter_helpers if h}
    out: list[dict] = []
    for entry in DISAMBIGUATION_PAIRS:
        a, b = entry["pair"]
        a_l, b_l = a.lower(), b.lower()
        if len(targets) >= 2:
            if a_l in targets and b_l in targets:
                out.append(entry)
        else:
            if a_l in targets or b_l in targets:
                out.append(entry)
    return out


def _get_category_helpers(category: str, registry: dict) -> list[dict]:
    """Return [{name, sig}] for helpers in the given category. Empty list if
    category is unknown or registry missing."""
    if not registry:
        return []
    return [
        {"name": name, "sig": entry.get("sig", "")} for name, entry in registry.items() if entry.get("cat") == category
    ]


def _get_topic_recipe(topic: str, format: str = "compact", include_code: bool = True) -> dict | None:
    """Resolve `topic` (domain key or alias) to a recipe dict.

    Returns ``{topic, format, steps, code_hint?}`` or None if no match.
    """
    if not topic:
        return None
    domain = _match_recipe(topic)
    if domain is None or domain not in _BUSINESS_RECIPES:
        return None
    recipe = _BUSINESS_RECIPES[domain]
    fmt = format if format in ("compact", "full") else "compact"
    steps = list(recipe.get(fmt) or recipe.get("compact") or [])
    out: dict = {"topic": domain, "format": fmt, "steps": steps}
    if include_code and recipe.get("code_hint"):
        out["code_hint"] = recipe["code_hint"]
    return out


def _get_helper_details(name: str, registry: dict) -> dict | None:
    """Return ``{name, sig, category, kw, recipe}`` or None if not registered."""
    if not registry or name not in registry:
        return None
    entry = registry[name]
    return {
        "name": name,
        "sig": entry.get("sig", ""),
        "category": entry.get("cat", ""),
        "kw": list(entry.get("kw") or []),
        "recipe": entry.get("recipe", ""),
    }


def _fuzzy_suggest(query: str, candidates: list[str], top_n: int = 3) -> list[str]:
    """Return up to `top_n` close matches from `candidates` using difflib."""
    if not query or not candidates:
        return []
    return difflib.get_close_matches(query, candidates, n=top_n, cutoff=0.5)


def list_topics() -> list[str]:
    """All resolvable topic keys (domain keys + aliases). For menu output."""
    return list(_BUSINESS_RECIPES.keys()) + list(_RECIPE_ALIASES.keys())


def list_sections() -> list[str]:
    """Section names valid for `rlm_help(section=...)` (excluding 'disambiguation',
    which is fetched separately as structured data — but listed here so the
    enum and the menu agree)."""
    return ["workflow", "performance", "batching", "io", "critical", "disambiguation"]


def list_categories() -> list[str]:
    """Helper category keys, in display order."""
    return [k for k, _ in _CATEGORY_ORDER]


def _ext_override_detail_budget() -> int:
    """Сколько строк поимённых перехватов расширений включать в стратегию
    СУММАРНО по всем расширениям. 0 (по умолчанию, #1 v1.28.0) — НИ поимённого
    дампа, НИ построчных счётчиков: только указатель на структурный
    nearby_extensions + get_overrides() (счётчики зеркалят
    nearby_extensions[].overrides_count). >0 — включить per-extension счётчики и
    до N detail-строк. Регулируется env RLM_EXT_OVERRIDE_DETAIL."""
    raw = os.environ.get("RLM_EXT_OVERRIDE_DETAIL", "").strip()
    if not raw:
        return 0
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


def _extension_strategy(ext_context, ext_overrides: dict) -> str:
    """Build strategy text for extension context."""
    from rlm_tools_bsl.extension_detector import ConfigRole, _ext_list_cap

    current = ext_context.current
    lines: list[str] = []

    if current.role == ConfigRole.MAIN and ext_context.nearby_extensions:
        # На extreme-extension конфигах (напр. 155 расш) заголовок + строки-счётчики
        # на КАЖДОЕ расширение раздували стратегию выше токен-лимита. Усекаем агент-facing
        # представление до top-N по overrides (полнота — через detect_extensions()).
        # Внутренний ext_context.nearby_extensions и питание песочницы НЕ трогаются.
        shown, total, n_shown = summarize_extensions_by_overrides(
            ext_context.nearby_extensions, ext_overrides, _ext_list_cap()
        )
        truncated = n_shown < total
        budget = _ext_override_detail_budget()
        # Header: total COUNT + a pointer to the authoritative machine-readable list, NOT
        # a prose enumeration of names/prefixes (#1, v1.28.0). The top-N names, prefixes and
        # overrides_count already live in extension_context.nearby_extensions (with its own
        # truncation markers — nearby_extensions_truncated/total/shown + extensions_hint);
        # serializing them a 2nd time here (prose header) and a 3rd (per-extension counters
        # below) cost ~600 tok on EVERY rlm_start. The behavioral block is preserved VERBATIM.
        lines.append(
            f"\nCRITICAL — {total} EXTENSIONS DETECTED "
            "(name/prefix/overrides_count в extension_context.nearby_extensions)\n"
            "Extensions OVERRIDE methods in this config via annotations:\n"
            "  &Перед (Before), &После (After), &Вместо (Instead), &ИзменениеИКонтроль (ChangeAndValidate)\n"
            "YOU MUST mention overridden methods in your response.\n"
            "Extension files are OUTSIDE the sandbox: read_file/grep/glob_files on '../' paths raise PermissionError.\n"
            "BUT: if find_module returned a path starting with '../' — it is an extension file; pass it directly\n"
            "to high-level BSL helpers (read_procedure, extract_procedures, parse_object_xml, find_attributes,\n"
            "find_predefined, search). They read extensions internally. For overrides use get_overrides/find_ext_overrides."
        )
        # Per-extension override COUNTS mirror nearby_extensions[].overrides_count, so by
        # default (budget==0) we DON'T re-emit them — a single pointer to get_overrides()
        # suffices (the ~600-tok dedup of #1). Detail is opt-in via RLM_EXT_OVERRIDE_DETAIL:
        # a SINGLE loop with a GLOBAL `detail_used` budget (not per-extension); detail rows
        # stay grouped UNDER each extension's count line (provenance). Omission is tracked
        # GLOBALLY and signalled by ONE pointer at the end — the per-extension "... and more"
        # marker is stripped so later extensions whose detail was dropped aren't silently
        # count-only. The hidden-extensions summary (top-N truncation) rides the same opt-in
        # path — at budget==0 the structured nearby_extensions markers already convey it.
        omitted_detail = False
        if budget:
            detail_used = 0
            for e in shown:
                overrides = ext_overrides.get(e.path, [])
                if not overrides:
                    continue
                lines.append(f"\n{e.name or '?'}: {len(overrides)} overrides")
                remaining = budget - detail_used
                if remaining <= 0:
                    omitted_detail = True  # later extension's detail dropped entirely
                    continue
                detail = _format_overrides_summary(overrides, max_lines=remaining)
                # Strip the helper's LOCAL truncation marker — a single GLOBAL pointer is
                # emitted at the end instead (so omission is unambiguous across all exts).
                if detail and "... and more" in detail[-1]:
                    detail = detail[:-1]
                    omitted_detail = True
                lines.extend(detail)
                detail_used += len(detail)
            if truncated:
                # Расширения вне top-N не получили строку-счётчик — одна сводка про скрытые
                # (их число + суммарные overrides) с указателем на полный список.
                shown_paths = {e.path for e in shown}
                hidden = total - n_shown
                hidden_overrides = sum(
                    len(ext_overrides.get(e.path, []))
                    for e in ext_context.nearby_extensions
                    if e.path not in shown_paths
                )
                lines.append(
                    f"\n… +{hidden} more extensions, {hidden_overrides} overrides total "
                    "— get_overrides('ИмяОбъекта') / detect_extensions()."
                )
        if not budget or omitted_detail:
            lines.append(
                "\nFull per-object override detail on demand: get_overrides('ИмяОбъекта') or find_ext_overrides()."
            )

    elif current.role == ConfigRole.EXTENSION:
        name_label = current.name or "?"
        purpose_label = current.purpose or "unknown"
        prefix_label = current.name_prefix or "—"
        lines.append(
            f"\nCRITICAL — THIS IS AN EXTENSION, NOT A MAIN CONFIG.\n"
            f"Extension: '{name_label}' (purpose: {purpose_label}, prefix: {prefix_label})\n"
            "Objects with ObjectBelonging=Adopted are borrowed from the main config.\n"
            "YOUR ANALYSIS IS INCOMPLETE without the main configuration.\n"
            "YOU MUST:\n"
            "  1. In your response, clearly state that this is an EXTENSION.\n"
            "  2. Warn the user that analysis without the main config may be misleading."
        )
        if ext_context.nearby_main:
            lines.append(
                f"  Main config found nearby: {ext_context.nearby_main.name or '?'} at {ext_context.nearby_main.path}"
            )
        # Own overrides under the same global budget as the MAIN branch.
        overrides = ext_overrides.get("self", [])
        if overrides:
            lines.append(f"\n{name_label}: {len(overrides)} overrides")
            budget = _ext_override_detail_budget()
            if budget:
                lines.append("")
                lines.extend(_format_overrides_summary(overrides, max_lines=budget))
            else:
                lines.append(
                    "\nPer-object override detail on demand: get_overrides('ИмяОбъекта') "
                    "or find_ext_overrides() (omitted from strategy to save tokens)."
                )

    return "\n".join(lines)


def summarize_extensions_by_overrides(nearby_extensions, ext_overrides: dict, cap: int):
    """Усечь агент-facing список расширений до top-N по числу overrides.

    Возвращает ``(shown_list, total, shown)``. На малых/средних конфигах и при
    отключённом лимите усечение НЕ происходит:

    - ``cap <= 0`` (без лимита, в т.ч. ``-1``) ИЛИ ``total <= cap`` →
      ``(list(nearby_extensions), total, total)`` — исходный порядок, ``shown == total``
      (caller НЕ добавляет companion-поля / маркер усечения).

    Иначе (``total > cap > 0``) → top-N по убыванию числа overrides с ПОЛНЫМ
    детерминированным тай-брейком ``(name, name_prefix, normcase(path))``. Вход
    идёт из несортированного ``iterdir()`` с возможными пустыми/дублирующимися
    именами, поэтому финальный ключ ``normcase(path)`` (пути уникальны) гарантирует
    воспроизводимый порядок. Возвращается НОВЫЙ список (вход не мутируется)."""
    total = len(nearby_extensions)
    if cap <= 0 or total <= cap:
        return list(nearby_extensions), total, total
    shown = sorted(
        nearby_extensions,
        key=lambda e: (
            -len(ext_overrides.get(e.path, [])),
            e.name or "",
            e.name_prefix or "",
            os.path.normcase(e.path),
        ),
    )[:cap]
    return shown, total, cap


def _format_overrides_summary(overrides: list[dict], max_lines: int = 30) -> list[str]:
    """Format overrides as compact grouped-by-object lines."""
    from collections import defaultdict

    by_object: dict[str, list[str]] = defaultdict(list)
    for o in overrides:
        obj = o.get("object_name") or "?"
        ann = o.get("annotation", "?")
        target = o.get("target_method", "?")
        by_object[obj].append(f'&{ann}("{target}")')

    lines: list[str] = []
    for obj, obj_annotations in sorted(by_object.items()):
        if len(lines) >= max_lines:
            lines.append("  ... and more — детали по объекту: get_overrides('ИмяОбъекта') (первые 200, см. truncated)")
            break
        lines.append(f"  {obj}: {', '.join(obj_annotations)}")
    return lines


# NOTE: legacy / NOT used by the MCP server — the real rlm_start tool description
# is the docstring of server.py::rlm_start (FastMCP reads it). Kept only for the
# existing test in test_bsl_knowledge.py; do NOT edit this expecting API changes.
RLM_START_DESCRIPTION = (
    "Start a BSL code exploration session on a 1C codebase.\n"
    "Returns session_id, detected config format, BSL helper functions, and exploration strategy.\n"
    "IMPORTANT: Use effort='high' for any multi-aspect analysis (recommended default).\n"
    "Use effort='low' ONLY for single quick lookups (find one module, read one procedure).\n"
    "For large 1C configs (23K+ files), NEVER grep on broad paths -- use find_module() first.\n"
    "NEVER call rlm_index(action='build') yourself — only the user decides when to build indexes. "
    "Build runs in background but requires the project password. If no index exists, work without it."
)
