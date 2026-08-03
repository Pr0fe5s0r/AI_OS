"use client";

import { useEffect, useState } from "react";
import * as api from "../api";
import { Collection, Trace, ms, num, statusTone, traceStatus } from "../data";
import { Button, Card, Chip, HeatLegend, Label, Mono, Stat, VectorField } from "../ui/kit";

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
  const empty = documents === 0;

  return (
    <div className="px-6 py-6">
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

      <div className="grid gap-4 lg:grid-cols-3">
        <div className="space-y-4 lg:col-span-2">
          <Card className="flex flex-wrap gap-x-10 gap-y-4 p-4">
            <Stat value={num(documents)} label="documents" />
            <Stat value={stats ? num(stats.queries) : "—"} label="queries · 24h" />
            <Stat
              value={stats ? ms(stats.p95_ms) : "—"}
              label="p95 latency"
              tone={stats && stats.p95_ms >= 2000 ? "text-warn" : undefined}
            />
            <Stat
              value={stats ? num(stats.degraded) : "—"}
              label="degraded"
              tone={stats && stats.degraded ? "text-hot" : undefined}
            />
          </Card>

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
        </div>

        <div className="space-y-4">
          <Card className="overflow-hidden p-0">
            <div className="px-4 pt-4">
              <Label>retrieval space</Label>
            </div>
            <VectorField height={210} />
            <div className="px-4 pb-4">
              <HeatLegend />
              <p className="mt-2 text-2xs leading-relaxed text-subtle">
                {empty
                  ? "Nothing indexed yet — this is the shape, not your data."
                  : "Run a query to light the nearest neighbours."}
              </p>
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
