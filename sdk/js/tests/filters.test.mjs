import assert from "node:assert/strict";
import test from "node:test";

import { Agent, InvalidRequest, Markvector, RateLimited } from "../dist/index.js";

// What the SDK gained when the server got ahead of it: metadata filtering, the
// location of a citation, deleting a document, the `manage` scope, and
// rate-limit cooperation. These pin the shapes callers now write against.

const BASE = "http://localhost:8000";

function clientWith(handler) {
  return new Markvector({ apiKey: "test", baseUrl: BASE, fetch: handler });
}

const json = (body, init = {}) =>
  new Response(JSON.stringify(body), {
    status: init.status ?? 200,
    headers: { "content-type": "application/json", ...(init.headers ?? {}) },
  });

// ------------------------------ metadata filters ------------------------------

test("a where object becomes repeated key:value pairs", async () => {
  const seen = [];
  const docs = clientWith(async (url) => {
    seen.push(new URL(url).searchParams.getAll("meta"));
    return json({ results: [], trace_id: "t" });
  }).collection("c");

  await docs.search("q", { where: { client: "acme" } });
  await docs.search("q", { where: { kind: ["policy", "notice"] } });
  assert.deepEqual(seen, [["client:acme"], ["kind:policy", "kind:notice"]]);
});

test("booleans are spelled the way JSON spells them", async () => {
  let seen = [];
  const docs = clientWith(async (url) => {
    seen = new URL(url).searchParams.getAll("meta");
    return json({ results: [], trace_id: "t" });
  }).collection("c");

  // String(true) is "true" in JS, but pinning it stops a future refactor from
  // reintroducing a value that matches nothing and looks like an empty result.
  await docs.search("q", { where: { active: true, archived: false } });
  assert.deepEqual(seen, ["active:true", "archived:false"]);
});

test("a key containing a colon is refused rather than misread", async () => {
  const docs = clientWith(async () => json({})).collection("c");
  await assert.rejects(
    () => docs.search("q", { where: { "a:b": "c" } }),
    /colon/,
  );
});

test("an empty value array is refused", async () => {
  const docs = clientWith(async () => json({})).collection("c");
  await assert.rejects(() => docs.search("q", { where: { client: [] } }), /no values/);
});

test("answer sends the filter too", async () => {
  let seen = [];
  const docs = clientWith(async (url) => {
    seen = new URL(url).searchParams.getAll("meta");
    return json({ answer: "", grounded: false, citations: [] });
  }).collection("c");

  await docs.answer("q", { where: { client: "acme" } });
  assert.deepEqual(seen, ["client:acme"]);
});

// -------------------- an agent cannot widen its own scope --------------------

test("the agent's confinement is the caller's, not the model's", async () => {
  const seen = [];
  const docs = clientWith(async (url) => {
    seen.push(new URL(url).searchParams.getAll("meta"));
    return json({ results: [], trace_id: "t" });
  }).collection("c");

  // The agent drives a STREAMING chat completion, so the stub yields chunks
  // the way a real OpenAI-compatible client does: one turn that calls search,
  // then one that answers.
  class FakeLLM {
    turn = 0;
    chat = {
      completions: {
        create: async () => this.chunks(),
      },
    };

    async *chunks() {
      if (this.turn++ === 0) {
        yield {
          choices: [
            {
              delta: {
                tool_calls: [
                  {
                    index: 0,
                    id: "call-1",
                    function: { name: "search", arguments: '{"query":"anything"}' },
                  },
                ],
              },
            },
          ],
        };
      } else {
        yield { choices: [{ delta: { content: "done" } }] };
      }
    }
  }

  const agent = new Agent(docs, { client: new FakeLLM(), model: "fake", where: { client: "acme" } });
  await agent.answer("anything");
  assert.ok(seen.length > 0, "the agent should have searched");
  for (const meta of seen) assert.deepEqual(meta, ["client:acme"]);
});

// --------------------------- deleting a document ---------------------------

test("an unconfirmed delete destroys nothing and reports what would go", async () => {
  const docs = clientWith(async (url) => {
    assert.ok(!new URL(url).searchParams.has("confirm"));
    return json(
      {
        detail: {
          message: "...",
          would_delete: { item_id: "i", title: "COMPUTER NETWORKS", versions: 2, passages: 3915 },
        },
      },
      { status: 409 },
    );
  }).collection("c");

  const plan = await docs.delete("i");
  assert.equal(plan.deleted, false);
  assert.equal(plan.versions, 2);
  assert.equal(plan.passages, 3915);
});

test("a confirmed delete reports what actually went", async () => {
  // The confirmed response counts by TABLE, not by the preview's words.
  // Reading only versions/passages made a real deletion announce "0 versions,
  // 0 passages" — seen against the live server.
  const docs = clientWith(async (url) => {
    assert.equal(new URL(url).searchParams.get("confirm"), "true");
    return json({ deleted: true, item_id: "i", title: "Gone", kb_items: 2, kb_chunks: 3915 });
  }).collection("c");

  const done = await docs.delete("i", { confirm: true });
  assert.equal(done.deleted, true);
  assert.equal(done.title, "Gone");
  assert.equal(done.versions, 2);
  assert.equal(done.passages, 3915);
});

// -------------------------------- key scopes --------------------------------

test("manage is a scope a caller can ask for", async () => {
  let sent;
  const mv = clientWith(async (_url, init) => {
    sent = JSON.parse(init.body);
    return json({ key: "kb_live_x", key_id: "k", name: "ops" }, { status: 201 });
  });
  await mv.createKey("ops", { scopes: ["manage"] });
  assert.equal(sent.scopes, "manage");
});

test("an unknown scope is refused before a key is minted", async () => {
  const mv = clientWith(async () => json({}));
  await assert.rejects(() => mv.createKey("x", { scopes: "admin" }), /unknown scope/);
});

// --------------------------- rate limit cooperation ---------------------------

test("a 429 becomes its own error carrying the server's timing", async () => {
  const mv = new Markvector({
    apiKey: "test",
    baseUrl: BASE,
    maxRetries: 0,
    fetch: async () =>
      json(
        { detail: { message: "Rate limit exceeded for read requests." } },
        { status: 429, headers: { "retry-after": "7", "ratelimit-limit": "120" } },
      ),
  });

  await assert.rejects(
    () => mv.whoami(),
    (error) => {
      assert.ok(error instanceof RateLimited);
      assert.equal(error.retryAfter, 7);
      assert.equal(error.limit, 120);
      // The message is the server's own sentence, not the wrapper around it.
      assert.match(error.message, /Rate limit exceeded/);
      return true;
    },
  );
});

// ------------------------------ nowhere to point ------------------------------

test("a client with no base URL refuses to be built", () => {
  // There is deliberately no default host: this used to be our own demo
  // deployment, so a caller who forgot baseUrl shipped documents to a server
  // they had never heard of, and nothing said so.
  const previous = process.env.MARKVECTOR_URL;
  delete process.env.MARKVECTOR_URL;
  try {
    assert.throws(() => new Markvector({ apiKey: "test" }), InvalidRequest);
  } finally {
    if (previous !== undefined) process.env.MARKVECTOR_URL = previous;
  }
});
