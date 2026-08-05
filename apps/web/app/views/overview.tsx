"use client";

import { useEffect, useState } from "react";
import * as api from "../api";
import { Collection, Trace, ms, num, statusTone, traceStatus } from "../data";
import { Button, Card, Chip, Label, Mono, Stat } from "../ui/kit";

type Section = "overview" | "upload" | "query" | "keys" | "sdk" | "playground" | "traces";

/** The knowledge base at a glance.
 *
 *  Every number here is counted from the store. An empty base shows zeroes and
 *  says what to do next, rather than being padded with something that looks
 *  like activity. */
export function Overview({
  collections,
  go,
}: {
  collections: Collection[];
  go: (s: Section) => void;
}) {
  const [stats, setStats] = useState<api.TraceStats | null>(null);
  const [recent, setRecent] = useState<Trace[]>([]);

  useEffect(() => {
    api.traceStats().then(setStats).catch(() => setStats(null));
    api.traces(undefined, { limit: 6 }).then(setRecent).catch(() => setRecent([]));
  }, []);

  const documents = collections.reduce((sum, c) => sum + c.items, 0);

  return (
    <div className="mx-auto max-w-[1600px] px-6 py-6">
      <div className="mb-5 flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-base font-semibold text-ink">Overview</h1>
          <div className="mt-1.5 flex items-center gap-2">
            <Chip tone="text-success border-success/30 bg-success/10">
              <span className="mr-1 h-1.5 w-1.5 rounded-full bg-success" />
              healthy
            </Chip>
            <span className="font-mono text-2xs text-subtle">
              your knowledge base at a glance
            </span>
          </div>
        </div>
        <div className="flex gap-2">
          <Button onClick={() => go("upload")}>Upload data</Button>
          <Button variant="primary" onClick={() => go("query")}>
            Query it
          </Button>
        </div>
      </div>

      <Card className="mb-5 grid grid-cols-2 divide-x-0 divide-y divide-edge/60 p-0 sm:grid-cols-4 sm:divide-x sm:divide-y-0">
        <div className="p-4 sm:p-5"><Stat value={num(documents)} label="documents" /></div>
        <div className="p-4 sm:p-5"><Stat value={stats ? num(stats.queries) : "—"} label="queries · 24h" /></div>
        <div className="p-4 sm:p-5">
          <Stat
            value={stats ? ms(stats.p95_ms) : "—"}
            label="p95 latency"
            tone={stats && stats.p95_ms >= 2000 ? "text-warn" : undefined}
          />
        </div>
        <div className="p-4 sm:p-5">
          <Stat
            value={stats ? num(stats.degraded) : "—"}
            label="degraded"
            tone={stats && stats.degraded ? "text-hot" : undefined}
          />
        </div>
      </Card>

      <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_20rem]">
        <div>
            <div className="mb-2 flex items-center justify-between">
              <Label>recent queries</Label>
              <button
                onClick={() => go("traces")}
                className="font-mono text-2xs text-subtle transition hover:text-ink"
              >
                trace console →
              </button>
            </div>
            {recent.length === 0 ? (
              <Card className="p-4">
                <p className="text-2xs leading-relaxed text-subtle">
                  Nothing queried yet. Every search writes down how it reached its answer,
                  and they appear here.
                </p>
              </Card>
            ) : (
              <Card className="divide-y divide-edge/60">
                {recent.map((t) => (
                  <div key={t.id} className="flex items-center gap-3 px-4 py-2.5">
                    <Chip tone={statusTone(traceStatus(t))}>{traceStatus(t)}</Chip>
                    <span className="min-w-0 flex-1 truncate text-xs text-muted">{t.query}</span>
                    <Mono className="text-2xs text-subtle">{t.resultCount} hits</Mono>
                    <Mono className="text-2xs text-subtle">{ms(t.durationMs)}</Mono>
                  </div>
                ))}
              </Card>
            )}
        </div>

        <div className="space-y-4">
          <Card className="p-4">
            <Label className="mb-2 block">next step</Label>
            <p className="text-xs leading-relaxed text-muted">
              {documents === 0
                ? "Add your first document to make this collection searchable."
                : recent.length === 0
                  ? "Your documents are ready. Ask a question to create the first trace."
                  : "Continue querying, or open Traces to inspect how each answer was produced."}
            </p>
            <div className="mt-3">
              <Button variant="primary" onClick={() => go(documents === 0 ? "upload" : "query")}>
                {documents === 0 ? "Upload a document" : "Ask a question"}
              </Button>
            </div>
          </Card>
          <Card className="p-4">
            <Label className="mb-2 block">connect</Label>
            <p className="text-2xs leading-relaxed text-subtle">
              Create a key, then read or write from anywhere — the SDK, an MCP client, a
              CI job.
            </p>
            <div className="mt-3 flex gap-2">
              <Button onClick={() => go("keys")}>API keys</Button>
              <Button onClick={() => go("sdk")}>SDK</Button>
            </div>
          </Card>
        </div>
      </div>
    </div>
  );
}
