# P11 — Phase-10 Plugin-Audit Deferral Batch

**Date:** 2026-06-19
**Status:** Draft — needs approval before implementation
**Plugin:** `anvil`
**Tracks:** roadmap "Version: next (v1.11 / v2.0 candidate)" and the [7 cross-cutting themes](../roadmap.md#cross-cutting-themes-high-leverage-batches); archived catalog [`docs/phase-11-backlog.md`](../phase-11-backlog.md); source [Phase 10 audit](../audits/2026-05-26-plugin-audit.md) (2026-05-26)
**Breaking:** NO. Every item is non-breaking hygiene (docs, schema-truthfulness, perf, robustness).

---

## 1. Goal & framing

The **P11 batch** is the set of deferred findings from the Phase-10 plugin audit
(2026-05-26): the five critics (agent / skill / hook / mcp / structure) raised 57
items below MUST-FIX, of which 56 were carried forward as live. None are
breaking; the bulk are mechanical. They are catalogued by critic in
[`phase-11-backlog.md`](../phase-11-backlog.md) and re-homed by version × theme in
[`roadmap.md`](../roadmap.md).

**Why this is lower-priority than the integrity track.** The roadmap explicitly
defers P11 behind the SL-* integrity work: "Adding a third sync provider makes
the product wider; it does not make the central claim more true. None of it
belongs in these 90 days." (`roadmap.md:160-162`). P11 is quality polish on
surfaces that already work — it does not move the replay/provenance/non-gameable
claims the product is built on. So it runs *opportunistically*, after the SL
specs, and never blocks them.

**The principle: batch by theme, one PR per theme, smallest correct diff.** The
audit grouped 56 items into 7 cross-cutting themes that each share a single root
cause; fixing a theme as a unit produces lockstep consistency and one reviewable
diff. We hold to the ponytail rule throughout — the smallest change that closes
the finding, no speculative refactor. NITs are never their own PR; they ride
along on whatever theme touches their file.

**Critical caveat — the codebase moved under the audit.** The audit is dated
2026-05-26. Since then the plugin was **renamed `fakoli-state` → `anvil` and
extracted to a standalone repo** (commits `f36ec8c` import, `28937ec` rename,
`ceb1d9e` standalone stand-up, `dc41ed5` skill dedup −174 lines), and a hook
hot-path perf pass landed (`7050abd` "collapse redundant python3 spawns"). As a
result **several audit items are already resolved, and several were superseded by
the extraction** (the fakoli-flow/fakoli-crew bridging the audit targeted no
longer exists in these skills). §2 records what was verified done; every
remaining unit carries a `Current status:` so the implementer re-checks before
touching anything line-number-anchored to the pre-extraction tree.

---

## 2. Already-resolved items (struck from the batch)

Verified against the current tree on this branch. Each is removed from scope with
evidence.

### Theme 3 (hook hot-path perf) — **DONE** (`7050abd`)

- **P11-HK-S1** (`check-claim.sh` spawned python3 twice): now **one** python3
  invocation. `hooks/check-claim.sh:38-52` extracts file path and actor in a
  single `python3 -c` that prints both fields; `grep -c python3` on the script
  body = 1 live spawn. **DONE.**
- **P11-HK-S2** (`record-file-change.sh` spawned 5-6 python3): now **one**
  `python3` heredoc pass (`record-file-change.sh:42-81`) that extracts fields
  *and* builds the full `json.dumps` event line, emitting shell-sourceable
  `shlex.quote`d assignments. No `_escape_json()` fallback remains. **DONE.**
- **P11-HK-N1** (three `printf … | sed -n 'Np'` forks): `grep sed
  hooks/record-file-change.sh` → no matches. The sed extraction is gone (the
  single heredoc replaced it). **DONE** as the drive-by it was scoped to be.

> Net: **Theme 3 closes with zero work.** The roadmap text (and `docs/design.md:218`)
> still points at Theme 3 as "the next hot-path pass" — that prose is stale and
> should be trimmed by Theme 7's docs sweep, not re-implemented.

### Theme 2 (non-empty actor validation) — **DONE**

- **P11-MC-S1**: `_require_actor(actor: str) -> str` exists at
  `bin/src/anvil/mcp_server.py:312-330` (strips, raises `ToolError` on empty),
  and is called as the first line of every mutating tool: `claim_task`
  (`:728`), `release_task` (`:788`), `renew_claim` (`:833`),
  `submit_progress` (`:969`), `submit_completion_evidence` (`:1022`),
  `update_task_status` (`:1330`). Six sites covered. **DONE.**

### Theme 4 (hook contract docs) — **DONE**

- **P11-HK-S3**: the non-blocking contract is documented at doc level in **four**
  places: `docs/design.md:190-218` (full "Why hooks are non-blocking" section
  with the exact "exit 0 / no set -e / wrap with `|| true` / <200ms" wording),
  `docs/faq.md:178-182`, `docs/hooks-reference.md:5`, and `docs/architecture.md:112,447`.
  The gap the finding described (contract only in script-header comments) is
  closed. **DONE.**

### Structure / Theme 7 — partially **DONE** (extraction re-versioned the repo)

- **P11-ST-S1 / P11-ST-S4 / P11-ST-N1** (install-messaging drift, "once
  published" phrasings): `README.md:176-187` now ships the real
  `/plugin marketplace add fakoli/anvil` + `/plugin install anvil@anvil` flow
  with a standalone-clone fallback. `grep -niE "not yet|once published|this
  release|coming soon"` over README → **no matches**. **DONE.**
- **P11-ST-S2** (CHANGELOG `[Unreleased]` narrates shipped v1.9.0): superseded.
  The repo re-versioned to **v0.0.8**; `CHANGELOG.md` `[Unreleased]` now carries
  forward-looking T017 work, not a v1.9.0 retrospective. **SUPERSEDED.**
- **P11-ST-S3** (`.gitignore` lacked root `.pytest_cache/`): `.gitignore:12` =
  `.pytest_cache/` (no `bin/` prefix). **DONE.**
- **P11-ST-C1** (no surface-count table): `README.md:34-40` is a "Surface |
  Count | Notes" table (8 skills, 5 agents, 4 hooks). **DONE.** (Counts differ
  from the audit's "6 agents / 7 skills" — the extraction changed the surface;
  the live table is correct.)

### Agent items mooted by the extraction

- **P11-AG-S4** (`sentinel.md` used `allowed-tools:`): now `tools:`
  (`agents/sentinel.md:11`). **DONE.**
- **P11-AG-N1** (`sentinel.md` missing trailing newline): file ends with `\n`
  (verified via `od -c`). **DONE.**
- **P11-AG-C2** (`marketplace-scribe.md` composition dup): the
  **`marketplace-scribe` agent no longer exists** (`ls agents/` →
  critic, docs-scribe, planner, sentinel, state-keeper). **MOOT.**

---

## 3. Execution plan — one section per PR unit

Themes whose every item resolved in §2 are not repeated. What follows is the
*remaining* work. Each unit = one PR.

### Unit A — Theme 5: phase-status table drift in `state-ops`

- **Closes:** P11-SK-S7, P11-SK-S8.
- **Current status:** **verified open.** `skills/state-ops/SKILL.md` still labels
  `list`/`show` as "Phase 3 — pending" (`:67`, `:85`), `next` as "Phase 4 —
  pending" (`:108`), `conflicts` as "Phase 5 — pending" (`:122`), and the
  command table (`:231-244`) marks `list`/`show`/`conflicts` as future phases —
  yet `execute/SKILL.md` and the rest of the plugin treat these as available.
- **Approach:** drop the "Phase N — pending" framing from `state-ops/SKILL.md`
  Steps 2-5 and the command table; mark these subcommands as available, matching
  every other skill. The plugin is well past these phases. **Also remove or
  rewrite the bottom "Phase 2 Limitations" section (`:223-248`)** — it still says
  *"All other commands … will error … until their respective phases land"* and
  carries a "Pending (do not invoke)" table for `list`/`show`/`next`/`conflicts`.
  Fixing only the Step headers while leaving this section intact would make the
  skill describe those commands as available in the body AND "do not invoke" at
  the bottom — a self-contradiction. Smallest correct diff is an in-place edit of
  `state-ops/SKILL.md` (no new reference file — see Unit-F note on SK-C1). If a
  single source of truth is wanted, a short `docs/phase-status.md` linked from
  `state-ops` is acceptable but optional.
- **Acceptance:** no "pending"/"do not invoke" status remains for
  `list`/`show`/`next`/`conflicts` anywhere in `state-ops/SKILL.md` (Steps,
  command table, AND the bottom limitations section); `grep -rn "pending" skills/`
  returns only genuinely-future items (if any); the four subcommands run as
  documented.
- **Effort:** ~30 min (docs-only, one file).
- **Dependencies/sequencing:** none. Independent of all other units.

### Unit B — Theme 6: composition duplication across doc/state agents

- **Closes:** P11-AG-C1, P11-AG-C3 (P11-AG-C2 mooted — see §2).
- **Current status:** **partially open / reduced.** With `marketplace-scribe`
  gone, the 3-way duplication is now 2-way: `agents/docs-scribe.md:105-113`
  ("Composition with state-keeper") and `agents/state-keeper.md:222-232`
  ("Composition") restate the same "two anvil maintenance specialists" split.
  Files are also smaller post-extraction (docs-scribe 303 lines, state-keeper
  259 — both under the 400 ceiling the audit flagged), so the size-pressure half
  of C1 is gone; only the lockstep-duplication half remains.
- **Approach:** because it is now only 2 files and ~10 lines each, the ponytail
  call is **do not extract a shared `docs/specs/internal-agents.md`** — a new
  file for two cross-references is heavier than the duplication. Instead, trim
  each agent's composition block to a one-line statement of its own lane plus a
  link to the other agent. Re-evaluate extraction only if a third doc/state
  agent is ever re-added.
- **Acceptance:** docs-scribe and state-keeper each describe only their own
  responsibility and link (not restate) the other; no duplicated prose block
  remains.
- **Effort:** ~20 min.
- **Dependencies/sequencing:** none.

### Unit C — MCP schema-truthfulness (remaining mcp-critic items)

- **Closes:** P11-MC-S2, P11-MC-S3, P11-MC-S4, P11-MC-N1, P11-MC-C1,
  P11-MC-C2, P11-MC-C3, P11-MC-N2. (P11-MC-S1 closed — see §2.)
- **Current status:** **verified open** (all of the above):
  - **MC-S2** — `list_tasks(status: str | None = None, …)` (`mcp_server.py:584`)
    still unconstrained; a typo returns a silently-empty list.
  - **MC-S3 / MC-N1** — `list_tasks` / `get_task` / `get_next_task` still return
    `dict[str, Any]` via the `json.loads(t.model_dump_json())` triple-roundtrip
    shim (`:612`, `:633`, `:706`).
  - **MC-S4** — `get_next_task(actor: str | None = None)` (`:644`) still accepts
    `actor` and never uses it (contract lie).
  - **MC-C1** — `_resolve_state_dir(cwd)` is re-resolved per call. This is
    **intentional, not a defect**: the docstring (`mcp_server.py:336-339`) states
    each call resolves state relative to cwd at call time, and tools like
    `list_tasks` forward a per-call `cwd` so one server can drive multiple
    projects. The audit's "cache it" suggestion is therefore **rejected** (see
    Approach). Likely re-classified as won't-fix.
  - **MC-C2** — `_reap_stale` (`:395-403`) swallows all exceptions with a bare
    `pass`; no `logger.warning`.
  - **MC-C3** — `WorkPacketResponse.content: Any` still wide.
  - **MC-N2** — `DependencyEdge(**{"from": …, "to": …})` splat still in place.
- **Approach:** single focused PR on `bin/src/anvil/mcp_server.py`. S2: replace
  `status: str | None` with a `Literal[...]` matching the 11 `TaskStatus` values
  verbatim. S3 (closes N1): define a `TaskSummary` or reuse the `Task` Pydantic
  model from `state.models` as the return type and drop the `json.loads(...)`
  shim. S4: remove the unused `actor` param. C1: **do NOT cache at import** — an
  import-time `_STATE_DIR = Path.cwd().resolve()` would silently ignore the
  per-call `cwd` and lock the server to its start directory, breaking
  multi-project use. Leave the per-call resolution (the perf saving is
  negligible); only if a real cost is measured, use `functools.lru_cache` keyed
  on the resolved `cwd`, never an import-time constant. Treat MC-C1 as won't-fix.
  C2: add `logger.warning("stale-claim reaping failed: %s", exc)` inside the
  except, keeping the swallow. C3: narrow `Any` → `str | dict[str, Any]`. N2:
  switch to `DependencyEdge.model_validate({"from": …, "to": …})` or annotate.
  **Sequencing note:** the roadmap pairs MC-C2 with `tech-debt-backlog` **CL-3**
  (`_reap_stale_claims` bare except) — close both in this patch if CL-3 is still
  open. **Re-verify the audit line ranges** (459-464, etc.) against the current
  file — they are pre-extraction; the *symptoms* above were re-confirmed at the
  cited current lines.
- **Acceptance:** `list_tasks` rejects an invalid status at the schema boundary;
  the three task tools expose field-level schema (Pydantic return type, no
  `json.loads` shim); `get_next_task` has no `actor` param; `_reap_stale` failure
  is visible under `claude --debug`; `pytest tests/test_mcp.py` green.
- **Effort:** ~2-3 h (code + tests). This is the only code-heavy unit.
- **Dependencies/sequencing:** none, but it touches one file across many tools —
  land it on its own to keep the diff reviewable.

### Unit D — MCP/hook robustness (CONSIDER-tier, opportunistic)

- **Closes:** P11-HK-C2, P11-HK-C3, P11-HK-C4, P11-HK-C5.
- **Current status:** **verified open:**
  - **HK-C2** — no `ANVIL_HOOK_DEBUG` support anywhere in `hooks/`.
  - **HK-C3** — `detect-state.sh:29` still `status --hook-format 2>&1` (merges
    stderr into the line shown to Claude).
  - **HK-C4** — all four hooks still set relative `STATE_DIR=".anvil"`
    (`capture-evidence.sh:27`, `check-claim.sh:17`, `detect-state.sh:7`,
    `record-file-change.sh:14`); should be `${CLAUDE_PROJECT_DIR:-$PWD}/.anvil`.
  - **HK-C5** — `detect-state.sh:14-20` still uses sequential overwrites
    (last match wins); polyglot projects mislabeled.
- **Approach:** one hooks PR. C4 is the highest-value (correctness under a hook
  cwd that isn't project root): set `STATE_DIR="${CLAUDE_PROJECT_DIR:-$PWD}/.anvil"`
  in all four. C2: one-line `ANVIL_HOOK_DEBUG=1` stderr→`.anvil/.hook-debug.log`
  wrapper at the top of each hook. C3: drop `2>&1`; capture stderr separately for
  the diagnostic branch. C5: emit a comma-joined language list or guard each line
  with `[ "$DETECTED_LANG" = "unknown" ]`. **Cross-plugin note:** C5's logic
  mirrors `fakoli-flow/hooks/detect-context.sh` if that plugin is co-maintained;
  the standalone anvil repo can fix locally without waiting.
- **Acceptance:** hooks resolve `.anvil` correctly when cwd ≠ project root;
  `ANVIL_HOOK_DEBUG=1` produces a debug log; `detect-state` no longer leaks
  stderr into the status line; polyglot repo is not silently mislabeled.
- **Effort:** ~1-1.5 h.
- **Dependencies/sequencing:** none. Pure CONSIDER tier — can be dropped if
  capacity is tight without affecting any SHOULD-FIX claim.

### Unit E — Skill-hygiene polish (workflow discipline + NITs)

- **Closes (re-verify each — line anchors are pre-extraction):** P11-SK-S9,
  P11-SK-C2, P11-SK-C3, P11-SK-C4, P11-SK-C5, P11-SK-C6, P11-SK-N1, P11-SK-N2,
  P11-SK-N3, P11-SK-N4.
- **Current status:** **unverified — likely mostly stale.** The skills were
  rewritten and deduplicated (`dc41ed5`, −174 lines) after the audit, so every
  line-number anchor is invalid and most of these may already be addressed.
  Spot-check: SK-S9 (state-ops description "60+ words") is now a single concise
  line (`state-ops/SKILL.md:3`) — the length complaint is resolved, though it
  still lacks quoted trigger phrases, so a light touch may remain. Treat the rest
  (C2-C6, N1-N4) as **verify-at-implementation-time**: re-read each target skill,
  keep only the findings that still reproduce.
- **Approach:** a single docs-only skill-hygiene pass. For each surviving
  finding apply the audit's fix shape (promote buried discipline rules to
  callouts: SK-C2 concrete stopping rule, SK-C4 one-question-per-message in
  `prd`, SK-C5 move execute's abort flow ahead of packet fetch, SK-C6 `--reason`
  callout; SK-S9 add quoted trigger phrases). NITs (N1-N4) are drive-by within
  the same files. **Drop any finding that no longer reproduces** — do not
  re-introduce structure just to "close" a stale id.
- **Acceptance:** each surviving finding's symptom is gone; closed ids list only
  the findings that were actually still present; no NIT got its own commit.
- **Effort:** ~1-1.5 h, most of it re-verification.
- **Dependencies/sequencing:** after Unit A (both touch `SKILL.md` files; doing
  A first avoids a merge overlap on `state-ops`).

### Unit F — superseded-by-extraction items (decision/cleanup, mostly no-op)

- **Concerns:** Theme 1 (P11-SK-S1, S2, S3, S4, S6), agent example-count items
  (P11-AG-S1, S2, S3), P11-AG-S5, P11-AG-C4, P11-SK-C1.
- **Current status:** **superseded / needs a one-time disposition.**
  - **Theme 1 (no-fuzzy-detection):** `grep -rn "fakoli\|claude plugin list"
    skills/` → **no matches**. The standalone extraction removed all
    fakoli-flow/fakoli-crew bridging from the skills; the "when X is installed"
    prose the audit targeted is gone. The surviving "when available" phrases
    (e.g. `prd/SKILL.md:102`, `finish/SKILL.md:111`) refer to **MCP tool
    availability**, not plugin bridging, and are correct as written. **Theme 1
    is moot** — close the 5 ids as superseded, do not add shell checks.
  - **Agent example-count items (AG-S1/S2/S3):** the audit counted `<example>`
    XML blocks against a *fakoli-crew* convention (floor 2-3). The extracted
    anvil agents use a `> **Context:**` prose format with zero `<example>` XML
    blocks (`grep -c "<example>" agents/*.md` → 0 across all). The fakoli-crew
    convention no longer governs these files. **Close as superseded** unless the
    team adopts an anvil-specific example-count rubric — out of scope here.
  - **AG-S5** (`sentinel.md` at proportionality floor, missing
    Composition/Inputs/NOT sections): `sentinel.md` is now 96 lines and still
    leaner than `critic.md` (123). This is a genuine **but optional** expansion;
    if picked up, fold it into Unit B (agent docs). Low priority.
  - **AG-C4** (`planner.md` composition mentions only one defer-to): re-verify
    against current `planner.md` (123 lines, rewritten); fold into Unit B if it
    still reproduces.
  - **SK-C1** (extract `references/` subdirs): the dedup pass (`dc41ed5`) already
    removed the boilerplate this aimed at; skills are 190-319 lines with no
    `references/` dirs. Ponytail call: **do not create empty `references/`
    scaffolding** — extraction is justified only if a real shared block survives
    (Theme 5 can link a `phase-status.md` if Unit A chooses that route). Close
    SK-C1 as superseded/won't-do.
- **Approach:** this is not really a PR — it's a documentation reconciliation.
  Update `roadmap.md` and `phase-11-backlog.md` to mark these ids
  superseded/closed with the evidence above, so the backlog stops claiming work
  that the extraction already invalidated. Can be folded into the Unit-A or
  Unit-B PR's docs changes rather than a standalone PR.
- **Acceptance:** roadmap/backlog no longer list moot ids as open; each carries a
  one-line "superseded by standalone extraction" note.
- **Effort:** ~30 min.
- **Dependencies/sequencing:** do last, so it can record the disposition of
  anything Units A-E decided to drop.

---

## 4. Sequencing & rollout

Cheapest / highest-confidence first; docs themes before code themes; the only
code-heavy unit (C) lands alone for a reviewable diff.

1. **Unit A — Theme 5 phase-status drift** (docs, verified open, ~30 min).
2. **Unit B — Theme 6 agent composition** (docs, verified open, ~20 min).
3. **Unit E — skill-hygiene polish** (docs; after A to avoid `state-ops` overlap;
   mostly re-verification).
4. **Unit D — hook/MCP robustness** (small code, CONSIDER tier).
5. **Unit C — MCP schema-truthfulness** (the one code-heavy unit; land alone).
6. **Unit F — supersession reconciliation** (docs bookkeeping; fold into A/B or
   run last).

Each unit is one themed PR. This whole batch runs well via the **`resolve-loop`**
skill: one item → research → implement in an isolated worktree → adversarial
self-review → **one PR per theme**, waiting for **CI + Greptile** on each before
merge. Because the units are independent (only E depends on A), `resolve-loop`
can fan them out in parallel worktrees, with C kept on its own lane.

Do **not** re-grade severities — the auditors' SHOULD/CONSIDER/NIT calls are
fixed (`phase-11-backlog.md` "Notes for Phase 11 planner" #1). The one standing
upgrade option (AG-S4 → MUST FIX) is moot since AG-S4 is already done.

---

## 5. Out of scope / explicitly deferred

- **All NITs as standalone work.** SK-N1..N4, MC-N1/N2, HK-N2, ST-N1 are
  drive-by only; they ride the theme PR touching their file (or are already
  closed — HK-N1, ST-N1).
- **P11-HK-C1** (race-prone append / `flock` on `events.jsonl`): the roadmap
  defers this to a v2.x sync-hardening pass, and it overlaps the SL integrity
  track's write-path work (`docs/specs/2026-06-01-sl1-rr-1-…`). **Out of P11
  scope** — fix it inside the integrity track, not as audit polish.
- **P11-HK-N3** (hardcoded verification-command pattern list): roadmap marks it
  "unscheduled," aligned with `tech-debt-backlog` CL-10. Defer.
- **Theme 1 (P11-SK-S1/S2/S3/S4/S6)** — superseded by the standalone extraction
  (no fakoli bridging remains). Closed, not implemented (Unit F).
- **Agent example-count items (P11-AG-S1/S2/S3)** — superseded; the fakoli-crew
  `<example>`-block rubric does not govern the extracted anvil agents. Closed
  unless an anvil-specific rubric is adopted (separate decision).
- **P11-SK-C1** (`references/` extraction) — superseded by the `dc41ed5` dedup;
  won't-do unless a real shared block survives.
- **All `P9B-*` items and the v2.0/v2.1 sync-provider / webhook / snapshot work**
  — these are the v2.x roadmap, not the P11 audit batch. Out of scope.
- **`tech-debt-backlog` CL-/TQ-/PS- items** — owned by that backlog; only CL-3
  is pulled in opportunistically alongside MC-C2 in Unit C.

---

## Appendix — net open count after verification

Of the 56 live P11 ids:

- **Already done / superseded (struck):** HK-S1, HK-S2, HK-N1, HK-S3 (Theme 3+4);
  MC-S1 (Theme 2); ST-S1, ST-S2, ST-S3, ST-S4, ST-C1, ST-N1 (structure/Theme 7);
  AG-S4, AG-N1, AG-C2; SK-S1, SK-S2, SK-S3, SK-S4, SK-S6 (Theme 1); AG-S1, AG-S2,
  AG-S3, SK-C1 — **~24 ids** no longer require implementation.
- **Genuinely open, scoped into Units A-D:** SK-S7, SK-S8 (A); AG-C1, AG-C3 (B);
  MC-S2, MC-S3, MC-S4, MC-N1, MC-C1, MC-C2, MC-C3, MC-N2 (C); HK-C2, HK-C3,
  HK-C4, HK-C5 (D) — **16 ids**.
- **Open but stale-anchored / re-verify (Unit E + low-priority):** SK-S9, SK-C2,
  SK-C3, SK-C4, SK-C5, SK-C6, SK-N1, SK-N2, SK-N3, SK-N4, AG-S5, AG-C4 —
  **~12 ids**, expected to shrink sharply on re-verification.
- **Explicitly deferred (out of scope):** HK-C1, HK-N3 — 2 ids.

**Net genuinely-open after verification: ~16 confirmed (Units A-D), plus up to
~12 to re-confirm (Unit E).** The confident, ship-now batch is **four PRs
closing 16 ids**; the remaining dozen are docs polish most of which the
extraction likely already absorbed.
