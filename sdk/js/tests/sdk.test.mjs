import assert from "node:assert/strict";
import test from "node:test";

import {
  AgentAnswer,
  AuthError,
  Markvector,
  Thinking,
  ToolCall,
  ToolResult,
} from "../dist/index.js";

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
  assert.equal(init.headers["X-Collection"], "sdk-demo");
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
  const docs = new Markvector({ apiKey: "test", fetch: mockFetch }).collection("sdk-demo");
  const result = await docs.search("why did paid results fall");
  assert.equal(result.traceId, "trace-1");
  assert.equal(result.matches[0].matchedOn, "meaning+wording");
  assert.equal(result.matches[0].cleanExcerpt, "paid conversions fell");
});

class FakeLLM {
  turn = 0;
  chat = { completions: { create: async () => this.chunks() } };

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
  const docs = new Markvector({ apiKey: "test", fetch: mockFetch }).collection("sdk-demo");
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
  const docs = new Markvector({ apiKey: "test", fetch: mockFetch }).collection("sdk-demo");
  const result = await docs.agent({ client: new FakeLLM(), model: "fake" }).answer("How?");
  assert.equal(result.toolCalls, 1);
  assert.match(result.answer, /item-1/);
  assert.ok(!result.steps.some((event) => event instanceof AgentAnswer));
});
