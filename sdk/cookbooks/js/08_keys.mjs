// 08 · API keys — mint a scoped key, list keys, revoke.
//
// A key minted with `collectionId` is confined to that collection server-side:
// every call it makes is forced into that collection whatever it asks for. The
// secret exists exactly once — in the create response. Store it then; only its
// hash is kept.
//
//   node js/08_keys.mjs
import { Markvector, MarkvectorError } from "markvector";

try {
  const mv = new Markvector();

  // A read-only key locked to one collection — safe to hand to a bot.
  const minted = await mv.createKey("cookbook-readonly-bot", {
    scopes: "read",
    collectionId: "cookbook",
  });
  console.log("NEW KEY (store it now, it is never shown again):");
  console.log(" ", minted.key);
  console.log("  scopes:", minted.scopes, "collection:", minted.collectionId);

  // List existing keys — prefixes and usage only, never the secrets.
  console.log("\nkeys in workspace:");
  for (const k of await mv.keys()) {
    const scope = k.collectionId ?? "workspace-wide";
    const state = k.revoked ? "revoked" : "active";
    console.log(`  ${k.prefix}…  ${k.name}  [${k.scopes.join(",")}]  ${scope}  ${state}`);
  }

  // Revoke the one we just made — it stops working immediately.
  await mv.revokeKey(minted.keyId);
  console.log(`\nrevoked ${minted.keyId}`);
} catch (error) {
  if (error instanceof MarkvectorError) console.error(`markvector error: ${error.message}`);
  else throw error;
}
