from __future__ import annotations

from sqlalchemy import text

from apps.common.understanding import describe
from packages.core import watchers
from packages.core.db import Session
from tests.conftest import INVENTORY_PROFILE, SOFTWARE_PROFILE

# "What do you know about my company?" — the screen that makes the system
# knowable. These tests pin the two things that matter: it reports the TRUTH
# about capabilities (a note-only action must never look real), and every word
# in it comes from the profile so a warehouse doesn't get software language.


async def _wipe(company_id: str) -> None:
    async with Session() as session:
        for table in ("events", "norm_baselines", "connector_health", "credentials", "settings"):
            await session.execute(
                text(f"DELETE FROM {table} WHERE company_id = :c"), {"c": company_id}  # noqa: S608
            )
        await session.commit()


async def test_it_reports_what_it_watches_and_what_it_learned() -> None:
    profile = SOFTWARE_PROFILE.model_copy(update={"company_id": "test-understanding"})
    await _wipe(profile.company_id)

    async with Session() as session:
        await session.execute(
            text(
                """
                INSERT INTO events (id, company_id, source, type, actor_id, actor_name,
                                     timestamp, content, metadata, content_tsv)
                VALUES ('u-1', :c, 'github', 'issue', 'u', 'u', now(), 'x',
                        '{"state": "open"}'::jsonb, to_tsvector('x'))
                ON CONFLICT (company_id, id, timestamp) DO NOTHING
                """
            ),
            {"c": profile.company_id},
        )
        await session.commit()
        picture = await describe(session, profile)

    assert picture["records"]["total"] == 1
    assert {f["key"] for f in picture["records"]["by_status"]} == {"open"}
    # what "finished" means is surfaced — the thing every timing depends on
    lifecycle = {row["metric"]: row["finished_when"] for row in picture["lifecycle"]}
    assert lifecycle["issue_resolution_hours"] == "closed_at"


async def test_note_only_actions_are_never_reported_as_real() -> None:
    """The screen tells a person which buttons touch their real systems. A
    `log` move that claimed an external effect would be the same lie we just
    removed from the runtime."""
    profile = SOFTWARE_PROFILE.model_copy(update={"company_id": "test-understanding"})
    await _wipe(profile.company_id)

    async with Session() as session:
        picture = await describe(session, profile)

    by_name = {a["name"]: a for a in picture["can_do"]}
    assert by_name["page_engineer"]["external_effect"] is False
    assert by_name["draft_reply"]["external_effect"] is False
    assert by_name["apply_label"]["external_effect"] is True
    assert by_name["comment_on_pr"]["external_effect"] is True


async def test_every_word_comes_from_the_profile() -> None:
    """Same code, two companies: the screen must speak each one's language."""
    software = SOFTWARE_PROFILE.model_copy(update={"company_id": "test-understanding"})
    inventory = INVENTORY_PROFILE.model_copy(update={"company_id": "test-understanding-inv"})
    await _wipe(software.company_id)
    await _wipe(inventory.company_id)

    async with Session() as session:
        a = await describe(session, software)
        b = await describe(session, inventory)

    assert a["terms"]["thing"] == "issue" and a["terms"]["situation"] == "flag"
    assert b["terms"]["thing"] == "order" and b["terms"]["situation"] == "supply risk"
    # and the moves offered differ entirely — nothing about the screen is
    # hardcoded to one industry
    assert "page_engineer" in {x["name"] for x in a["can_do"]}
    assert "reorder_stock" in {x["name"] for x in b["can_do"]}


async def test_the_universal_checks_are_listed_for_any_company() -> None:
    """A company with no rules of its own is still protected by the built-ins,
    and the screen must say so — otherwise day one looks like nothing works."""
    bare = SOFTWARE_PROFILE.model_copy(
        update={"company_id": "test-understanding-bare", "watchers": []}
    )
    await _wipe(bare.company_id)

    async with Session() as session:
        picture = await describe(session, bare)

    assert len(picture["checks"]["universal"]) == len(watchers.catalog()) == 5
    assert picture["checks"]["profile"] == []
    # descriptions must stay domain-free so any industry can read them
    blob = " ".join(c["summary"] + c["detail"] for c in picture["checks"]["universal"]).lower()
    for word in ("issue", "repo", "github", "pull request", "ticket"):
        assert word not in blob, f"the built-in descriptions leak a domain word: {word}"


async def test_practice_mode_is_reported() -> None:
    """The safety switch has to be visible; it was previously invisible."""
    profile = SOFTWARE_PROFILE.model_copy(update={"company_id": "test-understanding"})
    await _wipe(profile.company_id)

    async with Session() as session:
        picture = await describe(session, profile)

    assert isinstance(picture["practice_mode"], bool)
