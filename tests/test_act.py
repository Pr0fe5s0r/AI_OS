from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import text

from apps.common.analysis import default_argument, params_for_move
from packages.core.act import _execute, act, open_action_id
from packages.core.db import Session
from packages.shared.schema import ActionRequest, Evidence, Situation
from tests.conftest import SOFTWARE_PROFILE

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


async def test_a_log_action_is_reported_as_recorded_not_executed() -> None:
    """A `log` move reaches no external system. Reporting "executed" with the
    template as the detail made a button labelled "page engineer" look like
    somebody had actually been paged — the runtime must not imply an effect
    it never had."""
    entry = {"kind": "log", "template": "PAGE about {repo}"}
    status, detail, result = await _execute(entry, _REQ, {"dry_run": True})

    assert status == "recorded", "a log move did not contact anything"
    assert "no external system was contacted" in detail
    assert result["external_effect"] is False
    assert result["logged"] == "PAGE about acme/web"  # the intent is still captured


def test_only_http_moves_claim_an_external_effect() -> None:
    """The Feed labels a move by whether it really reaches outside. Every
    `log` move in every shipped profile must be marked as having no external
    effect, so no button can imply something it does not do."""
    from tests.conftest import INVENTORY_PROFILE

    for profile in (SOFTWARE_PROFILE, INVENTORY_PROFILE):
        registry = profile.moves.get("registry", {}) or {}
        for name, entry in registry.items():
            external = entry.get("kind") == "http"
            if entry.get("kind") == "log":
                assert not external, f"{name} is a log move and must not claim an external effect"


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


_ESCALATE_REGISTRY = {"page_engineer": {"kind": "log", "approval_required": True, "template": "PAGE {situation_id}"}}


async def test_pending_approval_is_not_duplicated_across_repeated_passes() -> None:
    """The bug: the watcher engine re-runs every 5 min, so an unchanged
    escalation re-proposed each pass stacked up dozens of identical approval
    cards. A pending ask must be reused, never re-queued."""
    company = "test-act-dedupe"
    async with Session() as session:
        await session.execute(text("DELETE FROM actions WHERE company_id = :c"), {"c": company})
        await session.commit()

    def _req() -> ActionRequest:
        return ActionRequest(
            company_id=company, action="page_engineer",
            params={"situation_id": "broken_rhythm:m:gh-1", "argument": "wake up"},
            situation_id="broken_rhythm:m:gh-1", requested_by="ai",
        )

    async with Session() as session:
        first = await act({"session": session}, _req(), _ESCALATE_REGISTRY, {"dry_run": True})
        second = await act({"session": session}, _req(), _ESCALATE_REGISTRY, {"dry_run": True})
        third = await act({"session": session}, _req(), _ESCALATE_REGISTRY, {"dry_run": True})
        await session.commit()

    assert first.status == "pending_approval"
    assert second.id == first.id and third.id == first.id  # all reuse the one ask

    async with Session() as session:
        n = (
            await session.execute(
                text(
                    "SELECT count(*) FROM actions WHERE company_id = :c "
                    "AND situation_id = :s AND status = 'pending_approval'"
                ),
                {"c": company, "s": "broken_rhythm:m:gh-1"},
            )
        ).scalar_one()
    assert n == 1, "exactly one pending approval, not three"


async def test_open_action_id_finds_only_matching_status() -> None:
    company = "test-act-open"
    async with Session() as session:
        await session.execute(text("DELETE FROM actions WHERE company_id = :c"), {"c": company})
        req = ActionRequest(
            company_id=company, action="page_engineer", params={},
            situation_id="sit-1", requested_by="ai",
        )
        result = await act({"session": session}, req, _ESCALATE_REGISTRY, {"dry_run": True})
        await session.commit()
        assert result.status == "pending_approval"

        found = await open_action_id(session, company, "sit-1", "page_engineer", ("pending_approval",))
        assert found == result.id
        # a status the row doesn't have -> no match
        assert await open_action_id(session, company, "sit-1", "page_engineer", ("executed",)) is None
        # a different situation -> no match
        assert await open_action_id(session, company, "sit-2", "page_engineer", ("pending_approval",)) is None


async def test_a_ui_button_gets_the_params_its_action_needs() -> None:
    """The Flags buttons only know a situation. The profile's move spec fills
    in the rest — repo/number from the evidence URL, body from the template."""
    situation = _sit()
    async with Session() as session:
        argument = await default_argument(session, SOFTWARE_PROFILE, "comment_on_pr", situation)
    params = params_for_move(SOFTWARE_PROFILE, "comment_on_pr", argument, situation)
    assert params is not None
    assert params["repo"] == "karthikeyan846/Chatbot" and params["number"] == "4"
    # the comment lands ON the existing issue and carries the whole analysis
    comment = params["body"]["body"]
    assert "The bot replies badly." in comment
    assert "Investigate the model config." in comment


def test_a_create_move_falls_back_to_the_home_target() -> None:
    """Cross-app: a create move fired from a Slack incident has no GitHub URL to
    derive a repo from. Without a home target it can't build (safe); with the
    connected repo passed in, it opens the issue there."""
    from apps.common.analysis import params_for_move
    from packages.connectors.github import MOVES
    from packages.core.profile import Profile

    prof = Profile(company_id="x", moves={"registry": {"create_issue": MOVES["create_issue"]}})
    sit = Situation(
        id="triage:slack:1", company_id="x", rule="needs_triage", severity="high",
        title="bug", summary="s", created_at=datetime(2026, 7, 25, tzinfo=UTC),
        evidence=[Evidence(event_id="1", source="slack",
                           timestamp=datetime(2026, 7, 25, tzinfo=UTC),
                           excerpt="x", url="https://app.slack.com/client/T/C?msg=1")],
    )
    # a Slack URL resolves to no repo -> without a home target, no action is built
    assert params_for_move(prof, "create_issue", "Fix login", sit) is None
    # with the connected repo as the home target, it builds and lands there
    built = params_for_move(prof, "create_issue", "Fix login", sit,
                            default_target={"repo": "karthikeyan846/Chatbot"})
    assert built is not None and built["repo"] == "karthikeyan846/Chatbot"


def test_a_confident_autonomous_action_is_pre_approved_and_runs() -> None:
    """An action on the autonomy allow-list that clears the confidence bar is
    pre-approved BY that policy — it must execute, not sit in the queue. Before
    this, a confident allowlisted public action still hit the default
    require-approval gate and waited, the opposite of acting on its own."""
    from packages.core.act import needs_approval

    public_move = {"public": True}  # no explicit approval_required
    # the autonomous step, not deferring to a human, marks it pre_approved
    assert needs_approval(public_move, {"pre_approved": True, "allow_public_actions": True}) is False
    # deferring to a human (low confidence / escalate) still queues
    assert needs_approval(public_move, {"pre_approved": False, "force_approval": True}) is True
