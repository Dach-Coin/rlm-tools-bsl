"""Общий бюджет потоков обхода дерева (v1.40.0) — leaf-модуль, только stdlib.

``RLM_SCAN_WORKERS`` задаёт ширину ОДНОГО обхода, а в process-режиме каждая
сессия — отдельный процесс, и внутрипроцессный семафор их не скоординирует: при
``RLM_SCAN_WORKERS=32`` и 20 сессиях одновременно шло до 640 потоков обхода.

Бюджет — разделяемый массив int32 («журнал»): у каждой process-сессии СВОЙ слот,
его единственный писатель — её worker; у всех inline-сессий — один общий слот
процесса-сервера (``INLINE_SLOT``, в пул не входит). Родитель — владелец
ЖИЗНЕННОГО ЦИКЛА слотов process-сессий: обнуляет слот только после
подтверждённой смерти корня worker-а (писателя больше нет).

Межпроцессной блокировки нет намеренно: hard-kill по таймауту бьёт в
произвольную инструкцию, и убитый держатель общей блокировки заклинил бы обход во
ВСЕХ сессиях. Убитый держатель СЛОТА не мешает никому — его удержание обнуляет
родитель.

Аренда оптимистична, по ОДНОМУ токену, и бюджет — предел УСТАНОВИВШЕГОСЯ режима,
а не жёсткая гарантия реального времени. Для строго одновременных заявок ``k``
обходов — две честные границы, обе только на время этих обходов: перебор не
больше ``k·min(RLM_SCAN_WORKERS−1, бюджет)``, недобор не больше
``min(бюджет, k−1)`` токенов.

Считаются ДОПОЛНИТЕЛЬНЫЕ потоки: вызывающий поток обхода — поток самой сессии, он
существует и без бюджета. Выдача 0 означает прежний последовательный обход, а не
ожидание — ожиданий нет вообще.
"""

from __future__ import annotations

import ctypes
import logging
import os
import threading

logger = logging.getLogger(__name__)

ENV_TOTAL = "RLM_SCAN_WORKERS_TOTAL"
DEFAULT_TOTAL = 16
MAX_TOTAL = 512
LEDGER_SLOTS = 256
INLINE_SLOT = 0


def scan_workers_total() -> int:
    """``RLM_SCAN_WORKERS_TOTAL``: дефолт 16, ``0..512``; мусор/отрицательное -> 16, больше 512 -> 512.

    ``0`` — валидное значение: дополнительных потоков нет, каждый обход
    последовательный.
    """
    raw = os.environ.get(ENV_TOTAL, "")
    try:
        n = int(raw) if raw.strip() else DEFAULT_TOTAL
    except ValueError:
        return DEFAULT_TOTAL
    return DEFAULT_TOTAL if n < 0 else min(MAX_TOTAL, n)


class ScanLease:
    """Сторона ОБХОДА: worker process-сессии либо все inline-сессии процесса разом."""

    def __init__(self, array, slot: int | None, total: int) -> None:
        self._array, self._slot, self._total = array, slot, total
        # Замок сериализует обходы ОДНОГО писателя слота: потоки одного worker-а
        # либо все inline-сессии процесса-сервера (у них одна общая аренда).
        self._lock = threading.Lock()

    def acquire(self, want: int) -> int:
        """Сколько ДОПОЛНИТЕЛЬНЫХ потоков выдано (``0..want``)."""
        if want <= 0 or self._slot is None:
            return 0
        granted = 0
        with self._lock:
            held = int(self._array[self._slot])
            # По ОДНОМУ токену: полная заявка разом заставила бы одновременных соседей
            # принять её за удержание и отказаться от свободных токенов (при двух заявках
            # по 3 и бюджете 4 — по 1 каждому). Временная заявка — не больше одного токена.
            while granted < want:
                self._array[self._slot] = held + granted + 1  # публикация одного токена
                if sum(self._array) > self._total:  # сумма — вместе со своим слотом
                    self._array[self._slot] = held + granted  # отказ от последнего
                    break
                granted += 1
        return granted

    def release(self, n: int) -> None:
        if n <= 0 or self._slot is None:
            return
        with self._lock:
            self._array[self._slot] = max(0, int(self._array[self._slot]) - n)


class ScanLedger:
    """Сторона РОДИТЕЛЯ: журнал удержаний и пул слотов process-сессий."""

    def __init__(self, slots: int = LEDGER_SLOTS) -> None:
        try:
            from multiprocessing.sharedctypes import RawArray

            self.array = RawArray(ctypes.c_int32, slots)
            self.shared = True
        except OSError as exc:
            # Среда, где разделяемая память запрещена (MCP-клиент с файловой
            # песочницей), — ровно та, где inline единственный рабочий режим: там
            # межпроцессный журнал не нужен, а отказ здесь сорвал бы rlm_start.
            # Process-режим в такой среде не стартует и без журнала (его пролог
            # создаёт ту же разделяемую память), поэтому журнал процесса остаётся
            # локальным, а process-backend его в worker не передаёт.
            logger.warning("scan budget: shared memory unavailable (%s), ledger is process-local", type(exc).__name__)
            self.array = (ctypes.c_int32 * slots)()
            self.shared = False
        self._lock = threading.Lock()
        # INLINE_SLOT в пул не входит: он принадлежит самому процессу-серверу.
        self._free = [s for s in range(slots - 1, -1, -1) if s != INLINE_SLOT]
        self._exhausted_logged = False
        self._inline_lease: ScanLease | None = None

    def allocate(self) -> int | None:
        """Слот process-сессии либо ``None`` — пул исчерпан (выдача всегда 0)."""
        with self._lock:
            if not self._free:
                if not self._exhausted_logged:
                    self._exhausted_logged = True
                    logger.warning("scan budget: slot pool exhausted, new sessions walk serially")
                return None
            slot = self._free.pop()
            self.array[slot] = 0
            return slot

    def reclaim(self, slot: int | None) -> None:
        """Обнулить слот. Только после ПОДТВЕРЖДЁННОЙ смерти корня: писателя больше нет."""
        if slot is not None:
            self.array[slot] = 0

    def release_slot(self, slot: int | None) -> None:
        """Обнулить и вернуть в пул (корень мёртв, backend финализирован)."""
        if slot is None:
            return
        with self._lock:
            self.array[slot] = 0
            self._free.append(slot)

    def inline_lease(self) -> ScanLease:
        """ОДНА аренда на все inline-сессии процесса — с одним замком, поэтому между
        собой inline-обходы строги. Закрытие inline-сессии бюджет НЕ трогает: прервать
        поток в CPython нельзя, и отцепленный прогрев продолжает работать."""
        with self._lock:
            if self._inline_lease is None:
                self._inline_lease = ScanLease(self.array, INLINE_SLOT, scan_workers_total())
            return self._inline_lease


_LEDGER: ScanLedger | None = None
_LEDGER_LOCK = threading.Lock()


def get_scan_ledger() -> ScanLedger:
    """Ленивый синглтон процесса-сервера."""
    global _LEDGER
    with _LEDGER_LOCK:
        if _LEDGER is None:
            _LEDGER = ScanLedger()
        return _LEDGER
