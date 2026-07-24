"use client";

import { useState } from "react";
import {
  CRON_PRESETS, Dot, StepApp, Workflow, WorkflowRun, WorkflowStep, WorkflowStepResult,
  WorkflowTrigger, api, appColor, Empty, isValidCron, nextRunLabel, Page, PrimaryBtn,
  relativeTime, runStatusColor, SecondaryBtn, streamPost, triggerLabel,
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

/* What a single step actually DID, in words a person can check.
   A step is not just "comment on issue — pending": it acted on a SPECIFIC
   record, with a SPECIFIC argument, or it read the data and found a count.
   Without those, run history is a row of identical grey lines and "what did
   this run do?" has no answer. Everything shown here is already in the step's
   own args/result — we were just dropping it. */
function stepSubject(r: WorkflowStepResult): string | null {
  // an action fanned out over records: which record, and with what
  const target = (r.args?.situation_id ?? r.args?.record_id ?? r.args?.target) as string | undefined;
  const argument = r.args?.argument as string | undefined;
  if (target || argument) {
    return [target, argument && `“${argument}”`].filter(Boolean).join(" · ");
  }
  // a read step: say how much it found, so a fan-out of zero is legible
  const found = r.result?.situations ?? r.result?.results ?? r.result?.records;
  if (Array.isArray(found)) return `found ${found.length}`;
  return null;
}

function StepResultLine({ r }: { r: WorkflowStepResult }) {
  const action = r.args?.action as string | undefined;
  const subject = stepSubject(r);
  return (
    <div className="flex items-baseline gap-2.5 py-1">
      <Dot color={runStatusColor(r.status)} size={6} />
      <span className="min-w-0 flex-1 truncate text-[12px] text-muted">
        {r.tool}{action ? ` · ${action}` : ""}
        {subject && <span className="text-ink"> {subject}</span>}
        {r.detail && <span className="text-subtle"> — {r.detail}</span>}
      </span>
      <span className="font-mono text-[11px]" style={{ color: runStatusColor(r.status) }}>{r.status}</span>
    </div>
  );
}

/* One historical run, expandable. The summary line is the same one you saw
   before; clicking it reveals the per-step detail the live "THIS RUN" panel
   already showed — so a run you did yesterday is as inspectable as one you
   just watched. */
function RunRow({ run }: { run: WorkflowRun }) {
  const [open, setOpen] = useState(false);
  const steps = run.step_results ?? [];
  return (
    <div className="border-b border-edge py-[7px] last:border-b-0">
      <button
        onClick={() => steps.length && setOpen(!open)}
        className="flex w-full items-center gap-2.5 border-none bg-transparent p-0 text-left"
        style={{ cursor: steps.length ? "pointer" : "default" }}
      >
        {steps.length > 0 && (
          <svg width="9" height="9" viewBox="0 0 10 10" fill="none" stroke="currentColor" strokeWidth="1.6"
            strokeLinecap="round" strokeLinejoin="round" className="flex-none text-subtle"
            style={{ transform: open ? "rotate(90deg)" : "none", transition: "transform 120ms ease" }}>
            <path d="M3 1.5 7 5l-4 3.5" />
          </svg>
        )}
        <Dot color={runStatusColor(run.status)} size={6} />
        <span className="flex-1 truncate text-[12px] text-muted">{run.summary}</span>
        <span className="text-[11px] text-subtle">{run.trigger}</span>
        <span className="text-[11px] text-subtle">{relativeTime(run.started_at)}</span>
      </button>
      {open && (
        <div className="mt-1 border-l border-edge pl-3">
          {steps.map((s, i) => <StepResultLine key={i} r={s} />)}
        </div>
      )}
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
      const r = await api(`/api/workflows/${workflowId}/edit`, {
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

/* ----------------------------- trigger editor -----------------------------
   "When does this run?" is the first question anyone asks of a saved workflow,
   so it gets a direct control rather than only being settable by prompt. Three
   kinds, matching the engine exactly. A schedule shows its next real fire time
   before you commit — the difference between setting a schedule and trusting
   one. */

const SEVERITIES = ["", "high", "medium", "low"];

function TriggerEditor({ trigger, onSave, onCancel, busy }: {
  trigger: WorkflowTrigger;
  onSave: (t: WorkflowTrigger) => void;
  onCancel: () => void;
  busy: boolean;
}) {
  const [type, setType] = useState(trigger?.type || "manual");
  const [cron, setCron] = useState(String(trigger?.config?.cron || "0 9 * * *"));
  const [label, setLabel] = useState(String(trigger?.config?.label || ""));
  const [rule, setRule] = useState(String(trigger?.config?.rule || ""));
  const [severity, setSeverity] = useState(String(trigger?.config?.severity || ""));

  const cronOk = isValidCron(cron);
  const matchedPreset = CRON_PRESETS.find((p) => p.cron === cron);

  function save() {
    if (type === "schedule") {
      if (!cronOk) return;
      onSave({ type, config: { cron, label: label || matchedPreset?.label || cron } });
    } else if (type === "event") {
      const config: Record<string, string> = {};
      if (rule) config.rule = rule;
      if (severity) config.severity = severity;
      onSave({ type, config });
    } else {
      onSave({ type: "manual", config: {} });
    }
  }

  const tabs = [
    { id: "manual", label: "Manual", hint: "You press Run." },
    { id: "schedule", label: "Schedule", hint: "Runs on a repeating cadence." },
    { id: "event", label: "Event", hint: "Runs when something is detected." },
  ];

  return (
    <div className="mt-2 rounded-lg border border-edge bg-panel px-[18px] py-3.5">
      <div className="mb-2.5 text-[11px] font-semibold uppercase tracking-[1px] text-subtle">When should this run?</div>

      <div className="flex gap-1.5">
        {tabs.map((t) => (
          <button
            key={t.id}
            onClick={() => setType(t.id)}
            className="rounded-md border px-2.5 py-1.5 text-[12px]"
            style={{
              borderColor: type === t.id ? "#3fb950" : "#242424",
              color: type === t.id ? "#e8e8e8" : "#8b8b8b",
              background: type === t.id ? "rgba(63,185,80,0.08)" : "transparent",
            }}
          >
            {t.label}
          </button>
        ))}
      </div>
      <div className="mt-1.5 text-[11.5px] text-subtle">{tabs.find((t) => t.id === type)?.hint}</div>

      {type === "schedule" && (
        <div className="mt-3">
          <div className="flex flex-wrap gap-1.5">
            {CRON_PRESETS.map((p) => (
              <button
                key={p.cron}
                onClick={() => { setCron(p.cron); setLabel(p.label); }}
                className="rounded-full border px-2.5 py-[3px] text-[11.5px]"
                style={{
                  borderColor: cron === p.cron ? "#3fb950" : "#242424",
                  color: cron === p.cron ? "#e8e8e8" : "#8b8b8b",
                }}
              >
                {p.label}
              </button>
            ))}
          </div>
          <div className="mt-2.5 flex items-center gap-2">
            <span className="text-[11.5px] text-subtle">Custom</span>
            <input
              value={cron}
              onChange={(e) => { setCron(e.target.value); setLabel(""); }}
              spellCheck={false}
              aria-label="Cron expression"
              className="w-[170px] rounded-md border bg-elevated px-2 py-1 font-mono text-[11.5px] text-ink outline-none"
              style={{ borderColor: cronOk ? "#242424" : "#8b3a3a" }}
            />
            <span className="text-[11.5px]" style={{ color: cronOk ? "#8b8b8b" : "#f85149" }}>
              {cronOk ? `next run ${nextRunLabel(cron)}` : "not a valid schedule"}
            </span>
          </div>
        </div>
      )}

      {type === "event" && (
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <input
            value={rule}
            onChange={(e) => setRule(e.target.value)}
            placeholder="rule (e.g. unassigned_bug) — blank means any"
            aria-label="Situation rule"
            className="w-[290px] rounded-md border border-edge bg-elevated px-2 py-1 text-[12px] text-ink outline-none placeholder:text-subtle"
          />
          <select
            value={severity}
            onChange={(e) => setSeverity(e.target.value)}
            aria-label="Severity"
            className="rounded-md border border-edge bg-elevated px-2 py-1 text-[12px] text-ink outline-none"
          >
            {SEVERITIES.map((s) => <option key={s} value={s}>{s || "any severity"}</option>)}
          </select>
          <span className="text-[11.5px] text-subtle">
            {!rule && !severity ? "fires on anything raised — usually too broad" : "fires once per matching item"}
          </span>
        </div>
      )}

      <div className="mt-3.5 flex items-center gap-2">
        <button
          onClick={save}
          disabled={busy || (type === "schedule" && !cronOk)}
          className="rounded-md border-none bg-accent px-3 py-1.5 text-[12.5px] font-semibold text-[#0a0a0a] hover:opacity-90 disabled:opacity-40"
        >
          {busy ? "Saving…" : "Save trigger"}
        </button>
        <button onClick={onCancel} className="border-none bg-transparent p-0 text-[12px] text-subtle hover:text-ink">Cancel</button>
        {type !== "manual" && (
          <span className="ml-auto text-[11px] text-warn">
            Runs on its own and acts for real — even in Practice mode.
          </span>
        )}
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
  const [editingTrigger, setEditingTrigger] = useState(false);
  type LiveStep = { index: number; tool: string; action?: string; running: boolean; status?: string; detail?: string };
  const [live, setLive] = useState<LiveStep[]>([]);

  async function saveTrigger(trigger: WorkflowTrigger) {
    setBusy("trigger");
    try {
      const updated = await api(`/api/workflows/${wf.id}`, {
        method: "PATCH", body: JSON.stringify({ trigger }),
      });
      setWf(updated);
      setEditingTrigger(false);
      onChanged();
      flash(trigger.type === "manual" ? "Now runs only when you press Run." : `Trigger set — ${triggerLabel(trigger)}`);
    } catch (e: any) { flash(e.message); } finally { setBusy(null); }
  }

  async function patchSteps(steps: WorkflowStep[]) {
    // optimistic; the response carries fresh app_info so we adopt it
    setWf((w) => ({ ...w, steps }));
    try {
      const updated = await api(`/api/workflows/${wf.id}`, {
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

  /* Streamed: a run can queue approvals or call a real API per step, so you
     watch each node land instead of staring at a spinner and being handed a
     verdict. The final frame carries the same payload the plain route returns. */
  async function run() {
    setBusy("run");
    setLive([]);
    setLastRun(null);
    try {
      await streamPost(`/api/workflows/${wf.id}/run/stream`, {}, (e) => {
        if (e.type === "step_start") {
          setLive((L) => [...L, { index: e.index, tool: e.tool, action: e.action, running: true }]);
        } else if (e.type === "step_done") {
          setLive((L) => L.map((s) => (s.index === e.index
            ? { ...s, running: false, status: e.result?.status, detail: e.result?.detail } : s)));
        } else if (e.type === "error") {
          throw new Error(e.error);
        } else if (e.type === "final") {
          setLive([]);
          setLastRun(e);
          flash(`${wf.name}: ${e.summary}`);
        }
      });
      onChanged();
      if (runs) await loadRuns();
    } catch (e: any) { flash(e.message); setLive([]); } finally { setBusy(null); }
  }
  async function loadRuns() {
    const r = await api(`/api/workflows/${wf.id}/runs`);
    setRuns(r.runs);
  }
  async function toggleEnabled() {
    try {
      const updated = await api(`/api/workflows/${wf.id}`, {
        method: "PATCH", body: JSON.stringify({ enabled: !wf.enabled }),
      });
      setWf(updated); onChanged();
    } catch (e: any) { flash(e.message); }
  }
  async function remove() {
    setBusy("delete");
    try {
      await api(`/api/workflows/${wf.id}`, { method: "DELETE" });
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
            {!wf.enabled && wf.trigger?.type !== "manual" && (
              <span className="text-warn">paused — it won&apos;t fire on its trigger</span>
            )}
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
      <WorkflowGraph
        trigger={wf.trigger}
        steps={wf.steps}
        controls={{ onToggle, onMove, onEditTrigger: () => setEditingTrigger((v) => !v) }}
      />
      {editingTrigger && (
        <TriggerEditor
          trigger={wf.trigger}
          onSave={saveTrigger}
          onCancel={() => setEditingTrigger(false)}
          busy={busy === "trigger"}
        />
      )}

      {live.length > 0 && (
        <div className="mt-5 rounded-lg border border-edge bg-panel px-[18px] py-3">
          <div className="mb-1 flex items-center gap-2 text-[11px] font-semibold tracking-[1px] text-accent">
            <span className="h-[6px] w-[6px] rounded-full bg-accent" style={{ animation: "blink 1.2s infinite" }} />
            RUNNING…
          </div>
          {live.map((s) => (
            <div key={s.index} className="flex items-baseline gap-2.5 py-1">
              <Dot color={s.running ? "#8b8b8b" : runStatusColor(s.status ?? "done")} size={6} />
              <span className="min-w-0 flex-1 truncate text-[12px] text-muted">
                {s.tool}{s.action ? ` · ${s.action}` : ""}
                {s.running
                  ? <span className="text-subtle"> — working…</span>
                  : s.detail && <span className="text-subtle"> — {s.detail}</span>}
              </span>
            </div>
          ))}
        </div>
      )}

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
            {runs.length > 0
              ? runs.map((r) => <RunRow key={r.id} run={r} />)
              : <div className="py-2 text-[12px] text-subtle">No runs yet.</div>}
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
          {/* a schedule on a disabled workflow will NOT fire — say so here
              rather than letting the trigger badge imply otherwise */}
          {wf.trigger?.type === "schedule" && wf.trigger.config?.cron && (
            wf.enabled
              ? ` · next ${nextRunLabel(String(wf.trigger.config.cron))}`
              : " · paused, won't run"
          )}
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

  const scheduled = workflows.filter((w) => w.trigger?.type === "schedule" && w.enabled).length;
  const live = workflows.filter((w) => w.enabled).length;

  return (
    <Page
      title="Workflows"
      purpose="Saved workflows run on their trigger — manually, on a schedule, or when something is detected. Open one to run it, watch it, or change it by chat."
      action={<SecondaryBtn onClick={onNewWorkflow}>＋ New workflow</SecondaryBtn>}
      stats={workflows.length ? [
        { label: "saved", value: workflows.length, tone: "ok" },
        { label: "active", value: live, tone: live ? "ok" : "idle" },
        { label: "on a schedule", value: scheduled, tone: scheduled ? "ok" : "idle" },
      ] : undefined}
    >
      {workflows.length > 0 ? (
        <div className="flex flex-col gap-3">
          {workflows.map((wf) => <WorkflowRow key={wf.id} wf={wf} onOpen={() => setOpenId(wf.id)} />)}
        </div>
      ) : (
        <Empty
          title="No workflows yet"
          next="Describe what you want in the Agent — “every morning, label unassigned bugs” — and it builds the whole flow for you to review before anything runs."
          action={<PrimaryBtn onClick={onNewWorkflow}>Build one in the Agent</PrimaryBtn>}
        />
      )}

      {toast && <Toast text={toast} />}
    </Page>
  );
}

function Toast({ text }: { text: string }) {
  return (
    <div className="toast-in fixed bottom-6 left-[calc(50%+80px)] z-50 flex -translate-x-1/2 items-center gap-2 rounded-lg border border-edge bg-elevated px-4 py-2.5 text-[13px] text-ink">
      {text}
    </div>
  );
}
