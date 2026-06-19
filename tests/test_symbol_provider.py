from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path


def _write_provider_fixture(root: Path) -> Path:
    project = root / "Проект с пробелом"
    module_dir = project / "CommonModules" / "СкладскойЖурнал" / "Ext"
    module_dir.mkdir(parents=True)
    (module_dir / "Module.bsl").write_text(
        textwrap.dedent(
            """\
            Процедура ПараметрыЗаполненияЗаписейСкладскогоЖурнала() Экспорт
                Возврат;
            КонецПроцедуры

            Функция НайтиПартии(Параметр)
                Возврат Параметр;
            КонецФункции
            """
        ),
        encoding="utf-8-sig",
    )
    object_ext_dir = project / "Catalogs" / "Склады" / "Ext"
    object_ext_dir.mkdir(parents=True)
    (object_ext_dir / "ManagerModule.bsl").write_text(
        "Процедура НайтиСклад() Экспорт\nКонецПроцедуры\n",
        encoding="utf-8-sig",
    )
    (object_ext_dir / "Catalog.xml").write_text(
        textwrap.dedent(
            """\
            <?xml version="1.0" encoding="UTF-8"?>
            <MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"
                            xmlns:mdclass="http://v8.1c.ru/8.3/MDClasses"
                            xmlns:v8="http://v8.1c.ru/8.1/data/core">
              <Catalog>
                <Properties>
                  <Name>Склады</Name>
                  <Synonym>
                    <v8:item>
                      <v8:lang>ru</v8:lang>
                      <v8:content>Склады организации</v8:content>
                    </v8:item>
                  </Synonym>
                </Properties>
              </Catalog>
            </MetaDataObject>
            """
        ),
        encoding="utf-8",
    )
    return project


def _build_index(project: Path) -> None:
    from rlm_tools_bsl.bsl_index import IndexBuilder

    IndexBuilder().build(str(project))


def test_provider_shapes_method_object_and_file_rows() -> None:
    from rlm_tools_bsl.symbol_provider import (
        PROVIDER_SCHEMA_VERSION,
        shape_file_candidate,
        shape_method_candidate,
        shape_object_candidate,
    )

    method = shape_method_candidate(
        {
            "name": "ПараметрыЗаполненияЗаписейСкладскогоЖурнала",
            "type": "procedure",
            "is_export": 1,
            "line": 10,
            "end_line": 12,
            "params": "",
            "module_path": "CommonModules/СкладскойЖурнал/Ext/Module.bsl",
            "object_name": "СкладскойЖурнал",
            "rank": -1.25,
        }
    )
    assert PROVIDER_SCHEMA_VERSION == 1
    assert method["kind"] == "method"
    assert method["relativePath"] == "CommonModules/СкладскойЖурнал/Ext/Module.bsl"
    assert method["symbolName"] == "ПараметрыЗаполненияЗаписейСкладскогоЖурнала"
    assert method["startLine"] == 10
    assert method["endLine"] == 12
    assert method["isExport"] is True
    assert method["source"] == "methods_fts"

    obj = shape_object_candidate(
        {
            "object_name": "Склады",
            "category": "Catalogs",
            "synonym": "Catalogs: Склады организации",
            "file": "Catalogs/Склады/Склады.xml",
        }
    )
    assert obj["kind"] == "object"
    assert obj["objectName"] == "Склады"
    assert obj["objectKind"] == "Catalogs"
    assert obj["relativePath"] == "Catalogs/Склады/Склады.xml"
    assert obj["source"] == "object_synonyms"

    file_candidate = shape_file_candidate("CommonModules/СкладскойЖурнал/Ext/Module.bsl")
    assert file_candidate == {
        "kind": "file",
        "relativePath": "CommonModules/СкладскойЖурнал/Ext/Module.bsl",
        "symbolName": "Module.bsl",
        "source": "file_paths",
    }


def test_provider_query_reuses_index_reader_results(tmp_path) -> None:
    from rlm_tools_bsl.bsl_index import IndexReader, get_index_db_path
    from rlm_tools_bsl.symbol_provider import query_symbol_provider

    project = _write_provider_fixture(tmp_path)
    _build_index(project)

    response = query_symbol_provider(
        str(project),
        "ПараметрыЗаполненияЗаписейСкладскогоЖурнала",
        limit=10,
    )

    assert response["status"] == "available"
    assert response["schemaVersion"] == 1
    assert response["provider"] == "rlm-tools-bsl"
    assert response["sourceRoot"] == str(project.resolve())
    assert response["capabilities"]["hasMethodsFts"] is True
    assert response["capabilities"]["hasFilePaths"] is True
    assert response["diagnostics"]["elapsedMs"] >= 0
    method_candidates = [c for c in response["candidates"] if c["kind"] == "method"]
    assert method_candidates
    assert method_candidates[0]["symbolName"] == "ПараметрыЗаполненияЗаписейСкладскогоЖурнала"
    assert method_candidates[0]["relativePath"].endswith("CommonModules/СкладскойЖурнал/Ext/Module.bsl")

    reader = IndexReader(get_index_db_path(str(project.resolve())))
    try:
        existing_methods = reader.search_methods("ПараметрыЗаполненияЗаписейСкладскогоЖурнала", limit=3)
        existing_objects = reader.search_objects("Склады", limit=3)
    finally:
        reader.close()
    assert existing_methods[0]["name"] == "ПараметрыЗаполненияЗаписейСкладскогоЖурнала"
    assert existing_objects is not None
    assert existing_objects[0]["object_name"] == "Склады"


def test_provider_cli_outputs_json_only_for_cyrillic_query_and_path_with_spaces(tmp_path) -> None:
    project = _write_provider_fixture(tmp_path)
    _build_index(project)
    env = os.environ.copy()

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "rlm_tools_bsl.cli",
            "provider",
            "query",
            str(project),
            "ПараметрыЗаполненияЗаписейСкладскогоЖурнала",
            "--limit",
            "5",
            "--json",
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )

    assert result.returncode == 0
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload["status"] == "available"
    assert payload["query"] == "ПараметрыЗаполненияЗаписейСкладскогоЖурнала"
    assert len(payload["candidates"]) <= 5


def test_provider_reports_missing_index_without_mutation(tmp_path) -> None:
    from rlm_tools_bsl.bsl_index import get_index_db_path
    from rlm_tools_bsl.symbol_provider import query_symbol_provider

    project = _write_provider_fixture(tmp_path)
    db_path = get_index_db_path(str(project.resolve()))
    response = query_symbol_provider(str(project), "Склады")

    assert response["status"] == "missing_index"
    assert response["candidates"] == []
    assert not db_path.exists()


def test_provider_does_not_migrate_legacy_method_index(tmp_path) -> None:
    from rlm_tools_bsl.bsl_index import get_index_dir
    from rlm_tools_bsl.symbol_provider import query_symbol_provider
    import hashlib

    project = _write_provider_fixture(tmp_path)
    source_root = str(project.resolve())
    index_dir = get_index_dir(source_root) / hashlib.md5(source_root.encode()).hexdigest()[:12]
    index_dir.mkdir(parents=True)
    legacy_db = index_dir / "method_index.db"
    legacy_db.write_bytes(b"legacy")

    response = query_symbol_provider(str(project), "Склады")

    assert response["status"] == "missing_index"
    assert legacy_db.exists()
    assert not (index_dir / "bsl_index.db").exists()


def test_provider_reports_busy_without_querying_locked_index(tmp_path) -> None:
    from rlm_tools_bsl.bsl_index import get_index_db_path
    from rlm_tools_bsl.symbol_provider import query_symbol_provider

    project = _write_provider_fixture(tmp_path)
    _build_index(project)
    db_path = get_index_db_path(str(project.resolve()))
    lock_path = db_path.parent / "bsl_index.lock"
    lock_path.write_text("locked", encoding="utf-8")

    response = query_symbol_provider(str(project), "Склады")

    assert response["status"] == "busy"
    assert response["diagnostics"]["reason"] == "index lock file exists"


def test_provider_reports_stale_without_update(tmp_path, monkeypatch) -> None:
    from rlm_tools_bsl.bsl_index import get_index_db_path
    from rlm_tools_bsl.symbol_provider import query_symbol_provider

    project = _write_provider_fixture(tmp_path)
    _build_index(project)
    db_path = get_index_db_path(str(project.resolve()))
    before_mtime = db_path.stat().st_mtime

    monkeypatch.setenv("RLM_INDEX_MAX_AGE_DAYS", "0")
    response = query_symbol_provider(str(project), "Склады")

    assert response["status"] == "stale"
    assert response["diagnostics"]["indexStatus"] == "stale_age"
    assert db_path.stat().st_mtime == before_mtime


def test_provider_cli_missing_index_stdout_is_parseable_json(tmp_path) -> None:
    project = _write_provider_fixture(tmp_path)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "rlm_tools_bsl.cli",
            "provider",
            "query",
            str(project),
            "Склады",
            "--json",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload["status"] == "missing_index"


def test_provider_query_returns_error_status_without_traceback(tmp_path) -> None:
    from rlm_tools_bsl.symbol_provider import query_symbol_provider

    missing_path = tmp_path / "нет такого каталога"
    response = query_symbol_provider(str(missing_path), "Склады")

    assert response["status"] == "error"
    assert response["diagnostics"]["errorType"] == "FileNotFoundError"


def test_provider_export_returns_snapshot_with_methods_synonyms_and_fingerprint(tmp_path) -> None:
    from rlm_tools_bsl.symbol_provider import export_symbol_snapshot

    project = _write_provider_fixture(tmp_path)
    _build_index(project)

    response = export_symbol_snapshot(str(project))

    assert response["status"] == "available"
    assert response["schemaVersion"] == 1
    assert response["provider"] == "rlm-tools-bsl"
    assert response["sourceRoot"] == str(project.resolve())
    assert response["sourceFingerprint"]
    assert response["capabilities"]["hasMethods"] is True
    assert response["capabilities"]["hasObjects"] is True
    assert response["capabilities"]["hasFilePaths"] is True
    assert response["diagnostics"]["indexStatus"] == "fresh"
    assert response["diagnostics"]["stats"]["modules"] >= 2

    files = {item["relativePath"]: item for item in response["files"]}
    module = files["CommonModules/СкладскойЖурнал/Ext/Module.bsl"]
    assert module["objectName"] == "СкладскойЖурнал"
    method = next(
        item
        for item in module["symbols"]
        if item["name"] == "ПараметрыЗаполненияЗаписейСкладскогоЖурнала"
    )
    assert method["declarationKind"] == "procedure"
    assert method["startLine"] == 1
    assert method["endLine"] == 3
    assert method["isExport"] is True

    catalog_module = files["Catalogs/Склады/Ext/ManagerModule.bsl"]
    assert catalog_module["objectName"] == "Склады"
    assert catalog_module["objectKind"] == "Catalogs"
    assert "Склады организации" in catalog_module["synonyms"]


def test_provider_export_cli_outputs_json_only_for_cyrillic_path_with_spaces(tmp_path) -> None:
    project = _write_provider_fixture(tmp_path)
    _build_index(project)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "rlm_tools_bsl.cli",
            "provider",
            "export",
            str(project),
            "--json",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload["status"] == "available"
    assert payload["files"]


def test_provider_export_reports_missing_index_without_mutation(tmp_path) -> None:
    from rlm_tools_bsl.bsl_index import get_index_db_path
    from rlm_tools_bsl.symbol_provider import export_symbol_snapshot

    project = _write_provider_fixture(tmp_path)
    db_path = get_index_db_path(str(project.resolve()))
    response = export_symbol_snapshot(str(project))

    assert response["status"] == "missing_index"
    assert response["files"] == []
    assert not db_path.exists()


def test_provider_export_reports_busy_without_reading_locked_index(tmp_path) -> None:
    from rlm_tools_bsl.bsl_index import get_index_db_path
    from rlm_tools_bsl.symbol_provider import export_symbol_snapshot

    project = _write_provider_fixture(tmp_path)
    _build_index(project)
    db_path = get_index_db_path(str(project.resolve()))
    (db_path.parent / "bsl_index.lock").write_text("locked", encoding="utf-8")

    response = export_symbol_snapshot(str(project))

    assert response["status"] == "busy"
    assert response["diagnostics"]["reason"] == "index lock file exists"
    assert response["files"] == []


def test_provider_export_reports_stale_without_update(tmp_path, monkeypatch) -> None:
    from rlm_tools_bsl.bsl_index import get_index_db_path
    from rlm_tools_bsl.symbol_provider import export_symbol_snapshot

    project = _write_provider_fixture(tmp_path)
    _build_index(project)
    db_path = get_index_db_path(str(project.resolve()))
    before_mtime = db_path.stat().st_mtime

    monkeypatch.setenv("RLM_INDEX_MAX_AGE_DAYS", "0")
    response = export_symbol_snapshot(str(project))

    assert response["status"] == "stale"
    assert response["diagnostics"]["indexStatus"] == "stale_age"
    assert response["files"] == []
    assert db_path.stat().st_mtime == before_mtime
