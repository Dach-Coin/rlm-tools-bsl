"""v1.40.0: общий бюджет потоков обхода дерева на РЕАЛЬНЫХ spawn-процессах.

Модель «обход идёт» — test-only хук ``ProcessBackendConfig.test_scan_hold_extra``:
worker сразу после сборки сессии берёт столько потоков из своей аренды и не отдаёт.
Проверяется предел УСТАНОВИВШЕГОСЯ режима (аренды разнесены во времени) и все пути,
которыми родитель возвращает потоки убитого воркера. Одновременный старт здесь не
моделируется — его граница задана формулой выдачи и юнит-тестом с барьерами.
"""

import time

import pytest

from _process_test_utils import make_cf_project
from rlm_tools_bsl import _scan_budget
from rlm_tools_bsl.format_detector import detect_format
from rlm_tools_bsl.sandbox_process import ProcessBackendConfig, ProcessSandboxBackend, format_info_to_payload

HANG = "print('hang')\nwhile True:\n    pass\n"


def _config(project_path, **overrides):
    overrides.setdefault("max_output_chars", 10_000)
    overrides.setdefault("execution_timeout_seconds", 45)
    overrides.setdefault("start_timeout_seconds", 60)
    overrides.setdefault("kill_grace_seconds", 1)
    overrides.setdefault("memory_mb", 0)
    overrides.setdefault("test_scan_hold_extra", 3)
    overrides.setdefault("format_info_payload", format_info_to_payload(detect_format(project_path)))
    return ProcessBackendConfig(base_path=project_path, **overrides)


def _close(backend):
    if backend is not None:
        backend.request_close("test_done")
        backend.finish_close(time.monotonic() + 10)


@pytest.fixture(scope="module")
def cf_project(tmp_path_factory):
    return make_cf_project(tmp_path_factory.mktemp("cf_scan_budget"))


@pytest.fixture
def budget(monkeypatch):
    """Бюджет 4 при ширине обхода 4 и СВЕЖИЙ журнал на тест (пул других тестов не
    должен влиять на выдачу)."""
    monkeypatch.setenv("RLM_SCAN_WORKERS", "4")
    monkeypatch.setenv("RLM_SCAN_WORKERS_TOTAL", "4")
    ledger = _scan_budget.ScanLedger()
    monkeypatch.setattr(_scan_budget, "_LEDGER", ledger)
    return ledger


def test_two_process_sessions_share_the_budget(cf_project, budget):
    a = b = c = None
    try:
        a = ProcessSandboxBackend(_config(cf_project, execution_timeout_seconds=2))
        assert budget.array[a._scan_slot] == 3
        b = ProcessSandboxBackend(_config(cf_project))
        assert budget.array[b._scan_slot] == 1, "журнал: 3 + 1 при бюджете 4"
        slot_a = a._scan_slot
        result = a.execute(HANG)
        assert result.sandbox_state is not None and result.sandbox_state["reason"] == "timeout"
        assert budget.array[slot_a] == 0, "потоки убитого воркера вернул родитель"
        c = ProcessSandboxBackend(_config(cf_project))
        assert budget.array[c._scan_slot] == 3
    finally:
        for backend in (a, b, c):
            _close(backend)


def test_crashed_worker_slot_is_reclaimed(cf_project, budget):
    a = None
    try:
        a = ProcessSandboxBackend(_config(cf_project))
        slot = a._scan_slot
        assert budget.array[slot] == 3
        proc = a._proc
        proc.terminate()
        proc.join(10)
        result = a.execute("print(1)")
        assert result.error is not None and "SandboxCrashedError" in result.error
        assert budget.array[slot] == 0
    finally:
        _close(a)


def test_closed_session_returns_its_slot(cf_project, budget):
    a = ProcessSandboxBackend(_config(cf_project))
    slot = a._scan_slot
    assert budget.array[slot] == 3
    a.request_close("test")
    assert a.finish_close(time.monotonic() + 10).closed is True
    assert budget.array[slot] == 0
    assert a._scan_slot is None
    assert budget.allocate() == slot, "слот вернулся в пул"


def test_restarted_generation_reuses_the_slot_cleanly(cf_project, budget):
    a = None
    try:
        a = ProcessSandboxBackend(_config(cf_project, execution_timeout_seconds=2))
        slot = a._scan_slot
        result = a.execute(HANG)
        assert result.sandbox_state is not None and result.sandbox_state["reason"] == "timeout"
        again = a.execute("print('gen2')")
        assert again.generation == 2 and again.error is None
        # Без обнуления на мёртвом корне в слоте остались бы 3 протухших, и новое
        # поколение получило бы 1 при бюджете 4 — в слоте было бы 4.
        assert budget.array[slot] == 3
    finally:
        _close(a)
