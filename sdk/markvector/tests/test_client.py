from __future__ import annotations

import json

import httpx
import pytest
from markvector import (
    Answer,
    AuthError,
    Chunk,
    Document,
    Markvector,
    NotFound,
    Results,
)

# A tiny fake of the MarkVector API, wired in through httpx's MockTransport so
# the whole client can be exercised with no server and no network.

DOC = {
    "id": "item-1",
    "title": "Q2 note",
    "body": "Paid conversions fell 18 percent in Q2.",
    "version": 1,
    "status": "active",
    "source": {"source": "sdk", "locator": "notes/q2"},
    "classes": [],
    "metadata": {"original": {"filename": "q2.pdf", "content_type": "application/pdf", "size": 5}},
}


def handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    method = request.method

    if path == "/api/whoami":
        return httpx.Response(200, json={"workspace_id": "ws", "via": "api_key"})

    if path == "/api/collections" and method == "POST":
        body = json.loads(request.content)
        return httpx.Response(
            201,
            json={"collection_id": body.get("collection_id") or "sdk-demo", "name": body["name"]},
        )
    if path.startswith("/api/collections/") and method == "PATCH":
        return httpx.Response(200, json={"collection_id": "sdk-demo", "name": "Renamed"})
    if path.startswith("/api/collections/") and method == "DELETE":
        return httpx.Response(200, json={"deleted": True, "items_removed": 3})

    if path == "/api/items" and method == "POST":
        return httpx.Response(202, json={"job_id": "job-1", "status": "queued"})
    if path == "/api/items" and method == "GET":
        return httpx.Response(200, json={"count": 1, "items": [DOC]})
    if path == "/api/items/item-1/chunks":
        return httpx.Response(
            200,
            json={
                "item_id": "item-1",
                "count": 1,
                "chunks": [{"chunk_id": "c1", "ordinal": 0, "heading": "Q2", "text": "…"}],
            },
        )
    if path == "/api/items/item-1/original":
        return httpx.Response(
            200, content=b"%PDF-1.4", headers={"content-type": "application/pdf"}
        )
    if path == "/api/items/missing/original":
        return httpx.Response(404, json={"detail": "No original file is stored."})

    if path == "/api/search":
        assert request.headers.get("X-Collection") == "sdk-demo"
        return httpx.Response(
            200,
            json={
                "query": request.url.params.get("q"),
                "count": 1,
                "trace_id": "trace-1",
                "took_ms": 12,
                "degraded": None,
                "results": [
                    {
                        "item_id": "item-1",
                        "title": "Q2 note",
                        "excerpt": "paid [[conversions]] fell",
                        "source": {"source": "sdk", "locator": "notes/q2"},
                        "score": 0.82,
                        "semantic": 0.6,
                        "keyword": 0.2,
                        "classes": [],
                    }
                ],
            },
        )
    if path == "/api/answer":
        return httpx.Response(
            200,
            json={
                "answer": "Paid conversions fell 18 percent [1].",
                "grounded": True,
                "mode": request.url.params.get("mode"),
                "trace_id": "trace-2",
                "took_ms": 30,
                "degraded": None,
                "citations": [
                    {
                        "marker": 1,
                        "chunk_id": "c1",
                        "item_id": "item-1",
                        "title": "Q2 note",
                        "heading": "Q2",
                        "text": "…",
                        "score": 0.8,
                    }
                ],
                "results": [],
            },
        )

    if path == "/api/keys" and method == "POST":
        body = json.loads(request.content)
        return httpx.Response(
            201,
            json={
                "key_id": "k1",
                "name": body["name"],
                "key": "kb_live_secret",
                "scopes": body["scopes"].split(","),
                "collection_id": body.get("collection_id"),
            },
        )
    if path == "/api/keys" and method == "GET":
        return httpx.Response(
            200,
            json={"keys": [{"key_id": "k1", "name": "ci", "prefix": "kb_live_x", "scopes": ["read"]}]},
        )
    if path.startswith("/api/keys/") and method == "DELETE":
        return httpx.Response(200, json={"revoked": True})

    return httpx.Response(404, json={"detail": f"unhandled {method} {path}"})


def make() -> Markvector:
    return Markvector(api_key="test", transport=httpx.MockTransport(handler))


def test_missing_key_is_an_auth_error(monkeypatch):
    monkeypatch.delenv("MARKVECTOR_API_KEY", raising=False)
    monkeypatch.delenv("KB_API_KEY", raising=False)
    with pytest.raises(AuthError):
        Markvector()


def test_whoami_and_key_from_env(monkeypatch):
    monkeypatch.setenv("MARKVECTOR_API_KEY", "from-env")
    mv = Markvector(transport=httpx.MockTransport(handler))
    assert mv.whoami()["workspace_id"] == "ws"


def test_write_then_wait_returns_the_document():
    with make() as mv:
        docs = mv.collection("sdk-demo")
        result = docs.add("Paid conversions fell 18 percent.", locator="notes/q2", wait=True)
        assert result.job_id == "job-1"
        assert result.indexed
        assert isinstance(result.document, Document)
        assert result.document.source.locator == "notes/q2"


def test_search_parses_and_iterates():
    docs = make().collection("sdk-demo")
    results = docs.search("why did paid results fall")
    assert isinstance(results, Results)
    assert results.trace_id == "trace-1"
    assert len(results) == 1
    hit = results[0]
    assert hit.id == "item-1"
    assert hit.matched_on == "meaning+wording"
    assert hit.clean_excerpt == "paid conversions fell"


def test_answer_is_grounded_and_cited():
    docs = make().collection("sdk-demo")
    ans = docs.answer("what happened in Q2?", mode="hybrid")
    assert isinstance(ans, Answer)
    assert ans and ans.grounded
    assert ans.mode == "hybrid"
    assert str(ans).startswith("Paid conversions")
    assert ans.citations[0].marker == 1


def test_chunks():
    chunks = make().collection("sdk-demo").chunks("item-1")
    assert len(chunks) == 1
    assert isinstance(chunks[0], Chunk)
    assert chunks[0].heading == "Q2"


def test_search_within_selected_files():
    seen: dict[str, list[str]] = {}

    def h(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/search":
            seen["item_ids"] = request.url.params.get_list("item_ids")
            return httpx.Response(
                200, json={"query": "q", "results": [], "trace_id": "t", "took_ms": 1}
            )
        return httpx.Response(404, json={"detail": "x"})

    mv = Markvector(api_key="test", transport=httpx.MockTransport(h))
    docs = mv.collection("sdk-demo")
    # Accepts both raw ids and Document objects.
    doc = Document.from_json(DOC)
    docs.search("why did paid results fall", files=["item-1", doc])
    assert seen["item_ids"] == ["item-1", "item-1"]


def test_files_returns_only_uploads():
    text_doc = {**DOC, "id": "item-2", "metadata": {}}  # no original -> not a file

    def h(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/items":
            return httpx.Response(200, json={"count": 2, "items": [DOC, text_doc]})
        return httpx.Response(404, json={"detail": "x"})

    docs = Markvector(api_key="test", transport=httpx.MockTransport(h)).collection("sdk-demo")
    assert len(docs.list()) == 2
    files = docs.files()
    assert [d.id for d in files] == ["item-1"]
    assert files[0].original is not None


def test_download_original_bytes_and_to_file(tmp_path):
    docs = make().collection("sdk-demo")
    data = docs.download_original("item-1")
    assert data == b"%PDF-1.4"

    out = docs.download_original("item-1", path=tmp_path / "copy.pdf")
    assert out.read_bytes() == b"%PDF-1.4"


def test_download_missing_original_raises_not_found():
    with pytest.raises(NotFound):
        make().collection("sdk-demo").download_original("missing")


def test_document_original_metadata():
    doc = make().collection("sdk-demo").list()[0]
    assert doc.original is not None
    assert doc.original.filename == "q2.pdf"
    assert doc.original.content_type == "application/pdf"


def test_keys_lifecycle():
    mv = make()
    minted = mv.create_key("acme-bot", scopes="read,write", collection_id="acme")
    assert minted.key == "kb_live_secret"
    assert minted.collection_id == "acme"
    assert [k.name for k in mv.keys()] == ["ci"]
    mv.revoke_key(minted.key_id)  # no raise


def test_collection_admin():
    mv = make()
    info = mv.create_collection("SDK demo", collection_id="sdk-demo")
    assert info.collection_id == "sdk-demo"
    assert mv.rename_collection("sdk-demo", "Renamed").name == "Renamed"
    assert mv.delete_collection("sdk-demo") == 3
