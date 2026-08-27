// Ad-hoc smoke test of the JS SDK against the local stack and the
// `tn-organization-brain` collection. Runs each stage independently so one
// failure does not abort the rest — and reports clearly when the running API
// predates the /neighbors endpoint (rebuild the api image to pick it up).
import { Markvector } from "./dist/index.js";

const KEY = process.env.MARKVECTOR_API_KEY;
const BASE = process.env.MARKVECTOR_URL ?? "http://localhost:8000";
const COLLECTION = process.env.MARKVECTOR_COLLECTION ?? "tn-organization-brain";

if (!KEY) {
  console.error("Set MARKVECTOR_API_KEY to run this smoke test.");
  process.exit(1);
}

const line = (s) => console.log(s);
const hr = () => line("─".repeat(60));

async function stage(name, fn) {
  hr();
  line(`▶ ${name}`);
  try {
    await fn();
  } catch (e) {
    line(`  ✗ ${e?.constructor?.name ?? "Error"}: ${e?.message ?? e}`);
  }
}

// Agentic answers run a multi-round LLM loop server-side, so give the client
// room well past its 30s default.
const mv = new Markvector({ apiKey: KEY, baseUrl: BASE, timeout: 180000 });
const docs = mv.collection(COLLECTION);

// 1. Who does the key belong to, and what may it do?
await stage("whoami", async () => {
  const who = await mv.whoami();
  line("  " + JSON.stringify(who));
});

// 2. What's in the collection?
let firstMatchChunkId = null;
await stage("collection info + documents", async () => {
  const info = await docs.info();
  line(`  ${info.name}  ·  ${info.items} items  ·  ${info.embeddingModel} (${info.dimensions}d)`);
  const files = await docs.list({ limit: 8 });
  line(`  documents (${files.length} shown):`);
  for (const d of files) line(`    - ${d.id}  ${d.title?.slice(0, 60) ?? ""}`);
});

// 3. Search — meaning + wording, and grab a chunk_id to hop from.
await stage("search", async () => {
  const results = await docs.search("overview of the organization", { limit: 5 });
  line(`  query: "${results.query}"  ·  ${results.matches.length} matches  ·  trace ${results.traceId}`);
  for (const m of results.matches) {
    line(`    ${m.score.toFixed(3)}  ${m.matchedOn.padEnd(15)}  chunk=${m.chunkId || "(none)"}  ${m.title?.slice(0, 40) ?? ""}`);
  }
  firstMatchChunkId = results.matches.find((m) => m.chunkId)?.chunkId ?? null;
});

// 4. THE HOP — walk the NEAR graph from the best passage.
await stage("neighbors (graph hop)", async () => {
  if (!firstMatchChunkId) {
    line("  (no chunk_id from search — cannot hop)");
    return;
  }
  line(`  hopping from chunk ${firstMatchChunkId}`);
  const hops = await docs.neighbors(firstMatchChunkId, { limit: 6 });
  if (hops.length === 0) {
    line("  0 neighbours. Either consolidation hasn't built NEAR edges yet");
    line("  (run POST /api/collections/<id>/consolidate) or this is a fresh collection.");
    return;
  }
  for (const n of hops) {
    // "*" marks an authored relation; without it the edge is only cosine "near".
    const rel = `${n.relation}${n.typed ? "*" : ""}`;
    line(`    ${n.similarity.toFixed(3)}  ${rel.padEnd(13)}  ${n.nodeType.padEnd(7)}  ${n.heading?.slice(0, 45) ?? ""}  → ${n.neighborId}`);
  }
});

const QUESTION = "How do I obtain a death certificate?";

// 5a. Hybrid — one fused ranked pass, fast and deterministic.
await stage("answer (hybrid)", async () => {
  const a = await docs.answer(QUESTION, { mode: "hybrid" });
  line(`  mode=${a.mode}  grounded=${a.grounded}  ${a.tookMs}ms`);
  if (a.degraded) line(`  ⚠ degraded: ${a.degraded}`);
  line("  " + (a.text || "(no answer)").slice(0, 500));
  for (const c of a.citations.slice(0, 4)) line(`    [${c.marker}] ${c.title} — ${c.heading}`);
});

// 5b. Agentic (default) — reasons, searches, hops; reaches the whole collection.
await stage("answer (agentic, default)", async () => {
  const a = await docs.answer(QUESTION);
  line(`  mode=${a.mode}  grounded=${a.grounded}  ${a.tookMs}ms`);
  if (a.degraded) line(`  ⚠ degraded: ${a.degraded}`);
  line("  " + (a.text || "(no answer)").slice(0, 600));
  for (const c of a.citations.slice(0, 5)) line(`    [${c.marker}] ${c.title} — ${c.heading}`);
});

hr();
line("done.");
