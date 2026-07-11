from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Connector(Protocol):
    """A source connector: transport only. fetch_raw() returns raw payloads;
    normalization into the Event schema is done by core.ingest() using the
    vertical-supplied mapping. Generic and domain-free.
    """

    name: str

    async def fetch_raw(self) -> list[dict]: ...


def build_connector(spec: dict) -> Connector:
    """Instantiate a connector from a vertical-supplied spec."""
    from packages.connectors.github import GitHubConnector
    from packages.connectors.slack import SlackConnector
    from packages.connectors.zendesk import ZendeskConnector

    kind = spec["type"]
    limit = int(spec.get("limit", 30))

    if kind == "github":
        return GitHubConnector(repo=spec["repo"], token=spec.get("token"), limit=limit)
    if kind == "slack":
        return SlackConnector(token=spec["token"], channel=spec.get("channel", ""), limit=limit)
    if kind == "zendesk":
        return ZendeskConnector(token=spec["token"], subdomain=spec["subdomain"], limit=limit)
    raise ValueError(f"Unknown connector type: {kind!r}")


SUPPORTED_SOURCES = ("github", "slack", "zendesk")
