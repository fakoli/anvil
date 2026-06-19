# Codex adapter — advance ONE Anvil task per scheduled fire

A Codex automation is a scheduled natural-language prompt that runs **one agent
per fire** (no author-controlled fan-out). That matches Anvil's other mode
exactly: **one governed task per invocation**. Each scheduled run advances a
single task; the next fire resumes from Anvil's durable state — no in-prompt loop
needed, because the queue itself is the cursor.

## The seam

`anvil next -q` decides whether this fire has work:

| exit | meaning | this fire |
|---|---|---|
| `0` | a task is ready | run the body for it |
| `3` | queue is empty | stop quietly (an empty run, nothing to report) |
| other | real error | report it to Triage |

## Configure the automation

In the Codex app, create a scheduled automation (cron — e.g. `0 9 * * 1-5`) and
paste the prompt below. The automation must run where `anvil` is on PATH and
`.anvil/` exists (your project checkout or a worktree). Codex's approval mode
gates the write step (`apply`); set it to allow writes if the automation should
ship, or leave it report-only to stop at `submit`.

## The prompt (one governed task per fire)

```
Advance exactly ONE Anvil task, then stop. Do not loop.

1. Run `anvil next -q`.
   - exit 3: the queue is empty. Stop. Report "no ready tasks" — nothing to do.
   - non-zero (not 3): report the error to Triage and stop.
   - exit 0: continue.
2. Read the recommended task id from `anvil next --json`.
3. `anvil claim <task>` — acquire the single-winner lease + file-conflict check.
   If the claim fails (another runner won it), stop; the next fire retries.
4. `anvil packet <task>` — read the work packet in full. It is the contract:
   acceptance criteria, files in scope, verification commands.
5. Implement against the acceptance criteria on the claim's branch. Run every
   verification command in the packet; do not proceed if any fails.
6. `anvil submit <task> --commands "<commands run>" --files-changed "<paths>"`
   — the evidence is the typed proof; this auto-releases the claim and moves the
   task to needs_review.
7. `anvil apply <task> --approve --strict` — the gate. --strict refuses approval
   when required evidence is missing. (Omit this step if the automation is
   report-only; a human runs apply from the evidence.)
8. Stop. Report the task id and outcome. The NEXT scheduled fire picks up the
   next ready task from durable state.
```

## Why one-per-invocation here

A scheduled automation fires on a cadence, not in a tight loop — so it does the
body once and exits. Anvil's durable, leased state is what makes that resumable:
the queue advances by one each fire, leases prevent two fires (or a fire + a CI
drainer) from claiming the same task, and evidence gating means a fire can't fake
"done". The drain loop and this single-fire form are the **same primitive** run
with different cadences.

## See also

- `packaging/loops/ci-drain.sh` — drain mode (the same body in a `while` loop).
- `packaging/loops/claude-loop.md` — drain mode driven by Claude Code `/loop`.
- `docs/how-to/drive-the-anvil-loop.md` — PRD → ready queue → both modes.
- `packaging/codex/` — installing the anvil MCP server / `AGENTS.md` for Codex.
