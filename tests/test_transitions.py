from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import text

from packages.core.db import Session
from packages.core.store import list_transitions, store_event
from packages.shared.schema import Actor, Event

# `events` says what is true now. This says what stopped being true.
#
# Ingest overwrites — one row per record, holding its latest snapshot — so a
# pull request that went open -> merged left no trace at all: the row simply
# read differently the next time anyone looked. "Did this move, and when?" is
# the question the whole product is built around, and it was unanswerable
# about anything but the present.

COMPANY = "test-transitions"


async def _reset() -> None:
    async with Session() as session:
        await session.execute(
            text("DELETE FROM record_transitions WHERE company_id = :c"), {"c": COMPANY}
        )
        await session.execute(text("DELETE FROM events WHERE company_id = :c"), {"c": COMPANY})
        await session.commit()


def _event(status: str, title: str = "Add README", record_id: str = "10") -> Event:
    return Event(
        id=record_id, company_id=COMPANY, source="github", type="pull_request",
        actor=Actor(id="u", name="u"),
        timestamp=datetime(2026, 7, 22, 12, 0, tzinfo=UTC),
        content=title, metadata={"state": status}, raw={},
    )


async def _store(event: Event) -> None:
    async with Session() as session:
        await store_event(session, event, status_field="state")
        await session.commit()


async def test_a_record_moving_from_open_to_merged_is_remembered() -> None:
    await _reset()
    await _store(_event("open"))
    await _store(_event("closed"))

    async with Session() as session:
        changes = await list_transitions(session, COMPANY, record_id="10")

    assert [(c["field"], c["from"], c["to"]) for c in changes] == [("status", "open", "closed")]


async def test_first_sighting_is_not_a_change() -> None:
    """An issue arriving for the first time did not "become open" — logging
    that would fill a freshly connected workspace with transitions nobody made."""
    await _reset()
    await _store(_event("open"))

    async with Session() as session:
        assert await list_transitions(session, COMPANY, record_id="10") == []


async def test_re_reading_an_unchanged_record_writes_nothing() -> None:
    """The scan runs every few minutes. A history that grew on every pass would
    be a log of our polling, not of the work."""
    await _reset()
    await _store(_event("open"))
    for _ in range(3):
        await _store(_event("open"))

    async with Session() as session:
        assert await list_transitions(session, COMPANY, record_id="10") == []


async def test_a_record_that_changes_back_records_both_moves() -> None:
    """Closed, reopened, closed again really is three changes. A unique index
    on the destination value would have swallowed the second close."""
    await _reset()
    await _store(_event("open"))
    await _store(_event("closed"))
    await _store(_event("open"))
    await _store(_event("closed"))

    async with Session() as session:
        changes = await list_transitions(session, COMPANY, record_id="10")

    assert [(c["from"], c["to"]) for c in changes] == [
        ("open", "closed"), ("closed", "open"), ("open", "closed"),
    ]


async def test_a_retitled_record_is_recorded_too() -> None:
    await _reset()
    await _store(_event("open", title="Add README"))
    await _store(_event("open", title="Add README and docs"))

    async with Session() as session:
        changes = await list_transitions(session, COMPANY, record_id="10")

    assert [(c["field"], c["from"], c["to"]) for c in changes] == [
        ("title", "Add README", "Add README and docs")
    ]


async def test_the_before_value_comes_from_the_snapshot_not_the_log() -> None:
    """The bug this replaced: reading the before-value from the transition log
    meant a record with a snapshot but no history logged 'status -> closed'
    with an empty before — a change nobody made, on every existing record at
    once, the first time this shipped."""
    await _reset()
    # a record already in the store, as every record was on that first scan
    await _store(_event("open"))
    async with Session() as session:
        await session.execute(
            text("DELETE FROM record_transitions WHERE company_id = :c"), {"c": COMPANY}
        )
        await session.commit()

    await _store(_event("closed"))

    async with Session() as session:
        changes = await list_transitions(session, COMPANY, record_id="10")

    assert len(changes) == 1
    assert changes[0]["from"] == "open"  # not None


async def test_work_moving_without_a_status_or_title_change_still_counts() -> None:
    """The case that exposed this: a pull request gained a second commit. Its
    name did not change and it was still open, so there was NO transition at
    all — and the agent, asked what had happened, answered "completely
    untouched since creation" about work that had visibly moved."""
    await _reset()
    opened = _event("open")
    opened.content = "Add README\n\nCommits: Add README"
    await _store(opened)

    pushed = _event("open")
    pushed.content = "Add README\n\nCommits: Add README; Update README with author details"
    await _store(pushed)

    async with Session() as session:
        changes = await list_transitions(session, COMPANY, record_id="10")

    assert [c["field"] for c in changes] == ["detail"]
    assert "Update README with author details" in changes[0]["to"]
    assert "Update README with author details" not in (changes[0]["from"] or "")


async def test_reformatting_alone_is_not_a_change() -> None:
    """Blank lines and trailing spaces shift for all sorts of reasons. A
    history that logged those would bury the changes that matter."""
    await _reset()
    first = _event("open")
    first.content = "Add README\n\nChanged 1 file: README.md"
    await _store(first)

    respaced = _event("open")
    respaced.content = "Add README\n\n\n   Changed 1 file: README.md   \n\n"
    await _store(respaced)

    async with Session() as session:
        assert await list_transitions(session, COMPANY, record_id="10") == []


async def test_history_is_scoped_to_one_workspace() -> None:
    await _reset()
    await _store(_event("open"))
    await _store(_event("closed"))

    async with Session() as session:
        assert await list_transitions(session, "someone-else") == []
