"""Tests for v1.10.0 changes:

- _resolve_object_xml fake .mdo / .xml normalization (Tier 1.1)
- parse_metadata_xml posting extraction (CF + EDT) (Tier 1.2)
- find_register_movements is_postable hint (Tier 1.2)
- find_event_subscriptions event_filter + limit (Tier 1.4)
- Sandbox._wrap_helpers session-wide duplicate detection (Tier 1.5)
- get_object_full_structure (Tier 2.1)
- find_call_hierarchy (Tier 2.2)
- bsl_help bridge to _BUSINESS_RECIPES / _RECIPE_ALIASES (G.5b)
- find_callers compact-mode positioning (G.6)
- Recipes coverage / build_helpers_table regression (G.9.3)
"""

from __future__ import annotations

import os
import pytest

from rlm_tools_bsl.bsl_xml_parsers import parse_metadata_xml


# ============================================================================
# Tier 1.1 — _resolve_object_xml normalization
# ============================================================================


_CF_DOC_TEMPLATE = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses" '
    'xmlns:v8="http://v8.1c.ru/8.1/data/core">'
    "<Document><Properties><Name>{name}</Name></Properties></Document>"
    "</MetaDataObject>"
)

_EDT_DOC_TEMPLATE = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<mdclass:Document xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass">'
    "<name>{name}</name></mdclass:Document>"
)


def test_parse_object_xml_real_xml_path(bsl_env):
    """Real, existing .xml path → returns AS-IS."""
    doc_dir = bsl_env.path / "Documents" / "ТестДок" / "Ext"
    doc_dir.mkdir(parents=True)
    (doc_dir / "Document.xml").write_text(_CF_DOC_TEMPLATE.format(name="ТестДок"), encoding="utf-8")
    # Direct XML path that exists
    result = bsl_env.bsl["parse_object_xml"]("Documents/ТестДок/Ext/Document.xml")
    assert isinstance(result, dict)
    assert result.get("name") == "ТестДок"


def test_parse_object_xml_fake_mdo_path_normalizes(bsl_env):
    """Fake 'Documents/X.mdo' (no actual file) but real Documents/X/X.mdo exists → normalized."""
    edt_dir = bsl_env.path / "Documents" / "ЕДТДок"
    edt_dir.mkdir(parents=True)
    (edt_dir / "ЕДТДок.mdo").write_text(_EDT_DOC_TEMPLATE.format(name="ЕДТДок"), encoding="utf-8")
    # 'Fake' .mdo path — should auto-resolve to ЕДТДок/ЕДТДок.mdo
    result = bsl_env.bsl["parse_object_xml"]("Documents/ЕДТДок.mdo")
    assert isinstance(result, dict)
    assert result.get("name") == "ЕДТДок"


def test_parse_object_xml_fake_xml_path_normalizes_to_cf(bsl_env):
    """Fake 'Documents/X.xml' → tries Documents/X/Ext/Document.xml."""
    cf_dir = bsl_env.path / "Documents" / "СФДок" / "Ext"
    cf_dir.mkdir(parents=True)
    (cf_dir / "Document.xml").write_text(_CF_DOC_TEMPLATE.format(name="СФДок"), encoding="utf-8")
    # 'Fake' .xml at Documents/СФДок.xml — should normalize to СФДок/Ext/Document.xml
    result = bsl_env.bsl["parse_object_xml"]("Documents/СФДок.xml")
    assert isinstance(result, dict)
    assert result.get("name") == "СФДок"


def test_parse_object_xml_truly_missing_raises_with_hint(bsl_env):
    """Nothing matches — FileNotFoundError with explicit message about directory candidate."""
    with pytest.raises(FileNotFoundError) as excinfo:
        bsl_env.bsl["parse_object_xml"]("Documents/НеСуществует.mdo")
    msg = str(excinfo.value)
    assert "Documents/НеСуществует" in msg or "Documents\\\\НеСуществует" in msg


def test_resolve_object_xml_no_garbage_candidates(bsl_env):
    """No 'Documents/X.mdo/X.mdo.mdo' generated when normalizing fake .mdo."""
    # Verify by tracking glob_files calls — but easier to just verify it raises cleanly.
    # The key is: input 'Documents/X.mdo' should not lead to generation of
    # candidates like 'Documents/X.mdo/X.mdo.mdo' (which old code did).
    # We verify by ensuring the error path is fast (no insane recursion) and message
    # mentions the normalized base, not the fake .mdo as a directory.
    with pytest.raises(FileNotFoundError) as excinfo:
        bsl_env.bsl["parse_object_xml"]("Documents/Призрак.mdo")
    # The hint in the message references the normalized base
    msg = str(excinfo.value)
    assert "Призрак.mdo/Призрак.mdo.mdo" not in msg.replace("\\", "/")


# ============================================================================
# Tier 1.2 — parse_metadata_xml posting extraction
# ============================================================================


_CF_DOC_POSTING = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses" '
    'xmlns:v8="http://v8.1c.ru/8.1/data/core">'
    "<Document><Properties>"
    "<Name>X</Name>"
    "{posting_tag}"
    "</Properties></Document>"
    "</MetaDataObject>"
)


def test_parse_metadata_xml_cf_posting_allow():
    xml = _CF_DOC_POSTING.format(posting_tag="<Posting>Allow</Posting>")
    parsed = parse_metadata_xml(xml)
    assert parsed.get("posting") == "Allow"


def test_parse_metadata_xml_cf_posting_deny():
    xml = _CF_DOC_POSTING.format(posting_tag="<Posting>Deny</Posting>")
    parsed = parse_metadata_xml(xml)
    assert parsed.get("posting") == "Deny"


def test_parse_metadata_xml_cf_posting_use_selectively():
    xml = _CF_DOC_POSTING.format(posting_tag="<Posting>UseSelectively</Posting>")
    parsed = parse_metadata_xml(xml)
    assert parsed.get("posting") == "UseSelectively"


def test_parse_metadata_xml_cf_no_posting_tag():
    xml = _CF_DOC_POSTING.format(posting_tag="")
    parsed = parse_metadata_xml(xml)
    assert "posting" not in parsed


def test_parse_metadata_xml_edt_posting_attribute():
    xml = (
        "<?xml version='1.0' encoding='UTF-8'?>"
        '<mdclass:Document xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass" '
        'posting="Deny">'
        "<name>X</name>"
        "</mdclass:Document>"
    )
    parsed = parse_metadata_xml(xml)
    assert parsed.get("posting") == "Deny"


def test_parse_metadata_xml_edt_posting_child_element():
    """EDT alternate: posting as a child element."""
    xml = (
        "<?xml version='1.0' encoding='UTF-8'?>"
        '<mdclass:Document xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass">'
        "<name>X</name>"
        "<posting>Allow</posting>"
        "</mdclass:Document>"
    )
    parsed = parse_metadata_xml(xml)
    assert parsed.get("posting") == "Allow"


# ============================================================================
# Tier 1.2 — find_register_movements is_postable hint
# ============================================================================


def test_find_register_movements_no_modules_with_posting_deny(bsl_env):
    """Document with no ObjectModule + Posting=Deny → is_postable=False + hint."""
    doc_dir = bsl_env.path / "Documents" / "Письмо"
    doc_dir.mkdir(parents=True)
    ext = doc_dir / "Ext"
    ext.mkdir()
    (ext / "Document.xml").write_text(
        _CF_DOC_POSTING.format(posting_tag="<Posting>Deny</Posting>").replace("<Name>X</Name>", "<Name>Письмо</Name>"),
        encoding="utf-8",
    )
    result = bsl_env.bsl["find_register_movements"]("Письмо")
    assert result["code_registers"] == []
    assert result.get("is_postable") is False
    assert "непровод" in result.get("hint", "").lower()
    assert result.get("posting") == "Deny"


def test_find_register_movements_use_selectively_not_marked_unpostable(bsl_env):
    """Posting=UseSelectively means PART of types post → is_postable not False."""
    doc_dir = bsl_env.path / "Documents" / "ЧастичноПровод"
    doc_dir.mkdir(parents=True)
    ext = doc_dir / "Ext"
    ext.mkdir()
    (ext / "Document.xml").write_text(
        _CF_DOC_POSTING.format(posting_tag="<Posting>UseSelectively</Posting>").replace(
            "<Name>X</Name>", "<Name>ЧастичноПровод</Name>"
        ),
        encoding="utf-8",
    )
    result = bsl_env.bsl["find_register_movements"]("ЧастичноПровод")
    # No modules, but UseSelectively → no is_postable=False.
    assert result.get("is_postable") is not False


# ============================================================================
# Tier 1.4 — find_event_subscriptions event_filter + limit
# ============================================================================


def _make_event_subs_fixture(tmpdir):
    """Add event subscriptions XML files."""
    sub_dir = os.path.join(tmpdir, "EventSubscriptions", "ПередЗаписью")
    os.makedirs(sub_dir, exist_ok=True)
    with open(os.path.join(sub_dir, "EventSubscription.xml"), "w", encoding="utf-8") as f:
        f.write(
            "<MetaDataObject xmlns:md='http://v8.1c.ru/8.3/MDClasses' xmlns:v8='http://v8.1c.ru/8.1/data/core'>"
            "<md:EventSubscription><md:Properties>"
            "<md:Name>СубПередЗаписью</md:Name>"
            "<md:Event>BeforeWrite</md:Event>"
            "<md:Handler>CommonModule.МойМодуль.Обработчик</md:Handler>"
            "</md:Properties></md:EventSubscription>"
            "</MetaDataObject>"
        )
    sub2 = os.path.join(tmpdir, "EventSubscriptions", "ПриЗаписи")
    os.makedirs(sub2, exist_ok=True)
    with open(os.path.join(sub2, "EventSubscription.xml"), "w", encoding="utf-8") as f:
        f.write(
            "<MetaDataObject xmlns:md='http://v8.1c.ru/8.3/MDClasses' xmlns:v8='http://v8.1c.ru/8.1/data/core'>"
            "<md:EventSubscription><md:Properties>"
            "<md:Name>СубПриЗаписи</md:Name>"
            "<md:Event>OnWrite</md:Event>"
            "<md:Handler>CommonModule.МойМодуль.Обработчик2</md:Handler>"
            "</md:Properties></md:EventSubscription>"
            "</MetaDataObject>"
        )


def test_find_event_subscriptions_default_returns_list(bsl_env):
    """Default call without limit → list[dict] (контракт прежний)."""
    _make_event_subs_fixture(str(bsl_env.path))
    result = bsl_env.bsl["find_event_subscriptions"]()
    assert isinstance(result, list)
    assert len(result) == 2


def test_find_event_subscriptions_limit_returns_dict(bsl_env):
    """With limit → top-level dict {subscriptions, total, returned, has_more}."""
    _make_event_subs_fixture(str(bsl_env.path))
    result = bsl_env.bsl["find_event_subscriptions"]("", limit=1)
    assert isinstance(result, dict)
    assert "subscriptions" in result
    assert result["total"] == 2
    assert result["returned"] == 1
    assert result["has_more"] is True


def test_find_event_subscriptions_event_filter(bsl_env):
    """event_filter works on live path (no index)."""
    _make_event_subs_fixture(str(bsl_env.path))
    result = bsl_env.bsl["find_event_subscriptions"]("", event_filter=["BeforeWrite"])
    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0]["event"] == "BeforeWrite"


def test_find_event_subscriptions_event_filter_russian(bsl_env):
    """event_filter with Russian event name."""
    _make_event_subs_fixture(str(bsl_env.path))
    # No Russian event name in fixture — empty result, but doesn't crash.
    result = bsl_env.bsl["find_event_subscriptions"]("", event_filter=["ПередЗаписью"])
    assert isinstance(result, list)


def test_find_event_subscriptions_filter_falls_back_when_index_empty(bsl_env):
    """Codex round 12 Medium: stale/empty event_subscriptions table + event_filter
    не должен заглушать live fallback.

    Раньше: get_event_subscriptions(..., event_filter=[...]) при пустой таблице
    возвращал [] (потому что if event_filter), и helpers.py принимал это как
    authoritative — live XML не вызывался, агент видел "ничего нет" хотя на
    диске есть подписки.

    Теперь: пустая таблица → None → live fallback срабатывает и применяет
    event_filter сам в memory."""

    class _EmptyEventSubsIdxReader(_StubIdxReader):
        """Index reader where event_subscriptions table EXISTS but is empty
        (stale index scenario)."""

        def get_event_subscriptions(self, object_name="", custom_only=False, event_filter=None, object_ref=""):
            # Имитируем реальное поведение IndexReader после фикса:
            # пустая таблица → None независимо от наличия event_filter.
            # object_ref — keyword-параметр реального ридера с v1.28.0 (category-aware
            # матчинг); двойник обязан повторять сигнатуру, иначе TypeError вместо фолбэка.
            return None

    # Создаём реальные подписки на диске
    _make_event_subs_fixture(str(bsl_env.path))

    bsl = _make_bsl_with_stub_idx(str(bsl_env.path), _EmptyEventSubsIdxReader())

    # С event_filter, но при пустой таблице — должен сработать live fallback
    result = bsl["find_event_subscriptions"]("", event_filter=["BeforeWrite"])
    assert isinstance(result, list)
    # В fixture есть подписка с event=BeforeWrite — должна найтись через live
    assert len(result) == 1
    assert result[0]["event"] == "BeforeWrite"


def test_index_reader_get_event_subscriptions_distinguishes_empty_vs_no_match():
    """Codex round 12 Medium: непосредственно тест IndexReader.get_event_subscriptions
    различает (а) пустую таблицу → None и (б) непустую таблицу с фильтром, который
    ничего не нашёл → []."""
    import sqlite3
    import tempfile
    from rlm_tools_bsl.bsl_index import IndexReader

    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test.db")
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE event_subscriptions ("
            "name TEXT, synonym TEXT, event TEXT, handler_module TEXT, "
            "handler_procedure TEXT, source_types TEXT, source_count INTEGER, file TEXT)"
        )
        conn.commit()
        conn.close()

        reader = IndexReader(db_path)

        # (а) Пустая таблица + event_filter → None (caller должен делать fallback)
        res = reader.get_event_subscriptions("", event_filter=["BeforeWrite"])
        assert res is None, f"Empty table with filter must return None for fallback, got {res}"

        # (а') Пустая таблица без event_filter → тоже None
        res2 = reader.get_event_subscriptions("")
        assert res2 is None

        # Заполняем таблицу одной подпиской на OnWrite
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO event_subscriptions VALUES ('Sub1', '', 'OnWrite', 'Mod1', 'Proc1', '[]', 0, 'file.xml')"
        )
        conn.commit()
        conn.close()
        reader.close()
        reader = IndexReader(db_path)

        # (б) Непустая таблица, фильтр НЕ совпадает → [] (authoritative)
        res3 = reader.get_event_subscriptions("", event_filter=["BeforeWrite"])
        assert res3 == [], f"Non-empty table with non-matching filter must return [], got {res3}"

        # (б') Непустая таблица, фильтр совпадает → [{...}]
        res4 = reader.get_event_subscriptions("", event_filter=["OnWrite"])
        assert isinstance(res4, list) and len(res4) == 1
        assert res4[0]["event"] == "OnWrite"

        reader.close()


# ============================================================================
# Tier 1.5 — Sandbox session-wide duplicate detection
# ============================================================================


def test_sandbox_duplicate_within_one_execute(bsl_env):
    from rlm_tools_bsl.sandbox import Sandbox

    from rlm_tools_bsl.format_detector import detect_format

    sandbox = Sandbox(str(bsl_env.path), execution_timeout_seconds=5, format_info=detect_format(str(bsl_env.path)))
    res = sandbox.execute("find_module('foo')\nfind_module('foo')\n")
    calls = res.helper_calls or []
    assert len(calls) == 2
    # Second call must reference first
    assert calls[1].duplicate_of == calls[0].seq
    assert calls[0].duplicate_of is None


def test_sandbox_duplicate_across_executes(bsl_env):
    """Critical: cross-execute duplicate detection (session-wide state)."""
    from rlm_tools_bsl.sandbox import Sandbox

    from rlm_tools_bsl.format_detector import detect_format

    sandbox = Sandbox(str(bsl_env.path), execution_timeout_seconds=5, format_info=detect_format(str(bsl_env.path)))
    res1 = sandbox.execute("find_module('xyz')")
    res2 = sandbox.execute("find_module('xyz')")
    # First execute call: no dup
    assert res1.helper_calls[0].duplicate_of is None
    first_seq = res1.helper_calls[0].seq
    # Second execute call: dup pointing to first execute's seq
    assert res2.helper_calls[0].duplicate_of == first_seq


def test_sandbox_different_args_no_duplicate(bsl_env):
    from rlm_tools_bsl.sandbox import Sandbox

    from rlm_tools_bsl.format_detector import detect_format

    sandbox = Sandbox(str(bsl_env.path), execution_timeout_seconds=5, format_info=detect_format(str(bsl_env.path)))
    res = sandbox.execute("find_module('a')\nfind_module('b')\n")
    assert res.helper_calls[0].duplicate_of is None
    assert res.helper_calls[1].duplicate_of is None


def test_sandbox_unhashable_kwargs_does_not_break(bsl_env):
    """Passing list/dict as kwargs must not break the wrapper (uses repr → safe)."""
    from rlm_tools_bsl.sandbox import Sandbox

    from rlm_tools_bsl.format_detector import detect_format

    sandbox = Sandbox(str(bsl_env.path), execution_timeout_seconds=5, format_info=detect_format(str(bsl_env.path)))
    # find_event_subscriptions with event_filter list — must not crash
    res = sandbox.execute("find_event_subscriptions('', event_filter=['BeforeWrite'])\n")
    assert res.error is None
    assert len(res.helper_calls) == 1


def test_sandbox_helper_return_value_unchanged(bsl_env):
    """Wrapper does NOT modify helper return values; anti-duplicate AND efficiency
    nudges are response-metadata-only (never the helper return / stdout) — R3 #4."""
    from rlm_tools_bsl.sandbox import Sandbox

    from rlm_tools_bsl.format_detector import detect_format

    sandbox = Sandbox(str(bsl_env.path), execution_timeout_seconds=5, format_info=detect_format(str(bsl_env.path)))
    res = sandbox.execute("result = find_module('МойМодуль')\nprint(type(result).__name__)\n")
    assert res.error is None
    assert "list" in res.stdout

    # The SECOND find_module on the same object fires the reuse_var efficiency nudge —
    # it must live ONLY in ExecutionResult.efficiency_hints, never in the return / stdout.
    res2 = sandbox.execute("result = find_module('МойМодуль')\nprint(type(result).__name__)\n")
    assert res2.error is None
    assert "list" in res2.stdout  # return value unchanged
    assert res2.efficiency_hints and any(h["id"] == "reuse_var" for h in res2.efficiency_hints)
    assert "HINT" not in res2.stdout
    assert "reuse_var" not in res2.stdout


# ============================================================================
# Tier 2.1 — get_object_full_structure
# ============================================================================


def test_get_object_full_structure_fallback_no_index(bsl_env):
    """Без индекса → live XML fallback, _meta.index_used=False.
    ts_synonyms_available становится True ТОЛЬКО когда live реально заполнил
    tabular_sections с синонимами. Здесь у Catalog нет ТЧ — значит флаг False
    (нечего обогащать)."""
    cat_dir = bsl_env.path / "Catalogs" / "Тест" / "Ext"
    cat_dir.mkdir(parents=True)
    (cat_dir / "Catalog.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses" '
        'xmlns:v8="http://v8.1c.ru/8.1/data/core">'
        "<Catalog><Properties>"
        "<Name>Тест</Name>"
        "</Properties>"
        "<ChildObjects>"
        "<Attribute><Properties>"
        "<Name>Поле1</Name>"
        "</Properties></Attribute>"
        "</ChildObjects>"
        "</Catalog>"
        "</MetaDataObject>",
        encoding="utf-8",
    )
    # Need a BSL module for find_module to discover the object
    mod_dir = bsl_env.path / "Catalogs" / "Тест" / "Ext"
    (mod_dir / "ObjectModule.bsl").write_text("// stub\n", encoding="utf-8")

    # Re-create fixture with new files
    bsl, _ = _make_bsl_fixture_with_catalog(str(bsl_env.path))
    res = bsl["get_object_full_structure"]("Тест")
    assert res["object_name"] == "Тест"
    assert res["_meta"]["index_used"] is False
    # У объекта нет ТЧ → флаг False, нечего обогащать.
    assert res["_meta"]["ts_synonyms_available"] is False
    assert res["tabular_sections"] == []


def test_get_object_full_structure_fallback_ts_synonyms_filled(bsl_env):
    """Без индекса + объект с ТЧ → live XML заполняет TS со synonym,
    флаг ts_synonyms_available становится True."""
    doc_dir = bsl_env.path / "Documents" / "СТЧ" / "Ext"
    doc_dir.mkdir(parents=True)
    (doc_dir / "Document.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses" '
        'xmlns:v8="http://v8.1c.ru/8.1/data/core">'
        "<Document><Properties><Name>СТЧ</Name></Properties>"
        "<ChildObjects>"
        "<TabularSection><Properties>"
        "<Name>Список</Name>"
        "<Synonym>"
        "<v8:item><v8:lang>ru</v8:lang><v8:content>Список товаров</v8:content></v8:item>"
        "</Synonym>"
        "</Properties></TabularSection>"
        "</ChildObjects>"
        "</Document>"
        "</MetaDataObject>",
        encoding="utf-8",
    )
    bsl, _ = _make_bsl_fixture_with_catalog(str(bsl_env.path))
    res = bsl["get_object_full_structure"]("СТЧ")
    assert res["_meta"]["index_used"] is False
    # ТЧ есть и заполнена через live → флаг True
    assert res["_meta"]["ts_synonyms_available"] is True
    assert len(res["tabular_sections"]) == 1
    assert res["tabular_sections"][0]["synonym"] == "Список товаров"


def _make_bsl_fixture_with_catalog(tmpdir):
    """Create fixture WITHOUT calling _create_cf_fixture (which overwrites)."""
    from rlm_tools_bsl.helpers import make_helpers
    from rlm_tools_bsl.format_detector import detect_format
    from rlm_tools_bsl.bsl_helpers import make_bsl_helpers

    # Configuration.xml needed for format_detector
    if not (os.path.exists(os.path.join(tmpdir, "Configuration.xml"))):
        with open(os.path.join(tmpdir, "Configuration.xml"), "w") as f:
            f.write("<Configuration/>")
    helpers, resolve_safe = make_helpers(tmpdir)
    format_info = detect_format(tmpdir)
    bsl = make_bsl_helpers(
        base_path=tmpdir,
        resolve_safe=resolve_safe,
        read_file_fn=helpers["read_file"],
        grep_fn=helpers["grep"],
        glob_files_fn=helpers["glob_files"],
        format_info=format_info,
    )
    return bsl, helpers


def test_get_object_full_structure_object_not_found(bsl_env):
    res = bsl_env.bsl["get_object_full_structure"]("НетТакого")
    assert "error" in res or res["_meta"]["fallback_reason"] is not None


# ---- index_used semantics (Codex round 7 High) ----


class _StubIdxReader:
    """Минимальный stub для тестирования index_used семантики.
    Все методы возвращают [] (таблица существует, но строк нет) — имитирует
    "stale index" сценарий.
    """

    has_calls = False

    def get_object_attributes(self, **kwargs):
        return []

    def get_predefined_items(self, **kwargs):
        return []

    def search_objects(self, q, limit=20):
        return []

    def get_form_elements(self, **kwargs):
        return []

    def get_enum_values(self, name):
        return None  # таблица отсутствует — возвращаем None как реальный reader

    def get_all_modules(self):
        return []

    def get_register_movements(self, doc):
        return None


def _make_bsl_with_stub_idx(tmpdir, stub):
    """Build bsl helpers with the given stub idx_reader."""
    from rlm_tools_bsl.helpers import make_helpers
    from rlm_tools_bsl.format_detector import detect_format
    from rlm_tools_bsl.bsl_helpers import make_bsl_helpers

    if not os.path.exists(os.path.join(tmpdir, "Configuration.xml")):
        with open(os.path.join(tmpdir, "Configuration.xml"), "w") as f:
            f.write("<Configuration/>")
    helpers, resolve_safe = make_helpers(tmpdir)
    format_info = detect_format(tmpdir)
    bsl = make_bsl_helpers(
        base_path=tmpdir,
        resolve_safe=resolve_safe,
        read_file_fn=helpers["read_file"],
        grep_fn=helpers["grep"],
        glob_files_fn=helpers["glob_files"],
        format_info=format_info,
        idx_reader=stub,
    )
    return bsl


def test_index_used_false_when_index_empty_for_normal_object(bsl_env):
    """Stale/incomplete index: для категории, где атрибуты ОБЯЗАНЫ быть в индексе
    (Catalogs/Documents/...), пустой результат — это не успех index path.
    Должен быть _meta.index_used=False + fallback_reason='index_empty_for_object',
    данные подтянуты из live XML."""
    # Создаём CF Document с реквизитом
    doc_dir = bsl_env.path / "Documents" / "ТестДокИнд" / "Ext"
    doc_dir.mkdir(parents=True)
    (doc_dir / "Document.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses" '
        'xmlns:v8="http://v8.1c.ru/8.1/data/core">'
        "<Document><Properties><Name>ТестДокИнд</Name></Properties>"
        "<ChildObjects>"
        "<Attribute><Properties>"
        "<Name>Поле1</Name>"
        "<Type><v8:Type>xs:string</v8:Type></Type>"
        "</Properties></Attribute>"
        "</ChildObjects>"
        "</Document>"
        "</MetaDataObject>",
        encoding="utf-8",
    )

    bsl = _make_bsl_with_stub_idx(str(bsl_env.path), _StubIdxReader())
    res = bsl["get_object_full_structure"]("ТестДокИнд")

    assert "error" not in res
    # КЛЮЧЕВАЯ ПРОВЕРКА: index был доступен, но пустой → index_used=False
    assert res["_meta"]["index_used"] is False, (
        f"Stale index не сигнализирован: _meta={res['_meta']}, attributes={res['attributes']}"
    )
    assert res["_meta"]["fallback_reason"] == "index_empty_for_object"
    # Данные при этом подтянуты из live XML
    assert any(a["name"] == "Поле1" for a in res["attributes"])


def test_index_used_false_for_xml_only_category_filled_via_live(bsl_env):
    """XML-only категория (Enum/Constant/...) + пустой index → структура
    заполняется live XML, поэтому index_used=False (структура НЕ из индекса).
    Маркер fallback_reason специфичный, чтобы агент понимал что это норма."""
    enum_dir = bsl_env.path / "Enums" / "ТестЕнумИнд" / "Ext"
    enum_dir.mkdir(parents=True)
    (enum_dir / "Enum.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">'
        "<Enum><Properties>"
        "<Name>ТестЕнумИнд</Name>"
        "</Properties></Enum>"
        "</MetaDataObject>",
        encoding="utf-8",
    )
    bsl = _make_bsl_with_stub_idx(str(bsl_env.path), _StubIdxReader())
    res = bsl["get_object_full_structure"]("ТестЕнумИнд")

    assert "error" not in res
    # index_used=False означает «структура из live XML, не из индекса»
    assert res["_meta"]["index_used"] is False
    # Специфичная причина: это категория без attributes по природе, не stale index
    assert res["_meta"]["fallback_reason"] == "category_without_attributes_filled_via_live_xml"


def test_index_used_false_when_only_synonym_in_index_for_normal_category(bsl_env):
    """КЛЮЧЕВОЙ regression от Codex round 8: для нормальной категории
    (Catalogs/Documents/Registers/...) наличие ТОЛЬКО synonym в индексе
    БЕЗ записей в object_attributes — НЕ повод считать индекс достаточным.
    Должен сработать live XML fallback, index_used=False."""

    class _StubSynonymOnly(_StubIdxReader):
        """object_attributes пустой, но object_synonyms даёт строку."""

        def search_objects(self, q, limit=20):
            if q == "ТестКатСин":
                return [
                    {
                        "object_name": "ТестКатСин",
                        "category": "Catalogs",
                        "synonym": "Тестовый каталог",
                        "file": "Catalogs/ТестКатСин.xml",
                    }
                ]
            return []

    cat_dir = bsl_env.path / "Catalogs" / "ТестКатСин" / "Ext"
    cat_dir.mkdir(parents=True)
    (cat_dir / "Catalog.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses" '
        'xmlns:v8="http://v8.1c.ru/8.1/data/core">'
        "<Catalog><Properties><Name>ТестКатСин</Name></Properties>"
        "<ChildObjects>"
        "<Attribute><Properties>"
        "<Name>Поле1</Name>"
        "<Type><v8:Type>xs:string</v8:Type></Type>"
        "</Properties></Attribute>"
        "</ChildObjects>"
        "</Catalog>"
        "</MetaDataObject>",
        encoding="utf-8",
    )

    bsl = _make_bsl_with_stub_idx(str(bsl_env.path), _StubSynonymOnly())
    res = bsl["get_object_full_structure"]("ТестКатСин")

    assert "error" not in res
    # КЛЮЧЕВАЯ проверка: synonym из индекса не должен заглушить fallback
    assert res["_meta"]["index_used"] is False, (
        f"synonym-only из индекса ошибочно расценивается как успех index path: "
        f"_meta={res['_meta']}, attributes={res['attributes']}"
    )
    assert res["_meta"]["fallback_reason"] == "index_empty_for_object"
    # Структура должна быть подтянута из live XML
    assert any(a["name"] == "Поле1" for a in res["attributes"])
    # synonym из индекса не теряется (бонус)
    # — но даже если не подхватился, главное что структура live


def test_ts_synonyms_available_false_when_index_path_no_ts(bsl_env):
    """Index path без TS → ts_synonyms_available должен быть False
    (не True по факту 'мы вызывали live'). Это новое поведение после Codex round 9:
    флаг отражает реальность, а не возможность."""

    class _StubWithAttrs(_StubIdxReader):
        def get_object_attributes(self, **kwargs):
            return [
                {
                    "object_name": kwargs.get("object_name", "Х"),
                    "category": kwargs.get("category", "Catalogs"),
                    "attr_name": "Поле1",
                    "attr_synonym": "Поле 1",
                    "attr_type": ["String"],
                    "attr_kind": "attribute",
                    "ts_name": None,
                    "source_file": "test.xml",
                }
            ]

    cat_dir = bsl_env.path / "Catalogs" / "БезТЧ" / "Ext"
    cat_dir.mkdir(parents=True)
    (cat_dir / "Catalog.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">'
        "<Catalog><Properties><Name>БезТЧ</Name></Properties></Catalog>"
        "</MetaDataObject>",
        encoding="utf-8",
    )

    bsl = _make_bsl_with_stub_idx(str(bsl_env.path), _StubWithAttrs())
    res = bsl["get_object_full_structure"]("БезТЧ")
    assert res["_meta"]["index_used"] is True
    assert res["tabular_sections"] == []
    # Нет TS → флаг должен быть False (мы ничего не обогатили/заполнили)
    assert res["_meta"]["ts_synonyms_available"] is False


def test_ts_synonyms_enriched_from_live_in_index_path(bsl_env):
    """Index path дал TS со synonym=None (таблица object_attributes не хранит
    синоним самой ТЧ). _populate_from_live_xml должен ОБОГАТИТЬ существующие
    TS-записи синонимами из live XML по совпадению name. Флаг
    ts_synonyms_available должен стать True (мы реально обогатили)."""

    class _StubWithTs(_StubIdxReader):
        def get_object_attributes(self, **kwargs):
            return [
                {
                    "object_name": kwargs.get("object_name", "Х"),
                    "category": kwargs.get("category", "Documents"),
                    "attr_name": "Номенклатура",
                    "attr_synonym": "Номенклатура",
                    "attr_type": ["CatalogRef.X"],
                    "attr_kind": "ts_attribute",
                    "ts_name": "Товары",
                    "source_file": "test.xml",
                },
                {
                    "object_name": kwargs.get("object_name", "Х"),
                    "category": kwargs.get("category", "Documents"),
                    "attr_name": "Колво",
                    "attr_synonym": "Кол-во",
                    "attr_type": ["Number"],
                    "attr_kind": "ts_attribute",
                    "ts_name": "Товары",
                    "source_file": "test.xml",
                },
            ]

    # Live XML содержит синоним для ТЧ "Товары" — для обогащения
    doc_dir = bsl_env.path / "Documents" / "ДокТЧ" / "Ext"
    doc_dir.mkdir(parents=True)
    (doc_dir / "Document.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses" '
        'xmlns:v8="http://v8.1c.ru/8.1/data/core">'
        "<Document><Properties><Name>ДокТЧ</Name></Properties>"
        "<ChildObjects>"
        "<TabularSection><Properties>"
        "<Name>Товары</Name>"
        "<Synonym>"
        "<v8:item><v8:lang>ru</v8:lang><v8:content>Товары и услуги</v8:content></v8:item>"
        "</Synonym>"
        "</Properties></TabularSection>"
        "</ChildObjects>"
        "</Document>"
        "</MetaDataObject>",
        encoding="utf-8",
    )

    bsl = _make_bsl_with_stub_idx(str(bsl_env.path), _StubWithTs())
    res = bsl["get_object_full_structure"]("ДокТЧ")

    assert res["_meta"]["index_used"] is True
    assert len(res["tabular_sections"]) == 1
    ts = res["tabular_sections"][0]
    assert ts["name"] == "Товары"
    # КЛЮЧЕВАЯ ПРОВЕРКА: synonym обогащён из live XML
    assert ts["synonym"] == "Товары и услуги", f"TS synonym не обогащён из live: ts={ts}"
    # Флаг True — обогащение реально произошло
    assert res["_meta"]["ts_synonyms_available"] is True
    # Колонки из индекса — на месте
    col_names = [c["name"] for c in ts["columns"]]
    assert "Номенклатура" in col_names
    assert "Колво" in col_names


def test_ts_synonyms_available_false_when_live_xml_unreachable(bsl_env):
    """Index path дал TS со synonym=None, но live XML недоступен (например, файл
    отсутствует) → флаг ts_synonyms_available остаётся False (ничего не
    обогатили). Это честный сигнал агенту, что синонимов ТЧ в результате нет."""

    class _StubWithTs(_StubIdxReader):
        def get_object_attributes(self, **kwargs):
            return [
                {
                    "object_name": kwargs.get("object_name", "Х"),
                    "category": kwargs.get("category", "Documents"),
                    "attr_name": "Поле",
                    "attr_synonym": "Поле",
                    "attr_type": ["String"],
                    "attr_kind": "ts_attribute",
                    "ts_name": "ЕстьТЧ",
                    "source_file": "test.xml",
                },
            ]

    # XML-файла объекта НЕ создаём — live XML парсинг упадёт.
    bsl = _make_bsl_with_stub_idx(str(bsl_env.path), _StubWithTs())
    res = bsl["get_object_full_structure"]("ОтсутствуетXMLФайл")

    # TS из индекса есть
    assert len(res["tabular_sections"]) == 1
    # Но synonym не обогатили — live недоступен
    assert res["tabular_sections"][0]["synonym"] is None
    # Флаг False — это новое контрактное поведение
    assert res["_meta"]["ts_synonyms_available"] is False


def test_index_partially_enriched_from_live_xml(bsl_env):
    """Codex round 10 Medium: индекс дал часть структуры (attributes), но не дал
    tabular_sections — а live XML дозаполнил TS. По новой семантике index_used
    должен ПОНИЗИТЬСЯ до False, чтобы агент видел что источник смешанный.
    fallback_reason='index_partially_enriched_from_live_xml'."""

    class _StubAttrsOnly(_StubIdxReader):
        def get_object_attributes(self, **kwargs):
            return [
                {
                    "object_name": kwargs.get("object_name", "Х"),
                    "category": "Documents",
                    "attr_name": "Реквизит1",
                    "attr_synonym": "Реквизит 1",
                    "attr_type": ["String"],
                    "attr_kind": "attribute",
                    "ts_name": None,
                    "source_file": "test.xml",
                }
                # ВАЖНО: НЕТ ts_attribute строк — индекс не дал TS.
            ]

    # Live XML, наоборот, содержит И attribute, И TS:
    doc_dir = bsl_env.path / "Documents" / "ЧастДок" / "Ext"
    doc_dir.mkdir(parents=True)
    (doc_dir / "Document.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses" '
        'xmlns:v8="http://v8.1c.ru/8.1/data/core">'
        "<Document><Properties><Name>ЧастДок</Name></Properties>"
        "<ChildObjects>"
        "<Attribute><Properties>"
        "<Name>Реквизит1</Name>"
        "<Type><v8:Type>xs:string</v8:Type></Type>"
        "</Properties></Attribute>"
        "<TabularSection><Properties>"
        "<Name>Строки</Name>"
        "</Properties>"
        "<ChildObjects>"
        "<Attribute><Properties>"
        "<Name>СтрокаПоле</Name>"
        "<Type><v8:Type>xs:string</v8:Type></Type>"
        "</Properties></Attribute>"
        "</ChildObjects>"
        "</TabularSection>"
        "</ChildObjects>"
        "</Document>"
        "</MetaDataObject>",
        encoding="utf-8",
    )

    bsl = _make_bsl_with_stub_idx(str(bsl_env.path), _StubAttrsOnly())
    res = bsl["get_object_full_structure"]("ЧастДок")

    assert "error" not in res
    # Index дал attributes, live дозаполнил TS — это смешанный источник.
    assert res["_meta"]["index_used"] is False, (
        f"Смешанный источник не сигнализирован: _meta={res['_meta']}, "
        f"attributes={res['attributes']}, tabular_sections={res['tabular_sections']}"
    )
    assert res["_meta"]["fallback_reason"] == "index_partially_enriched_from_live_xml"
    # Атрибут из индекса присутствует
    assert any(a["name"] == "Реквизит1" for a in res["attributes"])
    # TS добавлена из live
    assert len(res["tabular_sections"]) == 1
    assert res["tabular_sections"][0]["name"] == "Строки"


def test_index_used_true_for_document_no_unconditional_posting_live_read(bsl_env):
    """Codex round 12 Medium/Low: на чистом index path (index_used=True без
    enrichment) хелпер НЕ должен делать безусловный live XML read для posting.
    Это противоречило бы контракту «без чтения live XML». Если агенту нужен
    posting — должен использовать find_register_movements (который делает
    live read корректно при пустом результате)."""

    class _StubFullIndexNoTSEnrichment(_StubIdxReader):
        """Index возвращает: attributes + ts_attribute с заполненным synonym
        (через специальный stub для тестов — реальный object_attributes synonym
        у TS не хранит, но мы имитируем сценарий "live не нужен")."""

        def get_object_attributes(self, **kwargs):
            return [
                {
                    "object_name": kwargs.get("object_name", "Х"),
                    "category": "Documents",
                    "attr_name": "Реквизит",
                    "attr_synonym": "",
                    "attr_type": ["String"],
                    "attr_kind": "attribute",
                    "ts_name": None,
                    "source_file": "test.xml",
                }
            ]

        def search_objects(self, q, limit=20):
            return [
                {
                    "object_name": q,
                    "category": "Documents",
                    "synonym": "Тестовый документ",
                    "file": f"Documents/{q}.xml",
                }
            ]

        def get_form_elements(self, **kwargs):
            return [{"form_name": "ФормаДокумента"}]

    # Документ на диске С posting=Allow, чтобы убедиться что мы НЕ читаем его.
    doc_dir = bsl_env.path / "Documents" / "ДокБезПарса" / "Ext"
    doc_dir.mkdir(parents=True)
    (doc_dir / "Document.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">'
        "<Document><Properties>"
        "<Name>ДокБезПарса</Name>"
        "<Posting>Allow</Posting>"  # есть posting в XML
        "</Properties></Document>"
        "</MetaDataObject>",
        encoding="utf-8",
    )

    bsl = _make_bsl_with_stub_idx(str(bsl_env.path), _StubFullIndexNoTSEnrichment())
    res = bsl["get_object_full_structure"]("ДокБезПарса")

    # Чистый index path (live не вызывался — synonym/forms заполнены, нет TS для enrichment)
    assert res["_meta"]["index_used"] is True
    assert res["_meta"]["fallback_reason"] is None
    # КЛЮЧЕВАЯ проверка: posting НЕ заполнен (live XML не читался) —
    # согласовано с контрактом «без чтения live XML на чистом index path».
    assert res.get("posting") is None, (
        f"Posting не должен заполняться на чистом index path (нарушает 'no live XML' контракт): "
        f"posting={res.get('posting')}"
    )


def test_posting_filled_when_live_xml_was_called_for_enrichment(bsl_env):
    """Если live XML был вызван по другим причинам (для synonym/forms/ts
    enrichment), posting подхватывается естественно — там же. Это не extra
    parse: live уже читается, posting — побочный продукт того же чтения."""

    class _StubMissingForms(_StubIdxReader):
        """Index дал attributes но НЕ дал forms → live будет вызван для forms,
        и заодно подхватит posting."""

        def get_object_attributes(self, **kwargs):
            return [
                {
                    "object_name": kwargs.get("object_name", "Х"),
                    "category": "Documents",
                    "attr_name": "Поле",
                    "attr_synonym": "",
                    "attr_type": ["String"],
                    "attr_kind": "attribute",
                    "ts_name": None,
                    "source_file": "test.xml",
                }
            ]

        def search_objects(self, q, limit=20):
            return [{"object_name": q, "category": "Documents", "synonym": "Док", "file": ""}]

        def get_form_elements(self, **kwargs):
            return []  # пусто → live будет вызван для forms

    doc_dir = bsl_env.path / "Documents" / "ДокСПарсом" / "Ext"
    doc_dir.mkdir(parents=True)
    (doc_dir / "Document.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">'
        "<Document><Properties>"
        "<Name>ДокСПарсом</Name>"
        "<Posting>Deny</Posting>"
        "</Properties></Document>"
        "</MetaDataObject>",
        encoding="utf-8",
    )

    bsl = _make_bsl_with_stub_idx(str(bsl_env.path), _StubMissingForms())
    res = bsl["get_object_full_structure"]("ДокСПарсом")

    # live был вызван (forms нужны были) → posting подхватился вместе с тем же XML-чтением
    assert res.get("posting") == "Deny"


def test_index_used_true_about_source_not_completeness(bsl_env):
    """Codex round 11 Medium: контракт `index_used=True` — об ИСТОЧНИКЕ, не о
    ПОЛНОТЕ. Если индекс дал synonym + forms + attributes (но не TS), и условия
    для вызова live XML не сработали (synonym заполнен, forms заполнены, у
    индексных TS нет, чтобы у них что-то обогащать), то live НЕ вызывается даже
    если в XML могут быть TS. Хелпер не делает второй парсинг XML 'ради
    проверки полноты' — это performance-tradeoff. index_used=True остаётся."""

    class _StubIndexFullExceptTS(_StubIdxReader):
        def get_object_attributes(self, **kwargs):
            # Только обычный реквизит, никаких ts_attribute строк → индекс
            # утверждает что у объекта НЕТ tabular sections.
            return [
                {
                    "object_name": kwargs.get("object_name", "Х"),
                    "category": "Documents",
                    "attr_name": "Реквизит1",
                    "attr_synonym": "Реквизит 1",
                    "attr_type": ["String"],
                    "attr_kind": "attribute",
                    "ts_name": None,
                    "source_file": "test.xml",
                }
            ]

        def search_objects(self, q, limit=20):
            # synonym есть → live для synonym НЕ нужен
            return [
                {
                    "object_name": q,
                    "category": "Documents",
                    "synonym": "Тестовый документ",
                    "file": f"Documents/{q}.xml",
                }
            ]

        def get_form_elements(self, **kwargs):
            # forms есть → live для forms НЕ нужен
            return [{"form_name": "ФормаДокумента"}]

    # ВАЖНО: live XML на диске тоже содержит TS, которой нет в индексе —
    # имитация stale index. Хелпер должен не заметить (по контракту).
    doc_dir = bsl_env.path / "Documents" / "СтейлДок" / "Ext"
    doc_dir.mkdir(parents=True)
    (doc_dir / "Document.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses" '
        'xmlns:v8="http://v8.1c.ru/8.1/data/core">'
        "<Document><Properties><Name>СтейлДок</Name></Properties>"
        "<ChildObjects>"
        "<Attribute><Properties>"
        "<Name>Реквизит1</Name>"
        "<Type><v8:Type>xs:string</v8:Type></Type>"
        "</Properties></Attribute>"
        # TabularSection НЕ попала в индекс, но есть в XML
        "<TabularSection><Properties>"
        "<Name>СтейлТЧ</Name>"
        "</Properties></TabularSection>"
        "</ChildObjects>"
        "</Document>"
        "</MetaDataObject>",
        encoding="utf-8",
    )

    bsl = _make_bsl_with_stub_idx(str(bsl_env.path), _StubIndexFullExceptTS())
    res = bsl["get_object_full_structure"]("СтейлДок")

    # КЛЮЧЕВОЙ контракт: index_used=True означает ИСТОЧНИК, не полноту.
    # Скрытая в XML TS не попала в результат — это by design, без обращения к XML.
    assert res["_meta"]["index_used"] is True, (
        f"Контракт смягчён: index_used=True должен оставаться когда live "
        f"не вызывался. _meta={res['_meta']}, ts={res['tabular_sections']}"
    )
    assert res["_meta"]["fallback_reason"] is None
    # tabular_sections пустые — индекс «утверждает», что их нет.
    # В XML они есть, но мы их не подтянули — performance-tradeoff.
    assert res["tabular_sections"] == []
    # Атрибут из индекса присутствует
    assert any(a["name"] == "Реквизит1" for a in res["attributes"])
    # synonym/forms из индекса
    assert res["synonym"] == "Тестовый документ"
    assert res["forms"] == ["ФормаДокумента"]


def test_index_used_true_when_live_only_enriches_synonym(bsl_env):
    """Index path дал ВСЕ структурные секции, live вызывался только для
    обогащения synonym у TS (не дозаполнения). index_used=True остаётся,
    fallback_reason пустой / None — это чистый index path."""

    class _StubFullStructure(_StubIdxReader):
        def get_object_attributes(self, **kwargs):
            return [
                {
                    "object_name": kwargs.get("object_name", "Х"),
                    "category": "Documents",
                    "attr_name": "Поле",
                    "attr_synonym": "Поле",
                    "attr_type": ["String"],
                    "attr_kind": "ts_attribute",
                    "ts_name": "Список",
                    "source_file": "test.xml",
                }
            ]

    doc_dir = bsl_env.path / "Documents" / "ЧистДок" / "Ext"
    doc_dir.mkdir(parents=True)
    (doc_dir / "Document.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses" '
        'xmlns:v8="http://v8.1c.ru/8.1/data/core">'
        "<Document><Properties><Name>ЧистДок</Name></Properties>"
        "<ChildObjects>"
        "<TabularSection><Properties>"
        "<Name>Список</Name>"
        "<Synonym>"
        "<v8:item><v8:lang>ru</v8:lang><v8:content>Список услуг</v8:content></v8:item>"
        "</Synonym>"
        "</Properties></TabularSection>"
        "</ChildObjects>"
        "</Document>"
        "</MetaDataObject>",
        encoding="utf-8",
    )

    bsl = _make_bsl_with_stub_idx(str(bsl_env.path), _StubFullStructure())
    res = bsl["get_object_full_structure"]("ЧистДок")

    # Индекс дал TS со всеми колонками, live ТОЛЬКО обогатил synonym ТЧ.
    # Это enrichment, а не дозаполнение — index_used должен остаться True.
    assert res["_meta"]["index_used"] is True
    assert res["_meta"]["ts_synonyms_available"] is True
    assert res["tabular_sections"][0]["synonym"] == "Список услуг"
    # Колонки из индекса не задвоились
    assert len(res["tabular_sections"][0]["columns"]) == 1
    # Приватный маркер не утёк в API
    assert "_live_filled_structural" not in res["_meta"]


def test_ts_synonyms_available_false_when_ts_filled_without_synonyms(bsl_env):
    """Codex round 10 Low: live fallback заполнил TS, но у всех TS synonym
    отсутствует в XML → флаг ts_synonyms_available должен быть False
    (флаг означает «есть реальные synonyms», а не «мы парсили live»)."""
    doc_dir = bsl_env.path / "Documents" / "ТЧБезСинонимов" / "Ext"
    doc_dir.mkdir(parents=True)
    (doc_dir / "Document.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses" '
        'xmlns:v8="http://v8.1c.ru/8.1/data/core">'
        "<Document><Properties><Name>ТЧБезСинонимов</Name></Properties>"
        "<ChildObjects>"
        # TabularSection БЕЗ <Synonym> — synonym будет ""
        "<TabularSection><Properties>"
        "<Name>ПростоТЧ</Name>"
        "</Properties>"
        "<ChildObjects>"
        "<Attribute><Properties>"
        "<Name>Поле</Name>"
        "<Type><v8:Type>xs:string</v8:Type></Type>"
        "</Properties></Attribute>"
        "</ChildObjects>"
        "</TabularSection>"
        "</ChildObjects>"
        "</Document>"
        "</MetaDataObject>",
        encoding="utf-8",
    )
    bsl, _ = _make_bsl_fixture_with_catalog(str(bsl_env.path))
    res = bsl["get_object_full_structure"]("ТЧБезСинонимов")

    assert res["_meta"]["index_used"] is False
    # ТЧ заполнена через live
    assert len(res["tabular_sections"]) == 1
    # Но synonym=None у этой TS → флаг False (не «доступны»)
    assert res["tabular_sections"][0]["synonym"] is None
    assert res["_meta"]["ts_synonyms_available"] is False


def test_index_used_true_when_index_returns_data(bsl_env):
    """Sanity: если индекс реально дал данные → index_used=True."""

    class _StubWithAttrs(_StubIdxReader):
        def get_object_attributes(self, **kwargs):
            return [
                {
                    "object_name": kwargs.get("object_name", "Х"),
                    "category": kwargs.get("category", "Catalogs"),
                    "attr_name": "Поле1",
                    "attr_synonym": "Поле 1",
                    "attr_type": ["String"],
                    "attr_kind": "attribute",
                    "ts_name": None,
                    "source_file": "test.xml",
                }
            ]

    cat_dir = bsl_env.path / "Catalogs" / "ТестКатИнд" / "Ext"
    cat_dir.mkdir(parents=True)
    (cat_dir / "Catalog.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">'
        "<Catalog><Properties><Name>ТестКатИнд</Name></Properties></Catalog>"
        "</MetaDataObject>",
        encoding="utf-8",
    )

    bsl = _make_bsl_with_stub_idx(str(bsl_env.path), _StubWithAttrs())
    res = bsl["get_object_full_structure"]("ТестКатИнд")
    assert "error" not in res
    assert res["_meta"]["index_used"] is True
    assert any(a["name"] == "Поле1" for a in res["attributes"])


def test_get_object_full_structure_xml_only_enum_resolves(bsl_env):
    """Объект без BSL-модуля (например, Enum) должен резолвиться через live glob fallback,
    а не падать в object_not_found из-за того что find_module() не нашёл BSL-модуль."""
    # Создаём CF-формат Enum без какого-либо BSL-модуля — только XML
    enum_dir = bsl_env.path / "Enums" / "СтатусыПисьма" / "Ext"
    enum_dir.mkdir(parents=True)
    (enum_dir / "Enum.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses" '
        'xmlns:v8="http://v8.1c.ru/8.1/data/core">'
        "<Enum><Properties>"
        "<Name>СтатусыПисьма</Name>"
        "</Properties></Enum>"
        "</MetaDataObject>",
        encoding="utf-8",
    )

    bsl, _ = _make_bsl_fixture_with_catalog(str(bsl_env.path))
    res = bsl["get_object_full_structure"]("СтатусыПисьма")
    # Главное — НЕ object_not_found
    assert "error" not in res, f"Enum-only объект не нашёлся: {res}"
    assert res["object_name"] == "СтатусыПисьма"
    assert res["category"] == "Enums"


def test_get_object_full_structure_xml_only_constant_resolves(bsl_env):
    """Constant — ещё один XML-only тип без BSL-модуля по умолчанию."""
    const_dir = bsl_env.path / "Constants" / "ВерсияДанных" / "Ext"
    const_dir.mkdir(parents=True)
    (const_dir / "Constant.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">'
        "<Constant><Properties>"
        "<Name>ВерсияДанных</Name>"
        "</Properties></Constant>"
        "</MetaDataObject>",
        encoding="utf-8",
    )

    bsl, _ = _make_bsl_fixture_with_catalog(str(bsl_env.path))
    res = bsl["get_object_full_structure"]("ВерсияДанных")
    assert "error" not in res
    assert res["object_name"] == "ВерсияДанных"
    assert res["category"] == "Constants"


def test_get_object_full_structure_enum_dot_prefix_recognized(bsl_env):
    """Тип атрибута 'Enum.X' (канонизированный формат) должен распознаваться
    как enum-ref и попадать в enum_values_for_typed_refs (если find_enum_values
    нашёл его) — наравне с 'EnumRef.X' и 'ПеречислениеСсылка.X'.

    Тут проверяем (а) что атрибут с типом 'Enum.X' не теряется в результате,
    (б) что для матчинга enum_values используется sibling-only layout, который
    реально работает в find_enum_values (glob по имени файла = имени объекта).
    """
    # Sibling-only CF layout — имя файла совпадает с именем объекта,
    # это путь по которому find_enum_values реально его найдёт через
    # glob '**/Enums/**/*ТестЕнум*.xml'.
    enums_dir = bsl_env.path / "Enums" / "ТестЕнум"
    enums_dir.mkdir(parents=True)
    (enums_dir / "ТестЕнум.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses" '
        'xmlns:v8="http://v8.1c.ru/8.1/data/core">'
        "<Enum><Properties>"
        "<Name>ТестЕнум</Name>"
        "</Properties>"
        "<ChildObjects>"
        "<EnumValue><Properties><Name>Значение1</Name></Properties></EnumValue>"
        "</ChildObjects>"
        "</Enum>"
        "</MetaDataObject>",
        encoding="utf-8",
    )

    # Документ с реквизитом типа Enum.ТестЕнум (канонизированный формат)
    doc_dir = bsl_env.path / "Documents" / "ДокЕнум" / "Ext"
    doc_dir.mkdir(parents=True)
    (doc_dir / "Document.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses" '
        'xmlns:v8="http://v8.1c.ru/8.1/data/core">'
        "<Document><Properties><Name>ДокЕнум</Name></Properties>"
        "<ChildObjects>"
        "<Attribute><Properties>"
        "<Name>Статус</Name>"
        "<Type><v8:Type>Enum.ТестЕнум</v8:Type></Type>"
        "</Properties></Attribute>"
        "</ChildObjects>"
        "</Document>"
        "</MetaDataObject>",
        encoding="utf-8",
    )

    bsl, _ = _make_bsl_fixture_with_catalog(str(bsl_env.path))
    res = bsl["get_object_full_structure"]("ДокЕнум")

    assert "error" not in res
    # Базовая проверка — тип Enum.X сохраняется в attributes
    types_seen = []
    for a in res["attributes"]:
        types_seen.extend(a.get("type", []))
    assert "Enum.ТестЕнум" in types_seen, f"Тип Enum.ТестЕнум потерян при парсинге attributes: {res['attributes']}"

    # Главная проверка — Enum.X попал в enum_values_for_typed_refs
    # (если find_enum_values нашёл объект)
    assert "Enum.ТестЕнум" in res["enum_values_for_typed_refs"], (
        f"Префикс 'Enum.' не распознан как enum-ref. Ключи: {list(res['enum_values_for_typed_refs'].keys())}"
    )
    values = res["enum_values_for_typed_refs"]["Enum.ТестЕнум"]
    assert any(v["name"] == "Значение1" for v in values)


# ============================================================================
# Tier 2.2 — find_call_hierarchy
# ============================================================================


def test_find_call_hierarchy_callees_returns_error_dict(bsl_env):
    """direction='callees' → structured error-dict, NOT NotImplementedError."""
    res = bsl_env.bsl["find_call_hierarchy"]("ProcName", direction="callees")
    assert "error" in res
    assert "hint" in res
    assert res["supported_directions"] == ["callers"]


def test_find_call_hierarchy_both_returns_error_dict(bsl_env):
    res = bsl_env.bsl["find_call_hierarchy"]("ProcName", direction="both")
    assert "error" in res


def test_find_call_hierarchy_unknown_direction(bsl_env):
    res = bsl_env.bsl["find_call_hierarchy"]("ProcName", direction="xyz")
    assert "error" in res


def test_find_call_hierarchy_callers_returns_tree_structure(bsl_env):
    """Without index → empty tree but valid structure with caller_name field."""
    res = bsl_env.bsl["find_call_hierarchy"]("ЗаполнитьДанные", direction="callers", depth=2)
    assert "error" not in res
    assert res["root"] == "ЗаполнитьДанные"
    assert res["direction"] == "callers"
    assert res["depth"] == 2
    assert isinstance(res["tree"], list)
    # If callers found, they have caller_name (S.2.2 contract)
    for node in res["tree"]:
        for c in node["callers"]:
            assert "caller_name" in c
            assert "module_path" in c
            assert "category" in c
            assert "object_name" in c
            assert "line" in c
            assert "is_export" in c
            assert "level" in c


def test_find_call_hierarchy_depth_clamped(bsl_env):
    """Depth >3 clamped to 3, depth <1 clamped to 1."""
    res = bsl_env.bsl["find_call_hierarchy"]("X", depth=10)
    assert res["depth"] == 3
    res2 = bsl_env.bsl["find_call_hierarchy"]("X", depth=0)
    assert res2["depth"] == 1


def test_find_call_hierarchy_breaks_cycle(bsl_env):
    """Cycle A → B → A must NOT cause A to be revisited at deeper levels.

    Without the fix, cycle protection on (name, level) treated (A, 1) and (A, 3)
    as different entries and produced duplicate A-callers nodes. Now visited_names
    is keyed only by name (lower) — A is processed exactly once.
    """
    # Создаём CommonModules/Циклический/Ext/Module.bsl с парой взаимных вызовов A↔B
    cyc_dir = bsl_env.path / "CommonModules" / "Циклический" / "Ext"
    cyc_dir.mkdir(parents=True)
    bsl = "Процедура A() Экспорт\n    B();\nКонецПроцедуры\n\nПроцедура B() Экспорт\n    A();\nКонецПроцедуры\n"
    (cyc_dir / "Module.bsl").write_text(bsl, encoding="utf-8")

    res = bsl_env.bsl["find_call_hierarchy"]("A", direction="callers", depth=3)
    assert "error" not in res

    # Цикл должен быть отсечён по имени: A появляется в tree максимум один раз
    # как target (ключ узла).
    target_names_lower = [node["name"].lower() for node in res["tree"]]
    assert target_names_lower.count("a") <= 1, (
        f"A appears as a target {target_names_lower.count('a')} times — cycle protection broken"
    )
    # Аналогично B.
    assert target_names_lower.count("b") <= 1


def test_find_call_hierarchy_returns_truncated_targets_field(bsl_env):
    """Result always contains 'truncated_targets' (empty when no truncation)."""
    res = bsl_env.bsl["find_call_hierarchy"]("X", direction="callers", depth=2)
    assert "truncated_targets" in res
    assert isinstance(res["truncated_targets"], list)


def test_find_call_hierarchy_visited_counts_unique_names(bsl_env):
    """visited counts unique names (because cycle protection is by name)."""
    cyc_dir = bsl_env.path / "CommonModules" / "Циклический2" / "Ext"
    cyc_dir.mkdir(parents=True)
    bsl = "Процедура C() Экспорт\n    D();\nКонецПроцедуры\n\nПроцедура D() Экспорт\n    C();\nКонецПроцедуры\n"
    (cyc_dir / "Module.bsl").write_text(bsl, encoding="utf-8")

    res = bsl_env.bsl["find_call_hierarchy"]("C", direction="callers", depth=3)
    # C visited 1 time + D visited 1 time = 2 unique names.
    # Без фикса protection (по name+level) счётчик мог бы вырасти до 3+ из-за повторного захода в C.
    assert res["visited"] <= 2


# ============================================================================
# G.5b — bsl_help bridge to _BUSINESS_RECIPES
# ============================================================================


def test_bsl_help_bridge_movements_alias(bsl_env):
    """help('как проводится документ') matches alias → 'проведение' recipe via bridge."""
    res = bsl_env.bsl["help"]("как проводится документ")
    # Either bridge result or helper recipe — both contain useful business info.
    # Bridge format: starts with "BUSINESS RECIPE: проведение"
    assert "проведение" in res.lower() or "find_register_movements" in res


def test_bsl_help_bridge_unknown_falls_back(bsl_env):
    """Unknown task → fallback to all-recipes listing."""
    res = bsl_env.bsl["help"]("xyz_не_существует_никак")
    assert "Available recipes" in res


def test_bsl_help_helper_priority_over_bridge(bsl_env):
    """When kw matches a helper, helper-recipe wins over bridge."""
    res = bsl_env.bsl["help"]("exports")
    assert "find_exports" in res


def test_bsl_help_exact_keyword_wins_over_substring(bsl_env):
    """Regression от e2e #11: help('иерархия вызовов') должен возвращать
    find_call_hierarchy (точное совпадение с его keyword 'иерархия вызовов'),
    а НЕ find_callers_context (где substring 'вызов' матчится в keyword).
    Двухпроходная стратегия: exact keyword match — Pass 1, substring — Pass 2."""
    res = bsl_env.bsl["help"]("иерархия вызовов")
    assert "find_call_hierarchy" in res, (
        f"Точное совпадение с keyword 'иерархия вызовов' должно вернуть find_call_hierarchy, "
        f"а не другой хелпер. Получили:\n{res[:200]}"
    )


def test_bsl_help_exact_call_hierarchy_kw(bsl_env):
    """help('call hierarchy') — точное keyword find_call_hierarchy."""
    res = bsl_env.bsl["help"]("call hierarchy")
    assert "find_call_hierarchy" in res


def test_bsl_help_substring_still_works(bsl_env):
    """Pass 2 (substring) продолжает работать для запросов без точного match.
    Substring 'кто вызывает' есть в kw у find_callers_context, и при отсутствии
    точных совпадений возвращается его recipe."""
    res = bsl_env.bsl["help"]("кто вызывает обработчик")
    assert "find_callers_context" in res or "find_callers" in res


# ============================================================================
# G.6 — find_callers compact-mode positioning
# ============================================================================


def test_find_callers_sig_describes_compact_first_page(bsl_env):
    """find_callers sig presents it as 'compact first page' (not 'same search /
    same performance' — это слишком сильно: max_files=20 default, без has_more)."""
    reg = bsl_env.bsl["_registry"]
    assert "find_callers" in reg
    sig = reg["find_callers"]["sig"]
    # Не помечено DEPRECATED — это полноценный режим.
    assert "DEPRECATED" not in sig
    # Явно указывает на отношение к find_callers_context (для агента).
    assert "find_callers_context" in sig


def test_find_callers_recipe_explains_when_to_use(bsl_env):
    """find_callers recipe explains compact-first-page tradeoff vs find_callers_context."""
    reg = bsl_env.bsl["_registry"]
    recipe = reg["find_callers"]["recipe"]
    assert "find_callers_context" in recipe
    assert "DEPRECATED" not in recipe
    # Recipe должен явно упоминать tradeoff — лимит/пагинацию,
    # чтобы агент понимал что это first page а не полный список.
    assert "limit" in recipe.lower() or "пагинац" in recipe.lower() or "max_files" in recipe.lower()


# ============================================================================
# G.9.3 — build_helpers_table regression
# ============================================================================


def test_build_helpers_table_includes_v1_10_helpers(bsl_env):
    """Regression: build_helpers_table for the registry must contain new v1.10.0 helpers."""
    from rlm_tools_bsl.bsl_knowledge import build_helpers_table

    reg = bsl_env.bsl["_registry"]
    text = build_helpers_table(reg)
    # New aggregating helpers
    assert "get_object_full_structure" in text
    assert "find_call_hierarchy" in text
    # Existing helpers must still be there
    assert "find_module" in text
    assert "find_callers_context" in text
    assert "parse_object_xml" in text


def test_recipe_aliases_all_point_to_existing_domains():
    from rlm_tools_bsl.bsl_knowledge import _BUSINESS_RECIPES, _RECIPE_ALIASES

    for alias, dom in _RECIPE_ALIASES.items():
        assert dom in _BUSINESS_RECIPES, f"alias '{alias}' points to missing domain '{dom}'"


def test_business_recipes_v1_10_count():
    from rlm_tools_bsl.bsl_knowledge import _BUSINESS_RECIPES

    # v1.10.0: 9 base + перечисления + ввод на основании + структура объекта = 12
    # v1.11.0+ adds 'иерархия вызовов' + 'расширения' → 14
    # v1.19.0+ adds 'достижимость' + 'путь данных' → 16
    assert len(_BUSINESS_RECIPES) == 17
    for new in (
        "перечисления",
        "ввод на основании",
        "структура объекта",
        "иерархия вызовов",
        "расширения",
        "достижимость",
        "путь данных",
        "история",
    ):
        assert new in _BUSINESS_RECIPES


def test_recipe_snippets_compile_as_top_level_exec(bsl_env):
    """No top-level `return` in code_hints / recipes (sandbox runs via exec)."""
    reg = bsl_env.bsl["_registry"]
    for name, entry in reg.items():
        recipe = entry.get("recipe", "")
        if not recipe:
            continue
        # Strip non-code prefix (header line followed by indented lines)
        # We just check that any line with `    return` won't slip through.
        for line in recipe.splitlines():
            stripped = line.strip()
            assert not stripped.startswith("return"), f"{name}: top-level return in recipe: {line!r}"


# ============================================================================
# Post-e2e (Тест ДО3, 2026-05-02) bug fixes
# ============================================================================


def test_resolve_with_substring_collision_prefers_exact_match_from_other_source(bsl_env):
    """BUG-4: substring close-match в object_attributes НЕ должен блокировать
    exact-match в search_objects (другом источнике каскада).

    Регрессия test02 на Тест ДО3: get_object_full_structure('Согласование')
    возвращал реквизиты регистра 'тст_СогласованиеЗаявокСБ' (substring match),
    хотя в object_synonyms был exact БизнесПроцесс 'Согласование'.
    """

    class _StubSubstringCollision(_StubIdxReader):
        def get_object_attributes(self, **kwargs):
            obj = (kwargs.get("object_name") or "").lower()
            cat = kwargs.get("category") or ""
            # Resolver call (без category): substring close — регистр-омоним
            if obj == "согласование" and not cat:
                return [
                    {
                        "object_name": "тст_СогласованиеЗаявокСБ",
                        "category": "InformationRegisters",
                        "attr_name": "ЗаявкаСБ",
                        "attr_synonym": "Заявка СБ",
                        "attr_type": ["DocumentRef.X"],
                        "attr_kind": "dimension",
                        "ts_name": None,
                        "source_file": "InformationRegisters/тст_СогласованиеЗаявокСБ.xml",
                    }
                ]
            # Index path call для разрешённого БП — пусто (БП без атрибутов в индексе)
            if cat == "BusinessProcesses":
                return []
            return []

        def search_objects(self, q, limit=20):
            if q == "Согласование":
                return [
                    {
                        "object_name": "Согласование",
                        "category": "BusinessProcesses",
                        "synonym": "Согласование",
                        "file": "BusinessProcesses/Согласование.xml",
                    }
                ]
            return []

    # Live XML fixture для БП Согласование (CF layout) — без чужих реквизитов
    bp_dir = bsl_env.path / "BusinessProcesses" / "Согласование" / "Ext"
    bp_dir.mkdir(parents=True)
    (bp_dir / "BusinessProcess.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses" '
        'xmlns:v8="http://v8.1c.ru/8.1/data/core">'
        "<BusinessProcess><Properties><Name>Согласование</Name></Properties>"
        "<ChildObjects>"
        "<Attribute><Properties>"
        "<Name>Согласовать</Name>"
        "<Type><v8:Type>xs:boolean</v8:Type></Type>"
        "</Properties></Attribute>"
        "</ChildObjects>"
        "</BusinessProcess>"
        "</MetaDataObject>",
        encoding="utf-8",
    )

    bsl = _make_bsl_with_stub_idx(str(bsl_env.path), _StubSubstringCollision())
    res = bsl["get_object_full_structure"]("Согласование")

    assert "error" not in res
    # КЛЮЧЕВОЕ: разрешён БП, не регистр-омоним
    assert res["object_name"] == "Согласование", (
        f"resolver вернул неправильный объект: {res['object_name']} (ожидался БП 'Согласование')"
    )
    assert res["category"] == "BusinessProcesses"
    # Структура чужого регистра не должна попасть в результат
    attr_names = [a["name"] for a in res["attributes"]]
    assert "ЗаявкаСБ" not in attr_names, f"структура регистра-омонима утекла в результат БП: {attr_names}"
    # У БП нет dimensions (это контраст с регистром)
    assert res["dimensions"] == []


def test_resolve_close_match_used_only_when_no_exact_anywhere(bsl_env):
    """BUG-4: если ни один источник не дал exact-match — fallback на close-match
    из первого непустого источника (Pass 3). Сохраняет старое поведение.
    """

    class _StubOnlyCloseInAttrs(_StubIdxReader):
        def get_object_attributes(self, **kwargs):
            obj = (kwargs.get("object_name") or "").lower()
            cat = kwargs.get("category") or ""
            # Resolver call (substring close) — отдаём близкое имя
            if obj == "контрагент" and not cat:
                return [
                    {
                        "object_name": "Контрагенты",
                        "category": "Catalogs",
                        "attr_name": "ИНН",
                        "attr_synonym": "ИНН",
                        "attr_type": ["String"],
                        "attr_kind": "attribute",
                        "ts_name": None,
                        "source_file": "Catalogs/Контрагенты.xml",
                    }
                ]
            # Index path call для разрешённого имени Контрагенты
            if cat == "Catalogs":
                return [
                    {
                        "object_name": "Контрагенты",
                        "category": "Catalogs",
                        "attr_name": "ИНН",
                        "attr_synonym": "ИНН",
                        "attr_type": ["String"],
                        "attr_kind": "attribute",
                        "ts_name": None,
                        "source_file": "Catalogs/Контрагенты.xml",
                    }
                ]
            return []

    cat_dir = bsl_env.path / "Catalogs" / "Контрагенты" / "Ext"
    cat_dir.mkdir(parents=True)
    (cat_dir / "Catalog.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">'
        "<Catalog><Properties><Name>Контрагенты</Name></Properties></Catalog>"
        "</MetaDataObject>",
        encoding="utf-8",
    )

    bsl = _make_bsl_with_stub_idx(str(bsl_env.path), _StubOnlyCloseInAttrs())
    res = bsl["get_object_full_structure"]("Контрагент")  # substring, не exact

    assert "error" not in res
    # close-match из object_attributes (Pass 3 fallback)
    assert res["object_name"] == "Контрагенты"
    assert res["category"] == "Catalogs"


def test_resolve_close_match_falls_back_to_enum_values(bsl_env):
    """BUG-4: get_enum_values сам substring-based; если все остальные источники
    пустые, его непустой результат должен сработать как close-match (Pass 3).
    """

    class _StubOnlyEnumClose(_StubIdxReader):
        def get_enum_values(self, name):
            if name.lower() == "статус":
                return {
                    "name": "СтатусыЗаказов",
                    "synonym": "Статусы заказов",
                    "values": [{"name": "Открыт", "synonym": ""}, {"name": "Закрыт", "synonym": ""}],
                }
            return None

    enum_dir = bsl_env.path / "Enums" / "СтатусыЗаказов" / "Ext"
    enum_dir.mkdir(parents=True)
    (enum_dir / "Enum.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">'
        "<Enum><Properties>"
        "<Name>СтатусыЗаказов</Name>"
        "</Properties></Enum>"
        "</MetaDataObject>",
        encoding="utf-8",
    )

    bsl = _make_bsl_with_stub_idx(str(bsl_env.path), _StubOnlyEnumClose())
    res = bsl["get_object_full_structure"]("Статус")

    assert "error" not in res
    assert res["object_name"] == "СтатусыЗаказов"
    assert res["category"] == "Enums"


def test_sandbox_hint_for_key_error_attr_name_in_get_object_full_structure():
    """BUG-5: при KeyError + 'get_object_full_structure' в коде — Sandbox должен
    подсказать правильные ключи (агент применил паттерн find_attributes к новому
    хелперу, потерял calls на retry).
    """
    from rlm_tools_bsl.sandbox import Sandbox

    error = "Traceback (most recent call last):\n  File ...\nKeyError: 'attr_name'\n"
    code = (
        "s = get_object_full_structure('РеализацияТоваровУслуг')\nfor a in s['attributes']:\n    print(a['attr_name'])"
    )

    out = Sandbox._add_error_hints(error, code)

    assert "name/synonym/type" in out, f"hint про ключи не выдан: {out!r}"
    assert "find_attributes" in out
    # Регистры тоже упоминаются (важно для BUG-5c контекста)
    assert "dimensions" in out and "resources" in out


def test_sandbox_hint_skipped_when_no_get_object_full_structure_in_code():
    """BUG-5: hint про ключи НЕ должен лезть в чужой KeyError, если в коде
    нет вызова get_object_full_structure.
    """
    from rlm_tools_bsl.sandbox import Sandbox

    error = "KeyError: 'attr_name'"
    code = "for a in find_attributes(object_name='X'):\n    print(a['attr_name'])"

    out = Sandbox._add_error_hints(error, code)

    assert "get_object_full_structure возвращает ключи" not in out


def test_analyze_document_flow_non_postable_top_level_hint(bsl_env):
    """BUG-6: для непроводимого документа (Posting=Deny) analyze_document_flow
    добавляет top-level is_postable=False + hint, а также секции based_on / print_forms.
    Контракт register_movements сохранён (ключ присутствует).
    """
    # CF Document с Posting=Deny
    doc_dir = bsl_env.path / "Documents" / "ВходящееПисьмо" / "Ext"
    doc_dir.mkdir(parents=True)
    (doc_dir / "Document.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses" '
        'xmlns:v8="http://v8.1c.ru/8.1/data/core">'
        "<Document><Properties>"
        "<Name>ВходящееПисьмо</Name>"
        "<Posting>Deny</Posting>"
        "</Properties></Document>"
        "</MetaDataObject>",
        encoding="utf-8",
    )

    res = bsl_env.bsl["analyze_document_flow"]("ВходящееПисьмо")

    # Контракт сохранён — register_movements присутствует
    assert "register_movements" in res
    # Новые секции
    assert "based_on" in res
    assert "print_forms" in res
    # Top-level hint — главный сигнал для агента
    assert res.get("is_postable") is False, f"is_postable не выставлен на верхнем уровне: {res.get('is_postable')}"
    assert "hint" in res
    assert "event_subscriptions" in res["hint"]


def test_get_object_full_structure_recipe_warns_about_keys_and_registers(bsl_env):
    """BUG-5b/5c: recipe явно предупреждает о различии ключей с find_attributes
    и содержит пример для регистров (dimensions/resources)."""
    reg = bsl_env.bsl["_registry"]
    recipe = reg["get_object_full_structure"]["recipe"]

    # BUG-5b: предупреждение про различие ключей
    assert "attr_name" in recipe, "recipe не упоминает attr_name (контраст с find_attributes)"
    assert "find_attributes" in recipe
    # Контракт нового хелпера явно перечислен
    assert "name" in recipe and "synonym" in recipe and "type" in recipe

    # BUG-5c: пример для регистров
    assert "dimensions" in recipe
    assert "resources" in recipe


# ============================================================================
# Round 3 (Тест ДО3 fix run, 2026-05-02) — BUG-8 / BUG-9
# ============================================================================


_EVENT_SUB_BEFORE_WRITE = """\
<?xml version="1.0" encoding="UTF-8"?>
<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"
    xmlns:v8="http://v8.1c.ru/8.1/data/core"
    xmlns:cfg="http://v8.1c.ru/8.1/data/enterprise/current-config">
<EventSubscription uuid="aaaa1111-0000-0000-0000-000000000001">
  <Properties>
    <Name>ПодпискаДоЗаписи</Name>
    <Source>
      <v8:Type>cfg:DocumentObject.МойДок</v8:Type>
    </Source>
    <Event>BeforeWrite</Event>
    <Handler>CommonModule.МойМодуль.Обработчик1</Handler>
  </Properties>
</EventSubscription>
</MetaDataObject>
"""

_EVENT_SUB_ON_EXECUTE = """\
<?xml version="1.0" encoding="UTF-8"?>
<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"
    xmlns:v8="http://v8.1c.ru/8.1/data/core"
    xmlns:cfg="http://v8.1c.ru/8.1/data/enterprise/current-config">
<EventSubscription uuid="bbbb2222-0000-0000-0000-000000000002">
  <Properties>
    <Name>ПодпискаПриВыполнении</Name>
    <Source>
      <v8:Type>cfg:DocumentObject.МойДок</v8:Type>
    </Source>
    <Event>OnExecute</Event>
    <Handler>CommonModule.МойМодуль.Обработчик2</Handler>
  </Properties>
</EventSubscription>
</MetaDataObject>
"""


def _make_two_subs_fixture(tmp_path):
    """Fixture с двумя EventSubscription (BeforeWrite + OnExecute) без индекса.
    Возвращает bsl helpers (live XML fallback path для find_event_subscriptions).
    """
    from rlm_tools_bsl.helpers import make_helpers
    from rlm_tools_bsl.format_detector import detect_format
    from rlm_tools_bsl.bsl_helpers import make_bsl_helpers

    sub_dir = tmp_path / "EventSubscriptions"
    sub_dir.mkdir(parents=True, exist_ok=True)
    (sub_dir / "ПодпискаДоЗаписи.xml").write_text(_EVENT_SUB_BEFORE_WRITE, encoding="utf-8")
    (sub_dir / "ПодпискаПриВыполнении.xml").write_text(_EVENT_SUB_ON_EXECUTE, encoding="utf-8")
    (tmp_path / "Configuration.xml").write_text("<Configuration/>", encoding="utf-8")

    helpers, resolve_safe = make_helpers(str(tmp_path))
    format_info = detect_format(str(tmp_path))
    bsl = make_bsl_helpers(
        base_path=str(tmp_path),
        resolve_safe=resolve_safe,
        read_file_fn=helpers["read_file"],
        grep_fn=helpers["grep"],
        glob_files_fn=helpers["glob_files"],
        format_info=format_info,
    )
    return bsl


def test_event_filter_string_normalized_not_iterated_per_char(tmp_path):
    """BUG-8: голая строка 'BeforeWrite' раньше итерировалась посимвольно
    (['B','e','f',...]) — каждый одно-символьный substring-matcher ловил почти
    все события, фильтр де-факто игнорировался. После фикса: одиночная строка
    оборачивается в [строка] → один substring-matcher.
    """
    bsl = _make_two_subs_fixture(tmp_path)

    # String form — one matcher
    res_str = bsl["find_event_subscriptions"](event_filter="BeforeWrite")
    assert isinstance(res_str, list)
    events = sorted({s.get("event") for s in res_str})
    assert events == ["BeforeWrite"], (
        f"event_filter='BeforeWrite' (string) после фикса должен вернуть только BeforeWrite, получено: {events}"
    )

    # List form — equivalent
    res_list = bsl["find_event_subscriptions"](event_filter=["BeforeWrite"])
    assert {s.get("event") for s in res_list} == {"BeforeWrite"}

    # До фикса 'OnExecute' (string) посимвольно матчил бы и 'BeforeWrite' (буквы O,n,E,c,u → нет; но e,t,o есть в before).
    # Проверим явный негативный кейс:
    res_other = bsl["find_event_subscriptions"](event_filter="OnExecute")
    assert {s.get("event") for s in res_other} == {"OnExecute"}


def test_event_filter_empty_string_treated_as_no_filter(tmp_path):
    """BUG-8: пустая строка должна нормализоваться в None (= без фильтра),
    чтобы не превращаться в [''], который матчит всё (и побочно, '' в Like — это true)."""
    bsl = _make_two_subs_fixture(tmp_path)
    res_empty = bsl["find_event_subscriptions"](event_filter="")
    res_none = bsl["find_event_subscriptions"](event_filter=None)
    # Оба должны вернуть все подписки
    assert {s.get("event") for s in res_empty} == {"BeforeWrite", "OnExecute"}
    assert {s.get("event") for s in res_none} == {"BeforeWrite", "OnExecute"}


def test_index_reader_get_event_subscriptions_string_filter(tmp_path):
    """BUG-8 (нижний уровень): IndexReader.get_event_subscriptions сам
    нормализует голую строку. Создаём минимальную БД руками и читаем через
    IndexReader — обходим IndexBuilder для изоляции теста.
    """
    import sqlite3
    from rlm_tools_bsl.bsl_index import IndexReader

    db_path = tmp_path / "min.db"
    # Минимальная схема: только event_subscriptions с двумя строками.
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE event_subscriptions ("
        "id INTEGER PRIMARY KEY, name TEXT, synonym TEXT, event TEXT, "
        "handler_module TEXT, handler_procedure TEXT, source_types TEXT, "
        "source_count INTEGER, file TEXT)"
    )
    conn.executemany(
        "INSERT INTO event_subscriptions "
        "(name, synonym, event, handler_module, handler_procedure, source_types, source_count, file) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("ПодпискаДоЗаписи", "", "BeforeWrite", "МойМодуль", "Обработчик1", "[]", 0, "X.xml"),
            ("ПодпискаПриВыполнении", "", "OnExecute", "МойМодуль", "Обработчик2", "[]", 0, "Y.xml"),
        ],
    )
    conn.commit()
    conn.close()

    reader = IndexReader(str(db_path))
    try:
        # Sanity: без фильтра — все 2.
        all_rows = reader.get_event_subscriptions("", event_filter=None)
        assert all_rows is not None and len(all_rows) == 2

        # String form — нормализация в [string] → SQL с одним параметром.
        rows_str = reader.get_event_subscriptions("", event_filter="BeforeWrite")
        events = {r["event"] for r in rows_str}
        assert events == {"BeforeWrite"}, f"string 'BeforeWrite' посимвольно итерировался — events: {events}"

        # List form — эквивалент.
        rows_list = reader.get_event_subscriptions("", event_filter=["BeforeWrite"])
        assert {r["event"] for r in rows_list} == {"BeforeWrite"}

        # Пустая строка → как None (без фильтра).
        rows_empty = reader.get_event_subscriptions("", event_filter="")
        assert {r["event"] for r in rows_empty} == {"BeforeWrite", "OnExecute"}
    finally:
        reader.close()


def test_find_based_on_documents_back_scan_for_letters(tmp_path):
    """BUG-9: для документа без ManagerModule.ДобавитьКомандыСозданияНаОсновании
    (типичный кейс — Письма в ДО3) должен сработать обратный обход:
    другой документ с ОбработкаЗаполнения упоминает ДокументСсылка.<наш> →
    попадает в can_create_from_here с via='back_scan'.
    """
    from rlm_tools_bsl.helpers import make_helpers
    from rlm_tools_bsl.format_detector import detect_format
    from rlm_tools_bsl.bsl_helpers import make_bsl_helpers

    # Document A — без ManagerModule (= нет ДобавитьКомандыСозданияНаОсновании).
    # Object module пустой (никаких процедур).
    doc_a_dir = tmp_path / "Documents" / "ВходящееПисьмо" / "Ext"
    doc_a_dir.mkdir(parents=True)
    (doc_a_dir / "ObjectModule.bsl").write_text("// empty\n", encoding="utf-8")

    # Document B — есть ОбработкаЗаполнения с Тип("ДокументСсылка.ВходящееПисьмо")
    doc_b_dir = tmp_path / "Documents" / "Задача" / "Ext"
    doc_b_dir.mkdir(parents=True)
    (doc_b_dir / "ObjectModule.bsl").write_text(
        "Процедура ОбработкаЗаполнения(ДанныеЗаполнения, СтандартнаяОбработка) Экспорт\n"
        '\tЕсли ТипЗнч(ДанныеЗаполнения) = Тип("ДокументСсылка.ВходящееПисьмо") Тогда\n'
        "\t\tЗаполнитьИзПисьма(ДанныеЗаполнения);\n"
        "\tКонецЕсли;\n"
        "КонецПроцедуры\n",
        encoding="utf-8",
    )

    # Document C — без ОбработкаЗаполнения (контрольный негативный, не должен ловиться)
    doc_c_dir = tmp_path / "Documents" / "ИсходящееПисьмо" / "Ext"
    doc_c_dir.mkdir(parents=True)
    (doc_c_dir / "ObjectModule.bsl").write_text("// no fill\n", encoding="utf-8")

    (tmp_path / "Configuration.xml").write_text("<Configuration/>", encoding="utf-8")

    helpers, resolve_safe = make_helpers(str(tmp_path))
    format_info = detect_format(str(tmp_path))
    bsl = make_bsl_helpers(
        base_path=str(tmp_path),
        resolve_safe=resolve_safe,
        read_file_fn=helpers["read_file"],
        grep_fn=helpers["grep"],
        glob_files_fn=helpers["glob_files"],
        format_info=format_info,
    )

    res = bsl["find_based_on_documents"]("ВходящееПисьмо")

    docs = [d["document"] for d in res["can_create_from_here"]]
    assert "Задача" in docs, f"back_scan не нашёл Задача среди создаваемых: {res['can_create_from_here']}"
    assert "ИсходящееПисьмо" not in docs  # контрольный негативный
    # Записи помечены via='back_scan'
    zad = next(d for d in res["can_create_from_here"] if d["document"] == "Задача")
    assert zad.get("via") == "back_scan"


def test_find_based_on_documents_back_scan_skipped_when_direct_finds(tmp_path):
    """BUG-9: если прямой обход уже нашёл can_create_from_here — back_scan
    пропускается (не должен дублировать или мешать)."""
    from rlm_tools_bsl.helpers import make_helpers
    from rlm_tools_bsl.format_detector import detect_format
    from rlm_tools_bsl.bsl_helpers import make_bsl_helpers

    # Document X с ManagerModule.ДобавитьКомандыСозданияНаОсновании (прямой обход → найдёт Y)
    doc_x_dir = tmp_path / "Documents" / "ИсточникX" / "Ext"
    doc_x_dir.mkdir(parents=True)
    (doc_x_dir / "ManagerModule.bsl").write_text(
        "Процедура ДобавитьКомандыСозданияНаОсновании(КомандыСоздания) Экспорт\n"
        "\tДокументы.ЦельY.ДобавитьКомандуСозданияНаОснованииМассово(КомандыСоздания);\n"
        "КонецПроцедуры\n",
        encoding="utf-8",
    )

    # Document Z — никак не связан, но для контроля чтобы back_scan был не пуст,
    # упомянём ИсточникX в его ОбработкаЗаполнения.
    doc_z_dir = tmp_path / "Documents" / "ОмофонZ" / "Ext"
    doc_z_dir.mkdir(parents=True)
    (doc_z_dir / "ObjectModule.bsl").write_text(
        "Процедура ОбработкаЗаполнения(ДанныеЗаполнения, СтандартнаяОбработка) Экспорт\n"
        '\tТипЗнч(ДанныеЗаполнения) = Тип("ДокументСсылка.ИсточникX");\n'
        "КонецПроцедуры\n",
        encoding="utf-8",
    )

    (tmp_path / "Configuration.xml").write_text("<Configuration/>", encoding="utf-8")

    helpers, resolve_safe = make_helpers(str(tmp_path))
    format_info = detect_format(str(tmp_path))
    bsl = make_bsl_helpers(
        base_path=str(tmp_path),
        resolve_safe=resolve_safe,
        read_file_fn=helpers["read_file"],
        grep_fn=helpers["grep"],
        glob_files_fn=helpers["glob_files"],
        format_info=format_info,
    )

    res = bsl["find_based_on_documents"]("ИсточникX")
    docs = res["can_create_from_here"]
    # Прямой обход нашёл ЦельY → back_scan пропущен → ОмофонZ НЕ должен попасть в результат.
    names = [d["document"] for d in docs]
    assert "ЦельY" in names
    assert "ОмофонZ" not in names, f"back_scan не должен срабатывать когда прямой обход дал результат: {names}"
    # И записи прямого обхода НЕ помечены via='back_scan' (опциональное поле)
    yel = next(d for d in docs if d["document"] == "ЦельY")
    assert yel.get("via") != "back_scan"
