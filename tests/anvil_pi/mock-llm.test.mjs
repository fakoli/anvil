#!/usr/bin/env node
// mock-llm.mjs unit tests — M4 astra-review regressions:
// F1 (tree digest ambiguity), F5 (malformed/oversized requests), F6
// (correlation by the mock's own tool_call_id; no silent T001 fallback).
import * as assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve as resolvePath } from "node:path";

const REPO = resolvePath(new URL("../..", import.meta.url).pathname);
const POLICY = join(REPO, "scripts", "pi-sandbox-policy.mjs");
const MOCK = join(REPO, "packaging", "pi", "sandbox", "mock-llm.mjs");
const SEQUENCE = join(REPO, "packaging", "pi", "sandbox", "dogfood-sequence.json");
const scratch = mkdtempSync(join(tmpdir(), "mock-llm-test."));
process.on("exit", () => rmSync(scratch, { recursive: true, force: true }));

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

async function startMock(port, extraEnv = {}) {
  const child = execFileSync("true"); // placeholder to keep imports simple
  void child;
  const { spawn } = await import("node:child_process");
  const proc = spawn("node", [MOCK], {
    env: { ...process.env, MOCK_SEQUENCE_FILE: SEQUENCE, MOCK_PORT: String(port), ...extraEnv },
    stdio: ["ignore", "pipe", "pipe"],
  });
  let stderr = "";
  proc.stderr.on("data", (d) => (stderr += d));
  for (let i = 0; i < 50; i++) {
    try {
      await fetch(`http://127.0.0.1:${port}/v1/chat/completions`, { method: "POST", body: "{}" });
      break;
    } catch {
      await new Promise((r) => setTimeout(r, 100));
    }
  }
  return { proc, stderr: () => stderr };
}

const call = async (port, messages) => {
  const r = await fetch(`http://127.0.0.1:${port}/v1/chat/completions`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ messages }),
  });
  return { status: r.status, body: await r.json() };
};

await test("F1: tree digest is unambiguous under newline-containing filenames", async () => {
  // the exact collision pair from the review: two distinct trees that the v1
  // encoding (<rel>\n<hex>\n) digested identically, because a FILENAME could
  // forge a record boundary. v2 length-prefixes every relpath.
  const a = join(scratch, "a");
  const b = join(scratch, "b");
  execFileSync("node", ["-e", `
    const fs = require("fs");
    fs.mkdirSync(process.argv[1] + "/d", { recursive: true });
    fs.writeFileSync(process.argv[1] + "/d/x", "X");
    fs.writeFileSync(process.argv[1] + "/d/y", "Y");
  `, a]);
  execFileSync("node", ["-e", `
    const fs = require("fs");
    fs.mkdirSync(process.argv[1] + "/d", { recursive: true });
    fs.writeFileSync(process.argv[1] + "/d/y", "Y");
  `, b]);
  // tree B: ONE file whose NAME embeds a forged v1 record boundary
  const { createHash } = await import("node:crypto");
  const xSha = createHash("sha256").update("X").digest("hex");
  const forgedName = `a\n${xSha}\nb`;
  execFileSync("node", ["-e", `
    const fs = require("fs");
    fs.writeFileSync(process.argv[1] + "/d/" + process.argv[2], "Y");
  `, b, forgedName]);
  const ha = JSON.parse(execFileSync("node", [POLICY, "tree-hash", a]).toString()).digest;
  const hb = JSON.parse(execFileSync("node", [POLICY, "tree-hash", b]).toString()).digest;
  assert.notEqual(ha, hb, "v2 length-prefixed encoding must separate these trees");
});

await test("F5: null request body -> 400", async () => {
  const m = await startMock(8801);
  try {
    const r = await fetch("http://127.0.0.1:8801/v1/chat/completions", { method: "POST", body: "null" });
    assert.equal(r.status, 400);
  } finally {
    m.proc.kill();
  }
});

await test("F5: oversized body -> 413, server survives", async () => {
  const m = await startMock(8802);
  try {
    const r = await fetch("http://127.0.0.1:8802/v1/chat/completions", { method: "POST", body: "x".repeat(2 * 1024 * 1024) });
    assert.equal(r.status, 413);
    const r2 = await fetch("http://127.0.0.1:8802/v1/chat/completions", { method: "POST", body: JSON.stringify({ messages: [{ role: "user", content: "hi" }] }) });
    assert.equal(r2.status, 200);
  } finally {
    m.proc.kill();
  }
});

await test("F6: claim args resolve $NEXT_TASK_ID from the mock's OWN anvil_next result", async () => {
  const m = await startMock(8803);
  try {
    const callStep = async (messages) => {
      const r = await fetch(`http://127.0.0.1:8803/v1/chat/completions`, {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ messages }),
      });
      return (await r.json()).choices[0].message;
    };
    let hist = [{ role: "user", content: "go" }];
    let msg = await callStep(hist);
    assert.equal(msg.tool_calls[0].function.name, "anvil_status");
    hist.push(msg, { role: "tool", tool_call_id: msg.tool_calls[0].id, content: '{"ok":true,"command":"status","data":{}}' });
    msg = await callStep(hist);
    assert.equal(msg.tool_calls[0].function.name, "anvil_next");
    hist.push(msg, { role: "tool", tool_call_id: msg.tool_calls[0].id, content: '{"ok":true,"command":"next","data":{"task":{"id":"T042"}}}' });
    msg = await callStep(hist);
    assert.equal(msg.tool_calls[0].function.name, "anvil_claim");
    assert.equal(msg.tool_calls[0].function.arguments, '{"task_id":"T042"}');
  } finally {
    m.proc.kill();
  }
});

await test("F6: a next result under the WRONG call id yields a steering error, not a fallback", async () => {
  const m = await startMock(8804);
  try {
    // fast-forward the mock's own emissions to the claim step
    let messages = [{ role: "user", content: "go" }];
    let emitted = null;
    for (let i = 0; i < 3; i++) {
      const r = await fetch(`http://127.0.0.1:8804/v1/chat/completions`, {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ messages }),
      });
      const body = await r.json();
      emitted = body.choices[0].message;
      messages.push(emitted);
      const callId = emitted.tool_calls?.[0]?.id;
      // feed a WRONG-id result for the next step (the anvil_next result comes
      // back under a different id) — the mock must NOT use it
      const wrongId = callId === "call-mock-1" ? "decoy-id" : callId;
      messages.push({ role: "tool", tool_call_id: wrongId, content: '{"ok":true,"command":"next","data":{"task":{"id":"T999"}}}' });
    }
    const claimCall = messages.filter((m) => m.tool_calls).map((m) => m.tool_calls[0].function).find((f) => f.name === "anvil_claim");
    assert.ok(claimCall, "claim step reached");
    assert.equal(claimCall.arguments, '{"task_id":"<steering-error: no valid anvil_next result>"}');
  } finally {
    m.proc.kill();
  }
});

console.log(`\\n${passed} mock-llm tests passed`);
