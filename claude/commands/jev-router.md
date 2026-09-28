---
description: Show or override the Claude model pool used by Jev Router.
argument-hint: "[show | client and model choices]"
---

Use the bundled Python CLI through the same `uv run --no-project --python 3.11 --directory "${CLAUDE_PLUGIN_ROOT}" python -m jev_router` launcher as the setup and history commands. The user invokes this workflow with `/jev-router`; keep launcher details out of the user-facing instructions.

Run the launcher's `pool show --client claude --json` first. If `$ARGUMENTS` is empty or asks to show the current pools, display only the Claude models and fallback, then ask which client and model IDs to change if needed. If `$ARGUMENTS` asks to export the template (`init`), run the launcher's `init` command with an absolute destination in the user's current project, never inside the installed plugin.

When `$ARGUMENTS` specifies a pool change using model IDs already in the active or bundled pool, run `pool set --client claude --models <comma-separated-ids> --fallback <id>` through that launcher to preview. If the request is complete, rerun with `--apply` and report the saved pool. For a new model ID or changed model description or cost weight, create a JSON config in a temporary file based on `pool show --client claude --json`, validate it with `pool import --file <path> --client claude`, then apply it with `--apply`. The import merges only the Claude pool into the shared config. Remove the temporary file afterward. Never put a TypeSafe key or prompt text in the model config. Tell the user to restart Claude so coordinator settings reload.
