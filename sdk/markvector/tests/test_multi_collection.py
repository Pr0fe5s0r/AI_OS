"""Reading several collections at once, and handing back what they gave.

These collections sit inside ONE workspace and belong to one customer, so
there is no cross-tenant boundary here for data to cross. The rule this file
pins is therefore the opposite of a sanitiser's: what comes out must be what
the store returned — same excerpts, same citations, same page images — with
nothing redacted, generalised or merged into a new voice.
"""

from __future__ import annotations

import httpx
import pytest
from markvector import Markvector
from markvector.errors import InvalidRequest

BASE = "http://localhost:8000"

BODIES = {
    "acme": ("Acme onboarding", "Acme issues credentials on day one.", "acme/onboard.pdf"),
    "globex": ("Globex onboarding", "Globex issues credentials in week two.", "globex/onboard.pdf"),
}

PNG = bytes([0x89]) + b"PNG\r\n"


def store(handler) -> Markvector:
    return Markvector(api_key="test", base_url=BASE, transport=httpx.MockTransport(handler))


def _search_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/api/clusters":
        # collections() reads the cluster tree, not a flat list.
        return httpx.Response(
            200,
            json={
                "clusters": [
                    {
                        "cluster_id": "default",
                        "collections": [{"collection_id": c} for c in BODIES],
                    }
                ]
            },
        )
    name = request.headers.get("x-collection", "acme")
    title, text, locator = BODIES[name]
    return httpx.Response(
        200,
        json={
            "results": [
                {
                    "item_id": "item-" + name,
                    "title": title,
                    "excerpt": text,
                    "score": 0.9 if name == "acme" else 0.8,
                    "source": {"source": "seed", "locator": locator},
                    "passages": [{"chunk_id": "c-" + name, "text": text}],
                }
            ],
            "trace_id": "t",
        },
    )


def test_it_reads_every_collection_it_was_given():
    across = store(_search_handler).multi_collection(["acme", "globex"])
    hits = across.search("onboarding")
    assert {m.collection for m in hits.matches} == {"acme", "globex"}


def test_the_excerpts_come_back_verbatim():
    """The whole point. A sanitiser lived here once and this is what replaced
    it: the words the store holds are the words the caller gets."""
    across = store(_search_handler).multi_collection(["acme", "globex"])
    text = " ".join(m.excerpt for m in across.search("onboarding").matches)
    assert "Acme issues credentials on day one." in text
    assert "Globex issues credentials in week two." in text


def test_titles_and_locators_survive_too():
    across = store(_search_handler).multi_collection(["acme"])
    match = across.search("onboarding").matches[0]
    assert match.title == "Acme onboarding"
    assert match.source.locator == "acme/onboard.pdf"


def test_results_are_ordered_by_score_across_collections():
    across = store(_search_handler).multi_collection(["globex", "acme"])
    ordered = [m.collection for m in across.search("onboarding").matches]
    assert ordered == ["acme", "globex"], "0.9 outranks 0.8 whichever order they were read in"


def test_naming_nothing_reads_everything_the_key_reaches():
    across = store(_search_handler).multi_collection()
    assert sorted(across.collections) == ["acme", "globex"]


def test_a_collection_that_fails_does_not_end_the_read():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("x-collection") == "globex":
            return httpx.Response(500, json={"detail": "boom"})
        return _search_handler(request)

    hits = store(handler).multi_collection(["acme", "globex"]).search("onboarding")
    assert [m.collection for m in hits.matches] == ["acme"]


# ------------------------------- answers -------------------------------


def _answer_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/api/clusters":
        # collections() reads the cluster tree, not a flat list.
        return httpx.Response(
            200,
            json={
                "clusters": [
                    {
                        "cluster_id": "default",
                        "collections": [{"collection_id": c} for c in BODIES],
                    }
                ]
            },
        )
    name = request.headers.get("x-collection", "acme")
    title, text, _ = BODIES[name]
    return httpx.Response(
        200,
        json={
            "answer": text,
            "grounded": True,
            "citations": [
                {
                    "marker": 1,
                    "chunk_id": "c-" + name,
                    "item_id": "item-" + name,
                    "title": title,
                    "heading": "Onboarding",
                    "text": text,
                    "page": 3,
                    "page_label": "page 3",
                    "page_image": "/api/items/item-" + name + "/pages/3",
                }
            ],
            "results": [],
            "trace_id": "t",
        },
    )


def test_each_collection_answers_for_itself():
    """Not merged into one voice. Merging would rewrite several grounded
    answers into a new one no single collection supports, and the citations
    would stop pointing at what produced them."""
    answers = store(_answer_handler).multi_collection(["acme", "globex"]).answer("how?")
    assert [a.text for a in answers] == [
        "Acme issues credentials on day one.",
        "Globex issues credentials in week two.",
    ]


def test_citations_keep_their_page_and_image():
    answers = store(_answer_handler).multi_collection(["acme"]).answer("how?")
    cite = answers[0].citations[0]
    assert cite.page_label == "page 3"
    assert cite.page_image == "/api/items/item-acme/pages/3"
    assert cite.text == "Acme issues credentials on day one."


def test_a_citation_knows_which_collection_to_fetch_its_image_from():
    answers = store(_answer_handler).multi_collection(["acme", "globex"]).answer("how?")
    assert {c.collection for a in answers for c in a.citations} == {"acme", "globex"}


def test_a_page_image_is_fetched_from_the_right_collection():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "/pages/" in request.url.path:
            seen.append(request.headers.get("x-collection", ""))
            return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})
        return _answer_handler(request)

    across = store(handler).multi_collection(["acme", "globex"])
    citation = [c for a in across.answer("how?") for c in a.citations if c.collection == "globex"][0]
    assert across.page_image(citation).startswith(PNG[:4])
    assert seen == ["globex"]


def test_a_citation_with_no_collection_is_refused_rather_than_guessed():
    from markvector.models import Citation

    across = store(_answer_handler).multi_collection(["acme"])
    bare = Citation(marker=1, chunk_id="c", item_id="i", title="t", heading="", text="x")
    with pytest.raises(InvalidRequest):
        across.page_image(bare)


# ---------------------------- nothing sanitises ----------------------------


def test_the_sdk_no_longer_carries_a_sanitiser():
    """patterns.py generalised across tenants and redacted what it returned.
    These collections belong to one customer, so that machinery was removed
    rather than left switched off — a filter nobody wants is one that
    surprises somebody later."""
    import markvector

    assert not hasattr(markvector, "Pattern")
    assert not hasattr(markvector, "PatternReport")
    assert not hasattr(markvector.Markvector, "patterns")
    with pytest.raises(ImportError):
        __import__("markvector.patterns")


def test_single_collection_reads_are_unchanged():
    """A single collection carries no provenance, because the caller already
    knows which one they asked."""
    hits = store(_search_handler).collection("acme").search("onboarding")
    assert hits.matches[0].collection == ""
    assert hits.matches[0].excerpt == "Acme issues credentials on day one."
