from __future__ import annotations

import ast
import io
import contextlib
import builtins
import difflib
import functools
import pathlib
import re
import signal
import threading
import time as _time
import traceback
from contextlib import contextmanager
from dataclasses import dataclass

from rlm_tools_bsl.helpers import make_helpers
from rlm_tools_bsl.bsl_helpers import make_bsl_helpers


ALLOWED_MODULES = frozenset(
    {
        "re",
        "json",
        "collections",
        "math",
        "fnmatch",
        "itertools",
        "functools",
        "string",
        "textwrap",
        "difflib",
        "statistics",
    }
)

BLOCKED_BUILTINS = frozenset(
    {
        "exec",
        "eval",
        "compile",
        "__import__",
        "breakpoint",
        "exit",
        "quit",
        "input",
        # site._Printer объекты — читают файлы реальным open() в обход restricted_open
        # (вектор license._Printer__filenames=[...]; license()).
        "license",
        "credits",
        "copyright",
    }
)

# Опасные dunder — путь к классам/globals/коду (escape-гаджеты). НЕ блокируем
# безопасные __name__/__doc__/__len__/… (легит. интроспекция, e.g. type(x).__name__).
_BLOCKED_DUNDERS = frozenset(
    {
        "__class__",
        "__bases__",
        "__base__",
        "__mro__",
        "__subclasses__",
        "__subclasshook__",
        "__init_subclass__",
        "__class_getitem__",
        "__globals__",
        "__builtins__",
        "__import__",
        "__dict__",
        "__getattribute__",
        "__getattr__",
        "__setattr__",
        "__delattr__",
        "__init__",
        "__new__",
        "__code__",
        "__closure__",
        "__func__",
        "__self__",
        "__wrapped__",
        "__reduce__",
        "__reduce_ex__",
        "__traceback__",
    }
)
# Не-dunder интроспекция фрейма/кода/traceback генераторов и корутин.
_BLOCKED_ATTRS = frozenset(
    {
        "gi_frame",
        "gi_code",
        "cr_frame",
        "cr_code",
        "ag_frame",
        "ag_code",
        "f_globals",
        "f_builtins",
        "f_locals",
        "f_back",
        "f_code",
        "f_trace",
        "tb_frame",
        "tb_next",
    }
)
_BLOCKED_ACCESS = _BLOCKED_DUNDERS | _BLOCKED_ATTRS


# Частые «угаданные» имена хелперов → реальные (из e2e-логов бенчмарков).
_KNOWN_HELPER_ALIASES: dict[str, str] = {
    "get_method_source": "read_procedure",
    "get_module_source": "read_procedure",
    "read_method": "read_procedure",
    "get_procedure": "read_procedure",
    "parse_metadata_xml": "parse_object_xml",
    "get_object_structure": "get_object_full_structure",
    "find_subscriptions": "find_event_subscriptions",
    "get_callers": "find_callers_context",
}

# Сигнатуры generic-IO хелперов (НЕ в BSL _registry — приходят из make_helpers()).
# Нужны, чтобы kwarg-хинт давал реальную сигнатуру и для grep/read_file/… (в логах
# бенчмарка: grep(..., limit=...) → тупик). Зеркалит server.available_functions.
_GENERIC_HELPER_SIGNATURES: dict[str, dict] = {
    "read_file": {"sig": "read_file(path) -> str"},
    "read_files": {"sig": "read_files(paths) -> dict[path, str]"},
    "grep": {"sig": "grep(pattern, path='.') -> list[dict] keys: file, line, text"},
    "grep_summary": {"sig": "grep_summary(pattern, path='.') -> str"},
    "grep_read": {"sig": "grep_read(pattern, path='.', max_files=10, context_lines=0) -> {matches, files, summary}"},
    "glob_files": {"sig": "glob_files(pattern) -> list[str]"},
    "tree": {"sig": "tree(path='.', max_depth=3) -> str"},
    "find_files": {"sig": "find_files(name) -> list[str]"},
}


@dataclass
class HelperCall:
    name: str
    elapsed: float
    seq: int = 0
    duplicate_of: int | None = None


@dataclass
class ExecutionResult:
    stdout: str
    error: str | None
    variables: list[str]
    helper_calls: list[HelperCall] | None = None
    efficiency_hints: list[dict] | None = None


def _arg_fingerprint(args, kwargs) -> str | None:
    """Best-effort identity fingerprint of a helper call: the first non-empty STRING
    positional arg (object name / path), else the first string kwarg value, normalized
    (stripped + lower). Non-string / unreadable args → ``None`` (skip). Used only by the
    session efficiency-nudge aggregator — never affects helper behaviour or return value."""
    for a in args:
        if isinstance(a, str) and a.strip():
            return a.strip().lower()
    for v in kwargs.values():
        if isinstance(v, str) and v.strip():
            return v.strip().lower()
    return None


def _make_restricted_import(allowed: frozenset[str]):
    original_import = builtins.__import__

    def restricted_import(name, *args, **kwargs):
        if name not in allowed and name.split(".")[0] not in allowed:
            raise ImportError(f"Import of '{name}' is not allowed in the sandbox")
        return original_import(name, *args, **kwargs)

    return restricted_import


class Sandbox:
    def __init__(
        self,
        base_path: str,
        max_output_chars: int = 15_000,
        execution_timeout_seconds: int = 45,
        format_info=None,
        idx_reader=None,
        idx_zero_callers_authoritative: bool = False,
        extension_paths: list[str] | None = None,
    ):
        self._base_path = base_path
        self._max_output_chars = max_output_chars
        self._execution_timeout_seconds = execution_timeout_seconds
        self._format_info = format_info
        self._idx_reader = idx_reader
        self._idx_zero_callers_authoritative = idx_zero_callers_authoritative
        self._extension_paths = list(extension_paths or [])
        self._namespace: dict = {}
        self._resolve_safe = None
        self._helper_calls: list[HelperCall] = []
        # Session-wide state for duplicate-call detection. NOT cleared in execute().
        self._session_call_count: int = 0
        self._session_call_signatures: dict[str, int] = {}
        # Session-wide efficiency-nudge aggregators (strictly instance-local — never a
        # module singleton, else hints would leak across projects/sessions). NOT cleared
        # in execute(). `*_arg_counts` keys on (helper_name, arg_fingerprint).
        self._session_helper_name_counts: dict[str, int] = {}
        self._session_helper_arg_counts: dict[tuple[str, str], int] = {}
        self._emitted_efficiency_hints: set[str] = set()
        # v1.24.0 #5 — batch nudge granularity is rlm_execute ROUND-TRIPS, not raw
        # invocations: an execute with only 1-2 top-level helper calls is "sparse".
        # Accumulated across the session (NOT cleared in execute(), unlike _helper_calls).
        self._session_sparse_execute_count: int = 0
        self._setup_namespace()

    def _setup_namespace(self) -> None:
        safe_builtins = {k: v for k, v in builtins.__dict__.items() if k not in BLOCKED_BUILTINS}
        safe_builtins["__import__"] = _make_restricted_import(ALLOWED_MODULES)

        original_open = builtins.open

        def restricted_open(file, mode="r", *args, **kwargs):
            if any(c in mode for c in "wxa+"):
                raise PermissionError(f"Write access denied in sandbox (mode='{mode}')")

            if self._resolve_safe is None:
                raise RuntimeError("Sandbox path resolver was not initialized")

            if isinstance(file, int):
                raise PermissionError("File descriptor access is not allowed in sandbox")

            # Keep read access scoped to the sandbox root.
            safe_path = self._resolve_safe(str(pathlib.Path(file)))
            return original_open(safe_path, mode, *args, **kwargs)

        safe_builtins["open"] = restricted_open

        # Best-effort read-only hardening: убрать интроспекцию, дающую путь к real-
        # builtins/классам в обход AST-гейта; запретить запись атрибутов.
        for _n in ("vars", "globals", "locals", "setattr", "delattr"):
            safe_builtins.pop(_n, None)

        _real_getattr = safe_builtins.get("getattr")

        def restricted_getattr(obj, name, *default):
            if isinstance(name, str) and name in _BLOCKED_ACCESS:
                raise AttributeError(f"доступ к атрибуту '{name}' запрещён в песочнице")
            return _real_getattr(obj, name, *default)

        safe_builtins["getattr"] = restricted_getattr

        self._namespace["__builtins__"] = safe_builtins

        helpers, self._resolve_safe = make_helpers(self._base_path, idx_reader=self._idx_reader)
        self._namespace.update(self._wrap_helpers(helpers))

        if self._format_info is not None:
            bsl_helpers = make_bsl_helpers(
                base_path=self._base_path,
                resolve_safe=self._resolve_safe,
                read_file_fn=helpers["read_file"],
                grep_fn=helpers["grep"],
                glob_files_fn=helpers["glob_files"],
                format_info=self._format_info,
                idx_reader=self._idx_reader,
                idx_zero_callers_authoritative=self._idx_zero_callers_authoritative,
                extension_paths=self._extension_paths,
            )
            self._namespace.update(self._wrap_helpers(bsl_helpers))

            # --- Agent-facing line numbering (presentation layer) ---
            from rlm_tools_bsl._format import number_lines

            _raw_rf = helpers["read_file"]

            def _numbered_read_file(path: str) -> str:
                return number_lines(_raw_rf(path))

            def _numbered_read_files(paths: list[str]) -> dict[str, str]:
                result = {}
                for path in paths:
                    try:
                        result[path] = number_lines(_raw_rf(path))
                    except (OSError, PermissionError) as e:
                        result[path] = f"[error: {e}]"
                return result

            _raw_grep_read = helpers["grep_read"]

            def _numbered_grep_read(pattern, path=".", max_files=10, context_lines=0):
                result = _raw_grep_read(pattern, path, max_files, context_lines)
                if context_lines == 0:
                    for fp in list(result.get("files", {})):
                        content = result["files"][fp]
                        if not content.startswith("[error:"):
                            result["files"][fp] = number_lines(content)
                return result

            _raw_read_procedure = bsl_helpers.get("read_procedure")

            def _numbered_read_procedure(path, proc_name, include_overrides=False):
                return _raw_read_procedure(path, proc_name, include_overrides, numbered=True)

            numbered_overrides = [
                ("read_file", _numbered_read_file),
                ("read_files", _numbered_read_files),
                ("grep_read", _numbered_grep_read),
            ]
            if _raw_read_procedure is not None:
                numbered_overrides.append(("read_procedure", _numbered_read_procedure))
            for name, fn in numbered_overrides:
                self._namespace[name] = self._wrap_helpers({name: fn})[name]

    def _wrap_helpers(self, helpers: dict) -> dict:
        """Wrap callable helpers with timing + session-wide duplicate-call detection."""
        wrapped = {}
        for name, obj in helpers.items():
            if callable(obj):

                @functools.wraps(obj)
                def _timed(*args, _fn=obj, _name=name, **kwargs):
                    t0 = _time.monotonic()
                    self._session_call_count += 1
                    seq = self._session_call_count
                    duplicate_of: int | None = None
                    sig_key: str | None = None
                    try:
                        sig_key = repr((_name, args, sorted(kwargs.items())))
                    except Exception:
                        sig_key = None
                    if sig_key is not None:
                        prev = self._session_call_signatures.get(sig_key)
                        if prev is not None:
                            duplicate_of = prev
                        else:
                            self._session_call_signatures[sig_key] = seq
                    # Session-level efficiency aggregator (name count + arg fingerprint).
                    # Composite helpers call inner closures directly (bypassing this
                    # wrapper), so only TOP-LEVEL agent calls are counted here.
                    self._session_helper_name_counts[_name] = self._session_helper_name_counts.get(_name, 0) + 1
                    _fp = _arg_fingerprint(args, kwargs)
                    if _fp is not None:
                        _akey = (_name, _fp)
                        self._session_helper_arg_counts[_akey] = self._session_helper_arg_counts.get(_akey, 0) + 1
                    try:
                        return _fn(*args, **kwargs)
                    finally:
                        self._helper_calls.append(
                            HelperCall(
                                _name,
                                _time.monotonic() - t0,
                                seq=seq,
                                duplicate_of=duplicate_of,
                            )
                        )

                wrapped[name] = _timed
            else:
                wrapped[name] = obj
        return wrapped

    # Batch/aggregate helpers — using ANY of them means the agent is already batching,
    # which suppresses the generic "batch more" nudge.
    _AGGREGATE_HELPERS = frozenset(
        {"read_files", "get_object_profile", "get_object_modules", "get_object_full_structure"}
    )

    # v1.24.0 #5 — an execute with 1..THRESHOLD top-level helper calls is "sparse"
    # (poor batching); after NUDGE such sparse round-trips, suggest batching.
    _SPARSE_CALLS_THRESHOLD = 2
    _SPARSE_EXECUTES_TO_NUDGE = 8

    def _compute_efficiency_hints(self) -> list[dict]:
        """Session-cumulative efficiency nudges, throttled to ONE per id per session.

        Lives in the ``rlm_execute`` response metadata (never the helper return / stdout —
        see ``test_sandbox_helper_return_value_unchanged``). Each hint:
        ``{id, message, trigger, helper?, count?}`` with stable ids
        ``read_files`` / ``reuse_var`` / ``batch`` / ``redundant_get_index_info``."""
        out: list[dict] = []
        nc = self._session_helper_name_counts
        emitted = self._emitted_efficiency_hints

        # v1.24.0 #5 — batch nudge granularity: count THIS execute's top-level helper
        # calls. _helper_calls is cleared at the start of execute() and appended only
        # for top-level invocations (composite helpers bypass _timed), so its length
        # here == top-level calls in this execute (error-execute included: calls made
        # before the exception are already recorded). 1..THRESHOLD = sparse round-trip.
        calls_this_execute = len(self._helper_calls)
        if 1 <= calls_this_execute <= self._SPARSE_CALLS_THRESHOLD:
            self._session_sparse_execute_count += 1

        # (1) Non-batched homogeneous reads → read_files([...]) / read_procedure(path, [...]).
        rf = nc.get("read_file", 0)
        rp = nc.get("read_procedure", 0)
        if (rf >= 3 or rp >= 3) and "read_files" not in emitted:
            emitted.add("read_files")
            helper = "read_file" if rf >= rp else "read_procedure"
            out.append(
                {
                    "id": "read_files",
                    "helper": helper,
                    "count": max(rf, rp),
                    "trigger": f"{helper} x{max(rf, rp)} single calls in session",
                    "message": (
                        "Несколько одиночных чтений за сессию — батчи: read_files([...]) одним вызовом "
                        "вместо N×read_file; read_procedure(path, ['Проц1','Проц2']) списком вместо N вызовов."
                    ),
                }
            )

        # (3) Re-resolve: find_module / extract_procedures repeated on the SAME object.
        if "reuse_var" not in emitted:
            for h in ("find_module", "extract_procedures"):
                rep = max((c for (n, _fp), c in self._session_helper_arg_counts.items() if n == h), default=0)
                if rep >= 2:
                    emitted.add("reuse_var")
                    out.append(
                        {
                            "id": "reuse_var",
                            "helper": h,
                            "count": rep,
                            "trigger": f"{h} repeated on same arg x{rep}",
                            "message": (
                                "Повторный резолв того же объекта — сохрани результат в переменную "
                                "(переменные живут между rlm_execute) или возьми get_object_profile(name) за 1 вызов."
                            ),
                        }
                    )
                    break

        # (2) Many SPARSE rlm_execute round-trips (1-2 calls each) and no aggregate
        # helper used → batch more. Granularity is round-trips, not raw invocations,
        # so a single dense execute (e.g. 20 calls) is NOT penalised (v1.24.0 #5).
        sparse = self._session_sparse_execute_count
        used_aggregate = any(nc.get(h, 0) for h in self._AGGREGATE_HELPERS)
        if sparse >= self._SPARSE_EXECUTES_TO_NUDGE and not used_aggregate and "batch" not in emitted:
            emitted.add("batch")
            out.append(
                {
                    "id": "batch",
                    "helper": None,
                    "count": sparse,
                    "trigger": f"{sparse} sparse rlm_execute round-trips (1-2 calls each), no aggregate helper used",
                    "message": (
                        "Много отдельных rlm_execute с 1-2 вызовами — батчи 3–5 связанных операций в один "
                        "rlm_execute; для обзора объекта бери get_object_profile(name) (≈10 вызовов в 1)."
                    ),
                }
            )

        # (4) get_index_info() at session start — its payload (builder_version/has_*/counts)
        # already ships in rlm_start.index, so a discovery call on start wastes a round-trip.
        # Gate on the MINIMAL seq of get_index_info in THIS execute: seq<=2 = 1st/2nd call of
        # the session (start signal). A mid-session call for niche fields (has_regions/
        # has_module_headers/extension_overrides) has a high seq → no false nudge. Throttled once.
        if "redundant_get_index_info" not in emitted:
            gii_seqs = [c.seq for c in self._helper_calls if c.name == "get_index_info"]
            if gii_seqs and min(gii_seqs) <= 2:
                emitted.add("redundant_get_index_info")
                out.append(
                    {
                        "id": "redundant_get_index_info",
                        "helper": "get_index_info",
                        "count": len(gii_seqs),
                        "trigger": f"get_index_info() as session call #{min(gii_seqs)} (payload already in rlm_start.index)",
                        "message": (
                            "builder_version/has_*/counts уже пришли в rlm_start.index (поле index) — "
                            "не трать execute на get_index_info() на старте; отдельный вызов нужен лишь "
                            "для has_regions/has_module_headers/extension_overrides."
                        ),
                    }
                )
        return out

    @contextmanager
    def _execution_timeout(self):
        if self._execution_timeout_seconds <= 0:
            yield
            return

        if threading.current_thread() is threading.main_thread() and hasattr(signal, "SIGALRM"):
            # Unix: signal-based timeout (precise, interrupts C extensions)
            def _raise_timeout(_signum, _frame):
                raise TimeoutError(f"Execution timed out after {self._execution_timeout_seconds} seconds")

            previous_handler = signal.getsignal(signal.SIGALRM)
            signal.signal(signal.SIGALRM, _raise_timeout)
            signal.setitimer(signal.ITIMER_REAL, self._execution_timeout_seconds)
            try:
                yield
            finally:
                signal.setitimer(signal.ITIMER_REAL, 0)
                signal.signal(signal.SIGALRM, previous_handler)
        else:
            # Windows / non-main thread: threading-based timeout
            # Sets a flag that we check — cannot interrupt blocking I/O,
            # but catches long-running Python loops.
            import ctypes

            timed_out = threading.Event()
            target_tid = threading.current_thread().ident

            def _timeout_watchdog():
                timed_out.set()
                if target_tid is not None:
                    ctypes.pythonapi.PyThreadState_SetAsyncExc(
                        ctypes.c_ulong(target_tid),
                        ctypes.py_object(TimeoutError),
                    )

            timer = threading.Timer(self._execution_timeout_seconds, _timeout_watchdog)
            timer.daemon = True
            timer.start()
            try:
                yield
            finally:
                timer.cancel()
                if timed_out.is_set():
                    raise TimeoutError(f"Execution timed out after {self._execution_timeout_seconds} seconds")

    @staticmethod
    def _validate_readonly(code: str) -> str | None:
        """Базовая защита песочницы (AST-гейт) от основных инжекций: блокирует (а) доступ к ОПАСНЫМ dunder,
        frame-интроспекции и dunder-subscript (escape-гаджеты ().__class__...__subclasses__(),
        vars()[...], фреймы генераторов); (б) ПРИСВАИВАНИЕ/УДАЛЕНИЕ атрибутов (obj.attr=… —
        мутация объектов, напр. license._Printer__filenames). Безопасные dunder-ЧТЕНИЯ
        (__name__/__doc__/…) и subscript-присваивание (d['k']=v) разрешены. НЕ полная
        граница. SyntaxError пропускаем (exec поднимет на штатном пути)."""
        try:
            tree = ast.parse(code)
        except SyntaxError:
            return None
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                if isinstance(node.ctx, (ast.Store, ast.Del)):
                    return f"присваивание/удаление атрибута '.{node.attr}' запрещено в песочнице"
                if node.attr in _BLOCKED_ACCESS:
                    return f"доступ к атрибуту '.{node.attr}' запрещён в песочнице"
            elif isinstance(node, ast.Name):
                if node.id in _BLOCKED_DUNDERS:
                    return f"доступ к имени '{node.id}' запрещён в песочнице"
            elif isinstance(node, ast.Subscript):
                key = node.slice
                if isinstance(key, ast.Constant) and isinstance(key.value, str) and key.value in _BLOCKED_DUNDERS:
                    return f"subscript по ключу '{key.value}' запрещён в песочнице"
        return None

    def execute(self, code: str) -> ExecutionResult:
        self._helper_calls.clear()
        blocked = self._validate_readonly(code)
        if blocked is not None:
            return ExecutionResult(
                stdout="",
                error=f"SecurityError: {blocked}",
                variables=self.list_variables(),
                helper_calls=[],
                efficiency_hints=None,
            )
        stdout_capture = io.StringIO()
        error = None

        try:
            with contextlib.redirect_stdout(stdout_capture):
                with self._execution_timeout():
                    exec(code, self._namespace)
        except Exception:
            error = traceback.format_exc()
            # generic-IO хелперы (grep/read_file/…) не входят в BSL _registry —
            # домешиваем их сигнатуры, иначе kwarg-хинт для grep(..., limit=...) пуст.
            reg = {**_GENERIC_HELPER_SIGNATURES, **(self._namespace.get("_registry") or {})}
            available = {k for k in self._namespace if not k.startswith("_") and callable(self._namespace.get(k))}
            error = self._add_error_hints(error, code, available_names=available, registry=reg)

        stdout = stdout_capture.getvalue()
        if len(stdout) > self._max_output_chars:
            stdout = stdout[: self._max_output_chars] + "\n... [output truncated]"

        return ExecutionResult(
            stdout=stdout,
            error=error,
            variables=self.list_variables(),
            helper_calls=list(self._helper_calls),
            efficiency_hints=self._compute_efficiency_hints() or None,
        )

    @staticmethod
    def _add_error_hints(
        error: str,
        code: str,
        available_names: set[str] | None = None,
        registry: dict | None = None,
    ) -> str:
        """Append actionable hints to common errors."""
        hints: list[str] = []

        if "FileNotFoundError" in error or "No such file" in error:
            if "parse_object_xml" in code:
                hints.append(
                    "HINT: parse_object_xml auto-resolves directory paths and 'fake' .mdo/.xml paths. "
                    "Call parse_object_xml('Documents/Name') — it tries Documents/Name/Name.mdo (EDT) "
                    "and Documents/Name/Ext/Document.xml (CF) automatically."
                )
            if "read_procedure" in code:
                hints.append(
                    "HINT: read_procedure raised FileNotFoundError. Possible causes: "
                    "(1) wrong path — use find_module(name) to discover modules; "
                    "(2) object has only XML metadata (e.g. КОДСобытия, ДействияСобытия) — no BSL file. "
                    "If the path is correct but the procedure name might be wrong, call "
                    "extract_procedures(path) for the actual list (with case)."
                )
            if "parse_object_xml" not in code and "read_procedure" not in code and (".xml" in code or ".bsl" in code):
                hints.append(
                    "HINT: Use find_module('Name') or glob_files('**/pattern') to discover correct file paths first."
                )

        if "TimeoutError" in error:
            hints.append(
                "HINT: Operation timed out. For large configs, avoid composite helpers "
                "(analyze_document_flow, analyze_object) and call individual helpers instead: "
                "find_register_movements, find_event_subscriptions, find_callers_context."
            )

        if "NameError" in error:
            suggestion: str | None = None
            m = re.search(r"name '([^']+)' is not defined", error)
            if m:
                bad = m.group(1)
                alias = _KNOWN_HELPER_ALIASES.get(bad)
                if alias and (available_names is None or alias in available_names):
                    suggestion = alias
                elif available_names:
                    close = difflib.get_close_matches(bad, list(available_names), n=1, cutoff=0.72)
                    if close:
                        suggestion = close[0]
            if suggestion:
                hints.append(
                    f"HINT: '{m.group(1)}' не определён — нужен, скорее всего, '{suggestion}'. "
                    f"Сверь: help() или rlm_help(helpers=['{suggestion}']). Переменные сохраняются между вызовами."
                )
            else:
                hints.append(
                    "HINT: Call help() to see available functions. Variables persist between rlm_execute calls."
                )

        if "KeyError" in error and "get_object_full_structure" in code:
            bad_keys = ("'attr_name'", "'attr_synonym'", "'attr_type'", "'attr_kind'")
            if any(k in error for k in bad_keys):
                hints.append(
                    "HINT: get_object_full_structure отдаёт ключи name/synonym/type "
                    "(attr_name/attr_synonym/attr_type теперь принимаются как алиасы — это контракт "
                    "find_attributes). Если KeyError всё же возник — у структурных записей нет поля "
                    "attr_kind (алиаса для него нет). "
                    "Итерируй: for a in result['attributes']: print(a['name'], a['type']). "
                    "Для регистров — result['dimensions'] и result['resources']."
                )

        # v1.18.0 Фикс 2 follow-up: params теперь list[str] ИМЁН (не list[dict]).
        # Агент, ожидавший list[dict], пишет p['name'] / p.get('name') на строке-элементе
        # → TypeError "string indices must be integers" или AttributeError "'str' object
        # has no attribute …". Подсказываем форму контракта (зеркало attr_kind-хинта).
        params_str_misuse = "string indices must be integers" in error or (
            "AttributeError" in error and "'str' object has no attribute" in error
        )
        if params_str_misuse and "params" in code:
            hints.append(
                "HINT: поле params — это СПИСОК ИМЁН параметров (list[str], v1.18.0), а не "
                "list[dict]. Элемент params уже строка-имя: итерируй "
                "for name in m['params']: print(name). НЕ обращайся p['name'] / p.get('name') "
                "к элементу params (extract_procedures / find_exports / search_methods)."
            )

        # v1.19.0 — tolerant-contract hints for deterministic agent guesses (e2e).
        # Wrong kwarg name on a helper (observed: safe_grep(path=...) / safe_grep(hint=...);
        # the parameter is name_hint). Turn the dead-end TypeError into a correction.
        if "unexpected keyword argument" in error:
            mfn = re.search(r"(\w+)\(\) got an unexpected keyword argument '([^']+)'", error)
            fn = mfn.group(1) if mfn else None
            if "safe_grep" in code:
                hints.append(
                    "HINT: safe_grep(pattern, name_hint='', max_files=20) — второй параметр "
                    "называется name_hint (имя/фрагмент модуля для сужения), НЕ path и НЕ hint."
                )
            elif registry and fn and fn in registry and registry[fn].get("sig"):
                hints.append(
                    f"HINT: у {fn} нет такого параметра. Сигнатура: {registry[fn]['sig']}. "
                    "Лишние фильтры отбирай в Python по полям результата."
                )
            else:
                hints.append(
                    "HINT: неподдерживаемый именованный аргумент. Сверь сигнатуру через "
                    "help('имя_хелпера') / rlm_help(helpers=['имя_хелпера']); у хелпера может "
                    "не быть такого параметра-фильтра — отбирай поля вывода в Python."
                )

        # Slicing a dict like a list: d[:N] → KeyError: slice(None, N, None). Several
        # helpers return a dict, not a list.
        if "KeyError" in error and "slice(" in error:
            hints.append(
                "HINT: похоже, вы срезаете dict как список ([:N]). Ряд хелперов возвращают "
                "dict, а не list: analyze_document_flow → {event_subscriptions, "
                "register_movements, ...}; get_overrides → {overrides, total, truncated, source}; "
                "find_register_movements → {code_registers, erp_mechanisms, ...}; "
                "find_path/find_data_path → {found, path:[...], _meta}. "
                "Сначала возьми нужный СПИСОК по ключу (напр. res['path']), потом срезай. "
                "У find_path при многозначном имени без hint path=None, а ответ несёт "
                "{error, hint, candidates} — сначала проверь `if 'error' in res`."
            )

        # read_procedure returns the procedure BODY as a string, not a dict.
        if "AttributeError" in error and "'str' object has no attribute" in error and "read_procedure" in code:
            hints.append(
                "HINT: read_procedure(path, name) возвращает СТРОКУ (тело метода с номерами "
                "строк), не dict. Не вызывай .get()/[ключ] на результате — это уже текст."
            )

        # detect_extensions() returns a dict; agents recurrently treat it as a list
        # (iterate → str keys → .get/.attr; or [0] → KeyError). The list lives under
        # the 'nearby_extensions' key. (sig+recipe are correct — this is a reactive
        # nudge, not a contract fix.)
        if "detect_extensions" in code and (
            "'str' object has no attribute" in error or "KeyError: 0" in error or "list indices" in error
        ):
            hints.append(
                "HINT: detect_extensions() возвращает dict {config_role, config_name, "
                "config_prefix, warnings, nearby_extensions, nearby_main}, НЕ список. "
                "Расширения — это список ctx['nearby_extensions'] (каждый: {name, purpose, prefix, "
                "path, overrides_count}); роль — ctx['config_role']."
            )

        # v1.23.0 — three recurrent agent-shape errors (from A/B logs). Each caught retry
        # that the agent would otherwise spend a whole rlm_execute fixing = −1 call.
        # (a) .get()/.keys() on a list result (helper returned list[dict], not dict).
        if "AttributeError" in error and "'list' object has no attribute" in error:
            hints.append(
                "HINT: результат — СПИСОК (list[dict]), а не dict — не зови .get()/[ключ] на самом "
                "списке. Итерируй элементы: for r in result: print(r['name']). Списком отдают, напр., "
                "find_event_subscriptions (без limit), search_methods, extract_procedures, "
                "find_roles(...)['roles']."
            )
        # (b) set()/dict-key over list[dict] → unhashable.
        if "TypeError" in error and "unhashable type: 'dict'" in error:
            hints.append(
                "HINT: unhashable type 'dict' — нельзя положить dict в set() или в ключ dict. "
                "Элементы результата — словари: для дедупликации бери конкретное поле — "
                "{r['name'] for r in result} либо seen=set(); seen.add(r['name'])."
            )
        # (c) KeyError on a result-contract key not covered by the specific hints above.
        if "KeyError" in error and "slice(" not in error and "get_object_full_structure" not in code:
            mk = re.search(r"KeyError: '?([^'\n]+)'?", error)
            bad_key = mk.group(1) if mk else "<key>"
            hints.append(
                f"HINT: KeyError '{bad_key}' — форма результата иная, чем ожидалось. Многие хелперы "
                "возвращают dict с под-списками: find_register_movements → {code_registers, "
                "erp_mechanisms, manager_tables, adapted_registers}; get_object_profile → "
                "{object_name, category, sections:{...}, _meta}; get_overrides → {overrides, total, "
                "truncated, source}. Сверь ключи: print(list(result.keys())) или rlm_help(helpers=['имя'])."
            )

        if "import" in error.lower() and "restricted" in error.lower():
            hints.append(
                "HINT: Only standard library modules are allowed. Use built-in helpers instead of external libraries."
            )

        if hints:
            error = error.rstrip() + "\n\n" + "\n".join(hints)

        return error

    def list_variables(self) -> list[str]:
        return [k for k in self._namespace if not k.startswith("_") and k != "__builtins__"]
