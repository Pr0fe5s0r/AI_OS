"""Which documents a question is about, before any of them are opened.

The navigator reads its way to an answer, and to do that it must first be shown
what there is to read. That list was ``ORDER BY created_at DESC LIMIT 40``: on a
store of forty documents it is every document and the ordering is irrelevant; on
a store of a hundred it is the forty most recently uploaded and **the other
sixty do not exist as far as the answer is concerned**. Not ranked low —
absent. Asked something only document seventy could answer, the store said the
collection did not cover it, which is the most expensive sentence it can say.

So this module answers one question: given the question, WHICH documents should
be laid out for the agent to choose from?

Three ways, in order of how much they can be trusted:

  named       the caller passed item_ids — no routing at all, those documents
              and nothing else. A filter is an instruction, not a hint.
  everything  the store holds no more documents than the cap. Every document
              is shown, exactly as before this module existed. This is the
              safety property: a small store cannot regress, because nothing
              about it changed.
  routed      the store is larger than the cap, so something has to choose.
              Cards and passages both vote; see ``_rank``.

That middle case is deliberate and load-bearing. Routing is a guess, and a
guess that runs when it does not need to is a way to be wrong for free.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text as sql
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core import graph
from packages.shared.schema import Lifecycle, Scope


def _route_limit() -> int:
    """How many documents a routed question may consider.

    Read from the navigator rather than duplicated, because the two must agree:
    routing that ranked more documents than the navigator will lay out would
    hand back a list that gets silently truncated, and the documents dropped
    would be the ones routing rated LOWEST — which sounds harmless right up
    until the cut lands mid-ranking and nobody can say where.
    """
    from packages.core.navigator import MAX_DOCUMENTS

    return MAX_DOCUMENTS

# How many passage hits to ask for when routing by content. Higher than a
# normal search's limit because passages are being rolled up into documents:
# forty passages that all belong to three documents is three votes, not forty.
PASSAGE_SAMPLE = 40

# What a card match is worth against a passage match when both fire.
#
# A card describes the WHOLE document and is what actually answers "which file
# is this about" — it is the better routing signal, and it is weighted above
# passages for that reason. But it is model-written and it exists only where
# summaries were generated, so it can be confidently wrong and is often simply
# missing. It leads; it does not decide alone.
CARD_WEIGHT = 1.4
PASSAGE_WEIGHT = 1.0


@dataclass
class Routing:
    """Which documents to open, and why — the second half being the point.

    A router that returns ids and no reasons cannot be argued with: when it
    sends the agent to the wrong document there is nothing to look at. Every
    document here carries the signal that put it in the list.
    """

    item_ids: list[str] = field(default_factory=list)
    how: str = "everything"  # named | everything | ranked | routed
    # Every document, best match first. Populated for "ranked" and "routed"
    # alike — the difference between them is whether anything was EXCLUDED, not
    # whether the ranking happened. Kept separate from ``item_ids`` for exactly
    # that reason: item_ids means "load only these", order means "these look
    # most relevant, in this order", and conflating the two is how a ranking
    # silently becomes a filter.
    order: list[str] = field(default_factory=list)
    # item_id -> a short human sentence about why it is here.
    because: dict[str, str] = field(default_factory=dict)
    # How many documents the store holds in scope, whether or not they fit.
    available: int = 0
    # Set when routing ran but found nothing to rank on, and recency was used
    # as a last resort. Worth surfacing: it is the one case where the answer
    # may be missing a document for no better reason than its upload date.
    fell_back: bool = False

    @property
    def routed(self) -> bool:
        return self.how == "routed"

    def as_dict(self) -> dict[str, Any]:
        return {
            "how": self.how,
            "documents": len(self.item_ids),
            "available": self.available,
            "fell_back": self.fell_back,
        }


async def count_documents(session: AsyncSession, scope: Scope) -> int:
    """How many live documents are in scope. One cheap count, and it decides
    whether any of the rest of this module needs to run at all."""
    clause = "AND collection_id = :c" if scope.collection_id else ""
    row = (
        await session.execute(
            sql(
                f"""
                SELECT count(*) AS n FROM kb_items
                WHERE workspace_id = :w AND status = :active {clause}
                """  # noqa: S608 - clause is a fixed literal, not input
            ),
            {
                "w": scope.workspace_id,
                "c": scope.collection_id,
                "active": str(Lifecycle.ACTIVE),
            },
        )
    ).one()
    return int(row.n)


async def choose(
    session: AsyncSession,
    scope: Scope,
    question: str,
    *,
    only: tuple[str, ...] = (),
    limit: int | None = None,
) -> Routing:
    """The documents this question should be answered from.

    ``only`` is the explicit filter and short-circuits everything: a caller who
    names documents has told us where to look, and second-guessing that with a
    similarity score would make the filter advisory. It is not advisory.
    """
    limit = _route_limit() if limit is None else limit
    if only:
        return Routing(
            item_ids=list(only),
            how="named",
            because={i: "named in the request" for i in only},
            available=len(only),
        )

    available = await count_documents(session, scope)
    if available <= 1:
        # One document (or none) cannot be ranked against itself.
        return Routing(item_ids=[], how="everything", available=available)

    ranked, because, fell_back = await _rank(session, scope, question, limit or available)
    if not ranked:
        # Nothing to rank on: no cards, and search returned nothing or failed.
        # Recency is a poor answer and is reported as one rather than being
        # presented as a choice.
        return Routing(item_ids=[], how="everything", available=available, fell_back=True)

    if limit <= 0 or available <= limit:
        # Everything FITS — so nothing is excluded. But the ranking still runs
        # and is still reported, which is the whole point and was the bug.
        #
        # Before this, a store under the cap returned early and the card score
        # was computed for nobody. Measured on a four-document store asked
        # "server requirements": the cards separated cleanly — 0.778 for the
        # server document against 0.614 for a Harry Potter collection — and the
        # agent was told none of it. It searched all 11,460 passages of the
        # novel, read two of them, and wrote a paragraph explaining that Harry
        # Potter is unrelated to server requirements. It was right, and it
        # should never have had to work that out.
        #
        # ``item_ids`` stays empty so the navigator still lays out EVERY
        # document. This is guidance, not exclusion: a ranking says where to
        # look first and must never be able to hide the answer.
        return Routing(
            item_ids=[],
            how="ranked",
            because=because,
            order=ranked,
            available=available,
            fell_back=fell_back,
        )

    return Routing(
        item_ids=ranked,
        how="routed",
        because=because,
        order=ranked,
        available=available,
        fell_back=fell_back,
    )


async def _rank(
    session: AsyncSession, scope: Scope, question: str, limit: int
) -> tuple[list[str], dict[str, str], bool]:
    """Score every document that either arm surfaced, and take the best.

    Both arms are run concurrently and either is allowed to fail. Routing is an
    improvement on "the forty newest", not a dependency — if the card index is
    empty and search is down, the caller falls back rather than the question
    failing.
    """
    cards, passages = await asyncio.gather(
        _card_votes(scope, question),
        _passage_votes(session, scope, question),
        return_exceptions=True,
    )
    card_scores = cards if isinstance(cards, dict) else {}
    passage_scores, reasons = passages if isinstance(passages, tuple) else ({}, {})

    total: dict[str, float] = {}
    because: dict[str, str] = {}
    for item_id, score in card_scores.items():
        total[item_id] = total.get(item_id, 0.0) + score * CARD_WEIGHT
        because[item_id] = "its summary matches the question"
    for item_id, score in passage_scores.items():
        total[item_id] = total.get(item_id, 0.0) + score * PASSAGE_WEIGHT
        # A passage reason is more specific than a card one — it can quote the
        # text that matched — so it wins the description where both fired.
        because[item_id] = reasons.get(item_id, "a passage in it matches")

    ordered = sorted(total, key=lambda i: total[i], reverse=True)[:limit]
    return ordered, {i: because[i] for i in ordered}, not card_scores


async def _card_votes(scope: Scope, question: str) -> dict[str, float]:
    """Documents whose card is close to the question.

    Empty when summaries have never been generated, which is the default. That
    is not an error and not worth logging on every question — it is simply an
    index this store has not built, and the passage arm covers for it.
    """
    from packages.core.llm import embed

    try:
        vector = await asyncio.to_thread(embed, question)
        rows = await graph.card_matches(scope, vector, k=_route_limit())
    except Exception:
        return {}
    best: dict[str, float] = {}
    for row in rows:
        item_id = row.get("item_id")
        if not item_id:
            continue
        score = float(row.get("similarity") or 0.0)
        if score > best.get(item_id, -1.0):
            best[item_id] = score
    return best


async def _passage_votes(
    session: AsyncSession, scope: Scope, question: str
) -> tuple[dict[str, float], dict[str, str]]:
    """Documents holding a passage that matches, and the passage that did it.

    This is the arm that works on every store today, because it reuses the
    index that already exists. It is also the honest rebuttal to "vectorless
    does not use vectors": it does not use them to ANSWER — every word of the
    answer still comes from a section the agent opened and read — but it is
    allowed to use them to decide which door to walk through.
    """
    from packages.core.search import RetrievalConfig, search

    try:
        hits = await search(session, scope, question, RetrievalConfig(limit=PASSAGE_SAMPLE))
    except Exception:
        return {}, {}

    scores: dict[str, float] = {}
    reasons: dict[str, str] = {}
    for rank, hit in enumerate(hits):
        # Rank, not raw score: the two arms are on different scales and adding
        # them directly would let whichever happens to run hotter win every
        # time. A reciprocal rank is bounded, comparable and hard to game.
        scores[hit.item_id] = max(scores.get(hit.item_id, 0.0), 1.0 / (rank + 1))
        if hit.item_id not in reasons and hit.passages:
            snippet = " ".join(hit.passages[0].text.split())[:80]
            reasons[hit.item_id] = f"contains: “{snippet}…”"
    return scores, reasons
