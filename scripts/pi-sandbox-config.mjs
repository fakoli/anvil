#!/usr/bin/env node
// pi-sandbox-config.mjs — sandbox run-config resolution + fail-closed validation.
//
// A run config tunes the DOCKER PATH knobs that are otherwise hardcoded:
//   image (digest-pinned), network posture, capability preset, max containers.
// Security-relevant fields (image, network, caps) are TRUSTED-SCOPE ONLY:
//   explicit --config file or the user config (~/.config/anvil/sandbox.config.json).
// A project-local file (<workspace>/.pi/sandbox.config.json) may only set
//   max_containers — anything else there is a fail-closed refusal, because the
//   workspace is untrusted input and must not loosen container security.
//
// Resolution precedence:
//   --config (trusted)  >  user config  >  defaults
//   project file merges for max_containers only; the most restrictive wins.
//
// Output protocol (for the sh wrapper): one KEY<TAB>VALUE line per field.
// Values contain no tabs/newlines by construction.
// Exit codes: 0 ok · 2 policy/usage · 3 config struct.
// Stdlib node only; no subprocesses.

import { existsSync, readFileSync, statSync } from "node:fs";
import { homedir } from "node:os";
import { join, resolve as pathResolve } from "node:path";

const EXIT_OK = 0;
const EXIT_POLICY = 2;
const EXIT_STRUCT = 3;

const NETWORK_VALUES = ["none"]; // "inference" reserved, not implemented on the docker path yet
const CAPS_PRESETS = ["all-dropped", "docker-default"];
const MAX_CONTAINERS_MIN = 1;
const MAX_CONTAINERS_MAX = 256;
const IMAGE_RE = /^[a-zA-Z0-9][a-zA-Z0-9._/-]*@sha256:[a-f0-9]{64}$/;

const CONFIG_KEYS = ["image", "network", "caps", "max_containers"];
const PROJECT_ALLOWED_KEYS = ["max_containers"];

function fail(code, message) {
  process.stderr.write(`pi-sandbox-config: ${message}\n`);
  process.exit(code);
}

// ---- argv ------------------------------------------------------------------

function usage() {
  return [
    "usage: pi-sandbox-config.mjs resolve --profile <name> --workspace <dir>",
    "          [--allowlist <allowlist.json>] [--config <file>]",
    "       pi-sandbox-config.mjs validate --config <file> [--allowlist <allowlist.json>] [--profile <name>]",
  ].join("\n");
}

function parseArgv(argv) {
  const out = { _: [] };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--profile" || a === "--workspace" || a === "--allowlist" || a === "--config") {
      const v = argv[++i];
      if (v === undefined) fail(EXIT_POLICY, `missing value after ${a}`);
      out[a.slice(2)] = v;
    } else if (a.startsWith("--")) {
      fail(EXIT_POLICY, `unknown option ${a}`);
    } else {
      out._.push(a);
    }
  }
  return out;
}

// ---- file loading ----------------------------------------------------------

function loadJsonFile(file, label) {
  // Regular files only: a FIFO at a config path would block readFileSync
  // forever (hang = fail-closed by omission, but refuse loudly instead).
  let st;
  try {
    st = statSync(file);
  } catch (e) {
    fail(EXIT_POLICY, `${label}: cannot stat ${file}: ${e.code ?? e.message}`);
  }
  if (!st.isFile()) {
    fail(EXIT_POLICY, `${label}: ${file} is not a regular file`);
  }
  let raw;
  try {
    raw = readFileSync(file, "utf8");
  } catch (e) {
    fail(EXIT_POLICY, `${label}: cannot read ${file}: ${e.code ?? e.message}`);
  }
  let doc;
  try {
    doc = JSON.parse(raw);
  } catch (e) {
    fail(EXIT_STRUCT, `${label}: invalid JSON in ${file}: ${e.message}`);
  }
  if (doc === null || typeof doc !== "object" || Array.isArray(doc)) {
    fail(EXIT_STRUCT, `${label}: ${file} must contain a JSON object`);
  }
  return doc;
}

// ---- per-field validation --------------------------------------------------

function checkImage(config, where) {
  if (!("image" in config)) return;
  const v = config.image;
  if (typeof v !== "string" || !IMAGE_RE.test(v)) {
    fail(
      EXIT_STRUCT,
      `${where}: "image" must be a digest-pinned reference (<repo>@sha256:<64 hex>); got ${JSON.stringify(v)}`,
    );
  }
}

function checkNetwork(config, where, profileNetwork) {
  if (!("network" in config)) return;
  const v = config.network;
  if (typeof v !== "string" || !NETWORK_VALUES.includes(v)) {
    fail(
      EXIT_STRUCT,
      `${where}: "network" must be one of ${JSON.stringify(NETWORK_VALUES)} ("inference" is reserved and not implemented on the docker path); got ${JSON.stringify(v)}`,
    );
  }
  if (profileNetwork === "none" && v !== "none") {
    // Future-proofing: NETWORK_VALUES is ["none"] today, so this branch is
    // unreachable until "inference" ships — kept as the second gate so a
    // future enum widening cannot silently loosen a none-profile.
    fail(EXIT_POLICY, `${where}: network ${JSON.stringify(v)} is looser than the profile's network posture ("${profileNetwork}") — refusing`);
  }
}

function checkCaps(config, where) {
  if (!("caps" in config)) return;
  const v = config.caps;
  if (typeof v !== "string" || !CAPS_PRESETS.includes(v)) {
    fail(EXIT_STRUCT, `${where}: "caps" must be one of ${JSON.stringify(CAPS_PRESETS)}; got ${JSON.stringify(v)}`);
  }
}

function checkMaxContainers(config, where) {
  if (!("max_containers" in config)) return;
  const v = config.max_containers;
  if (!Number.isInteger(v) || v < MAX_CONTAINERS_MIN || v > MAX_CONTAINERS_MAX) {
    fail(EXIT_STRUCT, `${where}: "max_containers" must be an integer in [${MAX_CONTAINERS_MIN}, ${MAX_CONTAINERS_MAX}]; got ${JSON.stringify(v)}`);
  }
}

// ---- scope-checked validation ----------------------------------------------

// Validates a config document against its scope. `trusted` = user file or
// explicit --config; untrusted = project file (max_containers only).
function validateScoped(doc, where, trusted, profileNetwork) {
  const allowed = trusted ? CONFIG_KEYS : PROJECT_ALLOWED_KEYS;
  for (const key of Object.keys(doc)) {
    if (!allowed.includes(key)) {
      fail(
        EXIT_POLICY,
        trusted
          ? `${where}: unknown key "${key}" (allowed: ${CONFIG_KEYS.join(", ")})`
          : `${where}: project-scope config may only set ${PROJECT_ALLOWED_KEYS.join(", ")} — found "${key}" (security fields are trusted-scope only)`,
      );
    }
  }
  checkImage(doc, where);
  checkNetwork(doc, where, profileNetwork);
  checkCaps(doc, where);
  checkMaxContainers(doc, where);
  return doc;
}

// ---- profile network -------------------------------------------------------

function profileNetwork(allowlistPath, profileName) {
  if (!allowlistPath) return "none"; // default posture when no allowlist is supplied
  const doc = loadJsonFile(allowlistPath, "allowlist");
  const profile = doc?.profiles?.[profileName];
  if (!profile || typeof profile !== "object") {
    fail(EXIT_POLICY, `profile "${profileName}" not found in ${allowlistPath}`);
  }
  const net = profile.network ?? "none";
  if (typeof net !== "string") fail(EXIT_STRUCT, `allowlist: profile "${profileName}" has non-string network`);
  return net;
}

// ---- resolution ------------------------------------------------------------

function resolveConfig(args) {
  const { profile, workspace } = args;
  if (!profile) fail(EXIT_POLICY, usage());
  if (!workspace) fail(EXIT_POLICY, usage());
  const wsAbs = pathResolve(workspace);
  if (!existsSync(wsAbs)) fail(EXIT_POLICY, `workspace does not exist: ${wsAbs}`);

  const net = profileNetwork(args.allowlist, profile);
  const userFile = join(homedir(), ".config", "anvil", "sandbox.config.json");

  // Trusted-scope: explicit --config, else user file.
  let trusted = null;
  let trustedSource = "defaults";
  if (args.config) {
    const file = pathResolve(args.config);
    if (!existsSync(file)) fail(EXIT_POLICY, `--config file not found: ${file}`);
    trusted = validateScoped(loadJsonFile(file, "--config"), file, true, net);
    trustedSource = file;
  } else if (existsSync(userFile)) {
    trusted = validateScoped(loadJsonFile(userFile, "user config"), userFile, true, net);
    trustedSource = userFile;
  }

  // Project scope: max_containers only; most restrictive wins.
  let projectMax = null;
  const projectFile = join(wsAbs, ".pi", "sandbox.config.json");
  if (existsSync(projectFile)) {
    const doc = loadJsonFile(projectFile, "project config");
    const checked = validateScoped(doc, projectFile, false, net);
    if ("max_containers" in checked) projectMax = checked.max_containers;
  }

  let maxContainers = trusted?.max_containers ?? null;
  if (projectMax !== null) {
    maxContainers = maxContainers === null ? projectMax : Math.min(maxContainers, projectMax);
  }

  const resolved = {
    IMAGE: trusted?.image ?? "", // empty → docker script falls back to ANVIL_SANDBOX_IMAGE env or default
    NETWORK: net, // the docker path's posture (profile-derived; config may only confirm "none")
    CAPS: trusted?.caps ?? "all-dropped",
    MAX_CONTAINERS: maxContainers ?? "", // empty → no cap enforced
    CONFIG_SOURCE: trustedSource + (projectMax !== null ? " + project(max_containers)" : ""),
  };
  for (const [k, v] of Object.entries(resolved)) {
    if (String(v).includes("\t") || String(v).includes("\n")) {
      fail(EXIT_STRUCT, `internal: value for ${k} contains a tab/newline`);
    }
  }
  return resolved;
}

// ---- main ------------------------------------------------------------------

function main() {
  const args = parseArgv(process.argv.slice(2));
  const [verb] = args._;
  if (verb === "resolve") {
    const resolved = resolveConfig(args);
    for (const [k, v] of Object.entries(resolved)) {
      process.stdout.write(`${k}\t${v}\n`);
    }
    process.exit(EXIT_OK);
  }
  if (verb === "validate") {
    if (!args.config) fail(EXIT_POLICY, usage());
    const net = profileNetwork(args.allowlist, args.profile ?? "");
    validateScoped(loadJsonFile(args.config, "--config"), args.config, true, net);
    process.stdout.write(`OK\t${args.config}\n`);
    process.exit(EXIT_OK);
  }
  fail(EXIT_POLICY, usage());
}

main();