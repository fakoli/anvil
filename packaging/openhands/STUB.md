# Anvil on OpenHands — STUB (format NOT verified)

This is a deliberate STUB, not a manifest. We do NOT ship a guessed manifest for
OpenHands because the on-disk format has not been confirmed against a primary
source. Replacing this file with real config requires the verification below.

## TODO — verify before writing any manifest

1. **Microagent file location.** Confirm whether OpenHands reads repo microagents
   from `.openhands/microagents/` or from `repo/microagents/` (the two layouts
   have appeared in different OpenHands versions). The instruction-file
   destination depends on this.
2. **MCP config key shape.** Confirm the `config.toml` `[mcp]` key shape (the
   exact table name and the per-server field names) that OpenHands expects for a
   stdio MCP server. Only then can `anvil install openhands --write` enable an
   MCP write (the registry row would move off `mcp_merge="none"`).

## Primary source to clone and confirm against

- OpenHands repo: https://github.com/All-Hands-AI/OpenHands

Until both items are confirmed against that repo, no manifest lands here — a
placeholder that could be mistaken for real config is worse than a STUB.
