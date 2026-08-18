import {
  AuthError,
  IndexingTimeout,
  InvalidRequest,
  MarkvectorError,
  Unavailable,
  errorForStatus,
} from "./errors.js";
import { Agent, type AgentOptions } from "./agent.js";
import {
  type ApiKey,
  type Chunk,
  type Citation,
  type CollectionInfo,
  type Deletion,
  type Document,
  type IndexSummary,
  type Match,
  type MintedKey,
  type Neighbor,
  type Answer,
  type Results,
  type Structure,
  type WriteResult,
  toApiKey,
  toChunk,
  toCollectionInfo,
  toDeletion,
  toDocument,
  toIndexSummary,
  toMintedKey,
  toNeighbor,
  toAnswer,
  toResults,
  toStructure,
} from "./models.js";
import { SDK_CLIENT } from "./version.js";

// No default host. A client that forgets `baseUrl` should fail loudly rather
// than send its documents somewhere — this used to point at our own demo
// deployment, so an omitted option silently shipped a customer's content to a
// server they had never heard of. Data residency is not a default worth having.
const DEFAULT_URL = "";

/** What a key may be minted with.
 *
 *    read    see document contents — bodies, passages, answers, originals,
 *            page pictures, and traces, which carry queries and excerpts
 *    write   ingest and edit those contents, and delete a document
 *    manage  administer the CONTAINERS: create, rename and delete collections,
 *            mint and revoke keys. It reads NOTHING — which is what lets an
 *            operator set a tenant up and wind them down without being able to
 *            open one of their documents.
 */
export const SCOPES = ["read", "write", "manage"] as const;
export type Scope = (typeof SCOPES)[number];

function normaliseScopes(scopes: string | string[]): string {
  const wanted = (Array.isArray(scopes) ? scopes : scopes.split(","))
    .map((s) => s.trim())
    .filter(Boolean);
  const unknown = wanted.filter((s) => !(SCOPES as readonly string[]).includes(s));
  if (unknown.length) {
    // Refused here rather than sent: an unrecognised scope is silently dropped
    // by the server's parser, so a key asked for "admin" would come back
    // looking successful and able to do nothing.
    throw new TypeError(
      `unknown scope(s) ${unknown.join(", ")}; valid scopes are ${SCOPES.join(", ")}.`,
    );
  }
  return wanted.join(",");
}
const RETRYABLE = new Set([429, 500, 502, 503, 504]);

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/** The longest this client will sit inside one retry. A server is entitled to
 *  ask for a minute; a library is not entitled to hold a caller's request open
 *  for it without saying so — RateLimited carries the number for code that
 *  wants to schedule the work properly instead. */
const MAX_RETRY_WAIT_MS = 10_000;

/** What `answerStream` yields: each step as it happens, then the finished
 *  answer as the final value. Tagged by `kind` so a `switch` covers them. */
export type StreamEvent =
  | { kind: "thinking"; text: string }
  | { kind: "tool_call"; name: string; arguments: Record<string, unknown> }
  | { kind: "tool_result"; name: string; summary: string }
  | { kind: "answer"; answer: Answer };

/** One SSE block as a typed step, or null for anything this client does not
 *  recognise — the server may learn to report a new kind of step, and a client
 *  from last month should keep working when it does. */
function parseSse(block: string): StreamEvent | null {
  const line = block.split("\n").find((l) => l.startsWith("data:"));
  if (!line) return null;
  let event: Record<string, any>;
  try {
    event = JSON.parse(line.slice(5).trim());
  } catch {
    return null;
  }
  switch (event.type) {
    case "thinking":
      return { kind: "thinking", text: String(event.text ?? "") };
    case "token":
      return { kind: "thinking", text: String(event.delta ?? "") };
    case "tool_call":
      return {
        kind: "tool_call",
        name: String(event.tool ?? ""),
        arguments: (event.args ?? {}) as Record<string, unknown>,
      };
    case "tool_result":
      return {
        kind: "tool_result",
        name: String(event.tool ?? ""),
        summary: String(event.detail ?? event.count ?? "ok"),
      };
    case "done":
      return { kind: "answer", answer: toAnswer(event.answer ?? {}) };
    case "error":
      throw new Unavailable(String(event.message ?? "the stream failed"));
    default:
      return null;
  }
}

function backoffMs(res: Response, attempt: number): number {
  const raw = res.headers.get("retry-after");
  const asked = raw === null ? NaN : Number(raw);
  if (Number.isFinite(asked) && asked >= 0) {
    return Math.min(asked * 1000, MAX_RETRY_WAIT_MS);
  }
  return 400 * 2 ** attempt;
}

/** What a metadata filter may be given for one key: one value, or several to
 *  match any of. */
export type Filterable = string | number | boolean;
export type Where = Record<string, Filterable | Filterable[]>;

/** One filter value as the server compares it: text.
 *
 *  Booleans are lowercased because that is how JSON — and therefore the stored
 *  metadata — spells them. */
function metaValue(value: Filterable): string {
  return typeof value === "boolean" ? (value ? "true" : "false") : String(value);
}

/** A `where` object as the repeated key:value pairs the API takes.
 *
 *      { client: "acme", kind: ["policy", "notice"] }
 *      -> client:acme, kind:policy, kind:notice
 *
 *  Different keys must all match; the same key repeated matches any of its
 *  values. A key containing a colon cannot be expressed — the server splits on
 *  the first one — so it is refused here rather than silently misread as a
 *  shorter key with a longer value. */
function metaParams(where: Where | undefined): [string, string][] {
  if (!where) return [];
  const out: [string, string][] = [];
  for (const [key, value] of Object.entries(where)) {
    if (!key || key.includes(":")) {
      throw new TypeError(
        `metadata key "${key}" cannot be used as a filter: keys must be non-empty and cannot contain a colon.`,
      );
    }
    const values = Array.isArray(value) ? value : [value];
    if (values.length === 0) {
      throw new TypeError(
        `metadata filter "${key}" has no values; omit the key instead of passing an empty array, which would match nothing.`,
      );
    }
    for (const v of values) out.push(["meta", `${key}:${metaValue(v)}`]);
  }
  return out;
}

export interface MarkvectorOptions {
  apiKey?: string;
  baseUrl?: string;
  /** Per-request timeout, ms. Default 30000. */
  timeout?: number;
  /** Retries for idempotent (GET) failures. Default 2. */
  maxRetries?: number;
  /** Override fetch (tests, custom transport). Defaults to global fetch. */
  fetch?: typeof fetch;
}

/** A file to upload: bytes + a name, or (in Node) a path string. */
export type FileInput = string | { data: Uint8Array | Blob; name: string };

/**
 * A client for one workspace.
 *
 *     const mv = new Markvector({ apiKey: "kb_live_…" });
 *     const docs = mv.collection("client-research");
 *     await docs.add("Q2 paid conversions fell 18 percent.", { locator: "notes/q2", wait: true });
 *     for (const hit of (await docs.search("why did paid results fall")).matches) {
 *       console.log(hit.score, hit.title, hit.matchedOn);
 *     }
 *
 * The key identifies the workspace, so nothing here takes a workspace id.
 */
export class Markvector {
  readonly baseUrl: string;
  private key: string;
  private timeout: number;
  private maxRetries: number;
  private _fetch: typeof fetch;

  constructor(options: MarkvectorOptions = {}) {
    const key = options.apiKey ?? envVar("MARKVECTOR_API_KEY") ?? envVar("KB_API_KEY");
    if (!key) {
      throw new AuthError(
        "No API key. Pass { apiKey } or set MARKVECTOR_API_KEY. " +
          "Create one in the console under Developer → API keys.",
      );
    }
    this.key = key;
    const url = (
      options.baseUrl ??
      envVar("MARKVECTOR_URL") ??
      envVar("KB_URL") ??
      DEFAULT_URL
    ).replace(/\/+$/, "");
    if (!url) {
      throw new InvalidRequest(
        "No baseUrl. Pass { baseUrl } or set MARKVECTOR_URL — for example " +
          "http://localhost:8000 for a local stack, or your own deployment. " +
          "There is deliberately no default: a client that silently sent documents " +
          "to somebody else's server would be a data-residency incident, not a " +
          "convenience.",
      );
    }
    this.baseUrl = url;
    this.timeout = options.timeout ?? 30000;
    this.maxRetries = options.maxRetries ?? 2;
    this._fetch = options.fetch ?? fetch;
  }

  // ------------------------------ plumbing ------------------------------

  /** @internal */
  async send(
    method: string,
    path: string,
    opts: {
      query?: [string, string][];
      body?: unknown;
      form?: FormData;
      collection?: string;
    } = {},
  ): Promise<Response> {
    const url = new URL(this.baseUrl + path);
    for (const [k, v] of opts.query ?? []) url.searchParams.append(k, v);

    const headers: Record<string, string> = {
      Authorization: `Bearer ${this.key}`,
      // Unlike User-Agent, this is available in browsers too. The API records
      // it on retrieval traces so SDK traffic is distinguishable from raw API
      // key calls without trusting it for authentication or tenancy.
      "X-Markvector-Client": SDK_CLIENT,
    };
    if (opts.collection) headers["X-Collection"] = opts.collection;

    let payload: BodyInit | undefined;
    if (opts.form) {
      payload = opts.form; // fetch sets multipart content-type + boundary
    } else if (opts.body !== undefined) {
      headers["Content-Type"] = "application/json";
      payload = JSON.stringify(opts.body);
    }

    let last: MarkvectorError | undefined;
    for (let attempt = 0; attempt <= this.maxRetries; attempt++) {
      let res: Response;
      try {
        res = await this._fetch(url, {
          method,
          headers,
          body: payload,
          signal: AbortSignal.timeout(this.timeout),
        });
      } catch (e) {
        // A timeout on a write could mean the write landed; never repeat it.
        last = new Unavailable(`Could not reach ${this.baseUrl}: ${String(e)}`);
        if (method !== "GET") throw last;
        await sleep(400 * 2 ** attempt);
        continue;
      }
      if (method === "GET" && RETRYABLE.has(res.status) && attempt < this.maxRetries) {
        // The server's own number when it gave one. Guessing shorter hammers a
        // store that has just said it is busy; guessing longer wastes the
        // caller's time. The header is the only party that knows.
        await sleep(backoffMs(res, attempt));
        continue;
      }
      return res;
    }
    throw last ?? new Unavailable("Request failed.");
  }

  /** @internal */
  async request<T>(method: string, path: string, opts: Parameters<Markvector["send"]>[2] = {}): Promise<T> {
    const res = await this.send(method, path, opts);
    if (res.ok) {
      if (res.status === 204) return undefined as T;
      return (await res.json()) as T;
    }
    throw await errorFrom(res);
  }

  // ------------------------------ workspace ------------------------------

  /** Which workspace this key belongs to, and what it may do. */
  whoami(): Promise<Record<string, unknown>> {
    return this.request("GET", "/api/whoami");
  }

  /** Every collection in the workspace, across all clusters. */
  async collections(): Promise<CollectionInfo[]> {
    const tree = await this.request<{ clusters: { cluster_id: string; collections: any[] }[] }>(
      "GET",
      "/api/clusters",
    );
    return tree.clusters.flatMap((c) =>
      c.collections.map((col) => toCollectionInfo({ ...col, cluster_id: c.cluster_id })),
    );
  }

  async createCollection(
    name: string,
    opts: { collectionId?: string; clusterId?: string; description?: string } = {},
  ): Promise<CollectionInfo> {
    const created = await this.request("POST", "/api/collections", {
      body: {
        name,
        collection_id: opts.collectionId ?? null,
        cluster_id: opts.clusterId ?? null,
        description: opts.description ?? null,
      },
    });
    return toCollectionInfo(created as Record<string, unknown>);
  }

  async renameCollection(collectionId: string, name: string): Promise<CollectionInfo> {
    const renamed = await this.request("PATCH", `/api/collections/${collectionId}`, { body: { name } });
    return toCollectionInfo(renamed as Record<string, unknown>);
  }

  async deleteCollection(collectionId: string): Promise<number> {
    const r = await this.request<{ items_removed?: number }>("DELETE", `/api/collections/${collectionId}`);
    return r?.items_removed ?? 0;
  }

  /** A handle for reading and writing one collection. Cheap — no network call. */
  collection(collectionId: string): Collection {
    return new Collection(this, collectionId);
  }

  // -------------------------------- keys --------------------------------

  async keys(): Promise<ApiKey[]> {
    const payload = await this.request<{ keys: any[] }>("GET", "/api/keys");
    return payload.keys.map(toApiKey);
  }

  /** Issue a key. The secret is in the result and NOWHERE else, ever — store
   *  it now. Pass `collectionId` to bind the key to one collection. */
  async createKey(
    name: string,
    opts: { scopes?: string | string[]; collectionId?: string } = {},
  ): Promise<MintedKey> {
    const created = await this.request("POST", "/api/keys", {
      body: {
        name,
        scopes: normaliseScopes(opts.scopes ?? "read,write"),
        collection_id: opts.collectionId ?? null,
      },
    });
    return toMintedKey(created as Record<string, unknown>);
  }

  async revokeKey(keyId: string): Promise<void> {
    await this.request("DELETE", `/api/keys/${keyId}`);
  }

  /** Why a search returned what it did: every candidate, score and timing. */
  trace(traceId: string): Promise<Record<string, unknown>> {
    return this.request("GET", `/api/traces/${traceId}`);
  }

  /** Recent queries against this workspace, newest first.
   *
   *  One row per retrieval, whatever the outcome — an answer that found
   *  nothing is recorded exactly like one that found plenty, which is the
   *  point: the queries worth reading are usually the disappointing ones.
   *  `onlyDegraded` narrows to runs where something fell back, and each row
   *  says which. */
  async traces(
    opts: { limit?: number; onlyEmpty?: boolean; onlyDegraded?: boolean } = {},
  ): Promise<Record<string, unknown>[]> {
    const payload = await this.request<{ traces?: Record<string, unknown>[] }>(
      "GET",
      "/api/traces",
      {
        query: [
          ["limit", String(opts.limit ?? 50)],
          ["only_empty", String(opts.onlyEmpty ?? false)],
          ["only_degraded", String(opts.onlyDegraded ?? false)],
        ],
      },
    );
    return payload.traces ?? [];
  }

  /** How retrieval has behaved over a window: volume, empties, timings. */
  traceStats(opts: { hours?: number } = {}): Promise<Record<string, unknown>> {
    return this.request("GET", "/api/traces/stats", {
      query: [["hours", String(opts.hours ?? 24)]],
    });
  }
}

/** Read and write one collection. */
export class Collection {
  constructor(
    private mv: Markvector,
    readonly id: string,
  ) {}

  private get headers(): { collection: string } {
    return { collection: this.id };
  }

  // -------------------------------- write --------------------------------

  /**
   * Store text. `locator` is the document's stable id at its origin; re-using
   * it updates that document rather than adding a copy. Indexing is async —
   * pass `wait: true` when the next line needs to search what you wrote.
   */
  async add(
    text: string,
    opts: {
      locator: string;
      title?: string;
      source?: string;
      url?: string;
      metadata?: Record<string, unknown>;
      wait?: boolean;
      timeout?: number;
    },
  ): Promise<WriteResult> {
    const accepted = await this.mv.request<{ job_id?: string; status?: string }>("POST", "/api/items", {
      collection: this.id,
      body: {
        source: opts.source ?? "sdk",
        locator: opts.locator,
        body: text,
        title: opts.title ?? null,
        url: opts.url ?? null,
        metadata: opts.metadata ?? {},
      },
    });
    const result: WriteResult = { jobId: accepted.job_id ?? null, status: accepted.status ?? "queued", document: null };
    if (opts.wait) result.document = await this.awaitIndexing(opts.locator, opts.timeout ?? 60000);
    return result;
  }

  /**
   * Store a document from a file — PDF, Word (.docx), Markdown or text. In Node
   * pass a path string; anywhere, pass `{ data, name }`. The original bytes are
   * kept, too, so it can be downloaded again with `downloadOriginal`.
   */
  async addFile(
    file: FileInput,
    opts: { locator?: string; source?: string; wait?: boolean; timeout?: number } = {},
  ): Promise<WriteResult> {
    const { data, name } = await readFileInput(file);
    const locator = opts.locator ?? name;
    const form = new FormData();
    form.append(
      "file",
      data instanceof Blob ? data : new Blob([data.slice().buffer as ArrayBuffer]),
      name,
    );
    form.append("source", opts.source ?? "upload");
    form.append("locator", locator);
    const accepted = await this.mv.request<{ job_id?: string; status?: string }>("POST", "/api/items/file", {
      collection: this.id,
      form,
    });
    const result: WriteResult = { jobId: accepted.job_id ?? null, status: accepted.status ?? "queued", document: null };
    if (opts.wait) result.document = await this.awaitIndexing(locator, opts.timeout ?? 120000);
    return result;
  }

  private async awaitIndexing(locator: string, timeout: number): Promise<Document> {
    const deadline = Date.now() + timeout;
    let delay = 400;
    while (Date.now() < deadline) {
      for (const doc of await this.list({ limit: 200 })) {
        if (doc.source.locator === locator) return doc;
      }
      await sleep(delay);
      delay = Math.min(delay * 1.5, 3000);
    }
    throw new IndexingTimeout(
      `${locator} was accepted but was not searchable within ${Math.round(timeout / 1000)}s. ` +
        "It may still land — check the collection, or raise the timeout.",
    );
  }

  // -------------------------------- read --------------------------------

  /** Search by meaning and exact wording together. Restrict to specific files
   *  with `files` (ids or Documents). */
  async search(
    query: string,
    opts: {
      limit?: number;
      minScore?: number;
      files?: (string | Document)[];
      sources?: string[];
      includeSuperseded?: boolean;
      /** Filter on the metadata a document was ingested with — the same object
       *  passed to `add()` / `addFile()`. Different keys must all match; an
       *  array matches any of its values. */
      where?: Where;
      periodFrom?: string;
      periodTo?: string;
    } = {},
  ): Promise<Results> {
    const q: [string, string][] = [
      ["q", query],
      ["limit", String(opts.limit ?? 10)],
      ["min_score", String(opts.minScore ?? 0)],
      ["include_superseded", String(opts.includeSuperseded ?? false)],
    ];
    for (const f of opts.files ?? []) q.push(["item_ids", docId(f)]);
    for (const s of opts.sources ?? []) q.push(["sources", s]);
    if (opts.periodFrom) q.push(["period_from", opts.periodFrom]);
    if (opts.periodTo) q.push(["period_to", opts.periodTo]);
    q.push(...metaParams(opts.where));
    return toResults(await this.mv.request("GET", "/api/search", { collection: this.id, query: q }));
  }

  /** Retrieval, then a written answer built only from what was retrieved.
   *  `mode` is "agentic" (default; an agent reasons over the heading tree,
   *  searches passages, and hops the similarity graph, reaching the whole
   *  collection) or "hybrid" (passage embeddings + keyword, fused — fast and
   *  deterministic). "vectorless" is still accepted for backward compatibility.
   *  Check `answer.grounded`. */
  async answer(
    question: string,
    opts: {
      mode?: "agentic" | "hybrid" | "vectorless";
      limit?: number;
      sources?: string[];
      /** Narrow the answer to documents whose metadata matches, BEFORE
       *  anything is retrieved — so an agent working for one client can be
       *  held to that client's documents. */
      where?: Where;
    } = {},
  ): Promise<Answer> {
    const q: [string, string][] = [
      ["q", question],
      ["mode", opts.mode ?? "agentic"],
      ["limit", String(opts.limit ?? 8)],
    ];
    for (const s of opts.sources ?? []) q.push(["sources", s]);
    q.push(...metaParams(opts.where));
    return toAnswer(await this.mv.request("GET", "/api/answer", { collection: this.id, query: q }));
  }

  /** The same answer as `answer()`, yielded as it is produced.
   *
   *  Each step arrives as it happens — the model's reasoning, every tool call
   *  and its result — and the LAST value is always the finished Answer:
   *
   *      for await (const event of docs.answerStream("why did churn rise")) {
   *        if (event.kind === "thinking") process.stdout.write(event.text);
   *        if (event.kind === "answer") console.log(event.answer.text);
   *      }
   *
   *  Lazy: nothing is requested until you iterate, and breaking out of the
   *  loop closes the connection. Use `answer()` when you only want the result
   *  — this exists to show the work while it happens. */
  async *answerStream(
    question: string,
    opts: {
      mode?: "agentic" | "hybrid" | "vectorless";
      limit?: number;
      sources?: string[];
      files?: (string | Document)[];
      where?: Where;
    } = {},
  ): AsyncGenerator<StreamEvent> {
    const q: [string, string][] = [
      ["q", question],
      ["mode", opts.mode ?? "agentic"],
      ["limit", String(opts.limit ?? 8)],
    ];
    for (const s of opts.sources ?? []) q.push(["sources", s]);
    for (const f of opts.files ?? []) q.push(["doc", docId(f)]);
    q.push(...metaParams(opts.where));

    const res = await this.mv.send("GET", "/api/answer/stream", {
      collection: this.id,
      query: q,
    });
    if (!res.ok || !res.body) throw await errorFrom(res);

    // Server-Sent Events, decoded a chunk at a time. Split on the blank line
    // that ends an event rather than on every newline: a `data:` payload may
    // itself contain newlines, and splitting naively truncates the answer at
    // the first paragraph break.
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let split: number;
        while ((split = buffer.indexOf("\n\n")) !== -1) {
          const block = buffer.slice(0, split);
          buffer = buffer.slice(split + 2);
          const event = parseSse(block);
          if (event) yield event;
        }
      }
    } finally {
      // Reached on `break` as well as on completion, so abandoning the loop
      // does not leave the connection open.
      await reader.cancel().catch(() => {});
    }
  }

  /** What this collection holds. */
  async list(opts: { limit?: number; offset?: number; category?: string } = {}): Promise<Document[]> {
    const q: [string, string][] = [
      ["limit", String(opts.limit ?? 50)],
      ["offset", String(opts.offset ?? 0)],
    ];
    if (opts.category) q.push(["class_id", opts.category]);
    const payload = await this.mv.request<{ items: any[] }>("GET", "/api/items", {
      collection: this.id,
      query: q,
    });
    return payload.items.map(toDocument);
  }

  /** Just the uploaded files (those with a downloadable original), newest first. */
  async files(opts: { limit?: number } = {}): Promise<Document[]> {
    return (await this.list({ limit: opts.limit ?? 200 })).filter((d) => d.original !== null);
  }

  /** One document, current or at a specific version. */
  async get(documentId: string, opts: { version?: number } = {}): Promise<Document> {
    const q: [string, string][] = opts.version ? [["version", String(opts.version)]] : [];
    return toDocument(await this.mv.request("GET", `/api/items/${documentId}`, { collection: this.id, query: q }));
  }

  /** Every version of a document, newest first. */
  async versions(documentId: string): Promise<Document[]> {
    const payload = await this.mv.request<{ versions: any[] }>("GET", `/api/items/${documentId}/versions`, {
      collection: this.id,
    });
    return payload.versions.map(toDocument);
  }

  /** The passages a document was split into — what actually got indexed. */
  async chunks(documentId: string): Promise<Chunk[]> {
    const payload = await this.mv.request<{ chunks: any[] }>("GET", `/api/items/${documentId}/chunks`, {
      collection: this.id,
    });
    return payload.chunks.map(toChunk);
  }

  /** The collection's navigable index: a card per document (what it is about)
   *  and section summaries (what each part contains), with how many passages
   *  each connects. Read this FIRST to find the right sources — it is the map,
   *  and `search`/`structure`/`get` are how you follow it to the real passages
   *  an answer must cite. */
  async summaries(): Promise<IndexSummary[]> {
    const payload = await this.mv.request<{ summaries: any[] }>(
      "GET",
      `/api/collections/${this.id}/summaries`,
      { collection: this.id },
    );
    return payload.summaries.map(toIndexSummary);
  }

  /** The passages nearest a given one — a hop across the similarity graph. Pass
   *  a passage's chunkId (a Match from `search()` carries one, as does a Chunk
   *  from `chunks()`) and get back what sits next to it in meaning; feed a
   *  returned `neighborId` straight back in to keep walking. These are the
   *  stored links the consolidation pass maintains. */
  async neighbors(chunk: string | Chunk | Match, opts: { limit?: number } = {}): Promise<Neighbor[]> {
    const chunkId = typeof chunk === "string" ? chunk : chunk.chunkId;
    const payload = await this.mv.request<{ neighbors: any[] }>("GET", `/api/chunks/${chunkId}/neighbors`, {
      collection: this.id,
      query: [["limit", String(opts.limit ?? 10)]],
    });
    return payload.neighbors.map(toNeighbor);
  }

  /** The document's heading tree — the PageIndex structure the agent reasons
   *  over to choose which sections to read. */
  async structure(file: string | Document): Promise<Structure> {
    return toStructure(
      await this.mv.request("GET", `/api/items/${docId(file)}/structure`, { collection: this.id }),
    );
  }

  /** Fetch several documents in one call, in the order given. Missing ids are
   *  simply absent. */
  async getMany(files: (string | Document)[]): Promise<Document[]> {
    const payload = await this.mv.request<{ items: any[] }>("POST", "/api/items/batch", {
      collection: this.id,
      body: { ids: files.map(docId) },
    });
    return payload.items.map(toDocument);
  }

  /** Extract the PageIndex structure for a list of files in one round trip. */
  async structures(files: (string | Document)[]): Promise<Structure[]> {
    const payload = await this.mv.request<{ items: any[] }>("POST", "/api/items/batch", {
      collection: this.id,
      body: { ids: files.map(docId), structure: true },
    });
    return payload.items.filter((d) => d.structure).map((d) => toStructure(d.structure));
  }

  /** The original file as uploaded. Returns the bytes, or — with `path` (Node)
   *  — writes them there and returns the path. Throws NotFound when no original
   *  was kept. */
  async downloadOriginal(documentId: string, opts: { path?: string } = {}): Promise<Uint8Array | string> {
    const res = await this.mv.send("GET", `/api/items/${documentId}/original`, { collection: this.id });
    if (!res.ok) throw await errorFrom(res);
    const bytes = new Uint8Array(await res.arrayBuffer());
    if (!opts.path) return bytes;
    const { writeFile } = await import("node:fs/promises");
    await writeFile(opts.path, bytes);
    return opts.path;
  }

  /** File a document by hand. The choice sticks — re-ingestion will not overwrite it. */
  async categorise(documentId: string, categories: string[]): Promise<void> {
    await this.mv.request("PUT", `/api/items/${documentId}/classes`, {
      collection: this.id,
      body: { class_ids: categories },
    });
  }

  /** Delete a document and everything indexed from it.
   *
   *  Two steps, deliberately. The first call destroys NOTHING and returns what
   *  would go:
   *
   *      const plan = await docs.delete(doc);              // nothing deleted
   *      await docs.delete(doc, { confirm: true });        // now it is gone
   *
   *  There is no undo and no trash to restore from, so a caller that means it
   *  says so. Everything derived goes too — passages, vectors, the stored
   *  original, every rendered page — because a passage that outlived its
   *  document would still carry an embedding, and so would still answer
   *  questions. */
  async delete(
    document: string | Document,
    opts: { confirm?: boolean } = {},
  ): Promise<Deletion> {
    const path = `/api/items/${docId(document)}`;
    const res = await this.mv.send("DELETE", path, {
      collection: this.id,
      query: opts.confirm ? [["confirm", "true"]] : [],
    });
    if (opts.confirm) {
      if (!res.ok) throw await errorFrom(res);
      return toDeletion((await res.json()) ?? {}, true);
    }
    // The unconfirmed call is REFUSED by design: 409, carrying the summary. A
    // 2xx here would mean the server deleted something we promised it would
    // not, so it is treated as an error rather than parsed.
    if (res.status === 409) {
      const body = (await res.json()) ?? {};
      return toDeletion(body.detail?.would_delete ?? {});
    }
    throw await errorFrom(res);
  }

  /** The picture of one page — or slide — as PNG bytes.
   *
   *  Two ways to call it, and the first is the one you usually want:
   *
   *      const png = await docs.pageImage(answer.citations[0]);
   *      const png = await docs.pageImage(34, { documentId: doc });
   *
   *  A citation whose `pageImage` is null has no picture — a spreadsheet, a
   *  pasted note, a deck whose conversion has not run — and this throws rather
   *  than requesting a URL that cannot exist. Check `citation.pageImage` first
   *  if you would rather branch than catch. */
  async pageImage(
    page: number | Citation,
    opts: { documentId?: string | Document } = {},
  ): Promise<Uint8Array> {
    let path: string;
    if (typeof page === "number") {
      if (!opts.documentId) throw new TypeError("pageImage(page) needs { documentId }");
      path = `/api/items/${docId(opts.documentId)}/pages/${page}`;
    } else {
      if (!page.pageImage) {
        throw new TypeError(
          `"${page.title}" has no picture of ${page.pageLabel ?? "that page"} — pageImage is null.`,
        );
      }
      path = page.pageImage;
    }
    const res = await this.mv.send("GET", path, { collection: this.id });
    if (!res.ok) throw await errorFrom(res);
    return new Uint8Array(await res.arrayBuffer());
  }

  /** This collection's model, dimensions and live counts. */
  async info(): Promise<CollectionInfo> {
    return toCollectionInfo(await this.mv.request("GET", `/api/collections/${this.id}`));
  }

  // -------------------------------- agent --------------------------------

  /** An agent that answers questions about this collection by reasoning and
   *  calling tools in a loop, driven by an OpenAI-compatible LLM you configure.
   *  See {@link Agent}. */
  agent(options: AgentOptions): Agent {
    return new Agent(this, options);
  }
}

// -------------------------------- helpers --------------------------------

function docId(file: string | Document): string {
  return typeof file === "string" ? file : file.id;
}

function envVar(name: string): string | undefined {
  return typeof process !== "undefined" ? process.env?.[name] : undefined;
}

async function errorFrom(res: Response): Promise<MarkvectorError> {
  let detail = res.statusText;
  try {
    const body = (await res.json()) as { detail?: string | { message?: string } };
    // A refusal may carry structure rather than a sentence — the rate limiter
    // reports the class and the limit alongside its message. Take the message
    // when there is one, so the error still reads like an error.
    if (typeof body?.detail === "string") detail = body.detail;
    else if (body?.detail?.message) detail = body.detail.message;
  } catch {
    /* not a JSON error body */
  }
  return errorForStatus(res.status, detail, res.headers);
}

async function readFileInput(file: FileInput): Promise<{ data: Uint8Array | Blob; name: string }> {
  if (typeof file !== "string") return file;
  const { readFile } = await import("node:fs/promises");
  const { basename } = await import("node:path");
  return { data: new Uint8Array(await readFile(file)), name: basename(file) };
}
