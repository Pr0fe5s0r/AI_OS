from __future__ import annotations

from packages.core.norms import learn_norms


def test_learn_norms_returns_baseline() -> None:
    baseline = learn_norms(
        {"name": "ticket_resolution_hours", "unit": "hours", "window_days": 120},
        [2.0, 4.0, 6.0, 8.0, 30.0],
    )
    assert baseline.metric == "ticket_resolution_hours"
    assert baseline.unit == "hours"
    assert baseline.n == 5
    assert baseline.median == 6.0
    assert baseline.mean == 10.0
    assert baseline.std > 0


def test_learn_norms_handles_empty() -> None:
    baseline = learn_norms({"name": "pr_review_hours", "unit": "hours"}, [])
    assert baseline.n == 0
    assert baseline.median == 0.0
