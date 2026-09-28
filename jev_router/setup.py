"""Preview and apply assistant settings for marketplace-installed plugins."""

from __future__ import annotations

import difflib
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .paths import runtime_config_path, state_dir


MCP_START = "# BEGIN JEV ROUTER MCP (managed)"
MCP_END = "# END JEV ROUTER MCP (managed)"
GUIDE_START = "<!-- BEGIN JEV ROUTER (managed) -->"
GUIDE_END = "<!-- END JEV ROUTER (managed) -->"


def claude_settings_path() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")).expanduser() / "settings.json"


def codex_settings_path() -> Path:
    return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser() / "config.toml"


def _source_root() -> Path:
    return Path(__file__).resolve().parent.parent


def bundle_client() -> str | None:
    """Return an owner only for a single-assistant plugin root."""
    root = _source_root()
    owners = [client for client in ("claude", "codex")
              if (root / f".{client}-plugin" / "plugin.json").is_file()]
    return owners[0] if len(owners) == 1 else None


def client_root(client: str) -> Path:
    root = _source_root()
    child = root / client
    if child.is_dir():
        return child
    raise ValueError(f"{client} resources were not found beside the Python package")


def allowed_client(client: str | None) -> str:
    """Use the owning assistant in a bundle; allow both in a checkout."""
    owner = bundle_client()
    selected = client or owner or "both"
    if owner and selected != owner:
        raise ValueError(f"This {owner} plugin can configure only the {owner} model pool")
    return selected


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _claude_config(existing: str, key: str | None = None) -> str:
    data = json.loads(existing) if existing.strip() else {}
    if not isinstance(data, dict):
        raise ValueError("Claude settings must be a JSON object")
    env = data.get("env", {})
    if not isinstance(env, dict):
        raise ValueError("Claude settings.env must be an object")
    if data.get("apiKeyHelper"):
        raise ValueError("Claude has an apiKeyHelper; subscription routing requires the saved claude.ai sign-in")
    if any(env.get(name) for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")):
        raise ValueError("Claude settings contain an API credential or gateway URL; subscription routing requires the saved claude.ai sign-in")
    if key:
        env["TYPESAFE_API_KEY"] = key
        data["env"] = env
    data["agent"] = "jev-router:coordinator"
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def _claude_model_command() -> str:
    source = (client_root("claude") / "commands" / "jev-router.md").read_text(encoding="utf-8")
    return source.replace('"${CLAUDE_PLUGIN_ROOT}"', shlex.quote(str(_source_root())))


def _strip_block(content: str, start: str, end: str) -> str:
    if start not in content and end not in content:
        return content
    if content.count(start) != 1 or content.count(end) != 1 or content.index(start) > content.index(end):
        raise ValueError(f"Malformed managed block: {start}")
    return content[:content.index(start)] + content[content.index(end) + len(end):]


def _codex_config(existing: str, key: str | None = None) -> str:
    stripped = _strip_block(existing, MCP_START, MCP_END)
    parsed = tomllib.loads(stripped) if stripped.strip() else {}
    if "jev_router" in parsed.get("model_providers", {}):
        raise ValueError("Unmanaged Codex model_providers.jev_router table already exists")
    if "jev_router" in parsed.get("mcp_servers", {}):
        raise ValueError("Unmanaged Codex mcp_servers.jev_router table already exists")
    provider = parsed.get("model_provider")
    if provider not in (None, "openai"):
        raise ValueError("Codex uses a custom model provider; subscription routing requires its built-in ChatGPT provider")
    managed = (
        f"{MCP_START}\n"
        "[mcp_servers.jev_router]\n"
        'command = "uv"\n'
        'args = ["run", "--no-project", "--python", "3.11", "python", "-m", "jev_router", "mcp"]\n'
        f"cwd = {json.dumps(str(_source_root()))}\n"
        "\n[mcp_servers.jev_router.env]\n"
        f"JEV_ROUTER_HOME = {json.dumps(str(state_dir().resolve()))}\n"
        + (f"TYPESAFE_API_KEY = {json.dumps(key)}\n" if key else "")
        + f"{MCP_END}\n"
    )
    result = stripped.rstrip() + ("\n\n" if stripped.strip() else "") + managed
    tomllib.loads(result)
    return result


def _codex_guidance(existing: str, config: dict[str, Any]) -> str:
    base = _strip_block(existing, GUIDE_START, GUIDE_END).rstrip()
    fallback = config["clients"]["codex"]["fallback"]
    block = (
        f"{GUIDE_START}\n"
        "For each new user prompt in the main conversation, call the Jev Router MCP `choose_model` tool "
        "with `client=codex` and the current user request. If the prompt hook supplied a Jev routing ID, "
        "pass it unchanged as `route_id`. Call the tool once for that prompt. "
        "Then spawn the `jev_worker` subagent with the returned `model` as an explicit spawn setting. "
        "Pass the full task and relevant earlier conversation context, wait for the worker, and report its result. "
        "Keep the worker on that model for its tool loop. Do not do the coding task in this coordinator thread. "
        f"If the tool is unavailable, delegate on the fallback model `{fallback}` and say Jev was unavailable.\n"
        f"{GUIDE_END}\n"
    )
    return (base + "\n\n" if base else "") + block


def _codex_guidance_path() -> Path:
    root = codex_settings_path().parent
    override = root / "AGENTS.override.md"
    return override if override.exists() and _read(override).strip() else root / "AGENTS.md"


def codex_setup_targets() -> dict[str, Path]:
    root = codex_settings_path().parent
    return {
        "MCP server [mcp_servers.jev_router]": codex_settings_path(),
        "Worker agent jev_worker": root / "agents" / "jev-worker.toml",
        "Coordinator instructions": _codex_guidance_path(),
    }


def planned_settings(config: dict[str, Any], *, client: str = "both", key: str | None = None,
                     persist_config: bool = False) -> dict[Path, tuple[str, str]]:
    client = allowed_client(client)
    if client not in {"both", "claude", "codex"}:
        raise ValueError("client must be both, claude, or codex")
    codex_targets = codex_setup_targets()
    guide_path = codex_targets["Coordinator instructions"]
    desired: dict[Path, str] = {}
    if client in {"both", "claude"}:
        desired[claude_settings_path()] = _claude_config(_read(claude_settings_path()), key)
        desired[claude_settings_path().parent / "commands" / "jev-router.md"] = _claude_model_command()
    if client in {"both", "codex"}:
        worker_source = client_root("codex") / "integrations" / "codex-worker.toml"
        desired.update({
            codex_settings_path(): _codex_config(_read(codex_settings_path()), key),
            guide_path: _codex_guidance(_read(guide_path), config),
            codex_targets["Worker agent jev_worker"]: worker_source.read_text(encoding="utf-8"),
        })
    if persist_config or config != json.loads((_source_root() / "jev_router" / "router-config.example.json").read_text(encoding="utf-8")) or runtime_config_path().exists():
        desired[runtime_config_path()] = json.dumps(config, indent=2, ensure_ascii=False) + "\n"
    return {path: (_read(path), new) for path, new in desired.items()}


def preview_settings(changes: dict[Path, tuple[str, str]]) -> str:
    chunks = []
    for path, (old, new) in changes.items():
        chunks.extend(difflib.unified_diff(old.splitlines(True), new.splitlines(True), fromfile=str(path), tofile=str(path)))
    return re.sub(r'(?m)^([+ -].*?TYPESAFE_API_KEY[^:=\n]*[:=]\s*).+$', r'\1"[REDACTED]"', "".join(chunks))


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def apply_settings(changes: dict[Path, tuple[str, str]]) -> list[Path]:
    changed = [(path, old, new) for path, (old, new) in changes.items() if old != new]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backups: dict[Path, Path] = {}
    written: list[Path] = []
    for path, _, _ in changed:
        if path.exists():
            backup = path.with_name(path.name + f".jev-router-backup-{stamp}")
            shutil.copy2(path, backup)
            os.chmod(backup, 0o600)
            backups[path] = backup
    try:
        for path, _, new in changed:
            _atomic_write(path, new)
            written.append(path)
    except OSError:
        for path in reversed(written):
            if path in backups:
                shutil.copy2(backups[path], path)
            else:
                path.unlink(missing_ok=True)
        raise
    return list(backups.values())


def claude_version_warning() -> str | None:
    try:
        result = subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=10, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return "Claude Code was not found; install version 2.1.283 or newer"
    match = re.search(r"\b(\d+)\.(\d+)\.(\d+)\b", result.stdout)
    if not match or tuple(map(int, match.groups())) < (2, 1, 283):
        return f"Claude Code {result.stdout.strip() or 'unknown'} must be upgraded to 2.1.283 or newer"
    return None
