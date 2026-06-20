import { spawn } from "node:child_process";
import { unlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
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

interface AnvilResult {
  code: number | null;
  stdout: string;
  stderr: string;
  error?: Error;
}

function runAnvil(args: string[], cwd: string): Promise<AnvilResult> {
  return new Promise((resolve) => {
    let child;
    try {
      child = spawn("anvil", args, { cwd, env: process.env });
    } catch (error) {
      resolve({ code: null, stdout: "", stderr: "", error: error as Error });
      return;
    }
    let stdout = "";
    let stderr = "";
    child.stdout?.on("data", (chunk) => {
      stdout += chunk.toString();
    });
    child.stderr?.on("data", (chunk) => {
      stderr += chunk.toString();
    });
    child.on("error", (error) => resolve({ code: null, stdout, stderr, error }));
    child.on("close", (code) => resolve({ code, stdout, stderr }));
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
async function captureEvidence(
  command: string, exitCode: number, output: string, cwd: string,
): Promise<void> {
  const tmp = join(tmpdir(), `anvil-ev-${process.pid}-${Date.now()}.txt`);
  try {
    await writeFile(tmp, output, "utf8");
  } catch {
    return; // can't stage output — skip
  }
  await new Promise<void>((resolve) => {
    let child;
    try {
      child = spawn(
        "anvil",
        ["hook", "capture-evidence", "--command", command, "--exit-code", String(exitCode),
          "--stdout-file", tmp, "--actor", ANVIL_ACTOR, "--cwd", cwd],
        { cwd, env: process.env },
      );
    } catch {
      resolve();
      return;
    }
    child.on("error", () => resolve());
    child.on("close", () => resolve());
  });
  try {
    await unlink(tmp);
  } catch {
    /* best-effort cleanup */
  }
}

export default definePluginEntry({
  id: "anvil-finish-gate",
  name: "anvil finish-gate",
  description:
    "anvil's OpenClaw integration: blocks finalizing a turn while a claimed task lacks evidence (before_agent_finalize), and auto-captures verification-command output to the claim's evidence buffer (after_tool_call).",
  register(api) {
    // after_tool_call: auto-capture verification-command output as evidence. Pure
    // observer (fire-and-forget); only `exec` tools running a verification command.
    api.on("after_tool_call", async (event) => {
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
        await captureEvidence(command, exitCode, output, cwd);
      } catch (error) {
        api.logger?.debug?.(`anvil evidence-capture skipped: ${(error as Error)?.message}`);
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
