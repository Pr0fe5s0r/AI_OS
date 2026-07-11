"use client";

/* Shared theme primitives + helpers. Same tokens as the rest of the app:
   canvas / panel / elevated / edge / ink / muted / subtle / accent. */

export const SOURCES: Record<string, { label: string; letter: string; color: string; tint: string }> = {
  github: { label: "github", letter: "G", color: "#8b949e", tint: "rgba(139,148,158,0.15)" },
  slack: { label: "slack", letter: "S", color: "#a855f7", tint: "rgba(168,85,247,0.15)" },
  zendesk: { label: "zendesk", letter: "Z", color: "#2dd4bf", tint: "rgba(45,212,191,0.15)" },
};

export function src(source: string | null) {
  const key = source || "person";
  return SOURCES[key] ?? {
    label: key,
    letter: key[0].toUpperCase(),
    color: "#58a6ff",
    tint: "rgba(88,166,255,0.15)",
  };
}

export const SEVERITY: Record<string, { color: string; tint: string }> = {
  critical: { color: "#f85149", tint: "rgba(248,81,73,0.15)" },
  high: { color: "#d29922", tint: "rgba(210,153,34,0.15)" },
  medium: { color: "#58a6ff", tint: "rgba(88,166,255,0.15)" },
  low: { color: "#8b8b8b", tint: "rgba(139,139,139,0.15)" },
};

export const STATUS_COLOR: Record<string, string> = {
  pending_approval: "#d29922",
  executed: "#3fb950",
  dry_run: "#58a6ff",
  rejected: "#f85149",
  failed: "#f85149",
};

export const EDGE_COLOR: Record<string, string> = {
  SAME_AS: "#3fb950",
  MENTIONS: "#58a6ff",
  AUTHORED: "#6a6a6a",
  CLOSES: "#d29922",
  CAUSED_BY: "#f85149",
};

export function initials(name: string) {
  return name.split(/[\s_-]+/).map((p) => p[0]).slice(0, 2).join("").toUpperCase();
}

export function relativeTime(iso?: string) {
  if (!iso) return "";
  const diff = Date.now() - new Date(iso).getTime();
  const m = Math.round(diff / 60000);
  if (m < 60) return `${m}m ago`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.round(h / 24)}d ago`;
}

export function humanMetric(s: string) {
  return s.replace(/_/g, " ");
}

/* ------------------------------- icons ------------------------------- */

export const I = {
  leaf: () => (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none">
      <path d="M4 20C4 12 10 4 20 4c0 10-8 16-16 16Z" fill="currentColor" opacity="0.9" />
      <path d="M4 20C7 15 12 11 18 9" stroke="#0a0a0a" strokeWidth="1.6" strokeLinecap="round" />
    </svg>
  ),
  search: () => (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none">
      <circle cx="11" cy="11" r="7" stroke="currentColor" strokeWidth="2" />
      <path d="m20 20-3-3" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
    </svg>
  ),
  plug: () => (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none">
      <path d="M9 3v6M15 3v6M6 9h12v3a6 6 0 0 1-12 0V9ZM12 18v3" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
    </svg>
  ),
  issues: () => (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none">
      <circle cx="12" cy="12" r="8.5" stroke="currentColor" strokeWidth="1.8" />
      <circle cx="12" cy="12" r="2.5" fill="currentColor" />
    </svg>
  ),
  graph: () => (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none">
      <circle cx="5" cy="6" r="2.4" stroke="currentColor" strokeWidth="1.8" />
      <circle cx="18" cy="7" r="2.4" stroke="currentColor" strokeWidth="1.8" />
      <circle cx="12" cy="18" r="2.4" stroke="currentColor" strokeWidth="1.8" />
      <path d="M7 7 16 8M7 8l4 8M16 9l-3.5 7" stroke="currentColor" strokeWidth="1.5" />
    </svg>
  ),
  norms: () => (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none">
      <path d="M4 20h16" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
      <rect x="6" y="11" width="3" height="6" stroke="currentColor" strokeWidth="1.6" />
      <rect x="11" y="7" width="3" height="10" stroke="currentColor" strokeWidth="1.6" />
      <rect x="16" y="13" width="3" height="4" stroke="currentColor" strokeWidth="1.6" />
    </svg>
  ),
  flag: () => (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none">
      <path d="M5 21V4M5 4h10l-1.5 3L15 10H5" stroke="currentColor" strokeWidth="1.8" strokeLinejoin="round" />
    </svg>
  ),
  bolt: () => (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none">
      <path d="M13 2 4 14h6l-1 8 9-12h-6l1-8Z" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round" />
    </svg>
  ),
  people: () => (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none">
      <circle cx="9" cy="8" r="3.2" stroke="currentColor" strokeWidth="1.7" />
      <path d="M3.5 19a5.5 5.5 0 0 1 11 0" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" />
      <path d="M16 5.5a3 3 0 0 1 0 5.6M17.5 19a5 5 0 0 0-2.2-4.1" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
    </svg>
  ),
  ticket: () => (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none">
      <path d="M4 8a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2 2 2 0 0 0 0 4 2 2 0 0 1-2 2H6a2 2 0 0 1-2-2 2 2 0 0 0 0-4Z" stroke="currentColor" strokeWidth="1.6" />
      <path d="M13 6v10" stroke="currentColor" strokeWidth="1.5" strokeDasharray="2 2" />
    </svg>
  ),
  chevron: () => (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none">
      <path d="m9 18 6-6-6-6" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  ),
  dot: () => (
    <svg width="10" height="10" viewBox="0 0 10 10">
      <circle cx="5" cy="5" r="5" fill="currentColor" />
    </svg>
  ),
};

/* ---------------------------- small components ---------------------------- */

export function SideLabel({ children, className = "" }: { children: React.ReactNode; className?: string }) {
  return (
    <div className={`px-2.5 pb-1.5 text-[11px] font-medium uppercase tracking-wider text-subtle ${className}`}>
      {children}
    </div>
  );
}

export function NavButton({
  icon, label, active, onClick, badge,
}: { icon: React.ReactNode; label: string; active: boolean; onClick: () => void; badge?: number }) {
  return (
    <button
      onClick={onClick}
      className={`flex w-full items-center gap-2.5 rounded-md px-2.5 py-1.5 text-left text-[13px] transition ${
        active ? "bg-accent/10 font-medium text-ink" : "text-muted hover:bg-elevated hover:text-ink"
      }`}
    >
      <span className={`grid w-4 place-items-center ${active ? "text-accent" : ""}`}>{icon}</span>
      <span className="flex-1 truncate">{label}</span>
      {badge ? <Badge>{badge}</Badge> : null}
    </button>
  );
}

export function Badge({ children }: { children: React.ReactNode }) {
  return <span className="rounded-full bg-edge px-1.5 py-0.5 font-mono text-[10px] text-muted">{children}</span>;
}

export function Pill({ color, tint, children }: { color: string; tint: string; children: React.ReactNode }) {
  return (
    <span className="rounded-full px-1.5 py-0.5 text-[10px] font-medium" style={{ background: tint, color }}>
      {children}
    </span>
  );
}

export function Card({ children, className = "" }: { children: React.ReactNode; className?: string }) {
  return <div className={`rounded-lg border border-edge bg-panel ${className}`}>{children}</div>;
}

export function Empty({ children }: { children: React.ReactNode }) {
  return (
    <div className="rounded-lg border border-edge bg-panel px-6 py-10 text-center text-[13px] text-muted">
      {children}
    </div>
  );
}

export function SkeletonRows() {
  return (
    <div className="mt-2 overflow-hidden rounded-lg border border-edge bg-panel">
      {[0, 1, 2, 3].map((i) => (
        <div key={i} className={`flex items-start gap-3 px-4 py-3.5 ${i > 0 ? "border-t border-edge" : ""}`}>
          <div className="h-6 w-6 shrink-0 animate-pulse rounded-md bg-edge" />
          <div className="flex-1 space-y-2">
            <div className="h-3 w-1/3 animate-pulse rounded bg-edge" />
            <div className="h-3 w-4/5 animate-pulse rounded bg-edge" />
          </div>
        </div>
      ))}
    </div>
  );
}

export async function api(path: string, init?: RequestInit) {
  const res = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!res.ok) {
    let msg = `API ${res.status}`;
    try {
      const j = await res.json();
      if (j.detail) msg = j.detail;
    } catch {}
    throw new Error(msg);
  }
  return res.json();
}
