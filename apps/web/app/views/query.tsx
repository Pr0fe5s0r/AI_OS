"use client";

import { useEffect, useRef, useState } from "react";
import * as api from "../api";
import { Point, cx, ms } from "../data";
import {
  CitationChips,
  FocusPanel,
  renderAnswer,
  useConversation,
} from "../ui/conversation";
import { Card, Chip, HeatLegend, Label, Mono, ScoreBar, VectorField } from "../ui/kit";

const STARTER_QUESTIONS = [
  "Summarize the main topics in this collection",
  "Which documents discuss a specific topic?",
  "Compare what the sources say about a topic",
];

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
export function Query({ active }: { active: string | null }) {
  const collectionId = active ?? undefined;
  const [question, setQuestion] = useState("");
  const [focus, setFocus] = useState<api.Citation | null>(null);
  // The conversation itself — turns, the in-flight live trail, and the ask that
  // produces both — is shared with the Playground. See ui/conversation.tsx:
  // these are one conversation with different controls around it, and two
  // implementations of it is how the Playground ended up with a worse
  // rendering of citations than this page had all along.
  const { turns, live, pending, busy, error, ask: run } = useConversation(collectionId, "query");
  // Remembered rather than reset every visit: a retrieval preference is a
  // standing choice about how you want the store to work, not a per-question
  // one. Agentic is the default — it reaches the whole collection.
  const [mode, setMode] = useState<api.AskMode>("agentic");

  useEffect(() => {
    const saved = localStorage.getItem("retrieval-mode");
    if (saved === "hybrid" || saved === "agentic") setMode(saved);
    // vectorless was retired as a choice; carry an old preference over to
    // agentic, which does the same catalogue reasoning and also reaches the rest.
    else if (saved === "vectorless") setMode("agentic");
  }, []);

  function choose(next: api.AskMode) {
    setMode(next);
    localStorage.setItem("retrieval-mode", next);
  }
  // The most recent turn, for the retrieval-space aside on the right.
  const latest = turns[turns.length - 1];
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [turns, busy, live]);

  async function ask(text?: string) {
    const q = (text ?? question).trim();
    if (!q || busy) return;
    setQuestion("");
    await run(q, { mode });
  }

  return (
    <div className="relative flex h-full min-h-0">
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex shrink-0 flex-wrap items-center gap-3 border-b border-edge px-6 py-3">
          <div>
            <h1 className="text-base font-semibold text-ink">Query &amp; chat</h1>
            <p className="mt-0.5 text-2xs text-subtle">
              Answers stay linked to the passages they came from.
            </p>
          </div>
          <div className="ml-auto flex items-center gap-3">
            {/* Two modes: agentic reasons and reaches the whole collection;
                hybrid is the fast, deterministic ranking integrations build on. */}
            <div className="flex items-center gap-1.5">
              <Label>retrieval</Label>
              <div className="flex gap-1 rounded-lg border border-edge bg-elevated p-0.5">
                {(["agentic", "hybrid"] as const).map((m) => (
                  <button
                    key={m}
                    onClick={() => choose(m)}
                    title={
                      m === "agentic"
                        ? "One agent that reaches the whole collection: it reasons over the tables of contents, searches passages, and hops the similarity graph from a promising hit, then reads what it lands on. Best for most questions. (default)"
                        : "Match passages by meaning and by wording, then fuse into one ranked pass. Fast and deterministic — the primitive integrations build on."
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
          </div>
        </header>

        <div className="min-h-0 flex-1 overflow-y-auto px-6 py-5">
          {turns.length === 0 && !busy && (
            <div className="mx-auto flex min-h-[55vh] max-w-2xl flex-col items-center justify-center py-16 text-center">
              <span className="flex h-10 w-10 items-center justify-center rounded-xl border border-accent/30 bg-accent/10 text-lg text-accentSoft">
                ?
              </span>
              <h2 className="mt-4 text-lg font-semibold text-ink">Ask your collection</h2>
              <p className="mt-2 max-w-md text-sm leading-relaxed text-muted">
                {mode === "agentic"
                  ? "An agent navigates document structure, searches passages, and follows the similarity graph to reach the whole collection."
                  : "Combines semantic meaning with exact wording to find relevant passages, fast."}{" "}
                Every answer includes evidence you can open and verify.
              </p>
              <div className="mt-6 grid w-full gap-2 sm:grid-cols-3">
                {STARTER_QUESTIONS.map((starter) => (
                  <button
                    key={starter}
                    type="button"
                    onClick={() => setQuestion(starter)}
                    className="rounded-xl border border-edge bg-elevated/50 px-3 py-3 text-left text-xs leading-relaxed text-muted transition hover:border-accent/40 hover:bg-accent/5 hover:text-ink"
                  >
                    {starter}
                  </button>
                ))}
              </div>
            </div>
          )}

          <div className="mx-auto max-w-3xl space-y-6">
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

                      <CitationChips citations={t.citations} onOpen={setFocus} />
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
                    {t.mode === "hybrid"
                      ? "Nothing in this collection matched. The trace records what each arm looked at."
                      : "No section of these documents looked relevant. The trace records the structure it considered."}
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
              <div className="animate-rise">
                {pending && (
                  <div className="mb-3 flex items-start gap-2.5">
                    <span className="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-md bg-accent/20 font-mono text-2xs text-accentSoft">
                      ?
                    </span>
                    <p className="text-sm text-ink">{pending}</p>
                  </div>
                )}
                <div className="pl-8">
                  <div className="rounded-xl border border-edge bg-elevated/40 p-3.5">
                    <div className="mb-2 flex items-center gap-2 font-mono text-2xs text-subtle">
                      <span className="h-3 w-3 animate-spin rounded-full border-2 border-edgeStrong border-t-accent" />
                      {mode === "agentic"
                        ? "navigating structure and searching…"
                        : "embedding and searching…"}
                    </div>

                    {live && live.activity.length > 0 && (
                      <ol className="space-y-1 border-l border-edge pl-3">
                        {live.activity.map((step, i) => (
                          <li
                            key={i}
                            className={cx(
                              "font-mono text-2xs leading-relaxed",
                              step.kind === "status"
                                ? "text-subtle"
                                : step.ok === false
                                  ? "text-warn"
                                  : "text-accentSoft"
                            )}
                          >
                            {step.kind === "tool" ? "› " : ""}
                            {step.text}
                          </li>
                        ))}
                      </ol>
                    )}

                    {live?.reasoning && (
                      <p className="mt-2 line-clamp-3 whitespace-pre-wrap text-2xs italic leading-relaxed text-subtle">
                        {live.reasoning}
                      </p>
                    )}

                    {live?.draft && (
                      <p className="mt-2.5 whitespace-pre-wrap border-t border-edge pt-2.5 text-sm leading-relaxed text-ink">
                        {live.draft}
                        <span className="ml-0.5 inline-block h-3.5 w-1.5 animate-pulse bg-accent/60 align-middle" />
                      </p>
                    )}
                  </div>
                </div>
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

        <div className="shrink-0 border-t border-edge bg-canvas/95 px-6 py-3 backdrop-blur">
          <form
            onSubmit={(e) => {
              e.preventDefault();
              ask();
            }}
            className="mx-auto max-w-3xl"
          >
            <div className="flex items-end gap-2 rounded-xl border border-edge bg-elevated/40 p-2 transition focus-within:border-accent/60">
              <textarea
                autoFocus
                rows={1}
                value={question}
                onChange={(e) => setQuestion(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    ask();
                  }
                }}
                placeholder={
                  collectionId ? `Ask ${collectionId} anything…` : "Ask across all collections…"
                }
                className="max-h-36 min-h-10 flex-1 resize-none bg-transparent px-2 py-2 text-sm leading-relaxed text-ink outline-none [field-sizing:content] placeholder:text-subtle"
              />
              <button
                type="submit"
                disabled={busy || !question.trim()}
                className="h-10 rounded-lg border border-accent bg-accent px-4 text-xs font-semibold text-canvas transition hover:bg-accentSoft disabled:cursor-not-allowed disabled:border-edge disabled:bg-edge disabled:text-subtle"
              >
                Ask
              </button>
            </div>
            <div className="mt-1.5 flex flex-col gap-0.5 px-1 font-mono text-2xs text-subtle sm:flex-row sm:items-center sm:justify-between">
              <span>
                {mode === "agentic" ? "Agentic" : "Hybrid"} retrieval · citations included
              </span>
              <span>Enter to ask · Shift + Enter for a new line</span>
            </div>
          </form>
        </div>

        {/* The passage behind a citation, in full. The point of a citation is
            that it can be checked, which means the actual text has to be
            reachable without leaving the answer. */}
        {focus && <FocusPanel focus={focus} onClose={() => setFocus(null)} />}
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
