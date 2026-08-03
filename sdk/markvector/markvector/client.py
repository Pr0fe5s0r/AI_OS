from __future__ import annotations

import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from .agent import Agent

from .errors import (
    AuthError,
    IndexingTimeout,
    InvalidRequest,
    MarkvectorError,
    NotFound,
    Unavailable,
)
from .models import (
    Answer,
    ApiKey,
    Chunk,
    CollectionInfo,
    Document,
    MintedKey,
    Results,
    Structure,
    WriteResult,
)

DEFAULT_URL = "http://tnega-api-o9ecgm-5fbf26-217-154-175-169.traefik.me"
_RETRYABLE = {429, 500, 502, 503, 504}
__version__ = "0.1.0"

# Inside Collection the list() method shadows the builtin, so annotations that
# follow it reference this alias to still mean the container type.
_List = list


class Markvector:
    """A client for one workspace.

        mv = Markvector(api_key="kb_live_…")
        docs = mv.collection("client-research")
        docs.add("Q2 paid conversions fell 18 percent.", locator="notes/q2", wait=True)

        for hit in docs.search("why did paid results fall"):
            print(hit.score, hit.title, hit.matched_on)

        print(docs.answer("summarise Q2 performance"))

    The key identifies the workspace, so nothing here takes a workspace id. A
    key can also be bound to a single collection — then every call it makes is
    confined to that collection, whatever you ask for.
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 30.0,
        max_retries: int = 2,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        key = api_key or os.getenv("MARKVECTOR_API_KEY") or os.getenv("KB_API_KEY")
        if not key:
            raise AuthError(
                "No API key. Pass api_key= or set MARKVECTOR_API_KEY. "
                "Create one in the console under Developer → API keys."
            )
        self._key = key
        self.base_url = (
            base_url or os.getenv("MARKVECTOR_URL") or os.getenv("KB_URL") or DEFAULT_URL
        ).rstrip("/")
        self._max_retries = max_retries
        self._http = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            # `transport` exists so the client can be exercised without a running
            # server — otherwise testing this library would mean standing up the
            # whole stack or patching httpx internals.
            transport=transport,
            headers={
                "Authorization": f"Bearer {key}",
                "User-Agent": f"markvector-python/{__version__}",
            },
        )

    # ------------------------------ plumbing ------------------------------

    def _request(self, method: str, path: str, **kw: Any) -> Any:
        """One place for retries and error translation. Returns parsed JSON."""
        return self._unwrap(self._send(method, path, **kw))

    def _send(self, method: str, path: str, **kw: Any) -> httpx.Response:
        """Retries only idempotent failures — a timeout on a write could mean
        the write landed, and repeating it would be worse than reporting it."""
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
                return response
            time.sleep(0.4 * (2**attempt))
        raise last or Unavailable("Request failed.")

    @staticmethod
    def _unwrap(response: httpx.Response) -> Any:
        if response.is_success:
            return response.json() if response.content else None
        raise _error_for(response)

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
        self,
        name: str,
        collection_id: str | None = None,
        cluster_id: str | None = None,
        description: str | None = None,
    ) -> CollectionInfo:
        """Make a collection. Its embedding model and dimensions are fixed now —
        changing either later would invalidate every vector inside it."""
        created = self._request(
            "POST",
            "/api/collections",
            json={
                "name": name,
                "collection_id": collection_id,
                "cluster_id": cluster_id,
                "description": description,
            },
        )
        return CollectionInfo.from_json(created)

    def rename_collection(self, collection_id: str, name: str) -> CollectionInfo:
        """Change a collection's display name. Its id is fixed."""
        renamed = self._request(
            "PATCH", f"/api/collections/{collection_id}", json={"name": name}
        )
        return CollectionInfo.from_json(renamed)

    def delete_collection(self, collection_id: str) -> int:
        """Delete a collection and everything in it. Returns how many documents
        went with it."""
        result = self._request("DELETE", f"/api/collections/{collection_id}")
        return int((result or {}).get("items_removed", 0))

    def collection(self, collection_id: str) -> Collection:
        """A handle. Cheap — it does not call the server."""
        return Collection(self, collection_id)

    # -------------------------------- keys --------------------------------

    def keys(self) -> list[ApiKey]:
        """Existing keys — prefixes and usage only, never the secrets."""
        payload = self._request("GET", "/api/keys")
        return [ApiKey.from_json(k) for k in payload.get("keys", [])]

    def create_key(
        self,
        name: str,
        scopes: str = "read,write",
        collection_id: str | None = None,
    ) -> MintedKey:
        """Issue a key. The secret is in the return value and NOWHERE else,
        ever — store it now. Pass `collection_id` to bind the key to one
        collection; leave it None for a workspace-wide key."""
        created = self._request(
            "POST",
            "/api/keys",
            json={"name": name, "scopes": scopes, "collection_id": collection_id},
        )
        return MintedKey.from_json(created)

    def revoke_key(self, key_id: str) -> None:
        """Revoke a key immediately. Anything using it stops working at once."""
        self._request("DELETE", f"/api/keys/{key_id}")

    # ------------------------------- traces -------------------------------

    def trace(self, trace_id: str) -> dict[str, Any]:
        """Why a search returned what it did: every candidate, score and timing."""
        return self._request("GET", f"/api/traces/{trace_id}")

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> Markvector:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


class Collection:
    """Read and write one collection."""

    def __init__(self, mv: Markvector, collection_id: str) -> None:
        self._mv = mv
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
        locator again updates that document rather than adding a second copy, so
        re-running an import is safe.

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
        accepted = self._mv._request("POST", "/api/items", json=payload, headers=self._headers)
        result = WriteResult(
            job_id=accepted.get("job_id"), status=accepted.get("status", "queued")
        )
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
        """Store a document from disk — PDF, Word (.docx), Markdown or text.

        The file is converted to Markdown and indexed, and the original bytes
        are also kept so it can be downloaded again as it arrived
        (`download_original`).
        """
        p = Path(path)
        if not p.is_file():
            raise InvalidRequest(f"No such file: {p}")
        key = locator or p.name
        with p.open("rb") as fh:
            accepted = self._mv._request(
                "POST",
                "/api/items/file",
                files={"file": (p.name, fh)},
                data={"source": source, "locator": key},
                headers=self._headers,
            )
        result = WriteResult(
            job_id=accepted.get("job_id"), status=accepted.get("status", "queued")
        )
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
        files: list[str | Document] | None = None,
        sources: list[str] | None = None,
        period_from: str | None = None,
        period_to: str | None = None,
        include_superseded: bool = False,
    ) -> Results:
        """Search by meaning and exact wording together.

        Pass `files` to search only within specific documents — a list of
        document ids, or the `Document` objects returned by `list()` / `files()`.
        Omit it to search the whole collection.

        The result carries a `trace_id`; pass it to `mv.trace()` to see every
        candidate and score behind it.
        """
        params: dict[str, Any] = {
            "q": query,
            "limit": limit,
            "min_score": min_score,
            "include_superseded": include_superseded,
        }
        if files:
            params["item_ids"] = [_doc_id(f) for f in files]
        if sources:
            params["sources"] = sources
        if period_from:
            params["period_from"] = period_from
        if period_to:
            params["period_to"] = period_to
        return Results.from_json(
            self._mv._request("GET", "/api/search", params=params, headers=self._headers)
        )

    def answer(
        self,
        question: str,
        mode: str = "vectorless",
        limit: int = 8,
        sources: list[str] | None = None,
    ) -> Answer:
        """Retrieval, then a written answer built only from what was retrieved.

        Check `answer.grounded` before trusting the text: it is False when the
        store had nothing to answer from. `mode` is "vectorless" (reason over
        each document's table of contents) or "hybrid" (passage embeddings +
        keyword, fused).
        """
        params: dict[str, Any] = {"q": question, "mode": mode, "limit": limit}
        if sources:
            params["sources"] = sources
        return Answer.from_json(
            self._mv._request("GET", "/api/answer", params=params, headers=self._headers)
        )

    def list(
        self, limit: int = 50, offset: int = 0, category: str | None = None
    ) -> list[Document]:
        """What this collection holds."""
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if category:
            params["class_id"] = category
        payload = self._mv._request("GET", "/api/items", params=params, headers=self._headers)
        return [Document.from_json(d) for d in payload.get("items", [])]

    def files(self, limit: int = 200) -> _List[Document]:
        """The uploaded files in this collection — the documents that came from
        a real file (and so have a downloadable original), newest first.

        A thin filter over `list()`: use `list()` for everything, including text
        written straight in via `add()`; use `files()` for what you uploaded.
        """
        return [d for d in self.list(limit=limit) if d.original is not None]

    def get_many(self, files: _List[str | Document]) -> _List[Document]:
        """Fetch several documents in one call, in the order given — bulk
        retrieval instead of a request per id. Missing ids are simply absent."""
        payload = self._mv._request(
            "POST",
            "/api/items/batch",
            json={"ids": [_doc_id(f) for f in files]},
            headers=self._headers,
        )
        return [Document.from_json(d) for d in payload.get("items", [])]

    def structure(self, file: str | Document) -> Structure:
        """The document's heading tree — the PageIndex structure vectorless
        search reasons over. Titles, sizes and a one-line preview per section,
        built from the document's own headings (no model calls)."""
        return Structure.from_json(
            self._mv._request(
                "GET", f"/api/items/{_doc_id(file)}/structure", headers=self._headers
            )
        )

    def structures(self, files: _List[str | Document]) -> _List[Structure]:
        """Extract the PageIndex structure for a list of files in one round
        trip — bulk structure extraction over `structure()` one at a time."""
        payload = self._mv._request(
            "POST",
            "/api/items/batch",
            json={"ids": [_doc_id(f) for f in files], "structure": True},
            headers=self._headers,
        )
        return [
            Structure.from_json(d["structure"])
            for d in payload.get("items", [])
            if d.get("structure")
        ]

    def get(self, document_id: str, version: int | None = None) -> Document:
        """One document, current or at a specific version."""
        params = {"version": version} if version else None
        return Document.from_json(
            self._mv._request(
                "GET", f"/api/items/{document_id}", params=params, headers=self._headers
            )
        )

    def versions(self, document_id: str) -> _List[Document]:
        """Every version of a document, newest first."""
        payload = self._mv._request(
            "GET", f"/api/items/{document_id}/versions", headers=self._headers
        )
        return [Document.from_json(d) for d in payload.get("versions", [])]

    def chunks(self, document_id: str) -> _List[Chunk]:
        """The passages a document was split into — what actually got indexed."""
        payload = self._mv._request(
            "GET", f"/api/items/{document_id}/chunks", headers=self._headers
        )
        return [Chunk.from_json(c) for c in payload.get("chunks", [])]

    def download_original(
        self, document_id: str, path: str | Path | None = None
    ) -> bytes | Path:
        """The original file as it was uploaded.

        Returns the raw bytes, or — when `path` is given — writes them there and
        returns the Path. Raises `NotFound` for documents with no stored original
        (text written via `add`, or uploads from before originals were kept).
        """
        response = self._mv._send(
            "GET", f"/api/items/{document_id}/original", headers=self._headers
        )
        if not response.is_success:
            raise _error_for(response)
        data = response.content
        if path is None:
            return data
        out = Path(path)
        out.write_bytes(data)
        return out

    def info(self) -> CollectionInfo:
        """This collection's model, dimensions and live counts."""
        return CollectionInfo.from_json(
            self._mv._request("GET", f"/api/collections/{self.id}")
        )

    # -------------------------------- agent --------------------------------

    def agent(self, **kwargs: Any) -> Agent:
        """An agent that answers questions about this collection by reasoning
        and calling tools in a loop, driven by an OpenAI-compatible LLM you
        configure. Needs the extra: ``pip install 'markvector[agent]'``.

            agent = mv.collection("default").agent(api_key="sk-…", model="gpt-4o-mini")
            for event in agent.stream("what does the spec require?"):
                ...
            print(agent.answer("what does the spec require?").answer)

        See ``markvector.Agent`` for the full set of options.
        """
        from .agent import Agent

        return Agent(self, **kwargs)

    # ------------------------------ categories ------------------------------

    def categorise(self, document_id: str, categories: _List[str]) -> None:
        """File a document by hand. The choice sticks — re-ingestion will not
        overwrite it."""
        self._mv._request(
            "PUT",
            f"/api/items/{document_id}/classes",
            json={"class_ids": categories},
            headers=self._headers,
        )


def _doc_id(file: str | Document) -> str:
    """A document id, whether given the id itself or a Document from list()."""
    return file.id if isinstance(file, Document) else str(file)


def _error_for(response: httpx.Response) -> MarkvectorError:
    detail = response.text[:300]
    try:
        detail = response.json().get("detail", detail)
    except Exception:  # noqa: BLE001 - a non-JSON error body is still an error
        pass
    status = response.status_code
    if status in (401, 403):
        return AuthError(detail)
    if status == 404:
        return NotFound(detail)
    if 400 <= status < 500:
        return InvalidRequest(detail)
    return Unavailable(f"{status}: {detail}")


__all__ = ["Collection", "Markvector"]
