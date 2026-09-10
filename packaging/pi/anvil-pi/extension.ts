// anvil-pi — anvil state-layer integration for the pi coding agent.
//
// Registers coarse anvil_* tools (thin wrappers over `anvil --json`), the
// /anvil:* commands, and a bounded session-start snapshot when an anvil state
// root exists. Contract details: packaging/pi/anvil-pi/README.md and the M2
// plan (docs/plans/2026-09-10-pi-extension-and-sandbox-allowlist.md §A1).
//
// Tool naming uses the anvil_ prefix to avoid collisions with other
// extensions' tools. All CLI calls are async + abort-aware (tool-call
// cancellation terminates the child) and thread the session workspace
// (ctx.cwd) through every invocation — snapshots and mutations act on the
// SAME workspace. Output is bounded on every branch; silent modes (print /
// JSON) never notify or inject.
import { Type } from "@earendil-works/pi-ai";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import {
  checkVerb,
  isValidTaskId,
  presentResult,
  runAnvil,
  sessionSnapshot,
  tokenizeQuotedArgs,
  MAX_WRAPPER_ARG_CHARS,
  MAX_WRAPPER_ARGS_TOTAL_CHARS,
  type AnvilCliResult,
} from "./tools.js";

const WRAPPER_OPTS = { maxArgChars: MAX_WRAPPER_ARG_CHARS, maxTotalChars: MAX_WRAPPER_ARGS_TOTAL_CHARS };

function toolResult(result: AnvilCliResult) {
  const { text, isError } = presentResult(result);
  return { content: [{ type: "text" as const, text }], details: { isError, exitCode: result.exitCode } };
}

function invalidTaskId(taskId: string) {
  return toolResult({ ok: false, stdout: "", stderr: `invalid task id "${taskId}": must be 1-64 chars, no whitespace, no leading "-"`, exitCode: -1 });
}

const taskIdParam = Type.String({ description: "Anvil task ID, e.g. T001", minLength: 1, maxLength: 64 });

export default function (pi: ExtensionAPI): void {
  // --- tools ---------------------------------------------------------------

  pi.registerTool({
    name: "anvil_status",
    label: "Anvil status",
    description:
      "Read-only snapshot of anvil project + task + claim state. Runs `anvil status --json` in the session workspace. No arguments.",
    parameters: Type.Object({}),
    async execute(_toolCallId, _params, signal, _onUpdate, ctx) {
      return toolResult(await runAnvil("status", [], undefined, ctx.cwd, signal));
    },
  });

  pi.registerTool({
    name: "anvil_next",
    label: "Anvil next",
    description:
      "Next actionable work packet from anvil (respects claims, deps, gates). Runs `anvil next --json`. No arguments.",
    parameters: Type.Object({}),
    async execute(_toolCallId, _params, signal, _onUpdate, ctx) {
      return toolResult(await runAnvil("next", [], undefined, ctx.cwd, signal));
    },
  });

  pi.registerTool({
    name: "anvil_claim",
    label: "Anvil claim",
    description:
      "Claim an anvil task by ID (leases/heartbeats coordinate agents; file-conflict warnings are only silenced with force). Runs `anvil claim <id> --json`.",
    parameters: Type.Object({
      task_id: taskIdParam,
      actor: Type.Optional(Type.String({ description: "Claim actor override (defaults to ANVIL_ACTOR or derived identity)", maxLength: 128 })),
      lease_minutes: Type.Optional(Type.Number({ description: "Lease duration in minutes (overrides project config)", minimum: 1, maximum: 10080 })),
      force: Type.Optional(Type.Boolean({ description: "Silence file-conflict/dependency warnings (the claim proceeds either way)" })),
    }),
    async execute(_toolCallId, params, signal, _onUpdate, ctx) {
      if (!isValidTaskId(params.task_id)) return invalidTaskId(params.task_id);
      const args = [params.task_id];
      if (params.actor) args.push("--actor", params.actor);
      if (params.lease_minutes !== undefined) args.push("--lease", String(params.lease_minutes));
      if (params.force) args.push("--force");
      return toolResult(await runAnvil("claim", args, undefined, ctx.cwd, signal, WRAPPER_OPTS));
    },
  });

  pi.registerTool({
    name: "anvil_packet",
    label: "Anvil packet",
    description: "Fetch the full work packet for a claimed anvil task. Runs `anvil packet <id> --json`.",
    parameters: Type.Object({ task_id: taskIdParam }),
    async execute(_toolCallId, params, signal, _onUpdate, ctx) {
      if (!isValidTaskId(params.task_id)) return invalidTaskId(params.task_id);
      return toolResult(await runAnvil("packet", [params.task_id], undefined, ctx.cwd, signal, WRAPPER_OPTS));
    },
  });

  pi.registerTool({
    name: "anvil_submit",
    label: "Anvil submit",
    description:
      "Submit execution evidence for a claimed anvil task. Runs `anvil submit <id> --commands <cmds> --files-changed <files> --json`.",
    parameters: Type.Object({
      task_id: taskIdParam,
      commands: Type.Optional(Type.String({ description: "Validation commands to record, e.g. `pytest -q`", maxLength: 2000 })),
      files_changed: Type.Optional(Type.String({ description: "Space-separated files changed, e.g. `src/x.py docs/y.md`", maxLength: 2000 })),
    }),
    async execute(_toolCallId, params, signal, _onUpdate, ctx) {
      if (!isValidTaskId(params.task_id)) return invalidTaskId(params.task_id);
      const args = [params.task_id];
      if (params.commands) args.push("--commands", params.commands);
      if (params.files_changed) args.push("--files-changed", params.files_changed);
      return toolResult(await runAnvil("submit", args, undefined, ctx.cwd, signal, WRAPPER_OPTS));
    },
  });

  pi.registerTool({
    name: "anvil_apply",
    label: "Anvil apply",
    description: "Apply/accept an evidence-gated anvil task. Runs `anvil apply <id> --json`.",
    parameters: Type.Object({
      task_id: taskIdParam,
      force: Type.Optional(Type.Boolean({ description: "Override gate warnings where the CLI allows" })),
    }),
    async execute(_toolCallId, params, signal, _onUpdate, ctx) {
      if (!isValidTaskId(params.task_id)) return invalidTaskId(params.task_id);
      const args = [params.task_id];
      if (params.force) args.push("--force");
      return toolResult(await runAnvil("apply", args, undefined, ctx.cwd, signal, WRAPPER_OPTS));
    },
  });

  pi.registerTool({
    name: "anvil_run",
    label: "Anvil run",
    description:
      "Escape hatch: run any allowlisted `anvil <verb> [args...]` with --json appended. Execution verbs always allowed; planning verbs require ANVIL_PI_PLANNING=1; config/state-mutating operator verbs (install, mcp-config, hook, restore, migrate*, replay, run-workflow, backup) are always denied. Args must not contain --json or a bare -- separator.",
    parameters: Type.Object({
      verb: Type.String({ description: "Anvil CLI verb (registered Typer name, e.g. gate-check, claim-guard)", pattern: "^[a-z][a-z_-]*$", maxLength: 64 }),
      args: Type.Optional(Type.Array(Type.String({ maxLength: 200 }), { maxItems: 32, description: "Extra CLI args (options as separate elements: [\"--actor\", \"alice\"])" })),
    }),
    async execute(_toolCallId, params, signal, _onUpdate, ctx) {
      return toolResult(await runAnvil(params.verb, params.args ?? [], undefined, ctx.cwd, signal));
    },
  });

  // --- commands ------------------------------------------------------------

  pi.registerCommand("anvil:status", {
    description: "Anvil project/task/claim snapshot",
    handler: async (_args, ctx) => {
      if (!ctx.hasUI) return;
      const { text, isError } = presentResult(await runAnvil("status", [], undefined, ctx.cwd));
      ctx.ui.notify(text, isError ? "error" : "info");
    },
  });

  pi.registerCommand("anvil:next", {
    description: "Next actionable anvil packet",
    handler: async (_args, ctx) => {
      if (!ctx.hasUI) return;
      const { text, isError } = presentResult(await runAnvil("next", [], undefined, ctx.cwd));
      ctx.ui.notify(text, isError ? "error" : "info");
    },
  });

  pi.registerCommand("anvil:claim", {
    description: "Claim an anvil task: /anvil:claim T001",
    handler: async (args, ctx) => {
      if (!ctx.hasUI) return;
      const id = (args ?? "").trim();
      if (!id || !isValidTaskId(id)) {
        ctx.ui.notify(`usage: /anvil:claim <task-id> (invalid: "${id}")`, "warning");
        return;
      }
      const { text, isError } = presentResult(await runAnvil("claim", [id], undefined, ctx.cwd, undefined, WRAPPER_OPTS));
      ctx.ui.notify(text, isError ? "error" : "info");
    },
  });

  pi.registerCommand("anvil:submit", {
    description: "Submit evidence: /anvil:submit T001 --commands 'pytest -q'",
    handler: async (args, ctx) => {
      if (!ctx.hasUI) return;
      const trimmed = (args ?? "").trim();
      if (!trimmed) {
        ctx.ui.notify("usage: /anvil:submit <task-id> [--commands <cmds>] [--files-changed <files>]", "warning");
        return;
      }
      // Quote-aware tokenizer: '--commands "pytest -q"' survives verbatim;
      // unterminated quotes surface a usage error, never silent truncation.
      let tokens: string[];
      try {
        tokens = tokenizeQuotedArgs(trimmed);
      } catch (error) {
        ctx.ui.notify(`usage: ${(error as Error).message}`, "warning");
        return;
      }
      const [id, ...rest] = tokens;
      if (!isValidTaskId(id)) {
        ctx.ui.notify(`usage: /anvil:submit <task-id> (invalid: "${id}")`, "warning");
        return;
      }
      // Unknown args are rejected, not silently dropped.
      const known = new Set(["--commands", "--files-changed"]);
      const cmdArgs = [id];
      for (let i = 0; i < rest.length; i++) {
        const flag = rest[i];
        if (!known.has(flag)) {
          ctx.ui.notify(`usage: unknown argument "${flag}" (supported: --commands, --files-changed)`, "warning");
          return;
        }
        const value = rest[i + 1];
        if (value === undefined || value.startsWith("--")) {
          ctx.ui.notify(`usage: ${flag} requires a value`, "warning");
          return;
        }
        cmdArgs.push(flag, value);
        i++;
      }
      const { text, isError } = presentResult(await runAnvil("submit", cmdArgs, undefined, ctx.cwd, undefined, WRAPPER_OPTS));
      ctx.ui.notify(text, isError ? "error" : "info");
    },
  });

  // --- session snapshot ------------------------------------------------------

  let snapshotInjected = false;

  pi.on("before_agent_start", async (_event, ctx) => {
    try {
      // Bounded, once-per-session LLM injection when an anvil state root
      // exists; capped by tools.sessionSnapshot (~6k chars ≈ 1.5k tokens).
      // Silent modes (print/JSON) never inject AND never spawn the status
      // call. The flag is consumed only on a successful injection: a no-state
      // session start or a failed status call leaves the opportunity for the
      // next agent start.
      if (snapshotInjected || !ctx.hasUI) return;
      const snapshot = await sessionSnapshot(undefined, ctx.cwd ?? undefined);
      // Only SUCCESS snapshots inject; status failures surface to the
      // operator (session_start notify) but never enter LLM context, and do
      // not consume the once-per-session opportunity.
      if (!snapshot?.startsWith("[anvil session snapshot") || !ctx.hasUI) return;
      snapshotInjected = true;
      return { message: { customType: "anvil-pi.snapshot", content: snapshot, display: true } };
    } catch {
      // snapshot is best-effort; never block the agent start
    }
  });

  pi.on("session_start", async (_event, ctx) => {
    try {
      // Session reset: a NEW session on the same extension instance gets its
      // own snapshot opportunity (and its own operator notification).
      snapshotInjected = false;
      if (!ctx.hasUI) return;
      const snapshot = await sessionSnapshot(undefined, ctx.cwd ?? undefined);
      if (snapshot) ctx.ui.notify(snapshot, "info");
    } catch {
      // snapshot is best-effort; never block the session
    }
  });
}