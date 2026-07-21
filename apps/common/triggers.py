from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from apps.common.context import get_profile
from apps.common.workflows_flow import run_workflow
from packages.core import audit
from packages.core import workflows as wf
from packages.core.db import Session
from packages.core.profile import Profile

# The triggers runtime: what makes a saved workflow fire on its own.
#
# Two unattended paths, both landing in the SAME run_workflow() a person's Run
# button calls — no privileged back door. That matters more than the scheduling
# itself: an unattended run gets act()'s dry-run brake AND the allowlist gate,
# so the worst a mis-planned schedule can do is queue approvals, never fire an
# external write nobody sanctioned.
#
#   schedule — a once-a-minute cron scan matches each workflow's own cron
#   event    — a raised situation matches a workflow's {rule, severity} filter
#
# Both claim an idempotency key (core.workflows.claim_fire) before running, so
# a retried tick, a second worker, or a situation that stays open across many
# watcher passes can never double-fire.

# A single analysis pass can raise many situations at once (a first ingest, a
# noisy morning). An event workflow fires once per matching situation, so cap
# the fan-out per pass: better to handle ten and catch the rest next pass than
# to let one burst turn into a hundred unattended runs.
_MAX_EVENT_FIRES_PER_PASS = 10


# ------------------------------- schedule -------------------------------


async def _run_due(
    session: AsyncSession,
    profiles: dict[str, Profile],
    workflow: wf.Workflow,
    fire_key: str,
    trigger: str,
    only_situation_id: str | None = None,
) -> dict[str, Any] | None:
    """Load the company's own profile, claim the fire, run. Returns None when
    there is no profile to run against, or when the claim was already taken by
    another worker (or an earlier pass).

    Profile first, claim second: a company with no confirmed profile can't run
    anything, and burning its claim would mean the fire is silently lost once
    the profile does land. The claim is still the atomic gate — whoever wins the
    INSERT runs, everyone else backs off.
    """
    profile = profiles.get(workflow.company_id)
    if profile is None:
        try:
            profile = await get_profile(session, workflow.company_id)
        except Exception:
            return None  # nothing to run against yet
        profiles[workflow.company_id] = profile
    if not await wf.claim_fire(session, workflow.id or 0, fire_key):
        return None
    result = await run_workflow(
        session, profile, workflow.id or 0, trigger=trigger, only_situation_id=only_situation_id
    )
    await audit.record(
        session,
        workflow.company_id,
        trigger,
        "workflow.fired",
        metadata={
            "workflow_id": workflow.id,
            "name": workflow.name,
            "fire_key": fire_key,
            "status": result.get("status"),
            "summary": result.get("summary"),
        },
    )
    return {
        "workflow_id": workflow.id,
        "company_id": workflow.company_id,
        "name": workflow.name,
        **result,
    }


async def run_scheduled_workflows(ctx: dict, now: datetime | None = None) -> dict:
    """Worker cron entrypoint, every minute: fire every schedule-triggered
    workflow whose cron matches this minute.

    Scanning once a minute and asking each workflow "is this your minute?" keeps
    the whole scheduler in one dependency-free predicate, and means editing a
    workflow's cron by chat takes effect on the next tick with nothing to
    re-register.
    """
    when = now or datetime.now(UTC)
    bucket = wf.minute_bucket(when)
    fired: list[dict[str, Any]] = []
    async with Session() as session:
        profiles: dict[str, Profile] = {}
        for workflow in await wf.list_triggered(session, "schedule"):
            cron_expr = str((workflow.trigger.config or {}).get("cron") or "")
            if not wf.cron_matches(cron_expr, when):
                continue
            result = await _run_due(session, profiles, workflow, bucket, "scheduled")
            if result is not None:
                fired.append(result)
        await session.commit()
    return {"minute": bucket, "fired": len(fired), "runs": fired}


# -------------------------------- event --------------------------------


def _event_matches(config: dict[str, Any], situation: dict[str, Any]) -> bool:
    """Does a raised situation match an event trigger's filter? Same shape as a
    step selector: every key present must match, absent keys don't constrain."""
    if config.get("rule") and situation.get("rule") != config["rule"]:
        return False
    if config.get("severity") and situation.get("severity") != config["severity"]:
        return False
    return True


async def fire_event_workflows(
    session: AsyncSession, company_id: str, raised: list[dict[str, Any]]
) -> dict:
    """Fire event-triggered workflows for the situations a detection pass just
    raised. Called by the watcher/scan crons AFTER the pass commits, so a
    workflow only ever sees situations that are really on the feed.

    The claim key is the situation id, which is the whole reason this is safe to
    call on every pass: the watcher engine re-raises a still-true situation
    every five minutes, and each one fires its workflow exactly once, ever.
    """
    if not raised:
        return {"fired": 0, "runs": []}
    workflows = [w for w in await wf.list_triggered(session, "event") if w.company_id == company_id]
    if not workflows:
        return {"fired": 0, "runs": []}

    profiles: dict[str, Profile] = {}
    fired: list[dict[str, Any]] = []
    for workflow in workflows:
        config = workflow.trigger.config or {}
        for situation in raised:
            if len(fired) >= _MAX_EVENT_FIRES_PER_PASS:
                break
            if not _event_matches(config, situation):
                continue
            result = await _run_due(
                session,
                profiles,
                workflow,
                f"situation:{situation['id']}",
                "event",
                only_situation_id=str(situation["id"]),
            )
            if result is not None:
                fired.append({**result, "situation_id": situation["id"]})
    await session.commit()
    return {"fired": len(fired), "runs": fired}
