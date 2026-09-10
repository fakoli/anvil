# pi sandbox — unattended execution allowlist + launcher (M1)

Fail-closed machinery for running pi children unattended (background
orchestration, fresh-context reviewers, sandboxed exec). The launcher refuses
to start anything whose extension pins do not verify — a run either starts
with the exact pinned code, or it does not start.

## Components

| File | Role |
|---|---|
| `allowlist.json` | Named profiles: pinned extensions, strict tool allowlist, skills, network posture, project-trust posture |
| `allowlist.schema.json` | The contract `allowlist.json` must satisfy (enforced by the policy helper) |
| `scripts/pi-sandbox-policy.mjs` | `validate` / `pin-verify` / `compose` — policy brain, stdlib-only node, no supply-chain surface |
| `scripts/pi-sandbox-run.sh` | Launcher: validate → pin-verify → compose → launch (or `--dry-run`) |

## Profiles shipped today

- **`unattended-exec`** — read/edit/bash plus `--tools` strict list; no
  network (posture; container enforcement in M4). Extension list is empty
  until M2 publishes the hash-pinned anvil-pi package.
- **`read-only-review`** — `read,grep,find,ls` only; for fresh-context review
  children.

## What is enforced today (M1)

1. Structural validation (schema contract) — exit 2 on any violation.
2. Pin verification before launch: `path:`/`npm:` entries need a matching
   sha256 (exit 3 on mismatch, 2 on missing); `git:` entries pin `ref` +
   `commit` and are confirmed via `git ls-remote` (exit 3 if the ref moved).
   Unreachable registry ⇒ abort, exit 4 — never "launch anyway".
3. Strict argv: `--no-extensions` (discovery collapses to explicit `-e` paths),
   `--tools <allowlist>`, `--no-skills`, `-na` (ignore project-local settings),
   `--mode json`.
4. `PI_CODING_AGENT_DIR` points at a fresh per-run temp directory, so user and
   project extensions, skills, and settings cannot load regardless of cwd —
   including a hostile `.pi/` planted in the workspace.

## What lands in M4

- Docker image with the pinned extensions baked in; `--network none` /
  inference-only egress actually enforced by the container runtime.
- Post-run verification of the *actually loaded* extension set (the launcher
  checks what it asked for today; the image build attests what exists).
- npm/git pins in `allowlist.json` grow real entries as packages publish.

## Usage

```sh
# inspect without launching
scripts/pi-sandbox-run.sh --profile unattended-exec --workspace . \
  --task-file task.txt --dry-run

# launch (M1: on host, fresh agent dir, strict argv)
scripts/pi-sandbox-run.sh --profile unattended-exec --workspace . \
  --task-file task.txt
```

Exit codes: `0` ok · `1` pi exited non-zero (forwarded) · `2` policy/usage ·
`3` pin mismatch · `4` verification dependency unavailable.

## Honest boundary

No sandbox is a security boundary against a malicious model with `bash`. The
allowlist constrains **which code loads**; Docker (M4) confines blast radius.
For review children use `read-only-review` (no write tools) and consider
read-only workspace mounts at the container layer.