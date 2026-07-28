"use client";

import { useRef, useState } from "react";
import { Collection, HEAT_HEX, Point, retrieve, simBand } from "../data";
import { Button, Card, Chip, HeatLegend, Label, Mono, ScoreBar, VectorField } from "../ui/kit";

type Turn = {
  role: "user" | "assistant";
  text: string;
  points?: Point[];
  ms?: number;
};

const SUGGESTED = [
  "How do I rotate an API key safely?",
  "What does a 401 mean?",
  "Can I use a write key in the browser?",
];

export function Query({ collections }: { collections: Collection[] }) {
  const [col, setCol] = useState(collections[0]?.id || "");
  const [input, setInput] = useState("");
  const [turns, setTurns] = useState<Turn[]>([]);
  const [busy, setBusy] = useState(false);
  const [live, setLive] = useState<Point[] | undefined>(undefined);
  const scroller = useRef<HTMLDivElement>(null);
  const collection = collections.find((c) => c.id === col);

  function ask(q: string) {
    const query = q.trim();
    if (!query || busy) return;
    setInput("");
    setBusy(true);
    const points = retrieve(query);
    setLive(points);
    setTurns((t) => [...t, { role: "user", text: query }]);
    const t0 = performance.now();
    setTimeout(() => {
      const ms = Math.round(6 + Math.random() * 14);
      const answer = synth(query, points);
      setTurns((t) => [...t, { role: "assistant", text: answer, points, ms }]);
      setBusy(false);
      requestAnimationFrame(() =>
        scroller.current?.scrollTo({ top: scroller.current.scrollHeight, behavior: "smooth" })
      );
      void t0;
    }, 620);
  }

  return (
    <div className="flex h-full">
      {/* Chat column */}
      <div className="flex min-w-0 flex-1 flex-col">
        <div className="flex items-center gap-3 border-b border-edge px-6 py-3">
          <h1 className="text-sm font-semibold text-ink">Query & chat</h1>
          <div className="ml-auto flex items-center gap-2">
            <Label>collection</Label>
            <select
              value={col}
              onChange={(e) => setCol(e.target.value)}
              className="rounded-lg border border-edge bg-elevated px-2.5 py-1.5 font-mono text-2xs text-ink outline-none focus:border-accent/60"
            >
              {collections.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name}
                </option>
              ))}
            </select>
          </div>
        </div>

        <div ref={scroller} className="min-h-0 flex-1 space-y-4 overflow-y-auto px-6 py-6">
          {turns.length === 0 && (
            <div className="mx-auto max-w-lg pt-8 text-center">
              <p className="text-sm text-ink">Ask your data a question</p>
              <p className="mx-auto mt-1.5 max-w-sm text-xs leading-relaxed text-muted">
                Your question is embedded, matched against{" "}
                <Mono className="text-heat-2">{collection?.name}</Mono>, and the nearest
                passages are fed to the model. Every answer cites the points it used.
              </p>
              <div className="mt-4 flex flex-wrap justify-center gap-2">
                {SUGGESTED.map((s) => (
                  <Chip key={s} onClick={() => ask(s)} tone="text-muted border-edgeStrong bg-elevated">
                    {s}
                  </Chip>
                ))}
              </div>
            </div>
          )}

          {turns.map((t, i) =>
            t.role === "user" ? (
              <div key={i} className="flex justify-end">
                <div className="max-w-[80%] rounded-2xl rounded-br-sm bg-accent/15 px-4 py-2.5 text-sm text-ink">
                  {t.text}
                </div>
              </div>
            ) : (
              <div key={i} className="max-w-[88%] space-y-3 card-in">
                <div className="rounded-2xl rounded-bl-sm border border-edge bg-panel px-4 py-3">
                  <p className="text-sm leading-relaxed text-ink">{t.text}</p>
                  <div className="mt-2 flex items-center gap-2">
                    <Label>retrieved in {t.ms}ms</Label>
                    <span className="text-subtle">·</span>
                    <Label>{t.points?.length} points</Label>
                  </div>
                </div>
                {t.points && (
                  <div className="space-y-1.5 pl-1">
                    {t.points.map((p) => (
                      <div
                        key={p.id}
                        className="flex items-start gap-3 rounded-lg border border-edge bg-canvas px-3 py-2"
                      >
                        <span
                          className="mt-1 h-2 w-2 shrink-0 rounded-full"
                          style={{ background: HEAT_HEX[simBand(p.score)] }}
                        />
                        <div className="min-w-0 flex-1">
                          <div className="flex items-center justify-between gap-2">
                            <Mono className="truncate text-2xs text-muted">
                              {p.id} · {String(p.payload.title ?? "")}
                            </Mono>
                            <ScoreBar sim={p.score} />
                          </div>
                          <p className="mt-1 line-clamp-2 text-2xs leading-relaxed text-subtle">
                            {p.snippet}
                          </p>
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )
          )}

          {busy && (
            <div className="flex items-center gap-2 pl-1 text-2xs text-subtle">
              <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-accent [animation-delay:-.2s]" />
              <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-accent [animation-delay:-.1s]" />
              <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-accent" />
              <span className="ml-1 font-mono">searching {collection?.name}…</span>
            </div>
          )}
        </div>

        <div className="border-t border-edge px-6 py-3">
          <form
            onSubmit={(e) => {
              e.preventDefault();
              ask(input);
            }}
            className="flex items-center gap-2"
          >
            <input
              value={input}
              onChange={(e) => setInput(e.target.value)}
              placeholder={`Ask ${collection?.name ?? "your collection"} anything…`}
              className="flex-1 rounded-lg border border-edge bg-canvas px-3.5 py-2.5 text-sm text-ink outline-none placeholder:text-subtle focus:border-accent/60"
            />
            <Button variant="primary" type="submit" disabled={!input.trim() || busy}>
              Send
            </Button>
          </form>
        </div>
      </div>

      {/* Retrieval inspector — the vector field mirrors the last query's k-NN */}
      <aside className="hidden w-80 shrink-0 flex-col border-l border-edge bg-panel xl:flex">
        <div className="border-b border-edge px-4 py-3">
          <Label>retrieval space</Label>
        </div>
        <div className="p-3">
          <Card className="overflow-hidden field-grid">
            <VectorField neighbours={live} height={260} />
          </Card>
          <div className="mt-3 flex justify-center">
            <HeatLegend />
          </div>
        </div>
        <div className="border-t border-edge px-4 py-3">
          <Label className="mb-2 block">last matches</Label>
          {live ? (
            <div className="space-y-1.5">
              {live.map((p) => (
                <div key={p.id} className="flex items-center justify-between">
                  <Mono className="truncate text-2xs text-muted">{p.id}</Mono>
                  <ScoreBar sim={p.score} />
                </div>
              ))}
            </div>
          ) : (
            <p className="text-2xs text-subtle">Run a query to light up the field.</p>
          )}
        </div>
      </aside>
    </div>
  );
}

/** A tiny grounded-answer synthesiser: leans on the top passages so the reply
 *  reads like real RAG output without calling a model. */
function synth(q: string, points: Point[]): string {
  const top = points[0];
  if (/401|unauth/i.test(q))
    return "A 401 means the API key was missing, malformed, or revoked. Check the Authorization header reads `Bearer mvk_live_…` and confirm the key is still active on the API keys page.";
  if (/rotat/i.test(q))
    return "Rotate without downtime by creating a second key, deploying it, confirming traffic on the new key in the trace console, then revoking the old one. No requests are dropped because both keys are valid during the overlap.";
  if (/browser|client|leak/i.test(q))
    return "Don't ship a write key to the browser. Use a read-scoped key with a payload filter, or proxy queries through your own backend so the write key stays server-side.";
  return `Based on ${top.id} (“${String(top.payload.title ?? "")}”): ${top.snippet}`;
}
