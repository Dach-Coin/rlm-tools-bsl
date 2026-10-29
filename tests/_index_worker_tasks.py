"""Задачи-заглушки для spawn-воркеров `_index_workers` (тесты v1.48.0): модуль уровня tests, импортируемый
дочерним интерпретатором по имени — функции из тестового модуля в spawn-ребёнке пришлось бы импортировать вместе с
pytest."""

from __future__ import annotations

import logging
import os
import time


def echo(value, delay: float = 0.0):
    if delay:
        time.sleep(delay)
    return value


def warn_and_return(value):
    logging.getLogger("rlm_tools_bsl.bsl_index").warning("worker warning %s", value)
    return value


def boom(message: str):
    raise ValueError(message)


def die():
    os._exit(7)


def worker_pid(_value=None):
    return os.getpid()
