"use client";

import { useState } from "react";
import { Cluster, Collection, cx } from "../data";
import { Button, Card, Code, Copy, Label, Mono } from "../ui/kit";

type Lang = "python" | "javascript" | "go" | "curl";

const LANGS: { id: Lang; label: string; install: string }[] = [
  { id: "python", label: "Python", install: "pip install markvector" },
  { id: "javascript", label: "TypeScript", install: "npm install @markvector/sdk" },
  { id: "go", label: "Go", install: "go get github.com/markvector/go" },
  { id: "curl", label: "cURL", install: "# no install — just HTTP" },
];

export function Sdk({ cluster, collections }: { cluster: Cluster; collections: Collection[] }) {
  const [lang, setLang] = useState<Lang>("python");
  const col = collections[0]?.name ?? "support_docs";
  const active = LANGS.find((l) => l.id === lang)!;

  const quickstart: Record<Lang, string> = {
    python: `from markvector import Client

client = Client(
    endpoint="${cluster.endpoint}",
    api_key="mvk_live_…",
)

hits = client.search(
    collection="${col}",
    query="how do I rotate a key?",   # embedded for you
    top_k=5,
)
for h in hits:
    print(h.score, h.payload["title"])`,
    javascript: `import { Client } from "@markvector/sdk";

const client = new Client({
  endpoint: "${cluster.endpoint}",
  apiKey: process.env.MARKVECTOR_KEY,
});

const hits = await client.search({
  collection: "${col}",
  query: "how do I rotate a key?",
  topK: 5,
});

hits.forEach((h) => console.log(h.score, h.payload.title));`,
    go: `package main

import "github.com/markvector/go"

func main() {
    client := markvector.New("${cluster.endpoint}", "mvk_live_…")

    hits, _ := client.Search(markvector.SearchRequest{
        Collection: "${col}",
        Query:      "how do I rotate a key?",
        TopK:       5,
    })
    for _, h := range hits {
        println(h.Score, h.Payload["title"])
    }
}`,
    curl: `curl https://${cluster.endpoint}/collections/${col}/search \\
  -H "Authorization: Bearer mvk_live_…" \\
  -H "Content-Type: application/json" \\
  -d '{"query": "how do I rotate a key?", "top_k": 5}'`,
  };

  return (
    <div className="mx-auto max-w-4xl px-6 py-7">
      <h1 className="text-sm font-semibold text-ink">SDK & docs</h1>
      <p className="mt-1 max-w-xl text-xs leading-relaxed text-muted">
        Official clients wrap the REST API and handle batching, retries, and embedding. Pick a
        language, install, and you're one search away.
      </p>

      {/* Language tabs */}
      <div className="mt-5 flex gap-1.5">
        {LANGS.map((l) => (
          <button
            key={l.id}
            onClick={() => setLang(l.id)}
            className={cx(
              "rounded-lg border px-3.5 py-2 font-mono text-xs transition",
              lang === l.id
                ? "border-accent/50 bg-accent/10 text-ink"
                : "border-edge bg-panel text-muted hover:text-ink"
            )}
          >
            {l.label}
          </button>
        ))}
      </div>

      <div className="mt-4 space-y-4">
        <div>
          <Label className="mb-2 block">1 · install</Label>
          <div className="flex items-center gap-2 rounded-xl border border-edge bg-canvas px-4 py-3">
            <Mono className="text-subtle">$</Mono>
            <Mono className="flex-1 text-xs text-ink">{active.install}</Mono>
            <Copy text={active.install} />
          </div>
        </div>

        <div>
          <Label className="mb-2 block">2 · search your collection</Label>
          <Code code={quickstart[lang]} filename={fileFor(lang)} />
        </div>
      </div>

      {/* Resource cards */}
      <div className="mt-7 grid gap-3 sm:grid-cols-3">
        {[
          ["API reference", "Every endpoint, parameter, and error code.", "/docs/api"],
          ["Guides", "Chunking, hybrid search, filtering, reindexing.", "/docs/guides"],
          ["Changelog", "What shipped, weekly.", "/changelog"],
        ].map(([t, d, href]) => (
          <Card key={t} className="p-4" hover>
            <div className="flex items-center justify-between">
              <Mono className="text-xs font-medium text-ink">{t}</Mono>
              <span className="text-subtle">↗</span>
            </div>
            <p className="mt-1.5 text-2xs leading-relaxed text-muted">{d}</p>
            <Mono className="mt-2 block text-2xs text-accent">{href}</Mono>
          </Card>
        ))}
      </div>

      <div className="mt-6 flex items-center justify-between rounded-xl border border-edge bg-panel px-4 py-3">
        <div>
          <Mono className="text-xs text-ink">Base endpoint for {cluster.name}</Mono>
          <Mono className="mt-0.5 block text-2xs text-heat-2">https://{cluster.endpoint}</Mono>
        </div>
        <Button size="sm">
          <span className="font-mono">Open API playground →</span>
        </Button>
      </div>
    </div>
  );
}

function fileFor(l: Lang): string {
  return { python: "search.py", javascript: "search.ts", go: "main.go", curl: "search.sh" }[l];
}
