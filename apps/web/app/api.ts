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

// A public API origin is optional. In the normal Docker/Dokploy deployment the
// browser calls same-origin /api routes and Next.js proxies them to the
// server-only API_URL (for example http://api:8000). This avoids sending a
// container-only hostname—or the user's own localhost—to the browser.
export const API = (process.env.NEXT_PUBLIC_API_URL || "").replace(/\/$/, "");

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

/**
 * Told when the store stops recognising us.
 *
 * A session can end long after the page loaded — it expires, a key is revoked,
 * the workspace is deleted. The shell only checked identity once on mount, so
 * when that happened the console carried on rendering against dead
 * credentials and every action failed with "Not signed in" while the sidebar
 * still showed a workspace. One 401 anywhere is the answer to "are we still
 * signed in?", so it is handled here rather than at each of the callers.
 */
let onLost: (() => void) | null = null;
export const onUnauthorized = (fn: (() => void) | null) => {
  onLost = fn;
};

// Signing in is allowed to fail with a 401 — wrong password is not a lost
// session, and treating it as one would wipe the message explaining itself.
const IS_AUTH = (path: string) => path.startsWith("/api/auth/");

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
    if (res.status === 401 && !IS_AUTH(path)) onLost?.();
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

export const renameCollection = (id: string, name: string) =>
  call<{ collection_id: string; name: string }>(`/api/collections/${id}`, {
    method: "PATCH",
    body: JSON.stringify({ name }),
  });

export type CollectionShape = {
  nodes: {
    id: string;
    title: string;
    source: string;
    degree: number;
    category: string | null;
    categoryName: string | null;
    itemId?: string;
    ordinal?: number;
    document?: string;
    nodeType?: string;
    stage?: number;
    px?: number | null;
    py?: number | null;
  }[];
  edges: { src: string; dst: string; similarity: number; kind: string }[];
  truncated: boolean;
  k: number;
  floor: number;
  /** How many documents the passages came from. */
  documents: number;
  /** `method` is "none" when there were too few passages to project — in
   *  that case the points are laid out plainly and carry no meaning. */
  projection: { method: string; explained_variance: number };
};

/** The collection as a neighbour graph over PASSAGES, built from the
 *  embeddings themselves. A node is a passage, not a file. */
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
      collection_id: string | null;
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
    collectionId: k.collection_id,
    createdBy: k.created_by,
    createdAt: k.created_at,
    lastUsed: k.last_used_at,
    revoked: k.revoked,
  }));
}

export async function createKey(
  name: string,
  scopes: string,
  collectionId?: string | null
): Promise<MintedKey> {
  const k = await call<{
    key_id: string;
    name: string;
    key: string;
    scopes: string[];
    collection_id: string | null;
  }>("/api/keys", {
    method: "POST",
    body: JSON.stringify({ name, scopes, collection_id: collectionId || null }),
  });
  return {
    id: k.key_id,
    name: k.name,
    key: k.key,
    scopes: k.scopes,
    collectionId: k.collection_id,
  };
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

type RawOriginal = { filename: string; content_type: string; size: number };

type RawDoc = {
  id: string;
  title: string;
  body: string;
  version: number;
  status: string;
  created_at: string | null;
  source: { source: string; locator: string; url?: string | null };
  classes?: Category[];
  metadata?: ({ original?: RawOriginal | null } & Record<string, unknown>) | null;
};

const toDoc = (d: RawDoc): Document => {
  const o = d.metadata?.original;
  return {
    id: d.id,
    title: d.title,
    body: d.body,
    source: d.source?.source || "",
    locator: d.source?.locator || "",
    version: d.version,
    status: d.status,
    createdAt: d.created_at,
    categories: d.classes || [],
    metadata: d.metadata || {},
    original: o ? { filename: o.filename, contentType: o.content_type, size: o.size } : null,
  };
};

export async function updateDocument(
  itemId: string,
  collectionId: string | undefined,
  changes: { title?: string; body?: string; metadata?: Record<string, unknown> }
): Promise<Document> {
  const payload = await call<RawDoc>(`/api/items/${encodeURIComponent(itemId)}`, {
    method: "PATCH",
    body: JSON.stringify(changes),
    collection: collectionId,
  });
  return toDoc(payload);
}

export async function documents(
  collectionId: string | undefined,
  limit = 100
): Promise<Document[]> {
  const payload = await call<{ items: RawDoc[] }>(`/api/items?limit=${limit}`, {
    collection: collectionId,
  });
  return payload.items.map(toDoc);
}

/** The passages a document was split into — the unit of retrieval, in order. */
export type DocChunk = { chunk_id: string; ordinal: number; heading: string; text: string };

export async function documentChunks(
  itemId: string,
  collectionId?: string
): Promise<DocChunk[]> {
  const payload = await call<{ chunks: DocChunk[] }>(
    `/api/items/${encodeURIComponent(itemId)}/chunks`,
    { collection: collectionId }
  );
  return payload.chunks;
}

/** Fetch the original file and hand back an object URL for it.
 *
 *  Fetched through the same credentialed path as every other call — rather than
 *  pointing an <iframe src> straight at the API, which the browser would treat
 *  as a third-party request and may strip the session cookie from. The caller
 *  must revoke the URL when done. */
export async function originalBlobUrl(
  itemId: string,
  collectionId?: string
): Promise<{ url: string; contentType: string }> {
  const headers: Record<string, string> = {};
  if (collectionId) headers["X-Collection"] = collectionId;
  const res = await fetch(`${API}/api/items/${encodeURIComponent(itemId)}/original`, {
    credentials: "include",
    headers,
  });
  if (!res.ok) {
    if (res.status === 401) onLost?.();
    throw new ApiError(res.status, "The original file could not be loaded.");
  }
  const blob = await res.blob();
  return { url: URL.createObjectURL(blob), contentType: blob.type };
}

/** Ingests that produced no content, and the reason each gave.
 *
 *  Kept separate from `documents` because a failure is not a document: it must
 *  never appear in the library or be retrievable. It exists so the person who
 *  uploaded the file can be told what went wrong instead of watching a spinner
 *  time out. */
export async function failures(
  collectionId: string | undefined
): Promise<{ locator: string; reason: string }[]> {
  const payload = await call<{ items: (RawDoc & { metadata?: { failure?: string } })[] }>(
    "/api/items?status=failed&limit=50",
    { collection: collectionId }
  );
  return payload.items.map((i) => ({
    locator: i.source?.locator || "",
    reason: i.metadata?.failure || "could not be read",
  }));
}

export type SearchOutcome = {
  matches: Point[];
  traceId: string;
  tookMs: number;
  degraded: string | null;
};

/** One result as the API sends it. Shared by search and answer, which return
 *  the same result shape — answer simply adds written prose above it. */
type RawResult = {
  item_id: string;
  title: string;
  excerpt: string;
  score: number;
  semantic: number;
  keyword: number;
  heading?: string;
  passages?: {
    chunk_id: string;
    ordinal: number;
    heading: string;
    text: string;
    score: number;
    semantic: number;
    keyword: number;
  }[];
  source: { source: string; locator: string; url?: string | null };
  classes?: Category[];
};

// Highlight markers are the server's business, not the reader's.
const unmark = (text: string) => (text || "").replace(/\[\[|\]\]/g, "");

const toPoint = (r: RawResult): Point => ({
  id: r.item_id,
  title: r.title,
  excerpt: unmark(r.excerpt),
  score: r.score,
  semantic: r.semantic,
  keyword: r.keyword,
  source: r.source?.source || "",
  locator: r.source?.locator || "",
  url: r.source?.url,
  categories: r.classes || [],
  heading: r.heading || "",
  passages: (r.passages || []).map((p) => ({ ...p, text: unmark(p.text) })),
});

export async function search(
  collectionId: string | undefined,
  query: string,
  limit = 8
): Promise<SearchOutcome> {
  const payload = await call<{
    trace_id: string;
    took_ms: number;
    degraded: string | null;
    results: RawResult[];
  }>(`/api/search?q=${encodeURIComponent(query)}&limit=${limit}`, {
    collection: collectionId,
  });

  return {
    traceId: payload.trace_id,
    tookMs: payload.took_ms,
    degraded: payload.degraded,
    matches: payload.results.map(toPoint),
  };
}

export type Citation = {
  marker: number;
  chunk_id: string;
  item_id: string;
  title: string;
  heading: string;
  text: string;
  score: number;
  /** Set when this passage was read off a page picture rather than out of
   *  extracted text — the page it came from. The reader is shown that page
   *  beside the words, which is the only thing that makes a transcribed table
   *  checkable rather than merely plausible. */
  page?: number | null;
  /** Where on that page the reading came from, as percentages of the page.
   *  Drawn over the thumbnail so a citation points at the row it was read
   *  from rather than at a whole sheet of paper. Often empty — a box round
   *  the wrong thing is worse than none. */
  regions?: { x: number; y: number; w: number; h: number; label?: string }[];
};

/** The picture of one page, for a citation that was read with vision. */
export function pageImageUrl(itemId: string, page: number): string {
  return `${API}/api/items/${encodeURIComponent(itemId)}/pages/${page}`;
}

/** One thing the agent did on the way to the answer.
 *
 *  `action` is "read" (opened a section, which became a citation), "missed"
 *  (reached for a section id that does not exist) or "answered". A miss is
 *  shown rather than hidden: an agent that groped twice before finding the
 *  right section is telling you something true about the document. */
export type Step = { round: number; action: string; detail: string };

export type AnswerOutcome = SearchOutcome & {
  answer: string;
  citations: Citation[];
  /** The route taken. Empty for hybrid, which ranks rather than navigates. */
  steps: Step[];
  /** Which retrieval found the evidence. Two answers to one question can
   *  differ entirely on this. */
  mode: AskMode;
  /** False when the store had nothing to answer from, when the model said the
   *  passages did not cover the question, or when it wrote prose citing
   *  nothing. The difference between an answer and a guess. */
  grounded: boolean;
};

/** Retrieval plus a written answer built only from what was retrieved.
 *  Separate from `search`, which never returns generated text.
 *
 *  The two live modes are "agentic" (default) and "hybrid". "vectorless" is
 *  retired as a choice but kept in the union because historical turns and
 *  traces still carry it — it is a value we may receive, never one we send. */
export type AskMode = "hybrid" | "vectorless" | "agentic";

/** The JSON both /api/answer and the stream's terminal `done` event carry. */
type RawAnswer = {
  answer: string;
  grounded: boolean;
  citations: Citation[];
  trace_id: string;
  took_ms: number;
  degraded: string | null;
  mode: AskMode;
  /** The route the navigator took. Empty for hybrid, which ranks rather than
   *  navigates. */
  steps?: Step[];
  results: RawResult[];
};

function toAnswerOutcome(payload: RawAnswer, fallbackMode: AskMode): AnswerOutcome {
  return {
    answer: payload.answer,
    mode: payload.mode || fallbackMode,
    grounded: payload.grounded,
    citations: payload.citations || [],
    steps: payload.steps || [],
    traceId: payload.trace_id,
    tookMs: payload.took_ms,
    degraded: payload.degraded,
    matches: (payload.results || []).map(toPoint),
  };
}

/** Everything about an ask that is not the question itself.
 *
 *  Optional throughout and omitted from the URL when unset, so the request the
 *  console sends stays the request it always sent unless something was actually
 *  changed — a default that travels as an explicit parameter is a default
 *  nobody can change later without breaking callers. */
export type AskOptions = {
  /** Restrict the answer to these documents. Empty = the store decides. */
  docs?: string[];
  /** Let the agent read a page as a picture. Off, a figure stops being
   *  readable and the most expensive step in a walk is gone. */
  vision?: boolean;
  /** How the answer should be written. Style only — it cannot change what may
   *  be said, which the server enforces rather than trusting. */
  behaviour?: string;
  /** Let the agent open a whole document's outline when the catalogue was
   *  shortened. Off, it navigates from the catalogue and the hint alone. */
  openDocument?: boolean;
};

/** The query string for an ask, shared by `ask` and the curl the console shows,
 *  so what gets copied is what was actually sent. */
export function askParams(
  question: string,
  limit: number,
  mode: AskMode,
  options: AskOptions = {}
): string {
  const params = new URLSearchParams({ q: question, limit: String(limit), mode });
  for (const id of options.docs ?? []) params.append("doc", id);
  if (options.vision === false) params.set("vision", "false");
  if (options.behaviour?.trim()) params.set("behaviour", options.behaviour.trim());
  if (options.openDocument === false) params.set("open_document", "false");
  return params.toString();
}

export async function ask(
  collectionId: string | undefined,
  question: string,
  limit = 8,
  mode: AskMode = "agentic",
  options: AskOptions = {}
): Promise<AnswerOutcome> {
  const payload = await call<RawAnswer>(
    `/api/answer?${askParams(question, limit, mode, options)}`,
    { collection: collectionId }
  );
  return toAnswerOutcome(payload, mode);
}

/** One step the store reports while it works, as it happens. `done` carries the
 *  same answer /api/answer returns; everything before it is the work. */
export type StreamEvent =
  | { type: "start"; mode: AskMode; documents?: number; sections?: number }
  | { type: "thinking"; delta: string; round?: number }
  | { type: "tool_call"; tool: string; args: Record<string, unknown>; round?: number }
  | {
    type: "tool_result";
    tool: string;
    ok: boolean;
    detail?: string;
    heading?: string;
    title?: string;
    marker?: number;
    count?: number;
    headings?: string[];
  }
  | { type: "retrieved"; documents: number; passages: number }
  | { type: "token"; delta: string }
  | { type: "answer"; text: string; found: boolean }
  | { type: "degraded"; reason: string }
  | { type: "done"; answer: RawAnswer }
  | { type: "error"; message: string };

/** Ask, and watch it work. `onEvent` fires for every step as the server
 *  produces it; the promise resolves with the finished answer, the same shape
 *  `ask` returns. A fetch + stream reader rather than EventSource because the
 *  session travels as a cookie and the collection as a header, neither of which
 *  EventSource can send. */
export async function askStream(
  collectionId: string | undefined,
  question: string,
  limit: number,
  mode: AskMode,
  onEvent: (event: StreamEvent) => void,
  options: AskOptions = {}
): Promise<AnswerOutcome> {
  const params = askParams(question, limit, mode, options);
  const res = await fetch(`${API}/api/answer/stream?${params}`, {
    credentials: "include",
    headers: collectionId ? { "X-Collection": collectionId } : {},
  });
  if (!res.ok || !res.body) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail || detail;
    } catch {
      /* not a JSON error body */
    }
    if (res.status === 401) onLost?.();
    throw new ApiError(res.status, detail);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let outcome: AnswerOutcome | null = null;

  for (; ;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    // SSE frames are separated by a blank line; a frame may span reads, so only
    // whole frames are parsed and the remainder stays in the buffer.
    let boundary: number;
    while ((boundary = buffer.indexOf("\n\n")) !== -1) {
      const frame = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      const dataLine = frame.split("\n").find((line) => line.startsWith("data:"));
      if (!dataLine) continue;
      const event = JSON.parse(dataLine.slice(5).trim()) as StreamEvent;
      if (event.type === "error") throw new ApiError(500, event.message);
      if (event.type === "done") outcome = toAnswerOutcome(event.answer, mode);
      onEvent(event);
    }
  }

  if (!outcome) throw new ApiError(500, "the stream ended before an answer arrived");
  return outcome;
}

export type ChunkDetail = {
  chunk_id: string;
  item_id: string;
  ordinal: number;
  heading: string;
  text: string;
  document: string | null;
  source: string | null;
  locator: string | null;
  url: string | null;
  node_type: string;
  stage: number;
  importance: number;
  archived: boolean;
  merged_from: string[];
};

/** One passage in full — what a point in the graph actually holds. */
export const chunk = (id: string) => call<ChunkDetail>(`/api/chunks/${encodeURIComponent(id)}`);

// -------------------------------- playground --------------------------------

/** A single request run raw: the status, wall time and untransformed body.
 *
 *  `search` and `ask` above shape the payload into what a view renders and drop
 *  the status and timing on the floor. The Playground is the one place those
 *  are the point — it shows the developer exactly what the API returned — so it
 *  gets its own path that hands back the response verbatim. */
export type RawRun = { status: number; ms: number; ok: boolean; body: unknown };

export async function run(path: string, collection?: string): Promise<RawRun> {
  const started = performance.now();
  const res = await fetch(`${API}${path}`, {
    credentials: "include",
    headers: collection ? { "X-Collection": collection } : {},
  });
  const body = await res.json().catch(() => null);
  return { status: res.status, ms: performance.now() - started, ok: res.ok, body };
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
  retrieval?: string;
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
  retrieval:
    t.retrieval === "hybrid" || t.retrieval === "vectorless" ? t.retrieval : "unknown",
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

// -------------------------------- summaries --------------------------------

export type IndexSummary = {
  chunk_id: string;
  node_type: string | null;
  item_id: string | null;
  heading: string | null;
  text: string | null;
  covers: number;
  generated_by: string | null;
  probe_question: string | null;
};

export type MapperRun = {
  id: number;
  question: string | null;
  chunks_mapped: number;
  error: string | null;
  created_at: string | null;
};

export type MappingProgress = {
  total: number;
  mapped: number;
  unmapped: number;
  runs: MapperRun[];
};

export async function fetchSummaries(collectionId: string): Promise<IndexSummary[]> {
  const payload = await call<{ summaries: IndexSummary[] }>(
    `/api/collections/${encodeURIComponent(collectionId)}/summaries`
  );
  return payload.summaries;
}

export async function fetchMapping(collectionId: string): Promise<MappingProgress> {
  return call<MappingProgress>(
    `/api/collections/${encodeURIComponent(collectionId)}/mapping`
  );
}

export const triggerSummarize = (collectionId: string) =>
  call<{ documents: number; queued: number }>(
    `/api/collections/${encodeURIComponent(collectionId)}/summarize`,
    { method: "POST" }
  );

