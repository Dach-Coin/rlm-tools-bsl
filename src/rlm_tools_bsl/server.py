import argparse
import importlib.metadata
import json
import logging
import os
import pathlib
import sys
import threading
import time
import traceback
from typing import Annotated, Literal

import anyio

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field

from rlm_tools_bsl.session import SessionManager, build_session_manager_from_env
from rlm_tools_bsl.sandbox import Sandbox
from rlm_tools_bsl.sandbox_backend import (
    InlineSandboxBackend,
    SandboxBackendReaper,
    SandboxClosedError,
    SandboxStartupError,
)
from rlm_tools_bsl._sandbox_config import (
    SandboxConfigError,
    get_sandbox_mode,
    kill_grace_seconds,
    shutdown_deadline_seconds,
    validate_sandbox_env,
)
from rlm_tools_bsl.llm_bridge import validate_llm_env, warmup_openai_import
from rlm_tools_bsl.format_detector import (
    GENERIC_MODE_SESSION_WARNING,
    UNSUPPORTED_FORMAT_SESSION_WARNING,
    FormatInfo,
    SourceFormat,
    SourceSupport,
    classify_source,
    detect_format,
)
from rlm_tools_bsl.extension_detector import (
    ConfigRole,
    detect_extension_context,
    filter_alias_extension_infos,
    find_extension_overrides,
    resolve_config_root,
    validate_root_topology,
)
from rlm_tools_bsl.bsl_knowledge import (
    EFFORT_LEVELS,
    _auto_effort,
    build_generic_strategy,
    _fuzzy_suggest,
    _get_category_helpers,
    _get_disambiguation,
    _get_helper_details,
    _get_section,
    _get_topic_recipe,
    catalog_mode_env_warning,
    ext_list_display_cap,
    get_catalog_mode,
    get_strategy,
    get_strategy_mode,
    list_categories,
    list_sections,
    list_topics,
    slim_recipe_step_helpers,
    slim_recipe_topic,
    summarize_extensions_by_overrides,
)
from rlm_tools_bsl.bsl_strategy_data import (
    ALL_CATALOG,
    ALLOWED_DOMAIN_VALUES,
    CONDITIONAL_HELPERS,
    DOMAIN_KEYS_TEXT,
    HELPER_CORE,
    HELPER_DOMAINS,
    DomainChoice,
    domain_helper_names,
    domain_of_topic,
    domains_param_description,
    normalize_domains,
    recipe_mentions,
)
from rlm_tools_bsl.bsl_index import (
    BUILDER_VERSION,
    IndexReader,
    IndexStatus,
    check_index_usable,
    describe_index_root,
    get_index_db_path,
    index_incomplete,
    index_root_diagnostics,
    stats_indicate_load_failure,
)
from rlm_tools_bsl.sandbox import HelperCall

logging.basicConfig(level=logging.INFO, encoding="utf-8")

# rlm_start.index "index_status" — stable machine-readable contract (codex round 16/22/24):
# explicit IndexStatus→string map (FRESH.value is "fresh", we expose "ok" to mirror
# get_index_info.status); plain STALE/MISSING → "missing"; incomplete handled separately.
_INDEX_STATUS_LABELS = {
    IndexStatus.FRESH: "ok",
    IndexStatus.STALE_AGE: "stale_age",
    IndexStatus.STALE_CONTENT: "stale_content",
}
logger = logging.getLogger(__name__)

# v1.41.0: инструкция сервера — не больше 220 символов (задача 9 плана): предпочтение
# перед сырым grep, порядок rlm_start → rlm_execute, явный выбор доменов для BSL в slim
# и справка по надобности. Перечень ключей живёт в описании rlm_start.domains и в ответе
# на ошибочный старт — сюда не дублируется.
mcp = FastMCP(
    "rlm-tools-bsl",
    stateless_http=True,
    instructions=(
        "Search and navigation over 1C/BSL sources. Prefer these tools over raw grep: rlm_start "
        "(for BSL in slim — with domains), then rlm_execute; rlm_help — as needed."
    ),
)
# FastMCP 1.x не принимает version в конструкторе, а низкоуровневый сервер без неё
# подставляет версию пакета mcp — интегратор видел в serverInfo.version чужую версию
# (при mcp 1.29.0 и нашей 1.32.1 сервер представлялся как 1.29.0) и не мог выбрать
# парсер под нашу.
mcp._mcp_server.version = importlib.metadata.version("rlm-tools-bsl")

session_manager = SessionManager()  # defaults for tests/import

# v1.29.0: значения — backend-объекты (InlineSandboxBackend | ProcessSandboxBackend),
# не голые Sandbox. Имя сохранено для минимального diff. Замок защищает ТОЛЬКО
# словарь — его нельзя держать на время execute (§9.1).
_sandboxes: dict = {}
_sandboxes_lock = threading.Lock()
# A start captures this before slow initialization and may publish its backend
# only if shutdown has not crossed that operation.
_sandbox_registry_epoch = 0
# Once shutdown starts, registration remains closed for this server lifecycle.
# Tests that exercise several synthetic lifecycles reset the flag explicitly.
_sandbox_registry_accepting = True
# Process backends that already own (or are about to own) a worker, but have not
# yet been published in ``_sandboxes``.  Shutdown must be able to revoke them
# during a slow init instead of waiting for the 60s startup timeout.
_starting_sandbox_backends: dict[int, object] = {}

# Единственный владелец завершающей фазы lifecycle backend-ов (§9.4): teardown-пути
# только снимают backend из registry + request_close + enqueue сюда.
_reaper = SandboxBackendReaper()


def _begin_sandbox_backend_lifecycle() -> None:
    """Allow registrations for a new invocation without reviving an old epoch."""
    global _sandbox_registry_accepting

    with _sandboxes_lock:
        _sandbox_registry_accepting = True


@mcp.custom_route("/health", methods=["GET"])
async def _health_endpoint(request):  # type: ignore[no-untyped-def]
    from starlette.responses import JSONResponse

    return JSONResponse({"status": "ok"})


from rlm_tools_bsl.helpers import _SKIP_DIRS, _BINARY_EXTENSIONS


def _auto_scan_overrides(ext_context) -> dict[str, list[dict]]:
    """Auto-scan extension overrides during rlm_start.

    Returns dict mapping extension path -> list of override dicts (key "self" for
    an extension-role session). Consumed by (a) the strategy's bounded "CRITICAL
    EXTENSIONS DETECTED" by-object summary and (b) the rlm_start response — which
    surfaces only the COUNT, NOT the full per-override dump. v1.19.0: the inline
    dump was dropped from the response because it duplicated get_overrides()/
    find_ext_overrides(), was not actionable from the sandbox (tool-response text,
    not a variable), went unused by agents (e2e: all re-fetched), and cost ~30K
    tokens on EVERY session of an extension config. Full detail on demand via
    get_overrides('Object').
    """

    result: dict[str, list[dict]] = {}
    current = ext_context.current

    try:
        if current.role == ConfigRole.EXTENSION:
            result["self"] = find_extension_overrides(current.path)

        elif current.role == ConfigRole.MAIN and ext_context.nearby_extensions:
            for ext in ext_context.nearby_extensions:
                result[ext.path] = find_extension_overrides(ext.path)
    except Exception:
        pass  # non-critical, don't fail rlm_start

    return result


def _scan_metadata(path: str) -> dict:
    extensions: dict[str, int] = {}
    total_files = 0
    total_lines = 0
    sampled_lines = 0
    sampled_files = 0
    sample_budget = 500

    for dirpath, dirnames, filenames in os.walk(path):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]

        for fname in filenames:
            if fname.startswith("."):
                continue
            ext = os.path.splitext(fname)[1] or "(no ext)"
            extensions[ext] = extensions.get(ext, 0) + 1
            total_files += 1

            if ext not in _BINARY_EXTENSIONS:
                try:
                    fpath = os.path.join(dirpath, fname)
                    with open(fpath, encoding="utf-8-sig", errors="replace") as f:
                        file_line_count = sum(1 for _ in f)
                    total_lines += file_line_count

                    if sampled_files < sample_budget:
                        sampled_lines += file_line_count
                        sampled_files += 1
                except OSError:
                    pass

    return {
        "total_files": total_files,
        "total_lines": total_lines,
        "sampled_lines": sampled_lines,
        "sampled_files": sampled_files,
        "file_types": dict(sorted(extensions.items(), key=lambda x: -x[1])[:10]),
    }


def _release_session_resources(session_id: str, reason: str = "ttl_eviction") -> None:
    """Идемпотентный bounded teardown ресурсов сессии (двухфазная схема §9.3-9.4):
    detach из registry → неблокирующий request_close → enqueue в reaper.
    НИКОГДА не берёт session execution lock и не ждёт join/kill_grace."""
    with _sandboxes_lock:
        backend = _sandboxes.pop(session_id, None)
        if backend is not None and getattr(backend, "mode", None) == "process":
            try:
                backend.request_close(reason)
            except Exception:
                logger.warning("request_close failed for session %s", session_id, exc_info=True)
            finally:
                # Keep lifecycle ownership continuous: shutdown takes the same
                # registry lock before inspecting the reaper, so it cannot pass
                # between detaching a live worker and making it reaper-visible.
                _reaper.enqueue(backend)
            return
    if backend is not None:
        try:
            backend.request_close(reason)
        except Exception:
            logger.warning("request_close failed for session %s", session_id, exc_info=True)
        # Inline: процесса нет. IndexReader закрывается СИНХРОННО — асинхронная
        # сдача в reaper ломала внешний инвариант: на Windows открытый handle
        # bsl_index.db не даёт сразу после rlm_end пересобрать/удалить индекс
        # (WinError 32). Ресурс, однако, уже НЕ единственный (v1.36.0): inline
        # backend владеет ещё и фоновым прогревом живого каталога, поэтому
        # секундный deadline ниже может быть использован целиком — bounded join
        # этого потока идёт ПОСЛЕ закрытия reader-а. Пока поток жив и бюджет не
        # исчерпан, finish_close честно возвращает residual, и сессию доводит
        # reaper; это ПРОМЕЖУТОЧНОЕ состояние, а не вечное — на исчерпанном
        # бюджете (force_abort / последняя попытка reaper-а) закрытие
        # доводится, а незавершённый поток-демон отцепляется.
        # Deadline здесь по-прежнему НЕ ожидание для ветки активного execute, а
        # маркер «не форсировать под работающим кодом». Для process-backend путь
        # остаётся асинхронным — там ждать пришлось бы kill_grace/join, что
        # запрещено (§9.3).
        finished = False
        if getattr(backend, "mode", None) == "inline":
            try:
                finished = backend.finish_close(time.monotonic() + 1.0).closed
            except Exception:
                logger.warning("inline finish_close failed for session %s", session_id, exc_info=True)
        if not finished:
            _reaper.enqueue(backend)


def _cleanup_expired_resources() -> None:
    session_manager.cleanup_expired()  # on_evict → _release_session_resources


def _track_starting_backend(backend, expected_epoch: int) -> bool:
    """Publish an initializing process backend only to lifecycle management."""
    with _sandboxes_lock:
        if not _sandbox_registry_accepting or expected_epoch != _sandbox_registry_epoch:
            return False
        _starting_sandbox_backends[id(backend)] = backend
        return True


def _untrack_starting_backend(backend) -> None:
    with _sandboxes_lock:
        _starting_sandbox_backends.pop(id(backend), None)


def _reap_failed_starting_backend(backend) -> bool:
    """Detach a failed constructor while retaining ownership of any residual.

    Return whether this call transferred the backend.  ``False`` normally means
    shutdown already removed it from the startup registry and owns cleanup.
    """
    with _sandboxes_lock:
        if _starting_sandbox_backends.pop(id(backend), None) is None:
            # Shutdown already claimed this backend and owns its finalization.
            return False
        try:
            backend.request_close("start_failure")
        except Exception:
            logger.warning("failed-start backend revoke failed", exc_info=True)
        finally:
            # Transfer ownership before releasing the registry lock, so shutdown
            # cannot pass between detach and visibility in the reaper.
            _reaper.enqueue(backend)
        return True


def _failed_process_backend_has_lifecycle_owner(backend) -> bool:
    """Transfer a failed process backend, or recognize shutdown ownership."""
    if getattr(backend, "mode", None) != "process":
        return False
    if _reap_failed_starting_backend(backend):
        return True
    with _sandboxes_lock:
        # The production process factory always registers before spawning.  If
        # its entry is gone during shutdown, the shutdown snapshot owns it;
        # running a second finish_close here could block shutdown on _close_lock
        # beyond its single global deadline.
        return not _sandbox_registry_accepting


def _publish_session_backend(session_id: str, session, backend, expected_epoch: int) -> bool:
    """Publish *backend* only for the still-current session and server epoch.

    Lock order is SessionManager → backend registry.  Eviction callbacks run
    outside the manager lock, while registry holders never enter the manager.
    """

    def publish() -> bool:
        with _sandboxes_lock:
            _starting_sandbox_backends.pop(id(backend), None)
            if not _sandbox_registry_accepting or expected_epoch != _sandbox_registry_epoch:
                return False
            _sandboxes[session_id] = backend
            return True

    published = session_manager._run_if_current(session_id, session, publish)
    # If the Session disappeared, ``publish`` was not called at all.  Keep the
    # lifecycle-only entry until the caller's failure path can transfer it
    # atomically to the reaper; detaching it here would open an ownership gap in
    # which concurrent shutdown cannot see the live worker.
    return published


def _session_backend_is_current(session_id: str, session, backend) -> bool:
    """Identity recheck after waiting for the per-session execution lock."""

    def check_backend() -> bool:
        with _sandboxes_lock:
            return _sandboxes.get(session_id) is backend

    return session_manager._run_if_current(session_id, session, check_backend)


session_manager.on_evict = _release_session_resources


from rlm_tools_bsl._paths import (
    _resolve_mapped_drive,
    _resolve_path_map,
    canonicalize_path as _canonicalize_path,
)


def _normalize_and_validate_path(raw_path: str) -> tuple[str, str | None]:
    """Canonicalize + resolve config-root.

    Returns ``(effective_path, error_json)`` — if ``error_json`` is non-None
    it's a pre-serialized JSON error response to return directly to the caller
    (non-existent directory, or ambiguous MAIN candidates without a ``cf``
    tie-breaker).
    """
    canonical = _canonicalize_path(raw_path)
    if not os.path.isdir(canonical):
        hint = ""
        if len(raw_path) >= 2 and raw_path[1] == ":" and not os.path.isdir(raw_path[:3]):
            hint = (
                f" (drive {raw_path[:2]} is not accessible to this process; "
                "use UNC path like \\\\server\\share\\... instead)"
            )
        return (
            canonical,
            json.dumps(
                {"error": f"Directory not found: {raw_path}{hint}"},
                ensure_ascii=False,
            ),
        )

    effective, candidates = resolve_config_root(canonical)
    # Ambiguous: multiple MAINs, no cf-tie-breaker ⇒ `resolve_config_root`
    # returned the container path unchanged along with the candidate list.
    if len(candidates) > 1 and effective == canonical:
        return (
            canonical,
            json.dumps(
                {
                    "error": (
                        f"Multiple main configurations found under {canonical}. "
                        "Point 'path' at a specific configuration root, or rename one "
                        "of the direct subdirectories to 'cf' to use it as the primary."
                    ),
                    "main_candidates": [{"name": c.name, "path": c.path} for c in candidates],
                },
                ensure_ascii=False,
            ),
        )

    return (effective, None)


def _recover_index_base_path_for_missing_source(raw_path: str) -> str | None:
    """Locate an existing index by its stored metadata when the source dir is gone.

    Issue #16: ``rlm_index(action='drop'|'info')`` operate on the cache/index —
    which lives under ``get_index_dir_root()/<md5(effective_path)>/`` and stores
    the effective ``base_path`` in ``index_meta`` — not on the source tree. When
    the source directory has been deleted (project decommissioned) we can no
    longer run ``resolve_config_root`` to recompute the effective path, so we
    scan the index root and match a stored ``base_path`` against the requested
    (canonicalized) path, mirroring ``resolve_config_root``'s own contract:

    * exact match — registered path IS the config root (flat/``cf`` layout); or
    * a **direct child** of the requested path — ``resolve_config_root`` only
      ever selects the container itself or a depth-1 subdirectory (a single
      MAIN, or the ``cf`` subdir as tie-breaker among several MAINs). We apply
      the same rule: a single direct-child index wins; when several direct-child
      indexes exist we prefer the one named ``cf`` (matching the build-time
      tie-breaker). Nested/grandchild indexes (e.g. a separately-built extension
      index deeper in the tree) are ignored, so they no longer cause a false
      ``ambiguous`` bail (codex finding).

    Returns the stored effective ``base_path``, or ``None`` if nothing matches
    (or the direct-child match is genuinely ambiguous).
    """
    from rlm_tools_bsl.bsl_index import _read_index_meta, get_index_dir_root

    canonical = _canonicalize_path(raw_path)
    root = get_index_dir_root()
    try:
        if not root.is_dir():
            return None
        subdirs = list(root.iterdir())
    except OSError:
        return None

    norm_target = os.path.normcase(os.path.normpath(canonical))
    exact: list[str] = []
    children: list[tuple[str, str]] = []  # (stored base_path, lowercased basename)
    for sub in subdirs:
        try:
            if not sub.is_dir():
                continue
        except OSError:
            continue
        db_path = sub / "bsl_index.db"
        if not db_path.exists():
            db_path = sub / "method_index.db"  # legacy name (see _migrate_old_index_db)
            if not db_path.exists():
                continue
        meta = _read_index_meta(db_path)
        if not meta:
            continue
        stored = meta.get("base_path")
        if not stored:
            continue
        norm_stored = os.path.normcase(os.path.normpath(stored))
        if norm_stored == norm_target:
            exact.append(stored)
        elif os.path.dirname(norm_stored) == norm_target:
            children.append((stored, os.path.basename(norm_stored)))

    if exact:
        return exact[0]
    if len(children) == 1:
        return children[0][0]
    if len(children) > 1:
        # Case-insensitive `cf` tie-breaker (mirror resolve_config_root's
        # `.name.lower() == "cf"`): normcase is a no-op on POSIX, so a dir named
        # `CF` would not fold to lowercase here — lower() explicitly.
        cf_matches = [stored for stored, name in children if name.lower() == "cf"]
        if len(cf_matches) == 1:
            return cf_matches[0]
    return None


# --- Background build jobs (MCP async fire-and-forget) ---
_build_jobs_lock = threading.Lock()
# Key = resolved filesystem path (str).
# Value = {"status": "building"|"done"|"error", "action": "build"|"update",
#          "project": str|None, "started_at": float, "finished_at": float|None,
#          "result": dict|None, "error": str|None}
_build_jobs: dict[str, dict] = {}


def _unsupported_format_build_error(resolved: str) -> str | None:
    """Гейт нового построения индекса на чужом формате (v1.32.0).

    None — build разрешен; строка — готовый JSON-отказ.

    ``classify_source`` здесь НЕ используется намеренно: он сам зовёт
    ``probe_bsl``, и на чужом дереве без ``.bsl`` обход был бы двойным.
    Маппинг ``probe`` → ``source_support`` совпадает с ``classify_source``.
    """
    from rlm_tools_bsl.format_detector import (
        NO_BSL_INDEX_REFUSAL,
        UNSUPPORTED_FORMAT_INDEX_WARNING,
        has_our_format_descriptor,
        probe_bsl,
    )

    if has_our_format_descriptor(resolved):
        return None

    probe = probe_bsl(resolved)
    if probe != "found":
        # probe == "unknown" (нечитаемое дерево) тоже отказ: build требует
        # доказанный found. Текст NO_BSL_INDEX_REFUSAL покрывает оба случая.
        return json.dumps(
            {
                "error": NO_BSL_INDEX_REFUSAL,
                "path": resolved,
                "source_support": "foreign_no_bsl" if probe == "none" else "foreign_with_bsl",
            },
            ensure_ascii=False,
        )

    return json.dumps(
        {
            "error": (
                UNSUPPORTED_FORMAT_INDEX_WARNING
                + " Подтвердить сборку может только человек в терминале: "
                + f'rlm-bsl-index index build "{resolved}" --allow-unsupported-format'
            ),
            "path": resolved,
            "source_support": "foreign_with_bsl",
        },
        ensure_ascii=False,
    )


def _create_session_backend(
    *,
    sandbox_mode: str,
    resolved: str,
    session,
    max_output_chars: int,
    execution_timeout_seconds: int,
    format_info,
    idx_reader,
    db_path,
    callers_authoritative: bool,
    ext_paths_for_sandbox: list[str],
    registry_epoch: int,
    enable_bsl_helpers: bool = True,
    current_config_role: str | None = None,
    current_config_name: str = "",
    current_config_root: str = "",
    extension_name_by_root: dict[str, str] | None = None,
):
    """Фабрика backend по режиму (§5.2): выбор делается один раз при rlm_start,
    дальше server не ветвится по типу backend.

    Возвращает ``(backend, parent_reader_still_owned)``: в inline режиме reader
    переходит во владение backend (закрывается его finish_close); в process
    режиме reader остаётся временным parent-объектом и его закрывает вызывающий
    сразу после успешного init (§8.3).

    Роль/имя/current-root/карта имён расширений (v1.34.0) уже вычислены сервером
    через ``detect_extension_context`` и едут JSON-safe примитивами — внутри
    сессии detector не повторяется."""
    # Защитная проверка топологии корней ДО создания backend: на активном
    # BSL-пути пересекающиеся base/ext дали бы двойной учёт одного дерева.
    # В generic-режиме валидация не выполняется — provenance там не используется.
    if format_info is not None and enable_bsl_helpers:
        validate_root_topology(resolved, list(ext_paths_for_sandbox or []))
    if sandbox_mode == "process":
        from rlm_tools_bsl.sandbox_process import (
            ProcessBackendConfig,
            ProcessSandboxBackend,
            format_info_to_payload,
        )

        config = ProcessBackendConfig.from_env(
            base_path=resolved,
            max_output_chars=max_output_chars,
            execution_timeout_seconds=execution_timeout_seconds,
            format_info_payload=format_info_to_payload(format_info),
            db_path=str(db_path) if idx_reader is not None else None,
            index_expected=idx_reader is not None,
            idx_zero_callers_authoritative=callers_authoritative,
            extension_paths=ext_paths_for_sandbox,
            enable_bsl_helpers=enable_bsl_helpers,
            max_llm_calls=session.max_llm_calls,
            llm_calls_used=session.llm_calls_used,
            current_config_role=current_config_role,
            current_config_name=current_config_name,
            current_config_root=current_config_root,
            extension_name_by_root=dict(extension_name_by_root or {}),
        )

        def _startup_unregister(candidate) -> None:
            # Сначала ДИАГНОСТИКА, затем ОБЯЗАТЕЛЬНО передача в reaper. Lifecycle-
            # cleanup живёт в `finally`: исключение drain-а ловится и логируется, но
            # не пропускает reaper-transfer и не подменяет исходный SandboxClosedError.
            try:
                _drain_startup_log_records(session.session_id, candidate)
            except Exception:  # pragma: no cover - defensive
                logger.warning("sandbox: session=%s startup drain failed", session.session_id, exc_info=True)
            finally:
                _reap_failed_starting_backend(candidate)

        backend = ProcessSandboxBackend(
            config,
            startup_register=lambda candidate: _track_starting_backend(candidate, registry_epoch),
            startup_unregister=_startup_unregister,
        )
        return backend, True

    from rlm_tools_bsl._scan_budget import get_scan_ledger

    sandbox = Sandbox(
        base_path=resolved,
        max_output_chars=max_output_chars,
        execution_timeout_seconds=execution_timeout_seconds,
        format_info=format_info,
        idx_reader=idx_reader,
        idx_zero_callers_authoritative=callers_authoritative,
        extension_paths=ext_paths_for_sandbox,
        enable_bsl_helpers=enable_bsl_helpers,
        current_config_role=current_config_role,
        current_config_name=current_config_name,
        current_config_root=current_config_root,
        extension_name_by_root=dict(extension_name_by_root or {}),
        # v1.40.0: ОДНА аренда общего бюджета обхода на все inline-сессии процесса.
        # Сессии она не принадлежит, поэтому закрытию backend-а освобождать нечего.
        scan_lease=get_scan_ledger().inline_lease(),
    )
    backend = InlineSandboxBackend(
        sandbox,
        idx_reader,
        max_llm_calls=session.max_llm_calls,
        llm_calls_used=session.llm_calls_used,
    )
    return backend, False


def _drain_startup_log_records(session_id: str, backend) -> None:
    """Записать startup-предупреждения воркера в ``server.log`` (v1.34.0).

    Читает READ-ONCE свойство backend и пишет каждую запись ПАРАМЕТРИЗОВАННЫМ
    вызовом логгера (не format-string из worker), сохраняя связь с сессией.
    В inline-backend свойство всегда пусто — там логгер и так родительский.
    В public JSON-ответ строки НЕ копируются: это диагностика оператора.
    """
    try:
        records = getattr(backend, "startup_log_records", None) or []
    except Exception:  # pragma: no cover - диагностика не имеет права ломать lifecycle
        logger.warning("sandbox: session=%s startup log drain failed", session_id, exc_info=True)
        return
    for record in records:
        logger.warning("sandbox: session=%s %s", session_id, record)


def _start_extension_warnings(ext_context, ext_total: int, ext_shown: int) -> list[str]:
    """Предупреждения о расширениях для ОТВЕТА rlm_start (v1.41.0).

    Поимённый список (имя, назначение, префикс, путь) ответ уже несёт в
    ``extension_context.nearby_extensions``, аннотации перехвата объясняет блок
    стратегии — прежний warning повторял и то и другое. Для MAIN с соседями —
    одна строка: сколько, где список и, при усечении, как получить полный.
    ``_build_warnings`` не трогается: его же отдаёт хелпер ``detect_extensions()``,
    у которого ``extension_context`` нет. Сессии на расширении — прежние строки.
    """
    if ext_context.current.role != ConfigRole.MAIN or not ext_context.nearby_extensions:
        return list(ext_context.warnings)
    if ext_shown < ext_total:
        return [
            f"{ext_total} extensions detected near main config — top {ext_shown} by overrides in "
            "extension_context.nearby_extensions; complete list — detect_extensions()."
        ]
    return [f"{ext_total} extension(s) detected near main config — see extension_context.nearby_extensions."]


def _session_warnings(source_support: SourceSupport, ext_warnings: list[str]) -> list[str]:
    """Предупреждение о неподдерживаемом формате идёт ПЕРВЫМ (v1.32.0):
    агент читает warnings[0] и не должен узнать про чужой формат после
    сообщений про расширения."""
    if source_support is SourceSupport.FOREIGN_WITH_BSL:
        return [UNSUPPORTED_FORMAT_SESSION_WARNING, *ext_warnings]
    if source_support is SourceSupport.FOREIGN_NO_BSL:
        return [GENERIC_MODE_SESSION_WARNING, *ext_warnings]
    return list(ext_warnings)


# ─────────────────────────────────────────────────────────────────────
#              Домены хелперов: выдача подписей (v1.41.0)
# ─────────────────────────────────────────────────────────────────────

# Файловые и LLM-хелперы живут вне реестра BSL-хелперов и выдаются всегда: файловые —
# в каждой сессии, LLM — если провайдер настроен.
_FILE_HELPER_SIGNATURES = (
    "read_file(path) -> str (numbered: '  42 | code')",
    "read_files(paths) -> dict[path, str] — BATCH: читай N файлов одним вызовом вместо N×read_file (numbered)",
    "grep(pattern, path='.') -> list[dict] keys: file, line, text  # пути через /",
    "grep_summary(pattern, path='.') -> compact grouped string  # пути через /",
    "grep_read(pattern, path='.', max_files=10, context_lines=0) -> {matches, files (numbered), summary}  # пути через /",
    "glob_files(pattern) -> list[str]  # пути через /",
    "tree(path='.', max_depth=3) -> str",
    "find_files(name) -> list[str]  # пути через /",
)
_LLM_HELPER_SIGNATURES = (
    "llm_query(prompt, context='')",
    "llm_query_batched(prompts, context='')",
)
# Сколько исчезнувших после перезапуска воркера имён перечислять в registry_changed.
_REGISTRY_CHANGED_MAX = 10
# Описание параметра rlm_execute.domains (≤ 300): попутная догрузка без отдельного хода.
_EXECUTE_DOMAINS_DESCRIPTION = (
    "Догрузить домены хелперов: их подписи придут в signatures ЭТОГО ответа — используй их в "
    "следующем вызове. Пример: domains=['связи']; ключи — в rlm_start.domains."
)


def _registry_view(worker_names) -> dict[str, dict]:
    """Представление реестра сессии: имена — из реестра воркера, пересечённые с
    каталогом родителя (чужие отбрасываются), записи — из каталога.

    Питает ``available_functions``, стратегию (таблицу хелперов full и git-блок) и
    ``signatures`` — текст подписи от воркера не попадает агенту нигде. В норме
    тексты совпадают (воркер строит срез из того же каталога), поэтому full-ответ
    побайтно прежний.
    """
    from rlm_tools_bsl.bsl_helpers import build_helper_metadata_snapshot

    catalog = build_helper_metadata_snapshot()
    return {name: catalog[name] for name in worker_names if isinstance(name, str) and name in catalog}


def _domains_required_error(choice: DomainChoice) -> dict:
    """Отказ slim/BSL-старта без явного выбора доменов (до создания сессии)."""
    out: dict = {
        "error": (
            "Для BSL-проекта в slim нужен явный выбор доменов хелперов — параметр domains у rlm_start. "
            f"Ключи: {DOMAIN_KEYS_TEXT}. Пример: domains=['код'] (узкий вопрос) или domains=[] (только ядро)."
        ),
        "allowed_domains": list(ALLOWED_DOMAIN_VALUES),
    }
    if choice.ignored_total:
        out["domains_ignored"] = choice.ignored_summary()
    return out


def _session_domains_log_value(session) -> str:
    """Итог выдачи для журнала: ключи | core | весь каталог | - (нет BSL-реестра)."""
    if not session.registry_view:
        return "-"
    if session.catalog_all_at_start or ALL_CATALOG in session.helper_domains:
        return ALL_CATALOG
    return ",".join(session.helper_domains) if session.helper_domains else "core"


def _session_wanted_helpers(session) -> set[str]:
    """Имена, которые сессия должна видеть по своему способу выдачи: весь каталог
    или ядро плюс выбранные домены (в пределах живого реестра)."""
    view = session.registry_view
    if session.catalog_all_at_start or ALL_CATALOG in session.helper_domains:
        return set(view)
    return (set(HELPER_CORE) | domain_helper_names(session.helper_domains)) & set(view)


def _deliver_signatures(session, backend, result, requested_domains) -> tuple[dict, str]:
    """Подписи в ответе ``rlm_execute``: попутная догрузка, вызов по имени, смена
    поколения воркера. Возвращает ``(дополнительные ключи ответа, поля журнала)``.

    Состояние сессии меняется только здесь и только под ``execution_lock``
    (вызывается из ``_finish_rlm_execute``, то есть в ответе, прошедшем выполнение).
    """
    extra: dict = {}
    log = ""
    if not session.registry_view and not backend.registry_names:
        return extra, log  # generic: BSL-реестра нет, выдавать нечего

    domain_group: list[str] = []
    # 1. Смена поколения воркера: после перезапуска снимок реестра новый, а наличие
    #    git_search определяется при создании хелперов заново.
    if result.generation != session.registry_generation:
        old_view = session.registry_view
        new_view = _registry_view(backend.registry_names)
        removed = [name for name in old_view if name not in new_view]
        session.registry_view = new_view
        session.registry_generation = result.generation
        session.delivered_helpers &= set(new_view)
        wanted = _session_wanted_helpers(session)
        domain_group.extend(n for n in new_view if n not in old_view and n in wanted)
        if removed:
            extra["registry_changed"] = {"removed": removed[:_REGISTRY_CHANGED_MAX]}
            if len(removed) > _REGISTRY_CHANGED_MAX:
                extra["registry_changed"]["removed_total"] = len(removed)
    view = session.registry_view
    delivered_before = set(session.delivered_helpers)

    # 2. Попутная догрузка доменов. Весь каталог уже выдан на старте (full, all,
    #    выбор «весь каталог») — параметр не читается вовсе.
    domains_active = not session.catalog_all_at_start and ALL_CATALOG not in session.helper_domains
    if requested_domains is not None and domains_active:
        choice = normalize_domains(requested_domains)
        new_keys: list[str] = []
        if choice.all_catalog:
            new_keys = [ALL_CATALOG]
            target = set(view)
        else:
            new_keys = [k for k in choice.keys if k not in session.helper_domains]
            target = domain_helper_names(choice.keys) & set(view)
        if new_keys:
            session.helper_domains.extend(new_keys)
            session.domains_added += 1
            log += f" domains+=<{','.join(new_keys)}>"
        domain_group.extend(n for n in view if n in target and n not in delivered_before and n not in domain_group)
        if choice.ignored_total:
            extra["domains_ignored"] = {**choice.ignored_summary(), "allowed": list(ALLOWED_DOMAIN_VALUES)}
            log += f" domains_ignored=<{choice.ignored_log_value()}>"
    domain_group = [n for n in view if n in set(domain_group)]

    # 3. Вызов по имени. При hard timeout / потере воркера история вызовов неизвестна:
    #    начатые вызовы узнать нельзя, поэтому подписи по имени выданными не считаются,
    #    а журнал пишет outside=unknown вместо заключения «промахов не было».
    state = result.sandbox_state or {}
    by_name: list[str] = []
    if state.get("status") == "terminated":
        session.outside_unknown_executes += 1
        log += " outside=unknown"
    else:
        called = {h.name for h in result.helper_calls}
        outside = [n for n in view if n in called and n not in delivered_before]
        if outside:
            session.outside_helpers.extend(outside)
            log += f" outside={','.join(outside)}"
        by_name = [n for n in outside if n not in domain_group]

    # 4. Ответ: сначала домены, затем вызванные по имени — без повторов. Подпись,
    #    целиком стоящая в тексте ошибки (подсказка о неверном аргументе), не
    #    дублируется, но выданной считается.
    error_text = result.error or ""
    signatures: list[str] = []
    for name in (*domain_group, *by_name):
        session.delivered_helpers.add(name)
        sig = view[name]["sig"]
        if sig not in error_text:
            signatures.append(sig)
    if signatures:
        extra = {"signatures": signatures, **extra}
    return extra, log


def _rlm_start(
    path: str | None,
    query: str,
    effort: str = "auto",
    max_output_chars: int = 15_000,
    max_llm_calls: int | None = None,
    max_execute_calls: int | None = None,
    execution_timeout_seconds: int = 45,
    include_metadata: bool = False,
    project: str | None = None,
    domains: list[str] | str | None = None,
    require_domains: bool = False,
) -> str:
    t0 = time.monotonic()
    with _sandboxes_lock:
        if not _sandbox_registry_accepting:
            return json.dumps({"error": "Server is shutting down; new sandbox sessions are not accepted"})
        registry_epoch = _sandbox_registry_epoch
    _cleanup_expired_resources()

    # --- Resolve project name to path ---
    project_hint: str | None = None

    if path is None and project is None:
        return json.dumps(
            {"error": "Either 'path' or 'project' must be provided"},
            ensure_ascii=False,
        )

    if path is None:
        from rlm_tools_bsl.projects import RegistryCorruptedError, get_registry

        try:
            reg = get_registry()
            matches, method = reg.resolve(project)  # type: ignore[arg-type]
        except RegistryCorruptedError as exc:
            return json.dumps(
                {"error": f"Registry file is corrupted: {exc}. Run rlm_projects(action='list') after fixing the file."},
                ensure_ascii=False,
            )
        if not matches:
            all_projects = reg.list_projects()
            available = [{"name": p["name"], "description": p.get("description", "")} for p in all_projects]
            return json.dumps(
                {
                    "error": f"Project not found: {project}",
                    "available_projects": available,
                },
                ensure_ascii=False,
            )
        if len(matches) > 1:
            ambiguous = [{"name": p["name"], "description": p.get("description", "")} for p in matches]
            return json.dumps(
                {
                    "error": f"Ambiguous project name: {project}",
                    "matches": ambiguous,
                },
                ensure_ascii=False,
            )
        # Single match
        if method == "fuzzy":
            return json.dumps(
                {"error": f"Did you mean '{matches[0]['name']}'?"},
                ensure_ascii=False,
            )
        # exact or substring -- OK
        path = matches[0]["path"]

    # Shared normalization: path_map → resolve → mapped drive → cf-root
    resolved, error_json = _normalize_and_validate_path(path)
    if error_json is not None:
        return error_json

    if project is None:
        # path was provided directly — register-hint check (after cf-normalization)
        from rlm_tools_bsl.projects import get_registry

        try:
            reg = get_registry()
            if not reg.is_path_registered(resolved):
                project_hint = (
                    "This path is not in the project registry. "
                    "Register it with rlm_projects(action='add', name='...', path='...') "
                    "to use rlm_start(project='name') next time."
                )
        except Exception:
            pass  # non-critical

    logger.info("rlm_start: path=%s effort=%s include_metadata=%s", path, effort, include_metadata)

    # Fail-fast режим песочницы (§11.1): невалидный RLM_SANDBOX_MODE не имеет
    # права молча превратиться в inline. main() валидирует на старте сервера;
    # эта проверка закрывает прямые вызовы _rlm_start (тесты/embedding).
    try:
        sandbox_mode = get_sandbox_mode()
    except SandboxConfigError as e:
        return json.dumps({"error": f"Sandbox configuration error: {e}"}, ensure_ascii=False)

    # Гейт неподдерживаемых форматов (v1.32.0): классификация ВСЕГДА по живому
    # диску — index fast path тут не помогает, чужое дерево могло получить индекс
    # до появления гейта. Замеры: боевые cf/edt 0.4-1.4 мс. v1.41.0: классификация
    # идёт ДО создания сессии — выбор доменов проверяется только для BSL (generic
    # выясняется по пути), а отказ из-за выбора не должен оставлять ни сессии, ни
    # backend. Результат переиспользуется ниже, второго обхода нет.
    try:
        source_support = classify_source(resolved)
    except Exception as e:
        logger.error("rlm_start: source classification failed for path=%s: %s", resolved, e, exc_info=True)
        return json.dumps({"error": f"Session init failed: {type(e).__name__}: {e}"}, ensure_ascii=False)
    generic_mode = source_support is SourceSupport.FOREIGN_NO_BSL

    # v1.41.0: выбор доменов хелперов. Режимы стратегии и каталога читаются ОДИН раз
    # на старт; в full, при RLM_CATALOG_MODE=all и в generic значение не читается
    # вовсе (прежний вызов без domains проходит). Публичный тул требует явный выбор
    # (require_domains); прямой _rlm_start(domains=None) означает «только ядро».
    strategy_mode = get_strategy_mode()
    catalog_mode = get_catalog_mode()
    domains_mode = strategy_mode == "slim" and catalog_mode == "domains" and not generic_mode
    domain_choice = normalize_domains(domains) if domains_mode else None
    if domain_choice is not None and require_domains and not (domain_choice.recognized or domain_choice.explicit_empty):
        logger.info(
            "rlm_start: session=- domains=<rejected> ignored=<%s>%s",
            domain_choice.ignored_log_value(),
            _log_text_field("query", query),
        )
        return json.dumps(_domains_required_error(domain_choice), ensure_ascii=False)

    effort, max_llm_calls, max_execute_calls = resolve_session_limits(effort, query, max_llm_calls, max_execute_calls)
    # effort_config нужен дальше (safe_grep_max_files / guidance) — от ИТОГОВОГО effort
    # (после auto-эвристики/RLM_FORCE_EFFORT он всегда валиден, но .get безопаснее).
    effort_config = EFFORT_LEVELS.get(effort, EFFORT_LEVELS["medium"])

    try:
        session_id = session_manager.create(
            path=resolved,
            query=query,
            max_output_chars=max_output_chars,
            max_llm_calls=max_llm_calls,
            max_execute_calls=max_execute_calls,
        )
    except RuntimeError as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)

    try:
        from rlm_tools_bsl.cache import touch_project_cache

        touch_project_cache(resolved)
    except Exception as exc:
        logger.debug("rlm_start: touch_project_cache failed: %s", exc)

    session = session_manager.get(session_id)
    if not session:
        return json.dumps({"error": f"Failed to create session for path: {path}"}, ensure_ascii=False)

    logger.info("rlm_start: session=%s created for path=%s", session_id, resolved)

    # ПЕРЕД outer-try: иначе исключение в _scan_metadata (ниже) до присваивания
    # оставит idx_reader несвязанным и outer-except упадёт UnboundLocalError,
    # замаскировав исходную ошибку.
    idx_reader = None
    backend = None

    try:
        metadata = _scan_metadata(resolved) if include_metadata else {}

        # --- Try loading index FIRST (to enable fast-path startup) ---
        t_step = time.monotonic()
        idx_warnings: list[str] = []
        idx_stats: dict | None = None
        idx_status = None
        # True when a reader was opened but its stats came back as a zero/load-failure
        # sentinel (transient read of an EXISTING db mid-rebuild). Lets the no-stats branch
        # report "incomplete" (retry) rather than "missing" even if the marker was already
        # cleared by a finishing rebuild (codex Low).
        idx_load_failed = False
        # Computed BEFORE the try (round 27): the no-stats index_block branch below uses
        # db_path for index_incomplete(); get_index_db_path is a pure path construction.
        db_path = get_index_db_path(resolved)
        try:
            if db_path.exists():
                idx_status = check_index_usable(db_path, resolved)
                logger.info(
                    "rlm_start: session=%s index status=%s db=%s",
                    session_id,
                    idx_status.value,
                    db_path,
                )

                if idx_status in (IndexStatus.FRESH, IndexStatus.STALE_AGE, IndexStatus.STALE_CONTENT):
                    idx_reader = IndexReader(db_path)
                    idx_stats = idx_reader.get_statistics()
                    # Race guard (codex High): a rebuild may have set build_in_progress=1 and
                    # dropped tables between check_index_usable() above and now. get_statistics
                    # is _transient_safe → it returns ZERO_STATS (a TRUTHY dict), NOT an
                    # exception, so the loaded index_block branch would mislabel a partial
                    # index as loaded:true / index_status:"ok". Detect it two ways: the marker
                    # (build_in_progress=1) AND the timing-independent zero/load-failure
                    # sentinel (builder_version+built_at both None — the marker may have been
                    # cleared by a finishing rebuild before this check). Either → not-loaded →
                    # the no-stats branch reports loaded:false, index_status:"incomplete"/"missing".
                    if index_incomplete(db_path) or stats_indicate_load_failure(idx_stats):
                        try:
                            idx_reader.close()
                        except Exception:
                            pass
                        idx_reader = None
                        idx_stats = None
                        idx_load_failed = True  # existing db, transient/partial read → "incomplete"
                    else:
                        if idx_status == IndexStatus.STALE_AGE:
                            built_at = idx_stats.get("built_at")
                            age_days = int((time.time() - float(built_at)) / 86400) if built_at else "?"
                            idx_warnings.append(
                                f"Index is {age_days} days old — verify critical findings with live read_file()"
                            )
                        elif idx_status == IndexStatus.STALE_CONTENT:
                            idx_warnings.append(
                                "Index content may be outdated — run 'rlm-bsl-index index update' to refresh"
                            )
                        # Check index builder version
                        idx_version = int(idx_stats.get("builder_version") or 0)
                        if idx_version < BUILDER_VERSION:
                            msg = (
                                f"Индекс собран сборщиком v{idx_version}, текущий v{BUILDER_VERSION}. "
                                "Он продолжает работать, но содержит объявления и движения, взятые "
                                "из комментариев и строковых литералов. Следующий 'rlm-bsl-index "
                                f'index update "{resolved}"\' пересоберет индекс полностью — '
                                "это разовая длительная операция."
                            )
                            idx_warnings.append(msg)
                            logger.warning("rlm_start: session=%s %s", session_id, msg)
        except Exception as e:
            if idx_reader is not None:
                try:
                    idx_reader.close()
                except Exception:
                    pass
                idx_reader = None
            logger.warning("rlm_start: session=%s index load failed: %s", session_id, e)
        t_index = time.monotonic() - t_step

        # --- Format + extension detection (fast path from index or disk) ---
        startup_meta = None
        if idx_reader is not None and idx_status == IndexStatus.FRESH:
            startup_meta = idx_reader.get_startup_meta()

        if startup_meta is not None:
            # Fast path: reconstruct from cached index metadata
            t_step = time.monotonic()
            format_info = FormatInfo(
                primary_format=SourceFormat(startup_meta["source_format"]),
                root_path=resolved,
                bsl_file_count=int(startup_meta["shallow_bsl_count"]),
                has_configuration_xml=startup_meta.get("has_configuration_xml") == "1",
                metadata_categories_found=[],
            )
            t_format = time.monotonic() - t_step

            # Live extension scan (always fresh, <0.5s)
            t_step = time.monotonic()
            ext_context = detect_extension_context(resolved)
            t_ext = time.monotonic() - t_step

            t_step = time.monotonic()
            ext_overrides: dict[str, list[dict]] = _auto_scan_overrides(ext_context)
            t_overrides = time.monotonic() - t_step

            src_format = "index"
            src_ext = "live"
        else:
            # Disk path: full detection
            t_step = time.monotonic()
            format_info = detect_format(resolved)
            t_format = time.monotonic() - t_step

            t_step = time.monotonic()
            ext_context = detect_extension_context(resolved)
            t_ext = time.monotonic() - t_step

            # Auto-scan extension overrides (extensions are small, <1s)
            t_step = time.monotonic()
            ext_overrides = _auto_scan_overrides(ext_context)
            t_overrides = time.monotonic() - t_step

            src_format = "disk"
            src_ext = "disk"

            # Drift check: compare shallow counts (same methodology)
            if idx_reader is not None:
                _sm = idx_reader.get_startup_meta()
                stored_shallow = int(_sm["shallow_bsl_count"]) if _sm and _sm.get("shallow_bsl_count") else None
                if stored_shallow is not None and format_info.bsl_file_count:
                    drift = abs(format_info.bsl_file_count - stored_shallow) / max(stored_shallow, 1)
                    if drift > 0.05:
                        idx_warnings.append(
                            f"File count drift (shallow): index {stored_shallow}, "
                            f"disk {format_info.bsl_file_count} — "
                            "run 'rlm-bsl-index index build' if significant changes were made"
                        )

        logger.info(
            "rlm_start: session=%s format=%s shallow_bsl_files=%d config_role=%s overrides=%d",
            session_id,
            format_info.format_label,
            format_info.bsl_file_count,
            ext_context.current.role.value,
            sum(len(v) for v in ext_overrides.values()),
        )

        # Классификация формата сделана до создания сессии (v1.41.0) — здесь только
        # диагностика с уже известным session_id.
        if source_support is not SourceSupport.SUPPORTED:
            logger.warning(
                "rlm_start: session=%s unsupported source format: source_support=%s",
                session_id,
                source_support.value,
            )

        # Pre-import openai в фоне — только для inline: spawn-worker процесс
        # родительский прогрев всё равно не увидит (§12.1).
        if sandbox_mode == "inline" and os.environ.get("RLM_LLM_BASE_URL"):
            threading.Thread(target=warmup_openai_import, daemon=True).start()

        # Determine if index is authoritative for zero-callers results
        _callers_authoritative = idx_status == IndexStatus.FRESH and idx_reader is not None and idx_reader.has_calls

        t_step = time.monotonic()
        # Foundation-фильтр (v1.34.0): directory symlink/junction рядом с базой
        # может указывать ВНУТРЬ current root. Такой alias — не соседний
        # source-root, а второй путь к уже учтённому дереву: пропустив его в BSL
        # transport, мы получили бы либо topology-отказ штатного rlm_start, либо
        # двойной учёт тех же файлов. Публичный ext_context НЕ меняется —
        # index-builder и override-consumers видят прежний состав.
        _current_root_for_bsl = ext_context.current.path or resolved
        _nearby_for_bsl = (
            filter_alias_extension_infos(_current_root_for_bsl, ext_context.nearby_extensions)
            if ext_context.current.role == ConfigRole.MAIN
            else []
        )
        ext_paths_for_sandbox = [e.path for e in _nearby_for_bsl]
        ext_name_by_root = {e.path: e.name for e in _nearby_for_bsl if e.path and e.name}
        backend, parent_owns_reader = _create_session_backend(
            sandbox_mode=sandbox_mode,
            resolved=resolved,
            session=session,
            max_output_chars=max_output_chars,
            execution_timeout_seconds=execution_timeout_seconds,
            format_info=format_info,
            idx_reader=idx_reader,
            db_path=db_path,
            callers_authoritative=_callers_authoritative,
            ext_paths_for_sandbox=ext_paths_for_sandbox,
            registry_epoch=registry_epoch,
            enable_bsl_helpers=not generic_mode,
            current_config_role=ext_context.current.role.value,
            current_config_name=ext_context.current.name or "",
            current_config_root=_current_root_for_bsl,
            extension_name_by_root=ext_name_by_root,
        )
        _drain_startup_log_records(session_id, backend)
        if not parent_owns_reader:
            # inline: reader теперь во владении backend — не закрывать вторично.
            idx_reader = None
        elif idx_reader is not None:
            # process: worker открыл собственный read-only reader; временный
            # parent reader больше не нужен и закрывается сразу (§8.3).
            try:
                idx_reader.close()
            except Exception:
                pass
            idx_reader = None

        index_loaded = backend.index_loaded
        if sandbox_mode == "process" and idx_stats and not index_loaded:
            # Race parent-check → worker-open (§8.3): не заявлять loaded=true,
            # если worker сообщил обратное; index_block уйдёт в no-stats ветку
            # со статусом "incomplete" + retry warning.
            idx_stats = None
            idx_load_failed = True
            idx_warnings.append(
                backend.index_warning
                or "Index became unavailable during sandbox start — session continues in live/no-index mode"
            )

        has_llm_tools = backend.has_llm_tools
        t_sandbox = time.monotonic() - t_step
        logger.info(
            "rlm_start: session=%s sandbox ready, mode=%s gen=%d pid=%s llm_tools=%s index=%s",
            session_id,
            backend.mode,
            backend.generation,
            backend.worker_pid,
            has_llm_tools,
            index_loaded,
        )

        # Auto-detect custom prefixes — вычислены backend-ом (index fast path +
        # fallback-скан живут теперь рядом с namespace, §13.2).
        t_step = time.monotonic()
        detected_prefixes: list[str] = backend.detected_prefixes
        src_prefixes = backend.prefixes_source
        t_prefixes = time.monotonic() - t_step

        # v1.41.0: представление реестра сессии — имена от воркера, тексты из каталога
        # родителя. Оно же питает стратегию, available_functions и signatures.
        bsl_registry = _registry_view(backend.registry_names)
        # Какие подписи выдаются на старте. Весь каталог — в full, при
        # RLM_CATALOG_MODE=all и при выборе «весь каталог»; иначе ядро + выбранные
        # домены + подписи шагов строго совпавшего рецепта, если домен его темы не
        # выбран (иначе первый доменный шаг рецепта был бы вызовом вслепую).
        catalog_all = (
            strategy_mode == "full" or catalog_mode == "all" or bool(domain_choice and domain_choice.all_catalog)
        )
        if generic_mode or catalog_all:
            delivered = set(bsl_registry)
        else:
            choice = domain_choice or DomainChoice()
            wanted = set(HELPER_CORE) | domain_helper_names(choice.keys)
            recipe_topic = slim_recipe_topic(query)
            if recipe_topic and domain_of_topic(recipe_topic) not in choice.keys:
                wanted |= set(slim_recipe_step_helpers(recipe_topic, bsl_registry))
            delivered = wanted & set(bsl_registry)
        t_step = time.monotonic()
        if generic_mode:
            # BSL-хелперов в namespace нет — маршрутная карта не имеет права
            # ссылаться ни на один из них.
            strategy = build_generic_strategy(effort, has_llm_tools=has_llm_tools)
        else:
            strategy = get_strategy(
                effort,
                format_info,
                detected_prefixes,
                ext_context,
                ext_overrides,
                registry=bsl_registry,
                idx_stats=idx_stats,
                idx_warnings=idx_warnings,
                query=query,
                domain_choice=domain_choice,
                catalog_mode=catalog_mode,
            )
        t_strategy = time.monotonic() - t_step

        # Finding 4: если итоговые лимиты расходятся с пресетом effort (env-дефолт
        # или явный параметр тула), агент-facing текст «Limits:» в стратегии стал бы
        # ложным. Дописываем правдивый баннер в НАЧАЛО стратегии.
        if max_execute_calls != effort_config.max_execute_calls or max_llm_calls != effort_config.max_llm_calls:
            strategy = (
                "== SERVER LIMIT OVERRIDE ==\n"
                f"Effective this session: max_execute_calls={max_execute_calls}, "
                f"max_llm_calls={max_llm_calls}.\n"
                f"(These override the '{effort}' preset defaults shown in the EFFORT block below.)\n\n" + strategy
            )

        # Предупреждение о формате — САМОЕ первое в тексте стратегии, поэтому
        # prepend идёт ПОСЛЕ баннера лимитов (иначе баннер оказался бы выше).
        if source_support is SourceSupport.FOREIGN_WITH_BSL:
            strategy = UNSUPPORTED_FORMAT_SESSION_WARNING + "\n\n" + strategy

        # Состояние выдачи — ДО публикации сессии (v1.41.0): после неё его меняет
        # только _rlm_execute под execution_lock.
        session.registry_view = bsl_registry
        session.registry_generation = backend.generation
        session.catalog_all_at_start = catalog_all
        session.helper_domains = list(domain_choice.keys) if domain_choice is not None and not catalog_all else []
        session.delivered_helpers = set(delivered)

        # Публикация атомарна с проверкой владельца: TTL-эвикция и shutdown не
        # могут оставить backend без соответствующей живой Session (§13.3).
        if not _publish_session_backend(session_id, session, backend, registry_epoch):
            raise SandboxClosedError("session was revoked during initialization")
    except Exception as e:
        logger.error("rlm_start: session=%s failed: %s", session_id, e, exc_info=True)
        session_manager.end(session_id)
        if backend is not None:
            lifecycle_owns_backend = _failed_process_backend_has_lifecycle_owner(backend)
            # Pre-registration init failure — разрешённое исключение из
            # reaper-only правила (§13.3): execution lock ещё не задействован,
            # пользовательский код не выполнялся → bounded finish_close inline,
            # незавершённый residual уходит в reaper.
            if not lifecycle_owns_backend:
                _untrack_starting_backend(backend)
                try:
                    backend.request_close("start_failure")
                    report = backend.finish_close(time.monotonic() + kill_grace_seconds() + 2.0)
                    if report.residual:
                        _reaper.enqueue(backend)
                except Exception:
                    logger.warning("rlm_start: backend cleanup failed", exc_info=True)
                    _reaper.enqueue(backend)
        # idx_reader: жив только если backend его не принял (inline передаёт
        # владение backend-у и обнуляет ссылку; process закрывает сразу).
        if idx_reader is not None:
            try:
                idx_reader.close()
            except Exception:
                pass
        return json.dumps(
            {"error": f"Session init failed: {type(e).__name__}: {e}"},
            ensure_ascii=False,
        )

    # available_functions: подписи выданных BSL-хелперов в порядке реестра, затем
    # восемь файловых, затем LLM (если провайдер настроен). v1.41.0: в slim/domains —
    # ядро + выбранные домены, а не весь каталог.
    available_functions = [entry["sig"] for name, entry in bsl_registry.items() if name in delivered]
    available_functions.extend(_FILE_HELPER_SIGNATURES)
    if has_llm_tools:
        available_functions.extend(_LLM_HELPER_SIGNATURES)

    # Ключ domains — только в slim-сессии с BSL-хелперами: в full и generic ответ прежний.
    domains_key: dict | None = None
    if strategy_mode == "slim" and not generic_mode:
        if catalog_mode == "all":
            domains_key = DomainChoice(all_catalog=True).response_key()
        elif domain_choice is not None:
            domains_key = domain_choice.response_key()

    # На extreme-extension конфигах (напр. 155 расш) сериализация полного списка
    # расширений в ответ раздувала rlm_start выше токен-лимита. Усекаем агент-facing
    # поле до top-N по overrides; питание песочницы (ext_paths_for_sandbox) — полное.
    # Режем ТОЛЬКО ветку MAIN (как Site 1 _build_warnings и Site 3 _extension_strategy):
    # для EXTENSION/UNKNOWN-сессий nearby_extensions = соседи, их не усекаем (план).
    # v1.41.0: порог по режиму (5 в slim, 20 в full); явный RLM_EXT_LIST_CAP — он.
    if ext_context.current.role == ConfigRole.MAIN:
        shown_exts, ext_total, ext_shown = summarize_extensions_by_overrides(
            ext_context.nearby_extensions, ext_overrides, ext_list_display_cap()
        )
    else:
        shown_exts = list(ext_context.nearby_extensions)
        ext_total = ext_shown = len(shown_exts)

    # rlm_start.index carries a get_index_info()-shaped subset (PUBLIC key names —
    # builder_version + has_*/counts) so the agent does NOT need a separate
    # get_index_info() discovery call on start. Derivation mirrors get_index_info.
    if idx_stats:
        _bv = int(idx_stats.get("builder_version") or 0)
        _has_meta = bool(idx_stats.get("has_metadata"))
        index_block: dict = {
            "loaded": index_loaded,
            "index_check": "quick",
            "builder_version": _bv,
            "methods": idx_stats.get("methods"),
            "calls": idx_stats.get("calls"),
            "has_fts": idx_stats.get("has_fts", False),
            "has_synonyms": bool(idx_stats.get("object_synonyms", 0)),
            "object_synonyms": idx_stats.get("object_synonyms", 0),
            "has_object_attributes": _bv >= 11 and _has_meta,
            "object_attributes_count": idx_stats.get("object_attributes", 0),
            "has_predefined_items": _bv >= 11 and _has_meta,
            "predefined_items_count": idx_stats.get("predefined_items", 0),
            "has_form_elements": _bv >= 10 and _has_meta,
            "form_elements_count": idx_stats.get("form_elements", 0),
            "has_metadata_references": _bv >= 12 and (idx_stats.get("metadata_references") or 0) > 0,
            "metadata_references_count": idx_stats.get("metadata_references", 0),
            "has_metadata_code_usages": _bv >= 13,
            "metadata_code_usages_count": idx_stats.get("metadata_code_usages", 0),
            "config_name": idx_stats.get("config_name"),
            "config_version": idx_stats.get("config_version"),
            # Reader loaded → map the freshness status (FRESH→"ok"). The with-stats branch
            # is only reached for FRESH/STALE_AGE/STALE_CONTENT, so .get default is unused.
            "index_status": _INDEX_STATUS_LABELS.get(idx_status, "ok"),
            "warnings": idx_warnings,
        }
    else:
        # No index loaded — SAME key set with safe defaults so the payload shape is stable
        # (strategy/docs tell the agent to read these from rlm_start.index; a missing key
        # would break that or push agents back to a get_index_info() call).
        _incomplete_status = "incomplete" if (index_incomplete(db_path) or idx_load_failed) else "missing"
        if _incomplete_status == "incomplete":
            # v1.33.0: раньше здесь выставлялся только машинный index_status, а
            # idx_warnings оставался пустым — человек и агент об оборванной сборке
            # не узнавали. Текст без «ё» (ломает совпадение по ключевым словам).
            idx_warnings.append(
                "Предыдущая пересборка индекса не была завершена. Повторите "
                "'rlm-bsl-index index update' — это доведет сборку до конца."
            )
        elif source_support is SourceSupport.SUPPORTED:
            # v1.35.2 (#36): агент видел только loaded=false без причины и без пути.
            # Гейт на SUPPORTED обязателен: ветка "missing" достижима и для чужих
            # форматов, где сборка отвергается гейтом v1.32.0 — совет "соберите
            # индекс" там был бы вредным.
            #
            # Два текста, а не один: check_index_usable отдаёт MISSING и при
            # meta is None, то есть на СУЩЕСТВУЮЩЕМ, но нечитаемом файле. Сказать
            # про него "не найден" значит соврать про файл, который лежит на месте.
            #
            # Развилка берётся по stat(), а НЕ по exists(), и это существенно:
            # Path.exists() отвечает на нужный вопрос по-разному в разных версиях.
            # На 3.12 и 3.13 он ПЕРЕБРАСЫВАЕТ PermissionError (ERROR_ACCESS_DENIED
            # не входит в _IGNORED_WINERRORS), а с 3.14 делегирует в
            # os.path.exists() и на том же файле возвращает False — то есть
            # существующий, но нечитаемый индекс получил бы текст "не найден"
            # ровно на той версии, где сидят заявители. Граница именно 3.14:
            # тело Path.exists() в 3.12 и 3.13 побайтно одно и то же, замена на
            # os.path.exists() появляется только в 3.14 (сверено исходником
            # pathlib всех трёх версий; поведение проверено под icacls-deny).
            #
            # stat() не глотает OSError ни в одной версии, поэтому классификация
            # однозначна: FileNotFoundError — файла действительно нет; любой другой
            # OSError — файл есть, но недоступен, и это ВТОРОЙ случай, а не "нет".
            # Заодно сохраняется исходное требование: этот блок стоит ВНЕ try/except
            # самого _rlm_start (тот кончается выше), поэтому исключение обязано быть
            # погашено здесь — незащищённый вызов уронил бы тул там, где до правки
            # поднималась рабочая сессия со статусом missing.
            try:
                db_path.stat()
                _db_exists = True
            except FileNotFoundError:
                _db_exists = False
            except OSError:
                _db_exists = True
            if _db_exists:
                idx_warnings.append(
                    f"Индекс по пути {db_path} есть, но прочитать его не удалось: "
                    "файл поврежден или недоступен. Пересоберите: "
                    f"rlm_index(action='build', path='{resolved}')."
                )
            else:
                _root, _root_rule = describe_index_root()
                idx_warnings.append(
                    f"Индекс не найден: {db_path}. Корень индексов — {_root} "
                    f"({_root_rule}). Собрать: rlm_index(action='build', "
                    f"path='{resolved}') или в терминале 'rlm-bsl-index index "
                    f"build {resolved}'. Корень индексов определяется переменной "
                    "RLM_INDEX_DIR в конфигурации MCP-сервера."
                )
        index_block = {
            "loaded": index_loaded,
            "index_check": "quick",
            "builder_version": 0,
            "methods": None,
            "calls": None,
            "has_fts": False,
            "has_synonyms": False,
            "object_synonyms": 0,
            "has_object_attributes": False,
            "object_attributes_count": 0,
            "has_predefined_items": False,
            "predefined_items_count": 0,
            "has_form_elements": False,
            "form_elements_count": 0,
            "has_metadata_references": False,
            "metadata_references_count": 0,
            "has_metadata_code_usages": False,
            "metadata_code_usages_count": 0,
            "config_name": None,
            "config_version": None,
            # No reader: derive DIRECTLY (round 21/27) — NOT from a possibly-stale
            # idx_status. "incomplete" when build_in_progress=1 (reachable here both via
            # MISSING and the FRESH-but-reader-failed edge) OR when a reader was opened but
            # its stats were a transient zero/load-failure of an EXISTING db (idx_load_failed
            # — the marker may already be cleared by a finishing rebuild, so "missing" would
            # lie; "incomplete" signals retry — codex Low). Else "missing". index_incomplete
            # is None-safe → a non-existent db yields "missing".
            "index_status": _incomplete_status,
            "warnings": idx_warnings,
        }

    response: dict = {
        "session_id": session_id,
        "resolved_path": resolved,
        "warnings": _session_warnings(source_support, _start_extension_warnings(ext_context, ext_total, ext_shown)),
        "config_format": format_info.format_label,
        "source_support": source_support.value,
        "extension_context": {
            "is_extension": ext_context.current.role.value == "extension",
            "config_role": ext_context.current.role.value,
            "current_name": ext_context.current.name,
            "current_purpose": ext_context.current.purpose or None,
            "current_prefix": ext_context.current.name_prefix or None,
            "nearby_extensions": [
                {
                    "name": e.name,
                    "purpose": e.purpose,
                    "prefix": e.name_prefix,
                    # path остаётся АБСОЛЮТНЫМ — его потребляет find_ext_overrides(extension_path).
                    "path": e.path,
                    # Count only — full per-override detail via get_overrides('Object')
                    # (the inline dump was unused noise, ~30K on extension configs).
                    "overrides_count": len(ext_overrides.get(e.path, [])),
                }
                for e in shown_exts
            ],
            "nearby_main": (
                {"name": ext_context.nearby_main.name, "path": ext_context.nearby_main.path}
                if ext_context.nearby_main
                else None
            ),
            "own_overrides_count": (
                len(ext_overrides.get("self", [])) if ext_context.current.role.value == "extension" else None
            ),
        },
        "detected_custom_prefixes": detected_prefixes,
        "index": index_block,
        "metadata": metadata,
        "effective_effort": effort,
        "limits": {
            "max_llm_calls": session.max_llm_calls,
            "max_execute_calls": session.max_execute_calls,
            "execution_timeout_seconds": execution_timeout_seconds,
            # §16.2: агент/оператор обязаны видеть, включена ли процессная
            # изоляция — в inline hard-kill таймаута не гарантируется.
            "sandbox_mode": backend.mode,
        },
        **({"domains": domains_key} if domains_key is not None else {}),
        "available_functions": available_functions,
        "strategy": strategy,
    }
    if ext_shown < ext_total:
        # Soft-breaking: на N>cap конфигах nearby_extensions отдаёт только top-N.
        # Машинно-очевидный маркер усечения + указатель на полный список (F7).
        ec = response["extension_context"]
        ec["nearby_extensions_truncated"] = True
        ec["nearby_extensions_total"] = ext_total
        ec["nearby_extensions_shown"] = ext_shown
        ec["extensions_hint"] = (
            f"{ext_total} extensions; showing top {ext_shown} by overrides; detect_extensions() for full list"
        )
    if project_hint:
        response["project_hint"] = project_hint
    logger.info(
        "rlm_start: session=%s timings: format=%.1fs ext=%.1fs overrides=%.1fs index=%.1fs sandbox=%.1fs prefixes=%.1fs strategy=%.1fs",
        session_id,
        t_format,
        t_ext,
        t_overrides,
        t_index,
        t_sandbox,
        t_prefixes,
        t_strategy,
    )
    logger.info(
        "rlm_start: session=%s sources: format=%s ext=%s prefixes=%s",
        session_id,
        src_format,
        src_ext,
        src_prefixes,
    )
    # v1.41.0: тип выбора сессии — метрика нарезки доменов. query=<…> стоит последним
    # и подчиняется тому же выключателю RLM_LOG_EXECUTE_CODE, что и code=<…>.
    logger.info(
        "rlm_start: session=%s domains=<%s> ignored=<%s>%s",
        session_id,
        _session_domains_log_value(session),
        domain_choice.ignored_log_value() if domain_choice is not None else "-",
        _log_text_field("query", query),
    )
    result_json = json.dumps(response, ensure_ascii=False)
    out_chars = len(result_json)
    session.total_out_chars += out_chars
    logger.info(
        "rlm_start: session=%s mode=%s strategy_chars=%d completed in %.2fs out_chars=%d out_tokens~%d",
        session_id,
        get_strategy_mode(),
        len(strategy),
        time.monotonic() - t0,
        out_chars,
        int(out_chars / 1.75),
    )
    return result_json


def _format_helper_summary(helper_calls: list[HelperCall], threshold: float) -> tuple[str, int]:
    """Format helper calls for log. Returns (summary_string, notable_count)."""
    grouped: dict[str, list[float]] = {}
    for h in helper_calls:
        if h.elapsed >= threshold:
            grouped.setdefault(h.name, []).append(h.elapsed)
    parts = ", ".join(
        f"{name}({times[0]:.1f}s)" if len(times) == 1 else f"{name}({len(times)}\u00d7, total={sum(times):.1f}s)"
        for name, times in grouped.items()
    )
    return parts, len(grouped)


def _positive_int_env(name: str) -> int | None:
    """Положительный int из env или None (если не задан/невалиден)."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return None
    try:
        val = int(raw.strip())
    except ValueError:
        logger.warning("Игнорирую %s=%r: не целое число", name, raw)
        return None
    if val <= 0:
        logger.warning("Игнорирую %s=%d: должно быть > 0", name, val)
        return None
    return val


def resolve_session_limits(
    effort: str,
    query: str,
    max_llm_calls: int | None,
    max_execute_calls: int | None,
) -> tuple[str, int, int]:
    """Свести effort/лимиты.

    Выбор effort (по убыванию приоритета):
      1) RLM_FORCE_EFFORT (env) — жёсткий замок админа (по умолчанию НЕ задан);
      2) явный effort агента (low/medium/high/max);
      3) effort == "auto" (дефолт тула) → _auto_effort(query): medium, либо high
         по маркерам сложности запроса.
    Невалидный effort → medium. Числовые лимиты (по убыванию): ВАЛИДНЫЙ (>0) явный
    параметр тула > RLM_MAX_* (env) > пресет тира; невалидный explicit (≤0) игнор.
    Возвращает (effort, llm, execute) — все значения валидны (effort из EFFORT_LEVELS, лимиты >0).
    """
    forced = os.environ.get("RLM_FORCE_EFFORT", "").strip().lower()
    if forced and forced not in EFFORT_LEVELS:
        logger.warning("Игнорирую RLM_FORCE_EFFORT=%r: ожидается low|medium|high|max", forced)
    if forced in EFFORT_LEVELS:
        if forced != effort:
            logger.info("rlm_start: effort '%s' -> '%s' (RLM_FORCE_EFFORT)", effort, forced)
        effort = forced
    elif effort == "auto":
        effort = _auto_effort(query)  # 'medium' | 'high' по сложности запроса
        logger.info("rlm_start: effort='auto' -> '%s' (по запросу)", effort)
    elif effort not in EFFORT_LEVELS:
        effort = "medium"
    config = EFFORT_LEVELS[effort]

    # Числовые лимиты: валидный явный параметр (>0) > RLM_MAX_* (env) > пресет.
    # Невалидный explicit (None или ≤0) игнорируем — иначе max_execute_calls=0
    # создал бы сессию, мгновенно упирающуюся в лимит (server.py:719). Публичный
    # MCP Field имеет ge=1; эта проверка защищает прямые _rlm_start-вызовы.
    if max_execute_calls is None or max_execute_calls <= 0:
        max_execute_calls = _positive_int_env("RLM_MAX_EXECUTE_CALLS") or config.max_execute_calls
    if max_llm_calls is None or max_llm_calls <= 0:
        max_llm_calls = _positive_int_env("RLM_MAX_LLM_CALLS") or config.max_llm_calls

    return effort, max_llm_calls, max_execute_calls


# Default cap (chars) for the agent code echoed into the rlm_execute log line.
# A single-intent execute block (a few helper calls + print) is typically
# 100-400 chars; 300 captures most whole while still bounding runaway code.
_DEFAULT_EXECUTE_CODE_LOG_CAP = 300


def _log_text_field(name: str, text: str) -> str:
    """Return a `` <name>=<...>`` suffix with agent-supplied text for a log line.

    Controlled by ``RLM_LOG_EXECUTE_CODE`` (default: ON, capped):
      * unset / ``1`` / ``true`` / ``on`` / ``yes`` / ``all`` → default cap
        (``_DEFAULT_EXECUTE_CODE_LOG_CAP`` chars)
      * ``0`` / ``false`` / ``off`` / ``no`` → disabled (empty suffix)
      * positive integer ``N`` → cap at ``N`` chars (``<= 0`` → disabled)

    Newlines are flattened to ``⏎`` so the whole event stays one log line
    (grep-/parse-friendly). The log handlers are UTF-8, so Cyrillic in the text
    (object names) and the ``⏎``/``…`` markers are safe. The field is meant to be
    the LAST one on its line, so a parser can cut the user text off by its marker.
    """
    raw = os.environ.get("RLM_LOG_EXECUTE_CODE")
    cap = _DEFAULT_EXECUTE_CODE_LOG_CAP
    if raw is not None:
        v = raw.strip().lower()
        if v in ("0", "false", "off", "no"):
            return ""
        if v not in ("", "1", "true", "on", "yes", "all"):
            try:
                cap = int(v)
            except ValueError:
                cap = _DEFAULT_EXECUTE_CODE_LOG_CAP
            if cap <= 0:
                return ""
    flat = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "⏎")
    if len(flat) > cap:
        flat = flat[:cap] + "…"
    return f" {name}=<{flat}>"


def _execute_code_log_field(code: str) -> str:
    """Return a ``code=<...>`` suffix for the rlm_execute completion log line.

    The executed ``code`` IS the agent's query — helper calls with their
    parameters (``find_object("…")`` etc.). Logging it lets us later analyse
    which queries recur and where helpers could be improved. v1.41.0: the same
    switch (``RLM_LOG_EXECUTE_CODE``) and rules govern ``query=<…>`` of rlm_start.
    """
    return _log_text_field("code", code)


def _rlm_execute(
    session_id: str,
    code: str,
    detail_level: Literal["compact", "usage", "full"] = "compact",
    max_new_variables: int = 20,
    domains: list[str] | str | None = None,
) -> str:
    t0 = time.monotonic()
    logger.info("rlm_execute: session=%s code_len=%d", session_id, len(code))
    _cleanup_expired_resources()
    # Сильные локальные ссылки на session/backend до конца ответа (§9.1.2):
    # после снятия из registries активный execute завершает controlled response
    # на этих же объектах и не ищет их повторно.
    session = session_manager.get(session_id)
    if not session:
        return json.dumps({"error": f"Session '{session_id}' not found or expired"}, ensure_ascii=False)

    with _sandboxes_lock:
        backend = _sandboxes.get(session_id)
    if not backend:
        return json.dumps({"error": f"Sandbox not found for session '{session_id}'"}, ensure_ascii=False)

    # §9.1: два execute одной сессии строго последовательны. Глобальный
    # _sandboxes_lock на время выполнения НЕ держится (§3.5).
    with session.execution_lock:
        if not _session_backend_is_current(session_id, session, backend):
            return json.dumps(
                {"error": f"Session '{session_id}' was closed before execution (rlm_end/eviction/shutdown)"},
                ensure_ascii=False,
            )
        if session.execute_calls >= session.max_execute_calls:
            return json.dumps(
                {"error": (f"Execution call limit exceeded: {session.execute_calls} >= {session.max_execute_calls}")},
                ensure_ascii=False,
            )

        session.execute_calls += 1
        try:
            try:
                result = backend.execute(code)
            finally:
                # На успешном пути этот finally выполняется ДО _finish_rlm_execute,
                # поэтому startup-записи идут перед runtime-записями, а повторный
                # drain видит пустой read-once буфер.
                _drain_startup_log_records(session_id, backend)
        except SandboxClosedError:
            return json.dumps(
                {"error": f"Session '{session_id}' was closed during execution (rlm_end/eviction/shutdown)"},
                ensure_ascii=False,
            )
        except SandboxStartupError as e:
            # lazy restart не удался; backend остаётся dead — следующий execute
            # попробует новое поколение снова.
            logger.error("rlm_execute: session=%s sandbox restart failed: %s", session_id, e)
            return json.dumps(
                {
                    "error": f"Sandbox restart failed: {e}",
                    "sandbox_state": {"status": "dead", "restart": "on_next_execute"},
                },
                ensure_ascii=False,
            )
        except Exception as e:  # noqa: BLE001
            # Прежний Sandbox.execute() не бросал НИКОГДА (ловил всё внутри).
            # Backend добавил IPC/процессные пути, поэтому непредвиденная ошибка
            # инфраструктуры не должна превращаться в исключение уровня MCP —
            # сессия обязана получить controlled JSON-ошибку.
            logger.error("rlm_execute: session=%s backend failure", session_id, exc_info=True)
            return json.dumps(
                {"error": f"Sandbox backend failure: {type(e).__name__}: {e}"},
                ensure_ascii=False,
            )
        # Монотонная синхронизация LLM usage из backend (shared counter переживает
        # kill; accounting никогда не уменьшается по данным worker — §12.2.5).
        session.llm_calls_used = max(session.llm_calls_used, backend.llm_calls_used)
        return _finish_rlm_execute(session, backend, code, result, detail_level, max_new_variables, t0, domains)


def _finish_rlm_execute(session, backend, code, result, detail_level, max_new_variables, t0, domains=None) -> str:
    session_id = session.session_id
    # Runtime WARNING+ из воркера — ПОСЛЕ возврата валидированного результата, но ДО
    # сериализации public response. Параметризованный вызов логгера; в ответ агенту
    # строки не уезжают.
    for record in getattr(result, "log_records", None) or []:
        logger.warning("sandbox: session=%s %s", session_id, record)
    elapsed = time.monotonic() - t0
    # Log helper calls with timing (grouped by name)
    helpers_summary = ""
    if result.helper_calls:
        total = len(result.helper_calls)
        log_all = os.environ.get("RLM_LOG_HELPERS", "").lower() == "all"
        threshold = 0.0 if log_all else 0.1
        parts, notable_count = _format_helper_summary(result.helper_calls, threshold)
        if notable_count:
            helpers_summary = f" [{total} helpers: {parts}]"
        else:
            helpers_summary = f" [{total} helpers]"
    session.total_in_chars += len(code)

    response: dict = {
        "stdout": result.stdout,
        "error": result.error,
    }
    # v1.41.0: подписи, которых агент ещё не получал (попутная догрузка доменов,
    # первый вызов хелпера по имени, новые хелперы после перезапуска воркера).
    delivery, delivery_log = _deliver_signatures(session, backend, result, domains)
    response.update(delivery)

    if result.helper_calls:
        duplicates = [
            {
                "call": h.seq,
                "prev_call": h.duplicate_of,
                "helper": h.name,
            }
            for h in result.helper_calls
            if h.duplicate_of is not None
        ]
        if duplicates:
            response["duplicates"] = duplicates

    # Server-side efficiency nudges (session-cumulative, throttled). Response metadata
    # ONLY — never in the helper return or stdout. Stable ids: read_files/reuse_var/batch/
    # redundant_get_index_info.
    if result.efficiency_hints:
        response["efficiency_hints"] = result.efficiency_hints

    # Machine-readable маркер terminated/restarted (§10.6): только при аварии/
    # первом ответе нового поколения; обычные compact-ответы поле не несут.
    if result.sandbox_state:
        response["sandbox_state"] = result.sandbox_state
        # Namespace нового поколения начинается с нуля независимо от detail_level
        # аварийного ответа. Иначе compact/usage timeout оставлял snapshot старого
        # worker-а, и одноимённая переменная нового worker-а не считалась новой.
        if result.sandbox_state.get("state_lost"):
            session._last_reported_vars = set()

    if detail_level in {"usage", "full"}:
        response["usage"] = {
            "execute_calls_used": session.execute_calls,
            "execute_calls_remaining": session.max_execute_calls - session.execute_calls,
            "llm_calls_used": session.llm_calls_used,
        }

    if detail_level == "full":
        current_vars = set(result.variables)
        previous_vars = getattr(session, "_last_reported_vars", set())
        # Build excluded_vars from registry + static helpers. registry_names —
        # вычисляемое представление snapshot backend-а, не прямой _namespace (§13.2).
        excluded_vars = set(backend.registry_names) | {
            "_detected_prefixes",
            "_registry",
            "read_file",
            "read_files",
            "grep",
            "grep_summary",
            "grep_read",
            "glob_files",
            "tree",
            "find_files",
            "llm_query",
            "llm_query_batched",
        }
        new_vars = sorted(v for v in (current_vars - previous_vars) if v not in excluded_vars)
        session._last_reported_vars = current_vars

        response["variables"] = sorted(v for v in current_vars if v not in excluded_vars)
        response["total_variables"] = len(response["variables"])
        response["new_variables"] = new_vars[:max_new_variables]
        if len(new_vars) > max_new_variables:
            response["new_variables_truncated_count"] = len(new_vars) - max_new_variables

    result_json = json.dumps(response, ensure_ascii=False)
    out_chars = len(result_json)
    session.total_out_chars += out_chars
    hints_log = ""
    if result.efficiency_hints:
        hints_log = " hints=" + ",".join(h["id"] for h in result.efficiency_hints)
    # Bounded process-metadata (§13.4): PID/generation/reset — только server log,
    # payload IPC целиком не логируется.
    sandbox_log = f" mode={backend.mode} gen={result.generation}"
    state = result.sandbox_state
    if state:
        sandbox_log += f" sandbox_state={state.get('status')}:{state.get('reason')} hard_timeout={state.get('reason') == 'timeout'}"
    if backend.worker_pid is not None:
        sandbox_log += f" pid={backend.worker_pid}"
    # Поля выдачи подписей (domains+= / domains_ignored / outside=) стоят ПЕРЕД code=<…>,
    # который остаётся последним; от RLM_LOG_HELPERS они не зависят.
    logger.info(
        "rlm_execute: session=%s call=%d/%d error=%s elapsed=%.2fs out_chars=%d out_tokens~%d%s%s%s%s%s",
        session_id,
        session.execute_calls,
        session.max_execute_calls,
        bool(result.error),
        elapsed,
        out_chars,
        int(out_chars / 1.75),
        helpers_summary,
        hints_log,
        sandbox_log,
        delivery_log,
        _execute_code_log_field(code),
    )
    return result_json


def _log_session_end(session_id: str, session) -> None:
    """Итоговая строка ``rlm_end`` — после отцепления сессии.

    Новый execute после отцепления не проходит ``_session_backend_is_current`` и
    счётчиков не меняет. Занятый ``execution_lock`` значит, что execute, начатый до
    ``rlm_end``, ещё идёт (inline не прерывает запущенный код) и допишет счётчики
    после этой строки: она помечается ``in_flight=1``, вклад вызова — в его
    собственной строке ``rlm_execute``. Замок берётся только без ожидания.
    """
    in_flight = not session.execution_lock.acquire(blocking=False)
    try:
        total_chars = session.total_in_chars + session.total_out_chars
        # v1.41.0: итог выдачи подписей — метрика качества нарезки доменов.
        logger.info(
            "rlm_end: session=%s calls=%d in_chars=%d out_chars=%d total_chars=%d total_tokens~%d "
            "domains=<%s> added=%d outside=%d outside_unknown=%d%s",
            session_id,
            session.execute_calls,
            session.total_in_chars,
            session.total_out_chars,
            total_chars,
            int(total_chars / 1.75),
            _session_domains_log_value(session),
            session.domains_added,
            len(session.outside_helpers),
            session.outside_unknown_executes,
            " in_flight=1" if in_flight else "",
        )
    finally:
        if not in_flight:
            session.execution_lock.release()


def _rlm_end(session_id: str) -> str:
    session = session_manager.get(session_id)
    if not session:
        logger.info("rlm_end: session=%s (not found)", session_id)
    # Двухфазный идемпотентный teardown (§9.3): detach → неблокирующий
    # request_close → reaper. Session execution lock НЕ ждётся; success
    # возвращается, не дожидаясь kill_grace/join/освобождения SQLite handle.
    session_manager.end(session_id)
    _release_session_resources(session_id, reason="rlm_end")
    if session:
        _log_session_end(session_id, session)
    return json.dumps({"success": True}, ensure_ascii=False)


@mcp.tool()
async def rlm_start(
    query: str,
    # v1.41.0: схема ОДНА на все режимы и от окружения не зависит — поле в ней
    # необязательно (generic выясняется только по пути, режим из .env читается после
    # импорта). Обязательность для slim/BSL/domains проверяет сервер после
    # классификации пути, до создания сессии.
    domains: Annotated[
        list[str] | str | None,
        Field(description=domains_param_description()),
    ] = None,
    # Описания полей (задача 9 плана): только поведение, которого не видно в схеме, —
    # имя, тип, Literal, границы и дефолт агент читает из самой inputSchema.
    path: Annotated[
        str | None,
        Field(description="Корень конфигурации 1С или каталог-контейнер с ней."),
    ] = None,
    project: Annotated[str | None, Field(description="Имя из реестра (rlm_projects) — вместо path.")] = None,
    effort: Annotated[
        str,
        Field(description="auto|low|medium|high|max; auto — medium или high по запросу; итог — в effective_effort."),
    ] = "auto",
    max_output_chars: Annotated[int, Field(ge=100, le=100_000)] = 15_000,
    max_llm_calls: Annotated[int | None, Field(ge=1)] = None,
    max_execute_calls: Annotated[int | None, Field(ge=1)] = None,
    execution_timeout_seconds: Annotated[int, Field(ge=1, le=300)] = 45,
    include_metadata: Annotated[
        bool,
        Field(description="Счетчики файлов по типам; медленно на больших конфигурациях."),
    ] = False,
) -> str:
    """Opens analysis of 1C/BSL sources by project or path and query; for BSL in slim, choose domains. Then work via rlm_execute."""
    return await anyio.to_thread.run_sync(
        lambda: _rlm_start(
            path=path,
            query=query,
            effort=effort,
            max_output_chars=max_output_chars,
            max_llm_calls=max_llm_calls,
            max_execute_calls=max_execute_calls,
            execution_timeout_seconds=execution_timeout_seconds,
            include_metadata=include_metadata,
            project=project,
            domains=domains,
            require_domains=True,
        )
    )


@mcp.tool()
async def rlm_execute(
    session_id: str,
    code: Annotated[
        str,
        Field(description="Python с хелперами; связанные операции — одним вызовом."),
    ],
    detail_level: Annotated[
        Literal["compact", "usage", "full"],
        Field(description="usage — плюс счетчики вызовов, full — плюс переменные."),
    ] = "compact",
    max_new_variables: Annotated[int, Field(ge=1, le=200)] = 20,
    domains: Annotated[
        list[str] | str | None,
        Field(description=_EXECUTE_DOMAINS_DESCRIPTION),
    ] = None,
) -> str:
    """Runs Python with helpers in the session: output via print(), variables persist. Signatures of helpers not seen yet arrive in the signatures key."""
    return await anyio.to_thread.run_sync(
        lambda: _rlm_execute(session_id, code, detail_level, max_new_variables, domains)
    )


@mcp.tool()
async def rlm_end(
    session_id: str,
) -> str:
    """Closes the session and frees its resources."""
    return await anyio.to_thread.run_sync(lambda: _rlm_end(session_id))


# ─────────────────────────────────────────────────────────────────────
#                          rlm_help (slim mode)
# ─────────────────────────────────────────────────────────────────────


def _help_signatures(names, snapshot: dict) -> dict:
    """Подписи хелперов для справки без состояния сессии: тексты из статического
    каталога, ``git_search`` помечается условным (таблица статична, реестр живой)."""
    out: dict = {"signatures": [snapshot[name]["sig"] for name in names]}
    conditional = {name: note for name, note in CONDITIONAL_HELPERS.items() if name in names}
    if conditional:
        out["conditional"] = conditional
    return out


def _rlm_help_dispatch(
    topic: str | None = None,
    helpers: list[str] | None = None,
    category: str | None = None,
    section: str | None = None,
    format: str = "compact",
    include_code: bool = True,
    domain: list[str] | str | None = None,
) -> str:
    """Dispatch ``rlm_help`` arguments to one of seven modes (see table below)
    and return a JSON string. Pure function: does not touch the active session
    and uses the cached static helper-metadata snapshot.

    Mode priority (top-down — first match wins, later args ignored with a
    warning attached to the JSON response):

    1. all-empty            → menu        (topics/categories/sections/domains/helper count)
    2. domain given         → domain      (signatures of helper domains, no core; v1.41.0)
    3. topic given          → topic       (recipe via _match_recipe; alias-aware; + signatures
                                           of the helpers its returned steps mention)
    4. section=='disambiguation' → disambiguation (filtered by `helpers` if given)
    5. section given        → section     (raw text)
    6. helpers given        → helpers     (per-helper details, optional category filter)
    7. category given       → category    (one-line per helper in that category)

    Подписи, прочитанные через справку, сервер выданными НЕ считает: справка без
    состояния, иначе ей нужны были бы сессия и замок против параллельного execute.
    """
    from rlm_tools_bsl.bsl_helpers import build_helper_metadata_snapshot

    snapshot = build_helper_metadata_snapshot()
    warnings: list[str] = []

    def _emit(other_args: dict, kept: str) -> None:
        for arg_name, arg_val in other_args.items():
            if arg_val:
                warnings.append(f"argument '{arg_name}' ignored when '{kept}' is given")

    # Mode 1: menu
    if not (topic or helpers or category or section or domain):
        result = {
            "available_topics": list_topics(),
            "available_categories": list_categories(),
            "available_sections": list_sections(),
            "available_domains": {key: d["label"] for key, d in HELPER_DOMAINS.items()} | {ALL_CATALOG: "все подписи"},
            "helpers_count": len(snapshot),
            "hint": (
                "rlm_help(topic='проведение'|'печать'|'обмен'|...) → recipe for a topic. "
                "rlm_help(domain='код') → signatures of a helper domain. "
                "rlm_help(category='discovery'|'code'|...) → list helpers in a category. "
                "rlm_help(helpers=['name1','name2']) → details. "
                "rlm_help(section='workflow'|'disambiguation'|'performance'|'batching'|'io'|'critical'). "
                "Не уверен, что означают source/owner/extensions_included/total_exact/partial/"
                "index_coverage/truncated/scope → rlm_help(section='coverage')."
            ),
        }
        return json.dumps({"mode": "menu", "result": result, "warnings": warnings}, ensure_ascii=False)

    # Mode 2: domain (v1.41.0) — подписи доменов хелперов без ядра, их темы рецептов.
    if domain:
        _emit({"topic": topic, "helpers": helpers, "category": category, "section": section}, "domain")
        choice = normalize_domains(domain)
        if not choice.recognized:
            return json.dumps(
                {
                    "mode": "domain",
                    "result": {"error": "unknown", "allowed": list(ALLOWED_DOMAIN_VALUES), **choice.ignored_summary()},
                    "warnings": warnings,
                },
                ensure_ascii=False,
            )
        keys = list(HELPER_DOMAINS) if choice.all_catalog else list(choice.keys)
        wanted = domain_helper_names(keys)
        names = [name for name in snapshot if name in wanted and name not in HELPER_CORE]
        result = {
            "domains": choice.selected(),
            "topics": {key: list(HELPER_DOMAINS[key]["topics"]) for key in keys if HELPER_DOMAINS[key]["topics"]},
            **_help_signatures(names, snapshot),
        }
        if choice.ignored_total:
            result.update(choice.ignored_summary())
            result["allowed"] = list(ALLOWED_DOMAIN_VALUES)
        return json.dumps({"mode": "domain", "result": result, "warnings": warnings}, ensure_ascii=False)

    # Mode 3: topic
    if topic:
        _emit({"helpers": helpers, "category": category, "section": section}, "topic")
        recipe = _get_topic_recipe(topic, format=format, include_code=include_code)
        if recipe is None:
            suggestions = _fuzzy_suggest(topic, list_topics(), top_n=3)
            return json.dumps(
                {
                    "mode": "topic",
                    "result": {"topic": topic, "error": "unknown", "suggestions": suggestions},
                    "warnings": warnings,
                },
                ensure_ascii=False,
            )
        # v1.41.0: подписи хелперов, упомянутых в ВОЗВРАЩЁННЫХ шагах и code_hint, —
        # контракт до вызова, в каком бы домене агент ни начал сессию.
        text = "\n".join([*recipe["steps"], recipe.get("code_hint") or ""])
        recipe.update(_help_signatures(recipe_mentions(text, recipe["topic"], snapshot), snapshot))
        return json.dumps({"mode": "topic", "result": recipe, "warnings": warnings}, ensure_ascii=False)

    # Mode 4: section=='disambiguation' (structured array)
    if section == "disambiguation":
        _emit({"category": category}, "section='disambiguation'")
        pairs = _get_disambiguation(filter_helpers=helpers)
        return json.dumps(
            {"mode": "disambiguation", "result": pairs, "warnings": warnings},
            ensure_ascii=False,
        )

    # Mode 5: section
    if section:
        _emit({"helpers": helpers, "category": category}, f"section='{section}'")
        try:
            text = _get_section(section)
        except (KeyError, ValueError):
            return json.dumps(
                {
                    "mode": "section",
                    "result": {
                        "section": section,
                        "error": "unknown",
                        "available": list_sections(),
                    },
                    "warnings": warnings,
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {"mode": "section", "result": {"section": section, "text": text}, "warnings": warnings},
            ensure_ascii=False,
        )

    # Mode 6: helpers (optional category filter — AND, drops mismatches silently from result)
    if helpers:
        items: list[dict] = []
        dropped_by_category: list[tuple[str, str]] = []
        for name in helpers:
            details = _get_helper_details(name, snapshot)
            if details is None:
                items.append(
                    {
                        "name": name,
                        "error": "unknown",
                        "suggestions": _fuzzy_suggest(name, list(snapshot.keys()), top_n=3),
                    }
                )
                continue
            if category and details["category"] != category:
                dropped_by_category.append((name, details["category"]))
                continue
            items.append(details)
        if dropped_by_category:
            names_part = ", ".join(f"{n} (category='{c}')" for n, c in dropped_by_category)
            warnings.append(f"helpers dropped — not in requested category '{category}': {names_part}")
        return json.dumps({"mode": "helpers", "result": items, "warnings": warnings}, ensure_ascii=False)

    # Mode 7: category
    if category:
        cats = list_categories()
        if category not in cats:
            return json.dumps(
                {
                    "mode": "category",
                    "result": {
                        "category": category,
                        "error": "unknown",
                        "available": cats,
                    },
                    "warnings": warnings,
                },
                ensure_ascii=False,
            )
        cat_helpers = _get_category_helpers(category, snapshot)
        return json.dumps(
            {
                "mode": "category",
                "result": {"category": category, "helpers": cat_helpers},
                "warnings": warnings,
            },
            ensure_ascii=False,
        )

    # Should be unreachable — keep a defensive return for type-checkers.
    return json.dumps({"mode": "menu", "result": {"hint": "no input"}, "warnings": warnings}, ensure_ascii=False)


# Registered as an MCP tool only in slim mode. In RLM_STRATEGY_MODE=full the
# tool list does not include rlm_help — agents see the legacy strategy with
# all rules inlined and no extra tool to call.
#
# v1.41.0: функция определена НЕЗАВИСИМО от режима, а регистрацию согласует
# `_sync_rlm_help_registration()`: при импорте — по окружению процесса (как раньше),
# и ещё раз в `main()` сразу после загрузки `.env` — иначе режим, заданный только в
# `.env`, менял бы стратегию, но не список тулов. Публичное имя модуля `rlm_help`
# появляется и исчезает ВМЕСТЕ с регистрацией: реестр FastMCP и атрибут модуля не
# расходятся (это проверяет test_v1_34_0.py::test_help_tool_is_registered_only_in_slim).
async def _rlm_help_tool(
    topic: Annotated[
        str | None,
        Field(description="Тема или алиас (список — в меню)."),
    ] = None,
    helpers: Annotated[
        list[str] | None,
        Field(description="Имена: подпись и рецепт."),
    ] = None,
    category: Literal["discovery", "code", "xml", "composite", "business", "extension", "navigation"] | None = None,
    section: Annotated[
        Literal["workflow", "disambiguation", "performance", "batching", "io", "critical", "coverage"] | None,
        Field(description="disambiguation с helpers=[a, b] — одна пара; coverage — как читать полноту ответа."),
    ] = None,
    format: Annotated[
        Literal["compact", "full"],
        Field(description="Для темы: full — 7–9 шагов и code_hint."),
    ] = "compact",
    include_code: bool = True,
    domain: Annotated[
        list[str] | str | None,
        Field(description="Ключи как у rlm_start.domains: подписи домена без ядра. Сессию не меняет."),
    ] = None,
) -> str:
    """On-demand help: topic recipe, helper contract, helper domain or strategy section. No arguments — menu."""
    out = await anyio.to_thread.run_sync(
        lambda: _rlm_help_dispatch(
            topic=topic,
            helpers=helpers,
            category=category,
            section=section,
            format=format,
            include_code=include_code,
            domain=domain,
        )
    )
    try:
        parsed = json.loads(out)
        mode = parsed.get("mode", "?")
        warnings_count = len(parsed.get("warnings", []) or [])
    except Exception:
        mode = "?"
        warnings_count = 0
    helpers_count = len(helpers) if helpers else 0
    domains_log = ""
    if domain:
        choice = normalize_domains(domain)
        domains_log = f" domains=<{choice.log_value() if choice.recognized else '-'}>"
    logger.info(
        "rlm_help: mode=%s topic=%s category=%s section=%s helpers=%d format=%s out_chars=%d warnings=%d%s",
        mode,
        topic,
        category,
        section,
        helpers_count,
        format,
        len(out),
        warnings_count,
        domains_log,
    )
    return out


# FastMCP называет модель аргументов по имени функции (`<имя>Arguments`): без этого
# в схеме тула оказался бы заголовок `_rlm_help_toolArguments`.
_rlm_help_tool.__name__ = _rlm_help_tool.__qualname__ = "rlm_help"


def _sync_rlm_help_registration() -> bool:
    """Согласовать наличие тула ``rlm_help`` с итоговым режимом стратегии (v1.41.0).

    Идемпотентно; возвращает, зарегистрирован ли тул после вызова. До запуска
    транспорта ``add_tool`` добавляет имя в ``list_tools()``, а ``remove_tool`` его
    убирает (проверено на установленном FastMCP). Признак регистрации — публичное
    имя модуля: оно и реестр FastMCP меняются только здесь и только вместе.
    """
    want = get_strategy_mode() == "slim"
    has = "rlm_help" in globals()
    if want and not has:
        mcp.add_tool(_rlm_help_tool, name="rlm_help")
        globals()["rlm_help"] = _rlm_help_tool
    elif has and not want:
        try:
            mcp.remove_tool("rlm_help")
        except Exception:  # pragma: no cover - тула уже нет в реестре
            pass
        globals().pop("rlm_help", None)
    return want


# При импорте — по окружению процесса (прямой импорт в тестах и встраивании).
_sync_rlm_help_registration()


@mcp.tool()
async def rlm_projects(
    action: Literal["list", "add", "remove", "rename", "update"],
    name: Annotated[str | None, Field(description="Для add/remove/rename/update.")] = None,
    path: Annotated[
        str | None,
        Field(description="Корень конфигурации или каталог-контейнер (для add/update)."),
    ] = None,
    description: str | None = None,
    new_name: str | None = None,
    password: Annotated[
        str | None,
        Field(
            description=(
                "Пароль проекта: add задает, остальные изменения подтверждают. Не выдумывай — спроси у пользователя."
            )
        ),
    ] = None,
    clear_password: Annotated[
        bool, Field(description="Снять пароль: изменения через MCP закроются до нового.")
    ] = False,
) -> str:
    """Finds or changes registered projects (name → source path). Changes require the project password — ask the user for it."""

    # === MCP password enforcement ===

    logger.info(
        "rlm_projects: action=%s name=%s password=%s clear_password=%s",
        action,
        name,
        "***" if password else None,
        clear_password,
    )

    if action == "add":
        if not name:
            return json.dumps({"error": "name is required for 'add'"}, ensure_ascii=False)
        if not path:
            return json.dumps({"error": "path is required for 'add'"}, ensure_ascii=False)
        if not password:
            payload: dict = {
                "approval_required": True,
                "action": "add",
                "name": name,
                "path": path,
                "message": "Для регистрации проекта необходим пароль. "
                "Ask the user for a project password. "
                "Do NOT invent the password yourself.",
            }
            if description is not None:
                payload["description"] = description
            return json.dumps(payload, ensure_ascii=False)
        # password provided → fall through to _rlm_projects

    if action in ("remove", "update", "rename"):
        if not name:
            return json.dumps({"error": f"name is required for '{action}'"}, ensure_ascii=False)
        if action == "rename" and not new_name:
            return json.dumps({"error": "new_name is required for 'rename'"}, ensure_ascii=False)

        from rlm_tools_bsl.projects import RegistryCorruptedError, get_registry

        try:
            reg = get_registry()
            matches, method = reg.resolve(name)
        except RegistryCorruptedError as exc:
            return json.dumps(
                {"error": f"Registry file is corrupted: {exc}. Run rlm_projects(action='list') after fixing the file."},
                ensure_ascii=False,
            )

        if not matches:
            all_projects = reg.list_projects()
            available = [{"name": p["name"], "description": p.get("description", "")} for p in all_projects]
            return json.dumps(
                {"error": f"Project not found: {name}", "available_projects": available},
                ensure_ascii=False,
            )
        if len(matches) > 1:
            ambiguous = [{"name": m["name"], "description": m.get("description", "")} for m in matches]
            return json.dumps(
                {"error": f"Ambiguous project name: {name}", "matches": ambiguous},
                ensure_ascii=False,
            )
        if method == "fuzzy":
            return json.dumps(
                {"error": f"Did you mean '{matches[0]['name']}'?"},
                ensure_ascii=False,
            )

        # Exact or unique substring → single match
        project_name = matches[0]["name"]
        name = project_name  # override for exact-match CRUD in _rlm_projects
        has_pwd = reg.has_password(project_name)

        # --- Password enforcement ---
        # By design: legacy projects (no password) get a single generic
        # "set_password" response for ALL mutations except password-only
        # bootstrap.  This covers retargeting too: update(password="X",
        # path="/evil") on a legacy project hits the else-branch and
        # returns approval_required instead of silently applying the
        # path change.  A separate "set password first, then update"
        # error was considered (plan R4-1) but dropped — real-world
        # testing showed models correctly interpret "set_password" and
        # do the bootstrap in a separate call.
        if not has_pwd:
            if action == "update" and password and path is None and description is None and not clear_password:
                # Legacy bootstrap: password-only update sets initial password
                # Fall through to _rlm_projects
                pass
            else:
                return json.dumps(
                    {
                        "approval_required": True,
                        "action": "set_password",
                        "project": project_name,
                        "message": "У проекта не задан пароль. "
                        "Project has no password configured. "
                        "Ask the user what password to set for this project. "
                        "Do NOT invent or guess the password.",
                    },
                    ensure_ascii=False,
                )
        elif not password or not reg.verify_password(project_name, password):
            # Reaches here only when has_pwd=True (blocks above handle has_pwd=False)
            # Detect password change attempt: wrong password + no other mutations
            if action == "update" and password and path is None and description is None and not clear_password:
                return json.dumps(
                    {
                        "error": "Неверный пароль. Запросите у пользователя правильный текущий пароль проекта. "
                        "Wrong password. Ask the user for the correct CURRENT project password. "
                        "Do NOT guess or reuse passwords from other projects."
                    },
                    ensure_ascii=False,
                )
            # Build approval_required payload with all non-secret params
            payload = {
                "approval_required": True,
                "action": action,
                "project": project_name,
                "message": "Введите текущий пароль проекта для подтверждения. "
                "Ask the user for their CURRENT project password. "
                "Do NOT invent the password yourself.",
            }
            if action == "rename" and new_name:
                payload["new_name"] = new_name
            if action == "update":
                if path is not None:
                    payload["path"] = path
                if description is not None:
                    payload["description"] = description
                if clear_password:
                    payload["clear_password"] = True
            return json.dumps(payload, ensure_ascii=False)
        else:
            # Password verified → consumed for auth, not passed to _rlm_projects
            password = None

        # Fall through to _rlm_projects

    return await anyio.to_thread.run_sync(
        lambda: _rlm_projects(
            action=action,
            name=name,
            path=path,
            description=description,
            new_name=new_name,
            password=password,
            clear_password=clear_password,
        )
    )


def _rlm_projects(
    action: str,
    name: str | None = None,
    path: str | None = None,
    description: str | None = None,
    new_name: str | None = None,
    password: str | None = None,
    clear_password: bool = False,
) -> str:
    from rlm_tools_bsl.projects import RegistryCorruptedError, get_registry

    try:
        reg = get_registry()

        # Translate host paths to container paths (Docker)
        if path:
            path = _resolve_path_map(path)

        # Resolve mapped drives (Windows service in Session 0)
        if path and not os.path.isdir(path):
            unc = _resolve_mapped_drive(path)
            if unc:
                path = str(pathlib.Path(unc).resolve())

        if action == "list":
            return json.dumps({"projects": reg.list_projects()}, ensure_ascii=False)

        # For add/update: validate container-style paths by running the same
        # normalization as rlm_start/rlm_index. Save the original path as given
        # by the user (post path_map/mapped-drive translation only) so users see
        # their own path in rlm_projects list.
        if action in ("add", "update") and path:
            _effective, err_json = _normalize_and_validate_path(path)
            if err_json is not None:
                return err_json

        if action == "add":
            if not name:
                return json.dumps({"error": "name is required for 'add'"}, ensure_ascii=False)
            if not path:
                return json.dumps({"error": "path is required for 'add'"}, ensure_ascii=False)
            entry = reg.add(name, path, description or "", password=password)
            return json.dumps({"added": entry}, ensure_ascii=False)

        if action == "remove":
            if not name:
                return json.dumps({"error": "name is required for 'remove'"}, ensure_ascii=False)
            entry = reg.remove(name)
            return json.dumps({"removed": entry}, ensure_ascii=False)

        if action == "rename":
            if not name:
                return json.dumps({"error": "name is required for 'rename'"}, ensure_ascii=False)
            if not new_name:
                return json.dumps({"error": "new_name is required for 'rename'"}, ensure_ascii=False)
            entry = reg.rename(name, new_name)
            return json.dumps({"renamed": entry}, ensure_ascii=False)

        if action == "update":
            if not name:
                return json.dumps({"error": "name is required for 'update'"}, ensure_ascii=False)
            entry = reg.update(
                name, path=path, description=description, password=password, clear_password=clear_password
            )
            return json.dumps({"updated": entry}, ensure_ascii=False)

        return json.dumps({"error": f"Unknown action: {action}"}, ensure_ascii=False)

    except RegistryCorruptedError as exc:
        return json.dumps(
            {"error": f"Registry file is corrupted: {exc}. Run rlm_projects(action='list') after fixing the file."},
            ensure_ascii=False,
        )
    except (ValueError, KeyError) as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False)


@mcp.tool()
async def rlm_index(
    action: Literal["build", "update", "info", "drop"],
    path: Annotated[
        str | None,
        Field(description="Только для info; build/update/drop — через project."),
    ] = None,
    project: str | None = None,
    no_calls: bool = False,
    no_metadata: bool = False,
    no_fts: bool = False,
    no_synonyms: bool = False,
    confirm: Annotated[
        str | None,
        Field(description="Пароль проекта для build/update/drop — спроси у пользователя."),
    ] = None,
) -> str:
    """Builds, updates, shows or drops the index; build and update run in the background, status via info. Changes require a registered project and its password."""
    logger.info(
        "rlm_index: action=%s project=%s path=%s confirm=%s",
        action,
        project,
        path,
        "***" if confirm else None,
    )

    if action in ("build", "update", "drop"):
        from rlm_tools_bsl.projects import RegistryCorruptedError, get_registry

        # MCP: path запрещён для admin-действий
        if path is not None:
            return json.dumps(
                {
                    "error": f"STOP! Path '{path}' is NOT a registered project! "
                    f"Action '{action}' requires a registered project with password. "
                    "You MUST register the project first! "
                    "Tell the user: this path is not in the project list and needs to be registered. "
                    "Ask: 'Этого проекта нет в списке. Зарегистрировать его?' "
                    "Then use: rlm_projects(action='add', name='...', path='...', password='...'). "
                    "Do NOT ask for a password yet — first confirm with the user!"
                },
                ensure_ascii=False,
            )

        if not project:
            return json.dumps(
                {"error": f"Action '{action}' requires project=... (registered project with password)."},
                ensure_ascii=False,
            )

        # Resolve project name
        try:
            reg = get_registry()
            matches, method = reg.resolve(project)
        except RegistryCorruptedError as exc:
            return json.dumps(
                {"error": f"Registry file is corrupted: {exc}. Run rlm_projects(action='list') after fixing the file."},
                ensure_ascii=False,
            )
        if not matches:
            return json.dumps({"error": f"Project not found: {project}"}, ensure_ascii=False)
        if len(matches) > 1:
            names = [m["name"] for m in matches]
            return json.dumps({"error": f"Ambiguous project: {names}"}, ensure_ascii=False)
        if method == "fuzzy":
            return json.dumps({"error": f"Did you mean '{matches[0]['name']}'?"}, ensure_ascii=False)

        project_name = matches[0]["name"]

        # Password check
        if not reg.has_password(project_name):
            return json.dumps(
                {
                    "approval_required": True,
                    "action": "set_password",
                    "project": project_name,
                    "message": "У проекта не задан пароль. "
                    "Project has no password configured. "
                    "Ask the user what password to set for this project. "
                    "Do NOT invent or guess the password.",
                },
                ensure_ascii=False,
            )

        if not confirm or not reg.verify_password(project_name, confirm):
            return json.dumps(
                {
                    "approval_required": True,
                    "action": action,
                    "project": project_name,
                    "message": "Введите пароль проекта для подтверждения управления индексами. "
                    "Ask the user for their project password. Do NOT proceed without it.",
                },
                ensure_ascii=False,
            )

        # Password correct — proceed with project (not path)

        if action in ("build", "update"):
            resolved_path, err_json = _normalize_and_validate_path(matches[0]["path"])
            if err_json is not None:
                return err_json

            # Единственная MCP-точка гейта чужих форматов (v1.32.0): только новое
            # построение индекса и только до регистрации фоновой job — иначе
            # отказ оставил бы за собой висящий job-слот. `update` не гейтится:
            # без индекса builder.update() и так падает FileNotFoundError.
            if action == "build":
                gate_error = await anyio.to_thread.run_sync(lambda: _unsupported_format_build_error(resolved_path))
                if gate_error is not None:
                    logger.warning("rlm_index: build refused on unsupported format: path=%s", resolved_path)
                    return gate_error

            job_key = resolved_path

            with _build_jobs_lock:
                # Cleanup stale completed jobs (>1h)
                now = time.time()
                stale = [
                    k
                    for k, v in _build_jobs.items()
                    if v["status"] != "building" and v.get("finished_at") and now - v["finished_at"] > 3600
                ]
                for k in stale:
                    del _build_jobs[k]

                existing = _build_jobs.get(job_key)
                if existing and existing["status"] == "building":
                    elapsed = now - existing["started_at"]
                    return json.dumps(
                        {
                            "error": f"Build/update already in progress for '{project_name}' "
                            f"({elapsed:.0f}s elapsed). "
                            "Check status: rlm_index(action='info', project='...')",
                        },
                        ensure_ascii=False,
                    )
                _build_jobs[job_key] = {
                    "status": "building",
                    "action": action,
                    "project": project_name,
                    "started_at": now,
                    "finished_at": None,
                    "result": None,
                    "error": None,
                }

            def _bg() -> None:
                try:
                    result_json = _rlm_index(
                        action=action,
                        path=None,
                        project=project_name,
                        no_calls=no_calls,
                        no_metadata=no_metadata,
                        no_fts=no_fts,
                        no_synonyms=no_synonyms,
                    )
                    parsed = json.loads(result_json)
                    with _build_jobs_lock:
                        job = _build_jobs.get(job_key)
                        if job is None:
                            return
                        if "error" in parsed:
                            job["status"] = "error"
                            job["finished_at"] = time.time()
                            job["error"] = parsed["error"]
                        else:
                            job["status"] = "done"
                            job["finished_at"] = time.time()
                            job["result"] = parsed
                except Exception as exc:
                    with _build_jobs_lock:
                        job = _build_jobs.get(job_key)
                        if job is None:
                            return
                        job["status"] = "error"
                        job["finished_at"] = time.time()
                        job["error"] = str(exc)

            threading.Thread(target=_bg, daemon=False, name=f"build-{project_name}").start()
            return json.dumps(
                {
                    "started": True,
                    "action": action,
                    "project": project_name,
                    "message": f"{'Построение' if action == 'build' else 'Обновление'} индекса запущено в фоне. "
                    "Проверьте статус через rlm_index(action='info', project='...'). "
                    "Check status with rlm_index(action='info', project='...').",
                },
                ensure_ascii=False,
            )

        if action == "drop":
            resolved_path, err_json = _normalize_and_validate_path(matches[0]["path"])
            # Issue #16: a deleted source dir must not block dropping the index.
            # If the path no longer resolves, no build can be in progress for it,
            # so skip the in-progress guard and let _rlm_index recover the index
            # by its stored metadata. Only genuine in-progress builds are blocked.
            if err_json is None:
                with _build_jobs_lock:
                    job = _build_jobs.get(resolved_path)
                    if job and job["status"] == "building":
                        return json.dumps(
                            {
                                "error": f"Cannot drop: build/update in progress for '{project_name}'. "
                                "Wait for it to finish or restart the server.",
                            },
                            ensure_ascii=False,
                        )

    return await anyio.to_thread.run_sync(
        lambda: _rlm_index(
            action=action,
            path=path,
            project=project,
            no_calls=no_calls,
            no_metadata=no_metadata,
            no_fts=no_fts,
            no_synonyms=no_synonyms,
        )
    )


def _rlm_index(
    action: str,
    path: str | None = None,
    project: str | None = None,
    no_calls: bool = False,
    no_metadata: bool = False,
    no_fts: bool = False,
    no_synonyms: bool = False,
) -> str:
    from rlm_tools_bsl.projects import RegistryCorruptedError, get_registry
    from rlm_tools_bsl.bsl_index import IndexBuilder, IndexReader, get_index_db_path
    from rlm_tools_bsl.cache import touch_project_cache

    # --- Resolve path ---
    if path is None and project is None:
        return json.dumps({"error": "Either 'path' or 'project' must be provided"}, ensure_ascii=False)

    resolved_project_name: str | None = None
    if path is None:
        try:
            reg = get_registry()
            matches, method = reg.resolve(project)  # type: ignore[arg-type]
        except RegistryCorruptedError as exc:
            return json.dumps(
                {"error": f"Registry file is corrupted: {exc}. Run rlm_projects(action='list') after fixing the file."},
                ensure_ascii=False,
            )
        if not matches:
            all_projects = reg.list_projects()
            available = [{"name": p["name"], "description": p.get("description", "")} for p in all_projects]
            return json.dumps(
                {"error": f"Project not found: {project}", "available_projects": available}, ensure_ascii=False
            )
        if len(matches) > 1:
            ambiguous = [{"name": p["name"], "description": p.get("description", "")} for p in matches]
            return json.dumps({"error": f"Ambiguous project name: {project}", "matches": ambiguous}, ensure_ascii=False)
        if method == "fuzzy":
            return json.dumps({"error": f"Did you mean '{matches[0]['name']}'?"}, ensure_ascii=False)
        path = matches[0]["path"]
        resolved_project_name = matches[0]["name"]

    resolved, err_json = _normalize_and_validate_path(path)
    if err_json is not None:
        # Issue #16: drop/info act on the cache/index, not the sources. If the
        # source directory was deleted (project decommissioned) the index can
        # still be inspected/removed — recover its effective base_path from the
        # stored index metadata. Only when the dir is genuinely gone (not for a
        # dir-exists ambiguity error).
        recovered: str | None = None
        if action in ("info", "drop") and not os.path.isdir(_canonicalize_path(path)):
            recovered = _recover_index_base_path_for_missing_source(path)
        if recovered is None:
            return err_json
        resolved = recovered

    try:
        if action == "build":
            t0 = time.monotonic()
            builder = IndexBuilder()
            db_path = builder.build(
                resolved,
                build_calls=not no_calls,
                build_metadata=not no_metadata,
                build_fts=not no_fts,
                build_synonyms=not no_synonyms,
            )
            elapsed = time.monotonic() - t0
            try:
                touch_project_cache(resolved)
            except Exception as exc:
                logger.debug("rlm_index build: touch_project_cache failed: %s", exc)
            result = {
                "action": "build",
                "path": resolved,
                "db_path": str(db_path),
                "elapsed_seconds": round(elapsed, 1),
            }
            if resolved_project_name:
                result["project"] = resolved_project_name
            return json.dumps(result, ensure_ascii=False)

        if action == "update":
            t0 = time.monotonic()
            builder = IndexBuilder()
            delta = builder.update(resolved)
            elapsed = time.monotonic() - t0
            try:
                touch_project_cache(resolved)
            except Exception as exc:
                logger.debug("rlm_index update: touch_project_cache failed: %s", exc)
            result = {"action": "update", "path": resolved, "elapsed_seconds": round(elapsed, 1), **delta}
            if resolved_project_name:
                result["project"] = resolved_project_name
            return json.dumps(result, ensure_ascii=False)

        if action == "info":
            # Check in-memory build job state
            with _build_jobs_lock:
                job = _build_jobs.get(resolved)

            # Short-circuit during active build — DB may be deleted/partially written
            if job and job["status"] == "building":
                result: dict = {
                    "action": "info",
                    "path": resolved,
                    "build_status": "building",
                    "build_action": job["action"],
                    "build_started_at": job["started_at"],
                    "build_elapsed": round(time.time() - job["started_at"], 1),
                }
                if resolved_project_name:
                    result["project"] = resolved_project_name
                return json.dumps(result, ensure_ascii=False)

            # Error/done without DB (build failed before creating file)
            if job and job["status"] == "error":
                db_path = get_index_db_path(resolved)
                if not db_path.exists():
                    result = {
                        "action": "info",
                        "path": resolved,
                        "build_status": "error",
                        "build_error": job["error"],
                        "build_finished_at": job["finished_at"],
                    }
                    if resolved_project_name:
                        result["project"] = resolved_project_name
                    return json.dumps(result, ensure_ascii=False)

            db_path = get_index_db_path(resolved)
            if not db_path.exists():
                return json.dumps({"error": "Index not found", "path": resolved}, ensure_ascii=False)
            # Incomplete in-place build → report incomplete WITHOUT get_statistics (its
            # first COUNT(*) FROM modules/methods would hit the DROP→CREATE window).
            if index_incomplete(db_path):
                result = {
                    "action": "info",
                    "path": resolved,
                    "build_status": "incomplete",
                }
                if resolved_project_name:
                    result["project"] = resolved_project_name
                # Don't hide the real failure cause (codex round 11): if the job errored,
                # surface build_error/build_finished_at next to the incomplete marker.
                if job and job["status"] == "error":
                    result["build_error"] = job["error"]
                    result["build_finished_at"] = job["finished_at"]
                return json.dumps(result, ensure_ascii=False)
            try:
                touch_project_cache(resolved)
            except Exception as exc:
                logger.debug("rlm_index info: touch_project_cache failed: %s", exc)
            reader = IndexReader(str(db_path))
            try:
                stats = reader.get_statistics()
                # Race guard (codex High): get_statistics is _transient_safe → during a
                # concurrent rebuild's DROP window it returns a zero/load-failure sentinel
                # (not an exception). Re-check the marker too (not just the stats sentinel):
                # in the [empty tables + stale meta still present] sub-window of
                # _begin_inplace_rebuild, built_at/builder_version are NOT yet cleared so
                # stats_indicate_load_failure is False — the marker is the only signal.
                # Mirror rlm_start's combined post-read check. Don't emit a payload of zeros.
                if index_incomplete(db_path) or stats_indicate_load_failure(stats):
                    result = {"action": "info", "path": resolved, "build_status": "incomplete"}
                    if resolved_project_name:
                        result["project"] = resolved_project_name
                    if job and job["status"] == "error":
                        result["build_error"] = job["error"]
                        result["build_finished_at"] = job["finished_at"]
                    return json.dumps(result, ensure_ascii=False)
                result = {"action": "info", "path": resolved, **stats}
                if resolved_project_name:
                    result["project"] = resolved_project_name
                # Enrich with completed/errored build status
                if job:
                    if job["status"] == "done":
                        result["build_status"] = "done"
                        result["build_result"] = job["result"]
                        result["build_finished_at"] = job["finished_at"]
                    elif job["status"] == "error":
                        result["build_status"] = "error"
                        result["build_error"] = job["error"]
                        result["build_finished_at"] = job["finished_at"]
                return json.dumps(result, ensure_ascii=False)
            finally:
                reader.close()

        if action == "drop":
            from rlm_tools_bsl.cache import purge_project_cache

            db_path = get_index_db_path(resolved)
            db_existed = db_path.exists()
            if db_existed:
                db_path.unlink()
                # Remove parent dir if empty
                try:
                    db_path.parent.rmdir()
                except OSError:
                    pass
            # Complete decommission: also drop the project's file-listing cache
            # (<cache_root>/<hash>/file_index.json). Done best-effort even when the
            # DB is already gone/lost, so an orphaned cache dir still gets cleaned
            # up (matches the issue #16 manual workaround) instead of leaking.
            dropped_cache = purge_project_cache(resolved)
            if not db_existed and not dropped_cache:
                return json.dumps({"error": "Index not found", "path": resolved}, ensure_ascii=False)
            result: dict = {"action": "drop", "path": resolved}
            if db_existed:
                result["dropped"] = str(db_path)
            if dropped_cache:
                result["dropped_cache"] = dropped_cache
            if resolved_project_name:
                result["project"] = resolved_project_name
            return json.dumps(result, ensure_ascii=False)

        return json.dumps({"error": f"Unknown action: {action}"}, ensure_ascii=False)

    except FileNotFoundError as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False)
    except Exception as exc:
        logger.exception("rlm_index error: action=%s path=%s", action, resolved)
        return json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False)


class _HealthLogFilter(logging.Filter):
    """Suppress noisy uvicorn access-log lines for GET /health."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return "GET /health" not in msg


class _AsyncioConnResetFilter(logging.Filter):
    """Suppress ONLY the benign Windows ProactorEventLoop connection-teardown noise.

    On Windows, ``_ProactorBasePipeTransport._call_connection_lost`` raises
    ConnectionResetError [WinError 10054] when an HTTP client drops the connection;
    the default asyncio handler logs it with a full traceback. We drop the record
    ONLY when ALL three hold, so any real asyncio error still reaches the log:
      1. the exception is a ConnectionResetError (or subclass), AND
      2. its winerror or errno == 10054, AND
      3. ``_call_connection_lost`` appears in the traceback frames/text.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        exc_info = record.exc_info
        if not exc_info or not isinstance(exc_info, tuple):
            return True
        exc = exc_info[1]
        if not isinstance(exc, ConnectionResetError):
            return True
        if getattr(exc, "winerror", None) != 10054 and getattr(exc, "errno", None) != 10054:
            return True
        tb = exc_info[2]
        found = False
        while tb is not None:
            if tb.tb_frame.f_code.co_name == "_call_connection_lost":
                found = True
                break
            tb = tb.tb_next
        if not found:
            try:
                text = "".join(traceback.format_exception(*exc_info))
            except Exception:
                text = ""
            found = "_call_connection_lost" in text
        # Suppress (return False) only the benign teardown; keep everything else.
        return not found


def _install_asyncio_conn_reset_filter() -> None:
    """Attach _AsyncioConnResetFilter to the ``asyncio`` logger (idempotent)."""
    asyncio_logger = logging.getLogger("asyncio")
    if any(isinstance(f, _AsyncioConnResetFilter) for f in asyncio_logger.filters):
        return
    asyncio_logger.addFilter(_AsyncioConnResetFilter())


def _server_log_path() -> pathlib.Path:
    """Return the path of ``server.log`` for the HTTP transport.

    Single owner of the rule "RLM_CONFIG_FILE → dirname/logs, else
    ~/.config/rlm-tools-bsl/logs" **inside this module**: both
    :func:`_setup_file_logging` (which creates the directory) and
    :func:`_log_effective_env` (which only reports the path) ask here.

    Honest boundary: this is NOT a project-wide single source of truth — the
    same rule independently lives in ``_service_win.py``. Merging them would
    touch the service module, so the tripwire test in
    ``tests/test_server_logging.py`` locks the ``server`` ↔ ``service`` edge and
    the full merge is backlogged. The directory is NOT created here.
    """
    config_override = os.environ.get("RLM_CONFIG_FILE")
    if config_override:
        log_dir = pathlib.Path(config_override).parent / "logs"
    else:
        log_dir = pathlib.Path.home() / ".config" / "rlm-tools-bsl" / "logs"
    return log_dir / "server.log"


def _setup_file_logging():
    """Add rotating file handler for HTTP transport mode."""
    from logging.handlers import RotatingFileHandler

    # Use RLM_CONFIG_FILE-derived path if set (Windows service / Session 0)
    log_path = _server_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)

    # Time-based retention: drop entries older than RLM_LOG_RETENTION_DAYS (default 20)
    # so server.log doesn't grow unbounded. Skipped under the Windows service
    # (RLM_UNDER_SERVICE=1) — there the service purges before it opens the file for the
    # child's stderr redirect, so the child must not truncate a file the service holds open.
    retention_stats = None
    if not os.environ.get("RLM_UNDER_SERVICE"):
        from rlm_tools_bsl.log_retention import log_retention_days, purge_log_older_than

        retention_stats = purge_log_older_than(log_path, days=log_retention_days())

    handler = RotatingFileHandler(
        log_path,
        maxBytes=5 * 1024 * 1024,  # 5 MB
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    logging.getLogger().addHandler(handler)
    logging.getLogger("uvicorn.access").addFilter(_HealthLogFilter())
    # Drop benign Windows ProactorEventLoop teardown noise (ConnectionResetError
    # [WinError 10054] in _call_connection_lost). Idempotent — safe to re-call.
    _install_asyncio_conn_reset_filter()
    logger.info("File logging enabled: %s", log_path)
    if retention_stats and retention_stats.get("status") == "purged":
        logger.info(
            "Log retention: dropped %d lines older than %d days (kept %d)",
            retention_stats["removed_lines"],
            log_retention_days(),
            retention_stats["kept_lines"],
        )


def _shutdown_all_sandbox_backends() -> None:
    """Bounded остановка всех sandbox workers при server shutdown (§13.6).

    Один общий deadline ``RLM_SANDBOX_SHUTDOWN_DEADLINE_SECONDS`` на ВСЮ
    последовательность (не per-worker); после его истечения остатки получают
    немедленный force-kill без нового окна ожидания. Идемпотентна и не падает
    из-за одного проблемного worker.
    """
    global _sandbox_registry_accepting, _sandbox_registry_epoch

    # Общий deadline отсчитывается от НАЧАЛА всей последовательности, включая
    # revoke всех backend-ов, а не только последующий graceful/force проход.
    deadline = time.monotonic() + shutdown_deadline_seconds()
    with _sandboxes_lock:
        _sandbox_registry_accepting = False
        _sandbox_registry_epoch += 1
        backends = list(_sandboxes.items())
        backends.extend((f"starting-{key}", backend) for key, backend in _starting_sandbox_backends.items())
        _sandboxes.clear()
        _starting_sandbox_backends.clear()
    # Раннего выхода на пустом registry НЕТ: в очереди reaper-а могут лежать
    # backends, снятые эвикцией прямо перед остановкой, и их деревья тоже обязаны
    # быть добиты здесь, а не оставлены на семантику daemon-процессов.
    for sid, backend in backends:
        try:
            backend.request_close("server_shutdown")
        except Exception:
            logger.warning("shutdown: request_close failed for session %s", sid, exc_info=True)
    closed = forced = errors = 0
    unfinished: list[tuple[str, object]] = []
    for index, (sid, backend) in enumerate(backends):
        if time.monotonic() >= deadline:
            # Обычный finish_close даже с истёкшим deadline не должен вызываться
            # N раз: оставшиеся сразу идут в zero-wait force phase.
            unfinished.extend(backends[index:])
            break
        try:
            report = backend.finish_close(deadline)
        except Exception:
            errors += 1
            unfinished.append((sid, backend))
            logger.warning("shutdown: finish_close failed for session %s", sid, exc_info=True)
            continue
        if report.closed:
            closed += 1
        else:
            unfinished.append((sid, backend))
        if report.forced:
            forced += 1
        if report.errors:
            errors += 1
    drained = _reaper.drain(deadline)
    # Остатки получают НЕМЕДЛЕННЫЙ force-kill без нового окна ожидания (§13.6):
    # kill дерева для process, detached close reader для inline.
    # force_abort — НЕблокирующий: finish_close здесь встал бы в очередь за
    # _close_lock, которым может владеть reaper со своим собственным deadline.
    for sid, backend in unfinished:
        try:
            if backend.force_abort():
                forced += 1
            else:
                errors += 1
                logger.warning("shutdown: session %s не удалось добить (возможная утечка процесса)", sid)
                _reaper.enqueue(backend)
        except Exception:
            errors += 1
            logger.warning("shutdown: force_abort failed for session %s", sid, exc_info=True)
            _reaper.enqueue(backend)
    reaper_forced, reaper_left = _reaper.force_abort_pending()
    logger.info(
        "shutdown: sandbox backends total=%d closed=%d forced=%d errors=%d "
        "reaper_drained=%s reaper_forced=%d reaper_left=%d",
        len(backends),
        closed,
        forced,
        errors,
        drained,
        reaper_forced,
        reaper_left,
    )


def sandbox_diagnostics() -> dict:
    """Счётчики состояний sandbox-backend для тестов/health-метрик (§20).

    Только counts/states: ни путей, ни секретов, ни PID. Не является sandbox
    helper и агенту не доступна.
    """
    with _sandboxes_lock:
        backends = list(_sandboxes.values())
    states: dict[str, int] = {}
    modes: dict[str, int] = {}
    for backend in backends:
        try:
            states[backend.state] = states.get(backend.state, 0) + 1
            modes[backend.mode] = modes.get(backend.mode, 0) + 1
        except Exception:  # noqa: BLE001 — диагностика не имеет права падать
            states["unknown"] = states.get("unknown", 0) + 1
    return {
        "sessions_with_backend": len(backends),
        "states": states,
        "modes": modes,
        "reaper_pending": _reaper.pending_count(),
    }


def _warmup_imports():
    """Pre-import heavy modules so first rlm_start is fast. Best-effort."""
    _t0 = time.monotonic()
    try:
        import rlm_tools_bsl.bsl_helpers  # noqa: F401
        import rlm_tools_bsl.bsl_xml_parsers  # noqa: F401
        import rlm_tools_bsl.bsl_index  # noqa: F401
        import rlm_tools_bsl.helpers  # noqa: F401

        # openai греем только для inline: при spawn дочерний worker родительский
        # прогрев не наследует, а parent в process mode client не создаёт (§12.1).
        try:
            if get_sandbox_mode() == "inline":
                warmup_openai_import()
        except SandboxConfigError:
            pass  # невалидный режим уже отверг старт в main()
    except Exception:
        logger.debug("warmup: import error (non-critical)", exc_info=True)
    logger.info("warmup: completed in %.1fs", time.monotonic() - _t0)


def _prepare_stdio_transport():
    """Развести дескрипторы и выбрать способ запуска stdio-транспорта.

    Возвращает `(restore, runner)`: `runner is None` означает «поднимать
    транспорт обычным `mcp.run()`».

    Предпочтительный путь — отдать транспорту провода ЯВНО, оставив
    `sys.stdin`/`sys.stdout` на отводах: тогда посторонний `print()` из любого
    кода процесса физически не может попасть в JSON-RPC поток. Точка входа
    FastMCP для этого резолвится ДО разводки: если её не окажется, поднимать
    транспорт с уже уведённым fd 1 нельзя — ответы молча ушли бы в stderr,
    поэтому в этом случае разводка выполняется с подменой sys-потоков, а
    транспорт запускается штатно.
    """
    import anyio
    from mcp.server.stdio import stdio_server

    from rlm_tools_bsl._stdio_hardening import harden_stdio_for_children

    low_level = getattr(mcp, "_mcp_server", None)
    explicit_streams = callable(getattr(low_level, "run", None)) and callable(
        getattr(low_level, "create_initialization_options", None)
    )

    hardening = harden_stdio_for_children(swap_sys_streams=not explicit_streams)
    logger.info("stdio hardening: applied=%s (%s)", hardening.applied, hardening.detail)
    if not explicit_streams:
        logger.warning(
            "stdio transport: точка входа FastMCP недоступна — протокол берётся из sys-потоков, "
            "посторонний вывод в stdout не изолирован от протокола"
        )

    if not (explicit_streams and hardening.applied):
        return hardening.restore, None

    async def _serve() -> None:
        async with stdio_server(
            stdin=anyio.wrap_file(hardening.wire_stdin),
            stdout=anyio.wrap_file(hardening.wire_stdout),
        ) as (read_stream, write_stream):
            await low_level.run(read_stream, write_stream, low_level.create_initialization_options())

    return hardening.restore, lambda: anyio.run(_serve)


def _env_display(name: str) -> str:
    """Render an environment variable for the startup line.

    "Not set" and "set to an empty value" are DIFFERENT states: the second one
    silently shadows the value from ``.env`` (``load_dotenv(override=False)``
    sees the key as already present), so it must be distinguishable in the log.
    """
    raw = os.environ.get(name)
    if raw is None:
        return "not set"
    if not raw.strip():
        return "(blank)"
    return raw


def _log_effective_env(transport: str) -> None:
    """Log ONE line describing where this server keeps its state.

    Emitted for BOTH transports — under stdio there is no ``server.log`` at all,
    so stderr is the only place a human can see the effective configuration.
    Carries no secrets: four variable NAMES plus two derived roots.
    """
    try:
        from rlm_tools_bsl.cache import _cache_base

        try:
            version = importlib.metadata.version("rlm-tools-bsl")
        except Exception:
            version = "?"
        index_root, index_rule = describe_index_root()
        try:
            cache_root: object = _cache_base()
        except Exception as exc:
            cache_root = f"<недоступен: {exc}>"
        if transport == "stdio":
            log_target: object = "stderr"
        else:
            try:
                log_target = _server_log_path()
            except Exception as exc:
                log_target = f"<неизвестен: {exc}>"
        logger.info(
            "startup: version=%s transport=%s sandbox_mode=%s strategy_mode=%s catalog_mode=%s "
            "RLM_CONFIG_FILE=%s RLM_INDEX_DIR=%s index_root=%s (%s) cache_root=%s log=%s",
            version,
            transport,
            get_sandbox_mode(),
            get_strategy_mode(),
            get_catalog_mode(),
            _env_display("RLM_CONFIG_FILE"),
            _env_display("RLM_INDEX_DIR"),
            index_root,
            index_rule,
            cache_root,
            log_target,
        )
        for remark in index_root_diagnostics():
            logger.warning("%s", remark)
    except Exception as exc:  # pragma: no cover - diagnostics must not break startup
        logger.warning("startup env snapshot failed: %s", exc)


def main():
    global session_manager
    from rlm_tools_bsl._config import load_project_env

    # Force UTF-8 + line-buffered stdio.
    #
    # UTF-8: the Windows service redirects this child's stderr into server.log
    # (see _service_win.py). On Windows a *redirected* (non-console) stderr
    # otherwise encodes with the legacy ANSI code page (cp1251), so log records
    # carrying Cyrillic (object names, and the rlm_execute `code=<…>` field) were
    # written as cp1251 while the RotatingFileHandler writes UTF-8 — a mixed-
    # encoding file that no single decoder reads. Non-cp1251 chars (e.g. the ⏎
    # newline marker U+23CE) degraded to `⏎` via backslashreplace. Pinning
    # UTF-8 makes every sink consistent and the log fully readable. Belt-and-
    # braces with PYTHONUTF8 set by _service_win.py.
    #
    # line_buffering: log lines reach the service log file immediately, not in
    # 4-8 KB block-buffered chunks (also covered by PYTHONUNBUFFERED). Has no
    # effect when stdio is already line-buffered (interactive tty) or unbuffered.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="backslashreplace", line_buffering=True)
        except (AttributeError, OSError, ValueError):
            pass

    # A service command must keep the config selected by the CALLER. The .env loaded
    # below may legitimately contain RLM_CONFIG_FILE for ordinary server/CLI work, but
    # allowing it to redirect `service install` means we stop reading the very config
    # whose settings are supposed to survive the reinstall.
    config_file_was_set = "RLM_CONFIG_FILE" in os.environ
    config_file_before_env = os.environ.get("RLM_CONFIG_FILE")
    load_project_env()
    # v1.41.0: регистрация rlm_help решалась по RLM_STRATEGY_MODE при импорте, то
    # есть ДО загрузки .env — режим только из .env менял стратегию, но не список
    # тулов. Согласуем сразу после загрузки, до первого list_tools(). Загрузку .env
    # на импорт не переносим: service-команды сохраняют RLM_CONFIG_FILE вызывающего,
    # а --version работающей службы не требует.
    _sync_rlm_help_registration()

    parser = argparse.ArgumentParser(description="rlm-tools-bsl MCP server")
    parser.add_argument(
        "--version",
        "-V",
        action="version",
        version=f"%(prog)s {importlib.metadata.version('rlm-tools-bsl')}",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default=os.environ.get("RLM_TRANSPORT", "stdio"),
        help="Transport protocol (env: RLM_TRANSPORT, default: stdio)",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("RLM_HOST", "127.0.0.1"),
        help="Bind host for HTTP transport (env: RLM_HOST, default: 127.0.0.1)",
    )
    # Parsed defensively: this default is computed while the parser is BUILT, so a
    # RLM_PORT of "not-a-number" used to abort every invocation -- `--version` and
    # `service install --help` included -- before argparse could say anything useful.
    from rlm_tools_bsl.service import DEFAULT_PORT, _first_port

    env_port = _first_port(os.environ.get("RLM_PORT"))
    if env_port is None and os.environ.get("RLM_PORT"):
        logger.warning("RLM_PORT=%r is not a valid port, using %d", os.environ["RLM_PORT"], DEFAULT_PORT)
    parser.add_argument(
        "--port",
        type=int,
        default=env_port or DEFAULT_PORT,
        help="Bind port for HTTP transport (env: RLM_PORT, default: 9000)",
    )

    subparsers = parser.add_subparsers(dest="command")
    service_parser = subparsers.add_parser("service", help="Manage system service (Windows SC / Linux systemd)")
    service_sub = service_parser.add_subparsers(dest="service_action")

    install_p = service_sub.add_parser("install", help="Install and enable the service")
    # Defaults are resolved in service.resolve_install_settings(), NOT here: an omitted
    # flag means "keep what the previous installation used", so that re-running the
    # installer to upgrade cannot silently reset the service to 127.0.0.1:9000.
    install_p.add_argument(
        "--host",
        default=None,
        help="Bind host (default: saved value, then env RLM_HOST, then 127.0.0.1)",
    )
    install_p.add_argument(
        "--port",
        type=int,
        default=None,
        help="Bind port (default: saved value, then env RLM_PORT, then 9000)",
    )
    # --env and --no-env answer the same question, so let argparse reject the pair
    # instead of silently letting one of them win.
    env_group = install_p.add_mutually_exclusive_group()
    env_group.add_argument(
        "--env",
        default=None,
        metavar="PATH",
        help="Path to .env file (default: keep the saved one)",
    )
    env_group.add_argument(
        "--no-env",
        action="store_true",
        dest="no_env",
        help="Start the SERVICE without any .env file (drops the saved path). "
        "Does not affect the environment of this install command itself",
    )

    for _action in ("start", "stop", "status"):
        service_sub.add_parser(_action)

    uninstall_p = service_sub.add_parser("uninstall", help="Stop and remove the service")
    uninstall_p.add_argument(
        "--purge",
        action="store_true",
        help="Also delete service.json (host/port/.env settings)",
    )

    args = parser.parse_args()

    if args.command == "service":
        if config_file_was_set:
            # Membership above also covers an explicitly empty value. Keep it exact;
            # _config_path() deliberately treats empty as the default path.
            os.environ["RLM_CONFIG_FILE"] = config_file_before_env or ""
        else:
            os.environ.pop("RLM_CONFIG_FILE", None)
        from rlm_tools_bsl.service import handle_service_command

        handle_service_command(args)
        return

    # Validate only on the MCP-server path.  argparse's --version and service
    # management are independent utilities and must not require a usable sandbox.
    try:
        sandbox_env = validate_sandbox_env()
    except SandboxConfigError as e:
        logger.error("Invalid sandbox configuration: %s", e)
        raise SystemExit(f"rlm-tools-bsl: invalid sandbox configuration: {e}") from None
    logger.info("sandbox config: %s", sandbox_env)
    if sandbox_env["mode"] == "process" and sandbox_env["memory_mb"] == 0:
        logger.warning(
            "RLM_SANDBOX_MEMORY_MB=0: потолок памяти sandbox-worker ОТКЛЮЧЁН — "
            "один сеанс может исчерпать память хоста. Значение 0 предназначено "
            "для платформ/сценариев, где лимит мешает; иначе задайте >= 16."
        )
    if sandbox_env["mode"] == "inline":
        logger.warning(
            "RLM_SANDBOX_MODE=inline задан ЯВНО: hard process isolation ОТКЛЮЧЕНА — "
            "код агента выполняется в основном MCP-процессе; timeout не является hard-kill. "
            "Inline предназначен только для диагностики/аварийного восстановления, "
            "а также для клиентов, запрещающих дочерним процессам каналы и "
            "разделяемую память, — там это единственный рабочий режим."
        )

    session_manager = build_session_manager_from_env()
    session_manager.on_evict = _release_session_resources

    if args.transport != "stdio":
        _setup_file_logging()
        mcp.settings.host = args.host
        mcp.settings.port = args.port

        # Disable DNS rebinding protection for external interfaces —
        # when binding to 0.0.0.0 the Host header can be any IP.
        if args.host not in ("127.0.0.1", "localhost", "::1"):
            mcp.settings.transport_security = TransportSecuritySettings(
                enable_dns_rebinding_protection=False,
            )

    if args.transport != "stdio":
        logger.info(
            "transport=%s stateless_http=%s host=%s port=%s",
            args.transport,
            mcp.settings.stateless_http,
            getattr(mcp.settings, "host", "?"),
            getattr(mcp.settings, "port", "?"),
        )

    # Стартовый снимок окружения. Место выбрано намеренно: ПОСЛЕ
    # _setup_file_logging() (иначе при HTTP-запуске строка не попала бы в
    # server.log), после load_project_env() (иначе не увидели бы значения из
    # .env) и после validate_sandbox_env() (режим песочницы уже разобран).
    # Вызывается БЕЗУСЛОВНО для обоих транспортов: под stdio server.log не
    # ведётся вовсе, и stderr — единственное место, где человек может увидеть
    # фактические корни.
    _log_effective_env(args.transport)

    # Проверка env-настроек sub-LLM. Место выбрано намеренно: ПОСЛЕ
    # _setup_file_logging() (иначе предупреждение при HTTP-запуске не попало бы в
    # server.log) и после load_project_env() (иначе не увидели бы значения из .env).
    # Валидируем здесь, а не в провайдере: в дефолтном режиме песочницы провайдер
    # создаётся лениво в sandbox-воркере, а тот не настраивает logging и пишет
    # stderr в devnull — предупреждение оттуда не увидел бы никто.
    # Строго warning + откат к дефолту, НЕ fail-fast: опечатка в одной переменной
    # не имеет права лишить агента хелпера целиком.
    for llm_env_warning in validate_llm_env():
        logger.warning("%s", llm_env_warning)
    # v1.41.0: невалидный RLM_CATALOG_MODE — откат к domains с предупреждением. Здесь,
    # а не при импорте: при импорте предупреждение ушло бы мимо server.log.
    catalog_warning = catalog_mode_env_warning()
    if catalog_warning:
        logger.warning("%s", catalog_warning)

    # One-shot per server start: migrate legacy index directories from the
    # pre-v1.9.2 home-based location into the new RLM_CONFIG_FILE-aware root.
    # NOOP for desktop installs and Docker (legacy_root == new_root).
    try:
        from rlm_tools_bsl.bsl_index import (
            get_index_dir_root,
            migrate_legacy_index_root,
        )

        moved = migrate_legacy_index_root()
        if moved:
            logger.info(
                "migrate_legacy_index_root: migrated_legacy_index_dirs=%d to=%s",
                moved,
                get_index_dir_root(),
            )
    except Exception as exc:
        logger.warning("migrate_legacy_index_root failed: %s", exc)

    # One-shot per server start: clean up stale project caches. Only runs for
    # actual server startup (stdio or streamable-http) — not for --version or
    # `service` sub-commands, which are short-lived utilities.
    try:
        from rlm_tools_bsl.cache import cleanup_stale_cache

        stats = cleanup_stale_cache()
        if stats.get("disabled"):
            logger.info("cleanup_stale_cache: disabled (RLM_CACHE_MAX_AGE_DAYS<=0)")
        else:
            logger.info(
                "cleanup_stale_cache: legacy_markers_written=%d scanned=%d removed=%d bytes_freed=%d cache_root=%s",
                stats.get("legacy_markers_written", 0),
                stats.get("scanned", 0),
                stats.get("removed", 0),
                stats.get("bytes_freed", 0),
                stats.get("cache_root", "?"),
            )
            for err in stats.get("errors", [])[:5]:
                logger.warning("cleanup_stale_cache: %s", err)
    except Exception as exc:
        logger.warning("cleanup_stale_cache failed: %s", exc)

    # stdio: увести fd 0/1 с протокольных труб ДО старта транспорта и любого
    # спавна. Иначе дочерний Python (sandbox-worker) наследует стандартные
    # хэндлы родителя и зависает внутри инициализации интерпретатора на pipe,
    # где транспорт держит блокирующее чтение (CPython gh-78961). Best-effort:
    # неудача разводки не имеет права сорвать старт сервера.
    stdio_restore = None
    run_stdio = None
    if args.transport == "stdio":
        try:
            stdio_restore, run_stdio = _prepare_stdio_transport()
        except Exception as exc:
            logger.warning("stdio hardening failed: %s: %s", type(exc).__name__, exc)

    _begin_sandbox_backend_lifecycle()
    try:
        threading.Thread(target=_warmup_imports, daemon=True).start()
        if run_stdio is not None:
            run_stdio()
        else:
            mcp.run(transport=args.transport)
    finally:
        # Не полагаться на daemon-семантику процессов: явный bounded shutdown
        # всех sandbox workers с единым deadline (§13.6).
        _shutdown_all_sandbox_backends()
        if stdio_restore is not None:
            try:
                stdio_restore()
            except Exception as exc:  # shutdown-путь: диагностика, но не новая ошибка
                logger.warning("stdio hardening restore failed: %s: %s", type(exc).__name__, exc)
