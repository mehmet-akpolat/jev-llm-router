"""Private user-level paths shared by the CLI and MCP process."""

from __future__ import annotations

import os
from pathlib import Path


def state_dir() -> Path:
    return Path(os.environ.get("JEV_ROUTER_HOME", Path.home() / ".config" / "jev-router")).expanduser()


def runtime_config_path() -> Path:
    return state_dir() / "router-config.json"


def audit_path() -> Path:
    return state_dir() / "routes.jsonl"


def history_path() -> Path:
    return state_dir() / "history.sqlite3"
