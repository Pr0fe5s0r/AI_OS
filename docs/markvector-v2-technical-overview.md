# MarkVector v2 — Technical Overview

*Engineering-facing companion to the sales briefing. Describes the platform as it is
actually built in the "Knowledge-base" branch: the data model, the storage split, the
ingestion pipeline, both retrieval paths, the grounding guarantees, and the
self-organising memory. File references point at the real modules.*

---

## 1. What the platform is

MarkVector is a **document knowledge engine**: you feed it files, it indexes them into a
retrievable store, and it answers natural-language questions with answers that are
**grounded in — and cited back to — the exact passages they came from.**

The core (`packages/core`) is a plain Python library. It is wrapped by a FastAPI service
(`apps/api`), an arq background worker (`apps/worker` / `packages/core/pipeline.py`), a
Next.js frontend (`apps/web`), and a first-class SDK (`sdk/markvector` Python,
`sdk/js` TypeScript).

Two architectural rules are enforced in CI and shape everything below:
- **All Cypher lives in `packages/core/graph.py`.** Nothing else talks to Neo4j.
- **All LLM calls live in `packages/core/llm.py`.** One embed/chat/vision client.

---

## 2. The unit of knowledge, and the data model

Everything reduces to an **Item** (`packages/shared/schema.py`). Whatever the origin —
an uploaded PDF, a synced Drive doc, a generated report — by the time it reaches storage
it is an Item: **Markdown body + metadata**.

Key types:

| Type | Role |
|---|---|
| `Item` | One document. Carries `id`, `scope`, `title`, `body` (Markdown), `source`, `hash`, `version`, `status`, `period_start/end`, `metadata`. |
| `Scope` | Tenancy: `workspace_id` + optional `collection_id`. Frozen; travels with **every** read and write. |
| `SourceRef` | Where it came from and how to re-fetch: `source`, `locator`, `url`. |
| `Passage` | The retrieval unit — `chunk_id`, `ordinal`, `heading`, `text`, `score`, and (for picture reads) `page` + `regions`. |
| `Hit` | One document-level result, carrying the matching `passages` as evidence. |
| `Lifecycle` | `active` \| `superseded` \| `archived` \| `failed`. **Retrieval serves `active` only.** |

**Documents are the unit of identity and versioning; passages are the unit of
retrieval.** That distinction runs through the whole system.

### The content hash

`content_hash()` (SHA-256 over the normalised Markdown, not the raw bytes) is the pivot
of the write path. One mechanism satisfies four requirements: skip duplicates, reprocess
only what changed on sync, never re-embed unchanged content, and decide when a new
upload *supersedes* the prior version. Re-exporting the same document yields different
bytes but an identical hash, so it correctly reads as "unchanged."

---

## 3. Storage split

Two stores, each doing what it is good at.

### Postgres — system of record
- `kb_items` — documents: body (Markdown), title, source, version lineage, lifecycle
  status, period, metadata, `content_tsv`.
- `kb_chunks` — passages **and** the derived/navigation nodes. Columns worth knowing:
  `node_type` (`fact` | `summary` | `card` | `section_summary`), `ordinal`, `heading`,
  `text`, `content_tsv`, `importance`, `stage`, `merged_from`, `text_hash`,
  `archived_at`, `access_count`, `cycles`, `generated_by`, `probe_question`.
- `consolidation_runs`, `mapper_runs` — audit logs of the background passes.
- Plus classes/classifications, API keys, traces, blobs metadata.

Keyword search (`content_tsv` / `websearch_to_tsquery`) works the moment a passage is
written — it needs no model.

### Neo4j — the living graph + vectors
- `(:Item {item_id, workspace_id, collection_id, title, source, status})` — a
  **mirror**. The body stays in Postgres; the Item node carries identity and lineage and
  **holds no vector of its own** (a whole-document embedding is the average of everything
  it says, which matches nothing it says).
- `(:Chunk {chunk_id, …, embedding, node_type, stage, status})` — the passage, with its
  vector. `(c)-[:PART_OF]->(i)`.
- `(:Chunk)-[:NEAR {similarity}]->(:Chunk)` — the persisted k-NN similarity graph (the
  traversal substrate).
- `SUMMARIZES` / `DERIVED_FROM` edges — navigation and provenance for summary nodes.
- **A single cosine vector index on `Chunk.embedding`** (`EMBED_DIM`, cosine), created
  idempotently by `graph.bootstrap()` on every boot.

**Every Cypher query filters on `workspace_id`** (and collection where given). The
vector index is global, so `vector_search()` over-fetches (`k = limit * 8`) and applies
tenancy + lifecycle + node-type filters *before* returning — the caller never has to
remember to scope.

---

## 4. Ingestion pipeline

`packages/core/pipeline.py`, run as arq jobs so **nothing user-facing waits on it.**

```
ingest_file / ingest_text
   → normalise to Markdown
   → put_item        (content_hash decides create / skip / new version)
   → [if content changed] enqueue embed_item
                          enqueue classify_new_item
```

Then, as separate jobs:

```
embed_item      mirror Item to graph, embed every passage, write Chunk nodes + vectors
                 → [if SUMMARIES_ENABLED] enqueue summarize_item
classify_new_item   file the document into classes (async, never blocks the write)
summarize_item      write the card + section summaries (navigation layer)
```

Design points:

- **Format-agnostic intake.** PDF, DOCX, Markdown, text — all converted to one Markdown
  representation so everything downstream is uniform.
- **Scanned/image documents are read with vision.** When a PDF has no usable text layer,
  `_read_scan()` transcribes it page by page (`pages.transcribe_document`), emitting the
  same `<!-- page N -->` markers a real PDF produces, so sections, passages, and
  citations behave identically. Unread-tail limits are stated *on the page itself*, not
  hidden in metadata. A file that can't be read at all is recorded as `failed` —
  **visible, deterministic id, re-runnable — never silently dropped.**
- **Originals are kept** in blob storage (best-effort; never fails the ingest) so the
  file can be shown/downloaded exactly as uploaded.
- **Embedding is a separate, retryable job.** The Item is committed first, so a failing
  embed costs a retry of the embed, not the ingest. Re-syncing unchanged content makes
  **zero model calls** — that is the cost control, not a policy layer on top.
- **Passages are embedded in one batch call** per document (`embed_many`), not one call
  per passage.

### Chunking

`packages/core/chunks.py` + `chunk.py`. A document's passages are **rebuilt wholesale,
never patched** — editing a paragraph shifts every boundary after it, so reconciling
passage-by-passage would leave stale text that still answers queries. `chunk_id` is
deterministic from `(item_id, version, ordinal)`, so a rebuild lands on the same ids for
unchanged passages (which keeps the graph nodes replaceable cleanly).

---

## 5. Retrieval

Two public modes, both defined in `packages/core/answer.py`.

### Hybrid search (`packages/core/search.py`) — fast, deterministic

Two arms over the **same passages**, fused:
- **Semantic** — Neo4j native vector index, cosine over passage embeddings.
- **Keyword** — Postgres `tsvector`.

Both retrieve passages and **roll up to documents scoring each by its *best* passage**
(a spec that answers in one paragraph should outrank one vaguely on-topic throughout;
averaging inverts that). The winning passage is carried through — that is the citation.
Neither arm is sufficient alone: semantic finds "quarterly performance dip" for "why did
results fall"; keyword finds `SKU-4471` and proper nouns embeddings miss. No re-ranking
stage — fusion already returns the right content. `RetrievalConfig` (weights, limits,
`recall_multiplier`, source/period/item filters) makes "adding an agent configuration,
not code." **Every call writes a `Trace`** — what each arm proposed, what survived
filters, what returned, per-stage timings.

### Agentic navigation (`packages/core/navigator.py`) — the flagship

This replaced a brittle single "which sections should I read?" call that had to commit
before seeing any content — and when it guessed wrong, the store looked empty. Instead
the model **navigates**, one model call per round (`MAX_ROUNDS = 6`), with tools:

- `read_section(doc, section)` — full text of a section, numbered for citing.
- `hybrid_search(query)` — reach a figure/identifier no heading advertises (agentic mode
  only).
- `neighbors(chunk_id)` — hop the persisted `:NEAR` graph from a promising passage to
  related material one step away.
- `look_at_page(doc, page, looking_for)` — **escalation, offered only after something has
  been read**, and only for documents with renderable pages. Reads the page *image* with
  vision for tables whose columns collapsed, forms, and figures/charts whose contents
  never survive the text layer. Returns a faithful transcription (plus `REGION` boxes for
  UI highlighting), never a conclusion.
- `submit_answer(answer, found)` — finish, or declare the store can't answer.

Structural guarantees that make it trustworthy:

- **The catalogue is the entry point.** The agent is handed every document's title +
  table of contents, enriched with the **card blurb and section summaries** where they
  exist (`_catalogue` / `_enrich`). It reasons over *what a section contains*, not word
  overlap.
- **Citations are assigned by the system, not the model.** Each thing read is handed the
  next number in sequence, so `[2]` can only ever mean the second thing actually opened.
  An invented `[7]` refers to nothing and is dropped downstream.
- **Anti-loop and anti-laziness guards:** `MAX_READS`, `MAX_LOOKS = 2`, dedupe of
  re-reads, and once-only "push-backs" — a model that answers *without reading*, or says
  "not found" over a table it never opened, or answers about a figure *from the text
  layer*, is sent back exactly once (`pressed_to_read` / `pressed_to_look`).
- **Deterministic figure routing:** `_where_named_things_live()` looks up which section
  actually holds "Figure 9" from the index, rather than letting the agent guess across
  documents that each have a page 3.
- **Partial-view honesty:** a collection larger than `MAX_DOCUMENTS = 40` under the
  catalogue-only path is flagged as `degraded` rather than silently answered from the
  newest 40.

`vectorless` remains accepted for backward compatibility — it is agentic with search and
graph-hop disabled — but agentic is the default because it reaches the whole store and
picks its own strategy per question.

---

## 6. Grounding — the correctness contract

`packages/core/answer.py`. Three rules, enforced in code:

1. **No passages → no model call.** An empty context is exactly where an LLM confabulates
   most confidently, so the question is answered "nothing here covers that" without
   asking the model.
2. **Every claim cites its passage**, and the passages stay on screen. The writer prompt
   forbids outside knowledge and requires `NOT_IN_CONTEXT` when the passages don't answer.
3. **Citations are verified, not trusted.** `_resolve_citations()` keeps only markers
   pointing at a real supplied passage and strips the rest from the prose. When the model
   answers correctly but *forgets* to cite, `_attribute()` recovers the source by
   **7-word verbatim shingle overlap**; `_attribute_short()` handles one-word factual
   answers (a number/date/code) only when the match is **unambiguous** (present in exactly
   one passage).

Every `Answer` carries a `grounded` flag and an optional `degraded` reason — a degraded
answer can never masquerade as a healthy one. Picture-derived citations carry `page` +
`regions`, so the UI shows the page with the exact cell/row highlighted. If the LLM is
unreachable, the passages are still returned with the reason stated out loud.

---

## 7. Self-organising memory (consolidation)

`packages/core/consolidate.py`. A background pass reshapes the store between queries —
four operations, ordered because each feeds the next:

1. **dedupe** — exact content matches (whitespace-insensitive `text_hash`) collapse to
   the earliest; no threshold, no model call.
2. **merge** — clusters of passages that *say the same thing* (cosine ≥
   `MERGE_THRESHOLD`, default 0.88, configurable and recorded per run) become **one
   summary node**, via union-find transitive closure (so A~B~C fold together once).
   `merged_from` records exactly which passages produced it; runaway groups
   (> `MAX_MERGE_GROUP`) are left alone as a threshold signal.
3. **decay** — archived nodes lose `importance` on a 48h half-life *recomputed from
   `archived_at`* (idempotent under missed/double runs), then drop below `DROP_FLOOR`.
4. **promote** — each live node's `stage` (1 raw → 2 connected → 3 hub/summary → 4
   long-term) is **recomputed every pass** from degree + history, so a node that loses
   its neighbours falls back down.

The same pass computes k-NN once and uses it twice: as the degree signal for promotion,
and as the persisted `:NEAR` edge set the traversal tools walk (`replace_near_edges`).
Retrieval calls `touch()` — used passages gain importance; unused ones decay. **Merging
replaces retrievable text with model-written text**, stated plainly in the module: it is
always marked (`node_type`) and always traceable (`merged_from`), and the uploaded
document is never touched (passages regenerate from `kb_items.body`).

### Index summaries (navigation layer)

`packages/core/summarize.py` + migration `0030_summaries`. Per document: one **card**
(what the whole file covers) and one **section_summary** per section. Both are
model-written, both carry the chunk ids they cover, both live as summary nodes +
`SUMMARIZES` edges + vectors in the graph with text in Postgres — and **both are
navigation-only: retrieval filters them out** (`node_type NOT IN ('card',
'section_summary')`) so an answer's evidence is always a real passage. `generated_by`
distinguishes `ingest` (at upload) from `mapper` (a background probe pass that maps
still-uncovered chunks over time). Gated by `SUMMARIES_ENABLED`.

---

## 8. Multi-tenancy

Two-level: **workspace** (agency) → **collection** (client/brand). `Scope` is frozen and
passed into every core function; every SQL statement and every Cypher query filters on
it. API keys are minted per workspace and can be **bound to a single collection**, in
which case every call they make is confined there regardless of what is requested. A
collection's embedding model + dimensions are fixed at creation — changing either would
invalidate every vector inside it.

---

## 9. API surface (`apps/api/main.py`)

FastAPI, zero business logic — it loads the profile/scope per request and calls core.
Selected routes:

- **Ingest:** `POST /api/items` (text, 202), `POST /api/items/file` (upload, 202).
- **Retrieve:** `GET /api/search`, `GET /api/answer`, `GET /api/answer/stream` (SSE, the
  navigator emits step/token events live).
- **Documents:** `GET /api/items`, `/api/items/{id}`, `/{id}/versions`, `/{id}/chunks`,
  `/{id}/structure`, `/{id}/original`, `/{id}/pages/{page}`, `POST /api/items/batch`.
- **Graph / memory:** `GET /api/chunks/{id}/neighbors`, `/{id}/lineage`,
  `/collections/{id}/graph`, `/collections/{id}/consolidation`,
  `POST /collections/{id}/consolidate`, `/collections/{id}/summaries`,
  `/collections/{id}/mapping`, `POST /collections/{id}/summarize`.
- **Explainability:** `GET /api/traces`, `/traces/{id}`, `/traces/stats`.
- **Org & auth:** clusters, brands, collections CRUD, classes, `POST /api/keys`,
  `GET /api/whoami`, `GET /api/health`.

Ingest returns `202 Accepted` with a `job_id`; indexing is asynchronous.

---

## 10. SDK

`sdk/markvector` (Python) and `sdk/js` (TypeScript) wrap the API as `Markvector` →
`Collection`. Highlights:

- `collection.add(text, locator, wait=…)` / `add_file(path)` — `locator` is the stable
  origin id, so re-writing the same locator **updates** rather than duplicates. `wait=True`
  polls until the document is genuinely searchable (write and filing are separate jobs).
- `search()`, `answer(mode="agentic"|"hybrid")` — `answer.grounded` is checkable
  programmatically.
- `summaries()` — the navigable index (read first to find the right sources),
  `structure()`, `chunks()`, `neighbors(chunk)` — the traversal primitive over the stored
  `:NEAR` graph, `download_original()`.
- `mv.trace(trace_id)` — every candidate, score, and timing behind a search.
- Retries only idempotent (GET) failures — a timed-out write is never blindly repeated.
- `collection.agent(...)` — a tool-using loop driven by a caller-supplied
  OpenAI-compatible LLM, so a customer can plug the engine into their own model.

---

## 11. Deployment & operations

- **Compose stack:** Postgres 16, Neo4j 5 (community), Redis, `api`, `worker`, `web`.
  Ports remapped to avoid local clashes (Postgres `5442`, Redis `6389`, Neo4j
  `7475`/`7688`).
- **Worker:** arq, `max_tries = 3`, `job_timeout = 300s`; `graph.bootstrap()` +
  `blobs.ensure_bucket()` on startup. Scan/health crons for connected sources.
- **Migrations:** Alembic; `alembic upgrade head` inside the api container after adding
  one. `graph.bootstrap()` creates Neo4j constraints/indexes idempotently every boot.
- **Config:** one embedding provider key + `FERNET_KEY` (credential sealing); feature
  flags `SUMMARIES_ENABLED`, `MAPPER_ENABLED`, `CONSOLIDATION_MERGE_THRESHOLD`.
- **Note (this machine):** BuildKit build is broken; build legacy
  (`DOCKER_BUILDKIT=0 docker build -f apps/api/Dockerfile -t ai_os-api:latest .`), retag
  for the worker, and `--force-recreate --no-build`. `api`/`worker` don't hot-reload;
  `web` does.

---

## 12. Where each concern lives (map for readers going deeper)

| Concern | Module |
|---|---|
| Types / data model | `packages/shared/schema.py` |
| Ingestion jobs | `packages/core/pipeline.py` |
| Normalisation / scans | `packages/core/normalise.py`, `pages.py` |
| Passage index | `packages/core/chunks.py`, `chunk.py` |
| Hybrid search + Trace | `packages/core/search.py` |
| Grounded answering | `packages/core/answer.py` |
| Agentic reader | `packages/core/navigator.py` |
| Self-organising memory | `packages/core/consolidate.py`, `neighbours.py` |
| Navigation summaries | `packages/core/summarize.py` |
| All Cypher / graph | `packages/core/graph.py` |
| The one LLM client | `packages/core/llm.py` |
| HTTP surface | `apps/api/main.py` |
| SDKs | `sdk/markvector`, `sdk/js` |

---

*Prepared from the "Knowledge-base" build. The two invariants to keep in mind when
extending: Cypher only in `graph.py`, LLM calls only in `llm.py` — both are CI-enforced.*
