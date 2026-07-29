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
# deletes nothing and reports success, so the workspace's content outlives their
# own deletion request.

_WORKSPACE = "erasure-test-agency"
_SCOPE = Scope(workspace_id=_WORKSPACE)


async def _seed() -> str:
    """One item, in both stores, plus a collection and an audit entry."""
    await graph.bootstrap()
    await graph.wipe_tenant(_WORKSPACE)

    async with Session() as session:
        await session.execute(
            text("DELETE FROM kb_items WHERE workspace_id = :t"), {"t": _WORKSPACE}
        )
        await session.execute(text("DELETE FROM collections WHERE workspace_id = :t"), {"t": _WORKSPACE})
        await session.execute(
            text("DELETE FROM companies WHERE id = :c"), {"c": _WORKSPACE}
        )
        await session.execute(
            text("INSERT INTO companies (id, name) VALUES (:c, 'Erasure Test')"),
            {"c": _WORKSPACE},
        )
        await session.execute(
            text(
                "INSERT INTO collections (workspace_id, collection_id, cluster_id, name) "
                "VALUES (:t, 'client-x', 'default', 'Client X')"
            ),
            {"t": _WORKSPACE},
        )
        await session.execute(
            text(
                "INSERT INTO audit_log (company_id, actor, action, target) "
                "VALUES (:c, 'tester', 'ingest', 'doc-1')"
            ),
            {"c": _WORKSPACE},
        )
        await session.execute(
            text(
                "INSERT INTO query_traces (trace_id, workspace_id, query, result_count, "
                "duration_ms, via) VALUES ('erasure-trace-1', :t, "
                "'what did the client say about the budget', 3, 12, 'test')"
            ),
            {"t": _WORKSPACE},
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


async def test_offboarding_removes_the_workspace_from_both_stores():
    item_id = await _seed()

    assert (await graph.count_nodes(_SCOPE)).get("total", 0) >= 1

    async with Session() as session:
        req = await create_deletion_request(session, _WORKSPACE, requested_by="tester")
        await session.commit()
        confirmed = await confirm_deletion_request(session, _WORKSPACE, req["confirmation_token"])
        assert confirmed is not None and confirmed["status"] == "queued"
        await session.commit()

        result = await delete_company(session, _WORKSPACE, req["id"])
        await session.commit()

    # The summary reports real per-table counts, not a bare "ok". This is what
    # catches a table whose workspace column was named wrong: it would delete zero
    # rows and still say it succeeded.
    assert result["postgres"]["kb_items"] == 1
    assert result["postgres"]["collections"] == 1
    assert result["postgres"]["audit_log_anonymized"] >= 1
    assert result["neo4j_nodes_removed"] >= 1

    assert (await graph.count_nodes(_SCOPE)).get("total", 0) == 0

    async with Session() as session:
        remaining = (
            await session.execute(
                text("SELECT count(*) FROM kb_items WHERE workspace_id = :t"), {"t": _WORKSPACE}
            )
        ).scalar_one()
        assert remaining == 0

        collections_left = (
            await session.execute(
                text("SELECT count(*) FROM collections WHERE workspace_id = :t"), {"t": _WORKSPACE}
            )
        ).scalar_one()
        assert collections_left == 0

        # The audit record survives, anonymised: that something happened is
        # kept for compliance even though who and what are cleared.
        actor = (
            await session.execute(
                text("SELECT actor FROM audit_log WHERE company_id = :c LIMIT 1"),
                {"c": _WORKSPACE},
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


async def test_the_questions_asked_go_with_the_answers():
    """A trace holds the query someone typed, which is frequently more
    revealing than the documents it searched. These outlived their workspace:
    a store reported fully erased still had the questions in it."""
    await _seed()

    async with Session() as session:
        req = await create_deletion_request(session, _WORKSPACE, requested_by="tester")
        await session.commit()
        await confirm_deletion_request(session, _WORKSPACE, req["confirmation_token"])
        await session.commit()
        result = await delete_company(session, _WORKSPACE, req["id"])
        await session.commit()

    assert result["postgres"]["query_traces"] == 1

    async with Session() as session:
        left = (
            await session.execute(
                text("SELECT count(*) FROM query_traces WHERE workspace_id = :t"),
                {"t": _WORKSPACE},
            )
        ).scalar_one()
        assert left == 0


async def test_an_account_left_belonging_to_nothing_is_removed():
    """Memberships cascade with the company, which used to leave an account
    holding an email address and able to sign in to nowhere. Someone who
    belongs to another workspace must be untouched."""
    await _seed()

    async with Session() as session:
        await session.execute(
            text("INSERT INTO companies (id, name) VALUES ('erasure-other', 'Other')")
        )
        for email in ("orphan@example.test", "stays@example.test"):
            await session.execute(
                text("INSERT INTO users (email, name, password_hash) VALUES (:e, 'x', 'x')"),
                {"e": email},
            )
        await session.execute(
            text(
                "INSERT INTO memberships (user_id, company_id, role) "
                "SELECT id, :c, 'owner' FROM users WHERE email = 'orphan@example.test'"
            ),
            {"c": _WORKSPACE},
        )
        # This one is in both, so the deletion must leave the account alone.
        await session.execute(
            text(
                "INSERT INTO memberships (user_id, company_id, role) "
                "SELECT id, c, 'owner' FROM users, "
                "(VALUES (:a), ('erasure-other')) AS v(c) WHERE email = 'stays@example.test'"
            ),
            {"a": _WORKSPACE},
        )
        req = await create_deletion_request(session, _WORKSPACE, requested_by="tester")
        await session.commit()
        await confirm_deletion_request(session, _WORKSPACE, req["confirmation_token"])
        await session.commit()
        result = await delete_company(session, _WORKSPACE, req["id"])
        await session.commit()

    assert result["postgres"]["orphaned_users"] >= 1

    async with Session() as session:
        emails = (
            await session.execute(
                text(
                    "SELECT email FROM users WHERE email IN "
                    "('orphan@example.test', 'stays@example.test')"
                )
            )
        ).scalars().all()
        assert "orphan@example.test" not in emails
        assert "stays@example.test" in emails  # still belongs elsewhere

        await session.execute(text("DELETE FROM memberships WHERE company_id = 'erasure-other'"))
        await session.execute(text("DELETE FROM users WHERE email = 'stays@example.test'"))
        await session.execute(text("DELETE FROM companies WHERE id = 'erasure-other'"))
        await session.commit()


async def test_erasure_does_not_reach_outside_the_workspace():
    """Removing every account with no membership would take unrelated ones
    with it — a half-finished registration, whose membership has not been
    written yet, is indistinguishable from an orphan."""
    await _seed()

    async with Session() as session:
        await session.execute(
            text("INSERT INTO users (email, name, password_hash) VALUES (:e, 'x', 'x')"),
            {"e": "signing-up@example.test"},  # no membership yet, nothing to do with us
        )
        req = await create_deletion_request(session, _WORKSPACE, requested_by="tester")
        await session.commit()
        await confirm_deletion_request(session, _WORKSPACE, req["confirmation_token"])
        await session.commit()
        await delete_company(session, _WORKSPACE, req["id"])
        await session.commit()

    async with Session() as session:
        survived = (
            await session.execute(
                text("SELECT count(*) FROM users WHERE email = 'signing-up@example.test'")
            )
        ).scalar_one()
        assert survived == 1

        await session.execute(text("DELETE FROM users WHERE email = 'signing-up@example.test'"))
        await session.commit()
