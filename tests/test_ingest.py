from __future__ import annotations

from packages.connectors.github import GitHubConnector
from packages.core.ingest import ingest
from packages.shared.schema import Event
from verticals.software.config import _github_source_config

# Fixtures live here (not in the app) — no mock connector, no seed data. These
# are real GitHub REST payload shapes, so CI runs offline.

_SOURCE_CONFIG = _github_source_config("acme/web-store")

_ISSUE = {
    "number": 4821,
    "title": "Checkout returns 500 at payment step",
    "body": "Stripe charge created but order never marked paid.",
    "state": "open",
    "user": {"login": "maya", "id": 7},
    "labels": [{"name": "bug"}, {"name": "payments"}],
    "html_url": "https://github.com/acme/web-store/issues/4821",
    "created_at": "2026-07-09T14:22:00Z",
    "closed_at": None,
    "_repo": "web-store",
    "_merged_at": None,
}

_PR = {
    "number": 4830,
    "title": "Fix checkout 500",
    "body": "Wrap finalize in a transaction. Closes #4821.",
    "state": "closed",
    "user": {"login": "maya", "id": 7},
    "labels": [{"name": "fix"}],
    "html_url": "https://github.com/acme/web-store/pull/4830",
    "created_at": "2026-07-10T09:12:00Z",
    "closed_at": "2026-07-10T14:12:00Z",
    "pull_request": {"url": "..."},
    "_repo": "web-store",
    "_merged_at": "2026-07-10T14:12:00Z",
}


def test_ingest_normalizes_issue_into_event_schema() -> None:
    ev = ingest(_SOURCE_CONFIG, _ISSUE)
    assert isinstance(ev, Event)
    assert ev.id == "gh-web-store-4821"
    assert ev.source == "github"
    assert ev.type == "issue"
    assert ev.actor.name == "maya"
    assert "Checkout returns 500" in ev.content
    assert ev.metadata["number"] == 4821
    assert ev.metadata["state"] == "open"
    # the untouched source payload is preserved
    assert ev.raw["html_url"].endswith("/issues/4821")


def test_ingest_detects_pull_request_and_merge_time() -> None:
    ev = ingest(_SOURCE_CONFIG, _PR)
    assert ev.type == "pull_request"
    assert ev.id == "gh-web-store-4830"
    assert ev.metadata["merged_at"] == "2026-07-10T14:12:00Z"


def test_connector_is_token_aware(monkeypatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert GitHubConnector("acme/web-store", token=None).authenticated is False
    assert GitHubConnector("acme/web-store", token="ghp_x").authenticated is True
