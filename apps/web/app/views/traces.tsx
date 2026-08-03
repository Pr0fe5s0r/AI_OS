"use client";

import { useCallback, useEffect, useState } from "react";
import * as api from "../api";
import {
  HEAT_HEX,
  Trace,
  TraceDetail,
  ago,
  cx,
  latencyBand,
  ms,
  num,
  statusTone,
  traceStatus,
} from "../data";
import { Card, Chip, Empty, Label, Mono, Stat } from "../ui/kit";

/** The trace console.
 *
 *  Every retrieval writes down what it did, so this screen answers the only
 *  question that matters when an answer looks wrong: why did it return that?
 *  A trace holds each arm's candidates and scores, what survived the filters,
 *  and where the time went — so a bad result is a thing you read, not a thing
 *  you guess at. */
export function Traces({ active }: { active: string | null }) {
  const collectionId = active ?? undefined;
  const [filter, setFilter] = useState<"all" | "empty" | "degraded">("all");
  const [rows, setRows] = useState<Trace[] | null>(null);
  const [stats, setStats] = useState<api.TraceStats | null>(null);
  const [open, setOpen] = useState<TraceDetail | null>(null);

  const load = useCallback(async () => {
    const [list, summary] = await Promise.all([
      api.traces(active ?? undefined, {
        onlyEmpty: filter === "empty",
        onlyDegraded: filter === "degraded",
      }),
      api.traceStats(active ?? undefined).catch(() => null),
    ]);
    setRows(list);
    setStats(summary);
  }, [active, filter]);

  useEffect(() => {
    setRows(null);
    load();
  }, [load]);

  const maxMs = Math.max(1, ...(rows || []).map((r) => r.durationMs));

  return (
    <div className="px-6 py-6">
      <div className="mb-5 flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-base font-semibold text-ink">Trace console</h1>
          <p className="mt-1 max-w-xl text-xs leading-relaxed text-subtle">
            Every query, and how it reached its answer. The same inputs against the same
            index reproduce the same results — the trace is the derivation.
          </p>
        </div>
        <button
          onClick={load}
          className="rounded-lg border border-edge bg-elevated px-3 py-1.5 font-mono text-2xs text-muted transition hover:text-ink"
        >
          refresh
        </button>
      </div>

      {stats && (
        <Card className="mb-4 flex flex-wrap gap-x-10 gap-y-4 p-4">
          <Stat value={num(stats.queries)} label={`queries · ${stats.window_hours}h`} />
          <Stat value={ms(stats.avg_ms)} label="average" />
          <Stat
            value={ms(stats.p95_ms)}
            label="p95"
            tone={stats.p95_ms >= 2000 ? "text-warn" : undefined}
          />
          <Stat
            value={num(stats.empty)}
            label="no results"
            tone={stats.empty ? "text-subtle" : undefined}
          />
          <Stat
            value={num(stats.degraded)}
            label="degraded"
            tone={stats.degraded ? "text-hot" : undefined}
          />
        </Card>
      )}

      {/* latency, most recent last */}
      {rows && rows.length > 0 && (
        <Card className="mb-4 p-4">
          <Label className="mb-3 block">request latency · most recent last</Label>
          <div className="flex h-20 items-end gap-[3px]">
            {[...rows]
              .reverse()
              .slice(-64)
              .map((r) => (
                <div
                  key={r.id}
                  title={`${r.query} — ${ms(r.durationMs)}`}
                  onClick={() => api.trace(r.id).then(setOpen)}
                  style={{
                    height: `${Math.max(6, (r.durationMs / maxMs) * 100)}%`,
                    background: HEAT_HEX[latencyBand(r.durationMs)],
                  }}
                  className="flex-1 cursor-pointer rounded-sm opacity-80 transition hover:opacity-100"
                />
              ))}
          </div>
        </Card>
      )}

      <div className="mb-3 flex flex-wrap items-center gap-2">
        <Label>scope</Label>
        <Chip tone={collectionId ? "text-accentSoft border-accent/40 bg-accent/10" : undefined}>
          {collectionId || "all collections"}
        </Chip>
        <span className="mx-2 h-4 w-px bg-edge" />
        <Label>show</Label>
        {(["all", "empty", "degraded"] as const).map((f) => (
          <Chip key={f} active={filter === f} onClick={() => setFilter(f)}>
            {f}
          </Chip>
        ))}
      </div>

      {rows === null ? (
        <div className="font-mono text-xs text-subtle">Loading traces…</div>
      ) : rows.length === 0 ? (
        <Empty
          title={filter === "all" ? "No queries yet" : `No ${filter} queries`}
          hint={
            filter === "all"
              ? "Run a search from Query & chat, or through the SDK, and it appears here with its full derivation."
              : "Nothing matched that filter — which is the good outcome."
          }
        />
      ) : (
        <Card className="overflow-hidden">
          <table className="w-full text-left">
            <thead>
              <tr className="border-b border-edge">
                <Th className="pl-4">Query</Th>
                <Th>Collection</Th>
                <Th>Via</Th>
                <Th className="text-right">Results</Th>
                <Th className="text-right">Latency</Th>
                <Th className="pr-4 text-right">When</Th>
              </tr>
            </thead>
            <tbody>
              {rows.map((t) => {
                const status = traceStatus(t);
                return (
                  <tr
                    key={t.id}
                    onClick={() => api.trace(t.id).then(setOpen)}
                    className="cursor-pointer border-b border-edge/60 transition last:border-0 hover:bg-elevated"
                  >
                    <td className="max-w-0 py-2.5 pl-4 pr-3">
                      <div className="flex items-center gap-2">
                        <Chip tone={statusTone(status)}>{status}</Chip>
                        <span className="truncate text-xs text-ink">{t.query}</span>
                      </div>
                    </td>
                    <td className="py-2.5 pr-3 font-mono text-2xs text-subtle">
                      {t.collectionId || "—"}
                    </td>
                    <td className="py-2.5 pr-3">
                      <Chip
                        tone={
                          t.via === "api_key"
                            ? "text-hot border-hot/30 bg-hot/10"
                            : "text-muted border-edgeStrong bg-elevated"
                        }
                      >
                        {t.via}
                      </Chip>
                    </td>
                    <td className="py-2.5 pr-3 text-right font-mono text-2xs tabular-nums text-muted">
                      {t.resultCount}
                    </td>
                    <td
                      className="py-2.5 pr-3 text-right font-mono text-2xs tabular-nums"
                      style={{ color: HEAT_HEX[latencyBand(t.durationMs)] }}
                    >
                      {ms(t.durationMs)}
                    </td>
                    <td className="whitespace-nowrap py-2.5 pr-4 text-right font-mono text-2xs text-subtle">
                      {ago(t.createdAt)}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </Card>
      )}

      {open && <TracePanel trace={open} onClose={() => setOpen(null)} />}
    </div>
  );
}

/** One query, in full. */
function TracePanel({ trace, onClose }: { trace: TraceDetail; onClose: () => void }) {
  const timings = Object.entries(trace.timings_ms).filter(([k]) => k !== "total");
  const total = trace.timings_ms.total || 1;

  return (
    <>
      <div onClick={onClose} className="fixed inset-0 z-20 bg-black/50 backdrop-blur-[1px]" />
      <aside className="fixed inset-y-0 right-0 z-30 flex w-[min(44rem,94vw)] flex-col border-l border-edge bg-panel shadow-2xl shadow-black/60 animate-slide">
        <header className="flex items-start gap-3 border-b border-edge px-5 py-4">
          <div className="min-w-0 flex-1">
            <Label>query</Label>
            <p className="mt-1 truncate text-sm text-ink">{trace.query}</p>
            <Mono className="mt-1 block text-2xs text-subtle">{trace.id}</Mono>
          </div>
          <button
            onClick={onClose}
            className="rounded-lg border border-edge px-2 py-1 font-mono text-2xs text-muted transition hover:text-ink"
          >
            close
          </button>
        </header>

        <div className="min-h-0 flex-1 space-y-5 overflow-y-auto px-5 py-5">
          {trace.degraded && (
            <div className="rounded-lg border border-hot/40 bg-hot/10 px-3 py-2 text-2xs text-hot">
              {trace.degraded} — this answer was produced without one of the two arms.
            </div>
          )}

          <section>
            <Label className="mb-2 block">where the time went · {ms(total)} total</Label>
            <div className="flex h-3 overflow-hidden rounded-md">
              {timings.map(([k, v], i) => (
                <div
                  key={k}
                  title={`${k} ${ms(v)}`}
                  style={{ width: `${(v / total) * 100}%`, background: HEAT_HEX[i % 5] }}
                />
              ))}
            </div>
            <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1">
              {timings.map(([k, v], i) => (
                <span key={k} className="font-mono text-2xs text-subtle">
                  <span style={{ color: HEAT_HEX[i % 5] }}>■</span> {k} {ms(v)}
                </span>
              ))}
            </div>
          </section>

          <section className="grid gap-3 sm:grid-cols-2">
            <Card className="p-3">
              <Label>semantic arm</Label>
              <div className="mt-1 font-mono text-lg text-ink">{trace.semantic.length}</div>
              <p className="text-2xs text-subtle">
                candidates by meaning
                {trace.semantic[0] && ` · top ${trace.semantic[0].similarity.toFixed(3)}`}
              </p>
            </Card>
            <Card className="p-3">
              <Label>keyword arm</Label>
              <div className="mt-1 font-mono text-lg text-ink">{trace.keyword.length}</div>
              <p className="text-2xs text-subtle">
                candidates by wording
                {trace.keyword[0] && ` · top ${trace.keyword[0].rank.toFixed(4)}`}
              </p>
            </Card>
          </section>

          <section>
            <Label className="mb-2 block">
              fusion · {trace.fused.length} scored, {trace.returned.length} returned
            </Label>
            <Card className="overflow-hidden">
              <table className="w-full text-left">
                <thead>
                  <tr className="border-b border-edge">
                    <Th className="pl-3">Item</Th>
                    <Th className="text-right">Score</Th>
                    <Th className="text-right">Semantic</Th>
                    <Th className="text-right">Keyword</Th>
                    <Th className="pr-3 text-right">Recency</Th>
                  </tr>
                </thead>
                <tbody>
                  {trace.fused.map((f) => (
                    <tr
                      key={f.item_id}
                      className={cx(
                        "border-b border-edge/60 last:border-0",
                        !f.kept && "opacity-40"
                      )}
                    >
                      <td className="py-2 pl-3">
                        <Mono className="text-2xs text-muted">{f.item_id.slice(0, 12)}…</Mono>
                        {!f.kept && (
                          <span className="ml-2 font-mono text-2xs text-subtle">below floor</span>
                        )}
                      </td>
                      <td className="py-2 text-right font-mono text-2xs tabular-nums text-ink">
                        {f.score.toFixed(4)}
                      </td>
                      <td className="py-2 text-right font-mono text-2xs tabular-nums text-subtle">
                        {f.semantic.toFixed(3)}
                      </td>
                      <td className="py-2 text-right font-mono text-2xs tabular-nums text-subtle">
                        {f.keyword.toFixed(4)}
                      </td>
                      <td className="py-2 pr-3 text-right font-mono text-2xs tabular-nums text-subtle">
                        {f.recency.toFixed(3)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </Card>
          </section>

          <section>
            <Label className="mb-2 block">configuration it ran with</Label>
            <pre className="overflow-x-auto rounded-lg border border-edge bg-canvas px-3 py-2.5 font-mono text-2xs leading-relaxed text-muted">
              {JSON.stringify(trace.config, null, 2)}
            </pre>
          </section>
        </div>
      </aside>
    </>
  );
}

function Th({ children, className }: { children?: React.ReactNode; className?: string }) {
  return (
    <th
      className={cx(
        "py-2 pr-3 font-mono text-2xs font-medium uppercase tracking-[0.14em] text-subtle",
        className
      )}
    >
      {children}
    </th>
  );
}
