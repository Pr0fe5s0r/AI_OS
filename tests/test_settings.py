from __future__ import annotations

from apps.common.context import (
    DRY_RUN_KEY,
    approval_policy,
    save_team_roster,
    team_roster,
)
from packages.core.db import Session
from packages.core.profile import Profile
from packages.core.settings import get_setting, set_setting
from tests.conftest import SOFTWARE_PROFILE


def _profile_for(company_id: str) -> Profile:
    """The software profile re-homed to a test company (policy comes from slots)."""
    return SOFTWARE_PROFILE.model_copy(update={"company_id": company_id})


async def test_unset_setting_falls_back_to_the_default() -> None:
    async with Session() as session:
        assert await get_setting(session, "test-settings", "never-set", "fallback") == "fallback"


async def test_toggle_persists_and_changes_the_live_policy() -> None:
    profile = _profile_for("test-settings")
    defaults = profile.moves["approval_defaults"]
    async with Session() as session:
        # a fresh company starts in practice mode (profile default)
        await set_setting(session, profile.company_id, DRY_RUN_KEY, defaults["dry_run"])
        await session.commit()
        policy = await approval_policy(session, profile)
        assert policy["dry_run"] is defaults["dry_run"] is True

        # operator turns practice mode off -> the runtime goes live
        await set_setting(session, profile.company_id, DRY_RUN_KEY, False)
        await session.commit()
        assert (await approval_policy(session, profile))["dry_run"] is False

        # and back on again
        await set_setting(session, profile.company_id, DRY_RUN_KEY, True)
        await session.commit()
        assert (await approval_policy(session, profile))["dry_run"] is True


async def test_team_roster_saved_from_the_ui_is_what_the_agent_reads() -> None:
    member = {
        "id": "sam", "name": "Sam", "email": "sam@x.com",
        "roles": ["engineer"], "skills": ["python"],
        "max_open_issues": 4, "assignable": True,
    }
    async with Session() as session:
        # this test is re-runnable: start from a known-empty roster
        await save_team_roster(session, "test-team", [])
        await session.commit()
        assert await team_roster(session, "test-team") == []  # no team configured yet

        await save_team_roster(session, "test-team", [member])
        await session.commit()

        roster = await team_roster(session, "test-team")
        assert roster == [member]
        # and it is company-scoped
        assert await team_roster(session, "test-team-other") == []


async def test_setting_is_company_scoped() -> None:
    async with Session() as session:
        await set_setting(session, "test-settings-a", DRY_RUN_KEY, False)
        await session.commit()
        assert (await approval_policy(session, _profile_for("test-settings-a")))["dry_run"] is False
        # a different company is unaffected (falls back to the profile default: True)
        assert (await approval_policy(session, _profile_for("test-settings-b")))["dry_run"] is True
