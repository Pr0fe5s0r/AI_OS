from __future__ import annotations

import asyncio
import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core import graph
from packages.core.llm import embed

# Hybrid search:
#   - semantic recall: Neo4j native vector index (db.index.vector.queryNodes)
#   - keyword recall:  Postgres tsvector full-text over the same events
# Both result sets are merged and ranked by similarity + recency, with the
# text rank breaking in keyword-only hits. Content always comes from Postgres —
# the graph only holds the mirror + embedding.

_HYDRATE = text(
    """
    SELECT
        e.id, e.company_id, e.source, e.type, e.actor_id, e.actor_name,
        e.timestamp, e.content, e.metadata,
        exp(-EXTRACT(EPOCH FROM (now() - e.timestamp)) / 86400.0 / 30.0) AS recency,
        ts_rank(e.content_tsv, plainto_tsquery('english', :query)) AS text_rank
    FROM events e
    WHERE e.company_id = :company_id AND e.id = ANY(:ids)
    """
)

_TEXT_HITS = text(
    """
    SELECT e.id
    FROM events e
    WHERE e.company_id = :company_id
      AND e.content_tsv @@ plainto_tsquery('english', :query)
    ORDER BY ts_rank(e.content_tsv, plainto_tsquery('english', :query)) DESC
    LIMIT :limit
    """
)


def _row_to_dict(row: Any, similarity: float) -> dict[str, Any]:
    m = row.metadata
    if isinstance(m, str):
        m = json.loads(m)
    recency = float(row.recency)
    text_rank = float(row.text_rank)
    return {
        "id": row.id,
        "company_id": row.company_id,
        "source": row.source,
        "type": row.type,
        "actor": {"id": row.actor_id, "name": row.actor_name},
        "timestamp": row.timestamp.isoformat(),
        "content": row.content,
        "metadata": m,
        "similarity": round(similarity, 4),
        "recency": round(recency, 4),
        "text_rank": round(text_rank, 4),
        # similarity dominates, recency breaks ties, keyword hits get a floor
        "score": round(0.7 * max(similarity, min(text_rank, 0.5)) + 0.3 * recency, 4),
    }


async def search(
    session: AsyncSession,
    company_id: str,
    query: str,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Rank a company's events against ``query`` (vector + full-text + recency)."""
    vector = await asyncio.to_thread(embed, query)

    semantic = await graph.vector_search(company_id, vector, limit=limit)
    sim_by_id = {h["event_id"]: float(h["similarity"]) for h in semantic}

    text_rows = await session.execute(
        _TEXT_HITS, {"company_id": company_id, "query": query, "limit": limit}
    )
    ids = set(sim_by_id) | {r.id for r in text_rows}
    if not ids:
        return []

    rows = await session.execute(
        _HYDRATE, {"company_id": company_id, "query": query, "ids": list(ids)}
    )
    results = [_row_to_dict(r, sim_by_id.get(r.id, 0.0)) for r in rows]
    results.sort(key=lambda r: r["score"], reverse=True)
    return results[:limit]
