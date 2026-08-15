from __future__ import annotations

import os
import re
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from packages.shared.schema import Scope

# ---------------------------------------------------------------------------
# CLUSTERS AND COLLECTIONS — the shape of the store.
#
#   workspace  (who you are — comes from your credential)
#     cluster  (a grouping you create; where collections live)
#       collection  (what you actually read and write: items + vectors)
#
# A collection owns its embedding model and dimensions because those are the
# two settings that cannot be changed in place — altering either invalidates
# every vector beneath it, which is a migration, not an edit. Everything else
# about a collection is mutable.
# ---------------------------------------------------------------------------

DEFAULT_CLUSTER = "default"
_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug[:63] or "untitled"


def valid_id(value: str) -> bool:
    return bool(_SLUG.match(value))


def default_model() -> str:
    return os.getenv("EMBEDDING_MODEL", "")


def default_dimensions() -> int:
    return int(os.getenv("EMBED_DIM", "1536"))


# -------------------------------- clusters --------------------------------


async def ensure_default_cluster(session: AsyncSession, workspace_id: str) -> str:
    """Every workspace has somewhere to put its first collection.

    Created on demand rather than at sign-up: a workspace that never stores
    anything should not accumulate furniture it did not ask for.
    """
    await session.execute(
        text(
            """
            INSERT INTO clusters (workspace_id, cluster_id, name)
            VALUES (:ws, :cid, 'Default cluster')
            ON CONFLICT DO NOTHING
            """
        ),
        {"ws": workspace_id, "cid": DEFAULT_CLUSTER},
    )
    return DEFAULT_CLUSTER


async def create_cluster(
    session: AsyncSession, workspace_id: str, name: str, cluster_id: str | None = None
) -> dict[str, Any]:
    cid = cluster_id or slugify(name)
    if not valid_id(cid):
        raise ValueError("A cluster id must be lowercase letters, numbers, - or _.")
    await session.execute(
        text(
            """
            INSERT INTO clusters (workspace_id, cluster_id, name)
            VALUES (:ws, :cid, :name)
            ON CONFLICT (workspace_id, cluster_id) DO UPDATE SET name = EXCLUDED.name
            """
        ),
        {"ws": workspace_id, "cid": cid, "name": name},
    )
    return {"cluster_id": cid, "name": name}


async def list_clusters(session: AsyncSession, workspace_id: str) -> list[dict[str, Any]]:
    """Clusters with their collections and live item counts.

    One query with the counts joined in, rather than a call per collection —
    the console shows this on every page load.
    """
    rows = (
        await session.execute(
            text(
                """
                SELECT c.cluster_id, c.name AS cluster_name, c.region, c.created_at,
                       col.collection_id, col.name AS collection_name,
                       col.embedding_model, col.dimensions,
                       COUNT(i.item_id) FILTER (WHERE i.status = 'active') AS items
                FROM clusters c
                LEFT JOIN collections col
                       ON col.workspace_id = c.workspace_id AND col.cluster_id = c.cluster_id
                LEFT JOIN kb_items i
                       ON i.workspace_id = col.workspace_id
                      AND i.collection_id = col.collection_id
                WHERE c.workspace_id = :ws
                GROUP BY c.cluster_id, c.name, c.region, c.created_at,
                         col.collection_id, col.name, col.embedding_model, col.dimensions
                ORDER BY c.created_at, col.collection_id
                """
            ),
            {"ws": workspace_id},
        )
    ).all()

    clusters: dict[str, dict[str, Any]] = {}
    for r in rows:
        entry = clusters.setdefault(
            r.cluster_id,
            {
                "cluster_id": r.cluster_id,
                "name": r.cluster_name,
                "region": r.region,
                "created_at": r.created_at.isoformat(),
                "collections": [],
            },
        )
        if r.collection_id:
            entry["collections"].append(
                {
                    "collection_id": r.collection_id,
                    "name": r.collection_name,
                    "embedding_model": r.embedding_model,
                    "dimensions": r.dimensions,
                    "items": int(r.items or 0),
                }
            )
    return list(clusters.values())


# ------------------------------- collections -------------------------------


async def create_collection(
    session: AsyncSession,
    workspace_id: str,
    name: str,
    collection_id: str | None = None,
    cluster_id: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """Make a collection. Its model and dimensions are fixed here, on purpose."""
    cid = collection_id or slugify(name)
    if not valid_id(cid):
        raise ValueError("A collection id must be lowercase letters, numbers, - or _.")

    cluster = cluster_id or await ensure_default_cluster(session, workspace_id)
    exists = (
        await session.execute(
            text(
                "SELECT 1 FROM clusters WHERE workspace_id = :ws AND cluster_id = :cid"
            ),
            {"ws": workspace_id, "cid": cluster},
        )
    ).first()
    if exists is None:
        raise ValueError(f"No such cluster: {cluster!r}")

    await session.execute(
        text(
            """
            INSERT INTO collections
                (workspace_id, collection_id, cluster_id, name, description,
                 embedding_model, dimensions)
            VALUES (:ws, :cid, :cluster, :name, :description, :model, :dims)
            ON CONFLICT (workspace_id, collection_id)
            DO UPDATE SET name = EXCLUDED.name, description = EXCLUDED.description
            """
        ),
        {
            "ws": workspace_id, "cid": cid, "cluster": cluster, "name": name,
            "description": description,
            "model": default_model(), "dims": default_dimensions(),
        },
    )
    return {
        "collection_id": cid,
        "cluster_id": cluster,
        "name": name,
        "embedding_model": default_model(),
        "dimensions": default_dimensions(),
    }


async def get_collection(
    session: AsyncSession, workspace_id: str, collection_id: str
) -> dict[str, Any] | None:
    """One collection with the numbers the console shows: items, versions,
    how much of it is actually filed, and how it breaks down by source."""
    row = (
        await session.execute(
            text(
                """
                SELECT collection_id, cluster_id, name, description,
                       embedding_model, dimensions, created_at
                FROM collections WHERE workspace_id = :ws AND collection_id = :cid
                """
            ),
            {"ws": workspace_id, "cid": collection_id},
        )
    ).first()
    if row is None:
        return None

    stats = (
        await session.execute(
            text(
                """
                SELECT
                    count(*) FILTER (WHERE status = 'active')     AS items,
                    count(*) FILTER (WHERE status = 'superseded') AS superseded,
                    count(*) FILTER (WHERE status = 'failed')     AS failed,
                    COALESCE(sum(length(body)) FILTER (WHERE status = 'active'), 0) AS characters,
                    count(DISTINCT source) FILTER (WHERE status = 'active') AS sources
                FROM kb_items WHERE workspace_id = :ws AND collection_id = :cid
                """
            ),
            {"ws": workspace_id, "cid": collection_id},
        )
    ).one()

    return {
        "collection_id": row.collection_id,
        "cluster_id": row.cluster_id,
        "name": row.name,
        "description": row.description,
        "embedding_model": row.embedding_model,
        "dimensions": row.dimensions,
        "created_at": row.created_at.isoformat(),
        "stats": {
            "items": int(stats.items),
            "superseded": int(stats.superseded),
            "failed": int(stats.failed),
            "characters": int(stats.characters),
            "sources": int(stats.sources),
        },
    }


async def rename_collection(
    session: AsyncSession, workspace_id: str, collection_id: str, name: str
) -> dict[str, Any] | None:
    """Change a collection's display name. Only the name.

    The id is the collection's stable handle — it is in every URL, every SDK
    snippet, and the Scope stamped on every vector beneath it — so it is fixed
    at creation and cannot be edited here. Returns the renamed collection, or
    None when nothing matched, which the caller turns into a 404 rather than a
    silent success against a collection that does not exist.
    """
    result = await session.execute(
        text(
            "UPDATE collections SET name = :name "
            "WHERE workspace_id = :ws AND collection_id = :cid"
        ),
        {"ws": workspace_id, "cid": collection_id, "name": name},
    )
    # CursorResult carries rowcount; the base Result the stubs declare does
    # not. Read through the cursor attribute so the check is typed rather than
    # asserted — the value is what decides whether the collection existed.
    if not result.rowcount:  # type: ignore[attr-defined]
        return None
    return {"collection_id": collection_id, "name": name}


# Deleting a collection lives in `erasure.delete_collection_and_index`, with
# the rest of the deletion story. It used to live here, as two DELETE
# statements against Postgres, and that is precisely what was wrong with it:
# the graph passages and the stored originals were never touched, so a deleted
# collection went on answering questions from vectors nobody could see. A
# module that knows only about rows cannot delete a collection correctly, so
# it no longer offers to.


def scope_for(workspace_id: str, collection_id: str | None) -> Scope:
    return Scope(workspace_id=workspace_id, collection_id=collection_id)


__all__ = [
    "DEFAULT_CLUSTER",
    "create_cluster",
    "create_collection",
    "ensure_default_cluster",
    "get_collection",
    "list_clusters",
    "rename_collection",
    "scope_for",
    "slugify",
    "valid_id",
]
