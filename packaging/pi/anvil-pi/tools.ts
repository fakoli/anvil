// anvil-pi — thin wrappers over the `anvil --json` CLI.
//
// Contract (plan A1 / F6): the extension speaks CLI, not MCP. Every tool runs
// `anvil <verb> ... --json` as a subprocess, parses --json output, truncates
// large payloads with truncateHead, and fails with the CLI's stderr verbatim.
//
// Verb policy: `anvil_run` is an escape hatch over an EXECUTION allowlist.
// Planning verbs require ANVIL_PI_PLANNING=1 (mirrors the MCP surface gate,
// mcp_server.py apply_surface_gate). Config-writing / state-restoring verbs
// are ALWAYS denied — no env flag reaches them.

import { truncateHead, DEFAULT_MAX_BYTES, DEFAULT_MAX_LINES } from "@earendil-works/pi-coding-agent";
import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { resolve as resolvePath } from "node:path";

/** Verbs any agent can run via anvil_run without the planning opt-in. */
export const EXECUTION_VERBS = new Set([
  "status",
  "next",
  "claim",
  "release",
  "renew",
  "packet",
  "submit",
  "apply",
  "describe",
  "doctor",
  "gate_check",
  "progress",
  "graph",
  "conflicts",
  "scan",
]);

/** Extra verbs allowed only when ANVIL_PI_PLANNING is truthy (1/true/yes/on). */
export const PLANNING_EXTRA_VERBS = new Set([
  "plan",
  "prd",
  "init",
  "score",
  "review",
  "assumptions",
  "deps",
  "expand",
  "list_tasks",
  "show",
  "bundle",
]);

/** Verbs that mutate configuration or restore state — denied even with the
 * planning gate. These are operator actions, not agent actions. */
export const ALWAYS_DENY_VERBS = new Set([
  "install",
  "mcp_config",
  "hooks",
  "restore",
  "migrate",
  "migrate_workspace",
  "migrate_events",
  "replay",
  "run_workflow",
  "backup",
]);

export function planningSurfaceEnabled(env: Record<string, string | undefined> = process.env): boolean {
  const raw = env.ANVIL_PI_PLANNING ?? "";
  return ["1", "true", "yes", "on"].includes(raw.trim().toLowerCase());
}

/** Validate a verb for anvil_run. Returns an error message or null. */
export function checkVerb(verb: string, env: Record<string, string | undefined> = process.env): string | null {
  if (!/^[a-z_]+$/.test(verb)) {
    return `invalid verb "${verb}"`;
  }
  if (ALWAYS_DENY_VERBS.has(verb)) {
    return `verb "${verb}" is always denied by the anvil-pi extension (config/state-mutating operator action)`;
  }
  if (EXECUTION_VERBS.has(verb)) return null;
  if (PLANNING_EXTRA_VERBS.has(verb)) {
    if (planningSurfaceEnabled(env)) return null;
    return `verb "${verb}" is a planning verb; set ANVIL_PI_PLANNING=1 to expose it (mirrors the anvil MCP planning gate)`;
  }
  return `verb "${verb}" is not in the anvil-pi allowlist (execution verbs: ${[...EXECUTION_VERBS].join(", ")}; planning verbs additionally with ANVIL_PI_PLANNING=1: ${[...PLANNING_EXTRA_VERBS].join(", ")})`;
}

/** Hard cap on the args array passed through anvil_run (defense in depth). */
export const MAX_RUN_ARGS = 32;
export const MAX_RUN_ARG_CHARS = 200;
export const MAX_RUN_ARGS_TOTAL_CHARS = 4000;

export interface AnvilCliResult {
  ok: boolean;
  stdout: string;
  stderr: string;
  exitCode: number;
}

export function anvilBin(env: Record<string, string | undefined> = process.env): string {
  return env.ANVIL_BIN ?? "anvil";
}

/**
 * Run `anvil <verb> [args...] --json`. Options-style args must be passed as
 * their own elements ("--actor", "alice"). The `--json` flag is appended and
 * cannot be overridden.
 */
export function runAnvil(verb: string, args: string[] = [], env: Record<string, string | undefined> = process.env, cwd?: string): AnvilCliResult {
  const check = checkVerb(verb, env);
  if (check) {
    return { ok: false, stdout: "", stderr: check, exitCode: -1 };
  }
  const cleanArgs = args.filter((a) => typeof a === "string" && a.length > 0);
  if (cleanArgs.length > MAX_RUN_ARGS) {
    return { ok: false, stdout: "", stderr: `too many args (${cleanArgs.length} > ${MAX_RUN_ARGS})`, exitCode: -1 };
  }
  if (cleanArgs.some((a) => a.length > MAX_RUN_ARG_CHARS) || cleanArgs.join(" ").length > MAX_RUN_ARGS_TOTAL_CHARS) {
    return { ok: false, stdout: "", stderr: "args exceed anvil_run size caps", exitCode: -1 };
  }
  const result = spawnSync(anvilBin(env), [verb, ...cleanArgs, "--json"], {
    encoding: "utf8",
    cwd,
    env: env as NodeJS.ProcessEnv,
    timeout: 120_000,
  });
  if (result.error) {
    return { ok: false, stdout: result.stdout ?? "", stderr: String(result.error.message ?? result.error), exitCode: -1 };
  }
  return {
    ok: result.status === 0,
    stdout: result.stdout ?? "",
    stderr: result.stderr ?? "",
    exitCode: result.status ?? -1,
  };
}

/** Max chars for tool output reaching the model (bounded, ~1.5k tokens). */
export const TOOL_OUTPUT_MAX_BYTES = 12_000;

/** Parse + truncate CLI stdout for tool results. */
export function presentResult(result: AnvilCliResult): { text: string; isError: boolean } {
  if (!result.ok) {
    const detail = result.stderr.trim() || `anvil exited ${result.exitCode}`;
    return { text: `anvil error: ${detail}`, isError: true };
  }
  const truncation = truncateHead(result.stdout, { maxLines: DEFAULT_MAX_LINES, maxBytes: TOOL_OUTPUT_MAX_BYTES });
  let text = truncation.content;
  if (truncation.truncated) {
    text += `\n[truncated: ${truncation.outputLines}/${truncation.totalLines} lines, ${truncation.outputBytes}/${truncation.totalBytes} bytes; rerun with narrower args or use anvil directly]`;
  }
  return { text, isError: false };
}

/** Does an anvil state root exist for the given cwd? (ANVIL_ROOT > .anvil/ in cwd) */
export function hasAnvilState(env: Record<string, string | undefined> = process.env, cwd?: string): boolean {
  if (env.ANVIL_ROOT) return true;
  return existsSync(resolvePath(cwd ?? process.cwd(), ".anvil"));
}

/** Bounded session-start snapshot: status JSON capped to ~6k chars (~1.5k tokens) total. */
export const SNAPSHOT_MAX_CHARS = 6000;

export function sessionSnapshot(env: Record<string, string | undefined> = process.env, cwd?: string): string | null {
  if (!hasAnvilState(env, cwd)) return null;
  const result = runAnvil("status", [], env, cwd);
  if (!result.ok) {
    return `anvil state detected but status failed: ${(result.stderr || `exit ${result.exitCode}`).trim()}`;
  }
  const combined = `[anvil session snapshot — project state at session start]\n${result.stdout}`;
  // The cap is on the WHOLE returned string (header included), not the body.
  return combined.length > SNAPSHOT_MAX_CHARS
    ? combined.slice(0, SNAPSHOT_MAX_CHARS - 1) + "…"
    : combined;
}