// 10 · Download originals — get uploaded files back byte-for-byte.
//
// MarkVector keeps the original bytes of anything uploaded via addFile, so you
// can retrieve the exact file that came in — not the Markdown it was converted
// to. Text written via add() has no original and raises NotFound.
//
//   node js/10_download_originals.mjs ./downloaded
import { mkdir } from "node:fs/promises";
import { join } from "node:path";
import { Markvector, MarkvectorError, NotFound } from "markvector";

try {
  const outDir = process.argv[2] ?? "./downloaded";
  await mkdir(outDir, { recursive: true });

  const mv = new Markvector();
  const docs = mv.collection("cookbook");

  const uploads = await docs.files(); // only docs that have a downloadable original
  if (!uploads.length) throw new Error("No uploaded files — run 02_ingest_folder first.");

  for (const doc of uploads) {
    const name = doc.original.filename;
    try {
      // Pass { path } to stream straight to disk; omit it to get the bytes.
      const written = await docs.downloadOriginal(doc.id, { path: join(outDir, name) });
      console.log(`  ${written}  (${doc.original.size} bytes, ${doc.original.contentType})`);
    } catch (error) {
      if (error instanceof NotFound) console.log(`  ${doc.id}: no stored original, skipped`);
      else throw error;
    }
  }
} catch (error) {
  if (error instanceof MarkvectorError) console.error(`markvector error: ${error.message}`);
  else throw error;
}
