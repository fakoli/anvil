> ## Archived — see [roadmap.md](roadmap.md) for live planning
>
> This file is preserved as a historical audit trail of what was deferred at the
> close of Phase 10 (the plugin-audit pass). The live forward-planning source of
> truth has moved to [`docs/roadmap.md`](roadmap.md) (version × theme organized;
> evolves continuously).
>
> Every deferred item below (P11-XX-XN, 56 live) has been re-homed in the roadmap
> with its original ID preserved. The 7 cross-cutting themes are also carried
> over. Use this file only to understand the Phase 10 audit context and per-critic
> grouping. For "what's planned next", read `roadmap.md`.

# Phase 11 backlog (and beyond)

**Source:** [Phase 10 audit](audits/2026-05-26-plugin-audit.md)
**Audit date:** 2026-05-26
**Auditors:** agent-critic, skill-critic, hook-critic, mcp-critic, structure-critic (fakoli-crew v2.1.0)
**Subject:** anvil v1.9.0 (post Phase 10 MUST-FIX welder pass)
**Companion:** [phase-9-backlog.md](phase-9-backlog.md) (v2.x roadmap + earlier carry-forward)

This file catalogs the 57 deferred items from the Phase 10 plugin audit — every
finding the five critics raised at SHOULD FIX, CONSIDER, or NIT severity. The
8 MUST FIX items are closed under [Phase 10 T2–T6 welder
tasks](plans/2026-05-26-phase-10-plugin-audit.md) and are not duplicated here.

**Update (Phase 10 release prep, 2026-05-26):** 1 item from the catalog has been
closed as a bonus during Phase 10 — see P11-SK-S5 below (`[CLOSED in Phase 10
Fix #6]`). Net live count: **56 deferred** (24 SHOULD FIX | 21 CONSIDER | 11 NIT).
The summary tables below reflect the original 57-item audit baseline for
provenance; the live count is tracked here.

**Status legend**: items default to `Phase 11` unless explicitly deferred further.
Cross-cutting batch fixes are surfaced under [Cross-cutting themes](#cross-cutting-themes-high-leverage) below.

---

## Summary

**57 deferred items total: 25 SHOULD FIX | 21 CONSIDER | 11 NIT.**

| Critic | SHOULD FIX | CONSIDER | NIT | Total |
|---|---|---|---|---|
| agent-critic | 5 | 4 | 1 | 10 |
| skill-critic | 9 | 6 | 4 | 19 |
| hook-critic | 3 | 5 | 3 | 11 |
| mcp-critic | 4 | 3 | 2 | 9 |
| structure-critic | 4 | 3 | 1 | 8 |
| **Totals** | **25** | **21** | **11** | **57** |

Per-critic detail anchors in the source audit:
[agent-critic](audits/2026-05-26-plugin-audit.md#per-critic-detail-agent-critic) ·
[skill-critic](audits/2026-05-26-plugin-audit.md#per-critic-detail-skill-critic) ·
[hook-critic](audits/2026-05-26-plugin-audit.md#per-critic-detail-hook-critic) ·
[mcp-critic](audits/2026-05-26-plugin-audit.md#per-critic-detail-mcp-critic) ·
[structure-critic](audits/2026-05-26-plugin-audit.md#per-critic-detail-structure-critic).

---

## Suggested Phase 11 scope

Two viable execution shapes — user picks:

**Option A — five sub-phases (one per critic).** Each critic's deferred set
becomes its own welder pass with cohesive expertise. The skill-critic batch
(19 items) is largest and benefits most from concentration; the structure-critic
batch (8 items) is the cheapest standalone closeout. Total: 5 sub-phases.

**Option B — single Phase 11 with welder fan-out.** One planner pass scopes all
57 items into one Phase 11 plan; planner dispatches up to 5 welders in parallel,
each scoped to one critic's findings. Total: 1 phase, ~5 parallel welder tasks.

**Recommended:** Option B with carve-outs. The four "no-fuzzy-detection" SHOULD
FIX items (skill-critic) and the six "non-empty actor" SHOULD FIX items (mcp-critic)
are mechanical copy-paste fixes that one welder can close in a single hour. The
remaining work has more design surface and benefits from focused passes. See
[Cross-cutting themes](#cross-cutting-themes-high-leverage) for batching opportunities.

All NIT items can be picked up opportunistically by any welder touching the
file for a higher-severity reason — no dedicated pass needed.

---

## Items by critic

### agent-critic deferred items

#### SHOULD FIX
- **P11-AG-S1** · `agents/critic.md:12-22` — Description has only 1 `<example>` block (rubric floor 2; convention 3). **Action:** add 2 more `<example>` blocks: one for fakoli-crew fallback path, one for SHOULD-FIX-only verdict. Each needs its own `<commentary>`. **Target:** Phase 11.
- **P11-AG-S2** · `agents/planner.md:12-40` — Description has only 2 `<example>` blocks; below 3-example fakoli-crew convention. **Action:** add a third example covering re-planning after PRD rejection / incremental conflict. **Target:** Phase 11.
- **P11-AG-S3** · `agents/sentinel.md:12-19` — Description has only 1 `<example>` block AND example lacks `<commentary>`. **Action:** add 2 more examples + `<commentary>` to every example covering (a) standalone fallback, (b) merge-with-fakoli-crew:sentinel scorecards, (c) FAIL path with conflicting evidence. **Target:** Phase 11.
- **P11-AG-S4** · `agents/sentinel.md:23-28` — `allowed-tools:` used instead of `tools:` (graded SHOULD because tool list coincides with read-only needs). **Action:** rename key to `tools:`. **Target:** Phase 11. Reviewer may upgrade to MUST FIX for systemic consistency with the five Phase 10 MUST FIX agents.
- **P11-AG-S5** · `agents/sentinel.md:1-103` — File is 103 lines — at proportionality floor; missing Composition, Inputs, "NOT" boundary, Status File Output sections. **Action:** expand to ~140-180 lines mirroring critic.md structure. **Target:** Phase 11.

#### CONSIDER
- **P11-AG-C1** · `agents/docs-scribe.md:1-366` — 366 lines (near 400 ceiling); ~60 lines duplicate marketplace-scribe.md and state-keeper.md composition prose. **Action:** extract shared "three doc/state specialists" composition into `docs/specs/internal-agents.md`; link from all three agents. **Target:** Phase 11 (batch with C2/C3).
- **P11-AG-C2** · `agents/marketplace-scribe.md:1-308` — Same composition duplication (lines 144-161, 292-308). **Action:** same as C1. **Target:** Phase 11.
- **P11-AG-C3** · `agents/state-keeper.md:1-293` — Same duplication pattern (lines 94-107 restate fakoli-crew composition). **Action:** same as C1. **Target:** Phase 11.
- **P11-AG-C4** · `agents/planner.md:76-80` — Composition mentions only `fakoli-crew:guido` as defer-to; missing scout/critic acknowledgment. **Action:** add a one-line note acknowledging scout for codebase recon OR explicit "out of planner's responsibility". **Target:** Phase 11.

#### NIT
- **P11-AG-N1** · `agents/sentinel.md:103` — Missing trailing newline. **Action:** add trailing newline. **Target:** Phase 11 (drive-by during P11-AG-S3/S4/S5).

### skill-critic deferred items

#### SHOULD FIX
- **P11-SK-S1** · `skills/execute/SKILL.md:8,15,256` — Fuzzy detection for `fakoli-flow:execute` — no shell check. **Action:** add Step 0 `claude plugin list 2>/dev/null | grep -q "fakoli-flow"`; branch on exit code; document fall-through. Mirror start-prd/SKILL.md:48. **Target:** Phase 11 (batch with S2/S3/S4/S5/S6 — see no-fuzzy-detection theme). **Note:** the grep pattern must NOT use a leading `^` anchor — `claude plugin list` indents each row with `  ❯ ` so the anchored form never matches.
- **P11-SK-S2** · `skills/finish/SKILL.md:8,245-246` — Fuzzy detection for `fakoli-flow:finish`. **Action:** add explicit `claude plugin list 2>/dev/null | grep -q "fakoli-flow"` check at top of Step 1 (no `^` anchor — see S1 note). **Target:** Phase 11.
- **P11-SK-S3** · `skills/claim/SKILL.md:15,250` — Fuzzy detection for `fakoli-flow` and `fakoli-crew:welder/scout`. **Action:** wrap each "when X is installed" section in `claude plugin list 2>/dev/null | grep -q "fakoli-..."` check (no `^` anchor) or move prose to `references/composition.md`. **Target:** Phase 11.
- **P11-SK-S4** · `skills/finish/SKILL.md:177-212` — Fuzzy detection for sync provider availability — Step 5 prose-only "if configured" with no shell checks. **Action:** replace prose with `test -n "$GITHUB_REPOSITORY"`, `gh auth status >/dev/null 2>&1`, `anvil sync github --health`. **Target:** Phase 11.
- **P11-SK-S5** · `skills/finish/SKILL.md:247-252` — Fuzzy detection for `fakoli-crew:sentinel` — no shell check. **Action:** add `claude plugin list 2>/dev/null | grep -q "fakoli-crew"` gate. **Target:** ~~Phase 11~~ **[CLOSED in Phase 10 Fix #6]** — welder closed this as a bonus while fixing the dangling `/anvil:sentinel` slash-command reference at the same lines. The closure: removed the broken slash-command snippet entirely AND added the `claude plugin list 2>/dev/null | grep -q "fakoli-crew"` shell gate (mirroring start-prd/SKILL.md:48), with explicit branches for exit-0 (dispatch fakoli-crew:sentinel) vs non-zero (fall through to plugin-local sentinel agent). The PR-B fix-cycle subsequently dropped the `^` anchor from this pattern once it was discovered that `claude plugin list` indents each row (so `^fakoli-...` never matched). See `docs/plans/agent-welder-t11-status.md` § "Fix 6 approach" for the welder's full decision rationale.
- **P11-SK-S6** · `skills/start-prd/SKILL.md:194-197` — Fuzzy detection for LLM availability — no `test -n "$ANTHROPIC_API_KEY"` check. **Action:** add explicit shell check; document branches; or move to `references/llm-augmentation.md`. **Target:** Phase 11.
- **P11-SK-S7** · `skills/state-ops/SKILL.md:119-132` — Phase-availability tables contradict execute/SKILL.md — `anvil conflicts` "pending" here but "available" there. **Action:** reconcile phase tables across skills; consider single `references/phase-status.md`. **Target:** Phase 11 (batch with S8 — see phase-status-drift theme).
- **P11-SK-S8** · `skills/state-ops/SKILL.md:67-96` — Steps 2 and 3 labeled "Phase 3 — pending" but every other skill uses `list`/`show` as available. **Action:** update Steps 2/3/4/5 to "available" matching rest of plugin. **Target:** Phase 11.
- **P11-SK-S9** · `skills/state-ops/SKILL.md:1-4` — Description is longest in plugin (60+ words), weak trigger phrase; missing real user phrasings. **Action:** add concrete trigger phrases in quotes ("show project status", "list ready tasks", "what's blocking T012", "are there file conflicts", "show me active claims"); trim capability list. **Target:** Phase 11.

#### CONSIDER
- **P11-SK-C1** · All 7 `SKILL.md` files — No `references/`, `examples/`, or `scripts/` subdirectories; bodies bundle phase-status tables and composition prose. **Action:** extract to `references/phase-status.md`, `references/composition.md`, etc. Closes ~20-30% of SKILL.md body weight. **Target:** Phase 11 (high leverage — closes S7/S8 simultaneously).
- **P11-SK-C2** · `skills/start-prd/SKILL.md:70-110` — Six-question discipline explicit but stopping rule "material" is interpretive. **Action:** add concrete stopping rule — max 1 follow-up if under 5 words, never chain more than 8 total. **Target:** Phase 11.
- **P11-SK-C3** · `skills/plan/SKILL.md:107-137` — Step 3 documents Phase 7 limitation as 4-step workflow buried in paragraph. **Action:** promote to `### Step 3a — Author subtasks manually` block or extract to `references/manual-task-expansion.md`. **Target:** Phase 11.
- **P11-SK-C4** · `skills/prd/SKILL.md:76-84` — Step 1 lacks explicit one-question-per-message discipline (weaker than start-prd). **Action:** mirror start-prd — "Ask one question per message. Wait for the answer before asking the next." Bound the topic count. **Target:** Phase 11.
- **P11-SK-C5** · `skills/execute/SKILL.md:70-84` — Step 2 abort flow happens after packet fetch — dishonest agent skips it. **Action:** move Step 2 ahead of packet fetch OR merge into Step 1; make ambiguous-criteria detection a precondition for Step 3. **Target:** Phase 11.
- **P11-SK-C6** · `skills/finish/SKILL.md:109-116` — `--reason` requirement for `apply --reject` buried in prose. **Action:** promote requirement to callout at top of section. **Target:** Phase 11.

#### NIT
- **P11-SK-N1** · `skills/start-prd/SKILL.md:228` — Phase 7 Notes table cell has `\|` escape that may render literally. **Action:** use HTML entity `&#124;` in code span or rephrase. **Target:** Phase 11 (drive-by).
- **P11-SK-N2** · `skills/claim/SKILL.md:138-143` — Example ISO timestamp drifts vs execute/SKILL.md:121 (`2026-05-24` vs `2026-05-25`). **Action:** pick one wall-clock date or use placeholder `<ISO_TIMESTAMP>`. **Target:** Phase 11 (drive-by).
- **P11-SK-N3** · `skills/state-ops/SKILL.md:22` — "State-ops is NOT for" sentence repeats 4x in one paragraph. **Action:** format as bulleted "Do not use this skill for:" list. **Target:** Phase 11 (drive-by during P11-SK-S9).
- **P11-SK-N4** · `skills/prd/SKILL.md:200-215` — Phase 3 Limitations section duplicates content at lines 39-46. **Action:** delete one table or merge columns. **Target:** Phase 11 (drive-by).

### hook-critic deferred items

#### SHOULD FIX
- **P11-HK-S1** · `hooks/check-claim.sh:36-59` — Hot-path perf budget violation — spawns `python3` twice (100-300ms) on every Edit/Write/NotebookEdit; exceeds declared 200ms. **Action:** consolidate into single `python3 -c` printing both fields; mirror `record-file-change.sh:35-58` pattern. **Target:** Phase 11.
- **P11-HK-S2** · `hooks/record-file-change.sh:95-106` — Hot-path perf budget violation — `_escape_json()` spawns 4 `python3` instances on fallback path; 5-6 total spawns. **Action:** move JSON escaping into original extraction `python3` block (line 35); emit pre-escaped values. **Target:** Phase 11.
- **P11-HK-S3** · `README.md` / new `hooks/README.md` — Non-blocking contract undocumented at plugin/doc level; future maintainer will reintroduce `set -e`. **Action:** add hook-contract section to `hooks/README.md` (new) or `docs/hooks.md` (new) or README "Hooks" row. Single-paragraph copy: "All anvil hooks are non-blocking: must `exit 0` regardless of internal failure, must not use `set -e`/`set -u`/`set -o pipefail`, must wrap CLI calls with `|| true`, and must complete in < 200ms on hot events. SessionStart hook may take up to 1s." **Target:** Phase 11. High-leverage — single fix closes the documentation gap.

#### CONSIDER
- **P11-HK-C1** · `hooks/capture-evidence.sh:232` + `hooks/record-file-change.sh:113` — Race-prone append on shared files (`events.jsonl`, `orphan.json`); JSON records can exceed PIPE_BUF when STDOUT_EXCERPT is near MAX_EXCERPT=4000. **Action:** add `flock` guard OR document at-most-rare interleave and have replay tolerate truncation. **Target:** Phase 11 or deferred to v2.x sync-hardening pass.
- **P11-HK-C2** · All four `.sh` files (`2>/dev/null` throughout) — No diagnostic fallback when hook silently fails; no production trail at 3am. **Action:** support `ANVIL_HOOK_DEBUG=1` env var redirecting stderr to `.anvil/.hook-debug.log` (or `${TMPDIR}`). One-line wrapper at top of each hook. **Target:** Phase 11.
- **P11-HK-C3** · `hooks/detect-state.sh:29` — `$("$CLI" status --hook-format 2>&1)` merges stderr into status line shown to Claude. **Action:** drop `2>&1`; capture stdout only on success branch; capture stderr separately for diagnostic-fallback branch on lines 36-39. **Target:** Phase 11.
- **P11-HK-C4** · `hooks/capture-evidence.sh:27` + `hooks/check-claim.sh:17` + `hooks/record-file-change.sh:14` — Implicit assumption hook cwd is project root via relative `STATE_DIR=".anvil"`. **Action:** replace with `STATE_DIR="${CLAUDE_PROJECT_DIR:-$PWD}/.anvil"` in all three hooks. **Target:** Phase 11.
- **P11-HK-C5** · `hooks/detect-state.sh:14-20` — Language detection uses sequential overwrites — last match wins; polyglot projects mislabeled. **Action:** either emit comma-joined list OR guard each line with `[ "$DETECTED_LANG" = "unknown" ]`. **Target:** Phase 11. Note: same logic exists in `fakoli-flow/hooks/detect-context.sh` — consider cross-plugin coordination.

#### NIT
- **P11-HK-N1** · `hooks/record-file-change.sh:55-57` — Three `printf … | sed -n 'Np'` invocations; each `sed` is a fork. **Action:** replace with single `read` / `mapfile` over captured output. **Target:** Phase 11 (drive-by during P11-HK-S2).
- **P11-HK-N2** · `hooks/check-claim.sh:95` — `>/dev/null || true` discards CLI stdout; future structured JSON warning silently dropped. **Action:** add inline comment cross-referencing CLI subcommand contract docs. **Target:** Phase 11 (drive-by during P11-HK-S1).
- **P11-HK-N3** · `hooks/capture-evidence.sh:119-128` — Hardcoded verification-command pattern list; Phase 6+ TODO already flagged. **Action:** track as deferred config-driven matcher; no change in this audit. **Target:** deferred (v2.x or aligned with `tech-debt-backlog.md` CL-10).

### mcp-critic deferred items

#### SHOULD FIX
- **P11-MC-S1** · `bin/src/anvil/mcp_server.py:459-464,526-530,572-576,679-684,733-741,972-977` — All 6 mutating tools accept actor as plain `str` with no non-empty validation; empty actor persists into audit trail at every emitted event (`claim.created`, `progress.noted`, `evidence.submitted`, `task.status_changed`, `claim.released`, `claim.renewed`). **Action:** add `_require_actor(name: str, field: str = "actor") -> str` helper near `_resolve_state_dir`; call as first line of every mutating tool body. **Target:** Phase 11. Highest forensics-criticality; single helper closes 6 sites.
- **P11-MC-S2** · `bin/src/anvil/mcp_server.py:327-332` — `list_tasks.status: str | None` not constrained — typo like `"in-progress"` or `"DONE"` returns silently empty list. **Action:** replace with `Literal["proposed","drafted","reviewed","ready","claimed","in_progress","blocked","needs_review","accepted","done","rejected"] | None` matching `TaskCountsByStatus` fields verbatim. **Target:** Phase 11.
- **P11-MC-S3** · `bin/src/anvil/mcp_server.py:327-352,360-376,384-450` — Return type `list[dict[str, Any]]` / `dict[str, Any]` / `dict[str, Any] | None` strips field-level schema from Claude's view (3 task-shaped tools — `list_tasks`, `get_task`, `get_next_task`). **Action:** define `TaskSummary` or reuse `Task` Pydantic model from `state.models`; drop `json.loads(model_dump_json())` shim. Closes P11-MC-N1 simultaneously. **Target:** Phase 11.
- **P11-MC-S4** · `bin/src/anvil/mcp_server.py:384-389` — `get_next_task` accepts `actor` parameter but never uses it — contract lie. **Action:** remove `actor` from signature. **Target:** Phase 11. Trivial fix, schema-truthfulness win.

#### CONSIDER
- **P11-MC-C1** · `bin/src/anvil/mcp_server.py:215-221` + `1-17` — Every tool re-resolves `Path.cwd()` per call; future `os.chdir()` would silently address different project. **Action:** capture `_STATE_DIR` at module import as `_STATE_DIR = Path.cwd().resolve() / _STATE_DIR_NAME`; helpers return cached path. **Target:** Phase 11.
- **P11-MC-C2** · `bin/src/anvil/mcp_server.py:249-257` — `_reap_stale` swallows all exceptions silently; no surfacing in `claude --debug` traces. **Action:** add `logger.warning("stale-claim reaping failed: %s", exc)` inside except; keep swallow contract. **Target:** Phase 11.
- **P11-MC-C3** · `bin/src/anvil/mcp_server.py:105-112` — `WorkPacketResponse.content: Any` allowed but narrower union possible. **Action:** switch `Any` → `str | dict[str, Any]`. **Target:** Phase 11.

#### NIT
- **P11-MC-N1** · `bin/src/anvil/mcp_server.py:350,374,448` — `json.loads(t.model_dump_json())` triple-roundtrips data through JSON. **Action:** replace with `t.model_dump(mode="json")` or return Pydantic model directly. Closed automatically by P11-MC-S3. **Target:** Phase 11 (drive-by).
- **P11-MC-N2** · `bin/src/anvil/mcp_server.py:162-169` + `940-945` — `DependencyEdge` constructed with `**{"from": dep_id, "to": t.id}` splat to dodge keyword; future reader will trip. **Action:** add comment OR switch to `DependencyEdge.model_validate({"from": dep_id, "to": t.id})`. **Target:** Phase 11 (drive-by).

### structure-critic deferred items

#### SHOULD FIX
- **P11-ST-S1** · `README.md:37-48` — Install section says "not yet in marketplace" but root `.claude-plugin/marketplace.json` contains v1.9.0 entry (lines 83-88). **Action:** replace manual-clone paragraph with `/plugin marketplace add fakoli/fakoli-plugins && /plugin install anvil@fakoli-plugins` flow, keeping manual-clone as fallback. **Target:** Phase 11.
- **P11-ST-S2** · `CHANGELOG.md:7-14` — `[Unreleased]` opens with past-tense summary of v1.9.0; content already lives under dated section. **Action:** trim leading "v1.9.0 closes…" sentence; keep forward-looking v2.x notes (lines 11-14). **Target:** Phase 11.
- **P11-ST-S3** · `.gitignore:7` — Covers `bin/.pytest_cache/` but plugin-root `.pytest_cache/` not ignored locally; relies on repo-root `.gitignore`. **Action:** add `.pytest_cache/` (no `bin/` prefix) so rule is local and survives a future repo split. **Target:** Phase 11.
- **P11-ST-S4** · `README.md:17,39,49,190` — Internally inconsistent install messaging — 4 different phrasings about "once published". **Action:** settle on single install story (`/plugin install anvil@fakoli-plugins`); sweep all 4 sites. Batches with P11-ST-S1. **Target:** Phase 11.

#### CONSIDER
- **P11-ST-C1** · `README.md` (new section near top) — No top-level surface-count table; counts scattered across deep tables. **Action:** add header table: "ships 6 agents, 7 skills, 4 hooks, 0 commands, 1 CLI, 1 MCP server with 13 tools." **Target:** Phase 11.
- **P11-ST-C2** · `CHANGELOG.md:9-14` — Forward-looking v2.x items name LinearIssuesProvider/MondayBoardsProvider/webhooks without issue/PR links. **Action:** append `(see docs/phase-9-backlog.md § "v2.x roadmap" — P9B-1 / P9B-2 / P9B-5)` or equivalent anchor links. **Target:** Phase 11.
- **P11-ST-C3** · `README.md:5-7` — Minimal badge set (license, version, alpha); no CI / test-count badges. **Action:** add CI status badge (once live-GitHub nightly workflow public) and `tests: 967` count badge. **Target:** Phase 11.

#### NIT
- **P11-ST-N1** · `README.md:132` — "Phase 9 (this release, v1.9.0)" parenthetical will stale on v1.10.0 ship. **Action:** replace with "Phase 9 shipped in v1.9.0" for tense-stability. **Target:** Phase 11 (drive-by during P11-ST-S1/S4).

---

## Cross-cutting themes (high-leverage)

Several deferred items share a single underlying gap. Fixing the theme as a
batch is cheaper than item-by-item triage and produces stronger lockstep
consistency.

### Theme 1 — No-fuzzy-detection rule across skills
**Closes:** P11-SK-S1, P11-SK-S2, P11-SK-S3, P11-SK-S4, ~~P11-SK-S5~~, P11-SK-S6 (originally 6 SHOULD FIX items; **5 live** — P11-SK-S5 [CLOSED in Phase 10 Fix #6]).
**Pattern:** every skill that conditionally bridges to fakoli-flow, fakoli-crew, a sync provider, or LLM augmentation uses prose-only "when X is installed" framing without a `claude plugin list 2>/dev/null | grep -q "X"` shell check (no `^` anchor — `claude plugin list` indents each row, so the anchored form never matches). start-prd/SKILL.md:48 and finish/SKILL.md:249-254 are the reference implementations that every other skill should mirror.
**Welder effort:** ~3 lines per site, 5 sites — single welder pass, ~50 minutes.

### Theme 2 — Non-empty actor validation across MCP mutating tools
**Closes:** P11-MC-S1 (1 SHOULD FIX, 6 sites).
**Pattern:** all 6 mutating MCP tools accept `actor: str` / `claimed_by: str` with no `.strip()` non-empty check; empty actor persists into the audit trail.
**Fix shape:** single `_require_actor(name: str, field: str = "actor") -> str` helper near `_resolve_state_dir`; call as first line of every mutating tool body.
**Welder effort:** define helper + 6 one-liners — single welder pass, ~30 minutes.

### Theme 3 — Hot-path perf budget on hook scripts
**Closes:** P11-HK-S1, P11-HK-S2 (and P11-HK-N1 as drive-by).
**Pattern:** `check-claim.sh` spawns python3 twice; `record-file-change.sh` spawns python3 5-6 times. Each cold python3 spawn is 50-150ms; declared budget is 200ms on hot events. Pattern in `record-file-change.sh:35-58` already proves the consolidation works (1 spawn).
**Welder effort:** consolidate each script's extraction + escaping into single python3 round-trip — ~2 hours per script, 2 scripts.

### Theme 4 — Hook contract undocumented at plugin level
**Closes:** P11-HK-S3 (1 SHOULD FIX).
**Pattern:** the non-blocking contract lives only in script-header comments (3/4 hooks have identical "Rules: no set -e, no piped grep, always exit 0, complete in < 200ms" comments) but is absent from README and `docs/`. Future maintainer who hasn't read every script will reintroduce `set -e` and silently break PreToolUse.
**Fix shape:** new `hooks/README.md` OR `docs/hooks.md` section OR README "Hooks" row expansion. Single paragraph.
**Welder effort:** ~15 minutes.

### Theme 5 — Phase-status table drift across skills
**Closes:** P11-SK-S7, P11-SK-S8 (2 SHOULD FIX); enabled by P11-SK-C1 (extract to `references/`).
**Pattern:** `anvil conflicts` is "pending" in state-ops, "available" in execute. `list`/`show` are "pending" in state-ops Step 2/3 but available in every other skill. State-ops is the laggard; the plugin is at v1.9.0 / Phase 10 per the brief.
**Fix shape:** single source of truth at `references/phase-status.md` (or `docs/phase-status.md`); skills link to it instead of inlining tables.
**Welder effort:** create reference doc + update 7 SKILL.md references — ~2 hours.

### Theme 6 — Composition duplication across three doc/state agents
**Closes:** P11-AG-C1, P11-AG-C2, P11-AG-C3 (3 CONSIDER).
**Pattern:** docs-scribe, marketplace-scribe, and state-keeper each carry near-identical "three doc/state specialists inside anvil" composition tables. If the split ever changes, three files need to update in lockstep.
**Fix shape:** extract to `docs/specs/internal-agents.md`; agents link to it.
**Welder effort:** ~1 hour.

### Theme 7 — Install messaging drift in README + CHANGELOG
**Closes:** P11-ST-S1, P11-ST-S2, P11-ST-S4, P11-ST-N1 (3 SHOULD FIX + 1 NIT).
**Pattern:** README has 4 different phrasings about "once published"; CHANGELOG `[Unreleased]` narrates the just-shipped v1.9.0; README has stale "Phase 9 (this release)" parenthetical. All four are downstream effects of v1.9.0 shipping without a docs-scribe sweep.
**Fix shape:** single README sweep + CHANGELOG trim. Single docs-scribe pass.
**Welder effort:** ~1 hour.

---

## Carry-forward from earlier backlogs

Reviewed [`phase-9-backlog.md`](phase-9-backlog.md) for items still open after
Phase 9 → 10 work. The Phase 10 audit scope was **plugin-dev quality** (agents,
skills, hooks, MCP, structure) — it did not address the **v2.x roadmap** items
(P9B-1..P9B-9) or the carry-forward CL/TQ/PS items from PR #41.

### Open from phase-9-backlog.md (not addressed by Phase 10)
All items below remain `OPEN` or `TARGETED-V2.x` per phase-9-backlog.md and are
**not** subsumed by any Phase 11 deferred item:

| ID | Title | Status | Notes |
|---|---|---|---|
| P9B-1 | LinearIssuesProvider | OPEN, v2.0 | Sync-provider expansion |
| P9B-2 | MondayBoardsProvider | OPEN, v2.0 | Sync-provider expansion |
| P9B-3 | JiraIssuesProvider | OPEN, v2.1 | Sync-provider expansion |
| P9B-4 | GitHubProjectsProvider | OPEN, v2.1 | Sync-provider expansion |
| P9B-5 | Webhook-based sync | SPEC-FIRST, v2.0 | Needs design doc |
| P9B-6 | Immediate-apply `*_applied` resolution variants | TARGETED-V2.0 | Spec'd in agent-welder-honesty-status.md |
| P9B-7 | `anvil snapshot` subcommand | OPEN, v2.1 | sqlite3 .backup wrapper |
| P9B-8 | MCP sync tools surface | OPEN, v2.1 | 4 new MCP tools (sync_run/sync_health/sync_status/sync_reconcile) |
| P9B-9 | Provider config schemas in `config.yaml` | SPEC-FIRST, v2.0 | Co-required with P9B-1 |

### Open from tech-debt-backlog.md (cross-referenced via phase-9-backlog.md)
| ID | Source | Status |
|---|---|---|
| CL-1, CL-2, CL-3, CL-4, CL-5, CL-8, CL-10, CL-11, CL-12, CL-13 | PR #41 critics | OPEN |
| TQ-1, TQ-2, TQ-3, TQ-4, TQ-6, TQ-7, TQ-8 | PR #41 Critic-4 | OPEN |
| PS-1 | PR #41 Critic-2 | OPEN |

These items remain owned by `tech-debt-backlog.md` and are not duplicated into
Phase 11 scope unless a Phase 11 SHOULD FIX naturally touches the same file.
The MCP-critic Phase 10 finding P11-MC-C2 (silent `_reap_stale` swallow) is
adjacent to CL-3 (`_reap_stale_claims` bare except) — Phase 11 welder closing
P11-MC-C2 should consider also closing CL-3 in the same patch.

---

## Notes for Phase 11 planner

1. **Phase 10 closes 8 MUST FIX items.** Phase 11 picks up where T2-T6 of the Phase 10 plan stops. Do NOT re-grade audit items — the severity is fixed by the auditors' decisions documented in `audits/2026-05-26-plugin-audit.md`.
2. **Use Themes 1-7 as task boundaries.** Each theme is welder-sized and self-contained. Cross-theme dependencies are minimal: Theme 5 enables better fix shape for Themes 1 (skill SKILL.md hygiene) and 6 (composition extraction), but neither blocks Theme 5.
3. **NIT items are drive-by work.** Do not schedule dedicated NIT tasks. Any welder touching a file for a SHOULD/CONSIDER reason takes its NITs as part of the same patch.
4. **Severity upgrade candidate:** P11-AG-S4 (`agents/sentinel.md` `allowed-tools:` antipattern) could justifiably move from SHOULD FIX to MUST FIX for consistency with the 5 Phase 10 MUST FIX agent fixes. The auditor (agent-critic) graded it SHOULD because sentinel's tool list happens to coincide with read-only needs. If Phase 11 picks up the whole sentinel.md overhaul (P11-AG-S3 + S5), upgrading S4 to MUST FIX in the same patch costs nothing.
5. **Cross-plugin coordination opportunity:** P11-HK-C5 (language detection logic in `hooks/detect-state.sh`) mirrors `fakoli-flow/hooks/detect-context.sh`. A unified fix across both plugins keeps the detection surface consistent. Coordinate with fakoli-flow maintainer.
