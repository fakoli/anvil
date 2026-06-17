<div align="center">

![fakoli-state](assets/logo-256.png)

# fakoli-state

> fakoli-state turns rough ideas and PRDs into reviewed, lockable, evidence-backed work packets that humans and AI coding agents can execute in parallel without stepping on each other — the canonical project-state layer that fakoli-flow and fakoli-crew compose around.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Plugin Version](https://img.shields.io/badge/version-1.17.0-blue.svg)](.claude-plugin/plugin.json)
[![Marketplace](https://img.shields.io/badge/marketplace-fakoli-purple.svg)](https://github.com/fakoli/fakoli-state)
[![Tests](https://img.shields.io/badge/tests-1103%20passing-brightgreen.svg)](tests)

</div>

---

## Why fakoli-state

fakoli-state is a local-first, backend-neutral project-state layer for humans and AI coding agents — the durable record of every requirement, task, claim, and piece of evidence in your project, stored in SQLite under `.fakoli-state/` and exposed through a CLI and an MCP server.

It is for developers running Claude Code, Codex, Cursor, OpenHands, or Copilot who need multiple agents (and multiple humans) to coordinate against the same plan without overwriting each other. Solo builders who want PRDs that survive sessions. Project leads who want truth that outlives any one chat.

When an AI agent claims a task, that claim is an enforced database row with a lease and a heartbeat — not a convention in a markdown file that the next agent can silently overwrite.

---

## The trinity

fakoli-flow defines how work moves, fakoli-crew defines who does the work, and fakoli-state defines what is true. The three plugins compose: when all three are installed, `flow:execute` reads `fakoli-state next`, dispatches the right crew specialist, and submits evidence back to canonical state before the merge gate. When fakoli-state is absent, flow and crew fall back to their markdown-status conventions.

---

## What ships today (v1.17.0)

| Surface | Count | Notes |
|---|---|---|
| CLI commands | **23** | Top-level + `prd`, `review`, `hook`, `sync` sub-apps. v1.17.0: `--use-llm` augmentation now picks Anthropic API / Bedrock / OpenAI-compatible endpoints via the same multi-provider resolver as the LLM-planner backstop. |
| MCP tools | **22** | FastMCP stdio; works in any MCP-compatible client. v1.17.0: `plan_tasks` honors the project's `llm_provider` / `llm_tier` / Bedrock+custom knobs. |
| Skills | **8 skills** | start-prd, prd, plan, claim, execute, finish, state-ops, resolve-decisions |
| Agents | **6 agents** | planner (opus), critic (opus), docs-scribe (sonnet), marketplace-scribe (sonnet), sentinel (haiku), state-keeper (haiku) — tier-mapped in v1.17.0 per [docs/model-strategy.md](docs/model-strategy.md) |
| Hooks | **4 hooks** | detect-state, check-claim, record-file-change, capture-evidence |
| LLM providers | **3** | Anthropic API (default) · Amazon Bedrock (`[bedrock]` extra) · OpenAI-compatible custom endpoints (`[custom]` extra). See [docs/llm-providers.md](docs/llm-providers.md). |

Highlights from v1.17.0:

- **Multi-provider LLM access.** `BedrockProvider` (boto3 chain) and `CustomEndpointProvider` (vLLM / OpenRouter / LiteLLM-proxy / Together / Groq / Azure-OpenAI / self-hosted) ship alongside the existing `AnthropicProvider`. Precedence: explicit config > env auto-detect > fail loudly. Optional extras keep the default install lean.
- **Tier-aware model defaults.** New `MODEL_TIERS` vocabulary (`opus` / `sonnet` / `haiku`) with per-agent tier mapping that drops typical session cost ~60% versus the prior "everything routes to Opus" pattern. Override always wins.
- **Plugin-critic extraction.** The five plugin-surface critics (`agent-critic`, `skill-critic`, `hook-critic`, `mcp-critic`, `structure-critic`) move out of `fakoli-crew` 2.3.0 into a dedicated `fakoli-plugin-critic` 0.1.0 plugin so plugin-development teams can install only the review layer.
- 1103 tests passing (+20 since v1.16.0); SQLite schema unchanged.

Full release notes in [CHANGELOG.md](CHANGELOG.md).

---

## Quick Start

```bash
# 1. Scaffold per-project state
fakoli-state init --name "My Project"
# → creates .fakoli-state/{config.yaml,state.db,events.jsonl,packets/}
# → next step: author your PRD at .fakoli-state/prd.md

# 2. Author the PRD against the template (see docs/prd-template.md)
$EDITOR .fakoli-state/prd.md

# 3. Parse, review, approve — the state machine requires draft → reviewed → approved
fakoli-state prd parse
# → Parsed PRD: 4 requirements, 12 tasks staged for review
fakoli-state prd review             # draft → reviewed
fakoli-state prd review --approve   # reviewed → approved

# 4. Generate features and tasks; score across six dimensions
fakoli-state plan
fakoli-state score
# → tabular output: TaskID / Complexity / Parallel / CtxLoad / Blast / Review / Agent (1–5)
fakoli-state review tasks

# 5. Pick the next ready task and claim it
fakoli-state next
# → T001 — "Wire orchestrator retry to DLQ" (ready, no conflicts)
fakoli-state claim T001
# → Claim C001 active; branch agent/t001-<slug> created

# 6. Get the work packet, do the work, submit evidence
fakoli-state packet T001
fakoli-state submit T001 \
    --commands "pytest tests/test_retry.py" \
    --files-changed src/orchestrator/retry.py

# 7. Apply the review verdict — promotes needs_review → accepted → done
fakoli-state apply T001 --approve
# → Task T001 applied; event task.applied recorded in events.jsonl
```

> To break a complex task into subtasks, use `fakoli-state expand T001 --use-llm` (requires `ANTHROPIC_API_KEY`) or author `T001.1` / `T001.2` rows directly in `prd.md`. Full command reference forthcoming in [`docs/cli-reference.md`](docs/cli-reference.md).

Every mutation appends to `.fakoli-state/events.jsonl`. Replaying the log from scratch against an empty database reconstructs `state.db` byte-for-byte — the audit guarantee Phase 2 ships and every subsequent phase preserves.

---

## Architecture at a glance

| Layer | What it does |
|---|---|
| Skills | Workflow choreography — 8 skills: start-prd, prd, plan, claim, execute, finish, state-ops, resolve-decisions. Verification delegates to `fakoli-flow:verify` and `fakoli-crew:sentinel`. |
| CLI (`fakoli-state`) | Pure state operations — CRUD, scoring, packet generation, sync |
| MCP server | 22 agent-facing tools exposed via stdio to any MCP-compatible runtime |
| Hooks | Enforce claim discipline, record file changes, capture test evidence |
| State engine | SQLite backend + append-only JSONL event log (full replay guarantee) |
| Claims manager | Atomic SQLite transactions; stale lease detection on every operation |
| Planning engine | Deterministic template-based PRD parser; optional `--use-llm` augmentation |
| Context engine | Renders work packets as markdown or JSON from canonical state |
| Git ops | Auto-creates `agent/<task>-<slug>` branch on `claim` |
| Sync engine | Bidirectional GitHub Issues projection (polling, opt-in) |

Full architecture and lifecycle diagrams: [`docs/architecture.md`](docs/architecture.md).

---

## Comparison vs alternatives

| Wedge | fakoli-state | GitHub Issues / CCPM |
|---|---|---|
| **Canonical state shape** | Pydantic v2 models in SQLite, validated at every transition | Free-form markdown in an issue body or a `.md` file |
| **Claim / lock model** | `Claim` row with expiry + heartbeat; stale leases reaped on every call | Assignment-by-label or "I'll take this" in chat — no enforcement |
| **Agent work packets** | `fakoli-state packet T012` renders exact intent + acceptance criteria + non-goals | Agent must summarize the whole issue thread or plan |
| **Task scoring** | Six dimensions: complexity, parallelizability, context load, blast radius, review risk, agent suitability | Single-axis story points (if any) |
| **Runtime coupling** | Runtime-neutral: CLI + FastMCP stdio; any MCP client | Coupled to GitHub or to the CCPM markdown convention |

Source for the wedges: [`docs/_positioning.md`](docs/_positioning.md).

---

## Documentation

- [`docs/architecture.md`](docs/architecture.md) — layered architecture, lifecycles, audit guarantee
- [`docs/design.md`](docs/design.md) — design rationale and trade-offs
- [`docs/how-to/getting-started.md`](docs/how-to/getting-started.md) — end-to-end first-project walkthrough *(v1.11.0)*
- [`docs/cli-reference.md`](docs/cli-reference.md) — every CLI command, flag, and exit code *(v1.11.0)*
- [`docs/roadmap.md`](docs/roadmap.md) — Phase 11 plans, v2.0 and beyond backlog
- [`docs/mcp.md`](docs/mcp.md) — 22-tool MCP reference with error envelope contract
- [`docs/prd-template.md`](docs/prd-template.md) — PRD authoring schema and worked example
- [`docs/github-sync.md`](docs/github-sync.md) — bidirectional GitHub Issues sync reference
- [`docs/sync-providers.md`](docs/sync-providers.md) — contributor guide for adding Linear, Monday, Jira providers
- [`docs/llm.md`](docs/llm.md) — `--use-llm` augmentation, prompt caching, `RecordedLLMProvider` test pattern
- [`CHANGELOG.md`](CHANGELOG.md) — release history

---

## Install

### As a Claude Code plugin (recommended)

```bash
/plugin marketplace add fakoli/fakoli-state
/plugin install fakoli-state@fakoli-state
```

Installs the plugin, registers the four hooks, wires the MCP server, and makes the six agents discoverable to Claude Code at next session start.

### Standalone clone (CLI / MCP without the plugin layer)

The Python engine, CLI, and MCP server are fully self-contained under `bin/` and need only [uv](https://docs.astral.sh/uv/):

```bash
git clone https://github.com/fakoli/fakoli-state.git
cd fakoli-state/bin
uv sync                     # materializes .venv and resolves deps
uv run fakoli-state --help  # drive the CLI directly
```

The wrapper scripts `bin/fakoli-state` and `bin/fakoli-state-mcp` shell out to `uv run`, so once synced you can also add this directory to your Claude Code plugin paths and use the MCP server (`.mcp.json`) as-is.

### Install the full trinity

```bash
/plugin marketplace add fakoli/fakoli-state
/plugin install fakoli-state@fakoli-state
/plugin install fakoli-crew
/plugin install fakoli-flow
```

---

## Integration with fakoli-flow and fakoli-crew

When both fakoli-state and fakoli-flow are installed, the flow pipeline upgrades automatically:

- `flow:execute` detects fakoli-state, reads `fakoli-state next`, and calls `fakoli-state claim` before each wave. Status files are replaced by `fakoli-state submit`.
- `flow:verify` calls `fakoli-state status` and dispatches the sentinel only on tasks with submitted evidence.
- `flow:finish` calls `fakoli-state apply` per accepted task before the merge or PR.

When both fakoli-state and fakoli-crew are installed, all crew agents gain access to the `fakoli-state-mcp` MCP tool surface. The plugin-owned `agents/critic.md` and `agents/sentinel.md` defer to fakoli-crew specialists when detected.

When fakoli-state is absent, fakoli-flow and fakoli-crew continue to work via their existing markdown-status conventions. Integration is opt-in throughout.

MCP exposes capabilities; plugins encode operating discipline. The MCP server ships 22 tools any agent can call, but skills, subagents, and hooks decide *when* to claim, *which* specialist runs, *what* evidence is required, and *how* the critic gate fires. fakoli-state is plugin-first and MCP-compatible, not MCP-only.

---

## Agents shipped with this plugin

| Agent | Color | Owns | Defers to |
|---|---|---|---|
| `planner` | white | PRD-to-tasks transformation, feature/task drafting, expand routing | `fakoli-crew:guido` |
| `critic` | magenta | Code-review verdict on submitted-evidence diffs vs task acceptance criteria | `fakoli-crew:critic` |
| `sentinel` | gray | Verification-command + evidence-completeness scorecard | `fakoli-crew:sentinel` |
| `state-keeper` | teal | Sync drift detection + reconciliation triage across SQLite / FS / git | `fakoli-crew:keeper` |
| `marketplace-scribe` | cyan | `.claude-plugin/marketplace.json`, root README plugin table, `registry/*.json` | `fakoli-crew:keeper` |
| `docs-scribe` | purple | Plugin `docs/` cross-references, `CHANGELOG.md`, `plugin.json.description` | `fakoli-crew:herald` |

The Iron Rule (review agents never `Edit`/`Write`) is enforced at the `tools:` frontmatter level for `critic`, `sentinel`, `state-keeper`, and `docs-scribe`; `planner` proposes-but-does-not-mutate; `marketplace-scribe` is the only agent permitted `Bash` (it runs `scripts/generate-index.sh` and validates regenerated JSON).

---

## Status

fakoli-state shipped Phases 1–10 across v1.0.0 → v1.10.0. The Phase 10 plugin-dev audit closed every MUST FIX item; 57 SHOULD FIX / CONSIDER / NIT items are tracked in [`docs/phase-11-backlog.md`](docs/phase-11-backlog.md). v2.0 will add LinearIssuesProvider and MondayBoardsProvider, spec webhook-based sync, and the immediate-apply `*_applied` conflict-resolution variants — see [`docs/roadmap.md`](docs/roadmap.md).

---

## Requirements

- Claude Code with plugin support
- Python 3.11+ with `uv` (resolved on first invocation — no manual install)
- fakoli-flow (recommended — enables pipeline integration)
- fakoli-crew (recommended — provides specialist agents)

---

## Author

Sekou Doumbouya — [github.com/fakoli](https://github.com/fakoli)

## License

MIT — see [LICENSE](LICENSE)
