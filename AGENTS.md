# AGENTS.md — Anvil

Anvil is a local-first, backend-neutral **project-state engine**: it turns rough
ideas and PRDs into reviewed, lockable, evidence-backed work packets that humans
and AI agents coordinate on without conflicts. State lives in SQLite under
`.anvil/` (event-sourced; `events.jsonl` is the log). Two equivalent surfaces:

- **CLI** — `bin/anvil <command>` (single mutator, zero harness coupling).
- **MCP** — `bin/anvil-mcp` (24 FastMCP stdio tools). Run
  `anvil mcp-config <your-client>` to print paste-ready config.

Both resolve the project via `ANVIL_ROOT` env var, else the current directory.
Every read command supports `--json` for a `{"ok":…,"command":…,"data":…}`
envelope. **Prefer the MCP tool if your harness has MCP; otherwise use the CLI
command in the same row — they are equivalent.**

## The standalone loop

init → author/parse PRD → review → plan + score → claim → work packet →
submit evidence → apply review verdict.

```bash
anvil init                 # scaffold .anvil/
# author .anvil/prd.md (see docs/prd-template.md), then:
anvil prd parse && anvil review prd approve
anvil plan && anvil score
anvil next                 # pick a ready task
anvil claim T001           # lease it, get a branch
anvil packet T001          # work packet; do the work
anvil submit T001 --evidence …
anvil apply T001           # apply the review verdict
```

## Capabilities (MCP tool ⇄ CLI command)

| Capability | MCP tool | CLI command |
|---|---|---|
| Init project | `init_project` | `anvil init` |
| Project status | `get_project_status` | `anvil status` |
| Project summary | `get_project_summary` | `anvil status --json` |
| Parse PRD | `parse_prd` | `anvil prd parse` |
| Review PRD | `review_prd` | `anvil review prd …` |
| Plan tasks | `plan_tasks` | `anvil plan` |
| Score tasks | `score_tasks` | `anvil score` |
| Review tasks | `review_tasks` | `anvil review tasks …` |
| Apply review decision | `apply_review_decision` | `anvil apply` |
| Find open decisions | `find_decisions` | `anvil prd find-decisions` |
| List tasks | `list_tasks` | `anvil list` |
| Show one task | `get_task` | `anvil show <id>` |
| Next ready task | `get_next_task` | `anvil next` |
| Claim task | `claim_task` | `anvil claim <id>` |
| Release claim | `release_task` | `anvil release <id>` |
| Renew claim lease | `renew_claim` | `anvil renew <id>` |
| Work packet | `generate_work_packet` | `anvil packet <id>` |
| Submit progress | `submit_progress` | `anvil submit <id> --progress …` |
| Submit evidence | `submit_completion_evidence` | `anvil submit <id> --evidence …` |
| Update task status | `update_task_status` | (via claim/submit/apply flow) |
| File-conflict check | `check_conflicts` | `anvil conflicts` |
| Dependency graph | `get_dependency_graph` | `anvil graph` / `anvil deps` |
| Edit dependencies | `edit_dependencies` | `anvil deps --add/--remove` |
| Describe surface | `describe_surface` | `anvil describe` |

(Exact tool names mirror `bin/src/anvil/mcp_server.py`; CLI commands mirror
`bin/src/anvil/cli/__init__.py`. Run `anvil describe --json` for the live list.)

## Notes
- Claude Code adds SessionStart/PreToolUse/PostToolUse **hooks**; these are
  Claude-Code-only and have no cross-harness equivalent. Every capability is
  still reachable via the CLI/MCP rows above without them.
- `uv` is the only prerequisite; the wrappers self-sync on first run.
