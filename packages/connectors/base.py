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
    if kind == "slack":
        from packages.connectors.slack import oauth_provider

        return oauth_provider()
    return None


async def list_oauth_targets(kind: str, token: str) -> list[dict]:
    """What this token can watch — repos, channels, etc. — so the UI can offer
    a picker rather than a free-text field."""
    if kind == "github":
        from packages.connectors.github import list_repos

        return await list_repos(token)
    if kind == "slack":
        from packages.connectors.slack import list_channels

        return await list_channels(token)
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


# --------------------------- declared capability ---------------------------
# Each connector declares the actions it can perform, beside the code that
# speaks the API. Same registry pattern as raw_schema_for above: the engine
# asks a connector what it can do rather than being told by a config file.


def moves_for(kind: str) -> dict[str, dict]:
    """The action specs this connector supports. Empty when it is read-only."""
    if kind == "github":
        from packages.connectors.github import MOVES

        return {k: dict(v) for k, v in MOVES.items()}
    # slack + zendesk are read-only today: they declare no writes, so the
    # engine offers none. An empty registry is the correct, honest state for a
    # connector that cannot act — not something to paper over with a stub.
    return {}


def reviewers_for(kind: str) -> list[dict]:
    """The grounded reviewers this connector declares — its default specialist
    fan-out over a record it knows carries reviewable content (a PR diff).
    Empty when the connector declares none, the same honest resting state an
    action-less connector has for moves_for.

    Same registry pattern and same read-time-overlay contract as moves_for:
    the engine asks the connector rather than reading a file, and a stored
    profile may override any entry by key (see profile.with_connector_reviewers).
    """
    if kind == "github":
        from packages.connectors.github import REVIEWERS

        return [dict(r) for r in REVIEWERS]
    return []


def review_content_for(kind: str):
    """A connector's review-time content fetcher — the real diff of a record,
    fetched fresh and never stored (see github.fetch_review_content). None when
    the connector has no reviewable body to fetch. The engine calls this only
    while grounding a reviewer, so the code it returns lives for one review and
    is dropped."""
    if kind == "github":
        from packages.connectors.github import fetch_review_content

        return fetch_review_content
    return None


def record_kind_field_for(kind: str) -> str | None:
    """The raw field naming what KIND of record a payload is, or None when a
    connector returns only one kind.

    Some APIs return several kinds of thing down one endpoint — GitHub's
    `/issues` hands back pull requests alongside issues. A connector that knows
    it does this flattens the distinction into one field, and discovery reads
    `type` from that field instead of stamping a single constant on everything
    the source produces.
    """
    if kind == "github":
        from packages.connectors.github import RECORD_KIND_FIELD

        return RECORD_KIND_FIELD
    return None


def content_extra_fields_for(kind: str) -> list[str]:
    """Raw fields whose text should be appended to an event's CONTENT.

    Some records say most of what they mean in something other than their
    title and body. A pull request is the clearest case: opened with an empty
    description, it reaches search as a bare title matching nothing, while
    what it is actually about lives in the files it touched and in the commit
    messages its author wrote. Declared by the connector, because only it
    knows those fields exist.

    Order is the order they appear in the text.
    """
    if kind == "github":
        from packages.connectors.github import (
            CHANGE_SUMMARY_FIELD,
            COMMIT_SUMMARY_FIELD,
            THREAD_SUMMARY_FIELD,
        )

        return [CHANGE_SUMMARY_FIELD, COMMIT_SUMMARY_FIELD, THREAD_SUMMARY_FIELD]
    return []


def actor_name_field_for(kind: str) -> str | None:
    """The raw field holding the READABLE author name, when the connector had to
    resolve it (Slack's `user` id -> a name in `_user_name`). None when the
    payload already carries a readable actor and the induced mapping is fine."""
    if kind == "slack":
        from packages.connectors.slack import ACTOR_NAME_FIELD

        return ACTOR_NAME_FIELD
    return None


def activity_field_for(kind: str) -> str | None:
    """The raw field saying when a record last CHANGED, or None.

    Distinct from the creation stamp, and the one every stall watcher actually
    needs: we poll snapshots, so a record's own "last updated" value is the
    only evidence that anything happened between two scans.
    """
    if kind == "github":
        from packages.connectors.github import ACTIVITY_FIELD

        return ACTIVITY_FIELD
    return None


def extra_metadata_fields_for(kind: str) -> list[str]:
    """Raw fields that must land in metadata whatever discovery decides.

    Discovery names metadata by inspecting sample payloads, which is right for
    fields it can see the point of — but a number that only becomes meaningful
    once a rhythm measures it (how big is a normal pull request?) reads as noise
    in a sample and gets dropped. These are pinned instead of proposed.
    """
    if kind == "github":
        from packages.connectors.github import (
            ACTIVITY_FIELD,
            ADDITIONS_FIELD,
            CHANGED_FILES_FIELD,
            CHANGED_PATHS_FIELD,
            COMMITS_FIELD,
            DELETIONS_FIELD,
            THREAD_FIELD,
        )

        return [
            ADDITIONS_FIELD, DELETIONS_FIELD, CHANGED_FILES_FIELD, CHANGED_PATHS_FIELD,
            ACTIVITY_FIELD, COMMITS_FIELD, THREAD_FIELD,
        ]
    return []


def target_spec_for(kind: str) -> tuple[str | None, list[str]]:
    """(url_pattern, param_names) for turning an evidence URL into action
    params, or (None, []) when the connector declares no addressable target."""
    if kind == "github":
        from packages.connectors.github import TARGET_PARAMS, TARGET_URL_PATTERN

        return TARGET_URL_PATTERN, list(TARGET_PARAMS)
    return None, []
