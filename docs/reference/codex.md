# Codex CLI — feature surface & doc map

> **Purpose.** A living index of OpenAI **Codex CLI** features, *where each one is
> authoritatively documented* (on-disk + official URL), and *how it relates to
> anvil*. Use it to decide which Codex surfaces to exploit next without
> re-discovering them. Companion to the point-in-time research brief
> [`docs/research/2026-06-19-maximize-anvil-codex.md`](../research/2026-06-19-maximize-anvil-codex.md)
> (ranked opportunities) — this file is the **map**, that file is the **bets**.

**Verified against:** `codex-cli 0.130.0`, macOS, 2026-06-19. The running model on
this machine is `gpt-5.5` (reasoning `low|medium|high|xhigh`). Codex moves fast —
re-verify flags with `codex <cmd> --help` and re-read `~/.codex/` before relying on
any "needs smoke test" item below.

**How to refresh this doc:** fan out parallel research agents (one per section), each
reading the on-disk sources + the official URL, then reconcile and adversarially
fact-check the claims before updating here.

---

## Where to find the truth

| Kind | Location |
| --- | --- |
| Official docs root | <https://developers.openai.com/codex/> |
| Config reference | <https://developers.openai.com/codex/config-reference> · <https://developers.openai.com/codex/config-basic> |
| MCP | <https://developers.openai.com/codex/mcp> |
| Sandbox / approvals / trust | <https://developers.openai.com/codex/agent-approvals-security> |
| Non-interactive (`exec`) | <https://developers.openai.com/codex/noninteractive> |
| CLI reference (resume/fork) | <https://developers.openai.com/codex/cli/reference> |
| App-server (JSON-RPC) | <https://developers.openai.com/codex/app-server> |
| Cloud environments | <https://developers.openai.com/codex/cloud/environments> |
| Memories · AGENTS.md · custom prompts | <https://developers.openai.com/codex/memories> · <https://developers.openai.com/codex/guides/agents-md> · <https://developers.openai.com/codex/custom-prompts> |
| notify / advanced config | <https://developers.openai.com/codex/config-advanced> |
| **On-disk authoritative state** | `~/.codex/` (a.k.a. `CODEX_HOME`) |
| ↳ config | `~/.codex/config.toml` (TOML; **never text-edit — see constraints**) |
| ↳ valid models + reasoning levels | `~/.codex/models_cache.json` |
| ↳ installed plugins / marketplaces | `~/.codex/plugins/cache/`, `~/.codex/.tmp/marketplaces/` |
| ↳ a pulled plugin (anvil) | `~/.codex/worktrees/<hash>/anvil/` |
| ↳ installed skills | `~/.codex/skills/<name>/agents/openai.yaml` |
| ↳ automations | `~/.codex/automations/<id>/automation.toml` |
| ↳ sessions (rollouts) | `~/.codex/sessions/YYYY/MM/DD/rollout-<ISO>-<UUID>.jsonl` + `~/.codex/session_index.jsonl` |
| ↳ memories (experimental) | `~/.codex/memories/` (`MEMORY.md`, `raw_memories.md`, …) + `~/.codex/memories_1.sqlite` (store, at CODEX_HOME root) |
| Loader source of truth (schemas) | strings compiled into the `codex` binary (`core-skills/src/loader.rs` etc.) |

---

## anvil ↔ Codex at a glance

| Surface | anvil status | Notes |
| --- | --- | --- |
| **Plugin / marketplace** | ✅ shipped | `codex plugin marketplace add fakoli/anvil` pulls the **repo root** as the plugin |
| **MCP server** | ✅ shipped | `codex mcp add anvil -- uv run --quiet --project …/bin python -m anvil.mcp_server` (36 tools); never via config edit |
| **Skills** (`openai.yaml`) | ✅ this PR (B41) | 8 skills now named in the picker/Plugins panel |
| **Hooks** | ✅ free via root plugin | root `hooks/hooks.json` uses shell-free `uv run --quiet … anvil.cli hook dispatch …` commands |
| **Automations** | ✅ shipped, PAUSED | `anvil install codex --automations`; never auto-activated |
| **Headless `exec`/`review` runners** | ⬜ B41 follow-on | `packaging/codex/loops/` (not built yet) |
| **PostToolUse lease heartbeat** | ✅ shipped (B41) | `anvil hook heartbeat`, wired in `hooks.json` (non-blocking, cross-harness) |
| **Stop-hook evidence gate** | ✅ verb shipped, OPT-IN (B41) | `anvil hook stop-gate`; NOT auto-wired (blocking) — opt in + `/hooks` trust + verify (below) |
| **Sessions / fork / cloud** | ⬜ deferred | opt-in, experimental; see brief Phase 4 |

---

## Feature surface

Status legend: **✓ verified** (on-disk or CLI) · **▲ needs live smoke test** · **✗ avoid / blocked**.

### Plugins & marketplaces
- **What:** `codex plugin marketplace add <owner/repo | URL | local-dir>` registers a
  marketplace; the in-app `/plugin install <plugin>@<marketplace>` installs it.
  Codex reads a **Claude-compatible** marketplace at `.claude-plugin/marketplace.json`
  and the plugin manifest at `.claude-plugin/plugin.json`. ✓
- **anvil:** the install pulls the **repo root** (`source: "./"`), so `skills/`,
  `hooks/`, and `.mcp.json` ship from root — `packaging/codex/.codex-plugin/` is a
  staging artifact **not on the real install path** (install.py only consumes
  `packaging/codex/automations/`). ✓ (verified by local `marketplace add` + the pulled
  copy at `~/.codex/worktrees/315d/anvil/`).
- **Source:** `codex plugin marketplace --help`; `~/.codex/config.toml` `[marketplaces.*]`/`[plugins.*]`.

### Skills (`agents/openai.yaml`)
- **What:** per-skill metadata for the `/skills` picker + Plugins panel. Lives at
  `skills/<name>/agents/openai.yaml`; Claude Code ignores the `agents/` dir (dual-harness clean). ✓
- **Schema** (`interface:` block; the loader recognizes exactly these keys):

  | key | required | notes |
  | --- | --- | --- |
  | `display_name` | conv. | picker chip label; every shipped skill sets it (a folder-name fallback exists, but no on-disk skill omits it) |
  | `short_description` | conv. | **should be 25–64 chars** — a *scaffolder guideline* (`init_skill.py`), **not** enforced by the runtime loader: 170/760 shipped skills (incl. OpenAI's own) sit outside the range and load fine |
  | `default_prompt` | no | seed prompt; official guidance says reference the skill as `$skill-name` |
  | `icon_large` | no | `./assets/x.png` relative to skill dir (only ~28% of skills set it) |
  | `icon_small` | no | `./assets/x-small.svg` |
  | `brand_color` | no | hex e.g. `"#60a5fa"` — picker badge accent |

  "conv." = conventional: the loader recognizes the key and effectively defaults it; it is not a
  reproducible hard load error to omit it (no shipped skill does). Top-level (siblings of `interface:`,
  **not** under it): `policy.allow_implicit_invocation` (auto-invoke without a pick — ~112 skills),
  `dependencies.tools` (declare MCP deps — 27/760). anvil omits both (no MCP deps; don't want
  auto-claim). Corpus counts are point-in-time. ✓
- **Namespacing:** plugin skills are auto-prefixed `plugin_name:` → anvil's appear as
  `anvil:claim`, etc. **Do not** repeat "Anvil" in `display_name` (double-prefix). ✓
- **anvil:** 8 minimal files shipped this PR. Icons skipped (optional; avoids 8 duplicated
  binaries). `brand_color: "#60a5fa"` (reused from the repo's own skills diagram).
- **Source:** `~/.codex/skills/{pdf,playwright}/agents/openai.yaml`; loader strings in the binary.

### Hooks
- **What:** plugin lifecycle hooks using the **identical Claude Code `hooks.json` schema** (regex
  `matcher` on tool name; `command`/`timeout`). Codex **auto-discovers `hooks/hooks.json`** from the
  plugin root — no manifest key needed (a `hooks` key is supported but optional; Codex's own
  scaffolder emits `"hooks": "./hooks.json"` under `.codex-plugin/`). Every cached real plugin
  (handoff, fakoli-state, hookify, …) uses bare discovery. The runtime parser is strict: the root
  object of `hooks/hooks.json` must contain `hooks` only; top-level metadata such as `description`
  is rejected before hook review. ✓
- **Plugin-root var:** the binary honors **`${CLAUDE_PLUGIN_ROOT}`** (also `PLUGIN_ROOT`; **no**
  `CODEX_PLUGIN_ROOT`) — anvil's existing var is correct, no fallback needed. ✓
- **SessionStart output contract:** Codex expects SessionStart hooks that inject context to emit
  JSON on stdout, using `hookSpecificOutput.hookEventName="SessionStart"` and
  `hookSpecificOutput.additionalContext`. Plain-text stdout is rejected as invalid SessionStart
  JSON output. ✓
- **Events fired (0.130.0):** `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PermissionRequest`,
  `PostToolUse`, `PreCompact`, `PostCompact`, **`Stop`** ("right before Codex ends its turn"). Matcher
  semantics match Claude Code. **Not supported:** `SessionEnd`, `SubagentStart`/`SubagentStop`,
  `Notification`, `PostToolUseFailure`, `InstructionsLoaded` (the fakoli `hooks.schema.json` is a
  Claude *superset*, not Codex's runtime). ✓
- **Trust (the #1 gotcha):** plugin hooks are **non-managed — they silently no-op until trusted once.**
  On startup Codex shows *"N hook(s) need review before they can run. Open /hooks to review."*; the
  `/hooks` panel marks each "New hook — review required", and trust persists via a `trusted_hash` in
  `hooks.state` (re-prompts if the hook file changes; managed/config hooks are always on). Install +
  onboarding text MUST tell users to run `/hooks` and trust anvil's hooks. ✓
- **anvil:** SessionStart state-inject, PreToolUse claim-check, PostToolUse file-record + Bash
  evidence-capture all port via the shared root plugin. The manifest launches
  `uv run --quiet --project "${CLAUDE_PLUGIN_ROOT}/bin" python -m anvil.cli hook dispatch …`
  instead of `bash …/*.sh`, so Windows Codex does not resolve the System32/WSL `bash.exe`
  stub and hang. The legacy `.sh` hooks remain in the repo as a Claude-compatible
  fallback/test surface. **B41 (shipped):** a PostToolUse **lease heartbeat**
  (`anvil hook heartbeat`, wired, non-blocking) keeps a lazy lease fresh on tool activity.
- **B41 Stop-gate (OPT-IN, blocking).** `anvil hook stop-gate` is the Codex/Claude analogue of the
  OpenClaw finish-gate: on `Stop`, if a claimed task lacks submitted evidence it emits
  `{"decision":"block","reason":…}` + exit 2 to force a continuation. It is **NOT auto-wired** —
  anvil's bundled hooks are non-blocking by design (`docs/design.md`), and the Codex Stop-block
  mechanism is **unverified on codex-cli 0.130.0**. To enable: add a `Stop` hook running
  `anvil hook stop-gate` to your config, run `/hooks` to trust it, and **verify it blocks as
  expected before relying on it** (if exit-2 is treated as a hook error rather than a block signal,
  it could disrupt turns). Reuses `gate-check`'s decision logic; default-OPEN; loop-guarded via
  `stop_hook_active`.
- **Source:** root `hooks/hooks.json`; cached examples under `~/.codex/plugins/cache/*/hooks/hooks.json`;
  hook loader/trust strings in the `codex` binary.

### MCP servers
- **What:** `codex mcp add NAME -- CMD ARGS…` (stdio) or `--url URL` (HTTP); `codex mcp list|get|remove|login|logout`.
  Stored as `[mcp_servers.<NAME>]` in `config.toml` — stdio keys `command/args/env/cwd/startup_timeout_sec`;
  HTTP keys `url/bearer_token_env_var/http_headers`. ✓
- **anvil:** native, stable — `codex mcp add anvil -- uv run --quiet --project …/bin python -m anvil.mcp_server` (36 tools). The MCP
  is wired **out-of-band** from the plugin (not via the manifest's mcpServers). ✓
- **Source:** `codex mcp --help`; `~/.codex/config.toml` `[mcp_servers.*]`; <https://developers.openai.com/codex/mcp>.

### Config, models & profiles
- **What:** `~/.codex/config.toml` (TOML). Real top-level keys seen: `model` (`gpt-5.5`),
  `model_reasoning_effort` (`high`), `sandbox_mode`, `notify`, `[projects]`, `[marketplaces]`,
  `[plugins]`, `[features]`, `[memories]`, `[mcp_servers.*]`. **Profiles** `[profiles.NAME]` bundle
  model/sandbox/approval defaults, invoked `-p/--profile NAME`. ✓
- **Valid models / reasoning** (`models_cache.json`): `gpt-5.5`, `gpt-5.4`, `gpt-5.4-mini`,
  `gpt-5.3-codex-spark`, `codex-auto-review`; reasoning `low|medium|high|xhigh` (docs also list
  `minimal`). **Validate against the cache; don't hardcode.** ✓ (note: automation templates still
  say `gpt-5-codex`/`high` — likely stale, re-check).
- **`-c key=value`** overrides any config via dotted TOML path, highest precedence, parsed as TOML
  (literal-string fallback). All per-run config goes through this — **never text-edit config.toml.** ✓
- **Source:** `~/.codex/config.toml`, `~/.codex/models_cache.json`; config-reference URL above.

### Sandbox, approvals & trust
- **Sandbox** (`-s/--sandbox`): `read-only` · `workspace-write` · `danger-full-access`. ✓
- **Approval** (`-a`): `untrusted` · `on-request` · `never` · `on-failure` (deprecated);
  `--dangerously-bypass-approvals-and-sandbox` (alias `--yolo`) removes both. ✓
- **Trust:** `[projects]` → `"<path>" = { trust_level = "trusted" }`, granted on first-run prompt
  or `/permissions`. The anvil repo path is already trusted on this machine. ✓
- **anvil posture:** unattended runs → `workspace-write` with the worktree as cwd (+ `--add-dir`
  if the state dir is outside cwd); avoid `danger-full-access`/`--yolo` outside a dedicated VM.
- **Source:** `codex exec --help`; agent-approvals-security URL above.

### Automations (scheduled agent runs)
- **What:** `~/.codex/automations/<id>/automation.toml`. Keys: `version`, `id`, `kind="cron"`,
  `name`, `prompt`, `status`, `rrule`, `model`, `reasoning_effort`, `execution_environment`,
  `cwds` (array), `created_at`, `updated_at`. **Schedule = iCal RRULE** (e.g.
  `FREQ=WEEKLY;BYDAY=MO,WE,FR;BYHOUR=9;BYMINUTE=0`), not cron syntax. `status="PAUSED"` until the
  user activates. ✓
- **anvil:** ships `anvil-work-queue` + `anvil-sync-reconcile` PAUSED, namespaced per project
  (`anvil install codex --automations`). Each is an isolated dir — no shared state to corrupt.
- **▲ Skill sigil in an automation prompt** (`$claim`/`$execute`) is **documented but unconfirmed
  on this build** — no active automation uses it; skills must be marketplace-installed to resolve.
  **Smoke-test before rewriting prompts to drive skills by name.**
- **▲ Scheduler "local-vs-UTC double-fire"** is folklore in our brief (no upstream cite) — anvil's
  exclusive lease makes a double-fire idempotent regardless.
- **Source:** `packaging/codex/automations/*/automation.toml`; `~/.codex/automations/`.

### Headless runners — `codex exec` / `codex review`
- **`codex exec`** flags: `-s/--sandbox`, `-c key=value`, `-p/--profile`, `-m/--model`, `-C/--cd`,
  `--add-dir <DIR>`, `--output-schema <FILE>`, `--json` (JSONL events), `-o/--output-last-message <FILE>`,
  `--ephemeral`. No `-a` on `exec` (only `--dangerously-bypass-…`). ✓
- **`codex review`** flags: `--uncommitted`, `--base <BRANCH>`, `--commit <SHA>`, `--title`, `-c`
  (`--enable`/`--disable`). It has **no `--json` and no `-o`**. **`codex exec review`** is the
  capturable variant — it adds `-m/--model`, `--json`, and `-o/--output-last-message`. So to gate
  programmatically: run **`codex exec review`**, ask the prompt to end with a `VERDICT PASS|FAIL`
  line, and read the `-o`/`--json` output. Bare `codex review` must be parsed from stdout. ✓
- **▲ `--output-schema` may be silently ignored when MCP servers are active** (anvil's MCP is
  configured) — prefer calling the anvil CLI inside the run + parsing the last message. Verify live.
- **anvil:** B41 Phase 3 — `packaging/codex/loops/` (`anvil-exec-queue.sh`, `anvil-review-branch.sh`,
  `anvil-sync.sh`). Not built yet.
- **Source:** `codex exec --help`, `codex review --help`; noninteractive URL above.

### Sessions, resume & fork
- **Storage:** JSONL rollout per session at `~/.codex/sessions/YYYY/MM/DD/rollout-<ISO>-<UUID>.jsonl`;
  first line `session_meta` whose `payload.id` (= filename UUID) **is the session id**. Index at
  `~/.codex/session_index.jsonl` (`id`, `thread_name`, `updated_at`). ✓
- **Resume:** `codex resume [SESSION_ID|thread-name] [PROMPT]`, `--last`, `--all`; headless
  `codex exec resume [SESSION_ID|thread-name] [PROMPT] --json -o <FILE>`. ✓
- **Session id from `exec --json`:** the `thread.started` event carries **`thread_id`**. ✓
- **Fork:** `codex fork [SESSION_ID|--last]` — stable; forks a prior session into a new branch. ✓
- **▲ `--output-schema` + `resume`** appears unsupported together (`exec resume --help` omits the
  flag) — verify before relying on structured output across resumes.
- **anvil opportunity:** persist `thread_id` on a claim row → one claim ↔ one resumable session;
  `codex fork` maps one base session → N parallel agent branches.

### Cloud & apply (experimental)
- **`codex cloud`** `[EXPERIMENTAL]`: `exec` (`--env <ENV_ID>` **required**, `--branch`, `--attempts N`
  best-of-N), `status`, `list --json --limit 1-20 --cursor`, `apply`, `diff`. `cloud exec` needs a
  target environment id; environments are an **optional** customization layer (a default universal
  image exists), and a pushed remote branch is likely but unverified. ▲
- **`codex apply <TASK_ID>`** (alias `a`): `git apply` of the *latest diff produced by a Codex agent*
  for that task id (help wording — **not** explicitly Cloud-scoped in `--help`); `codex cloud apply
  <TASK_ID> [--attempt N]` is the cloud-scoped form. Cloud task ids appear to be a different namespace
  from local session UUIDs (plausible, not confirmed). ▲
- **anvil:** keep docs-only / opt-in (fights local-first; separate id namespace). Brief Phase 4.
- **Source:** `codex cloud --help`; cloud-environments URL above.

### App-server & remote-control (experimental)
- **What:** `codex app-server` and `codex remote-control` expose Codex over **JSON-RPC 2.0**
  (MCP-like, bidirectional; `thread/started` notification, identity via `thread.sessionId`).
  Transports: `stdio://` (default), `unix://`, `ws://IP:PORT` (experimental), `off`. Both
  `[experimental]`. Related: `codex exec-server`, `codex mcp-server` (Codex *as* an MCP server). ✓
- **anvil opportunity:** a single app-server as the headless orchestration backend with many thin
  agent clients — speculative, revisit when the surface stabilizes.
- **Source:** `codex app-server --help`; app-server URL above.

### Memory ("memories")
- **What:** Codex's **native durable cross-thread memory** — *not* a single growing transcript and
  *not* prompt-bounded. A structured, SQLite-backed background job pipeline. Markdown lives in
  `~/.codex/memories/` (`MEMORY.md` — structured `# Task Group` / `## User preferences` /
  `## Reusable knowledge` / `## Failures` — plus `raw_memories.md`, `memory_summary.md`,
  `rollout_summaries/*.md`); the store is **`~/.codex/memories_1.sqlite` at the CODEX_HOME root**
  (sibling of `memories/`, not inside it; tables `stage1_outputs`, `jobs`). Generated async in the
  background by Codex, secret-redacted; read back via `use_memories`. **Experimental, ON here.** ✓
- **Config:** `[features].memories`, `[memories] generate_memories/use_memories`. Inspect flags via
  `codex features list`.
- **anvil:** Codex's lossy, host-owned analog to anvil's authoritative cross-run state — informative,
  not a substitute. anvil should not depend on it.
- **Source:** `~/.codex/memories/`; <https://developers.openai.com/codex/memories>.

### Notify & instructions & prompts
- **notify** — a **single, exclusive** external program (`notify = ["prog","args…"]`) invoked async on
  events (documented: `agent-turn-complete`); JSON payload; **fire-and-forget, cannot block**. It is
  **already set on this machine** (Computer Use owns it). A plugin must **not** overwrite it — chain
  instead. Fine for a finish-gate ping, useless as a gate. ✓ <https://developers.openai.com/codex/config-advanced>
- **AGENTS.md layering** — directory-tree scoped; **more-deeply-nested wins**; a direct prompt beats
  all. Chain: `~/.codex/AGENTS(.override).md` (global) → git-root → … → cwd, concatenated root-first,
  later overrides earlier, capped at `project_doc_max_bytes` (32 KiB). Inspect live with
  `codex debug prompt-input`. anvil drops repo-root `AGENTS.md`. ✓ <https://developers.openai.com/codex/guides/agents-md>
- **Custom prompts / slash commands** — **deprecated in favor of skills**, user-home-only
  (`~/.codex/prompts/`), **not** repo/plugin/marketplace-distributable. Ship a `SKILL.md`, never a
  prompt file. ✗ <https://developers.openai.com/codex/custom-prompts>
- **Channels (Slack/Telegram/…)** — **not a Codex CLI feature.** The official Slack integration targets
  **Codex *cloud*** (`@Codex` → cloud task); `slack_*` strings in the binary are MCP connector tool
  titles, not a built-in channel. Native channels are an **OpenClaw** distinction, not Codex. So anvil
  can't use a Codex CLI channel for pings — use `notify` or an MCP connector. ✓

### Other subcommands (lower-priority, captured for completeness)
- `codex login`/`logout` (+ `login status`) — ChatGPT auth. `codex update` — self-update.
  `codex completion <shell>` — shell completions. `codex sandbox <macos|linux|windows> -- <CMD>` —
  run a command under the platform sandbox (takes a per-OS subcommand, not a bare command).
- `codex debug` → `prompt-input` (introspect the assembled instruction layers — handy for verifying
  AGENTS.md precedence), `models`, `app-server`. `codex features list|enable|disable` — the canonical
  feature-flag inspector. `codex mcp-server` / `codex exec-server` — Codex *as* a server.

---

## Hard constraints (do not violate)

1. **Never text-edit `~/.codex/config.toml`.** anvil corrupted it before (a
   `~/.codex/config.toml.corrupt-*` backup proves it — naive line-editing dropped the quotes on
   `[projects]`/`[plugins]` keys). All per-run config goes through `codex … -c key=value` flags or
   Codex's own `mcp add` / `marketplace add` commands.
2. **`codex review` has no `--json`/`-o`** — for a capturable verdict use `codex exec review` (which has
   both) and parse a `VERDICT PASS|FAIL` line; never promise structured JSON from bare `codex review`.
3. **Don't ship custom prompt files** (deprecated, user-home-only) or claim the global **`notify`** key.
4. **Validate model / reasoning_effort against `models_cache.json`** — don't hardcode `gpt-5-codex`.

## Verify-before-building (live smoke tests still owed)

- **Skill sigil** resolution inside an *active* automation run.
- **`--output-schema`** behavior with anvil's MCP server active (`exec`) and with `resume`.
- `medium` reasoning validity for the current model; whether automation templates' `gpt-5-codex` is stale.
- `codex exec review --json` verdict semantics.
