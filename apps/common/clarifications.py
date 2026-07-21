from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core import audit
from packages.core.conversations import (
    add_message,
    get_or_create_default_conversation,
    has_clarification_message,
)
from packages.core.norms import detect_drift, reset_norms
from packages.core.profile import Profile, disable_source, load_profile, save_profile
from packages.core.situations import (
    get_situation,
    is_snoozed,
    mark_resolved,
    save_situation,
    set_snoozed_until,
)
from packages.shared.schema import Choice, Situation


def clarification_artifact(situation: Situation) -> dict[str, Any]:
    """The one structured shape rendered by both Feed and Agent chat."""
    return {
        "type": "clarification",
        "situation_id": situation.id,
        "title": situation.title,
        "summary": situation.summary,
        "evidence": [e.model_dump(mode="json") for e in situation.evidence],
        "choices": [c.model_dump(mode="json") for c in situation.choices or []],
        "resolved_choice": situation.resolved_choice,
        "status": situation.status,
    }


async def post_clarification_to_chat(session: AsyncSession, situation: Situation) -> None:
    conversation_id = await get_or_create_default_conversation(session, situation.company_id)
    if await has_clarification_message(session, conversation_id, situation.id):
        return
    await add_message(
        session,
        conversation_id,
        "agent",
        situation.title,
        artifacts=[clarification_artifact(situation)],
    )


async def create_norm_drift_clarifications(
    session: AsyncSession, profile: Profile
) -> list[Situation]:
    out: list[Situation] = []
    for rhythm in profile.rhythms:
        report = await detect_drift(session, profile.company_id, rhythm)
        if report is None:
            continue
        metric = report["metric"]
        situation_id = f"clarification:norm_drift:{profile.company_id}:{metric}"
        if await is_snoozed(session, profile.company_id, situation_id):
            continue
        before = datetime.now(UTC) - timedelta(days=14)
        unit = report.get("unit", "")
        direction = "higher" if report["direction"] == "up" else "lower"
        situation = Situation(
            id=situation_id,
            company_id=profile.company_id,
            rule="norm_drift",
            severity="medium",
            kind="clarification",
            title=f"Your {metric} looks different lately",
            summary=(
                f"The last 14 days average {report['recent_mean']}{unit}, "
                f"which is {direction} than the prior average of "
                f"{report['prior_mean']}{unit} by {report['shift_std_devs']} std devs."
            ),
            recommended_action=None,
            evidence=[],
            status="open",
            created_at=datetime.now(UTC),
            choices=[
                Choice(
                    id="recalculate",
                    label="Recalculate from the last 14 days",
                    effect="reset_norm",
                    effect_args={"metric": metric, "before_date": before.isoformat()},
                ),
                Choice(id="keep", label="Keep current baseline", effect="none"),
                Choice(
                    id="snooze_14d",
                    label="Remind me in 2 weeks",
                    effect="snooze",
                    effect_args={"days": 14},
                ),
            ],
        )
        await save_situation(session, situation)
        await post_clarification_to_chat(session, situation)
        out.append(situation)
    return out


async def create_connector_clarification(
    session: AsyncSession, company_id: str, result: dict
) -> Situation | None:
    source = result["source"]
    situation_id = f"clarification:connector_degraded:{company_id}:{source}"
    if await is_snoozed(session, company_id, situation_id):
        return None
    median = max(float(result.get("median") or 0), 1.0)
    current = float(result.get("current") or 0)
    drop = max(0, round((1 - current / median) * 100))
    situation = Situation(
        id=situation_id,
        company_id=company_id,
        rule="connector_degraded",
        severity="medium",
        kind="clarification",
        title=f"The {source} connection looks broken",
        summary=(
            f"I'm seeing about {drop}% fewer {source} events than usual. "
            "Should I help reconnect, or did the team stop using this source?"
        ),
        recommended_action=None,
        evidence=[],
        status="open",
        created_at=datetime.now(UTC),
        choices=[
            Choice(id="reconnect", label="Help me reconnect", effect="none"),
            Choice(
                id="stop_watching",
                label="This is expected, stop watching",
                effect="disable_source",
                effect_args={"source": source},
            ),
            Choice(
                id="snooze_7d",
                label="Snooze 7 days",
                effect="snooze",
                effect_args={"days": 7},
            ),
        ],
    )
    await save_situation(session, situation)
    await post_clarification_to_chat(session, situation)
    return situation


async def sync_clarification_artifacts(session: AsyncSession, situation: Situation) -> None:
    """Keep chat artifacts as presentations of the same resolved situation."""
    rows = await session.execute(
        text(
            """
            SELECT id, artifacts FROM messages
            WHERE artifacts @> CAST(:needle AS jsonb)
            """
        ),
        {"needle": json.dumps([{"situation_id": situation.id}])},
    )
    for row in rows:
        artifacts = row.artifacts if isinstance(row.artifacts, list) else []
        changed = False
        for artifact in artifacts:
            if artifact.get("situation_id") == situation.id:
                artifact.update(clarification_artifact(situation))
                changed = True
        if changed:
            await session.execute(
                text("UPDATE messages SET artifacts = CAST(:artifacts AS jsonb) WHERE id = :id"),
                {"id": row.id, "artifacts": json.dumps(artifacts)},
            )

def _choice_by_id(situation: Situation, choice_id: str) -> Choice | None:
    return next((c for c in situation.choices or [] if c.id == choice_id), None)


async def _bump_profile_version(session: AsyncSession, company_id: str) -> int | None:
    profile = await load_profile(session, company_id)
    if profile is None:
        return None
    return await save_profile(session, profile, status="confirmed")


async def resolve_clarification(
    session: AsyncSession,
    profile: Profile,
    situation_id: str,
    choice_id: str,
    resolved_by: str = "ui",
) -> dict:
    before = await get_situation(session, profile.company_id, situation_id)
    if before is None:
        return {"status": "not_found"}
    choice = _choice_by_id(before, choice_id)
    if before.kind != "clarification" or choice is None:
        return {"status": "invalid_choice"}

    updated = await mark_resolved(session, profile.company_id, situation_id, choice_id, resolved_by)
    if updated is None:
        return {"status": "invalid_choice"}

    effect_result: dict[str, Any] = {"effect": choice.effect}
    args = choice.effect_args
    if choice.effect == "reset_norm":
        metric = str(args["metric"])
        before_date = datetime.fromisoformat(str(args["before_date"]).replace("Z", "+00:00"))
        defn = next((r for r in profile.rhythms if r.get("name") == metric), None)
        baseline = await reset_norms(
            session, profile.company_id, metric, before_date, reset_by=resolved_by, defn=defn
        )
        version = await _bump_profile_version(session, profile.company_id)
        effect_result.update(
            {
                "metric": metric,
                "reset_before": before_date.isoformat(),
                "profile_version": version,
                "baseline": baseline.model_dump(mode="json") if baseline else None,
            }
        )
    elif choice.effect == "disable_source":
        source = str(args["source"])
        new_profile = await disable_source(session, profile.company_id, source)
        effect_result.update(
            {"source": source, "profile_version": new_profile.version if new_profile else None}
        )
    elif choice.effect == "snooze":
        days = int(args.get("days", 7))
        until = datetime.now(UTC) + timedelta(days=days)
        await set_snoozed_until(session, profile.company_id, situation_id, until)
        effect_result.update({"snoozed_until": until.isoformat()})

    await sync_clarification_artifacts(session, updated)

    await audit.record(
        session,
        profile.company_id,
        resolved_by,
        "clarification.resolved",
        target=situation_id,
        metadata={"choice": choice.model_dump(mode="json"), "effect": effect_result},
    )
    return {
        "status": "resolved",
        "choice": choice.model_dump(mode="json"),
        "effect": effect_result,
        "situation": updated.model_dump(mode="json"),
    }



