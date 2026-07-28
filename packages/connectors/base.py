from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# SOURCES — where items come from.
#
# Transport only. A source lists what it holds and fetches one thing's bytes;
# it never decides what an item means, how it is classified, or whether it has
# changed. That is the pipeline's job, and keeping it there is what lets a new
# provider be added without touching the write path (KB-4's explicit design
# requirement).
#
# Providers (Google Drive, S3/Spaces, Notion) land in workstream 4. What is
# here is the contract they will implement, and nothing else.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SourceItem:
    """One thing a source holds, before we have read it.

    `revision` is whatever the provider offers to say "this changed" cheaply —
    an etag, a version id, a modified timestamp. It is an optimisation only:
    the content hash is what actually decides, so a provider that lies about
    revisions costs a wasted fetch, never a wrong answer.
    """

    locator: str
    filename: str
    url: str | None = None
    modified_at: datetime | None = None
    revision: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Source(Protocol):
    """A connected external system."""

    name: str

    async def list_items(self, since: datetime | None = None) -> list[SourceItem]:
        """What this source holds, optionally only what changed since `since`."""
        ...

    async def fetch(self, item: SourceItem) -> bytes:
        """The bytes of one item. Never stored — normalised, then dropped."""
        ...


_PROVIDERS: dict[str, type] = {}


def register_provider(kind: str, cls: type) -> None:
    """Add a provider. The whole of 'pluggable' — one call, no wiring."""
    _PROVIDERS[kind] = cls


def supported_sources() -> tuple[str, ...]:
    return tuple(sorted(_PROVIDERS))


def build_source(spec: dict) -> Source:
    """Instantiate a configured source from stored connection data."""
    kind = spec.get("type")
    if kind not in _PROVIDERS:
        raise ValueError(
            f"Unknown source type: {kind!r}; available: {', '.join(supported_sources()) or 'none'}"
        )
    return _PROVIDERS[kind](**{k: v for k, v in spec.items() if k != "type"})


__all__ = [
    "Source",
    "SourceItem",
    "build_source",
    "register_provider",
    "supported_sources",
]
