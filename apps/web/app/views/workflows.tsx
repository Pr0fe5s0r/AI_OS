"use client";

import { useState } from "react";
import {
  Dot, StepApp, Workflow, WorkflowRun, WorkflowStep, WorkflowStepResult,
  api, COMPANY, appColor, relativeTime, runStatusColor, triggerLabel,
} from "../lib";
import { WorkflowGraph } from "./workflow-graph";

/* The Workflows section: your saved agentic workflows. Creation happens in the
   Agent (a whole workflow from one prompt); here you see them as n8n-style
   graphs, run them, and — per workflow — talk to a chat that edits THAT flow.
   Light controls (enable/disable a node, reorder) sit on the graph; everything
   deeper is by prompt. */

type Props = { workflows: Workflow[]; onChanged: () => void; onNewWorkflow: () => void };

function AppsRow({ apps }: { apps: StepApp[] | undefined }) {
  if (!apps || apps.length === 0) return null;
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {apps.map((a) => (
        <span key={a.app} className="inline-flex items-center gap-[5px] text-[11px] text-muted">
          <Dot color={appColor(a.app)} size={5} />
          <span className="capitalize">{a.app}</span>
        </span>
      ))}
    </div>
  );
}

function StepResultLine({ r }: { r: WorkflowStepResult }) {
  const action = r.args?.action as string | undefined;
  return (
    <div className="flex items-baseline gap-2.5 py-1">
      <Dot color={runStatusColor(r.status)} size={6} />
      <span className="min-w-0 flex-1 truncate text-[12px] text-muted">
        {r.tool}{action ? ` · ${action}` : ""}
        {r.detail && <span className="text-subtle"> — {r.detail}</span>}
      </span>
      <span className="font-mono text-[11px]" style={{ color: runStatusColor(r.status) }}>{r.status}</span>
    </div>
  );
}

/* ------------------------------- edit chat ------------------------------- */

type EditMsg = { role: "you" | "agent"; text: string };

function EditChat({ workflowId, onApplied }: {
  workflowId: number; onApplied: (wf: Workflow) => void;
}) {
  const [msgs, setMsgs] = useState<EditMsg[]>([]);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);

  async function send() {
    const t = text.trim();
    if (!t || busy) return;
    setText("");
    setMsgs((m) => [...m, { role: "you", text: t }]);
    setBusy(true);
    try {
      const r = await api(`/api/workflows/${workflowId}/edit?company_id=${COMPANY}`, {
        method: "POST", body: JSON.stringify({ instruction: t }),
      });
      if (r.applied) {
        onApplied(r.workflow);
        setMsgs((m) => [...m, { role: "agent", text: "Done — updated the workflow above." }]);
      } else {
        setMsgs((m) => [...m, { role: "agent", text: (r.clarifications || ["I need a bit more detail."]).join(" ") }]);
      }
    } catch (e: any) {
      setMsgs((m) => [...m, { role: "agent", text: `Couldn't apply that: ${e.message}` }]);
    } finally { setBusy(false); }
  }

  return (
    <div className="rounded-lg border border-edge bg-panel">
      <div className="border-b border-edge px-3.5 py-2.5 text-[11px] font-semibold tracking-[1px] text-muted">
        EDIT THIS WORKFLOW BY CHAT
      </div>
      <div className="flex flex-col gap-2 px-3.5 py-3">
        {msgs.length === 0 && (
          <div className="text-[12px] text-subtle">
            e.g. “also assign it to me”, “change the trigger to every morning”, “remove the last step”.
          </div>
        )}
        {msgs.map((m, i) => (
          <div key={i} className={m.role === "you" ? "self-end rounded-lg border border-edge bg-elevated px-3 py-1.5 text-[12.5px]" : "self-start text-[12.5px] text-muted"}>
            {m.text}
          </div>
        ))}
        {busy && <div className="self-start text-[12px] text-subtle">thinking…</div>}
      </div>
      <div className="flex items-center gap-2 border-t border-edge px-2.5 py-2">
        <input
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); void send(); } }}
          placeholder="Tell the agent how to change this workflow…"
          className="flex-1 border-none bg-transparent px-1.5 py-1 text-[13px] text-ink outline-none placeholder:text-subtle"
        />
        <button onClick={() => void send()} disabled={busy || !text.trim()}
                className="rounded-md border-none bg-accent px-3 py-1.5 text-[12.5px] font-semibold text-[#0a0a0a] hover:opacity-90 disabled:opacity-40">
          Send
        </button>
      </div>
    </div>
  );
}

/* ------------------------------ detail view ------------------------------ */

function WorkflowDetail({ initial, onBack, onChanged, flash }: {
  initial: Workflow; onBack: () => void; onChanged: () => void; flash: (m: string) => void;
}) {
  const [wf, setWf] = useState<Workflow>(initial);
  const [lastRun, setLastRun] = useState<{ status: string; summary: string; step_results: WorkflowStepResult[] } | null>(null);
  const [runs, setRuns] = useState<WorkflowRun[] | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  async function patchSteps(steps: WorkflowStep[]) {
    // optimistic; the response carries fresh app_info so we adopt it
    setWf((w) => ({ ...w, steps }));
    try {
      const updated = await api(`/api/workflows/${wf.id}?company_id=${COMPANY}`, {
        method: "PATCH", body: JSON.stringify({ steps }),
      });
      setWf(updated);
      onChanged();
    } catch (e: any) { flash(e.message); }
  }

  function onToggle(i: number) {
    const steps = wf.steps.map((s, j) => (j === i ? { ...s, enabled: s.enabled === false } : s));
    void patchSteps(steps);
  }
  function onMove(i: number, dir: -1 | 1) {
    const j = i + dir;
    if (j < 0 || j >= wf.steps.length) return;
    const steps = [...wf.steps];
    [steps[i], steps[j]] = [steps[j], steps[i]];
    void patchSteps(steps);
  }

  async function run() {
    setBusy("run");
    try {
      const r = await api(`/api/workflows/${wf.id}/run?company_id=${COMPANY}`, { method: "POST" });
      setLastRun(r);
      flash(`${wf.name}: ${r.summary}`);
      onChanged();
      if (runs) await loadRuns();
    } catch (e: any) { flash(e.message); } finally { setBusy(null); }
  }
  async function loadRuns() {
    const r = await api(`/api/workflows/${wf.id}/runs?company_id=${COMPANY}`);
    setRuns(r.runs);
  }
  async function toggleEnabled() {
    try {
      const updated = await api(`/api/workflows/${wf.id}?company_id=${COMPANY}`, {
        method: "PATCH", body: JSON.stringify({ enabled: !wf.enabled }),
      });
      setWf(updated); onChanged();
    } catch (e: any) { flash(e.message); }
  }
  async function remove() {
    setBusy("delete");
    try {
      await api(`/api/workflows/${wf.id}?company_id=${COMPANY}`, { method: "DELETE" });
      flash(`Deleted "${wf.name}"`);
      onChanged(); onBack();
    } catch (e: any) { flash(e.message); setBusy(null); }
  }

  return (
    <div className="mx-auto max-w-[760px] px-7 pb-20 pt-9">
      <button onClick={onBack} className="mb-4 border-none bg-transparent p-0 text-[12.5px] text-muted hover:text-ink">← All workflows</button>

      <div className="mb-4 flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <Dot color={wf.enabled ? "#3fb950" : "#6a6a6a"} size={7} />
            <h1 className="m-0 truncate text-[18px] font-semibold text-ink">{wf.name}</h1>
          </div>
          <div className="mt-1 text-[12.5px] text-muted">{wf.goal}</div>
          <div className="mt-1.5 flex items-center gap-3 text-[11.5px] text-subtle">
            <span>{triggerLabel(wf.trigger)}</span>
            {wf.last_run_at && <span>last run {relativeTime(wf.last_run_at)}</span>}
          </div>
        </div>
        <div className="flex flex-none items-center gap-2">
          <button onClick={run} disabled={busy !== null}
                  className="rounded-md border-none bg-accent px-3.5 py-1.5 text-[13px] font-semibold text-[#0a0a0a] hover:opacity-90 disabled:opacity-40">
            {busy === "run" ? "Running…" : "Run now"}
          </button>
          <button onClick={toggleEnabled} className="rounded-md border border-edge bg-transparent px-2.5 py-1.5 text-[12px] text-muted hover:text-ink">
            {wf.enabled ? "Disable" : "Enable"}
          </button>
          <button onClick={remove} disabled={busy !== null} className="border-none bg-transparent p-0 text-[12px] text-subtle hover:text-danger disabled:opacity-40">Delete</button>
        </div>
      </div>

      <div className="mb-3"><AppsRow apps={wf.apps_used} /></div>

      {/* the n8n graph, with light controls */}
      <WorkflowGraph trigger={wf.trigger} steps={wf.steps} controls={{ onToggle, onMove }} />

      {lastRun && (
        <div className="mt-5 rounded-lg border border-edge bg-panel px-[18px] py-3">
          <div className="mb-1 flex items-center gap-2 text-[11px] font-semibold tracking-[1px]" style={{ color: runStatusColor(lastRun.status) }}>
            <Dot color={runStatusColor(lastRun.status)} size={6} /> THIS RUN — {lastRun.summary}
          </div>
          {lastRun.step_results.map((r, i) => <StepResultLine key={i} r={r} />)}
          {lastRun.step_results.length === 0 && <div className="text-[12px] text-subtle">Nothing matched — nothing to do right now.</div>}
        </div>
      )}

      <div className="mt-5"><EditChat workflowId={wf.id} onApplied={(u) => { setWf(u); onChanged(); }} /></div>

      <div className="mt-5">
        <button onClick={() => (runs ? setRuns(null) : loadRuns())} className="border-none bg-transparent p-0 text-[12px] text-muted underline hover:text-ink">
          {runs ? "Hide run history" : "Show run history"}
        </button>
        {runs && (
          <div className="mt-2 rounded-lg border border-edge bg-panel px-[18px] py-2">
            {runs.length > 0 ? runs.map((r) => (
              <div key={r.id} className="flex items-center gap-2.5 border-b border-edge py-[7px] last:border-b-0">
                <Dot color={runStatusColor(r.status)} size={6} />
                <span className="flex-1 truncate text-[12px] text-muted">{r.summary}</span>
                <span className="text-[11px] text-subtle">{r.trigger}</span>
                <span className="text-[11px] text-subtle">{relativeTime(r.started_at)}</span>
              </div>
            )) : <div className="py-2 text-[12px] text-subtle">No runs yet.</div>}
          </div>
        )}
      </div>
    </div>
  );
}

/* -------------------------------- list -------------------------------- */

function WorkflowRow({ wf, onOpen }: { wf: Workflow; onOpen: () => void }) {
  return (
    <button onClick={onOpen} className="w-full rounded-lg border border-edge bg-panel px-[18px] py-3.5 text-left hover:bg-elevated">
      <div className="flex items-center gap-2">
        <Dot color={wf.enabled ? "#3fb950" : "#6a6a6a"} size={7} />
        <span className="truncate text-[13.5px] font-semibold text-ink">{wf.name}</span>
        <span className="ml-auto flex-none rounded-full border border-edge px-2 py-[2px] text-[10.5px] text-muted">
          {triggerLabel(wf.trigger)}
        </span>
      </div>
      <div className="mt-1 truncate text-[12px] text-muted">{wf.goal}</div>
      <div className="mt-2 flex items-center justify-between">
        <AppsRow apps={wf.apps_used} />
        <span className="text-[11px] text-subtle">
          {wf.steps.length} step{wf.steps.length === 1 ? "" : "s"}
          {wf.last_run_at ? ` · ran ${relativeTime(wf.last_run_at)}` : ""}
        </span>
      </div>
    </button>
  );
}

export default function WorkflowsView({ workflows, onChanged, onNewWorkflow }: Props) {
  const [openId, setOpenId] = useState<number | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const flash = (m: string) => { setToast(m); setTimeout(() => setToast(null), 3600); };

  const open = openId != null ? workflows.find((w) => w.id === openId) : undefined;

  if (open) {
    return (
      <>
        <WorkflowDetail initial={open} onBack={() => setOpenId(null)} onChanged={onChanged} flash={flash} />
        {toast && <Toast text={toast} />}
      </>
    );
  }

  return (
    <div className="mx-auto max-w-[760px] px-7 pb-20 pt-9">
      <div className="mb-1 flex items-center justify-between">
        <div className="text-[15px] font-semibold text-ink">Workflows</div>
        <button onClick={onNewWorkflow} className="rounded-md border border-edgeStrong bg-transparent px-3 py-1.5 text-[12.5px] text-ink hover:bg-elevated">
          ＋ New workflow
        </button>
      </div>
      <div className="mb-5 text-[13px] text-muted">
        Saved workflows run on their trigger — manually, on a schedule, or on an event. Open one to
        run it, watch it, or edit it by chat.
      </div>

      {workflows.length > 0 ? (
        <div className="flex flex-col gap-3">
          {workflows.map((wf) => <WorkflowRow key={wf.id} wf={wf} onOpen={() => setOpenId(wf.id)} />)}
        </div>
      ) : (
        <div className="rounded-lg border border-edge bg-panel px-[18px] py-8 text-center">
          <div className="text-[13px] text-muted">No workflows yet.</div>
          <button onClick={onNewWorkflow} className="mt-3 rounded-md border-none bg-accent px-3.5 py-[7px] text-[13px] font-semibold text-[#0a0a0a] hover:opacity-90">
            Create one in the Agent
          </button>
        </div>
      )}

      {toast && <Toast text={toast} />}
    </div>
  );
}

function Toast({ text }: { text: string }) {
  return (
    <div className="toast-in fixed bottom-6 left-[calc(50%+80px)] z-50 flex -translate-x-1/2 items-center gap-2 rounded-lg border border-edge bg-elevated px-4 py-2.5 text-[13px] text-ink">
      {text}
    </div>
  );
}
