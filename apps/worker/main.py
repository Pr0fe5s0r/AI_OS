from __future__ import annotations

from arq import cron

from apps.common.consolidation import consolidate_all, enabled, interval_seconds
from apps.common.deletion import delete_company_job
from packages.core import graph
from packages.core.pipeline import (
    classify_new_item,
    embed_item,
    ingest_file,
    ingest_text,
    redis_settings,
)

# arq worker:  arq apps.worker.main.WorkerSettings
#
# The write flow: normalise -> index -> embed.
#
# Plus one recurring pass, the consolidation cycle, which is what makes the
# store self-organising: equivalent passages fold into summary nodes,
# duplicates collapse, archived nodes decay away. It is OFF unless
# CONSOLIDATION_ENABLED is set, because it rewrites the retrievable surface and
# a store that begins editing its own contents unasked is not one anybody can
# reason about.


def _schedule() -> list:
    """Cron entries, only when the pass is switched on.

    arq crons are minute-granular, so a sub-minute cadence is expressed as a
    set of second offsets within every minute — 30s becomes {0, 30}.
    """
    if not enabled():
        return []
    step = interval_seconds()
    offsets = {second for second in range(0, 60, step)} if step < 60 else {0}
    return [
        cron(
            consolidate_all,
            second=offsets,
            minute=None if step < 60 else set(range(0, 60, max(1, step // 60))),
            run_at_startup=False,
            max_tries=1,
            timeout=max(120, step * 4),
        )
    ]


class WorkerSettings:
    functions = [ingest_file, ingest_text, embed_item, classify_new_item, delete_company_job]
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
