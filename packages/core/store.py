from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from packages.shared.schema import Item, Lifecycle, Scope, SourceRef, content_hash

# ---------------------------------------------------------------------------
# THE ITEM INDEX — the catalogue of what the knowledge base holds.
#
# One table, one write path, and one mechanism that everything turns on: the
# content hash. A write either creates, changes nothing, or supersedes.
#
# Identity: an item's id is STABLE across re-ingestion, re-embedding and
# content updates (KB-1), because it is derived from where the content came
# from rather than from the content itself. Versions live under that id, so
# "the same document, changed" keeps its identity while its history is kept.
# The primary key is (item_id, version); exactly one version is `active`.
# ---------------------------------------------------------------------------


def stable_item_id(scope: Scope, source: SourceRef) -> str:
    """A durable id for a logical item — same source, same id, forever.

    Deliberately derived from scope + origin and NOT from the content: an
    edited document is the same item at a new version, not a new item. The
    tenant is part of it because two agencies may sync the same public file
    and must never collide.
    """
    seed = "\x1f".join([scope.tenant_id, scope.brand_id or "", source.source, source.locator])
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]


Outcome = Literal["created", "unchanged", "versioned"]


@dataclass(slots=True)
class PutResult:
    """What a write actually did — never a silent success.

    `unchanged` is the important one: it is the signal the pipeline uses to
    skip embedding entirely, which is where the cost of re-syncing a large
    drive is avoided (KB-8).
    """

    outcome: Outcome
    item: Item
    embedded_needed: bool

    @property
    def changed(self) -> bool:
        return self.outcome != "unchanged"


_CURRENT = text(
    """
    SELECT item_id, version, hash, title, body, status
    FROM kb_items
    WHERE tenant_id = :tenant AND item_id = :item_id AND status = :active
    ORDER BY version DESC
    LIMIT 1
    """
)

_INSERT = text(
    """
    INSERT INTO kb_items
        (item_id, version, tenant_id, brand_id, title, body, body_tsv,
         source, locator, url, hash, supersedes, status,
         created_at, updated_at, period_start, period_end, metadata)
    VALUES
        (:item_id, :version, :tenant, :brand, :title, :body,
         to_tsvector('english', :title || ' ' || :body),
         :source, :locator, :url, :hash, :supersedes, :status,
         :created_at, now(), :period_start, :period_end, CAST(:metadata AS jsonb))
    ON CONFLICT (item_id, version) DO UPDATE SET
        title      = EXCLUDED.title,
        body       = EXCLUDED.body,
        body_tsv   = EXCLUDED.body_tsv,
        url        = EXCLUDED.url,
        updated_at = now(),
        metadata   = EXCLUDED.metadata
    """
)

_SUPERSEDE = text(
    """
    UPDATE kb_items
    SET status = :superseded, updated_at = now()
    WHERE tenant_id = :tenant AND item_id = :item_id AND version < :version
      AND status = :active
    """
)


async def put_item(session: AsyncSession, item: Item) -> PutResult:
    """Write an item, honouring the hash.

    Three outcomes and no others:
      created    — first time we have seen this source location
      unchanged  — identical content already held; nothing is written or embedded
      versioned  — content differs; a new version lands and the prior is superseded

    The unchanged path is checked BEFORE any model call is made, which is what
    makes re-syncing an unchanged drive close to free.
    """
    item = item.with_hash()
    item_id = item.id or stable_item_id(item.scope, item.source)

    current = (
        await session.execute(
            _CURRENT,
            {"tenant": item.scope.tenant_id, "item_id": item_id, "active": Lifecycle.ACTIVE},
        )
    ).first()

    if current is not None and current.hash == item.hash:
        held = item.model_copy(
            update={"id": item_id, "version": current.version, "status": Lifecycle.ACTIVE}
        )
        return PutResult("unchanged", held, embedded_needed=False)

    version = 1 if current is None else current.version + 1
    supersedes = f"{item_id}@{current.version}" if current is not None else None
    stored = item.model_copy(
        update={
            "id": item_id,
            "version": version,
            "supersedes": supersedes,
            "status": Lifecycle.ACTIVE,
            "created_at": item.created_at or datetime.now(UTC),
        }
    )

    # The previous version stops being current BEFORE the new one lands. The
    # partial unique index allows exactly one active version per item, so the
    # other order is not merely untidy — it is rejected outright. Both
    # statements share the caller's transaction, so there is no window in
    # which the item has no active version: it commits as one or not at all.
    if current is not None:
        await session.execute(
            _SUPERSEDE,
            {
                "tenant": stored.scope.tenant_id,
                "item_id": item_id,
                "version": version,
                "active": Lifecycle.ACTIVE,
                "superseded": Lifecycle.SUPERSEDED,
            },
        )

    await session.execute(
        _INSERT,
        {
            "item_id": item_id,
            "version": version,
            "tenant": stored.scope.tenant_id,
            "brand": stored.scope.brand_id,
            "title": stored.title,
            "body": stored.body,
            "source": stored.source.source,
            "locator": stored.source.locator,
            "url": stored.source.url,
            "hash": stored.hash,
            "supersedes": supersedes,
            "status": Lifecycle.ACTIVE,
            "created_at": stored.created_at,
            "period_start": stored.period_start,
            "period_end": stored.period_end,
            "metadata": json.dumps(stored.metadata),
        },
    )

    return PutResult("created" if current is None else "versioned", stored, embedded_needed=True)


def _row_to_item(row: Any) -> Item:
    md = row.metadata if isinstance(row.metadata, dict) else json.loads(row.metadata or "{}")
    return Item(
        id=row.item_id,
        scope=Scope(tenant_id=row.tenant_id, brand_id=row.brand_id),
        title=row.title,
        body=row.body,
        source=SourceRef(source=row.source, locator=row.locator, url=row.url),
        hash=row.hash,
        version=row.version,
        supersedes=row.supersedes,
        status=Lifecycle(row.status),
        created_at=row.created_at,
        period_start=row.period_start,
        period_end=row.period_end,
        metadata=md,
    )


_SELECT = """
    SELECT item_id, version, tenant_id, brand_id, title, body, source, locator,
           url, hash, supersedes, status, created_at, period_start, period_end, metadata
    FROM kb_items
"""


async def get_item(
    session: AsyncSession, scope: Scope, item_id: str, version: int | None = None
) -> Item | None:
    """One item — its current version, or a specific one for point-in-time reads."""
    clauses = ["tenant_id = :tenant", "item_id = :item_id"]
    params: dict[str, Any] = {"tenant": scope.tenant_id, "item_id": item_id}
    if version is None:
        clauses.append("status = :active")
        params["active"] = Lifecycle.ACTIVE
    else:
        clauses.append("version = :version")
        params["version"] = version
    if scope.brand_id is not None:
        clauses.append("brand_id = :brand")
        params["brand"] = scope.brand_id

    row = (
        await session.execute(
            text(f"{_SELECT} WHERE {' AND '.join(clauses)} ORDER BY version DESC LIMIT 1"), params
        )
    ).first()
    return _row_to_item(row) if row else None


async def get_items_by_ids(
    session: AsyncSession, scope: Scope, item_ids: list[str]
) -> list[Item]:
    """Batch hydrate — one query, not N. The graph holds ids only."""
    if not item_ids:
        return []
    clauses = ["tenant_id = :tenant", "item_id = ANY(:ids)", "status = :active"]
    params: dict[str, Any] = {
        "tenant": scope.tenant_id,
        "ids": list(item_ids),
        "active": Lifecycle.ACTIVE,
    }
    if scope.brand_id is not None:
        clauses.append("brand_id = :brand")
        params["brand"] = scope.brand_id
    rows = (await session.execute(text(f"{_SELECT} WHERE {' AND '.join(clauses)}"), params)).all()
    return [_row_to_item(r) for r in rows]


async def list_items(
    session: AsyncSession,
    scope: Scope,
    source: str | None = None,
    status: Lifecycle = Lifecycle.ACTIVE,
    class_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[Item]:
    """The catalogue view: what is held, filterable — KB-1's listing requirement."""
    clauses = ["tenant_id = :tenant", "status = :status"]
    params: dict[str, Any] = {
        "tenant": scope.tenant_id, "status": status, "limit": limit, "offset": offset,
    }
    if scope.brand_id is not None:
        clauses.append("brand_id = :brand")
        params["brand"] = scope.brand_id
    if source:
        clauses.append("source = :source")
        params["source"] = source
    if class_id:
        # EXISTS rather than a join: an item may carry several classes, and a
        # join would return it once per class.
        clauses.append(
            "EXISTS (SELECT 1 FROM kb_item_classes ic "
            "WHERE ic.item_id = kb_items.item_id AND ic.tenant_id = :tenant "
            "AND ic.class_id = :class_id)"
        )
        params["class_id"] = class_id
    rows = (
        await session.execute(
            text(
                f"{_SELECT} WHERE {' AND '.join(clauses)} "
                "ORDER BY updated_at DESC LIMIT :limit OFFSET :offset"
            ),
            params,
        )
    ).all()
    return [_row_to_item(r) for r in rows]


async def item_versions(session: AsyncSession, scope: Scope, item_id: str) -> list[Item]:
    """Every version of an item, newest first — the lineage KB-1 requires."""
    rows = (
        await session.execute(
            text(f"{_SELECT} WHERE tenant_id = :tenant AND item_id = :item_id ORDER BY version DESC"),
            {"tenant": scope.tenant_id, "item_id": item_id},
        )
    ).all()
    return [_row_to_item(r) for r in rows]


async def mark_failed(
    session: AsyncSession, scope: Scope, item_id: str, version: int, reason: str
) -> None:
    """Ingestion failures are visible and re-runnable, never silent (KB-1/KB-3)."""
    await session.execute(
        text(
            """
            UPDATE kb_items
            SET status = :failed,
                -- the cast is required: asyncpg cannot infer a parameter's
                -- type inside jsonb_build_object and refuses the statement
                metadata = metadata || jsonb_build_object('failure', CAST(:reason AS text)),
                updated_at = now()
            WHERE tenant_id = :tenant AND item_id = :item_id AND version = :version
            """
        ),
        {
            "failed": Lifecycle.FAILED, "reason": reason,
            "tenant": scope.tenant_id, "item_id": item_id, "version": version,
        },
    )


__all__ = [
    "PutResult",
    "content_hash",
    "get_item",
    "get_items_by_ids",
    "item_versions",
    "list_items",
    "mark_failed",
    "put_item",
    "stable_item_id",
]
