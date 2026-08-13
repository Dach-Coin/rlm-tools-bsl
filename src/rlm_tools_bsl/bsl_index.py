"""BSL Method Index — SQLite-based pre-index of procedures, functions, and call graph.

Provides fast lookup of all methods across a 1C/BSL codebase without full file scans.
The index is stored on disk and supports incremental updates.
"""

from __future__ import annotations

import errno
import functools
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from enum import Enum
from pathlib import Path
from typing import NamedTuple

from rlm_tools_bsl.bsl_knowledge import (
    BSL_PATTERNS,
    _merge_proc_continuations_with_mask,
    mask_comments_and_strings,
)
from rlm_tools_bsl.cache import _paths_hash
from rlm_tools_bsl.format_detector import BslFileInfo, parse_bsl_path
from rlm_tools_bsl.bsl_xml_parsers import (
    _CODE_MANAGER_COLLECTIONS,
    _CODE_QUERY_COLLECTIONS,
    _RU_REFTYPE_TO_CANONICAL,
    canonicalize_type_ref,
)

logger = logging.getLogger(__name__)

BUILDER_VERSION = 15


_active_locks: dict[str, "_BuildLock"] = {}
# Guards check/register/release on _active_locks against a TOCTOU race between two
# threads (latent I3): the owner/depth check + OS-lock + registration must be atomic.
# The OS lock is ALWAYS non-blocking (LK_NBLCK / LOCK_NB), so it is safe to take it
# under this threading lock — RULE: never call a *blocking* OS-lock under _active_locks_lock.
_active_locks_lock = threading.Lock()


class _BuildLock:
    """Exclusive file lock for index build/update.

    Uses OS-level file locking (fcntl on Unix, msvcrt on Windows) which is
    automatically released when the process dies. Reentrant **only within the same
    thread** (sequential build+update, and the nested ``_update_locked → self.build()``
    recovery): a different thread of the same process gets ``RuntimeError`` (like the
    cross-process case) — silent reentry would let two threads DROP/CREATE the same
    in-place DB concurrently. Reentry is tracked via owner-thread-id + depth on a
    shared owner record; the OS lock and lock-file are released only at ``depth == 0``.
    """

    def __init__(self, db_path: Path):
        self.lock_path = db_path.with_suffix(".lock")
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._fd: int | None = None
        self._key = str(self.lock_path)
        self._owner_tid: int | None = None
        self._depth = 0
        self._acquired = False

    def acquire(self) -> None:
        """Acquire exclusive lock. RuntimeError if held by another process OR thread."""
        tid = threading.get_ident()
        with _active_locks_lock:
            existing = _active_locks.get(self._key)
            if existing is not None:
                # Already held in this process. Reentry allowed ONLY for the same thread.
                if existing._owner_tid != tid:
                    raise RuntimeError(f"Index build already in progress in another thread (lock: {self.lock_path}).")
                existing._depth += 1
                self._acquired = True
                return

            # New acquisition — take the non-blocking OS lock under the threading lock.
            fd = os.open(str(self.lock_path), os.O_CREAT | os.O_RDWR)
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (OSError, IOError):
                os.close(fd)
                raise RuntimeError(
                    f"Index build already in progress (lock: {self.lock_path}). "
                    "Wait for it to finish or remove the lock file manually."
                )
            self._fd = fd
            self._owner_tid = tid
            self._depth = 1
            self._acquired = True
            _active_locks[self._key] = self

    def release(self) -> None:
        """Release one acquisition level. OS-unlock + lock-file unlink only at depth 0."""
        with _active_locks_lock:
            if not self._acquired:
                return
            self._acquired = False
            owner = _active_locks.get(self._key)
            if owner is None:
                return
            owner._depth -= 1
            if owner._depth > 0:
                return
            # depth == 0 → fully release the OS lock and unregister.
            _active_locks.pop(self._key, None)
            fd = owner._fd
            owner._fd = None
            owner._owner_tid = None
            if fd is not None:
                try:
                    if os.name == "nt":
                        import msvcrt

                        try:
                            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                        except OSError:
                            pass
                    else:
                        import fcntl

                        fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)
            try:
                owner.lock_path.unlink(missing_ok=True)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Regex patterns copied from bsl_helpers._parse_procedures
# (autonomous — no runtime import from bsl_helpers)
# ---------------------------------------------------------------------------
_PROC_DEF_RE = re.compile(BSL_PATTERNS["procedure_def"], re.IGNORECASE)
_PROC_END_RE = re.compile(BSL_PATTERNS["procedure_end"], re.IGNORECASE)

# Call-extraction patterns
_QUALIFIED_CALL_RE = re.compile(r"(\w+)\.(\w+)\s*\(")
_SIMPLE_CALL_RE = re.compile(r"(\w+)\s*\(")

# Reverse code-usage extraction (v1.14.0, metadata_code_usages):
#   _CODE_DOTTED_RE — a dotted identifier pair, applied to *stripped* code
#                     (no strings/comments) to catch manager collection access
#                     `Документы.X` / `Documents.X`.
#   _CODE_PATH_RE   — a 2- or 3-segment dotted path, applied to the *contents*
#                     of string literals to catch reference types and query
#                     metadata paths (incl. an optional tabular-section segment).
_CODE_DOTTED_RE = re.compile(r"(\w+)\.(\w+)")
_CODE_PATH_RE = re.compile(r"(\w+)\.(\w+)(?:\.(\w+))?")

# BSL keywords to exclude from call graph
_BSL_KEYWORDS: frozenset[str] = frozenset(
    {
        # Russian
        "Если",
        "Тогда",
        "Иначе",
        "ИначеЕсли",
        "КонецЕсли",
        "Пока",
        "Для",
        "Каждого",
        "Цикл",
        "КонецЦикла",
        "Возврат",
        "Новый",
        "Тип",
        "ТипЗнч",
        "Знач",
        "Перем",
        "Попытка",
        "Исключение",
        "КонецПопытки",
        "Выполнить",
        "НЕ",
        "И",
        "ИЛИ",
        "Процедура",
        "Функция",
        "КонецПроцедуры",
        "КонецФункции",
        # English
        "If",
        "Then",
        "Else",
        "ElsIf",
        "EndIf",
        "While",
        "For",
        "Each",
        "Do",
        "EndDo",
        "Return",
        "New",
        "Type",
        "TypeOf",
        "Val",
        "Var",
        "Try",
        "Except",
        "EndTry",
        "Execute",
        "NOT",
        "AND",
        "OR",
        "Procedure",
        "Function",
        "EndProcedure",
        "EndFunction",
    }
)

# Case-insensitive set for fast lookup
_BSL_KEYWORDS_LOWER: frozenset[str] = frozenset(k.lower() for k in _BSL_KEYWORDS)


# Reserved 1C PLATFORM globals — dropped ONLY from bare (unqualified) call edges.
# A bare ``Сообщить(`` / ``НСтр(`` / ``Новый Структура(`` is always the platform
# global/type, never a real call target → those rows are permanently unresolved
# noise (~27% of the whole ``calls`` table on real configs). NOT applied to the
# qualified ``Модуль.Метод(`` branch — a same-named CommonModule export could be
# real, so qualified edges are left untouched.
#
# Validated against real indexes (multiple ERP / документооборот / БГУ configs):
# every name here has ZERO resolved edges (oracle: ``removed_resolved == 0`` on
# every validated config). The following look platform-ish but are REAL user methods in some
# production config (the resolver bound a bare call to a same-named user method)
# and are DELIBERATELY EXCLUDED — never add them: ЗакрытьФорму,
# ПолеКомпоновкиДанных, ТаблицаЗначений, УникальныйИдентификатор, Массив, and
# Квартал (shadowed by a real user method in a БГУ "бюджетный учёт" config — the
# extraction filter runs BEFORE resolution, so keeping it would drop those edges).
# Source of truth = this frozenset +
# ``tests/test_calls_noise_filter.py::test_curated_set_guard``; re-validation =
# the opt-in oracle test (skipped without RLM_VALIDATION_INDEXES).
_BSL_GLOBAL_FUNCS_LOWER: frozenset[str] = frozenset(
    k.casefold()
    for k in {
        # --- strings ---
        "Сообщить",
        "Формат",
        "НСтр",
        "СтрШаблон",
        "СтрНайти",
        "СтрЗаменить",
        "СтрДлина",
        "СтрРазделить",
        "СтрСоединить",
        "СтрЧислоСтрок",
        "СтрЧислоВхождений",
        "СтрПолучитьСтроку",
        "СтрНачинаетсяС",
        "СтрЗаканчиваетсяС",
        "СтрСравнить",
        "ВРег",
        "НРег",
        "ТРег",
        "СокрЛ",
        "СокрП",
        "СокрЛП",
        "Лев",
        "Прав",
        "Сред",
        "Символ",
        "КодСимвола",
        "ПустаяСтрока",
        # --- dates ---
        "ТекущаяДата",
        "ТекущаяДатаСеанса",
        "Год",
        "Месяц",
        "День",
        "Час",
        "Минута",
        "Секунда",
        "ДеньНедели",
        "ДобавитьМесяц",
        "НачалоГода",
        "КонецГода",
        "НачалоКвартала",
        "КонецКвартала",
        "НачалоМесяца",
        "КонецМесяца",
        "НачалоДня",
        "КонецДня",
        "НачалоНедели",
        "КонецНедели",
        # --- math ---
        "Цел",
        "Окр",
        "Макс",
        "Мин",
        "Abs",
        "Pow",
        "Sqrt",
        "Exp",
        # --- types / values ---
        "ТипЗнч",
        "ЗначениеЗаполнено",
        "ЕстьNull",
        "ПредопределенноеЗначение",
        # --- type casts ---
        "Строка",
        "Число",
        "Дата",
        "Булево",
        # --- reserved types (only ever `Новый X(`) ---
        "Структура",
        "Соответствие",
        "СписокЗначений",
        "Запрос",
        "СхемаЗапроса",
        "ОписаниеОповещения",
        "ОписаниеТипов",
        "КвалификаторыСтроки",
        "КвалификаторыЧисла",
        "ХранилищеЗначения",
        "Цвет",
        # --- transactions / locks ---
        "НачатьТранзакцию",
        "ЗафиксироватьТранзакцию",
        "ОтменитьТранзакцию",
        "ТранзакцияАктивна",
        "ЗаблокироватьДанныеДляРедактирования",
        # --- forms / messages / options / privileges / temp storage ---
        "ОткрытьФорму",
        "ПолучитьФорму",
        "ЗаполнитьЗначенияСвойств",
        "ВыполнитьОбработкуОповещения",
        "ПолучитьФункциональнуюОпцию",
        "ПоказатьВопрос",
        "ПоказатьПредупреждение",
        "ПоказатьОповещениеПользователя",
        "Вопрос",
        "Предупреждение",
        "ОповеститьОбИзменении",
        "УстановитьПривилегированныйРежим",
        "ПривилегированныйРежим",
        "ПоместитьВоВременноеХранилище",
        "ПолучитьИзВременногоХранилища",
        "УдалитьИзВременногоХранилища",
        "ИнформацияОбОшибке",
        "Состояние",
        "ПравоДоступа",
        "ПодключитьОбработчикОжидания",
        "ЗначениеВДанныеФормы",
        "КопироватьДанныеФормы",
        "РеквизитФормыВЗначение",
        "ЗаписьЖурналаРегистрации",
        "Оповестить",
        "ОчиститьСообщения",
    }
)


# ---------------------------------------------------------------------------
# Call-graph resolution helpers (v1.16.0)
# ---------------------------------------------------------------------------
def _make_callee_key(rel_path: str, method_name: str) -> str:
    """Build the stable resolved-target key for a call edge.

    Key = ``"<rel_path>::<casefold(method_name)>"`` with the path separator
    normalized to ``/``. ``rel_path`` is UNIQUE in ``modules`` and survives a
    rebuild / rowid re-creation, so the key stays valid after an incremental
    update (unlike a ``methods.id`` rowid, which is re-issued for changed
    modules). The SAME helper is used on build-time (resolver) and query-time
    (target resolution in ``IndexReader.get_callers``) so keys match byte-for-byte.
    """
    norm_path = rel_path.replace("\\", "/")
    return f"{norm_path}::{method_name.casefold()}"


def _build_common_exported(conn: sqlite3.Connection) -> dict[tuple[str, str], str | None]:
    """``(object_name_cf, method_cf) -> rel_path | None`` (None = ambiguous).

    The DB-wide lookup for resolving qualified ``A.B`` call edges: exported methods
    of common modules. Uses an SQL filter (CommonModules / Module / exported) instead
    of scanning every method in the DB — the only tier that needs whole-DB visibility
    (a target may live in an unchanged common module). Shared by ``_bulk_insert``'s
    resolver and ``_reresolve_qualified_callers`` so build / increment / re-resolve
    produce byte-identical keys. Value ``None`` marks an ambiguous key (collision).
    """
    common: dict[tuple[str, str], str | None] = {}
    for row in conn.execute(
        "SELECT mod.rel_path, mod.object_name, me.name "
        "FROM methods me JOIN modules mod ON me.module_id = mod.id "
        "WHERE mod.category='CommonModules' AND mod.module_type='Module' "
        "AND me.is_export=1 AND mod.object_name IS NOT NULL"
    ):
        if isinstance(row, sqlite3.Row):
            rel_path, object_name, mname = row["rel_path"], row["object_name"], row["name"]
        else:
            rel_path, object_name, mname = row
        ck = (object_name.casefold(), mname.casefold())
        common[ck] = None if ck in common else rel_path
    return common


def _callee_short_expr(col: str = "callee_name") -> str:
    """SQL expression yielding the bare method name (suffix after the dot).

    ``callee_name`` has ALWAYS 0 or 1 dot (extractor regex ``(\\w+)\\.(\\w+)``),
    so the substring after the dot is the method name; with no dot the column
    itself is the name. This is the SINGLE SOURCE of the expression used by both
    the ``idx_calls_callee_short`` index DDL and ``get_callers`` query-building
    (Codex finding 2 — copies would silently drift into a full-scan regression).
    """
    return f"(CASE WHEN instr({col},'.')>0 THEN substr({col}, instr({col},'.')+1) ELSE {col} END)"


def _callee_match_clause(proc_name: str, col: str = "callee_name") -> tuple[str, str]:
    """Indexed equality clause matching callers of ``proc_name``; (sql, param).

    Replaces the leading-wildcard ``callee_name = ? OR callee_name LIKE '%.name'``
    (the ``%`` prefix forbade index use → full scan of ~3.7M rows). Because
    ``callee_name`` has ≤1 dot:

    * **bare ``proc_name``** → match the method-name suffix
      (``idx_calls_callee_short``): equals ``proc_name`` for both bare and
      ``X.proc_name`` qualified edges — same recall as the old clause, indexed.
    * **dotted ``Module.Method``** → match the FULL ``callee_name``
      (``idx_calls_callee``), preserving legacy behavior (a suffix match would
      drop the qualifier; the old ``LIKE '%.Module.Method'`` never fired at ≤1 dot).

    ``COLLATE NOCASE`` is load-bearing: without it the planner ignores the
    case-insensitive expression index and falls back to a scan.
    """
    if "." in proc_name:
        return (f"{col} = ? COLLATE NOCASE", proc_name)
    return (f"{_callee_short_expr(col)} = ? COLLATE NOCASE", proc_name)


# module_hint normalization for IndexReader.get_callers / find_call_hierarchy.
# Maps a casefolded RU/EN singular metadata prefix → the ``modules.category``
# value. This is a module-level map living next to IndexReader on purpose: the
# analogous bsl_helpers._normalize_object_ref / _RU_META_PREFIXES are nested
# inside the make_bsl_helpers closure and are NOT importable here (would create
# a circular dependency). Keep this minimal and aligned with
# format_detector.METADATA_CATEGORIES.
_HINT_PREFIX_TO_CATEGORY: dict[str, str] = {
    "документ": "Documents",
    "document": "Documents",
    "справочник": "Catalogs",
    "catalog": "Catalogs",
    "перечисление": "Enums",
    "enum": "Enums",
    "регистрсведений": "InformationRegisters",
    "informationregister": "InformationRegisters",
    "регистрнакопления": "AccumulationRegisters",
    "accumulationregister": "AccumulationRegisters",
    "регистрбухгалтерии": "AccountingRegisters",
    "accountingregister": "AccountingRegisters",
    "регистррасчета": "CalculationRegisters",
    "calculationregister": "CalculationRegisters",
    "планвидовхарактеристик": "ChartsOfCharacteristicTypes",
    "chartofcharacteristictypes": "ChartsOfCharacteristicTypes",
    "плансчетов": "ChartsOfAccounts",
    "chartofaccounts": "ChartsOfAccounts",
    "планвидоврасчета": "ChartsOfCalculationTypes",
    "chartofcalculationtypes": "ChartsOfCalculationTypes",
    "планобмена": "ExchangePlans",
    "exchangeplan": "ExchangePlans",
    "бизнеспроцесс": "BusinessProcesses",
    "businessprocess": "BusinessProcesses",
    "задача": "Tasks",
    "task": "Tasks",
    "отчет": "Reports",
    "report": "Reports",
    "обработка": "DataProcessors",
    "dataprocessor": "DataProcessors",
    "константа": "Constants",
    "constant": "Constants",
    "общиймодуль": "CommonModules",
    "commonmodule": "CommonModules",
}


def _normalize_module_hint(hint: str) -> tuple[str | None, str | None, str | None]:
    """Normalize a ``module_hint`` into ``(rel_path, category, object_name)``.

    Three accepted forms (exactly one tuple slot is populated besides
    ``object_name`` for the public form):
      - ``rel_path`` (contains ``/`` or ``\\``) → ``(<norm_path>, None, None)`` —
        the most precise, unambiguous form (used for deep propagation);
      - public ``Документ.X`` / ``Document.X`` (RU/EN prefix) →
        ``(None, "Documents", "X")``;
      - bare ``object_name`` (``РеализацияТоваровУслуг``) →
        ``(None, None, "X")``.

    Returns all-``None`` for an empty/None hint. An unknown dotted prefix is
    treated as a bare object_name (it simply will not match → no exact mode,
    name-based fallback still applies).
    """
    if not hint:
        return (None, None, None)
    h = hint.strip()
    if not h:
        return (None, None, None)
    if "/" in h or "\\" in h:
        return (h.replace("\\", "/"), None, None)
    if "." in h:
        prefix, _, obj = h.partition(".")
        category = _HINT_PREFIX_TO_CATEGORY.get(prefix.casefold())
        if category and obj:
            return (None, category, obj)
        # Unknown prefix — not a recognized metadata reference; treat the whole
        # token as a bare object_name (will not match a dotted object_name).
        return (None, None, h)
    return (None, None, h)


# ---------------------------------------------------------------------------
# SQL schema
# ---------------------------------------------------------------------------
_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS index_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS modules (
    id INTEGER PRIMARY KEY,
    rel_path TEXT UNIQUE NOT NULL,
    category TEXT,
    object_name TEXT,
    module_type TEXT,
    form_name TEXT,
    is_form INTEGER DEFAULT 0,
    mtime REAL,
    size INTEGER
);

CREATE TABLE IF NOT EXISTS methods (
    id INTEGER PRIMARY KEY,
    module_id INTEGER NOT NULL REFERENCES modules(id),
    name TEXT NOT NULL,
    type TEXT NOT NULL,
    is_export INTEGER DEFAULT 0,
    params TEXT,
    line INTEGER,
    end_line INTEGER,
    loc INTEGER
);

CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY,
    caller_id INTEGER NOT NULL REFERENCES methods(id),
    callee_name TEXT NOT NULL,
    line INTEGER,
    callee_key TEXT
);

CREATE INDEX IF NOT EXISTS idx_mod_object ON modules(object_name);
CREATE INDEX IF NOT EXISTS idx_mod_category ON modules(category);
CREATE INDEX IF NOT EXISTS idx_meth_name ON methods(name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_meth_module ON methods(module_id);
CREATE INDEX IF NOT EXISTS idx_calls_callee ON calls(callee_name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_calls_callee_key ON calls(callee_key);
-- idx_calls_caller removed: saves ~56MB on ERP, update uses callee-based cleanup instead

-- Level-2 metadata tables (optional, controlled by --no-metadata flag)
CREATE TABLE IF NOT EXISTS event_subscriptions (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    synonym TEXT,
    event TEXT,
    handler_module TEXT,
    handler_procedure TEXT,
    source_types TEXT,
    source_count INTEGER,
    file TEXT
);
CREATE INDEX IF NOT EXISTS idx_es_name ON event_subscriptions(name);

CREATE TABLE IF NOT EXISTS scheduled_jobs (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    synonym TEXT,
    method_name TEXT,
    handler_module TEXT,
    handler_procedure TEXT,
    use INTEGER DEFAULT 1,
    predefined INTEGER DEFAULT 0,
    restart_count INTEGER DEFAULT 0,
    restart_interval INTEGER DEFAULT 0,
    file TEXT
);
CREATE INDEX IF NOT EXISTS idx_sj_name ON scheduled_jobs(name);

CREATE TABLE IF NOT EXISTS functional_options (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    synonym TEXT,
    location TEXT,
    content TEXT,
    file TEXT
);
CREATE INDEX IF NOT EXISTS idx_fo_name ON functional_options(name);

-- Level-3: role rights (normalized, one row per right)
CREATE TABLE IF NOT EXISTS role_rights (
    id INTEGER PRIMARY KEY,
    role_name TEXT NOT NULL,
    object_name TEXT NOT NULL,
    right_name TEXT NOT NULL,
    file TEXT
);
CREATE INDEX IF NOT EXISTS idx_rr_object ON role_rights(object_name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_rr_role ON role_rights(role_name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_rr_right ON role_rights(right_name);

-- Level-3: register movements (in-band, extracted during BSL processing)
CREATE TABLE IF NOT EXISTS register_movements (
    id INTEGER PRIMARY KEY,
    document_name TEXT NOT NULL,
    register_name TEXT NOT NULL,
    source TEXT DEFAULT 'code',
    file TEXT
);
CREATE INDEX IF NOT EXISTS idx_rm_document ON register_movements(document_name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_rm_register ON register_movements(register_name COLLATE NOCASE);

-- Level-3: enum values
CREATE TABLE IF NOT EXISTS enum_values (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    synonym TEXT,
    values_json TEXT NOT NULL,
    source_file TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_enum_name ON enum_values(name COLLATE NOCASE);

-- Level-3: subsystem content (normalized, one row per subsystem-object pair)
CREATE TABLE IF NOT EXISTS subsystem_content (
    id INTEGER PRIMARY KEY,
    subsystem_name TEXT NOT NULL,
    subsystem_synonym TEXT,
    object_ref TEXT NOT NULL,
    file TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sc_object ON subsystem_content(object_ref COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_sc_subsystem ON subsystem_content(subsystem_name COLLATE NOCASE);

-- Level-4: file navigation index (glob/tree/find_files acceleration)
CREATE TABLE IF NOT EXISTS file_paths (
    id INTEGER PRIMARY KEY,
    rel_path TEXT NOT NULL UNIQUE,
    extension TEXT NOT NULL,
    dir_path TEXT NOT NULL,
    filename TEXT NOT NULL,
    depth INTEGER NOT NULL,
    size INTEGER,
    mtime REAL
);
CREATE INDEX IF NOT EXISTS idx_fp_ext ON file_paths(extension);
CREATE INDEX IF NOT EXISTS idx_fp_dir ON file_paths(dir_path);
CREATE INDEX IF NOT EXISTS idx_fp_filename ON file_paths(filename COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_fp_depth ON file_paths(depth);

-- Level-5: integration metadata
CREATE TABLE IF NOT EXISTS http_services (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    root_url TEXT NOT NULL,
    templates_json TEXT NOT NULL,
    file TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_hs_name ON http_services(name COLLATE NOCASE);

CREATE TABLE IF NOT EXISTS web_services (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    namespace TEXT NOT NULL,
    operations_json TEXT NOT NULL,
    file TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ws_name ON web_services(name COLLATE NOCASE);

CREATE TABLE IF NOT EXISTS xdto_packages (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    namespace TEXT NOT NULL,
    types_json TEXT NOT NULL,
    file TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_xp_name ON xdto_packages(name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_xp_ns ON xdto_packages(namespace);

-- Level-6: object synonyms (business-name search)
CREATE TABLE IF NOT EXISTS object_synonyms (
    id INTEGER PRIMARY KEY,
    object_name TEXT NOT NULL,
    category TEXT NOT NULL,
    synonym TEXT NOT NULL,
    file TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_synonyms_object ON object_synonyms(object_name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_synonyms_synonym ON object_synonyms(synonym COLLATE NOCASE);

-- Level-7: regions and module headers (semantic context)
CREATE TABLE IF NOT EXISTS regions (
    id INTEGER PRIMARY KEY,
    module_id INTEGER REFERENCES modules(id),
    name TEXT NOT NULL,
    line INTEGER NOT NULL,
    end_line INTEGER
);
CREATE INDEX IF NOT EXISTS idx_regions_module ON regions(module_id);
CREATE INDEX IF NOT EXISTS idx_regions_name ON regions(name COLLATE NOCASE);

CREATE TABLE IF NOT EXISTS module_headers (
    module_id INTEGER PRIMARY KEY REFERENCES modules(id),
    header_comment TEXT NOT NULL
);

-- Level-8: extension overrides (связь исходный объект ↔ расширенный метод)
CREATE TABLE IF NOT EXISTS extension_overrides (
    id INTEGER PRIMARY KEY,
    -- Исходный объект (ЧТО перехвачено)
    object_name TEXT NOT NULL,
    source_path TEXT NOT NULL DEFAULT '',
    source_module_id INTEGER,
    target_method TEXT NOT NULL,
    target_method_line INTEGER,
    -- Тип перехвата
    annotation TEXT NOT NULL,
    -- Расширение (КТО перехватил)
    extension_name TEXT NOT NULL,
    extension_purpose TEXT,
    extension_method TEXT,
    extension_root TEXT NOT NULL,
    ext_module_path TEXT NOT NULL,
    ext_line INTEGER
);
CREATE INDEX IF NOT EXISTS idx_eo_object ON extension_overrides(object_name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_eo_method ON extension_overrides(target_method COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_eo_source ON extension_overrides(source_module_id);
CREATE INDEX IF NOT EXISTS idx_eo_ext ON extension_overrides(extension_name COLLATE NOCASE);

-- Level-11: object attributes (реквизиты, измерения, ресурсы с типами)
CREATE TABLE IF NOT EXISTS object_attributes (
    id INTEGER PRIMARY KEY,
    object_name TEXT NOT NULL,
    category TEXT NOT NULL,
    attr_name TEXT NOT NULL,
    attr_synonym TEXT,
    attr_type TEXT,
    attr_kind TEXT NOT NULL,
    ts_name TEXT,
    source_file TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_oa_object ON object_attributes(object_name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_oa_attr ON object_attributes(attr_name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_oa_category ON object_attributes(category);

-- Level-11: predefined items (предопределённые элементы ПВХ, справочников, планов счетов)
CREATE TABLE IF NOT EXISTS predefined_items (
    id INTEGER PRIMARY KEY,
    object_name TEXT NOT NULL,
    category TEXT NOT NULL,
    item_name TEXT NOT NULL,
    item_synonym TEXT,
    item_code TEXT,
    types_json TEXT,
    is_folder INTEGER DEFAULT 0,
    source_file TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pi_object ON predefined_items(object_name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_pi_item ON predefined_items(item_name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_pi_synonym ON predefined_items(item_synonym COLLATE NOCASE);

-- Level-12 (v1.9.0): metadata references reverse-index for find_references_to_object
CREATE TABLE IF NOT EXISTS metadata_references (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_object   TEXT NOT NULL,
    source_category TEXT NOT NULL,
    ref_object      TEXT NOT NULL,
    ref_kind        TEXT NOT NULL,
    used_in         TEXT NOT NULL,
    path            TEXT NOT NULL,
    line            INTEGER
);
CREATE INDEX IF NOT EXISTS idx_mref_ref      ON metadata_references(ref_object);
CREATE INDEX IF NOT EXISTS idx_mref_src      ON metadata_references(source_object);
CREATE INDEX IF NOT EXISTS idx_mref_kind     ON metadata_references(ref_kind);
CREATE INDEX IF NOT EXISTS idx_mref_category ON metadata_references(source_category);

-- Level-12: exchange plan content (specialised — for find_exchange_plan_content fast path)
CREATE TABLE IF NOT EXISTS exchange_plan_content (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_name   TEXT NOT NULL,
    object_ref  TEXT NOT NULL,
    auto_record INTEGER NOT NULL,
    path        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_epc_plan ON exchange_plan_content(plan_name);
CREATE INDEX IF NOT EXISTS idx_epc_ref  ON exchange_plan_content(object_ref);

-- Level-12: defined types (for find_defined_types)
CREATE TABLE IF NOT EXISTS defined_types (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT NOT NULL,
    type_refs_json TEXT NOT NULL,
    path           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dt_name ON defined_types(name);

-- Level-12: characteristic types (ChartsOfCharacteristicTypes Type list)
CREATE TABLE IF NOT EXISTS characteristic_types (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    pvh_name       TEXT NOT NULL,
    type_refs_json TEXT NOT NULL,
    path           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ct_pvh ON characteristic_types(pvh_name);

-- Level-13 (v1.14.0): reverse code-usage index for find_code_usages.
-- Code-derived (mirrors regions/module_headers): scanned from .bsl bodies,
-- keyed by module_id. object_ref_key = object_ref.lower() gives an indexed,
-- Unicode/Cyrillic case-insensitive exact lookup WITHOUT the py_lower UDF
-- (which is registered only on the reader connection and defeats a plain index).
CREATE TABLE IF NOT EXISTS metadata_code_usages (
    id             INTEGER PRIMARY KEY,
    module_id      INTEGER NOT NULL REFERENCES modules(id),
    object_ref     TEXT NOT NULL,
    object_ref_key TEXT NOT NULL,
    member_path    TEXT,
    usage_kind     TEXT NOT NULL,
    line           INTEGER
);
CREATE INDEX IF NOT EXISTS idx_mcu_ref ON metadata_code_usages(object_ref_key);
"""

# Expression index for the bare method-name suffix (v1.16.0): lets get_callers /
# find_call_hierarchy match callers by method name via index equality instead of
# a leading-wildcard ``LIKE '%.name'`` full scan. Appended (not f-string-ified
# into the big literal above) so the DDL is built from the SINGLE-SOURCE
# _callee_short_expr — covers BOTH executescript sites (empty-repo + normal build)
# with no DDL duplication / drift (Codex round-4).
_SCHEMA_SQL += (
    f"CREATE INDEX IF NOT EXISTS idx_calls_callee_short ON calls({_callee_short_expr('callee_name')} COLLATE NOCASE);\n"
)


# ---------------------------------------------------------------------------
# Category → Russian display name mapping for object synonyms
# ---------------------------------------------------------------------------
_CATEGORY_RU: dict[str, str] = {
    "CommonModules": "Общий модуль",
    "Documents": "Документ",
    "Catalogs": "Справочник",
    "InformationRegisters": "Регистр сведений",
    "AccumulationRegisters": "Регистр накопления",
    "AccountingRegisters": "Регистр бухгалтерии",
    "CalculationRegisters": "Регистр расчёта",
    "Reports": "Отчёт",
    "DataProcessors": "Обработка",
    "Constants": "Константа",
    "Enums": "Перечисление",
    "ChartsOfAccounts": "План счетов",
    "ChartsOfCharacteristicTypes": "План видов характеристик",
    "ChartsOfCalculationTypes": "План видов расчёта",
    "CommonForms": "Общая форма",
    "CommonCommands": "Общая команда",
    "CommonTemplates": "Общий макет",
    "BusinessProcesses": "Бизнес-процесс",
    "Tasks": "Задача",
    "ExchangePlans": "План обмена",
    "Roles": "Роль",
    "DocumentJournals": "Журнал документов",
    "FilterCriteria": "Критерий отбора",
    "SettingsStorages": "Хранилище настроек",
    "Subsystems": "Подсистема",
    "XDTOPackages": "XDTO-пакет",
    "ExternalDataSources": "Внешний источник данных",
    "HTTPServices": "HTTP-сервис",
    "WebServices": "Веб-сервис",
    "EventSubscriptions": "Подписка на событие",
    "ScheduledJobs": "Регламентное задание",
    "FunctionalOptions": "Функциональная опция",
    "DefinedTypes": "Определяемый тип",
}

# Map: top-level category folder -> 1C metadata type singular prefix (used in `used_in`)
_CATEGORY_TO_TYPE_PREFIX: dict[str, str] = {
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
    "Roles": "Role",
    "Constants": "Constant",
    "Reports": "Report",
    "DataProcessors": "DataProcessor",
    "DefinedTypes": "DefinedType",
    "CommonCommands": "CommonCommand",
    "DocumentJournals": "DocumentJournal",
}

# Top-level categories that contribute rows into metadata_references.
# Used for category-aware DELETE during incremental git fast path.
_METADATA_REFERENCES_TRIGGER_CATEGORIES: frozenset[str] = frozenset(
    {
        "Documents",
        "Catalogs",
        "InformationRegisters",
        "AccumulationRegisters",
        "AccountingRegisters",
        "CalculationRegisters",
        "ChartsOfCharacteristicTypes",
        "ChartsOfAccounts",
        "ChartsOfCalculationTypes",
        "Enums",
        "ExchangePlans",
        "DefinedTypes",
        "CommonCommands",
        "Subsystems",
        "FunctionalOptions",
        "EventSubscriptions",
        "Roles",
        "Tasks",
        "BusinessProcesses",
        "Constants",
        "DocumentJournals",
        "Reports",
        "DataProcessors",
    }
)

_SYNONYM_CATEGORIES: frozenset[str] = frozenset(_CATEGORY_RU.keys())

# Categories with object attributes (реквизиты, измерения, ресурсы, ТЧ)
_ATTR_CATEGORIES: list[str] = [
    "Documents",
    "Catalogs",
    "InformationRegisters",
    "AccumulationRegisters",
    "AccountingRegisters",
    "ChartsOfCharacteristicTypes",
]

# Categories with predefined items
_PREDEFINED_CATEGORIES: list[str] = [
    "ChartsOfCharacteristicTypes",
    "Catalogs",
    "ChartsOfAccounts",
]

# ---------------------------------------------------------------------------
# Module-level helpers shared between bulk collector and pointwise refresh
# ---------------------------------------------------------------------------

# CF XML filename hints by host category (used by _find_metadata_xml fallback)
_CF_XML_HINTS: dict[str, str] = {
    "Documents": "Document",
    "Catalogs": "Catalog",
    "InformationRegisters": "RecordSet",
    "AccumulationRegisters": "RecordSet",
    "AccountingRegisters": "RecordSet",
    "ChartsOfCharacteristicTypes": "ChartOfCharacteristicTypes",
}


def _find_metadata_xml(obj_dir: Path, category: str) -> Path | None:
    """Find metadata XML for an object directory using all known layouts.

    Priority order:
        1. EDT  ``<obj_dir>/<name>.mdo``
        2. CF sibling  ``<obj_dir>.parent/<name>.xml``  (works even if obj_dir
           does not physically exist — Path.parent does not require existence)
        3. CF Ext hint  ``<obj_dir>/Ext/<Hint>.xml``
        4. CF Ext fallback — first ``*.xml`` in ``<obj_dir>/Ext``
    """
    obj_name = obj_dir.name
    mdo = obj_dir / f"{obj_name}.mdo"
    if mdo.is_file():
        return mdo
    sibling_xml = obj_dir.parent / f"{obj_name}.xml"
    if sibling_xml.is_file():
        return sibling_xml
    hint = _CF_XML_HINTS.get(category)
    if hint:
        cf = obj_dir / "Ext" / f"{hint}.xml"
        if cf.is_file():
            return cf
    ext_dir = obj_dir / "Ext"
    if ext_dir.is_dir():
        for fp in sorted(ext_dir.iterdir()):
            if fp.suffix.lower() == ".xml" and fp.is_file():
                return fp
    return None


# Categories that host per-object Commands (Catalogs/X/Commands/...)
_CMD_HOST_CATS: tuple[str, ...] = (
    "Catalogs",
    "Documents",
    "InformationRegisters",
    "AccumulationRegisters",
    "AccountingRegisters",
    "ChartsOfCharacteristicTypes",
    "ChartsOfAccounts",
    "ChartsOfCalculationTypes",
    "BusinessProcesses",
    "Tasks",
    "ExchangePlans",
    "Reports",
    "DataProcessors",
)

# ---------------------------------------------------------------------------
# Pointwise incremental refresh — eligibility whitelist
# ---------------------------------------------------------------------------
# Categorical tables (object_attributes / predefined_items / object_synonyms /
# metadata_references). All listed categories must be fully covered by per-object
# refresh helpers, otherwise stale rows remain in tables not handled by the
# pointwise path.
_POINTWISE_ELIGIBLE_GROUP_A: frozenset[str] = frozenset(
    {
        "Catalogs",
        "Documents",
        "InformationRegisters",
        "AccumulationRegisters",
        "AccountingRegisters",
        "ChartsOfAccounts",
    }
)
# NB: ChartsOfCharacteristicTypes intentionally NOT in whitelist — it populates
# the dedicated `characteristic_types` table via parse_pvh_characteristics; that
# requires extra per-object support and is left for a future commit.

# Global tables (one row per object, no `category` column).
_POINTWISE_ELIGIBLE_GROUP_B: frozenset[str] = frozenset(
    {
        "EventSubscriptions",
        "ScheduledJobs",
        "XDTOPackages",
    }
)

# Soft thresholds for falling back to category-wide rescan when too many objects
# changed at once (tuning candidates — adjust based on real-project telemetry).
_POINTWISE_MAX_OBJECTS = 50
_POINTWISE_MAX_RATIO = 0.5

# Map: Group B category -> SQL table that stores its rows.
_GLOBAL_TABLE_BY_CATEGORY: dict[str, str] = {
    "EventSubscriptions": "event_subscriptions",
    "ScheduledJobs": "scheduled_jobs",
    "XDTOPackages": "xdto_packages",
}

# ---------------------------------------------------------------------------
# Git utilities for incremental delta detection
# ---------------------------------------------------------------------------

_git_exe: str | None | bool = None  # cached: str=path, False=not found, None=not yet searched

# Common kwargs for all git subprocess calls.
# errors="replace" handles cp1251 stderr from git on Windows services.
_GIT_SUBPROCESS_KW: dict = {
    "capture_output": True,
    "text": True,
    "encoding": "utf-8",
    "errors": "replace",
}


class _GitDirtyResult(NamedTuple):
    """Result of a git dirty / changed-files detection.

    *paths* — detected paths (relative to base_path). May be **incomplete**
    when *unreliable_reason* is set.
    *unreliable_reason* — ``None`` when the detection is trustworthy; otherwise a
    short human-readable reason (e.g. ``"unstaged diff timeout"``) meaning a
    best-effort command timed out / failed, so *paths* may be missing real
    changes. Callers must then force a full scan instead of trusting the set.
    """

    paths: set[str]
    unreliable_reason: str | None = None


def _git_dirty_timeout() -> int:
    """Timeout (seconds) for best-effort work-tree git commands.

    Configurable via ``RLM_GIT_DIRTY_TIMEOUT``. Defaults to 120s: on slow
    filesystems (Docker Desktop / Virtiofs on Windows) dirty detection over a
    large CRLF-vs-LF tree was measured at ~97s, so a lower bound would push the
    update into a full scan; 120s lets detection finish and keep the git fast
    path. Harmless for healthy repos (detection finishes in seconds, well under
    the ceiling). Invalid values fall back to 120.
    """
    try:
        return max(1, int(os.environ.get("RLM_GIT_DIRTY_TIMEOUT", "120")))
    except (ValueError, TypeError):
        return 120


def _lines_to_set(stdout: str) -> set[str]:
    """Parse ``--name-only`` style git output (one path per line)."""
    return {line.strip() for line in stdout.splitlines() if line.strip()}


def _parse_numstat_paths(stdout: str) -> set[str]:
    """Parse ``git diff --numstat`` output, returning the changed paths.

    Format per line: ``<added>\\t<deleted>\\t<path>`` (binary files use ``-``).
    Crucially, ``--numstat`` combined with ``--ignore-cr-at-eol`` **omits**
    files whose only difference is CRLF-vs-LF, whereas ``--name-only`` still
    lists them — so work-tree diffs use numstat to avoid re-parsing the whole
    tree when an LF index meets a CRLF checkout.
    """
    paths: set[str] = set()
    for raw in stdout.splitlines():
        if not raw.strip():
            continue
        parts = raw.split("\t", 2)
        if len(parts) == 3:
            p = parts[2].strip()
            if p:
                paths.add(p)
    return paths


def _find_git() -> str | None:
    """Resolve git executable path (cached).

    On Windows services ``git`` is often missing from PATH.
    Falls back to common install locations and registry.
    """
    global _git_exe  # noqa: PLW0603
    if _git_exe is not None:
        return _git_exe if _git_exe is not False else None

    found = shutil.which("git")
    if found:
        _git_exe = found
        logger.debug("_find_git: shutil.which → %s", found)
        return _git_exe

    # Windows fallback: check common locations
    if os.name == "nt":
        candidates = [
            os.path.expandvars(r"%ProgramFiles%\Git\cmd\git.exe"),
            os.path.expandvars(r"%ProgramFiles(x86)%\Git\cmd\git.exe"),
            os.path.expandvars(r"%LocalAppData%\Programs\Git\cmd\git.exe"),
        ]
        for c in candidates:
            if os.path.isfile(c):
                _git_exe = c
                logger.info("_find_git: found at %s (PATH fallback)", c)
                return _git_exe
        # Registry fallback
        try:
            import winreg

            for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                try:
                    with winreg.OpenKey(hive, r"SOFTWARE\GitForWindows") as key:
                        install_path = winreg.QueryValueEx(key, "InstallPath")[0]
                        candidate = os.path.join(install_path, "cmd", "git.exe")
                        if os.path.isfile(candidate):
                            _git_exe = candidate
                            logger.info("_find_git: found at %s (registry)", candidate)
                            return _git_exe
                except OSError:
                    pass
        except ImportError:
            pass

    logger.warning("_find_git: git not found (which=None, candidates checked, registry checked)")
    _git_exe = False  # cache negative result — don't retry
    return None


def _git_base_cmd(base_path: str) -> list[str] | None:
    """Return ``[git, -C, base_path, -c, safe.directory=*]`` or ``None``.

    ``safe.directory=*`` is required because Windows services run under SYSTEM
    which doesn't own user-created repos (CVE-2022-24765 protection).
    The flag is scoped to this subprocess only, not global git config.
    We never modify the repo — only read HEAD, diff, and ls-files.
    """
    git = _find_git()
    if git is None:
        return None
    return [git, "-C", base_path, "-c", "safe.directory=*"]


def _git_available(base_path: str) -> bool:
    """Check if *base_path* is inside a git work-tree **and** ``git`` is reachable."""
    cmd = _git_base_cmd(base_path)
    if cmd is None:
        logger.debug("_git_available: _find_git returned None")
        return False
    try:
        r = subprocess.run(
            [*cmd, "rev-parse", "--is-inside-work-tree"],
            **_GIT_SUBPROCESS_KW,
            timeout=10,
        )
        ok = r.returncode == 0 and r.stdout.strip() == "true"
        if not ok:
            logger.info(
                "_git_available: cmd=%s rc=%d stdout=%r stderr=%r",
                cmd[0],
                r.returncode,
                r.stdout.strip(),
                r.stderr.strip()[:200],
            )
        return ok
    except (subprocess.TimeoutExpired, OSError, ValueError) as exc:
        logger.info("_git_available: exception %s: %s", type(exc).__name__, exc)
        return False


def _git_repo_info(base_path: str) -> tuple[str, str] | None:
    """Return ``(git_root, prefix)`` or ``None`` on error.

    *prefix* is the POSIX relative path from *git_root* to *base_path*
    (empty string when they coincide).
    """
    cmd = _git_base_cmd(base_path)
    if cmd is None:
        return None
    try:
        r = subprocess.run(
            [*cmd, "rev-parse", "--show-toplevel"],
            **_GIT_SUBPROCESS_KW,
            timeout=10,
        )
        if r.returncode != 0:
            return None
        git_root = r.stdout.strip()
        prefix = Path(base_path).resolve().relative_to(Path(git_root).resolve()).as_posix()
        if prefix == ".":
            prefix = ""
        return (git_root, prefix)
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return None


def _git_head_sha(base_path: str) -> str | None:
    """Return current HEAD SHA (40-char hex) or ``None``."""
    cmd = _git_base_cmd(base_path)
    if cmd is None:
        return None
    try:
        r = subprocess.run(
            [*cmd, "rev-parse", "HEAD"],
            **_GIT_SUBPROCESS_KW,
            timeout=10,
        )
        return r.stdout.strip() if r.returncode == 0 else None
    except (subprocess.TimeoutExpired, OSError):
        return None


def _git_changed_files(base_path: str, since_commit: str, prefix: str) -> _GitDirtyResult | None:
    """Collect all files changed since *since_commit* (committed+staged+unstaged+untracked).

    Returns a :class:`_GitDirtyResult` with paths relative to *base_path*
    (prefix stripped). Returns ``None`` on a **critical** git error (caller must
    fall back to full scan). When a best-effort work-tree command times out /
    fails, the result's ``unreliable_reason`` is set: the path set may be
    incomplete and the caller must force a full scan (and persist the
    ``git_dirty_unreliable`` flag) rather than silently dropping the changes.
    """
    # Verify since_commit is an ancestor of HEAD
    cmd = _git_base_cmd(base_path)
    if cmd is None:
        return None
    try:
        r = subprocess.run(
            [*cmd, "merge-base", "--is-ancestor", since_commit, "HEAD"],
            **_GIT_SUBPROCESS_KW,
            timeout=60,
        )
        if r.returncode != 0:
            return None
    except (subprocess.TimeoutExpired, OSError):
        return None

    base_args = [*cmd, "-c", "core.quotePath=false"]
    best_effort_timeout = _git_dirty_timeout()
    unreliable_reason: str | None = None

    def _run_critical(args: list[str]) -> set[str] | None:
        """Run a critical git command; ``None`` on failure aborts the fast path."""
        try:
            r = subprocess.run(args, **_GIT_SUBPROCESS_KW, timeout=60)
            if r.returncode != 0:
                return None
            return _lines_to_set(r.stdout)
        except (subprocess.TimeoutExpired, OSError):
            return None

    def _run_best_effort(args: list[str], label: str, parser=_lines_to_set) -> set[str]:
        """Run a best-effort work-tree git command.

        On timeout/error the failing command's files are skipped, but
        *unreliable_reason* is set so the caller forces a full scan instead of
        silently losing uncommitted changes.
        """
        nonlocal unreliable_reason
        try:
            r = subprocess.run(args, **_GIT_SUBPROCESS_KW, timeout=best_effort_timeout)
            if r.returncode != 0:
                logger.info("_git_changed_files: %s rc=%d, marking unreliable", label, r.returncode)
                if unreliable_reason is None:
                    unreliable_reason = f"{label} rc={r.returncode}"
                return set()
            return parser(r.stdout)
        except subprocess.TimeoutExpired:
            logger.info("_git_changed_files: %s timeout, marking unreliable", label)
            if unreliable_reason is None:
                unreliable_reason = f"{label} timeout"
            return set()
        except OSError as exc:
            logger.info("_git_changed_files: %s %s, marking unreliable", label, type(exc).__name__)
            if unreliable_reason is None:
                unreliable_reason = f"{label} error"
            return set()

    def _strip_prefix(paths: set[str]) -> set[str]:
        """Strip git-root prefix to get paths relative to base_path."""
        if not prefix:
            return paths
        result: set[str] = set()
        pfx = prefix + "/"
        for p in paths:
            if p.startswith(pfx):
                result.add(p[len(pfx) :])
        return result

    # 1: committed diff — CRITICAL (compares git objects, no FS access, always fast).
    committed = _run_critical(
        [
            *base_args,
            "diff",
            "--name-only",
            "--no-renames",
            "--no-ext-diff",
            "--no-textconv",
            since_commit,
            "HEAD",
            "--",
            ".",
        ],
    )
    if committed is None:
        return None

    # 2-3: staged & unstaged diff — BEST-EFFORT.
    # These commands stat() every file in the work-tree which can exceed the
    # subprocess timeout on Docker Desktop / Virtiofs with large projects.
    # We use --numstat (not --name-only) together with --ignore-cr-at-eol: only
    # this combination OMITS files whose sole difference is CRLF-vs-LF (index LF,
    # Windows checkout CRLF). --name-only still lists such files even with the
    # flag, which would re-parse the whole tree. (git still reads the files, so
    # the command may still time out → unreliable → full scan.)
    staged = _run_best_effort(
        [
            *base_args,
            "diff",
            "--numstat",
            "--no-renames",
            "--no-ext-diff",
            "--no-textconv",
            "--ignore-cr-at-eol",
            "--cached",
            "--",
            ".",
        ],
        "staged diff",
        parser=_parse_numstat_paths,
    )
    unstaged = _run_best_effort(
        [
            *base_args,
            "diff",
            "--numstat",
            "--no-renames",
            "--no-ext-diff",
            "--no-textconv",
            "--ignore-cr-at-eol",
            "--",
            ".",
        ],
        "unstaged diff",
        parser=_parse_numstat_paths,
    )

    git_root_paths = committed | staged | unstaged

    # 4: untracked files — BEST-EFFORT (same Virtiofs concern).
    # ls-files returns paths relative to -C directory (base_path) → no prefix stripping.
    ls_paths = _run_best_effort(
        [*base_args, "ls-files", "--others", "--exclude-standard"],
        "untracked",
    )

    return _GitDirtyResult(_strip_prefix(git_root_paths) | ls_paths, unreliable_reason)


def _git_current_dirty(base_path: str, prefix: str) -> _GitDirtyResult:
    """Return files currently dirty (staged+unstaged+untracked), prefix-stripped.

    Returns a :class:`_GitDirtyResult`. When a command times out / fails the
    result's ``unreliable_reason`` is set so callers can avoid overwriting a
    previously-good dirty snapshot with a partial one and can raise the
    ``git_dirty_unreliable`` flag.
    """
    cmd = _git_base_cmd(base_path)
    if cmd is None:
        return _GitDirtyResult(set(), None)
    base_args = [*cmd, "-c", "core.quotePath=false"]
    best_effort_timeout = _git_dirty_timeout()
    unreliable_reason: str | None = None

    def _run(cmd: list[str], label: str, parser=_lines_to_set) -> set[str]:
        nonlocal unreliable_reason
        try:
            r = subprocess.run(cmd, **_GIT_SUBPROCESS_KW, timeout=best_effort_timeout)
            if r.returncode == 0:
                return parser(r.stdout)
            if unreliable_reason is None:
                unreliable_reason = f"{label} rc={r.returncode}"
        except subprocess.TimeoutExpired:
            if unreliable_reason is None:
                unreliable_reason = f"{label} timeout"
        except OSError:
            if unreliable_reason is None:
                unreliable_reason = f"{label} error"
        return set()

    # diff returns paths relative to git root → need prefix stripping.
    # "-- ." limits scope to base_path subtree (see _git_changed_files comment).
    # --numstat + --ignore-cr-at-eol: don't treat CRLF-vs-LF-only diffs as dirty
    # (see _parse_numstat_paths — --name-only would still list such files).
    diff_paths = _run(
        [
            *base_args,
            "diff",
            "--numstat",
            "--no-renames",
            "--no-ext-diff",
            "--no-textconv",
            "--ignore-cr-at-eol",
            "--cached",
            "--",
            ".",
        ],
        "staged dirty",
        parser=_parse_numstat_paths,
    )
    diff_paths |= _run(
        [
            *base_args,
            "diff",
            "--numstat",
            "--no-renames",
            "--no-ext-diff",
            "--no-textconv",
            "--ignore-cr-at-eol",
            "--",
            ".",
        ],
        "unstaged dirty",
        parser=_parse_numstat_paths,
    )
    # ls-files returns paths relative to -C directory → already correct
    ls_paths = _run([*base_args, "ls-files", "--others", "--exclude-standard"], "untracked dirty")

    stripped: set[str] = set()
    if prefix:
        pfx = prefix + "/"
        for p in diff_paths:
            if p.startswith(pfx):
                stripped.add(p[len(pfx) :])
    else:
        stripped = diff_paths

    return _GitDirtyResult(stripped | ls_paths, unreliable_reason)


# ---------------------------------------------------------------------------
# git grep — full-text search backend (opt-in: only when sources are under git)
# ---------------------------------------------------------------------------
def _git_grep_timeout() -> int:
    """Timeout (seconds) for a single ``git grep`` search.

    Configurable via ``RLM_GIT_SEARCH_TIMEOUT``. Defaults to 30s: ``git grep``
    over a full 1C config (tens of thousands of files) completes in well under a
    second, so 30s is a generous ceiling that still bounds a pathological regex
    or a frozen filesystem. Invalid values fall back to 30.
    """
    try:
        return max(1, int(os.environ.get("RLM_GIT_SEARCH_TIMEOUT", "30")))
    except (ValueError, TypeError):
        return 30


# Leading magic that turns a pathspec into a git "magic" pathspec (e.g. ``:/``
# escapes to repo root, ``:(top)`` likewise). User-supplied ``path`` must never
# carry these — see _sanitize_grep_path.
_PATHSPEC_MAGIC_PREFIXES = (":(", ":/", ":!", ":^")
_FILE_EXT_RE = re.compile(r"^[A-Za-z0-9_+-]+$")
_DRIVE_RE = re.compile(r"^[A-Za-z]:")
# git pathspec glob metacharacters — rejected in a user ``path`` so the filter
# stays a literal subtree/file and can't silently widen (e.g. ``CommonModules*``).
_PATH_GLOB_CHARS = frozenset("*?[]")


def _sanitize_grep_path(path: str | None) -> str | None:
    r"""Normalise a user-supplied ``path`` filter into a safe git pathspec base.

    Returns:
      * ``""`` — input was empty/unset → no path filter (search whole config).
      * a cleaned POSIX path (no leading/trailing ``/``) — valid directory/file
        filter.
      * ``None`` — input was **non-empty but malformed/unsafe**; caller must
        surface an error rather than silently widening the search to the whole
        config (or, worse, escaping ``base_path`` to the repo root).

    Internal backslashes are **normalised to ``/``** up-front (so
    ``CommonModules\Foo`` is accepted as ``CommonModules/Foo``), then the same
    checks apply as for POSIX input. A *leading* slash or backslash is rejected
    before normalisation: on Windows either roots the path at the current drive,
    and POSIX ``/x`` is absolute as well. None may be silently re-read as a
    relative config subtree. Rejected (→ ``None``):
    pathspec magic (``:/``, ``:(...)``, leading ``:``), rooted / UNC /
    network-absolute paths (``/CommonModules``, ``\CommonModules`` or
    ``\\server\share\X``), drive prefixes
    (``C:\...`` → ``C:/...`` → caught), ``..``/``.``
    segments (``a\..\b`` too), internal empty segments (``a//b``), glob
    NUL (не может быть частью пути ОС и отклоняется ``subprocess``), glob-
    metacharacters (``* ? [ ]`` — ``path`` is a **literal** subtree/file filter, so
    a glob would silently widen it past the documented contract), and inputs that
    collapse to empty after stripping surrounding slashes (``"/"``, ``"///"``) —
    the latter being non-empty intent that we refuse to reinterpret as "whole
    config".
    """
    raw = (path or "").strip()
    if not raw:
        return ""  # unset → no filter
    if "\x00" in raw:
        return None  # subprocess rejects embedded NUL; classify it as a bad filter, not a git failure
    if raw.startswith(("\\", "/")):
        # Both ``\CommonModules`` and ``/CommonModules`` express rooted input on
        # Windows. Normalising/stripping first would reinterpret either as the relative
        # subtree ``CommonModules``. This also rejects UNC and POSIX-absolute input;
        # the post-normalisation ``//`` guard remains as defence in depth.
        return None
    # Normalise Windows separators BEFORE the magic/drive/glob/'..' checks (#4).
    # A literal ``path=`` is often copied from a Windows path (``CommonModules\Foo``);
    # hard-rejecting any ``\`` made the adjacent replace dead code and surfaced an
    # error instead of filtering. ``C:\...`` still collapses to ``C:/...`` and is
    # caught by ``_DRIVE_RE``; ``a\..\b`` still trips the ``..`` segment guard.
    raw = raw.replace("\\", "/")
    if raw.startswith("//"):
        # UNC / network-absolute input (``\\server\share\X`` → ``//server/share/X``).
        # The strip('/') below would erase the absolute marker and silently re-read it
        # as the relative subtree ``server/share/X`` — a misleading empty search instead
        # of an error. Reject explicitly (checked AFTER the backslash normalisation so
        # ``\\``, ``//`` and mixed ``/\`` all collapse into this one guard).
        return None
    if raw.startswith(_PATHSPEC_MAGIC_PREFIXES) or raw.startswith(":"):
        return None
    cleaned = raw.strip("/")
    if not cleaned:
        return None  # non-empty input that was all slashes ("/", "///")
    if _DRIVE_RE.match(cleaned):
        return None  # absolute Windows path (C:/...)
    if any(c in _PATH_GLOB_CHARS for c in cleaned):
        return None  # glob metachar → would silently widen the literal filter
    for seg in cleaned.split("/"):
        if seg in ("", "..", "."):
            return None  # internal empty (a//b), parent escape, or '.'
        if seg.startswith(":"):
            return None
    return cleaned


def _sanitize_grep_file_types(file_types) -> list[str] | None:
    """Normalise a user-supplied ``file_types`` filter into a list of extensions.

    Accepts a comma-separated string (``"bsl,xml"``) or an iterable of strings.
    Returns:
      * ``[]`` — empty/unset → no type filter.
      * a list of bare extensions (no leading dot) — valid filter.
      * ``None`` — at least one extension is invalid (contains anything outside
        ``[A-Za-z0-9_+-]``). We reject the whole call rather than silently
        dropping the bad extension, because a silent drop could empty the filter
        and unexpectedly widen the search.
    """
    raw = file_types or ""
    if isinstance(raw, str):
        items = [t.strip() for t in raw.split(",")]
    else:
        items = [str(t).strip() for t in raw]
    items = [t for t in items if t]
    if not items:
        return []  # unset → no filter
    out: list[str] = []
    for t in items:
        if not _FILE_EXT_RE.match(t):
            return None  # invalid extension → error, no silent drop
        out.append(t)
    return out


def _sanitize_grep_excludes(exclude_path) -> list[str] | None:
    """Normalise a user-supplied ``exclude_path`` filter into a list of literal
    subtrees/files to drop from the search.

    Accepts a comma-separated string (``"Forms,Templates"``) or an iterable of
    strings. Returns:
      * ``[]`` — empty/unset → no exclusion.
      * a list of cleaned literal paths (no leading/trailing ``/``) — valid.
      * ``None`` — at least one element is malformed/unsafe. We reject the whole
        call rather than silently dropping the bad element, because a silent drop
        could widen the result set past what the caller asked to exclude.

    Each element runs through :func:`_sanitize_grep_path` (same safety rules:
    backslashes are normalised to ``/``; pathspec magic, drive prefixes,
    ``..``/``.``, glob metachars are all rejected). The input stays **literal** —
    glob is used only internally, by :func:`_git_grep`, to expand each name into
    an "at any depth" exclusion.
    """
    raw = exclude_path or ""
    if isinstance(raw, str):
        items = [t.strip() for t in raw.split(",")]
    else:
        items = [str(t).strip() for t in raw]
    items = [t for t in items if t]
    if not items:
        return []  # unset → no exclusion
    out: list[str] = []
    for it in items:
        cleaned = _sanitize_grep_path(it)  # reuse the same safety rules as ``path``
        if not cleaned:  # None (malformed) or "" (empty) — both invalid for exclude
            return None
        out.append(cleaned)
    return out


def _git_grep(
    base_path: str,
    pattern: str,
    *,
    path: str | None = None,
    file_types=None,
    exclude_path: str | None = None,
    literal_files: list[str] | None = None,
    regex: bool = False,
    ignore_case: bool = False,
    mode: str = "lines",
    max_results: int = 200,
    max_per_file: int = 50,
    include_truncation_sentinel: bool = False,
    timeout: int | None = None,
    err: dict | None = None,
) -> list[dict] | None:
    """Run a single ``git grep`` over the work tree at *base_path*.

    The one and only place that shells out to ``git grep``. Searches the working
    tree (tracked files as they are on disk, including uncommitted edits) plus
    untracked-but-not-ignored files (``--untracked``); ``.gitignore``'d paths are
    intentionally skipped.

    Parameters:
      * *pattern* — literal substring (default) or POSIX ERE when *regex* is True.
        ``-F``/``-E`` only; ``-P``/PCRE is avoided (optional git build feature).
      * *path* / *file_types* — scoped pathspec filter (composed as
        ``{path}/*.{ext}`` so multiple file types don't OR-leak outside *path*).
      * *exclude_path* — comma-separated list of **literal** directory/file names
        to drop from the search. Each name is expanded to an "at any depth"
        exclusion via ``:(glob,exclude)**/{name}/**`` (dir contents) plus
        ``:(glob,exclude)**/{name}`` (the node itself), so a nested ``Forms`` is
        excluded just like a top-level one. Applied only on the *path*/*file_types*
        (non-``literal_files``) branch; a malformed element → ``None`` (whole call
        fails rather than silently widening). When the positive scope is the whole
        config, an explicit ``.`` is added so git has a positive set to subtract from.
      * *literal_files* — a trusted, exact list of base-relative files (from the
        index) to restrict the search to; passed via ``:(literal)`` magic so a
        filename containing ``*`` or ``:`` can't act as a glob. When provided,
        *path*/*file_types* are ignored. An empty list returns ``[]`` (nothing to
        search) rather than widening to the whole config.
      * *mode* — ``"lines"`` → ``[{file, line, text}]``; ``"files"`` → ``[{file}]``.
      * *max_results* — hard cap on returned records.
      * *max_per_file* — per-file match cap (``-m``), anti-noise for heavy files
        (lines mode only).
      * *include_truncation_sentinel* — when True and the result was capped, the
        last element is ``{"_truncated": True, "shown": N}``. Kept False for
        ``safe_grep`` whose strict ``[{file, line, text}]`` contract internal
        callers rely on.

    Returns a list of dicts, or **``None``** on a real failure (git unavailable,
    rc≥2, timeout, or a malformed filter). ``None`` is distinct from ``[]`` (zero
    matches) so the caller can report "search failed" rather than "nothing found".

      * *err* — optional out-channel: on failure it is filled with the REASON
        (``{"kind": ...}``, plus ``rc``/``stderr`` for ``kind="rc"``). Without it a
        bare ``None`` collapses "git is down" and "git rejected YOUR pattern" into
        one outcome, and the caller can only GUESS which — that guess is exactly how
        a malformed regex ended up reported as a git failure. Guessing is also why a
        Python-side pre-check cannot close the gap: ``git grep -E`` speaks POSIX ERE,
        so ``(?=a)``/``(?P<x>a)`` compile fine in Python yet git exits 128 with
        "Invalid preceding regular expression". Let git say what is wrong — its own
        message names both the pattern and the reason. Populating *err* changes no
        return value, so existing callers are unaffected.
    """

    def _fail(kind: str, **extra) -> None:
        if err is not None:
            err.clear()
            err.update(kind=kind, **extra)
        return None

    if mode not in ("lines", "files"):
        return _fail("bad_mode", mode=mode)
    if not isinstance(pattern, str) or pattern == "":
        return _fail("bad_pattern", reason="empty")
    # Multi-pattern guard: a NL/NUL in the pattern makes git treat it as several
    # ``-e`` patterns (unexpected OR-search). Not supported in v1 → error.
    if "\n" in pattern or "\x00" in pattern:
        return _fail("bad_pattern", reason="newline_or_nul")

    cmd = _git_base_cmd(base_path)
    if cmd is None:
        return _fail("no_git")

    # Build pathspecs.
    if literal_files is not None:
        if not literal_files:
            return []  # explicit empty candidate set → nothing to search
        # literal_files is an exact, trusted set → exclude_path does not apply here
        # (it is sanitised/applied only on the path/file_types branch below).
        pathspecs = [f":(literal){f}" for f in literal_files]
    else:
        san_path = _sanitize_grep_path(path)
        if san_path is None:
            return _fail("bad_pathspec", arg="path")  # malformed path filter
        exts = _sanitize_grep_file_types(file_types)
        if exts is None:
            return _fail("bad_pathspec", arg="file_types")  # malformed file_types
        exclude_specs = _sanitize_grep_excludes(exclude_path)
        if exclude_specs is None:
            # malformed exclude filter → error, not silent widening
            return _fail("bad_pathspec", arg="exclude_path")
        if san_path and exts:
            pathspecs = [f"{san_path}/*.{ext}" for ext in exts]
        elif exts:
            pathspecs = [f"*.{ext}" for ext in exts]
        elif san_path:
            pathspecs = [san_path]
        else:
            pathspecs = []  # whole config
        # Exclude zones: literal names expanded to "any depth" via glob pathspec.
        # A magic-free ``:(exclude)Forms`` anchors at the repo root and would NOT
        # drop a nested ``*/Forms/*``; ``:(glob,exclude)**/Forms/**`` does. Git's
        # exclude magic only subtracts from a positive set, so when the scope is
        # the whole config (no positive pathspec) add an explicit ``.`` first.
        if exclude_specs:
            if not pathspecs:
                pathspecs = ["."]
            for e in exclude_specs:
                pathspecs.append(f":(glob,exclude)**/{e}/**")  # dir contents, any depth
                pathspecs.append(f":(glob,exclude)**/{e}")  # file/dir node, any depth

    output_flag = "-l" if mode == "files" else "-n"
    grep_cmd = [*cmd, "grep", "-z", "--untracked", "--no-color", "-I", output_flag]
    if ignore_case:
        grep_cmd.append("-i")
    if mode == "lines" and max_per_file and max_per_file > 0:
        grep_cmd += ["-m", str(max_per_file)]
    grep_cmd.append("-E" if regex else "-F")
    grep_cmd += ["-e", pattern, "--", *pathspecs]

    t = timeout if timeout is not None else _git_grep_timeout()
    try:
        r = subprocess.run(grep_cmd, **_GIT_SUBPROCESS_KW, timeout=t)
    except (subprocess.TimeoutExpired, OSError, ValueError) as exc:
        logger.info("_git_grep: %s: %s", type(exc).__name__, exc)
        kind = "timeout" if isinstance(exc, subprocess.TimeoutExpired) else "spawn_failed"
        return _fail(kind, detail=f"{type(exc).__name__}: {exc}")

    rc = r.returncode
    if rc == 1:
        return []  # no matches — not an error
    if rc != 0:
        # git ЗАПУСТИЛСЯ и сам объяснил, что не так (битый ERE → rc=128 + "Invalid preceding
        # regular expression" / "unmatched ( for expression group"). Его вердикт точнее любой
        # эвристики на нашей стороне: он называет и pattern, и причину.
        logger.info("_git_grep: rc=%d stderr=%r", rc, (r.stderr or "")[:200])
        return _fail("rc", rc=rc, stderr=(r.stderr or "")[:500])

    stdout = r.stdout or ""
    results: list[dict] = []
    if mode == "files":
        # ``-l -z`` → NUL-separated paths (printed verbatim).
        for f in stdout.split("\x00"):
            if f:
                results.append({"file": f})
    else:
        # ``-n -z`` → records ``path \0 lineno \0 text`` separated by the file's
        # line terminator. Split on ``\n``; ``.strip()`` removes the trailing
        # ``\r`` left by CRLF files (and any surrounding whitespace).
        for record in stdout.split("\n"):
            if not record:
                continue
            parts = record.split("\x00", 2)
            if len(parts) != 3:
                continue
            f, lineno, text = parts
            try:
                ln = int(lineno)
            except ValueError:
                continue
            results.append({"file": f, "line": ln, "text": text.strip()})

    if len(results) > max_results:
        results = results[:max_results]
        if include_truncation_sentinel:
            results.append({"_truncated": True, "shown": max_results})
    return results


# ---------------------------------------------------------------------------
# IndexStatus enum
# ---------------------------------------------------------------------------
class IndexStatus(Enum):
    """Result of freshness check for an existing method index."""

    FRESH = "fresh"
    STALE = "stale"
    STALE_AGE = "stale_age"
    STALE_CONTENT = "stale_content"
    MISSING = "missing"


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------
def get_index_dir_root() -> Path:
    """Return the root directory under which per-project BSL index folders live.

    Precedence (mirrors :func:`rlm_tools_bsl.cache._cache_base`):

    1. ``RLM_INDEX_DIR`` set → that path verbatim (explicit user override).
    2. ``RLM_CONFIG_FILE`` set → ``dirname(RLM_CONFIG_FILE)/index``. Fixes the
       Windows-service LocalSystem case where ``Path.home()`` resolves to
       ``system32/config/systemprofile``. Index lives under a dedicated
       ``index/`` subdir (not under ``cache/``) so cache cleanup never touches
       index data.
    3. Fallback → ``~/.cache/rlm-tools-bsl``.

    Note: BSL index directories (containing ``bsl_index.db``) are **not**
    subject to automatic cleanup by ``cleanup_stale_cache()``. Indexes are
    expensive to build and are managed manually via ``rlm_index(action='drop')``.
    """
    env_dir = os.environ.get("RLM_INDEX_DIR")
    if env_dir:
        return Path(env_dir)
    config_override = os.environ.get("RLM_CONFIG_FILE")
    if config_override:
        return Path(config_override).parent / "index"
    return Path.home() / ".cache" / "rlm-tools-bsl"


def get_index_dir(base_path: str) -> Path:
    """Return the directory for storing BSL indexes.

    Resolves through :func:`get_index_dir_root` — see its docstring for the
    full precedence chain (RLM_INDEX_DIR → dirname(RLM_CONFIG_FILE)/index →
    home fallback). Index storage is never auto-cleaned.
    """
    return get_index_dir_root()


def migrate_legacy_index_root() -> int:
    """One-shot: migrate per-hash index subdirs from the legacy root.

    When ``RLM_CONFIG_FILE`` is set (Windows-service LocalSystem case fixed in
    v1.9.2), :func:`get_index_dir_root` returns a path different from the
    legacy ``~/.cache/rlm-tools-bsl``. Already-built indexes still live there
    and would otherwise be silently abandoned. This helper moves each subdir
    that contains ``bsl_index.db`` or ``method_index.db`` to the new root.

    Triggered only when ``RLM_INDEX_DIR`` is unset (otherwise the user
    explicitly chose a path and we do not touch anything). Idempotent:
    repeated calls are NOOP because the legacy dir is empty after the first
    successful run, or the target already exists.

    Returns the number of subdirectories successfully moved.
    """
    if os.environ.get("RLM_INDEX_DIR"):
        return 0

    legacy_root = Path.home() / ".cache" / "rlm-tools-bsl"
    new_root = get_index_dir_root()

    try:
        if legacy_root.resolve() == new_root.resolve():
            return 0
    except OSError:
        if legacy_root == new_root:
            return 0

    if not legacy_root.exists() or not legacy_root.is_dir():
        return 0

    moved = 0
    try:
        entries = list(legacy_root.iterdir())
    except OSError as exc:
        logger.warning("migrate_legacy_index_root: cannot list %s: %s", legacy_root, exc)
        return 0

    for sub in entries:
        try:
            if not sub.is_dir():
                continue
            has_index = (sub / "bsl_index.db").exists() or (sub / "method_index.db").exists()
            if not has_index:
                continue
            target = new_root / sub.name
            if target.exists():
                logger.warning(
                    "migrate_legacy_index_root: target already exists, skipping: %s",
                    target,
                )
                continue
            new_root.mkdir(parents=True, exist_ok=True)
            try:
                sub.rename(target)
            except PermissionError as exc:
                logger.warning(
                    "migrate_legacy_index_root: permission denied, skipping %s: %s",
                    sub,
                    exc,
                )
                continue
            except OSError as exc:
                # Cross-device rename (errno 18 on POSIX, 17 on some Windows
                # configurations) → fall back to copy+remove via shutil.move.
                if exc.errno in (errno.EXDEV, 17):
                    try:
                        shutil.move(str(sub), str(target))
                    except Exception as exc2:
                        logger.warning(
                            "migrate_legacy_index_root: shutil.move failed for %s -> %s: %s",
                            sub,
                            target,
                            exc2,
                        )
                        continue
                else:
                    logger.warning(
                        "migrate_legacy_index_root: rename failed for %s -> %s: %s",
                        sub,
                        target,
                        exc,
                    )
                    continue
            logger.warning(
                "migrate_legacy_index_root: moved %s -> %s",
                sub,
                target,
            )
            moved += 1
        except Exception as exc:
            logger.warning(
                "migrate_legacy_index_root: unexpected error for %s: %s",
                sub,
                exc,
            )
            continue

    return moved


def _migrate_old_index_db(index_dir: Path) -> str:
    """One-time rename method_index.db → bsl_index.db (+ lock).

    Returns the actual DB filename to use ("bsl_index.db" or "method_index.db" as fallback).
    """
    new_db = index_dir / "bsl_index.db"
    old_db = index_dir / "method_index.db"
    if new_db.exists():
        return "bsl_index.db"
    if not old_db.exists():
        return "bsl_index.db"  # new installs
    try:
        old_db.rename(new_db)
    except OSError:
        # Another process may have already renamed it
        if new_db.exists():
            return "bsl_index.db"
        return "method_index.db"  # rename genuinely failed — use old file as-is
    # migrate lock file too
    old_lock = index_dir / "method_index.lock"
    new_lock = index_dir / "bsl_index.lock"
    if old_lock.exists():
        try:
            old_lock.rename(new_lock)
        except OSError:
            pass
    return "bsl_index.db"


def get_index_db_path(base_path: str) -> Path:
    """Return the full path to the index DB for a given base_path."""
    h = hashlib.md5(base_path.encode()).hexdigest()[:12]
    index_dir = get_index_dir(base_path) / h
    db_name = _migrate_old_index_db(index_dir)
    return index_dir / db_name


# ---------------------------------------------------------------------------
# Freshness check
# ---------------------------------------------------------------------------
def _read_index_meta(db_path: Path) -> dict[str, str] | None:
    """Read index_meta table from SQLite. Returns None on any error."""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error:
        return None
    try:
        meta: dict[str, str] = {}
        for row in conn.execute("SELECT key, value FROM index_meta"):
            meta[row["key"]] = row["value"]
        return meta
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def _meta_incomplete(meta: dict[str, str]) -> bool:
    """True ⇔ index_meta marks an UNFINISHED in-place build (``build_in_progress == "1"``).

    Strict ``== "1"`` (three states: key absent / ``"0"`` / ``"1"``): ``_read_index_meta``
    returns string values, so a naive ``bool(meta.get("build_in_progress"))`` would make
    ``"0"`` truthy and strand the index MISSING forever (codex round 13)."""
    return meta.get("build_in_progress") == "1"


def index_incomplete(db_path: str | Path) -> bool:
    """True ⇔ db_path is a partially-built (in-place) index that must not be used.

    Light read of ``index_meta`` only (no full IndexReader). None-safe (cross-review
    round 26): a missing / corrupt / locked db → ``_read_index_meta`` returns ``None`` →
    ``False`` here (existence/validity is the caller's own MISSING concern; without this
    guard ``_meta_incomplete(None)`` would ``AttributeError`` at no-stats / CLI info)."""
    meta = _read_index_meta(Path(db_path))
    return meta is not None and _meta_incomplete(meta)


def _check_age(meta: dict[str, str]) -> IndexStatus | None:
    """Return STALE_AGE if index exceeds max age, else None."""
    max_age_days = int(os.environ.get("RLM_INDEX_MAX_AGE_DAYS", "7"))
    built_at = meta.get("built_at")
    if built_at is not None:
        age_days = (time.time() - float(built_at)) / 86400
        if age_days > max_age_days:
            return IndexStatus.STALE_AGE
    return None


def _check_content_sample(db_path: Path, base_path: str) -> IndexStatus | None:
    """Sample random modules and compare mtime+size. Returns STALE_CONTENT or None."""
    sample_size = int(os.environ.get("RLM_INDEX_SAMPLE_SIZE", "5"))
    sample_threshold = int(os.environ.get("RLM_INDEX_SAMPLE_THRESHOLD", "30"))

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error:
        return None

    try:
        row = conn.execute("SELECT COUNT(*) AS cnt FROM modules").fetchone()
        total_modules = row["cnt"] if row else 0

        if total_modules < sample_threshold:
            return None

        rows = conn.execute(
            "SELECT rel_path, mtime, size FROM modules ORDER BY RANDOM() LIMIT ?",
            (sample_size,),
        ).fetchall()
    except sqlite3.Error:
        return None
    finally:
        conn.close()

    if not rows:
        return None

    base = Path(base_path)

    def _stat_check(r: sqlite3.Row) -> bool:
        """Return True if mismatch detected."""
        full_path = base / r["rel_path"]
        try:
            st = full_path.stat()
            return abs(st.st_mtime - r["mtime"]) > 1.0 or st.st_size != r["size"]
        except OSError:
            return True

    if len(rows) > 1:
        from concurrent.futures import ThreadPoolExecutor as _TP

        with _TP(max_workers=min(5, len(rows))) as pool:
            results = list(pool.map(_stat_check, rows))
        mismatches = sum(results)
    else:
        mismatches = 1 if _stat_check(rows[0]) else 0

    if mismatches > max(1, len(rows) // 5):
        return IndexStatus.STALE_CONTENT
    return None


def check_index_usable(
    db_path: str | Path,
    base_path: str,
) -> IndexStatus:
    """Lightweight freshness check for rlm_start (no rglob needed).

    Checks:
      1. File exists
      2. Age: RLM_INDEX_MAX_AGE_DAYS (default 7)
      3. Content sampling: random mtime+size on a small sample (default 5),
         skipped if index is younger than RLM_INDEX_SKIP_SAMPLE_HOURS (default 24)

    Structural drift (files added/removed) is NOT checked here — use
    check_index_strict() or compare format_info.bsl_file_count with
    index_meta bsl_count separately.
    """
    db_path = Path(db_path)
    if not db_path.exists():
        return IndexStatus.MISSING

    meta = _read_index_meta(db_path)
    if meta is None:
        return IndexStatus.MISSING

    # --- Incomplete in-place build → strictly MISSING (codex round 10) ---
    # rlm_start opens a reader for FRESH/STALE_AGE/STALE_CONTENT but NOT for MISSING,
    # so a partial DB must read as MISSING (not STALE_*). `meta` is already in hand →
    # _meta_incomplete directly, no second connection (round 25). Checked FIRST.
    if _meta_incomplete(meta):
        return IndexStatus.MISSING

    # --- Age check ---
    age_status = _check_age(meta)
    if age_status is not None:
        return age_status

    # --- Skip sampling for young indexes ---
    skip_hours = int(os.environ.get("RLM_INDEX_SKIP_SAMPLE_HOURS", "24"))
    built_at = meta.get("built_at")
    if built_at is not None:
        age_hours = (time.time() - float(built_at)) / 3600
        if age_hours < skip_hours:
            return IndexStatus.FRESH

    # --- Content sampling (parallel stat) ---
    content_status = _check_content_sample(db_path, base_path)
    if content_status is not None:
        return content_status

    return IndexStatus.FRESH


def check_index_strict(
    db_path: str | Path,
    current_bsl_count: int,
    current_paths_hash: str,
    base_path: str,
) -> IndexStatus:
    """Full freshness check for CLI ``index info`` (requires rglob data).

    Checks:
      1. File exists
      2. Structural match: bsl_count + paths_hash
      3. Age: RLM_INDEX_MAX_AGE_DAYS (default 7)
      4. Content sampling: random mtime+size checks on a sample of files
    """
    db_path = Path(db_path)
    if not db_path.exists():
        return IndexStatus.MISSING

    meta = _read_index_meta(db_path)
    if meta is None:
        return IndexStatus.MISSING

    # --- Incomplete in-place build → MISSING ---
    # A mid-fail AFTER _write_meta could have written built_at/bsl_count/paths_hash;
    # without this guard strict would mistake the partial build for FRESH.
    if _meta_incomplete(meta):
        return IndexStatus.MISSING

    # --- Structural check ---
    stored_count = meta.get("bsl_count")
    stored_hash = meta.get("paths_hash")
    if stored_count is None or stored_hash is None:
        return IndexStatus.STALE

    if int(stored_count) != current_bsl_count or stored_hash != current_paths_hash:
        return IndexStatus.STALE

    # --- Age check ---
    age_status = _check_age(meta)
    if age_status is not None:
        return age_status

    # --- Content sampling ---
    content_status = _check_content_sample(db_path, base_path)
    if content_status is not None:
        return content_status

    return IndexStatus.FRESH


# Backward-compatible alias
check_index_freshness = check_index_strict


# ---------------------------------------------------------------------------
# Internal parsing helpers
# ---------------------------------------------------------------------------
def _scan_module(lines: list[str]):
    """Yield ``(lineno, code, strings)`` for each line, tracking string state.

    A single multi-line-aware pass over the module body that separates real code
    from string-literal content:

      - ``code``    — the line with string-literal content AND comments removed
                      (multi-line literals opened on a previous line contribute
                      nothing to ``code`` here — fixes false ``manager`` matches
                      on continuation lines like ``|Документы.X``);
      - ``strings`` — the list of string-literal fragments on this line (each
                      multi-line literal contributes one fragment per physical
                      line, so a matched metadata path keeps its real line).

    ``""`` inside a string is an escaped quote, not a terminator; ``//`` starts a
    comment only outside of a string.
    """
    in_string = False
    for lineno, raw in enumerate(lines, start=1):
        # v1.33.0: комментарий между строками-продолжениями многострочного литерала
        # 1С допускает, и состояние он НЕ меняет. Без этого правила закомментированный
        # конец запроса (`//|  Поле";`) закрывал бы литерал, и разбор рассинхронизировался
        # бы до конца модуля. См. mask_comments_and_strings в bsl_knowledge.
        if in_string and raw.lstrip().startswith("//"):
            yield lineno, "", []
            continue
        n = len(raw)
        # Быстрый путь: строка ВНЕ литерала, где нет кавычек, — это чистый код
        # (с возможным хвостом-комментарием). Ни одного посимвольного шага и ни
        # одной склейки. Так идёт большинство строк, и именно это делает скан
        # дешёвым на ЕРП-масштабе.
        if not in_string:
            q = raw.find('"')
            c = raw.find("//")
            if q < 0:
                yield lineno, (raw if c < 0 else raw[:c]), []
                continue
            if 0 <= c < q:  # комментарий начинается раньше литерала
                yield lineno, raw[:c], []
                continue

        # Медленный путь — та же машина состояний, но прыжками по str.find и
        # срезами вместо символ-за-символом (семантика идентична посимвольной
        # версии, покрыта тестом эквивалентности на боевых исходниках).
        i = 0
        code_parts: list[str] = []
        strings: list[str] = []
        seg_start = 0 if in_string else -1
        while i < n:
            if in_string:
                j = raw.find('"', i)
                if j < 0:
                    break  # литерал продолжается на следующей строке
                if j + 1 < n and raw[j + 1] == '"':
                    i = j + 2  # экранированная "" — остаёмся внутри литерала
                    continue
                strings.append(raw[seg_start:j])
                in_string = False
                seg_start = -1
                i = j + 1
            else:
                q = raw.find('"', i)
                c = raw.find("//", i)
                if q < 0 and c < 0:
                    code_parts.append(raw[i:])
                    break
                if q >= 0 and (c < 0 or q < c):
                    code_parts.append(raw[i:q])
                    in_string = True
                    seg_start = q + 1
                    i = q + 1
                else:
                    code_parts.append(raw[i:c])
                    break  # комментарий — остаток строки не код и не литерал
        if in_string:
            # литерал продолжается на следующей строке — отдаём накопленное здесь
            strings.append(raw[seg_start:])
        yield lineno, "".join(code_parts), strings


def _extract_code_usages(
    lines: list[str],
) -> list[tuple[str, str | None, str, int]]:
    """Extract reverse code-usages of metadata objects from a .bsl module body.

    Source-aware light regex layer (see plan / docs/INDEXING.md). A single
    multi-line-aware scan (``_scan_module``) separates code from string content:
      - ``manager``  — collection accessor `Документы.X` / `Documents.X`, matched
        only on real *code* (string literals and comments removed, incl. across
        multi-line string literals).
      - ``ref_type`` — reference type in a *string literal*:
        `"ДокументСсылка.X"` (RU) or `"DocumentRef.X"` (EN, via canonicalize_type_ref).
      - ``query``    — metadata path in a *string literal* (query text):
        `Документ.X` and `Документ.X.Товары` (3rd segment = tabular section name).
        Multi-line query literals are covered.

    Returns list of ``(object_ref, member_path, usage_kind, line)`` where
    ``object_ref`` is canonical (e.g. ``Document.ПриобретениеТоваровУслуг``),
    ``member_path`` is the tabular-section name for ``query`` (else ``None``).
    Does NOT resolve local-variable types (`Док.Товары` is out of scope).
    """
    out: list[tuple[str, str | None, str, int]] = []

    for lineno, code, strings in _scan_module(lines):
        # manager — only on real code (no strings/comments, multiline-aware)
        if "." in code:
            for m in _CODE_DOTTED_RE.finditer(code):
                canonical = _CODE_MANAGER_COLLECTIONS.get(m.group(1).lower())
                if canonical:
                    out.append((canonical + m.group(2), None, "manager", lineno))

        # ref_type / query — only inside string-literal content
        for content in strings:
            if "." not in content:
                continue
            for pm in _CODE_PATH_RE.finditer(content):
                g1, g2, g3 = pm.group(1), pm.group(2), pm.group(3)
                low = g1.lower()
                ru_ref = _RU_REFTYPE_TO_CANONICAL.get(low + ".")
                if ru_ref:
                    out.append((ru_ref + g2, None, "ref_type", lineno))
                    continue
                query_canon = _CODE_QUERY_COLLECTIONS.get(low)
                if query_canon:
                    out.append((query_canon + g2, g3 or None, "query", lineno))
                    continue
                # BUILD keeps the pre-1.28 case-sensitive extraction contract.  Public
                # lookup canonicalization is intentionally case-insensitive, but widening
                # persisted code usages requires its own builder-version migration.
                en_ref = canonicalize_type_ref(f"{g1}.{g2}", fold_case=False)
                if en_ref:
                    out.append((en_ref, None, "ref_type", lineno))
    return out


def _chunked(seq: list, size: int = 500):
    """Yield ``seq`` in chunks of at most ``size`` (SQLite variable-limit safe)."""
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def _parse_procedures_from_lines(lines: list[str]) -> list[dict]:
    """Parse procedure/function definitions from a list of lines.

    Multi-line signatures (``Процедура X(a, b,\n    c, d)``) are folded into a
    single logical line before the BSL_PATTERNS["procedure_def"] regex runs.
    ``end_line`` is computed from the ORIGINAL line list — ``КонецПроцедуры``
    always sits alone on its own row.

    Returns list of dicts: {name, type, line, end_line, is_export, params, loc}.
    Здесь ``params`` — СЫРАЯ строка сигнатуры (build-time, хранится в TEXT-колонке).
    На helper-границе ``_split_params`` превращает её в list[str] для агента (v1.18.0).

    v1.33.0: и матч сигнатуры, и поиск ``КонецПроцедуры``, и решение о склейке идут
    по МАСКЕ (комментарии и содержимое строковых литералов погашены), а ``params``
    вырезается из ОРИГИНАЛА по смещениям маски — она посимвольно той же длины,
    поэтому строковый дефолт (``Знач Адрес = "http://x/y"``) сохраняется дословно.
    """
    masked = mask_comments_and_strings(lines)
    merged_lines, merged_masked, line_map = _merge_proc_continuations_with_mask(lines, masked)
    total_merged = len(merged_lines)
    total_orig = len(lines)

    procedures: list[dict] = []
    m_idx = 0
    while m_idx < total_merged:
        m = _PROC_DEF_RE.search(merged_masked[m_idx])
        if not m:
            m_idx += 1
            continue

        original = merged_lines[m_idx]
        proc_type = m.group(1)
        proc_name = m.group(2)
        # params — из ОРИГИНАЛА по смещениям маски (см. докстринг).
        params = original[m.start(3) : m.end(3)].strip() if m.group(3) is not None else ""
        is_export = m.group(4) is not None and m.group(4).strip() != ""
        line_number = line_map[m_idx]  # 1-based original line

        # Body starts at the original line right after the merged signature row.
        next_start = line_map[m_idx + 1] if m_idx + 1 < total_merged else total_orig + 1
        scan_from = next_start - 1  # 0-based

        end_line: int | None = None
        for orig_idx in range(scan_from, total_orig):
            # По маске: строковый литерал "КонецПроцедуры" в начале строки обманул бы
            # заякоренный _PROC_END_RE и оборвал тело раньше времени.
            if _PROC_END_RE.search(masked[orig_idx]):
                end_line = orig_idx + 1
                break

        if end_line is None:
            current = {
                "name": proc_name,
                "type": proc_type,
                "line": line_number,
                "is_export": is_export,
                "end_line": total_orig,
                "params": params,
                "loc": total_orig - line_number + 1,
            }
            procedures.append(current)
            break

        current = {
            "name": proc_name,
            "type": proc_type,
            "line": line_number,
            "is_export": is_export,
            "end_line": end_line,
            "params": params,
            "loc": end_line - line_number + 1,
        }
        procedures.append(current)

        # Advance m_idx past the КонецПроцедуры line.
        # Find the merged index whose line_map start > end_line.
        new_m = m_idx + 1
        while new_m < total_merged and line_map[new_m] <= end_line:
            new_m += 1
        m_idx = new_m

    return procedures


def _extract_calls_from_body(
    lines: list[str],
    start_line: int,
    end_line: int,
) -> list[tuple[str, int]]:
    """Extract call targets from method body lines.

    Args:
        lines: All lines of the file (0-indexed).
        start_line: 1-based start (definition line, skipped).
        end_line: 1-based end (EndProcedure line, skipped).

    Returns:
        List of (callee_name, line_number_1based).
    """
    calls: list[tuple[str, int]] = []

    # Body line range (0-based indices): skip the definition line and the
    # EndProcedure/EndFunction line.
    body_start = start_line  # 0-based index of the first body line (def skipped)
    body_end = min(end_line - 1, len(lines))  # skip EndProcedure line
    if body_start >= body_end:
        return calls

    # Use the multi-line-aware scanner instead of a per-line strip: ``code`` has
    # string literals (incl. multi-line query texts opened with a continuation
    # ``|``) and comments already removed across line boundaries. This stops
    # query-language functions like ЕСТЬNULL(/ВЫРАЗИТЬ(/СУММА( inside multi-line
    # string literals from leaking in as false call edges (a per-line strip
    # cannot see that a continuation line is still inside a string).
    #
    # Line numbering: _scan_module yields 1-based ``lineno`` relative to the
    # slice ``lines[body_start:body_end]``; the absolute 1-based line number of
    # the original file is ``body_start + lineno``.
    for lineno, code, _strings in _scan_module(lines[body_start:body_end]):
        if not code.strip():
            continue

        line_number = body_start + lineno  # 1-based absolute

        seen_on_line: set[str] = set()

        # Qualified calls first: Module.Method(
        for qm in _QUALIFIED_CALL_RE.finditer(code):
            module_part = qm.group(1)
            method_part = qm.group(2)
            if module_part.lower() in _BSL_KEYWORDS_LOWER:
                continue
            if method_part.lower() in _BSL_KEYWORDS_LOWER:
                continue
            callee = f"{module_part}.{method_part}"
            if callee not in seen_on_line:
                seen_on_line.add(callee)
                calls.append((callee, line_number))

        # Simple calls: FunctionName(
        for sm in _SIMPLE_CALL_RE.finditer(code):
            func_name = sm.group(1)
            if func_name.lower() in _BSL_KEYWORDS_LOWER:
                continue
            # Drop bare calls to reserved platform globals (Сообщить/НСтр/Новый
            # Структура(/…): permanently unresolved navigation noise. Applied
            # ONLY here, never to the qualified Module.Method branch above.
            if func_name.casefold() in _BSL_GLOBAL_FUNCS_LOWER:
                continue
            # Skip if already captured as part of a qualified call on this line
            # (the simple regex also matches the method part of Module.Method)
            if func_name not in seen_on_line:
                # Check this isn't the method part of a qualified call
                start_pos = sm.start()
                if start_pos > 0 and code[start_pos - 1] == ".":
                    continue
                seen_on_line.add(func_name)
                calls.append((func_name, line_number))

    return calls


# ---------------------------------------------------------------------------
# Configuration XML parsing (Level 1 metadata)
# ---------------------------------------------------------------------------
def _parse_configuration_meta(base_path: str) -> dict[str, str]:
    """Extract top-level config metadata from Configuration.xml (CF) or Configuration.mdo (EDT).

    Returns dict with keys: config_name, config_synonym, config_version,
    config_vendor, source_format, config_role.
    """
    import xml.etree.ElementTree as ET

    from rlm_tools_bsl.format_detector import SourceFormat, detect_format

    base = Path(base_path)
    fmt_info = detect_format(base_path)
    # detect_format returns FormatInfo; extract the primary_format enum value
    if hasattr(fmt_info, "primary_format"):
        fmt_str = fmt_info.primary_format.value
    elif isinstance(fmt_info, SourceFormat):
        fmt_str = fmt_info.value
    else:
        fmt_str = str(fmt_info)
    meta: dict[str, str] = {"source_format": fmt_str}

    # Store shallow bsl_file_count from detect_format (fast glob, not rglob)
    if hasattr(fmt_info, "bsl_file_count"):
        meta["shallow_bsl_count"] = str(fmt_info.bsl_file_count)

    # Store has_configuration_xml flag
    meta["has_configuration_xml"] = "1" if (base / "Configuration.xml").is_file() else "0"

    # Try CF format: Configuration.xml in root
    cf_xml = base / "Configuration.xml"
    mdo_xml = base / "Configuration" / "Configuration.mdo"

    ns_cf = {
        "md": "http://v8.1c.ru/8.3/MDClasses",
        "v8": "http://v8.1c.ru/8.1/data/core",
    }

    def _cf_text(props, tag: str) -> str:
        el = props.find(f"md:{tag}", ns_cf)
        return (el.text or "").strip() if el is not None else ""

    def _cf_synonym(props) -> str:
        syn_el = props.find("md:Synonym", ns_cf)
        if syn_el is None:
            return ""
        for item in syn_el.findall("v8:item", ns_cf):
            lang = item.find("v8:lang", ns_cf)
            content = item.find("v8:content", ns_cf)
            if lang is not None and content is not None and lang.text == "ru":
                return (content.text or "").strip()
        # Fallback to first item
        for item in syn_el.findall("v8:item", ns_cf):
            content = item.find("v8:content", ns_cf)
            if content is not None and content.text:
                return content.text.strip()
        return ""

    if cf_xml.is_file():
        try:
            tree = ET.parse(str(cf_xml))
            root = tree.getroot()
            # Find <Configuration><Properties>
            cfg_el = root.find("md:Configuration", ns_cf)
            if cfg_el is not None:
                props = cfg_el.find("md:Properties", ns_cf)
                if props is not None:
                    meta["config_name"] = _cf_text(props, "Name")
                    meta["config_synonym"] = _cf_synonym(props)
                    meta["config_version"] = _cf_text(props, "Version")
                    meta["config_vendor"] = _cf_text(props, "Vendor")
                    ext_el = props.find("md:ConfigurationExtensionPurpose", ns_cf)
                    if ext_el is not None and ext_el.text:
                        meta["config_role"] = "extension"
                        meta["extension_purpose"] = ext_el.text.strip()
                    else:
                        meta["config_role"] = "base"
                    meta["extension_prefix"] = _cf_text(props, "NamePrefix")
        except (ET.ParseError, OSError):
            pass
    elif mdo_xml.is_file():
        try:
            tree = ET.parse(str(mdo_xml))
            root = tree.getroot()

            def _mdo_text(tag: str) -> str:
                for ch in root:
                    local = ch.tag.split("}")[-1] if "}" in ch.tag else ch.tag
                    if local == tag and ch.text:
                        return ch.text.strip()
                return ""

            def _mdo_synonym() -> str:
                for ch in root:
                    local = ch.tag.split("}")[-1] if "}" in ch.tag else ch.tag
                    if local == "synonym":
                        # Look for <v8:item> children
                        for item in ch:
                            item_local = item.tag.split("}")[-1] if "}" in item.tag else item.tag
                            if item_local == "item":
                                lang_el = None
                                content_el = None
                                for sub in item:
                                    sl = sub.tag.split("}")[-1] if "}" in sub.tag else sub.tag
                                    if sl == "lang":
                                        lang_el = sub
                                    elif sl == "content":
                                        content_el = sub
                                if lang_el is not None and content_el is not None:
                                    if lang_el.text == "ru":
                                        return (content_el.text or "").strip()
                        # Fallback: text content directly
                        if ch.text and ch.text.strip():
                            return ch.text.strip()
                return ""

            meta["config_name"] = _mdo_text("name")
            meta["config_synonym"] = _mdo_synonym()
            meta["config_version"] = _mdo_text("version")
            meta["config_vendor"] = _mdo_text("vendor")
            ext = _mdo_text("configurationExtensionPurpose")
            if ext:
                meta["config_role"] = "extension"
                meta["extension_purpose"] = ext
            else:
                meta["config_role"] = "base"
            meta["extension_prefix"] = _mdo_text("namePrefix")
        except (ET.ParseError, OSError):
            pass

    return meta


# ---------------------------------------------------------------------------
# Level-2 metadata collection (ES, SJ, FO)
# ---------------------------------------------------------------------------
def _collect_metadata_tables(
    base_path: str,
    *,
    collect_es: bool = True,
    collect_sj: bool = True,
    collect_fo: bool = True,
    collect_enums: bool = True,
    collect_subs: bool = True,
    collect_http: bool = True,
    collect_ws: bool = True,
    collect_xdto: bool = True,
    collect_attrs_categories: set[str] | None = None,
    collect_exchange_plans: bool = True,
    collect_defined_types: bool = True,
    collect_pvh_types: bool = True,
    collect_metadata_refs_categories: set[str] | None = None,
) -> dict[str, list[tuple]]:
    """Scan and parse metadata XMLs selectively.

    By default (all flags True, collect_attrs_categories=None) collects everything.
    When *collect_attrs_categories* is ``None`` all ``_ATTR_CATEGORIES`` are scanned;
    when it is an empty ``set()`` object_attributes/predefined_items are skipped;
    when it contains specific categories only those are scanned.

    *collect_metadata_refs_categories* — same pattern for metadata_references rows;
    None means full collection (all known top-level categories).

    Returns dict with keys: event_subscriptions, scheduled_jobs, functional_options,
    enum_values, subsystem_content, http_services, web_services, xdto_packages,
    object_attributes, predefined_items, metadata_references, exchange_plan_content,
    defined_types, characteristic_types.
    Each value is a list of tuples ready for INSERT.
    """
    from rlm_tools_bsl.bsl_xml_parsers import (
        canonicalize_type_ref,
        normalize_type_string,
        parse_command_parameter_type,
        parse_defined_type,
        parse_enum_xml,
        parse_event_subscription_xml,
        parse_exchange_plan_content,
        parse_functional_option_xml,
        parse_http_service_xml,
        parse_metadata_xml,
        parse_predefined_items,
        parse_pvh_characteristics,
        parse_scheduled_job_xml,
        parse_web_service_xml,
        parse_xdto_package_xml,
        parse_xdto_types,
    )

    base = Path(base_path)
    result: dict[str, list[tuple]] = {
        "event_subscriptions": [],
        "scheduled_jobs": [],
        "functional_options": [],
        "enum_values": [],
        "subsystem_content": [],
        "http_services": [],
        "web_services": [],
        "xdto_packages": [],
        "object_attributes": [],
        "predefined_items": [],
        "metadata_references": [],
        "exchange_plan_content": [],
        "defined_types": [],
        "characteristic_types": [],
    }

    # Active set of source_categories for metadata_references rows.
    # None means: include everything from triggered set.
    _active_ref_cats: set[str] | None = collect_metadata_refs_categories

    def _ref_allowed(cat: str) -> bool:
        if _active_ref_cats is None:
            return cat in _METADATA_REFERENCES_TRIGGER_CATEGORIES
        return cat in _active_ref_cats

    def _emit_refs(
        parsed_refs: list[dict],
        source_object: str,
        source_category: str,
        rel_path: str,
        content_lines: list[str] | None = None,
    ) -> None:
        """Append parsed parser-level references to result['metadata_references']."""
        if not parsed_refs or not _ref_allowed(source_category):
            return
        type_prefix = _CATEGORY_TO_TYPE_PREFIX.get(source_category, source_category)
        used_in_root = f"{type_prefix}.{source_object}"
        # Индекс якорей строится ОДИН раз на файл: порядок отдачи ссылок парсером не
        # совпадает с порядком в документе, поэтому резолв идёт по ключу, а не по позиции.
        anchor = _ref_anchor_index(content_lines)
        for ref in parsed_refs:
            ref_object = ref.get("ref_object", "")
            if not ref_object:
                continue
            suffix = ref.get("used_in_suffix", "")
            used_in = f"{used_in_root}.{suffix}" if suffix else used_in_root
            line = anchor.line_for(ref)
            result["metadata_references"].append(
                (
                    source_object,
                    source_category,
                    ref_object,
                    ref.get("ref_kind", ""),
                    used_in,
                    rel_path,
                    line,
                )
            )

    def _read(fp: Path) -> str | None:
        try:
            return fp.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            return None

    def _glob_xml(category: str) -> list[Path]:
        files: list[Path] = []
        cat_dir = base / category
        if not cat_dir.is_dir():
            return files
        for fp in cat_dir.rglob("*"):
            if fp.suffix.lower() in (".xml", ".mdo") and fp.is_file():
                files.append(fp)
        return files

    # EventSubscriptions
    for fp in _glob_xml("EventSubscriptions") if collect_es else []:
        content = _read(fp)
        if not content:
            continue
        parsed = parse_event_subscription_xml(content)
        if not parsed or not parsed.get("name"):
            continue
        handler = parsed.get("handler") or ""
        parts = handler.rsplit(".", 1)
        handler_module = parts[0].replace("CommonModule.", "") if len(parts) > 1 else ""
        handler_procedure = parts[-1] if parts else ""
        source_types = parsed.get("source_types") or []
        rel = fp.relative_to(base).as_posix()
        result["event_subscriptions"].append(
            (
                parsed["name"],
                parsed.get("synonym") or "",
                parsed.get("event") or "",
                handler_module,
                handler_procedure,
                json.dumps(source_types, ensure_ascii=False),
                len(source_types),
                rel,
            )
        )
        # Emit metadata_references for each source type
        if _ref_allowed("EventSubscriptions"):
            for st in source_types:
                canon = canonicalize_type_ref(st)
                if canon:
                    result["metadata_references"].append(
                        (
                            parsed["name"],
                            "EventSubscriptions",
                            canon,
                            "event_subscription_source",
                            f"EventSubscription.{parsed['name']}.Source",
                            rel,
                            None,
                        )
                    )

    # ScheduledJobs
    for fp in _glob_xml("ScheduledJobs") if collect_sj else []:
        content = _read(fp)
        if not content:
            continue
        parsed = parse_scheduled_job_xml(content)
        if not parsed or not parsed.get("name"):
            continue
        method_name = parsed.get("method_name") or ""
        parts = method_name.rsplit(".", 1)
        handler_module = parts[0].replace("CommonModule.", "") if len(parts) > 1 else ""
        handler_procedure = parts[-1] if parts else ""
        restart = parsed.get("restart_on_failure") or {}
        rel = fp.relative_to(base).as_posix()
        result["scheduled_jobs"].append(
            (
                parsed["name"],
                parsed.get("synonym") or "",
                method_name,
                handler_module,
                handler_procedure,
                1 if parsed.get("use", True) else 0,
                1 if parsed.get("predefined", False) else 0,
                restart.get("count", 0),
                restart.get("interval", 0),
                rel,
            )
        )

    # FunctionalOptions
    for fp in _glob_xml("FunctionalOptions") if collect_fo else []:
        content = _read(fp)
        if not content:
            continue
        parsed = parse_functional_option_xml(content)
        if not parsed or not parsed.get("name"):
            continue
        fo_content = parsed.get("content") or []
        rel = fp.relative_to(base).as_posix()
        result["functional_options"].append(
            (
                parsed["name"],
                parsed.get("synonym") or "",
                parsed.get("location") or "",
                json.dumps(fo_content, ensure_ascii=False),
                rel,
            )
        )
        # Emit metadata_references for each content object
        if _ref_allowed("FunctionalOptions"):
            for ref_str in fo_content:
                canon = canonicalize_type_ref(ref_str)
                if canon:
                    result["metadata_references"].append(
                        (
                            parsed["name"],
                            "FunctionalOptions",
                            canon,
                            "functional_option_content",
                            f"FunctionalOption.{parsed['name']}.Content",
                            rel,
                            None,
                        )
                    )
        # Emit reference for storage location (Constant.X / InformationRegister.X)
        loc = parsed.get("location") or ""
        if loc and _ref_allowed("FunctionalOptions"):
            parts = loc.split(".")
            if len(parts) >= 2:
                ref_obj = ".".join(parts[:2])
                canon = canonicalize_type_ref(ref_obj)
                if canon:
                    result["metadata_references"].append(
                        (
                            parsed["name"],
                            "FunctionalOptions",
                            canon,
                            "functional_option_content",
                            f"FunctionalOption.{parsed['name']}.Location",
                            rel,
                            None,
                        )
                    )

    # Enums
    for fp in _glob_xml("Enums") if collect_enums else []:
        content = _read(fp)
        if not content:
            continue
        parsed = parse_enum_xml(content)
        if not parsed or not parsed.get("name"):
            continue
        rel = fp.relative_to(base).as_posix()
        result["enum_values"].append(
            (
                parsed["name"],
                parsed.get("synonym") or "",
                json.dumps(parsed.get("values", []), ensure_ascii=False),
                rel,
            )
        )

    # Subsystems
    for fp in _glob_xml("Subsystems") if collect_subs else []:
        content = _read(fp)
        if not content:
            continue
        # Битый XML одного файла не должен ронять весь сбор подсистем.
        try:
            parsed = parse_metadata_xml(content)
        except Exception as exc:
            logger.warning("subsystems collector: skipping malformed %s: %s", fp, exc)
            continue
        if parsed is None:
            # parse_metadata_xml глотает ParseError в None (Finding #5) — сохраняем warning,
            # иначе битый XML тихо пропадёт в `if not parsed` ниже.
            logger.warning("subsystems collector: skipping malformed %s (parser returned None)", fp)
            continue
        if not parsed or parsed.get("object_type") != "Subsystem":
            continue
        sub_name = parsed.get("name", "")
        sub_synonym = parsed.get("synonym", "")
        sub_content = parsed.get("content", [])
        rel = fp.relative_to(base).as_posix()
        for obj_ref in sub_content:
            result["subsystem_content"].append(
                (
                    sub_name,
                    sub_synonym,
                    obj_ref,
                    rel,
                )
            )
            if _ref_allowed("Subsystems"):
                canon = canonicalize_type_ref(obj_ref)
                if canon:
                    result["metadata_references"].append(
                        (
                            sub_name,
                            "Subsystems",
                            canon,
                            "subsystem_content",
                            f"Subsystem.{sub_name}.Content",
                            rel,
                            None,
                        )
                    )

    # HTTPServices
    for fp in _glob_xml("HTTPServices") if collect_http else []:
        content = _read(fp)
        if not content:
            continue
        parsed = parse_http_service_xml(content)
        if not parsed or not parsed.get("name"):
            continue
        rel = fp.relative_to(base).as_posix()
        result["http_services"].append(
            (
                parsed["name"],
                parsed.get("root_url") or "",
                json.dumps(parsed.get("templates", []), ensure_ascii=False),
                rel,
            )
        )

    # WebServices
    for fp in _glob_xml("WebServices") if collect_ws else []:
        content = _read(fp)
        if not content:
            continue
        parsed = parse_web_service_xml(content)
        if not parsed or not parsed.get("name"):
            continue
        rel = fp.relative_to(base).as_posix()
        result["web_services"].append(
            (
                parsed["name"],
                parsed.get("namespace") or "",
                json.dumps(parsed.get("operations", []), ensure_ascii=False),
                rel,
            )
        )

    # XDTOPackages
    for fp in _glob_xml("XDTOPackages") if collect_xdto else []:
        content = _read(fp)
        if not content:
            continue
        parsed = parse_xdto_package_xml(content)
        if not parsed or not parsed.get("name"):
            continue
        # For EDT: check for sibling Package.xdto
        if fp.suffix.lower() == ".mdo":
            xdto_path = fp.parent / "Package.xdto"
            if xdto_path.exists():
                xdto_content = _read(xdto_path)
                if xdto_content:
                    parsed["types"] = parse_xdto_types(xdto_content)
        rel = fp.relative_to(base).as_posix()
        result["xdto_packages"].append(
            (
                parsed["name"],
                parsed.get("namespace") or "",
                json.dumps(parsed.get("types", []), ensure_ascii=False),
                rel,
            )
        )

    # --- Level-11: Object attributes (реквизиты, измерения, ресурсы, колонки ТЧ) ---
    # NB: _find_metadata_xml + _CF_XML_HINTS теперь module-level (см. начало файла)
    # — нужны и pointwise-пути в _update_git_fast.

    # Determine which attribute categories to scan
    _active_attr_cats = set(_ATTR_CATEGORIES) if collect_attrs_categories is None else collect_attrs_categories

    for category in _ATTR_CATEGORIES:
        if category not in _active_attr_cats:
            continue
        cat_dir = base / category
        if not cat_dir.is_dir():
            continue
        for obj_dir in sorted(cat_dir.iterdir()):
            if not obj_dir.is_dir():
                continue
            obj_name = obj_dir.name

            xml_path = _find_metadata_xml(obj_dir, category)
            if xml_path is None:
                continue

            content = _read(xml_path)
            if not content:
                continue
            try:
                parsed = parse_metadata_xml(content)
            except Exception:
                continue
            if not parsed:
                continue

            rel = xml_path.relative_to(base).as_posix()

            # Emit metadata_references from parser (attributes, owners, based_on, forms, etc.)
            # Pass content lines so attribute-level refs get an approximate line number.
            _emit_refs(
                parsed.get("references", []),
                obj_name,
                category,
                rel,
                content_lines=content.splitlines(),
            )

            for attr in parsed.get("attributes", []):
                result["object_attributes"].append(
                    (
                        obj_name,
                        category,
                        attr.get("name", ""),
                        attr.get("synonym", ""),
                        normalize_type_string(attr.get("type", "")),
                        "attribute",
                        None,
                        rel,
                    )
                )

            for dim in parsed.get("dimensions", []):
                result["object_attributes"].append(
                    (
                        obj_name,
                        category,
                        dim.get("name", ""),
                        dim.get("synonym", ""),
                        normalize_type_string(dim.get("type", "")),
                        "dimension",
                        None,
                        rel,
                    )
                )

            for res in parsed.get("resources", []):
                result["object_attributes"].append(
                    (
                        obj_name,
                        category,
                        res.get("name", ""),
                        res.get("synonym", ""),
                        normalize_type_string(res.get("type", "")),
                        "resource",
                        None,
                        rel,
                    )
                )

            for ts in parsed.get("tabular_sections", []):
                ts_name = ts.get("name", "")
                for ts_attr in ts.get("attributes", []):
                    result["object_attributes"].append(
                        (
                            obj_name,
                            category,
                            ts_attr.get("name", ""),
                            ts_attr.get("synonym", ""),
                            normalize_type_string(ts_attr.get("type", "")),
                            "ts_attribute",
                            ts_name,
                            rel,
                        )
                    )

    # --- Level-11: Predefined items (ПВХ, справочники, планы счетов) ---
    _active_predef_cats = (
        set(_PREDEFINED_CATEGORIES)
        if collect_attrs_categories is None
        else (collect_attrs_categories & set(_PREDEFINED_CATEGORIES))
    )
    for category in _PREDEFINED_CATEGORIES:
        if category not in _active_predef_cats:
            continue
        cat_dir = base / category
        if not cat_dir.is_dir():
            continue
        for obj_dir in sorted(cat_dir.iterdir()):
            if not obj_dir.is_dir():
                continue
            obj_name = obj_dir.name

            # CF: Ext/Predefined.xml
            predef_path = obj_dir / "Ext" / "Predefined.xml"
            predef_items_list = None
            rel = ""
            if predef_path.is_file():
                content = _read(predef_path)
                if content:
                    predef_items_list = parse_predefined_items(content)
                rel = predef_path.relative_to(base).as_posix()
            else:
                # EDT: predefined section inside .mdo
                mdo_path = obj_dir / f"{obj_name}.mdo"
                if mdo_path.is_file():
                    content = _read(mdo_path)
                    if content:
                        predef_items_list = parse_predefined_items(content)
                    rel = mdo_path.relative_to(base).as_posix()
                else:
                    continue

            if not predef_items_list:
                continue

            for item in predef_items_list:
                types_json = json.dumps(item.get("types", []), ensure_ascii=False)
                result["predefined_items"].append(
                    (
                        obj_name,
                        category,
                        item.get("name", ""),
                        item.get("synonym", ""),
                        item.get("code", ""),
                        types_json,
                        1 if item.get("is_folder") else 0,
                        rel,
                    )
                )
                # Emit predefined_characteristic_type refs
                if _ref_allowed(category):
                    for type_str in item.get("types", []):
                        canon = canonicalize_type_ref(type_str)
                        if canon:
                            result["metadata_references"].append(
                                (
                                    obj_name,
                                    category,
                                    canon,
                                    "predefined_characteristic_type",
                                    f"{_CATEGORY_TO_TYPE_PREFIX.get(category, category)}.{obj_name}.PredefinedItem.{item.get('name', '')}.Type",
                                    rel,
                                    None,
                                )
                            )

    # ExchangePlans (specialised + metadata_references)
    if collect_exchange_plans:
        ep_dir = base / "ExchangePlans"
        if ep_dir.is_dir():
            for plan_dir in sorted(ep_dir.iterdir()):
                if not plan_dir.is_dir():
                    continue
                plan_name = plan_dir.name
                # CF: Ext/Content.xml; EDT: <plan>.mdo (inline content)
                content_files: list[Path] = []
                cf_content = plan_dir / "Ext" / "Content.xml"
                if cf_content.is_file():
                    content_files.append(cf_content)
                mdo_path = plan_dir / f"{plan_name}.mdo"
                if mdo_path.is_file():
                    content_files.append(mdo_path)
                for cfp in content_files:
                    text = _read(cfp)
                    if not text:
                        continue
                    items = parse_exchange_plan_content(text)
                    if not items:
                        continue
                    rel = cfp.relative_to(base).as_posix()
                    for item in items:
                        ref_str = item.get("ref", "")
                        ar = 1 if item.get("auto_record") else 0
                        canon = canonicalize_type_ref(ref_str)
                        result["exchange_plan_content"].append(
                            (
                                plan_name,
                                canon or ref_str,
                                ar,
                                rel,
                            )
                        )
                        if canon and _ref_allowed("ExchangePlans"):
                            result["metadata_references"].append(
                                (
                                    plan_name,
                                    "ExchangePlans",
                                    canon,
                                    "exchange_plan_content",
                                    f"ExchangePlan.{plan_name}.Content",
                                    rel,
                                    None,
                                )
                            )

    # DefinedTypes (specialised + metadata_references)
    if collect_defined_types:
        from rlm_tools_bsl.bsl_xml_parsers import _XS_TYPE_MAP, _strip_ns_prefix

        def _normalize_dt_type(type_str: str) -> str:
            """Canonical metadata ref OR primitive (Number/String/...) — never empty.

            Handles three input shapes: "xs:decimal" (raw EDT), "decimal" (CF parser
            already stripped the prefix), and metadata refs like "CatalogRef.X".
            """
            canon = canonicalize_type_ref(type_str)
            if canon:
                return canon
            stripped = type_str.strip()
            mapped = _XS_TYPE_MAP.get(stripped)
            if mapped:
                return mapped
            # CF parser already stripped "xs:" — try with prefix re-attached.
            mapped = _XS_TYPE_MAP.get(f"xs:{stripped}")
            if mapped:
                return mapped
            return _strip_ns_prefix(stripped)

        def _process_defined_type(text: str, rel: str) -> None:
            parsed = parse_defined_type(text)
            if not parsed:
                return
            stored_types: list[str] = []
            for type_str in parsed.get("types", []):
                normalized = _normalize_dt_type(type_str)
                if normalized:
                    stored_types.append(normalized)
                # Only canonical metadata refs go into the reverse-index
                canon = canonicalize_type_ref(type_str)
                if canon and _ref_allowed("DefinedTypes"):
                    result["metadata_references"].append(
                        (
                            parsed["name"],
                            "DefinedTypes",
                            canon,
                            "defined_type_content",
                            f"DefinedType.{parsed['name']}.Type",
                            rel,
                            None,
                        )
                    )
            result["defined_types"].append((parsed["name"], json.dumps(stored_types, ensure_ascii=False), rel))

        dt_dir = base / "DefinedTypes"
        if dt_dir.is_dir():
            for fp in sorted(dt_dir.iterdir()):
                if fp.is_file() and fp.suffix.lower() == ".xml":
                    # CF format: DefinedTypes/<Name>.xml
                    text = _read(fp)
                    if text:
                        _process_defined_type(text, fp.relative_to(base).as_posix())
                elif fp.is_dir():
                    # EDT: DefinedTypes/<Name>/<Name>.mdo
                    mdo_path = fp / f"{fp.name}.mdo"
                    if mdo_path.is_file():
                        text = _read(mdo_path)
                        if text:
                            _process_defined_type(text, mdo_path.relative_to(base).as_posix())

    # ChartsOfCharacteristicTypes (specialised characteristic_types + Type refs)
    if collect_pvh_types:
        pvh_dir = base / "ChartsOfCharacteristicTypes"
        if pvh_dir.is_dir():
            for obj_dir in sorted(pvh_dir.iterdir()):
                if not obj_dir.is_dir():
                    continue
                obj_name = obj_dir.name
                # Locate metadata XML using same fallback as attributes loop
                xml_path = None
                # EDT
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
                if xml_path is None:
                    continue
                text = _read(xml_path)
                if not text:
                    continue
                parsed_pvh = parse_pvh_characteristics(text)
                if not parsed_pvh:
                    continue
                rel = xml_path.relative_to(base).as_posix()
                canonical_refs = []
                for type_str in parsed_pvh.get("types", []):
                    canon = canonicalize_type_ref(type_str)
                    if canon:
                        canonical_refs.append(canon)
                result["characteristic_types"].append(
                    (parsed_pvh["pvh_name"], json.dumps(canonical_refs, ensure_ascii=False), rel)
                )
                # Refs already emitted via parse_metadata_xml.references for the same object;
                # but only if ChartsOfCharacteristicTypes was processed via the attributes loop.
                # We don't double-emit characteristic_type refs here to avoid duplicates.

    # CommonCommands and per-object commands → command_parameter_type
    if _ref_allowed("CommonCommands") or any(
        c in (_active_ref_cats if _active_ref_cats is not None else _METADATA_REFERENCES_TRIGGER_CATEGORIES)
        for c in (
            "Catalogs",
            "Documents",
            "InformationRegisters",
            "AccumulationRegisters",
            "AccountingRegisters",
            "ChartsOfCharacteristicTypes",
            "ChartsOfAccounts",
            "ChartsOfCalculationTypes",
            "BusinessProcesses",
            "Tasks",
            "ExchangePlans",
        )
    ):
        # CommonCommands
        common_cmd_dir = base / "CommonCommands"
        if common_cmd_dir.is_dir() and _ref_allowed("CommonCommands"):
            for fp in sorted(common_cmd_dir.iterdir()):
                cmd_files: list[Path] = []
                if fp.is_file() and fp.suffix.lower() == ".xml":
                    cmd_files.append(fp)
                elif fp.is_dir():
                    # EDT: <Name>/<Name>.mdo (alternative .command path)
                    for cand in (fp / f"{fp.name}.mdo", fp / f"{fp.name}.command"):
                        if cand.is_file():
                            cmd_files.append(cand)
                            break
                for cfp in cmd_files:
                    text = _read(cfp)
                    if not text:
                        continue
                    parsed_cmds = parse_command_parameter_type(text)
                    if not parsed_cmds:
                        continue
                    rel = cfp.relative_to(base).as_posix()
                    for ref_dict in parsed_cmds:
                        canon = ref_dict.get("ref_object", "")
                        cmd_name = ref_dict.get("command_name", "") or fp.stem
                        if canon:
                            result["metadata_references"].append(
                                (
                                    cmd_name,
                                    "CommonCommands",
                                    canon,
                                    "command_parameter_type",
                                    f"CommonCommand.{cmd_name}.CommandParameterType",
                                    rel,
                                    None,
                                )
                            )

        # Per-object commands: Catalogs/X/Commands/*.xml or Catalogs/X/Commands/Y/Y.command
        # _CMD_HOST_CATS module-level — переиспользуется pointwise (_emit_object_command_refs)
        for category in _CMD_HOST_CATS:
            if not _ref_allowed(category):
                continue
            cat_dir = base / category
            if not cat_dir.is_dir():
                continue
            for obj_dir in sorted(cat_dir.iterdir()):
                if not obj_dir.is_dir():
                    continue
                obj_name = obj_dir.name
                cmd_dir = obj_dir / "Commands"
                if not cmd_dir.is_dir():
                    continue
                for entry in sorted(cmd_dir.iterdir()):
                    cmd_files: list[Path] = []
                    if entry.is_file() and entry.suffix.lower() == ".xml":
                        cmd_files.append(entry)
                    elif entry.is_dir():
                        for cand in (entry / f"{entry.name}.command", entry / f"{entry.name}.mdo"):
                            if cand.is_file():
                                cmd_files.append(cand)
                                break
                    for cfp in cmd_files:
                        text = _read(cfp)
                        if not text:
                            continue
                        parsed_cmds = parse_command_parameter_type(text)
                        if not parsed_cmds:
                            continue
                        rel = cfp.relative_to(base).as_posix()
                        type_prefix = _CATEGORY_TO_TYPE_PREFIX.get(category, category)
                        for ref_dict in parsed_cmds:
                            canon = ref_dict.get("ref_object", "")
                            cmd_name = ref_dict.get("command_name", "") or entry.stem
                            if canon:
                                result["metadata_references"].append(
                                    (
                                        obj_name,
                                        category,
                                        canon,
                                        "command_parameter_type",
                                        f"{type_prefix}.{obj_name}.Command.{cmd_name}.CommandParameterType",
                                        rel,
                                        None,
                                    )
                                )

    return result


def _insert_metadata_tables(conn: sqlite3.Connection, tables: dict[str, list[tuple]]) -> None:
    """Insert Level-2 metadata into the database."""
    # Clear existing data
    conn.execute("DELETE FROM event_subscriptions")
    conn.execute("DELETE FROM scheduled_jobs")
    conn.execute("DELETE FROM functional_options")
    try:
        conn.execute("DELETE FROM enum_values")
    except sqlite3.OperationalError:
        pass

    if tables["event_subscriptions"]:
        conn.executemany(
            "INSERT INTO event_subscriptions "
            "(name, synonym, event, handler_module, handler_procedure, source_types, source_count, file) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            tables["event_subscriptions"],
        )

    if tables["scheduled_jobs"]:
        conn.executemany(
            "INSERT INTO scheduled_jobs "
            "(name, synonym, method_name, handler_module, handler_procedure, "
            "use, predefined, restart_count, restart_interval, file) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            tables["scheduled_jobs"],
        )

    if tables["functional_options"]:
        conn.executemany(
            "INSERT INTO functional_options (name, synonym, location, content, file) VALUES (?, ?, ?, ?, ?)",
            tables["functional_options"],
        )

    if tables.get("enum_values"):
        conn.executemany(
            "INSERT INTO enum_values (name, synonym, values_json, source_file) VALUES (?, ?, ?, ?)",
            tables["enum_values"],
        )

    try:
        conn.execute("DELETE FROM subsystem_content")
    except sqlite3.OperationalError:
        pass
    if tables.get("subsystem_content"):
        conn.executemany(
            "INSERT INTO subsystem_content (subsystem_name, subsystem_synonym, object_ref, file) VALUES (?, ?, ?, ?)",
            tables["subsystem_content"],
        )

    # Integration metadata
    for table in ("http_services", "web_services", "xdto_packages"):
        try:
            conn.execute(f"DELETE FROM {table}")
        except sqlite3.OperationalError:
            pass

    if tables.get("http_services"):
        conn.executemany(
            "INSERT INTO http_services (name, root_url, templates_json, file) VALUES (?, ?, ?, ?)",
            tables["http_services"],
        )
    if tables.get("web_services"):
        conn.executemany(
            "INSERT INTO web_services (name, namespace, operations_json, file) VALUES (?, ?, ?, ?)",
            tables["web_services"],
        )
    if tables.get("xdto_packages"):
        conn.executemany(
            "INSERT INTO xdto_packages (name, namespace, types_json, file) VALUES (?, ?, ?, ?)",
            tables["xdto_packages"],
        )

    # Object attributes
    try:
        conn.execute("DELETE FROM object_attributes")
    except sqlite3.OperationalError:
        pass
    if tables.get("object_attributes"):
        conn.executemany(
            "INSERT INTO object_attributes "
            "(object_name, category, attr_name, attr_synonym, attr_type, attr_kind, ts_name, source_file) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            tables["object_attributes"],
        )

    # Predefined items
    try:
        conn.execute("DELETE FROM predefined_items")
    except sqlite3.OperationalError:
        pass
    if tables.get("predefined_items"):
        conn.executemany(
            "INSERT INTO predefined_items "
            "(object_name, category, item_name, item_synonym, item_code, types_json, is_folder, source_file) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            tables["predefined_items"],
        )

    # Level-12: metadata_references + 3 specialised tables (v1.9.0)
    for table in ("metadata_references", "exchange_plan_content", "defined_types", "characteristic_types"):
        try:
            conn.execute(f"DELETE FROM {table}")  # noqa: S608
        except sqlite3.OperationalError:
            pass
    if tables.get("metadata_references"):
        conn.executemany(
            "INSERT INTO metadata_references "
            "(source_object, source_category, ref_object, ref_kind, used_in, path, line) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            tables["metadata_references"],
        )
    if tables.get("exchange_plan_content"):
        conn.executemany(
            "INSERT INTO exchange_plan_content (plan_name, object_ref, auto_record, path) VALUES (?, ?, ?, ?)",
            tables["exchange_plan_content"],
        )
    if tables.get("defined_types"):
        conn.executemany(
            "INSERT INTO defined_types (name, type_refs_json, path) VALUES (?, ?, ?)",
            tables["defined_types"],
        )
    if tables.get("characteristic_types"):
        conn.executemany(
            "INSERT INTO characteristic_types (pvh_name, type_refs_json, path) VALUES (?, ?, ?)",
            tables["characteristic_types"],
        )


def _insert_metadata_tables_selective(
    conn: sqlite3.Connection,
    tables: dict[str, list[tuple]],
    changed_categories: set[str],
) -> None:
    """Insert Level-2 metadata selectively — only categories present in *changed_categories*.

    Tables without a ``category`` column (event_subscriptions, scheduled_jobs, etc.)
    are DELETE-d entirely when their trigger category is in *changed_categories*.
    Tables with a ``category`` column are DELETE-d per-category.
    """
    # Category-independent tables: full DELETE + INSERT when flag is on
    _TABLE_CATEGORY_MAP: dict[str, str] = {
        "event_subscriptions": "EventSubscriptions",
        "scheduled_jobs": "ScheduledJobs",
        "functional_options": "FunctionalOptions",
        "enum_values": "Enums",
        "subsystem_content": "Subsystems",
        "http_services": "HTTPServices",
        "web_services": "WebServices",
        "xdto_packages": "XDTOPackages",
        "exchange_plan_content": "ExchangePlans",
        "defined_types": "DefinedTypes",
        "characteristic_types": "ChartsOfCharacteristicTypes",
    }
    _TABLE_INSERT_SQL: dict[str, str] = {
        "event_subscriptions": (
            "INSERT INTO event_subscriptions "
            "(name, synonym, event, handler_module, handler_procedure, source_types, source_count, file) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
        ),
        "scheduled_jobs": (
            "INSERT INTO scheduled_jobs "
            "(name, synonym, method_name, handler_module, handler_procedure, "
            "use, predefined, restart_count, restart_interval, file) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        ),
        "functional_options": "INSERT INTO functional_options (name, synonym, location, content, file) VALUES (?, ?, ?, ?, ?)",
        "enum_values": "INSERT INTO enum_values (name, synonym, values_json, source_file) VALUES (?, ?, ?, ?)",
        "subsystem_content": "INSERT INTO subsystem_content (subsystem_name, subsystem_synonym, object_ref, file) VALUES (?, ?, ?, ?)",
        "http_services": "INSERT INTO http_services (name, root_url, templates_json, file) VALUES (?, ?, ?, ?)",
        "web_services": "INSERT INTO web_services (name, namespace, operations_json, file) VALUES (?, ?, ?, ?)",
        "xdto_packages": "INSERT INTO xdto_packages (name, namespace, types_json, file) VALUES (?, ?, ?, ?)",
        "exchange_plan_content": (
            "INSERT INTO exchange_plan_content (plan_name, object_ref, auto_record, path) VALUES (?, ?, ?, ?)"
        ),
        "defined_types": "INSERT INTO defined_types (name, type_refs_json, path) VALUES (?, ?, ?)",
        "characteristic_types": "INSERT INTO characteristic_types (pvh_name, type_refs_json, path) VALUES (?, ?, ?)",
    }

    for table_name, trigger_cat in _TABLE_CATEGORY_MAP.items():
        if trigger_cat not in changed_categories:
            continue
        try:
            conn.execute(f"DELETE FROM {table_name}")  # noqa: S608
        except sqlite3.OperationalError:
            pass
        rows = tables.get(table_name)
        if rows:
            conn.executemany(_TABLE_INSERT_SQL[table_name], rows)

    # Category-aware tables: DELETE by category
    for cat in changed_categories:
        try:
            conn.execute("DELETE FROM object_attributes WHERE category = ?", (cat,))
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("DELETE FROM predefined_items WHERE category = ?", (cat,))
        except sqlite3.OperationalError:
            pass

    if tables.get("object_attributes"):
        conn.executemany(
            "INSERT INTO object_attributes "
            "(object_name, category, attr_name, attr_synonym, attr_type, attr_kind, ts_name, source_file) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            tables["object_attributes"],
        )
    if tables.get("predefined_items"):
        conn.executemany(
            "INSERT INTO predefined_items "
            "(object_name, category, item_name, item_synonym, item_code, types_json, is_folder, source_file) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            tables["predefined_items"],
        )

    # Category-aware DELETE for metadata_references (v1.9.0)
    triggered_ref_cats = _METADATA_REFERENCES_TRIGGER_CATEGORIES & changed_categories
    if triggered_ref_cats:
        placeholders = ",".join("?" for _ in triggered_ref_cats)
        try:
            conn.execute(
                f"DELETE FROM metadata_references WHERE source_category IN ({placeholders})",  # noqa: S608
                tuple(triggered_ref_cats),
            )
        except sqlite3.OperationalError:
            pass
    if tables.get("metadata_references"):
        try:
            conn.executemany(
                "INSERT INTO metadata_references "
                "(source_object, source_category, ref_object, ref_kind, used_in, path, line) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                tables["metadata_references"],
            )
        except sqlite3.OperationalError:
            pass


# ---------------------------------------------------------------------------
# Pointwise incremental refresh — per-object updates inside git fast path
# ---------------------------------------------------------------------------
# Architecture: see plan tmp/wiggly-hopping-ember.md.  These helpers replace the
# category-wide DELETE+rescan in _update_git_fast for a narrow whitelist of
# categories so that touching one object does not trigger a 1000-object scan.


class PointwiseRefreshFailed(Exception):
    """Raised inside _refresh_object* when a single-object update cannot
    complete (missing/empty file, parse error, ...).  The dispatcher rolls
    back the SAVEPOINT and routes the category to the bulk fallback path.
    """

    def __init__(self, category: str, object_name: str, reason: str) -> None:
        super().__init__(f"{category}/{object_name}: {reason}")
        self.category = category
        self.object_name = object_name
        self.reason = reason


class _PointwiseTelemetry:
    """Lightweight counter aggregator used for one INFO line per update."""

    def __init__(self) -> None:
        self.pointwise_categories = 0
        self.pointwise_objects = 0
        self.fallback_categories = 0
        self.reasons: dict[str, int] = {}

    def note_pointwise(self, _category: str, n_objects: int) -> None:
        self.pointwise_categories += 1
        self.pointwise_objects += n_objects

    def note_fallback(self, _category: str, reason: str, _exc: Exception | None = None) -> None:
        self.fallback_categories += 1
        self.reasons[reason] = self.reasons.get(reason, 0) + 1


def _read_text(fp: Path) -> str | None:
    try:
        return fp.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return None


def _resolve_object_from_path(rel_path: str, base_path: str) -> tuple[str | None, str | None]:
    """Resolve metadata path → (category, object_name).

    Использует parse_bsl_path как primary, но НЕ ограничивается им: parse_bsl_path
    знает только категории из format_detector.METADATA_CATEGORIES (категории-хосты
    BSL-модулей), и не понимает чисто метаданные категории — EventSubscriptions,
    ScheduledJobs, FunctionalOptions, DefinedTypes и т.п. Для них fallback берёт
    первый сегмент пути и сверяет с _CATEGORY_RU (полный список indexable категорий).

    Layouts:
      EDT:           Category/<Name>/<Name>.mdo                — len>=3, name = parts[1]
      CF Ext:        Category/<Name>/Ext/...                   — len>=3, name = parts[1]
      CF sibling:    Category/<Name>.xml                       — len==2, name = stem
      Root file:     Configuration.xml / outside categories    — return (None, None)

    Возвращает (category, object_name) либо (category, None) для нераспознанного
    файла внутри категории, либо (None, None) для путей вне категорий.
    """
    info = parse_bsl_path(rel_path, base_path)
    if info.category and info.object_name:
        return (info.category, info.object_name)

    rel_parts = rel_path.replace("\\", "/").split("/")
    if not rel_parts:
        return (None, None)

    # parse_bsl_path может не знать категорию (отсутствует в METADATA_CATEGORIES) —
    # fallback на первый сегмент пути с проверкой по _CATEGORY_RU.
    category = info.category
    if not category and rel_parts[0] in _CATEGORY_RU:
        category = rel_parts[0]
    if not category:
        return (None, None)

    # CF sibling-only: Category/<Name>.xml (без объектного подкаталога).
    if len(rel_parts) == 2 and rel_parts[0] == category:
        filename = rel_parts[1]
        if filename.lower().endswith(".xml"):
            return (category, filename[: -len(".xml")])

    # EDT mdo / CF Ext / любой layout с объектным подкаталогом.
    if len(rel_parts) >= 3 and rel_parts[0] == category:
        return (category, rel_parts[1])

    return (category, None)


def _build_changed_objects(
    metadata_paths: set[str],
    base_path: str,
) -> tuple[dict[str, set[str]], set[str]]:
    """Resolve flat list of metadata paths into ``{category: {object_names}}``.

    Categories whose roots/unparseable paths cannot resolve to an object_name
    are returned in *unresolved* — those force the bulk fallback path.
    """
    changed: dict[str, set[str]] = {}
    unresolved: set[str] = set()
    for path in metadata_paths:
        category, object_name = _resolve_object_from_path(path, base_path)
        if category is None:
            continue
        if object_name is None:
            unresolved.add(category)
            continue
        changed.setdefault(category, set()).add(object_name)
    return changed, unresolved


def _get_optional_tables_state(conn: sqlite3.Connection) -> dict[str, bool]:
    """Single check of optional tables/features used by the pointwise dispatcher.

    Returns dict with: has_synonyms (feature flag), has_synonyms_table (physical
    table exists), has_metadata_references_table (physical table exists).
    """
    row = conn.execute("SELECT value FROM index_meta WHERE key='has_synonyms'").fetchone()
    if row is None:
        has_synonyms = True  # default for legacy
    else:
        try:
            value = row["value"]
        except (IndexError, KeyError):
            value = row[0]
        has_synonyms = str(value) == "1"
    existing = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('object_synonyms', 'metadata_references')"
        )
    }
    return {
        "has_synonyms": has_synonyms,
        "has_synonyms_table": "object_synonyms" in existing,
        "has_metadata_references_table": "metadata_references" in existing,
    }


def _category_object_count(conn: sqlite3.Connection, category: str) -> int:
    """Inventory size used by the soft (relative) threshold check.

    The source table depends on which table the category populates: Group A
    rows go into ``object_attributes``/``predefined_items`` (with category
    column), Group B rows go into dedicated single-table-per-category storage.
    """
    if category in {
        "Catalogs",
        "Documents",
        "InformationRegisters",
        "AccumulationRegisters",
        "AccountingRegisters",
    }:
        sql = "SELECT COUNT(DISTINCT object_name) FROM object_attributes WHERE category=?"
        params: tuple = (category,)
    elif category == "ChartsOfAccounts":
        sql = "SELECT COUNT(DISTINCT object_name) FROM predefined_items WHERE category=?"
        params = (category,)
    elif category == "EventSubscriptions":
        sql, params = "SELECT COUNT(*) FROM event_subscriptions", ()
    elif category == "ScheduledJobs":
        sql, params = "SELECT COUNT(*) FROM scheduled_jobs", ()
    elif category == "XDTOPackages":
        sql, params = "SELECT COUNT(*) FROM xdto_packages", ()
    else:
        return 0
    try:
        row = conn.execute(sql, params).fetchone()
        return int(row[0]) if row and row[0] is not None else 0
    except sqlite3.OperationalError:
        return 0  # legacy index without the table → relative threshold off


def _can_use_pointwise(
    category: str,
    objects: set[str],
    unresolved: set[str],
    conn: sqlite3.Connection,
    telemetry: _PointwiseTelemetry,
) -> bool:
    """Decide pointwise vs bulk fallback for a single category."""
    if category not in _POINTWISE_ELIGIBLE_GROUP_A and category not in _POINTWISE_ELIGIBLE_GROUP_B:
        telemetry.note_fallback(category, "not_eligible")
        return False
    if category in unresolved:
        telemetry.note_fallback(category, "unresolved_root")
        return False
    if not objects:
        telemetry.note_fallback(category, "no_objects")
        return False
    total = _category_object_count(conn, category)
    if total and len(objects) >= _POINTWISE_MAX_RATIO * total:
        telemetry.note_fallback(category, "threshold_relative")
        return False
    if len(objects) >= _POINTWISE_MAX_OBJECTS:
        telemetry.note_fallback(category, "threshold_absolute")
        return False
    return True


# ---------------------------------------------------------------------------
# Якорь ссылок метаданных: номер строки САМОЙ ссылки (v1.33.0)
#
# Заменяет _line_for_ref / _line_for_ref_pointwise, которые искали <Name>X</Name>
# и отдавали ПЕРВОЕ вхождение имени по файлу: замер на боевых показал, что на
# 3000 ссылок attribute_type строка НИ РАЗУ не совпала со строкой тега типа,
# а 30% имён реквизитов встречаются в одном XML больше одного раза.
# ---------------------------------------------------------------------------
_SECTION_OPEN_RE = re.compile(
    r"<\s*(Attribute|Dimension|Resource|TabularSection"
    r"|attributes|dimensions|resources|tabularSections)\b"
)
_TS_CLOSE_RE = re.compile(r"</\s*(?:TabularSection|tabularSections)\s*>")
_ANCHOR_NAME_RE = re.compile(r"<\s*[Nn]ame\s*>([^<]+)<\s*/\s*[Nn]ame\s*>")
_TYPE_BLOCK_OPEN_RE = re.compile(r"<\s*(?:Type|type|valueType)\s*>")
_TYPE_BLOCK_CLOSE_RE = re.compile(r"</\s*(?:Type|type|valueType)\s*>")
# Отдельный ТИП внутри блока: его текст нужен, чтобы сопоставить строку с ref_object
_TYPE_ITEM_RE = re.compile(r"<\s*(?:v8:Type|types)\s*>([^<]+)<")
# ref_kind -> охватывающий тег (CF-вариант, EDT-вариант). Нужен, чтобы owner и
# based_on с ОДНИМ ref_object не схлопнулись в одну строку: такие пары есть в боевых
# конфигурациях, и порядок секций у CF и EDT противоположный.
_KIND_SECTION_TAGS: dict[str, tuple[str, ...]] = {
    "owner": ("Owners", "owners"),
    "based_on": ("BasedOn", "basedOn"),
}

# Виды, которым v1.33.0 заполняет `line`. Ровно те, что физически проходят через
# `_emit_refs` (единственный call-site — внутри цикла по `_ATTR_CATEGORIES`) и
# получают там `content_lines`. Всё остальное пишется своими коллекторами с жёстким
# `None` и остаётся NULL.
# ВАЖНО: `subsystem_content` в этот список НЕ входит. Подсистемы собирает отдельный
# цикл, который аппендит ссылку с `None` напрямую и `_emit_refs` не зовёт;
# Subsystems нет и в `_ATTR_CATEGORIES`.
# `characteristic_type` через `_emit_refs` проходит, но резолвится лишь частично
# (замер на боевых: заполнено 5 из 486), поэтому принудительно возвращается в None:
# «заполнено у одного процента» — контракт хуже честного NULL.
_ANCHORED_REF_KINDS: frozenset[str] = frozenset(
    {
        "attribute_type",
        "owner",
        "based_on",
        "default_object_form",
        "default_list_form",
    }
)

_TAG_VALUE_RE_CACHE: dict[str, re.Pattern] = {}


def _tag_value_re(value: str) -> re.Pattern:
    """Точное значение внутри тега: ``>Document.X<`` или ``="Document.X"``.

    Подстрочный поиск здесь запрещён: ``Document.Заказ`` нашёлся бы внутри
    ``Document.ЗаказКлиента`` и дал бы чужую строку.
    """
    pat = _TAG_VALUE_RE_CACHE.get(value)
    if pat is None:
        e = re.escape(value)
        pat = re.compile(rf">\s*{e}\s*<|=\s*\"{e}\"")
        _TAG_VALUE_RE_CACHE[value] = pat
    return pat


_ANCHOR_KIND_BY_TAG = {
    "attribute": "Attribute",
    "attributes": "Attribute",
    "dimension": "Dimension",
    "dimensions": "Dimension",
    "resource": "Resource",
    "resources": "Resource",
    "tabularsection": "TabularSection",
    "tabularsections": "TabularSection",
}


class _RefAnchorIndex:
    """Строки ссылок метаданных: один проход по файлу, резолв ПО КЛЮЧУ ВЛАДЕЛЬЦА + ССЫЛКЕ.

    Почему не «монотонный курсор по файлу»: парсеры отдают ссылки НЕ в порядке
    документа (у регистров <Resource> в файле раньше <Dimension>, а эмитится позже;
    у документов <BasedOn> раньше <ChildObjects>, а эмитится позже), поэтому любой
    позиционный резолвер промахивается или откатывается к началу файла — то есть
    возвращает первое вхождение имени, а это ровно тот дефект, который тут чинится.

    Почему ключ включает ref_object, а не только имя владельца: составной тип даёт по
    ссылке на КАЖДЫЙ тип, и все они лежат на РАЗНЫХ строках внутри одного блока <Type>.
    Максимум на боевых — 266 типов у одного реквизита, растянутых на 266 строк:
    резолвер, отдающий всем строку контейнера <Type>, промахивается на сотни строк.

    ``line_for`` отдаёт строку КОНКРЕТНОГО типа, если ссылка сопоставлена; иначе строку
    открывающего <Type>; иначе строку объявления. ``None`` означает «якорь не найден»;
    подставлять произвольную строку запрещено.
    """

    _TYPE_LOOKAHEAD = 60

    def __init__(self, content_lines: list[str]) -> None:
        self._lines = content_lines
        # ключ владельца -> список экземпляров в порядке файла
        self._owners: dict[tuple[str, str, str], list[dict]] = {}
        # ключ владельца -> выбранный экземпляр (кеш: составной тип спрашивает многократно)
        self._picked: dict[tuple[str, str, str], dict | None] = {}
        self._plain: dict[tuple[str, str, str], int | None] = {}
        self._spans: dict[tuple[str, ...], list[tuple[int, int]]] = {}
        self._build()

    def _collect_types(self, start: int) -> tuple[int | None, dict[str, int]]:
        """От строки объявления найти блок типа и запомнить строку КАЖДОГО типа."""
        lines = self._lines
        block: int | None = None
        for j in range(start, min(start + self._TYPE_LOOKAHEAD, len(lines))):
            if _TYPE_BLOCK_OPEN_RE.search(lines[j]):
                block = j + 1
                break
            if _ANCHOR_NAME_RE.search(lines[j]):
                return None, {}  # начался следующий элемент — у этого типа нет
        if block is None:
            return None, {}
        by_ref: dict[str, int] = {}
        for j in range(block - 1, len(lines)):
            for m in _TYPE_ITEM_RE.finditer(lines[j]):
                canon = canonicalize_type_ref(m.group(1).strip())
                if canon and canon not in by_ref:
                    by_ref[canon] = j + 1
            if j > block - 1 and _TYPE_BLOCK_CLOSE_RE.search(lines[j]):
                break
        return block, by_ref

    def _build(self) -> None:
        cur_kind: str | None = None
        cur_ts = ""
        in_ts = False
        pending_ts_name = False
        lines = self._lines
        for idx, line in enumerate(lines):
            if _TS_CLOSE_RE.search(line):
                in_ts = False
                cur_ts = ""
                cur_kind = None
            for m in _SECTION_OPEN_RE.finditer(line):
                tag = _ANCHOR_KIND_BY_TAG.get(m.group(1).lower())
                if tag == "TabularSection":
                    in_ts = True
                    pending_ts_name = True
                    cur_kind = None
                else:
                    cur_kind = tag
            nm = _ANCHOR_NAME_RE.search(line)
            if nm is None:
                continue
            name = nm.group(1).strip()
            if pending_ts_name:
                cur_ts = name
                pending_ts_name = False
                continue
            if cur_kind is None:
                continue
            block, by_ref = self._collect_types(idx + 1)
            key = (cur_kind, cur_ts if in_ts else "", name)
            self._owners.setdefault(key, []).append({"decl": idx + 1, "block": block, "by_ref": by_ref})
            cur_kind = None  # один <Name> на секцию

    @staticmethod
    def _key(ref: dict) -> tuple[str, str, str] | None:
        suffix = ref.get("used_in_suffix", "") or ""
        if suffix.startswith(("Attribute.", "Dimension.", "Resource.")):
            head, rest = suffix.split(".", 1)
            return head, "", rest.split(".", 1)[0]
        if suffix.startswith("TabularSection.") and ".Attribute." in suffix:
            ts = suffix.split("TabularSection.", 1)[1].split(".", 1)[0]
            name = suffix.split(".Attribute.", 1)[1].split(".", 1)[0]
            return "Attribute", ts, name
        return None

    def line_for(self, ref: dict) -> int | None:
        # Белый список видов — ЖЁСТКИЙ контракт, а не оптимизация (см. _ANCHORED_REF_KINDS).
        if ref.get("ref_kind", "") not in _ANCHORED_REF_KINDS:
            return None
        key = self._key(ref)
        if key is not None:
            # Экземпляр владельца выбирается ОДИН раз и кешируется: составной тип
            # спрашивает один и тот же ключ по разу на каждый тип (до 266 раз), и без
            # кеша владелец «исчерпался» бы после первого обращения. Следствие: если
            # один ключ (вид, табличная часть, имя) встречается в файле дважды — а в
            # валидных метаданных 1С имена внутри секции уникальны, — все ссылки уедут
            # в ПЕРВЫЙ экземпляр. На боевых конфигурациях такого входа не встретилось.
            if key not in self._picked:
                bucket = self._owners.get(key)
                self._picked[key] = bucket.pop(0) if bucket else None
            owner = self._picked[key]
            if owner is None:
                return None
            canon = (ref.get("ref_object") or "").strip()
            return owner["by_ref"].get(canon) or owner["block"] or owner["decl"]

        # Виды без имени реквизита в суффиксе: owner, based_on,
        # default_object_form, default_list_form.
        # Якорь — ТОЧНОЕ значение внутри тега; и значение, и область поиска зависят от ВИДА.
        kind = ref.get("ref_kind", "") or ""
        suffix = ref.get("used_in_suffix", "") or ""
        needle = self._plain_needle(ref)
        if not needle:
            return None
        # Кеш ключуется ТРОЙКОЙ (вид, ПОЛНЫЙ суффикс, значение). Ни вида, ни пары
        # (вид, значение) недостаточно: один объект бывает и владельцем, и основанием,
        # а полный суффикс несёт имя элемента и разводит одноимённые роли.
        ck = (kind, suffix, needle)
        if ck in self._plain:
            return self._plain[ck]
        found = self._search_tag_value(kind, needle)
        self._plain[ck] = found
        return found

    def _search_tag_value(self, kind: str, needle: str) -> int | None:
        """Линейный поиск точного значения тега в пределах секции вида.

        МИНА ДЛЯ ТОГО, КТО БУДЕТ РАСШИРЯТЬ ПОКРЫТИЕ. Это O(ссылок x строк).
        Здесь это дёшево ТОЛЬКО потому, что якорятся исключительно виды из
        ``_ANCHORED_REF_KINDS``: у них 2-3 ссылки на небольшой объектный XML.
        Замер на боевых: 3122 ссылки на 1255 файлах — 4.4 с, и это БЫСТРЕЕ
        одно-проходной карты (6.1 с): построить карту по каждому мелкому файлу
        дороже, чем трижды его просканировать.

        Как только сюда заведут ЛЮБОЙ объёмный вид (``role_rights``,
        ``subsystem_content``, ``exchange_plan_content``, ``defined_type_content``),
        линейный скан ОБЯЗАН быть заменён на одно-проходную карту
        «значение тега -> первая строка». Замер, показывающий цену ошибки: боевой
        файл прав на 1 162 203 строки и 105 282 ссылки — экстраполяция равномерной
        выборки даёт ~8700 с резолва на ОДИН файл против ~0.9 с с картой при
        идентичном результате.

        Порядок секций у CF и EDT РАЗНЫЙ (в CF Owners раньше BasedOn, в EDT наоборот),
        поэтому опираться на позицию нельзя — только на секцию.
        """
        pat = _tag_value_re(needle)
        found: int | None = None
        spans = self._kind_spans(kind)
        if spans:
            for a, b in spans:
                for i in range(a, b + 1):
                    if pat.search(self._lines[i]):
                        found = i + 1
                        break
                if found is not None:
                    break
        else:
            for i, line in enumerate(self._lines):
                if pat.search(line):
                    found = i + 1
                    break
        return found

    def _kind_spans(self, kind: str) -> list[tuple[int, int]]:
        """Границы секции, в которой обязана лежать ссылка данного вида (0-based, включая).

        Пустой список -> ограничения нет, ищем по всему файлу.
        """
        tags = _KIND_SECTION_TAGS.get(kind)
        if not tags:
            return []
        if tags in self._spans:
            return self._spans[tags]
        opens = [re.compile(rf"<\s*{t}\b") for t in tags]
        closes = [re.compile(rf"</\s*{t}\s*>") for t in tags]
        spans: list[tuple[int, int]] = []
        start: int | None = None
        for i, line in enumerate(self._lines):
            if start is None and any(o.search(line) for o in opens):
                start = i
                if any(c.search(line) for c in closes):
                    spans.append((start, i))
                    start = None
                continue
            if start is not None and any(c.search(line) for c in closes):
                spans.append((start, i))
                start = None
        self._spans[tags] = spans
        return spans

    @staticmethod
    def _plain_needle(ref: dict) -> str | None:
        """Что именно искать в файле для видов без имени реквизита.

        Для default_object_form / default_list_form ``ref_object`` — это САМ объект
        (self-reference), а путь формы лежит в ``used_in_suffix`` ПОСЛЕ ``=``: парсеры
        кладут туда ``DefaultObjectForm=Document.X.Form.ФормаДокумента``. Поиск по
        ``ref_object`` попадает в ``<Name>ИмяОбъекта</Name>`` в шапке файла — верных
        было бы НОЛЬ, а раньше там стоял честный NULL, то есть это был бы регресс.

        Ключа ``used_in`` в словаре ref НЕТ — он собирается в ``_emit_refs`` /
        ``_insert_references_for_object`` уже ПОСЛЕ вызова резолвера.
        """
        kind = ref.get("ref_kind", "") or ""
        suffix = ref.get("used_in_suffix", "") or ""
        if kind in ("default_object_form", "default_list_form"):
            return suffix.split("=", 1)[1].strip() if "=" in suffix else None
        return (ref.get("ref_object") or "").strip() or None


class _NullAnchorIndex:
    def line_for(self, ref: dict) -> int | None:  # noqa: ARG002
        return None


def _ref_anchor_index(content_lines: list[str] | None):
    """Фабрика: ``None`` на входе -> индекс, всегда отдающий ``None``."""
    if content_lines is None:
        return _NullAnchorIndex()
    return _RefAnchorIndex(content_lines)


def _insert_references_for_object(
    conn: sqlite3.Connection,
    source_category: str,
    source_object: str,
    parsed_refs: list[dict],
    rel_path: str,
    *,
    content_lines: list[str] | None = None,
) -> None:
    """Полный аналог локального _emit_refs из _collect_metadata_tables.

    Применяет _CATEGORY_TO_TYPE_PREFIX, пропускает refs без ref_object,
    собирает used_in (root + optional suffix), вычисляет line через
    _ref_anchor_index. Caller обязан проверить has_metadata_references_table.
    """
    if not parsed_refs:
        return
    type_prefix = _CATEGORY_TO_TYPE_PREFIX.get(source_category, source_category)
    used_in_root = f"{type_prefix}.{source_object}"
    # Один индекс якорей на файл — см. _emit_refs.
    anchor = _ref_anchor_index(content_lines)
    rows: list[tuple] = []
    for ref in parsed_refs:
        ref_object = ref.get("ref_object", "")
        if not ref_object:
            continue
        suffix = ref.get("used_in_suffix", "")
        used_in = f"{used_in_root}.{suffix}" if suffix else used_in_root
        line = anchor.line_for(ref)
        rows.append(
            (
                source_object,
                source_category,
                ref_object,
                ref.get("ref_kind", ""),
                used_in,
                rel_path,
                line,
            )
        )
    if rows:
        conn.executemany(
            "INSERT INTO metadata_references "
            "(source_object, source_category, ref_object, ref_kind, used_in, path, line) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )


def _insert_attributes_for_object(
    conn: sqlite3.Connection,
    category: str,
    object_name: str,
    parsed: dict,
    rel_path: str,
) -> None:
    from rlm_tools_bsl.bsl_xml_parsers import normalize_type_string

    rows: list[tuple] = []
    for attr in parsed.get("attributes", []):
        rows.append(
            (
                object_name,
                category,
                attr.get("name", ""),
                attr.get("synonym", ""),
                normalize_type_string(attr.get("type", "")),
                "attribute",
                None,
                rel_path,
            )
        )
    for dim in parsed.get("dimensions", []):
        rows.append(
            (
                object_name,
                category,
                dim.get("name", ""),
                dim.get("synonym", ""),
                normalize_type_string(dim.get("type", "")),
                "dimension",
                None,
                rel_path,
            )
        )
    for res in parsed.get("resources", []):
        rows.append(
            (
                object_name,
                category,
                res.get("name", ""),
                res.get("synonym", ""),
                normalize_type_string(res.get("type", "")),
                "resource",
                None,
                rel_path,
            )
        )
    for ts in parsed.get("tabular_sections", []):
        ts_name = ts.get("name", "")
        for ts_attr in ts.get("attributes", []):
            rows.append(
                (
                    object_name,
                    category,
                    ts_attr.get("name", ""),
                    ts_attr.get("synonym", ""),
                    normalize_type_string(ts_attr.get("type", "")),
                    "ts_attribute",
                    ts_name,
                    rel_path,
                )
            )
    if rows:
        conn.executemany(
            "INSERT INTO object_attributes "
            "(object_name, category, attr_name, attr_synonym, attr_type, "
            "attr_kind, ts_name, source_file) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )


def _find_predefined_file(base_path: str, category: str, object_name: str) -> Path | None:
    """Найти файл с предопределёнными элементами объекта (CF: Ext/Predefined.xml,
    EDT: <obj_dir>/<obj>.mdo)."""
    obj_dir = Path(base_path) / category / object_name
    cf = obj_dir / "Ext" / "Predefined.xml"
    if cf.is_file():
        return cf
    mdo = obj_dir / f"{object_name}.mdo"
    if mdo.is_file():
        return mdo
    return None


def _insert_predefined_for_object(
    conn: sqlite3.Connection,
    category: str,
    object_name: str,
    items: list[dict],
    rel_path: str,
) -> None:
    if not items:
        return
    rows: list[tuple] = []
    for item in items:
        types_json = json.dumps(item.get("types", []), ensure_ascii=False)
        rows.append(
            (
                object_name,
                category,
                item.get("name", ""),
                item.get("synonym", ""),
                item.get("code", ""),
                types_json,
                1 if item.get("is_folder") else 0,
                rel_path,
            )
        )
    if rows:
        conn.executemany(
            "INSERT INTO predefined_items "
            "(object_name, category, item_name, item_synonym, item_code, "
            "types_json, is_folder, source_file) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )


def _emit_predefined_refs(
    conn: sqlite3.Connection,
    category: str,
    object_name: str,
    items: list[dict],
    rel_path: str,
) -> None:
    """Refs из types предопределённых (ref_kind='predefined_characteristic_type')."""
    from rlm_tools_bsl.bsl_xml_parsers import canonicalize_type_ref

    if not items:
        return
    type_prefix = _CATEGORY_TO_TYPE_PREFIX.get(category, category)
    rows: list[tuple] = []
    for item in items:
        for type_str in item.get("types", []):
            canon = canonicalize_type_ref(type_str)
            if not canon:
                continue
            rows.append(
                (
                    object_name,
                    category,
                    canon,
                    "predefined_characteristic_type",
                    f"{type_prefix}.{object_name}.PredefinedItem.{item.get('name', '')}.Type",
                    rel_path,
                    None,
                )
            )
    if rows:
        conn.executemany(
            "INSERT INTO metadata_references "
            "(source_object, source_category, ref_object, ref_kind, used_in, path, line) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )


def _emit_object_command_refs(
    conn: sqlite3.Connection,
    base_path: str,
    category: str,
    object_name: str,
) -> None:
    """Refs из per-object команд: Category/<obj>/Commands/* — ref_kind='command_parameter_type'."""
    from rlm_tools_bsl.bsl_xml_parsers import parse_command_parameter_type

    cmd_dir = Path(base_path) / category / object_name / "Commands"
    if not cmd_dir.is_dir():
        return
    type_prefix = _CATEGORY_TO_TYPE_PREFIX.get(category, category)
    rows: list[tuple] = []
    for entry in sorted(cmd_dir.iterdir()):
        cmd_files: list[Path] = []
        if entry.is_file() and entry.suffix.lower() == ".xml":
            cmd_files.append(entry)
        elif entry.is_dir():
            for cand in (entry / f"{entry.name}.command", entry / f"{entry.name}.mdo"):
                if cand.is_file():
                    cmd_files.append(cand)
                    break
        for cfp in cmd_files:
            text = _read_text(cfp)
            if not text:
                continue
            parsed_cmds = parse_command_parameter_type(text)
            if not parsed_cmds:
                continue
            rel = cfp.relative_to(Path(base_path)).as_posix()
            for ref_dict in parsed_cmds:
                canon = ref_dict.get("ref_object", "")
                cmd_name = ref_dict.get("command_name", "") or entry.stem
                if not canon:
                    continue
                rows.append(
                    (
                        object_name,
                        category,
                        canon,
                        "command_parameter_type",
                        f"{type_prefix}.{object_name}.Command.{cmd_name}.CommandParameterType",
                        rel,
                        None,
                    )
                )
    if rows:
        conn.executemany(
            "INSERT INTO metadata_references "
            "(source_object, source_category, ref_object, ref_kind, used_in, path, line) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )


def _insert_synonym_for_object(
    conn: sqlite3.Connection,
    category: str,
    object_name: str,
    parsed: dict,
    rel_path: str,
) -> None:
    raw_synonym = parsed.get("synonym") or ""
    if not raw_synonym:
        return
    prefix = _CATEGORY_RU.get(category, category)
    synonym = f"{prefix}: {raw_synonym}"
    conn.execute(
        "INSERT INTO object_synonyms (object_name, category, synonym, file) VALUES (?, ?, ?, ?)",
        (object_name, category, synonym, rel_path),
    )


def _refresh_object(
    conn: sqlite3.Connection,
    base_path: str,
    category: str,
    object_name: str,
    opt: dict[str, bool],
) -> None:
    """Pointwise refresh — Group A category (Catalogs, Documents, registers, ChartsOfAccounts).

    Удаляет старые строки во всех таблицах объекта и вставляет новые из
    содержимого .mdo/.xml объекта. Если файл удалён — INSERT не выполняется.
    """
    from rlm_tools_bsl.bsl_xml_parsers import parse_metadata_xml, parse_predefined_items

    # 1. DELETE по таблицам объекта.
    conn.execute(
        "DELETE FROM object_attributes WHERE category=? AND object_name=?",
        (category, object_name),
    )
    conn.execute(
        "DELETE FROM predefined_items WHERE category=? AND object_name=?",
        (category, object_name),
    )
    if opt.get("has_metadata_references_table"):
        conn.execute(
            "DELETE FROM metadata_references WHERE source_category=? AND source_object=?",
            (category, object_name),
        )
    if opt.get("has_synonyms") and opt.get("has_synonyms_table"):
        conn.execute(
            "DELETE FROM object_synonyms WHERE category=? AND object_name=?",
            (category, object_name),
        )

    # 2. Найти файл объекта (EDT, CF sibling, CF Ext) — _find_metadata_xml сам
    #    закрывает CF sibling-only через obj_dir.parent (Path.parent работает
    #    даже на несуществующих путях).
    obj_dir = Path(base_path) / category / object_name
    file_path = _find_metadata_xml(obj_dir, category)
    if file_path is None:
        return  # объект удалён — DELETE уже сделан, выходим.

    content = _read_text(file_path)
    if not content:
        raise PointwiseRefreshFailed(category, object_name, "empty content")
    try:
        parsed = parse_metadata_xml(content)
    except Exception as exc:  # noqa: BLE001 — diagnostic shim
        raise PointwiseRefreshFailed(category, object_name, f"parse error: {exc}") from exc
    if parsed is None:
        # parse_metadata_xml глотает ParseError в None (Finding #5); распознаём
        # битый XML отдельно от валидного-но-пустого, чтобы причина rollback не терялась.
        raise PointwiseRefreshFailed(category, object_name, "malformed metadata XML")
    if not parsed:
        raise PointwiseRefreshFailed(category, object_name, "parser returned empty")

    rel = file_path.relative_to(Path(base_path)).as_posix()

    # 4. Attributes (Group A subset с object_attributes).
    if category in _ATTR_CATEGORIES:
        _insert_attributes_for_object(conn, category, object_name, parsed, rel)

    # 5. Predefined — отдельный source_file для CF (Predefined.xml).
    if category in _PREDEFINED_CATEGORIES:
        predefined_file = _find_predefined_file(base_path, category, object_name)
        if predefined_file is not None:
            pred_content = _read_text(predefined_file)
            if pred_content:
                try:
                    items = parse_predefined_items(pred_content) or []
                except Exception:  # noqa: BLE001
                    items = []
                pred_rel = predefined_file.relative_to(Path(base_path)).as_posix()
                _insert_predefined_for_object(conn, category, object_name, items, pred_rel)
                if opt.get("has_metadata_references_table"):
                    _emit_predefined_refs(conn, category, object_name, items, pred_rel)

    # 6. Synonyms (gated by feature + table existence).
    if opt.get("has_synonyms") and opt.get("has_synonyms_table"):
        _insert_synonym_for_object(conn, category, object_name, parsed, rel)

    # 7-8. metadata_references — все DELETE/INSERT защищены has_metadata_references_table.
    if opt.get("has_metadata_references_table"):
        # Refs из parsed.references — bulk collector эмитит их ТОЛЬКО в loop по
        # _ATTR_CATEGORIES ([bsl_index.py:1962-2055]). ChartsOfAccounts в этот loop
        # не попадает (только в _PREDEFINED_CATEGORIES) — поэтому pointwise тоже
        # не должен их эмитить, иначе CoA pointwise разойдётся с fresh full build.
        if category in _ATTR_CATEGORIES:
            _insert_references_for_object(
                conn,
                category,
                object_name,
                parsed.get("references", []),
                rel,
                content_lines=content.splitlines(),
            )
        if category in _CMD_HOST_CATS:
            _emit_object_command_refs(conn, base_path, category, object_name)


# ---------------------------------------------------------------------------
# Pointwise — Group B (event_subscriptions / scheduled_jobs / xdto_packages)
# ---------------------------------------------------------------------------


def _find_global_metadata_file(base_path: str, category: str, object_name: str) -> Path | None:
    """EventSubscriptions / ScheduledJobs: EDT (<obj>/<obj>.mdo) или CF sibling
    (<Category>/<obj>.xml) или CF Ext fallback."""
    obj_dir = Path(base_path) / category / object_name
    return _find_metadata_xml(obj_dir, category)


def _escape_for_sql_like(s: str) -> str:
    """Escape SQL LIKE special chars (\\, %, _) using ``\\`` as ESCAPE char.

    Used by deletion path to safely build prefix-match LIKE patterns from
    user-provided category/object names without risking over-deletion when
    the name contains ``_`` (single-char wildcard) or ``%`` (multi-char wildcard).
    Backslashes are escaped FIRST so subsequent escape-prefixes are not double-escaped.
    """
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _insert_global_object(
    conn: sqlite3.Connection,
    category: str,
    parsed: dict,
    rel_path: str,
) -> None:
    if category == "EventSubscriptions":
        handler = parsed.get("handler") or ""
        parts = handler.rsplit(".", 1)
        handler_module = parts[0].replace("CommonModule.", "") if len(parts) > 1 else ""
        handler_procedure = parts[-1] if parts else ""
        source_types = parsed.get("source_types") or []
        conn.execute(
            "INSERT INTO event_subscriptions "
            "(name, synonym, event, handler_module, handler_procedure, "
            "source_types, source_count, file) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                parsed.get("name") or "",
                parsed.get("synonym") or "",
                parsed.get("event") or "",
                handler_module,
                handler_procedure,
                json.dumps(source_types, ensure_ascii=False),
                len(source_types),
                rel_path,
            ),
        )
    elif category == "ScheduledJobs":
        method_name = parsed.get("method_name") or ""
        parts = method_name.rsplit(".", 1)
        handler_module = parts[0].replace("CommonModule.", "") if len(parts) > 1 else ""
        handler_procedure = parts[-1] if parts else ""
        restart = parsed.get("restart_on_failure") or {}
        conn.execute(
            "INSERT INTO scheduled_jobs "
            "(name, synonym, method_name, handler_module, handler_procedure, "
            "use, predefined, restart_count, restart_interval, file) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                parsed.get("name") or "",
                parsed.get("synonym") or "",
                method_name,
                handler_module,
                handler_procedure,
                1 if parsed.get("use", True) else 0,
                1 if parsed.get("predefined", False) else 0,
                restart.get("count", 0),
                restart.get("interval", 0),
                rel_path,
            ),
        )


def _insert_xdto_package(
    conn: sqlite3.Connection,
    parsed: dict,
    rel_path: str,
) -> None:
    conn.execute(
        "INSERT INTO xdto_packages (name, namespace, types_json, file) VALUES (?, ?, ?, ?)",
        (
            parsed.get("name") or "",
            parsed.get("namespace") or "",
            json.dumps(parsed.get("types", []), ensure_ascii=False),
            rel_path,
        ),
    )


def _emit_event_subscription_source_refs(
    conn: sqlite3.Connection,
    parsed: dict,
    rel_path: str,
) -> None:
    """Refs из source_types — ref_kind='event_subscription_source'."""
    from rlm_tools_bsl.bsl_xml_parsers import canonicalize_type_ref

    name = parsed.get("name") or ""
    if not name:
        return
    rows: list[tuple] = []
    for st in parsed.get("source_types") or []:
        canon = canonicalize_type_ref(st)
        if not canon:
            continue
        rows.append(
            (
                name,
                "EventSubscriptions",
                canon,
                "event_subscription_source",
                f"EventSubscription.{name}.Source",
                rel_path,
                None,
            )
        )
    if rows:
        conn.executemany(
            "INSERT INTO metadata_references "
            "(source_object, source_category, ref_object, ref_kind, used_in, path, line) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )


def _refresh_xdto_package(
    conn: sqlite3.Connection,
    base_path: str,
    object_name: str,
    opt: dict[str, bool],
) -> None:
    """Pointwise refresh для XDTOPackages: EDT (<obj>/<obj>.mdo + Package.xdto) или
    CF sibling (XDTOPackages/<obj>.xml; Package.bin рядом игнорируется)."""
    from rlm_tools_bsl.bsl_xml_parsers import parse_metadata_xml, parse_xdto_package_xml

    pkg_dir = Path(base_path) / "XDTOPackages" / object_name
    mdo = pkg_dir / f"{object_name}.mdo"
    if mdo.is_file():
        main_path: Path | None = mdo
    else:
        sibling = Path(base_path) / "XDTOPackages" / f"{object_name}.xml"
        main_path = sibling if sibling.is_file() else None

    # object_synonyms cleanup — всегда (bulk path делает category-wide DELETE,
    # pointwise — точечно по (category, object_name)).
    if opt.get("has_synonyms") and opt.get("has_synonyms_table"):
        conn.execute(
            "DELETE FROM object_synonyms WHERE category=? AND object_name=?",
            ("XDTOPackages", object_name),
        )

    if main_path is None:
        # Удаление: row.name могло быть != object_name (внутренний <Name> в XML),
        # а row.file — любым из layouts, поддерживаемых _find_metadata_xml + bulk
        # rglob: EDT <obj>/<obj>.mdo, CF sibling <obj>.xml, CF Ext <obj>/Ext/*.xml.
        # Чистим по name + точным sibling-кандидатам + LIKE-prefix для всего,
        # что лежит под <Category>/<object_name>/ (ловит произвольный CF Ext layout).
        candidate_files = [
            f"XDTOPackages/{object_name}/{object_name}.mdo",
            f"XDTOPackages/{object_name}.xml",
        ]
        placeholders = ",".join("?" * len(candidate_files))
        like_prefix = _escape_for_sql_like(f"XDTOPackages/{object_name}/") + "%"
        conn.execute(
            f"DELETE FROM xdto_packages WHERE name=? OR file IN ({placeholders}) "  # noqa: S608
            f"OR file LIKE ? ESCAPE '\\'",
            (object_name, *candidate_files, like_prefix),
        )
        if opt.get("has_metadata_references_table"):
            conn.execute(
                f"DELETE FROM metadata_references "  # noqa: S608
                f"WHERE source_category=? AND (source_object=? "
                f"OR path IN ({placeholders}) OR path LIKE ? ESCAPE '\\')",
                ("XDTOPackages", object_name, *candidate_files, like_prefix),
            )
        return

    main_content = _read_text(main_path)
    if not main_content:
        raise PointwiseRefreshFailed("XDTOPackages", object_name, "empty main content")

    xdto_path = pkg_dir / "Package.xdto"
    xdto_content = _read_text(xdto_path) if xdto_path.is_file() else ""
    if xdto_content is None:
        xdto_content = ""

    try:
        parsed = parse_xdto_package_xml(main_content, xdto_content)
    except Exception as exc:  # noqa: BLE001
        raise PointwiseRefreshFailed("XDTOPackages", object_name, f"parse error: {exc}") from exc
    if not parsed:
        raise PointwiseRefreshFailed("XDTOPackages", object_name, "parser returned empty")

    rel = main_path.relative_to(Path(base_path)).as_posix()
    parsed_name = parsed.get("name") or object_name
    conn.execute(
        "DELETE FROM xdto_packages WHERE name=? OR file=?",
        (parsed_name, rel),
    )
    if opt.get("has_metadata_references_table"):
        conn.execute(
            "DELETE FROM metadata_references WHERE source_category=? AND (source_object=? OR path=?)",
            ("XDTOPackages", parsed_name, rel),
        )
    _insert_xdto_package(conn, parsed, rel)
    if opt.get("has_metadata_references_table"):
        _insert_references_for_object(
            conn,
            "XDTOPackages",
            parsed_name,
            parsed.get("references", []) or [],
            rel,
        )

    # object_synonyms — паритет с bulk (`_collect_object_synonyms` использует
    # parse_metadata_xml, не parse_xdto_package_xml). Лишний разовый парсинг
    # одного файла гарантирует совпадение Tier 5.
    if opt.get("has_synonyms") and opt.get("has_synonyms_table"):
        meta_parsed = parse_metadata_xml(main_content)
        if meta_parsed is None:
            # Finding #5: ParseError → None. Ветка недостижима (тот же main_content уже
            # успешно ET-распарсен parse_xdto_package_xml выше), debug-лог для hardening.
            logger.debug("_refresh_xdto_package: parse_metadata_xml returned None for %s", rel)
            meta_parsed = {}
        _insert_synonym_for_object(conn, "XDTOPackages", object_name, meta_parsed, rel)


def _refresh_global_object(
    conn: sqlite3.Connection,
    base_path: str,
    category: str,
    object_name: str,
    opt: dict[str, bool],
) -> None:
    """Pointwise refresh — Group B (один объект / одна целевая таблица)."""
    if category == "XDTOPackages":
        _refresh_xdto_package(conn, base_path, object_name, opt)
        return

    from rlm_tools_bsl.bsl_xml_parsers import (
        parse_event_subscription_xml,
        parse_metadata_xml,
        parse_scheduled_job_xml,
    )

    table = _GLOBAL_TABLE_BY_CATEGORY[category]
    parser = parse_event_subscription_xml if category == "EventSubscriptions" else parse_scheduled_job_xml

    # object_synonyms cleanup — всегда (Group B входит в _SYNONYM_CATEGORIES через
    # _CATEGORY_RU; bulk path делает category-wide DELETE, pointwise — точечно).
    if opt.get("has_synonyms") and opt.get("has_synonyms_table"):
        conn.execute(
            "DELETE FROM object_synonyms WHERE category=? AND object_name=?",
            (category, object_name),
        )

    file_path = _find_global_metadata_file(base_path, category, object_name)
    if file_path is None:
        # Удаление: parsed недоступен, чистим по object_name И по возможным
        # file-путям. row.name мог быть != object_name (внутренний <Name> в
        # XML), а row.file — любой из layouts: EDT <obj>/<obj>.mdo, CF sibling
        # <obj>.xml, CF Ext <obj>/Ext/*.xml (bulk-collector через rglob их
        # поддерживает все). Чистим по name + точным sibling-кандидатам + LIKE
        # prefix для всего, что лежит под <Category>/<object_name>/.
        candidate_files = [
            f"{category}/{object_name}/{object_name}.mdo",  # EDT
            f"{category}/{object_name}.xml",  # CF sibling
        ]
        placeholders = ",".join("?" * len(candidate_files))
        like_prefix = _escape_for_sql_like(f"{category}/{object_name}/") + "%"
        conn.execute(
            f"DELETE FROM {table} WHERE name=? OR file IN ({placeholders}) "  # noqa: S608
            f"OR file LIKE ? ESCAPE '\\'",
            (object_name, *candidate_files, like_prefix),
        )
        if opt.get("has_metadata_references_table"):
            conn.execute(
                f"DELETE FROM metadata_references "  # noqa: S608
                f"WHERE source_category=? AND (source_object=? "
                f"OR path IN ({placeholders}) OR path LIKE ? ESCAPE '\\')",
                (category, object_name, *candidate_files, like_prefix),
            )
        return

    content = _read_text(file_path)
    if not content:
        raise PointwiseRefreshFailed(category, object_name, "empty content")
    try:
        parsed = parser(content)
    except Exception as exc:  # noqa: BLE001
        raise PointwiseRefreshFailed(category, object_name, f"parse error: {exc}") from exc
    if not parsed:
        raise PointwiseRefreshFailed(category, object_name, "parser returned empty")

    rel = file_path.relative_to(Path(base_path)).as_posix()
    parsed_name = parsed.get("name") or object_name
    # full collector хранит row.name из parsed XML — поэтому DELETE/INSERT
    # идёт по parsed_name; дополнительно чистим по file/path, чтобы переименование
    # внутреннего <Name> в том же файле не оставило stale row.
    conn.execute(
        f"DELETE FROM {table} WHERE name=? OR file=?",  # noqa: S608
        (parsed_name, rel),
    )
    if opt.get("has_metadata_references_table"):
        conn.execute(
            "DELETE FROM metadata_references WHERE source_category=? AND (source_object=? OR path=?)",
            (category, parsed_name, rel),
        )

    _insert_global_object(conn, category, parsed, rel)

    if opt.get("has_metadata_references_table"):
        _insert_references_for_object(
            conn,
            category,
            parsed_name,
            parsed.get("references", []) or [],
            rel,
        )
        if category == "EventSubscriptions":
            _emit_event_subscription_source_refs(conn, parsed, rel)

    # object_synonyms — паритет с bulk: `_collect_object_synonyms` использует
    # `parse_metadata_xml`, не специализированный парсер. Лишний один парсинг
    # на объект гарантирует совпадение синонима с bulk path.
    if opt.get("has_synonyms") and opt.get("has_synonyms_table"):
        try:
            meta_parsed = parse_metadata_xml(content)
        except Exception as exc:
            logger.warning(
                "pointwise synonym refresh: skipping %s/%s due to parse error: %s",
                category,
                object_name,
                exc,
            )
            meta_parsed = {}
        else:
            if meta_parsed is None:
                # ParseError проглочена в None (Finding #5) — сохраняем warning
                logger.warning(
                    "pointwise synonym refresh: skipping %s/%s — malformed metadata XML",
                    category,
                    object_name,
                )
                meta_parsed = {}
        if meta_parsed:
            _insert_synonym_for_object(conn, category, object_name, meta_parsed, rel)


# ---------------------------------------------------------------------------
# File paths collection for navigation index
# ---------------------------------------------------------------------------
_FILE_NAV_EXTENSIONS = {".bsl", ".mdo", ".xml"}

# Directories to skip (same as helpers._SKIP_DIRS)
_SKIP_DIRS_NAV = {
    ".git",
    ".build",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".tox",
    ".mypy_cache",
    ".cache",
    ".rlm_cache",
}


def _collect_file_paths(base_path: str) -> list[tuple]:
    """Collect all .bsl/.mdo/.xml file paths for the navigation index.

    Returns list of tuples ready for INSERT:
        (rel_path, extension, dir_path, filename, depth, size, mtime)
    """
    base = Path(base_path)
    rows: list[tuple] = []

    for dirpath, dirnames, filenames in os.walk(base):
        # Filter out hidden/skip directories
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS_NAV and not d.startswith(".")]
        for fname in filenames:
            if fname.startswith("."):
                continue
            ext = os.path.splitext(fname)[1].lower()
            if ext not in _FILE_NAV_EXTENSIONS:
                continue

            full_path = Path(dirpath) / fname
            try:
                st = full_path.stat()
            except OSError:
                continue

            rel = full_path.relative_to(base).as_posix()
            parts = rel.split("/")
            dir_path = "/".join(parts[:-1]) if len(parts) > 1 else ""
            depth = len(parts)

            rows.append((rel, ext, dir_path, fname, depth, st.st_size, st.st_mtime))

    return rows


def _insert_file_paths(conn: sqlite3.Connection, rows: list[tuple]) -> None:
    """Insert file navigation paths into the database."""
    try:
        conn.execute("DELETE FROM file_paths")
    except sqlite3.OperationalError:
        pass
    if rows:
        conn.executemany(
            "INSERT OR REPLACE INTO file_paths "
            "(rel_path, extension, dir_path, filename, depth, size, mtime) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )


# ---------------------------------------------------------------------------
# Regex for register movements (in-band extraction from Document BSL)
# ---------------------------------------------------------------------------
# Capture the register name after ``Движения.`` but reject BSL method-calls on the
# recordset-manager itself (``Движения.Записать()`` — commit-all method; likewise
# .Очистить()/.Прочитать()/…). The negative lookahead ``(?!\w+\s*\()`` is placed
# BEFORE the capture on purpose: after a greedy ``(\w+)`` it would backtrack to
# ``Записат`` (dropping the trailing ``ь``) and slip a truncated artefact through.
# Placed first, it rejects the whole ``<ident>(`` position; a real register
# (``Движения.Рег.`` / ``Движения.Рег[…]``) is never immediately followed by ``(``.
_MOVEMENTS_RE = re.compile(r"\u0414\u0432\u0438\u0436\u0435\u043d\u0438\u044f\.(?!\w+\s*\()(\w+)")  # Движения.RegName
# Unambiguous collection-methods of the ``Движения`` recordset manager. Used as a
# read-time stop-set to clean pre-existing indexes (rows built before the lookahead
# fix) AND as belt-and-suspenders on build/live. Conservative on purpose (Codex #3):
# ambiguous names that could be a real register (Количество/Получить/Найти/Добавить/
# Отбор/…) are deliberately excluded — the lookahead already drops their call form.
_MOVEMENT_METHOD_NOISE = frozenset(
    {
        "записать",
        "прочитать",
        "читать",
        "очистить",
        "записывать",
        "выгрузить",
        "загрузить",
        "модифицированность",
        "обойти",
    }
)
_ERP_MECHANISM_RE = re.compile(
    r"\u041c\u0435\u0445\u0430\u043d\u0438\u0437\u043c\u044b\u0414\u043e\u043a\u0443\u043c\u0435\u043d\u0442\u0430"
    r'\.\u0414\u043e\u0431\u0430\u0432\u0438\u0442\u044c\(\s*"(\w+)"'
)  # МеханизмыДокумента.Добавить("RegName")
_MANAGER_TABLE_RE = re.compile(
    r"(?:\u0424\u0443\u043d\u043a\u0446\u0438\u044f|\u041f\u0440\u043e\u0446\u0435\u0434\u0443\u0440\u0430)\s+"
    r"\u0422\u0435\u043a\u0441\u0442\u0417\u0430\u043f\u0440\u043e\u0441\u0430"
    r"\u0422\u0430\u0431\u043b\u0438\u0446\u0430(\w+)\s*\(",
    re.IGNORECASE,
)  # Функция|Процедура ТекстЗапросаТаблицаRegName(
_ADAPTED_PROC_RE = re.compile(
    r"(?:\u0424\u0443\u043d\u043a\u0446\u0438\u044f|\u041f\u0440\u043e\u0446\u0435\u0434\u0443\u0440\u0430)\s+"
    r"\u0410\u0434\u0430\u043f\u0442\u0438\u0440\u043e\u0432\u0430\u043d\u043d\u044b\u0439"
    r"\u0422\u0435\u043a\u0441\u0442\u0417\u0430\u043f\u0440\u043e\u0441\u0430"
    r"\u0414\u0432\u0438\u0436\u0435\u043d\u0438\u0439\u041f\u043e\u0420\u0435\u0433\u0438\u0441\u0442\u0440\u0443\b.*?\n"
    r"(.*?)"
    r"\n\s*\u041a\u043e\u043d\u0435\u0446(?:\u0424\u0443\u043d\u043a\u0446\u0438\u0438|\u041f\u0440\u043e\u0446\u0435\u0434\u0443\u0440\u044b)",
    re.IGNORECASE | re.DOTALL,
)  # Функция|Процедура АдаптированныйТекстЗапросаДвиженийПоРегистру...КонецФункции|КонецПроцедуры
_ADAPTED_REG_RE = re.compile(
    r'\u0418\u043c\u044f\u0420\u0435\u0433\u0438\u0441\u0442\u0440\u0430\s*=\s*"(\w+)"',
    re.IGNORECASE,
)  # ИмяРегистра = "RegName"


# Прямое создание набора записей из ManagerModule:
#   РегистрыСведений.X.СоздатьНаборЗаписей()  |  InformationRegisters.X.CreateRecordSet()
# Наивное расширение `Движения.X` на менеджер давало 31-34% ложных срабатываний
# (Движения.Количество / .Отбор / .Колонки — поля набора, а не имена регистров),
# поэтому берётся только эта однозначная форма, и имя дополнительно
# валидируется по каталогу регистров при вставке.
_MANAGER_RECORDSET_RE = re.compile(
    r"(?<![\w.])(?:\u0420\u0435\u0433\u0438\u0441\u0442\u0440\u044b(?:\u0421\u0432\u0435\u0434\u0435\u043d\u0438\u0439|\u041d\u0430\u043a\u043e\u043f\u043b\u0435\u043d\u0438\u044f|\u0411\u0443\u0445\u0433\u0430\u043b\u0442\u0435\u0440\u0438\u0438|\u0420\u0430\u0441\u0447\u0435\u0442\u0430)"
    r"|(?:Information|Accumulation|Accounting|Calculation)Registers)"
    r"\s*\.\s*(\w+)\s*\.\s*(?:\u0421\u043e\u0437\u0434\u0430\u0442\u044c\u041d\u0430\u0431\u043e\u0440\u0417\u0430\u043f\u0438\u0441\u0435\u0439|CreateRecordSet)\s*\(",
    re.IGNORECASE,
)  # РегистрыСведений.X.СоздатьНаборЗаписей( | InformationRegisters.X.CreateRecordSet(

_REGISTER_CATALOG_DIRS = (
    "InformationRegisters",
    "AccumulationRegisters",
    "AccountingRegisters",
    "CalculationRegisters",
)


def _known_register_names(base: Path) -> frozenset[str]:
    """Имена всех регистров конфигурации (нижний регистр) — по каталогам на диске.

    Четыре ``iterdir``: дешевле и полнее, чем выборка из ``object_synonyms`` (у регистра
    может не быть ни синонима, ни модуля). Нужен для отбраковки ложных имён,
    извлечённых из ManagerModule: сам регекс однозначен, но сверка защищает от
    мусорных имён на конфигурациях, которые мы не мерили.
    """
    names: set[str] = set()
    for cat in _REGISTER_CATALOG_DIRS:
        d = base / cat
        if not d.is_dir():
            continue
        try:
            for entry in d.iterdir():
                if entry.is_dir():
                    names.add(entry.name.lower())
                elif entry.suffix.lower() in (".xml", ".mdo"):
                    names.add(entry.stem.lower())
        except OSError:
            continue
    return frozenset(names)


# ---------------------------------------------------------------------------
# Region parser (stack-based #Область/#КонецОбласти)
# ---------------------------------------------------------------------------
_REGION_OPEN_RE = re.compile(r"^\s*#(?:Область|Region)\s+(\S.*)$", re.IGNORECASE)
_REGION_CLOSE_RE = re.compile(r"^\s*#(?:КонецОбласти|EndRegion)\b", re.IGNORECASE)


def _parse_regions(lines: list[str]) -> list[dict]:
    """Parse #Область/#КонецОбласти directives, return regions with line ranges."""
    regions: list[dict] = []
    stack: list[dict] = []
    for lineno, raw in enumerate(lines, 1):
        stripped = raw.strip()
        # Skip comment lines (// #Область is a commented-out directive)
        if stripped.startswith("//"):
            continue
        m = _REGION_OPEN_RE.match(raw)
        if m:
            name = m.group(1).strip()
            if not name:
                continue
            entry = {"name": name, "line": lineno, "end_line": None}
            stack.append(entry)
            regions.append(entry)
            continue
        if _REGION_CLOSE_RE.match(raw):
            if stack:
                stack[-1]["end_line"] = lineno
                stack.pop()
            # else: extra #КонецОбласти — ignore
    return regions


# ---------------------------------------------------------------------------
# Module header comment extractor
# ---------------------------------------------------------------------------
_HEADER_STOP_WORDS = frozenset(
    (
        "процедура",
        "функция",
        "procedure",
        "function",
        "#область",
        "#region",
        "перем",
        "var",
    )
)


def _extract_header_comment(lines: list[str], max_chars: int = 500) -> str:
    """Extract leading // comment block from BSL module."""
    collected: list[str] = []
    found_comment = False
    for raw in lines:
        stripped = raw.strip()
        if not stripped:
            if found_comment:
                break  # empty line after comment block ends it
            continue  # skip leading empty lines
        lower = stripped.lower()
        if any(lower.startswith(sw) for sw in _HEADER_STOP_WORDS):
            break
        if stripped.startswith("//"):
            found_comment = True
            # Strip // prefix and optional space
            text = stripped[2:]
            if text.startswith(" "):
                text = text[1:]
            collected.append(text)
        else:
            break
    result = "\n".join(collected)
    # Skip BSP copyright blocks — noise, present in ~60-70% of modules
    if "Copyright" in result:
        return ""
    if len(result) > max_chars:
        result = result[:max_chars]
    return result


class FileResult(NamedTuple):
    """Result of processing a single .bsl file."""

    info: BslFileInfo
    mtime: float
    size: int
    methods: list[dict]
    raw_calls: list[tuple[int, str, int]]
    movements: list[tuple[str, str, str]]  # (register_name, source, rel_path)
    regions: list[dict] = []
    header_comment: str = ""
    # (object_ref, member_path, usage_kind, line) — reverse code-usages (v1.14.0)
    code_usages: tuple[tuple[str, str | None, str, int], ...] = ()


def _extract_movements(
    content: str,
    info: BslFileInfo,
    rel_path: str,
) -> list[tuple[str, str, str]]:
    """Extract register movements from Document modules (in-band, no extra I/O).

    v1.33.0: ни один регекс больше не работает по СЫРОМУ тексту.
      * ``full`` — комментарии И строковые литералы погашены. По ней идут регексы,
        читающие имя из КОДА: ``Движения.X``, ``ТекстЗапросаТаблицаX``. Иначе
        ``// Движения.X`` и ``"Движения.X"`` попадали в таблицу как настоящая запись.
      * ``comments_only`` — погашены только комментарии, литералы целы. По ней идут
        ``_ERP_MECHANISM_RE`` и ``_ADAPTED_REG_RE``: они по замыслу читают имя ИЗ
        литерала, полная маска убила бы сигнал. Работа по сырому тексту тащила
        закомментированные ``//МеханизмыДокумента.Добавить("X")``.
    """
    if info.category != "Documents":
        return []
    if info.module_type not in ("ObjectModule", "ManagerModule"):
        return []

    results: list[tuple[str, str, str]] = []
    src_lines = content.splitlines()
    full = "\n".join(mask_comments_and_strings(src_lines))
    comments_only = "\n".join(mask_comments_and_strings(src_lines, keep_string_content=True))

    if info.module_type == "ObjectModule":
        for m in _MOVEMENTS_RE.finditer(full):
            reg = m.group(1)
            # Belt-and-suspenders: the lookahead already rejects Движения.Method(),
            # but a paren-less Движения.Записать would still capture — drop stop-set names.
            if reg.lower() in _MOVEMENT_METHOD_NOISE:
                continue
            results.append((reg, "code", rel_path))
    elif info.module_type == "ManagerModule":
        for m in _ERP_MECHANISM_RE.finditer(comments_only):
            results.append((m.group(1), "erp_mechanism", rel_path))
        for m in _MANAGER_TABLE_RE.finditer(full):
            results.append((m.group(1), "manager_table", rel_path))
        # ИЗВЕСТНАЯ ГРАНИЦА (не регресс v1.33.0): `_ADAPTED_PROC_RE` ищет границы функции
        # по `comments_only`, где литералы целы, поэтому строка `"КонецФункции"` ВНУТРИ
        # литерала обрывает блок раньше и следующие `ИмяРегистра = "X"` теряются. В v14
        # поиск шёл по сырому тексту с тем же эффектом. Лечится разделением: границы
        # функции искать по полной маске, а имя — по `comments_only`; на боевых
        # конфигурациях случай не встретился, поэтому в этот релиз не входит.
        adapted_match = _ADAPTED_PROC_RE.search(comments_only)
        if adapted_match:
            for m in _ADAPTED_REG_RE.finditer(adapted_match.group(1)):
                results.append((m.group(1), "adapted", rel_path))
        # Имя регистра берётся из КОДА, поэтому маска ``full``. Отбраковка по каталогу
        # регистров идёт при ВСТАВКЕ: per-file экстрактор каталога не видит.
        for m in _MANAGER_RECORDSET_RE.finditer(full):
            results.append((m.group(1), "manager_code", rel_path))

    return results


def _process_single_file(
    file_path: Path,
    base_path: str,
    build_calls: bool,
) -> FileResult | None:
    """Process a single .bsl file: parse metadata, methods, optionally calls and movements.

    Returns:
        FileResult namedtuple or None on error.
    """
    try:
        st = file_path.stat()
        mtime = st.st_mtime
        size = st.st_size
    except OSError:
        return None

    info = parse_bsl_path(str(file_path), base_path)

    try:
        with open(file_path, encoding="utf-8-sig", errors="replace") as f:
            content = f.read()
    except OSError:
        return None

    lines = content.splitlines()
    methods = _parse_procedures_from_lines(lines)

    raw_calls: list[tuple[int, str, int]] = []
    if build_calls:
        for method_idx, method in enumerate(methods):
            start = method["line"]
            end = method["end_line"] if method["end_line"] else len(lines)
            for callee_name, call_line in _extract_calls_from_body(lines, start, end):
                raw_calls.append((method_idx, callee_name, call_line))

    # In-band: extract register movements from Document modules (no extra I/O)
    rel_path = info.relative_path
    movements = _extract_movements(content, info, rel_path)

    # In-band: parse regions and header comment (zero extra I/O)
    regions = _parse_regions(lines)
    header_comment = _extract_header_comment(lines)

    # In-band: reverse code-usages of metadata objects (zero extra I/O).
    # Built unconditionally (like movements/regions) — the table is code-derived
    # and must be populated even when metadata indexing is disabled (--no-metadata).
    code_usages = tuple(_extract_code_usages(lines))

    return FileResult(
        info,
        mtime,
        size,
        methods,
        raw_calls,
        movements,
        regions,
        header_comment,
        code_usages,
    )


# ---------------------------------------------------------------------------
# Regex-based role rights parsing (4x faster than ElementTree)
# ---------------------------------------------------------------------------
# Both CF (Rights.xml) and EDT (.rights) use the same XML format:
#   <object>
#     <name>Category.ObjectName</name>
#     <right><name>Read</name><value>true</value></right>
#   </object>
def _parse_role_rights_for_index(
    content: str,
    role_name: str,
    file_path: str,
) -> list[tuple[str, str, str, str]]:
    """Parse role rights using ElementTree. Returns list of (role_name, object_name, right_name, file)."""
    from rlm_tools_bsl.bsl_xml_parsers import parse_rights_xml

    results: list[tuple[str, str, str, str]] = []
    for entry in parse_rights_xml(content):
        full_name = entry["object"]
        for right in entry["rights"]:
            results.append((role_name, full_name, right, file_path))
    return results


def _role_rights_to_references(
    role_rights_rows: list[tuple[str, str, str, str]],
) -> list[tuple]:
    """Convert role_rights rows into metadata_references rows (one per (role, object))."""
    from rlm_tools_bsl.bsl_xml_parsers import canonicalize_type_ref

    seen: set[tuple[str, str]] = set()
    out: list[tuple] = []
    for role_name, object_name, _right_name, file_path in role_rights_rows:
        canon = canonicalize_type_ref(object_name)
        if not canon:
            continue
        key = (role_name, canon)
        if key in seen:
            continue
        seen.add(key)
        out.append(
            (
                role_name,
                "Roles",
                canon,
                "role_rights",
                f"Role.{role_name}.Rights",
                file_path,
                None,
            )
        )
    return out


def _collect_role_rights(base_path: str) -> list[tuple[str, str, str, str]]:
    """Collect role rights from all Roles directories.

    Returns list of (role_name, object_name, right_name, file_path).
    """

    base = Path(base_path)
    all_results: list[tuple[str, str, str, str]] = []

    # Find all rights files
    rights_files: list[tuple[str, Path]] = []

    # CF format: Roles/*/Ext/Rights.xml
    for f in base.glob("**/Roles/*/Ext/Rights.xml"):
        role_name = f.parent.parent.name
        rights_files.append((role_name, f))

    # EDT format: Roles/*/*.rights
    for f in base.glob("**/Roles/*/*.rights"):
        role_name = f.parent.name
        rights_files.append((role_name, f))

    def _process_rights_file(
        item: tuple[str, Path],
    ) -> list[tuple[str, str, str, str]]:
        role_name, f = item
        try:
            content = f.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError):
            return []
        rel = f.relative_to(base).as_posix()
        return _parse_role_rights_for_index(content, role_name, rel)

    if len(rights_files) > 1:
        workers = min(os.cpu_count() or 4, 8)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for batch in pool.map(_process_rights_file, rights_files):
                all_results.extend(batch)
    elif rights_files:
        all_results.extend(_process_rights_file(rights_files[0]))

    return all_results


# ---------------------------------------------------------------------------
# Object synonym collection (business-name search)
# ---------------------------------------------------------------------------


def _iter_metadata_xml_files(
    base_path: str,
    *,
    categories: frozenset[str] | None = None,
) -> list[tuple[str, str, str]]:
    """Discover layout-canonical metadata XML/MDO files for `_SYNONYM_CATEGORIES`.

    Same layout-discovery as ``_collect_object_synonyms`` (CF
    ``Cat/Obj/Ext/<Type>.xml`` → CF sibling ``Cat/Obj.xml`` → CF sibling-only
    ``Cat/<Name>.xml`` for EventSubscriptions → EDT ``Cat/Obj/Obj.mdo`` →
    Subsystems recursive), but WITHOUT parsing XML and WITHOUT the synonym
    filter. Used both by ``_collect_object_synonyms`` (which then parses
    synonyms) and by ``bsl_helpers`` extension-pass (which needs the raw
    locator list for XML-only ext objects without ``<Synonym>``).

    When *categories* is ``None`` (default) — all ``_SYNONYM_CATEGORIES`` are scanned.

    Returns list of (category, object_name, rel_path_posix).
    """
    base = Path(base_path)
    all_results: list[tuple[str, str, str]] = []

    def _emit(fp: Path, cat: str, obj_name: str, results: list[tuple[str, str, str]]) -> None:
        try:
            rel = fp.relative_to(base).as_posix()
        except ValueError:
            return
        results.append((cat, obj_name, rel))

    def _pick_object_metadata_xml(ext_dir: Path) -> Path | None:
        """Pick an object's metadata XML from its ``Ext/`` directory, deterministically.

        ``iterdir()`` order is filesystem-dependent, so we sort by name for
        reproducibility AND skip ``Predefined.xml`` — it holds predefined items,
        never the object's metadata (attributes/synonyms). Picking it as the
        object locator made ``find_attributes`` return ``[]`` on filesystems
        where ``Predefined.xml`` happened to sort before the object XML (this is
        a fallback path; real CF dumps use a sibling ``Obj.xml`` and EDT uses
        ``Obj.mdo``, both handled before this branch).
        """
        if not ext_dir.is_dir():
            return None
        for xml in sorted(ext_dir.iterdir(), key=lambda p: p.name):
            if xml.suffix.lower() == ".xml" and xml.name.lower() != "predefined.xml" and xml.is_file():
                return xml
        return None

    def _collect_category(cat: str) -> list[tuple[str, str, str]]:
        results: list[tuple[str, str, str]] = []
        cat_dir = base / cat
        if not cat_dir.is_dir():
            return results

        if cat == "Subsystems":
            _collect_subsystems_recursive(cat_dir, cat, results)
            return results

        seen: set[str] = set()
        for obj_dir in sorted(cat_dir.iterdir(), key=lambda p: p.name):
            if not obj_dir.is_dir():
                continue
            mdo = obj_dir / f"{obj_dir.name}.mdo"
            if mdo.is_file():
                _emit(mdo, cat, obj_dir.name, results)
                seen.add(obj_dir.name)
                continue
            sibling_xml = cat_dir / f"{obj_dir.name}.xml"
            if sibling_xml.is_file():
                _emit(sibling_xml, cat, obj_dir.name, results)
                seen.add(obj_dir.name)
                continue
            ext_xml = _pick_object_metadata_xml(obj_dir / "Ext")
            if ext_xml is not None:
                _emit(ext_xml, cat, obj_dir.name, results)
                seen.add(obj_dir.name)

        for fp in sorted(cat_dir.iterdir(), key=lambda p: p.name):
            if not fp.is_file() or fp.suffix.lower() != ".xml":
                continue
            obj_name = fp.stem
            if obj_name in seen:
                continue
            _emit(fp, cat, obj_name, results)
            seen.add(obj_name)

        return results

    def _collect_subsystems_recursive(
        parent_dir: Path,
        cat: str,
        results: list[tuple[str, str, str]],
    ) -> None:
        seen: set[str] = set()
        for obj_dir in sorted(parent_dir.iterdir(), key=lambda p: p.name):
            if not obj_dir.is_dir():
                continue
            mdo = obj_dir / f"{obj_dir.name}.mdo"
            if mdo.is_file():
                _emit(mdo, cat, obj_dir.name, results)
                seen.add(obj_dir.name)
            else:
                sibling_xml = parent_dir / f"{obj_dir.name}.xml"
                if sibling_xml.is_file():
                    _emit(sibling_xml, cat, obj_dir.name, results)
                    seen.add(obj_dir.name)
                else:
                    ext_xml = _pick_object_metadata_xml(obj_dir / "Ext")
                    if ext_xml is not None:
                        _emit(ext_xml, cat, obj_dir.name, results)
                        seen.add(obj_dir.name)
            nested = obj_dir / "Subsystems"
            if nested.is_dir():
                _collect_subsystems_recursive(nested, cat, results)

        for fp in sorted(parent_dir.iterdir(), key=lambda p: p.name):
            if not fp.is_file() or fp.suffix.lower() != ".xml":
                continue
            obj_name = fp.stem
            if obj_name in seen:
                continue
            _emit(fp, cat, obj_name, results)
            seen.add(obj_name)

    target_cats = categories if categories is not None else _SYNONYM_CATEGORIES
    cats_list = [c for c in target_cats if (base / c).is_dir()]
    if not cats_list:
        return all_results

    workers = min(os.cpu_count() or 4, 8)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for batch in pool.map(_collect_category, cats_list):
            all_results.extend(batch)

    return all_results


def _collect_object_synonyms(
    base_path: str,
    *,
    categories: frozenset[str] | None = None,
) -> list[tuple[str, str, str, str]]:
    """Collect object synonyms from metadata categories.

    When *categories* is ``None`` (default) — all ``_SYNONYM_CATEGORIES`` are scanned.
    When a specific frozenset is passed — only those categories are scanned.

    Returns list of (object_name, category, prefixed_synonym, rel_path).
    """
    from rlm_tools_bsl.bsl_xml_parsers import parse_metadata_xml

    base = Path(base_path)
    all_results: list[tuple[str, str, str, str]] = []

    candidates = _iter_metadata_xml_files(base_path, categories=categories)
    if not candidates:
        return all_results

    def _parse_one(entry: tuple[str, str, str]) -> tuple[str, str, str, str] | None:
        cat, obj_name, rel = entry
        fp = base / rel
        try:
            content = fp.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            return None
        try:
            parsed = parse_metadata_xml(content)
        except Exception as exc:
            logger.warning(
                "_collect_object_synonyms: skipping %s due to unexpected parse error: %s: %s",
                fp,
                type(exc).__name__,
                exc,
            )
            return None
        if parsed is None:
            # parse_metadata_xml глотает ParseError в None (Finding #5) — сохраняем warning,
            # иначе битый XML тихо пропадёт в `if not parsed` ниже.
            logger.warning(
                "_collect_object_synonyms: skipping malformed XML %s",
                fp,
            )
            return None
        if not parsed:
            return None
        raw_synonym = parsed.get("synonym") or ""
        if not raw_synonym:
            return None
        prefix = _CATEGORY_RU.get(cat, cat)
        synonym = f"{prefix}: {raw_synonym}"
        return (obj_name, cat, synonym, rel)

    workers = min(os.cpu_count() or 4, 8)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for parsed_row in pool.map(_parse_one, candidates):
            if parsed_row is not None:
                all_results.append(parsed_row)

    return all_results


# ---------------------------------------------------------------------------
# Prefix detection from index
# ---------------------------------------------------------------------------
_PREFIX_RE = re.compile(r"^([a-z\u0430-\u044f\u0451]+_?)")


def _detect_prefixes(conn: sqlite3.Connection) -> list[str]:
    """Detect custom prefixes from object_name in modules table.

    Uses the same heuristic as bsl_helpers._ensure_prefixes() but runs on
    already-indexed data (no I/O). Returns sorted list of frequent prefixes.
    """
    rows = conn.execute("SELECT DISTINCT object_name FROM modules WHERE object_name IS NOT NULL").fetchall()

    prefix_counts: dict[str, int] = {}
    for row in rows:
        name = row[0]
        if not name or not name[0].islower():
            continue
        m = _PREFIX_RE.match(name)
        if m:
            key = m.group(1).rstrip("_").lower()
            if len(key) >= 2:
                prefix_counts[key] = prefix_counts.get(key, 0) + 1

    # Keep prefixes appearing 3+ times
    frequent = sorted(
        ((k, v) for k, v in prefix_counts.items() if v >= 3),
        key=lambda x: -x[1],
    )
    return [k for k, _ in frequent]


# ---------------------------------------------------------------------------
# Level-8: extension overrides collector
# ---------------------------------------------------------------------------


def _collect_extension_overrides(
    base_path: str,
    conn: sqlite3.Connection,
) -> list[tuple]:
    """Collect extension override data for indexing.

    For MAIN configs: scans nearby extensions, links overrides to source modules.
    For EXTENSION configs: records overrides without source linking.
    Returns list of tuples ready for INSERT into extension_overrides.
    """
    from rlm_tools_bsl.extension_detector import (
        ConfigRole,
        detect_extension_context,
        find_extension_overrides,
    )

    try:
        ext_context = detect_extension_context(base_path)
    except Exception as exc:
        logger.warning("Extension detection failed for %s, overrides skipped: %s", base_path, exc)
        return []

    rows: list[tuple] = []

    def _lookup_source(override: dict) -> tuple[str, int | None, int | None]:
        """Resolve source module and method line from base config index."""
        module_path = override.get("module_path", "")
        object_name = override.get("object_name", "")
        module_type = override.get("module_type", "")
        target_method = override.get("target_method", "")

        source_path = ""
        source_module_id = None
        target_method_line = None

        # Primary lookup by rel_path
        if module_path:
            row = conn.execute(
                "SELECT id, rel_path FROM modules WHERE rel_path = ?",
                (module_path,),
            ).fetchone()
            if row:
                source_module_id = row[0]
                source_path = row[1]

        # Fallback by object_name + module_type
        if source_module_id is None and object_name and module_type:
            row = conn.execute(
                "SELECT id, rel_path FROM modules WHERE object_name = ? AND module_type = ? ORDER BY rel_path LIMIT 1",
                (object_name, module_type),
            ).fetchone()
            if row:
                source_module_id = row[0]
                source_path = row[1]

        # Lookup method line (Python-side case-insensitive for Cyrillic)
        if source_module_id is not None and target_method:
            rows = conn.execute(
                "SELECT name, line FROM methods WHERE module_id = ?",
                (source_module_id,),
            ).fetchall()
            target_lower = target_method.lower()
            for r in rows:
                if r[0].lower() == target_lower:
                    target_method_line = r[1]
                    break

        return source_path, source_module_id, target_method_line

    current = ext_context.current

    if current.role == ConfigRole.MAIN and ext_context.nearby_extensions:
        # Main config: scan each nearby extension
        for ext in ext_context.nearby_extensions:
            try:
                overrides = find_extension_overrides(ext.path)
            except Exception as exc:
                logger.warning("Override scan failed for extension %s: %s", ext.path, exc)
                continue
            for ov in overrides:
                source_path, source_module_id, target_method_line = _lookup_source(ov)
                rows.append(
                    (
                        ov.get("object_name", ""),
                        source_path,
                        source_module_id,
                        ov.get("target_method", ""),
                        target_method_line,
                        ov.get("annotation", ""),
                        ext.name,
                        ext.purpose or None,
                        ov.get("extension_method", ""),
                        ext.path,
                        ov.get("module_path", ""),
                        ov.get("line"),
                    )
                )

    elif current.role == ConfigRole.EXTENSION:
        # Extension config: record overrides without source linking
        try:
            overrides = find_extension_overrides(base_path)
        except Exception as exc:
            logger.warning("Override scan failed for extension %s: %s", base_path, exc)
            return []
        for ov in overrides:
            rows.append(
                (
                    ov.get("object_name", ""),
                    "",  # source_path
                    None,  # source_module_id
                    ov.get("target_method", ""),
                    None,  # target_method_line
                    ov.get("annotation", ""),
                    current.name,
                    current.purpose or None,
                    ov.get("extension_method", ""),
                    current.path,
                    ov.get("module_path", ""),
                    ov.get("line"),
                )
            )

    return rows


# ---------------------------------------------------------------------------
# Form elements collection (v1.6.0)
# ---------------------------------------------------------------------------


def _collect_form_elements(base_path: str) -> list[tuple]:
    """Collect form elements (handlers, commands, attributes) from all forms.

    Returns list of tuples ready for INSERT into form_elements:
    (object_name, category, form_name, kind, scope, element_name, element_type,
     event, handler, data_path, main_table, attribute_is_main, extra_json, file).
    """
    from rlm_tools_bsl.bsl_xml_parsers import parse_form_xml
    from rlm_tools_bsl.format_detector import METADATA_CATEGORIES

    base = Path(base_path)
    form_files: list[tuple[str, str, str, Path]] = []  # (category, object_name, form_name, path)

    # Discover forms in standard categories
    for cat in METADATA_CATEGORIES:
        cat_dir = base / cat
        if not cat_dir.is_dir():
            continue
        for obj_dir in cat_dir.iterdir():
            if not obj_dir.is_dir():
                continue
            forms_dir = obj_dir / "Forms"
            if not forms_dir.is_dir():
                continue
            for form_dir in forms_dir.iterdir():
                if not form_dir.is_dir():
                    continue
                # EDT: Form.form
                ff = form_dir / "Form.form"
                if ff.is_file():
                    form_files.append((cat, obj_dir.name, form_dir.name, ff))
                    continue
                # CF: Ext/Form.xml
                ff = form_dir / "Ext" / "Form.xml"
                if ff.is_file():
                    form_files.append((cat, obj_dir.name, form_dir.name, ff))

    # CommonForms (separate path structure)
    cf_dir = base / "CommonForms"
    if cf_dir.is_dir():
        for form_dir in cf_dir.iterdir():
            if not form_dir.is_dir():
                continue
            # EDT: Form.form (no intermediate Forms/)
            ff = form_dir / "Form.form"
            if ff.is_file():
                form_files.append(("CommonForms", form_dir.name, form_dir.name, ff))
                continue
            # CF: Ext/Form.xml
            ff = form_dir / "Ext" / "Form.xml"
            if ff.is_file():
                form_files.append(("CommonForms", form_dir.name, form_dir.name, ff))

    if not form_files:
        return []

    def _process_form(item: tuple[str, str, str, Path]) -> list[tuple]:
        cat, obj_name, frm_name, fpath = item
        try:
            content = fpath.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError):
            return []
        parsed = parse_form_xml(content)
        if parsed is None:
            return []
        rel = fpath.relative_to(base).as_posix()
        rows: list[tuple] = []
        for h in parsed.get("handlers", []):
            rows.append(
                (
                    obj_name,
                    cat,
                    frm_name,
                    "handler",
                    h.get("scope", ""),
                    h.get("element", ""),
                    h.get("element_type", ""),
                    h.get("event", ""),
                    h.get("handler", ""),
                    h.get("data_path", ""),
                    "",
                    0,
                    "",
                    rel,
                )
            )
        for c in parsed.get("commands", []):
            rows.append(
                (
                    obj_name,
                    cat,
                    frm_name,
                    "command",
                    "",
                    c.get("name", ""),
                    "",
                    "",
                    c.get("action", ""),
                    "",
                    "",
                    0,
                    "",
                    rel,
                )
            )
        for a in parsed.get("attributes", []):
            # v1.33.0: parse_form_xml отдаёт types СПИСКОМ. Колонка element_type — TEXT,
            # и передача списка дала бы sqlite3.InterfaceError на первой же форме.
            # Формат хранения НЕ меняется (v14 и v15 идентичны): ", ".join — и он же
            # единственный источник истины, дублировать types в extra_json не надо
            # (замер: 455 912 XML-узлов типов, значений со встроенной запятой — 0).
            types_list = a.get("types") or []
            if isinstance(types_list, str):  # толерантность к старому shape
                types_list = [t.strip() for t in types_list.split(",") if t.strip()]
            qt = a.get("query_text", "")
            extra = json.dumps({"query_text": qt}, ensure_ascii=False) if qt else ""
            rows.append(
                (
                    obj_name,
                    cat,
                    frm_name,
                    "attribute",
                    "",
                    a.get("name", ""),
                    ", ".join(types_list),
                    "",
                    "",
                    "",
                    a.get("main_table", ""),
                    1 if a.get("main") else 0,
                    extra,
                    rel,
                )
            )
        return rows

    all_results: list[tuple] = []
    if len(form_files) > 1:
        workers = min(os.cpu_count() or 4, 8)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for batch in pool.map(_process_form, form_files):
                all_results.extend(batch)
    elif form_files:
        all_results.extend(_process_form(form_files[0]))

    return all_results


_FORM_ELEMENTS_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS form_elements ("
    "id INTEGER PRIMARY KEY, "
    "object_name TEXT NOT NULL, "
    "category TEXT NOT NULL, "
    "form_name TEXT NOT NULL, "
    "kind TEXT NOT NULL, "
    "scope TEXT NOT NULL DEFAULT '', "
    "element_name TEXT NOT NULL DEFAULT '', "
    "element_type TEXT NOT NULL DEFAULT '', "
    "event TEXT NOT NULL DEFAULT '', "
    "handler TEXT NOT NULL DEFAULT '', "
    "data_path TEXT NOT NULL DEFAULT '', "
    "main_table TEXT NOT NULL DEFAULT '', "
    "attribute_is_main INTEGER DEFAULT 0, "
    "extra_json TEXT NOT NULL DEFAULT '', "
    "file TEXT NOT NULL);\n"
    "CREATE INDEX IF NOT EXISTS idx_fe_object ON form_elements(object_name COLLATE NOCASE);\n"
    "CREATE INDEX IF NOT EXISTS idx_fe_object_form ON form_elements(object_name, form_name);\n"
    "CREATE INDEX IF NOT EXISTS idx_fe_handler ON form_elements(handler COLLATE NOCASE);\n"
    "CREATE INDEX IF NOT EXISTS idx_fe_kind ON form_elements(kind);\n"
)

_FORM_ELEMENTS_INSERT = (
    "INSERT INTO form_elements "
    "(object_name, category, form_name, kind, scope, element_name, element_type, "
    "event, handler, data_path, main_table, attribute_is_main, extra_json, file) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


# ---------------------------------------------------------------------------
# IndexBuilder
# ---------------------------------------------------------------------------
class IndexBuilder:
    """Builds and incrementally updates the SQLite method index."""

    @staticmethod
    def _save_dirty_snapshot(
        conn: sqlite3.Connection,
        base_path: str,
        prefix: str,
        *,
        preserve_on_unreliable: bool,
    ) -> str | None:
        """Persist the git dirty snapshot together with its reliability flag.

        On a **reliable** detection: stores ``git_dirty_paths`` and clears
        ``git_dirty_unreliable`` (=0) so the next update may use the git fast path.

        On an **unreliable** detection (best-effort command timed out / failed):
        sets ``git_dirty_unreliable`` (=1) so the next update is forced into a
        correct full scan. When *preserve_on_unreliable* (update callsites, a
        previous good snapshot exists) the stale-but-good ``git_dirty_paths`` is
        kept intact rather than overwritten with a partial set; otherwise (fresh
        build, no prior snapshot) the partial set is stored as best-effort info.

        Returns the unreliable reason (or ``None`` when reliable).
        """
        result = _git_current_dirty(base_path, prefix)
        if result.unreliable_reason is not None:
            conn.execute("INSERT OR REPLACE INTO index_meta (key, value) VALUES ('git_dirty_unreliable', '1')")
            if not preserve_on_unreliable:
                conn.execute(
                    "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)",
                    ("git_dirty_paths", json.dumps(sorted(result.paths), ensure_ascii=False)),
                )
            logger.info("dirty snapshot unreliable (%s) — git_dirty_unreliable=1", result.unreliable_reason)
            return result.unreliable_reason
        conn.execute(
            "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)",
            ("git_dirty_paths", json.dumps(sorted(result.paths), ensure_ascii=False)),
        )
        conn.execute("INSERT OR REPLACE INTO index_meta (key, value) VALUES ('git_dirty_unreliable', '0')")
        return None

    # --- In-place rebuild support (v1.26.0, Finding #1) -------------------------
    # Keys preserved across the in-place schema clear: the marker itself + the
    # intended build options (recovery via _update_locked reads these — clearing
    # them would silently downgrade calls/metadata/fts/synonyms on a recovery build).
    _MARKER_PRESERVE_KEYS = (
        "build_in_progress",
        "has_calls",
        "has_metadata",
        "has_fts",
        "has_synonyms",
    )

    def _self_heal_db(self, db_path: Path) -> None:
        """Before an in-place rebuild, ensure db_path is absent OR a valid SQLite with a
        key/value ``index_meta`` (``key`` as PRIMARY KEY). A corrupt/incompatible file is
        unlinked & recreated — safe, because a healthy open ``IndexReader`` cannot exist
        on a corrupt file (no live handle to clash with). Cheap probe (``SELECT 1 FROM
        index_meta LIMIT 1`` + ``PRAGMA table_info`` pk check), NOT a full
        ``PRAGMA integrity_check`` (that would full-scan the whole DB on EVERY build)."""
        if not db_path.exists():
            return
        valid = False
        try:
            probe = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                probe.execute("SELECT 1 FROM index_meta LIMIT 1").fetchone()
                # table_info columns: (cid, name, type, notnull, dflt_value, pk).
                # `key` must be the PRIMARY KEY (pk > 0) — else INSERT OR REPLACE on the
                # marker could spawn duplicate rows with no row-order guarantee.
                cols = probe.execute("PRAGMA table_info(index_meta)").fetchall()
                valid = any(c[1] == "key" and c[5] > 0 for c in cols)
            finally:
                probe.close()
        except sqlite3.Error:
            valid = False
        if valid:
            return
        logger.warning("build: existing index at %s is invalid/incompatible — recreating", db_path)
        try:
            db_path.unlink()
        except OSError as exc:
            # Windows: a live handle (very unlikely on a corrupt file) blocks unlink.
            raise RuntimeError(
                f"Cannot self-heal corrupt index at {db_path}: {exc}. Close active sessions and retry."
            ) from exc

    def _drop_all_objects_except_meta(self, conn: sqlite3.Connection) -> None:
        """DROP every table/view/trigger/index except ``index_meta`` (FTS5-safe).

        Order: triggers → views → ordinary tables → fts5-virtual → leftover indexes.
        ``index_meta`` is preserved (it holds the build_in_progress marker). FTS5 virtual
        tables are detected by ``sql LIKE '%USING fts5%'`` and dropped BY NAME; their
        shadow tables (``<fts>_config/_data/_idx/_docsize/_content``) are excluded from
        the explicit list (they cascade from the virtual-table drop) — addressed by the
        found fts name, NOT a global suffix (codex round 12). ``IF EXISTS`` everywhere
        (anti-regression on sqlite_master ordering)."""
        rows = conn.execute(
            "SELECT name, type, sql FROM sqlite_master "
            "WHERE type IN ('table','view','trigger','index') "
            "AND name NOT LIKE 'sqlite_%' AND name != 'index_meta'"
        ).fetchall()
        fts_names = {name for (name, _type, sql) in rows if sql and "using fts5" in sql.lower()}
        shadows = {f"{fts}_{suf}" for fts in fts_names for suf in ("config", "data", "idx", "docsize", "content")}

        for name, type_, _sql in rows:
            if type_ == "trigger":
                conn.execute(f'DROP TRIGGER IF EXISTS "{name}"')
        for name, type_, _sql in rows:
            if type_ == "view":
                conn.execute(f'DROP VIEW IF EXISTS "{name}"')
        for name, type_, _sql in rows:
            if type_ == "table" and name not in fts_names and name not in shadows:
                conn.execute(f'DROP TABLE IF EXISTS "{name}"')
        for fts in fts_names:
            conn.execute(f'DROP TABLE IF EXISTS "{fts}"')  # cascades its shadow tables
        for name, type_, _sql in rows:
            if type_ == "index":
                conn.execute(f'DROP INDEX IF EXISTS "{name}"')

    def _begin_inplace_rebuild(self, conn: sqlite3.Connection, opts: dict[str, bool]) -> None:
        """In-place rebuild prologue (no unlink/rename → safe under an open RO reader on
        Windows). Order matters — covers the whole dangerous window:
          (a) ensure ``index_meta`` exists; (b) ``build_in_progress=1`` + intended build
              options (``has_*``) + **commit** (durable marker BEFORE any destructive op;
              recovery reads the right options); (c) DROP every object except
              ``index_meta`` (FTS5-safe); (d) ``executescript(_SCHEMA_SQL)`` recreates
              empty tables; (e) DELETE stale meta keys (built_at/bsl_count/paths_hash/
              version/config_*) PRESERVING the marker + build options → strict-check can't
              mistake a mid-fail for FRESH."""
        conn.execute("CREATE TABLE IF NOT EXISTS index_meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT OR REPLACE INTO index_meta (key, value) VALUES ('build_in_progress', '1')")
        for key, flag in (
            ("has_calls", opts["has_calls"]),
            ("has_metadata", opts["has_metadata"]),
            ("has_fts", opts["has_fts"]),
            ("has_synonyms", opts["has_synonyms"]),
        ):
            conn.execute(
                "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)",
                (key, "1" if flag else "0"),
            )
        conn.commit()
        self._drop_all_objects_except_meta(conn)
        conn.executescript(_SCHEMA_SQL)
        placeholders = ",".join("?" * len(self._MARKER_PRESERVE_KEYS))
        conn.execute(
            f"DELETE FROM index_meta WHERE key NOT IN ({placeholders})",  # noqa: S608 — fixed-tuple
            self._MARKER_PRESERVE_KEYS,
        )
        conn.commit()

    def _finish_inplace_rebuild(self, conn: sqlite3.Connection) -> None:
        """Success gate: clear the build_in_progress marker (data is now complete).
        Marker cleared ⇔ index usable (this is the ONLY place it is cleared)."""
        conn.execute("DELETE FROM index_meta WHERE key = 'build_in_progress'")
        conn.commit()

    def _finalize_build_best_effort(self, conn: sqlite3.Connection) -> None:
        """Commit any pending writes, then ANALYZE + VACUUM (best-effort), then clear the
        marker. The leading commit makes finalize SELF-CONTAINED — VACUUM cannot run inside
        an open transaction. (Verified empirically: callers already commit before this and
        ANALYZE runs in autocommit — ``conn.in_transaction`` is False after it — so VACUUM
        already succeeds today; the commit is defensive against a future caller leaving a
        transaction open, NOT a fix for a current failure.) ANALYZE/VACUUM affect only
        size/planner, never data validity; ``except sqlite3.Error`` only (must not mask
        KeyboardInterrupt/MemoryError). Marker cleared AFTER VACUUM = single success gate."""
        try:
            conn.commit()
        except sqlite3.Error as exc:
            logger.warning("finalize: pre-VACUUM commit failed (best-effort): %s", exc)
        for stmt in ("ANALYZE", "VACUUM"):
            try:
                conn.execute(stmt)
            except sqlite3.Error as exc:
                logger.warning("%s failed (best-effort): %s", stmt, exc)
        self._finish_inplace_rebuild(conn)

    def build(
        self,
        base_path: str,
        build_calls: bool = True,
        build_metadata: bool = True,
        build_fts: bool = True,
        build_synonyms: bool = True,
    ) -> Path:
        """Full build of the method index.

        Scans all .bsl files under base_path, extracts methods and optionally
        a heuristic call graph, and writes results to a SQLite database.

        Args:
            base_path: Root directory of the 1C configuration.
            build_calls: Whether to build the call graph.
            build_metadata: Whether to parse Level-2 metadata (ES/SJ/FO).
            build_fts: Whether to build FTS5 full-text search index.
            build_synonyms: Whether to build object synonyms table.

        Returns:
            Path to the created database file.
        """
        db_path = get_index_db_path(base_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)

        lock = _BuildLock(db_path)
        lock.acquire()
        try:
            return self._build_locked(base_path, db_path, build_calls, build_metadata, build_fts, build_synonyms)
        finally:
            lock.release()

    def _build_locked(
        self,
        base_path: str,
        db_path: Path,
        build_calls: bool,
        build_metadata: bool,
        build_fts: bool,
        build_synonyms: bool,
    ) -> Path:
        """Internal build with lock already acquired.

        In-place rebuild (v1.26.0): the DB file is NOT unlink/rename'd on the normal
        path — on Windows that PermissionErrors under an open RO ``IndexReader``. The
        schema is cleared IN PLACE under a ``build_in_progress`` marker (concurrent old
        readers degrade via ``_transient_safe``; new sessions are refused until done).
        Self-heal unlinks ONLY a corrupt/incompatible file (no healthy reader can hold
        it). M1: the connection lifecycle is wrapped in try/finally so a mid-build
        exception never leaks the handle / WAL."""
        # Self-heal: recreate only a corrupt/incompatible file. Normal path NEVER
        # unlinks → no Windows reader clash (the actual root of H1).
        self._self_heal_db(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path))
        try:
            return self._build_into_conn(
                conn, base_path, db_path, build_calls, build_metadata, build_fts, build_synonyms
            )
        finally:
            conn.close()

    def _build_into_conn(
        self,
        conn: sqlite3.Connection,
        base_path: str,
        db_path: Path,
        build_calls: bool,
        build_metadata: bool,
        build_fts: bool,
        build_synonyms: bool,
    ) -> Path:
        """Build into an already-open connection (the caller opened it and closes it in
        a finally — M1). In-place: set the marker + clear the schema before populating,
        and clear the marker only after ANALYZE/VACUUM (success gate)."""
        opts = {
            "has_calls": build_calls,
            "has_metadata": build_metadata,
            "has_fts": build_fts,
            "has_synonyms": build_synonyms,
        }
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")

        logger.info("Building method index for %s -> %s", base_path, db_path)
        t0 = time.time()

        # Discover all .bsl files
        base = Path(base_path)
        bsl_files = sorted(base.rglob("*.bsl"))
        total_files = len(bsl_files)
        logger.info("Found %d .bsl files", total_files)

        if total_files == 0:
            # Empty rebuild — same in-place marker lifecycle; populate empty tables +
            # file_paths + meta only (codex round 8: must NOT skip the cleanup, else stale
            # modules/methods/calls would survive the now-unlink-free rebuild).
            self._begin_inplace_rebuild(conn, opts)
            fp_rows = _collect_file_paths(base_path)
            _insert_file_paths(conn, fp_rows)
            self._write_meta(
                conn,
                base_path,
                0,
                _paths_hash([]),  # round 21: NOT "" — else check_index_strict false STALE on a valid empty index
                build_calls,
                build_metadata,
                build_fts=build_fts,
                file_paths_count=len(fp_rows),
                build_synonyms=build_synonyms,
            )
            conn.execute(
                "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)",
                ("has_extension_overrides", "0"),
            )
            conn.execute(
                "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)",
                ("extension_overrides_count", "0"),
            )
            conn.execute(
                "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)",
                ("has_form_elements", "0"),
            )
            conn.execute(
                "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)",
                ("form_elements_count", "0"),
            )
            # Save git HEAD so first update can use git fast path
            if _git_available(base_path):
                head = _git_head_sha(base_path)
                if head:
                    conn.execute(
                        "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)",
                        ("git_head_commit", head),
                    )
                    repo_info = _git_repo_info(base_path)
                    if repo_info:
                        _, pfx = repo_info
                        # Fresh build: no prior snapshot to preserve. On unreliable
                        # detection the flag forces a full scan on the first update.
                        self._save_dirty_snapshot(conn, base_path, pfx, preserve_on_unreliable=False)
            conn.commit()
            self._finalize_build_best_effort(conn)
            return db_path

        # Compute paths hash
        rel_paths = [Path(f).relative_to(base).as_posix() for f in bsl_files]
        paths_hash = _paths_hash(rel_paths)

        # Parallel processing
        results: list[tuple[BslFileInfo, float, int, list[dict], list[tuple[int, str, int]]]] = []
        workers = min(os.cpu_count() or 4, 8)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_process_single_file, fp, base_path, build_calls): fp for fp in bsl_files}
            done_count = 0
            for future in as_completed(futures):
                done_count += 1
                if done_count % 1000 == 0:
                    elapsed = time.time() - t0
                    rate = done_count / elapsed if elapsed > 0 else 0
                    logger.info(
                        "Progress: %d/%d files (%.0f files/sec)",
                        done_count,
                        total_files,
                        rate,
                    )
                result = future.result()
                if result is not None:
                    results.append(result)

        # Write to SQLite — in-place: set the build_in_progress marker + clear the schema
        # before populating (conn is opened AND closed by the caller — M1 try/finally).
        self._begin_inplace_rebuild(conn, opts)

        self._bulk_insert(conn, results, build_calls)
        self._refresh_call_resolution_stats(conn)

        # Level-1 metadata: Configuration XML
        config_meta = _parse_configuration_meta(base_path)

        # Level-2 metadata: ES, SJ, FO
        if build_metadata:
            md_tables = _collect_metadata_tables(base_path)
            _insert_metadata_tables(conn, md_tables)

        # Level-3: register movements (in-band, already extracted)
        # Строки source='manager_code' проходят через каталог регистров: извлечение
        # из ManagerModule статически неоднозначно, и без сверки в таблицу попали бы
        # имена полей набора записей.
        known_registers = _known_register_names(base)
        all_movements: list[tuple[str, str, str, str]] = []
        dropped_manager = 0
        for r in results:
            if r.movements and r.info.object_name:
                for reg_name, source, file_path_str in r.movements:
                    if source == "manager_code" and reg_name.lower() not in known_registers:
                        dropped_manager += 1
                        continue
                    all_movements.append((r.info.object_name, reg_name, source, file_path_str))
        if all_movements:
            conn.executemany(
                "INSERT INTO register_movements (document_name, register_name, source, file) VALUES (?, ?, ?, ?)",
                all_movements,
            )
            conn.commit()
            logger.info(
                "Register movements: %d entries (manager_code rows dropped by register catalog: %d)",
                len(all_movements),
                dropped_manager,
            )

        # Level-3: role rights (parallel regex parsing)
        role_rights = _collect_role_rights(base_path)
        if role_rights:
            conn.executemany(
                "INSERT INTO role_rights (role_name, object_name, right_name, file) VALUES (?, ?, ?, ?)",
                role_rights,
            )
            conn.commit()
            logger.info("Role rights: %d entries from %d roles", len(role_rights), len(set(r[0] for r in role_rights)))
            # v1.9.0: emit role_rights into metadata_references (Roles category)
            rr_refs = _role_rights_to_references(role_rights)
            if rr_refs:
                try:
                    conn.executemany(
                        "INSERT INTO metadata_references "
                        "(source_object, source_category, ref_object, ref_kind, used_in, path, line) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        rr_refs,
                    )
                except sqlite3.OperationalError:
                    pass

        # Level-4: file navigation index (.bsl/.mdo/.xml paths)
        file_paths_rows = _collect_file_paths(base_path)
        _insert_file_paths(conn, file_paths_rows)
        conn.commit()
        logger.info("File paths: %d entries", len(file_paths_rows))

        # FTS5 full-text search index for methods
        if build_fts:
            conn.execute("CREATE VIRTUAL TABLE methods_fts USING fts5(name, object_name, tokenize='trigram')")
            conn.execute(
                "INSERT INTO methods_fts(rowid, name, object_name) "
                "SELECT m.id, m.name, mod.object_name "
                "FROM methods m JOIN modules mod ON mod.id = m.module_id"
            )

        # Level-6: object synonyms (business-name search)
        if build_synonyms:
            synonyms = _collect_object_synonyms(base_path)
            if synonyms:
                conn.executemany(
                    "INSERT INTO object_synonyms (object_name, category, synonym, file) VALUES (?, ?, ?, ?)",
                    synonyms,
                )
                conn.commit()
                logger.info("Object synonyms: %d entries", len(synonyms))

        # Level-8: extension overrides
        override_rows = _collect_extension_overrides(base_path, conn)
        if override_rows:
            conn.executemany(
                "INSERT INTO extension_overrides "
                "(object_name, source_path, source_module_id, target_method, "
                "target_method_line, annotation, extension_name, extension_purpose, "
                "extension_method, extension_root, ext_module_path, ext_line) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                override_rows,
            )
            conn.commit()
            logger.info("Extension overrides: %d entries", len(override_rows))
        conn.execute(
            "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)",
            ("has_extension_overrides", "1" if override_rows else "0"),
        )
        conn.execute(
            "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)",
            ("extension_overrides_count", str(len(override_rows))),
        )

        # Level-9: form elements (handlers, commands, attributes)
        fe_rows: list[tuple] = []
        if build_metadata:
            conn.executescript(_FORM_ELEMENTS_SCHEMA)
            fe_rows = _collect_form_elements(base_path)
            if fe_rows:
                conn.executemany(_FORM_ELEMENTS_INSERT, fe_rows)
                conn.commit()
                logger.info("Form elements: %d entries", len(fe_rows))
        conn.execute(
            "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)",
            ("has_form_elements", "1" if build_metadata else "0"),
        )
        conn.execute(
            "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)",
            ("form_elements_count", str(len(fe_rows))),
        )

        # Level-11: object attributes and predefined items meta
        oa_count = len(md_tables.get("object_attributes", [])) if build_metadata else 0
        pi_count = len(md_tables.get("predefined_items", [])) if build_metadata else 0
        conn.execute(
            "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)",
            ("object_attributes_count", str(oa_count)),
        )
        conn.execute(
            "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)",
            ("predefined_items_count", str(pi_count)),
        )

        # Detect custom prefixes from object names in index
        detected_prefixes = _detect_prefixes(conn)

        self._write_meta(
            conn,
            base_path,
            total_files,
            paths_hash,
            build_calls,
            build_metadata,
            config_meta,
            build_fts,
            detected_prefixes=detected_prefixes,
            file_paths_count=len(file_paths_rows),
            build_synonyms=build_synonyms,
        )

        # Save git HEAD commit so first update can use git fast path
        if _git_available(base_path):
            head = _git_head_sha(base_path)
            if head:
                conn.execute(
                    "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)",
                    ("git_head_commit", head),
                )
                # Save dirty snapshot for dirty→clean detection.
                # Fresh build: no prior snapshot to preserve.
                repo_info = _git_repo_info(base_path)
                if repo_info:
                    _, pfx = repo_info
                    self._save_dirty_snapshot(conn, base_path, pfx, preserve_on_unreliable=False)
                conn.commit()

        # ANALYZE + VACUUM (best-effort) then clear the marker (success gate). conn is
        # closed by the caller (_build_locked) in a finally — M1.
        self._finalize_build_best_effort(conn)

        elapsed = time.time() - t0
        total_methods = sum(len(r.methods) for r in results)
        total_calls = sum(len(r.raw_calls) for r in results)
        logger.info(
            "Index built: %d modules, %d methods, %d calls in %.1fs (%.0f files/sec)",
            len(results),
            total_methods,
            total_calls,
            elapsed,
            total_files / elapsed if elapsed > 0 else 0,
        )

        return db_path

    def update(self, base_path: str) -> dict:
        """Incremental update by mtime+size delta.

        Returns:
            dict with keys: added, changed, removed (counts).
        """
        db_path = get_index_db_path(base_path)
        if not db_path.exists():
            raise FileNotFoundError(f"Index not found: {db_path}")

        lock = _BuildLock(db_path)
        lock.acquire()
        conn = None
        try:
            # connect INSIDE the try (codex): a connect-time sqlite3.Error/OSError must
            # NOT leak the acquired lock — otherwise it stays in _active_locks and blocks
            # later builds/updates in the same process.
            conn = sqlite3.connect(str(db_path))  # M1: closed in finally
            return self._update_locked(base_path, db_path, conn)
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            lock.release()

    def _update_locked(self, base_path: str, db_path: Path, conn: sqlite3.Connection) -> dict:
        """Internal update with lock held and the connection opened by the caller
        (M1: ``update()`` closes it in a finally). Existing explicit ``conn.close()``
        calls before each return remain (idempotent double-close — the safety net only
        fires on an exception path)."""
        t0 = time.time()
        base = Path(base_path)

        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")

        # Recovery: a prior in-place build left build_in_progress=1 → meta/tables are
        # untrustworthy. Do NOT increment a partial index — full rebuild (mirrors the
        # schema-upgrade branch below: close conn → self.build → same return shape).
        # Recover the intended build options only if ALL FOUR has_* are present AND
        # valid 0/1 (codex round 6/18); otherwise full-feature + warning.
        bip_row = conn.execute("SELECT value FROM index_meta WHERE key = 'build_in_progress'").fetchone()
        if bip_row is not None and bip_row["value"] == "1":
            opt_flags: dict[str, bool] = {}
            valid = True
            for key in ("has_calls", "has_metadata", "has_fts", "has_synonyms"):
                row = conn.execute("SELECT value FROM index_meta WHERE key = ?", (key,)).fetchone()
                if row is None or row["value"] not in ("0", "1"):
                    valid = False
                    break
                opt_flags[key] = row["value"] == "1"
            if not valid:
                logger.warning("update: incomplete prior build lacks valid has_* options → full-feature rebuild")
                opt_flags = {"has_calls": True, "has_metadata": True, "has_fts": True, "has_synonyms": True}
            bsl_files = sorted(base.rglob("*.bsl"))
            conn.close()
            self.build(
                base_path,
                build_calls=opt_flags["has_calls"],
                build_metadata=opt_flags["has_metadata"],
                build_fts=opt_flags["has_fts"],
                build_synonyms=opt_flags["has_synonyms"],
            )
            return {
                "added": len(bsl_files),
                "changed": 0,
                "removed": 0,
                "git_fast_path": False,
                "rebuild_reason": "incomplete prior build",
            }

        # Read build settings from meta
        meta_row = conn.execute("SELECT value FROM index_meta WHERE key = 'has_calls'").fetchone()
        build_calls = meta_row is not None and meta_row["value"] == "1"

        meta_row = conn.execute("SELECT value FROM index_meta WHERE key = 'has_metadata'").fetchone()
        has_metadata = meta_row is not None and meta_row["value"] == "1"

        meta_row = conn.execute("SELECT value FROM index_meta WHERE key = 'has_fts'").fetchone()
        has_fts = meta_row is not None and meta_row["value"] == "1"

        # has_synonyms: default=True when key missing (v6→v7 migration)
        meta_row = conn.execute("SELECT value FROM index_meta WHERE key = 'has_synonyms'").fetchone()
        has_synonyms = meta_row is None or meta_row["value"] == "1"

        # Schema upgrades that add code-derived tables/columns (regions/
        # module_headers in v8, metadata_code_usages in v13, calls.callee_key in
        # v14) cannot be back-filled incrementally — they require a one-time full
        # rebuild that recreates the schema. Threshold = newest such version.
        # NB: a plain version bump without this forced rebuild would route v13
        # DBs into the version-mismatch full *scan* below, which INSERTs into the
        # OLD schema (no callee_key column) and fails — so the rebuild is required.
        #
        # v15 — миграция СОДЕРЖИМОГО, а не схемы, но механизм тот же: полный СКАН
        # считает дельту по mtime/size и нетронутые модули не перечитывает, поэтому
        # без подъёма порога старые methods / register_movements / form_elements /
        # metadata_references пережили бы бамп вместе с призраками из комментариев.
        meta_row = conn.execute("SELECT value FROM index_meta WHERE key = 'builder_version'").fetchone()
        old_version = int(meta_row["value"]) if meta_row else 0
        if old_version < 15:
            # Need disk scan for the return count
            bsl_files = sorted(base.rglob("*.bsl"))
            logger.info(
                "Upgrading index v%d → v%d: full rebuild required for code-derived tables",
                old_version,
                BUILDER_VERSION,
            )
            conn.close()
            self.build(
                base_path,
                build_calls=build_calls,
                build_metadata=has_metadata,
                build_fts=has_fts,
                build_synonyms=has_synonyms,
            )
            return {
                "added": len(bsl_files),
                "changed": 0,
                "removed": 0,
                "git_fast_path": False,
                "rebuild_reason": f"schema upgrade v{old_version}->{BUILDER_VERSION}",
            }

        # Self-heal perf indexes (idx_calls_callee_short, idx_meth_module) on EVERY
        # v14+ update (no-op, metadata-only, git-fast, full-scan all pass this
        # point). Unconditional — NOT under `if bsl_changed:` — so a v14 DB built
        # before an index existed gets it even on a no-op update (Codex round-3).
        # Build paths already have them via _SCHEMA_SQL. old_version >= 14 is
        # guaranteed here (the < 14 branch above returned), so the tables have
        # their final v14 shape.
        with conn:
            self._ensure_callee_short_index(conn)
            self._ensure_meth_module_index(conn)
            # Guarantee calls.callee_key exists before the delta-independent
            # re-resolve migration below (the BSL-delta branches do this too, but
            # the once-migration runs even on a no-op update).
            self._ensure_callee_key_column(conn)

        # One-time in-place cleanup of bare-global call noise for legacy indexes
        # built before the extraction filter existed. Idempotent (meta flag),
        # runs only on update/build — never on read-only rlm_start. Placed AFTER
        # the schema-upgrade early return (rebuild is already clean) but BEFORE
        # fast-path / full-scan branching so it fires regardless of BSL delta.
        purged = self._purge_call_noise_once(conn)
        if purged:
            logger.info(
                "Purged %d bare-global noise rows from calls (legacy index migration)",
                purged,
            )

        # One-time GLOBAL re-resolve of qualified callee_key for legacy v14 indexes
        # that accumulated drift before the per-delta re-resolve existed. Idempotent
        # (meta flag), delta-independent (same rationale as the noise purge above),
        # no schema change. Per-delta re-resolve below prevents NEW drift going forward.
        reresolved = self._reresolve_qualified_callers_once(conn)
        if reresolved:
            logger.info(
                "Re-resolved %d stale qualified callee_key (legacy index migration)",
                reresolved,
            )

        # --- Git fast path attempt ---
        version_mismatch = old_version != BUILDER_VERSION and old_version >= 8
        # A previously-unreliable dirty snapshot means uncommitted changes may
        # never have been indexed → force a correct full scan to catch up.
        unreliable_row = conn.execute("SELECT value FROM index_meta WHERE key = 'git_dirty_unreliable'").fetchone()
        prev_unreliable = unreliable_row is not None and unreliable_row["value"] == "1"
        force_full_scan = version_mismatch or prev_unreliable
        fallback_reason = ""
        # A git-tracked index (stored commit present) — used to flag a
        # suspiciously high dirty ratio in the full-scan branch as a probable EOL
        # mismatch rather than silent slowness. Read from meta (cheap, no git
        # call) so it also covers the version-mismatch / prev-unreliable paths.
        stored_commit_row = conn.execute("SELECT value FROM index_meta WHERE key = 'git_head_commit'").fetchone()
        stored_commit = stored_commit_row["value"] if stored_commit_row else None
        git_tracked = stored_commit is not None

        if not force_full_scan:
            if stored_commit and _git_available(base_path):
                repo_info = _git_repo_info(base_path)
                if repo_info:
                    git_root, prefix = repo_info
                    git_head = _git_head_sha(base_path)
                    git_result = _git_changed_files(base_path, stored_commit, prefix)

                    if git_result is None or git_head is None:
                        fallback_reason = "git error" if git_result is None else "head sha error"
                    elif git_result.unreliable_reason is not None:
                        # Best-effort detection timed out — the change set is
                        # partial and must NOT be trusted. Fall through to a full
                        # scan; the unreliable flag is (re)persisted there.
                        fallback_reason = git_result.unreliable_reason
                    else:
                        git_changed = git_result.paths
                        # Merge previously-dirty files into delta
                        stored_dirty_row = conn.execute(
                            "SELECT value FROM index_meta WHERE key = 'git_dirty_paths'"
                        ).fetchone()
                        if stored_dirty_row:
                            prev_dirty = set(json.loads(stored_dirty_row["value"]))
                            git_changed |= prev_dirty

                        delta = self._update_git_fast(
                            conn,
                            base_path,
                            base,
                            git_changed,
                            git_head,
                            prefix,
                            build_calls,
                            has_metadata,
                            has_fts,
                            has_synonyms,
                        )
                        conn.commit()
                        conn.execute("ANALYZE")
                        conn.close()
                        elapsed = time.time() - t0
                        logger.info("Git fast path update done in %.1fs", elapsed)
                        return {**delta, "git_fast_path": True, "git_fallback_reason": None}
                else:
                    fallback_reason = "repo info error"
            elif not stored_commit:
                fallback_reason = "no stored commit"
            else:
                fallback_reason = "no git"
        elif version_mismatch:
            fallback_reason = "builder version mismatch"
        else:  # prev_unreliable
            fallback_reason = "prev dirty unreliable"

        if fallback_reason:
            logger.info("Git fast path unavailable (%s), using full scan", fallback_reason)

        # --- Fallback: full scan (original logic) ---
        bsl_files = sorted(base.rglob("*.bsl"))
        disk_files: dict[str, Path] = {}
        for fp in bsl_files:
            rel = fp.relative_to(base).as_posix()
            disk_files[rel] = fp

        # Existing modules in DB
        db_modules: dict[str, dict] = {}
        for row in conn.execute("SELECT id, rel_path, mtime, size FROM modules"):
            db_modules[row["rel_path"]] = {
                "id": row["id"],
                "mtime": row["mtime"],
                "size": row["size"],
            }

        # Compute delta
        disk_set = set(disk_files.keys())
        db_set = set(db_modules.keys())

        added_paths = disk_set - db_set
        removed_paths = db_set - disk_set
        common_paths = disk_set & db_set

        changed_paths: set[str] = set()
        for rel in common_paths:
            fp = disk_files[rel]
            try:
                st = fp.stat()
                db_info = db_modules[rel]
                if abs(st.st_mtime - db_info["mtime"]) > 1.0 or st.st_size != db_info["size"]:
                    changed_paths.add(rel)
            except OSError:
                changed_paths.add(rel)

        # EOL-mismatch heuristic: in a git repo a full scan that finds most
        # tracked modules "changed" by size/mtime almost always means the index
        # was built with LF but the work-tree is CRLF (or vice-versa). Surface
        # the likely cause instead of letting it look like silent slowness.
        if git_tracked and len(common_paths) >= 20 and len(changed_paths) > 0.5 * len(common_paths):
            logger.warning(
                "Suspiciously high dirty ratio (%d/%d modules changed): possible EOL mismatch "
                "(index LF vs worktree CRLF). Consider normalizing line endings via "
                ".gitattributes (* text=auto eol=lf) or core.autocrlf so git can skip rehashing.",
                len(changed_paths),
                len(common_paths),
            )

        to_remove = removed_paths | changed_paths
        to_add = added_paths | changed_paths

        logger.info(
            "Incremental update: %d added, %d changed, %d removed",
            len(added_paths),
            len(changed_paths),
            len(removed_paths),
        )

        bsl_changed = bool(to_remove or to_add)

        # Process BSL delta (modules, methods, calls, movements)
        if bsl_changed:
            results: list[FileResult] = []
            if to_add:
                workers = min(os.cpu_count() or 4, 8)
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    futures = {
                        pool.submit(
                            _process_single_file,
                            disk_files[rel],
                            base_path,
                            build_calls,
                        ): rel
                        for rel in to_add
                        if rel in disk_files
                    }
                    for future in as_completed(futures):
                        result = future.result()
                        if result is not None:
                            results.append(result)

            # Casefolded names of common modules touched by this delta — drives the
            # per-delta re-resolve of qualified callee_key on UNCHANGED callers.
            # NEW names from results; OLD names queried from the DB before delete.
            changed_common_cf: set[str] = {
                r.info.object_name.casefold()
                for r in results
                if r.info.category == "CommonModules" and r.info.module_type == "Module" and r.info.object_name
            }

            with conn:
                # Delete old data for removed + changed
                if to_remove:
                    # metadata_code_usages has no module_id index (weight) — batch
                    # delete by chunk to stay under the SQLite variable limit.
                    removed_ids = [db_modules[rel]["id"] for rel in to_remove if rel in db_modules]
                    # Old common-module names for the call-graph re-resolve — only
                    # needed when calls are built; chunk to stay under the SQLite
                    # variable limit (like the metadata_code_usages delete below).
                    if build_calls and removed_ids:
                        for chunk in _chunked(removed_ids):
                            ph = ",".join("?" * len(chunk))
                            for row in conn.execute(
                                f"SELECT object_name FROM modules WHERE id IN ({ph}) "  # noqa: S608
                                "AND category='CommonModules' AND module_type='Module' "
                                "AND object_name IS NOT NULL",
                                chunk,
                            ):
                                old_name = row[0] if not isinstance(row, sqlite3.Row) else row["object_name"]
                                changed_common_cf.add(old_name.casefold())
                    for chunk in _chunked(removed_ids):
                        placeholders = ",".join("?" * len(chunk))
                        try:
                            conn.execute(
                                f"DELETE FROM metadata_code_usages WHERE module_id IN ({placeholders})",
                                chunk,
                            )
                        except sqlite3.OperationalError:
                            pass  # table may not exist in pre-v13 index
                    for rel in to_remove:
                        mod_info = db_modules.get(rel)
                        if mod_info is None:
                            continue
                        mod_id = mod_info["id"]
                        method_ids = [
                            row[0] for row in conn.execute("SELECT id FROM methods WHERE module_id = ?", (mod_id,))
                        ]
                        if method_ids:
                            placeholders = ",".join("?" * len(method_ids))
                            conn.execute(
                                f"DELETE FROM calls WHERE caller_id IN ({placeholders})",
                                method_ids,
                            )
                            if has_fts:
                                conn.execute(
                                    f"DELETE FROM methods_fts WHERE rowid IN ({placeholders})",
                                    method_ids,
                                )
                        conn.execute("DELETE FROM methods WHERE module_id = ?", (mod_id,))
                        # Clean up regions and module_headers (no FK cascade)
                        try:
                            conn.execute("DELETE FROM regions WHERE module_id = ?", (mod_id,))
                        except sqlite3.OperationalError:
                            pass  # table may not exist in v7 index
                        try:
                            conn.execute("DELETE FROM module_headers WHERE module_id = ?", (mod_id,))
                        except sqlite3.OperationalError:
                            pass  # table may not exist in v7 index
                        conn.execute("DELETE FROM modules WHERE id = ?", (mod_id,))

                # Insert new data
                self._ensure_callee_key_column(conn)
                self._bulk_insert(conn, results, build_calls)
                # Re-resolve qualified callee_key on unchanged callers of common
                # modules touched by this delta (keeps update ≡ build).
                if build_calls:
                    self._reresolve_qualified_callers(conn, changed_common_cf)
                self._refresh_call_resolution_stats(conn)

                # Update FTS for newly inserted methods
                if has_fts and results:
                    new_rel_paths = [r.info.relative_path for r in results]
                    placeholders = ",".join("?" * len(new_rel_paths))
                    conn.execute(
                        f"INSERT INTO methods_fts(rowid, name, object_name) "
                        f"SELECT m.id, m.name, mod.object_name "
                        f"FROM methods m JOIN modules mod ON mod.id = m.module_id "
                        f"WHERE mod.rel_path IN ({placeholders})",
                        new_rel_paths,
                    )

                # Update register_movements for changed/added Document modules
                # `or to_remove`: при ЧИСТОМ удалении модуля results пуст, и прежний
                # guard `if results:` оставлял его движения в таблице сиротами навсегда.
                if results or to_remove:
                    # Удаление идёт по ФАЙЛУ, а не по имени документа. Удаление по
                    # document_name сносило строки ОБОИХ модулей документа, а
                    # восстанавливались только строки переобработанного: правка
                    # ObjectModule стирала erp_mechanism/manager_table/adapted/manager_code
                    # из НЕтронутого ManagerModule и наоборот. Дефект пре-существующий
                    # (воспроизводится и на источниках v14), но с появлением manager_code
                    # он стал массовым: у большинства документов с записью из менеджера
                    # есть и ObjectModule. `to_remove` = removed | changed, поэтому
                    # удалённый модуль тоже чистится, а нетронутый сосед остаётся цел.
                    stale_files = set(to_remove) | {r.info.relative_path for r in results}
                    for stale_rel in stale_files:
                        try:
                            conn.execute(
                                "DELETE FROM register_movements WHERE file = ?",
                                (stale_rel,),
                            )
                        except sqlite3.OperationalError:
                            pass
                    # Каталог регистров считаем ОДИН раз перед циклом (тот же фильтр,
                    # что и на полной сборке — иначе инкремент вернул бы мусорные имена).
                    known_registers = _known_register_names(base)
                    new_movements: list[tuple[str, str, str, str]] = []
                    for r in results:
                        if r.movements and r.info.object_name:
                            for reg_name, source, fpath in r.movements:
                                if source == "manager_code" and reg_name.lower() not in known_registers:
                                    continue
                                new_movements.append((r.info.object_name, reg_name, source, fpath))
                    if new_movements:
                        conn.executemany(
                            "INSERT INTO register_movements "
                            "(document_name, register_name, source, file) VALUES (?, ?, ?, ?)",
                            new_movements,
                        )

                # Update meta
                new_paths_hash = _paths_hash(sorted(disk_files.keys()))
                conn.execute(
                    "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)",
                    ("bsl_count", str(len(disk_files))),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)",
                    ("paths_hash", new_paths_hash),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)",
                    ("built_at", str(time.time())),
                )

        # Refresh Level-1 metadata (config version may have changed)
        config_meta = _parse_configuration_meta(base_path)
        for key, value in config_meta.items():
            conn.execute(
                "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)",
                (key, value),
            )

        # Level-13: reverse code-usages table (code-derived, built regardless of
        # metadata). Defensive create — a pre-v13 index would have been fully
        # rebuilt above (old_version < 13), so this only guards same-version DBs.
        conn.executescript(
            "CREATE TABLE IF NOT EXISTS metadata_code_usages ("
            "id INTEGER PRIMARY KEY, module_id INTEGER NOT NULL REFERENCES modules(id), "
            "object_ref TEXT NOT NULL, object_ref_key TEXT NOT NULL, "
            "member_path TEXT, usage_kind TEXT NOT NULL, line INTEGER);\n"
            "CREATE INDEX IF NOT EXISTS idx_mcu_ref ON metadata_code_usages(object_ref_key);\n"
        )

        # Refresh Level-2 metadata if originally built with metadata
        if has_metadata:
            # Ensure tables exist (in case of schema upgrade)
            conn.executescript(
                "CREATE TABLE IF NOT EXISTS event_subscriptions ("
                "id INTEGER PRIMARY KEY, name TEXT NOT NULL, synonym TEXT, "
                "event TEXT, handler_module TEXT, handler_procedure TEXT, "
                "source_types TEXT, source_count INTEGER, file TEXT);\n"
                "CREATE TABLE IF NOT EXISTS scheduled_jobs ("
                "id INTEGER PRIMARY KEY, name TEXT NOT NULL, synonym TEXT, "
                "method_name TEXT, handler_module TEXT, handler_procedure TEXT, "
                "use INTEGER DEFAULT 1, predefined INTEGER DEFAULT 0, "
                "restart_count INTEGER DEFAULT 0, restart_interval INTEGER DEFAULT 0, file TEXT);\n"
                "CREATE TABLE IF NOT EXISTS functional_options ("
                "id INTEGER PRIMARY KEY, name TEXT NOT NULL, synonym TEXT, "
                "location TEXT, content TEXT, file TEXT);\n"
                "CREATE TABLE IF NOT EXISTS enum_values ("
                "id INTEGER PRIMARY KEY, name TEXT NOT NULL, synonym TEXT, "
                "values_json TEXT NOT NULL, source_file TEXT NOT NULL);\n"
                "CREATE INDEX IF NOT EXISTS idx_enum_name ON enum_values(name COLLATE NOCASE);\n"
                "CREATE TABLE IF NOT EXISTS subsystem_content ("
                "id INTEGER PRIMARY KEY, subsystem_name TEXT NOT NULL, "
                "subsystem_synonym TEXT, object_ref TEXT NOT NULL, file TEXT NOT NULL);\n"
                "CREATE INDEX IF NOT EXISTS idx_sc_object ON subsystem_content(object_ref COLLATE NOCASE);\n"
                "CREATE INDEX IF NOT EXISTS idx_sc_subsystem ON subsystem_content(subsystem_name COLLATE NOCASE);\n"
            )
            # Level-5 integration tables (schema upgrade v5→v6)
            conn.executescript(
                "CREATE TABLE IF NOT EXISTS http_services ("
                "id INTEGER PRIMARY KEY, name TEXT NOT NULL, "
                "root_url TEXT NOT NULL, templates_json TEXT NOT NULL, "
                "file TEXT NOT NULL);\n"
                "CREATE INDEX IF NOT EXISTS idx_hs_name ON http_services(name COLLATE NOCASE);\n"
                "CREATE TABLE IF NOT EXISTS web_services ("
                "id INTEGER PRIMARY KEY, name TEXT NOT NULL, "
                "namespace TEXT NOT NULL, operations_json TEXT NOT NULL, "
                "file TEXT NOT NULL);\n"
                "CREATE INDEX IF NOT EXISTS idx_ws_name ON web_services(name COLLATE NOCASE);\n"
                "CREATE TABLE IF NOT EXISTS xdto_packages ("
                "id INTEGER PRIMARY KEY, name TEXT NOT NULL, "
                "namespace TEXT NOT NULL, types_json TEXT NOT NULL, "
                "file TEXT NOT NULL);\n"
                "CREATE INDEX IF NOT EXISTS idx_xp_name ON xdto_packages(name COLLATE NOCASE);\n"
                "CREATE INDEX IF NOT EXISTS idx_xp_ns ON xdto_packages(namespace);\n"
            )
            # Level-11: object_attributes and predefined_items (schema upgrade)
            conn.executescript(
                "CREATE TABLE IF NOT EXISTS object_attributes ("
                "id INTEGER PRIMARY KEY, "
                "object_name TEXT NOT NULL, category TEXT NOT NULL, "
                "attr_name TEXT NOT NULL, attr_synonym TEXT, "
                "attr_type TEXT, attr_kind TEXT NOT NULL, "
                "ts_name TEXT, source_file TEXT NOT NULL);\n"
                "CREATE INDEX IF NOT EXISTS idx_oa_object ON object_attributes(object_name COLLATE NOCASE);\n"
                "CREATE INDEX IF NOT EXISTS idx_oa_attr ON object_attributes(attr_name COLLATE NOCASE);\n"
                "CREATE INDEX IF NOT EXISTS idx_oa_category ON object_attributes(category);\n"
                "CREATE TABLE IF NOT EXISTS predefined_items ("
                "id INTEGER PRIMARY KEY, "
                "object_name TEXT NOT NULL, category TEXT NOT NULL, "
                "item_name TEXT NOT NULL, item_synonym TEXT, "
                "item_code TEXT, types_json TEXT, "
                "is_folder INTEGER DEFAULT 0, source_file TEXT NOT NULL);\n"
                "CREATE INDEX IF NOT EXISTS idx_pi_object ON predefined_items(object_name COLLATE NOCASE);\n"
                "CREATE INDEX IF NOT EXISTS idx_pi_item ON predefined_items(item_name COLLATE NOCASE);\n"
                "CREATE INDEX IF NOT EXISTS idx_pi_synonym ON predefined_items(item_synonym COLLATE NOCASE);\n"
            )
            # Level-12 (v1.9.0): metadata_references + 3 specialised tables
            conn.executescript(
                "CREATE TABLE IF NOT EXISTS metadata_references ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "source_object TEXT NOT NULL, source_category TEXT NOT NULL, "
                "ref_object TEXT NOT NULL, ref_kind TEXT NOT NULL, "
                "used_in TEXT NOT NULL, path TEXT NOT NULL, line INTEGER);\n"
                "CREATE INDEX IF NOT EXISTS idx_mref_ref      ON metadata_references(ref_object);\n"
                "CREATE INDEX IF NOT EXISTS idx_mref_src      ON metadata_references(source_object);\n"
                "CREATE INDEX IF NOT EXISTS idx_mref_kind     ON metadata_references(ref_kind);\n"
                "CREATE INDEX IF NOT EXISTS idx_mref_category ON metadata_references(source_category);\n"
                "CREATE TABLE IF NOT EXISTS exchange_plan_content ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "plan_name TEXT NOT NULL, object_ref TEXT NOT NULL, "
                "auto_record INTEGER NOT NULL, path TEXT NOT NULL);\n"
                "CREATE INDEX IF NOT EXISTS idx_epc_plan ON exchange_plan_content(plan_name);\n"
                "CREATE INDEX IF NOT EXISTS idx_epc_ref  ON exchange_plan_content(object_ref);\n"
                "CREATE TABLE IF NOT EXISTS defined_types ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "name TEXT NOT NULL, type_refs_json TEXT NOT NULL, path TEXT NOT NULL);\n"
                "CREATE INDEX IF NOT EXISTS idx_dt_name ON defined_types(name);\n"
                "CREATE TABLE IF NOT EXISTS characteristic_types ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "pvh_name TEXT NOT NULL, type_refs_json TEXT NOT NULL, path TEXT NOT NULL);\n"
                "CREATE INDEX IF NOT EXISTS idx_ct_pvh ON characteristic_types(pvh_name);\n"
            )
            md_tables = _collect_metadata_tables(base_path)
            _insert_metadata_tables(conn, md_tables)
            # Update meta counts for new tables
            conn.execute(
                "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)",
                ("object_attributes_count", str(len(md_tables.get("object_attributes", [])))),
            )
            conn.execute(
                "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)",
                ("predefined_items_count", str(len(md_tables.get("predefined_items", [])))),
            )

        # Level-6: object synonyms (schema upgrade v6→v7, full refresh)
        if has_synonyms:
            conn.executescript(
                "CREATE TABLE IF NOT EXISTS object_synonyms ("
                "id INTEGER PRIMARY KEY, object_name TEXT NOT NULL, "
                "category TEXT NOT NULL, synonym TEXT NOT NULL, "
                "file TEXT NOT NULL);\n"
                "CREATE INDEX IF NOT EXISTS idx_synonyms_object "
                "ON object_synonyms(object_name COLLATE NOCASE);\n"
                "CREATE INDEX IF NOT EXISTS idx_synonyms_synonym "
                "ON object_synonyms(synonym COLLATE NOCASE);\n"
            )
            conn.execute("DELETE FROM object_synonyms")
            synonyms = _collect_object_synonyms(base_path)
            if synonyms:
                conn.executemany(
                    "INSERT INTO object_synonyms (object_name, category, synonym, file) VALUES (?, ?, ?, ?)",
                    synonyms,
                )
            conn.execute(
                "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)",
                ("has_synonyms", "1"),
            )

        # Refresh role_rights (full rebuild — cheap, ~346K entries in ~2s)
        conn.executescript(
            "CREATE TABLE IF NOT EXISTS role_rights ("
            "id INTEGER PRIMARY KEY, role_name TEXT NOT NULL, "
            "object_name TEXT NOT NULL, right_name TEXT NOT NULL, file TEXT);\n"
            "CREATE INDEX IF NOT EXISTS idx_rr_object ON role_rights(object_name COLLATE NOCASE);\n"
        )
        conn.execute("DELETE FROM role_rights")
        role_rights = _collect_role_rights(base_path)
        if role_rights:
            conn.executemany(
                "INSERT INTO role_rights (role_name, object_name, right_name, file) VALUES (?, ?, ?, ?)",
                role_rights,
            )
        # v1.9.0: refresh role_rights subset of metadata_references
        try:
            conn.execute("DELETE FROM metadata_references WHERE source_category = 'Roles'")
        except sqlite3.OperationalError:
            pass
        if role_rights:
            rr_refs = _role_rights_to_references(role_rights)
            if rr_refs:
                try:
                    conn.executemany(
                        "INSERT INTO metadata_references "
                        "(source_object, source_category, ref_object, ref_kind, used_in, path, line) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        rr_refs,
                    )
                except sqlite3.OperationalError:
                    pass

        # Refresh file_paths (full rebuild — cheap for 30-50K files)
        file_paths_rows = _collect_file_paths(base_path)
        # Ensure table exists (schema upgrade v4→v5)
        conn.executescript(
            "CREATE TABLE IF NOT EXISTS file_paths ("
            "id INTEGER PRIMARY KEY, rel_path TEXT NOT NULL UNIQUE, "
            "extension TEXT NOT NULL, dir_path TEXT NOT NULL, "
            "filename TEXT NOT NULL, depth INTEGER NOT NULL, "
            "size INTEGER, mtime REAL);\n"
            "CREATE INDEX IF NOT EXISTS idx_fp_ext ON file_paths(extension);\n"
            "CREATE INDEX IF NOT EXISTS idx_fp_dir ON file_paths(dir_path);\n"
            "CREATE INDEX IF NOT EXISTS idx_fp_filename ON file_paths(filename COLLATE NOCASE);\n"
            "CREATE INDEX IF NOT EXISTS idx_fp_depth ON file_paths(depth);\n"
        )
        _insert_file_paths(conn, file_paths_rows)
        conn.execute(
            "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)",
            ("file_paths_count", str(len(file_paths_rows))),
        )

        # Recalculate detected_prefixes (may change after objects added/removed)
        detected_prefixes = _detect_prefixes(conn)
        conn.execute(
            "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)",
            ("detected_prefixes", json.dumps(detected_prefixes, ensure_ascii=False)),
        )

        # Level-8: extension overrides (schema upgrade v8→v9, full refresh)
        conn.executescript(
            "CREATE TABLE IF NOT EXISTS extension_overrides ("
            "id INTEGER PRIMARY KEY, "
            "object_name TEXT NOT NULL, "
            "source_path TEXT NOT NULL DEFAULT '', "
            "source_module_id INTEGER, "
            "target_method TEXT NOT NULL, "
            "target_method_line INTEGER, "
            "annotation TEXT NOT NULL, "
            "extension_name TEXT NOT NULL, "
            "extension_purpose TEXT, "
            "extension_method TEXT, "
            "extension_root TEXT NOT NULL, "
            "ext_module_path TEXT NOT NULL, "
            "ext_line INTEGER);\n"
            "CREATE INDEX IF NOT EXISTS idx_eo_object ON extension_overrides(object_name COLLATE NOCASE);\n"
            "CREATE INDEX IF NOT EXISTS idx_eo_method ON extension_overrides(target_method COLLATE NOCASE);\n"
            "CREATE INDEX IF NOT EXISTS idx_eo_source ON extension_overrides(source_module_id);\n"
            "CREATE INDEX IF NOT EXISTS idx_eo_ext ON extension_overrides(extension_name COLLATE NOCASE);\n"
        )
        conn.execute("DELETE FROM extension_overrides")
        override_rows = _collect_extension_overrides(base_path, conn)
        if override_rows:
            conn.executemany(
                "INSERT INTO extension_overrides "
                "(object_name, source_path, source_module_id, target_method, "
                "target_method_line, annotation, extension_name, extension_purpose, "
                "extension_method, extension_root, ext_module_path, ext_line) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                override_rows,
            )
            logger.info("Extension overrides: %d entries", len(override_rows))
        conn.execute(
            "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)",
            ("has_extension_overrides", "1" if override_rows else "0"),
        )
        conn.execute(
            "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)",
            ("extension_overrides_count", str(len(override_rows))),
        )

        # Level-9: form elements (schema upgrade v9→v10, full refresh)
        if has_metadata:
            conn.executescript(_FORM_ELEMENTS_SCHEMA)
            conn.execute("DELETE FROM form_elements")
            fe_rows = _collect_form_elements(base_path)
            if fe_rows:
                conn.executemany(_FORM_ELEMENTS_INSERT, fe_rows)
                logger.info("Form elements: %d entries", len(fe_rows))
            conn.execute(
                "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)",
                ("has_form_elements", "1"),
            )
            conn.execute(
                "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)",
                ("form_elements_count", str(len(fe_rows))),
            )

        # Bump builder_version to current
        conn.execute(
            "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)",
            ("builder_version", str(BUILDER_VERSION)),
        )
        conn.execute(
            "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)",
            ("version", str(BUILDER_VERSION)),
        )

        # Save git_head_commit on fallback so NEXT update can use git fast path.
        # The full scan already made the index correct; here we only refresh the
        # dirty snapshot. If the snapshot can't be captured reliably, keep the
        # previous good one (preserve_on_unreliable) and raise the flag so the
        # next update full-scans again instead of trusting a partial set.
        if _git_available(base_path):
            head = _git_head_sha(base_path)
            if head:
                conn.execute(
                    "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)",
                    ("git_head_commit", head),
                )
                repo_info = _git_repo_info(base_path)
                if repo_info:
                    _, pfx = repo_info
                    self._save_dirty_snapshot(conn, base_path, pfx, preserve_on_unreliable=True)

        conn.commit()
        conn.execute("ANALYZE")
        conn.close()

        elapsed = time.time() - t0
        logger.info("Incremental update done in %.1fs", elapsed)

        return {
            "added": len(added_paths),
            "changed": len(changed_paths),
            "removed": len(removed_paths),
            "git_fast_path": False,
            "git_fallback_reason": fallback_reason or None,
        }

    def _update_git_fast(
        self,
        conn: sqlite3.Connection,
        base_path: str,
        base: Path,
        git_changed: set[str],
        git_head: str,
        prefix: str,
        build_calls: bool,
        has_metadata: bool,
        has_fts: bool,
        has_synonyms: bool,
    ) -> dict:
        """Git-accelerated incremental update — only process changed files."""
        # --- BSL delta ---
        bsl_changed_rel = {p for p in git_changed if p.lower().endswith(".bsl")}

        db_modules: dict[str, dict] = {}
        for row in conn.execute("SELECT id, rel_path, mtime, size FROM modules"):
            db_modules[row["rel_path"]] = {
                "id": row["id"],
                "mtime": row["mtime"],
                "size": row["size"],
            }

        added: set[str] = set()
        changed: set[str] = set()
        removed: set[str] = set()

        for rel in bsl_changed_rel:
            full_path = base / rel
            if full_path.exists():
                if rel in db_modules:
                    changed.add(rel)
                else:
                    added.add(rel)
            else:
                if rel in db_modules:
                    removed.add(rel)

        to_remove = removed | changed
        to_add = added | changed

        logger.info(
            "Git fast path: %d changed files (%d bsl: +%d ~%d -%d)",
            len(git_changed),
            len(bsl_changed_rel),
            len(added),
            len(changed),
            len(removed),
        )

        # Build disk_files map for files to add
        disk_files: dict[str, Path] = {}
        for rel in to_add:
            fp = base / rel
            if fp.exists():
                disk_files[rel] = fp

        bsl_delta = bool(to_remove or to_add)

        if bsl_delta:
            results: list[FileResult] = []
            if to_add:
                workers = min(os.cpu_count() or 4, 8)
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    futures = {
                        pool.submit(
                            _process_single_file,
                            disk_files[rel],
                            base_path,
                            build_calls,
                        ): rel
                        for rel in to_add
                        if rel in disk_files
                    }
                    for future in as_completed(futures):
                        result = future.result()
                        if result is not None:
                            results.append(result)

            # Casefolded names of common modules touched by this delta (NEW from
            # results, OLD queried before delete) — drives per-delta re-resolve.
            changed_common_cf: set[str] = {
                r.info.object_name.casefold()
                for r in results
                if r.info.category == "CommonModules" and r.info.module_type == "Module" and r.info.object_name
            }

            with conn:
                if to_remove:
                    # metadata_code_usages has no module_id index (weight) — batch
                    # delete by chunk to stay under the SQLite variable limit.
                    removed_ids = [db_modules[rel]["id"] for rel in to_remove if rel in db_modules]
                    # Old common-module names for the call-graph re-resolve — only
                    # needed when calls are built; chunk to stay under the SQLite
                    # variable limit (like the metadata_code_usages delete below).
                    if build_calls and removed_ids:
                        for chunk in _chunked(removed_ids):
                            ph = ",".join("?" * len(chunk))
                            for row in conn.execute(
                                f"SELECT object_name FROM modules WHERE id IN ({ph}) "  # noqa: S608
                                "AND category='CommonModules' AND module_type='Module' "
                                "AND object_name IS NOT NULL",
                                chunk,
                            ):
                                old_name = row[0] if not isinstance(row, sqlite3.Row) else row["object_name"]
                                changed_common_cf.add(old_name.casefold())
                    for chunk in _chunked(removed_ids):
                        placeholders = ",".join("?" * len(chunk))
                        try:
                            conn.execute(
                                f"DELETE FROM metadata_code_usages WHERE module_id IN ({placeholders})",
                                chunk,
                            )
                        except sqlite3.OperationalError:
                            pass  # table may not exist in pre-v13 index
                    for rel in to_remove:
                        mod_info = db_modules.get(rel)
                        if mod_info is None:
                            continue
                        mod_id = mod_info["id"]
                        method_ids = [
                            row[0] for row in conn.execute("SELECT id FROM methods WHERE module_id = ?", (mod_id,))
                        ]
                        if method_ids:
                            placeholders = ",".join("?" * len(method_ids))
                            conn.execute(
                                f"DELETE FROM calls WHERE caller_id IN ({placeholders})",
                                method_ids,
                            )
                            if has_fts:
                                conn.execute(
                                    f"DELETE FROM methods_fts WHERE rowid IN ({placeholders})",
                                    method_ids,
                                )
                        conn.execute("DELETE FROM methods WHERE module_id = ?", (mod_id,))
                        try:
                            conn.execute("DELETE FROM regions WHERE module_id = ?", (mod_id,))
                        except sqlite3.OperationalError:
                            pass
                        try:
                            conn.execute("DELETE FROM module_headers WHERE module_id = ?", (mod_id,))
                        except sqlite3.OperationalError:
                            pass
                        conn.execute("DELETE FROM modules WHERE id = ?", (mod_id,))

                self._ensure_callee_key_column(conn)
                self._bulk_insert(conn, results, build_calls)
                # Re-resolve qualified callee_key on unchanged callers of common
                # modules touched by this delta (keeps update ≡ build).
                if build_calls:
                    self._reresolve_qualified_callers(conn, changed_common_cf)
                self._refresh_call_resolution_stats(conn)

                if has_fts and results:
                    new_rel_paths = [r.info.relative_path for r in results]
                    placeholders = ",".join("?" * len(new_rel_paths))
                    conn.execute(
                        f"INSERT INTO methods_fts(rowid, name, object_name) "
                        f"SELECT m.id, m.name, mod.object_name "
                        f"FROM methods m JOIN modules mod ON mod.id = m.module_id "
                        f"WHERE mod.rel_path IN ({placeholders})",
                        new_rel_paths,
                    )

                # `or to_remove`: при ЧИСТОМ удалении модуля results пуст, и прежний
                # guard `if results:` оставлял его движения в таблице сиротами навсегда.
                if results or to_remove:
                    # Удаление идёт по ФАЙЛУ, а не по имени документа. Удаление по
                    # document_name сносило строки ОБОИХ модулей документа, а
                    # восстанавливались только строки переобработанного: правка
                    # ObjectModule стирала erp_mechanism/manager_table/adapted/manager_code
                    # из НЕтронутого ManagerModule и наоборот. Дефект пре-существующий
                    # (воспроизводится и на источниках v14), но с появлением manager_code
                    # он стал массовым: у большинства документов с записью из менеджера
                    # есть и ObjectModule. `to_remove` = removed | changed, поэтому
                    # удалённый модуль тоже чистится, а нетронутый сосед остаётся цел.
                    stale_files = set(to_remove) | {r.info.relative_path for r in results}
                    for stale_rel in stale_files:
                        try:
                            conn.execute(
                                "DELETE FROM register_movements WHERE file = ?",
                                (stale_rel,),
                            )
                        except sqlite3.OperationalError:
                            pass
                    # Каталог регистров считаем ОДИН раз перед циклом (см. полную сборку).
                    known_registers = _known_register_names(base)
                    new_movements: list[tuple[str, str, str, str]] = []
                    for r in results:
                        if r.movements and r.info.object_name:
                            for reg_name, source, fpath in r.movements:
                                if source == "manager_code" and reg_name.lower() not in known_registers:
                                    continue
                                new_movements.append((r.info.object_name, reg_name, source, fpath))
                    if new_movements:
                        conn.executemany(
                            "INSERT INTO register_movements "
                            "(document_name, register_name, source, file) VALUES (?, ?, ?, ?)",
                            new_movements,
                        )

        # --- Update bsl_count, paths_hash, built_at ---
        stored_count_row = conn.execute("SELECT value FROM index_meta WHERE key = 'bsl_count'").fetchone()
        stored_count = int(stored_count_row["value"]) if stored_count_row else 0
        new_bsl_count = stored_count + len(added) - len(removed)
        conn.execute(
            "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)",
            ("bsl_count", str(new_bsl_count)),
        )

        all_paths = [row[0] for row in conn.execute("SELECT rel_path FROM modules ORDER BY rel_path")]
        new_hash = _paths_hash(all_paths)
        conn.execute(
            "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)",
            ("paths_hash", new_hash),
        )
        conn.execute(
            "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)",
            ("built_at", str(time.time())),
        )

        # --- Level-1: configuration meta (always) ---
        config_meta = _parse_configuration_meta(base_path)
        for key, value in config_meta.items():
            conn.execute(
                "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)",
                (key, value),
            )

        # --- Selective metadata refresh based on separate trigger sets ---
        # .xml/.mdo → category-based metadata tables, attrs, synonyms
        xml_mdo_changed = {p for p in git_changed if p.lower().endswith((".xml", ".mdo"))}
        # .form (EDT) ИЛИ */Ext/Form.xml (CF) → form_elements.
        # v1.33.0: без CF-ветки обычная правка формы Конфигуратора не пересобирала
        # form_elements ВООБЩЕ — такой путь попадает только в xml_mdo_changed, а
        # пересборка идёт строго `if form_changed`. Form.xml остаётся и в
        # xml_mdo_changed: он несёт и метаданные объекта.
        # Сузить до `/Forms/<Имя>/Ext/Form.xml` нельзя: у CommonForms промежуточного
        # `Forms/` НЕТ (`CommonForms/<Имя>/Ext/Form.xml`), и такой регекс пропускал бы
        # 433 общие формы на боевом CF — их `main` и `types` замерзали бы навсегда.
        # Признак `*/Ext/Form.xml` точен: на боевом CF 10 124 таких файла, и все до
        # единого — формы (`Ext/Form.xml` вне каталога формы не встречается).
        form_changed = {
            p
            for p in git_changed
            if p.lower().endswith(".form") or re.search(r"(?:^|/)Ext/Form\.xml$", p, re.IGNORECASE)
        }
        # .rights → role_rights only
        rights_changed = {p for p in git_changed if p.lower().endswith(".rights")}
        # .xdto → xdto_packages (via category detection from path)
        xdto_changed = {p for p in git_changed if p.lower().endswith(".xdto")}
        # .command → metadata_references (per-object commands и CommonCommands).
        # Только для metadata refresh; synonym-ветка использует obj_meta_changed
        # без .command, чтобы чистые .command-дельты не триггерили category-wide
        # synonym rescan (perf-регрессия).
        command_changed = {p for p in git_changed if p.lower().endswith(".command")}

        # Categories from .xml/.mdo/.xdto only (NOT .form/.rights/.command) — synonym-ветка.
        obj_meta_changed = xml_mdo_changed | xdto_changed
        # Trigger set для metadata refresh (pointwise + bulk fallback) — включает .command.
        metadata_trigger_set = obj_meta_changed | command_changed
        changed_categories = {p.split("/")[0] for p in metadata_trigger_set} if metadata_trigger_set else set()
        # Категории, разрешённые для synonym-ветки (без .command).
        synonym_changed_categories = {p.split("/")[0] for p in obj_meta_changed} if obj_meta_changed else set()

        # Any meta file changed (union of all trigger sets — включает .command).
        any_meta_changed = xml_mdo_changed | form_changed | rights_changed | xdto_changed | command_changed

        # Telemetry и pointwise_categories инициализируются ДО has_metadata-guard:
        # они используются финальным INFO-логом и фильтром synonym-ветки даже
        # на путях без metadata (BSL-only / .form-only / has_metadata=False).
        pointwise_telemetry = _PointwiseTelemetry()
        pointwise_categories: set[str] = set()

        if metadata_trigger_set and has_metadata:
            # --- Pointwise dispatcher (Round 4: единый has_metadata guard) ---
            opt = _get_optional_tables_state(conn)
            changed_objects, unresolved_categories = _build_changed_objects(metadata_trigger_set, base_path)
            for category in list(changed_categories):
                objects = changed_objects.get(category, set())
                if not _can_use_pointwise(category, objects, unresolved_categories, conn, pointwise_telemetry):
                    continue
                conn.execute("SAVEPOINT pw_cat")
                try:
                    for object_name in objects:
                        if category in _POINTWISE_ELIGIBLE_GROUP_B:
                            _refresh_global_object(conn, base_path, category, object_name, opt)
                        else:
                            _refresh_object(conn, base_path, category, object_name, opt)
                except Exception as exc:  # noqa: BLE001 — любой сбой → fallback
                    conn.execute("ROLLBACK TO pw_cat")
                    conn.execute("RELEASE pw_cat")
                    pointwise_telemetry.note_fallback(category, "refresh_exception", exc)
                    logger.debug("Pointwise refresh failed for %s, falling back: %s", category, exc)
                else:
                    conn.execute("RELEASE pw_cat")
                    pointwise_telemetry.note_pointwise(category, len(objects))
                    pointwise_categories.add(category)

            # --- Bulk fallback (only for categories not handled pointwise) ---
            fallback_categories = changed_categories - pointwise_categories
            if fallback_categories:
                attrs_cats = fallback_categories & (set(_ATTR_CATEGORIES) | set(_PREDEFINED_CATEGORIES))
                ref_cats = fallback_categories & _METADATA_REFERENCES_TRIGGER_CATEGORIES

                md_tables = _collect_metadata_tables(
                    base_path,
                    collect_es="EventSubscriptions" in fallback_categories,
                    collect_sj="ScheduledJobs" in fallback_categories,
                    collect_fo="FunctionalOptions" in fallback_categories,
                    collect_enums="Enums" in fallback_categories,
                    collect_subs="Subsystems" in fallback_categories,
                    collect_http="HTTPServices" in fallback_categories,
                    collect_ws="WebServices" in fallback_categories,
                    collect_xdto="XDTOPackages" in fallback_categories,
                    collect_attrs_categories=attrs_cats,
                    collect_exchange_plans="ExchangePlans" in fallback_categories,
                    collect_defined_types="DefinedTypes" in fallback_categories,
                    collect_pvh_types="ChartsOfCharacteristicTypes" in fallback_categories,
                    collect_metadata_refs_categories=ref_cats,
                )
                _insert_metadata_tables_selective(conn, md_tables, fallback_categories)

        # form_elements: only if .form files changed
        if form_changed and has_metadata:
            try:
                conn.execute("DELETE FROM form_elements")
            except sqlite3.OperationalError:
                pass
            fe_rows = _collect_form_elements(base_path)
            if fe_rows:
                conn.executemany(_FORM_ELEMENTS_INSERT, fe_rows)
            conn.execute(
                "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)",
                ("has_form_elements", "1"),
            )
            conn.execute(
                "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)",
                ("form_elements_count", str(len(fe_rows))),
            )

        # object_synonyms: selective by category (only .xml/.mdo trigger).
        # Категории, обработанные pointwise (synonyms уже вставлены через
        # _insert_synonym_for_object), исключаются — иначе category-wide DELETE
        # перезаписал бы точечный результат.
        if obj_meta_changed and has_synonyms:
            synonym_cats = (synonym_changed_categories & _SYNONYM_CATEGORIES) - pointwise_categories
            if synonym_cats:
                for cat in synonym_cats:
                    conn.execute("DELETE FROM object_synonyms WHERE category = ?", (cat,))
                synonyms = _collect_object_synonyms(base_path, categories=frozenset(synonym_cats))
                if synonyms:
                    conn.executemany(
                        "INSERT INTO object_synonyms (object_name, category, synonym, file) VALUES (?, ?, ?, ?)",
                        synonyms,
                    )

        # role_rights: only if .rights files or Roles/.xml changed
        if rights_changed or (obj_meta_changed and "Roles" in changed_categories):
            try:
                conn.execute("DELETE FROM role_rights")
            except sqlite3.OperationalError:
                pass
            role_rights = _collect_role_rights(base_path)
            if role_rights:
                conn.executemany(
                    "INSERT INTO role_rights (role_name, object_name, right_name, file) VALUES (?, ?, ?, ?)",
                    role_rights,
                )
            # v1.9.0: refresh role_rights subset of metadata_references
            try:
                conn.execute("DELETE FROM metadata_references WHERE source_category = 'Roles'")
            except sqlite3.OperationalError:
                pass
            if role_rights:
                rr_refs = _role_rights_to_references(role_rights)
                if rr_refs:
                    try:
                        conn.executemany(
                            "INSERT INTO metadata_references "
                            "(source_object, source_category, ref_object, ref_kind, used_in, path, line) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?)",
                            rr_refs,
                        )
                    except sqlite3.OperationalError:
                        pass

        # file_paths: incremental for BSL-only, full if any meta file changed
        if any_meta_changed:
            file_paths_rows = _collect_file_paths(base_path)
            _insert_file_paths(conn, file_paths_rows)
            conn.execute(
                "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)",
                ("file_paths_count", str(len(file_paths_rows))),
            )
        elif bsl_delta:
            self._refresh_file_paths_delta(conn, base, bsl_changed_rel, added, removed)

        # detected_prefixes
        detected_prefixes = _detect_prefixes(conn)
        conn.execute(
            "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)",
            ("detected_prefixes", json.dumps(detected_prefixes, ensure_ascii=False)),
        )

        # extension_overrides: rebuild if BSL or any meta changed
        if bsl_delta or any_meta_changed:
            try:
                conn.execute("DELETE FROM extension_overrides")
            except sqlite3.OperationalError:
                pass
            override_rows = _collect_extension_overrides(base_path, conn)
            if override_rows:
                conn.executemany(
                    "INSERT INTO extension_overrides "
                    "(object_name, source_path, source_module_id, target_method, "
                    "target_method_line, annotation, extension_name, extension_purpose, "
                    "extension_method, extension_root, ext_module_path, ext_line) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    override_rows,
                )
            conn.execute(
                "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)",
                ("has_extension_overrides", "1" if override_rows else "0"),
            )
            conn.execute(
                "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)",
                ("extension_overrides_count", str(len(override_rows))),
            )

        # Save git_head_commit + dirty snapshot. On an unreliable snapshot keep
        # the previous good one and raise git_dirty_unreliable so the next update
        # full-scans; on a reliable snapshot the flag is cleared.
        conn.execute(
            "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)",
            ("git_head_commit", git_head),
        )
        self._save_dirty_snapshot(conn, base_path, prefix, preserve_on_unreliable=True)

        # Telemetry — одна INFO-строка с агрегатами pointwise/fallback.
        logger.info(
            "git fast path: pointwise refresh used for %d categories (%d objects), "
            "fallback used for %d categories (reasons: %s), bsl modules incremental: %d",
            pointwise_telemetry.pointwise_categories,
            pointwise_telemetry.pointwise_objects,
            pointwise_telemetry.fallback_categories,
            dict(pointwise_telemetry.reasons),
            len(added) + len(changed),
        )

        return {
            "added": len(added),
            "changed": len(changed),
            "removed": len(removed),
        }

    @staticmethod
    def _refresh_file_paths_delta(
        conn: sqlite3.Connection,
        base: Path,
        bsl_changed_rel: set[str],
        added: set[str],
        removed: set[str],
    ) -> None:
        """Incremental update of file_paths for changed BSL files only."""
        all_affected = added | removed | bsl_changed_rel
        for rel in all_affected:
            try:
                conn.execute("DELETE FROM file_paths WHERE rel_path = ?", (rel,))
            except sqlite3.OperationalError:
                pass

        new_rows: list[tuple] = []
        for rel in all_affected:
            fp = base / rel
            if fp.exists():
                try:
                    st = fp.stat()
                except OSError:
                    continue
                ext = fp.suffix.lower()
                parts = rel.split("/")
                dir_path = "/".join(parts[:-1]) if len(parts) > 1 else ""
                filename = fp.name
                depth = len(parts)
                new_rows.append((rel, ext, dir_path, filename, depth, st.st_size, st.st_mtime))

        if new_rows:
            conn.executemany(
                "INSERT OR REPLACE INTO file_paths "
                "(rel_path, extension, dir_path, filename, depth, size, mtime) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                new_rows,
            )

        count = conn.execute("SELECT COUNT(*) FROM file_paths").fetchone()[0]
        conn.execute(
            "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)",
            ("file_paths_count", str(count)),
        )

    # --- Private helpers ---

    @staticmethod
    def _ensure_callee_key_column(conn: sqlite3.Connection) -> None:
        """Defensively add ``calls.callee_key`` if a same-version DB lacks it.

        A v14 index is always created via a full rebuild (old_version < 14 forces
        it), so the column exists on every real v14 DB. This guard mirrors the
        defensive ``CREATE TABLE IF NOT EXISTS`` pattern used elsewhere and keeps
        the incremental INSERT from failing should a malformed DB slip through.
        """
        try:
            cols = conn.execute("PRAGMA table_info(calls)").fetchall()
            names = {(c[1] if not isinstance(c, sqlite3.Row) else c["name"]) for c in cols}
            if "callee_key" not in names:
                conn.execute("ALTER TABLE calls ADD COLUMN callee_key TEXT")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_calls_callee_key ON calls(callee_key)")
        except sqlite3.OperationalError:
            pass

    @staticmethod
    def _ensure_callee_short_index(conn: sqlite3.Connection) -> None:
        """Self-heal: create ``idx_calls_callee_short`` if a v14 DB lacks it.

        Build paths get the index from ``_SCHEMA_SQL`` (both executescript sites).
        This guard exists ONLY for already-built v14 DBs created before the index
        existed: ``_update_locked`` calls it UNCONDITIONALLY on every update (incl.
        no-op / metadata-only), since ``_SCHEMA_SQL`` is not re-run on the update
        path. DDL comes from the single-source ``_callee_short_expr`` — same
        expression as the schema and the query (no drift). ``IF NOT EXISTS`` makes
        it a cheap no-op once present.
        """
        try:
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_calls_callee_short "
                f"ON calls({_callee_short_expr('callee_name')} COLLATE NOCASE)"
            )
        except sqlite3.OperationalError:
            pass

    @staticmethod
    def _ensure_meth_module_index(conn: sqlite3.Connection) -> None:
        """Self-heal: create ``idx_meth_module`` if a v14 DB lacks it.

        FK index on ``methods(module_id)`` that lets ``_resolve_target_key`` go
        modules→methods by an index lookup instead of scanning all methods (the
        ``py_lower(name)`` predicate already defeats ``idx_meth_name``). Build
        paths get it from ``_SCHEMA_SQL``; this guard covers already-built v14 DBs
        created before the index existed (``_SCHEMA_SQL`` is not re-run on update).
        Called UNCONDITIONALLY by ``_update_locked`` on every update. ``IF NOT
        EXISTS`` makes it a cheap no-op once present.
        """
        try:
            conn.execute("CREATE INDEX IF NOT EXISTS idx_meth_module ON methods(module_id)")
        except sqlite3.OperationalError:
            pass

    @staticmethod
    def _refresh_call_resolution_stats(conn: sqlite3.Connection) -> None:
        """Recompute call-resolution aggregates over the WHOLE calls table.

        Writes ``calls_total`` / ``calls_resolved`` into ``index_meta`` with a
        single SQL pass. Must be called AFTER ``_bulk_insert`` in every write
        path (full build, full-scan update, git fast path) — counting inside
        ``_bulk_insert`` would only see the current batch on an incremental run.
        """
        try:
            row = conn.execute("SELECT COUNT(*), COUNT(callee_key) FROM calls").fetchone()
        except sqlite3.OperationalError:
            return
        total = row[0] if row else 0
        resolved = row[1] if row else 0
        conn.execute(
            "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)",
            ("calls_total", str(total)),
        )
        conn.execute(
            "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)",
            ("calls_resolved", str(resolved)),
        )

    @staticmethod
    def _reresolve_qualified_callers(conn: sqlite3.Connection, changed_common_cf: set[str] | None) -> int:
        """Re-resolve ``callee_key`` of qualified ``A.B`` edges against current DB.

        The incremental update only re-resolves edges of REPROCESSED modules, so a
        qualified edge from an UNCHANGED caller keeps a stale key when its target
        common module ``A`` changed (rename / delete / export-flip / add). This pass
        recomputes such keys with the SAME resolver as build (``_build_common_exported``
        + ``_make_callee_key`` + ``casefold``) so ``update`` matches a fresh build.

        ``changed_common_cf`` — casefolded names of common modules touched by the
        delta; only edges ``A.B`` with ``A`` in this set are recomputed. ``None`` =
        GLOBAL mode (every qualified edge), for the one-time legacy migration. An
        empty set is a no-op (returns 0). Returns the number of updated edges.

        A correctness-first single scan (not ``LIKE 'A.%'``): the resolver matches
        module names via ``casefold()``, but LIKE/NOCASE does not fold Cyrillic, so an
        indexed prefix would miss differently-cased callers and break ``update ≡ build``.
        """
        if changed_common_cf is not None and not changed_common_cf:
            return 0
        common = _build_common_exported(conn)
        updates: list[tuple[str | None, int]] = []
        # Positional access (row[i]) — works for both sqlite3.Row and tuple
        # factories, and ``rowid`` is not always addressable by name.
        for row in conn.execute("SELECT rowid, callee_name, callee_key FROM calls WHERE instr(callee_name,'.')>0"):
            rowid, callee_name, stored = row[0], row[1], row[2]
            a, _, b = callee_name.partition(".")
            if changed_common_cf is not None and a.casefold() not in changed_common_cf:
                continue
            rel = common.get((a.casefold(), b.casefold()))
            new_key = _make_callee_key(rel, b) if rel else None
            if new_key != stored:
                updates.append((new_key, rowid))
        if updates:
            conn.executemany("UPDATE calls SET callee_key=? WHERE rowid=?", updates)
        return len(updates)

    @staticmethod
    def _reresolve_qualified_callers_once(conn: sqlite3.Connection) -> int:
        """One-time GLOBAL re-resolve of qualified ``callee_key`` for legacy v14 DBs.

        Idempotent via the ``qualified_callers_reresolved`` meta flag — heals drift
        accumulated by updates made BEFORE the per-delta re-resolve existed. New builds
        set the flag in ``_write_meta`` (already consistent), so this fires only once,
        on the next update of a pre-existing index. No schema change / BUILDER_VERSION
        bump. ``_ensure_callee_key_column`` first keeps it self-sufficient for a
        malformed v14 DB lacking the column. Returns rows updated (0 if already done).
        """
        IndexBuilder._ensure_callee_key_column(conn)
        if IndexBuilder._meta_get(conn, "qualified_callers_reresolved"):
            return 0
        with conn:
            n = IndexBuilder._reresolve_qualified_callers(conn, None)
            IndexBuilder._refresh_call_resolution_stats(conn)
            IndexBuilder._meta_set(conn, "qualified_callers_reresolved", "1")
        return n

    @staticmethod
    def _meta_get(conn: sqlite3.Connection, key: str) -> str | None:
        """Read a single ``index_meta`` value (k/v store; None if missing)."""
        row = conn.execute("SELECT value FROM index_meta WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        return row[0] if isinstance(row, tuple) else row["value"]

    @staticmethod
    def _meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
        """Upsert a single ``index_meta`` value."""
        conn.execute(
            "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)",
            (key, str(value)),
        )

    @staticmethod
    def _register_casefold_udf(conn: sqlite3.Connection) -> None:
        """Register a NULL-safe ``py_casefold`` UDF on this connection.

        The update connection (``_update_locked``) registers NO UDF — unlike
        ``IndexReader`` which sets ``py_lower`` (``.lower()``). The builtin SQLite
        ``lower()`` does NOT fold Cyrillic, so the in-place purge needs its own
        Python folder. ``casefold()`` is used for consistency with
        ``_BSL_GLOBAL_FUNCS_LOWER`` and the extraction-time check.
        """
        conn.create_function("py_casefold", 1, lambda s: s.casefold() if s is not None else None)

    @staticmethod
    def _purge_call_noise_once(conn: sqlite3.Connection) -> int:
        """One-time in-place purge of bare-global noise from a legacy ``calls`` table.

        Idempotent via the ``calls_noise_cleaned`` meta flag: new builds set the
        flag in ``_write_meta`` (extraction filter already produced clean data),
        so this only fires on indexes built before the filter existed. Deletes
        ONLY unresolved (``callee_key IS NULL``) bare (``instr(name,'.')=0``) rows
        whose name is a reserved platform global — physically cannot touch a real
        edge. Recomputes ``calls_total`` / ``calls_resolved`` so the meta invariant
        holds even when the surrounding update has no BSL delta (no-op / git-fast).

        Returns the number of deleted rows (0 if already cleaned).
        """
        if IndexBuilder._meta_get(conn, "calls_noise_cleaned"):
            return 0
        IndexBuilder._register_casefold_udf(conn)
        names = list(_BSL_GLOBAL_FUNCS_LOWER)
        placeholders = ",".join("?" * len(names))
        with conn:
            cur = conn.execute(
                "DELETE FROM calls "
                "WHERE callee_key IS NULL AND instr(callee_name, '.') = 0 "
                f"AND py_casefold(callee_name) IN ({placeholders})",
                names,
            )
            deleted = cur.rowcount
            IndexBuilder._refresh_call_resolution_stats(conn)
            IndexBuilder._meta_set(conn, "calls_noise_cleaned", "1")
        return deleted

    @staticmethod
    def _bulk_insert(
        conn: sqlite3.Connection,
        results: list[FileResult],
        build_calls: bool,
    ) -> None:
        """Insert modules, methods, and calls in batch."""
        module_rows: list[tuple] = []
        for r in results:
            module_rows.append(
                (
                    r.info.relative_path,
                    r.info.category,
                    r.info.object_name,
                    r.info.module_type,
                    r.info.form_name,
                    1 if r.info.is_form_module else 0,
                    r.mtime,
                    r.size,
                )
            )

        conn.executemany(
            "INSERT OR REPLACE INTO modules "
            "(rel_path, category, object_name, module_type, form_name, is_form, mtime, size) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            module_rows,
        )

        # Build rel_path -> module_id map
        path_to_id: dict[str, int] = {}
        for row in conn.execute("SELECT id, rel_path FROM modules"):
            path_to_id[row[0] if isinstance(row, tuple) else row["rel_path"]] = (
                row[1] if isinstance(row, tuple) else row["id"]
            )
        # Fix: sqlite3 without row_factory returns tuples (id, rel_path)
        path_to_id_fixed: dict[str, int] = {}
        for row in conn.execute("SELECT id, rel_path FROM modules"):
            if isinstance(row, sqlite3.Row):
                path_to_id_fixed[row["rel_path"]] = row["id"]
            else:
                path_to_id_fixed[row[1]] = row[0]
        path_to_id = path_to_id_fixed

        # Insert methods and collect method IDs for calls
        method_rows: list[tuple] = []
        # We need to track method insertions to map method_idx -> method_id for calls
        call_pending: list[tuple[str, int, str, int]] = []  # (rel_path, method_idx, callee, line)

        for r in results:
            mod_id = path_to_id.get(r.info.relative_path)
            if mod_id is None:
                continue
            for method in r.methods:
                method_rows.append(
                    (
                        mod_id,
                        method["name"],
                        method["type"],
                        1 if method["is_export"] else 0,
                        method["params"],
                        method["line"],
                        method["end_line"],
                        method.get("loc"),
                    )
                )
            if build_calls:
                for method_idx, callee_name, call_line in r.raw_calls:
                    call_pending.append((r.info.relative_path, method_idx, callee_name, call_line))

        conn.executemany(
            "INSERT OR REPLACE INTO methods "
            "(module_id, name, type, is_export, params, line, end_line, loc) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            method_rows,
        )

        # Insert calls — need to resolve method_idx to method_id
        if build_calls and call_pending:
            # Module ids of the CURRENT batch (the only callers we resolve here).
            # NOTE: path_to_id spans the WHOLE DB, so take ids from ``results``.
            batch_ids = [path_to_id[r.info.relative_path] for r in results if r.info.relative_path in path_to_id]
            # On a small increment, narrow the two batch-local lookups to the batch
            # (indexed IN scan) instead of scanning every method in the DB. The
            # threshold keeps the IN list under SQLITE_MAX_VARIABLE_NUMBER and lets
            # large batches / full builds keep the plain full scan. ``common_exported``
            # always needs whole-DB visibility (handled by _build_common_exported).
            narrow = 0 < len(batch_ids) <= 900

            # Build module_id -> method IDs ORDERED BY line (method_idx in raw_calls
            # indexes this list; the ORDER BY MUST be preserved or caller_id drifts).
            methods_by_module: dict[int, list[int]] = {}
            if narrow:
                ph = ",".join("?" * len(batch_ids))
                mbm_cur = conn.execute(
                    f"SELECT id, module_id FROM methods WHERE module_id IN ({ph}) "  # noqa: S608
                    "ORDER BY module_id, line",
                    batch_ids,
                )
            else:
                mbm_cur = conn.execute("SELECT id, module_id FROM methods ORDER BY module_id, line")
            for row in mbm_cur:
                if isinstance(row, sqlite3.Row):
                    mid, modid = row["id"], row["module_id"]
                else:
                    mid, modid = row[0], row[1]
                methods_by_module.setdefault(modid, []).append(mid)

            # Resolution lookups. Two provably-safe tiers only:
            #   local           — bare ``B()`` → a method of the caller's own module
            #                      (caller ∈ batch, so batch-local lookup suffices)
            #   common_exported — ``A.B()`` → an exported method of common module A
            #                      (target may be in an UNCHANGED module → whole-DB)
            # Object/manager qualified calls (Справочники.X.Метод / Объект.Метод)
            # are intentionally NOT resolved (ambiguous variable/manager method).
            # Value ``None`` marks an ambiguous key (multiple candidates) → NULL.
            local_lookup: dict[tuple[int, str], str | None] = {}
            if narrow:
                ph = ",".join("?" * len(batch_ids))
                ll_cur = conn.execute(
                    "SELECT mod.id AS module_id, mod.rel_path AS rel_path, me.name AS name "
                    "FROM methods me JOIN modules mod ON me.module_id = mod.id "
                    f"WHERE mod.id IN ({ph})",  # noqa: S608
                    batch_ids,
                )
            else:
                ll_cur = conn.execute(
                    "SELECT mod.id AS module_id, mod.rel_path AS rel_path, me.name AS name "
                    "FROM methods me JOIN modules mod ON me.module_id = mod.id"
                )
            for row in ll_cur:
                if isinstance(row, sqlite3.Row):
                    module_id, rel_path, mname = row["module_id"], row["rel_path"], row["name"]
                else:
                    module_id, rel_path, mname = row
                lk = (module_id, mname.casefold())
                local_lookup[lk] = None if lk in local_lookup else rel_path

            common_exported = _build_common_exported(conn)

            def _resolve_callee_key(caller_module_id: int, callee: str) -> str | None:
                if "." in callee:
                    # Qualified A.B — only common-exported is safe to resolve.
                    a, _, b = callee.partition(".")
                    rel = common_exported.get((a.casefold(), b.casefold()))
                    return _make_callee_key(rel, b) if rel else None
                # Bare B — resolve only within the caller's own module.
                rel = local_lookup.get((caller_module_id, callee.casefold()))
                return _make_callee_key(rel, callee) if rel else None

            call_rows: list[tuple] = []
            for r in results:
                mod_id = path_to_id.get(r.info.relative_path)
                if mod_id is None:
                    continue
                method_ids = methods_by_module.get(mod_id, [])

                for method_idx, callee_name, call_line in r.raw_calls:
                    if method_idx < len(method_ids):
                        caller_method_id = method_ids[method_idx]
                        callee_key = _resolve_callee_key(mod_id, callee_name)
                        call_rows.append((caller_method_id, callee_name, call_line, callee_key))

            if call_rows:
                conn.executemany(
                    "INSERT INTO calls (caller_id, callee_name, line, callee_key) VALUES (?, ?, ?, ?)",
                    call_rows,
                )

        # Insert regions and module_headers
        region_rows: list[tuple] = []
        header_rows: list[tuple] = []
        for r in results:
            mod_id = path_to_id.get(r.info.relative_path)
            if mod_id is None:
                continue
            for reg in r.regions:
                region_rows.append((mod_id, reg["name"], reg["line"], reg["end_line"]))
            if r.header_comment:
                header_rows.append((mod_id, r.header_comment))

        if region_rows:
            conn.executemany(
                "INSERT INTO regions (module_id, name, line, end_line) VALUES (?, ?, ?, ?)",
                region_rows,
            )
        if header_rows:
            conn.executemany(
                "INSERT OR REPLACE INTO module_headers (module_id, header_comment) VALUES (?, ?)",
                header_rows,
            )

        # Insert reverse code-usages (v1.14.0). object_ref_key = object_ref.lower()
        # computed here for indexed, Cyrillic-aware case-insensitive lookup.
        usage_rows: list[tuple] = []
        for r in results:
            mod_id = path_to_id.get(r.info.relative_path)
            if mod_id is None:
                continue
            for object_ref, member_path, usage_kind, line in r.code_usages:
                usage_rows.append((mod_id, object_ref, object_ref.lower(), member_path, usage_kind, line))
        if usage_rows:
            conn.executemany(
                "INSERT INTO metadata_code_usages "
                "(module_id, object_ref, object_ref_key, member_path, usage_kind, line) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                usage_rows,
            )

    @staticmethod
    def _write_meta(
        conn: sqlite3.Connection,
        base_path: str,
        bsl_count: int,
        paths_hash: str,
        build_calls: bool,
        build_metadata: bool = False,
        config_meta: dict[str, str] | None = None,
        build_fts: bool = False,
        detected_prefixes: list[str] | None = None,
        file_paths_count: int = 0,
        build_synonyms: bool = False,
    ) -> None:
        """Write index metadata."""
        meta_entries = [
            ("version", str(BUILDER_VERSION)),
            ("bsl_count", str(bsl_count)),
            ("paths_hash", paths_hash),
            ("built_at", str(time.time())),
            ("builder_version", str(BUILDER_VERSION)),
            ("base_path", base_path),
            ("has_calls", "1" if build_calls else "0"),
            ("has_metadata", "1" if build_metadata else "0"),
            ("has_fts", "1" if build_fts else "0"),
            ("has_synonyms", "1" if build_synonyms else "0"),
            ("file_paths_count", str(file_paths_count)),
            # A full build produces clean data (extraction filter applied), so the
            # legacy in-place purge in update() can skip straight away.
            ("calls_noise_cleaned", "1"),
            # A full build resolves every qualified callee_key in one pass against
            # the final DB state, so the one-time global re-resolve migration in
            # update() can skip straight away (no accumulated drift to heal).
            ("qualified_callers_reresolved", "1"),
        ]
        # Level-1: Configuration metadata
        if config_meta:
            for key, value in config_meta.items():
                meta_entries.append((key, value))

        # Detected custom prefixes
        if detected_prefixes:
            meta_entries.append(
                (
                    "detected_prefixes",
                    json.dumps(
                        detected_prefixes,
                        ensure_ascii=False,
                    ),
                )
            )

        conn.executemany(
            "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)",
            meta_entries,
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Glob pattern dispatcher (whitelist-based, not universal translator)
# ---------------------------------------------------------------------------
def _can_index_glob(pattern: str) -> tuple[str, dict] | None:
    """Check if a glob pattern can be served from the file_paths index.

    Returns (strategy_name, params) or None for FS fallback.

    Supported patterns:
      **/*.ext            → ('by_extension', {ext: '.ext'})
      **/Dir/**/*.ext     → ('under_prefix_ext', {dir_name: 'Dir', ext: '.ext'})
      Dir/**/*.ext        → ('prefix_recursive_ext', {prefix: 'Dir', ext: '.ext'})
      Dir/*/File.ext      → ('dir_file', {dir: 'Dir', file: 'File.ext'})
      Dir/** or Dir/**/*  → ('under_prefix', {prefix: 'Dir'})
      exact/path          → ('exact', {path: 'exact/path'})
      **/Name.*           → ('name_wildcard', {name_prefix: 'Name', ext: ''})
      **/Name.ext         → ('name_wildcard', {name_prefix: 'Name', ext: '.ext'})
    """
    if not pattern:
        return None

    # Normalize to POSIX
    pattern = pattern.replace("\\", "/")

    # **/*.ext — all files with given extension
    if pattern.startswith("**/") and pattern.count("/") == 1:
        rest = pattern[3:]  # after **/
        if "*" not in rest and "?" not in rest:
            # **/Name.ext — specific file by name
            if "." in rest:
                name, ext = rest.rsplit(".", 1)
                return ("name_wildcard", {"name_prefix": name, "ext": "." + ext})
            return None
        if rest.startswith("*.") and "*" not in rest[2:] and "?" not in rest[2:]:
            ext = "." + rest[2:]
            return ("by_extension", {"ext": ext})
        if rest == "*":
            # **/* — all files (too broad, let FS handle)
            return None
        # **/Name.* — name wildcard
        if rest.endswith(".*") and "*" not in rest[:-2] and "?" not in rest[:-2]:
            name_prefix = rest[:-2]
            return ("name_wildcard", {"name_prefix": name_prefix, "ext": ""})
        return None

    # Dir/**/*.ext — recursive under prefix (anchored), filter by extension
    m = re.match(r"^([^*?]+)/\*\*/\*(\.[^/*?]+)$", pattern)
    if m:
        return ("prefix_recursive_ext", {"prefix": m.group(1), "ext": m.group(2)})

    # **/Dir/**/*.ext — recursive under directory name, filter by extension
    m = re.match(r"^\*\*/([^/*?]+)/\*\*/\*(\.[^/*?]+)$", pattern)
    if m:
        return ("under_prefix_ext", {"dir_name": m.group(1), "ext": m.group(2)})

    # Dir/** or Dir/**/*
    if pattern.endswith("/**") or pattern.endswith("/**/*"):
        prefix = pattern.split("/**")[0]
        if "*" not in prefix and "?" not in prefix:
            return ("under_prefix", {"prefix": prefix})
        return None

    # Dir/*/File.ext — single-level wildcard
    parts = pattern.split("/")
    if len(parts) == 3 and parts[1] == "*" and "*" not in parts[0] and "*" not in parts[2]:
        return ("dir_file", {"dir": parts[0], "file": parts[2]})

    # No wildcards — exact path
    if "*" not in pattern and "?" not in pattern:
        return ("exact", {"path": pattern})

    # Everything else → fallback to FS
    return None


# ---------------------------------------------------------------------------
# Defensive reader (v1.26.0, Finding #1): transient-safe wrapper + zero-stats
# ---------------------------------------------------------------------------
# Stable ASCII engine texts (locale-independent) of the ONLY two transient
# OperationalError classes an in-place rebuild can produce on a concurrent old reader.
_TRANSIENT_SQLITE_MARKERS = ("no such table", "database is locked")


def _transient_safe(sentinel_factory):
    """Decorate an IndexReader READ-method reachable via ``idx_reader.<m>()``.

    An in-place rebuild can make a concurrent OLD reader hit a TRANSIENT
    ``OperationalError`` — the DROP→CREATE window (``"no such table"``) or the brief
    exclusive lock of best-effort ``VACUUM`` (``"database is locked"``). Those degrade
    to a FRESH per-call sentinel instead of crashing. **Any other** ``OperationalError``
    (``"no such column"``, a SQL typo, ``"disk I/O error"``, ``"malformed"``, …) is
    **RE-RAISED** — swallowing a real defect into an empty result would slip past the
    tests (that is the very 'new bug' to avoid). ``sentinel_factory()`` is called fresh
    per invocation (mutable sentinels must NOT be shared singletons).

    Honest caveat (also in scope): ``"database is locked"`` could in theory surface
    OUTSIDE a rebuild under rare lock contention (unlikely with WAL + RO readers + a
    single ``_BuildLock``-serialized writer) — there it too degrades to the sentinel.
    Acceptable best-effort for a transient error. In normal operation (no rebuild) this
    decorator is a no-op (nothing to catch)."""

    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except sqlite3.OperationalError as exc:
                if not any(m in str(exc).lower() for m in _TRANSIENT_SQLITE_MARKERS):
                    raise
                logger.debug("_transient_safe: %s degraded to sentinel (%s)", fn.__name__, exc)
                return sentinel_factory()

        wrapper._is_transient_safe = True  # marker for the AST coverage gate
        return wrapper

    return decorator


def _zero_stats() -> dict:
    """Fresh zero-filled mirror of ``IndexReader.get_statistics()`` — the per-call
    sentinel for the ``@_transient_safe``-wrapped ``get_statistics`` (a concurrent reader
    during an in-place rebuild gets a stable-shaped payload, never an ``OperationalError``).

    MUST carry the SAME key set as ``get_statistics`` (consumers do ``**stats`` and
    ``.get(...)``); ``test_zero_stats_key_parity`` fails on any drift. Returned fresh
    (factory) — never a shared singleton (a consumer mutating it would poison the next
    call)."""
    return {
        "modules": 0,
        "methods": 0,
        "calls": 0,
        "exports": 0,
        "built_at": None,
        "config_name": None,
        "config_version": None,
        "config_synonym": None,
        "config_vendor": None,
        "source_format": None,
        "config_role": None,
        "has_metadata": False,
        "has_fts": False,
        "bsl_count": None,
        "builder_version": None,
        "event_subscriptions": 0,
        "scheduled_jobs": 0,
        "functional_options": 0,
        "role_rights": 0,
        "register_movements": 0,
        "enum_values": 0,
        "subsystem_content": 0,
        "http_services": 0,
        "web_services": 0,
        "xdto_packages": 0,
        "file_paths": 0,
        "object_synonyms": 0,
        "regions": 0,
        "module_headers": 0,
        "extension_overrides": 0,
        "form_elements": 0,
        "object_attributes": 0,
        "predefined_items": 0,
        "metadata_references": 0,
        "exchange_plan_content": 0,
        "defined_types": 0,
        "characteristic_types": 0,
        "metadata_code_usages": 0,
        "git_head_commit": None,
        "git_accelerated": False,
    }


def stats_indicate_load_failure(stats: dict) -> bool:
    """True ⇔ a ``get_statistics()`` result is a zero/load-failure sentinel rather than a
    real index read.

    A VALID index — even an empty one (0 .bsl) — ALWAYS has ``builder_version`` AND
    ``built_at`` written by ``_write_meta``. ``_zero_stats()`` (the ``_transient_safe``
    sentinel) and a mid-rebuild read (where ``_begin_inplace_rebuild`` has stripped those
    keys) both leave BOTH of them ``None``. This is **timing-independent** — it inspects
    the stats actually returned, not the ``build_in_progress`` marker (which a finishing
    rebuild may have already cleared between ``get_statistics`` and a marker re-check).
    Lets consumers refuse to report a partial/transient read as a loaded ``"ok"`` index
    with zero counts (codex High)."""
    return stats.get("builder_version") is None and stats.get("built_at") is None


# ---------------------------------------------------------------------------
# IndexReader (read-only)
# ---------------------------------------------------------------------------
class IndexReader:
    """Read-only interface to the method index database.

    Thread-safe: uses a per-instance lock for all database operations.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._lock = __import__("threading").Lock()
        # Open in read-only mode via URI
        self._conn = sqlite3.connect(
            f"file:{self._db_path}?mode=ro",
            uri=True,
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        # UDF for Cyrillic case-insensitive search. NULL-safe: modules.object_name
        # is nullable (e.g. root application/session modules), and SQLite would
        # otherwise call str.lower(None) → "user-defined function raised exception"
        # for any query that applies py_lower() to such a column (bare module_hint
        # resolution). Returning None makes the comparison NULL (row excluded),
        # which is the correct semantics.
        self._conn.create_function("py_lower", 1, lambda s: s.lower() if s is not None else None)
        # Capability check (Phase B): a v13 DB opened read-only BEFORE a
        # background rebuild has no calls.callee_key column. Probe once and gate
        # the exact (resolved) call-graph mode on it — otherwise get_callers must
        # not touch callee_key at all (would raise "no such column").
        self._has_callee_key = self._probe_callee_key()

    def _probe_callee_key(self) -> bool:
        try:
            cols = self._conn.execute("PRAGMA table_info(calls)").fetchall()
            return any(c["name"] == "callee_key" for c in cols)
        except sqlite3.Error:
            return False

    @property
    def has_calls(self) -> bool:
        """Check if the calls table has any data.

        Existence probe (``LIMIT 1``), NOT ``COUNT(*)``: find_callers_context
        reads this property on EVERY hierarchy node, and a full count scans
        ~3.7M rows (~112ms) on big ERP. ``SELECT 1 ... LIMIT 1`` is O(1).
        """
        with self._lock:
            try:
                row = self._conn.execute("SELECT 1 FROM calls LIMIT 1").fetchone()
                return row is not None
            except sqlite3.Error:
                return False

    @_transient_safe(lambda: None)
    def get_methods_by_path(self, rel_path: str) -> list[dict] | None:
        """Get all methods for a given module path.

        Returns:
            List of dicts {name, type, line, end_line, is_export, params} or None
            if the module is not in the index.
        """
        with self._lock:
            mod_row = self._conn.execute("SELECT id FROM modules WHERE rel_path = ?", (rel_path,)).fetchone()
            if mod_row is None:
                return None

            rows = self._conn.execute(
                "SELECT name, type, line, end_line, is_export, params FROM methods WHERE module_id = ? ORDER BY line",
                (mod_row["id"],),
            ).fetchall()

            return [
                {
                    "name": r["name"],
                    "type": r["type"],
                    "line": r["line"],
                    "end_line": r["end_line"],
                    "is_export": bool(r["is_export"]),
                    "params": r["params"],
                }
                for r in rows
            ]

    @_transient_safe(lambda: None)
    def get_definitions(self, name: str, module_hint: str = "", limit: int = 50) -> dict | None:
        """Find every place a method ``name`` is defined (forward go-to-definition).

        The forward complement of the reverse call-graph lookup: a direct
        ``methods JOIN modules`` seek by name, optionally narrowed by
        ``module_hint`` (the same three forms ``find_callers`` accepts: rel_path,
        ``Документ.X`` / ``Document.X``, or a bare object name). Unlike
        ``_resolve_target_key`` this does NOT force a single result — a same-named
        method in many objects (``ОбработкаПроведения``) is the norm in 1С, so all
        candidates are returned, capped by ``limit``.

        Returns:
            ``{"rows": [...], "total": int, "truncated": bool, "slow_fallback": bool}``
            — ``rows`` capped to ``limit`` (each a raw dict; ``params`` is the
            stored string, normalised to ``list[str]`` at the helper border);
            ``total`` is **exact**; ``truncated`` flags the ``limit+1`` probe;
            ``slow_fallback`` flags the rare Cyrillic ``py_lower`` rescan.

            ``None`` on ``OperationalError`` (corrupt/missing core tables) —
            distinct from a valid empty result ``{"rows": [], "total": 0, ...}``
            ("no such method"). The helper routes ``None`` to live fallback rather
            than reporting a silent false-negative.

        Perf (EXPLAIN, builder v14, production-sized ERP indexes): the name predicate is
        ``= ? COLLATE NOCASE`` → ``SEARCH m USING idx_meth_name`` (~3 ms seek), NOT
        ``py_lower(m.name)`` which disables the index → SCAN ~700 ms.
        """
        rel_hint, category, object_name = _normalize_module_hint(module_hint)

        base = (
            "SELECT mod.rel_path, mod.category, mod.object_name, mod.module_type, "
            "m.name, m.type, m.is_export, m.line, m.end_line, m.params "
            "FROM methods m JOIN modules mod ON mod.id = m.module_id "
        )

        def _where(name_pred: str) -> tuple[str, list]:
            # Shared WHERE builder: the name predicate (``name_pred``) plus the
            # optional module_hint filter (same forms as ``_resolve_target_key``).
            # ``name`` is always the first bound param of ``name_pred``.
            clauses = [name_pred]
            params: list = [name]
            if rel_hint is not None:
                clauses.append("mod.rel_path = ?")
                params.append(rel_hint)
            elif category is not None and object_name is not None:
                clauses.append("mod.category = ? AND py_lower(mod.object_name) = py_lower(?)")
                params.extend([category, object_name])
            elif object_name is not None:
                clauses.append("py_lower(mod.object_name) = py_lower(?)")
                params.append(object_name)
            return " AND ".join(clauses), params

        with self._lock:
            try:
                # Primary: NOCASE index seek (~3 ms).
                where, params = _where("m.name = ? COLLATE NOCASE")
                sql = base + "WHERE " + where + " ORDER BY mod.rel_path, m.line LIMIT ?"
                rows = self._conn.execute(sql, [*params, limit + 1]).fetchall()

                slow_fallback = False
                # Cyrillic rescan: NOCASE folds only ASCII, so a lowercase Cyrillic
                # input misses the canonical PascalCase name. Pay one py_lower SCAN
                # ONLY on an empty result with a non-ASCII name (rare). No casefold
                # gate (it would suppress the rescan for already-lowercase input).
                if not rows and not name.isascii():
                    # Flag marks that the SCAN was PAID — set it whether or not the
                    # rescan finds rows (a missing Cyrillic name still pays the SCAN).
                    slow_fallback = True
                    where, params = _where("py_lower(m.name) = py_lower(?)")
                    sql = base + "WHERE " + where + " ORDER BY mod.rel_path, m.line LIMIT ?"
                    rows = self._conn.execute(sql, [*params, limit + 1]).fetchall()

                truncated = len(rows) > limit
                kept = rows[:limit]
                if truncated:
                    # Exact total via COUNT with the SAME predicate/hint (NOCASE
                    # seek ~3 ms) — only in the rare truncated case.
                    name_pred = "py_lower(m.name) = py_lower(?)" if slow_fallback else "m.name = ? COLLATE NOCASE"
                    cwhere, cparams = _where(name_pred)
                    csql = (
                        "SELECT COUNT(*) AS c FROM methods m JOIN modules mod ON mod.id = m.module_id WHERE " + cwhere
                    )
                    total = self._conn.execute(csql, cparams).fetchone()["c"]
                else:
                    total = len(kept)
            except sqlite3.OperationalError:
                return None

        return {
            "rows": [
                {
                    "rel_path": r["rel_path"],
                    "category": r["category"],
                    "object_name": r["object_name"],
                    "module_type": r["module_type"],
                    "name": r["name"],
                    "type": r["type"],
                    "is_export": bool(r["is_export"]),
                    "line": r["line"],
                    "end_line": r["end_line"],
                    "params": r["params"],
                }
                for r in kept
            ],
            "total": total,
            "truncated": truncated,
            "slow_fallback": slow_fallback,
        }

    def sample_method_definitions(self, name: str, limit: int = 5) -> dict | None:
        """Cheap NOCASE probe for the find_path ambiguity guard (v1.25.0).

        Returns ``{"total": int, "candidates": [{file, line, object_name,
        category, module_type}]}`` — a fast yes/no on "is this name defined in
        more than one module?" plus a small disambiguation sample. ``total > 1``
        ⇔ multi-defined (on normal 1С code = distinct modules).

        One locked round trip: a ``LIMIT (limit + 1)`` NOCASE seek
        (``idx_meth_name`` ~3 ms). If the result is NOT truncated, ``total`` is
        just ``len(rows)`` (no separate COUNT). Only when truncated do we pay a
        single ``COUNT(*)`` — with the SAME ``methods JOIN modules`` predicate as
        the SELECT, so orphan ``methods`` rows without a ``modules`` row can't
        inflate ``total`` past what ``candidates`` could ever show (keeps the
        ``{total, candidates}`` contract consistent, cf. ``get_definitions``).

        NOCASE-only — NO ``py_lower`` fallback: the guard must never pay a
        Cyrillic SCAN, so a lowercase / case-mismatched Cyrillic input simply
        yields ``total == 0`` (guard stays OFF — deliberate, documented).

        ``None`` on ``OperationalError`` (corrupt/missing core table), like
        ``get_definitions`` — the caller treats ``None`` as guard-OFF, so "index
        broken" is never conflated with "no such method" (``total == 0``).
        """
        with self._lock:
            try:
                rows = self._conn.execute(
                    "SELECT mod.rel_path, mod.object_name, mod.category, mod.module_type, m.line "
                    "FROM methods m JOIN modules mod ON mod.id = m.module_id "
                    "WHERE m.name = ? COLLATE NOCASE ORDER BY mod.rel_path, m.line LIMIT ?",
                    (name, limit + 1),
                ).fetchall()
                if len(rows) > limit:
                    total = self._conn.execute(
                        "SELECT COUNT(*) AS c FROM methods m JOIN modules mod ON mod.id = m.module_id "
                        "WHERE m.name = ? COLLATE NOCASE",
                        (name,),
                    ).fetchone()["c"]
                else:
                    total = len(rows)
            except sqlite3.OperationalError:
                return None
            return {
                "total": int(total),
                "candidates": [
                    {
                        "file": r["rel_path"],
                        "line": r["line"],
                        "object_name": r["object_name"],
                        "category": r["category"],
                        "module_type": r["module_type"],
                    }
                    for r in rows[:limit]
                ],
            }

    @_transient_safe(lambda: None)
    def get_outline_data(self, rel_path: str) -> dict | None:
        """Atomically read everything needed to build a module outline tree.

        One lock, three reads (module row + regions + methods **incl. ``loc``**) so
        the helper can rebuild the ``#Область`` tree without touching
        ``get_methods_by_path`` (whose contract omits ``loc``).

        Returns:
            ``{"module": {category, object_name, module_type} | None,
               "regions": [{name, line, end_line}],
               "methods": [{name, type, is_export, line, end_line, loc}]}``

            * ``module`` is ``None`` when ``rel_path`` is not in ``modules`` (a
              valid "not indexed" answer — the helper routes it to live fallback).
            * ``regions`` ``end_line`` may be NULL (unclosed ``#Область``).

            ``None`` (sentinel) when ``regions`` is missing/unreadable
            (``OperationalError`` on an old pre-v8 index, or corrupt core) —
            distinct from a valid empty ``regions`` list ("flat module, 0
            regions"). The helper routes the sentinel to live fallback so it never
            builds a silently-incomplete outline.
        """
        with self._lock:
            try:
                mod_row = self._conn.execute(
                    "SELECT id, category, object_name, module_type FROM modules WHERE rel_path = ?",
                    (rel_path,),
                ).fetchone()
                if mod_row is None:
                    return {"module": None, "regions": [], "methods": []}
                mid = mod_row["id"]
                reg_rows = self._conn.execute(
                    "SELECT name, line, end_line FROM regions WHERE module_id = ? ORDER BY line",
                    (mid,),
                ).fetchall()
                meth_rows = self._conn.execute(
                    "SELECT name, type, is_export, line, end_line, loc FROM methods WHERE module_id = ? ORDER BY line",
                    (mid,),
                ).fetchall()
            except sqlite3.OperationalError:
                return None  # regions table absent (old index) or corrupt core → go live
            return {
                "module": {
                    "category": mod_row["category"],
                    "object_name": mod_row["object_name"],
                    "module_type": mod_row["module_type"],
                },
                "regions": [{"name": r["name"], "line": r["line"], "end_line": r["end_line"]} for r in reg_rows],
                "methods": [
                    {
                        "name": r["name"],
                        "type": r["type"],
                        "is_export": bool(r["is_export"]),
                        "line": r["line"],
                        "end_line": r["end_line"],
                        "loc": r["loc"],
                    }
                    for r in meth_rows
                ],
            }

    def _resolve_target_key(self, proc_name: str, module_hint: str) -> str | None:
        """Resolve a query target to a single stable ``callee_key`` (or None).

        Exact mode only (requires ``self._has_callee_key``). The target of a
        ``get_callers`` query has no caller_module_id, so the build-time
        ``local`` tier is inapplicable — the target is resolved by module
        identity instead:

          - with ``module_hint`` → normalize it (rel_path / category+object_name
            / bare object_name) and find the module carrying ``proc_name``;
          - without hint → exact only if ``proc_name`` is an exported method of a
            common module AND unique across the whole DB.

        Returns ``_make_callee_key(rel_path, proc_name)`` when the target maps to
        exactly one module; otherwise None (→ name-based fallback).
        """
        if not self._has_callee_key:
            return None
        rel_hint, category, object_name = _normalize_module_hint(module_hint)
        rel_paths: list[str]
        if rel_hint is not None:
            rows = self._conn.execute(
                "SELECT DISTINCT mod.rel_path FROM methods m JOIN modules mod ON mod.id = m.module_id "
                "WHERE mod.rel_path = ? AND py_lower(m.name) = py_lower(?)",
                (rel_hint, proc_name),
            ).fetchall()
            rel_paths = [r["rel_path"] for r in rows]
        elif category is not None and object_name is not None:
            rows = self._conn.execute(
                "SELECT DISTINCT mod.rel_path FROM methods m JOIN modules mod ON mod.id = m.module_id "
                "WHERE mod.category = ? AND py_lower(mod.object_name) = py_lower(?) "
                "AND py_lower(m.name) = py_lower(?)",
                (category, object_name, proc_name),
            ).fetchall()
            rel_paths = [r["rel_path"] for r in rows]
        elif object_name is not None:
            rows = self._conn.execute(
                "SELECT DISTINCT mod.rel_path FROM methods m JOIN modules mod ON mod.id = m.module_id "
                "WHERE py_lower(mod.object_name) = py_lower(?) AND py_lower(m.name) = py_lower(?)",
                (object_name, proc_name),
            ).fetchall()
            rel_paths = [r["rel_path"] for r in rows]
        else:
            # No hint — exact ONLY if the name is GLOBALLY UNIQUE across the whole
            # DB AND that single method is an exported common-module method. A
            # same-named method ANYWHERE else (local object method, form, another
            # module) makes the name ambiguous → NOT exact, so name-based fallback
            # preserves recall instead of silently pinning the common-module target
            # and dropping resolved local edges (plan §Консьюмеры п.1).
            rows = self._conn.execute(
                "SELECT mod.rel_path, mod.category, mod.module_type, m.is_export "
                "FROM methods m JOIN modules mod ON mod.id = m.module_id "
                "WHERE py_lower(m.name) = py_lower(?) LIMIT 2",
                (proc_name,),
            ).fetchall()
            rel_paths = []
            if len(rows) == 1:
                r = rows[0]
                if r["is_export"] and r["category"] == "CommonModules" and r["module_type"] == "Module":
                    rel_paths = [r["rel_path"]]
        if len(rel_paths) == 1:
            return _make_callee_key(rel_paths[0], proc_name)
        return None

    @_transient_safe(lambda: None)
    def get_callers(
        self,
        proc_name: str,
        module_hint: str = "",
        offset: int = 0,
        limit: int = 50,
    ) -> dict | None:
        """Find callers of a procedure/function using the call graph index.

        Returns a dict matching the find_callers_context format:
        {
            "callers": [{file, caller_name, caller_is_export, line, object_name,
                         category, module_type}],
            "_meta": {total_callers, returned, offset, has_more,
                      exact_available, exact_rows, fallback_rows}
        }

        Exact (resolved) mode: when the schema has ``callee_key`` and the target
        resolves to exactly one module, callers are matched precisely on
        ``callee_key`` (collisions from same-named methods elsewhere are dropped),
        plus any still-unresolved (``callee_key IS NULL``) name matches kept for
        recall. ``_meta`` reports ``exact_rows`` vs ``fallback_rows`` so the
        consumer can tell precise edges from heuristic ones. When the column is
        absent (v13 DB read before rebuild) exact mode is off
        (``exact_available=False``) and the legacy name-based query runs.

        Returns None if the calls table has no data.
        """
        with self._lock:
            try:
                # Existence check, NOT a full COUNT(*) (which scans ~3.7M rows
                # ~112ms on big ERP — see perf fix v1.16.0). get_callers is called
                # per node in find_call_hierarchy, so this must be O(1).
                exists_row = self._conn.execute("SELECT 1 FROM calls LIMIT 1").fetchone()
                if exists_row is None:
                    return None
            except sqlite3.Error:
                return None

            # Indexed name-match clause (single source: _callee_match_clause →
            # _callee_short_expr). Replaces the old leading-wildcard
            # ``= ? OR LIKE '%.name'`` (full scan) — bare names hit
            # idx_calls_callee_short, dotted input hits idx_calls_callee. Shared
            # verbatim by every match site below so the planner uses the index.
            match_sql, match_param = _callee_match_clause(proc_name, "c.callee_name")

            # --- Exact (resolved) mode ---------------------------------------
            exact_key = self._resolve_target_key(proc_name, module_hint)
            if exact_key is not None:
                base_select = (
                    "SELECT c.line AS call_line, c.callee_name, c.callee_key AS edge_callee_key, "
                    "m.name AS caller_name, "
                    "m.is_export AS caller_is_export, mod.rel_path, mod.object_name, "
                    "mod.category, mod.module_type "
                    "FROM calls c JOIN methods m ON m.id = c.caller_id "
                    "JOIN modules mod ON mod.id = m.module_id "
                )
                where = f"WHERE (c.callee_key = ? OR (c.callee_key IS NULL AND {match_sql}))"

                exact_cnt_row = self._conn.execute(
                    "SELECT COUNT(*) AS cnt FROM calls WHERE callee_key = ?", (exact_key,)
                ).fetchone()
                exact_rows = exact_cnt_row["cnt"] if exact_cnt_row else 0
                fb_cnt_row = self._conn.execute(
                    f"SELECT COUNT(*) AS cnt FROM calls c WHERE c.callee_key IS NULL AND {match_sql}",
                    (match_param,),
                ).fetchone()
                fallback_rows = fb_cnt_row["cnt"] if fb_cnt_row else 0
                total_callers = exact_rows + fallback_rows

                query = base_select + where + " ORDER BY mod.rel_path, call_line LIMIT ? OFFSET ?"
                rows = self._conn.execute(query, (exact_key, match_param, limit, offset)).fetchall()
                callers = [
                    {
                        "file": r["rel_path"],
                        "caller_name": r["caller_name"],
                        "caller_is_export": bool(r["caller_is_export"]),
                        "line": r["call_line"],
                        "object_name": r["object_name"],
                        "category": r["category"],
                        "module_type": r["module_type"],
                        # Additive per-edge precision flag (v1.19.0, for find_path):
                        # True ⇔ this edge matched the stable callee_key, False ⇔
                        # kept by name recall (callee_key IS NULL). _meta aggregates
                        # cannot carry this per-row, so find_path reads it here.
                        "edge_exact": bool(r["edge_callee_key"] is not None and r["edge_callee_key"] == exact_key),
                    }
                    for r in rows
                ]
                return {
                    "callers": callers,
                    "_meta": {
                        "total_callers": total_callers,
                        "returned": len(callers),
                        "offset": offset,
                        "has_more": (offset + limit) < total_callers,
                        "exact_available": True,
                        "target_exact": True,
                        "target_key": exact_key,
                        "exact_rows": exact_rows,
                        "fallback_rows": fallback_rows,
                    },
                }

            # --- Fallback (name-based) mode — legacy behavior ----------------
            # Match callers by method name via the shared indexed clause; for a
            # bare proc_name this also covers qualified "Module.proc_name" edges.
            query = f"""
                SELECT
                    c.line AS call_line,
                    c.callee_name,
                    m.name AS caller_name,
                    m.is_export AS caller_is_export,
                    mod.rel_path,
                    mod.object_name,
                    mod.category,
                    mod.module_type
                FROM calls c
                JOIN methods m ON m.id = c.caller_id
                JOIN modules mod ON mod.id = m.module_id
                WHERE ({match_sql})
            """
            params_list: list = [match_param]

            if module_hint:
                # Filter by callee qualification: match qualified "hint.proc"
                # OR unqualified "proc" (ambiguous — could belong to hint module)
                escaped_hint = module_hint.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                query += " AND (c.callee_name LIKE ? ESCAPE '\\' OR c.callee_name = ? COLLATE NOCASE)"
                params_list.append(f"{escaped_hint}.%")
                params_list.append(proc_name)

                # Scope narrowing: non-export or form → callers only in same file
                target = self._conn.execute(
                    """SELECT m.is_export, mod.is_form, mod.rel_path
                       FROM methods m JOIN modules mod ON mod.id = m.module_id
                       WHERE m.name = ? COLLATE NOCASE
                         AND mod.object_name = ? COLLATE NOCASE""",
                    [proc_name, module_hint],
                ).fetchone()
                if target and (not target["is_export"] or target["is_form"]):
                    query += " AND mod.rel_path = ?"
                    params_list.append(target["rel_path"])

            # Count total
            _t0 = time.monotonic()
            if not module_hint:
                # Fast path: COUNT on calls table only. Unaliased column → matches
                # idx_calls_callee_short (bare) / idx_calls_callee (dotted).
                count_sql, count_param = _callee_match_clause(proc_name, "callee_name")
                count_query = f"SELECT COUNT(*) AS cnt FROM calls WHERE ({count_sql})"
                count_row = self._conn.execute(count_query, [count_param]).fetchone()
            else:
                # Exact path: COUNT via JOIN (precise, with module filter)
                count_query = f"SELECT COUNT(*) AS cnt FROM ({query})"
                count_row = self._conn.execute(count_query, params_list).fetchone()
            _t_count = time.monotonic() - _t0
            total_callers = count_row["cnt"] if count_row else 0

            # Fetch page
            query += " ORDER BY mod.rel_path, call_line LIMIT ? OFFSET ?"
            params_list.extend([limit, offset])

            _t0 = time.monotonic()
            rows = self._conn.execute(query, params_list).fetchall()
            _t_rows = time.monotonic() - _t0

            logger.debug(
                "get_callers: proc=%s count_time=%.2fs rows_time=%.2fs total=%d returned=%d",
                proc_name,
                _t_count,
                _t_rows,
                total_callers,
                len(rows),
            )

            callers = [
                {
                    "file": r["rel_path"],
                    "caller_name": r["caller_name"],
                    "caller_is_export": bool(r["caller_is_export"]),
                    "line": r["call_line"],
                    "object_name": r["object_name"],
                    "category": r["category"],
                    "module_type": r["module_type"],
                    # Name-based fallback: no edge resolved to a stable key.
                    "edge_exact": False,
                }
                for r in rows
            ]

            return {
                "callers": callers,
                "_meta": {
                    "total_callers": total_callers,
                    "returned": len(callers),
                    "offset": offset,
                    "has_more": (offset + limit) < total_callers,
                    # Name-based fallback: schema may support exact mode
                    # (exact_available) but the target was not uniquely resolved,
                    # so every returned row is heuristic (name match).
                    "exact_available": self._has_callee_key,
                    "target_exact": False,
                    "exact_rows": 0,
                    "fallback_rows": total_callers,
                },
            }

    @_transient_safe(lambda: None)
    def resolve_target_identity(self, proc_name: str, module_hint: str = "") -> str | None:
        """Public, LOCKED wrapper over ``_resolve_target_key`` (v1.19.0).

        ``_resolve_target_key`` is an INTERNAL, lockless SQL helper: ``get_callers``
        and ``get_inbound_edges`` already call it while holding ``self._lock`` (a
        plain ``threading.Lock``, NOT an ``RLock`` — re-acquiring would deadlock).
        Callers running OUTSIDE the reader lock (e.g. ``find_path`` in
        ``bsl_helpers``) must use THIS method, never the lockless internal directly.

        Returns ``"<rel_path>::<casefold(proc_name)>"`` when the target maps to
        exactly one module; otherwise None (ambiguous / no exact mode / old index).
        """
        with self._lock:
            return self._resolve_target_key(proc_name, module_hint)

    @_transient_safe(list)
    def get_inbound_edges(self, proc_name: str, module_hint: str = "") -> list[dict]:
        """Non-call inbound edges into ``proc_name`` (the 1C control-flow graph).

        A method is reached not only from code (``calls`` → ``get_callers``) but
        also via four declarative sources, all resolved through the SAME
        ``callee_key`` identity space used by the call graph:

          * ``cfe_override``  — an extension CFE-override (``&Перед``/``&После``/
            ``&Вместо``) that wraps the method;
          * ``form_event``    — a form element/event whose handler IS the method;
          * ``subscription``  — an EventSubscription whose handler IS the method;
          * ``scheduled_job`` — a ScheduledJob whose handler IS the method.

        Contract (v1.19.0): ALWAYS returns ``list[dict]`` (NEVER None — an empty
        list means "no non-call edges", so ``find_call_hierarchy`` can blindly
        merge it). Each row::

            {edge_type, source_name, source_kind, detail, file, line,
             caller_name, object_name, category, target_key: str|None, resolved: bool}

        ``resolved=True`` ⇔ matched on the stable ``target_key``; ``False`` ⇔
        matched by name only (recall path). Mirroring ``get_callers``, even in
        exact mode we keep name-matched-but-unresolved rows (``resolved=False``):
        extension-only indexes store ``extension_overrides.source_path == ""`` and
        a subscription whose ``handler_module`` does not resolve in ``modules`` —
        dropping them would silently lose edges.

        ``COLLATE NOCASE`` folds ONLY ASCII, so Cyrillic method names are compared
        in Python via ``.casefold()`` (the method in ``target_key`` is already
        casefold'd by ``_make_callee_key``). No-op safe: each block is guarded by
        ``try/except sqlite3.OperationalError`` so old indexes simply add nothing.
        """
        edges: list[dict] = []
        proc_cf = proc_name.casefold()
        with self._lock:
            # Resolve the target identity ONCE (lockless internal — we hold the
            # lock). exact_key = "<rel_path>::<casefold(method)>" or None.
            try:
                exact_key = self._resolve_target_key(proc_name, module_hint)
            except sqlite3.Error:
                exact_key = None
            target_rel_path = exact_key.rsplit("::", 1)[0] if exact_key is not None else None

            # --- (1) cfe_override — reverse, scan of the SMALL overrides table ---
            # No index fits our filter (idx_eo_source=source_module_id;
            # idx_eo_method=COLLATE NOCASE, which does not fold Cyrillic), so we
            # mirror get_extension_overrides: read rows, filter in Python casefold.
            try:
                rows = self._conn.execute(
                    "SELECT object_name, source_path, target_method, annotation, "
                    "extension_name, extension_method, ext_module_path, ext_line "
                    "FROM extension_overrides"
                ).fetchall()
            except sqlite3.OperationalError:
                rows = []
            for r in rows:
                if (r["target_method"] or "").casefold() != proc_cf:
                    continue
                src_path = (r["source_path"] or "").replace("\\", "/")
                resolved = bool(
                    exact_key is not None
                    and src_path != ""
                    and target_rel_path is not None
                    and src_path.casefold() == target_rel_path.casefold()
                )
                edges.append(
                    {
                        "edge_type": "cfe_override",
                        "source_name": r["extension_name"],
                        "source_kind": "Extension",
                        "detail": r["annotation"],
                        "file": r["ext_module_path"],
                        "line": r["ext_line"],
                        "caller_name": r["extension_method"],
                        "object_name": r["object_name"],
                        "category": None,
                        "target_key": exact_key if resolved else None,
                        "resolved": resolved,
                    }
                )

            # --- (2) form_event — reverse, two steps (handler is BARE, in form) ---
            frow = None
            if exact_key is not None and target_rel_path is not None:
                try:
                    frow = self._conn.execute(
                        "SELECT object_name, category, form_name FROM modules WHERE rel_path = ? AND is_form = 1",
                        (target_rel_path,),
                    ).fetchone()
                except sqlite3.OperationalError:
                    frow = None
            if frow is not None:
                # Authoritative form context: emit ONLY this form's matching
                # handlers (exact, resolved=True). object_name/form_name come from
                # modules so they are precise; match the BARE handler in Python.
                try:
                    handlers = self._conn.execute(
                        "SELECT object_name, category, form_name, element_name, event, handler, file "
                        "FROM form_elements WHERE kind = 'handler' AND object_name = ? AND form_name = ?",
                        (frow["object_name"], frow["form_name"]),
                    ).fetchall()
                except sqlite3.OperationalError:
                    handlers = []
                _form_resolved = True
            elif exact_key is not None:
                # Target resolved to a NON-form module → it CANNOT be a form event
                # handler (handlers are bare procedures living IN the form module).
                # Skip the scan entirely. PERF (v1.19.0 fix): the old recall branch
                # ran a FULL form_elements scan PER NODE; on a wide include_triggers
                # tree (dozens of nodes) over a big config (~250K form_elements) that
                # was O(nodes × rows) → seconds-to-minutes. Most expanded nodes resolve
                # to common/object modules, so this skip removes the dominant cost.
                handlers = []
                _form_resolved = False
            else:
                # Truly unknown target (no exact key): name-narrow in SQL via the
                # Cyrillic-aware py_lower UDF (returns ONLY name-matches), NOT a
                # fetch-all + Python loop. resolved=False (recall path).
                try:
                    handlers = self._conn.execute(
                        "SELECT object_name, category, form_name, element_name, event, handler, file "
                        "FROM form_elements WHERE kind = 'handler' AND py_lower(handler) = py_lower(?)",
                        (proc_name,),
                    ).fetchall()
                except sqlite3.OperationalError:
                    handlers = []
                _form_resolved = False
            for h in handlers:
                if (h["handler"] or "").casefold() != proc_cf:
                    continue
                elem = h["element_name"] or "[form]"
                edges.append(
                    {
                        "edge_type": "form_event",
                        "source_name": h["form_name"],
                        "source_kind": "Form",
                        "detail": f"{elem}.{h['event']}",
                        "file": h["file"],
                        "line": None,
                        "caller_name": h["handler"],
                        "object_name": h["object_name"],
                        "category": h["category"],
                        "target_key": exact_key if _form_resolved else None,
                        "resolved": _form_resolved,
                    }
                )

            # --- (3)+(4) subscription / scheduled_job — FORWARD resolve (cheaper) ---
            # handler_module is the common module's object_name (not rel_path);
            # handler_procedure is BARE. Resolve forward with one LEFT JOIN. The
            # filter is handler_procedure<>'' (NOT handler_module<>'' — that would
            # drop dotless handlers, builder allows handler_module=""); the module
            # condition lives in the ON clause so a non-resolving module yields a
            # NULL rel_path row instead of a dropped one (recall). Forward cleanly
            # distinguishes same-named handlers in different common modules, so a
            # row that provably resolves to a DIFFERENT module is dropped.
            #
            # PERF (v1.19.0 fix): the handler_procedure name-match is pushed into the
            # SQL WHERE via py_lower(...) — WITHOUT it the LEFT JOIN computed
            # py_lower(object_name) over every CommonModule for EVERY subscription
            # (then filtered by name in Python), ~4s per call on ERP → ~250s across a
            # 60-node include_triggers tree. Now only the 0–few subscriptions whose
            # handler_procedure matches get joined. Cyrillic-aware (py_lower, not
            # COLLATE NOCASE). The Python casefold check below stays as cheap safety.
            for edge_type, source_kind, table, alias in (
                ("subscription", "EventSubscription", "event_subscriptions", "es"),
                ("scheduled_job", "ScheduledJob", "scheduled_jobs", "sj"),
            ):
                detail_col = "es.event" if alias == "es" else "''"
                try:
                    rows = self._conn.execute(
                        f"SELECT {alias}.name AS name, {detail_col} AS detail, "  # noqa: S608
                        f"{alias}.handler_module AS handler_module, "
                        f"{alias}.handler_procedure AS handler_procedure, "
                        f"{alias}.file AS file, mod.rel_path AS rel_path "
                        f"FROM {table} {alias} "
                        "LEFT JOIN modules mod ON mod.category='CommonModules' "
                        "  AND mod.module_type='Module' "
                        f"  AND {alias}.handler_module <> '' "
                        f"  AND py_lower(mod.object_name)=py_lower({alias}.handler_module) "
                        f"WHERE {alias}.handler_procedure <> '' "
                        f"  AND py_lower({alias}.handler_procedure)=py_lower(?)",
                        (proc_name,),
                    ).fetchall()
                except sqlite3.OperationalError:
                    rows = []
                for r in rows:
                    if (r["handler_procedure"] or "").casefold() != proc_cf:
                        continue
                    rel_path = r["rel_path"]
                    cand_key = _make_callee_key(rel_path, r["handler_procedure"]) if rel_path else None
                    if exact_key is not None and cand_key is not None and cand_key != exact_key:
                        # Provably a DIFFERENT module's handler → not our target.
                        continue
                    resolved = bool(exact_key is not None and cand_key is not None and cand_key == exact_key)
                    edges.append(
                        {
                            "edge_type": edge_type,
                            "source_name": r["name"],
                            "source_kind": source_kind,
                            "detail": r["detail"] or "",
                            "file": r["file"],
                            "line": None,
                            "caller_name": r["handler_procedure"],
                            "object_name": r["handler_module"],
                            "category": "CommonModules" if rel_path else None,
                            "target_key": cand_key if resolved else None,
                            "resolved": resolved,
                        }
                    )
        return edges

    def get_exports_by_path(self, rel_path: str) -> list[dict] | None:
        """Get exported methods for a given module path.

        Returns:
            List of dicts {name, type, line, end_line, params} or None
            if the module is not in the index.
        """
        with self._lock:
            mod_row = self._conn.execute("SELECT id FROM modules WHERE rel_path = ?", (rel_path,)).fetchone()
            if mod_row is None:
                return None

            rows = self._conn.execute(
                "SELECT name, type, line, end_line, params "
                "FROM methods WHERE module_id = ? AND is_export = 1 ORDER BY line",
                (mod_row["id"],),
            ).fetchall()

            return [
                {
                    "name": r["name"],
                    "type": r["type"],
                    "line": r["line"],
                    "end_line": r["end_line"],
                    "params": r["params"],
                }
                for r in rows
            ]

    @_transient_safe(lambda: None)
    def get_register_movements(self, document_name: str) -> list[dict] | None:
        """Get register movements for a given document.

        Returns list of {register_name, source, file} or None if table empty/missing.
        """
        with self._lock:
            try:
                rows = self._conn.execute(
                    "SELECT DISTINCT register_name, source, file "
                    "FROM register_movements WHERE document_name = ? COLLATE NOCASE",
                    (document_name,),
                ).fetchall()
            except sqlite3.OperationalError:
                return None

            if not rows:
                try:
                    cnt = self._conn.execute("SELECT COUNT(*) AS cnt FROM register_movements").fetchone()
                    if cnt and cnt["cnt"] == 0:
                        return None
                except sqlite3.Error:
                    return None

            # Read-time noise scrub (v1.27.0): drop code-extracted rows whose register name is
            # really a Движения collection-method (Записать/Очистить/…). Cleans pre-existing
            # indexes built before the lookahead fix WITHOUT a rebuild, and centrally covers
            # every consumer of this reader (find_register_movements + get_object_profile's
            # registers section). Only source=="code" is filtered — erp_mechanism/manager_table/
            # adapted come from quoted-name regexes and are trusted. Empty/missing table → None
            # is decided above, so an all-noise document returns [] (authoritative), never None.
            return [
                {"register_name": r["register_name"], "source": r["source"], "file": r["file"]}
                for r in rows
                if not (r["source"] == "code" and (r["register_name"] or "").lower() in _MOVEMENT_METHOD_NOISE)
            ]

    @_transient_safe(lambda: None)
    def get_register_writers(self, register_name: str) -> list[dict] | None:
        """Get documents that write to a given register.

        Returns list of {document_name, source, file} or None if table empty/missing.
        """
        with self._lock:
            try:
                rows = self._conn.execute(
                    "SELECT document_name, source, file FROM register_movements WHERE register_name = ? COLLATE NOCASE",
                    (register_name,),
                ).fetchall()
            except sqlite3.OperationalError:
                return None

            if not rows:
                try:
                    cnt = self._conn.execute("SELECT COUNT(*) AS cnt FROM register_movements").fetchone()
                    if cnt and cnt["cnt"] == 0:
                        return None
                except sqlite3.Error:
                    return None

            # Read-time consistency with get_register_movements: a reverse lookup by a noise
            # method-name (get_register_writers("Записать")) must not surface code-rows from a
            # pre-existing index. Filter code-source rows when the QUERIED name is stop-set;
            # non-code sources (real quoted-name mechanisms) are kept.
            arg_is_noise = (register_name or "").lower() in _MOVEMENT_METHOD_NOISE
            return [
                {"document_name": r["document_name"], "source": r["source"], "file": r["file"]}
                for r in rows
                if not (arg_is_noise and r["source"] == "code")
            ]

    @_transient_safe(lambda: None)
    def get_roles(self, object_name: str) -> list[dict] | None:
        """Get roles that grant rights to a given object.

        Returns list of {role_name, object_name, right_name, file} or None
        if role_rights table is empty/missing.
        """
        with self._lock:
            try:
                rows = self._conn.execute(
                    "SELECT role_name, object_name, right_name, file FROM role_rights "
                    "WHERE py_lower(object_name) LIKE py_lower(?)",
                    (f"%{object_name}%",),
                ).fetchall()
            except sqlite3.OperationalError:
                return None

            if not rows:
                # Check if the table has any data at all
                try:
                    cnt = self._conn.execute("SELECT COUNT(*) AS cnt FROM role_rights").fetchone()
                    if cnt and cnt["cnt"] == 0:
                        return None
                except sqlite3.Error:
                    return None

            # Group by role_name, deduplicate rights
            role_map: dict[str, dict] = {}
            for r in rows:
                key = r["role_name"]
                if key not in role_map:
                    role_map[key] = {
                        "role_name": r["role_name"],
                        "object": r["object_name"],
                        "rights": [],
                        "file": r["file"],
                    }
                right = r["right_name"]
                if right not in role_map[key]["rights"]:
                    role_map[key]["rights"].append(right)
            return list(role_map.values())

    @_transient_safe(lambda: None)
    def get_roles_exact(self, object_ref: str, include_members: bool = False) -> list[dict] | None:
        """Roles granting rights to an EXACT object ref — no substring false-positives.

        Where ``get_roles`` matches ``object_name LIKE '%name%'`` (so ``Document.Заказ``
        pulls in ``Document.ЗаказПоставщику`` and ``Document.X`` pulls in ``Document.XYZ``),
        this matches ``role_rights.object_name`` against the canonical English ref
        (e.g. ``Document.ЗаказПоставщику``) exactly. Read-only; no schema/builder bump.

        Args:
            object_ref: canonical English ``Type.Name`` ref (see ``_CATEGORY_TO_TYPE_PREFIX``).
            include_members: when ``True`` also include member rights whose ``object_name``
                starts with ``<ref>.`` (e.g. ``Document.X.TabularSection.Y.Attribute.Z``).
                Default ``False`` = object-level grants only.

        Returns:
            Grouped ``[{role_name, object, rights:[...], file}]`` (same shape as
            ``get_roles``), or ``None`` if the ``role_rights`` table is empty/missing.
            ``[]`` when the table is present but no role grants the exact ref.
        """
        with self._lock:
            try:
                if include_members:
                    # Escape LIKE wildcards in the ref so a literal '_' or '%' (both legal in
                    # 1C identifiers, e.g. тст_Смета) cannot act as a wildcard and over-match a
                    # near-homonym's member grants. The trailing '.%' stays the intended wildcard;
                    # the '=' branch is exact (no LIKE) and needs no escaping.
                    like_prefix = object_ref.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                    rows = self._conn.execute(
                        "SELECT role_name, object_name, right_name, file FROM role_rights "
                        "WHERE object_name = ? COLLATE NOCASE OR object_name LIKE ? || '.%' ESCAPE '\\'",
                        (object_ref, like_prefix),
                    ).fetchall()
                else:
                    rows = self._conn.execute(
                        "SELECT role_name, object_name, right_name, file FROM role_rights "
                        "WHERE object_name = ? COLLATE NOCASE",
                        (object_ref,),
                    ).fetchall()
            except sqlite3.OperationalError:
                return None

            if not rows:
                # Distinguish empty table (→ None, caller marks unavailable) from
                # "table has data but exact ref matched nothing" (→ [], authoritative).
                try:
                    cnt = self._conn.execute("SELECT COUNT(*) AS cnt FROM role_rights").fetchone()
                    if cnt and cnt["cnt"] == 0:
                        return None
                except sqlite3.Error:
                    return None

            role_map: dict[str, dict] = {}
            for r in rows:
                key = r["role_name"]
                if key not in role_map:
                    role_map[key] = {
                        "role_name": r["role_name"],
                        "object": r["object_name"],
                        "rights": [],
                        "file": r["file"],
                    }
                right = r["right_name"]
                if right not in role_map[key]["rights"]:
                    role_map[key]["rights"].append(right)
            return list(role_map.values())

    @_transient_safe(lambda: None)
    def get_enum_values(self, enum_name: str) -> dict | None:
        """Get enum definition from the index.

        Args:
            enum_name: Enum name (or fragment, case-insensitive).

        Returns:
            Dict with name, synonym, values, file — or None if table missing / not found.
        """
        with self._lock:
            try:
                rows = self._conn.execute("SELECT name, synonym, values_json, source_file FROM enum_values").fetchall()
            except sqlite3.OperationalError:
                return None

            if not rows:
                return None

            name_lower = enum_name.lower()
            for r in rows:
                if name_lower in r["name"].lower():
                    values = []
                    try:
                        values = json.loads(r["values_json"]) if r["values_json"] else []
                    except (ValueError, TypeError):
                        pass
                    return {
                        "name": r["name"],
                        "synonym": r["synonym"] or "",
                        "values": values,
                        "file": r["source_file"] or "",
                    }

            # Table exists but enum not found — return error, don't fallback
            return {"error": f"Перечисление '{enum_name}' не найдено"}

    @_transient_safe(lambda: None)
    def get_startup_meta(self) -> dict | None:
        """Get cached startup metadata for fast rlm_start.

        Returns dict with source_format, shallow_bsl_count, config_role,
        config_name, extension_prefix, extension_purpose, has_configuration_xml —
        or None if required keys are missing.
        """
        with self._lock:
            meta: dict[str, str | None] = {}
            required_keys = ("source_format", "shallow_bsl_count")
            for key in (
                "source_format",
                "shallow_bsl_count",
                "config_role",
                "config_name",
                "extension_prefix",
                "extension_purpose",
                "has_configuration_xml",
            ):
                row = self._conn.execute("SELECT value FROM index_meta WHERE key = ?", (key,)).fetchone()
                meta[key] = row["value"] if row else None

            # Required keys must be present
            if any(meta.get(k) is None for k in required_keys):
                return None

            return meta

    @_transient_safe(list)
    def get_detected_prefixes(self) -> list[str]:
        """Return detected custom prefixes from index_meta, or empty list."""
        with self._lock:
            row = self._conn.execute("SELECT value FROM index_meta WHERE key = 'detected_prefixes'").fetchone()
            if row and row["value"]:
                try:
                    return json.loads(row["value"])
                except (json.JSONDecodeError, TypeError):
                    return []
            return []

    @_transient_safe(lambda: None)
    def get_all_modules(self) -> list[dict] | None:
        """Return all modules from the index for fast _index_state init.

        Returns:
            list of dicts {rel_path, category, object_name, module_type, form_name}.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT rel_path, category, object_name, module_type, form_name FROM modules"
            ).fetchall()
            return [
                {
                    "rel_path": r["rel_path"],
                    "category": r["category"],
                    "object_name": r["object_name"],
                    "module_type": r["module_type"],
                    "form_name": r["form_name"],
                }
                for r in rows
            ]

    # ------------------------------------------------------------------
    # File navigation (Level-4: file_paths table)
    # ------------------------------------------------------------------
    @property
    def has_file_paths(self) -> bool:
        """Check if file_paths table exists and has data."""
        with self._lock:
            try:
                row = self._conn.execute("SELECT COUNT(*) AS cnt FROM file_paths").fetchone()
                return row is not None and row["cnt"] > 0
            except sqlite3.OperationalError:
                return False

    def glob_files(self, pattern: str) -> list[str] | None:
        """Resolve a glob pattern from the index.

        Returns sorted list of POSIX-relative paths, or None if the pattern
        is not supported (caller should fall back to FS).
        """
        strategy = _can_index_glob(pattern)
        if strategy is None:
            return None
        kind, params = strategy
        with self._lock:
            try:
                if kind == "by_extension":
                    rows = self._conn.execute(
                        "SELECT rel_path FROM file_paths WHERE extension = ? ORDER BY rel_path",
                        (params["ext"],),
                    ).fetchall()
                elif kind == "under_prefix":
                    prefix = params["prefix"]
                    rows = self._conn.execute(
                        "SELECT rel_path FROM file_paths WHERE rel_path LIKE ? ORDER BY rel_path",
                        (prefix + "/%",),
                    ).fetchall()
                elif kind == "dir_file":
                    dir_pat = params["dir"]
                    fname = params["file"]
                    rows = self._conn.execute(
                        "SELECT rel_path FROM file_paths WHERE dir_path LIKE ? AND filename = ? ORDER BY rel_path",
                        (dir_pat + "/%", fname),
                    ).fetchall()
                elif kind == "exact":
                    rows = self._conn.execute(
                        "SELECT rel_path FROM file_paths WHERE rel_path = ?",
                        (params["path"],),
                    ).fetchall()
                elif kind == "prefix_recursive_ext":
                    prefix = params["prefix"]
                    ext = params["ext"]
                    rows = self._conn.execute(
                        "SELECT rel_path FROM file_paths WHERE rel_path LIKE ? AND extension = ? ORDER BY rel_path",
                        (prefix + "/%", ext),
                    ).fetchall()
                elif kind == "under_prefix_ext":
                    dir_name = params["dir_name"]
                    ext = params["ext"]
                    rows = self._conn.execute(
                        "SELECT rel_path FROM file_paths WHERE dir_path LIKE ? AND extension = ? ORDER BY rel_path",
                        (f"%/{dir_name}/%", ext),
                    ).fetchall()
                elif kind == "name_wildcard":
                    name_prefix = params["name_prefix"]
                    ext = params.get("ext", "")
                    if ext:
                        rows = self._conn.execute(
                            "SELECT rel_path FROM file_paths WHERE filename LIKE ? AND extension = ? ORDER BY rel_path",
                            (name_prefix + "%", ext),
                        ).fetchall()
                    else:
                        rows = self._conn.execute(
                            "SELECT rel_path FROM file_paths WHERE filename LIKE ? ORDER BY rel_path",
                            (name_prefix + "%",),
                        ).fetchall()
                else:
                    return None
            except sqlite3.OperationalError:
                return None

        return [r["rel_path"] for r in rows]

    def tree_paths(self, prefix: str, max_depth: int) -> list[str] | None:
        """Get file paths for tree rendering from the index.

        Args:
            prefix: Directory prefix (POSIX), empty string for root.
            max_depth: Maximum depth relative to prefix.

        Returns sorted list of POSIX-relative paths, or None if table missing.
        """
        with self._lock:
            try:
                if prefix and prefix != ".":
                    # Normalize prefix
                    prefix = prefix.replace("\\", "/").strip("/")
                    base_depth = prefix.count("/") + 1
                    rows = self._conn.execute(
                        "SELECT rel_path FROM file_paths WHERE rel_path LIKE ? AND depth <= ? ORDER BY rel_path",
                        (prefix + "/%", base_depth + max_depth),
                    ).fetchall()
                else:
                    rows = self._conn.execute(
                        "SELECT rel_path FROM file_paths WHERE depth <= ? ORDER BY rel_path",
                        (max_depth,),
                    ).fetchall()
            except sqlite3.OperationalError:
                return None

        return [r["rel_path"] for r in rows]

    def find_files_indexed(self, name: str, limit: int = 100) -> list[str] | None:
        """Find files by substring match using the index.

        Ranking: exact filename > prefix filename > substring filename > substring path.

        Returns sorted list of POSIX-relative paths, or None if table missing.
        """
        if not name:
            return None
        with self._lock:
            try:
                # Cyrillic-aware: py_lower() UDF folds case for Unicode too (SQLite
                # LIKE/NOCASE fold ASCII only). NO SQL LIMIT — Python ranking (below)
                # needs ALL matches, иначе расширённый py_lower-набор вытолкнет
                # качественный exact/prefix с длинным путём за окно ДО ранжирования
                # (mirror search_objects; ORDER BY и так сканирует всё — LIMIT не экономил).
                # %/_ в name экранируются (ESCAPE '\') → литеральный поиск, а не SQL-
                # wildcard: иначе '%'/'_'/один символ дают match-all и SQL вытягивает
                # все ~110K строк до Python-постфильтра.
                needle_sql = "%" + _escape_for_sql_like(name) + "%"
                rows = self._conn.execute(
                    "SELECT rel_path, filename "
                    "FROM file_paths "
                    "WHERE py_lower(filename) LIKE py_lower(?) ESCAPE '\\' "
                    "   OR py_lower(rel_path) LIKE py_lower(?) ESCAPE '\\' "
                    "ORDER BY length(rel_path), rel_path",
                    (needle_sql, needle_sql),
                ).fetchall()
            except sqlite3.OperationalError:
                return None

        # Python-side ranking: exact filename > prefix > substring filename > substring path
        needle_lower = name.lower()
        ranked: list[tuple[int, str]] = []
        for r in rows:
            fn = r["filename"].lower()
            rp = r["rel_path"].lower()
            if fn == needle_lower:
                rank = 0
            elif fn.startswith(needle_lower):
                rank = 1
            elif needle_lower in fn:
                rank = 2
            elif needle_lower in rp:
                rank = 3
            else:
                continue
            ranked.append((rank, r["rel_path"]))
        ranked.sort(key=lambda x: (x[0], len(x[1]), x[1]))
        return [rp for _, rp in ranked[:limit]]

    @_transient_safe(_zero_stats)
    def get_statistics(self) -> dict:
        """Get summary statistics about the index.

        Returns:
            dict with keys: modules, methods, calls, exports, built_at,
            config_name, config_version, source_format, has_metadata,
            event_subscriptions, scheduled_jobs, functional_options.
        """
        with self._lock:
            stats: dict = {}

            row = self._conn.execute("SELECT COUNT(*) AS cnt FROM modules").fetchone()
            stats["modules"] = row["cnt"] if row else 0

            row = self._conn.execute("SELECT COUNT(*) AS cnt FROM methods").fetchone()
            stats["methods"] = row["cnt"] if row else 0

            try:
                row = self._conn.execute("SELECT COUNT(*) AS cnt FROM calls").fetchone()
                stats["calls"] = row["cnt"] if row else 0
            except sqlite3.Error:
                stats["calls"] = 0

            row = self._conn.execute("SELECT COUNT(*) AS cnt FROM methods WHERE is_export = 1").fetchone()
            stats["exports"] = row["cnt"] if row else 0

            # built_at from meta
            meta_row = self._conn.execute("SELECT value FROM index_meta WHERE key = 'built_at'").fetchone()
            stats["built_at"] = float(meta_row["value"]) if meta_row else None

            # Configuration metadata from index_meta
            for key in (
                "config_name",
                "config_version",
                "config_synonym",
                "config_vendor",
                "source_format",
                "config_role",
                "has_metadata",
                "has_fts",
                "bsl_count",
                "builder_version",
            ):
                meta_row = self._conn.execute("SELECT value FROM index_meta WHERE key = ?", (key,)).fetchone()
                stats[key] = meta_row["value"] if meta_row else None

            # Convert stringly-typed flags to proper booleans
            for flag in ("has_fts", "has_metadata"):
                stats[flag] = stats.get(flag) == "1"

            # Convert bsl_count to int
            if stats.get("bsl_count") is not None:
                stats["bsl_count"] = int(stats["bsl_count"])

            # Level-2 metadata counts
            for table in ("event_subscriptions", "scheduled_jobs", "functional_options"):
                try:
                    row = self._conn.execute(f"SELECT COUNT(*) AS cnt FROM {table}").fetchone()  # noqa: S608
                    stats[table] = row["cnt"] if row else 0
                except sqlite3.Error:
                    stats[table] = 0

            # Level-3 counts
            for table in ("role_rights", "register_movements", "enum_values", "subsystem_content"):
                try:
                    row = self._conn.execute(f"SELECT COUNT(*) AS cnt FROM {table}").fetchone()  # noqa: S608
                    stats[table] = row["cnt"] if row else 0
                except sqlite3.Error:
                    stats[table] = 0

            # Level-5: integration metadata counts
            for table in ("http_services", "web_services", "xdto_packages"):
                try:
                    row = self._conn.execute(f"SELECT COUNT(*) AS cnt FROM {table}").fetchone()  # noqa: S608
                    stats[table] = row["cnt"] if row else 0
                except sqlite3.Error:
                    stats[table] = 0

            # Level-4: file navigation
            try:
                row = self._conn.execute("SELECT COUNT(*) AS cnt FROM file_paths").fetchone()
                stats["file_paths"] = row["cnt"] if row else 0
            except sqlite3.Error:
                stats["file_paths"] = 0

            # Level-6: object synonyms
            try:
                row = self._conn.execute("SELECT COUNT(*) AS cnt FROM object_synonyms").fetchone()
                stats["object_synonyms"] = row["cnt"] if row else 0
            except sqlite3.Error:
                stats["object_synonyms"] = 0

            # Level-7: regions and module headers
            for table in ("regions", "module_headers"):
                try:
                    row = self._conn.execute(f"SELECT COUNT(*) AS cnt FROM {table}").fetchone()  # noqa: S608
                    stats[table] = row["cnt"] if row else 0
                except sqlite3.Error:
                    stats[table] = 0

            # Level-8: extension overrides
            try:
                row = self._conn.execute("SELECT COUNT(*) AS cnt FROM extension_overrides").fetchone()
                stats["extension_overrides"] = row["cnt"] if row else 0
            except sqlite3.Error:
                stats["extension_overrides"] = 0

            # Level-9: form elements
            try:
                row = self._conn.execute("SELECT COUNT(*) AS cnt FROM form_elements").fetchone()
                stats["form_elements"] = row["cnt"] if row else 0
            except sqlite3.Error:
                stats["form_elements"] = 0

            # Level-11: object attributes and predefined items
            for table in ("object_attributes", "predefined_items"):
                try:
                    row = self._conn.execute(f"SELECT COUNT(*) AS cnt FROM {table}").fetchone()  # noqa: S608
                    stats[table] = row["cnt"] if row else 0
                except sqlite3.Error:
                    stats[table] = 0

            # Level-12 (v1.9.0): metadata_references + 3 specialised tables
            for table in (
                "metadata_references",
                "exchange_plan_content",
                "defined_types",
                "characteristic_types",
                "metadata_code_usages",
            ):
                try:
                    row = self._conn.execute(f"SELECT COUNT(*) AS cnt FROM {table}").fetchone()  # noqa: S608
                    stats[table] = row["cnt"] if row else 0
                except sqlite3.Error:
                    stats[table] = 0

            # Git acceleration info
            meta_row = self._conn.execute("SELECT value FROM index_meta WHERE key = 'git_head_commit'").fetchone()
            stats["git_head_commit"] = meta_row["value"] if meta_row else None
            # Live check: git available at base_path right now?
            bp_row = self._conn.execute("SELECT value FROM index_meta WHERE key = 'base_path'").fetchone()
            bp = bp_row["value"] if bp_row else None
            stats["git_accelerated"] = stats["git_head_commit"] is not None and bp is not None and _git_available(bp)

            return stats

    @property
    def has_fts(self) -> bool:
        """Check if the FTS5 full-text search index exists."""
        with self._lock:
            try:
                row = self._conn.execute("SELECT COUNT(*) AS cnt FROM methods_fts").fetchone()
                return row is not None and row["cnt"] > 0
            except sqlite3.OperationalError:
                return False

    @_transient_safe(list)
    def search_methods(self, query: str, limit: int = 30) -> list[dict]:
        """FTS5 full-text search for methods by substring.

        Uses trigram tokenizer for substring matching with BM25 ranking.

        Args:
            query: Search query (substring match).
            limit: Max results (default 30).

        Returns:
            List of dicts ordered by relevance. Empty list if FTS not built.
        """
        if not query or not query.strip():
            return []
        with self._lock:
            try:
                # Wrap query in quotes for trigram substring matching
                fts_query = '"' + query.replace('"', '""') + '"'
                rows = self._conn.execute(
                    "SELECT "
                    "  m.name, m.type, m.is_export, m.line, m.end_line, m.params, "
                    "  mod.rel_path AS module_path, mod.object_name, "
                    "  methods_fts.rank "
                    "FROM methods_fts "
                    "JOIN methods m ON m.id = methods_fts.rowid "
                    "JOIN modules mod ON mod.id = m.module_id "
                    "WHERE methods_fts MATCH ? "
                    "ORDER BY methods_fts.rank "
                    "LIMIT ?",
                    (fts_query, limit),
                ).fetchall()

                return [
                    {
                        "name": r["name"],
                        "type": r["type"],
                        "is_export": bool(r["is_export"]),
                        "line": r["line"],
                        "end_line": r["end_line"],
                        "params": r["params"],
                        "module_path": r["module_path"],
                        "object_name": r["object_name"],
                        "rank": r["rank"],
                    }
                    for r in rows
                ]
            except sqlite3.OperationalError:
                return []

    @_transient_safe(lambda: None)
    def search_objects(self, query: str, limit: int = 50) -> list[dict] | None:
        """Search objects by business name (synonym) or technical name.

        Uses py_lower() UDF for Cyrillic case-insensitive LIKE.
        Python-side 4-level ranking: exact name > prefix > synonym substring > category.

        Args:
            query: Search query (case-insensitive substring).
            limit: Max results (default 50).

        Returns:
            Ranked list of dicts, or None if table missing.
        """
        with self._lock:
            try:
                if not query or not query.strip():
                    # Empty query: alphabetical listing
                    rows = self._conn.execute(
                        "SELECT object_name, category, synonym, file "
                        "FROM object_synonyms "
                        "ORDER BY category, object_name LIMIT ?",
                        (limit,),
                    ).fetchall()
                else:
                    q = query.strip()
                    # %/_ экранируются (ESCAPE '\') → литеральный поиск, а не SQL-
                    # wildcard: иначе '%'/'_' матчат ВСЁ, а ранжирование (else: rank=3)
                    # вернуло бы произвольные объекты вместо литерального совпадения.
                    like_q = f"%{_escape_for_sql_like(q)}%"
                    # No SQL LIMIT — full scan is <15ms on 15K rows,
                    # Python ranking needs ALL matches to guarantee
                    # exact name (rank 0) is never lost.
                    rows = self._conn.execute(
                        "SELECT object_name, category, synonym, file "
                        "FROM object_synonyms "
                        "WHERE py_lower(synonym) LIKE py_lower(?) ESCAPE '\\' "
                        "   OR py_lower(object_name) LIKE py_lower(?) ESCAPE '\\'",
                        (like_q, like_q),
                    ).fetchall()

                if not query or not query.strip():
                    return [
                        {
                            "object_name": r["object_name"],
                            "category": r["category"],
                            "synonym": r["synonym"],
                            "file": r["file"],
                        }
                        for r in rows
                    ]

                # Python-side ranking
                q_lower = query.strip().lower()
                ranked: list[tuple[int, str, str, dict]] = []
                for r in rows:
                    d = {
                        "object_name": r["object_name"],
                        "category": r["category"],
                        "synonym": r["synonym"],
                        "file": r["file"],
                    }
                    name_lower = r["object_name"].lower()
                    synonym_lower = r["synonym"].lower()

                    if name_lower == q_lower:
                        rank = 0  # exact object_name
                    elif name_lower.startswith(q_lower):
                        rank = 1  # prefix object_name
                    elif (
                        q_lower in synonym_lower.split(": ", 1)[-1]
                        if ": " in synonym_lower
                        else q_lower in synonym_lower
                    ):
                        rank = 2  # substring in raw synonym (after prefix)
                    else:
                        rank = 3  # substring in category prefix
                    ranked.append((rank, r["category"], r["object_name"], d))

                ranked.sort(key=lambda x: (x[0], x[1], x[2]))
                return [item[3] for item in ranked[:limit]]

            except sqlite3.OperationalError:
                return None

    @_transient_safe(lambda: None)
    def search_regions(self, query: str, limit: int = 200) -> list[dict] | None:
        """Search code regions (#Область) by name substring.

        Returns:
            List of dicts, or None if table missing.
        """
        with self._lock:
            try:
                if not query or not query.strip():
                    rows = self._conn.execute(
                        "SELECT r.name, r.line, r.end_line, "
                        "m.rel_path AS module_path, m.object_name, m.category "
                        "FROM regions r "
                        "JOIN modules m ON m.id = r.module_id "
                        "ORDER BY r.name LIMIT ?",
                        (limit,),
                    ).fetchall()
                else:
                    rows = self._conn.execute(
                        "SELECT r.name, r.line, r.end_line, "
                        "m.rel_path AS module_path, m.object_name, m.category "
                        "FROM regions r "
                        "JOIN modules m ON m.id = r.module_id "
                        "WHERE py_lower(r.name) LIKE '%' || py_lower(?) || '%' "
                        "LIMIT ?",
                        (query.strip(), limit),
                    ).fetchall()
                return [
                    {
                        "name": r["name"],
                        "line": r["line"],
                        "end_line": r["end_line"],
                        "module_path": r["module_path"],
                        "object_name": r["object_name"],
                        "category": r["category"],
                    }
                    for r in rows
                ]
            except sqlite3.OperationalError:
                return None

    @_transient_safe(lambda: None)
    def search_module_headers(self, query: str, limit: int = 200) -> list[dict] | None:
        """Search module header comments by substring.

        Returns:
            List of dicts, or None if table missing.
        """
        with self._lock:
            try:
                if not query or not query.strip():
                    rows = self._conn.execute(
                        "SELECT m.rel_path AS module_path, m.object_name, "
                        "m.category, mh.header_comment "
                        "FROM module_headers mh "
                        "JOIN modules m ON m.id = mh.module_id "
                        "LIMIT ?",
                        (limit,),
                    ).fetchall()
                else:
                    rows = self._conn.execute(
                        "SELECT m.rel_path AS module_path, m.object_name, "
                        "m.category, mh.header_comment "
                        "FROM module_headers mh "
                        "JOIN modules m ON m.id = mh.module_id "
                        "WHERE py_lower(mh.header_comment) LIKE '%' || py_lower(?) || '%' "
                        "LIMIT ?",
                        (query.strip(), limit),
                    ).fetchall()
                return [
                    {
                        "module_path": r["module_path"],
                        "object_name": r["object_name"],
                        "category": r["category"],
                        "header_comment": r["header_comment"],
                    }
                    for r in rows
                ]
            except sqlite3.OperationalError:
                return None

    @_transient_safe(lambda: None)
    def count_regions(self, query: str = "") -> int | None:
        """COUNT of code regions matching ``query`` (same WHERE as search_regions).

        Index-side total — does NOT include live extension rows. None if the
        ``regions`` table is missing (как у search_regions).
        """
        with self._lock:
            try:
                # JOIN modules mirrors search_regions exactly: the INNER JOIN drops
                # orphaned region rows (module_id w/o a modules row) so the count
                # never diverges from len(search_regions(q, limit=huge)).
                if not query or not query.strip():
                    row = self._conn.execute(
                        "SELECT COUNT(*) FROM regions r JOIN modules m ON m.id = r.module_id"
                    ).fetchone()
                else:
                    row = self._conn.execute(
                        "SELECT COUNT(*) FROM regions r JOIN modules m ON m.id = r.module_id "
                        "WHERE py_lower(r.name) LIKE '%' || py_lower(?) || '%'",
                        (query.strip(),),
                    ).fetchone()
                return int(row[0]) if row is not None else 0
            except sqlite3.OperationalError:
                return None

    @_transient_safe(lambda: None)
    def group_regions(self, query: str = "", by: str = "name", limit: int = 200) -> dict | None:
        """Топ-N областей, сгруппированных по ``name`` либо ``category``.

        Зачем отдельный метод, а не срез выдачи: списочная ветка ``search_regions``
        при пустом query идёт ``ORDER BY r.name``, поэтому ``limit`` даёт АЛФАВИТНЫЙ
        префикс, а не выборку. Любая агрегация по такому срезу систематически неверна
        (на боевой конфигурации топ-10 по 5000 строкам из 57 652 не пересекался с
        истинным). Здесь считает SQLite по ПОЛНОМУ набору, а ``limit`` режет уже
        готовые группы.

        WHERE повторяет ``search_regions``/``count_regions`` слово в слово, поэтому
        сумма COUNT по ВСЕМ группам равна ``count_regions(query)``.

        Returns:
            ``{"groups": [{"key", "count"}], "groups_total": N}`` (группы отсортированы
            по убыванию count, при равенстве — по ключу), либо None если таблицы нет.
        """
        col = "m.category" if by == "category" else "r.name"
        with self._lock:
            try:
                where = ""
                params: tuple = ()
                if query and query.strip():
                    where = "WHERE py_lower(r.name) LIKE '%' || py_lower(?) || '%' "
                    params = (query.strip(),)
                rows = self._conn.execute(
                    f"SELECT {col} AS k, COUNT(*) AS c "
                    "FROM regions r JOIN modules m ON m.id = r.module_id "
                    f"{where}"
                    f"GROUP BY {col} ORDER BY c DESC, k LIMIT ?",
                    (*params, limit),
                ).fetchall()
                total_row = self._conn.execute(
                    "SELECT COUNT(*) FROM (SELECT 1 FROM regions r JOIN modules m ON m.id = r.module_id "
                    f"{where}"
                    f"GROUP BY {col})",
                    params,
                ).fetchone()
                return {
                    "groups": [{"key": r["k"], "count": int(r["c"])} for r in rows],
                    "groups_total": int(total_row[0]) if total_row is not None else 0,
                }
            except sqlite3.OperationalError:
                return None

    @_transient_safe(lambda: None)
    def count_module_headers(self, query: str = "") -> int | None:
        """COUNT of module headers matching ``query`` (same WHERE as search_module_headers).

        Index-side total — does NOT include live extension rows. None if the
        ``module_headers`` table is missing.
        """
        with self._lock:
            try:
                # JOIN modules mirrors search_module_headers exactly (drops orphaned
                # header rows so the count matches len(search_module_headers(q, huge))).
                if not query or not query.strip():
                    row = self._conn.execute(
                        "SELECT COUNT(*) FROM module_headers mh JOIN modules m ON m.id = mh.module_id"
                    ).fetchone()
                else:
                    row = self._conn.execute(
                        "SELECT COUNT(*) FROM module_headers mh JOIN modules m ON m.id = mh.module_id "
                        "WHERE py_lower(mh.header_comment) LIKE '%' || py_lower(?) || '%'",
                        (query.strip(),),
                    ).fetchone()
                return int(row[0]) if row is not None else 0
            except sqlite3.OperationalError:
                return None

    @_transient_safe(lambda: None)
    def get_event_subscriptions(
        self,
        object_name: str = "",
        custom_only: bool = False,
        event_filter: list[str] | str | None = None,
        object_ref: str = "",
    ) -> list[dict] | None:
        """Get event subscriptions from the index, optionally filtered.

        Args:
            object_name: Имя объекта БЕЗ префикса. Матчинг exact-с-фолбэком (v1.28.0):
                         точное совпадение по ИМЕННОЙ части типа-источника; подстрока —
                         только если точных нет (вход = фрагмент имени). Category-blind.
            custom_only: Not applied here (requires prefix detection from helpers).
            object_ref:  Canonical ref ("Document.X"). Если задан — сравнение идет по
                         canonicalize_type_ref, то есть category-AWARE, и partial-фолбэка
                         НЕТ (явный ref = точное намерение).
            event_filter: Фильтр по событию (case-insensitive substring). Применяется
                          ПОСЛЕ классификации exact/partial — иначе отсутствие события у
                          точного источника воскрешало бы подстрочного омонима. Допустима
                          одна строка — она оборачивается в список: без этого Python
                          итерирует строку по символам и каждый символ становится
                          substring-матчем.

        Returns:
            List of dicts matching find_event_subscriptions format, or None
            if the table is empty / missing. При непустом object_name/object_ref каждая
            строка несет ``scope``: "exact" | "partial" | "universal"; без фильтра объекта
            возвращается прежний компактный список (без scope и без source_types).
        """
        # Normalize a bare string to a one-element list (see docstring).
        if isinstance(event_filter, str):
            event_filter = [event_filter] if event_filter else None
        with self._lock:
            try:
                # NB: НЕТ WHERE по событию (v1.28.0). Фильтр уехал в Python и применяется
                # ПОСЛЕ классификации exact/partial — см. развёрнутый комментарий ниже.
                # Таблица мала (сотни строк), полное чтение дешево.
                rows = self._conn.execute(
                    "SELECT name, synonym, event, handler_module, handler_procedure, "
                    "source_types, source_count, file FROM event_subscriptions"
                ).fetchall()
            except sqlite3.OperationalError:
                return None

            if not rows:
                # Таблица пуста/отсутствует → None (caller падает в live-фолбэк).
                # Случай «таблица непуста, а фильтр не совпал → [] (authoritative)»
                # теперь получается сам собой: rows непусты → возвращаем (возможно
                # пустой) отфильтрованный список.
                return None

            name_lower = object_name.lower() if object_name else ""
            ref_lower = object_ref.lower() if object_ref else ""
            entries: list[tuple[dict, list[str]]] = []

            for r in rows:
                source_types: list[str] = []
                try:
                    source_types = json.loads(r["source_types"]) if r["source_types"] else []
                except (ValueError, TypeError):
                    pass

                handler_module = r["handler_module"] or ""
                handler_procedure = r["handler_procedure"] or ""
                handler = f"CommonModule.{handler_module}.{handler_procedure}" if handler_module else handler_procedure

                entries.append(
                    (
                        {
                            "name": r["name"],
                            "synonym": r["synonym"] or "",
                            "source_types": source_types,
                            "source_count": r["source_count"] or 0,
                            "event": r["event"] or "",
                            "handler": handler,
                            "handler_module": handler_module,
                            "handler_procedure": handler_procedure,
                            "file": r["file"] or "",
                        },
                        source_types,
                    )
                )

            def _apply_event_filter(items: list[dict]) -> list[dict]:
                if not event_filter:
                    return items
                evs = [e.lower() for e in event_filter]
                return [s for s in items if any(ev in (s.get("event") or "").lower() for ev in evs)]

            if not name_lower and not ref_lower:
                compact = [{k: v for k, v in e.items() if k != "source_types"} for e, _ in entries]
                return _apply_event_filter(compact)

            # v1.28.0: EXACT-С-ФОЛБЭКОМ.
            # Раньше искомое имя сравнивалось с ПОЛНОЙ формой типа подстрокой
            # (`name in "CatalogObject.XПрисоединенныеФайлы"`), из-за чего полное имя
            # объекта цепляло более длинный омоним — ДРУГОЙ объект.
            #   1) сравниваем с ИМЕННОЙ частью типа (или canonical ref при object_ref);
            #   2) есть точные → отдаём только их (подстрочные не подмешиваем);
            #   3) точных нет → фолбэк на подстроку (вход был ФРАГМЕНТОМ имени).
            # ПОРЯДОК КРИТИЧЕН: классификация идёт по ПОЛНОМУ набору подписок, а
            # event_filter применяется ПОСЛЕ. Если фильтровать по событию в SQL (как было),
            # точная подписка без нужного события выпадает ДО классификации, exact-набор
            # пустеет и partial-фолбэк воскрешает омонима.
            # Universal (пустой source_types) — catch-all, включается ВСЕГДА.
            exact_hits: list[dict] = []
            partial_hits: list[dict] = []
            universal: list[dict] = []

            for entry, source_types in entries:
                if not source_types:
                    universal.append({**entry, "scope": "universal"})
                    continue
                if ref_lower:
                    if any(t and canonicalize_type_ref(t).lower() == ref_lower for t in source_types):
                        exact_hits.append({**entry, "scope": "exact"})
                    continue  # типизированный вход — без partial-фолбэка
                names = [(t.split(".", 1)[1] if "." in t else t).lower() for t in source_types if t]
                if any(n == name_lower for n in names):
                    exact_hits.append({**entry, "scope": "exact"})
                elif any(name_lower in n for n in names):
                    partial_hits.append({**entry, "scope": "partial"})

            matched = exact_hits if exact_hits else partial_hits
            return _apply_event_filter(matched + universal)

    @_transient_safe(lambda: None)
    def get_event_subscriptions_exact(self, object_ref: str) -> list[dict] | None:
        """Event subscriptions whose source canonicalizes to an EXACT object ref.

        ``source_types`` stores type-FORMS (``DocumentObject.X``, ``DocumentRef.X``,
        ``CatalogManager.X`` …). Each element is normalized via ``canonicalize_type_ref``
        → ``Document.X`` and compared to ``object_ref`` exactly, so ``Document.X`` will
        NOT pull in ``Document.XYZ`` (the substring trap of ``get_event_subscriptions``).
        Subscriptions with an EMPTY ``source_types`` are **universal** catch-alls (they
        fire for any source) and are included too — ``scope`` marks each row ``"exact"``
        or ``"universal"``, and the list is sorted exact-first (v1.28.0, #2). Read-only;
        no schema/builder bump.

        Args:
            object_ref: canonical English ``Type.Name`` ref.

        Returns:
            ``list[dict]`` (``find_event_subscriptions`` shape, with ``source_types`` and
            a ``scope`` field) of subscriptions whose source is exactly this object OR
            universal, or ``None`` if the table is empty/missing. ``[]`` when present but
            nothing matches (and no universal subscriptions exist).
        """
        ref_lower = object_ref.lower()
        with self._lock:
            try:
                rows = self._conn.execute(
                    "SELECT name, synonym, event, handler_module, handler_procedure, "
                    "source_types, source_count, file FROM event_subscriptions"
                ).fetchall()
            except sqlite3.OperationalError:
                return None
            if not rows:
                return None
            result: list[dict] = []
            for r in rows:
                source_types: list[str] = []
                try:
                    source_types = json.loads(r["source_types"]) if r["source_types"] else []
                except (ValueError, TypeError):
                    pass
                # #2 (v1.28.0): a subscription with EMPTY source_types is a *universal*
                # catch-all — it fires for ANY source, so it applies to this object too.
                # Silently dropping it (the old ``if not any(...)``) undercounted the
                # section vs ``find_event_subscriptions`` (on configs with many shared
                # catch-all subscriptions the count was off by a large factor).
                # A NON-empty source_types still needs an EXACT canonical match (no
                # substring re-introduction); ``scope`` marks the two apart for the caller.
                if source_types and not any(t and canonicalize_type_ref(t).lower() == ref_lower for t in source_types):
                    continue
                scope = "exact" if source_types else "universal"
                handler_module = r["handler_module"] or ""
                handler_procedure = r["handler_procedure"] or ""
                handler = f"CommonModule.{handler_module}.{handler_procedure}" if handler_module else handler_procedure
                result.append(
                    {
                        "name": r["name"],
                        "synonym": r["synonym"] or "",
                        "source_types": source_types,
                        "source_count": r["source_count"] or 0,
                        "event": r["event"] or "",
                        "handler": handler,
                        "handler_module": handler_module,
                        "handler_procedure": handler_procedure,
                        "scope": scope,
                        "file": r["file"] or "",
                    }
                )
            # Stable: exact before universal so a limited preview always shows the
            # explicit-source subscriptions first (universal is the long catch-all tail).
            result.sort(key=lambda d: 0 if d["scope"] == "exact" else 1)
            return result

    @_transient_safe(lambda: None)
    def get_scheduled_jobs(self, name: str = "") -> list[dict] | None:
        """Get scheduled jobs from the index, optionally filtered by name.

        Returns:
            List of dicts matching find_scheduled_jobs format, or None
            if the table is empty / missing.
        """
        with self._lock:
            try:
                sql = (
                    "SELECT name, synonym, method_name, handler_module, "
                    "handler_procedure, use, predefined, restart_count, "
                    "restart_interval, file FROM scheduled_jobs"
                )
                params: tuple = ()
                if name:
                    sql += " WHERE py_lower(name) LIKE py_lower(?)"
                    params = (f"%{name}%",)
                rows = self._conn.execute(sql, params).fetchall()
            except sqlite3.OperationalError:
                return None

            if not rows:
                return [] if name else None

            return [
                {
                    "name": r["name"],
                    "synonym": r["synonym"] or "",
                    "method_name": r["method_name"] or "",
                    "handler_module": r["handler_module"] or "",
                    "handler_procedure": r["handler_procedure"] or "",
                    "use": bool(r["use"]),
                    "predefined": bool(r["predefined"]),
                    "restart_on_failure": {
                        "count": r["restart_count"] or 0,
                        "interval": r["restart_interval"] or 0,
                    },
                    "file": r["file"] or "",
                }
                for r in rows
            ]

    @_transient_safe(lambda: None)
    def get_functional_options(self, object_name: str = "") -> list[dict] | None:
        """Get functional options from the index, optionally filtered.

        Args:
            object_name: Filter by content list (case-insensitive substring).

        Returns:
            List of dicts matching find_functional_options format, or None
            if the table is empty / missing.
        """
        with self._lock:
            try:
                rows = self._conn.execute(
                    "SELECT name, synonym, location, content, file FROM functional_options"
                ).fetchall()
            except sqlite3.OperationalError:
                return None

            if not rows:
                return None

            result: list[dict] = []
            name_lower = object_name.lower() if object_name else ""

            for r in rows:
                content_list: list[str] = []
                try:
                    content_list = json.loads(r["content"]) if r["content"] else []
                except (ValueError, TypeError):
                    pass

                entry = {
                    "name": r["name"],
                    "synonym": r["synonym"] or "",
                    "location": r["location"] or "",
                    "content": content_list,
                    "file": r["file"] or "",
                }

                if name_lower:
                    if any(name_lower in c.lower() for c in content_list):
                        result.append(entry)
                else:
                    result.append(entry)

            return result

    @_transient_safe(lambda: None)
    def get_functional_options_exact(self, object_ref: str, include_members: bool = True) -> list[dict] | None:
        """Functional options whose content references an EXACT object ref.

        ``content`` stores canonical English refs (``Document.X`` and attribute-scoped
        ``Document.X.TabularSection.Y.Attribute.Z``). Matches ``object_ref`` exactly,
        plus — when *include_members* — content starting with ``<ref>.``. Avoids the
        substring false-positive of ``get_functional_options``. Read-only; no bump.

        Args:
            object_ref: canonical English ``Type.Name`` ref.
            include_members: include attribute/TS-scoped content (default ``True`` — an
                FO scoping a single attribute still "affects" the object).

        Returns:
            ``list[dict]`` (``find_functional_options`` shape) or ``None`` if the table
            is empty/missing. ``[]`` when present but nothing matches.
        """
        ref_lower = object_ref.lower()
        member_prefix = ref_lower + "."
        with self._lock:
            try:
                rows = self._conn.execute(
                    "SELECT name, synonym, location, content, file FROM functional_options"
                ).fetchall()
            except sqlite3.OperationalError:
                return None
            if not rows:
                return None
            result: list[dict] = []
            for r in rows:
                content_list: list[str] = []
                try:
                    content_list = json.loads(r["content"]) if r["content"] else []
                except (ValueError, TypeError):
                    pass
                matched = False
                for c in content_list:
                    cl = (c or "").lower()
                    if cl == ref_lower or (include_members and cl.startswith(member_prefix)):
                        matched = True
                        break
                if not matched:
                    continue
                result.append(
                    {
                        "name": r["name"],
                        "synonym": r["synonym"] or "",
                        "location": r["location"] or "",
                        "content": content_list,
                        "file": r["file"] or "",
                    }
                )
            return result

    @_transient_safe(lambda: None)
    def get_subsystems_for_object(self, object_name: str) -> list[dict] | None:
        """Find subsystems containing a given object.

        Args:
            object_name: Object name (case-insensitive substring match against object_ref).

        Returns:
            List of dicts {name, synonym, file, matched_refs} or None if table missing.
        """
        with self._lock:
            try:
                rows = self._conn.execute(
                    "SELECT subsystem_name, subsystem_synonym, object_ref, file "
                    "FROM subsystem_content WHERE py_lower(object_ref) LIKE py_lower(?)",
                    (f"%{object_name}%",),
                ).fetchall()
            except sqlite3.OperationalError:
                return None

            if not rows:
                return []  # Table exists but no matches — don't fallback

            # Group by subsystem

            grouped: dict[str, dict] = {}
            for r in rows:
                key = r["subsystem_name"]
                if key not in grouped:
                    grouped[key] = {"synonym": "", "file": "", "matched_refs": []}
                grouped[key]["synonym"] = r["subsystem_synonym"] or ""
                grouped[key]["file"] = r["file"] or ""
                grouped[key]["matched_refs"].append(r["object_ref"])

            return [
                {
                    "name": name,
                    "synonym": info["synonym"],
                    "file": info["file"],
                    "matched_refs": info["matched_refs"],
                }
                for name, info in grouped.items()
            ]

    @_transient_safe(lambda: None)
    def get_http_services(self, name: str = "") -> list[dict] | None:
        """Get HTTP services from the index, optionally filtered by name."""
        with self._lock:
            try:
                sql = "SELECT name, root_url, templates_json, file FROM http_services"
                params: tuple = ()
                if name:
                    sql += " WHERE py_lower(name) LIKE py_lower(?)"
                    params = (f"%{name}%",)
                rows = self._conn.execute(sql, params).fetchall()
            except sqlite3.OperationalError:
                return None

            if not rows:
                return [] if name else None

            return [
                {
                    "name": r["name"],
                    "root_url": r["root_url"],
                    "templates": json.loads(r["templates_json"]),
                    "file": r["file"],
                }
                for r in rows
            ]

    @_transient_safe(lambda: None)
    def get_web_services(self, name: str = "") -> list[dict] | None:
        """Get web services from the index, optionally filtered by name."""
        with self._lock:
            try:
                sql = "SELECT name, namespace, operations_json, file FROM web_services"
                params: tuple = ()
                if name:
                    sql += " WHERE py_lower(name) LIKE py_lower(?)"
                    params = (f"%{name}%",)
                rows = self._conn.execute(sql, params).fetchall()
            except sqlite3.OperationalError:
                return None

            if not rows:
                return [] if name else None

            return [
                {
                    "name": r["name"],
                    "namespace": r["namespace"],
                    "operations": json.loads(r["operations_json"]),
                    "file": r["file"],
                }
                for r in rows
            ]

    @_transient_safe(lambda: None)
    def get_xdto_packages(self, name: str = "") -> list[dict] | None:
        """Get XDTO packages from the index, optionally filtered by name."""
        with self._lock:
            try:
                sql = "SELECT name, namespace, types_json, file FROM xdto_packages"
                params: tuple = ()
                if name:
                    sql += " WHERE py_lower(name) LIKE py_lower(?)"
                    params = (f"%{name}%",)
                rows = self._conn.execute(sql, params).fetchall()
            except sqlite3.OperationalError:
                return None

            if not rows:
                return [] if name else None

            return [
                {
                    "name": r["name"],
                    "namespace": r["namespace"],
                    "types": json.loads(r["types_json"]),
                    "file": r["file"],
                }
                for r in rows
            ]

    # --- Level-8: extension overrides ---

    @_transient_safe(lambda: None)
    def get_extension_overrides(
        self,
        object_name: str = "",
        method_name: str = "",
    ) -> list[dict] | None:
        """Query extension overrides with optional filters.

        Returns None if the table doesn't exist (pre-v9 index).
        """
        with self._lock:
            try:
                rows = self._conn.execute("SELECT * FROM extension_overrides").fetchall()
                result = [dict(r) for r in rows]
                # Python-side case-insensitive filter (COLLATE NOCASE doesn't work for Cyrillic)
                if object_name:
                    obj_lower = object_name.lower()
                    result = [r for r in result if r.get("object_name", "").lower() == obj_lower]
                if method_name:
                    meth_lower = method_name.lower()
                    result = [r for r in result if r.get("target_method", "").lower() == meth_lower]
                return result
            except sqlite3.OperationalError:
                return None

    @_transient_safe(lambda: None)
    def count_overrides_by_extension_root(self) -> dict[str, int] | None:
        """COUNT перехватов по корню расширения (index-side, дёшево).

        Возвращает {extension_root: count}. None если таблицы нет (pre-v9 индекс).
        Ключ — extension_root в той форме, как сохранил builder (ext.path /
        current.path); потребитель нормализует пути на своей стороне.
        """
        with self._lock:
            try:
                rows = self._conn.execute(
                    "SELECT extension_root, COUNT(*) AS n FROM extension_overrides GROUP BY extension_root"
                ).fetchall()
                return {r["extension_root"]: int(r["n"]) for r in rows}
            except sqlite3.OperationalError:
                return None

    @_transient_safe(dict)
    def get_overrides_for_path(self, rel_path: str) -> dict[str, list[dict]]:
        """Get all overrides for a module by source_path, grouped by target_method.

        Returns empty dict if table doesn't exist or no overrides found.
        """
        with self._lock:
            try:
                rows = self._conn.execute(
                    "SELECT * FROM extension_overrides WHERE source_path = ?",
                    (rel_path,),
                ).fetchall()
            except sqlite3.OperationalError:
                return {}
            result: dict[str, list[dict]] = {}
            for r in rows:
                d = dict(r)
                method = d.get("target_method", "")
                result.setdefault(method, []).append(d)
            return result

    def get_extension_overrides_grouped(
        self,
        base_path: str = "",
    ) -> dict[str, list[dict]] | None:
        """Get all overrides grouped by extension_root.

        For extension configs: if extension_root == base_path, key is "self".
        Returns None if table doesn't exist.
        """
        with self._lock:
            try:
                rows = self._conn.execute("SELECT * FROM extension_overrides").fetchall()
            except sqlite3.OperationalError:
                return None

            # Determine config role
            config_role = ""
            try:
                meta_row = self._conn.execute("SELECT value FROM index_meta WHERE key = 'config_role'").fetchone()
                if meta_row:
                    config_role = meta_row["value"]
            except sqlite3.Error:
                pass

            result: dict[str, list[dict]] = {}
            for r in rows:
                d = dict(r)
                ext_root = d.get("extension_root", "")
                if config_role == "extension" and base_path and ext_root == base_path:
                    key = "self"
                else:
                    key = ext_root
                result.setdefault(key, []).append(d)
            return result

    @_transient_safe(lambda: None)
    def get_form_elements(
        self,
        object_name: str = "",
        form_name: str = "",
        handler: str = "",
    ) -> list[dict] | None:
        """Get form elements (handlers, commands, attributes) as raw rows.

        Returns None if table doesn't exist (pre-v10 index).
        """
        with self._lock:
            try:
                conditions: list[str] = []
                params: list[str] = []
                if object_name:
                    conditions.append("object_name = ?")
                    params.append(object_name)
                if form_name:
                    conditions.append("form_name = ?")
                    params.append(form_name)
                if handler:
                    conditions.append("handler = ? COLLATE NOCASE")
                    params.append(handler)

                where = " WHERE " + " AND ".join(conditions) if conditions else ""
                rows = self._conn.execute(
                    f"SELECT * FROM form_elements{where}",  # noqa: S608
                    params,
                ).fetchall()
                return [dict(r) for r in rows]
            except sqlite3.OperationalError:
                return None

    @_transient_safe(lambda: None)
    def get_object_attributes(
        self,
        attr_name: str = "",
        object_name: str = "",
        category: str = "",
        kind: str = "",
        limit: int = 500,
    ) -> list[dict] | None:
        """Search indexed object attributes by name, object, category, or kind.

        Returns list of dicts or None if table missing (fallback allowed).
        """
        with self._lock:
            try:
                conditions: list[str] = []
                params: list[str | int] = []
                if attr_name:
                    conditions.append(
                        "(py_lower(attr_name) LIKE '%' || py_lower(?) || '%'"
                        " OR py_lower(attr_synonym) LIKE '%' || py_lower(?) || '%')"
                    )
                    params.extend([attr_name, attr_name])
                if object_name:
                    conditions.append("py_lower(object_name) LIKE '%' || py_lower(?) || '%'")
                    params.append(object_name)
                if category:
                    conditions.append("category = ?")
                    params.append(category)
                if kind:
                    conditions.append("attr_kind = ?")
                    params.append(kind.lower())

                where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
                sql = f"SELECT * FROM object_attributes{where} LIMIT ?"  # noqa: S608
                params.append(limit)
                rows = self._conn.execute(sql, params).fetchall()
            except sqlite3.OperationalError:
                return None

            results = []
            for r in rows:
                attr_type: list[str] = []
                try:
                    attr_type = json.loads(r["attr_type"]) if r["attr_type"] else []
                except (ValueError, TypeError):
                    pass
                results.append(
                    {
                        "object_name": r["object_name"],
                        "category": r["category"],
                        "attr_name": r["attr_name"],
                        "attr_synonym": r["attr_synonym"] or "",
                        "attr_type": attr_type,
                        "attr_kind": r["attr_kind"],
                        "ts_name": r["ts_name"],
                        "source_file": r["source_file"],
                    }
                )
            return results

    @_transient_safe(lambda: None)
    def get_predefined_items(
        self,
        item_name: str = "",
        object_name: str = "",
        limit: int = 500,
    ) -> list[dict] | None:
        """Search indexed predefined items by name or parent object.

        Returns list of dicts or None if table missing (fallback allowed).
        """
        with self._lock:
            try:
                conditions: list[str] = []
                params: list[str | int] = []
                if item_name:
                    conditions.append(
                        "(py_lower(item_name) LIKE '%' || py_lower(?) || '%'"
                        " OR py_lower(item_synonym) LIKE '%' || py_lower(?) || '%')"
                    )
                    params.extend([item_name, item_name])
                if object_name:
                    conditions.append("py_lower(object_name) LIKE '%' || py_lower(?) || '%'")
                    params.append(object_name)

                where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
                sql = f"SELECT * FROM predefined_items{where} LIMIT ?"  # noqa: S608
                params.append(limit)
                rows = self._conn.execute(sql, params).fetchall()
            except sqlite3.OperationalError:
                return None

            results = []
            for r in rows:
                types: list[str] = []
                try:
                    types = json.loads(r["types_json"]) if r["types_json"] else []
                except (ValueError, TypeError):
                    pass
                results.append(
                    {
                        "object_name": r["object_name"],
                        "category": r["category"],
                        "item_name": r["item_name"],
                        "item_synonym": r["item_synonym"] or "",
                        "item_code": r["item_code"] or "",
                        "types": types,
                        "is_folder": bool(r["is_folder"]),
                        "source_file": r["source_file"],
                    }
                )
            return results

    # Priority for ORDER BY (matches _REF_KIND_PRIORITY in bsl_helpers.py).
    # Lower number = higher priority = surfaced first when results are truncated.
    _REF_KIND_SQL_PRIORITY: dict[str, int] = {
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

    @_transient_safe(lambda: None)
    def find_metadata_references(
        self,
        ref_object: str,
        kinds: list[str] | None = None,
        limit: int = 1000,
    ) -> list[dict] | None:
        """Find all references to a metadata object via metadata_references table.

        Rows are ORDER-ed by ref_kind priority (CASE WHEN), then path, then used_in,
        so SQL-side LIMIT preserves the highest-priority kinds even if total > limit.

        Args:
            ref_object: canonical reference (e.g. "Catalog.Контрагенты"). Case-insensitive.
            kinds: optional filter by ref_kind values; None = all.
            limit: maximum rows to fetch (full count returned via count_metadata_references).

        Returns:
            list of dicts with keys: source_object, source_category, ref_object,
            ref_kind, used_in, path, line. Or None if metadata_references table
            is missing (caller should fall back to live scan).
        """
        # Build CASE WHEN expression for kind-priority ORDER BY (must be safe-list)
        case_clauses = " ".join(f"WHEN '{k}' THEN {p}" for k, p in self._REF_KIND_SQL_PRIORITY.items())
        priority_expr = f"(CASE ref_kind {case_clauses} ELSE 99 END)"

        with self._lock:
            try:
                conditions = ["py_lower(ref_object) = py_lower(?)"]
                params: list = [ref_object]
                if kinds:
                    placeholders = ",".join("?" for _ in kinds)
                    conditions.append(f"ref_kind IN ({placeholders})")
                    params.extend(kinds)
                where = " AND ".join(conditions)
                sql = (
                    "SELECT source_object, source_category, ref_object, ref_kind, "
                    "used_in, path, line FROM metadata_references "
                    f"WHERE {where} "  # noqa: S608
                    f"ORDER BY {priority_expr}, path, used_in "
                    "LIMIT ?"
                )
                params.append(limit)
                rows = self._conn.execute(sql, params).fetchall()
            except sqlite3.OperationalError:
                return None
            return [
                {
                    "source_object": r["source_object"],
                    "source_category": r["source_category"],
                    "ref_object": r["ref_object"],
                    "ref_kind": r["ref_kind"],
                    "used_in": r["used_in"],
                    "path": r["path"],
                    "line": r["line"],
                }
                for r in rows
            ]

    @_transient_safe(lambda: None)
    def find_metadata_refs_from(
        self,
        source_object: str,
        source_category: str | None = None,
        kinds: list[str] | None = None,
        limit: int = 1000,
    ) -> list[dict] | None:
        """OUTBOUND metadata references FROM ``source_object`` (v1.19.0).

        The reverse direction of ``find_metadata_references``: "which objects does
        ``source_object`` reference?" (its attribute types, owner, based-on, …).
        Mirrors that method's pattern, INCLUDING its perf profile — ``source_object``
        is stored **bare** (e.g. ``"РеализацияТоваровУслуг"``) and matched via
        ``py_lower(...)``, which is a SCAN, not an ``idx_mref_src`` point-lookup
        (a UDF expression index would need a ``BUILDER_VERSION`` bump — forbidden).
        ``metadata_references`` is object-level (orders of magnitude smaller than
        ``calls``), so the scan is cheap; ``find_data_path`` additionally caps node
        expansion. ``source_category`` (e.g. ``"Documents"``) disambiguates a bare
        name shared by two categories (``Catalog.X`` vs ``Document.X``) and narrows
        the scan via ``idx_mref_category``.

        ``ref_object`` is returned in CANONICAL form (e.g. ``"Catalog.Контрагенты"``).
        Returns None if the table is missing (pre-v9 index) → caller may fall back.
        """
        with self._lock:
            try:
                conditions = ["py_lower(source_object) = py_lower(?)"]
                params: list = [source_object]
                if source_category:
                    conditions.append("source_category = ?")
                    params.append(source_category)
                if kinds:
                    placeholders = ",".join("?" for _ in kinds)
                    conditions.append(f"ref_kind IN ({placeholders})")
                    params.extend(kinds)
                where = " AND ".join(conditions)
                sql = (
                    "SELECT source_object, source_category, ref_object, ref_kind, "
                    "used_in, path, line FROM metadata_references "
                    f"WHERE {where} "  # noqa: S608
                    "LIMIT ?"
                )
                params.append(limit)
                rows = self._conn.execute(sql, params).fetchall()
            except sqlite3.OperationalError:
                return None
            return [
                {
                    "source_object": r["source_object"],
                    "source_category": r["source_category"],
                    "ref_object": r["ref_object"],
                    "ref_kind": r["ref_kind"],
                    "used_in": r["used_in"],
                    "path": r["path"],
                    "line": r["line"],
                }
                for r in rows
            ]

    @_transient_safe(lambda: None)
    def count_metadata_references(
        self,
        ref_object: str,
        kinds: list[str] | None = None,
    ) -> dict | None:
        """Return {'total': int, 'by_kind': {kind: count}} or None if table missing."""
        with self._lock:
            try:
                conditions = ["py_lower(ref_object) = py_lower(?)"]
                params: list = [ref_object]
                if kinds:
                    placeholders = ",".join("?" for _ in kinds)
                    conditions.append(f"ref_kind IN ({placeholders})")
                    params.extend(kinds)
                where = " AND ".join(conditions)
                rows = self._conn.execute(
                    f"SELECT ref_kind, COUNT(*) AS cnt FROM metadata_references "  # noqa: S608
                    f"WHERE {where} GROUP BY ref_kind",
                    params,
                ).fetchall()
            except sqlite3.OperationalError:
                return None
            by_kind = {r["ref_kind"]: r["cnt"] for r in rows}
            return {"total": sum(by_kind.values()), "by_kind": by_kind}

    @_transient_safe(lambda: None)
    def find_code_usages(
        self,
        object_ref: str,
        kind: str | None = None,
        limit: int = 1000,
    ) -> list[dict] | None:
        """Find reverse code-usages of a metadata object via metadata_code_usages.

        Indexed exact-match on ``object_ref_key`` (= ``object_ref.lower()``) — a
        plain, Cyrillic-aware case-insensitive lookup without the py_lower UDF.

        Args:
            object_ref: canonical reference (e.g. "Document.ПриобретениеТоваровУслуг").
            kind: optional filter — 'manager' | 'ref_type' | 'query'.
            limit: maximum rows to fetch.

        Returns:
            list of dicts {path, object_name, category, module_type, line, kind, member}
            (JOIN to modules), or None if the metadata_code_usages table is missing
            (caller should fall back / report a stale index).
        """
        with self._lock:
            try:
                conditions = ["u.object_ref_key = ?"]
                params: list = [object_ref.lower()]
                if kind:
                    conditions.append("u.usage_kind = ?")
                    params.append(kind)
                where = " AND ".join(conditions)
                sql = (
                    "SELECT mod.rel_path AS path, mod.object_name AS object_name, "
                    "mod.category AS category, mod.module_type AS module_type, "
                    "u.line AS line, u.usage_kind AS kind, u.member_path AS member "
                    "FROM metadata_code_usages u "
                    "JOIN modules mod ON mod.id = u.module_id "
                    f"WHERE {where} "  # noqa: S608
                    "ORDER BY mod.rel_path, u.line "
                    "LIMIT ?"
                )
                params.append(limit)
                rows = self._conn.execute(sql, params).fetchall()
            except sqlite3.OperationalError:
                return None
            return [
                {
                    "path": r["path"],
                    "object_name": r["object_name"],
                    "category": r["category"],
                    "module_type": r["module_type"],
                    "line": r["line"],
                    "kind": r["kind"],
                    "member": r["member"],
                }
                for r in rows
            ]

    @_transient_safe(lambda: None)
    def count_code_usages(
        self,
        object_ref: str,
        kind: str | None = None,
    ) -> dict | None:
        """Return {'total': int, 'by_kind': {kind: count}} or None if table missing."""
        with self._lock:
            try:
                conditions = ["object_ref_key = ?"]
                params: list = [object_ref.lower()]
                if kind:
                    conditions.append("usage_kind = ?")
                    params.append(kind)
                where = " AND ".join(conditions)
                rows = self._conn.execute(
                    f"SELECT usage_kind, COUNT(*) AS cnt FROM metadata_code_usages "  # noqa: S608
                    f"WHERE {where} GROUP BY usage_kind",
                    params,
                ).fetchall()
            except sqlite3.OperationalError:
                return None
            by_kind = {r["usage_kind"]: r["cnt"] for r in rows}
            return {"total": sum(by_kind.values()), "by_kind": by_kind}

    @_transient_safe(lambda: None)
    def find_defined_type(self, name: str) -> dict | None:
        """Look up a DefinedType by name. Returns {name, types: list[str], path}
        or None if table missing or not found."""
        with self._lock:
            try:
                row = self._conn.execute(
                    "SELECT name, type_refs_json, path FROM defined_types WHERE py_lower(name) = py_lower(?)",
                    (name,),
                ).fetchone()
            except sqlite3.OperationalError:
                return None
            if row is None:
                return None
            try:
                types = json.loads(row["type_refs_json"])
            except (ValueError, TypeError):
                types = []
            return {"name": row["name"], "types": types, "path": row["path"]}

    def close(self) -> None:
        """Close the database connection."""
        with self._lock:
            self._conn.close()
