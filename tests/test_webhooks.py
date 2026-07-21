from __future__ import annotations

import hashlib
import hmac

from apps.common.context import build_source_config
from apps.common.webhooks import _analysis_job_id
from packages.connectors.github import webhook_raws as github_webhook_raws
from packages.core.ingest import ingest
from packages.core.webhooks import verify_signature
from tests.conftest import SOFTWARE_PROFILE

_GITHUB_DEF = next(s for s in SOFTWARE_PROFILE.sources if s["source"] == "github")


def _github_source_config(repo: str, company_id: str = "default") -> dict:
    cfg = build_source_config(SOFTWARE_PROFILE, _GITHUB_DEF, {"repo": repo})
    return {**cfg, "company_id": company_id}

# --------------------------- signature (core) ---------------------------

_SECRET = "s3cret"
_BODY = b'{"action":"opened"}'


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_a_correctly_signed_body_verifies() -> None:
    assert verify_signature(_SECRET, _BODY, _sign(_SECRET, _BODY)) is True


def test_the_wrong_secret_is_rejected() -> None:
    assert verify_signature(_SECRET, _BODY, _sign("guess", _BODY)) is False


def test_a_tampered_body_is_rejected() -> None:
    assert verify_signature(_SECRET, b'{"action":"closed"}', _sign(_SECRET, _BODY)) is False


def test_missing_or_malformed_signatures_are_rejected() -> None:
    assert verify_signature(_SECRET, _BODY, None) is False
    assert verify_signature(_SECRET, _BODY, "") is False
    assert verify_signature(_SECRET, _BODY, "sha1=abc") is False
    assert verify_signature("", _BODY, _sign("", _BODY)) is False  # no secret -> never


# ------------------------ payload translation (vertical) ------------------------


def _issue_payload(action: str = "opened") -> dict:
    return {
        "action": action,
        "issue": {
            "number": 12,
            "title": "Login button crashes",
            "body": "Tapping login crashes the app.",
            "state": "open",
            "user": {"login": "reporter1"},
            "labels": [],
            "assignee": None,
            "comments": 0,
            "html_url": "https://github.com/karthikeyan846/Chatbot/issues/12",
            "created_at": "2026-07-10T09:00:00Z",
            "updated_at": "2026-07-10T09:00:00Z",
            "closed_at": None,
        },
        "repository": {"name": "Chatbot", "full_name": "karthikeyan846/Chatbot"},
    }


def test_an_issue_webhook_becomes_the_same_event_the_poller_produces() -> None:
    """The push path and the poll path must land in the store identically —
    same id, so a webhook update overwrites the polled row, never duplicates."""
    raws = github_webhook_raws("issues", _issue_payload())
    assert len(raws) == 1
    event = ingest(_github_source_config("karthikeyan846/Chatbot", "test-wh"), raws[0])
    assert event.id == "gh-Chatbot-12"
    assert event.type == "issue"
    assert event.metadata["state"] == "open"
    assert event.metadata["assignee"] is None
    assert "Login button crashes" in event.content


def test_a_pull_request_webhook_is_typed_as_a_pull_request() -> None:
    payload = {
        "action": "closed",
        "pull_request": {
            "number": 5,
            "title": "Fix login crash",
            "body": "",
            "state": "closed",
            "user": {"login": "dev1"},
            "html_url": "https://github.com/karthikeyan846/Chatbot/pull/5",
            "created_at": "2026-07-09T10:00:00Z",
            "closed_at": "2026-07-10T10:00:00Z",
            "merged_at": "2026-07-10T10:00:00Z",
        },
        "repository": {"name": "Chatbot", "full_name": "karthikeyan846/Chatbot"},
    }
    raws = github_webhook_raws("pull_request", payload)
    event = ingest(_github_source_config("karthikeyan846/Chatbot", "test-wh"), raws[0])
    assert event.type == "pull_request"
    assert event.metadata["merged_at"] == "2026-07-10T10:00:00Z"


def test_unsupported_events_are_ignored_not_crashed() -> None:
    assert github_webhook_raws("watch", {"repository": {"name": "x"}}) == []
    assert github_webhook_raws("issues", {"repository": {"name": "x"}}) == []  # no issue key


def test_a_burst_of_webhooks_shares_one_analysis_job() -> None:
    """Label + assign + comment within the window must trigger ONE detection
    pass — the job id is the debounce."""
    assert _analysis_job_id("default", now=100.0) == _analysis_job_id("default", now=119.0)
    assert _analysis_job_id("default", now=100.0) != _analysis_job_id("default", now=121.0)
    # companies never share a debounce bucket
    assert _analysis_job_id("a", now=100.0) != _analysis_job_id("b", now=100.0)
