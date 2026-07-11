from __future__ import annotations

from datetime import UTC, datetime

from packages.core.act import _execute
from packages.shared.schema import ActionRequest, Evidence, Situation
from verticals.software.alerting import _default_argument, _params_for

_REQ = ActionRequest(
    company_id="test-act",
    action="comment_on_pr",
    params={"repo": "acme/web", "number": 7, "body": {"body": "hi"}},
)


async def test_http_action_is_not_sent_when_dry_run() -> None:
    entry = {
        "kind": "http",
        "method": "POST",
        "url": "https://api.github.com/repos/{repo}/issues/{number}/comments",
    }
    status, detail, result = await _execute(entry, _REQ, {"dry_run": True})
    assert status == "dry_run"
    assert "not sent" in detail
    # it records exactly what it WOULD have sent
    assert result["would_send"]["url"].endswith("/repos/acme/web/issues/7/comments")
    assert result["would_send"]["body"] == {"body": "hi"}


async def test_log_action_executes() -> None:
    entry = {"kind": "log", "template": "PAGE about {repo}"}
    status, detail, result = await _execute(entry, _REQ, {"dry_run": True})
    assert status == "executed"
    assert detail == "PAGE about acme/web"
    assert result["logged"] == "PAGE about acme/web"


async def test_unknown_kind_fails_cleanly() -> None:
    status, detail, _ = await _execute({"kind": "carrier_pigeon"}, _REQ, {})
    assert status == "failed"
    assert "carrier_pigeon" in detail


async def test_a_missing_http_param_fails_instead_of_raising() -> None:
    """A caller that forgets `repo` must get a failed action, never a 500."""
    bare = ActionRequest(company_id="test-act", action="comment_on_pr",
                         params={"situation_id": "needs_owner:gh-1"})
    entry = {"kind": "http", "method": "POST",
             "url": "https://api.github.com/repos/{repo}/issues/{number}/comments"}
    status, detail, _ = await _execute(entry, bare, {"dry_run": True})
    assert status == "failed"
    assert "repo" in detail


async def test_a_missing_log_param_fails_instead_of_raising() -> None:
    bare = ActionRequest(company_id="test-act", action="page_engineer", params={})
    status, detail, _ = await _execute(
        {"kind": "log", "template": "PAGE about {situation_id}"}, bare, {"dry_run": True}
    )
    assert status == "failed"
    assert "situation_id" in detail


def _sit() -> Situation:
    return Situation(
        id="needs_owner:gh-4", company_id="test-act", rule="needs_owner", severity="high",
        title="AI response is not working well", summary="The bot replies badly.",
        recommended_action="Investigate the model config.",
        evidence=[Evidence(event_id="gh-4", source="github",
                           timestamp=datetime(2026, 7, 10, tzinfo=UTC), excerpt="x",
                           url="https://github.com/karthikeyan846/Chatbot/issues/4")],
        created_at=datetime(2026, 7, 10, tzinfo=UTC),
    )


def test_a_ui_button_gets_the_params_its_action_needs() -> None:
    """The Flags buttons only know a situation. The vertical must fill in the rest."""
    situation = _sit()
    params = _params_for("comment_on_pr", _default_argument("comment_on_pr", situation), situation)
    assert params is not None
    assert params["repo"] == "karthikeyan846/Chatbot" and params["number"] == "4"
    # the comment lands ON the existing issue and carries the whole analysis
    comment = params["body"]["body"]
    assert "The bot replies badly." in comment
    assert "Investigate the model config." in comment
