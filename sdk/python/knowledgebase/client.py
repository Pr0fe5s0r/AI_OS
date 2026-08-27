from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import httpx

from .errors import (
    AuthError,
    IndexingTimeout,
    InvalidRequest,
    KnowledgeBaseError,
    NotFound,
    Unavailable,
)
from .models import CollectionInfo, Document, Results, WriteResult

DEFAULT_URL = "http://tnega-api-o9ecgm-5fbf26-217-154-175-169.traefik.me"
_RETRYABLE = {429, 500, 502, 503, 504}


class KnowledgeBase:
    """A client for one workspace.

        kb = KnowledgeBase(api_key="kb_live_…")
        docs = kb.collection("client-research")
        docs.add("Q2 paid conversions fell 18 percent.", locator="notes/q2")
        for match in docs.search("why did paid results fall"):
            print(match.title, match.score)

    The key identifies the workspace, so nothing here takes a workspace id.
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 30.0,
        max_retries: int = 2,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        key = api_key or os.getenv("KB_API_KEY")
        if not key:
            raise AuthError(
                "No API key. Pass api_key= or set KB_API_KEY. "
                "Create one in the console under Keys."
            )
        self._key = key
        self.base_url = (base_url or os.getenv("KB_URL") or DEFAULT_URL).rstrip("/")
        self._max_retries = max_retries
        self._http = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            # `transport` exists so the client can be exercised without a
            # server. Without it, testing this library would mean either
            # standing up the whole stack or patching httpx internals.
            transport=transport,
            headers={
                "Authorization": f"Bearer {key}",
                "User-Agent": "knowledgebase-python/0.1",
            },
        )

    # ------------------------------ plumbing ------------------------------

    def _request(self, method: str, path: str, **kw: Any) -> Any:
        """One place for retries and error translation.

        Retries only idempotent failures — a timeout on a write could mean the
        write landed, and repeating it would be worse than reporting it.
        """
        last: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                response = self._http.request(method, path, **kw)
            except httpx.RequestError as exc:
                last = Unavailable(f"Could not reach {self.base_url}: {exc}")
                if method != "GET":
                    raise last from exc
            else:
                if response.status_code in _RETRYABLE and attempt < self._max_retries:
                    time.sleep(0.4 * (2**attempt))
                    continue
                return self._unwrap(response)
            time.sleep(0.4 * (2**attempt))
        raise last or Unavailable("Request failed.")

    @staticmethod
    def _unwrap(response: httpx.Response) -> Any:
        if response.is_success:
            return response.json() if response.content else None

        detail = response.text[:300]
        try:
            detail = response.json().get("detail", detail)
        except Exception:
            pass

        if response.status_code in (401, 403):
            raise AuthError(detail)
        if response.status_code == 404:
            raise NotFound(detail)
        if 400 <= response.status_code < 500:
            raise InvalidRequest(detail)
        raise Unavailable(f"{response.status_code}: {detail}")

    # ------------------------------ workspace ------------------------------

    def whoami(self) -> dict[str, Any]:
        """Which workspace this key belongs to, and what it may do."""
        return self._request("GET", "/api/whoami")

    def collections(self) -> list[CollectionInfo]:
        """Every collection in the workspace, across all clusters."""
        tree = self._request("GET", "/api/clusters")
        return [
            CollectionInfo.from_json({**c, "cluster_id": cluster["cluster_id"]})
            for cluster in tree.get("clusters", [])
            for c in cluster.get("collections", [])
        ]

    def create_collection(
        self, name: str, collection_id: str | None = None, cluster_id: str | None = None
    ) -> CollectionInfo:
        created = self._request(
            "POST",
            "/api/collections",
            json={"name": name, "collection_id": collection_id, "cluster_id": cluster_id},
        )
        return CollectionInfo.from_json(created)

    def collection(self, collection_id: str) -> Collection:
        """A handle. Cheap — it does not call the server."""
        return Collection(self, collection_id)

    def trace(self, trace_id: str) -> dict[str, Any]:
        """Why a search returned what it did: every candidate, score and timing."""
        return self._request("GET", f"/api/traces/{trace_id}")

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> KnowledgeBase:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


class Collection:
    """Read and write one collection."""

    def __init__(self, kb: KnowledgeBase, collection_id: str) -> None:
        self._kb = kb
        self.id = collection_id

    def __repr__(self) -> str:
        return f"<Collection {self.id!r}>"

    @property
    def _headers(self) -> dict[str, str]:
        return {"X-Collection": self.id}

    # -------------------------------- write --------------------------------

    def add(
        self,
        text: str,
        locator: str,
        title: str | None = None,
        source: str = "sdk",
        url: str | None = None,
        period_start: str | None = None,
        period_end: str | None = None,
        metadata: dict[str, Any] | None = None,
        wait: bool = False,
        timeout: float = 60.0,
    ) -> WriteResult:
        """Store text.

        `locator` is the document's stable id at its origin. Writing the same
        locator again updates that document rather than creating a second copy,
        so re-running an import is safe.

        Indexing is asynchronous. Pass `wait=True` when the next line of your
        code needs to search for what you just wrote.
        """
        payload = {
            "source": source,
            "locator": locator,
            "body": text,
            "title": title,
            "url": url,
            "period_start": period_start,
            "period_end": period_end,
            "metadata": metadata or {},
        }
        accepted = self._kb._request(
            "POST", "/api/items", json=payload, headers=self._headers
        )
        result = WriteResult(job_id=accepted.get("job_id"), status=accepted.get("status", "queued"))
        if wait:
            result.document = self._await_indexing(locator, timeout)
        return result

    def add_file(
        self,
        path: str | Path,
        locator: str | None = None,
        source: str = "upload",
        wait: bool = False,
        timeout: float = 120.0,
    ) -> WriteResult:
        """Store a document from disk. PDF, Markdown and text today.

        The file is read, converted to Markdown and indexed; the original is
        never stored, so keep your copy.
        """
        p = Path(path)
        if not p.is_file():
            raise InvalidRequest(f"No such file: {p}")
        key = locator or p.name

        with p.open("rb") as fh:
            accepted = self._kb._request(
                "POST",
                "/api/items/file",
                files={"file": (p.name, fh)},
                data={"source": source, "locator": key},
                headers=self._headers,
            )
        result = WriteResult(job_id=accepted.get("job_id"), status=accepted.get("status", "queued"))
        if wait:
            result.document = self._await_indexing(key, timeout)
        return result

    def _await_indexing(self, locator: str, timeout: float) -> Document:
        """Poll until the document is really searchable.

        Writing and filing are separate jobs, so this waits for the document to
        exist rather than sleeping for a guessed interval — the guess is what
        makes a test flaky on a slow machine.
        """
        deadline = time.monotonic() + timeout
        delay = 0.4
        while time.monotonic() < deadline:
            for doc in self.list(limit=200):
                if doc.source.locator == locator:
                    return doc
            time.sleep(delay)
            delay = min(delay * 1.5, 3.0)
        raise IndexingTimeout(
            f"{locator!r} was accepted but was not searchable within {timeout:.0f}s. "
            "It may still land — check the collection, or raise timeout=."
        )

    # -------------------------------- read --------------------------------

    def search(
        self,
        query: str,
        limit: int = 10,
        min_score: float = 0.0,
        sources: list[str] | None = None,
        period_from: str | None = None,
        period_to: str | None = None,
        include_superseded: bool = False,
    ) -> Results:
        """Search by meaning and exact wording together.

        The result carries a `trace_id`; pass it to `kb.trace()` to see every
        candidate and score behind the answer.
        """
        params: dict[str, Any] = {
            "q": query,
            "limit": limit,
            "min_score": min_score,
            "include_superseded": include_superseded,
        }
        if sources:
            params["sources"] = sources
        if period_from:
            params["period_from"] = period_from
        if period_to:
            params["period_to"] = period_to

        return Results.from_json(
            self._kb._request("GET", "/api/search", params=params, headers=self._headers)
        )

    def list(
        self, limit: int = 50, offset: int = 0, category: str | None = None
    ) -> list[Document]:
        """What this collection holds."""
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if category:
            params["class_id"] = category
        payload = self._kb._request(
            "GET", "/api/items", params=params, headers=self._headers
        )
        return [Document.from_json(d) for d in payload.get("items", [])]

    def get(self, document_id: str, version: int | None = None) -> Document:
        """One document, current or at a specific version."""
        params = {"version": version} if version else None
        return Document.from_json(
            self._kb._request(
                "GET", f"/api/items/{document_id}", params=params, headers=self._headers
            )
        )

    def versions(self, document_id: str) -> list[Document]:
        """Every version of a document, newest first."""
        payload = self._kb._request(
            "GET", f"/api/items/{document_id}/versions", headers=self._headers
        )
        return [Document.from_json(d) for d in payload.get("versions", [])]

    def info(self) -> CollectionInfo:
        return CollectionInfo.from_json(
            self._kb._request("GET", f"/api/collections/{self.id}")
        )

    # ------------------------------ categories ------------------------------

    def categorise(self, document_id: str, categories: list[str]) -> None:
        """File a document by hand. The choice sticks — re-ingestion will not
        overwrite it."""
        self._kb._request(
            "PUT",
            f"/api/items/{document_id}/classes",
            json={"class_ids": categories},
            headers=self._headers,
        )


__all__ = ["Collection", "KnowledgeBase", "KnowledgeBaseError"]
