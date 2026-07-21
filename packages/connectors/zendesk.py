from __future__ import annotations

import base64

import httpx
from pydantic import BaseModel, Field


class ZendeskTicketRaw(BaseModel):
    """What Zendesk's tickets API guarantees per ticket."""

    model_config = {"extra": "allow"}
    id: int
    status: str
    created_at: str
    subject: str | None = None
    description: str | None = None
    requester_id: int | None = None
    priority: str | None = None
    tags: list = Field(default_factory=list)
    assignee_id: int | None = None


class ZendeskConnector:
    """Read-only Zendesk connector.

    token format: "<email>/token:<api_token>"  (Zendesk basic-auth convention)
    config: {"subdomain": "acme"}
    """

    name = "zendesk"

    def __init__(self, token: str, subdomain: str, limit: int = 30) -> None:
        self.token = token
        self.subdomain = subdomain
        self.limit = limit

    @property
    def authenticated(self) -> bool:
        return bool(self.token and self.subdomain)

    def _headers(self) -> dict[str, str]:
        basic = base64.b64encode(self.token.encode()).decode()
        return {"Authorization": f"Basic {basic}", "Accept": "application/json"}

    async def fetch_raw(self) -> list[dict]:
        url = f"https://{self.subdomain}.zendesk.com/api/v2/tickets.json"
        async with httpx.AsyncClient(timeout=25.0) as client:
            resp = await client.get(
                url, headers=self._headers(), params={"per_page": min(self.limit, 100), "sort_order": "desc"}
            )
            resp.raise_for_status()
            data = resp.json()

        out = []
        for ticket in data.get("tickets", [])[: self.limit]:
            ticket["_subdomain"] = self.subdomain
            # a solved/closed ticket's updated_at is a fair resolution timestamp
            ticket["_resolved_at"] = (
                ticket.get("updated_at") if ticket.get("status") in ("solved", "closed") else None
            )
            out.append(ticket)
        return out

    async def backfill(self, since_days: int) -> list[dict]:
        """Zendesk's incremental ticket export API supports real deep
        pagination by `start_time`; not wired up yet (this connector is a
        stub). Falls back to the same recent window fetch_raw returns."""
        return await self.fetch_raw()
