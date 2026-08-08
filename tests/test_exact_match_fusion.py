"""A document that matched a word exactly always shows the reader where.

This guard shipped alongside query expansion, but it is not part of it and
must not be allowed to become part of it. Expansion is a recall feature with
a cost, and it has an off switch. The fusion fix is a correctness fix: without
it the store can hold the answer 182 times, find all 182 in 73ms, and still
show the reader five passages that do not contain it.

The scoring is ``max(semantic, min(keyword * 2, 0.6))``. A keyword-only
passage is capped at 0.6, which is fine while semantic scores are spread out
and fatal when they are not — on a store holding one novel every cosine lands
near 0.85, because all the prose is the same domain. Every semantic passage
then outranks every exact match, structurally, for any query.

So these tests pin the behaviour with expansion OFF, which is the
configuration where the guarantee has to hold on its own.
"""

from __future__ import annotations

from packages.core.search import _keep_the_exact_matches
from packages.shared.schema import Passage


def _p(chunk_id: str, semantic: float = 0.0, keyword: float = 0.0) -> Passage:
    return Passage(
        chunk_id=chunk_id,
        ordinal=0,
        heading="",
        text="",
        score=max(semantic, min(keyword * 2, 0.6)),
        semantic=semantic,
        keyword=keyword,
    )


def _one_novel() -> list[Passage]:
    """The measured pathology: six passages about owls in general, all scoring
    the flat ~0.85 that same-domain prose produces, and one passage that
    actually says the name — which the cap holds at 0.6."""
    group = [_p(f"sem{n}", semantic=0.87 - n * 0.005) for n in range(6)]
    group.append(_p("names-hedwig", keyword=0.94))
    return sorted(group, key=lambda p: p.score, reverse=True)


def test_the_exact_match_survives_a_flat_semantic_field():
    """The whole point. Without the guard "names-hedwig" ranks seventh of
    seven and is cut by keep=5, no matter how strongly it matched."""
    group = _one_novel()
    assert [p.chunk_id for p in group[:5]] == ["sem0", "sem1", "sem2", "sem3", "sem4"]

    kept = {p.chunk_id for p in _keep_the_exact_matches(group)}
    assert "names-hedwig" in kept


def test_it_holds_with_expansion_off():
    """The guard takes must_keep from the expansion block. With
    QUERY_EXPANSION=false that set is empty, and the guarantee must come from
    the keyword slots instead — this is the configuration the fix exists for,
    not a degraded one."""
    kept = _keep_the_exact_matches(_one_novel(), must_keep=None)
    assert "names-hedwig" in {p.chunk_id for p in kept}
    assert _keep_the_exact_matches(_one_novel(), must_keep=set()) == kept


def test_a_weak_match_on_a_common_word_does_not_satisfy_it():
    """The first version of this guard asked "does the top-5 hold ANY keyword
    match" and returned early when it did. On this store that was satisfied by
    a passage matching "Harry Potter" — a phrase on nearly every page — while
    the passages naming the owl were still cut. A weak match being present is
    not the same as the best match being present.
    """
    group = _one_novel()
    group.insert(2, _p("says-harry-potter", semantic=0.86, keyword=0.35))
    kept = {p.chunk_id for p in _keep_the_exact_matches(group)}
    assert "says-harry-potter" in kept
    assert "names-hedwig" in kept, "the stronger match must not be crowded out"


def test_the_result_is_never_longer_than_the_budget():
    """Reserving slots trades the weakest semantic passages away. It must not
    quietly widen the context window instead."""
    for group in (_one_novel(), _one_novel()[:2], []):
        assert len(_keep_the_exact_matches(group, keep=5)) <= 5


def test_a_document_with_no_lexical_match_is_left_exactly_alone():
    """Most documents match nothing exactly. Their ranking is not this
    function's business and must come through untouched."""
    group = [_p(f"sem{n}", semantic=0.9 - n * 0.01) for n in range(8)]
    assert _keep_the_exact_matches(group) == group[:5]


def test_each_named_winner_gets_in_rather_than_competing_on_rank():
    """must_keep carries one best passage PER expansion term. Pooling them and
    taking the top two let a single strong term ("Harry Potter", scoring 1.000
    on both its hits) fill every slot and starve the term that would have
    found the answer.
    """
    group = [
        _p("harry-a", keyword=1.0),
        _p("harry-b", keyword=1.0),
        _p("hedwig", keyword=0.55),
        *[_p(f"sem{n}", semantic=0.88 - n * 0.01) for n in range(5)],
    ]
    group.sort(key=lambda p: p.score, reverse=True)

    kept = {
        p.chunk_id
        for p in _keep_the_exact_matches(group, must_keep={"harry-a", "hedwig"})
    }
    assert kept >= {"harry-a", "hedwig"}
    assert "harry-b" not in kept or len(kept) == 5


def test_a_named_winner_that_is_not_in_the_group_is_ignored():
    """must_keep is built across every document. Applied to one document's
    passages, most of its ids belong to other documents."""
    group = _one_novel()
    kept = _keep_the_exact_matches(group, must_keep={"a-chunk-of-another-doc"})
    assert len(kept) <= 5
    assert kept, "an unmatched hint must not empty the document"
