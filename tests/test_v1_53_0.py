"""v1.53.0 — стратегии, рецепты и подписи приведены к реальной работе хелперов.

Агент-фэйсинг слой (стратегии slim/full, рецепты тем, DISAMBIGUATION, подписи и рецепты
хелперов, подсказки на неиндексных и ошибочных путях) сверяется с фактическим поведением
хелперов статически (AST подписей против ``def``, запрет сломанных паттернов, синхронность
копий стратегии) и исполнением: фрагменты, извлечённые из выдач ``rlm_start``, ``rlm_help`` и
``help()``, гоняются в настоящей песочнице на CF+CFE, EDT, обёртках и индексе ``--no-calls``.

Дерево «полной структуры» — world18 из ``tests/test_v1_47_0.py`` (CF и EDT, расширение с
перехватом события формы без базового обработчика) с дополнениями релиза: определяемый тип и
перечисление названы именами-плейсхолдерами рецептов, добавлены роли трех раскладок, планы
обмена трех раскладок, функциональная опция, печатная форма и экспортный метод модуля объекта
со вызывающим. Объекты world18 (`Док`, `Спр`) подставляются в рецепты вместо имен типовой
конфигурации.
"""

from __future__ import annotations

import ast
import builtins
import json
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

import rlm_tools_bsl.bsl_helpers as BH
import rlm_tools_bsl.helpers as H
from rlm_tools_bsl import bsl_knowledge as K
from rlm_tools_bsl.bsl_helpers import build_helper_metadata_snapshot
from rlm_tools_bsl.bsl_index import IndexBuilder, IndexReader, get_index_db_path
from rlm_tools_bsl.bsl_strategy_data import DISAMBIGUATION_PAIRS, FILE_HELPER_NAMES, STRATEGY_SECTIONS
from rlm_tools_bsl.extension_detector import ConfigRole
from rlm_tools_bsl.format_detector import detect_format
from rlm_tools_bsl.sandbox import Sandbox, _GENERIC_HELPER_SIGNATURES
from rlm_tools_bsl.server import (
    _FILE_HELPER_SIGNATURES,
    _rlm_end,
    _rlm_execute,
    _rlm_help_dispatch,
    _rlm_start,
    describe_index_root,
)
from test_v1_47_0 import _cf_obj, _edt_obj, _world18_files, _write_tree
from test_v1_50_0 import CF_DESCRIPTOR, CF_EXT_DESCRIPTOR, _rights, _write

DOCS = Path(__file__).resolve().parents[1] / "docs"

_DT = "ДенежнаяСуммаНеотрицательная"
_ENUM = "СтатусыЗаказовКлиентов"
_FO = "ИспользоватьПродажи"
_ROOT_MODULE = "CommonModules/{name}/Ext/Module.bsl"

# Плейсхолдеры рецептов → имена фикстуры (документы и справочники world18 — `Док` и `Спр`).
_SUBST = (
    ("РеализацияТоваровУслуг", "Док"),
    ("ПриобретениеТоваровУслуг", "Док"),
    ("ВидыПодарочныхСертификатов", "Спр"),
    ("Номенклатура", "Док"),
    ("'Организация'", "'Сумма'"),
    ("'Documents/X", "'Documents/Док"),
)


def _subst(text: str) -> str:
    for old, new in _SUBST:
        text = text.replace(old, new)
    return text


# ═══════════════════════════ Деревья фикстур ═══════════════════════════


def _bsl(*lines: str) -> str:
    return "\n".join(lines) + "\n"


def _rename_world(files: dict[str, str]) -> dict[str, str]:
    """Имена-плейсхолдеры рецептов: определяемый тип и перечисление (в путях и текстах)."""

    def ren(text: str) -> str:
        text = text.replace("ДенежнаяСумма", _DT)
        return re.sub(r"Виды(?!\w)", _ENUM, text)

    return {
        ren(rel): ren(text) if rel.endswith((".xml", ".mdo", ".bsl", ".form")) else text for rel, text in files.items()
    }


_OBJ_TAIL = _bsl("Процедура ЗаполнитьТестовыйДокумент() Экспорт", "\tОбщМод.Экспорт1(5);", "КонецПроцедуры")
# Печатная форма в модуле менеджера и вызывающий экспортного метода модуля объекта.
_MGR_TAIL = _bsl(
    "Процедура ДобавитьКомандыПечати(КомандыПечати, СтандартныеОбработки) Экспорт",
    "\tКомандаПечати = КомандыПечати.Добавить();",
    '\tКомандаПечати.Идентификатор = "Накладная";',
    "\tКомандаПечати.Представление = НСтр(\"ru = 'Накладная'\");",
    "КонецПроцедуры",
    "Процедура ЗаполнитьТестовыеДокументы() Экспорт",
    "\tДок = Документы.Док.СоздатьДокумент();",
    "\tДок.ЗаполнитьТестовыйДокумент();",
    "КонецПроцедуры",
)
_PLAN_MODULE = _bsl("Процедура ПриЗаписи(Отказ) Экспорт", "\tОбщМод.Экспорт1(1);", "КонецПроцедуры")
_EP_CONTENT_CF = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<ExchangePlanContent xmlns="http://v8.1c.ru/8.3/xcf/extrnprops">\n'
    "  <Item>\n    <Metadata>Catalog.Спр</Metadata>\n    <AutoRecord>Deny</AutoRecord>\n  </Item>\n"
    "</ExchangePlanContent>\n"
)
_EP_CONTENT_EDT = ["<content>", "<mdObject>Catalog.Спр</mdObject>", "<autoRecord>Deny</autoRecord>", "</content>"]


def _role_mdo(name: str) -> str:
    return f'<mdclass:Role xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass"><name>{name}</name></mdclass:Role>\n'


def _plan_mdo(name: str, body: list[str] | None = None) -> str:
    return _edt_obj("ExchangePlan", name, body=body or ["<codeAllowed>true</codeAllowed>"])


def _layout_files(fmt: str) -> dict[str, str]:
    """Роли (родная раскладка формата, разнорегистровый дубль `RealRole.xml` / `realrole/realrole.mdo`)
    и планы обмена (`Plain` с модулем и составом, `MdoPlan/MdoPlan.mdo` без `.bsl`, разнорегистровый
    дубль `SHIP.xml` / `Ship/Ship.mdo`; в CF еще третья раскладка `ExtPlan/Ext/ExchangePlan.xml` —
    каталог `Ext/` с XML в EDT-дереве детектор формата считает сигналом CF)."""
    from test_bsl_helpers import PREDEFINED_CF_XML, PREDEFINED_EDT_MDO

    cf = fmt == "cf"
    ext = "Ext/" if cf else ""
    f: dict[str, str] = {}
    if cf:
        f["Roles/Admin.xml"] = _cf_obj("Role", "Admin")
        f["Roles/Admin/Ext/Rights.xml"] = _rights({"Document.Док": ["Read", "Update"]})
        f["ExchangePlans/Plain.xml"] = _cf_obj("ExchangePlan", "Plain")
        f["ExchangePlans/Plain/Ext/Content.xml"] = _EP_CONTENT_CF
        f["ExchangePlans/ExtPlan/Ext/ExchangePlan.xml"] = (
            '<?xml version="1.0" encoding="UTF-8"?>\n' + _cf_obj("ExchangePlan", "ExtPlan").split("\n", 1)[1]
        )
    else:
        f["Roles/Admin/Admin.mdo"] = _role_mdo("Admin")
        f["Roles/Admin/Rights.rights"] = _rights({"Document.Док": ["Read", "Update"]})
        f["ExchangePlans/Plain/Plain.mdo"] = _plan_mdo("Plain", _EP_CONTENT_EDT)
    f[f"ExchangePlans/Plain/{ext}Module.bsl"] = _PLAN_MODULE
    f["Roles/RealRole.xml"] = _cf_obj("Role", "RealRole")
    f["Roles/realrole/realrole.mdo"] = _role_mdo("realrole")
    f["ExchangePlans/MdoPlan/MdoPlan.mdo"] = _plan_mdo("MdoPlan")
    f["ExchangePlans/SHIP.xml"] = _cf_obj("ExchangePlan", "SHIP")
    f["ExchangePlans/Ship/Ship.mdo"] = _plan_mdo("Ship")
    if cf:
        f["ChartsOfCharacteristicTypes/ВидыСубконто/Ext/Predefined.xml"] = PREDEFINED_CF_XML
    else:
        f["ChartsOfCharacteristicTypes/ВидыСубконтоХозрасчетные/ВидыСубконтоХозрасчетные.mdo"] = PREDEFINED_EDT_MDO
    return f


def _extra_files(fmt: str) -> dict[str, str]:
    """Дополнения 1.53.0 к world18; ``fmt`` — 'cf' | 'edt'."""
    cf = fmt == "cf"
    ext = "Ext/" if cf else ""
    f = _layout_files(fmt)
    f[f"Documents/Док/{ext}ManagerModule.bsl"] = _MGR_TAIL
    if cf:
        f["Documents/ДокНепроводимый.xml"] = _cf_obj("Document", "ДокНепроводимый", props=["<Posting>Deny</Posting>"])
        f[f"FunctionalOptions/{_FO}.xml"] = _cf_obj(
            "FunctionalOption",
            _FO,
            synonym="Использовать продажи",
            props=[
                "<Location>Constant.Конст</Location>",
                "<Content>\n<xr:Object>Document.Док</xr:Object>\n<xr:Object>Catalog.Спр</xr:Object>\n</Content>",
            ],
        )
        f["ScheduledJobs/ОбменДанными.xml"] = _cf_obj(
            "ScheduledJob",
            "ОбменДанными",
            props=["<MethodName>CommonModule.ОбщМод.Экспорт1</MethodName>", "<Use>true</Use>"],
        )
    else:
        f["Documents/ДокНепроводимый/ДокНепроводимый.mdo"] = _edt_obj(
            "Document", "ДокНепроводимый", body=["<posting>Deny</posting>"]
        )
        f[f"FunctionalOptions/{_FO}/{_FO}.mdo"] = _edt_obj(
            "FunctionalOption",
            _FO,
            synonym="Использовать продажи",
            body=[
                "<location>Constant.Конст</location>",
                "<content>Document.Док</content>",
                "<content>Catalog.Спр</content>",
            ],
        )
        f["ScheduledJobs/ОбменДанными/ОбменДанными.mdo"] = _edt_obj(
            "ScheduledJob",
            "ОбменДанными",
            body=["<methodName>CommonModule.ОбщМод.Экспорт1</methodName>", "<use>true</use>"],
        )
    return f


def _main_files(fmt: str) -> dict[str, str]:
    files = _rename_world(_world18_files(fmt))
    ext = "Ext/" if fmt == "cf" else ""
    extra = _extra_files(fmt)
    extra[f"Documents/Док/{ext}ObjectModule.bsl"] = files[f"Documents/Док/{ext}ObjectModule.bsl"] + _OBJ_TAIL
    files.update(extra)
    return files


def _root_module(name: str, body: list[str], f: dict[str, str]) -> None:
    f[f"CommonModules/{name}.xml"] = _cf_obj("CommonModule", name, props=["<Server>true</Server>"])
    f[_ROOT_MODULE.format(name=name)] = _bsl(*body)


def _run3_files() -> dict[str, str]:
    """ModRoot.Root() зовут одноименные Run из ModA/ModB/ModC; у тех — 1 / 401 / 202 вызывающих."""
    f = {"Configuration.xml": CF_DESCRIPTOR}
    _root_module("ModRoot", ["Процедура Root() Экспорт", "КонецПроцедуры"], f)
    for tag, count in (("A", 1), ("B", 401), ("C", 202)):
        body = ["Процедура Run() Экспорт", "\tRoot();", "КонецПроцедуры"]
        for i in range(1, count + 1):
            body += [f"Процедура Caller{tag}{i:03d}() Экспорт", "\tRun();", "КонецПроцедуры"]
        _root_module(f"Mod{tag}", body, f)
    return f


_WIDE_ROOT = "WideRoot"
_LONG_MODULE = "ОченьДлинноеИмяОбщегоМодуляДляПроверкиОграниченияПревью"
_LONG_CALLER = "ОченьДлинноеИмяМетодаВызывающегоШирокийКореньДляПроверкиОграниченияПревьюПечати"


def _wide_files() -> dict[str, str]:
    """Широкий корень: WideRoot() зовут 201 метод в 201 модуле с длинными именами; каждого зовут еще 5."""
    f = {"Configuration.xml": CF_DESCRIPTOR}
    _root_module(_WIDE_ROOT, [f"Процедура {_WIDE_ROOT}() Экспорт", "КонецПроцедуры"], f)
    for i in range(1, 202):
        caller = f"{_LONG_CALLER}{i:04d}"
        body = [f"Процедура {caller}() Экспорт", f"\t{_WIDE_ROOT}();", "КонецПроцедуры"]
        for j in range(1, 6):
            deep = f"Обработчик{j}_ДлинноеИмяВторогоУровняВызыва{'а' * 30}{i:04d}"
            body += [f"Процедура {deep}() Экспорт", f"\t{caller}();", "КонецПроцедуры"]
        _root_module(f"{_LONG_MODULE}{i:04d}", body, f)
    return f


def _nocalls_modules() -> dict[str, str]:
    """К основному дереву: 200 модулей, где Root упомянут только в комментарии, и 201-й с реальным вызовом."""
    f: dict[str, str] = {}
    _root_module("ModRoot", ["Процедура Root() Экспорт", "КонецПроцедуры"], f)
    for i in range(1, 202):
        body = ["// Процедура Root() упомянута только в комментарии"]
        if i == 201:
            body = ["Процедура РеальныйВызов() Экспорт", "\tRoot();", "КонецПроцедуры"]
        _root_module(f"Cand{i:03d}", body, f)
    return f


_EXT_DECL_BUDGET = 2000  # _DECL_SCAN_MODULE_BUDGET: проход объявлений по модулям расширений


def _ext_flood_files() -> dict[str, str]:
    """Основная с одним объявлением `ЦелевоеИмя` и расширение с (бюджет + 1) модулем, где оно же объявлено."""
    f: dict[str, str] = {"Configuration.xml": CF_DESCRIPTOR}
    _root_module("Мод", ["Процедура ЦелевоеИмя() Экспорт", "КонецПроцедуры"], f)
    f["../cfe/Расш1/Configuration.xml"] = CF_EXT_DESCRIPTOR
    for i in range(_EXT_DECL_BUDGET + 1):
        f[f"../cfe/Расш1/CommonModules/М{i:04d}/Ext/Module.bsl"] = _bsl(
            "Процедура ЦелевоеИмя() Экспорт", "КонецПроцедуры"
        )
    return f


# ═══════════════════════════ Мир и сессии ═══════════════════════════


class _Session:
    """Сессия настоящего `_rlm_start`: код исполняется `_rlm_execute` в песочнице сервера."""

    def __init__(self, data: dict):
        self.sid = data["session_id"]
        self.data = data

    def run(self, code: str) -> dict:
        return json.loads(_rlm_execute(self.sid, code, "compact"))

    def ok(self, code: str) -> dict:
        """Фрагмент исполнен без исключения: ответ с `stdout`."""
        out = self.run(code)
        assert not out["error"], f"фрагмент упал:\n{out['error']}\n--- код ---\n{code}"
        return out

    def text(self, code: str) -> str:
        return self.ok(code)["stdout"].strip()

    def json(self, code: str):
        return json.loads(self.text("import json\n" + code))

    def value(self, snippet: str, expression: str):
        """Исполнить сниппет (он печатает свою сводку) и вернуть значение выражения его переменных."""
        self.ok(snippet)
        return self.json(f"print(json.dumps({expression}, ensure_ascii=False))\n")

    def __enter__(self) -> _Session:
        return self

    def __exit__(self, *exc) -> None:
        _rlm_end(self.sid)


class _World:
    """Дерево + индекс + фабрика сессий. Индекс строится один раз на корень, в каталог, который видит сессия."""

    def __init__(self, base: Path, root: Path, files: dict[str, str] | None = None, **build_kw):
        self.base = base
        self.root = root
        self.idx = base / "idx"
        self.build_kw = build_kw
        self._dbs: dict[Path, Path] = {}
        if files is not None:
            _write_tree(root, files)

    def build(self, root: Path) -> Path:
        if root not in self._dbs:
            with pytest.MonkeyPatch.context() as mp:
                mp.setenv("RLM_INDEX_DIR", str(self.idx))
                self._dbs[root] = IndexBuilder().build(str(root), **self.build_kw)
        return self._dbs[root]

    def session(self, monkeypatch: pytest.MonkeyPatch, *, indexed: bool = True, path: Path | None = None, **start_kw):
        if indexed:
            self.build(path or self.root)
            monkeypatch.setenv("RLM_INDEX_DIR", str(self.idx))
        else:
            monkeypatch.setenv("RLM_INDEX_DIR", str(self.base / "idx_absent"))
        data = json.loads(
            _rlm_start(
                path=str(path or self.root),
                query=start_kw.pop("query", ""),
                effort="high",
                domains=["весь каталог"],
                max_execute_calls=200,
                **start_kw,
            )
        )
        assert "session_id" in data, data
        return _Session(data)


@pytest.fixture(scope="module")
def worlds(tmp_path_factory) -> dict[str, _World]:
    made: dict[str, _World] = {}
    trees = (("cf", _main_files("cf")), ("edt", _main_files("edt")), ("run3", _run3_files()), ("wide", _wide_files()))
    trees += (
        ("nocalls", {**_main_files("cf"), **_nocalls_modules()}),  # то же дерево, индекс без графа вызовов
        ("flood", _ext_flood_files()),
    )
    for name, files in trees:
        base = tmp_path_factory.mktemp(f"w1530_{name}")
        made[name] = _World(base, base / "cf", files, **({"build_calls": False} if name == "nocalls" else {}))
    return made


class _Direct:
    """Прямой `Sandbox` на каталоге-контейнере (встраивание): база — контейнер, пути — с префиксом."""

    def __init__(self, base: Path, *, indexed: bool):
        self.reader = None
        if indexed:
            with pytest.MonkeyPatch.context() as mp:
                mp.setenv("RLM_INDEX_DIR", str(base.parent / f"{base.name}_idx"))
                self.reader = IndexReader(IndexBuilder().build(str(base)))
        self.box = Sandbox(base_path=str(base), format_info=detect_format(str(base)), idx_reader=self.reader)

    def ok(self, code: str) -> str:
        result = self.box.execute(code)
        assert not result.error, f"фрагмент упал:\n{result.error}\n--- код ---\n{code}"
        return result.stdout.strip()

    def json(self, code: str) -> object:
        return json.loads(self.ok("import json\n" + code))

    def value(self, snippet: str, expression: str):
        """Исполнить сниппет (он печатает свою сводку) и вернуть значение выражения его переменных."""
        self.ok(snippet)
        return self.json(f"print(json.dumps({expression}, ensure_ascii=False))\n")

    def __enter__(self) -> _Direct:
        return self

    def __exit__(self, *exc) -> None:
        if self.reader is not None:
            self.reader.close()


# ═══════════════════════════ Извлечение фрагментов ═══════════════════════════


def _dedent(lines: list[str]) -> str:
    cut = min((len(ln) - len(ln.lstrip()) for ln in lines if ln.strip()), default=0)
    return "\n".join(ln[cut:] for ln in lines) + "\n"


def _block(lines: list[str], first: str) -> str:
    """Непрерывный блок кода: с первой строки, начинающейся с ``first``, до ближайшего комментария,
    пустой строки или конца."""
    starts = [i for i, ln in enumerate(lines) if ln.strip().startswith(first) and not ln.lstrip().startswith("#")]
    assert starts, f"нет фрагмента, начинающегося с {first!r}:\n" + "\n".join(lines)
    block: list[str] = []
    for ln in lines[starts[0] :]:
        stripped = ln.strip()
        if not stripped or stripped.startswith("#"):
            break
        block.append(ln)
    return _dedent(block)


def _fragment(recipe: str, first: str) -> str:
    return _block(recipe.split("\n"), first)


def _recipe_code(recipe: str) -> str:
    """Весь код рецепта `help()` без заголовка и строк-комментариев."""
    return _dedent([ln for ln in recipe.split("\n")[1:] if ln.strip() and not ln.lstrip().startswith("#")])


def _help(**kw) -> dict:
    return json.loads(_rlm_help_dispatch(**kw))["result"]


def _code_hint(topic: str) -> str:
    return _subst(_help(topic=topic, format="full")["code_hint"])


def _help_recipe(session: _Session, name: str) -> str:
    """Рецепт `help(name)` так, как его видит агент в песочнице."""
    return session.text(f"print(help({name!r}))")


def _xml_error(session: _Session, path: str) -> str:
    """Текст FileNotFoundError подсказки разрешения пути к описателю."""
    return session.text(
        "try:\n"
        f"    parse_object_xml({path!r})\n"
        "except FileNotFoundError as e:\n"
        "    print(str(e))\n"
        "else:\n"
        "    print('RESOLVED')\n"
    )


def _tail(message: str) -> str:
    assert message != "RESOLVED" and "Попробуйте " in message, message
    return message.split("Попробуйте ", 1)[1]


_ROLES_HEAD = "pfx, by_k = '', {}"
_PLANS_HEAD = "pfx, c, ep = '', 'ExchangePlans', {}"


def _with_pfx(fragment: str, head: str, pfx: str) -> str:
    """Первую строку сниппета (объявление ``pfx``) заменить на явный префикс."""
    assert fragment.count(head) == 1, fragment
    return fragment.replace(head, head.replace("''", repr(pfx), 1), 1)


def _roles_fragment_from(recipe: str, pfx: str = "") -> str:
    fragment = _fragment(recipe, _ROLES_HEAD)
    return _with_pfx(fragment, _ROLES_HEAD, pfx) if pfx else fragment


def _roles_fragment(session: _Session, pfx: str = "") -> str:
    return _roles_fragment_from(_help_recipe(session, "find_roles"), pfx)


def _roles_fragment_any(pfx: str = "") -> str:
    """Тот же фрагмент по тексту рецепта реестра — для прямой `Sandbox` без сессии сервера."""
    return _roles_fragment_from(build_helper_metadata_snapshot()["find_roles"]["recipe"], pfx)


def _plans_fragment(pfx: str = "") -> str:
    fragment = _fragment(_code_hint("интеграция"), _PLANS_HEAD)
    return _with_pfx(fragment, _PLANS_HEAD, pfx) if pfx else fragment


def _hierarchy_paging(session: _Session) -> str:
    return _fragment(_help_recipe(session, "find_call_hierarchy"), "paged = {}")


# ═══════════════════════════ Подписи: AST ═══════════════════════════

_RENAMED = (
    "find_callers_context",
    "find_callers",
    "analyze_document_flow",
    "find_custom_modifications",
    "find_event_subscriptions",
    "find_register_movements",
    "find_register_writers",
    "find_based_on_documents",
    "find_print_forms",
    "find_functional_options",
    "find_roles",
    "extract_procedures",
    "get_module_outline",
    "read_procedure",
    "find_definition",
    "find_enum_values",
    "get_object_full_structure",
    "search_objects",
    "search_regions",
    "search_module_headers",
)
# Документированный алиас: функция принимает оба имени.
_SIG_ALIASES = {"find_by_type": {"meta_type|category": ("meta_type", "category")}}
# Параметры `def`, которые в подпись не входят: внутренние (`_…`) и служебный `numbered`.
_HIDDEN_PARAMS = frozenset({"numbered"})
_SIG_PARAMS_RE = re.compile(r"^(\w+)\((.*?)\)\s*->", re.S)
_ITEM_RE = re.compile(r"^([A-Za-z_]\w*)\s*(=(.*))?$", re.S)


def _split_top(text: str) -> list[str]:
    out, depth, cur, quote = [], 0, [], None
    for ch in text:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            out.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    if "".join(cur).strip():
        out.append("".join(cur).strip())
    return out


def _public_params(node: ast.FunctionDef) -> list[tuple[str, str | None]]:
    """Публичные параметры `def` (позиционные и keyword-only) с repr литерального дефолта или None."""
    a = node.args
    positional = a.posonlyargs + a.args
    defaults: list[ast.expr | None] = [None] * (len(positional) - len(a.defaults)) + list(a.defaults)
    pairs = [(p.arg, d) for p, d in zip(positional, defaults)]
    pairs += [(p.arg, d) for p, d in zip(a.kwonlyargs, a.kw_defaults)]
    return [
        (name, ast.unparse(d) if isinstance(d, ast.Constant) else None)
        for name, d in pairs
        if not name.startswith("_") and name not in _HIDDEN_PARAMS
    ]


def _nested_defs(module_name: str, path: Path) -> dict[str, ast.FunctionDef]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    outer = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == module_name)
    return {n.name: n for n in ast.walk(outer) if isinstance(n, ast.FunctionDef)}


def _registry() -> list[tuple[str, str, str]]:
    """[(хелпер, имя функции, sig)] по вызовам `_reg(...)` внутри `make_bsl_helpers`."""
    outer = _nested_defs("make_bsl_helpers", Path(BH.__file__))["make_bsl_helpers"]
    out = []
    for call in ast.walk(outer):
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == "_reg":
            name, fn, sig = call.args[:3]
            out.append((ast.literal_eval(name), fn.id, ast.literal_eval(sig)))
    return out


def _sig_items(sig: str) -> list[str]:
    match = _SIG_PARAMS_RE.match(sig)
    assert match, f"sig без списка параметров и стрелки: {sig[:90]!r}"
    return _split_top(match.group(2))


def _sig_params(sig: str) -> str:
    return _SIG_PARAMS_RE.match(sig).group(2)


def _file_helper_sigs() -> list[str]:
    return [*_FILE_HELPER_SIGNATURES, *(entry["sig"] for entry in _GENERIC_HELPER_SIGNATURES.values())]


def _doc_sig(helper: str) -> str:
    """Подпись для документации — из единого источника (реестр BSL-хелперов или файловые подписи)."""
    snap = build_helper_metadata_snapshot()
    sig = snap[helper]["sig"] if helper in snap else next(s for s in _file_helper_sigs() if s.startswith(f"{helper}("))
    return f"{helper}({_sig_params(sig)})"


class TestSignatureMatchesDef:
    """Каждый параметр из `sig` принимается реальным `def` как keyword — и наоборот."""

    def test_every_sig_parameter_is_accepted_as_keyword(self):
        bad: list[str] = []
        defs = _nested_defs("make_bsl_helpers", Path(BH.__file__))
        for helper, fn, sig in _registry():
            real = {name for name, _ in _public_params(defs[fn])}
            for item in _sig_items(sig):
                match = _ITEM_RE.match(item)
                if item in _SIG_ALIASES.get(helper, {}):  # документированный алиас: функция принимает оба имени
                    accepted = _SIG_ALIASES[helper][item]
                elif match:
                    accepted = (match.group(1),)
                else:
                    bad.append(f"{helper}: {item!r} — не имя параметра (псевдо-синтаксис или литерал)")
                    continue
                for name in accepted:
                    if name not in real:
                        bad.append(f"{helper}: параметр {name!r} отвергается def {fn}{ast.unparse(defs[fn].args)}")
        # Файловые хелперы: обе копии подписей (сервер и песочница) — против `make_helpers`.
        file_defs = _nested_defs("make_helpers", Path(H.__file__))
        file_sigs = _file_helper_sigs()
        assert len(file_sigs) == 16  # две копии по восьми хелперам
        for sig in file_sigs:
            match = _SIG_PARAMS_RE.match(sig)
            assert match, sig
            real = {name for name, _ in _public_params(file_defs[match.group(1)])}
            for item in _split_top(match.group(2)):
                item_match = _ITEM_RE.match(item)
                if not item_match or item_match.group(1) not in real:
                    bad.append(f"файловый хелпер: {item!r} в {sig!r} — не параметр def {match.group(1)}")
        assert not bad, "\n".join(bad)

    def test_sig_lists_every_public_parameter_with_its_default(self):
        defs = _nested_defs("make_bsl_helpers", Path(BH.__file__))
        bad: list[str] = []
        for helper, fn, sig in _registry():
            shown = _sig_items(sig)
            names = {m.group(1) for m in filter(None, (_ITEM_RE.match(item) for item in shown))}
            names |= {n for pair in _SIG_ALIASES.get(helper, {}).values() for n in pair}
            for name, default in _public_params(defs[fn]):
                if name not in names:
                    bad.append(f"{helper}: параметр {name!r} нет в sig")
                elif default is not None and f"{name}={default}" not in shown:
                    bad.append(f"{helper}: в sig нет {name}={default}")
        assert not bad, "\n".join(bad)


# ═══════════════════════════ Документация ═══════════════════════════


def _stale_call(helper: str, *names: str) -> re.Pattern:
    """Вызов хелпера со старым именем первого параметра. После имени — НЕ буква: ``proc`` не матчит
    ``proc_name``, ``obj`` — ``object_name``, ``doc`` — ``document_name`` (иначе правильная подпись
    сама считалась бы устаревшей)."""

    return re.compile(rf"{helper}\(\s*(?:{'|'.join(names)})(?!\w)")


# Строки — буквальные формы; регулярки — вызовы со старым именем параметра.
_STALE_DOC_FORMS = (
    _stale_call("find_callers_context", "proc"),
    _stale_call("find_callers", "proc", "name", "hint"),
    "read_procedure(path, name)",
    "read_procedure(ext_path, name)",
    _stale_call("analyze_document_flow", "doc", "doc_name"),
    _stale_call("find_custom_modifications", "obj", "obj_name"),
    _stale_call("find_event_subscriptions", "obj", "obj_name"),
    _stale_call("find_register_movements", "doc", "doc_name", "document"),
    _stale_call("find_register_writers", "reg", "reg_name", "register"),
    _stale_call("find_based_on_documents", "doc", "doc_name"),
    _stale_call("find_print_forms", "obj", "obj_name"),
    _stale_call("find_functional_options", "obj", "obj_name", "name"),
    _stale_call("find_roles", "obj", "obj_name"),
    _stale_call("find_references_to_object", "obj", "ref", "qualified"),
    _stale_call("find_code_usages", "obj", "ref", "qualified"),
    _stale_call("find_enum_values", "name"),
    "find_enum_values(name)",
    "extract_procedures(path|object_name)",
    "get_module_outline(path|object_name)",
    "proc_name(str|list)",
    "name(str|list)",
    "enum_name(str|list)",
    "get_object_full_structure(name)`",
    "search_objects(query, limit",
    "search_regions(query, limit",
    "search_module_headers(query, limit",
    "find_files(name, limit)",
    "find_callers_context(proc_name, module_hint, offset, limit)",
)
_FS_ROUTE_AS_EXACT_FALSE = re.compile(r"FS-маршрут\w*\s*\([^)]*exact_available\s*=\s*False")


def _stale_hits(doc: str) -> list[str]:
    hits = []
    for form in _STALE_DOC_FORMS:
        if form.search(doc) if isinstance(form, re.Pattern) else form in doc:
            hits.append(form.pattern if isinstance(form, re.Pattern) else form)
    return hits


class TestDocsSync:
    """Документация называет те же подписи и контракты, что и код."""

    def test_helpers_doc_signatures_match_the_registry(self):
        doc = (DOCS / "HELPERS.md").read_text(encoding="utf-8")
        missing = [sig for helper in (*_RENAMED, "find_files") if (sig := f"`{_doc_sig(helper)}`") not in doc]
        stale = {
            name: hits
            for name in ("HELPERS.md", "INDEXING.md", "full_analysis_prompt.md")
            if (hits := _stale_hits((DOCS / name).read_text(encoding="utf-8")))
        }
        assert not missing and not stale, f"в HELPERS.md нет: {missing}; устарели: {stale}"
        # Добор усечённых узлов find_call_hierarchy — циклом по has_more (а не одиночным offset=200).
        hier_line = next(ln for ln in doc.splitlines() if ln.startswith("- `find_call_hierarchy("))
        assert "offset=200, limit=200" not in hier_line, hier_line
        assert "has_more" in hier_line and "help('find_call_hierarchy')" in hier_line, hier_line
        # Интеграционный промпт не ограничивается маской ExchangePlans/*.xml и покрывает все три раскладки.
        prompt = (DOCS / "full_analysis_prompt.md").read_text(encoding="utf-8")
        assert "glob_files('ExchangePlans/*.xml')" not in prompt
        plans_line = next(ln for ln in prompt.splitlines() if "Собери список планов по описателям" in ln)
        assert "Ext/" in plans_line and ".mdo" in plans_line and "help('интеграция')" in plans_line, plans_line
        # Промпт find_code_usages: несуществующая типизированная ссылка -> ValueError,
        # total=0, partial=False — только для существующего объекта без обращений.
        code_usages_sec = prompt.split("find_code_usages('Документ.РеализацияТоваровУслуг')", 1)[1].split(
            "\n# Control-Flow", 1
        )[0]
        assert "('Документ.ТакогоНетВПринципе') — ожидается ValueError" in code_usages_sec, code_usages_sec
        assert "| Несуществующий объект | `total=0`" not in code_usages_sec
        assert "существующего объекта без обращений" in code_usages_sec

    def test_helpers_doc_does_not_tie_file_route_to_exact_available_false(self):
        doc = (DOCS / "HELPERS.md").read_text(encoding="utf-8")
        assert not _FS_ROUTE_AS_EXACT_FALSE.search(doc)
        line = next(ln for ln in doc.splitlines() if "exact_available=False" in ln and "--no-calls" in ln)
        assert "v13" in line and "context" in line, line

    def test_docs_show_no_broken_helper_calls(self):
        broken = {
            "find_ext_overrides() без аргумента": re.compile(r"find_ext_overrides\(\s*\)"),
            "атрибутный доступ к ключу строки": re.compile(r"\brow\.\w+"),
            "атрибут у результата-словаря": re.compile(r"\)\.is_postable"),
        }
        hits = [
            f"{path.name}: {what}"
            for path in sorted(DOCS.glob("*.md"))
            for what, pattern in broken.items()
            if pattern.search(path.read_text(encoding="utf-8"))
        ]
        assert not hits, hits

    def test_architecture_example_binds_its_names(self):
        doc = (DOCS / "ARCHITECTURE.md").read_text(encoding="utf-8")
        example = doc.split("## Пример работы агента", 1)[1].split("\n## ", 1)[0]
        fenced = re.findall(r"```\n(.*?)```", example, re.S)
        assert fenced, "в примере нет блока кода"
        helpers = set(build_helper_metadata_snapshot()) | set(FILE_HELPER_NAMES)
        # Каждый «Агент → rlm_execute …» — отдельный вызов: строки с отступом до ближайшей строки прозы.
        calls: list[list[str]] = []
        current: list[str] | None = None  # строки текущего вызова; None — между вызовами (проза)
        for line in fenced[0].splitlines():
            if line.startswith("Агент → rlm_execute"):
                current = []
                calls.append(current)
            elif current is not None and (not line.strip() or line[0] in " \t"):
                if line.strip():
                    current.append(re.sub(r"#.*", "", line).rstrip())
            elif line.strip():
                current = None  # проза между вызовами — не код
        assert len(calls) >= 2 and all(calls), calls
        bound: set[str] = set()
        unbound: list[str] = []
        for chunk in calls:
            tree = ast.parse(textwrap.dedent("\n".join(chunk)))
            bound |= {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
            unbound += [
                n.id
                for n in ast.walk(tree)
                if isinstance(n, ast.Name)
                and isinstance(n.ctx, ast.Load)
                and n.id not in bound | helpers | set(dir(builtins))
            ]
        assert not unbound, f"в примере читаются необъявленные имена: {sorted(set(unbound))}"
        assert {"export_names", "c3"} <= bound  # обе правки на месте: имя объявлено до использования
        assert (
            "полный граф" not in example.lower()
            and "has_more" in example
            and "offset" in example
            and "help('find_call_hierarchy')" in example
        )

    def test_extension_live_comparison_states_same_object_and_root(self):
        step = K._BUSINESS_RECIPES["расширения"]["full"][6]
        assert "тому же объекту" in step and "ext_path" in step, step
        prompt = (DOCS / "full_analysis_prompt.md").read_text(encoding="utf-8")
        line = next(ln for ln in prompt.splitlines() if "| Live fallback |" in ln)
        assert "get_overrides('ИмяОбъекта')" in line and "ext_path" in line, line


# ═══════════════════════════ Агент-фэйсинг тексты ═══════════════════════════

_IDX_STATS = {"methods": 1000, "calls": 500, "builder_version": "19", "config_name": "X"}


@pytest.fixture(scope="module")
def fmt_info(tmp_path_factory):
    root = tmp_path_factory.mktemp("fmt1530")
    _write(root / "Configuration.xml", "<Configuration/>")
    return detect_format(str(root))


def _full_strategy(fmt_info, query: str = "") -> str:
    return K._build_full_strategy(
        "high", fmt_info, registry=build_helper_metadata_snapshot(), idx_stats=_IDX_STATS, query=query
    )


def _extension_blocks() -> dict[str, str]:
    main = SimpleNamespace(
        current=SimpleNamespace(role=ConfigRole.MAIN, name="Тест", purpose="", name_prefix=""),
        nearby_extensions=[SimpleNamespace(name="Расш1", name_prefix="Расш1_", path="/tmp/cfe/Расш1")],
        nearby_main=None,
    )
    own = SimpleNamespace(
        current=SimpleNamespace(role=ConfigRole.EXTENSION, name="Расш1", purpose="Customization", name_prefix="Расш1_"),
        nearby_extensions=[],
        nearby_main=SimpleNamespace(name="Тест", path="/tmp/cf"),
    )
    row = {"object_name": "Док", "annotation": "После", "target_method": "ПриЗаписи"}
    return {
        "расширения (MAIN)": K._extension_strategy(main, {}),
        "расширения (MAIN, slim)": K._extension_strategy(main, {}, slim=True),
        "расширения (EXTENSION)": K._extension_strategy(own, {"self": [row]}),
    }


def _agent_texts(fmt_info) -> dict[str, str]:
    """Все агент-фэйсинг тексты: {источник: текст}."""
    snap = build_helper_metadata_snapshot()
    out: dict[str, str] = {f"sig {name}": entry["sig"] for name, entry in snap.items()}
    out.update({f"recipe {name}": entry.get("recipe") or "" for name, entry in snap.items()})
    for topic, recipe in K._BUSINESS_RECIPES.items():
        for form in ("compact", "full"):
            out[f"тема {topic} {form}"] = "\n".join(recipe.get(form) or [])
        if recipe.get("code_hint"):
            out[f"тема {topic} code_hint"] = recipe["code_hint"]
    out.update({f"секция {key}": text for key, text in STRATEGY_SECTIONS.items()})
    out["_STRATEGY_HEADER"] = K._STRATEGY_HEADER
    for query in ("", *K._BUSINESS_RECIPES):
        out[f"full-стратегия {query or '(none)'}"] = _full_strategy(fmt_info, query)
    out.update(_extension_blocks())
    return out


# Тексты, которые агент видит и в RLM_STRATEGY_MODE=full, где инструмента rlm_help нет.
_FULL_VISIBLE = ("sig ", "recipe ", "тема ", "_STRATEGY_HEADER", "full-стратегия", "расширения")
# Единственное разрешенное упоминание команды сборки: явный запрет политики в блоке «индекса нет».
_POLICY_BAN = re.compile(r"NEVER call rlm_index\(action='build'\) yourself")


def _hits(texts: dict[str, str], pattern: re.Pattern) -> list[str]:
    return [f"{name}: {m.group(0)!r}" for name, text in texts.items() for m in pattern.finditer(text)]


def _no_hits(texts: dict[str, str], patterns: dict[str, re.Pattern]) -> None:
    hits = [f"{what} → {hit}" for what, pattern in patterns.items() for hit in _hits(texts, pattern)]
    assert not hits, "\n".join(hits)


def _looks_like_code(step: str) -> str:
    """Кодовая часть шага рецепта (до стрелки-пояснения); проза дает пустую строку."""
    head = step.split(" → ")[0].strip()
    assignment = r"[A-Za-z_]\w*(?:\[[^\]]*\])?\s*=\s*(?:[A-Za-z_]\w*\(|[\[{(\"'])"
    return head if head.startswith(("for ", "print(")) or re.match(assignment, head) else ""


def _header_block(a: str, b: str) -> str:
    """Локальный блок пары в `== DISAMBIGUATION ==` full-стратегии: от заголовка пары до следующего заголовка."""
    lines = K._STRATEGY_HEADER.split("== DISAMBIGUATION ==", 1)[1].splitlines()
    words = re.compile(r"[A-Za-z_]\w*")

    def heading(line: str) -> bool:
        return (
            line == line.lstrip()
            and " vs " in line
            and line.rstrip().endswith(":")
            and {a, b} <= set(words.findall(line))
        )

    start = next(i for i, ln in enumerate(lines) if heading(ln))
    out = [lines[start]]
    for line in lines[start + 1 :]:
        if heading(line) or line.startswith("== "):
            break
        out.append(line)
    return "\n".join(out)


_PAIR_FACTS = {
    ("find_call_hierarchy", "find_callers_context"): (
        "caller_is_export",
        "file",
        "is_export",
        "module_path",
        "context",
    ),
    ("find_callers", "find_callers_context"): ("max_files=20", "caller_is_export"),
    ("find_register_movements", "analyze_document_flow"): (".get('is_postable')",),
    ("find_register_movements", "find_register_writers"): (".get('is_postable')",),
}
_MOVEMENT_PAIRS = [pair for pair in _PAIR_FACTS if pair[0] == "find_register_movements"]


class TestNoBrokenRecipePatterns:
    """Буквальная вставка текста в `rlm_execute` не падает и не ведет агента менять индекс."""

    def test_recipe_code_lines_are_valid_python(self):
        checked = 0
        for topic, recipe in K._BUSINESS_RECIPES.items():
            sources = [("code_hint", recipe.get("code_hint") or "")]
            sources += [
                (f"{form}[{i}]", step) for form in ("compact", "full") for i, step in enumerate(recipe.get(form) or [])
            ]
            for where, text in sources:
                code = text if where == "code_hint" else _looks_like_code(text)
                if not code:
                    continue
                try:
                    ast.parse(code)
                except SyntaxError as exc:
                    pytest.fail(f"тема {topic!r}, {where} — не Python: {exc}\n{text}")
                checked += 1
        assert checked >= 12, checked  # проверка не выродилась: кодовых строк хватает

    def test_all_helper_recipes_are_valid_python_and_do_not_shadow_helpers(self):
        snap = build_helper_metadata_snapshot()
        all_helpers = set(snap) | set(FILE_HELPER_NAMES) | {"git_search", "help"}
        bad: list[str] = []
        for name, entry in sorted(snap.items()):
            recipe = entry.get("recipe") or ""
            if not recipe:
                continue
            code = _recipe_code(recipe)
            try:
                tree = ast.parse(code)
            except SyntaxError as exc:
                bad.append(f"рецепт {name}: синтаксис/отступы — {exc}")
                continue
            assigned = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
            shadowed = sorted(assigned & all_helpers)
            if shadowed:
                bad.append(f"рецепт {name}: затеняет хелпер(ы) {shadowed}")
        for topic, data in sorted(K._BUSINESS_RECIPES.items()):
            sources = [("code_hint", data.get("code_hint") or "")]
            sources += [
                (f"{form}[{i}]", _looks_like_code(step))
                for form in ("compact", "full")
                for i, step in enumerate(data.get(form) or [])
            ]
            for where, code in sources:
                if not code:
                    continue
                tree = ast.parse(code)
                assigned = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
                shadowed = sorted(assigned & all_helpers)
                if shadowed:
                    bad.append(f"тема {topic!r} {where}: затеняет хелпер(ы) {shadowed}")
        assert not bad, "\n".join(bad)

    def test_no_broken_calls_and_dict_access(self, fmt_info):
        _no_hits(
            _agent_texts(fmt_info),
            {
                "find_ext_overrides() без аргумента": re.compile(r"find_ext_overrides\(\s*\)"),
                "атрибутный доступ к словарю row.key": re.compile(r"\brow\.\w+"),
                "атрибут у результата-вызова ().is_postable": re.compile(r"\)\.is_postable"),
                # `(?![\w(])`: имя атрибута кончается здесь — вызовы методов (`d['k'].items()`, `.get(…)`)
                # регулярка с одним `(?!\()` ловила откатом `\w+` до `item`/`ge`.
                "псевдоатрибут после индекса x['y'].z": re.compile(r"\w+\['[^'\]]+'\]\.\w+(?![\w(])"),
                "live_scan.truncated": re.compile(r"\blive_scan\.truncated\b"),
                "delegates_expanded[i].status": re.compile(r"delegates_expanded\[i\]\.status"),
                "parse_object_xml(...)['content']": re.compile(r"parse_object_xml\([^()\n]*\)\[['\"]content['\"]"),
                "find_by_type('Roles')": re.compile(r"find_by_type\(\s*['\"]Roles['\"]"),
                "Ext/Document.xml в примере пути": re.compile(r"Ext/Document\.xml"),
            },
        )

    def test_no_index_commands_addressed_to_the_agent(self, fmt_info):
        texts = {name: _POLICY_BAN.sub("", text) for name, text in _agent_texts(fmt_info).items()}
        texts["bsl_helpers.py (подсказки)"] = Path(BH.__file__).read_text(encoding="utf-8")
        _no_hits(
            texts,
            {
                "rlm_index(action='build'": re.compile(r"rlm_index\(action=\\?'build"),
                "rlm_index build": re.compile(r"rlm_index build\b"),
                "(rlm_index update)": re.compile(r"\(rlm_index update\)"),
                "(rlm_index)": re.compile(r"\(rlm_index\)"),
            },
        )
        advice = {
            name: text
            for name, text in texts.items()
            if name.startswith(("секция coverage", "тема ", "bsl_helpers.py"))
        }
        _no_hits(
            advice,
            {
                "собери его / собери индекс": re.compile(r"\bсобери (?:его|индекс)\b", re.I),
                "пересобери индекс": re.compile(r"\bпересобери индекс\b", re.I),
                "пересобери с нужной опцией": re.compile(r"\bпересобери с нужной опцией\b", re.I),
                "«индекс старый» по одному partial=True": re.compile(r"индекс старый"),
            },
        )

    def test_functional_option_budget_hint_does_not_order_a_rebuild(self):
        source = Path(BH.__file__).read_text(encoding="utf-8")
        # Ветка `scope_partial` при непустом object_name: код-скан всегда живой, индекс на него не влияет.
        assert "code-скан уперся в бюджет модулей или потолок совпадений" in source
        assert "rlm_index build" not in source

    def test_no_pointer_to_rlm_help_where_it_is_not_registered(self, fmt_info):
        texts = _agent_texts(fmt_info)
        _no_hits(texts, {"позиционный вызов MCP-тула": re.compile(r"rlm_help\(\s*['\"]")})
        _no_hits(
            {name: text for name, text in texts.items() if name.startswith(_FULL_VISIBLE)},
            {"rlm_help( там, где тула нет (full, рецепты хелперов)": re.compile(r"rlm_help\(")},
        )

    def test_stale_parameter_names_are_gone(self, fmt_info):
        stale = {
            "find_callers_context": ("proc",),
            "find_callers": ("proc",),
            "analyze_document_flow": ("doc", "doc_name"),
            "find_custom_modifications": ("obj", "obj_name"),
            "find_event_subscriptions": ("obj",),
            "find_register_movements": ("doc", "doc_name"),
            "find_register_writers": ("reg", "reg_name"),
            "find_based_on_documents": ("doc", "doc_name"),
            "find_print_forms": ("obj", "obj_name"),
            "find_functional_options": ("obj", "obj_name", "name"),
            "find_roles": ("obj", "obj_name"),
        }
        patterns = {
            f"{helper}({old})": re.compile(rf"\b{helper}\(\s*(?:{'|'.join(old)})\b") for helper, old in stale.items()
        }
        patterns.update(
            {
                "default limit=20 у find_callers": re.compile(r"default limit=20"),
                "read_procedure(path, name)": re.compile(r"read_procedure\(path, name\b"),
                "аргумент с типом arg(str|list)": re.compile(r"\w+\(str\|list\)"),
                "альтернатива path|object_name": re.compile(r"\bpath\|object_name\b"),
                # Имя, которого больше нет ни у одного `def`, — и в прозе рецепта, а не только в вызове.
                "устаревшее имя параметра в тексте": re.compile(r"\b(?:obj_name|doc_name|reg_name)\b"),
                # Позиционный плейсхолдер — имя из подписи (`object_ref`, `name`), а не прежнее короткое.
                "плейсхолдер вне подписи у хелперов ссылок": re.compile(
                    r"find_(?:references_to_object|code_usages)\(\s*(?:obj|ref|qualified)(?!\w)"
                ),
                "get_object_full_structure(doc": re.compile(r"get_object_full_structure\(\s*doc(?!\w)"),
            }
        )
        _no_hits(_agent_texts(fmt_info), patterns)

    def test_caller_rows_contract_is_stated(self):
        snap = build_helper_metadata_snapshot()
        assert "caller_is_export" in snap["find_callers_context"]["sig"]
        assert "caller_name, object_name, category, is_export" not in snap["find_callers_context"]["recipe"]
        assert "caller_is_export" in snap["find_callers"]["recipe"]
        wrong = (
            "caller_name, object_name, category, is_export",
            "контекст вызова (line/text)",
            "контекстом строк",
            "те же ключи",
            "те же метаданные",
        )
        for pair in (("find_call_hierarchy", "find_callers_context"), ("find_callers", "find_callers_context")):
            entry = next(e for e in DISAMBIGUATION_PAIRS if tuple(e["pair"]) == pair)
            copies = {
                "STRATEGY_SECTIONS": "\n".join(str(v) for k, v in entry.items() if k != "pair"),
                "_STRATEGY_HEADER": _header_block(*pair),
            }
            for where, text in copies.items():
                assert "caller_is_export" in text and "context" in text, (where, pair)
                if pair[0] == "find_call_hierarchy":
                    assert "module_path" in text and "is_export" in text and "file" in text, (where, pair)
                for form in wrong:
                    assert form not in text, (where, pair, form)
        compact = K._BUSINESS_RECIPES["иерархия вызовов"]
        for form, index in (("compact", 4), ("full", 10)):
            step = compact[form][index]
            assert "контекстом строк" not in step and "context" in step, step
        reachable = K._BUSINESS_RECIPES["достижимость"]["full"][7]
        assert "контекстом строк" not in reachable and "context" in reachable, reachable
        _no_hits(
            {
                "sig find_callers_context": snap["find_callers_context"]["sig"],
                "sig find_call_hierarchy": snap["find_call_hierarchy"]["sig"],
                "recipe find_callers_context": snap["find_callers_context"]["recipe"],
                "recipe find_call_hierarchy": snap["find_call_hierarchy"]["recipe"],
            },
            {
                "файловый маршрут отождествлен с exact_available=False": re.compile(
                    r"FS-маршрут\w*[\s#]*\([^)]*exact_available\s*=\s*False"
                ),
                "default limit=20 у find_callers": re.compile(r"default limit=20"),
            },
        )

    def test_conditional_and_missing_keys_are_named(self):
        snap = build_helper_metadata_snapshot()
        assert "orphan_methods?" in snap["get_module_outline"]["sig"]
        assert "exports_count" in snap["code_metrics"]["sig"]
        overrides = snap["get_overrides"]["sig"]
        assert "row['extension_file']" in overrides
        recipe = snap["get_overrides"]["recipe"]
        assert "source_path" in recipe and "EXTENSION" in recipe  # путь оригинала: пуст в EXTENSION-индексе
        for pair in _MOVEMENT_PAIRS:
            entry = next(e for e in DISAMBIGUATION_PAIRS if tuple(e["pair"]) == pair)
            copies = {
                "STRATEGY_SECTIONS": "\n".join(str(v) for k, v in entry.items() if k != "pair"),
                "_STRATEGY_HEADER": _header_block(*pair),
            }
            for where, text in copies.items():
                assert ".get('is_postable')" in text and "непроводим" in text, (where, pair)
        step = K._BUSINESS_RECIPES["проведение"]["compact"][2]
        assert "find_register_movements('ДокИмя').get('is_postable') is False" in step, step
        assert ").is_postable" not in "\n".join(
            K._BUSINESS_RECIPES["проведение"]["compact"] + K._BUSINESS_RECIPES["проведение"]["full"]
        )

    def test_refusals_and_versions_are_stated(self):
        snap = build_helper_metadata_snapshot()
        for name in ("find_print_forms", "find_based_on_documents", "find_register_movements", "find_defined_types"):
            recipe = snap[name]["recipe"]
            assert "ValueError" in recipe and "EXTENSION" in recipe, name
        assert "приоритет" in snap["find_based_on_documents"]["recipe"]
        print_recipe = snap["find_print_forms"]["recipe"]
        assert "подстрочный поиск" not in print_recipe
        assert "объект другой категории" not in print_recipe and "ManagerModule любой категории" in print_recipe
        assert "layer='main'" in snap["find_unresolved_calls"]["recipe"]
        search = snap["search"]["recipe"]
        first = re.findall(r"\w+", search.split("\n")[0])
        assert {"attributes", "predefined"} <= set(first), first  # заголовок перечисляет обе области
        assert "scope='attributes'" in search and "scope='predefined'" in search
        assert "rlm-bsl-index index build" in snap["get_index_info"]["recipe"]
        references = snap["find_references_to_object"]["recipe"]
        assert "On v11" not in references and "до v12" in references
        assert "В v15" not in snap["search_objects"]["recipe"]

    def test_index_advice_follows_real_signals(self):
        full = "\n".join(K._BUSINESS_RECIPES["ссылки"]["full"])
        assert "rlm_index" not in full
        for signal in (
            "rlm_start.index.index_status",
            "_meta.reasons",
            "kinds_unavailable",
            "index build",
            "index update",
        ):
            assert signal in full, signal
        # missing требует index build, а не index update (CLI при missing завершается кодом 1)
        step = next(s for s in K._BUSINESS_RECIPES["ссылки"]["full"] if "index_status" in s)
        assert "stale_content|incomplete|missing" not in step, step
        assert (
            step.index("missing") < step.index("index build") < step.index("stale_content") < step.index("index update")
        ), step
        prompt_doc = (DOCS / "full_analysis_prompt.md").read_text(encoding="utf-8")
        assert "(`stale_content`, `missing`)" not in prompt_doc


class TestStrategyCopiesSync:
    """Секции стратегии и пары DISAMBIGUATION в двух копиях говорят одно и то же."""

    def test_sections_are_verbatim_in_the_full_header(self):
        for key in ("critical", "workflow", "performance", "batching"):
            assert STRATEGY_SECTIONS[key] in K._STRATEGY_HEADER, f"секция {key!r} разошлась с _STRATEGY_HEADER"
        assert STRATEGY_SECTIONS["io"] == K._STRATEGY_IO_SECTION

    @pytest.mark.parametrize("pair", sorted(_PAIR_FACTS))
    def test_disambiguation_facts_agree_in_both_copies(self, pair):
        entry = next(e for e in DISAMBIGUATION_PAIRS if tuple(e["pair"]) == pair)
        structured = "\n".join(str(v) for k, v in entry.items() if k != "pair")
        for fact in _PAIR_FACTS[pair]:
            assert fact in structured, (pair, fact, "STRATEGY_SECTIONS")
            assert fact in _header_block(*pair), (pair, fact, "_STRATEGY_HEADER")


# ═══════════════════════════ Runtime-подсказки и предупреждения ═══════════════════════════

_BUDGET_MODULE = "Процедура П() Экспорт\nКонецПроцедуры\n"
# То, чего в подсказке агенту быть не должно: индекс меняет только пользователь.
_AGENT_INDEX_ORDERS = (
    re.compile(r"rlm_index\(action=\\?'build"),
    re.compile(r"rlm_index build\b"),
    re.compile(r"\(rlm_index update\)"),
    re.compile(r"\(rlm_index\)"),
    re.compile(r"\b(?:собери|пересобери) (?:его|индекс|с нужной опцией)", re.I),
)


def _assert_addresses_user(text: str, where: str) -> None:
    forbidden = [pattern.pattern for pattern in _AGENT_INDEX_ORDERS if pattern.search(text)]
    assert not forbidden, f"{where}: агенту велено менять индекс — {forbidden}:\n{text}"
    assert "rlm-bsl-index index build" in text, f"{where}: нет CLI для пользователя:\n{text}"


class TestRuntimeHints:
    """Подсказки на неиндексных и ошибочных путях ведут к пользователю, а не к `rlm_index`."""

    @pytest.mark.parametrize("fmt", ["cf", "edt"])
    def test_xml_hint_is_built_from_the_object_directory(self, worlds, monkeypatch, fmt):
        with worlds[fmt].session(monkeypatch) as s:
            tail = _tail(_xml_error(s, "Documents/Док/Ext/Document.xml"))
            assert "'Documents/Док' (директория)" in tail and "'Documents/Док.xml'" in tail, tail
            # без абсурдного хвоста и без повтора пути, который уже не нашелся
            assert "Ext/Document/Ext/Document.xml" not in tail and "'Documents/Док/Ext/Document.xml'" not in tail, tail
            # явный отсутствующий EDT-путь <Cat>/<Obj>/<Obj>.mdo не дублирует имя объекта и не повторяет себя
            for bad, base_dir in (
                ("Documents/Another/Another.mdo", "Documents/Another"),
                ("ExtA/Documents/Another/Another.mdo", "ExtA/Documents/Another"),
            ):
                tail = _tail(_xml_error(s, bad))
                assert (
                    f"'{base_dir}' (директория)" in tail
                    and f"'{base_dir}.xml'" in tail
                    and f"'{base_dir}/Ext/Document.xml'" in tail
                ), tail
                assert "Another/Another" not in tail, tail

    def test_xml_hint_does_not_repeat_the_failed_path_and_names_the_object_type(self, worlds, monkeypatch):
        with worlds["cf"].session(monkeypatch) as s:
            tail = _tail(_xml_error(s, "Documents/Unknown.xml"))
            assert "'Documents/Unknown.xml'" not in tail, tail
            assert "'Documents/Unknown/Ext/Document.xml' (CF)" in tail, tail
            # обертка и путь соседнего расширения: категория — из каталога объекта, а не из первого сегмента
            for path in ("ExtA/Documents/Unknown.xml", "../cfe/Расш1/Documents/Unknown.xml"):
                tail = _tail(_xml_error(s, path))
                assert f"'{path[:-4]}/Ext/Document.xml' (CF)" in tail and "<Type>" not in tail, tail

    @pytest.mark.parametrize("fmt", ["cf", "edt"])
    def test_xml_hint_leads_with_object_xml_for_an_mdo_request(self, worlds, monkeypatch, fmt):
        with worlds[fmt].session(monkeypatch) as s:
            tail = _tail(_xml_error(s, "Documents/НетТакого.mdo"))
            xml_at, ext_at = tail.find("'Documents/НетТакого.xml'"), tail.find("'Documents/НетТакого/Ext/Document.xml'")
            assert 0 <= xml_at < ext_at, tail  # типичный путь CF — первым, Ext/<Type>.xml — запасная раскладка
            assert (tail.index("(CF)") < tail.index("(EDT)")) is (fmt == "cf"), tail
            # регрессия: существующий описатель CF по «фейковому» .mdo по-прежнему разрешается
            assert s.text("print(parse_object_xml('Documents/Док.mdo')['name'])") == "Док"

    def test_sandbox_hint_names_object_xml_before_the_ext_layout(self, worlds, monkeypatch):
        with worlds["cf"].session(monkeypatch) as s:
            out = s.run("parse_object_xml('Documents/НетТакого.mdo')\n")
            assert out["error"] and "FileNotFoundError" in out["error"]
            hint = out["error"].split("HINT:", 1)[1]
            assert 0 <= hint.find("Documents/Name.xml") < hint.find("Documents/Name/Ext/Document.xml"), hint

    @pytest.mark.parametrize("broken", ["missing", "corrupt"])
    def test_start_warning_gives_the_user_a_cli_command_with_an_absolute_index_root(
        self, tmp_path, monkeypatch, broken
    ):
        src = tmp_path / "My Config"
        _write(src / "Configuration.xml", CF_DESCRIPTOR)
        _write(src / "Documents" / "Док" / "Ext" / "ObjectModule.bsl", _BUDGET_MODULE)
        monkeypatch.chdir(tmp_path)  # рабочий каталог «терминала» совпадает с каталогом сервера
        monkeypatch.setenv("RLM_INDEX_DIR", ".rel_idx")
        if broken == "corrupt":
            db = Path(get_index_db_path(str(src)))
            db.parent.mkdir(parents=True)
            db.write_bytes(b"this is not a sqlite database " * 64)

        def start() -> _Session:
            data = json.loads(_rlm_start(path=str(src), query="", effort="high", domains=["весь каталог"]))
            assert "session_id" in data, data
            return _Session(data)

        with start() as s:
            assert s.data["index"]["loaded"] is False
            warning = next(w for w in s.data["index"]["warnings"] if "Индекс" in w)
            assert "rlm_index(action=" not in warning, warning
            command = re.search(r'rlm-bsl-index index build "([^"]+)"', warning)
            root = re.search(r'RLM_INDEX_DIR="([^"]+)"', warning)
            assert command and root, warning
            assert command.group(1) == s.data["resolved_path"]  # путь с пробелом — в кавычках и целиком
            assert (
                Path(root.group(1)).is_absolute() and Path(root.group(1)) == Path(describe_index_root()[0]).absolute()
            )
            assert ("есть, но прочитать его не удалось" in warning) is (broken == "corrupt")
            # Команда из предупреждения, запущенная из ДРУГОГО рабочего каталога, собирает индекс
            # туда, где его ждет сервер.
            elsewhere = tmp_path / "elsewhere"
            elsewhere.mkdir()
            env = dict(
                os.environ,
                RLM_INDEX_DIR=root.group(1),
                RLM_CONFIG_FILE=str(tmp_path / "no_service.json"),
                HOME=str(tmp_path),
                USERPROFILE=str(tmp_path),
                PYTHONIOENCODING="utf-8",
            )
            proc = subprocess.run(
                [sys.executable, "-m", "rlm_tools_bsl.cli", "index", "build", command.group(1)],
                cwd=elsewhere,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=180,
            )
            assert proc.returncode == 0, proc.stderr[-600:]
        with start() as s:
            assert s.data["index"]["loaded"] is True

    def test_hints_without_index_address_the_user(self, worlds, monkeypatch):
        with worlds["cf"].session(monkeypatch, indexed=False) as s:
            hints = s.json(
                "tpl = find_templates(name='Накладная')['hint']\n"
                "d = find_definition('Экспорт1')\n"
                "based = find_based_on_documents('Справочник.Спр')['hint']\n"
                "usage = find_code_usages('Документ.Док')['_meta']['hint']\n"
                "print(json.dumps({'tpl': tpl, 'definition': d['_meta'].get('hint', ''), "
                "'definition_index_used': d['_meta']['index_used'], 'based': based, 'usage': usage}, ensure_ascii=False))\n"
            )
        assert hints["definition_index_used"] is False
        for where in ("tpl", "definition", "based", "usage"):
            _assert_addresses_user(hints[where], where)
        assert "сузь module_hint" in hints["definition"], hints["definition"]

    def test_definition_hint_follows_the_index_state(self, worlds, monkeypatch):
        with worlds["flood"].session(monkeypatch) as s:
            meta = s.json(
                "d = find_definition('ЦелевоеИмя')\n"
                "print(json.dumps([d['_meta']['index_used'], d['_meta']['source'], d['_meta'].get('reasons'), "
                "d['_meta'].get('hint', '')], ensure_ascii=False))\n"
            )
        assert meta[:3] == [True, "index+live", ["module_budget"]], (
            meta
        )  # модули соседнего расширения: пересборка не поможет
        assert (
            meta[3]
            == "ответ неполон: сузь module_hint (точное имя объекта либо rel_path) — подробности в _meta.reasons."
        )

    def test_coverage_and_index_info_do_not_order_index_build(self, worlds, monkeypatch):
        coverage = json.dumps(_help(section="coverage"), ensure_ascii=False)
        _assert_addresses_user(coverage, "rlm_help(section='coverage')")
        for marker in ("index_status", "stale_content", "stale_age", "index update"):
            assert marker in coverage, marker
        with worlds["cf"].session(monkeypatch, indexed=False) as s:
            recipe = _help_recipe(s, "get_index_info")
            assert recipe == _help(helpers=["get_index_info"], include_code=True)[0]["recipe"].strip()
            output = s.text(_recipe_code(recipe))
            assert s.json(
                "print(json.dumps([info['status'], find_templates(name='Накладная')['source']], ensure_ascii=False))"
            ) == ["no_index", "unavailable"]
        _assert_addresses_user(output, "вывод примера help('get_index_info')")
        assert "Почти все хелперы читают исходники напрямую." in output
        assert "Для find_templates нужен индекс." in output
        assert "rlm_index(" not in recipe and "не вызывай" in recipe


# ═══════════════════════════ Исполняемые рецепты ═══════════════════════════

_ROOT_HINT = _ROOT_MODULE.format(name="ModRoot")
_WIDE_HINT = _ROOT_MODULE.format(name=_WIDE_ROOT)
_TRUNCATED = "... [output truncated]"
_EXT_METHOD = "Расш1_СуммаПриИзмененииПеред"  # перехват события поля без базового обработчика: target_method == ''


def _module(fmt: str, category: str, name: str, file: str) -> str:
    return f"{category}/{name}/{'Ext/' if fmt == 'cf' else ''}{file}"


def _ext_root(world: _World) -> Path:
    return world.base / "cfe" / "Расш1"


class TestHierarchyRecipeOnFixtures:
    """`help('find_call_hierarchy')`: сводка первой, превью ограничено, добор усеченных узлов без потерь."""

    def test_every_truncated_node_is_paged_once(self, worlds, monkeypatch):
        with worlds["run3"].session(monkeypatch) as s:
            s.ok(f"res = find_call_hierarchy('Root', module_hint={_ROOT_HINT!r}, depth=2)\n")
            out = s.ok(_hierarchy_paging(s))
            assert out["stdout"].startswith("visited=4, nodes=4, truncated=2\nPAGED nodes: 2 (+203 callers"), out[
                "stdout"
            ]
            assert "без спуска глубже" in out["stdout"] and _TRUNCATED not in out["stdout"]
            paged = dict(
                s.json(
                    "print(json.dumps([[k[1].split('/')[1], [c['caller_name'] for c in v]] "
                    "for k, v in paged.items()], ensure_ascii=False))\n"
                )
            )
            assert paged["ModA"] == []  # узел не усечен: добор пуст, страницы не дублируются
            assert paged["ModB"] == [f"CallerB{i:03d}" for i in range(201, 402)]  # 201-я … 401-я: две страницы
            assert paged["ModC"] == ["CallerC201", "CallerC202"]
            assert out["stdout"].count("PAGED Run [") == 2

    def test_wide_graph_keeps_stdout_compact_and_data_complete(self, worlds, monkeypatch):
        with worlds["wide"].session(monkeypatch) as s:
            s.ok(f"res = find_call_hierarchy({_WIDE_ROOT!r}, module_hint={_WIDE_HINT!r}, depth=2)\n")
            out = s.ok(_hierarchy_paging(s))
            assert out["stdout"].startswith("visited=201, nodes=201, truncated=1\nPAGED nodes: 1 (+1 callers"), out[
                "stdout"
            ]
            assert _TRUNCATED not in out["stdout"] and len(out["stdout"]) < 15_000, len(out["stdout"])
            assert out["stdout"].index("PAGED nodes:") < out["stdout"].index(f"{_WIDE_ROOT} [")  # сводка — первой
            assert out["stdout"].rstrip("\n").endswith("... (все данные в res и paged)"), out["stdout"][-120:]
            # превью срезано по символам, а данные целиком остались в переменных
            shape = s.json(
                "print(json.dumps([len(res['tree']), len(preview), len(lines), [len(v) for v in paged.values()]], "
                "ensure_ascii=False))\n"
            )
            assert shape[0] == 201 and shape[1] > 12_000 and shape[2] == 61 and shape[3] == [1]  # 60 + PAGED

    def test_node_with_an_empty_first_page_is_not_skipped(self, worlds, monkeypatch):
        """Индекс `--no-calls`: первые 200 файлов-кандидатов содержат имя только в комментарии."""
        with worlds["nocalls"].session(monkeypatch) as s:
            shape = s.json(
                f"res = find_call_hierarchy('Root', module_hint={_ROOT_HINT!r}, depth=2)\n"
                "print(json.dumps([res['_meta']['exact_available'], len(res['tree']), len(res['tree'][0]['callers']), "
                "res['truncated_targets']], ensure_ascii=False))\n"
            )
            assert shape[:3] == [False, 1, 0] and shape[3][0]["name"] == "Root", shape  # усеченный узел с callers=[]
            out = s.ok(_hierarchy_paging(s))
            assert "PAGED nodes: 1 (+1 callers" in out["stdout"], out["stdout"]
            found = s.json(
                "print(json.dumps([[(c['caller_name'], c['file']) for c in v] for v in paged.values()], "
                "ensure_ascii=False))\n"
            )
            assert found == [[["РеальныйВызов", _ROOT_MODULE.format(name="Cand201")]]]  # реальный вызов — на offset=200

    @pytest.mark.parametrize("world", ["cf", "edt", "nocalls"])
    def test_hierarchy_hint_takes_a_root_that_is_really_called(self, worlds, monkeypatch, world):
        with worlds[world].session(monkeypatch) as s:
            out = s.ok(_code_hint("иерархия вызовов"))
            first = out["stdout"].splitlines()[0]
            assert first.startswith("ЗаполнитьТестовыйДокумент [") and first.endswith("(1 callers)"), out["stdout"]
            # exact на корне включается там, где есть граф; без графа (`--no-calls`) обещания exact нет
            assert ("цель: True" in first) is (world != "nocalls"), out["stdout"]
            # фактические ключи двух API графа различны (как обещает DISAMBIGUATION)
            keys = s.json(
                "h = find_call_hierarchy('ЗаполнитьТестовыйДокумент', module_hint=om, depth=1)\n"
                "c = find_callers_context('ЗаполнитьТестовыйДокумент', om, 0, 1)\n"
                "print(json.dumps([sorted(h['tree'][0]['callers'][0]), sorted(c['callers'][0])], ensure_ascii=False))\n"
            )
            assert {"module_path", "is_export", "level"} <= set(keys[0]) and "caller_is_export" not in keys[0], keys[0]
            assert {"file", "caller_is_export"} <= set(keys[1]), keys[1]

    def test_hierarchy_hint_prefers_exact_rows_and_survives_edge_cases(self, worlds, tmp_path, monkeypatch):
        # 1) Alpha: target_exact=True, exact_rows=0, fallback_rows=1 (чужой одноименный метод);
        #    Zeta: target_exact=True, exact_rows=1 → рецепт обязан выбрать Zeta, а не Alpha.
        files = {
            "Configuration.xml": CF_DESCRIPTOR,
            "Documents/Док.xml": _cf_obj("Document", "Док"),
            "Documents/Док/Ext/ObjectModule.bsl": _bsl(
                "Процедура Alpha() Экспорт",
                "КонецПроцедуры",
                "Процедура Zeta() Экспорт",
                "КонецПроцедуры",
                "Процедура ВызовТочного() Экспорт",
                "\tZeta();",
                "КонецПроцедуры",
            ),
            "Documents/ДругойДок.xml": _cf_obj("Document", "ДругойДок"),
            "Documents/ДругойДок/Ext/ObjectModule.bsl": _bsl(
                "Процедура Alpha() Экспорт",
                "КонецПроцедуры",
                "Процедура ВызовЧужойAlpha(НеизвестныйОбъект) Экспорт",
                "\tНеизвестныйОбъект.Alpha();",
                "КонецПроцедуры",
            ),
        }
        w = _World(tmp_path, tmp_path / "cf", files)
        with w.session(monkeypatch) as s:
            hint = _code_hint("иерархия вызовов")
            out = s.ok(hint)
            first = out["stdout"].splitlines()[0]
            assert first.startswith("Zeta [ребра: точно; цель: True]"), out["stdout"]
        # 2) Документ без модулей при наличии однокоренного NoCodeArchive и одноименного Catalogs/NoCode:
        #    подстрочный поиск не должен подбирать чужой объект или другую категорию.
        homonym_files = {
            "Configuration.xml": CF_DESCRIPTOR,
            "Documents/NoCode.xml": _cf_obj("Document", "NoCode"),
            "Documents/NoCodeArchive.xml": _cf_obj("Document", "NoCodeArchive"),
            "Documents/NoCodeArchive/Ext/ManagerModule.bsl": _bsl(
                "Процедура АрхивМетод() Экспорт",
                "КонецПроцедуры",
                "Процедура ВызовАрхива() Экспорт",
                "\tАрхивМетод();",
                "КонецПроцедуры",
            ),
            "Catalogs/NoCode.xml": _cf_obj("Catalog", "NoCode"),
            "Catalogs/NoCode/Ext/ObjectModule.bsl": _bsl(
                "Процедура СпрМетод() Экспорт",
                "КонецПроцедуры",
                "Процедура ВызовСпр() Экспорт",
                "\tСпрМетод();",
                "КонецПроцедуры",
            ),
        }
        wh = _World(tmp_path / "homonym", tmp_path / "homonym" / "cf", homonym_files)
        with wh.session(monkeypatch) as s:
            no_om = _help(topic="иерархия вызовов", format="full")["code_hint"].replace(
                "РеализацияТоваровУслуг", "NoCode"
            )
            assert s.ok(no_om)["stdout"].strip() == "Нет вызываемых экспортов"
        # 3) --no-calls: 1-й файл-кандидат без вызова, 2-й — InvokeTarget (находится);
        #    при >50 файлах-кандидатах с комментариями (has_more=True) — явное предупреждение, а не ложное «нет».
        nocalls_two = {
            "Configuration.xml": CF_DESCRIPTOR,
            "Documents/Док.xml": _cf_obj("Document", "Док"),
            "Documents/Док/Ext/ObjectModule.bsl": _bsl("Процедура Target() Экспорт", "КонецПроцедуры"),
            "CommonModules/AКомментарий.xml": _cf_obj("CommonModule", "AКомментарий", props=["<Server>true</Server>"]),
            "CommonModules/AКомментарий/Ext/Module.bsl": _bsl("// Target() только в комментарии"),
            "CommonModules/ZВызывающий.xml": _cf_obj("CommonModule", "ZВызывающий", props=["<Server>true</Server>"]),
            "CommonModules/ZВызывающий/Ext/Module.bsl": _bsl(
                "Процедура InvokeTarget() Экспорт", "\tTarget();", "КонецПроцедуры"
            ),
        }
        wn = _World(tmp_path / "nc2", tmp_path / "nc2" / "cf", nocalls_two, build_calls=False)
        with wn.session(monkeypatch) as s:
            out = s.ok(_code_hint("иерархия вызовов"))
            assert "InvokeTarget" in out["stdout"] and out["stdout"].startswith("Target ["), out["stdout"]
        with worlds["nocalls"].session(monkeypatch) as s:
            incomplete = (
                _help(topic="иерархия вызовов", format="full")["code_hint"]
                .replace("РеализацияТоваровУслуг", "ModRoot")
                .replace("'Documents'", "'CommonModules'")
            )
            out = s.ok(incomplete)["stdout"].strip()
            assert "Проверка неполная (has_more)" in out and "Нет вызываемых экспортов" not in out, out
        # 4) Широкое дерево (201 узел) — stdout ограничен и не режется песочницей, все узлы в res;
        #    файловый хелпер tree() не затенен и остается вызываемым.
        with worlds["wide"].session(monkeypatch) as s:
            wide_code = (
                _help(topic="иерархия вызовов", format="full")["code_hint"]
                .replace("РеализацияТоваровУслуг", _WIDE_ROOT)
                .replace("'Documents'", "'CommonModules'")
            )
            out = s.ok(wide_code)
            assert _TRUNCATED not in out["stdout"] and len(out["stdout"]) < 15_000, len(out["stdout"])
            assert s.json("print(json.dumps([len(res['tree']), callable(tree)], ensure_ascii=False))\n") == [201, True]
            assert "CommonModules" in s.ok("print(tree('.'))\n")["stdout"]

    def test_call_hierarchy_recipe_examples_use_exact_object_module(self, tmp_path, monkeypatch):
        snap = build_helper_metadata_snapshot()
        recipe = snap["find_call_hierarchy"]["recipe"]
        step0 = _help(topic="иерархия вызовов", format="full")["steps"][0]
        helpers_doc = (DOCS / "HELPERS.md").read_text(encoding="utf-8")
        hier_line = next(ln for ln in helpers_doc.splitlines() if ln.startswith("- `find_call_hierarchy("))
        for where, text in (("recipe", recipe), ("topic full[0]", step0), ("HELPERS.md", hier_line)):
            assert "ЗаполнитьДокумент" not in text and "ОтразитьВУчете" not in text, (where, text)
            assert "ВременныеТаблицыДанныхДокумента" in text, (where, text)

        files = _main_files("cf")
        path = "Documents/Док/Ext/ObjectModule.bsl"
        files[path] += _bsl("Процедура ИзНужногоМодуля()", "    ЗаполнитьТестовыйДокумент();", "КонецПроцедуры")
        files["Documents/ДокАрхив.xml"] = _cf_obj("Document", "ДокАрхив")
        files["Documents/ДокАрхив/Ext/ObjectModule.bsl"] = _OBJ_TAIL + _bsl(
            "Процедура ИзЧужогоМодуля()", "    ЗаполнитьТестовыйДокумент();", "КонецПроцедуры"
        )
        world = _World(tmp_path, tmp_path / "cf", files)
        with world.session(monkeypatch) as s:
            code = _recipe_code(_help_recipe(s, "find_call_hierarchy"))
            assert "Не найден модуль объекта" in s.text(code)
            adapted = _subst(code).replace("ВременныеТаблицыДанныхДокумента", "ЗаполнитьТестовыйДокумент")
            preview = s.text(adapted)
            assert f"ЗаполнитьТестовыйДокумент [{path}]" in preview, preview
            assert "ИзНужногоМодуля" in preview and "ИзЧужогоМодуля" not in preview, preview
            assert s.json("print(json.dumps(res['_meta']['root_exact'], ensure_ascii=False))\n") is True

            absent_method = adapted.replace("ЗаполнитьТестовыйДокумент", "ОтсутствующийЭкспорт")
            assert "нет экспортного метода" in s.text(absent_method)

            topic_code = _looks_like_code(step0).replace("ВременныеТаблицыДанныхДокумента", "ЗаполнитьТестовыйДокумент")
            assert "module_hint=om" in topic_code and "[i]" not in topic_code, step0
            assert s.value(f"om = {path!r}\n{topic_code}", "res['_meta']['root_exact']") is True


class TestTopicRecipes:
    """Темы `rlm_help`: шаги-компакт и code_hint исполняются буквальной вставкой."""

    @pytest.mark.parametrize("fmt", ["cf", "edt"])
    def test_code_hints_run_verbatim(self, worlds, monkeypatch, fmt):
        with worlds[fmt].session(monkeypatch) as s:
            assert s.data["config_format"] == fmt
            # интеграция: планы обмена по описателям корня — все раскладки дерева, без усечения по limit и без дублей
            plans = _plans_of(fmt)
            out = s.ok(_code_hint("интеграция"))
            assert "Обертка" not in out["stdout"], out["stdout"]
            assert f"Svc/Plans: 0 0 0 {len(plans)}" in out["stdout"], out["stdout"]
            assert "Plain 1" in out["stdout"] and "Jobs: 1/6" in out["stdout"], out["stdout"]
            assert s.text("print(plan_names)") == repr(plans)  # SHIP побеждает Ship
            # ссылки: имя определяемого типа существует — без ValueError
            assert "code total=1" in s.ok(_code_hint("ссылки"))["stdout"]
            # перечисления: полная развилка на ошибочном имени
            hint = _code_hint("перечисления")
            assert "Первый:" in s.ok(hint)["stdout"]
            assert "не найдено" in s.ok(hint.replace(_ENUM, "Перечисление.Нет"))["stdout"].lower()
            # структура объекта и тип реквизита
            assert "attrs=3" in s.ok(_code_hint("структура объекта"))["stdout"]
            assert "РеализуемыеАктивы" in s.ok(_code_hint("тип реквизита"))["stdout"]
            # расширения: строка перехвата с пустой целью называет метод расширения
            lines = s.ok(_code_hint("расширения"))["stdout"].splitlines()
            assert (f"Перед {_EXT_METHOD} ← Расш1" in lines) is (fmt == "cf"), lines
            assert not [ln for ln in lines if re.fullmatch(r"\s+\S+\s+←\s+\S+", ln)], lines  # пустого маркера нет

    @pytest.mark.parametrize("fmt", ["cf", "edt"])
    def test_rights_topic_routes_to_the_roles_recipe(self, worlds, monkeypatch, fmt):
        rights = _help(topic="права", format="full")["steps"]
        assert "help('find_roles')" in rights[4] and "find_by_type('Roles')" not in "\n".join(rights)
        integration = _help(topic="интеграция", format="full")["steps"]
        assert "из шага 5" in integration[5] and "из шага 4" not in "\n".join(integration), integration
        with worlds[fmt].session(monkeypatch) as s:
            recipe = _help_recipe(s, "find_roles")
            assert recipe == build_helper_metadata_snapshot()["find_roles"]["recipe"]  # help() отдает рецепт реестра
            assert "find_by_type('Roles')" not in recipe
            out = s.ok(_roles_fragment(s))
            assert out["stdout"].strip() == "2 ['Admin', 'RealRole']", out["stdout"]  # RealRole побеждает realrole
            assert s.json("print(json.dumps([len(roles), roles[:20]], ensure_ascii=False))\n") == [
                2,
                ["Admin", "RealRole"],
            ]
            assert "Обертка" not in out["stdout"]

    def test_form_events_step_is_python_and_prints(self, worlds, monkeypatch):
        step = _help(topic="события формы")["steps"][2]
        ast.parse(step)  # на HEAD — SyntaxError в одной строке
        with worlds["cf"].session(monkeypatch) as s:
            assert "ФормаДокумента 3" in s.ok(f"forms = parse_form('Док')\n{step}\n")["stdout"]

    @pytest.mark.parametrize("fmt", ["cf", "edt"])
    def test_posting_check_reads_the_conditional_key(self, worlds, monkeypatch, fmt):
        step = _help(topic="проведение")["steps"][2]
        check = re.search(r"find_register_movements\('[^']+'\)\.get\('is_postable'\) is False", step)
        assert check, step
        with worlds[fmt].session(monkeypatch) as s:
            got = [s.text(f"print({check.group(0).replace('ДокИмя', doc)})") for doc in ("Док", "ДокНепроводимый")]
            assert got == ["False", "True"], got  # у проводимого ключа is_postable нет вовсе


def _wrapper(tmp_path: Path, child: str) -> Path:
    wrap = tmp_path / "wrapper"
    _write_tree(
        wrap,
        {
            f"{child}/Configuration.xml": CF_DESCRIPTOR,
            **{f"{child}/{rel}": text for rel, text in _layout_files("cf").items()},
        },
    )
    return wrap


def _with_foreign_dirs(wrap: Path) -> None:
    """Посторонние каталоги рядом с оберткой: еще один `Configuration.xml` (не описатель) и «примеры»."""
    _write(wrap / "ADocs" / "Configuration.xml", "<doc/>\n")
    _write(wrap / "ADocs" / "Roles" / "ForeignRole.xml", _cf_obj("Role", "ForeignRole"))
    _write(wrap / "samples" / "Roles" / "FakeRole.xml", _cf_obj("Role", "FakeRole"))
    _write(wrap / "samples" / "ExchangePlans" / "FakePlan.xml", _cf_obj("ExchangePlan", "FakePlan"))


_PLANS = ["ExtPlan", "MdoPlan", "Plain", "SHIP"]  # CF-дерево: три раскладки описателей
_PLANS_EDT = ["MdoPlan", "Plain", "SHIP"]  # EDT-дерево: без раскладки `Ext/<Тип>.xml`


def _plans_of(fmt: str) -> list[str]:
    return _PLANS if fmt == "cf" else _PLANS_EDT


class TestRolesAndPlansOnWrappers:
    """Списки ролей и планов обмена на обертках: явный префикс выбирает один корень, буквальный `pfx=''`
    не дает тихого нуля."""

    @pytest.mark.parametrize("indexed", [True, False])
    def test_explicit_prefix_selects_one_wrapped_root(self, tmp_path, indexed):
        wrap = _wrapper(tmp_path, "ExtA")
        _with_foreign_dirs(wrap)
        _write_tree(wrap / "Configuration", {"Configuration.xml": CF_DESCRIPTOR, **_layout_files("cf")})
        with _Direct(wrap, indexed=indexed) as box:  # база — контейнер, пути — с префиксом
            assert box.value(_roles_fragment_any("ExtA/"), "roles") == ["Admin", "RealRole"]
            assert box.value(_plans_fragment("ExtA/"), "plan_names") == _PLANS
            assert box.value(_roles_fragment_any("Configuration/"), "roles") == ["Admin", "RealRole"]
            literal = box.ok(_roles_fragment_any())
            assert "задай pfx из ['ADocs/', 'Configuration/', 'ExtA/', 'samples/']" in literal, literal
            plans = box.ok(_plans_fragment())
            assert "Обертка pfx: ['Configuration/', 'ExtA/', 'samples/']" in plans, plans

    def test_brackets_in_the_wrapped_name_do_not_break_the_literal_prefix(self, tmp_path):
        wrap = _wrapper(tmp_path, "Ext[Wrapped]")
        with _Direct(wrap, indexed=False) as box:
            # префикс в маске ломал бы glob без индекса — поэтому маска фиксирована, а префикс сравнивается буквально
            assert box.json("print(json.dumps(glob_files('Ext[Wrapped]/**/Roles/**/*'), ensure_ascii=False))\n") == []
            assert "задай pfx из ['Ext[Wrapped]/']" in box.ok(_roles_fragment_any())
            assert "Обертка pfx: ['Ext[Wrapped]/']" in box.ok(_plans_fragment())
            assert box.value(_roles_fragment_any("Ext[Wrapped]/"), "roles") == ["Admin", "RealRole"]
            assert box.value(_plans_fragment("Ext[Wrapped]/"), "plan_names") == _PLANS

    def test_names_like_test_exchange_plans_are_not_cut_to_test(self, tmp_path):
        wrap = tmp_path / "wrapper"
        _write(wrap / "TestRoles" / "Configuration.xml", CF_DESCRIPTOR)
        _write(wrap / "TestRoles" / "Roles" / "Admin.xml", _cf_obj("Role", "Admin"))
        _write(wrap / "TestExchangePlans" / "Configuration.xml", CF_DESCRIPTOR)
        _write(wrap / "TestExchangePlans" / "ExchangePlans" / "Share.xml", _cf_obj("ExchangePlan", "Share"))
        with _Direct(wrap, indexed=False) as box:
            assert "задай pfx из ['TestRoles/']" in box.ok(_roles_fragment_any())  # полный префикс, не «Test»
            plans = box.ok(_plans_fragment())
            assert "Обертка pfx: ['TestExchangePlans/']" in plans, plans
            assert box.value(_roles_fragment_any("TestRoles/"), "roles") == ["Admin"]
            assert box.value(_plans_fragment("TestExchangePlans/"), "plan_names") == ["Share"]


class TestWrapperSessions:
    """То же через настоящий `rlm_start`: обертку глубины 1 сервер резолвит до корня конфигурации."""

    def test_depth_one_wrapper_is_resolved_to_the_config_root(self, tmp_path, monkeypatch):
        wrap = _wrapper(tmp_path, "ExtA")
        _with_foreign_dirs(wrap)
        world = _World(tmp_path, wrap / "ExtA")
        with world.session(monkeypatch, path=wrap) as s:
            assert Path(s.data["resolved_path"]).samefile(wrap / "ExtA")
            out = s.ok(_roles_fragment(s))  # пути — от корня конфигурации: буквальный pfx='' уже верен
            assert out["stdout"].strip() == "2 ['Admin', 'RealRole']", out["stdout"]
            plans = s.ok(_plans_fragment())
            assert "Обертка" not in plans["stdout"], plans["stdout"]
            assert s.text("print(plan_names)") == repr(_PLANS)
            foreign = s.run("print([p for p in glob_files('**/*.xml') if 'ForeignRole' in p or 'FakeRole' in p])\n")
            assert foreign["stdout"].strip() == "[]"  # посторонние каталоги вне корня сессии не видны

    def test_depth_two_container_stays_the_base_and_the_warning_names_the_full_prefix(self, tmp_path, monkeypatch):
        wrap = tmp_path / "wrapper"
        _write(wrap / "src" / "TestRoles" / "Configuration.xml", CF_DESCRIPTOR)
        _write(wrap / "src" / "TestRoles" / "Roles" / "Admin.xml", _cf_obj("Role", "Admin"))
        _write(
            wrap / "src" / "TestRoles" / "ScheduledJobs" / "ОбменСоседа.xml",
            _cf_obj(
                "ScheduledJob", "ОбменСоседа", props=["<MethodName>CommonModule.М.П</MethodName>", "<Use>true</Use>"]
            ),
        )
        _write(wrap / "src" / "TestExchangePlans" / "Configuration.xml", CF_DESCRIPTOR)
        _write(wrap / "src" / "TestExchangePlans" / "ExchangePlans" / "Share.xml", _cf_obj("ExchangePlan", "Share"))
        _write(wrap / "src" / "TestExchangePlans" / "ExchangePlans" / "Share" / "Ext" / "Content.xml", _EP_CONTENT_CF)
        _write(
            wrap / "src" / "TestExchangePlans" / "HTTPServices" / "Api.xml",
            _cf_obj("HTTPService", "Api", props=["<RootURL>api</RootURL>"]),
        )
        _write(
            wrap / "src" / "TestExchangePlans" / "ScheduledJobs" / "ОбменПлана.xml",
            _cf_obj(
                "ScheduledJob", "ОбменПлана", props=["<MethodName>CommonModule.М.П</MethodName>", "<Use>true</Use>"]
            ),
        )
        world = _World(tmp_path, wrap)
        with world.session(monkeypatch, indexed=False) as s:
            assert Path(s.data["resolved_path"]) == wrap
            assert "задай pfx из ['src/TestRoles/']" in s.ok(_roles_fragment(s))["stdout"]
            warn = s.ok(_plans_fragment())["stdout"]
            assert "Обертка pfx: ['src/TestExchangePlans/']" in warn and "(по каталогу сессии)" in warn, warn
            roles = s.ok(_roles_fragment(s, "src/TestRoles/"))
            assert roles["stdout"].strip() == "1 ['Admin']", roles["stdout"]
            # При непустом pfx не смешивает сервисы и задания соседней конфигурации из корня-обертки,
            # а печатает число планов под префиксом и предлагает открыть этот корень через rlm_start.
            plans = s.ok(_plans_fragment("src/TestExchangePlans/"))
            assert (
                "Plans: 1 — открой в rlm_start для сервисов/состава/заданий: src/TestExchangePlans/" in plans["stdout"]
            ), plans["stdout"]
            assert "Svc/Plans:" not in plans["stdout"] and "Jobs:" not in plans["stdout"], plans["stdout"]
            assert s.text("print(plan_names)") == "['Share']"
        # При открытии самого вложенного корня тот же фрагмент (pfx='') дает точные сервисы, состав и задания.
        nested = _World(tmp_path / "nested_idx", wrap / "src" / "TestExchangePlans")
        with nested.session(monkeypatch, indexed=False) as s:
            plans = s.ok(_plans_fragment())
            assert (
                "Svc/Plans: 1 0 0 1 (по каталогу сессии)" in plans["stdout"]
                and "Share 1" in plans["stdout"]
                and "Jobs: 1/1 (по каталогу сессии)" in plans["stdout"]
            ), plans["stdout"]

    @pytest.mark.parametrize("indexed", [True, False])
    def test_direct_root_without_plans_keeps_summary_with_samples_candidate(self, tmp_path, monkeypatch, indexed):
        # Прямой CF-корень без своих планов, но с samples/ExchangePlans/FakePlan.xml, HTTP-сервисом и заданием:
        # сводка по каталогу сессии сохраняется, а вложенный каталог показывается кандидатом для проверки.
        files = {
            "Configuration.xml": CF_DESCRIPTOR,
            "CommonModules/Мод/Ext/Module.bsl": _BUDGET_MODULE,
            "HTTPServices/Api.xml": _cf_obj("HTTPService", "Api", props=["<RootURL>api</RootURL>"]),
            "ScheduledJobs/ОбменДанными.xml": _cf_obj(
                "ScheduledJob", "ОбменДанными", props=["<MethodName>CommonModule.Мод.П</MethodName>", "<Use>true</Use>"]
            ),
            "samples/ExchangePlans/FakePlan.xml": _cf_obj("ExchangePlan", "FakePlan"),
        }
        w = _World(tmp_path / f"direct_{indexed}", tmp_path / f"direct_{indexed}" / "cf", files)
        with w.session(monkeypatch, indexed=indexed) as s:
            out = s.ok(_plans_fragment())["stdout"]
            assert (
                "Обертка pfx: ['samples/']" in out
                and "Svc/Plans: 1 0 0 0 (по каталогу сессии)" in out
                and "Jobs: 1/1 (по каталогу сессии)" in out
            ), out
            assert s.text("print(plan_names)") == "[]"

    def test_wrapper_without_plans_marks_session_directory_scope(self, tmp_path, monkeypatch):
        # На обертке без планов при буквальном pfx='' счетчики выводятся с явной пометкой «по каталогу сессии».
        wrap = tmp_path / "wrapper_no_plans"
        for child, job in (("CfgA", "ОбменА"), ("CfgB", "ОбменБ")):
            _write(wrap / "src" / child / "Configuration.xml", CF_DESCRIPTOR)
            _write(wrap / "src" / child / "CommonModules" / "Мод" / "Ext" / "Module.bsl", _BUDGET_MODULE)
            _write(
                wrap / "src" / child / "ScheduledJobs" / f"{job}.xml",
                _cf_obj("ScheduledJob", job, props=["<MethodName>CommonModule.Мод.П</MethodName>", "<Use>true</Use>"]),
            )
        w = _World(tmp_path / "wrap_np_idx", wrap)
        with w.session(monkeypatch, indexed=False) as s:
            out = s.ok(_plans_fragment())["stdout"]
            assert "Svc/Plans: 0 0 0 0 (по каталогу сессии)" in out and "Jobs: 2/2 (по каталогу сессии)" in out, out

    @pytest.mark.parametrize("indexed", [True, False])
    def test_prefixed_cf_and_default_ns_edt_roots_keep_summary(self, tmp_path, monkeypatch, indexed):
        # Валидные описатели с нестандартным префиксом (<md:Configuration> в CF и <Configuration xmlns=".../mdclass">
        # в EDT) не ломают сводку — рецепт не гадает о корне по подстрокам XML.
        cf_prefixed = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<md:MetaDataObject xmlns:md="http://v8.1c.ru/8.3/MDClasses">\n'
            '  <md:Configuration uuid="00000000-0000-0000-0000-000000000001">\n'
            "    <md:Properties><md:Name>Тест</md:Name></md:Properties>\n"
            "  </md:Configuration>\n"
            "</md:MetaDataObject>\n"
        )
        edt_default_ns = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<Configuration xmlns="http://g5.1c.ru/v8/dt/metadata/mdclass" name="Тест"/>\n'
        )
        cf_files = {
            "Configuration.xml": cf_prefixed,
            "CommonModules/Мод/Ext/Module.bsl": _BUDGET_MODULE,
            "ExchangePlans/Share.xml": _cf_obj("ExchangePlan", "Share"),
            "ExchangePlans/Share/Ext/Content.xml": _EP_CONTENT_CF,
            "ScheduledJobs/Обмен.xml": _cf_obj(
                "ScheduledJob", "Обмен", props=["<MethodName>CommonModule.Мод.П</MethodName>", "<Use>true</Use>"]
            ),
        }
        w_cf = _World(tmp_path / f"cf_ns_{indexed}", tmp_path / f"cf_ns_{indexed}" / "cf", cf_files)
        with w_cf.session(monkeypatch, indexed=indexed) as s:
            out = s.ok(_plans_fragment())["stdout"]
            assert (
                "Svc/Plans: 0 0 0 1 (по каталогу сессии)" in out
                and "Share 1" in out
                and "Jobs: 1/1 (по каталогу сессии)" in out
            ), out
        edt_files = {
            "Configuration/Configuration.mdo": edt_default_ns,
            "CommonModules/Мод/Module.bsl": _BUDGET_MODULE,
            "ExchangePlans/Share/Share.mdo": _plan_mdo("Share", _EP_CONTENT_EDT),
            "ScheduledJobs/Обмен/Обмен.mdo": _edt_obj(
                "ScheduledJob", "Обмен", body=["<methodName>CommonModule.Мод.П</methodName>", "<use>true</use>"]
            ),
        }
        w_edt = _World(tmp_path / f"edt_ns_{indexed}", tmp_path / f"edt_ns_{indexed}" / "edt", edt_files)
        with w_edt.session(monkeypatch, indexed=indexed) as s:
            out = s.ok(_plans_fragment())["stdout"]
            assert (
                "Svc/Plans: 0 0 0 1 (по каталогу сессии)" in out
                and "Share 1" in out
                and "Jobs: 1/1 (по каталогу сессии)" in out
            ), out


class TestExtensionBlock:
    """Динамический блок `_extension_strategy` из `rlm_start`: каждый вызов блока исполним."""

    @pytest.mark.parametrize("role", ["main", "extension"])
    @pytest.mark.parametrize("mode", ["slim", "full"])
    def test_every_call_of_the_block_executes(self, worlds, monkeypatch, role, mode):
        monkeypatch.setenv("RLM_STRATEGY_MODE", mode)
        world = worlds["cf"]
        path = _ext_root(world) if role == "extension" else None
        with world.session(monkeypatch, path=path) as s:
            block = s.data["strategy"]
            assert ("EXTENSION DETECTED" in block) is (role == "main")  # одно расширение — единственное число
            assert ("THIS IS AN EXTENSION" in block) is (role == "extension")
            if mode == "full":
                assert "rlm_help(" not in block  # тула rlm_help в full не зарегистрировано
            line = next(ln for ln in block.splitlines() if "override detail on demand" in ln)
            calls = re.findall(r"\w+\([^()]*\)", line)
            assert calls == ["get_overrides('ИмяОбъекта')"], line  # на HEAD здесь же find_ext_overrides() без аргумента
            got = s.json(
                "ov = get_overrides('Док')\nprint(json.dumps([ov['total'], sorted(o['target_method'] for o in ov['overrides'])], ensure_ascii=False))\n"
            )
            assert got == [4, ["", "Команда1", "ПриОткрытии", "ПриСозданииНаСервере"]], got

    @pytest.mark.parametrize("role", ["main", "extension"])
    def test_overrides_recipe_reads_both_ways_without_undefined_names(self, worlds, monkeypatch, role):
        world = worlds["cf"]
        path = _ext_root(world) if role == "extension" else None
        with world.session(monkeypatch, path=path) as s:
            recipe = _subst(_help_recipe(s, "get_overrides"))
            assert "rlm_help(" not in recipe and "row.extension_file" not in recipe
            code = _recipe_code(recipe)
            s.ok(code)  # на HEAD: read_procedure(path, …) с необъявленным path — NameError
            assert s.json(
                "print(json.dumps([type(body).__name__, len(result['overrides'])], ensure_ascii=False))\n"
            ) == [
                "str",
                4,
            ]
            if role == "main":
                assert s.json("print(json.dumps('Перехвачен' in orig, ensure_ascii=False))\n") is True
            else:  # в EXTENSION-индексе source_path пуст: оригинал недоступен, читается только расширение
                assert (
                    s.json(
                        "print(json.dumps(all(o['source_path'] == '' for o in result['overrides']), ensure_ascii=False))\n"
                    )
                    is True
                )


class TestHelperRecipes:
    """Фрагменты рецептов `help(...)`, извлеченные из выдачи хелпера, исполняются в песочнице."""

    @pytest.mark.parametrize("fmt", ["cf", "edt"])
    def test_parse_object_xml_examples_use_real_paths(self, worlds, monkeypatch, fmt):
        with worlds[fmt].session(monkeypatch) as s:
            recipe = _subst(_help_recipe(s, "parse_object_xml"))
            assert "Ext/Document.xml" not in recipe  # на CF такого файла в выгрузке нет
            calls = re.findall(r"(parse_object_xml\('[^']+'\))", recipe)
            assert len(calls) >= 3, calls  # директория, явный CF-путь и явный EDT-путь
            for call in calls:
                path = re.search(r"'([^']+)'", call).group(1)
                explicit_edt = re.search(r"/[^/]+/[^/]+\.mdo$", path) is not None
                if (explicit_edt and fmt != "edt") or (path.endswith(".xml") and fmt != "cf"):
                    continue  # явный путь другого формата в этом дереве не существует
                assert s.text(_subst(f"meta = {call}\n") + "print(meta['name'])") == "Док", call

    @pytest.mark.parametrize("fmt", ["cf", "edt"])
    def test_functional_option_composition_is_read_by_file(self, worlds, monkeypatch, fmt):
        with worlds[fmt].session(monkeypatch) as s:
            recipe = _subst(_help_recipe(s, "find_functional_options"))
            assert not re.search(r"parse_object_xml\([^()\n]*\)\[['\"]content", recipe), recipe  # KeyError на CF
            s.ok(
                _fragment(
                    recipe, "page = find_functional_options('', include_code=False, include_content=False, limit=50)"
                )
            )
            rows = s.json("print(json.dumps(len(page['xml_options']), ensure_ascii=False))\n")
            assert rows >= 1
            read = _fragment(recipe, "if page['xml_options']:")
            for i in range(rows):
                s.ok(read.replace("page['xml_options'][0]", f"page['xml_options'][{i}]"))
            assert s.json("print(json.dumps([type(detail).__name__, fo['name']], ensure_ascii=False))\n") == [
                "str",
                _FO,
            ]

    @pytest.mark.parametrize("fmt", ["cf", "edt"])
    def test_code_helper_fragments_run_in_full_mode(self, worlds, monkeypatch, fmt):
        monkeypatch.setenv("RLM_STRATEGY_MODE", "full")  # тула rlm_help в этом режиме нет
        with worlds[fmt].session(monkeypatch) as s:
            # find_callers: плейсхолдеры вместо неопределенных proc и hint
            recipe = _help_recipe(s, "find_callers")
            assert "caller_is_export" in recipe
            s.ok(_recipe_code(recipe).replace("'ИмяПроцедуры'", "'Экспорт1'"))  # на HEAD: NameError — proc
            assert s.json("print(json.dumps(len(hits), ensure_ascii=False))\n") >= 1
            # find_enum_values: ошибочное имя не роняет чтение
            first = _fragment(_help_recipe(s, "find_enum_values"), "result = find_enum_values(")
            assert "Первый:" in s.ok(first)["stdout"]
            assert "не найдено" in s.ok(first.replace(_ENUM, "Перечисление.Нет"))["stdout"].lower()
            # find_definition: переход — хелпер песочницы
            recipe = _help_recipe(s, "find_definition")
            assert "rlm_help(" not in recipe and "help('find_register_movements')" in recipe
            assert _help_recipe(s, "find_register_movements").startswith("TRACE DOCUMENT REGISTER MOVEMENTS")
            # code_metrics и get_module_outline
            metrics = _fragment(_help_recipe(s, "code_metrics"), "m = code_metrics(")
            s.ok(metrics.replace("'path/to/Module.bsl'", repr(_module(fmt, "CommonModules", "ОбщМод", "Module.bsl"))))
            outline = _fragment(_help_recipe(s, "get_module_outline"), "mods = find_module(")
            s.ok(outline.replace("'Расчёты'", "'ОбщМод'"))
            assert s.json(
                "print(json.dumps([o['path'].endswith('Module.bsl'), 'totals' in o], ensure_ascii=False))\n"
            ) == [True, True]
            # search: обе объявленные области возвращают строки
            recipe = _help_recipe(s, "search")
            assert "scope='attributes'" in recipe and "scope='predefined'" in recipe
            found = s.json(
                "print(json.dumps([len(search('Сумма', scope='attributes')), "
                "len(search('РеализуемыеАктивы', scope='predefined'))], ensure_ascii=False))\n"
            )
            assert found == [2, 1], found

    @pytest.mark.parametrize("fmt", ["cf", "edt"])
    def test_read_procedure_recipe_survives_an_empty_module_list(self, worlds, monkeypatch, fmt):
        with worlds[fmt].session(monkeypatch) as s:
            code = _recipe_code(_help_recipe(s, "read_procedure"))
            assert (
                s.ok(code)["stdout"].strip() == "Не найдено"
            )  # find_module('Name') пуст: ни full, ни bodies не читаются
            found = s.ok(code.replace("'Name'", "'ОбщМод'"))
            assert "Экспорт1 export= True" in found["stdout"] and "нет тела" in found["stdout"], found["stdout"]

    @pytest.mark.parametrize("fmt", ["cf", "edt"])
    def test_extract_procedures_recipe_survives_missing_object_and_lists_procedures(self, worlds, monkeypatch, fmt):
        with worlds[fmt].session(monkeypatch) as s:
            recipe = _help_recipe(s, "extract_procedures")
            assert "'Заказ'" not in recipe, recipe
            code = _recipe_code(recipe)
            assert s.ok(code)["stdout"].strip() == "Не найдено"  # буквальная вставка без объекта не падает
            prefix = code.replace("РеализацияТоваровУслуг", "До")
            assert s.text(prefix) == "Не найдено"  # есть Док, но объекта с точным именем До нет
            found = s.ok(_subst(code))
            assert "ЗаполнитьТестовыйДокумент" in found["stdout"], found["stdout"]

    @pytest.mark.parametrize("fmt", ["cf", "edt"])
    def test_find_callers_context_recipe_survives_an_empty_module_list(self, worlds, monkeypatch, fmt):
        with worlds[fmt].session(monkeypatch) as s:
            code = _recipe_code(_help_recipe(s, "find_callers_context"))
            # В свежей сессии `exports` не определен: при пустом find_module('Name') батч не выполняется.
            assert s.ok(code)["stdout"].strip() == "Не найдено"
            found = s.ok(code.replace("'Name'", "'ОбщМод'"))
            assert "Экспорт1 <-" in found["stdout"], found["stdout"]

    @pytest.mark.parametrize("fmt", ["cf", "edt"])
    def test_find_register_movements_recipe_executes_as_a_whole(self, worlds, monkeypatch, fmt):
        with worlds[fmt].session(monkeypatch) as s:
            recipe = _help_recipe(s, "find_register_movements")
            code = _subst(_recipe_code(recipe))
            ast.parse(code)
            s.ok(code)
            assert s.json(
                "print(json.dumps(['code_registers' in result, 'writers' in writers_res], ensure_ascii=False))\n"
            ) == [
                True,
                True,
            ]
            # Непроводимый документ (без code_registers) тоже проходит рецепт целиком без ошибок.
            s.ok(code.replace("'Док'", "'ДокНепроводимый'"))

    def test_print_forms_works_for_any_category_with_manager_module(self, tmp_path, monkeypatch):
        files = {
            "Configuration.xml": CF_DESCRIPTOR,
            "Catalogs/СпрПечать.xml": _cf_obj("Catalog", "СпрПечать"),
            "Catalogs/СпрПечать/Ext/ManagerModule.bsl": _MGR_TAIL,
            "Catalogs/СпрБезМенеджера.xml": _cf_obj("Catalog", "СпрБезМенеджера"),
        }
        w = _World(tmp_path, tmp_path / "cf", files)
        with w.session(monkeypatch) as s:
            got = s.json(
                "print(json.dumps([[p['name'] for p in find_print_forms('Справочник.СпрПечать')['print_forms']], "
                "find_print_forms('Справочник.СпрБезМенеджера')['print_forms']], ensure_ascii=False))\n"
            )
            assert got == [["Накладная"], []], got
