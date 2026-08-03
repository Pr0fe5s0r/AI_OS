// Typed shapes for what the API returns, plus small parsers that turn the raw
// snake_case JSON into camelCase objects an editor can complete.

/* eslint-disable @typescript-eslint/no-explicit-any */
type Raw = Record<string, any>;

export interface Source {
  source: string;
  locator: string;
  url: string | null;
}

export function toSource(d: Raw = {}): Source {
  return { source: d.source ?? "", locator: d.locator ?? "", url: d.url ?? null };
}

export interface Category {
  classId: string;
  name: string;
  confidence: number;
  basis: string | null;
  pinned: boolean;
}

export function toCategory(d: Raw): Category {
  return {
    classId: d.class_id ?? "",
    name: d.name ?? "",
    confidence: Number(d.confidence ?? 0) || 0,
    basis: d.basis ?? null,
    pinned: Boolean(d.pinned),
  };
}

/** The file as uploaded, when the store kept it (PDFs, images, …). */
export interface Original {
  filename: string;
  contentType: string;
  size: number;
}

export interface Document {
  id: string;
  title: string;
  body: string;
  source: Source;
  version: number;
  status: string;
  hash: string;
  createdAt: string | null;
  categories: Category[];
  metadata: Raw;
  /** Present when this document came from an uploaded file. */
  original: Original | null;
}

export function toDocument(d: Raw): Document {
  const o = d.metadata?.original;
  return {
    id: d.id ?? "",
    title: d.title ?? "",
    body: d.body ?? "",
    source: toSource(d.source ?? {}),
    version: Number(d.version ?? 1),
    status: d.status ?? "active",
    hash: d.hash ?? "",
    createdAt: d.created_at ?? null,
    categories: (d.classes ?? []).map(toCategory),
    metadata: d.metadata ?? {},
    original: o ? { filename: o.filename, contentType: o.content_type, size: Number(o.size ?? 0) } : null,
  };
}

/** A passage a document was split into — the unit of retrieval. */
export interface Chunk {
  chunkId: string;
  ordinal: number;
  heading: string;
  text: string;
}

export function toChunk(d: Raw): Chunk {
  return {
    chunkId: d.chunk_id ?? "",
    ordinal: Number(d.ordinal ?? 0),
    heading: d.heading ?? "",
    text: d.text ?? "",
  };
}

/** One node in a document's heading tree (the PageIndex structure). */
export interface Section {
  id: string;
  title: string;
  tokens: number;
  opens: string;
  sections: Section[];
}

export function toSection(d: Raw): Section {
  return {
    id: d.id ?? "",
    title: d.title ?? "",
    tokens: Number(d.tokens ?? 0),
    opens: d.opens ?? "",
    sections: (d.sections ?? []).map(toSection),
  };
}

/** A document's table of contents as a tree — what vectorless search reasons
 *  over. `walk()` flattens every section depth-first. */
export interface Structure {
  itemId: string;
  title: string;
  nodes: number;
  sections: Section[];
}

export function toStructure(d: Raw): Structure {
  return {
    itemId: d.item_id ?? "",
    title: d.title ?? "",
    nodes: Number(d.nodes ?? 0),
    sections: (d.sections ?? []).map(toSection),
  };
}

export function walkSections(sections: Section[]): Section[] {
  const out: Section[] = [];
  for (const s of sections) {
    out.push(s);
    out.push(...walkSections(s.sections));
  }
  return out;
}

/** One search result, with the provenance a citation needs. */
export interface Match {
  id: string;
  title: string;
  excerpt: string;
  source: Source;
  score: number;
  semantic: number;
  keyword: number;
  categories: Category[];
  /** How it was found: "meaning+wording" | "meaning" | "wording". */
  matchedOn: string;
  /** The excerpt without the highlight markers. */
  cleanExcerpt: string;
}

export function toMatch(d: Raw): Match {
  const semantic = Number(d.semantic ?? 0) || 0;
  const keyword = Number(d.keyword ?? 0) || 0;
  const byMeaning = semantic > 0.3;
  const byWording = keyword > 0.01;
  const excerpt = d.excerpt ?? "";
  return {
    id: d.item_id ?? "",
    title: d.title ?? "",
    excerpt,
    source: toSource(d.source ?? {}),
    score: Number(d.score ?? 0) || 0,
    semantic,
    keyword,
    categories: (d.classes ?? []).map(toCategory),
    matchedOn: byMeaning && byWording ? "meaning+wording" : byMeaning ? "meaning" : "wording",
    cleanExcerpt: excerpt.replaceAll("[[", "").replaceAll("]]", ""),
  };
}

/** What a search returned, and the id of the record explaining why. */
export interface Results {
  query: string;
  matches: Match[];
  traceId: string;
  tookMs: number;
  degraded: string | null;
}

export function toResults(d: Raw): Results {
  return {
    query: d.query ?? "",
    matches: (d.results ?? []).map(toMatch),
    traceId: d.trace_id ?? "",
    tookMs: Number(d.took_ms ?? 0),
    degraded: d.degraded ?? null,
  };
}

/** A passage an answer leaned on, addressable by its `[n]` marker. */
export interface Citation {
  marker: number;
  chunkId: string;
  itemId: string;
  title: string;
  heading: string;
  text: string;
  score: number;
}

export function toCitation(d: Raw): Citation {
  return {
    marker: Number(d.marker ?? 0),
    chunkId: d.chunk_id ?? "",
    itemId: d.item_id ?? "",
    title: d.title ?? "",
    heading: d.heading ?? "",
    text: d.text ?? "",
    score: Number(d.score ?? 0) || 0,
  };
}

/** A written answer built only from what was retrieved. Check `grounded`. */
export interface Answer {
  text: string;
  grounded: boolean;
  citations: Citation[];
  matches: Match[];
  mode: string;
  traceId: string;
  tookMs: number;
  degraded: string | null;
}

export function toAnswer(d: Raw): Answer {
  return {
    text: d.answer ?? "",
    grounded: Boolean(d.grounded),
    citations: (d.citations ?? []).map(toCitation),
    matches: (d.results ?? []).map(toMatch),
    mode: d.mode ?? "",
    traceId: d.trace_id ?? "",
    tookMs: Number(d.took_ms ?? 0),
    degraded: d.degraded ?? null,
  };
}

/** What a write did. `document` is set only when `wait: true` was passed. */
export interface WriteResult {
  jobId: string | null;
  status: string;
  document: Document | null;
}

export interface CollectionInfo {
  collectionId: string;
  name: string;
  clusterId: string;
  embeddingModel: string;
  dimensions: number;
  items: number;
}

export function toCollectionInfo(d: Raw): CollectionInfo {
  const stats = d.stats ?? {};
  return {
    collectionId: d.collection_id ?? "",
    name: d.name ?? "",
    clusterId: d.cluster_id ?? "default",
    embeddingModel: d.embedding_model ?? "",
    dimensions: Number(d.dimensions ?? 0),
    items: Number(stats.items ?? d.items ?? 0),
  };
}

/** A key as the server describes it — never the secret itself. */
export interface ApiKey {
  keyId: string;
  name: string;
  prefix: string;
  scopes: string[];
  collectionId: string | null;
  createdBy: string | null;
  createdAt: string | null;
  lastUsedAt: string | null;
  revoked: boolean;
}

export function toApiKey(d: Raw): ApiKey {
  return {
    keyId: d.key_id ?? "",
    name: d.name ?? "",
    prefix: d.prefix ?? "",
    scopes: d.scopes ?? [],
    collectionId: d.collection_id ?? null,
    createdBy: d.created_by ?? null,
    createdAt: d.created_at ?? null,
    lastUsedAt: d.last_used_at ?? null,
    revoked: Boolean(d.revoked),
  };
}

/** A freshly created key — the ONE moment the secret exists. Store `key` now. */
export interface MintedKey {
  keyId: string;
  name: string;
  key: string;
  scopes: string[];
  collectionId: string | null;
}

export function toMintedKey(d: Raw): MintedKey {
  return {
    keyId: d.key_id ?? "",
    name: d.name ?? "",
    key: d.key ?? "",
    scopes: d.scopes ?? [],
    collectionId: d.collection_id ?? null,
  };
}
