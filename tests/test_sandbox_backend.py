"""v1.29.0 этап 1: backend-интерфейс, InlineSandboxBackend, registry snapshot, reaper."""

import json
import shutil
import subprocess
import threading
import time

import pytest

from rlm_tools_bsl.format_detector import FormatInfo, SourceFormat
from rlm_tools_bsl.sandbox import Sandbox
from rlm_tools_bsl.sandbox_backend import (
    CloseReport,
    InlineSandboxBackend,
    SandboxBackendReaper,
    SandboxClosedError,
)


def _make_sandbox(tmp_path, with_bsl=True):
    format_info = (
        FormatInfo(
            primary_format=SourceFormat.CF,
            root_path=str(tmp_path),
            bsl_file_count=0,
            has_configuration_xml=True,
            metadata_categories_found=[],
        )
        if with_bsl
        else None
    )
    return Sandbox(base_path=str(tmp_path), max_output_chars=10_000, format_info=format_info)


def _make_backend(tmp_path, **kwargs):
    kwargs.setdefault("install_llm_tools", False)
    return InlineSandboxBackend(_make_sandbox(tmp_path), None, **kwargs)


# ---------------------------------------------------------------------------
# Registry snapshot
# ---------------------------------------------------------------------------


def test_snapshot_json_serializable_and_no_callables(tmp_path):
    backend = _make_backend(tmp_path)
    snapshot = backend.registry_snapshot
    assert snapshot, "BSL session must have registered helpers"
    text = json.dumps(snapshot, ensure_ascii=False)  # не должно упасть
    assert "fn" not in text or all("fn" not in entry for entry in snapshot.values())
    for entry in snapshot.values():
        assert set(entry.keys()) == {"sig", "cat", "kw", "recipe"}
        assert not any(callable(v) for v in entry.values())


def test_snapshot_matches_actual_session_registry(tmp_path):
    sandbox = _make_sandbox(tmp_path)
    backend = InlineSandboxBackend(sandbox, None, install_llm_tools=False)
    real_registry = sandbox._namespace["_registry"]
    assert set(backend.registry_snapshot.keys()) == set(real_registry.keys())
    # sig в снапшоте совпадает с реальным registry сессии
    for name, entry in backend.registry_snapshot.items():
        assert entry["sig"] == real_registry[name]["sig"]


def test_snapshot_mutation_does_not_leak(tmp_path):
    sandbox = _make_sandbox(tmp_path)
    backend = InlineSandboxBackend(sandbox, None, install_llm_tools=False)
    snap = backend.registry_snapshot
    name = next(iter(snap))
    snap[name]["sig"] = "EVIL"
    snap[name]["kw"].append("evil")
    del snap[name]
    fresh = backend.registry_snapshot
    assert fresh[name]["sig"] != "EVIL"
    assert "evil" not in fresh[name]["kw"]
    # Реальный registry не тронут и всё ещё несёт callable fn
    assert callable(sandbox._namespace["_registry"][name]["fn"])
    assert sandbox._namespace["_registry"][name]["sig"] != "EVIL"


def test_registry_names_is_computed_view(tmp_path):
    backend = _make_backend(tmp_path)
    assert backend.registry_names == tuple(backend.registry_snapshot.keys())


def test_no_bsl_session_has_empty_snapshot(tmp_path):
    backend = InlineSandboxBackend(_make_sandbox(tmp_path, with_bsl=False), None, install_llm_tools=False)
    assert backend.registry_snapshot == {}
    assert backend.registry_names == ()


def test_git_search_in_snapshot_iff_in_session_registry(tmp_path):
    # Каталог rlm_help собран с force и ВСЕГДА документирует git_search; session
    # snapshot обязан отражать фактический registry (§23.15).
    no_git_backend = _make_backend(tmp_path)
    sandbox_registry = "git_search" in no_git_backend._sandbox._namespace["_registry"]
    assert ("git_search" in no_git_backend.registry_snapshot) == sandbox_registry
    if shutil.which("git"):
        assert "git_search" not in no_git_backend.registry_snapshot  # tmp_path не под git

        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
        backend = InlineSandboxBackend(
            Sandbox(
                base_path=str(repo),
                max_output_chars=10_000,
                format_info=FormatInfo(SourceFormat.CF, str(repo), 0, True, []),
            ),
            None,
            install_llm_tools=False,
        )
        assert "git_search" in backend.registry_snapshot


def test_snapshot_catalog_mismatch_is_init_error(tmp_path):
    sandbox = _make_sandbox(tmp_path)
    sandbox._namespace["_registry"]["helper_not_in_catalog"] = {"fn": lambda: None, "sig": "x()"}
    with pytest.raises(RuntimeError, match="helper_not_in_catalog"):
        InlineSandboxBackend(sandbox, None, install_llm_tools=False)


# ---------------------------------------------------------------------------
# Execute / metadata / lifecycle
# ---------------------------------------------------------------------------


def test_execute_passthrough(tmp_path):
    backend = _make_backend(tmp_path)
    result = backend.execute("x = 21\nprint(x * 2)")
    assert result.error is None
    assert result.stdout.strip() == "42"
    assert result.generation == 1
    assert result.sandbox_state is None
    assert "x" in result.variables


def test_execute_after_request_close_raises(tmp_path):
    backend = _make_backend(tmp_path)
    backend.request_close("rlm_end")
    with pytest.raises(SandboxClosedError):
        backend.execute("print(1)")


def test_finish_close_idempotent_and_owns_reader(tmp_path):
    closed = []

    class FakeReader:
        def close(self):
            closed.append(1)

    backend = InlineSandboxBackend(_make_sandbox(tmp_path), FakeReader(), install_llm_tools=False)
    backend.request_close("test")
    report = backend.finish_close(time.monotonic() + 5)
    assert report.closed and not report.residual
    report2 = backend.finish_close(time.monotonic() + 5)
    assert report2.closed
    assert closed == [1], "reader закрыт ровно один раз"
    assert backend.state == "closed"


def test_request_close_idempotent_nonblocking(tmp_path):
    backend = _make_backend(tmp_path)
    t0 = time.monotonic()
    for _ in range(5):
        backend.request_close("repeat")
    assert time.monotonic() - t0 < 0.5
    assert backend.state == "closing"


def test_backend_mode_and_diagnostics(tmp_path):
    backend = _make_backend(tmp_path)
    assert backend.mode == "inline"
    assert backend.generation == 1
    assert backend.worker_pid is None
    assert backend.extension_paths == []
    assert backend.extension_paths_count == 0
    assert backend.index_loaded is False
    assert backend.prefixes_source in ("index", "fallback", "none")


# ---------------------------------------------------------------------------
# LLM quota (inline)
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_llm(monkeypatch):
    calls = []

    def provider(prompt, context=""):
        calls.append(prompt)
        return f"ans:{prompt}"

    monkeypatch.setattr("rlm_tools_bsl.llm_bridge.get_llm_query_fn", lambda: provider)
    return calls


def test_inline_llm_quota_single(tmp_path, fake_llm):
    backend = InlineSandboxBackend(_make_sandbox(tmp_path), None, max_llm_calls=2, install_llm_tools=True)
    assert backend.has_llm_tools
    r = backend.execute("print(llm_query('a'))\nprint(llm_query('b'))")
    assert r.error is None
    assert backend.llm_calls_used == 2
    r = backend.execute("print(llm_query('c'))")
    assert r.error is not None and "LLM call limit exceeded" in r.error
    assert backend.llm_calls_used == 2
    assert fake_llm == ["a", "b"]


def test_inline_llm_batch_all_or_nothing(tmp_path, fake_llm):
    backend = InlineSandboxBackend(_make_sandbox(tmp_path), None, max_llm_calls=2, install_llm_tools=True)
    r = backend.execute("print(llm_query_batched(['p1','p2','p3']))")
    assert r.error is not None and "LLM call limit exceeded" in r.error
    assert backend.llm_calls_used == 0, "неудачный batch не расходует quota"
    assert fake_llm == [], "provider получил ноль вызовов"
    r = backend.execute("print(llm_query_batched(['p1','p2']))")
    assert r.error is None
    assert backend.llm_calls_used == 2
    assert sorted(fake_llm) == ["p1", "p2"]


def test_inline_llm_used_carryover(tmp_path, fake_llm):
    backend = InlineSandboxBackend(
        _make_sandbox(tmp_path), None, max_llm_calls=3, llm_calls_used=2, install_llm_tools=True
    )
    r = backend.execute("print(llm_query('x'))")
    assert r.error is None
    assert backend.llm_calls_used == 3
    r = backend.execute("print(llm_query('y'))")
    assert r.error is not None and "LLM call limit exceeded" in r.error


def test_inline_no_llm_config(tmp_path, monkeypatch):
    monkeypatch.setattr("rlm_tools_bsl.llm_bridge.get_llm_query_fn", lambda: None)
    backend = InlineSandboxBackend(_make_sandbox(tmp_path), None, install_llm_tools=True)
    assert backend.has_llm_tools is False
    r = backend.execute("print('llm_query' in dir())")
    assert r.error is None


# ---------------------------------------------------------------------------
# Reaper
# ---------------------------------------------------------------------------


class _FakeBackend:
    def __init__(self, close_delay=0.0, residual_times=0):
        self.close_delay = close_delay
        self.residual_times = residual_times
        self.finish_calls = 0
        self.closed_event = threading.Event()

    def request_close(self, reason):
        pass

    def finish_close(self, deadline):
        self.finish_calls += 1
        if self.close_delay:
            time.sleep(self.close_delay)
        if self.finish_calls <= self.residual_times:
            return CloseReport(closed=False, residual=True)
        self.closed_event.set()
        return CloseReport(closed=True)


def _wait(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def test_reaper_enqueue_is_nonblocking_and_bounded():
    """cleanup N expired backends не занимает N × grace в caller-потоке (§9.4)."""
    reaper = SandboxBackendReaper()
    backends = [_FakeBackend(close_delay=0.3) for _ in range(5)]
    t0 = time.monotonic()
    for b in backends:
        reaper.enqueue(b)
    enqueue_elapsed = time.monotonic() - t0
    assert enqueue_elapsed < 0.2, f"enqueue blocked caller for {enqueue_elapsed:.2f}s"
    assert _wait(lambda: all(b.closed_event.is_set() for b in backends), timeout=10)
    assert _wait(lambda: reaper.pending_count() == 0)


def test_reaper_duplicate_enqueue_suppressed():
    reaper = SandboxBackendReaper()
    backend = _FakeBackend(close_delay=0.2)
    reaper.enqueue(backend)
    reaper.enqueue(backend)
    reaper.enqueue(backend)
    assert _wait(lambda: backend.closed_event.is_set())
    assert _wait(lambda: reaper.pending_count() == 0)
    assert backend.finish_calls == 1


def test_reaper_residual_retry():
    reaper = SandboxBackendReaper()
    backend = _FakeBackend(residual_times=2)
    reaper.enqueue(backend)
    assert _wait(lambda: backend.closed_event.is_set())
    assert backend.finish_calls == 3
    assert _wait(lambda: reaper.pending_count() == 0)


def test_reaper_survives_raising_backend():
    reaper = SandboxBackendReaper()

    class Boom:
        def finish_close(self, deadline):
            raise RuntimeError("boom")

    reaper.enqueue(Boom())
    ok = _FakeBackend()
    reaper.enqueue(ok)
    assert _wait(lambda: ok.closed_event.is_set())


def test_reaper_retries_after_finish_close_raises():
    """§9.4.5 «queue не может молча потерять backend»: исключение в finish_close
    раньше вело к discard из pending-set без повтора — при падении ДО kill дерева
    worker остался бы жить."""
    reaper = SandboxBackendReaper()
    calls = []

    class FlakyBackend:
        mode = "flaky"

        def finish_close(self, deadline):
            calls.append(deadline)
            if len(calls) < 3:
                raise RuntimeError("boom")
            return CloseReport(closed=True)

    backend = FlakyBackend()
    try:
        reaper.enqueue(backend)
        deadline = time.monotonic() + 20
        while reaper.pending_count() > 0 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert reaper.pending_count() == 0, "backend потерян/завис в pending"
        assert len(calls) >= 3, f"после исключения повтора не было: {len(calls)} вызовов"
    finally:
        reaper.stop()


def test_reaper_drain_respects_deadline():
    """drain() при server shutdown ограничен ОБЩИМ deadline, а не ждёт вечно."""
    reaper = SandboxBackendReaper()

    class StuckBackend:
        mode = "stuck"

        def finish_close(self, deadline):
            return CloseReport(closed=False, residual=True)

    try:
        reaper.enqueue(StuckBackend())
        t0 = time.monotonic()
        assert reaper.drain(time.monotonic() + 0.5) is False
        assert time.monotonic() - t0 < 3, "drain не уложился в отведённый deadline"
    finally:
        reaper.stop()


def test_inline_finish_close_is_bounded_while_executing(tmp_path):
    """Codex P1: inline finish_close звал IndexReader.close() из reaper-потока.
    close() ждёт внутренний lock reader-а, поэтому активный helper подвесил бы
    ЕДИНСТВЕННЫЙ reaper и остановил уборку всех остальных сессий."""
    started = threading.Event()
    release = threading.Event()

    class BlockingReader:
        """Зеркалит настоящий IndexReader.close(): ``with self._lock: conn.close()``.
        Пока helper держит lock, close() не вернётся."""

        def __init__(self):
            self._lock = threading.Lock()
            self.closed = False

        def close(self):
            with self._lock:
                self.closed = True

    reader = BlockingReader()
    sandbox = _make_sandbox(tmp_path, with_bsl=False)

    def _block():
        # Захватываем lock reader-а ровно как долгий helper внутри execute.
        with reader._lock:
            started.set()
            release.wait(timeout=30)

    # test-only инъекция прямо в namespace (как в §18.2): production-хелпера с
    # блокировкой в песочнице нет и быть не должно.
    sandbox._namespace["_block"] = _block
    backend = InlineSandboxBackend(sandbox, idx_reader=reader, install_llm_tools=False)
    thread = threading.Thread(target=lambda: backend.execute("_block()"))
    try:
        thread.start()
        assert started.wait(timeout=10), "execute не стартовал"

        backend.request_close("rlm_end")
        t0 = time.monotonic()
        report = backend.finish_close(time.monotonic() + 0.3)
        elapsed = time.monotonic() - t0
        assert elapsed < 3, f"finish_close заблокировал reaper на {elapsed:.1f}s"
        assert report.residual is True, "во время активного execute ожидается residual-повтор"
        assert report.closed is False
        assert reader.closed is False, "reader закрыт ПОД работающим кодом"
    finally:
        release.set()
        thread.join(timeout=30)
        # После завершения execute повторный проход reaper закрывает reader штатно.
        final = backend.finish_close(time.monotonic() + 10)
        assert final.closed is True
        assert reader.closed is True, "reader так и не закрыт — утечка SQLite handle"


def test_reaper_final_attempt_gets_expired_deadline():
    """Codex P1: раньше КАЖДЫЙ retry получал свежий `now + 15s`, поэтому активный
    inline-execute всегда возвращал residual, force-ветка finish_close не
    вызывалась ни разу, а по исчерпании бюджета backend просто забывали —
    с открытым reader. Финальная попытка обязана прийти с ИСТЁКШИМ deadline."""
    reaper = SandboxBackendReaper()
    reaper._RESIDUAL_BUDGET_SECONDS = 0.4  # ускоряем бюджет, семантику не меняем
    reaper._RESIDUAL_BACKOFF_START = 0.05
    reaper._RESIDUAL_BACKOFF_MAX = 0.05
    seen = []

    class AlwaysResidualUntilForced:
        mode = "stubborn"

        def __init__(self):
            self.force_closed = False

        def finish_close(self, deadline):
            expired = deadline < time.monotonic()
            seen.append(expired)
            if expired:
                self.force_closed = True
                return CloseReport(closed=True, forced=True)
            return CloseReport(closed=False, residual=True)

    backend = AlwaysResidualUntilForced()
    reaper.enqueue(backend)
    limit = time.monotonic() + 20
    while reaper.pending_count() > 0 and time.monotonic() < limit:
        time.sleep(0.05)

    assert reaper.pending_count() == 0, "backend не был отпущен"
    assert len(seen) >= 2, f"повторов не было вовсе: {seen}"
    assert seen[0] is False, "первая попытка не должна быть force"
    assert seen[-1] is True, "финальная попытка обязана прийти с истёкшим deadline"
    assert backend.force_closed, "force-ветка cleanup так и не отработала"


def test_reaper_force_abort_pending_is_nonblocking_and_honest():
    """§13.6: shutdown обязан ДОБИТЬ очередь reaper-а, но неблокирующе, и не
    считать очищенным то, что добить не удалось."""
    reaper = SandboxBackendReaper()
    aborted = []

    class AbortableBackend:
        mode = "abortable"

        def finish_close(self, deadline):
            return CloseReport(closed=False, residual=True)

        def force_abort(self):
            aborted.append(self)
            return True

    class UnkillableBackend:
        mode = "unkillable"

        def finish_close(self, deadline):
            return CloseReport(closed=False, residual=True)

        def force_abort(self):
            return False

    good = [AbortableBackend() for _ in range(3)]
    bad = UnkillableBackend()
    try:
        for b in [*good, bad]:
            reaper.enqueue(b)
        assert reaper.drain(time.monotonic() + 0.3) is False, "drain обязан честно сообщить, что не успел"

        t0 = time.monotonic()
        closed, left = reaper.force_abort_pending()
        assert time.monotonic() - t0 < 2, "force_abort_pending заблокировался"
        assert closed == 3, f"добито {closed} из 3"
        assert len(aborted) == 3
        # Неубиваемый обязан ОСТАТЬСЯ в pending: иначе pending_count соврёт про
        # успешный drain при живом процессе.
        assert left >= 1 and reaper.pending_count() >= 1
    finally:
        # Без stop() поток продолжал бы повторы и логирование до конца pytest.
        reaper.stop()


def test_reaper_keeps_residual_backend_in_pending_after_budget():
    """Codex P1: после исчерпания бюджета backend удалялся из _pending, и живой
    worker исчезал из диагностики. Он обязан остаться и получать редкие повторы."""
    reaper = SandboxBackendReaper()
    reaper._RESIDUAL_BUDGET_SECONDS = 0.3
    reaper._RESIDUAL_BACKOFF_START = 0.05
    reaper._RESIDUAL_BACKOFF_MAX = 0.05
    reaper._SLOW_RETRY_SECONDS = 0.2
    attempts = []

    class NeverCloses:
        mode = "never"

        def finish_close(self, deadline):
            attempts.append(deadline < time.monotonic())
            return CloseReport(closed=False, residual=True)

    try:
        reaper.enqueue(NeverCloses())
        # Ждём именно force-попытку (истёкший deadline), а не просто N повторов:
        # по счётчику цикл вышел бы раньше, чем истечёт бюджет.
        limit = time.monotonic() + 5
        while not any(attempts) and time.monotonic() < limit:
            time.sleep(0.05)
        assert any(attempts), f"force-попытка так и не пришла: {attempts}"

        # И повторы обязаны продолжаться ПОСЛЕ неё, а не прекратиться.
        after_force = len(attempts)
        limit = time.monotonic() + 5
        while len(attempts) <= after_force and time.monotonic() < limit:
            time.sleep(0.05)
        assert len(attempts) > after_force, "после неуспешной force-попытки повторы прекратились"
        assert reaper.pending_count() == 1, "незакрытый backend пропал из pending — диагностика соврала"
    finally:
        # Без stop() поток повторял бы попытки и логировал до конца pytest-процесса.
        reaper.stop()


def test_reaper_slow_retry_does_not_block_new_backends():
    """Codex P1: backoff реализовывался через sleep() в ЕДИНСТВЕННОМ consumer-е,
    поэтому один residual-backend с 30-секундным повтором задерживал все
    штатно закрывающиеся сессии позади него в FIFO."""
    reaper = SandboxBackendReaper()
    reaper._RESIDUAL_BUDGET_SECONDS = 0.0  # сразу в slow-retry режим
    reaper._SLOW_RETRY_SECONDS = 30.0
    fast_closed = threading.Event()

    class NeverCloses:
        mode = "never"

        def finish_close(self, deadline):
            return CloseReport(closed=False, residual=True)

    class ClosesFast:
        mode = "fast"

        def finish_close(self, deadline):
            fast_closed.set()
            return CloseReport(closed=True)

    try:
        reaper.enqueue(NeverCloses())
        time.sleep(0.2)  # дать ему уйти в 30-секундный откладывание
        reaper.enqueue(ClosesFast())
        assert fast_closed.wait(timeout=5), (
            "новый backend не обслужен — consumer заблокирован чужим 30-секундным backoff"
        )
    finally:
        reaper.stop()
