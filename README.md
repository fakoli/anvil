<div align="center">

![Anvil — system of record for agent teams](assets/banner.png)

# Anvil

> **The system of record for agent teams.**

> Durable, evidence-gated, lease-coordinated state for multi-agent software work.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Plugin Version](https://img.shields.io/badge/version-0.3.1-blue.svg)](.claude-plugin/plugin.json)
[![Marketplace](https://img.shields.io/badge/marketplace-fakoli-purple.svg)](https://github.com/fakoli/anvil)
[![Tests](https://img.shields.io/badge/tests-passing-brightgreen.svg)](tests)

</div>

> **Beta — v0.3.1.** The core loop works today; command surfaces and APIs may change before 1.0.

---

## Why Anvil

Anvil is a local-first, backend-neutral project-state layer for humans and AI coding agents. It records requirements, tasks, claims, evidence, and reviews in SQLite under `.anvil/`, then exposes that state through a CLI (`anvil`) and an MCP server.

It is for developers running Claude Code, Codex, Cursor, OpenHands, or Copilot who need multiple agents, and multiple humans, to coordinate against the same plan without overwriting each other. Solo builders can use it to keep PRDs and task state across sessions; project leads can use it to audit what work was claimed, reviewed, and completed.

When an AI agent claims a task, that claim is an enforced database row with a lease and heartbeat. Completion is evidence-gated: agents submit structured evidence before review, and with `strict_evidence: true` Anvil refuses to approve work whose required evidence is missing (advisory by default).

---

## What ships today

| Surface | Count | Notes |
|---|---|---|
| CLI command entries | **39** | 33 top-level commands plus `prd`, `review`, `hook`, `sync`, `migrate`, and `proof` sub-app entries. `--use-llm` augmentation picks Anthropic API / Bedrock / OpenAI-compatible endpoints via the same multi-provider resolver as the LLM-planner backstop. |
| MCP tools | **24** | FastMCP stdio; works in any MCP-compatible client. `plan_tasks` honors the project's `llm_provider` / `llm_tier` / Bedrock+custom knobs. |
| Skills | **8 skills** | start-prd, prd, plan, claim, execute, finish, state-ops, resolve-decisions |
| Agents | **5 agents** | planner (opus), critic (opus), docs-scribe (sonnet), sentinel (haiku), state-keeper (haiku) — tier-mapped per [docs/model-strategy.md](docs/model-strategy.md) |
| Hooks | **5 hooks** | detect-state, check-claim, record-file-change, capture-evidence, heartbeat |
| LLM providers | **4** | Claude Agent SDK (default — subscription auth, no API key) · Anthropic API · Amazon Bedrock (`[bedrock]` extra) · OpenAI-compatible custom endpoints (`[custom]` extra). See [docs/llm-providers.md](docs/llm-providers.md). |

Highlights:

- **Multi-PRD projects (v0.3).** One project can hold several release-scoped PRDs in a single partitioned `state.db`, each separately gated (a task is claimable only when its owning PRD is approved) yet coordinated globally for cross-PRD file conflicts. `anvil prd list` and `--prd <id>` scope the workflow; PRDs are revisable, with re-parse appending a non-destructive `prd.revised`. Single-PRD projects are unaffected.
- **Multi-provider LLM access.** `BedrockProvider` (boto3 chain) and `CustomEndpointProvider` (vLLM / OpenRouter / LiteLLM-proxy / Together / Groq / Azure-OpenAI / self-hosted) ship alongside the existing `AnthropicProvider`. Precedence: explicit config > env auto-detect > fail loudly. Optional extras keep the default install lean.
- **Tier-aware model defaults.** New `MODEL_TIERS` vocabulary (`opus` / `sonnet` / `haiku`) with per-agent tier mapping that drops typical session cost ~60% versus the prior "everything routes to Opus" pattern. Override always wins.
- CI covers the full pytest suite and benchmark smoke test; the on-disk SQLite schema is at version 8, auto-upgraded from older DBs via the additive v6 -> v7 -> v8 migration ladder.

Full release notes in [CHANGELOG.md](CHANGELOG.md).

---

## Quick Start

### Zero-to-next in one command

`anvil` is a standalone CLI. To see the whole loop end-to-end against a seeded sample project:

```bash
anvil init --with-sample
# → scaffolds the state workspace, writes a valid sample prd.md, and runs
#   parse → review → approve → plan → score → review tasks offline (no API key)
anvil next
# → returns a ready task immediately — nothing else to author or run
```

`--with-sample` is purely additive: plain `anvil init` is unchanged and
seeds nothing. Use the sample to learn the flow, then run
`anvil init --force` to start over for real on your own PRD as shown below
(state lives outside the repo, so deleting a local `.anvil/` is a no-op —
`--force` is the reset).

### The full loop on your own PRD

```bash
# 1. Scaffold per-project state — it lands in a HOME workspace keyed to this directory,
#    ~/.anvil/workspaces/<dirname>-<hash8>/.anvil, not in the repo itself
anvil init --name "My Project"
# → Initialized anvil for 'My Project' (id: my-project)
# → Next step: author your PRD at
#   ~/.anvil/workspaces/my-project-f4ffc446/.anvil/prd.md, then run `anvil prd parse`.

# 2. Author the PRD at the path init printed, against the template (see docs/prd-template.md)
$EDITOR ~/.anvil/workspaces/my-project-f4ffc446/.anvil/prd.md
#   Requirement IDs must be R0NN (R001, R002, …) — the strict parser refuses
#   suffixed IDs like R003a. Already have a PRD file? `anvil prd parse --file <path>`
#   parses it in place instead.

# 3. Parse, review, approve — the state machine requires draft → reviewed → approved
anvil prd parse
# → Parsed 6 requirements, 2 features, 2 tasks.
anvil prd review             # draft → reviewed
anvil prd review --approve   # reviewed → approved

# 4. Generate features and tasks; score across six dimensions
anvil plan
anvil score
# → tabular output: TaskID / Complexity / Parallel / CtxLoad / Blast / Review / Agent (1–5)
anvil review tasks

# 5. Pick the next ready task and claim it
anvil next
# → Next recommended task: T001 — "Implement Markdown link extraction"
anvil claim T001
# → Claim C7FDBA6B9 active; branch agent/t001-implement-markdown-link-extraction
#   created in your project's git repo

# 6. Get the work packet, do the work, submit evidence
anvil packet T001
anvil submit T001 \
    --commands "pytest tests/test_links.py" \
    --files-changed src/mdlinks/extract.py
# → Task 'T001' status → needs_review. A trailing `Evidence gate: INCOMPLETE`
#   line is expected here: --commands records commands as strings, while the
#   typed exit-code proofs the gate checks for come from the run hooks. The
#   gate is advisory by default, so this does not block the review.

# 7. Apply the review verdict — promotes needs_review → accepted → done
anvil apply T001 --approve
# → Task 'T001' approved by 'human' → done.
```

> To break a complex task into subtasks, use `anvil expand T001 --use-llm` (uses your Claude subscription via the Agent SDK by default — no API key) or author `T001.1` / `T001.2` rows directly in `prd.md`. See [`docs/cli-reference.md`](docs/cli-reference.md) for command details.

**Where state lives:** the workspace layout above is the default; if you want state inside the repo instead, set `ANVIL_STATE_LAYOUT=local` (restores `./.anvil`) or `ANVIL_ROOT=<dir>` (pins state to `<dir>/.anvil`) — `anvil status` always prints the real path on its `Path:` line.

Every mutation appends to `events.jsonl` in that state directory. Replaying the log from scratch against an empty database reconstructs `state.db`; this is the audit guarantee the state engine is built around.

---

## Architecture at a glance

<div align="center">
<img src="assets/anvil-hero.png" alt="Anvil terminal session showing the claim → execute → evidence → done loop" width="800" />
</div>

| Layer | What it does |
|---|---|
| Skills | Workflow choreography — 8 skills: start-prd, prd, plan, claim, execute, finish, state-ops, resolve-decisions. |
| CLI (`anvil`) | Pure state operations — CRUD, scoring, packet generation, sync |
| MCP server | 24 agent-facing tools exposed via stdio to any MCP-compatible runtime |
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

| Capability | anvil | GitHub Issues / CCPM |
|---|---|---|
| **Canonical state shape** | Pydantic v2 models in SQLite, validated at every transition | Free-form markdown in an issue body or a `.md` file |
| **Claim / lock model** | `Claim` row with expiry + heartbeat; stale leases reaped on every call | Assignment-by-label or "I'll take this" in chat — no enforcement |
| **Agent work packets** | `anvil packet T012` renders exact intent + acceptance criteria + non-goals | Agent must summarize the whole issue thread or plan |
| **Task scoring** | Six dimensions: complexity, parallelizability, context load, blast radius, review risk, agent suitability | Single-axis story points (if any) |
| **Runtime coupling** | Runtime-neutral: CLI + FastMCP stdio; any MCP client | Coupled to GitHub or to the CCPM markdown convention |
| **Always-on context cost** | Measured ~2.4k tokens (14 execution-tool schemas + skill/agent descriptions; 10 planning tools stay off the wire until `ANVIL_MCP_PLANNING=1`) — audit: [`benchmarks/CONTEXT_AUDIT.md`](benchmarks/CONTEXT_AUDIT.md) | Unmeasured — whole issue threads or plan markdown enter context on demand |

Source for this comparison: [`docs/_positioning.md`](docs/_positioning.md).

---

## Proven in real sessions

Numbers from recorded working sessions
([fakoli/post-session-findings](https://github.com/fakoli/post-session-findings)),
not projections:

- **32 tasks, 21 PRs, and a release from one 23.7-hour autonomous session** —
  the anvil loop (claim → packet → execute → evidence → review → apply) ran
  20 end-to-end task cycles under ~2 dozen human messages, and stayed
  coherent across 28M generated tokens by keeping the orchestrator at ~10%.
- **Two concurrent agent loops completed an 18/18-task PRD in 16.7 hours.**
  In a separate run, "anvil's exclusive claim deconflicted two sessions with
  zero explicit negotiation" — the lease model working in the field, not
  just in the [benchmark](benchmarks/RESULTS.md) (where it measured file
  collisions 3.0 → 0.0 vs a shared-markdown control).
- **The gates catch real defects every time they run.** Across sessions:
  a fail-open deny gate, log-injection bugs, a semantically broken "clean"
  merge, and a malformed requirement ID the strict PRD parser refused
  before it could ship a broken plan.
- **The loop is portable:** the same claim/evidence discipline ran on
  Codex, driven entirely through the CLI.

---

## Documentation

Browsable docs site: **[anvil-state.readthedocs.io](https://anvil-state.readthedocs.io/)** — built from `docs/` via MkDocs ([`mkdocs.yml`](mkdocs.yml)); `uvx --with-requirements docs/requirements.txt mkdocs serve` previews it locally.

- [`docs/architecture.md`](docs/architecture.md) — layered architecture, lifecycles, audit guarantee
- [`docs/design.md`](docs/design.md) — design rationale and trade-offs
- [`docs/how-to/getting-started.md`](docs/how-to/getting-started.md) — end-to-end first-project walkthrough
- [`docs/how-to/using-anvil-on-any-harness.md`](docs/how-to/using-anvil-on-any-harness.md) — wire Anvil's MCP server into Cursor / Windsurf / Cline / VS Code / Zed / Codex (one command), or drive it from any shell via the CLI. See also [`AGENTS.md`](AGENTS.md).
- [`docs/cli-reference.md`](docs/cli-reference.md) — every CLI command, flag, and exit code
- [`docs/roadmap.md`](docs/roadmap.md) — Phase 11 plans, v2.0 and beyond backlog
- [`docs/mcp.md`](docs/mcp.md) — 24-tool MCP reference with error envelope contract
- [`docs/prd-template.md`](docs/prd-template.md) — PRD authoring schema and worked example
- [`docs/github-sync.md`](docs/github-sync.md) — bidirectional GitHub Issues sync reference
- [`docs/sync-providers.md`](docs/sync-providers.md) — contributor guide for adding Linear, Monday, Jira providers
- [`docs/llm.md`](docs/llm.md) — `--use-llm` augmentation, prompt caching, `RecordedLLMProvider` test pattern
- [`CHANGELOG.md`](CHANGELOG.md) — release history

---

## Install

### As a Claude Code plugin (recommended)

```bash
/plugin marketplace add fakoli/anvil
/plugin install anvil@anvil
```

Installs the plugin, registers the five hooks, wires the MCP server, and makes the five agents discoverable to Claude Code at next session start.

### Standalone via `uv tool` (any harness)

Install the published package — it provides the `anvil` CLI and the `anvil-mcp` MCP server, no checkout needed (only [uv](https://docs.astral.sh/uv/)):

```bash
uv tool install anvil-state        # or: pipx install anvil-state
anvil install <harness>            # wire anvil into Codex, Cursor, VS Code, ...
```

Or wire a harness in one line (installs `anvil-state` via `uv tool`, then runs `anvil install`):

```bash
curl -fsSL https://raw.githubusercontent.com/fakoli/anvil/main/scripts/install.sh | sh -s -- <harness>
```

For MCP clients without an in-place writer, `anvil mcp-config <client>` prints the paste-ready block — see [`docs/how-to/using-anvil-on-any-harness.md`](docs/how-to/using-anvil-on-any-harness.md).

### From source (development)

The engine, CLI, and MCP server are self-contained under `bin/`:

```bash
git clone https://github.com/fakoli/anvil.git
cd anvil/bin
uv sync                     # materializes .venv and resolves deps
uv run anvil --help         # drive the CLI directly
```

---

## Agents shipped with this plugin

| Agent | Color | Owns |
|---|---|---|
| `planner` | white | PRD-to-tasks transformation, feature/task drafting, expand routing |
| `critic` | magenta | Code-review verdict on submitted-evidence diffs vs task acceptance criteria |
| `sentinel` | gray | Verification-command + evidence-completeness scorecard |
| `state-keeper` | teal | Sync drift detection + reconciliation triage across SQLite / FS / git |
| `docs-scribe` | purple | Plugin `docs/` cross-references, `CHANGELOG.md`, `plugin.json.description` |

The Iron Rule (review agents never `Edit`/`Write`) is enforced at the `tools:` frontmatter level for `critic` and `sentinel`; `planner` proposes-but-does-not-mutate; `docs-scribe` and `state-keeper` may write only the artifacts they own (docs/CHANGELOG/`plugin.json.description` and sync-report files respectively), never source or state files.

---

## Status

Anvil is in beta (v0.3.1). The full PRD → plan → claim → execute → verify → finish loop works today, plus GitHub Issues sync and multi-provider LLM support. Known gaps and hardening are tracked in [`docs/phase-11-backlog.md`](docs/phase-11-backlog.md); the near-term focus is correctness for claim races, evidence gates, and replay before adding more surface. Linear and Monday providers, webhook sync, and immediate-apply conflict resolution are tracked in [`docs/roadmap.md`](docs/roadmap.md).

---

## Requirements

**Required**

- Python 3.11+ with `uv` (resolved on first invocation — no manual install). This alone runs the full standalone CLI/MCP loop.

**Optional**

- Claude Code with plugin support — to run anvil as a plugin rather than a bare CLI / MCP server.

---

## Author

Sekou Doumbouya — [github.com/fakoli](https://github.com/fakoli)

## License

MIT — see [LICENSE](LICENSE)
