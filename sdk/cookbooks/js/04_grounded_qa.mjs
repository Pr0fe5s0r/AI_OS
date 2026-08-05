// 04 · Grounded Q&A — a cited answer, and how to trust it.
//
// `answer.grounded` is the whole point: it is false when the store had nothing
// to answer from, so you can refuse to show a confident-sounding guess. This
// recipe also contrasts the two retrieval modes.
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
  }
}

try {
  const question = process.argv.slice(2).join(" ") || "summarise Q2 paid performance";
  const mv = new Markvector();
  const docs = mv.collection("cookbook");

  // vectorless (default): reason over each document's heading tree.
  await ask(docs, question, "vectorless");
  // hybrid: passage embeddings + keyword, fused. Try both on hard questions.
  await ask(docs, question, "hybrid");
} catch (error) {
  if (error instanceof MarkvectorError) console.error(`markvector error: ${error.message}`);
  else throw error;
}
