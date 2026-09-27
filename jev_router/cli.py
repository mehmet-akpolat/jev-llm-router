"""CLI for configuring and running the Python Jev MCP server."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import tomllib
from pathlib import Path

from .config import ConfigError, load_config
from .credentials import read_typesafe_key
from .history import History
from .hooks import main as hook_main
from .key_prompt import hidden_key_prompt
from .mcp import run_stdio
from .pools import set_pool
from .paths import state_dir
from .setup import apply_settings, back_up_legacy_claude_plugin, claude_version_warning, legacy_claude_plugin_path, planned_settings, preview_settings


def _legacy_key_path() -> Path:
    return state_dir() / "typesafe-key"


def _legacy_key_for_migration() -> str | None:
    try:
        return _legacy_key_path().read_text(encoding="utf-8").strip() or None
    except FileNotFoundError:
        return None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jev-router", description="Jev MCP model recommendations for Claude Code and Codex")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Copy the example model configuration")
    init.add_argument("path", nargs="?", default="router-config.json")
    mcp = commands.add_parser("mcp", help="Run the MCP server over stdio")
    mcp.add_argument("--config")
    setup = commands.add_parser("setup", help="Preview or apply native assistant settings")
    setup.add_argument("--config")
    setup.add_argument("--client", choices=("both", "claude", "codex"), default="both")
    setup.add_argument("--apply", action="store_true")
    status = commands.add_parser("status", help="Show TypeSafe key status and recent recommendations")
    status.add_argument("--json", action="store_true")
    history = commands.add_parser("history", help="Show routing history and attributed token usage")
    history.add_argument("--limit", type=int, default=20)
    history.add_argument("--client", choices=("claude", "codex"))
    history.add_argument("--json", action="store_true")
    pool = commands.add_parser("pool", help="Inspect or override model pools")
    pool_commands = pool.add_subparsers(dest="pool_command", required=True)
    show = pool_commands.add_parser("show", help="Show the active model pools")
    show.add_argument("--client", choices=("claude", "codex"))
    show.add_argument("--json", action="store_true")
    set_command = pool_commands.add_parser("set", help="Select existing or bundled models for a client")
    set_command.add_argument("--client", choices=("claude", "codex"), required=True)
    set_command.add_argument("--models", required=True, help="Comma-separated model IDs")
    set_command.add_argument("--fallback", required=True)
    set_command.add_argument("--apply", action="store_true")
    import_command = pool_commands.add_parser("import", help="Use a complete JSON config for new model definitions")
    import_command.add_argument("--file", required=True)
    import_command.add_argument("--client", choices=("both", "claude", "codex"), default="both")
    import_command.add_argument("--apply", action="store_true")
    hook = commands.add_parser("hook", help="Internal assistant lifecycle hook")
    hook.add_argument("--client", choices=("claude", "codex", "auto"), required=True)
    auth = commands.add_parser("auth", help="Check the TypeSafe key used by the MCP server")
    auth_commands = auth.add_subparsers(dest="auth_command", required=True)
    auth_commands.add_parser("status", help="Check whether a TypeSafe key is available")
    return parser


def _status(as_json: bool) -> None:
    records = History().recent(20)
    if as_json:
        print(json.dumps({"typesafe_key_configured": bool(read_typesafe_key()), "recommendations": records}, indent=2))
    else:
        print("TypeSafe key: " + ("configured" if read_typesafe_key() else "missing"))
        if not records:
            print("No recommendations recorded yet")
        for record in records:
            print(f"{record.get('at')} {record.get('client')} {record.get('model')} ({record.get('reason')})")


def _history(limit: int, client: str | None, as_json: bool) -> None:
    if not 1 <= limit <= 500:
        raise ValueError("--limit must be from 1 to 500")
    records = History().recent(limit, client)
    if as_json:
        print(json.dumps({"routes": records}, indent=2))
        return
    if not records:
        print("No routing recommendations recorded yet")
        return
    print("Recent recommendations, assistant tokens, and notional USD reduction vs the prompt's baseline model")
    print("Negative reduction means an estimated increase; amounts include coordinator usage")
    maximum = max((item["total_tokens"] or 0 for item in records), default=0)
    print(f"{'When (UTC)':19} {'Client':6} {'Recommended':17} {'Baseline':17} {'Main':>8} {'Worker':>8} {'Total':>8} {'Base $':>10} {'Routed $':>10} {'Est. Δ$':>11}  Tokens")
    for item in records:
        total = item["total_tokens"]
        bar = "" if total is None or maximum == 0 else "█" * max(1, round(total / maximum * 16))
        numeric = lambda value: "unknown" if value is None else str(value)
        cost = lambda value: "unknown" if value is None else f"{value:.5f}"
        reduction = item["estimated_reduction_usd"]
        money = "unknown" if reduction is None else f"{reduction:+.5f}"
        print(f"{item['at'][:19]:19} {item['client'][:6]:6} {item['model'][:17]:17} "
              f"{(item['baseline_model'] or 'unknown')[:17]:17} {numeric(item['main_tokens']):>8} "
              f"{numeric(item['worker_tokens']):>8} {numeric(total):>8} "
              f"{cost(item['baseline_cost_usd']):>10} {cost(item['routed_cost_usd']):>10} "
              f"{money:>11}  {bar}")


def _pool(args: argparse.Namespace) -> None:
    if args.pool_command == "show":
        config = load_config()
        selected = {args.client: config["clients"][args.client]} if args.client else config["clients"]
        if args.json:
            print(json.dumps({"clients": selected} if args.client else config, indent=2))
        else:
            for client, pool in selected.items():
                print(f"{client} (fallback: {pool['fallback']})")
                for model in pool["models"]:
                    print(f"  {model['id']}  weight={model['cost_weight']}  {model['description']}")
        return
    if args.pool_command == "set":
        ids = [item.strip() for item in args.models.split(",")]
        if any(not item for item in ids):
            raise ValueError("--models must be a comma-separated list of model IDs")
        config = set_pool(load_config(), args.client, ids, args.fallback)
        client = args.client
    else:
        imported = load_config(args.file)
        client = args.client
        if client == "both":
            config = imported
        else:
            config = load_config()
            config = {**config, "clients": {**config["clients"], client: imported["clients"][client]}}
    changes = planned_settings(config, client=client, key=read_typesafe_key(), persist_config=True)
    print(preview_settings(changes) or "Model pools already match the requested configuration")
    if args.apply:
        backups = apply_settings(changes)
        print("Model pools updated. Backups: " + (", ".join(map(str, backups)) or "none"))
        print("Restart the configured assistant so coordinator settings reload")
    else:
        print("Preview only. Rerun the same pool command with --apply to save it")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "init":
            target = Path(args.path).resolve()
            source = Path(__file__).resolve().parent / "router-config.example.json"
            if target.exists():
                raise ValueError(f"Config already exists: {target}")
            shutil.copyfile(source, target)
            print(f"Created {target}. Review model IDs and relative costs")
        elif args.command == "mcp":
            run_stdio(args.config)
        elif args.command == "auth":
            print("TypeSafe key: " + ("configured" if read_typesafe_key() else "missing"))
        elif args.command == "status":
            _status(args.json)
        elif args.command == "history":
            _history(args.limit, args.client, args.json)
        elif args.command == "pool":
            _pool(args)
        elif args.command == "hook":
            return hook_main(args.client)
        elif args.command == "setup":
            config = load_config(args.config)
            key = read_typesafe_key() or _legacy_key_for_migration()
            changes = planned_settings(config, client=args.client, key=key)
            print("Assistant setup targets:")
            for path, (before, after) in changes.items():
                state = "already configured" if before == after else "will create" if not before else "will update"
                print(f"  {path} ({state})")
            if args.client in {"both", "claude"} and (legacy_plugin := legacy_claude_plugin_path()):
                print(f"  {legacy_plugin} (will move to a backup outside the skills directory)")
            print("Preview only: no files are written without --apply.\n" if not args.apply else "Applying the changes shown below.\n")
            print(preview_settings(changes) or "Assistant settings already match the router configuration")
            if args.client in {"both", "claude"}:
                if warning := claude_version_warning():
                    print(f"Warning: {warning}", file=sys.stderr)
            overrides = [name for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY") if os.environ.get(name)]
            if overrides:
                print("Warning: provider credential environment variables may override subscription sign-in: " + ", ".join(overrides), file=sys.stderr)
            if args.apply:
                if not key:
                    key = hidden_key_prompt()
                    changes = planned_settings(config, client=args.client, key=key)
                backups = apply_settings(changes)
                if args.client in {"both", "claude"}:
                    if legacy_backup := back_up_legacy_claude_plugin():
                        backups.append(legacy_backup)
                _legacy_key_path().unlink(missing_ok=True)
                print("Assistant settings applied. Backups: " + (", ".join(map(str, backups)) or "none"))
                print("Restart the configured assistant" +
                      ("; for Codex, review and trust Jev hooks with /hooks" if args.client in {"both", "codex"} else ""))
            else:
                print("Preview only. Rerun the same setup command with --apply to write these settings")
        return 0
    except (ValueError, ConfigError, OSError, sqlite3.Error, json.JSONDecodeError, tomllib.TOMLDecodeError) as exc:
        print(f"jev-router: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
