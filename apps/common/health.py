from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import text

from apps.common.clarifications import create_connector_clarification
from apps.common.context import get_profile
from packages.core import connector_health as ch
from packages.core.db import Session
from packages.core.norms import compute_volume_baseline, current_hourly_volume
from packages.core.situations import resolve_stale, save_situation
from packages.shared.schema import Situation

# Self-monitoring cron (checkpoint 6, part A): every 15 minutes, recompute
# connector_health.status for every connector of every company, weighing all
# three signals (schema failures, field-completeness drift, ingest volume vs
# its own norm), and raise/retire a `kind="system"` situation through the
# SAME situation lifecycle business detection uses (save_situation /
# resolve_stale) — this is not a parallel mechanism, it's the same one
# pointed at the system itself.

_VOLUME_DROP_K = 2.0     # current hourly volume below median - k*std -> anomaly
_MIN_OBSERVATIONS = 24   # need at least a day of hourly buckets before judging volume


async def _companies_with_profiles(session) -> list[str]:
    rows = await session.execute(
        text("SELECT DISTINCT company_id FROM profiles WHERE status = 'confirmed'")
    )
    return [r.company_id for r in rows]


async def _evaluate_one(session, company_id: str, source: str) -> dict:
    """Recompute connector_health.status for one (company, source) pair."""
    prior = await ch.get_health_one(session, company_id, source)
    consecutive_failures = (prior or {}).get("consecutive_failures", 0)
    dropped_fields = ch.completeness_drop((prior or {}).get("field_completeness", {}))

    baseline = await compute_volume_baseline(session, company_id, source)
    current = await current_hourly_volume(session, company_id, source)
    volume_low = bool(
        baseline.n >= _MIN_OBSERVATIONS and current < (baseline.median - _VOLUME_DROP_K * baseline.std)
    )

    if consecutive_failures >= ch.BROKEN_FAILURES:
        status = "broken"
    elif consecutive_failures >= ch.DEGRADED_FAILURES or volume_low or dropped_fields:
        status = "degraded"
    else:
        status = "healthy"

    await ch.set_status(session, source, company_id, status, events_per_hour_norm=baseline.median)
    return {
        "source": source,
        "status": status,
        "volume_low": volume_low,
        "current": current,
        "median": baseline.median,
        "dropped_fields": dropped_fields,
        "consecutive_failures": consecutive_failures,
    }


def _system_situation(company_id: str, result: dict) -> Situation:
    reasons = []
    if result["consecutive_failures"] >= ch.DEGRADED_FAILURES:
        reasons.append(f"{result['consecutive_failures']} consecutive malformed payloads")
    if result["volume_low"]:
        reasons.append(
            f"only {result['current']} events in the last hour vs a normal ~{round(result['median'])}"
        )
    if result["dropped_fields"]:
        reasons.append(f"data quality dropped on: {', '.join(result['dropped_fields'])}")
    summary = "; ".join(reasons) or "connector health degraded"

    return Situation(
        id=f"connector_health:{company_id}:{result['source']}",
        company_id=company_id,
        rule="connector_health",
        severity="critical" if result["status"] == "broken" else "high",
        title=f"The {result['source']} connection looks {result['status']}",
        summary=summary,
        recommended_action="Check the connection's credentials, and whether the source's API has changed.",
        evidence=[],
        status="open",
        created_at=datetime.now(UTC),
        kind="system",
    )


async def evaluate_connector_health(ctx: dict) -> dict:
    """Worker cron entrypoint, every 15 minutes."""
    summary: dict[str, list[str]] = {}
    async with Session() as session:
        for company_id in await _companies_with_profiles(session):
            profile = await get_profile(session, company_id)
            sources = [
                s["source"] for s in profile.sources
                if s.get("kind") == "connector" and s.get("enabled", True)
            ]

            flagged_ids: list[str] = []
            flagged_sources: list[str] = []
            for source in sources:
                result = await _evaluate_one(session, company_id, source)
                if result["status"] in ("degraded", "broken"):
                    await save_situation(session, _system_situation(company_id, result))
                    await create_connector_clarification(session, company_id, result)
                    flagged_ids.append(f"connector_health:{company_id}:{source}")
                    flagged_sources.append(source)

            # a connector that recovered retires its own system situation —
            # scoped to kind="system" so business situations are untouched
            await resolve_stale(session, company_id, flagged_ids, kind="system")
            summary[company_id] = flagged_sources
        await session.commit()
    return summary

