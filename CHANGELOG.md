# Changelog

All notable changes to anvil are documented here. This project adheres to [Keep a Changelog](https://keepachangelog.com/en/1.0.0/) and [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Added

- **Risk-axis eligibility on `anvil next` — the pull-fleet MVP (B45).** `anvil next
  --max-blast N --max-review-risk M` returns only ready tasks scoring at or below
  those ceilings, composing with the existing dependency/priority sort and
  preserving the `-q` exit codes (0 = a task printed, 3 = none eligible). It is
  **safe-by-construction from the first commit**: the blast/review-risk scores
  ride on an untrusted filename regex, so a task is eligible for a ceilinged
  (low-risk, e.g. local) runner ONLY if that dimension is **confirmed**
  (`Score.blast_radius_confirmed` / `review_risk_confirmed`, new) *and* within the
  ceiling — an unscored, unconfirmed, or over-ceiling task is treated as
  frontier-only (ineligible), so the filter fails safe, never routing
  weakly-scored risk to a weak executor. Defaults make every task unconfirmed
  until a trusted risk-label source ships (follow-up), so ceilinged `next` is
  conservative by design; unrestricted `anvil next` is unchanged.

- **Unified actor identity — one `resolve_actor()` across every claim surface (B47).**
  Before this, `anvil claim` defaulted to `$USER`, the bundled heartbeat hook
  passed the Claude `session_id`, gate-check/claim-guard used `$USER`, and the
  hook verbs used `$ANVIL_GATE_ACTOR` — four different identities. A claim made
  under one and heartbeated/gated under another renewed **zero** leases (the
  lease silently expired mid-work) and the finish-gate, seeing no matching claim,
  failed **OPEN**. Now claim / release / renew / next / heartbeat / stop-gate /
  gate-check / claim-guard / submit all resolve identity through one
  `resolve_actor()` with precedence **explicit `--actor` > `$ANVIL_ACTOR` >
  `$ANVIL_GATE_ACTOR` (legacy) > `$USER` > stable per-runner signing-key
  fingerprint > `agent`**. The bundled `heartbeat.sh` no longer passes a
  per-session id — it lets the CLI resolve the same identity the claim used. The
  stable fingerprint (from B48's keypair) means two headless runners no longer
  collide on `agent`. Set `$ANVIL_ACTOR` to pin a fleet runner's identity. (MCP
  tools still **require** an explicit actor by design — an audit-integrity guard
  against blank attribution.)

- **Max-claim-age cutoff — wedged agents can't hold a lease forever (B46, part 1).**
  `renew()` now refuses once a claim is older than `default_lease_minutes ×
  max_claim_age_multiplier` (new config, default **4×**), even if the agent keeps
  heartbeating — so the lease then expires and the stale reaper reclaims the task
  (and frees its conflict group). Protection applies even without a `config.yaml`
  (ClaimManager defaults to 4× the base lease). `anvil doctor` gains a
  `max_claim_age` probe that surfaces over-age active claims as a warning. This is
  the robust half of B46; the progress-gated heartbeat (extend only on observed
  `file_changed`/evidence) is deferred until `file_changed` emission under the
  HOME layout (B44-2) and unified actor identity (B47) make it correct rather than
  fragile (`file_changed` is file-keyed + actor-tagged).

- **Portable signed AcceptanceProof + strict-by-default for loops (B48, part 2 of 2).**
  On acceptance the engine now emits a typed, **signed** `AcceptanceProof` — a
  portable receipt binding the task id, claim/lease id, actor, the observed
  `CommandProof`s, and the event-log range it covers, with a **detached Ed25519
  signature** over a canonical serialization. It is written to
  `<state>/proofs/<task>-<event>.json` and verifies **off-host with only the
  signer's public key** (`anvil proof verify <file> --trust <list>`): the three
  checks — valid signature, genuine `signer_id` fingerprint, and key in the
  trust list — make it verifiable *without trusting the producer* (asymmetric,
  not HMAC). Keys are a per-runner Ed25519 keypair under `~/.anvil/keys/`
  (override `ANVIL_KEYS_DIR`); the public-key fingerprint is the signer id.
  Emission is best-effort and **file-only** (never blocks acceptance, never
  enters replayable state, so replay-equivalence stays deterministic).
  `ANVIL_STRICT_EVIDENCE` joins the strict-evidence precedence
  (`--strict` flag > env > config > off) so an autonomous loop/fleet can enforce
  the evidence gate everywhere without per-project config. `cryptography` is now
  an explicit dependency. Builds on part 1's typed `CommandProof`s.

- **Typed, observed evidence proofs (SL-3 / B48, part 1 of 2).** The review gate
  no longer has to trust strings. A new typed `ProofArtifact` union
  (`CommandProof` / `DiffProof` / `LinkProof` / `AssertionProof`) is carried on
  `Evidence.proofs`, and tasks can declare typed `Verification.required_proofs`.
  A `command` requirement is satisfiable **only** by a `CommandProof` whose real
  `exit_code` is in the passing set — so a free-text claim can't fake it, and a
  recorded command that *failed* (exit != 0) no longer passes the gate. The
  `capture-evidence` PostToolUse hook now records each command's real exit code
  and an `output_sha256` over its full output, and `anvil submit` (CLI **and**
  MCP) reconciles that per-claim buffer into `Evidence.proofs` — proofs are
  *observed by the engine*, not asserted by the agent. The planner emits a typed
  command requirement per verification command, so the gate is active for real
  tasks. **Additive and non-breaking:** the legacy free-text `required_evidence`
  path is untouched (both surfaces are enforced), so old event logs replay
  unchanged. Schema **v5 -> v6** (auto-upgrade adds the `evidence.proofs` column,
  DEFAULT `'[]'`). The portable **signed** ProofArtifact + strict-by-default for
  autonomous loops land in part 2.

- **HOME-workspace follow-ups (B44).** (1) **`anvil migrate-workspace`** — one-time,
  safe-first migration of legacy in-repo `<repo>/.anvil/` (or `<repo>/bin/.anvil/`)
  into the home workspace: dry-run by default (`--yes` to apply), never clobbers an
  existing home workspace, copies (never moves) so the legacy dir survives, and
  uses an atomic temp-then-rename so an interrupted copy can't half-populate.
  (2) **Basename-collision fix (dual-key):** new workspaces are keyed by basename +
  a path hash (`app-1a2b3c4d`) so two projects sharing a basename no longer collide;
  a pre-existing **bare-name** workspace is still honored (resolves unchanged — no
  orphaning of existing state). (3) **Plugin-root init guard** now fires only under
  the legacy `local` layout (it was dead in workspace layout — it checked the
  resolved HOME base, never a plugin root); initializing at the plugin root in
  workspace layout is harmless and correct for anvil-on-anvil dogfooding. (4) The
  **SessionStart hook** (`detect-state.sh`) is now workspace-aware — it no longer
  gates on a local `./.anvil` (which the home layout doesn't create) and nudges to
  `anvil migrate-workspace` when it detects un-migrated legacy in-repo state.

- **Codex/cross-harness hook verbs (B41).** Two new `anvil hook` subcommands, both
  reusing existing engine logic: **`anvil hook heartbeat`** (PostToolUse lease
  renew — renews the actor's active claim lease(s) on tool activity; non-blocking,
  wired into the bundled `hooks.json`, cross-harness Claude + Codex) and **`anvil
  hook stop-gate`** (the Codex/Claude analogue of the OpenClaw finish-gate — on
  `Stop`, emits `{"decision":"block","reason":…}` + exit 2 when a claimed task lacks
  submitted evidence, reusing `gate-check`'s `decide_from_rows`). The Stop-gate is
  **OPT-IN, not auto-wired** — anvil's bundled hooks are non-blocking by design and
  the Codex Stop-block mechanism is unverified on codex-cli 0.130.0; enable it via a
  documented `/hooks`-trusted recipe after verifying. `gate_check.py` refactored to
  share `decide_from_rows`. Verb decisions pinned by `tests/test_codex_hooks.py`
  (incl. a gate-check↔stop-gate parity test); `heartbeat.sh` smoke-tested in
  `tests/test_hooks.sh`.


- **OpenClaw native finish-gate (B42 Phase 2).** anvil's first native OpenClaw
  `definePluginEntry` plugin (`packaging/openclaw/plugin/`): a
  `before_agent_finalize` hook that **blocks an agent from finalizing a turn while
  its claimed anvil task lacks submitted verification evidence** — a blocking gate,
  stronger than anvil's non-blocking Claude-Code hooks. It shells out to a new
  read-only verb, **`anvil gate-check --json`**, which reports `block`/`continue`
  (exit 0 continue / 2 block / 1 error) for the actor's active claim using anvil's
  own accept-path predicate (`review.gates.evidence_complete`). The gate is
  **default-open** (no project / no claim / anvil missing ⇒ finalize normally) and
  **bounded** (revise capped at 3 attempts per run, keyed task+runId). Opt-in
  install recipe via `anvil install openclaw --finish-gate` (anvil registers
  nothing — honoring the OpenClaw no-files contract). Requires
  `plugins.entries.anvil-finish-gate.hooks.allowConversationAccess=true` and
  `anvil` on the Gateway PATH; hook dispatch verified against OpenClaw 2026.6.6.
- **OpenClaw `after_tool_call` evidence auto-capture (B42 Phase 2).** The same
  native plugin now registers an `after_tool_call` hook: when the OpenClaw `exec`
  tool runs a verification command (pytest / ruff check / mypy / npm test / cargo
  test / bun test), it forwards the command + exit code + combined output to the
  existing `anvil hook capture-evidence` verb, appending to the active claim's
  `.anvil/.evidence-buffer/`. The OpenClaw-native equivalent of anvil's Claude-Code
  `capture-evidence.sh` PostToolUse hook — a pure fire-and-forget observer that
  never blocks or fails a tool call. Extractor verified against OpenClaw 2026.6.6's
  exec result shape (`event.result.details.{exitCode,aggregated,cwd}`; stdout+stderr
  are combined).
- **OpenClaw `before_prompt_build` guidance injection (B42 Phase 2).** The native
  plugin injects a short, **cacheable** anvil-usage note into the system prompt for
  anvil-tracked projects (how to `claim`/`submit` + a heads-up that the finish-gate
  blocks finalizing un-evidenced work). Uses `prependSystemContext` (provider
  prompt-cached) and is gated on a single memoized `anvil status` probe per
  workspace — no per-turn shell-out. Returns nothing for non-anvil projects;
  disable via `plugins.entries.anvil-finish-gate.hooks.allowPromptInjection=false`.
  (`session_start` can't inject — it's observation-only — so `before_prompt_build`
  is the injection seam.)
- **OpenClaw `before_tool_call` claim-guard (B42 Phase 2 — completes Phase 2).** The
  native plugin's fourth hook: when a mutating tool (`write`/`edit`/`apply_patch`)
  runs while the actor holds no active anvil claim, it shells out to a new read-only
  verb **`anvil claim-guard`** and acts per mode. **Default `warn`** (log-only, never
  blocks — chosen by a 3-model vote as the only false-block-safe non-inert default);
  opt-in `block` (hard veto) / `require_approval` (interactive only — hard-blocks in
  headless) / `off`. Editing outside a claim's declared scope only warns (advisory).
  Default-OPEN on every uncertain path. `before_tool_call` carries no cwd, so the
  guard reuses the `workspaceDir` cached by `before_prompt_build` (session-keyed),
  with an absolute-path fallback. `claim-guard` decision pinned by stub +
  real-`SqliteBackend` tests. The plugin now hosts all four Phase-2 hooks.

## [0.0.9] — 2026-06-19

### Changed

- **Harness install is now tiered: three supported, the rest MCP-only.** Only the
  harnesses anvil supports end-to-end — **claude-code**, **codex**, **openclaw** —
  get the full native integration; **every other harness is MCP-only best-effort**:
  `anvil install <harness>` now merges *only* the anvil MCP server into its config.
  The `AGENTS.md` instruction splice is kept **only for codex** (openclaw's plugin
  ships it). This shrinks the blast radius behind the earlier config-corruption
  incident — anvil no longer writes instruction files into a dozen third-party
  harnesses. To give an MCP-only agent anvil's usage guidance, point it at the
  repo's `AGENTS.md`. Docs reframed to the two-tier model — supported vs MCP-only
  (`docs/how-to/using-anvil-on-any-harness.md`).

### Removed

- **The neutral `.agents/skills/` drop.** No harness used it anymore (supported
  harnesses ship anvil's skills via their plugin), so install no longer copies
  `SKILL.md` dirs into `.agents/skills/`, the `--json` install envelope no longer
  carries a `skills` key, and the dead `Harness.reads_agents_skills` flag is gone.

### Fixed

- **State is no longer stranded inside individual git worktrees.** anvil kept
  `state.db` at `<cwd>/.anvil/` (or `bin/.anvil/`), so every git worktree got its
  own separate, gitignored database — work tracked in one worktree was invisible in
  another, and the canonical data ended up trapped in whichever worktree last
  touched it. The default state dir is now a per-project **home workspace**,
  `~/.anvil/workspaces/<repo>/.anvil/`, keyed by the canonical git repo
  (`git rev-parse --git-common-dir`) so **all worktrees + the main checkout share
  one `state.db`**. `ANVIL_ROOT` is unchanged (a literal `<ANVIL_ROOT>/.anvil`
  override); `ANVIL_STATE_LAYOUT=local` keeps the legacy in-repo layout. CLI and
  MCP resolve identically. See `docs/how-to/state-location.md`.

- **The one-line installer no longer runs stale, config-corrupting anvil.**
  `scripts/install.sh` cached a shallow checkout at `~/.anvil-src` and updated it
  with `git pull --ff-only || warn` — a **fail-soft** update. When the shallow
  pull failed it kept running whatever stale version was cached; a checkout from
  before the native-install fix still had the old TOML-mangling `install codex`,
  so re-running the installer could re-corrupt `~/.codex/config.toml`. The updater
  now **force-syncs** the cache to the latest `main` (`git fetch --depth 1` +
  `reset --hard FETCH_HEAD`) and **exits loudly** if that fails, rather than
  silently running an old, harmful version.

- **`anvil install` no longer corrupts a user's config or clobbers their files.**
  The Codex path used to hand-edit `~/.codex/config.toml` by parsing and
  re-emitting the whole file; that mangled inline tables and special-character
  keys (`[projects]` paths, `foo@bar` plugin keys) into invalid TOML — Codex
  reported "Failed to load plugins". Two layers of fix:
  - **Codex installs natively now.** `anvil install codex` drives Codex's own CLI
    (`codex plugin marketplace add` + `codex mcp add`) and lets Codex write its
    own config — anvil never text-edits `config.toml`. This deletes the fragile
    TOML splicer entirely (including a latent silent-corruption bug where a
    `[mcp_servers.anvil]` line inside a triple-quoted string dropped the rest of
    the file). `--rollback` runs Codex's own removers. anvil's `plugin.json` gains
    a Codex `interface` block + `skills` pointer so it appears in the Plugins
    panel and ships its skills natively.
  - **Instruction files are spliced, not overwritten.** `AGENTS.md` /
    `copilot-instructions.md` etc. get anvil's content inside a marked, removable
    `<!-- BEGIN ANVIL -->…<!-- END ANVIL -->` block appended after the user's
    text; re-running replaces only that block. Ambiguous pre-existing markers
    make install refuse rather than risk corruption.

### Added

- **`anvil notify-digest` + OpenClaw cron recipes (B42 Phase 1).** New
  `anvil notify-digest` verb — a one-line `needs_review` + blockers summary that
  prints **nothing** on a clean queue, so a recurring OpenClaw `cron … --announce`
  stays silent instead of pinging a channel every interval; it always exits 0 (a
  notifier must never fail a cron run) and `--json` emits the counts.
  `anvil install openclaw` now surfaces the sandbox-allowlist prerequisite (add
  anvil's tools to `sandbox.tools.allow`, or the 24 MCP tools vanish in sandboxed
  turns), and a new `--cron-recipes` flag **prints** opt-in Gateway cron recipes
  (queue probe, nightly reconcile, lease watchdog, finish-gate nudge) — anvil
  registers nothing, honoring the OpenClaw no-files contract.

- **`install.sh --path` — put `anvil` on your PATH.** After the one-line install
  the `anvil` launcher lives inside the checkout, so you can't run it globally.
  Pass `--path` (e.g. `… | sh -s -- codex --path`) and the installer symlinks
  `anvil` into `~/.local/bin` (override with `ANVIL_BIN_DIR`) — opt-in, idempotent,
  and it **never clobbers** a pre-existing `anvil` you put there yourself; it also
  prints a hint if that dir isn't on your `PATH` yet.

- **Codex skill metadata — anvil's 8 skills now show up named in Codex.** Each
  skill ships a minimal `agents/openai.yaml` (`interface:` with `display_name`,
  `short_description`, `default_prompt`, and a `#60a5fa` `brand_color`), so the
  `/skills` picker and Plugins panel render `anvil:claim` … `anvil:state-ops` with
  human labels and a consistent badge instead of blank/auto-titled entries. Claude
  Code ignores the `agents/` dir, so the skill tree stays dual-harness clean.
  Validated against the Codex skill schema (the `interface` keys the loader
  recognizes; `short_description` within OpenAI's 25–64-char scaffolder
  guideline). The "bundle hooks into the Codex plugin"
  headline (backlog B41) needed **no packaging change**: Codex installs anvil from
  the repo root, so `hooks/hooks.json` (using `${CLAUDE_PLUGIN_ROOT}`, the same
  variable real Codex plugins use) already ships and runs on Codex.

- **Codex feature-surface reference — `docs/reference/codex.md`.** A living map of
  the Codex CLI surface (plugins, skills, hooks, MCP, config/profiles,
  sandbox/approval/trust, automations, headless `exec`/`review`, sessions/fork,
  cloud, app-server, memory, notify, instructions) with, for each, where it's
  authoritatively documented (on-disk under `~/.codex/` **and** the official URL)
  and how it relates to anvil — so future work picks Codex surfaces without
  re-discovering them. Verified against `codex-cli 0.130.0`.

- **Native OpenClaw install.** OpenClaw is its own agent platform (not a Claude
  `.mcp.json` bundle, as the old harness row wrongly assumed) — it manages MCP,
  skills, and plugins through the `openclaw` CLI. `anvil install openclaw` now
  drives that CLI: `openclaw mcp add anvil --no-probe --command bash --arg
  <wrapper>` plus `openclaw plugins install anvil --marketplace fakoli/anvil
  --force` (skills + commands via anvil's Claude-compatible marketplace).
  `--no-probe` avoids a cold-start `uv sync` overrunning OpenClaw's 30s connect
  probe (which would half-install); `--force` refreshes the plugin on re-install.
  It touches **none** of the user's
  files — no `.mcp.json`, no `AGENTS.md`, no skills drop — and `--rollback` undoes
  via `openclaw mcp unset` / `plugins uninstall`, only removing the global
  registration when no other project still references it. OpenClaw joins Claude
  Code and Codex as a truly-supported harness.

- **Backups + `anvil install <harness> --rollback`.** Every file an install
  modifies is copied to `<file>.anvil-bak` first and logged to
  `~/.anvil/install-log.json`; `--rollback` undoes the install. Rollback of an
  instruction file **strips only anvil's block**, preserving everything the user
  wrote around it (before or after install) — it never deletes a file the user
  has adopted. The log is keyed per project+harness and refcounts shared resources
  (a project `AGENTS.md` / `.agents/skills/` used by two harnesses is only undone
  when the last one rolls back); for Codex, the global MCP/marketplace removers
  only fire when no other project still references them. The record is persisted
  before any write (crash-safe), directory restores are atomic, broken-symlink
  destinations are refused, and the manifest tracks each path's kind so rollback
  picks the right reversal. Non-Codex harnesses also get anvil's skills dropped
  into the neutral `.agents/skills/anvil-*` location read by most harnesses.

- **Codex automations (`anvil install codex --automations`).** Installs anvil's
  scheduled-automation templates (`anvil-work-queue`, `anvil-sync-reconcile`) into
  Codex's native `~/.codex/automations/` — recurring agent runs that work the
  task queue / reconcile state on a cron schedule. They are installed
  **`status = "PAUSED"`** with the project path filled in (anvil never
  auto-activates a background run on your subscription — you turn them on in the
  Codex app), namespaced per project, and tracked by the same backup/manifest so
  `--rollback` removes them. Templates live under `packaging/codex/automations/`.

- **Surface prior unresolved review findings on file overlap (T017).** When a
  reviewer rejects a task or requests changes at the finish gate, that verdict
  is recorded as a `reject` / `needs_changes` review whose notes carry the
  finding. A later task that intends to touch one of those same files no longer
  starts blind: its work packet now surfaces the outstanding finding so the
  agent can address it or explicitly carry it forward.
  - **Two pure helpers in `review/gates.py`.** A new frozen `DeferredFinding`
    dataclass plus `deferred_findings(reviews, tasks, evidence)` and
    `deferred_findings_for_files(..., expected_files)`. The first derives one
    finding per task-targeted `reject` / `needs_changes` review — its touched
    files being the union of the reviewed task's `likely_files` and the
    `files_changed` of any evidence submitted on it (approvals and
    non-task-targeted reviews are ignored). The second filters those to the ones
    overlapping an incoming claim's files and annotates each with the exact
    overlapping subset. Both are I/O-free and deterministic; they are re-exported
    from `review/__init__.py`.
  - **Wired into both surfaces.** `anvil packet` (CLI) and the
    `generate_work_packet` MCP tool now compute overlap against the active
    claim's `expected_files` (falling back to the task's planner-populated
    `likely_files` before a claim exists) and pass the findings to the renderer.
  - **Rendered in both packet formats.** `render_packet` / `fast_lane_packet`
    gain a `deferred_findings` parameter (defaults to none — fully
    back-compatible). When non-empty the markdown packet grows a "Prior
    unresolved review findings (overlapping files)" section and the JSON packet
    grows a `deferred_findings` array; when empty, neither is rendered.
  - **Import-cycle fix.** `state/transitions.py` now imports `evidence_complete`
    lazily at call time so `review.gates` can be the import entry point (e.g.
    running `tests/test_review.py` standalone) without re-entering a
    partially-initialized module.
  - Covered by `tests/test_review.py`.

---

## [0.0.8] — 2026-06-17

### Changed

- Renamed fakoli-state -> Anvil (the system of record for agent teams). **BREAKING:** CLI command is now `anvil` (was fakoli-state); state dir is `.anvil/` (was .fakoli-state/); env var is `ANVIL_ROOT` (was FAKOLI_STATE_ROOT); import package is `anvil` (PyPI dist `anvil-state`).
  - Anvil (formerly fakoli-state, extracted from the fakoli-plugins monorepo).
  - The Python import package moved from `fakoli_state` to `anvil`; the
    distribution is published as `anvil-state` because the bare `anvil` name is
    taken on PyPI. The console script and MCP launcher are now `anvil` and
    `anvil-mcp`. The companion env vars (`ANVIL_GLOBAL_CONFIG`,
    `ANVIL_TEST_GH_TOKEN`, `ANVIL_TEST_REPO`, `ANVIL_HOOK_DEBUG`) follow the same
    prefix change.

---

## [1.39.0] — 2026-06-17

### Added

- **Fast-lane work packets — right-size process by score (T020).** A task
  whose `complexity` AND `blast_radius` scores are both at or below configurable
  ceilings now routes to a minimal, single-step work packet: the trimmed
  update-protocol prose introduced in T015 *plus* a required-evidence checklist
  trimmed to the single essential field. A trivial change no longer drags an
  agent through a multi-item evidence ceremony, while anything above either
  ceiling (e.g. a one-line change that touches a 5/5-blast schema/config/public-API
  surface) — or any unscored task — keeps the full packet, the safe default.
  - **Configurable ceilings.** Two new `config.yaml` keys,
    `fast_lane_complexity_max` (default 2) and `fast_lane_blast_radius_max`
    (default 2), let a project widen or narrow the fast-lane without touching
    code. Both are validated to the 1-5 score scale (booleans rejected); absent
    keys fall back to the renderer's built-in conservative defaults, so every
    pre-T020 config keeps its exact prior packet routing.
  - **New config-aware seam.** A new public `context.packets.fast_lane_packet`
    helper reads those two ceilings and drives `is_lightweight` / `render_packet`,
    giving CLI and MCP callers one obvious entry point instead of re-deriving the
    `lightweight` decision at every call site. `render_packet` gains a
    `fast_lane_required_evidence_max` parameter (default 1; negative disables the
    evidence trim) and now exports `FAST_LANE_REQUIRED_EVIDENCE_MAX`.
  - **Wired into both surfaces.** `anvil packet` (CLI) and the
    `generate_work_packet` MCP tool now route the fast-lane from the project's
    config when one is loadable, and fall back to `render_packet` with built-in
    defaults when there is no/broken `config.yaml` — a broken config never blocks
    packet generation.
  - **Packet-shape only — never the evidence ledger.** The trim is purely a
    rendering concern: the task's stored `Verification.required_evidence` is
    never mutated, the JSON packet still carries the full declared list for audit
    (alongside the right-sized `required_evidence` the agent is shown), completion
    still records the same immutable evidence transition, and the review gate
    still reads the full stored list. The fast-lane is advisory right-sizing, not
    a back-door that weakens the evidence record.
  - Covered by `tests/test_config.py` and `tests/test_context.py`.

---

## [1.38.0] — 2026-06-17

### Added

- **Structured acceptance grammar (EARS / Gherkin) in the PRD parser
  (T028/F007).** Acceptance criteria can now carry machine-readable intent: a
  new public `parse_acceptance_grammar` helper in `planning/template.py`
  decomposes each criterion into a structured `AcceptanceClause`
  (`text`, `kind`, `clauses`) when an EARS or Gherkin grammar is present, and
  falls back to a `freeform` clause otherwise — so the same loop that already
  reads a PRD's bullet criteria can now act on *why*/*when*/*then* structure,
  not just opaque strings.
  - **EARS recognition (single-line).** Handles the conditional forms
    `WHEN <trigger> THEN <response>`, `WHEN <trigger>, THE SYSTEM SHALL
    <response>`, plus `WHILE`/`WHERE`/`IF` preconditions (including a nested
    `WHILE x, WHEN y, the system shall z`), and the ubiquitous
    `THE SYSTEM SHALL <response>` form. A conditional clause is only classified
    as EARS when it actually yields a response, so an ordinary sentence merely
    *starting* with "When" is not misclassified — it falls through to freeform.
  - **Gherkin recognition (single- and multi-line).** Parses
    `Given <context> When <event> Then <outcome>` whether written inline or as
    separate Given/When/Then bullets in the same criteria block; `And`/`But`
    continuations fold into the nearest preceding keyword, and structural noise
    lines (`Scenario:`, `Feature:`, `Background:`) are skipped. A minimal viable
    scenario requires at least a `When` and a `Then`.
  - **Purely additive — never raises, never does I/O.** The canonical
    `Task.acceptance_criteria` list of raw strings is unchanged; callers opt in
    to the structured view, and a freeform PRD parses exactly as before. The
    raw criterion text is always preserved verbatim on the clause (multi-line
    Gherkin blocks collapse several input strings into one clause, joined by
    newlines). `AcceptanceClause` and `parse_acceptance_grammar` are exported
    from `planning.template`.
  - Covered by `tests/test_parser.py` (`-k acceptance_grammar`, 20 cases)
    spanning EARS when/then/shall/while/where/if, inline and multi-line Gherkin,
    freeform fallback, and the round-trip that a PRD with structured criteria
    parses into clauses while a freeform PRD still parses unchanged.

---

## [1.37.0] — 2026-06-17

### Added

- **Batch dependency-edit primitive — atomic, cycle-detecting (T022/F007).**
  A new way to rewire task dependencies in bulk, exposed both as the
  `anvil deps` CLI command and the `edit_dependencies` MCP tool, so a
  human or an agent can add and remove many dependency edges in a single
  transaction instead of mutating tasks one at a time.
  - **Edge model.** Each edge is `SOURCE:TARGET` (CLI) / `[source, target]`
    (MCP), meaning *source depends on target*. `--add` / `--remove` (CLI) and
    `add` / `remove` (MCP) are both repeatable and may be mixed in one call;
    the CLI also accepts the arrow form `SOURCE->TARGET`.
  - **Atomic, all-or-nothing.** The entire batch is planned and validated up
    front. Any unknown task, self-dependency, or edit that would introduce a
    dependency cycle rejects the WHOLE batch with zero events appended — there
    is never a partial application. On success exactly one `task.created`
    upsert is emitted per task whose dependency set actually changed (no-op
    edges are skipped), and each task's status is preserved.
  - **Structured results.** `--json` (CLI) and the tool response return
    `changed` (tasks mutated) plus the `added` / `removed` edges that took
    effect. Rejections carry a machine-readable `code` (`cycle`,
    `unknown_task`, `self_loop`, `bad_request`); the CLI exits 1 on a rejected
    batch and 2 on a malformed request.
  - Implemented via new `planning/_plan_helpers.py` primitives
    (`parse_dep_edge`, `plan_batch_dep_edits`, `emit_batch_dep_events`,
    `DepEdge`, `BatchDepError`); the MCP server now exposes 24 agent-facing
    tools. Covered by new tests in `tests/test_cli_plan.py` and
    `tests/test_mcp.py`.

---

## [1.36.0] — 2026-06-17

### Added

- **Docker MCP catalog packaging for the FastMCP stdio server (T021/F006).**
  anvil's MCP surface can now be published to (and run from) the Docker
  MCP catalog as a self-contained stdio server image, so a host without a local
  Python/uv toolchain can launch the full PRD → plan → review → claim → apply
  tool set by pulling one image and bind-mounting a project directory.
  - **Repo-root `Dockerfile`** builds the engine on the distroless-friendly
    `ghcr.io/astral-sh/uv` base, installs dependencies from `bin/uv.lock` in a
    cached layer (frozen, no dev/provider extras), runs as a non-root user, and
    sets `ENTRYPOINT ["python", "-m", "anvil.mcp_server"]`. The image is
    stateless — project state lives under `.anvil/` in a bind-mounted
    host directory resolved via `ANVIL_ROOT` (defaulting to `/project`,
    declared as a `VOLUME`), never baked into the image.
  - **`server.yaml` Docker MCP catalog manifest** (mirroring
    `docker/mcp-registry` conventions): server metadata, `productivity`
    category, tags, the `{{anvil.project_path}}:/project` volume mount,
    the `ANVIL_ROOT` env wiring, and a `project_path` parameter — plus
    inline submission steps for opening the registry PR.
  - **`--help` / `--version` entry point on the MCP server** — a new
    `main(argv)` short-circuits before `mcp.run()` so a container smoke test
    (`docker run --rm anvil-mcp --help`) prints and exits 0 *without*
    blocking on stdio. The help page is self-contained (no backend/project
    access, so it works in a bare image), introspects the live FastMCP tool
    list (it can never drift from the registered surface), documents
    `ANVIL_ROOT`, and rejects unknown flags with exit 2 rather than
    silently starting the server. The no-argument path (start the blocking stdio
    server) is byte-for-byte unchanged.
  - **`.dockerignore`** keeps the build context minimal and deterministic (only
    `bin/{pyproject.toml,uv.lock,src}` and `README.md` reach the image).
  - **Publishing walkthrough** documented in `docs/mcp.md` (build, smoke test,
    bind-mount run, and the catalog submission flow).
  - Covered by `tests/test_docker_mcp_catalog.py`: in-process entry-point checks
    (`--help`/`--version` print and exit 0, unknown flags exit 2, the help page
    lists every registered tool) run always in CI; the real
    `docker build` / `docker run` smoke test is guarded behind a
    Docker-availability skip.

---

## [1.35.0] — 2026-06-17

### Added

- **`graph --format mermaid` dependency/state diagram (T019/F008).** A new
  read-only `anvil graph` command renders the persisted task graph as a
  copy-pasteable diagram. `--format mermaid` emits a deterministic Mermaid
  `graph LR` flowchart whose nodes are tasks (labelled with ID, title, and
  status, and coloured per status via `classDef`) and whose edges are
  dependencies oriented `dep --> task` — the same node/edge/ready-to-claim view
  the MCP `get_dependency_graph` tool exposes, so the two surfaces never drift.
  - **Three output formats.** `--format text` (default) prints a short
    human-readable summary (node count, edge count, by-status breakdown,
    ready-to-claim list); `--format mermaid` prints the flowchart; `--json` (or
    `--format json`) emits the v1.24 envelope under `data` with `nodes`,
    `edges`, `ready_to_claim`, and — when `--format mermaid` is requested — the
    rendered `diagram` string alongside the structured graph.
  - **Scoping.** `--scope all` (default) renders the whole project,
    `--scope feature --target F001` narrows to one feature, and
    `--scope task --target T007` renders a task plus its transitive
    dependencies — mirroring the MCP tool's scope semantics exactly.
  - **Deterministic, pipe-safe rendering.** Nodes are emitted in task-ID order
    and edges in `(from, to)` order so identical state always produces
    byte-identical output. Subtask IDs like `T001.1` are sanitised to
    Mermaid-safe node IDs, labels are HTML-escaped, and an empty scope still
    yields a valid (renderable) diagram rather than a broken block. Invalid
    `--scope`/`--format` values and a bad `ANVIL_ROOT` still return a
    parseable error envelope under `--json`.

---

## [1.34.0] — 2026-06-17

### Added

- **Non-feature task types (bugfix / refactor / modify) through the
  PRD-to-claims loop (T015/F005).** Tasks are no longer implicitly "features":
  a new `TaskType` enum (`feature` / `bugfix` / `refactor` / `modify`) flows end
  to end — `plan` → `score` → `claim` → work-packet → evidence — so a brownfield
  or maintenance PRD can describe repairs and restructuring as first-class work
  instead of dressing every item up as net-new capability.
  - **`**Type:** feature|bugfix|refactor|modify` in the PRD task block.** The
    template parser reads an optional `**Type:**` field (documented in
    `docs/prd-template.md`); an unknown value falls back to `feature` with a
    non-fatal parse warning rather than aborting the parse — the same forgiving
    policy `**Priority:**` already uses.
  - **`--type` filter on `list` and `next`.** `anvil list --type bugfix`
    and `anvil next --type refactor` scope to a single type, pushed down
    to SQL via a new `task_type` parameter threaded through
    `Backend.list_tasks` / `SqliteBackend.list_tasks`,
    `ClaimManager.next_claimable`, and the MCP `list_tasks` tool. The `list`
    table and `--json` `filters` object both surface the type; omitting the flag
    keeps the pre-T015 behaviour (all types eligible).
  - **Score-routed lightweight work-packet variant.** A new pure
    `is_lightweight(task)` predicate (`context/packets.py`) routes a task whose
    six-dimension score puts *both* complexity and blast_radius at/below
    conservative ceilings (`LIGHTWEIGHT_COMPLEXITY_MAX` = 2,
    `LIGHTWEIGHT_BLAST_RADIUS_MAX` = 2) to a trimmed packet — the update-protocol
    section collapses to the single submit line, dropping the heartbeat reminder
    and status-flow walkthrough. Every load-bearing section (goal, acceptance
    criteria, scope, verification, claim) is byte-for-byte identical in both
    variants; an unscored or high-blast task always gets the full packet.
    Routing is by *score*, never by type, so a high-blast `refactor` is treated
    exactly like a high-blast `feature`. `WorkPacket` gains a `variant`
    (`"lightweight"` / `"full"`) field, echoed into `json_data["variant"]` for
    MCP consumers; the thresholds are parameters so T020 can later drive them
    from config.
  - **Schema v4 → v5 (auto-upgrade, purely additive).** `tasks` gains
    `task_type TEXT NOT NULL DEFAULT 'feature'`; the `DEFAULT` backfills every
    pre-v5 row to `feature` — exactly its original meaning — so no data
    migration is required. The on-open upgrade path retrofits the column
    idempotently across every legacy version (`0/1 → 5`, `2 → 5`, `3 → 5`,
    `4 → 5`) via a new `_ensure_task_type_column` ALTER wrapped in
    duplicate-column tolerance. `TaskCreatedPayload.task_type` defaults to
    `feature` so a payload predating the enum (or any minimal hand-rolled
    caller) deserialises unchanged, preserving the replay byte-equality
    invariant.
  - A bugfix PRD item now produces a typed task that claims, executes, and
    submits evidence end-to-end. Covered by new cases in `tests/test_models.py`
    (`-k task_type`), `tests/test_template.py`, `tests/test_context.py`,
    `tests/test_sqlite.py`, `tests/test_mcp.py`, `tests/test_schema_version.py`,
    and a new `tests/test_task_type_e2e.py`. `docs/cli-reference.md` and
    `docs/migrations.md` document the `--type` filter and the v5 migration.

---

## [1.33.1] — 2026-06-17

### Changed

- **Standalone-first onboarding docs (T011/F002).** The Getting Started
  walkthrough and the README now lead with a crew/flow-free path — every step
  (`init` → `prd` → `plan` → `claim` → `execute` → `finish`) is driven entirely
  through the `anvil` CLI (or the equivalent MCP tools), so a new user
  can run the full loop without `fakoli-flow` or `fakoli-crew` installed and
  without Claude Code.
  - `docs/how-to/getting-started.md` gains an explicit **"Optional:
    fakoli-flow / fakoli-crew integration"** section, clearly marked as
    *purely additive* — the sibling plugins are an opt-in orchestration upgrade
    over the same state engine, never a prerequisite, and nothing in the
    standalone walkthrough degrades without them.
  - `README.md` reframes the integration section as "Optional:" and splits
    **Requirements** into **Required** (Python 3.11+ with `uv`) vs **Optional**
    (Claude Code, fakoli-flow, fakoli-crew — each additive), correcting the
    prior "recommended" framing that implied the siblings were needed.

### Added

- **Docs self-sufficiency gate.** New `tests/test_standalone_docs.py` enforces
  the standalone-first contract so the onboarding story cannot silently rot
  back into "you also need flow/crew": it mirrors the backlog verification
  (`grep -L -e 'flow:' -e 'crew:' docs/how-to/getting-started.md`) — the
  standalone walkthrough references no `flow:`/`crew:` command token — and adds
  structural checks that the standalone path leads and that an explicitly
  optional, additive integration section exists.

---

## [1.33.0] — 2026-06-17

### Added

- **`anvil claim --branch NAME` — caller-supplied / existing-branch
  claims (T027).** A claim can now attach to a branch the user names directly
  instead of always generating the default `agent/<task>-<slug>` name, so the
  engine meets an agent or human where their own git workflow already is rather
  than forcing every claim onto a synthesized branch.
  - **New `--branch` flag on `claim`.** When supplied, the branch is resolved
    *before* the claim is recorded: an existing local branch is checked out
    (`git checkout NAME`), and a non-existent one is created
    (`git checkout -b NAME`). The name is used verbatim — no slugging — so the
    caller controls the exact ref; git itself rejects invalid ref names, which
    surfaces as a non-fatal branch warning rather than a failed claim.
  - **The resolved branch is recorded on the claim.** Unlike the default path
    (where the auto-generated branch is reported but not stored), a
    `--branch` name is persisted on the `Claim` and the claims row, so project
    state reflects the branch the work actually lives on. `ClaimManager.claim()`
    gains an optional `branch` parameter threaded into the constructed `Claim`;
    the `ClaimResult` carries it back.
  - **Non-git working trees are tolerated.** If git is unavailable or the
    directory is not a repo, the claim still succeeds — the requested branch
    name is recorded on the claim to preserve intent, and a branch warning is
    surfaced exactly like the default path (same no-fail contract as
    `create_branch_for_task`).
  - **Default path is byte-for-byte unchanged.** Without `--branch`, the
    auto-generated `agent/<task>-<slug>` branch is created *after* the claim and
    reported (not stored) as before. The CLI now prefers the branch recorded on
    the claim when emitting both the `--json` envelope and human output, falling
    back to the checkout result for the default path.
  - New `use_named_branch()` in `git_ops/branch.py` (checkout-or-create against a
    verbatim name, optional `base` ref). Covered by new cases in
    `tests/test_cli.py`.

---

## [1.32.0] — 2026-06-17

### Added

- **`anvil prd resolve-decision` — decision back-propagation into the
  PRD (T018).** Closes the loop opened by `prd find-decisions`: an answered
  decision can now be written back into `prd.md` itself, not just tracked in a
  reviewer's head, so the PRD stays the single source of truth instead of
  drifting from the conversation that resolved its open questions.
  - **Resolves a decision by its detector id.** Takes a `DECISION_ID` exactly as
    `prd find-decisions` reports it (`ND-001` / `OQ001` / `MF-T012-AC`) plus a
    `--resolution` answer, locates the referenced PRD span via the **same**
    `find_unresolved_decisions` detector (no second, drift-prone parser), and
    edits only that span.
  - **Kind-aware back-propagation, never a blind overwrite.** A `[NEEDS
    DECISION]` marker is rewritten inline on its linked requirement; an open
    question is moved into a `## Decisions` section; a missing acceptance/
    verification field is added under its task block. Unrelated PRD content is
    left byte-for-byte intact — the new `apply_decision_to_markdown` engine in
    `planning/decisions.py` rewrites the targeted span and nothing else, and
    raises `ResolutionError` rather than guessing when the anchor no longer
    matches.
  - **Recorded as an additive, replayable audit fact.** A new
    `prd.decision_resolved` event (payload `PrdDecisionResolvedPayload`,
    transition `prd_decision_resolved`) is appended to the log capturing the
    decision id, kind, PRD ref, resolution, resolver, and the exact
    `before`/`after` span that changed. It is **audit-only** — it mutates no
    SQLite table and replays as a no-op — so the immutable event log records
    *that* a decision was answered while the PRD source itself remains the
    canonical text (refresh `state.db` with a later `prd parse`). The transition
    refuses to resolve a decision on a `rejected` PRD (`wrong_status`) and
    rejects blank ids/refs/resolutions (`invalid_input`).
  - Honors the v1.24 `--json` envelope and `--by` / `--file` / `--cwd`
    resolution. Covered by new cases in `tests/test_decisions.py`,
    `tests/test_transitions.py`, and `tests/test_cli.py`.

---

## [1.31.0] — 2026-06-17

### Added

- **Global-config layer `~/.config/anvil/config.yaml` with project
  override (T016/F006).** A user-wide config now supplies defaults that any
  individual project can override, so settings like `default_lease_minutes`,
  `llm_tier`, or `auto_expand_threshold` can be set once per machine instead of
  re-declared in every project's `.anvil/config.yaml`.
  - **Strict precedence, lowest → highest:** built-in dataclass default <
    global config (`~/.config/anvil/config.yaml`) < project config
    (`.anvil/config.yaml`) < explicit CLI flag. A key set in the project
    config wins over the same key in the global config; a global-only key
    supplies the value; a key in neither falls through to the dataclass default.
  - **New `load_merged_config()` and `global_config_path()` in `config.py`.**
    The merge is a shallow project-over-global overlay validated against the
    MERGED mapping, so a global config MAY supply a default `project_name` /
    `project_id` that a project overrides — but it is never *required* to carry
    them, and a project missing them still raises the same `ValueError` when the
    global layer does not supply them either. Per-project paths (`db_path` /
    `events_path`) always resolve relative to the PROJECT config's directory,
    never `~/.config`. `load_config()` stays a single-file load (no merge) for
    callers that want it; the single-file YAML parse and Config construction are
    factored into shared `_read_yaml_mapping` / `_build_config` helpers so both
    paths validate identically.
  - **Global-path resolution honours XDG.** `global_config_path()` resolves
    `ANVIL_GLOBAL_CONFIG` (explicit file override, for tests / unusual
    installs) > `$XDG_CONFIG_HOME/anvil/config.yaml` > the documented
    `~/.config/anvil/config.yaml` default. A missing or empty global
    config means "no global defaults" rather than an error; a *broken* global
    config the user explicitly wrote raises the same loud `ValueError` /
    `YAMLError` as a broken project config.
  - **CLI consumers now load the merged config.** `claim`, `renew`, and `plan`
    read through `load_merged_config()`, so global planning/lease defaults apply
    everywhere the project config used to be the only source.
  - **New `--lease` flag on `claim` and `renew`.** An explicit `--lease N`
    (minutes) overrides the merged project>global `default_lease_minutes`, and
    works even in a project with no `config.yaml` at all — closing the top of the
    lease precedence chain (flag > project > global > built-in 60).
  - `docs/cli-reference.md` documents the global layer, the precedence order,
    and the `--lease` flag. Covered by new cases in `tests/test_config.py`
    (`TestGlobalConfigPath`, `TestLoadMergedGlobalConfig`,
    `TestGlobalConfigLeasePrecedence`), including the acceptance scenario where a
    global default lease of 45 is overridden to 30 by a project config and to 15
    by the CLI flag.

---

## [1.30.0] — 2026-06-17

### Added

- **`next_ready` field in finish/submit responses (CLI + MCP) (T014).** The
  surfaces an agent hits when it *finishes* a task now hand back the next safe
  task to pick up, so a multi-agent loop can chain finish → claim without a
  separate `next` round-trip. Both response paths carry a `next_ready` object
  (`{id, title, priority}`) or `null` when the ready queue is empty.
  - **CLI `apply` `--json`** gains `next_ready` in **both** review-only and
    decision modes, and the **submit** path emits it too — a stable key set
    script authors can rely on regardless of which apply mode ran.
  - **MCP `apply_review_decision` and `submit_completion_evidence`** return a
    typed `NextReadyTask | None` field with the same shape, so the agent-facing
    tools and the CLI stay in lockstep.
  - **Suggestion is file-conflict-aware, not just the raw queue head.** Both
    surfaces compute the field through the new
    `ClaimManager.next_ready_excluding_active_files()` helper, which starts from
    `next_claimable` (priority desc, complexity asc, created_at asc; honoring
    status, dependencies, active claims, and conflict groups) and adds ONE
    stricter exclusion: a candidate is skipped when its `likely_files` overlap
    the `expected_files` of any active claim held by *another* actor. The named
    "next ready" task can never be one whose files collide with work another
    agent is already holding — and re-suggesting work the same actor already
    holds is not treated as a conflict.
  - `docs/mcp.md` documents the field on both tools. Covered by new cases in
    `tests/test_claims.py`, `tests/test_json_output.py`, and `tests/test_mcp.py`.

---

## [1.29.0] — 2026-06-17

### Added

- **`anvil describe` self-describing command surface + `--version` schema
  reporting (T012/F003).** The engine now exposes a machine-readable manifest of
  its own public surface so a non-Claude host (Codex/Cursor/CI/another agent) can
  discover what it can do without parsing `--help` text or hard-coding a tool list.
  - **`anvil --version` now reports engine AND schema version** —
    `anvil 1.29.0 (schema 4)`. The first token stays
    `anvil {__version__}` for backward compatibility with existing parsers;
    the `(schema N)` suffix identifies the on-disk state format the engine speaks,
    which a host pinning behaviour needs alongside the build id.
  - **New `describe` command** emits the standard `{"ok": true, "command":
    "describe", "data": {...}}` envelope by default (it is inherently
    machine-readable), carrying `api_version`, `engine_version`, `schema_version`,
    `envelope`, the sorted list of every CLI subcommand (grouped commands rendered
    as their full invocation path, e.g. `prd parse`), and the sorted list of every
    MCP tool name — each with a `count`. `--human` prints a readable summary;
    `--json` is an accepted no-op for flag symmetry. Needs no project: it never
    opens a backend and works from any directory, even before `init`.
  - **Stable `api_version` to pin against.** `__version__` bumps on every release
    (often metadata-only), but the shape of the command surface changes far less
    often. `api_version` (currently `"1"`) only changes when the surface changes
    in a way consumers must react to — a CLI command or MCP tool added, renamed,
    or removed, or the `--json` envelope shape changing — so a host can pin the
    contract without re-pinning on every patch release.
  - **No drift, no hand-maintained list.** The manifest is introspected live from
    the registered Typer app (via `typer.main.get_command`) and the FastMCP
    instance (via `mcp.list_tools()`) at call time, so the described surface is
    generated from the exact objects the CLI and MCP server actually expose —
    including `describe`/`describe_surface` themselves.
  - **New MCP tool `describe_surface` (tool 23)** is the agent-facing counterpart:
    it returns the identical manifest dict by reusing the CLI's `build_manifest`
    (single source of truth), so an MCP-only host gets the same self-description
    without shelling out to the CLI.
  - Covered by `tests/test_cli.py::TestDescribe` (7 cases), which asserts the
    described CLI commands match the registered Typer commands and the described
    MCP tools match the live FastMCP registry — so CI fails if a command or tool
    is added or renamed without the surface staying coherent.

---

## [1.28.0] — 2026-06-17

### Added

- **`anvil doctor` one-shot health diagnosis (T010/F002).** A read-only
  triage command that answers "is this project's state healthy?" in a single
  pass, emitting a list of severity-tagged findings (`ok`/`info`/`warning`/`error`)
  and **exiting non-zero when ANY finding is ERROR-level** — a real health gate,
  unlike `drift`'s always-0 report. Five probes:
  - **state.db reachability + schema version** — reads the TRUE on-disk
    `PRAGMA user_version` *before* open (so a re-stamp on open can't mask drift)
    and reports it against the engine-targeted `SCHEMA_VERSION`. An
    un-migratable / unknown schema is the canonical "your install needs
    migration" signal — an ERROR. A migrated-on-open db is healthy with an
    `info` note.
  - **config parse status + effective lease/heartbeat** — does `config.yaml`
    parse, and what lease/heartbeat would a claim actually use? A broken config
    is a WARNING (the CLI falls back to the built-in 60/5-minute defaults), not
    a hard error; a missing config is `info`.
  - **active / stale claim counts** — counts active claims and how many have an
    expired lease (work silently wedged until a mutating command reaps it).
    Read-only: it compares leases against the clock WITHOUT reaping. A stale
    claim is an ERROR.
  - **replay integrity** — rebuilds canonical state from `events.jsonl` into a
    scratch db via the same `replay_from_empty` engine `replay` uses, then
    byte-compares it against the live `state.db` through the `serialize_state`
    snapshot the SL-1 replay-equivalence test uses. A mismatch (the event log no
    longer reproduces the projection) is an ERROR.
  - **reconciliation drift summary** — the local INTENT/STATE/FS/GIT drift
    `drift` reports (orphan branches/worktrees/packets, missing expected files),
    reusing `ReconciliationEngine.scan()` with no providers. A non-empty drift
    set is a WARNING (a report, not a gate); stale claims are not re-escalated
    here since the claims probe already counts them as an ERROR.
  - **Degrades gracefully.** Every probe is wrapped so one failed check (corrupt
    config, unreadable db) produces a finding rather than a traceback, and the
    rest of the report still runs. When the backend can't open (schema mismatch),
    replay/reconciliation are reported as skipped `info` findings — but the
    stale-claim probe falls back to a narrow read-only SELECT against the
    version-stable `claims` columns, so a project with BOTH an injected stale
    claim and a schema mismatch lists BOTH findings, not just the schema one.
  - Honors the v1.24 `--json` envelope (`{"ok": true, "command": "doctor",
    "data": {"healthy", "worst_severity", "findings": [...]}}`) and the
    `ANVIL_ROOT` / `--cwd` resolution precedence. Covered by
    `tests/test_cli.py` (11 cases, incl. the healthy-clean-exit and
    stale-claim-plus-schema-mismatch acceptance scenarios).

---

## [1.27.0] — 2026-06-17

### Added

- **`anvil scan` brownfield ingest + `init --from-repo` (T008).** A new
  command takes an *existing* repo from nothing to a ready-to-claim task graph
  with no PRD authoring, no LLM, and no API key. `scan` walks the working tree,
  persists a re-scannable **codebase model** in its own SQLite db
  (`.anvil/scan.db`), and — on the first scan of a project with no PRD yet
  — synthesises a draft `prd.md` plus an initial feature/task graph by driving
  the same offline parse → plan → score → review pipeline `init --with-sample`
  uses. `init --from-repo` is the convenience entry point: it scaffolds
  `.anvil/` like a bare `init`, then immediately runs the scan.
  - **Separate `scan.db` keeps the replay/audit guarantee intact.** The codebase
    model lives in its own database, never in the event-sourced `state.db`, so
    the canonical event log and its byte-equality replay invariant are untouched.
  - **Re-scan reports a delta, never clobbers.** A second `scan` reconciles the
    refreshed working tree against the persisted model and reports added /
    removed / changed files instead of overwriting the seeded graph. An authored
    or already-parsed `prd.md` is left alone unless `--force` is passed — only a
    project with no PRD yet (the common brownfield first run) gets seeded.
  - **Fully offline / no API key.** Seeding reuses the generalised
    `cli._sample.seed_pipeline_from_prd` engine (extracted from the sample path)
    so the brownfield seed drives the exact parse → plan → score → review modules
    the per-command CLI bodies use and cannot drift from the hand-run sequence.
    The generated draft PRD carries `## Features`/`## Tasks` sections with
    non-empty acceptance-criteria + verification blocks, so the run ends with at
    least one task in `ready` and `anvil next` returns a first task.
  - **`--from-repo` is mutually exclusive with `--with-sample`** (both own
    `prd.md` and seed the graph; the combination is refused up front) and honours
    the v1.24 conventions — `ANVIL_ROOT` resolution via the shared helpers
    and the single `--json` envelope. Covered by `tests/test_scan.py`.

---

## [1.26.0] — 2026-06-17

### Added

- **`anvil init --with-sample` one-command quickstart (T004).** A single
  command now takes a brand-new user from nothing to a ready-to-claim task with
  no PRD authoring, no LLM, and no API key. Passing `--with-sample` to `init`
  scaffolds the `.anvil/` directory as before, then writes a
  self-contained sample `prd.md` (a fictional "Markdown Link Checker" CLI) and
  runs the full deterministic pipeline offline — parse → review → approve →
  plan → score → review tasks — leaving at least one task in `ready` so
  `anvil next` returns a first task immediately.
  - **Fully offline / no API key.** The sample PRD already contains a `## Tasks`
    section, so `plan` never reaches its LLM task-generation backstop. Seeding
    drives the same engine modules the per-command CLI bodies use
    (`planning.template.parse_prd`, `planning.inference.infer_all`,
    `planning.scoring.score_task`, `state.transitions`) so the seeded path
    cannot drift from the hand-run command path.
  - **Every sample task carries non-empty `**Acceptance criteria:**` and
    `**Verification:**` blocks** — exactly the gate `review tasks` enforces for
    the `drafted → reviewed → ready` promotion — so the run actually ends with
    ready tasks rather than an empty queue.
  - **Atomic with `init`.** The seed runs inside the same backend session that
    `init` opens, so the sample pipeline is applied in one shot. The sample PRD
    is embedded as a module constant (`_sample.SAMPLE_PRD`) rather than a data
    file, since the wheel only packages `src/anvil`.
  - **Opt-in and back-compatible.** Without the flag, `init` behaviour is
    unchanged. The confirmation output reports the seeded feature/task/ready
    counts and points straight at `anvil next`.

---

## [1.25.1] — 2026-06-17

### Added

- **Token-footprint CI budget gate (T013/F003).** A self-audit that enforces an
  explicit token budget on the plugin's skill surface, so the always-on context
  tax cannot creep up unnoticed. `tests/test_token_budget.py` measures, against a
  documented baseline-plus-headroom budget, (1) the combined **always-loaded skill
  frontmatter** — the command surface Claude Code injects into the system prompt on
  *every* turn whether or not a skill fires — capped at
  `ALWAYS_LOADED_FRONTMATTER_BUDGET` (1000 tok, baseline ~688); (2) any single
  `SKILL.md` body via `PER_SKILL_FULL_CEILING` (6000 tok, largest `plan` ~5097); and
  (3) all bodies combined via `TOTAL_FULL_BUDGET` (40000 tok, baseline ~33200).
  - **No tokenizer dependency** — counts use the deterministic `ceil(chars / 4)`
    heuristic rather than pulling in `tiktoken`, so the gate is stable across
    machines and Python versions and needs no network fetch. It catches *relative
    growth* (a skill quietly doubling) without flapping on ordinary edits.
  - **Reports every offender, not just the first** — a single failing CI run prints
    per-skill token counts so all the trimming work is named at once.
  - **Doc and gate can't silently drift** — `docs/context-budget.md` is the
    human-readable contract (budgets, baseline table, "changing a budget" guidance),
    and `test_budget_is_documented` asserts the doc exists and names every budget
    knob. Raising a budget requires updating both in the same change.
  - `docs/architecture.md` cross-references the budget and the gate from the skills
    section.

---

## [1.25.0] — 2026-06-17

### Added

- **`anvil migrate state` command (T009/F006).** Promotes the schema
  migration that previously only ran **silently inside `initialize()`** — on the
  first command after an engine upgrade — to an explicit, backed-up,
  dry-run-by-default operation. It does **not** introduce a new migration
  framework: it runs the exact ordered, idempotent forward branches (`0/1→4`,
  `2→4`, `3→4`) that already live in `SqliteBackend._check_schema_version`.
  - **Detects the TRUE on-disk version** via the `read_db_schema_version`
    accessor (read-only — detection never migrates as a side effect).
  - **Dry-run by default** — reports `from → to` and exits without touching the
    db; `--yes` applies.
  - **Backs up `state.db`** (and any `-wal`/`-shm` sidecars) to
    `state.db.pre-schema-migration.bak` before mutating, and refuses to clobber
    a leftover backup from a prior attempt.
  - **Refuses while any claim is active** — same guard as `migrate-events`;
    listing active claims on the dry-run path uses a scratch copy so the live db
    is never migrated before `--yes`. A db version newer than the engine
    (`schema_too_new`) is surfaced cleanly instead of failing mid-apply.
  - **Idempotent** — once at the current `SCHEMA_VERSION`, re-running is a
    reported no-op that mutates nothing.
  - **`--json`** emits the standard envelope (`{"ok": true, "command": "migrate
    state", "data": {"from_version", "to_version", "applied", "migrated",
    "backup"}}`). `migrate-events` stays a top-level command; `migrate` is now a
    sub-app grouping `migrate state`.
- `docs/migrations.md`: documents the explicit-migration flow, behaviour, and its
  relationship to the `replay` escape hatch (in-place row-preserving vs. rebuild
  from the audit log). Covered by new cases in `tests/test_snapshot.py`.

---

## [1.24.0] — 2026-06-17

### Changed

- **Trimmed agent descriptions.** Moved the `<example>` blocks out of every agent's
  frontmatter `description` and into the agent body, shrinking the always-on context
  footprint from 7,248 → 5,414 tokens (−25%). The triggering examples still live in each
  agent file — just below the frontmatter rather than inside it — so dispatch guidance is
  preserved without paying the token cost on every session load.

### Removed

- **The monorepo-only `marketplace-scribe` agent.** Its duties — keeping the root
  `README.md` plugins table, `.claude-plugin/marketplace.json`, and `registry/*.json` in
  sync — only exist inside the fakoli-plugins monorepo, not in a standalone anvil
  checkout. `docs-scribe` and `state-keeper` are refocused for the standalone surface, and
  marketplace-level regeneration defers to `fakoli-crew:keeper` when that plugin is present.
  anvil now ships **5 plugin-owned agents**: `critic`, `docs-scribe`, `planner`,
  `sentinel`, `state-keeper`.

---

## [1.23.8] — 2026-06-17

### Added

- **`anvil drift` command** (read-only): surfaces three-source divergence —
  INTENT (PRD/tasks) vs STATE (SQLite) vs FILESYSTEM/GIT — by reusing the reconciliation
  engine (no duplicated logic). Reports `missing_expected_file` (a done/accepted task
  whose `likely_files` are absent on disk — a new engine check), `orphan_branch`,
  `orphan_worktree`, `orphan_packet`, and `stale_claim`. Works **without** a configured
  sync provider (local drift only), emits the `--json` envelope, exits 0 (a report, not a
  gate), and honors `ANVIL_ROOT`. This is the "is the code still what the spec
  said?" view competitors structurally lack.

### Fixed

- **Reconciliation: orphan-packet scan never fired.** `_scan_orphan_packets` used a
  doubled `.anvil/.anvil/packets` path under the real CLI/MCP calling
  convention, so orphan packets were undetectable via both `drift` and `sync` (only
  unit tests that pass the project root masked it). Now resolves correctly.
- **Expected-file path normalization corrupted dotfiles.** `str.lstrip("./")` stripped a
  character set, mangling `.env` → `env`, `.github/workflows/ci.yml` → `github/...`, and
  `../x` → `x`. Replaced with a proper leading-`"./"` prefix strip + in-tree `..`
  clamping, so dot-rooted `likely_files` are checked at the correct path.

---

## [1.23.7] — 2026-06-17

### Added

- **`ANVIL_ROOT` environment override** for project resolution across the CLI and
  the MCP server. When set, it points anvil at the project root (the directory
  containing `.anvil`) regardless of the current working directory — making the
  engine correct inside containers and non-cwd MCP hosts. Precedence: explicit `--cwd` >
  `ANVIL_ROOT` > cwd. An invalid value fails loudly (no silent cwd fallback), and
  `init` / `init_project` honor it too so create-then-read target the same directory.
  Centralized in `cli/_helpers._resolve_base_dir`.
- **`schema_version` exposed to tooling.** `status` (human + `--json`) now reports
  `schema_version` (the code-targeted `SCHEMA_VERSION`) and `db_schema_version` (the true
  on-disk `PRAGMA user_version`, read **before** migration so pre-migration drift is
  observable). An un-migratable schema is reported as a clean
  `{"error":{"code":"schema_mismatch"}}` + exit 1 (no traceback), and `status
  --hook-format` stays exit-0-safe. Accessors: `schema.get_schema_version()`,
  `SqliteBackend.get_schema_version()`, `sqlite.read_db_schema_version()`.

### Known follow-up

- An un-migratable DB is silently re-stamped to the current `SCHEMA_VERSION` by
  `_apply_ddl` *before* the mismatch is raised, so `schema_mismatch` is reported only on
  the first open (masked afterward). Pre-existing; a clean fix requires reordering core
  `initialize`/`_apply_ddl`. Tracked.

---

## [1.23.6] — 2026-06-17

### Added

- **Context-footprint self-audit** (`benchmarks/context_audit.py` +
  `benchmarks/CONTEXT_AUDIT.md`): a reproducible measurement (tiktoken, chars/4 fallback)
  of the always-on context cost anvil imposes when installed — ~**7,248 tokens**
  (MCP tool schemas 54%, agent descriptions 38%, skill descriptions 8%, hooks 0.3%), with
  the ~42.7K of skill/agent bodies excluded by progressive disclosure. MCP schemas are
  measured live from the real FastMCP serialization. Verdict: modest (~3.6% of a 200K
  window) but **mixed** — ~68% of agent-description tokens are verbose `<example>` blocks
  and two repo-maintenance agents (~20%) a standalone user never needs. Identified trims
  (strip agent-description examples into bodies, opt-in the maintenance agents, tighten
  MCP docstrings) could cut the baseline ~48% with no capability loss. Earns the right to
  the "context-frugal" claim by measuring it honestly.

---

## [1.23.5] — 2026-06-17

### Added

- **Enforceable completion-evidence gate (strict mode).** The evidence gate was
  advisory — it flagged missing required evidence but never blocked, so "agents lie
  about done" (the single most-felt pain in the competitor-issue analysis) was
  unenforceable. Now, when strict mode is active, **accepting a task with unsatisfied
  `required_evidence` is refused** (the task stays `needs_review`) on **both** surfaces:
  - CLI `apply --approve` (a `--strict / --no-strict` flag), and
  - the MCP `apply_review_decision` tool (a `strict` parameter) — the surface agents
    actually use.
  Strict is resolved as: explicit flag/param > project config `strict_evidence` > `False`
  (default **advisory**, fully back-compatible). It reuses `review.gates.evidence_complete`
  (no reimplementation). Under CLI `--json`, a refusal is `{"ok": false, "command":
  "apply", "error": {"code": "evidence_incomplete", "missing": [...]}}` with exit 1; the
  MCP tool raises `ToolError` carrying the same `evidence_incomplete` code + missing items.
  `--reject` is never gated; a complete gate or empty `required_evidence` is a no-op.
  `submit` stays advisory (it transitions to `needs_review`, not `done` — the gate belongs
  at the accept transition). Config `strict_evidence: false` is added to the `init`
  scaffold. Covered by `tests/test_strict_evidence.py` (CLI) and
  `tests/test_mcp.py::TestApplyReviewDecisionStrictEvidence` (MCP).

---

## [1.23.4] — 2026-06-17

### Added

- **Machine-readable `--json` output** across the query and mutation CLI commands
  (`list`, `show`, `status`, `next`, `prd find-decisions`, `plan`, `score`,
  `review tasks`, `claim`, `renew`, `release`, `submit`, `apply`) — the standalone /
  non-Claude-host product surface. One consistent envelope, emitted as a single compact
  JSON line on stdout: success `{"ok": true, "command": "<name>", "data": {...}}`; error
  `{"ok": false, "command": "<name>", "error": {"code": "<stable-token>", "message":
  "..."}}` with a non-zero exit. Domain objects serialize via Pydantic
  `model_dump(mode="json")` (ISO-8601 datetimes, enum values), so the wire form stays in
  lockstep with the models. Non-fatal warnings fold into `data.warnings`; stderr stays
  empty in `--json` mode so stdout is always pipeable. Helpers live in `cli/_json.py`;
  covered by `tests/test_json_output.py`.
- The `command` field is the space-joined CLI invocation path (e.g. `"review tasks"`,
  `"prd find-decisions"`), and `apply --json` returns an identical key set in both
  review-only and decision modes — a predictable, stable contract for script authors.

### Not yet covered (by design, this pass)

- `expand` (`--format text|prd` already), `packet` (`--format json` already), `sync`
  (provider/network surface), `init`, and `prd parse`/`prd review` remain human-output
  only — tracked for a follow-up. `score --json` and the MCP `score_tasks` tool expose
  the same operation with different shapes; alignment is a tracked follow-up.

---

## [1.23.3] — 2026-06-17

### Fixed

- **TOCTOU race in the file-overlap claim check** (the known issue from 1.23.2). The
  cross-task file-overlap **and** conflict-group re-checks now run inside the same
  `BEGIN IMMEDIATE` transaction that inserts the claim (in `state/sqlite.py`
  `_check_claim_created`, via a short `BEGIN IMMEDIATE … COMMIT` so reads see the latest
  committed snapshot rather than a stale WAL read mark). A `force` flag is threaded
  through `ClaimCreatedPayload` so `claim --force` still overrides the cross-task guards;
  the same-task double-claim guard is never overridable. Two concurrent claims on
  file-overlapping or same-conflict-group tasks now yield exactly one winner. Benchmark
  `overlapping_files` collisions drop from a ~8%-dual-claim residual to **0 across all
  trials**.
- **CLI `claim`/`renew` ignored `default_lease_minutes`** (the known issue from 1.23.2).
  Both now load the project config and thread `default_lease_minutes` /
  `default_heartbeat_minutes` into `ClaimManager`. `config.py` stores these as `float`
  and coerces via `float(str(...))`, so fractional (sub-minute) leases are honored
  instead of truncated/rejected.

### Added

- **Concurrency regression suite** (`tests/test_claim_concurrency.py`): N=8 threads ×
  300 iterations assert exactly one winner for same-task, file-overlap, and
  conflict-group claims (fails 4/5 pre-fix), plus a force-override test and a CLI test
  asserting a `default_lease_minutes: 0.5` config yields a ~30s lease.

### Known follow-ups (from the fix review)

- Overlap guards do hot-path scans of active claims; add an index / filter by
  `lease_expires_at` if it matters at scale. MCP `claim_task` still derives its lease
  from its own parameter, not project config. `renew` lease wiring is covered by the CLI
  but not a dedicated unit test. Tracked in the backlog.

---

## [1.23.2] — 2026-06-17

### Added

- **Coordination benchmark (`benchmarks/`).** A reproducible harness that runs the
  same multi-actor workload through two coordinators — naive shared-markdown vs. the
  real anvil CLI — across four scenarios (overlapping files, dependency
  ordering, crash/stale-lease recovery, evidence gaming). Drives the live engine; no
  third-party deps. `python3 benchmarks/run_benchmark.py`.

### Known issues (surfaced by the benchmark, not yet fixed)

- **TOCTOU race in the file-overlap claim check** — two file-overlapping tasks can
  both be claimed concurrently (~8% of head-to-head attempts) because the overlap
  read happens outside the `BEGIN IMMEDIATE` claim transaction.
- **CLI `claim` ignores `default_lease_minutes`** — `cli/claim.py` builds
  `ClaimManager` without the configured lease, so the CLI always uses the 60-minute
  default (the MCP path wires it correctly). The config loader also rejects
  fractional minutes via `int(str(...))`.

---

## [1.23.1] — 2026-06-12

### Fixed

- **Recursive expansion queue now reaches CLI and MCP.** `score` and
  `score_tasks` use the depth-capped recursive frontier, so over-depth leaves
  are no longer surfaced for automatic expansion after repeated splits.
- **Git-backed event IDs include full event identity.** Hash IDs now include
  action, target kind, and target id in addition to parent, payload, actor, and
  timestamp. Fields are joined with the ASCII unit separator `\x1f`,
  preventing same-payload branch events from colliding and being deduped away
  during git-mode replay.

---

## [1.23.0] — 2026-06-10

### Added

- **Score recursion + parent roll-up** — closes the four weaknesses claude-task-master users documented around its complexity scoring.
  - **Parent roll-up (TM #250):** an expanded task (one with children) is now a *container*, not an actionable unit. Its stored complexity score is preserved (the event log is immutable audit history — the score still records how big the unsplit unit was), but `build_expansion_queue` excludes it from the actionable EXPANSION QUEUE, so an already-split parent is never re-queued. New `is_expanded(task, all_tasks)` predicate.
  - **Recursive expand-to-threshold:** new `build_recursive_expansion_queue()` returns the leaf-only expansion frontier annotated with tree `depth`. Recursion happens across re-scores (expand a leaf → re-score → its over-threshold children re-enter the queue), bounded by `DEFAULT_RECURSION_DEPTH_CAP` (3) and a cycle guard in `_depth_of` so the walk always terminates — a lineage deeper than the cap is dropped from the auto-queue as a signal that the PRD block needs human restructuring.
  - **Partial-score merge (TM #1644):** re-scoring a subset of tasks no longer wipes the others. This already held by construction — scores persist as per-task `task.scored` events, so a single-task re-score is an append that leaves every other task's projected score intact — and is now proven by a regression test (the event-sourced answer to task-master's overwrite-on-partial bug).
- `skills/plan/SKILL.md` documents the roll-up + across-re-score recursion model and its depth/cycle safety rails.

---

## [1.22.0] — 2026-06-10

### Added
- **Git-backed events, Phase A (opt-in).** First slice of the 2026-06-10 git-backed-events spec: `events.jsonl` can now be a repo-scoped, merge-friendly source of truth instead of a machine-scoped one (idea adopted from steveyegge/beads):
  - New config knob `events_storage: local | git` (default `local`; validated at load time like every other literal field). `local` keeps pre-1.22.0 behaviour byte-for-byte — sequence ids, strict sequential replay, byte-equality guarantee untouched
  - In git mode, event ids are hash-chained — `"E-" + sha256(parent_event_id ‖ canonical_json(payload) ‖ actor ‖ ts)[:12]` — so two branches/machines append concurrently with zero collision risk; the envelope gains `parent_event_id` (chain link) and `lamport` (writer sets max-seen + 1). Local mode omits both keys from serialized lines
  - Order-tolerant replay in git mode: load all lines (torn trailing line tolerated exactly as before), dedupe by event id (a line duplicated by a `merge=union` union applies once), order by `(lamport, ts, event_id)`. Competing cross-branch `claim.created` events on one task resolve deterministically — earliest `(lamport, ts, id)` wins the task transition (`claim.superseded` materialization is Phase B)
  - New CLI command `anvil migrate-events --to git [--yes]`: rewrites the local log with hash ids preserving order, emits `id_mapping.json` (old → new), writes `.anvil/.gitattributes` (`events.jsonl merge=union`), prints `.gitignore` guidance, flips `events_storage: git` in config.yaml, and rebuilds the projection. Dry-run by default; refuses while claims are active; backs up the original log
  - Schema v4 (auto-upgrade from v0–v3): `events.id` CHECK widened to accept hash ids; new nullable `events.seq` column — the replay-assigned display order in git mode (derived state, never written to the log; NULL in local mode where the monotonic id is the order)
  - New pure module `state/hashing.py` (`hash_event_id` / `canonical_payload_json`) shared by the write path, the migration command, and tests
  - 33 new tests: hash chain linkage, dedupe replay, lamport tie ordering, a divergent-merge simulation (common prefix + two branch suffixes concatenated both ways converge to identical state, with the competing-claims winner deterministic), migration round-trip (pre-state equals post-state modulo the id mapping), config knob validation, v3→v4 schema auto-upgrade

---

## [1.21.0] — 2026-06-10

### Added
- **Complexity score → auto-expansion loop.** After scoring, every task whose `complexity` is at/above the configurable threshold is queued for decomposition instead of just sitting in a report (idea adopted from claude-task-master):
  - New config knobs: `auto_expand` (default `true`) and `auto_expand_threshold` (default 4, validated 1–5; booleans explicitly rejected so `true` cannot silently become threshold 1)
  - CLI `score` now prints an **EXPANSION QUEUE** section — task id, complexity, deterministic suggested sub-task count (`complexity - 1`, clamped to the expand engine's 2–5 envelope), and the exact `anvil expand TXXX --use-llm` follow-up per task
  - MCP `score_tasks` response gains an `expansion_queue` field with the same data (queueing stays deterministic; LLM-side decomposition still only runs via `expand --use-llm` or the planner agent)
  - `expand` threshold no longer hardcoded — `planning.inference.expand_task` accepts the config threshold
  - `skills/plan/SKILL.md`: after scoring, the orchestrator auto-dispatches planner expansion for queued tasks (one summary checkpoint, no per-task Q&A) unless `auto_expand: false`
  - New pure helpers in `planning/scoring.py`: `build_expansion_queue()` / `suggested_subtask_count()` / `ExpansionCandidate`
  - 18 new tests (queue filtering/ordering/edge cases, config knob validation, template coverage)

---

## [1.20.2] — 2026-06-10

### Fixed

- **Flock contention deadline measured on the monotonic clock (Greptile P1 on #80).** 1.20.1 computed the `_append_lock` 5 s contention deadline from wall-clock `Clock.now()`, so an NTP step mid-contention could silently stretch the timeout (backward step) or fire `StateLocked` early (forward step). The deadline is now measured on an injectable monotonic source — new keyword-only `SqliteBackend(monotonic_fn=...)` parameter, defaulting to `time.monotonic` — mirroring `sleep_fn`. The contention unit test drives a fake monotonic counter instead of advancing `FrozenClock`, keeping the timeout path fully deterministic.

---

## [1.20.1] — 2026-06-10

### Fixed

- **Jittered exponential backoff for event-log flock contention.** `_append_lock` retried a contended `flock` on `events.jsonl` with a fixed-interval sleep, which woke every waiter on the same tick. Under a coordinated multi-agent wave (10+ concurrent claimants) early winners re-acquired the lock in lockstep and late arrivals exhausted the 5 s budget without ever winning a release, surfacing as spurious `StateLocked`. Retries now follow an exponential schedule — 10 ms initial, doubling to a 500 ms cap, ±10% jitter — under the same 5 s overall timeout, so wake-ups are de-synchronized and each lock release is contested by waiters at staggered offsets.
- **`_append_lock` now follows the Clock-protocol convention.** The contention deadline is computed via the injected `Clock` (previously a raw inline `time.monotonic()`), and the retry sleep is injectable via a new keyword-only `SqliteBackend(sleep_fn=...)` constructor parameter (defaults to `time.sleep`). The backoff schedule and timeout path are covered by deterministic unit tests using `FrozenClock` — no real sleeping, no monkey-patching.

---

## [1.20.0] — 2026-06-01

### Added / Changed — Event-Sourced Write Path (SL1-RR-1 closed)

**Breaking internal API** (additive at the feature level; no CLI surface change):

- **`append(EventDraft) -> Event | None`** replaces `apply_event` as the sole production write entry point. Returns a committed `Event` on success, `None` on idempotent no-op; raises `EventRejected` on validation failure. The old `apply_event`, `next_event_id`, and `PENDING_EVENT_ID` sentinel are removed.
- **Decide/apply split (`_check_*` / `_write_*`).** Each of the ~17 action handlers is split into a read-only `_check_<action>` (raises `EventRejected` on any illegal transition; no side effects) and an infallible `_write_<action>` (mutates given a passing check). Dispatch table maps `action -> ActionSpec(payload_model, check, write)`.
- **Log-as-id-authority via `flock`.** Event ids are assigned inside `append()` by scanning the tail of `events.jsonl` under an exclusive `flock`, not from `SELECT MAX(id)`. This closes the PR #41 Critic-3 cross-process id-collision race: two concurrent processes can no longer observe the same `MAX(id)` and collide on insert.
- **Append-only `events.jsonl` + sibling `audit.jsonl`.** `events.jsonl` records only committed, successfully applied events. Validation rejections and idempotent no-ops are written exclusively to `audit.jsonl`, making a poison canonical line structurally impossible.
- **Strict no-skip-list replay.** `replay_from_empty` applies every line via `_write_*` only — no validation, no logging, no skip-list. Any well-formed log replays infallibly.
- **Forward-catch-up self-heal.** On open, the backend scans the tail of `events.jsonl` for lines not yet reflected in the SQLite projection and replays them forward, closing the post-COMMIT audit gap (crash between COMMIT and JSONL write no longer leaves the projection ahead of the log).
- **Relaxed-default durability with strict opt-in.** `append()` accepts a `durability` parameter; the default relaxes `fsync` overhead for the common single-writer case; `strict` mode forces a full sync for multi-writer / crash-recovery scenarios.

### Closed

- **SL1-RR-1** (tech-debt-backlog): a rejected non-PENDING event can no longer persist a poison canonical line in `events.jsonl` — validation now happens entirely before any log write.
- **Inverse post-COMMIT audit gap**: a crash between SQLite COMMIT and JSONL append is recovered on next open via forward-catch-up.
- **PR #41 Critic-3 cross-process id-collision race**: closed by log-as-id-authority + `flock`.

### Removed

- `apply_event` (replaced by `append`)
- `next_event_id` (id assignment is now internal to `append`)
- `PENDING_EVENT_ID` sentinel (no longer needed; all callers use `append(EventDraft)`)

---

## [1.19.0] — 2026-06-01

### Added

- **Replay proven in CI (SL-1).** New `serialize_state(backend)` produces a deterministic, total snapshot of canonical state; `test_replay_equivalence` asserts `serialize_state(normal apply path) == serialize_state(replay_from_empty) == a committed golden`, plus cross-db and same-handle idempotence. A new `.github/workflows/anvil.yml` runs the full suite (incl. this check) on every PR — the replay guarantee is now an enforced invariant, not an assertion.
- `anvil replay --from-events <jsonl> --into <db>` CLI wrapping the existing `replay_from_empty` engine; guards the live `state.db` and missing-file inputs.
- Backend read methods `list_claims` / `list_reviews` / `list_evidence` / `list_requirements` returning full canonical collections (not active-only), deterministically ordered.

### Fixed

- Review-decision read mapping: a rejected `task.applied` returns the task to `drafted` for rework, so `list_reviews()` now reports `needs_changes` (not a terminal `reject`).
- Pre-existing `test_claim_type_values` failure on Python 3.11 (`str in EnumType` raised `TypeError`).
- Version drift across `pyproject.toml` / `__init__.py` / `plugin.json`, now all `1.19.0`.

### Known issues

- Latent replay-robustness gap: a rejected non-PENDING event leaves a poison canonical line in `events.jsonl` that `replay_from_empty` re-applies and re-fails on, aborting a full replay. Tracked in [`tech-debt-backlog.md`](docs/tech-debt-backlog.md) (SL1-RR-1) and the fakoli-style P4 `open_work`.

---

## [1.18.2] — 2026-06-01

### Added

- Roadmap: deferral note for semantic indexing (`sqlite-vec`) and a knowledge-graph view. Useful after Wave 1 (SL-5 contract conflicts, SL-6 spec assumptions) but kept **outside the replay boundary** as rebuildable derived indexes — never canonical state in `events.jsonl`. Cross-references fakoli-style principle P11. Docs-only.

---

## [1.18.1] — 2026-06-01

### Added

- Operating-model up-links: `_evidence_complete` (the unified evidence gate) and the integrity-first roadmap track now back-reference their fakoli-style principles (**P1** advisory and enforcing share one code path; **P5** sequence by credibility risk). Documentation-only; no behavior change.

---

## [1.18.0] — 2026-05-31

### Added

- `docs/roadmap.md` — **integrity-first 90-day priority track.** A committed direction that sits above the version buckets, sequenced by credibility risk: Wave 1 (days 1-30) makes existing claims true (prove replay in CI, measure the critic false-pass rate, plus the already-shipped evidence-gate unification), Wave 2 (days 31-60) makes governance non-gameable (typed `ProofArtifact` evidence, scoring spec assumptions), Wave 3 (days 61-90) earns the "state layer underneath a dynamic-workflow runtime" reframe (promote status-file coordination to canonical state, contract-level conflict detection, a workflow-adapter spike). Documents the three integration postures (beside / governed step / projection) and the two new types (`ProofArtifact`, `OutputContract`) the track introduces.

---

## [1.17.1] — 2026-05-31

### Fixed

- **Evidence gate divergence (correctness).** The `needs_review → accepted` transition enforced a different evidence check than the one `anvil apply` previews to the reviewer. The transition used a raw, case-sensitive substring match against a flattened corpus of every `Evidence` field, while `apply` (`cli/packet_apply.py`) used the intent-based `review.gates.evidence_complete`. The two could disagree in both directions: a task could preview as INCOMPLETE yet be accepted, or preview as complete yet be rejected. The substring gate was also trivially gameable — writing a required literal (e.g. `"test output"`) into any field passed it, and `pytest --collect-only` satisfied a "tests pass" requirement. `transitions._evidence_complete` now delegates to `review.gates.evidence_complete`, making it the single source of truth. A parametrized agreement test locks the enforcing gate to the preview gate so they can never diverge again.

---

## [1.17.0] — 2026-05-26

Major capability release: **multi-provider LLM access** (direct Anthropic API, Amazon Bedrock, OpenAI-compatible custom endpoints) plus **tier-aware default model selection** that drops typical session cost by ~60% versus the prior "everything routes through Opus" pattern. The five plugin-surface critics extract to a new dedicated `fakoli-plugin-critic` plugin (`fakoli-crew` 2.3.0+ no longer ships them).

### Added

- `planning/llm.BedrockProvider` — Anthropic-on-Bedrock via `anthropic.AnthropicBedrock`. Boto3 credential chain (env vars / profile / IAM role) just works. Optional dep: `pip install 'anvil[bedrock]'`.
- `planning/llm.CustomEndpointProvider` — any OpenAI-compatible `/v1/chat/completions` endpoint via the `openai` SDK with `base_url=`. Targets vLLM, LiteLLM proxy, OpenRouter, Together, Groq, Azure OpenAI, local llama.cpp. Optional dep: `pip install 'anvil[custom]'`.
- `planning/llm.MODEL_TIERS`, `BEDROCK_MODEL_TIERS`, `DEFAULT_TIER`, `resolve_model_for_tier()` — tier vocabulary (`opus` / `sonnet` / `haiku`) and the helper that maps a logical tier to the right model id for each provider's namespace.
- `Config` fields: `llm_provider` (anthropic/bedrock/custom), `llm_tier` (opus/sonnet/haiku), `bedrock_region`, `bedrock_profile`, `custom_base_url`, `custom_api_key_env`. All optional; env auto-detect kicks in when blank.
- `docs/llm-providers.md` — provider setup guide with worked examples for direct API, Bedrock (env / profile / IAM), and three custom-endpoint shapes (vLLM, OpenRouter, LiteLLM proxy).
- `docs/model-strategy.md` — tier rationale, per-agent assignments, override precedence, May 2026 cost figures, and the rationale for *not* shipping a dynamic complexity router by default.
- `[bedrock]` / `[custom]` / `[all-providers]` optional extras in `pyproject.toml` so the default install stays lean (no boto3, no openai) for users on the Anthropic-API-only path.
- Test coverage: `TestResolvePlannerProvider` (8 new tests covering env auto-detect precedence, config overrides, tier threading); `TestResolveModelForTier` (3 tests for direct-API + Bedrock tier tables + error on unknown tier); `TestCustomEndpointProvider` (2 tests for required-model and required-base_url validation).

### Changed

- `planning/llm_planner.resolve_planner_provider()` — gained an optional `config: Config | None` parameter. New precedence: explicit `config.llm_provider` > env auto-detect (`ANTHROPIC_API_KEY` > `AWS_REGION`+bedrock-extras > `CUSTOM_LLM_BASE_URL`) > fail loudly. Single provider per process; no silent fallback across providers.
- `generate_tasks_markdown()` — gained an optional `config:` parameter, threaded through to the resolver so projects' explicit provider+tier+credential knobs apply.
- `cli/plan._resolve_llm_provider()` — delegates to `resolve_planner_provider(config)` so `--use-llm` augmentation honors the same multi-provider precedence as the no-tasks LLM backstop. Single source of truth for provider selection across the CLI.
- `cli/plan._load_config_optional()` — new helper that soft-loads `.anvil/config.yaml` and emits a stderr warning naming the exception class on load failure. Mirrors `cli/claim.py`'s existing pattern.
- `AnthropicProvider` — gained `tier=` kwarg (Opus / Sonnet / Haiku), resolves via `MODEL_TIERS`. Existing `model=` arg still wins when both are passed; backward compatible for every existing caller.
- Default model tier across the codebase shifts from "Sonnet (hardcoded constant)" to `DEFAULT_TIER = "sonnet"` (community consensus per anthropics/claude-code#27665). Functional behaviour unchanged on the default path; the change is documentation + config plumbing.
- Agent frontmatter — `model:` set explicitly across all 6 anvil agents (was `opus` uniformly; now `opus` for reasoning/synthesis, `sonnet` for structured generation, `haiku` for mechanical/read-only):
  - `planner` → opus | `critic` → opus | `docs-scribe` → sonnet | `marketplace-scribe` → sonnet | `sentinel` → haiku | `state-keeper` → haiku
- `config.yaml` template — gains commented `llm_*`, `bedrock_*`, `custom_*` blocks with tier-mapping reference so new projects see the multi-provider shape at init time.

### Fixed

- `cli/plan._resolve_llm_provider()` previously hardcoded an `ANTHROPIC_API_KEY` env check, diverging from the resolver's own logic and forcing users on Bedrock or custom endpoints to skip `--use-llm` entirely even when their non-Anthropic provider was correctly configured. Now both paths share the resolver.

### Fixed

- **greptile MUST FIX #1.** `_choose_provider_family` was using `hasattr(anthropic, "AnthropicBedrock")` to detect whether the Bedrock extras were installed. The `AnthropicBedrock` class ships with the base `anthropic` install — only `boto3` (the transitive dep added by the `[bedrock]` extra) actually gates it. Switched to `try: import boto3` so AWS_REGION-set boxes without the extras correctly fall through to "no provider available" instead of picking Bedrock and crashing at construction.
- **greptile MUST FIX #2 + critic MUST FIX #1.** When the operator pinned `llm_provider: bedrock` (or `custom`) in config without installing the extras, the underlying `LLMProviderError` propagated past the resolver's `PlannerProviderUnavailable` contract — users saw a raw traceback where curated help text was promised. The resolver now wraps every per-family `_build_*` call's `LLMProviderError` into `PlannerProviderUnavailable` with an install-command suggestion.
- **critic MUST FIX #2.** `_build_custom` silently defaulted to `claude-sonnet-4-6` when the operator had `llm_provider: custom` but no `llm_model` / `llm_tier`. On a local vLLM serving Mistral-7B (or any non-Anthropic OpenRouter route) this produced a confusing "model not found" failure that looked like a network issue. The resolver now refuses to invent a model and raises `PlannerProviderUnavailable` with an actionable message naming the config keys.
- **structure-critic MUST FIX.** `bin/src/anvil/__init__.py` `__version__` was stale at `1.16.0` — every other source of truth had bumped to `1.17.0`. Now in sync, plus a new `tests/test_version_sync.py` regression that asserts `pyproject.toml`, `__init__.py`, and `plugin.json` agree at the start of every test run.

### Changed (post-review polish)

- `mcp_server.py` `plan_tasks` soft-load — narrowed the broad `except Exception` to `(FileNotFoundError, OSError, ValueError)` first, then a labeled last-resort guard for `yaml.YAMLError` and friends. Mirrors `cli/plan.py:_load_config_optional`'s pattern (mcp-critic SHOULD FIX).
- `cli/plan.py:_load_config_optional` — caught `yaml.YAMLError` explicitly; dropped the misleading "yaml.YAMLError is a subclass of yaml.YAMLError" comment (critic SHOULD FIX #3).
- `cli/plan.py` — removed unused `os` and `re` imports left over from the refactor (critic SHOULD FIX #6).
- `mcp_server.PlanTasksResponse.llm_provider` field comment and `plan_tasks` docstring — updated to document the v1.17.0 multi-provider story (`anthropic` / `bedrock` / `custom` rather than the stale "anthropic today, claude-agent-sdk reserved for v1.16+"); added a note that the MCP server inherits env from the host process (mcp-critic SHOULD FIX).
- `bin/pyproject.toml` `keywords` — synced with `plugin.json` keywords (was drifted: 10 keys differ pre-PR). Cosmetic alignment for marketplace search (structure-critic SHOULD FIX).
- `fakoli-plugin-critic` agent files — sed sweep of "fakoli-crew critic severity rubric" → "fakoli-plugin-critic severity rubric" and similar namespace-stale prose left over from the extraction. Agent system prompts now read as part of `fakoli-plugin-critic`, not `fakoli-crew` (structure-critic SHOULD FIX #4).
- `fakoli-plugin-critic/README.md` — added standard shields.io badges (license, version, marketplace) to match sibling plugins; install snippet shows the `marketplace add` prerequisite (structure-critic SHOULD FIX #5, #10).
- `fakoli-plugin-critic/CHANGELOG.md` — added an `_No unreleased changes._` placeholder under `[Unreleased]` (structure-critic CONSIDER #7).
- `fakoli-plugin-critic/docs/` — removed (was an empty directory).

### Tests

1103 passing (was 1083 baseline). Diff: +20 net (8 new `TestResolvePlannerProvider` + 3 `TestResolveModelForTier` + 2 `TestCustomEndpointProvider` + 4 `TestResolvePlannerProviderGreptileFixes` (greptile + critic regression) + 4 `TestBedrockProvider` (closes the BedrockProvider test gap critic SHOULD FIX #8 flagged) + 2 `test_version_sync.py` (structure-critic regression); 4 existing tests updated for the new resolver signature and the MUST FIX #2 contract change.

---

## [1.16.0] — 2026-05-26

Single-bug release driven by in-the-wild testing: the planner missed an
obvious task→task dependency. T002 (chaos tests in 2-process mode)
clearly depended on T001 (HttpTransport implementation) — without T001
the 2-process mode the tests need doesn't exist — but the generated
task graph showed `dependencies=[]` for T002, and the user only caught
it by reading the PRD acceptance criteria during claim.

Root cause was a three-layer gap:

1. **Parser** (`planning/template.py`) didn't recognise a
   `**Dependencies:** T001, T002` field in task blocks. Even if the
   PRD author wrote it explicitly, the parser silently dropped it.
2. **LLM planner prompt** (`planning/llm_planner.py`) didn't instruct
   the model to identify dependencies from acceptance criteria text.
   The model had no example to emit and no rule telling it to look.
3. **Only existing dep inference** (`planning/inference.py`
   `infer_dependencies`) was a file-subset heuristic — purely
   file-based. It would NEVER catch "T002 needs T001 because the
   criteria say 'in 2-process mode'" if the tests lived in
   `tests/chaos/` while the implementation lived in
   `packages/transport/` (no file overlap).

v1.16.0 closes all three.

### Added

- **Parser support for `**Dependencies:**` field** in task blocks
  (`planning/template.py`). Comma-separated TaskIDs, normalised to
  upper-case (`t001, T002` → `["T001", "T002"]`). Post-parse
  validation surfaces a `ParseError` warning when a dependency
  references a task ID that doesn't exist in the same `## Tasks`
  section — the dep is kept on the task regardless so downstream
  tooling can see the author's intent.
- **LLM planner prompt instructions for dependency emission**
  (`planning/llm_planner.py`). The system prompt now includes
  `**Dependencies:**` in the example task block AND a "Dependencies
  (CRITICAL — read carefully)" rule block explaining the two
  trigger conditions:
  - **Infrastructure dependency** — Task A creates infrastructure
    (API, service, transport, schema, CLI command) that Task B
    needs.
  - **Phrasal dependency in acceptance criteria** — "in X mode",
    "using Y", "after Z is complete", "given W from <other task>".
  Plus an explicit cycle-avoidance rule and an instruction to OMIT
  the field entirely when no deps exist (no empty `**Dependencies:**`
  lines).
- **`anvil claim` warns on undone dependencies (soft gate)**.
  Before acquiring the lease, claim fetches `task.dependencies` and
  checks each one's status. If any are not yet `done`, emits a
  stderr warning naming each dep + status, then proceeds with the
  claim. `--force` silences the warning. The soft-gate design
  preserves legitimate stacked-PR workflows (claim T002 while T001
  is still in_progress and merge them together) while ensuring the
  user knows what they're doing.
- **10 new regression tests:**
  - 4 in `tests/test_template.py::TestTaskParsing`: explicit deps
    field parses, multi-value normalises uppercase, unknown ID
    warns, omitted field defaults to empty.
  - 4 in `tests/test_llm_planner.py::TestSystemPromptInstructsDependencyEmission`:
    prompt shows the field, prompt names the two triggers, prompt
    says omit-when-empty, prompt warns against cycles.
  - 2 in `tests/test_cli.py::TestClaimCommand`: claim warns on
    undone deps; `--force` silences the warning.

### Changed

- README badges updated for v1.16.0: tests 1071 → 1081 (+10);
  version 1.15.0 → 1.16.0.
- `docs/prd-template.md` task-field reference table gains a
  `**Dependencies:**` row, the canonical-example T003 demonstrates
  using it (`**Dependencies:** T001, T002`), and a paragraph
  explains when to emit the field vs leave it to file-overlap
  conflict groups.

### Migration

No breaking changes. Schema unchanged. The `Task.dependencies` field
already existed in the Pydantic model since pre-v1.0 — v1.16.0 just
wires it through the parser, the planner prompt, and the claim
gate. PRDs without `**Dependencies:**` fields continue to work
unchanged (the field is optional, defaults to empty list). The
existing file-subset `infer_dependencies()` still runs after parse
— v1.16.0 layers explicit semantic deps on top of, not in place of,
the file-based inference.

The claim warning is soft (proceeds with the claim) so existing CI /
scripts that call `claim` won't suddenly start failing. Users who
prefer stricter behaviour can wrap the warning in their own
project's git-pre-push hook or check `anvil show TASK_ID`
before claiming.

### Fixed (post-greptile review)

- **Dependency ParseError now points at the offending `### Txxx:`
  block**, not at the `## Tasks` section header. The parser tracks
  a `task_id → block_line` map during the parse loop and consults
  it during post-loop validation. Before the fix, a user with a
  bad `**Dependencies:** T099` on T002 would be pointed at line 1
  of the section instead of T002's heading.
- **Self-dependency now stripped + warned** instead of passing
  silently. A task with `**Dependencies:** T001` on T001 would
  otherwise trigger a perpetual claim-time warning (T001 can never
  be `done` before it is claimed). The parser strips the self-ref
  AND emits a clear "remove yourself from your own dependencies"
  warning naming the offending task. Note this differs from the
  unknown-ID handling, which KEEPS the bad ID so downstream tooling
  can see the author's intent — self-refs are unambiguously wrong.
- **`--force` help text updated** to mention that the flag silences
  both file-conflict warnings AND dependency warnings. Previously
  only the file-conflict half was documented, so users wouldn't
  know `--force` cleared the dep-warning noise too.
- Suite is **1083 passing** (+2 regression tests for self-dep
  stripping and per-block line attribution).

---

## [1.15.0] — 2026-05-26

Five-bug release driven by in-the-wild testing on a real project. Every
bug shares the same anti-pattern at progressively deeper layers: **the
agent silently failed or asked the user to make a routing/typing
decision the agent had the context to make**. v1.15.0 closes all five.

What changed in this release:

1. **`anvil plan` GUARANTEES task generation** — calls the LLM
   automatically when the PRD has features + requirements but no
   `## Tasks` section. The user no longer has to remember to dispatch
   the `anvil:planner` subagent as a workaround.
2. **`anvil plan` PRUNES ORPHAN tasks/features on re-parse** —
   the docs always claimed "Re-parse replaces, not merges" but the
   implementation only upserted by ID. New `task.deleted` /
   `feature.deleted` event types with safety guards land the
   re-parse contract.
3. **`anvil expand --use-llm` tolerates fenced JSON + prose** —
   previously every call failed with `Expecting value: line 1 column
   1 (char 0)` because the parser couldn't handle the markdown
   fences Claude routinely wraps JSON in.
4. **`branch_prefix` is host-project-configurable** — host projects
   with `feature/` / `fix/` conventions no longer get silently-
   incompatible `agent/` branches.
5. **`execute` skill auto-dispatches to fakoli-crew** — instead of
   asking the user "how would you like to proceed?" the skill
   encodes a routing heuristic that picks the right specialist
   from the task's signals (likely_files, verb, criteria).

Suite is **1071 passing** (was 1024 in v1.14.0; +47 new tests across
the LLM planner module, orphan-cleanup handlers, fenced-JSON parser,
`branch_prefix` validation, and CLI/MCP integration). Schema
unchanged. No breaking changes for existing callers, with one
behaviour-change callout in **Migration** below.

### Added

- **New `planning/llm_planner.py` module** with `generate_tasks_markdown()`
  and `resolve_planner_provider()`. Pure module; emits `## Tasks`
  markdown that the existing `planning.template.parse_prd` consumes
  via round-trip. Tier-chain provider resolver (see "Provider
  tier-chain design" below). 15 unit tests covering tier resolution,
  prompt assembly, output validation, and end-to-end with a recorded
  LLM provider.
- **`anvil plan` LLM backstop (CLI).** When the deterministic
  parse yields 0 tasks but ≥1 features, the CLI calls
  `generate_tasks_markdown()`, idempotently appends the `## Tasks`
  block to `prd.md` (re-runs are no-ops once the section exists),
  re-reads + re-parses, emits `task.created` events. The output
  line explicitly tells the user the file was modified.
- **New `--no-llm` flag on `plan` CLI.** Opts out of auto-generation
  (e.g. on CI without API keys). When passed and 0 tasks are parsed,
  CLI exits 1 with a clear "author them manually" message — never
  silently returns 0.
- **MCP `plan_tasks` mirrors the CLI.** New `use_llm: bool = True`
  and `prune_force: bool = False` parameters. Response model gains
  `llm_generated`, `llm_provider`, `pruned_task_ids`,
  `pruned_feature_ids` fields (all with defaults so old clients see
  no breaking change). `PlannerProviderUnavailable` and
  `TaskGenerationError` raise `ToolError` with safe summaries — no
  silent 0-task responses, no LLM-output leakage in error messages.
- **New `task.deleted` and `feature.deleted` event types** with
  handlers in `state/sqlite.py`. The schema's
  `tasks.parent_task_id ON DELETE SET NULL` and
  `sync_mappings.task_id ON DELETE CASCADE` were designed for
  deletion from the start (see `state/schema.py:40` comment); v1.15.0
  finally wires the events. Safety: `task.deleted` refuses non-safe
  statuses (claimed, in_progress, needs_review, etc.) unless
  `force=True`, AND refuses unconditionally when claims or evidence
  rows still reference the task (FK-protected audit history that
  not even `--prune-force` overrides). `feature.deleted` refuses if
  tasks still reference the feature (FK RESTRICT pre-check).
- **`anvil plan` auto-prunes orphans on re-parse.** Computes
  the diff between `state.db` and the new parse, emits the deletion
  events. Safe-status orphans prune silently; unsafe-status orphans
  cause exit 1 with a blocked-IDs list and the `--prune-force`
  escape hatch. Output line surfaces what was pruned.
- **`Backend.list_features()` Protocol method + SQLite impl** —
  orphan detection needs the full feature set for the diff.
- **New `branch_prefix` field in `.anvil/config.yaml`**
  (default `"agent"`, preserving pre-v1.15.0 behaviour). The CLI's
  `claim` command reads this and creates branches as
  `<branch_prefix>/<task-id>-<slug>` — set `branch_prefix: feature`
  and `claim` produces `feature/t012-...` instead of the
  silently-incompatible `agent/t012-...`. Nested prefixes
  (`feature/agent`) preserved verbatim. Empty string (`""`) is
  explicit no-prefix mode. Validation at config-load time:
  leading/trailing slashes, whitespace, non-string → `ValueError`
  with a clear message.
- **`create_branch_for_task` `branch_prefix=` keyword arg** (default
  `"agent"` for backwards compat). Pre-v1.15.0 callers see no change.
- **`config.yaml` template** (`write_default_config`) now emits the
  `branch_prefix:` line with inline guidance so fresh projects
  see the choice at init time.
- **Shared helpers module `planning/_plan_helpers.py`** consolidates
  the `_has_tasks_section` regex, `SAFE_DELETE_STATUSES` constant,
  `classify_orphans()`, and `emit_prune_events()`. Both CLI and MCP
  import from here instead of carrying twin copies (post-review
  consolidation — see Fixed below).
- **47 new regression tests** across:
  - `tests/test_llm_planner.py` (NEW, 15 tests): tier resolution,
    prompt assembly, fence-stripping, round-trip parse contract
  - `tests/test_llm_integration.py::TestExpandTaskHandlesLlmQuirks`
    (5 tests): fenced JSON, prose preamble, empty response, garbage
  - `tests/test_cli.py` (7 tests): `TestPlanLlmBackstop` +
    `TestPlanOrphanPrune` (CLI integration)
  - `tests/test_mcp.py` (6 tests): MCP integration mirrors
  - `tests/test_config.py` + `tests/test_git_ops.py` (13 tests):
    `branch_prefix` validation and end-to-end
  - +1 sanity test added during post-review CHANGELOG consolidation

### Changed

- **`plan` skill Step 1 rewritten.** The pre-v1.15.0 "if 0 tasks,
  dispatch the planner subagent" workaround is gone — the CLI now
  guarantees tasks, so the skill just runs `plan` and surfaces the
  result. Agent MUST surface `(N generated via LLM ...)` output to
  the user so they know `prd.md` was modified.
- **`plan` skill new Step 1.5 — structured Q&A for post-plan
  decisions.** Scope overruns, structural concerns, expansion
  candidates each become a one-turn `AskUserQuestion` (in Claude
  Code) or numbered prompt elsewhere. **One decision per turn —
  do NOT batch.** Bolded as the leading sentence per skill-critic
  review.
- **`execute` skill Step 3 rewritten — auto-routes to fakoli-crew
  specialist.** First-match-wins routing table maps task signals to
  crew members (smith/guido/welder/scout/herald/keeper/sentinel/
  flow-execute). Tie-break rule disambiguates when two rows match
  (per skill-critic review). Step split into Step 3 (routing) +
  Step 3a (implementation discipline). Anti-pattern callout names
  the "How would you like to proceed?" failure mode.
- **`finish` skill new "Decision-presentation discipline" subsection.**
  Generalizes the v1.13.0 disposition-gate pattern (accept / reject /
  hold / discard) from a one-off to a rule: any 2+ option decision
  uses `AskUserQuestion` or explicit numbered prompts, never
  prose-with-bullets.
- **`claim` skill anti-pattern subsection extended** with the same
  Q&A discipline rule for claim-time decisions.
- **`prd` skill `## Iterating` section** clarified — the
  destructive-re-parse contract now reads as two distinct bullets
  (`prd parse` replaces requirements; `plan` prunes orphan
  features/tasks) so readers cannot conflate which command does
  which prune (per skill-critic review).
- README badges + "What ships today" + Architecture table updated
  for v1.15.0: version 1.14.0 → 1.15.0; test count 1024 → 1071;
  CLI commands 23 (was previously claimed as 24, but disk shows 23
  — corrected per structure-critic review); MCP tools 22 (the
  pre-v1.13.0 "13 tools" prose was stale in three places).
- README "Highlights from v1.10.0" section replaced with v1.15.0
  highlights — the previous block was 5 releases stale.

### Fixed

- **`expand --use-llm` was failing for every task** because
  `planning.inference._parse_subtask_response` called `json.loads`
  with no tolerance for markdown fences. Modern Claude models
  routinely wrap JSON in ` ```json … ``` ` despite the prompt. The
  parser saw the leading backtick instead of `[` and emitted
  `Expecting value: line 1 column 1 (char 0)`. Three-layer fix:
  strip fences (` ```json `, ` ```jsonl `, plain ` ``` `) →
  fall back to string-aware bracket-matching extractor for
  `Here are 3 sub-tasks: [...]` shapes → warning now includes a
  300-char sample of the response so debugging doesn't require
  extra verbosity. System prompt strengthened in parallel.
- **CLI `plan` orphan-deletion loops now catch `TransactionAborted`**
  (post-greptile fix). Previously the MCP path caught and re-raised
  as `ToolError`, but the CLI emitted the raw Python traceback —
  most accessible trigger was "user removes a feature heading from
  prd.md while keeping its referencing tasks." Now CLI surfaces the
  handler's clear message via `typer.echo` + `Exit(1)`.
- **CLI/MCP duplication consolidated** (post-critic fix). The
  `_has_tasks_section` helper, `SAFE_DELETE_STATUSES` constant
  (previously triplicated across `cli/plan.py`, `mcp_server.py`,
  and `state/sqlite.py`), and the orphan-prune emit loops (~90
  duplicated lines) now live in `planning/_plan_helpers.py` and
  both layers import from there. Future changes to safe-statuses
  no longer require three synchronized edits.
- **`cli/claim.py` no longer silently swallows config-load errors**
  (post-critic fix — "exactly the bug class this PR is supposed to
  fix"). A YAML typo in `branch_prefix: feature` now emits a stderr
  warning naming the failure before falling back to the default,
  instead of silently producing an `agent/...` branch the user
  thought they had configured away.
- **`planning/llm_planner.py` prompt-injection defense**
  (post-critic fix). User PRD text (summary, goals, requirements,
  features) now wraps in a `<prd>...</prd>` XML fence and the
  system prompt instructs the model to treat anything inside as
  data, not instructions. PRDs are author-controlled so practical
  risk is low; this is defense-in-depth.
- **`mcp_server.py` `TaskGenerationError` safe summary**
  (post-mcp-critic fix). The exception's message can include up
  to 500 chars of raw LLM output; re-raising via `ToolError`
  leaked that to MCP clients. Full exception logged to stderr;
  client sees a safe, actionable summary.
- **`state/sqlite.py` `_handle_task_deleted` conflict_groups
  cleanup** (post-critic fix). Malformed `task_ids` JSON in a
  conflict_group row was silently `continue`'d, leaving the
  deleted task ID reachable from subsequent queries. Now logs the
  corruption to stderr AND resets the malformed row to `"[]"` so
  state.db ends consistent. Tuple-unpack of cursor rows replaced
  with explicit `row["id"]` / `row["task_ids"]` access via the
  `sqlite3.Row` row factory.

### Migration

**No breaking changes for existing callers.** Schema unchanged. All
23 CLI commands and 22 MCP tools continue to work; the new fields
on `PlanTasksResponse` have defaults so old clients see no surprises.
New CLI flags (`--no-llm`, `--prune-force`) and MCP parameters
(`use_llm`, `prune_force`) are opt-in.

**One behaviour callout for MCP and CLI callers (post-mcp-critic
review).** Pre-1.15.0 callers of `plan_tasks` / `anvil plan`
on a PRD with features + requirements but no `## Tasks` section
previously got `task_count=0` and unchanged `prd.md`. As of v1.15.0
the default behaviour is to **call the LLM and rewrite `prd.md`** —
the file gets a fresh `## Tasks` section appended. Pass
`use_llm=False` (MCP) or `--no-llm` (CLI) to preserve the
pre-1.15.0 "task_count=0, file untouched" behaviour. The CLI
output line and the new MCP response fields explicitly surface
when the file was modified.

The `anvil:planner` agent file (`agents/planner.md`) is
unchanged. It remains useful as a reference and for explicit-
dispatch use cases that need the subagent's structured-output
discipline (PRD critique, expansion proposals, incremental
planning across PRD revisions).

### Provider tier-chain design

`resolve_planner_provider()` walks an ordered chain:

1. **Tier 1 — claude-agent-sdk** (RESERVED for v1.16+). Currently
   falls through silently. Hook is in place so a future PR can land
   the wrapper without touching callers. Deferred because the SDK
   is async, requires Node.js + Claude Code CLI installed system-
   wide, and Claude Code's environment already exposes
   `ANTHROPIC_API_KEY` — so Tier 2 covers the same use case at zero
   extra setup cost today.
2. **Tier 2 — anthropic SDK with `ANTHROPIC_API_KEY`** (CURRENT).
   Direct Anthropic API call via the existing `AnthropicProvider`.
   Used in both standalone and Claude Code contexts.
3. **Tier 3 — fail loudly** with a multi-line message naming both
   the env var path and the future SDK path. Never returns 0 tasks
   silently.

---

## [1.14.0] — 2026-05-26

Generalizes v1.13.0's "drive interactively" principle one layer deeper:
when the PRD has `[NEEDS DECISION]` markers, unresolved `## Open Questions`,
or task-level missing fields (empty acceptance criteria, missing
verification), the agent now drives each one as a one-question
conversational turn with proposed options — instead of telling the
user "open the editor and fix these first." An LLM agent's strength
over a CLI is exactly this: turning *blocked on a decision* into
*let me ask you the right question*.

### Added

- **New `planning/decisions.py` module** with `find_unresolved_decisions`
  function. Scans both raw markdown (for inline `[NEEDS DECISION]`
  markers — case-sensitive bracket-enclosed; HTML comments stripped
  to avoid false positives on draft notes) and parsed PRD/Tasks
  (for `## Open Questions` items and missing acceptance criteria /
  verification commands). Returns a flat ordered list of
  `UnresolvedDecision` records. Pure module — no I/O, no backend.
  18 unit tests in `tests/test_decisions.py`.
- **New CLI subcommand `anvil prd find-decisions`** that prints
  a structured per-kind summary with id, location, text, surrounding
  context paragraph, and suggested resolution field. Exits 0
  regardless of finding count (it's a read-only inspection command).
  4 new tests in `tests/test_cli.py`.
- **New MCP tool `find_decisions(cwd)`** mirroring the CLI with a
  typed `FindDecisionsResponse` model. Total MCP surface 21 → 22.
  6 new tests in `tests/test_mcp.py`.
- **New `resolve-decisions` skill** (`skills/resolve-decisions/SKILL.md`).
  Drives each unresolved item as one Q&A turn with proposed options
  when the surrounding context allows. Applies answers to `prd.md`
  inline: `[NEEDS DECISION]` markers get rewritten in place; resolved
  Open Questions get moved to a new `## Decisions` section that
  preserves the audit trail (what was unclear at draft time + what
  was decided + when); missing-field decisions edit the relevant
  `### TXXX:` block to add the chosen acceptance criteria or
  verification commands. Re-parses on completion. Total skills 7 → 8.
- **Soft gates** wired into `prd` skill (Step 2 — after parse) and
  `plan` skill (new Step 0 — before plan_tasks). When
  `find_decisions` returns non-empty, the agent surfaces the summary
  and asks "resolve now via the resolve-decisions skill, or proceed
  without resolving?" The gate is soft by design: Open Questions
  are informational and don't block review/approval; the agent
  surfaces the cost of proceeding and lets the user pick the cadence.

### Changed

- README badges and "What ships today" table updated for v1.14.0:
  version 1.13.0 → 1.14.0; tests 994 → 1022 (+28 new across
  decisions/CLI/MCP); CLI commands 23 → 24; MCP tools 21 → 22;
  skills 7 → 8.

### Migration

No breaking changes. Schema unchanged. The 21 existing MCP tools and
23 existing CLI commands are unchanged. The skill rewrites only add
soft-gate prose; they do not change the user-visible behavior of
`prd review` or `plan` for clean PRDs (find_decisions returns empty,
the gate skips). Existing PRDs that have always-clean (no markers,
no Open Questions, no missing fields) see no change at all.

### Detection scope details

- **`[NEEDS DECISION]` marker:** case-sensitive, bracket-enclosed,
  optional `: <question>` payload (e.g.
  `[NEEDS DECISION: which encoding?]`). Markers inside HTML comments
  (`<!-- [NEEDS DECISION: ...] -->`) are intentionally ignored so
  drafts can carry TODO-style notes without triggering the resolver.
  Fuzzy prose like "needs decision on the auth flow" inside a
  paragraph does NOT trigger detection — the marker is the explicit
  contract.
- **`## Open Questions`:** explicit "none identified" / "none" /
  "n/a" / "tbd" bullets are recognized as placeholders and skipped.
  This preserves the v1.10.0 convention of declaring "no open
  questions" with an explicit bullet instead of an empty section.
- **Missing fields:** only `task.acceptance_criteria` and
  `task.verification.commands` are checked. Empty requirements text
  and empty feature descriptions are reserved for v1.15+ (the
  detection module accepts `requirements=` and `features=`
  parameters now to avoid a signature break later).

### Fixed (post-greptile review)

- **OQ IDs are now contiguous after placeholder skipping.** Previously
  the counter advanced for every Open Questions item including
  `"none identified"` / `"n/a"` / `"tbd"` placeholders, so a PRD with
  `[placeholder, real, placeholder, real]` produced `OQ002` + `OQ004`
  instead of `OQ001` + `OQ002`. Non-contiguous IDs would confuse the
  resolver skill (which iterates decisions sequentially). The
  contiguous-ID counter only advances for items that survive the
  placeholder filter; the `location` field still carries the source
  position so users can find the item in the file.
- **MCP `find_decisions` now matches the CLI on parse failures.**
  Previously the MCP tool silently proceeded when `parse_prd` returned
  errors, yielding a deceptive `0 open_questions` count even though
  the PRD was malformed. Now it raises `ToolError` with the first
  few errors summarised in the message, matching the CLI's exit-1
  behaviour so MCP clients see the parse failure before drawing
  conclusions from the decision list.
- Test suite is **1024 passing** (was 1022 — +1 regression test for
  the OQ contiguous-ID fix, +1 for the MCP parse-failure fix).

---

## [1.13.0] — 2026-05-26

Two-axis release driven by a single user observation: agents using
anvil were ending every workflow with a CLI to-do list ("1. Run
`prd parse`, 2. Run `prd review`, 3. Run `prd review --approve`,
4. Run `plan`, …") instead of driving the workflow inline. The
handoff pattern only makes sense when work is leaving the session
entirely; with one agent and one user in the same conversation, the
agent should drive each command, surface its output, and present the
next decision. The fix is at the skill layer (so every future session
inherits it) AND at the MCP layer (so non-shell MCP clients can drive
the full workflow without dropping to a Bash tool they may not have).

### Changed

- **Skills now drive the workflow interactively.** `start-prd`, `prd`,
  `plan`, `claim`, and `finish` rewrote their interactive sections to
  encode "agent runs each state-engine command itself; surfaces the
  output inline; asks the user the next decision; runs the next
  command on the user's word." The closing-with-a-CLI-to-do-list
  pattern is explicitly named as an anti-pattern in each of these
  skills. The two approval gates (`prd review --approve` and
  `apply --approve`) are the only hard handoffs, and even at those
  the agent asks "ready? yes/no" and runs the command on confirmation
  — it does not paste the command for the user to type. An explicit
  escape hatch is preserved: when the user says "just give me the
  commands," or when the runtime has no execution tool, the CLI list
  output is correct.
- README test-count badge 975 → 993; version badges 1.12.1 → 1.13.0.

### Added

- **8 new MCP tools** so the full PRD → plan → review → approve →
  claim → submit → apply workflow is drivable from any MCP client
  without a Bash tool: `init_project`, `get_project_status`,
  `parse_prd`, `review_prd`, `plan_tasks`, `score_tasks`,
  `review_tasks`, `apply_review_decision`. Total MCP surface is now
  21 tools (was 13). All new tools call the same shared modules the
  CLI calls — no logic duplication. Structured response models
  return well-shaped data; operational failures (missing file,
  uninitialized project) raise `ToolError` with a clear message,
  parse-level errors are returned as data so callers can inspect-
  and-retry without exception handling.
- **19 new MCP regression tests** in `tests/test_mcp.py` covering
  happy and error paths for each new tool, including a regression
  test for the `plan_tasks` ordering guard (must run `parse_prd`
  first or fail loudly). Suite is 994 passing (was 975 in v1.12.1).

### Fixed (post-greptile review)

- **`plan_tasks` no longer mutates state when called out of order.**
  Previously, calling `plan_tasks` before `parse_prd` would emit
  `feature.created` and `task.created` events into a backend with
  no PRD row — leaving `review_prd` and `apply_review_decision` to
  fail later with "No PRD found in state" after the state was
  already partially mutated. The tool now verifies `backend.get_prd()`
  returns a row before emitting any events and raises `ToolError`
  with a clear "call parse_prd first" message if not.
- **`init_project` resource cleanup hardened.** `backend.initialize()`
  was being called outside the `try/finally` block, so a failure
  during schema bootstrap would leak the backend connection. Moved
  inside the try so `backend.close()` always runs.
- **`score_tasks` docstring clarified** to match the CLI's intentional
  behavior: an explicit `task_id` always re-scores (whether or not
  scores are complete); omitting `task_id` only scores tasks whose
  Score is incomplete. The asymmetry is deliberate and matches
  `anvil score [TASK_ID]`.
- New "Workflow tools (v1.13.0)" section in `docs/mcp.md`,
  organized by lifecycle phase (Bootstrap → PRD → Planning → Review)
  with parameters, returns, examples, and CLI equivalents for each
  new tool.

### Migration

No breaking changes. The 13 existing MCP tools, all 23 CLI commands,
and the on-disk state schema are unchanged. The skill rewrites
change the agent's interactive behavior only — any CLI script,
hook, or pre-existing workflow that invokes `anvil` commands
directly continues to work identically. Plugins that wrap anvil
(`fakoli-flow`, `fakoli-crew`) are unaffected.

### Notes on `init_project` and helper extraction

The MCP `init_project` tool inlines a small amount of project-seeding
logic that today lives privately inside `cli/init_status.py`
(`_apply_init_event`). A future cleanup could lift that helper into
`state/init.py` so MCP and CLI share a single source. Flagged in the
codebase; not blocking for v1.13.0. Similarly `_PRD_FILENAME` is
duplicated between `cli/_helpers.py` and `mcp_server.py` to avoid
pulling typer into the MCP import graph — worth promoting to a
shared constants module someday.

---

## [1.12.1] — 2026-05-26

Bug-fix release for a silent-drop in the PRD parser. Reported by a user
running anvil in another project: an agent authored a PRD with
`## Features` written as bullets (instead of `### F001:` H3 blocks) and
`anvil prd parse` reported "0 features, 0 tasks" before exiting
0. The agent's work was invisibly discarded.

### Fixed

- `_parse_features` and `_parse_tasks` in
  `bin/src/anvil/planning/template.py` now emit a `ParseError`
  when their section body has non-empty / non-comment content but
  produces zero `### Fxxx:` / `### Txxx:` H3 blocks. The error message
  names the canonical format and points to `docs/prd-template.md`. This
  matches the parser's own documented contract ("Silent fallback is
  explicitly rejected") — previously both functions returned `[]`
  silently when the format was wrong, in violation of that contract.
  CLI behavior unchanged: any `ParseError` causes `prd parse` to exit
  non-zero before writing to `state.db`, so no malformed data ever
  reaches state.
- `_parse_features` and `_parse_tasks` now also warn when an H3 heading
  looks like an attempted custom ID (e.g. `### F-DURABILITY: foo`,
  `### T-1: foo`) but does not match the required `Fxxx` / `Txxx`
  format (letter + 3+ digits). Previously the parser silently auto-
  assigned a default ID and the user had no way to know their custom
  ID had been discarded. Conservative detection (only F/T + a
  separator like `-`, `_`, `.`) so legitimate auto-ID fallbacks for
  English headings like `### My Feature` keep working unchanged.
- An empty `## Features` or `## Tasks` section header (no body at all)
  still parses cleanly with no error, preserving the existing escape
  hatch for "section declared but no items yet".

### Added

- Eight regression tests in `tests/test_template.py`: four
  `features` variants (`bullets_only`, `prose_only`,
  `empty_section`, `malformed_id_prefix`) and four parallel `tasks`
  variants. Suite now 975 passing (was 967).

### Changed

- README test-count badge 967 → 975; version badges 1.12.0 → 1.12.1.
- The malformed-ID warning messages in `_parse_features` and
  `_parse_tasks` were reworded to be unambiguous: the previous "an ID
  was auto-assigned" phrasing described an internal fallback the user
  never sees (because the ParseError blocks persistence), which read
  as if the parse had succeeded. The new wording names the format
  violation and tells the user to rename the heading to
  `### F001:` / `### T001:` and re-run.

---

## [1.12.0] — 2026-05-26

Skill rename to resolve the `brainstorm` namespace collision with
`fakoli-flow:brainstorm`. The anvil skill that drafts a PRD from
a rough idea is now `start-prd` — slug, slash command, and every
cross-reference updated. The skill router can now route "let's
brainstorm" cleanly to `fakoli-flow` and "start a PRD" cleanly to
anvil. Markdown-choreography only; no Python source, schema, or
test changes (967 tests still passing).

### Changed

- **BREAKING:** `/anvil:brainstorm` skill renamed to
  `/anvil:start-prd`. Directory (`skills/brainstorm/` →
  `skills/start-prd/`), frontmatter `name:`, slash-command form, and
  every cross-reference in README, `architecture.md`,
  `skills-reference.md`, `how-to/integrating-with-fakoli-flow-and-crew.md`,
  `skills/prd/SKILL.md`, `roadmap.md`, `phase-11-backlog.md`, and the
  v0 spec updated. Skill description rewritten to remove "brainstorm"
  as a trigger word and add concrete trigger phrases ("start a PRD",
  "draft requirements", "author a PRD", "spec out a project"). Bridge
  to `/fakoli-flow:brainstorm` (the OTHER plugin's skill, unrelated to
  this rename) is unchanged and still fires when fakoli-flow is
  installed.
- Marketing copy updated to drop "brainstorms" in favor of "rough
  ideas" — README hero, `_positioning.md` canonical (Q) sentence,
  `bin/pyproject.toml` description, CLI `--help` description, and the
  v0 spec. The vocabulary now matches the renamed skill instead of
  pulling the skill router back toward the colliding term.
- README badges refreshed: version 1.10.0 → 1.12.0; test count 965 → 967
  (catches up the v1.11.0 additions that did not update the badges).

### Migration

If your scripts, hooks, CI, agents, or muscle memory invoke
`/anvil:brainstorm`, change to `/anvil:start-prd`. No
state schema migration is required (still v3, unchanged since v1.8.0);
no CLI command was renamed (no `anvil brainstorm` CLI command
ever shipped — only the skill); the rename is purely at the skill-
router layer.

### Notes on historical artifacts

The `docs/tech-debt-backlog.md` entry P9-5 and the
`docs/specs/2026-05-26-plugin-audit-and-critics.md` audit doc still
reference `skills/brainstorm/SKILL.md` — these are intentionally
unchanged. P9-5 documents a Phase 9 fix to the file at the path it had
AT THAT TIME; the audit doc is a frozen v1.9.0 snapshot. Rewriting
either would obscure audit traceability. Status files under
`docs/plans/agent-*-status.md` are similarly frozen.

### Versioning note

By strict SemVer this is a breaking change and would warrant 2.0.0.
The project reserves 2.0.0 for the inflection-point release planned
on `docs/roadmap.md` (sync v2, replay tooling, multi-backend sync
abstraction). This rename is shipped as 1.12.0 with an explicit
`BREAKING:` marker to keep the v2.0 slot intact.

---

## [1.11.0] — 2026-05-25

Comprehensive documentation overhaul + 3 silent-failure bug fixes surfaced
during research (missing fakoli-flow detection in `claim`/`execute` skills;
`submit` CLI hardcoding `screenshots=[]`; broken `grep -q "^fakoli-..."`
anchor pattern at 5 sites). No breaking changes; the `submit --screenshots`
flag is the only behavior change and is purely additive (optional).

### Added (docs)

- **Positioning + foundation:** `docs/_positioning.md` (internal reference),
  rewritten `README.md` with hero block / 5-wedge comparison / working v1.10.0
  Quick Start, `docs/architecture.md` (579 lines, 3 mermaid diagrams, 25-entity
  data model, replay guarantee), `docs/design.md` (301 lines, design rationale
  + Deferred decisions). [PR #54]
- **User-facing how-to (5 files):** `docs/how-to/getting-started.md`,
  `authoring-a-prd.md`, `claiming-and-shipping-a-task.md`,
  `syncing-with-github.md`, `integrating-with-fakoli-flow-and-crew.md`. [PR #55]
- **Reference (5 files):** `docs/cli-reference.md` (all 23 commands),
  `skills-reference.md` (7 skills + dependency graph),
  `agents-reference.md` (6 agents + defer-to-crew mapping),
  `hooks-reference.md` (4 hooks + non-blocking contract),
  `faq.md` (15 Q&A). [PR #55]
- **Visual brand:** `assets/logo-{64,256,1024}.png` generated via
  nano-banana-pro; `assets/diagrams/{component,lifecycle,trinity}.mmd`
  for re-render. [PR #54]
- **CLI flag:** `submit --screenshots PATH1,PATH2` for tasks whose
  `verification.required_evidence` includes "screenshots". 2 new tests. [PR #55]

### Fixed (silent-failure bugs)

- **`skills/claim/SKILL.md` + `skills/execute/SKILL.md` had no fakoli-flow
  detection snippet** — both used prose-only "when fakoli-flow is installed"
  framing with no shell check, so the bridge to `/fakoli-flow:execute`
  never fired even when the plugin was installed. Added Step 0 detection
  blocks mirroring `skills/brainstorm/SKILL.md`. [PR #55]
- **`submit` CLI hardcoded `screenshots=[]`** — Evidence model field,
  evidence_complete gate, and gate tests all existed; only the CLI surface
  was missing. Tasks requiring "screenshots" evidence were unsatisfiable.
  See `Added` above. [PR #55]
- **`grep -q "^fakoli-..."` detection pattern was broken at 5 sites** —
  real `claude plugin list` output starts with `  ❯ ` indent + `@source`
  suffix; `^` anchor never matched. Trinity composition silently never
  activated even when fakoli-flow + fakoli-crew were installed. Fixed in
  `skills/brainstorm/SKILL.md`, `skills/finish/SKILL.md`, and 3 PR-B docs.
  Updated 5 roadmap entries (P11-SK-S1/S2/S3/S6) so future work uses the
  corrected pattern. [PR #55]
- **PR A doc drifts caught during PR B research:** `docs/architecture.md`
  default lease 15 → 60 min; `docs/mcp.md` `get_next_task` ranking corrected
  (priority desc → complexity asc → created_at asc, not agent_suitability);
  `agents/sentinel.md` frontmatter `allowed-tools:` → `tools:` (Phase 10
  audit leftover); `docs/design.md` + `docs/faq.md` config file pointer
  `settings.json` → `config.yaml`. [PR #55]

### Changed

- `.claude-plugin/plugin.json` description tightened from 192 → 131 chars;
  10 high-signal keywords (added `local-first`, `runtime-neutral`,
  `terraform-for-work`, `llm-work-packets`; dropped generic terms). [PR #54]
- Repo root `README.md`: added anvil to "The Fakoli Ecosystem"
  trinity narrative; refreshed "Available Plugins" row (removed stale
  "Scaffolded; phases 2-8 in progress"). [This PR]

### Tests

- 967 passed (up from 965 in v1.10.0; +2 for the new `--screenshots` flag).
- 0 regressions.
- Manual scratch-dir verification of the getting-started.md Quick Start
  end-to-end (init → PRD → parse → review → approve → plan → score → claim
  → packet → submit → apply → done).

---

## [1.10.0] — 2026-05-26

Phase 10: first plugin-dev best-practices audit + 8 MUST FIX items closed
inline. fakoli-crew v2.2.0 ships 5 new cross-plugin specialist critic agents
(agent-critic, skill-critic, hook-critic, mcp-critic, structure-critic — see
`plugins/fakoli-crew/CHANGELOG.md` § 2.2.0); this release applies their first
audit pass against anvil v1.9.0's surface area and closes every MUST
FIX they surfaced. 57 SHOULD FIX / CONSIDER / NIT items are deferred to
`docs/phase-11-backlog.md` with per-critic provenance preserved.

Audit doc: [`docs/audits/2026-05-26-plugin-audit.md`](docs/audits/2026-05-26-plugin-audit.md).
Phase plan: [`docs/plans/2026-05-26-phase-10-plugin-audit.md`](docs/plans/2026-05-26-phase-10-plugin-audit.md).

Ships v1.10.0.

### Fixed — MUST FIX items closed in this phase (8)

1. `agents/critic.md:26` — renamed `allowed-tools:` → `tools:` (the `allowed-tools:` key is the *command* frontmatter key; on agent files it is silently ignored and the agent loads with full unrestricted tool access, defeating the Iron Rule's least-privilege intent for the read-only reviewer). Found by agent-critic.
2. `agents/docs-scribe.md:67` — same `allowed-tools:` → `tools:` rename. Found by agent-critic.
3. `agents/marketplace-scribe.md:67` — same rename. Found by agent-critic.
4. `agents/planner.md:44` — same rename (highest-stakes occurrence: the Iron Rule explicitly forbids planner writes to `.anvil/`, but the frontmatter that was supposed to enforce least-privilege was silently ignored). Found by agent-critic.
5. `agents/state-keeper.md:45` — same rename (highest blast-radius occurrence given agent proximity to git and state files). Found by agent-critic.
6. `skills/finish/SKILL.md:249-252` — removed dangling `/anvil:sentinel` slash-command reference (sentinel is an agent surface, not a skill — there is no slash-command for it; the broken reference would 404 on invocation). Replaced with explicit `claude plugin list 2>/dev/null | grep -q "^fakoli-crew"` shell gate plus prose explaining the agent-dispatch contract so the next Claude session cannot re-introduce the bug by pattern-matching. As a bonus closure (welder Fix #6), this also resolves what would have been P11-SK-S5 in the Phase 11 backlog ("Fuzzy detection for `fakoli-crew:sentinel` — no shell check") — reducing the deferred SHOULD FIX count from 25 to 24. Found by skill-critic.
7. `skills/prd/SKILL.md:57-59` — added overwrite-confirmation gate before `prd parse` mutates `state.db` rows. Mirrors the brainstorm/SKILL.md:162-176 exists-check + summary + `yes/no/save-as-backup` prompt; applied at two mutation points (the editor open in Step 1 and the re-parse during iteration) since the prd flow has two destructive entry points. Found by skill-critic.
8. `README.md:103` — Skills row claimed a `verify` skill that does NOT exist on disk (overpromise); rewrote the row to list the 7 real skills (brainstorm, prd, plan, claim, execute, finish, state-ops) and document that verification is delegated to `fakoli-flow:verify` and `fakoli-crew:sentinel`, so the reader understands *why* `verify` is absent. Found by structure-critic.

### Added — 5 new fakoli-crew critic agents (audit infrastructure)

fakoli-crew v2.2.0 (see its CHANGELOG) ships 5 new cross-plugin specialist
critic agents that this release was the first subject of:

- **agent-critic** (magenta) — reviews `<plugin>/agents/*.md` frontmatter (name/description/color/model/tools), color-collision detection, `<example>` count discipline, the `allowed-tools:` vs `tools:` antipattern, defer-to validity, and file-length proportionality.
- **skill-critic** (teal) — reviews `<plugin>/skills/*/SKILL.md` for frontmatter validity, one-question-at-a-time discipline, hard-gate presence on irreversible actions, decision-flow clarity, lazy-loading discipline, and the no-fuzzy-detection rule.
- **hook-critic** (gray) — reviews `<plugin>/hooks/*.sh` + `hooks.json` for shebang portability, `${CLAUDE_PLUGIN_ROOT}` usage, stdin handling, hot-path performance, idempotency, matcher specificity, and — critically — whether the script's error-handling style matches the plugin's declared hook contract (e.g., anvil's non-blocking contract forbids `set -e`).
- **mcp-critic** (white) — reviews `.mcp.json` + MCP server source for schema validity, `@mcp.tool()` decoration discipline, typed parameter annotations, structured error returns, secret-leak risks, transport choice rationale, and actor-identification on mutating tools.
- **structure-critic** (brown) — reviews cross-plugin structure: `plugin.json` required fields, version sync across `plugin.json` / `pyproject.toml` / `__init__.py` / `marketplace.json` / `registry/index.json`, README surface tables vs actual filesystem counts, CHANGELOG Keep-a-Changelog discipline, and `[Unreleased]` hygiene after a tag.

Each critic ships with a known-bad fixture at `plugins/fakoli-crew/tests/fixtures/audit-targets/` and a manual-verification recipe at `plugins/fakoli-crew/tests/RECIPES.md`. Together they form the cross-plugin critic surface — anvil was the first subject; future plugins (fakoli-flow, fakoli-speak, etc.) can run the same five-critic audit.

### Deferred — Phase 11 backlog

57 SHOULD FIX / CONSIDER / NIT items deferred (25 SHOULD FIX, 21 CONSIDER, 11 NIT) — full per-critic detail with file:line provenance, recommended actions, and cross-cutting themes (no-fuzzy-detection across skills, non-empty actor validation across MCP tools, hot-path perf budget on hook scripts, hook-contract documentation gap, phase-status table drift, composition duplication across doc/state agents, install-messaging drift in README + CHANGELOG) recorded in [`docs/phase-11-backlog.md`](docs/phase-11-backlog.md). The Phase 10 welder Fix #6 closed one SHOULD FIX item (P11-SK-S5, fuzzy detection for `fakoli-crew:sentinel`) as a bonus during the dangling-slash-command fix — `phase-11-backlog.md` marks that line `[CLOSED in Phase 10 Fix #6]`. Net deferred SHOULD FIX count is **24** (down from 25 at audit time).

### Documentation

- `docs/audits/2026-05-26-plugin-audit.md` (NEW) — consolidated audit with severity-sorted findings table, per-critic detail sections, and "Items applied this phase" annotations linking each MUST FIX to the welder fix that closed it.
- `docs/phase-11-backlog.md` (NEW) — Phase 11 work-tracking doc mirroring the format of `docs/phase-9-backlog.md`. Includes 7 cross-cutting themes and explicit Phase 11 planner notes.
- `docs/plans/2026-05-26-phase-10-plugin-audit.md` (NEW) — full 16-task / 9-wave execution plan for the audit + fix cycle.
- `docs/specs/2026-05-26-plugin-audit-and-critics.md` (NEW) — source spec for the 5 new critic agents.

### Tests

- 965 passing (unchanged from v1.9.0 — all 8 MUST FIX items were markdown/frontmatter edits; no Python code touched, no behaviour drift).

---

## [1.9.0] — 2026-05-25

Phase 9: audit honesty + multi-provider config + Phase 7 cleanup + two
new plugin-owned doc agents. The sync engine's audit stream is now
truthful — six dishonest `sync.pull.completed` emissions on
conflict-resolution branches that did not actually mutate local state
now correctly emit `sync.pull.deferred`. The `local_moved`-only pull
path bug-collapse (mapping was set to `in_sync` despite local being
ahead) is fixed to set `sync_state="local_ahead"` and emit a
`sync.push.deferred` hint. The `SyncAuditPayload` model is now a Pydantic
v2 discriminated union with `extra="forbid"` per action — field-vs-action
mismatches surface as `ValidationError` instead of being silently
accepted. A new opt-in `sync.providers` config key lets projects narrow
or fully opt out of provider iteration.

Phase 7 leftovers closed: `RecordedLLMProvider.record_key` now folds
`max_tokens` and `temperature` into the canonical hash (two recordings
under different tuning args no longer collide); brainstorm-skill
fakoli-flow detection uses an explicit `claude plugin list` check rather
than fuzzy prose; `anvil expand --use-llm --format prd` emits
paste-ready markdown blocks matching `docs/prd-template.md` (with
`**Feature:**` and `**Priority:**` populated from the parent task).

Ships v1.9.0.

### Added — Audit honesty (T5)

- `bin/src/anvil/cli/sync.py`:
  - Six deferred conflict-resolution branches (`local_wins_deferred`, `remote_wins_deferred`, `prompt_defaulted_to_local`, `prompt_chose_local`, `prompt_chose_remote`, `prompt_skipped`) now emit `sync.pull.deferred` instead of the prior `sync.pull.completed`. The JSONL is safe to grep for "did this task actually update?".
  - `local_moved`-only pull path (local Task ahead of `last_synced_at`, no remote movement) now sets `sync_state="local_ahead"` (was `in_sync` — wrong) and emits `sync.push.deferred` with `resolution="local_moved_no_push"`. Operators grep `events.jsonl` for the token to find tasks awaiting a follow-up `--push`.
  - `_resolve_conflict` signature: `-> bool` → `-> tuple[bool, bool, str]` = `(resolved, applied, resolution)`. Internal private function; no external caller impact.
- `bin/src/anvil/state/payloads.py`:
  - 13 new per-action Pydantic v2 subclasses replacing the v1.8.0 single all-optional `SyncAuditPayload` model: `SyncBatchStartedPayload`, `SyncBatchCompletedPayload`, `SyncPushStartedPayload`, `SyncPushCompletedPayload`, `SyncPushDeferredPayload`, `SyncPushFailedPayload`, `SyncPullStartedPayload`, `SyncPullCompletedPayload`, `SyncPullDeferredPayload`, `SyncPullFailedPayload`, `SyncConflictDetectedPayload`, `SyncReconciliationStartedPayload`, `SyncReconciliationCompletedPayload`. Each has `extra="forbid"`; field-vs-action mismatches now fail at validate time.
  - `SyncAuditPayload` preserved as a backwards-compat module-level type-form (`Annotated[Union[...], Field(discriminator="action")]`) — existing imports resolve; callers that used `SyncAuditPayload.model_validate(d)` directly migrate to `TypeAdapter(SyncAuditPayload).validate_python(d)` or look up the concrete subclass via `ACTION_TO_PAYLOAD[action]`.
  - `ACTION_TO_PAYLOAD: dict[str, type[BaseModel]]` exported for direct dispatcher lookup.
- `bin/src/anvil/state/sqlite.py`: dispatcher uses the new `ACTION_TO_PAYLOAD` registry; the prior 13 explicit `(SyncAuditPayload, handler)` entries collapse into a single dict comprehension. When a new sync action is added the dispatcher auto-picks it up.

### Added — Multi-provider config (T5)

- `bin/src/anvil/config.py`: new optional top-level `sync.providers` config key parsed into `Config.sync_providers: tuple[str, ...] | None`. Three-way semantics:
  - Key absent → `None` → fall back to `sorted(PROVIDER_REGISTRY)` (v1.8.0 default).
  - `sync.providers: [a, b]` → `("a", "b")` → use the explicit list.
  - `sync.providers: []` → `()` (NOT `None`) → opt out of every provider; sync is a no-op.
- `bin/src/anvil/cli/sync.py::_resolve_configured_providers` is the single lookup seam; wrapped in `try/except (ValueError, OSError)` so a malformed config falls back to the registry rather than breaking `anvil sync`. Loud config errors are the job of `init`/`doctor`.
- Documented in `docs/sync-providers.md` § "Per-provider configuration (v1.9.0)" — full schema, three-way table, fallback semantics, reconciliation interaction.

### Added — Phase 7 cleanup (T6)

- **C2 — `RecordedLLMProvider.record_key`:** signature extended to `record_key(system, user, *, max_tokens=4096, temperature=0.0)`; canonical hash folds `str(int(max_tokens))` and `repr(float(temperature))` as length-prefixed chunks 3 and 4. `repr(float(...))` normalises `0`, `0.0`, and `0.00` to the same key. Default values mirror `LLMProvider.generate` defaults so back-compat calls with no kwargs still work. The v1.7.0 footgun where two recordings under different tuning args silently collided is closed; tests that pre-compute keys MUST pass the matching values the engine uses at lookup time (`_SCORE_EXPLAIN_MAX_TOKENS=300`, `_DESCRIPTION_ENRICH_MAX_TOKENS=400`, `_EXPAND_MAX_TOKENS=2000`). Collateral updates to 8 call sites in `tests/test_llm_integration.py` + 1 in `tests/test_cli.py`.
- **C3 — Brainstorm-flow detection:** `skills/brainstorm/SKILL.md` replaces the fuzzy "if fakoli-flow seems available" prose with an explicit `claude plugin list 2>/dev/null | grep -q "^fakoli-flow"` shell check plus exit-code-driven branching. Slash-command name corrected from `/flow:brainstorm` (typo) to the fully-qualified `/fakoli-flow:brainstorm` — the typo would have broken the bridge invocation when fakoli-flow IS installed. Detection is OPTIONAL: exit non-zero (or missing `claude` binary) falls through to the local interview.
- **C4 — `expand --format prd`:** new `--format {text,prd}` Typer option on `anvil expand`. `--format prd` emits ready-to-paste markdown blocks matching `docs/prd-template.md`'s `## Tasks` schema (H3 heading, `**Feature:**`, `**Priority:**`, `**Likely files:**`, description paragraph, `**Acceptance criteria:**` bullets, `**Verification:**` with `TODO` placeholder). `**Feature:**` and `**Priority:**` are populated from the parent task's metadata (Phase 9 critic CONSIDER fix — eliminates the manual-edit step in the paste-into-`prd.md` workflow). Default `--format text` keeps the v1.7.0 human-readable per-subtask block output unchanged. The new mode round-trips cleanly through `parse_prd` — see `tests/test_cli_plan.py::test_prd_format_output_round_trips_to_prd_parser` for the canonical proof.

### Added — Two new plugin-owned doc agents (T4)

- `plugins/anvil/agents/marketplace-scribe.md` — cyan, opus. Owns `.claude-plugin/marketplace.json`, the root `README.md` plugins table, and `registry/*.json` index files. Fires after any version bump, agent add/remove, or skill add/remove inside anvil. Defers to `fakoli-crew:keeper` when not in an anvil context. Includes `Bash` in `allowed-tools` so it can run `scripts/generate-index.sh` and validate regenerated JSON via `python -m json.tool` / `jq .`.
- `plugins/anvil/agents/docs-scribe.md` — purple, opus. Owns the plugin's `docs/` folder, `CHANGELOG.md`, and the `description` field of `.claude-plugin/plugin.json`. Audits cross-references between docs — broken `[[wikilinks]]`, mismatched section anchors, dangling `see also` pointers, references to moved/archived files. Fires after any schema change, new CLI command, new agent, or completed phase. Defers to `fakoli-crew:herald` for general README work. No `Bash` (pure docs work).
- Color collisions checked vs the existing four agents (planner=white, critic=magenta, sentinel=gray, state-keeper=teal). Cyan and purple are unused by every existing anvil agent and by every fakoli-crew agent.

### Added — Documentation

- `docs/phase-9-backlog.md` (NEW) — forward-looking v2.x roadmap. Carries the items consciously deferred from Phase 9 (LinearIssuesProvider, MondayBoardsProvider, JiraIssuesProvider, GitHubProjectsProvider, webhook-based sync spec, immediate-apply `*_applied` resolution variants, `anvil snapshot` CLI, MCP sync tools surface, per-provider config nesting), plus the carry-forward `CL-N` / `TQ-N` / `PS-N` items still open in `docs/tech-debt-backlog.md`.
- `docs/github-sync.md` — new "Audit honesty" section explaining `sync.pull.completed` vs `sync.pull.deferred` semantics, the `local_ahead` mapping state, and the full controlled vocabulary of `resolution` tokens (including the new `local_moved_no_push`). Audit events table grows by one (`sync.push.deferred`).
- `docs/sync-providers.md` — new "Per-provider configuration (v1.9.0)" section documenting the optional `sync.providers` config key with the three-way absent/explicit/empty semantics.
- `docs/llm.md` — corrected `RecordedLLMProvider.record_key` signature and example (tuning args participate in the key per Phase 9 C2); new `expand --format prd` worked example with paste-ready output and round-trip note.
- `docs/tech-debt-backlog.md` — new "Phase 8 / Phase 9 closures" section at the top covering P9-1..P9-8 (audit honesty fixes, discriminated payloads, multi-provider config, record_key fix, brainstorm detection, --format prd, two new doc agents). Status legend grows `MOVED-P9-BACKLOG` for items forward-carried to `phase-9-backlog.md`.
- `README.md` — version badge bumped to 1.9.0; phase table marks phases 1–9 Done with per-release version pointers; new "Plugin-owned agents" section listing all six agents (planner, critic, sentinel, state-keeper, marketplace-scribe, docs-scribe) with color + ownership + crew defer-to target.

### Changed

- `bin/src/anvil/state/payloads.py::SyncPullCompletedPayload` docstring — enumerates the four honest emission conditions (clean pull, tombstone, in_sync no-divergence, local-moved-only with paired `sync.push.deferred` hint). The local-moved-only branch is the one most likely to surprise readers who expect "completed" to imply "mutated"; the docstring is explicit that the pull terminal is honest because the pull itself succeeded — only the follow-up push is deferred.
- `bin/src/anvil/cli/sync.py::_emit_audit` docstring — updated to reflect that the discriminated union has REQUIRED fields per action now; the None-strip is still load-bearing for OPTIONAL fields with `None` defaults (JSONL would otherwise carry `"audit_note": null` rows that clutter forensic queries and break `jq 'has("audit_note")'` filters).
- `bin/src/anvil/planning/llm.py` module docstring — `RecordedLLMProvider` key shape description corrected from the stale `sha256(system + "---" + user)` to the length-prefixed `sha256` over `(system, user, max_tokens, temperature)`.
- `bin/src/anvil/state/payloads.py:393-399` — `TypeAlias` terminology corrected to "module-level type-form (`Annotated[Union[...], Field(discriminator="action")]`)". `SyncAuditPayload` is NOT a `typing.TypeAlias` (no `: TypeAlias =` annotation); it is a Pydantic discriminated-union type form.
- `bin/src/anvil/cli/plan.py::_render_subtask_proposals_as_prd` — added optional kw-only `parent_feature_id` and `parent_priority` parameters. The CLI caller (`expand --format prd`) now threads `task.feature_id` and `str(task.priority)` through — eliminates the prior empty `**Feature:**` line and `**Priority:** medium` default that the user had to manually edit before `prd parse`. Default behaviour (when the helper is called without parent context) preserved for backwards compat.
- `tests/test_llm.py::test_separator_prevents_collision` docstring — updated from the stale `"\n---\n"` separator description to "Length-prefixed encoding prevents concat-collisions across any byte boundary." Assertion unchanged.

### Tests

- 935 → 964 passing, 3 skipped (Δ +29 net):
  - +14 new tests authored across T5 / T6: 5 in `test_cli_sync.py` (deferred-branch emission, local_moved_no_push), 7 in `test_config.py` (sync_providers three-way semantics), 2 in `test_sqlite.py` (dispatcher uses concrete subclasses).
  - +4 new tests in `test_llm.py::TestRecordedLLMProviderKey` for the C2 tuning-args participation.
  - +11 new tests in `test_cli_plan.py` (greenfield) covering `--format text` baseline, `--format prd` blocks + round-trip + bullets + headings + suppression of legacy delimiter, validation precedence, and `--help` documentation.
  - 1 inverted-name test: `test_ignores_max_tokens_and_temperature` → `test_distinguishes_max_tokens_and_temperature` (assertions inverted to lock the new contract).
  - 15 pre-existing tests that were failing on T3's payload changes are now passing again (dispatcher fix in `state/sqlite.py`).
- One existing test (`test_prd_format_includes_template_fields`) updated to assert the new `**Feature:** F001` and `**Priority:** high` shape (inherited from the parent T001) rather than the prior empty-Feature / default-medium values.

### Migration notes

- Schema version unchanged (still v3 from v1.8.0). No DB migration required.
- The audit-event action change is forward-compat for log readers: a tool that filtered `sync.pull.completed` on v1.8.0 will now see fewer rows under v1.9.0, but the rows it does see are honest. To recover the union, filter `(.action == "sync.pull.completed" or .action == "sync.pull.deferred")`.
- The `SyncAuditPayload` rename is backwards-compatible at the import path; callers that used `SyncAuditPayload.model_validate(d)` directly must migrate to `TypeAdapter(SyncAuditPayload).validate_python(d)` or look up the concrete subclass via `ACTION_TO_PAYLOAD[action]`.
- `RecordedLLMProvider.record_key` now requires matching `max_tokens` / `temperature` at lookup time when the engine overrides the defaults — tests that pre-computed keys with the no-kwargs form will see `LLMProviderError("no recording for prompt hash ...")` if the engine passes non-default tuning args. The collateral updates in `tests/test_llm_integration.py` (this release) are the template.
- The new `agents/marketplace-scribe.md` and `agents/docs-scribe.md` are created in v1.9.0 but only become invocable as subagent types in NEW sessions (Claude Code discovers agents at session start). Existing sessions need to restart to pick them up.

---

## [1.8.0] — 2026-05-25

Phase 8: bidirectional sync. Adds a multi-provider `SyncProvider` Protocol
abstraction with GitHub Issues as the first concrete implementation, wires
opt-in `anvil sync` CLI surface with bidirectional push/pull, four
conflict-resolution strategies, watch-loop polling, and full reconciliation
between SQLite state / filesystem / git. The Protocol is registry-driven
so Monday, Linear, Jira, and custom providers can plug in without engine
changes — see docs/sync-providers.md for the contributor guide.

The schema gains a sync_mappings table (SCHEMA_VERSION 2 → 3) with an
auto-upgrade path for existing v1.7.x databases; the diff is purely
additive. See docs/migrations.md.

### Added — Sync abstraction layer

- `bin/src/anvil/sync/` package — `SyncProvider` Protocol (`push_task`, `fetch_task`, `list_tasks`, `delete_task`, `health_check`), `ExternalRef` + `ExternalTask` + `ProviderHealth` Pydantic models, `SyncProviderError` hierarchy (`AuthenticationFailed`, `RateLimitExceeded`, `ProviderUnavailable`, `SyncConflict`), `RecordedSyncProvider` test double (sha256 length-prefixed keyed), `PROVIDER_REGISTRY` + `register_sync_provider` / `get_sync_provider` / `list_sync_providers`. snake_case `provider_id` discipline.
- `bin/src/anvil/sync/providers/github_issues.py` — `GitHubIssuesProvider` concrete impl. Auto-registers as `"github_issues"` on package import. Dual transport: `gh` CLI primary, `httpx` + `GITHUB_TOKEN` fallback. Status mapping: 11 TaskStatus values → `status:*` labels; only `done` closes the issue. Body footer convention (`---\n_synced from anvil task {task_id}_`) is round-trippable via `_strip_footer`. Label preservation across pushes (HTTP transport reads existing labels first, preserves non-`status:*`).
- `bin/src/anvil/sync/clients/gh_cli.py` — subprocess wrapper for `gh issue create/edit/view/list/close`. Stderr-scan error classification (auth/rate-limit/network).
- `bin/src/anvil/sync/clients/github_http.py` — httpx wrapper with Link-header pagination + 1000-page safety cap + `responses`-style HTTP mocking via respx in tests.
- `bin/src/anvil/sync/reconciliation.py` — `ReconciliationEngine.scan() / fix(dry_run=False)` covering 6 discrepancy kinds: orphan_branch, orphan_packet, orphan_worktree, stale_claim, missing_sync_mapping, drift_sync_state. The first 4 have full fix paths; the latter 2 emit operator-facing CLI commands (`anvil sync provider <id> --pull --task T001`) for Phase 9 immediate-apply.

### Added — State schema (SCHEMA_VERSION 3)

- `sync_mappings` table: composite PK `(task_id, external_system)` + `UNIQUE(external_system, external_id)` (prevents cross-task collisions) + `FK ON DELETE CASCADE` to tasks + `external_url` + `provider_metadata_json`. Auto-upgrade from v1/v2 dbs in `_check_schema_version`; purely additive.
- New Pydantic models: `SyncMapping`, `SyncState`, `ConflictResolutionStrategy` (enums), `ExternalSystem` (snake_case enum).
- New payload models in `state/payloads.py`: `SyncMappingUpsertedPayload`, `SyncMappingDeletedPayload`, `SyncAuditPayload`. All `extra="forbid"`.
- New event handlers in `state/sqlite.py`: `_handle_sync_mapping_upserted`, `_handle_sync_mapping_deleted`, `_handle_sync_audit` (no-op like file_changed / progress.noted).
- New Backend Protocol methods: `get_sync_mapping(task_id, *, external_system=None)`, `list_sync_mappings(external_system=None)`, `apply_sync_mapping(mapping, *, actor='system')` (uses PENDING_EVENT_ID for race-free assignment).
- Nine new `sync.*` audit-event actions: `sync.batch.started/completed`, `sync.push.started/completed/failed`, `sync.pull.started/completed/failed`, `sync.conflict_detected`.

### Added — CLI surface

- `anvil sync` — runs reconciliation only (scan + print report).
- `anvil sync --fix --yes` — reconciliation + apply remediations (`--yes` required for non-interactive; refuses without `--yes` on cron/CI).
- `anvil sync <provider>` — push+pull all tasks via the named provider.
- `anvil sync github` — backwards-compat alias for `sync github_issues`.
- `anvil sync provider <id>` — generic provider invocation.
- Flags: `--push` (push-only), `--pull` (pull-only), `--task T001` (single-task), `--watch --interval N` (long-running poll loop with per-iteration error recovery), `--health` (provider auth probe, works pre-init), `--fix` (forces remote_wins conflict strategy).
- Conflict resolution: per-task `SyncMapping.conflict_resolution_strategy` ∈ {`local_wins`, `remote_wins`, `prompt`, `manual_merge`}. Resolution events emit `*_deferred` audit strings (truthful — actual mutations happen on the next pass; Phase 9 will wire immediate apply). `manual_merge` writes `.anvil/.sync-conflicts/<task_id>.md`; batch exits 2 if any task needed operator input.

### Added — Plugin-owned agent

- `plugins/anvil/agents/state-keeper.md` (color `teal`, model `opus`) — specialized agent for sync drift detection + reconciliation triage. Defers to `fakoli-crew:keeper` when crew is installed.

### Added — Documentation

- `docs/github-sync.md` (245 lines, 12 sections) — user-facing GitHub Issues sync reference.
- `docs/sync-providers.md` (280 lines, 11 sections) — contributor-facing Protocol reference with a step-by-step Linear-provider walkthrough.
- `docs/live-tests.md` — operator runbook for the nightly live-GitHub CI.
- `docs/migrations.md` — already shipped in 1.7.1; documents the v1/v2 → v3 auto-upgrade.

### Added — Nightly CI

- `.github/workflows/anvil-live-github.yml` — daily cron at 06:00 UTC. Gated on `secrets.ANVIL_TEST_GH_TOKEN` (job exits 0 with a notice if secret missing). Runs `pytest -m live_github -v` against a real test repo.
- `tests/test_github_issues_live.py` — 3 live tests (lifecycle, label preservation, rate-limit handling). All decorated `@pytest.mark.live_github`; excluded from default `pytest -q` via `addopts = "-m 'not live_github'"` in pyproject.toml. Cleanup contract: every test closes its own issues + leaves a `[fakoli-test]` UUID prefix for orphan sweeping.

### Changed

- `bin/pyproject.toml` — dropped unused `responses>=0.25` dev dep; added `httpx>=0.27` runtime; added `respx>=0.21` dev (for httpx-side HTTP mocking); registered `live_github` pytest marker.
- `cli/__init__.py` — wires the new `sync_app` Typer sub-app into the main CLI.

### Tests

- 750 → 917 baseline tests (+167) plus 3 live-github tests (excluded from default).
- Across waves: 58 sync_provider tests, 23 sync_mapping tests, 82 github_issues_provider tests, 37 reconciliation tests, 42+ cli_sync tests, 4 follow-up + Wave 3 fix-cycle additions.
- Ruff clean. Migration auto-upgrade path tested for v0/v1/v2 → v3.

### Migration notes

- Schema bumps 2 → 3. Existing v1.7.x databases auto-upgrade on first `anvil` invocation under 1.8.0. The diff is purely additive (new table, no shape changes to existing tables). No manual action required.
- The `responses` dev dep has been dropped; if you have a custom test that imported it, switch to `respx` for httpx mocking.
- `anvil sync` is a NEW command. Existing CLI commands are unchanged.

---

## [1.7.1] — 2026-05-25

Backlog cleanup. Closes 14 items from the deferred review backlog
(`docs/tech-debt-backlog.md`) — 6 correctness fixes (welder), 5 doc/config
cleanups, and the leftover deferrals from the PR #47 critic review. No
behavior changes visible to existing CLI / MCP callers.

### Fixed (correctness — welder backlog wave)

- CL-1: `hooks/check-claim.sh` now invokes the `hook check-claim --file --actor` CLI subcommand (Phase 5) instead of parsing `status --hook-format` output (Phase 4 leftover that fired on any claim regardless of file scope).
- CL-3: `_reap_stale_claims` no longer swallows `SchemaMismatch`; narrowed catch to `(StateLocked, TransactionAborted)` so DDL drift surfaces loudly.
- CL-8: `_handle_evidence_submitted` rejects double-submit with a different `evidence_id` for the same claim; emits the established `warn.idempotent_no_op` JSONL tombstone instead of inserting a duplicate row.
- CL-11: `planning.template.parse_prd` accepts an optional `clock: Clock`; `_parse_tasks` now requires a clock injection instead of calling `datetime.now()` directly.
- CL-13: `SqliteBackend.next_event_id` now raises `RuntimeError` via `_require_conn()` instead of returning the hardcoded `"E000001"` when the connection is closed — eliminates the silent collision-on-reopen footgun.
- PS-1: `ClaimManager._check_group_conflicts` collapses 1+N round-trips into 2 via a single bulk `list_tasks()` + in-memory `dict[task_id, Task]` lookup.

### Fixed (small cleanups)

- CL-7: `agents/critic.md` and `agents/sentinel.md` color collisions with fakoli-crew — state/critic purple → magenta, state/sentinel cyan → gray.
- CL-9: `review.gates._contains_test_keyword` no longer matches `pytest --collect-only` / `--co` (zero-test runs were satisfying the "tests pass" evidence gate).
- CL-14: `skills/finish/SKILL.md` text updated — the apply flow emits a single `task.applied` event, not the nonexistent `review.created` + `task.status_changed` pair.
- PS-2: `init` no longer pre-creates `.anvil/snapshots/`; the directory will be created on first use when `anvil snapshot` ships.

### Fixed (PR #47 critic deferrals)

- S2 / Greptile-G1 (already in 1.7.0): noted closed.
- S5: `template.DESCRIPTION_SHORT_THRESHOLD` is now public; CLI `plan --use-llm` help text references the constant rather than the literal "50".
- N1: comment in `parse_prd` clarifies that HTML-comment stripping runs before the LLM augmentation pass.
- N2: `parse_prd`'s reserved `prd_id` parameter now uses `# noqa: ARG001` instead of the `_ = prd_id` discard idiom.
- N3: `planning.llm._DEFAULT_MODEL` carries a "Last verified" date comment so future maintainers know when to refresh.
- N5: removed the unused `responses>=0.25` dev dependency (the test suite mocks the anthropic SDK at the `unittest.mock` level since `anthropic` uses `httpx`, not `requests`).

### Documentation

- `docs/evidence-buffer.md` (NEW) — format, lifecycle, orphan.json policy, sentinel interaction, cleanup. Closes CL-15.
- `docs/tech-debt-backlog.md` status markers updated: P6-1..P6-5 marked DONE (closed in PR #44); CL-7/CL-9/CL-14/CL-15/PS-2 DONE (this PR); TQ-5 DONE (PR #42 fixup).

### Tests

- 639 → 653 pytest tests (+14): 6 for CL-9 collection-only exclusion, 3 for CL-3 SchemaMismatch propagation, 1 for CL-8 double-submit guard + strengthened existing CL-8 test, 2 for CL-11 clock injection, 1 for CL-13 require-conn guard, 1 for PS-1 N+1 → 2 query collapse.
- 18 → 21 bash hook tests (+3 for CL-1 invocation surface).

---

## [1.7.0] — 2026-05-25

Phase 7: LLM augmentation. Adds an `LLMProvider` Protocol with an Anthropic-backed implementation (ephemeral prompt caching on the system block) and a `RecordedLLMProvider` test double, wires opt-in `--use-llm` flags into `plan`, `score`, and `expand`, and ships a brainstorm skill that bridges to `fakoli-flow:brainstorm`. The deterministic planning engine is unchanged — LLM enrichment is strictly additive and falls back cleanly on missing key, missing recording, or mid-operation failure.

### Added

- `bin/src/anvil/planning/llm.py` — `LLMProvider` Protocol + `AnthropicProvider` (with ephemeral prompt-caching on the system block per the claude-api skill guidance) + `RecordedLLMProvider` for deterministic tests + `LLMResponse` Pydantic model + `LLMProviderError`. Default model: `claude-sonnet-4-6`; API key sourced from `ANTHROPIC_API_KEY` env var.
- `--use-llm` flag on `anvil plan`, `score`, and `expand`. Off by default — opt-in augmentation that enriches deterministic output (score explanations, short task descriptions, sub-task proposals for complex tasks).
- `bin/src/anvil/planning/inference.py::expand_task` — new function returning `list[SubtaskProposal]`. Deterministic path returns `[]`; with provider + `complexity >= 4`, calls LLM to propose 2-5 sub-tasks. JSON-parse-tolerant; malformed responses fall back to `[]` with a warning.
- `plugins/anvil/skills/brainstorm/SKILL.md` — interview-style PRD authoring skill. Bridges to `fakoli-flow:brainstorm` when installed; standalone otherwise.
- `docs/llm.md` — provider config, prompt-caching usage, `RecordedLLMProvider` test pattern, failure modes.
- 46 new tests: 29 in `tests/test_llm.py` (provider unit tests), 17 in `tests/test_llm_integration.py` (engine integration via `RecordedLLMProvider`), plus 10 new CLI flag tests in `tests/test_cli.py`.

### Changed

- `planning.scoring.score_task` / `score_all` — new kw-only `provider: LLMProvider | None = None`. Default behavior unchanged.
- `planning.template.parse_prd` — new kw-only `provider: LLMProvider | None = None`. Default behavior unchanged.
- LLM failures during augmentation print a warning to stderr; the engine returns the deterministic-only result. LLM augmentation never aborts a planning operation.

### Technical notes

- One ephemeral cache breakpoint on the system block per Anthropic call. Repeated `score --use-llm` runs against the same task batch hit the cache and pay only for new user tokens.
- `RecordedLLMProvider` keys are a length-prefixed `sha256` over `(system, user)` — tests pre-compute via `RecordedLLMProvider.record_key(...)`. (v1.9.0 extended the key to include `max_tokens` and `temperature` — see the v1.9.0 entry above.)

Tests: 613 → 640 + Wave 3a additions (Wave 3a may add a few more — total to be confirmed at sentinel time).

---

## [1.6.0] — 2026-05-25

Phase 6: MCP server. Exposes 13 agent-facing tools via FastMCP (stdio), wires them into Claude Code via `.mcp.json`, adds the `progress.noted` audit event, and ships 50 MCP integration tests. Any agent in a project with anvil installed now has direct programmatic access to the full state engine without shelling out to the CLI.

### Added

- `bin/src/anvil/mcp_server.py` — FastMCP (stdio) server with 13 agent-facing tools. Read-only tools: `get_project_summary`, `list_tasks`, `get_task`, `get_next_task`, `generate_work_packet`, `check_conflicts`, `get_dependency_graph`. Mutating tools: `claim_task`, `release_task`, `renew_claim`, `submit_progress`, `submit_completion_evidence`, `update_task_status`. Stale-claim reaping runs at the top of `get_project_summary` and all six mutating tools. The server opens a fresh `SqliteBackend` per tool call (`Path.cwd() / .anvil`) — agents in different cwds see their own state, no leakage.
- `plugins/anvil/.mcp.json` — wires `anvil-mcp` as a stdio MCP server via `${CLAUDE_PLUGIN_ROOT}/bin/anvil-mcp`. Claude Code agents in any project with this plugin installed automatically see the 13 tools.
- `progress.noted` event action — audit-only, structurally parallel to `file_changed`. New `ProgressNotedPayload` in `state/payloads.py` and a no-op handler in `sqlite.py`. Emitted by `submit_progress`.
- `docs/mcp.md` — 645-line full tool reference covering each tool's signature, return shape, error cases, integration notes for fakoli-flow / fakoli-crew, and the documented error envelope contract.
- 50 new MCP integration tests in `tests/test_mcp.py` via the FastMCP in-process Client. 2 additional `progress.noted` payload tests in the existing payload test suite.

### Changed

- `bin/anvil-mcp` — wrapper now executes `python -m anvil.mcp_server` via `uv run` (fully functional). The Phase-6 "not yet implemented" guard block is removed.

### Technical notes

- Error envelope: tools raise `fastmcp.exceptions.ToolError(message)` with a human-readable string. The spec's structured `{code, message, target_id, payload}` envelope is deferred — the documented contract lives in `docs/mcp.md`.
- The process-per-request connection pattern keeps the server a thin shim. No shared in-process state, no connection pooling concerns across concurrent agent calls.

Tests: 530 → 580 (+50 MCP integration tests, +2 payload tests). Ruff clean.

---

## [1.5.0] — 2026-05-25

Phase 6 prep: backend / state-engine refactors that unblock the MCP server
(landing next in Phase 6 proper) by closing the five must-fix items from the
PR #41 critic and Greptile reviews tracked as P6-1..P6-5 in
`docs/tech-debt-backlog.md`.

### Added

- `bin/src/anvil/cli/` — new package replacing the 2,499-line `cli.py`
  monolith. Per-command modules: `init_status`, `prd`, `plan`, `claim`,
  `packet_apply`, `hooks`, plus `_helpers` for shared utilities and
  `__init__.py` as the Typer-app assembler. Public import path
  (`from anvil.cli import app`) is unchanged. (P6-4)
- `bin/src/anvil/state/payloads.py` — 17 per-action Pydantic v2 payload
  models (`ProjectCreatedPayload`, `PrdParsedPayload`,
  `EvidenceSubmittedPayload`, etc.) all using `ConfigDict(extra="forbid")`.
  `SqliteBackend._apply_mutation` now validates `event.payload_json` against
  the model for `event.action` once before dispatch, replacing the 17-elif
  chain with a `dict[str, (PayloadModel, handler)]` table. Handler signatures
  normalize to `(conn, payload: TypedPayload, event: Event)` — handlers read
  fields via attribute access rather than `payload.get(...)`. (P6-5)
- `Backend` Protocol gains three methods previously only on the SqliteBackend
  reach-through: `get_feature(feature_id)`, `list_events(target_id,
  target_kind, limit)`, `get_latest_evidence(task_id)`. The CLI no longer
  touches `backend._conn` directly; the three call sites in
  `cli/_helpers.py` (`_fetch_recent_events`, `_fetch_latest_evidence`)
  collapse into Protocol calls. (P6-1)
- `PENDING_EVENT_ID = "PENDING"` sentinel on `state.backend`. Callers
  construct events as `Event(id=PENDING_EVENT_ID, ...)` and the backend
  assigns the real `E000001`-format ID inside `apply_event`'s BEGIN IMMEDIATE
  transaction, closing the read-before-lock race that allowed event drops
  under concurrent claim/release. (P6-2)
- 37 new test cases in `tests/test_sqlite.py::TestPayloadValidation`
  covering each payload model's happy path and `extra="forbid"` rejection
  plus dispatch-level `ValidationError` propagation.
- 7 new test cases in `tests/test_sqlite.py::TestBackendProtocolExtensions`
  covering the three new Protocol methods.

### Changed

- All CLI commands and `claims.stale.detect_and_release_stale()` now emit
  events via `PENDING_EVENT_ID` instead of pre-allocating IDs through
  `backend.next_event_id()`. `next_event_id()` remains for backward
  compatibility but is documented as the legacy path.
- `SqliteBackend.apply_event` rewrites `event.id` in place when the sentinel
  is passed and returns the updated event so callers can recover the assigned
  ID without re-querying.

### Removed

- `bin/src/anvil/cli.py` — replaced by the package above. Imports
  resolve identically.
- `TaskStatus.stale` from `state.models` and the corresponding
  `task_to_stale` / `task_stale_to_ready` transitions. The state was
  structurally unreachable — only claims can be stale, and the task returns
  directly to `ready` when the claim is reaped. Task lifecycle ASCII diagram
  in `docs/specs/2026-05-24-anvil-v0.md` updated. CL-16 (claim.stale
  task transition skips the intermediate `stale` state) is resolved as a
  side-effect. (P6-3)
- `cli/_helpers.py::_fetch_recent_events` and `_fetch_latest_evidence` —
  callers now go through the Protocol methods.

### Migration notes

- External code calling `apply_event` should switch to passing
  `Event(id=PENDING_EVENT_ID, ...)` to get race-free ID assignment. Pre-built
  events with concrete IDs still work (the replay path requires this) but the
  pre-allocation path is racy under concurrency.
- Subclasses of `Backend` must implement the three new methods or accept the
  `NotImplementedError` from the Protocol default.
- The CLI external surface (`anvil <subcommand>`) is unchanged.

---

## [1.4.1] — 2026-05-25

Docs-only patch release. Stages the deferred items from the PR #41 critic +
Greptile reviews into a single backlog document so Phase 6 work picks them
up explicitly without re-reading chat transcripts.

### Added

- `docs/tech-debt-backlog.md` — 31 open items + 11 already-closed (for
  reference), grouped into: Phase 6 must-close (5), Cleanup (16), Test
  quality (8), Performance (2). Each entry cites its source (Greptile,
  Critic-1/2/3/4) and includes a concrete fix sketch.

---

## [1.4.0] — 2026-05-25

Phase 5: Context engine. Delivers the context engine, review apply gate, three new CLI commands, one new hook subcommand, two new skills, two new plugin-owned agents, a new PostToolUse hook, state engine extensions, and a comprehensive test suite. The plugin now supports the complete claim → packet → work → submit → apply lifecycle.

### Added

- Context engine (`context/packets.py`) — `render_packet()` produces both markdown (for `.anvil/packets/T001.md`) and JSON (for MCP `get_work_packet` in Phase 6). Pure function; no I/O.
- Review engine apply gate (`review/gates.py`) — `evidence_complete(task, evidence)` validates that submitted Evidence satisfies the task's `required_evidence` list; surfaces specific missing items.
- Three new CLI commands: `packet TASK_ID [--format md|json]`, `submit TASK_ID --commands ... --files-changed ... [--output-file --pr-url --commit-sha --known-limitations --actor]`, `apply TASK_ID [--approve | --reject] [--reason --reviewer]`.
- One new hook subcommand: `anvil hook capture-evidence --command --exit-code --stdout-file --stderr-file --actor` — used by the new PostToolUse Bash hook.
- Two new skills: `skills/execute/SKILL.md` (full claim → packet → work → submit loop; coordinates with `fakoli-flow:execute` when installed) and `skills/finish/SKILL.md` (apply + ship decision: merge/PR/keep/discard).
- Two new plugin-owned agents: `agents/critic.md` (code reviewer; defers to `fakoli-crew:critic`) and `agents/sentinel.md` (evidence validator; defers to `fakoli-crew:sentinel`). Both `allowed-tools` exclude Edit/Write (Iron Rule at tool-permission level).
- New PostToolUse hook: `hooks/capture-evidence.sh` (Bash matcher) — captures stdout/stderr/exit-code of verification commands (`pytest`, `ruff`, `mypy`, `npm test`, `cargo test`, `bun test`) into `.anvil/.evidence-buffer/` per-claim JSON files for later attachment to Evidence.
- State engine: 2 new event handlers (`evidence.submitted`, `task.applied`) both routed via `_apply_mutation`. `evidence.submitted` atomically inserts Evidence + transitions task to `needs_review` + auto-releases the active claim. `task.applied` combines `needs_review` → `accepted` → `done` in one transaction when `decision='accepted'`.
- 81 new tests (403 → 484): `test_context.py` (24 tests), `test_review.py` (20), `test_sqlite.py` extensions (16 new Phase 5 handler tests + the audit replay test for `evidence` + `applied`), `test_cli.py` extensions (17 new), `test_hooks.sh` extensions (5 new capture-evidence smoke tests).
- Coverage: context 93%, review 97%, state 95.70%, claims 99%, overall 91.16%.
- Audit guarantee extended: `TestReplayIncludesPhase5Events` byte-compares `sqlite3 .dump` after replaying the full lifecycle including `evidence.submitted` and `task.applied` (both accepted and rejected branches).

### Fixed

- Dead-code unreachable branch in `_handle_evidence_submitted` — `if commands_run is None` was never reachable because the field defaulted to `[]`. Fixed to `if not commands_run` which catches both None and empty (submitting evidence with no verification commands is meaningless).

---

## [1.3.0] — 2026-05-24

Phase 4: Claims manager. Delivers atomic claim/release/renew/next semantics with lease and heartbeat enforcement, git branch auto-creation, two new bash hooks, a claim skill, and a comprehensive test suite. The plugin now supports the complete claim-based coordination workflow for AI agents working in parallel.

### Added

- Claims manager (`claims/manager.py` — atomic claim/release/renew with lease and heartbeat semantics; Clock-injected for deterministic tests).
- Stale claim detector (`claims/stale.py` — runs on every CLI invocation; returns expired claims back to the ready pool with audit trail).
- Four new CLI commands: `claim TASK_ID [--worktree] [--force] [--actor]`, `release CLAIM_ID [--force] [--reason]`, `renew CLAIM_ID`, `next [--actor]`.
- Hook sub-app: `anvil hook check-claim` and `anvil hook record-file-change` (used by the new bash hooks).
- Git ops module: `git_ops/branch.py` auto-creates `agent/<task>-<slug>` branches on claim (with name-collision suffix, graceful no-op when git absent); `git_ops/worktree.py` for optional `--worktree` parallel-checkout.
- Two new hooks: `check-claim.sh` (PreToolUse on Edit|Write|NotebookEdit; warns when active claims exist) and `record-file-change.sh` (PostToolUse; appends file_changed events to the audit log).
- New skill: `skills/claim/SKILL.md` — workflow choreography for the claim → work → renew → release loop.
- State engine: 4 new event handlers (`claim.created`, `claim.released`, `claim.renewed`, `claim.stale`) all routed through `_apply_mutation` dispatch.
- 98 new tests (300 → 398): `test_claims.py` (concurrency-critical, `claims/` coverage 99%), `test_git_ops.py` (real git per test), `test_hooks.sh` (11 bash smoke tests), extended `test_sqlite.py` and `test_cli.py`.
- Audit guarantee extended: `TestReplayIncludesPhase4ClaimActions` byte-compares `sqlite3 .dump` after replaying `claim.created` → `claim.renewed` → `claim.released`; companion `test_replay_includes_claim_stale` covers the stale path.

### Fixed

- `claims/stale.py` event payload was missing the required `reason` field expected by `_handle_claim_stale` (caught by Wave 3 tests).
- `_handle_claim_released` was incorrectly requiring `release_reason` — payload field is optional and the ClaimManager legitimately passes None when no reason is given.

### Notes

- Stale claim reaping is automatic on every mutating CLI command (`claim`, `release`, `renew`, `next`); users don't need to think about it.
- Claims survive without git: when git is absent or cwd is not a git repo, the claim succeeds without a branch and prints a warning (record-only mode).

---

## [1.2.0] — 2026-05-24

Phase 3: Planning engine. Delivers the full planning runtime — deterministic PRD parser, six-dimension scoring engine, dependency and conflict-group inference, eight new CLI subcommands, two new skills, a new agent, and a PRD template doc. The plugin now supports the complete PRD-to-ready-tasks workflow without LLM augmentation.

### Added

- Planning engine: deterministic template parser (`planning/template.py` — turns structured markdown into Pydantic Requirements/Features/Tasks; full quick-start example documented at `docs/prd-template.md`).
- Six-dimension scoring engine (`planning/scoring.py` — rule-based heuristics for complexity, parallelizability, context_load, blast_radius, review_risk, agent_suitability; explanation string per task).
- Dependency and conflict-group inference (`planning/inference.py` — subset-overlap heuristic for dependencies, partial-overlap detection for conflict groups).
- Eight new CLI subcommands: `prd parse`, `prd review [--approve]`, `plan`, `score [TASK_ID]`, `expand TASK_ID` (Phase 7 scaffold), `review tasks`, `list [--status STATUS --feature F]`, `show TASK_ID`.
- Two new skills: `skills/prd/` (PRD authoring/review workflow) and `skills/plan/` (PRD → ready tasks workflow), both following the state-ops imperative-voice and scannable-description conventions.
- New agent: `agents/planner.md` (PRD-to-tasks specialist; defers to `fakoli-crew:guido` when fakoli-crew is installed; allowed-tools excludes Edit/Write to enforce the "propose, don't mutate" Iron Rule at the tool-permission level).
- PRD template doc (`docs/prd-template.md` — ~2,500 words; quick-start JSON-to-YAML converter example demonstrates every documented field).
- SQLite event router extended with 8 new actions: `prd.parsed`, `prd.reviewed`, `prd.approved`, `feature.created`, `task.created`, `task.scored`, `task.expanded`, `task.status_changed`; all routed via `_apply_mutation` dispatch; replay-from-empty handles all 8.

### Fixed

- `_insert_task_row` switched from `INSERT OR REPLACE` to `INSERT ... ON CONFLICT DO UPDATE` to preserve task row identity across `plan` re-runs. `INSERT OR REPLACE` is DELETE+INSERT, which trips `ON DELETE RESTRICT` on `claims.task_id` and `evidence.task_id` once work has begun. Regression test: `test_plan_is_idempotent`.

### Tests

- 122 new tests (174 → 296). `state/` coverage 95.05% (audit-critical), `planning/` ~93%, `cli` ~88%, overall 92.72%.
- Audit guarantee extended: `test_replay_includes_new_event_actions` byte-compares `sqlite3 .dump` before/after replaying a mixed sequence of all 8 new event actions.

---

## [1.1.0] — 2026-05-24

Phase 2: State engine. Delivers the full runtime core — data models, state machine, SQLite backend, event log, CLI, skill, hook, and test suite. The plugin is now operationally useful for tracking project state.

### Added

- State engine: Pydantic v2 models (14 entities) in `state/models.py` — `Project`, `Requirement`, `Feature`, `Task`, `Claim`, `Evidence`, `FileChange`, `Snapshot`, `TaskScore`, `SnapshotEntry`, `Config`, and supporting enums (`TaskStatus`, `ClaimStatus`, `EvidenceKind`).
- Pure state machine transitions in `state/transitions.py` — 17 transition functions plus `TransitionError` and gate helper predicates; no I/O, fully deterministic.
- Backend Protocol + concrete `SqliteBackend` in `state/sqlite.py` — WAL journal mode, JSONL event log (`events.jsonl`) written atomically on every mutation, full replay guarantee.
- DDL schema generator (`state/schema.py`) — foreign keys, composite indexes, schema versioning table; generates idempotent `CREATE TABLE IF NOT EXISTS` SQL.
- Clock Protocol with `SystemClock` and `FrozenClock` for deterministic tests — injected via `SqliteBackend(clock=...)`.
- Config loader (`config.py`) — reads `config.yaml` from the `.anvil/` directory; Pydantic-validated; falls back to sensible defaults.
- PEP 561 `py.typed` marker — `anvil` is now a typed package.
- CLI subcommand `init` — scaffolds `.anvil/` directory in the caller's project: `config.yaml`, `state.db`, `events.jsonl`, `prd.md`, `packets/`, and `snapshots/`. Fixed a wrapper bug (`--project "$BIN_DIR"` → wrapper now passes `--project` to preserve the caller's working directory so `init` scaffolds in the correct location).
- CLI subcommand `status` — human-readable summary of project state; `--hook-format` flag emits compact key=value pairs for hook consumption.
- First skill: `state-ops` — covers common state inspection and manipulation workflows from within Claude Code.
- `SessionStart` hook `detect-state.sh` — detects `.anvil/state.db` in the project root on session start and surfaces a brief status banner to the agent.
- 173 tests covering `state/models.py`, `state/transitions.py`, `state/sqlite.py`, CLI (`init`, `status`, `--version`), `config.py`, and the `detect-state.sh` hook; 94% overall coverage, 95% on `state/`.
- Audit-guarantee test `test_replay_from_empty_reconstructs_state_exactly` — replays `events.jsonl` from scratch against an empty database and asserts byte-for-byte equality with the live `state.db`.

---

## [1.0.0] — 2026-05-24

Phase 1: Plugin scaffold. No executable state operations ship in this release — this entry records the structural foundation that all subsequent phases build on. Version 1.0.0 follows the fakoli-plugins repository convention that new plugins ship at 1.0.0 regardless of feature completeness (per `CLAUDE.md` § New Plugin Checklist).

### Added

- `.claude-plugin/plugin.json` — plugin manifest declaring name, version (`1.0.0`), description, author, repository, license, and marketplace keywords.
- `README.md` — positions anvil against CCPM and issue-tracker-as-state patterns; documents the "5 must-do-better" list; install instructions (git clone until marketplace publication); Quick Start teaser for the intended `anvil init` flow; architecture overview; 8-phase build status table; integration notes for fakoli-flow and fakoli-crew.
- `CHANGELOG.md` — this file; Keep a Changelog format.
- `LICENSE` — MIT license, copyright 2026 Sekou Doumbouya.
- `docs/specs/2026-05-24-anvil-v0.md` — canonical build specification: data model, CLI command set, MCP tool surface, hook event mappings, phasing plan, and integration contracts.
- `bin/anvil` — bash wrapper that invokes `uv run python -m anvil.cli`; `--version` stub returns `1.0.0`.
- `bin/anvil-mcp` — bash wrapper that invokes `uv run python -m anvil.mcp_server`; stubbed pending Phase 6 with a clean error message instead of a raw Python traceback.
- `bin/pyproject.toml` — uv-managed Python project (Hatchling build backend); declares dependencies: Typer, Pydantic v2, FastMCP, and test tooling (pytest, ruff, mypy, responses).
- `bin/uv.lock` — locked dependency tree for reproducible installs.
- `bin/src/anvil/__init__.py` — package init; exports `__version__ = "1.0.0"`.
- `bin/src/anvil/cli.py` — Typer application; single `--version` flag functional; all other subcommands stubbed with `typer.echo("Not yet implemented")`.
- Skeleton directories establishing the plugin layout: `skills/`, `agents/`, `hooks/`, `tests/`, `docs/`.
