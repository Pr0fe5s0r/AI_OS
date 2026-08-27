from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# Plain dataclasses rather than dicts, so an editor can complete them and a
# typo is an AttributeError at the call site instead of a KeyError three
# frames deeper.


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
class Match:
    """One search result, with the provenance a citation needs."""

    id: str
    title: str
    excerpt: str
    source: Source
    score: float
    semantic: float = 0.0
    keyword: float = 0.0
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
        return cls(
            id=d.get("item_id", ""),
            title=d.get("title", ""),
            excerpt=d.get("excerpt", ""),
            source=Source.from_json(d.get("source") or {}),
            score=float(d.get("score", 0) or 0),
            semantic=float(d.get("semantic", 0) or 0),
            keyword=float(d.get("keyword", 0) or 0),
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
class WriteResult:
    """What a write did. `unchanged` means the content was already held."""

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
