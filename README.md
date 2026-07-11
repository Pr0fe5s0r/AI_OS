# AI OS — a modular AI Operating System

A **generic core engine** plus a **software-company vertical** that calls down into it.

> **Absolute rule:** the vertical calls the core; the core **never** imports, references, or
> branches on any vertical. Enforced by an automated AST import-boundary check that fails CI.

## Architecture

```
packages/shared      Unified Event schema + Understand result types (Pydantic v2). No I/O.
packages/adapter     Provider resolution (openai | nebius | openrouter) -> base_url/api_key
packages/connectors  Connector framework (transport only): GitHub. Extension points for
                     Slack / Linear / Zendesk / Notion.
packages/core        Generic engine. NEVER imports verticals/* or apps/*:
                       llm.py        THE single OpenAI-compatible client (embed + chat)
                       ingest.py     ingest(source_config, raw_payload) -> Event  (declarative normalizer)
                       pipeline.py   Redis/arq jobs: normalize -> store -> embed
                       store.py      append-only, month-partitioned events + tsvector
                       search.py     hybrid pgvector cosine + tsvector full-text
                       resolve.py    resolve(event, entity_rules) -> [ResolvedEntity]
                       graph.py      query_graph(...) via recursive CTEs (no Neo4j)
                       norms.py      learn_norms(metric_config, observations) -> NormBaseline
                       tenancy.py    company_id scoping dependency
                       crypto.py     KMS-style secret sealing (Fernet locally)
                       audit.py      append-only audit log
verticals/software   Domain knowledge as DATA: connector_config (+ field mappings),
                     graph_schema, entity_rules, norm_definitions. Imports core.
apps/api             FastAPI: wires the vertical to the core, exposes HTTP
apps/worker          arq worker running the core pipeline jobs
apps/web             Next.js 14 app-shell: Search / Related graph / Norms
```

### The boundary, enforced

```bash
python scripts/check_core_boundary.py   # exit 1 + file:line on any violation
make boundary                            # same, inside the container
pytest tests/test_import_boundary.py     # same scanner, as a test
```

The scanner parses every file under `packages/core` with `ast` and fails if any import
resolves to `verticals.*` or `apps.*`.

## No mock data

There is **no fixture/seed source**. Events come only from **real connectors**. If no connector
is configured (`GITHUB_REPO` unset), nothing is ingested. Test fixtures live inside `pytest`
only, so CI runs offline.

### GitHub connector — token-aware

| env | meaning |
| --- | --- |
| `GITHUB_REPO` | `owner/name` of any repo to pull issues + PRs from. Required for ingest. |
| `GITHUB_TOKEN` | optional. **Changes behavior** (see below). |
| `GITHUB_LIMIT` | max items to pull (default 30). |

- **without a token:** anonymous public access, one shallow page (`per_page<=30`), PR merge
  time approximated from `closed_at`.
- **with a token:** private repos readable, higher rate limit, deep pagination up to
  `GITHUB_LIMIT`, and each PR enriched with its **real `merged_at`** via the pulls API.

## LLM / embeddings — one client, any provider

Every embedding and chat call goes through `packages/core/llm.py`, which reads `base_url` /
`api_key` from env via the adapter. Works against OpenAI, Nebius, a local server, or any
OpenAI-compatible endpoint.

| `LLM_PROVIDER` | env used |
| --- | --- |
| `openai` | `OPENAI_BASE_URL`, `OPENAI_API_KEY` |
| `nebius` | `NEBIUS_BASE_URL`, `NEBIUS_API_KEY` |
| `openrouter` | `OPENROUTER_BASE_URL`, `OPENROUTER_API_KEY` |

`EMBEDDING_MODEL` (1536 dims) and `CHAT_MODEL` are overridable.

## Quick start (under 5 minutes)

```bash
cp .env.example .env     # set an embedding key + GITHUB_REPO + FERNET_KEY
make up                  # postgres(pgvector) + redis + api + worker + web, then migrations
make ingest              # pull real events -> arq queue -> worker -> Postgres + embeddings
# open http://localhost:3000 and search
```

Then:

```bash
make test        # pytest (boundary, ingestion, search, resolve, graph, norms, health)
make lint        # ruff + mypy
make boundary    # import-boundary guardrail
make down        # tear down, drop the volume
```

> **Windows:** run `make` from Git Bash / WSL, or run the underlying `docker compose`
> commands shown in the [Makefile](Makefile) directly.

## Verify by hand

```bash
# events is a PARTITIONED table with monthly partitions
docker compose exec postgres psql -U aios -d aios -c "SELECT relname, relkind FROM pg_class WHERE relname='events';"
docker compose exec postgres psql -U aios -d aios -c "SELECT tableoid::regclass AS partition, count(*) FROM events GROUP BY 1;"

# ranked search JSON (company-scoped)
curl "http://localhost:8000/api/search?q=bug%20report&company_id=default"

# trigger a real ingest (also writes the audit log)
curl -X POST "http://localhost:8000/api/ingest?company_id=default"

# Understand layer
curl "http://localhost:8000/api/entity/<event_id>/related?company_id=default&hops=2"
curl "http://localhost:8000/api/norms?company_id=default"
```

## Checkpoints

- **CP1 — Foundation + Connect** ✅ boundary guardrail, partitioned append-only store,
  arq ingestion pipeline, resilient embeddings, hybrid search, API + UI.
- **CP2 — Understand** ✅ `resolve()`, knowledge graph (`nodes`/`edges` + recursive CTEs),
  `learn_norms()`, related-graph + norms UI.
- **CP3 — Alert** ⬜ `detect()` (rule executor + structured LLM reasoning), brief assembly,
  routing/delivery, situation feed.
- **CP4 — Act** ⬜ `act()` with human-approval gating, outcome feedback, command center.
