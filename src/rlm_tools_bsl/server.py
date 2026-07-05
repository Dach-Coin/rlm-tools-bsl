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
from rlm_tools_bsl.llm_bridge import get_llm_query_fn, make_llm_query_batched, warmup_openai_import
from rlm_tools_bsl.format_detector import FormatInfo, SourceFormat, detect_format
from rlm_tools_bsl.extension_detector import (
    ConfigRole,
    _ext_list_cap,
    detect_extension_context,
    find_extension_overrides,
    resolve_config_root,
)
from rlm_tools_bsl.bsl_knowledge import (
    EFFORT_LEVELS,
    _auto_effort,
    _fuzzy_suggest,
    _get_category_helpers,
    _get_disambiguation,
    _get_helper_details,
    _get_section,
    _get_topic_recipe,
    get_strategy,
    get_strategy_mode,
    list_categories,
    list_sections,
    list_topics,
    summarize_extensions_by_overrides,
)
from rlm_tools_bsl.bsl_index import (
    BUILDER_VERSION,
    IndexReader,
    IndexStatus,
    check_index_usable,
    get_index_db_path,
    index_incomplete,
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

mcp = FastMCP(
    "rlm-tools-bsl",
    stateless_http=True,
    instructions=(
        "1C/BSL code search & navigation backend. Prefer these tools over raw grep or reading "
        "files whenever a task touches 1C source: finding modules / objects / methods, call "
        "graphs (who calls what), references and usages of metadata objects, full-text search, "
        "and form / metadata XML parsing. A deterministic SQLite index answers in milliseconds "
        "even on 23K+ file configs and keeps file bodies on the server. Start with "
        "rlm_start(query=..., project=...) or rlm_start(query=..., path=...), then run the "
        "helpers via rlm_execute; rlm_help() lists the available recipes and helpers."
    ),
)

session_manager = SessionManager()  # defaults for tests/import

_sandboxes: dict[str, Sandbox] = {}
_idx_readers: dict[str, IndexReader] = {}
_sandboxes_lock = threading.Lock()


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


def _release_session_resources(session_id: str) -> None:
    """Идемпотентный teardown ресурсов сессии (sandbox + idx_reader)."""
    with _sandboxes_lock:
        _sandboxes.pop(session_id, None)
        reader = _idx_readers.pop(session_id, None)
    if reader is not None:
        try:
            reader.close()
        except Exception:
            pass


def _cleanup_expired_resources() -> None:
    session_manager.cleanup_expired()  # on_evict → _release_session_resources


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


def _install_session_llm_tools(session, sandbox: Sandbox) -> bool:
    try:
        base_llm_query = get_llm_query_fn()
        if base_llm_query is None:
            logger.info("llm_query not available (no LLM provider configured)")
            return False
        base_llm_query_batched = make_llm_query_batched(base_llm_query)
        lock = threading.Lock()

        def _reserve_llm_calls(count: int) -> None:
            if count < 1:
                raise ValueError("count must be >= 1")
            with lock:
                if session.llm_calls_used + count > session.max_llm_calls:
                    raise RuntimeError(
                        f"LLM call limit exceeded: {session.llm_calls_used} + {count} > {session.max_llm_calls}"
                    )
                session.llm_calls_used += count

        def llm_query(prompt: str, context: str = "") -> str:
            _reserve_llm_calls(1)
            return base_llm_query(prompt, context)

        def llm_query_batched(prompts: list[str], context: str = "") -> list[str]:
            if not prompts:
                return []
            _reserve_llm_calls(len(prompts))
            return base_llm_query_batched(prompts, context)

        sandbox._namespace["llm_query"] = llm_query
        sandbox._namespace["llm_query_batched"] = llm_query_batched
        return True
    except Exception as e:
        logger.warning(f"Could not initialize llm_query: {e}")
        return False


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
) -> str:
    t0 = time.monotonic()
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
                                f"Index built with v{idx_version}, current v{BUILDER_VERSION} — "
                                f'new helpers available after rebuild: rlm-bsl-index index build "{resolved}"'
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

        # Pre-import openai in background while Sandbox builds (~13s on slow PCs)
        if os.environ.get("RLM_LLM_BASE_URL"):
            threading.Thread(target=warmup_openai_import, daemon=True).start()

        # Determine if index is authoritative for zero-callers results
        _callers_authoritative = idx_status == IndexStatus.FRESH and idx_reader is not None and idx_reader.has_calls

        t_step = time.monotonic()
        ext_paths_for_sandbox = (
            [e.path for e in ext_context.nearby_extensions] if ext_context.current.role == ConfigRole.MAIN else []
        )
        sandbox = Sandbox(
            base_path=resolved,
            max_output_chars=max_output_chars,
            execution_timeout_seconds=execution_timeout_seconds,
            format_info=format_info,
            idx_reader=idx_reader,
            idx_zero_callers_authoritative=_callers_authoritative,
            extension_paths=ext_paths_for_sandbox,
        )
        has_llm_tools = _install_session_llm_tools(session, sandbox)
        t_sandbox = time.monotonic() - t_step
        logger.info(
            "rlm_start: session=%s sandbox ready, llm_tools=%s index=%s",
            session_id,
            has_llm_tools,
            idx_reader is not None,
        )

        # Auto-detect custom prefixes — fast path from index, fallback to glob scan
        t_step = time.monotonic()
        detected_prefixes: list[str] = []
        src_prefixes = "none"
        if idx_reader is not None:
            try:
                detected_prefixes = idx_reader.get_detected_prefixes()
                if detected_prefixes:
                    src_prefixes = "index"
            except Exception:
                pass
        if not detected_prefixes:
            _prefix_fn = sandbox._namespace.get("_detected_prefixes")
            if callable(_prefix_fn):
                try:
                    detected_prefixes = _prefix_fn()
                    if detected_prefixes:
                        src_prefixes = "fallback"
                except Exception:
                    pass
        t_prefixes = time.monotonic() - t_step

        bsl_registry = sandbox._namespace.get("_registry") or {}
        t_step = time.monotonic()
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

        with _sandboxes_lock:
            _sandboxes[session_id] = sandbox
            if idx_reader is not None:
                _idx_readers[session_id] = idx_reader
    except Exception as e:
        logger.error("rlm_start: session=%s failed: %s", session_id, e, exc_info=True)
        session_manager.end(session_id)
        # idx_reader создан (405), но мог НЕ успеть зарегистрироваться в _idx_readers
        # (исключение между созданием и 581) → on_evict его не увидит, end() callback не зовёт.
        if idx_reader is not None:
            try:
                idx_reader.close()
            except Exception:
                pass
        return json.dumps(
            {"error": f"Session init failed: {type(e).__name__}: {e}"},
            ensure_ascii=False,
        )

    # Build available_functions from registry (BSL helpers) + static IO helpers
    available_functions = [entry["sig"] for entry in bsl_registry.values()]
    available_functions.extend(
        [
            "read_file(path) -> str (numbered: '  42 | code')",
            "read_files(paths) -> dict[path, str] — BATCH: читай N файлов одним вызовом вместо N×read_file (numbered)",
            "grep(pattern, path='.') -> list[dict] keys: file, line, text",
            "grep_summary(pattern, path='.') -> compact grouped string",
            "grep_read(pattern, path='.', max_files=10, context_lines=0) -> {matches, files (numbered), summary}",
            "glob_files(pattern) -> list[str]",
            "tree(path='.', max_depth=3) -> str",
            "find_files(name) -> list[str]",
        ]
    )
    if has_llm_tools:
        available_functions.extend(
            [
                "llm_query(prompt, context='')",
                "llm_query_batched(prompts, context='')",
            ]
        )

    # На extreme-extension конфигах (напр. 155 расш) сериализация полного списка
    # расширений в ответ раздувала rlm_start выше токен-лимита. Усекаем агент-facing
    # поле до top-N по overrides; питание песочницы (ext_paths_for_sandbox) — полное.
    # Режем ТОЛЬКО ветку MAIN (как Site 1 _build_warnings и Site 3 _extension_strategy):
    # для EXTENSION/UNKNOWN-сессий nearby_extensions = соседи, их не усекаем (план).
    if ext_context.current.role == ConfigRole.MAIN:
        shown_exts, ext_total, ext_shown = summarize_extensions_by_overrides(
            ext_context.nearby_extensions, ext_overrides, _ext_list_cap()
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
            "loaded": idx_reader is not None,
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
        index_block = {
            "loaded": idx_reader is not None,
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
            "index_status": "incomplete" if (index_incomplete(db_path) or idx_load_failed) else "missing",
            "warnings": idx_warnings,
        }

    response: dict = {
        "session_id": session_id,
        "resolved_path": resolved,
        "warnings": ext_context.warnings,
        "config_format": format_info.format_label,
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
        },
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


def _execute_code_log_field(code: str) -> str:
    """Return a ``code=<...>`` suffix for the rlm_execute completion log line.

    The executed ``code`` IS the agent's query — helper calls with their
    parameters (``find_object("…")`` etc.). Logging it lets us later analyse
    which queries recur and where helpers could be improved.

    Controlled by ``RLM_LOG_EXECUTE_CODE`` (default: ON, capped):
      * unset / ``1`` / ``true`` / ``on`` / ``yes`` / ``all`` → default cap
        (``_DEFAULT_EXECUTE_CODE_LOG_CAP`` chars)
      * ``0`` / ``false`` / ``off`` / ``no`` → disabled (empty suffix)
      * positive integer ``N`` → cap at ``N`` chars (``<= 0`` → disabled)

    Newlines are flattened to ``⏎`` so the whole event stays one log line
    (grep-/parse-friendly). The log handlers are UTF-8, so Cyrillic in the code
    (object names) and the ``⏎``/``…`` markers are safe.
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
    flat = code.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "⏎")
    if len(flat) > cap:
        flat = flat[:cap] + "…"
    return f" code=<{flat}>"


def _rlm_execute(
    session_id: str,
    code: str,
    detail_level: Literal["compact", "usage", "full"] = "compact",
    max_new_variables: int = 20,
) -> str:
    t0 = time.monotonic()
    logger.info("rlm_execute: session=%s code_len=%d", session_id, len(code))
    _cleanup_expired_resources()
    session = session_manager.get(session_id)
    if not session:
        return json.dumps({"error": f"Session '{session_id}' not found or expired"}, ensure_ascii=False)

    with _sandboxes_lock:
        sandbox = _sandboxes.get(session_id)
    if not sandbox:
        return json.dumps({"error": f"Sandbox not found for session '{session_id}'"}, ensure_ascii=False)

    if session.execute_calls >= session.max_execute_calls:
        return json.dumps(
            {"error": (f"Execution call limit exceeded: {session.execute_calls} >= {session.max_execute_calls}")},
            ensure_ascii=False,
        )

    session.execute_calls += 1
    result = sandbox.execute(code)

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

    if detail_level in {"usage", "full"}:
        response["usage"] = {
            "execute_calls_used": session.execute_calls,
            "execute_calls_remaining": session.max_execute_calls - session.execute_calls,
            "llm_calls_used": session.llm_calls_used,
        }

    if detail_level == "full":
        current_vars = set(result.variables)
        previous_vars = getattr(session, "_last_reported_vars", set())
        # Build excluded_vars from registry + static helpers
        bsl_reg = sandbox._namespace.get("_registry") or {}
        excluded_vars = set(bsl_reg.keys()) | {
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
    logger.info(
        "rlm_execute: session=%s call=%d/%d error=%s elapsed=%.2fs out_chars=%d out_tokens~%d%s%s%s",
        session_id,
        session.execute_calls,
        session.max_execute_calls,
        bool(result.error),
        elapsed,
        out_chars,
        int(out_chars / 1.75),
        helpers_summary,
        hints_log,
        _execute_code_log_field(code),
    )
    return result_json


def _rlm_end(session_id: str) -> str:
    session = session_manager.get(session_id)
    if session:
        total_chars = session.total_in_chars + session.total_out_chars
        logger.info(
            "rlm_end: session=%s calls=%d in_chars=%d out_chars=%d total_chars=%d total_tokens~%d",
            session_id,
            session.execute_calls,
            session.total_in_chars,
            session.total_out_chars,
            total_chars,
            int(total_chars / 1.75),
        )
    else:
        logger.info("rlm_end: session=%s (not found)", session_id)
    session_manager.end(session_id)
    with _sandboxes_lock:
        _sandboxes.pop(session_id, None)
        reader = _idx_readers.pop(session_id, None)
    if reader is not None:
        try:
            reader.close()
        except Exception:
            pass
    return json.dumps({"success": True}, ensure_ascii=False)


@mcp.tool()
async def rlm_start(
    query: Annotated[str, Field(description="What you want to find or analyze in the BSL codebase")],
    path: Annotated[
        str | None,
        Field(
            description=(
                "Absolute path to a 1C configuration root, or to a parent container "
                "directory that holds the main configuration in a direct subdirectory "
                "(alongside optional extension subdirectories). The main configuration "
                "root is auto-detected; if multiple main configs are found in direct "
                "subdirectories without one named 'cf', an error listing the candidates "
                "is returned."
            )
        ),
    ] = None,
    project: Annotated[str | None, Field(description="Project name from the registry (alternative to path)")] = None,
    effort: Annotated[
        str,
        Field(
            description="Analysis depth. 'auto' (default) — the server picks medium, or high for multi-aspect queries (lifecycle / mechanism / end-to-end). Or force a tier: low / medium / high / max. The admin can hard-lock depth via RLM_FORCE_EFFORT; the effective value is returned as 'effective_effort'."
        ),
    ] = "auto",
    max_output_chars: Annotated[
        int, Field(description="Max characters per execute output", ge=100, le=100_000)
    ] = 15_000,
    max_llm_calls: Annotated[
        int | None,
        Field(
            ge=1,
            description="Override max llm_query calls. Wins over the effort preset and the RLM_MAX_LLM_CALLS server default.",
        ),
    ] = None,
    max_execute_calls: Annotated[
        int | None,
        Field(
            ge=1,
            description="Override max rlm_execute calls. Wins over the effort preset and the RLM_MAX_EXECUTE_CALLS server default.",
        ),
    ] = None,
    execution_timeout_seconds: Annotated[
        int, Field(description="Per-rlm_execute timeout in seconds", ge=1, le=300)
    ] = 45,
    include_metadata: Annotated[
        bool,
        Field(
            description="Scan directory and include file counts/types in response (slow on large configs, disabled by default)"
        ),
    ] = False,
) -> str:
    """Open a fast, token-efficient search & navigation session over a 1C/BSL codebase. Returns JSON with session_id.
    Reach for this INSTEAD of raw grep or reading files whenever you need to find or navigate anything in 1C source:
    locate a module / object / method, find who calls a procedure (call graph), find references or usages of a
    metadata object, run full-text search, or parse forms and metadata XML. A deterministic SQLite index answers in
    milliseconds even on 23K+ file configs, and file bodies stay on the server -- only your print() output enters context.
    You can specify either 'path' (absolute filesystem path) or 'project' (name from the project registry).
    If you don't know the path, call rlm_projects(action='list') first to see registered projects,
    then use rlm_start(project='name', query='...').
    If the user mentions a project by name -- always try project parameter first.
    If the path is not registered, the response will include a project_hint suggesting to register it.
    Then call rlm_execute(session_id, code) where code is Python that calls helper functions and uses print() to output results.
    In the default 'slim' strategy mode the returned strategy is condensed; for detailed recipes,
    helper-comparison rules and per-step menus you MUST call rlm_help(...) BEFORE running rlm_execute
    on non-trivial queries. (In legacy 'full' mode (RLM_STRATEGY_MODE=full) rlm_help is not exposed
    and the strategy contains everything inline.)
    Analysis depth defaults to 'auto': the server picks medium for simple lookups and high for multi-aspect
    queries; pass effort=low/medium/high/max to force a tier. RLM_MAX_EXECUTE_CALLS / RLM_MAX_LLM_CALLS set
    server-side default call limits (an explicit max_execute_calls / max_llm_calls you pass still wins); the
    admin can hard-lock depth via RLM_FORCE_EFFORT. Effective depth/limits are echoed in 'effective_effort' and 'limits'.
    On configs with very many extensions (> RLM_EXT_LIST_CAP, default 20) the 'extension_context.nearby_extensions'
    field is truncated to the top-N by overrides and carries 'nearby_extensions_truncated'/'nearby_extensions_total'/
    'extensions_hint'; call detect_extensions() for the full extension list (get_overrides() returns the first 200 overrides + total/truncated — check truncated; search and overrides scanning are unaffected).
    IMPORTANT: For large 1C configs (23K+ files), NEVER grep on broad paths -- use find_module() first."""
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
        )
    )


@mcp.tool()
async def rlm_execute(
    session_id: Annotated[str, Field(description="Session ID from rlm_start")],
    code: Annotated[
        str,
        Field(
            description=(
                "Python code to execute. IMPORTANT: Batch multiple related operations into each call. "
                "Object overview in ONE call: get_object_profile(name) (structure+modules+registers+"
                "subscriptions+roles+functional_options). Batch reads: read_files([p1,p2]), "
                "read_procedure(path, ['ProcA','ProcB']). A good call does several related ops + prints a "
                "summary; a bad call does just one grep or one read_file. Variables persist between calls."
            )
        ),
    ],
    detail_level: Annotated[
        Literal["compact", "usage", "full"],
        Field(
            description="Response payload level: compact=stdout+error, usage=add usage metrics, full=add variable details"
        ),
    ] = "compact",
    max_new_variables: Annotated[
        int,
        Field(
            description="When detail_level=full, cap returned new_variables list to this size",
            ge=1,
            le=200,
        ),
    ] = 20,
) -> str:
    """Run the BSL search & navigation helpers (find_module, find_callers_context, find_references_to_object,
    find_code_usages, git_search, parse_form, ...) by executing Python in the sandbox -- this is how you actually
    search and navigate the 1C codebase after rlm_start. Variables persist between calls. Use print() to see results.
    The full helper list with signatures is returned by rlm_start in the `available_functions` array.
    In the default 'slim' strategy mode call rlm_help(...) for detailed recipes, helper-comparison rules
    and per-step menus; in 'full' mode the strategy contains everything inline.
    CRITICAL: grep on path='.' ALWAYS times out on large 1C configs — use find_module() first."""
    return await anyio.to_thread.run_sync(lambda: _rlm_execute(session_id, code, detail_level, max_new_variables))


@mcp.tool()
async def rlm_end(
    session_id: Annotated[str, Field(description="Session ID to end")],
) -> str:
    """End an RLM exploration session and free resources."""
    return await anyio.to_thread.run_sync(lambda: _rlm_end(session_id))


# ─────────────────────────────────────────────────────────────────────
#                          rlm_help (slim mode)
# ─────────────────────────────────────────────────────────────────────


def _rlm_help_dispatch(
    topic: str | None = None,
    helpers: list[str] | None = None,
    category: str | None = None,
    section: str | None = None,
    format: str = "compact",
    include_code: bool = True,
) -> str:
    """Dispatch ``rlm_help`` arguments to one of six modes (see table below)
    and return a JSON string. Pure function: does not touch the active session
    and uses the cached static helper-metadata snapshot.

    Mode priority (top-down — first match wins, later args ignored with a
    warning attached to the JSON response):

    1. all-empty            → menu        (topics/categories/sections/helper count)
    2. topic given          → topic       (recipe via _match_recipe; alias-aware)
    3. section=='disambiguation' → disambiguation (filtered by `helpers` if given)
    4. section given        → section     (raw text)
    5. helpers given        → helpers     (per-helper details, optional category filter)
    6. category given       → category    (one-line per helper in that category)
    """
    from rlm_tools_bsl.bsl_helpers import build_helper_metadata_snapshot

    snapshot = build_helper_metadata_snapshot()
    warnings: list[str] = []

    def _emit(other_args: dict, kept: str) -> None:
        for arg_name, arg_val in other_args.items():
            if arg_val:
                warnings.append(f"argument '{arg_name}' ignored when '{kept}' is given")

    # Mode 1: menu
    if not (topic or helpers or category or section):
        result = {
            "available_topics": list_topics(),
            "available_categories": list_categories(),
            "available_sections": list_sections(),
            "helpers_count": len(snapshot),
            "hint": (
                "rlm_help(topic='проведение'|'печать'|'обмен'|...) → recipe for a domain. "
                "rlm_help(category='discovery'|'code'|...) → list helpers in a category. "
                "rlm_help(helpers=['name1','name2']) → details. "
                "rlm_help(section='workflow'|'disambiguation'|'performance'|'batching'|'io'|'critical')."
            ),
        }
        return json.dumps({"mode": "menu", "result": result, "warnings": warnings}, ensure_ascii=False)

    # Mode 2: topic
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
        return json.dumps({"mode": "topic", "result": recipe, "warnings": warnings}, ensure_ascii=False)

    # Mode 3: section=='disambiguation' (structured array)
    if section == "disambiguation":
        _emit({"category": category}, "section='disambiguation'")
        pairs = _get_disambiguation(filter_helpers=helpers)
        return json.dumps(
            {"mode": "disambiguation", "result": pairs, "warnings": warnings},
            ensure_ascii=False,
        )

    # Mode 4: section
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

    # Mode 5: helpers (optional category filter — AND, drops mismatches silently from result)
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

    # Mode 6: category
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
# all rules inlined and no extra tool to call. Mode is read once at module
# import time; FastMCP caches the tool list, so changing the env later
# requires a server restart.
if get_strategy_mode() == "slim":

    @mcp.tool()
    async def rlm_help(
        topic: Annotated[
            str | None,
            Field(
                description=(
                    "Business domain or alias to fetch a recipe for. "
                    "Supported domains include: 'проведение', 'печать', 'права', 'интеграция' "
                    "(aliases: 'обмен', 'синхронизация', 'exchange'), 'события формы' "
                    "(aliases: 'формы', 'обработчики формы', 'элементы формы'), 'ссылки' "
                    "(aliases: 'найти ссылки', 'where used', 'где используется в коде', 'code usages'), "
                    "'перечисления' (alias: 'enum'), "
                    "'ввод на основании', 'структура объекта' (alias: 'карточка объекта'), "
                    "'тип реквизита' (alias: 'субконто'), 'себестоимость', 'распределение', "
                    "'достижимость' (aliases: 'reachability', 'путь вызовов', 'доходит ли'), "
                    "'путь данных' (aliases: 'data path', 'как связаны', 'граф данных'). "
                    "Use rlm_help() with no args to see the full menu."
                )
            ),
        ] = None,
        helpers: Annotated[
            list[str] | None,
            Field(
                description="Helper names to fetch full sigs+kw+recipes for (e.g. ['find_callers_context','parse_form'])"
            ),
        ] = None,
        category: Annotated[
            Literal["discovery", "code", "xml", "composite", "business", "extension", "navigation"] | None,
            Field(description="Helper category to list (one-line entries: name+sig, no recipes)"),
        ] = None,
        section: Annotated[
            Literal["workflow", "disambiguation", "performance", "batching", "io", "critical"] | None,
            Field(
                description=(
                    "Strategy section to fetch. 'disambiguation' returns a structured array of "
                    "overlapping-helper pairs (use with helpers=[a,b] to narrow to one pair). "
                    "Other values return raw text."
                )
            ),
        ] = None,
        format: Annotated[
            Literal["compact", "full"],
            Field(description="For topics: 'compact' = 3-4 quick steps, 'full' = 7-9 steps + code_hint"),
        ] = "compact",
        include_code: Annotated[
            bool,
            Field(description="Include code_hint Python snippet for topics that have it (default true)"),
        ] = True,
    ) -> str:
        """Slim-mode helper companion to rlm_start. Returns details about helpers, business
        recipes and strategy sections that were intentionally omitted from the slim
        rlm_start strategy. Call this BEFORE rlm_execute on any non-trivial query.

        Six dispatch modes (priority order):
          - menu       — no args → list of topics/categories/sections + helper count.
          - topic      — domain/alias → 3-4 (compact) or 7-9 (full) steps + optional code_hint.
          - disambiguation — section='disambiguation' → array of overlapping-helper pairs;
                             pass helpers=[a,b] to narrow to one pair.
          - section    — section='workflow'|'performance'|'batching'|'io'|'critical' → raw text.
          - helpers    — list[str] of names → details with category+kw+recipe.
          - category   — single category → list of helpers in it (name+sig, no recipes).

        Output is JSON: {mode, result, warnings: list[str]}. `warnings` is always a list
        (empty if no argument conflicts) — when arguments overlap, the higher-priority
        mode wins and lower-priority args are recorded in `warnings`."""
        out = await anyio.to_thread.run_sync(
            lambda: _rlm_help_dispatch(
                topic=topic,
                helpers=helpers,
                category=category,
                section=section,
                format=format,
                include_code=include_code,
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
        logger.info(
            "rlm_help: mode=%s topic=%s category=%s section=%s helpers=%d format=%s out_chars=%d warnings=%d",
            mode,
            topic,
            category,
            section,
            helpers_count,
            format,
            len(out),
            warnings_count,
        )
        return out


@mcp.tool()
async def rlm_projects(
    action: Annotated[
        Literal["list", "add", "remove", "rename", "update"],
        Field(description="Action to perform on the project registry"),
    ],
    name: Annotated[str | None, Field(description="Project name (required for add/remove/rename/update)")] = None,
    path: Annotated[
        str | None,
        Field(
            description=(
                "Absolute filesystem path to a 1C configuration root, or to a parent "
                "container directory with the main configuration in a direct subdirectory "
                "(required for 'add'). Auto-detection of the main configuration mirrors "
                "rlm_start; if multiple candidates exist without a 'cf' subdirectory, "
                "an error is returned."
            )
        ),
    ] = None,
    description: Annotated[str | None, Field(description="Optional project description")] = None,
    new_name: Annotated[str | None, Field(description="New name for rename action")] = None,
    password: Annotated[
        str | None,
        Field(
            description="Project password. For 'add': sets the initial password (required). "
            "For 'remove/rename/update': current password for confirmation. "
            "Ask the user for their project password when server returns approval_required."
        ),
    ] = None,
    clear_password: Annotated[
        bool, Field(description="Remove project password (disables all MCP mutations until new password is set)")
    ] = False,
) -> str:
    """Manage the server-side project registry -- a mapping of human-readable project names to filesystem paths.
    Use 'list' to see all registered 1C projects, 'add' to register a new project (name + path + password),
    'remove' to unregister, 'rename' to change a project's display name, 'update' to change path or description.
    After registering a project, you can open sessions via rlm_start(project='name') instead of specifying the full path.
    When the user mentions a project by name, call list first to find available projects.
    Password is required for all mutating operations. For 'add' it sets the initial password.
    For 'remove/rename/update' it confirms the operation with the current password."""

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
    action: Annotated[
        Literal["build", "update", "info", "drop"],
        Field(description="Action to perform on the index"),
    ],
    path: Annotated[
        str | None,
        Field(
            description=(
                "Absolute path to a 1C configuration root, or to a parent container "
                "directory that holds the main configuration in a direct subdirectory. "
                "The main configuration is auto-detected; multiple candidates without a "
                "direct 'cf' subdirectory return an error listing the candidates."
            )
        ),
    ] = None,
    project: Annotated[str | None, Field(description="Project name from the registry")] = None,
    no_calls: Annotated[bool, Field(description="Skip call graph (build only)")] = False,
    no_metadata: Annotated[bool, Field(description="Skip L2 metadata (build only)")] = False,
    no_fts: Annotated[bool, Field(description="Skip FTS5 full-text index (build only)")] = False,
    no_synonyms: Annotated[bool, Field(description="Skip object synonyms (build only)")] = False,
    confirm: Annotated[
        str | None,
        Field(
            description="Project password for build/update/drop confirmation. "
            "Ask the user for their project password when server returns approval_required."
        ),
    ] = None,
) -> str:
    """Manage the BSL method index — build, update, get info, or drop.
    build/update run in background and return {"started": true} immediately;
    check progress with info (build_status field). CLI 'rlm-bsl-index' remains synchronous.
    Provide either 'path' (filesystem path) or 'project' (registered project name).
    'build', 'update' and 'drop' require a registered project with password —
    ask the user for the project password."""
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


def _setup_file_logging():
    """Add rotating file handler for HTTP transport mode."""
    from logging.handlers import RotatingFileHandler

    # Use RLM_CONFIG_FILE-derived path if set (Windows service / Session 0)
    config_override = os.environ.get("RLM_CONFIG_FILE")
    if config_override:
        log_dir = pathlib.Path(config_override).parent / "logs"
    else:
        log_dir = pathlib.Path.home() / ".config" / "rlm-tools-bsl" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "server.log"

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


def _warmup_imports():
    """Pre-import heavy modules so first rlm_start is fast. Best-effort."""
    _t0 = time.monotonic()
    try:
        import rlm_tools_bsl.bsl_helpers  # noqa: F401
        import rlm_tools_bsl.bsl_xml_parsers  # noqa: F401
        import rlm_tools_bsl.bsl_index  # noqa: F401
        import rlm_tools_bsl.helpers  # noqa: F401

        warmup_openai_import()
    except Exception:
        logger.debug("warmup: import error (non-critical)", exc_info=True)
    logger.info("warmup: completed in %.1fs", time.monotonic() - _t0)


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

    load_project_env()

    session_manager = build_session_manager_from_env()
    session_manager.on_evict = _release_session_resources

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
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("RLM_PORT", "9000")),
        help="Bind port for HTTP transport (env: RLM_PORT, default: 9000)",
    )

    subparsers = parser.add_subparsers(dest="command")
    service_parser = subparsers.add_parser("service", help="Manage system service (Windows SC / Linux systemd)")
    service_sub = service_parser.add_subparsers(dest="service_action")

    install_p = service_sub.add_parser("install", help="Install and enable the service")
    install_p.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    install_p.add_argument("--port", type=int, default=9000, help="Bind port (default: 9000)")
    install_p.add_argument("--env", default=None, metavar="PATH", help="Path to .env file")

    for _action in ("start", "stop", "status", "uninstall"):
        service_sub.add_parser(_action)

    args = parser.parse_args()

    if args.command == "service":
        from rlm_tools_bsl.service import handle_service_command

        handle_service_command(args)
        return

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

    threading.Thread(target=_warmup_imports, daemon=True).start()
    mcp.run(transport=args.transport)
