#!/usr/bin/env node
// anvil-pi extension tests — node + jiti (from the pi harness install), with a
// recording fake `anvil` on PATH. Covers tool→CLI mapping, verb allowlists
// (REGISTERED Typer names), planning gate, truncation, snapshot bounds,
// silent modes, cwd threading, abort, and extension wiring.

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
pwd > "${recordDir}/last-cwd"
[ -n "$ANVIL_FAKE_EXIT" ] && {
  if [ -n "$ANVIL_BIG_STDERR" ]; then
    head -c "$ANVIL_BIG_STDERR" /dev/zero | tr '\\0' 'E' >&2
  else
    echo "boom: $verb" >&2
  fi
  exit "$ANVIL_FAKE_EXIT"
}
if [ -n "$ANVIL_SLEEP" ]; then sleep "$ANVIL_SLEEP"; fi
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
async function test(name, fn) {
  try {
    await fn();
    passed += 1;
    console.log(`  ok ${name}`);
  } catch (error) {
    console.error(`  FAIL ${name}`);
    throw error;
  }
}

// --- verb policy (REGISTERED Typer names) ------------------------------------------

test("execution verbs allowed without planning gate (registered names)", () => {
  for (const verb of ["status", "next", "claim", "packet", "submit", "apply", "doctor", "gate-check", "drift", "claim-guard", "merge-check"]) {
    assert.equal(tools.checkVerb(verb, {}), null, verb);
  }
});

test("planning verbs denied by default, allowed with ANVIL_PI_PLANNING=1", () => {
  assert.match(tools.checkVerb("plan", {}), /ANVIL_PI_PLANNING/);
  assert.equal(tools.checkVerb("plan", { ANVIL_PI_PLANNING: "1" }), null);
  assert.equal(tools.checkVerb("prd", { ANVIL_PI_PLANNING: "yes" }), null);
  assert.equal(tools.checkVerb("list", { ANVIL_PI_PLANNING: "on" }), null, "registered name is `list`, not list_tasks");
});

test("config/state-mutating verbs denied even with planning gate (registered names)", () => {
  for (const verb of ["install", "mcp-config", "hook", "restore", "migrate", "migrate-events", "migrate-workspace", "replay", "run-workflow", "backup"]) {
    assert.match(tools.checkVerb(verb, { ANVIL_PI_PLANNING: "1" }), /always denied/, verb);
  }
});

test("unclassified verbs fail closed regardless of gate", () => {
  for (const verb of ["sync", "proof", "project", "notify-digest"]) {
    assert.match(tools.checkVerb(verb, { ANVIL_PI_PLANNING: "1" }), /not yet classified/, verb);
  }
});

test("unknown and malformed verbs rejected; hyphens accepted", () => {
  assert.match(tools.checkVerb("teleport", {}), /not in the anvil-pi allowlist/);
  assert.match(tools.checkVerb("Bad Verb", {}), /invalid verb/);
  assert.equal(tools.checkVerb("gate-check", {}), null);
});

// --- runAnvil: CLI mapping ------------------------------------------------------------

await test("runAnvil appends --json and passes args in order", async () => {
  const result = await tools.runAnvil("claim", ["T001", "--actor", "alice"], envWithFake());
  assert.equal(result.ok, true, result.stderr);
  const recorded = readLastArgs().split("\n").filter(Boolean);
  assert.deepEqual(recorded, ["claim", "T001", "--actor", "alice", "--json"]);
});

await test("runAnvil honors ANVIL_BIN override", async () => {
  const result = await tools.runAnvil("status", [], { ...envWithFake(), ANVIL_BIN: join(fakeBinDir, "anvil") });
  assert.equal(result.ok, true);
});

await test("runAnvil fails closed on denied verb without spawning", async () => {
  rmSync(join(recordDir, "last-args"), { force: true });
  const result = await tools.runAnvil("install", [], envWithFake());
  assert.equal(result.ok, false);
  assert.equal(result.exitCode, -1);
  assert.match(result.stderr, /always denied/);
  assert.equal(existsSync(join(recordDir, "last-args")), false, "fake anvil must not have run");
});

await test("runAnvil enforces per-caller arg caps", async () => {
  const tooMany = await tools.runAnvil("status", Array.from({ length: 33 }, (_, i) => `a${i}`), envWithFake());
  assert.equal(tooMany.ok, false);
  assert.match(tooMany.stderr, /too many args/);
  const escapeHatch = await tools.runAnvil("status", ["x".repeat(201)], envWithFake());
  assert.equal(escapeHatch.ok, false, "anvil_run default cap is 200 chars/arg");
  const wrapper = await tools.runAnvil("submit", ["x".repeat(1500)], envWithFake(), undefined, undefined, { maxArgChars: 2000, maxTotalChars: 8000 });
  assert.equal(wrapper.ok, true, "structured wrappers allow 2000-char args");
});

await test("runAnvil rejects --json and bare -- in args (flag displacement)", async () => {
  const stolen = await tools.runAnvil("status", ["--json"], envWithFake());
  assert.equal(stolen.ok, false);
  assert.match(stolen.stderr, /--json/);
  const terminator = await tools.runAnvil("status", ["--"], envWithFake());
  assert.equal(terminator.ok, false);
});

await test("runAnvil surfaces CLI stderr verbatim on failure", async () => {
  const result = await tools.runAnvil("status", [], envWithFake({ ANVIL_FAKE_EXIT: "3" }));
  assert.equal(result.ok, false);
  assert.equal(result.exitCode, 3);
  assert.match(result.stderr, /boom: status/);
});

await test("runAnvil surfaces spawn errors (binary missing)", async () => {
  const result = await tools.runAnvil("status", [], { PATH: "/nonexistent-dir" });
  assert.equal(result.ok, false);
  assert.equal(result.exitCode, -1);
  assert.ok(result.stderr.length > 0);
});

// --- abort + async behavior -------------------------------------------------------------

await test("abort signal terminates a hanging anvil call quickly", async () => {
  const controller = new AbortController();
  const started = Date.now();
  const pending = tools.runAnvil("status", [], envWithFake({ ANVIL_SLEEP: "30" }), undefined, controller.signal);
  setTimeout(() => controller.abort(), 150);
  const result = await pending;
  const elapsed = Date.now() - started;
  assert.equal(result.ok, false);
  assert.ok(elapsed < 5000, `abort took ${elapsed}ms`);
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

test("presentResult caps oversized stdout AND oversized stderr", () => {
  const bigOut = JSON.stringify({ blob: "x".repeat(tools.TOOL_OUTPUT_MAX_BYTES + 5000) });
  const out = tools.presentResult({ ok: true, stdout: bigOut, stderr: "", exitCode: 0 });
  assert.ok(out.text.length < bigOut.length);
  assert.match(out.text, /\[truncated:/);
  const bigErr = "E".repeat(5000);
  const err = tools.presentResult({ ok: false, stdout: "", stderr: bigErr, exitCode: 1 });
  assert.ok(err.text.length <= "anvil error: ".length + tools.ERROR_MAX_CHARS + 2, `len ${err.text.length}`);
  assert.ok(err.text.endsWith("…"));
});

// --- per-verb flag contract (dogfood-caused: packet rejects --json) -------

await test("runAnvil json:false omits --json (packet uses --format json)", async () => {
  const result = await tools.runAnvil("packet", ["T001", "--format", "json"], envWithFake(), undefined, undefined, { json: false });
  assert.equal(result.ok, true, result.stderr);
  const args = readLastArgs().split("\n").filter(Boolean);
  assert.deepEqual(args, ["packet", "T001", "--format", "json"], args.join(" "));
  assert.ok(!args.includes("--json"), "--json must not be appended for json:false");
});

await test("runAnvil default still appends --json", async () => {
  await tools.runAnvil("status", [], envWithFake());
  const args = readLastArgs().split("\n").filter(Boolean);
  assert.ok(args.includes("--json"), args.join(" "));
});

// --- task ids + tokenizer ---------------------------------------------------------------

test("isValidTaskId rejects option-like and spaced ids", () => {
  assert.equal(tools.isValidTaskId("T001"), true);
  assert.equal(tools.isValidTaskId("--help"), false);
  assert.equal(tools.isValidTaskId("T 1"), false);
  assert.equal(tools.isValidTaskId(""), false);
});

test("tokenizeQuotedArgs preserves quoted values verbatim without shell execution", () => {
  assert.deepEqual(tools.tokenizeQuotedArgs("T001 --commands 'pytest -q && echo $(id)'"), ["T001", "--commands", "pytest -q && echo $(id)"]);
  assert.deepEqual(tools.tokenizeQuotedArgs('T001 --files-changed "src/a.py docs/b.md"'), ["T001", "--files-changed", "src/a.py docs/b.md"]);
  assert.throws(() => tools.tokenizeQuotedArgs("T001 --commands 'unterminated"), /unterminated quote/);
});

// --- snapshot -----------------------------------------------------------------------------

await test("sessionSnapshot returns null without anvil state", async () => {
  assert.equal(await tools.sessionSnapshot({ PATH: process.env.PATH }, join(scratch, "nowhere")), null);
});

await test("sessionSnapshot returns bounded status JSON with state present", async () => {
  const snap = await tools.sessionSnapshot(envWithFake({ ANVIL_ROOT: scratch }), scratch);
  assert.ok(snap.includes("anvil session snapshot"));
  assert.ok(snap.includes('"project":"demo"'));
  assert.ok(snap.length <= tools.SNAPSHOT_MAX_CHARS + 100);
});

await test("sessionSnapshot caps oversized status output", async () => {
  writeFileSync(join(fixtureDir, "status.json"), JSON.stringify({ blob: "y".repeat(20000) }));
  const snap = await tools.sessionSnapshot(envWithFake({ ANVIL_ROOT: scratch }), scratch);
  assert.ok(snap.length <= tools.SNAPSHOT_MAX_CHARS + 2);
  assert.ok(snap.endsWith("…"));
  writeFileSync(join(fixtureDir, "status.json"), STATUS_FIXTURE);
});

await test("sessionSnapshot failure branch is capped too", async () => {
  const snap = await tools.sessionSnapshot(envWithFake({ ANVIL_ROOT: scratch, ANVIL_FAKE_EXIT: "5", ANVIL_BIG_STDERR: "90000" }), scratch);
  assert.match(snap, /status failed/);
  assert.ok(snap.length <= tools.SNAPSHOT_MAX_CHARS, `len ${snap.length}`);
});

// --- extension wiring -------------------------------------------------------------------------

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

// Extension tools read env at call time (process.env), so wiring tests point
// the real process.env at the fake anvil and restore it afterwards.
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
  } finally {
    restoreEnv();
  }
});

await test("tools thread the session workspace (ctx.cwd), not process cwd", async () => {
  useFakeEnv();
  try {
    const pi = makePi();
    await extensionMod.default(pi);
    const otherWorkspace = mkdtempSync(join(tmpdir(), "anvil-pi-ws."));
    const result = await pi.registry.tools.anvil_status.execute("t1", {}, undefined, undefined, makeCtx({}, { cwd: otherWorkspace }));
    assert.equal(result.details.isError, false);
    const recordedCwd = readFileSync(join(recordDir, "last-cwd"), "utf8").trim();
    assert.equal(recordedCwd, otherWorkspace);
  } finally {
    restoreEnv();
  }
});

await test("task-id validation blocks option-like ids before spawn", async () => {
  useFakeEnv();
  try {
    const pi = makePi();
    await extensionMod.default(pi);
    const result = await pi.registry.tools.anvil_claim.execute("t1", { task_id: "--help" }, undefined, undefined, makeCtx({}));
    assert.equal(result.details.isError, true);
    assert.match(result.content[0].text, /invalid task id/);
    if (existsSync(join(recordDir, "last-args"))) {
      assert.ok(!readFileSync(join(recordDir, "last-args"), "utf8").includes("--help"));
    }
  } finally {
    restoreEnv();
  }
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
    const allowed = await pi.registry.tools.anvil_run.execute("t1", { verb: "gate-check" }, undefined, undefined, ctx);
    assert.equal(allowed.details.isError, false);
    const planning = await pi.registry.tools.anvil_run.execute("t1", { verb: "plan" }, undefined, undefined, ctx);
    assert.equal(planning.details.isError, false); // gate is on
    delete process.env.ANVIL_PI_PLANNING; // gated case: planning off
    const gated = await pi.registry.tools.anvil_run.execute("t1", { verb: "plan" }, undefined, undefined, makeCtx({}));
    assert.match(gated.content[0].text, /ANVIL_PI_PLANNING/);
  } finally {
    restoreEnv();
  }
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
  } finally {
    restoreEnv();
  }
});

await test("commands are silent in print mode (ctx.hasUI false)", async () => {
  useFakeEnv();
  try {
    const pi = makePi();
    await extensionMod.default(pi);
    const ctx = makeCtx({}, { hasUI: false });
    await pi.registry.commands["anvil:status"].handler("", ctx);
    await pi.registry.commands["anvil:claim"].handler("T001", ctx);
    assert.deepEqual(ctx.ui.notifications, []);
  } finally {
    restoreEnv();
  }
});

await test("before_agent_start is silent AND spawn-free in print/JSON mode", async () => {
  useFakeEnv();
  try {
    const pi = makePi();
    await extensionMod.default(pi);
    rmSync(join(recordDir, "last-args"), { force: true });
    const ctx = makeCtx({}, { hasUI: false });
    const result = await pi.registry.handlers.before_agent_start({}, ctx);
    assert.equal(result, undefined, "print/JSON mode must not inject");
    assert.deepEqual(ctx.ui.notifications, []);
    assert.equal(existsSync(join(recordDir, "last-args")), false, "silent mode must not even run status");
  } finally {
    restoreEnv();
  }
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
    // quoted command value survives verbatim
    await pi.registry.commands["anvil:submit"].handler("T004 --commands 'pytest -q'", ctx);
    assert.deepEqual(
      readLastArgs().split("\n").filter(Boolean),
      ["submit", "T004", "--commands", "pytest -q", "--json"],
    );
    // unknown args rejected, not dropped
    ctx.ui.notifications.length = 0;
    await pi.registry.commands["anvil:submit"].handler("T005 --bogus x", ctx);
    assert.match(ctx.ui.notifications[0].msg, /unknown argument "--bogus"/);
    // unterminated quote surfaces usage error
    ctx.ui.notifications.length = 0;
    await pi.registry.commands["anvil:submit"].handler("T006 --commands 'unterminated", ctx);
    assert.match(ctx.ui.notifications[0].msg, /unterminated quote/);
  } finally {
    restoreEnv();
  }
});

await test("before_agent_start injects bounded snapshot once per session, resets on session_start", async () => {
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
    // failed status attempt does NOT consume the opportunity
    const failPi = makePi();
    await extensionMod.default(failPi);
    process.env.ANVIL_FAKE_EXIT = "4";
    const failed = await failPi.registry.handlers.before_agent_start({}, ctx);
    assert.ok(failed === undefined || failed.message === undefined || !("message" in failed), "failed snapshot must not inject");
    const retry = await failPi.registry.handlers.before_agent_start({}, ctx);
    assert.ok(retry === undefined || !("message" in retry));
    delete process.env.ANVIL_FAKE_EXIT;
    const afterFailure = await failPi.registry.handlers.before_agent_start({}, ctx);
    assert.ok(afterFailure.message.content.includes("anvil session snapshot"), "opportunity not consumed by failure");
    // session_start resets the once-per-session flag
    await pi.registry.handlers.session_start({}, ctx);
    const afterReset = await pi.registry.handlers.before_agent_start({}, ctx);
    assert.ok(afterReset.message.content.includes("anvil session snapshot"), "new session gets its own snapshot");
  } finally {
    restoreEnv();
  }
});

await test("before_agent_start injects nothing without anvil state", async () => {
  const pi = makePi();
  await extensionMod.default(pi);
  const ctx = makeCtx({ PATH: process.env.PATH }, { cwd: join(scratch, "nowhere") });
  const result = await pi.registry.handlers.before_agent_start({}, ctx);
  assert.equal(result, undefined);
});

// --- real CLI contract (skipped when anvil is not installed) -----------------------------

const hasRealAnvil = (() => {
  try {
    const { spawnSync } = require("node:child_process");
    return spawnSync("anvil", ["--version"], { encoding: "utf8", timeout: 15_000 }).status === 0;
  } catch {
    return false;
  }
})();

const requireContract = process.env.ANVIL_REQUIRE_CONTRACT === "1";

if (hasRealAnvil) {
  await test("contract: every allowlisted/denied verb exists in the real CLI registry", async () => {
    const { spawnSync, execFileSync } = require("node:child_process");
    const help = spawnSync("anvil", ["--help"], { encoding: "utf8", timeout: 15_000 }).stdout;
    assert.ok(help.length > 0);
    const helpTokens = new Set(help.split(/\s+/));
    const anvilBin = "anvil";
    const allVerbs = [...tools.EXECUTION_VERBS, ...tools.PLANNING_EXTRA_VERBS, ...tools.ALWAYS_DENY_VERBS, ...tools.UNCLASSIFIED_VERBS];
    const missing = allVerbs.filter((verb) => !helpTokens.has(verb));
    assert.deepEqual(missing, [], `verbs not registered in real CLI: ${missing.join(", ")}`);

  // per-verb FLAG contract: the extension sends exact flags — each must exist
  // on the real CLI (M4 dogfood caught packet rejecting --json).
  const flagContract = {
    packet: ["--format"],
    submit: ["--commands", "--files-changed"],
    claim: ["--actor", "--lease", "--force"],
    apply: ["--approve", "--reject", "--reason"],
  };
  const flagProblems = [];
  for (const [verb, flags] of Object.entries(flagContract)) {
    const verbHelp = execFileSync(anvilBin, [verb, "--help"], { encoding: "utf8", timeout: 15_000 });
    for (const flag of flags) {
      if (!verbHelp.includes(flag)) flagProblems.push(`anvil ${verb} does not offer ${flag}`);
    }
  }
  assert.deepEqual(flagProblems, [], flagProblems.join("; "));
  });
} else if (requireContract) {
  assert.fail("ANVIL_REQUIRE_CONTRACT=1 but the real anvil CLI is not on PATH — CI must expose it");
} else {
  console.log("  skip contract: real anvil CLI not on PATH");
}

console.log(`\n${passed} anvil-pi tests passed`);
