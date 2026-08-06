"use client";

import { useCallback, useEffect, useState } from "react";
import * as api from "../api";
import { ago, cx, num } from "../data";
import { Button, Card, Chip, Label, Mono, Stat } from "../ui/kit";

// ---------------------------------------------------------------------------
// Summaries — an at-a-glance view of the semantic index coverage.
//
// It reads two endpoints:
//   GET  /api/collections/:id/summaries  → every card + section summary
//   GET  /api/collections/:id/mapping    → total/mapped/unmapped + mapper runs
//
// And exposes one action:
//   POST /api/collections/:id/summarize  → generate summaries on demand
// ---------------------------------------------------------------------------

type Props = {
    active: string | null;
    toast: (msg: string) => void;
};

export function Summaries({ active, toast }: Props) {
    const [summaries, setSummaries] = useState<api.IndexSummary[] | null>(null);
    const [mapping, setMapping] = useState<api.MappingProgress | null>(null);
    const [generating, setGenerating] = useState(false);
    const [filter, setFilter] = useState<"all" | "card" | "section_summary">("all");

    const load = useCallback(async () => {
        if (!active) return;
        const [s, m] = await Promise.all([
            api.fetchSummaries(active).catch(() => []),
            api.fetchMapping(active).catch(() => null),
        ]);
        setSummaries(s);
        setMapping(m);
    }, [active]);

    useEffect(() => { load(); }, [load]);

    async function generate() {
        // Already running? Ignore. The work is queued in the background and a
        // second press would only double-queue it — which is the bug this guards.
        if (!active || generating) return;
        setGenerating(true);
        try {
            const result = await api.triggerSummarize(active);
            toast(
                `Queued ${result.queued} document${result.queued === 1 ? "" : "s"} for ` +
                `summarization — the bar below fills as the worker finishes.`
            );
            await load();
            // Deliberately do NOT clear `generating` here. The enqueue call
            // returns immediately while the worker is still summarizing, so the
            // poll effect below keeps the button frozen and clears it only once
            // every passage is mapped (or progress stalls).
        } catch (e) {
            toast((e as Error).message);
            setGenerating(false);
        }
    }

    // While a generation is in flight, poll for progress and free the button
    // only when the collection is fully mapped (or coverage stops moving).
    useEffect(() => {
        if (!generating || !active) return;
        let cancelled = false;
        let lastMapped = -1;
        let stalls = 0;
        const tick = async () => {
            const [s, m] = await Promise.all([
                api.fetchSummaries(active).catch(() => null),
                api.fetchMapping(active).catch(() => null),
            ]);
            if (cancelled) return;
            if (s) setSummaries(s);
            if (!m) return;
            setMapping(m);
            if (m.unmapped === 0) {
                setGenerating(false);
                return;
            }
            // Stall guard: if coverage hasn't moved for ~30s the queue is likely
            // drained — stop waiting rather than freeze the button forever.
            if (m.mapped === lastMapped) {
                stalls += 1;
                if (stalls >= 10) setGenerating(false);
            } else {
                stalls = 0;
                lastMapped = m.mapped;
            }
        };
        const timer = setInterval(tick, 3000);
        return () => {
            cancelled = true;
            clearInterval(timer);
        };
    }, [generating, active]);

    if (!active) {
        return (
            <div className="flex h-full items-center justify-center font-mono text-xs text-subtle">
                Select a collection to view summaries
            </div>
        );
    }

    const filtered = summaries
        ? filter === "all"
            ? summaries
            : summaries.filter((s) => s.node_type === filter)
        : [];

    const cards = summaries?.filter((s) => s.node_type === "card") ?? [];
    const sections = summaries?.filter((s) => s.node_type === "section_summary") ?? [];

    const coveragePct =
        mapping && mapping.total > 0
            ? Math.round((mapping.mapped / mapping.total) * 100)
            : 0;

    return (
        <div className="mx-auto max-w-[1600px] px-6 py-6">
            {/* Header */}
            <div className="mb-5 flex flex-wrap items-start justify-between gap-4">
                <div>
                    <h1 className="text-base font-semibold text-ink">Summaries</h1>
                    <div className="mt-1.5 flex items-center gap-2">
                        <Chip
                            tone={
                                coveragePct >= 80
                                    ? "text-success border-success/30 bg-success/10"
                                    : coveragePct >= 40
                                        ? "text-warn border-warn/30 bg-warn/10"
                                        : "text-subtle border-edgeStrong bg-elevated"
                            }
                        >
                            <span
                                className={cx(
                                    "mr-1 h-1.5 w-1.5 rounded-full",
                                    coveragePct >= 80
                                        ? "bg-success"
                                        : coveragePct >= 40
                                            ? "bg-warn"
                                            : "bg-subtle"
                                )}
                            />
                            {coveragePct}% mapped
                        </Chip>
                        <span className="font-mono text-2xs text-subtle">
                            semantic navigation index
                        </span>
                    </div>
                </div>
                <Button
                    variant="primary"
                    onClick={generate}
                    disabled={generating}
                >
                    {generating ? (
                        <>
                            <span className="mr-1.5 inline-block h-3 w-3 animate-spin rounded-full border-2 border-canvas border-t-accent" />
                            Generating…
                        </>
                    ) : (
                        "Generate summaries"
                    )}
                </Button>
            </div>

            {/* Live progress while the worker summarizes — the button stays
                frozen until this reaches full or coverage stops moving. */}
            {generating && (
                <Card className="mb-5 p-4">
                    <div className="mb-2 flex items-center justify-between gap-3">
                        <div className="flex items-center gap-2">
                            <span className="h-3 w-3 animate-spin rounded-full border-2 border-edgeStrong border-t-accent" />
                            <Label>summarizing in the background</Label>
                        </div>
                        <Mono className="text-2xs text-subtle">
                            {mapping
                                ? `${num(mapping.mapped)} / ${num(mapping.total)} passages · ${coveragePct}%`
                                : "queuing…"}
                        </Mono>
                    </div>
                    <div className="h-2.5 w-full overflow-hidden rounded-full bg-elevated">
                        <div
                            className="h-full rounded-full bg-accent transition-all duration-700"
                            style={{ width: `${Math.max(4, coveragePct)}%` }}
                        />
                    </div>
                    <p className="mt-2 text-2xs leading-relaxed text-subtle">
                        Each document is summarized by a background job; the bar fills as its
                        passages come under a summary. You can leave this page — it keeps running.
                    </p>
                </Card>
            )}

            {/* Stats bar */}
            <Card className="mb-5 grid grid-cols-2 divide-x-0 divide-y divide-edge/60 p-0 sm:grid-cols-4 sm:divide-x sm:divide-y-0">
                <div className="p-4 sm:p-5">
                    <Stat value={mapping ? num(mapping.total) : "—"} label="total chunks" />
                </div>
                <div className="p-4 sm:p-5">
                    <Stat
                        value={mapping ? num(mapping.mapped) : "—"}
                        label="mapped"
                        tone={mapping && mapping.mapped > 0 ? "text-success" : undefined}
                    />
                </div>
                <div className="p-4 sm:p-5">
                    <Stat
                        value={mapping ? num(mapping.unmapped) : "—"}
                        label="unmapped"
                        tone={mapping && mapping.unmapped > 0 ? "text-warn" : undefined}
                    />
                </div>
                <div className="p-4 sm:p-5">
                    <Stat value={num(cards.length)} label="document cards" />
                </div>
            </Card>

            {/* Body */}
            <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_20rem]">
                {/* Summary list */}
                <div>
                    <div className="mb-3 flex items-center gap-2">
                        <Label>index summaries</Label>
                        <span className="font-mono text-2xs text-subtle">
                            ({filtered.length})
                        </span>
                        <div className="ml-auto flex gap-1">
                            {(["all", "card", "section_summary"] as const).map((f) => (
                                <button
                                    key={f}
                                    onClick={() => setFilter(f)}
                                    className={cx(
                                        "rounded-md px-2 py-1 font-mono text-2xs transition",
                                        filter === f
                                            ? "bg-accent/10 text-accent"
                                            : "text-muted hover:bg-elevated hover:text-ink"
                                    )}
                                >
                                    {f === "all" ? "all" : f === "card" ? "cards" : "sections"}
                                </button>
                            ))}
                        </div>
                    </div>

                    {summaries === null ? (
                        <Card className="flex items-center justify-center p-8">
                            <span className="h-4 w-4 animate-spin rounded-full border-2 border-edgeStrong border-t-accent" />
                        </Card>
                    ) : filtered.length === 0 ? (
                        <Card className="p-5">
                            <p className="text-2xs leading-relaxed text-subtle">
                                No summaries yet. Click &ldquo;Generate summaries&rdquo; to create
                                document cards and section summaries for every document in this
                                collection, or enable <Mono className="text-2xs">SUMMARIES_ENABLED</Mono> for
                                automatic generation at ingest.
                            </p>
                        </Card>
                    ) : (
                        <div className="space-y-2">
                            {filtered.map((s) => (
                                <SummaryRow key={s.chunk_id} summary={s} />
                            ))}
                        </div>
                    )}
                </div>

                {/* Sidebar: mapper run feed */}
                <div className="space-y-4">
                    <Card className="p-4">
                        <Label className="mb-2 block">coverage</Label>
                        <div className="mb-2 h-2 w-full overflow-hidden rounded-full bg-elevated">
                            <div
                                className="h-full rounded-full bg-accent transition-all duration-700"
                                style={{ width: `${coveragePct}%` }}
                            />
                        </div>
                        <p className="text-2xs text-subtle">
                            {mapping
                                ? `${num(mapping.mapped)} of ${num(mapping.total)} chunks sit under a summary.`
                                : "Loading…"}
                        </p>
                    </Card>

                    <Card className="p-4">
                        <Label className="mb-2 block">mapper feed</Label>
                        {mapping?.runs && mapping.runs.length > 0 ? (
                            <div className="max-h-80 space-y-2 overflow-y-auto">
                                {mapping.runs.map((r) => (
                                    <div
                                        key={r.id}
                                        className={cx(
                                            "rounded-lg border px-3 py-2",
                                            r.error
                                                ? "border-hot/30 bg-hot/5"
                                                : "border-edge bg-elevated"
                                        )}
                                    >
                                        {r.question && (
                                            <p className="mb-1 truncate text-2xs text-ink">
                                                &ldquo;{r.question}&rdquo;
                                            </p>
                                        )}
                                        <div className="flex items-center justify-between gap-2">
                                            {r.error ? (
                                                <Mono className="truncate text-2xs text-hot">
                                                    {r.error}
                                                </Mono>
                                            ) : (
                                                <Mono className="text-2xs text-success">
                                                    +{r.chunks_mapped} mapped
                                                </Mono>
                                            )}
                                            <Mono className="shrink-0 text-2xs text-subtle">
                                                {ago(r.created_at)}
                                            </Mono>
                                        </div>
                                    </div>
                                ))}
                            </div>
                        ) : (
                            <p className="text-2xs leading-relaxed text-subtle">
                                The probe-mapper hasn&apos;t run yet. Enable{" "}
                                <Mono className="text-2xs">MAPPER_ENABLED</Mono> to
                                activate background mapping.
                            </p>
                        )}
                    </Card>

                    <Card className="p-4">
                        <Label className="mb-2 block">how it works</Label>
                        <p className="text-2xs leading-relaxed text-subtle">
                            Summaries are a <strong>navigation-only</strong> layer. They tell the
                            agent what each document and section is about so it can decide where to
                            look — but they are never cited as evidence. Answers always point at
                            the real passage.
                        </p>
                    </Card>
                </div>
            </div>
        </div>
    );
}

// ---------------------------------------------------------------------------
// One row in the summary list.
// ---------------------------------------------------------------------------

function SummaryRow({ summary: s }: { summary: api.IndexSummary }) {
    const [open, setOpen] = useState(false);

    const isCard = s.node_type === "card";
    const badge = isCard ? "card" : "section";
    const badgeTone = isCard
        ? "text-accent border-accent/30 bg-accent/10"
        : "text-heat-2 border-heat-2/30 bg-heat-2/10";

    return (
        <Card className="overflow-hidden">
            <button
                onClick={() => setOpen((o) => !o)}
                className="flex w-full items-center gap-3 px-4 py-3 text-left transition hover:bg-elevated/50"
            >
                <Chip tone={badgeTone}>{badge}</Chip>
                <span className="min-w-0 flex-1 truncate text-xs text-ink">
                    {s.heading || "(untitled)"}
                </span>
                <Mono className="shrink-0 text-2xs text-subtle">
                    covers {s.covers}
                </Mono>
                {s.generated_by && (
                    <Chip tone="text-subtle border-edge bg-elevated">
                        {s.generated_by}
                    </Chip>
                )}
                <svg
                    width="12"
                    height="12"
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="2"
                    className={cx("text-subtle transition", open && "rotate-180")}
                >
                    <path d="M6 9l6 6 6-6" />
                </svg>
            </button>

            {open && (
                <div className="border-t border-edge bg-canvas/50 px-4 py-3">
                    {s.text && (
                        <p className="mb-2 whitespace-pre-wrap text-2xs leading-relaxed text-muted">
                            {s.text}
                        </p>
                    )}
                    <div className="flex flex-wrap gap-x-4 gap-y-1 text-2xs text-subtle">
                        {s.item_id && (
                            <span>
                                <strong className="text-muted">doc:</strong>{" "}
                                <Mono className="text-2xs">{s.item_id}</Mono>
                            </span>
                        )}
                        {s.probe_question && (
                            <span>
                                <strong className="text-muted">probe:</strong>{" "}
                                &ldquo;{s.probe_question}&rdquo;
                            </span>
                        )}
                        <span>
                            <strong className="text-muted">id:</strong>{" "}
                            <Mono className="text-2xs">{s.chunk_id.slice(0, 12)}…</Mono>
                        </span>
                    </div>
                </div>
            )}
        </Card>
    );
}
