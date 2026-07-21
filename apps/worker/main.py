from __future__ import annotations

from arq import cron

from apps.common.deletion import delete_company_job
from apps.common.health import evaluate_connector_health
from apps.common.scheduling import (
    analyze_company,
    scan_enabled,
    scan_minutes,
    scheduled_scan,
)
from apps.common.watching import evaluate_all_watchers
from packages.core import graph
from packages.core.pipeline import (
    backfill_source,
    embed_event,
    ingest_raw,
    redis_settings,
    resolve_event,
)

# arq worker: `arq apps.worker.main.WorkerSettings`
# Composes the generic core pipeline jobs. All domain knowledge (source_config,
# things/links slots) arrives as job arguments loaded from profile rows.

_SCAN = cron(
    scheduled_scan,
    minute=scan_minutes(),
    # unique: with two workers running, only one scan happens per tick
    unique=True,
    # never on boot — a restart must not fire off a round of real emails
    run_at_startup=False,
    # a retried scan would re-email everyone; fail loudly instead
    max_tries=1,
    # ingest waits for the queue, then every situation gets an LLM call
    timeout=900,
)

# Self-monitoring: independent of the business scan cadence, always every 15
# minutes, on its own cron minutes so a slow business scan never delays it.
_HEALTH_SCAN = cron(
    evaluate_connector_health,
    minute={0, 15, 30, 45},
    unique=True,
    run_at_startup=False,
    max_tries=1,
    timeout=300,
)

# The watcher engine (checkpoint 2, part C): every 5 minutes, its own
# cadence, faster than the LLM-heavier business scan because the universal
# built-in primitives are pure algorithms — no LLM call, no external fetch.
_WATCHER_SCAN = cron(
    evaluate_all_watchers,
    minute={0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55},
    unique=True,
    run_at_startup=False,
    max_tries=1,
    timeout=300,
)


async def startup(ctx: dict) -> None:
    # constraints + vector index must exist before the first embed job lands
    await graph.bootstrap()


async def shutdown(ctx: dict) -> None:
    await graph.close_driver()


class WorkerSettings:
    # analyze_company is the event-driven path: enqueued by webhooks seconds
    # after something changes, debounced so bursts run once.
    functions = [
        ingest_raw, embed_event, resolve_event, analyze_company,
        evaluate_connector_health, delete_company_job, backfill_source,
    ]
    cron_jobs = ([_SCAN] if scan_enabled() else []) + [_HEALTH_SCAN, _WATCHER_SCAN]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = redis_settings()
    max_tries = 4
    keep_result = 3600
