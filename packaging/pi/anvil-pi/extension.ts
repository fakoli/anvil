// anvil-pi — anvil state-layer integration for the pi coding agent.
//
// Registers coarse anvil_* tools (thin wrappers over `anvil --json`), the
// /anvil:* commands, and a bounded session-start snapshot when an anvil state
// root exists. Contract details: packaging/pi/anvil-pi/README.md and the M2
// plan (docs/plans/2026-09-10-pi-extension-and-sandbox-allowlist.md §A1).
//
// Tool naming uses the anvil_ prefix to avoid collisions with other
// extensions' tools. Output is bounded (tools.ts caps); large payloads are
// truncated with truncateHead and the CLI stderr is surfaced verbatim on
// failure.
import { Type } from "@earendil-works/pi-ai";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import {
  checkVerb,
  presentResult,
  runAnvil,
  sessionSnapshot,
  type AnvilCliResult,
} from "./tools.js";

function toolResult(result: AnvilCliResult) {
  const { text, isError } = presentResult(result);
  return { content: [{ type: "text" as const, text }], details: { isError, exitCode: result.exitCode } };
}

const taskIdParam = Type.String({ description: "Anvil task ID, e.g. T001", minLength: 1, maxLength: 64 });

export default function (pi: ExtensionAPI): void {
  // --- tools ---------------------------------------------------------------

  pi.registerTool({
    name: "anvil_status",
    label: "Anvil status",
    description:
      "Read-only snapshot of anvil project + task + claim state. Runs `anvil status --json` in the current workspace. No arguments.",
    parameters: Type.Object({}),
    async execute(_toolCallId, _params, _signal, _onUpdate, _ctx) {
      return toolResult(runAnvil("status"));
    },
  });

  pi.registerTool({
    name: "anvil_next",
    label: "Anvil next",
    description:
      "Next actionable work packet from anvil (respects claims, deps, gates). Runs `anvil next --json`. No arguments.",
    parameters: Type.Object({}),
    async execute() {
      return toolResult(runAnvil("next"));
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
    async execute(_toolCallId, params) {
      const args = [params.task_id];
      if (params.actor) args.push("--actor", params.actor);
      if (params.lease_minutes !== undefined) args.push("--lease", String(params.lease_minutes));
      if (params.force) args.push("--force");
      return toolResult(runAnvil("claim", args));
    },
  });

  pi.registerTool({
    name: "anvil_packet",
    label: "Anvil packet",
    description: "Fetch the full work packet for a claimed anvil task. Runs `anvil packet <id> --json`.",
    parameters: Type.Object({ task_id: taskIdParam }),
    async execute(_toolCallId, params) {
      return toolResult(runAnvil("packet", [params.task_id]));
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
    async execute(_toolCallId, params) {
      const args = [params.task_id];
      if (params.commands) args.push("--commands", params.commands);
      if (params.files_changed) args.push("--files-changed", params.files_changed);
      return toolResult(runAnvil("submit", args));
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
    async execute(_toolCallId, params) {
      const args = [params.task_id];
      if (params.force) args.push("--force");
      return toolResult(runAnvil("apply", args));
    },
  });

  pi.registerTool({
    name: "anvil_run",
    label: "Anvil run",
    description:
      "Escape hatch: run any allowlisted `anvil <verb> [args...]` with --json appended. Execution verbs always allowed; planning verbs require ANVIL_PI_PLANNING=1; config/state-mutating operator verbs (install, restore, migrate, hooks, mcp_config, replay, run_workflow, backup) are always denied.",
    parameters: Type.Object({
      verb: Type.String({ description: "Anvil CLI verb, e.g. doctor, gate_check, progress", pattern: "^[a-z_]+$", maxLength: 64 }),
      args: Type.Optional(Type.Array(Type.String({ maxLength: 200 }), { maxItems: 32, description: "Extra CLI args (options as separate elements: [\"--actor\", \"alice\"])" })),
    }),
    async execute(_toolCallId, params) {
      return toolResult(runAnvil(params.verb, params.args ?? []));
    },
  });

  // --- commands ------------------------------------------------------------

  pi.registerCommand("anvil:status", {
    description: "Anvil project/task/claim snapshot",
    handler: async (_args, ctx) => {
      if (!ctx.hasUI) return;
      const { text, isError } = presentResult(runAnvil("status"));
      ctx.ui.notify(text, isError ? "error" : "info");
    },
  });

  pi.registerCommand("anvil:next", {
    description: "Next actionable anvil packet",
    handler: async (_args, ctx) => {
      if (!ctx.hasUI) return;
      const { text, isError } = presentResult(runAnvil("next"));
      ctx.ui.notify(text, isError ? "error" : "info");
    },
  });

  pi.registerCommand("anvil:claim", {
    description: "Claim an anvil task: /anvil:claim T001",
    handler: async (args, ctx) => {
      if (!ctx.hasUI) return;
      const id = (args ?? "").trim();
      if (!id) {
        ctx.ui.notify("usage: /anvil:claim <task-id>", "warning");
        return;
      }
      const verbCheck = checkVerb("claim");
      const { text, isError } = presentResult(verbCheck === null ? runAnvil("claim", [id]) : { ok: false, stdout: "", stderr: verbCheck, exitCode: -1 });
      ctx.ui.notify(text, isError ? "error" : "info");
    },
  });

  pi.registerCommand("anvil:submit", {
    description: "Submit evidence: /anvil:submit T001 --commands 'pytest -q'",
    handler: async (args, ctx) => {
      if (!ctx.hasUI) return;
      const trimmed = (args ?? "").trim();
      const id = trimmed.split(/\s+/)[0] ?? "";
      if (!id) {
        ctx.ui.notify("usage: /anvil:submit <task-id> [--commands <cmds>] [--files-changed <files>]", "warning");
        return;
      }
      const rest = trimmed.slice(id.length).trim();
      const commands = /--commands\s+([^\s]+)/.exec(rest)?.[1];
      const files = /--files-changed\s+([^\s]+)/.exec(rest)?.[1];
      const cmdArgs = [id];
      if (commands) cmdArgs.push("--commands", commands);
      if (files) cmdArgs.push("--files-changed", files);
      const { text, isError } = presentResult(runAnvil("submit", cmdArgs));
      ctx.ui.notify(text, isError ? "error" : "info");
    },
  });

  // --- session snapshot ------------------------------------------------------

  let snapshotInjected = false;

  pi.on("before_agent_start", async (_event, ctx) => {
    try {
      // Bounded, once-per-session LLM injection when an anvil state root
      // exists; capped by tools.sessionSnapshot (~6k chars ≈ 1.5k tokens).
      // Zero cost outside anvil projects.
      if (snapshotInjected) return;
      snapshotInjected = true;
      const snapshot = sessionSnapshot(undefined, ctx.cwd ?? undefined);
      if (!snapshot) return;
      return { message: { customType: "anvil-pi.snapshot", content: snapshot, display: true } };
    } catch {
      // snapshot is best-effort; never block the agent start
    }
  });

  pi.on("session_start", async (_event, ctx) => {
    try {
      // Operator orientation only (UI, not LLM context): the model can pull
      // the same data on demand via anvil_status.
      if (!ctx.hasUI) return;
      const snapshot = sessionSnapshot(undefined, ctx.cwd ?? undefined);
      if (snapshot) ctx.ui.notify(snapshot, "info");
    } catch {
      // snapshot is best-effort; never block the session
    }
  });
}