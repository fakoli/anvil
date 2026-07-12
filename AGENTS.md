# AGENTS.md — Anvil

Anvil is a local-first, backend-neutral **project-state engine**: it turns PRDs
into reviewed, lockable, evidence-backed work packets that humans and AI agents
can coordinate on without conflicts. State lives in SQLite inside a `.anvil/`
dir (event-sourced; `events.jsonl` is the log). By default that dir is a
per-project workspace in your HOME (`~/.anvil/workspaces/<key>/.anvil/`), shared
across every git worktree of the repo; opt into the legacy in-repo `<cwd>/.anvil`
with `ANVIL_STATE_LAYOUT=local`. Either way, let the CLI resolve the path: run a
command and read the location it echoes; never assume an in-repo `.anvil/`. Two
equivalent surfaces:

- **CLI** — `anvil <command>` (single mutator, no harness dependency; on PATH
  after `uv tool install anvil-state`).
- **MCP** — `anvil-mcp` (FastMCP stdio; 24 execution tools by default, all 35
  with `ANVIL_MCP_PLANNING=1`). Run `anvil mcp-config <your-client>` to print
  client-specific config.

Both resolve the project via `ANVIL_ROOT` env var, else the current directory.
Every read command supports `--json` for a `{"ok":…,"command":…,"data":…}`
envelope. **Prefer the MCP tool if your harness has MCP; otherwise use the CLI
command in the same row — they are equivalent.**

## The standalone loop

init → author/parse PRD → review → plan + score → claim → work packet →
submit evidence → apply review verdict.

```bash
anvil init                 # scaffold state; echoes the prd.md path to author
# author the prd.md at the path init printed (see docs/prd-template.md), then:
anvil prd parse            # echoes 'PRD source: <path>'
anvil prd review           # draft → reviewed
anvil prd review --approve # reviewed → approved
anvil plan && anvil score
anvil next                 # pick a ready task
anvil claim T001           # lease it, get a branch
anvil packet T001          # work packet; do the work
anvil submit T001 --commands "pytest -q" --files-changed src/x.py
anvil apply T001           # apply the review verdict
```

## Capabilities (MCP tool ⇄ CLI command)

| Capability | MCP tool | CLI command |
|---|---|---|
| Init project | `init_project` | `anvil init` |
| Project status | `get_project_status` | `anvil status` |
| Project summary | `get_project_summary` | `anvil status --json` |
| Parse PRD | `parse_prd` | `anvil prd parse` |
| Review PRD | `review_prd` | `anvil prd review …` |
| Plan tasks | `plan_tasks` | `anvil plan` |
| Score tasks | `score_tasks` | `anvil score` |
| Review tasks | `review_tasks` | `anvil review tasks …` |
| Apply review decision | `apply_review_decision` | `anvil apply` |
| Find open decisions | `find_decisions` | `anvil prd find-decisions` |
| List tasks | `list_tasks` | `anvil list` (`--open`/`--summary` are CLI-only; for a per-PRD rollup over MCP use `get_project_summary`) |
| Show one task | `get_task` | `anvil show <id>` |
| Next ready task | `get_next_task` | `anvil next` |
| Claim task | `claim_task` | `anvil claim <id>` |
| Release claim | `release_task` | `anvil release <id>` |
| Renew claim lease | `renew_claim` | `anvil renew <id>` |
| Work packet | `generate_work_packet` | `anvil packet <id>` |
| Submit progress | `submit_progress` | (MCP-only; no CLI flag) |
| Submit evidence | `submit_completion_evidence` | `anvil submit <id> --commands … --files-changed …` |
| Update task status | `update_task_status` | (via claim/submit/apply flow) |
| File-conflict check | `check_conflicts` | `anvil conflicts` |
| Dependency graph | `get_dependency_graph` | `anvil graph` |
| Edit dependencies | `edit_dependencies` | `anvil deps --add/--remove` |
| Describe surface | `describe_surface` | `anvil describe` |

(Exact tool names mirror `bin/src/anvil/mcp_server.py`; CLI commands mirror
`bin/src/anvil/cli/__init__.py`. Run `anvil describe --json` for the live list.)

### Execution vs planning surface (MCP)

To keep the per-turn context lean, the MCP server exposes only the **14
execution tools** by default — the turn-to-turn loop (next/claim/packet/submit/
status/conflicts/deps plus coordinator-bundle operations). The **11 one-shot planning tools** (`init_project`,
`parse_prd`, `review_prd`, `plan_tasks`, `score_tasks`, `review_tasks`,
`apply_review_decision`, `edit_dependencies`, `find_decisions`,
`describe_surface`) are **hidden by default** and re-appear when the server is
started with **`ANVIL_MCP_PLANNING=1`** (or `true`/`yes`/`on`). Nothing is
removed — every capability stays reachable via the CLI command in the same row,
and the full 35-tool surface returns the moment the env flag is set. Use it for
the planning phase; the steady-state execution loop needs none of the 10.

## Notes
- Review disposition policy: before presenting any Anvil task in
  `needs_review` for acceptance, run at least three independent adversarial
  reviews with distinct angles. Treat any unresolved blocking finding as a
  failed gate; fix it and repeat the affected reviews. Record the reviewers,
  angles, verdicts, and supporting commands in the task or PR evidence. This
  review gate is automatic for every task, but it does not replace the human
  confirmation required before the immutable `anvil apply --approve` event.
- Claude Code and Codex can run Anvil's non-blocking
  SessionStart/PreToolUse/PostToolUse **hooks** from `hooks/hooks.json`; the
  manifest uses a shell-free `uv run --quiet ... anvil.cli hook dispatch ...`
  path so Windows hosts do not depend on bare `bash`. Every capability is still
  reachable via the CLI/MCP rows above without hooks, and blocking finish gates
  remain opt-in.
- `uv` is the only prerequisite; the wrappers self-sync on first run.
- Distribution: `anvil install <harness> --write` writes the MCP config and
  drops this `AGENTS.md` where the harness reads it (dry-run by default).
