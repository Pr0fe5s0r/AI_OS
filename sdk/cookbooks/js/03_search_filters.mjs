// 03 · Search filters & traces — scope a search, then see why it ranked.
//
// Shows: restricting to specific documents (`files`), to connectors (`sources`),
// a score floor (`minScore`), and pulling the retrieval trace behind a result.
//
//   node js/03_search_filters.mjs "refund policy"
import { Markvector, MarkvectorError } from "markvector";

try {
  const query = process.argv.slice(2).join(" ") || "refund policy";
  const mv = new Markvector();
  const docs = mv.collection("cookbook");

  // 1) Whole collection, but drop weak matches with a score floor.
  console.log("# whole collection (minScore=0.2)");
  const results = await docs.search(query, { limit: 10, minScore: 0.2 });
  for (const hit of results.matches) {
    console.log(`  ${hit.score.toFixed(3)}  ${hit.title}  (${hit.matchedOn})`);
  }

  // 2) Only inside the three newest uploaded files. A scoped search never
  //    returns a document outside the set you pass.
  const picked = await docs.files({ limit: 3 });
  if (picked.length) {
    console.log(`\n# scoped to ${picked.length} files`);
    const scoped = await docs.search(query, { files: picked });
    for (const hit of scoped.matches) console.log(`  ${hit.score.toFixed(3)}  ${hit.title}`);
  }

  // 3) Only from a particular connector (text via add() defaults to
  //    source='sdk'; uploads default to 'upload').
  console.log("\n# scoped to source='upload'");
  const bySource = await docs.search(query, { sources: ["upload"] });
  for (const hit of bySource.matches) {
    console.log(`  ${hit.score.toFixed(3)}  ${hit.source.source}  ${hit.title}`);
  }

  // 4) Why did the first search rank the way it did? Every search carries a
  //    traceId; hand it back for each arm's candidates, scores and timing.
  if (results.traceId) {
    console.log(`\n# trace ${results.traceId}  (took ${results.tookMs} ms)`);
    const trace = await mv.trace(results.traceId);
    console.log("  keys:", Object.keys(trace).join(", "));
  }
} catch (error) {
  if (error instanceof MarkvectorError) console.error(`markvector error: ${error.message}`);
  else throw error;
}
