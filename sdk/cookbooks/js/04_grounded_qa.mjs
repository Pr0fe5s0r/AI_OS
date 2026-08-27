// 04 · Grounded Q&A — a cited answer, and how to trust it.
//
// `answer.grounded` is the whole point: it is false when the store had nothing
// to answer from, so you can refuse to show a confident-sounding guess. This
// recipe also contrasts the two retrieval modes, and prints the two things that
// let a reader check the answer rather than take it on trust: where on the page
// each citation was read from, and the route the answer took to get there.
//
//   node js/04_grounded_qa.mjs "what does the contract say about termination?"
import { Markvector, MarkvectorError } from "markvector";

async function ask(docs, question, mode) {
  const ans = await docs.answer(question, { mode });
  console.log(`\n# ${mode}  (grounded=${ans.grounded}, ${ans.tookMs} ms)`);
  if (!ans.grounded) {
    // Do not print ans.text here — an ungrounded answer is a guess.
    console.log("  The knowledge base does not cover this. Not answering.");
    return;
  }
  console.log(" ", ans.text);
  console.log("  sources:");
  for (const c of ans.citations) {
    console.log(`    [${c.marker}] ${c.title} — ${c.heading}`);
    console.log(`        ${c.text.slice(0, 100)}…`);
    // `page` is set only when the passage was read off a PICTURE of the page
    // rather than out of its text, and `regions` say where on it. Draw those
    // boxes and a transcribed table becomes checkable against the original
    // instead of merely plausible. Both are empty for text passages, which is
    // the normal case, not a failure.
    if (c.page !== null) {
      console.log(`        page ${c.page}`);
      for (const r of c.regions) {
        // Percentages of the page, so they survive whatever size you render it
        // at: multiply by your rendered width and height.
        console.log(`          box ${r.x},${r.y} ${r.w}×${r.h} — ${r.label}`);
      }
    }
  }

  // The route taken to get here — sections opened, ids reached for and missed,
  // where it stopped. It rides on the answer rather than sitting in the trace
  // because "why should I believe this" is answered by the route, and nobody
  // goes and opens a trace. Empty for hybrid, which ranks and hands over.
  if (ans.steps.length) {
    console.log("  route:");
    for (const step of ans.steps) console.log("   ", step);
  }
}

try {
  const question = process.argv.slice(2).join(" ") || "summarise Q2 paid performance";
  const mv = new Markvector();
  const docs = mv.collection("cookbook");

  // agentic (default): an agent reasons over the heading trees, searches
  // passages, and hops the similarity graph — reaching the whole collection.
  await ask(docs, question, "agentic");
  // hybrid: passage embeddings + keyword, fused. Fast and deterministic;
  // try both on hard questions.
  await ask(docs, question, "hybrid");
} catch (error) {
  if (error instanceof MarkvectorError) console.error(`markvector error: ${error.message}`);
  else throw error;
}
