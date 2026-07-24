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

# Which raw field says what KIND of record this is. Declared beside the code
# that knows the API, the same way MOVES and TARGET_PARAMS are: discovery reads
# `type` from here instead of freezing one constant for the whole source, so a
# repo's issues and pull requests stop arriving as the same thing.
RECORD_KIND_FIELD = "_record_kind"

# When a record last MOVED. GitHub stamps every issue and pull request with
# `updated_at` on any change — a new commit, an edit, a label, a comment — and
# that is the only witness we get to activity between two polls. Declared
# rather than left to discovery so an already-connected workspace picks it up
# without re-learning its profile; see resolve._last_activity for why the whole
# stall engine depends on it.
ACTIVITY_FIELD = "updated_at"

# What a pull request CHANGED, flattened out of the API's shape. `_change_summary`
# is the one that reaches `content` (and therefore the vector index); the rest
# are numbers a rhythm can learn "normal PR size" from. Never a line of code —
# see GitHubConnector._attach_changed_paths for why that is this file's job.
ADDITIONS_FIELD = "_additions"
DELETIONS_FIELD = "_deletions"
CHANGED_FILES_FIELD = "_changed_files"
CHANGED_PATHS_FIELD = "_changed_paths"
CHANGE_SUMMARY_FIELD = "_change_summary"
COMMITS_FIELD = "_commits"
COMMIT_SUMMARY_FIELD = "_commit_summary"
CHANGED_FILE_LIMIT = 100          # one page; a 300-file PR is a review problem, not a data one
CHANGED_PATHS_IN_SUMMARY = 12     # keep the embedded string about a topic, not a manifest
COMMIT_LIMIT = 50

# The conversation on a record. `_review_comment_count` is lifted off the PR
# detail response so we can skip the request entirely when there is nothing to
# read — the usual case, and the difference between one extra call per scan and
# one per record per scan.
THREAD_FIELD = "_thread"
THREAD_SUMMARY_FIELD = "_thread_summary"
REVIEW_COMMENT_COUNT_FIELD = "_review_comment_count"
THREAD_LIMIT = 100
THREAD_IN_SUMMARY = 8
COMMENT_CHARS = 400

# A pull request's own URL. GitHub spells it /pull/N while an issue is
# /issues/N, and the two are otherwise interchangeable across the issues API —
# so this is what separates a move that works on both from one that works only
# on a PR. Referenced by MOVES below via `applies_to_url`.
PULL_REQUEST_URL_PATTERN = r"github\.com/[^/]+/[^/]+/pull/\d+"


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


async def list_repos(token: str, limit: int = 500) -> list[dict]:
    """Repositories this token can see — so a human picks one from a list
    instead of typing `owner/name` and hoping.

    PAGES. `/user/repos` returns at most 100 per request, and this used to make
    a single call: anyone with more than 100 repositories simply never saw the
    rest, with nothing on screen to say the list was cut short. Keep asking
    until a short page comes back.

    A repo missing from this list despite existing is usually SCOPE, not
    paging: `public_repo` cannot see private repositories at all, so they are
    absent from the response rather than filtered out here. `visibility=all` is
    explicit so the intent is legible even though it is the API's default.
    """
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Authorization": f"Bearer {token}",
    }
    per_page = 100
    collected: list[dict] = []
    async with httpx.AsyncClient(timeout=20.0) as client:
        page = 1
        while len(collected) < limit:
            resp = await client.get(
                f"{GITHUB_API}/user/repos",
                headers=headers,
                params={
                    "per_page": str(per_page),
                    "page": str(page),
                    "sort": "updated",
                    "visibility": "all",
                    "affiliation": "owner,collaborator,organization_member",
                },
            )
            resp.raise_for_status()
            batch = resp.json()
            if not batch:
                break
            collected.extend(batch)
            if len(batch) < per_page:
                break
            page += 1
    return [
        {
            "key": r["full_name"],
            "label": r["full_name"],
            "private": bool(r.get("private")),
            "updated_at": r.get("updated_at"),
        }
        for r in collected[:limit]
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

    async def _attach_changed_paths(self, client: httpx.AsyncClient, raw: dict) -> None:
        """Which files a pull request touches — WITHOUT the code in them.

        `/pulls/{n}/files` returns a `patch` field on every entry by default,
        holding the real diff, and GitHub offers no parameter to suppress it.
        There is no paths-only endpoint. So the separation between "we know
        what changed" and "we hold your source code" is not something the API
        gives us — it is this function, and only this function.

        That matters more than it looks, because the pipeline stores the WHOLE
        raw payload in `events.raw`. A mapping that reads nothing but filenames
        would still have left every diff sitting in Postgres and, through the
        content field, inside the vector index. Stripping has to happen HERE,
        before the payload is handed on, or it does not happen at all.

        What survives is the shape of the change: paths, status, and per-file
        line counts. Enough to link a description-less PR to the issues about
        the same area; not enough to reconstruct a line of anyone's code.
        """
        try:
            resp = await self._get(
                client,
                f"{GITHUB_API}/repos/{self.repo}/pulls/{raw['number']}/files",
                {"per_page": str(CHANGED_FILE_LIMIT)},
            )
        except httpx.HTTPError:
            return  # size still landed; paths are an enrichment, not a promise

        files = [
            {
                "path": f.get("filename"),
                "status": f.get("status"),
                "additions": f.get("additions"),
                "deletions": f.get("deletions"),
            }
            for f in resp.json()[:CHANGED_FILE_LIMIT]
            if f.get("filename")
        ]
        if not files:
            return
        raw[CHANGED_PATHS_FIELD] = files

        # One flat string, because this is what gets read into `content` and
        # therefore embedded. A list of dicts stringifies into JSON noise that
        # searches badly; "Changed 7 files (+240 -12): a.py, b.tsx" reads well
        # to a person and embeds as the topic it actually is.
        shown = ", ".join(str(f["path"]) for f in files[:CHANGED_PATHS_IN_SUMMARY])
        more = len(files) - CHANGED_PATHS_IN_SUMMARY
        if more > 0:
            shown += f", and {more} more"
        count = raw.get(CHANGED_FILES_FIELD) or len(files)
        raw[CHANGE_SUMMARY_FIELD] = (
            f"Changed {count} file{'' if count == 1 else 's'} "
            f"(+{raw.get(ADDITIONS_FIELD) or 0} -{raw.get(DELETIONS_FIELD) or 0}): {shown}"
        )

    async def _attach_commits(self, client: httpx.AsyncClient, raw: dict) -> None:
        """The commit messages on a pull request — what the author SAID they
        did, in their own words.

        Without this a PR is frozen at whatever its title claimed when it was
        opened. A second commit called "Update README with author details" is
        the clearest possible statement that the work moved and how, and none
        of it reached us: the size counters ticked from +1 to +3 and the title
        never changed, so the product could show that *something* happened
        while being unable to say *what*.

        Messages, not diffs — the same line this connector draws everywhere
        else. A commit message is prose a person wrote to be read; the patch
        it refers to is their source code, and only the first belongs here.
        """
        try:
            resp = await self._get(
                client,
                f"{GITHUB_API}/repos/{self.repo}/pulls/{raw['number']}/commits",
                {"per_page": str(COMMIT_LIMIT)},
            )
        except httpx.HTTPError:
            return

        commits = []
        for c in resp.json()[:COMMIT_LIMIT]:
            commit = c.get("commit") or {}
            message = str(commit.get("message") or "").strip()
            if not message:
                continue
            commits.append({
                "sha": str(c.get("sha") or "")[:7],
                # first line only: the rest is a body that belongs to the diff
                "message": message.splitlines()[0][:200],
                "author": ((commit.get("author") or {}).get("name")) or "",
                "at": (commit.get("author") or {}).get("date"),
            })
        if not commits:
            return
        raw[COMMITS_FIELD] = commits
        raw[COMMIT_SUMMARY_FIELD] = "Commits: " + "; ".join(str(c["message"]) for c in commits)

    async def _attach_thread(self, client: httpx.AsyncClient, raw: dict, is_pr: bool) -> None:
        """The conversation on an issue or pull request — what people SAID.

        GitHub splits this across two endpoints, and using only one loses half
        the discussion:

          /issues/{n}/comments  the Conversation tab. Every pull request is
                                also an issue, so this is where the top-level
                                back-and-forth lives for both.
          /pulls/{n}/comments   review comments pinned to a line of code, with
                                `in_reply_to_id` threading a reply to its
                                parent (GitHub supports one level, no deeper).

        Skipped entirely when the record's own comment counters are zero, which
        is the common case — a repo of 200 quiet issues would otherwise cost
        200 requests a scan to learn nothing.

        Bodies are kept, unlike patches. A comment is prose a person wrote for
        other people to read, and it is usually the only place the actual
        reasoning about a piece of work exists — "we're blocked on the vendor",
        "this needs the migration first". Losing it would leave the agent able
        to say a thing is stalled while never knowing why.
        """
        thread: list[dict] = []

        if int(raw.get("comments") or 0) > 0:
            thread += await self._fetch_comments(
                client, f"{GITHUB_API}/repos/{self.repo}/issues/{raw['number']}/comments", "comment"
            )
        if is_pr and int(raw.get(REVIEW_COMMENT_COUNT_FIELD) or 0) > 0:
            thread += await self._fetch_comments(
                client, f"{GITHUB_API}/repos/{self.repo}/pulls/{raw['number']}/comments", "review_comment"
            )

        if not thread:
            return
        thread.sort(key=lambda c: str(c.get("at") or ""))
        raw[THREAD_FIELD] = thread[:THREAD_LIMIT]
        raw[THREAD_SUMMARY_FIELD] = "Discussion: " + " | ".join(
            f"{c['author']}: {c['body']}" for c in thread[:THREAD_IN_SUMMARY]
        )

    async def _fetch_comments(
        self, client: httpx.AsyncClient, url: str, kind: str
    ) -> list[dict]:
        try:
            resp = await self._get(client, url, {"per_page": str(THREAD_LIMIT)})
        except httpx.HTTPError:
            return []  # a thread we could not read must not lose the record
        out = []
        for c in resp.json()[:THREAD_LIMIT]:
            body = str(c.get("body") or "").strip()
            if not body:
                continue
            out.append({
                "kind": kind,
                "author": (c.get("user") or {}).get("login") or "",
                "at": c.get("created_at"),
                "body": body[:COMMENT_CHARS],
                # where a review comment is pinned, and what it answers — the
                # difference between "somebody commented" and "somebody replied
                # about this line of this file"
                "path": c.get("path"),
                "line": c.get("line"),
                "in_reply_to": c.get("in_reply_to_id"),
            })
        return out

    async def _tag(self, client: httpx.AsyncClient, raw: dict, slug: str) -> dict:
        """Attach the transport-only fields the profile mapping's `context`
        and `_merged_at` depend on — shared by fetch_raw and backfill so the
        two paths normalize identically."""
        raw["_repo"] = slug
        is_pr = "pull_request" in raw
        # `/issues` returns pull requests too, distinguished only by the
        # presence of a `pull_request` key — a GitHub API quirk, so recognising
        # it belongs here rather than in a mapping someone (or discovery) has
        # to infer. Flattened into a plain value the declarative mapping can
        # read straight into `type`, which is what makes a PR separable from an
        # issue everywhere downstream: facets, watchers, and which moves apply.
        raw[RECORD_KIND_FIELD] = "pull_request" if is_pr else "issue"
        merged_at = raw.get("closed_at")
        if is_pr and self.authenticated:
            # enrich with the real merge time (extra call, token only)
            try:
                pr = await self._get(client, f"{GITHUB_API}/repos/{self.repo}/pulls/{raw['number']}")
                detail = pr.json()
                merged_at = detail.get("merged_at") or merged_at
                # Size comes free: this response already carries it and we were
                # throwing it away. A pull request whose description is empty
                # still says a great deal by its shape.
                raw[ADDITIONS_FIELD] = detail.get("additions")
                raw[DELETIONS_FIELD] = detail.get("deletions")
                raw[CHANGED_FILES_FIELD] = detail.get("changed_files")
                # lifted so _attach_thread can skip the request when there is
                # nothing pinned to a line of code — which is most of the time
                raw[REVIEW_COMMENT_COUNT_FIELD] = detail.get("review_comments")
                await self._attach_changed_paths(client, raw)
                await self._attach_commits(client, raw)
            except httpx.HTTPError:
                pass
        raw["_merged_at"] = merged_at
        # Outside the PR-only block: an ISSUE's discussion matters just as much,
        # and its comment count comes on the payload we already have. Needs a
        # token, like every other enrichment here.
        if self.authenticated:
            await self._attach_thread(client, raw, is_pr)
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


# --------------------------- declared capability ---------------------------
# What this connector can DO, declared beside the code that speaks the API.
#
# This is the honest home for an action registry. An action's HTTP shape can
# never be learned from event data — a payload tells you a repo uses a "bug"
# label, never that POST /repos/{repo}/issues/{n}/labels is the call. That
# knowledge only exists in code that already talks to GitHub, so it lives
# here rather than in a per-company config file someone has to author.
#
# Capability only. Everything situational — whether a move may run
# unattended, which label to apply, who to assign — is NOT here: it comes
# from what the engine learns and what the operator tells the agent.
#
# `argument` is the description the planner and the agent see; `params` is the
# request body template, filled from the situation's target. `target:
# github_issue` means "needs {repo} and {number}", extracted from an evidence
# URL by TARGET_URL_PATTERN below.

MOVES: dict[str, dict] = {
    "apply_label": {
        "kind": "http",
        "method": "POST",
        "url": f"{GITHUB_API}/repos/{{repo}}/issues/{{number}}/labels",
        "auth": {"source": "github"},
        "argument": "ONE GitHub label name",
        "params": {"body": {"labels": ["{argument}"]}, "target": "github_issue"},
        "reversible": True,
        # Where this argument's real vocabulary lives in a normalized event.
        # GitHub returns labels as objects, so the readable name is under
        # "name" — wire-format knowledge, which is why it sits here. The
        # engine reads the values out of ingested events; nothing declares
        # a default. Applying a label a repo has never used would silently
        # CREATE it, so the only safe source of truth is what's really there.
        "argument_values": {"field": "labels", "name_key": "name"},
    },
    "assign_issue": {
        "kind": "http",
        "method": "POST",
        "url": f"{GITHUB_API}/repos/{{repo}}/issues/{{number}}/assignees",
        "auth": {"source": "github"},
        "argument": "ONE GitHub username to assign",
        "params": {"body": {"assignees": ["{argument}"]}, "target": "github_issue"},
        "reversible": True,
        "argument_values": {"field": "assignee"},
    },
    "comment_on_issue": {
        "kind": "http",
        "method": "POST",
        "url": f"{GITHUB_API}/repos/{{repo}}/issues/{{number}}/comments",
        "auth": {"source": "github"},
        "argument": "comment text posted on the EXISTING issue or PR",
        "params": {"body": {"body": "{argument}"}, "target": "github_issue"},
        # writes where teammates and customers read it — never silently
        "public": True,
    },
    "close_issue": {
        "kind": "http",
        "method": "PATCH",
        "url": f"{GITHUB_API}/repos/{{repo}}/issues/{{number}}",
        "auth": {"source": "github"},
        "params": {"body": {"state": "closed"}, "target": "github_issue"},
        "reversible": True,
    },
    "create_issue": {
        "kind": "http",
        "method": "POST",
        "url": f"{GITHUB_API}/repos/{{repo}}/issues",
        "auth": {"source": "github"},
        "argument": "the title of the issue to open",
        # no `target` — creating needs a repo but no existing item number
        "params": {"body": {"title": "{argument}"}, "target": "github_repo"},
        "public": True,
    },
    # --- pull requests only -------------------------------------------------
    # `applies_to_url` is the reason these can exist at all. Every move above
    # works on a PR too, because GitHub treats one as an issue for labels,
    # assignees and comments — but /pulls/{n}/reviews does NOT, and offering
    # "approve" on a plain issue would 404 only AFTER a human had approved it.
    #
    # Expressed as a pattern over the target URL rather than a record type, for
    # the same reason TARGET_URL_PATTERN is: the engine already has to resolve
    # an evidence URL before it can build these params at all, so the check
    # costs nothing and stays true no matter which watcher raised the situation
    # — including the graph ones, which carry a Thing and never an event type.
    "request_changes_on_pull_request": {
        "kind": "http",
        "method": "POST",
        "url": f"{GITHUB_API}/repos/{{repo}}/pulls/{{number}}/reviews",
        "auth": {"source": "github"},
        "applies_to_url": PULL_REQUEST_URL_PATTERN,
        "argument": "what must change before this can merge, in review-comment form",
        "params": {
            "body": {"event": "REQUEST_CHANGES", "body": "{argument}"},
            "target": "github_issue",
        },
        # a review is visible to the author and everyone watching, and it BLOCKS
        # the merge — public, and not something to undo by deleting
        "public": True,
        # How a line-anchored finding becomes ONE inline comment in the review's
        # `comments` array. Declared as DATA so the engine that assembles a PR
        # review (apps.common.analysis.assemble_pr_review) names none of these
        # GitHub keys itself: `path`+`line`+`side` is the reviews-API shape for
        # a comment pinned to a line of the diff's new side.
        "review_comment": {"path_key": "path", "line_key": "line", "side": "RIGHT", "body_key": "body"},
    },
    "approve_pull_request": {
        "kind": "http",
        "method": "POST",
        "url": f"{GITHUB_API}/repos/{{repo}}/pulls/{{number}}/reviews",
        "auth": {"source": "github"},
        "applies_to_url": PULL_REQUEST_URL_PATTERN,
        "argument": "the note to leave with the approval",
        "params": {"body": {"event": "APPROVE", "body": "{argument}"}, "target": "github_issue"},
        # An approval is a person vouching for code, and on a protected branch
        # it unblocks a merge. Every instinct says to pin approval_required
        # here — but that is POLICY, and a connector declares only capability
        # (tests/test_capability.py enforces exactly this). It reaches a human
        # anyway: act() requires approval unless a policy says otherwise, and
        # autonomy only ever acts alone on moves an operator listed by name.
        "public": True,
    },
}

# How to turn an evidence URL back into this connector's action params.
# Positional capture groups map onto TARGET_PARAMS.
TARGET_URL_PATTERN = r"github\.com/([^/]+/[^/]+)/(?:issues|pull)/(\d+)"
TARGET_PARAMS = ["repo", "number"]


REVIEW_DIFF_BUDGET = 8000      # chars of real diff handed to a reviewer, total
REVIEW_PATCH_PER_FILE = 2500   # cap one file's hunk so a big file can't crowd the rest


async def fetch_review_content(repo: str, number: str, token: str) -> str:
    """The actual diff of a pull request, fetched fresh AT REVIEW TIME and never
    stored — the deliberate counterpart to _attach_changed_paths, which keeps
    code OUT of the event store.

    A reviewer has to read the code to judge it, but holding that code in
    Postgres and the vector index is exactly what we refuse. Both are true at
    once only because the patch lives for the duration of a single review call,
    here, and is handed to the model and dropped — never written back.

    Returns a compact `path` + hunk digest (the `@@` headers give the model real
    line numbers to anchor findings on), capped so a giant PR cannot blow the
    context. Empty string on any failure — grounding then degrades to the change
    summary rather than aborting the review.
    """
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(
                f"{GITHUB_API}/repos/{repo}/pulls/{number}/files",
                headers=headers,
                params={"per_page": str(CHANGED_FILE_LIMIT)},
            )
            resp.raise_for_status()
            files = resp.json()
    except httpx.HTTPError:
        return ""

    parts: list[str] = []
    used = 0
    for f in files:
        patch, path = f.get("patch"), f.get("filename")
        if not patch or not path:
            continue  # binary files and the like carry no textual hunk
        block = (
            f"### {path} ({f.get('status')}, "
            f"+{f.get('additions', 0)} -{f.get('deletions', 0)})\n"
            f"{patch[:REVIEW_PATCH_PER_FILE]}"
        )
        if used + len(block) > REVIEW_DIFF_BUDGET:
            break
        parts.append(block)
        used += len(block)
    return "\n\n".join(parts)


# --------------------------- declared reviewers ----------------------------
# A grounded specialist fan-out over one open pull request — the blog's PR
# reviewer, expressed as DATA so the engine that runs it (core.review) names no
# concern. Declared here, beside the code that knows a GitHub PR carries a diff,
# a commit list and a review thread, the same read-time way MOVES is: connect
# the repo and the reviewer exists; disconnect it and the reviewer goes away
# (see profile.with_connector_reviewers). A stored profile may override any
# entry by key.
#
# `select.thing_type` is the normalized record kind (`pull_request`, the value
# RECORD_KIND_FIELD flattens PRs to); `where.status` is matched through the
# profile's status_field, so this never hardcodes GitHub's "state"/"open".
# `ground.attach` names the already-ingested summary fields folded into the
# model's context; `ground.retrieve.top_k` tunes how many hybrid-search
# neighbours ground it.
#
# `post` keeps the autonomy gate CLOSED (min_confidence above any real score,
# every critical to a human): until review quality is proven, findings surface
# in the product but nothing posts to a real repo. `request_changes_on_pull_request`
# is the move a future posting step would act through.
REVIEWERS: list[dict] = [
    {
        "key": "pull_request_review",
        "select": {"thing_type": "pull_request", "where": {"status": "open"}},
        "ground": {
            "attach": [CHANGE_SUMMARY_FIELD, COMMIT_SUMMARY_FIELD, THREAD_SUMMARY_FIELD],
            "retrieve": {"top_k": 6},
        },
        "concerns": [
            {
                "key": "security",
                "severity_ceiling": "critical",
                "prompt": (
                    "You are a security reviewer. In THIS change only, find "
                    "injection, hardcoded secrets, auth/authorization bypasses, "
                    "unsafe deserialization, SSRF, path traversal, and missing "
                    "input validation. Flag only defects the diff actually "
                    "introduces or newly exposes — not pre-existing code it "
                    "merely sits near."
                ),
            },
            {
                "key": "quality",
                "severity_ceiling": "high",
                "prompt": (
                    "You are a code-quality reviewer. In THIS change, find logic "
                    "errors, unhandled edge cases (null/empty/overflow), resource "
                    "leaks, race conditions, and needless complexity. Ignore "
                    "formatting and style a linter would catch."
                ),
            },
            {
                "key": "tests",
                "severity_ceiling": "high",
                "prompt": (
                    "You are a test-coverage reviewer. Identify behaviour this "
                    "change adds or alters that has no accompanying test, and name "
                    "the specific case that is missing. Do not ask for tests on "
                    "unchanged code."
                ),
            },
            {
                "key": "docs",
                "severity_ceiling": "low",
                "prompt": (
                    "You are a documentation reviewer. Flag new public functions, "
                    "APIs or flags introduced here without docs, and comments or "
                    "docstrings this change makes wrong. Never exceed low severity."
                ),
            },
        ],
        "post": {
            "move": "request_changes_on_pull_request",
            "autonomy": {"min_confidence": 1.01, "critical_always_human": True},
        },
    }
]
