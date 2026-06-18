# Model strategy

This document explains *why* anvil's agents default to specific Claude tiers (Opus / Sonnet / Haiku) and how to override them. Companion to [`docs/llm-providers.md`](llm-providers.md), which covers *how* to configure each provider.

---

## The headline rule

**Default everything to Sonnet, escalate to Opus only for reasoning/architecture/synthesis, drop to Haiku for read-only / mechanical / lookup work.**

This is the 2026 community consensus, codified in Anthropic's own docs and surfaced via the routing-telemetry issue ([anthropics/claude-code#27665](https://github.com/anthropics/claude-code/issues/27665)) which documented that 93.8% of tokens were being routed to Opus when smarter defaults would cut that to ~30%. Defaulting every agent to Opus is the headline cost anti-pattern in agent setups — it costs roughly 5× more per token than Sonnet without quality wins on most agent work.

anvil's tier defaults (v1.17.0) follow this rule directly: `DEFAULT_TIER = "sonnet"` in `planning/llm.py`, and each agent's frontmatter sets `model:` to the tier appropriate for the work it does.

---

## Tier ↔ agent mapping

### anvil (5 agents)

| Agent | Tier | Why |
| --- | --- | --- |
| `planner` | **opus** | PRD-to-tasks synthesis. Requires understanding implicit dependencies, sizing tasks against acceptance criteria, and deciding what to leave for `expand`. Hard reasoning over structured but ambiguous input. |
| `critic` | **opus** | Code review against acceptance criteria. Subtle bugs (race conditions, broken invariants, security regressions) are the high-value finds; reasoning depth dominates token efficiency. |
| `docs-scribe` | **sonnet** | Structured generation of CHANGELOG entries, README updates, cross-reference fixes. The input (a code change) and output shape (a Keep-a-Changelog entry) are both well-defined. |
| `sentinel` | **haiku** | Evidence validation: run a shell command, parse exit code, compare against acceptance criteria. The classic "read-only investigator" case Anthropic's own `Explore` subagent uses Haiku for. |
| `state-keeper` | **haiku** | Cross-source-of-truth scan: glob the filesystem, query SQLite, list git branches, report drift. Pure read-and-classify. |

---

## Override precedence

Users always win:

1. **Per-call argument** — pass `model=` or `tier=` to a provider constructor in code.
2. **Env var** — set `ANTHROPIC_MODEL` (Anthropic-supported) or use Claude Code's `CLAUDE_CODE_SUBAGENT_MODEL=inherit` to force every subagent to the session model.
3. **Project config** — set `llm_tier: opus` or `llm_model: <id>` in `.anvil/config.yaml` to apply project-wide.
4. **Agent frontmatter** — the `model:` field in each agent's `.md` file sets the tier-default for that agent.
5. **Module default** — `DEFAULT_TIER = "sonnet"` in `planning/llm.py`.

Higher numbers override lower ones. If a user explicitly wants Opus everywhere, **that choice is respected** — the tier defaults are recommendations, not lock-ins.

---

## Automatic escalation (deferred)

Anthropic ships exactly one first-party "escalate on complexity" pattern: the `opusplan` model alias, which uses Opus in plan mode and auto-switches to Sonnet for execution. This is the only escalation pattern with first-party support as of May 2026.

Third-party community routers exist (`tzachbon/claude-model-router-hook`, `0xrdan/claude-router`, `musistudio/claude-code-router`) that classify prompt complexity at the PreToolUse hook level and rewrite the model. None of these are first-party, and anvil does not ship its own router because:

1. **Measuring before optimizing** — most projects' agent spend is dominated by overuse of Opus on simple turns, not by undertuning of any single turn. Switching defaults to Sonnet (already done in v1.17.0) captures the bulk of the savings without dynamic routing.
2. **Cost predictability** — a dynamic router can surprise ops teams when prompt classification flips a critical path to Haiku. The cost win is real, but the failure mode (a planning task accidentally classified as "simple" and run on Haiku) is opaque to debug.
3. **Opt-in over default** — users who want dynamic routing today can wire `tzachbon/claude-model-router-hook` themselves and override per-agent. Bundling it would force a one-size-fits-all policy.

If a future anvil release ships dynamic escalation, it will be via an explicit `llm_router:` config key, not by default.

---

## Cost reference (May 2026 prices, per million tokens)

| Tier | Direct API in / out | Notes |
| --- | --- | --- |
| Opus 4.7 | $15 / $75 | Use sparingly; deep reasoning only. |
| Sonnet 4.6 | $3 / $15 | The default. Roughly **5× cheaper than Opus**. |
| Haiku 4.5 | $1 / $5 | Roughly **15× cheaper than Opus**. The floor for mechanical work. |

Bedrock pricing varies by region and inference profile. Custom endpoints (vLLM self-hosted) carry hosting cost only; OpenRouter / Together pass through provider rates with a margin.

For a typical anvil planning session (one PRD → tasks generation + 4 expansions + 6 score augmentations + 3 critic reviews), the v1.17.0 tier defaults reduce per-session token spend by ~60% versus the prior "everything inherits Opus" pattern, with no measurable quality regression on the planner and critic paths (which stay on Opus).

---

## Pinning a project to Opus

Some teams genuinely want Opus everywhere — for compliance, audit, or because their work is consistently in the "deep reasoning" bucket. To pin:

```yaml
# .anvil/config.yaml
llm_tier: opus
```

This sets the floor: every provider built by `resolve_planner_provider(config)` uses Opus. Individual agent frontmatter `model:` values are independent of this (they govern Claude Code subagent dispatch, not the planning-augmentation calls anvil makes), so if you want Opus across the whole agent fleet too, also set `CLAUDE_CODE_SUBAGENT_MODEL=claude-opus-4-7` in your environment.

---

## References

- [Claude Code model-config](https://code.claude.com/docs/en/model-config) — `opusplan`, `CLAUDE_CODE_SUBAGENT_MODEL`, and the alias system.
- [Choosing a Claude model](https://platform.claude.com/docs/en/about-claude/models/choosing-a-model) — Anthropic's official per-task tier guidance.
- [anthropics/claude-code#27665](https://github.com/anthropics/claude-code/issues/27665) — the routing-telemetry issue that triggered the 2026 "Sonnet by default" consensus.
- [Anthropic prompt caching docs](https://platform.claude.com/docs/en/build-with-claude/prompt-caching) — referenced in `planning/llm.py` for the `cache_control: {"type": "ephemeral"}` pattern.
