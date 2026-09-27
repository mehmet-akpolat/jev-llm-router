---
name: jev-router-models
description: Show or override the Claude or Codex model pool used by Jev Router.
---

Locate this SKILL.md file, then resolve the plugin root as the parent of its `codex-skills` directory. Use `uv run --no-project --python 3.11 --directory <plugin-root> python -m jev_router` as the bundled launcher. Run `pool show --json` to inspect the current pools. If the user asks to export the template (`init`), run `init` with an absolute destination in the current project, never inside the installed plugin. For a request that names existing or bundled models, preview with `pool set --client <claude|codex> --models <comma-separated-ids> --fallback <id>`, then apply with `--apply` when the request gives a complete choice. For new model IDs or metadata, make a complete JSON config in a temporary file and use `pool import --file <path> --client <claude|codex|both>` followed by `--apply`; remove the temporary file afterward. Do not put a TypeSafe key or prompt text in the config. Tell the user to restart the affected assistant after applying.
