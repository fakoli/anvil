# Anvil

![Anvil — system of record for agent teams](https://raw.githubusercontent.com/fakoli/anvil/main/assets/banner.png)

**The system of record for agent teams** — a local-first, backend-neutral
project-state layer for humans and AI coding agents. Anvil records
requirements, tasks, claims, evidence, and reviews in SQLite plus an
append-only event log, and exposes that state through a CLI (`anvil`) and an
MCP server (`anvil-mcp`) that any harness can drive.

```bash
uv tool install anvil-state      # or: pipx install anvil-state
anvil init --with-sample         # seed a runnable sample project
anvil next                       # → a ready task, immediately
```

## Start here

- **[Getting started](how-to/getting-started.md)** — first project,
  end-to-end: init → PRD → plan → claim → evidence → done.
- **[Where state lives](how-to/state-location.md)** — the HOME-workspace
  default, `ANVIL_STATE_LAYOUT=local`, and `ANVIL_ROOT`.
- **[Authoring a PRD](how-to/authoring-a-prd.md)** and the
  **[PRD template](prd-template.md)** — the input format, including the
  strict `RNNN` requirement-ID rule.
- **[Using anvil on any harness](how-to/using-anvil-on-any-harness.md)** —
  wire the MCP server into Claude Code, Codex, Cursor, VS Code, Zed, and
  more with one command.

## The core ideas

- **Claims are enforced, not conventional.** A claim is a database row with
  a lease and heartbeat; single-winner coordination spans sessions, loops,
  and machines. Stale leases are reaped on every operation.
- **Status is downstream of proof.** Completion is evidence-gated: agents
  submit structured evidence (typed command/diff/link proofs), reviews gate
  acceptance, and every accepted task mints a signed, portable
  `AcceptanceProof` verifiable off-host with `anvil proof verify`.
- **Everything replays.** Every mutation appends to `events.jsonl`;
  replaying the log reconstructs the database — the audit guarantee is
  tested in CI on every PR.

## Reference

The [CLI reference](cli-reference.md) covers every command and exit code;
the [MCP reference](mcp.md) documents all 24 tools (14 on the wire by
default — see its tool-surface gating section). The
[architecture guide](architecture.md) maps the layers; the
[design doc](design.md) records the trade-offs.

## Project

Planning lives in the [roadmap](roadmap.md), the
[prioritized backlog](backlog/anvil-backlog.md), and the
[strategic backlog](backlog/strategic-backlog.md); the
[production-readiness plan](plans/2026-07-02-production-readiness.md)
sequences current work. Release history is in the repository's
[CHANGELOG](https://github.com/fakoli/anvil/blob/main/CHANGELOG.md).
