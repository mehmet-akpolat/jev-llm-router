---
description: Configure Jev Router for Claude Code without entering the TypeSafe key in chat.
---

Run `uv run --no-project --python 3.11 --directory "${CLAUDE_PLUGIN_ROOT}" python -m jev_router setup --client claude` to show the preview. Then run the same command with `--apply`. The local helper collects the TypeSafe key through a hidden prompt; never ask for the key in chat or command arguments. Report settings changed and the restart requirement without displaying secrets.
