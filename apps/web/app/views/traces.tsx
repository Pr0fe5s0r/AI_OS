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
  const [error, setError] = useState<string | null>(null);
  const [opening, setOpening] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const [list, summary] = await Promise.all([
        api.traces(active ?? undefined, {
          onlyEmpty: filter === "empty",
          onlyDegraded: filter === "degraded",
        }),
        api.traceStats(active ?? undefined).catch(() => null),
      ]);
      setRows(list);
      setStats(summary);
    } catch (cause) {
      setRows([]);
      setStats(null);
      setError(cause instanceof Error ? cause.message : "Traces could not be loaded.");
    }
  }, [active, filter]);

  const inspect = useCallback(async (id: string) => {
    setOpening(id);
    setError(null);
    try {
      setOpen(await api.trace(id));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "That trace could not be loaded.");
    } finally {
      setOpening(null);
    }
  }, []);

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
          disabled={rows === null}
          className="rounded-lg border border-edge bg-elevated px-3 py-1.5 font-mono text-2xs text-muted transition hover:text-ink disabled:cursor-wait disabled:opacity-50"
        >
          {rows === null ? "loading" : "refresh"}
        </button>
      </div>

      {error && (
        <div role="alert" className="mb-4 flex items-center justify-between gap-3 rounded-lg border border-danger/40 bg-danger/10 px-3 py-2 text-xs text-danger">
          <span>{error}</span>
          <button onClick={load} className="font-mono text-2xs underline underline-offset-2">
            try again
          </button>
        </div>
      )}

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
                  onClick={() => inspect(r.id)}
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
                <Th>Retrieval</Th>
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
                    onClick={() => inspect(t.id)}
                    className="cursor-pointer border-b border-edge/60 transition last:border-0 hover:bg-elevated"
                  >
                    <td className="max-w-0 py-2.5 pl-4 pr-3">
                      <div className="flex items-center gap-2">
                        <Chip tone={statusTone(status)}>
                          {opening === t.id ? "loading" : status}
                        </Chip>
                        <span className="truncate text-xs text-ink">{t.query}</span>
                      </div>
                    </td>
                    <td className="py-2.5 pr-3 font-mono text-2xs text-subtle">
                      {t.collectionId || "—"}
                    </td>
                    <td className="py-2.5 pr-3">
                      <Chip
                        tone={
                          t.retrieval === "vectorless"
                            ? "border-accent/30 bg-accent/10 text-accentSoft"
                            : undefined
                        }
                      >
                        {t.retrieval === "vectorless" ? "navigate" : t.retrieval}
                      </Chip>
                    </td>
                    <td className="py-2.5 pr-3">
                      <Chip
                        tone={
                          t.via === "api_key" || t.via.startsWith("sdk:")
                            ? "text-hot border-hot/30 bg-hot/10"
                            : "text-muted border-edgeStrong bg-elevated"
                        }
                      >
                        {t.via.startsWith("sdk:javascript/")
                          ? `JS SDK ${t.via.slice("sdk:javascript/".length)}`
                          : t.via.startsWith("sdk:python/")
                            ? `Python SDK ${t.via.slice("sdk:python/".length)}`
                            : t.via}
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
  // Agentic runs the navigator loop too, so its trace is a decision path, not
  // a candidate list — render it the same way, not as a fusion table.
  const navigation =
    trace.retrieval === "vectorless" ||
    trace.retrieval === "agentic" ||
    trace.config.retrieval === "vectorless" ||
    trace.config.retrieval === "agentic";
  const steps = trace.semantic.filter(isNavigationStep);

  useEffect(() => {
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [onClose]);

  return (
    <>
      <button
        type="button"
        aria-label="Close trace details"
        onClick={onClose}
        className="fixed inset-0 z-20 cursor-default bg-black/50 backdrop-blur-[1px]"
      />
      <aside
        role="dialog"
        aria-modal="true"
        aria-labelledby="trace-title"
        className="fixed inset-y-0 right-0 z-30 flex w-[min(48rem,96vw)] flex-col border-l border-edge bg-panel shadow-2xl shadow-black/60 animate-slide"
      >
        <header className="flex items-start gap-3 border-b border-edge px-5 py-4">
          <div className="min-w-0 flex-1">
            <Label>query</Label>
            <p id="trace-title" className="mt-1 break-words text-sm text-ink">{trace.query}</p>
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
              {trace.degraded}. The trace below shows how far the request got.
            </div>
          )}

          <section>
            <Label className="mb-2 block">where the time went · {ms(total)} total</Label>
            {timings.length ? (
              <>
                <div className="flex h-3 overflow-hidden rounded-md bg-elevated">
                  {timings.map(([k, v], i) => (
                    <div
                      key={k}
                      title={`${k} ${ms(v)}`}
                      style={{
                        width: `${Math.max(1, (v / total) * 100)}%`,
                        background: HEAT_HEX[i % 5],
                      }}
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
              </>
            ) : (
              <p className="text-xs text-subtle">No phase timings were recorded.</p>
            )}
          </section>

          {navigation ? (
            <NavigationTrace trace={trace} steps={steps} />
          ) : (
            <HybridTrace trace={trace} />
          )}

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

function NavigationTrace({
  trace,
  steps,
}: {
  trace: TraceDetail;
  steps: { step: number; action: string; detail: string }[];
}) {
  const documents = new Set(trace.fused.map((candidate) => candidate.item_id)).size;
  const rounds = steps.reduce((highest, step) => Math.max(highest, step.step), 0);

  return (
    <>
      <section>
        <Label className="mb-2 block">navigation summary</Label>
        <div className="grid grid-cols-3 divide-x divide-edge rounded-xl border border-edge bg-canvas">
          <TraceMeasure value={rounds} label="rounds" />
          <TraceMeasure value={trace.fused.length} label="sections read" />
          <TraceMeasure value={documents} label="documents used" />
        </div>
      </section>

      <section>
        <Label className="mb-2 block">decision path · {steps.length} steps</Label>
        {steps.length ? (
          <ol className="overflow-hidden rounded-xl border border-edge bg-canvas">
            {steps.map((step, index) => (
              <li
                key={`${step.step}-${step.action}-${index}`}
                className="grid grid-cols-[2.25rem_minmax(0,1fr)] border-b border-edge/70 last:border-0"
              >
                <div className="flex items-center justify-center border-r border-edge/70 font-mono text-2xs tabular-nums text-subtle">
                  {step.step}
                </div>
                <div className="min-w-0 px-3 py-2.5">
                  <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
                    <Chip tone={navigationTone(step.action)}>{step.action}</Chip>
                    <span className="break-words text-xs text-muted">{step.detail || "No detail"}</span>
                  </div>
                </div>
              </li>
            ))}
          </ol>
        ) : (
          <p className="rounded-xl border border-dashed border-edge px-4 py-6 text-center text-xs text-subtle">
            No navigation steps were recorded. The collection may have been empty.
          </p>
        )}
      </section>

      <section>
        <Label className="mb-2 block">
          evidence opened · {trace.fused.length} sections, {trace.returned.length} documents returned
        </Label>
        {trace.fused.length ? (
          <div className="overflow-hidden rounded-xl border border-edge bg-canvas">
            {trace.fused.map((candidate, index) => (
              <div
                key={`${candidate.chunk_id || candidate.item_id}-${index}`}
                className="grid gap-1 border-b border-edge/70 px-3 py-2.5 last:border-0 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center"
              >
                <div className="min-w-0">
                  <p className="truncate text-xs text-ink" title={candidate.title || undefined}>
                    {candidate.title || candidate.heading || "Untitled section"}
                  </p>
                  {candidate.heading && candidate.title && (
                    <p className="truncate text-2xs text-muted">{candidate.heading}</p>
                  )}
                  <Mono className="mt-0.5 block truncate text-2xs text-subtle">
                    {candidate.item_id} · {candidate.chunk_id || "section id unavailable"}
                  </Mono>
                </div>
                <Mono className="text-2xs tabular-nums text-muted">
                  read order {index + 1} · {safeScore(candidate.score, 2)}
                </Mono>
              </div>
            ))}
          </div>
        ) : (
          <p className="rounded-xl border border-dashed border-edge px-4 py-6 text-center text-xs text-subtle">
            The navigator did not open any evidence for this request.
          </p>
        )}
      </section>
    </>
  );
}

function HybridTrace({ trace }: { trace: TraceDetail }) {
  const semantic = trace.semantic.filter(isSemanticCandidate);

  return (
    <>
      <section className="grid gap-3 sm:grid-cols-2">
        <Card className="p-3">
          <Label>semantic arm</Label>
          <div className="mt-1 font-mono text-lg text-ink">{semantic.length}</div>
          <p className="text-2xs text-subtle">
            candidates by meaning
            {semantic[0] && ` · top ${safeScore(semantic[0].similarity, 3)}`}
          </p>
        </Card>
        <Card className="p-3">
          <Label>keyword arm</Label>
          <div className="mt-1 font-mono text-lg text-ink">{trace.keyword.length}</div>
          <p className="text-2xs text-subtle">
            candidates by wording
            {trace.keyword[0] && ` · top ${safeScore(trace.keyword[0].rank, 4)}`}
          </p>
        </Card>
      </section>

      <section>
        <Label className="mb-2 block">
          fusion · {trace.fused.length} scored, {trace.returned.length} returned
        </Label>
        {trace.fused.length ? (
          <Card className="overflow-x-auto">
            <table className="w-full min-w-[38rem] text-left">
              <thead>
                <tr className="border-b border-edge">
                  <Th className="pl-3">Candidate</Th>
                  <Th className="text-right">Score</Th>
                  <Th className="text-right">Semantic</Th>
                  <Th className="text-right">Keyword</Th>
                  <Th className="pr-3 text-right">Recency</Th>
                </tr>
              </thead>
              <tbody>
                {trace.fused.map((candidate, index) => (
                  <tr
                    key={`${candidate.item_id}-${candidate.chunk_id || index}`}
                    className={cx(
                      "border-b border-edge/60 last:border-0",
                      !candidate.kept && "opacity-50"
                    )}
                  >
                    <td className="max-w-[17rem] py-2 pl-3 pr-3">
                      <p className="truncate text-2xs text-ink" title={candidate.title || undefined}>
                        {candidate.title || candidate.heading || "Untitled document"}
                      </p>
                      {candidate.heading && candidate.title && (
                        <p className="truncate text-[10px] text-muted">{candidate.heading}</p>
                      )}
                      <Mono className="block truncate text-[10px] text-subtle">
                        {candidate.source ? `${candidate.source} · ` : ""}
                        {candidate.item_id}
                      </Mono>
                      {!candidate.kept && (
                        <span className="font-mono text-[10px] text-warn">below score floor</span>
                      )}
                    </td>
                    <ScoreCell value={candidate.score} digits={4} tone="text-ink" />
                    <ScoreCell value={candidate.semantic} digits={3} />
                    <ScoreCell value={candidate.keyword} digits={4} />
                    <ScoreCell value={candidate.recency} digits={3} end />
                  </tr>
                ))}
              </tbody>
            </table>
          </Card>
        ) : (
          <p className="rounded-xl border border-dashed border-edge px-4 py-6 text-center text-xs text-subtle">
            Neither retrieval arm produced a candidate to score.
          </p>
        )}
      </section>
    </>
  );
}

function TraceMeasure({ value, label }: { value: number; label: string }) {
  return (
    <div className="min-w-0 px-3 py-3">
      <div className="font-mono text-lg tabular-nums text-ink">{value}</div>
      <p className="truncate text-2xs text-subtle">{label}</p>
    </div>
  );
}

function ScoreCell({
  value,
  digits,
  tone = "text-subtle",
  end,
}: {
  value?: number;
  digits: number;
  tone?: string;
  end?: boolean;
}) {
  return (
    <td
      className={cx(
        "py-2 text-right font-mono text-2xs tabular-nums",
        end ? "pr-3" : "",
        tone
      )}
    >
      {safeScore(value, digits)}
    </td>
  );
}

function safeScore(value: number | undefined, digits: number): string {
  return typeof value === "number" && Number.isFinite(value) ? value.toFixed(digits) : "—";
}

function isNavigationStep(
  value: TraceDetail["semantic"][number]
): value is { step: number; action: string; detail: string } {
  return "step" in value && typeof value.step === "number";
}

function isSemanticCandidate(
  value: TraceDetail["semantic"][number]
): value is { item_id: string; chunk_id?: string; similarity: number } {
  return "similarity" in value && typeof value.similarity === "number";
}

function navigationTone(action: string): string {
  if (action === "read") return "border-accent/30 bg-accent/10 text-accentSoft";
  if (action === "searched") return "border-heat-2/30 bg-heat-2/10 text-heat-2";
  if (action === "answered") return "border-success/30 bg-success/10 text-success";
  if (action === "missed") return "border-warn/30 bg-warn/10 text-warn";
  return "border-edgeStrong bg-elevated text-muted";
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
