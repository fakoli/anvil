# anvil design rationale

> Why [anvil](https://github.com/fakoli/anvil) is shaped the way it is. Companion to `architecture.md` (what is built). For positioning soundbites see `_positioning.md`; for what is planned next see `roadmap.md`.

This document answers "why was it built this way." Each section names the choice, the alternatives rejected, the trade-off accepted, and where to challenge the decision. `_positioning.md` keeps the positioning summary; this file keeps the engineering rationale.

No section exceeds 200 words. Read the section you care about; the rest will still be here.

---

## Mental model: Terraform for agentic work

anvil is to agentic software work what Terraform is to infrastructure: a canonical state file holds the truth, derived views project from it, and the plan-then-apply rhythm gates execution behind review. The analogy drove four shape decisions; naming things in those terms makes the system legible to anyone who has used Terraform.

| Terraform concept | anvil equivalent | Where it lives |
|---|---|---|
| `.tf` configuration | `prd.md` + parsed `Requirement`/`Feature`/`Task` rows | `.anvil/prd.md`, rows in `state.db` |
| `terraform.tfstate` | `state.db` (Pydantic-validated SQLite) | `.anvil/state.db` |
| `terraform plan` | `anvil packet T012` (work-packet preview before claim) | derived view, not stored |
| `terraform apply` | `anvil apply T012` (reviewed transition to `done`) | `Review` row + `apply.*` event |
| State locking | `Claim` row with `lease_expires_at` + heartbeat | `claims` table, `BEGIN IMMEDIATE` txn |
| Drift detection | stale-claim sweep + `sync --fix` reconciliation | `claims/stale.py`, `sync.reconcile` |
| Workspace | `.anvil/` per repo | created by `anvil init` |
| Backend protocol | `Backend` Protocol (SQLite ships) | `state/backend.py` |

### What the analogy gets right

- **Canonical state separate from derived views.** Work packets, markdown plans, dependency graphs are *projected* from the DB on demand, never stored as the source of truth. The same way Terraform regenerates `terraform plan` output from the state file each time.
- **Plan-before-apply as a hard rhythm.** Agents cannot mark `done` without a `Review` row, the same way Terraform cannot apply without a plan diff.
- **Drift is detected and reported, not papered over.** Stale claims, orphan branches, sync conflicts surface as explicit `conflicts` or `sync --fix` flows, not silent reconciliation.

### Where the analogy stops

- **No resource ownership.** Terraform owns the resources it manages; anvil does not own source code. Agents and humans edit files; anvil records *that* they edited and *what evidence* they produced.
- **No `destroy` verb.** No resource graph to tear down: only `release --force` for stuck claims, and `apply --reject` to send a task back to drafted.
- **No `import` flow.** Existing repos don't have a "state to discover." `init` creates an empty workspace; the PRD authoring flow populates it.

---

## Why SQLite + WAL

**Choice:** one SQLite database per project at `.anvil/state.db`, opened in WAL mode with `BEGIN IMMEDIATE` for mutating transactions. Append-only JSONL event log alongside (`events.jsonl`) as the replay source of truth.

### Rejected alternatives

- **Postgres / hosted DB.** Requires a server, credentials, and network access. It changes the "clone the repo and `anvil init`" workflow. The local-first model is the product boundary; a hosted DB is a different product.
- **Redis / in-memory.** Loses durability across CLI invocations (each is a short-lived process). Loses the audit trail. Forces a sidecar daemon, which is explicitly a non-goal.
- **File-only (JSON / YAML).** The v0 brief's first instinct. Rejected because: (a) cross-process atomic writes on plain JSON race on macOS NFS and Windows, (b) claim leases need `BEGIN IMMEDIATE` semantics that JSON cannot provide, (c) querying becomes O(n) over the entire file on every CLI call.

### Trade-offs

- **Accepted:** SQLite is single-writer. Two concurrent claims serialize at the DB level. Fine: claims are a coordination primitive, not a throughput primitive. SQLite handles ~10k writes/sec; we will hit other walls first.
- **Accepted:** schema migrations are our problem, not the user's. We ship a `anvil migrate` command and version the schema explicitly (`docs/migrations.md`).
- **Lost:** network multi-writer. Two laptops cannot share a `state.db` over a shared drive. That use case is served by sync providers, not by sharing the DB.

### Why WAL specifically

Default SQLite journaling mode (`DELETE`) holds an exclusive lock during writes, blocking all readers. WAL mode lets readers proceed concurrently with a single writer. For our workload (many `anvil status` reads from hooks, occasional `anvil claim` writes from agents) WAL is the right pick. We pay a `wal` + `wal-shm` sidecar file cost in `.anvil/`; both are git-ignored.

---

## Why local-first

**Choice:** state lives under `.anvil/` inside the user's repository. No hosted backend, no account, no telemetry, no network call unless the user opts into a sync provider.

### Rejected alternative: SaaS-first

A hosted control plane would let us ship a web dashboard, real-time collaboration, and a single sign-up funnel. Rejected because:

1. **It changes the product category.** "Backend-neutral local-first state" is what distinguishes Anvil from CCPM-on-GitHub-Issues, Hamster Studio, and Jira/Rovo (see `competitive_gap_analysis_agentic_project_state.md` § "Strategic Positioning"). A SaaS control plane would compete in the task-management category instead.
2. **Data ownership.** Users running PRDs through an LLM already worry about leakage; making the project plan itself leave the repo doubles that surface.
3. **Offline-first is inherent.** Plane mode, airgapped networks, and unreliable Wi-Fi do not affect the core workflow. The system has no required online mode.
4. **No auth flow.** `anvil init` is the entire onboarding.

### Trade-offs

- **Accepted:** cross-machine collaboration goes through sync providers (a projection into GitHub Issues / Linear / Jira), not shared state.db. Slower and lossier than a CRDT, and that audience is buying Linear, not anvil.
- **Accepted:** if `.anvil/` is git-ignored (sometimes recommended for `state.db` to avoid binary merge conflicts), the canonical state does not survive a `git clone` on a second machine. `events.jsonl` *can* be committed; `replay` rebuilds the DB. The user chooses the trade-off per repo.
- **Lost:** hosted dashboard and cross-project search. Those are outside the local-first scope.

---

## Why claims with leases

**Choice:** a `Claim` row is created on `anvil claim T012`, with `claimed_by`, `lease_expires_at`, `last_heartbeat_at`, `expected_files`, and an optional branch/worktree binding. Heartbeats via `renew T012` every 5 min; stale leases detected and released on every CLI/MCP op.

### Rejected alternatives

- **Assignment by label / issue assignee.** This is how CCPM does it (see competitive gap doc § 1). Works for one human at a time. Fails for two concurrent Claude Code sessions: labels have no expiry, no heartbeat, no `expected_files`.
- **Branch-name convention (`agent/t012-foo`).** Better than labels but still inadequate: branches do not expire, do not heartbeat, and an agent that forgets to push leaves no claim trace. We *use* `agent/<task>-<slug>` branches as a projection of the canonical claim, not as the claim itself.
- **Git-lfs-style file lock.** Wrong granularity. A task often touches files it could not predict. Locking files would block legitimate work. We warn (not block) on `expected_files` overlap.

### What this buys

The "first-class claim/lock/lease model" differentiator from `agentic_project_state_design_brief.md` Gap 2: lease expiry releases abandoned claims; heartbeats distinguish active from stale work; `expected_files` enables pre-claim conflict warnings; cross-runtime safety comes from storing the claim in SQLite rather than in one agent session.

### Trade-off accepted

Heartbeat discipline is on the agent. An agent that never calls `renew` will see its claim go stale at the lease deadline. We deliberately did not automate this via a daemon (see "Daemon" below).

### Why lease + heartbeat, not lease alone

A 1-hour lease without heartbeat means stale claims wait the full hour to release. A 5-minute lease without heartbeat means long-running work can be released mid-task. Lease + heartbeat lets Anvil use a configurable lease while active work renews and keeps moving. The combination gives fast stale detection without releasing active claims.

---

## Why evidence is required

**Choice:** `anvil submit T012` requires a structured `Evidence` payload (see `state/models.py`) with `commands_run`, `files_changed`, `output_excerpt`, exit codes, and optional artifacts. The `sentinel` agent validates it before a task can move to `accepted`. Free-form "tests passed" strings are rejected.

### Rejected alternatives

- **Trust-based "done."** Agent says "I'm done"; task transitions to `done`. Chat-driven workflows often rely on this pattern, which allows models to declare completion without running or recording verification. See competitive gap doc Gap 6 for the full argument.
- **Opaque `evidence: string` field.** Unsearchable, unparseable, trivially gameable ("evidence: looks good to me"). No downstream tooling can act on it.

### What "structured" means

The `Evidence` Pydantic model requires:

- `commands_run: list[str]`: every shell command actually executed during work
- `files_changed: list[str]`: paths touched, cross-checked against `record-file-change.sh` events
- `output_excerpt: str`: last N lines of test/build output, captured by `capture-evidence.sh`
- `exit_codes: dict[str, int]`: per-command exit
- `artifacts: list[Artifact]`: optional screenshots, logs, links

### Why hooks capture, not the agent

`capture-evidence.sh` runs as a PostToolUse hook on `Bash` and records every command the agent actually executed (with stdout/stderr/exit). The agent's job is to *cite* what to include in the submission; the hook supplies the ground truth. An agent that fabricates `commands_run: ["pytest"]` without having actually run pytest gets caught because the hook stream does not show a pytest invocation in the claim's window. The split is deliberate: agent-supplied evidence is auditable against system-captured evidence.

### Trade-off accepted

Submitting evidence is more work than typing "done." That extra step is intentional: it requires the agent to cite commands and files that can be checked against captured hook output. "Quick fix" workflows get `apply --skip-evidence` as an explicit, logged override.

---

## Why MCP + CLI both

**Choice:** every state operation has two front doors. A Typer CLI for humans and shell scripts (`anvil claim T012`), and a FastMCP stdio server exposing 24 tools for agents (`claim_task(task_id="T012", actor="claude-session-abc")`). Both delegate to the same `state/` engine; neither owns workflow logic.

### Rejected alternatives

- **CLI only.** Agents would have to shell out and parse stdout. Some can (Claude Code, with Bash); some cannot (Cursor, with no shell). Shell-out loses structured errors.
- **MCP only.** Humans, shell scripts, and hooks still need a command-line surface. `anvil status` in a terminal during debugging is faster than starting an MCP client. Hooks are sh, not Python, so they shell out to the CLI.
- **REST/HTTP server.** A long-running process. Daemon problems (see below). Authentication. Port collisions. We get the agent-tool benefits via MCP stdio without any of that.

### The principle

From `_positioning.md` § MCP vs plugin: **MCP exposes capabilities; the plugin layer encodes operating discipline.** The MCP tool `claim_task` does not decide *when* to claim, *which* specialist should execute, or *what* evidence is required; those decisions live in skills (`execute/SKILL.md`), agents (`sentinel.md`), and hooks (`check-claim.sh`).

### Trade-off accepted

Two front doors means two surfaces to keep in sync. We mitigate by sharing the engine: both surfaces construct the same `Backend`, call the same `ClaimManager.claim()`, surface the same exceptions. If the engine layer is correct, both surfaces are correct.

### Who calls which

- **Hooks** call the CLI (sh scripts can't speak MCP).
- **Humans** call the CLI (faster than spinning up an MCP client).
- **Skills** call the CLI (they are markdown choreography invoking shell).
- **Agents inside MCP-capable runtimes** call MCP (typed responses, structured errors).
- **Agents in shell-only runtimes** fall back to CLI via Bash tool.

Both paths are first-class. Neither is the "main" API.

---

## Why six-dimension scoring

**Choice:** every task carries six 1-5 scores: `complexity`, `parallelizability`, `context_load`, `blast_radius`, `review_risk`, `agent_suitability`. `complexity ≥ 4` triggers an expand recommendation. `agent_suitability` drives orchestration routing.

### Rejected alternatives

- **Single-axis story points / t-shirt sizes.** Story points conflate "hard to think about" with "lots of files to touch" with "scary to ship." The conflation hides the actually interesting signal: a task can be low-complexity but high-blast-radius (renaming a public API), or high-complexity but low-blast-radius (a tricky algorithm in one file). Routing needs both axes; story points give one.
- **Only complexity.** Taskmaster does this (per design brief). Useful for "should I expand?" but not enough for "which agent should take this?" or "can this run in parallel with T015?". Parallelizability supports multi-agent dispatch; agent_suitability supports model-tier routing.

### Why these six (from design brief § 3)

- `complexity`: gates expand recommendations.
- `parallelizability`: gates multi-agent dispatch.
- `context_load`: predicts whether a small-context agent can hold the task.
- `blast_radius`: predicts review risk and conflict probability.
- `review_risk`: escalates to human review.
- `agent_suitability`: routes between Opus / Sonnet / Haiku / local models.

### Trade-off accepted

Six dimensions is more cognitive load than one. We mitigated by making LLM scoring the default (`score --use-llm`) so humans rarely score by hand; the template-based fallback gives reasonable defaults from heuristics on the task description. Lost: comparability with existing story-point velocity charts. The audience is not running sprint retros.

---

## Why hooks are non-blocking

**Choice:** all bundled, auto-wired hooks (`detect-state.sh`, `check-claim.sh`, `record-file-change.sh`, `capture-evidence.sh`, and the B41 `heartbeat.sh`) `exit 0` regardless of internal failure, do not use `set -e`/`set -u`/`set -o pipefail`, wrap CLI calls with `|| true`, and must complete in <200ms on hot events (PreToolUse / PostToolUse).

**One opt-in exception (B41):** `anvil hook stop-gate` is a *blocking* Stop-hook evidence gate (the Codex/Claude analogue of the OpenClaw finish-gate). It is **deliberately NOT auto-wired**: it ships as a verb + a documented opt-in recipe, consistent with the blocking-gates-are-opt-in stance (the OpenClaw `--finish-gate` works the same way). Blocking is reserved for surfaces the user explicitly enables and verifies; the *default* hook posture stays non-blocking.

### Rejected alternative: blocking PreToolUse on claim violations

A blocking hook would refuse the Edit tool call when an agent tries to write a file outside its claimed scope. Rejected because:

1. **Agents can bypass editor hooks.** A model that hits a blocking hook can still edit through shell commands, reducing hook coverage.
2. **False positives interrupt valid work.** A task can legitimately need to touch a file it did not predict. A blocking hook turns an advisory warning into a session interruption.
3. **Hooks run in the user's shell, not a sandbox.** A `set -e` script that hits an unexpected condition can stop PreToolUse for all matching tools, not just the attempted edit.

### The right shape

Warn + log + audit trail. The check-claim hook prints a one-line warning to stderr ("warning: editing src/foo.py outside active claim T012 scope") and appends an event to `events.jsonl`. The human or downstream sentinel decides whether the warning matters.

### What gets enforced anyway

Non-blocking does not mean unenforced:

- The `apply` gate is a hard gate. No `Evidence` → no transition to `accepted`. No `Review` → no transition to `done`. The hooks observe; the apply gate enforces.
- The `claim_task` MCP tool refuses if the PRD is still `draft` or another active claim already holds the task. That refusal is at the engine layer, not in a hook, and the engine layer *does* block.
- Schema validation in Pydantic models refuses malformed input at every boundary.

The discipline is layered: hooks observe and warn, the engine enforces invariants, the apply gate is the final hard checkpoint.

### Trade-off accepted

Hooks observe and report; the engine enforces state transitions. An agent that ignores warnings and submits evidence anyway is checked at `apply` time by the sentinel, which cross-references `files_changed` against the warning stream. Performance remains a constraint; see `roadmap.md` Theme 3 for the next hot-path pass on the 200ms budget.

---

## Why risk-axis eligibility now, matching later (fleet mode)

> Added 2026-06-20, revised the same day after a code-grounded red-team. Direction,
> not yet shipped. Tracked as epic **E13**; positioning in `_positioning.md`
> § "The fleet thesis". An earlier draft specced full capability-matching as the
> build; the review cut it to an MVP.

**Choice:** multi-runtime execution is *pull*, not a router. Runtimes poll, self-select, claim under a lease, submit evidence. anvil owns *eligibility*; the harness owns *assignment*; anvil never names a model. The MVP eligibility test is the **risk axis only**: `anvil next --max-blast --max-review-risk`, reusing the shipping `fast_lane` ceilings, plus autonomous loops keyed by `--actor`. The eligibility test must be **safe by construction from the first commit (B45)**: a task scored only by a filename regex (untrusted) defaults to **frontier-only**, so a low score never *lowers* the eligibility bar; it can only raise it once a trusted risk label is confirmed. That is the whole first build; let *measured* mis-routing pull richer machinery into existence.

### Why packets, not bigger models, carry local work

A fast local model (measured 200+ tok/s on an RTX 5090) lacks the frontier worldview, so it cannot explore an underspecified task well. A tight, fully-specified anvil **packet** (intent, acceptance criteria, scope, non-goals, exact context) is the worldview substitute: it lets the local model skip exploration and execute a bounded task directly. Packet quality is what turns "fast but dumb" into "fast and sufficient," and tighter packets cut steps, which cuts tokens, which buys back flat-rate capacity. Packet quality is therefore a **first-class, measured workstream**, not a side effect: the bake-off measures packet sufficiency alongside throttle frequency.

### Deliberately deferred (cut from the MVP)

- **Capacity-pools as a first-class concept** is deferred. Before anvil models pools, the two-loop bake-off measures how often each flat-rate pool throttles and whether naive spillover to the local box already suffices. Build the pool concept only if measured throttling shows naive spillover is not enough.
- **`type` matching:** `TaskType` is a 4-value advisory enum where feature≈refactor; the real work-kind taxonomy is an unbuilt prerequisite.
- **`tier` axis:** no field exists, and "minimum-sufficient model" is a model-capability judgment anvil must not make. If it returns, it is a pure function of model-neutral dimensions with the runner owning `tier→model`.
- **profile schema / `--profile` / shared backend / many-runners:** machinery over a loop that already drains the queue. On one box the "market" has two participants, a lookup table, not a market.

### Rejected alternatives

- **Central score-driven router (push).** Puts model knowledge inside anvil and centralizes a call the environment makes better. Pull lets each runtime self-select.
- **Specialist agents baked into the producer** (the [fakoli-crew](https://github.com/fakoli/fakoli-plugins) tax). The producer *describes* the work; the environment maps it to its own agents.

### Prerequisites before unattended autonomy (red-team, in shipped code)

- **Progress-gated heartbeat + max-claim-age:** `renew()` is unconditional, so a wedged agent heartbeats forever and never reaps.
- **One `ANVIL_ACTOR` resolver:** claim (`$USER`), heartbeat (`$ANVIL_GATE_ACTOR`), and hooks (`session_id`) resolve the actor three ways, so heartbeat and the finish-gate fail silently *open*.
- **`strict_evidence` on + a portable, signed proof artifact:** the gate is advisory by default and `Evidence` has no `exit_code` field. The target is a typed proof artifact that is **portable, signed, and bound to task identity plus claim/lease and pull**, not an internal-only blob. Frame this honestly: verification is a *contested* wedge, not a moat anvil holds. Portable proof formats (AGEF, Proof of Insight) and enforced evidence-gates already exist separately, and platforms are absorbing verification; the unoccupied position is the *fusion* of portable signed proof with task/claim/lease/pull, local-first. Learn from AGEF / Proof of Insight; treat it as a bet to execute, not a won position.

### Trade-offs

- **Safe by construction, not bolted on (B45):** risk dimensions are eligibility *ceilings*, but they fire on a filename regex (untrusted): a dangerous change in an innocuously-named file scores low. From the first commit, regex-only-scored tasks default to **frontier-only**; a confirmed risk label is what *relaxes* the bar, never the absence of one.
- **Concede durable-execution, keep hardening leases (weak-pillar strategy):** anvil does not try to out-engineer Temporal / LangGraph Platform on durable execution. It interoperates and stays local-first SQLite there, while it keeps hardening the lease/claim single-winner semantics, the cheap-to-keep-ahead core. Be honest: anvil is behind on durable state and matched-or-behind on leases today.
- **Measured, not assumed:** local auto-apply is gated to `needs_review` until local false-pass is measured; the "local is cheaper" case is unproven inside maxed flat-rate subscriptions and must be measured (rate-limit-hit frequency) before it is claimed. The driver is *capacity*, not per-token cost: drain multiple flat-rate pools in parallel and route overflow to the zero-marginal local box, priced against per-token API, not against the flat plan.
- **Lost (until later):** cross-host pull needs a *self-hosted* backend; single-host SQLite stays the default. When replay verifies state in CI it checks **logical equivalence** (canonical row-ordered dump or per-table content hash), never byte-identical SQLite.

---

## Deferred decisions

Each item below was considered, has a sketch, and was explicitly *not* shipped in v0/v1. Reasoning here; tracking and target version in `roadmap.md`.

### Webhook-based sync (deferred to v2.0, SPEC-FIRST)

Polling-only in v0. Sync providers (`github_issues` today, more in v2.0) call `gh api` on a `--watch` loop every N seconds.

**Why deferred:** webhooks require a public HTTP endpoint, HMAC verification, out-of-order event de-duplication, and an at-most-once delivery contract. That complexity is not needed at the current single-user-laptop scale. The cost/benefit changes if sync runs on a server with public DNS or if sub-minute updates become important.

Tracked as P9B-5 (`roadmap.md` § v2.0, SPEC-FIRST). A spec doc must precede implementation because the engine's current "one fetch round-trip per pass" assumption does not hold under webhooks.

### Multi-backend abstraction beyond SQLite (deferred indefinitely)

A `Backend` Protocol exists in `state/backend.py`, but only `SqliteBackend` ships.

**Why deferred:** abstractions calcify against their only implementation. Until a second real backend forces us to find the seams (Postgres for a team deployment, JSON-file for a constrained-runtime use case), the Protocol is a placeholder. We keep it as a refactoring affordance, not a product promise.

The right time to add the second backend is when a real user has a concrete requirement. If that day comes, the Protocol gives us a refactoring target; until then, one well-tested SQLite implementation is preferable to multiple lightly tested implementations.

### Multi-provider LLM beyond Anthropic (deferred indefinitely)

`LLMProvider` Protocol exists; only `AnthropicProvider` ships.

**Why deferred:** the LLM is used for *augmentation* (PRD parsing, scoring, expansion), never for state mutation. A user without an API key gets the template-based deterministic fallback and loses no correctness, only convenience.

Adding OpenAI or local-LLM providers is a 1-day task when a user asks; shipping all three preemptively means three test matrices and three error-handling code paths for zero current benefit. Same logic as the backend Protocol: the seam exists; the second impl waits for a forcing function.

### Daemon / long-running service (not planned)

The MCP server is the only long-running process and only lives for the duration of an agent session.

**Why not:** a background daemon would solve real problems (heartbeats without explicit `renew` calls, push notifications for sync, scheduled snapshots) at the cost of becoming an operational liability. `launchd` / `systemd` integration, PID files, log rotation, "is it running?" debugging.

The user's mental model becomes "two things to manage" instead of "one CLI." We accept the trade: agents must heartbeat, sync is polled, snapshots are manual. The MCP server is acceptable because it dies with the agent that spawned it; the user never sees it.

### Hosted SaaS / web dashboard (out of scope)

See "Why local-first" above.

**Why not:** going SaaS is choosing a different product. A hosted dashboard would compete with Linear, Jira, and Asana in the task-management category.

Anvil's direction is durable state that survives session resets, lives in the repo, and does not require an account. If a dashboard ships later, it should be a downstream viewer of the same `.anvil/` directory.

### Real-time collaborative editing (out of scope)

Two humans editing the PRD simultaneously, CRDT-style.

**Why not:** the use case is rare (PRDs are written by one author, reviewed by others) and the engineering cost is enormous (Yjs / Automerge / operational transforms, conflict UI, presence indicators).

Solved by git: branch, edit, PR, merge. anvil inherits git's collaboration model for the PRD; the SQLite state.db is regenerable from `events.jsonl` so merge conflicts on the DB itself are recoverable by replay.

### Bundled single static binary (uv trade-off)

anvil ships Python source in `bin/src/`; `uv` resolves and caches dependencies on first invocation via `bin/anvil` shell wrapper.

**Why not bundled:** PyInstaller / Nuitka / shiv binaries are 50-200 MB each, must be built per-platform (macOS x86_64, macOS arm64, Linux x86_64, Linux arm64, Windows), require signing on macOS, and break the "I want to read what it does" debuggability that source distribution gives.

**Trade-off accepted:** first-invocation latency is ~3-5 seconds while uv resolves the lock file; every subsequent invocation is <200ms warm. We documented this in the install README; users have not complained. `uv tool install` is the path of least resistance if they ever do.

### Bundled monitor / sentinel daemon (deferred to v0.2 minimum)

The `monitors/` directory exists in the layout but is empty.

**Why not:** monitors would be background processes that watch for stale claims, sync drift, or PRD changes and act on them. Same daemon problem as above. The on-every-op stale-claim sweep handles the most common case without a daemon.

If monitors ever ship, they ship as opt-in `launchd` plists or systemd units, not as default behavior.

---

### Shared backend for multi-host fleets (deferred; fleet-mode forcing function)

The "Multi-backend abstraction beyond SQLite" note above asks for a forcing function. The **fleet thesis is that forcing function**: a fleet spanning a laptop, a cloud VM, and a local GPU box cannot pull from one job board over single-host SQLite. PR #12 (MySQL/Aurora) is the candidate seam.

**Why still deferred:** two hard sub-problems must be specified first. (1) Lease atomicity: the claim's `BEGIN IMMEDIATE` single-winner guarantee needs a real shared-DB equivalent (`SELECT … FOR UPDATE` / row locks), proven under the concurrency suite. (2) The `events.jsonl` replay guarantee is a local-file contract today; in a multi-host world the event log's location and ownership are an open question. Until both are specified (SPEC-FIRST, `docs/specs/`), shared-backend fleet mode stays opt-in and unbuilt. Solo / local-SQLite remains the default.

### Status of the fleet open questions (post-review, 2026-06-20)

The 2026-06-20 review resolved the open questions by *removing* most of them from the near-term build:

- **Actor identity, promoted to a shipped-bug prerequisite.** No longer a design question: claim (`$USER`), heartbeat (`$ANVIL_GATE_ACTOR`), and hooks (`session_id`) resolve the actor three ways, so heartbeat and the finish-gate fail silently *open* today. One `ANVIL_ACTOR` resolver is a prerequisite (see "Why risk-axis eligibility now" § Prerequisites).
- **Type vocabulary, deferred (cut from MVP).** `TaskType` is a 4-value advisory enum; the real work-kind taxonomy is an unbuilt prerequisite, so the `type` axis is out of the MVP entirely.
- **Tier semantics, deferred (cut from MVP).** No `tier` field exists and "minimum-sufficient model" collides with the never-name-a-model invariant; if `tier` ever returns it is a pure function of model-neutral dimensions with the runner owning the `tier→model` map.
- **Capacity-pools, deferred (measure first).** anvil does not model pools yet. The two-loop bake-off measures throttle frequency per flat-rate pool and whether naive spillover to the local box suffices before any pool concept is built.

---

## Where to weigh in

If you disagree with anything here, the design is open for argument.

- **For new capability requests** (a sync provider, a new MCP tool, a missing CLI verb): add to `roadmap.md` under the right theme and target version. Include a one-paragraph "why now": what forcing function makes this the right moment.
- **For cleanups and refactors** (untangle a duplication, fix a hot-path perf budget, close a critic finding): add to `tech-debt-backlog.md` with the origin PR or critic round, severity, and adjacency hints.
- **For new architectural choices** (a second backend, a daemon, a webhook listener): write a SPEC-FIRST design doc in `docs/specs/` before opening a PR. The roadmap items tagged SPEC-FIRST (P9B-5, P9B-9) are the precedents to mirror.
- **For positioning-level changes** (the choices in this file): open an issue, link the user evidence that motivated the change, and propose the trade-off explicitly. The choices in this doc are opinionated, but they are based on current evidence and can change.

The point of writing rationale down is making the trade-offs legible enough to revisit. Each item here is a choice, and most can be changed with new evidence.
