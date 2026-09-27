<div align="center">

# Jev LLM Router

### Route each coding prompt to a model recommended by TypeSafe Jev.

Jev LLM Router is a Python MCP plugin for Claude Code and Codex CLI. It delegates each
prompt to a model-selected worker while keeping the assistants' native interfaces and
subscription sign-ins. Only a TypeSafe API key is needed for Jev.

[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![MCP](https://img.shields.io/badge/MCP-stdio-5A67D8)](https://modelcontextprotocol.io/)
[![SQLite](https://img.shields.io/badge/SQLite-history-003B57?logo=sqlite&logoColor=white)](https://www.sqlite.org/)
[![uv](https://img.shields.io/badge/Python_runner-uv-DE5FE9)](https://docs.astral.sh/uv/)
[![Claude Code](https://img.shields.io/badge/Claude_Code-plugin-D97757)](https://code.claude.com/docs/en/plugins)
[![Codex CLI](https://img.shields.io/badge/Codex_CLI-plugin-101010)](https://developers.openai.com/plugins/build/plugins)
[![License](https://img.shields.io/badge/License-MIT-D22128)](LICENSE)

Open source under the [MIT License](LICENSE).

[https://github.com/mehmet-akpolat/jev-llm-router](https://github.com/mehmet-akpolat/jev-llm-router)

</div>

## How routing works

~~~mermaid
flowchart LR
    A[New user prompt] --> B[Prompt hook creates route ID]
    B --> C[Coordinator calls choose_model once]
    C --> D{Jev returns a valid, confident choice?}
    D -- Yes --> E[Recommended model]
    D -- No --> F[Configured fallback]
    E --> G[Worker subagent]
    F --> G
    G --> H[Worker completes its tool loop]
    H --> I[Completion hooks match token usage]
    C --> J[Recommendation saved in SQLite]
    I --> J
    J --> K[History command]
~~~

The coordinator sends the current prompt and the eligible models in that client's pool to Jev. The MCP tool validates Jev's answer and returns a fallback for a missing key, timeout, low confidence, invalid answer, or incompatible pool. The coordinator then delegates to a worker with that model. It should call Jev **once per new user prompt**, so the worker stays on its selected model through tool calls. A follow-up prompt gets a fresh decision.

Routing is **best effort**: assistant instructions ask for the MCP call and delegation, but cannot force either. The coordinator remains active and also consumes subscription allowance. Workers have separate context, so the coordinator must pass relevant earlier decisions. History labels models as *recommendations* because it cannot prove which model the assistant actually ran.

## Technology stack

| Layer | Implementation |
| --- | --- |
| Runtime | Python 3.11+ and `uv`; no Node runtime or third-party Python dependencies |
| Routing | Local stdio MCP server, TypeSafe Jev request, per-client JSON model pools |
| Assistants | Claude Code plugin commands and agents; Codex CLI plugin skills and subagent |
| History | Native lifecycle hooks, private SQLite database, terminal token and cost view |

## Install and set up

You need [uv](https://docs.astral.sh/uv/), Python 3.11+, a TypeSafe API key, and a signed-in Claude Code or Codex CLI. Claude Code requires version 2.1.283 or newer. The Python runtime is bundled with the plugin; there is no separate package installation step.

### Claude Code

Add the Git marketplace and install the plugin from a terminal:

~~~sh
claude plugin marketplace add https://github.com/mehmet-akpolat/jev-llm-router
claude plugin install jev-router@jev-router-marketplace
claude
~~~

See [Claude Code plugin installation](https://code.claude.com/docs/en/plugins/install) for marketplace and plugin management.

In the Claude Code session, run `/jev-router:setup`. It previews the changes, applies them, and opens a **hidden local prompt** for the TypeSafe key when needed. Restart Claude Code after setup. Setup enables the coordinator agent, MCP connection, lifecycle hooks, and a user-level `/jev-router` model command.

An illustrative setup output excerpt:

~~~text
> /jev-router:setup
Assistant setup targets:
  .../.claude/settings.json (will update)
Preview only: no files are written without --apply.
...
Assistant settings applied. Backups: ...
Restart the configured assistant
~~~

### Codex CLI

~~~sh
codex plugin marketplace add https://github.com/mehmet-akpolat/jev-llm-router
codex plugin add jev-router@jev-router-marketplace
codex
~~~

See [OpenAI's plugin packaging and marketplace guide](https://developers.openai.com/plugins/build/plugins) for Codex marketplace behavior and hook trust.

In Codex, open `/skills` and select **Jev Router Setup**. After setup, open `/hooks` and trust the Jev Router hooks, then restart Codex. Setup writes the `jev_router` MCP connection, coordinator guidance, and a `jev_worker` agent with no pinned model. Check the connection with `codex mcp list` from a terminal.

Codex exposes these workflows as skills in `/skills`; it does not expose this plugin's Claude-style slash commands. `$jev-router-setup`, `$jev-router-models`, and `$jev-router-history` are optional skill shortcuts.

Setup shows a diff before writing, backs up changed files, and preserves unrelated settings. It stores the TypeSafe key in Claude's user `settings.json` environment or Codex's `mcp_servers.jev_router.env` in `config.toml`, with private file permissions. It never writes a new key file. `TYPESAFE_API_KEY` in the process environment overrides the stored value. An old `typesafe-key` file is removed only after successful migration. Keep the key out of chat and command arguments.

If the hidden key dialog cannot open, run setup from an interactive terminal in a checkout so its hidden terminal prompt can be used. For Claude, use the command below; change `claude` to `codex` for Codex. Setup reports when neither a graphical nor terminal prompt is available. The installer and examples here target the **terminal clients and Git marketplaces**.

~~~sh
uv run --no-project --python 3.11 python -m jev_router setup --client claude --apply
~~~

## Use the router

After setup, send ordinary coding prompts. You do not need to prefix each prompt with a router command. Check the assistant's subagent view to confirm its actual worker model.

| Task | Claude Code | Codex CLI |
| --- | --- | --- |
| Set up | `/jev-router:setup` | `/skills` → **Jev Router Setup** |
| Show or change model pools | `/jev-router` | `/skills` → **Jev Router Models** |
| View route history | `/jev-router:history` | `/skills` → **Jev Router History** |

### Route two prompts

The following is an **illustrative** Claude Code session. Jev's answer depends on the prompt and active model pool.

~~~text
$ claude
> Fix the typo in the README heading.
Jev recommendation: claude-haiku-4-5-20251001
Worker completes the edit.

> Now trace the intermittent failure in the integration tests.
Jev recommendation: claude-sonnet-5
Worker investigates, runs tests, and reports the result.
~~~

The second prompt gets a new recommendation. The coordinator should include relevant context from the first prompt when spawning the second worker.

In Codex CLI, the same flow starts with ordinary prompts. Use `/skills` for the management workflows:

~~~text
$ codex
> /skills
  Select Jev Router History
> Show the last 5 Codex routes.
Recent recommendations, assistant tokens, and notional USD reduction ...
~~~

### Inspect or change a model pool

~~~text
> /jev-router
claude (fallback: claude-sonnet-5)
  claude-haiku-4-5-20251001  weight=1
  claude-sonnet-5            weight=2.6
  claude-opus-5-5            weight=5.2

> /jev-router use Claude Haiku 4.5 and Sonnet 5, with Sonnet 5 as fallback
~~~

The model command previews the pool change before applying it. In Codex, select **Jev Router Models** from `/skills` and ask for the same change in plain language. For example: “Use `gpt-6-luna` and `gpt-6-sol` for Codex, with `gpt-6-sol` as fallback.” Restart the affected assistant after a change. To export an editable JSON template, use `/jev-router init` in Claude or ask **Jev Router Models** to export it in Codex; this is optional.

The bundled [model configuration](jev_router/router-config.example.json) is the default. `cost_weight` is a relative price proxy **within one client's pool**; Jev also considers likely reasoning, output, tool-loop length, and retries. The bundled weights are 1 / 2.6 / 5.2 for Claude Haiku 4.5 / Sonnet 5 / Opus 5.5 and 1 / 20 / 100 for Codex GPT-6 Luna / Sol / Astra. These weights are not measured cost per completed task. See [Anthropic pricing](https://platform.claude.com/docs/en/about-claude/pricing) and [OpenAI pricing](https://developers.openai.com/api/docs/pricing).

For custom IDs or weights, the model workflow can import a complete JSON config. Existing `router-config.json` files remain supported. Configuration resolution is: explicit `--config` in the underlying Python CLI, then `JEV_ROUTER_CONFIG`, then the private runtime config, then `router-config.json` in the current directory, then the bundled default. Each pool's fallback must name one of its models. Use only models available in your assistant subscription and compatible with the worker's tools.

### View history

In Claude Code, `/jev-router:history --limit 5 --client claude` shows a filtered table. In Codex, select **Jev Router History** in `/skills` and ask for the last five Codex routes. The underlying history command also supports `--json` for structured output.

Example terminal output (illustrative values):

~~~text
Recent recommendations, assistant tokens, and notional USD reduction vs the prompt's baseline model
Negative reduction means an estimated increase; amounts include coordinator usage
When (UTC)          Client Recommended       Baseline              Main   Worker    Total     Base $   Routed $     Est. Δ$  Tokens
2026-09-27T14:02:50 codex  gpt-6-luna        gpt-6-sol            1100    11000    12100    0.02640    0.00396    +0.02244  ████████████████
~~~

Main is coordinator usage; Worker is subagent usage. Base $ prices the worker's observed tokens as if the prompt had run on the model active at prompt start. Routed $ adds coordinator usage on that baseline model and worker usage on the recommended model. **Est. Δ$ = Base $ − Routed $**; a negative value means the estimate increased. The comparison uses standard API list prices per million uncached input, cached input, cache writes, and output tokens as recorded on 2026-09-27 in [the pricing table](jev_router/costs.py), with an approximate Claude tokenizer adjustment. These are **what-if USD estimates**, not subscription charges or verified savings. Model behavior, discounts, longer cache writes, and TypeSafe costs are outside the estimate.

History uses a private `JEV_ROUTER_HOME/history.sqlite3` SQLite database to store route IDs, model names, transcript paths, and token counts. It does **not** store prompt text or the TypeSafe key. Legacy `routes.jsonl` entries are imported once and retained. Counts and costs show `unknown` when hooks did not run, usage cannot be matched confidently, a model has no known price, or older records lack the data needed for an estimate.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Claude commands are missing | Confirm `claude plugin list` includes the plugin, then restart Claude Code. Run `claude plugin validate . --strict` from a checkout if installation fails. |
| Jev Router skills are missing in Codex | Confirm `codex plugin list` includes the plugin, restart Codex, then open `/skills`. |
| MCP tool is unavailable | Run `codex mcp list` for Codex or inspect `/mcp` in Claude. Rerun setup and restart; check that `uv` and Python 3.11+ are on the assistant's `PATH`. The coordinator uses the configured fallback if it cannot call Jev. |
| Every prompt uses the fallback | Check the TypeSafe key in assistant settings, network access to TypeSafe, and `timeout_ms`. Other fallback reasons include `low_confidence`, `invalid_answer`, and `no_text_prompt`. |
| No hidden key prompt appears | Use setup in a local interactive terminal with a graphical desktop or TTY. The key must not be entered in assistant chat. |
| No history rows, or token counts are `unknown` | Send a new prompt after setup. In Codex, review and trust hooks with `/hooks`; restart after trusting them. Usage remains `unknown` when the transcript or worker cannot be matched reliably. |
| Estimated cost is `unknown` or negative | `unknown` means missing usage, baseline, or price data. A negative amount is a possible increase after coordinator overhead and the selected worker's price. |
| Worker model differs from the recommendation | Check that the model is available in the subscription and that the coordinator passed it as the worker's explicit model. Routing instructions are best effort. |
| Subscription sign-in is unexpectedly bypassed | Remove conflicting `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, or `OPENAI_API_KEY` environment overrides and any old gateway settings, then rerun setup. |

## Development

From a checkout, run:

~~~sh
python3 -m unittest discover -s tests -v
claude plugin validate . --strict
~~~

Tests use mock Jev replies and fixture transcripts; they do not call provider models. For a live check, send two different prompts in one conversation, inspect one recommendation and one worker delegation per prompt, and compare history totals with the assistant's usage view.
