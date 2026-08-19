"""Agentic retrieval, with vision, across several collections.

A multi-collection read is not limited to ranked search: the same agent loop a
single collection gets — read the catalogue, search, open a section, hop the
graph — runs over all of them at once, and can open a page as a PICTURE when
the text layer cannot answer.

What is pinned here is that the fan-out is real (every collection is reached),
that a document opened by id is fetched from the collection that actually
holds it, and that vision is a choice the caller can make rather than a
default nobody can see.
"""

from __future__ import annotations

import httpx
from markvector import Markvector

BASE = "http://localhost:8000"
NAMES = ["acme", "globex"]


def store(handler) -> Markvector:
    return Markvector(api_key="test", base_url=BASE, transport=httpx.MockTransport(handler))


def _clusters() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "clusters": [
                {"cluster_id": "default", "collections": [{"collection_id": c} for c in NAMES]}
            ]
        },
    )


# ------------------------------- vision -------------------------------


def test_vision_is_sent_and_defaults_on():
    """The route has taken a `vision` flag all along and the SDK never sent
    it, so a caller could neither turn it off nor tell it was on."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params.get("vision", "<absent>"))
        return httpx.Response(200, json={"answer": "a", "grounded": True, "citations": [], "results": []})

    docs = store(handler).collection("acme")
    docs.answer("q")
    docs.answer("q", vision=False)
    assert seen == ["true", "false"]


def test_vision_reaches_every_collection_of_a_multi_read():
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/clusters":
            return _clusters()
        seen.append(
            (request.headers.get("x-collection", ""), request.url.params.get("vision", ""))
        )
        return httpx.Response(200, json={"answer": "a", "grounded": True, "citations": [], "results": []})

    store(handler).multi_collection(NAMES).answer("q", vision=False)
    assert seen == [("acme", "false"), ("globex", "false")]


def test_agentic_is_the_default_mode_across_collections():
    modes: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/clusters":
            return _clusters()
        modes.append(request.url.params.get("mode", ""))
        return httpx.Response(200, json={"answer": "a", "grounded": True, "citations": [], "results": []})

    store(handler).multi_collection(NAMES).answer("q")
    assert modes == ["agentic", "agentic"], "a multi read must not quietly downgrade to hybrid"


# --------------------------- the agent's own tools ---------------------------


def _doc(item_id: str, title: str) -> dict:
    return {
        "id": item_id,
        "title": title,
        "body": "body of " + title,
        "source": {"source": "seed", "locator": item_id + ".md"},
        "version": 1,
        "status": "active",
        "hash": "h",
        "created_at": "2026-01-01T00:00:00Z",
    }


def _tools_handler(seen: list[str]):
    def handler(request: httpx.Request) -> httpx.Response:
        path, name = request.url.path, request.headers.get("x-collection", "")
        if path == "/api/clusters":
            return _clusters()
        if path == "/api/items":
            seen.append("list:" + name)
            return httpx.Response(200, json={"items": [_doc("doc-" + name, "Doc " + name)]})
        if path.startswith("/api/items/"):
            item_id = path.split("/api/items/")[1].split("/")[0]
            # Each document exists in exactly ONE collection.
            if not item_id.endswith(name):
                return httpx.Response(404, json={"detail": "No such item."})
            seen.append("get:" + name)
            return httpx.Response(200, json=_doc(item_id, "Doc " + name))
        return httpx.Response(200, json={"results": [], "trace_id": "t"})

    return handler


def test_list_reaches_every_collection():
    seen: list[str] = []
    docs = store(_tools_handler(seen)).multi_collection(NAMES).list()
    assert sorted(d.id for d in docs) == ["doc-acme", "doc-globex"]
    assert seen == ["list:acme", "list:globex"]


def test_a_document_is_opened_from_the_collection_that_holds_it():
    """The agent hands back an id and nothing else, so the id has to be enough
    to find its way home."""
    seen: list[str] = []
    across = store(_tools_handler(seen)).multi_collection(NAMES)
    assert across.get("doc-globex").title == "Doc globex"
    assert "get:globex" in seen


def test_listing_first_saves_hunting_for_it_later():
    """Once a document has been seen in a collection, opening it does not try
    the others — the same read twice should not cost more the second time."""
    seen: list[str] = []
    across = store(_tools_handler(seen)).multi_collection(NAMES)
    across.list()
    seen.clear()
    across.get("doc-globex")
    assert seen == ["get:globex"], seen


def test_a_graph_hop_stays_in_the_collection_it_started_from():
    """A hop must not become a way of reaching a collection the search never
    returned."""
    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/clusters":
            return _clusters()
        if "/neighbors" in request.url.path:
            asked.append(request.headers.get("x-collection", ""))
            return httpx.Response(200, json={"neighbors": []})
        return httpx.Response(200, json={"results": [], "trace_id": "t"})

    from markvector.models import Match, Source

    across = store(handler).multi_collection(NAMES)
    hit = Match(
        id="i", title="t", excerpt="e", source=Source(source="s", locator="l"),
        score=1.0, chunk_id="c-globex", collection="globex",
    )
    across.neighbors(hit)
    assert asked == ["globex"]


def test_an_agent_can_be_built_over_several_collections():
    across = store(_tools_handler([])).multi_collection(NAMES)
    bot = across.agent(api_key="k", base_url=BASE, model="m")
    # It was handed the multi-collection reader, not one collection.
    assert bot._c is across


def test_a_collection_that_fails_is_reported_not_silently_empty():
    """Measured: an agentic run whose every call timed out on the client's
    30-second default reported "0 answered", which reads as "the store has
    nothing" rather than "nobody was asked properly"."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/clusters":
            return _clusters()
        if request.headers.get("x-collection") == "globex":
            return httpx.Response(500, json={"detail": "boom"})
        return httpx.Response(200, json={"answer": "a", "grounded": True, "citations": [], "results": []})

    across = store(handler).multi_collection(NAMES)
    answers = across.answer("q")
    assert [a.collection for a in answers] == ["acme"]
    assert set(across.failures) == {"globex"}
    assert "boom" in across.failures["globex"]


def test_failures_do_not_leak_from_one_read_into_the_next():
    """A stale failure would report a collection as broken long after it
    recovered."""
    broken = {"yes": True}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/clusters":
            return _clusters()
        if broken["yes"]:
            return httpx.Response(500, json={"detail": "boom"})
        return httpx.Response(200, json={"answer": "a", "grounded": True, "citations": [], "results": []})

    across = store(handler).multi_collection(NAMES)
    across.answer("first")
    assert set(across.failures) == set(NAMES)

    broken["yes"] = False
    across.answer("second")
    assert across.failures == {}
