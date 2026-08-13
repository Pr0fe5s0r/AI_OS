import { AgentAnswer, Markvector, Thinking, ToolCall, ToolResult } from "markvector";

const question = process.argv.slice(2).join(" ") || "who is  R. Anitha?";

// Reads MARKVECTOR_API_KEY and MARKVECTOR_URL from the environment when the
// constructor arguments are omitted. Keys do not belong in a file that gets
// committed — an example is the easiest place in a repository to leak one.
const mv = new Markvector();

const agent = mv.collection(process.env.MARKVECTOR_COLLECTION ?? "cookbook").agent({
  apiKey: process.env.OPENAI_API_KEY,
  baseUrl: process.env.OPENAI_BASE_URL, // any OpenAI-compatible endpoint
  model: process.env.OPENAI_MODEL ?? "gpt-4o-mini",
});

for await (const event of agent.stream(question)) {
  if (event instanceof Thinking) process.stdout.write(event.text);
  else if (event instanceof ToolCall) console.log(`\n  → ${event.name}`, event.arguments);
  else if (event instanceof ToolResult) console.log(`  ← ${event.summary}`);
  else if (event instanceof AgentAnswer) console.log(`\n\nANSWER\n${event.text}`);
}

