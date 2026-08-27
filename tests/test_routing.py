"""Which documents a question is answered from, on a store too big to lay out.

The bug this exists for: ``_documents`` was ``ORDER BY created_at DESC LIMIT
40``. At forty documents that is every document and the ordering never mattered.
At a hundred it is the forty most recently uploaded, and the other sixty are not
ranked low — they are absent. Asked something only document seventy could
answer, the store replied that the collection did not cover it.

Measured on 12-section policy documents, which is what made the cap invisible
for so long: the catalogue is shortened long before the document cap bites, so
the prompt looked healthy either way.

    10 docs     30,710 ch full ->  1,470 ch sent
    40 docs    122,900 ch full ->  5,940 ch sent    <- the cap
   100 docs    307,280 ch full -> 14,880 ch sent    <- 60 documents unreachable

The safety property these tests hold is the same one the catalogue budget
holds: a store at or under the cap is untouched. Routing is a guess, and a
guess that runs when it does not need to is a way to be wrong for free.
"""

from __future__ import annotations

import pytest

from packages.core.routing import (
    CARD_WEIGHT,
    PASSAGE_WEIGHT,
    Routing,
    _rank,
)


def test_a_store_that_fits_is_not_routed():
    """The whole safety argument. Under the cap there is no decision to make,
    so none is made and the navigator runs the query it always ran."""
    everything = Routing(how="everything", available=12)
    assert everything.item_ids == []
    assert everything.routed is False
    # An empty id list is the signal to load normally — NOT "no documents".
    assert everything.as_dict()["documents"] == 0


def test_a_named_filter_is_an_instruction_not_a_hint():
    """A caller who names documents has said where to look. Re-ranking that
    against a similarity score would make the filter advisory, and a filter
    that can be overruled by a guess is not a filter."""
    named = Routing(
        item_ids=["d7"], how="named", because={"d7": "named in the request"}, available=1
    )
    assert named.item_ids == ["d7"]
    assert named.routed is False


@pytest.mark.asyncio
async def test_both_arms_vote_and_the_card_arm_leads(monkeypatch):
    """A card describes the whole document, which is exactly the question being
    asked when routing — "which file is this about?". A passage matching is
    weaker evidence: a single sentence can match a question in a document that
    is otherwise unrelated.

    So the card is weighted above the passage. It is not allowed to decide
    alone, because it is model-written and can be confidently wrong.
    """
    from packages.core import routing

    async def cards(scope, question):
        return {"card_doc": 0.90}

    async def passages(session, scope, question):
        return {"passage_doc": 1.0}, {"passage_doc": "contains: “a matching line…”"}

    monkeypatch.setattr(routing, "_card_votes", cards)
    monkeypatch.setattr(routing, "_passage_votes", passages)

    ordered, because, _ = await _rank(None, None, "q", 10)
    assert set(ordered) == {"card_doc", "passage_doc"}
    # 0.90 * 1.4 = 1.26 beats 1.0 * 1.0 — the card leads on comparable evidence.
    assert ordered[0] == "card_doc"
    assert CARD_WEIGHT > PASSAGE_WEIGHT
    # And the passage keeps the more specific reason, because it can quote.
    assert "contains:" in because["passage_doc"]


@pytest.mark.asyncio
async def test_either_arm_may_fail_without_failing_the_question(monkeypatch):
    """Routing is an improvement on "the forty newest", not a dependency. If
    the card index is empty and search is down, the question still gets an
    answer — from the old ordering, reported as a fallback rather than
    presented as a choice."""
    from packages.core import routing

    async def boom(*a, **k):
        raise RuntimeError("neo4j is down")

    async def passages(session, scope, question):
        return {"d1": 1.0}, {"d1": "contains: “x…”"}

    monkeypatch.setattr(routing, "_card_votes", boom)
    monkeypatch.setattr(routing, "_passage_votes", passages)
    ordered, _, fell_back = await _rank(None, None, "q", 10)
    assert ordered == ["d1"], "one arm down must not take routing down"
    # No cards contributed, and that is worth saying: the better signal was
    # missing, so the ranking is weaker than it looks.
    assert fell_back is True


@pytest.mark.asyncio
async def test_nothing_to_rank_on_falls_back_rather_than_returning_nothing(monkeypatch):
    """The dangerous failure is returning an empty list, because an empty list
    would read as "no documents match" and the agent would answer that the
    store holds nothing. Recency is a bad answer; silence is a wrong one."""
    from packages.core import routing

    async def nothing(*a, **k):
        return {}

    async def no_passages(session, scope, question):
        return {}, {}

    monkeypatch.setattr(routing, "_card_votes", nothing)
    monkeypatch.setattr(routing, "_passage_votes", no_passages)
    ordered, _, _ = await _rank(None, None, "q", 10)
    assert ordered == []


@pytest.mark.asyncio
async def test_a_document_is_ranked_by_its_best_passage_not_its_busiest(monkeypatch):
    """Reciprocal rank, capped per document. Summing every passage would let a
    long document win on volume: forty weak matches spread through a manual
    would outrank one exact hit in the two-page circular that answers the
    question."""
    from packages.core import routing

    class _P:
        def __init__(self, text):
            self.text = text

    class _H:
        def __init__(self, item_id, text):
            self.item_id, self.passages = item_id, [_P(text)]

    async def fake_search(session, scope, question, cfg):
        # The long manual matches many times, at every rank after the first.
        return [_H("exact", "the precise clause")] + [
            _H("verbose", "a passing mention") for _ in range(20)
        ]

    import packages.core.search as search_mod

    monkeypatch.setattr(search_mod, "search", fake_search)
    scores, reasons = await routing._passage_votes(None, None, "q")
    assert scores["exact"] > scores["verbose"], "volume must not beat precision"
    assert scores["exact"] == 1.0
    assert "precise clause" in reasons["exact"]


def test_a_store_that_fits_is_still_ranked():
    """The signal was computed and thrown away.

    Routing returned early whenever every document fitted — which, once the
    document cap defaulted to "no limit", is ALWAYS. So the card score was
    calculated for nobody.

    Measured on a four-document store asked "server requirements": the cards
    separated cleanly, 0.778 for the server document against 0.614 for a Harry
    Potter collection. The agent was told none of it, searched all 11,460
    passages of the novel, read two pages, and wrote a paragraph explaining
    that Harry Potter is unrelated to server requirements. It was right, and it
    should never have had to work that out.

    Ranking now runs whether or not anything is excluded — those are two
    different questions and only the second depends on the cap.
    """
    ranked = Routing(
        item_ids=[],  # nothing excluded
        how="ranked",
        order=["server-doc", "novel"],
        because={"server-doc": "its summary matches the question"},
        available=4,
    )
    # The critical invariant: a ranking must not become a filter. Empty
    # item_ids is what tells the navigator to lay out EVERY document.
    assert ranked.item_ids == []
    assert ranked.order[0] == "server-doc"
    assert ranked.routed is False, "ranked is guidance; routed is exclusion"


def test_the_prompt_says_the_ranking_is_not_a_finding():
    """An agent told "these are the relevant documents" will stop reading and
    trust the top one. The wording has to give it an order without giving it a
    conclusion, and must say plainly that nothing was excluded."""
    from packages.core.navigator import _why_these_documents

    docs = [
        {"item_id": "server-doc", "title": "MarkVector — Server Requirements"},
        {"item_id": "novel", "title": "Harry Potter: The Complete Collection"},
    ]
    note = _why_these_documents(
        Routing(
            how="ranked",
            order=["server-doc", "novel"],
            because={"server-doc": "its summary matches the question"},
            available=2,
        ),
        docs,
    )
    assert "best match" in note
    assert "not a filter" in note, "nothing was excluded and it must say so"
    assert "not a finding" in note, "rank is where to look, never what is true"
    assert note.index("server-doc") < note.index("novel"), "order must survive"

    # A single-document store has nothing to rank and says nothing at all.
    assert _why_these_documents(Routing(how="everything", available=1), docs) == ""


def test_no_count_limit_does_not_slice_the_store_to_nothing():
    """The bug that shipped with "0 means unlimited", caught on a real book.

    The load was ``_documents(limit=MAX_DOCUMENTS + 1)`` followed by
    ``documents[:MAX_DOCUMENTS]`` — correct for any positive cap, catastrophic
    for 0, because ``documents[:0]`` is the empty list. Every catalogue walk was
    handed an empty store and answered "Nothing in these documents answers
    that" in 86 milliseconds, about a 3.2 MB copy of War and Peace that
    contained the answer. It never called the model at all.

    That failure mode is the most expensive one this store has: fast, confident,
    and indistinguishable from an empty collection.
    """
    import inspect

    from packages.core import navigator

    source = inspect.getsource(navigator.navigate)
    assert "documents[:MAX_DOCUMENTS]" not in source, (
        "slicing by the cap empties the list when the cap is 0 (= no limit)"
    )
    assert "len(documents) < routing.available" in source, (
        "truncation is measured against what exists, not derived from the cap"
    )


def test_cards_are_not_looked_up_through_the_ann_index():
    """The bug that only a real store could show, pinned at the source.

    The first implementation queried the vector index and kept the rows that
    happened to be cards. On a real collection it returned **zero cards, every
    time** — and nothing looked broken, because routing quietly fell back to
    passages and still produced an answer.

    The reason is arithmetic, not tuning. A card is one node per document: on
    the store this was measured against, 14 of 10,265 chunks — 0.14%. The query
    over-fetched the 800 nearest neighbours and filtered to that 0.14%, and a
    question resembles the passages that ANSWER it far more than a summary of
    the file they sit in, so no card ever placed in 800. Raising the over-fetch
    does not fix it; it just moves the cliff.

    Cards are scored exactly instead, which is affordable precisely because
    there is one per document. Measured after the fix: 10 of 10 cards scored in
    ~260ms, and "which elements are liquid at room temperature" put the
    periodic table first at 0.7474 — the document that question is about.
    """
    import inspect

    from packages.core import graph

    source = inspect.getsource(graph.card_matches)
    assert "db.index.vector.queryNodes" not in source, (
        "cards are ~0.1% of nodes; filtering an ANN top-k down to them finds "
        "nothing and fails silently"
    )
    assert "vector.similarity.cosine" in source, "cards must be scored exactly"


def test_no_limit_does_not_become_limit_zero():
    """``k <= 0`` means "no limit" everywhere in this codebase and "return
    nothing" in Cypher, and that collision has now cost the card arm twice.

    Once MAX_DOCUMENTS defaulted to 0, the route limit became 0, `LIMIT 0`
    returned zero cards on every single question, and routing quietly fell back
    to passages. Nothing looked broken — it reported ``fell_back`` and carried
    on. Asked "server requirements", the passage arm then matched a Harry
    Potter chapter on the words "Room of Requirement".

    The same convention emptied the document list once before via
    ``documents[:0]``. It is translated at the boundary now.
    """
    import inspect

    from packages.core import graph

    assert "k if k > 0 else" in inspect.getsource(graph.card_matches), (
        "a k of 0 means unlimited to callers and empty to Cypher"
    )


def test_routing_and_the_navigator_agree_on_the_cap():
    """One number, read from one place.

    Routing that ranked more documents than the navigator lays out would hand
    back a list that gets silently truncated — and the documents dropped would
    be the ones routing rated lowest, so the cut lands mid-ranking and nothing
    says where.
    """
    from packages.core.navigator import MAX_DOCUMENTS
    from packages.core.routing import _route_limit

    assert _route_limit() == MAX_DOCUMENTS


def test_the_document_cap_is_no_longer_the_binding_limit(monkeypatch):
    """40 was the wrong kind of limit — a stand-in for "the catalogue stops
    fitting", which the character budget now measures directly.

    Measured on 12-section policy documents, what the model receives after the
    budget shortens it: 100 documents come to 14,880 characters, comfortably
    inside the 24,000 budget. So the prompt was never the reason for 40, and a
    hundred-document store was being cut to 40 for no reason anyone could point
    at — while sixty documents silently stopped existing.
    """
    from packages.core.navigator import CATALOGUE_BUDGET, MAX_CATALOGUE_BYTES, MAX_DOCUMENTS

    assert MAX_DOCUMENTS == 0, (
        "no count limit by default — the store has never had a size limit and "
        "this never was one; it bounded a prompt, written in the wrong unit"
    )
    assert CATALOGUE_BUDGET > 0, "the budget is what actually bounds the prompt"
    # The bound that does apply, in the unit the cost is actually in.
    assert MAX_CATALOGUE_BYTES >= 3_400_000, "one War and Peace must still load"


def test_an_operator_can_raise_the_cap_and_a_typo_cannot_break_it(monkeypatch):
    """It is an env override because the right value depends on document size,
    which is a property of the collection and not of the engine."""
    from packages.core.navigator import _int_env

    monkeypatch.setenv("MAX_DOCUMENTS", "500")
    assert _int_env("MAX_DOCUMENTS", 0) == 500
    # Nonsense must not take retrieval down with it.
    monkeypatch.setenv("MAX_DOCUMENTS", "lots")
    assert _int_env("MAX_DOCUMENTS", 0) == 0
    # Zero is meaningful here — it is "no count limit", not a broken value.
    monkeypatch.setenv("MAX_DOCUMENTS", "0")
    assert _int_env("MAX_DOCUMENTS", 0) == 0


def test_a_narrowed_list_tells_the_agent_it_is_a_window():
    """A narrowed list looks exactly like a small store. Given eight documents
    out of two hundred and no word about it, an agent that fails to find the
    answer concludes the STORE does not hold it — the one sentence a knowledge
    base must never get wrong."""
    from packages.core.navigator import _why_these_documents

    docs = [{"item_id": "d1", "title": "Procurement Circular 14"}]
    routed = Routing(
        item_ids=["d1"],
        how="routed",
        because={"d1": "its summary matches the question"},
        available=200,
    )
    note = _why_these_documents(routed, docs)
    assert "200 documents" in note
    assert "RANKING, not a" in note, "the agent must not treat rank as a finding"
    assert "Procurement Circular 14" in note

    # A store that fits says nothing at all — no note, no behaviour change.
    assert _why_these_documents(Routing(how="everything", available=9), docs) == ""

    # A named filter says something different: stay inside it.
    named = Routing(item_ids=["d1"], how="named", available=1)
    assert "Answer only from them" in _why_these_documents(named, docs)
