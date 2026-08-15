# MarkVector — manual API testing

Everything below runs against **your local stack** with **your real documents**.
Every id, filename and expected result was taken from the running database, so
if a command returns something different, that is a finding rather than a typo.

---

## Contents

1. [Before you start](#1-before-you-start)
2. [Get a key](#2-get-a-key) — you currently have **none**
3. [Swagger](#3-swagger)
4. [Your corpus](#4-your-corpus) — ids to paste
5. [Metadata filtering](#5-metadata-filtering)
6. [The manage scope](#6-the-manage-scope)
7. [Figures and page pictures](#7-figures-and-page-pictures)
8. [Answers, streaming and traces](#8-answers-streaming-and-traces)
9. [Things that should fail](#9-things-that-should-fail)
10. [Cleaning up](#10-cleaning-up)

---

## 1 · Before you start

| what | where |
|---|---|
| API | `http://localhost:8000` |
| Swagger | `http://localhost:8000/docs` |
| Console | `http://localhost:3005` — **not 3000** |
| Workspace | `acme-7a3d9c` (`karthisk846@gmail.com`) |
| Collection | `default`, 8 documents |

> **Port 3000 is not your stack.** A host `node` process serves it and proxies to
> a remote deployment. Everything here is 8000 (API) and 3005 (console).

Check the API is up:

```bash
curl -s http://localhost:8000/api/health
```

---

## 2 · Get a key

There are **zero live keys** right now — I revoked every test key I made. Mint
two: one that reads and writes, one that only administers.

```bash
docker compose exec -T api python -c "
import asyncio, json
from packages.core.db import Session
from packages.core.keys import create_key
async def main():
    async with Session() as s:
        out = {}
        for name, scopes in (('manual-rw','read,write'), ('manual-operator','manage')):
            out[name] = (await create_key(s, 'acme-7a3d9c', name, scopes=scopes))['key']
        await s.commit(); print(json.dumps(out, indent=1))
asyncio.run(main())"
```

The key is shown **once**. Keep both in your shell:

```bash
export RW=kb_live_paste_the_read_write_one
export OP=kb_live_paste_the_manage_one
```

You can also mint keys from the console (**API keys** page) once signed in at
`http://localhost:3005`.

---

## 3 · Swagger

Open `http://localhost:8000/docs`.

1. Click **Authorize** (top right).
2. Paste a key **without** the word `Bearer` — Swagger adds it.
3. **Apply credentials**, then **Close**. Padlocks close across the page.
4. Open an operation → **Try it out** → fill fields → **Execute**.

Two quirks worth knowing:

- Array parameters (`meta`, `doc`, `sources`) need **Add string item** first, and
  the new row is pre-filled with the literal word `string`. Select it and type
  over it, or you will send `meta=string`.
- The authorization now persists across reloads. If a call suddenly 401s, check
  the padlocks are still closed.

---

## 4 · Your corpus

| document | item_id | format | pages | derived type |
|---|---|---|---|---|
| Bitcoin: A Peer-to-Peer… | `439da45c8cd482d95544c8337ff13862` | pdf | 9 | technical whitepaper |
| Casino Conversational AI | `688c22cb904d30e7e3a3c31a25614499` | pdf | 12 | technical design specification |
| COMPUTER NETWORKS | `24e307cddd2f250f0320316d5965291e` | pdf | 962 | textbook |
| Harry Potter | `999055f1e8b05178040649ff5924b010` | pdf | 3623 | — |
| International Journal | `1e34ee55024fb2f4e79b48dabbc02324` | pdf | 4 | historical and cultural overview |
| MarkOS | `3c320e2aec5292447039b0dc4fbd316a` | **docx** | — | product description |
| MarkVector — Server Requirements | `cdde126fad81da11896e4713994e21eb` | pdf | 2 | deployment guide |
| Picture1 | `7becf9e3e27834618b89241886f1e7a3` | **png** | 1 | system readiness assessment |

```bash
curl -s -H "Authorization: Bearer $RW" -H "X-Collection: default" \
  "http://localhost:8000/api/items?limit=20" | jq '.items[] | {title, id}'
```

---

## 5 · Metadata filtering

`?meta=key:value`, repeatable. **Different keys must all match; the same key
repeated matches any of its values.**

### It works with no tagging at all

Ingest records `format`, `pages`, and the original's filename and size, so this
works on documents nobody has touched:

```bash
curl -s -H "Authorization: Bearer $RW" \
  "http://localhost:8000/api/search?q=bitcoin&meta=format:pdf" | jq '.count, [.results[].title]'
```

**Expect 6** — every PDF. The DOCX (MarkOS) and the PNG (Picture1) must be absent.

```bash
curl -s -H "Authorization: Bearer $RW" \
  "http://localhost:8000/api/search?q=platform&meta=format:docx" | jq '.count, [.results[].title]'
```

**Expect 1** — MarkOS only.

### OR within a key, AND across keys

```bash
curl -s -H "Authorization: Bearer $RW" \
  "http://localhost:8000/api/search?q=diagram&meta=format:pdf&meta=format:png" | jq '.count'
```

**Expect 7** — PDFs *or* PNG, i.e. everything except the DOCX.

### The two kinds of nothing

This is the part worth testing carefully, because both return zero results and
they mean opposite things.

```bash
# The key exists; no document has that value.
curl -s -H "Authorization: Bearer $RW" \
  "http://localhost:8000/api/search?q=anything&meta=format:xlsx" | jq '{count, degraded}'
```
**Expect** `count: 0` and a `degraded` saying no document carries those *values*.

```bash
# The key exists NOWHERE — a typo, or an untagged corpus.
curl -s -H "Authorization: Bearer $RW" \
  "http://localhost:8000/api/search?q=anything&meta=frmat:pdf" | jq '{count, degraded}'
```
**Expect** `degraded` to **name the key**: *"no document in scope carries the
metadata key 'frmat' — check the spelling, or the documents may have been
ingested without it"*.

> Why it matters: a **partially tagged** corpus otherwise gives a confident
> answer from half your evidence with nothing on screen saying so.

### Tagging your own metadata

```bash
curl -s -X POST -H "Authorization: Bearer $RW" -H "Content-Type: application/json" \
  -H "X-Collection: default" \
  -d '{"source":"upload","locator":"note-1","body":"Quarterly revenue was up 12%.",
       "title":"Q3 note","metadata":{"client":"acme","kind":"note"}}' \
  http://localhost:8000/api/items
```

Wait a few seconds for indexing, then:

```bash
curl -s -H "Authorization: Bearer $RW" -H "X-Collection: default" \
  "http://localhost:8000/api/search?q=revenue&meta=client:acme" | jq '.count, [.results[].title]'
```

### On the answer path too

```bash
curl -s -H "Authorization: Bearer $RW" \
  "http://localhost:8000/api/answer?q=what+problem+does+it+solve&mode=hybrid&meta=format:docx" \
  | jq '{answer, grounded, citations: [.citations[].title]}'
```

---

## 6 · The manage scope

Three scopes: **read** sees contents, **write** ingests them, **manage**
administers collections and keys and reads nothing.

### An operator cannot read

Every one of these must return **403 "This credential needs the read scope."**

```bash
for path in \
  "search?q=bitcoin" \
  "answer?q=what+is+bitcoin" \
  "items" \
  "traces" \
  "items/439da45c8cd482d95544c8337ff13862/original" \
  "items/439da45c8cd482d95544c8337ff13862/pages/2" ; do
  printf '%-52s %s\n' "$path" \
    "$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $OP" \
       "http://localhost:8000/api/$path")"
done
```

### …but can administer

```bash
curl -s -X POST -H "Authorization: Bearer $OP" -H "Content-Type: application/json" \
  -d '{"name":"manual probe"}' http://localhost:8000/api/collections | jq

curl -s -H "Authorization: Bearer $OP" http://localhost:8000/api/keys | jq '.keys[].name'
```

**Expect 201** and a key list. The operator just created a collection it cannot
read a single document in.

### The escalation attempt

```bash
curl -s -X POST -H "Authorization: Bearer $OP" -H "Content-Type: application/json" \
  -d '{"name":"escalate","scopes":"read"}' http://localhost:8000/api/keys | jq
```

**Expect 403** — *"A key cannot grant scopes it does not hold: read"*. A key may
only mint keys no stronger than itself; without that rule the whole separation
is one API call deep.

```bash
# Minting an equal key is allowed.
curl -s -X POST -H "Authorization: Bearer $OP" -H "Content-Type: application/json" \
  -d '{"name":"peer-operator","scopes":"manage"}' http://localhost:8000/api/keys | jq '.key_id'
```

### And a reader cannot administer

```bash
curl -s -o /dev/null -w 'search %{http_code}\n'  -H "Authorization: Bearer $RW" "http://localhost:8000/api/search?q=x"
curl -s -o /dev/null -w 'keys   %{http_code}\n'  -H "Authorization: Bearer $RW" "http://localhost:8000/api/keys"
```

**Expect** 200 then **403** — `manual-rw` holds read and write, not manage.

---

## 7 · Figures and page pictures

Figures are detected from the PDF's own object tree — no vision call — and
cached. Boxes are percentages of the page, origin top-left.

```bash
# Bitcoin page 2: the transaction chain and the block chain.
curl -s -H "Authorization: Bearer $RW" \
  "http://localhost:8000/api/items/439da45c8cd482d95544c8337ff13862/pages/2/figures" | jq

# Casino page 6: the architecture diagram.
curl -s -H "Authorization: Bearer $RW" \
  "http://localhost:8000/api/items/688c22cb904d30e7e3a3c31a25614499/pages/6/figures" | jq

# A page of prose: expect an empty list, which is the common and correct answer.
curl -s -H "Authorization: Bearer $RW" \
  "http://localhost:8000/api/items/439da45c8cd482d95544c8337ff13862/pages/1/figures" | jq
```

Save a page picture to check a crop against it:

```bash
curl -s -H "Authorization: Bearer $RW" \
  "http://localhost:8000/api/items/688c22cb904d30e7e3a3c31a25614499/pages/6" -o casino-p6.png
```

Ask a question about a diagram and see the figure travel with the citation:

```bash
curl -s -H "Authorization: Bearer $RW" -H "X-Collection: default" \
  "http://localhost:8000/api/answer?q=show+me+the+casino+architecture+diagram" \
  | jq '{grounded, answer: .answer[0:220], pages: [.citations[] | {title, page}]}'
```

Then fetch `/figures` for the page it cited.

---

## 8 · Answers, streaming and traces

```bash
# Agentic (default): navigates, may look at a page. Slower, better on diagrams.
time curl -s -H "Authorization: Bearer $RW" -H "X-Collection: default" \
  "http://localhost:8000/api/answer?q=how+does+dijkstra+shortest+path+work" \
  | jq '{mode, grounded, took_ms, steps: (.steps|length), answer: .answer[0:200]}'

# Hybrid: one search, one write. 3-5x faster.
time curl -s -H "Authorization: Bearer $RW" -H "X-Collection: default" \
  "http://localhost:8000/api/answer?q=how+does+dijkstra+shortest+path+work&mode=hybrid" \
  | jq '{mode, grounded, took_ms}'
```

Streaming — reasoning and answer arrive token by token:

```bash
curl -N -H "Authorization: Bearer $RW" -H "X-Collection: default" \
  "http://localhost:8000/api/answer/stream?q=what+is+the+OSI+model" | head -40
```

Toggles worth trying: `&vision=false` (no page reading), `&open_document=false`,
`&doc=<item_id>` to confine the answer, `&behaviour=Answer+in+one+sentence.`

Traces — `steps` and `trace_id` come back **inline**; the candidate detail is a
second call:

```bash
TRACE=$(curl -s -H "Authorization: Bearer $RW" \
  "http://localhost:8000/api/search?q=bitcoin" | jq -r .trace_id)
curl -s -H "Authorization: Bearer $RW" "http://localhost:8000/api/traces/$TRACE" \
  | jq '{query, filters, timings_ms}'
```

The trace's `filters` shows the metadata filter that was applied — including
`unknown_keys` when a filter matched nothing.

---

## 9 · Things that should fail

Worth running: a boundary you have not seen fail is one you have not tested.

```bash
# No credential at all.
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8000/api/search?q=x        # 401

# A revoked key.
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer kb_live_nonsense" \
  http://localhost:8000/api/search?q=x                                              # 401

# A collection that is not yours.
curl -s -H "Authorization: Bearer $RW" -H "X-Collection: someone-elses" \
  http://localhost:8000/api/search?q=x | jq                                         # 404

# An unreadable format.
curl -s -X POST -H "Authorization: Bearer $RW" -F "file=@/etc/hostname;filename=x.exe" \
  http://localhost:8000/api/items/file | jq                                         # 415
```

---

## 10 · Cleaning up

```bash
docker compose exec -T api python -c "
import asyncio
from sqlalchemy import text
from packages.core.db import Session
async def main():
    async with Session() as s:
        await s.execute(text(\"UPDATE api_keys SET revoked_at=now() WHERE name LIKE 'manual-%' OR name='peer-operator'\"))
        await s.execute(text(\"DELETE FROM collections WHERE workspace_id='acme-7a3d9c' AND collection_id='manual-probe'\"))
        await s.commit(); print('cleaned')
asyncio.run(main())"
```

If you ingested the `note-1` test document in §5, note there is **no
delete-a-document route** — that gap is on the list. It lives in the `default`
collection under the title *Q3 note*.
