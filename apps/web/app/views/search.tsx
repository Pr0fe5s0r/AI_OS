"use client";

import { useState } from "react";
import { Hit, api, confidenceTone, cx, sourceTone } from "../lib";
import { Chip, Empty, Spinner } from "../ui/kit";

/** Retrieval, shown honestly.
 *
 *  Each result says how it was found — by meaning, by wording, or both. That
 *  is not decoration: when an answer is wrong, the first useful question is
 *  which arm surfaced the source, and a score with no provenance cannot
 *  answer it. */
export function Search({ onOpen }: { onOpen: (id: string) => void }) {
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<Hit[] | null>(null);
  const [ran, setRan] = useState("");
  const [busy, setBusy] = useState(false);

  async function run(e?: React.FormEvent) {
    e?.preventDefault();
    if (!q.trim()) return;
    setBusy(true);
    setRan(q);
    try {
      const res = await api<{ results: Hit[] }>(
        `/api/search?q=${encodeURIComponent(q)}&limit=20`
      );
      setHits(res.results);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="min-h-0 flex-1 overflow-y-auto px-6 py-5">
      <form onSubmit={run} className="mx-auto max-w-3xl">
        <div className="flex gap-2">
          <input
            autoFocus
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="Ask in your own words — “why did paid results fall in Q2?”"
            className="flex-1 rounded-xl border border-edge bg-panel px-4 py-3 text-sm text-ink outline-none transition placeholder:text-subtle focus:border-accent/60"
          />
          <button
            type="submit"
            disabled={busy || !q.trim()}
            className="rounded-xl border border-accent bg-accent px-5 text-sm font-medium text-white transition hover:bg-accentSoft disabled:opacity-40"
          >
            Search
          </button>
        </div>
        <p className="mt-2 text-2xs text-subtle">
          Meaning and exact wording are searched together, so a paraphrase and a
          product code both find what you need. Replaced documents are left out.
        </p>
      </form>

      <div className="mx-auto mt-6 max-w-3xl">
        {busy && <Spinner label="Searching…" />}

        {!busy && hits === null && (
          <Empty
            title="Search everything you have put in"
            hint="Results come back with the passage that matched and a link to where it came from, so you can check any answer against its source."
          />
        )}

        {!busy && hits?.length === 0 && (
          <Empty
            title={`Nothing matched “${ran}”`}
            hint="Either the knowledge base does not hold this yet, or it is filed under a brand you are not currently viewing."
          />
        )}

        {!busy && hits && hits.length > 0 && (
          <>
            <p className="mb-3 text-2xs text-subtle">
              {hits.length} result{hits.length === 1 ? "" : "s"} for “{ran}”
            </p>
            <ul className="space-y-2">
              {hits.map((h) => (
                <li key={h.item_id}>
                  <button
                    onClick={() => onOpen(h.item_id)}
                    className="w-full animate-rise rounded-xl border border-edge bg-panel px-4 py-3.5 text-left transition hover:border-edgeStrong hover:bg-elevated"
                  >
                    <div className="flex items-start gap-3">
                      <h3 className="min-w-0 flex-1 truncate text-sm font-medium text-ink">
                        {h.title}
                      </h3>
                      <Match semantic={h.semantic} keyword={h.keyword} />
                    </div>
                    <p className="mt-1.5 text-xs leading-relaxed text-muted">
                      …
                      <Highlighted text={h.excerpt} />…
                    </p>
                    <div className="mt-2.5 flex flex-wrap items-center gap-1.5">
                      <Chip tone={sourceTone(h.source.source)}>{h.source.source}</Chip>
                      <span className="font-mono text-2xs text-subtle">
                        {h.source.locator}
                      </span>
                      {(h.classes || []).map((c) => (
                        <Chip key={c.class_id} tone={confidenceTone(c.confidence, c.pinned)}>
                          {c.name}
                        </Chip>
                      ))}
                    </div>
                  </button>
                </li>
              ))}
            </ul>
          </>
        )}
      </div>
    </div>
  );
}

/** The matched words, marked.
 *
 *  The server marks them with inert tokens rather than HTML, so this renders
 *  them as elements without ever treating stored content as markup. */
function Highlighted({ text }: { text: string }) {
  const parts = text.split(/(\[\[.*?\]\])/g);
  return (
    <>
      {parts.map((part, i) =>
        part.startsWith("[[") && part.endsWith("]]") ? (
          <mark key={i} className="rounded bg-accent/25 px-0.5 text-ink">
            {part.slice(2, -2)}
          </mark>
        ) : (
          <span key={i}>{part}</span>
        )
      )}
    </>
  );
}

/** How this result was found. Both arms lit is the strongest signal there is. */
function Match({ semantic, keyword }: { semantic: number; keyword: number }) {
  const byMeaning = semantic > 0.3;
  const byWording = keyword > 0.01;
  const label = byMeaning && byWording ? "meaning + wording" : byMeaning ? "meaning" : "wording";
  return (
    <span
      title={`semantic ${semantic.toFixed(3)} · keyword ${keyword.toFixed(3)}`}
      className={cx(
        "shrink-0 rounded-full border px-2 py-0.5 text-2xs",
        byMeaning && byWording
          ? "border-success/30 bg-success/10 text-success"
          : "border-edgeStrong bg-elevated text-subtle"
      )}
    >
      {label}
    </span>
  );
}
