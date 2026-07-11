from __future__ import annotations

from arq import cron

from packages.core.pipeline import embed_event, ingest_raw, redis_settings, resolve_event
from verticals.software.scheduler import (
    analyze_company,
    scan_enabled,
    scan_minutes,
    scheduled_scan,
)

# arq worker: `arq apps.worker.main.WorkerSettings`
# Composes the generic core pipeline jobs. All domain knowledge (source_config,
# entity_rules) arrives as job arguments — never imported.
#
# The app layer is also where the vertical's cron lives: the core has no idea a
# "scan" exists, and the vertical has no idea it is being scheduled.

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


class WorkerSettings:
    # analyze_company is the event-driven path: enqueued by the GitHub webhook
    # seconds after an issue changes, debounced so bursts run once.
    functions = [ingest_raw, embed_event, resolve_event, analyze_company]
    cron_jobs = [_SCAN] if scan_enabled() else []
    redis_settings = redis_settings()
    max_tries = 4
    keep_result = 3600
