"""Entry-point sandbox worker-процесса (spawn target).

Намеренно МАЛЕНЬКИЙ top-level модуль без импорта ``server.py``: Windows spawn
импортирует его в дочернем процессе, и он не должен тянуть MCP main или
циклические импорты (§5.1 плана). Тяжёлые модули (bsl_helpers/bsl_index)
импортируются лениво после отвязки stdio и применения resource limits.

Порядок запуска (§8.1): detach stdin/stdout/stderr → отдельная process
group/session + orphan-guard (Linux PDEATHSIG через стабильный spawn-broker;
остальной POSIX — getppid-watchdog) → RLIMIT_AS (POSIX) → recv init →
FormatInfo → собственный read-only IndexReader → Sandbox (timeout=0 — deadline
авторитетен только у родителя) → lazy LLM probe → init_ok → command loop.

stdout пользовательского кода уходит ТОЛЬКО в shared buffer (writer ниже);
``execute_result`` поле stdout не содержит (§6.3/§23.4).
"""

from __future__ import annotations

import io
import importlib.util
import os
import sys
import threading
import traceback

from rlm_tools_bsl._sandbox_protocol import (
    PROTOCOL_VERSION,
    WORKER_FATAL_REQUEST_ID,
    SandboxProtocolError,
    bounded_text,
    decode_frame,
    encode_frame,
    make_message,
    validate_message,
)


class SharedStdoutWriter(io.TextIOBase):
    """Пишет UTF-8 непосредственно в shared buffer, считая СИМВОЛЫ writer-side.

    Протокол публикации (§6.3): под writer-lock сначала копируются только
    целые UTF-8 символы, затем ПОСЛЕДНЕЙ операцией атомарно публикуется один
    aligned 32-bit ``published_bytes``. Parent после kill не берёт этот lock —
    он доверяет только опубликованному счётчику, поэтому битый UTF-8 или хвост
    чужого execute вернуться не могут; теряется максимум незакоммиченный хвост.
    Промежуточного StringIO нет — частичный вывод доступен родителю в момент
    принудительного убийства worker.
    """

    def __init__(self, buf, published_bytes, truncated_flag, lock, max_chars: int):
        self._buf = buf
        self._capacity = len(buf)
        self._published = published_bytes
        self._truncated = truncated_flag
        self._lock = lock
        self._max_chars = max_chars
        self._offset = 0
        self._used_chars = 0

    def reset(self) -> None:
        """Начало нового execute: parent уже обнулил header (§6.3)."""
        self._offset = 0
        self._used_chars = 0

    def writable(self) -> bool:  # pragma: no cover - io contract
        return True

    def write(self, s: str) -> int:
        if not isinstance(s, str):
            s = str(s)
        if not s:
            return 0
        remaining = self._max_chars - self._used_chars
        if remaining <= 0:
            self._truncated.value = 1
            return len(s)
        chunk = s[:remaining]
        clipped = len(chunk) < len(s)
        data = chunk.encode("utf-8", errors="replace")
        with self._lock:
            space = self._capacity - self._offset
            if len(data) > space:
                # Capacity = max_chars*4 + запас, поэтому ветка defensive:
                # срезаем по последней целой UTF-8 границе, которая помещается.
                while chunk and len(data) > space:
                    chunk = chunk[:-1]
                    data = chunk.encode("utf-8", errors="replace")
                clipped = True
            if data:
                self._buf[self._offset : self._offset + len(data)] = data
                self._offset += len(data)
                self._used_chars += len(chunk)
                # Публикация строго ПОСЛЕ копирования bytes — одна aligned запись.
                self._published.value = self._offset
            if clipped:
                self._truncated.value = 1
        return len(s)

    def flush(self) -> None:
        pass

    def getvalue(self) -> str:
        # Авторитетный stdout — shared buffer на стороне parent; worker в
        # execute_result stdout не передаёт.
        return ""

    @property
    def truncated(self) -> bool:
        # Sandbox добавил бы маркер к getvalue(); для shared writer маркер
        # добавляет ТОЛЬКО parent по shared-флагу, поэтому здесь всегда False.
        return False


def _detach_stdio() -> tuple[bool, str]:
    """Отвязать raw fd 0/1/2 от родителя ДО любых пользовательских команд.

    При MCP stdio родительский stdout — это протокол; escaped code не должен
    иметь возможности испортить framing даже raw os.write(1, ...) (§15).
    """
    errors: list[str] = []
    try:
        devnull_fd = os.open(os.devnull, os.O_RDWR)
    except OSError as exc:
        return False, f"open({os.devnull}) failed: {type(exc).__name__}: {exc}"
    try:
        for fd in (0, 1, 2):
            try:
                os.dup2(devnull_fd, fd)
            except OSError as exc:
                errors.append(f"dup2(fd={fd}) failed: {type(exc).__name__}: {exc}")
    finally:
        if devnull_fd > 2:
            try:
                os.close(devnull_fd)
            except OSError:
                pass
    try:
        sys.stdin = open(os.devnull, "r", encoding="utf-8")
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    except OSError:
        # Raw descriptors are the security boundary.  Existing wrappers already
        # point at those redirected descriptors, so reopening is best effort.
        pass
    if errors:
        return False, "; ".join(errors)
    return True, "raw fd 0/1/2 redirected to null"


def _isolate_process_group(expected_parent_pid: int) -> tuple[bool, str]:
    """POSIX: собственная session/process group (kill tree убивает и descendants);
    Linux: PDEATHSIG от стабильного parent spawn-broker (§10.2); остальной POSIX:
    best-effort getppid-watchdog. Windows — Job Object на стороне родителя.

    Возвращает ``(isolation_ready, detail)``. Провал ``setsid()`` или обязательного
    Linux orphan-guard НЕ глушится: заявленная граница отключилась бы молча.
    Родитель превращает это в controlled ошибку старта на обязательных gate-
    платформах — симметрично поведению Job Object на Windows (§23.9).
    """
    if os.name != "posix":
        return True, "windows: job object (parent-side)"
    if expected_parent_pid <= 0:
        return False, "expected parent PID is unavailable"
    try:
        os.setsid()
    except OSError as exc:
        return False, f"setsid failed: {type(exc).__name__}: {exc}"
    detail = "posix: own session/process group"
    if sys.platform.startswith("linux"):
        parent_guarded, guard_detail = _set_linux_parent_death_signal(expected_parent_pid)
        if not parent_guarded:
            return False, f"{detail}; {guard_detail}"
        return True, f"{detail} + {guard_detail}"

    # На macOS/прочем POSIX нет PR_SET_PDEATHSIG. Ожидаемый PID передан сервером,
    # а не захвачен уже после spawn: это закрывает стартовую гонку с init/subreaper.
    if os.getppid() != expected_parent_pid:
        os._exit(1)
    if _start_parent_death_watchdog(expected_parent_pid):
        detail += " + parent-death watchdog (best effort)"
    else:
        detail += " (parent-death watchdog unavailable)"
    return True, detail


def _set_linux_parent_death_signal(expected_parent_pid: int) -> tuple[bool, str]:
    """Kernel-level orphan-guard, привязанный к долгоживущему spawn-broker thread."""
    try:
        import ctypes
        import signal as _signal

        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
        libc.prctl.restype = ctypes.c_int
        ctypes.set_errno(0)
        rc = libc.prctl(1, _signal.SIGKILL, 0, 0, 0)  # PR_SET_PDEATHSIG = 1
        if rc != 0:
            err = ctypes.get_errno()
            suffix = f": {os.strerror(err)}" if err else ""
            return False, f"PDEATHSIG unavailable (prctl rc={rc}, errno={err}{suffix})"
    except Exception as exc:  # noqa: BLE001 — превращаем в controlled startup error
        return False, f"PDEATHSIG unavailable ({type(exc).__name__}: {exc})"

    # Сервер мог умереть до prctl. Сравнение с PID, захваченным в сервере ДО spawn,
    # работает и при reparent на non-PID1 subreaper, и когда сам сервер имеет PID 1.
    if os.getppid() != expected_parent_pid:
        os._exit(1)
    return True, "PDEATHSIG (stable spawn broker)"


def _start_parent_death_watchdog(expected_parent_pid: int, interval: float = 0.5) -> bool:
    """Best-effort orphan-guard для POSIX без Linux ``PR_SET_PDEATHSIG``.

    Отдельный Python-thread не блокируется command-loop ``recv`` и обычным Python-
    кодом, но не является hard-гарантией при native-коде, удерживающем GIL. Поэтому
    Linux использует kernel-level guard выше; этот fallback нужен для macOS и других
    best-effort платформ.
    """
    import time as _time

    def _watch() -> None:
        while True:
            try:
                _time.sleep(interval)
                if os.getppid() != expected_parent_pid:
                    os._exit(1)
            except Exception:
                os._exit(1)

    try:
        threading.Thread(target=_watch, name="rlm-parent-death-watchdog", daemon=True).start()
        return True
    except Exception:
        return False


def _apply_memory_limit(memory_mb: int) -> tuple[bool, str]:
    """POSIX RLIMIT_AS per-worker; Windows-лимит ставит родитель через Job Object.

    Возвращает ``(applied, detail)`` — провал не фатален, но обязан быть виден:
    иначе оператор считает, что потолок памяти стоит, а его нет (§11.2).
    """
    if memory_mb <= 0:
        return False, "disabled (memory_mb=0)"
    if os.name != "posix":
        return True, "windows: job object process memory limit (parent-side)"
    try:
        import resource

        limit_bytes = memory_mb * 1024 * 1024
        soft, hard = resource.getrlimit(resource.RLIMIT_AS)
        # Worker must only tighten inherited limits.  In particular, raising an
        # inherited soft limit would let the sandbox consume more memory than
        # the parent process was configured to allow.  Pin hard to the same
        # effective value so executed code cannot raise soft afterwards.
        inherited_finite = [value for value in (soft, hard) if value != resource.RLIM_INFINITY]
        effective_limit = min([limit_bytes, *inherited_finite])
        resource.setrlimit(resource.RLIMIT_AS, (effective_limit, effective_limit))
        return True, f"posix: RLIMIT_AS={effective_limit} bytes (requested={memory_mb}MB)"
    except Exception as exc:  # noqa: BLE001 — best effort, но диагностируемый
        return False, f"RLIMIT_AS failed: {type(exc).__name__}: {exc}"


def _probe_llm_available(test_provider: str | None) -> bool:
    """Точное повторение provider priority/config gates ``get_llm_query_fn()``
    БЕЗ импорта llm_bridge/создания client (§12.1): env + наличие пакета.
    """
    if test_provider:
        return True
    base_url = os.environ.get("RLM_LLM_BASE_URL")
    if base_url:
        # base URL задан → только OpenAI-path, без fallback к Anthropic.
        if not os.environ.get("RLM_LLM_MODEL"):
            return False
        return importlib.util.find_spec("openai") is not None
    if os.environ.get("ANTHROPIC_API_KEY"):
        return importlib.util.find_spec("anthropic") is not None
    return False


class _WorkerLlmTools:
    """Lazy LLM helpers worker-а + межпроцессная quota (§12.1/§12.2).

    Provider создаётся под lock при ПЕРВОМ single/batch вызове; ошибка поздней
    инициализации — bounded RuntimeError без расхода quota (документированная
    дивергенция от eager init). Резервирование: single +1, batch атомарно +N до
    первого provider call; авансы не возвращаются. Quota lock никогда не
    удерживается во время provider init или network call.
    """

    def __init__(self, quota_value, quota_lock, max_llm_calls: int, test_provider: str | None):
        self._quota_value = quota_value
        self._quota_lock = quota_lock
        self._max = max_llm_calls
        self._test_provider = test_provider
        self._init_lock = threading.Lock()
        self._provider = None
        self._batched = None

    def _ensure_provider(self) -> None:
        with self._init_lock:
            if self._provider is not None:
                return
            try:
                if self._test_provider:
                    module_name, _, attr = self._test_provider.partition(":")
                    module = __import__(module_name, fromlist=[attr])
                    provider = getattr(module, attr)()
                else:
                    from rlm_tools_bsl.llm_bridge import get_llm_query_fn

                    provider = get_llm_query_fn()
                if provider is None:
                    raise RuntimeError("provider factory returned None")
                from rlm_tools_bsl.llm_bridge import make_llm_query_batched

                self._provider = provider
                self._batched = make_llm_query_batched(provider)
            except Exception as exc:
                raise RuntimeError(
                    f"LLM provider initialization failed: {bounded_text(f'{type(exc).__name__}: {exc}', 500)}. "
                    "Configuration/package probe succeeded at rlm_start, but the client could not be created; "
                    "no quota was consumed."
                ) from None

    def _reserve(self, count: int) -> None:
        with self._quota_lock:
            used = int(self._quota_value.value)
            if used + count > self._max:
                raise RuntimeError(f"LLM call limit exceeded: {used} + {count} > {self._max}")
            # Одна aligned запись ДО provider call — переживает kill (§12.2).
            self._quota_value.value = used + count

    def llm_query(self, prompt: str, context: str = "") -> str:
        self._ensure_provider()
        self._reserve(1)
        return self._provider(prompt, context)

    def llm_query_batched(self, prompts: list[str], context: str = "") -> list[str]:
        if not prompts:
            return []
        self._ensure_provider()
        self._reserve(len(prompts))  # all-or-nothing: весь N или controlled error
        return self._batched(prompts, context)


def _serialize_helper_calls(helper_calls) -> list[dict]:
    return [
        {"name": h.name, "elapsed": h.elapsed, "seq": h.seq, "duplicate_of": h.duplicate_of}
        for h in (helper_calls or [])
    ]


def _send(conn, msg: dict, ipc_max_bytes: int) -> None:
    conn.send_bytes(encode_frame(msg, ipc_max_bytes))


_ERROR_TRUNCATION_MARKER = "… [truncated]"


def _json_string_content_bytes(char: str) -> int:
    """Размер одного символа внутри JSON string при ensure_ascii=False."""
    if char in {'"', "\\", "\b", "\f", "\n", "\r", "\t"}:
        return 2
    codepoint = ord(char)
    if codepoint < 0x20:
        return 6
    if codepoint < 0x80:
        return 1
    if codepoint < 0x800:
        return 2
    if codepoint < 0x10000:
        return 3
    return 4


def _replace_surrogates(text: str) -> str:
    """Заменить code points, которые нельзя закодировать строгим UTF-8."""
    return "".join("\ufffd" if 0xD800 <= ord(char) <= 0xDFFF else char for char in text)


def _fit_execute_error_to_frame(message: dict, ipc_max_bytes: int) -> None:
    """Сохранить полный user error, обрезая его только по реальному IPC-бюджету."""
    payload = message["payload"]
    error = payload.get("error")
    if not error:
        return
    # Сначала считаем бюджет без error: это не даёт огромному traceback породить
    # ещё одну заведомо oversized JSON-копию. Если frame не помещается уже здесь,
    # сохраняется прежняя protocol-error семантика для прочего payload.
    payload["error"] = ""
    empty_frame_size = len(encode_frame(message, ipc_max_bytes))

    content_budget = ipc_max_bytes - empty_frame_size
    if len(error) <= content_budget:
        error = _replace_surrogates(error)
        payload["error"] = error
        try:
            encode_frame(message, ipc_max_bytes)
            return
        except SandboxProtocolError:
            payload["error"] = ""

    marker_size = sum(_json_string_content_bytes(char) for char in _ERROR_TRUNCATION_MARKER)
    if marker_size > content_budget:
        payload["error"] = error
        return

    used = marker_size
    prefix_end = 0
    for prefix_end, char in enumerate(error, start=1):
        char_size = _json_string_content_bytes(char)
        if used + char_size > content_budget:
            prefix_end -= 1
            break
        used += char_size
    payload["error"] = _replace_surrogates(error[:prefix_end]) + _ERROR_TRUNCATION_MARKER


def sandbox_worker_main(conn, out_buf, out_published, out_truncated, out_lock, quota_value, quota_lock, boot: dict):
    """Top-level spawn target. ``boot`` — только примитивы (trusted bootstrap);
    вся конфигурация сессии приходит первым ``init``-frame по JSON IPC."""
    stdio_detached, stdio_detail = _detach_stdio()
    expected_parent_pid = int(boot.get("expected_parent_pid", 0))
    isolation_ready, group_detail = _isolate_process_group(expected_parent_pid)
    ipc_max_bytes = int(boot.get("ipc_max_bytes", 4 * 1024 * 1024))
    generation = int(boot.get("generation", 1))
    idx_reader = None
    try:
        try:
            raw = conn.recv_bytes(maxlength=ipc_max_bytes)
        except (EOFError, OSError):
            return
        msg = decode_frame(raw, ipc_max_bytes)
        _type, payload = validate_message(msg, allowed_types={"init"}, expected_generation=generation)
        request_id = msg["request_id"]

        if not stdio_detached:
            # In stdio transport the inherited stdout is the MCP protocol.  Do
            # not execute any session code if raw fd isolation is unavailable.
            _send(
                conn,
                make_message(
                    "init_error",
                    request_id,
                    generation,
                    {"error": f"stdio isolation unavailable ({stdio_detail})"},
                ),
                ipc_max_bytes,
            )
            return

        if not isolation_ready:
            # Без своей process group / обязательного Linux orphan-guard parent не
            # может гарантировать заявленную границу. Fail-fast, как Job Object.
            _send(
                conn,
                make_message(
                    "init_error",
                    request_id,
                    generation,
                    {"error": f"process isolation unavailable ({group_detail})"},
                ),
                ipc_max_bytes,
            )
            return

        memory_limit_applied, memory_limit_detail = _apply_memory_limit(int(payload.get("memory_mb", 0)))

        # Test-only init flag (§18.7): задержка инициализации для проверки
        # parent startup timeout. _rlm_start это поле никогда не заполняет.
        test_delay = payload.get("test_init_delay_seconds")
        if test_delay:
            import time as _time

            _time.sleep(float(test_delay))

        try:
            sandbox, idx_reader, llm_tools, index_loaded, index_warning = _build_session(
                payload, out_buf, out_published, out_truncated, out_lock, quota_value, quota_lock
            )
            registry_snapshot = sandbox.registry_metadata_snapshot()
            detected_prefixes, prefixes_source = _compute_prefixes(sandbox, idx_reader)
        except Exception:
            _send(
                conn,
                make_message(
                    "init_error",
                    request_id,
                    generation,
                    {"error": bounded_text(traceback.format_exc())},
                ),
                ipc_max_bytes,
            )
            return

        _send(
            conn,
            make_message(
                "init_ok",
                request_id,
                generation,
                {
                    "registry_snapshot": registry_snapshot,
                    "detected_prefixes": detected_prefixes,
                    "prefixes_source": prefixes_source,
                    "index_loaded": index_loaded,
                    "index_warning": index_warning,
                    "has_llm_tools": llm_tools is not None,
                    "extension_paths_count": len(payload.get("extension_paths") or []),
                    "python_version": sys.version.split()[0],
                    "pid": os.getpid(),
                    # Диагностика фактически применённых платформенных гарантий:
                    # оператор не должен догадываться, стоит ли потолок памяти (§11.2).
                    "process_group_detail": group_detail,
                    "memory_limit_applied": memory_limit_applied,
                    "memory_limit_detail": memory_limit_detail,
                },
            ),
            ipc_max_bytes,
        )

        _command_loop(conn, sandbox, ipc_max_bytes, generation)
    except SandboxProtocolError as exc:
        try:
            _send(
                conn,
                make_message("worker_error", WORKER_FATAL_REQUEST_ID, generation, {"error": bounded_text(str(exc))}),
                ipc_max_bytes,
            )
        except Exception:
            pass
    except Exception:
        try:
            _send(
                conn,
                make_message(
                    "worker_error", WORKER_FATAL_REQUEST_ID, generation, {"error": bounded_text(traceback.format_exc())}
                ),
                ipc_max_bytes,
            )
        except Exception:
            pass
    finally:
        if idx_reader is not None:
            try:
                idx_reader.close()
            except Exception:
                pass
        try:
            conn.close()
        except Exception:
            pass


def _build_session(payload: dict, out_buf, out_published, out_truncated, out_lock, quota_value, quota_lock):
    """Реконструировать FormatInfo, открыть собственный reader, создать Sandbox.

    Race parent-check → worker-open (§8.3): недоступный индекс НЕ валит init —
    сессия продолжает в live/no-index режиме с warning.
    """
    from rlm_tools_bsl.bsl_index import IndexReader
    from rlm_tools_bsl.format_detector import FormatInfo, SourceFormat
    from rlm_tools_bsl.sandbox import Sandbox

    format_info = None
    fi_payload = payload.get("format_info")
    if isinstance(fi_payload, dict):
        format_info = FormatInfo(
            primary_format=SourceFormat(fi_payload["primary_format"]),
            root_path=fi_payload["root_path"],
            bsl_file_count=int(fi_payload["bsl_file_count"]),
            has_configuration_xml=bool(fi_payload["has_configuration_xml"]),
            metadata_categories_found=list(fi_payload.get("metadata_categories_found") or []),
        )

    idx_reader = None
    index_warning = None
    db_path = payload.get("db_path")
    if db_path:
        try:
            idx_reader = IndexReader(db_path)
        except Exception as exc:
            idx_reader = None
            if payload.get("index_expected"):
                index_warning = (
                    f"Index expected but could not be opened in sandbox worker ({type(exc).__name__}); "
                    "session continues in live/no-index mode — retry after rebuild"
                )

    max_output_chars = int(payload["max_output_chars"])
    writer = SharedStdoutWriter(out_buf, out_published, out_truncated, out_lock, max_output_chars)

    sandbox = Sandbox(
        base_path=payload["base_path"],
        max_output_chars=max_output_chars,
        # Авторитетный deadline — у родителя; внутренний signal/async-exc timeout
        # не должен конкурировать с parent kill (§10.1).
        execution_timeout_seconds=0,
        format_info=format_info,
        idx_reader=idx_reader,
        idx_zero_callers_authoritative=bool(payload.get("idx_zero_callers_authoritative")),
        extension_paths=list(payload.get("extension_paths") or []),
        output_capture_factory=lambda: writer,
        # Отсутствие ключа = прежнее поведение (BSL-хелперы включены).
        enable_bsl_helpers=bool(payload.get("enable_bsl_helpers", True)),
    )
    # reset() на каждый execute делает command loop; сохранить ссылку.
    sandbox._shared_stdout_writer = writer

    llm_tools = None
    if _probe_llm_available(payload.get("test_llm_provider")):
        # quota_value/quota_lock — доверенные bootstrap-handles spawn, не JSON (§12.2.1).
        llm_tools = _WorkerLlmTools(
            quota_value,
            quota_lock,
            int(payload.get("max_llm_calls", 50)),
            payload.get("test_llm_provider"),
        )
        sandbox._namespace["llm_query"] = llm_tools.llm_query
        sandbox._namespace["llm_query_batched"] = llm_tools.llm_query_batched

    return sandbox, idx_reader, llm_tools, idx_reader is not None, index_warning


def _compute_prefixes(sandbox, idx_reader) -> tuple[list[str], str]:
    if idx_reader is not None:
        try:
            prefixes = idx_reader.get_detected_prefixes()
            if prefixes:
                return list(prefixes), "index"
        except Exception:
            pass
    prefix_fn = sandbox._namespace.get("_detected_prefixes")
    if callable(prefix_fn):
        try:
            prefixes = prefix_fn()
            if prefixes:
                return list(prefixes), "fallback"
        except Exception:
            pass
    return [], "none"


def _command_loop(conn, sandbox, ipc_max_bytes: int, generation: int) -> None:
    """Последовательный command loop: execute / ping / shutdown (§8.1.12)."""
    writer = getattr(sandbox, "_shared_stdout_writer", None)
    while True:
        try:
            raw = conn.recv_bytes(maxlength=ipc_max_bytes)
        except (EOFError, OSError):
            return  # parent закрыл канал (kill/close) — просто выходим
        msg = decode_frame(raw, ipc_max_bytes)
        msg_type, payload = validate_message(
            msg, allowed_types={"execute", "shutdown", "ping"}, expected_generation=generation
        )
        request_id = msg["request_id"]
        if msg_type == "ping":
            _send(conn, make_message("pong", request_id, generation), ipc_max_bytes)
            continue
        if msg_type == "shutdown":
            _send(conn, make_message("shutdown_ok", request_id, generation), ipc_max_bytes)
            return
        # execute
        if writer is not None:
            writer.reset()
        try:
            result = sandbox.execute(payload["code"])
            result_payload = {
                # stdout НЕ передаётся: авторитет — shared buffer (§23.4).
                "error": result.error,
                "variables": list(result.variables),
                "helper_calls": _serialize_helper_calls(result.helper_calls),
                "efficiency_hints": result.efficiency_hints,
            }
        except Exception:
            # Ошибка вне пользовательского exec (инфраструктура Sandbox) —
            # worker_error, parent убьёт worker и вернёт controlled error.
            _send(
                conn,
                make_message("worker_error", request_id, generation, {"error": bounded_text(traceback.format_exc())}),
                ipc_max_bytes,
            )
            return
        response = make_message("execute_result", request_id, generation, result_payload)
        _fit_execute_error_to_frame(response, ipc_max_bytes)
        _send(conn, response, ipc_max_bytes)


__all__ = [
    "PROTOCOL_VERSION",
    "SharedStdoutWriter",
    "sandbox_worker_main",
]
