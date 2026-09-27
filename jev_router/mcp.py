"""Dependency-free MCP stdio server for Jev model recommendations."""

from __future__ import annotations

import json
import sqlite3
import sys
import uuid
from pathlib import Path
from typing import Any, TextIO

from . import __version__
from .config import load_config
from .history import History, new_route_id
from .routing import choose_model


SUPPORTED_VERSIONS = {"2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"}
TOOL = {
    "name": "choose_model",
    "description": "Ask TypeSafe Jev which configured Claude Code or Codex model should handle the current user prompt. Call once before delegating that prompt to a worker. Returns a fallback if Jev is unavailable.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "client": {"type": "string", "enum": ["claude", "codex"]},
            "prompt": {"type": "string", "description": "The latest user prompt, including relevant task details."},
            "route_id": {"type": "string", "description": "Optional opaque ID supplied by the prompt hook for token accounting."},
        },
        "required": ["client", "prompt"],
        "additionalProperties": False,
    },
    "annotations": {"readOnlyHint": True},
}


def _error(message_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": message}}


class JevMcpServer:
    def __init__(self, config: dict[str, Any], history: History):
        self.config = config
        self.history = history

    def handle(self, message: Any) -> dict[str, Any] | None:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return _error(message.get("id") if isinstance(message, dict) else None, -32600, "Invalid JSON-RPC request")
        method = message.get("method")
        if not isinstance(method, str):
            return _error(message.get("id"), -32600, "Missing method")
        if "id" not in message:
            return None  # MCP notifications do not receive responses.
        message_id = message["id"]
        params = message.get("params", {})
        if method == "initialize":
            if not isinstance(params, dict):
                return _error(message_id, -32602, "Invalid initialize parameters")
            proposed = params.get("protocolVersion")
            version = proposed if isinstance(proposed, str) and proposed in SUPPORTED_VERSIONS else "2025-11-25"
            result = {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "jev-router", "version": __version__},
                "instructions": "For each main user prompt, call choose_model for that client before spawning a worker. Pass the returned model as the worker's explicit model. A fallback is a valid selection.",
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": [TOOL]}
        elif method == "tools/call":
            if not isinstance(params, dict) or params.get("name") != "choose_model":
                return _error(message_id, -32602, "Unknown tool")
            arguments = params.get("arguments")
            if not isinstance(arguments, dict):
                return _error(message_id, -32602, "Tool arguments must be an object")
            client, prompt = arguments.get("client"), arguments.get("prompt")
            if not isinstance(client, str) or client not in self.config["clients"] or not isinstance(prompt, str):
                return _error(message_id, -32602, "Expected a configured client and text prompt")
            route_id = arguments.get("route_id")
            if route_id is not None:
                if not isinstance(route_id, str):
                    return _error(message_id, -32602, "route_id must be a UUID")
                try:
                    valid_id = uuid.UUID(route_id)
                except ValueError:
                    return _error(message_id, -32602, "route_id must be a UUID")
                if str(valid_id) != route_id:
                    return _error(message_id, -32602, "route_id must be a canonical UUID")
            else:
                route_id = new_route_id()
            try:
                existing = self.history.get_route(route_id)
            except (sqlite3.Error, OSError):
                existing = None
            if existing and existing["client"] != client:
                return _error(message_id, -32602, "route_id belongs to another client")
            if existing:
                decision = {"route_id": route_id, **existing}
            else:
                selected = choose_model(prompt, client, self.config)
                decision = {"route_id": route_id, "client": client, "model": selected.model,
                            "reason": selected.reason, "confidence": selected.confidence}
                try:
                    self.history.add_route(route_id, client, selected.model, selected.reason, selected.confidence)
                except (sqlite3.Error, OSError):
                    pass  # A history failure must not break routing.
            result = {"content": [{"type": "text", "text": json.dumps(decision, separators=(",", ":"))}], "structuredContent": decision, "isError": False}
        else:
            return _error(message_id, -32601, "Method not found")
        return {"jsonrpc": "2.0", "id": message_id, "result": result}


def run_stdio(config_path: str | Path | None = None, *, source: TextIO | None = None, sink: TextIO | None = None) -> None:
    server = JevMcpServer(load_config(config_path), History())
    source, sink = source or sys.stdin, sink or sys.stdout
    for line in source:
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            response = _error(None, -32700, "Invalid JSON")
        else:
            response = server.handle(message)
        if response is not None:
            sink.write(json.dumps(response, separators=(",", ":"), ensure_ascii=False) + "\n")
            sink.flush()
