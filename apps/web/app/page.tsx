"use client";

import { useCallback, useEffect, useState } from "react";
import {
  Action, Artifact, AuditEntry, Briefing, CheckIcon, Conn, Dot, Item, ItemFacets, Knowledge,
  Learning, OAuthTarget, Registry, Situation, Understanding, Workflow, api, COMPANY, healthColor,
  humanize, relativeTime,
} from "./lib";
import ActivityView from "./views/activity";
import AgentView, { Msg } from "./views/agent";
import ConnectionsView from "./views/connections";
import FeedView from "./views/feed";
import HomeView from "./views/home";
import KnowledgeView from "./views/business-profile";
import WorkflowsView from "./views/workflows";

/* "Knowledge" is a VIEWER over real data, not a status page in prose: the
   graph it actually built from your events, and the profile it inferred,
   both clickable down to the source record. The working behind any single
   number opens in a modal rather than owning the screen. Anything else a
   status page would say, you ask the agent, which can also CHANGE the
   answer. The two always-on indicators are the ones you must never have to
   ask about: is data arriving, and can this thing touch my real systems. */
type Screen = "home" | "agent" | "workflows" | "attention" | "work" | "knowledge" | "activity" | "connections";

/* ------------------------------ sidebar icons ------------------------------ */

const ICONS: Record<Screen, React.ReactNode> = {
  home: (
    <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round">
      <path d="M2.5 7 8 2.5 13.5 7v6.5h-4v-4h-3v4h-4z" />
    </svg>
  ),
  agent: (
    <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round">
      <path d="M3 5a2 2 0 0 1 2-2h6a2 2 0 0 1 2 2v4a2 2 0 0 1-2 2H8l-3.2 2.6V11H5a2 2 0 0 1-2-2z" />
    </svg>
  ),
  workflows: (
    <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="4" cy="4" r="1.6" />
      <circle cx="4" cy="12" r="1.6" />
      <circle cx="12" cy="8" r="1.6" />
      <path d="M5.6 4H9a1.5 1.5 0 0 1 1.5 1.5v.8M5.6 12H9a1.5 1.5 0 0 0 1.5-1.5v-.8" />
    </svg>
  ),
  attention: (
    <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round">
      <path d="M2 9.5 4 4h8l2 5.5V13H2z" />
      <path d="M2 9.5h3.5l1 1.8h3l1-1.8H14" />
    </svg>
  ),
  work: (
    <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round">
      <path d="M2.5 4.5h11v8h-11zM5 4.5V3h6v1.5M5 7h6M5 10h4" />
    </svg>
  ),
  knowledge: (
    <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="4" cy="4" r="2" />
      <circle cx="12" cy="6" r="2" />
      <circle cx="7" cy="12.5" r="2" />
      <path d="M5.7 5.2 10.3 5M5.2 5.8 6.2 10.6M10.9 7.7 8.4 11.2" />
    </svg>
  ),
  activity: (
    <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round">
      <circle cx="8" cy="8" r="6" />
      <path d="M8 4.8V8l2.2 1.8" />
    </svg>
  ),
  connections: (
    <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round">
      <path d="M6 2v3M10 2v3" />
      <path d="M5 5h6v2.5a3 3 0 0 1-6 0z" />
      <path d="M8 10.5V14" />
    </svg>
  ),
};

export default function Home() {
  const [screen, setScreen] = useState<Screen>("home");
  const [briefing, setBriefing] = useState<Briefing | null>(null);
  const [situations, setSituations] = useState<Situation[] | null>(null);
  const [actions, setActions] = useState<Action[] | null>(null);
  const [registry, setRegistry] = useState<Registry | null>(null);
  const [audit, setAudit] = useState<AuditEntry[] | null>(null);
  const [conns, setConns] = useState<Conn[]>([]);
  const [messages, setMessages] = useState<Msg[]>([]);
  const [items, setItems] = useState<Item[]>([]);
  const [itemFacets, setItemFacets] = useState<ItemFacets | null>(null);
  const [itemNoun, setItemNoun] = useState("item");
  const [itemFilters, setItemFilters] = useState<{ source?: string; type?: string; status?: string }>({});
  const [understanding, setUnderstanding] = useState<Understanding | null>(null);
  const [learning, setLearning] = useState<Learning | null>(null);
  const [knowledge, setKnowledge] = useState<Knowledge | null>(null);
  const [workflows, setWorkflows] = useState<Workflow[]>([]);
  const [agentMode, setAgentMode] = useState<"chat" | "workflow">("chat");
  /* "This looks wrong" hands you to the agent with the sentence started, so
     disagreeing costs one click instead of knowing what to type. */
  const [prefill, setPrefill] = useState<string | null>(null);
  const [targets, setTargets] = useState<Record<string, OAuthTarget[]>>({});
  const [dismissed, setDismissed] = useState<Set<string>>(new Set());
  const [handled, setHandled] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [tourStep, setTourStep] = useState(0);

  const flash = (m: string) => {
    setToast(m);
    setTimeout(() => setToast(null), 3400);
  };

  /* ------------------------------ data loading ------------------------------ */

  const loadAll = useCallback(async () => {
    const grab = async <T,>(p: Promise<T>, fallback: T): Promise<T> => {
      try { return await p; } catch { return fallback; }
    };
    const [b, f, r, au, c, m, u, lrn, kn, wfs] = await Promise.all([
      grab(api(`/api/briefing?company_id=${COMPANY}`), null),
      grab(api(`/api/feed?company_id=${COMPANY}`), { situations: [], actions: [] }),
      grab(api(`/api/actions/registry?company_id=${COMPANY}`), null),
      grab(api(`/api/audit?company_id=${COMPANY}&limit=80`), { entries: [] }),
      grab(api(`/api/connections?company_id=${COMPANY}`), { connections: [] }),
      grab(api(`/api/conversations/default/messages?company_id=${COMPANY}`), { messages: [] }),
      grab(api(`/api/understanding?company_id=${COMPANY}`), null),
      grab(api(`/api/learning?company_id=${COMPANY}`), null),
      grab(api(`/api/knowledge?company_id=${COMPANY}`), null),
      grab(api(`/api/workflows?company_id=${COMPANY}`), { workflows: [] }),
    ]);
    setBriefing(b);
    setSituations(f.situations);
    setActions(f.actions);
    setRegistry(r);
    setAudit(au.entries);
    setConns(c.connections);
    setUnderstanding(u);
    setLearning(lrn);
    setKnowledge(kn);
    setWorkflows(wfs.workflows ?? []);
    const serverMsgs: Msg[] = (m.messages ?? []).map((x: any) => ({
      id: `server:${x.id}`, kind: x.role === "user" ? "user" : "agent", text: x.content, artifacts: x.artifacts ?? [],
    }));
    setMessages((prev) => {
      const seen = new Set(prev.map((x) => x.id).filter(Boolean));
      return [...prev, ...serverMsgs.filter((x) => !x.id || !seen.has(x.id))];
    });
  }, []);

  useEffect(() => {
    void loadAll();
    const t = setInterval(() => void loadAll(), 30000);
    return () => clearInterval(t);
  }, [loadAll]);

  /* The work list is fetched separately from the feed because it re-queries
     whenever a facet chip is clicked — the server does the counting, so the
     numbers are always the truth rather than something the UI tallied. */
  const loadItems = useCallback(async () => {
    const q = new URLSearchParams({ company_id: COMPANY });
    if (itemFilters.source) q.set("source", itemFilters.source);
    if (itemFilters.type) q.set("type", itemFilters.type);
    if (itemFilters.status) q.set("status", itemFilters.status);
    try {
      const r = await api(`/api/items?${q.toString()}`);
      setItems(r.items ?? []);
      setItemFacets(r.facets ?? null);
      setItemNoun(r.noun ?? "item");
    } catch {
      setItemFacets((f) => f ?? { sources: [], types: [], statuses: [] });
    }
  }, [itemFilters]);

  useEffect(() => { void loadItems(); }, [loadItems]);

  /* Live arrival: the watcher engine (every 5 min, or right after a webhook
     or a manual scan) publishes one "the feed changed" nudge per pass over
     Redis pub/sub; this SSE connection forwards it, and we just refetch —
     the 30s poll above stays as a fallback if the stream ever drops. */
  useEffect(() => {
    const es = new EventSource(`/api/feed/stream?company_id=${COMPANY}`);
    es.onmessage = () => void loadAll();
    return () => es.close();
  }, [loadAll]);

  useEffect(() => {
    if (typeof window === "undefined") return;
    if (!localStorage.getItem("markos-tour-done")) {
      const t = setTimeout(() => setTourStep(1), 1000);
      return () => clearTimeout(t);
    }
  }, []);

  useEffect(() => {
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") endTour(); };
    window.addEventListener("keydown", esc);
    return () => window.removeEventListener("keydown", esc);
  });

  const endTour = () => {
    setTourStep(0);
    if (typeof window !== "undefined") localStorage.setItem("markos-tour-done", "1");
  };

  /* -------------------------------- handlers -------------------------------- */

  async function runMove(move: string, situationId: string) {
    setBusy(`move:${move}`);
    try {
      const r = await api(`/api/actions?company_id=${COMPANY}`, {
        method: "POST",
        body: JSON.stringify({ action: move, situation_id: situationId, params: {}, requested_by: "ui" }),
      });
      if (r.status === "pending_approval") {
        flash(`${humanize(move)} prepared — approve it on the Feed`);
      } else {
        setHandled((h) => ({ ...h, [situationId]: move }));
        flash(`${humanize(move)}: ${humanize(r.status)} — logged in Activity`);
      }
      await loadAll();
    } catch (e: any) { flash(e.message); } finally { setBusy(null); }
  }

  async function decide(actionId: number, verdict: "approve" | "reject") {
    setBusy(`decide:${actionId}`);
    try {
      const r = await api(`/api/actions/${actionId}/${verdict}?company_id=${COMPANY}`, { method: "POST" });
      flash(verdict === "approve" ? `Approved — ${humanize(r.status)}` : "Discarded");
      await loadAll();
    } catch (e: any) { flash(e.message); } finally { setBusy(null); }
  }

  async function resolveClarification(situationId: string, choiceId: string) {
    setBusy(`clarification:${situationId}`);
    try {
      const r = await api(`/api/situations/${encodeURIComponent(situationId)}/resolve?company_id=${COMPANY}`, {
        method: "POST",
        body: JSON.stringify({ choice: choiceId, by: "ui" }),
      });
      const label = r.choice?.label ?? "choice";
      setHandled((h) => ({ ...h, [situationId]: label }));
      flash(`${label} recorded`);
      await loadAll();
    } catch (e: any) { flash(e.message); } finally { setBusy(null); }
  }
  async function dismiss(situationId: string) {
    setDismissed((d) => new Set(d).add(situationId)); // instant UI feedback
    try {
      await api(`/api/situations/${encodeURIComponent(situationId)}/dismiss?company_id=${COMPANY}`, {
        method: "POST",
      });
    } catch (e: any) {
      flash(`Dismiss not saved: ${e.message}`);
    }
  }

  /* The ONE place a chat turn is written — every message on screen, from
     either side, exists in the database. No local-only chat state. */
  async function say(role: "user" | "agent", text: string, artifacts?: Artifact[]) {
    try {
      const saved = await api(`/api/conversations/default/messages?company_id=${COMPANY}`, {
        method: "POST",
        body: JSON.stringify({ role, content: text, artifacts: artifacts ?? [] }),
      });
      setMessages((m) => [...m, { id: `server:${saved.id}`, kind: role, text, artifacts }]);
    } catch (e: any) {
      // never silently drop what was typed/said just because persistence hiccuped
      setMessages((m) => [...m, { kind: role, text, artifacts }]);
      flash(`Message not saved: ${e.message}`);
    }
  }

  function discuss(topic: { chip: string; text: string }) {
    // the context chip is a local navigation breadcrumb (which card sent you
    // here), not a conversational turn — it stays UI-only, unlike the reply
    setMessages((m) => [...m, { kind: "context", text: topic.chip }]);
    void say("agent", topic.text);
    setScreen("agent");
  }

  /* CP4: one turn of the REAL tool-use agent. The backend persists both
     sides and may take an action (through the approval brake), so after the
     reply lands we refresh the feed. The optimistic user bubble is given its
     real server id on success, so the 30s poll never double-renders it. */
  async function askAgent(text: string) {
    const localId = `local:${Date.now()}`;
    setMessages((m) => [...m, { id: localId, kind: "user", text }]);
    try {
      const r = await api(`/api/agent/chat?company_id=${COMPANY}`, {
        method: "POST",
        body: JSON.stringify({ message: text }),
      });
      setMessages((m) =>
        m
          .map((x) => (x.id === localId ? { ...x, id: `server:${r.user_message.id}` } : x))
          .concat([{ id: `server:${r.message.id}`, kind: "agent", text: r.reply, artifacts: r.artifacts ?? [] }]),
      );
      await loadAll(); // the agent may have queued an action — reflect it
    } catch (e: any) {
      setMessages((m) => m.filter((x) => x.id !== localId)); // roll back the bubble
      flash(`Agent error: ${e.message}`);
      throw e;
    }
  }

  /* Workflow authored IN the chat: the composer's workflow button sends the
     goal here. We show the user's goal, then render the compiled plan inline
     as an n8n graph the user can Save or Run — a draft, so it lives only in the
     thread until saved (a refresh drops an unsaved draft, which is correct). */
  async function createWorkflowInChat(goal: string) {
    setMessages((m) => [...m, { kind: "user", text: goal }]);
    try {
      const plan = await api(`/api/workflows/plan?company_id=${COMPANY}`, {
        method: "POST", body: JSON.stringify({ goal }),
      });
      const lead = plan.clarifications?.length
        ? "I need one thing before I can build this:"
        : "Here's the workflow I'd build:";
      setMessages((m) => [...m, { kind: "agent", text: lead, artifacts: [{ type: "workflow_plan", plan }] }]);
    } catch (e: any) {
      flash(`Couldn't build that: ${e.message}`);
      throw e;
    }
  }

  async function saveWorkflowPlan(plan: any) {
    await api(`/api/workflows?company_id=${COMPANY}`, {
      method: "POST",
      body: JSON.stringify({ name: plan.name, goal: plan.goal, trigger: plan.trigger, steps: plan.steps }),
    });
    flash(`Saved "${plan.name}" to Workflows`);
    await loadAll();
  }

  async function runWorkflowPlan(plan: any) {
    const saved = await api(`/api/workflows?company_id=${COMPANY}`, {
      method: "POST",
      body: JSON.stringify({ name: plan.name, goal: plan.goal, trigger: plan.trigger, steps: plan.steps }),
    });
    const r = await api(`/api/workflows/${saved.id}/run?company_id=${COMPANY}`, { method: "POST" });
    flash(`${plan.name}: ${r.summary}`);
    await loadAll();
  }

  async function connect(source: string, token: string, config: any) {
    setBusy(`connect:${source}`);
    try {
      await api(`/api/connections/${source}?company_id=${COMPANY}`, {
        method: "POST", body: JSON.stringify({ token, ...config }),
      });
      flash(`${source} connected — reading your history`);
      await loadAll();
    } catch (e: any) { flash(e.message); } finally { setBusy(null); }
  }

  async function disconnect(source: string) {
    try {
      await api(`/api/connections/${source}?company_id=${COMPANY}`, { method: "DELETE" });
      flash(`${source} disconnected`);
      await loadAll();
    } catch (e: any) { flash(e.message); }
  }

  /* OAuth: hand the browser to the API's start endpoint, which mints a
     single-use state and bounces on to the provider. A full navigation (not
     fetch) because the human has to see and approve on the provider's own
     page — that's the entire point of the flow. */
  function startOAuth(source: string) {
    window.location.href = `/api/oauth/${source}/start?company_id=${COMPANY}`;
  }

  const loadTargets = useCallback(async (source: string) => {
    try {
      const r = await api(`/api/oauth/${source}/targets?company_id=${COMPANY}`);
      setTargets((t) => ({ ...t, [source]: r.targets ?? [] }));
    } catch {
      /* not connected via OAuth, or the token can't list — leave the picker off */
    }
  }, []);

  async function pickTarget(source: string, key: string) {
    if (!key) return;
    setBusy(`connect:${source}`);
    try {
      // reuse the normal connect path: token stays as-is server-side, we only
      // set which repo/channel to watch
      await api(`/api/connections/${source}?company_id=${COMPANY}`, {
        method: "POST",
        body: JSON.stringify({ repo: key }),
      });
      flash(`Watching ${key}`);
      await loadAll();
    } catch (e: any) { flash(e.message); } finally { setBusy(null); }
  }

  /* Coming back from the provider: say what happened, then clean the query
     string so a refresh doesn't re-toast a stale result. */
  useEffect(() => {
    if (typeof window === "undefined") return;
    const params = new URLSearchParams(window.location.search);
    const connected = params.get("connected");
    const err = params.get("connect_error");
    if (!connected && !err) return;
    if (connected) {
      flash(`${connected} connected — choose what to watch`);
      void loadTargets(connected);
      setScreen("connections");
    } else if (err) {
      flash(`Connection failed: ${err}`);
      setScreen("connections");
    }
    window.history.replaceState({}, "", window.location.pathname);
  }, [loadTargets]);

  async function sync(source: string) {
    setBusy(`sync:${source}`);
    try {
      const d = await api(`/api/connections/${source}/sync?company_id=${COMPANY}`, { method: "POST" });
      const n = Object.values(d.enqueued as Record<string, number>).reduce((x, y) => x + y, 0);
      flash(`Pulled ${n} events — linking them now`);
      setTimeout(() => void loadAll(), 4000);
    } catch (e: any) { flash(e.message); } finally { setBusy(null); }
  }

  /* Practice mode is the single most important thing to see: while it is on,
     nothing the AI does reaches your real systems.

     Going LIVE takes two deliberate clicks on two different labels, and the
     armed state times out. A native confirm() was not enough: it is auto-
     accepted by automation, and a single stray click on a button sitting in
     the chrome flipped this company to live twice — once while the engine
     went on to write real labels to a real repository. A destructive switch
     should not be one click away from a mis-tap. */
  const [armLive, setArmLive] = useState(false);
  useEffect(() => {
    if (!armLive) return;
    const t = setTimeout(() => setArmLive(false), 5000);
    return () => clearTimeout(t);
  }, [armLive]);

  async function setPracticeMode(on: boolean) {
    if (!on && !armLive) {
      setArmLive(true); // first click only arms it
      return;
    }
    setArmLive(false);
    setBusy("practice");
    try {
      await api(`/api/settings/dry_run?company_id=${COMPANY}`, {
        method: "POST",
        body: JSON.stringify({ enabled: on }),
      });
      flash(on ? "Practice mode on — nothing will be sent" : "Live mode — actions now affect your real tools");
      await loadAll();
    } catch (e: any) { flash(e.message); } finally { setBusy(null); }
  }

  async function scan() {
    setBusy("scan");
    try {
      const d = await api(`/api/analyze?company_id=${COMPANY}`, { method: "POST" });
      flash(`Scan done — ${d.situations} situation${d.situations === 1 ? "" : "s"} live`);
      await loadAll();
      setScreen("attention");
    } catch (e: any) { flash(e.message); } finally { setBusy(null); }
  }

  /* --------------------------------- derived --------------------------------- */

  const liveCount =
    (situations ?? []).filter((s) => s.status !== "resolved" && !dismissed.has(s.id) && !handled[s.id]).length +
    (actions ?? []).filter((a) => a.status === "pending_approval").length;
  const eventsTotal = briefing?.stats.events ?? 0;
  const connectedCount = conns.filter((c) => c.connected).length;
  const eventsBySource: Record<string, number> = {};
  for (const e of briefing?.events ?? []) {
    eventsBySource[e.source] = (eventsBySource[e.source] ?? 0) + e.count;
  }

  const NavBtn = ({ k, label, badge }: { k: Screen; label: string; badge?: number }) => (
    <button
      onClick={() => setScreen(k)}
      className="relative flex w-full items-center justify-center gap-[9px] rounded-md border-none px-2.5 py-2 text-left text-[13px] font-medium hover:text-ink sm:justify-start"
      style={{
        background: screen === k ? "#161616" : "transparent",
        color: screen === k ? "#e8e8e8" : "#8b8b8b",
      }}
    >
      {ICONS[k]}
      <span className="hidden sm:inline">{label}</span>
      {badge ? (
        <span className="absolute right-0 top-0 inline-flex h-[15px] min-w-[15px] items-center justify-center rounded-full bg-accent px-1 text-[9px] font-bold text-[#0a0a0a] sm:static sm:ml-auto sm:h-[17px] sm:min-w-[17px] sm:px-[5px] sm:text-[10.5px]">
          {badge}
        </span>
      ) : null}
    </button>
  );

  return (
    <div className="flex h-screen overflow-hidden bg-canvas text-ink">
      {/* ============ SIDEBAR ============ */}
      <div className="flex w-[62px] flex-none flex-col border-r border-edge bg-panel px-1.5 pb-3.5 pt-4 sm:w-40 sm:px-2.5">
        <div className="flex items-center justify-center gap-2 px-2.5 pb-[18px] sm:justify-start">
          <Dot color="#3fb950" size={7} />
          <span className="hidden text-[13.5px] font-semibold tracking-[0.2px] sm:inline">MarkOS</span>
        </div>

        <nav className="flex flex-col gap-0.5">
          <NavBtn k="home" label="Home" />
          <div className="relative">
            <NavBtn k="agent" label="Agent" />
            {tourStep === 1 && (
              <div className="absolute -top-1.5 left-[calc(100%+14px)] z-30 w-[226px] rounded-lg border border-accent bg-elevated px-3.5 py-3">
                <div className="font-mono text-[10.5px] text-accent">1 / 3</div>
                <div className="my-1.5 mb-2.5 text-[12.5px] leading-normal text-ink">
                  Talk to your agent — ask anything, or say what to watch.
                </div>
                <div className="flex items-center gap-2.5">
                  <button
                    onClick={() => { setTourStep(2); setScreen("attention"); }}
                    className="flex-none whitespace-nowrap rounded-[5px] bg-accent px-[11px] py-[5px] text-[12px] font-semibold text-[#0a0a0a] hover:opacity-90"
                  >
                    Next
                  </button>
                  <button onClick={endTour} className="flex-none border-none bg-transparent p-0 text-[12px] text-subtle hover:text-muted">
                    Skip
                  </button>
                </div>
              </div>
            )}
          </div>
          <NavBtn k="workflows" label="Workflows" />
          <NavBtn k="attention" label="Attention" badge={liveCount || undefined} />
          <NavBtn k="work" label="Work" />
          <NavBtn k="knowledge" label="Business profile" />
          <NavBtn k="activity" label="Activity" />
          <NavBtn k="connections" label="Connections" />
        </nav>

        <div className="mt-auto hidden flex-col gap-2 px-2.5 sm:flex">
          {/* Is each connection actually alive? Without this, a dead
              connection just looks like a quiet week. */}
          {(understanding?.watching ?? []).filter((w) => w.connected).map((w) => (
            <button
              key={w.source}
              onClick={() => { setScreen("agent"); void askAgent(`What do you know about our ${w.source} connection?`); }}
              className="flex items-center gap-[7px] border-none bg-transparent p-0 text-left text-[11.5px] text-subtle hover:text-muted"
              title={w.last_success_at ? `last heard ${relativeTime(w.last_success_at)}` : "no data yet"}
            >
              <Dot color={healthColor(w.health)} size={6} />
              <span className="capitalize">{w.source}</span>
              <span className="text-subtle">
                {w.last_success_at ? relativeTime(w.last_success_at) : "—"}
              </span>
            </button>
          ))}
          <div className="flex items-center gap-[7px] text-[11.5px] text-subtle">
            <Dot color="#3fb950" size={6} />
            <span>{eventsTotal.toLocaleString()} events watched</span>
          </div>
          <button
            onClick={scan}
            disabled={busy === "scan"}
            className="self-start border-none bg-transparent p-0 text-[11.5px] text-subtle hover:text-ink disabled:opacity-50"
          >
            {busy === "scan" ? "Scanning…" : "Scan now"}
          </button>
        </div>
      </div>

      {/* ============ MAIN ============ */}
      <div className="flex min-w-0 flex-1 flex-col">
        {/* Safety state, always visible. You should never have to wonder
            whether this thing can touch your real systems. */}
        {understanding && (
          <div
            className="flex flex-none flex-wrap items-center gap-x-3 gap-y-1 border-b px-7 py-2 text-[12px]"
            style={{
              borderColor: understanding.practice_mode ? "#3a3320" : "#242424",
              background: understanding.practice_mode ? "rgba(210,153,34,0.07)" : "transparent",
            }}
          >
            <span className="flex items-center gap-[7px]">
              <Dot color={understanding.practice_mode ? "#d29922" : "#3fb950"} size={6} />
              <span style={{ color: understanding.practice_mode ? "#d29922" : "#8b8b8b" }}>
                {understanding.practice_mode
                  ? "Practice mode — nothing is sent to your real tools"
                  : "Live — approved actions really change your tools"}
              </span>
            </span>
            <button
              onClick={() => setPracticeMode(!understanding.practice_mode)}
              disabled={busy === "practice"}
              className="border-none bg-transparent p-0 text-[12px] underline disabled:opacity-50"
              style={{ color: armLive ? "#f85149" : undefined }}
            >
              {busy === "practice"
                ? "saving…"
                : !understanding.practice_mode
                  ? "back to practice"
                  : armLive
                    ? "click again to confirm — this writes to your real tools"
                    : "go live"}
            </button>
            {armLive && (
              <button
                onClick={() => setArmLive(false)}
                className="border-none bg-transparent p-0 text-[12px] text-muted underline hover:text-ink"
              >
                cancel
              </button>
            )}
          </div>
        )}

        {screen === "home" && (
          <div className="min-h-0 flex-1 overflow-y-auto overflow-x-hidden">
            <HomeView
              briefing={briefing}
              situations={situations ?? []}
              actions={actions ?? []}
              understanding={understanding}
              learning={learning}
              knowledge={knowledge}
              onGoAttention={() => setScreen("attention")}
              onGoWork={() => setScreen("work")}
              onGoProfile={() => setScreen("knowledge")}
              onGoConnections={() => setScreen("connections")}
              onAsk={(text) => { setScreen("agent"); setPrefill(text); }}
            />
          </div>
        )}
        {screen === "agent" && (
          <AgentView
            messages={messages}
            onAsk={askAgent}
            onResolveClarification={resolveClarification}
            prefill={prefill}
            onPrefillUsed={() => setPrefill(null)}
            mode={agentMode}
            onModeChange={setAgentMode}
            onCreateWorkflow={createWorkflowInChat}
            onSaveWorkflowPlan={saveWorkflowPlan}
            onRunWorkflowPlan={runWorkflowPlan}
          />
        )}
        {screen === "workflows" && (
          <div className="min-h-0 flex-1 overflow-y-auto overflow-x-hidden">
            <WorkflowsView
              workflows={workflows}
              onChanged={loadAll}
              onNewWorkflow={() => { setAgentMode("workflow"); setScreen("agent"); }}
            />
          </div>
        )}
        {(screen === "attention" || screen === "work") && (
          <div className="min-h-0 flex-1 overflow-y-auto overflow-x-hidden">
            <FeedView
              mode={screen === "work" ? "work" : "attention"}
              situations={situations}
              actions={actions}
              registry={registry}
              dismissed={dismissed}
              handled={handled}
              eventsToday={eventsTotal}
              connectedCount={connectedCount}
              items={items}
              itemFacets={itemFacets}
              itemNoun={itemNoun}
              itemFilters={itemFilters}
              onFilterItems={setItemFilters}
              onRunMove={runMove}
              onDecide={decide}
              onResolveClarification={resolveClarification}
              onDismiss={dismiss}
              onDiscuss={discuss}
              onGoConnections={() => setScreen("connections")}
              onGoWork={() => setScreen("work")}
              tourStep={tourStep}
              onTourNext={() => setTourStep(3)}
              onTourSkip={endTour}
            />
          </div>
        )}
        {screen === "knowledge" && (
          <div className="min-h-0 flex-1 overflow-y-auto overflow-x-hidden">
            <KnowledgeView
              knowledge={knowledge}
              learning={learning}
              understanding={understanding}
              situations={situations ?? []}
              onAsk={(text) => { setScreen("agent"); setPrefill(text); }}
            />
          </div>
        )}
        {screen === "activity" && (
          <div className="min-h-0 flex-1 overflow-y-auto overflow-x-hidden">
            <ActivityView entries={audit} />
          </div>
        )}
        {screen === "connections" && (
          <div className="min-h-0 flex-1 overflow-y-auto overflow-x-hidden">
            <ConnectionsView
              conns={conns}
              eventsBySource={eventsBySource}
              busy={busy}
              onConnect={connect}
              onDisconnect={disconnect}
              onSync={sync}
              onOAuth={startOAuth}
              onPickTarget={pickTarget}
              targets={targets}
            />
          </div>
        )}
      </div>

      {/* ============ TOAST ============ */}
      {toast && (
        <div className="toast-in fixed bottom-6 left-[calc(50%+80px)] z-50 flex -translate-x-1/2 items-center gap-[9px] rounded-lg border border-edge bg-elevated px-4 py-2.5 text-[13px] text-ink">
          <CheckIcon />
          <span>{toast}</span>
        </div>
      )}
    </div>
  );
}










