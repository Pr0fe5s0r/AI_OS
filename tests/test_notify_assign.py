from __future__ import annotations

from datetime import UTC, datetime

from packages.core.deliver import _render, _route, deliver
from packages.shared.schema import Brief, Evidence
from verticals.software.alerting import free_engineers
from verticals.software.config import NOTIFY_ROLES, recipients_for, routing_config

_ROSTER = [
    {"id": "alice", "name": "Alice", "email": "alice@x.com", "roles": ["engineer"],
     "skills": ["python"], "max_open_issues": 2, "assignable": True},
    {"id": "bob", "name": "Bob", "email": "bob@x.com", "roles": ["team_lead", "engineer"],
     "skills": ["infra"], "max_open_issues": 3, "assignable": True},
    {"id": "cara", "name": "Cara", "email": "cara@x.com", "roles": ["project_manager"],
     "skills": [], "max_open_issues": 5, "assignable": True},
    {"id": "dan", "name": "Dan", "email": "dan@x.com", "roles": ["engineer"],
     "skills": [], "max_open_issues": 3, "assignable": False},
]


def _brief(severity: str = "high", assigned: str | None = "alice") -> Brief:
    return Brief(
        situation_id="untriaged_issue:gh-1",
        title="Open issue with no triage",
        severity=severity,
        summary="The API is down.",
        recommended_action="Label it bug.",
        assigned_to=assigned,
        evidence=[Evidence(event_id="gh-1", source="github",
                           timestamp=datetime(2026, 7, 10, tzinfo=UTC), excerpt="x",
                           url="https://github.com/a/b/issues/1")],
        citations=["github:gh-1 <https://github.com/a/b/issues/1>"],
    )


# ------------------------------- availability -------------------------------


def test_only_free_assignable_engineers_are_offered() -> None:
    workload = {"alice": 2, "bob": 1}  # alice is at capacity (2/2)
    free = {m["id"] for m in free_engineers(_ROSTER, workload)}
    assert free == {"bob"}, "alice is full, cara is not an engineer, dan is not assignable"


def test_an_idle_engineer_is_offered_with_their_workload() -> None:
    free = free_engineers(_ROSTER, {})
    assert {m["id"] for m in free} == {"alice", "bob"}
    assert next(m for m in free if m["id"] == "alice")["open_issues"] == 0


def test_nobody_free_means_no_candidates() -> None:
    assert free_engineers(_ROSTER, {"alice": 2, "bob": 3}) == []


# -------------------------------- notification --------------------------------


def test_pm_and_team_lead_are_the_recipients() -> None:
    to = recipients_for(NOTIFY_ROLES, _ROSTER)
    assert set(to) == {"bob@x.com", "cara@x.com"}, "team lead + PM, not plain engineers"


def test_every_severity_routes_to_email() -> None:
    cfg = routing_config(dry_run=True, roster=_ROSTER)
    for severity in ("critical", "high", "medium", "low"):
        channel, recipients = _route(severity, cfg)
        assert channel == "email"
        assert recipients


def test_an_empty_roster_degrades_to_console_instead_of_dropping_the_brief() -> None:
    cfg = routing_config(dry_run=True, roster=[])
    assert _route("high", cfg)[0] == "console"


def test_email_falls_back_to_console_when_nobody_is_listed() -> None:
    cfg = {"routes": {"high": {"channel": "email", "recipients": []}}}
    assert _route("high", cfg)[0] == "console", "a brief must never be silently dropped"


def test_dry_run_composes_the_mail_but_does_not_send_it() -> None:
    receipt = deliver(_brief(), routing_config(dry_run=True, roster=_ROSTER))
    assert receipt.channel == "email"
    assert receipt.status == "dry_run"


def test_the_mail_names_the_issue_the_description_and_the_owner() -> None:
    subject, body = _render(_brief(assigned="alice"))
    assert subject == "[HIGH] Open issue with no triage"
    assert "The API is down." in body          # description
    assert "Owner    : alice" in body          # assigned employee
    assert "https://github.com/a/b/issues/1" in body  # evidence link


def test_unassigned_is_stated_explicitly() -> None:
    _, body = _render(_brief(assigned=None))
    assert "Owner    : UNASSIGNED" in body
