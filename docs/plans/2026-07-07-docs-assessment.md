# Documentation assessment — 2026-07-07

> **Status:** assessment complete; work items below are ready to be driven as
> one-PR-per-item. Audited against the code at v0.4.0 (schema 8), commit
> `236484c`. Every BROKEN finding below was verified against the source, not
> just read from the docs.
>
> **Audience:** maintainers. Not part of the user-facing docs nav.

## Executive summary

The docs are unusually deep for a v0.4 project — the problem is not coverage,
it is **drift and audience-mixing**. Three systemic issues explain almost every
finding:

1. **Two migrations left stale islands.** The 0.1.0 HOME-workspace layout and
   the shell-free `hook dispatch` rewrite were propagated to some docs
   (getting-started, state-location, AGENTS.md are clean) but not others
   (faq.md is badly wrong, evidence-buffer.md, migrations.md, parts of
   claiming/syncing guides).
2. **A fictional version scheme haunts the reference docs.** The pre-rename
   v1.x milestone labels (v1.8.0 … v1.22, "v2.0/v2.1") persist across mcp.md,
   llm.md, model-strategy.md, github-sync.md, sync-providers.md, migrations.md,
   faq.md, prd-template.md, and roadmap.md — against a product that says
   "Beta — v0.4.0" everywhere a new user first looks.
3. **The public site ships internal docs.** The mkdocs nav exposes ~35 internal
   files to end users: draft specs, dead phase plans, competitive-intelligence
   research that candidly lists anvil's weaknesses, an unfilled results stub,
   and two files containing personal machine paths (a CLAUDE.md rule
   violation in a public repo).

Counting only verified factual errors: **~20 BROKEN** (wrong command, wrong
flag, wrong behavior, dead link), **~25 STALE** (superseded layout/version/
count), plus the IA problems. The single worst doc is `faq.md`; the single
best are `README.md`, `docs/how-to/syncing-with-github.md`, and
`docs/context-budget.md` (zero findings above NIT).

## Scorecard by area

| Area | Verdict |
| --- | --- |
| README / index / getting-started | **Good.** Accurate at v0.4.0; one install-command inconsistency; no upgrade/uninstall docs. |
| FAQ | **Worst in tree.** Storage/backup answers describe the pre-0.1.0 in-repo layout; two shipped features described as unshipped/removed. |
| How-to guides | **Mostly strong.** syncing-with-github and claiming-and-shipping verified nearly flawless; using-anvil-on-any-harness has 2 broken commands; 2 of 6 "how-tos" are internal runbooks. |
| Reference docs | **Coverage gaps + drift.** cli-reference documents ~half the CLI; skills-reference misses a whole shipped skill; agents-reference has 3 wrong model tiers; evidence-buffer misdescribes its central lifecycle. |
| Architecture & design | **Good.** Both current at v0.4.0; keep both (what vs why); minor code-map staleness. |
| Site IA (mkdocs nav) | **Needs restructure.** Internal/user split does not exist; stale plans and strategy research are published. |

## P0 — factually wrong, user-facing (fix first)

Each bullet is sized to be one small PR (or batched where noted).

1. **Rewrite `docs/faq.md` storage/inspection/backup answers** for the
   HOME-workspace default (`~/.anvil/workspaces/<key>/.anvil/`). Today it
   tells users canonical state lives in-repo, to commit `.anvil/` to git
   (impossible under the default layout), and points sqlite3/backup examples
   at paths that don't exist. Crib from `state-location.md`, which is correct.
   Also in faq.md: `anvil replay` **is** shipped (`cli/__init__.py:159`) —
   the "on the roadmap, not yet shipped" answer is wrong; the
   disable-a-hook-by-renaming-`.sh` advice is dead (hooks run via
   `anvil hook dispatch` from `hooks/hooks.json`, no `.sh` in the manifest);
   the `README.md#comparison-vs-alternatives` anchor doesn't exist.
2. **Kill the phantom `submit --evidence` flag**:
   `using-anvil-on-any-harness.md:109` (broken command) and the
   `drive-the-anvil-loop.md:50` flow diagram. Real flags are
   `--commands` / `--files-changed`.
3. **Fix broken harness setup commands** in `using-anvil-on-any-harness.md`:
   `anvil mcp-config gemini` and `anvil mcp-config openhands` are not valid
   clients (`cli/mcp_config.py CLIENTS`). Point gemini at
   `packaging/gemini/gemini-extension.json` and openhands at
   `packaging/openhands/config.toml.snippet` — or add the two CLIENTS rows in
   code and document that. Also: the referenced `packaging/continue/` does not
   exist (same dead pointer in `install.py:206`).
4. **`docs/evidence-buffer.md` lifecycle is wrong**: `submit` reads buffer
   files but never deletes them (no unlink anywhere in `packet_apply.py`) —
   the doc's "consume-and-rotate / then **deletes**" story is false. The
   documented record schema also omits `kind` and `output_sha256`, and the
   consumer silently drops records without `output_sha256` — the doc's own
   example record would be skipped. Fix both, and the dead `docs/hooks.md`
   link.
5. **`docs/skills-reference.md` + `docs/agents-reference.md`**: add the
   shipped-but-undocumented `resolve-decisions` skill (count is 8, not 7 —
   also fix "seven plugin-owned skills" in agents-reference:166); correct
   3 of 5 agent model tiers (sentinel and state-keeper are `haiku`,
   docs-scribe is `sonnet`; docs say `opus` for all). Remove references to
   nonexistent "welder" / "smith" agents.
6. **`docs/cli-reference.md`**: `claim --lease` built-in default is **240**,
   not 60 (config.py:139; contradicts the doc's own line 91). "All 23
   commands" is false — 25 commands/subcommands are undocumented, including
   `doctor`, `install`, `graph`, `backup`/`restore`, `replay`,
   `migrate-workspace`, `proof verify`. Minimum viable fix: correct the
   default + count and add a one-line index entry per missing command;
   full flag docs can follow.
7. **`docs/mcp.md`**: `plan_tasks` is documented as deterministic/no-LLM but
   defaults `use_llm=True` (mcp_server.py:2190) — this also falsifies
   llm.md:297; `get_next_task` sort key is agent_suitability-desc/id-asc,
   not complexity-asc; `edit_dependencies` and `describe_surface` have no
   reference sections; `hooks.md` dead link.
8. **`docs/how-to/authoring-a-prd.md:249-257`**: re-parse is documented as
   destroying claimed/in-progress tasks; actual behavior is non-destructive
   supersede with lineage, and `plan` fails loudly rather than pruning
   claimed tasks (`cli/prd.py`, and prd-template.md:565-575 — which this
   section cites — says so correctly). Also delete the contradictory
   `ANTHROPIC_API_KEY` failure-table row at line 330.
9. **Scrub personal machine paths from the public tree** (CLAUDE.md rule):
   `docs/plans/2026-05-25-phase-9.md` and
   `docs/findings/2026-07-05-openclaw-weak-runner.md` both contain
   `/Users/<name>/…` paths (the findings doc also includes gateway
   hostnames). Scrub in place; archiving does not fix public exposure.
10. **`docs/migrations.md`**: history table stops at v6; code is at **v8**
    (schema.py). Forward-branch list, worked example (`v3 -> v4`), and the
    `user_version` = 4 snippet under the "→ v3" section are all stale;
    `migrate-workspace` is never mentioned. (`migrate.py:294` docstring has
    the same staleness — fix together.)
11. **`docs/llm-providers.md:98,148`**: `pip install 'anvil[bedrock]'` /
    `'anvil[custom]'` — the package is `anvil-state`. Same bug in the code's
    error strings (`planning/llm.py:789,951`) — fix both in one PR.

## P1 — information architecture (make the site look professional)

12. **Split user docs from internal docs in `mkdocs.yml`.** Proposed shape:
    keep *Home / Getting started / How-to / Reference / Architecture* as the
    user site; add a **Development** section for live contributor docs
    (sync-providers, live backlogs, production-readiness plan, specs,
    decisions, quality/evidence docs); remove **research/** from the nav
    entirely (competitive-intelligence content — keep in repo via
    `not_in_nav`). Create `docs/archive/` for dead weight: BUILD-REPORT.md,
    phase-9/phase-11 backlogs (self-described as archived), the four
    pre-rename plans (phase-8/9/10, SL-1), audits/2026-05-26 (audits
    "v1.9.0"). Delete or blank `research/2026-06-21-bake-off-results.md`
    (published unfilled stub). Note: CI builds `--strict` — moving files
    means fixing every relative link in the same PR.
13. **Move the two internal runbooks out of How-to**: `how-to/bake-off.md`
    and `how-to/packet-quality.md` are B-numbered maintainer docs (and both
    contain broken bare-`python` invocations — must be
    `uv run --project bin python …`). Move under Development/plans and fix
    the commands.
14. **`docs/roadmap.md`** says "Last updated: 2026-05-31" and is organized
    around the retired v1.11/v2.0/v2.1 line. Either refresh it against the
    0.x reality or replace the nav entry with the production-readiness plan
    until it's rewritten.
15. **Version-scheme sweep**: purge or annotate every v1.x/v2.x milestone
    anchor in mcp.md, llm.md, model-strategy.md, github-sync.md,
    sync-providers.md, migrations.md, faq.md, prd-template.md ("v1.16.0").
    Cheapest consistent fix: a one-line "historical milestone labels
    pre-date the 0.x renumbering" legend in each affected doc, then remove
    labels opportunistically.
16. **`docs/live-tests.md`** documents a nightly workflow that was never
    committed ("Status: designed, not yet committed" — confirmed absent from
    `.github/workflows/`). Either commit the workflow or move the doc to
    Development with a clear not-yet-built banner.

## P2 — consolidation and polish

17. **Merge the LLM triplet into two docs**: llm.md absorbs llm-providers.md
    (one canonical provider matrix + tier/cost table + ONE prompt-caching
    explanation — it currently appears three times, twice in llm.md alone);
    model-strategy.md stays as the contributor "why", linking to the
    canonical table (it already has the *correct* agent-model mapping).
18. **One canonical quickstart.** README, index.md, and getting-started.md
    carry three copies (consistent today, drift surface tomorrow). Keep the
    full path in getting-started; README/index keep a 5-line teaser + link.
    Same for the PRD example duplicated between authoring-a-prd.md and
    prd-template.md, and the `sync.providers` schema duplicated between
    github-sync.md and sync-providers.md.
19. **Unify the plugin install command**: README says
    `/plugin marketplace add fakoli/anvil` + `/plugin install anvil@anvil`;
    getting-started says `/plugin install anvil` with no marketplace step.
    It's step 1 of onboarding — pick one form everywhere.
20. **Add upgrade/uninstall/troubleshooting coverage**:
    `uv tool upgrade anvil-state`, `/plugin marketplace update`,
    `anvil install <harness> --rollback` all exist in code and appear
    nowhere in entry docs. Also link faq.md from README's docs list and
    index's "Start here".
21. **Add a glossary** (packet, claim, lease, loop, gate, PRD, workspace),
    linked from every how-to intro — each term is currently defined in
    exactly one doc's prose, and readers entering mid-sequence meet them
    cold.
22. **Audience banners.** Only sync-providers.md states who it's for. Add a
    one-line user-vs-contributor banner to every reference doc.
23. **Small verified fixes, batchable**: hooks-reference lacks a section for
    the 5th hook (`heartbeat`) and mentions 3 of 6 `anvil hook` subcommands
    (misses `stop-gate` et al.); `bin/anvil-mcp` header comment says
    "13 tools" (24); drive-the-anvil-loop.md:117 says `run-workflow` is
    deferred (it shipped — `cli/run_workflow.py`); claiming guide still
    teaches legacy comma-separated `--commands` (repeatable flags are
    canonical now); mcp-config client list in cli-reference shows 7 of 12
    clients; architecture.md "where to read the code" map omits `scan/`,
    `workflows/`, `signing.py`, `state/durable.py` etc.; add the one-line
    workspace-path caveat (as in getting-started) to claiming-and-shipping
    and syncing-with-github.

## Prevention — stop the drift from recurring

The repo already proves the pattern that works: `tests/test_version_sync.py`
and `tests/test_install_manifests.py` pin manifests to `anvil.__version__`,
and those files never drifted. Extend it:

- **Count/roster sync tests**: assert skills-reference covers every
  `skills/*/SKILL.md`, agents-reference matches `agents/*.md` frontmatter
  (name + model), mcp.md's tool list matches the `@mcp.tool` registrations,
  hooks-reference covers every `hooks.json` entry. Each is a ~20-line pytest
  that would have caught findings #5, #7, #23 automatically.
- **Command-example lint**: a test that extracts `anvil …` invocations from
  docs code fences and checks the subcommand + flags exist in the Typer app
  (`--help` parse). Would have caught `--evidence`, `mcp-config gemini`,
  and the lease-default class of errors at PR time.
- **Path hygiene check**: grep CI step failing on `/Users/` in `docs/`
  (enforces the CLAUDE.md public-repo rule mechanically).
- These slot into the existing `docs` job in `ci.yml` alongside
  `mkdocs build --strict` — and are a natural first brick for the planned
  multi-harness CI work, since the same roster tests can assert
  `packaging/<harness>/` completeness per harness.

## Suggested sequencing

- **Wave 1 (correctness, ~6 small PRs):** items 1–4, 9, 11 — everything a
  user can copy-paste and have fail, plus the public-repo path scrub.
- **Wave 2 (reference truth, ~4 PRs):** items 5–8, 10.
- **Wave 3 (IA, 2–3 PRs):** items 12–16 (nav restructure is one big
  mechanical PR; roadmap refresh separate).
- **Wave 4 (consolidation, ongoing):** items 17–23, prevention tests
  alongside — land the roster-sync tests *before* the consolidation wave so
  the merges can't reintroduce drift.

Publish (patch bump per CLAUDE.md) after Wave 1 lands — those are the fixes
users are currently being misled by.
