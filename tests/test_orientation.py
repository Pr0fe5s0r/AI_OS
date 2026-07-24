from __future__ import annotations

from sqlalchemy import text

from packages.core.db import Session
from packages.core.orientation import (
    ORGANIZATION_PRIMER,
    onboarding_state,
    orientation_prompt,
)
from packages.core.profile import Profile

# The agent's day-one competence. With no seed profile a new company starts
# blank, so the engine carries a prior: what is true of ANY organization. The
# tests that matter are that the prior contains no vertical, and that the
# "where am I" answer is measured from real data rather than a stored flag that
# could drift into lying.

_CO = "test-orientation"


def _profile(**kw) -> Profile:
    base = dict(
        company_id=_CO, sources=[], things={}, links={},
        rhythms=[], watchers=[], moves={}, vocabulary={},
    )
    base.update(kw)
    return Profile(**base)


def test_the_primer_names_no_industry() -> None:
    """One primer, every company. The moment it mentions a vertical it stops
    being a prior and becomes a template."""
    lowered = ORGANIZATION_PRIMER.lower()
    for vertical in ("github", "issue", "pull request", "ticket", "zendesk",
                     "slack", "inventory", "warehouse", "sprint", "engineer"):
        assert vertical not in lowered, vertical


def test_the_primer_forbids_inventing_numbers() -> None:
    assert "never" in ORGANIZATION_PRIMER.lower()
    assert "plausible" in ORGANIZATION_PRIMER.lower()


async def _reset(session) -> None:
    await session.execute(text("DELETE FROM events WHERE company_id = :c"), {"c": _CO})
    await session.execute(text("DELETE FROM norm_baselines WHERE company_id = :c"), {"c": _CO})
    await session.commit()


async def test_a_company_with_nothing_connected_is_told_to_connect() -> None:
    async with Session() as session:
        await _reset(session)
        state = await onboarding_state(session, _CO, _profile())
    assert state["stage"] == "no_source"
    assert "connect" in state["next"].lower()


async def test_a_connected_company_with_no_records_does_not_speculate() -> None:
    async with Session() as session:
        await _reset(session)
        state = await onboarding_state(
            session, _CO, _profile(sources=[{"source": "github"}])
        )
    assert state["stage"] == "awaiting_data"
    assert "not speculate" in state["next"].lower()


async def test_a_disabled_source_does_not_count_as_connected() -> None:
    async with Session() as session:
        await _reset(session)
        state = await onboarding_state(
            session, _CO, _profile(sources=[{"source": "github", "enabled": False}])
        )
    assert state["stage"] == "no_source"


async def test_records_without_a_confirmed_shape_is_still_shaping() -> None:
    async with Session() as session:
        await _reset(session)
        await session.execute(
            text(
                """
                INSERT INTO events (id, company_id, source, type, actor_id, actor_name,
                                    timestamp, content, metadata, raw, backfilled)
                VALUES ('o1', :c, 'github', 'issue', 'x', 'x', now(), 'c',
                        '{}'::jsonb, '{}'::jsonb, false)
                ON CONFLICT DO NOTHING
                """
            ),
            {"c": _CO},
        )
        await session.commit()
        state = await onboarding_state(
            session, _CO, _profile(sources=[{"source": "github"}])
        )
        await _reset(session)

    assert state["stage"] == "shaping"
    assert state["events"] == 1


def test_the_prompt_carries_the_real_numbers() -> None:
    prompt = orientation_prompt(
        {
            "stage": "learning", "events": 42, "stable_norms": 0,
            "sources": ["github"], "state": "S", "next": "N",
        }
    )
    assert "42" in prompt and "github" in prompt
    assert "WHAT TO DO NEXT: N" in prompt
