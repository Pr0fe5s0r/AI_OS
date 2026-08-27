"use client";

/** Types and helpers shared by every view. One API client, one vocabulary. */

// Empty means same-origin. next.config.js forwards /api/* to the backend using
// the server-only API_URL environment variable.
export const API = (process.env.NEXT_PUBLIC_API_URL || "").replace(/\/$/, "");

export type Klass = {
  class_id: string;
  name: string;
  confidence: number;
  basis: string | null;
  pinned: boolean;
  actor: string | null;
};

export type SourceRef = {
  source: string;
  locator: string;
  url: string | null;
  fetched_at?: string | null;
};

export type Item = {
  id: string;
  title: string;
  body: string;
  source: SourceRef;
  hash: string;
  version: number;
  supersedes: string | null;
  status: "active" | "superseded" | "archived" | "failed";
  created_at: string | null;
  period_start: string | null;
  period_end: string | null;
  metadata: Record<string, unknown>;
  scope: { tenant_id: string; brand_id: string | null };
  classes?: Klass[];
};

export type Hit = {
  item_id: string;
  title: string;
  excerpt: string;
  source: SourceRef;
  score: number;
  semantic: number;
  keyword: number;
  classes?: Klass[];
};

export type TaxonomyClass = {
  class_id: string;
  name: string;
  description: string | null;
  parent_id: string | null;
  system: boolean;
  scope: "platform" | "tenant";
};

export type Facets = {
  total: number;
  sources: { source: string; count: number }[];
  classes: { class_id: string; name: string; count: number }[];
};

/** Every request carries the session cookie; nothing takes a tenant id. */
export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API}${path}`, {
    credentials: "include",
    headers:
      init?.body && !(init.body instanceof FormData)
        ? { "Content-Type": "application/json" }
        : undefined,
    ...init,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail || detail;
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(res.status, detail);
  }
  return res.status === 204 ? (undefined as T) : ((await res.json()) as T);
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

// ------------------------------- formatting -------------------------------

export function when(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  const mins = Math.round((Date.now() - d.getTime()) / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.round(hours / 24);
  if (days < 30) return `${days}d ago`;
  return d.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
}

export function period(item: Item | null): string | null {
  if (!item?.period_start && !item?.period_end) return null;
  const fmt = (s: string | null) =>
    s ? new Date(s).toLocaleDateString(undefined, { month: "short", year: "numeric" }) : "…";
  return `${fmt(item.period_start)} – ${fmt(item.period_end)}`;
}

/** Strip the scaffolding our own parser inserts, for previews. */
export function preview(body: string, chars = 180): string {
  const clean = body
    .replace(/<!--[\s\S]*?-->/g, "")
    .replace(/^#+\s*/gm, "")
    .replace(/^-{3,}$/gm, "")
    .replace(/\s+/g, " ")
    .trim();
  return clean.length > chars ? `${clean.slice(0, chars)}…` : clean;
}

/** A stable colour per source, so the eye learns them. */
export function sourceTone(source: string): string {
  const tones: Record<string, string> = {
    upload: "text-info border-info/30 bg-info/10",
    gdrive: "text-success border-success/30 bg-success/10",
    s3: "text-warn border-warn/30 bg-warn/10",
    notion: "text-accentSoft border-accent/30 bg-accent/10",
    report: "text-accentSoft border-accent/30 bg-accent/10",
    chat: "text-muted border-edgeStrong bg-elevated",
  };
  return tones[source] || "text-muted border-edgeStrong bg-elevated";
}

/** Confidence is shown as a judgement, not a number to decode. */
export function confidenceTone(c: number, pinned: boolean): string {
  if (pinned) return "text-accentSoft border-accent/40 bg-accent/10";
  if (c >= 0.8) return "text-success border-success/30 bg-success/10";
  if (c >= 0.55) return "text-muted border-edgeStrong bg-elevated";
  return "text-warn border-warn/30 bg-warn/10";
}

export function cx(...parts: (string | false | null | undefined)[]): string {
  return parts.filter(Boolean).join(" ");
}
