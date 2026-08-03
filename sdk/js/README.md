# markvector

The JavaScript and TypeScript client for **MarkVector**. Store text, PDFs,
Word, Markdown, and text files; search by meaning and exact wording; get
grounded answers with citations; inspect document structure; and run a
client-side, tool-using agent with your own OpenAI-compatible LLM.

```bash
npm install markvector
```

## Quick start

```ts
import { Markvector } from "markvector";

// Or set MARKVECTOR_API_KEY and MARKVECTOR_URL.
const mv = new Markvector({ apiKey: "kb_live_…" });
const docs = mv.collection("client-research");

await docs.add("Q2 paid conversions fell 18 percent quarter over quarter.", {
  locator: "notes/q2",
  wait: true,
});

const results = await docs.search("why did paid results fall");
for (const hit of results.matches) {
  console.log(hit.score, hit.title, hit.matchedOn, hit.cleanExcerpt);
}

const answer = await docs.answer("summarise Q2 paid performance");
if (answer.grounded) {
  console.log(answer.text);
  for (const citation of answer.citations) {
    console.log(`[${citation.marker}] ${citation.title} — ${citation.heading}`);
  }
}
```

The key identifies a workspace; no method takes a workspace ID. By default the
client reads `MARKVECTOR_API_KEY` and `MARKVECTOR_URL` (falling back to
`http://localhost:8000`).

## Files and document structure

```ts
await docs.addFile("reports/q2.pdf", { wait: true });

const files = await docs.files();
const doc = files[0];

for (const chunk of await docs.chunks(doc.id)) {
  console.log(chunk.ordinal, chunk.heading, chunk.text);
}

const tree = await docs.structure(doc);
console.log(tree.title, tree.nodes, tree.sections);

await docs.downloadOriginal(doc.id, { path: "q2-copy.pdf" }); // Node
const bytes = await docs.downloadOriginal(doc.id);             // browser or Node
```

`addFile` accepts a Node path or `{ data: Uint8Array | Blob, name: string }`.
Search within selected documents by passing IDs or `Document` objects:

```ts
const hits = await docs.search("refund policy", { files: files.slice(0, 3) });
const documents = await docs.getMany(files);
const structures = await docs.structures(files);
```

`answer()` defaults to `mode: "vectorless"`, which reasons over each
document's heading tree. Use `{ mode: "hybrid" }` for embeddings plus keyword
retrieval.

## Agent (bring your own LLM)

The agent is read-only. It can search, list files, inspect a PageIndex heading
tree, and read documents; the loop and model calls run on your machine.

```ts
import {
  AgentAnswer,
  Markvector,
  Thinking,
  ToolCall,
  ToolResult,
} from "markvector";

const mv = new Markvector({ apiKey: "kb_live_…" });
const agent = mv.collection("default").agent({
  apiKey: process.env.OPENAI_API_KEY,
  model: "gpt-4o-mini",
  // baseUrl: "https://your-openai-compatible-endpoint/v1",
  // client: existingOpenAIClient,
});

for await (const event of agent.stream("How does voice input work?")) {
  if (event instanceof Thinking) process.stdout.write(event.text);
  else if (event instanceof ToolCall) console.log("→", event.name, event.arguments);
  else if (event instanceof ToolResult) console.log("←", event.summary);
  else if (event instanceof AgentAnswer) console.log("\nANSWER\n" + event.text);
}

const result = await agent.answer("How does voice input work?");
console.log(result.answer, result.toolCalls, result.steps);
```

The `openai` package is an optional peer dependency. Install it when building
an agent internally (`npm install openai`), or pass any compatible `client`.

## Collections, keys, and traces

```ts
const collection = await mv.createCollection("Client research");
await mv.renameCollection(collection.collectionId, "ACME research");

const key = await mv.createKey("ci-pipeline", { scopes: "read" });
console.log(key.key); // returned once; store it now
await mv.revokeKey(key.keyId);

const results = await docs.search("refund policy");
console.log(await mv.trace(results.traceId));
```

## Errors and retries

All client errors inherit from `MarkvectorError`. More specific classes are
`AuthError`, `NotFound`, `InvalidRequest`, `Unavailable`, and
`IndexingTimeout`.

```ts
import { MarkvectorError } from "markvector";

try {
  await docs.search("…");
} catch (error) {
  if (error instanceof MarkvectorError) console.error(error.message);
}
```

GET requests retry transient 429/5xx responses with backoff. Writes are never
retried because a timed-out write may already have landed.

