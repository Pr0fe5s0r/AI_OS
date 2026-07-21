"use client";

/* Shared primitives for the MarkOS design (imported from MarkOS.dc.html).
   Tokens: canvas #0a0a0a · panel #0f0f0f · elevated #161616 · edge #242424 ·
   edgeStrong #333 · ink #e8e8e8 · muted #8b8b8b · subtle #6a6a6a ·
   accent #3fb950 · warn #d29922 · danger #f85149 · info #58a6ff */

export const COMPANY = "default";

export const SOURCE_COLOR: Record<string, string> = {
  github: "#8b949e",
  slack: "#a855f7",
  zendesk: "#2dd4bf",
  ops: "#58a6ff",
};

export function sourceColor(source: string | null | undefined) {
  return SOURCE_COLOR[source ?? ""] ?? "#58a6ff";
}

export const SEVERITY_LABEL: Record<string, { label: string; color: string }> = {
  critical: { label: "URGENT", color: "#f85149" },
  high: { label: "URGENT", color: "#f85149" },
  medium: { label: "WATCH", color: "#d29922" },
  low: { label: "WATCH", color: "#d29922" },
};

/* ------------------------------- types ------------------------------- */

export type Evidence = { event_id: string; source: string; timestamp: string; excerpt: string; url: string | null };
export type Choice = { id: string; label: string; effect?: string; effect_args?: any };
export type TraceStep = { tool: string; label: string; detail: string };
export type Artifact =
  | { type: "clarification"; situation_id?: string; title?: string; summary?: string; evidence?: Evidence[]; choices?: Choice[]; resolved_choice?: string | null; status?: string }
  | { type: "evidence"; title?: string; items: Evidence[] }
  | { type: "chip"; label: string }
  | { type: "trace"; steps: TraceStep[] }
  | { type: "workflow_plan"; plan: WorkflowPlan; saved?: boolean };
export type Message = { id?: number; role: string; content: string; artifacts: Artifact[]; created_at?: string | null };
export type Situation = {
  id: string; rule: string; severity: string; title: string; summary: string;
  recommended_action: string | null; evidence: Evidence[]; status: string;
  created_at: string; resolved_at: string | null; kind: string; choices?: Choice[] | null;
  resolved_choice?: string | null; resolved_by?: string | null; snoozed_until?: string | null;
};
export type Action = {
  id: number; situation_id: string | null; action: string; params: any; status: string;
  detail: string; result: any; requested_by: string; decided_by: string | null; requested_at: string;
};
export type AuditEntry = { actor: string; action: string; target: string; metadata: any; created_at: string };
export type Conn = {
  source: string; connected: boolean; config: any;
  oauth?: boolean;            // this source offers "sign in with…"
  oauth_configured?: boolean; // …and the operator has registered an app for it
};
export type OAuthTarget = { key: string; label: string; private?: boolean };

/* Work items — the records themselves, as opposed to the situations raised
   about them. Facet keys are whatever the data holds (open/closed for GitHub,
   open/fulfilled for an inventory profile); the UI never assumes any of them. */
export type Item = {
  id: string; source: string; type: string; status: string | null;
  title: string; actor: string; timestamp: string; url: string | null; backfilled: boolean;
};
export type Facet = { key: string; count: number };
export type ItemFacets = { sources: Facet[]; types: Facet[]; statuses: Facet[] };
export type ItemsResponse = { items: Item[]; facets: ItemFacets; noun: string };

/* A status is just a string from the source; colour it by meaning where we can
   recognise the shape, else fall back to neutral. Never assume a vocabulary. */
const DONE_WORDS = ["closed", "done", "resolved", "solved", "merged", "fulfilled", "completed", "cancelled", "canceled"];
export function statusColor(status: string | null) {
  if (!status) return "#6a6a6a";
  return DONE_WORDS.includes(status.toLowerCase()) ? "#8b8b8b" : "#3fb950";
}
export type Registry = {
  actions: { name: string; approval_required: boolean; kind: string | null; external_effect?: boolean }[];
  autonomy: { escalation_action?: string; allowed_actions?: string[] };
  policy: { dry_run: boolean };
  terms?: Terms;
};

/* The words THIS business uses, straight from the profile's vocabulary slot.
   A software team calls a flagged item a "flag"; a warehouse calls it a
   "supply risk". The UI should never hardcode either. */
export type Terms = { thing?: string; situation?: string; actor?: string; workspace?: string };

/* What the system knows about this company — the profile, the learned
   baselines, connector health and the safety switch, all in one answer. */
export type Watching = {
  source: string; connected: boolean; enabled: boolean; target: string | null;
  records: number; health: string | null; last_success_at: string | null;
  consecutive_failures: number;
};
export type Norm = {
  metric: string; unit: string; n: number; median: number; mean: number; std: number;
  trend_per_period: number; maturity: string; window_days: number; scope: string;
};
export type Check = { name: string; summary: string; detail?: string };
export type Capability = {
  name: string; external_effect: boolean; approval_required: boolean; autonomous: boolean;
};
export type Understanding = {
  terms: Terms;
  profile_version: number;
  watching: Watching[];
  records: { total: number; by_status: Facet[]; by_type: Facet[] };
  lifecycle: { metric: string; source: string; type: string; finished_when: string | null; unit: string }[];
  learned: Norm[];
  checks: { universal: Check[]; profile: Check[] };
  can_do: Capability[];
  practice_mode: boolean;
};

/* What was learned, with the working shown — every number carries the real
   records that produced it, so it can be checked rather than trusted. */
export type LearnPoint = {
  event_id: string; title: string; hours: number; started: string; finished: string;
  url: string | null; counted: boolean;
};
export type Measurement = {
  metric: string; unit: string; typical: number; spread: number; maturity: string;
  samples: number; trend_per_period: number; alert_above: number;
  finished_when: string | null; measured_from: { source: string; type: string };
  window_days: number; reset_before: string | null; unmeasurable_reason?: string | null;
  points: LearnPoint[];
};
export type Inferred = {
  type: string; source: string; finished_when: string | null;
  timestamp_from: string | null; actor_from: string | null; status_from: string | null;
};
export type Learning = {
  measurements: Measurement[];
  inferred: Inferred[];
  records: { total: number; by_type: Facet[] };
  graph: { things: number; events: number; total: number };
  terms: Terms;
  profile_version: number;
};

/* ---- the company as the system sees it: profile-as-data + the real graph ---- */
export type GraphThing = {
  id: string; thing_type: string | null; title: string | null;
  status: string | null; last_activity: string | null; source: string | null; events: number;
};
export type GraphLink = { src: string; dst: string; type: string; confidence: number };
export type Knowledge = {
  company: { id: string; name?: string; version: number; status: string };
  sources: { source: string; kind: string; enabled: boolean; produces: string[] }[];
  thing_types: { name: string; source: string; from_event: string; count: number }[];
  link_types: string[];
  rhythms: { name: string; type: string; finished_when: string | null }[];
  terms: Terms;
  graph: { things: GraphThing[]; links: GraphLink[] };
};
export type ThingEvent = {
  event_id: string; event_time: string; source: string;
  content?: string; actor_name?: string; type?: string; url?: string | null;
};
export type ThingDetail = {
  thing: GraphThing;
  events: ThingEvent[];
  neighbours: { id: string; thing_type: string | null; title: string | null; status: string | null; type: string; outgoing: boolean }[];
};

/* ---- Workflows: saved agentic plans, n8n-style (Phase 2) ---- */
export type WorkflowTrigger = { type: string; config: Record<string, any> };
export type StepApp = { app: string; does: string };
export type WorkflowStep = {
  tool: string;
  args: Record<string, any>;
  description: string;
  requires_approval: boolean;
  select?: Record<string, any> | null;
  enabled?: boolean;
  app_info?: StepApp; // annotated server-side: which app this node touches + why
};
export type WorkflowPlan = {
  goal: string;
  name: string;
  trigger: WorkflowTrigger;
  steps: WorkflowStep[];
  clarifications: string[];
  apps_used?: StepApp[];
};
export type Workflow = {
  id: number;
  company_id: string;
  name: string;
  goal: string;
  steps: WorkflowStep[];
  trigger: WorkflowTrigger;
  enabled: boolean;
  created_by: string;
  created_at: string | null;
  updated_at: string | null;
  last_run_at: string | null;
  apps_used?: StepApp[];
};

/* An app badge's colour: reuse source colours for connectors, neutral for the
   internal/data pseudo-apps. */
export function appColor(app: string) {
  if (app === "your data") return "#58a6ff";
  if (app === "internal") return "#8b8b8b";
  return sourceColor(app);
}

/* A trigger rendered for humans: "Every morning" / "When a high issue appears". */
export function triggerLabel(t: WorkflowTrigger | undefined): string {
  if (!t) return "Manual";
  if (t.type === "schedule") return String(t.config?.label || t.config?.cron || "On a schedule");
  if (t.type === "event") {
    const bits = [t.config?.severity, t.config?.rule].filter(Boolean).join(" · ");
    return bits ? `When: ${bits}` : "On an event";
  }
  return "Manual — you run it";
}
export type WorkflowStepResult = {
  tool: string;
  args: Record<string, any>;
  status: string;
  detail: string;
  result: Record<string, any>;
};
export type WorkflowRun = {
  id: number;
  workflow_id: number;
  company_id: string;
  status: string;
  trigger: string;
  step_results: WorkflowStepResult[];
  summary: string;
  started_at: string | null;
  finished_at: string | null;
};

/* One vocabulary for run/step status across the Workflows UI. */
export function runStatusColor(status: string) {
  if (status === "done" || status === "executed") return "#3fb950";
  if (status === "dry_run") return "#d29922";
  if (status === "needs_approval" || status === "pending_approval") return "#58a6ff";
  if (status === "failed") return "#f85149";
  return "#8b8b8b"; // recorded / running / other
}

/* Health has three states and they must read differently at a glance. */
export function healthColor(status: string | null | undefined) {
  if (status === "broken") return "#f85149";
  if (status === "degraded") return "#d29922";
  return "#3fb950";
}
export const MATURITY_TEXT: Record<string, string> = {
  insufficient: "not enough examples yet — don't rely on this",
  learning: "still learning — treat as a rough guide",
  stable: "dependable",
  unmeasurable: "cannot be measured from what's connected",
};
export function term(terms: Terms | undefined, key: keyof Terms, fallback: string) {
  return terms?.[key]?.trim() || fallback;
}
export function plural(word: string) {
  return /s$/i.test(word) ? word : `${word}s`;
}
/* The noun is profile data, so "a"/"an" can't be written into the copy —
   "a issue" reads as broken to every user who sees it. */
export function article(word: string) {
  return /^[aeiou]/i.test(word.trim()) ? "an" : "a";
}
export type Briefing = {
  mode: string; headline: string;
  stats: { events: number; active_situations: number; high_risk: number; pending_approvals: number; sources_connected: number };
  events: { source: string; type: string; count: number; latest: string | null }[];
};

/* ------------------------------ helpers ------------------------------ */

export async function api(path: string, init?: RequestInit) {
  const res = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!res.ok) {
    let msg = `API ${res.status}`;
    try {
      const j = await res.json();
      if (j.detail) msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail);
    } catch {}
    throw new Error(msg);
  }
  return res.json();
}

export function timeHM(iso?: string | null) {
  if (!iso) return "";
  const d = new Date(iso);
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

export function dayTime(iso?: string | null) {
  if (!iso) return "";
  const d = new Date(iso);
  const day = d.toLocaleDateString(undefined, { weekday: "short" });
  return `${day} ${timeHM(iso)}`;
}

export function relativeTime(iso?: string | null) {
  if (!iso) return "";
  const diff = Date.now() - new Date(iso).getTime();
  const m = Math.round(diff / 60000);
  if (m < 1) return "now";
  if (m < 60) return `${m}m ago`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.round(h / 24)}d ago`;
}

export function humanize(s: string) {
  return s.replace(/[_.]/g, " ");
}

export function firstLine(s: string, max = 84) {
  const line = (s || "").trim().split(/\r?\n/)[0];
  return line.length > max ? line.slice(0, max - 1) + "…" : line;
}

/* -------------------------------- atoms -------------------------------- */

export function CheckIcon({ size = 14 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 16 16">
      <circle cx="8" cy="8" r="7" fill="#3fb950" />
      <path d="M5 8.2l2 2 4-4.4" stroke="#0a0a0a" strokeWidth="1.6" fill="none" />
    </svg>
  );
}

export function Dot({ color, size = 8 }: { color: string; size?: number }) {
  return (
    <span
      className="inline-block flex-none rounded-full"
      style={{ width: size, height: size, background: color }}
    />
  );
}

export function CardLabel({ color, children }: { color: string; children: React.ReactNode }) {
  return (
    <div className="mb-[9px] flex items-center gap-2">
      <Dot color={color} />
      <span className="text-[11px] font-semibold tracking-[1px]" style={{ color }}>
        {children}
      </span>
    </div>
  );
}

export function SourceDots({ sources }: { sources: string[] }) {
  if (!sources.length) return null;
  const uniq = Array.from(new Set(sources));
  return (
    <div className="mt-[10px] flex items-center gap-1.5 whitespace-nowrap text-[12px] text-subtle">
      {uniq.map((s) => (
        <Dot key={s} color={sourceColor(s)} size={6} />
      ))}
      <span className="ml-0.5 capitalize">{uniq.join(" · ")}</span>
    </div>
  );
}

export function FilledBtn({ onClick, disabled, children }: {
  onClick?: () => void; disabled?: boolean; children: React.ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      className="flex-none whitespace-nowrap rounded-md border-none bg-accent px-3.5 py-[7px] text-[13px] font-semibold text-[#0a0a0a] hover:opacity-90 disabled:opacity-50"
    >
      {children}
    </button>
  );
}

export function OutlineBtn({ onClick, disabled, children }: {
  onClick?: () => void; disabled?: boolean; children: React.ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      className="flex-none whitespace-nowrap rounded-md border border-edgeStrong bg-transparent px-[13px] py-[7px] text-[13px] text-ink hover:bg-elevated disabled:opacity-50"
    >
      {children}
    </button>
  );
}

export function TextBtn({ onClick, children }: { onClick?: () => void; children: React.ReactNode }) {
  return (
    <button
      onClick={onClick}
      className="flex-none whitespace-nowrap border-none bg-transparent p-0 text-[13px] text-muted hover:text-ink"
    >
      {children}
    </button>
  );
}

export function DoneChip({ children = "Done ✓" }: { children?: React.ReactNode }) {
  return (
    <span className="inline-flex flex-none items-center gap-[7px] whitespace-nowrap rounded-md border border-accent px-[13px] py-1.5 text-[13px] font-semibold text-accent">
      {children}
    </span>
  );
}

export function Card({ dimmed, children }: { dimmed?: boolean; children: React.ReactNode }) {
  return (
    <div
      className="card-in rounded-lg border border-edge bg-panel px-[18px] py-4"
      style={{ opacity: dimmed ? 0.55 : 1 }}
    >
      {children}
    </div>
  );
}

/* ------------------------- markdown, the small subset -------------------------
   Models answer in markdown. Rendering it as raw text put literal `**` on
   screen, which reads as broken. A dependency would need a container rebuild
   for four constructs, so this handles exactly what an answer uses: bold,
   inline code, bullets and numbered lists. Everything is built as React
   nodes — no dangerouslySetInnerHTML, so model output can never inject markup. */

function inline(text: string, keyBase: string): React.ReactNode[] {
  const out: React.ReactNode[] = [];
  // split on **bold** and `code`, keeping the delimiters
  const parts = text.split(/(\*\*[^*]+\*\*|`[^`]+`)/g);
  parts.forEach((part, i) => {
    if (!part) return;
    if (part.startsWith("**") && part.endsWith("**")) {
      out.push(<strong key={`${keyBase}-b${i}`} className="font-semibold text-ink">{part.slice(2, -2)}</strong>);
    } else if (part.startsWith("`") && part.endsWith("`")) {
      out.push(
        <code key={`${keyBase}-c${i}`} className="rounded border border-edge bg-elevated px-1 py-[1px] font-mono text-[12.5px]">
          {part.slice(1, -1)}
        </code>,
      );
    } else {
      out.push(<span key={`${keyBase}-t${i}`}>{part}</span>);
    }
  });
  return out;
}

export function Markdown({ text }: { text: string }) {
  // models sometimes emit "- " bullets glued onto the previous sentence
  const normalized = text.replace(/\s+-\s+(?=\S)/g, "\n- ");
  const lines = normalized.split(/\r?\n/);
  const blocks: React.ReactNode[] = [];
  let bullets: string[] = [];

  const flush = () => {
    if (!bullets.length) return;
    blocks.push(
      <ul key={`ul-${blocks.length}`} className="flex list-disc flex-col gap-1 pl-[18px]">
        {bullets.map((b, i) => <li key={i}>{inline(b, `li${blocks.length}-${i}`)}</li>)}
      </ul>,
    );
    bullets = [];
  };

  lines.forEach((raw, i) => {
    const line = raw.trim();
    if (!line) { flush(); return; }
    const bullet = line.match(/^(?:[-*•]|\d+[.)])\s+(.*)$/);
    if (bullet) { bullets.push(bullet[1]); return; }
    flush();
    blocks.push(<p key={`p-${i}`}>{inline(line.replace(/^#+\s*/, ""), `p${i}`)}</p>);
  });
  flush();

  return <div className="flex flex-col gap-2">{blocks}</div>;
}

export function EvidenceRow({ e }: { e: Evidence }) {
  return (
    <div className="flex items-baseline gap-2.5 py-1.5">
      <span className="relative -top-px flex-none">
        <Dot color={sourceColor(e.source)} size={7} />
      </span>
      <span className="w-11 flex-none font-mono text-[11.5px] text-subtle">{timeHM(e.timestamp)}</span>
      <span className="text-[13px] leading-normal text-ink">
        {firstLine(e.excerpt)}
        {e.url && (
          <a href={e.url} target="_blank" rel="noreferrer" className="ml-1.5 font-mono text-[11px]">
            ↗
          </a>
        )}
      </span>
    </div>
  );
}



