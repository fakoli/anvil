#!/usr/bin/env node
// pi-sandbox-policy.mjs — policy brain for the pi sandbox launcher.
//
// M1-rework contract (per adversarial review):
// - ONLY `path:` extension pins are supported. npm:/git: entries are REJECTED
//   until artifacts are materialized and staged at build time (M4) — pin
//   verification that is not bound to the loaded bytes is false assurance.
// - `skills` must be empty: nonempty skill lists reopen discovery.
// - `projectTrust` is not configurable here; the launcher hard-codes -na.
//
// Subcommands:
//   validate <allowlist.json>            structural validation. Exit 2 on problems.
//   compose  <allowlist.json> --profile P (--task-file F | --task S)
//                                        Dry-run report (no dirs, no launch).
//
// Exit codes: 0 ok · 2 policy/usage error.
//
// The launcher (pi-sandbox-launch.mjs) imports the exported functions and is
// the ONLY component that stages bytes and spawns pi.

import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";

export const EXIT_OK = 0;
export const EXIT_POLICY = 2;

function die(code, message) {
  console.error(`pi-sandbox-policy: ${message}`);
  process.exit(code);
}

// --- validation (single source of truth; allowlist.schema.json mirrors it) ---

export function validateExtensionPin(where, entry) {
  if (typeof entry !== "object" || entry === null || Array.isArray(entry)) {
    return [`${where}: must be an object`];
  }
  const problems = [];
  if (typeof entry.source !== "string" || !entry.source.startsWith("path:") || entry.source.length < 6) {
    problems.push(`${where}: source must be a "path:..." pin (npm:/git: entries are rejected until M4 stages verified artifacts)`);
    return problems;
  }
  const allowed = new Set(["source", "sha256"]);
  for (const key of Object.keys(entry)) {
    if (!allowed.has(key)) problems.push(`${where}: unknown key "${key}"`);
  }
  if (typeof entry.sha256 !== "string" || !/^[a-f0-9]{64}$/.test(entry.sha256)) {
    problems.push(`${where}: sha256 must be 64 lowercase hex chars`);
  }
  return problems;
}

export function validateProfile(name, profile) {
  const problems = [];
  const where = `profile "${name}"`;
  if (typeof profile !== "object" || profile === null || Array.isArray(profile)) {
    return [`${where}: must be an object`];
  }
  if (!Array.isArray(profile.extensions)) problems.push(`${where}: extensions must be an array`);
  else profile.extensions.forEach((entry, i) => problems.push(...validateExtensionPin(`${where}.extensions[${i}]`, entry)));
  if (!Array.isArray(profile.tools) || profile.tools.length === 0) {
    problems.push(`${where}: tools must be a non-empty array`);
  } else if (profile.tools.some((t) => typeof t !== "string" || !/^[a-zA-Z0-9_-]+$/.test(t))) {
    problems.push(`${where}: tools entries must be [a-zA-Z0-9_-]+ strings`);
  }
  if (!Array.isArray(profile.skills)) problems.push(`${where}: skills must be an array`);
  else if (profile.skills.some((s) => typeof s !== "string" || s.length === 0)) {
    problems.push(`${where}: skills entries must be non-empty strings`);
  } else if (profile.skills.length > 0) {
    problems.push(`${where}: non-empty skills are not supported by the sandbox launcher (skill loading reopens discovery); keep skills: []`);
  }
  if (!["none", "inference"].includes(profile.network)) {
    problems.push(`${where}: network must be "none" or "inference"`);
  }
  if (profile.projectTrust !== undefined) {
    problems.push(`${where}: projectTrust is not configurable in the sandbox launcher (-na is hard-coded); remove the key`);
  }
  const allowed = new Set(["description", "extensions", "tools", "skills", "network"]);
  for (const key of Object.keys(profile)) {
    if (!allowed.has(key)) problems.push(`${where}: unknown key "${key}"`);
  }
  if (profile.description !== undefined && typeof profile.description !== "string") {
    problems.push(`${where}: description must be a string`);
  }
  return problems;
}

export function validateAllowlistDoc(doc, label) {
  const problems = [];
  if (typeof doc !== "object" || doc === null || Array.isArray(doc)) return [`${label}: must be a JSON object`];
  if (doc.version !== 1) problems.push(`version must be 1, got ${JSON.stringify(doc.version)}`);
  if (doc.$comment !== undefined && typeof doc.$comment !== "string") problems.push("$comment must be a string");
  const rootAllowed = new Set(["version", "$comment", "profiles"]);
  for (const key of Object.keys(doc)) {
    if (!rootAllowed.has(key)) problems.push(`unknown root key "${key}"`);
  }
  if (typeof doc.profiles !== "object" || doc.profiles === null || Array.isArray(doc.profiles)) {
    problems.push("profiles must be an object");
  } else if (Object.keys(doc.profiles).length === 0) {
    problems.push("profiles must not be empty");
  } else {
    for (const [name, profile] of Object.entries(doc.profiles)) {
      problems.push(...validateProfile(name, profile));
    }
  }
  return problems;
}

export async function loadAllowlist(file) {
  const raw = await readFile(file, "utf8");
  let doc;
  try {
    doc = JSON.parse(raw);
  } catch (error) {
    return { doc: null, problems: [`${file}: not valid JSON: ${error.message}`] };
  }
  const problems = validateAllowlistDoc(doc, file);
  return { doc: problems.length === 0 ? doc : null, problems };
}

// --- staging (exported for the launcher; CLI does not stage) -------------------
import { lstat, mkdir, readdir, writeFile } from "node:fs/promises";
import { basename, dirname, join, resolve } from "node:path";

export const STAGE_MAX_BYTES = 25 * 1024 * 1024; // per-extension staging cap

/**
 * Stage a `path:` extension: ONE read of the entry bytes, hash those exact
 * bytes, compare to the pin, and copy entry + same-directory siblings (no
 * symlinks) into `<stageDir>/<key>/`. The launcher loads ONLY staged paths —
 * what was verified is what pi loads. Relative pins resolve against the
 * allowlist's directory (one resolution base, no cwd dependence).
 *
 * Returns { stagedPath, sha256, files, bytes } or throws { code, message }.
 */
export async function stagePathExtension(entry, allowlistPath, stageDir, key) {
  const base = dirname(resolve(allowlistPath));
  const sourceRel = entry.source.slice("path:".length);
  const sourceAbs = resolve(base, sourceRel);
  const destDir = join(stageDir, key);
  const copied = [];
  let bytes = 0;

  let info;
  try {
    info = await lstat(sourceAbs);
  } catch {
    throw { code: "missing", message: `${sourceAbs} does not exist (resolved against ${base})` };
  }
  if (info.isSymbolicLink()) {
    throw { code: "mismatch", message: `${sourceAbs} is a symlink; symlinks are not allowed in pinned extensions` };
  }
  if (!info.isFile()) {
    throw { code: "missing", message: `${sourceAbs} is not a regular file` };
  }

  await mkdir(destDir, { recursive: true });
  const entryDir = dirname(sourceAbs);
  const entries = await readdir(entryDir, { withFileTypes: true });
  for (const sibling of entries) {
    if (sibling.name === basename(sourceAbs)) continue;
    if (sibling.isSymbolicLink()) continue; // not staged; entry import of a symlinked sibling fails at load, not silently
    if (!sibling.isFile()) continue;
    bytes += sibling.size + (await addSibling(entryDir, sibling.name, destDir, copied));
    if (bytes > STAGE_MAX_BYTES) {
      throw { code: "missing", message: `extension directory exceeds staging cap ${STAGE_MAX_BYTES} bytes` };
    }
  }

  const entryBytes = await readFile(sourceAbs);
  const sha256 = createHash("sha256").update(entryBytes).digest("hex");
  if (sha256 !== entry.sha256) {
    throw { code: "mismatch", message: `${sourceAbs}: expected sha256 ${entry.sha256}, got ${sha256}` };
  }
  bytes += entryBytes.length;
  await writeFile(join(destDir, basename(sourceAbs)), entryBytes);
  copied.push(basename(sourceAbs));

  return { stagedPath: join(destDir, basename(sourceAbs)), sha256, files: copied, bytes };
}

async function addSibling(sourceDir, name, destDir, copied) {
  const data = await readFile(join(sourceDir, name));
  await writeFile(join(destDir, name), data);
  copied.push(name);
  return data.length;
}

// --- compose (dry-run report; no dirs created, no launch) -----------------------

export function composeReport(profile, profileName, task) {
  const args = ["--no-extensions", "--tools", profile.tools.join(","), "--no-skills", "-na", "--mode", "json"];
  return {
    profile: profileName,
    // task is NOT on argv: it travels as stdin JSONL, so option-like or
    // @file-looking task text can never become pi flags or attachments.
    argv: args,
    taskChars: task.length,
    env: {
      PI_CODING_AGENT_DIR: "<fresh per-run dir>",
      scrubbed: SCRUBBED_ENV_KEYS,
      passthrough: "everything else except npm_config_*/NODE_*",
    },
    docker:
      profile.network === "none"
        ? { network: "none", note: "containerization lands in M4; today the child runs on the host with a fresh agent dir" }
        : { network: "inference-only-allowlist", note: "egress restriction enforced in M4" },
  };
}

export const SCRUBBED_ENV_KEYS = [
  "NODE_OPTIONS", "NODE_PATH", "LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT",
  "DYLD_INSERT_LIBRARIES", "DYLD_LIBRARY_PATH", "BASH_ENV", "ENV",
  "PYTHONSTARTUP", "PYTHONPATH", "ZDOTDIR", "GEM_HOME", "RUBYOPT",
];

export function childEnv(extra = {}) {
  const env = {};
  for (const [key, value] of Object.entries(process.env)) {
    if (/^npm_config_/i.test(key)) continue;
    if (/^NODE_/i.test(key)) continue;
    if (SCRUBBED_ENV_KEYS.includes(key)) continue;
    env[key] = value;
  }
  return { ...env, ...extra };
}

// --- CLI ----------------------------------------------------------------------

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const command = args._[0];
  if (!command || !["validate", "compose"].includes(command)) {
    die(EXIT_POLICY, 'usage: pi-sandbox-policy.mjs <validate|compose> <allowlist.json> [--profile <name>] [--task-file <f>|--task <s>]');
  }
  const file = args._[1];
  if (!file) die(EXIT_POLICY, "missing allowlist path");

  const { doc, problems } = await loadAllowlist(file);
  if (command === "validate") {
    if (problems.length > 0) {
      for (const problem of problems) console.error(`INVALID: ${problem}`);
      process.exit(EXIT_POLICY);
    }
    console.log(`OK: ${file} validates`);
    process.exit(EXIT_OK);
  }

  if (problems.length > 0) {
    for (const problem of problems) console.error(`INVALID: ${problem}`);
    process.exit(EXIT_POLICY);
  }
  const profileName = args.profile;
  const profile = doc.profiles[profileName];
  if (!profile) die(EXIT_POLICY, `profile "${profileName}" not in ${file} (have: ${Object.keys(doc.profiles).join(", ")})`);

  let task = "";
  if (args["task-file"]) task = await readFile(args["task-file"], "utf8");
  else if (typeof args.task === "string") task = args.task;
  else die(EXIT_POLICY, "compose needs --task-file <file> or --task <text>");
  task = task.trim();
  if (task.length === 0) die(EXIT_POLICY, "task is empty");
  if (task.length > 100_000) die(EXIT_POLICY, "task exceeds 100k chars");

  console.log(JSON.stringify(composeReport(profile, profileName, task), null, 2));
  process.exit(EXIT_OK);
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

// note: main() only runs when executed directly; the launcher imports this module
if (process.argv[1] && resolve(process.argv[1]) === resolve(import.meta.url.replace("file://", ""))) {
  main().catch((error) => die(EXIT_POLICY, error.message));
}
