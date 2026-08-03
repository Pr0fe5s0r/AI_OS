"use client";

import { useEffect, useRef, useState } from "react";
import * as api from "../api";
import { Point, cx, ms } from "../data";
import { Card, Chip, HeatLegend, Label, Mono, ScoreBar, VectorField } from "../ui/kit";

/** Query & chat.
 *
 *  Asks the store a question, writes an answer from what it found, and shows
 *  the passages underneath.
 *
 *  An earlier version showed passages ONLY, on the principle that every line
 *  on screen should be real text from a real document. That principle is kept
 *  rather than dropped: the answer must cite the passages it used, the
 *  citations are clickable and land on the passage, and an answer that cites
 *  nothing is marked as unsupported instead of being presented as fact. Prose
 *  you cannot trace is the thing worth refusing, not prose. */
type Turn = {
  question: string;
  mode: api.AskMode;
  answer: string;
  citations: api.Citation[];
  grounded: boolean;
  matches: Point[];
  tookMs: number;
  traceId: string;
  degraded: string | null;
};

/** Turn the [n] markers in an answer into buttons that open the passage.
 *
 *  Split rather than replaced into HTML: the answer is model output, and
 *  putting model output through anything that interprets markup is how a
 *  store starts rendering whatever a document happened to contain. */
function renderAnswer(
  text: string,
  citations: api.Citation[],
  onOpen: (c: api.Citation) => void
): React.ReactNode[] {
  const byMarker = new Map(citations.map((c) => [c.marker, c]));
  const out: React.ReactNode[] = [];
  const pattern = /\[(\d+)\]/g;
  let last = 0;
  let match: RegExpExecArray | null;

  while ((match = pattern.exec(text)) !== null) {
    if (match.index > last) out.push(text.slice(last, match.index));
    const cited = byMarker.get(Number(match[1]));
    out.push(
      cited ? (
        <button
          key={`${match.index}-${match[1]}`}
          onClick={() => onOpen(cited)}
          title={cited.heading || cited.title}
          className="mx-0.5 rounded bg-accent/20 px-1 align-super font-mono text-[0.6rem] text-accentSoft transition hover:bg-accent/40"
        >
          {match[1]}
        </button>
      ) : (
        // A marker the answer invented. It points at nothing, so it is not
        // shown as though it were checkable.
        <span key={`${match.index}-x`} />
      )
    );
    last = match.index + match[0].length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

export function Query({ active }: { active: string | null }) {
  const collectionId = active ?? undefined;
  const [question, setQuestion] = useState("");
  const [turns, setTurns] = useState<Turn[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [focus, setFocus] = useState<api.Citation | null>(null);
  // Remembered rather than reset every visit: a retrieval preference is a
  // standing choice about how you want the store to work, not a per-question
  // one. Vectorless is the default until someone says otherwise.
  const [mode, setMode] = useState<api.AskMode>("vectorless");

  useEffect(() => {
    const saved = localStorage.getItem("retrieval-mode");
    if (saved === "hybrid" || saved === "vectorless") setMode(saved);
  }, []);

  function choose(next: api.AskMode) {
    setMode(next);
    localStorage.setItem("retrieval-mode", next);
  }
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
      const out = await api.ask(collectionId, q, 8, mode);
      setTurns((t) => [
        ...t,
        {
          question: q,
          mode: out.mode,
          answer: out.answer,
          citations: out.citations,
          grounded: out.grounded,
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
    <div className="relative flex h-full min-h-0">
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex shrink-0 items-center gap-3 border-b border-edge px-6 py-3">
          <h1 className="text-sm font-semibold text-ink">Query &amp; chat</h1>
          <div className="ml-auto flex items-center gap-3">
            {/* Two ways of finding the evidence. Neither is strictly better,
                so it is a choice rather than a setting with a right answer. */}
            <div className="flex items-center gap-1.5">
              <Label>retrieval</Label>
              <div className="flex gap-1 rounded-lg border border-edge bg-elevated p-0.5">
                {(["vectorless", "hybrid"] as const).map((m) => (
                  <button
                    key={m}
                    onClick={() => choose(m)}
                    title={
                      m === "hybrid"
                        ? "Match passages by meaning and by wording, then fuse. Better at finding a specific figure or identifier anywhere in a collection."
                        : "Read each document's table of contents and reason about which sections answer the question. No embeddings. Better on long structured documents. (default)"
                    }
                    className={cx(
                      "rounded-md px-2.5 py-1 font-mono text-2xs transition",
                      mode === m ? "bg-accent/15 text-ink" : "text-subtle hover:text-muted"
                    )}
                  >
                    {m}
                  </button>
                ))}
              </div>
            </div>
            <Label>scope</Label>
            <Chip tone={collectionId ? "text-accentSoft border-accent/40 bg-accent/10" : undefined}>
              {collectionId || "all collections"}
            </Chip>
          </div>
        </header>

        <div className="min-h-0 flex-1 overflow-y-auto px-6 py-5">
          {turns.length === 0 && !busy && (
            <div className="mx-auto max-w-lg py-16 text-center">
              <h2 className="text-sm text-ink">Ask your data a question</h2>
              <p className="mt-2 text-xs leading-relaxed text-subtle">
                <span className="text-muted">hybrid</span> matches passages by meaning and
                by exact wording together. <span className="text-muted">vectorless</span>{" "}
                reads each document&rsquo;s table of contents and reasons about which sections
                answer you — no embeddings at all. Either way the answer is written only from
                what came back, every claim carries a number you can click to see the passage
                behind it, and an answer with nothing behind it is labelled as such.
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

                {/* The answer. Its citations are the whole point: a claim you
                    cannot follow back to a passage is one this store has no
                    business making. */}
                {t.answer && (
                  <div className="mb-3 pl-8">
                    <div
                      className={cx(
                        "rounded-xl border p-4",
                        t.grounded
                          ? "border-edge bg-elevated/50"
                          : "border-hot/30 bg-hot/5"
                      )}
                    >
                      <p className="whitespace-pre-wrap text-sm leading-relaxed text-ink">
                        {renderAnswer(t.answer, t.citations, setFocus)}
                      </p>

                      {!t.grounded && (
                        <p className="mt-2.5 border-t border-hot/20 pt-2 font-mono text-2xs text-hot">
                          not supported by the collection — nothing below was cited
                        </p>
                      )}

                      {t.citations.length > 0 && (
                        <div className="mt-3 flex flex-wrap items-center gap-1.5 border-t border-edge pt-2.5">
                          <Label>from</Label>
                          {t.citations.map((c) => (
                            <button
                              key={c.chunk_id}
                              onClick={() => setFocus(c)}
                              title={c.text.slice(0, 300)}
                              className="max-w-[15rem] truncate rounded-md border border-accent/30 bg-accent/10 px-2 py-0.5 font-mono text-2xs text-accentSoft transition hover:border-accent/60"
                            >
                              [{c.marker}] {c.heading || c.title}
                            </button>
                          ))}
                        </div>
                      )}
                    </div>
                  </div>
                )}

                <div className="mb-2 flex flex-wrap items-center gap-2 pl-8">
                  <Mono className="text-2xs text-subtle">
                    {/* matches are DOCUMENTS; the passages sit inside them. It
                        read eight pages of one PDF and the line said "1
                        passage", which is the wrong number AND the wrong
                        noun. */}
                    {(() => {
                      const docs = t.matches.length;
                      const passages = t.matches.reduce(
                        (n, m) => n + (m.passages?.length ?? 0),
                        0
                      );
                      const docLabel = `${docs} document${docs === 1 ? "" : "s"}`;
                      return passages > 0
                        ? `${docLabel} · ${passages} passage${passages === 1 ? "" : "s"}`
                        : docLabel;
                    })()}{" "}
                    · {ms(t.tookMs)}
                  </Mono>
                  <Chip title="which retrieval found the evidence">{t.mode}</Chip>
                  <Chip title="the derivation of this answer">trace {t.traceId.slice(0, 8)}</Chip>
                  {t.degraded && (
                    <Chip tone="text-hot border-hot/40 bg-hot/10">{t.degraded}</Chip>
                  )}
                </div>

                {t.matches.length === 0 ? (
                  <p className="pl-8 text-xs text-subtle">
                    {/* "arms" is hybrid's vocabulary — vectorless has none, it
                        reads a table of contents. Saying the wrong thing about
                        how an answer was reached is a small lie in the one
                        place this store claims to be honest. */}
                    {t.mode === "vectorless"
                      ? "No section of these documents looked relevant. The trace records the structure it considered."
                      : "Nothing in this collection matched. The trace records what each arm looked at."}
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
                            {m.heading && (
                              <div className="mt-0.5 truncate font-mono text-2xs text-accentSoft">
                                {m.heading}
                              </div>
                            )}
                            <p className="mt-1 text-xs leading-relaxed text-muted">
                              …{m.excerpt}…
                            </p>

                            {/* The document is the result; the passages are the
                                evidence. A file matching in six places is a
                                different answer from one matching in a single
                                line, and the score alone cannot say which. */}
                            {(m.passages?.length ?? 0) > 1 && (
                              <details className="mt-2 group">
                                <summary className="cursor-pointer list-none font-mono text-2xs text-subtle transition hover:text-muted">
                                  matched in {m.passages!.length} passages ▸
                                </summary>
                                <div className="mt-1.5 space-y-1.5 border-l border-edge pl-3">
                                  {m.passages!.map((p) => (
                                    <div key={p.chunk_id}>
                                      <div className="flex items-baseline gap-2">
                                        <Mono className="text-2xs text-accentSoft">
                                          {p.score.toFixed(3)}
                                        </Mono>
                                        <span className="min-w-0 flex-1 truncate font-mono text-2xs text-subtle">
                                          {p.heading || `passage ${p.ordinal + 1}`}
                                        </span>
                                      </div>
                                      <p className="mt-0.5 line-clamp-2 text-2xs leading-relaxed text-muted">
                                        {p.text}
                                      </p>
                                    </div>
                                  ))}
                                </div>
                              </details>
                            )}

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

        {/* The passage behind a citation, in full. The point of a citation is
            that it can be checked, which means the actual text has to be
            reachable without leaving the answer. */}
        {focus && (
          <div className="absolute inset-x-0 bottom-0 z-10 border-t border-edgeStrong bg-raised/97 backdrop-blur">
            <div className="mx-auto max-w-2xl px-6 py-4">
              <div className="mb-2 flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <div className="truncate text-xs font-medium text-ink">{focus.title}</div>
                  {focus.heading && (
                    <div className="truncate font-mono text-2xs text-accentSoft">
                      {focus.heading}
                    </div>
                  )}
                </div>
                <button
                  onClick={() => setFocus(null)}
                  className="shrink-0 rounded-md border border-edge px-2 py-0.5 font-mono text-2xs text-subtle transition hover:text-ink"
                >
                  close
                </button>
              </div>
              <p className="max-h-56 overflow-y-auto whitespace-pre-wrap text-xs leading-relaxed text-muted">
                {focus.text}
              </p>
            </div>
          </div>
        )}
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
