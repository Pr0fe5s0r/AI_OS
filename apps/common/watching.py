from __future__ import annotations

from sqlalchemy import text

from apps.common.analysis import evaluate_watchers
from apps.common.context import get_profile
from apps.common.triggers import fire_event_workflows
from packages.core import audit
from packages.core.db import Session
from packages.core.norms import get_norms

# Checkpoint 2, part C: the watcher engine's OWN cadence — every 5 minutes,
# independent of the (LLM-heavier) business scan's 15-minute/webhook
# cadence. Reads whatever norm baselines already exist rather than
# recomputing them; those refresh on the business scan's schedule, and a
# few minutes of staleness doesn't meaningfully change a threshold.


async def _companies_with_profiles(session) -> list[str]:
    rows = await session.execute(
        text("SELECT DISTINCT company_id FROM profiles WHERE status = 'confirmed'")
    )
    return [r.company_id for r in rows]


async def evaluate_all_watchers(ctx: dict) -> dict:
    """Worker cron entrypoint, every 5 minutes."""
    summaries: dict[str, dict] = {}
    async with Session() as session:
        for company_id in await _companies_with_profiles(session):
            profile = await get_profile(session, company_id)
            norms = await get_norms(session, company_id)
            summary = await evaluate_watchers(session, profile, norms)
            summary["trigger"] = "watcher_cron"
            # event-triggered workflows fire off what this pass raised. Each
            # situation claims its own key, so a situation that stays true
            # across passes fires once, not every five minutes.
            summary["events"] = await fire_event_workflows(
                session, company_id, summary.get("raised", [])
            )
            await audit.record(session, company_id, "watcher_cron", "watchers.run", metadata=summary)
            summaries[company_id] = summary
        await session.commit()
    return summaries
