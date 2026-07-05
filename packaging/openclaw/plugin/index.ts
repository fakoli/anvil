import { spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import { unlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, isAbsolute, join } from "node:path";
import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";

/**
 * anvil finish-gate — B42 Phase 2, anvil's first native OpenClaw blocking gate.
 *
 * On `before_agent_finalize` (the agent about to end its turn with a natural
 * final answer) this shells out to the python `anvil` CLI:
 *
 *     anvil gate-check --json --actor <actor>   (cwd = event.cwd)
 *
 * If the actor holds an active claim whose task lacks submitted verification
 * evidence, anvil returns block:true and the hook asks the harness for one more
 * model pass (action:"revise") carrying anvil's instruction. Stronger than
 * Claude Code's anvil hooks, which are non-blocking by design.
 *
 * Contracts honored:
 *  - anvil writes NOTHING for OpenClaw — `gate-check` only reads state.db.
 *  - DEFAULT-OPEN: any error / no cwd / anvil-missing / no-claim => continue.
 *    A finalize is never crashed and never falsely blocked.
 *  - BOUNDED: the block is returned via `retry` (idempotencyKey task:runId,
 *    maxAttempts 3) so the harness auto-continues after the budget — no loop.
 *  - allowConversationAccess: this hook only fires when the user sets
 *    plugins.entries.anvil-finish-gate.hooks.allowConversationAccess=true in the
 *    gateway config (see packaging/openclaw/README.md install recipe).
 */

// The identity to gate. Must match the actor anvil claims under in this harness;
// MCP/agent claims default to "agent". Override via env if your setup differs.
const ANVIL_ACTOR = process.env.ANVIL_GATE_ACTOR || "agent";
const MAX_ATTEMPTS = 3;

// Every anvil invocation is bounded by a timeout: these hooks run inside the
// agent loop (before_agent_finalize and before_prompt_build are AWAITED), so a
// hung `anvil` (locked sqlite, stalled IO) must never block the turn forever.
const ANVIL_TIMEOUT_MS = 5000;

interface AnvilResult {
  code: number | null;
  stdout: string;
  stderr: string;
  error?: Error;
}

/** Spawn `anvil <args>` in `cwd`, bounded by ANVIL_TIMEOUT_MS. On timeout the
 *  child is killed and resolved with an error (callers default-open on error).
 *  `ignoreOutput` uses stdio:"ignore" when the caller only needs the exit code
 *  (avoids unconsumed pipes filling and blocking the child). Never rejects. */
function runAnvil(
  args: string[], cwd: string, opts?: { ignoreOutput?: boolean },
): Promise<AnvilResult> {
  return new Promise((resolve) => {
    let child;
    try {
      child = spawn("anvil", args, {
        cwd, env: process.env, ...(opts?.ignoreOutput ? { stdio: "ignore" } : {}),
      });
    } catch (error) {
      resolve({ code: null, stdout: "", stderr: "", error: error as Error });
      return;
    }
    let stdout = "";
    let stderr = "";
    let settled = false;
    const finish = (result: AnvilResult) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      try {
        child.kill();
      } catch {
        /* already exited */
      }
      resolve(result);
    };
    const timer = setTimeout(
      () => finish({ code: null, stdout, stderr, error: new Error("anvil timed out") }),
      ANVIL_TIMEOUT_MS,
    );
    child.stdout?.on("data", (chunk) => {
      stdout += chunk.toString();
    });
    child.stderr?.on("data", (chunk) => {
      stderr += chunk.toString();
    });
    child.on("error", (error) => finish({ code: null, stdout, stderr, error }));
    child.on("close", (code) => finish({ code, stdout, stderr }));
  });
}

/** Parse the --json envelope. anvil emits exactly one JSON line, but tolerate a
 *  wrapper preamble (scan for the last JSON-looking line) and a pretty-printed
 *  multi-line blob (whole-string fallback). Returns null if nothing parses. */
function parseEnvelope(stdout: string): { ok?: boolean; data?: Record<string, unknown> } | null {
  const lines = stdout.split("\n").map((l) => l.trim()).filter(Boolean);
  for (let i = lines.length - 1; i >= 0; i--) {
    if (lines[i].startsWith("{")) {
      try {
        return JSON.parse(lines[i]);
      } catch {
        /* keep scanning earlier lines */
      }
    }
  }
  // Fallback: a multi-line / pretty-printed envelope.
  try {
    return JSON.parse(stdout.trim());
  } catch {
    return null;
  }
}

// Only these commands are worth capturing as evidence — mirrors
// hooks/capture-evidence.sh's VERIFICATION_PATTERNS so the OpenClaw and
// Claude-Code capture paths agree on what counts.
const VERIFICATION_PATTERNS = ["pytest", "ruff check", "mypy", "npm test", "cargo test", "bun test"];

function isVerificationCommand(command: string): boolean {
  return VERIFICATION_PATTERNS.some((p) => command.includes(p));
}

/** Shape of the OpenClaw `exec` tool's after_tool_call result (verified against
 *  ~/openclaw-node bash-tools.exec-types.ts: exitCode is numeric, stdout+stderr
 *  are COMBINED in `aggregated`, no separate streams). */
interface ExecResult {
  details?: { exitCode?: number | null; aggregated?: string; cwd?: string };
}

/** Forward one exec capture to the existing `anvil hook capture-evidence` verb
 *  (single source of truth for the evidence-buffer format). Best-effort: writes
 *  the combined output to a temp file, spawns anvil, cleans up. Never throws. */
// `anvil hook capture-evidence` only reads the first 4000 chars of the stdout
// file, so cap what we stage to avoid writing megabytes of build log to disk.
const MAX_OUTPUT_CHARS = 4000;

async function captureEvidence(
  command: string, exitCode: number, output: string, cwd: string,
): Promise<void> {
  // Unpredictable name + 0600 perms: verification output can be sensitive and
  // lives in a world-readable tmp dir.
  const tmp = join(tmpdir(), `anvil-ev-${process.pid}-${randomBytes(8).toString("hex")}.txt`);
  try {
    await writeFile(tmp, output.slice(0, MAX_OUTPUT_CHARS), { encoding: "utf8", mode: 0o600 });
  } catch {
    return; // can't stage output — skip
  }
  // runAnvil bounds this by ANVIL_TIMEOUT_MS (kills a hung child) and uses
  // stdio:"ignore" so the unconsumed pipe can't block the child.
  await runAnvil(
    ["hook", "capture-evidence", "--command", command, "--exit-code", String(exitCode),
      "--stdout-file", tmp, "--actor", ANVIL_ACTOR, "--cwd", cwd],
    cwd, { ignoreOutput: true },
  );
  try {
    await unlink(tmp);
  } catch {
    /* best-effort cleanup */
  }
}

// Cacheable static guidance injected into the system prompt for anvil-tracked
// projects: primes the agent to use anvil and warns about the finish-gate. Static
// (same for every anvil project) so it rides the provider's prompt cache.
const ANVIL_GUIDANCE =
  "[anvil] This project is tracked by anvil. Pick up work with `anvil next` then " +
  "`anvil claim <task-id>`; inspect state with `anvil status` / `anvil list`. Before " +
  "ending your turn on a claimed task, run its verification commands and submit " +
  "evidence (`anvil submit <task-id>`) — anvil's finish-gate will otherwise block " +
  "finalization.";

// Memoize the per-workspace decision so before_prompt_build (which fires every
// turn and is awaited) costs at most ONE `anvil status` probe per session — not a
// shell-out on every prompt build.
const guidanceCache = new Map<string, string>();

/** Return the guidance to inject for a workspace, or "" if it is not an anvil
 *  project. Memoized; probes `anvil status --json` once (exit 0 ⇒ tracked). */
async function anvilGuidanceFor(workspaceDir: string): Promise<string> {
  const cached = guidanceCache.get(workspaceDir);
  if (cached !== undefined) return cached;
  // Timeout-bounded probe (runAnvil kills a hung child) so a stalled `anvil
  // status` can't block prompt assembly forever. exit 0 ⇒ tracked anvil project.
  const probe = await runAnvil(["status", "--json", "--cwd", workspaceDir], workspaceDir, {
    ignoreOutput: true,
  });
  const guidance = probe.code === 0 ? ANVIL_GUIDANCE : "";
  guidanceCache.set(workspaceDir, guidance);
  return guidance;
}

interface PromptBuildContext {
  workspaceDir?: string;
  sessionKey?: string;
  sessionId?: string;
}

interface ToolContext {
  sessionKey?: string;
  sessionId?: string;
}

// --- claim-guard (before_tool_call) -----------------------------------------
// before_tool_call's event/ctx carry NO cwd, so we cannot locate the anvil
// project from the tool call alone. before_prompt_build (which fires earlier in
// the turn) DOES get ctx.workspaceDir — cache it here keyed by session so the
// guard can scope `anvil claim-guard` to the right project.
const workspaceBySession = new Map<string, string>();

function sessionKeyOf(ctx: ToolContext | PromptBuildContext | undefined): string {
  return ctx?.sessionKey || ctx?.sessionId || "";
}

// Tight allowlist (NOT openclaw's broad isMutatingToolCall): file-mutating tools
// are always guarded; exec/bash only when guardExec is on (no command parser, so
// gating all exec would false-warn on read-only shell).
const FILE_TOOLS = new Set(["write", "edit", "apply_patch"]);
const EXEC_TOOLS = new Set(["exec", "bash"]);
const FILE_PARAM_KEYS = ["path", "file_path", "filePath", "filepath", "file"];

function filesForToolCall(event: { params?: Record<string, unknown>; derivedPaths?: readonly string[] }): string[] {
  const out = new Set<string>();
  const params = event.params ?? {};
  for (const key of FILE_PARAM_KEYS) {
    const v = params[key];
    if (typeof v === "string" && v) out.add(v);
  }
  // apply_patch carries only an opaque patch string in params; the host's
  // best-effort destination hints live in derivedPaths.
  for (const p of event.derivedPaths ?? []) {
    if (typeof p === "string" && p) out.add(p);
  }
  return [...out];
}

/** Resolve the anvil project cwd for a tool call: the session's cached
 *  workspaceDir (from before_prompt_build), else the dir of any absolute edited
 *  path, else "" (⇒ guard allows — cannot scope). */
function cwdForToolCall(event: { params?: Record<string, unknown>; derivedPaths?: readonly string[] }, ctx: ToolContext | undefined, files: string[]): string {
  const key = sessionKeyOf(ctx);
  if (key) {
    const cached = workspaceBySession.get(key);
    if (cached) return cached;
  }
  for (const f of files) {
    if (isAbsolute(f)) return dirname(f);
  }
  return "";
}

type GuardMode = "off" | "warn" | "require_approval" | "block";

export default definePluginEntry({
  id: "anvil-finish-gate",
  name: "anvil finish-gate",
  description:
    "anvil's OpenClaw integration: blocks finalizing a turn while a claimed task lacks evidence (before_agent_finalize), auto-captures verification-command output to the claim's evidence buffer (after_tool_call), and injects anvil usage guidance into the system prompt for tracked projects (before_prompt_build).",
  register(api) {
    // claim-guard mode (default "warn" — the unanimous safe default; never
    // false-blocks). Precedence ENV → per-plugin config → "warn": env wins so an
    // explicit ANVIL_CLAIM_GUARD_MODE is never shadowed by an injected schema
    // default in pluginConfig. Invalid values fall through (no silent surprise).
    const cfg = ((api as { pluginConfig?: Record<string, unknown> }).pluginConfig) ?? {};
    const asMode = (v: unknown): GuardMode | undefined =>
      (["off", "warn", "require_approval", "block"].includes(String(v)) ? (v as GuardMode) : undefined);
    const CLAIM_GUARD_MODE: GuardMode =
      asMode(process.env.ANVIL_CLAIM_GUARD_MODE) ?? asMode(cfg.claimGuardMode) ?? "warn";
    const GUARD_EXEC = cfg.guardExec === true || process.env.ANVIL_GUARD_EXEC === "true";

    // Propagate the risk-ceiling / PRD-scope config to the `anvil next` the agent
    // runs (and any anvil subprocess), via the env vars those commands read
    // ($ANVIL_MAX_BLAST / $ANVIL_MAX_REVIEW_RISK / $ANVIL_PRD — the same envvar the
    // `--prd` flag honors). Same precedence as the guard mode above: an explicit
    // ambient env var always wins over the plugin config, so an operator override
    // is never shadowed by a config default. Absent knobs export nothing (no
    // ceiling / all-PRDs), so existing installs are unaffected.
    const exportIfUnset = (key: string, value: unknown): void => {
      if (
        process.env[key] === undefined &&
        (typeof value === "number" || (typeof value === "string" && value !== ""))
      ) {
        process.env[key] = String(value);
      }
    };
    exportIfUnset("ANVIL_MAX_BLAST", cfg.maxBlast);
    exportIfUnset("ANVIL_MAX_REVIEW_RISK", cfg.maxReviewRisk);
    exportIfUnset("ANVIL_PRD", cfg.activePrd);

    // after_tool_call: auto-capture verification-command output as evidence. Pure
    // observer (fire-and-forget); only `exec` tools running a verification command.
    api.on("after_tool_call", (event) => {
      try {
        if (event.toolName !== "exec") return;
        const command =
          typeof event.params?.command === "string" ? event.params.command : "";
        if (!command || !isVerificationCommand(command)) return;
        const details = (event.result as ExecResult | undefined)?.details;
        const cwd = typeof details?.cwd === "string" ? details.cwd : "";
        if (!cwd) return; // can't scope to an anvil project without a cwd
        const exitCode = typeof details?.exitCode === "number" ? details.exitCode : -1;
        const output = typeof details?.aggregated === "string" ? details.aggregated : "";
        // Fire-and-forget: do NOT await — return immediately so the hook never
        // adds latency to tool completion. captureEvidence swallows its own errors.
        void captureEvidence(command, exitCode, output, cwd);
      } catch (error) {
        api.logger?.debug?.(`anvil evidence-capture skipped: ${(error as Error)?.message}`);
      }
    });

    // before_prompt_build: inject cacheable anvil usage guidance into the system
    // prompt for anvil-tracked projects. Non-blocking by nature; returns {} (no
    // injection) for non-anvil projects or any error.
    api.on("before_prompt_build", async (_event, ctx) => {
      try {
        const pctx = ctx as PromptBuildContext;
        const workspaceDir = typeof pctx?.workspaceDir === "string" ? pctx.workspaceDir : "";
        if (!workspaceDir) return {};
        // Cache for the claim-guard (before_tool_call has no cwd of its own).
        const key = sessionKeyOf(pctx);
        if (key) {
          // Bound growth in a long-lived gateway (sessions accumulate).
          if (workspaceBySession.size >= 1000) workspaceBySession.clear();
          workspaceBySession.set(key, workspaceDir);
        }
        const guidance = await anvilGuidanceFor(workspaceDir);
        return guidance ? { prependSystemContext: guidance } : {};
      } catch {
        return {};
      }
    });

    // before_tool_call: claim-guard. When a MUTATING tool runs with no active
    // anvil claim, warn (default) / requireApproval / hard-block per mode. MUST
    // never throw (the host fails CLOSED on a thrown handler) and default-OPEN on
    // every uncertain path. Tight allowlist + early-return keep it off the hot path.
    api.on("before_tool_call", async (event, ctx) => {
      try {
        if (CLAIM_GUARD_MODE === "off") return {};
        const name = String(event.toolName ?? "").toLowerCase();
        const isFile = FILE_TOOLS.has(name);
        const isExec = EXEC_TOOLS.has(name);
        if (!isFile && !(GUARD_EXEC && isExec)) return {}; // not guarded → no shell-out
        const files = filesForToolCall(event);
        const cwd = cwdForToolCall(event, ctx as ToolContext, files);
        if (!cwd) return {}; // cannot scope to a project → allow
        // If before_prompt_build already probed this workspace as NON-anvil (cached
        // ""), skip the per-tool-call shell-out entirely.
        if (guidanceCache.get(cwd) === "") return {};

        const args = ["claim-guard", "--json", "--actor", ANVIL_ACTOR, "--cwd", cwd];
        for (const f of files) args.push("--file", f);
        const res = await runAnvil(args, cwd);
        // anvil missing / timeout / genuine error (exit 1) → allow.
        if (res.error || (res.code !== 0 && res.code !== 2)) return {};
        const env = parseEnvelope(res.stdout);
        const data = env?.ok ? env.data : null;
        if (!data) return {};
        const action = data.action;
        const reason = String(data.reason ?? "Claim an anvil task before editing.");

        if (action === "block") {
          // has-NO-claim. Escalation (block / requireApproval) is allowed ONLY for
          // file-mutating tools. exec/bash NEVER escalate — they carry no file to
          // protect, and hard-blocking arbitrary commands would also block
          // `anvil next`/`anvil claim` (a claim-acquisition deadlock). exec → warn.
          if (isFile && CLAIM_GUARD_MODE === "block") {
            api.logger?.info?.(`anvil claim-guard: blocking ${name} — ${reason}`);
            return { block: true, blockReason: reason };
          }
          if (isFile && CLAIM_GUARD_MODE === "require_approval") {
            return {
              requireApproval: {
                title: "anvil claim-guard",
                description: reason,
                severity: "warning" as const,
                timeoutBehavior: "allow" as const,
                timeoutMs: 30000,
                pluginId: "anvil-finish-gate",
              },
            };
          }
          // warn (default), or any exec tool: log only, allow. before_tool_call
          // can't inject text — the agent is nudged by before_prompt_build instead.
          api.logger?.warn?.(`anvil claim-guard: ${name} with no active claim — ${reason}`);
          return {};
        }
        if (action === "warn") {
          // edit-outside-scope is advisory in every mode — log, never block.
          api.logger?.info?.(`anvil claim-guard: ${reason}`);
          return {};
        }
        return {}; // continue
      } catch (error) {
        api.logger?.debug?.(`anvil claim-guard skipped: ${(error as Error)?.message}`);
        return {}; // MUST allow — host fails closed on a thrown handler
      }
    });

    api.on("before_agent_finalize", async (event) => {
      // Already revising on this stop cycle: honor the harness budget, don't re-block.
      if (event.stopHookActive) return { action: "continue" as const };
      // No cwd => cannot scope to an anvil project => cannot gate.
      if (!event.cwd) return { action: "continue" as const };

      // Pass --cwd explicitly: it wins precedence over a Gateway-level ANVIL_ROOT
      // in anvil's state resolver, so the gate always judges the agent's actual
      // project (not a stray env var's project). This is the no-false-block fix.
      const res = await runAnvil(
        ["gate-check", "--json", "--actor", ANVIL_ACTOR, "--cwd", event.cwd],
        event.cwd,
      );

      // anvil missing / spawn error / genuine error exit (1, e.g. bad ANVIL_ROOT)
      // => never block. Only 0 (continue) and 2 (block) are gate signals.
      if (res.error || (res.code !== 0 && res.code !== 2)) {
        const why = res.error ? res.error.message : `code=${res.code}`;
        const tail = res.stderr ? ` stderr=${res.stderr.trim().slice(-300)}` : "";
        api.logger?.debug?.(`anvil finish-gate: gate-check unavailable (${why}); allowing finalize.${tail}`);
        return { action: "continue" as const };
      }

      const env = parseEnvelope(res.stdout);
      if (!env?.ok || !env.data?.block) return { action: "continue" as const };

      const data = env.data;
      const task = typeof data.task === "string" ? data.task : "unknown";
      const runId = event.runId ?? event.sessionId ?? "unknown-run";
      api.logger?.info?.(`anvil finish-gate: blocking finalize — task ${task} lacks verification evidence`);

      return {
        action: "revise" as const,
        retry: {
          instruction: String(data.instruction ?? "Finish your claimed anvil task and submit evidence before ending the turn."),
          idempotencyKey: `anvil-finish-gate:${task}:${runId}`,
          maxAttempts: MAX_ATTEMPTS,
        },
      };
    });
  },
});
