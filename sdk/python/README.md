# knowledgebase

Python client for the Knowledge Base. Store documents, search them by meaning
and by exact wording at once, and see why every result came back.

```bash
pip install knowledgebase
```

## Getting started

Create a key in the console under **Keys**, then:

```python
import os
from knowledgebase import KnowledgeBase

kb = KnowledgeBase(api_key=os.environ["KB_API_KEY"])
docs = kb.collection("client-research")

docs.add(
    "Paid conversions fell 18 percent in Q2, driven by a paused campaign.",
    locator="notes/q2-summary",
    title="Q2 summary",
)

for match in docs.search("why did paid results fall"):
    print(f"{match.score:.3f}  {match.title}")
```

The key identifies your workspace, so nothing in this library takes a
workspace id.

## Writing

`locator` is the document's stable id at its origin — a file path, a row id, a
URL. Writing the same locator again **updates that document** rather than
creating a second copy, so re-running an import is safe:

```python
docs.add(text, locator="reports/q2")   # creates
docs.add(text, locator="reports/q2")   # unchanged — costs nothing
docs.add(revised, locator="reports/q2")  # new version; the old one is kept
```

Files are read, converted to Markdown and indexed. The original is never
stored, so keep your copy:

```python
docs.add_file("q2-review.pdf")
```

### Indexing is asynchronous

`add()` returns as soon as the document is accepted, not when it is
searchable. If the next line of your code searches for what you just wrote,
ask to wait:

```python
docs.add(text, locator="notes/1", wait=True)
results = docs.search("...")           # it will be there
```

Without `wait=True` a document typically appears within a second or two.
`wait=True` polls until it is really indexed and raises `IndexingTimeout` if
it never arrives — which is better than a `sleep()` that passes on your
machine and fails in CI.

## Searching

Semantic and keyword search run together and are fused, so a paraphrase and a
product code both find what you need:

```python
results = docs.search("why did advertising results drop", limit=5)

for match in results:
    print(match.score, match.title)
    print(match.matched_on)      # "meaning", "wording", or "meaning+wording"
    print(match.clean_excerpt)   # the passage, without highlight markers
    print(match.source.url)      # where it came from
```

Documents replaced by a newer version are excluded automatically. Pass
`include_superseded=True` for point-in-time reads.

Filters:

```python
docs.search("budget", sources=["gdrive"], period_from="2026-01-01",
            period_to="2026-06-30", min_score=0.4)
```

## Why did I get that result?

Every search carries a trace id, and the trace holds the whole derivation —
each arm's candidates and scores, what survived the filters, and where the
time went:

```python
results = docs.search("why did paid results fall")
trace = kb.trace(results.trace_id)

print(trace["timings_ms"])     # {'embed': 670, 'semantic': 79, 'total': 807}
for candidate in trace["fused"]:
    print(candidate["item_id"], candidate["score"], candidate["kept"])
```

If an arm was unavailable, the result says so rather than quietly returning a
weaker answer:

```python
if results.degraded:
    print("answered without the semantic arm:", results.degraded)
```

## Categories

Documents are filed automatically as they arrive, with a confidence and a
stated basis. You can override that, and your choice sticks — re-ingestion
will not undo it:

```python
doc = docs.list(limit=1)[0]
for category in doc.categories:
    print(category.name, category.confidence, category.pinned)

docs.categorise(doc.id, ["strategy", "client"])
```

## Collections

```python
kb.create_collection("Client research")
for c in kb.collections():
    print(c.collection_id, c.items, c.embedding_model)
```

A collection fixes its embedding model and dimensions when it is created,
because changing either invalidates every vector inside it.

## Errors

| Exception | When |
|---|---|
| `AuthError` | key missing, wrong, revoked, or read-only for a write |
| `NotFound` | no such collection, document or trace |
| `InvalidRequest` | the server rejected the request |
| `Unavailable` | could not reach the service, or it failed after retries |
| `IndexingTimeout` | `wait=True` gave up before the document was searchable |

All inherit from `KnowledgeBaseError`.

## Configuration

| Argument | Environment | Default |
|---|---|---|
| `api_key` | `KB_API_KEY` | — required |
| `base_url` | `KB_URL` | `http://localhost:8000` |
| `timeout` | — | `30.0` |
| `max_retries` | — | `2` (GET only) |

Writes are never retried automatically: a timeout on a write may mean it
landed, and repeating it would be worse than reporting it.
