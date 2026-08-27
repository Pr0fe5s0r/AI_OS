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
  /** How this document can be addressed, declared by the server so nothing has
   *  to be discovered by trial: how many parts it has, what they are called,
   *  and whether a picture of one can be fetched. A deck answers 79 / "slide" /
   *  true; a spreadsheet answers 0 / null / false. */
  pages: number;
  pageUnit: string | null;
  pageImage: boolean;
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
    pages: Number(d.pages ?? 0) || 0,
    pageUnit: d.page_unit ?? null,
    pageImage: Boolean(d.page_image),
  };
}

/** What deleting one document would destroy, or did.
 *
 *  Returned by `collection.delete(id)` — which destroys nothing — so a person
 *  can be shown the real numbers before they agree. "Delete this document?" is
 *  a question nobody can answer well; "delete COMPUTER NETWORKS, 2 versions,
 *  3,915 passages, permanently" is. */
export interface Deletion {
  itemId: string;
  title: string;
  versions: number;
  passages: number;
  deleted: boolean;
}

export function toDeletion(d: Raw, deleted = false): Deletion {
  // Two shapes, one meaning. The PREVIEW counts what would go (`versions`,
  // `passages`); the confirmed delete reports what did, by table (`kb_items`,
  // `kb_chunks`). Reading only the first made a real deletion announce
  // "0 versions, 0 passages" — exactly the sort of wrong number that makes a
  // caller doubt whether anything happened.
  return {
    itemId: d.item_id ?? "",
    title: d.title ?? "",
    versions: Number(d.versions ?? d.kb_items ?? 0) || 0,
    passages: Number(d.passages ?? d.kb_chunks ?? 0) || 0,
    deleted,
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
  /** The winning passage's id — the handle a graph hop starts from. Pass it to
   *  `collection.neighbors()` to walk from this hit to what sits near it. */
  chunkId: string;
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
    // The winning passage rides along under `passages`, best first.
    chunkId: d.passages?.[0]?.chunk_id ?? "",
    matchedOn: byMeaning && byWording ? "meaning+wording" : byMeaning ? "meaning" : "wording",
    cleanExcerpt: excerpt.replaceAll("[[", "").replaceAll("]]", ""),
  };
}

/** A passage adjacent to another in the store's similarity graph — what a hop
 *  returns. Feed `neighborId` back to `neighbors()` to keep walking. */
export interface Neighbor {
  neighborId: string;
  itemId: string;
  heading: string;
  title: string;
  nodeType: string;
  /** The edge kind: an authored judgement ("elaborates", "defines", "supports",
   *  "contradicts", "precedes") when `typed` is true, else "near" (cosine). */
  relation: string;
  typed: boolean;
  similarity: number;
}

export function toNeighbor(d: Raw): Neighbor {
  return {
    neighborId: d.neighbor_id ?? "",
    itemId: d.item_id ?? "",
    heading: d.heading ?? "",
    title: d.title ?? "",
    nodeType: d.node_type ?? "fact",
    relation: d.relation ?? "near",
    typed: Boolean(d.typed),
    similarity: Number(d.similarity ?? 0) || 0,
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

/** A navigation summary over a document's passages: a `card` (what the whole
 *  document is about) or a `section_summary` (what one section contains). Read
 *  these first to find the right sources; they are never cited — drop to real
 *  passages (search / get) for evidence. `covers` is how many passages it links. */
export interface IndexSummary {
  chunkId: string;
  nodeType: string;
  itemId: string | null;
  heading: string;
  text: string;
  covers: number;
  generatedBy: string | null;
  probeQuestion: string | null;
}

export function toIndexSummary(d: Raw): IndexSummary {
  return {
    chunkId: d.chunk_id ?? "",
    nodeType: d.node_type ?? "",
    itemId: d.item_id ?? null,
    heading: d.heading ?? "",
    text: d.text ?? "",
    covers: Number(d.covers ?? 0) || 0,
    generatedBy: d.generated_by ?? null,
    probeQuestion: d.probe_question ?? null,
  };
}

/** A box on a page, in PERCENT of the page — not pixels and not points.
 *  Multiply by the width and height you render the page at. The server clamps
 *  every box to the page and drops any that covers most of it, so a region that
 *  survives is a pointer at something specific. */
export interface Region {
  x: number;
  y: number;
  w: number;
  h: number;
  label: string;
}

export function toRegion(d: Raw): Region {
  return {
    x: Number(d.x ?? 0) || 0,
    y: Number(d.y ?? 0) || 0,
    w: Number(d.w ?? 0) || 0,
    h: Number(d.h ?? 0) || 0,
    label: d.label ?? "",
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
  /** Set when the passage was read off a PICTURE of the page rather than out of
   *  its text. Show that page beside the words — it is what makes a transcribed
   *  table checkable instead of merely plausible. `null` means the passage came
   *  from text, where there is no single page to point at. */
  page: number | null;
  /** The same location in the document's own words — "slide 34", "page 12".
   *  Show THIS rather than composing the phrase yourself: a deck has slides,
   *  and working the noun out from a file extension is exactly the per-format
   *  special case the API exists to absorb. */
  pageLabel: string | null;
  /** Where to fetch the picture of that page, or `null` when this document has
   *  none — a spreadsheet, a pasted note, a deck whose conversion has not run.
   *  Stated, so nothing has to request a URL and read a 404 to find out.
   *  Relative to the client's base URL; `collection.pageImage(citation)`
   *  fetches the bytes. */
  pageImage: string | null;
  /** Where on that page. Without these a reader is told "page 14" and left to
   *  search it; with them the highlight lands on the row that was actually
   *  read. Empty whenever the passage came from text. */
  regions: Region[];
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
    page: d.page === null || d.page === undefined ? null : Number(d.page),
    pageLabel: d.page_label ?? null,
    pageImage: d.page_image ?? null,
    regions: (d.regions ?? []).map(toRegion),
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
  /** The route the agent took to get here: which sections it opened, which ids
   *  it reached for and missed, where it stopped. It travels on the answer
   *  rather than in the trace because "why should I believe this" is answered
   *  by the route taken, and nobody opens a trace to find out. Empty for
   *  `mode: "hybrid"`, which has no route — it ranks and hands over. */
  steps: Raw[];
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
    steps: d.steps ?? [],
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
