from __future__ import annotations

import asyncio
import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.embeddings import embed
from packages.core.store import _to_vector_literal

# Hybrid search:
#   - semantic recall via pgvector cosine distance (<=>)
#   - keyword signal available via the tsvector column (ts_rank)
# Final rank is exactly 0.7 * similarity + 0.3 * recency, per spec. The tsvector
# is surfaced as `text_rank` so keyword hits are visible/usable by callers.
_SEARCH = text(
    """
    SELECT
        e.id,
        e.company_id,
        e.source,
        e.type,
        e.actor_id,
        e.actor_name,
        e.timestamp,
        e.content,
        e.metadata,
        1 - (em.embedding <=> CAST(:embedding AS vector)) AS similarity,
        exp(-EXTRACT(EPOCH FROM (now() - e.timestamp)) / 86400.0 / 30.0) AS recency,
        ts_rank(e.content_tsv, plainto_tsquery('english', :query)) AS text_rank
    FROM events e
    JOIN event_embeddings em ON em.event_id = e.id
    WHERE e.company_id = :company_id
    ORDER BY (
        0.7 * (1 - (em.embedding <=> CAST(:embedding AS vector)))
        + 0.3 * exp(-EXTRACT(EPOCH FROM (now() - e.timestamp)) / 86400.0 / 30.0)
    ) DESC
    LIMIT :limit
    """
)


def _row_to_dict(row: Any) -> dict[str, Any]:
    m = row.metadata
    if isinstance(m, str):
        m = json.loads(m)
    similarity = float(row.similarity)
    recency = float(row.recency)
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
        "text_rank": round(float(row.text_rank), 4),
        "score": round(0.7 * similarity + 0.3 * recency, 4),
    }


async def search(
    session: AsyncSession,
    company_id: str,
    query: str,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Rank a company's events against ``query`` (semantic + recency)."""
    vector = await asyncio.to_thread(embed, query)
    result = await session.execute(
        _SEARCH,
        {
            "embedding": _to_vector_literal(vector),
            "query": query,
            "company_id": company_id,
            "limit": limit,
        },
    )
    return [_row_to_dict(r) for r in result]
