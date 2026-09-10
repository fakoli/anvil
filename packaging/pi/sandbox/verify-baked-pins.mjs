#!/usr/bin/env node
// verify-baked-pins.mjs — build-time pin verification for the sandbox image.
// Runs INSIDE the image build: structurally validates the baked allowlist and
// verifies EVERY supported pin (flat sha256 + tree_sha256) against the baked
// bytes — the build FAILS on any drift. (Runtime verification still happens
// per container start; this is the first, independent gate.)
import { execFileSync } from "node:child_process";
import { dirname, resolve } from "node:path";
import { readFileSync } from "node:fs";

const SANDBOX_ROOT = "/opt/anvil-sandbox";
const sandboxDir = resolve(SANDBOX_ROOT, "packaging/pi/sandbox");
const allowlistPath = resolve(sandboxDir, "allowlist.json");
const policy = resolve(SANDBOX_ROOT, "scripts/pi-sandbox-policy.mjs");

// 1. Structural validation (same rules as the launcher).
execFileSync("node", [policy, "validate", allowlistPath], { stdio: "inherit" });

// 2. Per-entry verification via the policy's own readPinSnapshot — the exact
// acceptance rules staging will enforce later.
const { readPinSnapshot } = await import(
  `file://${policy}?verify=${Date.now()}`
);
const doc = JSON.parse(readFileSync(allowlistPath, "utf8"));
let checked = 0;
const problems = [];
for (const [name, profile] of Object.entries(doc.profiles ?? {})) {
  for (const [index, entry] of (profile.extensions ?? []).entries()) {
    try {
      const snapshot = await readPinSnapshot(entry, allowlistPath);
      checked += 1;
      console.log(`OK: ${name}.extensions[${index}] ${entry.source} (${snapshot.kind}, ${snapshot.bytes} bytes)`);
    } catch (error) {
      problems.push(`${name}.extensions[${index}] ${entry.source}: ${error.message}`);
    }
  }
}
if (problems.length > 0) {
  for (const problem of problems) console.error(`DRIFT: ${problem}`);
  process.exit(1);
}
console.log(`build-time pin verification: OK (${checked} pin(s) verified)`);
