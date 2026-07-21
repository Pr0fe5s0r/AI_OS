from __future__ import annotations

import httpx
from pydantic import BaseModel, Field

SLACK_API = "https://slack.com/api"


class SlackMessageRaw(BaseModel):
    """What Slack's conversations.history guarantees per message."""

    model_config = {"extra": "allow"}
    ts: str
    text: str = ""
    user: str | None = None
    thread_ts: str | None = None
    reactions: list = Field(default_factory=list)


class SlackConnector:
    """Read-only Slack connector. Needs a bot token with channels:history +
    channels:read. Returns raw message payloads enriched with `_channel`."""

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
        resp = await client.get(
            f"{SLACK_API}/conversations.list",
            headers=self._headers(),
            params={"limit": 1000, "types": "public_channel"},
        )
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"slack conversations.list failed: {data.get('error')}")
        wanted = self.channel.lstrip("#")
        for ch in data.get("channels", []):
            if ch.get("name") == wanted:
                return ch["id"]
        raise RuntimeError(f"slack channel not found: {self.channel!r}")

    async def fetch_raw(self) -> list[dict]:
        async with httpx.AsyncClient(timeout=25.0) as client:
            channel_id = await self._channel_id(client)
            resp = await client.get(
                f"{SLACK_API}/conversations.history",
                headers=self._headers(),
                params={"channel": channel_id, "limit": self.limit},
            )
            data = resp.json()
            if not data.get("ok"):
                raise RuntimeError(f"slack conversations.history failed: {data.get('error')}")

        out = []
        for msg in data.get("messages", []):
            if msg.get("subtype"):  # joins/leaves etc.
                continue
            msg["_channel"] = self.channel or channel_id
            msg["_channel_id"] = channel_id
            out.append(msg)
        return out

    async def backfill(self, since_days: int) -> list[dict]:
        """Slack's `conversations.history` supports an `oldest` cursor for
        real deep pagination; not wired up yet (this connector is a stub —
        see the module docstring). Falls back to the same recent window
        fetch_raw returns rather than pretending to walk history it can't."""
        return await self.fetch_raw()
