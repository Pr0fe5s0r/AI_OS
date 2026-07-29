from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.chunk import Chunk, split
from packages.shared.schema import Scope

# ---------------------------------------------------------------------------
# THE PASSAGE INDEX.
#
# Documents are the unit of identity and versioning. Passages are the unit of
# retrieval. This module is the join between them, and it holds one rule that
# everything else depends on: a document's passages are rebuilt wholesale,
# never patched. Editing a paragraph shifts every boundary after it, so
# reconciling passage by passage would leave stale text behind that still
# answers queries — the worst possible failure for a store whose whole claim
# is that it can show you where an answer came from.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class StoredChunk:
    chunk_id: str
    item_id: str
    ordinal: int
    heading: str
    text: str


def chunk_id(item_id: str, version: int, ordinal: int) -> str:
    """Deterministic, so a rebuild lands on the same ids for unchanged passages.

    That matters for the graph: identifiers that churned on every re-index
    would make every node look new, and the Neo4j nodes for a document could
    never be replaced cleanly.
    """
    seed = f"{item_id}\x1f{version}\x1f{ordinal}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]


async def rebuild(
    session: AsyncSession, scope: Scope, item_id: str, version: int, title: str, body: str
) -> list[StoredChunk]:
    """Replace a document's passages with the ones its current text produces."""
    pieces: list[Chunk] = split(body)

    # Every prior version's passages go too. A superseded version's text is
    # still in kb_items for lineage, but it must never be retrievable, or the
    # store would answer from a document the user has already replaced.
    await session.execute(
        text("DELETE FROM kb_chunks WHERE workspace_id = :w AND item_id = :i"),
        {"w": scope.workspace_id, "i": item_id},
    )

    stored: list[StoredChunk] = []
    for piece in pieces:
        cid = chunk_id(item_id, version, piece.ordinal)
        await session.execute(
            text(
                """
                INSERT INTO kb_chunks
                    (chunk_id, workspace_id, collection_id, item_id, version,
                     ordinal, heading, text)
                VALUES (:cid, :w, :c, :i, :v, :ord, :heading, :text)
                """
            ),
            {
                "cid": cid, "w": scope.workspace_id, "c": scope.collection_id,
                "i": item_id, "v": version, "ord": piece.ordinal,
                "heading": piece.heading, "text": piece.text,
            },
        )
        stored.append(
            StoredChunk(
                chunk_id=cid, item_id=item_id, ordinal=piece.ordinal,
                heading=piece.heading, text=piece.text,
            )
        )
    return stored


async def for_item(session: AsyncSession, scope: Scope, item_id: str) -> list[StoredChunk]:
    rows = (
        await session.execute(
            text(
                """
                SELECT chunk_id, item_id, ordinal, heading, text FROM kb_chunks
                WHERE workspace_id = :w AND item_id = :i AND archived_at IS NULL
                ORDER BY ordinal
                """
            ),
            {"w": scope.workspace_id, "i": item_id},
        )
    ).all()
    return [StoredChunk(r.chunk_id, r.item_id, r.ordinal, r.heading, r.text) for r in rows]


async def by_ids(
    session: AsyncSession, scope: Scope, chunk_ids: list[str]
) -> dict[str, StoredChunk]:
    if not chunk_ids:
        return {}
    rows = (
        await session.execute(
            text(
                """
                SELECT chunk_id, item_id, ordinal, heading, text FROM kb_chunks
                WHERE workspace_id = :w AND chunk_id = ANY(:ids)
                """
            ),
            {"w": scope.workspace_id, "ids": chunk_ids},
        )
    ).all()
    return {
        r.chunk_id: StoredChunk(r.chunk_id, r.item_id, r.ordinal, r.heading, r.text)
        for r in rows
    }


async def keyword_search(
    session: AsyncSession, scope: Scope, query: str, limit: int
) -> list[dict[str, Any]]:
    """The keyword arm, at passage level.

    Matching a whole document tells you the file mentions a word somewhere,
    which is not an answer. Matching a passage tells you where.
    """
    collection_clause = "AND collection_id = :c" if scope.collection_id else ""
    rows = (
        await session.execute(
            text(
                f"""
                SELECT chunk_id, item_id, ordinal, heading,
                       ts_rank(content_tsv, websearch_to_tsquery('english', :q)) AS score,
                       ts_headline('english', text,
                                   websearch_to_tsquery('english', :q),
                                   'StartSel=[[, StopSel=]], MaxFragments=2, MaxWords=40, MinWords=15'
                       ) AS excerpt
                FROM kb_chunks
                WHERE workspace_id = :w {collection_clause}
                  -- Archived nodes are out of retrieval. Consolidation has
                  -- already decided they are superseded or decaying, and
                  -- answering from them would contradict that.
                  AND archived_at IS NULL
                  AND content_tsv @@ websearch_to_tsquery('english', :q)
                ORDER BY score DESC
                LIMIT :limit
                """  # noqa: S608 - collection_clause is a fixed literal, not input
            ),
            {"q": query, "w": scope.workspace_id, "c": scope.collection_id, "limit": limit},
        )
    ).all()
    return [
        {
            "chunk_id": r.chunk_id, "item_id": r.item_id, "ordinal": r.ordinal,
            "heading": r.heading, "score": float(r.score), "excerpt": r.excerpt,
        }
        for r in rows
    ]


async def count_for(session: AsyncSession, scope: Scope) -> int:
    clause = "AND collection_id = :c" if scope.collection_id else ""
    return int(
        (
            await session.execute(
                text(
                    "SELECT count(*) FROM kb_chunks WHERE workspace_id = :w "  # noqa: S608
                    f"AND archived_at IS NULL {clause}"
                ),
                {"w": scope.workspace_id, "c": scope.collection_id},
            )
        ).scalar_one()
    )


__all__ = ["StoredChunk", "by_ids", "chunk_id", "count_for", "for_item", "keyword_search", "rebuild"]
