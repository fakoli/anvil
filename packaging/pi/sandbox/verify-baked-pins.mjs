#!/usr/bin/env node
// verify-baked-pins.mjs — build-time pin verification for the sandbox image.
// Runs INSIDE the image build: recomputes tree digests for every tree_sha256
// pin in the baked allowlist and fails the build on any drift. (Runtime
// verification still happens per container start — this is the first gate.)
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";

const SANDBOX_ROOT = "/opt/anvil-sandbox";
const allowlistPath = resolve(SANDBOX_ROOT, "packaging/pi/sandbox/allowlist.json");
const doc = JSON.parse(readFileSync(allowlistPath, "utf8"));
const policy = resolve(SANDBOX_ROOT, "scripts/pi-sandbox-policy.mjs");

let checked = 0;
for (const [name, profile] of Object.entries(doc.profiles ?? {})) {
  for (const entry of profile.extensions ?? []) {
    if (entry.tree_sha256 === undefined) continue;
    const dir = resolve(
      dirname(allowlistPath),
      entry.source.slice("path:".length),
      ".."
    );
    const out = JSON.parse(execFileSync("node", [policy, "tree-hash", dir]).toString());
    if (out.digest !== entry.tree_sha256) {
      console.error(`DRIFT: ${name} ${entry.source}: expected ${entry.tree_sha256}, got ${out.digest}`);
      process.exit(1);
    }
    checked += 1;
  }
}
console.log(`build-time pin verification: OK (${checked} tree pin(s))`);