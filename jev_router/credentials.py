"""Read the TypeSafe key from the process or assistant MCP settings."""

from __future__ import annotations

import os
import json
import tomllib
from pathlib import Path


def read_typesafe_key() -> str | None:
    value = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if value:
        return value
    # Claude's settings environment is inherited by its plugin MCP process.
    claude = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")) / "settings.json"
    try:
        value = json.loads(claude.read_text(encoding="utf-8")).get("env", {}).get("TYPESAFE_API_KEY", "")
        if isinstance(value, str) and value.strip():
            return value.strip()
    except (FileNotFoundError, json.JSONDecodeError, AttributeError):
        pass
    codex = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "config.toml"
    try:
        value = tomllib.loads(codex.read_text(encoding="utf-8")).get("mcp_servers", {}).get("jev_router", {}).get("env", {}).get("TYPESAFE_API_KEY", "")
        if isinstance(value, str) and value.strip():
            return value.strip()
    except (FileNotFoundError, tomllib.TOMLDecodeError, AttributeError):
        pass
    return None
