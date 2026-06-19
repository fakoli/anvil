# Maximizing anvil on Codex — research brief

_Deep-research workflow (6 agents, 32 opportunities). Generated 2026-06-19. Verify the flagged items against the current CLI before building._

## Summary

Verified against live codex-cli 0.130.0 and the anvil repo at HEAD. The 32 opportunities collapse to about 14 distinct bets after merging duplicates (the openai.yaml metadata cluster, the hooks-bundle cluster, the codex-exec runner cluster, and three near-identical review-as-gate items). Decisive finding: Codex plugin hooks use the IDENTICAL Claude Code hooks.json schema, confirmed by the on-machine fakoli hooks.schema.json (it says based on Claude Code docs) and by real cached plugin hooks.json files using the CLAUDE_PLUGIN_ROOT variable. anvil already ships hooks/hooks.json plus four hook scripts plus the cli/hooks.py helpers, so bundling them into the Codex plugin is a packaging move with ZERO engine change and closes the single biggest gap (today Codex gets MCP + plugin + automations but no hooks). Second tier is cheap polish: per-skill agents/openai.yaml (the real installed schema is minimal, only display_name, short_description, icon_large, icon_small, default_prompt; NO dependencies.tools or policy.allow_implicit_invocation appears in any real installed example, so treat those as unverified) and tightening automation prompts to call the claim and execute skills by name and bound memory.md. Third tier (new engine code) is the Stop-hook evidence gate plus PostToolUse heartbeat (one new anvil hook subcommand each, cross-harness) and the codex exec and review runners under packaging/codex/loops/. Hard constraints throughout: anvil must NEVER text-edit the codex config.toml (it corrupted it before; a config.toml.corrupt file is present on this machine), and codex exec and review accept -c key=value plus -s -a -p flags so all per-run config goes through invocation flags. codex review has NO --json flag (verified on 0.130.0), so review-as-gate must grep a verdict line, not parse JSON. Ranking by value over effort: the openai.yaml polish and automation-prompt fixes are S-effort high-value quick wins; the hooks bundle is the M-effort headline; the exec and review runners and Stop-gate are M-effort follow-ons; cloud, fork, session-fork and trust-level are L-effort or constraint-blocked and should stay opt-in or be dropped.

## Quick wins

- Ship per-skill agents/openai.yaml for all 8 anvil skills plus shared anvil.png and anvil-small.svg icons, using the REAL minimal schema confirmed on this machine (interface block with display_name, short_description, icon_large, icon_small, default_prompt), NOT the speculative dependencies.tools or policy blocks. Today anvil ships zero, so skills appear nameless in the /skills picker and Plugins panel. The Codex plugin.json already points skills at ./skills/ so they install verbatim; Claude Code ignores the agents/ subdir, keeping the tree dual-harness clean. Effort S, value high.
- Rewrite the anvil-work-queue automation.toml prompt to drive skills by name (run the claim skill then the execute skill) instead of re-describing the loop in prose, so the recurring run cannot drift from the interactive skill logic. Add the guardrail: open one PR per task, never merge. Effort S, value high. Verify the skill sigil resolves in an automation run before shipping.
- Add a standing memory.md-trimming instruction to every automation prompt: keep only the last 3 run summaries plus a short persistent-facts header, rewrite to stay under about 40 lines. The real on-machine memory.md is an ever-growing transcript; the prompt is the only lever. Effort S, value high.
- Drop reasoning_effort to medium on the read-only sync-reconcile and new finish-sweep templates; keep high only on anvil-work-queue. Cost-proportional fleet. Effort S, value medium. Confirm medium is valid for gpt-5-codex on the installed build.
- Add a note to packaging/codex/automations/README.md: anvil's exclusive lease makes the work-queue automation idempotent under the known local-vs-UTC double-fire scheduler bug (a second concurrent run claims nothing), so lean on the lease rather than fighting BYHOUR. Effort S, value low.
- Add a packaging/codex decision record: do NOT ship custom slash-command prompt files (deprecated in favor of skills, user-home-only so the plugin cannot distribute them) and do NOT write a notify config key (global, exclusive, already taken by Computer Use on this machine, and cannot block). Prevents future contributors reaching for the wrong surface. Effort S, value low.

## Roadmap

### Phase 0 — quick wins (S-effort, days)

_All packaging or prompt only, zero engine change, immediately visible in the skills picker and Plugins panel. Ships the cheapest polish first and removes the wrong-surface traps before anyone reaches for them._

- Ship agents/openai.yaml with the minimal interface schema plus shared anvil icons for all 8 skills.
- Rewrite the anvil-work-queue prompt to drive the claim and execute skills by name and add the never-merge guardrail.
- Add the memory.md-trimming instruction to every automation prompt.
- Drop reasoning_effort to medium on the read-only sweep templates and keep high on the work-queue.
- Add README decision records: no custom prompt files, no notify config write, exclusive-lease makes double-fire safe.

### Phase 1 — the headline: bundle the hooks (M-effort)

_Closes the largest capability gap (claim discipline and evidence capture on Codex) using anvil's already-shipped, already-tested hook scripts and CLI helpers, verified to be a drop-in because Codex plugin hooks use the identical Claude Code schema. Highest value over effort of any code-touching item._

- Copy the hooks dir into packaging/codex/ and add the hooks key to the Codex plugin.json (SessionStart state, PreToolUse claim-check, PostToolUse file-change and Bash evidence).
- Switch script paths to PLUGIN_ROOT with a CLAUDE_PLUGIN_ROOT fallback.
- Update install and onboarding text to instruct trusting anvil's non-managed plugin hooks.
- Add the read-only anvil-finish-sweep automation template that surfaces needs_review and never approves.

### Phase 2 — lifecycle enforcement (M-effort engine code)

_Turns anvil's evidence gate and lease promise into actively enforced behavior during a run, not just convention. Small well-scoped code on top of existing renew and list_active_claims; depends on Phase 1 hook plumbing._

- Add anvil hook turn-end, the Stop-hook evidence gate that blocks once when a claim is unfinished and has no evidence and honors stop_hook_active.
- Add anvil hook heartbeat, the PostToolUse lease renewal that is rate-limited.
- Wire both into the Codex plugin hooks.json and reuse on Claude Code Stop and PostToolUse for a cross-harness win.

### Phase 3 — headless runners plus semantic review gate (M-effort)

_Gives a cron or CI path outside the Codex app and fills the execute skill Phase 7 self-review gap with a real semantic check. M-effort because codex review has no JSON output (verified) so the verdict line must be parsed, and sandbox enforcement is OS-specific._

- packaging/codex/loops/anvil-exec-queue.sh that drains the queue with codex exec, calls the anvil CLI inside the run, and parses the output-last-message.
- packaging/codex/loops/anvil-review-branch.sh that runs codex exec review with acceptance criteria and a VERDICT grep for the submit and finish gates and never auto-approves.
- packaging/codex/loops/anvil-sync.sh.
- A per-task sandbox and approval matrix derived from task kind, passed as codex flags, never the config.toml.

### Phase 4 — opt-in, experimental, deferred (L-effort or constraint-blocked)

_These either fight anvil local-first ethos (cloud), require unverified session-capture internals, or collide with the hard no-config-edit constraint. Park them behind explicit opt-in flags or documentation and revisit only after the surfaces stabilize._

- Per-task session fork where codex exec json captures the session id and resume continues a long task, storing the session id on the claim. Verify the field name first; output-schema cannot combine with resume.
- codex cloud exec best-of-N queue plus the codex apply bridge: EXPERIMENTAL, needs an env and a pushed branch, separate id namespace; keep docs-only and opt-in.
- Per-project trust_level and per-role profiles: MUST go through codex's own flags or commands or a copy-paste snippet the user applies; anvil NEVER text-edits config.toml. Deliver as printed snippets, not automated writes.
- notify-bridge and PostToolUse-on-MCP audit: low value, fire-and-forget bookkeeping only, not safety gates. Skip unless a concrete need appears.

## Top opportunities

### Bundle anvil's existing hooks.json into the Codex plugin (the headline move)  (Closes the single biggest Codex gap: today Codex gets MCP + plugin + automations but none of the claim-discipline and evidence-capture loop the Claude plugin has. Verified that Codex plugin hooks use the IDENTICAL Claude Code hooks.json schema (the fakoli hooks.schema.json says based on Claude Code docs; real cached plugin hooks.json files use CLAUDE_PLUGIN_ROOT). So SessionStart project-state injection, PreToolUse claim-check, PostToolUse file-change recording, and Bash evidence-capture all come along for free./M, packaging-only, ZERO engine change. The cli/hooks.py helpers parse the same JSON fields Codex passes.)

- **Integration:** Copy hooks.json and the four hook scripts (detect-state, check-claim, record-file-change, capture-evidence) into packaging/codex/ and add a hooks key pointing at ./hooks/hooks.json in packaging/codex/.codex-plugin/plugin.json, mirroring how figma's plugin.json declares apps and skills. detect-state.sh already calls anvil status with hook-format (verified to exist).
- **How:** Mirror the root hooks.json into packaging/codex/hooks/. Change script paths to use PLUGIN_ROOT with a CLAUDE_PLUGIN_ROOT fallback. Add the hooks key to the Codex plugin.json. Update install and onboarding text to tell the user to trust anvil's hooks since plugin hooks are non-managed until trusted once. Test by installing the plugin into a throwaway Codex profile and confirming the SessionStart banner fires.

### Ship agents/openai.yaml plus icons for all 8 skills (Plugins-panel metadata)  (Pure polish versus regression: with zero openai.yaml today, all 8 anvil skills show up nameless and iconless in the /skills picker and Plugins panel. A display_name, short_description, anvil icon, and default_prompt per skill is the cheapest visible quality win and the literal point of the maximize-on-Codex goal./S.)

- **Integration:** Files live in this repo at skills/NAME/agents/openai.yaml; the Codex plugin already ships skills at ./skills/ so they install verbatim. Claude Code reads only SKILL.md and ignores the agents dir, so the shared tree stays dual-harness clean.
- **How:** Use the minimal schema confirmed from the installed pdf and playwright skills: an interface block with display_name, short_description, icon_large pointing at assets/anvil.png, icon_small pointing at assets/anvil-small.svg, and default_prompt. One shared assets icon pair. Give each skill a human display_name such as Anvil Claim a task or Anvil Execute a task and a default_prompt that seeds the turn. Do NOT add dependencies.tools or policy blocks yet, unverified.

### Tighten the automation prompts: drive claim and execute skills by name, bound memory.md, guard the finish gate  (Keeps recurring runs in lockstep with interactive skill logic (no prose drift), caps unbounded memory.md growth (cheaper and faster runs), and hard-codes the invariant that automations stop AT the human finish gate. High value for a few prompt edits./S.)

- **Integration:** Edit packaging/codex/automations/anvil-work-queue and anvil-sync-reconcile automation.toml files; add a new anvil-finish-sweep template that runs the READ side of anvil apply with json and no approve or reject flags, reporting needs_review tasks for a human and never deciding.
- **How:** Work-queue prompt: run the claim skill to acquire the next ready task, then the execute skill to implement to passing evidence and submit; if the queue is empty, stop; open one PR per task; never merge. Append to every prompt: maintain memory.md as a rolling log with the last 3 run summaries plus a short persistent-facts header, rewrite to stay under about 40 lines. The finish-sweep prompt MUST forbid anvil apply approve and say so loudly in the template and README. Verify the skill sigil resolves in an active run first; verify anvil subcommand names against the relevant help output.

### Stop-hook evidence gate plus PostToolUse lease heartbeat (one new hook subcommand each)  (Two cross-harness wins with no equivalent today. The Stop hook blocks the case where an agent ends a turn holding an active claim with no submitted evidence, by emitting a block decision with a reason, exactly the evidence-gate enforcement the task asks for. The PostToolUse heartbeat renews the lease during long turns so a turn over 60 minutes does not let the claim go stale and get double-claimed. Both also wire into Claude Code's identical Stop and PostToolUse events./M, new code but small: two siblings to the three existing helpers in cli/hooks.py, reusing list_active_claims and renew which already exist.)

- **Integration:** Add an anvil hook turn-end subcommand that looks up the actor's active claim, inspects the evidence buffer, and prints block JSON only when the claim is unfinished AND stop_hook_active is false. Add an anvil hook heartbeat subcommand that does a rate-limited renew of the actor's claim. Wire both into the Codex plugin hooks.json on the Stop and PostToolUse events shipped in the headline bundle.
- **How:** turn-end must honor stop_hook_active to avoid a block loop and pass silently and fast (under 200ms, deferred imports) when there is no claim or evidence is already submitted. heartbeat must swallow renew's stale-or-expired error since hooks must never block, and rate-limit using default_heartbeat_minutes of 5 so it is not a DB write per tool call. Verify Codex injects the Stop reason as a continuation before relying on it.

### Headless exec and review runners under packaging/codex/loops/ (codex exec drives the queue; codex review gates submit and finish)  (Gives users a cron or CI path outside the Codex desktop app, the headless twin of the paused automations, and fills the execute skill's documented Phase 7 gap (LLM-assisted self-review on submit) by running codex review against the claim branch with the task acceptance criteria as the review prompt. Review covers the SEMANTIC acceptance axis that anvil's presence-only evidence gate cannot./M, thin shell over existing seams: anvil next quiet exit-3 loop terminator, anvil packet json, anvil submit json, codex exec workspace-write sandbox, codex exec review against base main.)

- **Integration:** Three scripts: anvil-exec-queue.sh that loops while anvil next quiet succeeds, claims then runs codex exec then submits; anvil-review-branch.sh that wraps codex exec review for the submit and finish gate; anvil-sync.sh that wraps anvil sync fix yes. Per-run config goes through codex flags, NEVER the config.toml.
- **How:** Map task kind to sandbox: read-only tasks get read-only sandbox and on-request approval; code tasks get workspace-write sandbox with the worktree as cwd and add-dir if the anvil state dir is outside cwd; never danger-full-access without an explicit task flag. Review gate: pipe the acceptance criteria from anvil packet json as the review instruction, end the prompt with a VERDICT PASS or FAIL line, and grep the output-last-message since codex review has no json (verified). On FAIL loop back to fixing instead of submitting. Document workspace-write as the recommended unattended mode and warn against bypass-approvals outside a dedicated VM.

## Verify before building

- The extended openai.yaml schema (dependencies.tools and policy.allow_implicit_invocation) appears in NO real installed openai.yaml on this machine (pdf and playwright use only the minimal interface block). Ship interface-only YAML first and validate the extended keys against the Codex skills docs with the codex skills command locally before adding them; a malformed dependencies block can stop a skill loading.
- codex review and codex exec review have NO --json flag (verified on 0.130.0: only --uncommitted, --base, --commit). Any review-as-gate must ask the review prompt to end with a greppable verdict line such as VERDICT PASS or FAIL and grep for it; do not promise structured JSON.
- The skill invocation sigil inside an automation.toml prompt is documented but unconfirmed on this build. Run one ACTIVE non-paused automation that uses the claim skill and confirm it resolves from the codex skills dir before shipping the rewritten prompt. Skills must already be installed via the marketplace for the sigil to resolve.
- Plugin-bundled hooks are non-managed: Codex skips them until the user reviews and trusts the hook definition once (per-plugin trust toggle). The install and onboarding flow must tell the user to trust anvil's hooks or they silently no-op. Verify the exact trust UX on 0.130.0.
- Confirm Codex exposes PLUGIN_ROOT and/or the legacy CLAUDE_PLUGIN_ROOT alias to plugin hooks (cached example plugin hooks.json files use CLAUDE_PLUGIN_ROOT, strong evidence the alias works). Make scripts reference PLUGIN_ROOT with a CLAUDE_PLUGIN_ROOT fallback to be future-proof.
- Confirm Codex's Stop hook injects the block-decision-with-reason continuation the same way Claude does, and honors stop_hook_active to prevent block loops, before building the evidence-gate Stop hook.
- Confirm reasoning_effort allowed values (low, medium, high) and the exact model id for the installed build before tuning per-automation effort (the on-machine file uses gpt-5-codex and high).
- codex exec output-schema is reported to be silently ignored when MCP servers are active (anvil's own MCP server is configured) and to also constrain intermediate messages. Prefer having the runner call the anvil CLI directly and parse the output-last-message file rather than relying on output-schema enforcement. Verify on 0.130.0.
- codex cloud is EXPERIMENTAL on 0.130.0 and requires a configured env and a pushed remote branch (not local-first). codex apply takes a Codex cloud TASK_ID, a different namespace from an anvil task id. Keep cloud and apply work docs-only and opt-in until the surface stabilizes; record the id mapping if pursued.
- Programmatic codex session-id capture for the per-task fork idea is via the json event stream; the exact event or field name is unconfirmed. Run codex exec with json on a noop and inspect the output before persisting a session id on the claim row. output-schema cannot be combined with resume yet.
