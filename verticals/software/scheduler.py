from __future__ import annotations

import os

from packages.core import audit
from packages.core.db import Session
from verticals.software.alerting import run_analysis
from verticals.software.config import COMPANY_ID
from verticals.software.ingest import trigger_ingest

# Unattended scanning.
#
# Everything the "Scan workspace" button does, on a timer: pull each connected
# source, wait for the events to land, then detect -> brief -> email the PM ->
# take the autonomous follow-up action.
#
# This is the difference between an assistant you have to poke and an OS that
# notices on its own. It is also the moment the system starts sending real email
# and writing to real repos with nobody watching, so it is switchable and it
# refuses to overlap with itself.

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


async def scheduled_scan(ctx: dict) -> dict:
    """The cron entrypoint. Same path as POST /api/ingest + POST /api/analyze."""
    if not scan_enabled():
        return {"skipped": "SCAN_ENABLED=false"}

    async with Session() as session:
        ingested = await trigger_ingest(session, COMPANY_ID, wait=ingest_timeout())
        summary = await run_analysis(session, COMPANY_ID)
        summary["ingested"] = ingested
        summary["trigger"] = "scheduler"
        await audit.record(
            session, COMPANY_ID, "scheduler", "analyze.run", metadata=summary
        )
        await session.commit()
    return summary


async def analyze_company(ctx: dict, company_id: str = COMPANY_ID, trigger: str = "webhook") -> dict:
    """Analysis without a fresh poll — the event was already pushed to us.

    Enqueued by the webhook endpoint with a debounced job id, so a burst of
    GitHub deliveries (label + assign + comment in one minute) becomes ONE
    detection pass instead of five.
    """
    async with Session() as session:
        summary = await run_analysis(session, company_id)
        summary["trigger"] = trigger
        await audit.record(session, company_id, trigger, "analyze.run", metadata=summary)
        await session.commit()
    return summary
