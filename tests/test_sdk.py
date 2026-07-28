from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest

# The SDK is a separate distribution, so the suite imports it from the tree
# rather than requiring it to be installed to run the tests.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sdk" / "python"))

from knowledgebase import (  # noqa: E402
    AuthError,
    IndexingTimeout,
    KnowledgeBase,
    NotFound,
    Unavailable,
)

# Driven against a stub transport rather than a live server: these pin the
# client's own behaviour — how it authenticates, what it retries, how it
# reports failure — which a running stack would obscure rather than prove.


def client(handler, **kw) -> KnowledgeBase:
    return KnowledgeBase(
        api_key="kb_live_test",
        base_url="http://kb.test",
        transport=httpx.MockTransport(handler),
        **kw,
    )


def json_response(payload, status=200) -> httpx.Response:
    return httpx.Response(status, json=payload)


def test_the_key_is_sent_as_a_bearer_token():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return json_response({"workspace_id": "ws-1"})

    client(handler).whoami()
    assert seen["auth"] == "Bearer kb_live_test"


def test_a_missing_key_fails_before_any_request(monkeypatch):
    """Told at construction, not on the first call — a failure three layers
    into someone's script is far harder to place."""
    monkeypatch.delenv("KB_API_KEY", raising=False)
    with pytest.raises(AuthError, match="No API key"):
        KnowledgeBase(base_url="http://kb.test")


def test_the_key_can_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("KB_API_KEY", "kb_live_from_env")
    kb = KnowledgeBase(base_url="http://kb.test", transport=httpx.MockTransport(
        lambda r: json_response({"workspace_id": "ws"})
    ))
    assert kb.whoami() == {"workspace_id": "ws"}


def test_the_collection_travels_as_a_header():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["collection"] = request.headers.get("x-collection")
        return json_response({"items": []})

    client(handler).collection("research").list()
    assert seen["collection"] == "research"


def test_writing_sends_the_locator_that_decides_identity():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return json_response({"job_id": "j1", "status": "queued"}, 202)

    result = client(handler).collection("c").add("body text", locator="notes/1", title="T")
    assert seen["locator"] == "notes/1"
    assert seen["body"] == "body text"
    assert result.job_id == "j1"
    assert result.indexed is False  # not waited on


def test_wait_polls_until_the_document_is_really_there():
    """Writing and filing are separate jobs, so the client waits for the
    document to exist rather than sleeping for a guessed interval."""
    calls = {"list": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/items" and request.method == "POST":
            return json_response({"job_id": "j", "status": "queued"}, 202)
        calls["list"] += 1
        if calls["list"] < 3:
            return json_response({"items": []})  # not indexed yet
        return json_response(
            {"items": [{"id": "i1", "title": "T", "body": "b",
                        "source": {"source": "sdk", "locator": "notes/1"}}]}
        )

    result = client(handler).collection("c").add("x", locator="notes/1", wait=True, timeout=10)
    assert result.indexed is True
    assert result.document.id == "i1"
    assert calls["list"] >= 3


def test_wait_gives_up_loudly_rather_than_returning_nothing():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return json_response({"job_id": "j"}, 202)
        return json_response({"items": []})  # never arrives

    with pytest.raises(IndexingTimeout, match="not searchable"):
        client(handler).collection("c").add("x", locator="ghost", wait=True, timeout=1.2)


def test_results_iterate_and_carry_their_trace():
    payload = {
        "query": "q", "trace_id": "t-1", "took_ms": 42, "degraded": None,
        "results": [
            {"item_id": "a", "title": "A", "excerpt": "an [[exact]] hit",
             "source": {"source": "sdk", "locator": "l"}, "score": 0.9,
             "semantic": 0.8, "keyword": 0.2},
        ],
    }
    results = client(lambda r: json_response(payload)).collection("c").search("q")

    assert len(results) == 1 and bool(results)
    assert [m.id for m in results] == ["a"]
    assert results.trace_id == "t-1" and results.took_ms == 42
    assert results[0].matched_on == "meaning+wording"
    # Highlight markers are the server's, and callers should not have to know.
    assert results[0].clean_excerpt == "an exact hit"


def test_how_a_result_was_found_is_reported_honestly():
    def one(semantic, keyword):
        payload = {"query": "q", "results": [
            {"item_id": "a", "title": "A", "excerpt": "",
             "source": {"source": "s", "locator": "l"},
             "score": 0.5, "semantic": semantic, "keyword": keyword}]}
        return client(lambda r: json_response(payload)).collection("c").search("q")[0]

    assert one(0.8, 0.0).matched_on == "meaning"
    assert one(0.0, 0.5).matched_on == "wording"
    assert one(0.8, 0.5).matched_on == "meaning+wording"


def test_a_degraded_answer_is_visible_on_the_result():
    payload = {"query": "q", "results": [], "degraded": "semantic arm unavailable"}
    results = client(lambda r: json_response(payload)).collection("c").search("q")
    assert results.degraded == "semantic arm unavailable"


@pytest.mark.parametrize(
    ("status", "expected"),
    [(401, AuthError), (403, AuthError), (404, NotFound), (500, Unavailable)],
)
def test_server_errors_become_typed_exceptions(status, expected):
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response({"detail": "nope"}, status)

    with pytest.raises(expected):
        client(handler, max_retries=0).collection("c").list()


def test_reads_are_retried_but_writes_are_not():
    """A timeout on a write may mean the write landed. Repeating it would be
    worse than reporting it."""
    attempts = {"get": 0, "post": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            attempts["get"] += 1
            if attempts["get"] < 2:
                return json_response({"detail": "busy"}, 503)
            return json_response({"items": []})
        attempts["post"] += 1
        raise httpx.ConnectError("network gone")

    kb = client(handler, max_retries=2)
    kb.collection("c").list()
    assert attempts["get"] == 2  # retried once, then succeeded

    with pytest.raises(Unavailable):
        kb.collection("c").add("x", locator="l")
    assert attempts["post"] == 1  # never repeated


def test_collections_are_flattened_across_clusters():
    payload = {"clusters": [
        {"cluster_id": "default", "collections": [
            {"collection_id": "a", "name": "A", "items": 3, "dimensions": 1536}]},
        {"cluster_id": "prod", "collections": [
            {"collection_id": "b", "name": "B", "items": 7, "dimensions": 1536}]},
    ]}
    found = client(lambda r: json_response(payload)).collections()
    assert [(c.collection_id, c.cluster_id, c.items) for c in found] == [
        ("a", "default", 3),
        ("b", "prod", 7),
    ]


def test_a_missing_file_is_refused_before_the_request():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("should not have reached the network")

    with pytest.raises(Exception, match="No such file"):
        client(handler).collection("c").add_file("definitely-not-here.pdf")
