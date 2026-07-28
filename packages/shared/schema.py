from __future__ import annotations

import hashlib
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# The Knowledge Base speaks about exactly one unit of knowledge: an Item.
#
# Every other word the requirements use for it — content, record, document,
# knowledge object — means this. Whatever produced it (an uploaded file, a
# synced Drive document, a generated report), by the time it reaches storage
# it is an Item: Markdown, plus the metadata needed to find it, scope it and
# know whether it is still current.
# ---------------------------------------------------------------------------


class Lifecycle(StrEnum):
    """Where an item is in its life. Retrieval serves ACTIVE only."""

    ACTIVE = "active"
    SUPERSEDED = "superseded"  # a newer version of the same source exists
    ARCHIVED = "archived"  # withdrawn deliberately
    FAILED = "failed"  # ingestion did not complete; visible, never silent


class Scope(BaseModel):
    """Two-level tenancy: an agency, and a client collection beneath it.

    Isolation between agencies AND between brands is a precondition, so this
    travels with every write and every query rather than being remembered at
    each call site. ``collection_id`` is optional because some items belong to the
    agency itself rather than to one of its clients.
    """

    workspace_id: str
    collection_id: str | None = None

    model_config = {"frozen": True}


class SourceRef(BaseModel):
    """Where an item came from, and how to get back to it.

    The KB never keeps the original binary (KB-7), so this is the only route
    back to it. ``locator`` is whatever the source needs to re-fetch: a Drive
    file id, an S3 key, a URL, a chat session id.
    """

    source: str  # "upload" | "gdrive" | "s3" | "notion" | ...
    locator: str
    url: str | None = None
    fetched_at: datetime | None = None


def content_hash(markdown: str) -> str:
    """The hash the whole write path turns on.

    One mechanism answers four separate requirements: skip duplicates (KB-1),
    reprocess only what changed on sync (KB-4), never re-embed unchanged
    content (KB-8), and decide when a new version supersedes the last (KB-1).
    Computed over the normalised Markdown, not the original bytes — the same
    document re-exported produces different bytes but identical knowledge.
    """
    return hashlib.sha256(markdown.strip().encode("utf-8")).hexdigest()


class Item(BaseModel):
    """One unit of knowledge in the KB."""

    id: str
    scope: Scope
    title: str
    body: str  # Markdown — the canonical stored representation
    source: SourceRef
    hash: str = ""
    version: int = 1
    supersedes: str | None = None  # id of the version this replaced
    status: Lifecycle = Lifecycle.ACTIVE

    # When the content was written vs. the period it *describes*. A July report
    # about Q2 has a created_at in July and a period covering Q2; time-aware
    # retrieval needs both and they are routinely different.
    created_at: datetime | None = None
    period_start: datetime | None = None
    period_end: datetime | None = None

    metadata: dict[str, Any] = Field(default_factory=dict)

    model_config = {"extra": "forbid"}

    def with_hash(self) -> Item:
        return self.model_copy(update={"hash": self.hash or content_hash(self.body)})


class Classification(BaseModel):
    """One class assigned to an item.

    The KB owns this decision, not the uploader (KB-2.a): ``confidence`` and
    ``basis`` record how it was reached so low-confidence assignments can be
    surfaced, and ``pinned`` marks a human override that re-classification
    must never silently revert (KB-2.b).
    """

    item_id: str
    class_id: str
    confidence: float = 0.0
    basis: str | None = None
    pinned: bool = False


class Hit(BaseModel):
    """One retrieval result, carrying the provenance a citation needs."""

    item_id: str
    title: str
    excerpt: str
    source: SourceRef
    score: float
    semantic: float = 0.0
    keyword: float = 0.0


# ---------------------------------------------------------------------------
# Graph — relationships between items, and the vectors used to find them.
# ---------------------------------------------------------------------------


class Link(StrEnum):
    """The relationship types the KB models. Deliberately few."""

    SUPERSEDES = "SUPERSEDES"  # version lineage
    DERIVES_FROM = "DERIVES_FROM"  # a report built from other items
    REFERENCES = "REFERENCES"  # one item cites another


class Connection(BaseModel):
    """A configured link to an external source, as shown to an operator.

    The token itself never appears here — it stays sealed in the credential
    store and is only unsealed at the moment of a fetch.
    """

    workspace_id: str
    source: str
    connected: bool = True
    config: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None


class GraphNode(BaseModel):
    id: str
    workspace_id: str
    collection_id: str | None = None
    title: str
    status: str = Lifecycle.ACTIVE
    source: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class GraphEdge(BaseModel):
    src_id: str
    dst_id: str
    type: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class GraphResult(BaseModel):
    center: str
    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)
