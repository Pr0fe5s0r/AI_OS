from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core import chunks, graph
from packages.core.llm import embed
from packages.shared.schema import Hit, Lifecycle, Passage, Scope, SourceRef

# ---------------------------------------------------------------------------
# HYBRID SEARCH — semantic recall and keyword recall, fused.
#
#   semantic : Neo4j native vector index, cosine over passage embeddings
#   keyword  : Postgres tsvector over the same passages
#
# Both arms retrieve PASSAGES and roll up to documents, scoring each document
# by its best passage. A specification answering the question in one paragraph
# should outrank one vaguely on topic throughout, and averaging over a
# document's passages inverts exactly that. The passage that won is carried
# through to the result, because that is what a citation is.
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
    item_ids: tuple[str, ...] = ()  # empty = every document; else search only these
    period_from: datetime | None = None
    period_to: datetime | None = None
    include_superseded: bool = False
    recall_multiplier: int = 4  # candidates fetched per arm before fusing

    def candidates(self) -> int:
        base = max(self.limit * self.recall_multiplier, self.limit)
        # When the search is confined to specific documents, cast a much wider
        # net per arm so their passages survive to hydration even in a large
        # collection — the item filter there then keeps only them.
        if self.item_ids:
            return max(base, 200)
        return base

    def as_dict(self) -> dict[str, Any]:
        return {
            "limit": self.limit,
            "min_score": self.min_score,
            "semantic_weight": self.semantic_weight,
            "sources": list(self.sources),
            "item_ids": list(self.item_ids),
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


# Embedding the QUERY dominates a search: a trace showed 13,198ms of a 13,333ms
# call, so the store answered in 130ms and spent thirteen seconds asking a
# provider what the question meant. Repeat questions are the common case — a
# console refresh, a dashboard, two agents asking the same thing — so the
# result is kept. Bounded, and keyed on the exact string: an embedding is a
# pure function of its input and the model, and the model cannot change without
# a restart.
_QUERY_CACHE_SIZE = 512


@lru_cache(maxsize=_QUERY_CACHE_SIZE)
def _embed_query_cached(query: str) -> tuple[float, ...]:
    # A tuple, not a list: lru_cache hands every caller the same object, and a
    # caller that mutated a cached list would corrupt every later search.
    return tuple(embed(query))


def embed_query(query: str) -> list[float]:
    return list(_embed_query_cached(query))


_HYDRATE = """
    SELECT item_id, title, source, locator, url, created_at,
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
    # Confine the search to specific documents. Applied at hydration like every
    # other filter, so a candidate from either arm that is not in the set is
    # dropped here and the trace shows it go.
    if cfg.item_ids:
        clauses.append("item_id = ANY(:item_ids)")
        params["item_ids"] = list(cfg.item_ids)
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
            "item_ids": list(cfg.item_ids),
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
        vector = await asyncio.to_thread(embed_query, query)
        trace.timings_ms["embed"] = elapsed(mark)
        mark = time.perf_counter()
        semantic = await graph.vector_search(
            scope, vector, limit=k, active_only=not cfg.include_superseded
        )
        trace.timings_ms["semantic"] = elapsed(mark)
    except Exception as exc:
        trace.degraded = f"semantic arm unavailable: {type(exc).__name__}"
        trace.timings_ms["semantic"] = elapsed(mark)

    # Both arms now return PASSAGES. A document's score is its best passage:
    # a specification that answers the question in one paragraph should rank
    # above one that is vaguely on topic throughout, and averaging over
    # passages would invert exactly that.
    sim_by_id: dict[str, float] = {}
    best_chunk: dict[str, str] = {}
    # Per-passage scores, kept so the result can show every place a document
    # matched rather than only the strongest.
    chunk_semantic: dict[str, float] = {}
    chunk_owner: dict[str, str] = {}
    for candidate in semantic:
        item_id, similarity = candidate["item_id"], float(candidate["similarity"])
        chunk_semantic[candidate["chunk_id"]] = similarity
        chunk_owner[candidate["chunk_id"]] = item_id
        if similarity > sim_by_id.get(item_id, -1.0):
            sim_by_id[item_id] = similarity
            best_chunk[item_id] = candidate["chunk_id"]
    trace.semantic = [
        {
            "item_id": r["item_id"],
            "chunk_id": r["chunk_id"],
            "ordinal": r["ordinal"],
            "similarity": round(float(r["similarity"]), 4),
        }
        for r in semantic[:_MAX_RECORDED]
    ]

    # --- keyword arm ----------------------------------------------------
    mark = time.perf_counter()
    passages = await chunks.keyword_search(session, scope, query, limit=k)
    rank_by_id: dict[str, float] = {}
    excerpt_by_id: dict[str, str] = {}
    best_keyword_chunk: dict[str, str] = {}
    chunk_keyword: dict[str, float] = {}
    for hit_row in passages:
        item_id, rank = hit_row["item_id"], hit_row["score"]
        chunk_keyword[hit_row["chunk_id"]] = rank
        chunk_owner[hit_row["chunk_id"]] = item_id
        if rank > rank_by_id.get(item_id, -1.0):
            rank_by_id[item_id] = rank
            # The keyword arm can produce a highlighted excerpt directly, which
            # is better than one computed over the whole document: it points at
            # the passage that actually matched.
            excerpt_by_id[item_id] = hit_row["excerpt"]
            best_keyword_chunk[item_id] = hit_row["chunk_id"]
            best_chunk.setdefault(item_id, hit_row["chunk_id"])
    trace.timings_ms["keyword"] = elapsed(mark)
    trace.keyword = [
        {
            "item_id": r["item_id"],
            "chunk_id": r["chunk_id"],
            "heading": r["heading"],
            "rank": round(r["score"], 4),
        }
        for r in passages[:_MAX_RECORDED]
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
    # Every passage either arm proposed, fetched in one go rather than per row.
    # The winning one becomes the excerpt; the rest become the evidence.
    winning = await chunks.by_ids(session, scope, list(chunk_owner))

    # Passages grouped by the document they belong to, best first. A document
    # matching in six places is a materially different answer from one matching
    # in a single line, and a document-level score cannot say which it is.
    per_item: dict[str, list[Passage]] = {}
    for cid, owner in chunk_owner.items():
        stored = winning.get(cid)
        if stored is None:
            continue
        sem, kw = chunk_semantic.get(cid, 0.0), chunk_keyword.get(cid, 0.0)
        per_item.setdefault(owner, []).append(
            Passage(
                chunk_id=cid,
                ordinal=stored.ordinal,
                heading=stored.heading,
                text=stored.text[:600],
                score=round(max(sem, min(kw * 2.0, 0.6)), 4),
                semantic=round(sem, 4),
                keyword=round(kw, 4),
            )
        )
    for group in per_item.values():
        group.sort(key=lambda p: p.score, reverse=True)

    scored: list[tuple[Hit, dict[str, Any]]] = []
    for row in hydrated:
        similarity = sim_by_id.get(row.item_id, 0.0)
        keyword = rank_by_id.get(row.item_id, 0.0)
        # A keyword-only hit still deserves a floor score: an exact identifier
        # match is a strong signal even when the embedding disagrees.
        relevance = max(similarity, min(keyword * 2.0, 0.6))
        recency = float(row.recency)
        score = cfg.semantic_weight * relevance + (1 - cfg.semantic_weight) * recency
        # ONE winning passage, and both the heading and the excerpt come from
        # it. Taking the heading from the vector arm's best and the excerpt
        # from the keyword arm's best let them disagree: a result was cited as
        # "7. Deliverables" while quoting text from the connectors section,
        # which is a citation pointing at the wrong place — worse than no
        # citation, because it looks checkable and is not.
        ranked_passages = per_item.get(row.item_id, [])
        winner = ranked_passages[0] if ranked_passages else None
        detail = {
            "item_id": row.item_id,
            "chunk_id": winner.chunk_id if winner else None,
            "heading": winner.heading if winner else None,
            "score": round(score, 4),
            "semantic": round(similarity, 4),
            "keyword": round(keyword, 4),
            "recency": round(recency, 4),
            "kept": score >= cfg.min_score,
        }
        if score < cfg.min_score:
            scored.append((None, detail))  # type: ignore[arg-type]
            continue
        # The keyword arm's highlighted version is preferred, but only when it
        # is the SAME passage that won — otherwise the winner's own text.
        highlighted = excerpt_by_id.get(row.item_id)
        excerpt = (
            highlighted
            if winner and highlighted and best_keyword_chunk.get(row.item_id) == winner.chunk_id
            else (winner.text if winner else "") or row.head
        )
        scored.append(
            (
                Hit(
                    item_id=row.item_id,
                    title=row.title,
                    excerpt=(excerpt or "").strip()[:600],
                    source=SourceRef(source=row.source, locator=row.locator, url=row.url),
                    score=round(score, 4),
                    semantic=round(similarity, 4),
                    keyword=round(keyword, 4),
                    heading=winner.heading if winner else "",
                    passages=ranked_passages[:5],
                ),
                detail,
            )
        )
    trace.timings_ms["hydrate"] = elapsed(mark)

    # What gets used survives; what is never used decays. Retrieval is the only
    # evidence the store has about which passages matter, so it is recorded
    # here — after fusion, so a passage that was fetched and then filtered out
    # does not count as used.
    from packages.core.consolidate import touch

    await touch(session, scope, [p.chunk_id for h, _ in scored if h for p in h.passages])

    scored.sort(key=lambda pair: pair[1]["score"], reverse=True)
    trace.fused = [d for _, d in scored[:_MAX_RECORDED]]

    hits = [h for h, _ in scored if h is not None][: cfg.limit]
    trace.returned = [h.item_id for h in hits]
    trace.timings_ms["total"] = elapsed(started)
    return hits, trace
