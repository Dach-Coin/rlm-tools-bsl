"""Tests for _config.load_project_env()."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    """Remove env vars that could interfere with tests."""
    monkeypatch.delenv("RLM_CONFIG_FILE", raising=False)
    monkeypatch.delenv("RLM_INDEX_DIR", raising=False)
    monkeypatch.delenv("_RLM_SERVICE_NO_ENV", raising=False)


@pytest.fixture
def no_cwd_dotenv(monkeypatch):
    """Keep fallback tests independent from a .env above pytest's temp directory."""
    import dotenv

    monkeypatch.setattr(dotenv, "find_dotenv", lambda **_kwargs: "")


def _write_service_json(path: Path, env_file: str | None = None) -> Path:
    data = {"host": "127.0.0.1", "port": 9000, "env_file": env_file}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _write_dotenv(path: Path, content: str = "RLM_INDEX_DIR=/test/dir\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


class TestLoadProjectEnv:
    """Test the .env search chain."""

    def test_service_json_env_file(self, monkeypatch, tmp_path):
        """service.json → env_file takes priority."""
        env_path = _write_dotenv(tmp_path / "project" / ".env")
        cfg_path = _write_service_json(
            tmp_path / "config" / "service.json",
            env_file=str(env_path),
        )
        monkeypatch.setenv("RLM_CONFIG_FILE", str(cfg_path))

        from rlm_tools_bsl._config import load_project_env

        result = load_project_env()

        assert result == str(env_path)
        assert os.environ.get("RLM_INDEX_DIR") == "/test/dir"

    def test_relative_service_env_is_resolved_from_config_directory(self, monkeypatch, tmp_path):
        config_dir = tmp_path / "config"
        env_path = _write_dotenv(config_dir / ".env")
        cfg_path = _write_service_json(config_dir / "service.json", env_file=".env")
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
        monkeypatch.setenv("RLM_CONFIG_FILE", str(cfg_path))

        from rlm_tools_bsl._config import load_project_env

        assert load_project_env() == str(env_path)
        assert os.environ.get("RLM_INDEX_DIR") == "/test/dir"

    def test_default_service_json(self, monkeypatch, tmp_path):
        """Default ~/.config/rlm-tools-bsl/service.json is read."""
        env_path = _write_dotenv(tmp_path / ".env")
        svc_json = tmp_path / ".config" / "rlm-tools-bsl" / "service.json"
        _write_service_json(svc_json, env_file=str(env_path))

        import rlm_tools_bsl._config as cfg_mod

        monkeypatch.setattr(cfg_mod, "SERVICE_JSON", svc_json)
        monkeypatch.setattr(cfg_mod, "CONFIG_DIR", svc_json.parent)

        result = cfg_mod.load_project_env()
        assert result == str(env_path)

    def test_user_level_dotenv(self, monkeypatch, tmp_path):
        """~/.config/rlm-tools-bsl/.env is used as fallback."""
        config_dir = tmp_path / ".config" / "rlm-tools-bsl"
        _write_dotenv(config_dir / ".env")

        import rlm_tools_bsl._config as cfg_mod

        monkeypatch.setattr(cfg_mod, "SERVICE_JSON", config_dir / "nonexistent.json")
        monkeypatch.setattr(cfg_mod, "CONFIG_DIR", config_dir)

        result = cfg_mod.load_project_env()
        assert result == str(config_dir / ".env")

    def test_cwd_fallback(self, monkeypatch, tmp_path):
        """find_dotenv(usecwd=True) is the last resort."""
        _write_dotenv(tmp_path / ".env")
        monkeypatch.chdir(tmp_path)

        import rlm_tools_bsl._config as cfg_mod

        monkeypatch.setattr(cfg_mod, "SERVICE_JSON", tmp_path / "nonexistent.json")
        monkeypatch.setattr(cfg_mod, "CONFIG_DIR", tmp_path / "nonexistent_dir")

        result = cfg_mod.load_project_env()
        assert result == str(tmp_path / ".env")

    def test_no_env_found(self, monkeypatch, tmp_path, no_cwd_dotenv):
        """Returns None when nothing is found."""
        monkeypatch.chdir(tmp_path)

        import rlm_tools_bsl._config as cfg_mod

        monkeypatch.setattr(cfg_mod, "SERVICE_JSON", tmp_path / "no.json")
        monkeypatch.setattr(cfg_mod, "CONFIG_DIR", tmp_path / "nodir")

        result = cfg_mod.load_project_env()
        assert result is None

    def test_service_json_missing_env_file(self, monkeypatch, tmp_path, no_cwd_dotenv):
        """service.json exists but env_file points to missing file → skip."""
        cfg_path = _write_service_json(
            tmp_path / "service.json",
            env_file=str(tmp_path / "nonexistent.env"),
        )
        monkeypatch.setenv("RLM_CONFIG_FILE", str(cfg_path))
        monkeypatch.chdir(tmp_path)

        import rlm_tools_bsl._config as cfg_mod

        monkeypatch.setattr(cfg_mod, "CONFIG_DIR", tmp_path / "nodir")

        result = cfg_mod.load_project_env()
        assert result is None

    def test_service_json_no_env_file_key(self, monkeypatch, tmp_path, no_cwd_dotenv):
        """service.json without env_file key → skip to next."""
        cfg_path = _write_service_json(tmp_path / "service.json", env_file=None)
        monkeypatch.setenv("RLM_CONFIG_FILE", str(cfg_path))
        monkeypatch.chdir(tmp_path)

        import rlm_tools_bsl._config as cfg_mod

        monkeypatch.setattr(cfg_mod, "CONFIG_DIR", tmp_path / "nodir")

        result = cfg_mod.load_project_env()
        assert result is None

    def test_service_no_env_does_not_fall_back_to_user_or_cwd_dotenv(self, monkeypatch, tmp_path):
        """`service install --no-env` must suppress the fallback search at runtime."""
        config_dir = tmp_path / "config"
        _write_service_json(config_dir / "service.json", env_file=None)
        _write_dotenv(config_dir / ".env", "RLM_INDEX_DIR=/must-not-load\n")
        _write_dotenv(tmp_path / ".env", "RLM_INDEX_DIR=/must-not-load-either\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("_RLM_SERVICE_NO_ENV", "1")

        import rlm_tools_bsl._config as cfg_mod

        monkeypatch.setattr(cfg_mod, "CONFIG_DIR", config_dir)

        assert cfg_mod.load_project_env() is None
        assert "RLM_INDEX_DIR" not in os.environ


class TestSaveConfigOverride:
    """save_config must respect RLM_CONFIG_FILE override."""

    def test_save_config_respects_override(self, monkeypatch, tmp_path):
        """save_config writes to RLM_CONFIG_FILE path when set."""
        override_path = tmp_path / "custom" / "service.json"
        monkeypatch.setenv("RLM_CONFIG_FILE", str(override_path))

        import rlm_tools_bsl.service as svc_mod

        svc_mod.save_config("0.0.0.0", 8080, None)

        assert override_path.exists()
        cfg = svc_mod.load_config()
        assert cfg["host"] == "0.0.0.0"
        assert cfg["port"] == 8080

    def test_save_config_default_path(self, monkeypatch, tmp_path):
        """save_config uses default CONFIG_FILE when no override set."""
        default_cfg = tmp_path / "default" / "service.json"
        monkeypatch.delenv("RLM_CONFIG_FILE", raising=False)

        import rlm_tools_bsl.service as svc_mod

        monkeypatch.setattr(svc_mod, "CONFIG_FILE", default_cfg)

        svc_mod.save_config("127.0.0.1", 9000, None)

        assert default_cfg.exists()
        cfg = json.loads(default_cfg.read_text(encoding="utf-8"))
        assert cfg["host"] == "127.0.0.1"


def test_server_reports_our_version_not_mcp_version():
    """FastMCP 1.x не принимает version в конструкторе, а низкоуровневый сервер без неё
    подставляет версию пакета mcp — интегратор видел в serverInfo.version чужую версию."""
    import importlib.metadata as im

    from rlm_tools_bsl.server import mcp

    assert mcp._mcp_server.version == im.version("rlm-tools-bsl")
    assert mcp._mcp_server.version != im.version("mcp")


class TestBrokenServiceJson:
    """`load_project_env()` runs in `main()` BEFORE argparse: whatever is wrong with
    service.json, it must not take the server down on startup."""

    @pytest.mark.parametrize(
        "payload",
        [b"{not json", b"[]", b'"just a string"', bytes([0xFF, 0xFE, 0x7B, 0x7D]), b""],
    )
    def test_unusable_config_yields_no_env_file(self, monkeypatch, tmp_path, payload):
        cfg = tmp_path / "config" / "service.json"
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_bytes(payload)
        monkeypatch.setenv("RLM_CONFIG_FILE", str(cfg))
        monkeypatch.chdir(tmp_path)

        from rlm_tools_bsl._config import _env_file_from_service_json

        assert _env_file_from_service_json() is None

    def test_non_string_env_file_is_ignored(self, monkeypatch, tmp_path):
        cfg = tmp_path / "config" / "service.json"
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(json.dumps({"env_file": 42}), encoding="utf-8")
        monkeypatch.setenv("RLM_CONFIG_FILE", str(cfg))

        from rlm_tools_bsl._config import _env_file_from_service_json

        assert _env_file_from_service_json() is None
