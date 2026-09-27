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


OLD_START = "# BEGIN JEV ROUTER (managed)"
OLD_END = "# END JEV ROUTER (managed)"
MCP_START = "# BEGIN JEV ROUTER MCP (managed)"
MCP_END = "# END JEV ROUTER MCP (managed)"
GUIDE_START = "<!-- BEGIN JEV ROUTER (managed) -->"
GUIDE_END = "<!-- END JEV ROUTER (managed) -->"


def claude_settings_path() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")).expanduser() / "settings.json"


def codex_settings_path() -> Path:
    return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser() / "config.toml"


def legacy_claude_plugin_path() -> Path | None:
    target = claude_settings_path().parent / "skills" / "jev-router"
    manifest = target / ".claude-plugin" / "plugin.json"
    if target.is_symlink() or not manifest.is_file():
        return None
    try:
        return target if json.loads(manifest.read_text(encoding="utf-8")).get("name") == "jev-router" else None
    except (json.JSONDecodeError, AttributeError):
        return None


def back_up_legacy_claude_plugin() -> Path | None:
    target = legacy_claude_plugin_path()
    if target is None:
        return None
    backup_root = claude_settings_path().parent / "jev-router-backups"
    backup_root.mkdir(mode=0o700, exist_ok=True)
    os.chmod(backup_root, 0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = backup_root / f"jev-router-{stamp}"
    shutil.move(str(target), str(backup))
    return backup


def _source_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _cheap_model(config: dict[str, Any], client: str) -> str:
    return min(config["clients"][client]["models"], key=lambda item: item["cost_weight"])["id"]


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _claude_config(existing: str, key: str | None = None) -> str:
    data = json.loads(existing) if existing.strip() else {}
    if not isinstance(data, dict):
        raise ValueError("Claude settings must be a JSON object")
    env = data.get("env", {})
    if not isinstance(env, dict):
        raise ValueError("Claude settings.env must be an object")
    helper = data.get("apiKeyHelper")
    old_helper = isinstance(helper, str) and ("jev_router token" in helper or "jev-router token" in helper)
    if helper and not old_helper:
        raise ValueError("Claude has another apiKeyHelper; remove it before using subscription routing")
    if old_helper:
        data.pop("apiKeyHelper")
        if re.fullmatch(r"http://(?:127\.0\.0\.1|localhost):\d+", str(env.get("ANTHROPIC_BASE_URL", ""))):
            env.pop("ANTHROPIC_BASE_URL", None)
        if env.get("CLAUDE_CODE_GATEWAY_HINT_HEADERS") == "1":
            env.pop("CLAUDE_CODE_GATEWAY_HINT_HEADERS", None)
        if not env:
            data.pop("env", None)
    if any(env.get(name) for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")):
        raise ValueError("Claude settings contain an API credential or gateway URL; subscription routing requires the saved claude.ai sign-in")
    if key:
        env["TYPESAFE_API_KEY"] = key
        data["env"] = env
    data["agent"] = "jev-router:coordinator"
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def _claude_model_command() -> str:
    source = (_source_root() / "commands" / "jev-router.md").read_text(encoding="utf-8")
    return source.replace('"${CLAUDE_PLUGIN_ROOT}"', shlex.quote(str(_source_root())))


def _strip_block(content: str, start: str, end: str) -> str:
    if start not in content and end not in content:
        return content
    if content.count(start) != 1 or content.count(end) != 1 or content.index(start) > content.index(end):
        raise ValueError(f"Malformed managed block: {start}")
    return content[:content.index(start)] + content[content.index(end) + len(end):]


def _root_value(prefix: str, name: str, value: str | None) -> str:
    pattern = re.compile(rf"(?m)^[ \t]*{re.escape(name)}[ \t]*=.*(?:\n|$)")
    prefix = pattern.sub("", prefix, count=1)
    if value is None:
        return prefix
    return prefix.rstrip() + ("\n" if prefix.strip() else "") + f"{name} = {json.dumps(value)}\n"


def _codex_config(existing: str, config: dict[str, Any], key: str | None = None) -> str:
    stripped = _strip_block(_strip_block(existing, OLD_START, OLD_END), MCP_START, MCP_END)
    parsed = tomllib.loads(stripped) if stripped.strip() else {}
    if "jev_router" in parsed.get("model_providers", {}):
        raise ValueError("Unmanaged Codex model_providers.jev_router table already exists")
    if "jev_router" in parsed.get("mcp_servers", {}):
        raise ValueError("Unmanaged Codex mcp_servers.jev_router table already exists")
    provider = parsed.get("model_provider")
    if provider not in (None, "openai", "jev_router"):
        raise ValueError("Codex uses a custom model provider; restore its built-in ChatGPT provider before setup")
    table = re.search(r"(?m)^\s*\[", stripped)
    prefix, suffix = (stripped[:table.start()], stripped[table.start():]) if table else (stripped, "")
    if provider == "jev_router":
        prefix = _root_value(prefix, "model_provider", None)
    prefix = _root_value(prefix, "model", _cheap_model(config, "codex"))
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
    result = prefix.rstrip() + "\n" + (suffix.strip() + "\n\n" if suffix.strip() else "\n") + managed
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


def _remove_legacy_codex_hooks(existing: str) -> str:
    if not existing.strip():
        return existing
    data = json.loads(existing)
    hooks = data.get("hooks") if isinstance(data, dict) else None
    if not isinstance(hooks, dict):
        raise ValueError("Codex hooks.json must contain a hooks object")
    changed = False
    for event, groups in list(hooks.items()):
        if not isinstance(groups, list):
            raise ValueError(f"Codex hooks.{event} must be an array")
        kept = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                raise ValueError(f"Codex hooks.{event} contains an invalid group")
            handlers = [entry for entry in group["hooks"] if not (
                isinstance(entry, dict) and "-m jev_router hook --client codex" in str(entry.get("command", ""))
            )]
            changed |= len(handlers) != len(group["hooks"])
            if handlers:
                kept.append({**group, "hooks": handlers})
        if kept:
            hooks[event] = kept
        else:
            hooks.pop(event)
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n" if changed else existing


def planned_settings(config: dict[str, Any], *, client: str = "both", key: str | None = None,
                     persist_config: bool = False) -> dict[Path, tuple[str, str]]:
    if client not in {"both", "claude", "codex"}:
        raise ValueError("client must be both, claude, or codex")
    codex_targets = codex_setup_targets()
    worker_source = _source_root() / "integrations" / "codex-worker.toml"
    guide_path = codex_targets["Coordinator instructions"]
    desired: dict[Path, str] = {}
    if client in {"both", "claude"}:
        desired[claude_settings_path()] = _claude_config(_read(claude_settings_path()), key)
        desired[claude_settings_path().parent / "commands" / "jev-router.md"] = _claude_model_command()
    if client in {"both", "codex"}:
        desired.update({
            codex_settings_path(): _codex_config(_read(codex_settings_path()), config, key),
            guide_path: _codex_guidance(_read(guide_path), config),
            codex_targets["Worker agent jev_worker"]: worker_source.read_text(encoding="utf-8"),
        })
        legacy_hooks = codex_settings_path().parent / "hooks.json"
        if legacy_hooks.exists():
            desired[legacy_hooks] = _remove_legacy_codex_hooks(_read(legacy_hooks))
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
