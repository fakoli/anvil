# anvil — Positioning (internal reference)

> Internal source of truth for README, architecture.md, design.md, and the
> `plugin.json` description. Not part of user-facing docs nav. Reusable
> sentences are marked **(Q)**.

## What it is

**(Q)** anvil is a local-first, backend-neutral project-state layer for
humans and AI coding agents — the durable record of every requirement, task,
claim, and piece of evidence in your project, stored in SQLite under
`.anvil/` and exposed through a CLI and an MCP server.

## Who it is for

Developers running Claude Code, Codex, Cursor, OpenHands, or Copilot who need
multiple agents, and multiple humans, to coordinate against the same plan
without overwriting each other. Solo builders can preserve PRDs across
sessions. Project leads can audit what was claimed, reviewed, and completed.

## The 5 differentiators (vs CCPM, issue-trackers, chat-driven workflows)

1. **Richer canonical state than issue text.** Pydantic v2 models in SQLite, validated at every transition — not free-form markdown in an issue body.
2. **Explicit claim / lock / lease model.** A `Claim` row with an expiry timestamp and heartbeat; stale leases are detected and released on every CLI or MCP call — not assignment-by-label.
3. **LLM-optimized work packets.** `anvil packet T012` renders the exact intent, acceptance criteria, scope, and non-goals an agent needs — not an entire issue thread the agent must summarize.
4. **Six-dimension task scoring.** Complexity, parallelizability, context load, blast radius, review risk, and agent suitability drive routing and expand recommendations — not single-axis story points.
5. **Runtime-neutral CLI + MCP.** The state engine is not coupled to any one agent runtime; the FastMCP stdio server exposes 24 tools to any MCP-compatible client.

## Terraform analogy

**(Q)** anvil is to agentic software work what Terraform is to
infrastructure: a canonical state file holds the truth, derived views (work
packets, markdown plans, dependency graphs) are projected from it, and the
plan-then-apply rhythm gates execution behind review. The PRD is the
configuration; the SQLite database is the state; `anvil apply` is the
commit point that records evidence and transitions a task to `done`. Drift
(stale claims, orphan branches, sync conflicts) is detected and reconciled,
not papered over.

## MCP vs plugin

**(Q)** MCP exposes capabilities; plugins encode operating discipline. The
MCP server ships 24 tools (`claim_task`, `submit_completion_evidence`,
`generate_work_packet`, …) any agent can call, but a tool does not decide *when*
to claim, *which* specialist should execute, *what* evidence is required, or
*how* the critic gate runs. That behaviour lives in skills, subagents, and
hooks — the plugin layer. anvil is plugin-first and MCP-compatible,
not MCP-only.

## Elevator pitch

**(Q)** anvil turns PRDs into reviewed, lockable, evidence-backed work packets
that humans and AI coding agents can execute in parallel without overwriting
each other — the canonical project-state layer for agent teams.

## What it is NOT

- **Not a SaaS.** State lives in your repo under `.anvil/`; no hosted backend, no account, no telemetry.
- **Not a chat memory layer.** Chat history is not a database. State survives session resets, model swaps, and agent runtime changes.
- **Not an issue tracker.** GitHub Issues is an opt-in *sync target* via the bidirectional Phase 8 sync engine — not the source of truth.
- **Not a coding agent.** It is the coordination layer around coding agents: work packets in, evidence out.
