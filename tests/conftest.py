import sys
from pathlib import Path

# Ensure tests/ is on sys.path so bare imports work on all platforms
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest
from types import SimpleNamespace

from test_bsl_helpers import _make_bsl_fixture


def pytest_configure(config):
    # Register custom marker so `pytest --strict-markers` does not warn.
    config.addinivalue_line(
        "markers",
        "strategy_mode_slim: run test under RLM_STRATEGY_MODE=slim (default in tests is 'full' for back-compat)",
    )


@pytest.fixture(autouse=True)
def _strategy_mode_default(request, monkeypatch):
    """Pin RLM_STRATEGY_MODE for every test based on marker presence.

    Default in tests is ``full`` so existing assertions ("== HELPERS ==",
    "Step 0 — UNDERSTAND", etc.) keep matching. Tests that need slim opt-in
    via ``@pytest.mark.strategy_mode_slim``.

    The fixture sets the env explicitly in BOTH directions — without this,
    a stray ``RLM_STRATEGY_MODE=slim`` in the developer's shell would silently
    flip every legacy assertion to slim and hide regressions.
    """
    mode = "slim" if "strategy_mode_slim" in request.keywords else "full"
    monkeypatch.setenv("RLM_STRATEGY_MODE", mode)


@pytest.fixture(autouse=True)
def _sandbox_mode_default(monkeypatch):
    """Pin RLM_SANDBOX_MODE=inline для существующей suite (§16.3 плана v1.29.0).

    Многие тесты monkeypatch-ят объекты процесса, которые spawn-child не увидит.
    Process-mode integration-тесты ставят RLM_SANDBOX_MODE=process локальным
    monkeypatch.setenv ПОВЕРХ этой autouse-фикстуры.
    """
    monkeypatch.setenv("RLM_SANDBOX_MODE", "inline")


@pytest.fixture(autouse=True)
def _isolate_ext_display_env(monkeypatch):
    """Снять переменные, управляющие агент-facing представлением расширений.

    Существующие тесты ассертят ПОЛНЫЕ списки расширений (напр.
    ``test_strategy_ext_budget.py`` ждёт все 14 ``Расш0..Расш13``). Стрэй
    ``RLM_EXT_LIST_CAP`` в шелле разработчика/CI урезал бы их и уронил тест на
    нерелевантной причине. Снимаем обе переменные перед каждым тестом по образцу
    ``_strategy_mode_default``; тесты, которым нужно конкретное значение,
    ставят его локальным ``monkeypatch.setenv`` поверх этой autouse-фикстуры.
    """
    monkeypatch.delenv("RLM_EXT_LIST_CAP", raising=False)
    monkeypatch.delenv("RLM_EXT_OVERRIDE_DETAIL", raising=False)


@pytest.fixture(autouse=True)
def _isolate_real_home(tmp_path_factory, monkeypatch):
    """Default-isolation: every test writes indexes AND file-cache to tmp dirs.

    Without this:
    - ``IndexBuilder.build()`` without a test-local ``RLM_INDEX_DIR`` patch
      writes a ~360 KiB ``bsl_index.db`` into the developer's real
      ``~/.cache/rlm-tools-bsl/<hash>/``.
    - ``rlm_start`` / cache helpers without a test-local ``RLM_CONFIG_FILE``
      patch resolve ``cache._cache_base()`` to ``Path.home()/.cache/...`` and
      drop ``file_index.json`` files there.

    Found in v1.9.2 smoke test: a single ``pytest -q`` run accumulated 19
    stale ``bsl_index.db`` and 87 stale ``file_index.json`` in real home.

    Three-layer isolation:
    1. Set ``RLM_INDEX_DIR`` → indexes go to a session-shared tmp dir.
    2. Set ``RLM_CONFIG_FILE`` → ``_cache_base()`` resolves to
       ``dirname/cache`` WITHOUT touching ``Path.home()``. Layers 1 and 2 are
       env vars, so they are the only ones that survive ``spawn``: v1.29.0
       process-mode workers are separate processes and do NOT inherit the
       ``Path.home`` patch below. Without this layer every no-index process
       test wrote ``file_index.json`` into the developer's real ``~/.cache``.
    3. Patch ``pathlib.Path.home`` → in-process code that still falls back to
       ``Path.home()/.cache/...`` (migration helper) sees a fake home.

    Tests that explicitly verify fallback behavior (migration tests, cache
    tests) can still override any layer — monkeypatch applies later
    changes on top of this autouse setup.
    """
    import pathlib

    isolated_root = tmp_path_factory.mktemp("rlm_index_root")
    fake_home = tmp_path_factory.mktemp("rlm_fake_home")
    monkeypatch.setenv("RLM_INDEX_DIR", str(isolated_root))
    # Файл намеренно не создаётся: нужен только его dirname. load_config()
    # на отсутствующем пути — no-op.
    monkeypatch.setenv("RLM_CONFIG_FILE", str(fake_home / "service.json"))
    monkeypatch.setattr(pathlib.Path, "home", lambda: fake_home)


@pytest.fixture(autouse=True)
def _isolate_llm_env(monkeypatch):
    """Снять LLM-переменные окружения перед каждым тестом (§18.10.1 плана v1.29.0).

    Критично именно для process mode: worker пробует провайдера по env, а env
    наследуется spawn-ребёнком. Без снятия ``has_llm_tools`` зависел бы от
    того, экспортирован ли ключ в шелле разработчика, и parity-тесты падали бы
    по причине, не связанной с паритетом. Тесты, которым нужен провайдер,
    ставят переменные локальным ``monkeypatch.setenv`` поверх этой фикстуры.
    """
    for var in ("ANTHROPIC_API_KEY", "RLM_LLM_BASE_URL", "RLM_LLM_MODEL", "RLM_LLM_API_KEY"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def bsl_env(tmp_path):
    """Shared BSL test environment with default CF fixture.

    Returns SimpleNamespace with:
        path  – tmp_path (pathlib.Path) where the CF structure lives
        bsl   – dict of BSL helper functions
        helpers – dict of generic helper functions
    """
    tmpdir = str(tmp_path)
    bsl, helpers = _make_bsl_fixture(tmpdir)
    return SimpleNamespace(path=tmp_path, bsl=bsl, helpers=helpers)
