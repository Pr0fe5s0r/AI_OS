from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
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
    RateLimited,
    Unavailable,
)
from .models import (
    Answer,
    ApiKey,
    Chunk,
    Citation,
    CollectionInfo,
    Deletion,
    Document,
    IndexSummary,
    Match,
    MintedKey,
    Neighbor,
    Results,
    Structure,
    Thinking,
    ToolCall,
    ToolResult,
    WriteResult,
)

# What `answer_stream` yields: each step as it happens, then the finished
# answer as the final item.
StreamEvent = Thinking | ToolCall | ToolResult | Answer

# No default host. A client that forgets `base_url` should fail loudly rather
# than send its documents somewhere — this used to point at our own demo
# deployment, so an omitted argument silently shipped a customer's content to a
# server they had never heard of. Data residency is not a default worth having.
DEFAULT_URL = os.getenv("MARKVECTOR_URL", "")
_RETRYABLE = {429, 500, 502, 503, 504}
# What a key may be minted with. Kept here so an unknown scope is refused by the
# client instead of being dropped by the server's parser, which would hand back
# a key that looks fine and can do nothing.
SCOPES = frozenset({"read", "write", "manage"})
__version__ = "0.1.0"

# Inside Collection the list() method shadows the builtin, so annotations that
# follow it reference this alias to still mean the container type.
_List = list

# What a metadata filter may be given for one key: one value, or several to
# match any of. Numbers and booleans are accepted because metadata written as
# JSON keeps its type, and making the caller stringify it here would be busywork
# with a trap in it — see _meta_params for how each is rendered.
Filterable = str | int | float | bool
Where = dict[str, "Filterable | _List[Filterable]"]


def _meta_value(value: Filterable) -> str:
    """One filter value as the server compares it: text.

    Booleans are lowercased because that is how JSON — and therefore the stored
    metadata — spells them. Python's str(True) is "True", which would match
    nothing and look like a filter that simply found no documents.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _meta_params(where: Where | None) -> _List[str]:
    """A `where` mapping as the repeated key:value pairs the API takes.

        {"client": "acme", "kind": ["policy", "notice"]}
        -> ["client:acme", "kind:policy", "kind:notice"]

    Different keys must all match; the same key repeated matches any of its
    values. A key containing a colon cannot be expressed — the server splits on
    the first one — so it is refused here, with the reason, rather than sent to
    be misread as a shorter key with a longer value.
    """
    if not where:
        return []
    pairs: _List[str] = []
    for key, value in where.items():
        if not key or ":" in key:
            raise ValueError(
                f"metadata key {key!r} cannot be used as a filter: keys must be "
                "non-empty and cannot contain a colon."
            )
        values = value if isinstance(value, list | tuple) else [value]
        if not values:
            raise ValueError(
                f"metadata filter {key!r} has no values; omit the key instead of "
                "passing an empty list, which would match nothing."
            )
        pairs.extend(f"{key}:{_meta_value(v)}" for v in values)
    return pairs


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
        url = (
            base_url or os.getenv("MARKVECTOR_URL") or os.getenv("KB_URL") or DEFAULT_URL
        ).rstrip("/")
        if not url:
            raise InvalidRequest(
                "No base_url. Pass base_url= or set MARKVECTOR_URL — for example "
                "http://localhost:8000 for a local stack, or your own deployment. "
                "There is deliberately no default: a client that silently sent "
                "documents to somebody else's server would be a data-residency "
                "incident, not a convenience."
            )
        self.base_url = url
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
        the write landed, and repeating it would be worse than reporting it.

        A 429 is waited out for the time the SERVER asked for, not for a
        backoff of our own invention. Guessing shorter hammers a store that has
        just said it is busy; guessing longer wastes the caller's time. The
        header is the only party that knows.
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
                    time.sleep(_backoff(response, attempt))
                    continue
                return response
            time.sleep(0.4 * (2**attempt))
        raise last or Unavailable("Request failed.")

    def _stream_events(
        self, path: str, *, params: dict[str, Any], headers: dict[str, str]
    ) -> Iterator[StreamEvent]:
        """Server-Sent Events, as typed steps.

        The server sends one `data:` line per step and a terminal `done`
        carrying the same payload the blocking route returns — so the last item
        yielded here is the finished Answer, and a caller that stops early
        simply closes the connection.

        Unknown event types are SKIPPED rather than raised on: the server may
        learn to report a new kind of step, and a client from last month should
        keep working when it does.
        """
        with self._http.stream("GET", path, params=params, headers=headers) as response:
            if not response.is_success:
                response.read()
                raise _error_for(response)
            for line in response.iter_lines():
                if not line.startswith("data:"):
                    continue
                try:
                    event = json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    continue
                parsed = _stream_event(event)
                if parsed is not None:
                    yield parsed

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

    def workspaces(self) -> _List[str]:
        """Every workspace this key may reach, home first.

        One entry for an ordinary key, several for a key that was explicitly
        granted more. Asked of the server rather than assumed, because the
        grant lives on the key and the caller may not know what they were
        given — and because discovering a boundary by being refused at it is a
        poor way to learn where it is.
        """
        me = self.whoami()
        found = me.get("workspaces") or [me.get("workspace_id")]
        return [str(w) for w in found if w]

    def multi_collection(self, collections: _List[str] | None = None) -> MultiCollection:
        """Read several collections at once, with a MULTI-COLLECTION key.

            across = mv.multi_collection()                    # every collection the key reaches
            across = mv.multi_collection(["acme", "globex"])  # or name them

            hits = across.search("renewal terms")
            for match in hits:
                match.collection      # which one it came from

        A multi-collection key is one with no collection binding, so the server
        already allows every collection in its workspace; naming a subset here
        narrows what is READ, it does not widen what is permitted. A
        single-collection key is confined by the server and a collection it is
        not bound to is refused, whatever is passed here.

        What comes back is what each collection returned — the same excerpts,
        scores, citations and page images a single-collection read produces,
        with nothing merged away or rewritten. The only thing added is
        `collection` on each match and citation, without which a merged list
        cannot be told apart and a page image cannot be fetched for the right
        one.
        """
        return MultiCollection(self, collections)

    def collection(self, collection_id: str, workspace: str | None = None) -> Collection:
        """A handle. Cheap — it does not call the server.

        `workspace` selects among the workspaces a multi-workspace key was
        granted; the server checks it against that grant on every request. An
        ordinary key may name its own workspace or leave it out — anything
        else is refused there, not here, because a client-side check is a
        convenience and the server's is the boundary.
        """
        return Collection(self, collection_id, workspace)

    # NOTE: deleting a DOCUMENT lives on Collection.delete(), where the
    # collection it belongs to is already known. Deleting a COLLECTION is a
    # workspace-level act and lives here.

    # -------------------------------- keys --------------------------------

    def keys(self) -> list[ApiKey]:
        """Existing keys — prefixes and usage only, never the secrets."""
        payload = self._request("GET", "/api/keys")
        return [ApiKey.from_json(k) for k in payload.get("keys", [])]

    def create_key(
        self,
        name: str,
        scopes: str | _List[str] = "read,write",
        collection_id: str | None = None,
    ) -> MintedKey:
        """Issue a key. The secret is in the return value and NOWHERE else,
        ever — store it now.

        Three scopes, and the third is the one people miss:

          read    see document contents — bodies, passages, answers, originals,
                  page pictures, and traces, which carry queries and excerpts
          write   ingest and edit those contents, and delete a document
          manage  administer the CONTAINERS: create, rename and delete
                  collections, mint and revoke keys. It reads NOTHING.

        `manage` without `read` is what lets a platform operator set a tenant up
        and wind them down without being able to open a single one of their
        documents:

            mv.create_key("ops", scopes=["manage"])

        Pass `collection_id` for a SINGLE-COLLECTION key — every call it
        makes is then confined there, whatever the caller asks for, and it may
        only mint keys bound to the same collection. Leave it None for a
        MULTI-COLLECTION key, which reaches every collection in the
        workspace.
        """
        wanted = ",".join(scopes) if isinstance(scopes, list) else scopes
        unknown = sorted({s.strip() for s in wanted.split(",") if s.strip()} - SCOPES)
        if unknown:
            # Refused here rather than sent: an unrecognised scope is silently
            # dropped by the parser, so a key asked for "admin" would come back
            # looking successful and able to do nothing.
            raise ValueError(
                f"unknown scope(s) {', '.join(unknown)}; valid scopes are "
                f"{', '.join(sorted(SCOPES))}."
            )
        created = self._request(
            "POST",
            "/api/keys",
            json={"name": name, "scopes": wanted, "collection_id": collection_id},
        )
        return MintedKey.from_json(created)

    def revoke_key(self, key_id: str) -> None:
        """Revoke a key immediately. Anything using it stops working at once."""
        self._request("DELETE", f"/api/keys/{key_id}")

    # ------------------------------- traces -------------------------------

    def traces(
        self,
        limit: int = 50,
        only_empty: bool = False,
        only_degraded: bool = False,
    ) -> _List[dict[str, Any]]:
        """Recent queries against this workspace, newest first.

        One row per retrieval, whatever the outcome — an answer that found
        nothing is recorded exactly like one that found plenty, which is the
        point: the queries worth reading are usually the disappointing ones.

            for t in mv.traces(only_empty=True):
                print(t["query"])          # what people asked and got nothing for

        `only_degraded` narrows to runs where something fell back — a provider
        timeout, a missing index — and each row says which.
        """
        payload = self._request(
            "GET",
            "/api/traces",
            params={
                "limit": limit,
                "only_empty": only_empty,
                "only_degraded": only_degraded,
            },
        )
        return list(payload.get("traces", []))

    def trace_stats(self, hours: int = 24) -> dict[str, Any]:
        """How retrieval has behaved over a window: volume, empties, timings."""
        return self._request("GET", "/api/traces/stats", params={"hours": hours})

    def trace(self, trace_id: str) -> dict[str, Any]:
        """Why a search returned what it did: every candidate, score and timing."""
        return self._request("GET", f"/api/traces/{trace_id}")

    # ------------------------------ the store itself ------------------------------

    def formats(self) -> dict[str, Any]:
        """What this store can read, and what it will do to it.

        The capability contract, versioned — `version` moves when the list
        does, so software that converts files on its own side can tell when
        the ground shifted. Each entry says what a passage looks like once the
        format is parsed and what is dropped on the way, because "supported"
        alone does not tell you whether your tables survive.

            contract = mv.formats()
            contract["version"]         # "1.0.0"
            contract["supported"]       # ["csv", "docx", "html", ...]
            contract["not_supported"]   # with a reason, and what to convert to
        """
        return self._request("GET", "/api/formats")

    # -------------------------------- passages --------------------------------

    def chunk(self, chunk_id: str) -> dict[str, Any]:
        """One passage in full, with the document it belongs to.

        Search returns excerpts; this returns the whole passage. Take the id
        from `match.chunk_id` or `citation.chunk_id` — it is the unit that was
        actually scored, so this is what the answer was really reading.
        """
        return self._request("GET", f"/api/chunks/{chunk_id}")

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> Markvector:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


class Collection:
    """Read and write one collection."""

    def __init__(
        self, mv: Markvector, collection_id: str, workspace: str | None = None
    ) -> None:
        self._mv = mv
        self.id = collection_id
        # None for the key's own workspace. A value is sent as X-Workspace and
        # is checked by the SERVER against the key's grant — this handle is
        # addressing, not authorisation.
        self.workspace = workspace

    def __repr__(self) -> str:
        where = f" in {self.workspace!r}" if self.workspace else ""
        return f"<Collection {self.id!r}{where}>"

    @property
    def _headers(self) -> dict[str, str]:
        headers = {"X-Collection": self.id}
        if self.workspace:
            headers["X-Workspace"] = self.workspace
        return headers

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
        where: Where | None = None,
    ) -> Results:
        """Search by meaning and exact wording together.

        Pass `files` to search only within specific documents — a list of
        document ids, or the `Document` objects returned by `list()` / `files()`.
        Omit it to search the whole collection.

        `where` filters on the metadata a document was ingested with — the same
        object you passed to `add()` / `add_file()`:

            docs.search("renewal terms", where={"client": "acme"})
            docs.search("renewal terms", where={"kind": ["policy", "notice"]})

        Different keys must ALL match; the same key with several values matches
        any of them. Values are compared as text.

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
        if meta := _meta_params(where):
            params["meta"] = meta
        return Results.from_json(
            self._mv._request("GET", "/api/search", params=params, headers=self._headers)
        )

    def answer(
        self,
        question: str,
        mode: str = "agentic",
        limit: int = 8,
        sources: list[str] | None = None,
        documents: list[str | Document] | None = None,
        where: Where | None = None,
        vision: bool = True,
    ) -> Answer:
        """Retrieval, then a written answer built only from what was retrieved.

        `where` narrows the answer to documents whose metadata matches, before
        anything is retrieved — so an agent working for one client can be held
        to that client's documents:

            docs.answer("what did we agree on pricing", where={"client": "acme"})

        `documents` restricts the answer to those documents and nothing else —
        pass ids or Document objects. Leave it off and the store decides which
        documents the question is about, which is what you want unless you
        already know: on a collection larger than the catalogue window it ranks
        every document against the question rather than falling back to the
        most recently uploaded.

        Check `answer.grounded` before trusting the text: it is False when the
        store had nothing to answer from. `mode` is one of:

          "agentic"  (default) an agent reaches the whole collection — reasoning
                     over each document's table of contents, searching passages,
                     and hopping the similarity graph as the question needs.
          "hybrid"   passage embeddings + keyword, fused into one ranked pass:
                     fast and deterministic.

        ("vectorless" is still accepted for backward compatibility — catalogue
        reasoning only — but agentic does the same and also reaches the rest of
        a large collection, so prefer it.)

        `vision` lets the agent READ A PAGE AS A PICTURE when the text layer
        cannot answer — a figure, a scanned table, a slide whose content is
        drawn rather than written. On by default, because a question about a
        diagram otherwise gets answered from its caption. Turn it off for
        speed: it is the single most expensive step in a walk, and with it off
        a page-picture question fails honestly instead.
        """
        params: dict[str, Any] = {
            "q": question,
            "mode": mode,
            "limit": limit,
            "vision": vision,
        }
        if sources:
            params["sources"] = sources
        if documents:
            # Accepts either, because you usually have the Document already —
            # from list(), files() or add() — and reaching into it for .id is
            # busywork the caller should not have to write.
            params["doc"] = [d.id if isinstance(d, Document) else d for d in documents]
        if meta := _meta_params(where):
            params["meta"] = meta
        return Answer.from_json(
            self._mv._request("GET", "/api/answer", params=params, headers=self._headers)
        )

    def answer_stream(
        self,
        question: str,
        mode: str = "agentic",
        limit: int = 8,
        sources: _List[str] | None = None,
        documents: _List[str | Document] | None = None,
        where: Where | None = None,
        vision: bool = True,
    ) -> Iterator[StreamEvent]:
        """The same answer as `answer()`, yielded as it is produced.

        Each step arrives as it happens — the agent's reasoning, every tool call
        and its result — and the LAST item is always the finished `Answer`:

            for event in docs.answer_stream("why did churn rise"):
                match event:
                    case Thinking(text):        print(text, end="", flush=True)
                    case ToolCall(name, args):  print(f"
[{name}]")
                    case Answer() as final:     print(final.text)

        The generator is lazy: nothing is requested until you iterate, and
        abandoning it closes the connection. Use `answer()` when you only want
        the result — this exists to show the work while it happens.
        """
        params: dict[str, Any] = {
            "q": question,
            "mode": mode,
            "limit": limit,
            "vision": vision,
        }
        if sources:
            params["sources"] = sources
        if documents:
            params["doc"] = [d.id if isinstance(d, Document) else d for d in documents]
        if meta := _meta_params(where):
            params["meta"] = meta
        return self._mv._stream_events(
            "/api/answer/stream", params=params, headers=self._headers
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

    def summaries(self) -> _List[IndexSummary]:
        """The collection's navigable index: a card per document (what it is
        about) and section summaries (what each part contains), with how many
        passages each connects.

        Read this FIRST to find the right sources. It is the map — a card tells
        you which document holds what — and then `search`, `structure` and `get`
        are how you follow it to the real passages an answer must cite.
        """
        payload = self._mv._request(
            "GET", f"/api/collections/{self.id}/summaries", headers=self._headers
        )
        return [IndexSummary.from_json(s) for s in payload.get("summaries", [])]

    def neighbors(self, chunk: str | Chunk | Match, limit: int = 10) -> _List[Neighbor]:
        """The passages nearest a given one — a hop across the similarity graph.

        The traversal primitive. Pass a passage's `chunk_id` — a `Match` from
        `search()` carries one, as does a `Chunk` from `chunks()` — and get back
        what sits next to it in meaning, so an agent can follow the thread from a
        hit to related passages instead of searching again from the top. Feed a
        returned `neighbor_id` straight back in to keep walking.

        These are the stored links the consolidation pass maintains, so the walk
        is over the graph as the store has organised it, not a fresh computation.
        """
        cid = chunk if isinstance(chunk, str) else chunk.chunk_id
        payload = self._mv._request(
            "GET",
            f"/api/chunks/{cid}/neighbors",
            params={"limit": limit},
            headers=self._headers,
        )
        return [Neighbor.from_json(n) for n in payload.get("neighbors", [])]

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

    def update(
        self,
        document: str | Document,
        title: str | None = None,
        text: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Document:
        """Edit a document, keeping its version history.

        The old version is superseded, not overwritten — `versions()` still
        returns it, and anything that cited it can still resolve. Pass only
        what changes; at least one of title, text or metadata is required.

        Re-editing the text re-embeds it, so a search will reflect the change
        once indexing catches up.
        """
        if title is None and text is None and metadata is None:
            raise InvalidRequest(
                "update() needs a title, text or metadata to change."
            )
        body: dict[str, Any] = {}
        if title is not None:
            body["title"] = title
        if text is not None:
            body["body"] = text
        if metadata is not None:
            body["metadata"] = metadata
        got = self._mv._request(
            "PATCH",
            f"/api/items/{_doc_id(document)}",
            json=body,
            headers=self._headers,
        )
        return Document.from_json(got.get("item", got))

    def delete(self, document: str | Document, confirm: bool = False) -> Deletion:
        """Delete a document and everything indexed from it.

        Two steps, deliberately. The first call destroys NOTHING and returns
        what would go:

            plan = docs.delete(doc)          # nothing is deleted
            print(plan)                      # Would delete 'COMPUTER NETWORKS': 2 version(s), 3915 passage(s)
            docs.delete(doc, confirm=True)   # now it is gone

        There is no undo and no trash to restore from, so a caller that means it
        says so. Everything derived goes too — passages, vectors, the stored
        original, every rendered page — because a passage that outlived its
        document would still carry an embedding, and so would still answer
        questions.

        Raises `NotFound` if there is no such document in this collection.
        """
        doc = document.id if isinstance(document, Document) else document
        response = self._mv._send(
            "DELETE",
            f"/api/items/{doc}",
            params={"confirm": "true"} if confirm else None,
            headers=self._headers,
        )
        if confirm:
            if not response.is_success:
                raise _error_for(response)
            body = response.json() or {}
            return Deletion.from_json(body, deleted=True)

        # The unconfirmed call is REFUSED by design: 409, carrying the summary.
        # A 2xx here would mean the server deleted something we promised it
        # would not, so it is treated as an error rather than parsed.
        if response.status_code == 409:
            detail = (response.json() or {}).get("detail") or {}
            return Deletion.from_json(detail.get("would_delete") or {})
        raise _error_for(response)

    def page_image(
        self,
        page: int | Citation,
        document_id: str | Document | None = None,
        path: str | Path | None = None,
    ) -> bytes | Path:
        """The picture of one page — or slide — as a PNG.

        Two ways to call it, and the first is the one you usually want:

            image = docs.page_image(answer.citations[0])   # what was cited
            image = docs.page_image(34, document_id=doc)   # a page you chose

        Returns the bytes, or writes them to `path` and returns the Path.

        A citation whose `page_image` is None has no picture — a spreadsheet, a
        pasted note, a deck uploaded before conversion was available — and this
        raises `InvalidRequest` rather than requesting a URL that cannot exist.
        Check `citation.page_image` (or `document.page_image`) first if you want
        to branch instead of catching.
        """
        if isinstance(page, Citation):
            if not page.page_image:
                raise InvalidRequest(
                    f"{page.title!r} has no picture of "
                    f"{page.page_label or 'that page'} — page_image is None."
                )
            url = page.page_image
        else:
            if document_id is None:
                raise InvalidRequest("page_image(page) needs document_id=...")
            doc = document_id.id if isinstance(document_id, Document) else document_id
            url = f"/api/items/{doc}/pages/{int(page)}"

        response = self._mv._send("GET", url, headers=self._headers)
        if not response.is_success:
            raise _error_for(response)
        if path is None:
            return response.content
        out = Path(path)
        out.write_bytes(response.content)
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

    def classes(self) -> list[dict[str, Any]]:
        """The classes a document here can be filed into.

        Companion to `categorise()`, which takes ids: without this a caller
        has to guess them. Every document ingested is filed automatically
        against this list, and the result comes back on `document.categories`.
        """
        got = self._mv._request("GET", "/api/classes", headers=self._headers)
        return got.get("classes", [])

    # --------------------------- what the store has learned ---------------------------

    def graph(self, k: int = 3, limit: int = 200) -> dict[str, Any]:
        """The collection as a neighbour graph over passages.

        `k` is how many neighbours each passage keeps. What the index looks
        like, rather than what any one query returned.
        """
        return self._mv._request(
            "GET",
            f"/api/collections/{self.id}/graph",
            params={"k": k, "limit": limit},
            headers=self._headers,
        )

    def mapping(self, limit: int = 20) -> dict[str, Any]:
        """How far the semantic map has been built over this collection."""
        return self._mv._request(
            "GET",
            f"/api/collections/{self.id}/mapping",
            params={"limit": limit},
            headers=self._headers,
        )

    def summarise(self, rebuild: bool = False) -> dict[str, Any]:
        """Build the summary index for this collection.

        `rebuild=True` discards what is there and starts again; the default
        fills in only what is missing.
        """
        return self._mv._request(
            "POST",
            f"/api/collections/{self.id}/summarize",
            params={"rebuild": rebuild},
            headers=self._headers,
        )

    # The spelling the rest of this SDK uses is British, and the API route is
    # American. Both names work rather than making anyone remember which.
    summarize = summarise


class MultiCollection:
    """Several collections, read together and returned unchanged.

    Built by `mv.multi_collection(...)`. Every call fans out over the chosen
    collections and hands back exactly what each one gave: verbatim excerpts,
    real citations with document, page label, heading and page image. Nothing
    is redacted, summarised, generalised or merged into a single voice — the
    collections belong to the caller's own workspace, so there is no boundary
    here for the data to cross.
    """

    def __init__(self, mv: Markvector, collections: _List[str] | None = None) -> None:
        self._mv = mv
        self._named = list(collections) if collections else None
        # document id -> the collection it was found in, learned as documents
        # are listed or opened. An id carries no collection with it, and the
        # agent hands back nothing but ids.
        self._home: dict[str, str] = {}
        # What went wrong on the last read, per collection. A collection that
        # times out or errors is NOT one with nothing to say, and reporting
        # them the same way turned an agentic run whose calls all timed out
        # into a confident "0 answered".
        self.failures: dict[str, str] = {}

    @property
    def collections(self) -> _List[str]:
        """The collections this will read. Resolved from the server when the
        caller named none, so "everything I can reach" does not have to be
        maintained by hand."""
        if self._named is not None:
            return list(self._named)
        try:
            return [c.collection_id for c in self._mv.collections()]
        except AuthError as exc:
            # Listing collections is a MANAGE route, and a key that only reads
            # across them has no business holding manage. So discovery is not
            # available to the very keys most likely to want it, and the bare
            # "needs the manage scope" that came back named the wrong problem.
            raise InvalidRequest(
                "This key cannot list collections — that needs the manage "
                "scope, which a read-only key should not have. Name them "
                "instead: mv.multi_collection(['acme', 'globex'])."
            ) from exc

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"MultiCollection({', '.join(self.collections)})"

    def search(self, query: str, limit: int = 10, **kwargs: Any) -> Results:
        """Search every collection and return the matches, best first.

        One ordinary search per collection — each enforced by the server
        against the key — merged by score. The excerpts are the store's own,
        untouched; `match.collection` says where each came from.

        `limit` is per collection, so the merged list can be longer. That is
        deliberate: trimming it to `limit` overall would silently drop a
        collection's best hit because another collection scored higher, which
        is the opposite of reading them all.
        """
        found: _List[Match] = []
        self.failures = {}
        for name in self.collections:
            try:
                hits = self._mv.collection(name).search(query, limit=limit, **kwargs)
            except MarkvectorError as exc:
                # One collection failing must not end the read — but it must
                # not look like an empty one either. See `failures`.
                self.failures[name] = f"{type(exc).__name__}: {exc}"
                continue
            for match in hits.matches:
                match.collection = name
                found.append(match)
        found.sort(key=lambda m: m.score, reverse=True)
        return Results(query=query, matches=found)

    def answer(self, question: str, **kwargs: Any) -> _List[Answer]:
        """Answer from each collection, one answer each, unchanged.

        A list rather than one merged answer, and deliberately so: merging
        would mean rewriting several grounded answers into a new one that no
        single collection actually supports, and the citations would no longer
        point at what produced them. Each answer keeps its own text, its own
        `grounded` flag and its own citations — every citation carrying the
        collection it came from, so its page image can be fetched.

        Collections with nothing to say are left out, so an empty list means
        nothing answered rather than nothing was read — and `failures` says
        which ones could not be read at all, which is a different thing again.

        Agentic answers are slow: give the client a timeout that suits them
        (`Markvector(..., timeout=180)`) or every call will time out and land
        in `failures`.
        """
        out: _List[Answer] = []
        self.failures = {}
        for name in self.collections:
            try:
                answer = self._mv.collection(name).answer(question, **kwargs)
            except MarkvectorError as exc:
                self.failures[name] = f"{type(exc).__name__}: {exc}"
                continue
            # Grounded, not merely non-empty: the store answers "nothing here
            # covers that" in prose, and returning one of those per silent
            # collection buries the answers that exist. What WAS read is on
            # `collections`, so nothing is hidden by leaving them out.
            if not answer.grounded:
                continue
            answer.collection = name
            for citation in answer.citations:
                citation.collection = name
            for match in answer.matches:
                match.collection = name
            out.append(answer)
        return out

    # ---- the read surface an Agent needs, fanned out over every collection ----
    #
    # The agent calls list/summaries/structure/get/neighbors/search on whatever
    # it was handed. Implementing them here is what lets one agent reason over
    # several collections at once: it does not know it is doing so, and does
    # not have to.

    def list(self, limit: int = 50, **kwargs: Any) -> _List[Document]:
        """Documents from every collection, each tagged with where it lives."""
        out: _List[Document] = []
        for name in self.collections:
            try:
                found = self._mv.collection(name).list(limit=limit, **kwargs)
            except MarkvectorError:
                continue
            for doc in found:
                self._remember(doc.id, name)
                # Stamped, not merely remembered. The agent reads these rows
                # and has to be able to SAY which collection a document came
                # out of; a lookup table it cannot see does not help it cite.
                doc.collection = name
                out.append(doc)
        return out

    def summaries(self, **kwargs: Any) -> _List[IndexSummary]:
        """Every collection's index cards, concatenated."""
        out: _List[IndexSummary] = []
        for name in self.collections:
            try:
                cards = self._mv.collection(name).summaries(**kwargs)
            except MarkvectorError:
                continue
            for card in cards:
                self._remember(card.item_id, name)
                card.collection = name
                out.append(card)
        return out

    def get(self, document: str | Document, **kwargs: Any) -> Document:
        """One document, from whichever collection holds it."""
        home = self._where_it_lives(_doc_id(document))
        found = home.get(document, **kwargs)
        found.collection = home.id
        return found

    def structure(self, file: str | Document, **kwargs: Any) -> Structure:
        """A document's heading tree, from whichever collection holds it."""
        return self._where_it_lives(_doc_id(file)).structure(file, **kwargs)

    def neighbors(self, chunk: str | Chunk | Match, **kwargs: Any) -> _List[Neighbor]:
        """Hop the similarity graph. A passage sits in exactly one collection,
        so the hop stays inside it — a graph walk must not become a way of
        reaching a collection the search never returned."""
        if isinstance(chunk, Match) and chunk.collection:
            return self._mv.collection(chunk.collection).neighbors(chunk, **kwargs)
        chunk_id = chunk if isinstance(chunk, str) else chunk.chunk_id
        for name in self.collections:
            try:
                return self._mv.collection(name).neighbors(chunk_id, **kwargs)
            except MarkvectorError:
                continue
        return []

    def _remember(self, item_id: str, collection: str) -> None:
        """Note which collection a document came from, so a later read of it
        does not have to try them all."""
        self._home.setdefault(item_id, collection)

    def _where_it_lives(self, item_id: str) -> Collection:
        """The collection holding a document — remembered if it was listed,
        otherwise found by asking. Documents are addressed by id alone once
        the agent has one, and an id carries no collection with it."""
        known = self._home.get(item_id)
        if known:
            return self._mv.collection(known)
        for name in self.collections:
            try:
                self._mv.collection(name).get(item_id)
            except MarkvectorError:
                continue
            self._home[item_id] = name
            return self._mv.collection(name)
        raise NotFound(f"No document {item_id!r} in {', '.join(self.collections)}.")

    def agent(self, **options: Any) -> Any:
        """An agent that reasons across EVERY collection this reaches.

            bot = mv.multi_collection(["acme", "globex"]).agent(
                api_key="…", base_url="…", model="…",
            )
            print(bot.ask("how is onboarding handled?").text)

        The same loop as `collection.agent()` — the model reads the catalogue,
        searches, opens sections, hops the similarity graph — except every
        tool reaches all the collections at once. Vision comes with it: the
        agent can open a page as a picture when the text layer cannot answer,
        which is how a question about a figure or a scanned table gets a real
        answer rather than one built from a caption.

        Nothing is redacted on the way back. The answers and citations are the
        collections' own.
        """
        from .agent import Agent

        return Agent(self, **options)  # type: ignore[arg-type]

    def page_image(self, citation: Citation) -> bytes:
        """The picture of a cited page, fetched from the collection it is in.

        A citation from a merged read carries its own collection, so this
        needs nothing else from the caller — which is the whole reason that
        field exists.
        """
        if not citation.collection:
            raise InvalidRequest(
                "This citation carries no collection. Fetch it through the "
                "collection it came from, or use a citation returned by "
                "MultiCollection.answer()."
            )
        return self._mv.collection(citation.collection).page_image(citation)


# The longest this client will sit inside one retry. A server is entitled to
# ask for a minute; a library is not entitled to block a caller's thread for it
# without saying so, and RateLimited carries the number for code that wants to
# schedule the work properly instead.
MAX_RETRY_WAIT_SECONDS = 10.0


def _retry_after(response: httpx.Response) -> float | None:
    """The server's own answer to "when should I come back?", in seconds."""
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        # The HTTP date form. Rare from an API, and not worth parsing badly:
        # falling back to the caller's backoff is honest, and never wrong by
        # more than a few seconds.
        return None


def _backoff(response: httpx.Response, attempt: int) -> float:
    """How long to wait before retrying — the server's number when it gave one."""
    asked = _retry_after(response)
    if asked is not None:
        return min(asked, MAX_RETRY_WAIT_SECONDS)
    return 0.4 * (2**attempt)


def _stream_event(event: dict[str, Any]) -> StreamEvent | None:
    """One SSE payload as the same event types the local agent yields.

    Deliberately the same vocabulary: whether the reasoning happens on the
    server (`answer_stream`) or in your own process (`Agent.stream`), a caller
    renders it with one piece of code.
    """
    kind = event.get("type")
    if kind == "thinking":
        return Thinking(str(event.get("text") or ""))
    if kind == "token":
        return Thinking(str(event.get("delta") or ""))
    if kind == "tool_call":
        return ToolCall(str(event.get("tool") or ""), dict(event.get("args") or {}))
    if kind == "tool_result":
        detail = event.get("detail") or event.get("count")
        return ToolResult(str(event.get("tool") or ""), str(detail if detail is not None else "ok"))
    if kind == "done":
        return Answer.from_json(event.get("answer") or {})
    if kind == "error":
        raise Unavailable(str(event.get("message") or "the stream failed"))
    return None


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
    if status == 429:
        # Its own type: the caller can schedule around this one, and the server
        # has told us exactly when. Reaching here means the automatic waits
        # were already spent.
        wait = _retry_after(response)
        limit = response.headers.get("ratelimit-limit")
        if isinstance(detail, dict):
            detail = detail.get("message") or str(detail)
        return RateLimited(
            str(detail),
            retry_after=int(wait) if wait is not None else 1,
            limit=int(limit) if limit and limit.isdigit() else None,
        )
    if 400 <= status < 500:
        return InvalidRequest(detail)
    return Unavailable(f"{status}: {detail}")


__all__ = ["SCOPES", "Collection", "Markvector", "StreamEvent", "Where"]
