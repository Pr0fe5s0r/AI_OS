from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from packages.core.db import Session
from packages.core.items import item_facets, list_items

# The "what have I got?" view. The point of these tests is that the status
# facet is driven by PROFILE data (things.status_field), so the same code
# serves a GitHub profile (status lives in `state`) and an inventory profile
# (status lives in `status`) with nothing hardcoded either way.

_CO = "test-items"


async def _seed(session) -> None:
    await session.execute(text("DELETE FROM events WHERE company_id = :c"), {"c": _CO})
    now = datetime.now(UTC)
    rows = [
        # id,        source,   type,           metadata,                       backfilled
        ("i-1", "github", "issue", {"state": "open"}, False),
        ("i-2", "github", "issue", {"state": "open"}, False),
        ("i-3", "github", "issue", {"state": "closed"}, False),
        ("i-4", "github", "pull_request", {"state": "open"}, False),
        ("i-5", "zendesk", "ticket", {"state": "closed"}, False),
        # history still counts as "what I've got", unlike for the watchers
        ("i-6", "github", "issue", {"state": "closed"}, True),
    ]
    for i, (eid, source, type_, md, backfilled) in enumerate(rows):
        await session.execute(
            text(
                """
                INSERT INTO events (id, company_id, source, type, actor_id, actor_name,
                                     timestamp, content, metadata, content_tsv, backfilled)
                VALUES (:id, :c, :s, :t, 'u', 'u', :ts, :content, CAST(:md AS jsonb),
                        to_tsvector('english', :content), :bf)
                ON CONFLICT (id, timestamp) DO NOTHING
                """
            ),
            {
                "id": eid, "c": _CO, "s": source, "t": type_,
                "ts": now - timedelta(hours=i), "content": f"Item {eid}\n\nbody",
                "md": json.dumps(md), "bf": backfilled,
            },
        )
    await session.commit()


async def test_facets_count_every_dimension() -> None:
    async with Session() as session:
        await _seed(session)
        facets = await item_facets(session, _CO, status_field="state")

    assert {f["key"]: f["count"] for f in facets["sources"]} == {"github": 5, "zendesk": 1}
    assert {f["key"]: f["count"] for f in facets["types"]} == {
        "issue": 4, "pull_request": 1, "ticket": 1,
    }
    assert {f["key"]: f["count"] for f in facets["statuses"]} == {"open": 3, "closed": 3}


async def test_a_facets_counts_respect_other_filters_but_not_its_own() -> None:
    """Picking "open" must not collapse the status counts to just open — you
    still need to see how many closed items you'd get by switching."""
    async with Session() as session:
        await _seed(session)
        facets = await item_facets(session, _CO, status_field="state", status="open")

    # status facet ignores its own filter: both values still visible
    assert {f["key"]: f["count"] for f in facets["statuses"]} == {"open": 3, "closed": 3}
    # but the other facets DO narrow to open items only
    assert {f["key"]: f["count"] for f in facets["sources"]} == {"github": 3}


async def test_filtering_returns_only_matching_items() -> None:
    async with Session() as session:
        await _seed(session)
        open_issues = await list_items(session, _CO, status_field="state", type_="issue", status="open")
        zendesk = await list_items(session, _CO, status_field="state", source="zendesk")

    assert {i["id"] for i in open_issues} == {"i-1", "i-2"}
    assert all(i["status"] == "open" and i["type"] == "issue" for i in open_issues)
    assert {i["id"] for i in zendesk} == {"i-5"}


async def test_history_is_included_here_even_though_watchers_ignore_it() -> None:
    """Backfilled events must never raise fresh alerts, but they ARE part of
    "what have I got" — so this view counts them."""
    async with Session() as session:
        await _seed(session)
        every = await list_items(session, _CO, status_field="state")

    assert "i-6" in {i["id"] for i in every}
    assert next(i for i in every if i["id"] == "i-6")["backfilled"] is True


async def test_status_field_comes_from_the_profile_not_a_hardcoded_word() -> None:
    """The same rows read through a profile whose status lives in a DIFFERENT
    key must produce a different (here: empty) status facet — proving the
    field name is data, not an assumption about GitHub."""
    async with Session() as session:
        await _seed(session)
        as_github = await item_facets(session, _CO, status_field="state")
        as_inventory = await item_facets(session, _CO, status_field="status")

    assert as_github["statuses"], "github profile reads status from `state`"
    assert as_inventory["statuses"] == [], "an inventory profile finds nothing in `status` here"
    # …and the non-status facets are unaffected either way
    assert as_github["types"] == as_inventory["types"]


async def test_a_profile_with_no_status_concept_gets_no_status_facet() -> None:
    async with Session() as session:
        await _seed(session)
        facets = await item_facets(session, _CO, status_field=None)
        rows = await list_items(session, _CO, status_field=None)

    assert facets["statuses"] == []
    assert all(i["status"] is None for i in rows)
    assert len(rows) == 6  # everything still lists


async def test_title_falls_back_to_the_id_when_there_is_no_content() -> None:
    async with Session() as session:
        await session.execute(text("DELETE FROM events WHERE company_id = :c"), {"c": _CO})
        await session.execute(
            text(
                """
                INSERT INTO events (id, company_id, source, type, actor_id, actor_name,
                                     timestamp, content, content_tsv)
                VALUES ('blank-1', :c, 'github', 'issue', 'u', 'u', now(), '', to_tsvector(''))
                ON CONFLICT (id, timestamp) DO NOTHING
                """
            ),
            {"c": _CO},
        )
        await session.commit()
        rows = await list_items(session, _CO, status_field="state")

    assert rows[0]["title"] == "blank-1"
