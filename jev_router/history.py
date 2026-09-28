"""Private, concurrent-safe routing and usage history."""

from __future__ import annotations

import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .costs import estimate_usd
from .paths import history_path


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class History:
    def __init__(self, path: Path | None = None):
        self.path = path or history_path()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        connection = sqlite3.connect(self.path, timeout=5)
        try:
            os.chmod(self.path, 0o600)
            connection.row_factory = sqlite3.Row
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS turns (
                    route_id TEXT PRIMARY KEY, client TEXT NOT NULL, session_id TEXT NOT NULL,
                    native_turn_id TEXT, transcript_path TEXT, start_offset INTEGER NOT NULL,
                    started_at TEXT NOT NULL, ended INTEGER NOT NULL DEFAULT 0,
                    baseline_model TEXT
                );
                CREATE INDEX IF NOT EXISTS turns_native ON turns(client, session_id, native_turn_id);
                CREATE TABLE IF NOT EXISTS session_models (
                    client TEXT NOT NULL, session_id TEXT NOT NULL, model TEXT NOT NULL,
                    PRIMARY KEY(client, session_id)
                );
                CREATE TABLE IF NOT EXISTS routes (
                    route_id TEXT PRIMARY KEY, at TEXT NOT NULL, client TEXT NOT NULL,
                    model TEXT NOT NULL, reason TEXT NOT NULL, confidence REAL, source TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS routes_recent ON routes(at DESC);
                CREATE TABLE IF NOT EXISTS subagents (
                    route_id TEXT NOT NULL, agent_id TEXT NOT NULL, ended INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(route_id, agent_id)
                );
                CREATE TABLE IF NOT EXISTS usage_parts (
                    route_id TEXT NOT NULL, source_key TEXT NOT NULL, response_id TEXT NOT NULL,
                    kind TEXT NOT NULL, input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
                    cached_input_tokens INTEGER NOT NULL, total_tokens INTEGER NOT NULL,
                    cache_write_tokens INTEGER,
                    PRIMARY KEY(route_id, response_id)
                );
            """)
            yield connection
            connection.commit()
        finally:
            connection.close()

    def session_model(self, client: str, session_id: str) -> str | None:
        with self.connect() as db:
            row = db.execute("SELECT model FROM session_models WHERE client=? AND session_id=?",
                             (client, session_id)).fetchone()
            return row["model"] if row else None

    def record_session_model(self, client: str, session_id: str, model: str) -> None:
        with self.connect() as db:
            db.execute("""INSERT INTO session_models(client,session_id,model) VALUES(?,?,?)
                ON CONFLICT(client,session_id) DO UPDATE SET model=excluded.model""",
                (client, session_id, model))

    def start_turn(self, route_id: str, client: str, session_id: str, native_turn_id: str | None,
                   transcript_path: str | None, start_offset: int, baseline_model: str | None = None) -> None:
        with self.connect() as db:
            db.execute("""INSERT INTO turns
                (route_id,client,session_id,native_turn_id,transcript_path,start_offset,started_at,baseline_model)
                VALUES (?,?,?,?,?,?,?,?)""",
                (route_id, client, session_id, native_turn_id, transcript_path, start_offset, now(), baseline_model))

    def get_route(self, route_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT client,model,reason,confidence FROM routes WHERE route_id=?", (route_id,)).fetchone()
            return dict(row) if row else None

    def add_route(self, route_id: str, client: str, model: str, reason: str, confidence: float | None) -> None:
        with self.connect() as db:
            db.execute("""INSERT OR IGNORE INTO routes
                (route_id,at,client,model,reason,confidence,source)
                VALUES (?,?,?,?,?,?,'mcp')""", (route_id, now(), client, model, reason, confidence))

    def find_turn(self, client: str, session_id: str, native_turn_id: str | None) -> dict[str, Any] | None:
        with self.connect() as db:
            if native_turn_id:
                rows = db.execute("""SELECT * FROM turns WHERE client=? AND session_id=? AND native_turn_id=?""",
                                  (client, session_id, native_turn_id)).fetchall()
            else:
                rows = db.execute("""SELECT * FROM turns WHERE client=? AND session_id=? AND ended=0
                                     ORDER BY started_at DESC LIMIT 2""", (client, session_id)).fetchall()
            return dict(rows[0]) if len(rows) == 1 else None

    def subagent(self, route_id: str, agent_id: str, *, ended: bool = False) -> None:
        with self.connect() as db:
            db.execute("""INSERT INTO subagents(route_id,agent_id,ended) VALUES(?,?,?)
                ON CONFLICT(route_id,agent_id) DO UPDATE SET ended=max(ended,excluded.ended)""",
                (route_id, agent_id, int(ended)))

    def find_agent(self, agent_id: str) -> str | None:
        with self.connect() as db:
            rows = db.execute("SELECT route_id FROM subagents WHERE agent_id=?", (agent_id,)).fetchall()
            if len(rows) == 1:
                return rows[0]["route_id"]
            return "" if rows else None  # An ambiguous ID must not fall back to the active turn.

    def add_usage(self, route_id: str, source_key: str, kind: str,
                  records: list[tuple[str, int, int, int, int, int]]) -> None:
        with self.connect() as db:
            db.executemany("""INSERT INTO usage_parts
                (route_id,source_key,response_id,kind,input_tokens,output_tokens,cached_input_tokens,total_tokens,cache_write_tokens)
                VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(route_id,response_id) DO UPDATE SET
                input_tokens=excluded.input_tokens,output_tokens=excluded.output_tokens,
                cached_input_tokens=excluded.cached_input_tokens,total_tokens=excluded.total_tokens,
                cache_write_tokens=excluded.cache_write_tokens""",
                [(route_id, source_key, response_id, kind, inp, out, cached, total, write)
                 for response_id, inp, out, cached, total, write in records])

    def end_turn(self, route_id: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE turns SET ended=1 WHERE route_id=?", (route_id,))

    def recent(self, limit: int = 20, client: str | None = None) -> list[dict[str, Any]]:
        with self.connect() as db:
            query = """SELECT r.*, t.ended, t.baseline_model,
                (SELECT count(*) FROM usage_parts u WHERE u.route_id=r.route_id AND u.kind='main') main_count,
                (SELECT count(*) FROM subagents s WHERE s.route_id=r.route_id) agent_count,
                (SELECT count(*) FROM subagents s WHERE s.route_id=r.route_id AND s.ended=0) open_agents,
                (SELECT count(*) FROM subagents s WHERE s.route_id=r.route_id AND s.ended=1
                 AND NOT EXISTS (SELECT 1 FROM usage_parts u WHERE u.route_id=r.route_id
                                 AND u.source_key=s.agent_id)) empty_agents,
                (SELECT sum(total_tokens) FROM usage_parts u WHERE u.route_id=r.route_id AND u.kind='main') main_tokens,
                (SELECT sum(total_tokens) FROM usage_parts u WHERE u.route_id=r.route_id AND u.kind='worker') worker_tokens,
                (SELECT sum(input_tokens) FROM usage_parts u WHERE u.route_id=r.route_id) input_tokens,
                (SELECT sum(output_tokens) FROM usage_parts u WHERE u.route_id=r.route_id) output_tokens,
                (SELECT sum(cached_input_tokens) FROM usage_parts u WHERE u.route_id=r.route_id) cached_input_tokens,
                (SELECT sum(total_tokens) FROM usage_parts u WHERE u.route_id=r.route_id) total_tokens
                FROM routes r LEFT JOIN turns t ON t.route_id=r.route_id"""
            params: list[Any] = []
            if client:
                query += " WHERE r.client=?"
                params.append(client)
            query += " ORDER BY r.at DESC, r.route_id DESC LIMIT ?"
            params.append(limit)
            result = []
            for row in db.execute(query, params):
                item = dict(row)
                complete = bool(item.pop("ended")) and bool(item.pop("main_count"))
                complete = complete and bool(item.pop("agent_count"))
                complete = complete and not item.pop("open_agents") and not item.pop("empty_agents")
                item["usage_status"] = "complete" if complete else "unknown"
                if not complete:
                    for key in ("main_tokens", "worker_tokens", "input_tokens", "output_tokens",
                                "cached_input_tokens", "total_tokens"):
                        item[key] = None
                item["baseline_cost_usd"] = None
                item["routed_cost_usd"] = None
                item["estimated_reduction_usd"] = None
                item["estimated_reduction_pct"] = None
                if complete and item["baseline_model"]:
                    parts = [dict(part) for part in db.execute("""SELECT kind,input_tokens,output_tokens,
                        cached_input_tokens,cache_write_tokens FROM usage_parts WHERE route_id=?""",
                        (item["route_id"],))]
                    main = [part for part in parts if part["kind"] == "main"]
                    workers = [part for part in parts if part["kind"] == "worker"]
                    baseline = estimate_usd(workers, item["baseline_model"], item["model"])
                    worker_cost = estimate_usd(workers, item["model"], item["model"])
                    main_cost = estimate_usd(main, item["baseline_model"], item["baseline_model"])
                    if None not in (baseline, worker_cost, main_cost):
                        routed = round(main_cost + worker_cost, 8)
                        reduction = round(baseline - routed, 8)
                        item["baseline_cost_usd"] = baseline
                        item["routed_cost_usd"] = routed
                        item["estimated_reduction_usd"] = reduction
                        item["estimated_reduction_pct"] = round(reduction / baseline * 100, 1) if baseline else None
                result.append(item)
            return result


def new_route_id() -> str:
    return str(uuid.uuid4())
