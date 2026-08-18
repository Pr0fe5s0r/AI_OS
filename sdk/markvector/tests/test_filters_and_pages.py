"""What the SDK gained when the server got ahead of it.

Metadata filtering, the location of a citation, deleting a document, the
`manage` scope, and rate-limit cooperation all existed on the server and could
not be reached from this library. These pin the shapes callers now write
against.
"""

from __future__ import annotations

import httpx
import pytest
from markvector import (
    Citation,
    Deletion,
    Document,
    InvalidRequest,
    Markvector,
    RateLimited,
)
from markvector.client import _meta_params

BASE = "http://localhost:8000"


def client(handler) -> Markvector:
    return Markvector(api_key="test", base_url=BASE, transport=httpx.MockTransport(handler))


# ----------------------------- metadata filters -----------------------------


def test_a_where_mapping_becomes_repeated_key_value_pairs():
    assert _meta_params({"client": "acme"}) == ["client:acme"]
    assert _meta_params({"kind": ["policy", "notice"]}) == ["kind:policy", "kind:notice"]


def test_booleans_are_spelled_the_way_json_spells_them():
    """Python's str(True) is "True", which matches nothing in stored metadata
    and looks exactly like a filter that simply found no documents."""
    assert _meta_params({"active": True}) == ["active:true"]
    assert _meta_params({"active": False}) == ["active:false"]


def test_numbers_survive_the_trip():
    assert _meta_params({"year": 2026, "score": 0.5}) == ["year:2026", "score:0.5"]


def test_a_key_with_a_colon_is_refused_rather_than_misread():
    """The server splits on the first colon, so a key of "a:b" with value "c"
    would arrive as key "a" with value "b:c" — a filter that silently means
    something else."""
    with pytest.raises(ValueError, match="colon"):
        _meta_params({"a:b": "c"})


def test_an_empty_value_list_is_refused():
    """It would match nothing, which is never what somebody meant to ask for."""
    with pytest.raises(ValueError, match="no values"):
        _meta_params({"client": []})


def test_nothing_is_sent_when_no_filter_is_given():
    assert _meta_params(None) == []
    assert _meta_params({}) == []


def test_search_and_answer_put_the_filter_on_the_wire():
    seen: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params.get_list("meta"))
        if request.url.path == "/api/search":
            return httpx.Response(200, json={"results": [], "trace_id": "t"})
        return httpx.Response(200, json={"answer": "", "grounded": False, "citations": []})

    docs = client(handler).collection("c")
    docs.search("q", where={"client": "acme"})
    docs.answer("q", where={"client": "acme", "kind": ["policy", "notice"]})
    assert seen == [["client:acme"], ["client:acme", "kind:policy", "kind:notice"]]


# ------------------------ where a citation came from ------------------------


def test_a_citation_carries_its_label_and_picture():
    cited = Citation.from_json(
        {
            "marker": 1,
            "title": "Retention Plan",
            "page": 34,
            "page_label": "slide 34",
            "page_image": "/api/items/abc/pages/34",
        }
    )
    assert (cited.page, cited.page_label) == (34, "slide 34")
    assert cited.page_image == "/api/items/abc/pages/34"


def test_a_document_declares_how_it_can_be_addressed():
    deck = Document.from_json({"id": "d", "pages": 79, "page_unit": "slide", "page_image": True})
    sheet = Document.from_json({"id": "s"})
    assert (deck.pages, deck.page_unit, deck.page_image) == (79, "slide", True)
    assert (sheet.pages, sheet.page_unit, sheet.page_image) == (0, None, False)


def test_asking_for_a_picture_that_does_not_exist_says_so_before_the_request():
    """Rather than requesting a URL that cannot exist and handing back a 404
    the caller has to interpret."""
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(str(request.url))
        return httpx.Response(200, content=b"png")

    docs = client(handler).collection("c")
    textual = Citation.from_json({"marker": 1, "title": "Notes", "page": None})
    with pytest.raises(InvalidRequest, match="no picture"):
        docs.page_image(textual)
    assert sent == [], "nothing should have been requested"


def test_a_citation_with_a_picture_fetches_the_url_the_server_gave():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/items/abc/pages/34"
        return httpx.Response(200, content=b"\x89PNG")

    docs = client(handler).collection("c")
    cited = Citation.from_json({"marker": 1, "page": 34, "page_image": "/api/items/abc/pages/34"})
    assert docs.page_image(cited) == b"\x89PNG"


# --------------------------- deleting a document ---------------------------


def test_an_unconfirmed_delete_destroys_nothing_and_reports_what_would_go():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "confirm" not in request.url.params
        return httpx.Response(
            409,
            json={
                "detail": {
                    "message": "...",
                    "would_delete": {
                        "item_id": "i",
                        "title": "COMPUTER NETWORKS",
                        "versions": 2,
                        "passages": 3915,
                    },
                }
            },
        )

    plan = client(handler).collection("c").delete("i")
    assert isinstance(plan, Deletion) and not plan.deleted
    assert plan.versions == 2 and plan.passages == 3915
    assert "COMPUTER NETWORKS" in str(plan)


def test_a_confirmed_delete_reports_what_actually_went():
    """The confirmed response counts by TABLE, not by the preview's words.

    Reading only `versions`/`passages` made a real deletion announce
    "0 version(s), 0 passage(s)" — seen against the live server, and exactly
    the sort of wrong number that makes a caller doubt anything happened.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params.get("confirm") == "true"
        return httpx.Response(
            200,
            json={
                "deleted": True,
                "item_id": "i",
                "title": "Gone",
                "kb_items": 2,
                "kb_chunks": 3915,
            },
        )

    done = client(handler).collection("c").delete("i", confirm=True)
    assert done.deleted and done.title == "Gone"
    assert (done.versions, done.passages) == (2, 3915)


# -------------------------------- key scopes --------------------------------


def test_manage_is_a_scope_a_caller_can_ask_for():
    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        asked.append(_json.loads(request.read())["scopes"])
        return httpx.Response(201, json={"key": "kb_live_x", "key_id": "k", "name": "ops"})

    client(handler).create_key("ops", scopes=["manage"])
    assert asked == ["manage"]


def test_an_unknown_scope_is_refused_before_a_key_is_minted():
    """The server's parser drops what it does not recognise, so a key asked for
    "admin" would come back looking successful and able to do nothing."""
    with pytest.raises(ValueError, match="unknown scope"):
        client(lambda r: httpx.Response(200, json={})).create_key("x", scopes="admin")


# --------------------------- rate limit cooperation ---------------------------


def test_a_429_becomes_its_own_error_carrying_the_servers_timing():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={"Retry-After": "7", "RateLimit-Limit": "120"},
            json={"detail": {"message": "Rate limit exceeded for read requests."}},
        )

    mv = Markvector(
        api_key="test", base_url=BASE, max_retries=0, transport=httpx.MockTransport(handler)
    )
    with pytest.raises(RateLimited) as raised:
        mv.whoami()
    assert raised.value.retry_after == 7
    assert raised.value.limit == 120


def test_the_client_waits_the_time_the_server_asked_for(monkeypatch):
    """Not a backoff of our own invention: guessing shorter hammers a store
    that has just said it is busy, guessing longer wastes the caller's time."""
    from markvector import client as client_module

    waited: list[float] = []
    monkeypatch.setattr(client_module.time, "sleep", waited.append)

    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "3"}, json={"detail": "slow down"})
        return httpx.Response(200, json={"workspace_id": "ws"})

    mv = Markvector(
        api_key="test", base_url=BASE, max_retries=2, transport=httpx.MockTransport(handler)
    )
    assert mv.whoami()["workspace_id"] == "ws"
    assert waited == [3.0]


def test_a_wait_longer_than_the_clients_patience_is_capped(monkeypatch):
    """A server may ask for a minute. A library that holds a caller's thread
    for that long without saying so is worse than one that raises and lets them
    schedule the work."""
    from markvector import client as client_module

    waited: list[float] = []
    monkeypatch.setattr(client_module.time, "sleep", waited.append)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "600"}, json={"detail": "much later"})

    mv = Markvector(
        api_key="test", base_url=BASE, max_retries=1, transport=httpx.MockTransport(handler)
    )
    with pytest.raises(RateLimited):
        mv.whoami()
    assert waited == [client_module.MAX_RETRY_WAIT_SECONDS]
