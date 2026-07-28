from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass, field
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
# keyword finds "SKU-4471" and proper nouns, which embeddings routinely miss.
# Fusing them is what makes retrieval reliable enough to answer from, which is
# why there is no re-ranking stage here: hybrid already returns the right
# content, and re-ranking would add cost and latency for marginal reordering.
#
# Every call records a Trace: what each arm proposed, what survived the
# filters, what came back and how long each stage took. Retrieval that cannot
# explain itself is retrieval nobody can debug — and the same inputs against
# the same index reproduce the same output, which is as deterministic as
# retrieval gets.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class RetrievalConfig:
    """What one caller asks for. Behaviour is varied by these values rather
    than by bespoke code per agent, so adding an agent is configuration."""

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

    def as_dict(self) -> dict[str, Any]:
        return {
            "limit": self.limit,
            "min_score": self.min_score,
            "semantic_weight": self.semantic_weight,
            "sources": list(self.sources),
            "period_from": self.period_from.isoformat() if self.period_from else None,
            "period_to": self.period_to.isoformat() if self.period_to else None,
            "include_superseded": self.include_superseded,
            "recall_multiplier": self.recall_multiplier,
        }


DEFAULT = RetrievalConfig()

# Candidate lists are capped before they are stored. A trace is written on the
# read path, and an unbounded one would make searching more expensive the more
# there is to search.
_MAX_RECORDED = 50


@dataclass(slots=True)
class Trace:
    """Why this answer came back."""

    trace_id: str
    query: str
    config: dict[str, Any]
    filters: dict[str, Any]
    semantic: list[dict[str, Any]] = field(default_factory=list)
    keyword: list[dict[str, Any]] = field(default_factory=list)
    fused: list[dict[str, Any]] = field(default_factory=list)
    returned: list[str] = field(default_factory=list)
    timings_ms: dict[str, int] = field(default_factory=dict)
    degraded: str | None = None

    @property
    def duration_ms(self) -> int:
        return self.timings_ms.get("total", 0)


def new_trace_id() -> str:
    return secrets.token_hex(12)


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
           -- Custom delimiters, not the default <b></b>: the excerpt is data
           -- travelling to an API and then to a browser, and HTML in it either
           -- renders as literal tags or has to be trusted as markup. Neither is
           -- acceptable, so the marks are inert tokens the client can style.
           ts_headline(
               'english', body, plainto_tsquery('english', :q),
               'MaxWords=40, MinWords=15, ShortWord=3, MaxFragments=1,'
               'StartSel="[[", StopSel="]]"'
           ) AS excerpt,
           left(body, 320) AS head,
           -- Age is floored at zero before the decay is applied. A document
           -- describing a period that has not finished yet — a plan for the
           -- year, a forecast — has a negative age, and exp(-negative) grows
           -- without bound: one such item scored 2.14 and outranked results
           -- with far higher similarity. Content about the future is as recent
           -- as content about today, and no more.
           exp(-GREATEST(
                   EXTRACT(EPOCH FROM (now() - COALESCE(period_end, created_at))), 0
               ) / 86400.0 / 90.0) AS recency
    FROM kb_items
    WHERE {where} AND item_id = ANY(:ids)
"""


def _filters(scope: Scope, cfg: RetrievalConfig) -> tuple[str, dict[str, Any]]:
    """Scope, lifecycle, source and period — applied to every arm alike."""
    clauses = ["workspace_id = :workspace"]
    params: dict[str, Any] = {"workspace": scope.workspace_id}
    if scope.collection_id is not None:
        clauses.append("collection_id = :collection")
        params["collection"] = scope.collection_id
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
    """Rank items against ``query``. The store's primary interface."""
    hits, _ = await search_traced(session, scope, query, cfg)
    return hits


async def search_traced(
    session: AsyncSession,
    scope: Scope,
    query: str,
    cfg: RetrievalConfig = DEFAULT,
) -> tuple[list[Hit], Trace]:
    """Search, and account for itself.

    The trace is built whether or not anyone stores it, because a trace
    assembled only when asked for would not describe the call that actually
    happened.
    """
    started = time.perf_counter()
    where, params = _filters(scope, cfg)
    trace = Trace(
        trace_id=new_trace_id(),
        query=query,
        config=cfg.as_dict(),
        filters={
            "workspace_id": scope.workspace_id,
            "collection_id": scope.collection_id,
            "sources": list(cfg.sources),
            "include_superseded": cfg.include_superseded,
        },
    )

    def elapsed(since: float) -> int:
        return int((time.perf_counter() - since) * 1000)

    if not query.strip():
        trace.timings_ms = {"total": elapsed(started)}
        return [], trace

    k = cfg.candidates()

    # --- semantic arm ---------------------------------------------------
    # Embedding is network-bound and synchronous, so it runs off the event
    # loop. A vector store outage degrades the call to keyword-only rather
    # than failing it — but the trace says so, because a degraded answer that
    # looks identical to a healthy one is the worst kind.
    mark = time.perf_counter()
    semantic: list[dict[str, Any]] = []
    try:
        vector = await asyncio.to_thread(embed, query)
        trace.timings_ms["embed"] = elapsed(mark)
        mark = time.perf_counter()
        semantic = await graph.vector_search(
            scope, vector, limit=k, active_only=not cfg.include_superseded
        )
        trace.timings_ms["semantic"] = elapsed(mark)
    except Exception as exc:
        trace.degraded = f"semantic arm unavailable: {type(exc).__name__}"
        trace.timings_ms["semantic"] = elapsed(mark)

    sim_by_id = {r["item_id"]: float(r["similarity"]) for r in semantic}
    trace.semantic = [
        {"item_id": r["item_id"], "similarity": round(float(r["similarity"]), 4)}
        for r in semantic[:_MAX_RECORDED]
    ]

    # --- keyword arm ----------------------------------------------------
    mark = time.perf_counter()
    rows = await session.execute(
        text(_KEYWORD.format(where=where)), {**params, "q": query, "limit": k}
    )
    rank_by_id = {r.item_id: float(r.rank) for r in rows}
    trace.timings_ms["keyword"] = elapsed(mark)
    trace.keyword = [
        {"item_id": i, "rank": round(v, 4)}
        for i, v in list(rank_by_id.items())[:_MAX_RECORDED]
    ]

    ids = list(sim_by_id | rank_by_id)
    if not ids:
        trace.timings_ms["total"] = elapsed(started)
        return [], trace

    # --- fusion ---------------------------------------------------------
    # The semantic arm queries the graph, which knows nothing of period or
    # source filters, so hydration re-applies the full filter set. Anything the
    # vector index offered that the caller is not entitled to see dies here —
    # and the trace shows the drop, which is how a filter bug becomes visible.
    mark = time.perf_counter()
    hydrated = await session.execute(
        text(_HYDRATE.format(where=where)), {**params, "q": query, "ids": ids}
    )

    scored: list[tuple[Hit, dict[str, Any]]] = []
    for row in hydrated:
        similarity = sim_by_id.get(row.item_id, 0.0)
        keyword = rank_by_id.get(row.item_id, 0.0)
        # A keyword-only hit still deserves a floor score: an exact identifier
        # match is a strong signal even when the embedding disagrees.
        relevance = max(similarity, min(keyword * 2.0, 0.6))
        recency = float(row.recency)
        score = cfg.semantic_weight * relevance + (1 - cfg.semantic_weight) * recency
        detail = {
            "item_id": row.item_id,
            "score": round(score, 4),
            "semantic": round(similarity, 4),
            "keyword": round(keyword, 4),
            "recency": round(recency, 4),
            "kept": score >= cfg.min_score,
        }
        if score < cfg.min_score:
            scored.append((None, detail))  # type: ignore[arg-type]
            continue
        scored.append(
            (
                Hit(
                    item_id=row.item_id,
                    title=row.title,
                    excerpt=(row.excerpt or row.head or "").strip(),
                    source=SourceRef(source=row.source, locator=row.locator, url=row.url),
                    score=round(score, 4),
                    semantic=round(similarity, 4),
                    keyword=round(keyword, 4),
                ),
                detail,
            )
        )
    trace.timings_ms["hydrate"] = elapsed(mark)

    scored.sort(key=lambda pair: pair[1]["score"], reverse=True)
    trace.fused = [d for _, d in scored[:_MAX_RECORDED]]

    hits = [h for h, _ in scored if h is not None][: cfg.limit]
    trace.returned = [h.item_id for h in hits]
    trace.timings_ms["total"] = elapsed(started)
    return hits, trace
