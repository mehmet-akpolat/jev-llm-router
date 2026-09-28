"""One Python runtime with separate Claude and Codex plugin resources."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


class PluginLayoutTests(unittest.TestCase):
    def run_router(self, *arguments: str, home: Path) -> subprocess.CompletedProcess[str]:
        env = {
            **os.environ,
            "PYTHONPATH": str(ROOT),
            "HOME": str(home),
            "CLAUDE_CONFIG_DIR": str(home / "claude-settings"),
            "CODEX_HOME": str(home / "codex-settings"),
            "JEV_ROUTER_HOME": str(home / "state"),
            "TYPESAFE_API_KEY": "test-secret",
        }
        env.pop("JEV_ROUTER_CONFIG", None)
        return subprocess.run([sys.executable, "-m", "jev_router", *arguments], cwd=home, env=env, text=True, capture_output=True, check=False)

    def test_marketplaces_point_to_one_root_and_client_assets(self):
        claude_catalog = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text())
        codex_catalog = json.loads((ROOT / ".agents/plugins/marketplace.json").read_text())
        self.assertEqual(claude_catalog["plugins"][0]["source"], "./")
        self.assertEqual(codex_catalog["plugins"][0]["source"]["path"], "./")
        claude = json.loads((ROOT / ".claude-plugin/plugin.json").read_text())
        codex = json.loads((ROOT / ".codex-plugin/plugin.json").read_text())
        self.assertEqual(claude["commands"], "./claude/commands/")
        self.assertEqual(claude["hooks"], "./claude/hooks/hooks.json")
        self.assertEqual(claude["mcpServers"], "./claude/.mcp.json")
        self.assertTrue((ROOT / "agents/coordinator.md").is_file())
        self.assertTrue((ROOT / "agents/worker.md").is_file())
        self.assertEqual(codex["skills"], "./codex/skills/")
        self.assertEqual(codex["hooks"], "./codex/hooks/hooks.json")
        for client in ("claude", "codex"):
            hooks = json.loads((ROOT / client / "hooks/hooks.json").read_text())["hooks"]
            for groups in hooks.values():
                for group in groups:
                    for hook in group["hooks"]:
                        self.assertIn(f"'--client','{client}'", hook["command"])
            self.assertFalse((ROOT / client / "jev_router").exists())
        self.assertIn("PostModelSwitch", json.loads((ROOT / "claude/hooks/hooks.json").read_text())["hooks"])
        codex_hooks = json.loads((ROOT / "codex/hooks/hooks.json").read_text())["hooks"]
        self.assertNotIn("PostModelSwitch", codex_hooks)
        for event in ("SubagentStop", "Stop"):
            self.assertFalse(codex_hooks[event][0]["hooks"][0].get("async", False))
        self.assertFalse((ROOT / "scripts/sync_plugin_runtime.py").exists())

    def test_one_package_imports_from_outside_checkout(self):
        with tempfile.TemporaryDirectory() as temp:
            imported = subprocess.run([sys.executable, "-c", "import jev_router; print(jev_router.__file__)"],
                                      cwd=temp, env={**os.environ, "PYTHONPATH": str(ROOT)}, capture_output=True, text=True)
            self.assertEqual(imported.returncode, 0, imported.stderr)
            self.assertEqual(Path(imported.stdout.strip()).resolve(), ROOT / "jev_router/__init__.py")

    def test_setup_targets_each_client_and_can_target_both(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            for client, other in (("claude", "codex"), ("codex", "claude")):
                with self.subTest(client=client):
                    preview = self.run_router("setup", "--client", client, home=home)
                    self.assertEqual(preview.returncode, 0, preview.stderr)
                    self.assertIn("Preview only", preview.stdout)
                    self.assertNotIn("test-secret", preview.stdout)
                    self.assertNotIn(f"{other}-settings", preview.stdout)
                    applied = self.run_router("setup", "--client", client, "--apply", home=home)
                    self.assertEqual(applied.returncode, 0, applied.stderr)
                    repeated = self.run_router("setup", "--client", client, "--apply", home=home)
                    self.assertEqual(repeated.returncode, 0, repeated.stderr)
                    self.assertIn("already configured", repeated.stdout)
                    show = self.run_router("pool", "show", "--client", client, "--json", home=home)
                    self.assertEqual(set(json.loads(show.stdout)["clients"]), {client})
            both = self.run_router("setup", home=home)
            self.assertEqual(both.returncode, 0, both.stderr)
            self.assertIn("claude-settings", both.stdout)
            self.assertIn("codex-settings", both.stdout)
            codex = tomllib.loads((home / "codex-settings/config.toml").read_text())
            self.assertNotIn("model", codex)
            self.assertEqual(Path(codex["mcp_servers"]["jev_router"]["cwd"]), ROOT)
            self.assertFalse((home / "codex-settings/jev-router.config.toml").exists())
            command = (home / "claude-settings/commands/jev-router.md").read_text()
            self.assertIn(str(ROOT), command)
            self.assertIn("pool show --client claude --json", command)

    def test_partial_pool_import_preserves_other_pool(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            current = self.run_router("pool", "show", "--client", "codex", "--json", home=home)
            self.assertEqual(current.returncode, 0, current.stderr)
            data = json.loads(current.stdout)
            data["clients"]["codex"]["fallback"] = "gpt-6-luna"
            source = home / "codex-pool.json"
            source.write_text(json.dumps(data))
            imported = self.run_router("pool", "import", "--file", str(source), "--client", "codex", "--apply", home=home)
            self.assertEqual(imported.returncode, 0, imported.stderr)
            stored = json.loads((home / "state/router-config.json").read_text())
            self.assertEqual(stored["clients"]["codex"], data["clients"]["codex"])
            self.assertIn("claude", stored["clients"])

    def test_mcp_starts_from_one_package(self):
        with tempfile.TemporaryDirectory() as temp:
            request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n"
            env = {**os.environ, "PYTHONPATH": str(ROOT), "JEV_ROUTER_HOME": str(Path(temp) / "state")}
            started = subprocess.run([sys.executable, "-m", "jev_router", "mcp"], cwd=temp, env=env,
                                     input=request, capture_output=True, text=True)
            self.assertEqual(started.returncode, 0, started.stderr)
            self.assertEqual(json.loads(started.stdout)["result"]["tools"][0]["name"], "choose_model")


if __name__ == "__main__":
    unittest.main()
