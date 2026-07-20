from __future__ import annotations
import collections
import concurrent.futures
import json
import logging
import os
import re
import threading
import time as _time_mod
import warnings
from dataclasses import replace
from pathlib import Path
from rlm_tools_bsl.format_detector import parse_bsl_path, BslFileInfo, FormatInfo
from rlm_tools_bsl.bsl_knowledge import (
    BSL_PATTERNS,
    _AttrRecord,
    _merge_proc_continuations,
    _normalize_method_params,
    _split_params,
)
from rlm_tools_bsl.bsl_index import (
    _BSL_GLOBAL_FUNCS_LOWER,
    _make_callee_key,
    _MOVEMENT_METHOD_NOISE,
    _scan_module,
)
from rlm_tools_bsl.cache import load_index, save_index
from rlm_tools_bsl.helpers import _SKIP_DIRS as _GENERIC_SKIP_DIRS
from rlm_tools_bsl.regex_safety import NESTED_QUANTIFIER_ERROR, has_catastrophic_nesting

logger = logging.getLogger(__name__)
from rlm_tools_bsl.bsl_xml_parsers import (
    _normalize_category,
    parse_metadata_xml,
    parse_event_subscription_xml,
    parse_scheduled_job_xml,
    parse_enum_xml,
    parse_functional_option_xml,
    parse_rights_xml,
)

# Прямое обращение к коллекции движений в ЖИВОМ модуле: ``Движения.<Регистр>``.
# Lookahead-before-capture отбрасывает вызовы методов самой коллекции
# (``Движения.Записать()`` и т.п.) — зеркало ``_MOVEMENTS_RE`` в bsl_index (там же и
# развёрнутое объяснение, почему запрет стоит ДО захвата). Единственная копия на весь
# модуль: её используют И live-ветка find_register_movements, И перепроверка
# posting_handler_present — иначе две «почти одинаковые» регулярки разъезжаются.
_MOVEMENTS_LIVE_RE = re.compile(r"(?:Движения|RegisterRecords)\s*\.\s*(?!\w+\s*\()(\w+)", re.IGNORECASE)

# Объявление обработчика проведения. Ключевые слова — как в BSL_PATTERNS["procedure_def"]:
# 1С принимает и английский синтаксис (Procedure/Function), и своя «только русская» регулярка
# дала бы false-negative на валидном модуле. Якорь ^\s* (MULTILINE) + ``\b`` после имени:
# не путаем с однофамильцем-суффиксом (ОбработкаПроведенияДоп). ``\s+`` покрывает и перенос
# строки между ключевым словом и именем, и multiline-сигнатуру (имя всегда в одной строке с
# ключевым словом, продолжение уезжает внутрь скобок).
_POSTING_HANDLER_DECL_RE = re.compile(
    r"^\s*(?:Процедура|Функция|Procedure|Function)\s+ОбработкаПроведения\b",
    re.IGNORECASE | re.MULTILINE,
)
# CFE-перехваты проведения живут в том же ObjectModule, но имя процедуры у них произвольное:
# ``&После("ОбработкаПроведения") Процедура ПослеПроведения(...)``. Само объявление целевого
# метода поэтому их не покрывает. Эти две регулярки используются только на уже отобранных ТОЧНЫХ
# ObjectModule документа; аннотацию читаем по сырой строке с якорем (строка-продолжение BSL
# начинается с ``|``, комментарий — с ``//``), а тело процедуры затем разбираем обычным live-кодом.
_CFE_POSTING_ANNOTATION_RE = re.compile(
    r'^\s*&(Перед|После|Вместо|ИзменениеИКонтроль)\s*\(\s*"ОбработкаПроведения"\s*\)',
    re.IGNORECASE,
)
_ANY_PROC_DECL_RE = re.compile(
    r"^\s*(?:Процедура|Функция|Procedure|Function)\s+(\w+)\b",
    re.IGNORECASE,
)
_CFE_POSTING_REPLACEMENTS = frozenset({"вместо", "изменениеиконтроль"})
_CONTINUE_MAIN_RE = re.compile(r"\b(ПродолжитьВызов|ProceedWithCall)\s*\(", re.IGNORECASE)

# --- Разбор тела обработчика (v1.28.0 follow-up) --------------------------------------------
# Классификацию получателя (`X.Метод()` — это общий модуль? переменная? реквизит?) делает СЕРВЕР,
# а не агент. Причины ровно две, и обе — отказы, а не эстетика:
#   1) АГЕНТ НЕ МОЖЕТ ПРОЧИТАТЬ МОДУЛЬ, если обработчик уехал в CFE-расширение: путь оттуда
#      `../<Ext>/...` лежит ВНЕ песочницы, и generic read_file бросает PermissionError
#      (helpers.py `_resolve_safe`). Сервер читает через `_ext_read_file` — ему можно.
#   2) АГЕНТ НЕ МОЖЕТ ПОДТВЕРДИТЬ РЕЗУЛЬТАТ: проверка `definitions[0]['category'] == 'CommonModules'`
#      ТАВТОЛОГИЧНА — `module_hint='ОбщийМодуль.X'` уже добавляет в SQL `mod.category='CommonModules'`
#      (bsl_index `_normalize_module_hint` + WHERE), поэтому она истинна ПО ПОСТРОЕНИЮ и про
#      настоящего получателя не говорит НИЧЕГО.
# У сервера есть и живой текст модуля, и `_index_state`, и индекс реквизитов — то есть ровно то,
# чем получателя можно РАЗРЕШИТЬ, а не угадать. Агенту уходят факты и только исполнимые шаги.
# Прямая запись регистра платформой: набор записей ИЛИ менеджер записи. Оба вида называют
# регистр прямо в строке создания — этого достаточно, чтобы отдать агенту готовый факт.
_RECORD_SET_RE = re.compile(
    r"\b(?P<manager>Регистры(?:Накопления|Сведений|Бухгалтерии|Расчета)|"
    r"(?:Accumulation|Information|Accounting|Calculation)Registers)\s*\.\s*"
    r"(?P<register>\w+)\s*\.\s*(?:Создать(?:НаборЗаписей|МенеджерЗаписи)|"
    r"Create(?:RecordSet|RecordManager))",
    re.IGNORECASE,
)
# Получатель — ЦЕПОЧКА из одного или более идентификаторов: `Сервис.М()`, но и
# `ЭтотОбъект.Реквизит.М()`. Одноидентификаторная версия теряла цепочку свойств ЦЕЛИКОМ —
# вызов не попадал даже в НЕ ОПОЗНАН, все списки фактов пустели, и hint честно врал
# «движений не пишет». Хвост после `()` цепочкой НЕ считается (перед стартом цепочки
# запрещены и `\w`, и `.`): `Запрос.Выполнить().Выбрать()` матчится как Запрос.Выполнить.
_DOTTED_CALL_RE = re.compile(r"(?<![\w.])(\w+(?:\s*\.\s*\w+)*)\s*\.\s*(\w+)\s*\(", re.UNICODE)
_DOTLESS_CALL_RE = re.compile(r"(?<![\w.])(\w+)\s*\(", re.UNICODE)
_PROC_END_RE = re.compile(
    r"^\s*(?:КонецПроцедуры|КонецФункции|EndProcedure|EndFunction)\b", re.IGNORECASE | re.MULTILINE
)
# Получатели, которые НЕ являются ни модулем, ни переменной пользователя: платформенные
# пространства имён и сам объект. Их не классифицируем и в делегаты не записываем.
# NB: «Объект» здесь НЕТ намеренно: в ObjectModule это НЕ предопределённое имя (форменная
# сущность живёт в модулях форм), а обычная переменная — `Объект = ПолучитьСервис();
# Объект.ОтразитьДвижения()` содержит настоящего делегата, и глотать его receiver-шумом нельзя.
_MANAGER_RECEIVER_CATEGORIES = {
    "документы": "Documents",
    "справочники": "Catalogs",
    "перечисления": "Enums",
    "константы": "Constants",
    "планывидовхарактеристик": "ChartsOfCharacteristicTypes",
    "планысчетов": "ChartsOfAccounts",
    "регистрынакопления": "AccumulationRegisters",
    "регистрысведений": "InformationRegisters",
    "регистрыбухгалтерии": "AccountingRegisters",
    "регистрырасчета": "CalculationRegisters",
    "documents": "Documents",
    "catalogs": "Catalogs",
    "enums": "Enums",
    "constants": "Constants",
    "chartsofcharacteristictypes": "ChartsOfCharacteristicTypes",
    "chartsofaccounts": "ChartsOfAccounts",
    "accumulationregisters": "AccumulationRegisters",
    "informationregisters": "InformationRegisters",
    "accountingregisters": "AccountingRegisters",
    "calculationregisters": "CalculationRegisters",
}
_REGISTER_MANAGER_RECEIVERS = frozenset(
    {
        "регистрынакопления",
        "регистрысведений",
        "регистрыбухгалтерии",
        "регистрырасчета",
        "accumulationregisters",
        "informationregisters",
        "accountingregisters",
        "calculationregisters",
    }
)
_RECEIVER_NOISE = frozenset(
    {
        "движения",
        "этотобъект",
        "новый",
        "new",
        "thisobject",
    }
) | frozenset(_MANAGER_RECEIVER_CATEGORIES)

# English aliases of the same platform-global families.  The call-graph curated set
# historically contains mostly Russian spellings, while BSL permits an English script
# variant (and even mixing both variants in one module).  Keep this list local to the
# posting analyzer: broadening the persisted call-graph cleanup is a separate contract.
_DOTLESS_PLATFORM_GLOBALS_EN_LOWER = frozenset(
    name.casefold()
    for name in {
        # strings
        "Message",
        "Format",
        "NStr",
        "StrTemplate",
        "StrFind",
        "StrReplace",
        "StrLen",
        "StrSplit",
        "StrConcat",
        "StrLineCount",
        "StrOccurrenceCount",
        "StrGetLine",
        "StrStartsWith",
        "StrEndsWith",
        "StrCompare",
        "Upper",
        "Lower",
        "Title",
        "TrimL",
        "TrimR",
        "TrimAll",
        "Left",
        "Right",
        "Mid",
        "Char",
        "CharCode",
        "IsBlankString",
        # dates
        "CurrentDate",
        "SessionDate",
        "Year",
        "Month",
        "Day",
        "Hour",
        "Minute",
        "Second",
        "WeekDay",
        "AddMonth",
        "BegOfYear",
        "EndOfYear",
        "BegOfQuarter",
        "EndOfQuarter",
        "BegOfMonth",
        "EndOfMonth",
        "BegOfDay",
        "EndOfDay",
        "BegOfWeek",
        "EndOfWeek",
        # math, types and casts
        "Int",
        "Round",
        "Max",
        "Min",
        "Abs",
        "Pow",
        "Sqrt",
        "Exp",
        "TypeOf",
        "ValueIsFilled",
        "IsNull",
        "PredefinedValue",
        "String",
        "Number",
        "Date",
        "Boolean",
        "Structure",
        "Map",
        "ValueList",
        "Query",
        "QuerySchema",
        "NotifyDescription",
        "TypeDescription",
        "StringQualifiers",
        "NumberQualifiers",
        "ValueStorage",
        "Color",
        # transactions, forms and other common global-context methods
        "BeginTransaction",
        "CommitTransaction",
        "RollbackTransaction",
        "TransactionActive",
        "LockDataForEdit",
        "OpenForm",
        "GetForm",
        "FillPropertyValues",
        "ExecuteNotifyProcessing",
        "GetFunctionalOption",
        "ShowQueryBox",
        "ShowWarning",
        "ShowUserNotification",
        "DoQueryBox",
        "DoMessageBox",
        "NotifyChanged",
        "SetPrivilegedMode",
        "PrivilegedMode",
        "PutToTempStorage",
        "GetFromTempStorage",
        "DeleteFromTempStorage",
        "ErrorInfo",
        "Status",
        "AccessRight",
        "AttachIdleHandler",
        "ValueToFormData",
        "CopyFormData",
        "FormAttributeToValue",
        "WriteLogEvent",
        "Notify",
        "ClearMessages",
    }
)

# Ключевые слова BSL и зарезервированные глобальные функции платформы, которые синтаксически
# выглядят как вызов без точки. Локальное объявление проверяется ДО этого набора, поэтому
# одноимённый метод текущего модуля сохраняется. Платформенную часть не дублируем вручную:
# это курируемый набор call-graph extractor с отдельно исключёнными реальными коллизиями.
_DOTLESS_NOISE = (
    frozenset(
        {
            "если",
            "иначеесли",
            "пока",
            "для",
            "возврат",
            "новый",
            "сообщить",
            "тип",
            "типзнч",
            "формат",
            "строка",
            "число",
            "дата",
            "булево",
            "значениезаполнено",
            "выполнить",
            "найти",
            "и",
            "или",
            "не",
            "вызватьисключение",
            "and",
            "or",
            "not",
            "raise",
            "if",
            "while",
            "for",
            "return",
            "new",
        }
    )
    | _BSL_GLOBAL_FUNCS_LOWER
    | _DOTLESS_PLATFORM_GLOBALS_EN_LOWER
)
# Методы платформы: `НаборЗаписей.Записать()` — это НЕ делегат, а запись уже созданного набора.
# Без этого списка разбор объявил бы «делегатом» каждый служебный вызов и утопил бы в шуме
# единственный настоящий. ВНИМАНИЕ: шум применяется ПАРОЙ (вид получателя, метод), а НЕ одним
# именем метода — экспортный метод общего модуля законно зовётся Записать/Выполнить/Получить
# (боевой паттерн: `ПроведениеДокументов.Записать(ЭтотОбъект, Отказ)`), и фильтр по одному имени
# терял бы единственного делегата, а hint заявлял бы «движений не пишет». Шумом эти имена
# считаются только у НЕ-модульных получателей (переменная/реквизит/неопознанный).
_DELEGATE_METHOD_NOISE = frozenset(
    {
        "записать",
        "прочитать",
        "очистить",
        "добавить",
        "вставить",
        "удалить",
        "выполнить",
        "установить",
        "получить",
        "загрузить",
        "выгрузить",
        "найти",
        "заблокировать",
        "разблокировать",
        "количество",
        "создатьнаборзаписей",
        "выбрать",
        "установитьпараметр",
        "write",
        "read",
        "clear",
        "add",
        "insert",
        "delete",
        "execute",
    }
)


def _live_code_only(body: str) -> str:
    """Тело модуля БЕЗ комментариев и строковых литералов (общесистемный ``_scan_module``).

    И «объявление процедуры», и «прямое обращение `Движения.<Регистр>`» — это утверждения про
    КОД, а не про текст в комментарии или в тексте запроса. Общий парсер методов
    (``BSL_PATTERNS["procedure_def"]``) применяется через неякорный ``.search()`` к СЫРОЙ строке
    и потому считает процедурой даже `// Процедура X()`; экстрактор движений в билдере тоже
    матчит по сырому content. Мы на это НЕ опираемся: обе перепроверки этого хелпера идут по
    вырезанному коду, поэтому отвечают ровно то, что обещает контракт.

    NB: сквозная слепота билдера к комментариям/строкам (закомментированная процедура попадает
    в таблицу ``methods``, закомментированное `Движения.X` — в ``register_movements``) — ОТДЕЛЬНЫЙ
    пре-существующий дефект. Лечится только в билдере, а это бамп BUILDER_VERSION + пересборка
    индексов, что в этот релиз не входит. Здесь мы лишь не тиражируем его в новый сигнал.
    """
    return "\n".join(code for _lineno, code, _strings in _scan_module(body.splitlines()))


# Regex metacharacters. A pattern with none of these is a plain literal, so a
# ``git grep -F`` over it is identical to a Python ``re.search`` — that lets
# safe_grep route literal patterns through the (much faster) git backend while
# keeping real regexes on Python ``re``.
_RE_METACHARS = frozenset(r"\^$.|?*+()[]{}")

# Backstop for find_call_hierarchy BFS: a hard cap on distinct visited targets.
# visited-by-target (v1.16.0) correctly keeps same-named callers from different
# modules as separate nodes, but a wide root with no hint (e.g. ОбработкаПроведения,
# called by ~150 documents that each re-call it) can fan out into hundreds of
# exact-mode targets. Unreachable for small/medium trees (namesake tests touch a
# handful of nodes); only bounds the pathological wide-root case. Read as a module
# global at call time so tests can monkeypatch it.
_HIERARCHY_VISITED_CAP = 2000

# Separate, modest node budget for find_data_path: every expanded node is one
# py_lower scan of metadata_references (same cost profile as find_metadata_references),
# so it is capped much tighter than the call-graph BFS. Read as a module global at
# call time so tests can monkeypatch it.
_DATA_PATH_NODE_BUDGET = 400

# Per-node callers page size for find_path's reverse-BFS (mirrors find_call_hierarchy).
# A node with MORE callers than this is only partially expanded → find_path flags
# the search as truncated (budget_exceeded) so a found=False stays inconclusive
# instead of silently dropping caller #N+1. Module global so tests can monkeypatch.
_FIND_PATH_NODE_LIMIT = 200


def _is_literal_pattern(pattern: str) -> bool:
    """True when *pattern* contains no regex metacharacters (treat as literal)."""
    return not any(c in _RE_METACHARS for c in pattern)


class LazyList:
    """Thread-safe lazy-init list with double-check locking."""

    __slots__ = ("data", "_built", "_lock")

    def __init__(self):
        self.data: list = []
        self._built = False
        self._lock = threading.Lock()

    def ensure(self, builder):
        if self._built:
            return self.data
        with self._lock:
            if not self._built:
                self.data.extend(builder())
                self._built = True
        return self.data


class LazyDict:
    """Thread-safe per-key lazy cache with double-check locking."""

    __slots__ = ("data", "_lock")

    def __init__(self):
        self.data: dict = {}
        self._lock = threading.Lock()

    def get_or_set(self, key, builder):
        if key in self.data:
            return self.data[key]
        with self._lock:
            if key not in self.data:
                self.data[key] = builder()
        return self.data[key]


# --- Static helper-metadata snapshot for `rlm_help` -------------------------
# `make_bsl_helpers` registers every helper into its closure-local `_registry`
# even before any helper function is called: registration only writes
# {sig, cat, kw, recipe} via `_reg(...)`. We exploit that to build a static
# snapshot of helper metadata without an active sandbox or filesystem — the
# stub callbacks below are wired only because `make_bsl_helpers` requires
# them; their behaviour is irrelevant because no helper body is executed.

_HELPER_METADATA_SNAPSHOT: dict[str, dict] | None = None
_HELPER_METADATA_SNAPSHOT_LOCK = threading.Lock()


def build_helper_metadata_snapshot() -> dict[str, dict]:
    """Return a frozen ``{name: {sig, cat, kw, recipe}}`` map of every helper.

    Module-level cache; first call pays the registration cost (no I/O), every
    subsequent call returns the same dict instance. Used by the ``rlm_help``
    MCP tool to answer ``category=`` / ``helpers=`` / menu queries without
    holding an open session.
    """
    global _HELPER_METADATA_SNAPSHOT
    if _HELPER_METADATA_SNAPSHOT is not None:
        return _HELPER_METADATA_SNAPSHOT
    with _HELPER_METADATA_SNAPSHOT_LOCK:
        if _HELPER_METADATA_SNAPSHOT is not None:
            return _HELPER_METADATA_SNAPSHOT

        def _stub_resolve_safe(p):
            return Path(p)

        def _stub_read(_p):
            return ""

        def _stub_grep(_pat, _p="."):
            return []

        def _stub_glob(_pat):
            return []

        helpers = make_bsl_helpers(
            base_path=".",
            resolve_safe=_stub_resolve_safe,
            read_file_fn=_stub_read,
            grep_fn=_stub_grep,
            glob_files_fn=_stub_glob,
            # Force git_search into the snapshot regardless of the server's cwd /
            # whether git is reachable, so `rlm_help git_search` is always
            # documented. Live sessions gate it via "auto" (see make_bsl_helpers).
            register_git_search="force",
        )
        registry = helpers.get("_registry") or {}
        snapshot: dict[str, dict] = {}
        for name, entry in registry.items():
            snapshot[name] = {
                "sig": entry.get("sig", ""),
                "cat": entry.get("cat", ""),
                "kw": list(entry.get("kw") or []),
                "recipe": entry.get("recipe", ""),
            }
        _HELPER_METADATA_SNAPSHOT = snapshot
        return snapshot


def _module_meta_from_path(rel_path: str, base_path: str) -> dict:
    """Best-effort module identity ``{category, object_name, module_type}`` derived
    structurally from a rel_path via ``parse_bsl_path`` — no index / ``_index_state``
    needed. Used by the live (no-index) paths of ``find_definition`` /
    ``get_module_outline`` so their declared metadata fields are filled even on a
    direct call where the live file index has not been populated yet.
    """
    try:
        from rlm_tools_bsl.format_detector import parse_bsl_path

        info = parse_bsl_path(rel_path, base_path)
        return {
            "category": info.category,
            "object_name": info.object_name,
            "module_type": info.module_type,
        }
    except Exception:
        return {"category": None, "object_name": None, "module_type": None}


def _build_outline_tree(
    regions: list[dict], methods: list[dict], include_methods: bool = True
) -> tuple[list[dict], list[dict]]:
    """Rebuild the ``#Область`` tree from flat ``[line, end_line]`` intervals (pure).

    The index stores regions/methods flat (no parent links), but every region and
    method carries ``[line, end_line]``, so nesting is reconstructed on the fly by
    interval containment — no BUILDER_VERSION bump.

    Args:
        regions: ``[{name, line, end_line}]`` — ``end_line`` may be ``None`` for an
            unclosed ``#Область``.
        methods: ``[{name, type, is_export, line, end_line, loc?}]``.
        include_methods: when ``False``, leaf methods/orphans are dropped from the
            output (only the region tree + per-region totals remain).

    Returns ``(outline, orphan_methods)``:
        * ``outline`` — list of root region nodes, each
          ``{region, line, end_line, totals:{methods, exports}, children:[...],
          methods:[...]}`` (``methods`` present only when *include_methods*).
          Per-region ``totals`` are aggregated bottom-up (descendants included).
        * ``orphan_methods`` — methods outside every region (same method shape);
          empty when *include_methods* is ``False``.

    Determinism: stable sorts with index tie-breaks → identical input yields an
    identical tree. ``end_line=None`` is treated as ``+inf`` for **containment
    only** (an unclosed region spans to EOF); the reported ``end_line`` stays
    ``None``. Crossing (non-nested) intervals → the inner one is treated as a root.
    """
    inf = float("inf")

    def _eff_end(end) -> float:
        return end if end is not None else inf

    # Region node scaffold (private keys stripped before return).
    nodes: list[dict] = [
        {
            "region": r["name"],
            "line": r["line"],
            "end_line": r["end_line"],
            "_eff_end": _eff_end(r["end_line"]),
            "children": [],
            "methods": [],
            "_own_methods": 0,
            "_own_exports": 0,
        }
        for r in regions
    ]

    # --- Build the tree via a stack over (line asc, span desc, idx) order ---
    # Outer region precedes an inner one starting on the same line; the stack
    # holds the current ancestor chain. A region that the stack-top does NOT
    # contain pops ancestors until a container is found (or it becomes a root) —
    # this also degrades crossing intervals to roots, deterministically.
    order = sorted(range(len(nodes)), key=lambda i: (nodes[i]["line"], -nodes[i]["_eff_end"], i))
    roots: list[dict] = []
    stack: list[dict] = []
    for idx in order:
        node = nodes[idx]
        while stack and not (stack[-1]["line"] <= node["line"] and node["_eff_end"] <= stack[-1]["_eff_end"]):
            stack.pop()
        if stack:
            stack[-1]["children"].append(node)
        else:
            roots.append(node)
        stack.append(node)

    # --- Assign each method to the innermost containing region (or orphan) ---
    orphan_methods: list[dict] = []
    for m in methods:
        m_eff_end = _eff_end(m.get("end_line"))
        host: dict | None = None
        host_key = None
        for i, node in enumerate(nodes):
            if node["line"] <= m["line"] and m_eff_end <= node["_eff_end"]:
                # innermost = largest line, then smallest span; idx tie-break = determinism
                key = (node["line"], -node["_eff_end"], i)
                if host is None or key > host_key:
                    host, host_key = node, key
        mrow = {
            "name": m["name"],
            "type": m.get("type"),
            "is_export": bool(m.get("is_export")),
            "line": m.get("line"),
            "end_line": m.get("end_line"),
            "loc": m.get("loc"),
        }
        if host is None:
            orphan_methods.append(mrow)
        else:
            host["methods"].append(mrow)
            host["_own_methods"] += 1
            if mrow["is_export"]:
                host["_own_exports"] += 1

    # --- Bottom-up totals (a parent's totals include all descendants') ---
    def _aggregate(node: dict) -> tuple[int, int]:
        tm, te = node["_own_methods"], node["_own_exports"]
        for ch in node["children"]:
            cm, ce = _aggregate(ch)
            tm += cm
            te += ce
        node["totals"] = {"methods": tm, "exports": te}
        return tm, te

    for root in roots:
        _aggregate(root)

    # --- Strip private keys; honor include_methods ---
    def _clean(node: dict) -> dict:
        out = {
            "region": node["region"],
            "line": node["line"],
            "end_line": node["end_line"],
            "totals": node["totals"],
            "children": [_clean(ch) for ch in node["children"]],
        }
        if include_methods:
            out["methods"] = node["methods"]
        return out

    outline = [_clean(r) for r in roots]
    return outline, (orphan_methods if include_methods else [])


def make_bsl_helpers(
    base_path: str,
    resolve_safe,  # callable: str -> pathlib.Path
    read_file_fn,  # callable: str -> str
    grep_fn,  # callable: (pattern, path) -> list[dict]
    glob_files_fn,  # callable: (pattern) -> list[str]
    format_info: FormatInfo | None = None,
    idx_reader=None,  # optional IndexReader for SQLite index acceleration
    idx_zero_callers_authoritative: bool = False,
    extension_paths: list[str] | None = None,
    register_git_search: str = "auto",
) -> dict:
    """Creates BSL helper functions for sandbox namespace.
    Internal _bsl_index is built lazily on first find_module() call.
    If idx_reader is provided, helpers use it as a fast path with fallback.

    ``extension_paths`` — absolute paths to nearby extension roots (only when
    sandbox base is a MAIN config). When non-empty, the lazy index pass also
    scans BSL + metadata XML/MDO under each extension root so that find_module,
    find_attributes, parse_object_xml, find_predefined and search() see the
    extension objects. The generic sandbox resolver (helpers.make_helpers) is
    NOT touched — extension files stay invisible to read_file/grep/glob_files.

    ``register_git_search`` controls the opt-in full-text ``git_search`` helper:
    ``"auto"`` (live sessions) registers it only when *base_path* is under a git
    work-tree and ``git`` is reachable; ``"force"`` always registers it (used by
    the rlm_help doc snapshot, independent of cwd/git); ``"never"`` never does.
    """

    _base_path_resolved = Path(base_path).resolve()
    _ext_paths_raw: list[str] = list(extension_paths or [])
    # Любой сконфигурированный root расширения обязан быть проверен целиком. Если root нельзя
    # даже разрешить или открыть, пустой набор локаторов означает «не смогли посмотреть», а не
    # «расширения ничего не добавляют» — live-проверка реквизитов тогда НЕПОЛНАЯ.
    _ext_metadata_scan_failed: list[bool] = [False]
    _ext_roots_resolved: list[Path] = []
    for ext in _ext_paths_raw:
        try:
            _ext_roots_resolved.append(Path(ext).resolve())
        except OSError:
            _ext_metadata_scan_failed[0] = True
            continue

    # Caches/structures filled during _ensure_index extension pass.
    _extension_paths_set: set[str] = set()
    _extension_root_for: dict[str, str] = {}
    _extension_metadata_xml: list[tuple[str, str, str]] = []  # (category, object_name, rel_xml_to_base)
    _extension_synonyms: list[tuple[str, str, str, str]] = []  # (obj_name, category, prefixed_synonym, rel_to_base)

    # Lazy session cache: extension root → REAL configured name (parsed from
    # Configuration.xml/.mdo by extension_detector). Module provenance uses this so it
    # shows the extension's metadata name (consistent with get_overrides), not just the
    # folder basename; basename is only a best-effort fallback when the root isn't matched.
    _ext_name_by_root: dict[str, str] = {}
    _ext_names_resolved: list[bool] = [False]

    def _extension_name_for_root(root: str) -> str | None:
        if not root:
            return None
        if not _ext_names_resolved[0]:
            _ext_names_resolved[0] = True
            try:
                from rlm_tools_bsl.extension_detector import detect_extension_context as _det

                ctx = _det(base_path)
                for e in getattr(ctx, "nearby_extensions", None) or []:
                    try:
                        if e.path and e.name:
                            _ext_name_by_root[os.path.normcase(os.path.abspath(e.path))] = e.name
                    except Exception:
                        pass
            except Exception:
                pass
        return _ext_name_by_root.get(os.path.normcase(os.path.abspath(root))) or (
            os.path.basename(root.rstrip("/\\")) or None
        )

    # Small OrderedDict cache for files outside the sandbox base (extension reads).
    _ext_file_cache: "collections.OrderedDict[str, str]" = collections.OrderedDict()
    _ext_file_cache_lock = threading.Lock()
    _EXT_FILE_CACHE_MAX = 200

    # Per-session parsed-attribute / parsed-predefined caches for extensions.
    # Built lazily on first name-only find_attributes / find_predefined call —
    # subsequent calls filter the cache in memory instead of re-parsing XML
    # for every ext object. Critical for large extensions (~150+ objects) where
    # parsing all metadata XMLs takes 5-15s on cold cache.
    _ext_attrs_cache: dict[tuple[str, str], list[dict]] = {}
    _ext_attrs_cache_built: list[bool] = [False]
    _ext_attrs_cache_lock = threading.Lock()
    _ext_predefined_cache: dict[tuple[str, str], list[dict]] = {}
    _ext_predefined_cache_built: list[bool] = [False]
    _ext_predefined_cache_lock = threading.Lock()

    # Lazy, per-session git-availability for the full-text search backend.
    # Cheap ``.git``-ancestor pre-check, confirmed by ``_git_available`` (one
    # subprocess per session, then cached). Gates git_search registration and
    # routes safe_grep's literal patterns through the git backend.
    _git_search_state: dict = {"checked": False, "available": False}
    _git_search_lock = threading.Lock()

    def _git_search_available() -> bool:
        if _git_search_state["checked"]:
            return _git_search_state["available"]
        with _git_search_lock:
            if _git_search_state["checked"]:
                return _git_search_state["available"]
            avail = False
            try:
                has_git = False
                for cand in (_base_path_resolved, *_base_path_resolved.parents):
                    if (cand / ".git").exists():  # dir (normal) or file (worktree)
                        has_git = True
                        break
                if has_git:
                    from rlm_tools_bsl.bsl_index import _git_available

                    avail = bool(_git_available(base_path))
            except Exception:
                avail = False
            _git_search_state["available"] = avail
            _git_search_state["checked"] = True
            return avail

    def _ext_resolve_safe(path: str) -> Path:
        """Multi-root path resolver: accept any path resolving under base OR any
        configured extension root. Raises PermissionError when outside all roots.

        Generic sandbox-base-only invariants in ``read_file``/``grep``/``glob_files``
        are NOT affected — this resolver is internal to BSL-helpers that already
        receive `../`-relative paths from ``_index_state``.
        """
        candidate = (_base_path_resolved / path).resolve()
        # Cheap path: inside base.
        try:
            candidate.relative_to(_base_path_resolved)
            return candidate
        except ValueError:
            pass
        # Try each extension root.
        for ext_root in _ext_roots_resolved:
            try:
                candidate.relative_to(ext_root)
                return candidate
            except ValueError:
                continue
        raise PermissionError(f"Access denied: path '{path}' escapes sandbox and extension roots")

    def _ext_read_file(path: str) -> str:
        """Reader that delegates to the sandbox cache for base files and reads
        extension-root files directly (with a small OrderedDict cache).
        """
        resolved = _ext_resolve_safe(path)
        try:
            resolved.relative_to(_base_path_resolved)
            in_base = True
        except ValueError:
            in_base = False

        if in_base:
            # Delegate to the sandbox cache via the wrapped read_file_fn.
            return read_file_fn(path)

        key = str(resolved)
        with _ext_file_cache_lock:
            if key in _ext_file_cache:
                _ext_file_cache.move_to_end(key)
                return _ext_file_cache[key]
        content = resolved.read_text(encoding="utf-8-sig", errors="replace")
        with _ext_file_cache_lock:
            _ext_file_cache[key] = content
            if len(_ext_file_cache) > _EXT_FILE_CACHE_MAX:
                _ext_file_cache.popitem(last=False)
        return content

    # Mutable closure state for lazy index
    _index_state: list = []  # list of tuples (relative_path, BslFileInfo)
    _index_built: list[bool] = [False]
    _index_lock = threading.Lock()

    # v1.18.0 Фикс 4b: формат дампа ("cf"/"edt"/"unknown"/None) — для упорядочивания
    # XML-кандидатов и текста HINT _resolve_object_xml. format_info уже в сигнатуре.
    _dump_format = format_info.primary_format.value if format_info is not None else None

    def _load_main_into_index_state() -> None:
        """Load main config modules into _index_state (idx_reader or glob+cache)."""
        # Fast path: load from SQLite index (instant, <1s)
        if idx_reader is not None:
            try:
                rows = idx_reader.get_all_modules()
                # rows is None ⇒ no `modules` table (e.g. mid in-place rebuild) → fall
                # through to the glob/cache fallback below. rows == [] is a VALID empty
                # index → populate nothing and return (NOT a fallback trigger). Explicit
                # (round 24) so the two cases are distinguished here, not via an implicit
                # None→TypeError caught by the broad except.
                if rows is not None:
                    for r in rows:
                        info = BslFileInfo(
                            relative_path=r["rel_path"],
                            category=r["category"],
                            object_name=r["object_name"],
                            module_type=r["module_type"],
                            form_name=r["form_name"],
                            command_name=None,
                            is_form_module=bool(r["form_name"]),
                        )
                        _index_state.append((r["rel_path"], info))
                    return
            except Exception:
                pass  # fallback to glob

        # Fallback: glob + disk cache
        all_bsl = glob_files_fn("**/*.bsl")
        bsl_count = len(all_bsl)

        cached = load_index(base_path, bsl_count, bsl_paths=all_bsl)
        if cached is not None:
            _index_state.extend(cached)
        else:
            for file_path in all_bsl:
                info = parse_bsl_path(file_path, base_path)
                _index_state.append((info.relative_path, info))
            save_index(base_path, bsl_count, _index_state)

    def _load_extensions_into_index_state() -> None:
        """Scan each extension root for BSL + metadata XML/MDO and side-load
        into _index_state with paths relative to the main base.
        """
        if not _ext_roots_resolved:
            return

        # Lazy import — avoids a cycle since bsl_index imports from bsl_knowledge.
        try:
            from rlm_tools_bsl.bsl_index import _collect_object_synonyms, _iter_metadata_xml_files
        except Exception:  # pragma: no cover - defensive
            _iter_metadata_xml_files = None  # type: ignore[assignment]
            _collect_object_synonyms = None  # type: ignore[assignment]
            _ext_metadata_scan_failed[0] = True

        total_ext_files = 0
        for ext_root in _ext_roots_resolved:
            if not ext_root.is_dir():
                _ext_metadata_scan_failed[0] = True
                continue
            ext_root_str = str(ext_root)

            # --- BSL pass ---
            for dirpath, dirnames, filenames in os.walk(ext_root):
                dirnames[:] = [d for d in dirnames if d not in _GENERIC_SKIP_DIRS and not d.startswith(".")]
                for fname in filenames:
                    if not fname.lower().endswith(".bsl"):
                        continue
                    full = Path(dirpath) / fname
                    try:
                        rel = os.path.relpath(str(full), base_path).replace("\\", "/")
                    except ValueError:
                        continue
                    info_ext = parse_bsl_path(str(full), ext_root_str)
                    info_bound = replace(info_ext, relative_path=rel)
                    _index_state.append((rel, info_bound))
                    _extension_paths_set.add(rel)
                    _extension_root_for[rel] = ext_root_str
                    total_ext_files += 1

            # --- Metadata-XML pass: locators for all ext objects (incl. XML-only) ---
            if _iter_metadata_xml_files is not None:
                try:
                    locators = _iter_metadata_xml_files(ext_root_str)
                except Exception:
                    locators = []
                    # «Не смогли перечислить» != «нечего перечислять»: молча пустой список
                    # позволил бы live-проверке реквизитов заявить полноту, которой не было.
                    _ext_metadata_scan_failed[0] = True
                for cat, obj_name, rel_to_ext in locators:
                    try:
                        rel_to_base = os.path.relpath(str(ext_root / rel_to_ext), base_path).replace("\\", "/")
                    except ValueError:
                        # Кросс-дисковое расширение (Windows: база на D:, расширение на E:) —
                        # relpath невыразим. «Не смогли выразить путь» = «не смогли посмотреть»:
                        # молча выпавший локатор позволил бы live-проверке реквизитов заявить
                        # полноту, которой не было (contract extension_paths допускает
                        # абсолютные пути с любого диска).
                        _ext_metadata_scan_failed[0] = True
                        continue
                    _extension_metadata_xml.append((cat, obj_name, rel_to_base))
            else:
                _ext_metadata_scan_failed[0] = True

            # --- Synonyms pass: parity with index for search_objects ---
            if _collect_object_synonyms is not None:
                try:
                    syn_rows = _collect_object_synonyms(ext_root_str)
                except Exception:
                    syn_rows = []
                for obj_name, cat, prefixed_synonym, rel_to_ext in syn_rows:
                    try:
                        rel_to_base = os.path.relpath(str(ext_root / rel_to_ext), base_path).replace("\\", "/")
                    except ValueError:
                        continue
                    _extension_synonyms.append((obj_name, cat, prefixed_synonym, rel_to_base))

        if total_ext_files > 5000:
            logger.warning(
                "extension pass scanned %d BSL files — consider RLM_EXTENSION_MAX_FILES env or check ext layout",
                total_ext_files,
            )

    def _ensure_index() -> None:
        if _index_built[0]:
            return
        with _index_lock:
            if _index_built[0]:
                return
            _load_main_into_index_state()
            _load_extensions_into_index_state()
            _index_built[0] = True

    # ``_index_state`` may come from an older SQLite build.  Searches advertised as
    # live must enumerate the current main source tree instead of treating that
    # snapshot as a file catalog.  The source tree is immutable for the lifetime of
    # one helper session, so one filesystem pass is both sufficient and deterministic.
    _live_bsl_catalog: list[tuple[str, BslFileInfo]] = []
    _live_bsl_catalog_built: list[bool] = [False]
    _live_bsl_catalog_lock = threading.Lock()

    def _ensure_live_bsl_catalog() -> list[tuple[str, BslFileInfo]]:
        if _live_bsl_catalog_built[0]:
            return _live_bsl_catalog
        with _live_bsl_catalog_lock:
            if _live_bsl_catalog_built[0]:
                return _live_bsl_catalog
            _ensure_index()

            if idx_reader is None:
                # The non-SQLite index was itself built from the current filesystem.
                entries = list(_index_state)
            else:
                entries = []
                # Do not use glob_files_fn here: in the production sandbox that helper
                # is itself index-backed and therefore can expose the same stale module
                # list we are deliberately bypassing.
                main_root = Path(base_path).resolve()
                for dirpath, dirnames, filenames in os.walk(main_root):
                    dirnames[:] = [
                        name for name in dirnames if name not in _GENERIC_SKIP_DIRS and not name.startswith(".")
                    ]
                    for filename in filenames:
                        if not filename.lower().endswith(".bsl"):
                            continue
                        try:
                            resolved = (Path(dirpath) / filename).resolve()
                            resolved.relative_to(main_root)
                        except (OSError, ValueError):
                            continue
                        info = parse_bsl_path(str(resolved), str(main_root))
                        entries.append((info.relative_path, info))
                # Extension modules are already enumerated live by the side-load pass;
                # the direct walk above is intentionally scoped to the main root.
                entries.extend((rel, info) for rel, info in _index_state if rel in _extension_paths_set)

            unique: dict[str, tuple[str, BslFileInfo]] = {}
            for rel, info in entries:
                normalized = rel.replace("\\", "/")
                unique.setdefault(normalized.casefold(), (normalized, info))
            _live_bsl_catalog.extend(sorted(unique.values(), key=lambda item: item[0].casefold()))
            _live_bsl_catalog_built[0] = True
            return _live_bsl_catalog

    # --- Auto-detect custom prefixes from object names ---
    _detected_prefixes: list[str] = []
    _prefixes_built: list[bool] = [False]
    _prefixes_lock = threading.Lock()

    def _ensure_prefixes() -> list[str]:
        if _prefixes_built[0]:
            return _detected_prefixes
        with _prefixes_lock:
            if _prefixes_built[0]:
                return _detected_prefixes
            _ensure_index()

            # Collect unique object names from index
            object_names: set[str] = set()
            for _, info in _index_state:
                if info.object_name:
                    object_names.add(info.object_name)

            # Custom objects start with a lowercase letter in 1C conventions.
            # Extract prefix: sequence of lowercase letters (+ optional _) before
            # the first uppercase letter.
            prefix_re = re.compile(r"^([a-zа-яё]+_?)")
            prefix_counts: dict[str, int] = {}
            for name in object_names:
                if not name or not name[0].islower():
                    continue
                m = prefix_re.match(name)
                if m:
                    prefix = m.group(1)
                    # Normalize: strip trailing _ for counting, keep in result
                    key = prefix.rstrip("_").lower()
                    if len(key) >= 2:
                        prefix_counts[key] = prefix_counts.get(key, 0) + 1

            # For extensions, lower threshold to 1 (fewer custom objects expected)
            config_role = None
            if idx_reader is not None:
                try:
                    config_role = idx_reader.get_statistics().get("config_role")
                except Exception:
                    pass
            min_count = 1 if config_role == "extension" else 3

            frequent = sorted(
                ((k, v) for k, v in prefix_counts.items() if v >= min_count),
                key=lambda x: -x[1],
            )
            _detected_prefixes.clear()
            _detected_prefixes.extend(k for k, _ in frequent)

            _prefixes_built[0] = True
            return _detected_prefixes

    # --- Strip 1C metadata type prefixes from object names ---
    # Models often pass "Документ.РеализацияТоваровУслуг" instead of "РеализацияТоваровУслуг"
    _META_TYPE_PREFIXES = (
        "Документ.",
        "Справочник.",
        "Перечисление.",
        "РегистрСведений.",
        "РегистрНакопления.",
        "РегистрБухгалтерии.",
        "РегистрРасчета.",
        "Отчет.",
        "Обработка.",
        "ПланОбмена.",
        "ПланСчетов.",
        "ПланВидовХарактеристик.",
        "ПланВидовРасчета.",
        "БизнесПроцесс.",
        "Задача.",
        "Константа.",
        "ПодпискаНаСобытие.",
        "РегламентноеЗадание.",
        "Document.",
        "Catalog.",
        "Enum.",
        "InformationRegister.",
        "AccumulationRegister.",
        "AccountingRegister.",
        "CalculationRegister.",
        "Report.",
        "DataProcessor.",
        "ExchangePlan.",
        "ChartOfAccounts.",
        "ChartOfCharacteristicTypes.",
        "ChartOfCalculationTypes.",
        "BusinessProcess.",
        "Task.",
        "Constant.",
        "DocumentObject.",
        "CatalogObject.",
        "DocumentRef.",
        "CatalogRef.",
        "ДокументОбъект.",
        "СправочникОбъект.",
        "ДокументСсылка.",
        "СправочникСсылка.",
        "ОбщаяФорма.",
        "CommonForm.",
    )

    def _strip_meta_prefix(name: str) -> str:
        """Strip 1C metadata type prefix if present: 'Документ.X' -> 'X'."""
        for prefix in _META_TYPE_PREFIXES:
            if name.startswith(prefix):
                return name[len(prefix) :]
        return name

    def _info_to_dict(relative_path: str, info: BslFileInfo) -> dict:
        return {
            "path": relative_path,
            "category": info.category,
            "object_name": info.object_name,
            "module_type": info.module_type,
            "form_name": info.form_name,
        }

    def _single_or_map(arg, fn):
        """P1 list-перегрузка: целевой аргумент list/tuple → ``{str(x): fn(x)}``
        (изоляция ошибок поэлементно — плохой элемент даёт свой ключ, не роняя
        батч); скаляр → ``fn(arg)`` (прежний контракт байт-в-байт).

        ОБЯЗАН быть ПЕРВЫМ оператором перегруженного хелпера: list не должен
        дойти до скалярной логики (``proc_name.lower()`` / ``_strip_meta_prefix``
        / reader-вызовов), иначе она падает на списке.

        Изоляция РЕАЛЬНАЯ: исключение скаляра на одном элементе ловится и кладётся
        как ``{"error": ...}`` под его ключ, остальные элементы батча доезжают (а не
        обрываются). В скалярном режиме исключение пробрасывается как прежде.
        """
        if isinstance(arg, (list, tuple)):
            out = {}
            for x in arg:
                try:
                    out[str(x)] = fn(x)
                except Exception as exc:  # изоляция: один битый элемент не роняет батч
                    out[str(x)] = {"error": f"{type(exc).__name__}: {exc}"}
            return out
        return fn(arg)

    def _looks_like_path(s) -> bool:
        """P3-детектор: rel-путь модуля всегда содержит '/' (или '\\'), начинается
        с '..' (extension), либо оканчивается на .bsl/.os/.mdo/.xml; имя объекта 1С
        — нет. Внутренние вызовы хелперов всегда передают путь → детект имени не
        срабатывает ложно."""
        if not isinstance(s, str):
            return False
        return "/" in s or "\\" in s or s.startswith("..") or s.lower().endswith((".bsl", ".os", ".mdo", ".xml"))

    def _module_rank(category, module_type) -> int:
        """Единое правило выбора модуля по ПАРЕ (category, module_type) — НЕ по одному
        module_type: у общих модулей и общих/обычных форм module_type всегда 'Module',
        семантику несёт category (MODULE_TYPE_MAP не содержит CommonModule/CommonForm).
        Меньше ранг = выше приоритет:
          0: (CommonModules|CommonForms, Module)  →  1: (*, ObjectModule)  →
          2: (*, ManagerModule)  →  3: всё остальное (формы/команды/прочее)."""
        cat_l = (category or "").lower()
        mt = module_type or ""
        if cat_l in ("commonmodules", "commonforms") and mt == "Module":
            return 0
        if mt == "ObjectModule":
            return 1
        if mt == "ManagerModule":
            return 2
        return 3

    def _resolve_module_arg(arg):
        """P3: принять путь ИЛИ имя объекта. Возвращает ``(path, meta)`` — ``meta``
        ВСЕГДА (даже на path-пути), чтобы у вызывающих не было двух контрактов.

        Путь → ``(arg, {resolved_from_name: False})``. Имя → exact-перечисление
        модулей тем же прямым ``_index_state``-сканом, что и P2 (НЕ capped
        ``find_module``), выбор по единому правилу ``_module_rank``;
        ``meta = {resolved_from_name: True, chosen_module, chosen_reason,
        candidates:[paths по возрастанию ранга], ambiguous}``. ``ambiguous=True``
        когда после приоритета остаётся >1 кандидата одного (верхнего) ранга — выбор
        всё равно детерминирован (первый по стабильной сортировке пути). Имя без
        кандидатов → ``(arg, {... candidates: []})``: трактуем ``arg`` как литеральный
        путь (обратная совместимость — поведение как у прежнего bad-path)."""
        if _looks_like_path(arg):
            return arg, {"resolved_from_name": False}
        _ensure_index()
        # Снять префикс типа (``Документ.X`` → ``X``) перед exact-сканом — как в
        # get_object_modules; иначе ``Документ.X`` не матчит bare object_name и уходит
        # битым путём. Оригинал (arg) сохраняется у вызывающих для error/_meta-текста.
        a_lower = _strip_meta_prefix(arg).lower()
        cands = [(rel, info) for rel, info in _index_state if info.object_name and info.object_name.lower() == a_lower]
        if not cands:
            return arg, {
                "resolved_from_name": True,
                "chosen_module": None,
                "chosen_reason": "name_not_found",
                "candidates": [],
                "ambiguous": False,
            }
        ranked = sorted(cands, key=lambda ri: (_module_rank(ri[1].category, ri[1].module_type), ri[0]))
        top_rank = _module_rank(ranked[0][1].category, ranked[0][1].module_type)
        top = [ri for ri in ranked if _module_rank(ri[1].category, ri[1].module_type) == top_rank]
        chosen_rel, chosen_info = top[0]
        return chosen_rel, {
            "resolved_from_name": True,
            "chosen_module": chosen_rel,
            "chosen_reason": f"({chosen_info.category}, {chosen_info.module_type})",
            "candidates": [rel for rel, _ in ranked],
            "ambiguous": len(top) > 1,
        }

    # ── Helper registry ──────────────────────────────────────────
    _registry: dict[str, dict] = {}

    def _reg(name: str, fn, sig: str, cat: str, kw: list[str] | None = None, recipe: str = ""):
        """Register a helper: sig for strategy table, kw+recipe for help()."""
        _registry[name] = {
            "fn": fn,
            "sig": sig,
            "cat": cat,
            "kw": kw or [],
            "recipe": recipe,
        }

    def _find_module_matches(
        name: str = "",
        module_type: str = "",
        category: str = "",
        max_results: int | None = None,
        entries: list[tuple[str, BslFileInfo]] | None = None,
    ) -> list[dict]:
        """Shared matcher for public ``find_module`` and internally exhaustive scans."""
        name = _strip_meta_prefix(name)
        if entries is None:
            _ensure_index()
            entries = _index_state
        name_lower = name.lower()
        mt_lower = module_type.lower() if module_type else ""
        cat_lower = category.lower() if category else ""
        results = []
        for relative_path, info in entries:
            matched = False
            if info.object_name and name_lower in info.object_name.lower():
                matched = True
            if not matched and name_lower in relative_path.lower():
                matched = True
            if matched and mt_lower and (info.module_type or "").lower() != mt_lower:
                matched = False
            if matched and cat_lower and (info.category or "").lower() != cat_lower:
                matched = False
            if matched:
                results.append(_info_to_dict(relative_path, info))
            if max_results is not None and len(results) >= max_results:
                break
        return results

    def find_module(name: str = "", module_type: str = "", category: str = "") -> list[dict]:
        """Find BSL modules by name fragment (case-insensitive).

        v1.19.0 — tolerant contract: ``name`` is OPTIONAL and ``module_type`` /
        ``category`` are optional filters (matched case-insensitively against the
        output fields). Agents naturally try both ``find_module(name,
        module_type='ObjectModule')`` AND filter-only ``find_module(module_type=
        'ObjectModule')`` (no name) — instead of raising, both work: an empty
        ``name`` means "any module", narrowed by the filters and capped at 50.

        Returns: list of dicts {path, category, object_name, module_type, form_name}."""
        return _find_module_matches(name, module_type, category, max_results=50)

    def find_by_type(meta_type: str, name: str = "") -> list[dict]:
        """Find BSL modules by metadata category, optionally filtered by object name.

        Accepts plural folder names (InformationRegisters), singular (InformationRegister),
        and Russian names (РегистрСведений).
        Categories: CommonModules, Documents, Catalogs, InformationRegisters,
        AccumulationRegisters, AccountingRegisters, CalculationRegisters,
        Reports, DataProcessors, Constants.

        Returns: list of dicts {path, category, object_name, module_type, form_name}."""
        name = _strip_meta_prefix(name)
        _ensure_index()
        meta_type_lower = _normalize_category(meta_type)
        name_lower = name.lower()
        results = []
        for relative_path, info in _index_state:
            if not info.category or info.category.lower() != meta_type_lower:
                continue
            if name_lower and (not info.object_name or name_lower not in info.object_name.lower()):
                continue
            results.append(_info_to_dict(relative_path, info))
            if len(results) >= 50:
                break
        return results

    _proc_lazy = LazyDict()
    _prefilter_lazy = LazyDict()

    def _parse_procedures(path: str) -> list[dict]:
        """Parse BSL file — internal, result gets cached by LazyDict.

        Handles multi-line procedure signatures (``Процедура X(a,\n  b)``) by
        merging continuation lines before matching ``BSL_PATTERNS['procedure_def']``.
        ``end_line`` is taken from the original line list.
        """
        content = _ext_read_file(path)
        lines = content.splitlines()
        merged_lines, line_map = _merge_proc_continuations(lines)
        total_orig = len(lines)
        total_merged = len(merged_lines)

        proc_def_re = re.compile(BSL_PATTERNS["procedure_def"], re.IGNORECASE)
        proc_end_re = re.compile(BSL_PATTERNS["procedure_end"], re.IGNORECASE)

        procedures: list[dict] = []
        m_idx = 0
        while m_idx < total_merged:
            merged = merged_lines[m_idx]
            m = proc_def_re.search(merged)
            if not m:
                m_idx += 1
                continue

            proc_type = m.group(1)
            proc_name = m.group(2)
            # v1.18.0 Фикс 2: params -> list[str] имён параметров (на агент-границе).
            params = _split_params(m.group(3) or "")
            is_export = m.group(4) is not None and m.group(4).strip() != ""
            line_number = line_map[m_idx]  # 1-based

            next_start = line_map[m_idx + 1] if m_idx + 1 < total_merged else total_orig + 1
            scan_from = next_start - 1

            end_line: int | None = None
            for orig_idx in range(scan_from, total_orig):
                if proc_end_re.search(lines[orig_idx]):
                    end_line = orig_idx + 1
                    break

            if end_line is None:
                procedures.append(
                    {
                        "name": proc_name,
                        "type": proc_type,
                        "line": line_number,
                        "is_export": is_export,
                        "end_line": total_orig,
                        "params": params,
                    }
                )
                break

            procedures.append(
                {
                    "name": proc_name,
                    "type": proc_type,
                    "line": line_number,
                    "is_export": is_export,
                    "end_line": end_line,
                    "params": params,
                }
            )

            new_m = m_idx + 1
            while new_m < total_merged and line_map[new_m] <= end_line:
                new_m += 1
            m_idx = new_m

        return procedures

    def _attach_overrides(result: list[dict], overrides_map: dict | None) -> None:
        """Mutate ``result`` in place: attach ``overridden_by`` from a
        case-insensitive (Cyrillic) ``{name -> [override_dicts]}`` map.
        """
        if not overrides_map:
            return
        ov_lower = {k.lower(): v for k, v in overrides_map.items()}
        for proc in result:
            method_overrides = ov_lower.get(proc["name"].lower())
            if method_overrides:
                proc["overridden_by"] = [
                    {
                        "annotation": ov.get("annotation", ""),
                        "extension_name": ov.get("extension_name", ""),
                        "extension_method": ov.get("extension_method", ""),
                        "extension_root": ov.get("extension_root", ""),
                        "ext_module_path": ov.get("ext_module_path", ""),
                        "ext_line": ov.get("ext_line"),
                    }
                    for ov in method_overrides
                ]

    def extract_procedures(path: str) -> list[dict]:
        """Parse BSL file and return list of procedures/functions with metadata.
        Results are memoized per file path within the session.
        Uses SQLite index when available (instant), falls back to regex parsing.

        For indexed paths, also performs an opportunistic live-fill: if the
        live regex parser finds a procedure NOT present in the index (typically
        a multi-line signature that older indexes missed), it is appended to
        the result with the same shape, including ``overridden_by`` enrichment
        from ``idx_reader.get_overrides_for_path``. This makes the helper
        self-healing — multi-line procedures appear immediately, without
        requiring ``rlm-bsl-index index update``.

        ``path`` — rel_path модуля ИЛИ имя объекта (P3). По имени модуль выбирается
        единым правилом ``(category, module_type)`` (см. ``_resolve_module_arg``)
        ТОЛЬКО при детерминированном выборе; при неоднозначности — **``ValueError``**
        (а НЕ ``[]``: пустой список неотличим от «у модуля нет процедур» и тихо
        просаживает анализ). Прозрачное разрешение по имени с ``_meta`` —
        ``get_module_outline``. Внутренние вызовы передают реальные пути.

        Returns: list of dicts {name, type, line, end_line, is_export, params, overridden_by?}.
        ``params`` — список имён параметров (list[str], v1.18.0; напр. "Знач А, Б=5" → ["А", "Б"])."""

        _orig_arg = path
        path, _arg_meta = _resolve_module_arg(path)
        if _arg_meta.get("resolved_from_name") and _arg_meta.get("ambiguous"):
            raise ValueError(
                f"неоднозначное имя модуля '{_orig_arg}': кандидаты {_arg_meta['candidates']}; "
                "передайте путь или используйте get_module_outline (прозрачный авто-выбор в _meta)"
            )

        def _extract_with_index():
            overrides_map: dict | None = None
            if idx_reader is not None:
                try:
                    overrides_map = idx_reader.get_overrides_for_path(path)
                except Exception:
                    overrides_map = None

            result: list[dict] | None = None
            if idx_reader is not None:
                idx_result = idx_reader.get_methods_by_path(path)
                if idx_result is not None:
                    # v1.18.0 Фикс 2: нормализуем params строкой -> list на границе.
                    result = _normalize_method_params(idx_result)
                    _attach_overrides(result, overrides_map)

            if result is None:
                # No index — fall through to live parsing.
                live = _parse_procedures(path)
                _attach_overrides(live, overrides_map)
                return live

            # Opportunistic live-fill: add procedures missing from the index.
            try:
                live = _parse_procedures(path)
            except Exception:
                return result
            existing_names = {p["name"].lower() for p in result}
            additions: list[dict] = []
            for proc in live:
                if proc["name"].lower() in existing_names:
                    continue
                additions.append(proc)
            if additions:
                _attach_overrides(additions, overrides_map)
                result.extend(additions)
            return result

        return _proc_lazy.get_or_set(path, _extract_with_index)

    def find_exports(path: str) -> list[dict]:
        """Return only exported procedures/functions from a BSL file.

        Returns: list of dicts {name, type, line, end_line, is_export, params}.
        ``params`` — список имён параметров (list[str], v1.18.0)."""
        return [p for p in extract_procedures(path) if p["is_export"]]

    def safe_grep(
        pattern: str,
        name_hint: str = "",
        max_files: int = 20,
        _result_cap: int | None = None,
    ) -> list[dict]:
        r"""Parallel grep across BSL files, optionally scoped by module name hint.

        Public contract is unchanged: returns ``[{file, line, text}]`` (no sentinel,
        no result cap — scope is bounded by *max_files* candidates).  Generated
        internal routes may pass ``_result_cap``; then scanning stops in bounded
        batches and an early stop is reported by a final
        ``{"_truncated": True, "shown": N}`` sentinel. ``file`` is always
        POSIX-separated (``/``), homogeneous across the git/Python/extension branches
        (#7, v1.28.0). When the sources
        are under git **and** *pattern* is a plain literal, the non-extension
        (base) candidates are searched with a single ``git grep`` call instead of
        a thread-pool of per-file Python greps — the result is identical (literal
        == substring) but far cheaper. Real regexes stay on Python ``re`` (git
        ``-E`` is POSIX ERE, not equivalent to Python ``re``), and extension files
        always use the Python path (they live outside the sandbox base, which
        ``git -C base`` would not see).

        **Regex guard (v1.26.0, Finding #2):** входной *pattern* проверяется на
        вложенные неограниченные кванторы (``(a+)+b`` и т.п.) ПЕРВЫМ действием —
        такие паттерны вызывают catastrophic backtracking в C-движке ``_sre``,
        который таймаут песочницы (``PyThreadState_SetAsyncExc``) не прерывает.
        При срабатывании — ``ValueError`` с подсказкой (литерал / ``name_hint``).
        Guard отклоняет и «невинно выглядящие» перекрывающиеся паттерны
        (``(\w+\s*)+``) — это ожидаемо (реальный ReDoS), не баг. Это отсечение
        ЯВНЫХ exponential-паттернов по структуре, НЕ полноценный wall-clock-kill
        (для него нужна процессная изоляция); ``Timeout-safe`` это ранее обещал
        ложно. Литералы (git fast-path) guard'ом не затрагиваются.
        """
        # Guard ПЕРВЫМ действием — до _ensure_index/find_module/выбора файлов/
        # git-literal fast-path/thread-pool. _grep_one глотает Exception (см. ниже),
        # поэтому полагаться на guard внутри grep_fn нельзя.
        if has_catastrophic_nesting(pattern):
            raise ValueError(NESTED_QUANTIFIER_ERROR)
        # Validate the regex up-front too (#5): a syntactically broken pattern (e.g. "(")
        # used to raise a raw ``re.error`` traceback далеко ниже (после прогрева индекса и
        # выбора файлов). Compile ЗДЕСЬ — до _ensure_index/find_module — и переиспользуем
        # ``compiled`` в _grep_one; кривой паттерн даёт чистый ValueError, а не сырой re.error.
        try:
            compiled = re.compile(pattern)
        except re.error as e:
            raise ValueError(f"Некорректный regex: {e}. Упростите паттерн или используйте литерал/name_hint.") from None
        live_catalog = _ensure_live_bsl_catalog()

        if name_hint:
            # Public find_module is intentionally capped at 50. safe_grep has its own
            # max_files contract, so routing through that public cap made max_files > 50
            # silently ineffective and could undercount exhaustive internal scans.
            candidates = _find_module_matches(name_hint, entries=live_catalog)
            paths = [c["path"] for c in candidates[:max_files]]
        else:
            paths = [relative_path for relative_path, _ in live_catalog[:max_files]]

        if not paths:
            return []

        result_cap = None if _result_cap is None else max(0, int(_result_cap))
        results: list[dict] = []
        truncated = result_cap == 0
        py_paths: list[str] = list(paths)  # files still needing the Python path

        # Fast literal path via git grep over base (non-extension) candidates.
        if _is_literal_pattern(pattern) and _git_search_available():
            base_paths = [p for p in paths if p not in _extension_paths_set]
            if base_paths:
                from rlm_tools_bsl.bsl_index import _git_grep

                git_res = _git_grep(
                    base_path,
                    pattern,
                    literal_files=base_paths,
                    regex=False,
                    mode="lines",
                    max_results=result_cap + 1 if result_cap is not None else 10**9,
                    max_per_file=0,  # no per-file cap (parity with Python path)
                    include_truncation_sentinel=False,  # strict [{file,line,text}]
                )
                if git_res is not None:
                    results.extend(git_res)
                    base_set = set(base_paths)
                    py_paths = [p for p in paths if p not in base_set]
                    if result_cap is not None and len(results) > result_cap:
                        results = results[:result_cap]
                        truncated = True
                        py_paths = []
                    elif result_cap is not None and len(results) == result_cap and py_paths:
                        # Reaching the cap with unsearched extension files is a partial
                        # search even if those files would ultimately add no hits.
                        truncated = True
                        py_paths = []

        # ``compiled`` was validated/compiled up-front (before _ensure_index) — reuse it.
        def _grep_one(path: str) -> list[dict]:
            # Base paths: delegate to generic grep (cached, sandbox-checked).
            # Extension paths: read via _ext_read_file (sandbox base-only grep
            # would raise PermissionError) and apply the same regex contract.
            if path in _extension_paths_set:
                try:
                    content = _ext_read_file(path)
                except Exception:
                    return []
                out: list[dict] = []
                for i, line in enumerate(content.splitlines(), 1):
                    if compiled.search(line):
                        out.append({"file": path, "line": i, "text": line.strip()})
                        if result_cap is not None and len(out) > result_cap:
                            break
                return out
            try:
                matches = grep_fn(pattern, path) or []
                return matches[: result_cap + 1] if result_cap is not None else matches
            except Exception:
                return []

        if not truncated and py_paths:
            from concurrent.futures import ThreadPoolExecutor as _TP

            # Public calls preserve the one-shot behavior.  Capped generated routes
            # use small batches so a common method does not read the entire tree after
            # enough candidates have already been collected.
            path_batch_size = len(py_paths) if result_cap is None else min(64, len(py_paths))
            for start in range(0, len(py_paths), path_batch_size):
                path_batch = py_paths[start : start + path_batch_size]
                if len(path_batch) > 1:
                    with _TP(max_workers=min(8, len(path_batch))) as pool:
                        all_results = list(pool.map(_grep_one, path_batch))
                    for matches in all_results:
                        results.extend(matches)
                else:
                    results.extend(_grep_one(path_batch[0]))

                if result_cap is not None and len(results) >= result_cap:
                    batch_end = start + len(path_batch)
                    truncated = len(results) > result_cap or batch_end < len(py_paths)
                    results = results[:result_cap]
                    break

        # #7: normalize `file` to POSIX '/' at the single assembly point — covers the
        # git, Python base-grep and extension branches at once, so a single result set is
        # homogeneous (helpers.grep directory-walk yields '\' on Windows) and the (file,
        # line) sort is stable. Convention as in find_roles ('.replace("\\","/")').
        # COPY, never mutate in place: ``grep_fn`` is normally ``helpers.grep``, which
        # caches its result list and hands the SAME dicts back on a cache hit — an
        # in-place rewrite would silently and permanently change what a later direct
        # ``grep()`` returns in this session (low-level grep contract stays untouched).
        results = [{**m, "file": str(m["file"]).replace("\\", "/")} if m.get("file") else m for m in results]
        # Deterministic order: sort by (file, line)
        results.sort(key=lambda m: (m.get("file", ""), m.get("line", 0)))
        if truncated:
            results.append({"_truncated": True, "shown": len(results)})
        return results

    def git_search(
        pattern: str,
        path: str = "",
        file_types: str = "",
        regex: bool = False,
        ignore_case: bool = False,
        mode: str = "lines",
        max_results: int = 200,
        exclude_path: str = "",
    ) -> list[dict]:
        """Full-text search across ALL files under git (opt-in, only when the
        sources are a git work-tree).

        Unlike ``safe_grep`` (scoped to a known module / a bounded candidate set)
        this searches every tracked + untracked-not-ignored file — including raw
        ``.xml``/``.mdo`` (forms, rights, DCS, ConfigDumpInfo) and procedure
        bodies / string literals / query text that the name-based helpers and the
        SQLite index never see.

        Args:
            pattern: literal substring (default) or POSIX ERE when *regex* is True.
            path: optional subtree/file filter (e.g. ``"CommonModules"``).
            file_types: optional comma-separated extensions (e.g. ``"bsl,xml"``).
            regex: treat *pattern* as POSIX ERE. NOTE: on CRLF files a trailing
                CR sits before the line end, so the ``$`` anchor needs
                ``[[:space:]]*$`` (git matches bytes and its ERE does NOT read
                ``\\r`` as a carriage return — it is a literal ``r``).
            ignore_case: case-insensitive match.
            mode: ``"lines"`` → ``[{file, line, text}]``; ``"files"`` → ``[{file}]``
                (cheap overview — use first on common tokens, then drill down).
            max_results: cap; when hit, the last element is
                ``{"_truncated": True, "shown": max_results}``.
            exclude_path: optional comma-separated list of **literal** directory/
                file names to drop from the search (e.g. ``"Forms,Templates"`` or
                ``"ConfigDumpInfo.xml"``). Matched at **any depth** — a nested
                ``*/Forms/*`` is excluded just like a top-level ``Forms``. Glob
                metachars are rejected (literal only, like *path*); a malformed
                element → ``[{"error": ...}]`` rather than a silently widened
                search. Applied on top of the positive scope; with no positive
                scope the exclusion spans the whole tree.

        Returns the hit list, or ``[{"error": ..., "hint": ...}]`` (distinct from ``[]``
        = nothing found). **Причина НАЗВАНА.** Аргументные ошибки классифицируются здесь и
        называют виновника: ``mode``, ``pattern`` (NL/NUL; при ``regex=True`` — еще и
        некомпилируемое выражение), ``path``, ``file_types``, ``exclude_path``. **Но проверить
        POSIX ERE на стороне Python НЕЛЬЗЯ** (``re`` — надмножество: ``(?=a)``, ``(?P<x>a)``
        компилируются, а ``git grep -E`` их отвергает), поэтому вердикт по таким выражениям
        выносит САМ git: ``_git_grep`` отдает причину через ``err``.
        **Виновника при ``rc>=2`` называет STDERR, а не флаг ``regex``:** тем же ``rc=128`` git
        отвечает и на битое выражение, и на настоящий отказ (не git-репозиторий, поврежденный
        индекс). Ошибку компиляции git печатает ЭХОМ паттерна (``fatal: -e option, '(': ...``) —
        только тогда ответ винит **pattern**. Иначе это ``"git grep failed or timed out"``, и оно
        остается РОВНО за настоящим отказом git (недоступен / не репозиторий / поврежден /
        таймаут) при ЛЮБОМ значении ``regex``.
        Раньше ``_git_grep`` отдавал ``None`` на ВСЕ причины разом, хелпер схлопывал их в одно
        сообщение — и агент, сломавший СВОЙ аргумент, шел чинить git.
        Форма ошибки ЕДИНАЯ: ``hint`` есть на КАЖДОМ ошибочном пути, поэтому ``result[0]["hint"]``
        безопасен. Fallback в hint НЕ равноценен ДВАЖДЫ: ``safe_grep`` ищет только по BSL и без
        ``name_hint`` ограничен ``max_files`` кандидатами (для не-BSL и широкого поиска hint уводит
        в ``find_module``/``glob_files`` + ``grep(pattern, конкретный_путь)``), И меняет СЕМАНТИКУ
        паттерна: ``git_search`` по умолчанию литеральный (``git grep -F``), а ``safe_grep``/``grep``
        компилируют аргумент как Python-regex — поэтому hint велит экранировать (``re.escape``) и
        предупреждает про расхождение диалектов POSIX ERE vs Python ``re``.
        """
        # v1.18.0 Фикс 4a: пустой/пробельный паттерн -> внятный [{error, hint}]
        # (list-форма, как и любой результат git_search), а не таймаут-заглушка.
        if not pattern or not pattern.strip():
            return [
                {
                    "error": "empty pattern",
                    "hint": (
                        "задайте непустую подстроку или regex; для поиска по типу объекта — "
                        "find_by_type(...), по имени метода — search_methods(...)."
                    ),
                }
            ]
        from rlm_tools_bsl.bsl_index import (
            _git_grep,
            _sanitize_grep_excludes,
            _sanitize_grep_file_types,
            _sanitize_grep_path,
        )

        # РАЗВОДИМ ПРИЧИНЫ. ``_git_grep`` отдаёт None на ВСЁ подряд: битый фильтр
        # (path/file_types/exclude_path), неподдерживаемый ``mode``, NL/NUL в ``pattern`` — и на
        # настоящий сбой/таймаут git. Хелпер схлопывал это в "git grep failed or timed out", и
        # агент, сломавший СВОЙ аргумент, читал ответ как «git сломался» — шёл чинить не то (или
        # вовсе бросал git_search). Классифицируем КАЖДУЮ аргументную причину ЗДЕСЬ и называем
        # виновника; только после этого ``res is None`` означает РОВНО отказ git.
        # Порядок и содержание проверок ОБЯЗАНЫ повторять ранние guard'ы _git_grep
        # (bsl_index: mode -> pattern -> pathspec) — иначе классификация разъедется с реальностью.
        _FILTER_HINT = (
            "ожидается ОТНОСИТЕЛЬНЫЙ литеральный путь внутри конфигурации (напр. "
            "'CommonModules/ИмяМодуля'); Windows-разделитель '\\' допустим — он нормализуется. "
            "Отвергаются: rooted/абсолютные пути (\\CommonModules, C:\\...), UNC "
            "(\\\\server\\...), '..'-сегменты, "
            "NUL, glob-метасимволы (* ? [ ]) и git pathspec-magic (':/', ':(...)')."
        )
        if mode not in ("lines", "files"):
            return [
                {
                    "error": f"некорректный mode={mode!r}",
                    "hint": "допустимо 'lines' (по умолчанию, отдает {file,line,text}) или 'files' (только {file}).",
                }
            ]
        if "\n" in pattern or "\x00" in pattern:
            return [
                {
                    "error": "pattern содержит перевод строки или NUL",
                    "hint": (
                        "git трактовал бы их как НЕСКОЛЬКО -e паттернов (неожиданный OR-поиск) — не "
                        "поддержано. Ищи по одной строке; для нескольких токенов зови git_search несколько раз."
                    ),
                }
            ]
        if regex:
            # БЫСТРАЯ отсечка, а НЕ полная проверка: ловит выражения, битые и в Python, и в ERE
            # (несбалансированные скобки, nothing to repeat) — без запуска подпроцесса.
            # ПОЛНОТЫ здесь быть не может: git зовется с -E (POSIX ERE), а Python re — надмножество,
            # поэтому lookahead (?=a) и именованные группы (?P<x>a) КОМПИЛИРУЮТСЯ здесь, но git их
            # отвергает (rc=128, "Invalid preceding regular expression"). Такие случаи ловятся ниже
            # по вердикту САМОГО git (err["kind"]=="rc") — гадать на нашей стороне бессмысленно.
            # FutureWarning глушим: POSIX-класс [[:space:]] (наша же каноничная рекомендация
            # поиска объявлений) Python считает «possible nested set», но исполняет паттерн git,
            # а здесь — только sanity-отсечка; warning на КАЖДЫЙ рекомендованный вызов засорял
            # бы server.log.
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", FutureWarning)
                    re.compile(pattern)
            except re.error as exc:
                return [
                    {
                        "error": f"некорректный pattern={pattern!r} (regex=True)",
                        "hint": (
                            f"выражение не компилируется: {exc}. git зовется с -E (POSIX ERE) и отвергнет "
                            "его так же. Экранируй спецсимволы или ищи подстроку буквально: regex=False."
                        ),
                    }
                ]
        if _sanitize_grep_path(path) is None:
            return [{"error": f"некорректный path={path!r}", "hint": _FILTER_HINT}]
        if _sanitize_grep_file_types(file_types) is None:
            return [
                {
                    "error": f"некорректный file_types={file_types!r}",
                    "hint": "ожидается список расширений без точек и глобов, напр. 'bsl,xml'.",
                }
            ]
        if _sanitize_grep_excludes(exclude_path) is None:
            return [{"error": f"некорректный exclude_path={exclude_path!r}", "hint": _FILTER_HINT}]

        # ЗАМЕНА неравноценна ДВАЖДЫ, и вторая половина важнее первой. (1) Область поиска:
        # safe_grep ходит только по BSL. (2) СЕМАНТИКА PATTERN: git_search по умолчанию
        # ЛИТЕРАЛЬНЫЙ (git grep -F), а safe_grep/grep компилируют аргумент как Python-regex
        # (helpers.grep: re.compile; safe_grep: то же). Отправить агента с литеральным '(' в
        # safe_grep — значит отправить его в ValueError, а литеральную '.' тихо превратить в
        # «любой символ». Молчать об этом нельзя: подсказка на аварийном пути обязана быть
        # исполнимой, иначе она — второй отказ подряд.
        # Аварийный совет обязан быть ИСПОЛНИМЫМ ДОСЛОВНО: в свежей песочнице нет ни переменной
        # `pattern`, ни предзагруженного `re` (модуль лишь РАЗРЕШЕН к import) — совет вида
        # safe_grep(re.escape(pattern), ...) после настоящего отказа git давал бы NameError,
        # то есть второй отказ подряд. Поэтому эквивалент готовит СЕРВЕР: он сам экранирует
        # литеральный паттерн (re.escape) и вставляет РЕЗУЛЬТАТ готовым Python-литералом.
        # Гигантский паттерн (не наш случай в 99%) раздул бы hint — тогда маршрут с явным import.
        if len(pattern) <= 300:
            if regex:
                _semantics_route = (
                    f"Эквивалент с ТЕМ ЖЕ выражением (про разницу диалектов — ниже): "
                    f"safe_grep({pattern!r}, 'ИмяМодуля'); для выбранного не-BSL файла — "
                    f"grep({pattern!r}, 'конкретный/путь'). "
                )
            else:
                escaped_pattern = re.escape(pattern)
                _semantics_route = (
                    f"Готовый эквивалент — экранирование (re.escape) уже применил СЕРВЕР, копируй как "
                    f"есть, подставив только модуль/путь: safe_grep({escaped_pattern!r}, 'ИмяМодуля'); "
                    f"для выбранного не-BSL файла — grep({escaped_pattern!r}, 'конкретный/путь'). "
                )
        elif regex:
            # Для regex экранирование НЕ эквивалент, а смена смысла: re.escape превратил бы
            # выражение в литерал — и совет противоречил бы соседнему «при regex=True
            # экранировать не надо».
            _semantics_route = (
                "Паттерн длинный — перенеси выражение в safe_grep/grep КАК ЕСТЬ, БЕЗ re.escape "
                "(экранирование превратило бы regex в литерал); для grep сначала выбери конкретный файл; "
                "про разницу диалектов — ниже. "
            )
        else:
            _semantics_route = (
                "Паттерн длинный — собери эквивалент сам: import re; p = re.escape('<твой литерал>'); "
                "safe_grep(p, 'ИмяМодуля'); для выбранного не-BSL файла — grep(p, 'конкретный/путь') "
                "(модуль re в песочнице разрешен, но НЕ предзагружен — import обязателен). "
            )
        _FALLBACK_HINT = (
            "ЗАМЕНА НЕ РАВНОЦЕННАЯ, и дело не только в области поиска. "
            "(1) ОБЛАСТЬ: safe_grep ищет ТОЛЬКО по BSL и без name_hint смотрит лишь первые max_files "
            "кандидатов. Для не-BSL (xml и пр.) или широкого поиска сузь область через "
            "find_module/glob_files и зови grep с подготовленным ниже паттерном и КОНКРЕТНЫМ путем из их ответа — "
            "grep по широкому каталогу откажет намеренно. "
            "(2) СЕМАНТИКА PATTERN: git_search по умолчанию ЛИТЕРАЛЬНЫЙ (git grep -F), а safe_grep и grep "
            "компилируют аргумент как Python-regex: литеральная '(' у них упадет ошибкой, а '.' станет "
            f"«любым символом». {_semantics_route}"
            "При regex=True экранировать не надо, но диалекты РАЗНЫЕ: "
            "у git POSIX ERE, у замен Python re — lookahead (?=...), (?P<x>...), \\d есть только в Python re."
        )
        # err — канал ПРИЧИНЫ (см. _git_grep). Без него «git лежит» и «git отверг ТВОЙ pattern»
        # неразличимы, и хелперу остается гадать. Гадание и было корнем: битый ERE уезжал в
        # "git grep failed". Теперь причину называет САМ git.
        err: dict = {}
        res = _git_grep(
            base_path,
            pattern,
            path=path,
            file_types=file_types,
            exclude_path=exclude_path,
            regex=regex,
            ignore_case=ignore_case,
            mode=mode,
            max_results=max_results,
            include_truncation_sentinel=True,
            err=err,
        )
        if res is None:
            # Форма ошибки ЕДИНАЯ ({error, hint}) на ВСЕХ путях — потребитель, которому докстринг
            # обещал hint, не должен ловить KeyError именно на аварийном.
            git_msg = ""
            if err.get("kind") == "rc":
                # git запустился и САМ сказал, что не так. Его сообщение точнее любой эвристики.
                lines = [ln for ln in (err.get("stderr") or "").splitlines() if ln.strip()]
                stderr = err.get("stderr") or ""
                git_msg = lines[0].strip()[:200] if lines else f"rc={err.get('rc')}"
                # СУДЬЯ — stderr, а НЕ флаг regex. rc>=2 это не синоним «git отверг твой pattern»:
                # тем же rc=128 git отвечает на повреждённый индекс, отсутствующий объект и
                # «not a git repository». Классификация по regex обвинила бы КОРРЕКТНОЕ ERE-выражение
                # («перепиши») и увела бы настоящий отказ из "git grep failed or timed out" — то есть
                # воспроизвела бы ровно тот дефект, который этот код чинит (валим вину не на того).
                # Разводит их сам git: ошибку компиляции выражения он печатает ЭХОМ паттерна
                # (fatal: -e option, '(': Unmatched ( or \(), а отказ — без него. Проверяем оба
                # маркера: "-e option" (прямой) и эхо паттерна в кавычках (переживает локализацию git).
                if "-e option" in stderr or f"'{pattern}'" in stderr:
                    ere_hint = (
                        "git ищет по POSIX ERE, а не по синтаксису Python: lookahead (?=...), "
                        "lookbehind, именованные группы (?P<...>), \\d/\\A/\\Z в нем НЕ "
                        "поддержаны (re.compile их принимает — потому предварительная проверка "
                        "их и пропускает). Перепиши выражение в ERE или ищи подстроку "
                        "буквально: regex=False."
                    )
                    return [
                        {
                            "error": f"pattern отвергнут git grep -E: {git_msg}",
                            "hint": ere_hint if regex else f"{git_msg}. {_FALLBACK_HINT}",
                        }
                    ]
            # Сюда попадают ТОЛЬКО настоящие отказы: git недоступен / не git-репозиторий /
            # повреждён репозиторий / таймаут / не удалось запустить процесс. Аргументы уже
            # провалидированы выше, а pattern git не оспаривал — значит сообщение честное.
            return [
                {
                    "error": "git grep failed or timed out",
                    "hint": (
                        "git недоступен, каталог не под git, репозиторий поврежден или поиск упал по "
                        "таймауту (git_search работает ТОЛЬКО в git-репозитории)."
                        + (f" git ответил: {git_msg}." if git_msg else "")
                        + f" {_FALLBACK_HINT}"
                    ),
                }
            ]
        return res

    def _read_procedure_one(
        path: str, proc_name: str, include_overrides: bool = False, numbered: bool = False
    ) -> str | None:
        """Scalar core of read_procedure (single proc_name). See read_procedure."""
        procedures = extract_procedures(path)
        target = None
        for p in procedures:
            if p["name"].lower() == proc_name.lower():
                target = p
                break
        if target is None:
            return None

        content = _ext_read_file(path)
        lines = content.splitlines()

        start = target["line"] - 1  # convert to 0-based
        end = target["end_line"] if target["end_line"] is not None else len(lines)
        # end_line is 1-based and inclusive
        extracted = lines[start:end]
        body = "\n".join(extracted)

        if numbered:
            from rlm_tools_bsl._format import number_lines

            body = number_lines(body, start=target["line"])

        if not include_overrides:
            return body

        # Enrich with extension override bodies
        override_list = target.get("overridden_by")
        if not override_list and idx_reader is not None:
            try:
                overrides_map = idx_reader.get_overrides_for_path(path)
                # Case-insensitive lookup (Cyrillic)
                ov_lower = {k.lower(): v for k, v in overrides_map.items()}
                override_list = ov_lower.get(target["name"].lower())
            except Exception:
                override_list = None

        if not override_list:
            return body

        from rlm_tools_bsl.extension_detector import detect_extension_context as _det_ctx

        try:
            ext_context = _det_ctx(base_path)
        except Exception:
            return body

        trusted_roots: set[Path] = set()
        for e in ext_context.nearby_extensions:
            trusted_roots.add(Path(e.path).resolve())
        trusted_roots.add(Path(ext_context.current.path).resolve())

        parts = [body]
        for ov in override_list:
            ext_root = ov.get("extension_root", "")
            ext_mod = ov.get("ext_module_path", "")
            annotation = ov.get("annotation", "")
            ext_name = ov.get("extension_name", "")
            ext_method = ov.get("extension_method", "")
            ext_line = ov.get("ext_line")

            header = f'\n// === Перехвачен &{annotation} в расширении "{ext_name}" ==='
            file_ref = f"// Файл: {ext_name}/{ext_mod}"
            if ext_line:
                file_ref += f":{ext_line}"

            # Try to read extension method body
            ext_body = None
            if ext_root and ext_mod:
                candidate = Path(ext_root, ext_mod).resolve()
                if any(candidate.is_relative_to(root) for root in trusted_roots):
                    try:
                        ext_content = candidate.read_text(encoding="utf-8-sig", errors="replace")
                        ext_lines = ext_content.splitlines()
                        # Find method by name in extension file
                        proc_def_re = re.compile(BSL_PATTERNS["procedure_def"], re.IGNORECASE)
                        proc_end_re = re.compile(BSL_PATTERNS["procedure_end"], re.IGNORECASE)
                        search_name = (ext_method or "").lower()
                        in_target = False
                        start_idx = None
                        for i, ln in enumerate(ext_lines):
                            if not in_target:
                                m = proc_def_re.search(ln)
                                if m and m.group(2).lower() == search_name:
                                    in_target = True
                                    start_idx = i
                            else:
                                if proc_end_re.search(ln):
                                    ext_body = "\n".join(ext_lines[start_idx : i + 1])
                                    break
                        if in_target and ext_body is None and start_idx is not None:
                            ext_body = "\n".join(ext_lines[start_idx:])
                    except OSError:
                        pass

            parts.append(header)
            parts.append(file_ref)
            if ext_body:
                if numbered and start_idx is not None:
                    from rlm_tools_bsl._format import number_lines

                    ext_body = number_lines(ext_body, start=start_idx + 1)
                parts.append(ext_body)

        return "\n".join(parts)

    def read_procedure(path: str, proc_name, include_overrides: bool = False, numbered: bool = False):
        """Extract a single procedure body from a BSL file by name.
        With include_overrides=True, appends extension override bodies if available.

        ``proc_name`` — ``str`` (прежний контракт: ``str | None``) ИЛИ ``list[str]``
        (P1 list-перегрузка → ``{proc_name: str | None | {error}}``, тело модуля
        парсится один раз благодаря мемоизации ``extract_procedures``). Изоляция:
        ненайденный метод даёт ``None`` под своим ключом, а исключение на элементе —
        ``{"error": ...}`` (не роняя остальной батч); в обходе проверяй ``'error' in v``."""
        return _single_or_map(
            proc_name,
            lambda name: _read_procedure_one(path, name, include_overrides, numbered),
        )

    def find_callers(proc_name: str, module_hint: str = "", max_files: int = 20) -> list[dict]:
        """Find all callers of a procedure by name across BSL files.
        Delegates to find_callers_context for thorough cross-module search.

        Returns: list of dicts {file, line, text}."""
        result = find_callers_context(proc_name, module_hint, 0, max_files)
        return [{"file": c["file"], "line": c["line"], "text": c.get("context", "")} for c in result["callers"]]

    # --- Parallel prefilter for find_callers_context ---
    _base = Path(base_path)

    def _parallel_prefilter(
        files: list[tuple[str, BslFileInfo]],
        needle: str,
        base: str,
        max_workers: int = 12,
    ) -> list[tuple[str, BslFileInfo]]:
        """Scan all BSL files for substring in parallel using ThreadPoolExecutor.
        Bypasses sandbox read_file to avoid cache contention between threads.
        All paths come from the trusted index (built from glob inside base_path)."""
        base_p = Path(base)

        def _check(item: tuple[str, BslFileInfo]) -> tuple[str, BslFileInfo] | None:
            rel, info = item
            try:
                full = base_p / rel
                with open(full, "r", encoding="utf-8-sig", errors="replace") as f:
                    content = f.read()
                if needle in content.lower():
                    return (rel, info)
            except Exception:
                pass
            return None

        matched: list[tuple[str, BslFileInfo]] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            for result in pool.map(_check, files):
                if result is not None:
                    matched.append(result)
        return matched

    def _find_callers_context_one(
        proc_name: str,
        module_hint: str = "",
        offset: int = 0,
        limit: int = 50,
    ) -> dict:
        """Scalar core of find_callers_context (single proc_name). See find_callers_context.

        Find callers of a procedure with full context: which procedure
        in which module calls the target. Returns structured result with
        caller_name, caller_is_export, file metadata, and pagination info.

        Unlike find_callers() which is a flat grep, this helper identifies
        the exact calling procedure and filters out comments/strings.
        Uses SQLite call graph index when available (instant).

        Args:
            proc_name: Name of the target procedure/function.
            module_hint: Optional module name to determine export scope.
            offset: File offset for pagination (0-based).
            limit: Max files to scan per call (default 50).

        Returns:
            dict with "callers" list and "_meta" pagination info.
        """
        # --- v1.18.0 Фикс 3: int-guard ДО reader-вызова ---
        # _normalize_module_hint делает hint.strip(), поэтому НЕнулевой int роняет
        # AttributeError внутри ридера (`if not hint` ловит лишь 0/пусто). Политика —
        # НЕ угадывать сдвиг аргументов, а не падать и явно назвать сигнатуру.
        arg_warning: str | None = None
        if not isinstance(module_hint, str):
            arg_warning = (
                f"module_hint ожидался строкой, получено {type(module_hint).__name__}={module_hint!r} "
                "— проигнорирован. Сигнатура: find_callers_context(proc_name, module_hint, offset, limit)."
            )
            module_hint = ""

        def _tag(res: dict) -> dict:
            """Прокинуть arg_warning в _meta любого возвращаемого результата."""
            if arg_warning and isinstance(res, dict) and isinstance(res.get("_meta"), dict):
                res["_meta"]["arg_warning"] = arg_warning
            return res

        # --- Fast path: SQLite call graph ---
        if idx_reader is not None and idx_reader.has_calls:
            _t0 = _time_mod.monotonic()
            result = idx_reader.get_callers(proc_name, module_hint, offset, limit)
            _elapsed = _time_mod.monotonic() - _t0
            if result is not None:
                _n = len(result.get("callers", []))
                logger.debug(
                    "find_callers_context: proc=%s source=index rows=%d time=%.2fs",
                    proc_name,
                    _n,
                    _elapsed,
                )
                # v1.18.0 Фикс 3: offset-overshoot. returned=0, но total>0 и offset
                # за пределами — вероятно перепутаны позиционные аргументы. Возвращаем
                # индексный результат с HINT ДО authoritative/FS-fallback (которые
                # перетёрли бы _meta["hint"] или выбросили бы total_callers).
                _meta = result.get("_meta") or {}
                _total = _meta.get("total_callers")
                if _n == 0 and isinstance(_total, int) and _total > 0 and isinstance(offset, int) and offset >= _total:
                    _meta["hint"] = (
                        f"offset ({offset}) >= total_callers ({_total}): вероятно перепутаны "
                        "позиционные аргументы. Сигнатура: find_callers_context(proc_name, "
                        "module_hint, offset, limit). Повторите с offset=0."
                    )
                    result["_meta"] = _meta
                    return _tag(result)
                if _n > 0:
                    return _tag(result)
                if idx_zero_callers_authoritative:
                    logger.debug(
                        "find_callers_context: proc=%s index=0, authoritative=True, skip FS fallback",
                        proc_name,
                    )
                    result["_meta"]["fallback_skipped"] = True
                    result["_meta"]["hint"] = (
                        "No callers found in call index. Use safe_grep(proc_name) to search for text mentions."
                    )
                    return _tag(result)
                # Untrusted/stale index — fall back to FS scan
                logger.debug(
                    "find_callers_context: proc=%s index returned 0, falling back to scan",
                    proc_name,
                )
            else:
                logger.debug(
                    "find_callers_context: proc=%s source=index returned_none time=%.2fs, falling back to scan",
                    proc_name,
                    _elapsed,
                )

        _ensure_index()

        name_esc = re.escape(proc_name)
        # Patterns: direct call, qualified call (Module.Proc)
        call_patterns = [
            re.compile(r"(?<!\w)" + name_esc + r"\s*\(", re.IGNORECASE),
            re.compile(r"\." + name_esc + r"\s*\(", re.IGNORECASE),
            re.compile(r"(?<!\w)" + name_esc + r"(?!\w)", re.IGNORECASE),
        ]

        # --- Step 1: Determine scope based on export status ---
        target_files: list[str] | None = None  # None = search all

        if module_hint:
            hint_modules = find_module(module_hint)
            if hint_modules:
                # Find the target procedure in hint modules
                for hm in hint_modules:
                    try:
                        procs = extract_procedures(hm["path"])
                        for p in procs:
                            if p["name"].lower() == proc_name.lower():
                                if not p["is_export"] or hm.get("form_name") is not None:
                                    # Not exported or form module -> only search same file
                                    target_files = [hm["path"]]
                                break
                    except Exception:
                        pass
                    if target_files is not None:
                        break

        # --- Step 2: Build candidate file list ---
        if target_files is not None:
            # Scoped to specific files (non-export or form)
            candidate_files = [(rel, info) for rel, info in _index_state if rel in target_files]
        else:
            candidate_files = list(_index_state)

        # --- Step 3: Prefilter by substring (parallel scan, cached) ---
        proc_lower = proc_name.lower()

        if target_files is not None:
            # Scoped search — don't use global prefilter cache
            filtered_files: list[tuple[str, BslFileInfo]] = []
            for rel, info in candidate_files:
                try:
                    content = _ext_read_file(rel)
                    if proc_lower in content.lower():
                        filtered_files.append((rel, info))
                except Exception:
                    pass
        else:
            filtered_files = _prefilter_lazy.get_or_set(
                proc_lower,
                lambda: _parallel_prefilter(candidate_files, proc_lower, base_path),
            )

        total_files = len(filtered_files)

        # --- Step 4: Apply pagination ---
        page_files = filtered_files[offset : offset + limit]
        scanned_files = len(page_files)

        # --- Step 5: Scan each file for callers ---
        callers: list[dict] = []

        for rel, info in page_files:
            try:
                content = _ext_read_file(rel)
                lines = content.splitlines()
                procs = extract_procedures(rel)

                # Finding #3 (v1.26.0): multiline-aware очистка через _scan_module —
                # содержимое многострочных строковых литералов (тексты запросов с |)
                # больше НЕ даёт ложных callers (DRY с индексером). _scan_module
                # нумерует строки 1-based, поэтому для lines[line_idx] (0-based) ключ
                # scan_dict[line_idx + 1].
                scan_dict = {lineno: code for lineno, code, _strings in _scan_module(lines)}

                for proc in procs:
                    # Skip the definition line itself
                    body_start = proc["line"]  # 1-based, this is the def line
                    body_end = proc["end_line"] if proc["end_line"] else len(lines)

                    for line_idx in range(body_start, body_end):  # body_start is def line (skip it)
                        if line_idx >= len(lines):
                            break
                        raw_line = lines[line_idx]
                        cleaned = scan_dict.get(line_idx + 1, "")
                        if not cleaned.strip():
                            continue

                        for pattern in call_patterns:
                            if pattern.search(cleaned):
                                callers.append(
                                    {
                                        "file": rel,
                                        "caller_name": proc["name"],
                                        "caller_is_export": proc["is_export"],
                                        "line": line_idx + 1,  # 1-based
                                        "context": raw_line.rstrip(),
                                        "object_name": info.object_name,
                                        "category": info.category,
                                        "module_type": info.module_type,
                                    }
                                )
                                break  # one match per line is enough
            except Exception:
                pass

        logger.debug(
            "find_callers_context: proc=%s source=fallback callers=%d files_scanned=%d files_total=%d",
            proc_name,
            len(callers),
            scanned_files,
            total_files,
        )
        _fs_meta = {
            "total_callers": len(callers),
            "returned": len(callers),
            "offset": offset,
            "has_more": (offset + limit) < total_files,
            # FS fallback (no call index): exact (resolved) mode unavailable.
            "exact_available": False,
            "target_exact": False,
            "exact_rows": 0,
            "fallback_rows": len(callers),
        }
        # v1.18.0 Фикс 3: симметричный offset-overshoot guard по total_files.
        # Страница пуста, но файлы-кандидаты есть и offset за их пределами →
        # вероятно перепутаны позиционные аргументы, а не «нет вызовов».
        if not callers and total_files > 0 and isinstance(offset, int) and offset >= total_files:
            _fs_meta["hint"] = (
                f"offset ({offset}) >= файлов-кандидатов ({total_files}): вероятно перепутаны "
                "позиционные аргументы. Сигнатура: find_callers_context(proc_name, module_hint, "
                "offset, limit). Повторите с offset=0."
            )
        return _tag({"callers": callers, "_meta": _fs_meta})

    def find_callers_context(
        proc_name,
        module_hint: str = "",
        offset: int = 0,
        limit: int = 50,
    ) -> dict:
        """Find callers of a procedure with full context: which procedure
        in which module calls the target. Returns structured result with
        caller_name, caller_is_export, file metadata, and pagination info.

        Uses SQLite call graph index when available (instant); FS-scan fallback.

        Args:
            proc_name: target procedure/function name (``str``) ИЛИ ``list[str]``
                (P1 list-перегрузка → ``{proc_name: {callers, _meta} | {error}}``; общий
                module_hint/offset/limit применяется ко ВСЕМ именам — для разных
                модулей зови без hint или поимённо). Изоляция: имя без вызывающих
                даёт валидный пустой результат под своим ключом, а исключение на
                элементе — ``{"error": ...}`` (не роняя остальной батч); в обходе
                проверяй ``'error' in data``.
            module_hint: Optional module name to determine export scope.
            offset: File offset for pagination (0-based).
            limit: Max files to scan per call (default 50).

        Returns:
            ``str``-режим: dict with "callers" list and "_meta" pagination info.
            ``list``-режим: dict by name (значение — ``{callers, _meta}`` либо
            ``{error}`` на упавшем элементе).
        """
        return _single_or_map(
            proc_name,
            lambda name: _find_callers_context_one(name, module_hint, offset, limit),
        )

    def find_call_hierarchy(
        name: str,
        direction: str = "callers",
        depth: int = 2,
        module_hint: str = "",
        include_triggers: bool = False,
    ) -> dict:
        """Build multi-level call hierarchy. Only direction='callers'
        (uses idx_calls_callee). callees/both → structured error-dict.

        Args:
            name: Target procedure/function name.
            direction: 'callers' only.
            depth: Levels to traverse (1..3, default 2).
            include_triggers: when True, annotate each tree node with a `triggers`
                list — the NON-call inbound edges into that method (event
                subscriptions, form-event handlers, scheduled jobs, CFE overrides)
                via get_inbound_edges. Default False keeps the output byte-for-byte
                identical (the `triggers` key is added ONLY when True). Triggers are
                a leaf annotation, NOT new BFS targets — a subscription/job/form is
                an ENTRY POINT, not a caller. Each trigger:
                {edge_type, source_name, source_kind, detail, file, line,
                 caller_name, object_name, category, target_key, resolved}.
            module_hint: Optional disambiguator for the ROOT target — enables the
                exact (resolved) call-graph mode for same-named object methods
                (e.g. ОбработкаПроведения in many Documents). Forms:
                  - rel_path (the precise form, e.g. 'Documents/X/.../ObjectModule.bsl');
                  - public 'Документ.X' / 'Document.X' (RU/EN);
                  - bare object_name 'РеализацияТоваровУслуг'.
                An exported common-module method needs no hint ONLY if its name is
                globally unique across the whole DB (no-hint exact requires global
                name-uniqueness); if root_exact=False the name is ambiguous — pass
                module_hint. Deeper levels propagate each caller's rel_path
                automatically, so the exact mode continues without per-level hints.

        Returns:
            On success: {root, direction, depth, tree, visited, truncated_targets, _meta}
              where each tree node is
                {name, target_hint, target_key, meta:{exact_rows, fallback_rows,
                 exact_available, target_exact}, callers:[...]}
              and each caller is {caller_name, module_path, category, object_name,
              line, is_export, level}. top-level _meta:
                {exact_available, root_exact, exact_targets, fallback_targets,
                 exact_rows, fallback_rows, node_budget_exceeded, visited_cap}.
                node_budget_exceeded=True means a wide root hit visited_cap and the
                tree is partial (level-ordered) — pass module_hint to narrow it.
            On unsupported direction: {error, hint, supported_directions}.
        """
        if direction not in ("callers", "callees", "both"):
            return {
                "error": f"Unknown direction: {direction!r}",
                "hint": "Use direction='callers' (the only supported direction).",
                "supported_directions": ["callers"],
            }
        if direction != "callers":
            return {
                "error": f"Direction '{direction}' not supported",
                "hint": (
                    "Use direction='callers' to find callers transitively. "
                    "For callees, the alternative is: extract_procedures(path) + "
                    "safe_grep over names in the procedure body."
                ),
                "supported_directions": ["callers"],
            }
        try:
            depth_int = int(depth)
        except (TypeError, ValueError):
            depth_int = 2
        depth_int = max(1, min(3, depth_int))

        result: dict = {
            "root": name,
            "direction": "callers",
            "depth": depth_int,
            "tree": [],
            "visited": 0,
            "truncated_targets": [],
            "_meta": {
                "exact_available": False,
                "root_exact": False,
                "exact_targets": 0,
                "fallback_targets": 0,
                "exact_rows": 0,
                "fallback_rows": 0,
                # Node-budget backstop (see _HIERARCHY_VISITED_CAP). Always present
                # so the contract is stable; True only if the BFS hit the cap and
                # returned a partial (but connected, level-ordered) tree.
                "node_budget_exceeded": False,
                "visited_cap": _HIERARCHY_VISITED_CAP,
            },
        }

        # BFS with cycle protection BY TARGET (not by bare name). Queue entries
        # are (target_name, level, hint):
        #   - root hint = the user-supplied module_hint (may be '');
        #   - deeper levels propagate the caller's rel_path (most precise) so the
        #     exact mode stays engaged automatically.
        # The visited-key flavor depends on whether the target resolved exactly
        # (known only AFTER calling find_callers_context):
        #   - exact branch  → (name.casefold(), target_key=rel_path::method) so two
        #     same-named callers in different modules are NOT collapsed;
        #   - fallback branch → (name.casefold(), '') — legacy name-only behavior,
        #     no precision to preserve.
        visited: set[tuple[str, str]] = set()
        queue: list[tuple[str, int, str]] = [(name, 1, module_hint or "")]
        per_target_truncation: dict[tuple[str, str], dict] = {}
        root_seen = False

        while queue:
            # Node-budget backstop: stop BEFORE the next (expensive) lookup once
            # the cap is reached. BFS is level-ordered (append-tail / pop-head) so
            # the returned tree stays connected and shallow-first.
            if result["visited"] >= _HIERARCHY_VISITED_CAP:
                result["_meta"]["node_budget_exceeded"] = True
                break
            target_name, level, hint = queue.pop(0)
            name_cf = target_name.casefold()

            try:
                ctx = find_callers_context(target_name, module_hint=hint, offset=0, limit=200)
            except Exception:
                continue
            if not isinstance(ctx, dict):
                continue

            callers_list = ctx.get("callers", []) or []
            meta = ctx.get("_meta", {}) or {}
            target_exact = bool(meta.get("target_exact", False))
            exact_available = bool(meta.get("exact_available", False))
            target_key = meta.get("target_key") if target_exact else None

            # visited-key: exact → keep target identity; fallback → name only.
            vkey = (name_cf, target_key or "") if (target_exact and target_key) else (name_cf, "")
            if vkey in visited:
                continue
            visited.add(vkey)
            result["visited"] += 1

            if not root_seen:
                root_seen = True
                result["_meta"]["exact_available"] = exact_available
                result["_meta"]["root_exact"] = target_exact

            if meta.get("has_more"):
                per_target_truncation[vkey] = {
                    "name": target_name,
                    "level": level,
                    "total": meta.get("total_callers"),
                    "returned": meta.get("returned"),
                }

            node_callers: list[dict] = []
            for c in callers_list:
                caller_dict = {
                    "caller_name": c.get("caller_name", ""),
                    "module_path": c.get("file", ""),
                    "category": c.get("category", ""),
                    "object_name": c.get("object_name", ""),
                    "line": c.get("line", 0),
                    "is_export": bool(c.get("caller_is_export", False)),
                    "level": level,
                }
                node_callers.append(caller_dict)
                if level < depth_int:
                    next_name = c.get("caller_name", "")
                    if next_name:
                        # Propagate the caller's rel_path as the next hint — the
                        # most precise form, keeps exact mode engaged on descent.
                        queue.append((next_name, level + 1, c.get("file", "")))

            node_meta = {
                "exact_rows": meta.get("exact_rows", 0),
                "fallback_rows": meta.get("fallback_rows", len(node_callers)),
                "exact_available": exact_available,
                "target_exact": target_exact,
            }
            node = {
                "name": target_name,
                "target_hint": hint or None,
                "target_key": target_key,
                "meta": node_meta,
                "callers": node_callers,
            }
            # Opt-in leaf annotation: non-call inbound edges (subscriptions, form
            # events, scheduled jobs, CFE overrides). Reuse the SAME hint as the
            # callers query (on descent = caller's rel_path → exact mode for free).
            # Added ONLY under the flag so the default output is byte-for-byte prior;
            # under the flag the key is ALWAYS present (=[] on the FS/no-index path)
            # so the opt-in shape is reliable for consumers.
            if include_triggers:
                if idx_reader is not None:
                    try:
                        node["triggers"] = idx_reader.get_inbound_edges(target_name, module_hint=hint)
                    except Exception:
                        node["triggers"] = []
                else:
                    node["triggers"] = []
            result["tree"].append(node)

        # Top-level aggregates from per-target meta.
        for node in result["tree"]:
            nm = node["meta"]
            if nm["target_exact"]:
                result["_meta"]["exact_targets"] += 1
            else:
                result["_meta"]["fallback_targets"] += 1
            result["_meta"]["exact_rows"] += int(nm.get("exact_rows") or 0)
            result["_meta"]["fallback_rows"] += int(nm.get("fallback_rows") or 0)
        result["truncated_targets"] = list(per_target_truncation.values())

        return result

    def find_path(
        from_name: str,
        to_name: str,
        max_depth: int = 4,
        from_hint: str = "",
        to_hint: str = "",
        include_triggers: bool = False,
    ) -> dict:
        """Reachability over the CALL graph: can ``from_name`` transitively reach
        ``to_name`` through calls (``from → … → to``)?

        Implemented as an INDEXED reverse-BFS of callers starting at ``to_name``
        (forward callees would be a full scan — ``idx_calls_caller`` was dropped to
        save ~56MB). A callers chain ``to ← X ← … ← from`` IS the forward path
        ``from → … → to``; on a hit it is unrolled into forward order.

        Args:
            from_name: source method (the start of the forward path).
            to_name: target method (the end of the forward path).
            max_depth: max edges in the path (clamped 1..8, default 4).
            from_hint / to_hint: optional module disambiguators (rel_path |
                'Документ.X'/'Document.X' | bare object_name) — pin a same-named
                method to one module. ``to_hint`` enables the exact-mode root;
                ``from_hint`` makes the hit test pin ``from`` to its module.
            include_triggers: annotate each path node with its non-call inbound
                edges via get_inbound_edges (see find_call_hierarchy).

        Returns:
            {found, from, to, path:[{name, module_path, call_line, triggers?}]|None,
             depth, _meta:{max_depth, nodes_expanded, visited_cap, budget_exceeded,
             from_key, to_exact, to_key, precision:'exact'|'heuristic',
             direction:'callers-reverse'}}

            AMBIGUITY GUARD (v1.25.0): if an end name is defined in >1 module
            (NOCASE COUNT > 1 → distinct modules on normal 1С code) AND its hint
            is empty, find_path returns EARLY with the SAME keys plus
            ``{error, hint, candidates:[{object_name, category, module_type, file,
            line}]}`` and ``_meta.ambiguous=True`` / ``ambiguous_arg='to'|'from'``
            — it does NOT run the (potentially huge) reverse-BFS. So check
            ``if "error" in res`` FIRST, before interpreting ``found`` /
            ``budget_exceeded``: add the matching ``to_hint``/``from_hint`` (a
            ``file`` from ``candidates`` is the most reliable) and retry. A
            one-sided hint pins only its own end; a name not matched by the index
            NOCASE seek (incl. lowercase Cyrillic) is NOT guarded.

            ``call_line`` is EDGE metadata — the line where THIS node calls the NEXT
            (forward) node, NOT the line of the method definition; the terminal node
            (``to``) has no outgoing edge → ``call_line=None``.

            ``precision='exact'`` ⇔ ``to`` resolved exactly AND every edge of the
            path matched a stable ``callee_key`` (``edge_exact``); otherwise
            ``'heuristic'`` — on an old index (no ``callee_key``) or the FS fallback
            ``found=True`` is NAME-based reachability, not a proven resolved path.
            ``found=False`` with ``budget_exceeded=True`` means the search was
            truncated (widen scope / add a hint), NOT a proven absence — either the
            visited cap was hit OR some expanded node had more callers than one page
            (so a reaching edge may have been skipped). Only ``found=False`` with
            ``budget_exceeded=False`` AND no ``error`` key (the ambiguity guard
            above also returns ``found=False`` / ``budget_exceeded=False``, but it
            is "name ambiguous", NOT "not reachable") is a conclusive "not reachable".
        """
        try:
            max_depth_int = int(max_depth)
        except (TypeError, ValueError):
            max_depth_int = 4
        max_depth_int = max(1, min(8, max_depth_int))

        # --- Cheap ambiguity guard (v1.25.0) --------------------------------
        # A multi-defined name (NOCASE COUNT > 1 → distinct modules on normal 1С
        # code) WITHOUT a matching hint sends the reverse-BFS into a pathological
        # walk: every namesake definition fans out across the whole caller graph
        # until the node budget trips (~150s observed for
        # find_path('ОбработкаПроведения', <hot method>)). Probe the index NOCASE
        # (idx_meth_name seek ~3 ms) and bail to {error, hint, candidates} BEFORE
        # the py_lower resolve below — so an ambiguous bail never pays the
        # Cyrillic SCAN of _resolve_target_key. Each unhinted end is checked
        # independently (a one-sided hint pins only its own end). The guard runs
        # ONLY on a FRESH authoritative index WITH a call graph: on a
        # stale/no-calls index find_path falls back to FS anyway and the methods
        # probe is unreliable, so it stays out of the way.
        def _has_calls() -> bool:
            # has_calls is a SQL-backed property (SELECT 1 FROM calls LIMIT 1) —
            # evaluate it ONCE here, never per-end.
            try:
                return getattr(idx_reader, "has_calls", False) is True
            except Exception:
                return False

        _guard_on = bool(idx_zero_callers_authoritative) and idx_reader is not None and _has_calls()

        def _sample_name(name: str) -> tuple[int, list]:
            """(total, candidates), normalised + defensive. (0, []) ⇒ guard OFF
            for this end (missing/duck-typed probe, non-dict return, broken index
            → None, or simply name not multi-defined)."""
            try:
                probe = getattr(idx_reader, "sample_method_definitions", None)
            except Exception:  # a property/proxy may raise on ACCESS
                return 0, []
            if probe is None:
                return 0, []
            try:
                sample = probe(name)
            except Exception:
                return 0, []
            if not isinstance(sample, dict):  # MagicMock / duck-typed non-dict
                return 0, []
            try:
                total = int(sample.get("total") or 0)
            except (TypeError, ValueError):
                return 0, []
            cands = sample.get("candidates")
            return total, (cands if isinstance(cands, list) else [])

        def _ambiguous(arg: str, name: str, total: int, cands: list) -> dict:
            # Carry BOTH the {error, hint, candidates} contract AND the standard
            # keys, so an existing consumer reading res["found"]/_meta never
            # breaks. We bail BEFORE resolve → identities are unresolved
            # (to_key/from_key=None, to_exact=False), which is correct for an error.
            return {
                "found": False,
                "from": from_name,
                "to": to_name,
                "path": None,
                "depth": 0,
                "error": f"Имя '{name}' неоднозначно: {total} определений в разных модулях",
                "hint": (
                    "Уточни to_hint/from_hint: rel_path (надёжнее всего — file из candidates) | "
                    "'Документ.X'/'Document.X' | имя объекта/модуля."
                ),
                "candidates": [
                    {
                        "object_name": c.get("object_name"),
                        "category": c.get("category"),
                        "module_type": c.get("module_type"),
                        "file": c.get("file"),
                        "line": c.get("line"),
                    }
                    for c in cands[:5]
                    if isinstance(c, dict)
                ],
                "_meta": {
                    "ambiguous": True,
                    "ambiguous_arg": arg,
                    "definition_count": total,
                    "max_depth": max_depth_int,
                    "nodes_expanded": 0,
                    "visited_cap": _HIERARCHY_VISITED_CAP,
                    "budget_exceeded": False,
                    "from_key": None,
                    "to_exact": False,
                    "to_key": None,
                    "precision": "heuristic",
                    "direction": "callers-reverse",
                },
            }

        # Narrow self-exclusion: from==to AND no hints at all → a trivial
        # self-path (found=True below), let it through. A one-sided hint does NOT
        # suppress the guard on the OTHER (hintless) end.
        _self_trivial = (from_name.casefold() == to_name.casefold()) and not from_hint and not to_hint
        if _guard_on and not _self_trivial:
            if not to_hint:
                _t, _c = _sample_name(to_name)
                if _t > 1:
                    return _ambiguous("to", to_name, _t, _c)
            if not from_hint:
                _t, _c = _sample_name(from_name)
                if _t > 1:
                    return _ambiguous("from", from_name, _t, _c)
        # --- end ambiguity guard --------------------------------------------

        # Resolve target identities once (LOCKED public wrapper — find_path runs
        # outside the reader lock, so it must NOT touch the lockless internal).
        to_key = None
        from_key = None
        if idx_reader is not None:
            try:
                to_key = idx_reader.resolve_target_identity(to_name, to_hint or "")
            except Exception:
                to_key = None
            if from_hint:
                try:
                    from_key = idx_reader.resolve_target_identity(from_name, from_hint)
                except Exception:
                    from_key = None
        to_exact = to_key is not None
        to_module_path = to_key.rsplit("::", 1)[0] if to_key else ""

        from_cf = from_name.casefold()

        def _matches_from(caller_name: str, caller_file: str) -> bool:
            # With a resolved from_key, pin to module (disambiguates namesakes);
            # else (no hint / unresolved) fall back to name match — same recall as
            # the name-based callers branch.
            if from_key is not None:
                return _make_callee_key(caller_file, caller_name) == from_key
            return caller_name.casefold() == from_cf

        # nodes[id] = {name, module_path, call_line, edge_exact, parent_id}.
        # call_line/edge_exact describe the FORWARD edge from this node to its
        # parent (the node it calls); the start node (to) has parent_id=None.
        nodes: dict[int, dict] = {
            0: {
                "name": to_name,
                "module_path": to_module_path,
                "call_line": None,
                "edge_exact": None,
                "parent_id": None,
            }
        }
        counter = 0
        hit_id: int | None = None

        # Trivial self-path (from resolves to to).
        if _matches_from(to_name, to_module_path):
            hit_id = 0

        visited: set[tuple[str, str]] = set()
        queue: list[tuple[str, str, int, int]] = [(to_name, to_hint or "", 0, 0)]
        nodes_expanded = 0
        budget_exceeded = False
        # Set when an expanded node has MORE callers than one page (has_more) — that
        # branch is only partially walked, so a final found=False is inconclusive
        # (we may have skipped the caller that reaches `from`). Folded into
        # budget_exceeded ONLY on a miss (a hit is conclusive regardless).
        callers_truncated = False

        while queue and hit_id is None:
            if nodes_expanded >= _HIERARCHY_VISITED_CAP:
                budget_exceeded = True
                break
            cur_name, cur_hint, depth, cur_id = queue.pop(0)
            if depth >= max_depth_int:
                continue
            try:
                ctx = find_callers_context(cur_name, module_hint=cur_hint, offset=0, limit=_FIND_PATH_NODE_LIMIT)
            except Exception:
                continue
            if not isinstance(ctx, dict):
                continue
            meta = ctx.get("_meta", {}) or {}
            target_exact = bool(meta.get("target_exact", False))
            target_key = meta.get("target_key") if target_exact else None
            vkey = (cur_name.casefold(), target_key or "")
            if vkey in visited:
                continue
            visited.add(vkey)
            nodes_expanded += 1
            if meta.get("has_more"):
                callers_truncated = True

            for c in ctx.get("callers", []) or []:
                c_name = c.get("caller_name", "")
                if not c_name:
                    continue
                c_file = c.get("file", "")
                counter += 1
                cid = counter
                nodes[cid] = {
                    "name": c_name,
                    "module_path": c_file,
                    "call_line": c.get("line"),
                    "edge_exact": bool(c.get("edge_exact", False)),
                    "parent_id": cur_id,
                }
                if _matches_from(c_name, c_file):
                    hit_id = cid
                    break
                queue.append((c_name, c_file, depth + 1, cid))

        _meta = {
            "max_depth": max_depth_int,
            "nodes_expanded": nodes_expanded,
            "visited_cap": _HIERARCHY_VISITED_CAP,
            "budget_exceeded": budget_exceeded,
            "from_key": from_key,
            "to_exact": to_exact,
            "to_key": to_key,
            "precision": "heuristic",
            "direction": "callers-reverse",
        }
        if hit_id is None:
            # A miss is only conclusive if the whole reachable frontier was walked.
            # Either the visited cap (budget_exceeded) or a per-node caller-page
            # overflow (callers_truncated) means we may have skipped the edge.
            _meta["budget_exceeded"] = budget_exceeded or callers_truncated
            return {
                "found": False,
                "from": from_name,
                "to": to_name,
                "path": None,
                "depth": 0,
                "_meta": _meta,
            }

        # Reconstruct forward path [from → … → to] by walking parent_id from the
        # hit node (= from) up to the start node (= to). parent_id of a caller
        # points at the node it CALLS, so the walk already yields forward order.
        chain: list[dict] = []
        nid: int | None = hit_id
        while nid is not None:
            chain.append(nodes[nid])
            nid = nodes[nid]["parent_id"]

        all_edges_exact = True
        path: list[dict] = []
        for n in chain:
            elem: dict = {
                "name": n["name"],
                "module_path": n["module_path"],
                "call_line": n["call_line"],
            }
            if include_triggers:
                if idx_reader is not None:
                    try:
                        elem["triggers"] = idx_reader.get_inbound_edges(n["name"], module_hint=n["module_path"] or "")
                    except Exception:
                        elem["triggers"] = []
                else:
                    elem["triggers"] = []
            path.append(elem)
            if n["parent_id"] is not None and not n["edge_exact"]:
                all_edges_exact = False

        _meta["precision"] = "exact" if (to_exact and all_edges_exact) else "heuristic"
        return {
            "found": True,
            "from": from_name,
            "to": to_name,
            "path": path,
            "depth": len(path) - 1,
            "_meta": _meta,
        }

    def find_definition(name: str, module_hint: str = "", limit: int = 50) -> dict:
        """Where a method is defined — forward complement of find_callers_context.

        Lists every module that defines a procedure/function ``name`` (case-
        insensitive), optionally narrowed by ``module_hint`` (the same three forms
        find_callers_context accepts: rel_path, ``Документ.X``/``Document.X``, or a
        bare object name). Same-named methods across many objects
        (``ОбработкаПроведения`` in every document) are the norm in 1С, so all
        candidates are returned (capped by ``limit``) — narrow with ``module_hint``
        to pin a single one.

        Returns:
            {
              "name": <queried name>,
              "definitions": [{file, line, end_line, type, is_export,
                               params (list[str]), category, object_name,
                               module_type}],
              "total": int, "truncated": bool,
              "_meta": {"index_used", "unique", "hint_applied", "slow_fallback"}
            }
            Empty result → ``definitions: [], total: 0`` (NOT an error). A blank
            ``name`` → ``{"error", "hint"}`` (git_search style). ``hint_applied``
            means "a module_hint filter WAS applied to the query" (deterministic),
            not "the hint changed the row count". Without an index, a hint giving a
            module/object is required (live via extract_procedures); no hint →
            ``{"error": "no index", ...}``.
        """
        if not name or not name.strip():
            return {
                "error": "empty name",
                "hint": "задайте имя метода (без скобок); для поиска по тексту — git_search / safe_grep.",
            }
        name = name.strip()

        from rlm_tools_bsl.bsl_index import _normalize_module_hint

        rel_hint, category, object_name = _normalize_module_hint(module_hint)
        # hint_applied = "фильтр по hint применён к запросу" (детерминировано),
        # а НЕ "hint изменил число результатов" (последнее без доп. запроса не узнать).
        hint_applied = bool(rel_hint or category or object_name)

        def _live_row(proc: dict, file_path: str, mod: dict | None) -> dict:
            # rel_path branch passes mod=None → derive identity structurally from
            # the path so category/object_name/module_type are filled (parity with
            # the index path and the object-name fallback, which carry a mod dict).
            meta = mod if mod is not None else _module_meta_from_path(file_path, base_path)
            return {
                "file": file_path,
                "line": proc.get("line"),
                "end_line": proc.get("end_line"),
                "type": proc.get("type"),
                "is_export": bool(proc.get("is_export")),
                "params": proc.get("params") if isinstance(proc.get("params"), list) else [],
                "category": meta.get("category"),
                "object_name": meta.get("object_name"),
                "module_type": meta.get("module_type"),
            }

        # --- Index path ---
        if idx_reader is not None:
            res = idx_reader.get_definitions(name, module_hint, limit)
            if res is not None:
                # res is a valid result (possibly empty) — NOT a broken index.
                _normalize_method_params(res["rows"])  # str params -> list[str] in place
                definitions = [
                    {
                        "file": r["rel_path"],
                        "line": r["line"],
                        "end_line": r["end_line"],
                        "type": r["type"],
                        "is_export": r["is_export"],
                        "params": r["params"],
                        "category": r["category"],
                        "object_name": r["object_name"],
                        "module_type": r["module_type"],
                    }
                    for r in res["rows"]
                ]
                total = res["total"]
                return {
                    "name": name,
                    "definitions": definitions,
                    "total": total,
                    "truncated": res["truncated"],
                    "_meta": {
                        "index_used": True,
                        "unique": total == 1,
                        "hint_applied": hint_applied,
                        "slow_fallback": res["slow_fallback"],
                    },
                }
            # res is None → corrupt/missing core tables → fall through to live.

        # --- Live fallback (no index, or broken core index) ---
        # Без индекса нет глобального списка методов: live-поиск возможен только
        # при подсказке, дающей конкретный модуль (rel_path) или объект.
        if rel_hint is not None:
            try:
                procs = extract_procedures(rel_hint)
            except Exception:
                procs = []
            definitions = [_live_row(p, rel_hint, None) for p in procs if p["name"].casefold() == name.casefold()]
        elif object_name is not None:
            definitions = []
            for mod in find_module(object_name):
                if category is not None and (mod.get("category") or "").casefold() != category.casefold():
                    continue
                mpath = mod["path"]
                try:
                    procs = extract_procedures(mpath)
                except Exception:
                    continue
                definitions.extend(_live_row(p, mpath, mod) for p in procs if p["name"].casefold() == name.casefold())
        else:
            return {
                "error": "no index",
                "hint": "уточните module_hint (Документ.X / rel_path / имя объекта) или соберите индекс (rlm_index build).",
            }

        return {
            "name": name,
            "definitions": definitions,
            "total": len(definitions),
            "truncated": False,
            "_meta": {
                "index_used": False,
                "unique": len(definitions) == 1,
                "hint_applied": hint_applied,
                "slow_fallback": False,
            },
        }

    def get_module_outline(path: str, include_methods: bool = True, no_live: bool = False) -> dict:
        """Cheap structural 'skeleton' of a module — the ``#Область`` tree plus
        aggregates — as a first hop before reading bodies.

        Where ``extract_procedures`` returns a flat method list, this shows the
        region hierarchy (which method lives in which ``#Область``, nested), and
        per-region/per-module aggregates ({methods, exports, regions, loc}) so the
        agent can decide where to drill without reading a 5–15K-line module.

        Args:
            path: rel_path of the module (e.g. from ``find_module(...)[i]['path']``)
                ИЛИ имя объекта (P3): модуль выбирается единым правилом
                ``(category, module_type)`` (см. ``_resolve_module_arg``) с ПРОЗРАЧНЫМ
                авто-выбором — resolver-ключи домержены в ``_meta`` (см. ниже), при
                неоднозначности ``_meta.ambiguous=True`` (выбор всё равно детерминирован,
                ошибки нет — в отличие от ``extract_procedures``, который кидает ValueError).
            include_methods: ``True`` (default) → include leaf methods + orphans;
                ``False`` → region tree + totals only (even cheaper top-level map).
            no_live: ``False`` (default) → on a no/stale-index module fall back to a
                live parse (reads the file). ``True`` → NEVER read the file: the would-be
                live branches return a skipped marker instead (empty outline,
                ``_meta.skipped_live=True`` + ``fallback_reason``). Used by compact
                ``get_object_profile``/``get_object_modules`` so the modules section never
                triggers a live read on a stale index (R12/R13 — checking
                ``index_used`` after the fact is too late, the file is already read).

        Returns:
            {
              "path", "category", "object_name", "module_type",
              "totals": {"methods", "exports", "regions", "loc"},
              "outline": [{region, line, end_line, totals:{methods, exports},
                           children:[...], methods:[...]}],   # methods iff include_methods
              "orphan_methods": [...],                        # present iff include_methods
              "_meta": {"index_used": bool, "fallback_reason": str | None,
                        "resolved_from_name": bool,           # P3: всегда (False на path-пути)
                        "chosen_module", "chosen_reason", "candidates", "ambiguous"}  # iff by name
            }

        ``_meta`` mirrors ``get_object_full_structure``: ``index_used=True`` → tree
        built from the index; otherwise ``fallback_reason`` is one of
        ``'index_unavailable_or_table_missing'`` (no/old index, missing regions
        table, or module not indexed), ``'index_empty_for_module'`` (module row
        present but no methods — live safety net), or ``'parse_failed: …'``. P3
        resolver-ключи МЕРЖАТСЯ в ``_meta`` (не затирают ``index_used``/``fallback_reason``).
        """

        # P3: принять имя ИЛИ путь. meta резолва (resolved_from_name всегда + ключи
        # авто-выбора при name-режиме) домержится в _meta каждого возвращаемого resp.
        path, _arg_meta = _resolve_module_arg(path)

        def _finish(resp: dict) -> dict:
            resp["_meta"].update(_arg_meta)
            return resp

        def _resolve_meta_live() -> dict:
            # Best-effort module identity. Prefer the live file index when it is
            # already populated (it matches the index path); otherwise derive the
            # identity structurally from the path so a DIRECT call (no prior
            # find_module → ``_index_state`` still empty, since the live path does
            # not call ``_ensure_index``) still fills category/object_name/module_type.
            for rp, info in _index_state:
                if rp == path:
                    return {
                        "category": info.category,
                        "object_name": info.object_name,
                        "module_type": info.module_type,
                    }
            return _module_meta_from_path(path, base_path)

        def _assemble(regions: list, methods: list, meta: dict, index_used: bool, fallback_reason) -> dict:
            outline, orphans = _build_outline_tree(regions, methods, include_methods)
            totals = {
                "methods": len(methods),
                "exports": sum(1 for m in methods if m.get("is_export")),
                "regions": len(regions),
                "loc": sum((m.get("loc") or 0) for m in methods),
            }
            resp = {
                "path": path,
                "category": meta.get("category"),
                "object_name": meta.get("object_name"),
                "module_type": meta.get("module_type"),
                "totals": totals,
                "outline": outline,
                "_meta": {"index_used": index_used, "fallback_reason": fallback_reason},
            }
            if include_methods:
                resp["orphan_methods"] = orphans
            return resp

        def _live(reason: str) -> dict:
            # extract_procedures is self-healing; _parse_regions over raw lines.
            from rlm_tools_bsl.bsl_index import _parse_regions

            try:
                procs = extract_procedures(path)
                regions = _parse_regions(_ext_read_file(path).splitlines())
            except Exception as exc:
                resp = {
                    "path": path,
                    "category": None,
                    "object_name": None,
                    "module_type": None,
                    "totals": {"methods": 0, "exports": 0, "regions": 0, "loc": 0},
                    "outline": [],
                    "_meta": {"index_used": False, "fallback_reason": f"parse_failed: {exc}"},
                }
                if include_methods:
                    resp["orphan_methods"] = []
                return resp
            # Live methods carry no stored loc — approximate from the line span.
            meths = []
            for p in procs:
                ln, el = p.get("line"), p.get("end_line")
                loc = (el - ln + 1) if isinstance(ln, int) and isinstance(el, int) and el >= ln else None
                meths.append(
                    {
                        "name": p.get("name"),
                        "type": p.get("type"),
                        "is_export": bool(p.get("is_export")),
                        "line": ln,
                        "end_line": el,
                        "loc": loc,
                    }
                )
            return _assemble(regions, meths, _resolve_meta_live(), False, reason)

        def _no_live(reason: str) -> dict:
            # compact path: would-be live branch returns a skipped marker WITHOUT
            # reading the file. Identity is filled structurally (live file-index lookup
            # or pure path parse — both read no module body). R12/R13: prevent the live
            # read up-front, not by inspecting index_used after the body is already read.
            resp = {
                "path": path,
                **{k: _resolve_meta_live().get(k) for k in ("category", "object_name", "module_type")},
                "totals": {"methods": 0, "exports": 0, "regions": 0, "loc": 0},
                "outline": [],
                "_meta": {"index_used": False, "fallback_reason": reason, "skipped_live": True},
            }
            if include_methods:
                resp["orphan_methods"] = []
            return resp

        def _fallback(reason: str) -> dict:
            return _no_live(reason) if no_live else _live(reason)

        # --- Routing (codex round-3: explicit branches, no silently-empty outline) ---
        data = idx_reader.get_outline_data(path) if idx_reader is not None else None
        if data is None:
            # idx_reader is None, OR regions table missing / corrupt core → live / skip.
            return _finish(_fallback("index_unavailable_or_table_missing"))
        if data["module"] is None:
            # Module not in the index (valid 'not indexed') → live / skip.
            return _finish(_fallback("index_unavailable_or_table_missing"))
        if not data["methods"]:
            # Module row present but no methods (stale) → live safety net / skip.
            return _finish(_fallback("index_empty_for_module"))
        # Index path.
        return _finish(_assemble(data["regions"], data["methods"], data["module"], True, None))

    # XML file names by metadata category (CF format: Ext/<name>.xml)
    _CATEGORY_XML_NAMES = {
        "documents": "Document",
        "catalogs": "Catalog",
        "informationregisters": "RecordSet",
        "accumulationregisters": "RecordSet",
        "accountingregisters": "RecordSet",
        "calculationregisters": "RecordSet",
        "reports": "Report",
        "dataprocessors": "DataProcessor",
        "exchangeplans": "ExchangePlan",
        "chartsofaccounts": "ChartOfAccounts",
        "chartsofcharacteristictypes": "ChartOfCharacteristicTypes",
        "chartsofcalculationtypes": "ChartOfCalculationTypes",
        "businessprocesses": "BusinessProcess",
        "tasks": "Task",
        "constants": "Constant",
    }

    def _xml_candidates_named(object_name: str) -> list[str]:
        """Fast-path XML/MDO candidates: structural patterns + ext-metadata
        entries. No filesystem globs — keeps bulk ext scans (e.g.
        ``_live_attributes_in_extensions`` on 100+ ext objects) cheap.
        """
        parts = object_name.split("/")
        category = parts[0].lower() if parts else ""
        xml_name = _CATEGORY_XML_NAMES.get(category)
        last_segment = parts[-1] if parts else ""

        out: list[str] = []
        # v1.18.0 Фикс 4b: порядок директорных кандидатов под формат дампа
        # (CF -> Ext/*.xml сначала, EDT -> *.mdo сначала). ПЕРЕУПОРЯДОЧИВАЕМ, НЕ
        # сокращаем — все кандидаты пробуются (смешанные CF+EDT расширения корректны).
        edt_cand = f"{object_name}/{last_segment}.mdo" if last_segment else None
        cf_cand = f"{object_name}/Ext/{xml_name}.xml" if xml_name else None
        if _dump_format == "cf":
            ordered = [cf_cand, edt_cand]
        else:
            # EDT и UNKNOWN сохраняют прежний порядок (EDT-кандидат первым).
            ordered = [edt_cand, cf_cand]
        out.extend(c for c in ordered if c)
        out.append(f"{object_name}.xml")
        out.append(f"{object_name}.mdo")

        # Extension candidates from the metadata-XML pass — picks up XML-only
        # objects (Subsystems, EventSubscriptions) without a .bsl module.
        if _extension_metadata_xml and category and last_segment:
            target_cat = category.lower()
            target_name = last_segment.lower()
            for cat, obj_name, rel in _extension_metadata_xml:
                if cat.lower() == target_cat and obj_name.lower() == target_name:
                    out.append(rel)
        return out

    def _xml_candidates_glob_fallback(object_name: str) -> list[str]:
        """Slow-path glob fallback for non-standard layouts.

        Invoked ONLY when every named candidate from ``_xml_candidates_named``
        missed. Prior to this split the glob was unconditional, which on
        configs with many extensions (e.g. 197 ext objects on a 24K-BSL ERP)
        triggered 2× ``glob_files_fn`` calls per ext object inside
        ``_live_attributes_in_extensions`` — a runaway FS scan that could
        stall the session for tens of minutes. Now the glob fires only when
        a non-standard layout actually requires it.
        """
        out: list[str] = []
        try:
            ext_match = glob_files_fn(f"{object_name}/Ext/*.xml")
        except Exception:
            ext_match = []
        if ext_match:
            out.append(ext_match[0])
        try:
            mdo_match = glob_files_fn(f"{object_name}/*.mdo")
        except Exception:
            mdo_match = []
        if mdo_match:
            out.append(mdo_match[0])
        return out

    def _xml_candidates(object_name: str) -> list[str]:
        """Backwards-compatible wrapper combining named + glob fallback.
        Kept for any external callers; ``_resolve_object_xml`` now uses the
        two-tier helpers directly to avoid eager glob.
        """
        return _xml_candidates_named(object_name) + _xml_candidates_glob_fallback(object_name)

    def _resolve_object_xml(path: str) -> str:
        """Resolve path to the actual XML file.

        Accepts:
          - Direct path: 'Documents/Name/Ext/Document.xml' → as-is if exists
          - Directory path: 'Documents/Name' → tries Ext/<Type>.xml, then .xml, then .mdo
          - "Fake" file path: 'Documents/Name.mdo' / 'Documents/Name.xml'
            (no actual file at that exact location) → normalize base by stripping
            the extension and try the same candidate set as for a directory.

        Raises FileNotFoundError with an explicit hint when nothing resolves.
        """
        _ensure_index()  # ensure _extension_metadata_xml is populated

        normalized = path.replace("\\", "/")
        path_lower = normalized.lower()
        ends_with_xml = path_lower.endswith(".xml")
        ends_with_mdo = path_lower.endswith(".mdo")

        if ends_with_xml or ends_with_mdo:
            try:
                if _ext_resolve_safe(normalized).exists():
                    return normalized
            except Exception:
                pass
            # Fake .xml/.mdo path: normalize base (strip extension) and rebuild candidates.
            base = normalized[:-4]
        else:
            base = normalized

        if not base:
            raise FileNotFoundError(f"Path not found: {path!r}")

        parts = base.split("/")
        xml_name = _CATEGORY_XML_NAMES.get(parts[0].lower() if parts else "")
        last_segment = parts[-1] if parts else ""

        any_resolvable = False

        # Try named candidates first (no glob — fast path).
        for candidate in _xml_candidates_named(base):
            try:
                resolved = _ext_resolve_safe(candidate)
            except PermissionError:
                continue
            except Exception:
                continue
            any_resolvable = True
            try:
                if resolved.exists():
                    return candidate
            except OSError:
                continue

        # Slow path: glob fallback only when nothing named resolved. Critical
        # for bulk ext scans — see _xml_candidates_glob_fallback docstring.
        for candidate in _xml_candidates_glob_fallback(base):
            try:
                resolved = _ext_resolve_safe(candidate)
            except PermissionError:
                continue
            except Exception:
                continue
            any_resolvable = True
            try:
                if resolved.exists():
                    return candidate
            except OSError:
                continue

        if not any_resolvable:
            raise PermissionError(f"Access denied: path {path!r} escapes sandbox and extension roots")

        if ends_with_xml or ends_with_mdo:
            # v1.18.0 Фикс 4b: ведём подсказку форматом дампа (CF -> Ext/*.xml первым).
            edt_hint = f"'{base}/{last_segment}.mdo' (EDT)"
            cf_hint = f"'{base}/Ext/{xml_name or '<Type>'}.xml' (CF)"
            fmt_hints = f"{cf_hint} / {edt_hint}" if _dump_format == "cf" else f"{edt_hint} / {cf_hint}"
            raise FileNotFoundError(
                f"Path not found: {path!r}. "
                f"Возможно вы передали '{path}' (фейковый файл). "
                f"Попробуйте '{base}' (директория) или {fmt_hints}."
            )
        raise FileNotFoundError(
            f"Path not found: {path!r}. Use find_module('{last_segment}') to discover the correct path."
        )

    def parse_object_xml(path: str) -> dict:
        """Read a 1C metadata XML file and extract its structure:
        name, synonym, attributes, tabular sections, dimensions, resources,
        subsystem content. Works with any metadata XML (catalogs, documents,
        registers, subsystems, etc.).

        Accepts both direct XML paths and directory paths:
          parse_object_xml('Documents/Name/Ext/Document.xml')  — direct
          parse_object_xml('Documents/Name')                    — auto-resolves

        Returns: dict with keys like name, synonym, attributes, tabular_sections,
        dimensions, resources (depends on metadata type)."""
        resolved = _resolve_object_xml(path)
        content = _ext_read_file(resolved)
        parsed = parse_metadata_xml(content)
        # Finding #5 (v1.26.0): parse_metadata_xml теперь возвращает None на битом
        # XML (раньше бросал ET.ParseError). Agent-facing контракт parse_object_xml
        # — исключение на битом XML (его ловят analyze_object/find_custom_modifications
        # через except Exception). Воспроизводим факт «raise», тип меняется на ValueError.
        if parsed is None:
            raise ValueError(f"malformed metadata XML: {resolved}")
        # v1.18.0 Фикс 1: атрибутные записи толерантны к диалекту ключей
        # (name <-> attr_name). Оборачиваем ТОЛЬКО вложенные записи, не сам
        # верхний dict (его internal-консьюмеры проверяют isinstance(..., dict)).
        if isinstance(parsed, dict):
            for _section in ("attributes", "dimensions", "resources"):
                if isinstance(parsed.get(_section), list):
                    parsed[_section] = [_AttrRecord(a) if isinstance(a, dict) else a for a in parsed[_section]]
            for _ts in parsed.get("tabular_sections", []) or []:
                if isinstance(_ts, dict) and isinstance(_ts.get("attributes"), list):
                    _ts["attributes"] = [_AttrRecord(a) if isinstance(a, dict) else a for a in _ts["attributes"]]
        return parsed

    # ── Composite helpers (wrappers over existing functions) ────────

    def analyze_subsystem(name: str) -> dict:
        """Find a subsystem by name, parse its XML composition,
        classify objects as custom (non-standard prefix) or standard.

        Returns: dict with subsystems_found, subsystems list."""
        name = _strip_meta_prefix(name)

        # --- Fast path: SQLite index ---
        if idx_reader is not None:
            matches = idx_reader.get_subsystems_for_object(name)
            if matches is not None:
                # matches is [] or list of dicts
                results = []
                for m in matches:
                    results.append(
                        {
                            "file": m["file"],
                            "name": m["name"],
                            "synonym": m["synonym"],
                            "total_objects": len(m["matched_refs"]),
                            "matched_refs": m["matched_refs"],
                        }
                    )
                if not results:
                    return {
                        "error": f"Подсистема с '{name}' не найдена",
                        "hint": "Объект не входит ни в одну подсистему",
                    }
                return {"subsystems_found": len(results), "subsystems": results}

        # --- Fallback: glob + XML parse ---
        patterns = [
            f"**/Subsystems/**/*{name}*",
            f"**/Subsystems/*{name}*",
            # REMOVED: f"**/*{name}*.mdo" — scans entire tree, useless for subsystems
        ]
        found_files: list[str] = []
        for p in patterns:
            found_files.extend(glob_files_fn(p))

        subsystem_files = list(
            dict.fromkeys(f for f in found_files if "Subsystem" in f and (f.endswith(".xml") or f.endswith(".mdo")))
        )

        if not subsystem_files:
            return {
                "error": f"Подсистема '{name}' не найдена",
                "hint": "Попробуйте glob_files('**/Subsystems/**') для просмотра всех подсистем",
            }

        results = []
        for sf in subsystem_files:
            try:
                meta = parse_object_xml(sf)
            except Exception:
                continue
            if not meta or meta.get("object_type") != "Subsystem":
                continue

            content = meta.get("content", [])
            custom_objects = []
            standard_objects = []
            for item in content:
                parts = item.split(".", 1)
                obj_type = parts[0] if parts else ""
                obj_name = parts[1] if len(parts) > 1 else item
                is_custom = bool(obj_name) and obj_name[0].islower()
                entry = {"type": obj_type, "name": obj_name, "is_custom": is_custom}
                if is_custom:
                    custom_objects.append(entry)
                else:
                    standard_objects.append(entry)

            results.append(
                {
                    "file": sf,
                    "name": meta.get("name", ""),
                    "synonym": meta.get("synonym", ""),
                    "total_objects": len(content),
                    "custom_objects": custom_objects,
                    "standard_objects": standard_objects,
                    "raw_content": content,
                }
            )

        return {"subsystems_found": len(results), "subsystems": results}

    def find_custom_modifications(
        object_name: str,
        custom_prefixes: list[str] | None = None,
    ) -> dict:
        """Find all non-standard (custom) modifications in an object's modules:
        procedures with custom prefix, custom #Область regions, custom XML attributes.
        If custom_prefixes is not provided, uses auto-detected prefixes from the codebase.

        Returns: dict with modifications list and custom_attributes."""
        object_name = _strip_meta_prefix(object_name)
        prefix_source = "user" if custom_prefixes else "auto"
        prefixes = custom_prefixes or _ensure_prefixes()
        if not prefixes:
            return {"error": "Нетиповые префиксы не обнаружены. Укажите custom_prefixes вручную."}

        modules = find_module(object_name)
        exact = [m for m in modules if (m.get("object_name") or "").lower() == object_name.lower()]
        if not exact:
            exact = modules
        if not exact:
            return {"error": f"Объект '{object_name}' не найден"}

        def _match_prefix(s: str) -> bool:
            sl = s.lower()
            return any(sl.startswith(p.lower()) for p in prefixes)

        modifications = []
        for mod in exact:
            path = mod["path"]
            try:
                procs = extract_procedures(path)
            except Exception:
                continue

            custom_procs = [p for p in procs if _match_prefix(p["name"])]

            custom_regions: list[dict] = []
            try:
                content = _ext_read_file(path)
                for i, line in enumerate(content.splitlines(), 1):
                    stripped = line.strip()
                    if stripped.startswith("#") and "Область" in stripped:
                        region_name = stripped.split("Область", 1)[1].strip()
                        if _match_prefix(region_name):
                            custom_regions.append({"name": region_name, "line": i})
            except Exception:
                pass

            if custom_procs or custom_regions:
                modifications.append(
                    {
                        "path": path,
                        "module_type": mod.get("module_type", ""),
                        "form_name": mod.get("form_name"),
                        "total_procedures": len(procs),
                        "custom_procedures": custom_procs,
                        "custom_regions": custom_regions,
                    }
                )

        custom_attributes: list[dict] = []
        parse_error: str | None = None
        category = exact[0].get("category", "")
        obj_name = exact[0].get("object_name", "")
        if category and obj_name:
            try:
                meta = parse_object_xml(f"{category}/{obj_name}")
                for attr in meta.get("attributes", []):
                    if _match_prefix(attr["name"]):
                        custom_attributes.append(attr)
                for ts in meta.get("tabular_sections", []):
                    if _match_prefix(ts["name"]):
                        custom_attributes.append(
                            {
                                "name": ts["name"],
                                "type": "TabularSection",
                                "synonym": ts.get("synonym", ""),
                            }
                        )
            except Exception as exc:
                parse_error = f"{type(exc).__name__}: {exc}"

        result = {
            "object_name": object_name,
            "prefixes_used": prefixes,
            "prefix_source": prefix_source,
            "modules_analyzed": len(exact),
            "modifications": modifications,
            "custom_attributes": custom_attributes,
        }
        if parse_error:
            result["parse_error"] = parse_error
        return result

    # ── Categories whose objects are pure metadata (no BSL module by default) ──
    # Used by _resolve_object_for_full_structure live-fallback to find XML-only
    # objects (Enums, FunctionalOptions, EventSubscriptions, etc.) when the index
    # is unavailable.
    _METADATA_ONLY_CATEGORIES = (
        "Enums",
        "Constants",
        "FunctionalOptions",
        "EventSubscriptions",
        "ScheduledJobs",
        "DefinedTypes",
        "ExchangePlans",
        "Subsystems",
        "Roles",
        "ChartsOfCharacteristicTypes",
        "ChartsOfAccounts",
        "ChartsOfCalculationTypes",
        # Categories that usually have modules but can also be XML-only:
        "Catalogs",
        "Documents",
        "InformationRegisters",
        "AccumulationRegisters",
        "AccountingRegisters",
        "CalculationRegisters",
        "BusinessProcesses",
        "Tasks",
        "Reports",
        "DataProcessors",
    )

    def _resolve_object_for_full_structure(
        name: str, prefer_category: str | None = None
    ) -> tuple[str | None, str | None]:
        """Return (category, object_name) for a metadata object via a strict cascade.

        ``prefer_category`` (plural folder, e.g. ``'Documents'``) — when set, EVERY exact
        match is additionally gated on that category and the Pass-3 close-match fallback is
        skipped → on a cross-category homonym (``Document.X`` vs ``Catalog.X``) the explicitly
        requested category wins; returns ``(None, None)`` if the object is not in that category
        (caller may then retry without the filter). Default ``None`` keeps behaviour byte-for-byte.

        Pass 1 — exact-match через все источники по очереди (любой непустой
                 источник НЕ блокирует следующий, если в нём нет точного
                 совпадения):
            1. object_attributes (большинство объектов с реквизитами/ТЧ)
            2. object_synonyms via search_objects (synonym-only объекты)
            3. enum_values (Enums без записей в object_attributes)
            4. find_module (объекты с BSL-модулями)

        Pass 2 — live glob по известным метаданным категориям (всегда exact:
                 имя файла = name).

        Pass 3 — close-match fallback: если ВСЕ источники Pass 1 прошли без
                 exact-совпадения, вернуться к ним по тому же порядку и взять
                 первый non-empty. Воспроизводит старое поведение для случаев,
                 когда indexer положил объект под другим именем.

        Returns (None, None) если ничего не нашлось.

        v1.10.0 BUG-4 fix: ранее close-match из первого непустого источника
        (object_attributes c LIKE '%name%') блокировал exact-match в других
        источниках — БизнесПроцесс «Согласование» терялся за регистром-
        омонимом «тст_СогласованиеЗаявокСБ».
        """
        name_lower = name.lower()
        pc = prefer_category.lower() if prefer_category else None

        rows = None
        so_rows = None
        ev = None

        # ── Pass 1: exact-match через все источники ──────────────────────
        if idx_reader is not None:
            # 1. object_attributes — большинство объектов с реквизитами/ТЧ
            try:
                rows = idx_reader.get_object_attributes(object_name=name, limit=50)
            except Exception:
                rows = None
            for r in rows or []:
                if (r.get("object_name") or "").lower() == name_lower and (
                    pc is None or (r.get("category") or "").lower() == pc
                ):
                    return r.get("category"), r.get("object_name")

            # 2. object_synonyms — synonym-only объекты (Enum/Constant/FO)
            try:
                so_rows = idx_reader.search_objects(name, limit=20)
            except Exception:
                so_rows = None
            for s in so_rows or []:
                if (s.get("object_name") or "").lower() == name_lower and (
                    pc is None or (s.get("category") or "").lower() == pc
                ):
                    return s.get("category"), s.get("object_name")

            # 3. enum_values — Enum, у которого нет записей в object_synonyms
            try:
                ev = idx_reader.get_enum_values(name)
            except Exception:
                ev = None
            if (
                ev
                and not ev.get("error")
                and ev.get("name")
                and ev["name"].lower() == name_lower
                and (pc is None or pc == "enums")
            ):
                return "Enums", ev["name"]

        # 4. find_module — объекты с BSL-модулями (exact-проход). При prefer_category
        # фильтр категории отдаём ВНУТРЬ find_module (он применяется ДО cap-50), иначе на
        # частом имени-подстроке целевая категория могла бы выпасть за кап и явный префикс
        # ложно не нашёл бы module-only объект.
        modules = find_module(name, category=prefer_category or "")
        exact_modules = [
            m
            for m in modules
            if (m.get("object_name") or "").lower() == name_lower
            and (pc is None or (m.get("category") or "").lower() == pc)
        ]
        if exact_modules:
            return exact_modules[0].get("category"), exact_modules[0].get("object_name")

        # ── Pass 2: live glob по категориям (всегда exact, имя файла = name) ─
        for cat in _METADATA_ONLY_CATEGORIES:
            if pc is not None and cat.lower() != pc:
                continue
            # Try CF directory layout: {Cat}/{name}/Ext/*.xml
            try:
                hits = glob_files_fn(f"{cat}/{name}/Ext/*.xml")
            except Exception:
                hits = []
            if hits:
                return cat, name
            # Try EDT layout: {Cat}/{name}/{name}.mdo
            try:
                hits = glob_files_fn(f"{cat}/{name}/{name}.mdo")
            except Exception:
                hits = []
            if hits:
                return cat, name
            # Try CF sibling-only layout: {Cat}/{name}.xml
            try:
                hits = glob_files_fn(f"{cat}/{name}.xml")
            except Exception:
                hits = []
            if hits:
                return cat, name

        # ── Pass 3: close-match fallback ────────────────────────────────
        # prefer_category is STRICT: if no exact match exists in the requested category
        # (Pass 1/2), do NOT fall back to a close-match of another category — return
        # (None, None) so the caller can retry unfiltered. Otherwise a "Документ.Заказ"
        # request with only a Catalog.Заказ present would wrongly return Catalogs.
        if pc is not None:
            return None, None
        # Все источники Pass 1 не дали exact — берём первый non-empty в
        # исходном порядке. Сохраняет прежнее поведение «get_enum_values
        # как close-match» (агент пишет 'Статус', в индексе Enum
        # 'СтатусыЗаказов' — substring-based get_enum_values его находит).
        if rows:
            first = rows[0]
            return first.get("category"), first.get("object_name")
        if so_rows:
            first = so_rows[0]
            return first.get("category"), first.get("object_name")
        if ev and not ev.get("error") and ev.get("name"):
            return "Enums", ev["name"]
        if modules:
            return modules[0].get("category"), modules[0].get("object_name")

        return None, None

    def get_object_full_structure(name: str, category_hint: str | None = None) -> dict:
        """Aggregating helper: full object structure in one call.

        Combines metadata from object_attributes / predefined_items / object_synonyms /
        enum_values / form_elements (when index exists), with live XML fallback.
        Replaces the typical chain: parse_object_xml + find_attributes + find_predefined +
        find_enum_values per EnumRef.X.

        _meta semantics:
          index_used=True  означает «возвращённые в результате СТРУКТУРНЫЕ
                            секции (attributes, dimensions, resources,
                            tabular_sections, predefined_items) взяты из
                            индекса». Это контракт об ИСТОЧНИКЕ возвращаемых
                            данных, а НЕ об их полноте: если индекс stale
                            (например, часть TS не успела попасть в
                            object_attributes, но есть в XML), результат
                            вернёт только то, что есть в индексе, без чтения
                            live XML. Это сознательный performance-tradeoff —
                            хелпер не делает второй парсинг XML «ради
                            проверки полноты». Если агенту нужна гарантия
                            полноты — пусть дополнительно вызывает
                            parse_object_xml() или проверяет свежесть индекса
                            через get_index_info(). synonym/forms могут быть
                            подтянуты из live (они вспомогательные); synonym
                            самой ТЧ может быть обогащён из live по name
                            (это enrichment, а не замена структурных данных).
          index_used=False означает «хотя бы часть структуры пришла из live
                            XML». Причина — в fallback_reason:
            'index_unavailable_or_table_missing'        — индекса нет / таблицы нет.
            'index_empty_for_object'                    — индекс есть, но
                                                         object_attributes пустой
                                                         для нормальной категории
                                                         (stale/incomplete index).
            'category_without_attributes_filled_via_live_xml' — индекс есть, но
                                                         категория (Enum/Constant/...)
                                                         по природе не имеет attrs;
                                                         структура взята live.
            'index_partially_enriched_from_live_xml'    — индекс дал часть структуры,
                                                         live XML был вызван
                                                         (для synonym/forms/ts-synonym
                                                         enrichment) и ЗАОДНО
                                                         дозаполнил недостающие
                                                         структурные секции.
                                                         Источник смешанный. ВАЖНО:
                                                         этот reason возникает только
                                                         когда live в принципе
                                                         вызывался; если индекс дал
                                                         synonym+forms+attributes без
                                                         нужды в live, скрытые в XML
                                                         TS могут остаться
                                                         незамеченными — это всё ещё
                                                         index_used=True (см. выше).
            'parse_failed: ...'                         — live XML тоже не смог.

          ts_synonyms_available=True ставится ТОЛЬКО когда хотя бы у одной TS
          в результате есть непустой synonym (не просто факт «мы парсили live»).

        posting для документов:
          posting в индексе v12 не хранится. На чистом index path
          (index_used=True без enrichment) posting остаётся None — это согласовано
          с контрактом «без чтения live XML». Если live был вызван по другим
          причинам (synonym/forms/ts enrichment, fallback) — posting подхватывается
          из того же XML-чтения. Если posting нужен гарантированно, агенту
          следует использовать find_register_movements(doc_name): при пустом
          результате он сам делает live posting check.

        Returns dict:
          {object_name, category, synonym, posting,
           attributes, tabular_sections:[{name, synonym, columns}],
           dimensions, resources, predefined_items,
           enum_values_for_typed_refs:{Enum.X:[...]},
           forms:[str],
           _meta:{index_used:bool, fallback_reason:str|None, ts_synonyms_available:bool}}
        """
        name = _strip_meta_prefix(name)

        # --- Resolve (category, object_name) via metadata-first cascade ---
        # find_module() работает только по BSL-модулям, поэтому XML-only объекты
        # (Enums, Constants, многие Catalogs без ObjectModule, FunctionalOption и т.п.)
        # через него не находятся. Каскад: index metadata → index synonyms →
        # index enum_values → BSL modules → live glob по категориям.
        category, obj_name = _resolve_object_for_full_structure(name, prefer_category=category_hint)
        if not category and not obj_name and category_hint:
            # Object not present in the hinted category → retry unfiltered (find it anywhere).
            category, obj_name = _resolve_object_for_full_structure(name)
        if not category and not obj_name:
            return {
                "error": f"Объект '{name}' не найден",
                "_meta": {"index_used": False, "fallback_reason": "object_not_found", "ts_synonyms_available": False},
            }
        category = category or ""
        obj_name = obj_name or name

        result: dict = {
            "object_name": obj_name,
            "category": category,
            "synonym": None,
            "posting": None,
            "attributes": [],
            "tabular_sections": [],
            "dimensions": [],
            "resources": [],
            "predefined_items": [],
            "enum_values_for_typed_refs": {},
            "forms": [],
            "_meta": {
                "index_used": False,
                "fallback_reason": None,
                "ts_synonyms_available": False,
            },
        }

        # Категории, у которых ОБЪЕКТНЫХ атрибутов нет по природе:
        # для них пустой результат get_object_attributes — это норма, а не
        # признак "stale index". Не путать с _METADATA_ONLY_CATEGORIES, который
        # включает в т.ч. Catalogs/Documents — у них атрибуты есть.
        _CATEGORIES_WITHOUT_ATTRIBUTES = {
            "enums",
            "constants",
            "functionaloptions",
            "eventsubscriptions",
            "scheduledjobs",
            "definedtypes",
            "subsystems",
            "roles",
            "exchangeplans",  # имеют content вместо обычных атрибутов
        }

        def _populate_from_live_xml() -> str | None:
            """Read object via parse_object_xml and fill result fields.

            Возвращает None при успехе, текст ошибки при неудаче.

            Side-effects через `result["_meta"]`:
              - Если live дозаполнил СТРУКТУРНЫЕ секции (attributes, dimensions,
                resources, tabular_sections, predefined_items, которые были
                пустыми до вызова) — выставляется приватный маркер
                `_meta["_live_filled_structural"] = True`. Вызывающий код
                (index path) использует его чтобы понизить index_used=False
                с fallback_reason='index_partially_enriched_from_live_xml'.
              - `_meta["ts_synonyms_available"]` ставится True ТОЛЬКО когда после
                наполнения/обогащения у хотя бы одной TS есть НЕПУСТОЙ synonym.
            """
            try:
                meta = parse_object_xml(f"{category}/{obj_name}" if category else obj_name)
            except Exception as exc:
                return f"parse_failed: {type(exc).__name__}: {exc}"
            if not isinstance(meta, dict):
                return "parse_failed: non-dict result"

            structural_filled_from_live = False  # для понижения index_used

            # synonym / posting — НЕ структурные данные, обогащение не считается
            # за «mixed source».
            if not result["synonym"]:
                result["synonym"] = meta.get("synonym") or None
            if meta.get("posting") and not result.get("posting"):
                result["posting"] = meta["posting"]

            # --- Structural sections ---
            if not result["attributes"]:
                for attr in meta.get("attributes", []) or []:
                    result["attributes"].append(
                        {
                            "name": attr.get("name", ""),
                            "synonym": attr.get("synonym", "") or "",
                            "type": [attr.get("type", "")]
                            if isinstance(attr.get("type"), str)
                            else (attr.get("type") or []),
                        }
                    )
                if result["attributes"]:
                    result["attributes"] = [_AttrRecord(r) for r in result["attributes"]]
                    structural_filled_from_live = True
            if not result["dimensions"]:
                for dim in meta.get("dimensions", []) or []:
                    result["dimensions"].append(
                        {
                            "name": dim.get("name", ""),
                            "synonym": dim.get("synonym", "") or "",
                            "type": [dim.get("type", "")]
                            if isinstance(dim.get("type"), str)
                            else (dim.get("type") or []),
                        }
                    )
                if result["dimensions"]:
                    result["dimensions"] = [_AttrRecord(r) for r in result["dimensions"]]
                    structural_filled_from_live = True
            if not result["resources"]:
                for res_attr in meta.get("resources", []) or []:
                    result["resources"].append(
                        {
                            "name": res_attr.get("name", ""),
                            "synonym": res_attr.get("synonym", "") or "",
                            "type": [res_attr.get("type", "")]
                            if isinstance(res_attr.get("type"), str)
                            else (res_attr.get("type") or []),
                        }
                    )
                if result["resources"]:
                    result["resources"] = [_AttrRecord(r) for r in result["resources"]]
                    structural_filled_from_live = True

            # --- Tabular sections: либо полное заполнение, либо обогащение synonym ---
            if not result["tabular_sections"]:
                # Index не дал TS — заполняем целиком из live (synonym у TS будет).
                for ts in meta.get("tabular_sections", []) or []:
                    result["tabular_sections"].append(
                        {
                            "name": ts.get("name", ""),
                            "synonym": ts.get("synonym", "") or None,
                            "columns": [
                                {
                                    "name": c.get("name", ""),
                                    "synonym": c.get("synonym", "") or "",
                                    "type": [c.get("type", "")]
                                    if isinstance(c.get("type"), str)
                                    else (c.get("type") or []),
                                }
                                for c in ts.get("attributes", []) or []
                            ],
                        }
                    )
                if result["tabular_sections"]:
                    for _ts in result["tabular_sections"]:
                        _ts["columns"] = [_AttrRecord(c) for c in _ts.get("columns") or []]
                    structural_filled_from_live = True
            else:
                # TS уже из индекса — у них synonym=None. Обогащаем по name.
                live_ts_by_name = {(ts.get("name") or "").lower(): ts for ts in meta.get("tabular_sections", []) or []}
                for ts_in_result in result["tabular_sections"]:
                    name_key = (ts_in_result.get("name") or "").lower()
                    live_ts = live_ts_by_name.get(name_key)
                    if not live_ts:
                        continue
                    if not ts_in_result.get("synonym"):
                        new_syn = live_ts.get("synonym", "") or None
                        if new_syn:
                            ts_in_result["synonym"] = new_syn

            # ts_synonyms_available: True ТОЛЬКО если у хотя бы одной TS есть
            # реально непустой synonym (после полного заполнения / обогащения).
            if any(ts.get("synonym") for ts in result["tabular_sections"]):
                result["_meta"]["ts_synonyms_available"] = True

            # forms — НЕ структурные данные.
            forms = meta.get("forms")
            if forms and not result["forms"]:
                result["forms"] = list(forms)

            # predefined_items — структурные.
            if not result["predefined_items"]:
                try:
                    pi_results = find_predefined(object_name=f"{category}/{obj_name}" if category else obj_name)
                except Exception:
                    pi_results = []
                for item in pi_results or []:
                    result["predefined_items"].append(
                        {
                            "name": item.get("item_name", ""),
                            "synonym": item.get("item_synonym", "") or "",
                            "code": item.get("item_code", "") or "",
                            "types": item.get("types", []) or [],
                            "is_folder": item.get("is_folder", False),
                        }
                    )
                if result["predefined_items"]:
                    structural_filled_from_live = True

            if structural_filled_from_live:
                # Приватный маркер для index path: сигнал что live дозаполнил
                # структуру → нужно понизить index_used.
                result["_meta"]["_live_filled_structural"] = True
            return None

        # --- Index path ---
        attrs_rows: list[dict] | None = None
        if idx_reader is not None:
            try:
                attrs_rows = idx_reader.get_object_attributes(object_name=obj_name, category=category, limit=2000)
            except Exception:
                attrs_rows = None

        index_attempted = attrs_rows is not None  # таблица существует и доступна
        if index_attempted:
            # Group attributes by attr_kind / ts_name.
            ts_groups: dict[str, list[dict]] = {}
            for row in attrs_rows or []:
                kind = row.get("attr_kind") or ""
                attr_dict = _AttrRecord(
                    {
                        "name": row.get("attr_name", ""),
                        "synonym": row.get("attr_synonym", "") or "",
                        "type": row.get("attr_type", []) or [],
                    }
                )
                if kind == "attribute":
                    result["attributes"].append(attr_dict)
                elif kind == "dimension":
                    result["dimensions"].append(attr_dict)
                elif kind == "resource":
                    result["resources"].append(attr_dict)
                elif kind == "ts_attribute":
                    ts_name = row.get("ts_name") or ""
                    ts_groups.setdefault(ts_name, []).append(attr_dict)

            for ts_name, columns in ts_groups.items():
                result["tabular_sections"].append(
                    {
                        "name": ts_name,
                        "synonym": None,  # TS synonym is not in object_attributes table
                        "columns": columns,
                    }
                )

            # Predefined items
            try:
                pi_rows = idx_reader.get_predefined_items(object_name=obj_name, limit=2000)
            except Exception:
                pi_rows = None
            if pi_rows:
                result["predefined_items"] = [
                    {
                        "name": r.get("item_name", ""),
                        "synonym": r.get("item_synonym", "") or "",
                        "code": r.get("item_code", "") or "",
                        "types": r.get("types", []) or [],
                        "is_folder": r.get("is_folder", False),
                    }
                    for r in pi_rows
                ]

            # Object synonym (object_synonyms table) — small targeted query via search_objects.
            try:
                so_rows = idx_reader.search_objects(obj_name, limit=20)
            except Exception:
                so_rows = None
            if so_rows:
                for s in so_rows:
                    if (s.get("object_name") or "").lower() == obj_name.lower() and (
                        not category or (s.get("category") or "").lower() == category.lower()
                    ):
                        result["synonym"] = s.get("synonym") or None
                        break

            # Forms from form_elements (distinct form_name).
            try:
                fe_rows = idx_reader.get_form_elements(object_name=obj_name)
            except Exception:
                fe_rows = None
            if fe_rows:
                seen_forms: list[str] = []
                for r in fe_rows:
                    fname = r.get("form_name") or ""
                    if fname and fname not in seen_forms:
                        seen_forms.append(fname)
                result["forms"] = seen_forms

            # --- Determine if index actually delivered STRUCTURAL data ---
            # Семантика _meta.index_used:
            #   True  ⇒ структурные данные (attributes / dimensions / resources /
            #            tabular_sections / predefined_items) взяты из индекса.
            #   False ⇒ структуру дал live XML (или объект — XML-only по природе).
            #
            # synonym и forms сюда НЕ входят: одна строка в object_synonyms
            # без записей в object_attributes — это «индекс знает что объект
            # есть», но не «индекс дал структуру». Для нормальных категорий
            # (Catalogs/Documents/Registers/...) это сигнал stale/incomplete index
            # → нужен live fallback.
            cat_lower = category.lower() if category else ""
            has_structural_index_data = bool(
                result["attributes"]
                or result["dimensions"]
                or result["resources"]
                or result["tabular_sections"]
                or result["predefined_items"]
            )

            if has_structural_index_data:
                # Структура реально взята из индекса.
                result["_meta"]["index_used"] = True
                # synonym/forms — вспомогательные. Если индекс их не дал
                # (например, object_synonyms не наполнен или это объект без форм),
                # дополним live XML.
                # Также: TS из object_attributes идут с synonym=None — таблица
                # не хранит синоним самой ТЧ. Если есть TS без synonym —
                # подтягиваем синонимы из live XML по совпадению name (см.
                # _populate_from_live_xml). Ошибки игнорируем — структура уже
                # на руках.
                ts_needs_synonyms = any(ts.get("synonym") is None for ts in result["tabular_sections"])
                if not result["synonym"] or not result["forms"] or ts_needs_synonyms:
                    _populate_from_live_xml()
                # Если live дозаполнил СТРУКТУРНЫЕ секции (например, индекс дал
                # attributes, но не TS — а live добавил TS), это уже не «чистый
                # index path». Понижаем index_used и сигнализируем смешанный
                # источник через специальный fallback_reason. Маркер
                # _live_filled_structural — приватный, удаляем после использования.
                if result["_meta"].pop("_live_filled_structural", False):
                    result["_meta"]["index_used"] = False
                    result["_meta"]["fallback_reason"] = "index_partially_enriched_from_live_xml"
            else:
                # Структурных данных индекс не дал. Идём в live XML fallback,
                # независимо от того, нашёлся ли synonym/forms — они остаются
                # как «бонус из индекса», но index_used=False, потому что
                # СТРУКТУРА (то ради чего вызывается этот хелпер) пришла live.
                result["_meta"]["index_used"] = False
                if cat_lower in _CATEGORIES_WITHOUT_ATTRIBUTES:
                    # XML-only категория (Enum/Constant/FunctionalOption/...) —
                    # пустой object_attributes здесь норма, не stale index.
                    # Помечаем отдельной причиной чтобы агент не паниковал.
                    result["_meta"]["fallback_reason"] = "category_without_attributes_filled_via_live_xml"
                else:
                    # Нормальная категория, но object_attributes пустой —
                    # признак stale/incomplete index для конкретного объекта.
                    result["_meta"]["fallback_reason"] = "index_empty_for_object"
                err = _populate_from_live_xml()
                if err:
                    # И live тоже не смог — фиксируем причину парсинга,
                    # перетирая category_without_attributes_filled_via_live_xml.
                    result["_meta"]["fallback_reason"] = err
                # Приватный маркер уже отражён через index_used=False и
                # явный fallback_reason — удаляем чтобы не утекал в API.
                result["_meta"].pop("_live_filled_structural", None)
        else:
            # --- Fallback: live XML parse (index unavailable / table missing) ---
            result["_meta"]["index_used"] = False
            result["_meta"]["fallback_reason"] = "index_unavailable_or_table_missing"
            err = _populate_from_live_xml()
            if err:
                result["_meta"]["fallback_reason"] = err
                result["_meta"].pop("_live_filled_structural", None)
                return result
            result["_meta"].pop("_live_filled_structural", None)

        # NOTE: posting для документов в индексе v12 не хранится. Если live XML
        # был вызван (для enrichment / fallback), posting подхватывается внутри
        # _populate_from_live_xml. На чистом index path без enrichment posting
        # остаётся None — это согласовано с контрактом «index_used=True
        # = без чтения live XML». Если posting нужен независимо от пути —
        # используй find_register_movements(doc_name): при пустом результате
        # он сам делает live posting check (см. Tier 1.2).

        # --- Expand enum-ref types → values ---
        # Принимаем три формы записи типа перечисления:
        #   EnumRef.X            — стандартный 1С-формат (CF/EDT)
        #   ПеречислениеСсылка.X — русскоязычный alias
        #   Enum.X               — канонизированный формат, который может появиться
        #                          в нормализованных таблицах метаданных
        _ENUM_TYPE_PREFIXES = ("EnumRef.", "ПеречислениеСсылка.", "Enum.")

        def _is_enum_ref(t) -> bool:
            return isinstance(t, str) and t.startswith(_ENUM_TYPE_PREFIXES)

        enum_refs: list[str] = []
        for attr_group in (
            result["attributes"],
            result["dimensions"],
            result["resources"],
        ):
            for a in attr_group:
                for t in a.get("type", []) or []:
                    if _is_enum_ref(t) and t not in enum_refs:
                        enum_refs.append(t)
        for ts in result["tabular_sections"]:
            for c in ts.get("columns", []) or []:
                for t in c.get("type", []) or []:
                    if _is_enum_ref(t) and t not in enum_refs:
                        enum_refs.append(t)

        for ref in enum_refs:
            short_name = ref.split(".", 1)[1] if "." in ref else ref
            try:
                ev = find_enum_values(short_name)
            except Exception:
                continue
            if isinstance(ev, dict) and not ev.get("error"):
                result["enum_values_for_typed_refs"][ref] = [
                    {"name": v.get("name", ""), "synonym": v.get("synonym", "") or ""} for v in (ev.get("values") or [])
                ]

        return result

    def _resolve_object_for_modules(name: str):
        """``(category, object_name, modules)`` для объекта одним прямым exact-сканом
        ``_index_state`` — НЕ через capped ``find_module`` и НЕ через
        ``_resolve_object_for_full_structure`` напрямую (оба возвращают на шаг раньше
        cap-false-negative / fuzzy ``modules[0]``).

        ``modules`` — ``list[(rel_path, BslFileInfo)]`` собственных модулей объекта;
        идентичность ``(category, object_name)`` берётся из ПЕРВОЙ совпавшей по
        ``object_name`` строки, по ней же фильтруются модули (развязка коллизии
        одноимённых объектов в разных категориях — берётся первая). Synonym→canon
        fallback к ``_resolve_object_for_full_structure`` срабатывает ТОЛЬКО когда
        прямой скан пуст, с повторным exact-сканом по канон-имени (чтобы capped/fuzzy
        путь резолвера никогда не был источником финального списка модулей).
        ``(None, None, [])`` если ничего не нашлось.
        """
        _ensure_index()

        def _scan(target_lower: str, want_cat_lower: str | None = None):
            rows = [
                (rel, info)
                for rel, info in _index_state
                if info.object_name
                and info.object_name.lower() == target_lower
                and (want_cat_lower is None or (info.category or "").lower() == want_cat_lower)
            ]
            if not rows:
                return None, None, []
            # Детерминированный выбор identity при коллизии одноимённых объектов в
            # разных категориях: get_all_modules() без ORDER BY → порядок _index_state
            # не гарантирован. Сорт по rel_path фиксирует, какая категория «первая».
            rows.sort(key=lambda ri: ri[0])
            category = rows[0][1].category
            object_name = rows[0][1].object_name
            cat_lower = (category or "").lower()
            modules = [(rel, info) for rel, info in rows if (info.category or "").lower() == cat_lower]
            return category, object_name, modules

        category, object_name, modules = _scan(name.lower())
        if modules:
            return category, object_name, modules

        # Прямой скан пуст → synonym→canon, затем повторный exact-скан по канону.
        # Категорию канона (_canon_cat) ПРОКИДЫВАЕМ в скан как фильтр: иначе одноимённый
        # канон в другой категории мог бы перебить разрешённый резолвером объект.
        _canon_cat, canon_name = _resolve_object_for_full_structure(name)
        if canon_name:
            want_cat = _canon_cat.lower() if _canon_cat else None
            category, object_name, modules = _scan(canon_name.lower(), want_cat)
            if modules:
                return category, object_name, modules
        return None, None, []

    def _build_module_entries(modules, include_methods: bool, no_live: bool):
        """Build per-module entries + roll-ups from a resolved ``[(rel, info)]`` list.
        Shared by ``get_object_modules`` and ``get_object_profile`` (modules section).
        Returns ``(module_entries, totals, all_index_used, any_skipped_live)``."""
        module_entries: list[dict] = []
        roll_methods = roll_exports = roll_overrides = 0
        all_index_used = True
        any_skipped_live = False
        for rel, info in modules:
            outline = get_module_outline(rel, include_methods=include_methods, no_live=no_live)
            ov_methods: list[str] = []
            if idx_reader is not None:
                try:
                    ov_map = idx_reader.get_overrides_for_path(rel) or {}
                    ov_methods = sorted(ov_map.keys())
                except Exception:
                    ov_methods = []
            totals = outline.get("totals") or {}
            m_meta = outline.get("_meta") or {}
            if not m_meta.get("index_used"):
                all_index_used = False
            if m_meta.get("skipped_live"):
                any_skipped_live = True
            module_entries.append(
                {
                    "path": rel,
                    "module_type": info.module_type,
                    "form_name": info.form_name,
                    "totals": totals,
                    "outline": outline.get("outline", []),
                    "overrides": {"count": len(ov_methods), "methods": ov_methods},
                    "_meta": {
                        "index_used": bool(m_meta.get("index_used")),
                        "fallback_reason": m_meta.get("fallback_reason"),
                        "skipped_live": bool(m_meta.get("skipped_live")),
                    },
                }
            )
            roll_methods += totals.get("methods", 0)
            roll_exports += totals.get("exports", 0)
            roll_overrides += len(ov_methods)
        totals_roll = {
            "modules": len(module_entries),
            "methods": roll_methods,
            "exports": roll_exports,
            "overrides": roll_overrides,
        }
        return module_entries, totals_roll, all_index_used, any_skipped_live

    def _modules_for_identity(category: str | None, object_name: str | None):
        """``[(rel, info)]`` modules of EXACTLY ``(category, object_name)`` — category-aware
        (homonym-safe) direct index scan. Used by ``get_object_profile`` so the modules
        section never re-resolves to a different homonym than the profile identity."""
        _ensure_index()
        name_lower = (object_name or "").lower()
        if not name_lower:
            return []
        cat_lower = (category or "").lower()
        rows = [
            (rel, info)
            for rel, info in _index_state
            if info.object_name
            and info.object_name.lower() == name_lower
            and (not cat_lower or (info.category or "").lower() == cat_lower)
        ]
        rows.sort(key=lambda ri: ri[0])
        return rows

    def get_object_modules(name: str, include_methods: bool = False, no_live: bool = False) -> dict:
        """Лёгкий индексный «код-side» двойник ``get_object_full_structure``: объект →
        все его модули + скелеты ``#Область`` + агрегаты + флаги перехватов, в один вызов.

        В отличие от ``analyze_object`` НЕ зовёт ``parse_object_xml`` ни на одном пути
        и НЕ зовёт ``extract_procedures`` на валидном индексном пути (там скелет берётся
        из ``idx_reader.get_outline_data`` — см. ``module._meta.index_used``). При
        no/old-index, отсутствии module-row или пустых methods модуль честно уходит в
        live (виден в ``module._meta.fallback_reason``), поэтому «дешёвый индексный
        двойник» гарантирован именно на валидном индексном пути.

        Args:
            name: имя объекта (префикс типа ``Документ.`` снимается). Резолв — прямым
                exact-сканом индекса (см. ``_resolve_object_for_modules``).
            include_methods: ``False`` (дефолт) — дерево ``#Область`` + агрегаты
                (ограничивает вывод); ``True`` — листовые методы внутри областей.
            no_live: ``False`` (дефолт) — модуль без валидного индекс-пути уходит в live
                (читает файл). ``True`` — пробрасывается в ``get_module_outline``: stale/нет
                индекса по модулю → секция помечается ``_meta.skipped_live=True`` БЕЗ чтения
                файла (compact-профиль не должен ходить в live по модулям).

        Returns:
            ``{object_name, category,
               modules: [{path, module_type, form_name,
                          totals:{methods,exports,regions,loc}, outline:[...],
                          overrides:{count, methods:[...]}, _meta:{index_used, fallback_reason}}],
               totals: {modules, methods, exports, overrides},
               _meta: {index_used, modules_truncated}}``
            либо ``{error, _meta}`` если объект не найден.

        Дизамбигуация: метаданные → ``get_object_full_structure``; код-скелет →
        ``get_object_modules``; тяжёлый разбор тел → ``analyze_object``.
        """
        name = _strip_meta_prefix(name)
        category, object_name, modules = _resolve_object_for_modules(name)
        if not modules:
            return {
                "error": f"Объект '{name}' не найден (нет модулей в индексе)",
                "_meta": {"index_used": idx_reader is not None, "modules_truncated": False},
            }

        module_entries, totals_roll, all_index_used, any_skipped_live = _build_module_entries(
            modules, include_methods, no_live
        )
        return {
            "object_name": object_name,
            "category": category,
            "modules": module_entries,
            "totals": totals_roll,
            "_meta": {
                "index_used": idx_reader is not None and all_index_used,
                "modules_truncated": False,
                "modules_skipped_live": any_skipped_live,
            },
        }

    def analyze_object(name: str) -> dict:
        """Full object profile in one call: XML metadata + all modules + procedures + exports.

        Returns: dict with name, category, metadata, modules."""
        name = _strip_meta_prefix(name)
        modules = find_module(name)
        exact = [m for m in modules if (m.get("object_name") or "").lower() == name.lower()]
        if not exact:
            exact = modules[:20]
        if not exact:
            return {"error": f"Объект '{name}' не найден"}

        category = exact[0].get("category", "")
        obj_name = exact[0].get("object_name", "")

        metadata: dict = {}
        if category and obj_name:
            try:
                metadata = parse_object_xml(f"{category}/{obj_name}")
            except Exception:
                pass

        module_details = []
        for mod in exact:
            path = mod["path"]
            try:
                procs = extract_procedures(path)
                exports = [p for p in procs if p.get("is_export")]
            except Exception:
                procs, exports = [], []

            module_details.append(
                {
                    "path": path,
                    "module_type": mod.get("module_type", ""),
                    "form_name": mod.get("form_name"),
                    "procedures_count": len(procs),
                    "exports_count": len(exports),
                    "procedures": procs,
                    "exports": exports,
                }
            )

        return {
            "name": obj_name,
            "category": category,
            "metadata": metadata,
            "modules": module_details,
        }

    # ── get_object_profile — one-shot compact object aggregate ───
    _PROFILE_DEFAULT_SECTIONS = (
        "structure",
        "modules",
        "registers",
        "subscriptions",
        "roles",
        "functional_options",
    )
    _PROFILE_SECTION_ALIASES = {
        "structure": "structure",
        "attrs": "structure",
        "attributes": "structure",
        "metadata": "structure",
        "реквизиты": "structure",
        "структура": "structure",
        "modules": "modules",
        "code": "modules",
        "код": "modules",
        "модули": "modules",
        "registers": "registers",
        "movements": "registers",
        "движения": "registers",
        "регистры": "registers",
        "subscriptions": "subscriptions",
        "events": "subscriptions",
        "event_subscriptions": "subscriptions",
        "подписки": "subscriptions",
        "события": "subscriptions",
        "roles": "roles",
        "rights": "roles",
        "права": "roles",
        "роли": "roles",
        "functional_options": "functional_options",
        "fo": "functional_options",
        "options": "functional_options",
        "опции": "functional_options",
    }

    def get_object_profile(
        name: str,
        sections: list[str] | None = None,
        include_flow: bool = False,
        include_code_usages: bool = False,
        limit: int = 20,
    ) -> dict:
        """Один top-level агрегат «обзор объекта за 1 вызов»: понижает «пол» вызовов
        для доминирующей задачи (полный анализ объекта), отдавая compact roll-up дешёвых
        index-path секций ВМЕСТО ~10 одиночных хелперов.

        Дизамбигуация: ВЕСЬ обзор за 1 вызов → get_object_profile; только код-скелет →
        get_object_modules; только метаданные → get_object_full_structure; глубокий разбор
        потока/тел → analyze_document_flow / analyze_object.

        Args:
            name: имя объекта (можно с префиксом ``Документ.``/``Document.``).
            sections: ``None`` → дефолтный compact-набор
                (structure, modules, registers, subscriptions, roles, functional_options);
                список → ровно запрошенные (алиасы: attrs→structure, events→subscriptions,
                movements→registers, права→roles, …).
            include_flow: ``True`` → секция ``flow`` (полный analyze_document_flow,
                читает тела — ДОРОГО). Дефолт ``False``.
            include_code_usages: ``True`` → секция ``code_usages`` (reverse code-usage).
            limit: размер top-N preview для items каждой секции (дефолт 20).

        Returns:
            ``{object_name, category, sections:{<name>:section}, _meta:{identity_source,
            extension_visibility, total_elapsed_ms, sections:[{name, elapsed_ms, source,
            status, items_count, truncated}]}}`` либо ``{error, hint?, _meta}`` ТОЛЬКО если
            identity не резолвится. Каждая section: ``{status: ok|empty|error|unavailable|
            skipped, summary:{доменные счётчики}, items:[top-N], total, returned, has_more,
            _meta:{source: index|live|mixed|unknown, fallback_reason, reason, truncated,
            elapsed_ms, error}}``. БЕЗ тел процедур.

            Compact-инвариант: при ``idx_reader is None`` ВСЕ data-секции ``unavailable``
            (публичные get_object_*/find_* НЕ зовутся — никакого glob/live); тяжёлый live —
            только под ``include_flow``/``include_code_usages``.
        """
        import time as _time_prof
        from rlm_tools_bsl.bsl_index import (
            _CATEGORY_TO_TYPE_PREFIX as _cat2prefix,
            _HINT_PREFIX_TO_CATEGORY as _prefix2cat,
        )

        prof_t0 = _time_prof.monotonic()
        has_index = idx_reader is not None
        raw_name = name

        def _ms(t0: float) -> float:
            return round((_time_prof.monotonic() - t0) * 1000, 1)

        extension_visibility = "main_with_nearby_extensions" if _ext_paths_raw else "standalone"

        # An explicit input type-prefix (Документ.X / Catalog.X) → preferred category.
        # Honoured in BOTH paths so a cross-category homonym (Document.X vs Catalog.X)
        # resolves to the explicitly requested category, not whichever the name-cascade
        # happens to hit first. The prefix is recognised CASE-INSENSITIVELY (casefold), so
        # `bare` is derived from the suffix too — a case-sensitive _strip_meta_prefix would
        # NOT strip a lowercase 'document.' / upper 'DOCUMENT.', leaving a malformed name.
        prefix_category = None
        if "." in raw_name:
            head, _, _tail = raw_name.partition(".")
            prefix_category = _prefix2cat.get(head.casefold())
        bare = raw_name.partition(".")[2].strip() if prefix_category else _strip_meta_prefix(name)

        # ── upfront identity resolve (ONCE → (category, object_name)) ──
        identity_source = None
        category = object_name = None
        if has_index:
            if prefix_category:
                category, object_name = _resolve_object_for_full_structure(bare, prefer_category=prefix_category)
                identity_source = "index_prefix"
                if not object_name:
                    # Object not in the requested category → resolve by name anywhere.
                    category, object_name = _resolve_object_for_full_structure(bare)
                    identity_source = "index"
            else:
                category, object_name = _resolve_object_for_full_structure(bare)
                identity_source = "index"
            if not object_name:
                return {
                    "error": f"Объект '{raw_name}' не найден",
                    "_meta": {"identity_source": "unresolved", "total_elapsed_ms": _ms(prof_t0)},
                }
        else:
            # NO index → never glob. Identity strictly from the input type-prefix.
            if prefix_category:
                category, object_name, identity_source = prefix_category, bare, "input_prefix"
            else:
                return {
                    "error": "no_index_identity_unresolved",
                    "hint": "передай объект с префиксом типа (Документ.X / Справочник.X / Document.X) "
                    "или построй индекс — без индекса bare-имя не резолвится без glob",
                    "_meta": {"identity_source": "none", "total_elapsed_ms": _ms(prof_t0)},
                }

        ref = (
            f"{_cat2prefix.get(category, category)}.{object_name}"
            if (category and object_name)
            else (object_name or "")
        )

        # ── which sections to run ──────────────────────────────────
        if sections is None:
            wanted = list(_PROFILE_DEFAULT_SECTIONS)
        else:
            wanted = []
            for s in sections:
                key = _PROFILE_SECTION_ALIASES.get(str(s).strip().lower())
                if key and key not in wanted:
                    wanted.append(key)
        if include_flow and "flow" not in wanted:
            wanted.append("flow")
        if include_code_usages and "code_usages" not in wanted:
            wanted.append("code_usages")

        # ── section builders (each in its OWN try/except via the runner) ──
        def _unavailable(reason: str) -> dict:
            return {
                "status": "unavailable",
                "summary": {},
                "items": [],
                "total": 0,
                "returned": 0,
                "has_more": False,
                "_meta": {"source": "unknown", "reason": reason},
            }

        def _from_reader_list(rows, summary_fn, item_fn, source="index") -> dict:
            # rows: None → capability_missing (table missing); [] → empty; [...] → ok.
            if rows is None:
                return _unavailable("capability_missing")
            total = len(rows)
            items = [item_fn(r) for r in rows[: max(0, int(limit))]]
            return {
                "status": "empty" if total == 0 else "ok",
                "summary": summary_fn(rows),
                "items": items,
                "total": total,
                "returned": len(items),
                "has_more": total > len(items),
                "_meta": {"source": source},
            }

        def _module_provenance(rel: str) -> dict:
            # _index_state mixes MAIN + nearby-extension (CFE) rows, so a compact main+CFE
            # profile is ambiguous without telling the agent which root each module came from.
            if rel in _extension_paths_set:
                root = _extension_root_for.get(rel) or ""
                return {
                    "is_extension": True,
                    "source_root": root or None,
                    # REAL configured name (from Configuration.xml/.mdo via extension_detector),
                    # not the folder basename; basename only as a fallback when unmatched.
                    "extension_name": _extension_name_for_root(root),
                }
            return {"is_extension": False, "source_root": None, "extension_name": None}

        def _sec_structure() -> dict:
            if not has_index:
                return _unavailable("no_index")
            # category_hint keeps structure on the SAME homonym as the resolved identity.
            s = get_object_full_structure(object_name, category_hint=category)
            if not isinstance(s, dict) or s.get("error"):
                return {
                    "status": "error",
                    "summary": {},
                    "items": [],
                    "total": 0,
                    "returned": 0,
                    "has_more": False,
                    "_meta": {"source": "unknown", "error": (s or {}).get("error", "no structure")},
                }
            m = s.get("_meta") or {}
            idx_used = bool(m.get("index_used"))
            fr = m.get("fallback_reason")
            if idx_used and fr in (
                "category_without_attributes_filled_via_live_xml",
                "index_partially_enriched_from_live_xml",
            ):
                src = "mixed"
            else:
                src = "index" if idx_used else "live"
            attrs = s.get("attributes") or []
            dims = s.get("dimensions") or []
            res = s.get("resources") or []
            summary = {
                "posting": s.get("posting"),
                "attributes": len(attrs),
                "tabular_sections": len(s.get("tabular_sections") or []),
                "dimensions": len(dims),
                "resources": len(res),
                "predefined_items": len(s.get("predefined_items") or []),
                "forms": len(s.get("forms") or []),
            }
            primary = attrs or dims or res
            names = [x.get("name") for x in primary if x.get("name")]
            items = [{"name": n} for n in names[: max(0, int(limit))]]
            meta = {"source": src, "fallback_reason": fr}
            # Extensions are visible to the structure resolver (ext XML/live can merge attrs),
            # so flag it — counts may include CFE-contributed fields (per-attribute attribution
            # isn't tracked, hence a coarse section-level signal).
            if _ext_paths_raw:
                meta["extension_visibility"] = extension_visibility
            # Verification guardrail: structure uses the SAME resolver as identity, so a
            # mismatch flags resolver drift (not a normal homonym).
            if (s.get("object_name") or "").lower() != (object_name or "").lower() or (
                s.get("category") or ""
            ).lower() != (category or "").lower():
                meta["identity_match"] = False
            return {
                "status": "ok",
                "summary": summary,
                "items": items,
                "total": len(names),
                "returned": len(items),
                "has_more": len(names) > len(items),
                "_meta": meta,
            }

        def _sec_modules() -> dict:
            if not has_index:
                return _unavailable("no_index")
            mods = _modules_for_identity(category, object_name)
            entries, totals_roll, all_idx, skipped = _build_module_entries(mods, include_methods=False, no_live=True)
            if not entries:
                return {
                    "status": "empty",
                    "summary": {"modules": 0, "methods": 0, "exports": 0, "overrides": 0},
                    "items": [],
                    "total": 0,
                    "returned": 0,
                    "has_more": False,
                    "_meta": {"source": "index"},
                }
            items = [
                {
                    "path": e["path"],
                    "module_type": e["module_type"],
                    "methods": e["totals"].get("methods", 0),
                    "exports": e["totals"].get("exports", 0),
                    "overrides": e["overrides"]["count"],
                    "skipped_live": e["_meta"]["skipped_live"],
                    **_module_provenance(e["path"]),
                }
                for e in entries[: max(0, int(limit))]
            ]
            ext_modules = sum(1 for e in entries if e["path"] in _extension_paths_set)
            # skipped_live → totals (methods/exports) are NOT authoritative (stale modules
            # were not read to avoid live). Mark the whole section 'skipped', not 'ok', so the
            # zero counts aren't mistaken for the truth — caller can get_object_modules(no_live=False).
            meta = {"source": "index" if all_idx else "mixed", "modules_skipped_live": skipped}
            if skipped:
                meta["reason"] = "stale_modules_skipped_live"
            if _ext_paths_raw:
                meta["extension_visibility"] = extension_visibility
            return {
                "status": "skipped" if skipped else "ok",
                "summary": {**totals_roll, "extension_modules": ext_modules},
                "items": items,
                "total": len(entries),
                "returned": len(items),
                "has_more": len(entries) > len(items),
                "_meta": meta,
            }

        def _profile_movement_pairs(movement_rows: list[dict]) -> list[tuple[str, str]]:
            """Lossy compact representation, deduplicated after dropping file provenance."""
            ordered: list[tuple[str, str]] = []
            seen: set[tuple[str, str]] = set()
            for movement in movement_rows:
                source = str(movement.get("source", "code"))
                register_name = str(movement.get("register_name") or "")
                key = (source.casefold(), register_name.casefold())
                if key in seen:
                    continue
                seen.add(key)
                ordered.append((source, register_name))
            return ordered

        def _sec_registers() -> dict:
            if not has_index:
                return _unavailable("no_index")
            if (category or "") != "Documents":
                return {
                    "status": "skipped",
                    "summary": {},
                    "items": [],
                    "total": 0,
                    "returned": 0,
                    "has_more": False,
                    "_meta": {"source": "index", "reason": "not_a_document"},
                }
            rows = idx_reader.get_register_movements(object_name)
            # Compact-профиль сохраняет zero-live fast path, когда SQLite уже знает хотя бы
            # одно прямое code-движение документа. English bridge нужен для устранения ложного
            # нуля; полный live-union при непустом снимке остается подробному helper.
            has_indexed_code_movements = rows is not None and any(
                str(row.get("source") or "code").casefold() == "code" for row in rows
            )
            main_alias_movements = [] if has_indexed_code_movements else _live_main_alias_movements(object_name)
            (
                cfe_movements,
                cfe_modules_scanned,
                cfe_modules_unreadable,
                cfe_interceptors,
            ) = _live_extension_movements(object_name)
            _cfe_active, _cfe_suppressed, cfe_replacement_meta = _apply_cfe_posting_replacement(
                cfe_movements, cfe_interceptors
            )
            live_alias_movements = _merge_movement_rows(main_alias_movements, cfe_movements)
            if rows is None:
                # Таблица register_movements пуста ГЛОБАЛЬНО (или отсутствует) — мы НЕ вправе
                # заявлять code_registers=0 для main. Но точные live-движения CFE — положительный
                # факт, его нельзя прятать из-за неизвестной полноты main: отдаём известный
                # lower-bound со status=unavailable + partial-marker. Без CFE сохраняем прежний
                # unavailable и прикрепляем сигнал обработчика, если он доказан.
                suppressible_main_rows = (
                    _main_handler_only_movement_keys(object_name)
                    if cfe_replacement_meta and not cfe_replacement_meta["main_handler_continuation_visible"]
                    else set()
                )
                live_alias_movements, suppressed_main, cfe_replacement_meta = _apply_cfe_posting_replacement(
                    live_alias_movements, cfe_interceptors, suppressible_main_rows
                )
                if live_alias_movements:
                    ordered = _profile_movement_pairs(live_alias_movements)
                    total = len(ordered)
                    page = ordered[: max(0, int(limit))]
                    summary = {
                        "code_registers": total,
                        "erp_mechanisms": 0,
                        "manager_tables": 0,
                        "adapted_registers": 0,
                    }
                    suppressed_pairs = _profile_movement_pairs(suppressed_main)
                    if suppressed_pairs:
                        summary["main_code_registers_suppressed_by_cfe"] = len(suppressed_pairs)
                    return {
                        "status": "unavailable",
                        "summary": summary,
                        "items": [{"register": register_name, "source": source} for source, register_name in page],
                        "total": total,
                        "returned": len(page),
                        "has_more": total > len(page),
                        "_meta": {
                            "source": "live",
                            "reason": (
                                "main_index_capability_missing_and_extension_modules_unreadable"
                                if cfe_modules_unreadable
                                else "main_index_capability_missing"
                            ),
                            "partial": True,
                            "extension_modules_scanned": len(cfe_modules_scanned),
                            **(
                                {"extension_modules_unreadable": cfe_modules_unreadable}
                                if cfe_modules_unreadable
                                else {}
                            ),
                            **({"cfe_posting_replacement": cfe_replacement_meta} if cfe_replacement_meta else {}),
                        },
                    }
                sec = _unavailable("capability_missing")
                suppressed_pairs = _profile_movement_pairs(suppressed_main)
                if suppressed_pairs:
                    sec.setdefault("summary", {})["main_code_registers_suppressed_by_cfe"] = len(suppressed_pairs)
                if cfe_replacement_meta:
                    sec["_meta"]["cfe_posting_replacement"] = cfe_replacement_meta
                if cfe_modules_unreadable:
                    sec["_meta"].update(
                        {
                            "reason": "main_index_capability_missing_and_extension_modules_unreadable",
                            "partial": True,
                            "extension_modules_scanned": len(cfe_modules_scanned),
                            "extension_modules_unreadable": cfe_modules_unreadable,
                        }
                    )
                found = _live_posting_signal(object_name, index_prefilter=True)
                if found:
                    # Флаг кладём В SUMMARY — там же, где он лежит в нормальной ветке
                    # (рецепты ведут агента именно в registers.summary; два разных места для
                    # одного сигнала = он не будет найден). code_registers НЕ добавляем:
                    # таблицы движений нет, заявлять «0 регистров» мы не вправе.
                    sec.setdefault("summary", {})["posting_handler_present"] = True
                    sec["hint"] = _build_posting_hint(
                        object_name, found[0], found[1], profile=True, interceptors=found[2]
                    )
                return sec
            suppressible_main_rows = (
                _main_handler_only_movement_keys(object_name)
                if cfe_replacement_meta and not cfe_replacement_meta["main_handler_continuation_visible"]
                else set()
            )
            rows = _merge_movement_rows(rows, live_alias_movements)
            rows, suppressed_main, replacement_meta = _apply_cfe_posting_replacement(
                rows, cfe_interceptors, suppressible_main_rows
            )
            # items = the ONE main list (all movement targets, code first) so total/returned/
            # has_more are self-consistent; the per-source breakdown lives in summary (R5 #6).
            # Helper rows intentionally preserve provenance (main and CFE may both write the
            # same register and carry different ``file`` values). The compact profile drops
            # ``file``, however, so such rows would become indistinguishable and inflate its
            # summary. Collapse only that lossy representation, preserving stable display case.
            ordered = _profile_movement_pairs(rows)
            by_source: dict[str, list[str]] = {"code": [], "erp_mechanism": [], "manager_table": [], "adapted": []}
            for src, n in ordered:
                by_source.setdefault(src, []).append(n)
            summary = {
                "code_registers": len(by_source["code"]),
                "erp_mechanisms": len(by_source["erp_mechanism"]),
                "manager_tables": len(by_source["manager_table"]),
                "adapted_registers": len(by_source["adapted"]),
            }
            suppressed_pairs = _profile_movement_pairs(suppressed_main)
            if suppressed_pairs:
                summary["main_code_registers_suppressed_by_cfe"] = len(suppressed_pairs)
            total = len(ordered)
            page = ordered[: max(0, int(limit))]
            items = [{"register": n, "source": s} for s, n in page]
            section = {
                "status": "unavailable" if cfe_modules_unreadable else ("empty" if total == 0 else "ok"),
                "summary": summary,
                "items": items,
                "total": total,
                "returned": len(items),
                "has_more": total > len(page),
                "_meta": {
                    "source": "mixed" if main_alias_movements or cfe_modules_scanned else "index",
                    **({"extension_modules_scanned": len(cfe_modules_scanned)} if cfe_modules_scanned else {}),
                    **(
                        {
                            "reason": "extension_modules_unreadable",
                            "partial": True,
                            "extension_modules_unreadable": cfe_modules_unreadable,
                        }
                        if cfe_modules_unreadable
                        else {}
                    ),
                    **({"cfe_posting_replacement": replacement_meta} if replacement_meta else {}),
                },
            }
            # get_object_profile — ДЕФОЛТНЫЙ маршрут агента, а он читает reader напрямую и
            # нуджа из find_register_movements не видит. Дублируем сигнал сюда, иначе рецепт
            # «проведение» продолжит уводить в ложный вывод «документ непроводим».
            # ОБЕ половины сигнала проверяет _live_posting_signal — по ОДНОМУ живому телу и той
            # же проверкой, что в find_register_movements: доверять индексу нельзя ни в одной
            # половине (в methods попадает закомментированная процедура, а движения могли
            # дописать в файл после сборки). Чтение гейтится index-отсевом, поэтому на общем
            # пути секция файлов не открывает.
            # Постановку (Posting=Deny) здесь НЕ проверяем и НЕ обещаем: posting живет только в
            # live-XML, а тянуть его в секцию мы не будем. Это зафиксировано в hint и в docs.
            if summary["code_registers"] == 0:
                found = _live_posting_signal(object_name, index_prefilter=True)
                if found:
                    summary["posting_handler_present"] = True
                    section["hint"] = _build_posting_hint(
                        object_name, found[0], found[1], profile=True, interceptors=found[2]
                    )
                elif suppressed_main:
                    section["hint"] = _replacement_hint(suppressed_main) + _POSTING_PROFILE_TAIL.format(doc=object_name)
            elif suppressed_main:
                section["hint"] = _replacement_hint(suppressed_main) + _POSTING_PROFILE_TAIL.format(doc=object_name)
            return section

        def _sec_subscriptions() -> dict:
            if not has_index:
                return _unavailable("no_index")
            rows = idx_reader.get_event_subscriptions_exact(ref)

            def _sub_summary(rs) -> dict:
                # #2: split exact vs universal (empty-source catch-all) so the count
                # matches find_event_subscriptions AND the split is visible to the agent.
                exact = sum(1 for r in rs if r.get("scope") == "exact")
                universal = sum(1 for r in rs if r.get("scope") == "universal")
                return {"subscriptions": len(rs), "exact": exact, "universal": universal}

            return _from_reader_list(
                rows,
                summary_fn=_sub_summary,
                item_fn=lambda r: {
                    "name": r.get("name"),
                    "event": r.get("event"),
                    "handler": r.get("handler"),
                    "scope": r.get("scope"),
                },
            )

        def _sec_roles() -> dict:
            if not has_index:
                return _unavailable("no_index")
            # include_members=True counts member-level grants (Command/Attribute/… under the
            # object) the same way find_roles does, so the aggregate no longer undercounts. The
            # WHERE is anchored (object_name = ref OR LIKE ref || '.%'), so homonyms like
            # ВходящееПисьмоВложение are NOT over-matched — the aggregate is stricter than find_roles.
            rows = idx_reader.get_roles_exact(ref, include_members=True)
            return _from_reader_list(
                rows,
                summary_fn=lambda rs: {"roles": len(rs)},
                item_fn=lambda r: {"role_name": r.get("role_name"), "rights": len(r.get("rights") or [])},
            )

        def _sec_functional_options() -> dict:
            if not has_index:
                return _unavailable("no_index")
            rows = idx_reader.get_functional_options_exact(ref)
            return _from_reader_list(
                rows,
                summary_fn=lambda rs: {"functional_options": len(rs)},
                item_fn=lambda r: {"name": r.get("name")},
            )

        def _sec_flow() -> dict:
            # Heavy, opt-in: analyze_document_flow reads bodies (source=mixed).
            flow = analyze_document_flow(object_name)
            subs = flow.get("event_subscriptions") or []
            movements = flow.get("register_movements") or {}
            jobs = flow.get("related_scheduled_jobs") or []
            code_regs = (movements.get("code_registers") or []) if isinstance(movements, dict) else []
            summary = {
                "event_subscriptions": len(subs),
                "code_registers": len(code_regs),
                "related_scheduled_jobs": len(jobs),
                "is_postable": flow.get("is_postable"),
            }
            items = [{"event": s.get("event"), "handler": s.get("handler")} for s in subs[: max(0, int(limit))]]
            return {
                "status": "ok",
                "summary": summary,
                "items": items,
                "total": len(subs),
                "returned": len(items),
                "has_more": len(subs) > len(items),
                "_meta": {"source": "mixed"},
            }

        def _sec_code_usages() -> dict:
            if not has_index:
                return _unavailable("no_index")
            cu = find_code_usages(ref, limit=(max(0, int(limit)) * 10) or 100)
            # find_code_usages falls to a LIVE safe_grep when the v13 table is missing
            # (partial=True). Surface that — don't claim 'index' and hide the live cost.
            partial = bool(cu.get("partial"))
            usages = cu.get("usages") or []
            total = cu.get("total", len(usages))
            items = [
                {"path": u.get("path"), "line": u.get("line"), "kind": u.get("kind")}
                for u in usages[: max(0, int(limit))]
            ]
            meta = {"source": "live" if partial else "index", "truncated": bool(cu.get("truncated"))}
            if partial:
                meta["partial"] = True
                hint = (cu.get("_meta") or {}).get("hint")
                if hint:
                    meta["fallback_reason"] = hint
            return {
                "status": "empty" if total == 0 else "ok",
                "summary": {"total": total, "by_kind": cu.get("by_kind") or {}, "partial": partial},
                "items": items,
                "total": total,
                "returned": len(items),
                "has_more": total > len(items),
                "_meta": meta,
            }

        _builders = {
            "structure": _sec_structure,
            "modules": _sec_modules,
            "registers": _sec_registers,
            "subscriptions": _sec_subscriptions,
            "roles": _sec_roles,
            "functional_options": _sec_functional_options,
            "flow": _sec_flow,
            "code_usages": _sec_code_usages,
        }

        sections_out: dict[str, dict] = {}
        meta_sections: list[dict] = []
        for sec_name in wanted:
            builder = _builders.get(sec_name)
            if builder is None:
                continue
            s_t0 = _time_prof.monotonic()
            try:
                sec = builder()
            except Exception as exc:  # per-section isolation — never roll up to the profile
                sec = {
                    "status": "error",
                    "summary": {},
                    "items": [],
                    "total": 0,
                    "returned": 0,
                    "has_more": False,
                    "_meta": {"source": "unknown", "error": f"{type(exc).__name__}: {exc}"},
                }
            sec.setdefault("_meta", {})
            sec["_meta"]["elapsed_ms"] = _ms(s_t0)
            # Surface item-list truncation honestly: has_more (preview sliced by `limit`)
            # OR any section-specific truncation flag (e.g. code_usages reader cap). Set on
            # the section _meta so BOTH the section contract and the trace below agree.
            sec["_meta"]["truncated"] = bool(sec["_meta"].get("truncated")) or bool(sec.get("has_more"))
            sections_out[sec_name] = sec
            meta_sections.append(
                {
                    "name": sec_name,
                    "elapsed_ms": sec["_meta"]["elapsed_ms"],
                    "source": sec["_meta"].get("source", "unknown"),
                    "status": sec.get("status", "unknown"),
                    "items_count": len(sec.get("items") or []),
                    "truncated": bool(sec["_meta"].get("truncated")),
                }
            )

        return {
            "object_name": object_name,
            "category": category,
            "sections": sections_out,
            "_meta": {
                "identity_source": identity_source,
                "extension_visibility": extension_visibility,
                "total_elapsed_ms": _ms(prof_t0),
                "sections": meta_sections,
            },
        }

    # ── Business-process helpers ─────────────────────────────────

    _event_sub_lazy = LazyList()

    def _build_event_subscriptions() -> list[dict]:
        files = glob_files_fn("**/EventSubscriptions/**/*.xml")
        files.extend(glob_files_fn("**/EventSubscriptions/**/*.mdo"))
        files = list(dict.fromkeys(files))
        result: list[dict] = []
        for f in files:
            try:
                content = read_file_fn(f)
            except Exception:
                continue
            parsed = parse_event_subscription_xml(content)
            if parsed is None:
                continue
            handler = parsed["handler"]
            parts = handler.rsplit(".", 1)
            handler_procedure = parts[-1] if parts else handler
            handler_module = ""
            if len(parts) > 1:
                module_part = parts[0]
                if module_part.startswith("CommonModule."):
                    module_part = module_part[len("CommonModule.") :]
                handler_module = module_part
            result.append(
                {
                    "name": parsed["name"],
                    "synonym": parsed["synonym"],
                    "source_types": parsed["source_types"],
                    "source_count": len(parsed["source_types"]),
                    "event": parsed["event"],
                    "handler": handler,
                    "handler_module": handler_module,
                    "handler_procedure": handler_procedure,
                    "file": f,
                }
            )
        return result

    def _ensure_event_subscriptions() -> list[dict]:
        return _event_sub_lazy.ensure(_build_event_subscriptions)

    def find_event_subscriptions(
        object_name: str = "",
        custom_only: bool = False,
        event_filter: list[str] | str | None = None,
        limit: int | None = None,
    ) -> list[dict] | dict:
        """Find event subscriptions, optionally filtered by object name and/or event.
        Shows what fires when an object is written/posted/deleted.
        Uses SQLite index when available (instant), falls back to XML parsing.

        Args:
            object_name: Имя объекта. Матчинг EXACT-С-ФОЛБЭКОМ (v1.28.0): сперва точное
                         совпадение с ИМЕННОЙ частью типа-источника; если точных нет —
                         подстрочный фолбэк (поиск по фрагменту). Полное имя больше НЕ
                         цепляет более длинные омонимы (X не тянет XПрисоединенныеФайлы).
                         С ЯВНЫМ префиксом ('Документ.X') матчинг canonical и category-AWARE
                         (Document.X и Catalog.X не смешиваются), фолбэка нет. Голое имя —
                         category-blind. Пустое значение = вернуть все.
                         Universal-подписки (пустой source_types) включаются всегда.
                         ПРИ НЕПУСТОМ object_name каждая строка несет scope:
                         exact | partial | universal (без object_name — прежний компактный
                         список БЕЗ scope и без source_types).
                         event_filter применяется ПОСЛЕ классификации.
            custom_only: If True, return only subscriptions whose name starts
                         with a detected custom prefix (auto-detected from codebase).
            event_filter: List of event substrings (case-insensitive) — отбор
                          по полю event. None = без фильтра. ['BeforeWrite']
                          вернёт все подписки, у которых event содержит 'beforewrite'.
                          Допустима **одна строка** ('BeforeWrite') — она будет
                          автоматически обёрнута в [event_filter] (типичная ошибка
                          агентов: голая строка раньше итерировалась по символам
                          и матчила ВСЕ события).
            limit: Если задан, возврат становится top-level dict
                   {"subscriptions", "total", "returned", "has_more"}. Если None
                   (default) — возвращается list[dict] (контракт прежний).

        Returns:
            Default (limit is None): list[dict] of subscriptions.
            With limit: dict {"subscriptions": [...], "total": N, "returned": K,
                              "has_more": bool}."""
        # Явный префикс (Документ./Document./Справочник.) → canonical ref: матчинг станет
        # category-AWARE и сойдётся с get_object_profile (он всегда ходит по canonical ref).
        # Голое имя оставляем category-blind — обратная совместимость.
        object_ref = ""
        if object_name:
            from rlm_tools_bsl.bsl_xml_parsers import canonicalize_type_ref as _ctr

            object_name = object_name.strip()  # обе ветки одинаково терпимы к паддингу
            canon, _forms = _normalize_object_ref(object_name)
            # ``_ctr(canon)`` — гейт РАСПОЗНАННОСТИ префикса: _normalize_object_ref при неудаче
            # канонизации отдаёт вход ВЕРБАТИМ, поэтому одной проверки "." мало. Нераспознанный
            # dotted-ввод ('Последовательность.Партии', 'РегламентноеЗадание.X') стал бы заведомо
            # несопоставимым object_ref, а у category-aware ветки НЕТ partial-фолбэка — выдача
            # схлопнулась бы до одних universal. Не распознали → обычный матчинг по имени.
            canonical_ref = _ctr(canon) if canon else ""
            if canonical_ref and "." in canonical_ref:
                # Короткое имя берём из canonical suffix, а НЕ из регистрозависимого
                # _strip_meta_prefix: "документ.X" канонизируется (casefold), а
                # _strip_meta_prefix оставил бы префикс на месте. Пустой suffix известного
                # префикса остается пустым обзором при любом регистре букв.
                object_name = canonical_ref.split(".", 1)[1]
                if object_name:
                    object_ref = canonical_ref
            else:
                object_name = _strip_meta_prefix(object_name)

        # Normalize event_filter: голая строка → [строка]. Иначе Python итерирует
        # по символам ('BeforeWrite' → ['B','e',...]) и каждый одно-символьный
        # substring-matcher ловит почти все события — фильтр де-факто игнорируется.
        if isinstance(event_filter, str):
            event_filter = [event_filter] if event_filter else None

        # --- Fast path: SQLite index ---
        result: list[dict] | None = None
        if idx_reader is not None:
            idx_result = idx_reader.get_event_subscriptions(
                object_name, event_filter=event_filter, object_ref=object_ref
            )
            if idx_result is not None:
                if custom_only:
                    prefixes = _ensure_prefixes()
                    if prefixes:
                        idx_result = [s for s in idx_result if any(s["name"].lower().startswith(p) for p in prefixes)]
                result = idx_result

        if result is None:
            all_subs = _ensure_event_subscriptions()

            if not object_name and not object_ref:
                # Return without source_types to keep output compact
                result = [{k: v for k, v in s.items() if k != "source_types"} for s in all_subs]
            else:
                # Тот же матчер, что в IndexReader.get_event_subscriptions (см. там развёрнутый
                # комментарий): exact по именной части (или по canonical ref при явном префиксе),
                # подстрока — только когда точных нет, universal — всегда. Ветки индекса и live
                # ОБЯЗАНЫ совпадать: иначе одна конфигурация ответит по-разному до и после сборки
                # индекса. event_filter (ниже) применяется ПОСЛЕ классификации — как в reader.
                from rlm_tools_bsl.bsl_xml_parsers import canonicalize_type_ref as _ctr

                name_lower = object_name.lower()
                ref_lower = object_ref.lower()
                exact_hits: list[dict] = []
                partial_hits: list[dict] = []
                universal: list[dict] = []
                for s in all_subs:
                    types = s["source_types"]
                    if not types:
                        universal.append({**dict(s), "scope": "universal"})
                        continue
                    if ref_lower:
                        if any(t and _ctr(t).lower() == ref_lower for t in types):
                            exact_hits.append({**dict(s), "scope": "exact"})
                        continue  # типизированный вход — без partial-фолбэка
                    names = [(t.split(".", 1)[1] if "." in t else t).lower() for t in types if t]
                    if any(n == name_lower for n in names):
                        exact_hits.append({**dict(s), "scope": "exact"})
                    elif any(name_lower in n for n in names):
                        partial_hits.append({**dict(s), "scope": "partial"})
                result = (exact_hits if exact_hits else partial_hits) + universal

            if event_filter:
                evs_lower = [e.lower() for e in event_filter]
                result = [s for s in result if any(ev in (s.get("event", "") or "").lower() for ev in evs_lower)]

            if custom_only:
                prefixes = _ensure_prefixes()
                if prefixes:
                    result = [s for s in result if any(s["name"].lower().startswith(p) for p in prefixes)]

        if limit is None:
            return result

        # Paginated mode — return top-level dict.
        total = len(result)
        page = result[: max(0, int(limit))]
        return {
            "subscriptions": page,
            "total": total,
            "returned": len(page),
            "has_more": total > len(page),
        }

    _sched_job_lazy = LazyList()

    def _build_scheduled_jobs() -> list[dict]:
        files = glob_files_fn("**/ScheduledJobs/**/*.xml")
        files.extend(glob_files_fn("**/ScheduledJobs/**/*.mdo"))
        files = list(dict.fromkeys(files))
        result: list[dict] = []
        for f in files:
            try:
                content = read_file_fn(f)
            except Exception:
                continue
            parsed = parse_scheduled_job_xml(content)
            if parsed is None:
                continue
            method = parsed["method_name"]
            parts = method.rsplit(".", 1)
            handler_procedure = parts[-1] if parts else method
            handler_module = ""
            if len(parts) > 1:
                module_part = parts[0]
                if module_part.startswith("CommonModule."):
                    module_part = module_part[len("CommonModule.") :]
                handler_module = module_part
            result.append(
                {
                    "name": parsed["name"],
                    "synonym": parsed["synonym"],
                    "method_name": method,
                    "handler_module": handler_module,
                    "handler_procedure": handler_procedure,
                    "use": parsed["use"],
                    "predefined": parsed["predefined"],
                    "restart_on_failure": parsed["restart_on_failure"],
                    "file": f,
                }
            )
        return result

    def _ensure_scheduled_jobs() -> list[dict]:
        return _sched_job_lazy.ensure(_build_scheduled_jobs)

    def find_scheduled_jobs(name: str = "") -> list[dict]:
        """Find scheduled (background) jobs, optionally filtered by name.
        Uses SQLite index when available (instant), falls back to XML parsing.

        Args:
            name: Name substring to filter by (case-insensitive). Empty = all.

        Returns: list of dicts with name, synonym, method_name,
                 handler_module, handler_procedure, use, predefined, file."""
        if name:
            name = _strip_meta_prefix(name)

        # --- Fast path: SQLite index ---
        if idx_reader is not None:
            idx_result = idx_reader.get_scheduled_jobs(name)
            if idx_result is not None:
                return idx_result

        all_jobs = _ensure_scheduled_jobs()
        if not name:
            return all_jobs
        name_lower = name.lower()
        return [j for j in all_jobs if name_lower in j["name"].lower()]

    # ── Integration metadata helpers ─────────────────────────────

    def find_http_services(name: str = "") -> list[dict]:
        """Find HTTP services, optionally filtered by name.
        Uses SQLite index when available, falls back to XML parsing.

        Args:
            name: Name substring to filter by (case-insensitive). Empty = all.

        Returns: list of dicts with name, root_url, templates, file."""
        if name:
            name = _strip_meta_prefix(name)

        # Fast path: SQLite index
        if idx_reader is not None:
            idx_result = idx_reader.get_http_services(name)
            if idx_result is not None:
                return idx_result

        # Fallback: glob + parse
        from rlm_tools_bsl.bsl_xml_parsers import parse_http_service_xml

        files = glob_files_fn("HTTPServices/**/*.xml") + glob_files_fn("HTTPServices/**/*.mdo")
        results: list[dict] = []
        for fp in files:
            content = read_file_fn(fp)
            if not content:
                continue
            parsed = parse_http_service_xml(content)
            if parsed and (not name or name.lower() in parsed["name"].lower()):
                parsed["file"] = fp if not os.path.isabs(fp) else os.path.relpath(fp, base_path).replace("\\", "/")
                results.append(parsed)
        return results

    def find_web_services(name: str = "") -> list[dict]:
        """Find web services (SOAP), optionally filtered by name.
        Uses SQLite index when available, falls back to XML parsing.

        Args:
            name: Name substring to filter by (case-insensitive). Empty = all.

        Returns: list of dicts with name, namespace, operations, file."""
        if name:
            name = _strip_meta_prefix(name)

        # Fast path: SQLite index
        if idx_reader is not None:
            idx_result = idx_reader.get_web_services(name)
            if idx_result is not None:
                return idx_result

        # Fallback: glob + parse
        from rlm_tools_bsl.bsl_xml_parsers import parse_web_service_xml

        files = glob_files_fn("WebServices/**/*.xml") + glob_files_fn("WebServices/**/*.mdo")
        results: list[dict] = []
        for fp in files:
            content = read_file_fn(fp)
            if not content:
                continue
            parsed = parse_web_service_xml(content)
            if parsed and (not name or name.lower() in parsed["name"].lower()):
                parsed["file"] = fp if not os.path.isabs(fp) else os.path.relpath(fp, base_path).replace("\\", "/")
                results.append(parsed)
        return results

    def find_xdto_packages(name: str = "") -> list[dict]:
        """Find XDTO packages, optionally filtered by name.
        Uses SQLite index when available, falls back to XML parsing.

        Args:
            name: Name substring to filter by (case-insensitive). Empty = all.

        Returns: list of dicts with name, namespace, types, file."""
        if name:
            name = _strip_meta_prefix(name)

        # Fast path: SQLite index
        if idx_reader is not None:
            idx_result = idx_reader.get_xdto_packages(name)
            if idx_result is not None:
                return idx_result

        # Fallback: glob + parse
        from rlm_tools_bsl.bsl_xml_parsers import parse_xdto_package_xml, parse_xdto_types

        files = glob_files_fn("XDTOPackages/**/*.xml") + glob_files_fn("XDTOPackages/**/*.mdo")
        results: list[dict] = []
        for fp in files:
            content = read_file_fn(fp)
            if not content:
                continue
            parsed = parse_xdto_package_xml(content)
            if parsed and (not name or name.lower() in parsed["name"].lower()):
                # For EDT: check sibling Package.xdto
                if fp.endswith(".mdo"):
                    xdto_path = os.path.join(os.path.dirname(fp), "Package.xdto")
                    try:
                        xdto_content = read_file_fn(xdto_path)
                    except Exception:
                        xdto_content = None
                    if xdto_content:
                        parsed["types"] = parse_xdto_types(xdto_content)
                parsed["file"] = fp if not os.path.isabs(fp) else os.path.relpath(fp, base_path).replace("\\", "/")
                results.append(parsed)
        return results

    def find_exchange_plan_content(name: str) -> list[dict]:
        """Find exchange plan content (objects registered for exchange).
        Always parses XML at runtime (no index table).

        Args:
            name: Exchange plan name.

        Returns: list of dicts with ref, auto_record."""
        name = _strip_meta_prefix(name)
        from rlm_tools_bsl.bsl_xml_parsers import parse_exchange_plan_content as _parse_ep

        def _valid_files(pattern: str) -> list[str]:
            """Glob and filter out hint strings."""
            return [f for f in glob_files_fn(pattern) if not f.startswith("[")]

        # EDT: .mdo file of the exchange plan itself (content is inline)
        # CF: Ext/Content.xml
        files = (
            _valid_files(f"ExchangePlans/{name}/*.mdo")
            + _valid_files(f"ExchangePlans/{name}/**/*.mdo")
            + _valid_files(f"ExchangePlans/{name}/**/*.xml")
        )
        if not files:
            # Try wildcard search across all exchange plans
            all_files = _valid_files("ExchangePlans/**/*.xml") + _valid_files("ExchangePlans/**/*.mdo")
            name_lower = name.lower()
            files = [f for f in all_files if name_lower in f.lower()]

        results: list[dict] = []
        seen_refs: set[str] = set()
        for fp in files:
            content = read_file_fn(fp)
            if not content:
                continue
            items = _parse_ep(content)
            for item in items:
                if item["ref"] not in seen_refs:
                    results.append(item)
                    seen_refs.add(item["ref"])
        return results

    _postable_memo: dict[str, dict] = {}

    def _check_document_postable(document_name: str) -> dict:
        """Live read of Document.posting via parse_object_xml.
        Returns {"is_postable": bool, "posting": "Allow|Deny|UseSelectively|None"}
        or empty dict when posting cannot be determined.

        UseSelectively means part of the document types post — НЕ ставим is_postable=False.
        Только Deny → is_postable=False.

        Результат мемоизирован на сессию: функцию зовут два нуджа подряд
        (_maybe_add_postability_hint и _maybe_add_posting_handler_hint), а parse_object_xml
        каждый раз заново парсит XML (кешируется лишь чтение файла). Для документа с пустым
        code_registers, но непустыми manager_tables — основной кейс — это был бы двойной
        разбор. Postability документа внутри сессии не меняется.
        """
        memo_key = (document_name or "").lower()
        if memo_key in _postable_memo:
            return _postable_memo[memo_key]

        def _compute() -> dict | None:
            """dict — стабильный результат (мемоизируем); None — транзиентный сбой (НЕ мемоизируем)."""
            try:
                meta = parse_object_xml(f"Documents/{document_name}")
            except Exception:
                # Сбой чтения/разбора XML может быть транзиентным. Закешировав его, мы бы
                # заглушили postability документа до КОНЦА СЕССИИ (до мемоизации каждый вызов
                # пробовал заново). Отдаём прежний пустой контракт, но в кеш не кладём.
                return None
            if not isinstance(meta, dict) or meta.get("object_type") != "Document":
                return {}  # стабильный факт: это не документ
            posting = (meta.get("posting") or "").strip()
            if not posting:
                return {"posting": None}
            is_postable = posting.lower() != "deny"
            return {"posting": posting, "is_postable": is_postable}

        res = _compute()
        if res is None:
            return {}
        _postable_memo[memo_key] = res
        return res

    def _extract_live_procedure_code(module_body: str, method_name: str) -> str:
        """Return one procedure/function from live BSL with comments and strings removed.

        The extractor is deliberately small and shared by the posting analyzer and CFE
        replacement handling.  Keeping a single implementation matters here: a visible
        ``ПродолжитьВызов`` must be evaluated in exactly the same lexical body that feeds
        the agent-facing posting facts, never in comments, strings, or a neighbouring
        procedure.
        """
        code = _live_code_only(module_body or "")
        decl_re = (
            _POSTING_HANDLER_DECL_RE
            if method_name.casefold() == "обработкапроведения"
            else re.compile(
                r"^\s*(?:Процедура|Функция|Procedure|Function)\s+" + re.escape(method_name) + r"\b",
                re.IGNORECASE | re.MULTILINE,
            )
        )
        match = decl_re.search(code)
        if not match:
            return ""
        tail = code[match.start() :]
        end = _PROC_END_RE.search(tail, 1)
        return tail[: end.end()] if end else tail

    def _posting_interceptors_for_module(rel_path: str, body: str) -> list[dict]:
        """Live CFE annotations targeting ``ОбработкаПроведения`` in one exact module."""
        if rel_path not in _extension_paths_set:
            return []
        result: list[dict] = []
        lines = body.splitlines()
        for pos, line in enumerate(lines):
            annotation_match = _CFE_POSTING_ANNOTATION_RE.match(line)
            if not annotation_match:
                continue
            for candidate in lines[pos + 1 :]:
                stripped = candidate.strip()
                if not stripped or stripped.startswith("//") or stripped.startswith(("#", "&")):
                    continue
                proc_match = _ANY_PROC_DECL_RE.match(candidate)
                if proc_match:
                    result.append(
                        {
                            "path": rel_path,
                            "method": proc_match.group(1),
                            "body": body,
                            "annotation": annotation_match.group(1),
                        }
                    )
                break
        return result

    def _has_direct_main_continuation(module_body: str, procedure_code: str) -> bool:
        """Whether the replacement directly calls the platform continuation primitive.

        A qualified homonym (``Service.ProceedWithCall``) is an ordinary method call, and a
        module-local declaration shadows the global primitive.  Both must stay false even
        though the token itself is visible in the exact replacement procedure.
        """
        declared_names = {
            match.group(1).casefold()
            for line in _live_code_only(module_body or "").splitlines()
            if (match := _ANY_PROC_DECL_RE.match(line))
        }
        for line in (procedure_code or "").splitlines():
            if _ANY_PROC_DECL_RE.match(line):
                continue
            for match in _CONTINUE_MAIN_RE.finditer(line):
                if line[: match.start()].rstrip().endswith("."):
                    continue
                if match.group(1).casefold() in declared_names:
                    continue
                return True
        return False

    def _live_movement_names(code: str) -> set[str]:
        return {
            match.group(1).casefold()
            for match in _MOVEMENTS_LIVE_RE.finditer(code or "")
            if match.group(1).casefold() not in _MOVEMENT_METHOD_NOISE
        }

    def _live_main_object_module_paths(document_name: str) -> list[str]:
        """Exact main ObjectModule paths from the snapshot plus the current CF/EDT tree."""
        _ensure_index()
        target = (document_name or "").casefold()
        candidates = [
            rel_path
            for rel_path, info in _index_state
            if rel_path not in _extension_paths_set
            and (info.category or "") == "Documents"
            and info.module_type == "ObjectModule"
            and (info.object_name or "").casefold() == target
        ]
        candidate_keys = {path.replace("\\", "/").casefold() for path in candidates}

        documents_root = _base_path_resolved / "Documents"
        direct_object_dir = documents_root / document_name
        object_dirs = [direct_object_dir]
        try:
            if not direct_object_dir.is_dir():
                object_dirs.extend(
                    child for child in documents_root.iterdir() if child.is_dir() and child.name.casefold() == target
                )
        except (OSError, PermissionError):
            pass

        object_dir_keys: set[str] = set()
        for object_dir in object_dirs:
            try:
                object_dir_key = os.path.normcase(os.path.abspath(str(object_dir.resolve())))
            except (OSError, PermissionError):
                continue
            if object_dir_key in object_dir_keys:
                continue
            object_dir_keys.add(object_dir_key)
            for full_path in (object_dir / "Ext" / "ObjectModule.bsl", object_dir / "ObjectModule.bsl"):
                try:
                    full_path = full_path.resolve()
                    if not full_path.is_file():
                        continue
                    rel_path = os.path.relpath(str(full_path), base_path).replace("\\", "/")
                except (OSError, PermissionError, ValueError):
                    continue
                rel_key = rel_path.casefold()
                if rel_key not in candidate_keys:
                    candidates.append(rel_path)
                    candidate_keys.add(rel_key)
        return candidates

    _live_main_object_body_cache: dict[str, str | None] = {}

    def _live_main_object_module_body(rel_path: str) -> str | None:
        """Session-stable main ObjectModule body shared by the exact live posting passes."""
        key = rel_path.replace("\\", "/").casefold()
        if key not in _live_main_object_body_cache:
            try:
                _live_main_object_body_cache[key] = _ext_read_file(rel_path)
            except Exception:
                _live_main_object_body_cache[key] = None
        return _live_main_object_body_cache[key]

    def _main_handler_only_movement_keys(
        document_name: str,
        bodies: dict[str, str] | None = None,
    ) -> set[tuple[str, str]]:
        """Live-proven ``(file, register)`` rows confined to main posting handlers.

        The SQLite row has module provenance but no procedure provenance because the
        builder extracts ``Движения.X`` from the whole ObjectModule.  Suppressing every
        main-file row on ``&Вместо`` would therefore hide helpers that the replacement may
        call directly.  A row is suppressible only when its live register reference occurs
        in ``ОбработкаПроведения`` and nowhere else in that main ObjectModule.
        """
        suppressible: set[tuple[str, str]] = set()
        for rel_path in _live_main_object_module_paths(document_name):
            body = (bodies or {}).get(rel_path)
            if body is None:
                body = _live_main_object_module_body(rel_path)
                if body is None:
                    continue
            full_code = _live_code_only(body)
            handler_code = _extract_live_procedure_code(body, "ОбработкаПроведения")
            if not handler_code:
                continue
            handler_pos = full_code.find(handler_code)
            if handler_pos < 0:
                continue
            outside_code = full_code[:handler_pos] + full_code[handler_pos + len(handler_code) :]
            handler_only = _live_movement_names(handler_code) - _live_movement_names(outside_code)
            path_key = rel_path.replace("\\", "/").casefold()
            suppressible.update((path_key, register_name) for register_name in handler_only)
        return suppressible

    def _apply_cfe_posting_replacement(
        movement_rows: list[dict],
        interceptors: list[dict],
        suppressible_main_rows: set[tuple[str, str]] | None = None,
    ) -> tuple[list[dict], list[dict], dict | None]:
        """Separate main code rows suppressed by a live CFE posting replacement.

        ``&Вместо``/``&ИзменениеИКонтроль`` replace the main handler.  Main-handler rows
        remain possible only when every replacement body visibly contains the direct
        platform call ``ПродолжитьВызов`` / ``ProceedWithCall``.  Comments, strings,
        qualified homonyms and other procedures do not count.

        Returns ``(active_rows, suppressed_main_rows, public_replacement_meta)``.  CFE
        rows are never removed.  Non-code sources (ERP mechanisms/manager tables) retain
        their historical contract because this pass has evidence only about ObjectModule
        code rows.
        """
        replacements = [
            item for item in interceptors if str(item.get("annotation") or "").casefold() in _CFE_POSTING_REPLACEMENTS
        ]
        if not replacements:
            return list(movement_rows), [], None

        public_items: list[dict] = []
        continuation_visible = True
        for item in replacements:
            procedure_code = _extract_live_procedure_code(str(item.get("body") or ""), str(item.get("method") or ""))
            continues_main = _has_direct_main_continuation(str(item.get("body") or ""), procedure_code)
            continuation_visible = continuation_visible and continues_main
            public_items.append(
                {
                    "annotation": item.get("annotation") or "",
                    "method": item.get("method") or "",
                    "file": item.get("path") or "",
                    "continues_main": continues_main,
                }
            )

        public_meta = {
            "main_handler_continuation_visible": continuation_visible,
            "interceptors": public_items,
        }
        if continuation_visible:
            return list(movement_rows), [], public_meta

        active: list[dict] = []
        suppressed: list[dict] = []
        for row in movement_rows:
            source = str(row.get("source") or "code").casefold()
            path = str(row.get("file") or "")
            row_key = (
                path.replace("\\", "/").casefold(),
                str(row.get("register_name") or row.get("name") or "").casefold(),
            )
            if source == "code" and path not in _extension_paths_set and row_key in (suppressible_main_rows or set()):
                suppressed.append(row)
            else:
                active.append(row)
        return active, suppressed, public_meta

    def _replacement_hint(suppressed_rows: list[dict]) -> str:
        names = sorted(
            {
                str(row.get("register_name") or row.get("name") or "")
                for row in suppressed_rows
                if row.get("register_name") or row.get("name")
            },
            key=str.casefold,
        )
        listed = ", ".join(names)
        return (
            "CFE-перехват &Вместо/&ИзменениеИКонтроль заменяет ОбработкаПроведения, а прямой "
            "ПродолжитьВызов/ProceedWithCall хотя бы в одной точной процедуре замены не найден. Поэтому ссылки main-handler "
            f"({listed}) не включены в выполняемые code_registers и сохранены отдельно в "
            "suppressed_main_code_registers как статический снимок. Движения из CFE в "
            "code_registers остаются действующими кандидатами; при динамическом продолжении "
            "проверь тело указанного в _meta.cfe_posting_replacement перехвата. "
        )

    def _live_main_alias_movements(document_name: str) -> list[dict]:
        """Read-time bridge for English ``RegisterRecords.X`` on old SQLite builds.

        The persisted extractor remains unchanged in 1.28.0, so no builder-version
        migration is needed. Only the exact main ObjectModule is inspected and only
        English collection references are added; Russian rows keep their index contract.
        """
        rows: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for path in _live_main_object_module_paths(document_name):
            body = _live_main_object_module_body(path)
            if body is None:
                continue
            code = _live_code_only(body)
            if not re.search(r"\bRegisterRecords\s*\.", code, re.IGNORECASE):
                continue
            for movement in _MOVEMENTS_LIVE_RE.finditer(code):
                if not movement.group(0).lstrip().casefold().startswith("registerrecords"):
                    continue
                register_name = movement.group(1)
                if register_name.casefold() in _MOVEMENT_METHOD_NOISE:
                    continue
                key = (path.replace("\\", "/").casefold(), register_name.casefold())
                if key in seen:
                    continue
                seen.add(key)
                rows.append({"register_name": register_name, "source": "code", "file": path})
        return rows

    def _live_extension_movements(
        document_name: str,
    ) -> tuple[list[dict], list[str], list[str], list[dict]]:
        """Direct ``Движения.X`` rows from exact CFE ObjectModules, in reader shape.

        The persistent index belongs to the main configuration; nearby extensions are
        side-loaded into ``_index_state`` only for the live helper session. Consequently
        an authoritative ``[]`` from ``IndexReader.get_register_movements`` says nothing
        about CFE modules. Scan only exact extension ObjectModules for the requested
        document and merge these rows additively with the main-index answer.

        Returns ``(movement_rows, modules_scanned, modules_unreadable, posting_interceptors)``.
        A path enters
        ``modules_scanned`` only after a successful read; callers must expose a non-empty
        ``modules_unreadable`` as partial rather than treating the known rows as complete.
        Comments and strings are stripped because this is a live read and therefore does
        not need to inherit the builder's documented raw-regex limitation.
        """
        if not _ext_paths_raw:
            return [], [], [], []
        _ensure_index()
        target = (document_name or "").casefold()
        candidates = sorted(
            rel_path
            for rel_path, info in _index_state
            if rel_path in _extension_paths_set
            and (info.category or "") == "Documents"
            and info.module_type == "ObjectModule"
            and (info.object_name or "").casefold() == target
        )
        rows: list[dict] = []
        scanned: list[str] = []
        unreadable: list[str] = []
        posting_interceptors: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for path in candidates:
            try:
                raw_body = _ext_read_file(path)
                code = _live_code_only(raw_body)
            except Exception:
                unreadable.append(path)
                continue
            scanned.append(path)
            posting_interceptors.extend(_posting_interceptors_for_module(path, raw_body))
            for line in code.splitlines():
                for match in _MOVEMENTS_LIVE_RE.finditer(line):
                    register_name = match.group(1)
                    if register_name.casefold() in _MOVEMENT_METHOD_NOISE:
                        continue
                    key = (register_name.casefold(), path.replace("\\", "/").casefold())
                    if key in seen:
                        continue
                    seen.add(key)
                    rows.append(
                        {
                            "register_name": register_name,
                            "source": "code",
                            "file": path,
                        }
                    )
        return rows, scanned, unreadable, posting_interceptors

    def _merge_movement_rows(base_rows: list[dict], extra_rows: list[dict]) -> list[dict]:
        """Add movement rows without changing the existing reader row semantics/order."""
        merged = list(base_rows)
        seen = {
            (
                str(row.get("register_name") or "").casefold(),
                str(row.get("source") or "").casefold(),
                str(row.get("file") or "").replace("\\", "/").casefold(),
            )
            for row in merged
        }
        for row in extra_rows:
            key = (
                str(row.get("register_name") or "").casefold(),
                str(row.get("source") or "").casefold(),
                str(row.get("file") or "").replace("\\", "/").casefold(),
            )
            if key not in seen:
                seen.add(key)
                merged.append(row)
        return merged

    def find_register_movements(document_name: str, posting_calls_offset: int = 0) -> dict:
        """Find all registers that a document writes to during posting.
        Searches ObjectModule code for 'Движения.RegisterName' pattern.

        Args:
            document_name: Document name (or fragment).
            posting_calls_offset: Zero-based page offset for compact posting facts
                                  (record-set names and overflow call names). Detailed
                                  routes remain capped; use the exact next-page call
                                  from the hint.

        Returns: dict with document, code_registers, modules_scanned. При Posting=Deny
                 всегда добавляются is_postable=False + hint; найденные статические строки
                 сохраняются с явной пометкой, что при проведении они недостижимы."""
        document_name = _strip_meta_prefix(document_name)

        result: dict
        # Fast path: SQLite index
        if idx_reader is not None:
            idx_movements = idx_reader.get_register_movements(document_name)
            if idx_movements is not None:
                idx_movements = _merge_movement_rows(idx_movements, _live_main_alias_movements(document_name))
                (
                    cfe_movements,
                    cfe_modules_scanned,
                    cfe_modules_unreadable,
                    cfe_interceptors,
                ) = _live_extension_movements(document_name)
                idx_movements = _merge_movement_rows(idx_movements, cfe_movements)
                _active, _suppressed, pre_replacement_meta = _apply_cfe_posting_replacement([], cfe_interceptors)
                suppressible_main_rows = (
                    _main_handler_only_movement_keys(document_name)
                    if pre_replacement_meta and not pre_replacement_meta["main_handler_continuation_visible"]
                    else set()
                )
                idx_movements, suppressed_main, replacement_meta = _apply_cfe_posting_replacement(
                    idx_movements, cfe_interceptors, suppressible_main_rows
                )
                result = {
                    "document": document_name,
                    "code_registers": [
                        {"name": m["register_name"], "source": m["source"], "file": m["file"]}
                        for m in idx_movements
                        if m["source"] == "code"
                    ],
                    "modules_scanned": cfe_modules_scanned,
                    "erp_mechanisms": [m["register_name"] for m in idx_movements if m["source"] == "erp_mechanism"],
                    "manager_tables": [m["register_name"] for m in idx_movements if m["source"] == "manager_table"],
                    "adapted_registers": [m["register_name"] for m in idx_movements if m["source"] == "adapted"],
                }
                if suppressed_main:
                    result["suppressed_main_code_registers"] = [
                        {"name": m["register_name"], "source": m["source"], "file": m["file"]} for m in suppressed_main
                    ]
                if replacement_meta:
                    result.setdefault("_meta", {})["cfe_posting_replacement"] = replacement_meta
                if cfe_modules_unreadable:
                    result["partial"] = True
                    result.setdefault("_meta", {}).update(
                        {
                            "reason": "extension_modules_unreadable",
                            "extension_modules_scanned": len(cfe_modules_scanned),
                            "extension_modules_unreadable": cfe_modules_unreadable,
                        }
                    )
                _maybe_add_postability_hint(result, document_name)
                _maybe_add_posting_handler_hint(result, document_name, posting_calls_offset)
                return result

        # Exact document identity must win before the historical substring fallback.
        # ``find_by_type`` caps its fuzzy result at 50, so using it unconditionally can
        # omit an exact CFE ObjectModule appended after many similarly named main modules
        # and silently miss a blocking replacement.  The exact branch is uncapped and
        # includes every main/CFE module; fragments retain the legacy capped lookup.
        _ensure_index()
        exact_document_name = document_name.casefold()
        exact_modules = [
            _info_to_dict(relative_path, info)
            for relative_path, info in _index_state
            if info.category
            and info.category.casefold() == "documents"
            and info.object_name
            and info.object_name.casefold() == exact_document_name
        ]
        modules = exact_modules or find_by_type("Documents", document_name)
        obj_modules = [m for m in modules if m.get("module_type") == "ObjectModule"]

        if not obj_modules:
            result = {
                "document": document_name,
                "code_registers": [],
                "modules_scanned": [],
                "error": f"ObjectModule для документа '{document_name}' не найден",
            }
            _maybe_add_postability_hint(result, document_name)
            return result

        movement_re = _MOVEMENTS_LIVE_RE
        # Keep per-file provenance internally.  The legacy no-index response still
        # collapses duplicate register names below, but replacement semantics must first
        # be able to remove a main row without accidentally removing the same register
        # written by CFE.
        live_code_rows: list[dict] = []
        live_code_by_origin: dict[tuple[str, str], dict] = {}
        cfe_interceptors: list[dict] = []
        modules_scanned: list[str] = []
        extension_modules_scanned: list[str] = []
        extension_modules_unreadable: list[str] = []
        object_module_bodies: dict[str, str] = {}

        for mod in obj_modules:
            path = mod["path"]
            try:
                content = _ext_read_file(path)
            except Exception:
                if path in _extension_paths_set:
                    extension_modules_unreadable.append(path)
                continue
            modules_scanned.append(path)
            object_module_bodies[path] = content
            if path in _extension_paths_set:
                extension_modules_scanned.append(path)
                cfe_interceptors.extend(_posting_interceptors_for_module(path, content))
            for i, line in enumerate(content.splitlines(), 1):
                for m in movement_re.finditer(line):
                    reg_name = m.group(1)
                    # Belt-and-suspenders alongside the lookahead: skip paren-less stop-set names.
                    if reg_name.lower() in _MOVEMENT_METHOD_NOISE:
                        continue
                    origin_key = (reg_name.casefold(), path.replace("\\", "/").casefold())
                    if origin_key not in live_code_by_origin:
                        row = {
                            "name": reg_name,
                            "lines": [],
                            "file": path,
                        }
                        live_code_by_origin[origin_key] = row
                        live_code_rows.append(row)
                    if i not in live_code_by_origin[origin_key]["lines"]:
                        live_code_by_origin[origin_key]["lines"].append(i)

        _active, _suppressed, pre_replacement_meta = _apply_cfe_posting_replacement([], cfe_interceptors)
        suppressible_main_rows = (
            _main_handler_only_movement_keys(document_name, object_module_bodies)
            if pre_replacement_meta and not pre_replacement_meta["main_handler_continuation_visible"]
            else set()
        )
        active_code_rows, suppressed_main, replacement_meta = _apply_cfe_posting_replacement(
            live_code_rows, cfe_interceptors, suppressible_main_rows
        )
        code_registers: dict[str, dict] = {}
        for row in active_code_rows:
            # Preserve the historical no-index shape/dedup: one row per display name,
            # first module wins.  The only new behaviour is that a suppressed main origin
            # is removed before this collapse, so a same-named CFE origin can survive.
            code_registers.setdefault(row["name"], row)

        result = {
            "document": document_name,
            "code_registers": list(code_registers.values()),
            "modules_scanned": modules_scanned,
        }
        if suppressed_main:
            result["suppressed_main_code_registers"] = suppressed_main
        if replacement_meta:
            result.setdefault("_meta", {})["cfe_posting_replacement"] = replacement_meta

        # ── ERP framework fallback ──────────────────────────────
        # Look for ManagerModule to find ERP-style movement definitions
        mgr_modules = [m for m in modules if m.get("module_type") == "ManagerModule"]
        erp_mechanisms: list[str] = []
        manager_tables: list[str] = []
        adapted_registers: list[str] = []

        for mod in mgr_modules:
            mgr_path = mod["path"]
            try:
                mgr_content = _ext_read_file(mgr_path)
            except Exception:
                if mgr_path in _extension_paths_set:
                    extension_modules_unreadable.append(mgr_path)
                continue
            if mgr_path in _extension_paths_set:
                extension_modules_scanned.append(mgr_path)

            # ЗарегистрироватьУчетныеМеханизмы → МеханизмыДокумента.Добавить("X")
            mech_body = read_procedure(mgr_path, "ЗарегистрироватьУчетныеМеханизмы")
            if mech_body:
                mech_re = re.compile(r'МеханизмыДокумента\.Добавить\("(\w+)"\)', re.IGNORECASE)
                for m in mech_re.finditer(mech_body):
                    if m.group(1) not in erp_mechanisms:
                        erp_mechanisms.append(m.group(1))

            # ТекстЗапросаТаблицаXxx function names
            table_re = re.compile(r"(?:Функция|Процедура)\s+ТекстЗапросаТаблица(\w+)\s*\(", re.IGNORECASE)
            for m in table_re.finditer(mgr_content):
                table_name = m.group(1)
                if table_name not in manager_tables:
                    manager_tables.append(table_name)

            # АдаптированныйТекстЗапросаДвиженийПоРегистру → ИмяРегистра = "X"
            adapted_body = read_procedure(mgr_path, "АдаптированныйТекстЗапросаДвиженийПоРегистру")
            if adapted_body:
                reg_re = re.compile(r'ИмяРегистра\s*=\s*"(\w+)"', re.IGNORECASE)
                for m in reg_re.finditer(adapted_body):
                    if m.group(1) not in adapted_registers:
                        adapted_registers.append(m.group(1))

        result["erp_mechanisms"] = erp_mechanisms
        result["manager_tables"] = manager_tables
        result["adapted_registers"] = adapted_registers
        if extension_modules_unreadable:
            result["partial"] = True
            result.setdefault("_meta", {}).update(
                {
                    "reason": "extension_modules_unreadable",
                    "extension_modules_scanned": len(extension_modules_scanned),
                    "extension_modules_unreadable": extension_modules_unreadable,
                }
            )

        _maybe_add_postability_hint(result, document_name)
        _maybe_add_posting_handler_hint(result, document_name, posting_calls_offset)
        return result

    def _maybe_add_postability_hint(result: dict, document_name: str) -> None:
        """If the combined result has no register movements at all,
        live-read Document.posting from XML and annotate the result when posting=Deny.
        Полный итог: code_registers + erp_mechanisms + manager_tables + adapted_registers == [].
        """
        empty = (
            not result.get("code_registers")
            and not result.get("erp_mechanisms")
            and not result.get("manager_tables")
            and not result.get("adapted_registers")
        )
        if not empty:
            return
        info = _check_document_postable(document_name)
        if not info:
            return
        # info contains: posting (str | None) and optionally is_postable (bool)
        if info.get("posting"):
            result["posting"] = info["posting"]
        if info.get("is_postable") is False:
            result["is_postable"] = False
            result["hint"] = (
                "Документ непроводимый (Posting=Deny) — движений регистров нет. "
                "Связь с регистрами ищите через find_event_subscriptions / "
                "регистры сведений с типом источника = документ."
            )

    # РАЗБОР ТЕЛА ДЕЛАЕТ СЕРВЕР, А НЕ АГЕНТ — и это не стилистика, а два подтверждённых отказа.
    # (1) CFE: обработчик может жить ТОЛЬКО в расширении, и тогда handler_path это '../<Ext>/...'.
    #     Такой путь ВНЕ песочницы: generic read_file (helpers._resolve_safe) бросает
    #     PermissionError. Прошлая версия hint велела агенту звать read_file(path) — и на
    #     делегированном проведении в CFE (ровно тот случай, ради которого сигнал и делался)
    #     маршрут обрывался исключением. Сервер читает через _ext_read_file — ему можно.
    # (2) ТАВТОЛОГИЯ: подтверждать делегата проверкой definitions[0]['category'] == 'CommonModules'
    #     БЕССМЫСЛЕННО: module_hint='ОбщийМодуль.X' уже добавляет в SQL mod.category='CommonModules'
    #     (bsl_index._normalize_module_hint + WHERE), поэтому проверка истинна ПО ПОСТРОЕНИЮ и про
    #     настоящего получателя не говорит ничего. Агент, получив тело одноимённого общего модуля,
    #     считал бы его «подтверждённым» — отказ хуже падения, потому что выглядит как ответ.
    # У сервера есть живой текст модуля, _index_state (какие общие модули существуют) и индекс
    # реквизитов — то есть средства РАЗРЕШИТЬ получателя, а не гадать о нём. Поэтому в hint уходят
    # ФАКТЫ («получатель X — реквизит документа, а не общий модуль») и только те шаги, которые в
    # песочнице действительно исполнимы (read_procedure / find_definition / git_search — ext-safe).
    # Разбор — BEST-EFFORT по телу обработчика: вложенные вызовы он не раскручивает. Но он не врёт:
    # чего не смог разрешить, то помечает «НЕ ОПОЗНАН».
    _POSTING_PREAMBLE = (
        "У документа есть ОбработкаПроведения, но выполняемых обращений "
        "`Движения.<Регистр>`/`RegisterRecords.<Register>` в ObjectModule не найдено. "
        "Это НЕ означает «документ непроводимый»: движения МОГУТ писаться не через коллекцию Движения "
        "(делегированы в общие модули или записаны наборами записей — типовой паттерн БГУ/ERP), "
        "а могут и отсутствовать вовсе — по этим данным не доказано ни то, ни другое. "
    )
    _POSTING_TAIL = (
        "КОГДА ТЕЛО ПИШУЩЕГО МЕТОДА НАЙДЕНО: увидел прямые `Движения.<Регистр>` -> "
        "find_register_writers('ИмяРегистра') покажет статические reverse-кандидаты. "
        "find_register_movements(document) применяет Posting/CFE, но main code_registers берет из снимка "
        "индекса: для измененного после build main-модуля проверь живое тело найденного пути. Увидел НАБОР ЗАПИСЕЙ "
        "(Регистры<Тип>.X.СоздатьНаборЗаписей() — любого вида: Накопления/Сведений/Бухгалтерии/Расчета) -> "
        "find_register_writers ИХ НЕ НАЙДЕТ (он ищет только прямые Движения.X в ObjectModule документов): "
        "ищи имя регистра через git_search (он идет по ВСЕМУ дереву и любым типам файлов). "
        "РАЗБОР ТЕЛА ВЫШЕ СДЕЛАН СЕРВЕРОМ по живому модулю (комментарии и строковые литералы вырезаны) и он "
        "BEST-EFFORT: смотрит ТОЛЬКО тело обработчика и не раскручивает вложенные вызовы. Что не удалось "
        "разрешить — помечено «НЕ ОПОЗНАН», а не выдано за факт. "
        "Движения через find_call_hierarchy НА САМОМ обработчике не ищи: вызов от ПЛАТФОРМЫ в граф "
        "вызовов не попадает, поэтому callers=0 — это норма, а НЕ мертвый код. Обработчики по имени "
        "хелпер не исключает: ЯВНЫЙ вызов ОбработкаПроведения(...) из BSL, если он есть, он покажет — "
        "просто движений этим не найти. Трассируй им ДЕЛЕГАТА, а не обработчик."
    )
    _POSTING_PROFILE_TAIL = (
        " Постановку (Posting=Deny -> движений нет В ПРИНЦИПЕ) эта секция НЕ проверяет: поля posting "
        "в индексе нет, а live-чтение XML нарушило бы дешевый контракт профиля. Нужна постановка -> "
        "find_register_movements('{doc}') (там is_postable). "
        "Сам обработчик подтвержден по ЖИВОМУ модулю (не по таблице methods), поэтому ЛОЖНЫМ этот "
        "сигнал не бывает. Но КАНДИДАТА профиль ищет по индексу и чужих модулей не открывает: если "
        "индекс устарел и метода в нем еще нет, профиль ПРОМОЛЧИТ (промолчит, но не соврет). "
        "Сомневаешься -> find_register_movements('{doc}'): он читает модули напрямую."
    )
    _KIND_LABEL = {
        "common_module": "ОБЩИЙ МОДУЛЬ",
        "manager_module": "ЭКСПОРТНЫЙ МЕТОД МОДУЛЯ МЕНЕДЖЕРА",
        "manager_unverified": "MANAGER-ВЫЗОВ, ПОЛЬЗОВАТЕЛЬСКИЙ ЭКСПОРТ НЕ ПОДТВЕРЖДЕН",
        "variable": "ПЕРЕМЕННАЯ (или параметр) ЭТОГО модуля",
        "attribute": "РЕКВИЗИТ ДОКУМЕНТА",
        "shadow_risk": "ОБЩИЙ МОДУЛЬ С ТАКИМ ИМЕНЕМ ЕСТЬ, НО ВОЗМОЖНО ЗАТЕНЕН ПЕРЕМЕННОЙ",
        "module_unverified": "ОБЩИЙ МОДУЛЬ С ТАКИМ ИМЕНЕМ ЕСТЬ, НО РЕКВИЗИТЫ ДОКУМЕНТА НЕ ПРОВЕРЕНЫ",
        "unknown": "НЕ ОПОЗНАН",
    }

    def _analyze_posting_handler(
        document_name: str,
        handler_path: str,
        module_body: str,
        *,
        live_attributes: bool = False,
        live_manager_modules: bool = True,
        entry_method: str = "ОбработкаПроведения",
    ) -> dict:
        """Факты о теле обработчика: наборы записей, делегаты и КЕМ является получатель слева от точки.

        Получателя РАЗРЕШАЕМ, а не угадываем, и приоритет — как в самом BSL: переменная/параметр
        затеняет всё (имя переменной перекрывает одноимённый общий модуль), затем реквизит документа,
        и только потом общий модуль — и лишь если такой в конфигурации ДЕЙСТВИТЕЛЬНО есть. Иначе
        честное «НЕ ОПОЗНАН»: молчание лучше уверенной лжи.

        Читаем по коду с ВЫРЕЗАННЫМИ комментариями и строками (_live_code_only), поэтому
        `// СервисПроведения = ...` больше не выставляет ложный признак переменной, а
        закомментированный вызов не рождает делегата.
        """
        code = _live_code_only(module_body or "")
        handler_code = _extract_live_procedure_code(module_body or "", entry_method)
        params: set[str] = set()
        if handler_code:
            # Сигнатура BSL законно продолжается на следующих строках. Разбираем её тем же
            # штатным merger/parser, что extract_procedures: чтение только первой физической
            # строки теряло параметры-продолжения и позволяло одноимённому общему модулю
            # ошибочно победить параметр в правилах затенения.
            merged_decl, _line_map = _merge_proc_continuations(handler_code.splitlines())
            if merged_decl:
                decl_match = re.compile(BSL_PATTERNS["procedure_def"], re.IGNORECASE).search(merged_decl[0])
                if decl_match:
                    params.update(name.casefold() for name in _split_params(decl_match.group(3) or ""))

        # СОЗДАНИЕ набора/менеджера — еще НЕ запись: СоздатьНаборЗаписей()/СоздатьМенеджерЗаписи()
        # регистр не трогают до вызова Записать() (`Набор.Прочитать()` — чтение). Поэтому статус
        # раздваивается: «записан» — по результату фабрики виден Записать()/Write() (цепочкой сразу
        # за фабрикой либо на переменной, куда фабрика присвоена в позиции оператора); «создан без
        # видимой записи» — иначе. Записать() по переменной ищется ТОЛЬКО на участке от фабрики до
        # СЛЕДУЮЩЕГО присваивания той же переменной: имя переиспользуется законно (`Набор = ...А...;
        # Набор.Записать(); Набор = ...Б...; Набор.Прочитать();`), и поиск по всему телу приписал бы
        # раннюю запись А еще и регистру Б. Ветвления НЕ анализируем (это dataflow, регэкспам он не
        # по зубам) — сомнительный случай безопасно понижается до «создан, запись не видна».
        # Прежняя версия объявляла ЗАПИСЬЮ само создание — ложь на каждом чтении набора.
        rs_written: dict[str, bool] = {}
        for m_rs in _RECORD_SET_RE.finditer(handler_code):
            item = f"{m_rs.group('manager')}.{m_rs.group('register')}"
            written = bool(
                re.match(
                    r"\s*\([^()]*\)\s*\.\s*(?:Записать|Write)\s*\(",
                    handler_code[m_rs.end() :],
                    re.IGNORECASE,
                )
            )
            if not written:
                stmt_prefix = re.split(r"[;\n]", handler_code[: m_rs.start()])[-1]
                am = re.search(
                    r"(?:^|\bТогда\b|\bИначе\b|\bЦикл\b|\bThen\b|\bElse\b|\bDo\b)\s*(\w+)\s*=\s*\Z",
                    stmt_prefix,
                    re.IGNORECASE,
                )
                if am:
                    var_esc = re.escape(am.group(1))
                    region = handler_code[m_rs.end() :]
                    next_assign = re.search(
                        rf"(?:^|[;\n]|\bТогда\b|\bИначе\b|\bЦикл\b|\bThen\b|\bElse\b|\bDo\b)\s*{var_esc}\s*=(?!=)",
                        region,
                        re.IGNORECASE,
                    )
                    if next_assign:
                        region = region[: next_assign.start()]
                    if re.search(rf"\b{var_esc}\s*\.\s*(?:Записать|Write)\s*\(", region, re.IGNORECASE):
                        written = True
            rs_written[item] = rs_written.get(item, False) or written
        record_sets = [k for k, v in rs_written.items() if v]
        record_sets_created = [k for k, v in rs_written.items() if not v]

        # Локальные методы берём ИЗ УЖЕ ПРОЧИТАННОГО кода, а не через _parse_procedures: тот
        # открыл бы модуль ВТОРОЙ раз (_ext_read_file → read_file_fn), и секция registers профиля
        # перестала бы держать своё обещание «открываю не больше одного модуля-кандидата».
        # Комментарии здесь уже вырезаны, поэтому закомментированная процедура в набор не попадёт
        # (в отличие от таблицы methods, куда билдер её кладёт).
        local_methods = {
            name.casefold()
            for name in re.findall(
                r"^\s*(?:Процедура|Функция|Procedure|Function)\s+(\w+)", code, re.IGNORECASE | re.MULTILINE
            )
        }

        indexed_common_module_paths: dict[str, list[str]] = {}
        manager_module_paths: dict[tuple[str, str], list[str]] = {}
        for rel, info in _index_state:
            object_key = (info.object_name or "").casefold()
            if not object_key:
                continue
            if (info.category or "") == "CommonModules":
                indexed_common_module_paths.setdefault(object_key, []).append(rel)
            if info.module_type == "ManagerModule" and info.category:
                manager_module_paths.setdefault((info.category, object_key), []).append(rel)

        # `_index_state` при idx_reader — снимок SQLite и может помнить уже удалённый модуль.
        # Проверяем живьём только имена, реально встретившиеся слева от точки; полный обход всех
        # общих модулей превратил бы точечный posting-hint в дорогой прогрев конфигурации.
        live_module_cache: dict[str, str | None] = {}
        live_common_cache: dict[str, tuple[list[str], bool]] = {}

        def _live_module_text(rel_path: str) -> str | None:
            if rel_path not in live_module_cache:
                try:
                    live_module_cache[rel_path] = _ext_read_file(rel_path)
                except Exception:
                    live_module_cache[rel_path] = None
            return live_module_cache[rel_path]

        def _live_module_exists(rel_path: str) -> bool:
            # Для факта существования достаточно live-ФС. Не открываем каждый встреченный общий
            # модуль целиком: профиль держит отдельный бюджет чтений, а содержимое здесь не нужно.
            try:
                return _ext_resolve_safe(rel_path).is_file()
            except Exception:
                return False

        def _live_common_modules(name: str) -> tuple[list[str], bool]:
            """(живые пути, был ли такой модуль в снимке)."""
            key = name.casefold()
            if key not in live_common_cache:
                indexed_paths = indexed_common_module_paths.get(key, [])
                candidates = list(indexed_paths)
                candidate_keys: set[str] = set()
                for path in candidates:
                    try:
                        candidate_keys.add(os.path.normcase(os.path.abspath(str(_ext_resolve_safe(path)))))
                    except (OSError, PermissionError):
                        pass
                # SQLite и lazy-index — снимки. Для получателя с уже известным точным
                # именем проверяем только два штатных CF/EDT-пути в main и configured CFE:
                # новый CommonModule после build должен классифицироваться по живому файлу.
                for root in (_base_path_resolved, *_ext_roots_resolved):
                    common_root = root / "CommonModules"
                    direct_object_dir = common_root / name
                    object_dirs = [direct_object_dir]
                    try:
                        # На Windows и при точном регистре это O(1). Перечень соседей нужен
                        # только на регистрозависимой ФС, когда BSL-получатель написан иначе.
                        if not direct_object_dir.is_dir():
                            object_dirs.extend(
                                child
                                for child in common_root.iterdir()
                                if child.is_dir() and child.name.casefold() == key
                            )
                    except (OSError, PermissionError):
                        pass
                    object_dir_keys: set[str] = set()
                    for object_dir in object_dirs:
                        try:
                            object_dir_key = os.path.normcase(os.path.abspath(str(object_dir.resolve())))
                        except (OSError, PermissionError):
                            continue
                        if object_dir_key in object_dir_keys:
                            continue
                        object_dir_keys.add(object_dir_key)
                        module_paths = (object_dir / "Ext" / "Module.bsl", object_dir / "Module.bsl")
                        for full_path in module_paths:
                            try:
                                full_path = full_path.resolve()
                                full_key = os.path.normcase(os.path.abspath(str(full_path)))
                                if full_key in candidate_keys or not full_path.is_file():
                                    continue
                                rel_path = os.path.relpath(str(full_path), base_path).replace("\\", "/")
                            except (OSError, PermissionError, ValueError):
                                continue
                            candidates.append(rel_path)
                            candidate_keys.add(full_key)
                live_common_cache[key] = (
                    [path for path in candidates if _live_module_exists(path)],
                    bool(indexed_paths),
                )
            return live_common_cache[key]

        manager_export_cache: dict[tuple[str, str], tuple[str | None, bool]] = {}
        manager_exports_cache: dict[str, set[str] | None] = {}

        def _manager_exports(rel_path: str) -> set[str] | None:
            """Живые export-имена ManagerModule; None означает, что файл проверить не удалось."""
            if rel_path not in manager_exports_cache:
                text = _live_module_text(rel_path)
                if text is None:
                    manager_exports_cache[rel_path] = None
                else:
                    exports: set[str] = set()
                    merged_lines, _line_map = _merge_proc_continuations(_live_code_only(text).splitlines())
                    proc_re = re.compile(BSL_PATTERNS["procedure_def"], re.IGNORECASE)
                    for merged_line in merged_lines:
                        proc_match = proc_re.search(merged_line)
                        if proc_match and proc_match.group(4) and proc_match.group(4).strip():
                            exports.add(proc_match.group(2).casefold())
                    manager_exports_cache[rel_path] = exports
            return manager_exports_cache[rel_path]

        def _manager_export_path(receiver: str, method: str) -> tuple[str | None, bool]:
            """``(живой ManagerModule, проверка полна)`` для manager-вызова.

            Само пространство менеджеров шумом не является: рядом с платформенным
            `Документы.X.НайтиПоНомеру()` законно живёт `Документы.X.МойЭкспорт()` из
            ManagerModule. Отличаем их по объявлению, а не по ненадёжному списку имён.

            SQLite и lazy extension-index — снимки: новый ManagerModule может появиться после
            их сборки. Поэтому точечно проверяем два штатных live-пути (CF/EDT) в основной
            конфигурации и в КАЖДОМ уже сконфигурированном соседнем extension-root, не делая
            широкого glob. ``complete=False`` запрещает считать вызов платформенным: так бывает
            в compact-профиле (чужие ManagerModule он намеренно не открывает) либо при ошибке
            проверки/чтения живого кандидата.
            """
            if not live_manager_modules:
                return None, False
            cache_key = (receiver.casefold(), method.casefold())
            if cache_key in manager_export_cache:
                return manager_export_cache[cache_key]
            parts = receiver.split(".")
            if len(parts) != 2:
                manager_export_cache[cache_key] = (None, False)
                return manager_export_cache[cache_key]
            category = _MANAGER_RECEIVER_CATEGORIES.get(parts[0].casefold())
            if category is None:
                manager_export_cache[cache_key] = (None, False)
                return manager_export_cache[cache_key]

            complete = len(_ext_roots_resolved) == len(_ext_paths_raw)
            candidates = list(manager_module_paths.get((category, parts[1].casefold()), []))
            candidate_keys: set[str] = set()
            for path in candidates:
                try:
                    candidate_keys.add(os.path.normcase(os.path.abspath(str(_ext_resolve_safe(path)))))
                except (OSError, PermissionError):
                    # Stale indexed path всё равно оставляем кандидатом: _manager_exports ниже
                    # вернёт None. Здесь лишь не даём dedup-проверке превратить его в исключение.
                    complete = False
            # Ни main, ни соседние roots расширений после build широко не glob'им. Их точные
            # объектные пути детерминированы форматом дампа и безопасны: category из фиксированной
            # карты, имя объекта — BSL-идентификатор из разобранного вызова.
            for root in (_base_path_resolved, *_ext_roots_resolved):
                try:
                    if not root.is_dir():
                        complete = False
                        continue
                    for suffix in (
                        Path(category) / parts[1] / "Ext" / "ManagerModule.bsl",
                        Path(category) / parts[1] / "ManagerModule.bsl",
                    ):
                        full_path = (root / suffix).resolve()
                        full_key = os.path.normcase(os.path.abspath(str(full_path)))
                        if full_key in candidate_keys or not full_path.is_file():
                            continue
                        try:
                            rel_path = os.path.relpath(str(full_path), base_path).replace("\\", "/")
                        except ValueError:
                            # Штатная топология — соседние исходники на одном диске. Не изобретаем
                            # для manager-probe отдельную абсолютную адресацию: необычный root
                            # лишь делает проверку неполной и не позволяет проглотить вызов.
                            complete = False
                            continue
                        candidates.append(rel_path)
                        candidate_keys.add(full_key)
                except (OSError, PermissionError):
                    # Невозможность проверить точный configured-root — не доказательство платформы.
                    complete = False

            for rel_path in candidates:
                exports = _manager_exports(rel_path)
                if exports is None:
                    complete = False
                    continue
                if method.casefold() in exports:
                    manager_export_cache[cache_key] = (rel_path, True)
                    return manager_export_cache[cache_key]
            manager_export_cache[cache_key] = (None, complete)
            return manager_export_cache[cache_key]

        # РЕКВИЗИТЫ: без них получателя НЕ разрешить (реквизит затеняет одноимённый общий модуль).
        # attrs_source называет ИСТОЧНИК проверки — 'live' | 'live_partial' | 'index' | 'none' — и
        # уходит в hint: факт стоит ровно столько, сколько стоит его проверка. В `attributes`
        # попадают ТОЛЬКО имена из ЖИВОГО XML — имена из индекса НЕ подмешиваются: снимок может
        # и отставать от XML (реквизит добавлен после сборки), и ОПЕРЕЖАТЬ его (реквизит УДАЛЁН
        # из XML без пересборки). Смешанный набор выдавал удалённый реквизит за live-факт
        # «РЕКВИЗИТ ДОКУМЕНТА» и уводил от настоящего модуля-делегата. Поэтому index-позитив не
        # порождает НИКАКОГО факта — от индекса остаётся лишь attrs_source='index' как честная
        # ПРИЧИНА развилки; профиль (live-чтение запрещено контрактом) на любом получателе даёт
        # развилку либо НЕ ОПОЗНАН, а сильные утверждения остаются live-маршруту хелпера.
        attributes: set[str] = set()
        attrs_source = "none"
        if idx_reader is not None:
            try:
                rows = idx_reader.get_object_attributes(object_name=document_name, category="Documents")
            except Exception:
                rows = None
            if rows is not None:
                attrs_source = "index"
        if live_attributes:

            def _collect_attrs(meta: dict) -> None:
                for a in meta.get("attributes") or []:
                    nm = (a.get("name") or a.get("attr_name") or "") if isinstance(a, dict) else ""
                    if nm:
                        attributes.add(str(nm).casefold())
                for ts in meta.get("tabular_sections") or []:
                    nm = (ts.get("name") or "") if isinstance(ts, dict) else ""
                    if nm:
                        attributes.add(str(nm).casefold())

            try:
                meta = parse_object_xml(f"Documents/{document_name}") or {}
            except Exception:
                meta = {}
            main_live_ok = isinstance(meta, dict) and bool(meta)
            if main_live_ok:
                _collect_attrs(meta)
            ext_live_complete = not _ext_metadata_scan_failed[0]
            # Реквизит, добавленный РАСШИРЕНИЕМ, живет только в его метаданных — ни индекс
            # (main-only), ни основной XML его не видят. Локаторы берем у ШТАТНОГО обходчика
            # (_iter_metadata_xml_files -> _extension_metadata_xml): он знает ВСЕ поддержанные
            # диалекты дампа (sibling Documents/X.xml, EDT Documents/X/X.mdo, CF Ext/Document.xml)
            # — захардкоженный путь видел лишь один из них, и реквизит EDT-расширения молча
            # пропадал. А парсим САМ файл расширения по локатору: _resolve_object_xml здесь НЕ
            # годится — для adopted-объекта (расширение меняет существующий документ) он вернул
            # бы XML ОСНОВНОЙ конфигурации, и добавленный реквизит потерялся бы снова (по той же
            # причине не подходит кеш _live_attributes_in_extensions — он построен на резолвере).
            for _cat, _obj, _rel in _extension_metadata_xml:
                if _cat.casefold() != "documents" or (_obj or "").casefold() != document_name.casefold():
                    continue
                try:
                    _meta_ext = parse_metadata_xml(_ext_read_file(_rel))
                except Exception:
                    _meta_ext = None
                if isinstance(_meta_ext, dict) and _meta_ext:
                    _collect_attrs(_meta_ext)
                else:
                    # Файл расширения есть, а прочитать/разобрать его не удалось: проверка
                    # НЕПОЛНАЯ. Молча продолжить значило бы заявить «сверено, включая XML
                    # расширений» о проверке, которой не было, — и потерять реквизит,
                    # добавленный расширением, вместе с настоящим получателем.
                    ext_live_complete = False
            if main_live_ok:
                # Собранные имена остаются полезными в ЛЮБОМ случае (наличие доказуемо всегда),
                # но ФАКТ «это общий модуль» требует ПОЛНОЙ проверки отсутствия — неполный live
                # даёт только развилку.
                attrs_source = "live" if ext_live_complete else "live_partial"

        # ОБЛАСТЬ ВИДИМОСТИ для маркеров переменной: секция модульных переменных (до первой
        # процедуры) + ТЕЛО обработчика + главный раздел модуля (после последней процедуры).
        # `Перем X` и `X = ...` ВНУТРИ ДРУГОЙ процедуры — ЕЕ локальная переменная, к обработчику
        # отношения не имеет: межпроцедурный поиск объявлял «переменной» получателя, который в
        # обработчике разрешается в общий модуль, — и точный маршрут find_definition подменялся
        # широким поиском. Модульные же переменные (Перем до процедур) и присваивания в главном
        # разделе видимы обработчику ЗАКОННО — их из области не выкидываем.
        _first_proc = re.search(r"^\s*(?:Процедура|Функция|Procedure|Function)\b", code, re.IGNORECASE | re.MULTILINE)
        _module_prelude = code[: _first_proc.start()] if _first_proc else code
        _proc_ends = list(_PROC_END_RE.finditer(code))
        _module_trailing = code[_proc_ends[-1].end() :] if _proc_ends else ""
        shadow_scope = "\n".join((_module_prelude, handler_code, _module_trailing))

        def _shadowing(name: str) -> str:
            """Затеняет ли имя переменная: 'declared' | 'maybe' | 'no'.

            В BSL `=` — это И присваивание, И СРАВНЕНИЕ, поэтому голое `X =` доказательством НЕ
            является: на `Если ОбщийМодульУчета = Неопределено Тогда` мы объявили бы НАСТОЯЩИЙ
            общий модуль «переменной» — то есть соврали бы ровно тем способом, который этот релиз
            и чинит, только в другую сторону. Поэтому маркеры РАЗДЕЛЕНЫ:
              * 'declared' — ФАКТ: параметр, `Перем X`, переменная `Для Каждого X Из ...`, либо
                `X =` в позиции ОПЕРАТОРА (начало строки / после ';' / после
                Тогда|Иначе|Цикл) — там сравнение невозможно;
              * 'maybe'    — `X =` в иной позиции: скорее всего сравнение, но поручиться нельзя;
              * 'no'       — упоминаний нет.
            Все маркеры ищутся в shadow_scope (модульные переменные + тело обработчика + главный
            раздел), а НЕ по всему модулю: локальная переменная ЧУЖОЙ процедуры — другая область
            видимости, и считать ее затенением значит уводить от рабочего модуля-получателя.
            Регистр и пробелы не важны: BSL регистронезависим, и `сервис=Получить()` объявляет ту
            же переменную, которую зовёт `Сервис.Метод()`.
            """
            if name.casefold() in params:
                return "declared"
            esc = re.escape(name)
            if re.search(rf"\b(?:Перем|Var)\b[^;\n]*\b{esc}\b", shadow_scope, re.IGNORECASE):
                return "declared"
            if re.search(
                rf"\b(?:Для\s+Каждого|For\s+Each)\s+{esc}\s+(?:Из|In)\b",
                shadow_scope,
                re.IGNORECASE,
            ):
                return "declared"
            if re.search(
                rf"(?m)(?:^|;|\bТогда\b|\bИначе\b|\bЦикл\b|\bThen\b|\bElse\b|\bDo\b)\s*{esc}\s*=(?!=)",
                shadow_scope,
                re.IGNORECASE,
            ):
                return "declared"
            if re.search(rf"\b{esc}\s*=(?!=)", shadow_scope, re.IGNORECASE):
                return "maybe"
            return "no"

        def _platform_sourced(name: str) -> bool:
            """Присвоено ли имени значение ПЛАТФОРМЕННОЙ фабрики — единственное основание скрыть
            его Записать()/Выполнить() как платформенный вызов.

            Суждение — по ТЕЛУ ОБРАБОТЧИКА, и только когда возразить нечем: КАЖДОЕ присваивание
            имени в теле — платформенная фабрика. Почему так строго:
              * присваивание в ДРУГОЙ процедуре — это ДРУГАЯ переменная (локальная область
                видимости); искать «по всему модулю» значило бы навсегда пометить имя как
                платформенное и подавить настоящий делегат в обработчике;
              * порядок присваиваний внутри тела мы НЕ анализируем (это dataflow, регэкспам он
                не по зубам): после `Сервис = СоздатьНаборЗаписей(); Сервис = ПолучитьСервис();`
                в точке вызова значение уже другое — любое НЕплатформенное присваивание
                дисквалифицирует подавление целиком, и вызов ПОКАЗЫВАЕТСЯ.
            Сравнения (`Если Сервис = Неопределено`) завышают счёт непплатформенных присваиваний
            и тем самым тоже ведут к показу — безопасное направление.
            """
            esc = re.escape(name)
            assigns = re.findall(rf"\b{esc}\s*=(?!=)", handler_code, re.IGNORECASE)
            if not assigns:
                return False
            platform = re.findall(
                rf"\b{esc}\s*=\s*(?:(?:Регистры(?:Накопления|Сведений|Бухгалтерии|Расчета)|"
                rf"(?:Accumulation|Information|Accounting|Calculation)Registers)\s*\.\s*\w+\s*\.\s*"
                rf"(?:Создать(?:НаборЗаписей|МенеджерЗаписи)|Create(?:RecordSet|RecordManager))\b|"
                rf"(?:Новый|New)\b)",
                handler_code,
                re.IGNORECASE,
            )
            return len(platform) == len(assigns)

        delegates: list[dict] = []
        local_calls: list[dict] = []
        global_calls: list[str] = []
        dynamic_calls: list[str] = []
        seen: set[tuple[str, str]] = set()
        # Позиции ИМЕН МЕТОДОВ dotted-вызовов: dotted-регулярка терпит пробелы вокруг точки
        # (`Модуль . Метод()`), а dotless-регулярка запрещает точку только ВПЛОТНУЮ перед именем —
        # без этого набора то же `Метод(` матчилось бы еще и как «вызов без точки», и hint рядом с
        # правильным маршрутом по модулю печатал бы ложный «экспортный метод ГЛОБАЛЬНОГО модуля».
        dotted_method_starts: set[int] = set()
        for m_call in _DOTTED_CALL_RE.finditer(handler_code):
            dotted_method_starts.add(m_call.start(2))
            receiver, method = m_call.group(1), m_call.group(2)
            receiver = re.sub(r"\s*\.\s*", ".", receiver)
            rl, ml = receiver.casefold(), method.casefold()
            if (rl, ml) in seen:
                continue
            seen.add((rl, ml))
            head, _, chain_rest = rl.partition(".")
            if head in _MANAGER_RECEIVER_CATEGORIES:
                # Эти две фабрики уже разобраны выше как record-set facts. Даже compact-профилю
                # не нужно открывать ManagerModule, чтобы отличить штатную фабрику регистра от
                # пользовательского экспорта; иначе один вызов давал бы одновременно точный факт
                # о наборе и ложную развилку «manager-вызов не проверен».
                if head in _REGISTER_MANAGER_RECEIVERS and ml in (
                    "создатьнаборзаписей",
                    "создатьменеджерзаписи",
                    "createrecordset",
                    "createrecordmanager",
                ):
                    continue
                manager_path, manager_resolution_complete = _manager_export_path(receiver, method)
                if manager_path is None:
                    if not manager_resolution_complete:
                        # Не смешиваем «экспорта нет» с «проверить не удалось». Профиль сохраняет
                        # дешевый контракт и не читает чужие ManagerModule; live-хелпер аналогично
                        # не имеет права проглотить вызов при stale/нечитаемом модуле.
                        delegates.append(
                            {
                                "receiver": receiver,
                                "method": method,
                                "kind": "manager_unverified",
                                "homonym_module": False,
                                "stale_homonym_module": False,
                                "platform_method_name": ml in _DELEGATE_METHOD_NOISE,
                            }
                        )
                    # Только ПОЛНАЯ live-проверка без такого export доказывает платформенный
                    # manager-вызов (`НайтиПоНомеру`, `СоздатьНаборЗаписей` ...).
                    continue
                delegates.append(
                    {
                        "receiver": receiver,
                        "method": method,
                        "kind": "manager_module",
                        "module_path": manager_path,
                        "homonym_module": False,
                        "stale_homonym_module": False,
                        "platform_method_name": False,
                    }
                )
                continue
            if not chain_rest and rl in ("этотобъект", "thisobject"):
                # `ЭтотОбъект.Имя()` — вызов СВОЕГО метода (эквивалент локального `Имя()`), и
                # выбрасывать его целиком нельзя: локальный метод может вести к движениям. Имя,
                # не объявленное в этом модуле, — платформенный метод объекта (Записать/
                # Проверить/...), делегатом не является.
                if ml in local_methods and not any(c["name"].casefold() == ml for c in local_calls):
                    local_calls.append({"name": method, "path": handler_path})
                continue
            if head in _RECEIVER_NOISE and head not in ("этотобъект", "thisobject"):
                # Голова цепочки — платформенное пространство имен (Документы.X.Метод() и т.п.):
                # менеджерный вызов, не делегат. `ЭтотОбъект.X...` под это НЕ подпадает — там
                # дальше настоящий получатель (реквизит/свойство объекта).
                continue
            if chain_rest:
                # ЦЕПОЧКА СВОЙСТВ (`ЭтотОбъект.Реквизит.М()`, `А.Б.М()`): текстовый разбор такого
                # получателя НЕ разрешает — это dataflow, и честный ответ здесь НЕ ОПОЗНАН, а не
                # молчание. Раньше цепочка выпадала из анализа ЦЕЛИКОМ: все списки фактов пустели,
                # и hint заявлял «движений не пишет», пряча настоящего делегата. Исключение одно:
                # `ЭтотОбъект.X.М()` с X-реквизитом, найденным в ЖИВОМ XML (attributes собираются
                # только из него), — это доказуемый РЕКВИЗИТ: НАЛИЧИЕ доказуемо и при неполном
                # live (полнота нужна лишь для доказательства ОТСУТСТВИЯ — факта «общий модуль»).
                # _shadowing/_platform_sourced к цепочке неприменимы (они про ОДНО имя переменной).
                segs = rl.split(".")
                kind = "unknown"
                if head in ("этотобъект", "thisobject") and len(segs) == 2 and segs[1] in attributes:
                    kind = "attribute"
                live_homonyms, indexed_homonym = _live_common_modules(segs[-1])
                delegates.append(
                    {
                        "receiver": receiver,
                        "method": method,
                        "kind": kind,
                        # Однофамилец проверяется по ПОСЛЕДНЕМУ звену: именно его агент по ошибке
                        # принял бы за модуль в find_definition(..., 'ОбщийМодуль.<звено>').
                        "homonym_module": bool(live_homonyms),
                        "stale_homonym_module": indexed_homonym and not live_homonyms,
                        "platform_method_name": ml in _DELEGATE_METHOD_NOISE,
                    }
                )
                continue
            shadow = _shadowing(receiver)
            live_module_paths, indexed_module = _live_common_modules(receiver)
            is_module = bool(live_module_paths)
            if shadow == "declared":
                kind = "variable"
            elif rl in attributes:
                kind = "attribute"
            elif is_module and shadow == "maybe":
                # `X =` есть, но в позиции, где это МОЖЕТ быть сравнением. Общий модуль с таким
                # именем существует — и мы НЕ ЗНАЕМ, кто из них тут получатель. Честный ответ —
                # назвать оба варианта, а не выбрать удобный: молчание лучше уверенной лжи, но
                # уверенная ложь в ЛЮБУЮ сторону хуже честной развилки.
                kind = "shadow_risk"
            elif is_module and attrs_source == "live":
                kind = "common_module"
            elif is_module:
                # СТРУКТУРНЫЙ ПОТОЛОК: факт «это общий модуль» разрешен ТОЛЬКО при live-проверке
                # реквизитов. Index-источник сюда тоже попадает: успешный SQL-запрос не доказывает
                # полноту снимка (реквизит, добавленный после сборки, из него не виден), а факт с
                # оговоркой «индекс может отставать» — все равно ложный факт на stale-индексе.
                # Слабый источник не имеет права порождать сильное утверждение — только развилку.
                kind = "module_unverified"
            else:
                kind = "unknown"
            # Шум по имени метода обоснован ТОЛЬКО ПРОСЛЕЖЕННЫМ источником получателя.
            # Пара (вид получателя, имя метода) ничего не доказывает: статически
            # `МенеджерЗаписи.Записать()` и пользовательский `Сервис.Записать()` неотличимы,
            # и выбрасывать второй значит снова выдать эвристику за отрицательный факт
            # («движений не пишет»). Скрываем вызов лишь когда переменная присвоена из
            # ПЛАТФОРМЕННОЙ фабрики (Создать(НаборЗаписей|МенеджерЗаписи) — регистр уже назван
            # в record_sets — либо `Новый ...`); непрослеженный источник -> вызов ПОКАЗЫВАЕМ,
            # с оговоркой о платформенном имени.
            if ml in _DELEGATE_METHOD_NOISE and kind == "variable" and _platform_sourced(receiver):
                continue
            delegates.append(
                {
                    "receiver": receiver,
                    "method": method,
                    "kind": kind,
                    # Однофамилец-модуль при НЕ-модульном получателе — это и есть ловушка, из-за
                    # которой агент молча читал ЧУЖОЕ тело. Называем её явно.
                    "homonym_module": kind != "common_module" and is_module,
                    "stale_homonym_module": indexed_module and not is_module,
                    # Имя метода совпадает с платформенным -> в hint уйдет оговорка о двусмысленности.
                    "platform_method_name": ml in _DELEGATE_METHOD_NOISE,
                }
            )

        for m_call in _DOTLESS_CALL_RE.finditer(handler_code):
            if m_call.start(1) in dotted_method_starts:
                # Это имя метода dotted-вызова с пробелом после точки — оно уже классифицировано
                # выше по своему получателю, вторым «вызовом без точки» ему быть нельзя.
                continue
            name = m_call.group(1)
            nl = name.casefold()
            # ``Новый Структура(...)`` / ``New Structure(...)`` is a type constructor,
            # not a dotless method call. _live_code_only keeps whitespace positions, so
            # this also covers multiline formatting and comments between the tokens.
            if re.search(r"(?:\bНовый|\bNew)\s+$", handler_code[: m_call.start(1)], re.IGNORECASE):
                continue
            if nl == entry_method.casefold():
                continue
            if nl in local_methods:
                # Локальный метод проверяется ДО любого шума: процедура этого модуля может
                # законно называться Записать/Выполнить — она объявлена, значит это не платформа.
                if not any(c["name"].casefold() == nl for c in local_calls):
                    local_calls.append({"name": name, "path": handler_path})
            elif nl in ("выполнить", "execute"):
                # Встроенное динамическое выполнение — не обычный «шум»: внутри строки/переменной
                # может находиться весь код записи регистра. _live_code_only намеренно вырезает
                # строки, поэтому содержимое здесь статически НЕ видно. Локальная процедура с тем
                # же именем уже поймана веткой выше и остается обычным локальным делегатом.
                if not any(n.casefold() == nl for n in dynamic_calls):
                    dynamic_calls.append(name)
            elif nl in _DOTLESS_NOISE:
                continue
            elif name not in global_calls:
                # Методный шум сюда НЕ применяем: голых платформенных Записать()/Получить()/
                # Загрузить() не существует (реальные голые платформенные — Выполнить/Найти —
                # уже в _DOTLESS_NOISE), а экспортный метод ГЛОБАЛЬНОГО общего модуля законно
                # зовется как угодно — глотать его значило бы потерять делегата.
                # Вызов БЕЗ точки, которого в ЭТОМ модуле НЕ объявлено. «Значит метод тут же» —
                # неверно: без точки зовутся и экспортные методы ГЛОБАЛЬНОГО общего модуля, и
                # методы глобального контекста платформы. Молча выбросить такой вызов значило бы
                # потерять единственного делегата (и объявить, что обработчик движений не пишет).
                global_calls.append(name)

        return {
            "record_sets": record_sets,
            "record_sets_created": record_sets_created,
            "delegates": delegates,
            "local_calls": local_calls,
            "global_calls": global_calls,
            "dynamic_calls": dynamic_calls,
            "attrs_source": attrs_source,
        }

    # --- Capability-aware маршруты поиска для posting-hint --------------------------------------
    # git_search регистрируется ТОЛЬКО когда исходники под git (_want_git_search вычисляется ниже
    # по коду фабрики, к моменту вызова хелперов он уже есть). Безусловный совет git_search(...)
    # на не-git конфигурации — NameError ровно на fallback-пути: хелпера просто НЕТ в namespace.
    # Поэтому каждый терминальный совет в hint строится из хелперов, реально зарегистрированных
    # в ЭТОЙ песочнице, — а когда исчерпывающего маршрута нет, hint честно называет ограничение
    # вместо обещания несуществующего шага.

    def _all_bsl_decl_search_call(meth: str) -> str:
        """Exact declaration search over the current session's live BSL catalog.

        ``git grep -iE`` on Git for Windows can return a false zero for Cyrillic identifiers
        combined with a whitespace quantifier. Python ``re.IGNORECASE`` has the required BSL
        semantics.  The internal result cap prevents a common method from producing an
        unbounded hint; its sentinel makes an early stop explicit.
        """
        live_catalog = _ensure_live_bsl_catalog()
        live_pattern = rf"(?i)^\s*(?:Процедура|Функция|Procedure|Function)\s+{re.escape(meth)}\b"
        return f"safe_grep({live_pattern!r}, max_files={len(live_catalog)}, _result_cap=50)"

    def _decl_search_call(meth: str) -> str | None:
        """Чистый ИСПОЛНИМЫЙ вызов поиска объявления метода по всему дереву, или None,
        когда исчерпывающего маршрута в этой песочнице нет (не под git и без индекса)."""
        if _want_git_search:
            return _all_bsl_decl_search_call(meth)
        if idx_reader is not None:
            return f"find_definition({meth!r})"
        return None

    def _live_decl_search_call(meth: str) -> str:
        """Поиск объявления без доверия к stale methods/modules из SQLite."""
        return _all_bsl_decl_search_call(meth)

    def _decl_search_note(*, live: bool = False) -> str:
        if live or _want_git_search:
            reason = (
                "git_search намеренно не используется: git grep -iE на Windows дает ложный ноль "
                "для кириллицы с whitespace-квантором"
                if _want_git_search
                else "live-маршрут не доверяет stale snapshot"
            )
            return (
                "точный live Python-regex проверяет процедуры/функции, русский/английский синтаксис, "
                f"регистр и BSL-пробелы по текущему BSL-каталогу с потолком 50 кандидатов; {reason}; "
                "финальный элемент с _truncated=True означает, что поиск остановлен досрочно и кандидаты "
                "могут оставаться; без него каталог проверен полностью"
            )
        return (
            "git_search здесь НЕ зарегистрирован (исходники не под git), поэтому маршрут — "
            "find_definition БЕЗ module-hint: кандидаты по всему индексу, выбирай по category "
            "сам; индекс может отставать от свежих правок"
        )

    def _decl_search_route(meth: str, known_module: str = "") -> str:
        """Маршрут поиска объявления ПРОЗОЙ (вызов + оговорка источника). known_module — когда
        модуль-получатель уже разрешен и объявление можно искать прямо в нем (safe_grep живой,
        индекса и git не требует)."""
        if not _want_git_search and known_module:
            return (
                f"safe_grep({meth!r}, {known_module!r}) — git_search не зарегистрирован (исходники "
                "не под git), но модуль-получатель уже известен: ищем объявление прямо в нем, живьем"
            )
        call = _decl_search_call(meth)
        if call is None:
            return (
                "исчерпывающего поиска по дереву в этой песочнице НЕТ (git_search не зарегистрирован: "
                "исходники не под git; индекса тоже нет) — сузь кандидатов через find_module и "
                f"проверь каждого: safe_grep({meth!r}, 'ИмяМодуля')"
            )
        return f"{call} ({_decl_search_note()})"

    def _register_search_route() -> str:
        """Куда идти за ОСТАЛЬНЫМИ писателями регистра, найденного набором/менеджером записи."""
        if _want_git_search:
            return "git_search('ИмяРегистра')"
        all_bsl_candidates = len(_ensure_live_bsl_catalog())
        return (
            f"safe_grep('ИмяРегистра', max_files={all_bsl_candidates}) по ВСЕМ {all_bsl_candidates} "
            "известным BSL-модулям (git_search не зарегистрирован: исходники не под git; "
            "XML/тексты запросов safe_grep не покрывает)"
        )

    def _build_posting_hint(
        document_name: str,
        handler_path: str,
        module_body: str,
        *,
        profile: bool,
        interceptors: list[dict] | None = None,
        posting_calls_offset: int = 0,
    ) -> str:
        """Hint = ФАКТЫ разбора + только те шаги, которые в песочнице ИСПОЛНИМЫ.

        Шаги нумеруются подряд «(N) код -> пояснение» и являются валидным Python: тест вырезает их
        из текста и ИСПОЛНЯЕТ — псевдокод здесь = SyntaxError = красный тест. Ни один шаг не зовёт
        generic read_file: handler_path может указывать в CFE-расширение, а туда песочнице хода нет
        (read_procedure / find_definition / git_search — ext-safe).
        """
        entrypoints = [
            {
                "path": handler_path,
                "method": "ОбработкаПроведения",
                "body": module_body,
                "annotation": "",
            },
            *(interceptors or []),
        ]
        facts = {
            "record_sets": [],
            "record_sets_created": [],
            "delegates": [],
            "local_calls": [],
            "global_calls": [],
            "dynamic_calls": [],
            "attrs_source": "none",
        }
        created_candidates: list[str] = []
        source_rank = {"none": 0, "index": 1, "live_partial": 2, "live": 3}
        replacement_annotations = {
            entry["annotation"].casefold()
            for entry in entrypoints[1:]
            if (entry.get("annotation") or "").casefold() in ("вместо", "изменениеиконтроль")
        }
        _active, _suppressed, replacement_meta = _apply_cfe_posting_replacement([], entrypoints[1:])
        main_continuation_visible = bool(replacement_meta and replacement_meta["main_handler_continuation_visible"])
        # &Вместо и &ИзменениеИКонтроль заменяют исходную точку входа. Код main может выполниться
        # лишь если replacement явно продолжит вызов.  Видимый ПродолжитьВызов — тот же
        # conservative possible-execution сигнал, по которому code_registers сохраняет main rows.
        # Сами CFE-entrypoint (и соседние &Перед/&После) анализируются в обоих случаях.
        analyzed_entrypoints = (
            entrypoints[1:] if replacement_annotations and not main_continuation_visible else entrypoints
        )
        for entry in analyzed_entrypoints:
            found = _analyze_posting_handler(
                document_name,
                entry["path"],
                entry["body"],
                live_attributes=not profile,
                live_manager_modules=not profile,
                entry_method=entry["method"],
            )
            for reg in found["record_sets"]:
                if reg not in facts["record_sets"]:
                    facts["record_sets"].append(reg)
            for reg in found["record_sets_created"]:
                if reg not in created_candidates:
                    created_candidates.append(reg)
            for delegate in found["delegates"]:
                key = (
                    delegate["receiver"].casefold(),
                    delegate["method"].casefold(),
                    delegate["kind"],
                )
                if not any(
                    (
                        d["receiver"].casefold(),
                        d["method"].casefold(),
                        d["kind"],
                    )
                    == key
                    for d in facts["delegates"]
                ):
                    facts["delegates"].append(delegate)
            for call in found["local_calls"]:
                key = (call["name"].casefold(), call["path"].casefold())
                if not any((c["name"].casefold(), c["path"].casefold()) == key for c in facts["local_calls"]):
                    facts["local_calls"].append(call)
            for field in ("global_calls", "dynamic_calls"):
                for name in found[field]:
                    if not any(existing.casefold() == name.casefold() for existing in facts[field]):
                        facts[field].append(name)
            if source_rank.get(found["attrs_source"], 0) > source_rank.get(facts["attrs_source"], 0):
                facts["attrs_source"] = found["attrs_source"]
        # Если в одном entrypoint набор лишь создан, а в другом для того же регистра видна
        # запись, итоговый факт должен быть сильнейшим и не противоречить сам себе.
        facts["record_sets_created"] = [r for r in created_candidates if r not in facts["record_sets"]]

        parts = [_POSTING_PREAMBLE]
        step = 1
        primary_note = (
            "CFE-замена может подавить это тело, поэтому его записи НЕ включены в ФАКТЫ ниже без "
            "явно прослеженного продолжения вызова; читай для проверки. "
            if replacement_annotations and not main_continuation_visible
            else "сервер его уже разобрал, ФАКТЫ ниже; читай, если хочешь увидеть глазами. "
        )
        parts.append(
            f"({step}) body = read_procedure({handler_path!r}, 'ОбработкаПроведения') -> тело обработчика "
            f"({primary_note})"
        )
        step += 1
        for idx, entry in enumerate(entrypoints[1:], start=1):
            annotation = entry.get("annotation") or "перехват"
            parts.append(
                f"({step}) cfe_body_{idx} = read_procedure({entry['path']!r}, {entry['method']!r}) -> "
                f'тело CFE-перехвата &{annotation}("ОбработкаПроведения"); сервер включил его в ФАКТЫ ниже. '
            )
            step += 1
        if replacement_annotations:
            labels = ", ".join(f"&{name}" for name in sorted(replacement_annotations))
            if main_continuation_visible:
                parts.append(
                    f"CFE-ЗАМЕНА ({labels}): во всех точных процедурах замены виден прямой "
                    "ПродолжитьВызов/ProceedWithCall, поэтому main-handler включен в возможные ФАКТЫ. "
                )
            else:
                parts.append(
                    f"CFE-ЗАМЕНА ({labels}): исходная ОбработкаПроведения не считается "
                    "выполненной — прямой ПродолжитьВызов/ProceedWithCall хотя бы в одной точной процедуре замены "
                    "не найден. При динамическом продолжении проследи путь по показанным телам. "
                )

        # БЮДЖЕТ РАЗМЕРА: развернутые маршруты ограничены, но имена record set
        # раньше оставались без потолка и могли сами вытолкнуть хвост за ~15К. Одна
        # компактная страница теперь делится между ВСЕМИ длинными списками: записанными
        # и только созданными наборами, а также вызовами сверх route-budget. Смещение
        # остается прежним публичным posting_calls_offset, поэтому старые continuation-
        # вызовы совместимы, а новый hint всегда даёт точный offset следующего окна.
        _MAX_DELEGATE_ROUTES = 6
        _MAX_CALL_ROUTES = 6
        overflow_delegates = facts["delegates"][_MAX_DELEGATE_ROUTES:]
        overflow_local = facts["local_calls"][_MAX_CALL_ROUTES:]
        overflow_global = facts["global_calls"][_MAX_CALL_ROUTES:]
        compact_entries: list[tuple[str, str]] = (
            [("record_written", r) for r in facts["record_sets"]]
            + [("record_created", r) for r in facts["record_sets_created"]]
            + [("call", f"{d['receiver']}.{d['method']}") for d in overflow_delegates]
            + [("call", f"{c['name']}() [локальный]") for c in overflow_local]
            + [("call", f"{n}() [без точки]") for n in overflow_global]
        )
        compact_offset = max(0, int(posting_calls_offset))
        compact_page: list[tuple[str, str]] = []
        compact_chars = 0
        # Count-cap preserves the existing calls-only continuation offset (=40 for
        # 50 calls); char-cap bounds pages with unusually long valid BSL identifiers.
        for entry in compact_entries[compact_offset:]:
            entry_chars = len(entry[1]) + 3
            if compact_page and (len(compact_page) >= 40 or compact_chars + entry_chars > 2400):
                break
            compact_page.append(entry)
            compact_chars += entry_chars
        compact_page_end = compact_offset + len(compact_page)
        paged_record_sets = [value for kind, value in compact_page if kind == "record_written"]
        paged_record_sets_created = [value for kind, value in compact_page if kind == "record_created"]
        paged_calls = [value for kind, value in compact_page if kind == "call"]
        compact_has_more = compact_page_end < len(compact_entries)

        if paged_record_sets:
            regs = ", ".join(paged_record_sets)
            # «Идти дальше некуда» — правда ТОЛЬКО когда кроме прямой записи в теле ничего нет.
            # Обработчик законно пишет один регистр набором И делегирует остальные движения;
            # безусловная фраза противоречила бы соседнему абзацу этого же hint (который делегата
            # показывает), и агент, поверивший первой инструкции, потерял бы остальные движения.
            # Набор, СОЗДАННЫЙ без видимой записи, — тоже «еще есть куда идти»: его судьба не
            # прослежена, и финал «некуда» рядом с такой развилкой был бы самопротиворечием.
            more = bool(
                facts["delegates"]
                or facts["local_calls"]
                or facts["global_calls"]
                or facts["dynamic_calls"]
                or facts["record_sets_created"]
                or compact_has_more
            )
            closing = (
                "но выдача ЭТИМ НЕ исчерпана — ниже есть другие вызовы или точный переход "
                "к следующей компактной странице. "
                if more
                else "делегата нет, идти дальше некуда. "
            )
            parts.append(
                f"ЗАПИСЬ РЕГИСТРОВ ПРЯМО В ОБРАБОТЧИКЕ (набор/менеджер записи: после создания виден "
                f"Записать() по нему): {regs} -> регистры УЖЕ НАЗВАНЫ, {closing}"
                "ВНИМАНИЕ: find_register_writers их НЕ НАЙДЕТ (он ищет только прямые "
                f"Движения.X в ObjectModule документов) — статические reverse-кандидаты ищи через "
                f"{_register_search_route()}, затем проверяй живой вызывающий путь. "
            )

        if paged_record_sets_created:
            regs_created = ", ".join(paged_record_sets_created)
            # Создание — НЕ запись: СоздатьНаборЗаписей()/СоздатьМенеджерЗаписи() не меняют регистр
            # до вызова Записать() (`Набор.Прочитать()` — чтение). Прежняя версия объявляла ЗАПИСЬЮ
            # само создание — ложный «факт» на каждом чтении набора в обработчике. Формулировка
            # честно про ВИДИМОСТЬ: Записать() мог уехать в метод, куда набор передан параметром.
            parts.append(
                f"НАБОР/МЕНЕДЖЕР ЗАПИСИ СОЗДАН, НО Записать() ПО НЕМУ НЕ ВИДНО (смотрим от создания "
                f"до следующего присваивания той же переменной): {regs_created}. "
                "СОЗДАНИЕ — ЕЩЕ НЕ ЗАПИСЬ (Прочитать() — чтение), фактом записи это НЕ считай. "
                "Набор мог уйти параметром в другой метод — проверь вызовы ниже и само тело (шаг 1). "
            )

        # На каждый вызов строится крупный маршрут, поэтому подробно раскрывается только
        # фиксированное число вызовов. Остальные имена уже включены в compact_page вместе
        # с record set facts: теряется только повторяющийся шаблон маршрута.

        for d in facts["delegates"][:_MAX_DELEGATE_ROUTES]:
            recv, meth, kind = d["receiver"], d["method"], d["kind"]
            label = _KIND_LABEL[kind]
            # У цепочки свойств «модулем-однофамильцем» может быть только ПОСЛЕДНЕЕ звено — ровно
            # его агент подставил бы в find_definition. У одиночного получателя это он сам.
            module_ref = f"ОбщийМодуль.{recv.split('.')[-1]}"
            if kind == "manager_module":
                manager_path = d["module_path"]
                parts.append(
                    f"ДЕЛЕГАТ: {recv}.{meth}(...) — {label}; экспортное объявление подтверждено "
                    f"по живому файлу {manager_path!r}, поэтому это не платформенный manager-вызов. "
                    f"({step}) read_procedure({manager_path!r}, {meth!r}) -> тело делегата. "
                )
                step += 1
            elif kind == "manager_unverified":
                route_call = _decl_search_call(meth)
                profile_note = (
                    f"Compact-профиль чужие ManagerModule живьем не открывает; перепроверь через "
                    f"find_register_movements({document_name!r}). "
                    if profile
                    else "Живой ManagerModule прочитать/разобрать не удалось. "
                )
                if route_call is not None:
                    parts.append(
                        f"ВЫЗОВ {recv}.{meth}(...): {label}. {profile_note}"
                        f"Не считай его платформенным без проверки: ({step}) {route_call} -> "
                        f"ищи объявление метода по всему доступному дереву ({_decl_search_note()}). "
                    )
                    step += 1
                else:
                    parts.append(
                        f"ВЫЗОВ {recv}.{meth}(...): {label}. {profile_note}"
                        f"Не считай его платформенным без проверки; {_decl_search_route(meth)}. "
                    )
            elif kind == "common_module":
                # Факт разрешен ТОЛЬКО live-источнику (структурный потолок в классификации),
                # поэтому формулировка одна и не нуждается в оговорках «может отставать»:
                # оговорка не чинит классификацию, а факт со звездочкой — все равно ложь на
                # stale-снимке. Слабые источники сюда не доходят — они дают развилку.
                checked = (
                    "не реквизит и не табличная часть документа (сверено по ЖИВОМУ XML, включая XML "
                    "расширений), а общий модуль с таким именем в конфигурации ЕСТЬ. "
                )
                parts.append(
                    f"ДЕЛЕГАТ: {recv}.{meth}(...) — получатель '{recv}' это {label}. Проверено ЗДЕСЬ, по живому "
                    f"модулю: он не переменная и не параметр этого модуля; {checked}"
                    f"({step}) d = find_definition({meth!r}, {module_ref!r}) -> определение делегата (префикс "
                    "категории обязателен: голое имя category-blind, и одноименный справочник дал бы ЧУЖОЕ тело). "
                )
                step += 1
                parts.append(
                    f"({step}) read_procedure(d['definitions'][0]['file'], {meth!r}) if d.get('definitions') else None "
                    "-> тело делегата. Guard обязателен: пустой результат это definitions=[] (НЕ ошибка), а без "
                    f"индекса ключа 'definitions' нет вовсе. Пусто -> делегат новее индекса: "
                    f"{_decl_search_route(meth, known_module=recv.split('.')[-1])}. "
                    "НЕ ПОДТВЕРЖДАЙ результат проверкой category == 'CommonModules': префикс 'ОбщийМодуль.' уже "
                    "фильтрует запрос по ЭТОЙ ЖЕ категории в SQL, поэтому проверка ВСЕГДА истинна и не подтверждает "
                    "НИЧЕГО. Получателя уже разрешил сервер — см. выше. "
                )
                step += 1
            elif kind == "module_unverified":
                # Развилка, а не факт. Причина зависит от источника: 'index' — снимок мог отстать
                # от XML (успешный SQL-запрос полноту не доказывает), 'none' — проверить нечем
                # вовсе. В обоих случаях реквизит-однофамилец затенил бы модуль, и find_definition
                # молча отдал бы ЧУЖОЕ тело — поэтому решение отдается агенту с исполнимым шагом.
                if facts["attrs_source"] == "index":
                    reason = (
                        "но реквизиты сверены только ПО ИНДЕКСУ, который может отставать от XML "
                        "(реквизит, добавленный после сборки, отсюда НЕ виден — успешный запрос к "
                        "индексу полноту снимка не доказывает), а реквизит "
                    )
                elif facts["attrs_source"] == "live_partial":
                    reason = (
                        "но метаданные РАСШИРЕНИЙ прочитать/разобрать не удалось (проверка реквизитов "
                        "НЕПОЛНАЯ: реквизит, добавленный расширением, мог остаться невидимым), а реквизит "
                    )
                else:
                    reason = "но РЕКВИЗИТЫ документа серверу проверить НЕЧЕМ, а реквизит "
                # Ни ПОЛОЖИТЕЛЬНЫЙ, ни отрицательный ответ get_object_full_structure здесь не
                # классифицирует получателя: при index_used=True это все тот же снимок, который
                # может и отставать от XML, и ОПЕРЕЖАТЬ его (удаленный реквизит останется в
                # индексе). Прежний положительный шаг «есть -> это РЕКВИЗИТ» делал именно такую
                # stale-запись ложным фактом и уводил от настоящего общего модуля. Безопасный
                # маршрут в обоих мирах — поиск объявления по всему дереву; профилю дополнительно
                # дается find_register_movements — единственная полная live-перепроверка, включая
                # расширения (хелперу не предлагаем самого себя: это был бы цикл).
                recheck = (
                    (
                        f"Самая точная перепроверка — find_register_movements({document_name!r}): он сверяет "
                        "реквизиты ЖИВЬЕМ (включая расширения), и его hint разрешит получателя. "
                    )
                    if profile
                    else ""
                )
                parts.append(
                    f"ВЫЗОВ {recv}.{meth}(...): {label}. Переменной/параметром ЭТОГО модуля получатель не "
                    f"затенен (проверено по телу), {reason}"
                    f"'{recv}' затенил бы одноименный общий модуль — и find_definition молча отдал бы ЧУЖОЕ "
                    "тело. Снимок можно посмотреть, но НЕ классифицируй получателя по нему: "
                    f"({step}) s = get_object_full_structure({document_name!r}) -> и наличие, и отсутствие "
                    f"'{recv}' среди attributes/tabular_sections НИЧЕГО НЕ ДОКАЗЫВАЕТ о ЖИВОМ коде. При "
                    "index_used=True get_object_full_structure читает ТОТ ЖЕ индекс (флаг говорит об источнике, "
                    "не о полноте): он может не видеть свежий реквизит или, наоборот, помнить уже УДАЛЕННЫЙ. "
                    "Поэтому при любом ответе НЕ ходи в find_definition по 'ОбщийМодуль.<получатель>' — маршрут "
                    "один и тот же, "
                    f"по всему дереву: {_decl_search_route(meth)}; он найдет объявление и в общем модуле, "
                    f"если получатель был им. {recheck}"
                )
                step += 1
            elif kind == "shadow_risk":
                # Честная РАЗВИЛКА вместо удобного ответа. В BSL `=` это и присваивание, и
                # сравнение: `Если X = Неопределено Тогда` — сравнение, а `Тогда X = Получить();` —
                # присваивание. Позицию оператора мы разбираем, но общий случай не разрешаем, и
                # выдать догадку за факт нельзя НИ В ОДНУ сторону: назвать переменной — увести от
                # рабочего делегата; назвать модулем — молча подсунуть чужое тело.
                parts.append(
                    f"ВЫЗОВ {recv}.{meth}(...): {label}. В модуле встречается '{recv} =', но в позиции, где это "
                    "МОЖЕТ быть сравнением, а не присваиванием (в BSL '=' означает и то, и другое) — поэтому "
                    "получателя сервер НЕ разрешил и гадать не станет. РЕШИ ПО ТЕЛУ (шаг 1): "
                    f"если '{recv}' там ПРИСВАИВАЕТСЯ — это переменная, маршрут поиска объявления: "
                    f"{_decl_search_route(meth)}; "
                    f"если только СРАВНИВАЕТСЯ — это общий модуль: "
                    f"({step}) d = find_definition({meth!r}, {module_ref!r}) -> определение делегата, далее "
                    f"read_procedure(d['definitions'][0]['file'], {meth!r}) if d.get('definitions') else None. "
                )
                step += 1
            else:
                trap = ""
                if d["homonym_module"]:
                    trap = (
                        f"ЛОВУШКА: в конфигурации ЕСТЬ общий модуль-однофамилец '{recv}' — "
                        f"find_definition({meth!r}, {module_ref!r}) отработал бы УСПЕШНО и молча отдал ЕГО тело, "
                        "ЧУЖОЕ. Не ходи туда. "
                    )
                stale_note = ""
                if d.get("stale_homonym_module"):
                    stale_note = (
                        f"ИНДЕКС ПОМНИТ общий модуль-однофамилец '{recv}', но его файл ЖИВЬЕМ не "
                        "читается (удален/перемещен после сборки); считать модуль существующим и идти "
                        "в stale find_definition нельзя. "
                    )
                platform_note = ""
                if d.get("platform_method_name"):
                    # Вызов показан, потому что источник получателя НЕ прослежен, — но имя метода
                    # платформенное, и молчать об этой двусмысленности значило бы отправить агента
                    # искать «Процедуру Записать» там, где была платформенная запись.
                    platform_note = (
                        f"NB: имя '{meth}' совпадает с платформенным методом. Если получатель создан "
                        "платформенной фабрикой (Создать(НаборЗаписей|МенеджерЗаписи) / Новый ...) — это "
                        "платформенная запись, а не делегат; здесь источник получателя НЕ прослежен, поэтому "
                        "вызов показан. "
                    )
                force_live_decl = bool(d.get("stale_homonym_module"))
                route_call = _live_decl_search_call(meth) if force_live_decl else _decl_search_call(meth)
                if route_call is not None:
                    parts.append(
                        f"ВЫЗОВ {recv}.{meth}(...): получатель '{recv}' — {label}, а НЕ общий модуль. "
                        f"{trap}{stale_note}{platform_note}"
                        "Тип получателя по имени не восстановить, поэтому ищи ОБЪЯВЛЕНИЕ метода ПО ВСЕМУ ДЕРЕВУ: "
                        f"({step}) {route_call} -> объявление ({_decl_search_note(live=force_live_decl)}). "
                    )
                    step += 1
                else:
                    # Исполнимого исчерпывающего шага нет (не под git и без индекса) — честное
                    # ограничение вместо нумерованного шага с несуществующим хелпером.
                    parts.append(
                        f"ВЫЗОВ {recv}.{meth}(...): получатель '{recv}' — {label}, а НЕ общий модуль. "
                        f"{trap}{stale_note}{platform_note}"
                        f"Тип получателя по имени не восстановить; {_decl_search_route(meth)}. "
                    )

        for call in facts["local_calls"][:_MAX_CALL_ROUTES]:
            name, local_path = call["name"], call["path"]
            parts.append(
                f"ЛОКАЛЬНЫЙ ВЫЗОВ {name}(...): метод объявлен в ЭТОМ ЖЕ модуле (вызов без точки), find_definition "
                f"не нужен. ({step}) read_procedure({local_path!r}, {name!r}) -> его тело. "
            )
            step += 1

        for name in facts["global_calls"][:_MAX_CALL_ROUTES]:
            # Под git доступен полный live-каталог BSL: он нужен и без индекса, и когда snapshot
            # отстал и дал definitions=[]. Без git при живом индексе find_definition — единственный
            # полный маршрут; без git и без индекса повторный find_definition был бы циклом.
            if _want_git_search:
                no_index_fallback = (
                    f"Если d.get('definitions') пуст (индекса нет или snapshot отстал) — тогда "
                    f"{_decl_search_call(name)}. "
                )
            elif idx_reader is not None:
                no_index_fallback = ""
            else:
                no_index_fallback = (
                    "Индекса нет — find_definition вернет error 'no index', а git_search не "
                    "зарегистрирован (исходники не под git): сузь кандидатов через find_module и "
                    f"проверь каждого: safe_grep({name!r}, 'ИмяМодуля'). "
                )
            parts.append(
                f"ВЫЗОВ БЕЗ ТОЧКИ {name}(...): в ЭТОМ модуле такой метод НЕ объявлен, значит это экспортный метод "
                "ГЛОБАЛЬНОГО общего модуля либо метод глобального контекста платформы (в модуле объекта их нет). "
                f"({step}) d = find_definition({name!r}) -> кандидаты по всему дереву; выбирай по category "
                f"(глобальный общий модуль -> category='CommonModules'). {no_index_fallback}"
            )
            step += 1

        if facts["dynamic_calls"]:
            names = ", ".join(f"{name}(...)" for name in facts["dynamic_calls"])
            parts.append(
                f"ДИНАМИЧЕСКОЕ ВЫПОЛНЕНИЕ: {names}. Аргумент может содержать код создания и записи "
                "набора регистра, но строки намеренно вырезаны статическим разбором, а значение переменной "
                "без dataflow не восстановить. Поэтому отрицательный вывод о движениях ЗАПРЕЩЕН: проследи "
                "аргумент динамического вызова по телам из нумерованных шагов выше. "
            )

        if paged_calls:
            listed = "; ".join(paged_calls)
            parts.append(
                f"ЕЩЕ ВЫЗОВЫ ИЗ ОБРАБОТЧИКА — всего "
                f"{len(overflow_delegates) + len(overflow_local) + len(overflow_global)} шт. без "
                f"развернутого маршрута; текущая компактная страница: {listed}. Лимит "
                f"{_MAX_DELEGATE_ROUTES} делегатов и {_MAX_CALL_ROUTES} вызовов на категорию, иначе hint "
                "обрезался бы лимитом вывода. "
                "Маршруты — как в шагах выше: локальный -> read_procedure по этому же пути; делегат -> "
                "тот же маршрут поиска объявления, что в шагах выше. "
            )

        if compact_entries and (compact_has_more or compact_offset > 0):
            navigation: list[str] = []
            if compact_has_more:
                next_offset = 0 if profile else compact_page_end
                navigation.append(
                    "следующая страница: "
                    f"find_register_movements({document_name!r}, posting_calls_offset={next_offset})"
                )
            if compact_offset > 0:
                navigation.append(f"к началу: find_register_movements({document_name!r}, posting_calls_offset=0)")
            page_range = f"{compact_offset + 1}–{compact_page_end}" if compact_page else f"offset={compact_offset}"
            parts.append(
                f"КОМПАКТНЫЕ ФАКТЫ/ВЫЗОВЫ: элементы {page_range} из {len(compact_entries)}; {'; '.join(navigation)}. "
            )

        if (
            not facts["record_sets"]
            and not facts["record_sets_created"]
            and not facts["delegates"]
            and not facts["local_calls"]
            and not facts["global_calls"]
            and not facts["dynamic_calls"]
        ):
            parts.append(
                "В теле обработчика НЕ НАЙДЕНО ни наборов записей, ни вызовов-делегатов: судя по коду, движений "
                "он не пишет. Это ЗАКОННЫЙ ответ, а не ошибка — но разбор смотрит только тело обработчика, поэтому "
                "при сомнении прочитай его сам (шаг 1). "
            )

        tail = _POSTING_TAIL
        if not _want_git_search:
            # Хвост — та же поверхность, что и шаги: советовать незарегистрированный git_search
            # нельзя и здесь. safe_grep живой (git/индекс не нужны), но ходит только по BSL.
            tail = tail.replace(
                "ищи имя регистра через git_search (он идет по ВСЕМУ дереву и любым типам файлов). ",
                f"ищи имя регистра через {_register_search_route()}. ",
            )
        parts.append(tail)
        hint = "".join(parts)
        if profile:
            hint += _POSTING_PROFILE_TAIL.format(doc=document_name)
        return hint

    def _live_posting_signal(
        document_name: str, *, index_prefilter: bool = False
    ) -> tuple[str, str, list[dict]] | None:
        """``(rel_path, тело модуля, CFE-перехваты)``, если сигнал подтвержден ЖИВЬЁМ:
        ОбработкаПроведения объявлена И прямых ``Движения.<Регистр>`` в модуле НЕТ. Иначе None.

        Тело возвращается ВМЕСТЕ с путём (оно уже прочитано — см. ``_live_body``), потому что
        разбор получателя делает СЕРВЕР: агент прочитать этот модуль может не суметь вовсе —
        обработчик бывает только в CFE-расширении, а туда песочный ``read_file`` не пускает.

        ЕДИНСТВЕННАЯ точка истины для обоих маршрутов (find_register_movements и секция
        registers профиля). Держать проверки раздельно уже дважды приводило к тому, что
        половины конъюнкции разъезжались по источникам и сигнал лгал: сперва «движений нет»
        брали из индекса, а «обработчик есть» — живьём; потом это починили в хелпере, но
        забыли в профиле. Обе половины обязаны читаться по ОДНОМУ живому телу — структурно,
        а не по договорённости.

        Возвращаем ПУТЬ, а не bool: у документа может быть несколько точных ObjectModule
        (main + CFE-расширение), и тогда ``module_hint='Документ.X'`` резолвится неоднозначно,
        а find_call_hierarchy уходит в fallback-режим. Точный rel_path — самая сильная форма
        hint, которую call-hierarchy уже поддерживает.

        **Кандидат ВСЕГДА подтверждается живым кодом** — в ОБОИХ маршрутах, одной и той же
        проверкой. Верить тут индексу нельзя: общий парсер методов применяет
        ``BSL_PATTERNS["procedure_def"]`` неякорным ``.search()`` к СЫРОЙ строке, поэтому
        билдер кладёт в таблицу ``methods`` даже закомментированную
        ``// Процедура ОбработкаПроведения()``. Доверившись ей, профиль заявил бы обработчик,
        которого нет, — и разошёлся бы с ``find_register_movements`` НА ОДНОМ И ТОМ ЖЕ свежем
        индексе. ``extract_procedures``/``_parse_procedures`` тоже не годятся: первый
        объединяет индекс с live-fill (а live-fill только ДОБАВЛЯЕТ методы и не убирает
        исчезнувшие → «помнит» уже удалённую процедуру), второй наследует ту же неякорность.
        Матчим анкорным ``_POSTING_HANDLER_DECL_RE`` (ключевые слова — как в BSL_PATTERNS,
        включая английские Procedure/Function; якорь ``^\\s*`` отсекает и комментарий, и текст
        внутри строкового литерала: в 1С строка-продолжение всегда начинается с ``|``).

        ``index_prefilter`` — ТОЛЬКО про то, скольких кандидатов мы открываем В ПОИСКЕ
        ОБРАБОТЧИКА (фаза 1), а не про строгость проверки (она одна и та же):
          * ``False`` (find_register_movements): открываем КАЖДОГО точного кандидата →
            self-healing, обработчик, дописанный после сборки индекса, будет найден;
          * ``True`` (get_object_profile): сперва дешёвый отсев по индексу
            (``get_methods_by_path``) — модуль, на который индекс не указал, в фазе 1 не
            открываем. Плата: на устаревшем индексе профиль может ПРОМОЛЧАТЬ (метода ещё нет
            в ``methods``) — это безопасное направление; соврать он не может.

        **ВНИМАНИЕ про стоимость:** index-отсев действует ТОЛЬКО в фазе 1. Фаза 2 (движений
        нет) открывает ВСЕ точные ObjectModule документа, включая index-negative, — иначе
        движения, дописанные в НЕ-индексный модуль, остались бы незамеченными и конъюнкция
        снова собралась бы из разных источников. Но фаза 2 запускается ТОЛЬКО когда обработчик
        уже подтверждён, то есть на общем пути (у документа есть движения / индекс не знает
        обработчика) не открывается НИ ОДИН файл. Тело каждого модуля читается однократно
        за вызов (см. ``_live_body``).

        find_by_type здесь НЕ годится: он матчит имя ПОДСТРОКОЙ и режет выдачу на 50
        элементах — то есть даёт не только ложного омонима (это лечится post-фильтром), но и
        FALSE-NEGATIVE: нужный документ может не попасть в первые 50 подстрочных кандидатов.
        Для identity-sensitive кода main-кандидаты берутся из текущего CF/EDT-дерева, а
        точные CFE-кандидаты — из live side-load расширений в _index_state.
        """
        target = (document_name or "").casefold()
        if not target:
            return None
        _ensure_index()
        main_candidates = _live_main_object_module_paths(document_name)
        extension_candidates = sorted(
            rel_path
            for rel_path, info in _index_state
            if rel_path in _extension_paths_set
            and (info.category or "") == "Documents"
            and info.module_type == "ObjectModule"
            and (info.object_name or "").casefold() == target  # ТОЧНО: без подстроки и без cap-50
        )
        candidates = [*main_candidates, *extension_candidates]
        if not candidates:
            return None

        bodies: dict[str, str] = {}

        def _live_body(rel_path: str) -> str | None:
            """Тело модуля, прочитанное РОВНО ОДИН раз за вызов (обе половины смотрят на него)."""
            if rel_path in bodies:
                return bodies[rel_path]
            if rel_path in _extension_paths_set:
                try:
                    body = _ext_read_file(rel_path)
                except Exception:
                    return None
            else:
                body = _live_main_object_module_body(rel_path)
                if body is None:
                    return None
            bodies[rel_path] = body
            return body

        # --- Половина 1: где ЖИВЬЁМ объявлен обработчик ---
        # Анкорный матч по СЫРОМУ телу: якорь ^\s* сам отсекает и `// Процедура ...`, и текст
        # внутри строкового литерала (строка-продолжение в 1С всегда начинается с `|`), а стоит
        # это ~1 мс против ~85 мс полного вырезания комментариев.
        handler_path: str | None = None
        for rel_path in candidates:
            if index_prefilter:
                # Дешёвый отсев: без подсказки индекса модуль даже не открываем.
                if idx_reader is None:
                    continue
                try:
                    procs = idx_reader.get_methods_by_path(rel_path) or []
                except Exception:
                    continue
                if not any((p.get("name") or "").casefold() == "обработкапроведения" for p in procs):
                    continue
            body = _live_body(rel_path)
            if body is None:
                continue
            if _POSTING_HANDLER_DECL_RE.search(body):
                handler_path = rel_path
                break  # обработчик мог уехать в CFE — поэтому перебор, а не ранний выход
        if handler_path is None:
            return None

        # --- Половина 2: прямых ВЫПОЛНЯЕМЫХ Движения.X нет НИ В ОДНОМ модуле документа ---
        # Именно НИ В ОДНОМ, а не только в модуле обработчика: у документа бывает несколько
        # точных ObjectModule (main + CFE), и движения могли остаться в ДРУГОМ. Проверять
        # только модуль обработчика — значит снова собрать конъюнкцию из разных источников
        # (индекс про остальные модули + живой файл про этот) и снова соврать на устаревшем
        # индексе. Читаем всех кандидатов — но ТОЛЬКО когда уже собрались выставить сигнал,
        # то есть на общем пути лишних чтений нет, а модули эти уже в кеше.
        # Комментарии/строки тут вырезаем: у `Движения.X` якоря нет, и `// Движения.X` (или тот
        # же текст в запросе) иначе сошёл бы за обращение и отнял бы у агента верный сигнал.
        for rel_path in candidates:
            body = _live_body(rel_path)
            if body is None:
                return None  # не смогли прочитать модуль документа — молчим, а не гадаем

        interceptors: list[dict] = []
        seen_interceptors: set[tuple[str, str, str]] = set()
        for rel_path in candidates:
            for interceptor in _posting_interceptors_for_module(rel_path, bodies.get(rel_path) or ""):
                key = (
                    interceptor["path"].casefold(),
                    interceptor["method"].casefold(),
                    interceptor["annotation"].casefold(),
                )
                if key not in seen_interceptors:
                    seen_interceptors.add(key)
                    interceptors.append(interceptor)

        # A CFE replacement without a visible ProceedWithCall suppresses the main handler.
        # Therefore a live ``Движения.X`` in main is not an active movement and must not
        # veto the replacement-aware hint.  CFE movements always remain active; with a
        # visible continuation main rows keep the ordinary possible-execution semantics.
        _active, _suppressed, replacement_meta = _apply_cfe_posting_replacement([], interceptors)
        ignore_main_movements = bool(replacement_meta and not replacement_meta["main_handler_continuation_visible"])
        suppressible_main_rows = (
            _main_handler_only_movement_keys(document_name, bodies) if ignore_main_movements else set()
        )
        for rel_path in candidates:
            for movement in _MOVEMENTS_LIVE_RE.finditer(_live_code_only(bodies.get(rel_path) or "")):
                if movement.group(1).lower() in _MOVEMENT_METHOD_NOISE:
                    continue
                movement_key = (
                    rel_path.replace("\\", "/").casefold(),
                    movement.group(1).casefold(),
                )
                if ignore_main_movements and movement_key in suppressible_main_rows:
                    continue
                return None  # выполняемое движение ЕСТЬ живьём → handler-сигнала быть не должно
        return handler_path, (bodies.get(handler_path) or ""), interceptors

    def _maybe_add_posting_handler_hint(result: dict, document_name: str, posting_calls_offset: int = 0) -> None:
        """find_register_movements: обработчик есть, `Движения.X` нет → ФАКТ + условный hint.

        ПРИОРИТЕТ Posting=Deny. Полагаться на то, что его уже выставил
        _maybe_add_postability_hint, НЕЛЬЗЯ: тот гейтится ПОЛНОЙ пустотой результата
        (code_registers И erp_mechanisms И manager_tables И adapted_registers). Проверка здесь
        выполняется ДО раннего выхода и по code_registers: Deny обязан пометить статические
        ссылки недостижимыми, а не только подавить handler-сигнал на пустом результате.
        Поэтому постановку проверяем ЗДЕСЬ явно (live-XML — find_register_movements и так
        читает файлы; _check_document_postable мемоизирован, XML не парсится дважды).
        """
        if result.get("hint"):
            return
        info = _check_document_postable(document_name)
        if info.get("is_postable") is False:
            # Preserve provenance-bearing static rows, but mark them unreachable: the
            # platform will not invoke posting for a Deny document.
            result.setdefault("posting", info.get("posting"))
            result["is_postable"] = False
            result["hint"] = (
                "Документ непроводимый (Posting=Deny) — движений регистров при проведении нет. "
                "Непустые code_registers/manager_tables в этом ответе — только статические ссылки "
                "из недостижимого обработчика или снимка, а не выполняемые движения. "
                "Связь с регистрами ищите через find_event_subscriptions / "
                "регистры сведений с типом источника = документ."
            )
            return
        suppressed_main = result.get("suppressed_main_code_registers") or []
        if result.get("code_registers"):
            if suppressed_main:
                result["hint"] = _replacement_hint(suppressed_main)
            return
        # ОБЕ половины конъюнкции проверяет _live_posting_signal — по ОДНОМУ живому телу и той
        # же проверкой, что и профиль. Отдельной перепроверки движений здесь БОЛЬШЕ НЕТ: пока
        # половины жили в разных местах, они дважды успели разъехаться по источникам.
        found = _live_posting_signal(document_name, index_prefilter=False)
        if not found:
            if suppressed_main:
                # Live analysis may be unavailable/partial, but the exact CFE annotation
                # and the absence of a direct continuation were already established while
                # reading the same module for movement extraction.  Preserve that narrower
                # fact rather than silently re-promoting suppressed main rows.
                result["hint"] = _replacement_hint(suppressed_main)
            return
        handler_path, module_body, interceptors = found
        result["posting_handler_present"] = True
        result["hint"] = _build_posting_hint(
            document_name,
            handler_path,
            module_body,
            profile=False,
            interceptors=interceptors,
            posting_calls_offset=posting_calls_offset,
        )

    def find_register_writers(register_name: str) -> dict:
        """Find static document references to a specific register.
        Searches all document ObjectModules for 'Движения.RegisterName'. CFE
        replacement reachability and Posting=Deny are intentionally not applied.
        ``find_register_movements(document)`` applies those filters, but main code
        rows there still come from the index snapshot.

        Args:
            register_name: Register name to search for.

        Returns: dict with register, writers, total_documents_scanned, total_writers,
                 runtime_filtered=False, and an interpretation hint."""
        register_name = _strip_meta_prefix(register_name)
        runtime_hint = (
            "Статические ссылки из кода/индекса: CFE-замены и Posting=Deny здесь не применяются. "
            "find_register_movements(document) применяет эти фильтры, но main-строки там остаются "
            "снимком индекса; после изменения main-кода проверь живой файл кандидата."
        )

        # Fast path: SQLite index
        if idx_reader is not None:
            idx_writers = idx_reader.get_register_writers(register_name)
            if idx_writers is not None:
                return {
                    "register": register_name,
                    "writers": [
                        {"document": w["document_name"], "source": w["source"], "file": w["file"]} for w in idx_writers
                    ],
                    "total_documents_scanned": 0,
                    "total_writers": len(idx_writers),
                    "runtime_filtered": False,
                    "hint": runtime_hint,
                }

        _ensure_index()
        # Collect all document ObjectModule files
        doc_modules = [
            (rel, info)
            for rel, info in _index_state
            if info.category and info.category.lower() == "documents" and info.module_type == "ObjectModule"
        ]

        needle = f"движения.{register_name}".lower()
        matched = _parallel_prefilter(doc_modules, needle, base_path)

        # Tail (?!\s*\() rejects the method-call form Движения.Записать() so a reverse lookup by
        # a noise name yields nothing; a real register is never immediately followed by '('.
        movement_re = re.compile(r"Движения\." + re.escape(register_name) + r"(?!\s*\()", re.IGNORECASE)
        writers: list[dict] = []
        for rel, info in matched:
            try:
                content = _ext_read_file(rel)
            except Exception:
                continue
            lines: list[int] = []
            for i, line in enumerate(content.splitlines(), 1):
                if movement_re.search(line):
                    lines.append(i)
            if lines:
                writers.append(
                    {
                        "document": info.object_name or "",
                        "file": rel,
                        "lines": lines,
                    }
                )

        return {
            "register": register_name,
            "writers": writers,
            "total_documents_scanned": len(doc_modules),
            "total_writers": len(writers),
            "runtime_filtered": False,
            "hint": runtime_hint,
        }

    def analyze_document_flow(document_name: str) -> dict:
        """Full document lifecycle analysis: metadata, event subscriptions,
        register movements, related scheduled jobs, based-on, print forms.

        v1.10.0 enrichment (BUG-6 fix): для непроводимых документов добавлены
        top-level is_postable=False + hint, чтобы агенту не нужно было лезть
        внутрь register_movements; based_on/print_forms обогащают результат
        для всех документов.

        Args:
            document_name: Document name (or fragment).

        Returns: dict with document, metadata, event_subscriptions,
                 register_movements, related_scheduled_jobs, based_on,
                 print_forms; для непроводимых дополнительно is_postable+hint."""
        document_name = _strip_meta_prefix(document_name)
        obj = analyze_object(document_name)
        subs = find_event_subscriptions(document_name)
        movements = find_register_movements(document_name)

        # Find scheduled jobs referencing this document
        all_jobs = find_scheduled_jobs()
        doc_lower = document_name.lower()
        related_jobs = [
            j
            for j in all_jobs
            if doc_lower in j.get("method_name", "").lower() or doc_lower in j.get("name", "").lower()
        ]

        # Composition graceful-degrade — каждый суб-вызов в try/except,
        # чтобы один сломавшийся хелпер не валил весь analyze_document_flow.
        try:
            based_on = find_based_on_documents(document_name)
        except Exception as exc:
            based_on = {"error": f"{type(exc).__name__}: {exc}"}
        try:
            print_forms_data = find_print_forms(document_name)
        except Exception as exc:
            print_forms_data = {"error": f"{type(exc).__name__}: {exc}"}

        result: dict = {
            "document": obj.get("name", document_name),
            "metadata": obj.get("metadata", {}),
            "event_subscriptions": subs,
            "register_movements": movements,
            "related_scheduled_jobs": related_jobs,
            "based_on": based_on,
            "print_forms": print_forms_data,
        }

        # Top-level is_postable+hint для непроводимых — повторяем сигнал из
        # register_movements на верхний уровень для удобства агента.
        if isinstance(movements, dict) and movements.get("is_postable") is False:
            result["is_postable"] = False
            result["hint"] = (
                "Документ непроводимый (Posting=Deny). Строки в register_movements, если они есть, "
                "являются статическими ссылками из недостижимого обработчика, а не runtime-движениями. "
                "Связь с регистрами — через event_subscriptions, based_on "
                "или регистры сведений с типом-источником = документ."
            )

        return result

    # ── Based-on documents / Print forms helpers ───────────────

    def find_based_on_documents(document_name: str) -> dict:
        """Find what documents can be created FROM this document and what it can be created FROM.

        Прямой обход:
          - can_create_from_here:  ManagerModule.ДобавитьКомандыСозданияНаОсновании самого документа.
          - can_be_created_from:   ObjectModule.ОбработкаЗаполнения самого документа.

        Обратный обход (v1.10.x — BUG-9 fix): если прямой обход НИЧЕГО не нашёл
        для can_create_from_here (типичный кейс — Письма в ДО3: у них нет
        ДобавитьКомандыСозданияНаОсновании, но другие документы — Задача,
        Поручение и т.п. — декларируют ДокументСсылка.ВходящееПисьмо в своих
        ОбработкаЗаполнения), сканируется ОбработкаЗаполнения всех остальных
        ObjectModule в Documents/ и собираются документы, у которых ссылка на
        наш `document_name` есть в этой процедуре. Записи помечаются
        `"via": "back_scan"`.

        Вход category-aware: bare-имя и `Документ./Document.` → полный обход (FS + метаданные);
        типизированный НЕ-документ (`Справочник.X`) → только метаданные, document-specific
        сканы пропускаются (иначе при омонимичном Document.X подмешались бы связи документа).

        Returns: dict with document, can_create_from_here, can_be_created_from.
        Для прямых code-derived записей ``via`` отсутствует (backcompat; отсутствие означает
        direct); back_scan/metadata помечены явно. Metadata несет также category + canonical
        ref — Catalog-основания и омонимы."""
        # Canonical category is resolved BEFORE the FS walk (#3, code-review): every scan
        # below is document-specific (Documents/<name>/Ext/*, ДокументСсылка.<name>), so a
        # typed non-document input (``Справочник.X``) must not run them — with a homonymous
        # Document.X in the config they would silently attach the DOCUMENT's bases to the
        # CATALOG's answer, i.e. break exactly the homonym case this change exists for.
        canon, _forms = _normalize_object_ref((document_name or "").strip())
        if canon and "." not in canon:
            canon = f"Document.{canon}"  # bare name → document (this helper is document-centric)
        is_document_input = (not canon) or canon.startswith("Document.")
        # Короткое имя — из canonical suffix, а НЕ из регистрозависимого _strip_meta_prefix:
        # "документ.X" канонизируется (casefold), но _strip_meta_prefix оставил бы префикс
        # на месте и FS-поиск ушёл бы искать объект "документ.X".
        document_name = canon.split(".", 1)[1] if canon and "." in canon else _strip_meta_prefix(document_name)
        result: dict = {
            "document": document_name,
            "can_create_from_here": [],
            "can_be_created_from": [],
        }
        metadata_complete = False
        metadata_incomplete_at_cap = False

        modules: list[dict] = []
        if is_document_input:
            # Полное имя документа задаёт identity, а не substring-запрос. Иначе обычная
            # пара ``Заказ``/``ЗаказКлиента`` смешивает процедуры двух объектов; чужой
            # direct-hit вдобавок отключает корректный back_scan для точного документа.
            # Если точного объекта нет, сохраняем исторический fragment-fallback.
            _ensure_index()
            exact_name = document_name.casefold()
            exact_modules = [
                _info_to_dict(relative_path, info)
                for relative_path, info in _index_state
                if (info.category or "").casefold() == "documents" and (info.object_name or "").casefold() == exact_name
            ]
            exact_document_exists = bool(exact_modules)
            if not exact_document_exists:
                # У документа законно может не быть НИ ОДНОГО BSL-модуля. Проверяем его
                # identity по точному live XML/MDO-пути; иначе пустой точный объект снова
                # провалился бы в fuzzy-ветку и получил модули префиксного соседа. Общий
                # glob здесь не используем: index-backed glob имеет диагностические строки
                # для некоторых нулевых запросов, которые не являются найденными файлами.
                try:
                    _resolve_object_xml(f"Documents/{document_name}")
                except Exception:
                    exact_document_exists = False
                else:
                    exact_document_exists = True
            modules = exact_modules if exact_document_exists else find_by_type("Documents", document_name)

        # --- ManagerModule: ДобавитьКомандыСозданияНаОсновании ---
        mgr_modules = [m for m in modules if m.get("module_type") == "ManagerModule"]
        for mod in mgr_modules:
            path = mod["path"]
            body = read_procedure(path, "ДобавитьКомандыСозданияНаОсновании")
            if body:
                create_re = re.compile(r"Документы\.(\w+)\.ДобавитьКоманду\w*НаОснован", re.IGNORECASE)
                for m in create_re.finditer(body):
                    result["can_create_from_here"].append(
                        {
                            "document": m.group(1),
                            "file": path,
                        }
                    )

        # --- ObjectModule: ОбработкаЗаполнения ---
        obj_modules = [m for m in modules if m.get("module_type") == "ObjectModule"]
        for mod in obj_modules:
            path = mod["path"]
            body = read_procedure(path, "ОбработкаЗаполнения")
            if body:
                type_re = re.compile(r'Тип\("(\w+Ссылка\.\w+)"\)', re.IGNORECASE)
                for m in type_re.finditer(body):
                    result["can_be_created_from"].append(
                        {
                            "type": m.group(1),
                            "file": path,
                        }
                    )

        # --- Reverse scan для can_create_from_here ---
        # Только если прямой обход ничего не нашёл — иначе дёшево пропускаем.
        # Ищется `ДокументСсылка.<name>` → осмысленно только для документо-входа
        # (для `Справочник.X` это ссылка на ОМОНИМИЧНЫЙ документ, а не на наш справочник).
        if is_document_input and not result["can_create_from_here"]:
            try:
                obj_paths = glob_files_fn("Documents/*/Ext/ObjectModule.bsl") or []
            except Exception:
                obj_paths = []

            doc_lower = document_name.lower()
            # Pattern: ДокументСсылка.<our_name> с границей слова, case-insensitive.
            ref_re = re.compile(rf"ДокументСсылка\.{re.escape(document_name)}\b", re.IGNORECASE)
            seen: set[str] = set()
            for raw_path in obj_paths:
                # Path может прийти с разными разделителями (Windows/POSIX) — нормализуем.
                segs = raw_path.replace("\\", "/").split("/")
                if len(segs) < 2 or segs[0] != "Documents":
                    continue
                other = segs[1]
                # Пропускаем ObjectModule самого документа (он уже обработан в прямом обходе)
                # и дубли по object_name.
                if other.lower() == doc_lower or other in seen:
                    continue
                try:
                    body = read_procedure(raw_path, "ОбработкаЗаполнения")
                except Exception:
                    continue
                if not body or not ref_re.search(body):
                    continue
                seen.add(other)
                result["can_create_from_here"].append(
                    {
                        "document": other,
                        "file": raw_path,
                        "via": "back_scan",
                    }
                )

        # --- Metadata union: declarative <BasedOn> (#3, v1.28.0) ---
        # The FS scan above only walks Documents/* → it misses Catalog (and other)
        # objects that declare our document as their <BasedOn> basis. Those live in the
        # index (metadata_references, kind='based_on'). Run STRICTLY AFTER direct+back_scan
        # (purely additive) — back_scan is gated on an empty can_create_from_here, so
        # injecting metadata earlier would suppress a real code-declared basis.
        if idx_reader is not None:
            # ``canon`` — canonical ref of the INPUT object, resolved up-front (see above):
            # an explicit Справочник./Catalog. keeps its category, a bare name is a Document.
            if canon:
                try:
                    # find_metadata_references (NOT find_references_to_object, which drops
                    # source_object) → each row's source_* is an object creatable from us.
                    # ЛИМИТ — ПО ФАКТИЧЕСКОМУ СЧЁТУ. Дефолтный limit=1000 у ридера предназначен
                    # для agent-facing выдачи, а здесь union ВНУТРЕННИЙ и обязан быть ПОЛНЫМ:
                    # на дефолте хвост оснований молча исчез бы из can_create_from_here (тихое
                    # усечение читается как «это всё» — ровно то, что мы чиним в get_overrides).
                    # Если счетчик недоступен (нет таблицы / транзиентный сбой), резервная
                    # выборка остается ограниченной. Ответ короче лимита полный; ровно лимит
                    # строк означает неизвестный хвост и явно помечается как partial ниже.
                    cnt = idx_reader.count_metadata_references(canon, kinds=["based_on"])
                    count_available = isinstance(cnt, dict) and cnt.get("total") is not None
                    total_based_on = int(cnt["total"]) if count_available else 0
                    metadata_limit = max(total_based_on, 1000)
                    raw_mrows = idx_reader.find_metadata_references(canon, kinds=["based_on"], limit=metadata_limit)
                    if raw_mrows is not None:
                        if count_available:
                            metadata_complete = len(raw_mrows) >= total_based_on
                            metadata_incomplete_at_cap = not metadata_complete
                        else:
                            metadata_complete = len(raw_mrows) < metadata_limit
                            metadata_incomplete_at_cap = not metadata_complete
                    mrows = raw_mrows or []
                except Exception:
                    mrows = []
                # Folder-category → singular canonical for the ``ref`` field. Reuse the
                # shared _CATEGORY_TO_TYPE_PREFIX (complete over ALL metadata_references
                # trigger categories — Tasks/BusinessProcesses/Reports/… also support Ввод
                # на основании), NOT a local subset that would emit non-canonical "Tasks.X".
                # Same map the index uses to build canonical refs from these very rows.
                from rlm_tools_bsl.bsl_index import _CATEGORY_TO_TYPE_PREFIX

                # Dedup by canonical source (category, object) — NOT bare name — so a
                # Document.X and a Catalog.X basis don't collapse. Seed from what direct+
                # back_scan already found (all Documents → default category "Documents",
                # matching the plural folder category stored in metadata_references).
                seen_keys: set[tuple[str, str]] = set()
                for e in result["can_create_from_here"]:
                    cat = (e.get("category") or "Documents").lower()
                    seen_keys.add((cat, str(e.get("document", "")).lower()))
                for r in mrows:
                    src_obj = r.get("source_object")
                    if not src_obj:
                        continue
                    src_cat = r.get("source_category") or ""
                    key = (src_cat.lower(), str(src_obj).lower())
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)
                    canon_cat = _CATEGORY_TO_TYPE_PREFIX.get(src_cat, src_cat)
                    result["can_create_from_here"].append(
                        {
                            "document": src_obj,
                            "category": src_cat,
                            "via": "metadata",
                            "ref": f"{canon_cat}.{src_obj}" if canon_cat else src_obj,
                        }
                    )

        # Для типизированного не-документа metadata_references — единственный применимый
        # источник. Для любого входа достижение резервного лимита при неизвестном счете
        # означает возможный хвост metadata-union. В обоих случаях ответ честно partial.
        if canon and (metadata_incomplete_at_cap or (not is_document_input and not metadata_complete)):
            result["partial"] = True
            if metadata_incomplete_at_cap:
                result.setdefault("_meta", {})["reason"] = "metadata_references_incomplete"
                result["hint"] = (
                    "Счет BasedOn недоступен или расходится с выборкой, а выборка достигла "
                    "резервного лимита. В can_create_from_here может быть не весь хвост; "
                    "проверь индекс и повтори вызов."
                )
            else:
                result.setdefault("_meta", {})["reason"] = "metadata_references_unavailable"
                result["hint"] = (
                    "Связи BasedOn для типизированного не-документа читаются из metadata_references. "
                    "Таблица недоступна или индекс не подключен, поэтому пустые списки здесь неполны; "
                    "пересобери индекс и повтори вызов."
                )

        return result

    def find_print_forms(object_name: str) -> dict:
        """Find print forms registered for an object by parsing ДобавитьКомандыПечати in ManagerModule.

        Returns: dict with object, print_forms list."""
        object_name = _strip_meta_prefix(object_name)
        result: dict = {
            "object": object_name,
            "print_forms": [],
        }

        modules = find_by_type("Documents", object_name)
        mgr_modules = [m for m in modules if m.get("module_type") == "ManagerModule"]
        if not mgr_modules:
            # Try broader search (Catalogs, DataProcessors, etc.)
            modules = find_module(object_name)
            mgr_modules = [m for m in modules if m.get("module_type") == "ManagerModule"]

        for mod in mgr_modules:
            path = mod["path"]
            body = read_procedure(path, "ДобавитьКомандыПечати")
            if body:
                # Pattern 1: helper-function style (ERP 1.x / UPP)
                #   ДобавитьКомандуПечати(КомандыПечати, "Ид", НСтр("ru = 'Представление'"))
                print_re = re.compile(
                    r'ДобавитьКомандуПечати\([^,]+,\s*"(\w+)"(?:,\s*НСтр\("ru\s*=\s*\'([^\']+)\')?',
                    re.IGNORECASE,
                )
                for m in print_re.finditer(body):
                    result["print_forms"].append(
                        {
                            "name": m.group(1),
                            "presentation": m.group(2) or "",
                            "file": path,
                        }
                    )

                # Pattern 2: property-style (ERP 2.x)
                #   КомандаПечати.Идентификатор = "Ид";
                #   КомандаПечати.Представление = НСтр("ru = 'Текст'");
                seen_ids = {pf["name"] for pf in result["print_forms"]}
                id_re = re.compile(
                    r'КомандаПечати\.Идентификатор\s*=\s*"(\w+)"',
                    re.IGNORECASE,
                )
                pres_re = re.compile(
                    r"КомандаПечати\.Представление\s*=\s*НСтр\(\"ru\s*=\s*'([^']+)'",
                    re.IGNORECASE,
                )
                ids = id_re.findall(body)
                presentations = pres_re.findall(body)
                for i, name in enumerate(ids):
                    if name not in seen_ids:
                        result["print_forms"].append(
                            {
                                "name": name,
                                "presentation": presentations[i] if i < len(presentations) else "",
                                "file": path,
                            }
                        )
                        seen_ids.add(name)

        return result

    # ── Form XML parsing helper ──────────────────────────────────

    def parse_form(object_name: str, form_name: str = "", handler: str = "") -> list[dict]:
        """Form event handlers, commands and attributes for an object's forms.

        Without form_name — all forms of the object. With form_name — specific form.
        handler='ProcName' — reverse lookup: find what a BSL procedure is bound to.

        Returns: list of dicts grouped by form, each with:
            category, object_name, form_name, file, module_path,
            handlers, commands, attributes."""
        object_name = _strip_meta_prefix(object_name)
        if not object_name:
            raise ValueError("object_name is required, e.g. parse_form('РеализацияТоваровУслуг')")

        # --- Fast path: SQLite index ---
        if idx_reader is not None:
            # Query ALL rows for the object/form (no handler filter at SQL level).
            # handler filters the SET of forms in _group_form_rows, but inside
            # each form commands/attributes stay complete for context.
            raw = idx_reader.get_form_elements(object_name, form_name)
            if raw is not None and raw:
                return _group_form_rows(raw, handler)
            # raw == [] means table exists but no rows — fall through to live
            # path so that empty forms (zero elements) are still discoverable.

        # --- Fallback: path-heuristic discovery ---
        from rlm_tools_bsl.bsl_xml_parsers import parse_form_xml as _parse_form_xml

        form_files: list[tuple[str, str, str, str]] = []  # (cat, obj, frm, rel_path)

        # Check CommonForms first (object_name = form_name)
        for pattern in (
            f"CommonForms/{object_name}/Form.form",
            f"CommonForms/{object_name}/Ext/Form.xml",
        ):
            found = glob_files_fn(pattern)
            for fp in found:
                form_files.append(("CommonForms", object_name, object_name, fp))

        # Standard categories
        from rlm_tools_bsl.format_detector import METADATA_CATEGORIES

        for cat in METADATA_CATEGORIES:
            if cat in ("CommonForms", "CommonModules", "CommonCommands", "CommonTemplates"):
                continue
            for pattern in (
                f"{cat}/{object_name}/Forms/*/Form.form",
                f"{cat}/{object_name}/Forms/*/Ext/Form.xml",
            ):
                found = glob_files_fn(pattern)
                for fp in found:
                    parts = fp.replace("\\", "/").split("/")
                    try:
                        fi = parts.index("Forms")
                        frm = parts[fi + 1]
                    except (ValueError, IndexError):
                        frm = ""
                    form_files.append((cat, object_name, frm, fp))

        # Last resort: broad glob
        if not form_files:
            for pattern in ("**/Forms/*/Form.form", "**/Forms/*/Ext/Form.xml"):
                found = glob_files_fn(pattern)
                for fp in found:
                    if object_name.lower() in fp.lower():
                        parts = fp.replace("\\", "/").split("/")
                        try:
                            fi = parts.index("Forms")
                            frm = parts[fi + 1]
                            obj = parts[fi - 1] if fi > 0 else object_name
                            c = parts[fi - 2] if fi > 1 else ""
                        except (ValueError, IndexError):
                            frm, obj, c = "", object_name, ""
                        form_files.append((c, obj, frm, fp))

        if form_name:
            form_files = [(c, o, f, p) for c, o, f, p in form_files if f == form_name]

        results: list[dict] = []
        for cat, obj, frm, fp in form_files:
            content = read_file_fn(fp)
            if not content:
                continue
            parsed = _parse_form_xml(content)
            if parsed is None:
                continue

            rel = fp if not os.path.isabs(fp) else os.path.relpath(fp, base_path).replace("\\", "/")
            full_fp = fp if os.path.isabs(fp) else os.path.join(base_path, fp)

            # Determine module_path
            module_path = ""
            if full_fp.replace("\\", "/").endswith("Ext/Form.xml"):
                # CF: Ext/Form.xml → module at Ext/Form/Module.bsl
                form_dir = os.path.dirname(full_fp)
                _candidates: tuple[str, ...] = ("Form/Module.bsl", "Module.bsl")
            else:
                form_dir = os.path.dirname(full_fp)
                _candidates = ("Ext/Module.bsl", "Module.bsl")
            for candidate in _candidates:
                mp = os.path.join(form_dir, candidate)
                if os.path.isfile(mp):
                    module_path = os.path.relpath(mp, base_path).replace("\\", "/")
                    break

            hs = parsed.get("handlers", [])
            if handler:
                hs = [h for h in hs if h["handler"].lower() == handler.lower()]
                if not hs:
                    continue

            results.append(
                {
                    "category": cat,
                    "object_name": obj,
                    "form_name": frm,
                    "file": rel,
                    "module_path": module_path,
                    "handlers": hs,
                    "commands": parsed.get("commands", []),
                    "attributes": parsed.get("attributes", []),
                }
            )

        return results

    def _group_form_rows(raw_rows: list[dict], handler_filter: str = "") -> list[dict]:
        """Group raw form_elements rows into per-form dicts."""
        forms: dict[tuple[str, str, str], dict] = {}
        for r in raw_rows:
            key = (r["category"], r["object_name"], r["form_name"])
            if key not in forms:
                # Derive module_path from file path
                file_path = r.get("file", "")
                module_path = ""
                if file_path:
                    if file_path.endswith("Form.form"):
                        # EDT: Form.form → Module.bsl in same dir
                        mp = file_path.rsplit("/", 1)[0] + "/Module.bsl"
                    elif file_path.endswith("Form.xml"):
                        # CF: Ext/Form.xml → Ext/Form/Module.bsl
                        mp = file_path.rsplit("/", 1)[0] + "/Form/Module.bsl"
                    else:
                        mp = ""
                    # Check if exists via glob
                    if mp:
                        found = glob_files_fn(mp)
                        module_path = mp if found else ""

                forms[key] = {
                    "category": r["category"],
                    "object_name": r["object_name"],
                    "form_name": r["form_name"],
                    "file": file_path,
                    "module_path": module_path,
                    "handlers": [],
                    "commands": [],
                    "attributes": [],
                }

            form = forms[key]
            kind = r.get("kind", "")
            if kind == "handler":
                h = {
                    "element": r.get("element_name", ""),
                    "event": r.get("event", ""),
                    "handler": r.get("handler", ""),
                    "element_type": r.get("element_type", ""),
                    "data_path": r.get("data_path", ""),
                    "scope": r.get("scope", ""),
                }
                if handler_filter:
                    if h["handler"].lower() == handler_filter.lower():
                        form["handlers"].append(h)
                else:
                    form["handlers"].append(h)
            elif kind == "command":
                form["commands"].append(
                    {
                        "name": r.get("element_name", ""),
                        "action": r.get("handler", ""),
                    }
                )
            elif kind == "attribute":
                attr: dict = {
                    "name": r.get("element_name", ""),
                    "types": r.get("element_type", ""),
                    "main": bool(r.get("attribute_is_main", 0)),
                }
                mt = r.get("main_table", "")
                if mt:
                    attr["main_table"] = mt
                extra = r.get("extra_json", "")
                if extra:
                    try:
                        ex = json.loads(extra)
                        qt = ex.get("query_text", "")
                        if qt:
                            attr["query_text"] = qt
                    except (json.JSONDecodeError, TypeError):
                        pass
                form["attributes"].append(attr)

        # Filter out forms with no matching handlers when handler_filter is set
        result = list(forms.values())
        if handler_filter:
            result = [f for f in result if f["handlers"]]
        return result

    # ── Enum / FunctionalOption / Roles helpers ──────────────────

    def _find_enum_values_one(enum_name: str) -> dict:
        """Scalar core of find_enum_values (single enum_name). See find_enum_values."""
        enum_name = _strip_meta_prefix(enum_name)

        # --- Fast path: SQLite index ---
        if idx_reader is not None:
            result = idx_reader.get_enum_values(enum_name)
            if result is not None:
                return result

        # --- Fallback: glob + XML parse ---
        patterns = [
            f"**/Enums/**/*{enum_name}*.xml",
            f"**/Enums/**/*{enum_name}*.mdo",
        ]
        found_files: list[str] = []
        for p in patterns:
            found_files.extend(glob_files_fn(p))
        found_files = list(dict.fromkeys(found_files))

        for f in found_files:
            try:
                content = read_file_fn(f)
            except Exception:
                continue
            parsed = parse_enum_xml(content)
            if parsed is None:
                continue
            if enum_name.lower() in parsed["name"].lower():
                parsed["file"] = f
                return parsed

        return {"error": f"Перечисление '{enum_name}' не найдено"}

    def find_enum_values(enum_name) -> dict:
        """Find an enumeration by name and return its values.

        Args:
            enum_name: Enum name (or fragment) — ``str`` (прежний контракт) ИЛИ
                ``list[str]`` (P1 list-перегрузка → ``{enum_name: {...} | {error}}``).
                Изоляция: ненайденное перечисление даёт ``{error}`` под своим ключом,
                не роняя батч.

        Returns: dict with name, synonym, values, file — or error (str-режим);
            dict by name (list-режим)."""
        return _single_or_map(enum_name, _find_enum_values_one)

    # Predefined items only exist for these categories (CF + EDT).
    _PREDEFINED_CATS = frozenset(
        ("Catalogs", "ChartsOfCharacteristicTypes", "ChartsOfAccounts", "ChartsOfCalculationTypes")
    )

    def _build_ext_attrs_cache() -> None:
        """Parse all attribute-bearing ext objects once per session.

        Builds ``_ext_attrs_cache`` keyed by ``(cat_lower, obj_lower)`` with
        rows of the same shape ``find_attributes`` returns (with ``source_file``).
        Subsequent ``find_attributes(name=…)`` calls then filter the cache
        in-memory — no XML re-parsing per ext object. Critical for large
        extensions (~150+ objects) where the cold scan takes 5-15s.
        """
        if _ext_attrs_cache_built[0]:
            return
        with _ext_attrs_cache_lock:
            if _ext_attrs_cache_built[0]:
                return
            if not _extension_metadata_xml:
                _ext_attrs_cache_built[0] = True
                return

            from rlm_tools_bsl.bsl_xml_parsers import normalize_type_string as _nts

            def _make_type(raw: str) -> list[str]:
                try:
                    return json.loads(_nts(raw))
                except Exception:
                    return []

            seen: set[tuple[str, str]] = set()
            for cat, obj_name, _rel in _extension_metadata_xml:
                if cat.lower() not in _CATEGORY_XML_NAMES:
                    continue
                key = (cat.lower(), obj_name.lower())
                if key in seen:
                    continue
                seen.add(key)
                object_path = f"{cat}/{obj_name}"
                try:
                    resolved = _resolve_object_xml(object_path)
                    content = _ext_read_file(resolved)
                    parsed = parse_metadata_xml(content)
                except Exception:
                    continue
                if not parsed:
                    continue

                rows: list[dict] = []
                for attr in parsed.get("attributes", []):
                    rows.append(
                        {
                            "object_name": obj_name,
                            "category": cat,
                            "attr_name": attr.get("name", ""),
                            "attr_synonym": attr.get("synonym", ""),
                            "attr_type": _make_type(attr.get("type", "")),
                            "attr_kind": "attribute",
                            "ts_name": None,
                            "source_file": resolved,
                        }
                    )
                for dim in parsed.get("dimensions", []):
                    rows.append(
                        {
                            "object_name": obj_name,
                            "category": cat,
                            "attr_name": dim.get("name", ""),
                            "attr_synonym": dim.get("synonym", ""),
                            "attr_type": _make_type(dim.get("type", "")),
                            "attr_kind": "dimension",
                            "ts_name": None,
                            "source_file": resolved,
                        }
                    )
                for res in parsed.get("resources", []):
                    rows.append(
                        {
                            "object_name": obj_name,
                            "category": cat,
                            "attr_name": res.get("name", ""),
                            "attr_synonym": res.get("synonym", ""),
                            "attr_type": _make_type(res.get("type", "")),
                            "attr_kind": "resource",
                            "ts_name": None,
                            "source_file": resolved,
                        }
                    )
                for ts in parsed.get("tabular_sections", []):
                    ts_name = ts.get("name", "")
                    for ta in ts.get("attributes", []):
                        rows.append(
                            {
                                "object_name": obj_name,
                                "category": cat,
                                "attr_name": ta.get("name", ""),
                                "attr_synonym": ta.get("synonym", ""),
                                "attr_type": _make_type(ta.get("type", "")),
                                "attr_kind": "ts_attribute",
                                "ts_name": ts_name,
                                "source_file": resolved,
                            }
                        )

                if rows:
                    _ext_attrs_cache[key] = [_AttrRecord(r) for r in rows]

            _ext_attrs_cache_built[0] = True

    def _build_ext_predefined_cache() -> None:
        """Parse predefined items from ext objects once per session — mirror
        of ``_build_ext_attrs_cache`` for predefined data.
        """
        if _ext_predefined_cache_built[0]:
            return
        with _ext_predefined_cache_lock:
            if _ext_predefined_cache_built[0]:
                return
            if not _extension_metadata_xml:
                _ext_predefined_cache_built[0] = True
                return

            from rlm_tools_bsl.bsl_xml_parsers import parse_predefined_items as _ppi

            seen: set[tuple[str, str]] = set()
            for cat, obj_name, _rel in _extension_metadata_xml:
                if cat not in _PREDEFINED_CATS:
                    continue
                key = (cat.lower(), obj_name.lower())
                if key in seen:
                    continue
                seen.add(key)
                object_path = f"{cat}/{obj_name}"
                # Use _predefined_candidates to find the predefined.xml/mdo path.
                candidates = _predefined_candidates(object_path)
                content: str | None = None
                source_path: str | None = None
                for p in candidates:
                    try:
                        if not _ext_resolve_safe(p).exists():
                            continue
                    except Exception:
                        continue
                    try:
                        content = _ext_read_file(p)
                        source_path = p
                        break
                    except Exception:
                        continue
                if not content:
                    continue
                items = _ppi(content)
                if not items:
                    continue

                rows: list[dict] = []
                for item in items:
                    rows.append(
                        {
                            "object_name": obj_name,
                            "category": cat,
                            "item_name": item.get("name", ""),
                            "item_synonym": item.get("synonym", ""),
                            "types": item.get("types", []),
                            "item_code": item.get("code", ""),
                            "is_folder": item.get("is_folder", False),
                            "source_file": source_path,
                        }
                    )
                if rows:
                    _ext_predefined_cache[key] = rows

            _ext_predefined_cache_built[0] = True

    def _live_attributes_in_extensions(name: str, category: str, kind: str, limit: int) -> list[dict]:
        """Return ext-side attribute rows matching the name-only query.

        Reads from the per-session ``_ext_attrs_cache`` (built lazily on first
        call). Cold call parses every attribute-bearing ext object's XML once;
        subsequent calls filter the cache in memory. ``limit`` is a soft hint —
        full result returned, caller rank-merges and slices.
        """
        if not _extension_metadata_xml:
            return []
        _build_ext_attrs_cache()

        name_lower = name.lower() if name else ""
        category_lower = category.lower() if category else ""
        kind_lower = kind.lower() if kind else ""
        out: list[dict] = []
        for (cat_lower, _obj_lower), rows in _ext_attrs_cache.items():
            if category_lower and category_lower != cat_lower:
                continue
            for row in rows:
                if kind_lower and kind_lower != row["attr_kind"]:
                    continue
                if name_lower:
                    if name_lower not in row["attr_name"].lower() and name_lower not in row["attr_synonym"].lower():
                        continue
                out.append(row)
        return out

    def _live_predefined_in_extensions(name: str, limit: int) -> list[dict]:
        """Return ext-side predefined items matching the name-only query.

        Mirrors ``_live_attributes_in_extensions`` — reads from per-session
        ``_ext_predefined_cache`` (built lazily on first call).
        """
        if not _extension_metadata_xml:
            return []
        _build_ext_predefined_cache()

        name_lower = name.lower() if name else ""
        out: list[dict] = []
        for rows in _ext_predefined_cache.values():
            for row in rows:
                if name_lower:
                    if name_lower not in row["item_name"].lower() and name_lower not in row["item_synonym"].lower():
                        continue
                out.append(row)
        return out

    def _resolve_object_name_from_extension_metadata(object_name: str) -> tuple[str, str] | None:
        """For bare ``object_name`` (no category prefix, not present as a .bsl
        module), look up an XML-only extension object via
        ``_extension_metadata_xml``. Returns ``(category, "Category/Name")``
        using the CANONICAL ``object_name`` from the metadata entry — so that
        case-mismatch between the user argument and ext-metadata still produces
        a path that matches ``_xml_candidates`` later. Returns ``None`` if no
        ext object matches.
        """
        if not _extension_metadata_xml or not object_name:
            return None
        target = object_name.lower()
        for cat, obj_name, _rel in _extension_metadata_xml:
            if obj_name.lower() == target:
                return cat, f"{cat}/{obj_name}"
        return None

    def find_attributes(
        name: str = "", object_name: str = "", category: str = "", kind: str = "", limit: int = 500
    ) -> list[dict]:
        """Find object attributes/dimensions/resources by name, object, category, or kind."""
        if kind:
            kind = kind.lower()
        if object_name:
            object_name = _strip_meta_prefix(object_name)

        # Build extension state lazily when extensions are configured — the
        # ext attribute/predefined live-fallbacks depend on _extension_metadata_xml.
        if _ext_roots_resolved:
            _ensure_index()

        has_path = object_name and "/" in object_name

        # Fast path: index (None = table missing, [] = authoritative for name-only)
        if idx_reader is not None:
            results = idx_reader.get_object_attributes(
                attr_name=name,
                object_name=object_name,
                category=category,
                kind=kind,
                limit=limit,
            )
            if results:
                results = [_AttrRecord(r) for r in results]
            if results is not None:
                if results:  # non-empty — authoritative for main config
                    # Merge ext-side rows for name-only queries BEFORE truncation
                    # (codex round 5): rank-merge by attr_name so ext exact hits
                    # are not starved when main saturates `limit`.
                    if not object_name and _extension_metadata_xml:
                        ext_rows = _live_attributes_in_extensions(name, category, kind, limit)
                        return _rank_merge_ext_into_main(
                            results,
                            ext_rows,
                            name,
                            name_keys=("attr_name", "attr_synonym"),
                            dedup_keys=("category", "object_name", "attr_name", "attr_kind"),
                            limit=limit,
                        )
                    return results[:limit]
                if not object_name:
                    # Name-only search: index returned []. Before declaring authoritative,
                    # let extensions (which are NEVER in the main index) contribute.
                    if _extension_metadata_xml:
                        ext_rows = _live_attributes_in_extensions(name, category, kind, limit)
                        if ext_rows:
                            return _rank_merge_ext_into_main(
                                [],
                                ext_rows,
                                name,
                                name_keys=("attr_name", "attr_synonym"),
                                dedup_keys=("category", "object_name", "attr_name", "attr_kind"),
                                limit=limit,
                            )
                    return results
                # object_name given but empty result — try auto-resolve below

        # Auto-resolve category via find_module (same pattern as analyze_object)
        if object_name and not has_path:
            modules = find_module(object_name)
            exact = [m for m in modules if (m.get("object_name") or "").lower() == object_name.lower()]
            if exact:
                cat = exact[0].get("category", "")
                if cat:
                    object_name = f"{cat}/{object_name}"
                    has_path = True

        # Auto-resolve via extension metadata for XML-only ext objects (no .bsl).
        if object_name and not has_path:
            ext_resolved = _resolve_object_name_from_extension_metadata(object_name)
            if ext_resolved is not None:
                object_name = ext_resolved[1]
                has_path = True

        # Fallback: live XML parse via _resolve_object_xml (same as parse_object_xml)
        if has_path:
            from rlm_tools_bsl.bsl_xml_parsers import normalize_type_string as _nts

            try:
                resolved = _resolve_object_xml(object_name)
                content = _ext_read_file(resolved)
                parsed = parse_metadata_xml(content)
            except Exception:
                return []
            if not parsed:
                return []

            def _make_type(raw: str) -> list[str]:
                import json as _json

                return _json.loads(_nts(raw))

            results = []
            obj_short = object_name.split("/")[-1]
            cat = object_name.split("/")[0]

            # Validate category if provided
            if category and category.lower() != cat.lower():
                return []

            for attr in parsed.get("attributes", []):
                if name and (
                    name.lower() not in attr.get("name", "").lower()
                    and name.lower() not in attr.get("synonym", "").lower()
                ):
                    continue
                if kind and kind != "attribute":
                    continue
                results.append(
                    {
                        "object_name": obj_short,
                        "category": cat,
                        "attr_name": attr.get("name", ""),
                        "attr_synonym": attr.get("synonym", ""),
                        "attr_type": _make_type(attr.get("type", "")),
                        "attr_kind": "attribute",
                        "ts_name": None,
                        "source_file": resolved,
                    }
                )
            for dim in parsed.get("dimensions", []):
                if name and (
                    name.lower() not in dim.get("name", "").lower()
                    and name.lower() not in dim.get("synonym", "").lower()
                ):
                    continue
                if kind and kind != "dimension":
                    continue
                results.append(
                    {
                        "object_name": obj_short,
                        "category": cat,
                        "attr_name": dim.get("name", ""),
                        "attr_synonym": dim.get("synonym", ""),
                        "attr_type": _make_type(dim.get("type", "")),
                        "attr_kind": "dimension",
                        "ts_name": None,
                        "source_file": resolved,
                    }
                )
            for res in parsed.get("resources", []):
                if name and (
                    name.lower() not in res.get("name", "").lower()
                    and name.lower() not in res.get("synonym", "").lower()
                ):
                    continue
                if kind and kind != "resource":
                    continue
                results.append(
                    {
                        "object_name": obj_short,
                        "category": cat,
                        "attr_name": res.get("name", ""),
                        "attr_synonym": res.get("synonym", ""),
                        "attr_type": _make_type(res.get("type", "")),
                        "attr_kind": "resource",
                        "ts_name": None,
                        "source_file": resolved,
                    }
                )
            for ts in parsed.get("tabular_sections", []):
                for ta in ts.get("attributes", []):
                    if name and (
                        name.lower() not in ta.get("name", "").lower()
                        and name.lower() not in ta.get("synonym", "").lower()
                    ):
                        continue
                    if kind and kind != "ts_attribute":
                        continue
                    results.append(
                        {
                            "object_name": obj_short,
                            "category": cat,
                            "attr_name": ta.get("name", ""),
                            "attr_synonym": ta.get("synonym", ""),
                            "attr_type": _make_type(ta.get("type", "")),
                            "attr_kind": "ts_attribute",
                            "ts_name": ts.get("name", ""),
                            "source_file": resolved,
                        }
                    )
            return [_AttrRecord(r) for r in results[:limit]]

        # No idx_reader, no object_name → scan extension metadata as the only live source.
        if _extension_metadata_xml and not object_name:
            ext_rows = _live_attributes_in_extensions(name, category, kind, limit)
            return _rank_merge_ext_into_main(
                [],
                ext_rows,
                name,
                name_keys=("attr_name", "attr_synonym"),
                dedup_keys=("category", "object_name", "attr_name", "attr_kind"),
                limit=limit,
            )
        return []

    def _predefined_candidates(object_name: str) -> list[str]:
        """Predefined.xml/MDO path candidates for ``Category/Name``.

        EDT keeps predefined items inside the object's ``.mdo`` file; CF uses a
        separate ``Ext/Predefined.xml``. Extension layouts mirror these.
        """
        parts = object_name.split("/")
        category = parts[0].lower() if parts else ""
        obj_short = parts[-1] if parts else ""

        candidates: list[str] = []
        if obj_short:
            candidates.append(f"{object_name}/Ext/Predefined.xml")
            candidates.append(f"{object_name}/{obj_short}.mdo")

        # Extension candidates from the metadata-XML pass.
        if _extension_metadata_xml and category and obj_short:
            target_cat = category.lower()
            target_name = obj_short.lower()
            for cat, ent_name, rel in _extension_metadata_xml:
                if cat.lower() != target_cat or ent_name.lower() != target_name:
                    continue
                if rel.endswith(".mdo"):
                    candidates.append(rel)
                else:
                    # Derive the object dir from the locator, which may be either
                    # a sibling Cat/Name.xml (real CF/CFE dump) or Cat/Name/Ext/<Type>.xml.
                    rel_p = rel.replace("\\", "/")
                    parent = os.path.dirname(rel_p)
                    if parent.endswith("/Ext"):
                        ext_obj_dir = parent[: -len("/Ext")]
                    elif rel_p.lower().endswith(".xml"):
                        ext_obj_dir = rel_p[:-4]  # strip ".xml" → object dir
                    else:
                        ext_obj_dir = ""
                    if ext_obj_dir:
                        candidates.append(f"{ext_obj_dir}/Ext/Predefined.xml")
        return candidates

    def find_predefined(name: str = "", object_name: str = "", limit: int = 500) -> list[dict]:
        """Find predefined items of ChartsOfCharacteristicTypes, Catalogs, ChartsOfAccounts."""
        if object_name:
            object_name = _strip_meta_prefix(object_name)
        if _ext_roots_resolved:
            _ensure_index()
        has_path = object_name and "/" in object_name

        # Fast path: index (None = table missing, [] = authoritative for name-only)
        if idx_reader is not None:
            results = idx_reader.get_predefined_items(item_name=name, object_name=object_name, limit=limit)
            if results is not None:
                if results:  # non-empty — authoritative for main config
                    # Merge ext rows BEFORE truncation (codex round 5).
                    if not object_name and _extension_metadata_xml:
                        ext_rows = _live_predefined_in_extensions(name, limit)
                        return _rank_merge_ext_into_main(
                            results,
                            ext_rows,
                            name,
                            name_keys=("item_name", "item_synonym"),
                            dedup_keys=("category", "object_name", "item_name"),
                            limit=limit,
                        )
                    return results[:limit]
                if not object_name:
                    # Name-only search: index returned []. Let extensions contribute.
                    if _extension_metadata_xml:
                        ext_rows = _live_predefined_in_extensions(name, limit)
                        if ext_rows:
                            return _rank_merge_ext_into_main(
                                [],
                                ext_rows,
                                name,
                                name_keys=("item_name", "item_synonym"),
                                dedup_keys=("category", "object_name", "item_name"),
                                limit=limit,
                            )
                    return results
                # object_name given but empty result — try auto-resolve below

        # Index-authoritative for name-only search (no live XML scan across 6820+ files);
        # extensions are NEVER in the main index, so let them contribute live (v1.12.0).
        if not object_name:
            if _extension_metadata_xml:
                ext_rows = _live_predefined_in_extensions(name, limit)
                if ext_rows:
                    return _rank_merge_ext_into_main(
                        [],
                        ext_rows,
                        name,
                        name_keys=("item_name", "item_synonym"),
                        dedup_keys=("category", "object_name", "item_name"),
                        limit=limit,
                    )
            return []

        # Auto-resolve category via find_module (same pattern as analyze_object)
        if not has_path:
            modules = find_module(object_name)
            exact = [m for m in modules if (m.get("object_name") or "").lower() == object_name.lower()]
            if exact:
                cat = exact[0].get("category", "")
                if cat:
                    object_name = f"{cat}/{object_name}"
                    has_path = True

        # Auto-resolve via extension metadata for XML-only ext objects (no .bsl).
        if not has_path:
            ext_resolved = _resolve_object_name_from_extension_metadata(object_name)
            if ext_resolved is not None:
                object_name = ext_resolved[1]
                has_path = True

        if not has_path:
            return []

        from rlm_tools_bsl.bsl_xml_parsers import parse_predefined_items as _ppi

        obj_short = object_name.split("/")[-1]
        candidates = _predefined_candidates(object_name)

        for p in candidates:
            try:
                if not _ext_resolve_safe(p).exists():
                    continue
            except Exception:
                continue
            try:
                content = _ext_read_file(p)
            except Exception:
                continue
            items = _ppi(content)
            if not items:
                continue
            results = []
            for item in items:
                if (
                    name
                    and name.lower() not in item["name"].lower()
                    and name.lower() not in item.get("synonym", "").lower()
                ):
                    continue
                results.append(
                    {
                        "object_name": obj_short,
                        "category": object_name.split("/")[0] if "/" in object_name else "",
                        "item_name": item["name"],
                        "item_synonym": item.get("synonym", ""),
                        "types": item.get("types", []),
                        "item_code": item.get("code", ""),
                        "is_folder": item.get("is_folder", False),
                        "source_file": p,
                    }
                )
            return results[:limit]

        return []

    _fo_lazy = LazyList()

    def _build_functional_options() -> list[dict]:
        files = glob_files_fn("**/FunctionalOptions/**/*.xml")
        files.extend(glob_files_fn("**/FunctionalOptions/**/*.mdo"))
        files.extend(glob_files_fn("**/FunctionalOptions/*.xml"))
        files.extend(glob_files_fn("**/FunctionalOptions/*.mdo"))
        files = list(dict.fromkeys(files))
        result: list[dict] = []
        for f in files:
            try:
                content = read_file_fn(f)
            except Exception:
                continue
            parsed = parse_functional_option_xml(content)
            if parsed is None:
                continue
            parsed["file"] = f
            result.append(parsed)
        return result

    def _ensure_functional_options() -> list[dict]:
        return _fo_lazy.ensure(_build_functional_options)

    def find_functional_options(object_name: str, include_code: bool = True, limit: int | None = None) -> dict:
        """Find functional options that affect a given object.
        Also greps BSL modules for ПолучитьФункциональнуюОпцию("X") pattern.
        Uses SQLite index for XML options when available.

        Args:
            object_name: Object name to search for in FO content lists.
            include_code: ``True`` (default, backcompat) — also grep BSL modules for
                ``ПолучитьФункциональнуюОпцию("X")`` (a ``safe_grep`` code scan).
                ``False`` — XML-only (index/live FO definitions), без code-скана; так
                зовёт compact ``get_object_profile`` (тяжёлый grep — только под
                ``include_code_usages``).
            limit: ``None`` (default, backcompat) — вернуть все опции без пагинации.
                Иначе **per-bucket cap** (v1.28.0, #6): ``xml_options`` и ``code_options``
                режутся КАЖДЫЙ независимо до ``limit`` (``limit=10`` → до 10+10, НЕ 10
                суммарно) — зеркало ``find_event_subscriptions``. Защита от обрыва по
                ``max_output_chars`` на объектах с сотнями опций.

        Returns: dict with object, xml_options, code_options (empty when not
        include_code). При ``limit`` != None — плюс ``total`` (полный xt+ct для
        непустого object_name), ``returned`` (len(xp)+len(cp)), ``has_more``
        (per-bucket). Пустой code-обзор сохраняет бюджет 20 модулей; если каталог
        больше, пагинированный ответ помечен ``partial=True`` и ``total`` считается
        только по проверенному code-срезу."""
        object_name = _strip_meta_prefix(object_name)

        # --- Fast path for xml_options: SQLite index ---
        xml_options: list[dict] | None = None
        if idx_reader is not None:
            xml_options = idx_reader.get_functional_options(object_name)

        if xml_options is None:
            all_fo = _ensure_functional_options()
            name_lower = object_name.lower()
            xml_options = []
            for fo in all_fo:
                matched = any(name_lower in c.lower() for c in fo.get("content", []))
                if matched:
                    xml_options.append(dict(fo))

        # Grep for ПолучитьФункциональнуюОпцию in BSL code (skipped when XML-only).
        code_options: list[dict] = []
        code_scope_partial = False
        code_modules_scanned = 0
        code_modules_total = 0
        if include_code:
            try:
                live_catalog = _ensure_live_bsl_catalog()
                code_modules_total = len(live_catalog)
                # Непустой объект получает полный live-каталог. Пустой обзор сохраняет
                # прежний безопасный бюджет safe_grep — первые 20 модулей, а не весь dump.
                grep_kwargs = {"max_files": len(live_catalog)} if object_name else {}
                code_modules_scanned = len(live_catalog) if object_name else min(20, len(live_catalog))
                code_scope_partial = not object_name and code_modules_total > code_modules_scanned
                grep_results = safe_grep("(?i)ПолучитьФункциональнуюОпцию", name_hint=object_name, **grep_kwargs)
                for r in grep_results:
                    text = r.get("text", "") or r.get("content", "")
                    # Extract option name from ПолучитьФункциональнуюОпцию("OptionName")
                    m = re.search(r'ПолучитьФункциональнуюОпцию\(\s*"([^"]+)"', text, re.IGNORECASE)
                    if m:
                        code_options.append(
                            {
                                "option_name": m.group(1),
                                "file": r.get("file", ""),
                                "line": r.get("line", 0),
                            }
                        )
            except Exception:
                pass

        if limit is None:
            return {
                "object": object_name,
                "xml_options": xml_options,
                "code_options": code_options,
            }
        # Per-bucket cap (#6): each list truncated independently to ``limit``.
        xt, ct = len(xml_options), len(code_options)
        n = max(0, int(limit))
        xp, cp = xml_options[:n], code_options[:n]
        page = {
            "object": object_name,
            "xml_options": xp,
            "code_options": cp,
            "total": xt + ct,
            "returned": len(xp) + len(cp),
            "has_more": xt > len(xp) or ct > len(cp),
        }
        if code_scope_partial:
            page["partial"] = True
            page["_meta"] = {
                "reason": "code_scan_budget",
                "code_modules_scanned": code_modules_scanned,
                "code_modules_total": code_modules_total,
                "total_scope": "all_xml_plus_scanned_code",
                "hint": "Пустой обзор проверяет первые 20 BSL-модулей; укажи object_name для полного code-скана.",
            }
        return page

    def find_roles(object_name: str) -> dict:
        """Find roles that grant rights to a given object.

        Args:
            object_name: Object name substring to filter rights by.

        Returns: dict with object, roles list."""
        object_name = _strip_meta_prefix(object_name)

        # Fast path: SQLite index
        if idx_reader is not None:
            idx_roles = idx_reader.get_roles(object_name)
            if idx_roles is not None:
                return {"object": object_name, "roles": idx_roles}

        # Fallback: glob + XML parse
        patterns = [
            "**/Roles/*/Ext/Rights.xml",
            "**/Roles/*/*.rights",
        ]
        found_files: list[str] = []
        for p in patterns:
            found_files.extend(glob_files_fn(p))
        found_files = list(dict.fromkeys(found_files))

        roles: list[dict] = []
        for f in found_files:
            # Extract role name from path: Roles/RoleName/Ext/Rights.xml
            parts = f.replace("\\", "/").split("/")
            role_name = ""
            for i, part in enumerate(parts):
                if part == "Roles" and i + 1 < len(parts):
                    role_name = parts[i + 1]
                    break

            try:
                content = read_file_fn(f)
            except Exception:
                continue
            rights = parse_rights_xml(content, object_name)
            for r in rights:
                roles.append(
                    {
                        "role_name": role_name,
                        "object": r["object"],
                        "rights": r["rights"],
                        "file": f,
                    }
                )

        # Group by role_name, merge rights (match index behavior)
        grouped: dict[str, dict] = {}
        for r in roles:
            key = r["role_name"]
            if key not in grouped:
                grouped[key] = {
                    "role_name": key,
                    "object": object_name,
                    "rights": [],
                    "file": r["file"],
                }
            for right in r["rights"]:
                if right not in grouped[key]["rights"]:
                    grouped[key]["rights"].append(right)

        return {"object": object_name, "roles": list(grouped.values())}

    # ── FTS search (requires SQLite index with FTS5) ────────────

    def _iter_extension_bsl() -> list[tuple[str, BslFileInfo]]:
        """Return only the extension-side rows from ``_index_state``."""
        if not _extension_paths_set:
            return []
        return [(rel, info) for rel, info in _index_state if rel in _extension_paths_set]

    def _rank_merge_ext_into_main(
        main_rows: list[dict],
        ext_rows: list[dict],
        query: str,
        name_keys: tuple[str, ...],
        dedup_keys: tuple[str, ...],
        limit: int,
    ) -> list[dict]:
        """Merge main+ext rows with 3-level rank applied to ext, dedup by
        ``dedup_keys``, slice to ``limit``.

        ``name_keys`` is a tuple of fields to rank against — the row's rank is
        the BEST (lowest) rank found across all listed fields. This mirrors
        ``IndexReader.get_object_attributes`` / ``get_predefined_items`` which
        match against ``attr_name OR attr_synonym`` (resp. ``item_name OR
        item_synonym``), so passing both keys lets a row matching by Russian
        synonym ALSO claim rank 0/1 instead of being silently rank 2 and
        sliced away by a saturated main result (codex round 6).

        Strategy: ext rows with rank 0 (exact match on any key) or 1 (prefix
        on any key) go BEFORE all main rows. Main rows keep their original
        ordering (FTS or index-native). Ext rows with rank 2 (substring only)
        go AFTER main rows.
        """
        if not ext_rows:
            return list(main_rows)[:limit]
        seen = {tuple((r.get(k) or "") for k in dedup_keys) for r in main_rows}
        ext_dedup = [r for r in ext_rows if tuple((r.get(k) or "") for k in dedup_keys) not in seen]
        if not ext_dedup:
            return list(main_rows)[:limit]
        q_lower = (query or "").lower()
        if not q_lower:
            return (list(main_rows) + ext_dedup)[:limit]

        def _rank(row: dict) -> int:
            best = 2
            for key in name_keys:
                n = (row.get(key) or "").lower()
                if n == q_lower:
                    return 0
                if n.startswith(q_lower):
                    if best > 1:
                        best = 1
            return best

        primary_key = name_keys[0]
        ext_top = sorted(
            (r for r in ext_dedup if _rank(r) < 2),
            key=lambda r: (_rank(r), (r.get(primary_key) or "").lower()),
        )
        ext_bottom = [r for r in ext_dedup if _rank(r) >= 2]
        merged = ext_top + list(main_rows) + ext_bottom
        return merged[:limit]

    def _reserve_merge_ext_into_main(
        main_rows: list[dict],
        ext_rows: list[dict],
        dedup_keys: tuple[str, ...],
        limit: int,
        quota_ratio: int = 5,
    ) -> list[dict]:
        """Merge for helpers without a meaningful name-based rank
        (e.g. search_module_headers). Reserves up to
        ``min(len(ext), max(1, limit // quota_ratio))`` slots for ext rows by
        clipping the main tail, so a saturated main result still surfaces
        extension hits.
        """
        if not ext_rows:
            return list(main_rows)[:limit]
        seen = {tuple((r.get(k) or "") for k in dedup_keys) for r in main_rows}
        ext_dedup = [r for r in ext_rows if tuple((r.get(k) or "") for k in dedup_keys) not in seen]
        if not ext_dedup:
            return list(main_rows)[:limit]
        quota = min(len(ext_dedup), max(1, limit // quota_ratio))
        main_keep = max(0, limit - quota)
        return (list(main_rows)[:main_keep] + ext_dedup[:quota])[:limit]

    def _live_search_methods(query: str, limit: int) -> list[dict]:
        """Substring search in extension .bsl procedures.

        Result shape matches ``IndexReader.search_methods`` exactly, including
        ``rank=None`` (FTS-bm25 cannot be reproduced live). Full scan with no
        early break — the caller (``search_methods``) rank-merges and slices
        last so a high-quality ext hit (exact name) is not lost when main FTS
        already returned `limit` rows.
        """
        if not _extension_paths_set or not query:
            return []
        needle = query.lower()
        out: list[dict] = []
        for rel, info in _iter_extension_bsl():
            try:
                procs = _parse_procedures(rel)
            except Exception:
                continue
            for proc in procs:
                if needle not in proc["name"].lower():
                    continue
                out.append(
                    {
                        "name": proc["name"],
                        "type": proc["type"],
                        "is_export": proc["is_export"],
                        "line": proc["line"],
                        "end_line": proc["end_line"],
                        "params": proc["params"],
                        "module_path": rel,
                        "object_name": info.object_name,
                        "rank": None,
                    }
                )
        return out

    def _live_search_objects(query: str, limit: int) -> list[dict]:
        """Substring search in extension object synonyms / object names.

        Empty/whitespace ``query`` → alphabetical listing sorted by
        ``(category, object_name)``, sliced to ``limit`` (mirrors
        ``IndexReader.search_objects("")``).

        Non-empty query → **full scan**, no early slice. Mirrors the indexer's
        contract: ``IndexReader.search_objects`` explicitly does NOT apply a
        SQL LIMIT for substring queries because Python-side 4-level ranking
        needs all matches to guarantee an exact-name hit is never lost. The
        caller (``search_objects``) re-ranks the merged list and slices last.
        """
        if not _extension_synonyms:
            return []
        _ensure_index()

        if not query or not query.strip():
            rows = [
                {
                    "object_name": obj_name,
                    "category": cat,
                    "synonym": prefixed_synonym,
                    "file": rel,
                }
                for obj_name, cat, prefixed_synonym, rel in _extension_synonyms
            ]
            rows.sort(key=lambda r: (r["category"], r["object_name"]))
            return rows[:limit]

        needle = query.lower()
        out: list[dict] = []
        for obj_name, cat, prefixed_synonym, rel in _extension_synonyms:
            if needle in prefixed_synonym.lower() or needle in obj_name.lower():
                out.append(
                    {
                        "object_name": obj_name,
                        "category": cat,
                        "synonym": prefixed_synonym,
                        "file": rel,
                    }
                )
        return out

    def _live_search_regions(query: str, limit: int) -> list[dict]:
        """Substring search over #Область / #Region declarations in extension .bsl.

        Full scan, no early break — caller rank-merges and slices last.
        """
        if not _extension_paths_set or not query:
            return []
        needle = query.lower()
        region_start = re.compile(BSL_PATTERNS["region_start"], re.IGNORECASE)
        region_end = re.compile(BSL_PATTERNS["region_end"], re.IGNORECASE)
        out: list[dict] = []
        for rel, info in _iter_extension_bsl():
            try:
                content = _ext_read_file(rel)
            except Exception:
                continue
            lines = content.splitlines()
            open_stack: list[tuple[str, int]] = []
            for line_idx, line in enumerate(lines, 1):
                m_start = region_start.search(line)
                if m_start:
                    open_stack.append((m_start.group(1), line_idx))
                    continue
                if region_end.search(line) and open_stack:
                    name, start = open_stack.pop()
                    if needle in name.lower():
                        out.append(
                            {
                                "name": name,
                                "line": start,
                                "end_line": line_idx,
                                "module_path": rel,
                                "object_name": info.object_name,
                                "category": info.category,
                            }
                        )
            # Unclosed regions at EOF: skip — same behavior as indexer.
        return out

    def _live_search_module_headers(query: str, limit: int) -> list[dict]:
        """Substring search over leading-comment blocks in extension .bsl.

        Full scan, no early break — caller reserves a quota and slices last.
        """
        if not _extension_paths_set or not query:
            return []
        needle = query.lower()
        out: list[dict] = []
        for rel, info in _iter_extension_bsl():
            try:
                content = _ext_read_file(rel)
            except Exception:
                continue
            lines = content.splitlines()[:30]
            header_lines: list[str] = []
            for line in lines:
                stripped = line.strip()
                if stripped.startswith("//"):
                    header_lines.append(stripped[2:].strip())
                elif stripped == "":
                    if header_lines:
                        continue
                else:
                    break
            header_comment = "\n".join(header_lines).strip()
            if not header_comment or needle not in header_comment.lower():
                continue
            out.append(
                {
                    "module_path": rel,
                    "object_name": info.object_name,
                    "category": info.category,
                    "header_comment": header_comment,
                }
            )
        return out

    def search_methods(query: str, limit: int = 30) -> list[dict]:
        """Full-text search for methods by name substring (FTS5 trigram).
        Requires a pre-built SQLite index with FTS enabled.

        Args:
            query: Search substring (e.g. 'Провед', 'ОбработкаЗаполнения').
            limit: Max results (default 30).

        Returns: list of dicts {name, type, is_export, line, end_line, params,
                 module_path, object_name, rank} ordered by relevance.
                 ``params`` — список имён параметров (list[str], v1.18.0).
                 Empty list if index/FTS not available."""
        result: list[dict] = []
        if idx_reader is not None and idx_reader.has_fts:
            # v1.18.0 Фикс 2: params строкой -> list на helper-границе.
            result = _normalize_method_params(list(idx_reader.search_methods(query, limit)))
        _ensure_index()
        # Merge BEFORE truncation: even when main FTS fills `limit`, an ext
        # method with exact-name match must be visible. Rank-merge with 3-level
        # scheme on `name`: rank 0 (exact) and 1 (prefix) ext rows go BEFORE
        # main; rank 2 (substring) ext rows go AFTER main, sliced last (codex
        # round 5).
        if _extension_paths_set and query:
            ext_rows = _live_search_methods(query, limit)
            result = _rank_merge_ext_into_main(
                result, ext_rows, query, name_keys=("name",), dedup_keys=("module_path", "name"), limit=limit
            )
        return result[:limit]

    def search_objects(query: str = "", limit: int = 50) -> list[dict]:
        """Search 1C objects by business name (Russian synonym) or technical name.
        Uses pre-built SQLite index with object synonyms.

        Args:
            query: Search string (e.g. 'себестоимость', 'Авансовый', 'общий модуль').
            limit: Max results (default 50).

        Returns: list of dicts {object_name, category, synonym, file}.
                 Empty list if index not available or no synonyms built."""
        result: list[dict] = []
        if idx_reader is not None:
            indexed = idx_reader.search_objects(query, limit)
            if indexed is not None:
                result = list(indexed)
        _ensure_index()
        # Extension synonyms are never in the main index; merge BEFORE truncation
        # so a saturated main result does not starve ext rows. Empty query →
        # re-sort the merged list alphabetically by (category, object_name) to
        # honour IndexReader.search_objects("") contract. Non-empty query →
        # re-rank using the same 4-level scheme IndexReader uses (exact name >
        # prefix > synonym substring > category), so a matching ext object wins
        # a slot from a low-rank main row instead of being sliced away at
        # position 51+ (v1.12.0; codex round 3).
        if _extension_synonyms:
            seen = {(r.get("file", ""), r.get("object_name", "")) for r in result}
            ext_rows = [
                row for row in _live_search_objects(query, limit) if (row["file"], row["object_name"]) not in seen
            ]
            if ext_rows:
                is_empty_query = not query or not query.strip()
                if is_empty_query:
                    merged = result + ext_rows
                    merged.sort(key=lambda r: (r.get("category", ""), r.get("object_name", "")))
                    result = merged
                else:
                    q_lower = query.strip().lower()

                    def _rank_for_query(row: dict) -> int:
                        # Mirrors IndexReader.search_objects ranking exactly.
                        name_lower = (row.get("object_name") or "").lower()
                        synonym_lower = (row.get("synonym") or "").lower()
                        if name_lower == q_lower:
                            return 0
                        if name_lower.startswith(q_lower):
                            return 1
                        synonym_tail = synonym_lower.split(": ", 1)[-1] if ": " in synonym_lower else synonym_lower
                        if q_lower in synonym_tail:
                            return 2
                        return 3

                    ranked = [
                        (_rank_for_query(r), r.get("category", ""), r.get("object_name", ""), r)
                        for r in result + ext_rows
                    ]
                    ranked.sort(key=lambda x: (x[0], x[1], x[2]))
                    result = [item[3] for item in ranked]
        return result[:limit]

    def search_regions(query: str = "", limit: int = 200, count_only: bool = False) -> list[dict] | dict:
        """Search code regions (#Область/#Region) by name substring.

        Args:
            query: Search string (e.g. 'Себестоимость', 'Инициализация').
            limit: Max results (default 200).
            count_only: если True — вернуть само-описательный dict
                {total, source, truncated, scope:"main_index"} вместо списка.
                Это INDEX-side счёт по основной конфигурации (расширения НЕ
                учитываются — для них census обычно не нужен). Для учёта
                расширений считай по полной выдаче (count_only=False).

        Returns: list of dicts {name, line, end_line, module_path, object_name, category};
                 либо dict {total, source, truncated, scope} при count_only=True.
                 Empty list if index not available or no regions built."""
        if count_only:
            total = idx_reader.count_regions(query) if idx_reader is not None else None
            if total is None:
                return {"total": 0, "source": "unavailable", "truncated": False, "scope": "main_index"}
            return {"total": total, "source": "index", "truncated": False, "scope": "main_index"}
        result: list[dict] = []
        if idx_reader is not None:
            indexed = idx_reader.search_regions(query, limit)
            if indexed is not None:
                result = list(indexed)
        _ensure_index()
        # Same rank-merge as search_methods — see _rank_merge_ext_into_main.
        if _extension_paths_set and query:
            ext_rows = _live_search_regions(query, limit)
            result = _rank_merge_ext_into_main(
                result, ext_rows, query, name_keys=("name",), dedup_keys=("module_path", "name"), limit=limit
            )
        return result[:limit]

    def search_module_headers(query: str = "", limit: int = 200, count_only: bool = False) -> list[dict] | dict:
        """Search module header comments by substring.

        Args:
            query: Search string (e.g. 'себестоимость', 'доработка').
            limit: Max results (default 200).
            count_only: если True — вернуть само-описательный dict
                {total, source, truncated, scope:"main_index"} вместо списка.
                Это INDEX-side счёт по основной конфигурации (расширения НЕ
                учитываются). Для учёта расширений считай по полной выдаче.

        Returns: list of dicts {module_path, object_name, category, header_comment};
                 либо dict {total, source, truncated, scope} при count_only=True.
                 Empty list if index not available or no headers built."""
        if count_only:
            total = idx_reader.count_module_headers(query) if idx_reader is not None else None
            if total is None:
                return {"total": 0, "source": "unavailable", "truncated": False, "scope": "main_index"}
            return {"total": total, "source": "index", "truncated": False, "scope": "main_index"}
        result: list[dict] = []
        if idx_reader is not None:
            indexed = idx_reader.search_module_headers(query, limit)
            if indexed is not None:
                result = list(indexed)
        _ensure_index()
        # No clear name field for rank → reserve a quota for ext rows so a
        # saturated main index does not starve them (codex round 5).
        if _extension_paths_set and query:
            ext_rows = _live_search_module_headers(query, limit)
            result = _reserve_merge_ext_into_main(
                result, ext_rows, dedup_keys=("module_path", "header_comment"), limit=limit
            )
        return result[:limit]

    _VALID_SCOPES = frozenset({"all", "methods", "objects", "regions", "headers", "attributes", "predefined"})

    def search(query: str, scope: str = "all", limit: int = 30) -> list[dict]:
        """Unified search across methods, objects, regions, headers, attributes, predefined.

        Args:
            query: Search string (required).
            scope: Filter — 'all', 'methods', 'objects', 'regions', 'headers', 'attributes', 'predefined'.
            limit: Max results (applied to final list).

        Returns: list of dicts {text, source_type, object_name, path, path_kind, detail}.
        """
        if scope not in _VALID_SCOPES:
            msg = f"Unknown scope '{scope}'. Valid: {', '.join(sorted(_VALID_SCOPES))}"
            raise ValueError(msg)

        query = query.strip() if query else ""
        empty_query = not query
        if empty_query and scope == "all":
            return []

        per_source = max(limit // 6, 3) if scope == "all" else limit
        results: list[dict] = []

        if scope in ("all", "methods"):
            if not empty_query:  # search_methods('') → [] by design
                for m in search_methods(query, limit=per_source):
                    results.append(
                        {
                            "text": m["name"],
                            "source_type": "method",
                            "object_name": m.get("object_name", ""),
                            "path": m.get("module_path", ""),
                            "path_kind": "bsl",
                            "detail": m,
                        }
                    )

        if scope in ("all", "objects"):
            raw = search_objects(query, limit=per_source)
            if raw:
                for o in raw:
                    results.append(
                        {
                            "text": o["synonym"],
                            "source_type": "object",
                            "object_name": o.get("object_name", ""),
                            "path": o.get("file", ""),
                            "path_kind": "metadata",
                            "detail": o,
                        }
                    )

        if scope in ("all", "regions"):
            raw = search_regions(query, limit=per_source)
            if raw:
                for r in raw:
                    results.append(
                        {
                            "text": r["name"],
                            "source_type": "region",
                            "object_name": r.get("object_name", ""),
                            "path": r.get("module_path", ""),
                            "path_kind": "bsl",
                            "detail": r,
                        }
                    )

        if scope in ("all", "headers"):
            raw = search_module_headers(query, limit=per_source)
            if raw:
                for h in raw:
                    results.append(
                        {
                            "text": h["header_comment"],
                            "source_type": "header",
                            "object_name": h.get("object_name", ""),
                            "path": h.get("module_path", ""),
                            "path_kind": "bsl",
                            "detail": h,
                        }
                    )

        if scope in ("all", "attributes"):
            _attrs = find_attributes(name=query) if query else find_attributes()
            for a in _attrs[:per_source]:
                type_str = ", ".join(a["attr_type"]) if a["attr_type"] else ""
                results.append(
                    {
                        "text": f"{a['attr_name']} ({type_str})" if type_str else a["attr_name"],
                        "source_type": "attribute",
                        "object_name": a.get("object_name", ""),
                        "path": a.get("source_file", ""),
                        "path_kind": "metadata",
                        "detail": a,
                    }
                )

        if scope in ("all", "predefined"):
            _preds = find_predefined(name=query) if query else find_predefined()
            for p in _preds[:per_source]:
                type_str = ", ".join(p["types"]) if p.get("types") else ""
                results.append(
                    {
                        "text": f"{p.get('item_synonym') or p['item_name']} ({type_str})"
                        if type_str
                        else p.get("item_synonym") or p["item_name"],
                        "source_type": "predefined",
                        "object_name": p.get("object_name", ""),
                        "path": p.get("source_file", ""),
                        "path_kind": "metadata",
                        "detail": p,
                    }
                )

        return results[:limit]

    def get_index_info() -> dict:
        """Return index metadata: version, capabilities, staleness."""
        if idx_reader is None:
            return {"status": "no_index"}
        # An in-place rebuild leaves build_in_progress=1; reporting status:"ok" with zeros
        # would be a lie ("index empty and ok"). get_index_db_path is NOT imported at the
        # bsl_helpers top level → local import (mirrors _git_grep usage elsewhere).
        from rlm_tools_bsl.bsl_index import (
            get_index_db_path,
            index_incomplete,
            stats_indicate_load_failure,
        )

        db_path = get_index_db_path(base_path)
        if index_incomplete(db_path):
            return {"status": "incomplete"}
        stats = idx_reader.get_statistics()
        # Race guard (codex High): get_statistics is _transient_safe → a concurrent rebuild's
        # DROP window yields a zero/load-failure sentinel (not an exception). Re-check the
        # marker too (not just the stats sentinel): in the [empty tables + stale meta still
        # present] sub-window of _begin_inplace_rebuild, built_at/builder_version are NOT yet
        # cleared so stats_indicate_load_failure is False — the marker is the only signal.
        # Mirror rlm_start's combined post-read check. Don't report status:"ok" with zeros.
        if index_incomplete(db_path) or stats_indicate_load_failure(stats):
            return {"status": "incomplete"}
        builder = int(stats.get("builder_version") or 0)
        return {
            "status": "ok",
            "builder_version": builder,
            "config_name": stats.get("config_name", ""),
            "config_version": stats.get("config_version", ""),
            "modules": stats.get("modules", 0),
            "methods": stats.get("methods", 0),
            "has_fts": stats.get("has_fts", False),
            "has_synonyms": bool(stats.get("object_synonyms", 0)),
            "object_synonyms": stats.get("object_synonyms", 0),
            "has_regions": builder >= 8,
            "has_module_headers": builder >= 8,
            "has_extension_overrides": builder >= 9,
            "extension_overrides": stats.get("extension_overrides", 0),
            "has_form_elements": builder >= 10 and stats.get("has_metadata", False),
            "form_elements_count": stats.get("form_elements", 0),
            "has_object_attributes": builder >= 11 and stats.get("has_metadata", False),
            "object_attributes_count": stats.get("object_attributes", 0),
            "has_predefined_items": builder >= 11 and stats.get("has_metadata", False),
            "predefined_items_count": stats.get("predefined_items", 0),
            # v12 reverse-index (v1.9.0+): metadata_references + 3 specialised tables
            "has_metadata_references": builder >= 12 and (stats.get("metadata_references") or 0) > 0,
            "metadata_references_count": stats.get("metadata_references", 0),
            "exchange_plan_content_count": stats.get("exchange_plan_content", 0),
            "defined_types_count": stats.get("defined_types", 0),
            "characteristic_types_count": stats.get("characteristic_types", 0),
            # v13 reverse code-usage index (v1.14.0). Capability is builder-gated,
            # NOT count>0 — an empty table is a valid (no-usages) answer.
            "has_metadata_code_usages": builder >= 13,
            "metadata_code_usages_count": stats.get("metadata_code_usages", 0),
            # Git fast-path acceleration availability for incremental update (v1.8.0+)
            "git_accelerated": bool(stats.get("git_accelerated")),
            "git_head_commit": stats.get("git_head_commit"),
            "built_at": stats.get("built_at"),
        }

    # ── Help (uses _registry for recipes) ──────────────────────

    def bsl_help(task: str = "") -> str:
        """Get a recipe for your task. Call help() to see all recipes,
        or help('find exports') / help('граф вызовов') for a specific one.

        Returns: str with Python code example."""
        task_lower = task.lower()

        if not task_lower:
            lines = ["Available recipes (call help('keyword') for details):\n"]
            for name, entry in _registry.items():
                if entry["recipe"]:
                    first_line = entry["recipe"].split("\n")[0]
                    lines.append(f"  help('{name}') - {first_line}")
            return "\n".join(lines)

        # Search by helper name first (exact match)
        if task_lower in _registry and _registry[task_lower]["recipe"]:
            return _registry[task_lower]["recipe"]

        # Pass 1: ТОЧНОЕ совпадение task с keyword.
        # Без этого прохода длинные keywords типа "иерархия вызовов" в
        # find_call_hierarchy теряются: substring "вызов" в kw у
        # find_callers_context (зарегистрирован раньше) ловится первым.
        # Точное совпадение даёт правильный приоритет независимо от порядка
        # регистрации.
        for name, entry in _registry.items():
            if not entry["recipe"]:
                continue
            for kw in entry["kw"]:
                if kw == task_lower:
                    return entry["recipe"]

        # Pass 2: substring matching (для запросов, не совпадающих точно).
        for name, entry in _registry.items():
            if not entry["recipe"]:
                continue
            if name in task_lower:
                return entry["recipe"]
            for kw in entry["kw"]:
                if kw in task_lower:
                    return entry["recipe"]

        # Bridge to _BUSINESS_RECIPES (G.5b) — for words that are recipe domain
        # keys / aliases but not helper keywords.
        try:
            from rlm_tools_bsl.bsl_knowledge import _BUSINESS_RECIPES, _match_recipe

            domain = _match_recipe(task_lower)
            if domain and domain in _BUSINESS_RECIPES:
                recipe = _BUSINESS_RECIPES[domain]
                lines = [f"BUSINESS RECIPE: {domain}", ""]
                for i, step in enumerate(recipe.get("compact", []), 1):
                    lines.append(f"  {i}. {step}")
                code_hint = recipe.get("code_hint")
                if code_hint:
                    lines += ["", "Ready-to-use code:", code_hint]
                return "\n".join(lines)
        except Exception:
            pass  # bridge не должен ломать существующее поведение

        # Fallback: show all recipes
        return bsl_help("")

    # ── Query extraction ───────────────────────────────────────

    _QUERY_ASSIGN_RE = re.compile(
        r'(?:Запрос\.Текст|ТекстЗапроса)\s*=\s*["\']',
        re.IGNORECASE,
    )
    _QUERY_TABLE_RE = re.compile(
        r"\b(?:ИЗ|FROM|СОЕДИНЕНИЕ|JOIN)\s+"
        r"((?:РегистрНакопления|РегистрСведений|РегистрБухгалтерии|"
        r"Справочник|Документ|"
        r"AccumulationRegister|InformationRegister|AccountingRegister|"
        r"Catalog|Document)\.\w+)",
        re.IGNORECASE,
    )

    def extract_queries(path: str) -> list[dict]:
        """Extract embedded 1C queries from a BSL module.

        Finds Запрос.Текст = "..." and ТекстЗапроса = "..." patterns,
        extracts table names from query text.

        Returns: list of dicts {procedure, line, tables: [str], text_preview}."""
        content = _ext_read_file(path)
        lines = content.splitlines()
        procs = extract_procedures(path)

        queries: list[dict] = []
        i = 0
        while i < len(lines):
            line = lines[i]
            m = _QUERY_ASSIGN_RE.search(line)
            if not m:
                i += 1
                continue

            # Collect multiline query text (1C uses | prefix for continuation)
            query_start = i
            query_lines = [line[m.end() :]]
            j = i + 1
            while j < len(lines):
                stripped = lines[j].strip()
                if stripped.startswith("|") or stripped.startswith('"'):
                    query_lines.append(stripped.lstrip("|").lstrip('"'))
                elif stripped.startswith("'") or stripped == "":
                    query_lines.append(stripped.lstrip("'"))
                else:
                    break
                j += 1
            query_text = "\n".join(query_lines)

            # Extract table names
            tables = list(dict.fromkeys(m2.group(1) for m2 in _QUERY_TABLE_RE.finditer(query_text)))

            # Determine which procedure this belongs to
            line_num = query_start + 1  # 1-based
            proc_name = ""
            for p in procs:
                if p["line"] <= line_num <= (p["end_line"] or len(lines)):
                    proc_name = p["name"]
                    break

            preview = query_text[:200].strip()
            if len(query_text) > 200:
                preview += "..."

            queries.append(
                {
                    "procedure": proc_name,
                    "line": line_num,
                    "tables": tables,
                    "text_preview": preview,
                }
            )
            i = j
        return queries

    # ── Code metrics ─────────────────────────────────────────

    _COMMENT_RE = re.compile(r"^\s*//")
    _NESTING_OPEN_RE = re.compile(r"\b(Если|Для|Пока|Попытка|If|For|While|Try)\b", re.IGNORECASE)
    _NESTING_CLOSE_RE = re.compile(r"\b(КонецЕсли|КонецЦикла|КонецПопытки|EndIf|EndDo|EndTry)\b", re.IGNORECASE)

    def code_metrics(path: str) -> dict:
        """Compute code metrics for a BSL module.

        Returns: dict {total_lines, code_lines, comment_lines, empty_lines,
                 procedures_count, exports_count, avg_proc_size, max_nesting}."""
        content = _ext_read_file(path)
        lines = content.splitlines()

        # Single-pass: empty, comment, nesting depth
        total = len(lines)
        empty = 0
        comment = 0
        max_nesting = 0
        current_nesting = 0
        for line in lines:
            stripped = line.strip()
            if not stripped:
                empty += 1
            elif _COMMENT_RE.match(line):
                comment += 1
            else:
                for _ in _NESTING_OPEN_RE.finditer(line):
                    current_nesting += 1
                    if current_nesting > max_nesting:
                        max_nesting = current_nesting
                for _ in _NESTING_CLOSE_RE.finditer(line):
                    current_nesting = max(0, current_nesting - 1)
        code = total - empty - comment

        procs = extract_procedures(path)
        exports = [p for p in procs if p.get("is_export")]

        sizes = [(p["end_line"] or total) - p["line"] + 1 for p in procs]
        avg_size = round(sum(sizes) / len(sizes), 1) if sizes else 0

        return {
            "total_lines": total,
            "code_lines": code,
            "comment_lines": comment,
            "empty_lines": empty,
            "procedures_count": len(procs),
            "exports_count": len(exports),
            "avg_proc_size": avg_size,
            "max_nesting": max_nesting,
        }

    # ── Extensions ───────────────────────────────────────────

    def detect_extensions() -> dict:
        """Обнаружить расширения рядом и текущую роль конфигурации.

        Каждый элемент ``nearby_extensions`` несёт ``overrides_count`` — счёт
        перехватов из ИНДЕКСА по корню расширения (index-side, дёшево). В
        MAIN-сессии: ``0`` = расширение без перехватов, ``int`` = число. ``None`` =
        счётчик недоступен (нет индекса/таблицы) ЛИБО индекс не покрывал это
        расширение (напр. EXTENSION-сессия → siblings не в индексе).
        Caveat: ``0``/``int`` — по СНИМКУ индекса; таблица extension_overrides
        хранит только строки перехватов, НЕ список покрытых расширений, поэтому на
        stale-индексе НОВОЕ расширение без строк тоже отдаст ``0``. Для точного
        live-счёта используй rlm_start или find_ext_overrides(ext_path)."""
        from rlm_tools_bsl.extension_detector import ConfigRole, detect_extension_context as _det

        ctx = _det(base_path)

        # Index-first overrides_count by extension_root (no live BSL scan — keeps
        # detect_extensions a cheap discovery helper). Match by ONE normalized path
        # form on both sides (codex round 4/5).
        def _norm(p: str) -> str:
            return os.path.normcase(os.path.normpath(os.path.abspath(p)))

        raw_counts = idx_reader.count_overrides_by_extension_root() if idx_reader is not None else None
        norm_counts = {_norm(k): v for k, v in raw_counts.items()} if raw_counts is not None else None
        # "Known zero" (0) is valid ONLY when the index covers the nearby set — i.e.
        # a MAIN session builds rows for every nearby extension. In an EXTENSION
        # session the index only covers current.path, so siblings are "unknown" (None).
        main_covers = norm_counts is not None and ctx.current.role == ConfigRole.MAIN

        def _ovr_count(ext_path: str):
            if norm_counts is None:
                return None
            if main_covers:
                return norm_counts.get(_norm(ext_path), 0)
            return norm_counts.get(_norm(ext_path))

        result = {
            "config_role": ctx.current.role.value,
            "config_name": ctx.current.name,
            "config_prefix": ctx.current.name_prefix,
            "warnings": ctx.warnings,
            "nearby_extensions": [
                {
                    "name": e.name,
                    "purpose": e.purpose,
                    "prefix": e.name_prefix,
                    "path": e.path,
                    "overrides_count": _ovr_count(e.path),
                }
                for e in ctx.nearby_extensions
            ],
            "nearby_main": None,
        }
        if ctx.nearby_main:
            result["nearby_main"] = {
                "name": ctx.nearby_main.name,
                "path": ctx.nearby_main.path,
            }
        return result

    def find_ext_overrides(extension_path: str, object_name: str = "") -> dict:
        """Найти перехваченные методы в расширении.
        extension_path — путь к расширению (из detect_extensions).
        object_name — имя объекта для прицельного поиска ('' = все)."""
        from rlm_tools_bsl.extension_detector import find_extension_overrides as _feo

        diagnostics: dict = {}
        overrides = _feo(extension_path, object_name or None, diagnostics=diagnostics)
        result = {
            "extension_path": extension_path,
            "object_filter": object_name or "(all)",
            "overrides": overrides[:200],
            "total": len(overrides),
            "truncated": len(overrides) > 200,
            "partial": not diagnostics.get("complete", True),
        }
        if result["partial"]:
            result["_meta"] = {"scan_diagnostics": diagnostics}
        return result

    _OVERRIDES_CAP = 200
    _OVERRIDES_TOP_N = 20

    def _overrides_payload(
        rows: list[dict],
        source: str,
        *,
        partial: bool = False,
        failed_extension_roots: list[dict] | None = None,
    ) -> dict:
        """Ответ get_overrides: агрегаты по прочитанному набору + детерминированный срез.

        Срез в cap=200 шёл в порядке вставки (SELECT без ORDER BY), то есть сгруппированным
        по расширениям: на конфигурации с сотнями перехватов весь срез забивало ОДНО крупное
        расширение, и группировка по видимым строкам давала неверный топ-объект. Счётчики
        считаем ДО обрезки (полный список и так в памяти), срез сортируем — чтобы он был
        воспроизводим. Один и тот же shape отдают ВСЕ ветки (index/live/unavailable), иначе
        код агента, читающий by_object_top, падает на конфигурации без расширений.
        """

        # Агрегация CASE-INSENSITIVE: имена объектов/расширений в 1С регистронезависимы, и
        # фильтры reader'а (get_extension_overrides) сравнивают через .lower(). Если
        # группировать по исходному написанию, «Объект» и «объект» дадут ДВА элемента
        # by_object_top и unique_objects=2 — то есть агрегат разошёлся бы с семантикой
        # фильтров ТОГО ЖЕ API.
        # НОРМАЛИЗАЦИЯ ИМЕННО .lower(), НЕ .casefold(): цель — БУКВАЛЬНО повторить семантику
        # фильтра reader'а (bsl_index.py, get_extension_overrides). На русских именах они
        # совпадают, но в общем Unicode — нет, и casefold склеил бы значения, которые фильтр
        # того же API считает РАЗНЫМИ. Вторую семантику не изобретаем; если когда-то
        # переводить на casefold — то ОБА места одним изменением.
        # display-name выбирается ДЕТЕРМИНИРОВАННО — минимальное написание в группе.
        # «Первое встреченное» зависело бы от порядка выдачи SQLite (SELECT без ORDER BY), и
        # ключ by_object_top у пары «Номенклатура»/«номенклатура» скакал бы между прогонами,
        # хотя счётчик уже регистронезависим.
        def _bump(counter: dict[str, list], display: str) -> None:
            key = display.lower()
            slot = counter.get(key)
            if slot is None:
                counter[key] = [display, 1]
            else:
                slot[1] += 1
                if display < slot[0]:
                    slot[0] = display  # min() по исходному написанию — стабильно

        def _top(counter: dict[str, list], n: int) -> dict[str, int]:
            items = [(disp, cnt) for disp, cnt in counter.values()]
            return dict(sorted(items, key=lambda kv: (-kv[1], kv[0]))[:n])

        by_object: dict[str, list] = {}
        by_extension: dict[str, list] = {}
        by_annotation: dict[str, list] = {}
        methods: set[str] = set()
        for r in rows:
            obj = r.get("object_name") or ""
            ext = r.get("extension_name") or ""
            ann = r.get("annotation") or ""
            if obj:
                _bump(by_object, obj)
            if ext:
                _bump(by_extension, ext)
            if ann:
                _bump(by_annotation, ann)
            if r.get("target_method"):
                methods.add(str(r["target_method"]).lower())  # та же нормализация, что у фильтра

        # Детерминизм ПОЛНЫЙ: бизнес-ключи задают осмысленный порядок, а финальный
        # tie-breaker — стабильная сериализация ВСЕЙ строки, чтобы различимые записи не
        # зависели от порядка выдачи SQLite (SELECT * несёт и extension_root, и
        # target_method_line, и прочие поля, которых нет в ключе).
        def _sort_key(r: dict) -> tuple:
            return (
                (r.get("object_name") or "").lower(),
                (r.get("target_method") or "").lower(),
                (r.get("extension_name") or "").lower(),
                (r.get("annotation") or "").lower(),
                (r.get("extension_method") or "").lower(),
                (r.get("source_path") or "").lower(),
                json.dumps(r, sort_keys=True, ensure_ascii=False, default=str),
            )

        ordered = sorted(rows, key=_sort_key)
        total = len(rows)
        payload = {
            "overrides": ordered[:_OVERRIDES_CAP],
            "total": total,
            "truncated": total > _OVERRIDES_CAP,
            "source": source,
            "partial": partial,
            "by_annotation": _top(by_annotation, len(by_annotation)),  # аннотаций мало — все
            "by_object_top": _top(by_object, _OVERRIDES_TOP_N),
            "by_extension_top": _top(by_extension, _OVERRIDES_TOP_N),
            "unique_objects": len(by_object),  # lower()-ключи → регистр не двоит
            "unique_methods": len(methods),  # тот же lower()
            "unique_extensions": len(by_extension),
        }
        if failed_extension_roots:
            payload["_meta"] = {"failed_extension_roots": failed_extension_roots}
        return payload

    def get_overrides(object_name: str = "", method_name: str = "") -> dict:
        """Перехваченные методы из индекса (мгновенно).
        object_name/method_name — фильтры ('' = все).
        Возвращает: {overrides: [...], total: N, truncated: bool, partial: bool,
                     source: "index"|"live"|"unavailable",
                     by_annotation, by_object_top, by_extension_top,
                     unique_objects, unique_methods, unique_extensions}.
        Без фильтра ``overrides`` отдает ПЕРВЫЕ 200 перехватов (cap=200), детерминированно
        ОТСОРТИРОВАННЫХ. ``total`` — полное число перехватов; ``truncated`` сигналит обрезку.
        **СТАТИСТИКУ бери из агрегатов** (``by_annotation`` — все аннотации; ``by_object_top``
        / ``by_extension_top`` — топ-20; ``unique_*``): при ``partial=false`` они посчитаны по
        ПОЛНОМУ выбранному источнику, а при ``partial=true`` — только по успешно прочитанной
        части (см. ``_meta.failed_extension_roots``). Группировать усеченный ``overrides``
        вручную НЕЛЬЗЯ — срез не репрезентативен. Shape одинаков во всех ветках
        (index/live/unavailable).
        Каждый перехват ГАРАНТИРОВАННО несёт ключ ``extension_name`` (имя расширения)
        во всех ветках источника — index, live из main-сессии и live из сессии,
        открытой прямо на расширении (нормализуется из идентичности текущего
        расширения)."""
        # Try index first
        if idx_reader is not None:
            result = idx_reader.get_extension_overrides(object_name, method_name)
            if result is not None:
                return _overrides_payload(result, source="index")
        # Live fallback
        from rlm_tools_bsl.extension_detector import (
            detect_extension_context as _det,
            find_extension_overrides as _feo,
        )

        try:
            ctx = _det(base_path)
        except Exception as exc:
            return _overrides_payload(
                [],
                source="unavailable",
                partial=True,
                failed_extension_roots=[
                    {
                        "extension_name": "",
                        "extension_root": base_path,
                        "error": type(exc).__name__,
                        "message": str(exc),
                    }
                ],
            )  # ← единый shape

        from rlm_tools_bsl.extension_detector import ConfigRole

        all_overrides: list[dict] = []
        failed_extension_roots: list[dict] = []
        successful_scans = 0

        def _scan_live_extension(extension_path: str, extension_name: str) -> list[dict]:
            nonlocal successful_scans
            diagnostics: dict = {}
            try:
                rows = _feo(extension_path, object_name or None, diagnostics=diagnostics)
            except Exception as exc:
                failed_extension_roots.append(
                    {
                        "extension_name": extension_name,
                        "extension_root": extension_path,
                        "error": type(exc).__name__,
                        "message": str(exc),
                    }
                )
                return []

            # A complete empty root is a successful scan.  An incomplete scan counts as
            # successful only if at least one candidate file was actually read; a missing
            # root / top-level walk failure must not make an all-failed aggregate look live.
            if diagnostics.get("complete", True) or diagnostics.get("files_scanned", 0) > 0:
                successful_scans += 1
            if not diagnostics.get("complete", True):
                failed_extension_roots.append(
                    {
                        "extension_name": extension_name,
                        "extension_root": extension_path,
                        "diagnostics": diagnostics,
                    }
                )
            return rows

        if ctx.current.role == ConfigRole.EXTENSION:
            all_overrides = _scan_live_extension(base_path, ctx.current.name or "")
            # Contract normalization: raw _feo rows lack extension_name/extension_root
            # (unlike index rows and the MAIN-session branch below). Fill them from
            # the current extension's own identity so EVERY override carries
            # extension_name regardless of source — consumers/recipes rely on it.
            for ov in all_overrides:
                ov.setdefault("extension_name", ctx.current.name or "")
                ov.setdefault("extension_root", ctx.current.path or base_path)
        elif ctx.current.role == ConfigRole.MAIN and ctx.nearby_extensions:
            for ext in ctx.nearby_extensions:
                ovs = _scan_live_extension(ext.path, ext.name)
                for ov in ovs:
                    ov["extension_name"] = ext.name
                    ov["extension_root"] = ext.path
                all_overrides.extend(ovs)
        elif ctx.current.role == ConfigRole.UNKNOWN:
            # The current root cannot be classified as MAIN or EXTENSION, so even a
            # successful scan of detected sibling CFE roots is only a lower bound.
            failed_extension_roots.append(
                {
                    "extension_name": ctx.current.name or "",
                    "extension_root": ctx.current.path or base_path,
                    "error": "UnknownConfigRole",
                    "message": "Current configuration root could not be classified as main or extension",
                }
            )
            for ext in ctx.nearby_extensions:
                ovs = _scan_live_extension(ext.path, ext.name)
                for ov in ovs:
                    ov["extension_name"] = ext.name
                    ov["extension_root"] = ext.path
                all_overrides.extend(ovs)

        if method_name:
            all_overrides = [ov for ov in all_overrides if ov.get("target_method", "").lower() == method_name.lower()]

        failed_extension_roots.sort(
            key=lambda row: (
                (row.get("extension_name") or "").lower(),
                (row.get("extension_root") or "").lower(),
                row.get("error") or "",
            )
        )
        source = "unavailable" if failed_extension_roots and successful_scans == 0 else "live"
        return _overrides_payload(
            all_overrides,
            source=source,
            partial=bool(failed_extension_roots),
            failed_extension_roots=failed_extension_roots,
        )

    # ── v1.9.0: find_references_to_object + find_defined_types ───────
    # Russian → English metadata prefix map (canonical singular form)
    _RU_META_PREFIXES: dict[str, str] = {
        "Справочник.": "Catalog.",
        "Документ.": "Document.",
        "Перечисление.": "Enum.",
        "РегистрСведений.": "InformationRegister.",
        "РегистрНакопления.": "AccumulationRegister.",
        "РегистрБухгалтерии.": "AccountingRegister.",
        "РегистрРасчета.": "CalculationRegister.",
        "ПланВидовХарактеристик.": "ChartOfCharacteristicTypes.",
        "ПланСчетов.": "ChartOfAccounts.",
        "ПланВидовРасчета.": "ChartOfCalculationTypes.",
        "ПланОбмена.": "ExchangePlan.",
        "ОпределяемыйТип.": "DefinedType.",
        "БизнесПроцесс.": "BusinessProcess.",
        "Задача.": "Task.",
        "Отчет.": "Report.",
        "Обработка.": "DataProcessor.",
        "Константа.": "Constant.",
        "Подсистема.": "Subsystem.",
        "Роль.": "Role.",
        "ОбщаяКоманда.": "CommonCommand.",
        "ФункциональнаяОпция.": "FunctionalOption.",
        "ПодпискаНаСобытие.": "EventSubscription.",
        # Русские RUNTIME-формы (v1.28.0): английские (DocumentRef./DocumentObject./…)
        # канонизирует canonicalize_type_ref, а их русские двойники — нет. Без этих строк
        # "ДокументСсылка.X" оставался неканоничным ref'ом (запрос к индексу по нему заведомо
        # пуст, а category-aware ветвление считало его НЕ-документом). Четыре документ/
        # справочник-формы _META_TYPE_PREFIXES принимал и раньше (_strip_meta_prefix их срезал);
        # "ПеречислениеСсылка." там НЕТ — её поддержка новая (симметрии ради). Точка в ключе
        # обязательна: она не даёт более короткому "Документ." перехватить "ДокументСсылка.".
        "ДокументСсылка.": "Document.",
        "ДокументОбъект.": "Document.",
        "СправочникСсылка.": "Catalog.",
        "СправочникОбъект.": "Catalog.",
        "ПеречислениеСсылка.": "Enum.",
    }

    def _normalize_object_ref(s: str) -> tuple[str, list[str]]:
        """Normalize input object reference to canonical form (e.g. 'Catalog.X').

        Accepts Russian/English prefixes and Ref/Object/Manager/etc. forms.
        Returns (canonical, [canonical]) — match_forms list kept short because
        the index stores ref_object only in canonical form.
        """
        from rlm_tools_bsl.bsl_xml_parsers import canonicalize_type_ref as _ctr

        if not s:
            return ("", [])
        text = s.strip()
        # Convert Russian prefix to English (most common: "Справочник.X").
        # Case-insensitive on the prefix (casefold, Cyrillic-aware) so that
        # "ДОКУМЕНТ.X" / "документ.X" normalize the same as "Документ.X" — the
        # object NAME part is preserved as-is (its case is handled downstream by
        # object_ref_key/py_lower lookups).
        for ru, en in _RU_META_PREFIXES.items():
            if text[: len(ru)].casefold() == ru.casefold():
                text = en + text[len(ru) :]
                break
        # Already canonical form like "Catalog.X" passes through canonicalize unchanged.
        canonical = _ctr(text)
        if not canonical:
            # Could be just a name without prefix — assume Catalog as default? No, keep as-is.
            canonical = text
        return canonical, [canonical]

    # Priority for sorting + truncation
    _REF_KIND_PRIORITY: dict[str, int] = {
        "attribute_type": 0,
        "subsystem_content": 1,
        "exchange_plan_content": 2,
        "functional_option_content": 3,
        "event_subscription_source": 4,
        "role_rights": 5,
        "defined_type_content": 6,
        "characteristic_type": 7,
        "owner": 8,
        "based_on": 9,
        "choice_parameter_link": 10,
        "link_by_type": 11,
        "main_form": 12,
        "list_form": 13,
        "default_object_form": 14,
        "default_list_form": 15,
        "command_parameter_type": 16,
        "predefined_characteristic_type": 17,
    }

    def find_references_to_object(
        object_ref: str,
        kinds: list[str] | None = None,
        limit: int = 1000,
        include_code: bool = False,
    ) -> dict:
        """Find all references to a metadata object (Configurator "Найти ссылки → В свойствах" analogue).

        Covers declarative metadata-XML references (attribute types, owner, subsystems,
        functional options, rights, …). Pass include_code=True to additionally run
        find_code_usages and surface in-code usages under separate `code_*` keys.

        Args:
            object_ref: e.g. 'Справочник.Контрагенты' or 'Catalog.Контрагенты'.
            kinds: optional filter by ref_kind (see _REF_KIND_PRIORITY for the list).
            limit: maximum references returned (default 1000).
            include_code: also include in-code usages (find_code_usages) under
                top-level keys code_usages/code_total/code_by_kind/code_truncated/
                code_partial/code_meta. Metadata keys are unchanged.

        Returns:
            {object, references, total, truncated, partial, by_kind}
            (+ code_* keys when include_code=True).
        """

        def _finish(res: dict) -> dict:
            if include_code:
                code = find_code_usages(object_ref, limit=limit)
                res["code_usages"] = code["usages"]
                res["code_total"] = code["total"]
                res["code_by_kind"] = code["by_kind"]
                res["code_truncated"] = code["truncated"]
                res["code_partial"] = code["partial"]
                res["code_meta"] = code["_meta"]
            return res

        canonical, _ = _normalize_object_ref(object_ref)
        result: dict = {
            "object": canonical,
            "references": [],
            "total": 0,
            "truncated": False,
            "partial": False,
            "by_kind": {},
        }
        if not canonical or "." not in canonical:
            return _finish(result)

        if idx_reader is not None:
            # Authoritative total + by_kind FIRST (cheap GROUP BY count)
            try:
                counts = idx_reader.count_metadata_references(canonical, kinds=kinds)
            except Exception:
                counts = None
            try:
                # SQL already orders by ref_kind priority + path + used_in,
                # so passing exact `limit` keeps the highest-priority refs.
                rows = idx_reader.find_metadata_references(canonical, kinds=kinds, limit=limit)
            except Exception:
                rows = None
            if rows is not None:
                if counts is not None:
                    result["total"] = counts["total"]
                    result["by_kind"] = counts["by_kind"]
                    if counts["total"] > limit:
                        result["truncated"] = True
                else:
                    result["total"] = len(rows)
                    result["by_kind"] = _count_by_kind([{"kind": r["ref_kind"]} for r in rows])
                result["references"] = [
                    {
                        "used_in": r["used_in"],
                        "path": r["path"],
                        "line": r["line"],
                        "kind": r["ref_kind"],
                    }
                    for r in rows
                ]
                return _finish(result)

        # Fallback: live scan
        result["partial"] = True
        all_refs = list(_live_find_references(canonical, kinds))
        result["total"] = len(all_refs)
        result["by_kind"] = _count_by_kind(all_refs)
        all_refs.sort(key=lambda x: (_REF_KIND_PRIORITY.get(x["kind"], 99), x["path"], x["used_in"]))
        if len(all_refs) > limit:
            result["truncated"] = True
            all_refs = all_refs[:limit]
        result["references"] = all_refs
        return _finish(result)

    def find_data_path(
        from_object: str,
        to_object: str,
        max_depth: int = 4,
        kinds: list[str] | None = None,
    ) -> dict:
        """N-hop reachability over the METADATA reference graph (declarative links).

        Answers "is ``to_object`` reachable from ``from_object`` by following
        metadata references?" — a forward BFS over ``find_metadata_refs_from``
        (attribute types, owner, based-on, subsystem content, …). Distinct from
        find_path, which walks the CODE call graph.

        Contract (R2 №3): BOTH endpoints MUST carry a recognized metadata-type
        prefix (``Справочник.X``/``Catalog.X``, ``Документ.Y``/``Document.Y``). A
        bare name without a prefix is NOT canonicalized (so a bare ``to`` could
        never match the always-canonical ``ref_object``, and a bare ``from`` loses
        its category) → we return a structural hint instead of walking. A
        bare→canonical resolver (via synonyms) is intentionally out of scope (YAGNI).

        Args:
            from_object / to_object: prefixed refs (RU or EN prefix accepted).
            max_depth: max edges in the path (clamped 1..8, default 4).
            kinds: optional ref_kind filter (see find_references_to_object).

        Returns:
            {found, from:from_canon, to:to_canon,
             path:[{from, to, kind, used_in, path, line}]|None, depth, partial,
             _meta:{max_depth, nodes_expanded, node_budget, budget_exceeded, kinds}}

            Each path element is an EDGE (``from`` references ``to``). ``partial=True``
            ⇔ the index lacks metadata_references (no live fallback) or a scan hit
            the table-missing guard mid-walk. ``budget_exceeded=True`` ⇔ the node
            budget was reached (widen scope / lower depth), NOT a proven absence.
        """
        # Builder-internal category↔prefix map — lazy import + one-shot local
        # inversion (no module-level coupling, R2 №5). Values are singular prefixes
        # matching the canonical ref_object prefix.
        from rlm_tools_bsl.bsl_index import _CATEGORY_TO_TYPE_PREFIX as _cat2prefix

        prefix_to_category = {prefix: category for category, prefix in _cat2prefix.items()}

        try:
            max_depth_int = int(max_depth)
        except (TypeError, ValueError):
            max_depth_int = 4
        max_depth_int = max(1, min(8, max_depth_int))

        from_canon, _ = _normalize_object_ref(from_object)
        to_canon, _ = _normalize_object_ref(to_object)

        def _prefix_category(canon: str) -> str | None:
            if not canon or "." not in canon:
                return None
            return prefix_to_category.get(canon.split(".", 1)[0])

        base_meta = {
            "max_depth": max_depth_int,
            "nodes_expanded": 0,
            "node_budget": _DATA_PATH_NODE_BUDGET,
            "budget_exceeded": False,
            "kinds": kinds,
        }

        # Contract guard: both endpoints must carry a recognized prefix.
        if _prefix_category(from_canon) is None or _prefix_category(to_canon) is None:
            return {
                "found": False,
                "from": from_canon,
                "to": to_canon,
                "path": None,
                "depth": 0,
                "partial": False,
                "error": "endpoints must carry a recognized metadata-type prefix",
                "hint": (
                    "укажите префикс для ОБОИХ концов: Справочник./Документ./РегистрНакопления./… "
                    "(или Catalog./Document./AccumulationRegister./…)"
                ),
                "_meta": base_meta,
            }

        to_canon_cf = to_canon.casefold()

        # Trivial self-path.
        if from_canon.casefold() == to_canon_cf:
            return {
                "found": True,
                "from": from_canon,
                "to": to_canon,
                "path": [],
                "depth": 0,
                "partial": False,
                "_meta": base_meta,
            }

        if idx_reader is None:
            # No index → no metadata graph (no live fallback by design).
            return {
                "found": False,
                "from": from_canon,
                "to": to_canon,
                "path": None,
                "depth": 0,
                "partial": True,
                "_meta": base_meta,
            }

        # nodes[id] = {canon, in_edge: {from,to,kind,used_in,path,line}|None, parent_id}.
        # Cycle-detection in CANONICAL space (Catalog.X ≠ Document.X).
        nodes: dict[int, dict] = {0: {"canon": from_canon, "in_edge": None, "parent_id": None}}
        counter = 0
        hit_id: int | None = None
        visited: set[str] = set()
        queue: list[tuple[str, int, int]] = [(from_canon, 0, 0)]
        nodes_expanded = 0
        budget_exceeded = False
        partial = False

        while queue and hit_id is None:
            if nodes_expanded >= _DATA_PATH_NODE_BUDGET:
                budget_exceeded = True
                break
            cur_canon, depth, cur_id = queue.pop(0)
            cur_cf = cur_canon.casefold()
            if cur_cf in visited:
                continue
            visited.add(cur_cf)
            if depth >= max_depth_int:
                continue

            bare = cur_canon.split(".", 1)[-1]
            cat = _prefix_category(cur_canon)
            rows = idx_reader.find_metadata_refs_from(bare, source_category=cat, kinds=kinds)
            nodes_expanded += 1
            if rows is None:
                partial = True
                continue

            for r in rows:
                next_canon = r.get("ref_object") or ""
                if not next_canon:
                    continue
                edge = {
                    "from": cur_canon,
                    "to": next_canon,
                    "kind": r.get("ref_kind"),
                    "used_in": r.get("used_in"),
                    "path": r.get("path"),
                    "line": r.get("line"),
                }
                counter += 1
                cid = counter
                nodes[cid] = {"canon": next_canon, "in_edge": edge, "parent_id": cur_id}
                if next_canon.casefold() == to_canon_cf:
                    hit_id = cid
                    break
                if next_canon.casefold() not in visited:
                    queue.append((next_canon, depth + 1, cid))

        meta = {
            "max_depth": max_depth_int,
            "nodes_expanded": nodes_expanded,
            "node_budget": _DATA_PATH_NODE_BUDGET,
            "budget_exceeded": budget_exceeded,
            "kinds": kinds,
        }
        if hit_id is None:
            return {
                "found": False,
                "from": from_canon,
                "to": to_canon,
                "path": None,
                "depth": 0,
                "partial": partial,
                "_meta": meta,
            }

        # Reconstruct forward edge path [from→…→to]: walk parent_id from the hit
        # node back to start, collecting incoming edges, then reverse.
        edges_rev: list[dict] = []
        nid: int | None = hit_id
        while nid is not None:
            n = nodes[nid]
            if n["in_edge"] is not None:
                edges_rev.append(n["in_edge"])
            nid = n["parent_id"]
        path = list(reversed(edges_rev))
        return {
            "found": True,
            "from": from_canon,
            "to": to_canon,
            "path": path,
            "depth": len(path),
            "partial": partial,
            "_meta": meta,
        }

    def find_code_usages(
        object_ref: str,
        kind: str | None = None,
        limit: int = 1000,
    ) -> dict:
        """Find where a metadata object is used IN CODE (reverse code-usage search).

        Complements find_references_to_object (which covers declarative metadata-XML
        references). Backed by the metadata_code_usages index table (builder v13+).

        Captures (light regex layer, source-aware):
          - 'manager'  — collection access `Документы.X` / `Documents.X`;
          - 'ref_type' — type in a string literal `"ДокументСсылка.X"` / `"DocumentRef.X"`;
          - 'query'    — metadata path in a query literal `Документ.X` and
                         `Документ.X.Товары` ('member' = tabular section name).
        Does NOT capture attribute access via local variables (`Док.Товары.Количество`).

        Scope: main configuration modules only (extensions are not in the index).

        Args:
            object_ref: 'Документ.X' / 'Document.X'. The metadata-type prefix is
                accepted in either RU or EN form, case-insensitively; the object
                NAME part is also matched case-insensitively (incl. Cyrillic) via
                the stored object_ref_key.
            kind: optional filter — 'manager' | 'ref_type' | 'query'.
            limit: maximum usages returned (default 1000).

        Returns:
            {object, usages: [{path, object_name, category, module_type, line, kind, member}],
             by_kind, total, truncated, partial, _meta: {scope, extensions_included}}.
            partial=True only when the index lacks the table (rebuild required).
        """
        canonical, _ = _normalize_object_ref(object_ref)
        result: dict = {
            "object": canonical,
            "usages": [],
            "by_kind": {},
            "total": 0,
            "truncated": False,
            "partial": False,
            "_meta": {"scope": "main_config", "extensions_included": False},
        }
        if not canonical or "." not in canonical:
            return result

        if idx_reader is not None:
            try:
                counts = idx_reader.count_code_usages(canonical, kind=kind)
            except Exception:
                counts = None
            try:
                rows = idx_reader.find_code_usages(canonical, kind=kind, limit=limit)
            except Exception:
                rows = None
            if rows is not None:
                # Table present — authoritative answer (empty is a valid answer).
                if counts is not None:
                    result["total"] = counts["total"]
                    result["by_kind"] = counts["by_kind"]
                    if counts["total"] > limit:
                        result["truncated"] = True
                else:
                    result["total"] = len(rows)
                    result["by_kind"] = _count_by_kind([{"kind": r["kind"]} for r in rows])
                result["usages"] = rows
                return result

        # Fallback: table missing (pre-v13 index) — limited live grep by short name.
        result["partial"] = True
        result["_meta"]["hint"] = (
            "metadata_code_usages table missing — rebuild the index (rlm_index) for fast, complete code-usage search"
        )
        short_name = canonical.split(".", 1)[1] if "." in canonical else canonical
        usages: list[dict] = []
        try:
            for hit in safe_grep(re.escape(short_name), max_files=40):
                usages.append(
                    {
                        "path": hit["file"],
                        "object_name": short_name,
                        "category": "",
                        "module_type": "",
                        "line": hit["line"],
                        "kind": "unknown",
                        "member": None,
                    }
                )
        except Exception:
            pass
        result["total"] = len(usages)
        result["by_kind"] = _count_by_kind(usages)
        if len(usages) > limit:
            result["truncated"] = True
            usages = usages[:limit]
        result["usages"] = usages
        return result

    def _count_by_kind(refs: list[dict]) -> dict:
        out: dict[str, int] = {}
        for r in refs:
            k = r.get("kind", "")
            out[k] = out.get(k, 0) + 1
        return out

    def _live_find_references(canonical: str, kinds: list[str] | None) -> list[dict]:
        """Live scan fallback when metadata_references table is not available.

        Walks Documents/Catalogs/Subsystems/etc., parses metadata XML on the fly.
        """
        from rlm_tools_bsl.bsl_xml_parsers import (
            canonicalize_type_ref as _ctr,
            parse_command_parameter_type as _pcpt,
            parse_defined_type as _pdt,
            parse_exchange_plan_content as _pep,
            parse_metadata_xml as _pmx,
            parse_pvh_characteristics as _ppc,
        )

        canonical_lower = canonical.lower()
        kinds_set = set(kinds) if kinds else None
        results: list[dict] = []

        _CATEGORY_TYPE: dict[str, str] = {
            "Documents": "Document",
            "Catalogs": "Catalog",
            "Enums": "Enum",
            "InformationRegisters": "InformationRegister",
            "AccumulationRegisters": "AccumulationRegister",
            "AccountingRegisters": "AccountingRegister",
            "CalculationRegisters": "CalculationRegister",
            "ChartsOfAccounts": "ChartOfAccounts",
            "ChartsOfCharacteristicTypes": "ChartOfCharacteristicTypes",
            "ChartsOfCalculationTypes": "ChartOfCalculationTypes",
            "ExchangePlans": "ExchangePlan",
            "BusinessProcesses": "BusinessProcess",
            "Tasks": "Task",
            "Subsystems": "Subsystem",
            "FunctionalOptions": "FunctionalOption",
            "EventSubscriptions": "EventSubscription",
            "Reports": "Report",
            "DataProcessors": "DataProcessor",
            "Constants": "Constant",
            "DocumentJournals": "DocumentJournal",
        }

        scan_categories = list(_CATEGORY_TYPE.keys())
        # CommonCommands is also a top-level category contributing refs
        if "CommonCommands" not in scan_categories:
            scan_categories.append("CommonCommands")
            _CATEGORY_TYPE["CommonCommands"] = "CommonCommand"

        seen_files: set[Path] = set()
        # Object-level dedup: when same logical object is parsed via sibling .xml AND
        # via Ext/<Type>.xml, the second pass would emit duplicate refs.
        # Key: (used_in, kind) — the same logical reference is unambiguous regardless
        # of source file path (in production both files have identical content).
        emitted_keys: set[tuple[str, str]] = set()

        import re as _re

        def _resolve_attr_line(suffix: str, lines: list[str]) -> int | None:
            """Same heuristic as bsl_index._line_for_ref — find <Name>X</Name> line."""
            if not suffix:
                return None
            target_name: str | None = None
            if suffix.startswith(("Attribute.", "Dimension.", "Resource.")):
                parts = suffix.split(".")
                if len(parts) >= 2:
                    target_name = parts[1]
            elif suffix.startswith("TabularSection.") and ".Attribute." in suffix:
                after = suffix.split(".Attribute.", 1)[1]
                target_name = after.split(".", 1)[0]
            if not target_name:
                return None
            pat = _re.compile(rf"<\s*[Nn]ame\s*>{_re.escape(target_name)}<\s*/\s*[Nn]ame\s*>")
            for idx, line in enumerate(lines, start=1):
                if pat.search(line):
                    return idx
            return None

        def _emit_from_xml(xml_path: Path, category: str, fallback_name: str) -> None:
            if xml_path in seen_files:
                return
            seen_files.add(xml_path)
            try:
                content = xml_path.read_text(encoding="utf-8-sig", errors="replace")
            except OSError:
                return
            try:
                parsed = _pmx(content)
            except Exception:
                return
            if not parsed:
                return
            obj_name = parsed.get("name") or fallback_name
            rel = xml_path.relative_to(Path(base_path)).as_posix()
            type_prefix = _CATEGORY_TYPE.get(category, category)
            used_in_root = f"{type_prefix}.{obj_name}"
            content_lines: list[str] | None = None
            for ref in parsed.get("references", []):
                if ref.get("ref_object", "").lower() != canonical_lower:
                    continue
                kind = ref.get("ref_kind", "")
                if kinds_set is not None and kind not in kinds_set:
                    continue
                suffix = ref.get("used_in_suffix", "")
                used_in = f"{used_in_root}.{suffix}" if suffix else used_in_root
                key = (used_in, kind)
                if key in emitted_keys:
                    continue
                emitted_keys.add(key)
                if content_lines is None:
                    content_lines = content.splitlines()
                line = _resolve_attr_line(suffix, content_lines)
                results.append({"used_in": used_in, "path": rel, "line": line, "kind": kind})

        def _emit_command_param_refs(
            xml_path: Path,
            host_category: str,
            host_object: str,
        ) -> None:
            """Emit command_parameter_type refs from a single Command XML/.command/.mdo.

            host_category is the top-level category for source_category accounting:
            'CommonCommands' for top-level commands, or 'Catalogs'/'Documents'/...
            for object-nested commands.
            host_object is the source_object label used in `used_in`:
            command name itself for CommonCommands, parent object name otherwise.
            """
            if kinds_set is not None and "command_parameter_type" not in kinds_set:
                return
            if xml_path in seen_files:
                return
            seen_files.add(xml_path)
            try:
                content = xml_path.read_text(encoding="utf-8-sig", errors="replace")
            except OSError:
                return
            try:
                cmd_refs = _pcpt(content)
            except Exception:
                return
            if not cmd_refs:
                return
            rel = xml_path.relative_to(Path(base_path)).as_posix()
            for ref in cmd_refs:
                ref_object = ref.get("ref_object", "")
                if ref_object.lower() != canonical_lower:
                    continue
                cmd_name = ref.get("command_name", "") or xml_path.stem
                if host_category == "CommonCommands":
                    used_in = f"CommonCommand.{cmd_name}.CommandParameterType"
                else:
                    type_prefix = _CATEGORY_TYPE.get(host_category, host_category)
                    used_in = f"{type_prefix}.{host_object}.Command.{cmd_name}.CommandParameterType"
                key = (used_in, "command_parameter_type")
                if key in emitted_keys:
                    continue
                emitted_keys.add(key)
                results.append(
                    {
                        "used_in": used_in,
                        "path": rel,
                        "line": None,
                        "kind": "command_parameter_type",
                    }
                )

        # Walk every category: cover BOTH layouts
        # 1) <Category>/<Object>/{Object.mdo|Ext/<Type>.xml} (Catalogs/Documents/...)
        # 2) <Category>/<Object>.xml (top-level — Subsystems/X.xml, FunctionalOptions/X.xml,
        #    EventSubscriptions/X.xml, CommonCommands/X.xml — plus Subsystem nesting)
        for category in scan_categories:
            cat_dir = Path(base_path) / category
            if not cat_dir.is_dir():
                continue

            # Track layout-1 stems to avoid re-parsing the same logical object via
            # the sibling layout-2 pass (Catalogs/X/ + Catalogs/X.xml — same content).
            covered_stems: set[str] = set()

            # Layout 1: object subdirectories
            for obj_dir in cat_dir.iterdir():
                if not obj_dir.is_dir():
                    continue
                obj_name = obj_dir.name
                xml_path = None
                mdo = obj_dir / f"{obj_name}.mdo"
                if mdo.is_file():
                    xml_path = mdo
                else:
                    sibling = obj_dir.parent / f"{obj_name}.xml"
                    if sibling.is_file():
                        xml_path = sibling
                    else:
                        ext_dir = obj_dir / "Ext"
                        if ext_dir.is_dir():
                            for fp in sorted(ext_dir.iterdir()):
                                if fp.suffix.lower() == ".xml" and fp.is_file():
                                    xml_path = fp
                                    break
                if xml_path is not None:
                    _emit_from_xml(xml_path, category, obj_name)
                    covered_stems.add(obj_name)

                # Object-nested commands: <Cat>/<Obj>/Commands/<Cmd>.xml or
                # <Cat>/<Obj>/Commands/<Cmd>/<Cmd>.command (EDT)
                if category != "CommonCommands":
                    cmd_dir = obj_dir / "Commands"
                    if cmd_dir.is_dir():
                        for cmd_entry in cmd_dir.iterdir():
                            if cmd_entry.is_file() and cmd_entry.suffix.lower() == ".xml":
                                _emit_command_param_refs(cmd_entry, category, obj_name)
                            elif cmd_entry.is_dir():
                                for cand in (
                                    cmd_entry / f"{cmd_entry.name}.command",
                                    cmd_entry / f"{cmd_entry.name}.mdo",
                                ):
                                    if cand.is_file():
                                        _emit_command_param_refs(cand, category, obj_name)
                                        break

            # Layout 2: top-level *.xml / *.mdo files; skip files whose stem already
            # covered by a layout-1 obj-dir to avoid duplicate refs.
            for fp in cat_dir.rglob("*"):
                if not fp.is_file():
                    continue
                if fp.suffix.lower() not in (".xml", ".mdo"):
                    continue
                # Skip top-level sibling already handled by layout 1.
                if fp.parent == cat_dir and fp.stem in covered_stems:
                    continue
                # CommonCommands deserves command-parameter-type extraction in addition to
                # the regular metadata parse pass.
                if category == "CommonCommands":
                    _emit_command_param_refs(fp, "CommonCommands", fp.stem)
                _emit_from_xml(fp, category, fp.stem)

        # ExchangePlans content
        ep_dir = Path(base_path) / "ExchangePlans"
        if ep_dir.is_dir() and (kinds_set is None or "exchange_plan_content" in kinds_set):
            for plan_dir in ep_dir.iterdir():
                if not plan_dir.is_dir():
                    continue
                plan_name = plan_dir.name
                files = [plan_dir / "Ext" / "Content.xml", plan_dir / f"{plan_name}.mdo"]
                for fp in files:
                    if not fp.is_file():
                        continue
                    try:
                        text = fp.read_text(encoding="utf-8-sig", errors="replace")
                    except OSError:
                        continue
                    items = _pep(text)
                    if not items:
                        continue
                    rel = fp.relative_to(Path(base_path)).as_posix()
                    for item in items:
                        canon = _ctr(item.get("ref", ""))
                        if canon.lower() == canonical_lower:
                            results.append(
                                {
                                    "used_in": f"ExchangePlan.{plan_name}.Content",
                                    "path": rel,
                                    "line": None,
                                    "kind": "exchange_plan_content",
                                }
                            )

        # DefinedTypes
        dt_dir = Path(base_path) / "DefinedTypes"
        if dt_dir.is_dir() and (kinds_set is None or "defined_type_content" in kinds_set):
            for fp in dt_dir.iterdir():
                paths_to_try: list[Path] = []
                if fp.is_file() and fp.suffix.lower() == ".xml":
                    paths_to_try.append(fp)
                elif fp.is_dir():
                    mdo = fp / f"{fp.name}.mdo"
                    if mdo.is_file():
                        paths_to_try.append(mdo)
                for cfp in paths_to_try:
                    try:
                        text = cfp.read_text(encoding="utf-8-sig", errors="replace")
                    except OSError:
                        continue
                    parsed_dt = _pdt(text)
                    if not parsed_dt:
                        continue
                    rel = cfp.relative_to(Path(base_path)).as_posix()
                    for type_str in parsed_dt.get("types", []):
                        canon = _ctr(type_str)
                        if canon.lower() == canonical_lower:
                            results.append(
                                {
                                    "used_in": f"DefinedType.{parsed_dt['name']}.Type",
                                    "path": rel,
                                    "line": None,
                                    "kind": "defined_type_content",
                                }
                            )

        # ChartsOfCharacteristicTypes characteristic_types (Type list at top level)
        # Already covered via parse_metadata_xml path above (characteristic_type kind)
        # but parse_pvh_characteristics provides a clean list — reuse just for completeness.
        _ = _ppc  # parse_pvh_characteristics covered indirectly via parse_metadata_xml
        return results

    def find_defined_types(name: str) -> dict:
        """Resolve a DefinedType by name to its concrete type list.

        Args:
            name: e.g. 'Сумма' or 'ОпределяемыйТип.Сумма' or 'DefinedType.Сумма'.

        Returns:
            {name, types: list[str], path: str, partial: bool}.
            On v11 indexes (no defined_types table) does live XML scan.
        """
        text = name.strip()
        # strip prefix
        for prefix in ("ОпределяемыйТип.", "DefinedType."):
            if text.startswith(prefix):
                text = text[len(prefix) :]
                break
        result: dict = {"name": text, "types": [], "path": "", "partial": False}

        if idx_reader is not None:
            try:
                row = idx_reader.find_defined_type(text)
            except Exception:
                row = None
            if row is not None:
                return {"name": row["name"], "types": row["types"], "path": row["path"], "partial": False}

        # Live fallback
        from rlm_tools_bsl.bsl_xml_parsers import (
            canonicalize_type_ref as _ctr,
            parse_defined_type as _pdt,
        )

        result["partial"] = True
        dt_dir = Path(base_path) / "DefinedTypes"
        if not dt_dir.is_dir():
            return result
        text_lower = text.lower()
        for fp in dt_dir.iterdir():
            paths: list[Path] = []
            if fp.is_file() and fp.suffix.lower() == ".xml":
                paths.append(fp)
            elif fp.is_dir():
                mdo = fp / f"{fp.name}.mdo"
                if mdo.is_file():
                    paths.append(mdo)
            for cfp in paths:
                try:
                    content = cfp.read_text(encoding="utf-8-sig", errors="replace")
                except OSError:
                    continue
                parsed = _pdt(content)
                if not parsed or parsed["name"].lower() != text_lower:
                    continue
                from rlm_tools_bsl.bsl_xml_parsers import _XS_TYPE_MAP, _strip_ns_prefix

                canonical_types: list[str] = []
                for type_str in parsed.get("types", []):
                    canon = _ctr(type_str)
                    if canon:
                        canonical_types.append(canon)
                        continue
                    stripped = type_str.strip()
                    mapped = _XS_TYPE_MAP.get(stripped) or _XS_TYPE_MAP.get(f"xs:{stripped}")
                    canonical_types.append(mapped or _strip_ns_prefix(stripped))
                rel = cfp.relative_to(Path(base_path)).as_posix()
                result.update({"name": parsed["name"], "types": canonical_types, "path": rel})
                return result
        return result

    # ── Register all helpers ─────────────────────────────────────
    # Each _reg() call: name, function, signature (for strategy table),
    # category (for grouping), keywords (for help search), recipe (code example).
    # Adding a new helper = define function above + add _reg() here.

    _reg(
        "find_module",
        find_module,
        "find_module(name='', module_type='', category='') -> [{path, category, object_name, module_type}]  # name — опц. фрагмент имени (пусто = любой модуль); опц. фильтры module_type (напр. 'ObjectModule'/'ManagerModule') и category (напр. 'Documents'), в т.ч. без name; cap 50",
        "discovery",
    )
    _reg(
        "find_by_type",
        find_by_type,
        "find_by_type(category, name='') -> same. Categories: Documents, Catalogs, CommonModules, InformationRegisters, AccumulationRegisters, Reports, DataProcessors",
        "discovery",
    )

    _reg(
        "extract_procedures",
        extract_procedures,
        "extract_procedures(path|object_name) -> [{name, type, line, end_line, is_export, params(list)}]  "
        "# path ИЛИ имя объекта: имя → авто-выбор модуля по (category, module_type); неоднозначно → ValueError "
        "(для прозрачного разрешения по имени с _meta — get_module_outline)",
        "code",
    )
    _reg(
        "find_exports",
        find_exports,
        "find_exports(path) -> [{name, line, is_export, type, params(list)}]",
        "code",
        ["export", "экспорт", "find_exports", "процедур", "функци"],
        "FIND EXPORTS:\n"
        "  modules = find_module('Name')  # replace 'Name'\n"
        "  if not modules:\n"
        "      print('Не найдено')\n"
        "  else:\n"
        "      path = modules[0]['path']\n"
        "      exports = find_exports(path)\n"
        "      for e in exports:\n"
        "          print(e['name'], 'line:', e['line'], 'export:', e['is_export'])",
    )
    _reg(
        "read_procedure",
        read_procedure,
        "read_procedure(path, proc_name(str|list), include_overrides=False) -> str | None  "
        "# list имен → {proc_name: str|None|{error}} (модуль парсится один раз; {error} на упавшем элементе); numbered in MCP session",
        "code",
        ["read", "чтени", "читать", "содержим", "content", "тело", "body"],
        "READ PROCEDURE BODY:\n"
        "  modules = find_module('Name')\n"
        "  if not modules:\n"
        "      print('Не найдено')\n"
        "  else:\n"
        "      path = modules[0]['path']\n"
        "      body = read_procedure(path, 'ProcedureName')  # numbered in MCP session\n"
        "      if body is None:\n"
        "          # имя неточное или у объекта только XML-метаданные (КОДСобытия и т.п.)\n"
        "          procs = extract_procedures(path)\n"
        "          for p in procs:\n"
        "              print(p['name'], 'export=', p['is_export'])\n"
        "      else:\n"
        "          print(body)\n"
        "  # Если расширения перехватили метод — читать с перехватами:\n"
        "  full = read_procedure(path, 'ProcName', include_overrides=True)\n"
        "  # full = оригинал + '=== Перехвачен &Аннотация в расширении X ===' + тело перехвата\n"
        "  # BATCH: несколько методов одного модуля одним вызовом (модуль парсится 1 раз) → dict по имени:\n"
        "  bodies = read_procedure(path, ['ОбработкаПроведения', 'ПриЗаписи'])  # {name: str|None|{error}}\n"
        "  for name, b in bodies.items():\n"
        "      if isinstance(b, dict) and 'error' in b: continue  # упавший элемент изолирован\n"
        "      print(name, 'найден' if b else 'нет тела')",
    )
    _reg(
        "find_callers_context",
        find_callers_context,
        "find_callers_context(proc(str|list), module_hint, 0, 50) -> {callers: [{file, caller_name, line, ...}], _meta: {total_callers, returned, offset, has_more, exact_available, target_exact, exact_rows, fallback_rows}}  # list имён → {proc: {callers,_meta}|{error}} (общий module_hint/offset/limit на все имена; {error} на упавшем элементе); exact_rows/fallback_rows: точные (по callee_key) vs эвристические (по имени) рёбра",
        "code",
        ["caller", "call graph", "граф", "вызов", "вызыва", "кто вызывает", "find_callers"],
        "BUILD CALL GRAPH:\n"
        "  # With index: instant across the whole codebase, hint is optional\n"
        "  # Without index: parallel file scan, hint narrows scope\n"
        "  modules = find_module('Name')\n"
        "  if not modules:\n"
        "      print('Не найдено')\n"
        "  else:\n"
        "      path = modules[0]['path']\n"
        "      exports = find_exports(path)\n"
        "      for e in exports:\n"
        "          data = find_callers_context(e['name'], '', 0, 50)\n"
        "          for c in data['callers']:\n"
        "              print(e['name'], '<-', c['caller_name'], c['file'], 'line:', c['line'])\n"
        "          if data['_meta']['has_more']:\n"
        "              print('  ... more callers, increase offset')\n"
        "  # BATCH: вместо цикла по экспортам — один вызов со списком имён → {name: {callers,_meta}|{error}}:\n"
        "  by_name = find_callers_context([e['name'] for e in exports], '', 0, 50)\n"
        "  for name, data in by_name.items():\n"
        "      if 'error' in data: continue  # упавший элемент изолирован, батч цел\n"
        "      print(name, '<-', len(data['callers']), 'callers')",
    )
    _reg(
        "find_call_hierarchy",
        find_call_hierarchy,
        "find_call_hierarchy(name, direction='callers', depth=2, module_hint='', include_triggers=False) -> "
        "{root, direction, depth, tree:[{name, target_hint, target_key, "
        "meta:{exact_rows, fallback_rows, exact_available, target_exact}, "
        "callers:[{caller_name, module_path, category, object_name, line, is_export, level}], "
        "triggers:[{edge_type, source_name, source_kind, detail, file, line, caller_name, object_name, category, target_key, resolved}]}], "
        "visited:int, truncated_targets:[{name, level, total, returned}], "
        "_meta:{exact_available, root_exact, exact_targets, fallback_targets, exact_rows, fallback_rows, "
        "node_budget_exceeded, visited_cap}} "
        "| {error, hint, supported_directions}  # triggers: ключ есть ТОЛЬКО при include_triggers=True (не-call рёбра: подписки/события форм/рег.задания/CFE-перехваты)",
        "code",
        [
            "иерархия вызовов",
            "call hierarchy",
            "граф вызовов",
            "цепочка вызовов",
            "depth",
            "глубина",
            "транзитивный",
        ],
        "BUILD CALL HIERARCHY (multi-level callers tree):\n"
        "  # ВНИМАНИЕ — ОБРАБОТЧИКИ СОБЫТИЙ МОДУЛЯ ОБЪЕКТА (ОбработкаПроведения, ПередЗаписью,\n"
        "  #   ПриЗаписи, ОбработкаЗаполнения, ПриКопировании...): вызов от ПЛАТФОРМЫ в граф\n"
        "  #   ВЫЗОВОВ не попадает, поэтому callers=0 — ЭТО НОРМА, а НЕ мертвый код.\n"
        "  #   По имени хелпер их НЕ исключает: если BSL-код где-то ЯВНО зовет обработчик,\n"
        "  #     такое ребро в индексе ЕСТЬ и оно придет обычным caller'ом — не игнорируй.\n"
        "  #   Но ЧЕМ обработчик пишет движения, так не узнать: трассируй ДЕЛЕГАТА из его тела:\n"
        "  #     read_procedure(path, 'ОбработкаПроведения') -> имя делегата -> хелпер НА ДЕЛЕГАТЕ.\n"
        "  #   direction='callees' («куда уходит метод») НЕ поддержан — только читать тело.\n"
        "  #   include_triggers ребра «его зовет платформа» НЕ добавит (такого edge_type нет), но\n"
        "  #     ПОКАЖЕТ CFE-перехват самого обработчика (&Перед/&После/&Вместо) — это полезно.\n"
        "  # depth=1..3 (по умолчанию 2). Только direction='callers'.\n"
        "  res = find_call_hierarchy('ОтразитьВУчете', direction='callers', depth=2)  # метод, который РЕАЛЬНО зовут из кода\n"
        "  if 'error' in res:\n"
        "      print(res['hint'])  # callees/both пока не поддержаны\n"
        "  else:\n"
        "      for node in res['tree']:\n"
        "          for c in node['callers']:\n"
        "              print(f\"  L{c['level']} {c['caller_name']} <- {c['object_name']} ({c['module_path']}:{c['line']})\")\n"
        "      for t in res['truncated_targets']:  # callers>200 на узле — дерево неполное\n"
        "          print(f\"  TRUNCATED: {t['name']} (L{t['level']}): {t['returned']}/{t['total']}\")\n"
        "          # полный список callers метода — find_callers_context(t['name'], '', offset=200, limit=200)\n"
        "  # ТОЧНОСТЬ (exact-режим): для ОДНОИМЕННЫХ методов (один и тот же метод в сотнях объектов)\n"
        "  #   передай module_hint — привяжет КОРЕНЬ к одному модулю и уберет ложные звенья от\n"
        "  #   однофамильцев. NB: у платформенных обработчиков (см. выше) hint не добавит\n"
        "  #   отсутствующий ПЛАТФОРМЕННЫЙ вход; для ЯВНЫХ BSL-вызовов их обычные рёбра остаются.\n"
        "  #   Hint нужен для одноименных методов, которые РЕАЛЬНО зовут из кода:\n"
        "  res = find_call_hierarchy('ЗаполнитьДокумент', module_hint='Документ.РеализацияТоваровУслуг', depth=2)\n"
        "  #   формы hint: rel_path | 'Документ.X'/'Document.X' | голый object_name.\n"
        "  #   Экспортному методу общего модуля hint НЕ нужен, ЕСЛИ его имя уникально во всей БД\n"
        "  #   (exact включится сам); если root_exact=False — имя неуникально, передай module_hint.\n"
        "  #   Глубже 1-го уровня обход идёт по rel_path найденного caller'а → exact автоматически.\n"
        "  # ДОВЕРИЕ к рёбрам — читай _meta:\n"
        "  #   _meta.exact_available — поддерживает ли схема индекса точный режим (callee_key);\n"
        "  #   _meta.root_exact      — включился ли exact на корне (иначе корень по имени, возможны однофамильцы);\n"
        "  #   _meta.exact_rows/fallback_rows — сколько рёбер точные vs эвристические (по имени);\n"
        "  #   node['meta'].target_exact — точен ли конкретный узел; node['target_key'] = rel_path::метод.\n"
        "  # Одноимённые методы без hint возвращают список носителей — выбирай по object_name/category.\n"
        "  #   _meta.node_budget_exceeded=True — широкий корень упёрся в visited_cap, дерево частичное\n"
        "  #     (по уровням): передай module_hint, чтобы и сузить, и ускорить обход.\n"
        "  # Для глубины 1 эффективнее обычный find_callers_context().\n"
        "  # ТРИГГЕРЫ (include_triggers=True): метод вызывается не только из кода. Подмешивает на\n"
        "  #   КАЖДЫЙ узел node['triggers'] — не-call ребра (подписки/события форм/рег.задания/CFE).\n"
        "  #   Для ОбработкаПроведения из ТРИГГЕРОВ придет разве что CFE-перехват обработчика\n"
        "  #   расширением: ребра «его зовет платформа» не существует. (Явные BSL-вызовы, если\n"
        "  #   они есть, приходят обычными callers — триггеры к ним отношения не имеют.)\n"
        "  res = find_call_hierarchy('ОбработкаПроведения', module_hint='Документ.X', include_triggers=True)\n"
        "  for node in res['tree']:\n"
        "      for t in node.get('triggers', []):\n"
        "          print(f\"  TRIGGER {t['edge_type']}: {t['source_name']} ({t['detail']}) resolved={t['resolved']}\")\n"
        "  #   resolved=True — привязан по стабильному target_key; False — совпал по имени (recall).",
    )
    _reg(
        "find_path",
        find_path,
        "find_path(from_name, to_name, max_depth=4, from_hint='', to_hint='', include_triggers=False) -> "
        "{found, from, to, path:[{name, module_path, call_line, triggers?}]|None, depth, "
        "_meta:{max_depth, nodes_expanded, visited_cap, budget_exceeded, from_key, to_exact, to_key, "
        "precision:'exact'|'heuristic', direction:'callers-reverse'}} | "
        "{found:False, error, hint, candidates:[{object_name, category, module_type, file, line}], _meta:{ambiguous, ambiguous_arg}}  "
        "# ДОСТИЖИМОСТЬ по графу ВЫЗОВОВ (from → … → to). call_line = строка РЕБРА к следующему узлу (НЕ определения); у терминального (to) None. "
        "Многозначное имя без своего hint → ранний {error, hint, candidates} (проверяй 'error' in res ПЕРЕД found/budget_exceeded; добавь to_hint/from_hint из candidates)",
        "code",
        [
            "путь вызовов",
            "find_path",
            "достижимость",
            "reachability",
            "доходит ли",
            "вызывает ли",
            "путь между методами",
        ],
        "FIND PATH (достижим ли to_name из from_name по графу ВЫЗОВОВ):\n"
        "  res = find_path('НизкоуровневыйМетод', 'ОбработчикUI')\n"
        "  if 'error' in res:  # многозначное имя без hint — проверь ПЕРЕД found/budget_exceeded\n"
        "      # res['candidates'] = [{object_name, category, module_type, file, line}] — для МНОГОЗНАЧНОГО конца\n"
        "      f = res['candidates'][0]['file']  # file — самый надёжный hint\n"
        "      # пинь ИМЕННО многозначный конец: ambiguous_arg говорит, to это или from\n"
        "      kw = {'to_hint': f} if res['_meta']['ambiguous_arg'] == 'to' else {'from_hint': f}\n"
        "      res = find_path('НизкоуровневыйМетод', 'ОбработчикUI', **kw)  # (если многозначны ОБА конца — повтори ещё раз)\n"
        "  if res['found']:\n"
        "      for el in res['path']:  # forward: [from → … → to]\n"
        "          print(f\"  {el['name']} ({el['module_path']}) call_line={el['call_line']}\")\n"
        "      # call_line — строка ВЫЗОВА к СЛЕДУЮЩЕМУ узлу (ребро), НЕ определения; у to call_line=None\n"
        "  else:\n"
        "      print('путь не найден' if not res['_meta']['budget_exceeded'] else 'обход обрезан — сузь hint/уменьши max_depth')\n"
        "  # ТОЧНОСТЬ: _meta.precision='exact' ⇔ to разрешён точно И все рёбра пути по callee_key;\n"
        "  #   'heuristic' (старый индекс/FS/имя) → found=True = достижимость ПО ИМЕНИ, не доказанный путь.\n"
        "  # Одноимённые методы: from_hint/to_hint (rel_path | 'Документ.X' | object_name) пинят к модулю.\n"
        "  # _meta.budget_exceeded=True → обход обрезан (visited_cap ИЛИ у узла >одной страницы callers),\n"
        "  #   found=False НЕ доказывает отсутствие; только found=False+budget_exceeded=False И без 'error' — точно «не достижим».",
    )
    _reg(
        "find_definition",
        find_definition,
        "find_definition(name, module_hint='', limit=50) -> {name, definitions:[{file, line, end_line, type, "
        "is_export, params, category, object_name, module_type}], total, truncated, "
        "_meta:{index_used, unique, hint_applied, slow_fallback}}  "
        "# ГДЕ ОПРЕДЕЛЁН метод (форвард-комплемент find_callers_context). Одноимённые в N объектах — норма 1С: "
        "вернёт всех кандидатов, сужай module_hint",
        "code",
        [
            "definition",
            "определение",
            "где определён",
            "где определена",
            "где объявлен метод",
            "go to definition",
            "find_definition",
            "где находится метод",
            "перейти к определению",
        ],
        "FIND DEFINITION (где определён метод — форвард-комплемент find_callers_context):\n"
        "  d = find_definition('ПересчитатьИтоги')\n"
        "  for x in d['definitions']:\n"
        "      print(x['file'], x['line'], x['type'], 'export' if x['is_export'] else '')\n"
        "  # Одноимённые методы (ОбработкаПроведения есть в каждом документе — 600+ кандидатов):\n"
        "  #   сузь module_hint (rel_path | 'Документ.X' | имя объекта) → _meta.unique=True:\n"
        "  d = find_definition('ОбработкаПроведения', 'Документ.РеализацияТоваровУслуг')\n"
        "  # дальше: read_procedure(d['definitions'][0]['file'], 'ОбработкаПроведения')  # тело\n"
        "  #   NB: у платформенного обработчика вызов от ПЛАТФОРМЫ в граф не попадает, поэтому ПУСТЫЕ\n"
        "  #   обратные ссылки — это НОРМА, а не мертвый код (ЯВНЫЙ вызов из BSL, если он есть, найдется).\n"
        "  #   Но ЧЕМ он пишет движения, так не узнать: читай тело и трассируй ДЕЛЕГАТА (rlm_help('проведение')).\n"
        "  # _meta.hint_applied — фильтр по hint применён к запросу (НЕ «hint изменил счёт»);\n"
        "  #   total/truncated — потолок limit; _meta.slow_fallback=True — был кириллический py_lower-rescan\n"
        "  #   (имя передано в нижнем регистре). Пустой результат → definitions:[], total:0 (не ошибка).",
    )
    _reg(
        "get_module_outline",
        get_module_outline,
        "get_module_outline(path|object_name, include_methods=True, no_live=False) -> {path, category, object_name, "
        "module_type, totals:{methods, exports, regions, loc}, outline:[{region, line, end_line, totals:{methods, "
        "exports}, children:[...], methods:[...]}], orphan_methods, _meta:{index_used, fallback_reason, "
        "skipped_live?, resolved_from_name, chosen_module?, candidates?, ambiguous?}}  "
        "# ДЕШЁВЫЙ СКЕЛЕТ модуля (дерево #Область + агрегаты) — первый хоп перед чтением тел; "
        "path ИЛИ имя объекта (имя → прозрачный авто-выбор модуля, resolver-ключи в _meta, ambiguous=True при тай-брейке); "
        "no_live=True → на stale/no-index НЕ читает файл (skipped-маркер _meta.skipped_live)",
        "code",
        [
            "оглавление",
            "структура модуля",
            "области",
            "outline",
            "карта модуля",
            "#Область",
            "skeleton",
            "get_module_outline",
            "скелет модуля",
        ],
        "MODULE OUTLINE (дешёвая структурная карта ДО чтения тел):\n"
        "  mods = find_module('Расчёты')\n"
        "  if mods:\n"
        "      o = get_module_outline(mods[0]['path'], include_methods=False)  # только области + агрегаты\n"
        "      for r in o['outline']:\n"
        "          print(r['region'], r['totals'])  # {'methods': N, 'exports': M}\n"
        "      # затем нырнуть в нужную область с include_methods=True или read_procedure(path, name)\n"
        "  # totals модуля: {methods, exports, regions, loc}; orphan_methods — код вне любой #Область.\n"
        "  # _meta.index_used=False + fallback_reason — индекс недоступен/устарел (отработал live-парсинг).",
    )
    _reg(
        "find_callers",
        find_callers,
        "find_callers(proc, module_hint='', max_files=20) -> [{file, line, text}]  # COMPACT FIRST PAGE: thin wrapper над find_callers_context, default limit=20, без _meta/has_more — quick view; для полного аудита callers — find_callers_context",
        "code",
        ["compact callers", "плоский список вызовов", "только пути вызовов"],
        "COMPACT FIRST PAGE OF CALLERS (для quick view: 3 поля вместо 7, без пагинации):\n"
        "  hits = find_callers(proc, hint, max_files=20)\n"
        "  for h in hits:\n"
        "      print(h['file'], 'line:', h['line'], h['text'])\n"
        "  # Когда брать find_callers vs find_callers_context:\n"
        "  #   find_callers          → quick view, первые max_files (default 20). Без has_more —\n"
        "  #                           если callers > max_files, остаток молча отбрасывается.\n"
        "  #   find_callers_context  → полный API: caller_name, object_name, category, is_export\n"
        "  #                           + _meta с total_callers/has_more и пагинация (offset/limit).\n"
        "  # Под капотом find_callers вызывает find_callers_context — поиск тот же, но контракт\n"
        "  # урезан. Для аудита/полного списка — всегда find_callers_context.",
    )
    _reg(
        "safe_grep",
        safe_grep,
        "safe_grep(pattern, name_hint='', max_files=20) -> [{file, line, text}]",
        "code",
        ["search", "grep", "поиск", "искать", "найти", "pattern", "шаблон"],
        "SEARCH FOR CODE:\n"
        "  results = safe_grep('SearchPattern', 'ModuleHint', max_files=20)\n"
        "  for r in results:\n"
        "      print(r['file'], 'line:', r['line'], r['text'])\n"
        "  # Or find modules by name:\n"
        "  modules = find_module('PartOfName')\n"
        "  if not modules:\n"
        "      print('Не найдено')\n"
        "  else:\n"
        "      for m in modules:\n"
        "          print(m['path'], m['category'], m['object_name'])",
    )

    _reg(
        "parse_object_xml",
        parse_object_xml,
        "parse_object_xml(path) -> {name, synonym, attributes, tabular_sections, dimensions, resources, ...}",
        "xml",
        [
            "metadata",
            "метаданн",
            "реквизит",
            "attribute",
            "dimension",
            "измерен",
            "ресурс",
            "resource",
            "табличн",
            "tabular",
            "xml",
            "parse_object",
        ],
        "READ METADATA:\n"
        "  # Accepts directory or XML path — auto-resolves:\n"
        "  meta = parse_object_xml('Documents/РеализацияТоваровУслуг')  # directory\n"
        "  meta = parse_object_xml('Documents/Name/Ext/Document.xml')   # direct XML\n"
        "  # Также принимает 'фейковый' .mdo-путь — авто-нормализует base:\n"
        "  meta = parse_object_xml('Documents/X.mdo')   # => Documents/X/X.mdo (EDT) или Ext/Document.xml (CF)\n"
        "  # Если ничего не найдено — FileNotFoundError с явной подсказкой про директорию.\n"
        "  for key in meta:\n"
        "      print(key, ':', meta[key])",
    )
    _reg(
        "parse_form",
        parse_form,
        "parse_form(object_name, form_name='', handler='') -> [{form_name, module_path, handlers, commands, attributes:[{name, types, main, main_table, query_text}]}]  # у атрибута формы ключ types — СТРОКА 'Тип1, Тип2' (не list: для списка используй types.split(', ') if types else []), НЕ attr_type (это поле find_attributes)",
        "xml",
        kw=["parse_form", "события формы", "обработчики формы", "элементы формы", "form handler", "form event"],
        recipe=(
            "# Обработчики и команды формы объекта:\n"
            "forms = parse_form('БанковскиеСчетаОрганизаций')\n"
            "for f in forms:\n"
            '    print(f\'{f["form_name"]}: {len(f["handlers"])} handlers, {len(f["commands"])} commands\')\n'
            "    for h in f['handlers']:\n"
            '        print(f\'  {h["element"] or "[form]"}.{h["event"]} → {h["handler"]}\')\n\n'
            "# Обратный поиск: к чему привязана процедура?\n"
            "forms = parse_form('БанковскиеСчетаОрганизаций', handler='ПриСозданииНаСервере')\n\n"
            "# module_path для быстрого перехода к коду:\n"
            "for f in forms:\n"
            "    if f['module_path']:\n"
            "        procs = extract_procedures(f['module_path'])\n"
            "        print(f'{f[\"form_name\"]}: {len(procs)} procedures')\n"
        ),
    )
    _reg(
        "find_enum_values",
        find_enum_values,
        "find_enum_values(enum_name(str|list)) -> {name, synonym, values: [{name, synonym}]} | {error}  "
        "# list имён → {enum_name: {...}|{error}} (изоляция ошибок поэлементно)",
        "xml",
        ["перечислен", "enum", "значени перечислени"],
        "FIND ENUM VALUES:\n"
        "  result = find_enum_values('СтатусыЗаказовКлиентов')\n"
        "  print(f\"{result['name']} ({result['synonym']})\")\n"
        "  for v in result['values']:\n"
        "      print(f\"  {v['name']}: {v['synonym']}\")\n"
        "  # BATCH: несколько перечислений одним вызовом → {enum_name: {...}|{error}}:\n"
        "  many = find_enum_values(['СтатусыЗаказов', 'ВидыОпераций'])\n"
        "  for name, r in many.items():\n"
        "      print(name, len(r.get('values', [])) if 'error' not in r else r['error'])",
    )
    _reg(
        "find_attributes",
        find_attributes,
        "find_attributes(name='', object_name='', category='', kind='', limit=500) -> [{object_name, category, attr_name, attr_synonym, attr_type, attr_kind, ts_name}]",
        "xml",
        [
            "реквизит",
            "attribute",
            "тип",
            "type",
            "измерение",
            "dimension",
            "ресурс",
            "resource",
            "колонка",
            "табличная часть",
        ],
        "FIND ATTRIBUTE TYPES:\n"
        "  # By attribute name:\n"
        "  results = find_attributes('Организация')\n"
        "  for r in results:\n"
        "      print(r['object_name'], r['attr_name'], r['attr_type'])\n"
        "  # All attributes of a document:\n"
        "  attrs = find_attributes(object_name='РеализацияТоваровУслуг')\n"
        "  # Only dimensions of a register:\n"
        "  dims = find_attributes(object_name='ТоварыОрганизаций', kind='dimension')\n"
        "  # БЕЗ ИНДЕКСА: find_attributes(name='X') без object_name вернёт [] — невозможно сканировать всю кодовую базу.\n"
        "  # Решение: всегда передавай object_name на проектах без индекса.",
    )
    _reg(
        "find_predefined",
        find_predefined,
        "find_predefined(name='', object_name='', limit=500) -> [{object_name, category, item_name, item_synonym, types, item_code}]",
        "xml",
        ["предопределённ", "predefined", "субконто", "subconto", "счёт", "account", "предопределенн"],
        "FIND PREDEFINED ITEMS:\n"
        "  # By name (subconto type question):\n"
        "  items = find_predefined('РеализуемыеАктивы')\n"
        "  for i in items:\n"
        "      print(i['item_name'], i['types'])\n"
        "  # All predefined of an object:\n"
        "  all_sub = find_predefined(object_name='ВидыСубконтоХозрасчетные')\n"
        "  # Predefined of a catalog:\n"
        "  countries = find_predefined(object_name='СтраныМира')\n"
        "  # БЕЗ ИНДЕКСА: find_predefined(name='X') без object_name вернёт [] — невозможно сканировать всю кодовую базу.\n"
        "  # Решение: всегда передавай object_name на проектах без индекса.",
    )

    _reg(
        "get_object_profile",
        get_object_profile,
        "get_object_profile(name, sections=None, include_flow=False, include_code_usages=False, limit=20) -> "
        "{object_name, category, sections:{structure, modules, registers, subscriptions, roles, functional_options}, _meta}  "
        "# ОБЗОР ОБЪЕКТА ЗА 1 ВЫЗОВ: compact roll-up index-секций вместо ~10 хелперов; секция = "
        "{status: ok|empty|unavailable|skipped|error, summary, items:top-N, _meta:{source}}, БЕЗ тел; "
        "тяжёлое (поток/code-scan) — только include_flow=True / include_code_usages=True",
        "composite",
        [
            "обзор объекта",
            "профиль объекта",
            "profile",
            "профиль",
            "обзор",
            "overview",
            "object profile",
            "get_object_profile",
        ],
        "OBJECT PROFILE — ОБЗОР ОБЪЕКТА ЗА 1 ВЫЗОВ (Step 0 полного анализа: вместо ~10 одиночных хелперов):\n"
        "  p = get_object_profile('РеализацияТоваровУслуг')  # compact: structure+modules+registers+subscriptions+roles+functional_options\n"
        "  print(p['object_name'], p['category'])\n"
        "  for name, sec in p['sections'].items():\n"
        "      print(f\"  {name}: {sec['status']} {sec.get('summary')}\")  # счётчики; items — top-N preview без тел\n"
        "  # точечно глубже: read_procedure(path, 'Метод') по p['sections']['modules']['items'][i]['path']\n"
        "  # ровно нужное: get_object_profile(name, sections=['structure','roles'])\n"
        "  # тяжёлое ТОЛЬКО по флагу: get_object_profile(name, include_flow=True) → +секция flow (analyze_document_flow)\n"
        "  # ДИЗАМБИГУАЦИЯ: весь обзор за 1 вызов → get_object_profile; только код-скелет → get_object_modules;\n"
        "  #   только метаданные → get_object_full_structure; глубокий разбор тел/потока → analyze_document_flow / analyze_object",
    )
    _reg(
        "analyze_object",
        analyze_object,
        "analyze_object(name) -> {name, category, metadata (XML), modules:[{module_type, procedures, exports, ...}]}  "
        "# ДОРОГО: читает XML + ВСЕ тела всех модулей (extract_procedures). Для обзора бери get_object_profile; "
        "сюда — только когда реально нужны ВСЕ процедуры объекта сразу",
        "composite",
        ["analyze_object", "все тела объекта", "все процедуры объекта"],
        "DEEP OBJECT DUMP (ДОРОГО — XML + все тела; для обзора используй get_object_profile):\n"
        "  result = analyze_object('АвансовыйОтчет')  # бери ТОЛЬКО когда нужны ВСЕ процедуры объекта сразу\n"
        "  meta = result.get('metadata', {})\n"
        "  print(f\"Объект: {result['name']} ({meta.get('synonym', '')})\")\n"
        "  for m in result.get('modules', []):\n"
        "      print(f\"  {m['module_type']}: {m['procedures_count']} проц, {m['exports_count']} эксп\")\n"
        "  # Обзор за 1 дешёвый вызов → get_object_profile(name); код-скелет → get_object_modules(name).",
    )
    _reg(
        "get_object_full_structure",
        get_object_full_structure,
        "get_object_full_structure(name) -> {object_name, category, synonym, posting, attributes, "
        "tabular_sections:[{name, synonym, columns}], dimensions, resources, predefined_items, "
        "enum_values_for_typed_refs:{Enum.X:[{name,synonym}]}, forms:[str], "
        "_meta:{index_used: bool — True когда возвращённые структурные секции взяты из индекса "
        "(контракт об ИСТОЧНИКЕ, не о ПОЛНОТЕ — для проверки полноты на stale-индексе вызывай parse_object_xml); "
        "fallback_reason: 'index_unavailable_or_table_missing' | 'index_empty_for_object' | "
        "'category_without_attributes_filled_via_live_xml' | 'index_partially_enriched_from_live_xml' | "
        "'parse_failed: ...' | None; "
        "ts_synonyms_available: bool — True ТОЛЬКО если у хотя бы одной TS в результате непустой synonym}}",
        "composite",
        [
            "структура объекта",
            "полная структура",
            "карточка объекта",
            "object structure",
            "вся структура",
            "реквизиты документа",
            "реквизиты справочника",
            "табличные части",
            "колонки тч",
        ],
        "FULL OBJECT STRUCTURE (1 вызов вместо 3-5 — заменяет parse_object_xml + find_attributes + find_predefined + find_enum_values):\n"
        "  # ⚠️ КЛЮЧИ В РЕЗУЛЬТАТЕ ОТЛИЧАЮТСЯ от find_attributes!\n"
        "  #   find_attributes:           [{attr_name, attr_synonym, attr_type, attr_kind}]\n"
        "  #   get_object_full_structure: {attributes:[{name, synonym, type}], dimensions:[...], resources:[...], ...}\n"
        "  #   Итерация: for a in s['attributes']: a['name']  (a['attr_name'] тоже работает — алиас, v1.18.0)\n"
        "  s = get_object_full_structure('РеализацияТоваровУслуг')\n"
        "  print(f\"{s['object_name']} ({s.get('synonym')}) posting={s.get('posting')}\")\n"
        "  print(f\"Реквизитов: {len(s['attributes'])}, ТЧ: {len(s['tabular_sections'])}, форм: {len(s['forms'])}\")\n"
        "  for ts in s['tabular_sections']:\n"
        "      print(f\"  ТЧ {ts['name']}: {len(ts['columns'])} колонок\")\n"
        "  # Перечисления уже раскрыты:\n"
        "  for ref_type, values in s['enum_values_for_typed_refs'].items():\n"
        "      print(f\"  {ref_type}: {[v['name'] for v in values]}\")\n"
        "  # Для регистров — данные в dimensions/resources, attributes пустой:\n"
        "  reg = get_object_full_structure('ТоварыНаСкладах')  # AccumulationRegister\n"
        "  for d in reg.get('dimensions', []):\n"
        "      print(f\"  измерение {d['name']}: {d['type']}\")\n"
        "  for r in reg.get('resources', []):\n"
        "      print(f\"  ресурс {r['name']}: {r['type']}\")\n"
        "  # _meta.index_used=False означает live XML fallback (синонимы ТЧ доступны только в этом режиме)\n"
        "  if not s['_meta']['index_used']:\n"
        "      print('Fallback:', s['_meta']['fallback_reason'])",
    )
    _reg(
        "get_object_modules",
        get_object_modules,
        "get_object_modules(name, include_methods=False, no_live=False) -> {object_name, category, "
        "modules:[{path, module_type, form_name, totals:{methods,exports,regions,loc}, "
        "outline:[{region, line, end_line, totals, children, methods?}], "
        "overrides:{count, methods:[...]}, _meta:{index_used, fallback_reason, skipped_live}}], "
        "totals:{modules, methods, exports, overrides}, _meta:{index_used, modules_truncated, modules_skipped_live}} | {error, _meta}  "
        "# ДЕШЁВЫЙ КОД-СКЕЛЕТ объекта: все модули + дерево #Область + агрегаты + флаги перехватов в 1 вызов. "
        "НЕ читает тела (extract_procedures) на индексном пути и НЕ парсит XML — легче analyze_object; "
        "no_live=True → stale/no-index модули помечаются skipped_live БЕЗ live-чтения",
        "composite",
        [
            "модули объекта",
            "скелет объекта",
            "все модули",
            "object modules",
            "структура кода объекта",
            "get_object_modules",
            "области объекта",
        ],
        "OBJECT CODE SKELETON (все модули объекта + #Область + агрегаты, 1 вызов вместо find_module+N×get_module_outline):\n"
        "  om = get_object_modules('РеализацияТоваров')  # include_methods=False — только области + агрегаты\n"
        "  if 'error' in om:\n"
        "      print(om['error'])\n"
        "  else:\n"
        "      print(om['object_name'], om['category'], om['totals'])  # {modules, methods, exports, overrides}\n"
        "      for m in om['modules']:\n"
        "          flag = '' if m['_meta']['index_used'] else f\" (live: {m['_meta']['fallback_reason']})\"\n"
        "          print(f\"  {m['module_type']}: {m['totals']['methods']} методов, перехватов {m['overrides']['count']}{flag}\")\n"
        "          for r in m['outline']:\n"
        "              print(f\"    #Область {r['region']} {r['totals']}\")\n"
        "  # затем нырнуть: get_object_modules(name, include_methods=True) ИЛИ read_procedure(m['path'], 'Метод')\n"
        "  # ДИЗАМБИГУАЦИЯ: метаданные (реквизиты/ТЧ) → get_object_full_structure; код-скелет → get_object_modules;\n"
        "  #   тяжёлый разбор ВСЕХ тел + XML → analyze_object. Перехваты по имени метода — в m['overrides']['methods'].",
    )
    _reg(
        "analyze_document_flow",
        analyze_document_flow,
        "analyze_document_flow(doc_name) -> {document, metadata, event_subscriptions, register_movements, related_scheduled_jobs, based_on, print_forms}  # dict (+ is_postable/hint для непроводимых); register_movements — сам dict (см. find_register_movements), event_subscriptions/related_scheduled_jobs — списки",
        "composite",
        ["lifecycle", "жизненн", "flow", "end-to-end", "полный анализ", "как работает"],
        "FULL DOCUMENT LIFECYCLE:\n"
        "  flow = analyze_document_flow('АвансовыйОтчет')\n"
        "  print('Подписки:', len(flow['event_subscriptions']))\n"
        "  for s in flow['event_subscriptions']:\n"
        "      print(f\"  {s['event']}: {s['handler']}\")\n"
        "  regs = flow['register_movements'].get('code_registers', [])\n"
        "  print('Регистры:', len(regs))\n"
        "  for r in regs:\n"
        "      print(f\"  Движения.{r['name']}\")",
    )
    _reg(
        "analyze_subsystem",
        analyze_subsystem,
        "analyze_subsystem(name) -> composition, custom vs standard objects",
        "composite",
        ["subsystem", "подсистем", "состав подсистем"],
        "ANALYZE SUBSYSTEM:\n"
        "  result = analyze_subsystem('Спецодежда')\n"
        "  for sub in result.get('subsystems', []):\n"
        "      print(f\"Подсистема: {sub['name']} ({sub['synonym']})\")\n"
        "      print(f\"Нетиповых: {len(sub['custom_objects'])}, типовых: {len(sub['standard_objects'])}\")\n"
        "      for obj in sub['custom_objects']:\n"
        "          print(f\"  [нетип] {obj['type']}.{obj['name']}\")\n"
        "      for obj in sub['standard_objects']:\n"
        "          print(f\"  [типов] {obj['type']}.{obj['name']}\")",
    )
    _reg(
        "find_custom_modifications",
        find_custom_modifications,
        "find_custom_modifications(obj, custom_prefixes=None) -> custom procedures, regions, attributes",
        "composite",
        ["custom", "нетипов", "доработк", "модификац", "modification"],
        "FIND CUSTOM MODIFICATIONS:\n"
        "  result = find_custom_modifications('ВнутреннееПотребление')\n"
        "  for mod in result.get('modifications', []):\n"
        "      print(f\"Модуль: {mod['path']}\")\n"
        "      for p in mod['custom_procedures']:\n"
        "          print(f\"  {p['type']} {p['name']} (стр.{p['line']})\")\n"
        "      for r in mod['custom_regions']:\n"
        "          print(f\"  #Область {r['name']} (стр.{r['line']})\")\n"
        "  for attr in result.get('custom_attributes', []):\n"
        "      print(f\"Реквизит: {attr['name']} ({attr.get('synonym', '')})\")",
    )

    _reg(
        "find_event_subscriptions",
        find_event_subscriptions,
        "find_event_subscriptions(obj, custom_only=False, event_filter=None, limit=None) -> list[dict]"
        " | {subscriptions, total, returned, has_more} (limit)"
        "  # при непустом obj строки несут scope=exact|partial|universal; полное имя — ТОЧНОЕ совпадение"
        " (омонимы-подстроки не протекают), фрагмент — подстрока; 'Документ.X' -> category-aware",
        "business",
        ["подписк", "subscription", "событи", "event", "BeforeWrite", "OnWrite", "ПриЗаписи", "ПередЗаписью"],
        "FIND EVENT SUBSCRIPTIONS:\n"
        "  # Default — весь список (контракт прежний):\n"
        "  subs = find_event_subscriptions('АвансовыйОтчет')\n"
        "  for s in subs: print(s['event'], s['handler'])\n"
        "  # С фильтром по событию (case-insensitive substring) — list[str] ИЛИ одна строка:\n"
        "  before_write = find_event_subscriptions('АвансовыйОтчет', event_filter=['BeforeWrite','ПередЗаписью'])\n"
        "  before_write_one = find_event_subscriptions('АвансовыйОтчет', event_filter='BeforeWrite')  # ок: одна строка\n"
        "  # С пагинацией (формат меняется на dict!):\n"
        "  page = find_event_subscriptions('', limit=50)\n"
        "  # page = {'subscriptions': [...], 'total': N, 'returned': K, 'has_more': bool}\n"
        "  if page['has_more']: ...  # увеличить limit или сузить event_filter",
    )
    _reg(
        "find_scheduled_jobs",
        find_scheduled_jobs,
        "find_scheduled_jobs(name='') -> [{name, method_name, use, ...}]",
        "business",
        ["регламент", "schedule", "job", "задани", "фонов", "background"],
        "FIND SCHEDULED JOBS:\n"
        "  # With index: instant. Without: parses XML on first call.\n"
        "  jobs = find_scheduled_jobs('Курс')\n"
        "  for j in jobs:\n"
        "      print(f\"{j['name']}: {j['method_name']} (active={j['use']})\")",
    )
    _reg(
        "find_register_movements",
        find_register_movements,
        "find_register_movements(doc_name, posting_calls_offset=0) -> {code_registers:[dict], suppressed_main_code_registers?:[dict],"
        " erp_mechanisms/manager_tables/adapted_registers:[str], is_postable?, posting_handler_present?,"
        " hint?, partial?, _meta?}"
        "  # code_registers — словари, остальные три — списки ИМЕН-строк;"
        " partial=True означает неполное чтение CFE, modules_scanned содержит только успешно прочитанные модули;"
        " сначала смотри is_postable; при пустом code_registers — posting_handler_present + hint",
        "business",
        ["движени", "movement", "регистр", "register", "проведен", "posting"],
        "TRACE DOCUMENT REGISTER MOVEMENTS:\n"
        "  result = find_register_movements('ПриобретениеТоваровУслуг')\n"
        "  suppressed = result.get('suppressed_main_code_registers', [])  # main-handler, отсеченный CFE-заменой\n"
        "  if result.get('is_postable') is not False:\n"
        "      for r in result['code_registers']:\n"
        "          detail = r.get('lines') or r.get('source', '')\n"
        "          print(f\"  Движения.{r['name']} ({detail})\")\n"
        "  # Если документ непроводимый — результат содержит is_postable=False + hint:\n"
        "  if result.get('is_postable') is False:\n"
        "      print('Непроводимый (Posting=Deny) — движений нет:', result['hint'])\n"
        "  if result.get('is_postable') is not False and result.get('posting_handler_present'):\n"
        "      # ОбработкаПроведения есть без прямых Движения.X: запись может быть делегирована или отсутствовать.\n"
        "      # ЧИТАЙ result['hint'] И ИДИ ПО НЕМУ: тело обработчика УЖЕ РАЗОБРАНО СЕРВЕРОМ, и в hint\n"
        "      #   лежат ФАКТЫ, а не догадки:\n"
        "      #   * наборы/менеджеры записи прямо в обработчике -> регистры НАЗВАНЫ поименно;\n"
        "      #   * делегат ИмяМодуля.Метод(...) -> получатель РАЗРЕШЕН: сервер отличил ОБЩИЙ МОДУЛЬ от\n"
        "      #     ПЕРЕМЕННОЙ/параметра и от РЕКВИЗИТА документа. ФАКТ «общий модуль» разрешен ТОЛЬКО\n"
        "      #     ПОЛНОМУ live-источнику реквизитов: хелпер сверяет по живому XML, включая метаданные\n"
        "      #     расширений всех диалектов (нечитаемый файл расширения = проверка НЕПОЛНАЯ = развилка);\n"
        "      #     профиль видит snapshot: при index_used=True И наличие, И отсутствие реквизита ничего\n"
        "      #     не доказывают о live XML. Не классифицируй по ним: точная live-проверка —\n"
        "      #     find_register_movements; иначе следуй tree-search маршруту из hint.\n"
        "      #     Неразрешенный получатель -> маршрут из hint: точный live safe_grep по всему BSL-каталогу\n"
        "      #     либо find_definition без module-hint; одноименный модуль иначе отдал бы ЧУЖОЕ тело.\n"
        "      #   * вызов без точки -> hint скажет, локальный он или из ГЛОБАЛЬНОГО общего модуля.\n"
        "      #   Шаги в hint — ИСПОЛНИМЫЙ Python с уже подставленными путем и именами: копируй как есть.\n"
        "      # НЕ ПОДТВЕРЖДАЙ делегата проверкой category == 'CommonModules': module_hint 'ОбщийМодуль.X'\n"
        "      #   уже фильтрует запрос по этой категории в SQL -> проверка ВСЕГДА истинна (тавтология).\n"
        "      # Движения через find_call_hierarchy на обработчике не ищи: вызов от ПЛАТФОРМЫ в граф\n"
        "      #   не попадает (callers=0 — норма, а не мертвый код; ЯВНЫЙ BSL-вызов он все же покажет).\n"
        "      print('Обработчик есть, прямых Движения.X нет — разбор тела в hint:')\n"
        "      print(result['hint'])\n"
        "\n"
        "FIND STATIC WRITER CANDIDATES:\n"
        "  result = find_register_writers('ТоварыНаСкладах')\n"
        "  # CFE/Posting проверь через find_register_movements(document); свежесть main-строки — по живому файлу\n"
        "  for w in result['writers']:\n"
        "      detail = w.get('lines') or w.get('source', '')\n"
        "      print(f\"  {w['document']} ({detail})\")",
    )
    _reg(
        "find_register_writers",
        find_register_writers,
        "find_register_writers(reg_name) -> {writers:[{document,source|lines,file}],runtime_filtered:false,hint}",
        "business",
        ["писатели регистра", "кто пишет", "register writer", "writer"],
        "FIND STATIC WRITER CANDIDATES:\n"
        "  result = find_register_writers('ТоварыНаСкладах')\n"
        "  # runtime_filtered=False: CFE/Posting проверь через forward; свежесть main-строки — по живому файлу\n"
        "  for w in result['writers']:\n"
        "      detail = w.get('lines') or w.get('source', '')\n"
        "      print(f\"  {w['document']} ({detail})\")",
    )
    _reg(
        "find_based_on_documents",
        find_based_on_documents,
        "find_based_on_documents(doc_name) -> {can_create_from_here, can_be_created_from}",
        "business",
        ["основани", "ввод на основании", "создать на основании", "based on", "filling", "заполнени"],
        "FIND BASED-ON DOCUMENTS (ввод на основании):\n"
        "  result = find_based_on_documents('ПриобретениеТоваровУслуг')\n"
        "  print('Можно создать из этого документа:')\n"
        "  for d in result['can_create_from_here']:\n"
        "      via = d.get('via', 'direct')  # 'direct' / 'back_scan' / 'metadata'\n"
        "      ref = d.get('ref') or d['document']  # metadata: canonical Catalog.X/Document.X\n"
        '      print(f"  -> {ref} ({via})")\n'
        "  print('Этот документ создается на основании:')\n"
        "  for d in result['can_be_created_from']:\n"
        "      print(f\"  <- {d['type']}\")\n"
        "  # Если у документа нет ДобавитьКомандыСозданияНаОсновании (типичный кейс — Письма в ДО3),\n"
        "  # хелпер автоматически делает back_scan по ОбработкаЗаполнения других Documents и находит\n"
        "  # документы, у которых наш doc_name упомянут как ДокументСсылка.<doc_name>.\n"
        "  # Записи back_scan помечены via='back_scan'; декларативные <BasedOn> из индекса\n"
        "  # (в т.ч. Catalog-основания, невидимые для FS-скана Documents/*) — via='metadata'\n"
        "  # (несут d['category'] и canonical d['ref']).",
    )
    _reg(
        "find_print_forms",
        find_print_forms,
        "find_print_forms(obj_name) -> {print_forms: [{name, presentation}]}",
        "business",
        ["печат", "print", "макет", "template", "накладн"],
        "FIND PRINT FORMS:\n"
        "  result = find_print_forms('РеализацияТоваровУслуг')\n"
        "  for p in result['print_forms']:\n"
        "      print(f\"  {p['name']}: {p['presentation']}\")",
    )
    _reg(
        "find_functional_options",
        find_functional_options,
        "find_functional_options(obj_name, include_code=True, limit=None) -> {xml_options, code_options}"
        " | {…, total, returned, has_more, partial?, _meta?}  # limit — per-bucket cap;"
        " empty obj сканирует 20 BSL-модулей и при большем каталоге ставит partial=True;"
        " вызывать limit= ИМЕНОВАННО (2-й позиционный — include_code)",
        "business",
        ["функциональн", "опци", "functional", "option", "включен", "выключен"],
        "FIND FUNCTIONAL OPTIONS:\n"
        "  # With index: XML options instant. Code grep still runs live.\n"
        "  result = find_functional_options('РеализацияТоваровУслуг')\n"
        "  for fo in result['xml_options']:\n"
        "      print(f\"  {fo['name']}: {fo['synonym']}\")\n"
        "  for co in result['code_options']:\n"
        "      print(f\"  В коде: {co['option_name']} (стр.{co['line']})\")\n"
        "  # Опций сотни? — пагинация per-bucket (xml и code режутся КАЖДЫЙ до N):\n"
        "  page = find_functional_options('РеализацияТоваровУслуг', limit=10)  # limit= ИМЕНОВАННО\n"
        "  # page: {..., total, returned, has_more}",
    )
    _reg(
        "find_roles",
        find_roles,
        "find_roles(obj_name) -> {roles: [{role_name, rights: [str], object, file}]}  # rights — список ИМЁН прав (str), не dict",
        "business",
        ["роль", "role", "прав", "right", "доступ", "access", "разрешен"],
        "FIND ROLES AND RIGHTS:\n"
        "  result = find_roles('ПриобретениеТоваровУслуг')\n"
        "  for r in result['roles']:\n"
        "      print(f\"  {r['role_name']}: {', '.join(r['rights'])}\")",
    )

    _reg(
        "extract_queries",
        extract_queries,
        "extract_queries(path) -> [{procedure, line, tables, text_preview}]",
        "code",
        ["запрос", "query", "таблиц", "table", "select", "выбрать"],
        "EXTRACT QUERIES FROM MODULE:\n"
        "  queries = extract_queries('path/to/ObjectModule.bsl')\n"
        "  for q in queries:\n"
        "      print(f\"  {q['procedure']} стр.{q['line']}: таблицы={q['tables']}\")\n"
        "      print(f\"    {q['text_preview'][:100]}\")",
    )
    _reg(
        "code_metrics",
        code_metrics,
        "code_metrics(path) -> {total_lines, code_lines, comment_lines, procedures_count, avg_proc_size, max_nesting}",
        "code",
        ["метрик", "metric", "размер", "size", "complex", "сложност", "статистик", "statistic"],
        "CODE METRICS:\n"
        "  m = code_metrics('path/to/Module.bsl')\n"
        "  print(f\"Строк: {m['total_lines']} (код: {m['code_lines']}, комментарии: {m['comment_lines']})\")\n"
        "  print(f\"Процедур: {m['procedures_count']}, экспортных: {m['exports_count']}\")\n"
        "  print(f\"Средний размер: {m['avg_proc_size']} строк, макс. вложенность: {m['max_nesting']}\")",
    )

    _reg(
        "search_methods",
        search_methods,
        "search_methods(query, limit=30) -> [{name, type, is_export, params(list), module_path, object_name, rank}]",
        "discovery",
        ["поиск метод", "search", "fts", "full-text", "найти метод", "подстрок"],
        "SEARCH METHODS BY NAME (FTS5, requires pre-built index with --no-fts NOT set):\n"
        "  # Find methods by substring across the entire codebase — instant\n"
        "  results = search_methods('ОбработкаЗаполнения')\n"
        "  for r in results:\n"
        "      print(f\"  {r['name']} ({r['type']}) export={r['is_export']} in {r['module_path']}\")\n"
        "  # Returns [] if index or FTS not available\n"
        "  # Combine with read_procedure() to read found methods:\n"
        "  #   body = read_procedure(r['module_path'], r['name'])",
    )
    _reg(
        "search_objects",
        search_objects,
        "search_objects(query) -> [{object_name, category, synonym, file}] — find by BUSINESS NAME",
        "discovery",
        ["synonym", "синоним", "бизнес", "search_objects", "объект", "business"],
        "SEARCH BY BUSINESS NAME (requires index v7+):\n"
        "  results = search_objects('себестоимость')\n"
        "  for r in results:\n"
        "      print(r['synonym'], r['category'], r['object_name'])",
    )
    _reg(
        "search_regions",
        search_regions,
        "search_regions(query, limit=200, count_only=False) -> [{name, line, end_line, module_path, object_name, category}] "
        "| {total, source, truncated, scope:'main_index'}",
        "discovery",
        ["область", "region", "search_regions", "#Область"],
        "FIND CODE REGIONS:\n"
        "  regions = search_regions('Себестоимость')\n"
        "  for r in regions:\n"
        "      print(r['category'], r['object_name'], r['name'], f'L{r[\"line\"]}-{r[\"end_line\"]}')\n"
        "  # CENSUS (молча усекается по limit без сигнала) — точное число без выдачи:\n"
        "  n = search_regions('Себестоимость', count_only=True)['total']  # index-side, scope=main_index",
    )
    _reg(
        "search_module_headers",
        search_module_headers,
        "search_module_headers(query, limit=200, count_only=False) -> [{module_path, object_name, category, header_comment}] "
        "| {total, source, truncated, scope:'main_index'}",
        "discovery",
        ["заголовок", "header", "комментарий", "search_module_headers"],
        "FIND MODULES BY HEADER COMMENT:\n"
        "  headers = search_module_headers('себестоимость')\n"
        "  for h in headers:\n"
        "      print(h['category'], h['object_name'], h['header_comment'][:80])\n"
        "  # CENSUS (молча усекается по limit) — точное число без выдачи:\n"
        "  n = search_module_headers('доработка', count_only=True)['total']  # index-side, scope=main_index",
    )
    _reg(
        "search",
        search,
        "search(query, scope='all', limit=30) -> [{text, source_type, object_name, path, path_kind, detail}]",
        "discovery",
        ["поиск", "search", "найти", "unified", "discovery", "искать"],
        "UNIFIED SEARCH across methods, synonyms, regions, headers:\n"
        "  # Broad first pass:\n"
        "  results = search('себестоимость')\n"
        "  for r in results:\n"
        "      print(r['source_type'], r['text'], r['path'])\n"
        "  # Filter by scope:\n"
        "  search('себестоимость', scope='methods')   # only code methods\n"
        "  search('себестоимость', scope='objects')    # only 1C objects by synonym\n"
        "  search('себестоимость', scope='regions')    # only #Область\n"
        "  search('себестоимость', scope='headers')    # only module headers\n"
        "  # Browse mode (empty query, specific scope, set limit for full list):\n"
        "  search('', scope='objects', limit=20000)  # browse objects (default limit=30)",
    )
    _reg(
        "get_index_info",
        get_index_info,
        "get_index_info() -> {status, builder_version, config_name, has_fts, has_synonyms, ...}",
        "discovery",
        ["index", "version", "индекс", "версия", "info", "get_index_info"],
        "CHECK INDEX CAPABILITIES:\n"
        "  info = get_index_info()\n"
        "  if info.get('status') != 'ok':\n"
        "      print('No index — все хелперы работают через filesystem fallback (медленнее).')\n"
        "      print('USER может построить индекс командой rlm_index(action=\\'build\\') — НЕ вызывай эту команду сам.')\n"
        "  else:\n"
        "      print(f\"Index v{info['builder_version']} ({info['methods']} methods)\")\n"
        "      caps = []\n"
        "      if info.get('has_fts'): caps.append('search_methods')\n"
        "      if info.get('has_synonyms'): caps.append('search_objects')\n"
        "      if info.get('has_regions'): caps.append('search_regions')\n"
        "      if info.get('has_module_headers'): caps.append('search_module_headers')\n"
        "      if info.get('has_form_elements'): caps.append('parse_form')\n"
        "      if info.get('has_object_attributes'): caps.append('find_attributes')\n"
        "      if info.get('has_predefined_items'): caps.append('find_predefined')\n"
        "      if info.get('has_extension_overrides'): caps.append('get_overrides')\n"
        "      print('INSTANT helpers:', caps)",
    )

    _reg(
        "find_http_services",
        find_http_services,
        "find_http_services(name='') -> [{name, root_url, templates}]",
        "business",
        ["http", "сервис", "endpoint", "rest", "api"],
        "FIND HTTP SERVICES:\n"
        "  services = find_http_services()\n"
        "  for s in services:\n"
        "      print(f\"  {s['name']} (/{s['root_url']})\")\n"
        "      for t in s['templates']:\n"
        "          print(f\"    {t['template']}: {[m['http_method'] for m in t['methods']]}\")",
    )
    _reg(
        "find_web_services",
        find_web_services,
        "find_web_services(name='') -> [{name, namespace, operations}]",
        "business",
        ["soap", "wsdl", "веб", "web service", "ws"],
        "FIND WEB SERVICES (SOAP):\n"
        "  services = find_web_services()\n"
        "  for s in services:\n"
        "      print(f\"  {s['name']} ns={s['namespace']}\")\n"
        "      for op in s['operations']:\n"
        "          print(f\"    {op['name']}({', '.join(op['params'])}) -> {op['return_type']}\")",
    )
    _reg(
        "find_xdto_packages",
        find_xdto_packages,
        "find_xdto_packages(name='') -> [{name, namespace, types}]",
        "business",
        ["xdto", "пакет", "namespace", "схема", "тип данных"],
        "FIND XDTO PACKAGES:\n"
        "  pkgs = find_xdto_packages()\n"
        "  for p in pkgs:\n"
        "      print(f\"  {p['name']} ns={p['namespace']} types={len(p.get('types', []))}\")",
    )
    _reg(
        "find_exchange_plan_content",
        find_exchange_plan_content,
        "find_exchange_plan_content(name) -> [{ref, auto_record}]",
        "business",
        ["обмен", "exchange", "план обмена", "синхрониз", "регистрац"],
        "FIND EXCHANGE PLAN CONTENT:\n"
        "  content = find_exchange_plan_content('ОбменУправлениеПредприятием')\n"
        "  for item in content:\n"
        "      print(f\"  {item['ref']} auto_record={item['auto_record']}\")",
    )

    _reg(
        "find_references_to_object",
        find_references_to_object,
        "find_references_to_object(object_ref, kinds=None, limit=1000, include_code=False) -> {object, references: [{used_in, path, line, kind}], total, truncated, partial, by_kind} (+ code_usages/code_total/code_by_kind/code_truncated/code_partial/code_meta when include_code=True)",
        "business",
        [
            "ссылк",
            "references",
            "где используется",
            "найти ссылки",
            "в свойствах",
            "поиск ссылок",
            "вхождения",
        ],
        "FIND REFERENCES TO OBJECT (analogue of Configurator 'Найти ссылки → В свойствах'):\n"
        "  res = find_references_to_object('Справочник.ВидыПодарочныхСертификатов')\n"
        "  print(f\"total={res['total']} by_kind={res['by_kind']}\")\n"
        "  for r in res['references'][:20]:\n"
        "      print(f\"  {r['kind']:25s} {r['used_in']} ({r['path']})\")\n"
        "  # Filter by kind:\n"
        "  attrs_only = find_references_to_object('Справочник.X', kinds=['attribute_type'])\n"
        "  # Metadata refs + in-code usages in one call:\n"
        "  full = find_references_to_object('Документ.X', include_code=True)\n"
        "  print(f\"meta={full['total']} code={full['code_total']} {full['code_by_kind']}\")\n"
        "  # On v11 indexes (no metadata_references table) — partial=True via live scan\n"
        "  # NB: line у attribute_type — best-effort строка первого по файлу\n"
        "  #   <Name>Имя</Name> (CF) / <name>Имя</name> (EDT), а не строка тега типа\n"
        "  #   (<v8:Type> в CF / <types> в EDT);\n"
        "  #   при одноимённых элементах якорь может относиться к более раннему блоку",
    )

    _reg(
        "find_data_path",
        find_data_path,
        "find_data_path(from_object, to_object, max_depth=4, kinds=None) -> "
        "{found, from, to, path:[{from, to, kind, used_in, path, line}]|None, depth, partial, "
        "_meta:{max_depth, nodes_expanded, node_budget, budget_exceeded, kinds}} "
        "| {found:False, error, hint, ...}  "
        "# N-hop BFS по графу МЕТАДАННЫХ (ссылки). endpoints — С ПРЕФИКСОМ (Справочник.X/Документ.Y)",
        "navigation",
        [
            "путь данных",
            "find_data_path",
            "граф данных",
            "как связаны",
            "data path",
            "цепочка ссылок",
            "связь объектов",
        ],
        "FIND DATA PATH (достижим ли to_object из from_object по ссылкам МЕТАДАННЫХ):\n"
        "  res = find_data_path('Документ.РеализацияТоваровУслуг', 'РегистрНакопления.Продажи')\n"
        "  if res.get('error'):\n"
        "      print(res['hint'])  # endpoints ОБЯЗАНЫ быть с префиксом: Справочник./Документ./…\n"
        "  elif res['found']:\n"
        "      for e in res['path']:  # forward: [from → … → to], каждый элемент = РЕБРО\n"
        "          print(f\"  {e['from']} --{e['kind']}--> {e['to']} ({e['used_in']})\")\n"
        "  else:\n"
        "      print('partial — нет таблицы metadata_references' if res['partial'] else 'путь не найден')\n"
        "  # Фильтр по виду ссылки: find_data_path('Документ.X', 'Справочник.Y', kinds=['attribute_type'])\n"
        "  # RU/EN-префикс принимается; _meta.budget_exceeded=True → обход обрезан (сузь max_depth).",
    )

    _reg(
        "find_code_usages",
        find_code_usages,
        "find_code_usages(object_ref, kind=None, limit=1000) -> {object, usages: [{path, object_name, category, module_type, line, kind, member}], by_kind, total, truncated, partial, _meta}",
        "business",
        [
            "использования в коде",
            "где используется в коде",
            "code usages",
            "обращения",
            "find_code_usages",
            "ТЧ в запросах",
        ],
        "FIND CODE USAGES (reverse: where a metadata object is used IN CODE):\n"
        "  res = find_code_usages('Документ.ПриобретениеТоваровУслуг')\n"
        "  print(f\"total={res['total']} by_kind={res['by_kind']}\")\n"
        "  for u in res['usages'][:20]:\n"
        "      tail = f\" .{u['member']}\" if u['member'] else ''\n"
        "      print(f\"  {u['kind']:8s} {u['path']}:{u['line']}{tail}\")\n"
        "  # kind: 'manager' (Документы.X) | 'ref_type' (\"ДокументСсылка.X\") | 'query' (Документ.X.ТЧ)\n"
        "  # Filter: find_code_usages('Документ.X', kind='query')\n"
        "  # Pairs with find_references_to_object (metadata-XML refs). Scope: main config only.",
    )

    _reg(
        "find_defined_types",
        find_defined_types,
        "find_defined_types(name) -> {name, types: list[str], path, partial}",
        "business",
        ["определяемый тип", "defined type", "ОпределяемыйТип"],
        "FIND DEFINED TYPES (раскрытие ОпределяемогоТипа):\n"
        "  dt = find_defined_types('ДенежнаяСуммаНеотрицательная')\n"
        "  print(dt['types'])  # -> ['Number'] or ['Catalog.X', 'Document.Y', ...]",
    )

    _reg(
        "detect_extensions",
        detect_extensions,
        "detect_extensions() -> {config_role, nearby_extensions:[{name, purpose, prefix, path, overrides_count}], nearby_main, warnings}",
        "extension",
        ["обнаружить расширения", "детект", "detect", "extension list"],
        "DETECT EXTENSIONS (диагностика контекста):\n"
        "  ctx = detect_extensions()\n"
        "  print(f\"Роль: {ctx['config_role']}\")  # main / extension / unknown\n"
        "  for e in ctx.get('nearby_extensions', []):\n"
        "      print(f\"  {e.get('name')} (prefix={e.get('prefix')}) перехватов={e.get('overrides_count')}\")  # ключ 'prefix', не 'name_prefix'\n"
        "  # overrides_count — index-side счёт перехватов: int/0 в MAIN-сессии, None если индекс не покрывал расширение\n"
        "  # Дальше: get_overrides() для индексных перехватов или find_ext_overrides(ext_path) live",
    )
    _reg(
        "find_ext_overrides",
        find_ext_overrides,
        "find_ext_overrides(extension_path, object_name='') -> {overrides[:200], total, truncated, partial, _meta?}",
        "extension",
        ["перехваты расширения", "ext_overrides", "live overrides", "перехваты live"],
        "FIND OVERRIDES IN EXTENSION (live, без индекса):\n"
        "  ctx = detect_extensions()\n"
        "  for e in ctx.get('nearby_extensions', []):\n"
        "      print(f\"  {e.get('name')} -> {e.get('path')}\")\n"
        "      ovr = find_ext_overrides(e['path'])  # перехваты расширения (первые 200; см. total/truncated)\n"
        "      print(f\"    total={ovr['total']} truncated={ovr['truncated']} partial={ovr['partial']}\")\n"
        "      for o in ovr['overrides'][:5]:\n"
        "          print(f\"      &{o['annotation']} {o['target_method']}\")\n"
        "  # Прицельный поиск по объекту (если расширения есть):\n"
        "  if ctx.get('nearby_extensions'):\n"
        "      ext_path = ctx['nearby_extensions'][0]['path']\n"
        "      ovr_obj = find_ext_overrides(ext_path, 'Номенклатура')\n"
        "  # Если есть индекс v9+ — предпочитай get_overrides() (мгновенно из SQLite).\n"
        "  # find_ext_overrides — для live-проверки на проектах без индекса или для верификации.",
    )
    _reg(
        "get_overrides",
        get_overrides,
        "get_overrides(object_name='', method_name='') -> {overrides[:200], total, truncated, partial, source,"
        " by_annotation, by_object_top, by_extension_top, unique_*}  # stats full iff partial=False",
        "extension",
        ["перехват", "override", "расширен", "extension", "вместо", "после", "перед"],
        "GET OVERRIDES:\n"
        "  result = get_overrides('Номенклатура')\n"
        "  for ov in result['overrides']:\n"
        "      print(f\"  {ov['target_method']} <- {ov['annotation']} {ov.get('extension_name', '')}\")\n"
        "  # To read extension method body:\n"
        "  body = read_procedure(path, 'MethodName', include_overrides=True)\n"
        "  # NOTE: extension files are OUTSIDE the sandbox: read_file/grep/glob_files on '../' paths\n"
        "  # raise PermissionError. BUT: high-level BSL helpers (read_procedure, extract_procedures,\n"
        "  # parse_object_xml, find_attributes, find_predefined, search) accept '../' paths returned by\n"
        "  # find_module and read extensions internally.",
    )

    _reg(
        "help",
        bsl_help,
        "help(task='') -> str  # get recipe: help('exports'), help('movements'), help('flow')",
        "navigation",
    )

    # git_search — opt-in full-text backend. Registered only when the sources
    # are under git ("auto", live sessions) or unconditionally for the rlm_help
    # doc snapshot ("force"); never under "never".
    _want_git_search = register_git_search == "force" or (register_git_search == "auto" and _git_search_available())
    if _want_git_search:
        _reg(
            "git_search",
            git_search,
            "git_search(pattern, path='', file_types='', regex=False, ignore_case=False, mode='lines', max_results=200, exclude_path='')"
            " -> [{file,line,text}] | [{file}] (mode='files'). FULL-TEXT over ALL files incl. raw XML/forms/queries."
            " exclude_path drops noisy zones (literal names at any depth, e.g. 'Forms,Templates')."
            " Only available when sources are under git.",
            "navigation",
            [
                "полнотекст",
                "поиск везде",
                "grep по всем файлам",
                "найти подстроку",
                "найти строку",
                "найти текст",
                "xml поиск",
                "git_search",
                "git grep",
            ],
            "FULL-TEXT SEARCH — all files, incl. raw XML/forms/rights/DCS/queries (only under git):\n"
            "  hits = git_search('VIN')                       # substring anywhere\n"
            "  hits = git_search('VIN', file_types='xml')     # narrow to a file type\n"
            "  hits = git_search('VIN', path='Catalogs', mode='files')  # overview: which files\n"
            "  hits = git_search('VIN', exclude_path='Forms,Templates')  # drop noisy XML zones (any depth)\n"
            "  for h in hits:\n"
            "      print(h.get('file'), h.get('line'), h.get('text', ''))\n"
            "  # Searches CURRENT on-disk state (incl. uncommitted + new untracked); .gitignore'd skipped.\n"
            "  # Anti-noise on common tokens: start with mode='files' or a narrow file_types/path, then drill down.\n"
            "  # Mind max_results / the {'_truncated': True} sentinel; regex=True is POSIX ERE\n"
            "  #   (end-of-line anchor on CRLF files needs '[[:space:]]*$', not '$').\n"
            "  # Failure -> [{'error': ..., 'hint': ...}] (NOT []): follow hint for safe_grep/grep pattern semantics.",
        )

    # ── Return all helpers (auto-generated from registry) ────────
    return {
        "_detected_prefixes": _ensure_prefixes,
        "_registry": _registry,
        **{k: v["fn"] for k, v in _registry.items()},
    }
