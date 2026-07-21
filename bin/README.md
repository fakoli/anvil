# anvil-state

Local-first project state engine: turn rough ideas and PRDs into reviewed,
lockable, evidence-backed work packets that humans and AI coding agents can
coordinate on without conflicts.

`anvil-state` installs the `anvil` CLI and the `anvil-mcp` MCP server.

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

Full documentation, the cross-harness install guide, and the Claude Code plugin
live in the repository: https://github.com/fakoli/anvil
