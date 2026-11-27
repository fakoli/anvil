# anvil-state

The system of record for agent teams: a local-first ledger that turns PRDs into
reviewed, lockable, evidence-backed work packets, so AI coding agents and the
people working with them can execute one plan without overwriting each other.

`anvil-state` installs the `anvil` CLI, the `anvil-mcp` MCP server, and the
`anvil-web` board, which is read-only unless you start it with write access.

- **A ledger outside the harness.** Requirements, tasks, claims, and evidence
  live in an append-only event log and a SQLite projection on your machine, so
  Claude Code, Codex, Cursor, and a shell loop all work from the same state.
- **Single-winner claims.** A claim is a lease, not a label. The first writer
  wins and a second claimant is refused.
- **State that replays.** `anvil replay` rebuilds the database from the event
  log, and `anvil doctor` checks that the log still reproduces the live state.
- **Signed acceptance proofs.** Approving submitted evidence writes a signed
  receipt you can check with `anvil proof verify`. The evidence gate is advisory
  by default; `--strict` makes approval refuse until the required checks are on
  record.

Anvil is in beta. The
[changelog](https://github.com/fakoli/anvil/blob/main/CHANGELOG.md) lists what
each release adds.

Runtime compatibility: the published package requires Python 3.11+,
Pydantic 2.11.7+, and FastMCP 3.x (3.0.0 or newer). These are the lowest
versions that both resolve and provide Anvil's MCP registration and
surface-gating APIs; the next FastMCP major remains opt-in until qualified.

The immutable version-1 provider-read ceilings and exact limit-refusal shape
are published in the packaged `_data/provider-read-contract-v1.json` fixture;
the runtime pins its canonical-JSON SHA-256 so line-ending conversion cannot
mask or invent drift from the validated `anvil.read_contracts` models.

```bash
uv tool install anvil-state      # or: pipx install anvil-state
anvil --help
anvil install <harness>          # wire anvil into Codex, Cursor, VS Code, ...
```

The MCP server (`anvil-mcp`) exposes anvil's tool surface to any MCP-capable
harness. `anvil install <harness>` writes the MCP config and instruction file for
a target harness (idempotent, backed up, reversible with `--rollback`).

Documentation, including a replay of a recorded session and the cross-harness
install guide: https://fakoli.github.io/anvil/

Source and the Claude Code plugin: https://github.com/fakoli/anvil
