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


def test_the_vocabulary_is_open():
    """The model names its own fields.

    A closed set was tried first and was the wrong instinct for this mechanism.
    A fixed enum helps FILTERING, where two documents must agree on a label to
    be selected together. Here the attributes are rendered into the card and the
    card is embedded, so a document-shaped phrase carries more signal than a
    generic bucket. Forcing a vocabulary discards the part that helps.
    """
    out = derive.sanitise(
        {
            "document_kind": "Statement of Work",
            "purpose": "define scope and deliverables",
            "audience": ["casino operators", "engineering"],
            "systems_named": ["Whisper", "Kokoro TTS"],
            "period": "2026",
        }
    )
    assert set(out) == {
        "document_kind",
        "purpose",
        "audience",
        "systems_named",
        "period",
    }
    assert out["audience"] == ["casino operators", "engineering"]


def test_two_documents_may_be_described_completely_differently():
    a = derive.sanitise({"legal_form": "tender", "jurisdiction": "karnataka"})
    b = derive.sanitise({"protocol_layer": "data link", "standard": "ieee 802.3"})
    assert set(a).isdisjoint(set(b))
    assert a and b


def test_the_shape_is_not_open_even_though_the_vocabulary_is():
    """Flat keys, scalars or lists of scalars, everything capped.

    Open vocabulary is not open season on the record: a nested object cannot be
    reached by `->>` and an unbounded one would let a runaway model write a page
    into a navigation surface.
    """
    out = derive.sanitise({"meta": {"a": 1}, "topics": [{"x": 1}, "real"], "count": 5})
    assert "meta" not in out
    assert out["topics"] == ["real"]
    assert out["count"] == "5"  # scalars are kept, as text


def test_everything_is_capped():
    out = derive.sanitise(
        {
            **{f"field_{n}": "value" for n in range(40)},
            "many": [f"v{n}" for n in range(50)],
            "long": "x" * 500,
        }
    )
    assert len(out) <= derive.MAX_KEYS
    if "many" in out:
        assert len(out["many"]) <= derive.MAX_LIST
    if "long" in out:
        assert len(out["long"]) <= derive.MAX_VALUE_CHARS


def test_a_reserved_word_anywhere_in_a_field_name_is_refused():
    """Not only the exact spellings someone thought to list.

    `client_name`, `owner_email` and `account_reference` all name ownership as
    surely as `client_id` does, and a per-word check catches the ones nobody
    enumerated.
    """
    out = derive.sanitise(
        {
            "client_name": "acme",
            "owner_email": "x@y.z",
            "account_reference": "A1",
            "tenant_slug": "globex",
            "topics": ["kept"],
        }
    )
    assert out == {"topics": ["kept"]}


def test_a_reserved_key_spelled_differently_is_still_refused():
    """Normalisation runs BEFORE the reserved check, which is the whole reason
    it exists here: "Client ID" and "client-id" must both fail."""
    out = derive.sanitise(
        {"Client ID": "acme", "client-id": "acme", "CLIENT_ID": "acme", "ok_field": "kept"}
    )
    assert out == {"ok_field": "kept"}


def test_empty_is_a_correct_answer():
    """A wrong tag is worse than a missing one, so nothing is invented to avoid
    an empty object."""
    assert derive.sanitise({}) == {}
    assert derive.sanitise({"topics": [], "kind": ""}) == {}


# ------------------------------ the improvement ------------------------------


def test_the_card_line_reads_as_text_not_json():
    """This text is embedded and matched against a question.

    "Document type: statement of work" reads to an embedding model roughly as a
    question about statements of work does. Braces do not.
    """
    line = derive.as_card_line(
        {
            "document_kind": "statement of work",
            "subjects": ["conversational ai"],
            "systems_named": ["Temprl"],
        }
    )
    assert "{" not in line and '"' not in line
    assert "Document kind: statement of work." in line
    assert "Systems named: Temprl." in line


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
async def test_a_transient_error_is_retried_not_abandoned():
    """The failure a retry exists for.

    This gave up on the first exception, so a backfill firing one call per
    document met a rate limit and four of eight came back undescribed — while
    the model was answering every one of them correctly when asked again.
    """
    calls = 0

    async def rate_limited_once(_card, _model):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("429 Too Many Requests")
        return '{"document_type": "policy"}'

    original = derive._ask
    derive._ask = rate_limited_once  # type: ignore[assignment]
    try:
        assert await derive.describe("a card") == {"document_type": "policy"}
        assert calls == 2
    finally:
        derive._ask = original  # type: ignore[assignment]


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


# ------------------- generated once, at ingest, and reused -------------------


def test_no_retrieval_path_ever_regenerates():
    """Derived once, at ingest, and read from then on.

    A model call on the read path would put a per-question cost on every
    question — the exact mistake query expansion made inside the navigator,
    where agentic went from 5s to 70s because each search spent its own
    expansion call and no cache could help.

    So this is a tripwire, not a convention: every module a question passes
    through is read, and none of them may call describe().
    """
    from packages.core import answer, navigator, routing, search

    for module in (search, answer, navigator, routing):
        source = inspect.getsource(module)
        assert "derive.describe" not in source, (
            f"{module.__name__} regenerates derived metadata on the read path"
        )
        assert "derive.sanitise" not in source


def test_it_is_generated_where_the_card_is():
    """The card and the section summaries are the input, so extraction belongs
    where they are written — one place, holding everything it needs, on the
    ingest side of the line."""
    from packages.core import summarize

    source = inspect.getsource(summarize.summarize_document)
    assert "derive.describe" in source
    assert "derive.store" in source


def test_the_stored_attributes_are_what_retrieval_reuses():
    """Written to their own column AND into the card text.

    The column is the record; the card text is what routing and the vector
    index actually see. Storing only the column would be a fact nobody could
    retrieve on.
    """
    from packages.core import summarize

    source = inspect.getsource(summarize.summarize_document)
    assert "as_card_line" in source
    assert "card_body" in source


# ------------------------- what a model actually sends -------------------------


def test_a_javascript_object_literal_is_still_read():
    """Not an edge case — measured, and expensive.

    Asked for JSON with an open field set, the provider replied with unquoted
    keys and sometimes unquoted values. `json.loads` rejects all of it, and
    three of eight real documents were silently derived as `{}` before this
    existed.
    """
    got = derive._loads_lenient(
        "{   document_type: technical overview\n"
        "  subject: bitcoin\n"
        "  topics: [peer-to-peer electronic cash, merkle trees, proof-of-work] }"
    )
    assert got == {
        "document_type": "technical overview",
        "subject": "bitcoin",
        "topics": ["peer-to-peer electronic cash", "merkle trees", "proof-of-work"],
    }


def test_unquoted_keys_with_quoted_values_are_read():
    got = derive._loads_lenient(
        '{ document_type: "technical design specification",\n'
        '  systems_integrated: ["loyalty system", "gaming system"] }'
    )
    assert got["document_type"] == "technical design specification"
    assert got["systems_integrated"] == ["loyalty system", "gaming system"]


def test_well_formed_json_is_never_touched():
    """The repair only ever runs after a strict parse has already failed."""
    strict = '{"a": "b", "c": ["d", "e"], "n": 3, "ok": true}'
    assert derive._loads_lenient(strict) == {
        "a": "b",
        "c": ["d", "e"],
        "n": 3,
        "ok": True,
    }


def test_unrepairable_output_yields_nothing_rather_than_guesswork():
    assert derive._loads_lenient("not an object at all") is None
    assert derive._loads_lenient("{{{") is None


@pytest.mark.asyncio
async def test_an_empty_extraction_is_retried_once():
    """The failure is not deterministic: across two runs of the same eight
    documents, three came back unusable the first time and a different one the
    second."""
    replies = iter(["sorry, no JSON here", '{"document_type": "policy"}'])
    calls = 0

    async def flaky(_card, _model):
        nonlocal calls
        calls += 1
        return next(replies)

    original = derive._ask
    derive._ask = flaky  # type: ignore[assignment]
    try:
        assert await derive.describe("a card") == {"document_type": "policy"}
        assert calls == 2
    finally:
        derive._ask = original  # type: ignore[assignment]


@pytest.mark.asyncio
async def test_it_does_not_retry_forever():
    calls = 0

    async def never(_card, _model):
        nonlocal calls
        calls += 1
        return "no json"

    original = derive._ask
    derive._ask = never  # type: ignore[assignment]
    try:
        assert await derive.describe("a card") == {}
        assert calls == 2
    finally:
        derive._ask = original  # type: ignore[assignment]
