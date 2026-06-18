# Anvil on Cline — STUB (MCP path NOT verified)

This is a deliberate STUB, not a manifest. Cline's instruction file (`AGENTS.md`)
is already delivered by `anvil install cline --write`, but the MCP-config write
is intentionally disabled (the registry row in `bin/src/anvil/cli/install.py`
sets `mcp_merge="none"`) because the on-disk MCP settings path is editor-managed
and varies per OS. We do NOT guess it.

## TODO — verify before enabling `install cline --write` MCP

1. **Confirm the on-disk path of `cline_mcp_settings.json` per OS.** It lives in
   the VS Code (or compatible editor) global-storage directory for the Cline
   extension, which differs across macOS / Linux / Windows and across editor
   forks. Pin the exact path (or the resolution rule) for each supported OS.
2. Once confirmed, flip the `cline` registry row from `mcp_merge="none"` to
   `mcp_merge="json"` and set `mcp_path` / `mcp_scope` accordingly. The JSON
   merge logic and `CLIENTS["cline"]` envelope are already in place — only the
   destination path is missing.

## Primary source to clone and confirm against

- Cline repo: https://github.com/cline/cline

Until the per-OS path is confirmed against that repo, the MCP write stays off and
no path is guessed.
