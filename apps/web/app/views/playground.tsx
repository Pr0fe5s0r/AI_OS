"use client";

import { useEffect, useMemo, useState } from "react";
import * as api from "../api";
import { cx, ms } from "../data";
import type { Document } from "../data";
import { Button, Card, Chip, Code, Label, Mono } from "../ui/kit";

type Endpoint = "search" | "answer";

const ENDPOINTS: { id: Endpoint; label: string; path: string; blurb: string }[] = [
  {
    id: "search",
    label: "Search",
    path: "/api/search",
    blurb: "Ranked passages only — the contract other software builds on. No generated prose.",
  },
  {
    id: "answer",
    label: "Answer",
    path: "/api/answer",
    blurb: "Retrieval plus a written answer built only from what was retrieved, with citations.",
  },
];

/** Only the two modes the server wants sent.
 *
 *  `vectorless` is still accepted for backward compatibility and still arrives
 *  on historical traces, but it is not a choice any more — offering it here
 *  would be offering a worse agentic. */
const MODES: { id: api.AskMode; label: string; blurb: string }[] = [
  {
    id: "agentic",
    label: "Agentic",
    blurb:
      "An agent reads its way to the answer: tables of contents, then hybrid search for a figure no heading advertises, then the passages it lands on. Slower, and it can reach things ranking alone cannot.",
  },
  {
    id: "hybrid",
    label: "Hybrid",
    blurb:
      "Passage embeddings and keyword matching fused into one ranked pass. Fast and deterministic — the same question gives the same passages every time.",
  },
];

/** Behaviour presets.
 *
 *  These are STYLE, and the server enforces that: it fences whatever arrives
 *  here into a section that governs tone, length and formatting, then restores
 *  the evidence rules underneath. So a preset cannot be written that stops the
 *  answer citing what it read — which is why offering a free-text box next to
 *  them is safe. */
const BEHAVIOURS: { id: string; label: string; text: string; blurb: string }[] = [
  { id: "default", label: "Default", text: "", blurb: "The store's own voice — direct, cited, no preamble." },
  {
    id: "brief",
    label: "Brief",
    text: "Answer in at most two sentences. No preamble, no restating the question. Lead with the fact.",
    blurb: "One or two sentences, fact first.",
  },
  {
    id: "bullets",
    label: "Bullet points",
    text: "Answer as a short bulleted list. One fact per bullet, each with its citation. No introductory sentence.",
    blurb: "A list, one fact per line.",
  },
  {
    id: "analyst",
    label: "Analyst",
    text: "Write for a reader who will act on this. Lead with the answer, then the supporting detail, then anything that qualifies it. Note explicitly where the documents disagree or leave a gap.",
    blurb: "Answer first, then detail, then caveats.",
  },
  {
    id: "plain",
    label: "Plain language",
    text: "Write for a reader who is new to this subject. Avoid jargon, and when a technical term is unavoidable explain it in the same sentence. Keep sentences short.",
    blurb: "No jargon; explain the terms.",
  },
  {
    id: "extract",
    label: "Strict extract",
    text: "Quote the document verbatim wherever possible rather than paraphrasing. Keep the original wording, numbers and units exactly as written.",
    blurb: "Verbatim wording, numbers unchanged.",
  },
];

/** Starting points, not examples — each is a shape of question the store
 *  answers differently, so trying them teaches something about the retrieval
 *  rather than just filling the box. */
const QUERY_TEMPLATES: { label: string; q: string; why: string }[] = [
  { label: "Specific fact", q: "What is the ", why: "A named thing. Keyword and semantic agree, and it is fastest." },
  { label: "Terse keywords", q: "", why: "No sentence, no shared words with the answer — the hard case." },
  { label: "Across documents", q: "Compare what the sources say about ", why: "Routing has to reach more than one document." },
  { label: "From a figure", q: "What does the diagram show about ", why: "The text layer will not have it; vision reads the page." },
  { label: "Absence", q: "Does anything here cover ", why: "Should say no plainly rather than reaching for something close." },
];

function Toggle({
  on,
  onChange,
  label,
  hint,
  disabled,
}: {
  on: boolean;
  onChange: (v: boolean) => void;
  label: string;
  hint: string;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={() => !disabled && onChange(!on)}
      disabled={disabled}
      title={hint}
      className={cx(
        "flex items-center gap-2.5 rounded-lg border px-3 py-2 text-left transition",
        disabled
          ? "cursor-not-allowed border-edge bg-elevated/30 opacity-50"
          : on
            ? "border-accent/50 bg-accent/10"
            : "border-edge bg-elevated hover:border-edgeStrong"
      )}
    >
      <span
        className={cx(
          "relative h-4 w-7 shrink-0 rounded-full transition",
          on ? "bg-accent/70" : "bg-edgeStrong"
        )}
      >
        <span
          className={cx(
            "absolute top-0.5 h-3 w-3 rounded-full bg-canvas transition-all",
            on ? "left-3.5" : "left-0.5"
          )}
        />
      </span>
      <span>
        <span className="block font-mono text-2xs text-ink">{label}</span>
        <span className="block text-2xs text-subtle">{hint}</span>
      </span>
    </button>
  );
}

/** Playground — the API, run by hand, with every knob the API actually has.
 *
 *  Still the developer's console rather than a second chat window: it sends the
 *  exact request the SDK would and shows what came back verbatim. What changed
 *  is that the request is now configurable the way the endpoint is — retrieval
 *  mode, vision, document filter, and how the answer should be written — and
 *  the answer is rendered as well as dumped, so the settings can be judged by
 *  their effect and not only by their JSON.
 *
 *  Every control here maps to a real parameter. There is deliberately no knob
 *  that the server would ignore: a setting that does nothing is worse than a
 *  missing one, because it is indistinguishable from a broken one. */
export function Playground({ active }: { active: string | null }) {
  const [endpoint, setEndpoint] = useState<Endpoint>("answer");
  const [mode, setMode] = useState<api.AskMode>("agentic");
  const [vision, setVision] = useState(true);
  const [behaviourId, setBehaviourId] = useState("default");
  const [behaviour, setBehaviour] = useState("");
  const [docs, setDocs] = useState<string[]>([]);
  const [library, setLibrary] = useState<Document[]>([]);
  const [q, setQ] = useState("");
  const [limit, setLimit] = useState(8);
  const [busy, setBusy] = useState(false);
  const [turns, setTurns] = useState<
    { q: string; outcome: api.AnswerOutcome; mode: api.AskMode; vision: boolean }[]
  >([]);
  const [raw, setRaw] = useState<api.RawRun | null>(null);
  const [error, setError] = useState<string | null>(null);

  const chosen = ENDPOINTS.find((e) => e.id === endpoint)!;
  const answering = endpoint === "answer";
  // Vision is the agent's tool. Hybrid ranks and hands over — it never opens a
  // page — so the control is disabled rather than quietly ignored.
  const visionApplies = answering && mode === "agentic";

  useEffect(() => {
    let live = true;
    api
      .documents(active ?? undefined, 100)
      .then((d) => live && setLibrary(d))
      .catch(() => live && setLibrary([]));
    // The filter names documents by id, and those ids belong to the collection
    // that was selected. Kept rather than remapped when it changes: a filter
    // pointing at another collection's documents would silently return nothing.
    setDocs([]);
    return () => {
      live = false;
    };
  }, [active]);

  const path = useMemo(() => {
    if (!answering) {
      const params = new URLSearchParams({ q: q || "", limit: String(limit) });
      for (const id of docs) params.append("item_ids", id);
      return `/api/search?${params.toString()}`;
    }
    return `/api/answer?${api.askParams(q || "", limit, mode, {
      docs,
      vision: visionApplies ? vision : undefined,
      behaviour,
    })}`;
  }, [answering, q, limit, docs, mode, vision, visionApplies, behaviour]);

  const curl = [
    `curl -H "Authorization: Bearer $KB_API_KEY" \\`,
    active ? `     -H "X-Collection: ${active}" \\` : null,
    `     "${api.API}${path.replace(/q=(&|$)/, "q=your+question$1")}"`,
  ]
    .filter(Boolean)
    .join("\n");

  async function go() {
    if (!q.trim() || busy) return;
    setBusy(true);
    setError(null);
    const asked = q.trim();
    try {
      // The raw run is what this page is for, and it goes out for both
      // endpoints. The rendered answer is an extra read of the same request,
      // not a different one — so what the console shows and what the JSON says
      // can never disagree.
      const run = await api.run(path, active ?? undefined);
      setRaw(run);
      if (answering && run.ok) {
        const outcome = await api.ask(active ?? undefined, asked, limit, mode, {
          docs,
          vision: visionApplies ? vision : undefined,
          behaviour,
        });
        setTurns((t) => [...t, { q: asked, outcome, mode, vision: visionApplies && vision }]);
        setQ("");
      }
    } catch (e) {
      setError((e as Error).message);
      setRaw(null);
    } finally {
      setBusy(false);
    }
  }

  function pickBehaviour(id: string) {
    setBehaviourId(id);
    setBehaviour(BEHAVIOURS.find((b) => b.id === id)?.text ?? "");
  }

  return (
    <div className="px-6 py-6">
      <div className="mb-5">
        <h1 className="text-base font-semibold text-ink">Playground</h1>
        <p className="mt-1 max-w-2xl text-xs leading-relaxed text-subtle">
          Run the read API by hand with every setting it accepts, and see both the answer
          and the response that produced it. Requests go to{" "}
          {active ? (
            <>
              <Mono className="text-accentSoft">{active}</Mono>
            </>
          ) : (
            "the whole workspace"
          )}
          . Every control here maps to a real parameter — nothing on this page is
          decorative.
        </p>
      </div>

      <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_20rem]">
        {/* ------------------------------- ask ------------------------------ */}
        <div className="min-w-0">
          <div className="mb-3 flex flex-wrap items-center gap-2">
            <div className="flex gap-1 rounded-lg border border-edge bg-elevated p-0.5">
              {ENDPOINTS.map((e) => (
                <button
                  key={e.id}
                  onClick={() => setEndpoint(e.id)}
                  className={cx(
                    "rounded-md px-3 py-1 font-mono text-2xs transition",
                    endpoint === e.id ? "bg-accent/15 text-ink" : "text-subtle hover:text-muted"
                  )}
                >
                  {e.label}
                </button>
              ))}
            </div>
            <Chip tone="text-heat-2 border-heat-2/30 bg-heat-2/10">GET {chosen.path}</Chip>
            {answering && <Chip>{mode}</Chip>}
            {visionApplies && !vision && <Chip>vision off</Chip>}
            {docs.length > 0 && <Chip>{docs.length} document filter</Chip>}
          </div>
          <p className="mb-4 max-w-xl text-2xs leading-relaxed text-subtle">{chosen.blurb}</p>

          {/* templates */}
          <div className="mb-3 flex flex-wrap items-center gap-1.5">
            <Label className="mr-1">try</Label>
            {QUERY_TEMPLATES.map((t) => (
              <Chip key={t.label} onClick={() => setQ(t.q)} title={t.why}>
                {t.label}
              </Chip>
            ))}
          </div>

          <Card className="mb-4 p-4">
            <form
              onSubmit={(e) => {
                e.preventDefault();
                go();
              }}
              className="flex flex-wrap items-end gap-3"
            >
              <label className="min-w-[16rem] flex-1">
                <Label className="mb-1.5 block">q</Label>
                <input
                  autoFocus
                  value={q}
                  onChange={(e) => setQ(e.target.value)}
                  placeholder="refund policy"
                  className="w-full rounded-lg border border-edge bg-canvas px-3 py-2 text-sm text-ink outline-none focus:border-accent/60"
                />
              </label>
              <label className="w-20">
                <Label className="mb-1.5 block">limit</Label>
                <input
                  type="number"
                  min={1}
                  max={answering ? 20 : 100}
                  value={limit}
                  onChange={(e) => setLimit(Math.max(1, Number(e.target.value) || 1))}
                  className="w-full rounded-lg border border-edge bg-canvas px-3 py-2 font-mono text-xs text-ink outline-none focus:border-accent/60"
                />
              </label>
              <Button variant="primary" onClick={go} disabled={busy || !q.trim()}>
                {busy ? "Running…" : "Run"}
              </Button>
            </form>
          </Card>

          {error && (
            <p className="mb-4 rounded-lg border border-danger/30 bg-danger/10 px-3 py-2 font-mono text-2xs text-danger">
              {error}
            </p>
          )}

          {/* the conversation, when answering */}
          {turns.length > 0 && (
            <div className="mb-4 space-y-3">
              <div className="flex items-center gap-2">
                <Label>answers</Label>
                <Chip onClick={() => setTurns([])} title="Clear the answers on this page">
                  clear
                </Chip>
              </div>
              {turns.map((turn, n) => (
                <Card key={n} className="p-4">
                  <p className="mb-2 font-mono text-2xs text-subtle">{turn.q}</p>
                  <p
                    className={cx(
                      "whitespace-pre-wrap text-sm leading-relaxed",
                      turn.outcome.grounded ? "text-ink" : "text-muted"
                    )}
                  >
                    {turn.outcome.answer}
                  </p>
                  {!turn.outcome.grounded && (
                    <p className="mt-2 border-t border-hot/20 pt-2 font-mono text-2xs text-hot">
                      not supported by the collection — nothing below was cited
                    </p>
                  )}
                  <div className="mt-3 flex flex-wrap items-center gap-1.5 border-t border-edge pt-2.5">
                    <Chip>{turn.mode}</Chip>
                    {turn.vision && <Chip>vision</Chip>}
                    <Mono className="text-2xs text-subtle">{ms(turn.outcome.tookMs)}</Mono>
                    {turn.outcome.citations.map((c) => (
                      <Chip key={c.marker} title={c.text.slice(0, 300)}>
                        [{c.marker}] {c.title}
                      </Chip>
                    ))}
                  </div>
                </Card>
              ))}
            </div>
          )}

          <div className="mb-4">
            <Label className="mb-2 block">reproduce</Label>
            <Code code={curl} filename="curl" />
          </div>

          {raw && (
            <div>
              <div className="mb-2 flex items-center gap-2">
                <Label>response</Label>
                <Chip
                  tone={
                    raw.ok
                      ? "text-success border-success/30 bg-success/10"
                      : "text-danger border-danger/30 bg-danger/10"
                  }
                >
                  {raw.status}
                </Chip>
                <Mono className="text-2xs text-subtle">{ms(raw.ms)}</Mono>
              </div>
              <Code code={JSON.stringify(raw.body, null, 2)} filename="response.json" lang="json" />
            </div>
          )}
        </div>

        {/* ----------------------------- settings --------------------------- */}
        <aside className="space-y-4">
          <div>
            <Label className="mb-2 block">retrieval</Label>
            <div className="space-y-1.5">
              {MODES.map((m) => (
                <button
                  key={m.id}
                  type="button"
                  disabled={!answering}
                  onClick={() => setMode(m.id)}
                  className={cx(
                    "block w-full rounded-lg border px-3 py-2 text-left transition",
                    !answering
                      ? "cursor-not-allowed border-edge bg-elevated/30 opacity-50"
                      : mode === m.id
                        ? "border-accent/50 bg-accent/10"
                        : "border-edge bg-elevated hover:border-edgeStrong"
                  )}
                >
                  <span className="block font-mono text-2xs text-ink">{m.label}</span>
                  <span className="mt-0.5 block text-2xs leading-relaxed text-subtle">
                    {m.blurb}
                  </span>
                </button>
              ))}
            </div>
            {!answering && (
              <p className="mt-1.5 text-2xs text-subtle">
                Search returns passages and never writes prose, so retrieval mode and
                behaviour do not apply to it.
              </p>
            )}
          </div>

          <div>
            <Label className="mb-2 block">vision</Label>
            <Toggle
              on={vision}
              onChange={setVision}
              disabled={!visionApplies}
              label={vision ? "read pages as pictures" : "text only"}
              hint={
                visionApplies
                  ? "Off, a figure stops being readable — and the most expensive step in a walk is gone."
                  : "Only the agent opens pages. Hybrid ranks and hands over."
              }
            />
          </div>

          <div>
            <Label className="mb-2 block">how it should answer</Label>
            <div className="mb-2 flex flex-wrap gap-1.5">
              {BEHAVIOURS.map((b) => (
                <Chip
                  key={b.id}
                  title={b.blurb}
                  active={behaviourId === b.id}
                  onClick={() => answering && pickBehaviour(b.id)}
                >
                  {b.label}
                </Chip>
              ))}
            </div>
            <textarea
              value={behaviour}
              disabled={!answering}
              onChange={(e) => {
                setBehaviour(e.target.value);
                setBehaviourId("custom");
              }}
              rows={4}
              maxLength={600}
              placeholder="Write for a support engineer. Lead with the fix."
              className="w-full resize-none rounded-lg border border-edge bg-canvas px-3 py-2 text-xs leading-relaxed text-ink outline-none focus:border-accent/60 disabled:opacity-50"
            />
            <p className="mt-1.5 text-2xs leading-relaxed text-subtle">
              Style only — tone, length, formatting. It cannot change what may be said:
              citing only what was read, and saying so when the documents do not answer,
              are enforced by the server underneath whatever goes here.
            </p>
          </div>

          <div>
            <div className="mb-2 flex items-center gap-2">
              <Label>documents</Label>
              {docs.length > 0 && (
                <Chip onClick={() => setDocs([])} title="Let the store decide again">
                  clear
                </Chip>
              )}
            </div>
            <p className="mb-2 text-2xs leading-relaxed text-subtle">
              {docs.length === 0
                ? "None selected — the store decides which documents the question is about."
                : `Answering from ${docs.length} of ${library.length}. A filter is an instruction, not a hint.`}
            </p>
            <div className="max-h-64 space-y-1 overflow-y-auto rounded-lg border border-edge bg-elevated/40 p-1.5">
              {library.length === 0 && (
                <p className="px-2 py-3 text-2xs text-subtle">No documents in this collection.</p>
              )}
              {library.map((d) => {
                const on = docs.includes(d.id);
                return (
                  <button
                    key={d.id}
                    type="button"
                    onClick={() =>
                      setDocs((c) => (on ? c.filter((x) => x !== d.id) : [...c, d.id]))
                    }
                    className={cx(
                      "block w-full truncate rounded-md px-2 py-1.5 text-left font-mono text-2xs transition",
                      on ? "bg-accent/15 text-ink" : "text-subtle hover:bg-elevated hover:text-muted"
                    )}
                    title={d.locator}
                  >
                    {on ? "▸ " : "  "}
                    {d.title || d.locator}
                  </button>
                );
              })}
            </div>
          </div>
        </aside>
      </div>
    </div>
  );
}
