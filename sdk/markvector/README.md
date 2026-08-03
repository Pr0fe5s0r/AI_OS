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

## Agent — reasoning + tools, streamed (bring your own LLM)

Configure your own LLM and let the library run an agent over your collection: it
reasons, calls tools to look things up, reads the results, reasons again, and
keeps going until it can answer — streaming the chain of thought and every tool
call as it happens. The loop runs entirely client-side; the tools are ordinary
markvector reads.

```bash
pip install 'markvector[agent]'      # adds the openai client
```

```python
from markvector import Markvector, Thinking, ToolCall, ToolResult, AgentAnswer

mv = Markvector(api_key="kb_live_…")
agent = mv.collection("default").agent(
    api_key="sk-…",                  # your LLM key
    model="gpt-4o-mini",
    # base_url="…",                  # any OpenAI-compatible endpoint (local, OpenRouter, …)
    # client=my_openai_client,       # …or pass a client you already built
)

# Stream the chain of thought + tool execution:
for event in agent.stream("How does Brocaly handle voice input?"):
    if isinstance(event, Thinking):
        print(event.text, end="", flush=True)      # the model's reasoning, live
    elif isinstance(event, ToolCall):
        print(f"\n  → {event.name}({event.arguments})")
    elif isinstance(event, ToolResult):
        print(f"  ← {event.summary}")
    elif isinstance(event, AgentAnswer):
        print("\n\nANSWER:\n" + event.text)

# Or just get the answer, with the transcript of how it got there:
result = agent.answer("How does Brocaly handle voice input?")
print(result.answer)
print(result.tool_calls, "tool calls,", len(result.steps), "steps")
```

The agent is **read-only** — it can `search` (optionally within selected files),
`list_files`, read a document's `structure` (PageIndex), and `read_document`. It
never writes to your store. Configure `model`, `system` (prompt), `max_steps`,
and `temperature` on `.agent(...)`.

## Vectorless search & document structure (PageIndex)

Beyond embedding-based search, MarkVector can answer by reasoning over a
document's **own structure** — its heading tree — and reading only the sections
that matter. That's the default for `answer()`:

```python
ans = docs.answer("what does section 4 require?", mode="vectorless")  # default
# mode="hybrid" uses passage embeddings + keyword instead
```

Get the structure itself — the PageIndex tree a document is indexed as:

```python
doc = docs.files()[0]
tree = docs.structure(doc)                       # -> Structure
print(tree.title, f"{tree.nodes} sections")
for section in tree.sections:                    # top level; .walk() for all
    print(f"  {section.title}  (~{section.tokens} tokens)")
    print(f"    {section.opens}")                # one-line preview

# Bulk — extract the structure of many files in one round trip:
for s in docs.structures(docs.files()):
    print(s.item_id, s.nodes, "sections")
```

## Bulk retrieval

Fetch many documents in one call instead of a request per id (order preserved,
missing ids simply absent):

```python
picked = ["item-abc", "item-def", "item-ghi"]    # or Document objects
for doc in docs.get_many(picked):
    print(doc.id, doc.title)
```

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
