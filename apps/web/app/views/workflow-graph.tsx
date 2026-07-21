"use client";

import { useState } from "react";
import {
  Dot, StepApp, WorkflowStep, WorkflowTrigger, appColor, humanize, triggerLabel,
} from "../lib";

/* The n8n-style workflow canvas: a trigger node at the top, then action nodes
   connected top-to-bottom. Read-first — you look at it to understand the flow —
   with optional light controls (enable/disable, reorder) when a parent passes
   handlers. All authoring is by prompt; this never lets you wire nodes freehand. */

function TriggerIcon({ type }: { type: string }) {
  if (type === "schedule") {
    return (
      <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
        <circle cx="8" cy="8" r="6" /><path d="M8 4.8V8l2.2 1.6" />
      </svg>
    );
  }
  if (type === "event") {
    return (
      <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round">
        <path d="M8.5 1.5 3 9h4l-.5 5.5L13 7H9z" />
      </svg>
    );
  }
  return (
    <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round">
      <path d="M6 2.5v7M6 9.5l-2.2-2M6 9.5l2.2-2M4 12.5h8" />
    </svg>
  );
}

function Connector() {
  return (
    <div className="flex justify-center">
      <svg width="16" height="20" viewBox="0 0 16 20" className="text-edgeStrong">
        <path d="M8 0v14" stroke="currentColor" strokeWidth="1.4" />
        <path d="M4.5 11 8 14.5 11.5 11" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    </div>
  );
}

function AppBadge({ app }: { app: StepApp | undefined }) {
  if (!app) return null;
  return (
    <span className="inline-flex flex-none items-center gap-[5px] rounded-full border border-edge bg-elevated px-2 py-[2px] text-[10.5px] text-muted">
      <Dot color={appColor(app.app)} size={5} />
      <span className="capitalize">{app.app}</span>
    </span>
  );
}

function stepTitle(step: WorkflowStep): string {
  const action = step.args?.action as string | undefined;
  if (step.tool === "run_action" && action) return humanize(action);
  return humanize(step.tool);
}

function targetChip(step: WorkflowStep): string | null {
  if (step.select) {
    return "every " + Object.entries(step.select).map(([k, v]) => `${k}=${v}`).join(", ");
  }
  if (step.args?.situation_id) return "one item";
  return null;
}

type Controls = {
  onToggle?: (index: number) => void;
  onMove?: (index: number, dir: -1 | 1) => void;
};

function StepNode({ step, index, total, controls }: {
  step: WorkflowStep; index: number; total: number; controls?: Controls;
}) {
  const [open, setOpen] = useState(false);
  const off = step.enabled === false;
  const target = targetChip(step);

  return (
    <div
      className="rounded-lg border bg-panel"
      style={{ borderColor: off ? "#1f1f1f" : "#242424", opacity: off ? 0.5 : 1 }}
    >
      <div className="flex items-start gap-2.5 px-3.5 py-2.5">
        <span className="mt-[3px] flex h-[18px] w-[18px] flex-none items-center justify-center rounded-full border border-edge text-[10.5px] text-subtle">
          {index + 1}
        </span>
        <button onClick={() => setOpen(!open)} className="min-w-0 flex-1 border-none bg-transparent p-0 text-left">
          <div className="flex items-center gap-2">
            <span className="truncate text-[13px] font-medium text-ink">{stepTitle(step)}</span>
            <AppBadge app={step.app_info} />
          </div>
          <div className="mt-0.5 truncate text-[11.5px] text-muted">
            {step.description || (step.app_info?.does ?? "")}
          </div>
          <div className="mt-1 flex flex-wrap items-center gap-1.5">
            {target && (
              <span className="rounded border border-edge px-1.5 py-[1px] text-[10px] text-info">{target}</span>
            )}
            {step.requires_approval && (
              <span className="rounded border border-edge px-1.5 py-[1px] text-[10px] text-warn">waits for approval</span>
            )}
            {off && <span className="text-[10px] text-subtle">disabled</span>}
          </div>
        </button>

        {controls && (
          <div className="flex flex-none items-center gap-1.5">
            {controls.onMove && (
              <div className="flex flex-col">
                <button onClick={() => controls.onMove!(index, -1)} disabled={index === 0}
                        className="border-none bg-transparent p-0 text-[10px] text-subtle hover:text-ink disabled:opacity-25" aria-label="Move up">▲</button>
                <button onClick={() => controls.onMove!(index, 1)} disabled={index === total - 1}
                        className="border-none bg-transparent p-0 text-[10px] text-subtle hover:text-ink disabled:opacity-25" aria-label="Move down">▼</button>
              </div>
            )}
            {controls.onToggle && (
              <button
                onClick={() => controls.onToggle!(index)}
                title={off ? "Enable step" : "Disable step"}
                className="flex h-4 w-7 flex-none items-center rounded-full px-[2px]"
                style={{ background: off ? "#2a2a2a" : "#1f5130", justifyContent: off ? "flex-start" : "flex-end" }}
              >
                <span className="h-3 w-3 rounded-full" style={{ background: off ? "#6a6a6a" : "#3fb950" }} />
              </button>
            )}
          </div>
        )}
      </div>

      {open && (
        <div className="border-t border-edge px-3.5 py-2 font-mono text-[11px] text-subtle">
          <div>tool: <span className="text-muted">{step.tool}</span></div>
          {Object.keys(step.args || {}).length > 0 && (
            <div className="mt-0.5">args: <span className="text-muted">{JSON.stringify(step.args)}</span></div>
          )}
          {step.select && <div className="mt-0.5">select: <span className="text-muted">{JSON.stringify(step.select)}</span></div>}
        </div>
      )}
    </div>
  );
}

export function WorkflowGraph({ trigger, steps, controls }: {
  trigger: WorkflowTrigger; steps: WorkflowStep[]; controls?: Controls;
}) {
  return (
    <div className="flex flex-col">
      {/* trigger node */}
      <div className="rounded-lg border px-3.5 py-2.5" style={{ borderColor: "#2a3a2c", background: "rgba(63,185,80,0.06)" }}>
        <div className="flex items-center gap-2">
          <span className="flex h-[18px] w-[18px] flex-none items-center justify-center rounded-full text-accent" style={{ background: "rgba(63,185,80,0.14)" }}>
            <TriggerIcon type={trigger?.type ?? "manual"} />
          </span>
          <span className="text-[10px] font-semibold uppercase tracking-[1px] text-accent">Trigger</span>
          <span className="truncate text-[12.5px] text-ink">{triggerLabel(trigger)}</span>
        </div>
      </div>

      {steps.map((step, i) => (
        <div key={i}>
          <Connector />
          <StepNode step={step} index={i} total={steps.length} controls={controls} />
        </div>
      ))}

      {steps.length === 0 && (
        <>
          <Connector />
          <div className="rounded-lg border border-dashed border-edge px-3.5 py-3 text-center text-[12px] text-subtle">
            No steps yet.
          </div>
        </>
      )}
    </div>
  );
}
