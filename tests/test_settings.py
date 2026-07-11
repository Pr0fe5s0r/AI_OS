from __future__ import annotations

from packages.core.db import Session
from packages.core.settings import get_setting, set_setting
from verticals.software.config import (
    APPROVAL_DEFAULTS,
    DRY_RUN_KEY,
    approval_policy,
    save_team_roster,
    team_roster,
)

_CO = "test-settings"


async def test_unset_setting_falls_back_to_the_default() -> None:
    async with Session() as session:
        assert await get_setting(session, _CO, "never-set", "fallback") == "fallback"


async def test_toggle_persists_and_changes_the_live_policy() -> None:
    async with Session() as session:
        # default: practice mode
        policy = await approval_policy(session, _CO)
        assert policy["dry_run"] is APPROVAL_DEFAULTS["dry_run"] is True

        # operator turns practice mode off -> the runtime goes live
        await set_setting(session, _CO, DRY_RUN_KEY, False)
        await session.commit()
        assert (await approval_policy(session, _CO))["dry_run"] is False

        # and back on again
        await set_setting(session, _CO, DRY_RUN_KEY, True)
        await session.commit()
        assert (await approval_policy(session, _CO))["dry_run"] is True


async def test_team_roster_saved_from_the_ui_is_what_the_agent_reads() -> None:
    member = {
        "id": "sam", "name": "Sam", "email": "sam@x.com",
        "roles": ["engineer"], "skills": ["python"],
        "max_open_issues": 4, "assignable": True,
    }
    async with Session() as session:
        # this test is re-runnable: start from a known-empty roster
        await save_team_roster(session, [], "test-team")
        await session.commit()
        assert await team_roster(session, "test-team") == []  # no team configured yet

        await save_team_roster(session, [member], "test-team")
        await session.commit()

        roster = await team_roster(session, "test-team")
        assert roster == [member]
        # and it is company-scoped
        assert await team_roster(session, "test-team-other") == []


async def test_setting_is_company_scoped() -> None:
    async with Session() as session:
        await set_setting(session, "test-settings-a", DRY_RUN_KEY, False)
        await session.commit()
        assert (await approval_policy(session, "test-settings-a"))["dry_run"] is False
        # a different company is unaffected
        assert (await approval_policy(session, "test-settings-b"))["dry_run"] is True
