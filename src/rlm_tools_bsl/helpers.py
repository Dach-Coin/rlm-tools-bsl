import collections
import logging
import os
import pathlib
import re
import threading

from rlm_tools_bsl.regex_safety import NESTED_QUANTIFIER_ERROR, has_catastrophic_nesting

logger = logging.getLogger(__name__)

_FILE_CACHE_MAX_SIZE = 500
_GREP_CACHE_MAX_SIZE = 100


_SKIP_DIRS = {
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

_BINARY_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".ico",
    ".pdf",
    ".zip",
    ".tar",
    ".gz",
    ".xz",
    ".bz2",
    ".o",
    ".a",
    ".dylib",
    ".framework",
    ".xcassets",
    ".car",
    ".nib",
    ".storyboardc",
    ".momd",
    ".sqlite",
    ".db",
    ".epf",
    ".erf",
    ".bin",
    ".mxlx",
    ".cmi",
    ".dcss",
    ".dcssca",
}


def _walk_files(root: pathlib.Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
        for fname in filenames:
            if fname.startswith("."):
                continue
            yield pathlib.Path(dirpath) / fname


def scan_bsl_tree(root: pathlib.Path) -> tuple[list[str], int]:
    """Канон ``walk``: прямой обход дерева *root* поверх ``os.scandir``.

    Семантика повторяет прежний обход внутри live-каталога BSL-хелперов (резолв
    directory redirect ВНУТРЬ root + ``seen_dirs``), но дополнительно считает
    ошибки перечисления: молча выпавший каталог раньше был неотличим от каталога
    без модулей, и «пусто» выглядело как доказанный ноль.

    Обход СВОИМ стеком, а не ``os.walk``: тот дёргает ``islink`` на КАЖДЫЙ
    элемент (на боевой ЕРП-конфигурации 106 000 системных вызовов), тогда как
    ``DirEntry`` отдаёт тип из уже прочитанного каталога бесплатно.

    Returns: ``(absolute_paths, enumeration_errors)``.
    """
    errors = 0
    found: list[str] = []
    root_str = str(root)
    stack = [root_str]
    # normcase, а НЕ casefold: на Windows он приводит регистр (там `Alpha` и
    # `alpha` — один каталог), на регистрозависимой ФС оставляет строку как есть
    # (безусловный casefold схлопнул бы два РАЗНЫХ каталога Linux в один).
    seen_dirs: set[str] = {os.path.normcase(root_str)}
    while stack:
        try:
            scan = list(os.scandir(stack.pop()))
        except OSError:
            errors += 1
            continue
        for entry in scan:
            try:
                if entry.is_dir(follow_symlinks=False):
                    if entry.name in _SKIP_DIRS or entry.name.startswith("."):
                        continue
                    target = entry.path
                    # КАТАЛОГ-ПЕРЕНАПРАВЛЕНИЕ: на Windows это не только симлинк,
                    # но и junction, у которого ``is_symlink()`` == False.
                    # Резолвим ОДИН раз на каталог и дальше идём по РАЗРЕШЁННОМУ
                    # пути: цель вне root даст ValueError и будет пропущена, цель
                    # внутри root перечислится ровно как при прямом проходе.
                    if entry.is_symlink() or getattr(entry.stat(follow_symlinks=False), "st_reparse_tag", 0):
                        resolved_dir = pathlib.Path(target).resolve()
                        resolved_dir.relative_to(root)
                        target = str(resolved_dir)
                    key = os.path.normcase(target)
                    if key in seen_dirs:
                        continue
                    seen_dirs.add(key)
                    stack.append(target)
                    continue
                if not entry.name.lower().endswith(".bsl"):
                    continue
                path = entry.path
                # realpath — ТОЛЬКО для симлинка: гард «файл не уводит за пределы»
                # обязан остаться, но платить им за каждый обычный файл незачем.
                if entry.is_symlink():
                    # Симлинк на КАТАЛОГ с именем вида `X.bsl` сюда тоже попадает
                    # (`is_dir(follow_symlinks=False)` у ссылки — False), а
                    # os.walk такое клал в dirnames и модулем НИКОГДА не считал.
                    if entry.is_dir():
                        continue
                    resolved = pathlib.Path(path).resolve()
                    resolved.relative_to(root)
                    path = str(resolved)
            except ValueError:
                # Намеренно исключённая цель вне root — не ошибка полноты.
                continue
            except OSError:
                errors += 1
                continue
            found.append(path)
    return found, errors


def make_helpers(base_path: str, idx_reader=None, *, _private_io: dict | None = None) -> tuple[dict, callable]:
    """Generic (non-BSL) sandbox toolbox.

    ``_private_io`` (v1.34.0) — opt-in sink для ПРИВАТНЫХ status-aware каналов.
    Возвращаемая пара ``(public_helpers, resolve_safe)`` не меняется, приватные
    ключи в namespace/registry не попадают: их забирает только production
    ``Sandbox`` и передаёт напрямую в ``make_bsl_helpers``. Публичный
    ``grep(pattern, path=".")`` остаётся двухаргументным — служебный kwarg из
    кода песочницы по-прежнему даёт ``TypeError``.
    """
    base = pathlib.Path(base_path).resolve()
    _file_cache: collections.OrderedDict[str, str] = collections.OrderedDict()
    _file_cache_lock = threading.Lock()

    def _resolve_safe(path: str) -> pathlib.Path:
        resolved = (base / path).resolve()
        try:
            resolved.relative_to(base)
        except ValueError:
            raise PermissionError(f"Access denied: path '{path}' escapes sandbox root")
        return resolved

    def read_file(path: str) -> str:
        """Read file content as string. Returns str."""
        target = _resolve_safe(path)
        cache_key = str(target)
        with _file_cache_lock:
            if cache_key in _file_cache:
                _file_cache.move_to_end(cache_key)
                return _file_cache[cache_key]
        content = target.read_text(encoding="utf-8-sig", errors="replace")
        with _file_cache_lock:
            _file_cache[cache_key] = content
            if len(_file_cache) > _FILE_CACHE_MAX_SIZE:
                _file_cache.popitem(last=False)
        return content

    def read_files(paths: list[str]) -> dict[str, str]:
        """Read multiple files at once. Returns {path: content} dict."""
        result = {}
        for path in paths:
            try:
                result[path] = read_file(path)
            except (OSError, PermissionError) as e:
                result[path] = f"[error: {e}]"
        return result

    _grep_cache: collections.OrderedDict[tuple[str, str], list[dict]] = collections.OrderedDict()
    _grep_cache_lock = threading.Lock()

    _BROAD_DIR_THRESHOLD = 5000

    def _is_broad_directory(target: pathlib.Path) -> bool:
        """Check if target is a directory with >5000 files (would cause timeout).
        Uses fast os.scandir recursion with early exit."""
        if not target.is_dir():
            return False
        count = 0
        stack = [target]
        while stack:
            current = stack.pop()
            try:
                with os.scandir(current) as entries:
                    for entry in entries:
                        if entry.name.startswith(".") or entry.name in _SKIP_DIRS:
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            count += 1
                            if count > _BROAD_DIR_THRESHOLD:
                                return True
            except OSError:
                continue
        return False

    def _grep_impl(pattern: str, path: str, status: dict | None) -> list[dict]:
        """Общее ядро публичного ``grep`` и приватного ``grep_with_status``.

        Failure-count ведётся ВСЕГДА, даже когда публичная оболочка его не
        отдаёт: иначе публичный вызов первым закешировал бы ошибочный ``[]``, а
        последующий приватный принял бы его за доказанный ноль.
        """
        # Finding #2 (v1.26.0): отсечь явные catastrophic-backtracking паттерны ПЕРВЫМ
        # оператором — до cache-lookup и re.compile. C-движок _sre повиснет на (a+)+b,
        # а Windows-таймаут песочницы (PyThreadState_SetAsyncExc) его не прерывает.
        if has_catastrophic_nesting(pattern):
            raise ValueError(NESTED_QUANTIFIER_ERROR)
        # Статус инициализируется ДО cache lookup: cache-hit — это заведомо
        # безошибочный проход (неуспешные в кеш не кладутся), и его ноль честен.
        if status is not None:
            status["failed_files"] = 0
        cache_key = (pattern, path)
        with _grep_cache_lock:
            if cache_key in _grep_cache:
                _grep_cache.move_to_end(cache_key)
                return _grep_cache[cache_key]

        target = _resolve_safe(path)
        compiled = re.compile(pattern)
        results = []
        failed = 0

        try:
            is_dir = target.is_dir()
        except OSError:
            is_dir = False
        if not is_dir:
            # Конкретный файл (в т.ч. нечитаемый по ACL: is_file() глотает OSError
            # и вернул бы False, после чего путь молча выпадал из области).
            explicit_file = True
            search_paths = [target]
        elif _is_broad_directory(target):
            raise ValueError(
                f"grep on '{path}' would scan too many files and timeout. "
                "Use safe_grep(pattern, 'ModuleHint') or "
                "find_module('name') to get specific file paths first, "
                "then grep(pattern, 'path/to/specific/file.bsl')."
            )
        else:
            explicit_file = False
            search_paths = _walk_files(target)

        for file_path in search_paths:
            try:
                readable = file_path.is_file()
            except OSError:
                readable = False
            if not readable:
                if explicit_file:
                    failed += 1
                continue
            ext = file_path.suffix.lower()
            if ext in _BINARY_EXTENSIONS:
                continue
            try:
                for i, line in enumerate(file_path.read_text(encoding="utf-8-sig", errors="replace").splitlines(), 1):
                    if compiled.search(line):
                        results.append(
                            {
                                "file": str(file_path.relative_to(base)),
                                "line": i,
                                "text": line.strip(),
                            }
                        )
            except (OSError, UnicodeDecodeError):
                failed += 1
                continue

        if failed == 0:
            with _grep_cache_lock:
                _grep_cache[cache_key] = results
                if len(_grep_cache) > _GREP_CACHE_MAX_SIZE:
                    _grep_cache.popitem(last=False)
        if status is not None:
            status["failed_files"] = failed
        return results

    def grep(pattern: str, path: str = ".") -> list[dict]:
        """Search for regex pattern in files. Returns list of dicts {file, line, text}."""
        return _grep_impl(pattern, path, None)

    def grep_with_status(pattern: str, path: str, status: dict) -> list[dict]:
        """ПРИВАТНЫЙ вариант ``grep``: тот же результат + доказуемый failure-count."""
        return _grep_impl(pattern, path, status)

    def grep_summary(pattern: str, path: str = ".") -> str:
        """Grep with compact output grouped by file. Returns a formatted string."""
        results = grep(pattern, path)
        if not results:
            return "No matches found."
        if results and "error" in results[0]:
            return results[0]["error"]

        grouped: dict[str, list[dict]] = {}
        for r in results:
            grouped.setdefault(r["file"], []).append(r)

        lines = [f"{len(results)} matches in {len(grouped)} files:"]
        for file, matches in grouped.items():
            lines.append(f"\n  {file} ({len(matches)} matches):")
            for m in matches:
                lines.append(f"    L{m['line']}: {m['text']}")
        return "\n".join(lines)

    def grep_read(
        pattern: str,
        path: str = ".",
        max_files: int = 10,
        context_lines: int = 0,
    ) -> dict:
        """Grep then auto-read matching files. Returns match info + file contents.

        Args:
            pattern: Regex pattern to search for.
            path: Directory or file to search in.
            max_files: Maximum number of matching files to read (default 10).
            context_lines: Lines of context around each match (default 0).

        Returns:
            Dict with 'matches' (grouped by file) and 'files' (full contents).
        """
        results = grep(pattern, path)
        if not results:
            return {"matches": {}, "files": {}, "summary": "No matches found."}
        if results and "error" in results[0]:
            return {"matches": {}, "files": {}, "summary": results[0]["error"]}

        grouped: dict[str, list[dict]] = {}
        for r in results:
            grouped.setdefault(r["file"], []).append(r)

        file_paths = list(grouped.keys())[:max_files]
        file_contents = {}
        for fp in file_paths:
            try:
                content = read_file(fp)
                if context_lines > 0:
                    content_lines = content.splitlines()
                    relevant = set()
                    for m in grouped[fp]:
                        line_idx = m["line"] - 1
                        start = max(0, line_idx - context_lines)
                        end = min(len(content_lines), line_idx + context_lines + 1)
                        for i in range(start, end):
                            relevant.add(i)
                    excerpts = []
                    for i in sorted(relevant):
                        excerpts.append(f"L{i + 1}: {content_lines[i]}")
                    file_contents[fp] = "\n".join(excerpts)
                else:
                    file_contents[fp] = content
            except (OSError, PermissionError) as e:
                file_contents[fp] = f"[error: {e}]"

        truncated = len(grouped) - len(file_paths) if len(grouped) > max_files else 0
        summary = f"{len(results)} matches in {len(grouped)} files"
        if truncated:
            summary += f" (showing {max_files}, {truncated} more)"

        return {
            "matches": {fp: grouped[fp] for fp in file_paths},
            "files": file_contents,
            "summary": summary,
        }

    def _glob_files_fs(pattern: str) -> list[str]:
        """FS-based glob (original implementation)."""
        safe_matches: list[str] = []
        dir_matches = 0
        for match in base.glob(pattern):
            if not match.is_file():
                if match.is_dir():
                    dir_matches += 1
                continue
            parts = match.relative_to(base).parts
            if any(part in _SKIP_DIRS or part.startswith(".") for part in parts[:-1]):
                continue
            try:
                safe_matches.append(str(match.resolve().relative_to(base)))
            except ValueError:
                continue
        if not safe_matches and dir_matches:
            return [
                f"[hint: pattern '{pattern}' matched {dir_matches} directories but no files. "
                f"Add a file suffix, e.g. '{pattern}/**' or '{pattern}/Module.bsl']"
            ]
        return safe_matches

    def glob_files(pattern: str) -> list[str]:
        """Find files by glob pattern. Returns list of relative path strings.

        Uses SQLite index for supported patterns (instant), falls back to FS otherwise.
        """
        if idx_reader is not None:
            _fallback_reason = None
            try:
                indexed = idx_reader.glob_files(pattern)
            except Exception:
                _fallback_reason = "index_error"
                indexed = None
            if indexed is not None:
                logger.debug("glob_files: indexed pattern=%s results=%d", pattern, len(indexed))
                # Reproduce hint logic: if no file matches, check for dir-like matches
                if not indexed:
                    # Check if pattern without wildcards is a known directory prefix
                    norm = pattern.replace("\\", "/").rstrip("/")
                    if "*" not in norm and "?" not in norm:
                        return [
                            f"[hint: pattern '{pattern}' matched directories but no files. "
                            f"Add a file suffix, e.g. '{pattern}/**' or '{pattern}/Module.bsl']"
                        ]
                # Normalize separators to match FS behavior (backslash on Windows)
                return [p.replace("/", os.sep) for p in indexed]
            # Fallback: pattern unsupported or index error
            if _fallback_reason is None:
                _fallback_reason = "unsupported"
            logger.info("glob_files: FS fallback pattern=%s reason=%s", pattern, _fallback_reason)
        else:
            logger.info("glob_files: FS fallback pattern=%s reason=no_index", pattern)
        return _glob_files_fs(pattern)

    def _tree_fs(path: str = ".", max_depth: int = 3) -> str:
        """FS-based tree (original implementation)."""
        target = _resolve_safe(path)
        lines = []

        def _walk(dir_path: pathlib.Path, prefix: str, depth: int):
            if depth > max_depth:
                return
            try:
                entries = sorted(dir_path.iterdir(), key=lambda e: (not e.is_dir(), e.name))
            except PermissionError:
                return
            visible = [e for e in entries if not e.name.startswith(".") and e.name not in _SKIP_DIRS]
            for i, entry in enumerate(visible):
                connector = "└── " if i == len(visible) - 1 else "├── "
                lines.append(f"{prefix}{connector}{entry.name}")
                if entry.is_dir():
                    extension = "    " if i == len(visible) - 1 else "│   "
                    _walk(entry, prefix + extension, depth + 1)

        lines.append(str(target.relative_to(base)) if target != base else ".")
        _walk(target, "", 0)
        return "\n".join(lines)

    def _tree_from_paths(paths: list[str], root_label: str, prefix_strip: str, max_depth: int) -> str:
        """Build a tree string from a flat list of POSIX paths."""
        # Build a nested dict structure
        tree_dict: dict = {}
        for p in paths:
            if prefix_strip:
                if not p.startswith(prefix_strip + "/"):
                    continue
                p = p[len(prefix_strip) + 1 :]
            parts = p.split("/")
            if len(parts) > max_depth:
                continue
            node = tree_dict
            for part in parts:
                node = node.setdefault(part, {})

        lines = [root_label]

        def _render(node: dict, prefix: str):
            # Sort: directories first (non-empty children), then files
            items = sorted(node.items(), key=lambda kv: (len(kv[1]) == 0, kv[0]))
            for i, (name, children) in enumerate(items):
                connector = "└── " if i == len(items) - 1 else "├── "
                lines.append(f"{prefix}{connector}{name}")
                if children:
                    extension = "    " if i == len(items) - 1 else "│   "
                    _render(children, prefix + extension)

        _render(tree_dict, "")
        return "\n".join(lines)

    def tree(path: str = ".", max_depth: int = 3) -> str:
        """Print directory tree. Returns formatted string.

        Uses SQLite index for fast tree rendering when available.
        """
        if idx_reader is not None:
            try:
                norm_path = path.replace("\\", "/").strip("/") if path != "." else ""
                indexed_paths = idx_reader.tree_paths(norm_path, max_depth)
            except Exception:
                indexed_paths = None
            if indexed_paths is not None:
                root_label = norm_path if norm_path else "."
                return _tree_from_paths(indexed_paths, root_label, norm_path, max_depth)
        return _tree_fs(path, max_depth)

    _file_index: list[str] = []
    _file_index_built = [False]
    _file_index_lock = threading.Lock()

    def _build_file_index():
        if _file_index_built[0]:
            return
        with _file_index_lock:
            if _file_index_built[0]:
                return
            for fpath in _walk_files(base):
                try:
                    _file_index.append(str(fpath.relative_to(base)))
                except ValueError:
                    continue
            _file_index_built[0] = True

    def find_files(name: str) -> list[str]:
        """Find files by substring match in relative path (case-insensitive).

        Uses SQLite index for ranked results when available, falls back to FS scan.
        """
        if idx_reader is not None:
            try:
                indexed = idx_reader.find_files_indexed(name, limit=100)
            except Exception:
                indexed = None
            # Finding #4 (v1.26.0): `if indexed:` (не `is not None`) — честный zero-hit
            # из индекса ([]) теперь уходит в FS-fallback, а не возвращает пусто. Закрывает
            # ТОЛЬКО zero-hit staleness (файл есть на диске, но отсутствует в stale-индексе);
            # partial-hit staleness (индекс отдал старые строки) — вне scope.
            if indexed:
                return [p.replace("/", os.sep) for p in indexed]
        _build_file_index()
        needle = name.lower()
        return [f for f in _file_index if needle in f.lower()][:100]

    def _scan_main_bsl_catalog_status(route_canon: str) -> tuple[list[str], int]:
        """ПРИВАТНЫЙ status-aware producer списка BSL-путей основной конфигурации.

        ``route_canon`` — только ``"glob"`` либо ``"walk"``. Каноны намеренно
        РАЗНЫЕ и не сливаются: ``glob`` обязан сохранить порядок и состав
        прежнего ``glob_files("**/*.bsl")`` (его порядок наблюдаем — он решает,
        какие именно 50 строк отдадут ``find_module``/``find_by_type``), а
        ``walk`` — состав прежнего прямого обхода live-каталога (там порядок не
        наблюдаем, каталог всё равно сортируется сам).
        """
        if route_canon == "glob":
            try:
                return list(glob_files("**/*.bsl")), 0
            except OSError:
                return [], 1
        if route_canon == "walk":
            return scan_bsl_tree(base)
        raise ValueError(f"unknown route_canon: {route_canon!r}")

    if _private_io is not None:
        _private_io["grep_with_status"] = grep_with_status
        _private_io["scan_bsl_catalog_status"] = _scan_main_bsl_catalog_status

    return {
        "read_file": read_file,
        "read_files": read_files,
        "grep": grep,
        "grep_summary": grep_summary,
        "grep_read": grep_read,
        "glob_files": glob_files,
        "tree": tree,
        "find_files": find_files,
    }, _resolve_safe
