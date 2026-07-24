from __future__ import annotations

from datetime import UTC, datetime

from apps.common.analysis import params_for_move
from packages.connectors.base import moves_for, record_kind_field_for
from packages.connectors.github import RECORD_KIND_FIELD
from packages.core.discovery import _mapping_from
from packages.core.ingest import ingest
from packages.core.profile import Profile, with_connector_moves
from packages.shared.schema import Evidence, Situation

# A pull request is not an issue.
#
# GitHub's /issues endpoint returns both, distinguished only by a `pull_request`
# key, and the induced mapping used to stamp ONE constant type on everything a
# source produced. That flattened PRs and issues into one indistinguishable
# pile: no way to filter them apart, and no way for a review action to know it
# had been pointed at something it cannot review.

PROPOSAL = {
    "event_type": "issue",
    "id_template": "gh-{number}",
    "timestamp_field": "created_at",
    "title_field": "title",
    "url_field": "html_url",
}


def _source_config() -> dict:
    return {
        "source": "github",
        "company_id": "test-prs",
        "mapping": _mapping_from(PROPOSAL, "github"),
        "context": {},
    }


def _raw(number: int, kind: str) -> dict:
    path = "pull" if kind == "pull_request" else "issues"
    return {
        "number": number,
        "title": f"record {number}",
        "created_at": "2026-07-10T00:00:00Z",
        "html_url": f"https://github.com/acme/app/{path}/{number}",
        RECORD_KIND_FIELD: kind,
    }


def test_the_connector_declares_which_field_names_the_record_kind() -> None:
    assert record_kind_field_for("github") == RECORD_KIND_FIELD
    # a connector returning one kind of thing declares nothing, and the mapping
    # falls back to the single induced name
    assert record_kind_field_for("slack") is None


def test_a_pull_request_and_an_issue_normalize_to_different_types() -> None:
    cfg = _source_config()
    assert ingest(cfg, _raw(1, "issue")).type == "issue"
    assert ingest(cfg, _raw(2, "pull_request")).type == "pull_request"


def test_a_payload_missing_the_discriminator_still_normalizes() -> None:
    """Old rows, a push source, or an API that stops sending the field — none
    of those should fail to ingest; they fall back to the induced name."""
    cfg = _source_config()
    raw = _raw(3, "issue")
    del raw[RECORD_KIND_FIELD]
    assert ingest(cfg, raw).type == "issue"


# --------------------------- review moves ---------------------------


def _situation(url: str) -> Situation:
    return Situation(
        id="sit-1",
        company_id="test-prs",
        rule="broken_rhythm",
        severity="high",
        title="stalled",
        summary="stalled",
        evidence=[
            Evidence(
                event_id="e1", source="github",
                timestamp=datetime(2026, 7, 10, tzinfo=UTC), excerpt="x", url=url,
            )
        ],
        status="open",
        created_at=datetime(2026, 7, 22, tzinfo=UTC),
    )


def _profile() -> Profile:
    # through with_connector_moves, exactly as load_profile builds it: the
    # registry AND the url->params targeting are both derived from the
    # connected sources, and a hand-built registry has no targets to match on
    return with_connector_moves(
        Profile(company_id="test-prs", sources=[{"source": "github", "kind": "connector"}])
    )


def test_a_review_move_is_offered_on_a_pull_request() -> None:
    params = params_for_move(
        _profile(), "approve_pull_request", "looks good",
        _situation("https://github.com/acme/app/pull/7"),
    )
    assert params is not None
    assert params["repo"] == "acme/app"
    assert params["number"] == "7"
    assert params["body"] == {"event": "APPROVE", "body": "looks good"}


def test_a_review_move_is_refused_on_an_issue() -> None:
    """The URL resolves to a perfectly good repo and number — the target is
    extractable, so nothing else in the chain would have stopped this. Without
    the check it reaches POST /pulls/7/reviews and 404s, but only after a human
    has already approved the action."""
    assert params_for_move(
        _profile(), "approve_pull_request", "looks good",
        _situation("https://github.com/acme/app/issues/7"),
    ) is None


def test_moves_that_work_on_both_are_still_offered_on_an_issue() -> None:
    """GitHub treats a PR as an issue for labels, assignees and comments. Only
    the review endpoints are PR-only, and over-restricting would quietly remove
    the actions that do work."""
    for move in ("comment_on_issue", "apply_label", "assign_issue"):
        assert params_for_move(
            _profile(), move, "bug", _situation("https://github.com/acme/app/issues/7")
        ) is not None, move


def test_only_the_review_moves_are_restricted_to_pull_requests() -> None:
    """The Feed reads `applies_to_url` off /api/actions/registry to decide which
    buttons a card may show, so this set IS the UI contract. Restricting a move
    that works on both would silently remove a working button; forgetting one
    puts back the bug where an issue card offered "request changes on pull
    request" — a button whose only possible outcome was a refusal."""
    restricted = {
        name for name, spec in moves_for("github").items() if spec.get("applies_to_url")
    }
    assert restricted == {"approve_pull_request", "request_changes_on_pull_request"}


def test_a_review_reaches_a_human_by_default_without_declaring_policy() -> None:
    """Approving is a person vouching for code, so it must not run unattended —
    but pinning approval_required on the connector would put POLICY in a
    capability declaration, which test_capability.py forbids. The guarantee
    comes from act()'s default instead: no policy declared means a human is
    asked. This test exists so that default can't be quietly inverted."""
    from packages.core.act import needs_approval

    for move in ("approve_pull_request", "request_changes_on_pull_request"):
        spec = moves_for("github")[move]
        assert "approval_required" not in spec, move
        assert spec["public"] is True, move
        assert needs_approval(spec, {}) is True, move
