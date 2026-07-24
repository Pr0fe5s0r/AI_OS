from __future__ import annotations

import json
from datetime import UTC, datetime

import packages.core.agent as agent_mod
from apps.common import inbound
from apps.common.context import save_team_roster
from packages.core.db import Session
from packages.core.profile import Profile, ensure_company, save_profile
from packages.core.situations import save_situation
from packages.shared.schema import Evidence, Situation

# Reading the reply: a lead answers "give it to Bob" and the system reassigns.
# An email is untrusted input, so the guards matter as much as the happy path —
# only owners/leads may steer, and only a clear, on-roster instruction acts.

_CO = "test-inbound"
_ISSUE_URL = "https://github.com/karthikeyan846/Chatbot/issues/4"

_PROFILE = Profile(
    company_id=_CO,
    moves={
        "registry": {"assign_issue": {
            "kind": "http", "method": "POST",
            "url": "https://api.github.com/repos/{repo}/issues/{number}/assignees",
            "auth": {"source": "github"},
            "params": {"body": {"assignees": ["{argument}"]}, "target": "github_issue"},
        }},
        # legacy target fields (moves.targets is connector-derived and stripped on
        # save; these survive a round-trip for a profile with no live connector)
        "target_url_pattern": r"github\.com/([^/]+/[^/]+)/(?:issues|pull)/(\d+)",
        "target_params": ["repo", "number"],
        "team": {"assign_move": "assign_issue", "workload": {}},
    },
)

_ROSTER = [
    {"id": "lead1", "name": "Lead", "email": "lead@x.com", "roles": ["lead"], "assignable": False},
    {"id": "bob", "name": "Bob", "email": "bob@x.com", "roles": ["dev"], "skills": ["backend"]},
    {"id": "amy", "name": "Amy", "email": "amy@x.com", "roles": ["dev"], "skills": ["frontend"]},
]


async def _setup(session):
    await ensure_company(session, _CO, _CO)
    await save_profile(session, _PROFILE)
    await save_team_roster(session, _CO, _ROSTER)
    await save_situation(session, Situation(
        id="needs_owner:gh-4", company_id=_CO, rule="needs_owner", severity="high",
        title="API is down", summary="backend error", created_at=datetime.now(UTC),
        evidence=[Evidence(event_id="gh-4", source="github", timestamp=datetime.now(UTC),
                           excerpt="x", url=_ISSUE_URL)],
    ))
    await session.commit()


def _subject() -> str:
    return "Re: [HIGH] API is down [ref:test-inbound:needs_owner:gh-4]"


async def test_a_lead_reply_reassigns_to_the_named_person(monkeypatch):
    monkeypatch.setattr(agent_mod, "chat", lambda *a, **k: json.dumps(
        {"action": "reassign", "assignee": "bob", "confidence": 0.9, "rationale": "Bob owns backend"}
    ))
    async with Session() as session:
        await _setup(session)
        result = await inbound.handle_inbound_reply(session, {
            "from": "Lead <lead@x.com>", "subject": _subject(),
            "text": "Let Bob take this — he wrote that module.",
        })
    assert result["status"] == "reassigned" and result["assignee"] == "bob", result


async def test_a_reply_from_a_stranger_is_ignored(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(agent_mod, "chat", lambda *a, **k: called.__setitem__("n", called["n"] + 1) or "{}")
    async with Session() as session:
        await _setup(session)
        result = await inbound.handle_inbound_reply(session, {
            "from": "random@nowhere.com", "subject": _subject(), "text": "give it to bob",
        })
    assert result["status"] == "unauthorized_sender"
    assert called["n"] == 0, "an unauthorized reply must not even reach the model"


async def test_an_email_with_no_ref_tag_is_a_noop():
    async with Session() as session:
        result = await inbound.handle_inbound_reply(session, {
            "from": "lead@x.com", "subject": "just saying hi", "text": "hello",
        })
    assert result["status"] == "no_ref"


async def test_keep_or_unclear_replies_do_not_reassign(monkeypatch):
    monkeypatch.setattr(agent_mod, "chat", lambda *a, **k: json.dumps(
        {"action": "keep", "assignee": "none", "confidence": 0.8, "rationale": "fine as is"}
    ))
    async with Session() as session:
        await _setup(session)
        result = await inbound.handle_inbound_reply(session, {
            "from": "lead@x.com", "subject": _subject(), "text": "looks good, thanks",
        })
    assert result["status"] == "keep"
