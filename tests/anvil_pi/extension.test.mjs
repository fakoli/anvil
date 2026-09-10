#!/usr/bin/env node
// anvil-pi extension tests — node + jiti (from the pi harness install), with a
// recording fake `anvil` on PATH. Covers tool→CLI mapping, verb allowlists,
// planning gate, truncation, snapshot bounds, and extension wiring.

import * as assert from "node:assert/strict";
import { chmodSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { join, resolve as resolvePath } from "node:path";

const PI_INSTALL_DIR = process.env.PI_INSTALL_DIR
  ?? "/data/apps/devtools/node-24.20.0/lib/node_modules/@earendil-works/pi-coding-agent";
const require = createRequire(import.meta.url);
let createJiti;
try {
  ({ createJiti } = require(join(PI_INSTALL_DIR, "node_modules/jiti/lib/jiti.cjs")));
} catch {
  ({ createJiti } = require("jiti"));
}
const base = resolvePath(new URL("../../packaging/pi/anvil-pi/extension.ts", import.meta.url).pathname);
const jiti = createJiti(base, {
  interopDefault: true,
  fsCache: false,
  alias: {
    "@earendil-works/pi-ai": `${PI_INSTALL_DIR}/node_modules/@earendil-works/pi-ai/dist/index.js`,
    "@earendil-works/pi-coding-agent": `${PI_INSTALL_DIR}/dist/index.js`,
  },
});

const scratch = mkdtempSync(join(tmpdir(), "anvil-pi-test."));
process.on("exit", () => rmSync(scratch, { recursive: true, force: true }));

// --- fake anvil on PATH --------------------------------------------------------

const fakeBinDir = join(scratch, "bin");
mkdirSync(fakeBinDir, { recursive: true });
const recordDir = join(scratch, "record");
mkdirSync(recordDir, { recursive: true });
const fixtureDir = join(scratch, "fixtures");
mkdirSync(fixtureDir, { recursive: true });
const STATUS_FIXTURE = JSON.stringify({
  project: "demo", tasks: { claimed: 1, ready: 2 }, claims: [{ task_id: "T001", actor: "alice" }],
});
writeFileSync(join(fixtureDir, "status.json"), STATUS_FIXTURE);
writeFileSync(join(fakeBinDir, "anvil"), `#!/bin/sh
verb="$1"
printf '%s\\n' "$@" > "${recordDir}/last-args"
[ -n "$ANVIL_FAKE_EXIT" ] && { echo "boom: $verb" >&2; exit "$ANVIL_FAKE_EXIT"; }
if [ -f "${fixtureDir}/$verb.json" ]; then cat "${fixtureDir}/$verb.json"; else echo '{"ok":true}'; fi
`);
chmodSync(join(fakeBinDir, "anvil"), 0o755);

const envWithFake = (extra = {}) => ({
  ...process.env,
  PATH: `${fakeBinDir}:${process.env.PATH}`,
  ...extra,
});

function readLastArgs() {
  return readFileSync(join(recordDir, "last-args"), "utf8");
}

// --- import modules --------------------------------------------------------------

const tools = await jiti.import(resolvePath(new URL("../../packaging/pi/anvil-pi/tools.ts", import.meta.url).pathname));
const extensionMod = await jiti.import(resolvePath(new URL("../../packaging/pi/anvil-pi/extension.ts", import.meta.url).pathname));

let passed = 0;
function test(name, fn) {
  try {
    const outcome = fn();
    if (outcome instanceof Promise) {
      return outcome.then(() => {
        passed += 1;
        console.log(`  ok ${name}`);
      });
    }
    passed += 1;
    console.log(`  ok ${name}`);
    return undefined;
  } catch (error) {
    console.error(`  FAIL ${name}`);
    throw error;
  }
}

// --- verb policy -------------------------------------------------------------------

test("execution verbs allowed without planning gate", () => {
  for (const verb of ["status", "next", "claim", "packet", "submit", "apply", "doctor", "gate_check"]) {
    assert.equal(tools.checkVerb(verb, {}), null, verb);
  }
});

test("planning verbs denied by default, allowed with ANVIL_PI_PLANNING=1", () => {
  assert.match(tools.checkVerb("plan", {}), /ANVIL_PI_PLANNING/);
  assert.equal(tools.checkVerb("plan", { ANVIL_PI_PLANNING: "1" }), null);
  assert.equal(tools.checkVerb("prd", { ANVIL_PI_PLANNING: "yes" }), null);
});

test("config/state-mutating verbs denied even with planning gate", () => {
  for (const verb of ["install", "mcp_config", "hooks", "restore", "migrate", "replay", "run_workflow", "backup"]) {
    assert.match(tools.checkVerb(verb, { ANVIL_PI_PLANNING: "1" }), /always denied/, verb);
  }
});

test("unknown and malformed verbs rejected", () => {
  assert.match(tools.checkVerb("teleport", {}), /not in the anvil-pi allowlist/);
  assert.match(tools.checkVerb("Bad Verb", {}), /invalid verb/);
});

// --- runAnvil: CLI mapping ------------------------------------------------------------

test("runAnvil appends --json and passes args in order", () => {
  const result = tools.runAnvil("claim", ["T001", "--actor", "alice"], envWithFake());
  assert.equal(result.ok, true, result.stderr);
  const recorded = readLastArgs().split("\n").filter(Boolean);
  assert.deepEqual(recorded, ["claim", "T001", "--actor", "alice", "--json"]);
});

test("runAnvil honors ANVIL_BIN override", () => {
  const result = tools.runAnvil("status", [], { ...envWithFake(), ANVIL_BIN: join(fakeBinDir, "anvil") });
  assert.equal(result.ok, true);
});

test("runAnvil fails closed on denied verb without spawning", () => {
  rmSync(join(recordDir, "last-args"), { force: true });
  const result = tools.runAnvil("install", [], envWithFake());
  assert.equal(result.ok, false);
  assert.equal(result.exitCode, -1);
  assert.match(result.stderr, /always denied/);
  assert.equal(existsSync(join(recordDir, "last-args")), false, "fake anvil must not have run");
});

test("runAnvil enforces arg caps", () => {
  const tooMany = tools.runAnvil("status", Array.from({ length: 33 }, (_, i) => `a${i}`), envWithFake());
  assert.equal(tooMany.ok, false);
  assert.match(tooMany.stderr, /too many args/);
  const tooLong = tools.runAnvil("status", ["x".repeat(201)], envWithFake());
  assert.equal(tooLong.ok, false);
  assert.match(tooLong.stderr, /size caps/);
});

test("runAnvil surfaces CLI stderr verbatim on failure", () => {
  const result = tools.runAnvil("status", [], envWithFake({ ANVIL_FAKE_EXIT: "3" }));
  assert.equal(result.ok, false);
  assert.equal(result.exitCode, 3);
  assert.match(result.stderr, /boom: status/);
});

// --- presentResult ---------------------------------------------------------------------

test("presentResult passes stdout through and marks errors", () => {
  const ok = tools.presentResult({ ok: true, stdout: '{"a":1}', stderr: "", exitCode: 0 });
  assert.equal(ok.isError, false);
  assert.equal(ok.text, '{"a":1}');
  const bad = tools.presentResult({ ok: false, stdout: "", stderr: "claim rejected: leased by bob", exitCode: 1 });
  assert.equal(bad.isError, true);
  assert.equal(bad.text, "anvil error: claim rejected: leased by bob");
});

test("presentResult truncates oversized stdout with a marker", () => {
  const big = JSON.stringify({ blob: "x".repeat(tools.TOOL_OUTPUT_MAX_BYTES + 5000) });
  const out = tools.presentResult({ ok: true, stdout: big, stderr: "", exitCode: 0 });
  assert.ok(out.text.length < big.length);
  assert.match(out.text, /\[truncated:/);
});

// --- snapshot -----------------------------------------------------------------------------

test("sessionSnapshot returns null without anvil state", () => {
  assert.equal(tools.sessionSnapshot({ PATH: process.env.PATH }, join(scratch, "nowhere")), null);
});

test("sessionSnapshot returns bounded status JSON with state present", () => {
  const snap = tools.sessionSnapshot(envWithFake({ ANVIL_ROOT: scratch }), scratch);
  assert.ok(snap.includes("anvil session snapshot"), `snap: ${snap}`);
  assert.ok(snap.includes('"project":"demo"'));
  assert.ok(snap.length <= tools.SNAPSHOT_MAX_CHARS + 100);
});

test("sessionSnapshot caps oversized status output", () => {
  writeFileSync(join(fixtureDir, "status.json"), JSON.stringify({ blob: "y".repeat(20000) }));
  const snap = tools.sessionSnapshot(envWithFake({ ANVIL_ROOT: scratch }), scratch);
  assert.ok(snap.length <= tools.SNAPSHOT_MAX_CHARS + 2);
  assert.ok(snap.endsWith("…"));
  writeFileSync(join(fixtureDir, "status.json"), STATUS_FIXTURE);
});

test("sessionSnapshot reports status failure when the CLI fails", () => {
  const snap = tools.sessionSnapshot(envWithFake({ ANVIL_ROOT: scratch, ANVIL_FAKE_EXIT: "5" }), scratch);
  assert.match(snap, /status failed/);
  assert.match(snap, /boom: status/);
});

// --- extension wiring -------------------------------------------------------------------------
// Extension tools read env at call time (process.env), so the wiring tests
// point the real process.env at the fake anvil and restore it afterwards.

const savedEnv = new Map();
function useFakeEnv(extra = {}) {
  for (const [key, value] of Object.entries(extra)) {
    savedEnv.set(key, process.env[key]);
    process.env[key] = value;
  }
  savedEnv.set("PATH", process.env.PATH);
  process.env.PATH = `${fakeBinDir}:${process.env.PATH}`;
  savedEnv.set("ANVIL_ROOT", process.env.ANVIL_ROOT);
  process.env.ANVIL_ROOT = scratch;
}

function restoreEnv() {
  for (const [key, value] of savedEnv) {
    if (value === undefined) delete process.env[key];
    else process.env[key] = value;
  }
  savedEnv.clear();
}


function makePi() {
  const registry = { tools: {}, commands: {}, handlers: {} };
  return {
    registry,
    registerTool: (def) => { registry.tools[def.name] = def; },
    registerCommand: (name, def) => { registry.commands[name] = def; },
    on: (name, fn) => { registry.handlers[name] = fn; },
  };
}

function makeCtx(env, overrides = {}) {
  return {
    hasUI: true,
    cwd: scratch,
    ui: { notifications: [], notify(msg, level) { this.notifications.push({ msg, level }); } },
    env,
    ...overrides,
  };
}

await test("extension registers 6 coarse tools + anvil_run and 4 commands", async () => {
  const pi = makePi();
  await extensionMod.default(pi);
  for (const name of ["anvil_status", "anvil_next", "anvil_claim", "anvil_packet", "anvil_submit", "anvil_apply", "anvil_run"]) {
    assert.ok(pi.registry.tools[name], name);
  }
  for (const name of ["anvil:status", "anvil:next", "anvil:claim", "anvil:submit"]) {
    assert.ok(pi.registry.commands[name], name);
  }
});

await test("anvil_status tool returns CLI JSON as text", async () => {
  useFakeEnv();
  try {
  const pi = makePi();
  await extensionMod.default(pi);
  const result = await pi.registry.tools.anvil_status.execute("t1", {}, undefined, undefined, makeCtx({}));
  assert.match(result.content[0].text, /"project":"demo"/);
  assert.equal(result.details.isError, false);
  } finally { restoreEnv(); }
});
await test("anvil_run tool: denied verbs reach the model as errors, no spawn", async () => {
  useFakeEnv({ ANVIL_PI_PLANNING: "1" });
  try {
  const pi = makePi();
  await extensionMod.default(pi);
  const ctx = makeCtx({ ANVIL_PI_PLANNING: "1" });
  const denied = await pi.registry.tools.anvil_run.execute("t1", { verb: "install" }, undefined, undefined, ctx);
  assert.equal(denied.details.isError, true);
  assert.match(denied.content[0].text, /always denied/);
  const allowed = await pi.registry.tools.anvil_run.execute("t1", { verb: "gate_check" }, undefined, undefined, ctx);
  assert.equal(allowed.details.isError, false);
  const planning = await pi.registry.tools.anvil_run.execute("t1", { verb: "plan" }, undefined, undefined, ctx);
  assert.equal(planning.details.isError, false); // gate is on
  delete process.env.ANVIL_PI_PLANNING; // gated case: planning off
  const gated = await pi.registry.tools.anvil_run.execute("t1", { verb: "plan" }, undefined, undefined, makeCtx({}));
  assert.match(gated.content[0].text, /ANVIL_PI_PLANNING/);
  } finally { restoreEnv(); }
});

await test("anvil_claim tool maps optional params to CLI flags", async () => {
  useFakeEnv();
  try {
  const pi = makePi();
  await extensionMod.default(pi);
  const ctx = makeCtx({});
  await pi.registry.tools.anvil_claim.execute("t1", { task_id: "T002", actor: "bob", lease_minutes: 30, force: true }, undefined, undefined, ctx);
  assert.deepEqual(
    readLastArgs().split("\n").filter(Boolean),
    ["claim", "T002", "--actor", "bob", "--lease", "30", "--force", "--json"],
  );
  } finally { restoreEnv(); }
});

await test("commands are silent in print mode (ctx.hasUI false)", async () => {
  const pi = makePi();
  await extensionMod.default(pi);
  const ctx = makeCtx(envWithFake({ ANVIL_ROOT: scratch }), { hasUI: false });
  await pi.registry.commands["anvil:status"].handler("", ctx);
  await pi.registry.commands["anvil:claim"].handler("T001", ctx);
  assert.deepEqual(ctx.ui.notifications, []);
});

await test("commands notify in TUI mode and guard usage errors", async () => {
  useFakeEnv();
  try {
  const pi = makePi();
  await extensionMod.default(pi);
  const ctx = makeCtx({});
  await pi.registry.commands["anvil:claim"].handler("", ctx);
  assert.match(ctx.ui.notifications[0].msg, /usage: \/anvil:claim/);
  await pi.registry.commands["anvil:claim"].handler("T001", ctx);
  assert.match(ctx.ui.notifications[1].msg, /"ok":true/);
  await pi.registry.commands["anvil:submit"].handler("T003 --commands pytest", ctx);
  assert.match(ctx.ui.notifications[2].msg, /"ok":true/);
  assert.match(readLastArgs(), /--commands/);
  } finally { restoreEnv(); }
});

await test("before_agent_start injects bounded snapshot once per session", async () => {
  useFakeEnv();
  try {
  const pi = makePi();
  await extensionMod.default(pi);
  const ctx = makeCtx({});
  const first = await pi.registry.handlers.before_agent_start({}, ctx);
  assert.ok(first.message.content.includes("anvil session snapshot"));
  assert.equal(first.message.customType, "anvil-pi.snapshot");
  assert.equal(first.message.display, true);
  const second = await pi.registry.handlers.before_agent_start({}, ctx);
  assert.equal(second, undefined, "must inject only once per session");
  } finally { restoreEnv(); }
});

await test("before_agent_start injects nothing without anvil state", async () => {
  const pi = makePi();
  await extensionMod.default(pi);
  const ctx = makeCtx({ PATH: process.env.PATH }, { cwd: join(scratch, "nowhere") });
  const result = await pi.registry.handlers.before_agent_start({}, ctx);
  assert.equal(result, undefined);
});

console.log(`\n${passed} anvil-pi tests passed`);
