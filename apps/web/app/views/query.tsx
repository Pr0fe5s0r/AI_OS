"use client";

import { useEffect, useRef, useState } from "react";
import * as api from "../api";
import { Collection, Point, cx, ms } from "../data";
import { Card, Chip, HeatLegend, Label, Mono, ScoreBar, VectorField } from "../ui/kit";

/** Query & chat.
 *
 *  This asks the store a question and shows what came back, with the passage
 *  that matched and a link to the trace. It deliberately does NOT compose a
 *  written answer: nothing here generates prose, so every line on screen is a
 *  passage that exists in the store, attributable to a document. */
type Turn = {
  question: string;
  matches: Point[];
  tookMs: number;
  traceId: string;
  degraded: string | null;
};

export function Query({ collections }: { collections: Collection[] }) {
  const [collectionId, setCollectionId] = useState<string | undefined>(collections[0]?.id);
  const [question, setQuestion] = useState("");
  const [turns, setTurns] = useState<Turn[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [turns, busy]);

  async function ask(text?: string) {
    const q = (text ?? question).trim();
    if (!q || busy) return;
    setBusy(true);
    setError(null);
    setQuestion("");
    try {
      const out = await api.search(collectionId, q, 8);
      setTurns((t) => [
        ...t,
        {
          question: q,
          matches: out.matches,
          tookMs: out.tookMs,
          traceId: out.traceId,
          degraded: out.degraded,
        },
      ]);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const latest = turns[turns.length - 1];

  return (
    <div className="flex h-full min-h-0">
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex shrink-0 items-center gap-3 border-b border-edge px-6 py-3">
          <h1 className="text-sm font-semibold text-ink">Query &amp; chat</h1>
          <div className="ml-auto flex items-center gap-2">
            <Label>collection</Label>
            <select
              value={collectionId || ""}
              onChange={(e) => setCollectionId(e.target.value || undefined)}
              className="rounded-lg border border-edge bg-canvas px-2.5 py-1.5 font-mono text-2xs text-ink outline-none focus:border-accent/60"
            >
              <option value="">all collections</option>
              {collections.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.id}
                </option>
              ))}
            </select>
          </div>
        </header>

        <div className="min-h-0 flex-1 overflow-y-auto px-6 py-5">
          {turns.length === 0 && !busy && (
            <div className="mx-auto max-w-lg py-16 text-center">
              <h2 className="text-sm text-ink">Ask your data a question</h2>
              <p className="mt-2 text-xs leading-relaxed text-subtle">
                Your question is embedded and matched against the collection by meaning and
                by exact wording together. Every passage shown is one that exists in a
                document — nothing here is written for you.
              </p>
            </div>
          )}

          <div className="mx-auto max-w-2xl space-y-6">
            {turns.map((t, i) => (
              <div key={i} className="animate-rise">
                <div className="mb-3 flex items-start gap-2.5">
                  <span className="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-md bg-accent/20 font-mono text-2xs text-accentSoft">
                    ?
                  </span>
                  <p className="text-sm text-ink">{t.question}</p>
                </div>

                <div className="mb-2 flex flex-wrap items-center gap-2 pl-8">
                  <Mono className="text-2xs text-subtle">
                    {t.matches.length} passage{t.matches.length === 1 ? "" : "s"} · {ms(t.tookMs)}
                  </Mono>
                  <Chip title="the derivation of this answer">trace {t.traceId.slice(0, 8)}</Chip>
                  {t.degraded && (
                    <Chip tone="text-hot border-hot/40 bg-hot/10">{t.degraded}</Chip>
                  )}
                </div>

                {t.matches.length === 0 ? (
                  <p className="pl-8 text-xs text-subtle">
                    Nothing in this collection matched. The trace records what each arm
                    looked at.
                  </p>
                ) : (
                  <div className="space-y-2 pl-8">
                    {t.matches.map((m) => (
                      <Card key={m.id} className="p-3.5">
                        <div className="flex items-start gap-3">
                          <div className="min-w-0 flex-1">
                            <div className="truncate text-xs font-medium text-ink">
                              {m.title}
                            </div>
                            <p className="mt-1 text-xs leading-relaxed text-muted">
                              …{m.excerpt}…
                            </p>
                            <div className="mt-2 flex flex-wrap items-center gap-1.5">
                              <Chip>{m.source}</Chip>
                              <Mono className="text-2xs text-subtle">{m.locator}</Mono>
                              {m.categories.map((c) => (
                                <Chip key={c.class_id}>{c.name}</Chip>
                              ))}
                            </div>
                          </div>
                          <div className="shrink-0 pt-0.5">
                            <ScoreBar sim={m.score} />
                            <div className="mt-1 text-right font-mono text-2xs text-subtle">
                              {m.semantic > 0.3 && m.keyword > 0.01
                                ? "meaning + wording"
                                : m.semantic > 0.3
                                  ? "meaning"
                                  : "wording"}
                            </div>
                          </div>
                        </div>
                      </Card>
                    ))}
                  </div>
                )}
              </div>
            ))}

            {busy && (
              <div className="flex items-center gap-2 pl-8 font-mono text-2xs text-subtle">
                <span className="h-3 w-3 animate-spin rounded-full border-2 border-edgeStrong border-t-accent" />
                embedding and searching…
              </div>
            )}
            {error && (
              <p className="rounded-lg border border-danger/30 bg-danger/10 px-3 py-2 font-mono text-2xs text-danger">
                {error}
              </p>
            )}
            <div ref={endRef} />
          </div>
        </div>

        <div className="shrink-0 border-t border-edge px-6 py-3">
          <form
            onSubmit={(e) => {
              e.preventDefault();
              ask();
            }}
            className="mx-auto flex max-w-2xl gap-2"
          >
            <input
              autoFocus
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
              placeholder={
                collectionId ? `Ask ${collectionId} anything…` : "Ask across all collections…"
              }
              className="flex-1 rounded-lg border border-edge bg-canvas px-3.5 py-2.5 text-sm text-ink outline-none transition placeholder:text-subtle focus:border-accent/60"
            />
            <button
              type="submit"
              disabled={busy || !question.trim()}
              className="rounded-lg border border-accent bg-accent px-4 text-xs font-semibold text-canvas transition hover:bg-accentSoft disabled:opacity-40"
            >
              Ask
            </button>
          </form>
        </div>
      </div>

      {/* the retrieval space */}
      <aside className="hidden w-72 shrink-0 flex-col border-l border-edge px-4 py-4 xl:flex">
        <Label>retrieval space</Label>
        <Card className="mt-2 overflow-hidden p-0">
          <VectorField height={190} neighbours={latest?.matches} />
        </Card>
        <div className="mt-3">
          <HeatLegend />
        </div>

        <Label className="mt-6 block">last matches</Label>
        {!latest || latest.matches.length === 0 ? (
          <p className="mt-2 text-2xs leading-relaxed text-subtle">
            Run a query to light up the field.
          </p>
        ) : (
          <ul className="mt-2 space-y-2">
            {latest.matches.slice(0, 6).map((m) => (
              <li key={m.id} className={cx("border-l-2 border-edge pl-2.5")}>
                <div className="truncate text-2xs text-ink">{m.title}</div>
                <Mono className="text-2xs text-subtle">{m.score.toFixed(3)}</Mono>
              </li>
            ))}
          </ul>
        )}
      </aside>
    </div>
  );
}
