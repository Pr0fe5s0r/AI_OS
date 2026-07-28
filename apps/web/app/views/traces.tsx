"use client";

import { useMemo, useState } from "react";
import {
  Cluster,
  Collection,
  HEAT_HEX,
  Trace,
  TraceStatus,
  ago,
  cx,
  latencyBand,
  makeTraces,
  statusTone,
} from "../data";
import { Card, Chip, Label, Mono } from "../ui/kit";

const OP_TONE: Record<string, string> = {
  search: "text-heat-2 border-heat-2/40 bg-heat-2/10",
  retrieve: "text-heat-1 border-heat-1/40 bg-heat-1/10",
  upsert: "text-accentSoft border-accent/40 bg-accent/10",
  delete: "text-danger border-danger/40 bg-danger/10",
  create_index: "text-hot border-hot/40 bg-hot/10",
};

export function Traces({ cluster, collections }: { cluster: Cluster; collections: Collection[] }) {
  const all = useMemo(
    () => makeTraces(52).filter((t) => t.clusterId === cluster.id),
    [cluster.id]
  );
  const [colFilter, setColFilter] = useState<string | "all">("all");
  const [statusFilter, setStatusFilter] = useState<TraceStatus | "all">("all");
  const [open, setOpen] = useState<string | null>(null);

  const rows = all.filter(
    (t) =>
      (colFilter === "all" || t.collectionId === colFilter) &&
      (statusFilter === "all" || t.status === statusFilter)
  );

  const p50 = percentile(rows.map((r) => r.latencyMs), 50);
  const p99 = percentile(rows.map((r) => r.latencyMs), 99);
  const errs = rows.filter((r) => r.status === "error").length;
  const colName = (id: string) => collections.find((c) => c.id === id)?.name ?? id;

  return (
    <div className="mx-auto max-w-6xl px-6 py-7">
      <div className="flex items-center gap-3">
        <h1 className="text-sm font-semibold text-ink">Trace console</h1>
        <Chip tone="text-heat-1 border-heat-1/40 bg-heat-1/10">
          <span className="mr-1 h-1.5 w-1.5 animate-pulse rounded-full bg-heat-1" />
          live
        </Chip>
        <Mono className="ml-auto text-2xs text-subtle">{cluster.name}</Mono>
      </div>

      {/* Latency histogram — every request placed on the heat scale */}
      <Card className="mt-4 p-4">
        <div className="flex items-center justify-between">
          <Label>request latency · last {rows.length} ops</Label>
          <div className="flex gap-4">
            <span className="font-mono text-2xs text-subtle">
              p50 <span className="text-heat-1">{p50}ms</span>
            </span>
            <span className="font-mono text-2xs text-subtle">
              p99 <span className="text-heat-3">{p99}ms</span>
            </span>
            <span className="font-mono text-2xs text-subtle">
              errors <span className={errs ? "text-danger" : "text-success"}>{errs}</span>
            </span>
          </div>
        </div>
        <div className="mt-3 flex h-16 items-end gap-[3px]">
          {rows
            .slice()
            .reverse()
            .map((t) => {
              const h = Math.max(8, Math.min(100, (t.latencyMs / 260) * 100));
              return (
                <div
                  key={t.id}
                  title={`${t.op} · ${t.latencyMs}ms`}
                  onMouseEnter={() => setOpen(t.id)}
                  className="flex-1 rounded-t-sm transition-opacity hover:opacity-100"
                  style={{
                    height: `${h}%`,
                    background: HEAT_HEX[latencyBand(t.latencyMs)],
                    opacity: open === t.id ? 1 : 0.72,
                  }}
                />
              );
            })}
        </div>
      </Card>

      {/* Filters */}
      <div className="mt-4 flex flex-wrap items-center gap-2">
        <Label>collection</Label>
        <Chip active={colFilter === "all"} onClick={() => setColFilter("all")}>
          all
        </Chip>
        {collections.map((c) => (
          <Chip key={c.id} active={colFilter === c.id} onClick={() => setColFilter(c.id)}>
            {c.name}
          </Chip>
        ))}
        <span className="mx-1 h-4 w-px bg-edge" />
        <Label>status</Label>
        {(["all", "ok", "slow", "error"] as const).map((s) => (
          <Chip
            key={s}
            active={statusFilter === s}
            onClick={() => setStatusFilter(s)}
            tone={s === "all" ? undefined : statusTone(s)}
          >
            {s}
          </Chip>
        ))}
      </div>

      {/* Trace rows */}
      <div className="mt-3 overflow-hidden rounded-xl border border-edge">
        <div className="grid grid-cols-[auto_1fr_1.2fr_auto_auto] items-center gap-3 bg-elevated/60 px-4 py-2.5">
          {["op", "request", "collection", "latency", "when"].map((h) => (
            <Label key={h}>{h}</Label>
          ))}
        </div>
        {rows.map((t) => (
          <TraceRow
            key={t.id}
            t={t}
            colName={colName(t.collectionId)}
            open={open === t.id}
            onToggle={() => setOpen(open === t.id ? null : t.id)}
          />
        ))}
      </div>
    </div>
  );
}

function TraceRow({
  t,
  colName,
  open,
  onToggle,
}: {
  t: Trace;
  colName: string;
  open: boolean;
  onToggle: () => void;
}) {
  const band = latencyBand(t.latencyMs);
  return (
    <div className="border-t border-edge">
      <button
        onClick={onToggle}
        className={cx(
          "grid w-full grid-cols-[auto_1fr_1.2fr_auto_auto] items-center gap-3 px-4 py-2.5 text-left transition hover:bg-elevated/50",
          open && "bg-elevated/50"
        )}
      >
        <Chip tone={OP_TONE[t.op]}>{t.op}</Chip>
        <div className="flex min-w-0 items-center gap-2">
          {t.status !== "ok" && (
            <span
              className={cx(
                "h-1.5 w-1.5 shrink-0 rounded-full",
                t.status === "error" ? "bg-danger" : "bg-warn"
              )}
            />
          )}
          <Mono className="truncate text-2xs text-muted">{t.id}</Mono>
        </div>
        <Mono className="truncate text-2xs text-subtle">{colName}</Mono>
        <Mono className="tabular-nums text-2xs" >
          <span style={{ color: HEAT_HEX[band] }}>{t.latencyMs}ms</span>
        </Mono>
        <Mono className="text-2xs text-subtle">{ago(t.ts)}</Mono>
      </button>
      {open && (
        <div className="grid gap-2 border-t border-edge bg-canvas px-4 py-3 sm:grid-cols-2 card-in">
          {[
            ["request id", t.id],
            ["operation", t.op],
            ["collection", colName],
            ["status", t.status],
            ["latency", `${t.latencyMs}ms`],
            ["vectors touched", `${t.vectors}`],
            ["auth key", t.key],
            ["detail", t.detail],
          ].map(([l, v]) => (
            <div key={l} className="flex items-center justify-between gap-3 border-b border-edge/60 pb-1.5">
              <Label>{l}</Label>
              <Mono className="truncate text-2xs text-ink">{v}</Mono>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function percentile(xs: number[], p: number): number {
  if (!xs.length) return 0;
  const s = [...xs].sort((a, b) => a - b);
  return Math.round(s[Math.min(s.length - 1, Math.floor((p / 100) * s.length))]);
}
