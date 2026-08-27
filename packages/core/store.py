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
    workspace is part of it because two agencies may sync the same public file
    and must never collide.
    """
    seed = "\x1f".join([scope.workspace_id, scope.collection_id or "", source.source, source.locator])
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
    WHERE workspace_id = :workspace AND item_id = :item_id AND status = :active
    ORDER BY version DESC
    LIMIT 1
    """
)

_INSERT = text(
    """
    INSERT INTO kb_items
        (item_id, version, workspace_id, collection_id, title, body, body_tsv,
         source, locator, url, hash, supersedes, status,
         created_at, updated_at, period_start, period_end, metadata)
    VALUES
        (:item_id, :version, :workspace, :collection, :title, :body,
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
    WHERE workspace_id = :workspace AND item_id = :item_id AND version < :version
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
            {"workspace": item.scope.workspace_id, "item_id": item_id, "active": Lifecycle.ACTIVE},
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
                "workspace": stored.scope.workspace_id,
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
            "workspace": stored.scope.workspace_id,
            "collection": stored.scope.collection_id,
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

    # Passages are built here, in the same transaction as the document, rather
    # than in the embedding job. Two reasons, and the second is the important
    # one. Splitting text is pure string work with no network in it. And the
    # keyword arm searches passages — so deferring this would leave a document
    # unsearchable until an embedding provider answered, when the entire point
    # of having a keyword arm is that it still works when that provider does
    # not. Doing it here also makes "a document always has passages" an
    # invariant of the store instead of something each caller must remember.
    from packages.core import chunks

    await chunks.rebuild(session, stored.scope, item_id, version, stored.title, stored.body)

    return PutResult("created" if current is None else "versioned", stored, embedded_needed=True)


def _row_to_item(row: Any) -> Item:
    md = row.metadata if isinstance(row.metadata, dict) else json.loads(row.metadata or "{}")
    return Item(
        id=row.item_id,
        scope=Scope(workspace_id=row.workspace_id, collection_id=row.collection_id),
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
    SELECT item_id, version, workspace_id, collection_id, title, body, source, locator,
           url, hash, supersedes, status, created_at, period_start, period_end, metadata
    FROM kb_items
"""


async def get_item(
    session: AsyncSession, scope: Scope, item_id: str, version: int | None = None
) -> Item | None:
    """One item — its current version, or a specific one for point-in-time reads."""
    clauses = ["workspace_id = :workspace", "item_id = :item_id"]
    params: dict[str, Any] = {"workspace": scope.workspace_id, "item_id": item_id}
    if version is None:
        clauses.append("status = :active")
        params["active"] = Lifecycle.ACTIVE
    else:
        clauses.append("version = :version")
        params["version"] = version
    if scope.collection_id is not None:
        clauses.append("collection_id = :collection")
        params["collection"] = scope.collection_id

    row = (
        await session.execute(
            text(f"{_SELECT} WHERE {' AND '.join(clauses)} ORDER BY version DESC LIMIT 1"), params
        )
    ).first()
    return _row_to_item(row) if row else None


async def edit_item(
    session: AsyncSession,
    scope: Scope,
    item_id: str,
    *,
    title: str | None = None,
    body: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> tuple[Item, bool]:
    """Edit one active document without changing its stable identity.

    Title/body edits create a new version and rebuild passages. Metadata-only
    edits stay on the current version because they do not change retrievable
    content. The row is locked so two editors cannot both mint the same next
    version.
    """
    clauses = ["workspace_id = :workspace", "item_id = :item_id", "status = :active"]
    params: dict[str, Any] = {
        "workspace": scope.workspace_id,
        "item_id": item_id,
        "active": Lifecycle.ACTIVE,
    }
    if scope.collection_id is not None:
        clauses.append("collection_id = :collection")
        params["collection"] = scope.collection_id

    row = (
        await session.execute(
            text(
                f"{_SELECT} WHERE {' AND '.join(clauses)} "
                "ORDER BY version DESC LIMIT 1 FOR UPDATE"
            ),
            params,
        )
    ).first()
    if row is None:
        raise LookupError(item_id)

    current = _row_to_item(row)
    next_title = current.title if title is None else title.strip()
    next_body = current.body if body is None else body
    next_metadata = dict(current.metadata)
    if metadata is not None:
        next_metadata.update(metadata)

    content_changed = next_title != current.title or next_body != current.body
    metadata_changed = next_metadata != current.metadata
    if not content_changed and not metadata_changed:
        return current, False

    if not content_changed:
        await session.execute(
            text(
                "UPDATE kb_items SET metadata = CAST(:metadata AS jsonb), updated_at = now() "
                "WHERE workspace_id = :workspace AND item_id = :item_id "
                "AND version = :version AND status = :active"
            ),
            {
                **params,
                "version": current.version,
                "metadata": json.dumps(next_metadata),
            },
        )
        return current.model_copy(update={"metadata": next_metadata}), False

    version = current.version + 1
    supersedes = f"{item_id}@{current.version}"
    updated = current.model_copy(
        update={
            "title": next_title,
            "body": next_body,
            "hash": content_hash(next_body),
            "version": version,
            "supersedes": supersedes,
            "status": Lifecycle.ACTIVE,
            "metadata": next_metadata,
        }
    )
    await session.execute(
        _SUPERSEDE,
        {
            "workspace": scope.workspace_id,
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
            "workspace": updated.scope.workspace_id,
            "collection": updated.scope.collection_id,
            "title": updated.title,
            "body": updated.body,
            "source": updated.source.source,
            "locator": updated.source.locator,
            "url": updated.source.url,
            "hash": updated.hash,
            "supersedes": supersedes,
            "status": Lifecycle.ACTIVE,
            "created_at": updated.created_at,
            "period_start": updated.period_start,
            "period_end": updated.period_end,
            "metadata": json.dumps(updated.metadata),
        },
    )
    from packages.core import chunks

    await chunks.rebuild(session, scope, item_id, version, updated.title, updated.body)
    return updated, True


async def get_items_by_ids(
    session: AsyncSession, scope: Scope, item_ids: list[str]
) -> list[Item]:
    """Batch hydrate — one query, not N. The graph holds ids only."""
    if not item_ids:
        return []
    clauses = ["workspace_id = :workspace", "item_id = ANY(:ids)", "status = :active"]
    params: dict[str, Any] = {
        "workspace": scope.workspace_id,
        "ids": list(item_ids),
        "active": Lifecycle.ACTIVE,
    }
    if scope.collection_id is not None:
        clauses.append("collection_id = :collection")
        params["collection"] = scope.collection_id
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
    clauses = ["workspace_id = :workspace", "status = :status"]
    params: dict[str, Any] = {
        "workspace": scope.workspace_id, "status": status, "limit": limit, "offset": offset,
    }
    if scope.collection_id is not None:
        clauses.append("collection_id = :collection")
        params["collection"] = scope.collection_id
    if source:
        clauses.append("source = :source")
        params["source"] = source
    if class_id:
        # EXISTS rather than a join: an item may carry several classes, and a
        # join would return it once per class.
        clauses.append(
            "EXISTS (SELECT 1 FROM kb_item_classes ic "
            "WHERE ic.item_id = kb_items.item_id AND ic.workspace_id = :workspace "
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
            text(f"{_SELECT} WHERE workspace_id = :workspace AND item_id = :item_id ORDER BY version DESC"),
            {"workspace": scope.workspace_id, "item_id": item_id},
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
            WHERE workspace_id = :workspace AND item_id = :item_id AND version = :version
            """
        ),
        {
            "failed": Lifecycle.FAILED, "reason": reason,
            "workspace": scope.workspace_id, "item_id": item_id, "version": version,
        },
    )


async def record_failure(
    session: AsyncSession, scope: Scope, source: SourceRef, title: str, reason: str
) -> str:
    """An ingest that could not produce content, written down where it shows.

    Two rules, and the second is the one that matters:

    A first attempt leaves a row at status `failed`, so the collection's failed
    count moves and the reason is readable. Without it the only record was the
    job's return value, which nothing reads — the console could say the file
    was "not searchable yet" and never why.

    A repeat attempt does NOT touch the version already held. A document that
    indexed cleanly last week must not vanish from search because someone
    re-uploaded a corrupt copy of it today; the failure is stamped on the
    active row instead, and the content survives.
    """
    item_id = stable_item_id(scope, source)
    current = (
        await session.execute(
            _CURRENT,
            {"workspace": scope.workspace_id, "item_id": item_id, "active": Lifecycle.ACTIVE},
        )
    ).first()

    if current is not None:
        await session.execute(
            text(
                """
                UPDATE kb_items
                SET metadata = metadata || jsonb_build_object('failure', CAST(:reason AS text)),
                    updated_at = now()
                WHERE workspace_id = :workspace AND item_id = :item_id AND version = :version
                """
            ),
            {
                "reason": reason, "workspace": scope.workspace_id,
                "item_id": item_id, "version": current.version,
            },
        )
        return item_id

    await session.execute(
        _INSERT,
        {
            "item_id": item_id, "version": 1,
            "workspace": scope.workspace_id, "collection": scope.collection_id,
            "title": title, "body": "", "source": source.source,
            "locator": source.locator, "url": source.url,
            # No content means no hash worth keeping: a later, readable upload
            # of the same file must not be mistaken for unchanged.
            "hash": "", "supersedes": None, "status": Lifecycle.FAILED,
            "created_at": datetime.now(UTC), "period_start": None, "period_end": None,
            "metadata": json.dumps({"failure": reason}),
        },
    )
    return item_id


__all__ = [
    "PutResult",
    "content_hash",
    "get_item",
    "get_items_by_ids",
    "edit_item",
    "item_versions",
    "list_items",
    "mark_failed",
    "put_item",
    "record_failure",
    "stable_item_id",
]
