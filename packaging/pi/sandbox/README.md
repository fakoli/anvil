# pi sandbox — unattended execution allowlist + launcher (M1 rework)

Fail-closed machinery for running pi children unattended (background
orchestration, fresh-context reviewers, sandboxed exec). A run either starts
with verified, staged bytes and a sanitized environment — or it does not
start. Designed against an adversarial review of the original sh/eval
launcher; see the rework notes at the bottom.

## Components

| File | Role |
|---|---|
| `allowlist.json` | Named profiles: hash-pinned `path:` extensions, strict tool allowlist, network posture |
| `allowlist.schema.json` | The contract `allowlist.json` must satisfy (mirrored by the policy validator) |
| `scripts/pi-sandbox-policy.mjs` | `validate` / `compose` + exported staging/env libraries — stdlib node only, zero subprocesses during verification |
| `scripts/pi-sandbox-launch.mjs` | The launcher: structured argv, staging, stdin task transport, sanitized env, spawn |
| `scripts/pi-sandbox-run.sh` | Thin shim: strips injection vectors (`NODE_OPTIONS`, `LD_PRELOAD`, …) **before** node starts, then execs the launcher |

## Enforced guarantees

1. **Single policy snapshot** — one read, one parse; every stage uses the same
   document.
2. **What is verified is what loads** — `path:` pins are read once, hashed,
   compared (exit 3 on mismatch, 2 on missing), and **staged** (entry +
   same-directory siblings, symlinks rejected); pi is pointed at the staged
   copies. Relative pins resolve against the allowlist's directory — never the
   cwd — closing the verify-here/load-there bypass. The staged directory cap
   is 25 MB.
3. **Task text is data** — it travels as stdin JSONL, never on argv, so
   option-like (`--approve`) or attachment-like (`@/etc/passwd`) task text
   cannot become pi flags.
4. **Strict argv** — `--no-extensions` (discovery collapses to explicit `-e`
   staged paths), `--tools <allowlist>`, `--no-skills`, `-na` (ignore
   project-local settings), `--mode json`. No duplicate executable token.
5. **Fresh agent dir** — `PI_CODING_AGENT_DIR` points at a per-run temp
   directory: user/project extensions, skills, settings, and OAuth identities
   cannot load. **Auth consequence:** sandboxed children authenticate with
   env-var API keys only (e.g. `ANTHROPIC_API_KEY`, fleet `ANVIL_*` vars);
   OAuth logins are intentionally out of reach.
6. **Sanitized env, early** — the shim unsets `NODE_OPTIONS`, `LD_PRELOAD`,
   `LD_LIBRARY_PATH`, `LD_AUDIT`, `DYLD_*`, `BASH_ENV`, `ENV`, `PYTHONSTARTUP`,
   `PYTHONPATH`, `ZDOTDIR`, `RUBYOPT` before node starts; the launcher
   additionally scrubs `NODE_*`/`npm_config_*` from the child env.
7. **No ambient executables in the trust path** — verification is pure fs
   hashing (no npm/git subprocesses); the pi binary is resolved once to an
   absolute realpath before anything else happens.
8. **Cleanup** — staging and agent dirs are removed on every exit path,
   including staging failures; dry runs create no directories.

Exit codes: `0` ok · pi's exit code forwarded · `2` policy/usage/staging ·
`3` pin mismatch.

## M4: tree pins, image, dogfood

- **Tree pins** — `{"source": "path:<rel>/entry.ts", "tree_sha256": "<64hex>"}`:
  canonical digest over the entry's whole directory (sorted relpaths,
  length-prefixed per-file sha256 records; version 2 encoding). Symlinks, special files, and `.git` /
  `node_modules` directories are REJECTED, never skipped. Refresh after
  changing the pinned source: `node scripts/pi-sandbox-policy.mjs tree-hash
  packaging/pi/anvil-pi` (CI fails on drift via build-time + runtime verify).
- **Flat `sha256` pins** still work for single-file extensions (entry verified;
  siblings staged unverified; byte-counted caps).
- **Image** (`packaging/pi/sandbox/Dockerfile`): bakes pi@0.85.1, the anvil
  CLI (uv frozen), the sandbox scripts, the allowlist, and the pinned
  anvil-pi tree; the build FAILS on pin drift
  (`verify-baked-pins.mjs`). Entrypoint re-validates + re-stages from baked
  bytes on every start, seeds `models.json` for the loopback mock provider,
  and starts `mock-llm.mjs`.
- **Docker run** (`scripts/pi-sandbox-docker.sh [--build] <profile>
  <task-file> <workspace>`): `--network none`, read-only rootfs, tmpfs
  scratch, `--cap-drop ALL`, `no-new-privileges`, pids/nofile limits,
  workspace the only writable mount, task file read-only. `ANVIL_SANDBOX_IMAGE`
  selects the image (use a digest for reproducibility).
- **Dogfood** (CI job `pi-sandbox`): a fresh scratch anvil root + git repo;
  the container drives status → next → claim → packet → submit unattended
  (deterministic mock provider — no live model, no network). Assertions run
  OUTSIDE the agent: packet on disk, report with evidence, task in
  needs_review, and `done == 0` (apply is the HUMAN gate — the agent must
  not self-approve).

## Usage## Usage

```sh
# inspect without launching
scripts/pi-sandbox-run.sh --profile unattended-exec --workspace . \
  --task-file task.txt --dry-run

# launch (host, fresh agent dir, strict argv, staged extensions)
scripts/pi-sandbox-run.sh --profile unattended-exec --workspace . \
  --task-file task.txt
```

## Honest boundary

No sandbox is a security boundary against a malicious model with `bash`. The
allowlist constrains **which code loads**; Docker (M4) confines blast radius.
For review children use `read-only-review` (no write tools) and consider
read-only workspace mounts at the container layer.