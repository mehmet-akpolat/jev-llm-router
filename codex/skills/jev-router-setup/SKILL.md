---
name: jev-router-setup
description: Configure Jev Router for Codex CLI with a hidden TypeSafe key prompt.
---

Locate this SKILL.md file, then resolve the plugin root as the parent of its `codex` directory. Run `uv run --no-project --python 3.11 --directory <plugin-root> python -m jev_router setup --client codex` to preview and then run it with `--apply`. Never ask for or show the TypeSafe key in chat. The local helper collects it through a hidden prompt. Report `/hooks` trust and restart steps. Setup leaves their ordinary Codex model unchanged.
