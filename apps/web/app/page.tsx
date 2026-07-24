"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  Action, Artifact, AuditEntry, Briefing, CheckIcon, Conn, Dot, Item, ItemFacets, Knowledge,
  Learning, OAuthTarget, Registry, ReviewFinding, Situation, Thread, Understanding, Workflow, WorkflowPlanArtifact,
  api, healthColor, humanize, relativeTime, streamPost, NotSignedIn, Session,
} from "./lib";
import ActivityView from "./views/activity";
import AuthView from "./views/auth";
import TeamView from "./views/team";
import AutonomyView from "./views/autonomy";
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
type Screen = "home" | "agent" | "workflows" | "attention" | "work" | "knowledge" | "activity" | "connections" | "team" | "autonomy";

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
  team: (
    <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="6" cy="6" r="2.3" />
      <path d="M2.5 13c0-2 1.6-3.4 3.5-3.4S9.5 11 9.5 13" />
      <circle cx="11.5" cy="6.5" r="1.8" />
      <path d="M11 9.7c1.6 0 2.5 1.2 2.5 2.8" />
    </svg>
  ),
  autonomy: (
    <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="8" cy="8" r="2" />
      <path d="M8 1.5v2M8 12.5v2M1.5 8h2M12.5 8h2M3.4 3.4l1.4 1.4M11.2 11.2l1.4 1.4M12.6 3.4l-1.4 1.4M4.8 11.2l-1.4 1.4" />
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
  /* Which chat thread is on screen. Every turn replays the last 20 messages,
     so a single endless conversation kept feeding the model answers written
     when the workspace was empty — a new thread is how you leave that behind. */
  const [threads, setThreads] = useState<Thread[]>([]);
  const [threadId, setThreadId] = useState<number | null>(null);
  const switchedRef = useRef(false);
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
  /* null = not checked yet, false = signed out. The API rejects unauthenticated
     requests, so the shell must not render until we know which. */
  const [session, setSession] = useState<Session | null | false>(null);

  const flash = (m: string) => {
    setToast(m);
    setTimeout(() => setToast(null), 3400);
  };

  /* ------------------------------ data loading ------------------------------ */

  const loadAll = useCallback(async () => {
    const grab = async <T,>(p: Promise<T>, fallback: T): Promise<T> => {
      try { return await p; } catch (e) {
        // a session that expired mid-use should return you to sign-in, not
        // leave a shell of empty panels behind
        if (e instanceof NotSignedIn) setSession(false);
        return fallback;
      }
    };
    const [b, f, r, au, c, m, u, lrn, kn, wfs, th] = await Promise.all([
      grab(api(`/api/briefing`), null),
      grab(api(`/api/feed`), { situations: [], actions: [] }),
      grab(api(`/api/actions/registry`), null),
      grab(api(`/api/audit?limit=80`), { entries: [] }),
      grab(api(`/api/connections`), { connections: [] }),
      grab(api(threadId ? `/api/conversations/${threadId}/messages` : `/api/conversations/default/messages`), { messages: [], conversation_id: null }),
      grab(api(`/api/understanding`), null),
      grab(api(`/api/learning`), null),
      grab(api(`/api/knowledge`), null),
      grab(api(`/api/workflows`), { workflows: [] }),
      grab(api(`/api/conversations`), { conversations: [] }),
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
    setThreads(th.conversations ?? []);
    if (m.conversation_id) setThreadId(m.conversation_id);
    const serverMsgs: Msg[] = (m.messages ?? []).map((x: any) => ({
      id: `server:${x.id}`, kind: x.role === "user" ? "user" : "agent", text: x.content, artifacts: x.artifacts ?? [],
    }));
    setMessages((prev) => {
      // Merging is right for a poll of the SAME thread (keeps a streaming
      // draft on screen). Switching threads must replace outright, or the
      // thread you left bleeds into the one you opened.
      if (switchedRef.current) { switchedRef.current = false; return serverMsgs; }
      const seen = new Set(prev.map((x) => x.id).filter(Boolean));
      return [...prev, ...serverMsgs.filter((x) => !x.id || !seen.has(x.id))];
    });
  }, [threadId]);

  /* Open an existing thread, or start a new one. Both clear what is on screen
     first: leaving the previous thread visible while its replacement loads
     reads as the message having been sent to the wrong place. */
  function openThread(id: number | null) {
    switchedRef.current = true;
    setMessages([]);
    setThreadId(id);
  }

  async function newThread() {
    try {
      const created = await api(`/api/conversations`, { method: "POST", body: JSON.stringify({}) });
      openThread(created.conversation_id);
    } catch (e: any) { flash(e.message); }
  }

  /* Establish identity BEFORE any data call: every other endpoint 401s without
     a session, so loading first would just be a burst of failures. */
  const checkSession = useCallback(async () => {
    try {
      setSession(await api(`/api/auth/me`));
    } catch (e) {
      setSession(e instanceof NotSignedIn ? false : false);
    }
  }, []);

  useEffect(() => { void checkSession(); }, [checkSession]);

  useEffect(() => {
    if (!session) return;
    void loadAll();
    const t = setInterval(() => void loadAll(), 30000);
    return () => clearInterval(t);
  }, [loadAll, session]);

  /* The work list is fetched separately from the feed because it re-queries
     whenever a facet chip is clicked — the server does the counting, so the
     numbers are always the truth rather than something the UI tallied. */
  const loadItems = useCallback(async () => {
    const q = new URLSearchParams();
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

  /* Gated on session, exactly like loadAll — this used to run ungated on mount,
     so on a fresh load it fired /api/items BEFORE sign-in, 401'd, emptied Work,
     and never re-ran (its deps only change on a filter click). A freshly
     signed-in user saw an empty Work until they touched a facet or reloaded.
     Re-running when the session appears is what fills it. */
  useEffect(() => {
    if (!session) return;
    void loadItems();
  }, [loadItems, session]);

  /* Live arrival: the watcher engine (every 5 min, or right after a webhook
     or a manual scan) publishes one "the feed changed" nudge per pass over
     Redis pub/sub; this SSE connection forwards it, and we just refetch —
     the 30s poll above stays as a fallback if the stream ever drops. */
  useEffect(() => {
    const es = new EventSource(`/api/feed/stream`);
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
      const r = await api(`/api/actions`, {
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

  async function requestReview(thingId: string) {
    setBusy(`review:${thingId}`);
    try {
      const r = await api(`/api/reviews/${encodeURIComponent(thingId)}/request-changes`, { method: "POST" });
      flash(r.status === "pending_approval"
        ? "Review prepared — approve it on the Feed to post it to the PR"
        : `Review: ${humanize(r.status)}`);
      await loadAll();
    } catch (e: any) { flash(e.message); } finally { setBusy(null); }
  }

  // What the reviewer found, per record — the GitHub-style panel reads this.
  const [reviews, setReviews] = useState<Record<string, ReviewFinding[]>>({});

  async function loadReview(thingId: string) {
    try {
      const r = await api(`/api/reviews/${encodeURIComponent(thingId)}`);
      setReviews((m) => ({ ...m, [thingId]: r.findings }));
    } catch (e: any) { flash(e.message); }
  }

  async function runReview(thingId: string) {
    setBusy(`runreview:${thingId}`);
    try {
      const r = await api(`/api/reviews/${encodeURIComponent(thingId)}/run`, { method: "POST" });
      setReviews((m) => ({ ...m, [thingId]: r.findings }));
      const n = r.count as number;
      flash(n ? `Reviewed — ${n} finding${n === 1 ? "" : "s"}` : "Reviewed — nothing to flag");
    } catch (e: any) { flash(e.message); } finally { setBusy(null); }
  }

  async function dismissFinding(thingId: string, findingId: string) {
    try {
      await api(`/api/situations/${encodeURIComponent(findingId)}/dismiss`, { method: "POST" });
      await loadReview(thingId);
    } catch (e: any) { flash(e.message); }
  }

  async function decide(actionId: number, verdict: "approve" | "reject") {
    setBusy(`decide:${actionId}`);
    try {
      const r = await api(`/api/actions/${actionId}/${verdict}`, { method: "POST" });
      flash(verdict === "approve" ? `Approved — ${humanize(r.status)}` : "Discarded");
      await loadAll();
    } catch (e: any) { flash(e.message); } finally { setBusy(null); }
  }

  async function resolveClarification(situationId: string, choiceId: string) {
    setBusy(`clarification:${situationId}`);
    try {
      const r = await api(`/api/situations/${encodeURIComponent(situationId)}/resolve`, {
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
      await api(`/api/situations/${encodeURIComponent(situationId)}/dismiss`, {
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
      const saved = await api(`/api/conversations/default/messages`, {
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
    const draftId = `draft:${Date.now()}`;
    setMessages((m) => [
      ...m,
      { id: localId, kind: "user", text },
      // a live bubble the stream writes into; replaced by the persisted turn
      { id: draftId, kind: "agent", text: "", artifacts: [], streaming: true },
    ]);

    function patchDraft(fn: (d: Msg) => Msg) {
      setMessages((m) => m.map((x) => (x.id === draftId ? fn(x) : x)));
    }

    /* A stream can simply STOP — a suspended tab, a dropped socket, a proxy
       timeout — and that arrives as neither an error event nor a final one.
       Without this flag the draft bubble sat on "Thinking…" forever, which
       reads as a hung agent even though the server finished the turn and saved
       it (the route persists from its own final event, not from delivery). */
    let finished = false;
    try {
      await streamPost(`/api/agent/chat/stream`, { message: text, conversation_id: threadId }, (e) => {
        if (e.type === "user_message") {
          setMessages((m) => m.map((x) => (x.id === localId ? { ...x, id: `server:${e.message.id}` } : x)));
        } else if (e.type === "tool_start") {
          patchDraft((d) => ({ ...d, working: e.label }));
        } else if (e.type === "tool_done") {
          patchDraft((d) => ({ ...d, working: undefined }));
        } else if (e.type === "text") {
          patchDraft((d) => ({ ...d, text: (d.text || "") + e.delta, working: undefined }));
        } else if (e.type === "text_reset") {
          // that prose was a preamble to a tool call, not the answer
          patchDraft((d) => ({ ...d, text: "" }));
        } else if (e.type === "error") {
          throw new Error(e.error);
        } else if (e.type === "final") {
          finished = true;
          patchDraft(() => ({
            id: `server:${e.message.id}`, kind: "agent", text: e.reply,
            artifacts: e.artifacts ?? [], streaming: false,
          }));
        }
      });
      if (!finished) {
        // drop the stranded draft first so loadAll's merge brings in the real
        // saved turn instead of leaving both on screen
        setMessages((m) => m.filter((x) => x.id !== draftId));
        flash("Connection dropped mid-answer — reloading it.");
      }
      await loadAll(); // the agent may have queued an action — reflect it
    } catch (e: any) {
      setMessages((m) => m.filter((x) => x.id !== localId && x.id !== draftId));
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
      const plan = await api(`/api/workflows/plan`, {
        method: "POST", body: JSON.stringify({ goal }),
      });
      const lead = plan.clarifications?.length
        ? "I need one thing before I can build this:"
        : "Here's the workflow I'd build:";
      const planKey = `plan-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
      setMessages((m) => [...m, { kind: "agent", text: lead, artifacts: [{ type: "workflow_plan", plan, planKey }] }]);
    } catch (e: any) {
      flash(`Couldn't build that: ${e.message}`);
      throw e;
    }
  }

  /* Record what a draft became, in the thread itself. The Save button reads its
     state from here rather than from component-local state, so a re-render (or
     a dev hot-reload) can't re-arm it and create the same workflow twice. */
  function markPlanSaved(planKey: string, workflowId: number) {
    setMessages((m) =>
      m.map((msg) =>
        msg.artifacts
          ? {
              ...msg,
              artifacts: msg.artifacts.map((a: any) =>
                a.type === "workflow_plan" && a.planKey === planKey
                  ? { ...a, saved: true, workflowId }
                  : a
              ),
            }
          : msg
      )
    );
  }

  /* Save once, at most. "Save" then "Save & run" must act on ONE workflow, not
     two — previously each button POSTed its own copy. */
  async function ensureWorkflowSaved(a: WorkflowPlanArtifact): Promise<number> {
    if (a.workflowId) return a.workflowId;
    const saved = await api(`/api/workflows`, {
      method: "POST",
      body: JSON.stringify({
        name: a.plan.name, goal: a.plan.goal, trigger: a.plan.trigger, steps: a.plan.steps,
      }),
    });
    markPlanSaved(a.planKey, saved.id);
    return saved.id;
  }

  async function saveWorkflowPlan(a: WorkflowPlanArtifact) {
    await ensureWorkflowSaved(a);
    flash(`Saved "${a.plan.name}" to Workflows`);
    await loadAll();
  }

  async function runWorkflowPlan(a: WorkflowPlanArtifact) {
    const id = await ensureWorkflowSaved(a);
    const r = await api(`/api/workflows/${id}/run`, { method: "POST" });
    flash(`${a.plan.name}: ${r.summary}`);
    await loadAll();
  }

  async function connect(source: string, token: string, config: any) {
    setBusy(`connect:${source}`);
    try {
      await api(`/api/connections/${source}`, {
        method: "POST", body: JSON.stringify({ token, ...config }),
      });
      flash(`${source} connected — reading your history`);
      await loadAll();
    } catch (e: any) { flash(e.message); } finally { setBusy(null); }
  }

  async function disconnect(source: string) {
    try {
      await api(`/api/connections/${source}`, { method: "DELETE" });
      flash(`${source} disconnected`);
      await loadAll();
    } catch (e: any) { flash(e.message); }
  }

  /* OAuth: hand the browser to the API's start endpoint, which mints a
     single-use state and bounces on to the provider. A full navigation (not
     fetch) because the human has to see and approve on the provider's own
     page — that's the entire point of the flow. */
  function startOAuth(source: string) {
    window.location.href = `/api/oauth/${source}/start`;
  }

  const loadTargets = useCallback(async (source: string) => {
    try {
      const r = await api(`/api/oauth/${source}/targets`);
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
      // set which repo/channel to watch. The config field is per-source
      // (repo for GitHub, channel for Slack) so this isn't hardcoded to one tool.
      const field = ({ github: "repo", slack: "channel" } as Record<string, string>)[source] ?? "repo";
      await api(`/api/connections/${source}`, {
        method: "POST",
        body: JSON.stringify({ [field]: key }),
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
      const d = await api(`/api/connections/${source}/sync`, { method: "POST" });
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
      await api(`/api/settings/dry_run`, {
        method: "POST",
        body: JSON.stringify({ enabled: on }),
      });
      flash(on ? "Practice mode — the AI won't act on its own; your clicks still run for real" : "Live — the AI now acts on its own while you're away");
      await loadAll();
    } catch (e: any) { flash(e.message); } finally { setBusy(null); }
  }

  async function scan() {
    setBusy("scan");
    try {
      // /api/scan polls the connected tools first; /api/analyze only re-reasons
      // over what was already stored, which made this button unable to find
      // anything new despite being called "Scan now"
      const d = await api(`/api/scan`, { method: "POST" });
      const pulled = Object.values((d.ingested ?? {}) as Record<string, number>)
        .reduce((n: number, c: number) => n + c, 0);
      flash(
        `Scan done — ${pulled} record${pulled === 1 ? "" : "s"} read, ` +
        `${d.situations} situation${d.situations === 1 ? "" : "s"} live`,
      );
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
      /* Below `sm` the label is hidden and this collapses to an icon rail, so
         without these the whole nav is nine unlabelled glyphs — unreadable on a
         narrow window and silent to a screen reader at every width. `title`
         gives the hover tooltip a mouse user needs when the text is gone;
         `aria-label` names it regardless. */
      title={label}
      aria-label={label}
      aria-current={screen === k ? "page" : undefined}
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

  async function signOut() {
    try { await api(`/api/auth/logout`, { method: "POST" }); } catch { /* leaving anyway */ }
    setSession(false);
    setMessages([]);          // never leave one account's thread on screen for the next
    setScreen("home");
  }

  if (session === null) {
    return <div className="flex h-screen items-center justify-center bg-canvas text-[13px] text-subtle">Loading…</div>;
  }
  if (session === false) {
    return <AuthView onSignedIn={(s) => { setSession(s); void loadAll(); }} />;
  }

  const NavGroup = ({ label }: { label: string }) => (
    <div className="mt-3 hidden px-2.5 pb-1 pt-1 text-[10px] font-semibold uppercase tracking-[1.2px] text-subtle first:mt-0 sm:block">
      {label}
    </div>
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
          <NavGroup label="Work" />
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
          <NavBtn k="attention" label="Attention" badge={liveCount || undefined} />
          <NavBtn k="work" label="Work" />

          <NavGroup label="Automate" />
          <NavBtn k="workflows" label="Workflows" />
          <NavBtn k="autonomy" label="Autonomy" />

          <NavGroup label="System" />
          <NavBtn k="knowledge" label="Business profile" />
          <NavBtn k="activity" label="Activity" />
          <NavBtn k="connections" label="Connections" />
          <NavBtn k="team" label="Team" />
        </nav>

        <div className="mt-auto hidden flex-col gap-2 px-2.5 sm:flex">
          {/* Who you are signed in as, and which workspace this data belongs
              to — the two things you must never have to guess when several
              workspaces look alike. */}
          <button
            onClick={() => setScreen("team")}
            className="flex flex-col items-start border-none bg-transparent p-0 text-left hover:opacity-80"
          >
            <span className="truncate text-[11.5px] text-muted">{session.user.name}</span>
            <span className="truncate text-[11px] text-subtle">
              {session.workspaces.find((w) => w.company_id === session.workspace.company_id)?.name
                ?? session.workspace.company_id}
            </span>
          </button>
          <button
            onClick={signOut}
            className="self-start border-none bg-transparent p-0 text-[11px] text-subtle hover:text-ink"
          >
            Sign out
          </button>

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
                  ? "Practice mode — the AI won't act on its own. Your approvals and buttons run for real."
                  : "Live — the AI acts on its own while you're away: triage, assign, notify."}
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
                    ? "click again to confirm — the AI will start acting on its own"
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
            threads={threads}
            threadId={threadId}
            onOpenThread={openThread}
            onNewThread={newThread}
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
              onRequestReview={requestReview}
              reviews={reviews}
              onRunReview={runReview}
              onLoadReview={loadReview}
              onDismissFinding={dismissFinding}
              busy={busy}
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

        {screen === "autonomy" && (
          <div className="min-h-0 flex-1 overflow-y-auto overflow-x-hidden">
            <AutonomyView flash={flash} />
          </div>
        )}
        {screen === "team" && (
          <div className="min-h-0 flex-1 overflow-y-auto overflow-x-hidden">
            <TeamView
              session={session}
              flash={flash}
              onSwitched={async () => {
                await checkSession();
                await loadAll();
                flash("Switched workspace");
              }}
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










