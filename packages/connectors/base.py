from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ValidationError


@runtime_checkable
class Connector(Protocol):
    """A source connector: transport only. fetch_raw() returns raw payloads;
    normalization into the Event schema is done by core.ingest() using the
    vertical-supplied mapping. Generic and domain-free.
    """

    name: str

    async def fetch_raw(self) -> list[dict]: ...

    async def backfill(self, since_days: int) -> list[dict]: ...


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


# --------------------------- raw-payload schemas ---------------------------
# Each connector declares a Pydantic schema for the RAW payload it expects
# from its source API — separate from the normalized Event schema. This lets
# core.pipeline validate a payload BEFORE normalizing it, so an API change or
# a mangled response fails loudly into connector_health instead of silently
# producing a garbage Event (a null id, an empty actor, ...).


def raw_schema_for(kind: str) -> type[BaseModel] | None:
    """The connector's raw-payload schema, or None if it declares none."""
    if kind == "github":
        from packages.connectors.github import GitHubItemRaw

        return GitHubItemRaw
    if kind == "slack":
        from packages.connectors.slack import SlackMessageRaw

        return SlackMessageRaw
    if kind == "zendesk":
        from packages.connectors.zendesk import ZendeskTicketRaw

        return ZendeskTicketRaw
    return None


def validate_raw(kind: str, raw: dict) -> str | None:
    """Validate one raw payload against its connector's schema.

    Returns an error string on failure, None on success (or if the connector
    declares no schema — validation is opt-in, not a hard requirement).
    """
    schema = raw_schema_for(kind)
    if schema is None:
        return None
    try:
        schema.model_validate(raw)
    except ValidationError as exc:
        return str(exc)
    return None


# ------------------------------ OAuth registry ------------------------------
# Which connectors support "sign in with…" instead of a pasted token, and how
# to enumerate what a granted token can watch. Same shape as raw_schema_for:
# the API asks the registry, so no endpoint has to branch on a source name.


def oauth_provider_for(kind: str):
    """The connector's OAuth provider spec, or None if it has no OAuth flow
    (then a pasted token is the only way in)."""
    if kind == "github":
        from packages.connectors.github import oauth_provider

        return oauth_provider()
    return None


async def list_oauth_targets(kind: str, token: str) -> list[dict]:
    """What this token can watch — repos, channels, etc. — so the UI can offer
    a picker rather than a free-text field."""
    if kind == "github":
        from packages.connectors.github import list_repos

        return await list_repos(token)
    return []


def field_presence(kind: str, raw: dict) -> dict[str, bool]:
    """Which of the schema's top-level fields are non-null/non-empty in ``raw``.

    Used for field-completeness sampling: a schema that VALIDATES can still
    have fields that are technically-optional-but-usually-present quietly
    going empty (an API silently dropping a field for some accounts).
    """
    schema = raw_schema_for(kind)
    if schema is None:
        return {}
    return {name: raw.get(name) not in (None, "") for name in schema.model_fields}
