#!/usr/bin/env node
// pi-sandbox-policy.mjs — policy brain for scripts/pi-sandbox-run.sh.
//
// Subcommands (all fail closed):
//   validate   <allowlist.json>
//       Structural validation against the contract documented in
//       packaging/pi/sandbox/allowlist.schema.json. Exit 2 on any problem.
//   pin-verify <allowlist.json> --profile <name> [--offline]
//       Verify every extension pin in the profile. path:/npm: entries need a
//       sha256 that matches the resolved artifact; git: entries need ref+commit
//       and (unless --offline) a ls-remote confirmation that the ref still
//       resolves to the pinned commit. Exit 2 (missing/invalid pin) or 3
//       (mismatch) — a profile with NO extension entries passes trivially.
//   compose    <allowlist.json> --profile <name> (--task-file <file> | --task <text>)
//       Print {"argv": [...], "env": {...}, "docker": {...}} for the launcher.
//       Exit 2 on any policy problem.
//
// Design notes:
// - No dependencies (node stdlib only) so sandbox verification itself has no
//   supply-chain surface.
// - The composed argv uses pi's strict-flag recipe: --no-extensions collapses
//   discovery to explicit -e paths only; --tools is a strict allowlist;
//   PI_CODING_AGENT_DIR points at a fresh container-local dir so no user or
//   project extensions/skills/settings can leak in; -na ignores project-local
//   settings for this run.

import { createHash } from "node:crypto";
import { readFile, stat } from "node:fs/promises";
import { execFile } from "node:child_process";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);

const EXIT_OK = 0;
const EXIT_POLICY = 2;
const EXIT_MISMATCH = 3;
const EXIT_DEPENDENCY = 4;

function die(code, message) {
  console.error(`pi-sandbox-policy: ${message}`);
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

// --- validation -------------------------------------------------------------

function validateAllowlist(raw, label) {
  const problems = [];
  let doc;
  try {
    doc = JSON.parse(raw);
  } catch (error) {
    die(EXIT_POLICY, `${label}: not valid JSON: ${error.message}`);
  }
  if (doc.version !== 1) problems.push(`version must be 1, got ${JSON.stringify(doc.version)}`);
  if (typeof doc.profiles !== "object" || doc.profiles === null || Array.isArray(doc.profiles)) {
    problems.push("profiles must be an object");
  } else if (Object.keys(doc.profiles).length === 0) {
    problems.push("profiles must not be empty");
  } else {
    for (const [name, profile] of Object.entries(doc.profiles)) {
      problems.push(...validateProfile(name, profile));
    }
  }
  return { doc, problems };
}

function validateProfile(name, profile) {
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
  if (!["none", "inference"].includes(profile.network)) {
    problems.push(`${where}: network must be "none" or "inference"`);
  }
  if (profile.projectTrust !== undefined && profile.projectTrust !== "never" && profile.projectTrust !== "always") {
    problems.push(`${where}: projectTrust must be "never" or "always"`);
  }
  const allowed = new Set(["description", "extensions", "tools", "skills", "network", "projectTrust"]);
  for (const key of Object.keys(profile)) {
    if (!allowed.has(key)) problems.push(`${where}: unknown key "${key}"`);
  }
  return problems;
}

function validateExtensionPin(where, entry) {
  if (typeof entry !== "object" || entry === null || Array.isArray(entry)) {
    return [`${where}: must be an object`];
  }
  const problems = [];
  if (typeof entry.source !== "string" || !/^(path:|npm:|git:)/.test(entry.source) || entry.source.length < 5) {
    problems.push(`${where}: source must start with path:, npm:, or git:`);
    return problems;
  }
  const allowed = new Set(["source", "sha256", "ref", "commit"]);
  for (const key of Object.keys(entry)) {
    if (!allowed.has(key)) problems.push(`${where}: unknown key "${key}"`);
  }
  const sha = typeof entry.sha256 === "string" ? entry.sha256 : "";
  if (!/^[a-f0-9]{64}$/.test(sha)) problems.push(`${where}: sha256 must be 64 lowercase hex chars`);
  if (entry.source.startsWith("git:")) {
    if (typeof entry.ref !== "string" || entry.ref.length === 0) problems.push(`${where}: git pins need a "ref"`);
    if (typeof entry.commit !== "string" || !/^[a-f0-9]{40}$/.test(entry.commit ?? "")) {
      problems.push(`${where}: git pins need a 40-hex "commit"`);
    }
  } else if (!sha) {
    problems.push(`${where}: path:/npm: pins need sha256`);
  }
  return problems;
}

// --- pin verification ---------------------------------------------------------

async function sha256File(path) {
  const buf = await readFile(path);
  return createHash("sha256").update(buf).digest("hex");
}

async function sha256Url(url) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`fetch ${url}: HTTP ${response.status}`);
  const buf = Buffer.from(await response.arrayBuffer());
  return createHash("sha256").update(buf).digest("hex");
}

async function npmTarballUrl(spec) {
  const { stdout } = await execFileAsync("npm", ["view", spec, "dist.tarball", "--json"], { encoding: "utf8" });
  const url = JSON.parse(stdout);
  if (typeof url !== "string" || !url.startsWith("https://")) throw new Error(`npm view ${spec}: unexpected tarball ${url}`);
  return url;
}

async function gitResolve(ownerRepo, ref) {
  const { stdout } = await execFileAsync("git", ["ls-remote", `https://github.com/${ownerRepo}`, ref], { encoding: "utf8" });
  const line = stdout.split("\n").find((l) => l.includes(`refs/tags/${ref}`) || l.includes(`refs/heads/${ref}`)) ?? stdout.split("\n")[0];
  const oid = line?.split("\t")[0]?.trim();
  if (!oid || !/^[a-f0-9]{40}$/.test(oid)) throw new Error(`git ls-remote ${ownerRepo} ${ref}: no resolvable oid`);
  return oid;
}

async function verifyEntry(entry, profileName, index, offline) {
  const label = `profile "${profileName}" extensions[${index}] ${entry.source}`;
  if (entry.source.startsWith("path:")) {
    const path = entry.source.slice("path:".length);
    let actual;
    try {
      const info = await stat(path);
      if (!info.isFile()) throw new Error("not a regular file");
      actual = await sha256File(path);
    } catch (error) {
      return { label, status: "missing", detail: error.message };
    }
    if (actual !== entry.sha256) return { label, status: "mismatch", detail: `expected ${entry.sha256}, got ${actual}` };
    return { label, status: "ok", detail: actual };
  }
  if (entry.source.startsWith("npm:")) {
    if (offline) return { label, status: "skipped-offline", detail: "npm pin verified at image build time" };
    try {
      const url = await npmTarballUrl(entry.source.slice("npm:".length));
      const actual = await sha256Url(url);
      if (actual !== entry.sha256) return { label, status: "mismatch", detail: `expected ${entry.sha256}, got ${actual}` };
      return { label, status: "ok", detail: actual };
    } catch (error) {
      return { label, status: "dependency", detail: error.message };
    }
  }
  if (entry.source.startsWith("git:")) {
    const ownerRepo = entry.source.slice("git:".length);
    if (offline) {
      return { label, status: "skipped-offline", detail: `pinned commit ${entry.commit} verified at build time` };
    }
    try {
      const oid = await gitResolve(ownerRepo, entry.ref);
      if (oid !== entry.commit) return { label, status: "mismatch", detail: `ref ${entry.ref} now resolves to ${oid}, pinned ${entry.commit}` };
      return { label, status: "ok", detail: oid };
    } catch (error) {
      return { label, status: "dependency", detail: error.message };
    }
  }
  return { label, status: "missing", detail: "unreachable" };
}

// --- compose ------------------------------------------------------------------

function composeArgv(profile, profileName, task) {
  const argv = ["pi", "--no-extensions"];
  for (const entry of profile.extensions) {
    if (entry.source.startsWith("path:")) argv.push("-e", entry.source.slice("path:".length));
    else argv.push("-e", entry.source); // npm:/git: resolve via pi's strict -e loader
  }
  argv.push("--tools", profile.tools.join(","));
  if (Array.isArray(profile.skills) && profile.skills.length > 0) {
    for (const skill of profile.skills) argv.push("--skill", skill);
  } else {
    argv.push("--no-skills");
  }
  argv.push("-na", "--mode", "json", "-p", task);
  const env = {
    PI_CODING_AGENT_DIR: "$SANDBOX_AGENT_DIR", // launcher substitutes a fresh dir
  };
  if (profile.projectTrust) env.PI_DEFAULT_PROJECT_TRUST = profile.projectTrust;
  const docker =
    profile.network === "none"
      ? { network: "none", note: "containerization lands in M4; argv runs on host today" }
      : { network: "inference-only-allowlist", note: "egress restricted to provider endpoints; enforced in M4" };
  return { profile: profileName, argv, env, docker };
}

// --- main -----------------------------------------------------------------------

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const command = args._[0];
  if (!command || !["validate", "pin-verify", "compose"].includes(command)) {
    die(EXIT_POLICY, "usage: pi-sandbox-policy.mjs <validate|pin-verify|compose> <allowlist.json> [--profile <name>] [--offline] [--task-file <f>|--task <s>]");
  }
  const file = args._[1];
  if (!file) die(EXIT_POLICY, "missing allowlist path");

  if (command === "validate") {
    const { problems } = validateAllowlist(await readFile(file, "utf8"), file);
    if (problems.length > 0) {
      for (const problem of problems) console.error(`INVALID: ${problem}`);
      process.exit(EXIT_POLICY);
    }
    console.log(`OK: ${file} validates`);
    process.exit(EXIT_OK);
  }

  const { doc, problems } = validateAllowlist(await readFile(file, "utf8"), file);
  if (problems.length > 0) {
    for (const problem of problems) console.error(`INVALID: ${problem}`);
    process.exit(EXIT_POLICY);
  }
  const profileName = args.profile;
  const profile = doc.profiles[profileName];
  if (!profile) die(EXIT_POLICY, `profile "${profileName}" not in ${file} (have: ${Object.keys(doc.profiles).join(", ")})`);

  if (command === "pin-verify") {
    let sawMismatch = false;
    let sawDependency = false;
    let sawMissing = false;
    for (const [index, entry] of profile.extensions.entries()) {
      const result = await verifyEntry(entry, profileName, index, args.offline === true);
      console.log(`${result.status.toUpperCase()}: ${result.label} — ${result.detail}`);
      if (result.status === "mismatch") sawMismatch = true;
      if (result.status === "dependency") sawDependency = true;
      if (result.status === "missing") sawMissing = true;
    }
    if (sawMismatch) process.exit(EXIT_MISMATCH);
    if (sawDependency) die(EXIT_DEPENDENCY, "pin verification could not reach a registry; refusing to compose (fail closed)");
    if (sawMissing) die(EXIT_POLICY, "pinned extension missing or unreadable; refusing (fail closed)");
    process.exit(EXIT_OK);
  }

  // compose
  let task = "";
  if (args["task-file"]) task = await readFile(args["task-file"], "utf8");
  else if (typeof args.task === "string") task = args.task;
  else die(EXIT_POLICY, "compose needs --task-file <file> or --task <text>");
  task = task.trim();
  if (task.length === 0) die(EXIT_POLICY, "task is empty");
  if (task.length > 100_000) die(EXIT_POLICY, "task exceeds 100k chars");
  console.log(JSON.stringify(composeArgv(profile, profileName, task), null, 2));
  process.exit(EXIT_OK);
}

main().catch((error) => die(EXIT_POLICY, error.message));