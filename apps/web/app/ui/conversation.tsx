"use client";

import { useCallback, useEffect, useState } from "react";
import * as api from "../api";
import type { Point } from "../data";
import { cx } from "../data";
import { Label, Mono } from "./kit";

/** The conversation, shared.
 *
 *  Query & chat and the Playground are the same thing: ask the store, get an
 *  answer built only from what it retrieved, with the passages underneath. The
 *  Playground adds settings around that; it does not change any of it.
 *
 *  Two implementations of one conversation is how they drift — and drift here
 *  is not cosmetic. The Playground had already grown a second, worse rendering
 *  of citations (chips with the passage hidden in a tooltip) and a second,
 *  cruder progress line, while the real ones sat in query.tsx. Whichever page
 *  someone happens to open should not decide whether they can check an answer.
 *
 *  So the engine lives here: the live-stream reducer, the marker rendering, the
 *  evidence block, and the hook that runs a turn. The two views keep their own
 *  layouts and share everything underneath. */

// --------------------------------- types ---------------------------------

export type Turn = {
  question: string;
  mode: api.AskMode;
  answer: string;
  citations: api.Citation[];
  grounded: boolean;
  matches: Point[];
  tookMs: number;
  traceId: string;
  degraded: string | null;
  /** What the run was configured with, when the caller cares to say. The
   *  Playground stamps every answer so two settings can be compared; Query has
   *  no settings and leaves it undefined. */
  settings?: { vision?: boolean; behaviour?: string; docs?: number };
  /** The API's own response body, kept when the caller wants to show it. */
  raw?: unknown;
};

export type Activity = { kind: "status" | "tool"; text: string; ok?: boolean };

/** Everything the current in-flight turn has reported so far. */
export type Live = { activity: Activity[]; reasoning: string; draft: string };

export const EMPTY_LIVE: Live = { activity: [], reasoning: "", draft: "" };

// ------------------------------ live stream ------------------------------

export function describeCall(event: Extract<api.StreamEvent, { type: "tool_call" }>): string {
  if (event.tool === "read_section") return `Reading section ${event.args.section ?? ""}`.trim();
  if (event.tool === "hybrid_search") return `Searching “${event.args.query ?? ""}”`;
  if (event.tool === "write_answer") return `Writing from ${event.args.passages ?? 0} passages`;
  if (event.tool === "look_at_page") return `Looking at page ${event.args.page ?? ""}`.trim();
  if (event.tool === "open_document") return "Opening the document";
  return `Calling ${event.tool}`;
}

export function describeResult(
  event: Extract<api.StreamEvent, { type: "tool_result" }>
): string {
  if (event.tool === "read_section") {
    if (!event.ok) return event.detail || "section not found";
    const where = event.title ? `${event.title} › ${event.heading}` : event.heading || "";
    return `${where} [${event.marker}]`;
  }
  if (event.tool === "hybrid_search") {
    if (!event.ok) return "nothing new";
    const heads = event.headings?.length ? `: ${event.headings.join(", ")}` : "";
    return `found ${event.count} passage${event.count === 1 ? "" : "s"}${heads}`;
  }
  return "";
}

/** Fold one streamed event into the running live state. Pure, so the reducer
 *  reads as the event schema does and the render stays a plain projection. */
export function reduceLive(cur: Live, event: api.StreamEvent): Live {
  switch (event.type) {
    case "start":
      return {
        ...cur,
        activity: [
          ...cur.activity,
          {
            kind: "status",
            text:
              event.mode === "hybrid"
                ? "Searching passages…"
                : `Reading ${event.documents ?? 0} document${event.documents === 1 ? "" : "s"} · ${event.sections ?? 0} sections`,
          },
        ],
      };
    case "retrieved":
      return {
        ...cur,
        activity: [
          ...cur.activity,
          {
            kind: "status",
            text: `${event.documents} document${event.documents === 1 ? "" : "s"}, ${event.passages} passage${event.passages === 1 ? "" : "s"}`,
          },
        ],
      };
    case "thinking":
      return { ...cur, reasoning: cur.reasoning + event.delta };
    case "tool_call":
      return { ...cur, activity: [...cur.activity, { kind: "tool", text: describeCall(event) }] };
    case "tool_result":
      return {
        ...cur,
        activity: [...cur.activity, { kind: "tool", ok: event.ok, text: describeResult(event) }],
      };
    case "token":
      return { ...cur, draft: cur.draft + event.delta };
    case "answer":
      return { ...cur, draft: event.text };
    case "degraded":
      return { ...cur, activity: [...cur.activity, { kind: "status", text: event.reason }] };
    default:
      return cur;
  }
}

// ------------------------------- rendering -------------------------------

/** Turn the [n] markers in an answer into buttons that open the passage.
 *
 *  Split rather than replaced into HTML: the answer is model output, and
 *  putting model output through anything that interprets markup is how a store
 *  starts rendering whatever a document happened to contain.
 *
 *  A marker resolving to nothing is REMOVED rather than shown — it points at no
 *  passage, so leaving it would be a reference the reader cannot follow, which
 *  is worse than none because it looks checkable. */
export function renderAnswer(
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
        <span key={`${match.index}-x`} />
      )
    );
    last = match.index + match[0].length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

/** The citations under an answer, as the pressable row Query has always had. */
export function CitationChips({
  citations,
  onOpen,
}: {
  citations: api.Citation[];
  onOpen: (c: api.Citation) => void;
}) {
  if (citations.length === 0) return null;
  return (
    <div className="mt-3 flex flex-wrap items-center gap-1.5 border-t border-edge pt-2.5">
      <Label>from</Label>
      {citations.map((c) => (
        <button
          key={c.chunk_id}
          onClick={() => onOpen(c)}
          className="max-w-[15rem] truncate rounded-md border border-accent/30 bg-accent/10 px-2 py-0.5 font-mono text-2xs text-accentSoft transition hover:border-accent/60"
        >
          [{c.marker}] {c.heading || c.title}
        </button>
      ))}
    </div>
  );
}

/** The passage a citation points at, and the page it was read off.
 *
 *  The page picture is the whole argument for transcribing a table with
 *  vision: the transcription is a CLAIM, and the page is the thing that claim
 *  can be checked against. Without it a transcription is just a more confident
 *  guess — which is why this panel belongs on any page that shows an answer,
 *  not only on the one it happened to be written for. */
export function FocusPanel({
  focus,
  onClose,
}: {
  focus: api.Citation;
  onClose: () => void;
}) {
  return (
    <div className="absolute inset-x-0 bottom-0 z-10 border-t border-edgeStrong bg-raised/97 backdrop-blur">
      <div className="mx-auto max-w-3xl px-6 py-4">
        <div className="mb-2 flex items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="truncate text-xs font-medium text-ink">{focus.title}</div>
            {focus.heading && (
              <div className="truncate font-mono text-2xs text-accentSoft">{focus.heading}</div>
            )}
          </div>
          <button
            onClick={onClose}
            className="shrink-0 rounded-md border border-edge px-2 py-0.5 font-mono text-2xs text-subtle transition hover:text-ink"
          >
            close
          </button>
        </div>
        <div className="flex gap-4">
          <p className="max-h-56 min-w-0 flex-1 overflow-y-auto whitespace-pre-wrap text-xs leading-relaxed text-muted">
            {focus.text}
          </p>

          {focus.page ? (
            <figure className="hidden w-44 shrink-0 sm:block">
              <a
                href={api.pageImageUrl(focus.item_id, focus.page)}
                target="_blank"
                rel="noreferrer"
                className="relative block overflow-hidden rounded-md border border-warn/30 transition hover:border-warn/60"
              >
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img
                  src={api.pageImageUrl(focus.item_id, focus.page)}
                  alt={`Page ${focus.page} of ${focus.title}`}
                  // The API is a different origin and the session is a cookie:
                  // without this the browser sends no credentials and the page
                  // comes back 401.
                  crossOrigin="use-credentials"
                  className="max-h-56 w-full bg-canvas object-cover object-top"
                />
                {/* Where on the page it was read from. A thumbnail says
                    "somewhere in here"; the box says "this row". Percentages,
                    so the same numbers hold at any drawn size. Outline and tint
                    only — dimming the rest behind each box turned the page
                    black once there were four of them. */}
                {(focus.regions || []).map((box, index) => (
                  <span
                    key={index}
                    title={box.label || "read from here"}
                    className="pointer-events-none absolute rounded-[2px] border-2 border-warn bg-warn/25 ring-1 ring-canvas/70"
                    style={{
                      left: `${box.x}%`,
                      top: `${box.y}%`,
                      width: `${box.w}%`,
                      height: `${box.h}%`,
                    }}
                  />
                ))}
              </a>
              <figcaption className="mt-1 text-center font-mono text-2xs text-warn">
                read from page {focus.page}
                {(focus.regions?.length ?? 0) > 0 && (
                  <span className="block text-subtle">
                    {focus.regions!.length} highlighted{" "}
                    {focus.regions!.length === 1 ? "area" : "areas"}
                  </span>
                )}
              </figcaption>
            </figure>
          ) : null}
        </div>
      </div>
    </div>
  );
}

// --------------------------------- figures ---------------------------------
//
// A page picture answers "where did this come from?". It does not answer "show
// me the architecture diagram" — the diagram arrives three centimetres tall in
// the corner of a sheet of paper, with the rest of the page around it.
//
// So the pictures INSIDE the cited pages are cropped out and shown under the
// answer. Two sources, one presentation, because to a reader they are the same
// thing:
//
//   * what the store found on the page (deterministic, from the PDF's own
//     object tree — no model, no tokens, no time on the answer path), and
//   * where a vision read said it was looking, when the walk used one.
//
// Nothing here is on the answer path. The strip mounts with the finished turn
// and fetches per cited page; a store with no diagrams in it is exactly as fast
// as it was before any of this existed.

export type Shown = api.Figure & {
  item_id: string;
  page: number;
  title: string;
  /** "page" — found in the document. "read" — where a vision read looked. */
  source: "page" | "read";
};

/** Whether a question is ASKING about pictures.
 *
 *  Used only to decide how prominent the strip starts, never whether figures
 *  are fetched or shown. Getting this wrong therefore costs a click, not a
 *  diagram — which is the only reason a word list is allowed to make the call.
 */
const _ABOUT_PICTURES =
  /\b(diagram|diagrams|flow ?charts?|flow ?diagrams?|architectur\w*|chart|charts|graph|graphs|figure|figures|image|images|picture|pictures|photo\w*|screenshots?|drawing|drawings|illustrat\w*|visuali[sz]\w*|schematic|layout|wireframe|infographic|plot|map)\b/i;

export function asksAboutPictures(question: string): boolean {
  return _ABOUT_PICTURES.test(question);
}

/** Every picture on every page this answer cited, in citation order.
 *
 *  Fetched once per distinct page, not once per citation — three citations off
 *  page 6 is one request. Failures are silent by design: a page whose figures
 *  cannot be worked out still has its text, and an error banner over a picture
 *  nobody asked for would be the tail wagging the dog. */
export function useFigures(citations: api.Citation[]): Shown[] {
  const [found, setFound] = useState<Shown[]>([]);

  // The identity of the request, not the array: citations arrive as a new array
  // on every render, and depending on the array itself would refetch forever.
  const wanted = citations
    .filter((c) => c.page != null)
    .map((c) => `${c.item_id}:${c.page}`)
    .filter((key, index, all) => all.indexOf(key) === index)
    .join("|");

  useEffect(() => {
    let live = true;
    if (!wanted) {
      setFound([]);
      return;
    }
    const titles = new Map(citations.map((c) => [`${c.item_id}:${c.page}`, c.title]));

    (async () => {
      const pages = wanted.split("|");
      const results = await Promise.all(
        pages.map(async (key) => {
          const [itemId, page] = [key.slice(0, key.lastIndexOf(":")), Number(key.split(":").pop())];
          try {
            const figures = await api.pageFigures(itemId, page);
            return figures.map((f) => ({
              ...f,
              item_id: itemId,
              page,
              title: titles.get(key) || "",
              source: "page" as const,
            }));
          } catch {
            return [];
          }
        })
      );
      if (live) setFound(results.flat());
    })();

    return () => {
      live = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [wanted]);

  // What a vision read pointed at, kept alongside. A region is already a box on
  // a page in the same units, so it crops identically — and when the walk
  // looked at a page, its own idea of where it was looking is better evidence
  // than anything inferred from the file.
  const regions: Shown[] = citations.flatMap((c) =>
    c.page == null
      ? []
      : (c.regions || []).map((r) => ({
          ...r,
          kind: "read",
          caption: r.label || "",
          item_id: c.item_id,
          page: c.page as number,
          title: c.title,
          source: "read" as const,
        }))
  );

  // A read region sitting on top of a found figure is the same picture twice.
  const kept = found.filter(
    (f) =>
      !regions.some(
        (r) =>
          r.item_id === f.item_id &&
          r.page === f.page &&
          Math.abs(r.x - f.x) < 12 &&
          Math.abs(r.y - f.y) < 12
      )
  );
  return [...regions, ...kept];
}

/** One figure, cropped out of its page picture with CSS.
 *
 *  Cropped rather than cut: the page PNG is one request the browser already has
 *  cached from the citation panel, and every figure on it is a transform of
 *  that same image. Producing crops server-side would mean a new blob, a new
 *  route and a new cache for something the browser can do for nothing.
 *
 *  The page's own proportions are measured rather than assumed — letter, A4 and
 *  a landscape slide are all different, and a guess would show every diagram
 *  subtly stretched. */
export function FigureCrop({ figure, className }: { figure: Shown; className?: string }) {
  const [ratio, setRatio] = useState<number | null>(null);
  const src = api.pageImageUrl(figure.item_id, figure.page);

  useEffect(() => {
    let live = true;
    const probe = new Image();
    probe.crossOrigin = "use-credentials";
    probe.onload = () => {
      if (live && probe.naturalHeight) setRatio(probe.naturalWidth / probe.naturalHeight);
    };
    probe.src = src;
    return () => {
      live = false;
    };
  }, [src]);

  // Until the page has been measured, letter paper — close enough that nothing
  // visibly jumps when the real number arrives.
  const pageRatio = ratio ?? 8.5 / 11;
  const aspect = (figure.w * pageRatio) / figure.h;

  return (
    <div
      className={cx("relative overflow-hidden bg-white", className)}
      style={{ aspectRatio: `${aspect}` }}
    >
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img
        src={src}
        alt={figure.caption || `Figure on page ${figure.page} of ${figure.title}`}
        // Cross-origin API, cookie session: without this the browser sends no
        // credentials and every figure comes back 401.
        crossOrigin="use-credentials"
        className="absolute max-w-none"
        style={{
          width: `${(100 / figure.w) * 100}%`,
          left: `${-(figure.x / figure.w) * 100}%`,
          top: `${-(figure.y / figure.h) * 100}%`,
          height: `${(100 / figure.h) * 100}%`,
        }}
      />
    </div>
  );
}

/** The pictures behind an answer, as a row of crops under it.
 *
 *  Opens onto the figure at full size with the page it was cut from beside it,
 *  because a crop is a claim about where something is and the page is what
 *  makes that checkable — the same argument the citation panel makes about
 *  transcribed text. */
export function Figures({ citations, question }: { citations: api.Citation[]; question: string }) {
  const figures = useFigures(citations);
  const [open, setOpen] = useState<number | null>(null);

  // Asked about pictures? Then the first one is already open. Otherwise the
  // strip sits quietly under the answer and waits to be clicked.
  const asked = asksAboutPictures(question);
  useEffect(() => {
    setOpen(asked && figures.length > 0 ? 0 : null);
  }, [asked, figures.length]);

  if (figures.length === 0) return null;
  const showing = open != null ? figures[open] : null;

  return (
    <div className="mt-3 border-t border-edge pt-3">
      <Label>
        {figures.length === 1 ? "figure" : "figures"} · {figures.length}
      </Label>

      <div className="mt-2 flex flex-wrap gap-2">
        {figures.map((figure, index) => (
          <button
            key={`${figure.item_id}-${figure.page}-${figure.x}-${figure.y}-${index}`}
            onClick={() => setOpen(open === index ? null : index)}
            title={figure.caption || `page ${figure.page} · ${figure.title}`}
            className={cx(
              "group w-28 shrink-0 overflow-hidden rounded-md border text-left transition",
              open === index
                ? "border-accent"
                : "border-edge hover:border-accent/50"
            )}
          >
            <FigureCrop figure={figure} className="w-full" />
            <span className="block truncate border-t border-edge bg-canvas/60 px-1.5 py-1 font-mono text-[0.6rem] text-subtle">
              {figure.source === "read" ? "read" : `p${figure.page}`}
              {figure.caption ? ` · ${figure.caption}` : ""}
            </span>
          </button>
        ))}
      </div>

      {showing && (
        <figure className="mt-2.5 rounded-lg border border-edge bg-canvas/60 p-2.5">
          <FigureCrop figure={showing} className="mx-auto max-h-[26rem] w-full rounded" />
          <figcaption className="mt-2 flex items-baseline justify-between gap-3 text-2xs">
            <span className="min-w-0 flex-1 text-muted">
              {showing.caption || (
                <span className="text-subtle">
                  {showing.source === "read"
                    ? "where this answer was read from"
                    : `${showing.kind === "image" ? "picture" : "drawing"} on this page`}
                </span>
              )}
            </span>
            <a
              href={api.pageImageUrl(showing.item_id, showing.page)}
              target="_blank"
              rel="noreferrer"
              className="shrink-0 font-mono text-2xs text-accentSoft hover:underline"
            >
              page {showing.page} ↗
            </a>
          </figcaption>
        </figure>
      )}
    </div>
  );
}

/** The evidence, in full, under the answer it produced.
 *
 *  Chips with the passage in a tooltip were not showing a citation, they were
 *  hiding one. The claim this store makes is that an answer can be CHECKED, and
 *  a claim you must hover to verify is not one anybody checks. */
export function Evidence({
  citations,
  open,
  onToggle,
}: {
  citations: api.Citation[];
  open: number | null;
  onToggle: (marker: number) => void;
}) {
  if (citations.length === 0) return null;
  return (
    <div className="mt-3 space-y-1.5 border-t border-edge pt-3">
      <Label>evidence · {citations.length}</Label>
      {citations.map((c) => (
        <div key={c.marker} className="rounded-lg border border-edge bg-canvas/60">
          <button
            type="button"
            onClick={() => onToggle(c.marker)}
            className="flex w-full items-baseline gap-2 px-2.5 py-2 text-left"
          >
            <span className="rounded bg-accent/20 px-1 font-mono text-[0.6rem] text-accentSoft">
              {c.marker}
            </span>
            <span className="min-w-0 flex-1 truncate text-2xs text-ink">
              {c.title}
              {c.heading ? <span className="text-subtle"> › {c.heading}</span> : null}
            </span>
            {c.page != null && <Mono className="text-2xs text-subtle">p{c.page}</Mono>}
            <Mono className="text-2xs text-subtle">{c.score.toFixed(3)}</Mono>
          </button>
          <p
            className={cx(
              "whitespace-pre-wrap border-t border-edge px-2.5 py-2 text-2xs leading-relaxed text-muted",
              open === c.marker ? "" : "line-clamp-2"
            )}
          >
            {c.text}
          </p>
        </div>
      ))}
    </div>
  );
}

/** The turn in flight: what it is doing, what it is thinking, what it has
 *  written so far.
 *
 *  Lifted verbatim out of query.tsx rather than reinvented. The Playground had
 *  a flat list of grey lines and no streamed draft at all, so an agentic walk
 *  showed the question and then a blank pane for half a minute — while the very
 *  same events were already being folded into a live trail two files away.
 *
 *  Every part of it is the point: the spinner says it is alive, the ordered
 *  list says WHERE it is, the reasoning says what it is weighing, and the draft
 *  is the answer arriving word by word rather than all at once at the end. */
export function Thinking({
  live,
  mode,
  pending,
}: {
  live: Live | null;
  mode: api.AskMode;
  pending?: string | null;
}) {
  return (
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
  );
}

// --------------------------------- engine ---------------------------------

// How many turns a saved conversation keeps. Enough to be a history,
// bounded because every turn carries its passages and the quota is small.
const HISTORY_LIMIT = 40;

/** The retrieval preference, remembered and shared by every page that asks.
 *
 *  It is ONE preference. A standing choice about how you want the store to
 *  work does not become a different choice because you opened a different
 *  page — and when the two pages disagreed, the disagreement was invisible and
 *  looked like a bug in the slower one. Query remembered your choice while the
 *  Playground always started on agentic, so a store set to hybrid answered in
 *  about a second on one page and took the better part of a minute on the
 *  other, for the same question and the same code underneath. That reads as
 *  the Playground running something older. It was running what it was told.
 */
export function useRetrievalMode(): [api.AskMode, (next: api.AskMode) => void] {
  const [mode, setMode] = useState<api.AskMode>("agentic");

  useEffect(() => {
    const saved = window.localStorage.getItem("retrieval-mode");
    if (saved === "hybrid" || saved === "agentic") setMode(saved);
    // vectorless was retired as a choice; an old preference carries over to
    // agentic, which does the same catalogue reasoning and reaches the rest.
    else if (saved === "vectorless") setMode("agentic");
  }, []);

  const choose = useCallback((next: api.AskMode) => {
    setMode(next);
    try {
      window.localStorage.setItem("retrieval-mode", next);
    } catch {
      /* private mode — the choice still holds for this session */
    }
  }, []);

  return [mode, choose];
}

export type AskSettings = {
  limit?: number;
  mode?: api.AskMode;
  options?: api.AskOptions;
  /** Show the work as it happens. Off, nothing appears until the answer is
   *  whole — the difference between watching it think and being handed a
   *  finished reply.
   *
   *  Both go over the same SSE transport, deliberately. The blocking route
   *  exists and is correct, but Next abandons a proxied rewrite at ~30s and
   *  returns a bare 500 with nothing in the API log, and agentic answers run
   *  well past that. So "completion" here means the events are consumed and
   *  not shown, rather than a request that would sometimes simply fail. */
  stream?: boolean;
};

/** One conversation against one collection.
 *
 *  Streamed, always. The blocking route dies at the Next proxy, which gives up
 *  on a rewrite at ~30s and returns a bare 500 with nothing in the API log;
 *  agentic answers run 20-35s, astride that ceiling. A stream is bytes from the
 *  first step onward, so no idle timeout can fire. */
export function useConversation(collectionId: string | undefined, storageKey?: string) {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [live, setLive] = useState<Live | null>(null);
  const [pending, setPending] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const ask = useCallback(
    async (question: string, settings: AskSettings = {}) => {
      const q = question.trim();
      if (!q) return;
      const { limit = 8, mode = "agentic", options = {}, stream = true } = settings;
      setBusy(true);
      setError(null);
      setPending(q);
      setLive(EMPTY_LIVE);
      const started = performance.now();
      // Kept from the terminal `done` event, which carries exactly the payload
      // /api/answer returns — so a caller showing the JSON is showing the API's
      // own response, not something the page composed.
      let raw: unknown = null;
      try {
        const out = await api.askStream(
          collectionId,
          q,
          limit,
          mode,
          (event) => {
            if (event.type === "done") raw = event.answer;
            if (stream) setLive((cur) => reduceLive(cur ?? EMPTY_LIVE, event));
          },
          options
        );
        setTurns((t) => [
          ...t,
          {
            question: q,
            mode: out.mode,
            answer: out.answer,
            citations: out.citations,
            grounded: out.grounded,
            matches: out.matches,
            tookMs: out.tookMs || performance.now() - started,
            traceId: out.traceId,
            degraded: out.degraded,
            settings: {
              vision: options.vision,
              behaviour: options.behaviour,
              docs: options.docs?.length ?? 0,
            },
            raw,
          },
        ]);
      } catch (e) {
        setError((e as Error).message);
      } finally {
        setBusy(false);
        setLive(null);
        setPending(null);
      }
    },
    [collectionId]
  );

  // ------------------------------- history -------------------------------
  //
  // Kept per view AND per collection. A question is only meaningful against
  // the documents it was asked of, so carrying one collection's answers into
  // another would show evidence that is not in the store you are looking at.

  const key = storageKey ? `conversation:${storageKey}:${collectionId ?? "workspace"}` : "";

  useEffect(() => {
    if (!key) return;
    try {
      const saved = window.localStorage.getItem(key);
      setTurns(saved ? (JSON.parse(saved) as Turn[]) : []);
    } catch {
      // Unreadable or from an older shape. An unusable history is not worth an
      // error on screen; it is worth starting clean.
      setTurns([]);
    }
  }, [key]);

  useEffect(() => {
    if (!key) return;
    // Never write an EMPTY history from here. Restoring is asynchronous: this
    // effect runs once with the pre-restore state still in hand, and saving
    // then would wipe the very turns being loaded. Clearing is explicit, and
    // removes the key itself.
    if (turns.length === 0) return;
    try {
      window.localStorage.setItem(
        key,
        JSON.stringify(
          // `raw` is the entire API response per turn and by far the largest
          // thing here — dropped rather than risking the 5MB quota, which
          // fails by throwing and would cost the whole history to keep one
          // JSON dump nobody reloads a page to read.
          turns.slice(-HISTORY_LIMIT).map(({ raw: _raw, ...rest }) => rest)
        )
      );
    } catch {
      /* quota, or private mode. The conversation on screen is unaffected. */
    }
  }, [turns, key]);

  const clear = useCallback(() => {
    setTurns([]);
    setError(null);
    if (key) {
      try {
        window.localStorage.removeItem(key);
      } catch {
        /* nothing to do — the turns are already gone from the screen */
      }
    }
  }, [key]);

  return { turns, live, pending, busy, error, ask, clear, setError };
}
