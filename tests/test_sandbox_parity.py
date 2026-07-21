"""v1.29.0 этап 8: parity inline vs process — одинаковые запросы через оба backend
на одних fixtures дают идентичные публичные результаты (без изменения search/helper
contracts). Сравнивается stdout/error; timing/PID/generation исключены by design."""

import shutil
import subprocess
import time

import pytest

from _process_test_utils import make_cf_project
from rlm_tools_bsl.bsl_index import IndexBuilder
from rlm_tools_bsl.format_detector import detect_format
from rlm_tools_bsl.sandbox import Sandbox
from rlm_tools_bsl.sandbox_backend import InlineSandboxBackend
from rlm_tools_bsl.sandbox_process import ProcessBackendConfig, ProcessSandboxBackend, format_info_to_payload

# Публичные результаты хелперов детерминированы на замороженной фикстуре.
PARITY_CODES = [
    # generic IO
    "print(read_file('CommonModules/МойМодуль/Ext/Module.bsl'))",
    "print(sorted(glob_files('**/*.bsl')))",
    "print([(m['file'], m['line']) for m in grep('Экспорт', 'CommonModules')])",
    # discovery / code
    "r = find_module('МойМодуль')\nprint(r if isinstance(r, str) else sorted(str(x) for x in r))",
    "procs = extract_procedures('CommonModules/МойМодуль/Ext/Module.bsl')\nprint([(p['name'], p['is_export']) for p in procs])",
    "print(read_procedure('CommonModules/МойМодуль/Ext/Module.bsl', 'ПолучитьДатуСеанса'))",
    # composite (индексный путь)
    "p = get_object_profile('ТестовыйДокумент', sections=['structure','modules'])\nprint(p['object_name'], sorted(p['sections'].keys()))",
    # ошибки контрактов — тексты хинтов тоже часть публичного поведения
    "unknown_helper_name()",
]


@pytest.fixture(scope="module")
def parity_env(tmp_path_factory):
    root = tmp_path_factory.mktemp("parity")
    project = make_cf_project(root / "cf")
    import os

    os.environ.setdefault("RLM_INDEX_DIR", str(root / "idx"))  # уже изолирован conftest-ом на session tmp
    db_path = IndexBuilder().build(project)
    return project, str(db_path)


def _inline_backend(project, db_path):
    from rlm_tools_bsl.bsl_index import IndexReader

    reader = IndexReader(db_path) if db_path else None
    sandbox = Sandbox(
        base_path=project,
        max_output_chars=50_000,
        format_info=detect_format(project),
        idx_reader=reader,
        extension_paths=[],
    )
    # install_llm_tools=True намеренно: обе стороны должны ОДИНАКОВО решить по
    # одному и тому же окружению. conftest снимает LLM-переменные, поэтому обе
    # дают False детерминированно; жёсткий False на inline-стороне превратил бы
    # сравнение в тавтологию и скрыл расхождение probe-логики.
    return InlineSandboxBackend(sandbox, reader, install_llm_tools=True)


def _process_backend(project, db_path):
    return ProcessSandboxBackend(
        ProcessBackendConfig(
            base_path=project,
            max_output_chars=50_000,
            execution_timeout_seconds=60,
            format_info_payload=format_info_to_payload(detect_format(project)),
            db_path=db_path,
            index_expected=db_path is not None,
            memory_mb=0,
        )
    )


def _close(b):
    b.request_close("parity_done")
    b.finish_close(time.monotonic() + 10)


def _run_parity(project, db_path, codes):
    inline = _inline_backend(project, db_path)
    try:
        # Второй backend создаётся ВНУТРИ try: если spawn упадёт, inline (а с ним
        # открытый IndexReader → SQLite handle на Windows) обязан быть закрыт.
        process = _process_backend(project, db_path)
    except Exception:
        _close(inline)
        raise
    try:
        assert set(inline.registry_snapshot.keys()) == set(process.registry_snapshot.keys())
        assert inline.detected_prefixes == process.detected_prefixes
        assert inline.has_llm_tools == process.has_llm_tools
        for code in codes:
            ri = inline.execute(code)
            rp = process.execute(code)
            assert ri.stdout == rp.stdout, f"stdout mismatch for: {code}\ninline={ri.stdout!r}\nprocess={rp.stdout!r}"
            assert (ri.error is None) == (rp.error is None), f"error presence mismatch for: {code}"
            if ri.error is not None:
                # Полный traceback различается путями/адресами; сверяем последнюю
                # содержательную строку (тип ошибки) и наличие HINT-блока.
                assert ri.error.splitlines()[-1] == rp.error.splitlines()[-1], code
    finally:
        _close(inline)
        _close(process)


def test_parity_with_index(parity_env):
    project, db_path = parity_env
    _run_parity(project, db_path, PARITY_CODES)


def test_parity_no_index_live_fallback(parity_env):
    project, _db = parity_env
    codes = [c for c in PARITY_CODES if "get_object_profile" not in c]
    _run_parity(project, None, codes)


def test_parity_detail_variables(parity_env):
    project, db_path = parity_env
    inline = _inline_backend(project, db_path)
    try:
        process = _process_backend(project, db_path)
    except Exception:
        _close(inline)
        raise
    try:
        code = "alpha = 1\nbeta = [1, 2]\nprint('ok')"
        ri, rp = inline.execute(code), process.execute(code)
        assert ri.stdout == rp.stdout == "ok\n"
        assert set(ri.variables) == set(rp.variables)
    finally:
        _close(inline)
        _close(process)


@pytest.mark.skipif(not shutil.which("git"), reason="git недоступен")
def test_parity_git_search_registration(tmp_path):
    """§17.8 git-кейсы: (а) work-tree + git → helper есть в ОБОИХ; (б) не под
    git → отсутствует в обоих. Кейс (в) 'git недоступен' покрыт skip-условием
    и live/no-index на регистрацию не влияет (§23.15)."""
    repo_project = make_cf_project(tmp_path / "repo")
    subprocess.run(["git", "init", "-q"], cwd=repo_project, check=True, capture_output=True)
    plain_project = make_cf_project(tmp_path / "plain")

    for project, expected in ((repo_project, True), (plain_project, False)):
        inline = _inline_backend(project, None)
        try:
            process = _process_backend(project, None)
        except Exception:
            _close(inline)
            raise
        try:
            assert ("git_search" in inline.registry_snapshot) is expected, project
            assert ("git_search" in process.registry_snapshot) is expected, project
            if expected:
                ri = inline.execute("print(git_search('Экспорт')['total_matches'] >= 0)")
                rp = process.execute("print(git_search('Экспорт')['total_matches'] >= 0)")
                assert (ri.error is None) == (rp.error is None)
        finally:
            _close(inline)
            _close(process)


# ---------------------------------------------------------------------------
# §18.7: worker-копия provider-гейтов не должна разъезжаться с llm_bridge
# ---------------------------------------------------------------------------

# (base_url, model, api_key, anthropic_key) -> ожидаемая доступность LLM-хелперов
LLM_PROBE_MATRIX = [
    # OpenAI-путь: base URL требует model; ключ опционален
    ("https://api.example/v1", "gpt-x", "sk-1", None, True),
    ("https://api.example/v1", "gpt-x", None, None, True),
    # base URL без model → хелперов нет И fallback к Anthropic НЕ происходит
    ("https://api.example/v1", None, "sk-1", "sk-ant", False),
    ("https://api.example/v1", "", None, "sk-ant", False),
    # Anthropic выбирается только при отсутствии base URL
    (None, None, None, "sk-ant", True),
    # Ничего не задано
    (None, None, None, None, False),
]


@pytest.mark.parametrize("base_url,model,api_key,anthropic_key,expected", LLM_PROBE_MATRIX)
def test_worker_llm_probe_matches_llm_bridge(monkeypatch, base_url, model, api_key, anthropic_key, expected):
    """``_probe_llm_available`` — рукописная копия гейтов ``get_llm_query_fn``
    (worker не имеет права импортировать llm_bridge на init). Копия обязана
    давать тот же вердикт, иначе хелпер молча появится/исчезнет из
    ``available_functions``. Пакеты openai/anthropic присутствуют в тестовом
    окружении, поэтому сравниваем именно env-логику."""
    from rlm_tools_bsl.llm_bridge import get_llm_query_fn
    from rlm_tools_bsl.sandbox_worker import _probe_llm_available

    for name, value in (
        ("RLM_LLM_BASE_URL", base_url),
        ("RLM_LLM_MODEL", model),
        ("RLM_LLM_API_KEY", api_key),
        ("ANTHROPIC_API_KEY", anthropic_key),
    ):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)

    assert _probe_llm_available(None) is expected
    # Эталон: фабрика вернула callable ⇔ probe сказал "доступно".
    assert (get_llm_query_fn() is not None) is expected


def test_worker_llm_probe_test_provider_bypasses_env(monkeypatch):
    """Test-only инъекция провайдера не зависит от env (§18.7 про dependency
    injection в spawn-child)."""
    from rlm_tools_bsl.sandbox_worker import _probe_llm_available

    monkeypatch.delenv("RLM_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert _probe_llm_available("_sandbox_test_providers:make_echo_provider") is True
