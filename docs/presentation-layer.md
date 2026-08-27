# MarkVector as a backend — the presentation layer

## Who this is for

You are building a public-facing application. MarkVector is not that
application. It is the knowledge layer underneath it: it holds the documents,
finds the passage that answers a question, writes the answer, and records how
it got there.

This document says three things:

1. **What the backend hands over** — the exact response, field by field.
2. **What your app must show, and why** — the parts a citizen-facing service
   cannot leave out.
3. **What it costs in time** — real measured numbers, each with the reason it
   is that number, and an honest account of how much it varies.

Every figure marked **measured** was taken from this system answering real
questions about real documents. Figures marked **projected** are extrapolated
and say so. Nothing here is a vendor estimate.

---

## Part 1 — The boundary

Your app calls one endpoint and gets one object back.

```
GET /api/answer?q=<question>&mode=hybrid|vectorless
GET /api/answer/stream?q=…      (same, delivered as it happens)
```

```json
{
  "question": "What is the deadline for appealing a rates decision?",
  "answer": "An appeal must be lodged within 28 days of the notice [1].",
  "mode": "hybrid",
  "grounded": true,
  "degraded": null,
  "trace_id": "a2c0a65bf9da32e688da250a",
  "took_ms": 2861,
  "steps": [ { "round": 1, "action": "read", "detail": "Page 8 → [1]" } ],
  "citations": [
    {
      "marker": 1,
      "item_id": "ec68ea677a35006a118f7f04c7712df1",
      "title": "Rates and Valuation Notice 2025",
      "heading": "6.2 Appeals",
      "text": "An appeal must be lodged within 28 days of the date of the notice…",
      "chunk_id": "ec68ea…:n042",
      "score": 0.91,
      "page": 8,
      "regions": [ { "x": 12, "y": 44, "w": 60, "h": 9, "label": "appeal window" } ]
    }
  ]
}
```

### What each field is for

| field | what it means | why your app needs it |
|---|---|---|
| `answer` | the written answer, with `[n]` markers | the thing the user reads |
| `citations[]` | one entry per `[n]`, **with the passage text** | this is the product. See Part 2 |
| `marker` | the number in the prose | lets you make `[1]` clickable |
| `title` / `heading` | which document, which section | tells the user where they are |
| `text` | **the exact passage the answer came from** | the user checks the claim without leaving the screen |
| `page` / `regions` | page number, and boxes on it | show the page image with the used area highlighted |
| `grounded` | whether the answer stands on retrieved evidence | drives whether you present it as an answer at all |
| `degraded` | a plain-language warning, or null | must be surfaced, never swallowed |
| `trace_id` | the audit key for this query | the record that this answer was produced this way |
| `steps` | what the system did to reach it | "why should I believe this" |
| `mode` | which retrieval ran | two answers to one question can differ on this |
| `took_ms` | server-side duration | your own monitoring |

**Citations are assigned by the backend, not written by the model.** Every
section read is handed back with the next number in sequence, so `[2]` cannot
refer to anything but the second thing actually opened. A marker the model
invents resolves to nothing and is dropped before you see it.

---

## Part 2 — What your app must show

### 2.1 The citation is the product, not a footnote

For a government service, the answer is the least defensible thing on the
screen. The passage behind it is what makes it accountable.

**Show, for every `[n]`:**

- the document title and section heading
- **the passage text itself**, in full, without the user navigating away
- the page image with the used region highlighted, when `page` is present
- a link to the original file

A citation the user cannot open is decoration. The API returns `text` on every
citation precisely so that checking a claim costs one click and no round trip.

### 2.2 "Grounded" — and the limit you must design around

`grounded: false` means the answer does not stand on retrieved evidence.
**Present those differently.** Not a warning banner the user learns to skip —
a different treatment: no answer styling, the passages shown as "closest
matches", no confident sentence at the top.

> **A limitation to disclose, not to hide.**
>
> `grounded: true` means the answer cites passages that were actually
> retrieved and that a support check did not reject. It does **not** guarantee
> every claim is stated in those passages.
>
> A measured example. Asked *"which elements are liquid at room temperature?"*
> against a periodic table, the system answered *"Mercury (Hg) and Bromine (Br)
> are liquid at room temperature [1]"* and marked it grounded. The cited
> passage names both elements and **never contains the word "liquid"** — the
> model supplied that from its own knowledge and attached a citation.
>
> A support check now runs on every hybrid answer and catches two of the three
> classes we tested — world knowledge stated as fact, and invented quotes. It
> does not catch this third class, where the model reads meaning into a
> grouping the source never explains.
>
> **What this means for your app:** for any decision with legal or financial
> consequence, the citation must be read, not trusted. Design the screen so
> reading it is the natural act, not an extra step. Where a workflow cannot
> tolerate that, route it to `vectorless` mode, which answered the same
> question honestly — *"the image does not indicate which elements are liquid"*.

### 2.3 The trail — how the answer was reached

`steps[]` is a plain list of what the system did:

```
opened   the index of 2 documents, 16 sections
read     Page 8 → [1]
looked   page 3 → [2]
answered found
```

Show it collapsed by default, expandable. For most users it is reassurance;
for a caseworker challenged on a decision it is the record. It costs you a
disclosure triangle and it is the difference between a system that asserts and
one that shows its work.

### 2.4 The honest empty state

When nothing in the corpus answers the question, the backend says so rather
than improvising. Your app must not dress that up. *"Nothing in these documents
answers that"* is a correct, useful result for a records system — and it is
the behaviour that makes every other answer credible.

### 2.5 Show the wait, not a spinner

Retrieval takes seconds, sometimes tens of seconds. Use the streaming endpoint
and show the steps as they arrive.

**Measured: the first step reaches the browser in 85–230 ms**, on every mode
and every document type tested, even when the full answer takes 20 seconds.
A progress line that names what is happening — *"reading Page 8"* — is a
different experience from a spinner, and it costs nothing.

### 2.6 The audit record

Keep `trace_id` against your own record of who asked and when. The backend
stores the retrieval that produced each answer — which passages were
considered, which were used, which mode ran, how long each stage took. For a
government deployment this is the difference between "the system said so" and
a reconstructable decision.

---

## Part 3 — How retrieval actually works

Two modes. Neither is better; they suit different questions, and your app
should choose per use case rather than pick one.

### Hybrid — matching

1. The question is embedded (one call to the model provider).
2. Passages are ranked two ways at once: **vector similarity** in Neo4j
   (meaning) and **full-text search** in PostgreSQL (exact wording).
3. The two rankings are fused; the best passages across all documents are kept.
4. The model writes an answer **using only those passages**.

Good at: finding a specific figure, name, reference number or clause anywhere
in a large corpus. This is the mode for "what does the regulation say about X".

### Vectorless — navigating

1. The system builds a table of contents for the corpus.
2. The model chooses which sections to open, reads them, and may open more.
3. If the answer lives in something the page *shows* rather than says — a
   table, a chart, a stamped form — it escalates and reads the page **as an
   image** with a vision model.
4. It answers from what it opened.

Good at: long structured documents where the author labelled where things are,
and anything where the text layer is unreliable — scans, forms, tables that
collapse into a run of numbers when extracted.

**Recommended default: hybrid**, with vectorless offered for documents that are
scanned, image-heavy, or heavily tabular.

---

## Part 4 — Benchmarks

### 4.1 What was measured, on what

| corpus | content | size |
|---|---|---|
| **A — Technical** | *Attention Is All You Need* (arXiv) + a periodic table image | 15-page PDF, 60 passages; 153 KB image, 7 passages |
| **B — Long-form** | *War and Peace*, same text as `.docx` **and** `.pdf` | 566,334 words; 4,264 + 4,553 passages; 726 PDF pages |
| **C — Mixed office** | PDF, DOCX, XLSX, PPTX, PNG, scanned PDF | 10 documents, 469 sections |

### 4.2 Answering — measured

| question type | corpus | hybrid | vectorless |
|---|---|---|---|
| Fact from a table | A | **2.9 s** | 8.4 s |
| Fact in 566k words | B | **1.6–6.1 s** | 9.8–17.8 s |
| Spreadsheet lookup | C | — | 4.4 s |
| Presentation slide | C | **3.0 s** | — |
| Chart — reads the picture | A | n/a | 18.8 s |
| Scanned PDF | C | — | 20.8 s |
| Photograph / diagram | A | n/a | 27–36 s |
| **First feedback on screen** | all | **85–120 ms** | **130–230 ms** |

### 4.3 Why those are the numbers

**Hybrid is 2–6 s because it makes exactly two provider calls** — one to embed
the question, one to write the answer.

The database work between them is negligible, and this is the load-bearing
fact for capacity planning. Measured over **8,817 passages** (both copies of
*War and Peace*):

| stage | measured |
|---|---|
| Vector search (Neo4j) | **24–161 ms** |
| Full-text search (PostgreSQL) | **1–57 ms** |
| Assemble results | ~20 ms |
| **Total database work** | **~35–240 ms** |
| Embed the question | **0 ms cached, ~0.3 s typical — and 3.6 s to 22.3 s observed when the provider was congested** |

**Your hardware is not the constraint. The model provider is** — by two orders
of magnitude. Repeated questions embed in 0 ms because the last 512 query
embeddings are cached, which is why a popular query is faster than a novel one.

**Vectorless is 8–18 s because it makes one call per round.** It opens the
index, chooses, reads, and may choose again — typically 3 to 6 calls at ~1.5–2 s
each. It buys the ability to reason about structure and pays for it in round
trips.

**Vision costs 20–36 s because reading a page as an image is the most
expensive call in the system.** A single page read measured 8–35 s depending on
density. It is used only when the text layer cannot answer — which is exactly
when it is worth it.

**First feedback is ~100 ms because it is sent before any model call.** The
system reports what it is about to do, then does it.

### 4.4 Indexing — measured

| document | size | passages | usable after | vectors after |
|---|---|---|---|---|
| 15-page PDF | 2.2 MB | 60 | **4.9 s** | 10.8 s |
| 566k-word DOCX | 1.2 MB | 4,264 | **8.7 s** | 192 s |
| 566k-word PDF | 2.5 MB | 4,553 | **19.0 s** | 183 s |
| 153 KB image | 153 KB | 7 | **69 s** | — |

Two stages, and the distinction matters for your UI:

- **Usable after** — the document is searchable by wording. Show it as
  available at this point.
- **Vectors after** — searchable by meaning too. Typically ~45 ms per passage.

An image takes 69 s because an image has no text: the whole document must be
read by a vision model before anything can be indexed. **Warn users uploading
photographs that indexing takes about a minute.**

A 566k-word book is usable in **8.7 s**. Both formats of the same text produced
within 7% of the same passage count — the PDF slightly more, from page-break
artefacts.

### 4.5 The variance — read this before quoting any number

**The provider's latency varies far more than anything in this system.**
Measured, on identical code and identical inputs:

| observation | |
|---|---|
| Same question, 12 minutes apart | **9.4 s** and **29.3 s** |
| Same 153 KB image, indexed twice | **32.9 s** and **193.6 s** |
| Embedding one question, same text | **0.3 s** and **22.3 s** |
| Worst single query observed | **509 s** |

None of those are code differences. They are the same request to the same
provider at different moments, and the swing is larger than any optimisation
in this system.

**Do not promise a mean.** For a service commitment, quote a *ceiling* and
design for it:

| commitment | basis |
|---|---|
| First feedback under 1 s | ~100 ms measured, wide margin |
| Text answer under 30 s | covers hybrid and vectorless with headroom |
| Image/scan answer under 60 s | covers vision escalation |
| Hard ceiling **300 s**, then an error | enforced by the backend |

That last one is a real bound, not a hope: a query that exceeds it returns an
error explaining what happened. Your app should render that as a normal state.

### 4.6 Throughput and scale

| | |
|---|---|
| Indexing rate | ~45 ms per passage; **8,817 passages in 3.2 min** |
| Re-indexing an unchanged document | **2.7 s, zero provider calls** |
| Re-indexing after a small edit | only changed passages re-embedded — 13 of 15 vectors reused |
| Storage | 3.3 KB/passage in PostgreSQL, 17 KB/passage in Neo4j |
| Projected: 100,000 documents | ~135 GB total |

**Concurrent users are projected, not measured.** All figures come from a
single-user system. Run a load test before committing to a concurrency figure.

---

## Part 5 — Managing it

### 5.1 Choosing the mode per use case

| use case | mode | why |
|---|---|---|
| Search regulations, policy, correspondence | hybrid | fast, finds a clause anywhere |
| Scanned records, forms, certificates | vectorless | reads the page when text extraction fails |
| Spreadsheets and tabular returns | vectorless | keeps rows attached to headers |
| High-volume public self-service | hybrid | 2–6 s is the only mode that suits a web queue |
| Any decision with legal consequence | either, **citation read by a human** | see 2.2 |

### 5.2 Cost and dependency

Inference is a **remote HTTPS dependency**. There is no GPU in this system.
Loss of egress stops indexing and answering; documents already stored remain
readable. Provider spend is likely to exceed server cost at volume and is not
sized here.

### 5.3 What must be disclosed to stakeholders

1. **Answers are generated.** Citations are exact; the sentence around them is
   written by a model.
2. **`grounded` is a strong signal, not a guarantee.** See 2.2, with the
   worked example.
3. **Concurrency is untested.** Single-user latency is measured; throughput
   under load is not.
4. **Latency is provider-dependent** and varies by an order of magnitude.

### 5.4 Open items

| item | status |
|---|---|
| Fabricated claim carrying a citation (2.2) | detection partial — 2 of 3 classes |
| Concurrent-user load test | not run |
| High availability | Neo4j Community is single-instance; needs a decision |

---

## The one-paragraph version

MarkVector answers questions from your documents in **2–6 seconds** by matching,
or **8–18 seconds** by navigating, and reads scans and charts as images in
**20–36 seconds**. It shows the first sign of progress in about **a tenth of a
second**, returns the exact passage behind every claim, and records how each
answer was reached. A 566,000-word book is searchable **8.7 seconds** after
upload. It runs on ordinary servers with no GPU. Its answers are written by a
model, so the citation — not the sentence — is what your service should stand
behind, and the interface should make reading it the easy thing to do.
