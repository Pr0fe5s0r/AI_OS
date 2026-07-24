from __future__ import annotations

import os

from sqlalchemy import text

from apps.common.analysis import run_analysis
from apps.common.context import connector_specs, get_profile, resolve_cfg
from apps.common.ingestion import trigger_ingest
from apps.common.triggers import fire_event_workflows
from packages.core import audit
from packages.core.db import Session
from packages.core.pipeline import backfill_source

# Unattended scanning, for EVERY company with a confirmed profile. The worker
# has no idea what any company does — it loads the profile row and runs the
# same loop. Switchable, and it refuses to overlap with itself (arq unique).

_OFF = {"0", "false", "no", "off", ""}


def scan_enabled() -> bool:
    return os.getenv("SCAN_ENABLED", "true").strip().lower() not in _OFF


def scan_interval() -> int:
    """Minutes between scans, clamped to something a cron expression can express."""
    try:
        minutes = int(os.getenv("SCAN_INTERVAL_MINUTES", "15"))
    except ValueError:
        minutes = 15
    return max(1, min(minutes, 60))


def scan_minutes() -> set[int]:
    """Which minutes past the hour the scan fires on."""
    interval = scan_interval()
    return {m for m in range(60) if m % interval == 0}


def ingest_timeout() -> float:
    try:
        return float(os.getenv("SCAN_INGEST_TIMEOUT", "120"))
    except ValueError:
        return 120.0


async def _companies_with_profiles(session) -> list[str]:
    rows = await session.execute(
        text("SELECT DISTINCT company_id FROM profiles WHERE status = 'confirmed'")
    )
    return [r.company_id for r in rows]


async def scheduled_scan(ctx: dict) -> dict:
    """The cron entrypoint: sync every connected source, then analyse — per company."""
    if not scan_enabled():
        return {"skipped": "SCAN_ENABLED=false"}

    summaries: dict[str, dict] = {}
    async with Session() as session:
        for company_id in await _companies_with_profiles(session):
            profile = await get_profile(session, company_id)
            ingested = await trigger_ingest(session, profile, wait=ingest_timeout())
            summary = await run_analysis(session, profile)
            summary["ingested"] = ingested
            summary["trigger"] = "scheduler"
            summary["events"] = await fire_event_workflows(
                session, company_id, summary.get("raised", [])
            )
            await audit.record(
                session, company_id, "scheduler", "analyze.run", metadata=summary
            )
            summaries[company_id] = summary
        await session.commit()
    return summaries


async def first_sync(
    ctx: dict, company_id: str, source: str | None = None, since_days: int = 90
) -> dict:
    """Everything that has to happen the moment a tool is connected, in the
    one order that produces a truthful first screen.

    Connecting used to only run discovery — the shape of the data was learned
    and then nothing pulled any. The workspace sat empty until the next scan
    tick, and even then the agent knew nothing about issues that were opened or
    closed before we arrived.

    LIVE FIRST, history second, and the order is the whole point. `store_event`
    deliberately never overwrites the `backfilled` flag (it records how an
    event was FIRST seen), and the watcher engine only looks at
    backfilled=false. Walk history first and every issue that is open RIGHT NOW
    gets stamped as history — permanently invisible to detection, so Attention
    stays empty on a repo full of stalled work. The live pass covers current
    state (`state=all`), so running it first claims those events as live; the
    backfill afterwards can only add the older ones it did not reach.
    """
    async with Session() as session:
        profile = await get_profile(session, company_id)
        ingested = await trigger_ingest(
            session, profile, only_source=source, wait=ingest_timeout()
        )
        specs = await connector_specs(session, profile)

    history: dict[str, dict] = {}
    for spec in specs:
        if source and spec["source"] != source:
            continue
        history[spec["source"]] = await backfill_source(
            ctx, spec, resolve_cfg(profile), since_days
        )

    async with Session() as session:
        profile = await get_profile(session, company_id)
        summary = await run_analysis(session, profile)
        summary["ingested"] = ingested
        summary["history"] = history
        summary["trigger"] = "connect"
        await audit.record(session, company_id, "connect", "analyze.run", metadata=summary)
        await session.commit()
    return summary


async def analyze_company(ctx: dict, company_id: str, trigger: str = "webhook") -> dict:
    """Analysis without a fresh poll — the event was already pushed to us.

    Enqueued by the webhook endpoint with a debounced job id, so a burst of
    deliveries becomes ONE detection pass instead of five.
    """
    async with Session() as session:
        profile = await get_profile(session, company_id)
        summary = await run_analysis(session, profile)
        summary["trigger"] = trigger
        # the webhook path is the fastest route from "it happened" to "we acted"
        summary["events"] = await fire_event_workflows(
            session, company_id, summary.get("raised", [])
        )
        await audit.record(session, company_id, trigger, "analyze.run", metadata=summary)
        await session.commit()
    return summary
