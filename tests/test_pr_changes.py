from __future__ import annotations

import json

import httpx

from packages.connectors.base import content_extra_fields_for, extra_metadata_fields_for
from packages.connectors.github import (
    ADDITIONS_FIELD,
    CHANGE_SUMMARY_FIELD,
    CHANGED_FILES_FIELD,
    CHANGED_PATHS_FIELD,
    CHANGED_PATHS_IN_SUMMARY,
    COMMIT_SUMMARY_FIELD,
    COMMITS_FIELD,
    DELETIONS_FIELD,
    GitHubConnector,
)
from packages.core.discovery import _mapping_from
from packages.core.ingest import ingest

# What a pull request CHANGED, without holding anyone's code.
#
# `/pulls/{n}/files` returns a `patch` field on every entry by default and
# GitHub offers no way to suppress it, so "we know the paths, not the code" is
# not something the API provides — it is one function in the connector. And the
# pipeline stores the WHOLE raw payload in events.raw, so a mapping that reads
# only filenames would still have left every diff on disk and in the vector
# index. That is why the strip is tested harder than the feature.

PATCH = "@@ -1,3 +1,9 @@\n-secret_key = 'old'\n+secret_key = 'NEW-REAL-SECRET'\n"


def _files_payload(n: int = 2) -> list[dict]:
    return [
        {
            "filename": f"packages/core/mod{i}.py",
            "status": "modified",
            "additions": 10 + i,
            "deletions": i,
            "patch": PATCH,
            "blob_url": "https://github.com/acme/app/blob/abc/x.py",
        }
        for i in range(n)
    ]


COMMITS = [
    {"sha": "c90c44b" + "0" * 33, "commit": {
        "message": "Add README for PR testing", "author": {"name": "kar", "date": "2026-07-22T18:31:46Z"},
    }},
    {"sha": "70d0c65" + "0" * 33, "commit": {
        "message": "Update README with author details\n\nlonger body here",
        "author": {"name": "kar", "date": "2026-07-22T19:09:04Z"},
    }},
]


def _transport(
    files: list[dict], detail: dict | None = None, commits: list[dict] | None = None
) -> httpx.MockTransport:
    detail = detail or {"merged_at": None, "additions": 21, "deletions": 1, "changed_files": 2}
    commits = COMMITS if commits is None else commits

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/files"):
            return httpx.Response(200, json=files)
        if request.url.path.endswith("/commits"):
            return httpx.Response(200, json=commits)
        return httpx.Response(200, json=detail)

    return httpx.MockTransport(handler)


async def _tag_pr(transport: httpx.MockTransport, raw: dict) -> dict:
    """_tag takes the client as an argument (fetch_raw and backfill share one),
    so a mock transport goes straight in — no patching of the connector."""
    connector = GitHubConnector(repo="acme/app", token="t")
    async with httpx.AsyncClient(transport=transport) as client:
        return await connector._tag(client, raw, "acme/app")


def _raw_pr(number: int = 10) -> dict:
    return {
        "number": number,
        "title": "Add README",
        "body": "",
        "created_at": "2026-07-22T00:00:00Z",
        "html_url": f"https://github.com/acme/app/pull/{number}",
        "pull_request": {"url": "..."},
        "closed_at": None,
    }


async def test_no_diff_text_survives_anywhere_in_the_payload() -> None:
    """The whole point. events.raw persists this dict verbatim, so anything
    left here is on disk — and, via content, inside the vector index."""
    raw = await _tag_pr(_transport(_files_payload()), _raw_pr())

    serialized = json.dumps(raw)
    assert "patch" not in serialized
    assert "NEW-REAL-SECRET" not in serialized
    assert "@@" not in serialized


async def test_the_shape_of_the_change_does_survive() -> None:
    raw = await _tag_pr(_transport(_files_payload()), _raw_pr())

    assert raw[ADDITIONS_FIELD] == 21
    assert raw[DELETIONS_FIELD] == 1
    assert raw[CHANGED_FILES_FIELD] == 2
    assert [f["path"] for f in raw[CHANGED_PATHS_FIELD]] == [
        "packages/core/mod0.py", "packages/core/mod1.py",
    ]
    assert raw[CHANGED_PATHS_FIELD][0]["status"] == "modified"


async def test_the_summary_reads_as_a_topic_not_a_manifest() -> None:
    """This string is what gets embedded, so it has to describe the change in
    words rather than stringify a list of dicts into JSON noise."""
    raw = await _tag_pr(_transport(_files_payload()), _raw_pr())

    assert raw[CHANGE_SUMMARY_FIELD] == (
        "Changed 2 files (+21 -1): packages/core/mod0.py, packages/core/mod1.py"
    )


async def test_a_huge_pull_request_does_not_dump_every_path_into_the_summary() -> None:
    raw = await _tag_pr(_transport(_files_payload(40)), _raw_pr())

    summary = raw[CHANGE_SUMMARY_FIELD]
    assert summary.count(".py") == CHANGED_PATHS_IN_SUMMARY
    assert f"and {40 - CHANGED_PATHS_IN_SUMMARY} more" in summary
    # the structured list still holds them all — only the embedded string is cut
    assert len(raw[CHANGED_PATHS_FIELD]) == 40


async def test_a_description_less_pull_request_still_says_what_it_touched() -> None:
    """The reason for the feature: with an empty body this PR reached search as
    a bare title, matching nothing."""
    raw = await _tag_pr(_transport(_files_payload()), _raw_pr())
    mapping = _mapping_from(
        {
            "event_type": "issue", "id_template": "gh-{number}",
            "timestamp_field": "created_at", "title_field": "title",
            "body_field": "body", "url_field": "html_url",
        },
        "github",
    )
    event = ingest({"source": "github", "company_id": "test-prc", "mapping": mapping}, raw)

    assert "packages/core/mod0.py" in event.content
    assert "Add README" in event.content
    assert "NEW-REAL-SECRET" not in event.content
    assert event.metadata["_additions"] == 21


def test_an_issue_is_untouched_by_any_of_this() -> None:
    """Issues have no changed files, and the extra content slot must not leave
    them with a trailing separator or an empty stanza."""
    mapping = _mapping_from(
        {
            "event_type": "issue", "id_template": "gh-{number}",
            "timestamp_field": "created_at", "title_field": "title", "body_field": "body",
        },
        "github",
    )
    event = ingest(
        {"source": "github", "company_id": "test-prc", "mapping": mapping},
        {"number": 1, "title": "Broken login", "body": "It fails", "created_at": "2026-07-22T00:00:00Z"},
    )
    assert event.content.strip() == "Broken login\n\nIt fails"


async def test_the_commits_say_what_moved_after_the_pull_request_was_opened() -> None:
    """A PR is frozen at whatever its title claimed on day one. The second
    commit — "Update README with author details" — is the author stating in
    their own words that the work moved, and none of it reached us."""
    raw = await _tag_pr(_transport(_files_payload()), _raw_pr())

    assert [c["message"] for c in raw[COMMITS_FIELD]] == [
        "Add README for PR testing", "Update README with author details",
    ]
    assert raw[COMMIT_SUMMARY_FIELD] == (
        "Commits: Add README for PR testing; Update README with author details"
    )


async def test_a_commit_body_is_left_behind_with_the_diff() -> None:
    """Only the subject line. A commit body is written about the code and
    routinely quotes it — the same reason the patch does not come along."""
    raw = await _tag_pr(_transport(_files_payload()), _raw_pr())
    assert "longer body here" not in json.dumps(raw)


async def test_the_commit_messages_reach_the_content_that_gets_embedded() -> None:
    raw = await _tag_pr(_transport(_files_payload()), _raw_pr())
    mapping = _mapping_from(
        {
            "event_type": "issue", "id_template": "gh-{number}",
            "timestamp_field": "created_at", "title_field": "title", "body_field": "body",
        },
        "github",
    )
    event = ingest({"source": "github", "company_id": "test-prc", "mapping": mapping}, raw)
    assert "Update README with author details" in event.content


def test_the_connector_declares_these_rather_than_discovery_guessing() -> None:
    assert CHANGE_SUMMARY_FIELD in content_extra_fields_for("github")
    assert CHANGED_PATHS_FIELD in extra_metadata_fields_for("github")
    # a connector with no such concept declares nothing, and the mapping is
    # built exactly as it was before
    assert content_extra_fields_for("slack") == []
    assert extra_metadata_fields_for("slack") == []
