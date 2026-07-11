from __future__ import annotations

import time
from datetime import timedelta

from arq import create_pool
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.pipeline import redis_settings
from verticals.software.config import (
    COMPANY_ID,
    ENTITY_RULES,
    _github_source_config,
    connector_config,
)

# Event-driven ingestion: GitHub PUSHES the change the moment an issue or PR is
# opened, edited, labelled, assigned or closed. The webhook payload carries the
# same issue object the REST API returns, so it rides the exact same pipeline
# as a scheduled sync — normalize -> store -> embed -> resolve — and then a
# debounced analysis pass lets the AI react within seconds, not at the next
# 15-minute tick. The scheduler stays on as reconciliation: GitHub retries a
# failed delivery only once, so polling is the safety net for missed pushes.

SIGNATURE_HEADER = "X-Hub-Signature-256"
EVENT_HEADER = "X-GitHub-Event"

# Webhook event -> the payload key holding the REST-shaped object.
_PAYLOAD_KEY = {"issues": "issue", "pull_request": "pull_request"}

# Burst debounce: every webhook in the same window shares one analysis job id,
# and arq drops enqueues whose id already exists. 20s also gives ingest_raw
# time to commit the event before detection reads the store.
_DEBOUNCE_SECONDS = 20


def github_webhook_raws(event_type: str, payload: dict) -> list[dict]:
    """Reshape a webhook payload into the connector's raw format (pure)."""
    key = _PAYLOAD_KEY.get(event_type)
    if key is None or key not in payload:
        return []
    raw = dict(payload[key])
    raw["_repo"] = payload.get("repository", {}).get("name", "")
    if event_type == "pull_request":
        # the PR object has no "pull_request" marker key the way the issues
        # API does — set it so the field mapping types this as a pull_request
        raw.setdefault("pull_request", {})
        raw["_merged_at"] = raw.get("merged_at") or raw.get("closed_at")
    else:
        raw["_merged_at"] = raw.get("closed_at")
    return [raw]


def _analysis_job_id(company_id: str, now: float | None = None) -> str:
    bucket = int((now if now is not None else time.time()) // _DEBOUNCE_SECONDS)
    return f"webhook-analyze:{company_id}:{bucket}"


async def handle_github_webhook(
    session: AsyncSession, event_type: str, payload: dict, company_id: str = COMPANY_ID
) -> dict:
    """Ingest a pushed change and schedule one near-immediate analysis pass."""
    specs = await connector_config(session, company_id)
    github = next((s for s in specs if s["type"] == "github"), None)
    if github is None:
        return {"ignored": "github is not connected"}

    delivered_for = payload.get("repository", {}).get("full_name", "")
    if delivered_for != github["repo"]:
        # signed or not, we only ingest the repo the operator connected
        return {"ignored": f"repo {delivered_for!r} is not the connected repo"}

    raws = github_webhook_raws(event_type, payload)
    if not raws:
        return {"ignored": f"unsupported event {event_type!r}"}

    source_config = _github_source_config(github["repo"], company_id)
    pool = await create_pool(redis_settings())
    try:
        for raw in raws:
            await pool.enqueue_job("ingest_raw", source_config, raw, ENTITY_RULES)
        job = await pool.enqueue_job(
            "analyze_company",
            company_id,
            "webhook",
            _job_id=_analysis_job_id(company_id),
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
