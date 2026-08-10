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

/** The live trail, while a turn is in flight.
 *
 *  A progress line that never moves is indistinguishable from a hang, and an
 *  agentic answer is a long half minute. */
export function LiveTrail({ live }: { live: Live }) {
  return (
    <div className="space-y-1">
      {live.activity.map((a, n) => (
        <p
          key={n}
          className={cx(
            "font-mono text-2xs",
            a.kind === "tool" ? "text-subtle" : "text-muted",
            a.ok === false && "text-hot"
          )}
        >
          {a.kind === "tool" ? "› " : ""}
          {a.text}
        </p>
      ))}
      {live.draft && (
        <p className="whitespace-pre-wrap pt-1 text-sm leading-relaxed text-muted">{live.draft}</p>
      )}
    </div>
  );
}

// --------------------------------- engine ---------------------------------

// How many turns a saved conversation keeps. Enough to be a history,
// bounded because every turn carries its passages and the quota is small.
const HISTORY_LIMIT = 40;

export type AskSettings = {
  limit?: number;
  mode?: api.AskMode;
  options?: api.AskOptions;
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
      const { limit = 8, mode = "agentic", options = {} } = settings;
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
            setLive((cur) => reduceLive(cur ?? EMPTY_LIVE, event));
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
