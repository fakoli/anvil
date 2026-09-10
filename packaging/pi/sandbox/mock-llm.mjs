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

function substituteArgs(args, messages) {
  // "$NEXT_TASK_ID" placeholders resolve from the most recent anvil_next
  // tool result, so the steering sequence works on any seeded root.
  const out = {};
  for (const [key, value] of Object.entries(args)) {
    if (typeof value === "string" && value.includes("$NEXT_TASK_ID")) {
      const nextText = [...messages]
        .reverse()
        .map(toolContentText)
        .find((t) => t && t.includes('"command"') && t.includes("next"));
      let taskId = "T001";
      if (nextText) {
        try {
          const parsed = JSON.parse(nextText);
          taskId = parsed?.data?.task?.id ?? taskId;
        } catch {
          // fall back to the deterministic default
        }
      }
      out[key] = value.replaceAll("$NEXT_TASK_ID", taskId);
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

const server = createServer(async (req, res) => {
  if (req.method !== "POST" || !req.url.startsWith("/v1/chat/completions")) {
    res.writeHead(404, { "content-type": "application/json" });
    res.end(JSON.stringify({ error: { message: "not found" } }));
    return;
  }
  let bodyText = "";
  for await (const chunk of req) bodyText += chunk;
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
  const messages = Array.isArray(request.messages) ? request.messages : [];
  const done = toolResultsOf(messages).length;
  let message;
  if (done < sequence.length) {
    const step = sequence[done];
    const args = substituteArgs({ ...(step.args ?? {}) }, messages);
    if (step.name === "write" && args.embed_last_tool_result) {
      delete args.embed_last_tool_result;
      args.content = `${args.content ?? ""}\n\n<last-tool-result>\n${lastToolResultText(messages).slice(0, 4000)}\n</last-tool-result>`;
    }
    message = {
      role: "assistant",
      content: null,
      tool_calls: [
        {
          id: `call-mock-${done}`,
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
});

server.listen(port, "127.0.0.1", () => {
  console.log(`mock-llm: steering on 127.0.0.1:${port} (${sequence.length} steered step(s))`);
});
// Teardown is the entrypoint's job (trap kill on EXIT/INT/TERM).