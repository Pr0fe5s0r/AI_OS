from __future__ import annotations

from packages.core.briefing import evaluate_policy
from tests.conftest import SOFTWARE_PROFILE

BRIEFING_POLICY = SOFTWARE_PROFILE.vocabulary["briefing_policy"]


def _state(**stats) -> dict:
    base = {
        "events": 0, "active_situations": 0, "high_risk": 0, "pending_approvals": 0,
        "learned_norms": 0, "norm_metrics": 0, "sources_connected": 0,
    }
    base.update(stats)
    return {"stats": base}


def test_no_connection_asks_you_to_connect() -> None:
    assert evaluate_policy(_state(), BRIEFING_POLICY)["mode"] == "waiting_for_signal"


def test_connected_but_nothing_synced() -> None:
    v = evaluate_policy(_state(sources_connected=1), BRIEFING_POLICY)
    assert v["mode"] == "waiting_for_sync"


def test_pending_approval_outranks_an_empty_situation_queue() -> None:
    """The bug the duplicated frontend/backend logic produced: with a human
    decision blocking the loop but no active situations, the old backend said
    'monitoring - no active risks'. A blocked approval must always win."""
    v = evaluate_policy(
        _state(sources_connected=1, events=3, active_situations=0, pending_approvals=2),
        BRIEFING_POLICY,
    )
    assert v["mode"] == "needs_human"
    assert v["headline"] == "2 actions waiting for approval."


def test_high_risk_triage_and_singular_plural() -> None:
    v = evaluate_policy(
        _state(sources_connected=1, events=3, active_situations=1, high_risk=1),
        BRIEFING_POLICY,
    )
    assert v["mode"] == "triage_now"
    assert v["headline"] == "1 high-priority situation to triage."  # singular

    v = evaluate_policy(
        _state(sources_connected=1, events=3, active_situations=4, high_risk=0),
        BRIEFING_POLICY,
    )
    assert v["mode"] == "triage_queue"
    assert v["headline"] == "4 situations ready for triage."  # plural


def test_quiet_workspace_falls_through_to_the_default_rule() -> None:
    v = evaluate_policy(_state(sources_connected=1, events=9), BRIEFING_POLICY)
    assert v["mode"] == "monitoring"


def test_policy_default_rule_is_last_and_unconditional() -> None:
    assert BRIEFING_POLICY[-1]["when"] == []
    assert all(r.get("when") for r in BRIEFING_POLICY[:-1]), "only the last rule may be unconditional"
