"""v1.23.0 — start-cost budget guard for rlm_start.

The strategy / recipe / tool-description edits in this release are formulation
REPLACEMENTS (not bulk additions); the new get_object_profile signature + the
extended rlm_start.index fields are the only intended growth. This test pins the
whole-strategy payload (slim AND full, with/without a business recipe) to the
v1.23.0 baselines and fails if a future edit grows any case by more than ~5%.

Baselines are deterministic: the strategy is built from the FROZEN helper-metadata
snapshot (build_helper_metadata_snapshot force-registers git_search), so the numbers
do not depend on git availability or the live registry.

Это верно для ячеек СТРАТЕГИИ. Ячейки whole-payload идут через настоящий
`_rlm_start`, то есть через ЖИВОЙ реестр, и git-возможность там регистрируется по
дереву: их вариант закреплён барьером `_pin_git_discovery_to_the_tree`.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile

import pytest

from rlm_tools_bsl.bsl_helpers import build_helper_metadata_snapshot
from rlm_tools_bsl.bsl_knowledge import get_strategy
from rlm_tools_bsl.format_detector import detect_format

# Baselines (chars), measured with the frozen snapshot registry.
#
# v1.28.0 re-baseline (INTENTIONAL — per this test's own guidance). The v1.23.0 numbers had
# been eaten to 99.2–99.7% of ceiling by v1.28.0 release A, i.e. the +5% guard was already
# exhausted BEFORE this change and could no longer absorb any new contract text. The growth
# here is the agent-facing contract of the v1.28.0 fixes — without it the fixes are invisible
# to the agent (aggregate keys nobody reads == the bug we just fixed):
#   * find_event_subscriptions  → scope=exact|partial|universal, category-aware 'Документ.X'
#   * get_overrides             → by_annotation / by_object_top / by_extension_top / unique_*
#   * find_register_movements   → posting_handler_present + hint
#   * find_functional_options   → limit= (per-bucket cap)
# The prose was trimmed first (helper `sig` strings shrank 388/319/568 → 328/256/320 chars, and
# the long explanations moved from the BUDGETED `sig` into the unbudgeted `recipe`, which
# rlm_help serves on demand). What remains is irreducible without deleting the key names.
#
# NB the DEFAULT start path did not grow at all: slim/"" is byte-for-byte what release A
# emitted (7146) — Step 4/5 + performance strategy lines live in sections that slim serves via
# rlm_help, not inline. Growth is confined to full mode and to a query-matched recipe, i.e. it
# is paid only when the agent actually asked about that topic.
# Re-baselining to the measured values restores a real +5% margin for the next edit.
#
# v1.30.0 re-baseline of the FULL-mode numbers only (INTENTIONAL, same reasoning as v1.28.0).
# The v1.28.0 baselines were already at 99.1% (full/"") and 99.7% (full/"проведение") of their
# ceilings on the untouched v1.29.1 tree, i.e. the guard had ~270 and ~110 chars of headroom
# left before this release started. The +210 chars added here are the agent-facing contract of
# the v1.30.0 fixes — text the agent must see BEFORE the call, or the fix is invisible:
#   * safe_grep            → срез max_files действует ВСЕГДА (hint меняет лишь ЧТО режется),
#                            поэтому пустой результат не доказывает отсутствие
#   * search_regions /     → count_only считает в ТОМ ЖЕ scope, что и выдача
#     search_module_headers  (+ total_main/total_extensions при настроенных расширениях)
#   * get_overrides        → by_*_top — это dict{имя:N}; target_method_line=None валиден
# The prose was compacted first (the long explanations live in the UNBUDGETED `recipe`, which
# rlm_help serves on demand; the get_overrides sig got shorter, not longer). What remains is
# irreducible without deleting the contract itself.
#
# slim is NOT re-baselined: both slim cases are byte-for-byte what v1.29.1 emitted (the new
# text lands in sections slim serves via rlm_help, not inline), so the default start path did
# not grow at all — the cost is paid only in full mode.
#
# ВНИМАНИЕ (v1.34.0): фраза «byte-for-byte» выше относится к v1.28.0/v1.30.0 и с этим
# релизом БОЛЬШЕ НЕ ВЕРНА. Slim вырос: "" 7146 → ~7391, "проведение" 7990 → ~8330.
# Бэйслайны оставлены прежними СОЗНАТЕЛЬНО — они бэйслайн, а не потолок: потолок равен
# int(baseline * _DRIFT), то есть 7503 и 8389, и обе ячейки под ним. Коэффициент _DRIFT
# не ослаблялся. Заявлять «не дорожает» о slim нельзя — верное утверждение: slim удержан
# под ПРЕЖНИМ потолком, а ре-бэйслайн (то есть выдача нового запаса +5%) получил только
# full. Запас в "проведение" при этом съеден на ~85%, и следующая правка slim-текста
# упрётся в гард — это и есть намеренное поведение.
#
# v1.34.0 ре-бэйслайн ТОЛЬКО full-режима (ОСОЗНАННО, тем же правилом, что v1.28.0 и
# v1.30.0). Прецедент в этом же файле: «the cost is paid only in full mode».
#
# ПОЧЕМУ full, а не slim. `sig` лежит в payload ДВАЖДЫ: в `available_functions` И в
# таблице хелперов full-стратегии, поэтому прирост подписей входит в full с
# коэффициентом 2. Slim таблицу хелперов с подписями не инлайнит, и он ОСТАВЛЕН ПОД
# ПРЕЖНИМ ПОТОЛКОМ: суммарный прирост подписей ужат до величины, укладывающейся в
# прежний slim-ceiling. Не «не дорожает» — дорожает, но в пределах уже объявленного
# запаса, без выдачи нового.
#
# ЧТО добавлено (agent-facing контракт релиза «честная форма ответа» — без него фиксы
# для агента невидимы, а это ровно тот дефект, который релиз и чинит):
#   * git_search / safe_grep → СЛОВАРНАЯ форма ответа (results/returned/truncated/error)
#     и машинные оси охвата (scanned_files/candidates_total/failed_files/
#     read_status_complete/catalog_complete);
#   * find_definition        → работает БЕЗ индекса, домешивает nearby CFE, hint по
#     объекту ТОЧНЫЙ, partial/total_exact;
#   * search_objects / find_by_type → count_only (+ у find_by_type алиас `category`,
#     который agent-facing sig обещал, а функция отвергала TypeError);
#   * find_module            → limit (сигнал усечения стал исполнимым);
#   * get_overrides          → пагинация offset/returned/has_more;
#   * owner                  → провенанс строки в 8 списочных выдачах;
#   * find_references_to_object / find_code_usages → оси охвата + index_coverage;
#   * DISAMBIGUATION         → 12-я пара + дописанная пре-существующая пропущенная.
#
# Проза ужималась ПЕРВОЙ: длинные пояснения переехали в НЕбюджетируемый `recipe`
# (rlm_help отдаёт его по запросу), из подписей убраны дубли и marketing-фразы.
# Оставшееся неустранимо без удаления самих имён ключей.
#
# v1.37.0: ВОСЕМЬ новых full-ячеек — по одной на каждый домен «правим», кроме уже
# имевшегося «проведение». Это ЕДИНСТВЕННОЕ место, которое видит `full`-форму
# доменного рецепта: payload-ячейки идут по `auto`→`medium`, то есть инлайнят
# `compact`, а `get_strategy("high", …)` — `full`. Без этих ячеек правки
# `full`-формы девяти доменов не мерило бы НИЧТО. Числа сняты ДО первой правки
# релиза на нетронутом дереве (`29e5e0d`).
_BASELINES = {
    ("slim", ""): 7146,
    ("slim", "проведение"): 7990,
    ("full", ""): 33858,
    ("full", "проведение"): 35597,
    ("full", "права"): 35667,
    ("full", "расширения"): 37184,
    ("full", "структура объекта"): 35469,
    ("full", "события формы"): 35165,
    ("full", "ссылки"): 36677,
    ("full", "ввод на основании"): 35004,
    ("full", "иерархия вызовов"): 37180,
    ("full", "достижимость"): 35645,
}
# Whole rlm_start payload baselines (strategy + available_functions + index +
# extension_context) for a fixed minimal INDEXED config — the plan's real target.
# v1.26.0 re-baseline (intentional growth, per this test's own guidance): the new
# index.index_status machine-contract key (+~22 chars) and the find_files "instant on
# index-hit, else FS-fallback" hint update. Restores the +5% margin (the v1.23.0
# baseline sat at ~99% of ceiling, so the documented order-dependent extension-leak
# flakiness could tip it once the margin shrank).
# v1.28.0 re-baseline, same reasoning as _BASELINES above (see there). This payload also
# carries available_functions, i.e. every helper `sig` — after the sig trim it was back under
# the old ceiling on its own, but at 98–99% of it; re-baselining restores the +5% margin so the
# next edit trips the guard on its own merits rather than on inherited saturation.
# v1.34.0: slim НЕ ре-бэйслайнится (см. пояснение к _BASELINES) — прежний потолок
# 21725 держится. full двигается на измеренную величину.
_PAYLOAD_BASELINES = {"slim": 20691, "full": 48037}
# Domain-matched whole-payload бэйслайны (v1.34.0). Заполняются измерением ниже —
# см. test_domain_matched_rlm_start_payload_within_budget. «проведение» осознанно
# фиксируется отдельно: там потолок +5% был превышен ещё ДО релиза.
_DOMAIN_PAYLOAD_BASELINES: dict[tuple[str, str], int] = {
    # «права» — рамка Задачи 8 (доменный рецепт инлайнится в стратегию и уезжает в
    # payload). Ре-бэйслайн осознанный, по ИЗМЕРЕННОЙ serialized delta.
    ("slim", "права"): 22308,
    ("full", "права"): 48546,
    # «проведение» фиксируется ОТДЕЛЬНО и осознанно: на этом маршруте объявленный
    # +5% был превышен ещё ДО v1.34.0 (пре-существующее состояние вне изменяемого
    # пути — Задачи 1/2 этот рецепт СОКРАЩАЮТ). Маскировать его общим ре-бэйслайном
    # ячеек «права» нельзя.
    ("slim", "проведение"): 22631,
    ("full", "проведение"): 48864,
    # v1.37.0: остальные СЕМЬ доменов, чьи рецепты правит релиз, бюджетом не мерил
    # НИКТО — они росли бы вне любого гарда. Числа сняты ДО первой правки релиза на
    # нетронутом дереве (`29e5e0d`), поэтому «бэйслайн» не вобрал в себя уже
    # сделанный рост.
    ("slim", "расширения"): 22363,
    ("full", "расширения"): 50143,
    ("slim", "структура объекта"): 22004,
    ("full", "структура объекта"): 49328,
    ("slim", "события формы"): 21988,
    ("full", "события формы"): 48825,
    ("slim", "ссылки"): 22142,
    ("full", "ссылки"): 49993,
    ("slim", "ввод на основании"): 21927,
    ("full", "ввод на основании"): 48760,
    ("slim", "иерархия вызовов"): 22664,
    ("full", "иерархия вызовов"): 50321,
    ("slim", "достижимость"): 22217,
    ("full", "достижимость"): 49055,
}

# Девять доменов `_BUSINESS_RECIPES`, чьи рецепты правит v1.37.0.
_BUDGET_DOMAINS = (
    "проведение",
    "права",
    "расширения",
    "структура объекта",
    "события формы",
    "ссылки",
    "ввод на основании",
    "иерархия вызовов",
    "достижимость",
)

# v1.37.0: у доменных ячеек effort пинится ЯВНО. Через `auto` он зависит от ТЕКСТА
# запроса (`_auto_effort`), и «иерархия вызовов» — единственный домен с маркером
# сложности («иерарх») — ушла бы в `high`, то есть инлайнила бы `full`-форму
# рецепта, пока остальные восемь инлайнят `compact`. Одна и та же прибавка в
# «+120» означала бы тогда у разных доменов разное.
_DOMAIN_EFFORT = "medium"

_DRIFT = 1.05  # allow ≤5% growth before failing

# v1.32.0: бюджет меряется на ПОДДЕРЖИВАЕМОМ дереве. С гейтом чужих форматов
# заглушка `<Configuration/>` дала бы source_support=foreign_with_bsl и лишний
# блок предупреждения в стратегии — то есть бюджет считался бы не для того
# сценария, который защищает (тест ниже это ещё и ассертит).
_CF_DESCRIPTOR = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">\n'
    '  <Configuration uuid="00000000-0000-0000-0000-000000000001">\n'
    "    <Properties><Name>Тест</Name></Properties>\n"
    "  </Configuration>\n"
    "</MetaDataObject>\n"
)

_IDX_STATS = {
    "methods": 1000,
    "calls": 500,
    "config_name": "X",
    "config_version": "1.0",
    "has_fts": True,
    "object_synonyms": 10,
    "builder_version": "14",
    "has_metadata": True,
}


@pytest.fixture(scope="module")
def _fmt_info():
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "Configuration.xml"), "w") as f:
            f.write("<Configuration/>")
        yield detect_format(d)


@pytest.mark.parametrize("mode,query", list(_BASELINES))
def test_strategy_payload_within_budget(_fmt_info, monkeypatch, mode, query):
    monkeypatch.setenv("RLM_STRATEGY_MODE", mode)
    snap = build_helper_metadata_snapshot()
    text = get_strategy("high", _fmt_info, registry=snap, idx_stats=_IDX_STATS, query=query)
    baseline = _BASELINES[(mode, query)]
    ceiling = int(baseline * _DRIFT)
    assert len(text) <= ceiling, (
        f"{mode}/{query or '(none)'} strategy grew to {len(text)} chars "
        f"(> {ceiling} = baseline {baseline} +5%). Trim, or re-baseline intentionally."
    )


def test_get_object_profile_signature_stays_compact():
    """The new sig appears in available_functions + the strategy helpers table — keep it lean."""
    snap = build_helper_metadata_snapshot()
    sig = snap["get_object_profile"]["sig"]
    # v1.37.0: 525 -> 340, то же правило `ceil10(факт * 1.10)` и только вниз
    # (факт после Задачи 0 — 308; прежний потолок оставлял 217 свободных символов).
    # v1.38.0: 340 -> 310 (то же правило , только вниз).
    assert len(sig) <= 310, f"get_object_profile sig is {len(sig)} chars — trim to stay in budget"


def test_helper_snapshot_count_locked():
    """Adding/removing a registered helper is an intentional change — update this number."""
    assert len(build_helper_metadata_snapshot()) == 56


@pytest.mark.parametrize("mode", ["slim", "full"])
def test_full_rlm_start_payload_within_budget(monkeypatch, tmp_path, mode):
    """The WHOLE rlm_start payload (strategy + available_functions + index + extension_context),
    not just the strategy, stays within +5% of the v1.23.0 baseline — so a future edit cannot
    silently balloon available_functions or the index block (R7 #4/#5)."""
    _assert_payload_budget(monkeypatch, tmp_path, mode, query="", baselines=_PAYLOAD_BASELINES)


@pytest.mark.parametrize("mode,query", sorted(_DOMAIN_PAYLOAD_BASELINES))
def test_domain_matched_rlm_start_payload_within_budget(monkeypatch, tmp_path, mode, query):
    """v1.34.0: whole-payload мерился ТОЛЬКО на ``query=""``, поэтому инлайн доменного
    рецепта в payload не видел ни один тест — а бьющая рамка Задачи 8 именно там.

    Строки «проведение» получают СВОЙ осознанный бэйслайн: на них потолок +5% был
    превышен ещё ДО этого релиза (пре-существующее состояние вне изменяемого пути),
    и маскировать это общим ре-бэйслайном ячеек «права» нельзя."""
    _assert_payload_budget(monkeypatch, tmp_path, mode, query=query, baselines=None)


def _assert_payload_budget(monkeypatch, tmp_path, mode, query, baselines):
    if baselines is None:
        baseline = _DOMAIN_PAYLOAD_BASELINES[(mode, query)]
        effort = _DOMAIN_EFFORT
    else:
        baseline = baselines[mode]
        effort = "auto"
    _run_payload_budget(monkeypatch, tmp_path, mode, query, baseline, effort=effort)


# v1.36.0 (поправка V1): колонка «Факт» опорного замера зависела от ДЛИНЫ tmp-пути.
# `rlm_start` кладёт `resolved_path` в payload РОВНО ОДИН раз, поэтому длина
# `--basetemp` уезжала в измеряемую величину: с глубоким basetemp slim-ячейка падала
# (21729 > 21725) на НЕТРОННУТОМ дереве, без единой правки кода. Меряем path-free:
# фактическую escaped-длину пути заменяем на опорную константу.
#
# `_PATH_REF` не выбран, а ВЫЧИСЛЕН: он воспроизводит колонку «Факт» опорного замера
# плана до символа во всех шести снятых там ячейках. Потолки и `_PAYLOAD_BASELINES`
# при этом НЕ меняются — на опорном пути число то же, что и прежде, это не
# ре-бэйслайн, а снятие зависимости гарда от окружения.
_PATH_REF = 86


def _esc(value: str) -> str:
    """Значение так, как оно лежит ВНУТРИ JSON-строки payload (без кавычек)."""
    return json.dumps(value, ensure_ascii=False)[1:-1]


def _pathfree_len(raw: str, resolved_path: str) -> int:
    """Длина payload, нормализованная по длине `resolved_path`."""
    return len(raw) - len(_esc(resolved_path)) + _PATH_REF


# v1.37.0: ячейка обязана мерить ИМЕННО свой git-вариант, а не вариант окружения.
#
# `git_search` регистрируется, когда у `base_path` есть git-ПРЕДОК:
# `bsl_helpers._git_search_available` обходит `parents` в поиске `.git` и
# подтверждает находку вызовом `_git_available`. pytest кладёт `tmp_path` под
# `--basetemp`, поэтому basetemp внутри любого git-репозитория (скажем, внутри
# самого checkout'а) молча превращал non-git ячейку в git-ячейку: +413 символов
# `git_search.sig` в `available_functions` и +716 символов git-блока стратегии.
# Замер уезжал с 21 162 на 22 291 — то есть РОВНО в бэйслайн git-ячейки
# (22 814, потолок 23 954), а «base slim» краснел на НЕТРОНУТОМ коде. Гард
# ловил не рост payload, а место, куда указывал `--basetemp`.
#
# Барьер — штатный `GIT_CEILING_DIRECTORIES`: git не поднимается В перечисленные
# каталоги. Ставится на РОДИТЕЛЯ дерева, а не на само дерево: текущий каталог git
# из поиска не исключает, и потолок на самом дереве не помогает (проверено).
# Ячейке `test_git_backed_rlm_start_payload_within_budget` барьер безвреден: там
# `.git` лежит В дереве и находится без подъёма — и это не предположение, а её
# собственный ассерт `require_git_search=True`.
#
# Это снятие зависимости гарда от окружения, как `_pathfree_len` (длина пути) и
# `_clean_ctx` (соседние расширения), а НЕ ре-бэйслайн: на дереве без git-предка
# числа те же, что и прежде.
def _pin_git_discovery_to_the_tree(monkeypatch, root) -> None:
    """Запретить обнаружению git подниматься ВЫШЕ дерева замера."""
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(root.parent.resolve()))


def _payload_fixture(monkeypatch, tmp_path, mode):
    """Дерево + индекс + чистый extension-контекст ОДИН раз на тест.

    Возвращает `(query, effort) -> (raw, data)`: индекс строится единожды, поэтому
    несколько запросов в одном тесте (надбавка = домен − base) не платят за
    пересборку И, что важнее, меряются на ОДНОМ дереве — разность двух прогонов
    несла бы чужое смещение.
    """
    import rlm_tools_bsl.extension_detector as _ed
    from rlm_tools_bsl.bsl_index import IndexBuilder
    from rlm_tools_bsl.server import _rlm_start

    obj = tmp_path / "Documents" / "БюджетТест" / "Ext"
    obj.mkdir(parents=True)
    (obj / "ObjectModule.bsl").write_text("Процедура П() Экспорт\nКонецПроцедуры\n", encoding="utf-8")
    (tmp_path / "Configuration.xml").write_text(_CF_DESCRIPTOR, encoding="utf-8")
    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / ".idx"))
    monkeypatch.setenv("RLM_STRATEGY_MODE", mode)
    # До сборки индекса: git-обнаружение спрашивает и `IndexBuilder`.
    _pin_git_discovery_to_the_tree(monkeypatch, tmp_path)
    IndexBuilder().build(str(tmp_path), build_calls=False, build_metadata=True)

    # The baseline is the NO-extension start cost. detect_extension_context scans sibling /
    # grandparent dirs for extensions, so under pytest's shared tmp tree it can pick up OTHER
    # tests' extension fixtures and inject the "EXTENSIONS DETECTED" block — making the budget
    # ordering-dependent. Force a clean context (real current role, no nearby extensions).
    _real_single = _ed._detect_single

    def _clean_ctx(p):
        cur = _real_single(p) or _ed.ExtensionInfo(path=p, role=_ed.ConfigRole.UNKNOWN)
        return _ed.ExtensionContext(current=cur, nearby_extensions=[], nearby_main=None, warnings=[])

    monkeypatch.setattr("rlm_tools_bsl.server.detect_extension_context", _clean_ctx)

    def _start(query, effort):
        raw = _rlm_start(path=str(tmp_path), query=query, effort=effort)
        return raw, json.loads(raw)

    return _start


def _run_payload_budget(monkeypatch, tmp_path, mode, query, baseline, require_git_search=False, effort="auto"):
    from rlm_tools_bsl.server import _rlm_end

    start = _payload_fixture(monkeypatch, tmp_path, mode)
    raw, data = start(query, effort)
    try:
        assert not data["extension_context"]["nearby_extensions"], "budget config must be extension-free"
        # Бюджет обязан меряться на поддерживаемом дереве: на чужом формате
        # стратегия несёт лишний блок предупреждения, и число было бы не про то.
        assert data["source_support"] == "supported", "budget config must be a supported cf/edt tree"
        ceiling = int(baseline * _DRIFT)
        measured = _pathfree_len(raw, data["resolved_path"])
        assert measured <= ceiling, (
            f"{mode}/{query or '(none)'} rlm_start payload {measured} > {ceiling} (+5% of {baseline}). "
            "available_functions / index / strategy grew — trim or re-baseline intentionally."
        )
        # the new aggregate signature lives on available_functions — confirm it is present
        assert any("get_object_profile(name" in s for s in data["available_functions"])
        has_git_search = any(s.startswith("git_search(") for s in data["available_functions"])
        if require_git_search:
            assert has_git_search, "фикстура деградировала в non-git — бюджетная защита git_search.sig стала бы ложной"
        else:
            assert not has_git_search, (
                "ячейка обязана быть non-git, а git_search зарегистрирован: обнаружение git "
                "поднялось ВЫШЕ дерева замера. Ячейка мерила бы git-вариант (+1129 символов) "
                "против non-git бэйслайна — см. _pin_git_discovery_to_the_tree"
            )
        # index discovery keys present so the agent skips get_index_info() on start
        assert data["index"]["loaded"] is True
        assert "has_object_attributes" in data["index"]
    finally:
        _rlm_end(data["session_id"])


# ── v1.37.0: НАДБАВКА доменного рецепта, а не абсолют ячейки ────────────────
#
# Плановый предел правки доменного рецепта — +60 slim / +120 full, и он жёстче
# штатного `×1.05`: на full-ячейке ~50 000 символов тот даёт запас ~2 500, то есть
# случайный дубль абзаца в 900 символов прошёл бы зелёным.
#
# Мерить НАДБАВКУ обязательно, а не абсолют: ячейка домена = базовый payload ПЛЮС
# рецепт, и универсальные правки релиза (подписи, DISAMBIGUATION, COVERAGE) входят
# в неё целиком. Сравнение полной дельты ячейки с «+120» ложно остановило бы релиз
# на первой же задаче.
#
# Разность существующих словарей для этого НЕ годится: `_PAYLOAD_BASELINES` и
# `_DOMAIN_PAYLOAD_BASELINES` сняты в РАЗНЫЕ релизы, универсальная часть в них не
# одна и та же и не сокращается — на момент снятия смещение составляло −994 в slim,
# то есть гард был бы вакуумным. Поэтому здесь заморожена сама разность
# `домен − base`, снятая на ОДНОМ дереве, а в прогоне `base` меряется тем же
# способом и в том же процессе.
#
# v1.38.0 — ОСОЗНАННЫЙ ре-бэйслайн этих двух словарей (и ТОЛЬКО их).
#
# Числа выше были сняты на `29e5e0d`, а v1.37.0 съел бОльшую часть выданного ими
# запаса: замер на НЕТРОНУТОМ дереве `278f61f` (до первой правки v1.38.0) дал
# дельты +0…+57 в compact-форме и +0…+116 в full-форме при статье +120. То есть
# гард сравнивал бы рост ДВУХ релизов с лимитом ОДНОГО, и у четырёх доменов
# оставалось 4–28 символов — ни одна правка рецепта туда не влезала бы, а
# «уложиться» означало бы резать чужой, уже принятый текст.
#
# Заново заморожены ИЗМЕРЕННЫЕ значения `278f61f`, поэтому статья +60/+120 снова
# означает «рост ОДНОГО релиза». Ни абсолютные ячейки payload, ни ячейки текста
# стратегии при этом НЕ двигаются — там прежние потолки держатся.
_RECIPE_OVERHEAD_BASELINES: dict[tuple[str, str], int] = {
    ("slim", "проведение"): 987,
    ("full", "проведение"): 907,
    ("slim", "права"): 666,
    ("full", "права"): 591,
    ("slim", "расширения"): 750,
    ("full", "расширения"): 1649,
    ("slim", "структура объекта"): 382,
    ("full", "структура объекта"): 807,
    ("slim", "события формы"): 366,
    ("full", "события формы"): 283,
    ("slim", "ссылки"): 528,
    ("full", "ссылки"): 1459,
    ("slim", "ввод на основании"): 362,
    ("full", "ввод на основании"): 275,
    ("slim", "иерархия вызовов"): 1028,
    ("full", "иерархия вызовов"): 1787,
    ("slim", "достижимость"): 613,
    ("full", "достижимость"): 531,
}

# Та же величина для `full`-ФОРМЫ рецепта. Форму выбирает `effort`, а не режим
# стратегии: payload идёт по `medium` → `compact`, и `full`-форму девяти доменов
# не видит ни одна ячейка payload. Единственное место, где она меряется, —
# `get_strategy("high", …)`, то есть гард текста стратегии.
# v1.38.0: ре-бэйслайн по той же причине и теми же измерениями `278f61f`
# (см. комментарий к _RECIPE_OVERHEAD_BASELINES).
_FULL_FORM_RECIPE_OVERHEAD_BASELINES: dict[str, int] = {
    "проведение": 1831,
    "права": 1251,
    "расширения": 2837,
    "структура объекта": 1127,
    "события формы": 707,
    "ссылки": 2324,
    "ввод на основании": 533,
    "иерархия вызовов": 2764,
    "достижимость": 1197,
}

_RECIPE_OVERHEAD_LIMITS = {"slim": 60, "full": 120}


@pytest.mark.parametrize("mode", ["slim", "full"])
def test_compact_recipe_overhead_within_plan_limit(monkeypatch, tmp_path, mode):
    """`compact`-форма доменного рецепта растёт не более чем на плановую статью.

    Оба вычитаемых снимаются в ОДНОМ прогоне на ОДНОМ дереве — иначе в разность
    уезжает универсальный дрейф релиза, и предел перестаёт означать что-либо.
    """
    from rlm_tools_bsl.server import _rlm_end

    start = _payload_fixture(monkeypatch, tmp_path, mode)
    limit = _RECIPE_OVERHEAD_LIMITS[mode]

    def _measure(query, effort):
        raw, data = start(query, effort)
        try:
            return _pathfree_len(raw, data["resolved_path"])
        finally:
            _rlm_end(data["session_id"])

    base = _measure("", "auto")
    grown = []
    for domain in _BUDGET_DOMAINS:
        overhead = _measure(domain, _DOMAIN_EFFORT) - base
        frozen = _RECIPE_OVERHEAD_BASELINES[(mode, domain)]
        if overhead - frozen > limit:
            grown.append(f"{domain}: {frozen} -> {overhead} (+{overhead - frozen} > {limit})")
    assert not grown, (
        f"{mode}: compact-рецепт вырос сверх плановой статьи: {'; '.join(grown)}. "
        "Подрежьте текст рецепта или поднимите статью ОСОЗНАННО, отдельным решением."
    )


def test_full_form_recipe_overhead_within_plan_limit(_fmt_info, monkeypatch):
    """То же для `full`-формы рецепта — её видит ТОЛЬКО текст стратегии."""
    monkeypatch.setenv("RLM_STRATEGY_MODE", "full")
    snap = build_helper_metadata_snapshot()

    def _measure(query):
        return len(get_strategy("high", _fmt_info, registry=snap, idx_stats=_IDX_STATS, query=query))

    base = _measure("")
    grown = []
    for domain in _BUDGET_DOMAINS:
        overhead = _measure(domain) - base
        frozen = _FULL_FORM_RECIPE_OVERHEAD_BASELINES[domain]
        if overhead - frozen > 120:
            grown.append(f"{domain}: {frozen} -> {overhead} (+{overhead - frozen} > 120)")
    assert not grown, (
        f"full-форма рецепта выросла сверх плановой статьи: {'; '.join(grown)}. "
        "Подрежьте текст рецепта или поднимите статью ОСОЗНАННО, отдельным решением."
    )


# v1.34.0: whole-payload фикстура выше работает НЕ под git, поэтому её
# `available_functions` НЕ содержит `git_search` — вторая копия его `sig` (первую
# защищает snapshot full-стратегии) не была защищена ничем. Добавляем git-backed
# baseline; git — optional runtime capability, поэтому среда без него скипается тем
# же способом, что и tests/test_sandbox_parity.py. Production coverage это не
# ослабляет: там сама git-ветка недостижима, а non-git baseline выполняется всегда.
_GIT_PAYLOAD_BASELINES = {"slim": 22814, "full": 49528}


@pytest.mark.skipif(not shutil.which("git"), reason="git недоступен")
@pytest.mark.parametrize("mode", ["slim", "full"])
def test_git_backed_rlm_start_payload_within_budget(monkeypatch, tmp_path, mode):
    """Whole-payload на РЕАЛЬНОМ git-репозитории: только здесь в
    `available_functions` попадает `git_search`, и только здесь его `sig` виден
    бюджету дважды (available_functions + таблица хелперов full-стратегии).

    Проверяется и сам ФАКТ регистрации: иначе фикстура может тихо деградировать в
    non-git и дать ложную защиту."""
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, capture_output=True)
    _run_payload_budget(
        monkeypatch,
        tmp_path,
        mode,
        query="",
        baseline=_GIT_PAYLOAD_BASELINES[mode],
        require_git_search=True,
    )


# ── v1.35.2: ветка "индекса нет" получила СВОЙ бюджет ───────────────────────
#
# 26 существующих ячеек безусловно СТРОЯТ индекс, поэтому ветку `missing`
# бюджетом не мерило ничто — а именно туда v1.35.2 добавляет строку с причиной.
#
# Ячейка обязана быть path-free, и наивная замена одного `resolved_path` для
# этого НЕ годится: текст несёт путь четырежды (db_path, root, resolved×2) плюс
# `resolved_path` самого payload, а допущение «db_path и root начинаются с
# resolved_path» — свойство фикстуры, а не кода. Штатно корень индексов лежит ВНЕ
# дерева проекта (так же в autouse-фикстуре conftest), и наивная замена схлопнула
# бы 3 вхождения из 5. Поэтому заменяются ТРИ величины явно, от длинной к
# короткой: db_path содержит root, и обратный порядок оставил бы хвосты.
#
# Бэйслайны сняты прогоном ЭТОГО ЖЕ теста на НЕТРОНУТОМ коде.
_MISSING_INDEX_PAYLOAD_BASELINES = {"slim": 19995, "full": 47154}

# Тот же однопроцедурный модуль, что и у фикстуры с индексом.
_BUDGET_MODULE_BSL = "Процедура П() Экспорт\nКонецПроцедуры\n"


def _normalize_paths(raw: str, resolved: str) -> str:
    from rlm_tools_bsl.bsl_index import get_index_db_path, get_index_dir_root

    normalized = raw
    values = {str(get_index_db_path(resolved)), str(get_index_dir_root()), resolved}
    for value in sorted(values, key=len, reverse=True):
        normalized = normalized.replace(_esc(value), "<P>")
    return normalized


@pytest.mark.parametrize("mode", ["slim", "full"])
def test_missing_index_rlm_start_payload_within_budget(monkeypatch, tmp_path, mode):
    """Payload ветки `missing` на ПОДДЕРЖИВАЕМОМ дереве, нормализованный по путям.

    `_run_payload_budget` не переиспользуется намеренно: он безусловно строит
    индекс, а здесь предмет измерения — ровно его отсутствие.
    """
    import rlm_tools_bsl.extension_detector as _ed
    from rlm_tools_bsl.server import _rlm_end, _rlm_start

    project = tmp_path / "cfg"
    obj = project / "Documents" / "БюджетТест" / "Ext"
    obj.mkdir(parents=True)
    (obj / "ObjectModule.bsl").write_text(_BUDGET_MODULE_BSL, encoding="utf-8")
    (project / "Configuration.xml").write_text(_CF_DESCRIPTOR, encoding="utf-8")
    # Корень индексов — ВНЕ дерева проекта: так он лежит штатно, и именно там
    # наивная нормализация схлопнула бы 3 вхождения из 5.
    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
    monkeypatch.setenv("RLM_STRATEGY_MODE", mode)
    _pin_git_discovery_to_the_tree(monkeypatch, project)

    _real_single = _ed._detect_single

    def _clean_ctx(p):
        cur = _real_single(p) or _ed.ExtensionInfo(path=p, role=_ed.ConfigRole.UNKNOWN)
        return _ed.ExtensionContext(current=cur, nearby_extensions=[], nearby_main=None, warnings=[])

    monkeypatch.setattr("rlm_tools_bsl.server.detect_extension_context", _clean_ctx)

    raw = _rlm_start(path=str(project), query="")
    data = json.loads(raw)
    try:
        assert data["source_support"] == "supported", "бюджет мерится на поддерживаемом дереве"
        assert data["index"]["loaded"] is False
        assert data["index"]["index_status"] == "missing"
        assert not data["extension_context"]["nearby_extensions"], "budget config must be extension-free"
        assert not any(s.startswith("git_search(") for s in data["available_functions"]), (
            "ячейка обязана быть non-git — см. _pin_git_discovery_to_the_tree"
        )

        baseline = _MISSING_INDEX_PAYLOAD_BASELINES[mode]
        ceiling = int(baseline * _DRIFT)
        normalized = len(_normalize_paths(raw, data["resolved_path"]))
        assert normalized <= ceiling, (
            f"{mode}/missing rlm_start payload {normalized} > {ceiling} (+5% of {baseline}). "
            "Строка причины / available_functions / стратегия выросли — подрежьте или "
            "ре-бэйслайньте осознанно."
        )
    finally:
        _rlm_end(data["session_id"])


# ── v1.33.0: длинные пояснения переехали из sig в recipe ────────────────────
#
# `sig` уходит агенту на КАЖДОМ старте и лежит в бюджете, `recipe` — нет
# (его отдаёт rlm_help по запросу). Шесть самых длинных sig занимали 4166
# символов из 12727 всего available_functions; запаса под контракты v1.33.0
# при этом не оставалось (slim+рецепт 49 символов, full payload 244).
_SIG_CEILINGS = {
    "find_call_hierarchy": 560,
    "find_path": 540,
    # v1.37.0 (завершение релиза): 520 -> 410 по правилу `ceil10(факт * 1.10)` и
    # ТОЛЬКО ВНИЗ. Задача 0 срезала прозу, факт стал 370, и прежний потолок оставлял
    # 150 символов, которые следующая правка съела бы молча. Остальные потолки уже
    # ЖЁСТЧЕ этого правила и потому не двигаются — оно опускает, но не поднимает.
    "get_object_full_structure": 410,
    # v1.36.0 (Задача 3): sig 491 -> 467 (проекция orphan_methods вместо
    # дублирующего хвоста). Потолок ЖЁСТЧЕ общего правила «факт + 10 %»
    # (оно дало бы 520): у этой подписи запас был узким и до релиза
    # (500 при 491), а расширять его на механической правке не за что.
    # v1.40.0: 480 -> 490 под ОСОЗНАННОЕ расширение контракта — `loc=Σ строк методов`
    # (+16): рецепт с определением читают не всегда, и агент приёмки принял `loc` за
    # строки файла. Потолок поднят ровно под эту правку, запас остаётся узким (7).
    "get_object_modules": 490,
    "get_module_outline": 430,
    "find_register_movements": 380,
    # v1.36.0 (Задача 0): пояснения переехали из БЮДЖЕТИРУЕМОГО sig в recipe,
    # поэтому потолки опускаются под новый факт (`ceil10(факт * 1.10)`) — иначе
    # освобождённый запас будет молча съеден следующей правкой.
    # `find_functional_options` потолка не имел вовсе; он ДОБАВЛЕН здесь при
    # промежуточном факте 371 и поднят Задачей 4 до ceil10(412 * 1.10) под её
    # осознанно расширенный контракт (include_content / общий ключ name).
    # v1.38.0 (завершение релиза): потолки опускаются по правилу
    #  и ТОЛЬКО ВНИЗ: задача бюджета срезала прозу,
    # и освобождённый запас иначе будет молча съеден следующей правкой.
    "find_functional_options": 430,
    # Четыре подписи, несущие предупреждения о ложном отрицательном выводе
    # (см. test_sigs_warn_about_false_negative_answers). Потолок нужен именно им:
    # предупреждение тянет текст вверх, а sig уходит агенту на КАЖДОМ старте.
    # Запас к фактическому размеру ~10%: смысл дописывать можно, растекаться — нет.
    "parse_form": 510,
    "search_regions": 550,
    "search_module_headers": 340,
    "search_methods": 320,
}


@pytest.mark.parametrize("helper,ceiling", sorted(_SIG_CEILINGS.items()))
def test_long_sigs_are_trimmed(helper, ceiling):
    """v1.33.0: длинные пояснения переехали в recipe (не в бюджете), sig несёт имена
    ключей/параметров И критические pre-call предупреждения — те, без которых агент
    делает ЛОЖНЫЙ вывод из ответа (см. test_sigs_warn_about_false_negative_answers:
    рецепт читают не всегда, sig уходит на каждом старте). Всё остальное — в recipe.
    Растить обратно нельзя — бюджет старта на пределе."""
    snap = build_helper_metadata_snapshot()
    sig = snap[helper]["sig"]
    assert len(sig) <= ceiling, f"{helper} sig = {len(sig)} > {ceiling}: перенеси пояснение в recipe"


def test_trimmed_sigs_keep_their_keys():
    """Сокращение НЕ должно съесть имена ключей: агент выбирает вызов по ним."""
    snap = build_helper_metadata_snapshot()
    required = {
        "find_register_movements": [
            "code_registers",
            "suppressed_main_code_registers",
            "posting_handler_present",
            # v1.38.0 — заголовочные ключи релиза. Спутники `_total`/`_truncated`
            # идут суффиксной формой (та же идиома, что у `_meta.delegates`),
            # поэтому дословно их здесь не требуем.
            "declared_registers",
            "undeclared_code_registers",
            "unresolved",
        ],
        "get_object_modules": ["modules", "category", "object_name"],
        "find_path": ["from_name", "to_name", "max_depth"],
        "find_call_hierarchy": ["direction", "depth", "module_hint"],
        "get_module_outline": ["regions", "methods"],
        "get_object_full_structure": ["attributes", "tabular_sections"],
    }
    for helper, keys in required.items():
        sig = snap[helper]["sig"]
        for k in keys:
            assert k in sig, f"{helper}: ключ {k} пропал из sig при сокращении"


def _sig_says(sig: str, alternatives: tuple[str, ...], helper: str, what: str) -> None:
    """Хотя бы одна из формулировок обязана присутствовать.

    Гард намеренно НЕ требует дословного текста: он проверяет, что смысл на месте,
    и переживает нормальную редактуру. Дословное совпадение ломало бы даже более
    точную переформулировку — а это провоцирует «поправить тест», а не текст.
    """
    low = sig.casefold()
    assert any(a.casefold() in low for a in alternatives), (
        f"{helper}: из sig пропало {what} (ни одна из формулировок {alternatives} не найдена)"
    )


def test_sigs_warn_about_false_negative_answers():
    """Предупреждения о ЛОЖНОМ отрицательном выводе обязаны жить в sig, а не в рецепте.

    Рецепт (`rlm_help`) читают не всегда, а sig уходит агенту на КАЖДОМ старте.
    Каждое предупреждение обязано нести ТРИ вещи: причину, границы (где именно ответ
    неполон) и ДЕЙСТВИЕ — без действия предупреждение агента не спасает.

    Происхождение (важно для будущих правок — что здесь факт, а что профилактика):
      * `parse_form` — воспроизведённый инцидент: после смены контракта `types` со
        строки на list идиома `'DynamicList' in a['types']` стала сравнением по
        ЭЛЕМЕНТУ, а в выгрузке Конфигуратора элемент несёт префикс пространства имён
        (на боевой конфигурации это cfg:/xs:/v8:/mxl:/v8ui:/dcsset:). Отчёт заявил
        «DynamicList нет ни в одной форме», хотя он есть в двух. В EDT префикса нет
        вовсе, и та же проверка сработала бы — поэтому в sig названы ОБА формата.
      * `search_regions` — воспроизведённый инцидент: подстрока без стемминга,
        'Себестоимость' не находит 'Себестоимости', и отчёт заявил, что подсистемы
        себестоимости «нет как таковой».
      * `search_module_headers` — механика поиска та же самая, поэтому предупреждение
        закреплено ПРОФИЛАКТИЧЕСКИ: отдельного инцидента по нему не было.
      * `search_methods` — токенайзер trigram: запрос короче 3 символов по основному
        индексу не ищется вовсе. При расширениях live-ветка фильтрует подстрокой без
        порога длины, поэтому совпадения, ЕСЛИ они есть, придут только из расширений;
        если их нет — ответ останется пустым. Ни пустой, ни extension-only ответ не
        доказывает отсутствия метода в основной конфигурации.
    """
    snap = build_helper_metadata_snapshot()

    sig = snap["parse_form"]["sig"]
    for marker in ("list[str]", "CF", "EDT", "DynamicList"):
        assert marker.casefold() in sig.casefold(), f"parse_form: в sig нет упоминания {marker!r}"
    _sig_says(sig, ("rsplit(", "endswith(", "removeprefix(", "хвост"), "parse_form", "ДЕЙСТВИЕ (как сверять тип)")

    sig = snap["search_methods"]["sig"]
    _sig_says(sig, ("trigram",), "search_methods", "причина (токенайзер)")
    _sig_says(sig, ("короче 3", "от 3 символов", "3 символов"), "search_methods", "порог длины запроса")
    _sig_says(sig, ("основному", "main"), "search_methods", "граница scope (какой индекс не ищет)")
    _sig_says(sig, ("расширени", "extension"), "search_methods", "оговорка про live-ветку расширений")
    # Без этих двух предупреждение вырождается: «расширения поддерживаются» пройдёт
    # проверки выше, но не скажет ни что выдача СОСТОИТ из одних расширений, ни что
    # с этим делать. Ровно тот же набор (причина + граница + ДЕЙСТВИЕ), что ниже
    # требуется от search_regions/search_module_headers.
    _sig_says(
        sig,
        ("только из их методов", "только из расширен", "extension-only"),
        "search_methods",
        "граница результата при query < 3 (выдача — ОДНИ расширения)",
    )
    _sig_says(
        sig,
        ("бери от 3", "используй запрос от 3", "query >= 3", "запрос от 3"),
        "search_methods",
        "ДЕЙСТВИЕ (как получить ответ по main)",
    )

    for helper in ("search_regions", "search_module_headers"):
        sig = snap[helper]["sig"]
        _sig_says(sig, ("стемминг",), helper, "причина (нет стемминга)")
        _sig_says(sig, ("отсутстви",), helper, "суть (0 ≠ отсутствие)")
        _sig_says(sig, ("проверь", "попробуй", "возьми"), helper, "ДЕЙСТВИЕ (что делать при нуле)")

    # v1.37.0 — три НОВЫХ предупреждения того же класса: ноль (или None) в них
    # читается как доказанное отсутствие, хотя доказывает совсем другое. Без этих
    # ассертов следующая резка прозы снимет их молча — ровно так, как снялись бы
    # предупреждения выше. Провенанс каждого:
    #   * `get_object_full_structure.posting` — на ЧИСТОМ index-пути значение не
    #     хранится вовсе, и `None` там означает «НЕ читалось», а не «документ не
    #     проводится». Гарантированный маршрут — `find_register_movements`.
    #   * `find_references_to_object` — `kind='owner'` на индексе возвращал 0 и НЕ
    #     попадал в `unsupported_kinds`: тот список — capability-карта LIVE-парсера
    #     и на `source='index'` всегда пуст. Что применено РЕАЛЬНО, говорит
    #     `kinds_applied`.
    #   * `find_register_movements` — `code_registers=0` при делегировании читается
    #     как «движений нет»; имена получателей называет `_meta.delegates`.
    sig = snap["get_object_full_structure"]["sig"]
    _sig_says(sig, ("posting=None",), "get_object_full_structure", "предмет (какое поле)")
    _sig_says(sig, ("НЕ читалось", "не читалось"), "get_object_full_structure", "причина")
    _sig_says(
        sig,
        ("не «не проводится»", "не 'не проводится'", "не «не проводится"),
        "get_object_full_structure",
        "суть (None ≠ Posting=Deny)",
    )

    sig = snap["find_references_to_object"]["sig"]
    _sig_says(sig, ("kinds_applied",), "find_references_to_object", "ДЕЙСТВИЕ (что применено реально)")
    _sig_says(sig, ("unsupported_kinds",), "find_references_to_object", "предмет")
    _sig_says(sig, ("LIVE", "live"), "find_references_to_object", "граница (чья это карта)")

    sig = snap["find_register_movements"]["sig"]
    _sig_says(sig, ("_meta.delegates",), "find_register_movements", "ДЕЙСТВИЕ (где имена делегатов)")
