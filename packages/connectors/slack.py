from __future__ import annotations

import os
import time

import httpx
from pydantic import BaseModel, Field

SLACK_API = "https://slack.com/api"

# Slack OAuth v2 (bot token). Scopes: read channel history + names, and resolve
# who wrote a message into a readable name. Declared like GitHub's provider so
# the generic oauth flow (core.oauth) can drive "sign in with Slack".
OAUTH_AUTHORIZE_URL = "https://slack.com/oauth/v2/authorize"
OAUTH_TOKEN_URL = f"{SLACK_API}/oauth.v2.access"
DEFAULT_OAUTH_SCOPE = "channels:history,channels:read,users:read"


# The enrichment field holding the readable author name (fetch_raw resolves
# Slack's opaque `user` id into it). Declared so ingest maps actor_name from it.
ACTOR_NAME_FIELD = "_user_name"


class SlackMessageRaw(BaseModel):
    """What Slack's conversations.history guarantees per message."""

    model_config = {"extra": "allow"}
    ts: str
    text: str = ""
    user: str | None = None
    thread_ts: str | None = None
    reactions: list = Field(default_factory=list)


# Include private channels only if the token was granted the scope to see them;
# asking for `private_channel` without groups:read fails the WHOLE call with
# missing_scope, which once looked like "you have no channels". Try the richer
# set, and fall back to public-only on exactly that error.
_FULL_TYPES = "public_channel,private_channel"
_PUBLIC_TYPES = "public_channel"


async def _list_conversations(client: httpx.AsyncClient, headers: dict, types: str, cursor: str) -> dict:
    params: dict[str, str] = {"limit": "200", "types": types, "exclude_archived": "true"}
    if cursor:
        params["cursor"] = cursor
    resp = await client.get(f"{SLACK_API}/conversations.list", headers=headers, params=params)
    return resp.json()


async def list_channels(token: str, limit: int = 1000) -> list[dict]:
    """Channels this token can watch — so after connecting, a human picks one
    from a list instead of typing a name. Same shape github.list_repos returns,
    so the connect UI stays source-agnostic. Degrades to public channels when
    the token lacks the private-channel scope, rather than returning nothing."""
    headers = {"Authorization": f"Bearer {token}"}
    types = _FULL_TYPES
    out: list[dict] = []
    async with httpx.AsyncClient(timeout=20.0) as client:
        cursor = ""
        while len(out) < limit:
            data = await _list_conversations(client, headers, types, cursor)
            if not data.get("ok"):
                if data.get("error") == "missing_scope" and types == _FULL_TYPES:
                    types, cursor, out = _PUBLIC_TYPES, "", []  # retry public-only
                    continue
                break
            for ch in data.get("channels", []):
                out.append({
                    "key": ch["name"],           # the connector resolves a name to an id
                    "label": f"#{ch['name']}",
                    "private": bool(ch.get("is_private")),
                })
            cursor = (data.get("response_metadata") or {}).get("next_cursor", "")
            if not cursor:
                break
    return out[:limit]


def oauth_provider():
    from packages.core.oauth import OAuthProvider

    return OAuthProvider(
        name="slack",
        authorize_url=OAUTH_AUTHORIZE_URL,
        token_url=OAUTH_TOKEN_URL,
        client_id=os.getenv("SLACK_CLIENT_ID", ""),
        client_secret=os.getenv("SLACK_CLIENT_SECRET", ""),
        scope=os.getenv("SLACK_OAUTH_SCOPE", DEFAULT_OAUTH_SCOPE),
    )


class SlackConnector:
    """Read-only Slack connector. Needs a bot token with channels:history +
    channels:read (+ users:read to name authors). Returns raw message payloads
    enriched with `_channel` and `_user_name` — the readable author, resolved
    once per pull so a message reaches the engine as "Priya said…", not "U04…".
    """

    name = "slack"

    def __init__(self, token: str, channel: str = "", limit: int = 30) -> None:
        self.token = token
        self.channel = channel
        self.limit = limit

    @property
    def authenticated(self) -> bool:
        return bool(self.token)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    async def _channel_id(self, client: httpx.AsyncClient) -> str:
        if self.channel.startswith("C"):
            return self.channel
        wanted = self.channel.lstrip("#")
        types = _FULL_TYPES
        cursor = ""
        while True:
            data = await _list_conversations(client, self._headers(), types, cursor)
            if not data.get("ok"):
                if data.get("error") == "missing_scope" and types == _FULL_TYPES:
                    types, cursor = _PUBLIC_TYPES, ""  # no private scope — public only
                    continue
                raise RuntimeError(f"slack conversations.list failed: {data.get('error')}")
            for ch in data.get("channels", []):
                if ch.get("name") == wanted:
                    return ch["id"]
            cursor = (data.get("response_metadata") or {}).get("next_cursor", "")
            if not cursor:
                raise RuntimeError(f"slack channel not found: {self.channel!r}")

    async def _user_names(self, client: httpx.AsyncClient) -> dict[str, str]:
        """id -> readable name for the whole workspace, in one call. Best-effort:
        without users:read we simply can't name people, so leave the ids and let
        the engine show those rather than fail the pull."""
        try:
            resp = await client.get(
                f"{SLACK_API}/users.list", headers=self._headers(), params={"limit": 1000}
            )
            data = resp.json()
        except httpx.HTTPError:
            return {}
        if not data.get("ok"):
            return {}
        names = {}
        for u in data.get("members", []):
            profile = u.get("profile") or {}
            names[u["id"]] = profile.get("real_name") or profile.get("display_name") or u.get("name") or u["id"]
        return names

    def _enrich(self, messages: list[dict], channel_id: str, names: dict[str, str]) -> list[dict]:
        out = []
        for msg in messages:
            if msg.get("subtype"):  # joins/leaves/edits etc. — not a person speaking
                continue
            msg["_channel"] = self.channel or channel_id
            msg["_channel_id"] = channel_id
            msg["_user_name"] = names.get(msg.get("user", ""), msg.get("user") or "unknown")
            out.append(msg)
        return out

    async def _history(
        self, client: httpx.AsyncClient, channel_id: str, params: dict
    ) -> tuple[list[dict], str]:
        """One page of conversations.history — (messages, next_cursor)."""
        resp = await client.get(
            f"{SLACK_API}/conversations.history",
            headers=self._headers(),
            params={"channel": channel_id, **params},
        )
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"slack conversations.history failed: {data.get('error')}")
        return data.get("messages", []), data.get("response_metadata", {}).get("next_cursor", "")

    async def fetch_raw(self) -> list[dict]:
        async with httpx.AsyncClient(timeout=25.0) as client:
            channel_id = await self._channel_id(client)
            names = await self._user_names(client)
            messages, _ = await self._history(client, channel_id, {"limit": self.limit})
            return self._enrich(messages, channel_id, names)

    async def backfill(self, since_days: int) -> list[dict]:
        """Walk real history back ``since_days`` using conversations.history's
        `oldest` cursor and pagination — the deep pull norms need to learn from,
        not just the most recent page fetch_raw returns."""
        oldest = str(time.time() - since_days * 86400)
        async with httpx.AsyncClient(timeout=25.0) as client:
            channel_id = await self._channel_id(client)
            names = await self._user_names(client)
            collected: list[dict] = []
            cursor = ""
            for _ in range(20):  # hard page cap — a busy channel is not a data dump
                params = {"limit": 200, "oldest": oldest}
                if cursor:
                    params["cursor"] = cursor
                messages, cursor = await self._history(client, channel_id, params)
                collected.extend(messages)
                if not cursor:
                    break
            return self._enrich(collected, channel_id, names)
