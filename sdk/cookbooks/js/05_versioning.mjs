// 05 · Versioning — update a document in place and walk its history.
//
// Writing the same `locator` again does not add a copy; it supersedes the
// previous version. Old versions stay addressable, so you get an audit trail
// for free.
//
//   node js/05_versioning.mjs
import { Markvector, MarkvectorError } from "markvector";

const LOCATOR = "policy/refunds";

try {
  const mv = new Markvector();
  const docs = mv.collection("cookbook");

  // v1
  await docs.add("Refunds are issued within 30 days of purchase.", {
    locator: LOCATOR,
    title: "Refund policy",
    wait: true,
  });
  // v2 — same locator, so this supersedes v1 rather than duplicating it.
  await docs.add("Refunds are issued within 14 days of purchase. Digital goods excluded.", {
    locator: LOCATOR,
    title: "Refund policy",
    wait: true,
  });

  // Find the live document for this locator.
  const all = await docs.list({ limit: 200 });
  const current = all.find((d) => d.source.locator === LOCATOR);
  console.log(`current: v${current.version}  ${current.body}`);

  // Every version, newest first.
  console.log("\nhistory:");
  for (const v of await docs.versions(current.id)) {
    console.log(`  v${v.version}  ${v.status.padEnd(10)}  ${v.body.slice(0, 60)}…`);
  }

  // Fetch a specific old version by number.
  const first = await docs.get(current.id, { version: 1 });
  console.log(`\nv1 said: ${first.body}`);
} catch (error) {
  if (error instanceof MarkvectorError) console.error(`markvector error: ${error.message}`);
  else throw error;
}
