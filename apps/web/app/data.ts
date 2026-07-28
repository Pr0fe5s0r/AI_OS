"use client";

/**
 * Types and pure helpers for the console.
 *
 * This file used to carry sample clusters, collections, keys and traces
 * because there was no backend to read from. There is one now, so the data
 * lives in api.ts and this holds only the shapes and the formatting.
 *
 * The types mirror what the API actually returns rather than an idealised
 * model: a collection reports `items`, not `points`, because that is the word
 * the store uses, and inventing a nicer vocabulary here would only reintroduce
 * the drift the rename was meant to end.
 */

export function cx(...parts: (string | false | null | undefined)[]): string {
  return parts.filter(Boolean).join(" ");
}

// --------------------------------- types ---------------------------------

export type Collection = {
  id: string;
  clusterId: string;
  name: string;
  description?: string | null;
  embeddingModel: string;
  dimensions: number;
  items: number;
};

export type Cluster = {
  id: string;
  name: string;
  region: string;
  createdAt: string;
  collections: Collection[];
};

export type CollectionStats = {
  items: number;
  superseded: number;
  failed: number;
  characters: number;
  sources: number;
};

export type CollectionDetail = Collection & { stats: CollectionStats };

export type ApiKey = {
  id: string;
  name: string;
  /** Only ever a prefix. The key itself exists once, at creation, and is
   *  never retrievable — so there is no "reveal" to offer. */
  prefix: string;
  scopes: string[];
  createdBy: string | null;
  createdAt: string;
  lastUsed: string | null;
  revoked: boolean;
};

/** A key at the moment it is minted — the only time the secret exists. */
export type MintedKey = { id: string; name: string; key: string; scopes: string[] };

export type TraceStatus = "ok" | "slow" | "degraded" | "empty";

export type Trace = {
  id: string;
  query: string;
  collectionId: string | null;
  resultCount: number;
  durationMs: number;
  degraded: string | null;
  via: string;
  actor: string | null;
  createdAt: string;
};

/** One fused candidate inside a trace — the per-result derivation. */
export type Candidate = {
  item_id: string;
  score: number;
  semantic: number;
  keyword: number;
  recency: number;
  kept: boolean;
};

export type TraceDetail = Trace & {
  config: Record<string, unknown>;
  filters: Record<string, unknown>;
  semantic: { item_id: string; similarity: number }[];
  keyword: { item_id: string; rank: number }[];
  fused: Candidate[];
  returned: string[];
  timings_ms: Record<string, number>;
};

export type Category = {
  class_id: string;
  name: string;
  confidence: number;
  basis: string | null;
  pinned: boolean;
};

/** A retrieved match. `Point` is the name the vector field renders. */
export type Point = {
  id: string;
  title: string;
  score: number;
  semantic: number;
  keyword: number;
  excerpt: string;
  source: string;
  locator: string;
  url?: string | null;
  categories: Category[];
};

export type Document = {
  id: string;
  title: string;
  body: string;
  source: string;
  locator: string;
  version: number;
  status: string;
  createdAt: string | null;
  categories: Category[];
};

// -------------------------------- scales ---------------------------------

/** Cosine similarity onto the five-step heat scale. h0 is nearest. */
export function simBand(sim: number): 0 | 1 | 2 | 3 | 4 {
  if (sim >= 0.9) return 0;
  if (sim >= 0.8) return 1;
  if (sim >= 0.68) return 2;
  if (sim >= 0.55) return 3;
  return 4;
}

/** Latency onto the same scale, so fast reads as "near". */
export function latencyBand(ms: number): 0 | 1 | 2 | 3 | 4 {
  if (ms < 120) return 0;
  if (ms < 400) return 1;
  if (ms < 900) return 2;
  if (ms < 2000) return 3;
  return 4;
}

export const HEAT_HEX = ["#c6f45f", "#7fe0a0", "#4fd6c9", "#59a6ff", "#6b74ff"];

export function heatTone(band: 0 | 1 | 2 | 3 | 4): string {
  return [
    "text-heat-0 border-heat-0/30 bg-heat-0/10",
    "text-heat-1 border-heat-1/30 bg-heat-1/10",
    "text-heat-2 border-heat-2/30 bg-heat-2/10",
    "text-heat-3 border-heat-3/30 bg-heat-3/10",
    "text-heat-4 border-heat-4/30 bg-heat-4/10",
  ][band];
}

/** What a trace amounts to, derived rather than stored: a degraded run and an
 *  empty one are different failures and must not share a colour. */
export function traceStatus(t: Trace): TraceStatus {
  if (t.degraded) return "degraded";
  if (t.resultCount === 0) return "empty";
  if (t.durationMs >= 2000) return "slow";
  return "ok";
}

export function statusTone(s: TraceStatus): string {
  return {
    ok: "text-success border-success/30 bg-success/10",
    slow: "text-warn border-warn/30 bg-warn/10",
    degraded: "text-hot border-hot/40 bg-hot/10",
    empty: "text-subtle border-edgeStrong bg-elevated",
  }[s];
}

// ------------------------------- formatting -------------------------------

export function ago(iso: string | null): string {
  if (!iso) return "never";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "—";
  const mins = Math.round((Date.now() - then) / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.round(hours / 24);
  if (days < 30) return `${days}d ago`;
  return new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "short" });
}

export function num(n: number): string {
  return n.toLocaleString();
}

export function compact(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}

/** Characters of stored Markdown, as a size. The store keeps text, not files,
 *  so this is the honest measure of how much a collection holds. */
export function bytes(chars: number): string {
  if (chars >= 1_000_000) return `${(chars / 1_000_000).toFixed(1)} MB`;
  if (chars >= 1_000) return `${(chars / 1_000).toFixed(1)} kB`;
  return `${chars} B`;
}

export function ms(v: number): string {
  return v >= 1000 ? `${(v / 1000).toFixed(2)}s` : `${Math.round(v)}ms`;
}
