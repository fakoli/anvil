#!/usr/bin/env node
// pi-sandbox-launch.mjs — fail-closed launcher for unattended pi children.
//
// Replaces the sh/eval launcher after adversarial review (verdict: rework):
// - NO shell reconstruction: pi is spawned with an argv ARRAY, shell:false.
//   Task text travels as stdin JSONL, never on argv — option-like or
//   @file-looking task text cannot become pi flags or attachments.
// - What is verified is what loads: path: pins are read ONCE, hashed, and
//   staged (entry + same-dir siblings, symlinks rejected); pi loads the
//   staged copies. Relative pins resolve against the allowlist directory,
//   not the cwd — verify-then-load divergence is closed.
// - Env sanitized BEFORE anything runs: NODE_*, LD_*/DYLD_* loaders, BASH_ENV,
//   npm_config_* and other injection vectors are scrubbed for the child.
// - pi/npm/git are never invoked by the verifier (pure fs hashing), so there
//   is no ambient-executable window at all.
//
// Usage:
//   scripts/pi-sandbox-launch.mjs --profile <name> --workspace <dir> \
//     (--task-file <file> | --task <text>) [--allowlist <path>] [--pi <bin>] \
//     [--seed-dir <dir>] [--model provider/id] [--dry-run]
//
// Exit codes: 0 ok · pi's exit code forwarded · 2 policy/usage · 3 pin mismatch.

import { createHash } from "node:crypto";
import { spawn, spawnSync } from "node:child_process";
import { mkdtemp, rm, writeFile, readdir, readFile, lstat, mkdir, copyFile } from "node:fs/promises";
import { realpathSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, isAbsolute, join, resolve } from "node:path";
import {
  EXIT_POLICY,
  SCRUBBED_ENV_KEYS,
  childEnv,
  composeReport,
  loadAllowlist,
  stagePathExtension,
  treeHash,
} from "./pi-sandbox-policy.mjs";

function die(code, message) {
  console.error(`pi-sandbox-launch: ${message}`);
  process.exit(code);
}

function parseArgs(argv) {
  const out = { _: [] };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a.startsWith("--")) {
      const key = a.slice(2);
      const next = argv[i + 1];
      if (next !== undefined && !next.startsWith("--")) {
        out[key] = next;
        i++;
      } else {
        out[key] = true;
      }
    } else {
      out._.push(a);
    }
  }
  return out;
}

/** Resolve the pi binary to an absolute real path from the CURRENT env —
 * done once, before any other work, and never re-resolved after cwd changes. */
function resolvePiBinary(piArg) {
  if (piArg) {
    try {
      return realpathSync(resolve(piArg));
    } catch {
      die(EXIT_POLICY, `--pi does not resolve: ${piArg}`);
    }
  }
  const pathEnv = process.env.PATH ?? "";
  for (const dir of pathEnv.split(":")) {
    if (!dir) continue;
    const candidate = join(dir, "pi");
    const check = spawnSync("/usr/bin/test", ["-x", candidate], { shell: false });
    if (check.status === 0) {
      try {
        return realpathSync(candidate);
      } catch {
        continue;
      }
    }
  }
  die(EXIT_POLICY, "pi binary not found on PATH; pass --pi <absolute-path>");
}

/** Dry-run verification: hash the pinned bytes without staging anything. */
async function verifyOnly(entry, allowlistPath) {
  // Dry-run verification mirrors the real staging path's ACCEPTANCE rules
  // (tree pins verify the whole canonical tree; flat pins verify the entry
  // bytes) so a run can never pass dry-run and fail launch on different rules.
  const base = dirname(resolve(allowlistPath));
  const abs = resolve(base, entry.source.slice("path:".length));
  const info = await lstat(abs);
  if (info.isSymbolicLink()) throw { code: "mismatch", message: `${abs} is a symlink` };
  if (!info.isFile()) throw { code: "missing", message: `${abs} is not a regular file` };
  if (typeof entry.tree_sha256 === "string") {
    const { digest } = await treeHash(dirname(abs));
    if (digest !== entry.tree_sha256) {
      throw { code: "mismatch", message: `${dirname(abs)}: expected tree_sha256 ${entry.tree_sha256}, got ${digest}` };
    }
    return digest;
  }
  const sha256 = createHash("sha256").update(await readFile(abs)).digest("hex");
  if (sha256 !== entry.sha256) throw { code: "mismatch", message: `${abs}: expected ${entry.sha256}, got ${sha256}` };
  return sha256;
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const profileName = args.profile;
  const workspace = args.workspace;
  const allowlistPath = resolve(args.allowlist ?? join(resolve(import.meta.dirname), "..", "packaging", "pi", "sandbox", "allowlist.json"));
  const dryRun = args["dry-run"] === true;
  const seedDir = args["seed-dir"];
  const model = args.model;
  if (!profileName) die(EXIT_POLICY, "missing --profile");
  if (!workspace) die(EXIT_POLICY, "missing --workspace");

  // Single allowlist snapshot: one read, one parse, reused for every stage.
  const { doc, problems } = await loadAllowlist(allowlistPath);
  if (problems.length > 0) {
    for (const problem of problems) console.error(`INVALID: ${problem}`);
    process.exit(EXIT_POLICY);
  }
  const profile = doc.profiles[profileName];
  if (!profile) {
    die(EXIT_POLICY, `profile "${profileName}" not in ${allowlistPath} (have: ${Object.keys(doc.profiles).join(", ")})`);
  }

  let task = "";
  if (args["task-file"]) task = await (await import("node:fs/promises")).readFile(args["task-file"], "utf8");
  else if (typeof args.task === "string") task = args.task;
  else die(EXIT_POLICY, "need --task-file <file> or --task <text>");
  task = task.trim();
  if (task.length === 0) die(EXIT_POLICY, "task is empty");
  if (task.length > 100_000) die(EXIT_POLICY, "task exceeds 100k chars");

  const piBin = resolvePiBinary(args.pi);

  if (model !== undefined && !/^[a-zA-Z0-9_.:-]+\/[a-zA-Z0-9_.:-]+$/.test(model)) {
    die(EXIT_POLICY, `--model must be "provider/model-id" ([a-zA-Z0-9_.:-]), got: ${model}`);
  }
  if (seedDir !== undefined) {
    const seed = resolve(seedDir);
    const info = await lstat(seed).catch(() => null);
    if (!info || !info.isDirectory()) die(EXIT_POLICY, `--seed-dir must be an existing directory: ${seed}`);
  }

  // Verify (dry-run) or stage+verify (real run). Any pin problem aborts
  // BEFORE the child could exist — nothing launches on a bad pin.
  const cleanupDirs = [];
  const stagedArgs = [];
  // Failures after dir creation must flow through the finally-cleanup, so they
  // throw { exitCode, message } instead of calling die() (process.exit skips
  // finally blocks and would leak staging dirs).
  let exitError = null;
  try {
    if (dryRun) {
      for (const [index, entry] of profile.extensions.entries()) {
        const sha256 = await verifyOnly(entry, allowlistPath).catch((error) => {
          throw { exitCode: error.code === "mismatch" ? 3 : EXIT_POLICY, message: `pin check failed (extensions[${index}] ${entry.source}): ${error.message}` };
        });
        console.log(`OK: extensions[${index}] ${entry.source} — ${sha256}`);
      }
    } else {
      const stageDir = await mkdtemp(join(tmpdir(), "pi-sandbox-stage."));
      cleanupDirs.push(stageDir);
      for (const [index, entry] of profile.extensions.entries()) {
        const staged = await stagePathExtension(entry, allowlistPath, stageDir, `ext-${index}`).catch((error) => {
          throw { exitCode: error.code === "mismatch" ? 3 : EXIT_POLICY, message: `staging failed (extensions[${index}] ${entry.source}): ${error.message}` };
        });
        stagedArgs.push("-e", staged.stagedPath);
        console.log(`STAGED: extensions[${index}] ${entry.source} -> ${staged.stagedPath} (${staged.files.length} file(s))`);
      }
    }

    if (dryRun) {
      console.log(JSON.stringify({ ...composeReport(profile, profileName, task), seedDir: seedDir ?? null, model: model ?? null }, null, 2));
      console.log(`pi binary: ${piBin}`);
      console.log("dry-run: not launching, no runtime dirs created");
      process.exit(0);
    }

    const agentDir = await mkdtemp(join(tmpdir(), "pi-sandbox-agent."));
    cleanupDirs.push(agentDir);
    if (seedDir !== undefined) {
      // Pre-seed the fresh agent dir (e.g. models.json wiring a loopback
      // provider). Regular files only, symlinks/special files rejected — the
      // same rules staging enforces, so the seed cannot smuggle links.
      const seed = resolve(seedDir);
      let seeded = 0;
      const walk = async (dir, prefix = "", depth = 0) => {
        if (depth > 8) die(EXIT_POLICY, `--seed-dir exceeds depth cap at ${dir}`);
        for (const e of await readdir(dir, { withFileTypes: true })) {
          const rel = prefix ? `${prefix}/${e.name}` : e.name;
          if (e.isSymbolicLink()) die(EXIT_POLICY, `seed contains a symlink: ${rel}`);
          if (e.isDirectory()) await walk(join(dir, e.name), rel, depth + 1);
          else if (e.isFile()) {
            await mkdir(join(agentDir, prefix), { recursive: true });
            await copyFile(join(dir, e.name), join(agentDir, rel));
            seeded += 1;
          } else die(EXIT_POLICY, `seed contains a special file: ${rel}`);
        }
      };
      await walk(seed);
      console.log(`SEEDED: ${seeded} file(s) into fresh agent dir`);
    }
    const env = childEnv({ PI_CODING_AGENT_DIR: agentDir });

    const report = composeReport(profile, profileName, task);
    const argv = ["pi", ...stagedArgs, ...(model ? ["--model", model] : []), ...report.argv];
    const child = spawn(piBin, argv.slice(1), {
      cwd: resolve(workspace),
      env,
      shell: false,
      stdio: ["pipe", "inherit", "inherit"],
    });

    // Task as stdin JSONL: pure data, never parsed as CLI flags.
    child.stdin.on("error", () => {}); // pi may close stdin early; not fatal
    child.stdin.write(`${JSON.stringify({ id: "sandbox-run", type: "prompt", message: task })}\n`);
    child.stdin.end();

    const forwarded = ["SIGINT", "SIGTERM"];
    for (const signal of forwarded) {
      process.on(signal, () => child.kill(signal));
    }

    const code = await new Promise((resolveExit) => {
      child.on("exit", (exitCode) => resolveExit(exitCode ?? 1));
      child.on("error", (error) => {
        console.error(`pi-sandbox-launch: child failed to start: ${error.message}`);
        resolveExit(EXIT_POLICY);
      });
    });
    process.exitCode = code;
  } catch (error) {
    exitError = error;
  } finally {
    await Promise.all(cleanupDirs.map((dir) => rm(dir, { recursive: true, force: true })));
  }
  if (exitError) die(exitError.exitCode ?? EXIT_POLICY, exitError.message);
}

main().catch((error) => die(EXIT_POLICY, error?.message ?? String(error)));