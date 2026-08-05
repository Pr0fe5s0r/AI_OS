// 07 · Agent — reason + tools, streamed, with your own LLM.
//
// The agent runs entirely client-side: it reasons, calls read-only markvector
// tools (search / list / structure / read_document) to look things up, and keeps
// going until it can answer. Bring any OpenAI-compatible endpoint.
//
//   npm install openai
//   export OPENAI_API_KEY=sk-…
//   node js/07_agent.mjs "how are refunds handled?"
import { AgentAnswer, Markvector, MarkvectorError, Thinking, ToolCall, ToolResult } from "markvector";

try {
  if (!process.env.OPENAI_API_KEY) throw new Error("Set OPENAI_API_KEY to run the agent recipe.");
  const question = process.argv.slice(2).join(" ") || "how are refunds handled?";

  const mv = new Markvector();
  const docs = mv.collection("cookbook");

  const agent = docs.agent({
    apiKey: process.env.OPENAI_API_KEY, // your LLM key
    model: process.env.OPENAI_MODEL ?? "gpt-4o-mini",
    // baseUrl: "https://openrouter.ai/api/v1",  // any compatible endpoint
    instructions: "Answer in three sentences or fewer. Cite the filename.",
  });

  // Stream the chain of thought and every tool call as it happens.
  console.log(`Q: ${question}\n`);
  for await (const event of agent.stream(question)) {
    if (event instanceof Thinking) process.stdout.write(event.text);
    else if (event instanceof ToolCall) console.log(`\n  → ${event.name}`, event.arguments);
    else if (event instanceof ToolResult) console.log(`  ← ${event.summary}`);
    else if (event instanceof AgentAnswer) console.log(`\n\nANSWER:\n${event.text}`);
  }

  // Non-streaming variant, scoped to selected files:
  const picked = await docs.files({ limit: 3 });
  if (picked.length) {
    const result = await agent.answer("What do these say about refunds?", { files: picked });
    console.log(`\n[${result.toolCalls} tool calls] ${result.answer}`);
  }
} catch (error) {
  if (error instanceof MarkvectorError) console.error(`markvector error: ${error.message}`);
  else throw error;
}
