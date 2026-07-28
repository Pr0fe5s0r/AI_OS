from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core import graph
from packages.core.llm import embed
from packages.shared.schema import Hit, Lifecycle, Scope, SourceRef

# ---------------------------------------------------------------------------
# HYBRID SEARCH — semantic recall and keyword recall, fused.
#
#   semantic : Neo4j native vector index, cosine over item embeddings
#   keyword  : Postgres tsvector over the same items
#
# Both are needed and neither is sufficient. Semantic finds a document about
# "quarterly performance dip" when the query says "why did results fall";
# keyword finds "SKU-4471" and brand names, which embeddings routinely miss.
# Fusing them is what makes retrieval reliable enough to answer from, which is
# why there is no re-ranking stage here: hybrid already returns the right
# content, and re-ranking would add cost and latency for marginal reordering.
#
# Everything is scoped and filtered before it is returned. Superseded items are
# excluded by default — answering from content we know has been replaced is a
# correctness failure, not a ranking preference.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class RetrievalConfig:
    """What one agent asks for. The per-agent row behind the retrieval contract.

    Behaviour is varied by these values rather than by bespoke code per agent,
    so adding an agent is a configuration change (KB-5.0).
    """

    limit: int = 10
    min_score: float = 0.0
    semantic_weight: float = 0.7  # the remainder goes to recency
    sources: tuple[str, ...] = ()  # empty = every source
    period_from: datetime | None = None
    period_to: datetime | None = None
    include_superseded: bool = False
    recall_multiplier: int = 4  # candidates fetched per arm before fusing

    def candidates(self) -> int:
        return max(self.limit * self.recall_multiplier, self.limit)


DEFAULT = RetrievalConfig()


_KEYWORD = """
    SELECT item_id,
           ts_rank(body_tsv, plainto_tsquery('english', :q)) AS rank
    FROM kb_items
    WHERE {where}
      AND body_tsv @@ plainto_tsquery('english', :q)
    ORDER BY rank DESC
    LIMIT :limit
"""

_HYDRATE = """
    SELECT item_id, title, source, locator, url, created_at,
           ts_headline(
               'english', body, plainto_tsquery('english', :q),
               'MaxWords=40, MinWords=15, ShortWord=3, MaxFragments=1'
           ) AS excerpt,
           left(body, 320) AS head,
           exp(-EXTRACT(EPOCH FROM (now() - COALESCE(period_end, created_at)))
               / 86400.0 / 90.0) AS recency
    FROM kb_items
    WHERE {where} AND item_id = ANY(:ids)
"""


def _filters(scope: Scope, cfg: RetrievalConfig) -> tuple[str, dict[str, Any]]:
    """Tenancy, lifecycle, source and period — applied to every arm alike."""
    clauses = ["tenant_id = :tenant"]
    params: dict[str, Any] = {"tenant": scope.tenant_id}
    if scope.brand_id is not None:
        clauses.append("brand_id = :brand")
        params["brand"] = scope.brand_id
    if not cfg.include_superseded:
        clauses.append("status = :active")
        params["active"] = str(Lifecycle.ACTIVE)
    if cfg.sources:
        clauses.append("source = ANY(:sources)")
        params["sources"] = list(cfg.sources)
    # Time-aware: filter on the period the content DESCRIBES, falling back to
    # when it was created. A July report about Q2 must match a Q2 query.
    if cfg.period_from is not None:
        clauses.append("COALESCE(period_end, created_at) >= :period_from")
        params["period_from"] = cfg.period_from
    if cfg.period_to is not None:
        clauses.append("COALESCE(period_start, created_at) <= :period_to")
        params["period_to"] = cfg.period_to
    return " AND ".join(clauses), params


async def search(
    session: AsyncSession,
    scope: Scope,
    query: str,
    cfg: RetrievalConfig = DEFAULT,
) -> list[Hit]:
    """Rank items against ``query``. The KB's primary interface."""
    if not query.strip():
        return []

    where, params = _filters(scope, cfg)
    k = cfg.candidates()

    # Semantic arm. Embedding is CPU/network-bound and sync, so it runs off the
    # event loop; a vector store outage degrades to keyword-only rather than
    # failing the whole call.
    try:
        vector = await asyncio.to_thread(embed, query)
        semantic = await graph.vector_search(
            scope, vector, limit=k, active_only=not cfg.include_superseded
        )
    except Exception:
        semantic = []
    sim_by_id = {r["item_id"]: float(r["similarity"]) for r in semantic}

    # Keyword arm.
    rows = await session.execute(
        text(_KEYWORD.format(where=where)), {**params, "q": query, "limit": k}
    )
    rank_by_id = {r.item_id: float(r.rank) for r in rows}

    ids = list(sim_by_id | rank_by_id)
    if not ids:
        return []

    # The semantic arm queries the graph, which has no knowledge of period or
    # source filters — so hydration re-applies the full filter set. Anything the
    # vector index offered that the caller is not entitled to see dies here.
    hydrated = await session.execute(
        text(_HYDRATE.format(where=where)), {**params, "q": query, "ids": ids}
    )

    hits: list[Hit] = []
    for row in hydrated:
        similarity = sim_by_id.get(row.item_id, 0.0)
        keyword = rank_by_id.get(row.item_id, 0.0)
        # A keyword-only hit still deserves a floor score: an exact identifier
        # match is a strong signal even when the embedding disagrees.
        relevance = max(similarity, min(keyword * 2.0, 0.6))
        score = cfg.semantic_weight * relevance + (1 - cfg.semantic_weight) * float(row.recency)
        if score < cfg.min_score:
            continue
        hits.append(
            Hit(
                item_id=row.item_id,
                title=row.title,
                excerpt=(row.excerpt or row.head or "").strip(),
                source=SourceRef(source=row.source, locator=row.locator, url=row.url),
                score=round(score, 4),
                semantic=round(similarity, 4),
                keyword=round(keyword, 4),
            )
        )

    hits.sort(key=lambda h: h.score, reverse=True)
    return hits[: cfg.limit]
