import { AuthError, MarkvectorError, Unavailable, IndexingTimeout, errorForStatus } from "./errors.js";
import { Agent, type AgentOptions } from "./agent.js";
import {
  type ApiKey,
  type Chunk,
  type CollectionInfo,
  type Document,
  type MintedKey,
  type Answer,
  type Results,
  type Structure,
  type WriteResult,
  toApiKey,
  toChunk,
  toCollectionInfo,
  toDocument,
  toMintedKey,
  toAnswer,
  toResults,
  toStructure,
} from "./models.js";

const DEFAULT_URL = "http://localhost:8000";
const RETRYABLE = new Set([429, 500, 502, 503, 504]);

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

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
    this.baseUrl = (options.baseUrl ?? envVar("MARKVECTOR_URL") ?? envVar("KB_URL") ?? DEFAULT_URL).replace(
      /\/+$/,
      "",
    );
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

    const headers: Record<string, string> = { Authorization: `Bearer ${this.key}` };
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
        await sleep(400 * 2 ** attempt);
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
    opts: { scopes?: string; collectionId?: string } = {},
  ): Promise<MintedKey> {
    const created = await this.request("POST", "/api/keys", {
      body: { name, scopes: opts.scopes ?? "read,write", collection_id: opts.collectionId ?? null },
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
    return toResults(await this.mv.request("GET", "/api/search", { collection: this.id, query: q }));
  }

  /** Retrieval, then a written answer built only from what was retrieved.
   *  `mode` is "vectorless" (default; reason over the heading tree) or "hybrid"
   *  (passage embeddings + keyword). Check `answer.grounded`. */
  async answer(
    question: string,
    opts: { mode?: "vectorless" | "hybrid"; limit?: number; sources?: string[] } = {},
  ): Promise<Answer> {
    const q: [string, string][] = [
      ["q", question],
      ["mode", opts.mode ?? "vectorless"],
      ["limit", String(opts.limit ?? 8)],
    ];
    for (const s of opts.sources ?? []) q.push(["sources", s]);
    return toAnswer(await this.mv.request("GET", "/api/answer", { collection: this.id, query: q }));
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

  /** The document's heading tree — the PageIndex structure vectorless search
   *  reasons over. */
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
    const body = (await res.json()) as { detail?: string };
    if (body?.detail) detail = body.detail;
  } catch {
    /* not a JSON error body */
  }
  return errorForStatus(res.status, detail);
}

async function readFileInput(file: FileInput): Promise<{ data: Uint8Array | Blob; name: string }> {
  if (typeof file !== "string") return file;
  const { readFile } = await import("node:fs/promises");
  const { basename } = await import("node:path");
  return { data: new Uint8Array(await readFile(file)), name: basename(file) };
}
