"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import * as api from "../api";
import { cx, ms } from "../data";
import type { Document } from "../data";
import {
  CitationChips,
  Figures,
  FocusPanel,
  Thinking,
  renderAnswer,
  useConversation,
  useRetrievalMode,
} from "../ui/conversation";
import { Chip, Code, Label, Mono } from "../ui/kit";

type Endpoint = "search" | "answer";

/** Only the two modes the server wants sent.
 *
 *  `vectorless` is still accepted and still arrives on historical traces, but
 *  as a choice it is only a worse agentic, so it is not offered. */
const MODES: { id: api.AskMode; label: string; blurb: string }[] = [
  {
    id: "agentic",
    label: "Agentic",
    blurb:
      "An agent reads its way there: tables of contents, then search for a figure no heading advertises, then the passages it lands on.",
  },
  {
    id: "hybrid",
    label: "Hybrid",
    blurb:
      "Embeddings and keyword matching fused into one ranked pass. Fast, and the same question returns the same passages every time.",
  },
];

/** Behaviour presets — STYLE only, which the server enforces rather than
 *  trusts: whatever arrives is fenced into a section governing tone, length
 *  and formatting, with the evidence rules restored underneath it. That is why
 *  a free-text box can sit next to these safely. */
const BEHAVIOURS: { id: string; label: string; text: string }[] = [
  { id: "default", label: "Default", text: "" },
  {
    id: "brief",
    label: "Brief",
    text: "Answer in at most two sentences. No preamble, no restating the question. Lead with the fact.",
  },
  {
    id: "bullets",
    label: "Bullets",
    text: "Answer as a short bulleted list. One fact per bullet, each with its citation. No introductory sentence.",
  },
  {
    id: "analyst",
    label: "Analyst",
    text: "Write for a reader who will act on this. Lead with the answer, then the supporting detail, then anything that qualifies it. Note explicitly where the documents disagree or leave a gap.",
  },
  {
    id: "plain",
    label: "Plain language",
    text: "Write for a reader who is new to this subject. Avoid jargon, and when a technical term is unavoidable explain it in the same sentence. Keep sentences short.",
  },
  {
    id: "extract",
    label: "Strict extract",
    text: "Quote the document verbatim wherever possible rather than paraphrasing. Keep the original wording, numbers and units exactly as written.",
  },
];

type Preset = {
  id: string;
  glyph: string;
  tone: string;
  label: string;
  blurb: string;
  mode: api.AskMode;
  vision: boolean;
  behaviour: string;
  starter: string;
};

/** Whole configurations, not example questions.
 *
 *  Each one is a shape of work the store is actually good at, with the settings
 *  that suit it already applied — so picking one and pressing Run shows what
 *  that combination does, which is the thing this page exists to let somebody
 *  judge. The starter question is a beginning, not a demo script. */
const PRESETS: Preset[] = [
  {
    id: "support",
    glyph: "S",
    tone: "text-accent bg-accent/15",
    label: "Support answers",
    blurb: "Short, quotable replies for someone with a customer waiting. Fast retrieval, two sentences, cited.",
    mode: "hybrid",
    vision: false,
    behaviour: BEHAVIOURS.find((b) => b.id === "brief")!.text,
    starter: "How do I ",
  },
  {
    id: "analyst",
    glyph: "A",
    tone: "text-heat-2 bg-heat-2/15",
    label: "Analyst brief",
    blurb: "The answer, then what supports it, then what qualifies it — and where the documents disagree.",
    mode: "agentic",
    vision: true,
    behaviour: BEHAVIOURS.find((b) => b.id === "analyst")!.text,
    starter: "What do the documents say about ",
  },
  {
    id: "figures",
    glyph: "F",
    tone: "text-success bg-success/15",
    label: "Figure reader",
    blurb: "For charts, tables and diagrams. The agent may open a page as a picture when the text layer cannot answer.",
    mode: "agentic",
    vision: true,
    behaviour: BEHAVIOURS.find((b) => b.id === "bullets")!.text,
    starter: "What does the diagram show about ",
  },
  {
    id: "extract",
    glyph: "E",
    tone: "text-hot bg-hot/15",
    label: "Strict extract",
    blurb: "Verbatim wording, numbers and units unchanged. For quoting a policy or a spec rather than summarising it.",
    mode: "agentic",
    vision: false,
    behaviour: BEHAVIOURS.find((b) => b.id === "extract")!.text,
    starter: "Quote exactly what it says about ",
  },
  {
    id: "onboarding",
    glyph: "P",
    tone: "text-accentSoft bg-accentSoft/15",
    label: "Plain language",
    blurb: "For a reader new to the subject. No jargon, and any unavoidable term explained in the same sentence.",
    mode: "agentic",
    vision: true,
    behaviour: BEHAVIOURS.find((b) => b.id === "plain")!.text,
    starter: "Explain ",
  },
  {
    id: "audit",
    glyph: "C",
    tone: "text-muted bg-elevated",
    label: "Coverage check",
    blurb: "Does the collection cover this at all? Should say no plainly rather than reaching for something close.",
    mode: "agentic",
    vision: false,
    behaviour: BEHAVIOURS.find((b) => b.id === "brief")!.text,
    starter: "Does anything here cover ",
  },
];

function Toggle({
  on,
  onChange,
  disabled,
}: {
  on: boolean;
  onChange: (v: boolean) => void;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={() => !disabled && onChange(!on)}
      disabled={disabled}
      aria-pressed={on}
      className={cx(
        "relative h-5 w-9 shrink-0 rounded-full transition",
        disabled ? "cursor-not-allowed bg-edgeStrong opacity-40" : on ? "bg-accent" : "bg-edgeStrong"
      )}
    >
      <span
        className={cx(
          "absolute top-0.5 h-4 w-4 rounded-full bg-canvas transition-all",
          on ? "left-[1.125rem]" : "left-0.5"
        )}
      />
    </button>
  );
}

function Row({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex items-start justify-between gap-3 py-2.5">
      <div className="min-w-0">
        <p className="text-xs text-ink">{label}</p>
        {hint && <p className="mt-0.5 text-2xs leading-relaxed text-subtle">{hint}</p>}
      </div>
      {children}
    </div>
  );
}

/** Playground — the whole store, adjustable, with the effect on screen.
 *
 *  Laid out as a console rather than a form: the conversation holds the middle,
 *  every setting lives in one rail on the right, and the composer stays at the
 *  bottom where it can be reached without scrolling. Change a setting, ask
 *  again, and the two answers sit next to each other with the settings that
 *  produced each stamped on them — which is the only way to judge a setting.
 *
 *  Every control maps to a real parameter. There is deliberately no knob the
 *  server would ignore: a setting that does nothing is worse than a missing
 *  one, because it is indistinguishable from a broken one. */
export function Playground({ active }: { active: string | null }) {
  const [endpoint, setEndpoint] = useState<Endpoint>("answer");
  // The SAME preference Query & chat uses. Opening a different page must
  // not silently change how the store answers.
  const [mode, setMode] = useRetrievalMode();
  const [vision, setVision] = useState(true);
  // Show the work as it happens, or hand over a finished answer.
  const [stream, setStream] = useState(true);
  // Off by default. It is the one tool that can spend a round on navigation
  // rather than on reading, and measured across three questions on this store
  // the agent did not reach for it once.
  const [openDoc, setOpenDoc] = useState(false);
  const [behaviourId, setBehaviourId] = useState("default");
  const [behaviour, setBehaviour] = useState("");
  const [docs, setDocs] = useState<string[]>([]);
  const [library, setLibrary] = useState<Document[]>([]);
  const [limit, setLimit] = useState(8);
  const [q, setQ] = useState("");
  const [showCode, setShowCode] = useState(false);
  // The conversation is the SAME conversation Query & chat runs — same stream,
  // same turns, same live trail, same rendering of an answer and its evidence.
  // The Playground only puts settings around it. See ui/conversation.tsx.
  const {
    turns,
    live,
    pending,
    busy,
    error,
    ask,
    clear,
    setError,
  } = useConversation(active ?? undefined, "playground");
  // Search returns passages rather than prose, so it is not a turn in the
  // conversation. Its response is shown on its own.
  const [searchRun, setSearchRun] = useState<{ q: string; raw: api.RawRun } | null>(null);
  const [openJson, setOpenJson] = useState<number | null>(null);
  // The passage being examined. One at a time and page-level rather than
  // per-turn: it is a reading pane, and two open at once is two things to read.
  const [focus, setFocus] = useState<api.Citation | null>(null);
  const foot = useRef<HTMLDivElement | null>(null);

  const answering = endpoint === "answer";
  // Vision is the agent's tool. Hybrid ranks and hands over — it never opens a
  // page — so the control is disabled rather than quietly ignored.
  const visionApplies = answering && mode === "agentic";

  useEffect(() => {
    let alive = true;
    api
      .documents(active ?? undefined, 100)
      .then((d) => alive && setLibrary(d))
      .catch(() => alive && setLibrary([]));
    // The filter names document ids, and those belong to the collection that
    // was selected. Cleared rather than remapped: a filter pointing at another
    // collection's documents would silently return nothing.
    setDocs([]);
    return () => {
      alive = false;
    };
  }, [active]);

  useEffect(() => {
    foot.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [turns.length, busy, live]);

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
      openDocument: openDoc,
    })}`;
  }, [answering, q, limit, docs, mode, vision, visionApplies, behaviour, openDoc]);

  const curl = [
    `curl -H "Authorization: Bearer $KB_API_KEY" \\`,
    active ? `     -H "X-Collection: ${active}" \\` : null,
    `     "${api.API}${path.replace(/q=(&|$)/, "q=your+question$1")}"`,
  ]
    .filter(Boolean)
    .join("\n");

  function applyPreset(p: Preset) {
    setEndpoint("answer");
    setMode(p.mode);
    setVision(p.vision);
    setBehaviour(p.behaviour);
    setBehaviourId(BEHAVIOURS.find((b) => b.text === p.behaviour)?.id ?? "custom");
    setQ(p.starter);
  }

  async function go() {
    if (!q.trim() || busy) return;
    const asked = q.trim();
    setError(null);
    if (!answering) {
      // Search is sub-second and its JSON is the point, so it goes out as a
      // plain request rather than through the conversation.
      try {
        setSearchRun({ q: asked, raw: await api.run(path, active ?? undefined) });
        setQ("");
      } catch (e) {
        setError((e as Error).message);
      }
      return;
    }
    setSearchRun(null);
    setQ("");
    await ask(asked, {
      limit,
      mode,
      stream,
      options: {
        docs,
        vision: visionApplies ? vision : undefined,
        behaviour,
        openDocument: openDoc,
      },
    });
  }

  return (
    <div className="flex h-full min-h-0">
      {/* ------------------------------ console ------------------------------ */}
      <div className="relative flex min-w-0 flex-1 flex-col">
        <div className="flex items-center justify-between gap-3 px-6 pb-3 pt-5">
          <div className="flex items-center gap-2">
            <h1 className="text-base font-semibold text-ink">Playground</h1>
            <Chip>{active ?? "workspace-wide"}</Chip>
          </div>
          <div className="flex items-center gap-2">
            <Chip active={showCode} onClick={() => setShowCode((s) => !s)} title="The request as curl">
              &lt;&gt; Get code
            </Chip>
            {(turns.length > 0 || searchRun) && (
              <Chip onClick={clear} title="Clear this session">
                clear
              </Chip>
            )}
          </div>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto px-6">
          {showCode && (
            <div className="mb-4">
              <Code code={curl} filename="curl" />
            </div>
          )}

          {turns.length === 0 && !searchRun && !busy ? (
            <div className="mx-auto max-w-3xl py-6">
              <h2 className="text-2xl font-semibold text-ink">Ask your collection</h2>
              <p className="mt-1.5 max-w-xl text-xs leading-relaxed text-subtle">
                Start from a configuration, or set your own in the rail on the right. Every
                control there is a real parameter — change one, ask the same question again,
                and the two answers sit side by side with the settings that produced them.
              </p>
              <div className="mt-6 grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
                {PRESETS.map((p) => (
                  <button
                    key={p.id}
                    onClick={() => applyPreset(p)}
                    className="group rounded-xl border border-edge bg-elevated/40 p-4 text-left transition hover:border-edgeStrong hover:bg-elevated"
                  >
                    <span
                      className={cx(
                        "flex h-7 w-7 items-center justify-center rounded-lg font-mono text-2xs",
                        p.tone
                      )}
                    >
                      {p.glyph}
                    </span>
                    <p className="mt-3 text-sm text-ink">{p.label}</p>
                    <p className="mt-1 text-2xs leading-relaxed text-subtle">{p.blurb}</p>
                    <p className="mt-2.5 font-mono text-2xs text-subtle opacity-0 transition group-hover:opacity-100">
                      {p.mode}
                      {p.vision ? " · vision" : ""}
                    </p>
                  </button>
                ))}
              </div>
            </div>
          ) : (
            <div className="mx-auto max-w-3xl space-y-4 py-2">
              {turns.map((turn, n) => (
                <div key={n} className="animate-rise">
                  <div className="mb-3 flex items-start gap-2.5">
                    <span className="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-md bg-accent/20 font-mono text-2xs text-accentSoft">
                      ?
                    </span>
                    <p className="text-sm text-ink">{turn.question}</p>
                  </div>

                  <div className="pl-8">
                    <div
                      className={cx(
                        "rounded-xl border p-4",
                        turn.grounded ? "border-edge bg-elevated/50" : "border-hot/30 bg-hot/5"
                      )}
                    >
                      <p className="whitespace-pre-wrap text-sm leading-relaxed text-ink">
                        {renderAnswer(turn.answer, turn.citations, setFocus)}
                      </p>

                      {!turn.grounded && (
                        <p className="mt-2.5 border-t border-hot/20 pt-2 font-mono text-2xs text-hot">
                          not supported by the collection — nothing below was cited
                        </p>
                      )}
                      {turn.degraded && (
                        <p className="mt-2 font-mono text-2xs text-hot">{turn.degraded}</p>
                      )}

                      <CitationChips citations={turn.citations} onOpen={setFocus} />
                      <Figures citations={turn.citations} question={turn.question} />
                    </div>

                    {/* What the Playground adds, and only this: the settings
                        that produced the answer, so two runs can be compared. */}
                    <div className="mt-2 flex flex-wrap items-center gap-1.5">
                      <Chip>{turn.mode}</Chip>
                      {turn.settings?.vision && <Chip>vision</Chip>}
                      {turn.settings?.behaviour ? <Chip>styled</Chip> : null}
                      {(turn.settings?.docs ?? 0) > 0 && <Chip>{turn.settings?.docs} docs</Chip>}
                      <Mono className="text-2xs text-subtle">{ms(turn.tookMs)}</Mono>
                      <Chip
                        onClick={() => setOpenJson(openJson === n ? null : n)}
                        active={openJson === n}
                      >
                        json
                      </Chip>
                    </div>
                    {openJson === n && (
                      <div className="mt-2">
                        <Code
                          code={JSON.stringify(turn.raw, null, 2)}
                          filename="response.json"
                          lang="json"
                        />
                      </div>
                    )}
                  </div>
                </div>
              ))}

              {searchRun && (
                <div>
                  <p className="mb-2 text-right text-sm text-muted">{searchRun.q}</p>
                  <Code
                    code={JSON.stringify(searchRun.raw.body, null, 2)}
                    filename={`${searchRun.raw.status} · response.json`}
                    lang="json"
                  />
                </div>
              )}

              {busy && <Thinking live={live} mode={mode} pending={pending} />}
              <div ref={foot} />
            </div>
          )}
        </div>

        {focus && <FocusPanel focus={focus} onClose={() => setFocus(null)} />}

        {/* composer */}
        <div className="border-t border-edge px-6 py-4">
          <div className="mx-auto max-w-3xl">
            {error && (
              <p className="mb-2 rounded-lg border border-danger/30 bg-danger/10 px-3 py-2 font-mono text-2xs text-danger">
                {error}
              </p>
            )}
            <form
              onSubmit={(e) => {
                e.preventDefault();
                go();
              }}
              className="rounded-2xl border border-edge bg-elevated/60 p-2.5 focus-within:border-accent/50"
            >
              <input
                value={q}
                onChange={(e) => setQ(e.target.value)}
                placeholder="Ask anything about this collection…"
                className="w-full bg-transparent px-2 py-1.5 text-sm text-ink outline-none placeholder:text-subtle"
              />
              <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
                <Chip active={answering} onClick={() => setEndpoint("answer")}>
                  Answer
                </Chip>
                <Chip active={!answering} onClick={() => setEndpoint("search")}>
                  Search
                </Chip>
                <span className="mx-1 h-4 w-px bg-edge" />
                {answering && <Chip>{mode}</Chip>}
                {visionApplies && <Chip>{vision ? "vision on" : "vision off"}</Chip>}
                {answering && !stream && <Chip>completion</Chip>}
                {visionApplies && openDoc && <Chip>open_document</Chip>}
                {answering && behaviourId !== "default" && <Chip>{behaviourId}</Chip>}
                {docs.length > 0 && <Chip>{docs.length} documents</Chip>}
                <button
                  type="submit"
                  disabled={busy || !q.trim()}
                  className={cx(
                    "ml-auto rounded-lg px-4 py-1.5 font-mono text-2xs transition",
                    busy || !q.trim()
                      ? "cursor-not-allowed bg-elevated text-subtle"
                      : "bg-accent/20 text-ink hover:bg-accent/30"
                  )}
                >
                  {busy ? "Running…" : "Run ↵"}
                </button>
              </div>
            </form>
          </div>
        </div>
      </div>

      {/* ---------------------------- run settings ---------------------------- */}
      {/* Hidden on a narrow viewport rather than squeezed. At 704px the rail
          and the console were both unusable — the answer wrapped to four words
          a line while the settings were still cut off. A panel that fits
          nothing is worse than a panel you scroll to. */}
      <aside className="hidden w-80 shrink-0 overflow-y-auto border-l border-edge bg-elevated/20 px-4 py-5 lg:block">
        <p className="mb-3 text-xs font-semibold text-ink">Run settings</p>

        <div className="rounded-xl border border-edge bg-elevated/50 p-3">
          <p className="text-xs text-ink">{answering ? "Answer" : "Search"}</p>
          <p className="mt-1 text-2xs leading-relaxed text-subtle">
            {answering
              ? "Retrieval plus a written answer built only from what was retrieved, with citations."
              : "Ranked passages only — the contract other software builds on. No generated prose, so nothing below applies except the document filter and limit."}
          </p>
        </div>

        <div className="mt-4 border-t border-edge pt-1">
          <Label className="mb-2 mt-2 block">retrieval</Label>
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
                    ? "cursor-not-allowed border-edge opacity-40"
                    : mode === m.id
                      ? "border-accent/50 bg-accent/10"
                      : "border-edge bg-elevated/50 hover:border-edgeStrong"
                )}
              >
                <span className="block text-xs text-ink">{m.label}</span>
                <span className="mt-0.5 block text-2xs leading-relaxed text-subtle">{m.blurb}</span>
              </button>
            ))}
          </div>
        </div>

        <div className="mt-3 border-t border-edge">
          <Row
            label="Vision"
            hint={
              visionApplies
                ? "Read a page as a picture when the text layer cannot answer. The most expensive step in a walk."
                : "Only the agent opens pages. Hybrid ranks and hands over."
            }
          >
            <Toggle on={vision} onChange={setVision} disabled={!visionApplies} />
          </Row>
        </div>

        <div className="border-t border-edge">
          <Row
            label="Stream"
            hint={
              stream
                ? "Show the steps, the thinking and the answer as they arrive."
                : "Nothing appears until the answer is whole."
            }
          >
            <Toggle on={stream} onChange={setStream} disabled={!answering} />
          </Row>
        </div>

        <div className="border-t border-edge">
          <Row
            label="Open document"
            hint={
              visionApplies
                ? "Let the agent open a whole outline when the catalogue is shortened. Off, it navigates from the catalogue and the hint."
                : "Only the agent navigates. Hybrid ranks and hands over."
            }
          >
            <Toggle on={openDoc} onChange={setOpenDoc} disabled={!visionApplies} />
          </Row>
        </div>

        <div className="border-t border-edge py-3">
          <Label className="mb-2 block">agent behaviour</Label>
          <div className="mb-2 flex flex-wrap gap-1.5">
            {BEHAVIOURS.map((b) => (
              <Chip
                key={b.id}
                active={behaviourId === b.id}
                onClick={() => {
                  if (!answering) return;
                  setBehaviourId(b.id);
                  setBehaviour(b.text);
                }}
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
            rows={5}
            maxLength={600}
            placeholder="How should it write? Tone, length, formatting."
            className="w-full resize-none rounded-lg border border-edge bg-canvas px-3 py-2 text-xs leading-relaxed text-ink outline-none focus:border-accent/60 disabled:opacity-40"
          />
          <p className="mt-1.5 text-2xs leading-relaxed text-subtle">
            Style only. It cannot change what may be said — citing only what was read, and
            saying so when the documents do not answer, hold underneath whatever goes here.
          </p>
        </div>

        <div className="border-t border-edge py-3">
          <Row label="Passages" hint="How much evidence reaches the answer.">
            <input
              type="number"
              min={1}
              max={answering ? 20 : 100}
              value={limit}
              onChange={(e) => setLimit(Math.max(1, Number(e.target.value) || 1))}
              className="w-16 rounded-lg border border-edge bg-canvas px-2 py-1 text-right font-mono text-xs text-ink outline-none focus:border-accent/60"
            />
          </Row>
        </div>

        <div className="border-t border-edge py-3">
          <div className="mb-1.5 flex items-center justify-between">
            <Label>documents</Label>
            {docs.length > 0 && <Chip onClick={() => setDocs([])}>clear</Chip>}
          </div>
          <p className="mb-2 text-2xs leading-relaxed text-subtle">
            {docs.length === 0
              ? "None selected — the store decides which documents the question is about."
              : `${docs.length} of ${library.length}. A filter is an instruction, not a hint.`}
          </p>
          <div className="max-h-56 space-y-0.5 overflow-y-auto rounded-lg border border-edge bg-canvas/60 p-1.5">
            {library.length === 0 && (
              <p className="px-2 py-3 text-2xs text-subtle">No documents in this collection.</p>
            )}
            {library.map((d) => {
              const on = docs.includes(d.id);
              return (
                <button
                  key={d.id}
                  type="button"
                  onClick={() => setDocs((c) => (on ? c.filter((x) => x !== d.id) : [...c, d.id]))}
                  title={d.locator}
                  className={cx(
                    "block w-full truncate rounded-md px-2 py-1.5 text-left font-mono text-2xs transition",
                    on ? "bg-accent/15 text-ink" : "text-subtle hover:bg-elevated hover:text-muted"
                  )}
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
  );
}
