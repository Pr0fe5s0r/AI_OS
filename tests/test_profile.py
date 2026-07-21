from __future__ import annotations

import asyncio

from sqlalchemy import text

from packages.core.db import Session
from packages.core.norms import get_norm_reset
from packages.core.profile import Profile, load_profile, save_profile

# A changed rhythm carries a NEW definition of "finished" — confirming a
# version whose rhythm.end_field/source/type differs from what was last
# confirmed must reset that metric's baseline automatically, or old history
# measured under the old meaning blends silently with the new one.

_CO = "test-profile-rhythm-reset"


def _profile(rhythms: list[dict]) -> Profile:
    return Profile(company_id=_CO, sources=[], things={}, links={}, rhythms=rhythms,
                    watchers=[], moves={}, vocabulary={})


async def test_changing_a_rhythms_end_field_resets_its_baseline() -> None:
    async with Session() as session:
        await session.execute(text("DELETE FROM profiles WHERE company_id = :c"), {"c": _CO})
        await session.execute(text("DELETE FROM norm_resets WHERE company_id = :c"), {"c": _CO})
        await session.commit()

        original = _profile([
            {"name": "issue_resolution_hours", "source": "github", "type": "issue",
             "end_field": "closed_at", "unit": "hours", "window_days": 90},
        ])
        await save_profile(session, original, status="confirmed")
        await session.commit()
        assert await get_norm_reset(session, _CO, "issue_resolution_hours") is None

        changed = _profile([
            # same name, different meaning of "finished"
            {"name": "issue_resolution_hours", "source": "github", "type": "issue",
             "end_field": "resolved_at", "unit": "hours", "window_days": 90},
        ])
        await save_profile(session, changed, status="confirmed")
        await session.commit()

        assert await get_norm_reset(session, _CO, "issue_resolution_hours") is not None


async def test_unchanged_rhythm_is_not_reset() -> None:
    async with Session() as session:
        await session.execute(text("DELETE FROM profiles WHERE company_id = :c"), {"c": _CO})
        await session.execute(text("DELETE FROM norm_resets WHERE company_id = :c"), {"c": _CO})
        await session.commit()

        rhythms = [{"name": "pr_review_hours", "source": "github", "type": "pull_request",
                    "end_field": "merged_at", "unit": "hours", "window_days": 90}]
        await save_profile(session, _profile(rhythms), status="confirmed")
        await session.commit()

        # a second confirm with the SAME rhythm definition (e.g. an unrelated
        # source toggle) must not touch this metric's history
        await save_profile(session, _profile(rhythms), status="confirmed")
        await session.commit()

        assert await get_norm_reset(session, _CO, "pr_review_hours") is None


async def test_brand_new_rhythm_is_not_reset() -> None:
    async with Session() as session:
        await session.execute(text("DELETE FROM profiles WHERE company_id = :c"), {"c": _CO})
        await session.execute(text("DELETE FROM norm_resets WHERE company_id = :c"), {"c": _CO})
        await session.commit()

        await save_profile(session, _profile([]), status="confirmed")
        await session.commit()

        with_new_rhythm = _profile([
            {"name": "ticket_resolution_hours", "source": "zendesk", "type": "ticket",
             "end_field": "resolved_at", "unit": "hours", "window_days": 90},
        ])
        await save_profile(session, with_new_rhythm, status="confirmed")
        await session.commit()

        # nothing existed before under a different meaning, so nothing to reset
        assert await get_norm_reset(session, _CO, "ticket_resolution_hours") is None


async def test_proposed_status_never_triggers_a_reset() -> None:
    async with Session() as session:
        await session.execute(text("DELETE FROM profiles WHERE company_id = :c"), {"c": _CO})
        await session.execute(text("DELETE FROM norm_resets WHERE company_id = :c"), {"c": _CO})
        await session.commit()

        original = _profile([
            {"name": "issue_resolution_hours", "source": "github", "type": "issue",
             "end_field": "closed_at", "unit": "hours", "window_days": 90},
        ])
        await save_profile(session, original, status="confirmed")
        await session.commit()

        changed = _profile([
            {"name": "issue_resolution_hours", "source": "github", "type": "issue",
             "end_field": "resolved_at", "unit": "hours", "window_days": 90},
        ])
        # a PROPOSAL isn't live yet — it must not touch a real baseline
        await save_profile(session, changed, status="proposed")
        await session.commit()

        assert await get_norm_reset(session, _CO, "issue_resolution_hours") is None


# ------------------- overlapping confirms must not race (see profile.py) -------------------


async def test_overlapping_confirms_serialize_no_lost_versions_or_resets() -> None:
    """Regression test: two confirms for the same company landing at the same
    moment must not both compute the same next_version (only the unique
    constraint would catch that, and the loser's whole confirm — including
    its reset — would be silently dropped with an unhandled error). The
    advisory lock in save_profile() must make the second one wait and diff
    against the FIRST one's committed result instead."""
    async with Session() as session:
        await session.execute(text("DELETE FROM profiles WHERE company_id = :c"), {"c": _CO})
        await session.execute(text("DELETE FROM norm_resets WHERE company_id = :c"), {"c": _CO})
        await session.commit()

        base = _profile([
            {"name": "issue_resolution_hours", "source": "github", "type": "issue",
             "end_field": "closed_at", "unit": "hours", "window_days": 90},
        ])
        await save_profile(session, base, status="confirmed")
        await session.commit()

    async def _confirm(end_field: str) -> int:
        async with Session() as s:
            profile = _profile([
                {"name": "issue_resolution_hours", "source": "github", "type": "issue",
                 "end_field": end_field, "unit": "hours", "window_days": 90},
            ])
            version = await save_profile(s, profile, status="confirmed")
            await s.commit()
            return version

    versions = await asyncio.gather(_confirm("resolved_at"), _confirm("done_at"))

    assert sorted(versions) == [2, 3], "both confirms must land as distinct versions, not collide"

    async with Session() as session:
        latest = await load_profile(session, _CO)
        assert latest is not None and latest.version == 3
        # the metric was reset by whichever confirm actually changed it —
        # never silently dropped by the race
        assert await get_norm_reset(session, _CO, "issue_resolution_hours") is not None
