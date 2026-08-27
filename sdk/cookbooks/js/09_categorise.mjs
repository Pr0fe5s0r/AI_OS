// 09 · Categorise — pin a document's category, then list by it.
//
// A pinned category sticks: re-ingesting the document will not overwrite a
// choice you made by hand. Passing `category` to list() filters to a single
// class.
//
//   node js/09_categorise.mjs
import { Markvector, MarkvectorError } from "markvector";

try {
  const mv = new Markvector();
  const docs = mv.collection("cookbook");

  const picked = await docs.list({ limit: 1 });
  if (!picked.length) throw new Error("Collection is empty — run 01 or 02 first.");
  const doc = picked[0];

  // File it by hand under one or more classes.
  await docs.categorise(doc.id, ["legal", "reviewed-2026"]);
  console.log(`filed ${doc.id} under legal, reviewed-2026`);

  // The choice is now visible on the document, marked pinned.
  const refreshed = await docs.get(doc.id);
  for (const c of refreshed.categories) {
    const flag = c.pinned ? "pinned" : `auto ${c.confidence.toFixed(2)}`;
    console.log(`  ${c.classId}  (${flag})`);
  }

  // List everything in a single class.
  console.log("\ndocuments in 'legal':");
  for (const d of await docs.list({ category: "legal" })) {
    console.log(`  ${d.id}  ${d.title}`);
  }
} catch (error) {
  if (error instanceof MarkvectorError) console.error(`markvector error: ${error.message}`);
  else throw error;
}
