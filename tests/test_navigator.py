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

    assert [s.action for s in outcome.steps] == ["sent back", "read", "answered"]
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

    assert [s.action for s in outcome.steps] == ["read", "answered"]
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
    assert seen and seen[0] == "read", "and in the order they happened"


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
