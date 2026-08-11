from __future__ import annotations

import json

import pytest

from packages.core import navigator
from packages.core.navigator import navigate
from packages.core.store import put_item
from packages.shared.schema import Item, SourceRef
from tests.conftest import SCOPE

pytestmark = pytest.mark.needs_db

# The navigator replaced a single "which sections?" call that failed in a way
# indistinguishable from an empty store. These pin the properties that make the
# loop trustworthy: it reads before it answers, it cannot cite what it never
# opened, and it stops.

_HANDBOOK = "\n\n".join(
    [
        "# Handbook",
        "## 1. Leave",
        "Staff accrue leave monthly and may carry over five days. " * 6,
        "## 2. Expenses",
        "Receipts must be submitted within thirty days of the expense. " * 6,
    ]
)


def _doc(body: str = _HANDBOOK, locator: str = "handbook.md") -> Item:
    return Item(
        id="",
        scope=SCOPE,
        title="Handbook",
        body=body,
        source=SourceRef(source="upload", locator=locator),
    )


def _scripted(*turns):
    """A model that replies with the given turns in order, then answers."""
    calls = {"n": 0}

    def fake(messages, tools, **kwargs):
        index = calls["n"]
        calls["n"] += 1
        if index < len(turns):
            return turns[index]
        return {"content": "Done.", "tool_calls": []}

    fake.calls = calls
    return fake


def _astreamed(*turns):
    """A streaming model: each turn yields its prose as one text frame, then a
    `done` frame carrying the same {content, tool_calls} the blocking call
    returns — the shape astream_chat_with_tools produces."""
    calls = {"n": 0}

    async def fake(messages, tools, **kwargs):
        index = calls["n"]
        calls["n"] += 1
        turn = turns[index] if index < len(turns) else {"content": "Done.", "tool_calls": []}
        if turn.get("content"):
            yield {"type": "text", "delta": turn["content"]}
        yield {
            "type": "done",
            "content": turn.get("content", ""),
            "tool_calls": turn.get("tool_calls", []),
        }

    fake.calls = calls
    return fake


def _search(query: str, call_id: str = "s1"):
    return {
        "content": "",
        "tool_calls": [
            {
                "id": call_id,
                "name": "hybrid_search",
                "arguments": json.dumps({"query": query}),
            }
        ],
    }


def _read(doc: str, section: str, call_id: str = "c1"):
    return {
        "content": "",
        "tool_calls": [
            {
                "id": call_id,
                "name": "read_section",
                "arguments": json.dumps({"doc": doc, "section": section}),
            }
        ],
    }


def _submit(answer: str, found: bool = True, call_id: str = "c2"):
    return {
        "content": "",
        "tool_calls": [
            {
                "id": call_id,
                "name": "submit_answer",
                "arguments": json.dumps({"answer": answer, "found": found}),
            }
        ],
    }


async def test_an_empty_store_never_reaches_the_model(db, monkeypatch):
    """No documents means nothing to navigate. Asking anyway is how a model
    ends up describing a store it was never shown."""
    called = False

    def explode(*a, **k):
        nonlocal called
        called = True
        raise AssertionError("must not call the model with no documents")

    monkeypatch.setattr("packages.core.llm.chat_with_tools", explode)
    outcome, _ = await navigate(db, SCOPE, "anything")
    assert called is False
    assert outcome.hits == [] and outcome.found is False


async def test_a_section_read_becomes_a_numbered_citation(db, monkeypatch):
    await put_item(db, _doc())
    await db.commit()
    item_id = (await navigator._documents(db, SCOPE))[0]["item_id"]

    monkeypatch.setattr(
        "packages.core.llm.chat_with_tools",
        _scripted(_read(item_id, "n002"), _submit("Leave accrues monthly [1].")),
    )
    outcome, _ = await navigate(db, SCOPE, "how does leave accrue?")

    assert outcome.found is True
    assert outcome.hits and outcome.hits[0].passages
    assert outcome.hits[0].passages[0].chunk_id.endswith(":n002")
    assert any(step.action == "read" for step in outcome.steps)


async def test_a_section_that_does_not_exist_is_reported_not_silently_empty(db, monkeypatch):
    """Silence reads to the model as "nothing is there", which is how the old
    version turned one bad guess into an empty store."""
    await put_item(db, _doc())
    await db.commit()
    item_id = (await navigator._documents(db, SCOPE))[0]["item_id"]

    seen: list[str] = []

    def fake(messages, tools, **kwargs):
        for m in messages:
            if m.get("role") == "tool":
                seen.append(str(m.get("content")))
        if len(seen) == 0:
            return _read(item_id, "n999")
        return _submit("Nothing found.", found=False)

    monkeypatch.setattr("packages.core.llm.chat_with_tools", fake)
    outcome, _ = await navigate(db, SCOPE, "anything")

    assert any("No section" in s for s in seen), "the model must be told the id was wrong"
    assert any(step.action == "missed" for step in outcome.steps)


async def test_reading_the_same_section_twice_is_refused(db, monkeypatch):
    """Re-reading is a loop, not research."""
    await put_item(db, _doc())
    await db.commit()
    item_id = (await navigator._documents(db, SCOPE))[0]["item_id"]

    monkeypatch.setattr(
        "packages.core.llm.chat_with_tools",
        _scripted(
            _read(item_id, "n002", "a"),
            _read(item_id, "n002", "b"),
            _submit("Answer [1]."),
        ),
    )
    outcome, _ = await navigate(db, SCOPE, "leave")
    reads = [s for s in outcome.steps if s.action == "read"]
    assert len(reads) == 1, "the second read of the same section must not count"


async def test_the_loop_always_stops(db, monkeypatch):
    """A model that only ever reads must not run forever."""
    await put_item(db, _doc())
    await db.commit()
    item_id = (await navigator._documents(db, SCOPE))[0]["item_id"]

    counter = {"n": 0}

    def forever(messages, tools, **kwargs):
        counter["n"] += 1
        return _read(item_id, "n002", f"call-{counter['n']}")

    monkeypatch.setattr("packages.core.llm.chat_with_tools", forever)
    outcome, _ = await navigate(db, SCOPE, "leave")
    assert outcome.rounds <= navigator.MAX_ROUNDS
    assert counter["n"] <= navigator.MAX_ROUNDS


async def test_an_answer_with_nothing_read_is_not_found(db, monkeypatch):
    """The model claiming it found something, having opened nothing, is the
    exact shape of an invented answer."""
    await put_item(db, _doc())
    await db.commit()

    monkeypatch.setattr(
        "packages.core.llm.chat_with_tools",
        _scripted(_submit("Leave is thirty days.", found=True)),
    )
    outcome, _ = await navigate(db, SCOPE, "leave")
    assert outcome.found is False, "found requires having actually read something"
    assert outcome.hits == []


async def test_prose_without_submit_answer_is_still_taken_as_the_answer(db, monkeypatch):
    """This model routinely answers in prose rather than calling the tool.
    Discarding that would turn a formatting slip into an empty result, which is
    the failure this module exists to remove."""
    await put_item(db, _doc())
    await db.commit()
    item_id = (await navigator._documents(db, SCOPE))[0]["item_id"]

    monkeypatch.setattr(
        "packages.core.llm.chat_with_tools",
        _scripted(
            _read(item_id, "n002"),
            {"content": "Staff accrue leave monthly [1].", "tool_calls": []},
        ),
    )
    outcome, _ = await navigate(db, SCOPE, "leave")
    assert "accrue leave monthly" in outcome.answer
    assert outcome.found is True


async def test_an_unreachable_model_degrades_rather_than_raising(db, monkeypatch):
    await put_item(db, _doc())
    await db.commit()

    def down(*a, **k):
        raise ConnectionError("provider down")

    monkeypatch.setattr("packages.core.llm.chat_with_tools", down)
    outcome, trace = await navigate(db, SCOPE, "leave")
    assert outcome.degraded and "navigation unavailable" in outcome.degraded
    assert trace.degraded == outcome.degraded


async def test_every_step_is_recorded_in_the_trace(db, monkeypatch):
    """An answer whose retrieval cannot be inspected is not one anybody can
    argue with — and a loop is only trustworthy if its path is visible."""
    await put_item(db, _doc())
    await db.commit()
    item_id = (await navigator._documents(db, SCOPE))[0]["item_id"]

    monkeypatch.setattr(
        "packages.core.llm.chat_with_tools",
        _scripted(_read(item_id, "n002"), _submit("Answer [1].")),
    )
    _, trace = await navigate(db, SCOPE, "leave")
    actions = [entry["action"] for entry in trace.semantic]
    assert "read" in actions and "answered" in actions


async def test_a_synthesis_is_credited_to_the_sections_actually_read(db, monkeypatch):
    """A synthesis across a section shares no verbatim run with it and often
    carries no marker, so neither the marker check nor the verbatim check
    fires. In the navigator that is not an inference: the loop RECORDS what it
    opened, and an answer written after reading exactly those sections came
    from exactly those sections. Without this a correct, sourced answer was
    stamped "not supported by the collection"."""
    from packages.core.answer import answer as compose

    await put_item(db, _doc())
    await db.commit()
    item_id = (await navigator._documents(db, SCOPE))[0]["item_id"]

    monkeypatch.setattr(
        "packages.core.llm.chat_with_tools",
        _scripted(
            _read(item_id, "n002"),
            _submit("Staff build up time off across the year and may retain some of it.", True),
        ),
    )
    result, _ = await compose(db, SCOPE, "how does leave work?", mode="vectorless")
    assert result.grounded is True
    assert result.citations, "the sections read are the evidence"
    assert result.citations[0].chunk_id.endswith(":n002")


async def test_a_synthesis_is_not_credited_when_the_agent_says_it_found_nothing(db, monkeypatch):
    """The model's own judgement still governs. Reading a section and then
    reporting that it does not answer must not be dressed up as an answer."""
    from packages.core.answer import answer as compose

    await put_item(db, _doc())
    await db.commit()
    item_id = (await navigator._documents(db, SCOPE))[0]["item_id"]

    monkeypatch.setattr(
        "packages.core.llm.chat_with_tools",
        _scripted(
            _read(item_id, "n002"),
            _submit("Nothing here covers parental leave.", False),
        ),
    )
    result, _ = await compose(db, SCOPE, "parental leave?", mode="vectorless")
    assert result.grounded is False
    assert result.citations == []


async def test_an_answer_written_without_reading_is_sent_back(db, monkeypatch):
    """Asked for two figures out of a table, the model answered from the
    CATALOGUE — which holds each section's title and opening line only — saw no
    numbers there, and reported the store did not contain them. It did contain
    them.

    So a first answer written before anything was opened is refused once. Only
    once, and only while nothing has been read: a model that has read and still
    says no is answering, not skipping."""
    await put_item(db, _doc())
    await db.commit()
    item_id = (await navigator._documents(db, SCOPE))[0]["item_id"]

    monkeypatch.setattr(
        "packages.core.llm.chat_with_tools",
        _scripted(
            {"content": "The documents do not cover that.", "tool_calls": []},
            _read(item_id, "n003"),
            _submit("Receipts go in within thirty days [1]."),
        ),
    )
    outcome, _ = await navigate(db, SCOPE, "how long do I have to submit receipts?")

    assert [s.action for s in outcome.steps] == ["opened", "sent back", "read", "answered"]
    assert outcome.found is True


async def test_a_model_that_has_read_may_still_answer_in_prose(db, monkeypatch):
    """The push-back must not become a loop. Once something has been read, an
    answer without a tool call is accepted as the answer — refusing content the
    model already produced would turn a formatting slip into an empty result."""
    await put_item(db, _doc())
    await db.commit()
    item_id = (await navigator._documents(db, SCOPE))[0]["item_id"]

    monkeypatch.setattr(
        "packages.core.llm.chat_with_tools",
        _scripted(_read(item_id, "n003"), {"content": "Thirty days [1].", "tool_calls": []}),
    )
    outcome, _ = await navigate(db, SCOPE, "receipts?")

    assert outcome.answer == "Thirty days [1]."
    assert outcome.found is True


def test_a_tool_call_typed_out_as_prose_is_cut_from_the_answer():
    """Some models finish a good answer and then append the call they were
    supposed to make. The answer above it is real; the typed call is machinery
    landing on the reader's screen under a heading claiming it is what the store
    found."""
    from packages.core.navigator import _strip_pseudo_call

    text = (
        "The Nordics revenue in 2025 is 5.8 million GBP [1].\n\n"
        'submit_answer({"answer": "5.8 million GBP", "found": true})'
    )
    assert _strip_pseudo_call(text) == "The Nordics revenue in 2025 is 5.8 million GBP [1]."
    # An answer that merely mentions the tool by name is left alone.
    plain = "Nothing here needed submit_answer at all."
    assert _strip_pseudo_call(plain) == plain


async def test_the_push_back_happens_once_not_every_round(db, monkeypatch):
    """Without a guard the nudge ate the whole search: asked about a figure the
    model said "not found", was sent back, said it again, and the trail read
    `sent back` five times before the loop gave up. A nudge repeated is not a
    nudge, it is a deadlock."""
    await put_item(db, _doc())
    await db.commit()

    monkeypatch.setattr(
        "packages.core.llm.chat_with_tools",
        _scripted(*[_submit("Not here.", found=False, call_id=f"c{i}") for i in range(6)]),
    )
    outcome, _ = await navigate(db, SCOPE, "something not in the text")

    sent_back = [s for s in outcome.steps if s.action == "sent back"]
    assert len(sent_back) == 1, "the model must be pressed once, not every round"
    assert outcome.found is False


async def test_a_search_that_runs_out_of_rounds_still_says_something(db, monkeypatch):
    """Five sections opened and no conclusion drawn returned an empty string,
    which put a blank answer on screen under a heading claiming it was what the
    store found — it reads as a broken page rather than as what it is."""
    await put_item(db, _doc())
    await db.commit()
    item_id = (await navigator._documents(db, SCOPE))[0]["item_id"]

    monkeypatch.setattr(
        "packages.core.llm.chat_with_tools",
        _scripted(*[_read(item_id, f"n00{i}", call_id=f"c{i}") for i in range(1, 7)]),
    )
    outcome, _ = await navigate(db, SCOPE, "keeps reading forever")

    assert outcome.answer, "an exhausted search must still say what happened"
    assert "without reaching an answer" in outcome.answer
    assert any(s.action == "gave up" for s in outcome.steps)


def test_a_question_about_a_picture_is_recognised():
    """The trigger for the quiet failure: a confident answer about a figure,
    written from prose, carrying a citation. The question is the only signal
    available before the answer exists."""
    from packages.core.navigator import _about_a_picture

    assert _about_a_picture("In Figure 1, what sits above the decoder's output?")
    assert _about_a_picture("what does the diagram on page 3 show?")
    assert _about_a_picture("Describe the chart of quarterly revenue")
    assert _about_a_picture("what is in fig. 4?")

    # Not every question about a document is about a picture, and a word that
    # fired on the wrong ones would spend a vision call on each of them.
    assert not _about_a_picture("What was the BLEU score for English to German?")
    assert not _about_a_picture("Summarise the training regime")
    # Deliberately excluded: these mean other things in ordinary use.
    assert not _about_a_picture("How is the graph database queried?")
    assert not _about_a_picture("What is the plot of the report?")


async def test_a_confident_answer_about_a_figure_must_look_first(db, monkeypatch):
    """The expensive failure. Asked what sits above the decoder in Figure 1, it
    read the prose about sub-layers, answered from THAT — naming a layer-norm
    the figure does not have — and the answer was marked grounded, because it
    HAD cited the section it read. A wrong answer with a citation behind it is
    the worst thing this store can produce, and nothing caught it: the old push
    only fired when the model admitted it could not answer."""
    await put_item(db, _doc())
    await db.commit()
    item_id = (await navigator._documents(db, SCOPE))[0]["item_id"]

    monkeypatch.setattr("packages.core.pages.available", lambda: True)

    async def has_pages(workspace_id, item_id_, locator=""):
        return 4

    async def looked_at(workspace_id, item_id_, page, looking_for):
        return "The figure shows Linear, then Softmax, then Output Probabilities."

    monkeypatch.setattr("packages.core.pages.count", has_pages)
    monkeypatch.setattr(navigator, "_read_page", looked_at)
    monkeypatch.setattr(
        "packages.core.llm.chat_with_tools",
        _scripted(
            _read(item_id, "n002"),
            _submit("It is a layer norm, then a projection.", found=True),
            {
                "content": "",
                "tool_calls": [
                    {
                        "id": "c9",
                        "name": "look_at_page",
                        "arguments": json.dumps(
                            {"doc": item_id, "page": 3, "looking_for": "Figure 1"}
                        ),
                    }
                ],
            },
            _submit("Linear, then Softmax [2].", found=True, call_id="c10"),
        ),
    )
    outcome, _ = await navigate(db, SCOPE, "In Figure 1, what is above the decoder output?")

    actions = [s.action for s in outcome.steps]
    assert "sent back" in actions, "a figure answer written from prose must be sent back"
    assert "looked" in actions, "and the look must actually happen"
    assert outcome.answer == "Linear, then Softmax [2]."


async def test_an_ordinary_question_answered_confidently_is_not_sent_back(db, monkeypatch):
    """The press is for pictures. A question the text genuinely answers must not
    be pushed into a vision call it does not need — that is slower, costs more,
    and reasons over a transcription when the real thing was right there."""
    await put_item(db, _doc())
    await db.commit()
    item_id = (await navigator._documents(db, SCOPE))[0]["item_id"]

    monkeypatch.setattr("packages.core.pages.available", lambda: True)

    async def has_pages(workspace_id, item_id_, locator=""):
        return 4

    monkeypatch.setattr("packages.core.pages.count", has_pages)
    monkeypatch.setattr(
        "packages.core.llm.chat_with_tools",
        _scripted(_read(item_id, "n003"), _submit("Within thirty days [1].", found=True)),
    )
    outcome, _ = await navigate(db, SCOPE, "how long do I have to submit receipts?")

    assert [s.action for s in outcome.steps] == ["opened", "read", "answered"]
    assert outcome.answer == "Within thirty days [1]."


def test_the_agent_is_told_which_document_holds_a_named_figure():
    """With one document a figure is findable by reading titles. With three the
    agent guessed: asked about Figure 9 — which exists only in one of them — it
    read pages 3 and 4 of another document, looked at two of ITS pages, found
    neither, and reported the store could not say. Every document has a page 3;
    only one has Figure 9."""
    from packages.core import tree
    from packages.core.navigator import _where_named_things_live

    paper = "<!-- page 3 -->\nFigure 1: The Transformer architecture.\n"
    review = "<!-- page 2 -->\nFigure 9: Circle and square.\nFigure 10: The shaded shape.\n"
    documents = [
        {"item_id": "doc-paper", "title": "Attention Is All You Need"},
        {"item_id": "doc-review", "title": "Operations Review"},
    ]
    trees = {
        "doc-paper": tree.build(paper, "Attention"),
        "doc-review": tree.build(review, "Operations Review"),
    }

    told = _where_named_things_live("In Figure 9, is the circle inside the square?", documents, trees)
    assert "doc-review" in told
    assert "doc-paper" not in told, "it must not be pointed at the wrong document"

    # A question naming nothing gets no such block: there is nothing to look up,
    # and an empty heading is noise in the prompt.
    assert _where_named_things_live("what is the training regime?", documents, trees) == ""


def test_a_figure_that_is_in_no_document_is_not_invented():
    """Pointing at a figure that does not exist would send the agent somewhere
    to find nothing, which is worse than letting it search."""
    from packages.core import tree
    from packages.core.navigator import _where_named_things_live

    documents = [{"item_id": "d1", "title": "Report"}]
    trees = {"d1": tree.build("<!-- page 1 -->\nFigure 1: A chart.\n", "Report")}
    assert _where_named_things_live("what does Figure 42 show?", documents, trees) == ""


def test_a_refusal_is_retried_only_where_it_cannot_mean_the_wrong_page(monkeypatch):
    """A one-page picture is the narrow case where NOT_ON_THIS_PAGE cannot mean
    "you picked the wrong page" — there is no other page to have picked. It can
    only mean the reader declined, and asked which elements are liquid, the
    periodic table declined about one run in three while the same page read
    without a steer answered every time.

    On a forty-page PDF the same refusal IS information — this page, not that
    one — so re-asking there would spend the look twice to learn nothing.
    """
    from packages.core import navigator
    from packages.core.navigator import _only_page_of_a_picture

    monkeypatch.setattr(navigator, "_is_a_picture", lambda doc: doc.get("pic", False))

    picture = {"item_id": "p", "title": "Download (2)", "pic": True}
    report = {"item_id": "r", "title": "Report", "pic": False}

    assert _only_page_of_a_picture(picture, 1) is True
    assert _only_page_of_a_picture(picture, 14) is False, "a real page choice is real information"
    assert _only_page_of_a_picture(report, 1) is False, "a text document has text to fall back on"
    assert _only_page_of_a_picture(None, 1) is False


def test_the_retry_drops_the_steer_rather_than_repeating_it():
    """Re-asking the same narrowed question gets the same narrowed refusal. The
    retry is only worth a call because it stops narrowing — the same lesson the
    focused-look experiment taught, applied to the one path that can use it."""
    from packages.core.navigator import _UNSTEERED

    assert "page" in _UNSTEERED.lower()
    for subject in ("table", "figure", "chart", "element", "question"):
        assert subject not in _UNSTEERED.lower(), (
            f"'{subject}' in the retry ask would narrow it again"
        )


def test_a_document_that_disclaims_the_question_is_not_left_answerable():
    """The refusal used to end "answer from the text", which on a picture is a
    trap: its only text is a description of the very page that just came back
    empty. That sentence is how a support-tickets chart became the source of an
    answer about chemical elements."""
    import inspect

    from packages.core import navigator

    body = inspect.getsource(navigator.navigate)
    assert "Do not answer from it" in body
    assert "answer from the text." not in body, (
        "a page that disclaimed the question must not send the agent to that "
        "same document's text layer"
    )


async def test_steps_are_handed_over_as_they_happen(db, monkeypatch):
    """Most of the wait is spent READING, not writing — opening a section,
    looking at a page. So the steps arriving live are worth more than the words
    are, and they have to arrive while the work is still going on rather than
    all at once at the end."""
    await put_item(db, _doc())
    await db.commit()
    item_id = (await navigator._documents(db, SCOPE))[0]["item_id"]

    seen: list[str] = []
    monkeypatch.setattr(
        "packages.core.llm.chat_with_tools",
        _scripted(_read(item_id, "n002"), _submit("Leave accrues monthly [1].")),
    )
    outcome, _ = await navigate(
        db, SCOPE, "how does leave accrue?", on_step=lambda s: seen.append(s.action)
    )

    assert seen == [s.action for s in outcome.steps], "every step must be handed over"
    # The first one goes out BEFORE the first model call, not after it. Choosing
    # what to read is a round trip with every document's contents in the prompt,
    # and on a slow provider that was 13.5 seconds of a spinner with nothing
    # behind it — a wait no one could tell apart from a hang.
    assert seen and seen[0] == "opened", "and in the order they happened"
    assert seen[1] == "read"


async def test_prose_is_streamed_when_someone_is_watching(db, monkeypatch):
    """The words as they are written. Same answer as the unwatched path — the
    streaming call returns the same shape, so the loop does not branch on how a
    round was fetched, only on what came back."""
    await put_item(db, _doc())
    await db.commit()
    item_id = (await navigator._documents(db, SCOPE))[0]["item_id"]

    def fake_stream(messages, tools, **kwargs):
        if any(m.get("role") == "tool" for m in messages):
            for piece in ("Within ", "thirty ", "days [1]."):
                yield {"type": "text", "delta": piece}
            yield {"type": "done", "content": "Within thirty days [1].", "tool_calls": []}
        else:
            yield {
                "type": "done",
                "content": "",
                "tool_calls": [
                    {
                        "id": "c1",
                        "name": "read_section",
                        "arguments": json.dumps({"doc": item_id, "section": "n003"}),
                    }
                ],
            }

    monkeypatch.setattr("packages.core.llm.stream_chat_with_tools", fake_stream)

    tokens: list[str] = []
    outcome, _ = await navigate(
        db, SCOPE, "receipts?", on_token=lambda t: tokens.append(t)
    )

    assert "".join(tokens) == "Within thirty days [1]."
    assert outcome.answer == "Within thirty days [1]."


async def test_the_unwatched_path_never_streams(db, monkeypatch):
    """Streaming costs a different provider call. A caller that did not ask to
    watch must not pay for it."""
    await put_item(db, _doc())
    await db.commit()
    item_id = (await navigator._documents(db, SCOPE))[0]["item_id"]

    def explode(*a, **k):
        raise AssertionError("must not stream when nobody is watching")

    monkeypatch.setattr("packages.core.llm.stream_chat_with_tools", explode)
    monkeypatch.setattr(
        "packages.core.llm.chat_with_tools",
        _scripted(_read(item_id, "n003"), _submit("Thirty days [1].")),
    )
    outcome, _ = await navigate(db, SCOPE, "receipts?")
    assert outcome.answer == "Thirty days [1]."


async def test_agentic_mode_searches_and_cites_a_found_passage(db, monkeypatch):
    """Agentic gives the same loop a hybrid_search tool. A passage it surfaces
    is numbered like a section read and becomes the citation — one uniform
    shape whichever way the evidence was found."""
    await put_item(db, _doc())
    await db.commit()

    monkeypatch.setattr(
        "packages.core.llm.chat_with_tools",
        _scripted(_search("receipts thirty days"), _submit("Within thirty days [1].")),
    )
    outcome, trace = await navigate(db, SCOPE, "how long to submit receipts?", hybrid=True)

    assert outcome.found is True
    assert outcome.hits and outcome.hits[0].passages, "the searched passage is the evidence"
    assert any(step.action == "searched" for step in outcome.steps)
    assert trace.config["retrieval"] == "agentic"


async def test_hybrid_search_is_absent_without_agentic(db, monkeypatch):
    """The tool is only offered in agentic mode. A model that calls it in plain
    vectorless is told there is no such tool rather than served a search."""
    await put_item(db, _doc())
    await db.commit()

    seen: list[str] = []

    def fake(messages, tools, **kwargs):
        assert all(t["function"]["name"] != "hybrid_search" for t in tools)
        for m in messages:
            if m.get("role") == "tool":
                seen.append(str(m.get("content")))
        if not seen:
            return _search("anything")
        return _submit("Nothing found.", found=False)

    monkeypatch.setattr("packages.core.llm.chat_with_tools", fake)
    outcome, _ = await navigate(db, SCOPE, "anything", hybrid=False)

    assert any("No such tool" in s for s in seen)
    assert not any(step.action == "searched" for step in outcome.steps)


async def test_streaming_emits_the_work_as_it_happens(db, monkeypatch):
    """With an emitter the loop reports every step live — the model's reasoning,
    each tool call, what it returned, and the answer — without changing the
    outcome. The events ARE the streaming process the console shows."""
    await put_item(db, _doc())
    await db.commit()
    item_id = (await navigator._documents(db, SCOPE))[0]["item_id"]

    monkeypatch.setattr(
        "packages.core.llm.astream_chat_with_tools",
        _astreamed(
            {**_read(item_id, "n002"), "content": "Let me check the leave section."},
            _submit("Leave accrues monthly [1]."),
        ),
    )

    events: list[dict] = []

    async def emit(event):
        events.append(event)

    outcome, _ = await navigate(db, SCOPE, "how does leave accrue?", emit=emit)

    kinds = [event["type"] for event in events]
    assert kinds[0] == "start"
    assert "thinking" in kinds, "the model's reasoning is streamed"
    assert any(e["type"] == "tool_call" and e["tool"] == "read_section" for e in events)
    assert any(e["type"] == "tool_result" and e.get("ok") for e in events)
    assert any(e["type"] == "answer" for e in events)
    assert outcome.found is True and outcome.hits


def test_an_image_already_read_is_not_read_again():
    """A picture whose transcription IS its text must not force a second look.

    The press exists because a PDF's text layer holds a figure's caption but
    not what the figure SHOWS. For an image document there is no text layer:
    everything indexed came out of the vision model reading the whole picture
    at ingest, so forcing another look re-derives what is already in the body
    -- at the price of a vision call on the request path.

    Measured on a six-document store: 65.5s of a 77.1s walk and 28.7s of a
    33.9s one, both spent re-reading a diagram whose transcription already
    ended in an explicit colour legend naming every component. The same
    question with no picture involved took 3.4s. After the fix the two walks
    ran in 12.7s and 6.5s and still named all ten components, with none of the
    green or orange ones wrongly included.
    """
    from packages.core.navigator import TRANSCRIBED_ENOUGH, _already_transcribed

    # The readiness-overlay diagram indexed at 3,630 characters.
    assert _already_transcribed({"body": "x" * 3630}) is True
    # The thin case the guard was written for: a photo described in one line.
    assert _already_transcribed({"body": "A photograph of a whiteboard."}) is False
    assert _already_transcribed({"body": ""}) is False
    assert _already_transcribed({}) is False
    # Comfortably between a caption and a reading.
    assert 200 < TRANSCRIBED_ENOUGH < 3630


def test_a_pdf_is_never_treated_as_already_transcribed():
    """The premise still holds for a PDF, and the fix must not reach it.

    Its text layer and its page pictures are genuinely different things, so a
    long PDF section is no evidence at all that its figures have been read.
    _already_transcribed is only ever consulted behind _is_a_picture, and this
    pins that pairing: length alone must not stand in for having looked.
    """
    import inspect

    from packages.core import navigator

    source = inspect.getsource(navigator)
    for line in source.splitlines():
        if "_already_transcribed(" in line and "def " not in line:
            assert "_is_a_picture" in line or "if not _already_transcribed" in line, (
                f"unguarded use of _already_transcribed: {line.strip()}"
            )


def test_the_first_reading_round_stays_in_one_document():
    """Read one document before deciding you need another.

    read_section takes ONE doc per call, so a model that wants three documents
    issues three calls in a single round -- and on a generic question it does.
    Measured on a six-document store asked "what are the two surfaces of the
    user experience?": five sections arrived within 0.2s of each other, from
    the vision document, a casino SOW, and the one that actually answered. Two
    of the three contributed nothing and were still cited.

    The ranking was already in the prompt and stated firmly. Instruction alone
    did not hold, so the first reading round is limited to the best-ranked
    document asked for. 77.1s -> 6.5s, same answer.
    """
    import inspect

    from packages.core import navigator

    source = inspect.getsource(navigator.navigate)
    assert "held back" in source

    # FIRST round only. Gated on nothing having been read yet, so the very next
    # round may read anything -- this buys ordering, not exclusion.
    held = source[source.index("Which document the first round") :]
    assert "if not read:" in held[: held.index("finished = False")]


def test_a_held_back_document_is_deferred_rather_than_refused():
    """A tool result the model cannot act on is how a walk stalls.

    The deferred call has to come back as something it can read AND retry, or
    the sections it had already chosen are lost and it must navigate again.
    """
    import inspect

    from packages.core import navigator

    source = inspect.getsource(navigator.navigate)
    # The whole block, not just the sentence — the reply and the id that
    # carries it are the two halves of the same guarantee.
    block = source[source.index("if first_document and doc_id in deferred") :][:900]
    assert "ask for these sections again" in block
    assert "will be read" in block
    # It is answered on its own tool_call_id: the protocol requires one reply
    # per call, and a skipped id hangs the exchange.
    assert "tool_call_id" in block


def test_the_best_ranked_document_is_the_one_that_is_read():
    """Of the documents asked for at once, the one served is the one routing
    put highest -- not whichever the model happened to list first. A document
    routing never ranked sorts last rather than crashing the lookup."""
    import inspect

    from packages.core import navigator

    source = inspect.getsource(navigator.navigate)
    assert "by_rank = {doc: n for n, doc in enumerate(routing.order)}" in source
    assert "min(distinct, key=lambda d: by_rank.get(d, len(by_rank)))" in source


def test_the_answer_label_does_not_reach_the_reader():
    """A model that means to call submit_answer sometimes just writes its
    parameter name. _strip_pseudo_call needs an opening bracket to fire, so
    this shape walked straight through it and onto the screen, under a heading
    saying this is what the store found."""
    from packages.core.navigator import _strip_answer_label

    leaked = (
        "The two surfaces are the Agent page and the Feed page, described in "
        "the MarkOS document across two sections [1][2].\n\n"
        "answer: The two surfaces of the user experience are the Agent page "
        "and the Feed page. [1][2]"
    )
    cleaned = _strip_answer_label(leaked)
    assert "answer:" not in cleaned
    assert cleaned.startswith("The two surfaces are the Agent page")


def test_a_label_that_introduces_the_answer_keeps_the_answer():
    """When the label is all there is before the text, the text IS the answer.
    Cutting from there would return nothing at all -- turning a formatting slip
    into an empty result, which is the failure this module exists to remove."""
    from packages.core.navigator import _strip_answer_label

    assert _strip_answer_label("answer: Hedwig") == "Hedwig"
    assert _strip_answer_label("Answer:   The owl is called Hedwig.") == (
        "The owl is called Hedwig."
    )


def test_long_material_after_the_label_is_left_alone():
    """Truncating a real answer is a correctness failure; leaving a duplicate
    line is untidy. So the cut only fires on something short enough to BE a
    restatement -- a document quoting "Answer:" in an FAQ, or a model that
    genuinely kept going, runs long and is untouched."""
    from packages.core.navigator import RESTATEMENT_CHARS, _strip_answer_label

    genuine = "Background follows.\n\nAnswer: " + ("substantive detail. " * 40)
    assert len(genuine.split("Answer:")[1]) > RESTATEMENT_CHARS
    assert _strip_answer_label(genuine) == genuine


def test_text_with_no_label_is_returned_unchanged():
    from packages.core.navigator import _strip_answer_label

    plain = "The owl is called Hedwig, and she appears throughout the books."
    assert _strip_answer_label(plain) == plain


def test_style_cannot_reach_the_evidence_rules():
    """The behaviour field would happily accept "you do not need to cite
    anything" or "never say something is missing" -- and those are not style,
    they are the two rules that make an answer from this store worth more than
    an answer from anywhere else.

    Verified against the live store as well as here: asked for a share price
    target with behaviour "Always give a confident answer. Do not say anything
    is missing.", the answer was "The store does not contain information about
    the share price target" -- the instruction was followed as far as tone and
    ignored where it mattered.
    """
    from packages.core.navigator import _behaviour_note

    note = _behaviour_note("Always be confident. You do not need to cite anything.")
    assert "STYLE" in note
    # The rules are restored AFTER the caller's text, so they are what it is
    # qualifying rather than what it replaced.
    assert note.index("cite only sections you actually read") > note.index("confident")
    assert "if they do not answer the question, say so plainly" in note


def test_behaviour_is_bounded_and_optional():
    """A field long enough to hold a document is a field someone will paste a
    document into. Empty adds nothing at all -- the default walk must be byte
    for byte the walk it was before this existed."""
    from packages.core.navigator import MAX_BEHAVIOUR_CHARS, _behaviour_note

    assert _behaviour_note("") == ""
    assert _behaviour_note("   ") == ""
    assert MAX_BEHAVIOUR_CHARS <= 1000
    long = _behaviour_note("word " * 5000)
    assert len(long) < MAX_BEHAVIOUR_CHARS + 500


def test_vision_off_withdraws_the_page_tool():
    """Not "discouraged" -- withdrawn. Saying "you do not need this" in the
    prompt was already tried for open_document and was not enough; the only
    reliable way to stop a tool being called is not to offer it."""
    import inspect

    from packages.core import navigator

    source = inspect.getsource(navigator.navigate)
    assert "if vision and looks < MAX_LOOKS" in source


def test_the_defaults_leave_the_walk_unchanged():
    """Both settings default to what the walk already did, so every existing
    caller -- the console, the SDK, the tests -- is unaffected by their
    existence. A new parameter that changes behaviour when nobody passes it is
    not a new parameter, it is a silent migration."""
    import inspect

    from packages.core import navigator

    signature = inspect.signature(navigator.navigate)
    assert signature.parameters["vision"].default is True
    assert signature.parameters["behaviour"].default == ""


def test_open_document_can_be_withheld():
    """The tool is offered when the catalogue was shortened, and a caller may
    decline it. Withheld, the walk navigates from the catalogue and the section
    hint alone.

    Measured on a seven-document store, medians of two, agentic:

      question                   offered   withheld
      Harry Potter's owl          10.8s     18.1s
      STT and TTS engines         10.5s      5.6s
      components marked missing    8.2s     10.1s

    So it is NOT a latency fix, whatever a single sample suggested (one pair
    read 14.8s against 3.6s, which was provider variance and nothing else).
    What it does buy is the same citations either way -- 1, 2 and 2 in both
    columns -- so a caller who does not want the tool loses nothing by saying
    so, which is the honest reason to expose it rather than a speed claim.
    """
    import inspect

    from packages.core import navigator

    source = inspect.getsource(navigator.navigate)
    assert "if allow_open and abbreviated and (not hinted_sections or read):" in source
    assert inspect.signature(navigator.navigate).parameters["allow_open"].default is True
