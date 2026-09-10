#!/usr/bin/env node
// mock-llm.mjs — deterministic loopback inference provider for sandbox dogfood.
//
// Serves a tiny OpenAI-compatible /v1/chat/completions endpoint on 127.0.0.1
// (loopback only — reachable inside a --network none container, unreachable
// from outside). It STEERS the agent through a fixed tool-call sequence read
// from MOCK_SEQUENCE_FILE: for request N it emits tool_call[N]; once the
// sequence is exhausted it emits a final assistant message. The optional
// "write" tool call gets the preceding tool RESULT embedded, so the report the
// agent writes carries real loop evidence, not canned text.
//
// This is the CI-safe stand-in for a live model: network:none holds, the run is
// deterministic, and the anvil state assertions live OUTSIDE the agent.
//
// Env: MOCK_SEQUENCE_FILE (required), MOCK_PORT (default 8765).

import { createServer } from "node:http";
import { readFile } from "node:fs/promises";

const port = Number(process.env.MOCK_PORT ?? 8765);
if (!Number.isInteger(port) || port <= 0 || port > 65535) {
  console.error("mock-llm: invalid MOCK_PORT");
  process.exit(2);
}
const seqFile = process.env.MOCK_SEQUENCE_FILE;
if (!seqFile) {
  console.error("mock-llm: MOCK_SEQUENCE_FILE is required");
  process.exit(2);
}
let sequence;
try {
  const parsed = JSON.parse(await readFile(seqFile, "utf8"));
  sequence = Array.isArray(parsed) ? parsed : parsed.steps;
} catch (error) {
  console.error(`mock-llm: cannot read sequence: ${error.message}`);
  process.exit(2);
}
if (!Array.isArray(sequence) || sequence.length === 0) {
  console.error("mock-llm: sequence must be a non-empty array");
  process.exit(2);
}
for (const step of sequence) {
  if (typeof step !== "object" || step === null || typeof step.name !== "string") {
    console.error("mock-llm: sequence entries must be {name, args?} objects");
    process.exit(2);
  }
}

function toolContentText(m) {
  if (!m || m.role !== "tool") return null;
  if (typeof m.content === "string") return m.content;
  if (Array.isArray(m.content)) {
    return m.content.map((c) => (c && c.type === "text" ? c.text : "")).join("\n");
  }
  return null;
}

let lastEmittedCallId = null; // correlation anchor (advisory F6)
let nextCallId = null; // the anvil_next call id — $NEXT_TASK_ID anchors here
let healthy = true;

function resultForCall(messages, callId) {
  // The steering sequence reads only the tool result for the exact call the
  // mock itself emitted (matched by tool_call_id) — never any result whose
  // text happens to look relevant (advisory F6).
  if (callId === null) return null;
  return messages.find((m) => m && m.role === "tool" && m.tool_call_id === callId) ?? null;
}

function substituteArgs(args, messages) {
  // "$NEXT_TASK_ID" resolves ONLY from the anvil_next result the mock itself
  // elicited; it must be ok:true with a well-formed task id. No fallback: a
  // missing/failed next yields a steering error (visible in the final text
  // and in CI's per-step assertions), never a silently wrong task.
  const out = {};
  for (const [key, value] of Object.entries(args)) {
    if (typeof value === "string" && value.includes("$NEXT_TASK_ID")) {
      const result = resultForCall(messages, nextCallId);
      let taskId = null;
      if (result) {
        const text = toolContentText(result) ?? "";
        try {
          const parsed = JSON.parse(text);
          const id = parsed?.data?.task?.id;
          if (parsed?.ok === true && typeof id === "string" && /^[A-Za-z0-9_-]{1,64}$/.test(id)) {
            taskId = id;
          }
        } catch {
          // fall through to the steering error below
        }
      }
      if (taskId === null) {
        healthy = false;
        console.error(`mock-llm: STEERING ERROR: no valid anvil_next result for call ${nextCallId}`);
        out[key] = "<steering-error: no valid anvil_next result>";
      } else {
        out[key] = value.replaceAll("$NEXT_TASK_ID", taskId);
      }
    } else {
      out[key] = value;
    }
  }
  return out;
}

const FINAL_TEXT =
  process.env.MOCK_FINAL_TEXT ?? "Sandbox dogfood loop complete: every steered tool call executed.";

function toolResultsOf(messages) {
  // OpenAI tool results arrive as role:"tool" messages with tool_call_id.
  return messages.filter((m) => m && m.role === "tool");
}

function lastToolResultText(messages) {
  const tools = toolResultsOf(messages);
  const last = tools[tools.length - 1];
  if (!last) return "";
  const content = last.content;
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return content.map((c) => (c && c.type === "text" ? c.text : "")).join("\n");
  }
  return "";
}

function body(requestId, message) {
  return JSON.stringify({
    id: `chatcmpl-mock-${requestId}`,
    object: "chat.completion",
    created: Math.floor(Date.now() / 1000),
    model: "steerer",
    choices: [
      {
        index: 0,
        finish_reason: message.tool_calls ? "tool_calls" : "stop",
        message,
      },
    ],
    usage: { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 },
  });
}

function sseChunks(requestId, message) {
  const frames = [];
  // One content chunk + one role frame is enough for a non-streaming-aware
  // client; pi's openai-completions parser assembles deltas.
  const parts = message.tool_calls
    ? [{ tool_calls: message.tool_calls }]
    : [{ content: message.content }];
  parts.unshift({ role: "assistant" });
  for (const part of parts) {
    frames.push(
      `data: ${JSON.stringify({
        id: `chatcmpl-mock-${requestId}`,
        object: "chat.completion.chunk",
        created: Math.floor(Date.now() / 1000),
        model: "steerer",
        choices: [{ index: 0, delta: part, finish_reason: null }],
      })}\n\n`
    );
  }
  frames.push(
    `data: ${JSON.stringify({
      id: `chatcmpl-mock-${requestId}`,
      object: "chat.completion.chunk",
      created: Math.floor(Date.now() / 1000),
      model: "steerer",
      choices: [{ index: 0, delta: {}, finish_reason: message.tool_calls ? "tool_calls" : "stop" }],
    })}\n\n`
  );
  frames.push("data: [DONE]\n\n");
  return frames.join("");
}

const MAX_BODY_BYTES = 1024 * 1024; // request cap (advisory F5)

const server = createServer((req, res) => {
  void handle(req, res).catch((error) => {
    // the WHOLE handler is guarded: a malformed request can never crash the
    // steering server mid-loop
    console.error(`mock-llm: handler error: ${error?.message ?? error}`);
    if (!res.headersSent) {
      res.writeHead(500, { "content-type": "application/json" });
    }
    res.end(JSON.stringify({ error: { message: "internal" } }));
  });
});

async function handle(req, res) {
  if (req.method !== "POST" || !req.url.startsWith("/v1/chat/completions")) {
    res.writeHead(404, { "content-type": "application/json" });
    res.end(JSON.stringify({ error: { message: "not found" } }));
    return;
  }
  const chunks = [];
  let size = 0;
  for await (const chunk of req) {
    size += chunk.length;
    if (size > MAX_BODY_BYTES) {
      res.writeHead(413, { "content-type": "application/json" });
      res.end(JSON.stringify({ error: { message: "body too large" } }));
      req.destroy();
      return;
    }
    chunks.push(chunk);
  }
  const bodyText = Buffer.concat(chunks).toString("utf8"); // split-UTF-8 safe
  if (process.env.MOCK_DUMP_DIR) {
    try {
      const { writeFile: wf, mkdir: mkd } = await import("node:fs/promises");
      await mkd(process.env.MOCK_DUMP_DIR, { recursive: true });
      await wf(`${process.env.MOCK_DUMP_DIR}/req-${Date.now()}-${Math.random().toString(36).slice(2, 6)}.json`, bodyText);
    } catch { /* debug aid only */ }
  }
  let request;
  try {
    request = JSON.parse(bodyText);
  } catch {
    res.writeHead(400, { "content-type": "application/json" });
    res.end(JSON.stringify({ error: { message: "bad json" } }));
    return;
  }
  if (request === null || typeof request !== "object" || Array.isArray(request)
    || !Array.isArray(request.messages)
    || request.messages.some((m) => m === null || typeof m !== "object")) {
    res.writeHead(400, { "content-type": "application/json" });
    res.end(JSON.stringify({ error: { message: "malformed request: messages must be an array of objects" } }));
    return;
  }
  const messages = request.messages;
  const done = toolResultsOf(messages).length;
  let message;
  if (done < sequence.length) {
    const step = sequence[done];
    const args = substituteArgs({ ...(step.args ?? {}) }, messages);
    if (step.name === "write" && args.embed_last_tool_result) {
      delete args.embed_last_tool_result;
      args.content = `${args.content ?? ""}\n\n<last-tool-result>\n${lastToolResultText(messages).slice(0, 4000)}\n</last-tool-result>`;
    }
    lastEmittedCallId = `call-mock-${done}`;
    if (step.name === "anvil_next") nextCallId = lastEmittedCallId;
    message = {
      role: "assistant",
      content: null,
      tool_calls: [
        {
          id: lastEmittedCallId,
          type: "function",
          function: { name: step.name, arguments: JSON.stringify(args) },
        },
      ],
    };
  } else {
    message = { role: "assistant", content: FINAL_TEXT };
  }
  console.log(`mock-llm: request ${done} -> ${message.tool_calls ? message.tool_calls[0].function.name : "final"}`);
  if (request.stream === true) {
    res.writeHead(200, {
      "content-type": "text/event-stream",
      "cache-control": "no-cache",
      connection: "keep-alive",
    });
    res.end(sseChunks(done, message));
  } else {
    res.writeHead(200, { "content-type": "application/json" });
    res.end(body(done, message));
  }
}

server.listen(port, "127.0.0.1", () => {
  console.log(`mock-llm: steering on 127.0.0.1:${port} (${sequence.length} steered step(s))`);
});
// Teardown is the entrypoint's job (trap kill on EXIT/INT/TERM).