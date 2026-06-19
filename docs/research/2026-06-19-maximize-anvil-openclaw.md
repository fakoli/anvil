# Maximizing anvil on Openclaw — research brief

_Deep-research workflow (6 agents, 27 opportunities). Generated 2026-06-19. Verify the flagged items against the current CLI before building._

## Summary

Anvil installs natively on OpenClaw today (openclaw mcp add anvil plus the Claude-compatible marketplace plugin) -- that is the baseline. The 27 opportunities split on one fault line that should drive every decision. Cron and command-jobs rest on primitives I verified against the live CLI: openclaw cron add (--command, --command-cwd, --announce, --every, --cron) plus anvil next -q (exit 3 on empty queue), drift --json (exit 0), doctor --json (non-zero on problems), sync --fix --yes, list --status needs_review --json. The highest-ceiling items -- the native plugin hooks before_agent_finalize, before_tool_call, after_tool_call -- rest on type-def-only claims from hook-types.d.ts that do NOT appear in the live CLI surface and need a NEW native definePluginEntry plugin anvil lacks today. Ship the verified cron/CLI layer first, build the one missing seam (anvil notify-digest, does not exist, verified), then gate the native plugin behind a smoke test that the hooks fire in 2026.6.6. Two contracts bind everything: anvil writes no files for OpenClaw, and hooks are Claude-Code-only, so cron/agent/config side effects must be printed opt-in, never auto-registered. OpenClaw differentiators over the Claude plugin: blocking gates (Claude's anvil hooks are non-blocking by design), Gateway cron (no model cost, runs with zero active agents, makes lazy leases actively-enforced), chat channels (Slack/Telegram finish-gate pings), and isolated agents plus session-scoped sandboxes (one container per claimed task).

## Quick wins

- Queue-probe cron (Opp 8): anvil next -q every 10m, exit 0 claimable 3 empty, zero model cost; ship as a printed recipe.
- Nightly sync+drift reconcile cron (Opp 9): sync github then sync --fix --yes then drift --json; start read-only (no --fix) for the first runs.
- Lease-watchdog cron (Opp 11): doctor --json every 15m, chain sync --fix --yes only on a clear signal; doctor exits non-zero on stale-lease problems.
- Install-time sandbox allowlist note (Opp 13): print that the user must add anvil tools to sandbox.tools.allow or the 24 MCP tools vanish in sandboxed turns.
- Finish-gate nudge cron (Opp 10/21): list --status needs_review --json on a weekday cron with announce to slack; cheapest review ping.

## Roadmap

### Phase 0 -- Verification spikes

_Top tiers rest on .d.ts and docs claims absent from the verified CLI surface; cheap spikes de-risk before building against APIs that may not fire in 2026.6.6._

- Smoke-test a definePluginEntry plugin on before_agent_finalize and before_tool_call; confirm they fire and revise/retry plus requireApproval behave as the type defs claim.
- Confirm openclaw config set array-append for sandbox.tools.allow and memorySearch.extraPaths plus the re-index cadence.
- Run openclaw cron run <id> --wait on an anvil next -q job to confirm PATH, cwd, and exit-3 propagation on the Gateway host.

### Phase 1 -- Cron + CLI layer (verified, no native plugin)

_Every primitive is verified against the live CLI; zero long-running code; the Gateway owns scheduling and delivery; honors both contracts by printing recipes opt-in. The net-new anvil notify-digest is small and unblocks all channel work._

- Build anvil notify-digest (Opp 20): one-line needs_review plus blockers summary, prints nothing at count zero so --announce stays silent.
- Ship printed cron recipes: queue-probe (8), sync+drift reconcile (9), lease-watchdog (11), finish-gate nudge (10/21), work-queue drain (27).
- Add the sandbox-allowlist note (13) and an opt-in install flag that emits but does not run the cron add commands.
- Add anvil claims reap (17) or wire reaping onto a documented sync invocation.

### Phase 2 -- Native plugin: blocking gates (the differentiator)

_Makes OpenClaw stronger than the Claude plugin, whose anvil hooks are non-blocking by design. Gated behind the Phase 0 smoke test. Anvil's first native definePluginEntry plugin._

- before_agent_finalize finish-gate (1): block done when a claimed task has absent or failing evidence; revise plus retry with maxAttempts; scope tightly.
- after_tool_call evidence auto-capture (3): on exec tools matching verification patterns, write command, exit code, stdout, stderr to the active claim evidence buffer.
- before_tool_call claim guard (2): default requireApproval/warn, hard-block behind config; matcher from OpenClaw apply_patch/exec names plus derivedPaths.
- session_start plus before_prompt_build injection (4): state banner plus next-ready-task line; prependSystemContext for cacheable static guidance.

### Phase 3 -- Autonomy, isolation, recall

_Composes the verified cron layer with isolated agents, session sandboxes, channel pings, and memory recall into a full claim/execute/submit/review loop; sequence after the gates so the loop runs inside the finish-gate net._

- Ready-queue work loop (7): scheduled isolated agent doing one task per tick, pre-gated by the queue-probe (8).
- Dedicated isolated agent per project so claim.actor equals agent id equals container equals branch (14, opt-in).
- Session-scoped sandbox (12): task-id as session key, one container per task mounting the worktree; prereq 13 shipped in Phase 1.
- Work-packet memory recall (25): emit per-task memory files, register via extraPaths; gated on the Phase 0 config spike.
- anvil notify over openclaw message send for instant pings (16); ACP --session bridge for IDE work (15).

### Phase 4 -- Defer / parked

_Experimental upstream surfaces, low marginal value, or explicit non-opportunities; re-check when the surface stabilizes._

- DEFER fleet-spawn via Claw Supervisor (18): experimental; use generic sessions_spawn at most.
- SKIP anvil-as-custom-ACP-harness (19): the allowlist is closed in 2026.6.6; use the reverse session bridge (15).
- DEFER native message-event hook pack (24) and skill-workshop propose-create (26): cron covers notification; workshop is superseded by the marketplace install.
- OPTIONAL ClawHub skill bundle (23) and commitments import (22): only if native verify/update or follow-up pull is wanted; publish under a disambiguated slug and human-gate the import.

## Top opportunities

### before_agent_finalize finish-gate enforcer (blocking Stop hook)  (high/M)

- **Integration:** submit_completion_evidence and get_task evidence buffer
- **How:** Native definePluginEntry on before_agent_finalize. If the agent ends a turn with a claimed task whose verification commands have no passing evidence, return action revise with a retry instruction to run the commands then submit, idempotencyKey of task and runId, maxAttempts 3; else continue. Stronger than Claude (anvil hooks there are non-blocking by design). Gated on the Phase 0 smoke test since it is in the .d.ts but absent from the live CLI surface. Scope the block tightly to a claimed-but-unverified task.

### anvil notify-digest CLI plus cron finish-gate notifier  (high/S)

- **Integration:** list --status needs_review and ProjectSummary; new notify-digest subcommand
- **How:** Build anvil notify-digest (does not exist, verified): one-line needs_review plus blockers summary that prints nothing at count zero. Ship the recipe: cron add every 30m running notify-digest in the project bin with announce to slack. Empty-stdout-stays-silent keeps a 30-min cron non-spammy. Zero long-running code. Do not auto-register on install; offer opt-in per the no-files contract.

### Queue-probe plus ready-queue isolated-agent work loop  (high/M)

- **Integration:** next -q exit 0/3 pre-gate plus claim/execute/submit MCP loop
- **How:** Pre-gate (S, command cron, no model cost): cron add every 10m running anvil next -q (ready on 0, no-reply on 3). Loop (M, agent cron): isolated session, anvil-runner agent, message to get the next task and if claimable follow claim then execute and submit real evidence else stop, tools exec/read/write, timeout 1800. Exclusivity is anvil's, safe alongside humans. Confirm the agent workspace resolves to the project dir. Best paired with the Phase 2 finish-gate.

### after_tool_call evidence auto-capture  (high/M)

- **Integration:** submit_completion_evidence and evidence buffer; replaces capture-evidence.sh
- **How:** On after_tool_call, for exec tools whose command matches a verification pattern (pytest, ruff, mypy, cargo test), write command, exit code, stdout, stderr to the active claim evidence buffer. Native and event-driven versus the Claude-only PostToolUse script. OpenClaw result/error shape differs from Claude tool_response, so rewrite the extractor against the OpenClaw event. Keep truncation discipline. Gated on Phase 0.

### Nightly sync+drift reconcile and lease-watchdog crons  (high/S)

- **Integration:** sync, sync github, drift --json, doctor --json
- **How:** Reconcile (daily): cron add running sync github then sync --fix --yes then drift --json in the project bin with announce. Watchdog (15m): doctor --json chained to sync --fix --yes only on a clear stale-lease signal. Verified: sync --fix needs --yes; drift always exits 0 so surface failures from the sync step; doctor exits non-zero on lease problems. Use the unset-GITHUB_TOKEN form per CLAUDE.md. Makes lazy leases actively-enforced with zero active agents. Start read-only first.

### Session-scoped sandbox per claimed task plus MCP allowlist prerequisite  (high/M)

- **Integration:** claim/lease model and git worktree; task-id as the session key
- **How:** Convention only (no engine code): session key from the task id, set sandbox to non-main mode, session scope, rw workspace so each task runs in a throwaway container mounting its worktree. Prerequisite (Opp 13, first): under any sandbox mode the 24 MCP tools vanish unless the installer prints to allowlist anvil tools. Verify container-per-session-key (sandbox list showed 0, off by default) and that the worktree survives the rw mount. Docker, opt-in. Container lifecycle is independent of lease expiry; pair with prune or a reaper cron. Shipped as printed guidance, not auto-writes.

## Verify before building

- CRITICAL (Opps 1,2,3,5): the plugin hook names come only from hook-types.d.ts and are absent from the live CLI surface; the agent-loop docs only listed events up to agent_end. Smoke-test that before_agent_finalize and before_tool_call fire in 2026.6.6 before investing, or the tier collapses to its cron equivalent.
- anvil notify-digest does not exist (verified). Opps 20, 16, 22 and the notification crons depend on it; net-new CLI work, sequence first.
- anvil claims reap does not exist (verified). Opp 17 needs this new verb or must ride on sync --fix --yes; today reaping is only a side effect of other commands.
- memorySearch.extraPaths (Opp 25) is absent from the CLI surface. Verify openclaw config set can append to an array and the re-index cadence before wiring memory files.
- Tool-name matcher (Opp 2): before_tool_call fires on OpenClaw envelopes (code_mode_exec, apply_patch), not Claude Edit/Write; rewrite the matcher and the after_tool_call extractor against the OpenClaw event result/error shape.
- Sandbox session-key to container (Opp 12): sandbox list showed 0 (off by default). Confirm one container per session key and that a worktree path survives the rw mount.
- Cron host PATH (Opps 8,9,11): cron runs as sh -lc on the Gateway host; confirm anvil resolves there or use absolute uv run with command-env PATH, and confirm exit 3 propagates.
