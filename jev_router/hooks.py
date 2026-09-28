"""Non-blocking native-client hooks for per-route token accounting."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from .costs import known_model
from .history import History, new_route_id


def _nonnegative(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _records(path: Path, client: str, *, offset: int = 0, turn_id: str | None = None
             ) -> list[tuple[str, int, int, int, int, int]]:
    found: dict[str, tuple[str, int, int, int, int, int]] = {}
    previous_codex_usage = (0, 0, 0, 0)
    last_codex_usage: tuple[int, int, int, int] | None = None
    with path.open("rb") as stream:
        while raw := stream.readline():
            position = stream.tell() - len(raw)
            try:
                entry = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise ValueError("Malformed transcript record") from None
            if not isinstance(entry, dict):
                continue
            payload = entry.get("payload") if isinstance(entry, dict) else None
            if client == "codex":
                if entry.get("type") == "event_msg" and isinstance(payload, dict) and payload.get("type") == "token_count":
                    info = payload.get("info")
                    usage = info.get("total_token_usage") if isinstance(info, dict) else None
                    if not isinstance(usage, dict):
                        continue
                    values = tuple(_nonnegative(usage.get(key, 0 if key == "cached_input_tokens" else None))
                                   for key in ("input_tokens", "output_tokens", "cached_input_tokens", "total_tokens"))
                    if None in values:
                        raise ValueError("Invalid Codex cumulative usage count")
                    if position < offset:
                        previous_codex_usage = values
                    else:
                        last_codex_usage = values
                    continue
                if position < offset:
                    continue
                if entry.get("type") != "token_usage_record" or not isinstance(payload, dict):
                    continue
                if turn_id and payload.get("root_turn_id") != turn_id:
                    continue
                usage = payload.get("usage")
                response_id = payload.get("response_id")
                if not isinstance(usage, dict) or not isinstance(response_id, str) or not response_id:
                    raise ValueError("Incomplete Codex usage record")
                inp = _nonnegative(usage.get("input_tokens"))
                out = _nonnegative(usage.get("output_tokens"))
                cached = _nonnegative(usage.get("cached_input_tokens"))
                total = _nonnegative(usage.get("total_tokens"))
                if None in (inp, out, cached, total):
                    raise ValueError("Invalid Codex usage count")
                write = 0
            else:
                if position < offset:
                    continue
                if entry.get("type") != "assistant":
                    continue
                message = entry.get("message")
                if not isinstance(message, dict):
                    raise ValueError("Incomplete Claude assistant record")
                usage = message.get("usage")
                response_id = message.get("id")
                if not isinstance(usage, dict) or not isinstance(response_id, str) or not response_id:
                    raise ValueError("Incomplete Claude usage record")
                raw_input = _nonnegative(usage.get("input_tokens"))
                out = _nonnegative(usage.get("output_tokens"))
                read = _nonnegative(usage.get("cache_read_input_tokens", 0))
                write = _nonnegative(usage.get("cache_creation_input_tokens", 0))
                if None in (raw_input, out, read, write):
                    raise ValueError("Invalid Claude usage count")
                inp = raw_input + read + write
                cached = read + write
                total = inp + out
            found[response_id] = (response_id, inp, out, cached, total, write)
    if client == "codex" and not found and last_codex_usage is not None:
        differences = tuple(end - start for start, end in zip(previous_codex_usage, last_codex_usage))
        if any(value < 0 for value in differences) or differences[3] != differences[0] + differences[1]:
            raise ValueError("Inconsistent Codex cumulative usage count")
        response_id = hashlib.sha256(f"{path.resolve()}\0{offset}\0{turn_id or ''}".encode()).hexdigest()
        inp, out, cached, total = differences
        return [(f"codex-cumulative:{response_id}", inp, out, cached, total, 0)]
    return list(found.values())


def _path(value: Any) -> Path | None:
    return Path(value).expanduser() if isinstance(value, str) and value else None


def process(event: dict[str, Any], client: str, history: History) -> dict[str, Any]:
    name = event.get("hook_event_name")
    session_id = event.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return {}
    if client == "claude" and name in {"SessionStart", "PostModelSwitch"}:
        model = known_model(event.get("model" if name == "SessionStart" else "to_model"))
        if model:
            history.record_session_model(client, session_id, model)
        return {}
    native_turn_id = event.get("turn_id") if client == "codex" else event.get("prompt_id")
    if not isinstance(native_turn_id, str):
        native_turn_id = None
    if client == "codex" and not native_turn_id:
        return {}
    if name == "UserPromptSubmit":
        route_id = new_route_id()
        transcript = _path(event.get("transcript_path"))
        offset = transcript.stat().st_size if transcript and transcript.is_file() else 0
        baseline = known_model(event.get("model"))
        if client == "claude" and baseline is None:
            baseline = history.session_model(client, session_id)
        history.start_turn(route_id, client, session_id, native_turn_id,
                           str(transcript) if transcript else None, offset, baseline)
        return {"hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": f"Jev routing ID for this prompt: {route_id}. Pass it unchanged as route_id to choose_model."
        }}
    turn = history.find_turn(client, session_id, native_turn_id)
    agent_id = event.get("agent_id")
    if name == "SubagentStart":
        if client == "codex" and turn is None:
            # Codex subagents have their own turn ID but retain the parent session ID.
            # Only bind one when the parent has exactly one active turn.
            turn = history.find_turn(client, session_id, None)
        if turn and isinstance(agent_id, str) and agent_id:
            history.subagent(turn["route_id"], agent_id)
    elif name == "SubagentStop":
        route_id = history.find_agent(agent_id) if isinstance(agent_id, str) else None
        if route_id is None and turn:
            route_id = turn["route_id"]
        if route_id and isinstance(agent_id, str) and agent_id:
            transcript = _path(event.get("agent_transcript_path"))
            if transcript and transcript.is_file():
                root_turn = history.get_turn(route_id)
                root_turn_id = root_turn["native_turn_id"] if root_turn and client == "codex" else None
                records = _records(transcript, client, turn_id=root_turn_id)
                history.add_usage(route_id, agent_id, "worker", records)
            history.subagent(route_id, agent_id, ended=True)
    elif name == "Stop" and turn:
        transcript = _path(event.get("transcript_path")) or _path(turn["transcript_path"])
        if transcript and transcript.is_file():
            records = _records(transcript, client, offset=turn["start_offset"],
                               turn_id=native_turn_id if client == "codex" else None)
            history.add_usage(turn["route_id"], "main", "main", records)
        history.end_turn(turn["route_id"])
    return {}


def main(client: str) -> int:
    try:
        event = json.load(sys.stdin)
        if isinstance(event, dict):
            if client == "auto":
                client = "codex" if "turn_id" in event else "claude"
            output = process(event, client, History())
        else:
            output = {}
    except Exception:
        # A history failure must never block an assistant prompt or completion.
        output = {}
    print(json.dumps(output, separators=(",", ":")))
    return 0
