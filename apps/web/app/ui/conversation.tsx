"use client";

import { useCallback, useState } from "react";
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
export function useConversation(collectionId: string | undefined) {
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

  const clear = useCallback(() => {
    setTurns([]);
    setError(null);
  }, []);

  return { turns, live, pending, busy, error, ask, clear, setError };
}
