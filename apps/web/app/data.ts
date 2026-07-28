"use client";

/**
 * The control plane's domain model + realistic sample data.
 *
 * There is no vector-cloud backend yet, so every screen reads from here. The
 * shapes are deliberately close to what a real API would return (clusters,
 * collections, points, keys, request traces) so wiring this to live endpoints
 * later is a swap of the loaders below, not a rewrite of the views.
 */

export function cx(...parts: (string | false | null | undefined)[]): string {
  return parts.filter(Boolean).join(" ");
}

// --------------------------------- types ---------------------------------

export type Region = "us-east-1" | "eu-west-1" | "ap-south-1";
export type ClusterTier = "free" | "standard" | "dedicated";
export type ClusterStatus = "healthy" | "provisioning" | "degraded";

export type Cluster = {
  id: string;
  name: string;
  region: Region;
  tier: ClusterTier;
  status: ClusterStatus;
  nodes: number;
  ramGb: number;
  createdAt: string;
  endpoint: string;
};

export type Metric = "cosine" | "dot" | "euclid";

export type Collection = {
  id: string;
  clusterId: string;
  name: string;
  dims: number;
  metric: Metric;
  points: number;
  indexedPct: number;
  sizeMb: number;
  updatedAt: string;
  schema: { field: string; type: string }[];
};

export type ApiKey = {
  id: string;
  label: string;
  secret: string; // full secret; UI masks unless revealed
  scope: "read" | "write" | "admin";
  createdAt: string;
  lastUsed: string | null;
  revoked?: boolean;
};

export type TraceStatus = "ok" | "slow" | "error";
export type TraceOp = "upsert" | "search" | "retrieve" | "delete" | "create_index";

export type Trace = {
  id: string;
  ts: string;
  clusterId: string;
  collectionId: string;
  op: TraceOp;
  status: TraceStatus;
  latencyMs: number;
  vectors: number;
  key: string;
  detail: string;
};

export type Point = {
  id: string;
  score: number; // cosine similarity 0..1 (1 = identical)
  payload: Record<string, string>;
  snippet: string;
};

// --------------------------------- heat ----------------------------------

/** Map a cosine similarity (1 = perfect match) to a heat-scale band 0..4. */
export function simBand(sim: number): 0 | 1 | 2 | 3 | 4 {
  if (sim >= 0.9) return 0;
  if (sim >= 0.8) return 1;
  if (sim >= 0.68) return 2;
  if (sim >= 0.55) return 3;
  return 4;
}

/** Map a request latency to the same heat scale — fast is a "near" match. */
export function latencyBand(ms: number): 0 | 1 | 2 | 3 | 4 {
  if (ms < 12) return 0;
  if (ms < 30) return 1;
  if (ms < 70) return 2;
  if (ms < 160) return 3;
  return 4;
}

export const HEAT_HEX = ["#c6f45f", "#7fe0a0", "#4fd6c9", "#59a6ff", "#6b74ff"];

/** Tailwind text/border/bg tone for a heat band. */
export function heatTone(band: 0 | 1 | 2 | 3 | 4): string {
  return [
    "text-heat-0 border-heat-0/40 bg-heat-0/10",
    "text-heat-1 border-heat-1/40 bg-heat-1/10",
    "text-heat-2 border-heat-2/40 bg-heat-2/10",
    "text-heat-3 border-heat-3/40 bg-heat-3/10",
    "text-heat-4 border-heat-4/40 bg-heat-4/10",
  ][band];
}

export function statusTone(s: ClusterStatus | TraceStatus): string {
  const map: Record<string, string> = {
    healthy: "text-success border-success/40 bg-success/10",
    ok: "text-success border-success/40 bg-success/10",
    provisioning: "text-accentSoft border-accent/40 bg-accent/10",
    slow: "text-warn border-warn/40 bg-warn/10",
    degraded: "text-danger border-danger/40 bg-danger/10",
    error: "text-danger border-danger/40 bg-danger/10",
  };
  return map[s] || "text-muted border-edgeStrong bg-elevated";
}

// ------------------------------ formatting -------------------------------

export function ago(iso: string): string {
  const mins = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const h = Math.round(mins / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.round(h / 24);
  if (d < 30) return `${d}d ago`;
  return new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "short" });
}

export function num(n: number): string {
  return n.toLocaleString("en-US");
}

export function compact(n: number): string {
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)}K`;
  return `${n}`;
}

// -------------------------------- data -----------------------------------

const now = Date.now();
const iso = (minsAgo: number) => new Date(now - minsAgo * 60000).toISOString();

export const CLUSTERS: Cluster[] = [
  {
    id: "clw-atlas",
    name: "atlas-prod",
    region: "us-east-1",
    tier: "dedicated",
    status: "healthy",
    nodes: 3,
    ramGb: 64,
    createdAt: iso(60 * 24 * 214),
    endpoint: "atlas-prod.us-east-1.markvector.cloud",
  },
  {
    id: "clw-orion",
    name: "orion-staging",
    region: "eu-west-1",
    tier: "standard",
    status: "healthy",
    nodes: 1,
    ramGb: 16,
    createdAt: iso(60 * 24 * 71),
    endpoint: "orion-staging.eu-west-1.markvector.cloud",
  },
  {
    id: "clw-vega",
    name: "vega-sandbox",
    region: "ap-south-1",
    tier: "free",
    status: "provisioning",
    nodes: 1,
    ramGb: 2,
    createdAt: iso(6),
    endpoint: "vega-sandbox.ap-south-1.markvector.cloud",
  },
];

export const COLLECTIONS: Collection[] = [
  {
    id: "col-docs",
    clusterId: "clw-atlas",
    name: "support_docs",
    dims: 1536,
    metric: "cosine",
    points: 482119,
    indexedPct: 100,
    sizeMb: 3120,
    updatedAt: iso(4),
    schema: [
      { field: "title", type: "keyword" },
      { field: "url", type: "keyword" },
      { field: "section", type: "keyword" },
      { field: "updated", type: "datetime" },
    ],
  },
  {
    id: "col-tickets",
    clusterId: "clw-atlas",
    name: "ticket_history",
    dims: 1536,
    metric: "cosine",
    points: 1284402,
    indexedPct: 97,
    sizeMb: 8410,
    updatedAt: iso(19),
    schema: [
      { field: "ticket_id", type: "keyword" },
      { field: "product", type: "keyword" },
      { field: "resolved", type: "bool" },
    ],
  },
  {
    id: "col-products",
    clusterId: "clw-atlas",
    name: "product_catalog",
    dims: 768,
    metric: "dot",
    points: 61204,
    indexedPct: 100,
    sizeMb: 402,
    updatedAt: iso(240),
    schema: [
      { field: "sku", type: "keyword" },
      { field: "price", type: "float" },
      { field: "in_stock", type: "bool" },
    ],
  },
  {
    id: "col-stg",
    clusterId: "clw-orion",
    name: "docs_reindex_test",
    dims: 1536,
    metric: "cosine",
    points: 12044,
    indexedPct: 62,
    sizeMb: 96,
    updatedAt: iso(2),
    schema: [
      { field: "title", type: "keyword" },
      { field: "url", type: "keyword" },
    ],
  },
];

export const KEYS: ApiKey[] = [
  {
    id: "key-prod-rw",
    label: "production-server",
    secret: "mvk_live_7Qp3nR8sZ1vX4bK9wD2fL6hT0aG5eC",
    scope: "write",
    createdAt: iso(60 * 24 * 120),
    lastUsed: iso(3),
  },
  {
    id: "key-analytics",
    label: "analytics-readonly",
    secret: "mvk_live_2Hj9mK4pQ7wR1nB6xS3vD8fA0cL5tE",
    scope: "read",
    createdAt: iso(60 * 24 * 40),
    lastUsed: iso(88),
  },
  {
    id: "key-ci",
    label: "ci-deploy-bot",
    secret: "mvk_live_9Zx2cV5bN8mA3sD6fG1hJ4kL7pQ0wR",
    scope: "admin",
    createdAt: iso(60 * 24 * 12),
    lastUsed: null,
  },
  {
    id: "key-old",
    label: "laptop-scratch",
    secret: "mvk_live_0000deadbeefrevokedkey0000000",
    scope: "read",
    createdAt: iso(60 * 24 * 300),
    lastUsed: iso(60 * 24 * 190),
    revoked: true,
  },
];

const OPS: TraceOp[] = ["search", "upsert", "retrieve", "search", "search", "delete", "create_index"];

/** A believable, deterministic stream of recent requests. */
export function makeTraces(count = 46): Trace[] {
  const out: Trace[] = [];
  let seed = 1337;
  const rnd = () => ((seed = (seed * 1103515245 + 12345) & 0x7fffffff) / 0x7fffffff);
  for (let i = 0; i < count; i++) {
    const op = OPS[Math.floor(rnd() * OPS.length)];
    const col = COLLECTIONS[Math.floor(rnd() * COLLECTIONS.length)];
    const base = op === "upsert" ? 40 : op === "create_index" ? 220 : 9;
    const spike = rnd() > 0.86 ? 180 : rnd() > 0.6 ? 40 : 0;
    const latencyMs = Math.round(base + rnd() * 30 + spike);
    const status: TraceStatus =
      rnd() > 0.96 ? "error" : latencyMs > 120 ? "slow" : "ok";
    const vectors =
      op === "upsert" ? Math.round(50 + rnd() * 4000) : op === "search" ? 1 : Math.round(1 + rnd() * 20);
    out.push({
      id: `req_${(9e9 + i * 7919).toString(36)}`,
      ts: iso(Math.round(i * 1.7 + rnd() * 2)),
      clusterId: col.clusterId,
      collectionId: col.id,
      op,
      status,
      latencyMs,
      vectors,
      key: [KEYS[0], KEYS[1], KEYS[2]][Math.floor(rnd() * 3)].label,
      detail:
        op === "search"
          ? `top_k=${Math.round(3 + rnd() * 12)} · filter=${rnd() > 0.5 ? "product" : "none"}`
          : op === "upsert"
            ? `batch of ${vectors} vectors`
            : op === "create_index"
              ? "HNSW m=16 ef=128"
              : op === "delete"
                ? `by filter · ${vectors} matched`
                : `ids=[${vectors}]`,
    });
  }
  return out;
}

/** Sample retrieval results for the chatbot playground, keyed loosely by query. */
export function retrieve(query: string): Point[] {
  const q = query.toLowerCase();
  const bank: Point[] = [
    {
      id: "doc_8823",
      score: 0.94,
      payload: { title: "Rotating an API key", section: "Security", url: "/docs/keys#rotate" },
      snippet:
        "To rotate a key without downtime, create a second key, deploy it to your services, confirm traffic on the new key in the trace console, then revoke the old one.",
    },
    {
      id: "doc_4410",
      score: 0.88,
      payload: { title: "API key scopes", section: "Security", url: "/docs/keys#scopes" },
      snippet:
        "Keys carry one of three scopes — read, write, or admin. Read keys can query and retrieve; write keys can also upsert; admin keys can manage collections and keys.",
    },
    {
      id: "doc_2093",
      score: 0.81,
      payload: { title: "Handling 401 responses", section: "Errors", url: "/docs/errors#401" },
      snippet:
        "A 401 means the key was missing, malformed, or revoked. Check that the Authorization header reads `Bearer mvk_live_…` and that the key is still active.",
    },
    {
      id: "tkt_51827",
      score: 0.72,
      payload: { title: "Customer: key leaked in client bundle", product: "sdk-js", resolved: "true" },
      snippet:
        "Never ship a write key to the browser. Use a read key with a payload filter, or proxy queries through your own backend so the write key stays server-side.",
    },
    {
      id: "doc_1120",
      score: 0.63,
      payload: { title: "Rate limits", section: "Limits", url: "/docs/limits" },
      snippet:
        "Free clusters allow 60 requests/minute per key. Standard and dedicated clusters scale with node count; burst headroom is shown live in the trace console.",
    },
  ];
  // Nudge scores a little by query so the demo feels responsive, keep it stable.
  const bump = q.includes("key") || q.includes("auth") ? 0 : q.length % 5 === 0 ? -0.04 : -0.02;
  return bank.map((p) => ({ ...p, score: Math.max(0.4, Math.min(0.99, p.score + bump)) }));
}

export const REGION_LABEL: Record<Region, string> = {
  "us-east-1": "US East (N. Virginia)",
  "eu-west-1": "EU West (Ireland)",
  "ap-south-1": "Asia Pacific (Mumbai)",
};
