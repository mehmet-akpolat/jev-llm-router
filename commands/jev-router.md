---
description: Show or override the Claude or Codex model pool used by Jev Router.
argument-hint: "[show | client and model choices]"
---

Use the bundled Python CLI through the same `uv run --no-project --python 3.11 --directory "${CLAUDE_PLUGIN_ROOT}" python -m jev_router` launcher as the setup and history commands. The user invokes this workflow with `/jev-router`; keep launcher details out of the user-facing instructions.

Run the launcher's `pool show --json` first. If `$ARGUMENTS` is empty or asks to show the current pools, display the Claude and Codex models and fallbacks, then ask which client and model IDs to change if needed. If `$ARGUMENTS` asks to export the template (`init`), run the launcher's `init` command with an absolute destination in the user's current project, never inside the installed plugin.

When `$ARGUMENTS` specifies a pool change using model IDs already in the active or bundled pool, run `pool set --client <claude|codex> --models <comma-separated-ids> --fallback <id>` through that launcher to preview. If the request is complete, rerun with `--apply` and report the saved pool. For a new model ID or changed model description or cost weight, create a complete JSON config in a temporary file based on `pool show --json`, validate it with `pool import --file <path> --client <claude|codex|both>`, then apply it with `--apply`. Remove the temporary file afterward. Never put a TypeSafe key or prompt text in the model config. Tell the user to restart the affected assistant so coordinator settings reload.
