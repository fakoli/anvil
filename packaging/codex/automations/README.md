# Anvil automations for Codex

Codex [automations](https://developers.openai.com/codex) are scheduled agent runs
defined by `~/.codex/automations/<id>/automation.toml` (cron `rrule`, a `model`,
an `execution_environment`, and a `prompt`). They are how anvil delivers
**longer-running, recurring work** — the same project state, worked on a schedule.

These are **templates**. `anvil install codex --write --automations` materializes
them into `~/.codex/automations/<template>-<project-basename>-<hash8>/` (e.g.
`anvil-work-queue-myrepo-3f9a1c20`) with the project path and timestamps filled in,
**`status = "PAUSED"`** so nothing runs until you review and activate it in the
Codex app (Automations). Without `--write` the command is a dry-run that only
previews what it would create. Remove them with `anvil install codex --rollback`.

| Template | What it does |
|---|---|
| `anvil-work-queue` | Claim + execute the next ready anvil task to passing evidence, one PR per task. |
| `anvil-sync-reconcile` | Run `anvil sync` to detect (and, when safe, fix) state drift. |

Placeholders substituted at install: `{{ID}}` (dir name), `{{CWDS}}` (JSON-quoted
project root), `{{TS}}` (epoch-ms timestamp).
