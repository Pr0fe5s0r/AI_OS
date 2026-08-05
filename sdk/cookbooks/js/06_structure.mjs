// 06 · Structure (PageIndex) — the heading tree, and the chunks a doc became.
//
// structure() returns a document's own table of contents (built from its
// headings, no model calls) — this is what vectorless search reasons over.
// chunks() returns the passages the document was split into — what embedding
// search actually matches against. Two views of the same document.
//
//   node js/06_structure.mjs            // picks the first uploaded file
import { Markvector, MarkvectorError, walkSections } from "markvector";

try {
  const mv = new Markvector();
  const docs = mv.collection("cookbook");

  const files = await docs.files({ limit: 1 });
  const picked = files.length ? files : await docs.list({ limit: 1 });
  if (!picked.length) throw new Error("Collection is empty — run 01 or 02 first.");
  const doc = picked[0];

  // The heading tree. `sections` is the top level; walkSections() flattens all.
  const tree = await docs.structure(doc);
  console.log(`# structure of "${tree.title}" — ${tree.nodes} sections`);
  for (const section of walkSections(tree.sections)) {
    console.log(`  ~${String(section.tokens).padStart(5)} tok  ${section.title}`);
    if (section.opens) console.log(`              ${section.opens}`);
  }

  // The passages it was indexed as — the unit of retrieval.
  console.log("\n# chunks");
  for (const chunk of await docs.chunks(doc.id)) {
    console.log(`  #${chunk.ordinal + 1}  ${chunk.heading}`);
    console.log(`      ${chunk.text.slice(0, 100)}…`);
  }

  // Bulk: pull the structure of many files in one round trip.
  const many = await docs.files({ limit: 5 });
  if (many.length > 1) {
    console.log(`\n# structures of ${many.length} files (one call)`);
    for (const s of await docs.structures(many)) {
      console.log(`  ${s.itemId}  ${s.nodes} sections  ${s.title}`);
    }
  }
} catch (error) {
  if (error instanceof MarkvectorError) console.error(`markvector error: ${error.message}`);
  else throw error;
}
