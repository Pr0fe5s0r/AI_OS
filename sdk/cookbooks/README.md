# MarkVector SDK cookbooks

Runnable, copy-paste recipes for the two SDKs, side by side. Every Python recipe
under [`python/`](./python) has a byte-for-byte equivalent under [`js/`](./js),
so you can read one column and know the other.

The Python SDK is [`markvector`](../markvector); the JS SDK is
[`sdk/js`](../js) (published as `markvector`). Both talk to the same API, expose
the same `Markvector → collection → {add, search, answer, agent}` surface, and
differ only in casing (`matched_on` ↔ `matchedOn`) and idiom (context manager ↔
`await`).

## Setup

```bash
# Python
pip install markvector            # add 'markvector[agent]' for recipe 07
export MARKVECTOR_API_KEY=kb_live_…      # console → Developer → API keys
export MARKVECTOR_URL=http://localhost:8000   # optional; this is the default

# JavaScript (Node 18+ for global fetch)
npm install markvector openai     # openai only needed for recipe 07
```

Every script reads `MARKVECTOR_API_KEY` / `MARKVECTOR_URL` from the environment,
so no key is ever hard-coded. Run a recipe with:

```bash
python python/01_quickstart.py
node   js/01_quickstart.mjs
```

## Recipes

| #  | Recipe | What it shows |
| -- | ------ | ------------- |
| 01 | [Quickstart](./python/01_quickstart.py) · [js](./js/01_quickstart.mjs) | create a collection, add text, search, answer |
| 02 | [Ingest a folder](./python/02_ingest_folder.py) · [js](./js/02_ingest_folder.mjs) | bulk-upload PDFs/`.docx`/`.md`, idempotent by locator |
| 03 | [Search filters & traces](./python/03_search_filters.py) · [js](./js/03_search_filters.mjs) | scope to files/sources, `min_score`, read the trace |
| 04 | [Grounded Q&A](./python/04_grounded_qa.py) · [js](./js/04_grounded_qa.mjs) | citations, the `grounded` check, honest fallback |
| 05 | [Versioning](./python/05_versioning.py) · [js](./js/05_versioning.mjs) | update-in-place by locator, list & fetch old versions |
| 06 | [Structure (PageIndex)](./python/06_structure.py) · [js](./js/06_structure.mjs) | the heading tree and the chunks a doc became |
| 07 | [Agent (BYO LLM)](./python/07_agent.py) · [js](./js/07_agent.mjs) | streamed reason→tool→answer loop, file-scoped |
| 08 | [API keys](./python/08_keys.py) · [js](./js/08_keys.mjs) | mint a scoped key, list, revoke |
| 09 | [Categorise](./python/09_categorise.py) · [js](./js/09_categorise.mjs) | pin a document's category, then list by it |
| 10 | [Download originals](./python/10_download_originals.py) · [js](./js/10_download_originals.mjs) | fetch the file back exactly as uploaded |

## Two things worth knowing before you start

- **Indexing is asynchronous.** `add()` / `addFile()` return once the write is
  *accepted*, not once it is *searchable*. Pass `wait=True` / `{ wait: true }`
  when the very next line needs to search what you just wrote — the recipes do.
- **`locator` is the document's stable id at its origin.** Writing the same
  locator again *updates* that document (a new version) instead of adding a
  duplicate, so re-running any ingest recipe is safe.
