"""v1.23.0 — server-side efficiency nudges (session-cumulative, throttled).

Nudges live in the rlm_execute response metadata (never the helper return / stdout)
and target the call-leak classes from the A/B logs: non-batched reads, re-resolves.
"""

from __future__ import annotations

import os
import tempfile

from rlm_tools_bsl.format_detector import detect_format
from rlm_tools_bsl.sandbox import HelperCall, Sandbox


def _txt_sandbox(tmpdir, names=("a", "b", "c")):
    for n in names:
        with open(os.path.join(tmpdir, f"{n}.txt"), "w", encoding="utf-8") as f:
            f.write("CONTENT")
    return Sandbox(base_path=tmpdir)


def _bsl_sandbox(tmpdir):
    obj = os.path.join(tmpdir, "Documents", "Док", "Ext")
    os.makedirs(obj)
    with open(os.path.join(obj, "ObjectModule.bsl"), "w", encoding="utf-8") as f:
        f.write("Процедура П() Экспорт\nКонецПроцедуры\n")
    with open(os.path.join(tmpdir, "Configuration.xml"), "w") as f:
        f.write("<Configuration/>")
    return Sandbox(base_path=tmpdir, format_info=detect_format(tmpdir))


def test_read_file_triggers_read_files_nudge():
    with tempfile.TemporaryDirectory() as tmpdir:
        sb = _txt_sandbox(tmpdir)
        res = sb.execute("read_file('a.txt'); read_file('b.txt'); read_file('c.txt')")
        ids = {h["id"] for h in (res.efficiency_hints or [])}
        assert "read_files" in ids
        h = next(h for h in res.efficiency_hints if h["id"] == "read_files")
        assert h["helper"] == "read_file"
        assert h["count"] >= 3
        assert "read_files([" in h["message"]


def test_batched_read_files_no_nudge():
    """Using the aggregate form (read_files) once → nothing to nudge."""
    with tempfile.TemporaryDirectory() as tmpdir:
        sb = _txt_sandbox(tmpdir)
        res = sb.execute("d = read_files(['a.txt','b.txt','c.txt'])")
        assert not res.efficiency_hints


def test_repeated_find_module_triggers_reuse_var():
    with tempfile.TemporaryDirectory() as tmpdir:
        sb = _bsl_sandbox(tmpdir)
        sb.execute("find_module('Док')")
        res = sb.execute("find_module('Док')")  # same arg fingerprint
        ids = {h["id"] for h in (res.efficiency_hints or [])}
        assert "reuse_var" in ids
        h = next(h for h in res.efficiency_hints if h["id"] == "reuse_var")
        assert h["helper"] == "find_module"


def test_different_find_module_args_do_not_trigger_reuse():
    with tempfile.TemporaryDirectory() as tmpdir:
        sb = _bsl_sandbox(tmpdir)
        sb.execute("find_module('Док')")
        res = sb.execute("find_module('Другое')")  # different fingerprint
        assert not any(h["id"] == "reuse_var" for h in (res.efficiency_hints or []))


def test_nudge_throttled_once_per_session():
    with tempfile.TemporaryDirectory() as tmpdir:
        sb = _bsl_sandbox(tmpdir)
        sb.execute("find_module('Док')")
        r2 = sb.execute("find_module('Док')")
        r3 = sb.execute("find_module('Док')")
        assert any(h["id"] == "reuse_var" for h in (r2.efficiency_hints or []))
        assert not any(h["id"] == "reuse_var" for h in (r3.efficiency_hints or []))


def test_aggregator_is_instance_local():
    """Two sandboxes never share nudge state (no module singleton leak across sessions)."""
    with tempfile.TemporaryDirectory() as t1, tempfile.TemporaryDirectory() as t2:
        sb1 = _txt_sandbox(t1)
        sb2 = _txt_sandbox(t2)
        sb1.execute("read_file('a.txt'); read_file('b.txt'); read_file('c.txt')")
        # sb2 is fresh — one read, no nudge
        res2 = sb2.execute("read_file('a.txt')")
        assert not res2.efficiency_hints


def test_dense_batch_in_one_execute_no_batch_nudge():
    """v1.24.0 #5 — 20 invocations of one helper in ONE execute is perfect batching;
    the batch nudge (which counts rlm_execute ROUND-TRIPS now, not invocations) must
    NOT fire. Regression: agent 06 did this and still got the hint."""
    with tempfile.TemporaryDirectory() as tmpdir:
        sb = _bsl_sandbox(tmpdir)
        code = "\n".join(f"find_module('Mod{i}')" for i in range(20))
        res = sb.execute(code)
        ids = {h["id"] for h in (res.efficiency_hints or [])}
        assert "batch" not in ids, res.efficiency_hints


def test_few_execute_with_one_dense_execute_no_batch_nudge():
    """v1.24.0 #5 — 4 execute total, one of which packs 8 calls. Only 3 sparse
    round-trips < threshold → no batch nudge (agent 08 case: 4 execute, ideal)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        sb = _bsl_sandbox(tmpdir)
        # one dense execute: 8 calls
        sb.execute("\n".join(f"find_module('Dense{i}')" for i in range(8)))
        # three single-call execute
        last = None
        for i in range(3):
            last = sb.execute(f"find_module('Single{i}')")
        ids = {h["id"] for h in (last.efficiency_hints or [])}
        assert "batch" not in ids, last.efficiency_hints


def test_many_sparse_execute_triggers_batch_with_roundtrip_count():
    """v1.24.0 #5 — 8 separate execute, each a single non-aggregate call → batch
    nudge fires, count reflects ROUND-TRIPS (8), not summed invocations."""
    with tempfile.TemporaryDirectory() as tmpdir:
        sb = _bsl_sandbox(tmpdir)
        last = None
        for i in range(8):
            last = sb.execute(f"find_module('Round{i}')")  # distinct args → no reuse_var
        hints = last.efficiency_hints or []
        batch = [h for h in hints if h["id"] == "batch"]
        assert batch, hints
        assert batch[0]["count"] == 8
        assert "round-trip" in batch[0]["trigger"].lower()


def test_zero_helper_execute_does_not_count_as_sparse():
    """v1.24.0 #5 — execute with NO helper calls (pure Python/print) must not
    increment the sparse counter and must not raise AttributeError."""
    with tempfile.TemporaryDirectory() as tmpdir:
        sb = _bsl_sandbox(tmpdir)
        # 8 pure-python executes — would reach threshold if counted as sparse.
        last = None
        for i in range(8):
            last = sb.execute(f"x = {i}; print(x)")
        ids = {h["id"] for h in (last.efficiency_hints or [])}
        assert "batch" not in ids, last.efficiency_hints


def test_hints_in_metadata_not_stdout_and_return_unchanged():
    with tempfile.TemporaryDirectory() as tmpdir:
        sb = _txt_sandbox(tmpdir)
        res = sb.execute("x = read_file('a.txt'); read_file('b.txt'); read_file('c.txt'); print(x)")
        assert res.efficiency_hints  # present in metadata
        # NOT leaked into stdout
        assert "read_files([" not in res.stdout
        assert "HINT" not in res.stdout
        # helper return value unchanged (x is the file content)
        assert "CONTENT" in res.stdout


# ── v1.27.0 — redundant get_index_info() at session start ────────────────────
# get_index_info() runs only with an index, and the gate only inspects HelperCall.seq,
# so these drive _compute_efficiency_hints via a simulated _helper_calls list.


def test_redundant_get_index_info_hint_fires_at_session_start():
    with tempfile.TemporaryDirectory() as tmpdir:
        sb = Sandbox(base_path=tmpdir)
        sb._helper_calls = [HelperCall("get_index_info", 0.0, seq=1)]
        hints = sb._compute_efficiency_hints()
        ids = {h["id"] for h in hints}
        assert "redundant_get_index_info" in ids
        h = next(h for h in hints if h["id"] == "redundant_get_index_info")
        assert h["helper"] == "get_index_info"
        assert "rlm_start.index" in h["message"]


def test_redundant_get_index_info_hint_fires_as_second_call():
    """seq==2 is still a start signal (called 2nd) → nudge fires."""
    with tempfile.TemporaryDirectory() as tmpdir:
        sb = Sandbox(base_path=tmpdir)
        sb._helper_calls = [
            HelperCall("find_module", 0.0, seq=1),
            HelperCall("get_index_info", 0.0, seq=2),
        ]
        ids = {h["id"] for h in sb._compute_efficiency_hints()}
        assert "redundant_get_index_info" in ids


def test_redundant_get_index_info_hint_absent_mid_session():
    """A high-seq mid-session call (e.g. fetching has_regions) must NOT nudge."""
    with tempfile.TemporaryDirectory() as tmpdir:
        sb = Sandbox(base_path=tmpdir)
        sb._helper_calls = [HelperCall("get_index_info", 0.0, seq=5)]
        ids = {h["id"] for h in sb._compute_efficiency_hints()}
        assert "redundant_get_index_info" not in ids


def test_redundant_get_index_info_hint_throttled_once_per_session():
    with tempfile.TemporaryDirectory() as tmpdir:
        sb = Sandbox(base_path=tmpdir)
        sb._helper_calls = [HelperCall("get_index_info", 0.0, seq=1)]
        first = {h["id"] for h in sb._compute_efficiency_hints()}
        sb._helper_calls = [HelperCall("get_index_info", 0.0, seq=2)]
        second = {h["id"] for h in sb._compute_efficiency_hints()}
        assert "redundant_get_index_info" in first
        assert "redundant_get_index_info" not in second


def test_no_get_index_info_no_hint():
    with tempfile.TemporaryDirectory() as tmpdir:
        sb = Sandbox(base_path=tmpdir)
        sb._helper_calls = [HelperCall("find_module", 0.0, seq=1)]
        ids = {h["id"] for h in sb._compute_efficiency_hints()}
        assert "redundant_get_index_info" not in ids


# ---------------------------------------------------------------------------
# v1.33.x — списочная выдача упёрлась в limit: сигнала в самом ответе нет
# (list[dict], а count_only-контракт заморожен), поэтому он идёт сюда.
# ---------------------------------------------------------------------------


def _regions_sandbox(tmp_path, monkeypatch):
    """Проект с тремя областями — достаточно, чтобы упереться в limit=2.

    `tmp_path`, а не TemporaryDirectory: IndexReader держит файл БД открытым, и на
    Windows авто-очистка временного каталога падает с PermissionError.
    idx_reader подаётся ЯВНО — конструктор Sandbox индекс сам не подхватывает (его
    даёт серверная сессия), а без него search_regions вернул бы [] и тест был бы
    вакуумно зелёным на «усечения нет».
    """
    from rlm_tools_bsl.bsl_index import IndexBuilder, IndexReader

    obj = tmp_path / "CommonModules" / "М" / "Ext"
    obj.mkdir(parents=True)
    (obj / "Module.bsl").write_text(
        """#Область А
Процедура П1()
КонецПроцедуры
#КонецОбласти
#Область Б
Процедура П2()
КонецПроцедуры
#КонецОбласти
#Область В
Процедура П3()
КонецПроцедуры
#КонецОбласти
""",
        encoding="utf-8-sig",
    )
    (tmp_path / "Configuration.xml").write_text("<Configuration/>", encoding="utf-8")
    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / ".index"))
    db = IndexBuilder().build(str(tmp_path), build_calls=False, build_fts=False, build_synonyms=False)
    reader = IndexReader(str(db))
    return Sandbox(base_path=str(tmp_path), format_info=detect_format(str(tmp_path)), idx_reader=reader), reader


def test_saturated_list_triggers_truncation_hint(tmp_path, monkeypatch):
    sb, reader = _regions_sandbox(tmp_path, monkeypatch)
    try:
        res = sb.execute("rows = search_regions('', limit=2)\nprint(len(rows))")
        assert res.error is None
        assert "2" in res.stdout
        hints = res.efficiency_hints or []
        h = next((x for x in hints if x["id"] == "list_truncated:search_regions"), None)
        assert h is not None, f"нет подсказки об усечении: {[x['id'] for x in hints]}"
        assert h["helper"] == "search_regions"
        assert h["count"] == 2
        assert "group_by" in h["message"]
        # Возврат хелпера и stdout не тронуты — подсказка живёт ТОЛЬКО в метаданных.
        assert "list_truncated" not in res.stdout
    finally:
        reader.close()


def test_unsaturated_list_gives_no_hint(tmp_path, monkeypatch):
    sb, reader = _regions_sandbox(tmp_path, monkeypatch)
    try:
        res = sb.execute("rows = search_regions('', limit=50)\nprint(len(rows))")
        assert res.error is None
        ids = {h["id"] for h in (res.efficiency_hints or [])}
        assert "list_truncated:search_regions" not in ids
    finally:
        reader.close()


def test_limit_coercion_mirrors_helper_guard(tmp_path, monkeypatch):
    """Разбор limit обязан совпадать с `_coerce_bound`, иначе хинт врёт в обе стороны.

    float хелпер ПРИНИМАЕТ (усекает), поэтому limit=3.0 на трёх областях — это
    настоящее насыщение; строку он заменяет дефолтом 200, и три строки его не
    достигают; limit=0 даёт пустую выдачу, сигнализировать там нечего.
    """
    sb, reader = _regions_sandbox(tmp_path, monkeypatch)
    try:
        hit = sb.execute("rows = search_regions('', limit=3.0)\nprint(len(rows))")
        assert hit.error is None and "3" in hit.stdout
        assert any(h["id"] == "list_truncated:search_regions" for h in (hit.efficiency_hints or []))
    finally:
        reader.close()


def test_limit_coercion_no_false_hint(tmp_path, monkeypatch):
    """Строка заменяется дефолтом 200 (три строки его не достигают), limit=0 — молчит.

    Отдельный тест, а не продолжение предыдущего: там позитивный случай уже
    сработал, и держать негативные рядом — значит проверять их на состоянии,
    которое к ним отношения не имеет.
    """
    sb, reader = _regions_sandbox(tmp_path, monkeypatch)
    try:
        miss = sb.execute("rows = search_regions('', limit='200')\nprint(len(rows))")
        assert miss.error is None and "3" in miss.stdout
        assert not any(h["id"] == "list_truncated:search_regions" for h in (miss.efficiency_hints or []))
        zero = sb.execute("rows = search_regions('', limit=0)\nprint(len(rows))")
        assert zero.error is None and "0" in zero.stdout
        assert not any(h["id"] == "list_truncated:search_regions" for h in (zero.efficiency_hints or []))
    finally:
        reader.close()


def test_truncation_hint_repeats_for_every_truncated_execute(tmp_path, monkeypatch):
    """НЕ троттлится по сессии, в отличие от советов о стиле работы.

    Факт относится к конкретному результату: каждый усечённый вызов — свой набор
    данных, по которому агент может построить неверный агрегат. Однократность
    ломала бы контракт «сигнал в том же ответе, где получен срез».
    """
    sb, reader = _regions_sandbox(tmp_path, monkeypatch)
    try:
        first = sb.execute("search_regions('', limit=2)")
        second = sb.execute("search_regions('', limit=2)")
        for res in (first, second):
            assert any(h["id"] == "list_truncated:search_regions" for h in (res.efficiency_hints or []))
    finally:
        reader.close()


def test_accidental_saturation_does_not_mute_later_truncation(tmp_path, monkeypatch):
    """Сценарий из ревью: первый вызов вернул ровно limit=1 БЕЗ усечения.

    Такой вызов раньше съедал единственный hint-id за сессию, и следующий —
    по-настоящему усечённый — оставался беззвучным.
    """
    sb, reader = _regions_sandbox(tmp_path, monkeypatch)
    try:
        # 'Б' есть ровно одна область: выдача равна limit, но усечения нет.
        accidental = sb.execute("rows = search_regions('Б', limit=1)\nprint(len(rows))")
        assert accidental.error is None and "1" in accidental.stdout
        real = sb.execute("rows = search_regions('', limit=2)\nprint(len(rows))")
        assert real.error is None
        assert any(h["id"] == "list_truncated:search_regions" for h in (real.efficiency_hints or [])), (
            "случайное совпадение с limit не имеет права заглушить настоящее усечение"
        )
    finally:
        reader.close()


def _methods_sandbox(tmp_path, monkeypatch):
    """Песочница с ПОСТРОЕННЫМ FTS и тремя методами под один запрос.

    `build_fts=True` обязателен: без него `search_methods` возвращает [] всегда.
    Запрос берём длиной ≥3 символов — токенайзер `methods_fts` триграммный
    (`tokenize='trigram'`), и по одной букве он не матчит НИЧЕГО.
    """
    from rlm_tools_bsl.bsl_index import IndexBuilder, IndexReader

    obj = tmp_path / "CommonModules" / "МодульПроверок" / "Ext"
    obj.mkdir(parents=True)
    (obj / "Module.bsl").write_text(
        """Процедура ПроверкаОдин() Экспорт
КонецПроцедуры
Процедура ПроверкаДва() Экспорт
КонецПроцедуры
Процедура ПроверкаТри() Экспорт
КонецПроцедуры
""",
        encoding="utf-8-sig",
    )
    (tmp_path / "Configuration.xml").write_text("<Configuration/>", encoding="utf-8")
    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / ".index"))
    db = IndexBuilder().build(str(tmp_path), build_calls=False, build_fts=True, build_synonyms=False)
    reader = IndexReader(str(db))
    return Sandbox(base_path=str(tmp_path), format_info=detect_format(str(tmp_path)), idx_reader=reader), reader


def test_ranked_helper_does_not_claim_unranked_output(tmp_path, monkeypatch):
    """search_methods ранжирует по BM25 — фраза «порядок не релевантность» тут ложь."""
    sb, reader = _methods_sandbox(tmp_path, monkeypatch)
    try:
        res = sb.execute("rows = search_methods('Провер', limit=1)\nprint(len(rows))")
        assert res.error is None
        assert "1" in res.stdout, "FTS обязан найти совпадения — иначе тест ничего не проверяет"
        hints = [h for h in (res.efficiency_hints or []) if h["id"] == "list_truncated:search_methods"]
        assert hints, "выдача упёрлась в limit=1 — подсказка обязана быть"
        assert "не релевантность" not in hints[0]["message"]
        assert "возможно, усечена" in hints[0]["message"]
    finally:
        reader.close()


def test_unranked_helper_warns_about_order(tmp_path, monkeypatch):
    """А у search_regions порядок и правда не релевантность — оговорка обязана быть."""
    sb, reader = _regions_sandbox(tmp_path, monkeypatch)
    try:
        res = sb.execute("rows = search_regions('', limit=2)\nprint(len(rows))")
        assert res.error is None
        rh = next(h for h in res.efficiency_hints if h["id"] == "list_truncated:search_regions")
        assert "не релевантность" in rh["message"]
    finally:
        reader.close()


def test_count_only_and_group_by_never_flagged(tmp_path, monkeypatch):
    """dict-ветки усечения не имеют — подсказка обязана молчать."""
    sb, reader = _regions_sandbox(tmp_path, monkeypatch)
    try:
        res = sb.execute(
            "a = search_regions('', count_only=True)\n"
            "b = search_regions('', group_by='name', limit=1)\n"
            "print(a['total'], b['groups'][0]['count'])"
        )
        assert res.error is None
        ids = {h["id"] for h in (res.efficiency_hints or [])}
        assert not any(i.startswith("list_truncated") for i in ids)
    finally:
        reader.close()
