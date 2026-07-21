from __future__ import annotations

import time
from datetime import timedelta

from arq import create_pool
from sqlalchemy.ext.asyncio import AsyncSession

from apps.common.context import connector_specs, resolve_cfg
from packages.connectors.github import webhook_raws
from packages.core.pipeline import redis_settings
from packages.core.profile import Profile

# Event-driven ingestion: the source PUSHES the change the moment it happens.
# The payload rides the exact same pipeline as a scheduled sync — normalize ->
# store -> embed -> resolve — then a debounced analysis pass reacts within
# seconds instead of at the next cron tick. The scheduler stays on as
# reconciliation for missed deliveries.

# Burst debounce: every webhook in the same window shares one analysis job id,
# and arq drops enqueues whose id already exists. 20s also gives ingest_raw
# time to commit the event before detection reads the store.
_DEBOUNCE_SECONDS = 20


def _analysis_job_id(company_id: str, now: float | None = None) -> str:
    bucket = int((now if now is not None else time.time()) // _DEBOUNCE_SECONDS)
    return f"webhook-analyze:{company_id}:{bucket}"


async def handle_github_webhook(
    session: AsyncSession, profile: Profile, event_type: str, payload: dict
) -> dict:
    """Ingest a pushed change and schedule one near-immediate analysis pass."""
    specs = await connector_specs(session, profile)
    github = next((s for s in specs if s["type"] == "github"), None)
    if github is None:
        return {"ignored": "github is not connected"}

    delivered_for = payload.get("repository", {}).get("full_name", "")
    if delivered_for != github.get("repo"):
        # signed or not, we only ingest the repo the operator connected
        return {"ignored": f"repo {delivered_for!r} is not the connected repo"}

    raws = webhook_raws(event_type, payload)
    if not raws:
        return {"ignored": f"unsupported event {event_type!r}"}

    pool = await create_pool(redis_settings())
    try:
        for raw in raws:
            await pool.enqueue_job(
                "ingest_raw", github["source_config"], raw, resolve_cfg(profile)
            )
        job = await pool.enqueue_job(
            "analyze_company",
            profile.company_id,
            "webhook",
            _job_id=_analysis_job_id(profile.company_id),
            _defer_by=timedelta(seconds=_DEBOUNCE_SECONDS),
        )
    finally:
        await pool.aclose()

    return {
        "ingested": len(raws),
        "event": event_type,
        "action": payload.get("action"),
        "analysis": "scheduled" if job is not None else "already scheduled",
    }
