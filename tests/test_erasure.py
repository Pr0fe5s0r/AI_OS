from __future__ import annotations

import pytest
from sqlalchemy import text

from packages.core import graph
from packages.core.db import Session
from packages.core.erasure import (
    confirm_deletion_request,
    create_deletion_request,
    delete_company,
    get_deletion_request,
)

pytestmark = pytest.mark.needs_db

# Checkpoint 6, part C: delete_company must remove real rows from BOTH stores
# and leave a deletion_requests row that proves what happened, not just
# return a success flag.

_CO = "test-erasure"


async def _seed_postgres(session) -> int:
    await session.execute(text("DELETE FROM deletion_requests WHERE company_id = :c"), {"c": _CO})
    await session.execute(
        text("INSERT INTO companies (id, name) VALUES (:c, 'Erasure Test') ON CONFLICT (id) DO NOTHING"),
        {"c": _CO},
    )
    await session.execute(
        text(
            """
            INSERT INTO events (id, company_id, source, type, actor_id, actor_name,
                                 timestamp, content, content_tsv)
            VALUES ('era-1', :c, 'github', 'issue', 'u', 'u', now(), 'x', to_tsvector('x'))
            ON CONFLICT (company_id, id, timestamp) DO NOTHING
            """
        ),
        {"c": _CO},
    )
    await session.execute(
        text(
            """
            INSERT INTO situations (id, company_id, rule, severity, title, status, created_at)
            VALUES ('era-sit-1', :c, 'r', 'low', 't', 'open', now())
            ON CONFLICT (id) DO NOTHING
            """
        ),
        {"c": _CO},
    )
    await session.execute(
        text(
            """
            INSERT INTO connector_health (connector_type, company_id, status)
            VALUES ('github', :c, 'healthy')
            ON CONFLICT (connector_type, company_id) DO NOTHING
            """
        ),
        {"c": _CO},
    )
    await session.execute(
        text(
            """
            INSERT INTO credentials (company_id, source, sealed_token)
            VALUES (:c, 'github', 'sealed')
            ON CONFLICT (company_id, source) DO NOTHING
            """
        ),
        {"c": _CO},
    )
    await session.execute(
        text("INSERT INTO audit_log (company_id, actor, action, target) VALUES (:c, 'tester', 'test.seeded', 'era-1')"),
        {"c": _CO},
    )
    conversation_id = (
        await session.execute(
            text("INSERT INTO conversations (company_id, title) VALUES (:c, 'Agent') RETURNING id"),
            {"c": _CO},
        )
    ).scalar_one()
    await session.execute(
        text("INSERT INTO messages (conversation_id, role, content) VALUES (:cid, 'user', 'hello')"),
        {"cid": conversation_id},
    )
    await session.commit()
    return int(conversation_id)


async def test_delete_company_removes_rows_from_neo4j_and_postgres_and_completes() -> None:
    await graph.bootstrap()
    await graph.wipe_company(_CO)
    await graph.upsert_thing(_CO, "era-thing-1", "Incident", "an incident to erase")

    async with Session() as session:
        conversation_id = await _seed_postgres(session)

        req = await create_deletion_request(session, _CO, requested_by="tester")
        await session.commit()
        confirmed = await confirm_deletion_request(session, _CO, req["confirmation_token"])
        assert confirmed is not None and confirmed["status"] == "queued"
        await session.commit()

        result = await delete_company(session, _CO, req["id"])
        await session.commit()

    # ---- summary reflects real per-table row counts, not just "ok" ----
    assert result["postgres"]["events"] == 1
    assert result["postgres"]["situations"] == 1
    assert result["postgres"]["connector_health"] == 1
    assert result["postgres"]["credentials"] == 1
    assert result["postgres"]["conversations"] == 1
    assert result["postgres"]["companies"] == 1
    assert result["postgres"]["audit_log_anonymized"] >= 1
    assert result["neo4j_nodes_removed"] >= 1

    # ---- Neo4j is actually empty for this company ----
    counts = await graph.count_nodes(_CO)
    assert counts.get("total", 0) == 0

    # ---- Postgres is actually empty for this company ----
    async with Session() as session:
        n_events = (
            await session.execute(text("SELECT count(*) FROM events WHERE company_id = :c"), {"c": _CO})
        ).scalar_one()
        assert n_events == 0

        n_messages = (
            await session.execute(
                text("SELECT count(*) FROM messages WHERE conversation_id = :cid"), {"cid": conversation_id}
            )
        ).scalar_one()
        assert n_messages == 0, "messages must cascade when their conversation is deleted"

        n_companies = (
            await session.execute(text("SELECT count(*) FROM companies WHERE id = :c"), {"c": _CO})
        ).scalar_one()
        assert n_companies == 0

        # audit_log is ANONYMIZED, not deleted: the fact + timestamp survive
        audit_row = (
            await session.execute(
                text("SELECT actor, target FROM audit_log WHERE company_id = :c AND action = 'test.seeded'"),
                {"c": _CO},
            )
        ).first()
        assert audit_row is not None
        assert audit_row.actor == "[deleted]"
        assert audit_row.target == ""

        # ---- deletion_requests proves what happened, and it's resumable-safe ----
        final = await get_deletion_request(session, req["id"])
        assert final is not None
        assert final["status"] == "completed"
        assert final["neo4j_done"] is True
        assert final["redis_done"] is True
        assert final["postgres_done"] is True
        assert final["completed_at"] is not None
        assert final["error"] is None
