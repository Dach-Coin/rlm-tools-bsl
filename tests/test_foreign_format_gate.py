"""Гейт неподдерживаемых форматов исходников (v1.32.0).

Три класса дерева: наш CF/EDT (включая CFE), чужой формат с ``.bsl`` и чужой
формат без ``.bsl``. Проверяются обе точки решения — сессия ``rlm_start`` и
единственный MCP-гейт публичного ``rlm_index(action='build')``.

Файл самодостаточен: константы дескрипторов дублируются из
``tests/test_format_detector.py`` намеренно — кросс-импортов между тестовыми
файлами нет.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from rlm_tools_bsl.server import (
    _build_jobs,
    _build_jobs_lock,
    _rlm_end,
    _rlm_execute,
    _rlm_projects,
    _rlm_start,
    rlm_index,
)


CF_DESCRIPTOR = """<?xml version="1.0" encoding="UTF-8"?>
<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">
  <Configuration uuid="00000000-0000-0000-0000-000000000001">
    <Properties><Name>Тест</Name></Properties>
  </Configuration>
</MetaDataObject>
"""

EDT_DESCRIPTOR = """<?xml version="1.0" encoding="UTF-8"?>
<mdclass:Configuration
    xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass"
    name="Тест"/>
"""


def _write_descriptor(root: Path, kind: str) -> None:
    if kind == "cf":
        root.mkdir(parents=True, exist_ok=True)
        (root / "Configuration.xml").write_text(CF_DESCRIPTOR, encoding="utf-8")
    else:
        cfg = root / "Configuration"
        cfg.mkdir(parents=True, exist_ok=True)
        (cfg / "Configuration.mdo").write_text(EDT_DESCRIPTOR, encoding="utf-8")


@pytest.fixture()
def supported_cf(tmp_path):
    _write_descriptor(tmp_path, "cf")
    mod = tmp_path / "CommonModules" / "Тест" / "Ext"
    mod.mkdir(parents=True)
    (mod / "Module.bsl").write_text("Процедура Тест() Экспорт\nКонецПроцедуры\n", encoding="utf-8")
    return tmp_path


@pytest.fixture()
def nested_edt_cfe(tmp_path):
    _write_descriptor(tmp_path / "cfe" / "ExtA", "edt")
    (tmp_path / "cfe" / "ExtA" / "Module.bsl").write_text("// code", encoding="utf-8")
    return tmp_path


@pytest.fixture()
def foreign_with_bsl(tmp_path):
    (tmp_path / "Configuration.json").write_text('{"foreign": true}', encoding="utf-8")
    (tmp_path / "Module.bsl").write_text("Процедура Тест()\nКонецПроцедуры\n", encoding="utf-8")
    return tmp_path


@pytest.fixture()
def foreign_no_bsl(tmp_path):
    (tmp_path / "Configuration.json").write_text('{"foreign": true}', encoding="utf-8")
    # тест generic-режима читает notes.txt и ждет "1 | alpha" от numbered read_file
    (tmp_path / "notes.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    return tmp_path


@pytest.fixture()
def foreign_bad_xml_encoding(tmp_path):
    """Дескриптор с незнакомой кодировкой: ElementTree бросает LookupError."""
    (tmp_path / "Configuration.xml").write_text(
        '<?xml version="1.0" encoding="x-invalid"?><MetaDataObject/>', encoding="utf-8"
    )
    (tmp_path / "Module.bsl").write_text("// code", encoding="utf-8")
    return tmp_path


@pytest.fixture()
def foreign_xml_collision(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "Configuration.xml").write_text("<settings/>", encoding="utf-8")
    (tmp_path / "Module.bsl").write_text("// code", encoding="utf-8")
    return tmp_path


# --- rlm_start: три ветки -------------------------------------------------


def test_start_supported_cf_is_unchanged(supported_cf):
    data = json.loads(_rlm_start(path=str(supported_cf), query="обычная сессия"))
    try:
        assert data["source_support"] == "supported"
        assert "UNSUPPORTED SOURCE FORMAT" not in data["strategy"]
        assert any(sig.startswith("find_module(") for sig in data["available_functions"])
    finally:
        _rlm_end(data["session_id"])


def test_start_supported_nested_edt_cfe_is_unchanged(nested_edt_cfe):
    data = json.loads(_rlm_start(path=str(nested_edt_cfe), query="edt cfe"))
    try:
        assert data["source_support"] == "supported"
        assert "UNSUPPORTED SOURCE FORMAT" not in data["strategy"]
    finally:
        _rlm_end(data["session_id"])


def test_start_foreign_with_bsl_warns_and_keeps_bsl_helpers(foreign_with_bsl):
    data = json.loads(_rlm_start(path=str(foreign_with_bsl), query="чужой формат"))
    try:
        assert data["source_support"] == "foreign_with_bsl"
        assert data["warnings"]
        assert "UNSUPPORTED SOURCE FORMAT" in data["warnings"][0]
        assert data["strategy"].lstrip().startswith("== UNSUPPORTED SOURCE FORMAT ==")
        assert any(sig.startswith("find_module(") for sig in data["available_functions"])
    finally:
        _rlm_end(data["session_id"])


def test_start_foreign_no_bsl_uses_generic_helpers_only(foreign_no_bsl):
    data = json.loads(_rlm_start(path=str(foreign_no_bsl), query="не 1С"))
    try:
        assert data["source_support"] == "foreign_no_bsl"
        assert not any(sig.startswith("find_module(") for sig in data["available_functions"])
        assert any(sig.startswith("read_file(") for sig in data["available_functions"])
        assert "numbered" in " ".join(data["available_functions"]).lower()
        assert "GENERIC MODE" in data["strategy"]
        assert data["warnings"] and "GENERIC" in data["warnings"][0].upper()

        result = json.loads(_rlm_execute(session_id=data["session_id"], code="print(read_file('notes.txt'))"))
        assert result["error"] is None
        assert "1 | alpha" in result["stdout"]
    finally:
        _rlm_end(data["session_id"])


def test_start_foreign_no_bsl_has_no_bsl_helpers_in_sandbox(foreign_no_bsl):
    """Хелперы не только не заявлены в available_functions — их нет в namespace."""
    data = json.loads(_rlm_start(path=str(foreign_no_bsl), query="не 1С"))
    try:
        result = json.loads(_rlm_execute(session_id=data["session_id"], code="print(find_module('X'))"))
        assert result["error"] is not None
        assert "find_module" in result["error"]
    finally:
        _rlm_end(data["session_id"])


def test_format_warning_precedes_server_limit_banner(foreign_with_bsl):
    """Оба блока в одной стратегии: предупреждение о формате обязано быть ВЫШЕ
    баннера лимитов (иначе агент узнаёт про чужой формат после инструкций)."""
    data = json.loads(
        _rlm_start(path=str(foreign_with_bsl), query="чужой формат", effort="medium", max_execute_calls=7)
    )
    try:
        strategy = data["strategy"]
        fmt_at = strategy.find("== UNSUPPORTED SOURCE FORMAT ==")
        banner_at = strategy.find("== SERVER LIMIT OVERRIDE ==")
        assert fmt_at == 0, "предупреждение о формате не в начале стратегии"
        assert banner_at > 0, "баннер лимитов не сработал — тест перестал стеречь порядок"
        assert fmt_at < banner_at
    finally:
        _rlm_end(data["session_id"])


def test_start_foreign_xml_name_collision_still_warns(foreign_xml_collision):
    data = json.loads(_rlm_start(path=str(foreign_xml_collision), query="collision"))
    try:
        assert data["source_support"] == "foreign_with_bsl"
        assert "UNSUPPORTED SOURCE FORMAT" in data["warnings"][0]
    finally:
        _rlm_end(data["session_id"])


# --- rlm_start: тот же контракт в process-режиме --------------------------


@pytest.mark.parametrize("fixture_name", ["foreign_no_bsl", "supported_cf"])
def test_process_mode_matches_inline_helper_set(request, monkeypatch, fixture_name):
    """Прокидывание enable_bsl_helpers в worker: тот же состав хелперов и
    та же нумерация строк, что и inline."""
    monkeypatch.setenv("RLM_SANDBOX_MODE", "process")
    base = request.getfixturevalue(fixture_name)
    generic = fixture_name == "foreign_no_bsl"

    data = json.loads(_rlm_start(path=str(base), query="process mode"))
    try:
        assert "error" not in data, data
        assert data["limits"]["sandbox_mode"] == "process"
        has_bsl = any(sig.startswith("find_module(") for sig in data["available_functions"])
        assert has_bsl is (not generic)
        if generic:
            result = json.loads(_rlm_execute(session_id=data["session_id"], code="print(read_file('notes.txt'))"))
            assert result["error"] is None
            assert "1 | alpha" in result["stdout"]
    finally:
        _rlm_end(data["session_id"])


# --- MCP build gate -------------------------------------------------------


def test_mcp_build_gate_allows_supported(supported_cf):
    from rlm_tools_bsl.server import _unsupported_format_build_error

    assert _unsupported_format_build_error(str(supported_cf)) is None


def test_mcp_build_gate_allows_nested_edt_cfe(nested_edt_cfe):
    from rlm_tools_bsl.server import _unsupported_format_build_error

    assert _unsupported_format_build_error(str(nested_edt_cfe)) is None


def test_mcp_build_gate_refuses_foreign_without_bsl(foreign_no_bsl):
    from rlm_tools_bsl.server import _unsupported_format_build_error

    out = json.loads(_unsupported_format_build_error(str(foreign_no_bsl)))
    assert out["source_support"] == "foreign_no_bsl"
    assert ".bsl" in out["error"]


def test_mcp_build_gate_points_foreign_bsl_to_cli(foreign_with_bsl):
    from rlm_tools_bsl.server import _unsupported_format_build_error

    out = json.loads(_unsupported_format_build_error(str(foreign_with_bsl)))
    assert out["source_support"] == "foreign_with_bsl"
    assert "--allow-unsupported-format" in out["error"]


@pytest.mark.parametrize(
    ("fixture_name", "expected_walks"),
    [("supported_cf", 0), ("foreign_with_bsl", 1), ("foreign_no_bsl", 1)],
)
@pytest.mark.parametrize("gate", ["mcp", "cli"])
def test_build_gates_walk_the_tree_at_most_once(request, monkeypatch, fixture_name, expected_walks, gate):
    """Приёмка: оба гейта build обходят дерево не более одного раза.

    `classify_source` сам зовёт `probe_bsl`, поэтому его использование в гейте
    дало бы двойной обход на чужом дереве. Подменяем ОБА имени в модуле:
    гейты импортируют их внутри функции, то есть на момент вызова."""
    import rlm_tools_bsl.format_detector as fd

    base = str(request.getfixturevalue(fixture_name))
    calls = {"probe": 0}
    real_probe = fd.probe_bsl

    def counting_probe(path):
        calls["probe"] += 1
        return real_probe(path)

    def _forbidden(*_a, **_kw):
        raise AssertionError("гейт build не должен звать classify_source — это двойной обход")

    monkeypatch.setattr(fd, "probe_bsl", counting_probe)
    monkeypatch.setattr(fd, "classify_source", _forbidden)

    if gate == "mcp":
        from rlm_tools_bsl.server import _unsupported_format_build_error

        result = _unsupported_format_build_error(base)
        assert (result is None) is (fixture_name == "supported_cf")
    else:
        import sys

        from rlm_tools_bsl.cli import _gate_unsupported_format

        # Явный не-tty: под `pytest -s` stdin остался бы терминалом, и ветка
        # с вопросом заблокировала бы прогон на input().
        monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
        if fixture_name == "supported_cf":
            _gate_unsupported_format(base, allow=False)
        else:
            with pytest.raises(SystemExit):
                _gate_unsupported_format(base, allow=False)

    assert calls["probe"] == expected_walks


def test_poisoned_neighbour_descriptor_does_not_kill_the_session(tmp_path):
    """Битый дескриптор У СОСЕДА не имеет права ронять чужую сессию.

    Скан соседних конфигураций доходит до `сосед/обёртка/Configuration.xml`,
    а `ET.parse` на незнакомой кодировке бросает LookupError — не ParseError.
    Без перехвата `rlm_start` возвращал `Session init failed: LookupError`
    для каталога, к которому битый файл вообще не относится."""
    neighbour = tmp_path / "neighbour" / "wrapper"
    neighbour.mkdir(parents=True)
    (neighbour / "Configuration.xml").write_text(
        '<?xml version="1.0" encoding="x-invalid"?><MetaDataObject/>', encoding="utf-8"
    )

    session_dir = tmp_path / "session"
    session_dir.mkdir()
    (session_dir / "Configuration.json").write_text('{"foreign": true}', encoding="utf-8")
    (session_dir / "Module.bsl").write_text("// code", encoding="utf-8")

    data = json.loads(_rlm_start(path=str(session_dir), query="сосед с битой кодировкой"))
    try:
        assert "error" not in data, data
        assert data["source_support"] == "foreign_with_bsl"
    finally:
        _rlm_end(data["session_id"])


def test_both_gates_survive_unparseable_xml_encoding(foreign_bad_xml_encoding, monkeypatch, capsys):
    """Оба гейта обязаны быть тотальными: нечитаемый дескриптор = «не наш формат»,
    а не необработанное исключение на входе build."""
    import sys

    from rlm_tools_bsl.cli import _gate_unsupported_format
    from rlm_tools_bsl.server import _unsupported_format_build_error

    base = str(foreign_bad_xml_encoding)

    out = json.loads(_unsupported_format_build_error(base))
    assert out["source_support"] == "foreign_with_bsl"
    assert "--allow-unsupported-format" in out["error"]

    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    with pytest.raises(SystemExit) as excinfo:
        _gate_unsupported_format(base, allow=False)
    assert excinfo.value.code == 1
    assert "--allow-unsupported-format" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_mcp_build_refuses_before_job_registration():
    """Отказ приходит ДО регистрации фоновой job — job-слот не занимается."""
    from rlm_tools_bsl.projects import _reset_registry

    with tempfile.TemporaryDirectory() as tmpdir:
        src = os.path.join(tmpdir, "src")
        os.makedirs(src)
        Path(src, "Configuration.json").write_text('{"foreign": true}', encoding="utf-8")
        Path(src, "Module.bsl").write_text("Процедура Тест()\nКонецПроцедуры\n", encoding="utf-8")

        with _build_jobs_lock:
            _build_jobs.clear()
        _reset_registry()
        with patch.dict(
            os.environ,
            {
                "RLM_CONFIG_FILE": os.path.join(tmpdir, "service.json"),
                "RLM_INDEX_DIR": os.path.join(tmpdir, "indexes"),
            },
        ):
            _reset_registry()
            _rlm_projects(action="add", name="ForeignProj", path=src, password="pw")
            out = json.loads(await rlm_index(action="build", project="ForeignProj", confirm="pw"))
            assert "started" not in out
            assert out["source_support"] == "foreign_with_bsl"
            assert not _build_jobs
        with _build_jobs_lock:
            _build_jobs.clear()


@pytest.mark.asyncio
async def test_mcp_build_gate_does_not_block_supported_project():
    """Наш CF проходит гейт: build стартует в фоне как раньше."""
    import threading as _th

    from rlm_tools_bsl.projects import _reset_registry
    from rlm_tools_bsl.server import _rlm_index

    with tempfile.TemporaryDirectory() as tmpdir:
        src = os.path.join(tmpdir, "src")
        os.makedirs(src)
        _write_descriptor(Path(src), "cf")
        Path(src, "Module.bsl").write_text("Процедура Тест()\nКонецПроцедуры\n", encoding="utf-8")

        with _build_jobs_lock:
            _build_jobs.clear()
        _reset_registry()
        with patch.dict(
            os.environ,
            {
                "RLM_CONFIG_FILE": os.path.join(tmpdir, "service.json"),
                "RLM_INDEX_DIR": os.path.join(tmpdir, "indexes"),
            },
        ):
            _reset_registry()
            _rlm_projects(action="add", name="OkProj", path=src, password="pw")
            out = json.loads(await rlm_index(action="build", project="OkProj", confirm="pw"))
            assert out["started"] is True
            for t in _th.enumerate():
                if t.name == "build-OkProj":
                    t.join(timeout=30)
            _rlm_index(action="drop", path=src)
        with _build_jobs_lock:
            _build_jobs.clear()
