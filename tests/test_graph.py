from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from packages.core.db import Session
from packages.core.embeddings import embed
from packages.core.graph import query_graph
from packages.core.resolve import resolve
from packages.core.store import insert_event
from packages.shared.schema import Actor, Event
from verticals.software.config import ENTITY_RULES, GRAPH_SCHEMA

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


async def test_query_graph_returns_linked_cluster() -> None:
    events = [
        _ev("g-1", "github", "issue", "Checkout returns 500 at payment, see CHECKOUT-91."),
        _ev("g-2", "slack", "message", "The checkout payment failure CHECKOUT-91 is p0."),
        _ev("g-3", "zendesk", "ticket", "Customer charged but checkout failed, ref CHECKOUT-91."),
    ]

    async with Session() as session:
        for ev in events:
            vector = await asyncio.to_thread(embed, ev.content)
            await insert_event(session, ev, vector)
        await session.commit()
        for ev in events:
            await resolve(session, ev, ENTITY_RULES)
        await session.commit()

        result = await query_graph(session, _CO, "g-1", GRAPH_SCHEMA, hops=2)

    node_ids = {n.id for n in result.nodes}
    assert {"g-1", "g-2", "g-3"} <= node_ids, f"cluster missing members: {node_ids}"
    same_as = [e for e in result.edges if e.type == "SAME_AS"]
    assert same_as, "expected SAME_AS edges linking the cluster"
