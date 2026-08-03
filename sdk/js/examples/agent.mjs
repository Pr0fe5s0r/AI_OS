import { AgentAnswer, Markvector, Thinking, ToolCall, ToolResult } from "markvector";

const question = process.argv.slice(2).join(" ") || "what is the bank details?";

const mv = new Markvector({apiKey: "kb_live_U7wojtVTiKL0IvTUEHgcAfn3c4EJqlo9YDpwoaFXy74"}); // MARKVECTOR_API_KEY / MARKVECTOR_URL

const agent = mv.collection("checking-collection").agent({
  apiKey: "v1.CmMKHHN0YXRpY2tlZS1lMDBreGJhdnBxNTJwOTd6enQSIXNlcnZpY2VhY2NvdW50LWUwMHljeWt5bjhyendhNDRlcTILCP6wrswGEMDPzzM6DAj9s8aXBxCA_OuOAkACWgNlMDA.AAAAAAAAAAFX3TPuGB5p10KSS8cwpiVYwqtWfUPdUXSFnnTy4z17Vqzn8Hr2V_C-7B4BJkBtTwDviyGwibudnPbztpworoYE",
  baseUrl: "https://api.studio.nebius.com/v1",
  model: "zai-org/GLM-5.2",
});

for await (const event of agent.stream(question)) {
  if (event instanceof Thinking) process.stdout.write(event.text);
  else if (event instanceof ToolCall) console.log(`\n  → ${event.name}`, event.arguments);
  else if (event instanceof ToolResult) console.log(`  ← ${event.summary}`);
  else if (event instanceof AgentAnswer) console.log(`\n\nANSWER\n${event.text}`);
}

