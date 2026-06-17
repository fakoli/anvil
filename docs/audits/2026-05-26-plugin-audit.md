# fakoli-state plugin audit — 2026-05-26

**Auditors:** agent-critic, skill-critic, hook-critic, mcp-critic, structure-critic (fakoli-crew v2.0.0 → v2.1.0)
**Subject:** fakoli-state v1.9.0
**Methodology:** plugin-dev best practices
**Run:** first audit (Phase 10)

## Summary
8 MUST FIX | 25 SHOULD FIX | 21 CONSIDER | 11 NIT.

| Critic | MUST FIX | SHOULD FIX | CONSIDER | NIT |
|---|---|---|---|---|
| agent-critic | 5 | 5 | 4 | 1 |
| skill-critic | 2 | 9 | 6 | 4 |
| hook-critic | 0 | 3 | 5 | 3 |
| mcp-critic | 0 | 4 | 3 | 2 |
| structure-critic | 1 | 4 | 3 | 1 |

## Findings table (severity-sorted)

| Severity | Critic | Target file | Line | Finding | Action | Status |
|---|---|---|---|---|---|---|
| MUST FIX | agent-critic | agents/critic.md | 26 | `allowed-tools:` used instead of `tools:` — silently ignored; agent loads with full unrestricted tool access. | Rename key to `tools:`. | fixed inline |
| MUST FIX | agent-critic | agents/docs-scribe.md | 67 | `allowed-tools:` used instead of `tools:` — silently ignored; full tool access despite author scoping. | Rename key to `tools:`. | fixed inline |
| MUST FIX | agent-critic | agents/marketplace-scribe.md | 67 | `allowed-tools:` used instead of `tools:` — silently ignored; full tool access despite author scoping. | Rename key to `tools:`. | fixed inline |
| MUST FIX | agent-critic | agents/planner.md | 44 | `allowed-tools:` used instead of `tools:` — silently ignored; planner loads with Write/Edit access, breaking Iron Rule. | Rename key to `tools:`. | fixed inline |
| MUST FIX | agent-critic | agents/state-keeper.md | 45 | `allowed-tools:` used instead of `tools:` — silently ignored; full access despite Iron Rule forbidding destructive mutations. | Rename key to `tools:`. | fixed inline |
| MUST FIX | skill-critic | skills/finish/SKILL.md | 249–252 | Dangling reference `/fakoli-state:sentinel` — no such skill exists; invocation will 404. | Remove snippet or repoint to real entry; gate on `claude plugin list \| grep -q "^fakoli-crew"`. | fixed inline |
| MUST FIX | skill-critic | skills/prd/SKILL.md | 57–59 | No overwrite-confirmation gate before `prd parse` mutates `state.db` rows; clobbers hand-authored PRDs. | Add Step 0 preamble matching brainstorm/SKILL.md:162–176 (exists check + summary + `yes/no/save-as-backup` prompt). | fixed inline |
| MUST FIX | structure-critic | README.md | 103 | Skills row lists `verify` skill which does NOT exist on disk — overpromise. | Replace skills row to drop `verify`; note verification is delegated to `fakoli-flow:verify` / `fakoli-crew:sentinel`. | fixed inline |
| SHOULD FIX | agent-critic | agents/critic.md | 12-22 | Description has only 1 `<example>` block (rubric floor is 2; convention is 3). | Add 2 more `<example>` blocks (fakoli-crew fallback path, SHOULD-FIX-only verdict). | deferred |
| SHOULD FIX | agent-critic | agents/planner.md | 12-40 | Description has only 2 `<example>` blocks; below 3-example convention. | Add third example covering re-planning after PRD review failure. | deferred |
| SHOULD FIX | agent-critic | agents/sentinel.md | 12-19 | Description has only 1 `<example>` block AND example lacks `<commentary>`. | Add 2 more examples + `<commentary>` to every example. | deferred |
| SHOULD FIX | agent-critic | agents/sentinel.md | 23-28 | `allowed-tools:` used instead of `tools:` (graded SHOULD because tool list coincides with read-only needs). | Rename key to `tools:`. | deferred |
| SHOULD FIX | agent-critic | agents/sentinel.md | 1-103 | File is 103 lines — at proportionality floor; missing Composition, Inputs, "NOT" boundary, Status File Output sections. | Expand to ~140-180 lines mirroring critic.md structure. | deferred |
| SHOULD FIX | skill-critic | skills/execute/SKILL.md | 8, 15, 256 | Fuzzy detection for `fakoli-flow:execute` — no shell check. | Add Step 0 `claude plugin list \| grep -q "^fakoli-flow"`. | deferred |
| SHOULD FIX | skill-critic | skills/finish/SKILL.md | 8, 245–246 | Fuzzy detection for `fakoli-flow:finish` — no shell gate. | Add explicit `claude plugin list` check at top of Step 1. | deferred |
| SHOULD FIX | skill-critic | skills/claim/SKILL.md | 15, 250 | Fuzzy detection for `fakoli-flow` and `fakoli-crew:welder/scout` — no shell check. | Wrap each "when X is installed" section in `claude plugin list` check or move to `references/composition.md`. | deferred |
| SHOULD FIX | skill-critic | skills/finish/SKILL.md | 177–212 | Fuzzy detection for sync provider availability — Step 5 prose-only "if configured" with no shell checks. | Replace prose with `test -n "$GITHUB_REPOSITORY"`, `gh auth status`, `fakoli-state sync github --health`. | deferred |
| SHOULD FIX | skill-critic | skills/finish/SKILL.md | 247–252 | Fuzzy detection for `fakoli-crew:sentinel` — no shell check. | Add `claude plugin list \| grep -q "^fakoli-crew"` gate. | deferred |
| SHOULD FIX | skill-critic | skills/brainstorm/SKILL.md | 194–197 | Fuzzy detection for LLM availability — no `test -n "$ANTHROPIC_API_KEY"` check. | Replace with explicit shell check; document branches; or move to `references/llm-augmentation.md`. | deferred |
| SHOULD FIX | skill-critic | skills/state-ops/SKILL.md | 119–132 | Phase-availability tables contradict execute/SKILL.md — `fakoli-state conflicts` "pending" here but "available" there. | Reconcile phase tables across skills; consider single `references/phase-status.md`. | deferred |
| SHOULD FIX | skill-critic | skills/state-ops/SKILL.md | 67–96 | Steps 2 and 3 labeled "Phase 3 — pending" but every other skill uses `list`/`show` as available. | Update Step 2/3/4/5 to "available" matching rest of plugin. | deferred |
| SHOULD FIX | skill-critic | skills/state-ops/SKILL.md | 1–4 | Description is longest in plugin (60+ words), weak trigger phrase; missing real user phrasings. | Add concrete trigger phrases in quotes; trim capability list. | deferred |
| SHOULD FIX | hook-critic | hooks/check-claim.sh | 36-59 | Hot-path perf budget violation — spawns `python3` twice (100-300ms) on every Edit/Write/NotebookEdit; exceeds declared 200ms. | Consolidate into single `python3 -c` printing both fields; mirror record-file-change.sh:35-58 pattern. | deferred |
| SHOULD FIX | hook-critic | hooks/record-file-change.sh | 95-106 | Hot-path perf budget violation — `_escape_json()` spawns 4 `python3` instances on fallback path; 5-6 total spawns. | Move JSON escaping into original extraction `python3` block (line 35). | deferred |
| SHOULD FIX | hook-critic | README.md / hooks/README.md | n/a | Non-blocking contract undocumented at plugin/doc level; future maintainer will reintroduce `set -e`. | Add hook-contract section to `hooks/README.md` or `docs/hooks.md` or README "Hooks" row. | deferred |
| SHOULD FIX | mcp-critic | bin/src/fakoli_state/mcp_server.py | 459-464, 526-530, 572-576, 679-684, 733-741, 972-977 | All 6 mutating tools accept actor as plain `str` with no non-empty validation; empty actor persists into audit trail. | Add `_require_actor` helper; call as first line of every mutating tool body. | deferred |
| SHOULD FIX | mcp-critic | bin/src/fakoli_state/mcp_server.py | 327-332 | `list_tasks.status: str \| None` not constrained — typo returns silently empty list. | Replace with `Literal[...]` matching `TaskCountsByStatus` fields. | deferred |
| SHOULD FIX | mcp-critic | bin/src/fakoli_state/mcp_server.py | 327-352, 360-376, 384-450 | Return type `dict[str, Any]` strips field-level schema from Claude's view (3 task-shaped tools). | Define `TaskSummary` or reuse `Task` Pydantic model; drop `json.loads(model_dump_json())` shim. | deferred |
| SHOULD FIX | mcp-critic | bin/src/fakoli_state/mcp_server.py | 384-389 | `get_next_task` accepts `actor` parameter but never uses it — contract lie. | Remove `actor` from signature. | deferred |
| SHOULD FIX | structure-critic | README.md | 37–48 | Install section says "not yet in marketplace" but marketplace.json contains v1.9.0 entry — stale. | Replace manual-clone paragraph with `/plugin marketplace add` + `/plugin install` flow. | deferred |
| SHOULD FIX | structure-critic | CHANGELOG.md | 7–14 | `[Unreleased]` opens with past-tense summary of v1.9.0; content already lives under dated section. | Trim leading "v1.9.0 closes…" sentence; keep forward-looking v2.x notes. | deferred |
| SHOULD FIX | structure-critic | .gitignore | 7 | Covers `bin/.pytest_cache/` but plugin-root `.pytest_cache/` not ignored locally; relies on repo-root. | Add `.pytest_cache/` (no `bin/` prefix). | deferred |
| SHOULD FIX | structure-critic | README.md | 17, 39, 49, 190 | Internally inconsistent install messaging — 4 different phrasings about "once published". | Settle on single install story (`/plugin install fakoli-state@fakoli-plugins`); sweep all 4 sites. | deferred |
| CONSIDER | agent-critic | agents/docs-scribe.md | 1-366 | 366 lines (near 400 ceiling); ~60 lines duplicate marketplace-scribe.md and state-keeper.md composition prose. | Extract shared "three doc/state specialists" composition into `docs/specs/internal-agents.md`. | deferred |
| CONSIDER | agent-critic | agents/marketplace-scribe.md | 1-308 | Same composition duplication as docs-scribe (lines 144-161, 292-308). | Same as above: extract to shared reference. | deferred |
| CONSIDER | agent-critic | agents/state-keeper.md | 1-293 | Same duplication pattern (lines 94-107 restate fakoli-crew composition). | Same as above. | deferred |
| CONSIDER | agent-critic | agents/planner.md | 76-80 | Composition mentions only `fakoli-crew:guido` as defer-to; missing scout/critic. | Add one-line note acknowledging scout role OR explicit "out of planner's responsibility". | deferred |
| CONSIDER | skill-critic | All 7 SKILL.md | n/a | No `references/`, `examples/`, or `scripts/` subdirectories; bodies bundle phase-status tables and composition prose. | Extract to `references/phase-status.md`, `references/composition.md`, etc. | deferred |
| CONSIDER | skill-critic | skills/brainstorm/SKILL.md | 70–110 | Six-question discipline explicit but stopping rule "material" is interpretive. | Add concrete stopping rule: max 1 follow-up if < 5 words, never chain > 8 total. | deferred |
| CONSIDER | skill-critic | skills/plan/SKILL.md | 107–137 | Step 3 documents Phase 7 limitation as 4-step workflow buried in paragraph. | Promote to `### Step 3a — Author subtasks manually` block or extract to `references/manual-task-expansion.md`. | deferred |
| CONSIDER | skill-critic | skills/prd/SKILL.md | 76–84 | Step 1 lacks explicit one-question-per-message discipline (weaker than brainstorm). | Mirror brainstorm: "Ask one question per message. Wait for the answer before asking the next." | deferred |
| CONSIDER | skill-critic | skills/execute/SKILL.md | 70–84 | Step 2 abort flow happens after packet fetch — dishonest agent skips it. | Move Step 2 ahead of packet fetch OR merge into Step 1; make ambiguous-criteria a precondition. | deferred |
| CONSIDER | skill-critic | skills/finish/SKILL.md | 109–116 | `--reason` requirement for `apply --reject` buried in prose (line 112). | Promote requirement to callout at top of section. | deferred |
| CONSIDER | hook-critic | hooks/capture-evidence.sh + hooks/record-file-change.sh | 232 / 113 | Race-prone append on shared files (events.jsonl, orphan.json); JSON records can exceed PIPE_BUF. | Add `flock` guard OR document at-most-rare interleave and have replay tolerate truncation. | deferred |
| CONSIDER | hook-critic | All four .sh files | various `2>/dev/null` | No diagnostic fallback when hook silently fails; no production trail. | Support `FAKOLI_STATE_HOOK_DEBUG=1` env var redirecting stderr to debug log. | deferred |
| CONSIDER | hook-critic | hooks/detect-state.sh | 29 | `$("$CLI" status --hook-format 2>&1)` merges stderr into status line shown to Claude. | Drop `2>&1`; capture stdout only on success branch. | deferred |
| CONSIDER | hook-critic | hooks/capture-evidence.sh + hooks/check-claim.sh + hooks/record-file-change.sh | STATE_DIR | Implicit assumption hook cwd is project root via relative path. | Replace `STATE_DIR=".fakoli-state"` with `STATE_DIR="${CLAUDE_PROJECT_DIR:-$PWD}/.fakoli-state"`. | deferred |
| CONSIDER | hook-critic | hooks/detect-state.sh | 14-20 | Language detection uses sequential overwrites — last match wins; polyglot projects mislabeled. | Either emit comma-joined list OR guard each line with `[ "$DETECTED_LANG" = "unknown" ]`. | deferred |
| CONSIDER | mcp-critic | bin/src/fakoli_state/mcp_server.py | 215-221 + 1-17 | Every tool re-resolves `Path.cwd()` per call; future `os.chdir()` would silently address different project. | Capture `_STATE_DIR` at module import; helpers return cached path. | deferred |
| CONSIDER | mcp-critic | bin/src/fakoli_state/mcp_server.py | 249-257 | `_reap_stale` swallows all exceptions silently; no surfacing in debug traces. | Add `logger.warning("stale-claim reaping failed: %s", exc)`; keep swallow contract. | deferred |
| CONSIDER | mcp-critic | bin/src/fakoli_state/mcp_server.py | 105-112 | `WorkPacketResponse.content: Any` allowed but narrower union possible. | Switch `Any` → `str \| dict[str, Any]`. | deferred |
| CONSIDER | structure-critic | README.md | new section | No top-level surface-count table; counts scattered across deep tables. | Add header table: "ships 6 agents, 7 skills, 4 hooks, 0 commands, 1 CLI, 1 MCP server with 13 tools." | deferred |
| CONSIDER | structure-critic | CHANGELOG.md | 9–14 | Forward-looking v2.x items name LinearIssuesProvider/MondayBoardsProvider/webhooks without issue/PR links. | Append `(see docs/phase-9-backlog.md § "v2.x roadmap" — LB-1 / MB-1 / WS-1)` or equivalent anchor. | deferred |
| CONSIDER | structure-critic | README.md | 5–7 | Minimal badge set (license, version, alpha); no CI / test-count badges. | Add CI status badge and `tests: 964` count badge. | deferred |
| NIT | agent-critic | agents/sentinel.md | 103 | Missing trailing newline. | Add trailing newline. | deferred |
| NIT | skill-critic | skills/brainstorm/SKILL.md | 228 | Phase 7 Notes table cell has `\|` escape that may render literally. | Use HTML entity `&#124;` in code span or rephrase. | deferred |
| NIT | skill-critic | skills/claim/SKILL.md | 138–143 | Example ISO timestamp drifts vs execute/SKILL.md:121 (`2026-05-24` vs `2026-05-25`). | Pick one wall-clock date or use placeholder `<ISO_TIMESTAMP>`. | deferred |
| NIT | skill-critic | skills/state-ops/SKILL.md | 22 | "State-ops is NOT for" sentence repeats 4x in one paragraph. | Format as bulleted "Do not use this skill for:" list. | deferred |
| NIT | skill-critic | skills/prd/SKILL.md | 200–215 | Phase 3 Limitations section duplicates content at lines 39–46. | Delete one table or merge columns. | deferred |
| NIT | hook-critic | hooks/record-file-change.sh | 55-57 | Three `printf … \| sed -n 'Np'` invocations; each `sed` is a fork. | Replace with single `read` / `mapfile` over captured output. | deferred |
| NIT | hook-critic | hooks/check-claim.sh | 95 | `>/dev/null \|\| true` discards CLI stdout; future structured JSON warning would be silently dropped. | Add inline comment cross-referencing CLI subcommand contract docs. | deferred |
| NIT | hook-critic | hooks/capture-evidence.sh | 119-128 | Hardcoded verification-command pattern list; Phase 6+ TODO already flagged. | Track as deferred config-driven matcher; no change in this audit. | deferred |
| NIT | mcp-critic | bin/src/fakoli_state/mcp_server.py | 350, 374, 448 | `json.loads(t.model_dump_json())` triple-roundtrips data through JSON. | Replace with `t.model_dump(mode="json")` or return model directly. | deferred |
| NIT | mcp-critic | bin/src/fakoli_state/mcp_server.py | 162-169 + 940-945 | `DependencyEdge` constructed with `**{"from": ...}` splat to dodge keyword; future reader will trip. | Add comment OR switch to `DependencyEdge.model_validate({...})`. | deferred |
| NIT | structure-critic | README.md | 132 | "Phase 9 (this release, v1.9.0)" parenthetical will stale on v1.10.0 ship. | Replace with "Phase 9 shipped in v1.9.0" for tense-stability. | deferred |

## Per-critic detail: agent-critic

#### Files reviewed
- agents/critic.md (136 lines)
- agents/docs-scribe.md (366 lines)
- agents/marketplace-scribe.md (308 lines)
- agents/planner.md (141 lines)
- agents/sentinel.md (103 lines)
- agents/state-keeper.md (293 lines)

#### Summary
5 MUST FIX | 5 SHOULD FIX | 4 CONSIDER | 1 NIT

#### Findings

##### MUST FIX
| File | Line(s) | Finding | Action |
|---|---|---|---|
| agents/critic.md | 26 | Uses `allowed-tools:` (the COMMAND frontmatter key) instead of `tools:`. On an agent file this key is silently ignored — the agent loads with full unrestricted tool access, defeating the author's intent to scope to `Read/Grep/Glob/Bash` and violating least-privilege for a read-only reviewer. | Rename key to `tools:`. Replace `allowed-tools:` with `tools:` (same list members preserved). |
| agents/docs-scribe.md | 67 | Uses `allowed-tools:` instead of `tools:`. Silently ignored; agent loads with full tool access despite the author scoping to `Read/Write/Edit/Glob/Grep`. | Rename key to `tools:`. |
| agents/marketplace-scribe.md | 67 | Uses `allowed-tools:` instead of `tools:`. Silently ignored; agent loads with full tool access despite the author scoping to `Read/Write/Edit/Bash/Glob/Grep`. | Rename key to `tools:`. |
| agents/planner.md | 44 | Uses `allowed-tools:` instead of `tools:`. Silently ignored; agent intended to be read-only-plus-Bash but loads with full access including Write/Edit, breaking the Iron Rule that planner never writes to `.fakoli-state/`. This is the highest-stakes occurrence in the set because the Iron Rule explicitly forbids writes. | Rename key to `tools:`. |
| agents/state-keeper.md | 45 | Uses `allowed-tools:` instead of `tools:`. Silently ignored; agent loads with full unrestricted tool access. The Iron Rule forbids destructive git, state, and filesystem mutations — but the frontmatter that was supposed to enforce least-privilege does not actually take effect. Highest blast-radius occurrence given the agent's proximity to git and state files. | Rename key to `tools:`. |

##### SHOULD FIX
| File | Line(s) | Finding | Action |
|---|---|---|---|
| agents/critic.md | 12-22 | Description contains only 1 `<example>` block. The fakoli-crew convention is 3, the rubric floor is 2. With one example Claude has only one shape of trigger surface to match against, so the agent will dispatch unreliably for adjacent phrasings ("audit T012", "PR review T012", "is T012 ready"). | Add 2 more `<example>` blocks: one for the "fakoli-crew not installed" fallback path, one for a SHOULD-FIX-only verdict case. Each block needs its own `<commentary>`. |
| agents/planner.md | 12-40 | Description contains only 2 `<example>` blocks. Within rubric tolerance but below the 3-example fakoli-crew convention used by every other healthy agent in this set. Two of the three existing scenarios already exist (greenfield + incremental); the missing third is the expansion path (which has its own example) — but no example covers "PRD review failed, planner asked to retry". | Add a third `<example>` covering an edge case: re-planning after a `fakoli-state plan` rejection or an incremental update that conflicts with existing claims. |
| agents/sentinel.md | 12-19 | Description contains only 1 `<example>` block AND the example lacks a `<commentary>` rationale block. The commentary is what Claude reads to decide why the agent is right for the scenario; without it the example is decorative. Combined with single-example coverage, dispatch reliability is poor — sentinel will lose to fakoli-crew:sentinel even when the latter is not available, because the description offers no concrete trigger surface for "validate evidence". | Add 2 more `<example>` blocks, and add a `<commentary>` to every example. At minimum cover: (a) standalone fallback (current example, plus commentary), (b) merge-with-fakoli-crew:sentinel scorecards, (c) FAIL path with conflicting evidence. |
| agents/sentinel.md | 23-28 | Uses `allowed-tools:` instead of `tools:`. Same silent-ignore antipattern as the other four occurrences. Listed here as SHOULD FIX rather than MUST FIX only because sentinel.md's tool list (`Read/Grep/Glob/Bash`) happens to coincide with what a read-only validator actually needs — the over-grant is real (Write/Edit silently allowed) but the Iron Rule on line 38 still constrains behavior at the prompt level. NOTE: severity could justifiably be MUST FIX; see Decisions. | Rename key to `tools:`. |
| agents/sentinel.md | 1-103 | File is 103 lines — right at the proportionality floor (rubric flags <100 as under-specified). The agent has no Composition section, no Inputs section, no explicit defer-to map beyond the brief mention on line 34, and no "what does sentinel NOT do" boundary statement. By contrast critic.md (the sibling fallback agent) spends 136 lines and includes a Composition with fakoli-crew section. Sentinel.md should match that pattern. | Expand sentinel.md to ~140-180 lines: add a Composition with fakoli-crew section mirroring critic.md lines 84-86, add an Inputs section, add a "What you do NOT do" boundary statement, and add a Status File Output note matching docs-scribe / state-keeper / marketplace-scribe. |

##### CONSIDER
| File | Line(s) | Finding | Action |
|---|---|---|---|
| agents/docs-scribe.md | 1-366 | File is 366 lines — within the rubric ceiling of 400 but close. Approximately 60 lines of the body (Composition with fakoli-crew lines 143-160, Composition Inside fakoli-state lines 162-178) duplicate prose that also appears in marketplace-scribe.md and state-keeper.md. Three agents repeating the same three-way composition table is a maintenance hazard: any time one changes, all three must update. | Consider extracting the shared "three doc/state specialists inside fakoli-state" composition table into a skill or a shared docs page (e.g., `docs/specs/internal-agents.md`), then have all three agents link to it. Lower priority than the MUST FIX cluster but worth queueing. |
| agents/marketplace-scribe.md | 1-308 | Same duplication as docs-scribe.md — lines 144-161 (Composition with fakoli-crew) and lines 292-308 (Composition Inside fakoli-state) restate the same three-way breakdown. | Same as above: extract to shared reference. |
| agents/state-keeper.md | 1-293 | Same duplication pattern — lines 94-107 restate the fakoli-crew composition story. Less severe because state-keeper's composition is genuinely narrower (operator-facing only), but it still drifts if the others change. | Same as above. |
| agents/planner.md | 76-80 | Composition with fakoli-crew section mentions only `fakoli-crew:guido` as a defer-to. The fakoli-crew set also has `critic`, `scout`, and others that could be relevant when planning (e.g., scout for codebase recon before scoping). The current scope is defensible — planner explicitly stays in the WHAT — but a one-line "for codebase recon, ask the caller to dispatch fakoli-crew:scout first" would close a gap. | Add a one-line note acknowledging scout's role in pre-planning reconnaissance, OR explicitly note that scout is out of planner's dispatch responsibility. |

##### NIT
| File | Line(s) | Finding | Action |
|---|---|---|---|
| agents/sentinel.md | 103 | File ends without a trailing newline visible in the read output (last line is the verdict-rules bullet at line 103 with no continuation). All other agent files end with a blank trailing line. Minor style drift. | Add trailing newline. |

#### Cross-cutting observations

**Color collisions:** None within fakoli-state. The 6 agents use cyan, white, purple, magenta, teal, gray — all distinct. Cross-plugin collisions exist (state-keeper teal == fakoli-crew:skill-critic teal; critic magenta == fakoli-crew:agent-critic magenta) but the rubric scopes collision detection to siblings within the same plugin's `agents/` directory, so these are not flagged.

**Defer-to references are all valid.** Every `fakoli-crew:<agent>` reference in the set resolves to a real file in `plugins/fakoli-crew/agents/`:
- `fakoli-crew:critic` (referenced by critic.md, state-keeper.md) — exists
- `fakoli-crew:sentinel` (referenced by sentinel.md, state-keeper.md) — exists
- `fakoli-crew:herald` (referenced by docs-scribe.md, marketplace-scribe.md) — exists
- `fakoli-crew:keeper` (referenced by docs-scribe.md, marketplace-scribe.md, state-keeper.md) — exists
- `fakoli-crew:guido` (referenced by planner.md, state-keeper.md) — exists

**`allowed-tools:` is the dominant antipattern.** 5 of 6 files (all except… wait, all 6 use it). Re-verified: critic.md L26, docs-scribe.md L67, marketplace-scribe.md L67, planner.md L44, sentinel.md L25, state-keeper.md L45 — all six agents in fakoli-state use `allowed-tools:` instead of `tools:`. Every one of them loads with full unrestricted tool access in the runtime despite frontmatter scoping. This is a systemic issue in fakoli-state, not a per-file slip. Fix is mechanical and one-line per file but the impact is unrestricted-tool dispatch for the entire plugin. The plugin's tool least-privilege story is currently fiction.

**Description discipline drifts toward minimum across the set.** The three "scribe/keeper" agents (docs-scribe, marketplace-scribe, state-keeper) carry 3 strong examples each with full `<commentary>` blocks. The three "fallback/planner" agents (critic, planner, sentinel) carry 1, 2, and 1 examples respectively. The pattern suggests the scribe agents were authored to the fakoli-crew template and the fallback agents were authored against a thinner brief. The fix is to bring the three lighter agents up to the scribe agents' standard, not to relax the scribe agents.

**Composition sections are duplicated three ways.** docs-scribe, marketplace-scribe, and state-keeper each carry near-identical "Composition Inside fakoli-state" sections describing the three-way split. If the split ever changes, three files need to update in lockstep. This is a CONSIDER, not a MUST FIX, but it will become a maintenance burden.

**Model and color choices are sensible.** Every agent uses `model: opus`, appropriate for the analytical work these agents perform. Colors are distinct within plugin. No model misspellings detected.

**Naming is clean.** All `name:` values are lowercase, hyphen-only, match their filenames, are unique within plugin, and are within the 3-50 char range. No silent dispatch failures from naming.

#### Decisions

- **sentinel.md `allowed-tools:` graded SHOULD FIX, not MUST FIX.** The rubric explicitly calls this antipattern MUST FIX. I considered grading it MUST FIX for consistency with the other four occurrences. I ultimately graded it SHOULD FIX because sentinel.md is unique in this set: its requested tool list (Read/Grep/Glob/Bash) happens to be exactly what a read-only evidence validator needs, so the silent over-grant matters less in practice — the agent's behavior is constrained by its Iron Rule on line 38 rather than by tool restriction. A defensible alternative grading would be MUST FIX for systemic consistency. The reviewer of this audit is welcome to upgrade. The other four occurrences (critic, docs-scribe, marketplace-scribe, planner, state-keeper) are unambiguously MUST FIX because their tool lists include Write/Edit/Bash and the silent over-grant materially expands what the agent could do beyond the author's intent.

- **Single-example descriptions graded SHOULD FIX, not MUST FIX.** The rubric's MUST FIX list explicitly covers frontmatter validity (missing keys, wrong values, antipatterns) and cross-file collisions; example-count is in the SHOULD FIX (Description Quality) section. Even though one example is below the rubric floor of 2, the grading rule is clear.

- **Duplicated composition sections graded CONSIDER, not SHOULD FIX.** No file is currently inaccurate; the risk is future drift. SHOULD FIX implies "will bite" — this one might bite later. CONSIDER is the honest grade.

- **Cross-plugin color collisions not flagged.** The rubric scopes collision detection to "siblings in the same plugin's `agents/` directory" (line 106 of agent-critic.md). state-keeper.md `color: teal` collides with `fakoli-crew/agents/skill-critic.md`, and critic.md `color: magenta` collides with `fakoli-crew/agents/agent-critic.md`. These are noted in Cross-cutting observations but not graded as findings.

## Per-critic detail: skill-critic

#### Files reviewed
- skills/brainstorm/SKILL.md (230 lines)
- skills/claim/SKILL.md (279 lines)
- skills/execute/SKILL.md (273 lines)
- skills/finish/SKILL.md (269 lines)
- skills/plan/SKILL.md (253 lines)
- skills/prd/SKILL.md (214 lines)
- skills/state-ops/SKILL.md (327 lines)

#### Summary
2 MUST FIX | 9 SHOULD FIX | 6 CONSIDER | 4 NIT

#### Findings

##### MUST FIX
| File | Line(s) | Finding | Action |
|---|---|---|---|
| skills/finish/SKILL.md | 249–252 | Dangling reference — the snippet `/fakoli-state:sentinel` is shown as a manual trigger ("trigger manually"), but no `skills/sentinel/SKILL.md` exists in fakoli-state. Sentinel is an *agent* (`agents/sentinel.md`) and (per the prose) a `fakoli-crew:sentinel` skill — invoking `/fakoli-state:sentinel` will fail. The next Claude instance will follow the broken slash command and 404. | Either (a) remove the `/fakoli-state:sentinel` snippet entirely and replace with prose "if `fakoli-crew` is installed, dispatch the sentinel agent before applying", or (b) point at the real entry point (`/fakoli-crew:sentinel` if that exists, or the agent dispatch verb). Add an explicit `claude plugin list 2>/dev/null \| grep -q "^fakoli-crew"` gate before the snippet. |
| skills/prd/SKILL.md | 57–59 | Skill writes/edits `.fakoli-state/prd.md` via `$EDITOR` without first checking whether the file exists and without an overwrite-confirmation gate. Step 1 jumps straight to "open the PRD in an editor" and Step 2 then runs `prd parse`. This is the same hazard `brainstorm` correctly guards against in its Step 4. A solo PRD author who already has a hand-authored file and runs this skill could clobber it via re-prompted authoring → re-parse, with no `(yes / no / save-as-backup)` gate before the parse mutates `state.db`. | Add a Step 0 / Step 1 preamble matching the brainstorm template at brainstorm/SKILL.md:162–176: `ls .fakoli-state/prd.md 2>/dev/null`, show a one-line summary if present, then prompt `Overwrite / append / save-as-backup`. Apply the same gate before re-parse in the "Iterating" section. |

##### SHOULD FIX
| File | Line(s) | Finding | Action |
|---|---|---|---|
| skills/execute/SKILL.md | 8, 15, 256 | Fuzzy detection for `fakoli-flow:execute` — prose says "When `fakoli-flow:execute` is NOT installed" and "When `fakoli-flow:execute` is installed" with no shell check. brainstorm/SKILL.md:48 is the reference implementation (`claude plugin list 2>/dev/null \| grep -q "^fakoli-flow"`) — execute violates the no-fuzzy-detection rule. | Add an explicit Step 0 to execute/SKILL.md before Step 1: run `claude plugin list 2>/dev/null \| grep -q "^fakoli-flow"`; on exit 0, hand off to `/fakoli-flow:execute`; on non-zero, fall through. Mirror the brainstorm Step 1 block. |
| skills/finish/SKILL.md | 8, 245–246 | Same fuzzy-detection violation for `fakoli-flow:finish`. Prose-only "When `fakoli-flow:finish` is installed" with no shell gate. | Add explicit `claude plugin list 2>/dev/null \| grep -q "^fakoli-flow"` check at the top of Step 1 (or new Step 0), branch on exit code, document fall-through. |
| skills/claim/SKILL.md | 15, 250 | Same fuzzy-detection violation for `fakoli-flow:execute` / `fakoli-flow` and `fakoli-crew:welder`/`scout`. Prose mentions "When `fakoli-flow` is installed" / "When `fakoli-crew` is installed" with no shell-check gate. | Wrap each "when X is installed" section in a `claude plugin list 2>/dev/null \| grep -q "^X"` check or move the prose to a `references/composition.md` so the SKILL.md body only describes the solo path. |
| skills/finish/SKILL.md | 177–212 | Fuzzy detection for sync provider availability — Step 5 says "If the project has a sync provider configured (a `GITHUB_REPOSITORY` env var, a `gh auth` session, or any contributor-registered provider in `PROVIDER_REGISTRY`)" but no explicit shell checks are shown. The agent is left to interpret "configured" subjectively. | Replace prose with explicit checks: `test -n "$GITHUB_REPOSITORY"`, `gh auth status >/dev/null 2>&1`, and `fakoli-state sync github --health` (which the skill already documents at line 196). Branch on exit codes; document the fall-through clearly. |
| skills/finish/SKILL.md | 247–252 | Fuzzy detection for `fakoli-crew:sentinel` — "When `fakoli-crew:sentinel` is present" with no shell check. Compounds the MUST FIX dangling-reference issue above. | Add `claude plugin list 2>/dev/null \| grep -q "^fakoli-crew"` gate, then branch on exit code. Document fall-through. |
| skills/brainstorm/SKILL.md | 194–197 | Fuzzy detection for LLM availability — "when `ANTHROPIC_API_KEY` is set" / "if you want to use LLM augmentation explicitly, the user can set `ANTHROPIC_API_KEY`" with no `test -n "$ANTHROPIC_API_KEY"` check and no concrete branching behavior. Either remove the section as aspirational, or gate it on an explicit shell check. | Replace with `test -n "$ANTHROPIC_API_KEY"` shell check, document what the LLM-augmented branch actually does on exit 0, and what the deterministic fallback does on non-zero. Or move to a `references/llm-augmentation.md` and remove from SKILL.md body. |
| skills/state-ops/SKILL.md | 119–132 | Step 5 advertises `fakoli-state conflicts` as "Phase 5 — pending" while the same command in skills/execute/SKILL.md (line 272) is marked "Phase 5 — available". One of these is wrong; the next Claude instance gets contradictory phase-availability info from two skills in the same plugin. Same kind of stale-status issue at state-ops/SKILL.md:303–327 (Phase 2 Limitations) where commands the execute/finish/claim skills exercise as "available" are listed here as "pending". | Reconcile the phase-availability tables across skills. state-ops is the canonical inspection skill — its tables should reflect Phase 8 reality. Rewrite the Phase 2 Limitations section as "current availability" with one row per command, or move the section to `references/phase-status.md` and dedupe. |
| skills/state-ops/SKILL.md | 67–96 | Steps 2 and 3 are labeled "Phase 3 — pending" — but every other skill in the plugin uses `list` and `show` as available commands. A reader of state-ops will conclude these commands do not exist; a reader of claim/execute will conclude they do. Self-contradicting documentation is a SHOULD FIX. | Update Step 2 and Step 3 to "Phase 3 — available" (matching the rest of the plugin), and likewise for Step 4 (`fakoli-state next`, Phase 4) and Step 5 (`fakoli-state conflicts`, Phase 5). |
| skills/state-ops/SKILL.md | 1–4 | Description is the longest in the plugin (60+ words) and reads as a capability dump rather than a triggering instruction. It lists what the skill does ("list tasks, show task details, find the next claimable task, summarize active claims and blockers, check file-conflict warnings, and reconcile state with the filesystem and git") but the trigger phrase is weak: "Use this skill when you want to see what fakoli-state knows without changing anything." Real user phrasing is more likely "what's the status", "what tasks are ready", "show me active claims", "is there a conflict on this file" — none of those phrases appear. | Add concrete trigger phrases in quotes: "show project status", "list ready tasks", "what's blocking T012", "are there file conflicts", "show me active claims". Trim the capability list. |

##### CONSIDER
| File | Line(s) | Finding | Action |
|---|---|---|---|
| All 7 SKILL.md | n/a | No `references/`, `examples/`, or `scripts/` subdirectories exist in any skill, despite 1,600–2,100 word bodies. Files are still under the 5,000-word ceiling, so this is not MUST/SHOULD FIX, but several skills (state-ops, claim, finish) bundle phase-status tables, sync provider docs, and crew/flow composition prose into SKILL.md that would lazy-load better from `references/`. | Extract Phase N Limitations tables to `references/phase-status.md`, the GitHub-sync detail in finish/Step 5 to `references/github-sync-from-finish.md` (or reuse `docs/github-sync.md` directly), and the "When fakoli-flow/fakoli-crew is installed" composition prose to `references/composition.md`. Cuts SKILL.md bodies by 20–30%. |
| skills/brainstorm/SKILL.md | 70–110 | Six questions are well-structured and one-per-message discipline is explicit (line 72 "Ask one question per message. Wait for the answer before asking the next."), but the bound is fuzzy at line 110 ("Stop at six questions unless something material remains unclear"). "Material" is interpretive. | Add a concrete stopping rule: "ask at most one follow-up per question if the answer is under 5 words; never chain more than 8 total questions; if 8 are not enough, propose deferring to PRD revision and stop." |
| skills/plan/SKILL.md | 107–137 | Step 3 documents a Phase 7 limitation by telling the reader to "author the subtasks manually in prd.md". That's a 4-step workflow buried in a paragraph. | Promote the workaround to its own labeled `### Step 3a — Author subtasks manually (Phase 3 workaround)` block with the four shell commands as a numbered list. Or extract to `references/manual-task-expansion.md`. |
| skills/prd/SKILL.md | 76–84 | Step 1 says "When working interactively, resist the urge to dump the full template at once. Proceed one question at a time:" — but only enumerates three topics ("goals", "requirements", "features and tasks") with no explicit one-question-per-message instruction or wait-for-answer discipline. brainstorm/SKILL.md:72 is the gold standard; prd's interview discipline is weaker. | Tighten Step 1 to mirror brainstorm: explicit "Ask one question per message. Wait for the answer before asking the next." Bound the topic count. |
| skills/execute/SKILL.md | 70–84 | Step 2 ("Confirm scope before writing code") tells the agent to release the claim if criteria are ambiguous — but the release is a side-effect of a check that happens before any work. The flow ordering means an honest agent does the work, the dishonest agent skips Step 2. | Move Step 2 ahead of the packet fetch (Step 1), or merge it into Step 1's "Read the packet immediately after fetching it." Make ambiguous-criteria detection a precondition for Step 3, not a recoverable abort. |
| skills/finish/SKILL.md | 109–116 | "Reject and reopen" path requires `--reason` but the reason example ("pytest -x reports 3 failures in test_retry.py") is buried in prose. A reader scanning the section may not notice `--reason` is required until they hit the error. | Highlight the requirement at the top of the section: "`--reason` is required. Without it, `apply --reject` exits 2." Currently this appears at line 112 but not as a callout. |

##### NIT
| File | Line(s) | Finding | Action |
|---|---|---|---|
| skills/brainstorm/SKILL.md | 228 | Phase 7 Notes table cell contains a `\|` escape that may render literally in some markdown renderers: "detect via `claude plugin list \| grep fakoli-flow`". | Use a HTML entity `&#124;` inside the code span, or change the cell to "detect via `claude plugin list` piped to grep". |
| skills/claim/SKILL.md | 138–143 | Example output block shows ISO timestamp `2026-05-24T19:00:00Z`; execute/SKILL.md:121 shows `2026-05-25T14:35:00Z`. Trivial drift but distracting if the reader compares them. | Pick one wall-clock date across all examples or use a placeholder like `<ISO_TIMESTAMP>`. |
| skills/state-ops/SKILL.md | 22 | "State-ops is NOT for ..." sentence repeats "State-ops is NOT for" four times in one paragraph. Reads as a checklist crammed into prose. | Format as a bulleted "Do not use this skill for:" list, mirroring brainstorm/SKILL.md:19. |
| skills/prd/SKILL.md | 200–215 | Phase 3 Limitations section duplicates content already covered at lines 39–46 (Phase 3 commands table). The plugin author appears to have evolved the table without removing the older one. | Delete one of the two tables, or merge the columns. |

#### Cross-cutting observations

- **No-fuzzy-detection rule is the dominant gap across the plugin.** brainstorm is the gold standard — it gets the `claude plugin list 2>/dev/null | grep -q "^fakoli-flow"` check exactly right at line 48, branches on exit code, and documents the fall-through. Every other skill that conditionally bridges to fakoli-flow or fakoli-crew (claim, execute, finish) uses prose-only "when X is installed" framing without the shell check. The fix is a 3-line copy-paste of brainstorm/Step 1 into each peer skill — high leverage, low effort.

- **Hard-gate discipline is asymmetric.** brainstorm has a textbook overwrite gate (lines 162–176): existing-file check, summary preview, three-way (`yes / no / save-as-backup`) confirmation. prd, which writes the same `.fakoli-state/prd.md` file (via `$EDITOR` + `prd parse`), has no comparable gate. This is the MUST FIX entry — the protection brainstorm builds is bypassed entirely if the user enters via prd instead.

- **Phase-status tables drift across skills.** `fakoli-state conflicts` is "pending" in state-ops, "available" in execute. `fakoli-state list`/`show` are "pending" in state-ops Step 2/3, "available" in every other skill's Phase table. The plugin is at v1.9.0 / Phase 10 per the brief — state-ops is the laggard. Consolidating phase tables into `docs/phase-status.md` (single source of truth) and referencing it from each SKILL.md would eliminate the drift surface.

- **Composition with sibling skills is well-documented.** Every skill ends with a "Composition with Other Skills" table that names before/after/instead-of relationships. This is exactly what the methodology calls for and the plugin gets it right uniformly.

- **No `references/`, `examples/`, or `scripts/` subdirectories exist in any of the 7 skills.** All bodies are under 5,000 words so this is not a MUST FIX, but state-ops at 327 lines / ~2,050 words and claim at 279 lines / ~2,050 words are approaching the soft ceiling and would benefit from extraction.

- **One-question-at-a-time discipline.** brainstorm enforces it explicitly (line 72). prd documents the interview pattern but does not enforce one-question-per-message (CONSIDER above). No other skill conducts user interviews.

#### Decisions

- **The `/fakoli-state:sentinel` dangling reference (finish/SKILL.md:251) is MUST FIX rather than SHOULD FIX** because it is an executable slash command — the next Claude instance will literally invoke it and fail, with no graceful degradation. Path Hygiene in the rubric is unambiguous on this.

- **The PRD overwrite gate (prd/SKILL.md:57–59) is MUST FIX rather than SHOULD FIX** despite the file mutation being mediated by `$EDITOR` (which has its own overwrite semantics). The hazard is the *subsequent* `prd parse` step (line 91) which replaces all `Requirement`/`Feature`/`Task` rows in `state.db` — that's the destructive mutation the brainstorm gate guards against, and prd has no equivalent gate before re-parse. The methodology rule reads "any skill step that writes, overwrites, deletes, or otherwise mutates user-owned files" — the `state.db` rows are user-owned via the PRD.

- **Phase-status table drift in state-ops is SHOULD FIX rather than MUST FIX** because the wrong information is "more cautious than reality" (commands marked pending that are actually available) — a Claude instance reading state-ops will incorrectly hesitate to invoke a working command, but will not break anything. If the drift were the other way (marking pending commands as available) it would be MUST FIX.

- **Fuzzy detection across claim/execute/finish is SHOULD FIX rather than MUST FIX** because the prose framing "when X is installed" is interpretable — a careful agent will check `claude plugin list` even without being told to. But the methodology bar is explicit shell checks, and brainstorm proves it can be done in 3 lines, so the gap is real.

- **No `references/` directory is CONSIDER rather than SHOULD FIX** because all SKILL.md bodies are under 2,100 words — within the 1,500–2,000 target band and well under the 5,000 ceiling. The lazy-loading bar is met by being short, not by having a `references/` directory per se.

## Per-critic detail: hook-critic

#### Files reviewed
- hooks/capture-evidence.sh (235 lines)
- hooks/check-claim.sh (97 lines)
- hooks/detect-state.sh (47 lines)
- hooks/record-file-change.sh (115 lines)
- hooks/hooks.json (50 lines)

#### Contract detection
**Detected contract:** non-blocking

**Signals that produced this conclusion:**

1. **hooks.json events:** `SessionStart` (1 hook), `PreToolUse` with `Edit|Write|NotebookEdit` matcher (1 hook), `PostToolUse` with `Edit|Write|NotebookEdit` matcher (1 hook), `PostToolUse` with `Bash` matcher (1 hook). Three of the four dispatched scripts run on hot tool-event matchers where non-zero exits would block / surface as stderr-to-Claude.

2. **README/docs language:** README has NO non-blocking-specific prose. Searched `plugins/fakoli-state/README.md` and `CHANGELOG.md` for "non-blocking", "never block", "warning-only", "always exit 0", "must not block", "best-effort", "fast path", "< Nms" — no matches in the README. **No `docs/hooks.md` exists.** The contract is therefore not declared at the doc level. However, it IS declared explicitly in the script-header comments (see signal 3).

3. **Hook bodies & header comments:**
   - **3/4 hook scripts carry an identical explicit contract comment:**
     - `hooks/capture-evidence.sh:18` — `# Rules: no set -e, no piped grep, always exit 0, complete in < 200ms.`
     - `hooks/check-claim.sh:8` — `# Rules: no set -e, no piped grep, always exit 0, complete in < 200ms.`
     - `hooks/record-file-change.sh:12` — `# Rules: no set -e, no piped grep, always exit 0, complete in < 200ms.`
   - `hooks/detect-state.sh:5` — `# Rules: no set -e, no piped grep, always exit 0, complete in < 1 second.` (same contract, looser perf budget appropriate for SessionStart).
   - **4/4 hooks end with unconditional `exit 0`** (capture-evidence:235, check-claim:97, detect-state:47, record-file-change:115). Multiple intermediate fast-path `exit 0` lines also unconditional.
   - **4/4 hooks omit `set -e`, `set -u`, `set -o pipefail`** entirely.
   - **CLI shell-outs are wrapped to never surface non-zero:** check-claim:95 uses `>/dev/null || true`; capture-evidence:155-168 captures `CLI_EXIT` and falls through on non-zero; record-file-change:77-88 does the same.
   - **Errors are swallowed with `2>/dev/null`** throughout (capture-evidence: 9 occurrences; check-claim: 2; record-file-change: 5; detect-state: 1 in `head -1`).

   All three of the critic-prompt-mandated checks (unconditional exit 0, CLI calls wrapped, errors swallowed) are TRUE across all four scripts.

   **Conclusion: non-blocking contract is operative.** The contract is explicit in the per-script header comments but absent from README/docs — that's a documentation gap (flagged below as SHOULD FIX) but does not change which contract is enforced.

#### Summary
**0** MUST FIX | **3** SHOULD FIX | **5** CONSIDER | **3** NIT

#### Findings

##### MUST FIX
| File | Line(s) | Finding | Action |
|---|---|---|---|
_(none)_

##### SHOULD FIX
| File | Line(s) | Finding | Action |
|---|---|---|---|
| `hooks/check-claim.sh` | 36-59 | **Hot-path perf budget violation:** the script spawns `python3` twice on every Edit/Write/NotebookEdit — once to extract `FILE_PATH` (line 37), then again immediately to extract `ACTOR` (line 51). Each cold `python3` spawn is 50-150ms; together that's 100-300ms before the CLI is even invoked. The script's own header (line 8) declares "complete in < 200ms" and the hook fires on a hot PreToolUse matcher. Consolidate into a single python3 round-trip that prints both fields. | Replace the two separate `python3 -c` blocks with one call that prints two lines (path then actor) and `read` them, mirroring the pattern record-file-change.sh:35-58 already uses. |
| `hooks/record-file-change.sh` | 95-106 | **Hot-path perf budget violation:** `_escape_json()` is defined on line 95 and called four times on lines 103-106 (`ESCAPED_PATH`, `ESCAPED_TOOL`, `ESCAPED_ACTOR`, `ESCAPED_TS`). Each call spawns a fresh `python3` — four extra subprocess starts on the fallback path, on top of the one extraction call on line 35 and the optional CLI call on line 77. That's potentially 5-6 python3 spawns on a PostToolUse hot event, well over the 200ms budget declared at line 12. | Move the JSON escaping into the original extraction `python3` block (line 35) — emit pre-escaped values, or emit the entire well-formed JSON line and let bash just append it. Eliminates the four extra spawns. |
| `hooks/README.md` | (file does not exist) / README.md | **Contract is undocumented at the plugin level.** The non-blocking contract is enforced consistently across all four scripts via header comments, but a reader of the plugin README or `docs/` would not learn that fakoli-state hooks are non-blocking, why `set -e` is forbidden, or what the perf budget is. The next maintainer who hasn't read every script will reintroduce `set -e` and silently break PreToolUse. Document the contract in one of: (a) a new `hooks/README.md`, (b) a `docs/hooks.md` section, or (c) the main `README.md` "Hooks" row in the components table (line 105). | Add a short hook-contract section. Suggested copy: "All fakoli-state hooks are non-blocking: they must `exit 0` regardless of internal failure, must not use `set -e`/`set -u`/`set -o pipefail`, must wrap CLI calls with `\|\| true`, and must complete in < 200ms on hot events (PreToolUse, PostToolUse). The SessionStart hook may take up to 1 s." |

##### CONSIDER
| File | Line(s) | Finding | Action |
|---|---|---|---|
| `hooks/capture-evidence.sh` | 232 (via python3) and `hooks/record-file-change.sh` | 113 | **Race-prone append on shared files.** `events.jsonl` (record-file-change line 113) and `orphan.json` (capture-evidence line 232) are appended to with bare `>>` / python `open(..., 'a')`. Concurrent hook invocations from parallel agents would rarely but eventually interleave bytes. record-file-change.sh:110-112 acknowledges this. POSIX `O_APPEND` makes < PIPE_BUF (typically 4 KiB) single-write appends atomic, which usually saves these lines, but JSON records from `capture-evidence` can exceed PIPE_BUF when STDOUT_EXCERPT is near MAX_EXCERPT=4000 plus framing. | Either (a) guard the append with `flock` on a sibling lock file, or (b) document the at-most-rare interleave risk and let the replay engine tolerate truncated lines. Phase 4-era TODO is fine; track it. |
| All four .sh files | various `2>/dev/null` | **No diagnostic fallback when a hook silently fails.** Every `python3` and `mktemp` redirects stderr to `/dev/null`. When a hook misbehaves in production (DB lock, malformed payload, missing python3) there is no record. The on-call engineer at 3am has no trail. | Support `FAKOLI_STATE_HOOK_DEBUG=1` env var that redirects stderr to `.fakoli-state/.hook-debug.log` (or to a tmp file under `${TMPDIR}` when state dir is absent). One-line wrapper at the top of each hook. |
| `hooks/detect-state.sh` | 29 | `STATUS_OUTPUT=$("$CLI" status --hook-format 2>&1)` merges stderr into stdout. On the success branch (line 32) `$STATUS_OUTPUT` is echoed verbatim into the Claude session context. Any `uv` resolution log, deprecation warning, or stderr noise from the CLI bleeds into the model's view as part of the status line. | Drop `2>&1`. Capture stdout only on the success branch; capture stderr separately for the diagnostic-fallback branch on lines 36-39. |
| `hooks/capture-evidence.sh`, `hooks/check-claim.sh`, `hooks/record-file-change.sh` | `STATE_DIR=".fakoli-state"` (capture-evidence:27, check-claim:17, record-file-change:14) | **Implicit assumption that hook cwd is the project root.** The hooks resolve state via a relative path. Claude Code hooks run with the user's project as cwd in the common case, but this is brittle if a hook is ever invoked from a subdirectory or via a non-standard runner. The plugin already uses `${CLAUDE_PLUGIN_ROOT}` for its own paths — adopt `${CLAUDE_PROJECT_DIR:-$PWD}` for the project-side path for symmetry and defence. | Replace `STATE_DIR=".fakoli-state"` with `STATE_DIR="${CLAUDE_PROJECT_DIR:-$PWD}/.fakoli-state"` in all three hooks. |
| `hooks/detect-state.sh` | 14-20 | **Language-detection logic depends on file lookup order, not on a priority.** Five separate `[ -f file ] && DETECTED_LANG=...` lines run sequentially; later matches overwrite earlier ones. For a polyglot project (e.g. Python `pyproject.toml` + JS tooling `package.json`), `DETECTED_LANG` ends up as `TypeScript` regardless of what the project's primary language is. Same logic exists in `fakoli-flow/hooks/detect-context.sh` per the comment, so consistency is preserved — but the result is silently wrong on common monorepos. | Either (a) emit a comma-joined list of detected languages, or (b) keep first-match-wins ordering by guarding each line with `[ "$DETECTED_LANG" = "unknown" ] && [ -f ... ]`. |

##### NIT
| File | Line(s) | Finding | Action |
|---|---|---|---|
| `hooks/record-file-change.sh` | 55-57 | Three `printf … \| sed -n 'Np'` invocations to split python3's three-line output. Each `sed` is a fork. Cheap, but bash can do it in-process with `IFS=$'\n' read -d '' -r FILE_PATH TOOL_NAME ACTOR <<< "$EXTRACTED"` (or a `mapfile`). | Replace the three `sed` calls with a single `read` / `mapfile` over the captured output. |
| `hooks/check-claim.sh` | 95 | `"$CLI" hook check-claim … >/dev/null \|\| true` — `>/dev/null` discards CLI stdout. The header comment at 91-94 acknowledges the contract is "warnings on stderr, never stdout" — but if that contract is ever loosened (e.g. a future CLI returns a structured JSON warning on stdout for the IDE), this hook silently drops it. Worth a comment cross-referencing the CLI contract. | Add an inline comment pointing to the CLI subcommand contract docs (or the test that locks it in). |
| `hooks/capture-evidence.sh` | 119-128 | The verification-command pattern list is hardcoded with a `case … esac` block. The comment at 116-118 already flags "Phase 6+ moves this to config" — so this is a known follow-up. Mentioning it here so it doesn't slip. | Track as a deferred config-driven matcher; no change needed in this audit. |

#### Cross-cutting observations

- **Contract enforcement is uniformly disciplined.** All four scripts honor the non-blocking contract: no `set -e`, all CLI calls wrapped, all paths terminate in `exit 0`, all stdin reads guarded by `[ -t 0 ]`. The hook-test assertion mentioned in capture-evidence.sh:55-57 (Critic-4) shows the team has a prior history of catching contract drift. The infrastructure here is mature.
- **Performance budget is the dominant remaining risk.** Header comments declare a 200ms hot-path budget, but record-file-change.sh's fallback path can spawn 5-6 python3 processes and capture-evidence.sh's fallback path spawns 2. The author has already optimised capture-evidence.sh from 7 spawns → 1 (per the comment at lines 47-50, citing Greptile + Critic-1) — the same consolidation pattern should be applied to check-claim and to record-file-change's escape path. This is the single biggest lever for hot-event responsiveness.
- **Documentation gap, not implementation gap.** The non-blocking contract lives only in script-header comments. The README mentions hooks once (line 105) without describing their contract. A new maintainer reading only the README would not know that `set -e` is forbidden. Promoting the script comment to a short README block (or `hooks/README.md`) closes the loop and matches how `fakoli-crew` documents its hook contracts.
- **hooks.json is exemplary.** Wrapper structure correct, matchers narrow, timeout 5 set on every entry, every `${CLAUDE_PLUGIN_ROOT}` reference resolves to a file that exists, every event name is the canonical Claude Code spelling. Nothing to fix here.
- **No shebang, stdin, matcher, or quoting bugs found.** The portability rules are all satisfied.

#### Decisions
- **Severity choices given the detected contract:**
  - Non-blocking contract was confirmed via signal #3 (in-script comments + unconditional `exit 0` + wrapped CLI calls + 4/4 scripts conformant). Therefore the **absence** of `set -e` / `set -u` / `set -o pipefail` is **CORRECT** and was **not flagged** at any severity. The popular advice ("always `set -euo pipefail`") is wrong for this plugin and would silently break PreToolUse — see the critic-prompt contract-awareness rule.
  - No MUST FIX items: nothing in the suite breaks the contract, hangs on stdin, references a missing file, or contains a shell-injection vector. The hooks.json structure is fully valid. The bar for MUST FIX (per the critic prompt) is "blocks merge — contract violation, broken stdin contract, missing file referenced in hooks.json, shell injection, or event-name typo." None of those apply.
  - SHOULD FIX is reserved for items that will degrade production behaviour or maintainability without violating the contract: (1) check-claim's double python3 spawn, (2) record-file-change's quadruple `_escape_json` python3 spawns — both above the script's own declared 200ms budget on a hot event — and (3) the missing contract documentation, which will cause the contract to be silently broken by a future maintainer who hasn't read every script.
  - CONSIDER items are hardening opportunities (race-tolerance, debug logging, defensive `${CLAUDE_PROJECT_DIR}`) and small correctness concerns (language detection, stderr leakage) that don't move the needle on contract or runtime today.
  - NITs are style/efficiency micro-issues with no production impact.

## Per-critic detail: mcp-critic

#### Files reviewed
- plugins/fakoli-state/.mcp.json (9 lines)
- plugins/fakoli-state/bin/fakoli-state-mcp (24 lines, stdio wrapper)
- plugins/fakoli-state/bin/src/fakoli_state/mcp_server.py (1046 lines, 13 tools)
- plugins/fakoli-state/bin/src/fakoli_state/claims/manager.py (722 lines — verified ClaimError → ToolError translation surface)
- plugins/fakoli-state/bin/src/fakoli_state/claims/stale.py (head — verified `_reap_stale` swallow-safe contract)
- plugins/fakoli-state/bin/src/fakoli_state/state/backend.py (verified `PENDING_EVENT_ID`, `TransactionAborted`, `apply_event` contract)
- plugins/fakoli-state/bin/src/fakoli_state/state/sqlite.py (verified `list_tasks(status=str)` accepts raw strings)
- plugins/fakoli-state/bin/src/fakoli_state/state/models.py (verified `TaskStatus` is a `StrEnum`)
- plugins/fakoli-state/bin/src/fakoli_state/context/packets.py (verified `render_packet` returns `WorkPacket(markdown, json_data)`)

#### Tools enumerated
| # | Tool name | Mutating? | Has actor param? | Description string? | Typed params? | Structured errors? |
|---|---|---|---|---|---|---|
| 1 | get_project_summary | no | n/a | yes (docstring) | yes (no params) | yes (ToolError) |
| 2 | list_tasks | no | n/a | yes (docstring) | partial — `status: str` not `Literal[...]` | yes (ToolError on init failure) |
| 3 | get_task | no | n/a | yes (docstring) | yes | yes (ToolError) |
| 4 | get_next_task | no | n/a (read-only) | yes (docstring) | yes — but `actor` accepted and ignored | yes (ToolError) |
| 5 | claim_task | yes | yes (`claimed_by: str`, required) | yes (docstring) | partial — `lease_duration_seconds: int` not validated >0 | yes (ToolError) |
| 6 | release_task | yes | yes (`actor: str`, required) | yes (docstring) | yes | yes (ToolError) |
| 7 | renew_claim | yes | yes (`actor: str`, required) | yes (docstring) | partial — `extend_seconds: int` not validated >0 | yes (ToolError) |
| 8 | generate_work_packet | no (read-only render) | n/a | yes (docstring) | yes (`Literal["markdown","json"]`) | yes (ToolError) |
| 9 | submit_progress | yes | yes (`actor: str`, required) | yes (docstring) | yes — but `actor` non-empty not enforced, no claim-owner check | yes (ToolError) |
| 10 | submit_completion_evidence | yes | yes (`actor: str`, required) + owner check | yes (docstring) | yes | yes (ToolError, incl. claim-owner guard) |
| 11 | check_conflicts | no | n/a | yes (docstring) | yes | yes (ToolError on init failure) |
| 12 | get_dependency_graph | no | n/a | yes (docstring) | yes (`Literal["all","feature","task"]`) | yes (ToolError) |
| 13 | update_task_status | yes | yes (`actor: str`, required) | yes (docstring) | yes (`Literal["drafted","ready","blocked","in_progress"]`) | yes (ToolError) |

All 13 tools listed.

#### Summary
0 MUST FIX | 4 SHOULD FIX | 3 CONSIDER | 2 NIT

#### Findings

##### MUST FIX
_(none)_

The manifest is schema-valid and portable (`${CLAUDE_PLUGIN_ROOT}` used). Every tool has a docstring. Mutating tools all require a `claimed_by`/`actor` parameter. Error returns are routed through `ToolError(str(exc))` — no `repr()` leaks, no exception objects in returns. The wrapper does not export or print secrets. No `${VAR}` references exist in `.mcp.json` so no env-var documentation gap. Tool 10's claim-owner guard (lines 774-778) closes the prior P1 actor-spoofing hole called out in `critic-PR#45-P1`.

##### SHOULD FIX
| File | Line(s) | Finding | Action |
|---|---|---|---|
| bin/src/fakoli_state/mcp_server.py | 459-464 (`claim_task`), 526-530 (`release_task`), 572-576 (`renew_claim`), 679-684 (`submit_progress`), 733-741 (`submit_completion_evidence`), 972-977 (`update_task_status`) | Mutating tools accept the actor parameter as a plain `str` with no non-empty validation. The mcp-critic rubric explicitly forbids `claimed_by=""` writing into the audit trail. Today, `claim_task(task_id="T001", claimed_by="")` would call `ClaimManager(..., actor="")` and persist an empty actor on every emitted event (`claim.created`, `progress.noted`, `evidence.submitted`, `task.status_changed`, `claim.released`, `claim.renewed`). At 3am the forensics trail names nobody. Add an explicit guard at the top of each mutating tool: `if not actor.strip(): raise ToolError("actor is required and must be a non-empty agent identifier")` (and equivalently for `claimed_by` in `claim_task`). The persona spec gives this exact pattern verbatim. | Add a single helper `_require_actor(name: str, field: str = "actor") -> str` near `_resolve_state_dir` that strips and raises, then call it as the first line of every mutating tool body. |
| bin/src/fakoli_state/mcp_server.py | 327-332 (`list_tasks` signature) | `status: str \| None = None` is not constrained — a caller passing `status="in-progress"` (hyphen instead of underscore) or `status="DONE"` gets a silently empty list back because the SQL filter on `sqlite.list_tasks` matches zero rows. Claude has no way to discover the legal enum values from the tool schema. Replace `str` with `Literal["proposed","drafted","reviewed","ready","claimed","in_progress","blocked","needs_review","accepted","done","rejected"] \| None`. This matches the `TaskCountsByStatus` fields verbatim and pulls the legal set into the MCP schema where Claude reads it. | Tighten the type annotation; also consider adding the same `Literal` to `update_task_status.from_status` semantics (currently only the target is `Literal`, the source is whatever the DB returns). |
| bin/src/fakoli_state/mcp_server.py | 327-352 (`list_tasks` return), 360-376 (`get_task` return), 384-450 (`get_next_task` return) | Return type is `list[dict[str, Any]]` / `dict[str, Any]` / `dict[str, Any] \| None`. The persona checklist treats `dict[str, Any]` as SHOULD FIX because it strips the field-level schema from Claude's view (no priority enum, no status enum, no nested `scores` shape). Every other tool in the file returns a typed Pydantic model — only the three Task-shaped tools fall back to `dict`. Define a `TaskSummary` (or reuse the existing `Task` Pydantic model from `state.models`) and return that. The `json.loads(t.model_dump_json())` round-trip on lines 350, 374, 448 is also a wasted serialization-and-reparse — returning the Pydantic model directly lets FastMCP do one serialization at the JSON-RPC boundary. | Switch returns to typed Pydantic models; drop the `json.loads(model_dump_json())` shim. |
| bin/src/fakoli_state/mcp_server.py | 384-389 (`get_next_task`) | `actor` parameter is accepted but never used. The docstring promises "highest-priority ready task" with no per-actor scoping, and the body never references `actor`. This is a contract lie — Claude will pass an `actor` value believing it influences the result. Either (a) drop the parameter entirely (read-only, no need for actor), or (b) use it as a tiebreaker (e.g. skip tasks whose `agent_assignment` field disagrees with the caller). The persona checklist explicitly flags actor-on-read-only-tools as a SHOULD FIX when no rationale exists. | Remove the `actor` parameter from the signature; downstream callers can keep the same observed behavior. |

##### CONSIDER
| File | Line(s) | Finding | Action |
|---|---|---|---|
| bin/src/fakoli_state/mcp_server.py | 215-221 (`_resolve_state_dir`) + 1-17 (module docstring) | The server fixes its working directory at startup via the `bin/fakoli-state-mcp` wrapper (`cd "$ORIGINAL_PWD"` before `exec`). Every tool re-resolves `Path.cwd()` per call. This works today but creates a subtle invariant: if any tool ever calls `os.chdir()` (now or in a future maintenance change) every subsequent tool silently addresses a different project's state DB. Capture the resolved state directory once at import time as `_STATE_DIR = Path.cwd().resolve() / _STATE_DIR_NAME`, then have `_resolve_state_dir()` return that captured value. This makes the invariant explicit and immune to future cwd drift. | Cache `_STATE_DIR` at module import; helpers just return the cached path. |
| bin/src/fakoli_state/mcp_server.py | 249-257 (`_reap_stale`) | Stale-claim reaping wraps the entire detector in `except Exception: pass`. The comment says "best-effort (never block)" but the `# noqa: BLE001` silences the linter that would normally catch this. If `detect_and_release_stale` ever starts raising on a corrupted DB, every mutating tool silently proceeds against a stale state. Log the swallowed exception at WARNING so it surfaces in `claude --debug` traces — `logger.warning("stale-claim reaping failed: %s", exc)` — without changing the swallow contract. | Add a single `logger.warning` inside the except block; keep the swallow. |
| bin/src/fakoli_state/mcp_server.py | 105-112 (`WorkPacketResponse`) | The `content: Any` field is marked with an inline comment ("str for markdown, dict for json"). The persona explicitly allows `Any` with a comment, so this is not a SHOULD FIX, but `content: str \| dict[str, Any]` would let Pydantic and Claude see the actual union without sacrificing the comment. Same effect, narrower schema. | Switch `Any` → `str \| dict[str, Any]`. |

##### NIT
| File | Line(s) | Finding | Action |
|---|---|---|---|
| bin/src/fakoli_state/mcp_server.py | 350, 374, 448 | `json.loads(t.model_dump_json())` round-trips the same data through JSON twice (once to serialize, once to reparse) before FastMCP serializes it a third time. Either return the Pydantic model directly (preferred — see SHOULD FIX #3) or use `model_dump(mode="json")` which produces the dict in one pass without re-parsing a string. | Replace `json.loads(t.model_dump_json())` with `t.model_dump(mode="json")` or, better, return the model. |
| bin/src/fakoli_state/mcp_server.py | 162-169 (`DependencyEdge`) + 940-945 | `DependencyEdge` uses `Field(alias="from")` to handle `from`/`to` being Python keywords/builtins, and the call site constructs it with `**{"from": dep_id, "to": t.id}` to dodge the keyword issue. This works but is the kind of construct a future reader will trip over. A one-line comment at the construction site naming the alias workaround (or using `DependencyEdge.model_validate({"from": dep_id, "to": t.id})` which is more obviously schema-validation rather than splat-args) would age better. | Drop in a comment or switch to `model_validate({...})`. |

#### Cross-cutting observations

- **Manifest is correct and portable.** `.mcp.json` is 9 lines of clean JSON: explicit `type: stdio`, `command: bash`, `args` is an array, the only path is wrapped in `${CLAUDE_PLUGIN_ROOT}`. The wrapper at `bin/fakoli-state-mcp` is executable (`-rwxr-xr-x`), preserves `ORIGINAL_PWD` before `cd`, and resolves its own dir via `dirname "${BASH_SOURCE[0]}"` — fully portable across install locations. No env-var documentation gap because no `${VAR}` references exist in the manifest or wrapper.
- **No secret-leak surface.** The server uses no API keys, no auth headers, no connection strings. All persistence is local SQLite. There is nothing to leak. The persona's secret-leak checklist is structurally inapplicable here, which is itself a positive design choice — the MCP surface is local-only and operates on file-system state.
- **Actor identification is consistent across mutations.** All six mutating tools (`claim_task`, `release_task`, `renew_claim`, `submit_progress`, `submit_completion_evidence`, `update_task_status`) require an actor parameter. The single residual gap is non-empty validation (SHOULD FIX #1) — the persona's rubric calls out exactly this as the boundary between MUST and SHOULD: required but unvalidated is SHOULD FIX, optional-with-empty-default would be MUST FIX. Tool 10's claim-owner verification (lines 774-778) is the model the other mutating tools should aspire to.
- **Error returns are structurally clean.** Every catch in the file translates to `ToolError(str(exc))` and the source exceptions (`ClaimError`, `TransactionAborted`) carry curated human-readable messages, not stack frames or `repr()` output. The `_reap_stale` swallow is the only place exceptions die silently and it is bounded to a known best-effort routine (CONSIDER #2 covers the visibility gap).
- **Read-only listers correctly skip reaping** per the module docstring contract (lines 11-15) — `list_tasks`, `get_task`, `get_next_task`, `check_conflicts`, `get_dependency_graph` are reap-free for latency. `update_task_status`, every claim tool, and `get_project_summary` reap first. This bifurcation is documented and consistent.
- **Tool 4 (`get_next_task`) is the only contract lie** — it advertises an `actor` parameter that the body never reads. Every other tool's signature reflects its actual behavior.

#### Decisions

- **Verdict: PASS.** Zero MUST FIX findings. The MCP integration is launch-ready as a contract surface: the manifest installs correctly anywhere, every tool has an honest description and typed parameters, errors return structured `ToolError` messages without leaking internal state, and mutating tools all name a responsible actor.
- **Priority order for the SHOULD FIX items** (recommended for the next welder pass before publishing): (1) add `_require_actor` non-empty guard to all six mutating tools — this is the single forensics-critical hardening left, (2) tighten `list_tasks.status` to `Literal[...]` so typos surface as schema errors instead of empty lists, (3) drop the unused `actor` parameter from `get_next_task` so the schema stops lying, (4) return typed Pydantic models from the three task-shaped read tools.
- **CONSIDER items are quality-of-life**, not contract issues. Capturing `_STATE_DIR` at import and logging the `_reap_stale` swallow both make latent bugs easier to discover three months from now.
- **No changes were made.** This is a read-only audit per the persona contract.

## Per-critic detail: structure-critic

#### Files reviewed
- .claude-plugin/plugin.json
- bin/pyproject.toml
- bin/src/fakoli_state/__init__.py
- README.md
- CHANGELOG.md
- /.claude-plugin/marketplace.json (root)
- /registry/index.json (root)
- /registry/categories.json (root)

#### Version-sync check (4 sources, must all read 1.9.0)
| Source | Version | Status |
|---|---|---|
| plugin.json | 1.9.0 | PASS |
| pyproject.toml | 1.9.0 | PASS |
| __init__.py | 1.9.0 | PASS |
| marketplace.json (fakoli-state entry) | 1.9.0 | PASS |

Bonus syncs (out of the 4 required, but also checked):
- registry/index.json fakoli-state entry: `1.9.0` — PASS
- registry/categories.json fakoli-state entry: `1.9.0` — PASS
- README version badge: `version-1.9.0` — PASS
- CHANGELOG latest dated heading: `## [1.9.0] — 2026-05-25` — PASS

All seven version-bearing surfaces agree on `1.9.0`. No drift.

#### README surface table accuracy
| Type | README claim | Actual count | Status |
|---|---|---|---|
| agents | 6 (rows in "Plugin-owned agents" table: planner, critic, sentinel, state-keeper, marketplace-scribe, docs-scribe) | 6 (critic.md, docs-scribe.md, marketplace-scribe.md, planner.md, sentinel.md, state-keeper.md) | PASS |
| skills | 8 enumerated in "Component responsibilities" table ("brainstorm, prd, plan, claim, execute, verify, finish, state-ops") | 7 on disk (brainstorm, claim, execute, finish, plan, prd, state-ops) | **FAIL — `verify` listed but does not exist** |
| hooks | not surface-tabled (generic line only); README does not state a count | 4 (capture-evidence.sh, check-claim.sh, detect-state.sh, record-file-change.sh) | n/a (no claim to compare against — SHOULD FIX considered below) |
| commands | n/a — README makes no command claims | 0 | PASS |

#### Summary
1 MUST FIX | 4 SHOULD FIX | 3 CONSIDER | 1 NIT

#### Findings

##### MUST FIX
| File | Line(s) | Finding | Action |
|---|---|---|---|
| `plugins/fakoli-state/README.md` | 103 | Skills row of the "Component responsibilities" table lists 8 skills, including a `verify` skill that does NOT exist on disk. `ls plugins/fakoli-state/skills/` returns only 7 (brainstorm, claim, execute, finish, plan, prd, state-ops). README **overpromises** a capability — a user reading the table will expect to invoke `skills/verify` and find nothing. Per the rubric, overpromise is MUST FIX. Note: verification is delegated to `fakoli-flow:verify` / `fakoli-crew:sentinel`, so the right framing is to drop `verify` from the row and (if desired) add a one-liner explaining that verification is delegated. Suggested replacement: `\| Skills \| Workflow choreography: brainstorm, prd, plan, claim, execute, finish, state-ops \|` |

##### SHOULD FIX
| File | Line(s) | Finding | Action |
|---|---|---|---|
| `plugins/fakoli-state/README.md` | 37–48 | Installation section opens with "fakoli-state is not yet in the marketplace. Clone from the monorepo and wire the plugin manually" but the root `.claude-plugin/marketplace.json` DOES contain the fakoli-state entry at v1.9.0 (lines 83–88 of marketplace.json). The README install instructions are stale — the plugin IS published in the in-repo marketplace and users should install it the normal way. Replace the manual-clone paragraph with the standard `/plugin marketplace add fakoli/fakoli-plugins && /plugin install fakoli-state@fakoli-plugins` flow, keeping the manual-clone path as a fallback. |
| `plugins/fakoli-state/CHANGELOG.md` | 7–14 | `## [Unreleased]` opens with a sentence that summarises v1.9.0 in past tense ("v1.9.0 closes the Phase 8 audit-honesty deferrals…") even though v1.9.0 has its own dated section at line 18. After a tag is cut, `[Unreleased]` should not narrate the just-shipped release — that content already lives under `## [1.9.0] — 2026-05-25`. Keep the v2.0 / v2.x forward-looking sentences (lines 11–14) since those are genuinely unreleased. Trim the leading "v1.9.0 closes…" sentence so the section reads as forward-looking only. |
| `plugins/fakoli-state/.gitignore` | 7 | Plugin `.gitignore` covers `bin/.pytest_cache/` but the plugin root itself contains a `.pytest_cache/` directory (visible in `ls -la` of the plugin root). The root `.pytest_cache/` is not ignored by the plugin's own gitignore — it relies on the repo-root `.gitignore` to catch it. Add `.pytest_cache/` (no `bin/` prefix) to the plugin's `.gitignore` so the rule is local and survives a future repo split. |
| `plugins/fakoli-state/README.md` | 17, 39, 49, 190 | README is internally inconsistent about install method. Line 17 says the plugin "ships a CLI…and a set of skills and hooks"; line 39 says "fakoli-state is not yet in the marketplace"; line 49 says "Once published, install via `/plugin install fakoli-state`"; line 190 says "claude plugin install fakoli-state # once published". All four phrasings predate the marketplace listing and need a single coherent message. Settle on one install story (it's published — use `/plugin install fakoli-state@fakoli-plugins`) and replace every `once published` qualifier accordingly. |

##### CONSIDER
| File | Line(s) | Finding | Action |
|---|---|---|---|
| `plugins/fakoli-state/README.md` | (new section) | Add an explicit surface-count table near the top — "This plugin ships: 6 agents, 7 skills, 4 hooks, 0 commands, 1 CLI, 1 MCP server with 13 tools." Right now agent count is in a deep table (line 169), skill count is implicit in a freeform list (line 103), and hook count is only inferable by `ls hooks/`. A single header table makes the surface area scannable and gives the structure-critic a single line to verify on future audits. |
| `plugins/fakoli-state/CHANGELOG.md` | 9–14 | Forward-looking `[Unreleased]` notes name three v2.0 / v2.x items (LinearIssuesProvider, MondayBoardsProvider, webhook sync) without links to issues or PRs. Per Keep a Changelog convention, evidence links (PR / issue numbers) make the roadmap actionable. Suggest appending `(see docs/phase-9-backlog.md § "v2.x roadmap" — items LB-1 / MB-1 / WS-1)` or equivalent anchor link. |
| `plugins/fakoli-state/README.md` | 5–7 | Badge set is minimal (license, version, alpha status). Consider adding a CI status badge once the live-GitHub nightly workflow is publicly visible (`docs/live-tests.md` references the cron); and a test-count badge (`tests: 964`) since the CHANGELOG actively advertises the count. Both make release-readiness scannable from the README header. |

##### NIT
| File | Line(s) | Finding | Action |
|---|---|---|---|
| `plugins/fakoli-state/README.md` | 132 | Sentence reads "fakoli-state is built in 9 phases" immediately followed by "Phases 1–8 shipped in PRs #38–#49; Phase 9 (this release, v1.9.0) closes the audit-honesty deferrals from Phase 8". The "Phase 9 (this release, v1.9.0)" parenthetical will become stale the moment v1.10.0 ships. Replace with "Phase 9 shipped in v1.9.0" so the sentence is tense-stable across future releases. |

#### Cross-cutting observations

1. **Version discipline is exemplary.** Seven independent surfaces (plugin.json, pyproject.toml, `__init__.py`, marketplace.json, registry/index.json, registry/categories.json, README badge, CHANGELOG dated heading) all agree on `1.9.0`. The release pipeline behind this is clearly working — no drift in any direction. This is the structural-integrity bar other plugins in the marketplace should meet.

2. **Marketplace/registry mirror plugin.json byte-for-byte on the load-bearing fields.** `name`, `description`, `repository`, `version`, `author`, `license`, and `keywords` all match across plugin.json ↔ marketplace.json ↔ registry/index.json. The only marketplace.json field not in plugin.json is `source` (the relative path), which is correct — `source` is a marketplace-only concept.

3. **The README's "Plugin-owned agents" table at line 163–176 is the cleanest surface table in the audit.** Each row gives color, ownership, and the crew defer-to target. This format should be lifted into a fakoli-crew "structure-critic golden example" reference for other plugins to follow.

4. **CHANGELOG is dense and useful but the `[Unreleased]` discipline slipped.** This is a small lapse in an otherwise rigorous changelog — the dated v1.9.0 section is some of the best release notes in the marketplace (file-level granularity, migration notes, test-count delta). Tightening `[Unreleased]` to forward-looking-only after each cut is a 30-second discipline that's worth the rigor.

5. **The skill-count drift (`verify` listed but absent) is the single concrete release-blocker.** Everything else is polish. Once the `verify` row is removed from the Component responsibilities table, the plugin's structural surface is publish-ready.

6. **Dead-file hygiene at plugin root is acceptable but on the edge.** `.fakoli-state-build/` and `.pytest_cache/` live at plugin root and are covered by `.gitignore` (the build dir locally, pytest_cache via the repo-root `.gitignore`). They are not committed. Mentioning here only because future structure audits may want to confirm `git ls-files plugins/fakoli-state/ | grep -E '(\.fakoli-state-build|\.pytest_cache)'` returns empty.

#### Decisions

**Verdict: FAIL** (one MUST FIX item — README `verify` skill claim is an overpromise that must be removed before tag/publish). All other findings are SHOULD FIX or below; none are publish-blocking on their own.

Once the `verify` row is fixed in the README skills enumeration, the plugin's structural surface is in PASS shape. Recommend the structure-keeper or docs-scribe agent owner pick up the four SHOULD FIX items in a single follow-up patch (README install paragraph rewrite + CHANGELOG `[Unreleased]` trim + `.pytest_cache/` gitignore line + install-message consistency sweep) before cutting v1.10.0.

## Items applied this phase

- [#1] plugins/fakoli-state/agents/critic.md:26 — `allowed-tools:` → `tools:` rename so agent frontmatter is honored — fixed inline (commit pending)
- [#2] plugins/fakoli-state/agents/docs-scribe.md:67 — `allowed-tools:` → `tools:` rename so agent frontmatter is honored — fixed inline (commit pending)
- [#3] plugins/fakoli-state/agents/marketplace-scribe.md:67 — `allowed-tools:` → `tools:` rename so agent frontmatter is honored — fixed inline (commit pending)
- [#4] plugins/fakoli-state/agents/planner.md:44 — `allowed-tools:` → `tools:` rename so planner Iron Rule is enforceable — fixed inline (commit pending)
- [#5] plugins/fakoli-state/agents/state-keeper.md:45 — `allowed-tools:` → `tools:` rename so state-keeper Iron Rule is enforceable — fixed inline (commit pending)
- [#6] plugins/fakoli-state/skills/finish/SKILL.md:249–252 — dangling `/fakoli-state:sentinel` slash command replaced with `claude plugin list | grep -q "^fakoli-crew"` gate + explanation that sentinel is an agent surface dispatched via the agent mechanism, never via a slash command — fixed inline (commit pending)
- [#7] plugins/fakoli-state/skills/prd/SKILL.md:57–59 — added Step 1 overwrite gate (exists check + heading/line-count summary + `yes/no/save-as-backup` prompt) before opening `$EDITOR`, plus a parallel guard before re-parse in the Iterating section — fixed inline (commit pending)
- [#8] plugins/fakoli-state/README.md:103 — dropped non-existent `verify` skill from the Component responsibilities Skills row; corrected count to 7 skills and noted verification delegation to `fakoli-flow:verify` / `fakoli-crew:sentinel` — fixed inline (commit pending)

## Items deferred to Phase 11

### SHOULD FIX (25 items)
- [agent-critic, agents/critic.md:12-22] Only 1 `<example>` block; add 2 more (rubric floor 2, convention 3).
- [agent-critic, agents/planner.md:12-40] Only 2 `<example>` blocks; add third covering re-planning after PRD rejection.
- [agent-critic, agents/sentinel.md:12-19] Only 1 `<example>` block AND example lacks `<commentary>`; add 2 more + commentary to each.
- [agent-critic, agents/sentinel.md:23-28] `allowed-tools:` instead of `tools:` (graded SHOULD because tool list coincides with read-only needs).
- [agent-critic, agents/sentinel.md:1-103] 103 lines — at proportionality floor; missing Composition/Inputs/NOT/Status sections.
- [skill-critic, skills/execute/SKILL.md:8,15,256] Fuzzy detection for `fakoli-flow:execute` — no shell check.
- [skill-critic, skills/finish/SKILL.md:8,245-246] Fuzzy detection for `fakoli-flow:finish` — no shell gate.
- [skill-critic, skills/claim/SKILL.md:15,250] Fuzzy detection for `fakoli-flow` / `fakoli-crew:welder/scout` — no shell check.
- [skill-critic, skills/finish/SKILL.md:177-212] Fuzzy detection for sync provider availability in Step 5 — no explicit checks.
- [skill-critic, skills/finish/SKILL.md:247-252] Fuzzy detection for `fakoli-crew:sentinel` presence — no shell gate.
- [skill-critic, skills/brainstorm/SKILL.md:194-197] Fuzzy detection for LLM availability — no `test -n "$ANTHROPIC_API_KEY"` check.
- [skill-critic, skills/state-ops/SKILL.md:119-132] `fakoli-state conflicts` "pending" here but "available" in execute — phase tables drift.
- [skill-critic, skills/state-ops/SKILL.md:67-96] Steps 2/3 labeled "Phase 3 — pending" but `list`/`show` available everywhere else.
- [skill-critic, skills/state-ops/SKILL.md:1-4] Description is longest in plugin; weak trigger phrase missing real user phrasings.
- [hook-critic, hooks/check-claim.sh:36-59] Hot-path perf budget violation — double `python3` spawn (100-300ms vs 200ms budget).
- [hook-critic, hooks/record-file-change.sh:95-106] Hot-path perf budget violation — `_escape_json()` spawns 4 `python3` instances.
- [hook-critic, README.md / hooks/README.md] Non-blocking hook contract undocumented at plugin/doc level.
- [mcp-critic, bin/src/fakoli_state/mcp_server.py:459-464,526-530,572-576,679-684,733-741,972-977] All 6 mutating tools accept actor as plain `str` with no non-empty validation; empty actor persists into audit trail.
- [mcp-critic, bin/src/fakoli_state/mcp_server.py:327-332] `list_tasks.status: str` not constrained; typo returns silently empty list.
- [mcp-critic, bin/src/fakoli_state/mcp_server.py:327-352,360-376,384-450] 3 task-shaped read tools return `dict[str, Any]` — strips field-level schema.
- [mcp-critic, bin/src/fakoli_state/mcp_server.py:384-389] `get_next_task` accepts `actor` parameter but never uses it — contract lie.
- [structure-critic, README.md:37-48] Install section says "not yet in marketplace" but marketplace.json contains v1.9.0 entry.
- [structure-critic, CHANGELOG.md:7-14] `[Unreleased]` opens with past-tense v1.9.0 summary; content duplicates dated section.
- [structure-critic, .gitignore:7] Plugin `.gitignore` misses plugin-root `.pytest_cache/`; relies on repo-root.
- [structure-critic, README.md:17,39,49,190] Internally inconsistent install messaging — 4 different phrasings.

### CONSIDER (21 items)
- [agent-critic, agents/docs-scribe.md:1-366] 366 lines; ~60 lines duplicate marketplace-scribe and state-keeper composition prose.
- [agent-critic, agents/marketplace-scribe.md:1-308] Same composition duplication (lines 144-161, 292-308).
- [agent-critic, agents/state-keeper.md:1-293] Same duplication pattern (lines 94-107).
- [agent-critic, agents/planner.md:76-80] Composition mentions only `fakoli-crew:guido`; missing scout/critic acknowledgment.
- [skill-critic, All 7 SKILL.md] No `references/`, `examples/`, or `scripts/` subdirectories; bundles bloat SKILL.md bodies.
- [skill-critic, skills/brainstorm/SKILL.md:70-110] Six-question discipline explicit but stopping rule "material" is interpretive.
- [skill-critic, skills/plan/SKILL.md:107-137] Step 3 documents Phase 7 limitation as 4-step workflow buried in paragraph.
- [skill-critic, skills/prd/SKILL.md:76-84] Step 1 lacks explicit one-question-per-message discipline (weaker than brainstorm).
- [skill-critic, skills/execute/SKILL.md:70-84] Step 2 abort flow happens after packet fetch; dishonest agent skips it.
- [skill-critic, skills/finish/SKILL.md:109-116] `--reason` requirement for `apply --reject` buried in prose.
- [hook-critic, hooks/capture-evidence.sh:232 + hooks/record-file-change.sh:113] Race-prone append on shared files (events.jsonl, orphan.json).
- [hook-critic, All four .sh files] No diagnostic fallback when hook silently fails; no production trail at 3am.
- [hook-critic, hooks/detect-state.sh:29] `$("$CLI" status --hook-format 2>&1)` merges stderr into status line shown to Claude.
- [hook-critic, hooks/capture-evidence.sh + check-claim.sh + record-file-change.sh:STATE_DIR] Implicit assumption hook cwd is project root via relative path.
- [hook-critic, hooks/detect-state.sh:14-20] Language detection sequential overwrites — last match wins; polyglot projects mislabeled.
- [mcp-critic, bin/src/fakoli_state/mcp_server.py:215-221,1-17] `_resolve_state_dir` re-resolves `Path.cwd()` per call; future `os.chdir()` would silently address different project.
- [mcp-critic, bin/src/fakoli_state/mcp_server.py:249-257] `_reap_stale` swallows all exceptions silently; no surfacing in debug traces.
- [mcp-critic, bin/src/fakoli_state/mcp_server.py:105-112] `WorkPacketResponse.content: Any` allowed but narrower union possible (`str | dict[str, Any]`).
- [structure-critic, README.md:new section] No top-level surface-count table; counts scattered across deep tables.
- [structure-critic, CHANGELOG.md:9-14] Forward-looking v2.x items name LinearIssuesProvider/MondayBoardsProvider/webhooks without issue/PR links.
- [structure-critic, README.md:5-7] Minimal badge set (license, version, alpha); no CI / test-count badges.

### NIT (11 items)
- [agent-critic, agents/sentinel.md:103] Missing trailing newline.
- [skill-critic, skills/brainstorm/SKILL.md:228] Phase 7 Notes table cell has `\|` escape that may render literally.
- [skill-critic, skills/claim/SKILL.md:138-143] Example ISO timestamp drifts vs execute/SKILL.md:121.
- [skill-critic, skills/state-ops/SKILL.md:22] "State-ops is NOT for" sentence repeats 4x in one paragraph.
- [skill-critic, skills/prd/SKILL.md:200-215] Phase 3 Limitations section duplicates content at lines 39-46.
- [hook-critic, hooks/record-file-change.sh:55-57] Three `printf | sed -n 'Np'` invocations; each `sed` is a fork.
- [hook-critic, hooks/check-claim.sh:95] `>/dev/null || true` discards CLI stdout; future structured JSON warning silently dropped.
- [hook-critic, hooks/capture-evidence.sh:119-128] Hardcoded verification-command pattern list; Phase 6+ TODO already flagged.
- [mcp-critic, bin/src/fakoli_state/mcp_server.py:350,374,448] `json.loads(t.model_dump_json())` triple-roundtrips data through JSON.
- [mcp-critic, bin/src/fakoli_state/mcp_server.py:162-169,940-945] `DependencyEdge` constructed with `**{"from": ...}` splat to dodge keyword; future reader will trip.
- [structure-critic, README.md:132] "Phase 9 (this release, v1.9.0)" parenthetical will stale on v1.10.0 ship.
