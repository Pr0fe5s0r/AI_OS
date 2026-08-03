# markvector

The Python client for **MarkVector** — a knowledge store you write documents to
and search by meaning and exact wording together. Store text, PDFs, Word and
Markdown files; get grounded answers with citations; read the passages a
document was split into; and download the original file exactly as it was
uploaded.

```bash
pip install markvector
```

## Quick start

```python
from markvector import Markvector

# The key identifies your workspace. Create one in the console under
# Developer → API keys, then pass it here or set MARKVECTOR_API_KEY.
mv = Markvector(api_key="kb_live_…")

docs = mv.collection("client-research")

# Write. Indexing is async; wait=True blocks until it is searchable.
docs.add("Q2 paid conversions fell 18 percent quarter over quarter.",
         locator="notes/q2", wait=True)

# Search — by meaning and by wording, fused.
for hit in docs.search("why did paid results fall"):
    print(f"{hit.score:.3f}  {hit.title}  ({hit.matched_on})")
    print("   ", hit.clean_excerpt)

# Ask — a written answer built only from what was retrieved.
answer = docs.answer("summarise Q2 paid performance")
if answer.grounded:
    print(answer.text)
    for c in answer.citations:
        print(f"  [{c.marker}] {c.title} — {c.heading}")
else:
    print("Not enough in the store to answer that.")
```

`Markvector` is a context manager, so you can also write:

```python
with Markvector() as mv:            # reads MARKVECTOR_API_KEY / MARKVECTOR_URL
    ...
```

## Configuration

| argument   | env var               | default                 |
| ---------- | --------------------- | ----------------------- |
| `api_key`  | `MARKVECTOR_API_KEY`  | — (required)            |
| `base_url` | `MARKVECTOR_URL`      | `http://localhost:8000` |

```python
mv = Markvector()  # both read from the environment
```

## Uploading files

```python
docs.add_file("reports/q2.pdf", wait=True)      # PDF, .docx, .md, .txt

doc = docs.list(limit=1)[0]

# See what actually got indexed: the passages the document became.
for chunk in docs.chunks(doc.id):
    print(f"#{chunk.ordinal + 1}  {chunk.heading}\n{chunk.text[:120]}…")

# Get the original file back exactly as uploaded.
docs.download_original(doc.id, path="q2-copy.pdf")   # -> writes the file
raw_bytes = docs.download_original(doc.id)           # -> bytes
```

`download_original` raises `NotFound` for documents with no stored original
(text written via `add`, or files uploaded before originals were kept).

## Your files, and searching within them

```python
# Everything in the collection (including text added via add()):
for doc in docs.list():
    print(doc.id, doc.title, doc.source.locator)

# Just the uploaded files (those with a downloadable original), newest first:
for f in docs.files():
    print(f.id, f.original.filename, f"{f.original.size} bytes")
```

Restrict a search to specific documents with `files=` — pass their ids, or the
`Document` objects straight from `list()` / `files()`:

```python
picked = docs.files()[:3]                      # or ["item-abc", "item-def"]
for hit in docs.search("refund policy", files=picked):
    print(hit.title, hit.score)                # only ever from the picked files
```

Omit `files=` to search the whole collection. A scoped search never returns a
document outside the set.

## Collections

```python
mv.collections()                                   # list every collection
c = mv.create_collection("Client research")        # -> CollectionInfo
mv.rename_collection(c.collection_id, "ACME research")
mv.delete_collection(c.collection_id)              # -> number of docs removed
```

## API keys

```python
minted = mv.create_key("ci-pipeline", scopes="read")          # workspace-wide
scoped = mv.create_key("acme-bot", collection_id="acme")      # locked to one collection
print(minted.key)   # the ONLY time the secret exists — store it now

mv.keys()                       # existing keys (prefixes + usage, never secrets)
mv.revoke_key(minted.key_id)
```

A key created with `collection_id=` is confined to that collection server-side —
every call it makes is forced into that collection whatever it asks for.

## Why a result came back

Every search carries a `trace_id`. Hand it to `mv.trace(...)` for the full
derivation — each arm's candidates, scores and where the time went.

```python
results = docs.search("refund policy")
print(mv.trace(results.trace_id))
```

## Errors

Everything raised inherits from `MarkvectorError`:

| exception         | when                                                   |
| ----------------- | ------------------------------------------------------ |
| `AuthError`       | key missing, wrong, revoked, or lacks the scope        |
| `NotFound`        | no such collection, document or trace                  |
| `InvalidRequest`  | the server rejected the request (message is the server's) |
| `Unavailable`     | the service could not be reached, or failed after retries |
| `IndexingTimeout` | `wait=True` gave up before the document was searchable |

```python
from markvector import MarkvectorError

try:
    docs.search("…")
except MarkvectorError as e:
    print("markvector error:", e)
```

GET requests are retried on transient failures (429/5xx) with backoff; writes
are not retried, because a timed-out write may already have landed.
