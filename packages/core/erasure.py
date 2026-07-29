from __future__ import annotations

import json
import secrets
from typing import Any, cast

from sqlalchemy import text
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core import graph

# Data deletion (checkpoint 6, part C): one system of record for erasure.
#
# delete_company() fans out in a specific order: Neo4j and Redis hold DERIVED
# state (the graph mirror + vectors, and transient job/debounce keys) that is
# safe to lose first; Postgres is the system of record and goes LAST, so if
# anything upstream fails, the source of truth is still intact and the
# deletion_requests row shows exactly what did and didn't finish — nothing is
# silently half-done.
#
# Every company-scoped Postgres table is deleted outright except audit_log,
# which is anonymized by default (actor/target/metadata cleared, the fact +
# timestamp kept) rather than deleted — a compliance record that something
# happened survives even though who/what did not. Callers may request a hard
# "purge" of audit_log instead via ``audit_policy``.

# Every table that carries workspace data, with the column naming the workspace.
# Offboarding must leave nothing behind (NFR Data retention), so this mapping
# is the one place a new workspace-scoped table has to be registered — missing one
# means a workspace's content survives their own deletion request.
#
# The column differs by vintage: the KB's own tables say `workspace_id`, while the
# platform tables it inherited say `company_id`. Carried as data rather than
# assumed, because assuming it silently deleted nothing at all.
_TABLES: tuple[tuple[str, str], ...] = (
    ("kb_item_classes", "workspace_id"),
    ("kb_classes", "workspace_id"),
    ("kb_items", "workspace_id"),
    # A trace holds the query someone typed, which is frequently more
    # revealing than the documents it searched. Leaving these behind meant a
    # workspace could be reported fully erased while the questions it asked
    # were still readable in the table.
    ("query_traces", "workspace_id"),
    ("collections", "workspace_id"),
    ("clusters", "workspace_id"),
    ("api_keys", "workspace_id"),
    ("settings", "company_id"),
    ("action_tokens", "company_id"),
    ("credentials", "company_id"),
)


def _row_to_dict(r: Any) -> dict[str, Any]:
    return {
        "id": r.id,
        "company_id": r.company_id,
        "status": r.status,
        "confirmation_token": r.confirmation_token,
        "audit_policy": r.audit_policy,
        "requested_by": r.requested_by,
        "requested_at": r.requested_at,
        "confirmed_at": r.confirmed_at,
        "completed_at": r.completed_at,
        "neo4j_done": r.neo4j_done,
        "redis_done": r.redis_done,
        "postgres_done": r.postgres_done,
        "error": r.error,
        "result": r.result if isinstance(r.result, dict) else json.loads(r.result),
    }


_COLUMNS = (
    "id, company_id, status, confirmation_token, audit_policy, requested_by, "
    "requested_at, confirmed_at, completed_at, neo4j_done, redis_done, postgres_done, "
    "error, result"
)


# --------------------------- deletion_requests CRUD ---------------------------


async def create_deletion_request(
    session: AsyncSession, company_id: str, requested_by: str, audit_policy: str = "anonymize"
) -> dict[str, Any]:
    token = secrets.token_urlsafe(16)
    row = (
        await session.execute(
            text(
                f"""
                INSERT INTO deletion_requests (company_id, confirmation_token, audit_policy, requested_by)
                VALUES (:c, :token, :policy, :by)
                RETURNING {_COLUMNS}
                """
            ),
            {"c": company_id, "token": token, "policy": audit_policy, "by": requested_by},
        )
    ).one()
    return _row_to_dict(row)


async def get_deletion_request(session: AsyncSession, request_id: int) -> dict[str, Any] | None:
    row = (
        await session.execute(
            text(f"SELECT {_COLUMNS} FROM deletion_requests WHERE id = :id"), {"id": request_id}
        )
    ).first()
    return _row_to_dict(row) if row is not None else None


async def get_latest_deletion_request(session: AsyncSession, company_id: str) -> dict[str, Any] | None:
    row = (
        await session.execute(
            text(
                f"""
                SELECT {_COLUMNS} FROM deletion_requests
                WHERE company_id = :c ORDER BY requested_at DESC LIMIT 1
                """
            ),
            {"c": company_id},
        )
    ).first()
    return _row_to_dict(row) if row is not None else None


async def confirm_deletion_request(
    session: AsyncSession, company_id: str, token: str
) -> dict[str, Any] | None:
    """The second call of the two-step confirmation. Only a matching, still-
    pending request for this company can be confirmed — a stale or wrong
    token, or a request that's already running, is refused."""
    row = (
        await session.execute(
            text(
                f"""
                UPDATE deletion_requests
                SET status = 'queued', confirmed_at = now()
                WHERE company_id = :c AND confirmation_token = :token AND status = 'pending_confirmation'
                RETURNING {_COLUMNS}
                """
            ),
            {"c": company_id, "token": token},
        )
    ).first()
    return _row_to_dict(row) if row is not None else None


async def _set_status(
    session: AsyncSession, request_id: int, status: str, error: str | None = None
) -> None:
    await session.execute(
        text("UPDATE deletion_requests SET status = :s, error = :e WHERE id = :id"),
        {"s": status, "e": error, "id": request_id},
    )


async def _mark_store_done(session: AsyncSession, request_id: int, column: str) -> None:
    await session.execute(
        text(f"UPDATE deletion_requests SET {column} = true WHERE id = :id"),  # noqa: S608 (column is one of 3 fixed literals below, never user input)
        {"id": request_id},
    )


async def _complete(session: AsyncSession, request_id: int, result: dict) -> None:
    await session.execute(
        text(
            """
            UPDATE deletion_requests
            SET status = 'completed', completed_at = now(), result = CAST(:r AS jsonb)
            WHERE id = :id
            """
        ),
        {"id": request_id, "r": json.dumps(result)},
    )


# ------------------------------- the fan-out -------------------------------


async def _delete_neo4j(company_id: str) -> int:
    """Covers the graph AND its embeddings — vectors live on :Event nodes,
    not a separate store."""
    return await graph.wipe_tenant(company_id)


async def _delete_redis(company_id: str) -> int:
    from arq import create_pool

    from packages.core.pipeline import redis_settings

    pool = await create_pool(redis_settings())
    removed = 0
    try:
        async for key in pool.scan_iter(match=f"*{company_id}*"):
            await pool.delete(key)
            removed += 1
    finally:
        await pool.aclose()
    return removed


async def _anonymize_audit_log(session: AsyncSession, company_id: str) -> int:
    result = await session.execute(
        text(
            """
            UPDATE audit_log
            SET actor = '[deleted]', target = '', metadata = '{}'::jsonb
            WHERE company_id = :c AND actor <> '[deleted]'
            """
        ),
        {"c": company_id},
    )
    return int(cast(CursorResult, result).rowcount or 0)


async def _delete_postgres(session: AsyncSession, company_id: str, audit_policy: str) -> dict[str, int]:
    counts: dict[str, int] = {}

    # Read before the company row goes: deleting it cascades the memberships,
    # and after that there is no way to tell which accounts belonged to it.
    members = list(
        (
            await session.execute(
                text("SELECT user_id FROM memberships WHERE company_id = :c"), {"c": company_id}
            )
        ).scalars()
    )

    for table, column in _TABLES:
        result = await session.execute(
            text(f"DELETE FROM {table} WHERE {column} = :c"),  # noqa: S608 (table and column come from the fixed _TABLES tuple, never user input)
            {"c": company_id},
        )
        counts[table] = int(cast(CursorResult, result).rowcount or 0)

    if audit_policy == "purge":
        result = await session.execute(text("DELETE FROM audit_log WHERE company_id = :c"), {"c": company_id})
        counts["audit_log_purged"] = int(cast(CursorResult, result).rowcount or 0)
    else:
        counts["audit_log_anonymized"] = await _anonymize_audit_log(session, company_id)

    result = await session.execute(text("DELETE FROM companies WHERE id = :c"), {"c": company_id})
    counts["companies"] = int(cast(CursorResult, result).rowcount or 0)

    # Memberships go with the company by cascade, which can leave an account
    # belonging to nothing — still holding an email address, still able to
    # sign in, with no workspace to sign in to.
    #
    # Restricted to this company's own members on purpose. Deleting every
    # account with no membership would reach outside the workspace being
    # erased and take an unrelated one with it — a half-finished
    # registration, say, whose membership has not been written yet. Someone
    # who also belongs to another workspace keeps their account.
    counts["orphaned_users"] = 0
    if members:
        result = await session.execute(
            text(
                """
                DELETE FROM users
                WHERE id = ANY(:ids)
                  AND NOT EXISTS (SELECT 1 FROM memberships m WHERE m.user_id = users.id)
                """
            ),
            {"ids": members},
        )
        counts["orphaned_users"] = int(cast(CursorResult, result).rowcount or 0)
    return counts


async def delete_company(
    session: AsyncSession, company_id: str, request_id: int, audit_policy: str = "anonymize"
) -> dict[str, Any]:
    """Erase every trace of one company: Neo4j -> Redis -> Postgres (system
    of record last). Commits after each store so the ``deletion_requests``
    row is durable progress, not an in-memory promise — a step already
    marked done is skipped on retry, so a resumed run is idempotent."""
    req = await get_deletion_request(session, request_id)
    if req is None or req["company_id"] != company_id:
        raise ValueError(f"no deletion request {request_id} for company {company_id!r}")

    await _set_status(session, request_id, "running")
    await session.commit()

    try:
        neo4j_removed = 0 if req["neo4j_done"] else await _delete_neo4j(company_id)
        if not req["neo4j_done"]:
            await _mark_store_done(session, request_id, "neo4j_done")
            await session.commit()

        redis_removed = 0 if req["redis_done"] else await _delete_redis(company_id)
        if not req["redis_done"]:
            await _mark_store_done(session, request_id, "redis_done")
            await session.commit()

        postgres_counts = {} if req["postgres_done"] else await _delete_postgres(session, company_id, audit_policy)
        if not req["postgres_done"]:
            await _mark_store_done(session, request_id, "postgres_done")
            await session.commit()
    except Exception as exc:
        await _set_status(session, request_id, "failed", error=str(exc))
        await session.commit()
        raise

    result = {
        "neo4j_nodes_removed": neo4j_removed,
        "redis_keys_removed": redis_removed,
        "postgres": postgres_counts,
    }
    await _complete(session, request_id, result)
    await session.commit()

    from packages.core import audit

    await audit.record(
        session, company_id, "system", "company.deleted", target=company_id, metadata=result
    )
    await session.commit()
    return result
