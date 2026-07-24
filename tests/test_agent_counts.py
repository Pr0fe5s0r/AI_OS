from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from apps.common.assistant_tools import _make_dispatch
from packages.core.db import Session
from packages.core.profile import Profile

# The agent must be able to COUNT.
#
# list_work read item_facets under the singular keys (source/type/status) while
# item_facets returns them plural (sources/types/statuses). So the model got
# total=0 and every breakdown empty, and fell back to eyeballing the capped
# records list — which is why it answered "9 issues" one moment and "4 pull
# requests" the next. The counts were always computed; they just never reached
# the model. This pins the wiring so a person asking "how many issues?" gets a
# real number, not a guess.

_CO = "test-agent-counts"


async def _seed(session) -> None:
    await session.execute(text("DELETE FROM events WHERE company_id = :c"), {"c": _CO})
    now = datetime.now(UTC)
    # 9 issues (5 closed, 4 open) + 1 pull request (open) — the exact shape that
    # was miscounted in the wild
    rows = [
        ("i-1", "issue", "open"), ("i-2", "issue", "open"),
        ("i-3", "issue", "open"), ("i-4", "issue", "open"),
        ("i-5", "issue", "closed"), ("i-6", "issue", "closed"),
        ("i-7", "issue", "closed"), ("i-8", "issue", "closed"),
        ("i-9", "issue", "closed"), ("pr-1", "pull_request", "open"),
    ]
    for i, (eid, type_, state) in enumerate(rows):
        await session.execute(
            text(
                """
                INSERT INTO events (id, company_id, source, type, actor_id, actor_name,
                                     timestamp, content, metadata, content_tsv, backfilled)
                VALUES (:id, :c, 'github', :t, 'u', 'u', :ts, :content, CAST(:md AS jsonb),
                        to_tsvector('english', :content), false)
                ON CONFLICT (company_id, id, timestamp) DO NOTHING
                """
            ),
            {
                "id": eid, "c": _CO, "t": type_, "ts": now - timedelta(hours=i),
                "content": f"Item {eid}", "md": json.dumps({"state": state}),
            },
        )
    await session.commit()


def _profile() -> Profile:
    return Profile(company_id=_CO, things={"status_field": "state"})


async def test_list_work_reports_real_counts_not_empty_facets() -> None:
    async with Session() as session:
        await _seed(session)
        dispatch = _make_dispatch(session, _profile())
        result = await dispatch("list_work", {})

    # the bug: every one of these came back empty / zero
    assert result["total"] == 10
    by_type = {f["key"]: f["count"] for f in result["by_type"]}
    by_status = {f["key"]: f["count"] for f in result["by_status"]}
    assert by_type == {"issue": 9, "pull_request": 1}
    assert by_status == {"open": 5, "closed": 5}


async def test_a_status_filter_still_narrows_the_counts() -> None:
    """Counts must reflect the filter the model asked for, not the whole set —
    otherwise 'how many open PRs' can't be answered from a filtered call."""
    async with Session() as session:
        await _seed(session)
        dispatch = _make_dispatch(session, _profile())
        result = await dispatch("list_work", {"status": "open"})

    assert result["total"] == 5
    assert len(result["records"]) == 5
    assert all(r["status"] == "open" for r in result["records"])
