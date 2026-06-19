# Anvil automations for Codex

Codex [automations](https://developers.openai.com/codex) are scheduled agent runs
defined by `~/.codex/automations/<id>/automation.toml` (cron `rrule`, a `model`,
an `execution_environment`, and a `prompt`). They are how anvil delivers
**longer-running, recurring work** — the same project state, worked on a schedule.

These are **templates**. `anvil install codex --automations` materializes them into
`~/.codex/automations/anvil-<id>-<project>/` with the project path and timestamps
filled in, **`status = "PAUSED"`** so nothing runs until you review and activate it
in the Codex app (Automations). Remove them with `anvil install codex --rollback`.

| Template | What it does |
|---|---|
| `anvil-work-queue` | Claim + execute the next ready anvil task to passing evidence, one PR per task. |
| `anvil-sync-reconcile` | Run `anvil sync` to detect (and, when safe, fix) state drift. |

Placeholders substituted at install: `{{ID}}` (dir name), `{{CWDS}}` (JSON-quoted
project root), `{{TS}}` (epoch-ms timestamp).
