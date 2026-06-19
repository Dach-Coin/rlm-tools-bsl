"""Machine-readable BSL symbol provider API.

This module is intentionally query-only. It reads an existing BSL index and
normalizes method/object/file rows into a stable JSON-compatible response for
external consumers such as claude-context.
"""

from __future__ import annotations

import time
import hashlib
from pathlib import Path
from typing import Any, Literal, TypedDict

from rlm_tools_bsl._paths import canonicalize_path
from rlm_tools_bsl.extension_detector import resolve_config_root

PROVIDER_SCHEMA_VERSION = 1
PROVIDER_NAME = "rlm-tools-bsl"
DEFAULT_PROVIDER_LIMIT = 20
MAX_PROVIDER_LIMIT = 100

ProviderStatus = Literal["available", "missing_index", "stale", "busy", "error"]
CandidateKind = Literal["method", "object", "file"]


class ProviderCandidate(TypedDict, total=False):
    kind: CandidateKind
    relativePath: str
    symbolName: str
    declarationKind: str
    startLine: int
    endLine: int
    isExport: bool
    params: str | None
    objectName: str
    objectKind: str
    synonym: str | None
    modulePath: str
    rank: float | int | None
    source: str


class ProviderResponse(TypedDict):
    schemaVersion: int
    provider: str
    status: ProviderStatus
    sourceRoot: str
    query: str
    limit: int
    capabilities: dict[str, bool]
    candidates: list[ProviderCandidate]
    diagnostics: dict[str, Any]


class ProviderExportSymbol(TypedDict, total=False):
    name: str
    declarationKind: str
    startLine: int
    endLine: int
    isExport: bool
    params: str | None


class ProviderExportFile(TypedDict, total=False):
    relativePath: str
    objectName: str
    objectKind: str
    moduleKind: str
    formName: str
    synonyms: list[str]
    symbols: list[ProviderExportSymbol]


class ProviderExportResponse(TypedDict):
    schemaVersion: int
    provider: str
    status: ProviderStatus
    sourceRoot: str
    sourceFingerprint: str
    capabilities: dict[str, bool]
    files: list[ProviderExportFile]
    diagnostics: dict[str, Any]


def normalize_provider_limit(limit: int | None) -> int:
    if limit is None:
        return DEFAULT_PROVIDER_LIMIT
    try:
        parsed = int(limit)
    except (TypeError, ValueError):
        return DEFAULT_PROVIDER_LIMIT
    return max(1, min(MAX_PROVIDER_LIMIT, parsed))


def shape_method_candidate(row: dict[str, Any]) -> ProviderCandidate:
    candidate: ProviderCandidate = {
        "kind": "method",
        "relativePath": _as_rel_path(row.get("module_path") or ""),
        "symbolName": str(row.get("name") or ""),
        "declarationKind": _normalize_declaration_kind(row.get("type")),
        "modulePath": _as_rel_path(row.get("module_path") or ""),
        "source": "methods_fts",
    }
    _set_int(candidate, "startLine", row.get("line"))
    _set_int(candidate, "endLine", row.get("end_line"))
    if "is_export" in row:
        candidate["isExport"] = bool(row.get("is_export"))
    if "params" in row:
        candidate["params"] = row.get("params")
    if row.get("object_name"):
        candidate["objectName"] = str(row["object_name"])
    if "rank" in row:
        candidate["rank"] = row.get("rank")
    return candidate


def shape_object_candidate(row: dict[str, Any]) -> ProviderCandidate:
    candidate: ProviderCandidate = {
        "kind": "object",
        "objectName": str(row.get("object_name") or ""),
        "objectKind": str(row.get("category") or ""),
        "source": "object_synonyms",
    }
    file_path = row.get("file")
    if file_path:
        candidate["relativePath"] = _as_rel_path(file_path)
    if row.get("object_name"):
        candidate["symbolName"] = str(row["object_name"])
    if "synonym" in row:
        candidate["synonym"] = row.get("synonym")
    if "rank" in row:
        candidate["rank"] = row.get("rank")
    return candidate


def shape_file_candidate(rel_path: str) -> ProviderCandidate:
    return {
        "kind": "file",
        "relativePath": _as_rel_path(rel_path),
        "symbolName": Path(rel_path).name,
        "source": "file_paths",
    }


def query_symbol_provider(path: str, query: str, limit: int | None = None) -> ProviderResponse:
    """Query an existing BSL index without building or updating it."""
    started = time.perf_counter()
    effective_limit = normalize_provider_limit(limit)
    source_root = ""
    diagnostics: dict[str, Any] = {}
    candidates: list[ProviderCandidate] = []
    capabilities = {
        "hasMethodsFts": False,
        "hasObjects": False,
        "hasFilePaths": False,
    }

    try:
        source_root = _resolve_source_root(path)
        from rlm_tools_bsl.bsl_index import IndexReader, IndexStatus, check_index_usable

        db_path = _provider_index_db_path(source_root)
        diagnostics["dbPath"] = str(db_path)
        if not db_path.exists():
            return _response(
                status="missing_index",
                source_root=source_root,
                query=query,
                limit=effective_limit,
                capabilities=capabilities,
                candidates=[],
                diagnostics={**diagnostics, "reason": "index file not found"},
                started=started,
            )

        if (db_path.parent / "bsl_index.lock").exists() or (db_path.parent / "method_index.lock").exists():
            return _response(
                status="busy",
                source_root=source_root,
                query=query,
                limit=effective_limit,
                capabilities=capabilities,
                candidates=[],
                diagnostics={**diagnostics, "reason": "index lock file exists"},
                started=started,
            )

        index_status = check_index_usable(db_path, source_root)
        diagnostics["indexStatus"] = index_status.value
        if index_status == IndexStatus.MISSING:
            return _response(
                status="missing_index",
                source_root=source_root,
                query=query,
                limit=effective_limit,
                capabilities=capabilities,
                candidates=[],
                diagnostics=diagnostics,
                started=started,
            )

        reader = IndexReader(db_path)
        try:
            stats = reader.get_statistics()
            capabilities = {
                "hasMethodsFts": bool(reader.has_fts),
                "hasObjects": bool(stats.get("object_synonyms", 0)),
                "hasFilePaths": bool(reader.has_file_paths),
            }
            diagnostics["stats"] = {
                "modules": stats.get("modules", 0),
                "methods": stats.get("methods", 0),
                "filePaths": stats.get("file_paths", 0),
                "objectSynonyms": stats.get("object_synonyms", 0),
            }

            if index_status != IndexStatus.FRESH:
                return _response(
                    status="stale",
                    source_root=source_root,
                    query=query,
                    limit=effective_limit,
                    capabilities=capabilities,
                    candidates=[],
                    diagnostics=diagnostics,
                    started=started,
                )

            per_kind_limit = effective_limit
            candidates.extend(
                shape_method_candidate(row)
                for row in reader.search_methods(query, limit=per_kind_limit)
            )
            object_rows = reader.search_objects(query, limit=per_kind_limit)
            if object_rows:
                candidates.extend(shape_object_candidate(row) for row in object_rows)
            file_rows = reader.find_files_indexed(query, limit=per_kind_limit)
            if file_rows:
                candidates.extend(shape_file_candidate(rel_path) for rel_path in file_rows)
            candidates = _dedupe_candidates(candidates)[:effective_limit]
        finally:
            reader.close()

        return _response(
            status="available",
            source_root=source_root,
            query=query,
            limit=effective_limit,
            capabilities=capabilities,
            candidates=candidates,
            diagnostics=diagnostics,
            started=started,
        )
    except Exception as exc:  # pragma: no cover - exercised through CLI failure tests
        return _response(
            status="error",
            source_root=source_root,
            query=query,
            limit=effective_limit,
            capabilities=capabilities,
            candidates=[],
            diagnostics={**diagnostics, "errorType": type(exc).__name__, "message": str(exc)},
            started=started,
        )


def export_symbol_snapshot(path: str) -> ProviderExportResponse:
    """Export a read-only symbol snapshot from an existing BSL index."""
    started = time.perf_counter()
    source_root = ""
    diagnostics: dict[str, Any] = {}
    capabilities = {
        "hasMethods": False,
        "hasObjects": False,
        "hasFilePaths": False,
    }

    try:
        source_root = _resolve_source_root(path)
        from rlm_tools_bsl.bsl_index import IndexReader, IndexStatus, check_index_usable

        db_path = _provider_index_db_path(source_root)
        diagnostics["dbPath"] = str(db_path)
        if not db_path.exists():
            return _export_response(
                status="missing_index",
                source_root=source_root,
                source_fingerprint="",
                capabilities=capabilities,
                files=[],
                diagnostics={**diagnostics, "reason": "index file not found"},
                started=started,
            )

        if (db_path.parent / "bsl_index.lock").exists() or (db_path.parent / "method_index.lock").exists():
            return _export_response(
                status="busy",
                source_root=source_root,
                source_fingerprint="",
                capabilities=capabilities,
                files=[],
                diagnostics={**diagnostics, "reason": "index lock file exists"},
                started=started,
            )

        index_status = check_index_usable(db_path, source_root)
        diagnostics["indexStatus"] = index_status.value
        if index_status == IndexStatus.MISSING:
            return _export_response(
                status="missing_index",
                source_root=source_root,
                source_fingerprint="",
                capabilities=capabilities,
                files=[],
                diagnostics=diagnostics,
                started=started,
            )

        reader = IndexReader(db_path)
        try:
            stats = reader.get_statistics()
            meta = _read_provider_meta(reader)
            capabilities = {
                "hasMethods": bool(stats.get("methods", 0)),
                "hasObjects": bool(stats.get("object_synonyms", 0)),
                "hasFilePaths": bool(reader.has_file_paths),
            }
            diagnostics["stats"] = _provider_stats(stats)
            source_fingerprint = _source_fingerprint(source_root, stats, meta)

            if index_status != IndexStatus.FRESH:
                return _export_response(
                    status="stale",
                    source_root=source_root,
                    source_fingerprint=source_fingerprint,
                    capabilities=capabilities,
                    files=[],
                    diagnostics=diagnostics,
                    started=started,
                )

            synonyms = _read_object_synonyms(reader)
            files = [
                _shape_export_file(module, reader.get_methods_by_path(module["rel_path"]) or [], synonyms)
                for module in reader.get_all_modules()
            ]
        finally:
            reader.close()

        return _export_response(
            status="available",
            source_root=source_root,
            source_fingerprint=source_fingerprint,
            capabilities=capabilities,
            files=files,
            diagnostics=diagnostics,
            started=started,
        )
    except Exception as exc:  # pragma: no cover - exercised through CLI failure tests
        return _export_response(
            status="error",
            source_root=source_root,
            source_fingerprint="",
            capabilities=capabilities,
            files=[],
            diagnostics={**diagnostics, "errorType": type(exc).__name__, "message": str(exc)},
            started=started,
        )


def _resolve_source_root(raw_path: str) -> str:
    canonical = canonicalize_path(raw_path)
    if not Path(canonical).is_dir():
        raise FileNotFoundError(f"directory not found: {raw_path}")
    effective, candidates = resolve_config_root(canonical)
    if len(candidates) > 1 and effective == canonical:
        raise ValueError(f"multiple main configurations found under {canonical}")
    return effective


def _provider_index_db_path(source_root: str) -> Path:
    """Return bsl_index.db path without triggering legacy DB migration."""
    from rlm_tools_bsl.bsl_index import get_index_dir

    index_dir = get_index_dir(source_root) / hashlib.md5(source_root.encode()).hexdigest()[:12]
    return index_dir / "bsl_index.db"


def _response(
    *,
    status: ProviderStatus,
    source_root: str,
    query: str,
    limit: int,
    capabilities: dict[str, bool],
    candidates: list[ProviderCandidate],
    diagnostics: dict[str, Any],
    started: float,
) -> ProviderResponse:
    return {
        "schemaVersion": PROVIDER_SCHEMA_VERSION,
        "provider": PROVIDER_NAME,
        "status": status,
        "sourceRoot": source_root,
        "query": query,
        "limit": limit,
        "capabilities": capabilities,
        "candidates": candidates,
        "diagnostics": {
            **diagnostics,
            "elapsedMs": round((time.perf_counter() - started) * 1000, 3),
        },
    }


def _export_response(
    *,
    status: ProviderStatus,
    source_root: str,
    source_fingerprint: str,
    capabilities: dict[str, bool],
    files: list[ProviderExportFile],
    diagnostics: dict[str, Any],
    started: float,
) -> ProviderExportResponse:
    return {
        "schemaVersion": PROVIDER_SCHEMA_VERSION,
        "provider": PROVIDER_NAME,
        "status": status,
        "sourceRoot": source_root,
        "sourceFingerprint": source_fingerprint,
        "capabilities": capabilities,
        "files": files,
        "diagnostics": {
            **diagnostics,
            "elapsedMs": round((time.perf_counter() - started) * 1000, 3),
        },
    }


def _as_rel_path(value: Any) -> str:
    return str(value).replace("\\", "/").lstrip("/")


def _set_int(candidate: ProviderCandidate, key: str, value: Any) -> None:
    if value is None:
        return
    try:
        candidate[key] = int(value)  # type: ignore[literal-required]
    except (TypeError, ValueError):
        return


def _dedupe_candidates(candidates: list[ProviderCandidate]) -> list[ProviderCandidate]:
    seen: set[tuple[Any, ...]] = set()
    result: list[ProviderCandidate] = []
    for candidate in candidates:
        key = (
            candidate.get("kind"),
            candidate.get("relativePath"),
            candidate.get("symbolName"),
            candidate.get("startLine"),
            candidate.get("source"),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(candidate)
    return result


def _shape_export_file(
    module: dict[str, Any],
    methods: list[dict[str, Any]],
    synonyms: dict[tuple[str, str], list[str]],
) -> ProviderExportFile:
    rel_path = _as_rel_path(module.get("rel_path") or "")
    item: ProviderExportFile = {
        "relativePath": rel_path,
        "synonyms": [],
        "symbols": [_shape_export_symbol(row) for row in methods],
    }
    if module.get("object_name"):
        item["objectName"] = str(module["object_name"])
    if module.get("category"):
        item["objectKind"] = str(module["category"])
    if module.get("module_type"):
        item["moduleKind"] = str(module["module_type"])
    if module.get("form_name"):
        item["formName"] = str(module["form_name"])

    object_name = item.get("objectName")
    object_kind = item.get("objectKind")
    if object_name and object_kind:
        item["synonyms"] = synonyms.get((object_kind, object_name), [])
    return item


def _shape_export_symbol(row: dict[str, Any]) -> ProviderExportSymbol:
    symbol: ProviderExportSymbol = {
        "name": str(row.get("name") or ""),
        "declarationKind": _normalize_declaration_kind(row.get("type")),
        "isExport": bool(row.get("is_export")),
    }
    _set_export_int(symbol, "startLine", row.get("line"))
    _set_export_int(symbol, "endLine", row.get("end_line"))
    if "params" in row:
        symbol["params"] = row.get("params")
    return symbol


def _set_export_int(symbol: ProviderExportSymbol, key: str, value: Any) -> None:
    if value is None:
        return
    try:
        symbol[key] = int(value)  # type: ignore[literal-required]
    except (TypeError, ValueError):
        return


def _read_provider_meta(reader: Any) -> dict[str, str]:
    try:
        rows = reader._conn.execute("SELECT key, value FROM index_meta").fetchall()
    except Exception:
        return {}
    return {str(row["key"]): str(row["value"]) for row in rows}


def _read_object_synonyms(reader: Any) -> dict[tuple[str, str], list[str]]:
    result: dict[tuple[str, str], list[str]] = {}
    try:
        rows = reader._conn.execute(
            "SELECT category, object_name, synonym FROM object_synonyms ORDER BY category, object_name, synonym"
        ).fetchall()
    except Exception:
        return result
    for row in rows:
        key = (str(row["category"]), str(row["object_name"]))
        synonym = _normalize_export_synonym(row["synonym"])
        bucket = result.setdefault(key, [])
        if synonym not in bucket:
            bucket.append(synonym)
    return result


def _provider_stats(stats: dict[str, Any]) -> dict[str, Any]:
    keys = ("modules", "methods", "exports", "file_paths", "object_synonyms", "bsl_count", "builder_version")
    return {key: stats.get(key, 0) for key in keys}


def _source_fingerprint(source_root: str, stats: dict[str, Any], meta: dict[str, str]) -> str:
    fingerprint_parts = [
        source_root,
        str(stats.get("bsl_count") or ""),
        str(stats.get("modules") or ""),
        str(stats.get("methods") or ""),
        meta.get("paths_hash", ""),
        meta.get("git_head_commit", ""),
        meta.get("builder_version", ""),
    ]
    return hashlib.sha256("\0".join(fingerprint_parts).encode("utf-8")).hexdigest()


def _normalize_declaration_kind(value: Any) -> str:
    raw = str(value or "")
    lowered = raw.lower()
    if lowered in {"процедура", "procedure"}:
        return "procedure"
    if lowered in {"функция", "function"}:
        return "function"
    return raw


def _normalize_export_synonym(value: Any) -> str:
    raw = str(value or "").strip()
    if ": " in raw:
        return raw.split(": ", 1)[1].strip()
    return raw
