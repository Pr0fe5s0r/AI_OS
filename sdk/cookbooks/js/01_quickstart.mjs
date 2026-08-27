// 01 · Quickstart — create a collection, add text, search, answer.
//
//   export MARKVECTOR_API_KEY=kb_live_…
//   node js/01_quickstart.mjs
import { Markvector, MarkvectorError } from "markvector";

try {
  const mv = new Markvector(); // reads MARKVECTOR_API_KEY / MARKVECTOR_URL
  console.log("workspace:", await mv.whoami());

  // A place to work. The id is derived from the name when omitted; passing it
  // explicitly makes this script safe to re-run (collection() never calls the
  // server, and re-creating a known id is a no-op).
  const info = await mv.createCollection("Cookbook demo", { collectionId: "cookbook" });
  const docs = mv.collection(info.collectionId);

  // Write, and block until it is actually searchable (indexing is async).
  await docs.add("Paid conversions fell 18 percent in Q2, driven by a CPC increase.", {
    locator: "notes/q2",
    title: "Q2 note",
    wait: true,
  });

  // Search — meaning and exact wording, fused into one ranking.
  console.log("\n# search");
  const results = await docs.search("why did paid results drop", { limit: 5 });
  for (const hit of results.matches) {
    console.log(`  ${hit.score.toFixed(3)}  ${hit.title}  (${hit.matchedOn})`);
    console.log(`    ${hit.cleanExcerpt}`);
  }

  // Ask — a written answer built ONLY from what was retrieved.
  console.log("\n# answer");
  const answer = await docs.answer("what happened to paid conversions in Q2?");
  if (answer.grounded) {
    console.log(" ", answer.text);
    for (const c of answer.citations) console.log(`    [${c.marker}] ${c.title} — ${c.heading}`);
  } else {
    console.log("  Not enough in the store to answer that yet.");
  }
} catch (error) {
  if (error instanceof MarkvectorError) console.error(`markvector error: ${error.message}`);
  else throw error;
}
