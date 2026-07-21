from __future__ import annotations

from datetime import UTC, datetime

from apps.common.analysis import params_for_move, requires_human
from packages.core.act import needs_approval
from packages.shared.schema import ActionDecision, Evidence, Situation
from tests.conftest import SOFTWARE_PROFILE

_POLICY = {"default_require_approval": False, "dry_run": True}
_REGISTRY = SOFTWARE_PROFILE.moves["registry"]
_AUTONOMY = SOFTWARE_PROFILE.moves["autonomy"]


def _sit(severity: str = "medium", url: str | None = "https://github.com/acme/web/issues/7") -> Situation:
    return Situation(
        id=f"untriaged_issue:{severity}",
        company_id="test",
        rule="untriaged_issue",
        severity=severity,
        title="Open issue with no triage",
        summary="s",
        evidence=[Evidence(event_id="gh-web-7", source="github",
                           timestamp=datetime(2026, 7, 10, tzinfo=UTC), excerpt="x", url=url)],
        created_at=datetime(2026, 7, 10, tzinfo=UTC),
    )


def _dec(action: str, confidence: float) -> ActionDecision:
    return ActionDecision(action=action, argument="bug", confidence=confidence, rationale="r")


# ------------------------- the core gate (pure) -------------------------


def test_low_risk_action_runs_without_a_human() -> None:
    assert needs_approval(_REGISTRY["apply_label"], _POLICY) is False


def test_risky_action_always_needs_a_human_even_when_confident() -> None:
    for risky in ("page_engineer", "comment_on_pr"):
        assert needs_approval(_REGISTRY[risky], _POLICY) is True


def test_force_approval_overrides_a_low_risk_action() -> None:
    assert needs_approval(_REGISTRY["apply_label"], {**_POLICY, "force_approval": True}) is True


def test_a_direct_human_click_is_its_own_approval() -> None:
    """UI buttons run in one step: asking the clicker to re-approve adds nothing."""
    for name in ("page_engineer", "comment_on_pr", "apply_label"):
        assert needs_approval(_REGISTRY[name], {**_POLICY, "pre_approved": True}) is False


def test_the_agent_can_never_open_a_duplicate_issue() -> None:
    """Every situation is born from an existing issue; a new one would collide.
    The AI comments on the source issue instead."""
    assert "create_ticket" not in _REGISTRY
    assert "create_ticket" not in _AUTONOMY["allowed_actions"]


# ---------------------- the profile's autonomy policy ----------------------


def test_agent_acts_alone_when_confident_on_a_normal_situation() -> None:
    assert requires_human(_sit("high"), _dec("apply_label", 0.9), _AUTONOMY) is False


def test_low_confidence_pulls_in_a_human() -> None:
    assert requires_human(_sit("medium"), _dec("apply_label", 0.4), _AUTONOMY) is True


def test_critical_severity_always_pulls_in_a_human() -> None:
    assert requires_human(_sit("critical"), _dec("apply_label", 0.99), _AUTONOMY) is True


def test_model_saying_escalate_pulls_in_a_human() -> None:
    assert requires_human(_sit("low"), _dec("escalate", 1.0), _AUTONOMY) is True


# ------------------------------ param mapping ------------------------------


def test_argument_maps_into_concrete_action_params() -> None:
    params = params_for_move(SOFTWARE_PROFILE, "apply_label", "bug", _sit())
    assert params is not None
    assert params["situation_id"] == "untriaged_issue:medium"
    assert params["repo"] == "acme/web" and params["number"] == "7"
    assert params["body"] == {"labels": ["bug"]}


def test_missing_target_forces_escalation_rather_than_a_bad_call() -> None:
    assert params_for_move(SOFTWARE_PROFILE, "apply_label", "bug", _sit(url=None)) is None
