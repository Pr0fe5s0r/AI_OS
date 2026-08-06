from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# Plain dataclasses rather than dicts, so an editor can complete them and a typo
# is an AttributeError at the call site instead of a KeyError three frames down.


def _dt(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


@dataclass(slots=True)
class Source:
    """Where a document came from, and how to get back to it."""

    source: str
    locator: str
    url: str | None = None

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Source:
        return cls(source=d.get("source", ""), locator=d.get("locator", ""), url=d.get("url"))


@dataclass(slots=True)
class Category:
    class_id: str
    name: str
    confidence: float = 0.0
    basis: str | None = None
    pinned: bool = False

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Category:
        return cls(
            class_id=d.get("class_id", ""),
            name=d.get("name", ""),
            confidence=float(d.get("confidence", 0) or 0),
            basis=d.get("basis"),
            pinned=bool(d.get("pinned")),
        )


@dataclass(slots=True)
class Original:
    """The file as uploaded, when the store kept it (PDFs, images, …)."""

    filename: str
    content_type: str
    size: int

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Original:
        return cls(
            filename=d.get("filename", ""),
            content_type=d.get("content_type", "application/octet-stream"),
            size=int(d.get("size", 0) or 0),
        )


@dataclass(slots=True)
class Document:
    """One stored document."""

    id: str
    title: str
    body: str
    source: Source
    version: int = 1
    status: str = "active"
    hash: str = ""
    created_at: datetime | None = None
    period_start: datetime | None = None
    period_end: datetime | None = None
    categories: list[Category] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def original(self) -> Original | None:
        """Metadata about the original file, if one was kept. Use
        `collection.download_original(doc.id)` to fetch the bytes."""
        raw = (self.metadata or {}).get("original")
        return Original.from_json(raw) if raw else None

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Document:
        return cls(
            id=d.get("id", ""),
            title=d.get("title", ""),
            body=d.get("body", ""),
            source=Source.from_json(d.get("source") or {}),
            version=int(d.get("version", 1)),
            status=d.get("status", "active"),
            hash=d.get("hash", ""),
            created_at=_dt(d.get("created_at")),
            period_start=_dt(d.get("period_start")),
            period_end=_dt(d.get("period_end")),
            categories=[Category.from_json(c) for c in d.get("classes") or []],
            metadata=d.get("metadata") or {},
        )


@dataclass(slots=True)
class Chunk:
    """A passage a document was split into — the unit of retrieval."""

    chunk_id: str
    ordinal: int
    heading: str
    text: str

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Chunk:
        return cls(
            chunk_id=d.get("chunk_id", ""),
            ordinal=int(d.get("ordinal", 0) or 0),
            heading=d.get("heading", ""),
            text=d.get("text", ""),
        )


@dataclass(slots=True)
class Section:
    """One node in a document's heading tree, and the sections beneath it.

    `tokens` is the section's size and `opens` a one-line preview of its
    content — enough to decide whether to read it without reading it, which is
    exactly what vectorless (PageIndex) retrieval does.
    """

    id: str
    title: str
    tokens: int = 0
    opens: str = ""
    sections: list[Section] = field(default_factory=list)

    def walk(self) -> list[Section]:
        """This section and every section nested under it, depth-first."""
        out: list[Section] = [self]
        for child in self.sections:
            out.extend(child.walk())
        return out

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Section:
        return cls(
            id=d.get("id", ""),
            title=d.get("title", ""),
            tokens=int(d.get("tokens", 0) or 0),
            opens=d.get("opens", ""),
            sections=[Section.from_json(s) for s in d.get("sections") or []],
        )


@dataclass(slots=True)
class Structure:
    """A document's own table of contents as a tree — the PageIndex structure.

    Built from the document's headings (deterministic, no model calls). It is
    what vectorless search reasons over: pick sections by their titles and
    previews, then read only those. Iterate `sections` for the top level or
    `walk()` for every section flat.
    """

    item_id: str
    title: str
    nodes: int
    sections: list[Section]

    def walk(self) -> list[Section]:
        out: list[Section] = []
        for s in self.sections:
            out.extend(s.walk())
        return out

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Structure:
        return cls(
            item_id=d.get("item_id", ""),
            title=d.get("title", ""),
            nodes=int(d.get("nodes", 0) or 0),
            sections=[Section.from_json(s) for s in d.get("sections") or []],
        )


@dataclass(slots=True)
class Match:
    """One search result, with the provenance a citation needs."""

    id: str
    title: str
    excerpt: str
    source: Source
    score: float
    semantic: float = 0.0
    keyword: float = 0.0
    # The passage that actually won this result — the unit that was scored, and
    # the handle a graph hop starts from. Pass it to `collection.neighbors()` to
    # walk from this hit to what sits near it in meaning.
    chunk_id: str = ""
    categories: list[Category] = field(default_factory=list)

    @property
    def matched_on(self) -> str:
        """How this result was found — useful when a result surprises you."""
        by_meaning = self.semantic > 0.3
        by_wording = self.keyword > 0.01
        if by_meaning and by_wording:
            return "meaning+wording"
        return "meaning" if by_meaning else "wording"

    @property
    def clean_excerpt(self) -> str:
        """The excerpt without the highlight markers."""
        return self.excerpt.replace("[[", "").replace("]]", "")

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Match:
        # The winning passage rides along under `passages`, best first; its id is
        # what a graph traversal hops from.
        passages = d.get("passages") or []
        return cls(
            id=d.get("item_id", ""),
            title=d.get("title", ""),
            excerpt=d.get("excerpt", ""),
            source=Source.from_json(d.get("source") or {}),
            score=float(d.get("score", 0) or 0),
            semantic=float(d.get("semantic", 0) or 0),
            keyword=float(d.get("keyword", 0) or 0),
            chunk_id=(passages[0].get("chunk_id", "") if passages else ""),
            categories=[Category.from_json(c) for c in d.get("classes") or []],
        )


@dataclass(slots=True)
class Results:
    """What a search returned, and the id of the record explaining why.

    Iterates and indexes like a list, so `for match in results` works, while
    still carrying the trace id and timing alongside.
    """

    query: str
    matches: list[Match]
    trace_id: str = ""
    took_ms: int = 0
    degraded: str | None = None

    def __iter__(self):
        return iter(self.matches)

    def __len__(self) -> int:
        return len(self.matches)

    def __getitem__(self, index: int) -> Match:
        return self.matches[index]

    def __bool__(self) -> bool:
        return bool(self.matches)

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Results:
        return cls(
            query=d.get("query", ""),
            matches=[Match.from_json(r) for r in d.get("results") or []],
            trace_id=d.get("trace_id", ""),
            took_ms=int(d.get("took_ms", 0) or 0),
            degraded=d.get("degraded"),
        )


@dataclass(slots=True)
class Neighbor:
    """A passage adjacent to another in the store's similarity graph.

    What a hop returns: the neighbour's own `neighbor_id` — feed it straight
    back to `collection.neighbors()` to keep walking — the document it belongs
    to, and how close it sits. `relation` names the edge; today always "near", a
    computed cosine link, with room for typed links later.
    """

    neighbor_id: str
    item_id: str
    heading: str
    title: str
    node_type: str = "fact"
    relation: str = "near"
    similarity: float = 0.0

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Neighbor:
        return cls(
            neighbor_id=d.get("neighbor_id", ""),
            item_id=d.get("item_id", "") or "",
            heading=d.get("heading", "") or "",
            title=d.get("title", "") or "",
            node_type=d.get("node_type", "fact") or "fact",
            relation=d.get("relation", "near") or "near",
            similarity=float(d.get("similarity", 0) or 0),
        )


@dataclass(slots=True)
class IndexSummary:
    """A navigation summary over a document's passages: a ``card`` (what the whole
    document is about) or a ``section_summary`` (what one section contains).

    Read these first to find the right sources. They are model-written and never
    cited — drop to the real passages (``search`` / ``get``) for evidence.
    ``covers`` is how many passages the summary connects.
    """

    chunk_id: str
    node_type: str
    item_id: str | None
    heading: str
    text: str
    covers: int
    generated_by: str | None = None
    probe_question: str | None = None

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> IndexSummary:
        return cls(
            chunk_id=d.get("chunk_id", ""),
            node_type=d.get("node_type", "") or "",
            item_id=d.get("item_id"),
            heading=d.get("heading", "") or "",
            text=d.get("text", "") or "",
            covers=int(d.get("covers", 0) or 0),
            generated_by=d.get("generated_by"),
            probe_question=d.get("probe_question"),
        )


@dataclass(slots=True)
class Citation:
    """A passage an answer leaned on, addressable by its marker `[n]`."""

    marker: int
    chunk_id: str
    item_id: str
    title: str
    heading: str
    text: str
    score: float = 0.0

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Citation:
        return cls(
            marker=int(d.get("marker", 0) or 0),
            chunk_id=d.get("chunk_id", ""),
            item_id=d.get("item_id", ""),
            title=d.get("title", ""),
            heading=d.get("heading", ""),
            text=d.get("text", ""),
            score=float(d.get("score", 0) or 0),
        )


@dataclass(slots=True)
class Answer:
    """A written answer built only from what was retrieved.

    `grounded` is False when the store had nothing to answer from, when the
    model said the passages did not cover the question, or when it wrote prose
    citing nothing. It is the difference between an answer and a guess, so check
    it before trusting `text`.
    """

    text: str
    grounded: bool
    citations: list[Citation]
    matches: list[Match]
    mode: str = ""
    trace_id: str = ""
    took_ms: int = 0
    degraded: str | None = None

    def __bool__(self) -> bool:
        return self.grounded and bool(self.text)

    def __str__(self) -> str:
        return self.text

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Answer:
        return cls(
            text=d.get("answer", ""),
            grounded=bool(d.get("grounded")),
            citations=[Citation.from_json(c) for c in d.get("citations") or []],
            matches=[Match.from_json(r) for r in d.get("results") or []],
            mode=d.get("mode", ""),
            trace_id=d.get("trace_id", ""),
            took_ms=int(d.get("took_ms", 0) or 0),
            degraded=d.get("degraded"),
        )


@dataclass(slots=True)
class WriteResult:
    """What a write did. `document` is set only when `wait=True` was passed."""

    job_id: str | None
    status: str = "queued"
    document: Document | None = None

    @property
    def indexed(self) -> bool:
        return self.document is not None


@dataclass(slots=True)
class CollectionInfo:
    collection_id: str
    name: str
    cluster_id: str = "default"
    embedding_model: str = ""
    dimensions: int = 0
    items: int = 0

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> CollectionInfo:
        stats = d.get("stats") or {}
        return cls(
            collection_id=d.get("collection_id", ""),
            name=d.get("name", ""),
            cluster_id=d.get("cluster_id", "default"),
            embedding_model=d.get("embedding_model", ""),
            dimensions=int(d.get("dimensions", 0) or 0),
            items=int(stats.get("items", d.get("items", 0)) or 0),
        )


@dataclass(slots=True)
class ApiKey:
    """An API key as the server describes it — never the secret itself, which
    exists once, in the response that created it (see `MintedKey`)."""

    key_id: str
    name: str
    prefix: str
    scopes: list[str]
    collection_id: str | None = None
    created_by: str | None = None
    created_at: datetime | None = None
    last_used_at: datetime | None = None
    revoked: bool = False

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> ApiKey:
        return cls(
            key_id=d.get("key_id", ""),
            name=d.get("name", ""),
            prefix=d.get("prefix", ""),
            scopes=list(d.get("scopes") or []),
            collection_id=d.get("collection_id"),
            created_by=d.get("created_by"),
            created_at=_dt(d.get("created_at")),
            last_used_at=_dt(d.get("last_used_at")),
            revoked=bool(d.get("revoked")),
        )


@dataclass(slots=True)
class MintedKey:
    """A freshly created key — the ONE moment the secret exists. Store `.key`
    now; only its hash is kept, so it can never be shown again."""

    key_id: str
    name: str
    key: str
    scopes: list[str]
    collection_id: str | None = None

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> MintedKey:
        return cls(
            key_id=d.get("key_id", ""),
            name=d.get("name", ""),
            key=d.get("key", ""),
            scopes=list(d.get("scopes") or []),
            collection_id=d.get("collection_id"),
        )


__all__ = [
    "Answer",
    "ApiKey",
    "Category",
    "Chunk",
    "Citation",
    "CollectionInfo",
    "Document",
    "IndexSummary",
    "Match",
    "MintedKey",
    "Neighbor",
    "Original",
    "Results",
    "Section",
    "Source",
    "Structure",
    "WriteResult",
]
