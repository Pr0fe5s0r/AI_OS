# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## What this is

MarkOS — one engine, any industry. `packages/core` is a plain Python library
with **zero domain knowledge** (ingest → resolve → search → norms → detect →
act). What a company's work looks like lives entirely in a **profile**: one
row of data per company with seven JSONB slots (`sources, things, links,
rhythms, watchers, moves, vocabulary`). `profiles/software.yaml` boots an
engineering-ops product; `profiles/inventory.yaml` boots an inventory
product — same engine, no code changes.

## Commands

```bash
cp .env.example .env         # fill in one embedding provider's key + FERNET_KEY
make up                      # postgres + neo4j + redis + api + worker + web, runs migrations
make seed                    # profile rows from profiles/*.yaml — never generates events
make ingest                  # pull real events from every connected source
make analyze                 # learn norms -> detect situations -> assemble briefs
make test                    # pytest inside the api container (106+ tests)
make boundary                # scripts/check_core_boundary.py (AST scan, see Enforced rules)
make lint                    # ruff check . && mypy packages apps
make down                    # docker compose down -v
make logs                    # follow api + worker logs
make psql                    # psql shell into the postgres container
```

A single test: `docker compose exec -T api pytest tests/test_norms.py::test_name -q`
(or run pytest directly on the host if the venv has the deps — `conftest.py`
adds the repo root to `sys.path` either way).

Frontend (`apps/web`, Next.js 14 + Tailwind): `npm run dev` / `npm run build`
/ `npm run lint` / `tsc --noEmit`. The `web` container bind-mounts source and
hot-reloads; `api`/`worker` do **not** — after backend or `.env` changes you
must rebuild + recreate them. On this machine `docker compose build` fails with
a BuildKit "invalid file request Dockerfile" error, so build the image the
legacy way and retag for both services, then recreate:
`DOCKER_BUILDKIT=0 docker build -f apps/api/Dockerfile -t ai_os-api:latest .`
then `docker tag ai_os-api:latest ai_os-worker:latest` and
`docker compose up -d --force-recreate --no-build api worker`. Run `alembic
upgrade head` inside the api container after adding a migration.

**Windows:** run `make` from Git Bash/WSL, or use the underlying `docker
compose` commands directly. Ports are remapped to avoid clashing with other
local stacks: Postgres `5442`, Redis `6389`, Neo4j `7475`/`7688`.

## Enforced rules (all fail CI — tests exist specifically to catch violations)

- `packages/core` imports nothing from `apps/*`. Checked two ways:
  `scripts/check_core_boundary.py` (AST scan) and `tests/test_import_boundary.py`.
- No `industry ==` or hardcoded company/vertical branching anywhere in the
  engine or services — `tests/test_tripwire.py`.
- ALL Cypher lives in `packages/core/graph.py`, nowhere else —
  `tests/test_cypher_location.py`. Same one-place rule applies to the LLM
  client (`packages/core/llm.py` is the only OpenAI-compatible caller).
- **No mock/demo/fixture data outside of tests.** `make seed` only creates
  profile *definitions*; it has never generated a fake event. Every row in
  `events` came from a real connector pull or a real `POST /api/ingest/push`.
  An unconnected company showing an empty state is the correct resting state,
  not a bug to paper over with synthetic data. Test fixtures live only inside
  `pytest` and use throwaway `company_id`s isolated from `default`/`acme-inventory`.

## Architecture

```
packages/core        The engine. Generic functions taking profile slots as ARGUMENTS.
  llm.py                THE single OpenAI-compatible client (embed + chat + chat_with_tools)
  assistant.py           generic tool-use loop (run_tool_loop): LLM <-> tools until a
                          plain-text answer; tools + dispatcher are ARGUMENTS, capped at
                          5 round-trips. Domain toolbox is apps/common/assistant_tools
  oauth.py                generic OAuth2 auth-code flow; provider spec (urls/scope/client)
                          comes from the connector as data. `state` = core.tokens
                          (sealed + 10-min expiry + single-use) and carries company_id —
                          the callback NEVER trusts a query param for whose grant it is
  discovery.py           induce_profile(): deterministic payload shape analysis
                          (inspect_payloads) + LLM naming, ALWAYS validated by normalizing
                          the samples through core.ingest — a proposal that can't normalize
                          its own payloads is rejected. Never induces `moves`.
  profile.py             seven-slot Profile model + loader + YAML seeds +
                          set_source_enabled/disable_source/enable_source (new version each)
  graph.py               THE Cypher module (Neo4j): Things, Event mirrors, typed links,
                          vector index, traversal — all tenancy-scoped
  ingest.py               declarative normalizer (profile mapping -> Event)
  pipeline.py             arq jobs: validate raw payload -> normalize -> store (PG) ->
                          embed (Neo4j) -> resolve
  store.py                 append-only, month-partitioned events + tsvector (PG)
  search.py                hybrid: Neo4j vector similarity + PG full-text, merged
  items.py                  "what have I got?" (vs the feed's "what needs me?"): work
                          items + facet counts (source/type/status). `status_field` is
                          PROFILE data, so nothing here knows GitHub says "state" and an
                          inventory profile says "status". INCLUDES backfilled history
                          (unlike the watcher engine, which is live-only)
  resolve.py                profile entity_rules/links -> Things + SAME_AS/MENTIONS/<closes> links
  norms.py                   rolling-window baselines per profile rhythm — trend-aware
                          (regression, not flat mean) + IQR outlier-trimmed + maturity
                          flag (insufficient|learning|stable); business AND system scope;
                          mean-shift z-test drift detection; durable reset floors (norm_resets)
  detect.py                   the watcher engine (run_watcher_engine): universal built-ins +
                          profile Tier-1 rules, one highest-severity-wins dedupe; LIVE
                          events only (backfilled=false)
  watchers.py                 the 5 universal built-in primitives (stalled_thing,
                          aging_commitment, orphaned_hotspot, broken_rhythm, volume_anomaly)
                          — pure graph/timing/norm, no profile needed
  connector_health.py          schema-failure streaks, field-completeness EMA, ingest-volume
                          baseline rolled into healthy|degraded|broken per connector
  situations.py                 situation lifecycle (raise/resolve_stale/snooze), scoped by
                          kind ("business" vs "system")
  conversations.py               minimal conversations/messages/artifacts backbone
  act.py                          approval-gated action runtime, dry_run safety
  agent.py                         structured decisions: choose move / choose assignee

packages/connectors   Transport only (GitHub real; Slack/Zendesk stubs). Each declares a
                      Pydantic schema for its RAW payload, validated before normalizing —
                      an API drift fails loudly into connector_health instead of producing
                      garbage events.

apps/common           Thin orchestration shared by api + worker: load the profile row,
                      call core functions with its slots.
  health.py               15-min cron: evaluate connector_health, raise/retire a
                          kind="system" situation through the same lifecycle business
                          detection uses
  clarifications.py         norm-drift / connector-degraded clarification cards, posted
                          into both the Feed and the agent chat via conversations.py

apps/api              FastAPI routes (apps/api/main.py). Zero business logic; the profile
                      is loaded per request and passed into core.
apps/worker           arq worker + scan cron (every connected source, every
                      SCAN_INTERVAL_MINUTES) + health cron (every company, every 15 min).
apps/web              Next.js UI, dark theme (tokens in tailwind.config.ts). Views under
                      apps/web/app/views/ (feed, agent, learning, activity, connections).
                      learning.tsx renders GET /api/learning: every learned number with the
                      REAL records that produced it, which were dropped as outliers, and the
                      alert line — evidence, never prose. (An earlier prose "what I know"
                      page was rejected as "a blog"; do not rebuild one, and never expose the
                      seven slots as form fields — that is YAML in a browser.) Words come
                      from vocabulary.terms via term()/plural()/article() in lib.tsx.
profiles/             THE domain knowledge: software.yaml + inventory.yaml seed files —
                      the only place industry-specific logic is allowed to live.
```

### Storage split

- **Postgres 16 (plain, no pgvector)** — system of record: `companies`,
  `profiles` (versioned, proposed|confirmed — versions are never overwritten,
  giving a built-in audit trail), `events` (append-only, month-partitioned,
  `content_tsv`), `norm_baselines`, `norm_resets`, `situations`,
  `conversations`/`messages`, `actions`, `credentials` (Fernet-sealed),
  `settings`, `tickets`, `action_tokens`, `connector_health`, `audit_log`.
- **Neo4j 5 (community)** — the living graph + vectors:
  `(:Thing {id, company_id, thing_type, title, status, last_activity})`,
  `(:Event {id, company_id, event_time, source, embedding})` (a lightweight
  mirror — content stays in Postgres, joined by id), `(:Event)-[:ABOUT]->(:Thing)`,
  and typed thing→thing links whose *names* come from the profile (`CLOSES`,
  `FULFILLS`, `SAME_AS {confidence}`, …). Constraints, indexes, and the cosine
  vector index (1536 dims) are created idempotently by `core.graph.bootstrap()`
  on every boot. **Every Cypher query filters on `company_id`.**

Migrating a legacy v1 database (Postgres `nodes`/`edges` + pgvector)?
`python scripts/migrate_pg_graph_to_neo4j.py` moves everything into Neo4j and
drops the legacy tables. Fresh installs never create them.

### The profile: seven slots

1. **sources** — how raw connector payloads become `Event`s (field mapping,
   templates, defaults).
2. **things** — what `Thing` types exist, which (source, event type) produces
   each, entity-resolution rules (`id_patterns`, similarity thresholds,
   keyword rules).
3. **links** — typed thing→thing relationships; names become Neo4j rel types
   (e.g. `CLOSES` inferred from a PR body matching a regex against an open
   Incident).
4. **rhythms** — what "normal" is measured on (e.g. `issue_resolution_hours`)
   — feeds `norms.py`'s rolling baselines.
5. **watchers** — profile-supplied Tier-1 structured rules (`select` +
   `where` + optional `norm` gate + optional graph condition), each with an
   LLM severity/summary prompt. Universal Level-0 watchers are built into the
   engine and need nothing from the profile.
6. **moves** — the action registry: what the agent may *do* (`http` calls
   against the connector's API, or `log`), which need human approval, plus
   `autonomy` config (confidence threshold, allowed actions, escalation).
7. **vocabulary** — the words this business uses (`thing` → "issue" vs
   "ticket"), `briefing_policy` (mode selection for the headline brief), and
   every LLM prompt template.

Editing a profile is a product change without a deploy: bump the YAML (or the
row), reseed, and the engine behaves differently.

### Self-monitoring (connector_health)

The same situation/card mechanism the business uses to flag risk is pointed
at the OS itself. Three signals roll up into `healthy | degraded | broken`,
recomputed every 15 minutes per company: schema-validation failure streaks
(atomic counter, ≥3 degrades / ≥10 breaks), field-completeness EMA drift
(fast vs. slow moving average on ~1% sampled events), and ingest-volume
baseline (reuses `norms.learn_norms()` with `scope="system"` to stay out of
the business-facing norms UI). A degraded/broken connector raises a
`situations.kind="system"` row through the same lifecycle as business
detection. **There is no user/auth system in MarkOS**, so `system`-kind
situations are hidden from `GET /api/situations` by default and require
`?include_system=true` — an honest default-visibility filter, not a claim of
authorization. Apply the same pattern (visibility flag, not fake role checks)
anywhere else access control might otherwise be tempting.

## Conventions worth knowing before editing

- Concurrent writes (arq runs jobs in parallel) must use atomic SQL
  (`INSERT ... ON CONFLICT DO UPDATE SET x = x + 1 ... RETURNING`, or
  `SELECT ... FOR UPDATE`) — never read-then-write in Python. This bit us
  once in `connector_health.record_failure`.
- Postgres interval arithmetic: use `make_interval(days => :window)`, not
  string-concatenation into `::interval` — asyncpg rejects the latter.
- `Choice` effects on clarification cards (`reset_norm | disable_source |
  snooze | none`) are dispatched data-driven, mirroring `moves.registry` —
  keep new effects generic in core, not hardcoded per-caller.
- `profile.py`'s idempotent seeding strips the runtime-only `enabled` field
  before comparing slots, so a redeploy can't silently revert a live
  `disable_source()` call.
- The watcher engine re-runs every 5 min, so anything it creates must be
  idempotent per situation or it piles up. Two guards: `act()` reuses an
  existing `pending_approval` action for the same (situation, action) instead
  of inserting a duplicate (the approval queue is a SET of live asks, not a
  log); and the autonomous step (`analysis._situation_already_actioned`) skips
  a situation that already has a terminal action — `pending_approval`/
  `executed` always, plus `dry_run` while Practice mode is on (so flipping to
  live still lets a rehearsed situation get a real attempt). Don't remove
  either guard when adding a new autonomous action path.
