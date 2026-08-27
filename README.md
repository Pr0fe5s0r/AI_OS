# MarkOS — a generic AI work agent

One engine, ANY industry. The engine (ingest → resolve → search → norms →
detect → act) is a plain Python library with **zero domain knowledge**; what a
company's work looks like lives in a **profile** — one row of pure data per
company, seven JSONB slots: `sources, things, links, rhythms, watchers, moves,
vocabulary`. Boot it with `profiles/software.yaml` and it's an engineering
ops product; boot it with `profiles/inventory.yaml` and the same code is an
inventory product.

> **Enforced rules (all fail CI):**
> - `packages/core` imports nothing from `apps/*` (AST scan:
>   `scripts/check_core_boundary.py` + `tests/test_import_boundary.py`)
> - no `industry ==` / hardcoded company branching anywhere in the engine or
>   services (`tests/test_tripwire.py`)
> - ALL Cypher lives in `packages/core/graph.py` — nowhere else
>   (`tests/test_cypher_location.py`), same rule as the single LLM client

## Architecture — three layers, strict boundaries

```
packages/core        The engine. Generic functions taking profile slots as ARGUMENTS:
                       llm.py        THE single OpenAI-compatible client (embed + chat +
                                     chat_with_tools)
                       assistant.py  the generic tool-use loop: drive LLM <-> tools until a
                                     plain-text answer; tools + dispatcher are ARGUMENTS
                       oauth.py      generic OAuth2 authorization-code flow; the provider
                                     (urls/scope/client) arrives as data from the connector
                       discovery.py  propose a profile from a connector's real payloads:
                                     deterministic shape analysis + LLM naming, validated
                                     by actually normalizing the samples
                       profile.py    seven-slot Profile model + loader + YAML seeds +
                                     versioned enable/disable of a source
                       graph.py      THE Cypher module (Neo4j): Things, Event mirrors,
                                     typed links, vector index, traversal — all tenancy-scoped
                       ingest.py     declarative normalizer (profile mapping -> Event)
                       pipeline.py   arq jobs: validate raw payload -> normalize -> store (PG)
                                     -> embed (Neo4j) -> resolve
                       store.py      append-only, month-partitioned events + tsvector (PG)
                       search.py     hybrid: Neo4j vector similarity + PG full-text, merged
                       resolve.py    profile entity_rules/links -> Things + SAME_AS/MENTIONS/
                                     <profile "closes" type> links
                       norms.py      rolling-window baselines per profile rhythm — trend-aware
                                     (regression, not flat mean) + IQR outlier-trimmed +
                                     maturity flag (insufficient|learning|stable); business
                                     AND system-scoped (e.g. connector ingest volume)
                       detect.py     the watcher engine: universal built-in primitives +
                                     profile Tier-1 rules, one highest-severity-wins dedupe;
                                     LIVE events only (backfilled=false)
                       watchers.py   the 5 universal built-ins (stalled_thing, aging_commitment,
                                     orphaned_hotspot, broken_rhythm, volume_anomaly) — pure
                                     graph/timing/norm primitives, no profile needed
                       connector_health.py  schema-failure streaks, field-completeness EMA,
                                     rolled up into healthy|degraded|broken per connector
                       situations.py  situation lifecycle: raise/resolve_stale/snooze/ack/
                                     dismiss, scoped by kind (business | system | clarification)
                       conversations.py  minimal conversations/messages/artifacts backbone
                       erasure.py    delete_company(): Neo4j -> Redis -> Postgres fan-out,
                                     tracked in deletion_requests
                       act.py        approval-gated action runtime, dry_run safety
                       agent.py      structured decisions: choose move / choose assignee
packages/connectors  Transport only (GitHub real; Slack/Zendesk stubs). Each declares a
                     Pydantic schema for its RAW payload — validated before normalizing —
                     and a backfill(since_days) that walks history back in time.
apps/common          Thin orchestration shared by api + worker: load profile row, call core.
                       health.py     15-min cron: evaluate connector_health, raise/retire a
                                     kind="system" situation through the SAME situation
                                     lifecycle business detection uses
                       clarifications.py  norm-drift / connector-degraded clarification
                                     cards, posted into both the Feed and the agent chat
                       assistant_tools.py  the agent's toolbox: read tools (search, situations,
                                     briefing, norms) + run_action (through the approval brake)
                       discovery_flow.py  onboarding: pull real payloads off a connection,
                                     induce a PROPOSED profile, confirm it into a live one
                       watching.py   5-min watcher-engine cron (every company); its own
                                     cadence, faster than the LLM-heavier business scan
                       feed_stream.py  publish "feed changed" over Redis pub/sub -> SSE
                       deletion.py   worker-job entrypoint for erasure.delete_company()
apps/api             FastAPI routes. Zero business logic; profile loaded per request.
                       GET /feed + GET /feed/stream (SSE) power the live Feed page.
apps/worker          arq worker + scan cron (15 min) + health cron (15 min) + watcher
                     cron (5 min); backfill runs as a low-priority job.
apps/web             Next.js UI (dark theme — tokens in tailwind.config.ts). The Feed page
                     reads /feed and refetches on each /feed/stream SSE nudge.
profiles/            THE domain knowledge: software.yaml + inventory.yaml seed files.
```

## Storage split — two stores, clear ownership

- **Postgres 16 (plain, NO pgvector)** — system of record: `companies`,
  `profiles` (versioned, proposed|confirmed), `events` (append-only,
  month-partitioned, `content_tsv`), `norm_baselines`, `norm_resets`,
  `situations` (business | system | clarification), `conversations` /
  `messages`, `actions`, `connector_health`, `credentials` (Fernet-sealed),
  `settings`, `tickets`, `action_tokens`, `audit_log`, `deletion_requests`.
- **Neo4j 5 (community)** — the living graph + vectors:
  `(:Thing {id, company_id, thing_type, title, status, last_activity})`,
  `(:Event {id, company_id, event_time, source, embedding})` (lightweight
  mirror — content stays in Postgres, joined by id),
  `(:Event)-[:ABOUT]->(:Thing)`, and typed thing→thing links whose names come
  from the profile (`CLOSES`, `FULFILLS`, `SAME_AS {confidence}`, …).
  Constraints, indexes and the cosine vector index (1536 dims) are created
  idempotently by `core.graph.bootstrap()` on every boot.
  **Every Cypher query filters on `company_id`.**

Migrating an old v1 database (Postgres `nodes`/`edges` + pgvector)?
`python scripts/migrate_pg_graph_to_neo4j.py` moves everything into Neo4j and
drops the legacy tables. Fresh installs never create them.

## LLM / embeddings — one client, any provider

Everything goes through `packages/core/llm.py` (`LLM_PROVIDER` = openai |
nebius | openrouter; `EMBEDDING_MODEL` must output 1536 dims, `CHAT_MODEL` for
watcher reasoning).

See [How MarkVector works](docs/markvector-flow.md) for the complete ingestion,
retrieval, grounded-answer, citation, trace, file-scope, and SDK-agent flow.

## Quick start

```bash
cp .env.example .env     # embedding key + FERNET_KEY, then set GITHUB_REPO to
                          # a real repo of yours (leave it empty and nothing ingests)
make up                  # postgres + neo4j + redis + api + worker + web + migrations
make seed                # profile rows from profiles/*.yaml — no mock events, ever
make ingest              # pull real GitHub events for the software company
# open http://localhost:3005
```

> **No mock/demo/fixture data, anywhere.** `make seed` only creates the
> profile definitions (what a company's work looks like); it has never
> generated a fake event. Every event in Postgres came from a real connector
> pull or a real `POST /api/ingest/push`. An unconnected company shows the
> empty "connect a tool" state — that is the correct resting state, not a
> bug to paper over. Test fixtures live only inside `pytest` and use their
> own throwaway `company_id`s, isolated from `default`/`acme-inventory`.

Then: `make test` (153 tests: guardrails, ingest, resolve, graph, search,
norms, detect, watchers, act, autonomy, agent tool-loop, discovery golden
test, OAuth + state CSRF, connector health, clarifications, deletion) ·
`make boundary` · `make lint` · `make down`.

> **Windows:** run `make` from Git Bash/WSL or use the underlying
> `docker compose` commands. Ports: Postgres `5442`, Redis `6389`, Neo4j
> `7475`/`7688` (defaults left free for other local stacks).

## The two-profile proof (CP1)

Same engine, two companies, two industries. `profiles/inventory.yaml` seeds
the `acme-inventory` company's profile with zero events — push a few real
(or hand-written, but real-shaped) purchase-order/delivery/stock-count
payloads to prove it:

```bash
# software company (company_id=default, profile from profiles/software.yaml)
curl "http://tnega-api-o9ecgm-5fbf26-217-154-175-169.traefik.me/api/search?q=payment%20bug&company_id=default"

# inventory company: push one real-shaped event, then search for it
curl -X POST "http://tnega-api-o9ecgm-5fbf26-217-154-175-169.traefik.me/api/ingest/push?company_id=acme-inventory" \
  -H "Content-Type: application/json" \
  -d '{"source":"ops","events":[{"ref":"PO-1","kind":"purchase_order","status":"open","occurred_at":"2026-07-01T00:00:00Z","title":"Restock widgets","supplier":"YourSupplier","sku":"SKU-1","quantity":100,"actor":{"id":"you","name":"You"}}]}'
curl "http://tnega-api-o9ecgm-5fbf26-217-154-175-169.traefik.me/api/search?q=widgets&company_id=acme-inventory"

# graph landed in Neo4j (Things typed by each profile, links typed by each profile)
docker compose exec neo4j cypher-shell -u neo4j -p markos-graph \
  "MATCH (n) RETURN labels(n)[0], n.company_id, count(n)"

# 2-hop cluster around that purchase order — FULFILLS comes from profile data
curl "http://tnega-api-o9ecgm-5fbf26-217-154-175-169.traefik.me/api/entity/ops-PO-1/related?company_id=acme-inventory&hops=2"
```

Editing a profile is a product change without a deploy: bump the YAML (or the
row), reseed, and the engine behaves differently — e.g. `id_patterns` decide
what counts as the same piece of work.

## Backfill, norms, and the watcher engine (CP2)

**Backfill (part A).** `POST /api/connections/{source}/backfill?since_days=90`
walks a connector's real history back in time (the GitHub connector pages
`sort=created&direction=desc` until it crosses the cutoff, pacing itself
politely between pages) and tags every event `backfilled=true`. It runs as a
low-priority worker job so it never contends with a live sync for a source's
rate-limit budget. History exists to give norms real depth — **the watcher
engine below evaluates `backfilled=false` events only**, so walking in months
of old issues enriches baselines without retroactively firing alerts on work
that's long finished.

```bash
curl -X POST "http://tnega-api-o9ecgm-5fbf26-217-154-175-169.traefik.me/api/connections/github/backfill?company_id=default&since_days=90"
```

**Norms (part B).** `core.learn_norms()` is trend-aware and outlier-trimmed,
not a flat average: `median`/`std` are computed on the IQR-trimmed set (one
freak 30-hour outlier can't drag a threshold around), and `mean` is a
least-squares regression line evaluated at the most-recent point — "what's
typical *right now*, given the trend" rather than a whole-window blur. Every
baseline carries a `maturity` flag (`insufficient` < 5 samples, `learning` <
20, `stable`) gated on the raw sample count, so the UI can be honest about how
much to trust a number.

```bash
curl "http://tnega-api-o9ecgm-5fbf26-217-154-175-169.traefik.me/api/norms?company_id=default"
# -> issue_resolution_hours  n=5  maturity=learning  median=1.76  trend_per_period=…
```

**Watcher engine (part C).** `core.detect.run_watcher_engine()` evaluates
watchers from **two origins** and collapses both through one
highest-severity-wins dedupe (so an issue that's both an orphaned hotspot AND
past its SLA shows as a single card):

- **Universal built-ins** (`core.watchers`) — `stalled_thing`,
  `aging_commitment`, `orphaned_hotspot`, `broken_rhythm`, `volume_anomaly`.
  Pure graph-topology / elapsed-time / norm-threshold primitives that need
  **no watcher declared anywhere** — they hold for any company in any
  industry.
- **Profile Tier-1 watchers** (`profile.watchers`) — structured `select` +
  `where` + optional `norm` gate + optional graph condition, each with its
  own LLM severity/summary prompt.

It runs on its **own 5-minute cron** (`apps.common.watching`), faster than the
LLM-heavier 15-minute business scan because the built-ins are pure algorithms.
Situations are raised through the same lifecycle as always; a human can
`POST /api/situations/{id}/ack` (I've seen it, I'm on it) or
`/dismiss` (doesn't need action — reopens if the condition is still true next
pass).

**Two different questions, two views.** `GET /api/feed` answers *"what needs
me?"* (the watcher engine's opinion). `GET /api/items` answers the more basic
*"what have I got?"* — the records themselves, with facet counts for
**source / type / status**. Which metadata key holds a status is profile data
(`things.status_field`), so the same code renders `open/closed` for a GitHub
profile and `open/fulfilled` for an inventory one, and the tab is labelled
from `vocabulary.terms.thing` ("All issues" vs "All orders"). Facets follow
proper faceting rules — picking *open* narrows the list but leaves the status
counts intact, so you can still see what switching would give you. Unlike the
watchers, this view **includes backfilled history**: old records are part of
what you have, even though they must never raise fresh alerts.

**Live feed (part C/D).** `GET /api/feed` returns situations + actions in one
round trip; `GET /api/feed/stream` is Server-Sent Events over Redis pub/sub.
Every watcher-engine pass (webhook, business scan, or the 5-min cron)
publishes one "the feed changed" nudge — carrying no situation data itself, so
the stream can never drift out of sync with Postgres. The CP3 Feed page
subscribes and refetches on each nudge; a new card lands without a refresh.

```bash
curl "http://tnega-api-o9ecgm-5fbf26-217-154-175-169.traefik.me/api/feed?company_id=default"
curl -N "http://tnega-api-o9ecgm-5fbf26-217-154-175-169.traefik.me/api/feed/stream?company_id=default"   # SSE
```

## The agent that can act (CP4)

`POST /api/agent/chat` runs a real tool-use loop, not keyword matching. The
model is given the company's open situations + available moves and a toolbox,
and it decides which tools to call:

- **Read tools** — `search_events`, `list_situations`, `get_briefing`,
  `get_norms`. Every answer is grounded in the company's real data; the model
  is told never to invent events, numbers, or situation ids.
- **One write tool** — `run_action`, whose `action` enum is exactly the
  profile's move registry. It rides the **same approval brake** as the Feed
  buttons, but with a deliberate difference from a button click: a typed
  request ("escalate this") is a high-level intent, not a precise approval, so
  the agent path is **not** pre-approved — any move the profile marks
  `approval_required` (e.g. `page_engineer`, `comment_on_pr`) still queues as
  `pending_approval` for a human, and Practice mode still rehearses external
  writes as `dry_run`. The model can decide to *do* things; it can never
  bypass the human gate or touch an external system directly.

The loop lives in `core.assistant.run_tool_loop` — pure mechanics (LLM ↔
tools until a plain-text answer, hard-capped at 5 tool round-trips), with the
tool specs and dispatcher passed in as arguments, exactly like `detect()`
takes its rules. The domain-aware toolbox is `apps/common/assistant_tools`.
Both turns persist through the same conversations/messages backbone the rest
of the chat uses, so the thread survives a refresh; and because `run_action`
reuses an existing pending approval instead of duplicating (the CP2 idempotency
guard), asking the agent to escalate something already queued is a no-op, not a
second card.

```bash
curl -X POST "http://tnega-api-o9ecgm-5fbf26-217-154-175-169.traefik.me/api/agent/chat?company_id=default" \
  -H "Content-Type: application/json" \
  -d '{"message":"which issue is most overdue, and label it a bug?"}'
```

Token streaming of the final answer (SSE) is a deliberate follow-up — the
tool-use loop and the act-through-chat capability are the substance of CP4 and
are proven end-to-end; streaming is UX polish layered on top.

## Connecting GitHub: "sign in" instead of a pasted token

Register an OAuth app once (github.com → Settings → Developer settings →
**OAuth Apps** → New OAuth App):

| Field | Value |
| --- | --- |
| Homepage URL | `http://localhost:3005` |
| Authorization callback URL | `http://tnega-api-o9ecgm-5fbf26-217-154-175-169.traefik.me/api/oauth/github/callback` |

Put the Client ID + a generated Client Secret in `.env`
(`GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET`) and recreate the api container.
The Connections tab then offers **Sign in with GitHub**; until it's set, the
card falls back to the token field and says why. A pasted Personal Access
Token keeps working either way — OAuth is an alternative, not a replacement.

**Scope is `public_repo` by default** — least privilege: enough to read
issues/PRs and write labels, assignees and comments on public repos, which is
everything the shipped moves do. `GITHUB_OAUTH_SCOPE=repo` is an explicit
opt-in for private repos, and worth understanding before you set it: `repo`
grants read/write to *every* repository the signing-in user can see.

How the flow is kept honest:

- **`state` is a real CSRF defence, not decoration.** It reuses the same
  sealed / expiring / **single-use** token machinery the one-click email links
  use (`core.tokens`), with a 10-minute life. It also carries the
  `company_id`, so the callback learns whose grant this is from a value the
  server minted — never from a query parameter a stranger could set. A
  replayed, forged, expired, or wrong-purpose state is refused.
- **The client secret never leaves the server.** It appears only in the
  back-channel POST that trades the code for a token — never in a redirect, a
  log, or an error message.
- **The redirect target is fixed** (`WEB_BASE_URL`), never echoed from a query
  param — that is exactly how an OAuth callback becomes an open redirect.
- **The token lands in the same sealed store** a pasted one does
  (Fernet, `credentials`), so nothing downstream knows the difference. It is
  never written to the audit log — only the fact a grant happened.
- **OAuth2 reports failure inside a 200 body**, so the exchange checks the
  payload's `error` field rather than trusting the status code.

After signing in, the account is connected but nothing is chosen yet: the card
lists the repos that token can actually see, so you pick one instead of typing
`owner/name` and hoping. (`GET /api/oauth/github/targets`.)

## Onboarding without a blank page — profile discovery (CP5)

A profile is data, which is the whole point — but somebody still has to write
the first one. Discovery removes that blank page: connect a tool and the
engine proposes a profile from what that tool actually returns.

```bash
# 1. a brand-new company connects a source (no profile needed yet)
curl -X POST "http://tnega-api-o9ecgm-5fbf26-217-154-175-169.traefik.me/api/connections/github?company_id=acme" \
  -H "Content-Type: application/json" -d '{"token":"","repo":"owner/name"}'
# 2. look at the real payloads and PROPOSE a profile (activates nothing)
curl -X POST "http://tnega-api-o9ecgm-5fbf26-217-154-175-169.traefik.me/api/profile/induce?company_id=acme" \
  -H "Content-Type: application/json" -d '{"source":"github"}'
# 3. a human says yes -> it becomes the live confirmed profile
curl -X POST "http://tnega-api-o9ecgm-5fbf26-217-154-175-169.traefik.me/api/profile/confirm?company_id=acme" \
  -H "Content-Type: application/json" -d '{"version":1}'
```

**The chicken-and-egg it breaks.** Ingestion needs a profile to normalize raw
payloads into Events, so a new company can't ingest first and learn from its
own events. Discovery therefore reads RAW payloads straight off the
connection — the one thing that exists before any profile does. (Which is why
both `POST /api/connections/{source}` and the induce path work with no profile
present, validating against the connector registry instead.)

**Two halves, on purpose** (`core.discovery`):

1. `inspect_payloads()` — pure, deterministic shape analysis: every dotted
   field path, its types, fill rate, cardinality, and a guessed ROLE. Values
   beat names (a field called `id` holding ISO dates is a timestamp), and a
   status must actually *vary* — a string that's identical on every payload is
   a constant, not a state machine. **This is what the golden test pins down**
   (`tests/test_discovery.py`), because it's the half that can be exact.
2. `propose_slots()` — the LLM names what the analysis found (thing type,
   vocabulary, rhythm name). Nondeterministic, so it is never trusted:

**Everything the model proposes is validated by actually running it.**
`validate_profile()` normalizes the very payloads the profile was induced from
through `core.ingest`; if the result has an unusable id, empty content, or an
unresolved actor, the proposal is rejected and discovery falls back to the
deterministic floor. A model that hallucinates `{nonexistent_field}` into an id
template cannot produce a profile — a guess that would quietly emit garbage
events is not a profile.

**What it deliberately does NOT induce.** `moves` is left empty: moves are what
the agent may *do* to a customer's real systems, and inventing write actions
from a payload shape would be reckless — a human adds those. `watchers` is
empty too, and costs nothing, because the CP2 universal built-ins fire on any
company with no profile watchers at all. An induced profile is useful the
moment it's confirmed.

Proven end to end on a real public repo: a company with no profile connected a
tool, discovery examined 9 real payloads and worked out `Issue` / `issue_{number}`
/ `created_at` / `user.login` / `state`, spotted that `closed_at` is only
*sometimes* set (the signature of "this finished") and built an
`issue_resolution_hours` rhythm from it — then, once confirmed, the engine
ingested all 9 real events through that self-written mapping and learned a
`median = 1.76h` baseline, **the same number the hand-written profile
produces**.

## Self-monitoring (CP6 part A)

The same situation/card mechanism the business uses to flag risk is pointed
at the OS itself. Each connector declares a Pydantic schema for its raw
payload (`packages/connectors/*.py`); `pipeline.ingest_raw` validates BEFORE
normalizing, so a source API change fails loudly into `connector_health`
instead of silently producing garbage events. Three signals roll up into
`healthy | degraded | broken`, recomputed every 15 minutes for every company:

1. **Schema failures** — an atomic streak counter (`connector_health.
   consecutive_failures`); ≥3 degrades, ≥10 breaks.
2. **Field completeness** — ~1% of ingested events get their expected fields
   checked for non-null; a fast EMA ("right now") compared against a slow EMA
   ("its own recent history") catches a field quietly going empty without a
   schema violation.
3. **Ingest volume** — `core.norms.compute_volume_baseline()` reuses the SAME
   `learn_norms()` the business rhythms use (`scope="system"` keeps it out of
   the business-facing norms UI); a connector whose hourly volume falls well
   below its own baseline is flagged even if every payload validates fine.

A degraded/broken connector raises a `situations.kind="system"` row through
the same `save_situation`/`resolve_stale` lifecycle as business detection —
recovery retires it automatically. **There is no user/auth system in MarkOS
today**, so rather than fake a "role check" against nothing, `system`-kind
situations are hidden from `GET /api/situations` by default and require
`?include_system=true` — an honest default-visibility filter, not a claim of
authorization.

```bash
curl "http://tnega-api-o9ecgm-5fbf26-217-154-175-169.traefik.me/api/connector-health?company_id=default"
curl "http://tnega-api-o9ecgm-5fbf26-217-154-175-169.traefik.me/api/situations?company_id=default&include_system=true"
```

## Clarifications (CP6 part B)

Some findings aren't a nudge the AI can act on alone — they're a genuine fork
only a human can resolve: "your issue-resolution time just jumped, did
something change or is this a blip?", "I'm seeing 100% fewer GitHub events
than usual, help me reconnect or is this expected?". These render as
`situations.kind="clarification"` — a `Choice[]` of real options instead of
a recommended move — and are the SAME persisted object on the Feed card and
in the agent chat, not two separately-simulated UI states: both surfaces
read the identical `situations` row (Feed) or the identical `messages.
artifacts` blob (chat), and resolving one updates both
(`sync_clarification_artifacts`).

Two detectors currently raise clarifications, both wired into the existing
lifecycle (`packages/core/situations.py`) rather than a parallel mechanism:

- **Norm drift** (`core.norms.detect_drift`) — a mean-shift z-test comparing
  a rhythm's last 14 days against its own prior 90-day history. Choices:
  *recalculate from the last 14 days* (`effect: reset_norm`, persists a
  durable floor in `norm_resets` so future computations keep respecting it),
  *keep the current baseline*, or *remind me in 2 weeks* (`effect: snooze`).
- **Connector degraded** (the same signal as part A above) — choices: *help
  me reconnect*, *this is expected, stop watching* (`effect: disable_source`,
  a new confirmed profile version with that source's `enabled: false` — the
  prior version is kept, so it's a reversible audit trail, not a delete),
  or *snooze 7 days*.

A minimal, real `conversations`/`messages` backbone (checkpoint 6, not the
full CP4 agent loop — no streaming, no tool-use) makes this possible: every
chat turn is persisted through `POST /api/conversations/default/messages`
before it renders, so a clarification card survives a page refresh exactly
like a Feed card does.

```bash
# resolve a clarification the same way the UI's choice buttons do
curl -X POST "http://tnega-api-o9ecgm-5fbf26-217-154-175-169.traefik.me/api/situations/<situation_id>/resolve?company_id=default" \
  -H "Content-Type: application/json" -d '{"choice":"recalculate"}'
```

## Data deletion (CP6 part C)

`POST /api/company/delete` erases every trace of one company. It is
deliberately two calls, not one:

1. **First call** (`{}`) — creates a `deletion_requests` row
   (`status="pending_confirmation"`) and returns a `confirmation_token`.
   **Nothing is deleted yet.**
2. **Second call** (`{"confirm": true, "confirmation_token": "..."}`) —
   transitions the request to `queued` and hands it to a worker job. Poll
   `GET /api/company/delete?company_id=...` to watch it move through
   `running` → `completed`.

`core.erasure.delete_company()` fans out in a fixed order — **Neo4j → Redis
→ Postgres**. Neo4j and Redis hold *derived* state (the graph mirror +
vectors, and transient debounce/job keys); Postgres is the system of record
and goes last, so if anything upstream fails, the source of truth is still
intact and `deletion_requests` shows exactly which stores finished
(`neo4j_done` / `redis_done` / `postgres_done`) — a partial failure is
visible and resumable, not silently incomplete. Every Postgres table scoped
by `company_id` is deleted outright (`events`, `situations`, `actions`,
`conversations` — `messages` cascade with it, `connector_health`,
`norm_baselines`, `norm_resets`, `settings`, `action_tokens`, `tickets`,
`credentials`, `profiles`, and the `companies` row itself), **except**
`audit_log`, which is **anonymized by default**: `actor`/`target`/`metadata`
are cleared but the row, its `action`, and its `created_at` survive — the
fact that something happened stays provable without keeping who did it or
what it said. A hard purge of `audit_log` is available via
`audit_policy: "purge"` on the request.

**In plain language: what backfill imports vs. what deletion removes.**
Connecting a source (Connections tab, or `make ingest`) pulls real
issues/PRs/tickets/messages in and stores them as `events`, mirrors them
into the graph, and links related ones together. `POST /api/company/delete`
reverses all of that for one company — the raw events, everything the AI
inferred from them (situations, the graph, norm baselines), every sealed
credential, every action it took or proposed, and the whole chat history —
down to zero. The one thing that survives is a stripped audit trail proving
*that* a deletion happened and *when*, with no identifying detail attached;
this is the basis for a real privacy policy, not a marketing promise.

```bash
curl -X POST "http://tnega-api-o9ecgm-5fbf26-217-154-175-169.traefik.me/api/company/delete?company_id=<id>" -d '{}'
curl -X POST "http://tnega-api-o9ecgm-5fbf26-217-154-175-169.traefik.me/api/company/delete?company_id=<id>" \
  -H "Content-Type: application/json" \
  -d '{"confirm": true, "confirmation_token": "<token from the first call>"}'
curl "http://tnega-api-o9ecgm-5fbf26-217-154-175-169.traefik.me/api/company/delete?company_id=<id>"   # poll status
```

## Checkpoints

- **CP1 — Profile as data** ✅ profiles table + seven-slot YAML seeds, Neo4j
  graph + native vector index, de-domained engine, guardrail tests (boundary +
  tripwire + Cypher location), two-profile proof.
- **CP2 — Backfill + watcher engine + live feed API** ✅ `backfill(since_days)`
  (history walk, `backfilled=true`, low-priority job); trend-aware +
  outlier-trimmed `learn_norms()` with a maturity flag; the watcher engine
  (5 universal built-ins + profile Tier-1 rules, one highest-severity-wins
  dedupe, live events only) on a 5-min cron; `GET /feed` + `GET /feed/stream`
  (SSE over Redis pub/sub) + `ack`/`dismiss`. (Tier-2 expression language
  deferred — Tier-1 structured conditions are enough for a working engine.)
- **CP3 — Feed page UI** ✅ three card types, evidence detail expand; now live
  via CP2's `/feed/stream` SSE (a new card lands without a refresh).
- **CP4 — Agent chat + Discuss bridge** ✅ real tool-use loop
  (`core.assistant`) over core functions: read tools (search/situations/
  briefing/norms) + `run_action` through the approval brake (agent path is
  not pre-approved, so risky moves still queue for a human). `POST
  /api/agent/chat`, both turns persisted, wired into the existing chat UI.
  (Token streaming of the final answer is a deliberate follow-up.)
- **CP5 — Act + approvals + discovery** ✅ one execution path (`core.act` is
  the ONLY outbound write in the codebase — UI button, autonomous step, agent
  chat and email token all funnel through it; connectors are read-only);
  activity page on the real audit trail; `core.discovery.induce_profile()`
  proposing a profile from a connector's real payloads, self-validated by
  normalizing its own samples, with a deterministic golden test.
- **CP6 — Self-monitoring + clarification + deletion** ✅ part A: connector
  health (schema failures, field-completeness drift, ingest-volume
  baseline) rolled into `healthy | degraded | broken`. part B: norm-drift +
  connector-degraded clarification cards, backed by a minimal real
  conversations/messages/artifacts table so the same card renders on the
  Feed and in chat. part C: `POST /api/company/delete` two-step-confirmed
  erasure (`core.erasure.delete_company`, Neo4j → Redis → Postgres,
  audit_log anonymized not deleted) tracked in `deletion_requests`.
