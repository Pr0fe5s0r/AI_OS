from __future__ import annotations

from typing import Any

import pytest
from fastapi import Header
from httpx import ASGITransport, AsyncClient

from tests.conftest import WORKSPACE

pytestmark = pytest.mark.needs_db

# The API layer had no tests, and two faults reached the running system through
# it: an upload accepted a format nothing could read, and a route imported a
# module that had been deleted so sign-up returned a 500. Both were a single
# request away from being caught.
#
# These exercise the routes themselves — status codes, refusals, and the shape
# of what comes back — without a browser and without the worker. The engine has
# its own tests; this is about the contract callers actually see.


@pytest.fixture
async def client(monkeypatch):
    """The app, with the queue stubbed and identity forced to a test workspace.

    The queue is replaced because these tests are about the HTTP contract, not
    about indexing: a route's job is to accept or refuse and hand off, and
    whether it did that is visible without running a worker.
    """
    from apps.api import main as api

    class _Queue:
        def __init__(self) -> None:
            self.jobs: list[tuple] = []

        async def enqueue_job(self, name, *args):
            self.jobs.append((name, args))
            return type("Job", (), {"job_id": f"job-{len(self.jobs)}"})()

        async def close(self):  # pragma: no cover - lifespan teardown
            pass

    queue = _Queue()

    async def fake_caller(x_markvector_client: str | None = Header(default=None)):
        return {
            "company_id": WORKSPACE,
            "via": "test",
            "client": x_markvector_client,
            "scopes": ["read", "write"],
        }

    from packages.core.tenancy import resolve_caller, workspace_scope
    from packages.shared.schema import Scope

    async def fake_scope():
        return Scope(workspace_id=WORKSPACE, collection_id=None)

    api.app.dependency_overrides[resolve_caller] = fake_caller
    api.app.dependency_overrides[workspace_scope] = fake_scope
    api.app.state.queue = queue

    transport = ASGITransport(app=api.app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        http.queue = queue  # type: ignore[attr-defined]
        yield http

    api.app.dependency_overrides.clear()


async def test_the_supported_formats_are_advertised(client):
    response = await client.get("/api/formats")
    assert response.status_code == 200
    formats = response.json()["supported"]
    assert ".pdf" in formats and ".docx" in formats and ".md" in formats


async def test_an_unreadable_format_is_refused_while_the_caller_is_listening(client):
    """The original fault: the upload was accepted, indexing failed in a job
    whose reason nobody reads, and the console could only say the document was
    "not searchable yet" — which sounds like a delay."""
    response = await client.post(
        "/api/items/file", files={"file": ("meeting.mp3", b"not a document", "application/octet-stream")}
    )
    assert response.status_code == 415
    detail = response.json()["detail"]
    assert "meeting.mp3" in detail
    assert ".pdf" in detail, "the refusal must say what IS accepted"
    assert client.queue.jobs == [], "nothing may be queued for a file we cannot read"


async def test_an_empty_file_is_refused(client):
    response = await client.post("/api/items/file", files={"file": ("empty.md", b"", "text/markdown")})
    assert response.status_code == 400
    assert client.queue.jobs == []


async def test_a_readable_upload_is_accepted_and_queued(client):
    response = await client.post(
        "/api/items/file", files={"file": ("notes.md", b"# Notes\n\nSomething.", "text/markdown")}
    )
    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    assert [name for name, _ in client.queue.jobs] == ["ingest_file"]


async def test_writing_text_is_accepted_and_queued(client):
    response = await client.post(
        "/api/items",
        json={"source": "console", "locator": "note/1", "body": "Some content."},
    )
    assert response.status_code == 202
    assert [name for name, _ in client.queue.jobs] == ["ingest_text"]


async def test_an_empty_body_is_rejected_by_the_contract(client):
    response = await client.post(
        "/api/items", json={"source": "console", "locator": "note/2", "body": ""}
    )
    assert response.status_code == 422
    assert client.queue.jobs == []


async def test_search_returns_the_documented_shape(client):
    response = await client.get("/api/search", params={"q": "anything at all"})
    assert response.status_code == 200
    payload = response.json()
    # The trace id comes back with the answer so any result can be taken
    # straight to the explanation of why it was returned.
    for key in ("query", "count", "trace_id", "results", "took_ms"):
        assert key in payload, f"missing {key}"
    assert isinstance(payload["results"], list)


async def test_search_requires_a_query(client):
    assert (await client.get("/api/search", params={"q": ""})).status_code == 422


async def test_sdk_search_is_identified_and_its_trace_can_be_opened(client):
    response = await client.get(
        "/api/search",
        params={"q": "sdk trace provenance"},
        headers={"X-Markvector-Client": "javascript/0.2.0"},
    )
    assert response.status_code == 200

    detail = await client.get(f"/api/traces/{response.json()['trace_id']}")
    assert detail.status_code == 200
    assert detail.json()["via"] == "sdk:javascript/0.2.0"


async def test_the_answer_route_defaults_to_agentic(client, monkeypatch):
    """The default is part of the contract callers depend on, so it is checked
    at the boundary rather than only in the engine."""
    seen: dict[str, Any] = {}

    # **extra rather than a fixed parameter list. A double that mirrors every
    # argument of the real function has to be edited each time the real one
    # gains a keyword, and it fails with a TypeError from inside the route —
    # which reads like a broken endpoint, not a stale test. This one records
    # what it was given and stays out of the way.
    async def fake_answer(session, scope, question, cfg=None, mode="agentic", **extra):
        from packages.core.answer import Answer
        from packages.core.search import Trace, new_trace_id

        seen["mode"] = mode
        seen.update(extra)
        trace = Trace(trace_id=new_trace_id(), query=question, config={}, filters={})
        return Answer(question=question, text="", mode=mode, grounded=False), trace

    monkeypatch.setattr("apps.api.main.answer", fake_answer)
    response = await client.get("/api/answer", params={"q": "anything"})
    assert response.status_code == 200
    assert seen["mode"] == "agentic"
    assert response.json()["mode"] == "agentic"
    # No document filter unless one was asked for: an empty tuple means "the
    # store decides", and anything else here would silently scope every
    # unfiltered question.
    assert seen.get("item_ids") == ()


async def test_the_answer_route_passes_a_document_filter_through(client, monkeypatch):
    """?doc= is the explicit filter. It has to reach the engine as given —
    a filter that is accepted at the boundary and dropped on the way in is
    worse than one that was never offered."""
    seen: dict[str, Any] = {}

    async def fake_answer(session, scope, question, cfg=None, mode="agentic", **extra):
        from packages.core.answer import Answer
        from packages.core.search import Trace, new_trace_id

        seen.update(extra)
        trace = Trace(trace_id=new_trace_id(), query=question, config={}, filters={})
        return Answer(question=question, text="", mode=mode, grounded=False), trace

    monkeypatch.setattr("apps.api.main.answer", fake_answer)
    response = await client.get(
        "/api/answer", params={"q": "anything", "doc": ["item-a", "item-b"]}
    )
    assert response.status_code == 200
    assert seen.get("item_ids") == ("item-a", "item-b")


async def test_an_unknown_retrieval_mode_is_refused(client):
    response = await client.get("/api/answer", params={"q": "x", "mode": "telepathy"})
    assert response.status_code == 422


async def test_a_missing_passage_is_a_404_not_an_empty_body(client):
    response = await client.get("/api/chunks/does-not-exist")
    assert response.status_code == 404


async def test_an_unknown_route_is_a_404(client):
    assert (await client.get("/api/not-a-route")).status_code == 404
