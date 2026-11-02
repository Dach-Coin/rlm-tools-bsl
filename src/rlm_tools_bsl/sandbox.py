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

from rlm_tools_bsl._arg_guards import coerce_bound
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

# Методы строки, которые хелпер зовёт на скалярном аргументе. AttributeError «у списка
# нет .strip()» в кадре пакета значит «в хелпер передан список вместо строки» (v1.43.0),
# а не «агент зовёт метод словаря на списке-результате».
_STR_ARG_METHODS = frozenset(
    {
        "strip",
        "lstrip",
        "rstrip",
        "lower",
        "upper",
        "casefold",
        "split",
        "rsplit",
        "startswith",
        "endswith",
        "replace",
        "partition",
        "rpartition",
        "find",
        "rfind",
        "encode",
        "isdigit",
        "isalpha",
    }
)

# Сигнатуры generic-IO хелперов (НЕ в BSL _registry — приходят из make_helpers()).
# Нужны, чтобы kwarg-хинт давал реальную сигнатуру и для grep/read_file/… (в логах
# бенчмарка: grep(..., limit=...) → тупик). Зеркалит server.available_functions.
_GENERIC_HELPER_SIGNATURES: dict[str, dict] = {
    "read_file": {"sig": "read_file(path) -> str"},
    "read_files": {"sig": "read_files(paths) -> dict[path, str]"},
    "grep": {"sig": "grep(pattern, path='.') -> list[dict] keys: file, line, text  # пути через /"},
    "grep_summary": {"sig": "grep_summary(pattern, path='.') -> str  # пути через /"},
    # v1.49.0 (Д15): подпись называет форму ответа — `matches` словарь «файл → записи»,
    # `files` — «файл → текст с номерами»; копия в server._FILE_HELPER_SIGNATURES байт в байт.
    "grep_read": {
        "sig": "grep_read(pattern, path='.', max_files=10, context_lines=0) -> {matches:{file:[{file,line,text}]}, files:{file:numbered}, summary}  # пути через /"
    },
    "glob_files": {"sig": "glob_files(pattern) -> list[str]  # пути через /"},
    "tree": {"sig": "tree(path='.', max_depth=3) -> str"},
    "find_files": {"sig": "find_files(name) -> list[str]  # пути через /"},
}

# v1.49.0 (Д15): подсказку конкретного хелпера выбирает ИСТОЧНИК ошибки, а не текст кода.
# Прежде `"read_procedure" in code` давал «read_procedure возвращает СТРОКУ» на падение
# значения grep_read строкой ниже. Источник — строка кода агента из трассировки и привязки
# имён этой строки по AST кода вызова; переменную прошлого вызова узнаёт только форма
# значения (_grep_read_shaped). Источник не доказан — подсказки конкретного хелпера нет.
# Кадр исполнения несёт `, in <module>` (или имя функции агента); у синтаксической ошибки
# его нет — она случилась до исполнения, и для неё действуют прежние правила.
_AGENT_FRAME_RE = re.compile(r'File "<rlm-sandbox>", line (\d+), in (\S+)')
# Сквозь эти вызовы значение сохраняет источник: res['matches'].items(), res.get('files'). `.get()` — только с одним
# аргументом: запасное значение `.get(ключ, запасное)` — иной источник (ревью Codex).
_SOURCE_TRANSPARENT_METHODS = frozenset({"items", "keys", "values", "get"})
_BRANCH_NODES = tuple(getattr(ast, n) for n in ("If", "Try", "TryStar", "Match") if hasattr(ast, n))
# Тело цикла может не выполниться ни разу: после цикла его привязки (и цель `for`) — как ветвь без `else` (ревью Codex).
_LOOP_NODES = (ast.For, ast.AsyncFor, ast.While)
_COND_NODES = _BRANCH_NODES + _LOOP_NODES
_SCOPE_NODES = (
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.Lambda,
    ast.ClassDef,  # привязка в теле класса — атрибут класса, а не имя модуля
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.GeneratorExp,
)
# Тело исполняется позже объявления — при вызове или переборе: внешние имена читаются тогда же.
_DEFERRED_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.GeneratorExp)
# Имена кадров трассировки, которые называют вид области, а не её имя (`<listcomp>`… — кадры включений до 3.12).
_FRAME_KINDS = {
    "<genexpr>": (ast.GeneratorExp,),
    "<lambda>": (ast.Lambda,),
    "<listcomp>": (ast.ListComp,),
    "<setcomp>": (ast.SetComp,),
    "<dictcomp>": (ast.DictComp,),
}


def _failing_agent_line(error: str, code: str) -> int | None:
    """Номер строки кода агента из ПОСЛЕДНЕГО кадра ``File "<rlm-sandbox>", line N`` трассировки."""
    found = _AGENT_FRAME_RE.findall(error)
    if not found:
        return None
    line_no = int(found[-1][0])
    return line_no if 1 <= line_no <= len(code.splitlines()) else None


def _failing_agent_frame(error: str) -> str | None:
    """Имя ПОСЛЕДНЕГО кадра кода агента: ``<module>``, имя функции, ``<lambda>``, ``<genexpr>``, имя класса —
    исполняющий контекст строки ошибки, который номер строки не различает (шестая перепроверка Codex)."""
    found = _AGENT_FRAME_RE.findall(error)
    return found[-1][1] if found else None


def _grep_read_shaped(value) -> bool:
    """Значение — ответ grep_read: ровно ``dict`` из трёх ключей ровно типа ``str`` — matches, files, summary.
    Диагностика ошибки не исполняет код агента: подкласс dict отсекается до вызова его методов, перебор ключей обычного
    dict их ``__hash__``/``__eq__`` не зовёт, а сравниваются только строки (перепроверка Codex)."""
    if type(value) is not dict or len(value) != 3:
        return False
    keys = list(value)
    return all(type(key) is str for key in keys) and set(keys) == {"matches", "files", "summary"}


@dataclass(eq=False)
class _Binding:
    """Привязка имени в коде вызова: с какой строки действует и какое значение несёт."""

    order: tuple[int, int]  # порядок привязок: где имя получает значение
    anchor: ast.AST  # узел, от которого считаются ветви if/try
    scope: ast.AST | None  # функция или включение, вне которых привязки нет
    visible_from: int  # первая строка, на которой привязка уже действует
    value: ast.AST | None  # выражение-значение; None — источник не выводится
    cutoff: int  # строка, на которой разрешаются имена значения
    # Область, вне которой вычисляется значение: первый итератор включения — в окружающей области (цели включения
    # там не действуют, а имена тела класса видны) — четвёртая перепроверка Codex.
    outside: ast.AST | None = None


class _SourceResolver:
    """Какой хелпер дал значение имени в точке ошибки — по AST кода одного вызова.

    Берётся ПОСЛЕДНЯЯ до строки ошибки привязка (присваивание, цель ``for``, цель
    включения); значение — вызов хелпера сквозь подписки, атрибуты и
    ``.items()/.keys()/.values()/.get()`` либо имя (его источник в точке привязки,
    рекурсивно). Привязка в ветвях ``if``/``try`` и в теле цикла (оно может не выполниться),
    вне которых стоит строка ошибки, доказывает источник, только если все привязки внутри и
    значение до ветвления сходятся; цель ``for`` вне тела цикла (после него и в его ``else``) —
    так же. Без привязки до ветвления в коде вызова значение до него неизвестно (переменная
    прошлого вызова) — кроме ``if``/``else``, обе ветви которого привязывают имя.
    """

    _MAX_DEPTH = 8

    def __init__(self, tree: ast.AST):
        self._parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        self._bindings: dict[str, list[_Binding]] = {}
        self._class_inner: dict[ast.AST, list[ast.AST]] = {}
        self._deferred = [n for n in ast.walk(tree) if isinstance(n, _DEFERRED_NODES)]
        self._scope_nodes = [n for n in ast.walk(tree) if isinstance(n, _SCOPE_NODES)]
        for node in ast.walk(tree):
            self._collect(node)

    def _holds_own(self, node: ast.AST, line_no: int) -> bool:
        """Строка лежит в теле области (не в её заголовке, который вычисляется в окружающей)."""
        if not node.lineno <= line_no <= node.end_lineno:
            return False
        return any(p.lineno <= line_no <= p.end_lineno for p in self._scope_parts(node)[1])

    def _executing_node(self, line_no: int, frame: str) -> ast.AST | None | bool:
        """Область, чей код исполняется в кадре ``frame`` на строке ``line_no``: самая внутренняя с тем же именем кадра
        (``<genexpr>``, ``<lambda>``, ``<listcomp>``… — по виду; иначе функция или класс с этим именем), тело которой
        держит строку. ``<module>`` — модуль (``None``); не опознана — ``False``."""
        if frame == "<module>":
            return None
        kinds = _FRAME_KINDS.get(frame)
        found = None
        for node in self._scope_nodes:
            if kinds is not None:
                if not isinstance(node, kinds):
                    continue
            elif not (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == frame):
                continue
            if self._holds_own(node, line_no) and (found is None or self._within(node, found)):
                found = node
        return False if found is None else found

    def _deferred_at(self, line_no: int, frame: str | None = None) -> ast.AST | None | bool:
        """Самая внутренняя функция, лямбда или генераторное выражение, в чьём исполнении идёт строка ``line_no`` —
        позже объявления, при вызове или переборе: внешние имена читаются тогда же, а не по порядку строк (общестыковая
        вычитка и шестая перепроверка Codex). ``frame`` — имя кадра строки ошибки: по нему — исполняемая область, а от
        неё — ближайшая отложенная: тело класса и включения исполняется вместе с окружающей областью (седьмая
        перепроверка), ``<module>`` и класс верхнего уровня — не отложено (как и первый итератор генератора и значения
        по умолчанию — они в окружающем кадре). Без кадра — по строкам: на строке и заголовок, и тело — ``False``; вне
        тел — ``None``."""
        if frame is not None:
            node = self._executing_node(line_no, frame)
            if node is not False:
                while node is not None and not isinstance(node, _DEFERRED_NODES):
                    node = self._parents.get(node)
                return node
        found = None
        for node in self._deferred:
            if not self._holds_own(node, line_no):
                continue
            if any(e.lineno <= line_no <= e.end_lineno for e in self._scope_parts(node)[0]):
                return False
            if found is None or self._within(node, found):
                found = node
        return found

    @staticmethod
    def _scope_parts(node: ast.AST) -> tuple[list[ast.AST], list[ast.AST]]:
        """Вложенная область: (выражения, которые вычисляются в ОКРУЖАЮЩЕЙ области — декораторы, значения по
        умолчанию, аннотации, базы класса, первый итератор включения; части, которые вычисляются в САМОЙ области)."""
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            a = node.args
            outer = [*a.defaults, *(d for d in a.kw_defaults if d is not None)]
            if isinstance(node, ast.Lambda):
                return outer, [node.body]
            params = (*a.posonlyargs, *a.args, *a.kwonlyargs, a.vararg, a.kwarg)
            outer += [p.annotation for p in params if p is not None and p.annotation is not None]
            outer += [*node.decorator_list, *([node.returns] if node.returns is not None else [])]
            return outer, list(node.body)
        if isinstance(node, ast.ClassDef):
            return [*node.decorator_list, *node.bases, *(k.value for k in node.keywords)], list(node.body)
        first, *rest = node.generators  # включения
        inner = [first.target, *first.ifs, *(part for gen in rest for part in (gen.target, gen.iter, *gen.ifs))]
        inner += [node.key, node.value] if isinstance(node, ast.DictComp) else [node.elt]
        return [first.iter], inner

    def _sees(self, scope: ast.AST | None, line_no: int, outside: ast.AST | None = None) -> bool | None:
        """Привязка области ``scope`` видна на строке ``line_no``: строка внутри области, а у тела класса — ещё и не в
        теле вложенной функции, лямбды, включения или класса: имена тела класса оттуда не видны (вторая перепроверка
        Codex), а в их значениях по умолчанию, декораторах, аннотациях, базах и первом итераторе включения — видны
        (третья). На строке и то, и другое — ``None``: контекст строки неоднозначен. ``outside`` — область, вне
        которой вычисляется значение (первый итератор включения): её привязки не видны, а сама она имена класса не
        скрывает (четвёртая)."""
        if scope is None:
            return True
        if scope is outside:
            return False
        if not scope.lineno <= line_no <= scope.end_lineno:
            return False
        if not isinstance(scope, ast.ClassDef):
            return True
        inner = self._class_inner.get(scope)
        if inner is None:
            inner = self._class_inner[scope] = [
                n for n in ast.walk(scope) if n is not scope and isinstance(n, _SCOPE_NODES)
            ]
        seen = True
        for node in inner:
            if node is outside or not node.lineno <= line_no <= node.end_lineno:
                continue
            outer, own = self._scope_parts(node)
            in_outer = any(e.lineno <= line_no <= e.end_lineno for e in outer)
            in_own = any(p.lineno <= line_no <= p.end_lineno for p in own)
            if in_outer and in_own:
                return None
            if not in_outer:
                seen = False
        return seen

    def bound(self, name: str) -> bool:
        return name in self._bindings

    def _scope_of(self, node: ast.AST) -> ast.AST | None:
        parent = self._parents.get(node)
        while parent is not None and not isinstance(parent, _SCOPE_NODES):
            parent = self._parents.get(parent)
        return parent

    def _add(self, name: str, **fields) -> None:
        self._bindings.setdefault(name, []).append(_Binding(**fields))

    def _collect(self, node: ast.AST) -> None:
        if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
            holder = self._parents.get(node)
            while isinstance(holder, (ast.Tuple, ast.List, ast.Starred)):
                holder = self._parents.get(holder)
            at = (node.lineno, node.col_offset)
            if isinstance(holder, ast.comprehension):
                comp = self._parents[holder]
                self._add(
                    node.id,
                    order=at,
                    anchor=node,
                    scope=comp,
                    visible_from=comp.lineno,
                    value=holder.iter,
                    cutoff=holder.iter.lineno,  # имена итератора — там, где стоит сам итератор
                    outside=comp if holder is comp.generators[0] else None,
                )
            elif isinstance(holder, (ast.For, ast.AsyncFor)):
                self._add(
                    node.id,
                    order=at,
                    anchor=holder,
                    scope=self._scope_of(holder),
                    visible_from=holder.lineno,
                    value=holder.iter,
                    cutoff=holder.lineno,
                )
            elif isinstance(holder, (ast.Assign, ast.AnnAssign, ast.NamedExpr, ast.AugAssign, ast.Delete)):
                carries = isinstance(holder, (ast.Assign, ast.AnnAssign, ast.NamedExpr)) and isinstance(
                    node.ctx, ast.Store
                )
                self._add(
                    node.id,
                    order=(holder.end_lineno, holder.end_col_offset),
                    anchor=holder,
                    scope=self._scope_of(holder),
                    visible_from=holder.end_lineno + 1,
                    value=holder.value if carries else None,
                    cutoff=holder.lineno,
                )
            else:  # with … as и прочие цели — источник не выводится
                self._add(
                    node.id,
                    order=at,
                    anchor=node,
                    scope=self._scope_of(node),
                    visible_from=node.lineno,
                    value=None,
                    cutoff=node.lineno,
                )
            return
        if isinstance(node, ast.arg):
            name = node.arg
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.ExceptHandler)):
            name = node.name
        elif isinstance(node, ast.alias):
            name = node.asname or node.name.split(".")[0]
        elif type(node).__name__ in ("MatchAs", "MatchStar", "MatchMapping"):
            name = getattr(node, "name", None) or getattr(node, "rest", None)
        else:
            return
        if name:
            at = (node.lineno, node.col_offset)
            self._add(
                name,
                order=at,
                anchor=node,
                scope=self._scope_of(node),
                visible_from=node.lineno,
                value=None,
                cutoff=node.lineno,
            )

    @staticmethod
    def _branches(node: ast.AST) -> list[list[ast.AST]]:
        if isinstance(node, (ast.If, *_LOOP_NODES)):
            return [node.body, node.orelse]
        if type(node).__name__ == "Match":
            return [[case] for case in node.cases]
        return [node.body, *([h] for h in node.handlers), node.orelse, node.finalbody]

    @staticmethod
    def _span(stmt: ast.AST) -> tuple[int, int]:
        if hasattr(stmt, "lineno"):
            return stmt.lineno, stmt.end_lineno
        return stmt.pattern.lineno, stmt.body[-1].end_lineno  # match_case без позиции

    @staticmethod
    def _stores(target: ast.AST, name: str) -> bool:
        return any(isinstance(n, ast.Name) and n.id == name and isinstance(n.ctx, ast.Store) for n in ast.walk(target))

    @classmethod
    def _definitely_binds(cls, stmts: list[ast.AST], name: str) -> bool:
        """Операторы привязывают имя на любом пути: прямое присваивание либо ``if``/``else`` (и цепочка ``elif``),
        обе ветви которого привязывают."""
        for stmt in stmts:
            if isinstance(stmt, ast.Assign) and any(cls._stores(t, name) for t in stmt.targets):
                return True
            if isinstance(stmt, ast.AnnAssign) and stmt.value is not None and cls._stores(stmt.target, name):
                return True
            if isinstance(stmt, ast.If) and stmt.orelse and cls._exhaustive(stmt, name):
                return True
        return False

    @classmethod
    def _exhaustive(cls, node: ast.AST, name: str) -> bool:
        """После ``node`` имя привязано наверняка: ``if``/``else``, обе ветви которого его привязывают. Цикл (ноль
        итераций), ``if`` без ``else``, ``try`` и ``match`` — нет."""
        return (
            isinstance(node, ast.If)
            and bool(node.orelse)
            and cls._definitely_binds(node.body, name)
            and cls._definitely_binds(node.orelse, name)
        )

    def _within(self, node: ast.AST, ancestor: ast.AST) -> bool:
        while node is not None:
            if node is ancestor:
                return True
            node = self._parents.get(node)
        return False

    def resolve(
        self,
        name: str,
        line_no: int,
        depth: int = 0,
        seen: frozenset = frozenset(),
        exclude: frozenset = frozenset(),
        outside: ast.AST | None = None,
        frame: str | None = None,
    ) -> str | None:
        visible = []
        for b in self._bindings.get(name, ()):
            if b in exclude or b.visible_from > line_no:
                continue
            sees = self._sees(b.scope, line_no, outside)
            if sees is None:
                return None  # контекст строки неоднозначен — источник не доказан
            if sees:
                visible.append(b)
        if not visible or depth > self._MAX_DEPTH:
            return None
        last = max(visible, key=lambda b: b.order)
        deferred = self._deferred_at(line_no, frame)
        if deferred is False or (
            deferred is not None and (last.scope is None or not self._within(last.scope, deferred))
        ):
            return None  # внешнее имя тела функции читается при её вызове — по порядку строк источник не доказан
        outer = None
        if isinstance(last.anchor, _LOOP_NODES) and not any(
            a <= line_no <= z for a, z in map(self._span, last.anchor.body)
        ):
            outer = last.anchor  # цель `for` действует только в теле: после цикла и в `else` — и значение до цикла
        child, node = last.anchor, self._parents.get(last.anchor)
        while node is not None:
            if isinstance(node, _COND_NODES):
                branches = self._branches(node)
                if not node.lineno <= line_no <= node.end_lineno:
                    outer = node  # строка ошибки вне ветвления — значение пришло из одной из ветвей
                else:
                    of_binding = next((i for i, br in enumerate(branches) if any(s is child for s in br)), None)
                    of_line = next(
                        (i for i, br in enumerate(branches) if any(a <= line_no <= z for a, z in map(self._span, br))),
                        None,
                    )
                    if of_binding is None or of_binding != of_line:
                        return None
                    break
            child, node = node, self._parents.get(node)
        if outer is None:
            return self._source(last, depth, seen)
        inside = [b for b in visible if self._within(b.anchor, outer)]
        sources = {self._source(b, depth, seen) for b in inside}
        if not self._exhaustive(outer, name):  # значение до ветвления доходит туда, где ветвь не привязала имя
            if len(inside) < len(visible):
                # Привязки внутри исключены: цель `for` видна уже со строки заголовка цикла.
                sources.add(
                    self.resolve(name, outer.lineno, depth + 1, seen, exclude=frozenset(inside), outside=outside)
                )
            else:
                sources.add(None)  # в коде вызова до ветвления имя не привязано: значение прошлого вызова неизвестно
        return sources.pop() if len(sources) == 1 else None

    def _source(self, binding: _Binding, depth: int, seen: frozenset) -> str | None:
        if binding.value is None or binding in seen:
            return None
        expr = binding.value
        while True:
            if isinstance(expr, (ast.Subscript, ast.Attribute, ast.Starred)):
                expr = expr.value
            elif (
                isinstance(expr, ast.Call)
                and isinstance(expr.func, ast.Attribute)
                and expr.func.attr in _SOURCE_TRANSPARENT_METHODS
                and not expr.keywords
                and not any(isinstance(arg, ast.Starred) for arg in expr.args)  # `*args` — число не доказано
                and len(expr.args) == (1 if expr.func.attr == "get" else 0)
            ):
                expr = expr.func.value
            else:
                break
        if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Name):
            return expr.func.id
        if isinstance(expr, ast.Name):
            return self.resolve(expr.id, binding.cutoff, depth + 1, seen | {binding}, outside=binding.outside)
        return None


def _value_source(code: str, line_no: int, name: str) -> str | None:
    """Какой хелпер дал значение имени ``name`` в строке ``line_no`` кода агента (None — не доказано)."""
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError):
        return None
    return _SourceResolver(tree).resolve(name, line_no)


@dataclass
class HelperCall:
    name: str
    elapsed: float
    seq: int = 0
    duplicate_of: int | None = None


# Один exec + redirect_stdout за раз на ВЕСЬ процесс: contextlib.redirect_stdout
# подменяет глобальный sys.stdout, поэтому два перекрывшихся inline-execute разных
# Sandbox смешивали вывод (доказанная stdout-гонка v1.28). В process mode на
# worker один Sandbox — конкуренции за замок нет, потеря параллельности касается
# только явного inline fallback (§6.2 плана v1.29.0).
_INLINE_STDOUT_LOCK = threading.Lock()

# Маркер усечения — публичный контракт, байт-в-байт как до v1.29.0.
TRUNCATION_MARKER = "\n... [output truncated]"


class BoundedTextCapture(io.TextIOBase):
    """Текстовый capture с лимитом в СИМВОЛАХ (контракт max_output_chars).

    В отличие от прежнего StringIO + post-hoc среза, перестаёт накапливать
    данные сразу по достижении лимита — огромный print() больше не занимает
    память процесса до обрезки (§3.4 плана). Маркер усечения добавляет
    вызывающий код по флагу ``truncated``.
    """

    def __init__(self, max_chars: int):
        self._max_chars = max_chars
        self._parts: list[str] = []
        self._used_chars = 0
        self.truncated = False

    def writable(self) -> bool:  # pragma: no cover - io.TextIOBase contract
        return True

    def write(self, s: str) -> int:
        if not isinstance(s, str):
            s = str(s)
        if not s:
            return 0
        remaining = self._max_chars - self._used_chars
        if remaining <= 0:
            self.truncated = True
            return len(s)
        chunk = s[:remaining]
        self._parts.append(chunk)
        self._used_chars += len(chunk)
        if len(chunk) < len(s):
            self.truncated = True
        return len(s)

    def flush(self) -> None:
        pass

    def getvalue(self) -> str:
        return "".join(self._parts)


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
        output_capture_factory=None,
        enable_bsl_helpers: bool = True,
        current_config_role: str | None = None,
        current_config_name: str = "",
        current_config_root: str = "",
        extension_name_by_root: dict[str, str] | None = None,
        scan_lease=None,
        index_start_status: dict | None = None,
        main_config_root: str = "",
    ):
        # Топология корней (v1.34.0) проверяется ДО построения namespace и до
        # любого обращения к ридеру/ФС, но ТОЛЬКО на активном BSL-пути:
        # документированный generic-режим (enable_bsl_helpers=False) provenance
        # и totals не использует и сохраняет прежнее право принимать эти поля
        # без BSL-валидации.
        if format_info is not None and enable_bsl_helpers:
            from rlm_tools_bsl.extension_detector import validate_root_topology

            validate_root_topology(base_path, list(extension_paths or []))
        self._base_path = base_path
        self._max_output_chars = max_output_chars
        self._execution_timeout_seconds = execution_timeout_seconds
        self._format_info = format_info
        # Узкий флаг generic-режима (v1.32.0): отключает ТОЛЬКО загрузку
        # BSL-хелперов. Нумерация строк и остальное поведение песочницы —
        # по-прежнему от format_info, чтобы один признак не управлял двумя
        # независимыми вещами. Default True: прямые создания Sandbox
        # (тесты/встраивание) сохраняют прежнее поведение.
        self._enable_bsl_helpers = enable_bsl_helpers
        # Role-aware provenance foundation (v1.34.0): server уже выполнил
        # detect_extension_context, поэтому внутри сессии он не повторяется.
        self._current_config_role = current_config_role
        self._current_config_name = current_config_name or ""
        self._current_config_root = current_config_root or ""
        # v1.46.0 (П11): корень основной рядом с расширением — только имена описателей для проверки существования.
        self._main_config_root = main_config_root or ""
        self._extension_name_by_root = dict(extension_name_by_root or {})
        self._idx_reader = idx_reader
        self._idx_zero_callers_authoritative = idx_zero_callers_authoritative
        # v1.45.0 (Д29): метка свежести и итог выборки старта сессии — для get_index_info
        self._index_start_status = dict(index_start_status) if index_start_status else None
        self._extension_paths = list(extension_paths or [])
        # Process mode подставляет фабрику writer-а в shared-buffer; None → bounded
        # in-memory capture. Возвращаемый объект обязан поддерживать write/flush,
        # getvalue() и атрибут truncated (см. BoundedTextCapture).
        self._output_capture_factory = output_capture_factory
        # v1.40.0: аренда общего бюджета потоков обхода дерева (`_scan_budget`).
        # Inline-сессии процесса делят ОДНУ аренду (слот процесса-сервера), worker —
        # свою; None — прямое создание (тесты/embedding), бюджета нет, как раньше.
        self._scan_lease = scan_lease
        # Defense-in-depth: сериализация execute ОДНОГО Sandbox (parent сериализует
        # сессию своим session execution lock, но прямые пользователи класса/tests
        # не обязаны об этом знать — §14.3 плана).
        self._execute_lock = threading.RLock()
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
        # v1.45.0 (Д26): ОДИНОЧНЫЕ чтения для подсказки `read_files` — `read_file` всегда, `read_procedure`
        # только с именем процедуры строкой (список имён — уже батч, раньше он считался как одиночный).
        self._session_single_read_counts: dict[str, int] = {}
        self._emitted_efficiency_hints: set[str] = set()
        # v1.24.0 #5 — batch nudge granularity is rlm_execute ROUND-TRIPS, not raw
        # invocations: an execute with only 1-2 top-level helper calls is "sparse".
        # Accumulated across the session (NOT cleared in execute(), unlike _helper_calls).
        self._session_sparse_execute_count: int = 0
        # Списочные хелперы, чья выдача упёрлась в limit В ЭТОМ execute:
        # {helper: (returned, limit)}. Очищается вместе с _helper_calls, потому что
        # подсказка обязана прийти в ТОМ ЖЕ ответе, где агент получил срез.
        self._execute_saturated: dict[str, tuple[int, int]] = {}
        # v1.36.0: прогрев живого каталога. Конструктор его НЕ запускает —
        # между возвратом `Sandbox(...)` и появлением backend есть достижимая
        # init-failure граница, на которой поток остался бы без владельца и
        # `_rlm_start` не смог бы передать его reaper-у.
        self._prewarm_thread = None
        self._prewarm_start = None
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

        # Приватные status-aware каналы (v1.34.0): sink остаётся ЛОКАЛЬНЫМ, в
        # namespace уезжает только публичный helper dict.
        private_io: dict = {}
        helpers, self._resolve_safe = make_helpers(
            self._base_path, idx_reader=self._idx_reader, _private_io=private_io, _scan_lease=self._scan_lease
        )
        self._namespace.update(self._wrap_helpers(helpers))

        bsl_helpers: dict = {}
        prewarm = None
        if self._format_info is not None and self._enable_bsl_helpers:
            bsl_helpers = make_bsl_helpers(
                base_path=self._base_path,
                resolve_safe=self._resolve_safe,
                read_file_fn=helpers["read_file"],
                grep_fn=helpers["grep"],
                glob_files_fn=helpers["glob_files"],
                format_info=self._format_info,
                idx_reader=self._idx_reader,
                idx_zero_callers_authoritative=self._idx_zero_callers_authoritative,
                index_start_status=self._index_start_status,
                extension_paths=self._extension_paths,
                grep_status_fn=private_io.get("grep_with_status"),
                catalog_scan_fn=private_io.get("scan_bsl_catalog_status"),
                glob_files_fs_fn=private_io.get("glob_files_fs"),
                current_config_role=self._current_config_role,
                current_config_name=self._current_config_name,
                current_config_root=self._current_config_root,
                main_config_root=self._main_config_root,
                extension_name_by_root=self._extension_name_by_root,
                scan_lease=self._scan_lease,
            )
            # Прогреватель — не хелпер: он не должен ни попасть в namespace
            # агента, ни считаться вызовом хелпера. `_wrap_helpers` приватные
            # ключи НЕ фильтрует (он оборачивает ВСЁ callable), поэтому изымаем
            # ДО него, а не рассчитываем на фильтр.
            prewarm = bsl_helpers.pop("_prewarm_live_catalog", None)
            self._namespace.update(self._wrap_helpers(bsl_helpers))

        if self._format_info is not None:
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

        # Namespace собран полностью — сохраняем callable, но НЕ запускаем:
        # старт делает session-владелец через `_start_owned_prewarm()`.
        self._prewarm_start = prewarm

    def _start_owned_prewarm(self) -> None:
        """Приватный one-shot: вызывается ТОЛЬКО session-владельцем после init.

        Владелец (``InlineSandboxBackend`` либо sandbox-worker) зовёт его последней
        строкой успешной инициализации: после неё падающих стадий конструктора уже
        нет, и поток гарантированно кому-то принадлежит. Прямой ownerless
        ``Sandbox`` фон не начинает вовсе.
        """
        prewarm = self._prewarm_start
        if prewarm is None:
            return
        self._prewarm_start = None
        prewarm()  # RuntimeError Thread.start поглощён внутри best-effort prewarm
        self._prewarm_thread = prewarm.thread()  # handle либо None; lazy сохранён

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
                    if _name == "read_file" or (
                        _name == "read_procedure"
                        and isinstance(args[1] if len(args) > 1 else kwargs.get("proc_name"), str)
                    ):
                        _src = self._session_single_read_counts
                        _src[_name] = _src.get(_name, 0) + 1
                    _fp = _arg_fingerprint(args, kwargs)
                    if _fp is not None:
                        _akey = (_name, _fp)
                        self._session_helper_arg_counts[_akey] = self._session_helper_arg_counts.get(_akey, 0) + 1
                    try:
                        _res = _fn(*args, **kwargs)
                        self._note_saturation(_name, args, kwargs, _res)
                        return _res
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

    # Списочные хелперы, у которых выдача молча режется по limit: {helper: (позиция
    # limit в позиционных аргументах, значение по умолчанию)}. Признака усечения в
    # самом ответе нет и добавить его некуда — это list[dict]. Поэтому сигнал уходит
    # в efficiency_hints конверта rlm_execute: возврат хелпера и stdout не меняются
    # (test_sandbox_helper_return_value_unchanged).
    # v1.34.0: добавлены два хелпера с ЖЁСТКИМ cap, у которых сигнала не было вовсе;
    # у `find_by_type` при этом появился и параллельный count_only-контракт (у
    # `search_regions`/`search_module_headers` он по-прежнему заморожен побайтно).
    # Нудж остаётся только для list-ответа (`isinstance(result, list)`), поэтому
    # dict-`count_only` его закономерно не вызывает.
    _SATURATING_LIST_HELPERS = {
        "search_regions": (1, 200),
        "search_module_headers": (1, 200),
        "search_objects": (1, 50),
        "search_methods": (1, 30),
        "find_by_type": (2, 50),
        "find_module": (3, 50),
        # v1.44.0: свободные места распределяются между источниками, поэтому строк меньше
        # limit — значит, ничего не отброшено; ровно limit — возможно усечение.
        "search": (2, 30),
    }

    def _note_saturation(self, name: str, args: tuple, kwargs: dict, result) -> None:
        """Запомнить, что списочный хелпер вернул РОВНО limit строк.

        Это не доказательство усечения (реальный итог мог совпасть с limit) — текст
        подсказки так и сформулирован. Ложный позитив здесь дешевле пропуска: агент
        строит агрегаты по срезу и получает систематически неверный ответ, а
        `search_regions` при пустом query отдаёт вообще алфавитный префикс.
        """
        spec = self._SATURATING_LIST_HELPERS.get(name)
        if spec is None or not isinstance(result, list):
            return
        pos, default = spec
        limit = kwargs.get("limit", args[pos] if len(args) > pos else default)
        # Эффективный лимит считает ТА ЖЕ функция, что у хелпера (v1.40.0): прежняя
        # собственная копия разбора обязана была совпадать с `_coerce_bound` и уже
        # дважды расходилась с ним (±inf, отрицательная дробь). Вызов с limit="200"
        # хелпер восстанавливает к дефолту, а limit=250.0 — ПРИНИМАЕТ и усекает до
        # 250; подсказка обязана знать ровно это значение.
        limit, _ = coerce_bound(limit, default, "limit", "")
        if limit <= 0:
            # limit=0 для `_coerce_bound` валиден и даёт пустую выдачу. Формально это
            # усечение, но сигнализировать нечего: агент сам попросил ноль строк.
            return
        if len(result) >= limit:
            self._execute_saturated[name] = (len(result), limit)

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
        ``read_files`` / ``reuse_var`` / ``batch`` / ``redundant_get_index_info`` /
        ``list_truncated:<helper>`` (последний — per-execute, остальные session-cumulative)."""
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
        # v1.45.0 (Д26): по счётчику ОДИНОЧНЫХ чтений — вызов списком имён батчем уже является.
        rf = self._session_single_read_counts.get("read_file", 0)
        rp = self._session_single_read_counts.get("read_procedure", 0)
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

        # (4) get_index_info() at session start — builder_version/has_* and the main counts
        # already ship in rlm_start.index, so a discovery call on start wastes a round-trip.
        # v1.49.0 (Д13): текст называет и то, чего в старте НЕТ (modules, regions_count, …) —
        # агент, которому это нужно, не должен читать упрёк как «данные уже есть».
        # Gate on the MINIMAL seq of get_index_info in THIS execute: seq<=2 = 1st/2nd call of
        # the session (start signal). A mid-session call for niche fields (has_* таблиц v16,
        # extension_overrides) has a high seq → no false nudge. Throttled once.
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
                            "builder_version/has_* и основные счетчики (methods, calls, object_synonyms, "
                            "object_attributes_count…) уже пришли в rlm_start.index — не трать execute на "
                            "get_index_info() на старте. Только get_index_info() отдает modules, regions_count, "
                            "module_headers_count, extension_overrides, content_sample и прочие счетчики таблиц "
                            "v12/v16 (exchange_plan_content_count, defined_types_count, … *_count). Доступность "
                            "таблиц v16 (declared_register_records, role_flags, common_module_props, constants, "
                            "templates) — builder_version >= 16 и has_object_attributes; число перехватов — "
                            "get_overrides()['total'] (полное при partial=False)."
                        ),
                    }
                )

        # (5) Списочная выдача упёрлась в limit В ЭТОМ execute. Признака усечения в
        # самом ответе нет (см. _SATURATING_LIST_HELPERS), поэтому агент строит топ-N
        # по срезу и получает неверный ответ: у search_regions с пустым query срез
        # вообще алфавитный.
        #
        # ЭТА подсказка НЕ троттлится по сессии, в отличие от четырёх выше, и
        # `emitted` намеренно не трогает. Те — советы о СТИЛЕ работы ("батчи",
        # "переиспользуй переменную"): их достаточно дать один раз. Здесь же факт
        # относится к КОНКРЕТНОМУ результату: каждый усечённый вызов — свой набор
        # данных, по которому агент может построить неверный агрегат. Однократность
        # ломала бы заявленный контракт «сигнал приходит в том же ответе, где получен
        # срез»: первый же вызов, случайно вернувший ровно limit строк (например
        # limit=1 на существующей единственной строке), съедал бы id, и настоящее
        # усечение дальше в сессии осталось бы беззвучным. Дедуп есть, но в пределах
        # одного execute: `_execute_saturated` — dict по имени хелпера.
        for helper, (returned, limit) in sorted(self._execute_saturated.items()):
            # Порядок выдачи упоминается ТОЛЬКО там, где он и правда не релевантность:
            # search_methods ранжирует по BM25, search_objects — четырёхуровневым
            # ранжированием, и для них общая фраза противоречила бы контракту хелпера.
            if helper == "search_regions":
                fix = (
                    "точное число — search_regions(query, count_only=True)['total'], "
                    "топ-N — search_regions(query, group_by='name')"
                )
                order_note = " Порядок выдачи — не релевантность."
            elif helper == "search_module_headers":
                fix = "точное число — search_module_headers(query, count_only=True)['total']"
                order_note = " Порядок выдачи — не релевантность."
            elif helper == "find_by_type":
                # v1.34.0: у хелпера появился count_only, и совет обязан назвать его,
                # а не «подними limit». Порядок — прямой проход `_index_state`, то есть
                # усекается произвольный по бизнес-смыслу срез, а не «хвост ранжирования».
                fix = (
                    "точное число — find_by_type(category, count_only=True): "
                    "'total' это МОДУЛИ, 'unique_objects' — объекты"
                )
                # v1.45.0 (Д16): с именем точный объект идёт первым; остальное — проход каталога.
                order_note = " С именем точный объект — первым; остальное — проход каталога, не релевантность."
            elif helper == "find_module":
                # count_only здесь НЕ вводится (п.3 даёт только limit), поэтому прежний
                # fix остаётся — но с оговоркой про порядок: это тот же прямой проход.
                fix = "подними limit или сузь запрос"
                order_note = " С именем точный объект — первым; остальное — проход каталога, не релевантность."
            elif helper == "search":
                fix = "сузь scope ('methods'|'objects'|'regions'|'headers'|'attributes'|'predefined') или подними limit"
                order_note = " Строки сгруппированы по источникам."
            else:
                fix = "подними limit или сузь запрос"
                order_note = ""  # ранжированная выдача: усекается хвост, а не случайное
            out.append(
                {
                    "id": f"list_truncated:{helper}",
                    "helper": helper,
                    "count": returned,
                    "trigger": f"{helper} returned {returned} rows == limit {limit}",
                    "message": (
                        f"{helper} вернул ровно limit={limit} строк — выдача, возможно, усечена."
                        f"{order_note} Не строй по ней агрегаты: {fix}."
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
        # Порядок замков единый (§14.3): instance execute lock → global inline
        # stdout lock → redirect/capture → timeout → exec.
        with self._execute_lock:
            return self._execute_locked(code)

    def _execute_locked(self, code: str) -> ExecutionResult:
        self._helper_calls.clear()
        self._execute_saturated.clear()
        blocked = self._validate_readonly(code)
        if blocked is not None:
            return ExecutionResult(
                stdout="",
                error=f"SecurityError: {blocked}",
                variables=self.list_variables(),
                helper_calls=[],
                efficiency_hints=None,
            )
        if self._output_capture_factory is not None:
            stdout_capture = self._output_capture_factory()
        else:
            stdout_capture = BoundedTextCapture(self._max_output_chars)
        error = None

        try:
            with _INLINE_STDOUT_LOCK:
                with contextlib.redirect_stdout(stdout_capture):
                    with self._execution_timeout():
                        # Явное имя вместо дефолтного "<string>": в process-режиме worker
                        # спавнится через multiprocessing `python -c "...spawn_main(...)"`, и у
                        # его bootstrap co_filename тоже "<string>". Тогда traceback-кадр кода
                        # агента (File "<string>", line 1) эхо-строкой показывал bootstrap
                        # worker-а вместо кода агента (косметика, не утечка). Отдельное имя
                        # убирает коллизию. Compile внутри try — SyntaxError ловится как и раньше.
                        exec(compile(code, "<rlm-sandbox>", "exec"), self._namespace)
        except Exception:
            error = traceback.format_exc()
            # generic-IO хелперы (grep/read_file/…) не входят в BSL _registry —
            # домешиваем их сигнатуры, иначе kwarg-хинт для grep(..., limit=...) пуст.
            reg = {**_GENERIC_HELPER_SIGNATURES, **(self._namespace.get("_registry") or {})}
            available = {k for k in self._namespace if not k.startswith("_") and callable(self._namespace.get(k))}
            try:
                error = self._add_error_hints(
                    error, code, available_names=available, registry=reg, namespace=self._namespace
                )
            except Exception:
                # v1.49.0: сбой диагностики не роняет ответ (и воркер) — трассировка без подсказок (перепроверка Codex).
                pass

        stdout = stdout_capture.getvalue()
        if getattr(stdout_capture, "truncated", False):
            stdout = stdout + TRUNCATION_MARKER
        elif len(stdout) > self._max_output_chars:
            # Последний defensive-рубеж для нестандартных capture-объектов;
            # с BoundedTextCapture недостижимо.
            stdout = stdout[: self._max_output_chars] + TRUNCATION_MARKER

        return ExecutionResult(
            stdout=stdout,
            error=error,
            variables=self.list_variables(),
            helper_calls=list(self._helper_calls),
            efficiency_hints=self._compute_efficiency_hints() or None,
        )

    def registry_metadata_snapshot(self) -> dict[str, dict]:
        """JSON-safe снимок metadata реестра хелперов ЭТОЙ сессии, без ``fn``.

        Не вторая независимая проекция registry: каталогом служит существующий
        ``build_helper_metadata_snapshot()`` (bsl_helpers), из которого берутся
        только ключи фактического session ``_registry`` — так условное наличие
        ``git_search`` (auto-режим) отражается честно, а каталог с ``force``
        остаётся только документацией (§14.4 плана). Хелпер сессии, которого нет
        в каталоге, — ошибка инициализации, не повод молча собрать другую схему.
        Возвращаемые структуры — копии: мутация снапшота не трогает registry.
        """
        registry = self._namespace.get("_registry") or {}
        if not registry:
            return {}
        from rlm_tools_bsl.bsl_helpers import build_helper_metadata_snapshot

        catalog = build_helper_metadata_snapshot()
        missing = sorted(name for name in registry if name not in catalog)
        if missing:
            raise RuntimeError(f"session helpers missing from metadata catalog: {missing}")
        return {
            name: {
                "sig": catalog[name]["sig"],
                "cat": catalog[name]["cat"],
                "kw": list(catalog[name]["kw"]),
                "recipe": catalog[name]["recipe"],
            }
            for name in registry
        }

    @staticmethod
    def _add_error_hints(
        error: str,
        code: str,
        available_names: set[str] | None = None,
        registry: dict | None = None,
        namespace: dict | None = None,
    ) -> str:
        """Append actionable hints to common errors.

        ``namespace`` — переменные сессии: по ним узнаётся значение прошлого вызова (Д15)."""
        hints: list[str] = []

        # v1.49.0 (Д15): источник ошибки — строка кода агента из трассировки и привязки
        # её имён (узлы ast.Name этой строки, а не подстроки текста).
        agent_line = _failing_agent_line(error, code)
        agent_frame = _failing_agent_frame(error)
        resolver: _SourceResolver | None = None
        line_names: set[str] = set()
        line_calls: set[str] = set()
        if agent_line is not None:
            # Диагностика не роняет обработку ошибки (ревью Codex): любой сбой разбора — «источник не доказан».
            try:
                tree = ast.parse(code)
                resolver = _SourceResolver(tree)
                for node in ast.walk(tree):
                    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.lineno == agent_line:
                        line_names.add(node.id)
                    elif (
                        isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Name)
                        and node.func.lineno == agent_line
                    ):
                        line_calls.add(node.func.id)
            except Exception:
                resolver, line_names, line_calls = None, set(), set()

        def from_helper(helper: str) -> bool:
            # Строка ошибки зовёт хелпер, либо значение одного из её имён дал он.
            if resolver is None:
                return False
            try:
                return helper in line_calls or any(
                    resolver.resolve(n, agent_line, frame=agent_frame) == helper for n in line_names
                )
            except Exception:
                return False

        def previous_grep_read() -> bool:
            # Имя строки ошибки, не привязанное в коде вызова, — переменная прошлого вызова: узнаётся по форме значения.
            if resolver is None or namespace is None:
                return False
            try:
                return any(not resolver.bound(n) and _grep_read_shaped(namespace.get(n)) for n in line_names)
            except Exception:
                return False

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

        # v1.34.0 — смена формы ответа git_search/safe_grep: список → словарь.
        # Страховка на переходный период: агент со старой привычкой падает ГРОМКО и
        # сразу получает исполнимую поправку. Гейт — по ИМЕНИ хелпера в коде execute:
        # без него общий `KeyError: 0` был бы слишком широким.
        # AttributeError здесь ОБЯЗАТЕЛЕН, а не «ещё один случай для полноты»: ровно
        # его даёт СОБСТВЕННЫЙ рецепт сервера (`for h in hits: h.get('file')`).
        # Соседняя ветка про `params` этот текст тоже ловит, но загейтена на
        # `"params" in code` и говорит про ДРУГОЙ контракт — поэтому ветка отдельная;
        # обе могут добавить свою строку в один ответ, они называют разные хелперы.
        if "git_search" in code or "safe_grep" in code:
            grep_dict_misuse = (
                "string indices must be integers" in error
                or ("AttributeError" in error and "'str' object has no attribute" in error)
                or any(f"KeyError: '{k}'" in error for k in ("file", "line", "text"))
                or "KeyError: 0" in error
                or "KeyError: -1" in error
            )
            if grep_dict_misuse:
                hints.append(
                    "HINT: git_search/safe_grep возвращают СЛОВАРЬ (v1.34.0), а не список: "
                    "строки лежат в res['results'], их число — в res['returned'], признак "
                    "усечения — res['truncated']. Итерируй: "
                    "for r in res['results']: print(r['file'], r['line'], r['text']). "
                    "У git_search ошибка — в постоянном res['error'] (None при успехе)."
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
        # v1.49.0 (Д15): только когда ошибку дало её значение; без кадра кода агента —
        # прежнее правило по тексту кода.
        if "AttributeError" in error and "'str' object has no attribute" in error:
            if from_helper("read_procedure") if agent_line is not None else "read_procedure" in code:
                hints.append(
                    "HINT: read_procedure(path, name) возвращает СТРОКУ (тело метода с номерами "
                    "строк), не dict. Не вызывай .get()/[ключ] на результате — это уже текст."
                )

        # v1.49.0 (Д15): grep_read отдаёт словари «файл → записи» и «файл → текст»;
        # обход matches как списка даёт строку-файл вместо записи. Переменная прошлого
        # вызова (в коде этого вызова не привязана) узнаётся по форме значения.
        grep_read_hinted = False
        grep_read_misuse = (
            "'str' object has no attribute" in error
            or "string indices must be integers" in error
            or "KeyError: 0" in error
            or "KeyError: -1" in error
        )
        if grep_read_misuse and (from_helper("grep_read") or previous_grep_read()):
            grep_read_hinted = True
            hints.append(
                "HINT: grep_read возвращает {matches: {файл: [{file, line, text}]}, files: {файл: текст с "
                "номерами}, summary}: обход — for f, recs in res['matches'].items(): for r in recs: "
                "print(f, r['line'], r['text']); текст файла — res['files'][f]"
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
        # (a) AttributeError у СПИСКА. Два разных прочтения различает последний кадр
        # трассировки: внутри пакета — хелпер получил список там, где ждёт строку (v1.43.0);
        # в коде агента — агент зовёт метод словаря на списке-результате (v1.23.0).
        if "AttributeError" in error and "'list' object has no attribute" in error:
            attr_m = re.search(r"'list' object has no attribute '(\w+)'", error)
            frames = re.findall(r'File "([^"]+)"', error)
            in_helper = bool(frames) and frames[-1] != "<rlm-sandbox>"
            if in_helper and attr_m and attr_m.group(1) in _STR_ARG_METHODS:
                hints.append(
                    "HINT: в хелпер передан СПИСОК там, где ждется строка (у списка нет "
                    f".{attr_m.group(1)}()). Списком принимают имена read_procedure, "
                    "find_callers_context, find_enum_values, find_definition; остальным — по одному: "
                    "for n in names: helper(n)."
                )
            else:
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
        # v1.49.0 (Д15): форму grep_read уже назвала его подсказка — общая лишь уводила бы.
        if (
            "KeyError" in error
            and "slice(" not in error
            and "get_object_full_structure" not in code
            and not grep_read_hinted
        ):
            mk = re.search(r"KeyError: '?([^'\n]+)'?", error)
            bad_key = mk.group(1) if mk else "<key>"
            hints.append(
                f"HINT: KeyError '{bad_key}' — форма результата иная, чем ожидалось. Многие хелперы "
                "возвращают dict с под-списками: find_register_movements → {code_registers, "
                "erp_mechanisms, manager_tables, adapted_registers}; get_object_profile → "
                "{object_name, category, sections:{...}, _meta}; get_overrides → {overrides, total, "
                "truncated, source}. Сверь ключи: print(list(result.keys())) или rlm_help(helpers=['имя'])."
            )

        # Текст обязан называть ФАКТИЧЕСКОЕ правило. Прежний ("only standard library
        # modules are allowed") был неверен: `time`, `os`, `datetime` — ровно stdlib, и они
        # заблокированы. Агент читал подсказку, делал единственный возможный из неё вывод и
        # упирался снова, теряя вызов (поймано приёмо-сдаточным e2e v1.36.0). Состав берётся
        # из самой константы, чтобы текст не мог разъехаться с гейтом.
        if "import" in error.lower() and "restricted" in error.lower():
            hints.append(
                "HINT: импорт ограничен БЕЛЫМ СПИСКОМ, а не «стандартной библиотекой». "
                "Доступны только: " + ", ".join(sorted(ALLOWED_MODULES)) + ". Всё остальное "
                "недоступно, включая stdlib (time, os, sys, datetime, pathlib) — это граница "
                "песочницы, а не сбой. Нужные данные бери готовыми хелперами."
            )

        if hints:
            error = error.rstrip() + "\n\n" + "\n".join(hints)

        return error

    def list_variables(self) -> list[str]:
        return [k for k in self._namespace if not k.startswith("_") and k != "__builtins__"]
