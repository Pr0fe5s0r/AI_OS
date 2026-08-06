import { AgentAnswer, Markvector, Thinking, ToolCall, ToolResult } from "markvector";

const question = process.argv.slice(2).join(" ") || "who is  R. Anitha?";

const mv = new Markvector({apiKey: "kb_live_PmIEhz0lebWBS1h2xWm3azF-9Wn5WbaPCxZMjODAjnQ", baseUrl: "http://localhost:8000"}); // MARKVECTOR_API_KEY / MARKVECTOR_URL

const agent = mv.collection("checking-collection").agent({
  apiKey: "v1.CmMKHHN0YXRpY2tleS1lMDBoajFjNTZiaHQ0M3BiNjUSIXNlcnZpY2VhY2NvdW50LWUwMGdhNmtrdnAxNDdxMXhxdzILCKbp29AGEPr69yc6DAik7PObBxDAw671AkACWgNlMDA.AAAAAAAAAAEbKvVycfxZnzQDxNeyjzAMx1bTwSUobZmE69qw655yYIdw5vrH2B-Q8QNUrHRkxGDaUV41fZ1lSJZ-ZG7pFCMM",
  baseUrl: "https://api.tokenfactory.uk-south1.nebius.com/v1",
  model: "zai-org/GLM-5.2",
});

for await (const event of agent.stream(question)) {
  if (event instanceof Thinking) process.stdout.write(event.text);
  else if (event instanceof ToolCall) console.log(`\n  → ${event.name}`, event.arguments);
  else if (event instanceof ToolResult) console.log(`  ← ${event.summary}`);
  else if (event instanceof AgentAnswer) console.log(`\n\nANSWER\n${event.text}`);
}

