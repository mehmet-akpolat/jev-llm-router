from __future__ import annotations

import json
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import redirect_stdout

from jev_router.cli import _history
from jev_router.costs import estimate_usd
from jev_router.history import History
from jev_router.hooks import main as hook_main, process


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"JEV_ROUTER_HOME": self.temp.name})
        self.env.start()
        self.history = History()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_codex_main_worker_deduplicated_and_concurrent_turns(self):
        main = Path(self.temp.name) / "main.jsonl"
        worker = Path(self.temp.name) / "worker.jsonl"
        first = process({"hook_event_name": "UserPromptSubmit", "session_id": "s", "turn_id": "t1",
                         "transcript_path": str(main), "prompt": "private request"}, "codex", self.history)
        route = first["hookSpecificOutput"]["additionalContext"].split("Jev routing ID for this prompt: ")[1].split(".")[0]
        second = process({"hook_event_name": "UserPromptSubmit", "session_id": "s", "turn_id": "t2",
                          "transcript_path": str(main), "prompt": "another request"}, "codex", self.history)
        self.assertNotEqual(first, second)
        self.history.add_route(route, "codex", "gpt-6-luna", "jev", 0.8)
        process({"hook_event_name": "SubagentStart", "session_id": "s", "turn_id": "t1",
                 "agent_id": "agent1"}, "codex", self.history)
        def record(response_id, root_turn_id, inp, out):
            return {"type": "token_usage_record", "payload": {"response_id": response_id,
                    "root_turn_id": root_turn_id, "usage": {"input_tokens": inp, "output_tokens": out,
                    "cached_input_tokens": 1, "total_tokens": inp + out}}}
        main.write_text("\n".join(json.dumps(x) for x in [record("r1", "t1", 10, 2),
                        record("r1", "t1", 10, 2), record("r3", "t2", 100, 50)]) + "\n")
        worker.write_text(json.dumps(record("r2", "t1", 20, 3)) + "\n")
        process({"hook_event_name": "SubagentStop", "session_id": "s", "turn_id": "t1",
                 "agent_id": "agent1", "agent_transcript_path": str(worker)}, "codex", self.history)
        process({"hook_event_name": "Stop", "session_id": "s", "turn_id": "t1",
                 "transcript_path": str(main)}, "codex", self.history)
        row = self.history.recent()[0]
        self.assertEqual((row["main_tokens"], row["worker_tokens"], row["total_tokens"]), (12, 23, 35))
        self.assertEqual((row["input_tokens"], row["output_tokens"], row["cached_input_tokens"]), (30, 5, 2))
        self.assertEqual(row["usage_status"], "complete")
        process({"hook_event_name": "Stop", "session_id": "s", "turn_id": "t1",
                 "transcript_path": str(main)}, "codex", self.history)
        self.assertEqual(self.history.recent()[0]["total_tokens"], 35)
        self.assertNotIn(b"private request", (Path(self.temp.name) / "history.sqlite3").read_bytes())

    def test_notional_reduction_uses_prompt_model_and_worker_tokens(self):
        main = Path(self.temp.name) / "main.jsonl"
        worker = Path(self.temp.name) / "worker.jsonl"
        prompt = process({"hook_event_name": "UserPromptSubmit", "session_id": "s", "turn_id": "t",
                          "model": "gpt-6-sol", "transcript_path": str(main)}, "codex", self.history)
        route = prompt["hookSpecificOutput"]["additionalContext"].split("Jev routing ID for this prompt: ")[1].split(".")[0]
        self.history.add_route(route, "codex", "gpt-6-luna", "jev", 0.9)
        process({"hook_event_name": "SubagentStart", "session_id": "s", "turn_id": "t",
                 "agent_id": "a"}, "codex", self.history)
        def record(response_id, inp, out, cached):
            return {"type": "token_usage_record", "payload": {"response_id": response_id,
                "root_turn_id": "t", "usage": {"input_tokens": inp, "output_tokens": out,
                "cached_input_tokens": cached, "total_tokens": inp + out}}}
        main.write_text(json.dumps(record("main", 1000, 100, 200)) + "\n")
        worker.write_text(json.dumps(record("worker", 10000, 1000, 2000)) + "\n")
        process({"hook_event_name": "SubagentStop", "session_id": "s", "turn_id": "t",
                 "agent_id": "a", "agent_transcript_path": str(worker)}, "codex", self.history)
        process({"hook_event_name": "Stop", "session_id": "s", "turn_id": "t",
                 "transcript_path": str(main)}, "codex", self.history)
        row = self.history.recent()[0]
        self.assertEqual(row["baseline_model"], "gpt-6-sol")
        self.assertAlmostEqual(row["baseline_cost_usd"], 0.0264)
        self.assertAlmostEqual(row["routed_cost_usd"], 0.00396)
        self.assertAlmostEqual(row["estimated_reduction_usd"], 0.02244)
        self.assertEqual(row["estimated_reduction_pct"], 85.0)
        with redirect_stdout(io.StringIO()) as output:
            _history(20, "codex", False)
        self.assertIn("0.02640", output.getvalue())
        self.assertIn("0.00396", output.getvalue())
        self.assertIn("+0.02244", output.getvalue())

    def test_more_expensive_recommendation_shows_negative_reduction(self):
        self.history.start_turn("r", "codex", "s", "t", None, 0, "gpt-6-luna")
        self.history.add_route("r", "codex", "gpt-6-astra", "jev", 0.9)
        self.history.subagent("r", "a", ended=True)
        self.history.add_usage("r", "main", "main", [("m", 100, 10, 0, 110, 0)])
        self.history.add_usage("r", "a", "worker", [("w", 1000, 100, 0, 1100, 0)])
        self.history.end_turn("r")
        row = self.history.recent()[0]
        self.assertLess(row["estimated_reduction_usd"], 0)
        self.assertLess(row["estimated_reduction_pct"], 0)
        with redirect_stdout(io.StringIO()) as output:
            _history(20, "codex", False)
        self.assertIn("-0.01486", output.getvalue())

    def test_claude_baseline_is_snapshotted_across_model_switches(self):
        process({"hook_event_name": "SessionStart", "session_id": "s", "model": "claude-sonnet-5"},
                "claude", self.history)
        first = process({"hook_event_name": "UserPromptSubmit", "session_id": "s", "prompt_id": "p1"},
                        "claude", self.history)
        first_id = first["hookSpecificOutput"]["additionalContext"].split("Jev routing ID for this prompt: ")[1].split(".")[0]
        process({"hook_event_name": "PostModelSwitch", "session_id": "s", "to_model": "claude-opus-5-5"},
                "claude", self.history)
        second = process({"hook_event_name": "UserPromptSubmit", "session_id": "s", "prompt_id": "p2"},
                         "claude", self.history)
        second_id = second["hookSpecificOutput"]["additionalContext"].split("Jev routing ID for this prompt: ")[1].split(".")[0]
        with self.history.connect() as db:
            rows = db.execute("SELECT route_id,baseline_model FROM turns").fetchall()
        self.assertEqual({row["route_id"]: row["baseline_model"] for row in rows},
                         {first_id: "claude-sonnet-5", second_id: "claude-opus-5-5"})

    def test_cost_estimator_accounts_for_cache_write_and_tokenizer(self):
        parts = [{"input_tokens": 1000, "output_tokens": 100, "cached_input_tokens": 600,
                  "cache_write_tokens": 100}]
        self.assertAlmostEqual(estimate_usd(parts, "claude-haiku-4-5-20251001",
                                            "claude-haiku-4-5-20251001"), 0.001075)
        self.assertAlmostEqual(estimate_usd(parts, "claude-sonnet-5",
                                            "claude-haiku-4-5-20251001"), 0.002795)
        self.assertAlmostEqual(estimate_usd(parts, "gpt-5.6-terra", "gpt-5.6-terra"), 0.00235)
        self.assertGreater(estimate_usd(parts, "gpt-5.6-terra", "gpt-5.6-terra"),
                           estimate_usd(parts, "gpt-6-sol", "gpt-6-sol"))
        self.assertIsNone(estimate_usd([{**parts[0], "cache_write_tokens": None}],
                                       "claude-sonnet-5", "claude-haiku-4-5-20251001"))
        self.assertIsNone(estimate_usd(parts, "unknown", "claude-haiku-4-5-20251001"))

    def test_claude_main_and_worker_usage_and_missing_worker(self):
        main = Path(self.temp.name) / "claude.jsonl"
        worker = Path(self.temp.name) / "agent.jsonl"
        process_result = process({"hook_event_name": "UserPromptSubmit", "session_id": "s",
                                  "prompt_id": "p", "transcript_path": str(main)}, "claude", self.history)
        route = process_result["hookSpecificOutput"]["additionalContext"].split("Jev routing ID for this prompt: ")[1].split(".")[0]
        self.history.add_route(route, "claude", "haiku", "jev", 0.9)
        process({"hook_event_name": "SubagentStart", "session_id": "s", "agent_id": "a"}, "claude", self.history)
        def record(message_id, inp, out, read=0, write=0):
            return {"type": "assistant", "message": {"id": message_id, "usage": {
                    "input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": read,
                    "cache_creation_input_tokens": write}}}
        main.write_text(json.dumps(record("m1", 4, 2, read=3)) + "\n")
        process({"hook_event_name": "Stop", "session_id": "s", "transcript_path": str(main)}, "claude", self.history)
        self.assertEqual(self.history.recent()[0]["usage_status"], "unknown")
        worker.write_text(json.dumps(record("m2", 5, 1, write=2)) + "\n")
        process({"hook_event_name": "SubagentStop", "session_id": "s", "agent_id": "a",
                 "agent_transcript_path": str(worker)}, "claude", self.history)
        row = self.history.recent()[0]
        self.assertEqual((row["main_tokens"], row["worker_tokens"], row["total_tokens"]), (9, 8, 17))

    def test_unmatched_route_remains_unknown(self):
        self.history.add_route("unmatched", "codex", "gpt-6-sol", "jev", 0.9)
        self.assertIsNone(self.history.recent()[0]["total_tokens"])

    def test_malformed_native_usage_remains_unknown(self):
        main = Path(self.temp.name) / "broken.jsonl"
        process({"hook_event_name": "UserPromptSubmit", "session_id": "s", "turn_id": "t",
                 "transcript_path": str(main)}, "codex", self.history)
        route_id = self.history.find_turn("codex", "s", "t")["route_id"]
        self.history.add_route(route_id, "codex", "gpt-6-sol", "jev", 0.9)
        # A damaged record prevents a complete total even if one record parses.
        main.write_text(json.dumps({"type": "token_usage_record", "payload": {
            "root_turn_id": "t", "response_id": "r1", "usage": {"input_tokens": 10,
                "output_tokens": 2, "cached_input_tokens": 0, "total_tokens": 12}
        }}) + "\n" + "{broken\n")
        with self.assertRaises(ValueError):
            process({"hook_event_name": "Stop", "session_id": "s", "turn_id": "t",
                     "transcript_path": str(main)}, "codex", self.history)
        self.assertEqual(self.history.recent()[0]["usage_status"], "unknown")

    def test_history_cli_shows_unknown_and_filters(self):
        self.history.add_route("unmatched", "codex", "gpt-6-sol", "jev", 0.9)
        output = io.StringIO()
        with redirect_stdout(output):
            _history(20, "codex", False)
        self.assertIn("gpt-6-sol", output.getvalue())
        self.assertIn("unknown", output.getvalue())
        output = io.StringIO()
        with redirect_stdout(output):
            _history(20, "claude", True)
        self.assertEqual(json.loads(output.getvalue()), {"routes": []})

    def test_ambiguous_claude_session_is_not_attributed(self):
        for _ in range(2):
            process({"hook_event_name": "UserPromptSubmit", "session_id": "shared"}, "claude", self.history)
        self.assertEqual(self.history.find_turn("claude", "shared", None), None)

    def test_reused_subagent_id_is_not_attributed_to_current_turn(self):
        for session in ("first", "second"):
            process({"hook_event_name": "UserPromptSubmit", "session_id": session}, "claude", self.history)
            process({"hook_event_name": "SubagentStart", "session_id": session,
                     "agent_id": "reused"}, "claude", self.history)
        self.assertEqual(self.history.find_agent("reused"), "")
        process({"hook_event_name": "SubagentStop", "session_id": "second",
                 "agent_id": "reused"}, "claude", self.history)
        with self.history.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM subagents WHERE ended=0").fetchone()[0], 2)

    def test_auto_hook_identifies_both_clients(self):
        events = [
            ({"hook_event_name": "UserPromptSubmit", "session_id": "s1", "turn_id": "t1"}, "codex"),
            ({"hook_event_name": "UserPromptSubmit", "session_id": "s2", "prompt_id": "p2"}, "claude"),
        ]
        for event, client in events:
            with patch("sys.stdin", io.StringIO(json.dumps(event))), redirect_stdout(io.StringIO()) as output:
                self.assertEqual(hook_main("auto"), 0)
            self.assertIn("Jev routing ID", output.getvalue())
            self.assertIsNotNone(self.history.find_turn(client, event["session_id"], event.get("turn_id") if client == "codex" else event.get("prompt_id")))


if __name__ == "__main__":
    unittest.main()
