from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from packages.core import graph
from packages.core.db import Session
from packages.core.embeddings import embed
from packages.core.resolve import resolve
from packages.core.store import store_event
from packages.shared.schema import Actor, Event
from tests.conftest import SOFTWARE_PROFILE

_CO = "test-graph"


def _ev(eid: str, source: str, type_: str, content: str) -> Event:
    return Event(
        id=eid,
        company_id=_CO,
        source=source,
        type=type_,
        actor=Actor(id=f"u_{eid}", name=f"User {eid}"),
        timestamp=datetime(2026, 7, 9, 12, 0, tzinfo=UTC),
        content=content,
        metadata={},
    )


async def _seed(session, ev: Event) -> None:
    vector = await asyncio.to_thread(embed, ev.content)
    await store_event(session, ev)
    await graph.mirror_event(ev.company_id, ev.id, ev.timestamp, ev.source, vector)


async def test_query_graph_returns_linked_cluster() -> None:
    await graph.bootstrap()
    await graph.wipe_company(_CO)  # re-runnable

    events = [
        _ev("g-1", "github", "issue", "Checkout returns 500 at payment, see CHECKOUT-91."),
        _ev("g-2", "slack", "message", "The checkout payment failure CHECKOUT-91 is p0."),
        _ev("g-3", "zendesk", "ticket", "Customer charged but checkout failed, ref CHECKOUT-91."),
    ]

    async with Session() as session:
        for ev in events:
            await _seed(session, ev)
        await session.commit()
        for ev in events:
            await resolve(session, ev, SOFTWARE_PROFILE.things, SOFTWARE_PROFILE.links)
        await session.commit()

    result = await graph.query_graph(_CO, "g-1", hops=2)

    node_ids = {n.id for n in result.nodes}
    assert {"g-1", "g-2", "g-3"} <= node_ids, f"cluster missing members: {node_ids}"
    same_as = [e for e in result.edges if e.type == "SAME_AS"]
    assert same_as, "expected SAME_AS links joining the cluster"


async def test_every_query_is_company_scoped() -> None:
    """Another company's graph must be invisible, even at 2 hops."""
    await graph.bootstrap()
    other = "test-graph-other"
    await graph.wipe_company(other)
    await graph.upsert_thing(other, "other-1", "Incident", "their incident")

    result = await graph.query_graph(_CO, "g-1", hops=2)
    assert all(n.id != "other-1" for n in result.nodes)

    counts = await graph.count_nodes(other)
    assert counts.get("Thing", 0) == 1
