// anvil-pi — thin wrappers over the `anvil --json` CLI.
//
// Contract (plan A1 / F6): the extension speaks CLI, not MCP. Every tool runs
// `anvil <verb> ... --json` as an async subprocess (abort-aware, output
// capture capped), truncates large payloads with truncateHead, and fails with
// the CLI's stderr verbatim (error output is capped too).
//
// Verb policy: `anvil_run` is an escape hatch over an EXECUTION allowlist.
// Planning verbs require ANVIL_PI_PLANNING=1 (mirrors the MCP surface gate,
// mcp_server.py apply_surface_gate). Config-writing / state-restoring verbs
// are ALWAYS denied — no env flag reaches them. Verb names are the REGISTERED
// Typer names from bin/src/anvil/cli/__init__.py (hyphens included), not the
// Python function names.

import { truncateHead, DEFAULT_MAX_LINES } from "@earendil-works/pi-coding-agent";
import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { resolve as resolvePath } from "node:path";

/** Verbs any agent can run via anvil_run without the planning opt-in.
 * Read-only/coordination surface; names match `anvil --help`. */
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
  "gate-check",
  "progress",
  "graph",
  "conflicts",
  "scan",
  "drift",
  "claim-guard",
  "merge-check",
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
  "list",
  "show",
  "bundle",
]);

/** Verbs that mutate configuration or restore state — denied even with the
 * planning gate. These are operator actions, not agent actions. */
export const ALWAYS_DENY_VERBS = new Set([
  "install",
  "mcp-config",
  "hook",
  "restore",
  "migrate",
  "migrate-workspace",
  "migrate-events",
  "replay",
  "run-workflow",
  "backup",
]);

/** Verb groups not yet audited for side effects — fail closed until an audit
 * classifies them (notify-digest sends notifications; sync/proof/project are
 * multi-command groups). */
export const UNCLASSIFIED_VERBS = new Set(["sync", "proof", "project", "notify-digest"]);

export function planningSurfaceEnabled(env: Record<string, string | undefined> = process.env): boolean {
  const raw = env.ANVIL_PI_PLANNING ?? "";
  return ["1", "true", "yes", "on"].includes(raw.trim().toLowerCase());
}

/** Validate a verb for anvil_run. Returns an error message or null. */
export function checkVerb(verb: string, env: Record<string, string | undefined> = process.env): string | null {
  if (!/^[a-z][a-z_-]*$/.test(verb)) {
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
  if (UNCLASSIFIED_VERBS.has(verb)) {
    return `verb "${verb}" is not yet classified for agent use by the anvil-pi extension (fail closed)`;
  }
  return `verb "${verb}" is not in the anvil-pi allowlist (execution verbs: ${[...EXECUTION_VERBS].join(", ")}; planning verbs additionally with ANVIL_PI_PLANNING=1: ${[...PLANNING_EXTRA_VERBS].join(", ")})`;
}

/** anvil_run (escape hatch) arg caps — stricter than the structured wrappers. */
export const MAX_RUN_ARGS = 32;
export const MAX_RUN_ARG_CHARS = 200;
export const MAX_RUN_ARGS_TOTAL_CHARS = 4000;

/** Structured-wrapper caps (submit/commands payloads are long by design). */
export const MAX_WRAPPER_ARG_CHARS = 2000;
export const MAX_WRAPPER_ARGS_TOTAL_CHARS = 8000;

export interface AnvilRunOptions {
  maxArgChars?: number;
  maxTotalChars?: number;
  timeoutMs?: number;
  /**
   * Append `--json` (default true). Verbs whose JSON output uses a different
   * flag must pass false and carry the flag in args — e.g. `packet` exposes
   * JSON via `--format json` and REJECTS `--json` (dogfood-caused, M4).
   */
  json?: boolean;
}

export interface AnvilCliResult {
  ok: boolean;
  stdout: string;
  stderr: string;
  exitCode: number;
}

export function anvilBin(env: Record<string, string | undefined> = process.env): string {
  return env.ANVIL_BIN ?? "anvil";
}

/** Hard cap on captured subprocess output (per stream) — a runaway CLI can
 * never balloon the extension's memory. */
const CAPTURE_MAX_BYTES = 1_000_000;

/**
 * Run `anvil <verb> [args...] --json` asynchronously. The task does NOT block
 * the host event loop; `signal` (a tool-call cancellation) terminates the
 * child. Options-style args must be passed as their own elements
 * ("--actor", "alice"). `--json` is appended; args containing "--json" or a
 * bare "--" separator are rejected so the appended flag cannot be displaced.
 */
export function runAnvil(
  verb: string,
  args: string[] = [],
  env: Record<string, string | undefined> = process.env,
  cwd?: string,
  signal?: AbortSignal,
  options: AnvilRunOptions = {}
): Promise<AnvilCliResult> {
  const check = checkVerb(verb, env);
  if (check) {
    return Promise.resolve({ ok: false, stdout: "", stderr: check, exitCode: -1 });
  }
  const maxArgChars = options.maxArgChars ?? MAX_RUN_ARG_CHARS;
  const maxTotalChars = options.maxTotalChars ?? MAX_RUN_ARGS_TOTAL_CHARS;
  const cleanArgs = args.filter((a) => typeof a === "string" && a.length > 0);
  if (cleanArgs.length > MAX_RUN_ARGS) {
    return Promise.resolve({ ok: false, stdout: "", stderr: `too many args (${cleanArgs.length} > ${MAX_RUN_ARGS})`, exitCode: -1 });
  }
  if (cleanArgs.some((a) => a.length > maxArgChars) || cleanArgs.join(" ").length > maxTotalChars) {
    return Promise.resolve({ ok: false, stdout: "", stderr: `args exceed size caps (${maxArgChars} per arg, ${maxTotalChars} total)`, exitCode: -1 });
  }
  if (cleanArgs.includes("--json") || cleanArgs.includes("--")) {
    return Promise.resolve({ ok: false, stdout: "", stderr: 'args must not contain "--json" or a bare "--" separator', exitCode: -1 });
  }
  const appendJson = options.json !== false;

  return new Promise((resolveResult) => {
    const child = spawn(anvilBin(env), [verb, ...cleanArgs, ...(appendJson ? ["--json"] : [])], {
      cwd,
      env: env as NodeJS.ProcessEnv,
      stdio: ["ignore", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    let settled = false;
    let aborted = false;
    const timeoutMs = options.timeoutMs ?? 120_000;

    const finish = (result: AnvilCliResult) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      signal?.removeEventListener("abort", onAbort);
      resolveResult(result);
    };

    const timer = setTimeout(() => {
      child.kill("SIGTERM");
      setTimeout(() => child.kill("SIGKILL"), 5_000).unref?.();
    }, timeoutMs);
    timer.unref?.();

    const onAbort = () => {
      aborted = true;
      child.kill("SIGTERM");
    };
    signal?.addEventListener("abort", onAbort, { once: true });

    const capStream = (current: string, chunk: string) => (current.length < CAPTURE_MAX_BYTES ? current + chunk : current);
    child.stdout?.on("data", (chunk: string) => {
      stdout = capStream(stdout, String(chunk));
    });
    child.stderr?.on("data", (chunk: string) => {
      stderr = capStream(stderr, String(chunk));
    });
    child.on("error", (error: Error) => {
      finish({ ok: false, stdout, stderr: String(error?.message ?? error), exitCode: -1 });
    });
    child.on("exit", (code) => {
      if (aborted) {
        finish({ ok: false, stdout, stderr: stderr || "anvil call aborted", exitCode: -1 });
        return;
      }
      finish({
        ok: code === 0,
        stdout,
        stderr,
        exitCode: code ?? -1,
      });
    });
  });
}

/** Max chars for tool output reaching the model (bounded, ~3k tokens). */
export const TOOL_OUTPUT_MAX_BYTES = 12_000;
/** Hard cap on error text surfaced to the model. */
export const ERROR_MAX_CHARS = 2_000;

/** Parse + truncate CLI stdout for tool results. EVERY branch is capped —
 * including failure stderr, so a giant traceback can never blow the bound. */
export function presentResult(result: AnvilCliResult): { text: string; isError: boolean } {
  if (!result.ok) {
    let detail = result.stderr.trim() || `anvil exited ${result.exitCode}`;
    if (detail.length > ERROR_MAX_CHARS) detail = detail.slice(0, ERROR_MAX_CHARS - 1) + "…";
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

/** Bounded session-start snapshot: status JSON capped to ~6k chars (~1.5k
 * tokens) TOTAL. Status call uses a short timeout; failures are capped too. */
export const SNAPSHOT_MAX_CHARS = 6000;
export const SNAPSHOT_TIMEOUT_MS = 15_000;

export async function sessionSnapshot(
  env: Record<string, string | undefined> = process.env,
  cwd?: string,
  signal?: AbortSignal
): Promise<string | null> {
  if (!hasAnvilState(env, cwd)) return null;
  const result = await runAnvil("status", [], env, cwd, signal, { timeoutMs: SNAPSHOT_TIMEOUT_MS });
  if (!result.ok) {
    let detail = (result.stderr || `exit ${result.exitCode}`).trim();
    if (detail.length > SNAPSHOT_MAX_CHARS - 80) detail = detail.slice(0, SNAPSHOT_MAX_CHARS - 80) + "…";
    return `anvil state detected but status failed: ${detail}`;
  }
  const combined = `[anvil session snapshot — project state at session start]\n${result.stdout}`;
  // The cap is on the WHOLE returned string (header included), not the body.
  return combined.length > SNAPSHOT_MAX_CHARS
    ? combined.slice(0, SNAPSHOT_MAX_CHARS - 1) + "…"
    : combined;
}

/** Task IDs: reject empty, oversized, and option-like values so a "task id"
 * can never be reinterpreted as a CLI flag. */
export function isValidTaskId(id: string): boolean {
  return typeof id === "string" && id.length > 0 && id.length <= 64 && !id.startsWith("-") && !/\s/.test(id);
}

/**
 * Tokenize a /anvil:submit argument string with quote support (single and
 * double quotes preserved verbatim; NO shell syntax execution). Unterminated
 * quotes throw — the caller surfaces a usage error rather than silently
 * truncating values.
 */
export function tokenizeQuotedArgs(input: string): string[] {
  const tokens: string[] = [];
  let current = "";
  let quote: '"' | "'" | null = null;
  for (const ch of input) {
    if (quote) {
      if (ch === quote) quote = null;
      else current += ch;
      continue;
    }
    if (ch === '"' || ch === "'") {
      quote = ch;
      continue;
    }
    if (/\s/.test(ch)) {
      if (current) {
        tokens.push(current);
        current = "";
      }
      continue;
    }
    current += ch;
  }
  if (quote) throw new Error("unterminated quote in arguments");
  if (current) tokens.push(current);
  return tokens;
}