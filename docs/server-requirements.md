# MarkVector — server requirements

Every figure marked **measured** was taken from this stack running the full
compose file (`api`, `worker`, `web`, `postgres`, `neo4j`, `redis`, `minio`) on
one host. Figures marked **projected** are extrapolated from those measurements
and say so. Nothing here is copied from a vendor's sizing page.

---

## 1. The fact that decides everything else

**Inference is remote. This server needs no GPU.**

Embedding, chat and vision all go out over HTTPS to an OpenAI-compatible
provider (`packages/core/llm.py` is the only caller). MarkVector orchestrates,
stores and searches; it does not run a model. That is why a knowledge base
serving real documents fits on a machine that would struggle to load a 7B model.

The consequence is the opposite of the usual one: **this workload is
latency-bound on an external API, not CPU-bound on your hardware.** Measured,
end to end:

| stage | time | where it goes |
|---|---|---|
| upload accepted (HTTP 202) | 0.17 s | your server |
| normalise + chunk + store | 0.24 s | your server |
| **embed 182 passages** | **5.5 s** | **the provider** |
| answer a text question | 2.5–16 s | mostly the provider |
| answer needing page vision | 20–36 s | mostly the provider |

Adding cores will not move the numbers in the right-hand column. Section 7 says
what to do if you want them to move.

---

## 2. Minimum — evaluation, pilot, single team

Runs the whole compose file on one host. Suitable up to roughly **5,000
documents** and a handful of concurrent users.

| | requirement |
|---|---|
| **CPU** | 4 vCPU (x86-64 or arm64) |
| **RAM** | 8 GB |
| **Disk** | 60 GB SSD |
| **Network** | outbound HTTPS to the model provider; 100 Mbit |
| **OS** | any Linux with Docker Engine 24+ and Compose v2 |

**Do not go below 4 vCPU.** Neo4j alone was measured spiking to **224 % CPU
(2.2 cores)** during background work on an *idle* system — JVM garbage
collection and index maintenance, not query load. On 2 vCPU those spikes starve
the API process, and the symptom is a query that intermittently takes seconds
longer for no visible reason.

RAM is the harder floor. Measured resident memory at idle:

| service | idle RAM (measured) |
|---|---|
| neo4j | **1.14 GB** (heap capped at 1 GB in compose) |
| web (Next.js) | 238 MB |
| api (FastAPI) | 148 MB |
| worker (arq) | 128 MB |
| minio | 92 MB |
| postgres | 74 MB |
| redis | 14 MB |
| **total** | **≈ 1.9 GB idle** |

8 GB leaves headroom for the Postgres page cache, the Neo4j page cache, and
document buffers during ingest. 4 GB will boot and will thrash once the
vector index no longer fits in cache.

---

## 3. Recommended — production, single host

Comfortable to roughly **50,000 documents** and tens of concurrent users.

| | requirement |
|---|---|
| **CPU** | 8 vCPU |
| **RAM** | 16 GB |
| **Disk** | 250 GB SSD (NVMe preferred) |
| **Network** | outbound HTTPS; 1 Gbit |
| **Backups** | nightly `pg_dump` + Neo4j snapshot + object-store replication |

Configuration changes from the defaults at this size:

```bash
# Neo4j: the vector index wants to live in the page cache.
NEO4J_server_memory_heap_max__size=4g
NEO4J_server_memory_pagecache__size=4g

# API: the shipped Dockerfile runs a SINGLE uvicorn process. One process
# serves one CPU. Scale it out to match your cores.
uvicorn apps.api.main:app --workers 4

# Embedding concurrency (see §7). 4 is the measured knee.
EMBED_CONCURRENCY=4
```

The single-uvicorn default is worth calling out because it is easy to miss:
`apps/api/Dockerfile` ends in a bare `uvicorn` invocation with no `--workers`.
On an 8-vCPU box that leaves most of the machine unused under concurrent load.

---

## 4. Maximum — scale-out, when one host stops being enough

Past roughly **100,000 documents** or sustained concurrent ingest, split the
tiers. They scale for different reasons and should not share a machine.

| tier | size | scales with |
|---|---|---|
| **API** | 2+ nodes × 4 vCPU / 8 GB behind a load balancer | concurrent users. Stateless — session lives in Postgres, so any node serves any request. |
| **Worker** | 2+ nodes × 4 vCPU / 8 GB | ingest volume. Add nodes to raise throughput; arq distributes via Redis. |
| **Postgres** | 8–16 vCPU / 32–64 GB / 500 GB+ NVMe | corpus size and full-text search. Add a read replica before adding cores. |
| **Neo4j** | 8–16 vCPU / 32–64 GB / 500 GB+ NVMe | vector index size. **Community edition is single-instance — it cannot cluster.** See the warning below. |
| **Redis** | 2 vCPU / 4 GB | queue depth only. Small and stays small. |
| **Object store** | S3/MinIO, sized by originals | see §5. Not compute-bound. |

> **Neo4j Community does not cluster.** The compose file pins
> `neo4j:5-community`, which is a single writable instance with no read
> replicas and no failover. At the scale where you need HA, that is a licensing
> decision (Neo4j Enterprise) or a migration decision (a dedicated vector store),
> and it needs making deliberately rather than discovering it during an outage.

---

## 5. Storage — measured per-unit, then projected

Measured on the live database:

| store | measured | per chunk |
|---|---|---|
| Postgres `kb_chunks` | 4.35 MB / 1,339 chunks | **3.3 KB** |
| Neo4j database | 19 MB / 1,121 chunks | **17 KB** |

The Neo4j figure is five times the Postgres one because that is where the
1536-dimension vector lives, plus its HNSW index. **Vectors dominate storage
growth.** Postgres holds text and is comparatively cheap.

Projected, at an observed average of ~40 chunks per document:

| corpus | Postgres | Neo4j | originals (S3) | total |
|---|---|---|---|---|
| 1,000 docs | 150 MB | 700 MB | ~500 MB | **≈ 1.4 GB** |
| 10,000 docs | 1.5 GB | 7 GB | ~5 GB | **≈ 14 GB** |
| 100,000 docs | 15 GB | 70 GB | ~50 GB | **≈ 135 GB** |

Three caveats on those projections, because they are the parts most likely to
be wrong for your corpus:

1. **Chunks per document varies enormously.** A 134 KB dense specification
   produced **182 chunks**; a one-page image produces **1**. Forty is the
   average of the test corpus, not a law. Measure your own first hundred
   documents before trusting the middle column.
2. **The originals column is a guess** — it depends entirely on your file mix.
   `S3_MAX_ORIGINAL_MB=25` caps any single file.
3. **Neo4j's data directory was 534 MB while the database was 19 MB.** The
   difference is transaction logs. Provision for the directory, not the
   database, and configure log retention.

Add 2× headroom over the total for WAL, transaction logs, backups and index
rebuilds. Disk should be SSD: vector search is random-read, and it is the one
part of this system where your hardware genuinely decides the latency.

---

## 6. Software and network

| | |
|---|---|
| Docker Engine | 24+ with Compose v2 |
| Postgres | **16** (plain — pgvector is *not* used; vectors live in Neo4j) |
| Neo4j | **5 Community** (see §4 warning) |
| Redis | 7 |
| Object store | S3-compatible (MinIO ships in the compose file) |
| Python / Node | 3.12 / 20 — both baked into the images |

**Outbound HTTPS to the model provider is mandatory.** No egress, no
embeddings, no answers. If outbound is restricted, allow-list the provider
host (`OPENAI_BASE_URL`, `NEBIUS_BASE_URL` or `OPENROUTER_BASE_URL`).

Inbound: only the web and API ports need exposing. Postgres, Neo4j, Redis and
MinIO should stay on the internal network — the compose file remaps their host
ports for local development, which is a development convenience and not a
deployment pattern.

---

## 7. If it is too slow, this is the order to fix it

The measurements say plainly where time goes, so this list is ordered by
evidence rather than by habit.

1. **Raise `EMBED_CONCURRENCY` before raising anything else.** Measured on
   1,024 texts: concurrency 1 took **27.0 s**, concurrency 2 took **5.9 s**,
   and 4/8/16 were flat at ~6 s. Sequential embedding was latency-bound —
   sixteen round trips at ~1.7 s each, spent waiting. Default is 4.
2. **Give the API more than one process.** `--workers N`. Free.
3. **Give Neo4j page cache.** When the vector index stops fitting, search goes
   to disk and every query pays.
4. **Then add cores.** By this point you have exhausted the changes that
   actually helped.

Two things worth knowing before you benchmark your own deployment:

- **Provider latency dominates and varies wildly.** The same question measured
  9.4 s and 29.3 s on identical code twelve minutes apart; one query took
  **509 s**. The same 146 KB image took **32.9 s** and **193.6 s** to ingest.
  A single before/after comparison across separate runs measures your provider,
  not your change. Interleave the arms and take medians.
- **Provider capacity is a real ceiling.** Beyond concurrency 2 the embedding
  curve went flat — that is the provider's throughput, and no amount of local
  hardware moves it. If ingest throughput is the constraint, the lever is a
  higher provider tier or a self-hosted embedding model, and self-hosting is the
  one change in this document that would put a GPU on the requirements list.

---

## 8. What this document does not cover

Stated so the gaps are visible rather than assumed:

- **No load test.** Every figure comes from a single-user working system.
  Concurrent-user throughput is projected, never measured. Before committing to
  the §4 numbers, run a load test — they are reasoned, not proven.
- **No HA or DR design.** Neo4j Community's single-instance limit (§4) means
  high availability needs an architectural decision this document does not make.
- **No cost model.** Provider token spend is likely to exceed server cost at
  scale, and it is not sized here.
