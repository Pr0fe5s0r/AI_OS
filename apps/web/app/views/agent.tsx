"use client";

import { useEffect, useRef, useState } from "react";
import { CheckIcon, Dot, EvidenceRow, Artifact, Markdown, TraceStep, WorkflowPlan, appColor } from "../lib";
import { WorkflowGraph } from "./workflow-graph";

/* The Agent thread — Claude-style: restrained, no heavy chrome. Every message
   the user sends goes to the REAL tool-use agent (POST /api/agent/chat): the
   model grounds its answer in the company's data via read tools and can take
   action via run_action — which rides the same human-approval brake as the
   Feed buttons. Both turns are persisted server-side, so what you see here is
   exactly what GET .../messages returns and a refresh never diverges. */

export type Msg = { id?: string; kind: "user" | "context" | "agent"; text: string; artifacts?: Artifact[] };

type Props = {
  messages: Msg[];
  onAsk: (text: string) => Promise<void>;
  onResolveClarification: (situationId: string, choiceId: string) => void;
  /* A half-written sentence handed over from another screen ("this looks
     wrong because …") so disagreeing costs a click, not a blank page. */
  prefill?: string | null;
  onPrefillUsed?: () => void;
  /* Workflow-building mode: a whole workflow authored from one prompt, shown
     inline as an n8n graph the user can Save or Run. Controlled by the parent
     so the Workflows tab's "New workflow" can arm it. */
  mode?: "chat" | "workflow";
  onModeChange?: (m: "chat" | "workflow") => void;
  onCreateWorkflow?: (goal: string) => Promise<void>;
  onSaveWorkflowPlan?: (plan: WorkflowPlan) => Promise<void>;
  onRunWorkflowPlan?: (plan: WorkflowPlan) => Promise<void>;
};

/* Prompt starters — none presuppose a business scenario. "Search your
   events" and "Watch something new" pre-fill a prefix and hand control back
   to the user, same as a slash-command; "What changed today?" is generic
   and answerable for any company from real data alone. */
const CHIPS: { label: string; prefill: string }[] = [
  { label: "Review attention", prefill: "Review everything that needs attention and tell me what to handle first." },
  { label: "Explain my profile", prefill: "Explain what you currently understand about our business and where you learned it." },
  { label: "Check normal patterns", prefill: "Show me what is normal, what is still learning, and what changed lately." },
  { label: "Search all work", prefill: "Search work across all connected apps for " },
  { label: "Check connections", prefill: "Check every connection and tell me whether data is arriving normally." },
  { label: "Review autonomy", prefill: "Explain what you can do alone, what needs approval, and what is in practice mode." },
];

/* The work behind an answer — collapsed by default, because most of the time
   you just want the answer. Expanded, it shows every step that really ran.
   That makes the reasoning inspectable, and it is also the thing that catches
   a model claiming work it never did: no step, no line. */
function TraceBlock({ steps }: { steps: TraceStep[] }) {
  const [open, setOpen] = useState(false);
  const changed = steps.some((s) => s.tool.startsWith("set_") || s.tool === "run_action");

  return (
    <div className="self-start">
      <button
        onClick={() => setOpen(!open)}
        className="flex items-center gap-[7px] border-none bg-transparent p-0 text-[12.5px] text-subtle hover:text-muted"
      >
        <svg
          width="9" height="9" viewBox="0 0 10 10" fill="none" stroke="currentColor" strokeWidth="1.6"
          strokeLinecap="round" strokeLinejoin="round"
          style={{ transform: open ? "rotate(90deg)" : "none", transition: "transform 120ms ease" }}
        >
          <path d="M3 1.5 7 5l-4 3.5" />
        </svg>
        <span>
          {open ? "Hide steps" : "Thought for a moment"}
          <span className="text-subtle"> · {steps.length} step{steps.length === 1 ? "" : "s"}</span>
          {changed && !open && <span className="text-accent"> · changed something</span>}
        </span>
      </button>

      {open && (
        <div className="mt-2 flex flex-col gap-1.5 border-l border-edge pl-3">
          {steps.map((s, i) => (
            <div key={i} className="text-[12.5px] leading-snug">
              <span className="text-ink">{s.label}</span>
              <span className="text-subtle"> — {s.detail}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function WorkflowPlanBlock({ plan, saved, onSave, onRun }: {
  plan: WorkflowPlan; saved?: boolean;
  onSave?: (p: WorkflowPlan) => Promise<void>; onRun?: (p: WorkflowPlan) => Promise<void>;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(saved ? "saved" : null);
  const blocked = plan.clarifications.length > 0 || plan.steps.length === 0;

  async function act(kind: "save" | "run", fn?: (p: WorkflowPlan) => Promise<void>) {
    if (!fn) return;
    setBusy(kind);
    try { await fn(plan); setDone(kind === "run" ? "ran" : "saved"); }
    finally { setBusy(null); }
  }

  return (
    <div className="self-start w-full max-w-[440px] rounded-lg border border-edgeStrong bg-panel">
      <div className="flex items-center justify-between border-b border-edge px-3.5 py-2.5">
        <span className="text-[11px] font-semibold tracking-[1px] text-muted">WORKFLOW</span>
        <span className="truncate text-[13px] font-semibold text-ink">{plan.name}</span>
      </div>

      {plan.apps_used && plan.apps_used.length > 0 && (
        <div className="flex flex-wrap gap-x-3 gap-y-1 border-b border-edge px-3.5 py-2 text-[11px] text-muted">
          {plan.apps_used.map((a) => (
            <span key={a.app} className="flex items-center gap-[5px]">
              <Dot color={appColor(a.app)} size={5} /><span className="capitalize">{a.app}</span>
              <span className="text-subtle">— {a.does}</span>
            </span>
          ))}
        </div>
      )}

      <div className="px-3.5 py-3">
        {plan.clarifications.length > 0 ? (
          <div className="text-[12.5px] text-muted">
            <div className="mb-1 text-[11px] font-semibold tracking-[1px] text-warn">NEEDS AN ANSWER</div>
            {plan.clarifications.join(" ")}
          </div>
        ) : (
          <WorkflowGraph trigger={plan.trigger} steps={plan.steps} />
        )}
      </div>

      {!blocked && (
        <div className="flex items-center gap-2.5 border-t border-edge px-3.5 py-2.5">
          {done ? (
            <span className="inline-flex items-center gap-2 text-[12.5px] font-semibold text-accent">
              <CheckIcon size={13} /> {done === "ran" ? "Saved & ran" : "Saved to Workflows"}
            </span>
          ) : (
            <>
              <button onClick={() => act("save", onSave)} disabled={busy !== null}
                      className="rounded-md border-none bg-accent px-3.5 py-1.5 text-[12.5px] font-semibold text-[#0a0a0a] hover:opacity-90 disabled:opacity-40">
                {busy === "save" ? "Saving…" : "Save workflow"}
              </button>
              <button onClick={() => act("run", onRun)} disabled={busy !== null}
                      className="rounded-md border border-edgeStrong bg-transparent px-3 py-1.5 text-[12.5px] text-ink hover:bg-elevated disabled:opacity-40">
                {busy === "run" ? "Running…" : "Save & run once"}
              </button>
            </>
          )}
        </div>
      )}
    </div>
  );
}

function ArtifactBlock({ artifact, onResolveClarification, onSaveWorkflowPlan, onRunWorkflowPlan }: {
  artifact: Artifact;
  onResolveClarification: Props["onResolveClarification"];
  onSaveWorkflowPlan?: Props["onSaveWorkflowPlan"];
  onRunWorkflowPlan?: Props["onRunWorkflowPlan"];
}) {
  if (artifact.type === "trace") {
    return <TraceBlock steps={artifact.steps} />;
  }

  if (artifact.type === "workflow_plan") {
    return <WorkflowPlanBlock plan={artifact.plan} saved={artifact.saved} onSave={onSaveWorkflowPlan} onRun={onRunWorkflowPlan} />;
  }

  if (artifact.type === "chip") {
    return (
      <div className="inline-flex items-center gap-[9px] self-start rounded-full border border-edge bg-panel px-3.5 py-2">
        <CheckIcon size={14} />
        <span className="text-[13px] text-ink">{artifact.label}</span>
      </div>
    );
  }

  if (artifact.type === "evidence") {
    return (
      <div className="rounded-lg border border-edge bg-panel">
        <div className="border-b border-edge px-3.5 py-[9px] text-[11px] font-semibold tracking-[1px] text-muted">
          {artifact.title ?? "EVIDENCE"}
        </div>
        <div className="px-3.5 py-1">
          {artifact.items.map((e) => <EvidenceRow key={e.event_id} e={e} />)}
        </div>
      </div>
    );
  }

  if (artifact.type !== "clarification") return null;
  const choices = artifact.choices ?? [];
  const done = artifact.status === "resolved" || !!artifact.resolved_choice;
  const chosen = choices.find((c) => c.id === artifact.resolved_choice);
  return (
    <div className="rounded-lg border border-edge bg-panel">
      <div className="border-b border-edge px-3.5 py-[9px] text-[11px] font-semibold tracking-[1px] text-muted">
        NEEDS CLARIFICATION
      </div>
      <div className="flex flex-col gap-3 px-3.5 py-3">
        <div>
          <div className="text-[13.5px] font-semibold text-ink">{artifact.title}</div>
          {artifact.summary && <div className="mt-1 text-[13px] leading-relaxed text-muted">{artifact.summary}</div>}
        </div>
        {artifact.evidence && artifact.evidence.length > 0 && (
          <div>{artifact.evidence.map((e) => <EvidenceRow key={e.event_id} e={e} />)}</div>
        )}
        <div className="flex flex-wrap gap-2.5">
          {done ? (
            <span className="inline-flex rounded-md border border-accent px-3 py-1.5 text-[13px] font-semibold text-accent">
              {chosen ? `${chosen.label} chosen` : "Answered"}
            </span>
          ) : choices.slice(0, 4).map((c) => (
            <button
              key={c.id}
              onClick={() => artifact.situation_id && onResolveClarification(artifact.situation_id, c.id)}
              className="flex-none whitespace-nowrap rounded-md border border-edgeStrong bg-transparent px-[13px] py-[7px] text-[13px] text-ink hover:bg-elevated"
            >
              {c.label}
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}

export default function AgentView({
  messages, onAsk, onResolveClarification, prefill, onPrefillUsed,
  mode = "chat", onModeChange, onCreateWorkflow, onSaveWorkflowPlan, onRunWorkflowPlan,
}: Props) {
  const [composer, setComposer] = useState("");
  const [focus, setFocus] = useState(false);
  const [typing, setTyping] = useState(false);
  const threadRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const building = mode === "workflow";

  useEffect(() => {
    const el = threadRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages, typing]);

  useEffect(() => {
    if (!prefill) return;
    setComposer(prefill);
    inputRef.current?.focus();
    onPrefillUsed?.();
  }, [prefill, onPrefillUsed]);

  async function send(text?: string) {
    const t = (text ?? composer).trim();
    if (!t || typing) return;
    setComposer("");
    setTyping(true);
    try {
      if (building && onCreateWorkflow) {
        await onCreateWorkflow(t);
        onModeChange?.("chat");  // one workflow per build; drop back to chat
      } else {
        await onAsk(t);
      }
    } catch {
      /* page.tsx already surfaced the error via a toast + rolled back */
    } finally {
      setTyping(false);
    }
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div ref={threadRef} className="min-h-0 flex-1 overflow-y-auto overflow-x-hidden">
        <div className="mx-auto flex max-w-[720px] flex-col gap-[26px] px-7 pb-4 pt-9">
          {messages.length === 0 && (
            <div className="flex flex-col gap-2 pt-10">
              <div className="text-[14.5px] leading-relaxed text-ink">
                I&apos;m watching your connected tools and linking what happens into one story.
                Ask me anything — or tell me what to watch.
              </div>
              <div className="text-[13px] leading-relaxed text-muted">
                Ask about any part of the system, or choose a starting point below.
              </div>
            </div>
          )}

          {messages.map((m, i) => {
            if (m.kind === "user") {
              return (
                <div key={m.id ?? i} className="card-in max-w-[75%] self-end rounded-xl border border-edge bg-elevated px-3.5 py-2.5 text-[14px] leading-relaxed">
                  {m.text}
                </div>
              );
            }
            if (m.kind === "context") {
              return (
                <div key={m.id ?? i} className="card-in inline-flex items-center gap-2 self-start rounded-full border border-edge bg-panel px-3 py-[5px] text-[12px] text-muted">
                  <Dot color="#3fb950" size={6} />
                  <span>Context loaded · {m.text}</span>
                </div>
              );
            }
            /* The trace goes ABOVE the answer, the way you'd watch someone
               work before hearing their conclusion. Everything else follows. */
            const trace = m.artifacts?.find((a) => a.type === "trace");
            const rest = m.artifacts?.filter((a) => a.type !== "trace") ?? [];
            const hasPlan = m.artifacts?.some((a) => a.type === "workflow_plan");
            return (
              <div key={m.id ?? i} className="card-in flex flex-col gap-3">
                {trace ? (
                  <ArtifactBlock artifact={trace} onResolveClarification={onResolveClarification} />
                ) : !hasPlan ? (
                  <div className="flex items-center gap-[7px] text-[12.5px] text-muted">
                    <CheckIcon size={13} />
                    <span>Answered from what I already knew</span>
                  </div>
                ) : null}
                {m.text && (
                  <div className="text-[14.5px] leading-[1.65] text-ink">
                    <Markdown text={m.text} />
                  </div>
                )}
                {rest.map((a, j) => (
                  <ArtifactBlock
                    key={a.type === "clarification" ? `clarification:${a.situation_id ?? j}` : `${a.type}:${j}`}
                    artifact={a}
                    onResolveClarification={onResolveClarification}
                    onSaveWorkflowPlan={onSaveWorkflowPlan}
                    onRunWorkflowPlan={onRunWorkflowPlan}
                  />
                ))}
              </div>
            );
          })}

          {typing && (
            <div className="flex gap-[5px] py-1">
              {[0, 0.2, 0.4].map((d) => (
                <span
                  key={d}
                  className="h-[5px] w-[5px] rounded-full bg-muted"
                  style={{ animation: "blink 1.2s infinite", animationDelay: `${d}s` }}
                />
              ))}
            </div>
          )}
        </div>
      </div>

      <div className="flex-none px-7 pb-5 pt-2">
        <div className="mx-auto max-w-[720px]">
          {building ? (
            <div className="mb-2.5 flex items-center gap-2 text-[12.5px]">
              <span className="inline-flex items-center gap-2 rounded-full border border-accent bg-panel px-3 py-1.5 text-accent">
                <svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
                  <circle cx="4" cy="4" r="1.6" /><circle cx="4" cy="12" r="1.6" /><circle cx="12" cy="8" r="1.6" />
                  <path d="M5.6 4H9a1.5 1.5 0 0 1 1.5 1.5v.8M5.6 12H9a1.5 1.5 0 0 0 1.5-1.5v-.8" />
                </svg>
                Building a workflow — describe what it should do
              </span>
              <button onClick={() => onModeChange?.("chat")} className="border-none bg-transparent p-0 text-[12px] text-subtle hover:text-ink">
                cancel
              </button>
            </div>
          ) : (
            <div className="mb-2.5 flex flex-wrap gap-2">
              {CHIPS.map((c) => (
                <button
                  key={c.label}
                  onClick={() => { setComposer(c.prefill); inputRef.current?.focus(); }}
                  className="flex-none whitespace-nowrap rounded-full border border-edge bg-panel px-3 py-1.5 text-[12.5px] text-muted hover:border-edgeStrong hover:text-ink"
                >
                  {c.label}
                </button>
              ))}
            </div>
          )}
          <div
            className="flex items-center gap-2 rounded-xl border bg-elevated py-1.5 pl-2 pr-1.5"
            style={{ borderColor: building ? "#2f6f3d" : focus ? "#333333" : "#242424" }}
          >
            {onCreateWorkflow && (
              <button
                onClick={() => onModeChange?.(building ? "chat" : "workflow")}
                title={building ? "Back to chat" : "Build a workflow from a prompt"}
                aria-label="Toggle workflow builder"
                className="flex h-8 w-8 flex-none items-center justify-center rounded-lg border-none bg-transparent hover:bg-panel"
                style={{ color: building ? "#3fb950" : "#8b8b8b" }}
              >
                <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
                  <circle cx="4" cy="4" r="1.6" /><circle cx="4" cy="12" r="1.6" /><circle cx="12" cy="8" r="1.6" />
                  <path d="M5.6 4H9a1.5 1.5 0 0 1 1.5 1.5v.8M5.6 12H9a1.5 1.5 0 0 0 1.5-1.5v-.8" />
                </svg>
              </button>
            )}
            <input
              ref={inputRef}
              value={composer}
              onChange={(e) => setComposer(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  void send();
                }
              }}
              onFocus={() => setFocus(true)}
              onBlur={() => setFocus(false)}
              placeholder={building ? "e.g. Every morning, label unassigned bugs and draft a reply" : "Ask about work, profile, norms, connections, or actions..."}
              className="flex-1 border-none bg-transparent py-1.5 text-[14px] text-ink outline-none placeholder:text-subtle"
            />
            <button
              onClick={() => void send()}
              aria-label="Send"
              className="flex h-8 w-8 flex-none items-center justify-center rounded-lg border-none bg-accent text-[#0a0a0a] hover:opacity-90"
            >
              <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
                <path d="M8 13V3M3.5 7.5 8 3l4.5 4.5" />
              </svg>
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

