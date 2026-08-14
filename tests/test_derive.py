"""Derived attributes: a signal for ranking, never an authority.

The rule these tests exist to keep true:

    TRUSTED METADATA CONSTRAINS RETRIEVAL. DERIVED METADATA ONLY IMPROVES
    DISCOVERY AND RANKING WITHIN THE SCOPE THAT CONSTRAINT ALREADY ALLOWED.

A boost cannot add a document to a result set — only reorder one the trusted
filter already permitted — so a wrong attribute costs ranking quality and can
never cost isolation. The moment a derived value reaches the constraint path
that stops being true, silently, and a multi-tenant platform scoping retrieval
by `client` would be filtering on a model's guess.

Several of these are tripwires in the style of tests/test_tripwire.py: they read
the source, because the property is about what the code MAY NOT do.
"""

from __future__ import annotations

import inspect

import pytest

from packages.core import derive, search

# ------------------------------- the boundary -------------------------------


def test_the_constraint_path_never_reads_derived():
    """The one that matters.

    `_filters` builds the WHERE that every arm is held to, and
    `_narrow_to_metadata` resolves a filter to a document set before either arm
    runs. If either ever consults `derived`, a model's guess has become a
    tenancy filter.
    """
    for function in (search._filters, search._narrow_to_metadata):
        source = inspect.getsource(function)
        assert "derived" not in source, (
            f"{function.__name__} reads derived metadata — a ranking signal has "
            "become a constraint"
        )


def test_the_hydration_query_selects_no_derived_column():
    # Belt and braces on the same boundary, from the other side: even if a
    # caller asked, the constrained read does not carry the column.
    assert "derived" not in search._HYDRATE


def test_a_reserved_key_can_never_be_written():
    """Refused at write time, where it is visible.

    Not because anything reads `derived.client_id` as authority today, but
    because a future bulk edit or migration that copies derived values across
    would move exactly these keys into the asserted column, and nobody would
    see the boundary being crossed.
    """
    for key in ("client_id", "owner_id", "workspace_id", "access_control", "acl"):
        assert key in derive.RESERVED_KEYS
        assert derive.sanitise({key: "acme", "doc_type": "invoice"}) == {
            "doc_type": "invoice"
        }


def test_a_reserved_key_is_dropped_not_renamed():
    # There is no path by which a value the model invented becomes something
    # the store treats as asserted — including a helpfully-renamed one.
    out = derive.sanitise({"client_id": "acme", "tenant_id": "globex"})
    assert out == {}
    assert "acme" not in str(out) and "globex" not in str(out)


def test_the_model_is_told_not_to_decide_ownership():
    # The guard above is the enforcement; this is the instruction. Both, because
    # a model that never emits the field costs nothing to filter.
    assert "client" in derive._PROMPT
    assert "not yours to decide" in derive._PROMPT


# ------------------------------- what is kept -------------------------------


def test_the_field_set_is_closed():
    """An open-ended "describe this document" produces a different vocabulary
    per document, and a facet nobody can enumerate is one nobody can filter or
    boost on."""
    out = derive.sanitise(
        {
            "doc_type": "Statement of Work",
            "topics": ["Conversational AI", "casino"],
            "entities": ["Temprl", "Whisper"],
            "year": "2026",
            "sentiment": "positive",
            "confidence": 0.9,
        }
    )
    assert set(out) == {"doc_type", "topics", "entities", "year"}
    assert out["doc_type"] == "statement of work"
    assert out["topics"] == ["conversational ai", "casino"]
    assert out["entities"] == ["Temprl", "Whisper"]  # names keep their case


def test_lists_are_capped_and_deduplicated():
    out = derive.sanitise(
        {
            "topics": ["a", "a", "b", "c", "d", "e", "f", "g", "h"],
            "entities": [f"E{n}" for n in range(20)],
        }
    )
    assert len(out["topics"]) <= derive.MAX_TOPICS
    assert len(out["entities"]) <= derive.MAX_ENTITIES
    assert out["topics"].count("a") == 1


def test_a_year_must_look_like_a_year():
    assert derive.sanitise({"year": "2026"})["year"] == "2026"
    for nonsense in ("last year", "20260", "n/a", "3026", ""):
        assert "year" not in derive.sanitise({"year": nonsense})


def test_empty_is_a_correct_answer():
    """A wrong tag is worse than a missing one, so nothing is invented to avoid
    an empty field."""
    assert derive.sanitise({}) == {}
    assert derive.sanitise({"topics": [], "entities": [], "doc_type": ""}) == {}


def test_nested_junk_is_refused():
    # Only scalars and lists of scalars. A nested object would not be reachable
    # by `->>` anyway, so storing one would be a filter that silently never
    # matches.
    out = derive.sanitise({"doc_type": {"kind": "invoice"}, "topics": [{"a": 1}, "real"]})
    assert "doc_type" not in out
    assert out["topics"] == ["real"]


# ------------------------------ the improvement ------------------------------


def test_the_card_line_reads_as_text_not_json():
    """This text is embedded and matched against a question.

    "Document type: statement of work" reads to an embedding model roughly as a
    question about statements of work does. Braces do not.
    """
    line = derive.as_card_line(
        {
            "doc_type": "statement of work",
            "topics": ["conversational ai"],
            "entities": ["Temprl"],
            "year": "2026",
        }
    )
    assert "{" not in line and '"' not in line
    assert "Document type: statement of work." in line
    assert "Mentions: Temprl." in line


def test_nothing_derived_means_nothing_appended():
    assert derive.as_card_line({}) == ""


def test_the_improvement_lands_on_the_card():
    """Which is why there is no new ranking code, and no new risk.

    routing.py already scores documents on their card, and the card is already
    embedded and searched — so an inferred topic starts helping the moment it is
    written down.
    """
    source = inspect.getsource(
        __import__("packages.core.summarize", fromlist=["summarize_document"]).summarize_document
    )
    assert "derive.describe" in source
    assert "derive.store" in source
    assert "as_card_line" in source


def test_a_card_can_never_become_evidence():
    """The safety property is INHERITED from a rule that already existed.

    An answer may be ROUTED by a derived attribute and can never be CITED to
    one, because retrieval has always excluded cards from evidence. If that
    exclusion ever goes, this whole design goes with it.
    """
    from packages.core import chunks

    assert "node_type NOT IN ('card', 'section_summary')" in inspect.getsource(
        chunks.keyword_search
    )


# ------------------------------- failure modes -------------------------------


@pytest.mark.asyncio
async def test_a_failed_extraction_is_not_a_failed_ingest():
    """A store that could not ingest because a labelling call timed out would
    be a worse product than one that labels nothing."""

    def boom(*_args, **_kwargs):
        raise RuntimeError("provider down")

    original = derive.chat
    derive.chat = boom  # type: ignore[assignment]
    try:
        assert await derive.describe("a card") == {}
    finally:
        derive.chat = original  # type: ignore[assignment]


@pytest.mark.asyncio
async def test_prose_instead_of_json_yields_nothing():
    async def prose(_card, _model):
        return "Sure! This document appears to be a statement of work."

    original = derive._ask
    derive._ask = prose  # type: ignore[assignment]
    try:
        assert await derive.describe("a card") == {}
    finally:
        derive._ask = original  # type: ignore[assignment]


@pytest.mark.asyncio
async def test_json_wrapped_in_chatter_is_still_read():
    async def wrapped(_card, _model):
        return 'Here you go:\n```json\n{"doc_type": "invoice"}\n```\nHope that helps!'

    original = derive._ask
    derive._ask = wrapped  # type: ignore[assignment]
    try:
        assert await derive.describe("a card") == {"doc_type": "invoice"}
    finally:
        derive._ask = original  # type: ignore[assignment]


@pytest.mark.asyncio
async def test_an_empty_card_asks_nothing():
    # No card, no call. A document with nothing summarised has nothing to
    # describe, and asking anyway is a bill for an empty answer.
    called = False

    async def watch(_card, _model):
        nonlocal called
        called = True
        return "{}"

    original = derive._ask
    derive._ask = watch  # type: ignore[assignment]
    try:
        assert await derive.describe("   ") == {}
        assert called is False
    finally:
        derive._ask = original  # type: ignore[assignment]
