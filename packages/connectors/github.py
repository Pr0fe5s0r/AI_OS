from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import httpx
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from packages.core.oauth import OAuthProvider

GITHUB_API = "https://api.github.com"


class GitHubUserRaw(BaseModel):
    model_config = {"extra": "allow"}
    login: str


class GitHubItemRaw(BaseModel):
    """What GitHub's issues API guarantees for both issues and PRs.

    Validated BEFORE normalization — a payload that doesn't match this shape
    (an API change, a proxy mangling the response) fails loudly into
    connector_health instead of silently producing a garbage Event.
    """

    model_config = {"extra": "allow"}
    number: int
    title: str
    state: str
    user: GitHubUserRaw
    html_url: str
    created_at: str
    body: str | None = None
    labels: list = Field(default_factory=list)
    assignee: dict | None = None
    comments: int = 0
    closed_at: str | None = None
    updated_at: str | None = None

# Webhook transport constants + payload reshaping live with the connector:
# they are GitHub wire-format knowledge, not business logic.
SIGNATURE_HEADER = "X-Hub-Signature-256"
EVENT_HEADER = "X-GitHub-Event"

_PAYLOAD_KEY = {"issues": "issue", "pull_request": "pull_request"}


def webhook_raws(event_type: str, payload: dict) -> list[dict]:
    """Reshape a webhook payload into the connector's raw format (pure)."""
    key = _PAYLOAD_KEY.get(event_type)
    if key is None or key not in payload:
        return []
    raw = dict(payload[key])
    raw["_repo"] = payload.get("repository", {}).get("name", "")
    if event_type == "pull_request":
        # the PR object has no "pull_request" marker key the way the issues
        # API does — set it so the field mapping types this as a pull_request
        raw.setdefault("pull_request", {})
        raw["_merged_at"] = raw.get("merged_at") or raw.get("closed_at")
    else:
        raw["_merged_at"] = raw.get("closed_at")
    return [raw]


def repo_slug(repo: str) -> str:
    return repo.split("/")[-1]


# ------------------------------- OAuth -------------------------------
# GitHub's OAuth endpoints + the scope we ask for: wire-format knowledge, so
# it lives with the connector, next to the webhook constants. The generic flow
# that uses it is core.oauth.

OAUTH_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
OAUTH_TOKEN_URL = "https://github.com/login/oauth/access_token"

# Least privilege by default: `public_repo` can read issues/PRs and write
# labels, assignees and comments on PUBLIC repos, which is everything the
# shipped moves do. Private repos need the broader `repo` — an explicit
# opt-in via env, never the default, because `repo` grants read/write to
# every repository the user can see.
DEFAULT_OAUTH_SCOPE = "public_repo"


def oauth_provider() -> OAuthProvider:
    from packages.core.oauth import OAuthProvider

    return OAuthProvider(
        name="github",
        authorize_url=OAUTH_AUTHORIZE_URL,
        token_url=OAUTH_TOKEN_URL,
        client_id=os.getenv("GITHUB_CLIENT_ID", ""),
        client_secret=os.getenv("GITHUB_CLIENT_SECRET", ""),
        scope=os.getenv("GITHUB_OAUTH_SCOPE", DEFAULT_OAUTH_SCOPE),
    )


async def list_repos(token: str, limit: int = 100) -> list[dict]:
    """Repositories this token can see — so a human picks one from a list
    instead of typing `owner/name` and hoping."""
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Authorization": f"Bearer {token}",
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(
            f"{GITHUB_API}/user/repos",
            headers=headers,
            params={"per_page": str(min(limit, 100)), "sort": "updated", "affiliation": "owner,collaborator,organization_member"},
        )
        resp.raise_for_status()
        repos = resp.json()
    return [
        {
            "key": r["full_name"],
            "label": r["full_name"],
            "private": bool(r.get("private")),
            "updated_at": r.get("updated_at"),
        }
        for r in repos
    ]


class GitHubConnector:
    """Read-only GitHub connector (transport only — returns raw payloads).

    Token-aware, so it *acts differently* depending on GITHUB_TOKEN:
      - no token : anonymous public access, one shallow page (per_page<=30),
                   PR merge time approximated from closed_at.
      - token    : can read private repos, higher rate limit, deep pagination
                   up to `limit`, and each PR is enriched with its real
                   merged_at via the pulls API.
    """

    name = "github"

    def __init__(
        self,
        repo: str,
        token: str | None = None,
        limit: int = 30,
    ) -> None:
        self.repo = repo
        self.token = token or os.getenv("GITHUB_TOKEN") or None
        self.limit = limit

    @property
    def authenticated(self) -> bool:
        return bool(self.token)

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    async def _get(self, client: httpx.AsyncClient, url: str, params: dict | None = None):
        for attempt in range(4):
            resp = await client.get(url, headers=self._headers(), params=params)
            if resp.status_code == 403 and "rate limit" in resp.text.lower():
                await asyncio.sleep(2 * (attempt + 1))
                continue
            resp.raise_for_status()
            return resp
        resp.raise_for_status()
        return resp

    async def _tag(self, client: httpx.AsyncClient, raw: dict, slug: str) -> dict:
        """Attach the transport-only fields the profile mapping's `context`
        and `_merged_at` depend on — shared by fetch_raw and backfill so the
        two paths normalize identically."""
        raw["_repo"] = slug
        is_pr = "pull_request" in raw
        merged_at = raw.get("closed_at")
        if is_pr and self.authenticated:
            # enrich with the real merge time (extra call, token only)
            try:
                pr = await self._get(client, f"{GITHUB_API}/repos/{self.repo}/pulls/{raw['number']}")
                merged_at = pr.json().get("merged_at") or merged_at
            except httpx.HTTPError:
                pass
        raw["_merged_at"] = merged_at
        return raw

    async def fetch_raw(self) -> list[dict]:
        per_page = 100 if self.authenticated else 30
        max_pages = max(1, (self.limit + per_page - 1) // per_page) if self.authenticated else 1
        slug = repo_slug(self.repo)

        collected: list[dict] = []
        async with httpx.AsyncClient(timeout=25.0) as client:
            for page in range(1, max_pages + 1):
                resp = await self._get(
                    client,
                    f"{GITHUB_API}/repos/{self.repo}/issues",
                    {"state": "all", "per_page": str(per_page), "page": str(page), "sort": "updated"},
                )
                batch = resp.json()
                if not batch:
                    break
                for raw in batch:
                    collected.append(await self._tag(client, raw, slug))
                    if len(collected) >= self.limit:
                        return collected
        return collected

    async def backfill(self, since_days: int) -> list[dict]:
        """Walk full history, oldest boundary first: page through
        `sort=created&direction=desc` until an item's `created_at` falls
        before the cutoff, then stop — no need to keep paging past it.
        Polite: one page in flight at a time, with a pause between pages so
        this never competes with a live sync for the same rate-limit budget.
        """
        cutoff = datetime.now(UTC) - timedelta(days=since_days)
        per_page = 100 if self.authenticated else 30
        slug = repo_slug(self.repo)

        collected: list[dict] = []
        async with httpx.AsyncClient(timeout=25.0) as client:
            page = 1
            while True:
                resp = await self._get(
                    client,
                    f"{GITHUB_API}/repos/{self.repo}/issues",
                    {
                        "state": "all", "per_page": str(per_page), "page": str(page),
                        "sort": "created", "direction": "desc",
                    },
                )
                batch = resp.json()
                if not batch:
                    break
                reached_cutoff = False
                for raw in batch:
                    created = datetime.fromisoformat(str(raw["created_at"]).replace("Z", "+00:00"))
                    if created < cutoff:
                        reached_cutoff = True
                        break
                    collected.append(await self._tag(client, raw, slug))
                if reached_cutoff or len(batch) < per_page:
                    break
                page += 1
                await asyncio.sleep(1.0)  # polite pacing between pages
        return collected
