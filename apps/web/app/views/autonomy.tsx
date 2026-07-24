"use client";

import { useCallback, useEffect, useState } from "react";
import { Page, api, humanize } from "../lib";

/* What the AI may do on its own while you're away, and who it can hand work to.
   Going Live stops being a mystery: the allow-list and the roster are right
   here, in plain sight, instead of buried in a profile row. */

type Member = {
  id: string; name: string; email: string;
  roles: string[]; skills: string[]; max_open_issues: number; assignable: boolean;
};
type AvailableAction = { name: string; public: boolean; kind: string | null };
type Autonomy = {
  enabled: boolean; allowed_actions: string[]; min_confidence: number;
  escalate_severities: string[]; allow_public_actions: boolean;
};

type Props = { flash: (m: string) => void };

const SEVERITIES = ["critical", "high", "medium", "low"];

export default function AutonomyView({ flash }: Props) {
  const [live, setLive] = useState(false);
  const [auto, setAuto] = useState<Autonomy | null>(null);
  const [actions, setActions] = useState<AvailableAction[]>([]);
  const [roster, setRoster] = useState<Member[] | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const r = await api(`/api/autonomy`);
      setLive(r.live); setAuto(r.autonomy); setActions(r.available_actions ?? []); setRoster(r.roster ?? []);
    } catch (e: any) { flash(e.message); }
  }, [flash]);
  useEffect(() => { void load(); }, [load]);

  async function saveAutonomy(next: Autonomy) {
    setAuto(next);
    try { await api(`/api/autonomy`, { method: "POST", body: JSON.stringify(next) }); }
    catch (e: any) { flash(e.message); void load(); }
  }
  function toggleAction(name: string) {
    if (!auto) return;
    const has = auto.allowed_actions.includes(name);
    void saveAutonomy({ ...auto, allowed_actions: has ? auto.allowed_actions.filter((a) => a !== name) : [...auto.allowed_actions, name] });
  }
  function toggleSeverity(sev: string) {
    if (!auto) return;
    const has = auto.escalate_severities.includes(sev);
    void saveAutonomy({ ...auto, escalate_severities: has ? auto.escalate_severities.filter((s) => s !== sev) : [...auto.escalate_severities, sev] });
  }

  async function saveRoster(next: Member[]) {
    setRoster(next); setBusy(true);
    try { await api(`/api/roster`, { method: "POST", body: JSON.stringify({ roster: next }) }); flash("Roster saved"); }
    catch (e: any) { flash(e.message); void load(); } finally { setBusy(false); }
  }

  return (
    <Page
      title="Autonomy"
      purpose="What the AI may do on its own while you're away — and who it can hand work to."
      stats={[
        { label: live ? "Live" : "Practice", value: live ? "on" : "off", tone: live ? "waiting" : "idle" },
        { label: "allowed actions", value: auto?.allowed_actions.length ?? "—", tone: "ok" },
        { label: "people", value: roster?.length ?? "—", tone: "ok" },
      ]}
    >
      {!live && (
        <div className="mb-4 rounded-lg border border-edge bg-panel px-4 py-2.5 text-[12.5px] text-muted">
          You’re in <b className="text-ink">Practice</b> — the AI won’t act on its own yet. These settings take effect the moment you go Live (top banner).
        </div>
      )}

      {/* allow-list */}
      <div className="rounded-lg border border-edge bg-panel">
        <div className="border-b border-edge px-[18px] py-2.5 text-[11px] font-semibold tracking-[1px] text-muted">
          THE AI MAY DO ON ITS OWN
        </div>
        {auto === null ? <div className="px-[18px] py-3 text-[12.5px] text-subtle">Loading…</div> : (
          <>
            <div className="flex flex-wrap gap-2 px-[18px] py-3">
              {actions.length === 0 && <span className="text-[12.5px] text-subtle">Connect a tool to see the actions it can take.</span>}
              {actions.map((a) => {
                const on = auto.allowed_actions.includes(a.name);
                return (
                  <button key={a.name} onClick={() => toggleAction(a.name)}
                    className={`flex items-center gap-1.5 rounded-md border px-2.5 py-1.5 text-[12.5px] transition-colors ${on ? "border-accent bg-accent/10 text-ink" : "border-edge text-muted hover:text-ink"}`}>
                    <span className={`inline-block h-3 w-3 rounded-sm border ${on ? "border-accent bg-accent" : "border-edge"}`} />
                    {humanize(a.name)}
                    {a.public && <span className="rounded bg-edge px-1 py-[1px] text-[9.5px] uppercase tracking-wide text-warn">public</span>}
                  </button>
                );
              })}
            </div>

            <div className="border-t border-edge px-[18px] py-3">
              <label className="flex items-center justify-between gap-4 text-[13px] text-ink">
                <span>Confidence needed to act alone
                  <span className="ml-2 text-[12px] text-subtle">below this, it asks a person</span></span>
                <span className="flex items-center gap-2">
                  <input type="range" min={0.5} max={0.95} step={0.05} value={auto.min_confidence}
                    onChange={(e) => setAuto({ ...auto, min_confidence: +e.target.value })}
                    onMouseUp={() => saveAutonomy(auto)} onTouchEnd={() => saveAutonomy(auto)} />
                  <span className="w-9 text-right font-mono text-[12.5px]">{Math.round(auto.min_confidence * 100)}%</span>
                </span>
              </label>
            </div>

            <div className="border-t border-edge px-[18px] py-3">
              <div className="mb-1.5 text-[12.5px] text-ink">Always escalate to a person (never act alone) at severity:</div>
              <div className="flex flex-wrap gap-2">
                {SEVERITIES.map((sev) => {
                  const on = auto.escalate_severities.includes(sev);
                  return (
                    <button key={sev} onClick={() => toggleSeverity(sev)}
                      className={`rounded-md border px-2.5 py-1 text-[12px] capitalize ${on ? "border-danger text-danger" : "border-edge text-muted hover:text-ink"}`}>
                      {sev}
                    </button>
                  );
                })}
              </div>
            </div>

            <div className="border-t border-edge px-[18px] py-3">
              <label className="flex cursor-pointer items-center gap-2.5 text-[13px] text-ink">
                <input type="checkbox" checked={auto.allow_public_actions}
                  onChange={(e) => saveAutonomy({ ...auto, allow_public_actions: e.target.checked })} />
                Let it post in public unattended (comments, reviews, new issues) without asking first
              </label>
            </div>
          </>
        )}
      </div>

      {/* roster */}
      <div className="mt-6 rounded-lg border border-edge bg-panel">
        <div className="flex items-center justify-between border-b border-edge px-[18px] py-2.5">
          <span className="text-[11px] font-semibold tracking-[1px] text-muted">WHO IT CAN ASSIGN TO</span>
          <span className="text-[11px] text-subtle">skills are what the matcher reasons over</span>
        </div>
        {roster === null ? <div className="px-[18px] py-3 text-[12.5px] text-subtle">Loading…</div> : (
          <>
            {roster.map((m, i) => (
              <MemberRow key={m.id + i} m={m}
                onChange={(next) => saveRoster(roster.map((x, j) => (j === i ? next : x)))}
                onRemove={() => saveRoster(roster.filter((_, j) => j !== i))} busy={busy} />
            ))}
            {roster.length === 0 && <div className="px-[18px] py-3 text-[12.5px] text-subtle">No one on the roster yet — add the people the AI can assign work to.</div>}
            <AddMember onAdd={(m) => saveRoster([...(roster ?? []), m])} existing={roster.map((m) => m.id)} />
          </>
        )}
      </div>
    </Page>
  );
}

function MemberRow({ m, onChange, onRemove, busy }: { m: Member; onChange: (m: Member) => void; onRemove: () => void; busy: boolean }) {
  return (
    <div className="flex flex-wrap items-center gap-2 border-b border-edge px-[18px] py-2.5 last:border-b-0">
      <span className="w-[120px] truncate text-[13px] text-ink" title={m.id}>{m.name || m.id}</span>
      <span className="w-[150px] truncate text-[12px] text-subtle">{m.email || "no email"}</span>
      <span className="flex flex-1 flex-wrap gap-1">
        {m.skills.map((s) => <span key={s} className="rounded bg-edge px-1.5 py-[1px] text-[10.5px] text-muted">{s}</span>)}
        {m.skills.length === 0 && <span className="text-[11px] text-subtle">no skills set</span>}
      </span>
      <span className="text-[11px] text-subtle">{(m.roles[0] ?? "—")}</span>
      <label className="flex items-center gap-1 text-[11px] text-subtle">
        <input type="checkbox" checked={m.assignable} onChange={(e) => onChange({ ...m, assignable: e.target.checked })} /> assignable
      </label>
      <button onClick={onRemove} disabled={busy} className="text-[11.5px] text-subtle underline hover:text-danger disabled:opacity-40">remove</button>
    </div>
  );
}

function AddMember({ onAdd, existing }: { onAdd: (m: Member) => void; existing: string[] }) {
  const [id, setId] = useState(""); const [name, setName] = useState("");
  const [email, setEmail] = useState(""); const [role, setRole] = useState("dev"); const [skills, setSkills] = useState("");
  function add() {
    const login = id.trim();
    if (!login || existing.includes(login)) return;
    onAdd({
      id: login, name: name.trim() || login, email: email.trim(), roles: [role],
      skills: skills.split(",").map((s) => s.trim()).filter(Boolean), max_open_issues: 3, assignable: true,
    });
    setId(""); setName(""); setEmail(""); setSkills("");
  }
  const inp = "rounded-md border border-edge bg-elevated px-2.5 py-1.5 text-[12.5px] text-ink outline-none placeholder:text-subtle";
  return (
    <div className="flex flex-wrap items-center gap-2 px-[18px] py-3">
      <input value={id} onChange={(e) => setId(e.target.value)} placeholder="login (used to assign)" className={`${inp} w-[150px]`} />
      <input value={name} onChange={(e) => setName(e.target.value)} placeholder="name" className={`${inp} w-[120px]`} />
      <input value={email} onChange={(e) => setEmail(e.target.value)} placeholder="email" type="email" className={`${inp} w-[160px]`} />
      <select value={role} onChange={(e) => setRole(e.target.value)} aria-label="Role" className={inp}>
        {["owner", "lead", "dev", "designer", "qa"].map((r) => <option key={r} value={r}>{r}</option>)}
      </select>
      <input value={skills} onChange={(e) => setSkills(e.target.value)} placeholder="skills, comma, separated" className={`${inp} flex-1 min-w-[160px]`} />
      <button onClick={add} disabled={!id.trim()} className="rounded-md border-none bg-accent px-3 py-1.5 text-[12.5px] font-semibold text-[#0a0a0a] hover:opacity-90 disabled:opacity-40">Add</button>
    </div>
  );
}
