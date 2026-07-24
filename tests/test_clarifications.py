from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from apps.common.clarifications import create_norm_drift_clarifications, resolve_clarification
from packages.core.conversations import get_or_create_default_conversation, list_messages
from packages.core.db import Session
from packages.core.profile import Profile
from packages.core.situations import get_situation, is_snoozed

# Checkpoint 6, part B: a norm-drift scenario in seeded data must produce a
# real `kind="clarification"` situation AND land the same artifact in chat —
# the two surfaces are two renderings of the one persisted object, not
# separately-simulated UI state.

_CO = "test-clarifications"

_RHYTHM = {
    "name": "issue_resolution_hours",
    "unit": "hours",
    "window_days": 120,
    "source": "github",
    "type": "issue",
    "end_field": "closed_at",
}


async def _reset(session) -> None:
    await session.execute(text("DELETE FROM events WHERE company_id = :c"), {"c": _CO})
    await session.execute(text("DELETE FROM situations WHERE company_id = :c"), {"c": _CO})
    await session.execute(text("DELETE FROM conversations WHERE company_id = :c"), {"c": _CO})
    await session.execute(text("DELETE FROM norm_resets WHERE company_id = :c"), {"c": _CO})
    await session.commit()


async def _seed_duration_event(session, eid: str, days_ago: float, duration_hours: float) -> None:
    now = datetime.now(UTC)
    start = now - timedelta(days=days_ago)
    end = start + timedelta(hours=duration_hours)
    await session.execute(
        text(
            """
            INSERT INTO events (id, company_id, source, type, actor_id, actor_name,
                                 timestamp, content, metadata, content_tsv)
            VALUES (:id, :c, 'github', 'issue', 'u', 'u', :ts, 'x', CAST(:md AS jsonb), to_tsvector('x'))
            ON CONFLICT (company_id, id, timestamp) DO NOTHING
            """
        ),
        {"id": eid, "c": _CO, "ts": start, "md": json.dumps({"closed_at": end.isoformat()})},
    )


async def _seed_drift(session) -> None:
    # prior window (14-104 days ago): fast and consistent, varied so std != 0
    for i, hrs in enumerate([1, 2, 3, 2, 1, 3, 2, 1, 3, 2, 1, 2]):
        await _seed_duration_event(session, f"prior-{i}", days_ago=20 + i, duration_hours=hrs)
    # recent window (0-14 days ago): a sustained, much slower pattern
    for i in range(6):
        await _seed_duration_event(session, f"recent-{i}", days_ago=1 + i * 0.5, duration_hours=20)
    await session.commit()


async def test_norm_drift_scenario_produces_a_clarification_situation() -> None:
    async with Session() as session:
        await _reset(session)
        await _seed_drift(session)

        profile = Profile(company_id=_CO, rhythms=[_RHYTHM])
        situations = await create_norm_drift_clarifications(session, profile)
        await session.commit()

    assert len(situations) == 1
    situation = situations[0]
    assert situation.kind == "clarification"
    assert situation.rule == "norm_drift"
    assert situation.company_id == _CO
    assert situation.choices is not None
    assert {c.id for c in situation.choices} == {"recalculate", "keep", "snooze_14d"}

    async with Session() as session:
        stored = await get_situation(session, _CO, situation.id)
        assert stored is not None and stored.status == "open"

        conversation_id = await get_or_create_default_conversation(session, _CO)
        messages = await list_messages(session, _CO, conversation_id)

    artifacts = [a for m in messages for a in m.artifacts if a.get("situation_id") == situation.id]
    assert artifacts, "the same clarification must also arrive in the agent chat"
    assert artifacts[0]["type"] == "clarification"
    assert artifacts[0]["choices"]


async def test_resolving_snooze_choice_suppresses_recreation() -> None:
    async with Session() as session:
        await _reset(session)
        await _seed_drift(session)

        profile = Profile(company_id=_CO, rhythms=[_RHYTHM])
        situations = await create_norm_drift_clarifications(session, profile)
        await session.commit()
        situation_id = situations[0].id

        result = await resolve_clarification(
            session, profile, situation_id, "snooze_14d", resolved_by="tester"
        )
        await session.commit()
        assert result["status"] == "resolved"
        assert result["effect"]["effect"] == "snooze"
        assert await is_snoozed(session, _CO, situation_id)

        again = await create_norm_drift_clarifications(session, profile)
        await session.commit()

    assert again == [], "a snoozed clarification must not re-fire"
