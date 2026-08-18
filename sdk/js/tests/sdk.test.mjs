import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";

import {
  AgentAnswer,
  AuthError,
  Markvector,
  Thinking,
  ToolCall,
  ToolResult,
  VERSION,
} from "../dist/index.js";

// Every client now needs an explicit base URL: the SDK no longer carries a
// default host, so an omitted one cannot quietly ship documents elsewhere.
const BASE = "http://localhost:8000";

const document = {
  id: "item-1",
  title: "Q2 note",
  body: "Paid conversions fell 18 percent.",
  source: { source: "sdk", locator: "notes/q2", url: null },
  version: 1,
  status: "active",
  hash: "abc",
  classes: [],
  metadata: {},
};

function mockFetch(input, init) {
  const url = new URL(input);
  assert.equal(init.headers["X-Markvector-Client"], `javascript/${VERSION}`);
  if (!url.pathname.startsWith("/api/traces/")) {
    assert.equal(init.headers["X-Collection"], "sdk-demo");
  }
  if (url.pathname === "/api/search") {
    return Promise.resolve(Response.json({
      query: url.searchParams.get("q"),
      trace_id: "trace-1",
      took_ms: 12,
      results: [{
        item_id: "item-1",
        title: "Q2 note",
        excerpt: "paid [[conversions]] fell",
        source: document.source,
        score: 0.82,
        semantic: 0.6,
        keyword: 0.2,
        classes: [],
      }],
    }));
  }
  if (url.pathname === "/api/items") {
    return Promise.resolve(Response.json({ items: [document] }));
  }
  if (url.pathname === "/api/traces/trace-1") {
    return Promise.resolve(Response.json({
      trace_id: "trace-1",
      query: "why did paid results fall",
      via: `sdk:javascript/${VERSION}`,
      timings_ms: { total: 12 },
    }));
  }
  return Promise.resolve(Response.json({ detail: "unhandled" }, { status: 404 }));
}

test("missing API key is AuthError", () => {
  const previous = process.env.MARKVECTOR_API_KEY;
  const legacy = process.env.KB_API_KEY;
  delete process.env.MARKVECTOR_API_KEY;
  delete process.env.KB_API_KEY;
  try {
    assert.throws(() => new Markvector(), AuthError);
  } finally {
    if (previous !== undefined) process.env.MARKVECTOR_API_KEY = previous;
    if (legacy !== undefined) process.env.KB_API_KEY = legacy;
  }
});

test("search maps server JSON to typed camelCase results", async () => {
  const docs = new Markvector({ apiKey: "test", baseUrl: BASE, fetch: mockFetch }).collection("sdk-demo");
  const result = await docs.search("why did paid results fall");
  assert.equal(result.traceId, "trace-1");
  assert.equal(result.matches[0].matchedOn, "meaning+wording");
  assert.equal(result.matches[0].cleanExcerpt, "paid conversions fell");
});

test("trace lookup works for the trace id returned by an SDK search", async () => {
  const mv = new Markvector({ apiKey: "test", baseUrl: BASE, fetch: mockFetch });
  const result = await mv.collection("sdk-demo").search("why did paid results fall");
  const trace = await mv.trace(result.traceId);
  assert.equal(trace.trace_id, result.traceId);
  assert.equal(trace.via, `sdk:javascript/${VERSION}`);
});

class FakeLLM {
  turn = 0;
  requests = [];
  chat = { completions: { create: async (options) => {
    this.requests.push(options);
    return this.chunks();
  } } };

  async *chunks() {
    if (this.turn++ === 0) {
      yield { choices: [{ delta: { content: "Let me search. " } }] };
      yield { choices: [{ delta: { tool_calls: [{ index: 0, id: "call-1", function: { name: "search", arguments: '{"query":' } }] } }] };
      yield { choices: [{ delta: { tool_calls: [{ index: 0, function: { arguments: '"voice input"}' } }] } }] };
    } else {
      yield { choices: [{ delta: { content: "Voice input is documented [item-1]." } }] };
    }
  }
}

test("agent streams thoughts, reassembled tools, results, then answer", async () => {
  const docs = new Markvector({ apiKey: "test", baseUrl: BASE, fetch: mockFetch }).collection("sdk-demo");
  const events = [];
  for await (const event of docs.agent({ client: new FakeLLM(), model: "fake" }).stream("How?")) {
    events.push(event);
  }
  assert.ok(events.some((event) => event instanceof Thinking));
  const call = events.find((event) => event instanceof ToolCall);
  assert.deepEqual(call.arguments, { query: "voice input" });
  assert.ok(events.some((event) => event instanceof ToolResult));
  assert.ok(events.at(-1) instanceof AgentAnswer);
});

test("agent answer includes transcript and tool count", async () => {
  const docs = new Markvector({ apiKey: "test", baseUrl: BASE, fetch: mockFetch }).collection("sdk-demo");
  const result = await docs.agent({ client: new FakeLLM(), model: "fake" }).answer("How?");
  assert.equal(result.toolCalls, 1);
  assert.match(result.answer, /item-1/);
  assert.ok(!result.steps.some((event) => event instanceof AgentAnswer));
});

test("agent file selection is enforced on every search", async () => {
  let searchedFiles = [];
  const scopedFetch = (input, init) => {
    const url = new URL(input);
    if (url.pathname === "/api/search") searchedFiles = url.searchParams.getAll("item_ids");
    return mockFetch(input, init);
  };
  const docs = new Markvector({ apiKey: "test", baseUrl: BASE, fetch: scopedFetch }).collection("sdk-demo");
  const result = await docs.agent({ client: new FakeLLM(), model: "fake" }).answer("How?", {
    files: ["item-1"],
  });
  assert.equal(result.toolCalls, 1);
  assert.deepEqual(searchedFiles, ["item-1"]);
});

test("agent cannot open a document outside the selected file scope", async () => {
  const docs = new Markvector({ apiKey: "test", baseUrl: BASE, fetch: mockFetch }).collection("sdk-demo");
  const agent = docs.agent({ client: new FakeLLM(), model: "fake" });
  const result = await agent.runTool(
    "read_document",
    { item_id: "item-2" },
    new Set(["item-1"]),
  );
  assert.match(result.error, /outside the selected file scope/);
});

test("custom instructions are appended to the protected system prompt", async () => {
  const llm = new FakeLLM();
  const docs = new Markvector({ apiKey: "test", baseUrl: BASE, fetch: mockFetch }).collection("sdk-demo");
  await docs.agent({
    client: llm,
    model: "fake",
    instructions: "Return a terse JSON object in Spanish.",
  }).answer("How?");

  const system = llm.requests[0].messages[0].content;
  assert.match(system, /using ONLY the tools provided/);
  assert.match(system, /Additional instructions from the caller/);
  assert.match(system, /terse JSON object in Spanish/);
});

test("the published version and the version the SDK reports are the same", async () => {
  // These drifted once: 0.2.4 went to npm while the client kept telling the
  // server it was 0.2.0, so four releases' worth of client telemetry was wrong.
  // The header assertions above derive from VERSION and so cannot catch that —
  // only comparing against package.json can.
  const manifest = JSON.parse(
    await readFile(new URL("../package.json", import.meta.url), "utf8"),
  );
  assert.equal(manifest.version, VERSION);
});
