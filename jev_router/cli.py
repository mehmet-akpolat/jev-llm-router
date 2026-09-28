"""CLI for configuring and running the Python Jev MCP server."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import textwrap
import tomllib
from pathlib import Path

from .config import ConfigError, load_config, validate_config
from .credentials import read_typesafe_key
from .history import History
from .hooks import main as hook_main
from .key_prompt import hidden_key_prompt
from .mcp import run_stdio
from .pools import set_pool
from .setup import allowed_client, apply_settings, bundle_client, claude_version_warning, planned_settings, preview_settings


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jev-router", description="Jev MCP model recommendations for Claude Code and Codex")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Copy the example model configuration")
    init.add_argument("path", nargs="?", default="router-config.json")
    mcp = commands.add_parser("mcp", help="Run the MCP server over stdio")
    mcp.add_argument("--config")
    setup = commands.add_parser("setup", help="Preview or apply native assistant settings")
    setup.add_argument("--config")
    setup.add_argument("--client", choices=("both", "claude", "codex"))
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
    import_command = pool_commands.add_parser("import", help="Import model definitions from JSON (one pool in a plugin, both in a checkout)")
    import_command.add_argument("--file", required=True)
    import_command.add_argument("--client", choices=("both", "claude", "codex"))
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
    print("Recent recommendations and estimated USD reduction vs prompt baseline")
    print("Negative reduction means an increase; costs include coordinator usage")
    maximum = max((item["total_tokens"] or 0 for item in records), default=0)
    numeric = lambda value: "unknown" if value is None else str(value)
    cost = lambda value: "unknown" if value is None else f"{value:.5f}"
    reduction = lambda value: "unknown" if value is None else f"{value:+.5f}"
    rows = []
    for item in records:
        total = item["total_tokens"]
        rows.append((item["at"][:19], item["client"], item["model"],
                     item["baseline_model"] or "unknown", numeric(item["main_tokens"]),
                     numeric(item["worker_tokens"]), numeric(total),
                     cost(item["baseline_cost_usd"]), cost(item["routed_cost_usd"]),
                     reduction(item["estimated_reduction_usd"]),
                     "unknown" if total is None or maximum == 0 else "█" * max(1, round(total / maximum * 16))))

    wide_headers = ("When (UTC)", "Client", "Recommended", "Baseline", "Coordinator", "Worker",
                    "Total", "Base $", "Routed $", "Cost Reduction$", "Tokens")
    wide_widths = [max(len(header), *(len(row[index]) for row in rows))
                   for index, header in enumerate(wide_headers)]
    wide_width = sum(wide_widths) + len(wide_widths) - 1
    # A captured stream has no known viewer width; use the compact table there.
    available_width = shutil.get_terminal_size().columns if sys.stdout.isatty() else 80
    compact = not sys.stdout.isatty() or available_width < wide_width
    if compact:
        groups = ((0, 1, 2, 3), (4, 5, 6, 10, 7, 8, 9))
        identity_width = sum(wide_widths[index] for index in groups[0]) + 2 * (len(groups[0]) - 1)
        expanded_identity = identity_width > available_width
        for group in groups:
            widths = [wide_widths[index] for index in group]
            if expanded_identity and group == groups[0]:
                print("When (UTC)  Client  Recommended  Baseline")
            else:
                separator = "  " if group == groups[0] else " "
                print(separator.join(wide_headers[index].ljust(width) if index in (0, 1, 2, 3, 10)
                                     else wide_headers[index].rjust(width)
                                     for index, width in zip(group, widths)).rstrip())
        for row in rows:
            for group in groups:
                if expanded_identity and group == groups[0]:
                    print(f"{row[0]}  {row[1]}")
                    for label, value in (("Recommended", row[2]), ("Baseline", row[3])):
                        prefix = f"  {label}: "
                        for index, part in enumerate(textwrap.wrap(value, width=max(1, available_width - len(prefix)),
                                                                   break_long_words=True, break_on_hyphens=False)):
                            print((prefix if index == 0 else " " * len(prefix)) + part)
                    continue
                widths = [wide_widths[index] for index in group]
                separator = "  " if group == groups[0] else " "
                print(separator.join(row[index].ljust(width) if index in (0, 1, 2, 3, 10)
                                     else row[index].rjust(width)
                                     for index, width in zip(group, widths)).rstrip())
    else:
        print(" ".join(header.ljust(width) if index in (0, 1, 2, 3, 10) else header.rjust(width)
                       for index, (header, width) in enumerate(zip(wide_headers, wide_widths))).rstrip())
        for row in rows:
            print(" ".join(value.ljust(width) if index in (0, 1, 2, 3, 10) else value.rjust(width)
                           for index, (value, width) in enumerate(zip(row, wide_widths))).rstrip())


def _pool(args: argparse.Namespace) -> None:
    if args.pool_command == "show":
        config = load_config()
        client = allowed_client(args.client) if args.client else bundle_client()
        selected = {client: config["clients"][client]} if client and client != "both" else config["clients"]
        if args.json:
            print(json.dumps({"clients": selected} if client and client != "both" else config, indent=2))
        else:
            for client, pool in selected.items():
                print(f"{client} (fallback: {pool['fallback']})")
                for model in pool["models"]:
                    print(f"  {model['id']}  weight={model['cost_weight']}  {model['description']}")
        return
    if args.pool_command == "set":
        allowed_client(args.client)
        ids = [item.strip() for item in args.models.split(",")]
        if any(not item for item in ids):
            raise ValueError("--models must be a comma-separated list of model IDs")
        config = set_pool(load_config(), args.client, ids, args.fallback)
        client = allowed_client(args.client)
    else:
        client = allowed_client(args.client)
        if client == "both":
            config = load_config(args.file)
        else:
            imported = json.loads(Path(args.file).expanduser().read_text(encoding="utf-8"))
            if not isinstance(imported, dict) or not isinstance(imported.get("clients"), dict) or client not in imported["clients"]:
                raise ConfigError(f"Import must contain clients.{client}")
            config = load_config()
            config = validate_config({**config, "clients": {**config["clients"], client: imported["clients"][client]}})
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
            args.client = allowed_client(args.client)
            config = load_config(args.config)
            key = read_typesafe_key()
            changes = planned_settings(config, client=args.client, key=key)
            print("Assistant setup targets:")
            for path, (before, after) in changes.items():
                state = "already configured" if before == after else "will create" if not before else "will update"
                print(f"  {path} ({state})")
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
