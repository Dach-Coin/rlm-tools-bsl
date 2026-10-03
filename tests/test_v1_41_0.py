"""v1.41.0 — домены хелперов: агент сам выбирает, какие подписи ему нужны.

Релиз read-time: схема индекса, ``BUILDER_VERSION`` и выход сборщика не тронуты.
Меняется только то, какие подписи хелперов и какой текст агент получает на старте
сессии и по ходу работы. Приватные фикстуры других файлов не импортируются
(каталог ``tests`` не package) — деревья собираются здесь же.
"""

from __future__ import annotations

import json
import pathlib
import re

import pytest

from rlm_tools_bsl.bsl_helpers import build_helper_metadata_snapshot
from rlm_tools_bsl.bsl_knowledge import _BUSINESS_RECIPES
from rlm_tools_bsl.bsl_strategy_data import (
    ALL_CATALOG,
    FILE_HELPER_NAMES,
    HELPER_CORE,
    HELPER_DOMAINS,
    RECIPE_NON_STEP_FRAGMENTS,
    domain_of_topic,
    domains_param_description,
    normalize_domains,
    recipe_mentions,
    render_domain_table,
)

CF_DESCRIPTOR = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">\n'
    '  <Configuration uuid="00000000-0000-0000-0000-000000000001">\n'
    "    <Properties><Name>Тест</Name></Properties>\n"
    "  </Configuration>\n"
    "</MetaDataObject>\n"
)


def _write(path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _recipe_texts(topic: str) -> dict[str, str]:
    """Тексты темы по частям: compact, full и code_hint (если есть)."""
    recipe = _BUSINESS_RECIPES[topic]
    out = {
        "compact": "\n".join(recipe.get("compact") or []),
        "full": "\n".join(recipe.get("full") or []),
    }
    if recipe.get("code_hint"):
        out["code_hint"] = recipe["code_hint"]
    return out


def _catalog_names() -> list[str]:
    return list(build_helper_metadata_snapshot()) + list(FILE_HELPER_NAMES)


# ═══════════════════════ Задача 1 — данные доменов и растяжки ═══════════════════════


class TestDomainDataStretch:
    """Растяжки данных: новый хелпер без домена, устаревшее исключение или рецепт,
    ведущий к вызову вслепую, роняют тест, а не уезжают агенту молча."""

    def test_every_snapshot_helper_is_in_core_or_a_domain(self):
        snap = build_helper_metadata_snapshot()
        in_domains = {name for d in HELPER_DOMAINS.values() for name in d["helpers"]}
        orphans = sorted(name for name in snap if name not in HELPER_CORE and name not in in_domains)
        assert not orphans, f"хелперы без домена: {orphans}"

    def test_every_core_and_domain_name_exists_in_snapshot(self):
        snap = build_helper_metadata_snapshot()
        names = set(HELPER_CORE) | {name for d in HELPER_DOMAINS.values() for name in d["helpers"]}
        assert not sorted(names - set(snap)), sorted(names - set(snap))

    def test_core_does_not_intersect_any_domain(self):
        for key, domain in HELPER_DOMAINS.items():
            assert not set(HELPER_CORE) & set(domain["helpers"]), key

    def test_topics_are_bound_exactly_once_and_exist(self):
        bound: list[str] = []
        for domain in HELPER_DOMAINS.values():
            bound.extend(domain["topics"])
        assert sorted(bound) == sorted(_BUSINESS_RECIPES), "каждая тема — ровно в одном домене"
        for topic in _BUSINESS_RECIPES:
            assert domain_of_topic(topic) in HELPER_DOMAINS

    def test_domain_keys_are_locked(self):
        assert list(HELPER_DOMAINS) == ["документ", "структура", "код", "связи", "расширения", "поиск"]
        assert ALL_CATALOG == "весь каталог"
        assert ALL_CATALOG not in HELPER_DOMAINS

    def test_domain_sizes_match_the_plan(self):
        """Размеры §3.3 плана (с git_search): документ 22, структура 18, код 16,
        связи 20, расширения 14, поиск 17; + find_unresolved_calls в v1.42.0 (код 17,
        расширения 15)."""
        sizes = {key: len(d["helpers"]) for key, d in HELPER_DOMAINS.items()}
        assert sizes == {"документ": 22, "структура": 18, "код": 17, "связи": 20, "расширения": 15, "поиск": 17}
        for key, d in HELPER_DOMAINS.items():
            assert len(set(d["helpers"])) == len(d["helpers"]), f"дубль в домене {key}"

    @pytest.mark.parametrize("topic", sorted(_BUSINESS_RECIPES))
    def test_recipe_mentions_are_covered_by_core_or_topic_domain(self, topic):
        """Рецепт темы исполним подписями ядра и домена этой темы: каждый упомянутый
        хелпер лежит в ядре, среди файловых или в домене темы."""
        domain = domain_of_topic(topic)
        allowed = set(HELPER_CORE) | set(FILE_HELPER_NAMES) | set(HELPER_DOMAINS[domain]["helpers"])
        names = _catalog_names()
        for part, text in _recipe_texts(topic).items():
            missing = [n for n in recipe_mentions(text, topic, names) if n not in allowed]
            assert not missing, f"{topic}/{part}: вне ядра и домена «{domain}»: {missing}"

    def test_mentions_without_parentheses_are_counted(self):
        names = _catalog_names()
        texts = _recipe_texts("проведение")
        assert "find_register_movements" in recipe_mentions(texts["compact"], "проведение", names)
        full = recipe_mentions(texts["full"], "проведение", names)
        assert "safe_grep" in full and "git_search" in full
        ext = recipe_mentions(_recipe_texts("расширения")["compact"], "расширения", names)
        for name in ("find_attributes", "parse_object_xml", "search"):
            assert name in ext, name

    def test_every_non_step_fragment_is_still_in_its_topic(self):
        """Устаревшее исключение (фрагмент ушёл из рецепта) роняет тест."""
        for topic, fragments in RECIPE_NON_STEP_FRAGMENTS.items():
            joined = "\n".join(_recipe_texts(topic).values())
            for fragment in fragments:
                assert fragment in joined, f"{topic}: фрагмента нет в рецепте: {fragment!r}"

    def test_fragment_removes_the_name_only_inside_itself(self):
        names = _catalog_names()
        texts = _recipe_texts("себестоимость")
        compact = recipe_mentions(texts["compact"], "себестоимость", names)
        assert "analyze_document_flow" not in compact
        assert "find_register_movements" not in compact
        full = recipe_mentions(texts["full"], "себестоимость", names)
        assert "analyze_document_flow" in full
        proc = recipe_mentions(_recipe_texts("проведение")["compact"], "проведение", names)
        assert "find_call_hierarchy" not in proc
        assert "search" not in proc, "шаги compact не упоминают хелпер search"

    def test_mentions_follow_catalog_order_and_whole_words(self):
        names = ["find_module", "search", "search_objects", "grep"]
        text = "search_objects('x'); safe_grep-маршрут; find_module/search; rlm_help"
        assert recipe_mentions(text, "любая", names) == ["find_module", "search", "search_objects"]


class TestDomainNormalizer:
    """Один нормализатор на rlm_start, rlm_execute и rlm_help."""

    def test_list_string_and_comma_forms_are_equivalent(self):
        a = normalize_domains(["документ", "код"])
        b = normalize_domains("документ, код")
        c = normalize_domains(['"документ"', "код"])
        d = normalize_domains(["документ, код"])
        e = normalize_domains('["документ", "код"]')
        for choice in (a, b, c, d, e):
            assert choice.keys == ("документ", "код"), choice
            assert not choice.ignored and choice.ignored_total == 0

    def test_keys_follow_domain_order_and_are_case_insensitive(self):
        choice = normalize_domains(["ПОИСК", "Документ", "поиск"])
        assert choice.keys == ("документ", "поиск")

    @pytest.mark.parametrize("alias", ["весь каталог", "all", "*", "все", "всё", "весь", "Весь  Каталог", "ALL"])
    def test_all_catalog_aliases(self, alias):
        choice = normalize_domains([alias])
        assert choice.all_catalog and choice.recognized

    def test_explicit_empty_list_is_core_choice(self):
        choice = normalize_domains([])
        assert choice.explicit_empty and not choice.recognized and choice.ignored_total == 0

    @pytest.mark.parametrize("value", ["[]", " [ ] ", "[\n]"])
    def test_empty_list_as_json_string_is_core_choice(self, value):
        """Строка «[]» — тот же явный пустой список, что FastMCP получает из неё
        разбором JSON: прямой вызов обёртки тула не расходится с вызовом по MCP."""
        choice = normalize_domains(value)
        assert choice.explicit_empty and not choice.recognized and choice.ignored_total == 0

    @pytest.mark.parametrize("value", [None, "", "  ", [""], [" ", ","], ["[]"], '"[]"', "''"])
    def test_missing_or_blank_is_not_an_explicit_choice(self, value):
        choice = normalize_domains(value)
        assert not choice.explicit_empty
        assert not choice.recognized
        assert choice.ignored_total == 0

    def test_partially_unknown_keeps_recognized(self):
        choice = normalize_domains(["код", "кот"])
        assert choice.keys == ("код",)
        assert choice.ignored == ("кот",) and choice.ignored_total == 1

    def test_hundreds_of_unknown_values_stay_bounded(self):
        choice = normalize_domains([f"мусор{i}" for i in range(500)] + ["x" * 5000])
        assert choice.ignored_total == 501
        assert len(choice.ignored) <= 3
        assert all(len(v) <= 17 for v in choice.ignored)
        assert choice.ignored_truncated

    def test_non_printable_fragments_are_neutralized(self):
        choice = normalize_domains(["код\nподделка\x1b[31m"])
        assert choice.ignored_total == 1
        assert all(ch.isprintable() for v in choice.ignored for ch in v)

    def test_response_key_is_bounded_to_200(self):
        choice = normalize_domains(
            ["документ", "структура", "код", "связи", "расширения", "поиск"]
            + [f"мусорноезначение{i}" for i in range(300)]
        )
        key = choice.response_key()
        assert len(json.dumps(key, ensure_ascii=False)) <= 200
        assert key["selected"] == ["документ", "структура", "код", "связи", "расширения", "поиск"]
        assert key["ignored_total"] == 300 and key["ignored_truncated"] is True

    def test_response_key_all_catalog(self):
        assert normalize_domains("all").response_key() == {"selected": [ALL_CATALOG], "ignored": []}


class TestDomainTableAndDescription:
    def test_table_carries_every_key_and_label(self):
        table = render_domain_table()
        for key, domain in HELPER_DOMAINS.items():
            assert f"{key} ({domain['label']}):" in table, key

    def test_table_with_names_lists_every_domain_helper(self):
        table = render_domain_table(with_names=True)
        for key, domain in HELPER_DOMAINS.items():
            line = next(ln for ln in table.splitlines() if ln.startswith(f"{key} ("))
            for name in domain["helpers"]:
                assert re.search(rf"(?<!\w){name}(?!\w)", line), (key, name)
        assert "git_search" in table and "только под git" in table

    def test_compact_table_has_no_names(self):
        table = render_domain_table(with_names=False)
        assert "find_register_movements" not in table
        for key in HELPER_DOMAINS:
            assert f"{key} (" in table

    def test_param_description_budget_and_contents(self):
        text = domains_param_description()
        assert len(text) <= 3000, len(text)
        assert text.startswith("Ключи: документ | структура | код | связи | расширения | поиск | весь каталог.")
        assert "rlm_execute" in text and "[]" in text
        assert "RLM_CATALOG_MODE=all" in text
        assert render_domain_table(with_names=True) in text

    def test_compact_param_description_is_cheaper(self):
        full = domains_param_description(with_names=True)
        compact = domains_param_description(with_names=False)
        assert len(compact) < len(full) - 1000


# ═══════════════════ Задачи 2–4, 8, 11 — выбор, догрузка, выдача по имени ═══════════════════

import asyncio  # noqa: E402
import logging  # noqa: E402
import os  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402

from rlm_tools_bsl import server  # noqa: E402
from rlm_tools_bsl.bsl_strategy_data import ALLOWED_DOMAIN_VALUES, domain_helper_names  # noqa: E402

_ALLOWED = list(ALLOWED_DOMAIN_VALUES)
_FILE_NAMES = list(FILE_HELPER_NAMES)


@pytest.fixture
def cf(tmp_path, monkeypatch):
    """Маленькое ПОДДЕРЖИВАЕМОЕ CF-дерево вне git (git-обнаружению запрещено
    подниматься выше tmp_path — иначе basetemp внутри репозитория поднял бы git_search)."""
    root = tmp_path / "cf"
    _write(root / "Configuration.xml", CF_DESCRIPTOR)
    _write(root / "Documents" / "Док" / "Ext" / "ObjectModule.bsl", "Процедура П() Экспорт\nКонецПроцедуры\n")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.resolve()))
    return root


@pytest.fixture
def generic_tree(tmp_path):
    """Чужой формат без единого .bsl — generic-режим."""
    root = tmp_path / "foreign"
    _write(root / "readme.txt", "hello\n")
    return root


def _call_tool(name: str, args: dict) -> dict:
    """Публичный вызов через FastMCP: так же, как его делает клиент."""
    res = asyncio.run(server.mcp.call_tool(name, args))
    content = res[0] if isinstance(res, tuple) else res
    return json.loads(content[0].text)


def _start(root, **kwargs) -> dict:
    kwargs.setdefault("query", "")
    return json.loads(server._rlm_start(path=str(root), **kwargs))


def _names(resp: dict) -> list[str]:
    return [sig.split("(", 1)[0] for sig in resp["available_functions"]]


def _sessions() -> set[str]:
    return set(server.session_manager._sessions)


def _expected_names(resp: dict, wanted: set[str]) -> list[str]:
    """Ожидаемый состав: выданные BSL-хелперы в порядке реестра сессии + файловые."""
    backend = server._sandboxes[resp["session_id"]]
    return [n for n in backend.registry_names if n in wanted] + _FILE_NAMES


def _domains_block(strategy: str) -> str:
    start = strategy.index("\n== ДОМЕНЫ ХЕЛПЕРОВ ==")
    end = strategy.find("\n==", start + 2)
    return strategy[start:] if end < 0 else strategy[start:end]


@pytest.mark.strategy_mode_slim
class TestStartRequiresExplicitChoice:
    """Задача 2: в slim/BSL/domains публичный rlm_start требует явный выбор."""

    def test_call_without_domains_is_refused_before_session(self, cf):
        before = _sessions()
        res = _call_tool("rlm_start", {"query": "x", "path": str(cf)})
        assert "domains" in res["error"] and "domains=[]" in res["error"]
        assert res["allowed_domains"] == _ALLOWED
        assert _sessions() == before, "отказ обязан случиться ДО создания сессии"
        with server._sandboxes_lock:
            assert not server._starting_sandbox_backends

    @pytest.mark.parametrize("value", [None, "", "   ", [" "], [",", ""], "null"])
    def test_blank_choice_is_refused(self, cf, value):
        before = _sessions()
        res = _call_tool("rlm_start", {"query": "x", "path": str(cf), "domains": value})
        assert "error" in res and res["allowed_domains"] == _ALLOWED
        assert "domains_ignored" not in res
        assert _sessions() == before

    @pytest.mark.parametrize("value", [["кот"], "кот, собака", ['"кот"']])
    def test_choice_without_a_single_known_key_is_refused(self, cf, value):
        before = _sessions()
        res = _call_tool("rlm_start", {"query": "x", "path": str(cf), "domains": value})
        assert "error" in res and res["allowed_domains"] == _ALLOWED
        assert res["domains_ignored"]["ignored"]
        assert _sessions() == before

    def test_explicit_empty_list_is_core_only(self, cf):
        res = _call_tool("rlm_start", {"query": "x", "path": str(cf), "domains": []})
        try:
            assert res["domains"] == {"selected": [], "ignored": []}
            assert _names(res) == _expected_names(res, set(HELPER_CORE))
        finally:
            server._rlm_end(res["session_id"])

    def test_empty_list_serialized_as_string_is_core_only(self, cf):
        """FastMCP сам разбирает строку-JSON: клиент, сериализующий массив, не
        получит отказа на осознанном «только ядро»."""
        res = _call_tool("rlm_start", {"query": "x", "path": str(cf), "domains": "[]"})
        try:
            assert res["domains"]["selected"] == []
            assert _names(res) == _expected_names(res, set(HELPER_CORE))
        finally:
            server._rlm_end(res["session_id"])

    def test_empty_list_string_past_fastmcp_is_core_only(self, cf):
        """Прямой вызов обёртки тула (мимо разбора JSON в FastMCP): строка «[]»
        означает то же «только ядро», что и у клиента MCP, а не отказ."""
        res = json.loads(asyncio.run(server.rlm_start(query="x", path=str(cf), domains="[]")))
        try:
            assert res["domains"]["selected"] == []
            assert _names(res) == _expected_names(res, set(HELPER_CORE))
        finally:
            server._rlm_end(res["session_id"])

    @pytest.mark.parametrize("value", [["код"], "код", '["код"]', ['"Код"']])
    def test_one_domain(self, cf, value):
        res = _call_tool("rlm_start", {"query": "x", "path": str(cf), "domains": value})
        try:
            assert res["domains"] == {"selected": ["код"], "ignored": []}
            wanted = set(HELPER_CORE) | domain_helper_names(["код"])
            assert _names(res) == _expected_names(res, wanted)
            assert "find_path" in _names(res) and "find_references_to_object" not in _names(res)
        finally:
            server._rlm_end(res["session_id"])

    def test_two_domains_as_comma_string(self, cf):
        res = _call_tool("rlm_start", {"query": "x", "path": str(cf), "domains": "документ, связи"})
        try:
            assert res["domains"]["selected"] == ["документ", "связи"]
            wanted = set(HELPER_CORE) | domain_helper_names(["документ", "связи"])
            assert _names(res) == _expected_names(res, wanted)
        finally:
            server._rlm_end(res["session_id"])

    @pytest.mark.parametrize("value", [["весь каталог"], ["all"], "*"])
    def test_all_catalog(self, cf, value):
        res = _call_tool("rlm_start", {"query": "x", "path": str(cf), "domains": value})
        try:
            assert res["domains"] == {"selected": [ALL_CATALOG], "ignored": []}
            backend = server._sandboxes[res["session_id"]]
            assert _names(res) == list(backend.registry_names) + _FILE_NAMES
            assert "Загружен весь каталог" in _domains_block(res["strategy"])
        finally:
            server._rlm_end(res["session_id"])

    def test_partially_unknown_choice_starts_and_reports(self, cf):
        res = _call_tool("rlm_start", {"query": "x", "path": str(cf), "domains": ["код", "кот"]})
        try:
            assert res["domains"] == {"selected": ["код"], "ignored": ["кот"]}
            assert "Не распознано: кот" in _domains_block(res["strategy"])
        finally:
            server._rlm_end(res["session_id"])

    def test_domains_key_and_block_bounded_with_hundreds_of_unknown_values(self, cf):
        junk = [f"неизвестныйдомен{i}" for i in range(400)] + ["я" * 3000]
        res = _call_tool("rlm_start", {"query": "x", "path": str(cf), "domains": ["поиск", *junk]})
        try:
            assert len(json.dumps(res["domains"], ensure_ascii=False)) <= 200
            assert res["domains"]["ignored_total"] == 401 and res["domains"]["ignored_truncated"] is True
            assert len(_domains_block(res["strategy"])) <= 400
        finally:
            server._rlm_end(res["session_id"])

    def test_internal_start_without_domains_is_core_only(self, cf):
        """Прямой _rlm_start(domains=None) — «только ядро» для тестов и встраивания."""
        res = _start(cf)
        try:
            assert res["domains"] == {"selected": [], "ignored": []}
            assert _names(res) == _expected_names(res, set(HELPER_CORE))
            assert "Загружено только ядро" in _domains_block(res["strategy"])
        finally:
            server._rlm_end(res["session_id"])


class TestModesThatIgnoreDomains:
    """Full, RLM_CATALOG_MODE=all и generic принимают прежний вызов без domains."""

    def test_full_accepts_old_call_and_answer_is_unchanged(self, cf):
        res = _call_tool("rlm_start", {"query": "x", "path": str(cf)})
        try:
            assert "domains" not in res
            backend = server._sandboxes[res["session_id"]]
            # Прежний алгоритм: все подписи реестра сессии (тексты воркера) + файловые.
            old = [entry["sig"] for entry in backend.registry_snapshot.values()]
            assert res["available_functions"][: len(old)] == old
            assert _names(res)[len(old) :] == _FILE_NAMES
            assert "== ДОМЕНЫ ХЕЛПЕРОВ" not in res["strategy"]
        finally:
            server._rlm_end(res["session_id"])

    def test_full_ignores_garbage_value(self, cf):
        res = _call_tool("rlm_start", {"query": "x", "path": str(cf), "domains": ["кот"]})
        try:
            assert "error" not in res and "domains" not in res
        finally:
            server._rlm_end(res["session_id"])

    @pytest.mark.strategy_mode_slim
    def test_catalog_all_accepts_old_call(self, cf, monkeypatch):
        monkeypatch.setenv("RLM_CATALOG_MODE", "all")
        res = _call_tool("rlm_start", {"query": "x", "path": str(cf)})
        try:
            assert res["domains"] == {"selected": [ALL_CATALOG], "ignored": []}
            backend = server._sandboxes[res["session_id"]]
            assert _names(res) == list(backend.registry_names) + _FILE_NAMES
            assert "== ДОМЕНЫ ХЕЛПЕРОВ" not in res["strategy"]
            assert "== HELPERS (compact index" not in res["strategy"]
        finally:
            server._rlm_end(res["session_id"])

    @pytest.mark.strategy_mode_slim
    def test_catalog_all_does_not_read_the_value(self, cf, monkeypatch):
        monkeypatch.setenv("RLM_CATALOG_MODE", "all")
        res = _call_tool("rlm_start", {"query": "x", "path": str(cf), "domains": ["кот"]})
        try:
            assert res["domains"] == {"selected": [ALL_CATALOG], "ignored": []}
        finally:
            server._rlm_end(res["session_id"])

    @pytest.mark.strategy_mode_slim
    @pytest.mark.parametrize("value", [None, ["кот"]])
    def test_generic_accepts_old_call(self, generic_tree, value):
        args = {"query": "x", "path": str(generic_tree)}
        if value is not None:
            args["domains"] = value
        res = _call_tool("rlm_start", args)
        try:
            assert res["source_support"] == "foreign_no_bsl"
            assert "domains" not in res
            assert _names(res) == _FILE_NAMES
        finally:
            server._rlm_end(res["session_id"])

    def test_tool_schemas_are_the_same_in_every_mode(self):
        """Схема rlm_start/rlm_execute одна и от окружения не зависит; domains в
        ней необязателен (обязательность проверяет сервер)."""
        probe = (
            "import asyncio, json\n"
            "import rlm_tools_bsl.server as s\n"
            "tools = {t.name: t.inputSchema for t in asyncio.run(s.mcp.list_tools())}\n"
            "print(json.dumps({k: tools[k] for k in ('rlm_start', 'rlm_execute')}, sort_keys=True))\n"
        )
        outs = []
        for mode, catalog in (("slim", "domains"), ("slim", "all"), ("full", "domains")):
            env = dict(os.environ, RLM_STRATEGY_MODE=mode, RLM_CATALOG_MODE=catalog)
            res = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, env=env, timeout=180)
            assert res.returncode == 0, res.stderr[-400:]
            outs.append(res.stdout.strip())
        assert outs[0] == outs[1] == outs[2]
        schemas = json.loads(outs[0])
        for tool in ("rlm_start", "rlm_execute"):
            assert "domains" in schemas[tool]["properties"]
            assert "domains" not in schemas[tool].get("required", [])
        assert schemas["rlm_start"]["required"] == ["query"]


def _exec(sid: str, code: str, **kwargs) -> dict:
    return json.loads(server._rlm_execute(sid, code, **kwargs))


def _sig(name: str) -> str:
    return build_helper_metadata_snapshot()[name]["sig"]


def _session(sid: str):
    return server.session_manager.get(sid)


@pytest.mark.strategy_mode_slim
class TestLazyDomainLoad:
    """Задача 3: попутная догрузка параметром rlm_execute(domains=…)."""

    def test_signatures_arrive_in_the_same_response(self, cf):
        res = _start(cf, domains=["код"])
        sid = res["session_id"]
        try:
            before = set(_names(res))
            r = _exec(sid, "print(1)", domains=["связи"])
            backend = server._sandboxes[sid]
            expected = [n for n in backend.registry_names if n in domain_helper_names(["связи"]) and n not in before]
            assert [s.split("(", 1)[0] for s in r["signatures"]] == expected
            assert r["signatures"] == [_sig(n) for n in expected]
            assert _session(sid).helper_domains == ["код", "связи"]
        finally:
            server._rlm_end(sid)

    def test_load_applies_when_agent_code_fails(self, cf):
        res = _start(cf, domains=[])
        sid = res["session_id"]
        try:
            r = _exec(sid, "1/0", domains=["поиск"])
            assert "ZeroDivisionError" in r["error"]
            assert any(s.startswith("search_regions(") for s in r["signatures"])
        finally:
            server._rlm_end(sid)

    def test_repeat_load_adds_nothing(self, cf):
        res = _start(cf, domains=[])
        sid = res["session_id"]
        try:
            assert _exec(sid, "print(1)", domains=["код"])["signatures"]
            again = _exec(sid, "print(1)", domains=["код"])
            assert "signatures" not in again and "domains_ignored" not in again
            assert _session(sid).domains_added == 1
        finally:
            server._rlm_end(sid)

    def test_typo_is_reported_with_allowed_keys(self, cf):
        res = _start(cf, domains=["код"])
        sid = res["session_id"]
        try:
            r = _exec(sid, "print(1)", domains=["кот"])
            assert r["domains_ignored"] == {"ignored": ["кот"], "allowed": _ALLOWED}
            assert "signatures" not in r
            assert _session(sid).helper_domains == ["код"]
        finally:
            server._rlm_end(sid)

    def test_hundreds_of_typos_stay_bounded(self, cf):
        res = _start(cf, domains=["код"])
        sid = res["session_id"]
        try:
            r = _exec(sid, "print(1)", domains=[f"опечатка{i}" for i in range(300)])
            ignored = r["domains_ignored"]
            assert len(ignored["ignored"]) <= 3 and ignored["ignored_total"] == 300
            assert ignored["ignored_truncated"] is True
        finally:
            server._rlm_end(sid)

    def test_refusal_before_execution_keeps_delivery_state(self, cf):
        res = _start(cf, domains=["код"], max_execute_calls=1)
        sid = res["session_id"]
        try:
            _exec(sid, "print(1)")
            session = _session(sid)
            delivered = set(session.delivered_helpers)
            r = _exec(sid, "print(1)", domains=["связи"])
            assert "limit exceeded" in r["error"]
            assert "signatures" not in r
            assert session.helper_domains == ["код"] and session.delivered_helpers == delivered
        finally:
            server._rlm_end(sid)

    def test_closed_session_refusal_does_not_deliver(self, cf):
        res = _start(cf, domains=["код"])
        sid = res["session_id"]
        session = _session(sid)
        server._rlm_end(sid)
        r = _exec(sid, "print(1)", domains=["связи"])
        assert "not found" in r["error"]
        assert session.helper_domains == ["код"]

    def test_helper_called_in_the_same_execute_that_loads_its_domain(self, cf):
        res = _start(cf, domains=["код"])
        sid = res["session_id"]
        try:
            r = _exec(sid, "find_references_to_object('Документ.Док')", domains=["связи"])
            assert r["signatures"].count(_sig("find_references_to_object")) == 1
            assert _session(sid).outside_helpers == ["find_references_to_object"]
        finally:
            server._rlm_end(sid)

    def test_all_catalog_session_does_not_read_the_param(self, cf):
        res = _start(cf, domains=["весь каталог"])
        sid = res["session_id"]
        try:
            r = _exec(sid, "find_references_to_object('Документ.Док')", domains=["кот"])
            assert "signatures" not in r and "domains_ignored" not in r
            assert _session(sid).outside_helpers == []
        finally:
            server._rlm_end(sid)

    def test_all_catalog_loaded_at_execute(self, cf):
        res = _start(cf, domains=[])
        sid = res["session_id"]
        try:
            r = _exec(sid, "print(1)", domains=["весь каталог"])
            backend = server._sandboxes[sid]
            assert len(r["signatures"]) == len([n for n in backend.registry_names if n not in HELPER_CORE])
            later = _exec(sid, "print(1)", domains=["кот"])
            assert "domains_ignored" not in later, "после «весь каталог» параметр не читается"
        finally:
            server._rlm_end(sid)


@pytest.mark.strategy_mode_slim
class TestSignatureOnFirstCallByName:
    """Задача 4: подпись хелперу, вызванному вне выданных, — один раз за сессию."""

    def test_once_per_helper(self, cf):
        res = _start(cf, domains=[])
        sid = res["session_id"]
        try:
            r = _exec(sid, "find_references_to_object('Документ.Док')")
            assert r["signatures"] == [_sig("find_references_to_object")]
            again = _exec(sid, "find_references_to_object('Документ.Док')")
            assert "signatures" not in again
            assert _session(sid).outside_helpers == ["find_references_to_object"]
        finally:
            server._rlm_end(sid)

    def test_delivered_after_a_failed_first_call(self, cf):
        """Хелпер упал (нет аргументов), но ответ доставлен: подпись приходит сразу."""
        res = _start(cf, domains=[])
        sid = res["session_id"]
        try:
            r = _exec(sid, "find_path()")
            assert r["error"] and "TypeError" in r["error"]
            assert r["signatures"] == [_sig("find_path")]
            assert "signatures" not in _exec(sid, "find_path('a', 'b')")
        finally:
            server._rlm_end(sid)

    def test_no_duplicate_with_the_error_hint(self, cf):
        """Подсказка о неверном именованном аргументе уже несёт подпись целиком."""
        res = _start(cf, domains=[])
        sid = res["session_id"]
        try:
            r = _exec(sid, "find_path('a', 'b', bogus=1)")
            assert _sig("find_path") in r["error"]
            assert "signatures" not in r
            assert "find_path" in _session(sid).delivered_helpers
        finally:
            server._rlm_end(sid)

    def test_delivered_helpers_are_not_repeated(self, cf):
        res = _start(cf, domains=["код"])
        sid = res["session_id"]
        try:
            r = _exec(sid, "find_path('a', 'b')\nfind_module('Док')")
            assert "signatures" not in r
            assert _session(sid).outside_helpers == []
        finally:
            server._rlm_end(sid)

    def test_by_name_group_follows_the_domain_group(self, cf):
        res = _start(cf, domains=[])
        sid = res["session_id"]
        try:
            r = _exec(sid, "find_references_to_object('Документ.Док')", domains=["поиск"])
            names = [s.split("(", 1)[0] for s in r["signatures"]]
            assert names[-1] == "find_references_to_object"
            assert "search_regions" in names[:-1]
        finally:
            server._rlm_end(sid)


@pytest.mark.strategy_mode_slim
class TestSignatureDeliveryInProcessMode:
    """Тот же контракт в process-режиме: подписи — из каталога родителя, история
    вызовов — от воркера; при hard timeout история неизвестна."""

    @pytest.fixture(autouse=True)
    def _process(self, monkeypatch):
        monkeypatch.setenv("RLM_SANDBOX_MODE", "process")

    def test_once_per_helper(self, cf):
        res = _start(cf, domains=[])
        sid = res["session_id"]
        try:
            assert res["limits"]["sandbox_mode"] == "process"
            r = _exec(sid, "find_references_to_object('Документ.Док')")
            assert r["signatures"] == [_sig("find_references_to_object")]
            assert "signatures" not in _exec(sid, "find_references_to_object('Документ.Док')")
        finally:
            server._rlm_end(sid)

    def test_hard_timeout_marks_history_unknown_and_restart_delivers(self, cf, caplog):
        res = _start(cf, domains=[], execution_timeout_seconds=2)
        sid = res["session_id"]
        try:
            with caplog.at_level(logging.INFO, logger="rlm_tools_bsl.server"):
                r = _exec(sid, "find_references_to_object('Документ.Док')\nwhile True:\n    pass")
            assert r["sandbox_state"]["status"] == "terminated"
            assert "signatures" not in r
            session = _session(sid)
            assert "find_references_to_object" not in session.delivered_helpers
            assert session.outside_unknown_executes == 1
            lines = [rec.getMessage() for rec in caplog.records if rec.getMessage().startswith("rlm_execute: session=")]
            assert any(" outside=unknown" in line for line in lines), lines
            r2 = _exec(sid, "find_references_to_object('Документ.Док')")
            assert r2["sandbox_state"]["status"] == "restarted"
            assert r2["signatures"] == [_sig("find_references_to_object")]
        finally:
            server._rlm_end(sid)


class _FakeBackend:
    def __init__(self, names):
        self.registry_names = tuple(names)


def _fake_session(names, *, domains=(), delivered=None, catalog_all=False, generation=1):
    from rlm_tools_bsl.session import Session

    session = Session(session_id="fake", path=".", query="")
    session.registry_view = server._registry_view(names)
    session.registry_generation = generation
    session.catalog_all_at_start = catalog_all
    session.helper_domains = list(domains)
    if delivered is None:
        delivered = set(names) if catalog_all else (set(HELPER_CORE) | domain_helper_names(domains)) & set(names)
    session.delivered_helpers = set(delivered)
    return session


def _result(generation, calls=(), error=None, state=None):
    from rlm_tools_bsl.sandbox import HelperCall
    from rlm_tools_bsl.sandbox_backend import BackendExecutionResult

    return BackendExecutionResult(
        stdout="",
        error=error,
        variables=[],
        helper_calls=[HelperCall(name, 0.01, seq=i) for i, name in enumerate(calls, 1)],
        sandbox_state=state,
        generation=generation,
    )


class TestGenerationChange:
    """Задача 4: после перезапуска воркера представление реестра перестраивается."""

    _NO_GIT = [n for n in build_helper_metadata_snapshot() if n != "git_search"]
    _WITH_GIT = list(build_helper_metadata_snapshot())

    def test_git_search_appears_in_a_selected_domain(self):
        session = _fake_session(self._NO_GIT, domains=["код"])
        extra, log = server._deliver_signatures(session, _FakeBackend(self._WITH_GIT), _result(2), None)
        assert extra == {"signatures": [_sig("git_search")]}
        assert "git_search" in session.delivered_helpers and session.registry_generation == 2
        assert log == ""

    def test_git_search_appears_outside_selected_domains(self):
        session = _fake_session(self._NO_GIT, domains=["структура"])
        extra, _ = server._deliver_signatures(session, _FakeBackend(self._WITH_GIT), _result(2), None)
        assert extra == {}
        assert "git_search" in session.registry_view and "git_search" not in session.delivered_helpers

    def test_git_search_disappears(self):
        session = _fake_session(self._WITH_GIT, domains=["поиск"])
        assert "git_search" in session.delivered_helpers
        extra, _ = server._deliver_signatures(session, _FakeBackend(self._NO_GIT), _result(2), None)
        assert extra == {"registry_changed": {"removed": ["git_search"]}}
        assert "git_search" not in session.delivered_helpers and "git_search" not in session.registry_view

    def test_new_helper_called_in_the_first_execute_of_the_new_generation(self):
        session = _fake_session(self._NO_GIT, domains=["код"])
        extra, log = server._deliver_signatures(session, _FakeBackend(self._WITH_GIT), _result(2, ["git_search"]), None)
        assert extra == {"signatures": [_sig("git_search")]}
        assert session.outside_helpers == ["git_search"]
        assert " outside=git_search" in log

    def test_full_catalog_session_gets_new_names(self):
        session = _fake_session(self._NO_GIT, catalog_all=True)
        extra, _ = server._deliver_signatures(session, _FakeBackend(self._WITH_GIT), _result(2), None)
        assert extra == {"signatures": [_sig("git_search")]}

    def test_unchanged_registry_adds_no_fields(self):
        session = _fake_session(self._WITH_GIT, catalog_all=True)
        extra, log = server._deliver_signatures(
            session, _FakeBackend(self._WITH_GIT), _result(1, ["find_module"]), None
        )
        assert extra == {} and log == ""

    def test_terminated_response_does_not_claim_absence_of_misses(self):
        session = _fake_session(self._WITH_GIT, domains=[])
        extra, log = server._deliver_signatures(
            session, _FakeBackend(self._WITH_GIT), _result(1, state={"status": "terminated"}), ["код"]
        )
        assert " outside=unknown" in log and session.outside_unknown_executes == 1
        # Явно запрошенный домен применяется и в таком ответе.
        assert extra["signatures"] and "find_path" in session.delivered_helpers


class TestRegistryView:
    def test_foreign_names_are_dropped_and_texts_come_from_the_catalog(self):
        view = server._registry_view(["find_module", "evil_helper", 123, "search_objects"])
        assert list(view) == ["find_module", "search_objects"]
        assert view["find_module"] is build_helper_metadata_snapshot()["find_module"]

    def test_worker_texts_never_reach_the_agent(self, cf, monkeypatch):
        """Подменённые воркером тексты и чужое имя не попадают в available_functions."""
        from rlm_tools_bsl.sandbox_backend import InlineSandboxBackend

        real_names = InlineSandboxBackend.registry_names
        real_snapshot = InlineSandboxBackend.registry_snapshot
        monkeypatch.setattr(
            InlineSandboxBackend, "registry_names", property(lambda self: (*real_names.fget(self), "evil_helper"))
        )
        monkeypatch.setattr(
            InlineSandboxBackend,
            "registry_snapshot",
            property(lambda self: {n: {**e, "sig": "ПОДМЕНА"} for n, e in real_snapshot.fget(self).items()}),
        )
        res = _start(cf)  # conftest: full — весь каталог
        try:
            assert "evil_helper" not in _names(res)
            assert not any("ПОДМЕНА" in s for s in res["available_functions"])
            assert "ПОДМЕНА" not in res["strategy"]
            snap = build_helper_metadata_snapshot()
            bsl = [s for s in res["available_functions"] if s.split("(", 1)[0] in snap]
            assert bsl == [snap[s.split("(", 1)[0]]["sig"] for s in bsl]
        finally:
            server._rlm_end(res["session_id"])


class TestRegistrySnapshotValidation:
    """init_ok.registry_snapshot неверной формы — нарушение протокола воркера."""

    _GOOD = {"sig": "f()", "cat": "code", "kw": ["a"], "recipe": ""}

    @pytest.mark.parametrize(
        "snapshot",
        [
            [],
            "строка",
            {"f": []},
            {"1bad": {"sig": "f()", "cat": "c", "kw": [], "recipe": ""}},
            {"": {"sig": "f()", "cat": "c", "kw": [], "recipe": ""}},
            {"f": {"sig": 1, "cat": "c", "kw": [], "recipe": ""}},
            {"f": {"sig": "f()", "cat": "c", "kw": "a", "recipe": ""}},
            {"f": {"sig": "f()", "cat": "c", "kw": [1], "recipe": ""}},
            {"f": {"sig": "f()", "kw": [], "recipe": ""}},
        ],
    )
    def test_bad_shapes_raise_protocol_error(self, snapshot):
        from rlm_tools_bsl._sandbox_protocol import SandboxProtocolError
        from rlm_tools_bsl.sandbox_process import ProcessSandboxBackend

        with pytest.raises(SandboxProtocolError):
            ProcessSandboxBackend._validate_registry_snapshot({"registry_snapshot": snapshot})

    @pytest.mark.parametrize("payload", [{}, {"registry_snapshot": None}, {"registry_snapshot": {}}])
    def test_empty_or_absent_is_accepted(self, payload):
        from rlm_tools_bsl.sandbox_process import ProcessSandboxBackend

        ProcessSandboxBackend._validate_registry_snapshot(payload)
        ProcessSandboxBackend._validate_registry_snapshot({"registry_snapshot": {"f": dict(self._GOOD)}})

    def _corrupt_init(self, monkeypatch):
        from rlm_tools_bsl.sandbox_process import ProcessSandboxBackend

        real = ProcessSandboxBackend._wait_init_response

        def corrupt(self_, *args, **kwargs):
            payload = real(self_, *args, **kwargs)
            payload["registry_snapshot"] = [["find_module", {}]]
            return payload

        monkeypatch.setattr(ProcessSandboxBackend, "_wait_init_response", corrupt)

    def test_bad_snapshot_on_start_is_a_controlled_refusal(self, cf, monkeypatch):
        monkeypatch.setenv("RLM_SANDBOX_MODE", "process")
        self._corrupt_init(monkeypatch)
        before = _sessions()
        res = _start(cf)
        assert "SandboxStartupError" in res["error"] and "protocol error" in res["error"]
        assert _sessions() == before

    def test_bad_snapshot_on_restart_is_a_controlled_refusal(self, cf, monkeypatch):
        monkeypatch.setenv("RLM_SANDBOX_MODE", "process")
        res = _start(cf, execution_timeout_seconds=2)
        sid = res["session_id"]
        try:
            r = _exec(sid, "while True:\n    pass")
            assert r["sandbox_state"]["status"] == "terminated"
            self._corrupt_init(monkeypatch)
            r2 = _exec(sid, "print(1)")
            assert "Sandbox restart failed" in r2["error"] and "protocol error" in r2["error"]
        finally:
            server._rlm_end(sid)


@pytest.mark.strategy_mode_slim
class TestCatalogModeLifetime:
    def test_get_catalog_mode_and_warning(self, monkeypatch):
        from rlm_tools_bsl.bsl_knowledge import catalog_mode_env_warning, get_catalog_mode

        assert get_catalog_mode() == "domains" and catalog_mode_env_warning() is None
        for raw, mode in (("all", "all"), (" ALL ", "all"), ("domains", "domains"), ("bogus", "domains")):
            monkeypatch.setenv("RLM_CATALOG_MODE", raw)
            assert get_catalog_mode() == mode
        assert "bogus" in catalog_mode_env_warning()

    def test_conftest_removes_catalog_mode(self, request):
        """Одна проверка окружения вакуумна: в чистом окружении она прошла бы и без
        фикстуры. Поэтому проверяется и то, что autouse-фикстура действует на тест, и
        то, что она действительно снимает переменную."""
        import ast
        import pathlib

        import conftest

        assert "_isolate_catalog_mode_env" in request.fixturenames
        # Исходник — по `conftest.__file__`, а НЕ `inspect.getsource(функции)`: тот идёт
        # по `co_filename`, а кеш переписанных pytest-модулей (`__pycache__/*-pytest-*.pyc`)
        # Windows и WSL делят на одном дереве — в WSL там оставался путь `D:\…`, и
        # getsource падал OSError. `__file__` модуля всегда настоящий.
        source = pathlib.Path(conftest.__file__).read_text(encoding="utf-8")
        fixture = next(
            node
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.FunctionDef) and node.name == "_isolate_catalog_mode_env"
        )
        assert 'monkeypatch.delenv("RLM_CATALOG_MODE", raising=False)' in ast.get_source_segment(source, fixture)
        assert "RLM_CATALOG_MODE" not in os.environ

    def test_session_keeps_its_way_of_delivery(self, cf, monkeypatch):
        res = _start(cf, domains=["код"])
        sid = res["session_id"]
        try:
            monkeypatch.setenv("RLM_CATALOG_MODE", "all")
            r = _exec(sid, "find_references_to_object('Документ.Док')")
            assert r["signatures"] == [_sig("find_references_to_object")], "сессия осталась в domains"
            fresh = _start(cf)
            try:
                assert fresh["domains"]["selected"] == [ALL_CATALOG]
                assert "signatures" not in _exec(fresh["session_id"], "find_references_to_object('Документ.Док')")
            finally:
                server._rlm_end(fresh["session_id"])
        finally:
            server._rlm_end(sid)


def _log_lines(caplog, prefix: str) -> list[str]:
    return [rec.getMessage() for rec in caplog.records if rec.getMessage().startswith(prefix)]


@pytest.mark.strategy_mode_slim
class TestDeliveryLog:
    """Задача 8: журнал выдачи — метрика нарезки и счётчик лишних ходов."""

    def test_start_line_query_last(self, cf, caplog):
        with caplog.at_level(logging.INFO, logger="rlm_tools_bsl.server"):
            res = _start(cf, domains=["код", "кот"], query="найди\nвсё")
        try:
            sid = res["session_id"]
            lines = [ln for ln in _log_lines(caplog, f"rlm_start: session={sid} domains=")]
            assert lines == [f"rlm_start: session={sid} domains=<код> ignored=<кот> query=<найди⏎всё>"]
        finally:
            server._rlm_end(res["session_id"])

    def test_start_line_types(self, cf, caplog):
        with caplog.at_level(logging.INFO, logger="rlm_tools_bsl.server"):
            core = _start(cf, domains=[])
            full = _start(cf, domains=["весь каталог"])
        try:
            assert _log_lines(caplog, f"rlm_start: session={core['session_id']} domains=<core> ignored=<->")
            assert _log_lines(caplog, f"rlm_start: session={full['session_id']} domains=<весь каталог>")
        finally:
            server._rlm_end(core["session_id"])
            server._rlm_end(full["session_id"])

    def test_query_follows_the_execute_code_switch(self, cf, caplog, monkeypatch):
        monkeypatch.setenv("RLM_LOG_EXECUTE_CODE", "0")
        with caplog.at_level(logging.INFO, logger="rlm_tools_bsl.server"):
            res = _start(cf, domains=["код"], query="секрет")
        try:
            line = _log_lines(caplog, f"rlm_start: session={res['session_id']} domains=")[0]
            assert "query=" not in line and "секрет" not in line
        finally:
            server._rlm_end(res["session_id"])

    def test_rejected_start_is_logged(self, cf, caplog):
        with caplog.at_level(logging.INFO, logger="rlm_tools_bsl.server"):
            _call_tool("rlm_start", {"query": "q", "path": str(cf), "domains": ["кот"]})
        assert _log_lines(caplog, "rlm_start: session=- domains=<rejected> ignored=<кот> query=<q>")

    def test_execute_fields_stand_before_code(self, cf, caplog):
        res = _start(cf, domains=["код"])
        sid = res["session_id"]
        try:
            with caplog.at_level(logging.INFO, logger="rlm_tools_bsl.server"):
                _exec(sid, "find_references_to_object('Документ.Док')", domains=["поиск", "кот"])
            line = _log_lines(caplog, f"rlm_execute: session={sid} call=")[-1]
            code_at = line.index(" code=<")
            for field in (" domains+=<поиск>", " domains_ignored=<кот>", " outside=find_references_to_object"):
                assert 0 <= line.index(field) < code_at, (field, line)
        finally:
            server._rlm_end(sid)

    def test_execute_without_delivery_adds_no_fields(self, cf, caplog):
        res = _start(cf, domains=["код"])
        sid = res["session_id"]
        try:
            with caplog.at_level(logging.INFO, logger="rlm_tools_bsl.server"):
                _exec(sid, "find_path('a', 'b')")
            line = _log_lines(caplog, f"rlm_execute: session={sid} call=")[-1]
            assert "domains+=" not in line and "outside=" not in line
        finally:
            server._rlm_end(sid)

    def test_end_line_totals(self, cf, caplog):
        res = _start(cf, domains=["код"])
        sid = res["session_id"]
        _exec(sid, "find_references_to_object('Документ.Док')")
        _exec(sid, "print(1)", domains=["поиск"])
        with caplog.at_level(logging.INFO, logger="rlm_tools_bsl.server"):
            server._rlm_end(sid)
        line = _log_lines(caplog, f"rlm_end: session={sid} calls=")[0]
        assert line.endswith("domains=<код,поиск> added=1 outside=1 outside_unknown=0"), line

    def test_end_line_marks_execute_still_running(self, cf, caplog):
        """rlm_end не ждёт активный execute, а inline не прерывает запущенный код: его
        счётчики допишутся ПОСЛЕ итоговой строки — строка обязана сказать, что неполна."""
        import threading

        res = _start(cf, domains=["код"])
        sid = res["session_id"]
        backend = server._sandboxes[sid]
        assert backend.mode == "inline"
        inner_execute = backend._sandbox.execute
        entered, release = threading.Event(), threading.Event()

        def slow_execute(code):
            entered.set()
            release.wait(30)
            return inner_execute(code)

        backend._sandbox.execute = slow_execute
        box: dict = {}
        worker = threading.Thread(
            target=lambda: box.update(res=_exec(sid, "find_references_to_object('Документ.Док')")), daemon=True
        )
        try:
            with caplog.at_level(logging.INFO, logger="rlm_tools_bsl.server"):
                worker.start()
                assert entered.wait(30)
                server._rlm_end(sid)
                release.set()
                worker.join(30)
            assert not worker.is_alive()
            end = _log_lines(caplog, f"rlm_end: session={sid} calls=")[0]
            assert end.endswith(" outside=0 outside_unknown=0 in_flight=1"), end
            late = _log_lines(caplog, f"rlm_execute: session={sid} call=")[-1]
            assert "outside=find_references_to_object" in late, "вклад вызова — в его собственной строке"
            assert box["res"]["signatures"], "запущенный код завершился штатно"
        finally:
            release.set()
            worker.join(30)

    def test_end_line_after_finished_execute_is_final(self, cf, caplog):
        """Пока execute не идёт, итог окончательный: метки нет, а поздний execute после
        rlm_end до кода не доходит и счётчиков не меняет."""
        res = _start(cf, domains=["код"])
        sid = res["session_id"]
        session = server.session_manager.get(sid)
        _exec(sid, "find_references_to_object('Документ.Док')")
        with caplog.at_level(logging.INFO, logger="rlm_tools_bsl.server"):
            server._rlm_end(sid)
        end = _log_lines(caplog, f"rlm_end: session={sid} calls=")[0]
        assert "in_flight" not in end and end.endswith(" outside=1 outside_unknown=0"), end
        assert not session.execution_lock._is_owned(), "rlm_end отпустил замок"
        assert "error" in _exec(sid, "print(1)")
        assert session.execute_calls == 1 and len(session.outside_helpers) == 1

    def test_help_domain_is_logged(self, caplog):
        with caplog.at_level(logging.INFO, logger="rlm_tools_bsl.server"):
            asyncio.run(server._rlm_help_tool(domain=["код"]))
        assert any(line.endswith(" domains=<код>") for line in _log_lines(caplog, "rlm_help: mode=domain"))


class TestHelpDomainAndTopicSignatures:
    """Задача 3: справка без состояния — по домену и подписи шагов темы."""

    @pytest.fixture
    def dispatch(self):
        def _call(**kwargs) -> dict:
            return json.loads(server._rlm_help_dispatch(**kwargs))

        return _call

    def test_menu_lists_domains(self, dispatch):
        res = dispatch()
        assert res["mode"] == "menu"
        assert list(res["result"]["available_domains"]) == _ALLOWED
        assert "domain=" in res["result"]["hint"]

    def test_single_domain_argument_is_its_own_mode(self, dispatch):
        res = dispatch(domain="код")
        assert res["mode"] == "domain" and res["warnings"] == []
        names = [s.split("(", 1)[0] for s in res["result"]["signatures"]]
        snap = build_helper_metadata_snapshot()
        assert names == [n for n in snap if n in domain_helper_names(["код"])]
        assert not set(names) & set(HELPER_CORE)
        assert res["result"]["topics"] == {"код": ["иерархия вызовов", "достижимость"]}
        assert res["result"]["conditional"] == {"git_search": "только под git"}

    def test_domain_wins_over_topic_with_warning(self, dispatch):
        res = dispatch(domain=["связи"], topic="проведение")
        assert res["mode"] == "domain"
        assert any("topic" in w for w in res["warnings"])

    def test_unknown_domain(self, dispatch):
        res = dispatch(domain=["кот"])
        assert res["result"]["error"] == "unknown" and res["result"]["allowed"] == _ALLOWED
        assert res["result"]["ignored"] == ["кот"]

    def test_help_does_not_touch_sessions(self, dispatch, cf):
        res = _start(cf, domains=[])
        sid = res["session_id"]
        try:
            session = _session(sid)
            delivered = set(session.delivered_helpers)
            dispatch(domain=["документ"])
            dispatch(topic="проведение", format="full")
            assert session.delivered_helpers == delivered and session.helper_domains == []
        finally:
            server._rlm_end(sid)

    def test_topic_compact_signatures(self, dispatch):
        res = dispatch(topic="проведение")
        names = [s.split("(", 1)[0] for s in res["result"]["signatures"]]
        # Порядок каталога; других хелперов шаги compact не упоминают.
        assert names == ["get_object_profile", "find_register_movements", "search_objects"]
        assert "conditional" not in res["result"]

    def test_topic_full_covers_its_extra_steps(self, dispatch):
        res = dispatch(topic="проведение", format="full")
        names = [s.split("(", 1)[0] for s in res["result"]["signatures"]]
        for name in (
            "find_register_writers",
            "find_event_subscriptions",
            "analyze_document_flow",
            "safe_grep",
            "git_search",
        ):
            assert name in names, name
        assert res["result"]["conditional"] == {"git_search": "только под git"}
        assert len(names) == len(set(names))
        snap = build_helper_metadata_snapshot()
        assert names == [n for n in snap if n in set(names)], "порядок каталога"

    def test_topic_code_hint_mentions_count_only_when_returned(self, dispatch):
        """compact-«интеграция» сама хелпер не называет, а её code_hint — называет:
        подпись приходит ровно тогда, когда code_hint возвращён."""
        with_code = dispatch(topic="интеграция", include_code=True)["result"]
        assert "code_hint" in with_code
        assert "find_exchange_plan_content" in [s.split("(", 1)[0] for s in with_code["signatures"]]
        without = dispatch(topic="интеграция", include_code=False)["result"]
        assert "code_hint" not in without
        assert "find_exchange_plan_content" not in [s.split("(", 1)[0] for s in without["signatures"]]


class TestEntryPointAndEnvFile:
    """Задача 11: режим стратегии и каталога из .env согласуются со списком тулов."""

    _PROBE = (
        "import asyncio, json, sys\n"
        "import rlm_tools_bsl.server as s\n"
        "import rlm_tools_bsl.cache as c, rlm_tools_bsl.bsl_index as bi\n"
        "c.cleanup_stale_cache = lambda: {'disabled': True}\n"
        "bi.migrate_legacy_index_root = lambda: 0\n"
        "def run(transport=None):\n"
        "    tools = sorted(t.name for t in asyncio.run(s.mcp.list_tools()))\n"
        "    print('RESULT ' + json.dumps({'tools': tools, 'strategy': s.get_strategy_mode(),\n"
        "        'catalog': s.get_catalog_mode(), 'attr': hasattr(s, 'rlm_help')}))\n"
        "s.mcp.run = run\n"
        "s._prepare_stdio_transport = lambda: (None, None)\n"
        "sys.argv = ['rlm-tools-bsl']\n"
        "s.main()\n"
    )

    def _run(self, tmp_path, env_file_lines, extra_env=None):
        env_path = tmp_path / "project.env"
        env_path.write_text("\n".join(env_file_lines) + "\n", encoding="utf-8")
        cfg = tmp_path / "service.json"
        cfg.write_text(json.dumps({"env_file": str(env_path)}), encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if k not in ("RLM_STRATEGY_MODE", "RLM_CATALOG_MODE")}
        env["RLM_CONFIG_FILE"] = str(cfg)
        env.update(extra_env or {})
        # Сервер пишет UTF-8. Без явной кодировки text=True декодирует кодировкой системы: на
        # Windows-раннере это cp1252, поток чтения stderr падает на кириллице и stderr = None.
        res = subprocess.run(
            [sys.executable, "-c", self._PROBE],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=180,
        )
        assert res.returncode == 0, res.stderr[-800:]
        line = next(ln for ln in res.stdout.splitlines() if ln.startswith("RESULT "))
        return json.loads(line[len("RESULT ") :]), res.stderr

    @pytest.mark.parametrize("mode", ["slim", "full"])
    def test_strategy_mode_only_from_env_file(self, tmp_path, mode):
        out, _ = self._run(tmp_path, [f"RLM_STRATEGY_MODE={mode}", "RLM_CATALOG_MODE=all"])
        assert out["strategy"] == mode and out["catalog"] == "all"
        assert ("rlm_help" in out["tools"]) is (mode == "slim")
        assert out["attr"] is (mode == "slim")

    def test_process_env_wins_over_env_file(self, tmp_path):
        out, _ = self._run(tmp_path, ["RLM_STRATEGY_MODE=slim"], {"RLM_STRATEGY_MODE": "full"})
        assert out["strategy"] == "full" and "rlm_help" not in out["tools"]

    def test_invalid_catalog_mode_warns_from_main(self, tmp_path):
        out, stderr = self._run(tmp_path, ["RLM_CATALOG_MODE=bogus"])
        assert out["catalog"] == "domains"
        assert "RLM_CATALOG_MODE='bogus'" in stderr
        assert "catalog_mode=domains" in stderr, "стартовая строка несёт режим каталога"

    def test_version_needs_no_running_service(self):
        res = subprocess.run(
            [sys.executable, "-m", "rlm_tools_bsl", "--version"], capture_output=True, text=True, timeout=120
        )
        assert res.returncode == 0 and res.stdout.strip()


# ═══════════════════ Задачи 5–7 — тексты старта ═══════════════════

from rlm_tools_bsl.bsl_knowledge import (  # noqa: E402
    _SLIM_HELP_BLOCK,
    build_domains_block,
    ext_list_display_cap,
    get_strategy,
    slim_recipe_step_helpers,
    slim_recipe_topic,
)

_IDX_STATS = {
    "methods": 1000,
    "calls": 500,
    "config_name": "X",
    "config_version": "1.0",
    "has_fts": True,
    "object_synonyms": 10,
    "builder_version": 16,
    "has_metadata": True,
    "object_attributes": 5,
    "predefined_items": 5,
    "file_paths": 100,
}


def _recipe_line(strategy: str) -> str | None:
    return next((ln for ln in strategy.splitlines() if ln.startswith("Остальные хелперы домена")), None)


class TestStrictTopicMatch:
    """Задача 5: slim-рецепт — только при совпадении темы с начала слова."""

    @pytest.mark.parametrize(
        "query,topic",
        [
            ("платформы 1С", None),
            ("разбор формы документа", "события формы"),
            ("предопределённые элементы", "тип реквизита"),
            ("как проводится документ", "проведение"),
            ("rest api сервиса", "интеграция"),
            ("http-сервисы", "интеграция"),
            ("платформы и перехваты", "расширения"),
            ("платформы, а потом формы", "события формы"),
            ("", None),
        ],
    )
    def test_strict_match(self, query, topic):
        assert slim_recipe_topic(query) == topic

    @pytest.mark.strategy_mode_slim
    @pytest.mark.parametrize("catalog", ["domains", "all"])
    def test_false_substring_brings_nothing_in_slim(self, cf, monkeypatch, catalog):
        monkeypatch.setenv("RLM_CATALOG_MODE", catalog)
        res = _start(cf, domains=[], query="платформы 1С")
        try:
            assert "== BUSINESS RECIPE" not in res["strategy"]
            assert _recipe_line(res["strategy"]) is None
            if catalog == "domains":
                assert _names(res) == _expected_names(res, set(HELPER_CORE))
        finally:
            server._rlm_end(res["session_id"])

    def test_full_keeps_the_old_substring_recipe(self, cf):
        res = _start(cf, query="платформы 1С")
        try:
            assert "== BUSINESS RECIPE: события формы ==" in res["strategy"]
        finally:
            server._rlm_end(res["session_id"])

    @pytest.mark.strategy_mode_slim
    def test_mixed_query_recipe_and_signatures_share_one_topic(self, cf):
        res = _start(cf, domains=[], query="платформы и перехваты")
        try:
            assert "== BUSINESS RECIPE: расширения ==" in res["strategy"]
            steps = slim_recipe_step_helpers("расширения", build_helper_metadata_snapshot())
            extra = [n for n in steps if n not in HELPER_CORE]
            assert extra and set(extra) <= set(_names(res))
            assert "parse_form" not in _names(res), "подписи чужой темы («события формы»)"
            assert _recipe_line(res["strategy"]).endswith("domains=['расширения'] к ближайшему rlm_execute.")
        finally:
            server._rlm_end(res["session_id"])


@pytest.mark.strategy_mode_slim
class TestRecipeStepSignatures:
    def test_undelivered_topic_domain_brings_step_signatures_and_line(self, cf):
        res = _start(cf, domains=["код"], query="как проводится документ")
        try:
            steps = slim_recipe_step_helpers("проведение", build_helper_metadata_snapshot())
            assert {"get_object_profile", "find_register_movements"} <= set(steps)
            wanted = set(HELPER_CORE) | domain_helper_names(["код"]) | set(steps)
            assert _names(res) == _expected_names(res, wanted)
            line = _recipe_line(res["strategy"])
            assert line and len(line) <= 150 and "domains=['документ']" in line
            session = _session(res["session_id"])
            assert {"get_object_profile", "find_register_movements"} <= session.delivered_helpers
        finally:
            server._rlm_end(res["session_id"])

    def test_delivered_topic_domain_adds_nothing(self, cf):
        res = _start(cf, domains=["документ"], query="как проводится документ")
        try:
            wanted = set(HELPER_CORE) | domain_helper_names(["документ"])
            assert _names(res) == _expected_names(res, wanted)
            assert _recipe_line(res["strategy"]) is None
        finally:
            server._rlm_end(res["session_id"])

    def test_catalog_all_has_no_line(self, cf, monkeypatch):
        monkeypatch.setenv("RLM_CATALOG_MODE", "all")
        res = _start(cf, query="как проводится документ")
        try:
            assert "== BUSINESS RECIPE: проведение ==" in res["strategy"]
            assert _recipe_line(res["strategy"]) is None
        finally:
            server._rlm_end(res["session_id"])

    def test_step_signatures_are_not_counted_as_outside(self, cf):
        res = _start(cf, domains=[], query="как проводится документ")
        sid = res["session_id"]
        try:
            r = _exec(sid, "get_object_profile('Док')")
            assert "signatures" not in r and _session(sid).outside_helpers == []
        finally:
            server._rlm_end(sid)


@pytest.mark.strategy_mode_slim
class TestSlimTexts:
    def test_help_block(self):
        assert len(_SLIM_HELP_BLOCK) <= 550
        assert "MUST" not in _SLIM_HELP_BLOCK and "BEFORE" not in _SLIM_HELP_BLOCK
        for needle in (
            "'coverage')",
            "rlm_help(helpers=['имя'])",
            "rlm_help(domain=",
            "готовый план по теме",
            "help('keyword')",
            "rlm_help()",
        ):
            assert needle in _SLIM_HELP_BLOCK, needle

    def test_no_mandatory_help_anywhere_in_slim_strategy(self):
        s = get_strategy("high", None, registry=build_helper_metadata_snapshot(), idx_stats=_IDX_STATS)
        assert "MUST call rlm_help" not in s and "BEFORE running rlm_execute" not in s

    def test_compact_index_is_gone(self):
        s = get_strategy("medium", None, registry=build_helper_metadata_snapshot())
        assert "== HELPERS (compact index" not in s
        assert "== ДОМЕНЫ ХЕЛПЕРОВ ==" in s

    def test_instant_line_is_gone_in_slim_only(self, monkeypatch):
        snap = build_helper_metadata_snapshot()
        slim = get_strategy("high", None, registry=snap, idx_stats=_IDX_STATS)
        # Убрана строка-перечень «INSTANT from index: …»; фраза в INDEX TIPS остаётся.
        assert "INSTANT from index: " not in slim
        for kept in ("Index v16", "get_index_info", "INDEX TIPS:"):
            assert kept in slim, kept
        monkeypatch.setenv("RLM_STRATEGY_MODE", "full")
        assert "INSTANT from index: " in get_strategy("high", None, registry=snap, idx_stats=_IDX_STATS)

    @pytest.mark.parametrize(
        "value,limit",
        [
            ([], 400),
            (["код"], 400),
            (["документ", "структура", "код", "связи", "расширения", "поиск"], 400),
            (["документ", "структура", "код", "связи", "расширения", "поиск", *[f"мусор{i}" for i in range(80)]], 400),
            (["кот", "пёс", "слон", "жираф"], 400),
            (["весь каталог"], 150),
            # Строка нераспознанного несёт перечень ключей и в 150 не входит — общий потолок.
            (["весь каталог", "кот"], 400),
            (["весь каталог", *[f"мусор{i}" for i in range(80)]], 400),
        ],
    )
    def test_domains_block_ceilings(self, value, limit):
        block = build_domains_block(normalize_domains(value), "domains")
        assert block.startswith("\n== ДОМЕНЫ ХЕЛПЕРОВ ==")
        assert len(block) <= limit, len(block)

    def test_domains_block_variants(self):
        core = build_domains_block(normalize_domains([]), "domains")
        assert "только ядро" in core and "документ | структура" in core and "rlm_execute" in core
        chosen = build_domains_block(normalize_domains(["код", "связи"]), "domains")
        assert "Загружены: код, связи" in chosen and "signatures" in chosen
        assert "иерархия вызовов" in chosen and "права" in chosen, "темы выбранных доменов"
        assert "Не распознано" not in chosen
        typo = build_domains_block(normalize_domains(["код", "кот"]), "domains")
        assert "Не распознано: кот" in typo and "весь каталог" in typo
        assert build_domains_block(normalize_domains(["код"]), "all") == ""

    def test_direct_call_without_choice_is_core_only_with_recipe_line(self):
        s = get_strategy("high", None, registry=build_helper_metadata_snapshot(), query="себестоимость")
        assert "== BUSINESS RECIPE: себестоимость ==" in s
        assert "Загружено только ядро" in s
        assert "domains=['документ']" in _recipe_line(s)


class TestExtensionListCap:
    """Задача 7: top-5 в slim только для поля и блока стратегии."""

    @staticmethod
    def _tree(tmp_path, n: int):
        cf = tmp_path / "src" / "cf"
        _write(cf / "Configuration.xml", CF_DESCRIPTOR)
        _write(cf / "Documents" / "Док" / "Ext" / "ObjectModule.bsl", "Процедура П() Экспорт\nКонецПроцедуры\n")
        for i in range(n):
            ext = tmp_path / "src" / "cfe" / f"Ext{i:02d}"
            _write(
                ext / "Configuration.xml",
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">\n'
                '  <Configuration uuid="00000000-0000-0000-0000-0000000000%02d">\n'
                "    <Properties><ObjectBelonging>Adopted</ObjectBelonging><Name>Расш%d</Name>"
                "<ConfigurationExtensionPurpose>Customization</ConfigurationExtensionPurpose>"
                "<NamePrefix>р%d_</NamePrefix></Properties>\n"
                "  </Configuration>\n"
                "</MetaDataObject>\n" % (i + 10, i, i),
            )
        return cf

    def test_display_cap_defaults_and_priority(self, monkeypatch):
        monkeypatch.setenv("RLM_STRATEGY_MODE", "slim")
        assert ext_list_display_cap() == 5
        monkeypatch.setenv("RLM_STRATEGY_MODE", "full")
        assert ext_list_display_cap() == 20
        for raw, cap in (("7", 7), ("0", 0), ("-1", -1)):
            monkeypatch.setenv("RLM_EXT_LIST_CAP", raw)
            for mode in ("slim", "full"):
                monkeypatch.setenv("RLM_STRATEGY_MODE", mode)
                assert ext_list_display_cap() == cap
        monkeypatch.setenv("RLM_EXT_LIST_CAP", "мусор")
        monkeypatch.setenv("RLM_STRATEGY_MODE", "slim")
        assert ext_list_display_cap() == 5

    @pytest.mark.parametrize("mode", ["slim", "full"])
    def test_invalid_cap_behaves_as_unset_for_every_display(self, monkeypatch, mode):
        """ENV_REFERENCE: невалидное значение — как не заданное. У поля и блока это
        дефолт режима, у detect_extensions() (и его warnings) — всегда 20."""
        from rlm_tools_bsl.extension_detector import _ext_list_cap

        monkeypatch.setenv("RLM_STRATEGY_MODE", mode)
        monkeypatch.delenv("RLM_EXT_LIST_CAP", raising=False)
        unset = (ext_list_display_cap(), _ext_list_cap())
        assert unset == ((5 if mode == "slim" else 20), 20)
        monkeypatch.setenv("RLM_EXT_LIST_CAP", "мусор")
        assert (ext_list_display_cap(), _ext_list_cap()) == unset

    @pytest.mark.strategy_mode_slim
    def test_slim_field_and_block_show_top5_with_route_to_full_list(self, tmp_path):
        cf = self._tree(tmp_path, 8)
        res = _start(cf, domains=[])
        try:
            ec = res["extension_context"]
            assert len(ec["nearby_extensions"]) == 5
            assert ec["nearby_extensions_truncated"] is True and ec["nearby_extensions_total"] == 8
            assert "8 EXTENSIONS DETECTED" in res["strategy"]
            assert "Показаны 5 из 8 расширений" in res["strategy"]
            assert "detect_extensions()" in res["strategy"]
            # Предупреждение не дублирует список из extension_context: сколько, где top-5
            # и маршрут к полному списку; имён в нём нет.
            assert res["warnings"] == [
                "8 extensions detected near main config — top 5 by overrides in "
                "extension_context.nearby_extensions; complete list — detect_extensions()."
            ]
        finally:
            server._rlm_end(res["session_id"])

    @pytest.mark.strategy_mode_slim
    def test_slim_full_list_has_no_hint(self, tmp_path):
        cf = self._tree(tmp_path, 3)
        res = _start(cf, domains=[])
        try:
            assert "nearby_extensions_truncated" not in res["extension_context"]
            assert "Показаны" not in res["strategy"] and "detect_extensions()" not in res["strategy"]
        finally:
            server._rlm_end(res["session_id"])

    def test_full_keeps_20_and_no_hint(self, tmp_path):
        cf = self._tree(tmp_path, 8)
        res = _start(cf)
        try:
            assert len(res["extension_context"]["nearby_extensions"]) == 8
            assert "nearby_extensions_truncated" not in res["extension_context"]
            assert "Показаны" not in res["strategy"]
        finally:
            server._rlm_end(res["session_id"])

    @pytest.mark.strategy_mode_slim
    def test_explicit_cap_wins_in_slim(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RLM_EXT_LIST_CAP", "20")
        cf = self._tree(tmp_path, 8)
        res = _start(cf, domains=[])
        try:
            assert len(res["extension_context"]["nearby_extensions"]) == 8
        finally:
            server._rlm_end(res["session_id"])

    def test_detect_extensions_warnings_do_not_depend_on_strategy_mode(self, tmp_path, monkeypatch):
        cf = self._tree(tmp_path, 8)
        outs = {}
        for mode in ("slim", "full"):
            monkeypatch.setenv("RLM_STRATEGY_MODE", mode)
            res = _start(cf, domains=[]) if mode == "slim" else _start(cf)
            try:
                r = _exec(res["session_id"], "d = detect_extensions()\nprint(d['warnings'])")
                assert r["error"] is None, r["error"]
                outs[mode] = r["stdout"]
            finally:
                server._rlm_end(res["session_id"])
        assert outs["slim"] == outs["full"] and "Расш7" in outs["slim"]


# ═══════════════════ Тексты схем и документов релиза ═══════════════════


def _tool_schema(name: str) -> dict:
    return next(t for t in asyncio.run(server.mcp.list_tools()) if t.name == name).inputSchema


class TestToolFieldTexts:
    """Правило задачи 9: описание поля оставляет только поведение, которого не видно в
    схеме. Сокращение не имеет права съесть то, что схема не выражает."""

    def test_effort_description_names_every_tier(self):
        """`effort` — голый str (Literal в схеме нет): без перечня агент не узнаёт про
        low/max, а неверный уровень молча превращается в medium."""
        desc = _tool_schema("rlm_start")["properties"]["effort"]["description"]
        for tier in ("auto", "low", "medium", "high", "max"):
            assert tier in desc, tier

    def test_include_metadata_description_says_what_it_adds(self):
        """В контексте 1С «metadata» читается как объекты метаданных — описание обязано
        назвать, что флаг добавляет на самом деле (счётчики файлов)."""
        desc = _tool_schema("rlm_start")["properties"]["include_metadata"]["description"]
        assert "файл" in desc and "медленно" in desc.lower()


_RELEASE_DOCS = (
    "docs/ENV_REFERENCE.md",
    "README.md",
    "docs/HELPERS.md",
    "docs/MODULE_MAP.md",
    "docs/ARCHITECTURE.md",
    "docs/AGENT_INSTRUCTIONS.md",
    "docs/FAQ.md",
    "docs/PROJECT_REGISTRY.md",
    "docs/QUICKSTART.md",
    "docs/full_analysis_prompt.md",
    "CHANGELOG.md",
)


def _doc(rel: str) -> str:
    import pathlib

    return (pathlib.Path(__file__).resolve().parents[1] / rel).read_text(encoding="utf-8")


class TestReleaseDocs:
    def test_no_paragraph_turns_into_a_setext_heading(self):
        """Строка текста вплотную над `---` — это заголовок H2 в GFM: абзац рендерится
        огромным заголовком, а разделитель исчезает."""
        bad = []
        for rel in _RELEASE_DOCS:
            lines = _doc(rel).split("\n")
            for i, line in enumerate(lines):
                prev = lines[i - 1].strip() if i else ""
                if line.strip() == "---" and prev and not prev.startswith(("|", "---")):
                    bad.append(f"{rel}:{i + 1}")
        assert not bad, bad

    def test_module_map_describes_the_current_rlm_help_registration(self):
        text = _doc("docs/MODULE_MAP.md")
        assert 'if get_strategy_mode() == "slim":' not in text, "условного @mcp.tool() больше нет"
        assert "6 режимов по приоритету" not in text, "режимов справки теперь семь"
        assert "_sync_rlm_help_registration" in text

    def test_release_texts_do_not_overclaim(self):
        assert "в generic-сессии выдаётся весь каталог" not in _doc("docs/HELPERS.md"), (
            "в generic-сессии BSL-хелперов нет — выдаются только файловые"
        )
        assert "всех 56 хелперов" not in _doc("CHANGELOG.md"), "без git в реестре 55, с git_search — 56"


# ═══════════════════ Стражи молчаливого нуля: вызов хелпера «вслепую» ═══════════════════
#
# Вне выданных доменов агент зовёт хелпер по имени, не видя подписи. Первый позиционный
# аргумент find_attributes/find_predefined — имя ЭЛЕМЕНТА, find_ext_overrides — путь
# КАТАЛОГА расширения; имя объекта туда давало пустой ответ без ошибки (замер этапа 1 e2e).

from test_v1_40_0 import _bsl_for, _build_reader, _task2_tree  # noqa: E402


@pytest.fixture(params=[False, True], ids=["live", "index"])
def homonyms(request, tmp_path, monkeypatch):
    import types

    root = _task2_tree(tmp_path / "cfg")
    reader = _build_reader(root, monkeypatch) if request.param else None
    yield types.SimpleNamespace(bsl=_bsl_for(root, reader), indexed=request.param)
    if reader is not None:
        reader.close()


def _refused(call) -> str | None:
    """Текст отказа или None, если хелпер ответил без исключения."""
    try:
        call()
    except ValueError as exc:
        return str(exc)
    return None


class TestSilentZeroGuards:
    @pytest.mark.parametrize("name", ["Документ.Альфа", "Documents/Альфа", "Документ.ТолькоДок"])
    def test_object_ref_as_attribute_name_is_refused_only_when_the_index_answered(self, homonyms, name):
        """Ссылка или путь СУЩЕСТВУЮЩЕГО объекта. С индексом — отказ с маршрутом. Без
        индекса поиск по основной конфигурации невозможен, а name ищется и в синониме:
        форма строки диагноза не даёт — пустой ответ по контракту."""
        msg = _refused(lambda: homonyms.bsl["find_attributes"](name))
        if homonyms.indexed:
            assert msg and f"find_attributes(object_name={name!r})" in msg and "имя ЭЛЕМЕНТА" in msg, msg
        else:
            assert msg is None and homonyms.bsl["find_attributes"](name) == []

    @pytest.mark.parametrize("name", ["Альфа", "ТолькоДок"])
    def test_bare_object_name_is_refused_only_when_the_index_answered(self, homonyms, name):
        """Без индекса поиск по одному name пуст по контракту: одноимённый объект не
        доказывает перепутанный аргумент — законный поиск элемента «Альфа» не должен
        получить ложный диагноз."""
        msg = _refused(lambda: homonyms.bsl["find_attributes"](name))
        if homonyms.indexed:
            assert msg and f"find_attributes(object_name={name!r})" in msg, msg
        else:
            assert msg is None and homonyms.bsl["find_attributes"](name) == []

    def test_the_route_from_the_refusal_works(self, homonyms):
        assert {r["attr_name"] for r in homonyms.bsl["find_attributes"](object_name="ТолькоДок")} == {"Т1"}

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"name": "НетТакогоОбъекта"},  # не объект — честный ноль
            {"name": "Документ.НетТакого"},  # ссылка на несуществующий объект — честный ноль
            {"name": "Документ.Альфа", "kind": "dimension"},  # фильтр kind — поиск по имени элемента
            {"name": "Документ.Альфа", "limit": 0},  # пустая страница по запросу
            {"name": "Документ.Альфа", "limit": 0.5},  # нормализуется в 0 — та же пустая страница
            {"name": "Документ.Альфа", "category": "Catalogs"},  # в этой категории объекта нет
            {"name": "Альфа", "object_name": "ТолькоДок"},  # объект задан — ответ честный
        ],
    )
    def test_honest_empty_answers_stay_empty(self, homonyms, kwargs):
        assert homonyms.bsl["find_attributes"](**kwargs) == []

    def test_limit_none_is_judged_after_normalization(self, homonyms):
        """limit=None нормализуется в 500 — ошибочный вызов не должен уйти в тихий ноль."""
        attrs = _refused(lambda: homonyms.bsl["find_attributes"]("Документ.Альфа", limit=None))
        preds = _refused(lambda: homonyms.bsl["find_predefined"]("Справочник.Бета", limit=None))
        assert (attrs is not None, preds is not None) == (homonyms.indexed, homonyms.indexed)

    def test_category_narrows_the_refusal(self, homonyms):
        msg = _refused(lambda: homonyms.bsl["find_attributes"]("Альфа", category="AccumulationRegisters"))
        if homonyms.indexed:
            assert msg and "AccumulationRegisters" in msg and "Documents" not in msg, msg
        else:
            assert msg is None

    def test_predefined_object_name_as_item_name_is_refused(self, homonyms):
        msg = _refused(lambda: homonyms.bsl["find_predefined"]("Справочник.Бета"))
        if homonyms.indexed:
            assert msg and "find_predefined(object_name='Справочник.Бета')" in msg, msg
        else:
            assert msg is None
        assert {r["item_name"] for r in homonyms.bsl["find_predefined"](object_name="Справочник.Бета")} == {"П1"}
        assert homonyms.bsl["find_predefined"]("НетТакогоЭлемента") == []
        bare = _refused(lambda: homonyms.bsl["find_predefined"]("Бета"))
        assert (bare is not None) is homonyms.indexed, bare

    @pytest.mark.parametrize("query", ["ТолькоДок", "Альфа", "Бета", "Документ.Альфа"])
    def test_search_keeps_its_list_contract(self, homonyms, query):
        """search() ищет по имени объекта и берёт реквизиты/предопределённые без стража:
        пустой набор элементов с таким именем там законен."""
        res = homonyms.bsl["search"](query)
        assert isinstance(res, list)

    def test_extension_homonym_in_another_category_is_not_lost(self, tmp_path, monkeypatch):
        """Каскад основной конфигурации находит Catalogs/MainCat, а Documents/MainCat —
        XML-only объект расширения: фильтр по категории ссылки не должен его терять."""
        from test_helpers_extension import _make_main_with_extension, _write

        from rlm_tools_bsl.bsl_helpers import make_bsl_helpers
        from rlm_tools_bsl.bsl_index import IndexBuilder, IndexReader
        from rlm_tools_bsl.format_detector import detect_format
        from rlm_tools_bsl.helpers import make_helpers

        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
        cf, cfe = _make_main_with_extension(str(tmp_path))
        _write(
            str(pathlib.Path(cfe) / "Documents" / "MainCat.xml"),
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses" xmlns:v8="http://v8.1c.ru/8.1/data/core">'
            "<Document><Properties><Name>MainCat</Name></Properties><ChildObjects><Attribute><Properties>"
            "<Name>ДокРекв</Name><Type><v8:Type>xs:string</v8:Type></Type></Properties></Attribute>"
            "</ChildObjects></Document></MetaDataObject>\n",
        )
        reader = IndexReader(IndexBuilder().build(cf, build_calls=False, build_metadata=True, build_fts=True))
        try:
            generic, resolve_safe = make_helpers(cf, idx_reader=reader)
            bsl = make_bsl_helpers(
                base_path=cf,
                resolve_safe=resolve_safe,
                read_file_fn=generic["read_file"],
                grep_fn=generic["grep"],
                glob_files_fn=generic["glob_files"],
                format_info=detect_format(cf),
                idx_reader=reader,
                extension_paths=[cfe],
            )
            msg = _refused(lambda: bsl["find_attributes"]("Документ.MainCat"))
            assert msg and "Documents" in msg and "find_attributes(object_name='Документ.MainCat')" in msg, msg
            assert {r["attr_name"] for r in bsl["find_attributes"](object_name="Документ.MainCat")} == {"ДокРекв"}
        finally:
            reader.close()

    @pytest.mark.parametrize("bad", ["ТолькоДок", "../cfe/НетТакого"])
    def test_ext_overrides_missing_root_is_refused(self, tmp_path, bad):
        bsl = _bsl_for(_task2_tree(tmp_path / "cfg"), None)
        with pytest.raises(ValueError) as exc:
            bsl["find_ext_overrides"](bad)
        msg = str(exc.value)
        assert "detect_extensions()['nearby_extensions'][i]['path']" in msg and "get_overrides(" in msg, msg

    def test_ext_overrides_existing_root_without_modules_is_an_honest_zero(self, tmp_path):
        empty_ext = tmp_path / "ext"
        empty_ext.mkdir()
        bsl = _bsl_for(_task2_tree(tmp_path / "cfg"), None)
        res = bsl["find_ext_overrides"](str(empty_ext))
        assert res["total"] == 0 and res["partial"] is False


# ═══════════════════ Что видит агент: оформление slim, тексты тулов ═══════════════════

from test_ext_list_cap import _make_main_with_n_extensions  # noqa: E402


class TestAgentFacingLayout:
    @pytest.mark.strategy_mode_slim
    def test_every_slim_section_header_follows_a_blank_line(self, tmp_path):
        cf = _make_main_with_n_extensions(str(tmp_path), 3)
        res = _start(cf, query="как проводится документ", domains=[])
        try:
            lines = res["strategy"].split("\n")
            assert lines[0] == "== CRITICAL — 3 EXTENSIONS DETECTED ==", lines[:2]
            headers = [i for i, ln in enumerate(lines) if ln.startswith("== ")]
            assert len(headers) >= 8, headers
            assert all(lines[i - 1] == "" for i in headers[1:]), [lines[i] for i in headers if lines[i - 1]]
            intro = lines.index("You are exploring a 1C BSL codebase via Python sandbox.")
            assert lines[intro - 1] == "", "блок расширений отделён от вступления пустой строкой"
        finally:
            server._rlm_end(res["session_id"])

    def test_full_extension_block_keeps_its_old_form(self, tmp_path):
        cf = _make_main_with_n_extensions(str(tmp_path), 3)
        res = _start(cf)
        try:
            assert res["strategy"].startswith(
                "\nCRITICAL — 3 EXTENSIONS DETECTED (name/prefix/overrides_count в extension_context.nearby_extensions)\n"
            )
        finally:
            server._rlm_end(res["session_id"])

    def test_extension_session_keeps_its_warnings(self, tmp_path):
        _make_main_with_n_extensions(str(tmp_path), 2)
        res = _start(tmp_path / "src" / "cfe" / "Ext000")
        try:
            assert any("This is an EXTENSION" in w for w in res["warnings"]), res["warnings"]
        finally:
            server._rlm_end(res["session_id"])

    def test_compact_recipe_steps_stay_readable(self):
        """Compact-рецепт приходит в старт: шаг — план действий, а не справка. Детали —
        в full-версии рецепта (rlm_help(topic=…, format='full'))."""
        long_steps = [
            (topic, i, len(step))
            for topic, recipe in _BUSINESS_RECIPES.items()
            for i, step in enumerate(recipe.get("compact") or [], 1)
            if len(step) > 400
        ]
        assert not long_steps, long_steps

    def test_tool_texts_are_english(self):
        """Описания тулов и инструкция сервера — по-английски (параметры и рецепты —
        по-русски, ключи доменов русские по замыслу)."""
        cyrillic = re.compile(r"[А-Яа-яЁё]")
        texts = {t.name: t.description for t in asyncio.run(server.mcp.list_tools())}
        texts["rlm_help"] = server._rlm_help_tool.__doc__
        texts["instructions"] = server.mcp.instructions
        assert set(texts) >= {"rlm_start", "rlm_execute", "rlm_end", "rlm_help", "rlm_projects", "rlm_index"}
        assert not {name for name, text in texts.items() if cyrillic.search(text or "")}
