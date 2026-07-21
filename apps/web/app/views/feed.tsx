"use client";

import { useEffect, useState } from "react";
import {
  Action, Card, CardLabel, CheckIcon, DoneChip, Dot, EvidenceRow, Facet, FilledBtn, Item,
  ItemFacets, OutlineBtn, Registry, SEVERITY_LABEL, Situation, SourceDots, TextBtn,
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

function weekday() {
  return new Date().toLocaleDateString(undefined, { weekday: "long" });
}

/* Which moves a card offers, from the profile registry (never invented). */
function movesFor(registry: Registry | null, severity: string): { primary: string | null; alts: string[] } {
  if (!registry) return { primary: null, alts: [] };
  const allowed = registry.autonomy?.allowed_actions ?? registry.actions.map((a) => a.name);
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
  const { primary, alts } = movesFor(registry, s.severity);
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

function ItemRow({ it }: { it: Item }) {
  return (
    <div
      className="grid items-center gap-3 border-b border-edge px-1 py-[10px]"
      style={{ gridTemplateColumns: "auto 1fr auto auto" }}
    >
      <span title={it.status ?? "no status"}><Dot color={statusColor(it.status)} size={7} /></span>
      <span className="min-w-0 truncate text-[13px] text-ink">
        {it.title}
        {it.url && (
          <a href={it.url} target="_blank" rel="noreferrer" className="ml-1.5 font-mono text-[11px]">↗</a>
        )}
      </span>
      <span className="flex items-center gap-1.5 whitespace-nowrap text-[11.5px] text-subtle">
        <Dot color={sourceColor(it.source)} size={5} />
        <span className="capitalize">{it.source}</span>
        <span>·</span>
        <span>{humanize(it.type)}</span>
      </span>
      <span className="w-[74px] text-right text-[11.5px] text-subtle">{relativeTime(it.timestamp)}</span>
    </div>
  );
}

function WorkPanel({ items, facets, noun, filters, onFilter }: {
  items: Item[]; facets: ItemFacets | null; noun: string;
  filters: ItemFilters; onFilter: (next: ItemFilters) => void;
}) {
  if (!facets) return <div className="pt-8 text-[13px] text-muted">Loading…</div>;
  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-col gap-2 rounded-lg border border-edge bg-panel px-3.5 py-3">
        <FacetRow label="Source" facets={facets.sources} active={filters.source}
                  onPick={(k) => onFilter({ ...filters, source: k })} />
        <FacetRow label="Type" facets={facets.types} active={filters.type}
                  onPick={(k) => onFilter({ ...filters, type: k })} />
        <FacetRow label="Status" facets={facets.statuses} active={filters.status}
                  onPick={(k) => onFilter({ ...filters, status: k })} />
      </div>

      <div>
        {items.map((it) => <ItemRow key={it.id} it={it} />)}
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

function AttentionDetail({ item, registry, handled, onRunMove, onDecide, onResolveClarification, onDismiss, onDiscuss }: {
  item: AttentionItem;
  registry: Registry | null;
  handled: Record<string, string>;
  onRunMove: Props["onRunMove"];
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
  const { primary, alts } = movesFor(registry, s.severity);
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

function AttentionCenter({ live, pending, registry, handled, onRunMove, onDecide, onResolveClarification, onDismiss, onDiscuss, onGoWork, totalItems, itemNoun }: {
  live: Situation[];
  pending: Action[];
  registry: Registry | null;
  handled: Record<string, string>;
  onRunMove: Props["onRunMove"];
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
          {selected && <AttentionDetail item={selected} registry={registry} handled={handled} onRunMove={onRunMove} onDecide={onDecide} onResolveClarification={onResolveClarification} onDismiss={onDismiss} onDiscuss={onDiscuss} />}
        </div>
      </div>
    </>
  );
}

export default function FeedView(props: Props) {
  const {
    mode, situations, actions, registry, dismissed, handled, eventsToday, connectedCount,
    items, itemFacets, itemNoun, itemFilters, onFilterItems,
    onRunMove, onDecide, onResolveClarification, onDismiss, onDiscuss, onGoConnections, onGoWork,
  } = props;

  if (situations === null || actions === null) {
    return <div className="mx-auto max-w-[1060px] px-7 pb-20 pt-9"><div className="text-[13px] text-muted">Loading attention...</div></div>;
  }

  if (connectedCount === 0 && eventsToday === 0) {
    return (
      <div className="flex flex-col items-center px-10 pt-[110px] text-center">
        <span className="mb-[18px]"><Dot color="#3fb950" /></span>
        <div className="mb-2 text-[16px] font-semibold">Connect your first tool and I&apos;ll start learning your normal</div>
        <div className="mb-[22px] max-w-[400px] text-[13.5px] leading-relaxed text-muted">MarkOS watches quietly, links events together, and only speaks up when something breaks the pattern.</div>
        <FilledBtn onClick={onGoConnections}>Connect a tool</FilledBtn>
      </div>
    );
  }

  const rank = (s: Situation) => ({ critical: 0, high: 1, medium: 2, low: 3 } as any)[s.severity] ?? 3;
  const live = situations.filter((s) => !dismissed.has(s.id) && s.status !== "resolved").sort((a, b) => rank(a) - rank(b) || +new Date(b.created_at) - +new Date(a.created_at));
  const pending = actions.filter((a) => a.status === "pending_approval");
  const needCount = live.length + pending.length;
  const totalItems = (itemFacets?.sources ?? []).reduce((n, f) => n + f.count, 0);

  return (
    <div className={`mx-auto px-5 pb-16 pt-7 sm:px-8 sm:pt-9 ${mode === "work" ? "max-w-[920px]" : "max-w-[1120px]"}`}>
      <header className="mb-6">
        <div className="text-[11px] uppercase tracking-[1px] text-subtle">{weekday()}</div>
        <h1 className="mb-0 mt-1 text-[22px] font-semibold text-ink">{mode === "work" ? "Work" : "Attention"}</h1>
        <p className="mb-0 mt-1 text-[13px] leading-relaxed text-muted">
          {mode === "work"
            ? `${totalItems} ${itemNoun}${totalItems === 1 ? "" : "s"} from every connected app, kept separate from AI judgments.`
            : needCount > 0
              ? `${needCount} ${needCount === 1 ? "item needs" : "items need"} your input. Choose a category, then handle one item without losing the rest.`
              : `Everything is quiet across ${eventsToday.toLocaleString()} watched events.`}
        </p>
      </header>

      {mode === "work" ? (
        <WorkPanel items={items} facets={itemFacets} noun={itemNoun} filters={itemFilters} onFilter={onFilterItems} />
      ) : (
        <AttentionCenter live={live} pending={pending} registry={registry} handled={handled} onRunMove={onRunMove} onDecide={onDecide} onResolveClarification={onResolveClarification} onDismiss={onDismiss} onDiscuss={onDiscuss} onGoWork={onGoWork} totalItems={totalItems} itemNoun={itemNoun} />
      )}
    </div>
  );
}
