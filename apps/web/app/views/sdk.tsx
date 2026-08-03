"use client";

import { useState } from "react";
import * as api from "../api";
import { Collection, cx } from "../data";
import { Code, Mono } from "../ui/kit";

type Lang = "javascript" | "python";
type Topic = "start" | "search" | "answer" | "files" | "agent" | "manage";

const TOPICS: { id: Topic; label: string }[] = [
  { id: "start", label: "Get started" },
  { id: "search", label: "Add & search" },
  { id: "answer", label: "Ask questions" },
  { id: "files", label: "Files" },
  { id: "agent", label: "Agent" },
  { id: "manage", label: "Manage" },
];

export function Sdk({
  collections,
  active,
}: {
  collections: Collection[];
  active: string | null;
}) {
  const [lang, setLang] = useState<Lang>("javascript");
  const [topic, setTopic] = useState<Topic>("start");
  const [copyState, setCopyState] = useState<"idle" | "copied" | "failed">("idle");
  const collection = active ?? collections[0]?.id ?? "default";

  const copyDoc = async () => {
    try {
      await navigator.clipboard.writeText(aiDoc(lang, collection, api.API));
      setCopyState("copied");
      window.setTimeout(() => setCopyState("idle"), 1800);
    } catch {
      setCopyState("failed");
    }
  };

  return (
    <div className="mx-auto max-w-5xl px-5 py-6 sm:px-8 sm:py-8">
      <header className="flex flex-wrap items-start justify-between gap-4 border-b border-edge pb-5">
        <div>
          <h1 className="text-lg font-semibold text-ink">SDK &amp; docs</h1>
          <p className="mt-1 text-sm text-muted">Choose a task. Copy the example. Run it.</p>
        </div>
        <div className="min-w-0 text-right">
          <p className="text-2xs text-subtle">Examples use</p>
          <Mono className="block max-w-64 truncate text-xs text-accentSoft">{collection}</Mono>
        </div>
      </header>

      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-edge py-4">
        <LanguageSwitch
          lang={lang}
          onChange={(next) => {
            setLang(next);
            setCopyState("idle");
          }}
        />
        <div className="flex items-center gap-3">
          <span className="hidden text-xs text-subtle sm:inline">
            {lang === "javascript" ? "Node 18+ · TypeScript ready" : "Python 3.10+"}
          </span>
          <button
            type="button"
            onClick={copyDoc}
            className={cx(
              "rounded-lg border px-3 py-2 text-xs font-semibold transition focus:outline-none focus-visible:ring-2 focus-visible:ring-accent",
              copyState === "copied"
                ? "border-success/40 bg-success/10 text-success"
                : copyState === "failed"
                  ? "border-danger/40 bg-danger/10 text-danger"
                  : "border-accent/50 bg-accent/15 text-accentSoft hover:bg-accent/25"
            )}
          >
            {copyState === "copied"
              ? "Doc copied"
              : copyState === "failed"
                ? "Copy failed"
                : "Copy Doc"}
          </button>
          <span className="sr-only" aria-live="polite">
            {copyState === "copied" ? `${lang} documentation copied` : ""}
          </span>
        </div>
      </div>

      <nav aria-label="Documentation topics" className="py-5">
        <p className="mb-2 text-xs font-medium text-muted">What do you want to do?</p>
        <div className="flex flex-wrap gap-2">
          {TOPICS.map((item) => (
            <button
              key={item.id}
              type="button"
              aria-current={topic === item.id ? "page" : undefined}
              onClick={() => setTopic(item.id)}
              className={cx(
                "rounded-lg border px-3 py-2 text-xs font-medium transition focus:outline-none focus-visible:ring-2 focus-visible:ring-accent",
                topic === item.id
                  ? "border-accent/50 bg-accent/15 text-ink"
                  : "border-edge bg-panel text-muted hover:border-edgeStrong hover:text-ink"
              )}
            >
              {item.label}
            </button>
          ))}
        </div>
      </nav>

      <main className="min-w-0 border-t border-edge pt-7">
        {topic === "start" && (
          <TopicPanel
            title="Get running in two minutes"
            description="Install MarkVector, connect to this collection, add one document, and ask a grounded question."
          >
            <Install lang={lang} />
            <Code
              filename={lang === "javascript" ? "quickstart.mjs" : "quickstart.py"}
              lang={lang}
              code={quickstart(lang, collection, api.API)}
            />
            <Note>
              Create your key under <span className="text-ink">API keys</span>. For real apps,
              put it in <Mono>MARKVECTOR_API_KEY</Mono> instead of source code.
            </Note>
          </TopicPanel>
        )}

        {topic === "search" && (
          <TopicPanel
            title="Add content and search it"
            description="A locator is the document’s stable ID. Reusing it updates the document. Use wait only when the next line must search the new content."
          >
            <LanguageCode
              lang={lang}
              python={`docs.add(
    "Q2 paid conversions fell 18 percent.",
    locator="notes/q2",
    wait=True,
)

results = docs.search("why did paid results fall", limit=8)
for hit in results:
    print(f"{hit.score:.3f}  {hit.title}")
    print(hit.clean_excerpt)

# See why these results ranked this way.
print(mv.trace(results.trace_id)["timings_ms"])`}
              javascript={`await docs.add("Q2 paid conversions fell 18 percent.", {
  locator: "notes/q2",
  wait: true,
});

const results = await docs.search("why did paid results fall", {
  limit: 8,
});

for (const hit of results.matches) {
  console.log(hit.score.toFixed(3), hit.title);
  console.log(hit.cleanExcerpt);
}

// See why these results ranked this way.
console.log(await mv.trace(results.traceId));`}
            />
            <Note>
              Search combines meaning with exact wording. Every run returns a trace ID for
              debugging ranking and latency.
            </Note>
          </TopicPanel>
        )}

        {topic === "answer" && (
          <TopicPanel
            title="Ask a grounded question"
            description="MarkVector answers only from retrieved evidence. Always check grounded before showing the result to a user."
          >
            <LanguageCode
              lang={lang}
              python={`answer = docs.answer(
    "Summarise Q2 paid performance",
    mode="vectorless",
)

if not answer.grounded:
    print("The collection does not contain enough evidence.")
else:
    print(answer.text)
    for citation in answer.citations:
        print(f"[{citation.marker}] {citation.title} — {citation.heading}")`}
              javascript={`const answer = await docs.answer("Summarise Q2 paid performance", {
  mode: "vectorless",
});

if (!answer.grounded) {
  console.log("The collection does not contain enough evidence.");
} else {
  console.log(answer.text);
  for (const citation of answer.citations) {
    console.log("[" + citation.marker + "] " + citation.title);
  }
}`}
            />
            <Note>
              <Mono>vectorless</Mono> reads document heading trees. Use <Mono>hybrid</Mono> for
              passage embeddings plus keyword retrieval.
            </Note>
          </TopicPanel>
        )}

        {topic === "files" && (
          <TopicPanel
            title="Upload and inspect files"
            description="Upload PDF, Word, Markdown, or text. MarkVector keeps the original and indexes its extracted structure."
          >
            <LanguageCode
              lang={lang}
              python={`docs.add_file("reports/q2.pdf", wait=True)

file = docs.files()[0]

# PageIndex heading tree used by vectorless retrieval.
for section in docs.structure(file).walk():
    print(section.title, section.opens)

# Download the exact original.
docs.download_original(file.id, path="q2-copy.pdf")`}
              javascript={`await docs.addFile("reports/q2.pdf", { wait: true });

const [file] = await docs.files();

// PageIndex heading tree used by vectorless retrieval.
const structure = await docs.structure(file);
for (const section of structure.sections) {
  console.log(section.title, section.opens);
}

// Download the exact original.
await docs.downloadOriginal(file.id, { path: "q2-copy.pdf" });`}
            />
            <Note>
              Need several records? Use <Mono>{lang === "javascript" ? "getMany" : "get_many"}</Mono>
              and <Mono>{lang === "javascript" ? "structures" : "structures"}</Mono> to fetch them
              in one round trip.
            </Note>
          </TopicPanel>
        )}

        {topic === "agent" && (
          <TopicPanel
            title="Run a read-only agent"
            description="Bring an OpenAI-compatible model. Ask across the entire collection, or enforce a selected-file scope for one question. The agent cannot write to the collection."
          >
            <Install lang={lang} agent />
            <LanguageCode
              lang={lang}
              python={`from markvector import ToolCall, ToolResult, AgentAnswer

agent = docs.agent(
    api_key="sk-…",
    model="gpt-4o-mini",
    instructions="Answer tersely in JSON and use ISO dates.",
)

# Pass ids or Document objects. Omit files= to use every file.
selected = docs.files()[:3]

for event in agent.stream(
    "How does the spec handle voice input?",
    files=selected,
):
    if isinstance(event, ToolCall):
        print("→", event.name, event.arguments)
    elif isinstance(event, ToolResult):
        print("←", event.summary)
    elif isinstance(event, AgentAnswer):
        print(event.text)`}
              javascript={`import { AgentAnswer, ToolCall, ToolResult } from "markvector";

const agent = docs.agent({
  apiKey: process.env.OPENAI_API_KEY,
  model: "gpt-4o-mini",
  instructions: "Answer tersely in JSON and use ISO dates.",
});

// Pass ids or Document objects. Omit files to use every file.
const selected = (await docs.files()).slice(0, 3);

for await (const event of agent.stream("How does the spec handle voice input?", {
  files: selected,
})) {
  if (event instanceof ToolCall) console.log("→", event.name, event.arguments);
  else if (event instanceof ToolResult) console.log("←", event.summary);
  else if (event instanceof AgentAnswer) console.log(event.text);
}`}
            />
            <div className="rounded-lg border border-edge bg-panel px-4 py-3">
              <h3 className="text-sm font-semibold text-ink">Custom agent instructions</h3>
              <p className="mt-1 text-xs leading-5 text-muted">
                Pass <Mono>instructions</Mono> when creating the agent to add role, tone,
                language, output-format, or domain guidance. These instructions are appended
                to the grounded, read-only system prompt. Use <Mono>system</Mono> only when you
                intentionally need to replace that complete prompt. If both are supplied,
                <Mono>instructions</Mono> is appended to your custom <Mono>system</Mono>.
              </p>
            </div>
            <Note>
              A selected-file run is a hard boundary: search, file listing, structure, and
              document reads are all restricted. Omit <Mono>files</Mono> to use the whole
              collection. Use <Mono>instructions</Mono> for additive custom prompting;
              <Mono>system</Mono> replaces the complete built-in prompt.
            </Note>
          </TopicPanel>
        )}

        {topic === "manage" && (
          <TopicPanel
            title="Manage collections and keys"
            description="Create collections and issue keys for an entire workspace or one collection. A new key secret is shown once."
          >
            <LanguageCode
              lang={lang}
              python={`collection = mv.create_collection("Client research")
mv.rename_collection(collection.collection_id, "ACME research")

key = mv.create_key(
    "ci-pipeline",
    scopes="read",
    collection_id="${collection}",
)
print(key.key)  # store this now
mv.revoke_key(key.key_id)`}
              javascript={`const created = await mv.createCollection("Client research");
await mv.renameCollection(created.collectionId, "ACME research");

const key = await mv.createKey("ci-pipeline", {
  scopes: "read",
  collectionId: "${collection}",
});
console.log(key.key); // store this now
await mv.revokeKey(key.keyId);`}
            />
            <Note>
              Client errors inherit from <Mono>MarkvectorError</Mono>. Reads retry temporary
              failures; writes do not, because a timed-out write may already have landed.
            </Note>
          </TopicPanel>
        )}
      </main>
    </div>
  );
}

function LanguageSwitch({ lang, onChange }: { lang: Lang; onChange: (lang: Lang) => void }) {
  return (
    <div className="inline-flex rounded-lg border border-edge bg-panel p-1" role="group" aria-label="SDK language">
      {(["javascript", "python"] as const).map((item) => (
        <button
          key={item}
          type="button"
          aria-pressed={lang === item}
          onClick={() => onChange(item)}
          className={cx(
            "rounded-md px-3 py-1.5 text-xs font-medium transition focus:outline-none focus-visible:ring-2 focus-visible:ring-accent",
            lang === item ? "bg-raised text-ink" : "text-subtle hover:text-muted"
          )}
        >
          {item === "javascript" ? "JavaScript" : "Python"}
        </button>
      ))}
    </div>
  );
}

function TopicPanel({
  title,
  description,
  children,
}: {
  title: string;
  description: string;
  children: React.ReactNode;
}) {
  return (
    <section>
      <h2 className="text-xl font-semibold tracking-[-0.02em] text-ink">{title}</h2>
      <p className="mb-6 mt-2 max-w-[68ch] text-sm leading-6 text-muted">{description}</p>
      <div className="space-y-4">{children}</div>
    </section>
  );
}

function LanguageCode({
  lang,
  javascript,
  python,
}: {
  lang: Lang;
  javascript: string;
  python: string;
}) {
  return (
    <Code
      filename={lang === "javascript" ? "JavaScript / TypeScript" : "Python"}
      lang={lang}
      code={lang === "javascript" ? javascript : python}
    />
  );
}

function Install({ lang, agent = false }: { lang: Lang; agent?: boolean }) {
  const command =
    lang === "javascript"
      ? `npm install markvector${agent ? " openai" : ""}`
      : agent
        ? "pip install 'markvector[agent]'"
        : "pip install markvector";
  return <Code filename="Install" lang="shell" code={command} />;
}

function Note({ children }: { children: React.ReactNode }) {
  return (
    <p className="rounded-lg bg-elevated px-3.5 py-3 text-xs leading-5 text-muted">{children}</p>
  );
}

function quickstart(lang: Lang, collection: string, base: string): string {
  if (lang === "python") {
    return `from markvector import Markvector

mv = Markvector(api_key="kb_live_…", base_url="${base}")
docs = mv.collection("${collection}")

docs.add(
    "Refunds are available within 30 days.",
    locator="policies/refunds",
    wait=True,
)

answer = docs.answer("When can a customer request a refund?")
print(answer.text if answer.grounded else "No evidence found.")`;
  }

  return `import { Markvector } from "markvector";

const mv = new Markvector({
  apiKey: "kb_live_…",
  baseUrl: "${base}",
});
const docs = mv.collection("${collection}");

await docs.add("Refunds are available within 30 days.", {
  locator: "policies/refunds",
  wait: true,
});

const answer = await docs.answer("When can a customer request a refund?");
console.log(answer.grounded ? answer.text : "No evidence found.");`;
}

/** A compact source-of-truth document meant to be pasted into an AI prompt.
 *  It follows the selected language and includes the whole SDK surface, not
 *  merely the task panel currently visible on screen. */
function aiDoc(lang: Lang, collection: string, base: string): string {
  const block = (language: string, code: string) =>
    ["```" + language, code, "```"].join("\n");
  const shared = [
    "# MarkVector SDK reference",
    "",
    "Use this document as the source of truth when generating MarkVector code. Do not invent methods or argument names.",
    "",
    `- API URL: ${base}`,
    `- Collection used in examples: ${collection}`,
    "- The API key identifies the workspace.",
    "- Text writes are asynchronous. Wait only when the next operation must read the new content.",
    "- Reusing a locator updates the existing document instead of creating a duplicate.",
    "- Search combines semantic and keyword retrieval and returns a trace ID.",
    "- Always check `grounded` before presenting an answer.",
    "- Agent tools are read-only.",
    "- Agent questions accept an optional file selection. Omit it to use all files; when provided it is enforced across every agent tool.",
    "- Agent `instructions` are appended to the protected grounding prompt. `system` replaces that prompt completely.",
    "",
  ];

  if (lang === "python") {
    return [
      ...shared,
      "## Python SDK",
      "",
      "Requires Python 3.10+. Install with `pip install markvector`. For agent support use `pip install 'markvector[agent]'`.",
      "The client reads `MARKVECTOR_API_KEY` and `MARKVECTOR_URL` when constructor arguments are omitted.",
      "",
      "## Quickstart",
      "",
      block("python", quickstart("python", collection, base)),
      "",
      "## Core API",
      "",
      "- `Markvector(api_key=..., base_url=...)` creates a workspace client.",
      "- `mv.collection(id)` returns a collection handle without a network call.",
      "- `docs.add(text, locator=..., title=..., wait=False)` stores text.",
      "- `docs.add_file(path, wait=False)` uploads PDF, Word, Markdown, or text.",
      "- `docs.search(query, limit=10, files=[...])` returns iterable results with `trace_id`.",
      "- `docs.answer(question, mode='vectorless')` returns text, citations, and `grounded`.",
      "- `docs.list()` lists documents; `docs.files()` lists uploaded files with originals.",
      "- `docs.structure(file)` returns the PageIndex heading tree.",
      "- `docs.chunks(document_id)` returns indexed passages.",
      "- `docs.download_original(document_id, path=...)` restores the uploaded file.",
      "- `docs.get_many(files)` and `docs.structures(files)` perform bulk reads.",
      "- `mv.trace(trace_id)` returns retrieval candidates, scores, and timings.",
      "- `agent.answer(question, files=[...])` and `agent.stream(question, files=[...])` restrict one run to selected ids or Document objects; omit `files` for all documents.",
      "- `docs.agent(..., instructions='...')` appends custom role, tone, format, or domain guidance; `system='...'` fully replaces the system prompt.",
      "",
      "## Search and answer",
      "",
      block(
        "python",
        `results = docs.search("refund policy", limit=8)
for hit in results:
    print(hit.score, hit.title, hit.clean_excerpt)

print(mv.trace(results.trace_id)["timings_ms"])

answer = docs.answer("When can a customer request a refund?")
if answer.grounded:
    print(answer.text)
    for citation in answer.citations:
        print(citation.marker, citation.title, citation.heading)`
      ),
      "",
      "## Agent",
      "",
      block(
        "python",
        `agent = docs.agent(
    api_key="sk-…",
    model="gpt-4o-mini",
    instructions="Answer tersely in JSON and use ISO dates.",
)
selected = docs.files()[:3]
result = agent.answer("Summarise the refund policy", files=selected)
print(result.answer, result.tool_calls)`
      ),
      "",
      "## Collections and keys",
      "",
      "Use `mv.collections()`, `mv.create_collection(name)`, `mv.rename_collection(id, name)`, and `mv.delete_collection(id)`.",
      "Use `mv.create_key(name, scopes='read', collection_id=...)`, `mv.keys()`, and `mv.revoke_key(key_id)`.",
      "All client errors inherit from `MarkvectorError`.",
    ].join("\n");
  }

  return [
    ...shared,
    "## JavaScript / TypeScript SDK",
    "",
    "Requires Node 18+. Install with `npm install markvector`. For agent support also install `openai`.",
    "The client reads `MARKVECTOR_API_KEY` and `MARKVECTOR_URL` when constructor options are omitted.",
    "",
    "## Quickstart",
    "",
    block("javascript", quickstart("javascript", collection, base)),
    "",
    "## Core API",
    "",
    "- `new Markvector({ apiKey, baseUrl })` creates a workspace client.",
    "- `mv.collection(id)` returns a collection handle without a network call.",
    "- `docs.add(text, { locator, title, wait })` stores text.",
    "- `docs.addFile(path, { wait })` uploads PDF, Word, Markdown, or text.",
    "- `docs.search(query, { limit, files })` returns `{ matches, traceId }`.",
    "- `docs.answer(question, { mode: 'vectorless' })` returns text, citations, and `grounded`.",
    "- `docs.list()` lists documents; `docs.files()` lists uploaded files with originals.",
    "- `docs.structure(file)` returns the PageIndex heading tree.",
    "- `docs.chunks(documentId)` returns indexed passages.",
    "- `docs.downloadOriginal(documentId, { path })` restores the uploaded file.",
    "- `docs.getMany(files)` and `docs.structures(files)` perform bulk reads.",
    "- `mv.trace(traceId)` returns retrieval candidates, scores, and timings.",
    "- `agent.answer(question, { files })` and `agent.stream(question, { files })` restrict one run to selected ids or Document objects; omit `files` for all documents.",
    "- `docs.agent({ instructions: '...' })` appends custom role, tone, format, or domain guidance; `system` fully replaces the system prompt.",
    "",
    "## Search and answer",
    "",
    block(
      "javascript",
      `const results = await docs.search("refund policy", { limit: 8 });
for (const hit of results.matches) {
  console.log(hit.score, hit.title, hit.cleanExcerpt);
}

console.log(await mv.trace(results.traceId));

const answer = await docs.answer("When can a customer request a refund?");
if (answer.grounded) {
  console.log(answer.text);
  for (const citation of answer.citations) console.log(citation.marker, citation.title);
}`
    ),
    "",
    "## Agent",
    "",
    block(
      "javascript",
      `const agent = docs.agent({
  apiKey: process.env.OPENAI_API_KEY,
  model: "gpt-4o-mini",
  instructions: "Answer tersely in JSON and use ISO dates.",
});
const selected = (await docs.files()).slice(0, 3);
const result = await agent.answer("Summarise the refund policy", { files: selected });
console.log(result.answer, result.toolCalls);`
    ),
    "",
    "## Collections and keys",
    "",
    "Use `mv.collections()`, `mv.createCollection(name)`, `mv.renameCollection(id, name)`, and `mv.deleteCollection(id)`.",
    "Use `mv.createKey(name, { scopes: 'read', collectionId })`, `mv.keys()`, and `mv.revokeKey(keyId)`.",
    "All client errors inherit from `MarkvectorError`.",
  ].join("\n");
}
