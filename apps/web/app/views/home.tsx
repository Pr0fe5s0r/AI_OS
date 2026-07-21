"use client";

import {
  Action, Briefing, Dot, Knowledge, Learning, Situation, Understanding,
  humanize, plural, relativeTime, sourceColor,
} from "../lib";

type Props = {
  briefing: Briefing | null;
  situations: Situation[];
  actions: Action[];
  understanding: Understanding | null;
  learning: Learning | null;
  knowledge: Knowledge | null;
  onGoAttention: () => void;
  onGoWork: () => void;
  onGoProfile: () => void;
  onGoConnections: () => void;
  onAsk: (text: string) => void;
};

function Metric({ label, value, detail, color }: { label: string; value: string | number; detail: string; color: string }) {
  return <div className="min-w-0 border-b border-edge px-4 py-4 last:border-b-0 sm:border-b-0 sm:border-r sm:last:border-r-0">
    <div className="flex items-center gap-2 text-[11px] uppercase tracking-[0.8px] text-subtle"><Dot color={color} size={6} />{label}</div>
    <div className="mt-2 text-[20px] font-semibold text-ink">{value}</div>
    <div className="mt-1 truncate text-[12px] text-muted">{detail}</div>
  </div>;
}

function StateStep({ label, value, state, color, onClick }: {
  label: string; value: string; state: string; color: string; onClick: () => void;
}) {
  return <button onClick={onClick} className="min-h-[96px] border-b border-edge bg-transparent p-4 text-left last:border-b-0 hover:bg-elevated sm:border-b-0 sm:border-r sm:last:border-r-0">
    <div className="flex items-center gap-2"><Dot color={color} size={7} /><span className="text-[12px] font-medium text-ink">{label}</span></div>
    <div className="mt-2 text-[14px] text-ink">{value}</div>
    <div className="mt-1 text-[11.5px] text-subtle">{state}</div>
  </button>;
}

function AttentionRow({ situation }: { situation: Situation }) {
  const color = situation.kind === "clarification" ? "#58a6ff" :
    situation.severity === "critical" || situation.severity === "high" ? "#f85149" : "#d29922";
  const sources = Array.from(new Set(situation.evidence.map((item) => item.source)));
  return <div className="grid gap-2 border-b border-edge px-4 py-3.5 last:border-b-0 sm:grid-cols-[1fr_auto] sm:items-center">
    <div className="min-w-0">
      <div className="flex items-center gap-2"><Dot color={color} size={7} /><span className="truncate text-[13.5px] font-medium text-ink">{situation.title}</span></div>
      <div className="ml-[15px] mt-1 truncate text-[12px] text-muted">{situation.summary}</div>
    </div>
    <div className="ml-[15px] flex items-center gap-2 text-[11.5px] text-subtle sm:ml-0">
      {sources.length ? sources.map((source) => <span key={source} className="flex items-center gap-1 capitalize"><Dot color={sourceColor(source)} size={5} />{source}</span>) : <span>System observation</span>}
    </div>
  </div>;
}

function NormRow({ learning }: { learning: Learning | null }) {
  const learned = learning?.measurements.filter((item) => item.samples > 0) ?? [];
  if (!learning?.measurements.length) return <div className="px-4 py-4 text-[12.5px] text-muted">Waiting for completed work before learning what is normal.</div>;
  return <div>{learning.measurements.slice(0, 3).map((item) => {
    const state = item.samples === 0 ? "Waiting for examples" : item.maturity === "stable" ? "Reliable" : "Still learning";
    return <div key={item.metric} className="grid gap-1 border-b border-edge px-4 py-3 last:border-b-0 sm:grid-cols-[1fr_auto] sm:items-center">
      <div><div className="text-[13px] text-ink">{humanize(item.metric.replace(/_hours$/, ""))}</div><div className="mt-1 text-[11.5px] text-subtle">{item.samples} completed {item.samples === 1 ? "record" : "records"}</div></div>
      <span className="text-[12px] text-muted">{state}</span>
    </div>;
  })}<div className="border-t border-edge px-4 py-2.5 text-[11.5px] text-subtle">{learned.length} of {learning.measurements.length} patterns have enough data</div></div>;
}

export default function HomeView({
  briefing, situations, actions, understanding, learning, knowledge,
  onGoAttention, onGoWork, onGoProfile, onGoConnections, onAsk,
}: Props) {
  const connected = understanding?.watching.filter((item) => item.connected && item.enabled) ?? [];
  const records = understanding?.records.total ?? briefing?.stats.events ?? 0;
  const live = situations.filter((item) => item.status !== "resolved");
  const needsDecision = live.filter((item) => item.kind === "clarification" || item.severity === "critical" || item.severity === "high");
  const pending = actions.filter((item) => item.status === "pending_approval");
  const learned = learning?.measurements.filter((item) => item.samples > 0) ?? [];
  const handled = actions.filter((item) => ["executed", "recorded", "dry_run"].includes(item.status)).slice(0, 3);
  const companyName = knowledge?.company.name || (knowledge?.company.id ? humanize(knowledge.company.id) : "Your business");
  const profileState = knowledge?.company.status === "confirmed" ? "Active profile" : "Needs confirmation";
  const normState = learning?.measurements.length ? `${learned.length} learned, ${learning.measurements.length - learned.length} learning` : "Waiting for data";

  if (!briefing && !understanding) return <div className="mx-auto max-w-[980px] px-6 pt-9 text-[13px] text-muted">Loading today...</div>;

  return <div className="mx-auto max-w-[980px] px-5 pb-20 pt-7 sm:px-8 sm:pt-9">
    <header className="mb-7">
      <div className="text-[11px] uppercase tracking-[1px] text-subtle">Today</div>
      <div className="mt-1 flex flex-col gap-2 sm:flex-row sm:items-end sm:justify-between">
        <div><h1 className="m-0 text-[22px] font-semibold text-ink">{companyName}</h1><p className="mb-0 mt-1 text-[13px] text-muted">{briefing?.headline || (live.length ? `${live.length} items need attention.` : "Everything being watched is quiet.")}</p></div>
        <button onClick={() => onAsk("Give me today's business summary and tell me what needs my attention.")} className="self-start border border-edgeStrong bg-transparent px-3 py-2 text-[12px] text-ink hover:bg-elevated sm:self-auto">Ask Agent for summary</button>
      </div>
    </header>

    <section className="mb-8">
      <div className="mb-3 text-[13px] font-semibold text-ink">System state</div>
      <div className="grid border border-edge bg-panel sm:grid-cols-4">
        <StateStep label="Connections" value={`${connected.length} active`} state={connected.length ? connected.map((item) => humanize(item.source)).join(", ") : "Connect a source"} color={connected.length ? "#3fb950" : "#d29922"} onClick={onGoConnections} />
        <StateStep label="Business profile" value={`Version ${knowledge?.company.version ?? "-"}`} state={profileState} color={knowledge?.company.status === "confirmed" ? "#3fb950" : "#d29922"} onClick={onGoProfile} />
        <StateStep label="Normal patterns" value={`${learned.length} available`} state={normState} color={learned.length ? "#58a6ff" : "#8b8b8b"} onClick={onGoProfile} />
        <StateStep label="Attention" value={`${live.length + pending.length} open`} state={needsDecision.length + pending.length ? `${needsDecision.length + pending.length} need you` : "No decision needed"} color={needsDecision.length + pending.length ? "#d29922" : "#3fb950"} onClick={onGoAttention} />
      </div>
    </section>

    <section className="mb-8">
      <div className="grid border border-edge bg-panel sm:grid-cols-3">
        <Metric label="Apps" value={connected.length} detail="Connected and being watched" color="#3fb950" />
        <Metric label="Work understood" value={records.toLocaleString()} detail="Records across all apps" color="#58a6ff" />
        <Metric label="Needs you" value={needsDecision.length + pending.length} detail="Decisions and approvals" color={needsDecision.length + pending.length ? "#d29922" : "#3fb950"} />
      </div>
    </section>

    <div className="grid gap-8 lg:grid-cols-[1.15fr_0.85fr]">
      <section>
        <div className="mb-3 flex items-baseline justify-between"><div><div className="text-[14px] font-semibold text-ink">Needs attention</div><div className="mt-1 text-[12px] text-muted">One situation can contain evidence from several apps.</div></div><button onClick={onGoAttention} className="border-none bg-transparent p-0 text-[12px] text-muted underline hover:text-ink">Open Attention</button></div>
        <div className="border border-edge bg-panel">
          {live.length ? live.slice(0, 4).map((item) => <AttentionRow key={item.id} situation={item} />) : <div className="px-4 py-7 text-center text-[13px] text-muted">Nothing needs attention.</div>}
        </div>
      </section>

      <section>
        <div className="mb-3 flex items-baseline justify-between"><div><div className="text-[14px] font-semibold text-ink">Learning</div><div className="mt-1 text-[12px] text-muted">Patterns from completed work.</div></div><button onClick={onGoProfile} className="border-none bg-transparent p-0 text-[12px] text-muted underline hover:text-ink">Open Profile</button></div>
        <div className="border border-edge bg-panel"><NormRow learning={learning} /></div>
      </section>
    </div>

    <section className="mt-8">
      <div className="mb-3 flex items-baseline justify-between"><div><div className="text-[14px] font-semibold text-ink">Recently handled</div><div className="mt-1 text-[12px] text-muted">Recorded here so automated work is never invisible.</div></div><button onClick={onGoWork} className="border-none bg-transparent p-0 text-[12px] text-muted underline hover:text-ink">Browse all work</button></div>
      <div className="border border-edge bg-panel">
        {handled.length ? handled.map((item) => <div key={item.id} className="flex items-center gap-3 border-b border-edge px-4 py-3 last:border-b-0"><Dot color={item.status === "executed" ? "#3fb950" : item.status === "dry_run" ? "#d29922" : "#8b8b8b"} size={7} /><span className="min-w-0 flex-1 truncate text-[13px] text-ink">{humanize(item.action)}</span><span className="text-[11.5px] text-subtle">{relativeTime(item.requested_at)}</span></div>) : <div className="px-4 py-5 text-[12.5px] text-muted">No recent actions.</div>}
      </div>
    </section>
  </div>;
}
