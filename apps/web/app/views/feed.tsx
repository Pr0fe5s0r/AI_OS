"use client";

import { useEffect, useState } from "react";
import {
  Action, Card, CardLabel, CheckIcon, DoneChip, Dot, Empty, EvidenceRow, Facet, FilledBtn, Item,
  ItemFacets, OutlineBtn, Page, PrimaryBtn, Registry, ReviewFinding, SEVERITY_LABEL, Situation, SourceDots, TextBtn,
  humanize, plural, relativeTime, sourceColor, statusColor, term,
} from "../lib";

/* The Feed: newest / most-severe first. Three card types —
   URGENT (danger dot, ONE filled button = recommended move),
   WATCH (warn dot, outlined button), DONE FOR YOU (success dot, no buttons) —
   plus NEEDS APPROVAL for pending actions. Details expand INLINE, never a modal. */

type ItemFilters = { source?: string; type?: string; status?: string };

type Props = {
  mode: "attention" | "work";
  situations: Situation[] | null;
  actions: Action[] | null;
  registry: Registry | null;
  dismissed: Set<string>;
  handled: Record<string, string>; // situation id -> move that ran
  eventsToday: number;
  connectedCount: number;
  items: Item[];
  itemFacets: ItemFacets | null;
  itemNoun: string;
  itemFilters: ItemFilters;
  onFilterItems: (next: ItemFilters) => void;
  onRunMove: (move: string, situationId: string) => void;
  onRequestReview: (thingId: string) => void;
  reviews: Record<string, ReviewFinding[]>;
  onRunReview: (thingId: string) => void;
  onLoadReview: (thingId: string) => void;
  onDismissFinding: (thingId: string, findingId: string) => void;
  busy: string | null;
  onDecide: (actionId: number, verdict: "approve" | "reject") => void;
  onResolveClarification: (situationId: string, choiceId: string) => void;
  onDismiss: (situationId: string) => void;
  onDiscuss: (topic: { chip: string; text: string }) => void;
  onGoConnections: () => void;
  onGoWork: () => void;
  tourStep: number;
  onTourNext: () => void;
  onTourSkip: () => void;
};

/* Which moves a card offers, from the profile registry (never invented).

   Filtered by the record this card is actually about. Some moves only work on
   one kind of thing behind an otherwise identical URL — "request changes on
   pull request" on a plain issue was being offered here, and it can only ever
   fail: the server refuses to build it (params_for_move), so the button's one
   possible outcome was an error after someone had chosen it. The connector
   declares the pattern; the card just checks its own evidence against it. */
function movesFor(
  registry: Registry | null, severity: string, evidenceUrl?: string | null,
): { primary: string | null; alts: string[] } {
  if (!registry) return { primary: null, alts: [] };
  const applicable = new Set(
    registry.actions
      .filter((a) => {
        if (!a.applies_to_url) return true;
        return !!evidenceUrl && new RegExp(a.applies_to_url).test(evidenceUrl);
      })
      .map((a) => a.name),
  );
  const allowed = (registry.autonomy?.allowed_actions ?? registry.actions.map((a) => a.name))
    .filter((name) => applicable.has(name));
  const escalation = registry.autonomy?.escalation_action;
  const urgent = severity === "critical" || severity === "high";
  const primary = urgent && escalation && allowed.includes(escalation) ? escalation : allowed[0] ?? null;
  const alts = allowed.filter((a) => a !== primary).slice(0, 2);
  return { primary, alts };
}

function ClarificationCard({ s, onResolveClarification, onDiscuss }: {
  s: Situation; onResolveClarification: Props["onResolveClarification"]; onDiscuss: Props["onDiscuss"];
}) {
  const [expanded, setExpanded] = useState(false);
  const choices = s.choices ?? [];
  const chosen = choices.find((c) => c.id === s.resolved_choice);
  const done = s.status === "resolved";
  return (
    <Card dimmed={done}>
      <CardLabel color="#58a6ff">NEEDS CLARIFICATION</CardLabel>
      <div className="mb-[5px] text-[15px] font-semibold">{s.title}</div>
      <div className="text-[13px] leading-relaxed text-muted">{s.summary}</div>
      <SourceDots sources={s.evidence.map((e) => e.source)} />

      <div className="mt-3.5 flex flex-wrap items-center gap-x-3 gap-y-3">
        {done ? (
          <DoneChip>{chosen ? `${chosen.label} chosen` : "Answered"}</DoneChip>
        ) : (
          choices.slice(0, 4).map((c) => (
            <OutlineBtn key={c.id} onClick={() => onResolveClarification(s.id, c.id)}>
              {c.label}
            </OutlineBtn>
          ))
        )}
        <TextBtn onClick={() => setExpanded(!expanded)}>{expanded ? "Hide details" : "Details"}</TextBtn>
        <TextBtn
          onClick={() =>
            onDiscuss({
              chip: `${s.title} · ${s.rule}`,
              text: `I need your call on "${s.title}". Pick one of the choices on the clarification card, or tell me what changed.`,
            })
          }
        >
          Discuss
        </TextBtn>
      </div>

      {expanded && (
        <div className="mt-[15px] flex flex-col gap-3 border-t border-edge pt-3.5">
          <div className="flex flex-col gap-0.5">
            {s.evidence.map((e) => <EvidenceRow key={e.event_id} e={e} />)}
            {s.evidence.length === 0 && (
              <div className="py-1.5 text-[13px] text-subtle">No cited events on this card.</div>
            )}
          </div>
          {done && chosen && (
            <div className="text-[13px] leading-relaxed text-muted">
              <span className="font-semibold text-ink">Chosen:</span> {chosen.label}
            </div>
          )}
        </div>
      )}
    </Card>
  );
}
function SituationCard({
  s, registry, handledMove, onRunMove, onResolveClarification, onDismiss, onDiscuss, tourStep, onTourNext, onTourSkip, isFirst,
}: {
  s: Situation; registry: Registry | null; handledMove: string | undefined;
  onRunMove: Props["onRunMove"]; onResolveClarification: Props["onResolveClarification"]; onDismiss: Props["onDismiss"]; onDiscuss: Props["onDiscuss"];
  tourStep: number; onTourNext: () => void; onTourSkip: () => void; isFirst: boolean;
}) {
  const [expanded, setExpanded] = useState(false);
  const done = !!handledMove || s.status === "resolved";
  const sev = SEVERITY_LABEL[s.severity] ?? SEVERITY_LABEL.low;
  const { primary, alts } = movesFor(registry, s.severity, s.evidence[0]?.url);
  const urgent = s.severity === "critical" || s.severity === "high";

  return (
    <div className="relative">
      <Card dimmed={done}>
        {isFirst && tourStep === 2 && (
          <div className="absolute right-3 top-3 z-30 w-[226px] rounded-lg border border-accent bg-elevated px-3.5 py-3">
            <div className="font-mono text-[10.5px] text-accent">2 / 3</div>
            <div className="my-1.5 mb-2.5 text-[12.5px] leading-normal text-ink">
              Decisions arrive here — one tap handles it.
            </div>
            <div className="flex items-center gap-2.5">
              <button onClick={onTourNext} className="flex-none whitespace-nowrap rounded-[5px] bg-accent px-[11px] py-[5px] text-[12px] font-semibold text-[#0a0a0a] hover:opacity-90">Next</button>
              <button onClick={onTourSkip} className="flex-none border-none bg-transparent p-0 text-[12px] text-subtle hover:text-muted">Skip</button>
            </div>
          </div>
        )}

        <CardLabel color={done ? "#3fb950" : sev.color}>{done ? "HANDLED" : sev.label}</CardLabel>
        <div className="mb-[5px] text-[15px] font-semibold">{s.title}</div>
        <div className="text-[13px] leading-relaxed text-muted">{s.summary}</div>
        <SourceDots sources={s.evidence.map((e) => e.source)} />

        <div className="relative mt-3.5 flex flex-wrap items-center gap-x-4 gap-y-3">
          {done ? (
            <DoneChip>{handledMove ? `${humanize(handledMove)} ✓` : "Resolved ✓"}</DoneChip>
          ) : primary ? (
            urgent ? (
              <FilledBtn onClick={() => onRunMove(primary, s.id)}>{humanize(primary)}</FilledBtn>
            ) : (
              <OutlineBtn onClick={() => onRunMove(primary, s.id)}>{humanize(primary)}</OutlineBtn>
            )
          ) : null}
          <TextBtn onClick={() => setExpanded(!expanded)}>{expanded ? "Hide details" : "Details"}</TextBtn>
          <TextBtn
            onClick={() =>
              onDiscuss({
                chip: `${s.title} · ${s.rule}`,
                text: `About "${s.title}" — ask me anything. The evidence and prepared moves are on its Feed card.`,
              })
            }
          >
            Discuss
          </TextBtn>
          {!done && <TextBtn onClick={() => onDismiss(s.id)}>Dismiss</TextBtn>}

          {isFirst && tourStep === 3 && (
            <div className="absolute left-0 top-[calc(100%+10px)] z-30 w-[246px] rounded-lg border border-accent bg-elevated px-3.5 py-3">
              <div className="font-mono text-[10.5px] text-accent">3 / 3</div>
              <div className="my-1.5 mb-2.5 text-[12.5px] leading-normal text-ink">
                Green is the recommended move. Details shows why.
              </div>
              <button onClick={onTourSkip} className="flex-none whitespace-nowrap rounded-[5px] bg-accent px-[11px] py-[5px] text-[12px] font-semibold text-[#0a0a0a] hover:opacity-90">Done</button>
            </div>
          )}
        </div>

        {expanded && (
          <div className="mt-[15px] flex flex-col gap-3 border-t border-edge pt-3.5">
            <div className="flex flex-col gap-0.5">
              {s.evidence.map((e) => (
                <EvidenceRow key={e.event_id} e={e} />
              ))}
              {s.evidence.length === 0 && (
                <div className="py-1.5 text-[13px] text-subtle">No cited events on this card.</div>
              )}
            </div>
            {s.recommended_action && (
              <div className="text-[13px] leading-relaxed text-muted">
                <span className="font-semibold text-ink">Why this matters:</span> {s.recommended_action}
              </div>
            )}
            {!done && alts.length > 0 && (
              <div className="flex gap-2.5">
                {alts.map((a) => (
                  <OutlineBtn key={a} onClick={() => onRunMove(a, s.id)}>
                    {humanize(a)}
                  </OutlineBtn>
                ))}
              </div>
            )}
          </div>
        )}
      </Card>
    </div>
  );
}

function ApprovalCard({ a, onDecide, onDiscuss }: {
  a: Action; onDecide: Props["onDecide"]; onDiscuss: Props["onDiscuss"];
}) {
  const argument = String(a.params?.argument ?? "");
  const rationale = String(a.params?.rationale ?? "");
  return (
    <Card>
      <CardLabel color="#58a6ff">NEEDS APPROVAL</CardLabel>
      <div className="mb-[5px] text-[15px] font-semibold">
        {humanize(a.action)} is ready — approve to run?
      </div>
      <div className="text-[13px] leading-relaxed text-muted">
        {argument ? `“${argument.length > 180 ? argument.slice(0, 179) + "…" : argument}”` : a.detail}
        {rationale && <span className="text-subtle"> — {rationale}</span>}
      </div>
      <div className="mt-3.5 flex flex-wrap items-center gap-x-4 gap-y-3">
        <FilledBtn onClick={() => onDecide(a.id, "approve")}>Approve &amp; run</FilledBtn>
        <TextBtn
          onClick={() =>
            onDiscuss({
              chip: `Pending: ${humanize(a.action)} · #${a.id}`,
              text: `About the pending "${humanize(a.action)}" action — it was proposed by ${a.requested_by}. Approve it from the Feed, or tell me what to change.`,
            })
          }
        >
          Discuss
        </TextBtn>
        <TextBtn onClick={() => onDecide(a.id, "reject")}>Reject</TextBtn>
      </div>
    </Card>
  );
}

/* ---------------------- the work itself (not the alerts) ----------------------
   Every label below comes from the data or the profile — "open"/"closed" are
   values GitHub happens to use, and an inventory profile would render its own
   ("fulfilled", "counted") through exactly the same code. */

function FacetRow({ label, facets, active, onPick }: {
  label: string; facets: Facet[]; active?: string; onPick: (key?: string) => void;
}) {
  if (facets.length <= 1) return null; // a single value is not a choice
  const total = facets.reduce((n, f) => n + f.count, 0);
  const chip = (key: string | undefined, text: string, count: number) => {
    const on = active === key;
    return (
      <button
        key={text}
        onClick={() => onPick(key)}
        className="flex-none whitespace-nowrap rounded-full border px-2.5 py-1 text-[12px]"
        style={{
          borderColor: on ? "#3fb950" : "#242424",
          color: on ? "#3fb950" : "#8b8b8b",
          background: on ? "rgba(63,185,80,0.08)" : "transparent",
        }}
      >
        {text} <span className="text-subtle">{count}</span>
      </button>
    );
  };
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      <span className="w-[52px] flex-none text-[11.5px] text-subtle">{label}</span>
      {chip(undefined, "All", total)}
      {facets.map((f) => chip(f.key, humanize(f.key), f.count))}
    </div>
  );
}

/* Kind of record gets TABS, not chips. Issues and pull requests are different
   work with different questions ("who owns this?" vs "who reviews this?"), and
   a chip in a row of three filters reads as one more refinement rather than as
   the two piles the day is actually made of. Same data as the Type facet it
   replaces — promoted, because where something lives should be obvious before
   it is filtered. Falls back to nothing when a workspace has only one kind:
   a tab bar with one tab is furniture. */
function TypeTabs({ facets, active, onPick }: {
  facets: Facet[]; active?: string; onPick: (key?: string) => void;
}) {
  if (facets.length <= 1) return null;
  const total = facets.reduce((n, f) => n + f.count, 0);
  const tab = (key: string | undefined, text: string, count: number) => {
    const on = active === key;
    return (
      <button
        key={text}
        onClick={() => onPick(key)}
        className="flex-none whitespace-nowrap border-b-2 px-1 pb-2 text-[13px]"
        style={{
          borderColor: on ? "#3fb950" : "transparent",
          color: on ? "#ededed" : "#8b8b8b",
        }}
      >
        {text} <span className="ml-0.5 text-[11.5px] text-subtle">{count}</span>
      </button>
    );
  };
  return (
    <div className="flex flex-wrap items-center gap-5 border-b border-edge">
      {tab(undefined, "All", total)}
      {facets.map((f) => tab(f.key, `${humanize(f.key)}s`, f.count))}
    </div>
  );
}

const SEV_COLOR: Record<string, string> = {
  critical: "#f85149", high: "#f0883e", medium: "#d29922", low: "#8b949e", info: "#8b949e",
};

/* What the reviewer did, shown where GitHub shows it: grouped by file, each
   finding pinned to its line with severity, concern and the fix. This is the
   whole point — the reviewer's work made visible, step by step, not buried as
   scattered feed cards. */
function ReviewPanel({ thingId, findings, running, onRunReview, onRequestReview, onDismissFinding }: {
  thingId: string; findings: ReviewFinding[] | undefined; running: boolean;
  onRunReview: (id: string) => void; onRequestReview: (id: string) => void;
  onDismissFinding: (thingId: string, findingId: string) => void;
}) {
  const byFile: Record<string, ReviewFinding[]> = {};
  (findings ?? []).forEach((f) => { (byFile[f.file_path ?? "(general)"] ??= []).push(f); });
  const files = Object.keys(byFile);
  return (
    <div className="border-t border-edge bg-black/20 px-3.5 py-3">
      <div className="mb-3 flex flex-wrap items-center gap-2.5">
        <OutlineBtn onClick={() => onRunReview(thingId)}>
          {running ? "Reviewing…" : findings && findings.length ? "Re-review" : "Review now"}
        </OutlineBtn>
        {findings && findings.length > 0 && (
          <FilledBtn onClick={() => onRequestReview(thingId)}>Request changes on PR</FilledBtn>
        )}
        {findings && (
          <span className="text-[11.5px] text-subtle">
            {findings.length} finding{findings.length === 1 ? "" : "s"}
          </span>
        )}
      </div>

      {findings === undefined && <div className="text-[12.5px] text-subtle">Loading review…</div>}
      {findings && findings.length === 0 && (
        <div className="py-1 text-[12.5px] text-muted">
          Nothing flagged yet — press “Review now” to read this diff.
        </div>
      )}

      {files.map((file) => (
        <div key={file} className="mb-3 last:mb-0">
          <div className="mb-1.5 font-mono text-[11.5px] text-subtle">{file}</div>
          <div className="flex flex-col gap-1.5">
            {byFile[file].map((f) => (
              <div key={f.id} className="rounded-md border border-edge bg-panel px-3 py-2.5">
                <div className="mb-1 flex flex-wrap items-center gap-2">
                  <Dot color={SEV_COLOR[f.severity] ?? "#8b949e"} size={7} />
                  <span className="text-[10.5px] font-semibold uppercase tracking-[0.5px]"
                        style={{ color: SEV_COLOR[f.severity] ?? "#8b949e" }}>{f.severity}</span>
                  <span className="rounded bg-edge px-1.5 py-0.5 text-[10.5px] text-subtle">{humanize(f.concern)}</span>
                  {f.line != null && <span className="font-mono text-[11px] text-subtle">line {f.line}</span>}
                </div>
                <div className="text-[13px] font-medium leading-snug text-ink">{f.title}</div>
                {f.rationale && <div className="mt-1 text-[12.5px] leading-relaxed text-muted">{f.rationale}</div>}
                <div className="mt-1.5"><TextBtn onClick={() => onDismissFinding(thingId, f.id)}>Dismiss</TextBtn></div>
              </div>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}

function ItemRow({ it, reviewable, reviews, running, onRunReview, onLoadReview, onRequestReview, onDismissFinding }: {
  it: Item; reviewable: boolean; reviews: Record<string, ReviewFinding[]>; running: boolean;
  onRunReview: (id: string) => void; onLoadReview: (id: string) => void;
  onRequestReview: (id: string) => void; onDismissFinding: (thingId: string, findingId: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const findings = reviews[it.id];
  const count = findings?.length;
  useEffect(() => {
    if (open && findings === undefined) onLoadReview(it.id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);
  return (
    <div className="border-b border-edge">
      <div className="grid items-center gap-3 px-1 py-[10px]"
           style={{ gridTemplateColumns: "auto 1fr auto auto auto" }}>
        <span title={it.status ?? "no status"}><Dot color={statusColor(it.status)} size={7} /></span>
        <span className="min-w-0 truncate text-[13px] text-ink">
          {it.title}
          {it.url && <a href={it.url} target="_blank" rel="noreferrer" className="ml-1.5 font-mono text-[11px]">↗</a>}
        </span>
        <span className="flex items-center gap-1.5 whitespace-nowrap text-[11.5px] text-subtle">
          <Dot color={sourceColor(it.source)} size={5} />
          <span className="capitalize">{it.source}</span><span>·</span><span>{humanize(it.type)}</span>
        </span>
        {reviewable ? (
          <button onClick={() => setOpen((v) => !v)}
                  className="whitespace-nowrap rounded border border-edge px-2 py-[3px] text-[11.5px] text-ink transition-colors hover:bg-edge">
            {count != null && count > 0
              ? <span className="text-danger">Review · {count}</span>
              : "Review"}
            <span className="ml-1 text-subtle">{open ? "▲" : "▼"}</span>
          </button>
        ) : <span />}
        <span className="w-[74px] text-right text-[11.5px] text-subtle">{relativeTime(it.timestamp)}</span>
      </div>
      {open && reviewable && (
        <ReviewPanel thingId={it.id} findings={findings} running={running}
                     onRunReview={onRunReview} onRequestReview={onRequestReview}
                     onDismissFinding={onDismissFinding} />
      )}
    </div>
  );
}

function WorkPanel({ items, facets, noun, filters, onFilter, reviewableTypes, reviews, busy,
                    onRunReview, onLoadReview, onRequestReview, onDismissFinding }: {
  items: Item[]; facets: ItemFacets | null; noun: string;
  filters: ItemFilters; onFilter: (next: ItemFilters) => void;
  reviewableTypes: string[]; reviews: Record<string, ReviewFinding[]>; busy: string | null;
  onRunReview: (id: string) => void; onLoadReview: (id: string) => void;
  onRequestReview: (id: string) => void; onDismissFinding: (thingId: string, findingId: string) => void;
}) {
  if (!facets) return <div className="pt-8 text-[13px] text-muted">Loading…</div>;
  return (
    <div className="flex flex-col gap-3">
      <TypeTabs facets={facets.types} active={filters.type}
                onPick={(k) => onFilter({ ...filters, type: k })} />

      <div className="flex flex-col gap-2 rounded-lg border border-edge bg-panel px-3.5 py-3">
        <FacetRow label="Source" facets={facets.sources} active={filters.source}
                  onPick={(k) => onFilter({ ...filters, source: k })} />
        <FacetRow label="Status" facets={facets.statuses} active={filters.status}
                  onPick={(k) => onFilter({ ...filters, status: k })} />
      </div>

      <div>
        {items.map((it) => (
          <ItemRow key={it.id} it={it}
                   reviewable={reviewableTypes.includes(it.type)}
                   reviews={reviews} running={busy === `runreview:${it.id}`}
                   onRunReview={onRunReview} onLoadReview={onLoadReview}
                   onRequestReview={onRequestReview} onDismissFinding={onDismissFinding} />
        ))}
        {items.length === 0 && (
          <div className="pt-10 text-center text-[13px] text-muted">
            No {noun}s match these filters.
          </div>
        )}
      </div>
    </div>
  );
}

/* Say what actually happened. Only `executed` means a real external system
   was contacted; `recorded` is a log-only move and `dry_run` is a rehearsal —
   labelling either of those "DONE" would claim an effect that never occurred. */
const OUTCOME: Record<string, { label: string; color: string; lead: string }> = {
  executed: { label: "DONE FOR YOU", color: "#3fb950", lead: "" },
  recorded: { label: "NOTED", color: "#8b8b8b", lead: "Recorded for the record — nothing was sent. " },
  dry_run: { label: "REHEARSED", color: "#d29922", lead: "Practice mode: prepared but not sent. " },
};

function DoneForYouCard({ a }: { a: Action }) {
  const o = OUTCOME[a.status] ?? OUTCOME.executed;
  return (
    <Card>
      <CardLabel color={o.color}>{o.label}</CardLabel>
      <div className="mb-[5px] text-[15px] font-semibold">{humanize(a.action)}</div>
      <div className="text-[13px] leading-relaxed text-muted">
        {o.lead}
        {a.detail}
        {a.situation_id ? ` — for ${humanize(a.situation_id)}.` : ""}
      </div>
    </Card>
  );
}

type AttentionCategory = "decision" | "question" | "approval" | "watch";

type AttentionItem =
  | { key: string; category: Exclude<AttentionCategory, "approval">; title: string; summary: string; at: string; sources: string[]; situation: Situation }
  | { key: string; category: "approval"; title: string; summary: string; at: string; sources: string[]; action: Action };

const ATTENTION_META: Record<AttentionCategory, { label: string; hint: string; color: string }> = {
  decision: { label: "Decisions", hint: "Needs your judgment", color: "#f85149" },
  question: { label: "Questions", hint: "The AI needs context", color: "#58a6ff" },
  approval: { label: "Approvals", hint: "Ready, waiting for permission", color: "#d29922" },
  watch: { label: "Watch", hint: "Worth knowing, no rush", color: "#8b949e" },
};

function attentionSources(sources: string[]) {
  return Array.from(new Set(sources.filter(Boolean)));
}

function AttentionDetail({ item, registry, handled, onRunMove, onRequestReview, onDecide, onResolveClarification, onDismiss, onDiscuss }: {
  item: AttentionItem;
  registry: Registry | null;
  handled: Record<string, string>;
  onRunMove: Props["onRunMove"];
  onRequestReview: Props["onRequestReview"];
  onDecide: Props["onDecide"];
  onResolveClarification: Props["onResolveClarification"];
  onDismiss: Props["onDismiss"];
  onDiscuss: Props["onDiscuss"];
}) {
  const meta = ATTENTION_META[item.category];
  if ("action" in item) {
    const a = item.action;
    const argument = String(a.params?.argument ?? "");
    const rationale = String(a.params?.rationale ?? "");
    return (
      <div className="flex min-h-[430px] flex-col p-5 sm:p-7">
        <div className="mb-5 flex items-center justify-between gap-3">
          <CardLabel color={meta.color}>{meta.label.toUpperCase()}</CardLabel>
          <span className="text-[11.5px] text-subtle">{relativeTime(a.requested_at)}</span>
        </div>
        <h2 className="m-0 max-w-[680px] text-[20px] font-semibold leading-snug text-ink">{item.title}</h2>
        <p className="mb-0 mt-2 max-w-[680px] text-[13.5px] leading-relaxed text-muted">{item.summary}</p>
        <SourceDots sources={item.sources} />
        <div className="mt-6 border-t border-edge pt-5">
          <div className="mb-2 text-[10.5px] font-semibold uppercase tracking-[1px] text-subtle">What will happen</div>
          <div className="text-[13px] leading-relaxed text-muted">
            {argument || a.detail || "This prepared action will run after you approve it."}
          </div>
          {rationale && <div className="mt-3 text-[13px] leading-relaxed text-muted"><span className="font-semibold text-ink">Why the AI proposed it:</span> {rationale}</div>}
        </div>
        <div className="mt-auto flex flex-wrap items-center gap-3 pt-7">
          <FilledBtn onClick={() => onDecide(a.id, "approve")}>Approve &amp; run</FilledBtn>
          <OutlineBtn onClick={() => onDecide(a.id, "reject")}>Reject</OutlineBtn>
          <TextBtn onClick={() => onDiscuss({ chip: `Pending: ${humanize(a.action)} · #${a.id}`, text: `Explain the pending "${humanize(a.action)}" action and help me decide whether to approve it.` })}>Discuss with Agent</TextBtn>
        </div>
      </div>
    );
  }

  const s = item.situation;
  const isQuestion = item.category === "question";
  const choices = s.choices ?? [];
  const { primary, alts } = movesFor(registry, s.severity, s.evidence[0]?.url);
  const done = !!handled[s.id] || s.status === "resolved";
  return (
    <div className="flex min-h-[430px] flex-col p-5 sm:p-7">
      <div className="mb-5 flex items-center justify-between gap-3">
        <CardLabel color={meta.color}>{meta.label.toUpperCase()}</CardLabel>
        <span className="text-[11.5px] text-subtle">{relativeTime(s.created_at)}</span>
      </div>
      <h2 className="m-0 max-w-[680px] text-[20px] font-semibold leading-snug text-ink">{s.title}</h2>
      <p className="mb-0 mt-2 max-w-[680px] text-[13.5px] leading-relaxed text-muted">{s.summary}</p>
      <SourceDots sources={item.sources} />

      <div className="mt-6 border-t border-edge pt-5">
        <div className="mb-2 text-[10.5px] font-semibold uppercase tracking-[1px] text-subtle">What the AI used</div>
        <div className="flex max-h-[210px] flex-col overflow-y-auto pr-1">
          {s.evidence.map((e) => <EvidenceRow key={e.event_id} e={e} />)}
          {s.evidence.length === 0 && <div className="py-2 text-[13px] text-subtle">No cited records are attached.</div>}
        </div>
        {!isQuestion && s.recommended_action && (
          <div className="mt-4 text-[13px] leading-relaxed text-muted"><span className="font-semibold text-ink">Why this matters:</span> {s.recommended_action}</div>
        )}
      </div>

      <div className="mt-auto flex flex-wrap items-center gap-3 pt-7">
        {done ? <DoneChip>{handled[s.id] ? `${humanize(handled[s.id])} completed` : "Resolved"}</DoneChip> : isQuestion ? (
          choices.slice(0, 4).map((choice) => <OutlineBtn key={choice.id} onClick={() => onResolveClarification(s.id, choice.id)}>{choice.label}</OutlineBtn>)
        ) : s.rule.startsWith("review.") && s.evidence[0]?.event_id ? (
          // A review finding: the useful action is not a single move but
          // posting ALL of this PR's findings back as one inline review, gated
          // for approval. The per-finding registry moves would each post their
          // own top-level review — noise — so they are deliberately not offered.
          <FilledBtn onClick={() => onRequestReview(s.evidence[0].event_id)}>Request changes on PR</FilledBtn>
        ) : (
          <>
            {primary && (item.category === "decision"
              ? <FilledBtn onClick={() => onRunMove(primary, s.id)}>{humanize(primary)}</FilledBtn>
              : <OutlineBtn onClick={() => onRunMove(primary, s.id)}>{humanize(primary)}</OutlineBtn>)}
            {alts.map((move) => <OutlineBtn key={move} onClick={() => onRunMove(move, s.id)}>{humanize(move)}</OutlineBtn>)}
          </>
        )}
        <TextBtn onClick={() => onDiscuss({ chip: `${s.title} · ${s.rule}`, text: `Help me understand "${s.title}". Use the evidence shown in Attention and explain what I should do.` })}>Discuss with Agent</TextBtn>
        {!done && !isQuestion && <TextBtn onClick={() => onDismiss(s.id)}>Dismiss</TextBtn>}
      </div>
    </div>
  );
}

function AttentionCenter({ live, pending, registry, handled, onRunMove, onRequestReview, onDecide, onResolveClarification, onDismiss, onDiscuss, onGoWork, totalItems, itemNoun }: {
  live: Situation[];
  pending: Action[];
  registry: Registry | null;
  handled: Record<string, string>;
  onRunMove: Props["onRunMove"];
  onRequestReview: Props["onRequestReview"];
  onDecide: Props["onDecide"];
  onResolveClarification: Props["onResolveClarification"];
  onDismiss: Props["onDismiss"];
  onDiscuss: Props["onDiscuss"];
  onGoWork: Props["onGoWork"];
  totalItems: number;
  itemNoun: string;
}) {
  const situations: AttentionItem[] = live.map((s) => {
    const category: Exclude<AttentionCategory, "approval"> = s.kind === "clarification"
      ? "question"
      : (s.severity === "critical" || s.severity === "high") ? "decision" : "watch";
    return { key: `s:${s.id}`, category, title: s.title, summary: s.summary, at: s.created_at, sources: attentionSources(s.evidence.map((e) => e.source)), situation: s };
  });
  const approvals: AttentionItem[] = pending.map((a) => ({
    key: `a:${a.id}`, category: "approval", title: `${humanize(a.action)} is ready`, summary: a.detail || "Review what the AI prepared before it runs.", at: a.requested_at, sources: attentionSources([String(a.params?.source ?? "")]), action: a,
  }));
  const order: AttentionCategory[] = ["decision", "question", "approval", "watch"];
  const all = order.flatMap((category) => [...situations, ...approvals].filter((item) => item.category === category));
  const keys = all.map((item) => item.key).join("|");
  const [selectedKey, setSelectedKey] = useState<string | null>(all[0]?.key ?? null);
  useEffect(() => {
    if (!all.some((item) => item.key === selectedKey)) setSelectedKey(all[0]?.key ?? null);
  }, [keys, selectedKey]);
  const selected = all.find((item) => item.key === selectedKey) ?? all[0];

  if (all.length === 0) {
    return (
      <div className="border-y border-edge py-16 text-center">
        <span className="mb-4 inline-block"><CheckIcon size={16} /></span>
        <div className="text-[14px] text-muted">Nothing needs you right now.</div>
        {totalItems > 0 && <button onClick={onGoWork} className="mt-2 border-none bg-transparent p-0 text-[13px] text-accent underline">See all {totalItems} {itemNoun}{totalItems === 1 ? "" : "s"}</button>}
      </div>
    );
  }

  return (
    <>
      <div className="mb-4 grid grid-cols-2 border border-edge sm:grid-cols-4">
        {order.map((category, index) => {
          const meta = ATTENTION_META[category];
          const categoryItems = all.filter((item) => item.category === category);
          const active = selected?.category === category;
          return (
            <button key={category} onClick={() => categoryItems[0] && setSelectedKey(categoryItems[0].key)} disabled={categoryItems.length === 0}
              className={`min-h-[72px] border-0 bg-transparent px-4 py-3 text-left transition-colors ${index > 0 ? "sm:border-l sm:border-edge" : ""} ${index > 1 ? "border-t border-edge sm:border-t-0" : ""} ${index === 1 ? "border-l border-edge sm:border-l" : ""} ${active ? "bg-elevated" : "hover:bg-panel"} disabled:cursor-default disabled:opacity-45`}>
              <div className="flex items-center gap-2"><Dot color={meta.color} /><span className="text-[18px] font-semibold text-ink">{categoryItems.length}</span></div>
              <div className="mt-1 text-[11.5px] text-muted">{meta.label}</div>
            </button>
          );
        })}
      </div>

      <div className="grid overflow-hidden border border-edge lg:grid-cols-[310px_minmax(0,1fr)]">
        <div className="max-h-[560px] overflow-y-auto border-b border-edge bg-panel lg:border-b-0 lg:border-r">
          {order.map((category) => {
            const categoryItems = all.filter((item) => item.category === category);
            if (!categoryItems.length) return null;
            const meta = ATTENTION_META[category];
            return (
              <div key={category}>
                <div className="sticky top-0 z-10 flex items-center justify-between border-b border-edge bg-panel px-4 py-2.5">
                  <span className="text-[10.5px] font-semibold uppercase tracking-[1px] text-subtle">{meta.label}</span>
                  <span className="text-[10.5px] text-subtle">{meta.hint}</span>
                </div>
                {categoryItems.map((item) => (
                  <button key={item.key} onClick={() => setSelectedKey(item.key)} className={`block w-full border-0 border-b border-edge px-4 py-3.5 text-left transition-colors ${selected?.key === item.key ? "bg-elevated" : "bg-transparent hover:bg-elevated/60"}`}>
                    <div className="flex gap-3">
                      <span className="mt-1"><Dot color={meta.color} /></span>
                      <span className="min-w-0 flex-1">
                        <span className="block truncate text-[13px] font-semibold text-ink">{item.title}</span>
                        <span className="mt-1 block truncate text-[11.5px] text-subtle">{item.sources.length ? item.sources.map(humanize).join(" + ") : "MarkOS"} · {relativeTime(item.at)}</span>
                      </span>
                    </div>
                  </button>
                ))}
              </div>
            );
          })}
        </div>
        <div className="min-w-0 bg-[#0d0d0d]">
          {selected && <AttentionDetail item={selected} registry={registry} handled={handled} onRunMove={onRunMove} onRequestReview={onRequestReview} onDecide={onDecide} onResolveClarification={onResolveClarification} onDismiss={onDismiss} onDiscuss={onDiscuss} />}
        </div>
      </div>
    </>
  );
}

export default function FeedView(props: Props) {
  const {
    mode, situations, actions, registry, dismissed, handled, eventsToday, connectedCount,
    items, itemFacets, itemNoun, itemFilters, onFilterItems,
    onRunMove, onRequestReview, reviews, onRunReview, onLoadReview, onDismissFinding, busy,
    onDecide, onResolveClarification, onDismiss, onDiscuss, onGoConnections, onGoWork,
  } = props;

  if (situations === null || actions === null) {
    return <div className="mx-auto max-w-[1060px] px-7 pb-20 pt-9"><div className="text-[13px] text-muted">Loading attention...</div></div>;
  }

  if (connectedCount === 0 && eventsToday === 0) {
    return (
      <Page
        title={mode === "work" ? "Work" : "Attention"}
        purpose={mode === "work"
          ? "Every record from every connected app, kept separate from anything the AI concluded."
          : "What needs a person right now — and nothing else."}
      >
        <Empty
          title="Nothing is connected yet"
          next="MarkOS watches quietly, links events together, and only speaks up when something breaks your pattern. Connect a tool and it starts learning what normal looks like here."
          action={<PrimaryBtn onClick={onGoConnections}>Connect a tool</PrimaryBtn>}
        />
      </Page>
    );
  }

  const rank = (s: Situation) => ({ critical: 0, high: 1, medium: 2, low: 3 } as any)[s.severity] ?? 3;
  const live = situations.filter((s) => !dismissed.has(s.id) && s.status !== "resolved").sort((a, b) => rank(a) - rank(b) || +new Date(b.created_at) - +new Date(a.created_at));
  const pending = actions.filter((a) => a.status === "pending_approval");
  const needCount = live.length + pending.length;
  const totalItems = (itemFacets?.sources ?? []).reduce((n, f) => n + f.count, 0);

  /* What to CALL what's on screen. `itemNoun` is the vocabulary's single word
     for this company's work ("issue"), which was fine while a source returned
     one kind of thing — but the moment a repo has pull requests too, the header
     read "10 issues" over a list containing a PR, and "1 issue" over a screen
     showing exactly one pull request. Name the filtered kind when one is
     selected; fall back to a neutral word when several kinds are mixed. */
  const kinds = itemFacets?.types ?? [];
  const shownNoun = itemFilters.type
    ? humanize(itemFilters.type)
    : kinds.length > 1 ? "record" : itemNoun;

  return (
    <Page
      title={mode === "work" ? "Work" : "Attention"}
      purpose={mode === "work"
        ? `Every ${shownNoun} from every connected app, kept separate from anything the AI concluded.`
        : "What needs a person right now. Handle one without losing the rest."}
      stats={mode === "work"
        ? [
            { label: totalItems === 1 ? shownNoun : `${shownNoun}s`, value: totalItems, tone: "ok" },
            { label: "apps", value: (itemFacets?.sources ?? []).length, tone: "ok" },
          ]
        : [
            { label: "need you", value: needCount, tone: needCount ? "waiting" : "ok" },
            { label: "waiting for approval", value: pending.length,
              tone: pending.length ? "waiting" : "idle" },
            { label: "events watched", value: eventsToday.toLocaleString(), tone: "ok" },
          ]}
    >
      {mode === "work" ? (
        <WorkPanel items={items} facets={itemFacets} noun={itemNoun} filters={itemFilters} onFilter={onFilterItems}
                   reviewableTypes={registry?.reviewable_types ?? []} reviews={reviews} busy={busy}
                   onRunReview={onRunReview} onLoadReview={onLoadReview}
                   onRequestReview={onRequestReview} onDismissFinding={onDismissFinding} />
      ) : (
        <AttentionCenter live={live} pending={pending} registry={registry} handled={handled} onRunMove={onRunMove} onRequestReview={onRequestReview} onDecide={onDecide} onResolveClarification={onResolveClarification} onDismiss={onDismiss} onDiscuss={onDiscuss} onGoWork={onGoWork} totalItems={totalItems} itemNoun={itemNoun} />
      )}
    </Page>
  );
}
