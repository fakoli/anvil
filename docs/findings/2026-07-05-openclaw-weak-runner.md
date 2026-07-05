# 2026-07-05 OpenClaw Weak Runner Live Validation

Task: `v0.4.0:T005` - End-to-end low-ceiling weak OpenClaw runner on Fakoli mini.

Host: `fakoli-mini` (`Darwin Mac 27.0.0`, arm64)

## Result

Pass for the live runner behavior. A weak OpenClaw runner inherited the
plugin-configured Anvil environment, used its own `exec` tool, was offered only
the low-risk `v0.4.0` task under the configured ceilings, completed
claim -> verification-command capture -> submit on that low-risk task, and left
the higher-risk ready task unclaimed.

This file is the evidence artifact for `v0.4.0:T005`. The gateway fixture task
completed on `fakoli-mini`; the local project task `v0.4.0:T005` is submitted
separately through the normal Anvil PR/evidence flow.

Final authoritative OpenClaw run:

- Agent: `fakoli-smith` (`openai/gpt-5.4-mini` configured; run used OpenClaw's `anvil/chat` route)
- Run id: `fc92b324-1c53-4811-be31-e66c5e230fcb`
- Session file: `/Users/sdoumbouya/.openclaw/agents/fakoli-smith/sessions/52bd6da4-f4b0-4af5-b277-30381e80316c.jsonl`
- Workspace: `/Users/sdoumbouya/.openclaw/workspace-fakoli-smith`
- State: `/Users/sdoumbouya/.anvil/workspaces/workspace-fakoli-smith-fa4a5b87/.anvil`

## Gateway Setup

Installed current Anvil on the gateway from a fresh clone:

- Clone: `/Users/sdoumbouya/anvil-t005-live-bIxJ4w`
- Linked executables:
  - `/opt/homebrew/bin/anvil -> /Users/sdoumbouya/.local/bin/anvil`
  - `/opt/homebrew/bin/anvil-mcp -> /Users/sdoumbouya/.local/bin/anvil-mcp`

The first OpenClaw plugin load selected an older stale copy before the fresh
plugin path:

```json
[
  "/Users/sdoumbouya/anvil/openclaw-anvil-intent-router",
  "/Users/sdoumbouya/.openclaw/workspace/openclaw/plugins/anvil/packaging/openclaw/plugin",
  "/Users/sdoumbouya/anvil-t005-live-bIxJ4w/packaging/openclaw/plugin"
]
```

That stale copy rejected the T004 risk keys (`maxBlast`, `maxReviewRisk`), so I
removed the stale path and refreshed OpenClaw. Re-running the documented linked
install after cleanup succeeded and left the current load paths as:

```json
[
  "/Users/sdoumbouya/anvil/openclaw-anvil-intent-router",
  "/Users/sdoumbouya/anvil-t005-live-bIxJ4w/packaging/openclaw/plugin"
]
```

Active plugin entry config after reinstall:

```json
{
  "enabled": true,
  "hooks": { "allowConversationAccess": true },
  "config": {
    "maxBlast": 2,
    "maxReviewRisk": 2,
    "activePrd": "v0.4.0",
    "guidanceLevel": "verbose",
    "claimGuardMode": "warn",
    "guardExec": false
  }
}
```

Residual gateway state intentionally left in place for reproducibility:
the fresh clone, `/opt/homebrew/bin` links, OpenClaw load-path cleanup, smith
workspace, and smith Anvil state.

## Guidance Hook Proof

OpenClaw session JSONL does not persist hidden system-prompt text. To avoid
claiming a prompt excerpt that the harness does not expose, I probed the actual
loaded plugin entry from a temporary copy with only the SDK import path rewritten
to OpenClaw's installed bundle. Calling `before_prompt_build` against the smith
workspace returned `prependSystemContext`:

```json
{
  "registeredHooks": [
    "after_tool_call",
    "before_prompt_build",
    "before_tool_call",
    "before_agent_finalize"
  ],
  "exportedEnv": {
    "ANVIL_MAX_BLAST": "2",
    "ANVIL_MAX_REVIEW_RISK": "2",
    "ANVIL_PRD": "v0.4.0"
  },
  "resultKeys": ["prependSystemContext"],
  "guidanceLength": 2714,
  "guidancePrefix": "[anvil] This project is tracked by anvil.\n\n# Working with anvil - step by step\n\n",
  "containsAnvilTracked": true
}
```

The same workspace also passed the runtime probe used by the hook:
`anvil status --json --cwd /Users/sdoumbouya/.openclaw/workspace-fakoli-smith`
returned `ok: true`.

## Fixture Shape

The final fixture had three reviewed, scored, risk-confirmed tasks:

- `T001` in `default`: low risk, high priority, present to prove PRD scoping.
- `v0.4.0:T001`: low risk, medium priority, `blast_radius=2`, `review_risk=2`.
- `v0.4.0:T002`: high risk, high priority, `blast_radius=5`, `review_risk=3`.

After the low-risk task was submitted, the same workspace still showed the
high-risk task was ready and would be offered without ceilings:

```text
anvil next --prd v0.4.0 --json --cwd /Users/sdoumbouya/.openclaw/workspace-fakoli-smith
-> task.id: v0.4.0:T002
-> task.status: ready
-> scores.blast_radius: 5
-> scores.review_risk: 3
```

With the plugin-equivalent env ceiling, no task was offered:

```text
ANVIL_PRD=v0.4.0 ANVIL_MAX_BLAST=2 ANVIL_MAX_REVIEW_RISK=2 anvil next --json --cwd /Users/sdoumbouya/.openclaw/workspace-fakoli-smith
-> task: null
-> withheld_reason: risk_ceiling
```

## Agent-Owned Exec Proof

Selected raw session excerpts from
`52bd6da4-f4b0-4af5-b277-30381e80316c.jsonl` show the runner used OpenClaw's
own `exec` tool with cwd `/Users/sdoumbouya/.openclaw/workspace-fakoli-smith`.

Tool calls:

```json
{"line":10,"toolCall":"exec","command":"env | sort | grep '^ANVIL_'","workdir":"/Users/sdoumbouya/.openclaw/workspace-fakoli-smith","shell":true}
{"line":12,"toolCall":"exec","command":"anvil next --json","workdir":"/Users/sdoumbouya/.openclaw/workspace-fakoli-smith"}
{"line":14,"toolCall":"exec","command":"anvil claim v0.4.0:T001 --actor agent --json","workdir":"/Users/sdoumbouya/.openclaw/workspace-fakoli-smith"}
{"line":16,"toolCall":"exec","command":"mkdir -p docs && printf 'T005 weak OpenClaw runner proof\\n' > docs/low-risk-note.md","workdir":"/Users/sdoumbouya/.openclaw/workspace-fakoli-smith","shell":true}
{"line":18,"toolCall":"exec","command":"python3 -m pytest --version || true","workdir":"/Users/sdoumbouya/.openclaw/workspace-fakoli-smith","shell":true}
{"line":24,"toolCall":"exec","command":"anvil submit v0.4.0:T001 --actor agent --commands \"python3 -m pytest --version || true\" --files-changed docs/low-risk-note.md --json","workdir":"/Users/sdoumbouya/.openclaw/workspace-fakoli-smith"}
{"line":26,"toolCall":"exec","command":"anvil next --json","workdir":"/Users/sdoumbouya/.openclaw/workspace-fakoli-smith"}
{"line":28,"toolCall":"exec","command":"anvil show v0.4.0:T002 --json","workdir":"/Users/sdoumbouya/.openclaw/workspace-fakoli-smith"}
```

Environment observed inside the runner:

```text
ANVIL_CLOUD_CLASSES=planning
ANVIL_MAX_BLAST=2
ANVIL_MAX_REVIEW_RISK=2
ANVIL_PRD=v0.4.0
```

The agent ran `anvil next --json` without explicit `--prd`, `--max-blast`, or
`--max-review-risk`. Session line 13 recorded:

```json
{
  "tool": "exec",
  "exitCode": 0,
  "command": "next",
  "task_id": "v0.4.0:T001",
  "withheld_reason": null,
  "scores": {
    "blast_radius": 2,
    "review_risk": 2,
    "blast_radius_confirmed": true,
    "review_risk_confirmed": true
  }
}
```

That proves plugin config -> process env -> Anvil CLI propagation for the active
PRD and both risk ceilings. The default PRD task was not selected, and the
high-risk `v0.4.0:T002` was not selected.

## Evidence Gate Proof

The fixture task's required verification command was deliberately
`python3 -m pytest --version || true` so the OpenClaw evidence-capture matcher
would record a bounded, harmless command. This is not a product-test proof; it
proves the runner executed a recognized verification-pattern command through
its own tool, the hook captured it, and `anvil submit` accepted the evidence.
Product validation for this PR is listed separately below.

Session line 25 recorded the submit result:

```json
{
  "tool": "exec",
  "exitCode": 0,
  "command": "submit",
  "evidence_id": "EV7B9D0463",
  "claim_id": "C1AD7E8D8",
  "submitted_by": "agent",
  "commands_run": ["python3 -m pytest --version || true"],
  "files_changed": ["docs/low-risk-note.md"],
  "task_status": "needs_review",
  "evidence_gate": {
    "passed": true,
    "missing": []
  }
}
```

Gateway event rows from
`/Users/sdoumbouya/.anvil/workspaces/workspace-fakoli-smith-fa4a5b87/.anvil/events.jsonl`:

```json
{"line":32,"action":"claim.created","actor":"agent","target_id":"C1AD7E8D8","task_id":"v0.4.0:T001"}
{"line":33,"action":"evidence.submitted","actor":"agent","evidence_id":"EV7B9D0463","claim_id":"C1AD7E8D8","task_id":"v0.4.0:T001","proofs":[{"command":"python3 -m pytest --version || true","exit_code":0,"captured_at":"2026-07-05T20:35:53.237318Z"}],"timestamp":"2026-07-05T20:36:06.957282Z"}
```

The capture timestamp precedes the submit timestamp, which matters because the
OpenClaw hook captures verification output fire-and-forget. The successful run
also depended on the default OpenClaw Anvil actor `agent`, because the capture
hook attaches buffered proof to the first active claim for that actor.

Post-submit session checks:

```json
{"line":27,"command":"next","task_id":null,"withheld_reason":"risk_ceiling"}
{"line":29,"command":"show","task_id":"v0.4.0:T002","status":"ready","scores":{"blast_radius":5,"review_risk":3,"blast_radius_confirmed":true,"review_risk_confirmed":true},"active_claims":[]}
```

## Local PR Validation

These commands were run in the Anvil repo after adding this finding:

```text
uv run --quiet --project .\bin pytest -q tests/test_install.py tests/test_install_manifests.py
-> 86 passed

node --experimental-strip-types --check packaging/openclaw/plugin/index.ts
-> exit 0

uv run --quiet --project .\bin pytest -q tests/test_cli.py -k "next_reads_max_blast_ceiling_from_env or review_tasks_confirms_risk_scores_making_the_ceiling_live"
-> 2 passed, 214 deselected
```

## Notes

Earlier scout/herald loop attempts submitted successfully but did not pass the
strict evidence gate because the OpenClaw capture hook only records
verification-pattern commands and uses actor `agent`. The final smith run is the
authoritative proof because it used actor `agent`, a recognized fixture
verification command, waited for capture, and passed the evidence gate.
