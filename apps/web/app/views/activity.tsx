"use client";

import { useState } from "react";
import { AuditEntry, Dot, dayTime, humanize, sourceColor } from "../lib";

const AUTOMATED = new Set(["ai", "scheduler", "system", "github-webhook", "api"]);
type Filter = "all" | "auto" | "people";

type ActivityRow = { key: string; entry: AuditEntry };

function rowSources(r: AuditEntry): string[] {
  const hay = `${r.target} ${JSON.stringify(r.metadata ?? {})}`.toLowerCase();
  return ["github", "slack", "zendesk", "ops"].filter((source) => hay.includes(source));
}

function activityKind(r: AuditEntry) {
  const value = `${r.action} ${r.target}`.toLowerCase();
  if (value.includes("profile") || value.includes("norm") || value.includes("baseline")) return { label: "Business memory", color: "#58a6ff" };
  if (value.includes("connect") || value.includes("source")) return { label: "Connection", color: "#d29922" };
  if (value.includes("approv") || value.includes("reject")) return { label: "Decision", color: "#f0883e" };
  if (value.includes("situation") || value.includes("clarif") || value.includes("resolve")) return { label: "Attention", color: "#a371f7" };
  return { label: "System activity", color: "#8b949e" };
}

function readableValue(value: unknown) {
  if (value === null || value === undefined || value === "") return "Not recorded";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "string" || typeof value === "number") return String(value);
  if (Array.isArray(value)) return value.map((item) => typeof item === "object" ? JSON.stringify(item) : String(item)).join(", ");
  return JSON.stringify(value, null, 2);
}

function ActivityDetail({ entry }: { entry: AuditEntry }) {
  const auto = AUTOMATED.has(entry.actor);
  const kind = activityKind(entry);
  const sources = rowSources(entry);
  const metadata = Object.entries(entry.metadata ?? {}).filter(([, value]) => value !== null && value !== "");
  return (
    <div className="max-h-[590px] min-h-[450px] overflow-y-auto p-5 sm:p-7">
      <div className="mb-5 flex items-center justify-between gap-3">
        <div className="flex items-center gap-2 text-[10.5px] font-semibold uppercase tracking-[1px]" style={{ color: kind.color }}>
          <Dot color={kind.color} />{kind.label}
        </div>
        <span className="font-mono text-[11px] text-subtle">{dayTime(entry.created_at)}</span>
      </div>

      <h2 className="m-0 text-[20px] font-semibold leading-snug text-ink">{humanize(entry.action)}</h2>
      {entry.target && <p className="mb-0 mt-2 text-[13.5px] leading-relaxed text-muted">{humanize(entry.target)}</p>}

      <div className="mt-6 grid border-y border-edge sm:grid-cols-3">
        <div className="py-3 sm:border-r sm:border-edge sm:pr-4">
          <div className="text-[10.5px] uppercase tracking-[1px] text-subtle">Done by</div>
          <div className="mt-1 text-[12.5px] text-ink">{auto ? "MarkOS automation" : entry.actor}</div>
        </div>
        <div className="border-t border-edge py-3 sm:border-r sm:border-t-0 sm:px-4">
          <div className="text-[10.5px] uppercase tracking-[1px] text-subtle">When</div>
          <div className="mt-1 text-[12.5px] text-ink">{new Date(entry.created_at).toLocaleString()}</div>
        </div>
        <div className="border-t border-edge py-3 sm:border-t-0 sm:pl-4">
          <div className="text-[10.5px] uppercase tracking-[1px] text-subtle">From</div>
          <div className="mt-1 flex min-h-[19px] items-center gap-2 text-[12.5px] text-ink">
            {sources.length ? sources.map((source) => <span key={source} className="flex items-center gap-1.5"><Dot color={sourceColor(source)} />{humanize(source)}</span>) : "MarkOS"}
          </div>
        </div>
      </div>

      <div className="mt-6">
        <div className="mb-2 text-[10.5px] font-semibold uppercase tracking-[1px] text-subtle">Recorded details</div>
        {metadata.length ? (
          <div className="border-t border-edge">
            {metadata.map(([key, value]) => {
              const shown = readableValue(value);
              const multiline = shown.includes("\n") || shown.length > 100;
              return (
                <div key={key} className="grid gap-1 border-b border-edge py-3 sm:grid-cols-[150px_minmax(0,1fr)] sm:gap-4">
                  <span className="text-[12px] text-subtle">{humanize(key)}</span>
                  {multiline ? <pre className="m-0 whitespace-pre-wrap break-words font-mono text-[11.5px] leading-relaxed text-muted">{shown}</pre> : <span className="break-words text-[12.5px] leading-relaxed text-ink">{shown}</span>}
                </div>
              );
            })}
          </div>
        ) : (
          <div className="border-y border-edge py-4 text-[12.5px] text-subtle">No extra fields were recorded for this event.</div>
        )}
      </div>
    </div>
  );
}

export default function ActivityView({ entries }: { entries: AuditEntry[] | null }) {
  const [filter, setFilter] = useState<Filter>("all");
  const [selectedKey, setSelectedKey] = useState<string | null>(null);

  if (entries === null) {
    return <div className="mx-auto max-w-[1080px] px-7 pb-20 pt-9"><div className="text-[13px] text-muted">Loading activity...</div></div>;
  }

  const allRows: ActivityRow[] = entries.map((entry, index) => ({ key: `${entry.created_at}:${entry.action}:${entry.target}:${index}`, entry }));
  const counts = {
    all: allRows.length,
    auto: allRows.filter(({ entry }) => AUTOMATED.has(entry.actor)).length,
    people: allRows.filter(({ entry }) => !AUTOMATED.has(entry.actor)).length,
  };
  const rows = allRows.filter(({ entry }) => filter === "all" || (filter === "auto" ? AUTOMATED.has(entry.actor) : !AUTOMATED.has(entry.actor)));

  const selected = rows.find((row) => row.key === selectedKey) ?? rows[0];

  const filters: { key: Filter; label: string; hint: string }[] = [
    { key: "all", label: "All", hint: "Complete history" },
    { key: "auto", label: "AI + system", hint: "Done automatically" },
    { key: "people", label: "People", hint: "Human decisions" },
  ];

  return (
    <div className="mx-auto max-w-[1080px] px-5 pb-16 pt-7 sm:px-8 sm:pt-9">
      <header className="mb-6">
        <div className="text-[11px] uppercase tracking-[1px] text-subtle">Audit trail</div>
        <h1 className="mb-0 mt-1 text-[22px] font-semibold text-ink">Activity</h1>
        <p className="mb-0 mt-1 text-[13px] leading-relaxed text-muted">Every change is kept here. Select an event to see who made it, where it came from, and the exact data recorded.</p>
      </header>

      <div className="mb-4 grid grid-cols-3 border border-edge">
        {filters.map((item, index) => (
          <button key={item.key} onClick={() => setFilter(item.key)} className={`min-h-[68px] border-0 bg-transparent px-3 py-2.5 text-left transition-colors sm:px-4 ${index > 0 ? "border-l border-edge" : ""} ${filter === item.key ? "bg-elevated" : "hover:bg-panel"}`}>
            <div className="text-[17px] font-semibold text-ink">{counts[item.key]}</div>
            <div className="mt-0.5 text-[11.5px] text-muted">{item.label}</div>
            <div className="mt-0.5 hidden text-[10.5px] text-subtle sm:block">{item.hint}</div>
          </button>
        ))}
      </div>

      <div className="grid overflow-hidden border border-edge lg:grid-cols-[330px_minmax(0,1fr)]">
        <div className="max-h-[310px] overflow-y-auto border-b border-edge bg-panel lg:max-h-[590px] lg:border-b-0 lg:border-r">
          {rows.map(({ key, entry }) => {
            const auto = AUTOMATED.has(entry.actor);
            const kind = activityKind(entry);
            const sources = rowSources(entry);
            return (
              <button key={key} onClick={() => setSelectedKey(key)} className={`block w-full border-0 border-b border-edge px-4 py-3.5 text-left transition-colors ${selected?.key === key ? "bg-elevated" : "bg-transparent hover:bg-elevated/60"}`}>
                <div className="flex gap-3">
                  <span className="mt-1"><Dot color={kind.color} /></span>
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-[13px] font-semibold text-ink">{humanize(entry.action)}</span>
                    <span className="mt-1 block truncate text-[11.5px] text-subtle">{entry.target ? humanize(entry.target) : kind.label}</span>
                    <span className="mt-1.5 flex items-center justify-between gap-3 text-[10.5px] text-subtle">
                      <span>{auto ? "MarkOS" : entry.actor}</span>
                      <span className="flex items-center gap-1.5">{sources.map((source) => <Dot key={source} color={sourceColor(source)} size={5} />)}{dayTime(entry.created_at)}</span>
                    </span>
                  </span>
                </div>
              </button>
            );
          })}
          {rows.length === 0 && <div className="px-5 py-12 text-center text-[13px] text-muted">Nothing in this view yet.</div>}
        </div>
        <div className="min-w-0 bg-[#0d0d0d]">
          {selected ? <ActivityDetail entry={selected.entry} /> : <div className="p-8 text-[13px] text-subtle">Select an event to inspect it.</div>}
        </div>
      </div>
    </div>
  );
}

