---
name: jev-router-models
description: Show or override the Codex model pool used by Jev Router.
---

Locate this SKILL.md file, then resolve the plugin root as the parent of its `codex` directory. Use `uv run --no-project --python 3.11 --directory <plugin-root> python -m jev_router` as the bundled launcher. Run `pool show --client codex --json` to inspect the Codex pool. If the user asks to export the template (`init`), run `init` with an absolute destination in the current project, never inside the installed plugin. For a request that names existing or bundled models, preview with `pool set --client codex --models <comma-separated-ids> --fallback <id>`, then apply with `--apply` when the request gives a complete choice. For new model IDs or metadata, make a JSON config in a temporary file based on `pool show --client codex --json` and use `pool import --file <path> --client codex` followed by `--apply`; the import merges only the Codex pool into the shared config. Remove the temporary file afterward. Do not put a TypeSafe key or prompt text in the config. Tell the user to restart Codex after applying.
