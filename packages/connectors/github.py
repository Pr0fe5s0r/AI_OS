from __future__ import annotations

import asyncio
import os

import httpx

GITHUB_API = "https://api.github.com"


def repo_slug(repo: str) -> str:
    return repo.split("/")[-1]


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
                    raw["_repo"] = slug
                    is_pr = "pull_request" in raw
                    merged_at = raw.get("closed_at")
                    if is_pr and self.authenticated:
                        # enrich with the real merge time (extra call, token only)
                        try:
                            pr = await self._get(
                                client, f"{GITHUB_API}/repos/{self.repo}/pulls/{raw['number']}"
                            )
                            merged_at = pr.json().get("merged_at") or merged_at
                        except httpx.HTTPError:
                            pass
                    raw["_merged_at"] = merged_at
                    collected.append(raw)
                    if len(collected) >= self.limit:
                        return collected
        return collected
