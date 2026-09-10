# anvil-pi — anvil for the pi coding agent (M2)

Pi package that wires the anvil state layer into pi sessions: coarse
`anvil_*` tools over the `anvil --json` CLI, `/anvil:*` commands, and a
bounded session-start snapshot. AGENTS.md needs no splice — pi loads it
natively.

## Install (M3 wires the writer; manual today)

Add to pi settings (`~/.pi/agent/settings.json` or project `.pi/settings.json`):

```json
{ "packages": ["git:github.com/fakoli/anvil@<pinned>"] }
```

The package manifest (`package.json`) declares `pi.extensions: ["./extension.ts"]`.
Recommended pin: an exact tag; M3's `anvil install pi` writes this for you.

## Tools (coarse, not a 1:1 MCP mirror)

| Tool | CLI mapping | Notes |
|---|---|---|
| `anvil_status` | `anvil status --json` | read-only snapshot |
| `anvil_next` | `anvil next --json` | next actionable packet |
| `anvil_claim` | `anvil claim <id> --json` | optional `--actor`, `--lease`, `--force` |
| `anvil_packet` | `anvil packet <id> --json` | full work packet |
| `anvil_submit` | `anvil submit <id> --json` | `--commands` / `--files-changed` passthrough |
| `anvil_apply` | `anvil apply <id> --json` | evidence-gated accept |
| `anvil_run` | `anvil <verb> --json` | escape hatch, verb-allowlisted (below) |

- Successful output is truncated via pi's `truncateHead` at 12 KB; failure
  stderr is surfaced after the `anvil error: ` prefix but capped at 2,000
  chars — every branch is bounded.
- `anvil_run` args are capped (≤32 elements, ≤200 chars each, ≤4k total) and
  options are passed as separate elements (`["--actor", "alice"]`). Args
  containing `--json` or a bare `--` separator are rejected so the appended
  flag cannot be displaced. Structured wrappers accept longer payloads
  (≤2,000 chars/arg).
- Task IDs that could be reinterpreted as CLI flags (leading `-`, whitespace)
  are rejected before spawn.

## Verb policy for `anvil_run`

- **Execution verbs** (always allowed; registered Typer names): `status next
  claim release renew packet submit apply describe doctor gate-check progress
  graph conflicts scan drift claim-guard merge-check`.
- **Planning verbs** (require `ANVIL_PI_PLANNING=1`, mirroring the MCP surface
  gate in `bin/src/anvil/mcp_server.py`): `plan prd init score review
  assumptions deps expand list show bundle`.
- **Always denied** (operator actions, no env flag reaches them): `install
  mcp-config hook restore migrate migrate-workspace migrate-events replay
  run-workflow backup`.
- **Not yet classified** (fail closed until a side-effect audit): `sync proof
  project notify-digest`.

## Commands

`/anvil:status`, `/anvil:next`, `/anvil:claim <id>`, `/anvil:submit <id>
[--commands <cmds>] [--files-changed <files>]` — thin UI over the same tools;
silent in print/JSON modes.

## Session snapshot

When an anvil state root exists (`ANVIL_ROOT` or `.anvil/` in the workspace),
the first successful agent start of a session injects a bounded status
snapshot (≤6,000 chars total, ≈1.5k tokens) as an LLM-visible message; the
session start also notifies the operator. The flag resets on `session_start`
(new sessions get their own snapshot) and is NOT consumed by no-state or
failed attempts. Silent modes (print/JSON) neither inject nor run the status
call. Zero cost outside anvil projects.

## Configuration

- `ANVIL_BIN` — anvil executable override (default: `anvil` on PATH).
- `ANVIL_ROOT` — project root override (else cwd-based detection).
- `ANVIL_PI_PLANNING` — truthy (1/true/yes/on) exposes planning verbs on
  `anvil_run`.

## Honest notes

- All CLI calls are async and abort-aware: cancelling a tool call terminates
  the anvil subprocess; nothing blocks pi's event loop.
- `--json` is appended by the extension; the CLI decides the response format
  per command. Snapshot status runs with a 15s timeout, tool calls with 120s.
- Verb classification is derived from `bin/src/anvil/cli/__init__.py`
  registrations and side-effect audits of drift/claim-guard/merge-check
  (read-only). `sync`, `proof`, `project`, and `notify-digest` are deliberately
  unclassified pending audit.

## Sandbox (M4)

The unattended sandbox profile pins this extension entry point by sha256 via
`packaging/pi/sandbox/allowlist.json`; the launcher stages verified bytes and
loads only those (see `packaging/pi/sandbox/README.md`). In sandboxed runs,
`anvil` must be reachable via `ANVIL_BIN` or the image's PATH.

## Tests

`node tests/anvil_pi/extension.test.mjs` — 33 hermetic cases against a
recording fake `anvil` on PATH (jiti loads the TS from the pi harness
install; `PI_INSTALL_DIR` overrides that path), including abort/cancellation,
cwd threading, quote-aware submit tokenization, and — when a real `anvil` is
installed — a registry-contract check that every allowlisted/denied verb name
exists in `anvil --help`. The pytest wrapper
(`tests/test_anvil_pi_extension.py`) runs the driver and skips cleanly when
node or the pi harness install is unavailable.