"use client";

import {
  ApiKey,
  Category,
  Cluster,
  Collection,
  CollectionDetail,
  Document,
  MintedKey,
  Point,
  Trace,
  TraceDetail,
} from "./data";

/**
 * The console's only route to the store.
 *
 * Every call carries the session cookie; the API derives the workspace from
 * it, which is why nothing here takes a workspace id. The collection travels
 * as a header rather than a path segment because most endpoints are
 * workspace-wide and only narrow when a collection is selected.
 */

export const API = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function call<T>(path: string, init?: RequestInit & { collection?: string }): Promise<T> {
  const { collection, ...rest } = init || {};
  const headers: Record<string, string> = {};
  if (collection) headers["X-Collection"] = collection;
  if (rest.body && !(rest.body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
  }

  const res = await fetch(`${API}${path}`, {
    credentials: "include",
    ...rest,
    headers: { ...headers, ...(rest.headers as Record<string, string>) },
  });

  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail || detail;
    } catch {
      /* not a JSON error body */
    }
    throw new ApiError(res.status, detail);
  }
  return res.status === 204 ? (undefined as T) : ((await res.json()) as T);
}

// --------------------------------- identity ---------------------------------

export type Me = { workspace_id: string; identified_as: string | null; via: string };

export const whoami = () => call<Me>("/api/whoami");

export const signIn = (email: string, password: string) =>
  call("/api/auth/login", { method: "POST", body: JSON.stringify({ email, password }) });

export const signUp = (email: string, password: string, workspace: string) =>
  call("/api/auth/register", {
    method: "POST",
    body: JSON.stringify({
      email,
      password,
      workspace_name: workspace || "My workspace",
      name: email.split("@")[0],
    }),
  });

export const signOut = () => call("/api/auth/logout", { method: "POST" });

// -------------------------- clusters and collections --------------------------

type RawCollection = {
  collection_id: string;
  name: string;
  description?: string | null;
  embedding_model: string;
  dimensions: number;
  items?: number;
  cluster_id?: string;
  stats?: {
    items: number;
    superseded: number;
    failed: number;
    characters: number;
    sources: number;
  };
};

const toCollection = (c: RawCollection, clusterId: string): Collection => ({
  id: c.collection_id,
  clusterId: c.cluster_id || clusterId,
  name: c.name,
  description: c.description,
  embeddingModel: c.embedding_model,
  dimensions: c.dimensions,
  items: c.stats?.items ?? c.items ?? 0,
});

export async function clusters(): Promise<Cluster[]> {
  const payload = await call<{
    clusters: {
      cluster_id: string;
      name: string;
      region: string;
      created_at: string;
      collections: RawCollection[];
    }[];
  }>("/api/clusters");

  return payload.clusters.map((c) => ({
    id: c.cluster_id,
    name: c.name,
    region: c.region,
    createdAt: c.created_at,
    collections: c.collections.map((col) => toCollection(col, c.cluster_id)),
  }));
}

export const createCluster = (name: string) =>
  call<{ cluster_id: string }>("/api/clusters", {
    method: "POST",
    body: JSON.stringify({ name }),
  });

export const createCollection = (name: string, clusterId?: string) =>
  call<RawCollection>("/api/collections", {
    method: "POST",
    body: JSON.stringify({ name, cluster_id: clusterId }),
  });

export async function collection(id: string): Promise<CollectionDetail> {
  const raw = await call<RawCollection & { cluster_id: string }>(`/api/collections/${id}`);
  return {
    ...toCollection(raw, raw.cluster_id),
    stats: raw.stats || { items: 0, superseded: 0, failed: 0, characters: 0, sources: 0 },
  };
}

export const deleteCollection = (id: string) =>
  call<{ items_removed: number }>(`/api/collections/${id}`, { method: "DELETE" });

export type CollectionShape = {
  nodes: {
    id: string;
    title: string;
    source: string;
    degree: number;
    category: string | null;
    categoryName: string | null;
  }[];
  edges: { src: string; dst: string; similarity: number; kind: string }[];
  truncated: boolean;
  k: number;
};

/** The collection as a neighbour graph, built from the embeddings themselves. */
export const collectionGraph = (id: string, k = 3) =>
  call<CollectionShape>(`/api/collections/${id}/graph?k=${k}`);

// ----------------------------------- keys -----------------------------------

export async function keys(): Promise<ApiKey[]> {
  const payload = await call<{
    keys: {
      key_id: string;
      name: string;
      prefix: string;
      scopes: string[];
      created_by: string | null;
      created_at: string;
      last_used_at: string | null;
      revoked: boolean;
    }[];
  }>("/api/keys");

  return payload.keys.map((k) => ({
    id: k.key_id,
    name: k.name,
    prefix: k.prefix,
    scopes: k.scopes,
    createdBy: k.created_by,
    createdAt: k.created_at,
    lastUsed: k.last_used_at,
    revoked: k.revoked,
  }));
}

export async function createKey(name: string, scopes: string): Promise<MintedKey> {
  const k = await call<{ key_id: string; name: string; key: string; scopes: string[] }>(
    "/api/keys",
    { method: "POST", body: JSON.stringify({ name, scopes }) }
  );
  return { id: k.key_id, name: k.name, key: k.key, scopes: k.scopes };
}

export const revokeKey = (id: string) => call(`/api/keys/${id}`, { method: "DELETE" });

// ---------------------------------- writing ----------------------------------

export const addText = (
  collectionId: string | undefined,
  body: {
    source: string;
    locator: string;
    body: string;
    title?: string;
    url?: string;
  }
) =>
  call<{ job_id: string }>("/api/items", {
    method: "POST",
    body: JSON.stringify(body),
    collection: collectionId,
  });

export function addFile(collectionId: string | undefined, file: File) {
  const form = new FormData();
  form.append("file", file);
  form.append("source", "upload");
  return call<{ job_id: string }>("/api/items/file", {
    method: "POST",
    body: form,
    collection: collectionId,
  });
}

export const formats = () => call<{ supported: string[] }>("/api/formats");

// ---------------------------------- reading ----------------------------------

type RawDoc = {
  id: string;
  title: string;
  body: string;
  version: number;
  status: string;
  created_at: string | null;
  source: { source: string; locator: string; url?: string | null };
  classes?: Category[];
};

const toDoc = (d: RawDoc): Document => ({
  id: d.id,
  title: d.title,
  body: d.body,
  source: d.source?.source || "",
  locator: d.source?.locator || "",
  version: d.version,
  status: d.status,
  createdAt: d.created_at,
  categories: d.classes || [],
});

export async function documents(
  collectionId: string | undefined,
  limit = 100
): Promise<Document[]> {
  const payload = await call<{ items: RawDoc[] }>(`/api/items?limit=${limit}`, {
    collection: collectionId,
  });
  return payload.items.map(toDoc);
}

export type SearchOutcome = {
  matches: Point[];
  traceId: string;
  tookMs: number;
  degraded: string | null;
};

export async function search(
  collectionId: string | undefined,
  query: string,
  limit = 8
): Promise<SearchOutcome> {
  const payload = await call<{
    trace_id: string;
    took_ms: number;
    degraded: string | null;
    results: {
      item_id: string;
      title: string;
      excerpt: string;
      score: number;
      semantic: number;
      keyword: number;
      source: { source: string; locator: string; url?: string | null };
      classes?: Category[];
    }[];
  }>(`/api/search?q=${encodeURIComponent(query)}&limit=${limit}`, {
    collection: collectionId,
  });

  return {
    traceId: payload.trace_id,
    tookMs: payload.took_ms,
    degraded: payload.degraded,
    matches: payload.results.map((r) => ({
      id: r.item_id,
      title: r.title,
      // Highlight markers are the server's business, not the reader's.
      excerpt: (r.excerpt || "").replace(/\[\[|\]\]/g, ""),
      score: r.score,
      semantic: r.semantic,
      keyword: r.keyword,
      source: r.source?.source || "",
      locator: r.source?.locator || "",
      url: r.source?.url,
      categories: r.classes || [],
    })),
  };
}

// ---------------------------------- traces ----------------------------------

type RawTrace = {
  trace_id: string;
  query: string;
  collection_id: string | null;
  result_count: number;
  duration_ms: number;
  degraded: string | null;
  via: string;
  actor: string | null;
  created_at: string;
};

const toTrace = (t: RawTrace): Trace => ({
  id: t.trace_id,
  query: t.query,
  collectionId: t.collection_id,
  resultCount: t.result_count,
  durationMs: t.duration_ms,
  degraded: t.degraded,
  via: t.via,
  actor: t.actor,
  createdAt: t.created_at,
});

export async function traces(
  collectionId: string | undefined,
  opts: { limit?: number; onlyEmpty?: boolean; onlyDegraded?: boolean } = {}
): Promise<Trace[]> {
  const params = new URLSearchParams({ limit: String(opts.limit ?? 100) });
  if (opts.onlyEmpty) params.set("only_empty", "true");
  if (opts.onlyDegraded) params.set("only_degraded", "true");
  const payload = await call<{ traces: RawTrace[] }>(`/api/traces?${params}`, {
    collection: collectionId,
  });
  return payload.traces.map(toTrace);
}

export async function trace(id: string): Promise<TraceDetail> {
  const raw = await call<RawTrace & Omit<TraceDetail, keyof Trace>>(`/api/traces/${id}`);
  return { ...toTrace(raw), ...raw } as TraceDetail;
}

export type TraceStats = {
  window_hours: number;
  queries: number;
  avg_ms: number;
  p95_ms: number;
  empty: number;
  degraded: number;
};

export const traceStats = (collectionId?: string) =>
  call<TraceStats>("/api/traces/stats", { collection: collectionId });

// --------------------------------- snippets ---------------------------------

export type Snippets = {
  collection_id: string;
  base_url: string;
  key_env: string;
  install: Record<string, string | null>;
  snippets: Record<string, string>;
};

export const snippets = (collectionId: string) =>
  call<Snippets>(`/api/snippets?collection=${encodeURIComponent(collectionId)}`);
