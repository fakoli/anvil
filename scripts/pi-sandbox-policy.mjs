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
    problems.push(`${where}: source must be a "path:..." pin`);
    return problems;
  }
  const hasFilePin = typeof entry.sha256 === "string";
  const hasTreePin = typeof entry.tree_sha256 === "string";
  if (hasFilePin && hasTreePin) {
    problems.push(`${where}: provide exactly one of sha256 (flat entry pin) or tree_sha256 (whole-directory pin), not both`);
    return problems;
  }
  if (!hasFilePin && !hasTreePin) {
    problems.push(`${where}: missing sha256 (flat entry pin) or tree_sha256 (whole-directory pin)`);
    return problems;
  }
  const digestPattern = /^[a-f0-9]{64}$/;
  if (hasFilePin && !digestPattern.test(entry.sha256)) {
    problems.push(`${where}: sha256 must be 64 lowercase hex chars`);
  }
  if (hasTreePin && !digestPattern.test(entry.tree_sha256)) {
    problems.push(`${where}: tree_sha256 must be 64 lowercase hex chars`);
  }
  const allowed = hasTreePin ? new Set(["source", "tree_sha256"]) : new Set(["source", "sha256"]);
  for (const key of Object.keys(entry)) {
    if (!allowed.has(key)) problems.push(`${where}: unknown key "${key}"`);
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
  if (profile.network === "inference") {
    // M4 (advisory): an inference-capable child needs an enforced proxy
    // boundary (isolated docker network + a restricted proxy that alone holds
    // credentials); env-var filtering alone is bypassable by a child with
    // bash. Until that boundary exists the launcher refuses this profile.
    problems.push(`${where}: network "inference" is not supported until an enforced egress-proxy boundary exists; use network "none" (a loopback provider is acceptable)`);
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
export const STAGE_MAX_FILES = 4096; // per-extension file cap
export const STAGE_MAX_DEPTH = 16; // per-extension directory depth cap

// Tree-pin encoding, version 2 (length-prefixed; advisory-reviewed): digest over the
// extension ROOT directory (the pinned entry's parent). Records are emitted for
// every regular file in deterministic bytewise-sorted posix-relative order:
//   <relByteLength>:<relpath><sha256hex(file bytes)>\n
// concatenated, then sha256'd. The byte-length prefix makes the record parse
// unambiguous for ANY filename (v1's bare <rel>\n<hex>\n records could be
// forged by newline-containing filenames — see canonicalTreeDigest). Empty directories are omitted (they carry no
// bytes and pi cannot import them). Hash RAW file bytes — CRLF, whitespace, and
// mode changes that matter must change the digest. Rejected, never skipped:
// symlinks or special files anywhere in the tree, and `.git` / `node_modules`
// directories (build junk must not silently enter a verified pin).
export const TREE_PIN_VERSION = 2;
const REJECTED_DIR_NAMES = new Set([".git", "node_modules"]);

async function walkTree(rootDir, prefix = "", depth = 0) {
  if (depth > STAGE_MAX_DEPTH) {
    throw { code: "missing", message: `extension tree exceeds depth cap ${STAGE_MAX_DEPTH} at ${rootDir}` };
  }
  const out = [];
  const entries = await readdir(rootDir, { withFileTypes: true });
  for (const e of entries) {
    const rel = prefix ? `${prefix}/${e.name}` : e.name;
    if (e.isSymbolicLink()) {
      throw { code: "invalid", message: `symlink in pinned tree: ${rel} (symlinks are not allowed in tree pins)` };
    }
    if (!e.isDirectory() && !e.isFile()) {
      throw { code: "invalid", message: `special file in pinned tree: ${rel} (only regular files are allowed)` };
    }
    if (e.isDirectory()) {
      if (REJECTED_DIR_NAMES.has(e.name)) {
        throw { code: "invalid", message: `rejected directory in pinned tree: ${rel} (${e.name} must not enter a verified pin)` };
      }
      out.push(...(await walkTree(join(rootDir, e.name), rel, depth + 1)));
    } else {
      out.push({ rel, abs: join(rootDir, e.name) });
    }
  }
  return out;
}

export function canonicalTreeDigest(files) {
  // files: [{ rel, bytes: Buffer }]. Bytewise sort on the relpath (no locale).
  // Encoding v2: LENGTH-PREFIXED records — `<relByteLen>:<rel><sha256hex>\n`.
  // v1 ("<rel>\n<hex>\n") had practical collisions: filenames containing
  // newlines could forge record boundaries without any hash collision.
  // The length prefix makes the parse unambiguous for ANY filename.
  const sorted = [...files].sort((a, b) => {
    const ka = Buffer.from(a.rel, "utf8");
    const kb = Buffer.from(b.rel, "utf8");
    return ka.compare(kb);
  });
  const hash = createHash("sha256");
  for (const f of sorted) {
    const relBytes = Buffer.from(f.rel, "utf8");
    hash.update(Buffer.from(`${relBytes.length}:`, "utf8"));
    hash.update(relBytes);
    hash.update(Buffer.from(`${createHash("sha256").update(f.bytes).digest("hex")}\n`, "utf8"));
  }
  return hash.digest("hex");
}

/**
 * Read+validate a pin snapshot: the SAME acceptance rules and the SAME bytes
 * for dry-run verification and real staging (advisory F3 — the rules can
 * never diverge). Returns { entryAbs, rootDir, kind, files: [{rel, name?,
 * bytes}], entryBytes, digest-or-sha } or throws { code, message }.
 *
 * Tree pins: the whole directory, recursively (symlinks/special/.git/
 * node_modules rejected), digest = canonicalTreeDigest v2.
 * Flat pins: entry + same-dir siblings (siblings staged unverified but
 * byte/file-capped); digest = sha256(entry bytes).
 */
export async function readPinSnapshot(entry, allowlistPath) {
  const base = dirname(resolve(allowlistPath));
  const sourceRel = entry.source.slice("path:".length);
  const sourceAbs = resolve(base, sourceRel);

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

  if (typeof entry.tree_sha256 === "string") {
    const rootDir = dirname(sourceAbs);
    const files = await walkTree(rootDir);
    if (files.length > STAGE_MAX_FILES) {
      throw { code: "missing", message: `extension tree exceeds file cap ${STAGE_MAX_FILES}` };
    }
    const withBytes = [];
    let bytes = 0;
    for (const f of files) {
      const data = await readFile(f.abs);
      withBytes.push({ rel: f.rel, bytes: data });
      bytes += data.length;
      if (bytes > STAGE_MAX_BYTES) {
        throw { code: "missing", message: `extension tree exceeds staging cap ${STAGE_MAX_BYTES} bytes` };
      }
    }
    const digest = canonicalTreeDigest(withBytes);
    if (digest !== entry.tree_sha256) {
      throw { code: "mismatch", message: `${rootDir}: expected tree_sha256 ${entry.tree_sha256}, got ${digest}` };
    }
    return { entryAbs: sourceAbs, rootDir, kind: "tree", files: withBytes, digest, bytes };
  }

  // Flat pin: entry verified by hash; siblings read under the same caps.
  const entryDir = dirname(sourceAbs);
  const entries = await readdir(entryDir, { withFileTypes: true });
  const siblingFiles = entries.filter(
    (e) => e.name !== basename(sourceAbs) && !e.isSymbolicLink() && e.isFile()
  );
  const withBytes = [];
  let bytes = 0;
  for (const sibling of siblingFiles) {
    const data = await readFile(join(entryDir, sibling.name));
    withBytes.push({ rel: sibling.name, bytes: data });
    bytes += data.length;
    if (bytes > STAGE_MAX_BYTES) {
      throw { code: "missing", message: `extension directory exceeds staging cap ${STAGE_MAX_BYTES} bytes` };
    }
  }
  if (siblingFiles.length + 1 > STAGE_MAX_FILES) {
    throw { code: "missing", message: `extension directory exceeds file cap ${STAGE_MAX_FILES}` };
  }
  const entryBytes = await readFile(sourceAbs);
  const sha256 = createHash("sha256").update(entryBytes).digest("hex");
  if (sha256 !== entry.sha256) {
    throw { code: "mismatch", message: `${sourceAbs}: expected sha256 ${entry.sha256}, got ${sha256}` };
  }
  bytes += entryBytes.length;
  if (bytes > STAGE_MAX_BYTES) {
    throw { code: "missing", message: `extension exceeds staging cap ${STAGE_MAX_BYTES} bytes` };
  }
  return { entryAbs: sourceAbs, rootDir: entryDir, kind: "flat", files: withBytes, entryBytes, digest: sha256, bytes };
}

/**
 * Stage a snapshot (from readPinSnapshot) into `<stageDir>/<key>/`. The
 * launcher loads ONLY staged paths — what was verified is what pi loads.
 */
export async function stageSnapshot(snapshot, stageDir, key) {
  const destDir = join(stageDir, key);
  await mkdir(destDir, { recursive: true });
  const copied = [];
  const entryRel = snapshot.kind === "tree"
    ? snapshot.entryAbs.slice(snapshot.rootDir.length + 1)
    : basename(snapshot.entryAbs);
  if (snapshot.kind === "tree") {
    for (const f of snapshot.files) {
      const dest = join(destDir, f.rel);
      await mkdir(dirname(dest), { recursive: true });
      await writeFile(dest, f.bytes);
      copied.push(f.rel);
    }
    return {
      stagedPath: join(destDir, entryRel),
      sha256: createHash("sha256").update(snapshot.files.find((f) => f.rel === entryRel).bytes).digest("hex"),
      tree_sha256: snapshot.digest,
      files: copied,
      bytes: snapshot.bytes,
    };
  }
  await writeFile(join(destDir, entryRel), snapshot.entryBytes);
  copied.push(entryRel);
  for (const f of snapshot.files) {
    await writeFile(join(destDir, f.rel), f.bytes);
    copied.push(f.rel);
  }
  return { stagedPath: join(destDir, entryRel), sha256: snapshot.digest, files: copied, bytes: snapshot.bytes };
}

/**
 * Stage a `path:` extension (snapshot + write). Kept for callers that want
 * one call; the launcher uses read+stage so dry-run and launch share rules.
 */
export async function stagePathExtension(entry, allowlistPath, stageDir, key) {
  const snapshot = await readPinSnapshot(entry, allowlistPath);
  return stageSnapshot(snapshot, stageDir, key);
}

export async function treeHash(dir) {
  // CLI helper for pin authoring: canonical digest of a directory tree.
  const files = await walkTree(dir);
  if (files.length > STAGE_MAX_FILES) {
    throw { code: "missing", message: `tree exceeds file cap ${STAGE_MAX_FILES}` };
  }
  const withBytes = [];
  let bytes = 0;
  for (const f of files) {
    const data = await readFile(f.abs);
    withBytes.push({ rel: f.rel, bytes: data });
    bytes += data.length;
    if (bytes > STAGE_MAX_BYTES) {
      throw { code: "missing", message: `tree exceeds staging cap ${STAGE_MAX_BYTES} bytes` };
    }
  }
  return { digest: canonicalTreeDigest(withBytes), files: withBytes.length, bytes };
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
        ? { network: "none", note: "M4: docker run --network none; loopback providers inside the container are reachable, everything else is not" }
        : { network: "inference", note: "REFUSED: an enforced egress-proxy boundary does not exist yet; network must be none" },
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
  if (!command || !["validate", "compose", "tree-hash"].includes(command)) {
    die(EXIT_POLICY, 'usage: pi-sandbox-policy.mjs <validate|compose|tree-hash> <allowlist.json|dir> [--profile <name>] [--task-file <f>|--task <s>]');
  }
  const file = args._[1];
  if (!file) die(EXIT_POLICY, "missing allowlist path or directory");

  if (command === "tree-hash") {
    try {
      const { digest, files, bytes } = await treeHash(resolve(file));
      console.log(JSON.stringify({ digest, files, bytes, version: TREE_PIN_VERSION }));
    } catch (error) {
      die(EXIT_POLICY, error.message);
    }
    process.exit(EXIT_OK);
  }

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
