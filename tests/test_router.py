from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import tomllib
import unittest
import uuid
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from urllib import request

from jev_router.config import load_config, validate_config
from jev_router.credentials import read_typesafe_key
from jev_router.cli import main as cli_main
from jev_router.key_prompt import _native_prompt, hidden_key_prompt
from jev_router.history import History
from jev_router.hooks import process as hook_process
from jev_router.mcp import run_stdio
from jev_router.pools import set_pool
from jev_router.routing import choose_model
from jev_router.setup import apply_settings, planned_settings, preview_settings


ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = json.loads((ROOT / "jev_router" / "router-config.example.json").read_text())
CLAUDE_IDS = [model["id"] for model in EXAMPLE["clients"]["claude"]["models"]]


class RoutingTests(unittest.TestCase):
    def test_init_creates_router_config(self):
        with tempfile.TemporaryDirectory() as temp:
            env = {**os.environ, "PYTHONPATH": str(ROOT)}
            result = subprocess.run([sys.executable, "-m", "jev_router", "init"], cwd=temp, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads((Path(temp) / "router-config.json").read_text()), EXAMPLE)

    def test_jev_fallback_and_validation(self):
        config = validate_config(EXAMPLE)
        self.assertEqual(choose_model("", "claude", config).reason, "no_text_prompt")
        self.assertEqual(choose_model("hello", "codex", config, api_key="").reason, "jev_unavailable")

        def reply(data):
            return lambda _request, timeout: io.BytesIO(json.dumps(data).encode())

        self.assertEqual(choose_model("hello", "claude", config, api_key="test", opener=reply({"answers": []})).reason, "invalid_answer")
        low = {"answers": {"selected_model": {"type": "choice", "choice": "option_1", "confidence": 0.2}}}
        self.assertEqual(choose_model("hello", "claude", config, api_key="test", opener=reply(low)).reason, "low_confidence")
        def select_astra(request, timeout):
            criteria = json.loads(request.data)["questions"]["selected_model"]["criteria"]
            self.assertEqual([value.split(":", 1)[0] for value in criteria.values()],
                             ["gpt-6-luna", "gpt-6-sol", "gpt-6-astra"])
            answer = {"answers": {"selected_model": {"type": "choice", "choice": "option_3", "confidence": 0.9}}}
            return io.BytesIO(json.dumps(answer).encode())

        self.assertEqual(choose_model("hard coding", "codex", config, api_key="test", opener=select_astra).model,
                         "gpt-6-astra")
        invalid = json.loads(json.dumps(EXAMPLE))
        invalid["clients"]["codex"]["models"][1]["id"] = invalid["clients"]["codex"]["models"][0]["id"]
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            validate_config(invalid)

    def test_type_safe_key_is_read_from_settings_or_environment_only(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {
            "JEV_ROUTER_HOME": temp + "/state", "CLAUDE_CONFIG_DIR": temp + "/claude",
            "CODEX_HOME": temp + "/codex", "TYPESAFE_API_KEY": "",
        }):
            self.assertIsNone(read_typesafe_key())
            claude = Path(temp) / "claude"
            claude.mkdir()
            (claude / "settings.json").write_text('{"env":{"TYPESAFE_API_KEY":"settings-secret"}}')
            self.assertEqual(read_typesafe_key(), "settings-secret")
            os.environ["TYPESAFE_API_KEY"] = "override-secret"
            self.assertEqual(read_typesafe_key(), "override-secret")

    def test_bundled_default_and_custom_config_precedence(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"JEV_ROUTER_HOME": str(Path(temp) / "state"), "JEV_ROUTER_CONFIG": ""}):
            cwd = Path.cwd()
            try:
                os.chdir(temp)
                os.environ.pop("JEV_ROUTER_CONFIG", None)
                self.assertEqual(load_config(), validate_config(EXAMPLE))
                custom = json.loads(json.dumps(EXAMPLE))
                custom["clients"]["claude"]["fallback"] = CLAUDE_IDS[0]
                (Path(temp) / "router-config.json").write_text(json.dumps(custom))
                self.assertEqual(load_config()["clients"]["claude"]["fallback"], CLAUDE_IDS[0])
                explicit = Path(temp) / "explicit.json"
                explicit.write_text(json.dumps(EXAMPLE))
                self.assertEqual(load_config(explicit)["clients"]["claude"]["fallback"], EXAMPLE["clients"]["claude"]["fallback"])
            finally:
                os.chdir(cwd)


class PoolTests(unittest.TestCase):
    def test_pool_validation(self):
        config = validate_config(EXAMPLE)
        selected = set_pool(config, "claude", CLAUDE_IDS[:2], CLAUDE_IDS[0])
        self.assertEqual([item["id"] for item in selected["clients"]["claude"]["models"]], CLAUDE_IDS[:2])
        self.assertEqual(selected["clients"]["claude"]["fallback"], CLAUDE_IDS[0])
        self.assertEqual(config["clients"]["claude"]["fallback"], CLAUDE_IDS[1])
        with self.assertRaisesRegex(ValueError, "Unknown model"):
            set_pool(config, "claude", ["new-model"], "new-model")
        with self.assertRaisesRegex(ValueError, "selected models"):
            set_pool(config, "claude", [CLAUDE_IDS[0]], CLAUDE_IDS[1])
        with self.assertRaisesRegex(ValueError, "distinct"):
            set_pool(config, "claude", [CLAUDE_IDS[0], CLAUDE_IDS[0]], CLAUDE_IDS[0])

    def test_pool_preview_apply_and_bundled_override_of_local_config(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {
            "JEV_ROUTER_HOME": temp + "/state", "CLAUDE_CONFIG_DIR": temp + "/claude",
            "CODEX_HOME": temp + "/codex", "TYPESAFE_API_KEY": "test-secret",
            "JEV_ROUTER_CONFIG": "",
        }):
            cwd = Path.cwd()
            try:
                os.chdir(temp)
                custom = json.loads(json.dumps(EXAMPLE))
                custom["clients"]["claude"]["models"] = custom["clients"]["claude"]["models"][:2]
                (Path(temp) / "router-config.json").write_text(json.dumps(custom))
                command = ["pool", "set", "--client", "claude", "--models", ",".join(CLAUDE_IDS), "--fallback", CLAUDE_IDS[1]]
                preview = io.StringIO()
                with redirect_stdout(preview):
                    self.assertEqual(cli_main(command), 0)
                self.assertIn("Preview only", preview.getvalue())
                runtime = Path(temp) / "state" / "router-config.json"
                self.assertFalse(runtime.exists())
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(cli_main(command + ["--apply"]), 0)
                self.assertEqual(load_config(), validate_config(EXAMPLE))
                self.assertTrue(runtime.exists())
                self.assertFalse((Path(temp) / "codex" / "config.toml").exists())
                self.assertEqual(json.loads((Path(temp) / "claude" / "settings.json").read_text())["env"]["TYPESAFE_API_KEY"], "test-secret")
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(cli_main(command + ["--apply"]), 0)
                self.assertEqual(list(runtime.parent.glob("router-config.json.jev-router-backup-*")), [])
            finally:
                os.chdir(cwd)

    def test_pool_import_adds_new_model_and_preserves_other_client(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {
            "JEV_ROUTER_HOME": temp + "/state", "CLAUDE_CONFIG_DIR": temp + "/claude",
            "CODEX_HOME": temp + "/codex", "TYPESAFE_API_KEY": "test-secret",
            "JEV_ROUTER_CONFIG": "",
        }):
            imported = json.loads(json.dumps(EXAMPLE))
            imported["clients"]["codex"]["models"].append({
                "id": "custom-model", "description": "Custom coding model", "cost_weight": 2,
            })
            imported["clients"]["codex"]["fallback"] = "custom-model"
            path = Path(temp) / "import.json"
            path.write_text(json.dumps(imported))
            with redirect_stdout(io.StringIO()):
                self.assertEqual(cli_main(["pool", "import", "--file", str(path), "--client", "codex", "--apply"]), 0)
            result = json.loads((Path(temp) / "state" / "router-config.json").read_text())
            self.assertEqual(result["clients"]["codex"]["fallback"], "custom-model")
            self.assertEqual(result["clients"]["claude"], EXAMPLE["clients"]["claude"])
            self.assertFalse((Path(temp) / "claude" / "settings.json").exists())
            codex = tomllib.loads((Path(temp) / "codex" / "config.toml").read_text())
            self.assertEqual(codex["mcp_servers"]["jev_router"]["command"], "uv")


class McpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.calls: list[tuple[dict, str]] = []
        self.contexts = []
        self.env = patch.dict(os.environ, {
            "JEV_ROUTER_HOME": self.temp.name,
            "CLAUDE_CONFIG_DIR": self.temp.name + "/claude",
            "CODEX_HOME": self.temp.name + "/codex",
            "TYPESAFE_API_KEY": "jev-test-key",
        })
        self.env.start()
        self.urlopen_patch = patch("jev_router.routing.request.urlopen", side_effect=self._mock_urlopen)
        self.urlopen_patch.start()
        (Path(self.temp.name) / "router-config.json").write_text(json.dumps(EXAMPLE))

    def _mock_urlopen(self, request: request.Request, timeout: float, context=None):
        body = json.loads(request.data)
        self.calls.append((body, request.get_header("Authorization")))
        self.contexts.append(context)
        choice = f"option_{len(body['questions']['selected_model']['criteria'])}" if "hard" in body["state"]["task"] else "option_1"
        return io.BytesIO(json.dumps({"answers": {"selected_model": {"type": "choice", "choice": choice, "confidence": 0.94}}}).encode())

    def tearDown(self):
        self.urlopen_patch.stop()
        self.env.stop()
        self.temp.cleanup()

    def test_mcp_starts_with_bundled_pool_when_no_config_was_exported(self):
        (Path(self.temp.name) / "router-config.json").unlink()
        original_dir = Path.cwd()
        try:
            os.chdir(self.temp.name)
            output = io.StringIO()
            message = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                "name": "choose_model", "arguments": {"client": "claude", "prompt": "small edit"}}}
            run_stdio(source=io.StringIO(json.dumps(message) + "\n"), sink=output)
        finally:
            os.chdir(original_dir)
        result = json.loads(output.getvalue())["result"]["structuredContent"]
        self.assertEqual(result["model"], CLAUDE_IDS[0])

    def test_stdio_handshake_tool_calls_and_private_history(self):
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "mock", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "choose_model", "arguments": {"client": "claude", "prompt": "small edit private A"}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "choose_model", "arguments": {"client": "codex", "prompt": "hard migration private B"}}},
        ]
        output = io.StringIO()
        run_stdio(source=io.StringIO("\n".join(json.dumps(item) for item in messages) + "\n"), sink=output)
        replies = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([item["id"] for item in replies], [1, 2, 3, 4])
        self.assertEqual(replies[0]["result"]["protocolVersion"], "2025-11-25")
        self.assertEqual(replies[1]["result"]["tools"][0]["name"], "choose_model")
        self.assertEqual(replies[2]["result"]["structuredContent"]["model"], CLAUDE_IDS[0])
        self.assertEqual(replies[3]["result"]["structuredContent"]["model"], "gpt-6-astra")
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(all(auth == "Bearer jev-test-key" for _, auth in self.calls))
        rows = History().recent()
        self.assertEqual(len(rows), 2)
        database = (Path(self.temp.name) / "history.sqlite3").read_bytes()
        self.assertNotIn(b"private A", database)
        self.assertNotIn(b"private B", database)
        self.assertNotIn(b"jev-test-key", database)
        self.assertEqual(stat.S_IMODE((Path(self.temp.name) / "history.sqlite3").stat().st_mode), 0o600)

    def test_missing_key_returns_configured_fallback(self):
        os.environ["TYPESAFE_API_KEY"] = ""
        output = io.StringIO()
        message = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "choose_model", "arguments": {"client": "codex", "prompt": "task"}}}
        run_stdio(source=io.StringIO(json.dumps(message) + "\n"), sink=output)
        result = json.loads(output.getvalue())["result"]["structuredContent"]
        self.assertEqual((result["model"], result["reason"]), ("gpt-6-sol", "jev_unavailable"))
        self.assertEqual(self.calls, [])

    def test_route_id_replay_uses_one_jev_decision(self):
        route_id = str(uuid.uuid4())
        message = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "choose_model", "arguments": {"client": "codex", "prompt": "task", "route_id": route_id}}}
        output = io.StringIO()
        run_stdio(source=io.StringIO(json.dumps(message) + "\n" + json.dumps(message) + "\n"), sink=output)
        replies = [json.loads(line)["result"]["structuredContent"] for line in output.getvalue().splitlines()]
        self.assertEqual(replies[0], replies[1])
        self.assertEqual(replies[0]["route_id"], route_id)
        self.assertEqual(len(self.calls), 1)

    def test_two_prompts_in_one_session_for_each_client(self):
        for client in ("claude", "codex"):
            history = History()
            session = f"two-prompts-{client}"
            messages = []
            for index, prompt in enumerate(("small edit", "hard migration"), start=1):
                event = {"hook_event_name": "UserPromptSubmit", "session_id": session}
                event["prompt_id" if client == "claude" else "turn_id"] = f"prompt-{index}"
                hook = hook_process(event, client, history)
                route_id = hook["hookSpecificOutput"]["additionalContext"].split("Jev routing ID for this prompt: ")[1].split(".")[0]
                messages.append({"jsonrpc": "2.0", "id": index, "method": "tools/call", "params": {
                    "name": "choose_model", "arguments": {"client": client, "prompt": prompt, "route_id": route_id}}})
            output = io.StringIO()
            run_stdio(source=io.StringIO("\n".join(json.dumps(item) for item in messages) + "\n"), sink=output)
            replies = [json.loads(line)["result"]["structuredContent"] for line in output.getvalue().splitlines()]
            self.assertNotEqual(replies[0]["model"], replies[1]["model"])
            self.assertEqual(len({item["route_id"] for item in replies}), 2)
            self.assertTrue(all(item["total_tokens"] is None for item in history.recent(2, client)))
        self.assertEqual(len(self.calls), 4)

    def test_system_ca_bundle_is_used_when_python_has_no_default(self):
        context = object()
        with tempfile.TemporaryDirectory() as temp:
            bundle = Path(temp) / "cert.pem"
            bundle.touch()
            with patch("jev_router.routing.ssl.get_default_verify_paths", return_value=SimpleNamespace(cafile=None)), \
                 patch("jev_router.routing.SYSTEM_CA_BUNDLE", bundle), \
                 patch("jev_router.routing.ssl.create_default_context", return_value=context):
                selected = choose_model("diagnostic", "codex", validate_config(EXAMPLE))
        self.assertEqual(selected.reason, "jev")
        self.assertIs(self.contexts[-1], context)


class SetupTests(unittest.TestCase):
    def test_setup_preserves_unrelated_settings_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            env = {"JEV_ROUTER_HOME": temp + "/state", "CLAUDE_CONFIG_DIR": temp + "/claude", "CODEX_HOME": temp + "/codex"}
            with patch.dict(os.environ, env):
                claude = Path(env["CLAUDE_CONFIG_DIR"])
                codex = Path(env["CODEX_HOME"])
                claude.mkdir()
                codex.mkdir()
                (claude / "settings.json").write_text(json.dumps({"permissions": {"defaultMode": "plan"}}))
                (codex / "config.toml").write_text('model = "gpt-6-sol"\n[features]\nhooks = true\n')
                changes = planned_settings(validate_config(EXAMPLE))
                preview = preview_settings(changes)
                self.assertIn("mcp_servers.jev_router", preview)
                self.assertIn("jev-router:coordinator", preview)
                self.assertFalse((Path(env["JEV_ROUTER_HOME"]) / "router-config.json").exists())
                backups = apply_settings(changes)
                self.assertEqual(len(backups), 2)
                self.assertTrue(all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in backups))
                claude_data = json.loads((claude / "settings.json").read_text())
                codex_data = tomllib.loads((codex / "config.toml").read_text())
                self.assertEqual(claude_data["agent"], "jev-router:coordinator")
                model_command = (claude / "commands" / "jev-router.md").read_text()
                self.assertIn("pool show --client claude --json", model_command)
                self.assertIn(str(ROOT), model_command)
                self.assertNotIn("${CLAUDE_PLUGIN_ROOT}", model_command)
                self.assertEqual(claude_data["permissions"]["defaultMode"], "plan")
                self.assertEqual(codex_data["model"], "gpt-6-sol")
                self.assertFalse((codex / "jev-router.config.toml").exists())
                self.assertEqual(codex_data["features"]["hooks"], True)
                self.assertIn("jev_router", codex_data["mcp_servers"])
                self.assertEqual(codex_data["mcp_servers"]["jev_router"]["cwd"], str(ROOT))
                worker = (codex / "agents" / "jev-worker.toml").read_text()
                self.assertNotIn("\nmodel =", worker)
                self.assertEqual(preview_settings(planned_settings(validate_config(EXAMPLE))), "")

    def test_key_storage_redaction_and_unrelated_hooks_preserved(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {
            "JEV_ROUTER_HOME": temp + "/state", "CLAUDE_CONFIG_DIR": temp + "/claude",
            "CODEX_HOME": temp + "/codex", "TYPESAFE_API_KEY": "settings-secret",
        }):
            claude = Path(temp) / "claude"
            codex = Path(temp) / "codex"
            claude.mkdir()
            codex.mkdir()
            (claude / "settings.json").write_text(json.dumps({"permissions": {"allow": ["Bash"]}}))
            hooks = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo unrelated"}]}]}}
            (codex / "hooks.json").write_text(json.dumps(hooks))
            changes = planned_settings(validate_config(EXAMPLE), key="settings-secret")
            preview = preview_settings(changes)
            self.assertNotIn("settings-secret", preview)
            self.assertIn("[REDACTED]", preview)
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(cli_main(["setup", "--apply"]), 0)
            self.assertEqual(json.loads((claude / "settings.json").read_text())["permissions"], {"allow": ["Bash"]})
            self.assertEqual(json.loads((claude / "settings.json").read_text())["env"]["TYPESAFE_API_KEY"], "settings-secret")
            self.assertEqual(tomllib.loads((codex / "config.toml").read_text())["mcp_servers"]["jev_router"]["env"]["TYPESAFE_API_KEY"], "settings-secret")
            self.assertEqual(json.loads((codex / "hooks.json").read_text()), hooks)
            self.assertEqual(stat.S_IMODE((claude / "settings.json").stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE((codex / "config.toml").stat().st_mode), 0o600)
            self.assertEqual(preview_settings(planned_settings(validate_config(EXAMPLE), key="settings-secret")), "")

    def test_no_gui_or_tty_gives_terminal_instruction(self):
        with patch("jev_router.key_prompt._tk_prompt", return_value=None), \
             patch("jev_router.key_prompt._native_prompt", return_value=None), \
             patch("jev_router.key_prompt.sys.stdin.isatty", return_value=False):
            with self.assertRaisesRegex(ValueError, "interactive terminal"):
                hidden_key_prompt()

    def test_native_hidden_prompt_commands_on_supported_platforms(self):
        for platform, os_name, executable in (
            ("darwin", "posix", "osascript"),
            ("linux", "posix", "zenity"),
            ("win32", "nt", "powershell.exe"),
        ):
            with self.subTest(platform=platform), \
                 patch("jev_router.key_prompt.sys.platform", platform), \
                 patch("jev_router.key_prompt.os.name", os_name), \
                 patch("jev_router.key_prompt.shutil.which", return_value=executable), \
                 patch("jev_router.key_prompt.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout="hidden-key")) as run:
                self.assertEqual(_native_prompt(), "hidden-key")
                self.assertEqual(run.call_args.args[0][0], executable)
                self.assertNotIn("hidden-key", " ".join(run.call_args.args[0]))

    def test_failed_setup_does_not_write_custom_config(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {
            "JEV_ROUTER_HOME": temp + "/state", "CLAUDE_CONFIG_DIR": temp + "/claude",
            "CODEX_HOME": temp + "/codex", "TYPESAFE_API_KEY": "test-secret",
        }):
            custom = json.loads(json.dumps(EXAMPLE))
            custom["clients"]["codex"]["fallback"] = "gpt-6-luna"
            custom_path = Path(temp) / "custom.json"
            custom_path.write_text(json.dumps(custom))
            changes = planned_settings(validate_config(custom), client="codex", key="test-secret")
            self.assertIn(Path(temp) / "state" / "router-config.json", changes)
            with patch("jev_router.cli.apply_settings", side_effect=OSError("write failed")):
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    self.assertEqual(cli_main(["setup", "--client", "codex", "--config", str(custom_path), "--apply"]), 1)
            self.assertFalse((Path(temp) / "state" / "router-config.json").exists())

    def test_setup_without_hidden_entry_does_not_write(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {
            "JEV_ROUTER_HOME": temp + "/state", "CLAUDE_CONFIG_DIR": temp + "/claude",
            "CODEX_HOME": temp + "/codex", "TYPESAFE_API_KEY": "",
        }), patch("jev_router.cli.read_typesafe_key", return_value=None), \
             patch("jev_router.cli.hidden_key_prompt", side_effect=ValueError("interactive terminal required")):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(cli_main(["setup", "--client", "claude", "--apply"]), 1)
            self.assertFalse((Path(temp) / "claude" / "settings.json").exists())

    def test_existing_api_key_helper_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": temp}):
            (Path(temp) / "settings.json").write_text('{"apiKeyHelper":"other-command"}')
            with self.assertRaisesRegex(ValueError, "apiKeyHelper"):
                planned_settings(validate_config(EXAMPLE))


if __name__ == "__main__":
    unittest.main()
