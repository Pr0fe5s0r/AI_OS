import { Markvector, MarkvectorError } from "markvector";

const mv = new Markvector(); // MARKVECTOR_API_KEY / MARKVECTOR_URL
const docs = mv.collection(process.env.MARKVECTOR_COLLECTION ?? "default");

try {
  const results = await docs.search(process.argv.slice(2).join(" ") || "refund policy");
  for (const hit of results.matches) {
    console.log(`${hit.score.toFixed(3)}  ${hit.title}  (${hit.matchedOn})`);
    console.log(`  ${hit.cleanExcerpt}`);
  }
  console.log(`trace: ${results.traceId}`);
} catch (error) {
  if (error instanceof MarkvectorError) console.error(`markvector: ${error.message}`);
  else throw error;
}

