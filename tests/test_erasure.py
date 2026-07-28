from __future__ import annotations

import pytest
from sqlalchemy import text

from packages.core import graph
from packages.core.db import Session
from packages.core.erasure import (
    confirm_deletion_request,
    create_deletion_request,
    delete_company,
)
from packages.core.store import put_item
from packages.shared.schema import Item, Scope, SourceRef

pytestmark = pytest.mark.needs_db

# Tenant offboarding must remove everything, everywhere (NFR Data retention).
# The failure mode this guards against is quiet: a table left off the list
# deletes nothing and reports success, so the tenant's content outlives their
# own deletion request.

_TENANT = "erasure-test-agency"
_SCOPE = Scope(tenant_id=_TENANT)


async def _seed() -> str:
    """One item, in both stores, plus a brand and an audit entry."""
    await graph.bootstrap()
    await graph.wipe_tenant(_TENANT)

    async with Session() as session:
        await session.execute(
            text("DELETE FROM kb_items WHERE tenant_id = :t"), {"t": _TENANT}
        )
        await session.execute(text("DELETE FROM brands WHERE tenant_id = :t"), {"t": _TENANT})
        await session.execute(
            text("DELETE FROM companies WHERE id = :c"), {"c": _TENANT}
        )
        await session.execute(
            text("INSERT INTO companies (id, name) VALUES (:c, 'Erasure Test')"),
            {"c": _TENANT},
        )
        await session.execute(
            text(
                "INSERT INTO brands (tenant_id, brand_id, name) "
                "VALUES (:t, 'client-x', 'Client X')"
            ),
            {"t": _TENANT},
        )
        await session.execute(
            text(
                "INSERT INTO audit_log (company_id, actor, action, target) "
                "VALUES (:c, 'tester', 'ingest', 'doc-1')"
            ),
            {"c": _TENANT},
        )
        result = await put_item(
            session,
            Item(
                id="",
                scope=_SCOPE,
                title="Confidential brief",
                body="client material that must not survive offboarding",
                source=SourceRef(source="upload", locator="brief.md"),
            ),
        )
        await session.commit()

    await graph.upsert_item(
        _SCOPE, item_id=result.item.id, title=result.item.title, source="upload"
    )
    return result.item.id


async def test_offboarding_removes_the_tenant_from_both_stores():
    item_id = await _seed()

    assert (await graph.count_nodes(_SCOPE)).get("total", 0) >= 1

    async with Session() as session:
        req = await create_deletion_request(session, _TENANT, requested_by="tester")
        await session.commit()
        confirmed = await confirm_deletion_request(session, _TENANT, req["confirmation_token"])
        assert confirmed is not None and confirmed["status"] == "queued"
        await session.commit()

        result = await delete_company(session, _TENANT, req["id"])
        await session.commit()

    # The summary reports real per-table counts, not a bare "ok". This is what
    # catches a table whose tenant column was named wrong: it would delete zero
    # rows and still say it succeeded.
    assert result["postgres"]["kb_items"] == 1
    assert result["postgres"]["brands"] == 1
    assert result["postgres"]["audit_log_anonymized"] >= 1
    assert result["neo4j_nodes_removed"] >= 1

    assert (await graph.count_nodes(_SCOPE)).get("total", 0) == 0

    async with Session() as session:
        remaining = (
            await session.execute(
                text("SELECT count(*) FROM kb_items WHERE tenant_id = :t"), {"t": _TENANT}
            )
        ).scalar_one()
        assert remaining == 0

        brands = (
            await session.execute(
                text("SELECT count(*) FROM brands WHERE tenant_id = :t"), {"t": _TENANT}
            )
        ).scalar_one()
        assert brands == 0

        # The audit record survives, anonymised: that something happened is
        # kept for compliance even though who and what are cleared.
        actor = (
            await session.execute(
                text("SELECT actor FROM audit_log WHERE company_id = :c LIMIT 1"),
                {"c": _TENANT},
            )
        ).scalar_one_or_none()
        assert actor == "[deleted]"

    # And the item itself is unreachable by id.
    async with Session() as session:
        gone = (
            await session.execute(
                text("SELECT count(*) FROM kb_items WHERE item_id = :i"), {"i": item_id}
            )
        ).scalar_one()
        assert gone == 0
