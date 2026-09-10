# Plan of Attack — pi Extension Support + Unattended Sandbox Allowlist (2026-09-10)

> Two workstreams, one release surface (`packaging/pi/`):
>
> 1. **pi extension support** — make pi a first-class anvil harness.
> 2. **Extension allowlist for unattended execution** — a fail-closed policy +
>    launcher for pi children running headless inside a Docker sandbox.
>
> Grounding verified 2026-09-10 against pi `@earendil-works/pi-coding-agent`
> (docs + `dist/core/resource-loader.js`) and this repo's
> `docs/how-to/using-anvil-on-any-harness.md`.

## 0. Findings that shape the design

| # | Fact | Consequence |
|---|------|-------------|
| F1 | **pi has no built-in MCP client** (`docs/usage.md`: "intentionally does not include built-in MCP, sub-agents, …"). | Anvil's **MCP-only tier does not exist for pi**. The native surfaces are extensions (TS: `registerTool`/`registerCommand`/events), skills, packages, and context files. |
| F2 | pi loads `AGENTS.md` natively as context. | No AGENTS.md splice needed — pi can be a **"supported end-to-end"** harness with *less* config-writer risk than codex. |
| F3 | `pi --no-extensions` (`-ne`) **disables all discovery** (global dir, project dir, settings `packages`/`extensions`); with `-ne`, ONLY explicit `-e` paths load (verified in `resource-loader.js`: `noExtensions ? cliEnabledExtensions : …`). `-e` also accepts `npm:` / `git:` specs. | A **strict extension allowlist already exists at the CLI level today**. No upstream pi feature is required for workstream 2 — it is a policy artifact + launcher + verification problem. |
| F4 | `--tools` is a strict allowlist for **all** tools (built-in + extension custom tools); `--no-skills`, `--no-prompt-templates` exist. Non-interactive modes never prompt for project trust; `defaultProjectTrust: "never"` / `--no-approve` ignore project `.pi/` resources. | The full fail-closed recipe for an unattended child is composeable from existing flags. |
| F5 | Anvil harness pattern: `anvil install <harness>` (dry-run default, `--write` idempotent) + committed reference under `packaging/<harness>/`; MCP-only tier merges `anvil-mcp` into harness config where possible. Two tiers today: supported end-to-end (claude-code, codex, openclaw) / MCP-only (12 others). | pi becomes a **third flavor of the supported tier**: package-install (npm/git pin) instead of MCP config merge. |
| F6 | Machine surface: `anvil <cmd> --json` (CLI) and `anvil-mcp` stdio server (24 execution tools; 36 with `ANVIL_MCP_PLANNING=1`). | The extension should shell out to the **CLI** (`--json`) — no MCP client, no bundled MCP SDK, matches the documented machine surface. |
| F7 | pi packages support **versioned npm pins and pinned git refs**, and per-package resource filtering (`"extensions": [...]`, `"skills": []`) in settings. | Supply-chain posture: ship as `npm:@fakoli/anvil-pi@<pin>` or `git:github.com/fakoli/anvil@<tag>` + path filter — same pattern as the vendored hermes-memory fork. |
| F8 | Context frugality is a tracked benchmark (`benchmarks/CONTEXT_AUDIT.md`, ~2.4k always-on tokens). | Extension must inject **bounded** session-start context and use coarse tools, not 24 schema-heavy tool registrations. |

---

## 1. Workstream A — pi extension support in anvil

### Target tier

**Supported end-to-end**, third flavor: *"pi native package"*. `anvil install pi --write`
writes a `packages` entry (pinned) into pi settings; everything else ships inside
the pi package itself (extension tools + skills + `/anvil:*` commands). AGENTS.md
needs no splice (F2).

### A1. The pi package — `packaging/pi/anvil-pi/`

Structure:

```
packaging/pi/anvil-pi/
├── package.json          # pi manifest: {"pi": {"extensions": [...], "skills": [...]}}
├── extension.ts          # the extension (single file, no deps)
├── tools.ts              # tool defs (thin wrappers over `anvil --json`)
└── README.md
```

Extension behavior:

1. **Tools (coarse, not a 1:1 MCP mirror — F8):**
   - `anvil_status` — project + task + claim snapshot (read-only).
   - `anvil_claim` — claim a task by ID (leases/heartbeats do the coordination).
   - `anvil_packet` — fetch the work packet for a claimed task.
   - `anvil_submit` — submit evidence (`--commands`, `--files-changed` passthrough).
   - `anvil_apply` — apply/accept an evidence-gated task.
   - `anvil_run` — escape hatch: any `anvil <verb> --json`, **verb-allowlisted**
     (deny-list planning verbs unless `ANVIL_PI_PLANNING=1`, mirroring the MCP
     planning gate).
   - All execute `anvil` as a subprocess, parse `--json`, use `truncateHead` for
     large payloads; fail with the CLI's stderr verbatim.
2. **Commands:** `/anvil:status`, `/anvil:claim <id>`, `/anvil:next`, `/anvil:submit <id>`
   (thin UI over the same tools; `ctx.hasUI` guards per `docs/extensions.md` mode table).
3. **`session_start` hook:** if an anvil state root exists (`ANVIL_ROOT` or default),
   inject a **bounded** (≤ ~1.5k tokens) snapshot: active claims w/ leases, next
   actionable packet, gate/PRD status. Nothing injected when no state → zero cost
   outside anvil projects.
4. **Evidence capture (stretch, A-late):** `tool_call` post-event on `edit`/`write`
   to feed the same evidence-buffer the Claude hooks use (`hooks/capture-evidence.sh`
   analog, in-process, opt-in flag).

Skills: anvil's existing harness-neutral `skills/` (claim, execute, finish, plan,
prd, start-prd, state-ops) are declared by the package manifest so pi installs them
via the package (no skills *drop* into user dirs — blast-radius rule).

### A2. `anvil install pi` writer

- Dry-run default (identical trailer contract as other harnesses); `--write` idempotent.
- Writes, in precedence order `--root`/`--global`:
  - **project** `.pi/settings.json`: `packages: [{"source": "npm:@fakoli/anvil-pi@<pinned>", "skills": [...]}]`
  - or **user** `~/.pi/agent/settings.json` same entry.
- Merge rules: never touch unrelated keys; if an `anvil-pi` entry exists, update pin only.
- Refuses to write if pi settings schema unrecognized (fail closed, like the
  config-corruption postmortem policy).
- `anvil install --help` + `scripts/install.sh` harness list + `docs/how-to/using-anvil-on-any-harness.md`
  table row:
  `| pi | supported (package) | writes pinned `npm:@fakoli/anvil-pi` into pi settings; AGENTS.md is native — no splice |`

### A3. Tests + CI

- Writer tests: dry-run trailer, idempotent re-run, pin update, unrelated-key preservation.
- Extension unit tests: tool→CLI mapping against `--json` contract fixtures;
  verb allowlist; planning-gate behavior; truncation.
- Smoke (CI job, Node 24 pin per repo convention):
  `pi --no-extensions -e packaging/pi/anvil-pi/extension.ts --mode json -p "anvil status"`
  against a seeded temp anvil state.

### A4. Open decisions (decide before M3)

| Question | Recommendation |
|---|---|
| npm publish `@fakoli/anvil-pi` vs git-pin into this repo | **Git-pin first** (`git:github.com/fakoli/anvil@<tag>` + path filter to `packaging/pi/anvil-pi`), publish to npm later — avoids a release pipeline for v1. |
| Extension execs CLI vs speaks MCP to `anvil-mcp` | CLI for v1 (F6); revisit MCP-in-extension if tool-schema fidelity ever matters. |
| Tool naming: `anvil_*` prefix vs bare MCP names | `anvil_*` prefix — avoids collisions with other extensions' tools and reads plainly in transcripts. |

---

## 2. Workstream B — extension allowlist for unattended Docker-sandbox execution

### Threat model

An unattended pi child (no human to confirm anything) inside a Docker sandbox, on a
workspace that may be untrusted. Must guarantee:

- No project `.pi/` resources load (trust fail-closed).
- No ambient global extensions, settings-declared packages, skills, or prompt
  templates load — **only the allowlist**.
- Only allowlisted tools are enabled.
- Host credentials/auth never enter the container.
- If the allowlist can't be satisfied, **nothing runs** (fail closed).

### The recipe (all existing pi flags — F3/F4)

```
pi --mode json --no-extensions \
   -e <allowlisted-ext-1> -e <allowlisted-ext-2> … \
   --tools <tool-allowlist> \
   --no-skills --no-prompt-templates \
   --no-approve \
   -p "<task>"
```

inside a container whose `~/.pi/agent` is a **container-local volume** (never a host
mount) and whose baked settings set `defaultProjectTrust: "never"`.

### B1. Policy artifact — `packaging/pi/sandbox/allowlist.json`

Named profiles; `unattended-exec` is the first:

```jsonc
{
  "profiles": {
    "unattended-exec": {
      "extensions": [
        // each entry: source (npm pin or git pin or in-image path), version pin, sha256
        {"source": "npm:@fakoli/anvil-pi@0.6.5", "sha256": "…"},
        {"source": "path:/opt/anvil/extensions/sandbox-guard.ts", "sha256": "…"}
      ],
      "tools": ["read", "grep", "find", "ls", "bash", "edit", "write",
                "anvil_status", "anvil_claim", "anvil_packet", "anvil_submit", "anvil_apply", "anvil_run"],
      "skills": [],
      "network": "none"          // or "inference" (egress to provider endpoints only)
    }
  }
}
```

Hashes pin *content*, not just version — the launcher verifies before launch.

### B2. Sandbox image — `packaging/pi/sandbox/Dockerfile`

Based on pi's documented plain-Docker pattern (`docs/containerization.md`):

- `node:24-bookworm-slim` + `pi` (global, pinned version) + `anvil-state` (pinned).
- Allowlisted packages **pre-installed and pinned** inside the image; baked
  container-local `~/.pi/agent/settings.json` with `defaultProjectTrust: "never"`.
- Entrypoint takes the composed pi argv (the launcher owns the flags, not the image).

### B3. Launcher — `scripts/pi-sandbox-run.sh`

```
pi-sandbox-run.sh --profile unattended-exec --task-file task.md [--timeout 1200]
```

Behavior:

1. Load profile; **verify every extension entry resolves and hash-matches**; refuse
   to start otherwise (fail closed — no container, no partial run).
2. Compose docker argv: `--network none|inference`, workspace bind-mount
   (`:rw` by default, `--read-only-workspace` option), container-local agent volume,
   only the env the task needs (no host auth).
3. Compose the pi argv from the recipe above (F3/F4), task from `--task-file`.
4. **Post-run verification:** parse the `--mode json` stream / session file for the
   actually-loaded extension set; any deviation from the profile marks the run
   **failed even if task output exists**. Emit a compact attestation line
   (profile, image digest, extension hashes, exit, verification verdict).
5. Exit non-zero on verification mismatch (caller must not ingest output blindly).

### B4. Subagent wiring

- Unattended sandbox children are launched via **`bg_run` (shell) or the attested
  `pi --mode json` background path, with the launcher as the command** — not via
  ambient extension discovery. `extensionMode` stays `isolated` (ambient executes
  arbitrary discovered extension code and is forbidden for unattended children).
- Document the boundary: pi-subagents children spawned by the `subagent()` tool run
  on the host trust context; the sandbox launcher is the only sanctioned route for
  untrusted-workspace unattended execution.
- Integration point with Workstream A: the allowlisted extension inside the sandbox
  **is** `anvil-pi` — the sandboxed agent drives the anvil loop through the same
  coarse tools, and its claims/evidence land in the same anvil state as any other
  harness. `anvil_run`'s verb allowlist doubles as the sandbox command allowlist.

### B5. Tests

- Resolver unit tests: pin/hash verification, missing-entry refusal, profile schema.
- Fail-closed integration: profile with a bad hash → no container launched, non-zero exit.
- Sandbox integration (CI, Docker-in-Docker or runner Docker): build image, run a
  seeded anvil state + `unattended-exec` profile, assert task completes, loaded-set
  verification passes, and a *deliberately poisoned* project `.pi/extensions/` file
  in the workspace does **not** load.

### B6. Optional upstream (pi-mono issue — non-blocking)

- Named allowlist surface: `--extensions-allowlist <file>` or `PI_EXTENSION_ALLOWLIST`
  (dedupe/verify inside pi instead of the launcher).
- A `session_start`-adjacent event exposing the final loaded-extension set in
  `--mode json` for cheap attestation (today: parse startup header/session file).
- File both as upstream feature requests; the launcher works without them.

---

## 3. Milestones & sequencing

| M | Scope | Why this order |
|---|-------|----------------|
| **M1** | B1 allowlist policy + B3 launcher + fail-closed tests | Standalone; works with **stock pi today** (F3). Unblocks unattended exec regardless of A. |
| **M2** | A1 pi package (extension + skills + commands) | Needed by both A's install writer and B's sandbox allowlist content. |
| **M3** | A2 `anvil install pi` writer + A3 tests + docs table | Depends on a published/pinnable M2 artifact. |
| **M4** | B2 sandbox image + B5 integration tests + B4 wiring docs; dogfood in anvil's own unattended loops | Full loop: orchestrator → sandbox child → anvil state, all evidence-gated. |

Each milestone = one PR (draft PR on open, per working style), with an
adversarial fresh-context review pass on the launcher (security-adjacent code).

## 4. Explicitly out of reach / risks

- **No sandbox is a security boundary against a malicious model with `bash`.**
  Docker confines blast radius (filesystem, network); prompt injection remains an
  accepted local-agent risk (pi `docs/security.md`). The allowlist constrains
  *code that loads*, not *what the model asks bash to do* — keep `--tools` tight
  for read-only profiles and consider read-only workspace mounts for review children.
- Sandbox CI requires a Docker-capable runner; without one, B5 integration tests
  stay locally-gated and CI runs the unit slice only (matches the repo's
  live-tests vs contract-test split).
- `anvil install pi` writing to user settings (`~/.pi/agent/settings.json`) is a
  cross-harness first (others write harness-owned configs); keep it strictly
  opt-in via `--global`, default to project settings.