# MarkVector — Answers to Enque AI vendor questions

**Answered:** 2026-08-14 · **Against:** MarkVector as built today (commit `d7a12f0`)

Every ✅ below is something you can exercise now. Every 🔨 is honest: it does not exist,
and we are telling you that rather than describing an intention as a feature.

| Mark | Meaning |
|---|---|
| ✅ | **Shipped.** Works today, in the code, covered by tests. |
| 🔨 | **Not built.** We would build it. Scope and timing to be agreed. |
| 💬 | **Commercial / contractual.** Needs our team, not our codebase. |

**The one thing to read first:** MarkVector's primary deployment model is **self-hosted**
— Docker Compose bringing up Postgres, Neo4j, Redis, MinIO, the API and a worker in your
own infrastructure. That single fact answers your residency, training, BYOK and
sub-processor blockers more cleanly than any assurance we could give about a service we
run for you. Where an answer below depends on it, we say so.

---

## 2 · Tenancy & scale

| # | P | Answer |
|---|---|---|
| 1 | 🔴 | ✅ **No hard limit, and nothing degrades with collection count.** A collection is a row plus an indexed `collection_id` on every query; there is no per-collection index, shard, or resource. 200+ per workspace is fine by construction. **Honest caveat:** our largest tested workspace is ~15 collections. We have not load-tested 200, and we will not claim a number we have not measured — we would run that test with you before you commit the schema. |
| 2 | 🟡 | ✅ **Many small collections is the better model, and it is not close.** A collection scopes the search at the database and graph query, so a small collection is a genuinely smaller search space. `files=[...]` on one large collection filters *after* the store has decided what the question is about — and in agentic mode the document catalogue the agent reasons over grows with the collection, which costs both latency and routing accuracy. No cost difference in the engine. |
| 3 | 🟡 | ✅ **Two levels exist:** workspace → **cluster** → collection. Clusters are real (`/api/clusters`, `create_cluster`), so agency → client maps onto cluster → collection. Deeper nesting than that is flat. |
| 4 | 🟡 | ✅ No limit on workspaces per account (membership rows). **We recommend one workspace per agency**, clusters for grouping, one collection per client. That makes your operator-isolation boundary the same boundary the product enforces. |
| 5 | ⚪ | ✅ No document-count or storage limit in the product — it is your Postgres/MinIO. Max upload **128 MB** default (`MAX_UPLOAD_MB`). Page rendering caps at 5,000 pages; vision transcription of scans caps at 200 pages (`MAX_TRANSCRIBE_PAGES`), and a truncated scan says so on the document rather than being quietly short. |

---

## 3 · Keys & access control

| # | P | Answer |
|---|---|---|
| 6 | 🟡 | ✅ **Yes, server-enforced, in two places deliberately.** `workspace_scope` confines every call a bound key makes to its collection; `enforce_binding` re-checks routes that read the collection from the URL path. Without the second check a key bound to A could reach B by putting B in the path — we found that and closed it. A header naming a different collection is a 403. |
| 7 | 🟡 | ✅ `read` and `write`. A read-only key gets a 403 on every mutating route (`require_write`, checked once at the edge). 🔨 **There is no ingest-only scope.** A write key can also read. We would add `write`-without-`read` — it is a scope-string change plus enforcement, not an architectural one. |
| 8 | ⚪ | 🔨 **One collection per key today**, or workspace-wide. Multi-collection binding is not built. |
| 9 | 🔴 | 🔨 **No — and this is the honest answer to your blocker.** A workspace-wide key today can read collection contents. There is no management-only scope, so "create/rename/delete collections and mint keys, but cannot read tenant documents" is **not achievable as shipped**. It is a real gap, not a misunderstanding. What it needs: a third scope (`manage`) and an audit of which routes require read vs. manage. We think it is small and we would want to agree the exact route matrix with you, because it is your isolation model that defines it. |
| 10 | 🟡 | ✅ Revocation is immediate and the row is kept so the audit trail survives. **Zero-downtime rotation works today** by minting the new key before revoking the old. ⚠️ In-flight requests do **not** survive revocation: the key is resolved on every call, so a revoked key fails at once. That is deliberate — a grace window on a revoked credential is the opposite of what revocation is for. |
| 11 | 🟡 | ✅ Three layers. `api_keys.last_used_at` per key; an `audit_log` table (actor, action, target, metadata); and **every retrieval writes a trace recording which key ran it** (`via`, `actor = key:<name>`). So "which key read what, when" is answerable per query, not just per session. |

---

## 4 · Models & bring-your-own-key

| # | P | Answer |
|---|---|---|
| 12 | 🔴 | **Precise answer, because this is your blocker.** `answer()` runs on the provider configured **on the server**; it does not accept a per-request model key. `agent()` is different in kind: **the agent loop runs inside your process, in the SDK, against an OpenAI-compatible client you construct** — the LLM call is yours, and MarkVector never sees the key. So: ✅ **`agent()` satisfies BYOK by construction.** For `answer()`, self-hosting makes the server's key *your* key, set by you in your own environment — which we believe meets your rule, but we want you to judge that rather than us assert it. 🔨 If you need per-request keys on `answer()` under a hosted deployment, that is a build. |
| 13 | 🟡 | ✅ Whatever you configure: `LLM_PROVIDER` selects an adapter (OpenAI, Nebius, OpenRouter — all OpenAI-compatible), with `CHAT_MODEL`, `EMBED_MODEL` and `VISION_MODEL` set independently. Region is your deployment region plus your provider's. Our own reference stack runs Qwen models on Nebius. |
| 14 | 🟡 | ✅ Configurable (`EMBED_MODEL`, 1536 dims in the reference stack). 🔨 **Changing it does not re-embed automatically**, and it must not — a mixed-dimension index silently returns nonsense. Changing the embedding model is a deliberate re-index of the affected collections. Who bears that cost is 💬 commercial. |
| 15 | 🟡 | ✅ Yes — the model is a pinned environment variable in your deployment. Nothing changes it but you. |

---

## 5 · Ingestion

| # | P | Answer |
|---|---|---|
| 16 | 🟡 | ✅ Ingestion is **already asynchronous**: `POST /api/items/file` returns **202 with a `job_id`** immediately. A 300-page PDF never blocks a request. 🔨 **No outbound completion webhook** — today you poll the item. We would add a callback; the job pipeline is the right place and it is a contained change. |
| 17 | ⚪ | ✅ **Measured, not estimated.** A 962-page, 8.1 MB PDF: **25s** to parse, normalise and store; **94–109s** to embed its 3,915 passages. Pro-rata a ~100-page PDF is roughly **3s parse + ~10s embed**. Embedding dominates and scales with passage count, not file size. |
| 18 | 🟡 | ✅ **Full replace, versioned.** Same locator → a **new version**; the prior version is marked `superseded` and stays for lineage but is never retrievable. Passages are rebuilt **wholesale**, so prior `chunk_id`s are invalidated — deliberately, because editing a paragraph shifts every boundary after it and reconciling passage-by-passage would leave stale text still answering queries. Verified on the 962-page book today: v1 superseded, v2 active. **Locator-as-idempotency-key is safe by construction.** ⚠️ One sharp edge: the identity is (workspace, **collection**, source, locator). The same file ingested into a different collection is a different document, not a new version. |
| 19 | 🟡 | ✅ **Spreadsheets and decks are covered.** PDF, DOCX, **XLSX/XLSM**, **PPTX**, **CSV/TSV**, Markdown, plain text, and images (PNG/JPG/WEBP/GIF/BMP/TIFF, read with a vision model). 🔨 **HTML is not** in the registry — it is one parser class and one registry line. |
| 20 | ⚪ | 🔨 **No bulk ingestion API** — one call per document. (`/api/items/batch` is a batch *read*, not a write.) For backfill today you fan out calls; the queue absorbs it. A true bulk endpoint is a build. |
| 21 | 🔴 | **Split answer, and the split is the point.** ✅ Arbitrary metadata **can be attached** — every document carries a JSONB `metadata` object you control, plus `period_start`/`period_end` and a first-class `classes` tagging system. 🔨 **Retrieval cannot filter by it.** Today's filters are collection, document ids (`files=[...]`), source, and date range. Filtering by `metadata->>'client_id'` is **not built**. It is a `WHERE` clause in the keyword arm and a property filter in the graph arm — genuinely small — but it does not exist today and we will not pretend otherwise. **See the note under §11.** |
| 22 | 🟡 | ✅ The write is an atomic `INSERT … ON CONFLICT DO UPDATE`, so two simultaneous writes to one locator cannot interleave into a corrupt row; one wins and both land as versions. ⚠️ We have not written a test that specifically races two ingests of the same locator, and we would add one before you rely on it. |

---

## 6 · Retrieval & answers

| # | P | Answer |
|---|---|---|
| 23 | 🟡 | 🔨 `files=[...]`, collection, source and date range — **no metadata filter**. Same gap as #21, same fix. |
| 24 | 🟡 | ✅ **Neither a score threshold nor an LLM judgement — it is structural, and that is deliberate.** `grounded` is true only when *all three* hold: at least one citation exists, the walk actually **read** something (not merely reasoned over a table of contents), and the answer text does not admit to coming from outside the documents. A score threshold would let a confident-sounding answer over weak evidence pass. 🔨 Not configurable — we would rather discuss what you need it to mean than expose a dial that weakens it. |
| 25 | 🟡 | ✅ **`page` is populated for text-native PDFs too.** Both PDF paths write `<!-- page N -->` markers, and citations that the chunker cut have their page derived by locating the passage text within the body. Verified today on a text-native 962-page book: citations came back on pages 288, 66 and 230. `page` is null only for formats with no page at all — pasted text, Markdown, DOCX, spreadsheets. |
| 26 | 🟡 | ✅ Percentages (0–100, top-left origin) of **the page as we render it** — a 1400px-wide render of the original page box, served at `GET /api/items/{id}/pages/{n}`. Because it is a straight render of the original page geometry, the same percentages land correctly on your own render of the same page. (We fixed a related bug this week: when we crop to a figure before reading it, the model's coordinates are translated back into page space before they reach a caller.) |
| 27 | ⚪ | ✅ Not rate-limited. 🔨 **No retention policy is implemented — traces persist indefinitely.** For your volume that is a growth question worth designing deliberately rather than inheriting; we would add a TTL and an export. |
| 28 | ⚪ | ✅ `agentic` and `hybrid`. A legacy `vectorless` mode is still accepted for old traces but is strictly worse than `agentic` and is not offered in the UI. |
| 29 | 🟡 | ✅ **Inferred by us, not authored.** An opt-in labelling pass during consolidation asks a model how two already-similar passages stand to each other, and stores the verdict at confidence 0.75 so a typed edge outranks raw cosine. Untyped neighbours are honestly labelled `near` — a resemblance nobody vouched for. 🔨 **No API to author them.** You cannot supply your own today. Worth noting: the `contradicts` rule you liked is enforced in the agent's instructions — it must cite both sides and say the base disagrees with itself. |

---

## 6A · Traces & observability

| # | P | Answer |
|---|---|---|
| 52 | 🔴 | ✅ **Better than your reading of the SDK.** `answer()` returns **`steps` and `trace_id` inline, in the same response** — the reasoning path costs you no second round-trip. `search()` returns `trace_id` inline. What needs the second call is only the **full candidate/score trace** (every candidate each arm proposed, per-arm scores, timings). So: provenance you would *show a client* is already inline; forensic retrieval detail is the extra fetch. |
| 53 | 🔴 | 🔨 **`include_trace=True` is not built.** Given #52 we think you may not need it, but it is a small addition — the trace object is already assembled in the request that would return it. If you want it, say so and we will scope it. |
| 54 | 🟡 | ✅ Not rate-limited, and not separately billed — self-hosted, it is your database. Under a hosted arrangement this is 💬 commercial. |
| 55 | 🟡 | ✅ The shape is stable in practice: `trace_id`, `query`, `config`, `filters`, `semantic[]`, `keyword[]`, `expanded[]`, `fused[]`, `returned[]`, `timings_ms{}`, `degraded`. 🔨 **It is not versioned**, and if you are persisting it you are right to ask. We would version it before you build against it. |
| 56 | 🟡 | 🔨 **No — `steps` is an answer-path concept.** `search()` performs no walk, so there are no steps to report; its equivalent is the candidate trace. |
| 57 | 🟡 | 🔨 **Not built.** `GET /api/traces` lists them (paged, filterable), which is a pull. Webhook, batch export and streaming push are all builds. Given you want them in your own observability store, we would suggest designing this one together rather than us guessing the shape. |
| 58 | 🟡 | ✅ **Yes — and that was deliberate.** A trace is recorded for every retrieval regardless of outcome, and carries a `degraded` field naming the reason when something fell back. An ungrounded answer is exactly when the route matters most. |
| 59 | ⚪ | ✅ Unique per request (128-bit random). Identical repeated queries get distinct ids — the trace records what *this* run did, and caching means two runs of one query genuinely differ. |
| 60 | ⚪ | ⚠️ **Careful answer.** The SDK agent runs in your process and calls MarkVector tools; each retrieval it makes produces its own `trace_id`, which comes back on that tool result. There is no single roll-up trace for a whole agent run, and `tool_calls` does not carry per-tool timings. 🔨 A run-level trace with per-tool timings is a build, and a sensible one. |

---

## 7 · Data governance

| # | P | Answer |
|---|---|---|
| 30 | 🔴 | ✅ **You choose, because you deploy it.** Self-hosted: Postgres, Neo4j, Redis and MinIO all run in your infrastructure, in your region. EU residency means deploying in the EU — there is no MarkVector-side store to reason about. 💬 If you want us to host, region becomes a commercial commitment we would put in writing. |
| 31 | 🟡 | ⚠️ **Honest, and it matters for right-to-erasure.** **Workspace-level erasure is thorough**: a purpose-built path deletes Neo4j → Redis → Postgres in order, commits progress durably at each step, and anonymises or purges the audit log by policy. **Collection-level delete is not** — it removes the Postgres rows, but **Neo4j chunk nodes and the stored originals in object storage are left behind**. That is a real gap. 🔨 We would extend collection delete to use the same path erasure already uses. No retention window: deletion is immediate, not soft. |
| 32 | 🟡 | 🔨 **No — there is no delete-a-document route at all.** The only removals today are delete-a-collection and erase-a-workspace. We found this while testing for you this week. It is a build, and given #31 the two should be done together as one erasure story. |
| 33 | 🔴 | ✅ **Structurally no, and you can verify it rather than trust it.** MarkVector trains nothing and has no training pipeline; it makes inference calls to an OpenAI-compatible endpoint you configure. Self-hosted with your own provider key, the only party that could train on your content is **your** LLM provider under **your** contract with them — we are not in that path. 💬 If you want a contractual "no" from us in writing, that is a clause our team should draft, and we would sign it. |
| 34 | 🟡 | 💬 **Team discussion.** We will not claim a certification we do not hold. |
| 35 | 🟡 | ✅ What the product does: connector credentials are **Fernet-sealed at rest**; API keys are stored **only as SHA-256 hashes** and shown once at creation, so a stolen database yields no working credential. Disk encryption and TLS termination are properties of your deployment, and the keys are yours. |
| 36 | 🟡 | 💬 Team discussion. Technically, self-hosted, your sub-processors are **your LLM provider and your own infrastructure** — MarkVector adds none. |
| 37 | 🟡 | ✅ **Available today — it is the primary deployment model**, not a roadmap item. `docker compose up` brings the whole stack. This is the answer we would lead with for your enterprise agency clients. |

---

## 8 · Commercial & limits

| # | P | Answer |
|---|---|---|
| 38 | 🟡 | 💬 Team discussion. |
| 39 | 🟡 | 💬 Pricing is a team question, but the **cost shape** is factual and worth your planning: `search()` is one embedding call plus two database queries. `answer(mode="hybrid")` adds one LLM call. `answer(mode="agentic")` is 3–5 model round-trips and, when a question is about a diagram, one vision call — which we measured at **84% of a 130-second answer** before optimising it. Agentic is materially more expensive than search. |
| 40 | 🟡 | 🔨 **Not built. There is no rate limiting at all** — not per key, per collection or per workspace. For per-agency fair-use you would need it, and we would build it keyed on the API key (which already carries workspace and collection). |
| 41 | 🟡 | 🔨 **Not built as an API.** The raw material exists — `api_keys.last_used_at`, the audit log, and one trace per query with timings — so attribution per agency is derivable today by querying the database. A metering endpoint on top is a build. |
| 42 | ⚪ | 🔨 No quota system exists, so there is no behaviour at quota to describe. It follows #40. |

---

## 9 · Reliability & operations

| # | P | Answer |
|---|---|---|
| 43 | 🟡 | 💬 Self-hosted, uptime is yours. A hosted SLA and status page are a team discussion. |
| 44 | 🟡 | ⚠️ Partial. ✅ Errors are typed HTTP statuses with human reasons: 401 (bad credential), 403 (scope or binding), 404, 413 (too large, with the actual size and limit), 415 (unreadable format, listing what is supported), 422, 500, **504** (an answer exceeded its deadline, and the message names the cheaper mode). 🔨 **No `Retry-After` header** and no published retryable/non-retryable taxonomy. Both are small and we would do them together. |
| 45 | 🟡 | ⚠️ **Read this one carefully.** `add()` returns 202 once the job is enqueued — for large files the bytes are already in object storage and only the key is queued, but **for small files the payload sits in Redis**. If Redis is lost before the worker picks it up, that write is lost. It is durable *after* indexing, not at 202. 🔨 If you need durability at acknowledgement we would stage every upload to object storage first, not just large ones. |
| 46 | 🟡 | ✅ Achievable today with a separate workspace (keys are per-workspace, data is hard-scoped) or a separate deployment, which is cleaner. 🔨 There is no first-class "environment" concept. |
| 47 | ⚪ | 🔨 **Routes are unversioned** (`/api/...`) and there is no deprecation policy. For a platform building against us, that is a fair thing to want fixed, and we would agree a policy with you. |
| 48 | ⚪ | ✅ **Both.** FastAPI generates a live OpenAPI document at `/openapi.json`. And you do not need to generate a TypeScript client — **one already exists**, see #49. |

---

## 10 · Roadmap

| # | P | Answer |
|---|---|---|
| 49 | ⚪ | ✅ **Already shipped, not planned.** `markvector` v0.3.0 for JavaScript/TypeScript — typed client, models, errors, and the streaming agent. Your frontend can have direct read access today, and a collection-bound read-only key is exactly the credential to give it. |
| 50 | ⚪ | ✅ **Already shipped.** `GET /api/answer/stream` is Server-Sent Events with **token-level streaming of the answer and of the agent's reasoning** — retrieval progress, each tool call and its result, the reasoning, then the answer arriving word by word. The SDK agent streams too. |
| 51 | ⚪ | ⚠️ Partial today: a **workspace-wide key already reads across every collection**, so agency-wide reference material works if you accept that scope. 🔨 What does not exist is sharing one collection *into* others while keeping keys collection-bound — which is what you actually want. That is a build. |

---

## 11 · The blockers — direct answers

| # | Verdict |
|---|---|
| **1** | ✅ **Not a blocker.** No ceiling, no degradation by design. We would run a 200-collection load test with you before you commit, because we have not measured it. |
| **9** | 🔨 **Genuine gap. Answer is no.** A workspace key can read contents; there is no management-only scope. Your operator-isolation model does not work as shipped. It needs a `manage` scope and a route audit — we think small, and we would want to define the route matrix with you. |
| **12** | ✅ **for `agent()`** — the loop runs in your process against your client, so BYOK is structural. **⚠️ for `answer()`** — it uses the server's configured provider, which self-hosted means your key in your environment. If that does not satisfy your rule, per-request keys on `answer()` is a build. |
| **21** | ⚠️ **Half.** Metadata can be **attached** today; retrieval **cannot filter on it**. This is the build we would prioritise first. |
| **30 / 33** | ✅ **Both answered by self-hosting.** Data lives where you deploy it; we train on nothing and are not in the path between your content and your LLM provider. 💬 Written contractual language is a team discussion, and one we welcome. |
| **52 / 53** | ✅ **Not a blocker.** `answer()` already returns `steps` and `trace_id` **inline** — no second round-trip for the provenance you would show a client. Only the full candidate/score trace is a separate fetch, and `include_trace=True` is a small build if you want it. |

### On reading #1 and #21 together — your own note is the right one

You are correct that they interact, and the answer is reassuring in one direction and not
the other. **The collection ceiling is not the constraint** — you can have as many
collections as you have clients. **Metadata filtering is the constraint.** If it stays
unbuilt, every scoping dimension beyond client (category, effective date, document type)
has to become its own collection, and you would be modelling a filter as a namespace.

We would rather build metadata filtering than have you design around its absence. It is a
`WHERE` clause on the keyword arm and a property filter on the vector arm, and the
metadata column already exists on every document. **If you tell us this is the deciding
item, it is the one we would do first.**

---

## Summary — what is honestly missing

Ranked as we would build them, and grouped so you can see the shape of the work.

**Would block your model as specified**
1. **Metadata filtering on retrieval** (#21, #23) — the deciding item.
2. **A `manage` scope that cannot read** (#9) — your operator isolation.
3. **Delete a single document, and complete collection deletion** (#31, #32) — your right-to-erasure story. These are one piece of work.

**Would block your operations at volume**
4. **Rate limiting** (#40) and a **usage/metering API** (#41) — per-agency fair use and billing attribution.
5. **Trace retention, export and versioning** (#27, #55, #57) — you are persisting traces at volume; today they grow forever, are pulled one at a time, and are unversioned.

**Smaller, and we would do them alongside**
6. Ingestion-completion webhook (#16); bulk ingest (#20); HTML parser (#19); ingest-only and multi-collection key scopes (#7, #8); `Retry-After` and a published error taxonomy (#44); durability at 202 for small uploads (#45); API versioning policy (#47); `include_trace=True` (#53); run-level agent traces (#60); cross-collection sharing (#51).

**Commercial, for our team and yours**
SOC 2 / ISO 27001 (#34), DPA and sub-processor list (#36), pricing (#38, #39), quota behaviour (#42), hosted SLA and status page (#43), and written contractual language on residency and training (#30, #33).
