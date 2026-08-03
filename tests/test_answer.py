from __future__ import annotations

import pytest

from packages.core import answer as answer_mod
from packages.core.answer import Answer, _gather, _prompt, _resolve_citations, answer
from packages.core.store import put_item
from packages.shared.schema import Hit, Item, Passage, SourceRef
from tests.conftest import SCOPE

# Answering writes prose, which is the one thing this store previously refused
# to do. What makes it acceptable is that every claim has to be traceable — so
# these tests are almost entirely about refusing to invent.
#
# Where a test sets up hybrid retrieval it asks for hybrid by name. Answer
# composition is identical whichever retrieval fed it, and a test that relied on
# the default silently changed meaning the day the default did.


def _passage(chunk_id: str, text: str, heading: str = "", score: float = 0.9) -> Passage:
    return Passage(chunk_id=chunk_id, ordinal=0, heading=heading, text=text, score=score)


def _hit(item_id: str, title: str, passages: list[Passage]) -> Hit:
    return Hit(
        item_id=item_id,
        title=title,
        excerpt=passages[0].text if passages else "",
        source=SourceRef(source="upload", locator=f"{item_id}.md"),
        score=passages[0].score if passages else 0.0,
        passages=passages,
    )


# ------------------------------- citations -------------------------------


def test_a_citation_that_points_at_nothing_is_removed():
    """A model occasionally cites a number it was never given. Left in place
    that is a reference the reader cannot follow — worse than none, because it
    looks checkable."""
    passages = [(_passage("c1", "Real text."), _hit("i1", "Doc", [_passage("c1", "Real text.")]))]
    text, citations = _resolve_citations("A claim [1] and an invented one [7].", passages)
    assert "[7]" not in text
    assert "[1]" in text
    assert [c.marker for c in citations] == [1]


def test_citations_resolve_to_the_passage_they_name():
    first = _passage("c1", "First passage.", "Section A")
    second = _passage("c2", "Second passage.", "Section B")
    passages = [
        (first, _hit("i1", "Doc", [first])),
        (second, _hit("i2", "Other", [second])),
    ]
    _, citations = _resolve_citations("Claim one [1]. Claim two [2].", passages)
    assert [c.chunk_id for c in citations] == ["c1", "c2"]
    assert [c.heading for c in citations] == ["Section A", "Section B"]


def test_a_citation_repeated_is_listed_once():
    p = _passage("c1", "Text.")
    passages = [(p, _hit("i1", "Doc", [p]))]
    _, citations = _resolve_citations("One [1]. Two [1]. Three [1].", passages)
    assert len(citations) == 1


def test_removing_a_bad_marker_does_not_leave_ragged_spacing():
    p = _passage("c1", "Text.")
    passages = [(p, _hit("i1", "Doc", [p]))]
    text, _ = _resolve_citations("A sentence [9] .", passages)
    assert "  " not in text
    assert " ." not in text


# -------------------------------- gathering --------------------------------


def test_passages_are_ranked_across_documents_not_within_them():
    """The answer wants the strongest evidence wherever it lives. Taking the
    best from each document in turn would push one document's second-best above
    another's best."""
    weak = _passage("weak", "Weak.", score=0.4)
    strong = _passage("strong", "Strong.", score=0.95)
    middling = _passage("mid", "Middling.", score=0.7)
    hits = [
        _hit("a", "A", [weak]),
        _hit("b", "B", [strong, middling]),
    ]
    order = [p.chunk_id for p, _ in _gather(hits)]
    assert order == ["strong", "mid", "weak"]


def test_only_a_bounded_number_of_passages_reach_the_model():
    """Beyond a handful the relevant passage gets buried and the answer drifts
    toward whatever came first."""
    many = [_passage(f"c{i}", f"Passage {i}.", score=1 - i / 100) for i in range(40)]
    hits = [_hit("a", "A", many)]
    assert len(_gather(hits)) == answer_mod.MAX_PASSAGES


def test_the_prompt_numbers_passages_and_names_where_they_came_from():
    p = _passage("c1", "The retention period is ninety days.", "4. Retention")
    prompt = _prompt("how long?", [(p, _hit("i1", "Policy", [p]))])
    assert "[1]" in prompt
    assert "Policy > 4. Retention" in prompt
    assert "how long?" in prompt


# ----------------------------- against the store -----------------------------

pytestmark = pytest.mark.needs_db

_HANDBOOK = "\n\n".join(
    [
        "# Handbook",
        "## 1. Expenses",
        "Receipts must be submitted within thirty days of the expense. " * 15,
    ]
)


def _doc(body: str, locator: str = "handbook.md") -> Item:
    return Item(
        id="",
        scope=SCOPE,
        title="Handbook",
        body=body,
        source=SourceRef(source="upload", locator=locator),
    )


async def test_an_empty_collection_is_answered_without_asking_a_model(db, monkeypatch):
    """An empty context is exactly where a language model invents most
    confidently, so it must not be asked at all."""
    called = False

    def explode(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("the model must not be called with no passages")

    monkeypatch.setattr("packages.core.llm.chat", explode)

    result, _ = await answer(db, SCOPE, "anything at all", mode="hybrid")
    assert called is False
    assert result.grounded is False
    assert result.citations == []
    assert "Nothing in this collection" in result.text


async def test_an_unreachable_model_still_returns_the_passages(db, monkeypatch):
    """A degraded answer that looks like a healthy one is the most dangerous
    thing this could produce, so the failure is said out loud and the evidence
    is kept."""
    await put_item(db, _doc(_HANDBOOK))
    await db.commit()

    def unreachable(*args, **kwargs):
        raise ConnectionError("provider down")

    monkeypatch.setattr("packages.core.llm.chat", unreachable)

    result, _ = await answer(db, SCOPE, "receipts submitted within thirty days", mode="hybrid")
    assert result.grounded is False
    assert result.degraded and "answer unavailable" in result.degraded
    assert result.hits, "the passages that were found must survive the failure"


async def test_an_answer_citing_nothing_is_not_treated_as_grounded(db, monkeypatch):
    """Confident prose with no evidence behind it is the case worth catching."""
    await put_item(db, _doc(_HANDBOOK))
    await db.commit()

    monkeypatch.setattr(
        "packages.core.llm.chat",
        lambda *a, **k: "Expenses must be filed promptly and approved by a manager.",
    )

    result, _ = await answer(db, SCOPE, "receipts submitted within thirty days", mode="hybrid")
    assert result.citations == []
    assert result.grounded is False


async def test_a_model_declining_is_reported_as_ungrounded(db, monkeypatch):
    await put_item(db, _doc(_HANDBOOK))
    await db.commit()

    monkeypatch.setattr(
        "packages.core.llm.chat",
        lambda *a, **k: "NOT_IN_CONTEXT the passages say nothing about holiday pay.",
    )

    result, _ = await answer(db, SCOPE, "receipts submitted within thirty days", mode="hybrid")
    assert result.grounded is False
    assert "NOT_IN_CONTEXT" not in result.text
    assert "holiday pay" in result.text


async def test_a_decline_keeps_any_near_miss_citations(db, monkeypatch):
    """The explanation of what IS there often points at the passages it looked
    at. Those stay reachable; only `grounded` says the question went
    unanswered."""
    await put_item(db, _doc(_HANDBOOK))
    await db.commit()

    monkeypatch.setattr(
        "packages.core.llm.chat",
        lambda *a, **k: "NOT_IN_CONTEXT They mention receipts [1] but give no figure.",
    )

    result, _ = await answer(db, SCOPE, "receipts submitted within thirty days", mode="hybrid")
    assert result.grounded is False
    assert [c.marker for c in result.citations] == [1]


async def test_a_grounded_answer_carries_its_evidence(db, monkeypatch):
    await put_item(db, _doc(_HANDBOOK))
    await db.commit()

    monkeypatch.setattr(
        "packages.core.llm.chat",
        lambda *a, **k: "Receipts go in within thirty days [1].",
    )

    result, _ = await answer(db, SCOPE, "receipts submitted within thirty days", mode="hybrid")
    assert result.grounded is True
    assert len(result.citations) == 1
    assert "thirty days" in result.citations[0].text
    assert result.trace_id, "the retrieval behind the answer must be inspectable"


async def test_the_answer_shape_survives_serialisation(db, monkeypatch):
    await put_item(db, _doc(_HANDBOOK))
    await db.commit()
    monkeypatch.setattr("packages.core.llm.chat", lambda *a, **k: "Within thirty days [1].")

    result, _ = await answer(db, SCOPE, "receipts", mode="hybrid")
    payload = result.as_dict()
    assert payload["grounded"] is True
    assert payload["citations"][0]["marker"] == 1
    assert payload["trace_id"] and payload["question"] == "receipts"
    assert isinstance(payload["answer"], str)


def test_an_answer_defaults_to_grounded_with_no_degradation():
    """The default must not quietly claim more than it has: a bare Answer is
    only ever constructed with text put into it deliberately."""
    blank = Answer(question="q", text="")
    assert blank.citations == [] and blank.degraded is None


async def test_vectorless_is_the_default_retrieval(db, monkeypatch):
    """The default is a decision, so it is pinned. Changing it should require
    changing this line, not discovering the change in production."""
    seen: list[str] = []

    async def spy(session, scope, question):
        seen.append("vectorless")
        from packages.core.search import Trace, new_trace_id
        from packages.core.vectorless import Outcome

        return Outcome(), Trace(trace_id=new_trace_id(), query=question, config={}, filters={})

    monkeypatch.setattr("packages.core.vectorless.retrieve", spy)
    result, _ = await answer(db, SCOPE, "anything")
    assert seen == ["vectorless"]
    assert result.mode == "vectorless"


async def test_an_empty_vectorless_result_says_so_in_its_own_terms(db, monkeypatch):
    """"Nothing in this collection" and "no section looks like it answers that"
    are different claims. The one shown should match the retrieval that ran."""

    async def nothing(session, scope, question):
        from packages.core.search import Trace, new_trace_id
        from packages.core.vectorless import Outcome

        return Outcome(), Trace(trace_id=new_trace_id(), query=question, config={}, filters={})

    monkeypatch.setattr("packages.core.vectorless.retrieve", nothing)
    result, _ = await answer(db, SCOPE, "anything", mode="vectorless")
    assert result.grounded is False
    assert "section" in result.text
