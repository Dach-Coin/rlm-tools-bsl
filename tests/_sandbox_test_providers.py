"""Fake LLM-провайдеры для process-mode тестов (v1.29.0, §18.7).

Импортируются В SPAWN CHILD по dotted-path из ``ProcessBackendConfig.
test_llm_provider`` (spawn передаёт sys.path родителя, tests/ в нём есть).
Родительский monkeypatch в child не действует, поэтому сигналинг — через
файлы, пути к которым приходят в env (env наследуется child-ом).

Env-переменные:
  RLM_TEST_LLM_SIGNAL_FILE — hang-провайдер создаёт файл ПЕРЕД зависанием;
  RLM_TEST_LLM_CALLS_FILE  — counting-провайдер дописывает строку на каждый вызов;
  RLM_TEST_CHILD_PID_FILE  — spawning-провайдер пишет PID запущенного descendant.
"""

import os
import subprocess
import sys
import time


def echo_provider():
    def provider(prompt, context=""):
        return f"echo:{prompt}"

    return provider


def counting_provider():
    calls_file = os.environ["RLM_TEST_LLM_CALLS_FILE"]

    def provider(prompt, context=""):
        with open(calls_file, "a", encoding="utf-8") as f:
            f.write(prompt + "\n")
        return f"counted:{prompt}"

    return provider


def hang_provider():
    """Сигналит о входе в provider call и виснет — parent должен убить worker,
    а зарезервированная quota обязана пережить kill (§12.2 тест а)."""
    signal_file = os.environ["RLM_TEST_LLM_SIGNAL_FILE"]

    def provider(prompt, context=""):
        with open(signal_file, "w", encoding="utf-8") as f:
            f.write("entered")
        time.sleep(300)
        return "never"

    return provider


def failing_factory():
    """Ошибка ПОЗДНЕЙ инициализации client: probe прошёл, factory падает —
    первый вызов возвращает bounded error без расхода quota (§18.7)."""
    raise RuntimeError("simulated late client-init failure")


def spawning_provider():
    """Запускает descendant-процесс (sys.executable sleep), пишет его PID и
    виснет — после parent hard timeout descendant обязан исчезнуть (§18.3.5)."""
    pid_file = os.environ["RLM_TEST_CHILD_PID_FILE"]

    def provider(prompt, context=""):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
        with open(pid_file, "w", encoding="utf-8") as f:
            f.write(str(child.pid))
        time.sleep(300)
        return "never"

    return provider
