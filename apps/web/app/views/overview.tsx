"use client";

import {
  Cluster,
  Collection,
  REGION_LABEL,
  ago,
  compact,
  cx,
  makeTraces,
  num,
  statusTone,
} from "../data";
import { Button, Card, Chip, HeatLegend, Label, Mono, Stat, VectorField } from "../ui/kit";

type Section = "overview" | "collections" | "upload" | "query" | "keys" | "sdk" | "traces";

export function Overview({
  cluster,
  collections,
  onOpen,
  go,
}: {
  cluster: Cluster;
  collections: Collection[];
  onOpen: (id: string) => void;
  go: (s: Section) => void;
}) {
  const totalPoints = collections.reduce((s, c) => s + c.points, 0);
  const totalSize = collections.reduce((s, c) => s + c.sizeMb, 0);
  const traces = makeTraces(60).filter((t) => t.clusterId === cluster.id);
  const p50 = median(traces.map((t) => t.latencyMs));
  const qpm = Math.round(traces.filter((t) => t.op === "search").length * 1.4);

  return (
    <div className="mx-auto max-w-6xl px-6 py-7">
      {/* Hero — the signature vector field is the thesis: this is a space of
          embeddings judged by distance, and here is a query resolving in it. */}
      <div className="grid gap-5 lg:grid-cols-[1.15fr_1fr]">
        <Card className="relative overflow-hidden field-grid">
          <div className="absolute inset-0 opacity-90">
            <VectorField height={340} />
          </div>
          <div className="relative flex h-full flex-col justify-between p-6">
            <div>
              <Label>{REGION_LABEL[cluster.region]}</Label>
              <h1 className="mt-2 font-mono text-2xl font-semibold tracking-tight text-ink">
                {cluster.name}
              </h1>
              <p className="mt-1.5 max-w-sm text-xs leading-relaxed text-muted">
                A live index of {num(totalPoints)} embeddings across{" "}
                {collections.length} collection{collections.length === 1 ? "" : "s"}. Every
                query lands here — the nearest neighbours light up on the scale below.
              </p>
            </div>
            <div className="mt-8 flex items-center justify-between">
              <HeatLegend />
              <Button variant="primary" onClick={() => go("query")}>
                Run a query →
              </Button>
            </div>
          </div>
        </Card>

        <div className="grid grid-cols-2 gap-3">
          <Card className="p-4">
            <Stat value={compact(totalPoints)} label="vectors indexed" />
          </Card>
          <Card className="p-4">
            <Stat value={`${p50}ms`} label="search p50" tone="text-heat-1" />
          </Card>
          <Card className="p-4">
            <Stat value={qpm} label="queries / min" />
          </Card>
          <Card className="p-4">
            <Stat value={`${(totalSize / 1024).toFixed(1)}GB`} label="on disk" />
          </Card>
          <Card className="col-span-2 p-4">
            <div className="flex items-center justify-between">
              <div>
                <Label>cluster health</Label>
                <div className="mt-1.5 flex items-center gap-2">
                  <Chip tone={statusTone(cluster.status)}>{cluster.status}</Chip>
                  <Mono className="text-xs text-muted">
                    {cluster.nodes} node{cluster.nodes === 1 ? "" : "s"} · {cluster.ramGb}GB RAM ·{" "}
                    {cluster.tier}
                  </Mono>
                </div>
              </div>
              <Button size="sm" onClick={() => go("traces")}>
                Traces
              </Button>
            </div>
          </Card>
        </div>
      </div>

      {/* Collections */}
      <div className="mt-8 mb-3 flex items-center justify-between">
        <h2 className="text-sm font-semibold text-ink">Collections</h2>
        <Button size="sm" onClick={() => go("upload")}>
          + Upload data
        </Button>
      </div>
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {collections.map((c) => (
          <Card key={c.id} onClick={() => onOpen(c.id)} className="p-4">
            <div className="flex items-start justify-between">
              <Mono className="text-sm font-medium text-ink">{c.name}</Mono>
              <Chip>{c.metric}</Chip>
            </div>
            <div className="mt-3 flex items-baseline gap-1.5">
              <Mono className="text-lg font-semibold tabular-nums text-ink">{num(c.points)}</Mono>
              <span className="text-2xs text-subtle">points · {c.dims}d</span>
            </div>
            <div className="mt-3">
              <div className="flex items-center justify-between">
                <Label>indexed</Label>
                <Mono className="text-2xs text-muted">{c.indexedPct}%</Mono>
              </div>
              <div className="mt-1 h-1 overflow-hidden rounded-full bg-elevated">
                <div
                  className={cx(
                    "h-full rounded-full",
                    c.indexedPct === 100 ? "bg-heat-0" : "bg-heat-2"
                  )}
                  style={{ width: `${c.indexedPct}%` }}
                />
              </div>
            </div>
            <div className="mt-3 text-2xs text-subtle">updated {ago(c.updatedAt)}</div>
          </Card>
        ))}
      </div>
    </div>
  );
}

function median(xs: number[]): number {
  if (!xs.length) return 0;
  const s = [...xs].sort((a, b) => a - b);
  return Math.round(s[Math.floor(s.length / 2)]);
}
