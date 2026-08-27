from __future__ import annotations

import json
import secrets
from typing import Any, cast

from sqlalchemy import text
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core import blobs, graph
from packages.shared.schema import Scope

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


# --------------------------- deleting one document ---------------------------
#
# A whole workspace could be erased and a single document could not, which is a
# strange pair of capabilities to ship together. Same order and the same reason:
# derived state first, the system of record last, so a failure part way through
# leaves the document still findable rather than half gone — a row whose
# passages have been deleted is worse than either outcome, because it is a
# document that exists and cannot answer.


async def preview_item_deletion(
    session: AsyncSession, scope: Scope, item_id: str
) -> dict[str, Any] | None:
    """Exactly what deleting this document would destroy. None if it is absent.

    Counted rather than estimated, and read before anything is touched, because
    this is what a person is shown when they are asked to confirm. "Delete this
    document?" is a question nobody can answer well; "delete COMPUTER NETWORKS,
    2 versions, 3,915 passages and 962 stored pages, permanently" is.
    """
    row = (
        await session.execute(
            text(
                """
                SELECT count(*) AS versions,
                       max(version) AS latest,
                       -- The title a person would recognise: the live one, or
                       -- the newest when nothing is live. Titles CHANGE between
                       -- versions — this book was 'This page intentionally left
                       -- blank' until its parser learned to skip front matter —
                       -- so picking one has to be deliberate.
                       (array_agg(title ORDER BY (status = 'active') DESC,
                                  version DESC))[1] AS title
                FROM kb_items
                WHERE workspace_id = :workspace AND item_id = :item
                """
            ),
            {"workspace": scope.workspace_id, "item": item_id},
        )
    ).first()
    # An aggregate with no GROUP BY always returns a row, so `row is None` can
    # never fire here and a missing document would have been offered for
    # deletion with a count of zero. The absence has to be read from the count.
    if row is None or not row.versions:
        return None

    passages = (
        await session.execute(
            text(
                "SELECT count(*) FROM kb_chunks "
                "WHERE workspace_id = :workspace AND item_id = :item"
            ),
            {"workspace": scope.workspace_id, "item": item_id},
        )
    ).scalar_one()

    return {
        "item_id": item_id,
        "title": row.title,
        "versions": int(row.versions),
        "passages": int(passages),
        "permanent": True,
    }


async def delete_item(
    session: AsyncSession, scope: Scope, item_id: str
) -> dict[str, Any]:
    """Erase one document and everything derived from it.

    Four stores, in the order that makes a partial failure survivable:

      graph     passages and the document node. Deleted FIRST because a Chunk
                still carries an embedding, so a passage that outlives its
                document still answers questions — which is the one outcome a
                deletion may never leave behind.
      objects   the original, every rendered page, every figure verdict. All
                beneath one prefix.
      postgres  passages, class assignments, then every VERSION of the item.
                Last, because it is the system of record: while these rows
                exist the document is still explicable.

    Neither the graph nor the object store failing is allowed to abort the row
    deletion. They hold derived state, and a document whose derived state was
    partly removed but whose rows remain is a document that answers from
    nothing — far worse than one whose orphaned page pictures linger in a
    bucket until the next sweep.
    """
    removed: dict[str, Any] = {"item_id": item_id}

    try:
        removed["graph"] = await graph.delete_item(scope, item_id)
    except Exception as exc:  # noqa: BLE001 - derived state, never fatal
        removed["graph_error"] = type(exc).__name__

    try:
        removed["objects"] = await blobs.delete_prefix(
            f"{blobs.key_for(scope.workspace_id, item_id)}"
        )
    except Exception as exc:  # noqa: BLE001 - derived state, never fatal
        removed["objects_error"] = type(exc).__name__

    for table in ("kb_chunks", "kb_item_classes", "kb_items"):
        result = await session.execute(
            text(  # noqa: S608 - table names come from the fixed tuple above
                f"DELETE FROM {table} WHERE workspace_id = :workspace AND item_id = :item"
            ),
            {"workspace": scope.workspace_id, "item": item_id},
        )
        removed[table] = int(cast(CursorResult, result).rowcount or 0)

    return removed


# -------------------------- deleting one collection --------------------------
#
# Deleting a collection used to be two DELETE statements — the items, then the
# collection row — and nothing else. Every passage those documents put in the
# graph stayed, still carrying its embedding, and every stored original stayed
# in the bucket. So a collection could be reported deleted while its content
# was still answering questions: the exact outcome deleting a single document
# is careful to avoid.
#
# It is the same work, so it is the same code. A collection is deleted by
# deleting each document in it through delete_item(), and only then dropping
# the collection row.


async def preview_collection_deletion(
    session: AsyncSession, workspace_id: str, collection_id: str
) -> dict[str, Any] | None:
    """What deleting this collection would destroy. None if there is no such
    collection — a caller turns that into a 404 rather than reporting a
    successful delete of nothing."""
    row = (
        await session.execute(
            text(
                "SELECT name FROM collections "
                "WHERE workspace_id = :ws AND collection_id = :cid"
            ),
            {"ws": workspace_id, "cid": collection_id},
        )
    ).first()
    if row is None:
        return None

    counts = (
        await session.execute(
            text(
                """
                SELECT count(DISTINCT item_id) AS documents, count(*) AS versions
                FROM kb_items
                WHERE workspace_id = :ws AND collection_id = :cid
                """
            ),
            {"ws": workspace_id, "cid": collection_id},
        )
    ).one()

    passages = (
        await session.execute(
            text(
                """
                SELECT count(*) FROM kb_chunks c
                WHERE c.workspace_id = :ws
                  AND EXISTS (
                    SELECT 1 FROM kb_items i
                    WHERE i.workspace_id = c.workspace_id
                      AND i.item_id = c.item_id
                      AND i.collection_id = :cid
                  )
                """
            ),
            {"ws": workspace_id, "cid": collection_id},
        )
    ).scalar_one()

    return {
        "collection_id": collection_id,
        "name": row.name,
        "documents": int(counts.documents),
        "versions": int(counts.versions),
        "passages": int(passages),
        "permanent": True,
    }


async def delete_collection_and_index(
    session: AsyncSession, workspace_id: str, collection_id: str
) -> dict[str, Any]:
    """Delete a collection, its documents, and everything they derived.

    Each document goes through delete_item(), which is what makes this
    complete: graph passages and their embeddings first, then the stored
    original and every rendered page, then the rows. Dropping the collection
    row is the last thing that happens, for the same reason Postgres is last
    inside delete_item — while it exists the collection is still explicable.

    The per-document errors are COUNTED rather than raised. A graph or bucket
    that is unreachable must not leave half the collection deleted and half
    not, and a caller that is told `graph_errors: 3` can retry; one that is
    told nothing cannot.

    Scoped to the workspace, not the collection, when the documents are
    deleted: an item id is derived from its collection already (see
    ``store.stable_item_id``), so it cannot reach another collection's
    document — while a graph node written before the collection property
    existed would be missed by a collection filter and orphaned, which is the
    bug this function exists to fix.
    """
    item_ids = list(
        (
            await session.execute(
                text(
                    "SELECT DISTINCT item_id FROM kb_items "
                    "WHERE workspace_id = :ws AND collection_id = :cid"
                ),
                {"ws": workspace_id, "cid": collection_id},
            )
        ).scalars()
    )

    scope = Scope(workspace_id=workspace_id, collection_id=None)
    totals: dict[str, Any] = {
        "collection_id": collection_id,
        "documents": len(item_ids),
        "items_removed": 0,
        "passages": 0,
        "graph_chunks": 0,
        "graph_items": 0,
        "objects": 0,
        "graph_errors": 0,
        "objects_errors": 0,
    }

    for item_id in item_ids:
        removed = await delete_item(session, scope, item_id)
        totals["items_removed"] += int(removed.get("kb_items", 0))
        totals["passages"] += int(removed.get("kb_chunks", 0))
        graph_counts = removed.get("graph") or {}
        totals["graph_chunks"] += int(graph_counts.get("chunks", 0))
        totals["graph_items"] += int(graph_counts.get("items", 0))
        totals["objects"] += int(removed.get("objects", 0))
        if "graph_error" in removed:
            totals["graph_errors"] += 1
        if "objects_error" in removed:
            totals["objects_errors"] += 1

    dropped = await session.execute(
        text("DELETE FROM collections WHERE workspace_id = :ws AND collection_id = :cid"),
        {"ws": workspace_id, "cid": collection_id},
    )
    totals["collection_removed"] = bool(cast(CursorResult, dropped).rowcount or 0)
    return totals


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
