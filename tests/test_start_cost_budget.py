"""v1.23.0 — start-cost budget guard for rlm_start.

The strategy / recipe / tool-description edits in this release are formulation
REPLACEMENTS (not bulk additions); the new get_object_profile signature + the
extended rlm_start.index fields are the only intended growth. This test pins the
whole-strategy payload (slim AND full, with/without a business recipe) to the
v1.23.0 baselines and fails if a future edit grows any case by more than ~5%.

Baselines are deterministic: the strategy is built from the FROZEN helper-metadata
snapshot (build_helper_metadata_snapshot force-registers git_search), so the numbers
do not depend on git availability or the live registry.
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
_BASELINES = {
    ("slim", ""): 7146,
    ("slim", "проведение"): 7990,
    ("full", ""): 33858,
    ("full", "проведение"): 35597,
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
}

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
    assert len(sig) <= 525, f"get_object_profile sig is {len(sig)} chars — trim to stay in budget"


def test_helper_snapshot_count_locked():
    """Adding/removing a registered helper is an intentional change — update this number."""
    assert len(build_helper_metadata_snapshot()) == 53


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
    else:
        baseline = baselines[mode]
    _run_payload_budget(monkeypatch, tmp_path, mode, query, baseline)


def _run_payload_budget(monkeypatch, tmp_path, mode, query, baseline, require_git_search=False):
    import rlm_tools_bsl.extension_detector as _ed
    from rlm_tools_bsl.bsl_index import IndexBuilder
    from rlm_tools_bsl.server import _rlm_end, _rlm_start

    obj = tmp_path / "Documents" / "БюджетТест" / "Ext"
    obj.mkdir(parents=True)
    (obj / "ObjectModule.bsl").write_text("Процедура П() Экспорт\nКонецПроцедуры\n", encoding="utf-8")
    (tmp_path / "Configuration.xml").write_text(_CF_DESCRIPTOR, encoding="utf-8")
    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / ".idx"))
    monkeypatch.setenv("RLM_STRATEGY_MODE", mode)
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

    raw = _rlm_start(path=str(tmp_path), query=query)
    data = json.loads(raw)
    try:
        assert not data["extension_context"]["nearby_extensions"], "budget config must be extension-free"
        # Бюджет обязан меряться на поддерживаемом дереве: на чужом формате
        # стратегия несёт лишний блок предупреждения, и число было бы не про то.
        assert data["source_support"] == "supported", "budget config must be a supported cf/edt tree"
        ceiling = int(baseline * _DRIFT)
        assert len(raw) <= ceiling, (
            f"{mode}/{query or '(none)'} rlm_start payload {len(raw)} > {ceiling} (+5% of {baseline}). "
            "available_functions / index / strategy grew — trim or re-baseline intentionally."
        )
        # the new aggregate signature lives on available_functions — confirm it is present
        assert any("get_object_profile(name" in s for s in data["available_functions"])
        if require_git_search:
            assert any(s.startswith("git_search(") for s in data["available_functions"]), (
                "фикстура деградировала в non-git — бюджетная защита git_search.sig стала бы ложной"
            )
        # index discovery keys present so the agent skips get_index_info() on start
        assert data["index"]["loaded"] is True
        assert "has_object_attributes" in data["index"]
    finally:
        _rlm_end(data["session_id"])


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


# ── v1.33.0: длинные пояснения переехали из sig в recipe ────────────────────
#
# `sig` уходит агенту на КАЖДОМ старте и лежит в бюджете, `recipe` — нет
# (его отдаёт rlm_help по запросу). Шесть самых длинных sig занимали 4166
# символов из 12727 всего available_functions; запаса под контракты v1.33.0
# при этом не оставалось (slim+рецепт 49 символов, full payload 244).
_SIG_CEILINGS = {
    "find_call_hierarchy": 560,
    "find_path": 540,
    "get_object_full_structure": 520,
    "get_object_modules": 500,
    "get_module_outline": 470,
    "find_register_movements": 380,
    # Четыре подписи, несущие предупреждения о ложном отрицательном выводе
    # (см. test_sigs_warn_about_false_negative_answers). Потолок нужен именно им:
    # предупреждение тянет текст вверх, а sig уходит агенту на КАЖДОМ старте.
    # Запас к фактическому размеру ~10%: смысл дописывать можно, растекаться — нет.
    "parse_form": 600,
    "search_regions": 560,
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
