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


async def test_agentic_is_the_default_retrieval(db, monkeypatch):
    """The default is a decision, so it is pinned. Changing it should require
    changing this line, not discovering the change in production.

    It moved from vectorless to agentic: the same catalogue reasoning, plus
    hybrid_search and a graph hop, so it reaches a collection larger than the
    catalogue window instead of answering from the part that fits. Both walk
    the navigator — the difference is which tools the agent is handed.
    """
    seen: list[bool] = []

    async def spy(session, scope, question, **watching):
        # ``hybrid`` is what makes the walk agentic rather than vectorless, so
        # that flag is the thing worth asserting on — recording a hardcoded
        # string here would have passed no matter what the default became.
        seen.append(bool(watching.get("hybrid")))
        from packages.core.navigator import Outcome
        from packages.core.search import Trace, new_trace_id

        return Outcome(), Trace(trace_id=new_trace_id(), query=question, config={}, filters={})

    monkeypatch.setattr("packages.core.navigator.navigate", spy)
    result, _ = await answer(db, SCOPE, "anything")
    assert seen == [True], "the default walk must hand the agent its search tools"
    assert result.mode == "agentic"


async def test_the_route_the_agent_took_travels_with_the_answer(db, monkeypatch):
    """The steps were always recorded and never shown, so the only account of
    how an answer was reached was a trace id — a receipt number. They ride on
    the answer now, including when nothing was found: a walk that opened two
    sections and still came back empty is the case where the route matters
    most, and an "answer only" field is exactly what would drop it."""

    async def walked(session, scope, question, **watching):
        from packages.core.navigator import Outcome, Step
        from packages.core.search import Trace, new_trace_id

        outcome = Outcome(
            steps=[
                Step(1, "read", "Expenses → [1]"),
                Step(1, "missed", "n099"),
                Step(2, "answered", "not found"),
            ]
        )
        return outcome, Trace(trace_id=new_trace_id(), query=question, config={}, filters={})

    monkeypatch.setattr("packages.core.navigator.navigate", walked)
    result, _ = await answer(db, SCOPE, "anything", mode="vectorless")

    assert result.grounded is False, "nothing was read, so nothing is grounded"
    assert [s["action"] for s in result.steps] == ["read", "missed", "answered"]
    assert result.as_dict()["steps"][1]["detail"] == "n099"


async def test_hybrid_reports_no_route_because_it_takes_none(db, monkeypatch):
    """Hybrid ranks and hands over. Inventing steps for it would put a story on
    screen that describes work nothing did."""
    await put_item(db, _doc(_HANDBOOK))
    await db.commit()
    monkeypatch.setattr("packages.core.llm.chat", lambda *a, **k: "Within thirty days [1].")

    result, _ = await answer(db, SCOPE, "receipts", mode="hybrid")
    assert result.steps == []


async def test_an_empty_vectorless_result_says_so_in_its_own_terms(db, monkeypatch):
    """"Nothing in this collection" and "no section looks like it answers that"
    are different claims. The one shown should match the retrieval that ran."""

    async def nothing(session, scope, question, **watching):
        from packages.core.navigator import Outcome
        from packages.core.search import Trace, new_trace_id

        return Outcome(), Trace(trace_id=new_trace_id(), query=question, config={}, filters={})

    monkeypatch.setattr("packages.core.navigator.navigate", nothing)
    result, _ = await answer(db, SCOPE, "anything", mode="vectorless")
    assert result.grounded is False
    assert "answers that" in result.text or "section" in result.text


async def test_a_verbatim_answer_without_a_marker_is_still_grounded(db, monkeypatch):
    """The model answered "how long do I have to submit receipts" by quoting the
    passage word for word and simply did not write [1]. Trusting the marker
    labelled a correct, sourced answer "not supported by the collection" — and a
    warning that fires on good answers is one people learn to ignore."""
    await put_item(db, _doc(_HANDBOOK))
    await db.commit()

    monkeypatch.setattr(
        "packages.core.llm.chat",
        lambda *a, **k: "Receipts must be submitted within thirty days of the expense.",
    )

    result, _ = await answer(db, SCOPE, "receipts submitted within thirty days", mode="hybrid")
    assert result.grounded is True
    assert len(result.citations) == 1
    assert "thirty days" in result.citations[0].text


async def test_prose_matching_no_passage_stays_ungrounded(db, monkeypatch):
    """The case attribution must never rescue: confident wording that appears
    nowhere in the evidence."""
    await put_item(db, _doc(_HANDBOOK))
    await db.commit()

    monkeypatch.setattr(
        "packages.core.llm.chat",
        lambda *a, **k: (
            "Employees are entitled to sixteen weeks of fully paid parental leave "
            "following a qualifying event, subject to annual review."
        ),
    )

    result, _ = await answer(db, SCOPE, "receipts submitted within thirty days", mode="hybrid")
    assert result.citations == []
    assert result.grounded is False


async def test_attribution_needs_a_real_run_of_words_not_a_stray_phrase(db, monkeypatch):
    """Ordinary phrasing shared by chance must not count as a source."""
    await put_item(db, _doc(_HANDBOOK))
    await db.commit()

    monkeypatch.setattr("packages.core.llm.chat", lambda *a, **k: "Receipts must be.")

    result, _ = await answer(db, SCOPE, "receipts submitted within thirty days", mode="hybrid")
    assert result.grounded is False


async def test_a_one_word_answer_is_still_credited_to_its_passage(db, monkeypatch):
    """Asked for a headcount the model replied "96" — correct, straight off the
    passage displayed beneath it — and the console stamped the answer "not
    supported by the collection", because one word cannot contain a seven-word
    run. Short factual answers are the commonest kind there is."""
    await put_item(db, _doc("# Segments\n\nNordics headcount is 96 across two offices."))
    await db.commit()
    monkeypatch.setattr("packages.core.llm.chat", lambda *a, **k: "96")

    result, _ = await answer(db, SCOPE, "nordics headcount", mode="hybrid")
    assert result.grounded is True
    assert result.citations and "96" in result.citations[0].text


async def test_an_ambiguous_short_answer_stays_uncited(db, monkeypatch):
    """If two passages both contain the figure there is no way to tell which was
    used. Guessing would attach a checkable-looking reference to the wrong
    place, which is worse than leaving it uncited."""
    from packages.shared.schema import Item, SourceRef

    for n, locator in ((1, "a.md"), (2, "b.md")):
        await put_item(
            db,
            Item(
                id="",
                scope=SCOPE,
                title=f"Report {n}",
                body=f"# Report {n}\n\nThe headcount recorded here is 96 people.",
                source=SourceRef(source="upload", locator=locator),
            ),
        )
    await db.commit()
    monkeypatch.setattr("packages.core.llm.chat", lambda *a, **k: "96")

    result, _ = await answer(db, SCOPE, "headcount", mode="hybrid")
    assert result.citations == []
    assert result.grounded is False


def test_a_short_answer_matches_whole_words_not_substrings():
    """"96" must not be credited to a passage that only says "960"."""
    from packages.core.answer import _attribute_short
    from packages.shared.schema import Hit, Passage, SourceRef

    hit = Hit(
        item_id="i",
        title="t",
        excerpt="",
        source=SourceRef(source="upload", locator="l"),
        score=1.0,
        passages=[],
    )
    passage = Passage(chunk_id="c", ordinal=0, heading="h", text="The figure is 960.", score=1.0)
    assert _attribute_short(["96"], "96", [(passage, hit)]) is None


def test_a_short_answer_of_ordinary_words_is_not_credited():
    """"Yes" or "the second one" shares its words with half the collection.
    Matching on them would credit a passage that merely uses the same
    vocabulary — the exact failure the strict rule exists to prevent."""
    from packages.core.answer import _attribute_short
    from packages.shared.schema import Hit, Passage, SourceRef

    hit = Hit(
        item_id="i",
        title="t",
        excerpt="",
        source=SourceRef(source="upload", locator="l"),
        score=1.0,
        passages=[],
    )
    passage = Passage(
        chunk_id="c", ordinal=0, heading="h", text="Receipts must be filed.", score=1.0
    )
    assert (
        _attribute_short(
            ["receipts", "must", "be"], "Receipts must be", [(passage, hit)]
        )
        is None
    )


def test_a_passage_is_located_on_the_page_it_came_from():
    """Passages cut by the chunker carry no page, so the same evidence showed
    its page under vectorless and showed nothing under hybrid — two stories
    about one passage, decided by a retrieval choice the reader never made."""
    from packages.core import tree

    body = (
        "<!-- page 1 -->\nOpening remarks about the year.\n\n---\n\n"
        "<!-- page 2 -->\nRevenue grew in both segments,\nwhile costs were held flat.\n\n"
        "---\n\n<!-- page 3 -->\nGuidance is unchanged.\n"
    )
    # newlines flattened, exactly as a passage arrives from the chunker
    assert tree.page_containing(body, "Revenue grew in both segments, while costs") == 2
    assert tree.page_containing(body, "Guidance is unchanged.") == 3
    assert tree.page_containing(body, "Opening remarks") == 1


def test_text_that_is_not_in_the_document_has_no_page():
    """A citation with no page shows text only, which is honest. Guessing one
    would put the wrong picture behind a checkable-looking reference."""
    from packages.core import tree

    body = "<!-- page 1 -->\nSomething entirely different.\n"
    assert tree.page_containing(body, "words that never appeared") is None
    assert tree.page_containing("no page markers at all", "no page markers") is None
    assert tree.page_containing(body, "") is None


async def test_hybrid_citations_carry_their_page_too(db, monkeypatch):
    """The fix, end to end: retrieval mode must not change what a citation can
    say about where it came from.

    Page one is padded so the receipts passage is cut as its own chunk. A
    passage that straddles a page boundary reports where it BEGINS, which is
    the only answer that is always true of it."""
    await put_item(
        db,
        _doc(
            "<!-- page 1 -->\n"
            + "Introductory matter about the scheme and its history. " * 60
            + "\n\n---\n\n<!-- page 2 -->\n"
            + "Receipts must be submitted within thirty days of the expense. " * 12,
            locator="expenses.pdf",
        ),
    )
    await db.commit()
    monkeypatch.setattr("packages.core.llm.chat", lambda *a, **k: "Within thirty days [1].")

    result, _ = await answer(db, SCOPE, "receipts submitted within thirty days", mode="hybrid")
    assert result.citations, "the answer must be cited at all"
    # A page at all is the fix. WHICH page is exercised precisely by
    # test_a_passage_is_located_on_the_page_it_came_from — here the chunker
    # decides where the passage starts, and pinning that would be testing the
    # chunker's size budget rather than this.
    assert result.citations[0].page is not None, "a hybrid citation must know its page"


def _passages(*texts: str) -> list[tuple[Passage, Hit]]:
    """The (passage, hit) pairs _distinctive scores against, one per text."""
    out: list[tuple[Passage, Hit]] = []
    for n, text in enumerate(texts):
        passage = _passage(f"c{n}", text)
        out.append((passage, _hit(f"i{n}", f"Doc {n}", [passage])))
    return out

def test_a_name_is_distinctive_enough_to_cite():
    """A correct one-word answer must not be stamped "not supported".

    Measured in the browser: asked "Harry owl name" the store answered
    "Hedwig", correctly, off a passage sitting directly beneath it -- and the
    UI printed "not supported by the collection" in red, because the
    distinctiveness test was "contains a digit" and a name contains none.
    Names, places and product identifiers are the other large class of short
    factual answer; a warning that fires on all of them fires on nothing.
    """
    from packages.core.answer import _distinctive, _words

    passages = _passages(
        "Ron asked the cat. Definitely an owl?",
        "A gray owl was soaring down toward Hermione.",
        "Harry carried Errol to Hedwig's cage.",
    )
    assert _distinctive(_words("Hedwig"), "Hedwig", passages) is True


def test_a_capital_letter_alone_is_not_enough():
    """"Yes" and "The second one" are capitalised too. A candidate name must
    also be RARE -- present in exactly one of the retrieved passages -- which
    is the caller's own unambiguity rule applied per word, and calibrates
    against the collection instead of a stop-word list nobody maintains."""
    from packages.core.answer import _distinctive, _words

    passages = _passages(
        "Yes, said Ron, yes of course.",
        "Yes -- Harry nodded. Yes.",
        "She said yes before he finished.",
    )
    assert _distinctive(_words("Yes"), "Yes", passages) is False


def test_an_ambiguous_name_stays_uncited():
    """If two passages both contain the name there is no way to tell which was
    used, and guessing attaches a checkable-looking reference to the wrong
    place -- worse than leaving it uncited.

    Asserted against _attribute_short rather than _distinctive, because that
    is where the guarantee actually lives: _distinctive only decides whether
    the answer carries something traceable at all, and the caller then demands
    that exactly one passage hold every word of it. Testing the gate instead
    of the contract would pass while the contract broke.
    """
    from packages.core.answer import _attribute_short, _words

    passages = _passages(
        "Harry carried Errol to Hedwig's cage.",
        "Ron watched as Hedwig nipped his ear.",
    )
    assert _attribute_short(_words("Hedwig"), "Hedwig", passages) is None

    # One passage, and it is credited.
    single = _passages(
        "Harry carried Errol to Hedwig's cage.",
        "A gray owl soared down toward Hermione.",
    )
    assert _attribute_short(_words("Hedwig"), "Hedwig", single) is not None


def test_a_figure_is_still_distinctive():
    """The original rule, unchanged: the headcount case this path was written
    for keeps working."""
    from packages.core.answer import _distinctive, _words

    passages = _passages("The team grew to 96 people.", "No numbers here at all.")
    assert _distinctive(_words("96"), "96", passages) is True


def test_an_answer_that_admits_it_came_from_outside_is_not_grounded():
    """The worst failure this system can produce is a RIGHT answer that did not
    come from the documents, presented as though it did.

    Measured in the console, asked the name of Harry Potter's owl: "The
    provided sections do not contain the name directly, but the full text of
    the document confirms this fact. Since the question is about a well-known
    detail from the Harry Potter series..." -- cited to a table of contents,
    which names no owl, and reported as grounded. The name was correct, which
    is exactly what makes it dangerous: a reader cannot tell it apart from an
    answer the store actually found.
    """
    from packages.core.answer import _answered_from_outside

    assert _answered_from_outside(
        "The provided sections do not contain the name directly, but the full "
        "text confirms this. Since this is a well-known detail..."
    )
    assert _answered_from_outside("The name is not stated in the provided passages.")
    assert _answered_from_outside("From general knowledge, the answer is Hedwig.")


def test_a_merely_cautious_answer_stays_grounded():
    """An answer that qualifies its FINDING is a good answer. Only one that
    disclaims its EVIDENCE is the failure -- confusing the two would stamp the
    most careful answers in the store as unsupported, and a warning that fires
    on good work is a warning nobody reads."""
    from packages.core.answer import _answered_from_outside

    assert not _answered_from_outside(
        "The documents do not say when this takes effect, but they do state "
        "that receipts must be filed within thirty days [1]."
    )
    assert not _answered_from_outside("Harry's owl is Hedwig [1].")
    assert not _answered_from_outside("The sources disagree: [1] says 30 days, [2] says 60.")
    assert not _answered_from_outside("")
