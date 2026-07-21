from __future__ import annotations

from datetime import UTC, datetime, timedelta

from packages.core.detect import _match, _norm_fires
from packages.shared.schema import NormBaseline

_NOW = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)

_EVENT = {
    "id": "gh-repo-1",
    "timestamp": _NOW - timedelta(days=45),
    "metadata": {
        "state": "open",
        "assignee": None,
        "labels": [{"name": "bug"}, {"name": "p0"}],
    },
}


def test_predicates_drive_rules_from_data() -> None:
    assert _match(_EVENT, {"field": "metadata.state", "op": "eq", "value": "open"}, _NOW)
    assert _match(_EVENT, {"field": "metadata.assignee", "op": "is_null"}, _NOW)
    assert _match(
        _EVENT, {"field": "metadata.labels", "op": "contains_any", "value": ["p0", "sev1"]}, _NOW
    )
    assert _match(_EVENT, {"field": "timestamp", "op": "older_than_days", "value": 30}, _NOW)
    # negative cases
    assert not _match(_EVENT, {"field": "metadata.state", "op": "eq", "value": "closed"}, _NOW)
    assert not _match(
        _EVENT, {"field": "metadata.labels", "op": "contains_any", "value": ["docs"]}, _NOW
    )


def _norm(median: float, std: float, n: int) -> NormBaseline:
    return NormBaseline(
        company_id="default", metric="issue_resolution_hours", unit="hours",
        n=n, median=median, mean=median, std=std, window_days=90, computed_at=_NOW,
    )


def test_norm_gate_needs_a_baseline() -> None:
    rule = {"norm": {"metric": "issue_resolution_hours", "k": 2.0}}
    # 45 days old = 1080h. median 24 + 2*10 = 44h -> fires
    assert _norm_fires(_EVENT, rule, [_norm(24, 10, 5)], _NOW)
    # no observations -> must NOT fire (nothing learned yet)
    assert not _norm_fires(_EVENT, rule, [_norm(0, 0, 0)], _NOW)
    # no matching metric -> must NOT fire
    assert not _norm_fires(_EVENT, rule, [], _NOW)


def test_rule_without_norm_always_passes_the_gate() -> None:
    assert _norm_fires(_EVENT, {}, [], _NOW)


def _matches(rule_name: str, event: dict) -> bool:
    from tests.conftest import SOFTWARE_PROFILE

    rule = next(r for r in SOFTWARE_PROFILE.watchers if r["name"] == rule_name)
    return all(_match(event, p, _NOW) for p in rule["select"]["where"])


def test_needs_owner_and_untriaged_issue_never_fire_on_the_same_issue() -> None:
    """Both firing means the project manager gets two emails for one problem."""
    unowned = {"timestamp": _NOW, "metadata": {"state": "open", "assignee": None, "labels": []}}
    owned_unlabelled = {"timestamp": _NOW, "metadata": {"state": "open", "assignee": "sam", "labels": []}}
    owned_labelled = {"timestamp": _NOW, "metadata": {"state": "open", "assignee": "sam",
                                                      "labels": [{"name": "bug"}]}}

    assert _matches("needs_owner", unowned)
    assert not _matches("untriaged_issue", unowned), "unowned work belongs to needs_owner alone"

    assert not _matches("needs_owner", owned_unlabelled)
    assert _matches("untriaged_issue", owned_unlabelled)

    assert not _matches("needs_owner", owned_labelled)
    assert not _matches("untriaged_issue", owned_labelled)
