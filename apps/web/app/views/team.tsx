"use client";

import { useCallback, useEffect, useState } from "react";
import { Dot, Page, ROLE_LABELS, ROLE_NOTE, Session, TeamMember, api, relativeTime } from "../lib";

/* Who is in this workspace, and how somebody else gets in.
   Invites are shown ONCE: only a hash is stored, so a code that scrolls away
   cannot be looked up again — it has to be re-issued. The UI says so rather
   than letting someone assume they can come back for it. */

type Props = { session: Session; onSwitched: () => void; flash: (m: string) => void };

export default function TeamView({ session, onSwitched, flash }: Props) {
  const [members, setMembers] = useState<TeamMember[] | null>(null);
  const [canInvite, setCanInvite] = useState(false);
  const [email, setEmail] = useState("");
  const [role, setRole] = useState("member");
  const [issued, setIssued] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [newWorkspace, setNewWorkspace] = useState("");

  const load = useCallback(async () => {
    try {
      const r = await api(`/api/team`);
      setMembers(r.members);
      setCanInvite(r.can_invite);
    } catch { setMembers([]); }
  }, []);

  useEffect(() => { void load(); }, [load, session.workspace.company_id]);

  async function invite() {
    if (!email.trim() || busy) return;
    setBusy(true);
    try {
      const r = await api(`/api/team/invite`, {
        method: "POST", body: JSON.stringify({ email: email.trim(), role }),
      });
      setIssued(r.invite_token);
      setEmail("");
      await load();
    } catch (e: any) { flash(e.message); } finally { setBusy(false); }
  }

  async function switchTo(companyId: string) {
    try {
      await api(`/api/auth/switch`, { method: "POST", body: JSON.stringify({ company_id: companyId }) });
      onSwitched();
    } catch (e: any) { flash(e.message); }
  }

  async function createWorkspace() {
    if (!newWorkspace.trim()) return;
    try {
      await api(`/api/auth/workspaces`, { method: "POST", body: JSON.stringify({ name: newWorkspace.trim() }) });
      setNewWorkspace("");
      onSwitched();
    } catch (e: any) { flash(e.message); }
  }

  const active = session.workspace.company_id;

  return (
    <Page
      title="Team"
      purpose={ROLE_NOTE}
      stats={[
        { label: members?.length === 1 ? "person" : "people", value: members?.length ?? "—", tone: "ok" },
        { label: session.workspaces.length === 1 ? "workspace" : "workspaces",
          value: session.workspaces.length, tone: "ok" },
      ]}
    >

      {/* members */}
      <div className="rounded-lg border border-edge bg-panel">
        <div className="border-b border-edge px-[18px] py-2.5 text-[11px] font-semibold tracking-[1px] text-muted">
          IN THIS WORKSPACE
        </div>
        {members === null ? (
          <div className="px-[18px] py-3 text-[12.5px] text-subtle">Loading…</div>
        ) : (
          members.map((m) => (
            <div key={m.id} className="flex items-center gap-3 border-b border-edge px-[18px] py-2.5 last:border-b-0">
              <Dot color={m.id === session.user.id ? "#3fb950" : "#6a6a6a"} size={7} />
              <span className="text-[13px] text-ink">{m.name}</span>
              {m.id === session.user.id && <span className="text-[11px] text-subtle">you</span>}
              <span className="flex-1 truncate text-[12px] text-subtle">{m.email}</span>
              <span className="rounded-full border border-edge px-2 py-[2px] text-[10.5px] text-muted">
                {ROLE_LABELS[m.role] ?? m.role}
              </span>
              <span className="w-[92px] text-right text-[11px] text-subtle">
                {m.last_login_at ? relativeTime(m.last_login_at) : "never signed in"}
              </span>
            </div>
          ))
        )}
      </div>

      {/* invite */}
      {canInvite && (
        <div className="mt-5 rounded-lg border border-edge bg-panel px-[18px] py-3.5">
          <div className="mb-2.5 text-[11px] font-semibold tracking-[1px] text-muted">INVITE SOMEONE</div>
          <div className="flex flex-wrap items-center gap-2">
            <input
              value={email} onChange={(e) => setEmail(e.target.value)}
              placeholder="their@email.com" type="email"
              className="w-[240px] rounded-md border border-edge bg-elevated px-2.5 py-1.5 text-[13px] text-ink outline-none placeholder:text-subtle"
            />
            <select
              value={role} onChange={(e) => setRole(e.target.value)} aria-label="Role"
              className="rounded-md border border-edge bg-elevated px-2.5 py-1.5 text-[13px] text-ink outline-none"
            >
              <option value="member">Member</option>
              <option value="manager">Manager</option>
              <option value="owner">Owner</option>
            </select>
            <button
              onClick={invite} disabled={busy || !email.trim()}
              className="rounded-md border-none bg-accent px-3 py-1.5 text-[12.5px] font-semibold text-[#0a0a0a] hover:opacity-90 disabled:opacity-40"
            >
              {busy ? "Creating…" : "Create invite"}
            </button>
          </div>

          {issued && (
            <div className="mt-3 rounded-md border border-edge bg-elevated px-3 py-2.5">
              <div className="mb-1 text-[11.5px] text-warn">
                Copy this now — it is shown once and cannot be recovered.
              </div>
              <code className="block break-all font-mono text-[12px] text-ink">{issued}</code>
              <button
                onClick={() => { void navigator.clipboard?.writeText(issued); flash("Invite code copied"); }}
                className="mt-2 border-none bg-transparent p-0 text-[12px] text-accent underline"
              >
                Copy code
              </button>
            </div>
          )}
        </div>
      )}

      {/* workspaces */}
      <div className="mt-5 rounded-lg border border-edge bg-panel px-[18px] py-3.5">
        <div className="mb-2.5 text-[11px] font-semibold tracking-[1px] text-muted">YOUR WORKSPACES</div>
        {session.workspaces.map((w) => (
          <div key={w.company_id} className="flex items-center gap-3 border-b border-edge py-2 last:border-b-0">
            <Dot color={w.company_id === active ? "#3fb950" : "#6a6a6a"} size={7} />
            <span className="text-[13px] text-ink">{w.name}</span>
            <span className="flex-1 text-[11px] text-subtle">{ROLE_LABELS[w.role] ?? w.role}</span>
            {w.company_id === active ? (
              <span className="text-[11.5px] text-subtle">current</span>
            ) : (
              <button
                onClick={() => switchTo(w.company_id)}
                className="border-none bg-transparent p-0 text-[12px] text-accent underline"
              >
                Switch
              </button>
            )}
          </div>
        ))}
        <div className="mt-3 flex items-center gap-2">
          <input
            value={newWorkspace} onChange={(e) => setNewWorkspace(e.target.value)}
            placeholder="New workspace name — a different team or client"
            className="flex-1 rounded-md border border-edge bg-elevated px-2.5 py-1.5 text-[13px] text-ink outline-none placeholder:text-subtle"
          />
          <button
            onClick={createWorkspace} disabled={!newWorkspace.trim()}
            className="rounded-md border border-edge bg-transparent px-3 py-1.5 text-[12.5px] text-muted hover:text-ink disabled:opacity-40"
          >
            Create
          </button>
        </div>
      </div>
    </Page>
  );
}
