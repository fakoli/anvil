# Anvil on OpenCode

OpenCode reads MCP servers from the `mcp` block of `opencode.json` (project root,
or `~/.config/opencode/opencode.json` for global) and reads `AGENTS.md` natively.

## Quick install (recommended)

```bash
anvil install opencode --write
```

This merges the `anvil` server into your `opencode.json` with the **real**
absolute path to `bin/anvil-mcp`, and drops `AGENTS.md` at your project root.
Drop `--write` for a dry-run that prints exactly what it would do.

## Manual

Copy [`opencode.json`](opencode.json) and replace `/path/to/anvil/bin/anvil-mcp`
with the absolute path to this checkout's `bin/anvil-mcp`. OpenCode's entry shape
differs from most clients:

- `command` is a single **argv array** (`["bash", "<path>"]`), not a
  `command` + `args` split.
- environment variables go under `environment` (not `env`) — e.g. to pin a
  project root in a multi-repo setup:

  ```json
  "anvil": {
    "type": "local",
    "command": ["bash", "/path/to/anvil/bin/anvil-mcp"],
    "enabled": true,
    "environment": { "ANVIL_ROOT": "/path/to/your/project" }
  }
  ```

Run `anvil mcp-config opencode` to print the block for this checkout, or
`anvil install opencode --uv-run --write` to use `uv run` instead of `bash`.
