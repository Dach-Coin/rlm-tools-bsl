"""Shared configuration loading for CLI and MCP server."""

from __future__ import annotations

import json
import os
from pathlib import Path

CONFIG_DIR = Path.home() / ".config" / "rlm-tools-bsl"
SERVICE_JSON = CONFIG_DIR / "service.json"
# Private service-to-child marker.  The public RLM_NO_ENV setting controls the
# installer; this one records the resulting service configuration at runtime.
SERVICE_NO_ENV_VAR = "_RLM_SERVICE_NO_ENV"


def get_projects_path() -> Path:
    """Return path to projects.json, co-located with the active service.json."""
    cfg_path = os.environ.get("RLM_CONFIG_FILE")
    if cfg_path:
        return Path(cfg_path).parent / "projects.json"
    return SERVICE_JSON.parent / "projects.json"


def load_project_env() -> str | None:
    """Load .env file and return path to the loaded file (or *None*).

    Search order:

    1. ``RLM_CONFIG_FILE`` env  →  read ``env_file`` from JSON  →  load that ``.env``
    2. ``~/.config/rlm-tools-bsl/service.json``  →  ``env_file``
    3. ``~/.config/rlm-tools-bsl/.env``  (user-level fallback)
    4. ``find_dotenv(usecwd=True)``  (CWD-based, original behaviour)
    """
    if os.environ.get(SERVICE_NO_ENV_VAR) == "1":
        return None

    try:
        from dotenv import find_dotenv, load_dotenv
    except ImportError:
        return None

    # 1-2. Service config (explicit override or default path)
    env_from_cfg = _env_file_from_service_json()
    if env_from_cfg and Path(env_from_cfg).is_file():
        load_dotenv(env_from_cfg, override=False)
        return env_from_cfg

    # 3. User-level .env
    user_env = CONFIG_DIR / ".env"
    if user_env.is_file():
        load_dotenv(str(user_env), override=False)
        return str(user_env)

    # 4. CWD-based search (walk up from cwd)
    found = find_dotenv(usecwd=True)
    if found:
        load_dotenv(found, override=False)
        return found

    return None


def _env_file_from_service_json() -> str | None:
    """Read ``env_file`` from *service.json* (respects ``RLM_CONFIG_FILE``)."""
    cfg_path = os.environ.get("RLM_CONFIG_FILE")
    if cfg_path:
        p = Path(cfg_path)
    else:
        p = SERVICE_JSON

    if not p.is_file():
        return None

    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        # ValueError covers JSONDecodeError AND UnicodeDecodeError: a config saved in
        # another encoding must not crash the server before argparse even runs.
        return None
    if not isinstance(data, dict):
        return None
    value = data.get("env_file")
    if not isinstance(value, str):
        return None
    path = Path(value)
    return value if path.is_absolute() else str((p.absolute().parent / path).absolute())
