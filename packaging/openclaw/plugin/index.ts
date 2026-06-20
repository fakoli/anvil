import { spawn } from "node:child_process";
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
  error?: Error;
}

function runAnvil(args: string[], cwd: string): Promise<AnvilResult> {
  return new Promise((resolve) => {
    let child;
    try {
      child = spawn("anvil", args, { cwd, env: process.env });
    } catch (error) {
      resolve({ code: null, stdout: "", error: error as Error });
      return;
    }
    let stdout = "";
    child.stdout?.on("data", (chunk) => {
      stdout += chunk.toString();
    });
    child.on("error", (error) => resolve({ code: null, stdout, error }));
    child.on("close", (code) => resolve({ code, stdout }));
  });
}

/** Parse the last JSON-looking line — the --json envelope is one line; tolerate
 *  any preamble a wrapper might emit. Returns null if nothing parses. */
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
  return null;
}

export default definePluginEntry({
  id: "anvil-finish-gate",
  name: "anvil finish-gate",
  description:
    "Blocks an agent from finalizing a turn while its claimed anvil task lacks submitted verification evidence.",
  register(api) {
    api.on("before_agent_finalize", async (event) => {
      // Already revising on this stop cycle: honor the harness budget, don't re-block.
      if (event.stopHookActive) return { action: "continue" as const };
      // No cwd => cannot scope to an anvil project => cannot gate.
      if (!event.cwd) return { action: "continue" as const };

      const res = await runAnvil(["gate-check", "--json", "--actor", ANVIL_ACTOR], event.cwd);

      // anvil missing / spawn error / genuine error exit (1, e.g. bad ANVIL_ROOT)
      // => never block. Only 0 (continue) and 2 (block) are gate signals.
      if (res.error || (res.code !== 0 && res.code !== 2)) {
        api.logger?.debug?.(`anvil finish-gate: gate-check unavailable (code=${res.code}); allowing finalize`);
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
