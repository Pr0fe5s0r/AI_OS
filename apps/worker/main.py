from __future__ import annotations

from arq import cron

from apps.common.consolidation import consolidate_all, enabled, interval_seconds
from apps.common.deletion import delete_company_job
from apps.common.mapper import map_unmapped_all, mapper_enabled, mapper_interval
from packages.core import graph
from packages.core.pipeline import (
    classify_new_item,
    embed_item,
    ingest_file,
    ingest_text,
    redis_settings,
    summarize_item,
)

# arq worker:  arq apps.worker.main.WorkerSettings
#
# The write flow: normalise -> index -> embed -> [summarize].
#
# Plus two recurring passes:
#   1. Consolidation — folds equivalent passages, drops duplicates, decays
#      archived nodes. OFF by default (CONSOLIDATION_ENABLED).
#   2. Probe-mapper — finds unmapped chunks and generates summaries until every
#      passage sits under one. OFF by default (MAPPER_ENABLED). Non-destructive:
#      it adds navigation but never archives or rewrites passages.


def _schedule() -> list:
    """Cron entries for consolidation and the probe-mapper (each gated)."""
    jobs: list = []

    # Consolidation cron — unchanged from before.
    if enabled():
        step = interval_seconds()
        offsets = {second for second in range(0, 60, step)} if step < 60 else {0}
        jobs.append(
            cron(
                consolidate_all,
                second=offsets,
                minute=None if step < 60 else set(range(0, 60, max(1, step // 60))),
                run_at_startup=False,
                max_tries=1,
                timeout=max(120, step * 4),
            )
        )

    # Probe-mapper cron — runs on a separate cadence.
    if mapper_enabled():
        step = mapper_interval()
        offsets = {second for second in range(0, 60, step)} if step < 60 else {0}
        jobs.append(
            cron(
                map_unmapped_all,
                second=offsets,
                minute=None if step < 60 else set(range(0, 60, max(1, step // 60))),
                run_at_startup=False,
                max_tries=1,
                timeout=max(120, step * 4),
            )
        )

    return jobs


class WorkerSettings:
    functions = [
        ingest_file, ingest_text, embed_item, classify_new_item,
        summarize_item, delete_company_job,
    ]
    cron_jobs = _schedule()
    redis_settings = redis_settings()
    max_tries = 3
    job_timeout = 300

    @staticmethod
    async def on_startup(ctx) -> None:
        await graph.bootstrap()

    @staticmethod
    async def on_shutdown(ctx) -> None:
        await graph.close_driver()
