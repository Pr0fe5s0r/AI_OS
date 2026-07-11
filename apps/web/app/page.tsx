"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Badge, Card, EDGE_COLOR, Empty, I, NavButton, Pill, SEVERITY, STATUS_COLOR,
  SideLabel, SkeletonRows, api, humanMetric, initials, relativeTime, src,
} from "./lib";

/* ------------------------------- types ------------------------------- */
type Ev = { id: string; source: string; type: string; actor: { name: string }; timestamp: string; content: string; metadata: any };
type Result = Ev & { similarity: number; recency: number; score: number };
type GNode = { id: string; type: string; label: string; source: string | null; metadata: any };
type GEdge = { src_id: string; dst_id: string; type: string; weight: number };
type GraphResp = { center: string; nodes: GNode[]; edges: GEdge[] };
type Norm = { metric: string; unit: string; n: number; median: number; mean: number; std: number; window_days: number };
type Evidence = { event_id: string; source: string; timestamp: string; excerpt: string; url: string | null };
type Situation = { id: string; rule: string; severity: string; title: string; summary: string; recommended_action: string | null; evidence: Evidence[]; status: string; created_at: string; resolved_at: string | null };
type Action = { id: number; situation_id: string | null; action: string; params: any; status: string; detail: string; result: any; requested_by: string; decided_by: string | null; requested_at: string };
type Conn = { source: string; connected: boolean; config: any };
type Member = { id: string; name: string; email: string; roles: string[]; skills: string[]; max_open_issues: number; assignable: boolean; open_issues?: number; free?: boolean };
type TeamResp = { members: Member[]; roles: string[]; notify_roles: string[]; assignable_role: string };
type TicketT = { id: number; situation_id: string | null; title: string; description: string; assignee: string; status: string; external_url: string | null; created_at: string; closed_at: string | null };
type OpsBrief = { mode: string; headline: string; next_best_action: string; stats: { events: number; active_situations: number; high_risk: number; pending_approvals: number; learned_norms: number; norm_metrics: number; sources_connected: number }; events: { source: string; type: string; count: number; latest: string | null }[]; norms: { metric: string; n: number; median: number; unit: string }[]; watchlist: { id: string; severity: string; title: string; summary: string; recommended_action: string | null; status: string; evidence_count: number }[] };

type View = "command" | "connections" | "team" | "issues" | "tickets" | "search" | "related" | "norms" | "flags" | "actions";

const CONNECT_FIELDS: Record<string, { key: string; label: string; placeholder: string }[]> = {
  github: [{ key: "repo", label: "Repository", placeholder: "owner/name" }],
  slack: [{ key: "channel", label: "Channel", placeholder: "#incidents" }],
  zendesk: [{ key: "subdomain", label: "Subdomain", placeholder: "acme" }],
};
const TOKEN_HINT: Record<string, string> = {
  github: "Optional. With a token: private repos, deeper history, real merge times.",
  slack: "Required. Bot token (xoxb-…) with channels:history + channels:read.",
  zendesk: "Required. Format: you@company.com/token:API_TOKEN",
};

/* The briefing (mode / headline / next_best_action) is decided ONCE, server-side,
   by the vertical's BRIEFING_POLICY. The UI only renders it. */

/* ================================ page ================================ */

export default function Home() {
  const [view, setView] = useState<View>("command");
  const [conns, setConns] = useState<Conn[]>([]);
  const [issues, setIssues] = useState<Ev[] | null>(null);
  const [situations, setSituations] = useState<Situation[] | null>(null);
  const [actions, setActions] = useState<Action[] | null>(null);
  const [norms, setNorms] = useState<Norm[] | null>(null);
  const [dryRun, setDryRun] = useState(true);
  const [briefing, setBriefing] = useState<OpsBrief | null>(null);
  const [team, setTeam] = useState<TeamResp | null>(null);
  const [tickets, setTickets] = useState<TicketT[] | null>(null);

  const [query, setQuery] = useState("");
  const [results, setResults] = useState<Result[]>([]);
  const [searched, setSearched] = useState(false);

  const [center, setCenter] = useState<string | null>(null);
  const [graph, setGraph] = useState<GraphResp | null>(null);

  const [busy, setBusy] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);

  const flash = (m: string) => { setToast(m); setTimeout(() => setToast(null), 4000); };

  const loadConnections = useCallback(async () => {
    try { setConns((await api("/api/connections?company_id=default")).connections); } catch {}
  }, []);
  const loadIssues = useCallback(async () => {
    setIssues(null);
    try { setIssues((await api("/api/events?company_id=default&source=github&limit=100")).events); }
    catch { setIssues([]); }
  }, []);
  const loadSituations = useCallback(async () => {
    setSituations(null);
    try { setSituations((await api("/api/situations?company_id=default")).situations); } catch { setSituations([]); }
  }, []);
  const loadActions = useCallback(async () => {
    setActions(null);
    try { const d = await api("/api/actions?company_id=default"); setActions(d.actions); setDryRun(d.dry_run); }
    catch { setActions([]); }
  }, []);
  const loadNorms = useCallback(async () => {
    setNorms(null);
    try { setNorms((await api("/api/norms?company_id=default")).norms); } catch { setNorms([]); }
  }, []);
  const loadBriefing = useCallback(async () => {
    // One call. The server already decided what matters and what to do next.
    try { setBriefing(await api("/api/briefing?company_id=default")); } catch {}
  }, []);

  const loadTeam = useCallback(async () => {
    setTeam(null);
    try { setTeam(await api("/api/team?company_id=default")); } catch { setTeam({ members: [], roles: [], notify_roles: [], assignable_role: "engineer" }); }
  }, []);

  async function saveMember(m: Member) {
    setBusy("team:save");
    try { await api("/api/team?company_id=default", { method: "POST", body: JSON.stringify(m) }); await loadTeam(); flash(`${m.name} saved`); }
    catch (e: any) { flash(e.message); } finally { setBusy(null); }
  }

  async function removeMember(id: string) {
    setBusy("team:remove");
    try { await api(`/api/team/${encodeURIComponent(id)}?company_id=default`, { method: "DELETE" }); await loadTeam(); flash(`${id} removed`); }
    catch (e: any) { flash(e.message); } finally { setBusy(null); }
  }

  const loadTickets = useCallback(async () => {
    setTickets(null);
    try { setTickets((await api("/api/tickets?company_id=default")).tickets); } catch { setTickets([]); }
  }, []);

  async function closeTicket(id: number, by: string) {
    setBusy(`ticket:${id}`);
    try {
      const r = await api(`/api/tickets/${id}/close?company_id=default`, { method: "POST", body: JSON.stringify({ by }) });
      flash(r.github?.github === "executed" ? "Ticket done — GitHub issue closed" : `Ticket done — GitHub: ${r.github?.github}`);
      await loadTickets(); await loadBriefing();
    } catch (e: any) { flash(e.message); } finally { setBusy(null); }
  }

  const loadSettings = useCallback(async () => {
    try { setDryRun(Boolean((await api("/api/settings?company_id=default")).dry_run)); } catch {}
  }, []);

  useEffect(() => { loadConnections(); loadBriefing(); loadSettings(); }, [loadConnections, loadBriefing, loadSettings]);

  const go = (v: View) => {
    setView(v);
    if (v === "issues") loadIssues();
    if (v === "flags") loadSituations();
    if (v === "actions") loadActions();
    if (v === "norms") loadNorms();
    if (v === "team") loadTeam();
    if (v === "tickets") loadTickets();
    if (v === "command") loadBriefing();
  };

  async function connect(source: string, token: string, config: any) {
    setBusy(`connect:${source}`);
    try {
      await api(`/api/connections/${source}?company_id=default`, {
        method: "POST", body: JSON.stringify({ token, ...config }),
      });
      await loadConnections();
      await loadBriefing();
      flash(`${source} connected`);
    } catch (e: any) { flash(e.message); } finally { setBusy(null); }
  }

  async function disconnect(source: string) {
    setBusy(`disconnect:${source}`);
    try { await api(`/api/connections/${source}?company_id=default`, { method: "DELETE" }); await loadConnections(); await loadBriefing(); flash(`${source} disconnected`); }
    catch (e: any) { flash(e.message); } finally { setBusy(null); }
  }

  async function sync(source: string) {
    setBusy(`sync:${source}`);
    try {
      const d = await api(`/api/connections/${source}/sync?company_id=default`, { method: "POST" });
      await loadBriefing();
      flash(`queued ${JSON.stringify(d.enqueued)} - the workspace will update after ingestion`);
    } catch (e: any) { flash(e.message); } finally { setBusy(null); }
  }

  async function analyze() {
    setBusy("analyze");
    try {
      const d = await api("/api/analyze?company_id=default", { method: "POST" });
      await loadBriefing();
      flash(`${d.situations} situations detected, ${d.delivered} delivered`);
      go("command");
    } catch (e: any) { flash(e.message); } finally { setBusy(null); }
  }

  async function runSearch(q: string) {
    if (!q.trim()) return;
    setView("search"); setSearched(true); setResults([]);
    try { setResults((await api(`/api/search?q=${encodeURIComponent(q)}&company_id=default`)).results); } catch {}
  }

  async function openRelated(id: string) {
    setView("related"); setCenter(id); setGraph(null);
    try { setGraph(await api(`/api/entity/${encodeURIComponent(id)}/related?company_id=default&hops=2`)); }
    catch { setGraph({ center: id, nodes: [], edges: [] }); }
  }

  async function requestAction(action: string, situation_id: string | null, params: any) {
    setBusy(`act:${action}`);
    try {
      const r = await api("/api/actions?company_id=default", {
        method: "POST", body: JSON.stringify({ action, situation_id, params: { ...params, situation_id }, requested_by: "ui" }),
      });
      flash(`${action}: ${r.status.replace("_", " ")}`);
      go("actions");
    } catch (e: any) { flash(e.message); } finally { setBusy(null); }
  }

  async function toggleDryRun(enabled: boolean) {
    setBusy("dry_run");
    try {
      const r = await api("/api/settings/dry_run?company_id=default", {
        method: "POST", body: JSON.stringify({ enabled }),
      });
      setDryRun(r.dry_run);
      flash(r.dry_run ? "Practice mode on — nothing will be sent" : "LIVE — the AI now writes to your real tools");
      await loadBriefing();
    } catch (e: any) { flash(e.message); } finally { setBusy(null); }
  }

  async function onAssigned(message: string) {
    flash(message);
    await Promise.all([loadSituations(), loadBriefing()]);
  }

  async function decide(id: number, verdict: "approve" | "reject") {
    setBusy(`decide:${id}`);
    try { const r = await api(`/api/actions/${id}/${verdict}?company_id=default`, { method: "POST" }); flash(`action ${id}: ${r.status.replace("_", " ")}`); await loadActions(); await loadBriefing(); }
    catch (e: any) { flash(e.message); } finally { setBusy(null); }
  }

  const openFlags = briefing?.stats.high_risk ?? situations?.filter((s) => s.severity === "critical" || s.severity === "high").length ?? 0;
  const pending = briefing?.stats.pending_approvals ?? actions?.filter((a) => a.status === "pending_approval").length ?? 0;
  const connectedCount = conns.filter((c) => c.connected).length;

  return (
    <div className="grid h-screen grid-cols-[248px_1fr] bg-canvas text-ink">
      {/* ============================ SIDEBAR ============================ */}
      <aside className="flex flex-col border-r border-edge bg-panel">
        <div className="flex items-center gap-2.5 px-4 py-3.5">
          <span className="grid h-7 w-7 place-items-center rounded-lg bg-accent text-black">{I.leaf()}</span>
          <span className="text-[15px] font-semibold tracking-tight">AI OS</span>
        </div>

        <div className="px-3">
          <div className="flex items-center gap-2.5 rounded-lg border border-edge bg-elevated px-2.5 py-2">
            <span className="grid h-7 w-7 place-items-center rounded-md bg-gradient-to-br from-accent to-teal-600 text-xs font-bold text-black">D</span>
            <div className="min-w-0 flex-1">
              <div className="truncate text-[13px] font-medium">Default</div>
              <div className="truncate font-mono text-[11px] text-subtle">{connectedCount} source{connectedCount === 1 ? "" : "s"} connected</div>
            </div>
          </div>
        </div>

        <nav className="mt-5 flex-1 overflow-y-auto px-3">
          <SideLabel>OS</SideLabel>
          <NavButton icon={I.bolt()} label="Command deck" active={view === "command"} onClick={() => go("command")} badge={openFlags || pending || undefined} />

          <SideLabel className="mt-5">Connect</SideLabel>
          <NavButton icon={I.plug()} label="Connections" active={view === "connections"} onClick={() => go("connections")} />
          <NavButton icon={I.issues()} label="Issues" active={view === "issues"} onClick={() => go("issues")} />
          <NavButton icon={I.people()} label="Team" active={view === "team"} onClick={() => go("team")} />
          <NavButton icon={I.ticket()} label="Tickets" active={view === "tickets"} onClick={() => go("tickets")}
            badge={tickets?.filter((t) => t.status !== "done").length || undefined} />

          <SideLabel className="mt-5">Understand</SideLabel>
          <NavButton icon={I.search()} label="Search" active={view === "search"} onClick={() => setView("search")} />
          <NavButton icon={I.graph()} label="Related graph" active={view === "related"} onClick={() => (center ? openRelated(center) : setView("related"))} />
          <NavButton icon={I.norms()} label="Norms" active={view === "norms"} onClick={() => go("norms")} />

          <SideLabel className="mt-5">Alert &amp; Act</SideLabel>
          <NavButton icon={I.flag()} label="Flags" active={view === "flags"} onClick={() => go("flags")} badge={openFlags || undefined} />
          <NavButton icon={I.bolt()} label="Command center" active={view === "actions"} onClick={() => go("actions")} badge={pending || undefined} />
        </nav>

        <div className="border-t border-edge px-3 py-3">
          <button
            onClick={analyze}
            disabled={busy === "analyze"}
            className="w-full rounded-md bg-accent px-3 py-2 text-[13px] font-semibold text-black hover:brightness-110 disabled:opacity-50"
          >
            {busy === "analyze" ? "Scanning..." : "Scan workspace"}
          </button>
          <div className="mt-2 text-center font-mono text-[10px] text-subtle">learn - detect - draft</div>
        </div>
      </aside>

      {/* ============================== MAIN ============================== */}
      <div className="flex min-w-0 flex-col">
        <header className="flex h-14 items-center justify-between border-b border-edge px-6">
          <div>
            <div className="text-[14px] font-semibold capitalize leading-tight">
              {view === "command" ? "Command deck" : view === "actions" ? "Command center" : view === "related" ? "Related graph" : view}
            </div>
            <div className="font-mono text-[11px] text-subtle">
              {view === "command" && (briefing?.headline ?? "Workspace briefing")}
              {view === "connections" && "connect your tools - tokens encrypted at rest"}
              {view === "issues" && "live issues + PRs from your connected repo"}
              {view === "search" && "hybrid: 0.7·similarity + 0.3·recency"}
              {view === "related" && `entity resolution · ${center ?? "—"}`}
              {view === "norms" && "rolling-window baselines"}
              {view === "flags" && "detected situations with evidence"}
              {view === "actions" && `approval-gated actions · dry_run=${dryRun}`}
            </div>
          </div>
          <span className="flex items-center gap-1.5 rounded-full border border-edge bg-elevated px-2.5 py-1 text-[12px] text-muted">
            <span className="h-2 w-2 rounded-full bg-accent" /> {briefing?.mode?.replace(/_/g, " ") ?? "ready"}
          </span>
        </header>

        <main className="flex-1 overflow-y-auto">
          <div className="mx-auto max-w-5xl px-6 py-6">
            {view === "command" && <CommandDeck briefing={briefing} busy={busy} conns={conns} onAnalyze={analyze} onGo={go} onSearch={runSearch} />}
            {view === "tickets" && <Tickets tickets={tickets} busy={busy} onClose={closeTicket} />}
            {view === "team" && <Team team={team} busy={busy} onSave={saveMember} onRemove={removeMember} />}
            {view === "connections" && <Connections conns={conns} busy={busy} onConnect={connect} onDisconnect={disconnect} onSync={sync} />}
            {view === "issues" && <Issues issues={issues} onRelated={openRelated} />}
            {view === "search" && <Search query={query} setQuery={setQuery} run={runSearch} results={results} searched={searched} onRelated={openRelated} />}
            {view === "related" && <Related graph={graph} center={center} onRecenter={openRelated} />}
            {view === "norms" && <Norms norms={norms} />}
            {view === "flags" && <Flags situations={situations} busy={busy} onAct={requestAction} onRelated={openRelated} onAssigned={onAssigned} />}
            {view === "actions" && <Commands actions={actions} dryRun={dryRun} busy={busy} onDecide={decide} onToggleDryRun={toggleDryRun} />}
          </div>
        </main>
      </div>

      {toast && (
        <div className="fixed bottom-5 left-1/2 z-50 -translate-x-1/2 rounded-lg border border-edge bg-elevated px-4 py-2.5 text-[13px] text-ink shadow-xl">
          {toast}
        </div>
      )}
    </div>
  );
}

/* =============================== CommandDeck =============================== */

function CommandDeck({ briefing, busy, conns, onAnalyze, onGo, onSearch }: {
  briefing: OpsBrief | null;
  busy: string | null;
  conns: Conn[];
  onAnalyze: () => void;
  onGo: (v: View) => void;
  onSearch: (q: string) => void;
}) {
  if (!briefing) return <SkeletonRows />;

  const connected = conns.filter((c) => c.connected);
  const modeColor = briefing.mode === "triage_now" || briefing.mode === "needs_human"
    ? "#d29922"
    : briefing.mode === "waiting_for_signal"
      ? "#58a6ff"
      : "#3fb950";
  const topSource = briefing.events[0];

  return (
    <div className="space-y-5">
      <div className="grid gap-4 lg:grid-cols-[1.35fr_0.65fr]">
        <section className="rounded-lg border border-edge bg-panel p-5">
          <div className="flex flex-wrap items-center gap-2">
            <Pill color={modeColor} tint={`${modeColor}22`}>{briefing.mode.replace(/_/g, " ")}</Pill>
            <span className="font-mono text-[11px] text-subtle">human controlled</span>
          </div>
          <h1 className="mt-3 text-[22px] font-semibold leading-tight tracking-normal text-ink">{briefing.headline}</h1>
          <p className="mt-2 text-[13.5px] leading-relaxed text-muted">{briefing.next_best_action}</p>
          <div className="mt-4 flex flex-wrap gap-2">
            <button onClick={onAnalyze} disabled={busy === "analyze"}
              className="rounded-md bg-accent px-3 py-2 text-[13px] font-semibold text-black hover:brightness-110 disabled:opacity-50">
              {busy === "analyze" ? "Scanning..." : "Scan workspace"}
            </button>
            <button onClick={() => onGo(briefing.stats.pending_approvals ? "actions" : "flags")}
              className="rounded-md border border-edge bg-elevated px-3 py-2 text-[13px] text-ink hover:border-accent/60">
              Review work queue
            </button>
            <button onClick={() => onSearch("unassigned high impact bug")}
              className="rounded-md border border-edge bg-elevated px-3 py-2 text-[13px] text-muted hover:text-ink">
              Trace risk
            </button>
          </div>
        </section>

        <section className="grid grid-cols-2 gap-2">
          <AiStat label="events read" value={briefing.stats.events} tone="#58a6ff" />
          <AiStat label="active flags" value={briefing.stats.active_situations} tone="#d29922" />
          <AiStat label="high risk" value={briefing.stats.high_risk} tone="#f85149" />
          <AiStat label="approvals" value={briefing.stats.pending_approvals} tone="#3fb950" />
        </section>
      </div>

      <div className="grid gap-4 lg:grid-cols-[1fr_1fr]">
        <section className="rounded-lg border border-edge bg-panel">
          <div className="flex items-center justify-between border-b border-edge px-4 py-3">
            <div className="text-[13px] font-semibold">Watchlist</div>
            <button onClick={() => onGo("flags")} className="font-mono text-[11px] text-subtle hover:text-ink">open flags</button>
          </div>
          {briefing.watchlist.length === 0 ? (
            <div className="px-4 py-8 text-center text-[13px] text-muted">No active risk in the current briefing.</div>
          ) : briefing.watchlist.map((item, i) => {
            const sev = SEVERITY[item.severity] ?? SEVERITY.low;
            return (
              <div key={item.id} className={`px-4 py-3 ${i > 0 ? "border-t border-edge" : ""}`}>
                <div className="flex items-center gap-2">
                  <Pill color={sev.color} tint={sev.tint}>{item.severity}</Pill>
                  <span className="min-w-0 flex-1 truncate text-[13px] font-medium">{item.title}</span>
                  <span className="font-mono text-[11px] text-subtle">{item.evidence_count} cite</span>
                </div>
                <p className="mt-2 line-clamp-2 text-[12.5px] leading-relaxed text-muted">{item.summary || item.recommended_action}</p>
              </div>
            );
          })}
        </section>

        <section className="rounded-lg border border-edge bg-panel">
          <div className="flex items-center justify-between border-b border-edge px-4 py-3">
            <div className="text-[13px] font-semibold">System model</div>
            <button onClick={() => onGo("norms")} className="font-mono text-[11px] text-subtle hover:text-ink">open norms</button>
          </div>
          <div className="grid gap-0 sm:grid-cols-2">
            <Insight label="sources connected" value={`${connected.length}/${conns.length || 3}`} detail={connected.map((c) => c.source).join(", ") || "waiting"} />
            <Insight label="latest signal" value={topSource ? `${topSource.count}` : "0"} detail={topSource ? `${topSource.source} ${topSource.type}` : "none"} />
            <Insight
              label="learned norms"
              value={`${briefing.stats.learned_norms}/${briefing.stats.norm_metrics}`}
              detail={
                briefing.stats.learned_norms > 0
                  ? humanMetric(briefing.norms.find((n) => n.n > 0)!.metric)
                  : briefing.stats.norm_metrics > 0
                    ? "no closed/merged items to learn from yet"
                    : "not computed"
              }
            />
            <Insight label="action safety" value="on" detail="approval gate + dry run" />
          </div>
        </section>
      </div>

      <section className="rounded-lg border border-edge bg-panel p-4">
        <div className="mb-3 text-[13px] font-semibold">Quick traces</div>
        <div className="flex flex-wrap gap-2">
          {["stale open issue", "P0 without owner", "PR with no linked issue", "untriaged bug report"].map((q) => (
            <button key={q} onClick={() => onSearch(q)} className="rounded-md border border-edge bg-elevated px-3 py-1.5 text-[12px] text-muted hover:border-accent/60 hover:text-ink">
              {q}
            </button>
          ))}
        </div>
      </section>
    </div>
  );
}

function AiStat({ label, value, tone }: { label: string; value: number; tone: string }) {
  return (
    <div className="rounded-lg border border-edge bg-panel p-4">
      <div className="font-mono text-[10px] uppercase tracking-wide text-subtle">{label}</div>
      <div className="mt-2 text-[28px] font-semibold leading-none" style={{ color: tone }}>{value}</div>
    </div>
  );
}

function Insight({ label, value, detail }: { label: string; value: React.ReactNode; detail: string }) {
  return (
    <div className="border-b border-edge px-4 py-3 odd:border-r sm:[&:nth-last-child(-n+2)]:border-b-0">
      <div className="text-[12px] text-muted">{label}</div>
      <div className="mt-1 flex items-baseline gap-2">
        <span className="text-[20px] font-semibold text-ink">{value}</span>
        <span className="min-w-0 truncate font-mono text-[11px] text-subtle">{detail}</span>
      </div>
    </div>
  );
}
/* ============================== Tickets ============================== */

function Tickets({ tickets, busy, onClose }: { tickets: TicketT[] | null; busy: string | null; onClose: (id: number, by: string) => void }) {
  if (tickets === null) return <SkeletonRows />;
  if (tickets.length === 0)
    return <Empty>No tickets yet. One is created when the project manager assigns an issue from the email.</Empty>;

  const open = tickets.filter((t) => t.status !== "done");
  const done = tickets.filter((t) => t.status === "done");

  return (
    <>
      <div className="mb-3 text-[13px] text-muted">
        <span className="text-ink">{open.length} open</span>{done.length > 0 && ` · ${done.length} done`}.
        Closing a ticket closes its GitHub issue.
      </div>
      <div className="overflow-hidden rounded-lg border border-edge bg-panel">
        {tickets.map((t, i) => {
          const finished = t.status === "done";
          return (
            <div key={t.id} className={`px-4 py-3 ${i > 0 ? "border-t border-edge" : ""} ${finished ? "opacity-55" : ""}`}>
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-mono text-[11px] text-subtle">#{t.id}</span>
                {finished
                  ? <Pill color="#3fb950" tint="rgba(63,185,80,0.15)">done</Pill>
                  : <Pill color="#d29922" tint="rgba(210,153,34,0.15)">open</Pill>}
                <span className={`text-[14px] font-medium ${finished ? "line-through decoration-subtle" : ""}`}>{t.title}</span>
                <span className="font-mono text-[11px] text-subtle">→ {t.assignee}</span>
                <span className="ml-auto font-mono text-[11px] text-subtle">
                  {finished ? `closed ${relativeTime(t.closed_at ?? undefined)}` : relativeTime(t.created_at)}
                </span>
              </div>
              <p className="mt-1.5 line-clamp-2 pl-1 text-[13px] leading-relaxed text-ink/80">{t.description}</p>
              <div className="mt-2 flex items-center gap-3">
                {t.external_url && (
                  <a href={t.external_url} target="_blank" rel="noreferrer" className="font-mono text-[11px] text-info hover:underline">
                    open on github ↗
                  </a>
                )}
                {!finished && (
                  <button onClick={() => onClose(t.id, t.assignee)} disabled={!!busy}
                    className="rounded-md bg-accent px-3 py-1.5 text-[12px] font-semibold text-black hover:brightness-110 disabled:opacity-50">
                    {busy === `ticket:${t.id}` ? "Closing..." : "Mark done & close issue"}
                  </button>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </>
  );
}

/* =============================== Team =============================== */

const EMPTY_MEMBER: Member = {
  id: "", name: "", email: "", roles: ["engineer"], skills: [],
  max_open_issues: 3, assignable: true,
};

function Team({ team, busy, onSave, onRemove }: {
  team: TeamResp | null; busy: string | null;
  onSave: (m: Member) => void; onRemove: (id: string) => void;
}) {
  const [draft, setDraft] = useState<Member | null>(null);
  if (!team) return <SkeletonRows />;

  const notify = team.notify_roles.join(" + ").replace(/_/g, " ");
  const edit = (m: Member) => setDraft({ ...m, skills: [...m.skills] });

  return (
    <>
      <div className="mb-4 text-[13px] text-muted">
        The AI emails everyone with the <span className="text-ink">{notify}</span> role when a
        situation is raised, and assigns the work to a <span className="text-ink">{team.assignable_role}</span> who
        still has spare capacity.
      </div>

      {team.members.length === 0 ? (
        <Empty>No team configured. The AI cannot notify or assign anyone yet — add someone below.</Empty>
      ) : (
        <div className="overflow-hidden rounded-lg border border-edge bg-panel">
          {team.members.map((m, i) => (
            <div key={m.id} className={`px-4 py-3 ${i > 0 ? "border-t border-edge" : ""}`}>
              <div className="flex flex-wrap items-center gap-2">
                <span className="grid h-7 w-7 place-items-center rounded-full bg-edge text-[10px] font-semibold text-ink">
                  {initials(m.name || m.id)}
                </span>
                <span className="text-[14px] font-medium">{m.name}</span>
                <span className="font-mono text-[11px] text-subtle">{m.id}</span>
                {m.roles.map((r) => (
                  <Pill key={r} color="#58a6ff" tint="rgba(88,166,255,0.12)">{r.replace(/_/g, " ")}</Pill>
                ))}
                {m.assignable && m.roles.includes(team.assignable_role) && (
                  m.free
                    ? <Pill color="#3fb950" tint="rgba(63,185,80,0.15)">free {m.open_issues}/{m.max_open_issues}</Pill>
                    : <Pill color="#d29922" tint="rgba(210,153,34,0.15)">busy {m.open_issues}/{m.max_open_issues}</Pill>
                )}
                <span className="ml-auto flex items-center gap-2">
                  <button onClick={() => edit(m)} className="rounded-md border border-edge bg-elevated px-2.5 py-1 text-[12px] text-muted hover:text-ink">Edit</button>
                  <button onClick={() => onRemove(m.id)} disabled={!!busy}
                    className="rounded-md border border-edge bg-elevated px-2.5 py-1 text-[12px] text-muted hover:text-danger disabled:opacity-50">Remove</button>
                </span>
              </div>
              <div className="mt-1.5 pl-9 font-mono text-[11px] text-subtle">
                {m.email}{m.skills.length > 0 && ` · ${m.skills.join(", ")}`}
              </div>
            </div>
          ))}
        </div>
      )}

      <div className="mt-4">
        {draft ? (
          <MemberForm draft={draft} roles={team.roles} busy={busy}
            onChange={setDraft} onCancel={() => setDraft(null)}
            onSubmit={() => { onSave(draft); setDraft(null); }} />
        ) : (
          <button onClick={() => setDraft({ ...EMPTY_MEMBER })}
            className="rounded-md bg-accent px-3 py-2 text-[13px] font-semibold text-black hover:brightness-110">
            Add team member
          </button>
        )}
      </div>
    </>
  );
}

function MemberForm({ draft, roles, busy, onChange, onCancel, onSubmit }: {
  draft: Member; roles: string[]; busy: string | null;
  onChange: (m: Member) => void; onCancel: () => void; onSubmit: () => void;
}) {
  const set = (patch: Partial<Member>) => onChange({ ...draft, ...patch });
  const toggleRole = (r: string) =>
    set({ roles: draft.roles.includes(r) ? draft.roles.filter((x) => x !== r) : [...draft.roles, r] });
  const valid = draft.id.trim() && draft.name.trim() && draft.email.includes("@") && draft.roles.length > 0;

  return (
    <Card className="p-4">
      <div className="mb-3 text-[13px] font-semibold">{draft.id ? "Edit member" : "New member"}</div>
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="GitHub username" hint="used to assign the issue">
          <input value={draft.id} onChange={(e) => set({ id: e.target.value })} placeholder="octocat" className={inputCls} />
        </Field>
        <Field label="Full name">
          <input value={draft.name} onChange={(e) => set({ name: e.target.value })} placeholder="Ada Lovelace" className={inputCls} />
        </Field>
        <Field label="Email" hint="where notifications go">
          <input value={draft.email} onChange={(e) => set({ email: e.target.value })} placeholder="ada@company.com" className={inputCls} />
        </Field>
        <Field label="Max open issues" hint="busy above this">
          <input type="number" min={1} value={draft.max_open_issues}
            onChange={(e) => set({ max_open_issues: Math.max(1, Number(e.target.value) || 1) })} className={inputCls} />
        </Field>
      </div>

      <div className="mt-3">
        <div className="mb-1 text-[12px] text-muted">Roles</div>
        <div className="flex flex-wrap gap-2">
          {roles.map((r) => (
            <button key={r} onClick={() => toggleRole(r)}
              className={`rounded-md border px-2.5 py-1.5 text-[12px] ${
                draft.roles.includes(r) ? "border-accent/60 bg-accent/10 text-ink" : "border-edge bg-elevated text-muted hover:text-ink"
              }`}>
              {r.replace(/_/g, " ")}
            </button>
          ))}
        </div>
      </div>

      <div className="mt-3">
        <Field label="Skills" hint="comma separated — the AI matches work to these">
          <input value={draft.skills.join(", ")}
            onChange={(e) => set({ skills: e.target.value.split(",").map((s) => s.trim()).filter(Boolean) })}
            placeholder="python, api, frontend" className={inputCls} />
        </Field>
      </div>

      <label className="mt-3 flex items-center gap-2 text-[12px] text-muted">
        <input type="checkbox" checked={draft.assignable} onChange={(e) => set({ assignable: e.target.checked })} />
        Can be assigned work
      </label>

      <div className="mt-4 flex gap-2">
        <button onClick={onSubmit} disabled={!valid || !!busy}
          className="rounded-md bg-accent px-3 py-1.5 text-[13px] font-semibold text-black hover:brightness-110 disabled:opacity-40">
          {busy?.startsWith("team") ? "Saving..." : "Save member"}
        </button>
        <button onClick={onCancel} className="rounded-md border border-edge bg-elevated px-3 py-1.5 text-[13px] text-muted hover:text-ink">Cancel</button>
      </div>
    </Card>
  );
}

const inputCls = "w-full rounded-md border border-edge bg-elevated px-3 py-2 text-[13px] text-ink placeholder-subtle outline-none focus:border-accent/60";

function Field({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <div>
      <label className="mb-1 block text-[12px] text-muted">
        {label} {hint && <span className="text-subtle">· {hint}</span>}
      </label>
      {children}
    </div>
  );
}

/* ============================ Connections ============================ */

function Connections({ conns, busy, onConnect, onDisconnect, onSync }: any) {
  return (
    <>
      <div className="mb-4 text-[13px] text-muted">
        Connect a tool, then <span className="text-ink">Sync</span> to pull real events. Tokens are encrypted at rest and never returned.
      </div>
      <div className="space-y-3">
        {conns.map((c: Conn) => <ConnectionCard key={c.source} c={c} busy={busy} onConnect={onConnect} onDisconnect={onDisconnect} onSync={onSync} />)}
      </div>
    </>
  );
}

function ConnectionCard({ c, busy, onConnect, onDisconnect, onSync }: any) {
  const meta = src(c.source);
  const fields = CONNECT_FIELDS[c.source] ?? [];
  const [token, setToken] = useState("");
  const [cfg, setCfg] = useState<any>(c.config ?? {});
  const [open, setOpen] = useState(false);

  return (
    <div className="rounded-lg border border-edge bg-panel">
      <div className="flex items-center gap-3 px-4 py-3">
        <span className="grid h-8 w-8 place-items-center rounded-md text-[12px] font-bold" style={{ background: meta.tint, color: meta.color }}>
          {meta.letter}
        </span>
        <div className="min-w-0 flex-1">
          <div className="text-[14px] font-medium capitalize">{c.source}</div>
          <div className="truncate font-mono text-[11px] text-subtle">
            {c.connected ? Object.entries(c.config).map(([k, v]) => `${k}=${v}`).join("  ") || "connected" : "not connected"}
          </div>
        </div>
        {c.connected ? (
          <>
            <span className="rounded-full px-2 py-0.5 text-[10px] font-medium" style={{ background: "rgba(63,185,80,0.15)", color: "#3fb950" }}>connected</span>
            <button onClick={() => onSync(c.source)} disabled={!!busy}
              className="rounded-md bg-accent px-3 py-1.5 text-[12px] font-semibold text-black hover:brightness-110 disabled:opacity-50">
              {busy === `sync:${c.source}` ? "Syncing…" : "Sync"}
            </button>
            <button onClick={() => onDisconnect(c.source)} disabled={!!busy}
              className="rounded-md border border-edge bg-elevated px-3 py-1.5 text-[12px] text-muted hover:text-ink">Remove</button>
          </>
        ) : (
          <button onClick={() => setOpen(!open)} className="rounded-md border border-edge bg-elevated px-3 py-1.5 text-[12px] font-medium text-ink hover:border-accent/60">
            {open ? "Cancel" : "Connect"}
          </button>
        )}
      </div>

      {open && !c.connected && (
        <div className="space-y-3 border-t border-edge bg-canvas px-4 py-4">
          {fields.map((f) => (
            <div key={f.key}>
              <label className="mb-1 block text-[12px] text-muted">{f.label}</label>
              <input value={cfg[f.key] ?? ""} onChange={(e) => setCfg({ ...cfg, [f.key]: e.target.value })} placeholder={f.placeholder}
                className="w-full rounded-md border border-edge bg-elevated px-3 py-2 font-mono text-[13px] text-ink placeholder-subtle outline-none focus:border-accent/60" />
            </div>
          ))}
          <div>
            <label className="mb-1 block text-[12px] text-muted">API token</label>
            <input type="password" value={token} onChange={(e) => setToken(e.target.value)} placeholder="paste token"
              className="w-full rounded-md border border-edge bg-elevated px-3 py-2 font-mono text-[13px] text-ink placeholder-subtle outline-none focus:border-accent/60" />
            <div className="mt-1 text-[11px] text-subtle">{TOKEN_HINT[c.source]}</div>
          </div>
          <button onClick={() => { onConnect(c.source, token, cfg); setOpen(false); setToken(""); }} disabled={!!busy}
            className="rounded-md bg-accent px-3 py-1.5 text-[13px] font-semibold text-black hover:brightness-110 disabled:opacity-50">
            {busy === `connect:${c.source}` ? "Saving…" : "Save connection"}
          </button>
        </div>
      )}
    </div>
  );
}

/* =============================== Issues =============================== */

function Issues({ issues, onRelated }: { issues: Ev[] | null; onRelated: (id: string) => void }) {
  const [tab, setTab] = useState<"issue" | "pull_request">("issue");
  if (issues === null) return <SkeletonRows />;
  if (issues.length === 0) return <Empty>No events yet. Connect a repo and hit <span className="text-ink">Sync</span>.</Empty>;

  const shown = issues.filter((e) => e.type === tab);
  const counts = { issue: issues.filter((e) => e.type === "issue").length, pull_request: issues.filter((e) => e.type === "pull_request").length };

  return (
    <>
      <div className="mb-3 flex gap-1 rounded-lg border border-edge bg-panel p-1">
        {(["issue", "pull_request"] as const).map((t) => (
          <button key={t} onClick={() => setTab(t)}
            className={`flex-1 rounded-md px-3 py-1.5 text-[13px] transition ${tab === t ? "bg-elevated font-medium text-ink" : "text-muted hover:text-ink"}`}>
            {t === "issue" ? "Issues" : "Pull requests"} <span className="font-mono text-[11px] text-subtle">{counts[t]}</span>
          </button>
        ))}
      </div>

      {shown.length === 0 ? <Empty>Nothing here.</Empty> : (
        <div className="overflow-hidden rounded-lg border border-edge bg-panel">
          {shown.map((e, i) => {
            const open = e.metadata?.state === "open";
            return (
              <div key={e.id} className={`px-4 py-3 ${i > 0 ? "border-t border-edge" : ""} hover:bg-elevated`}>
                <div className="flex items-center gap-2 text-[12px]">
                  <span className="font-mono text-subtle">#{e.metadata?.number ?? "?"}</span>
                  <Pill color={open ? "#3fb950" : "#8b8b8b"} tint={open ? "rgba(63,185,80,0.15)" : "rgba(139,139,139,0.15)"}>{e.metadata?.state ?? "?"}</Pill>
                  {(e.metadata?.labels ?? []).slice(0, 3).map((l: any) => (
                    <Pill key={l.name ?? l} color="#58a6ff" tint="rgba(88,166,255,0.12)">{l.name ?? l}</Pill>
                  ))}
                  {!e.metadata?.assignee && <Pill color="#d29922" tint="rgba(210,153,34,0.15)">unassigned</Pill>}
                  <span className="ml-auto text-subtle">{e.actor.name} · {relativeTime(e.timestamp)}</span>
                </div>
                <p className="mt-1.5 line-clamp-2 text-[13.5px] leading-relaxed text-ink/85">{e.content}</p>
                <div className="mt-2 flex items-center gap-3">
                  {e.metadata?.url && <a href={e.metadata.url} target="_blank" rel="noreferrer" className="font-mono text-[11px] text-info hover:underline">open on github ↗</a>}
                  <button onClick={() => onRelated(e.id)} className="font-mono text-[11px] text-subtle hover:text-ink">related cluster →</button>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </>
  );
}

/* =============================== Search =============================== */

function Search({ query, setQuery, run, results, searched, onRelated }: any) {
  return (
    <>
      <form onSubmit={(e) => { e.preventDefault(); run(query); }}>
        <div className="flex items-center gap-2.5 rounded-lg border border-edge bg-elevated px-3.5 py-2.5 focus-within:border-accent/60">
          <span className="text-muted">{I.search()}</span>
          <input autoFocus value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Search every event across your connected tools…"
            className="w-full bg-transparent text-[14px] text-ink placeholder-subtle outline-none" />
          <button type="submit" className="rounded-md bg-accent px-3 py-1.5 text-[13px] font-semibold text-black hover:brightness-110">Search</button>
        </div>
      </form>

      {!searched && <div className="mt-6"><Empty>Hybrid semantic + full-text ranking over real ingested events.</Empty></div>}
      {searched && results.length === 0 && <div className="mt-6"><SkeletonRows /></div>}

      {results.length > 0 && (
        <div className="mt-5 overflow-hidden rounded-lg border border-edge bg-panel">
          {results.map((r: Result, i: number) => {
            const meta = src(r.source);
            return (
              <div key={r.id} className={`px-4 py-3 ${i > 0 ? "border-t border-edge" : ""} hover:bg-elevated`}>
                <div className="flex items-center gap-2 text-[12px]">
                  <span className="grid h-5 w-5 place-items-center rounded text-[10px] font-bold" style={{ background: meta.tint, color: meta.color }}>{meta.letter}</span>
                  <span className="font-mono text-subtle">{r.id}</span>
                  <span className="text-subtle">{r.type}</span>
                  <span className="ml-auto font-mono text-subtle">score {r.score.toFixed(3)}</span>
                </div>
                <p className="mt-1.5 line-clamp-2 text-[13.5px] leading-relaxed text-ink/85">{r.content}</p>
                <button onClick={() => onRelated(r.id)} className="mt-2 font-mono text-[11px] text-subtle hover:text-ink">related cluster →</button>
              </div>
            );
          })}
        </div>
      )}
    </>
  );
}

/* =============================== Related =============================== */

function Related({ graph, center, onRecenter }: { graph: GraphResp | null; center: string | null; onRecenter: (id: string) => void }) {
  if (!center) return <Empty>Open a result or issue, then choose <span className="text-ink">related cluster</span>.</Empty>;
  if (!graph) return <SkeletonRows />;

  const eventNodes = graph.nodes.filter((n) => n.source);
  const people = graph.nodes.filter((n) => n.type === "Person");
  const rel = graph.edges.filter((e) => e.type !== "AUTHORED");
  const counts: Record<string, number> = {};
  for (const n of eventNodes) counts[n.source!] = (counts[n.source!] ?? 0) + 1;

  return (
    <>
      <Card className="p-4">
        <div className="text-[14px] font-semibold">These {eventNodes.length} events are the same thing</div>
        <div className="mt-2 flex flex-wrap items-center gap-2">
          {Object.entries(counts).map(([s, c]) => {
            const m = src(s);
            return <Pill key={s} color={m.color} tint={m.tint}>{c} {m.label}</Pill>;
          })}
          <span className="font-mono text-[11px] text-subtle">· {rel.length} links · 2 hops</span>
        </div>
        {people.length > 0 && (
          <div className="mt-3 flex flex-wrap gap-1.5">
            <span className="mr-1 font-mono text-[11px] text-subtle">people:</span>
            {people.map((p) => (
              <span key={p.id} className="rounded-full border border-edge bg-elevated px-2 py-0.5 text-[11px] text-muted">{p.label}</span>
            ))}
          </div>
        )}
      </Card>

      <div className="mt-5 mb-2 text-[13px] font-semibold">Events in this cluster</div>
      <div className="overflow-hidden rounded-lg border border-edge bg-panel">
        {eventNodes.map((n, i) => {
          const m = src(n.source);
          return (
            <button key={n.id} onClick={() => onRecenter(n.id)}
              className={`flex w-full items-start gap-3 px-4 py-3 text-left hover:bg-elevated ${i > 0 ? "border-t border-edge" : ""} ${n.id === center ? "bg-accent/5" : ""}`}>
              <span className="mt-0.5 grid h-6 w-6 place-items-center rounded-md text-[11px] font-bold" style={{ background: m.tint, color: m.color }}>{m.letter}</span>
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2 text-[12px]">
                  <span className="font-mono text-subtle">{n.id}</span>
                  <Pill color={m.color} tint={m.tint}>{n.type}</Pill>
                  {n.id === center && <Pill color="#3fb950" tint="rgba(63,185,80,0.15)">center</Pill>}
                </div>
                <p className="mt-1.5 line-clamp-2 text-[13.5px] text-ink/85">{n.metadata?.content ?? n.label}</p>
              </div>
            </button>
          );
        })}
      </div>

      <div className="mt-5 mb-2 text-[13px] font-semibold">Edges</div>
      <div className="overflow-hidden rounded-lg border border-edge bg-panel">
        {rel.length === 0 ? <div className="px-4 py-6 text-center text-[13px] text-muted">No links yet.</div> :
          rel.slice(0, 40).map((e, i) => (
            <div key={i} className={`flex items-center gap-2 px-4 py-2.5 text-[12px] ${i > 0 ? "border-t border-edge" : ""}`}>
              <span className="max-w-[34%] truncate font-mono text-subtle">{e.src_id}</span>
              <Pill color={EDGE_COLOR[e.type] ?? "#8b8b8b"} tint={`${EDGE_COLOR[e.type] ?? "#8b8b8b"}22`}>{e.type}</Pill>
              <span className="max-w-[34%] truncate font-mono text-subtle">{e.dst_id}</span>
              <span className="ml-auto font-mono text-[11px] text-subtle">w {e.weight.toFixed(2)}</span>
            </div>
          ))}
      </div>
    </>
  );
}

/* ================================ Norms ================================ */

function Norms({ norms }: { norms: Norm[] | null }) {
  if (norms === null) return <SkeletonRows />;
  if (norms.length === 0) return <Empty>No baselines yet. Hit <span className="text-ink">Run analysis</span>.</Empty>;
  return (
    <>
      <div className="mb-3 text-[13px] text-muted">Learned baselines over your real ingested data.</div>
      <div className="grid gap-3 sm:grid-cols-2">
        {norms.map((n) => (
          <Card key={n.metric} className="p-4">
            <div className="text-[12px] font-medium text-muted">{humanMetric(n.metric)}</div>
            <div className="mt-2 flex items-baseline gap-1.5">
              <span className="text-[30px] font-semibold tracking-tight text-accent">{n.median}</span>
              <span className="text-[12px] text-subtle">{n.unit} median</span>
            </div>
            <div className="mt-3 grid grid-cols-3 gap-2 border-t border-edge pt-3 font-mono text-[11px]">
              <div><div className="text-subtle">mean</div><div className="text-ink/80">{n.mean}</div></div>
              <div><div className="text-subtle">std</div><div className="text-ink/80">{n.std}</div></div>
              <div><div className="text-subtle">n</div><div className="text-ink/80">{n.n}</div></div>
            </div>
            {n.n === 0 && <div className="mt-2 text-[11px] text-warn">no observations in window — nothing closed/merged yet</div>}
          </Card>
        ))}
      </div>
    </>
  );
}

/* ================================ Flags ================================ */

/* The human's prepared moves on a flagged situation. "comment on issue" posts
   the AI's analysis on the EXISTING GitHub issue — deliberately no "new issue"
   move: the flag came FROM an issue, and a second one would collide with it. */
const FLAG_MOVES: [string, string][] = [
  ["comment_on_pr", "comment on issue"],
  ["draft_reply", "draft reply"],
  ["page_engineer", "page engineer"],
];

/* Assign…: the same proposal the email carries — free engineers, the AI's pick,
   single-use links — rendered inline so a lost email never blocks assignment. */
function AssignMenu({ s, busy, onAssigned }: any) {
  const [state, setState] = useState<"idle" | "loading" | "open" | "acting">("idle");
  const [proposal, setProposal] = useState<any>(null);

  async function open() {
    setState("loading");
    try {
      const p = await api(`/api/situations/${encodeURIComponent(s.id)}/propose?company_id=default`, { method: "POST" });
      setProposal(p);
      setState("open");
    } catch (e: any) { onAssigned(e.message); setState("idle"); }
  }

  async function pick(link: any) {
    setState("acting");
    try {
      // same single-use token endpoint the email buttons use
      const res = await fetch(new URL(link.url).pathname, { method: "POST" });
      if (!res.ok) throw new Error(`assign failed (HTTP ${res.status})`);
      onAssigned(`${link.label} — assigned, ticket opened`);
    } catch (e: any) { onAssigned(e.message); }
    setState("idle"); setProposal(null);
  }

  if (state === "open" && proposal) {
    if (proposal.status !== "awaiting_project_manager") {
      return (
        <span className="text-[12px] text-muted">
          {proposal.status === "already_owned" ? `already owned by ${proposal.assignee}`
            : proposal.status === "nobody_free" ? "nobody is free right now"
            : proposal.status === "no_team_configured" ? "add engineers in Team first"
            : "no GitHub target on this flag"}
          <button onClick={() => { setState("idle"); setProposal(null); }} className="ml-2 text-subtle hover:text-ink">✕</button>
        </span>
      );
    }
    return (
      <span className="flex flex-wrap items-center gap-2">
        {proposal.links.map((l: any) => (
          <button key={l.label} onClick={() => pick(l)} disabled={!!busy}
            className={`rounded-md border px-2.5 py-1.5 text-[12px] disabled:opacity-50 ${
              l.primary ? "border-accent/70 bg-accent/10 text-accent" : "border-edge bg-elevated text-ink hover:border-accent/60"}`}>
            {l.label}{l.primary ? " ★" : ""}
          </button>
        ))}
        <button onClick={() => { setState("idle"); setProposal(null); }} className="text-[12px] text-subtle hover:text-ink">cancel</button>
      </span>
    );
  }

  return (
    <button onClick={open} disabled={!!busy || state === "loading"}
      className="rounded-md border border-accent/50 bg-accent/10 px-2.5 py-1.5 text-[12px] font-medium text-accent hover:border-accent disabled:opacity-50">
      {state === "loading" ? "finding who is free…" : "assign…"}
    </button>
  );
}

function Flags({ situations, busy, onAct, onRelated, onAssigned }: any) {
  if (situations === null) return <SkeletonRows />;
  if (situations.length === 0) return <Empty>No situations. Hit <span className="text-ink">Run analysis</span> after syncing.</Empty>;

  const active = situations.filter((s: Situation) => s.status !== "resolved");
  const resolved = situations.filter((s: Situation) => s.status === "resolved");

  return (
    <>
      <div className="mb-3 text-[13px] text-muted">
        <span className="text-ink">{active.length} active</span>
        {resolved.length > 0 && <> · {resolved.length} resolved (their issue was closed, labelled or assigned)</>}
        . Each flag cites the events it fired on.
      </div>
      <div className="space-y-3">
        {situations.map((s: Situation) => {
          const sev = SEVERITY[s.severity] ?? SEVERITY.low;
          const done = s.status === "resolved";
          return (
            <Card key={s.id} className={`overflow-hidden ${done ? "opacity-55" : ""}`}>
              <div className="flex items-start gap-3 border-l-2 px-4 py-3" style={{ borderLeftColor: done ? "#3fb950" : sev.color }}>
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    {done
                      ? <Pill color="#3fb950" tint="rgba(63,185,80,0.15)">resolved</Pill>
                      : <Pill color={sev.color} tint={sev.tint}>{s.severity}</Pill>}
                    <span className={`text-[14px] font-medium ${done ? "line-through decoration-subtle" : ""}`}>{s.title}</span>
                    <span className="font-mono text-[11px] text-subtle">{s.rule}</span>
                    <span className="ml-auto font-mono text-[11px] text-subtle">
                      {done ? `resolved ${relativeTime(s.resolved_at ?? undefined)}` : `${s.status} · ${relativeTime(s.created_at)}`}
                    </span>
                  </div>
                  <p className="mt-2 text-[13.5px] leading-relaxed text-ink/85">{s.summary}</p>

                  {s.recommended_action && (
                    <div className="mt-3 rounded-md border border-edge bg-elevated px-3 py-2">
                      <div className="font-mono text-[10px] uppercase tracking-wide text-subtle">recommended</div>
                      <div className="mt-0.5 text-[13px] text-ink/85">{s.recommended_action}</div>
                    </div>
                  )}

                  <div className="mt-3">
                    <div className="mb-1 font-mono text-[10px] uppercase tracking-wide text-subtle">evidence</div>
                    {s.evidence.map((e) => (
                      <div key={e.event_id} className="flex items-center gap-2 text-[12px]">
                        <Pill color={src(e.source).color} tint={src(e.source).tint}>{e.source}</Pill>
                        <button onClick={() => onRelated(e.event_id)} className="font-mono text-subtle hover:text-ink">{e.event_id}</button>
                        {e.url && <a href={e.url} target="_blank" rel="noreferrer" className="font-mono text-info hover:underline">↗</a>}
                      </div>
                    ))}
                  </div>

                  {!done && (
                    <div className="mt-3 flex flex-wrap items-center gap-2">
                      <AssignMenu s={s} busy={busy} onAssigned={onAssigned} />
                      {FLAG_MOVES.map(([a, label]) => (
                        <button key={a} onClick={() => onAct(a, s.id, {})} disabled={!!busy}
                          className="rounded-md border border-edge bg-elevated px-2.5 py-1.5 text-[12px] text-ink hover:border-accent/60 disabled:opacity-50">
                          {label}
                        </button>
                      ))}
                    </div>
                  )}
                </div>
              </div>
            </Card>
          );
        })}
      </div>
    </>
  );
}

/* =========================== Command center =========================== */

function DryRunToggle({ dryRun, busy, onToggle }: { dryRun: boolean; busy: string | null; onToggle: (enabled: boolean) => void }) {
  const [confirming, setConfirming] = useState(false);
  useEffect(() => { setConfirming(false); }, [dryRun]);

  if (dryRun) {
    return (
      <div className="mb-4 rounded-lg border px-4 py-3" style={{ borderColor: "rgba(88,166,255,0.4)", background: "rgba(88,166,255,0.08)" }}>
        <div className="flex flex-wrap items-center gap-3">
          <span className="text-[13px] font-medium text-info">Practice mode</span>
          <span className="flex-1 text-[12px] text-muted">
            The AI decides and prepares every action, but nothing is sent to GitHub. Safe.
          </span>
          {confirming ? (
            <div className="flex items-center gap-2">
              <span className="text-[12px] text-danger">The AI will write to your real repo. Sure?</span>
              <button onClick={() => onToggle(false)} disabled={!!busy}
                className="rounded-md px-3 py-1.5 text-[12px] font-semibold text-black disabled:opacity-50" style={{ background: "#f85149" }}>
                {busy === "dry_run" ? "Switching..." : "Yes, go live"}
              </button>
              <button onClick={() => setConfirming(false)} className="rounded-md border border-edge bg-elevated px-3 py-1.5 text-[12px] text-muted hover:text-ink">
                Cancel
              </button>
            </div>
          ) : (
            <button onClick={() => setConfirming(true)}
              className="rounded-md border border-edge bg-elevated px-3 py-1.5 text-[12px] font-medium text-ink hover:border-accent/60">
              Turn off practice mode
            </button>
          )}
        </div>
      </div>
    );
  }

  return (
    <div className="mb-4 rounded-lg border px-4 py-3" style={{ borderColor: "rgba(248,81,73,0.45)", background: "rgba(248,81,73,0.08)" }}>
      <div className="flex flex-wrap items-center gap-3">
        <span className="flex items-center gap-1.5 text-[13px] font-semibold text-danger">
          <span className="h-2 w-2 animate-pulse rounded-full bg-danger" /> LIVE
        </span>
        <span className="flex-1 text-[12px] text-muted">
          The AI is writing to your real tools on its own. Risky actions still need your approval.
        </span>
        <button onClick={() => onToggle(true)} disabled={!!busy}
          className="rounded-md border border-edge bg-elevated px-3 py-1.5 text-[12px] font-medium text-ink hover:border-accent/60 disabled:opacity-50">
          {busy === "dry_run" ? "Switching..." : "Back to practice mode"}
        </button>
      </div>
    </div>
  );
}

function Commands({ actions, dryRun, busy, onDecide, onToggleDryRun }: any) {
  if (actions === null) return <SkeletonRows />;
  const pending = actions.filter((a: Action) => a.status === "pending_approval");
  const done = actions.filter((a: Action) => a.status !== "pending_approval");

  return (
    <>
      <DryRunToggle dryRun={dryRun} busy={busy} onToggle={onToggleDryRun} />

      <div className="mb-2 flex items-center gap-2 text-[13px] font-semibold">
        Needs review <Badge>{pending.length}</Badge>
      </div>
      {pending.length === 0 ? <Empty>Nothing awaiting approval.</Empty> : (
        <div className="space-y-2">
          {pending.map((a: Action) => (
            <Card key={a.id} className="p-4">
              <div className="flex items-center gap-2 text-[12px]">
                <Pill color={STATUS_COLOR[a.status]} tint={`${STATUS_COLOR[a.status]}22`}>{a.status.replace("_", " ")}</Pill>
                <span className="text-[14px] font-medium text-ink">{a.action.replace(/_/g, " ")}</span>
                <span className="ml-auto font-mono text-subtle">#{a.id} · by {a.requested_by} · {relativeTime(a.requested_at)}</span>
              </div>
              {a.situation_id && <div className="mt-1 font-mono text-[11px] text-subtle">for {a.situation_id}</div>}
              <div className="mt-3 flex gap-2">
                <button onClick={() => onDecide(a.id, "approve")} disabled={!!busy}
                  className="rounded-md bg-accent px-3 py-1.5 text-[12px] font-semibold text-black hover:brightness-110 disabled:opacity-50">Approve</button>
                <button onClick={() => onDecide(a.id, "reject")} disabled={!!busy}
                  className="rounded-md border border-edge bg-elevated px-3 py-1.5 text-[12px] text-muted hover:text-ink disabled:opacity-50">Reject</button>
              </div>
            </Card>
          ))}
        </div>
      )}

      <div className="mt-6 mb-2 flex items-center gap-2 text-[13px] font-semibold">
        Actions taken <Badge>{done.length}</Badge>
      </div>
      {done.length === 0 ? <Empty>No actions yet.</Empty> : (
        <div className="overflow-hidden rounded-lg border border-edge bg-panel">
          {done.map((a: Action, i: number) => {
            const byAi = a.requested_by === "ai" && !a.decided_by;
            return (
              <div key={a.id} className={`px-4 py-3 ${i > 0 ? "border-t border-edge" : ""}`}>
                <div className="flex items-center gap-2 text-[12px]">
                  <Pill color={STATUS_COLOR[a.status] ?? "#8b8b8b"} tint={`${STATUS_COLOR[a.status] ?? "#8b8b8b"}22`}>{a.status.replace("_", " ")}</Pill>
                  <span className="text-[13px] text-ink">{a.action.replace(/_/g, " ")}</span>
                  {byAi && <Pill color="#3fb950" tint="rgba(63,185,80,0.15)">autonomous</Pill>}
                  <span className="ml-auto font-mono text-subtle">
                    #{a.id} · {a.decided_by ? `approved by ${a.decided_by}` : `by ${a.requested_by}`}
                    {a.params?.confidence != null && ` · conf ${a.params.confidence}`}
                  </span>
                </div>
                <div className="mt-1 text-[12px] text-muted">{a.detail}</div>
                {a.params?.rationale && (
                  <div className="mt-1 text-[12px] italic text-subtle">“{a.params.rationale}”</div>
                )}
                {a.result?.would_send && (
                  <pre className="mt-2 overflow-x-auto rounded-md border border-edge bg-canvas p-2 font-mono text-[11px] text-subtle">
{JSON.stringify(a.result.would_send, null, 2)}
                  </pre>
                )}
              </div>
            );
          })}
        </div>
      )}
    </>
  );
}
