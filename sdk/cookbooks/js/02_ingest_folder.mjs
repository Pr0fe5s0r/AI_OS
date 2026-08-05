// 02 · Ingest a folder — bulk-upload every PDF/.docx/.md/.txt in a directory.
//
// Re-running is safe: each file's path is used as its `locator`, so a second run
// updates the existing document instead of adding a duplicate.
//
//   node js/02_ingest_folder.mjs ./reports
import { readdir } from "node:fs/promises";
import { join, relative, extname } from "node:path";
import { Markvector, MarkvectorError } from "markvector";

const SUPPORTED = new Set([".pdf", ".docx", ".md", ".txt"]);

async function walk(dir) {
  const out = [];
  for (const entry of await readdir(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) out.push(...(await walk(full)));
    else if (SUPPORTED.has(extname(entry.name).toLowerCase())) out.push(full);
  }
  return out.sort();
}

try {
  const root = process.argv[2] ?? ".";
  const files = await walk(root);
  if (files.length === 0) throw new Error(`No PDF/.docx/.md/.txt files under ${root}`);

  const mv = new Markvector();
  const docs = mv.collection("cookbook");

  // Kick off every upload without waiting, then wait on the last one — by the
  // time it is searchable, the earlier ones almost certainly are too.
  for (let i = 0; i < files.length; i++) {
    const last = i === files.length - 1;
    // locator = path relative to the folder, so the same file always maps to
    // the same document across runs.
    const locator = relative(root, files[i]).replaceAll("\\", "/");
    const result = await docs.addFile(files[i], { locator, wait: last });
    console.log(`  ${result.document ? "indexed" : "queued "}  ${locator}`);
  }

  console.log(`\n${files.length} files in collection '${docs.id}':`);
  for (const doc of await docs.files()) {
    console.log(`  ${doc.id}  ${doc.source.locator}  (${doc.original?.size ?? 0} bytes)`);
  }
} catch (error) {
  if (error instanceof MarkvectorError) console.error(`markvector error: ${error.message}`);
  else throw error;
}
