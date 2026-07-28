from __future__ import annotations

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
# Three jobs, one flow: normalise -> index -> embed. Nothing is scheduled yet;
# source polling arrives with the providers in workstream 4, at which point a
# cron lands here and calls the same ingest jobs the API already uses.


class WorkerSettings:
    functions = [ingest_file, ingest_text, embed_item, classify_new_item, delete_company_job]
    redis_settings = redis_settings()
    max_tries = 3
    job_timeout = 300

    @staticmethod
    async def on_startup(ctx) -> None:
        await graph.bootstrap()

    @staticmethod
    async def on_shutdown(ctx) -> None:
        await graph.close_driver()
