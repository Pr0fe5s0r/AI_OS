from __future__ import annotations

from arq import cron

from apps.common.consolidation import consolidate_all, enabled, interval_seconds
from apps.common.deletion import delete_company_job
from apps.common.mapper import map_unmapped_all, mapper_enabled, mapper_interval
from apps.common.summaries import backfill_all
from apps.common.summaries import enabled as summaries_enabled
from apps.common.summaries import interval_seconds as summaries_interval
from packages.core import graph
from packages.core.pipeline import (
    JOB_TIMEOUT_SECONDS,
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
#   3. Summary backfill — gives a card to every document that has none. ON by
#      default (SUMMARIES_ENABLED), because retrieval ROUTES on cards: on a
#      store bigger than the catalogue window the card decides which documents
#      a question may see. New uploads are summarised at ingest; this is for
#      everything uploaded before that was true.


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

    # Summary backfill cron — the only one of the three that is ON by default,
    # because a store with no cards routes on the weaker signal and nobody would
    # know. Bounded per pass (see summaries.BATCH) so a large backfill drains
    # steadily instead of starving live ingestion of the same rate limit.
    if summaries_enabled():
        step = summaries_interval()
        offsets = {second for second in range(0, 60, step)} if step < 60 else {0}
        jobs.append(
            cron(
                backfill_all,
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
    job_timeout = JOB_TIMEOUT_SECONDS

    @staticmethod
    async def on_startup(ctx) -> None:
        await graph.bootstrap()

    @staticmethod
    async def on_shutdown(ctx) -> None:
        await graph.close_driver()
