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
