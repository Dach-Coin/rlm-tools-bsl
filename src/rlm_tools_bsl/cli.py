"""CLI interface for rlm-bsl-index (method index management).

Usage::

    rlm-bsl-index index build <path> [--no-calls]
    rlm-bsl-index index update <path>
    rlm-bsl-index index info <path>
    rlm-bsl-index index drop <path>
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path


def _maybe_migrate_legacy_index_root() -> None:
    """Best-effort one-shot migration of legacy index directories.

    Runs at the start of every CLI subcommand so that direct CLI invocations
    after a v1.9.1→v1.9.2 upgrade pick up indexes that still live under the
    legacy ``~/.cache/rlm-tools-bsl`` (e.g. when the user was running as a
    Windows service before). Idempotent NOOP afterwards.
    """
    try:
        from rlm_tools_bsl.bsl_index import migrate_legacy_index_root

        migrate_legacy_index_root()
    except Exception:
        # Migration is opportunistic — never block CLI work on it.
        pass


def _resolve_path(raw: str) -> str:
    """Resolve, validate and cf-normalize a base path argument.

    Mirrors the MCP-side ``_normalize_and_validate_path``: a container-style
    path (``src/`` containing ``src/cf/``) is normalized onto the
    configuration root so CLI-built and MCP-built indexes always share the
    same hash directory.
    """
    from rlm_tools_bsl._paths import canonicalize_path
    from rlm_tools_bsl.extension_detector import resolve_config_root

    canonical = canonicalize_path(raw)
    if not Path(canonical).is_dir():
        print(f"Error: directory not found: {raw}", file=sys.stderr)
        sys.exit(1)

    effective, candidates = resolve_config_root(canonical)
    if len(candidates) > 1 and effective == canonical:
        print(
            f"Error: multiple main configurations found under {canonical}. "
            "Point <path> at a specific configuration root, or rename one of the "
            "direct subdirectories to 'cf' to use it as the primary.",
            file=sys.stderr,
        )
        for c in candidates:
            print(f"  - {c.path}", file=sys.stderr)
        sys.exit(1)

    return effective


def _gate_unsupported_format(base_path: str, allow: bool) -> None:
    """Гейт нового построения индекса на чужом формате (v1.32.0).

    Как и в MCP-гейте, ``classify_source`` не используется — дерево обходится
    не более одного раза (дескриптор, затем при необходимости один ``probe_bsl``).
    """
    from rlm_tools_bsl.format_detector import (
        NO_BSL_INDEX_REFUSAL,
        UNSUPPORTED_FORMAT_INDEX_WARNING,
        has_our_format_descriptor,
        probe_bsl,
    )

    if has_our_format_descriptor(base_path):
        return

    if probe_bsl(base_path) != "found":
        print(f"Error: {NO_BSL_INDEX_REFUSAL}", file=sys.stderr)
        sys.exit(1)

    if allow:
        print(f"ВНИМАНИЕ: {UNSUPPORTED_FORMAT_INDEX_WARNING}")
        return

    flag_hint = "Повторите build с флагом --allow-unsupported-format."

    if not sys.stdin.isatty():
        print(f"Error: {UNSUPPORTED_FORMAT_INDEX_WARNING} {flag_hint}", file=sys.stderr)
        sys.exit(1)

    print(f"ВНИМАНИЕ: {UNSUPPORTED_FORMAT_INDEX_WARNING}")
    try:
        answer = input("Продолжить? [y/N]: ").strip().casefold()
    except EOFError:
        # isatty() НЕ доказывает, что есть кому отвечать: mintty/Git Bash, docker
        # без -i, CI с псевдо-tty дают терминал, чтение из которого сразу упирается
        # в EOF. Без этого пользователь получал трейсбек EOFError вместо указания
        # на флаг — то есть ровно там, где подсказка нужнее всего. Предупреждение
        # уже напечатано выше, поэтому здесь только суть и флаг.
        print()  # закрыть строку приглашения
        print(f"Error: ответить на вопрос некому (stdin закрыт). {flag_hint}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print()
        print("Отменено.")
        sys.exit(1)
    if answer not in ("y", "yes", "д", "да"):
        print("Отменено.")
        sys.exit(1)


def _fmt_size(size_bytes: int) -> str:
    """Format file size in human-readable form."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes / (1024 * 1024):.1f} MB"


def _fmt_age(seconds: float) -> str:
    """Format age in human-readable form."""
    if seconds < 60:
        return f"{seconds:.0f}s ago"
    if seconds < 3600:
        return f"{seconds / 60:.0f}m ago"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}h ago"
    return f"{seconds / 86400:.1f}d ago"


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def _cmd_build(args: argparse.Namespace) -> None:
    from rlm_tools_bsl.bsl_index import IndexBuilder

    _maybe_migrate_legacy_index_root()
    base_path = _resolve_path(args.path)
    _gate_unsupported_format(
        base_path,
        allow=getattr(args, "allow_unsupported_format", False),
    )
    build_calls = not args.no_calls
    build_metadata = not args.no_metadata
    build_fts = not args.no_fts
    build_synonyms = not args.no_synonyms

    print(f"Building index for: {base_path}")
    print(f"Call graph:  {'yes' if build_calls else 'no'}")
    print(f"Metadata:    {'yes' if build_metadata else 'no'}")
    print(f"FTS search:  {'yes' if build_fts else 'no'}")
    print(f"Synonyms:    {'yes' if build_synonyms else 'no'}")

    t0 = time.time()
    builder = IndexBuilder()
    try:
        db_path = builder.build(
            base_path,
            build_calls=build_calls,
            build_metadata=build_metadata,
            build_fts=build_fts,
            build_synonyms=build_synonyms,
        )
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    elapsed = time.time() - t0

    # Read back stats — explicit marker check (codex round 18: don't rely on the
    # defensive wrapper turning a partial build into a "successful" zero-stats print).
    from rlm_tools_bsl.bsl_index import IndexReader, index_incomplete, stats_indicate_load_failure

    def _print_incomplete() -> None:
        print(f"\nIndex build did NOT finish cleanly in {elapsed:.1f}s — index is incomplete.")
        print("  Status:   incomplete/building — rebuild required")
        print(f"  DB path:  {db_path}")

    if index_incomplete(db_path):
        _print_incomplete()
        return

    reader = IndexReader(db_path)
    stats = reader.get_statistics()
    reader.close()
    # Post-read recheck (codex High follow-up): a concurrent rebuild may set the marker
    # DURING get_statistics; in the [empty tables + stale meta] window
    # stats_indicate_load_failure is False, so re-check the marker too (mirror rlm_start).
    if index_incomplete(db_path) or stats_indicate_load_failure(stats):
        _print_incomplete()
        return

    db_size = db_path.stat().st_size if db_path.exists() else 0

    print(f"\nIndex built in {elapsed:.1f}s")
    print(f"  Index:    v{stats.get('builder_version', '?')}")
    if stats.get("config_name"):
        print(f"  Config:   {stats['config_name']} {stats.get('config_version', '')}")
    print(f"  Format:   {stats.get('source_format', 'unknown')}")
    print(f"  Modules:  {stats['modules']}")
    print(f"  Methods:  {stats['methods']}")
    print(f"  Calls:    {stats['calls']}")
    print(f"  Exports:  {stats['exports']}")
    if build_metadata:
        print(f"  EventSubs:  {stats.get('event_subscriptions', 0)}")
        print(f"  SchedJobs:  {stats.get('scheduled_jobs', 0)}")
        print(f"  FuncOpts:   {stats.get('functional_options', 0)}")
    if stats.get("object_synonyms"):
        print(f"  Synonyms:   {stats['object_synonyms']}")
    if stats.get("object_attributes"):
        print(f"  Attributes: {stats['object_attributes']}")
    if stats.get("predefined_items"):
        print(f"  Predefined: {stats['predefined_items']}")
    if stats.get("metadata_references"):
        print(f"  MetaRefs:   {stats['metadata_references']}")
    if stats.get("exchange_plan_content"):
        print(f"  ExPlanCnt:  {stats['exchange_plan_content']}")
    if stats.get("defined_types"):
        print(f"  DefTypes:   {stats['defined_types']}")
    if stats.get("characteristic_types"):
        print(f"  CharTypes:  {stats['characteristic_types']}")
    # Code-derived (built even with --no-metadata) — kept outside the metadata block.
    if stats.get("metadata_code_usages"):
        print(f"  CodeUsages: {stats['metadata_code_usages']}")
    if stats.get("file_paths"):
        print(f"  FilePaths:  {stats['file_paths']}")
    print(f"  DB size:  {_fmt_size(db_size)}")
    print(f"  DB path:  {db_path}")


def _cmd_update(args: argparse.Namespace) -> None:
    from rlm_tools_bsl.bsl_index import IndexBuilder, get_index_db_path

    _maybe_migrate_legacy_index_root()
    base_path = _resolve_path(args.path)
    db_path = get_index_db_path(base_path)

    if not db_path.exists():
        print("Error: index not found. Run 'index build' first.", file=sys.stderr)
        sys.exit(1)

    print(f"Incremental update: {base_path}")

    t0 = time.time()
    builder = IndexBuilder()
    try:
        delta = builder.update(base_path)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    elapsed = time.time() - t0

    # Read back stats — explicit marker check (codex round 18).
    from rlm_tools_bsl.bsl_index import IndexReader, index_incomplete, stats_indicate_load_failure

    def _print_incomplete() -> None:
        print(f"\nUpdate did NOT finish cleanly in {elapsed:.1f}s — index is incomplete.")
        print("  Status:  incomplete/building — rebuild required")

    if index_incomplete(db_path):
        _print_incomplete()
        return

    reader = IndexReader(db_path)
    stats = reader.get_statistics()
    reader.close()
    # Post-read recheck (codex High follow-up): a concurrent rebuild may set the marker
    # DURING get_statistics; stats_indicate_load_failure misses the [empty + stale meta]
    # window, so re-check the marker too (mirror rlm_start).
    if index_incomplete(db_path) or stats_indicate_load_failure(stats):
        _print_incomplete()
        return

    print(f"\nUpdated in {elapsed:.1f}s")
    print(f"  Added:   {delta['added']}")
    print(f"  Changed: {delta['changed']}")
    print(f"  Removed: {delta['removed']}")
    print(f"  Fast path: {delta.get('git_fast_path')}")
    fallback_reason = delta.get("git_fallback_reason")
    if fallback_reason:
        print(f"  Fallback: full scan ({fallback_reason})")
    rebuild_reason = delta.get("rebuild_reason")
    if rebuild_reason:
        print(f"  Rebuild: {rebuild_reason}")
    if stats.get("has_metadata"):
        print(f"  EventSubs:  {stats.get('event_subscriptions', 0)}")
        print(f"  SchedJobs:  {stats.get('scheduled_jobs', 0)}")
        print(f"  FuncOpts:   {stats.get('functional_options', 0)}")


def _cmd_info(args: argparse.Namespace) -> None:
    from rlm_tools_bsl.bsl_index import (
        IndexReader,
        IndexStatus,
        check_index_strict,
        get_index_db_path,
        index_incomplete,
        stats_indicate_load_failure,
    )
    from rlm_tools_bsl.cache import _paths_hash

    _maybe_migrate_legacy_index_root()
    base_path = _resolve_path(args.path)
    db_path = get_index_db_path(base_path)

    if not db_path.exists():
        print(f"Index not found: {db_path}")
        sys.exit(0)

    # Incomplete in-place build → report it without opening get_statistics on a partial DB.
    if index_incomplete(db_path):
        print(f"Index: {db_path}")
        print("  Status:   incomplete/building — rebuild required")
        return

    reader = IndexReader(db_path)
    stats = reader.get_statistics()
    reader.close()
    # Post-read recheck (codex High follow-up): a concurrent rebuild may set the marker
    # DURING get_statistics; in the [empty tables + stale meta] window
    # stats_indicate_load_failure is False, so re-check the marker too (mirror rlm_start).
    if index_incomplete(db_path) or stats_indicate_load_failure(stats):
        print(f"Index: {db_path}")
        print("  Status:   incomplete/building — rebuild required")
        return

    db_size = db_path.stat().st_size

    # Freshness check
    base = Path(base_path)
    bsl_files = sorted(base.rglob("*.bsl"))
    rel_paths = [f.relative_to(base).as_posix() for f in bsl_files]
    paths_hash = _paths_hash(rel_paths)

    status = check_index_strict(db_path, len(bsl_files), paths_hash, base_path)

    status_labels = {
        IndexStatus.FRESH: "fresh",
        IndexStatus.STALE: "stale (structure changed)",
        IndexStatus.STALE_AGE: "stale (age)",
        IndexStatus.STALE_CONTENT: "stale (content)",
        IndexStatus.MISSING: "missing",
    }

    print(f"Index: {db_path}")
    if stats.get("config_name"):
        print(f"  Config:   {stats['config_name']} {stats.get('config_version', '')}")
    if stats.get("source_format"):
        print(f"  Format:   {stats['source_format']}")
    print(f"  Status:   {status_labels.get(status, status.value)}")
    print(f"  Modules:  {stats['modules']}")
    print(f"  Methods:  {stats['methods']}")
    print(f"  Calls:    {stats['calls']}")
    print(f"  Exports:  {stats['exports']}")
    if stats.get("has_metadata"):
        print(f"  EventSubs:  {stats.get('event_subscriptions', 0)}")
        print(f"  SchedJobs:  {stats.get('scheduled_jobs', 0)}")
        print(f"  FuncOpts:   {stats.get('functional_options', 0)}")
    elif stats.get("has_metadata") is not None:
        print("  Metadata: not indexed")
    if stats.get("object_synonyms"):
        print(f"  Synonyms:   {stats['object_synonyms']}")
    if stats.get("object_attributes"):
        print(f"  Attributes: {stats['object_attributes']}")
    if stats.get("predefined_items"):
        print(f"  Predefined: {stats['predefined_items']}")
    if stats.get("metadata_references"):
        print(f"  MetaRefs:   {stats['metadata_references']}")
    if stats.get("exchange_plan_content"):
        print(f"  ExPlanCnt:  {stats['exchange_plan_content']}")
    if stats.get("defined_types"):
        print(f"  DefTypes:   {stats['defined_types']}")
    if stats.get("characteristic_types"):
        print(f"  CharTypes:  {stats['characteristic_types']}")
    # Code-derived (built even with --no-metadata) — kept outside the metadata block.
    if stats.get("metadata_code_usages"):
        print(f"  CodeUsages: {stats['metadata_code_usages']}")
    if stats.get("file_paths"):
        print(f"  FilePaths:  {stats['file_paths']}")
    print(f"  FTS:      {'yes' if stats.get('has_fts') else 'no'}")
    if stats.get("git_accelerated"):
        sha_short = stats["git_head_commit"][:8] if stats.get("git_head_commit") else "?"
        print(f"  Git:      yes (commit: {sha_short})")
    elif stats.get("git_head_commit"):
        print("  Git:      no (.git unavailable, was: {})".format(stats["git_head_commit"][:8]))
    else:
        print("  Git:      no")
    print(f"  DB size:  {_fmt_size(db_size)}")

    if stats["built_at"]:
        age = time.time() - stats["built_at"]
        print(f"  Built:    {_fmt_age(age)}")

    print(f"  BSL files on disk: {len(bsl_files)}")


def _cmd_drop(args: argparse.Namespace) -> None:
    from rlm_tools_bsl.bsl_index import get_index_db_path

    _maybe_migrate_legacy_index_root()
    base_path = _resolve_path(args.path)
    db_path = get_index_db_path(base_path)

    if not db_path.exists():
        print("Index not found, nothing to drop.")
        return

    size = db_path.stat().st_size
    db_path.unlink()
    print(f"Index dropped: {db_path} ({_fmt_size(size)})")

    # Remove parent dir if empty
    try:
        db_path.parent.rmdir()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    from rlm_tools_bsl._config import load_project_env

    load_project_env()

    import importlib.metadata

    parser = argparse.ArgumentParser(
        prog="rlm-bsl-index",
        description="1C (BSL) method index management",
    )
    parser.add_argument(
        "--version",
        "-V",
        action="version",
        version=f"%(prog)s {importlib.metadata.version('rlm-tools-bsl')}",
    )
    sub = parser.add_subparsers(dest="group")

    # --- index group ---
    idx_parser = sub.add_parser("index", help="Method index management")
    idx_sub = idx_parser.add_subparsers(dest="command")

    # build
    build_p = idx_sub.add_parser("build", help="Full index build")
    build_p.add_argument("path", help="Root directory of 1C configuration")
    build_p.add_argument("--no-calls", action="store_true", help="Skip call graph")
    build_p.add_argument("--no-metadata", action="store_true", help="Skip metadata tables (ES/SJ/FO)")
    build_p.add_argument("--no-fts", action="store_true", help="Skip FTS5 full-text search index")
    build_p.add_argument("--no-synonyms", action="store_true", help="Skip object synonyms table")
    build_p.add_argument(
        "--allow-unsupported-format",
        action="store_true",
        help="Строить неполный индекс на формате, отличном от cf/edt",
    )

    # update
    update_p = idx_sub.add_parser("update", help="Incremental update by mtime+size")
    update_p.add_argument("path", help="Root directory of 1C configuration")

    # info
    info_p = idx_sub.add_parser("info", help="Index status and statistics")
    info_p.add_argument("path", help="Root directory of 1C configuration")

    # drop
    drop_p = idx_sub.add_parser("drop", help="Delete index")
    drop_p.add_argument("path", help="Root directory of 1C configuration")

    args = parser.parse_args()

    if args.group is None:
        parser.print_help()
        sys.exit(0)

    if args.group == "index":
        if args.command is None:
            idx_parser.print_help()
            sys.exit(0)
        handlers = {
            "build": _cmd_build,
            "update": _cmd_update,
            "info": _cmd_info,
            "drop": _cmd_drop,
        }
        handlers[args.command](args)
    else:
        parser.print_help()
        sys.exit(0)


if __name__ == "__main__":
    main()
