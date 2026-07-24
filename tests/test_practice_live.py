from __future__ import annotations

from sqlalchemy import text

import packages.core.act as act_mod
from packages.core.act import act, decide
from packages.core.db import Session
from packages.shared.schema import ActionRequest

# Practice mode is NOT a puppet mode. It governs whether the AI acts ON ITS OWN,
# never whether YOUR click does its job. An explicit human approval always runs
# for real — approving something and watching nothing happen is the exact
# "puppet app" feeling we refuse. These pin that: whatever the workspace mode,
# a decided/approved action reaches _execute with dry_run=False.

_CO = "test-practice-live"
_REGISTRY = {"page_engineer": {"kind": "log", "approval_required": True, "template": "PAGE {situation_id}"}}


def _req() -> ActionRequest:
    return ActionRequest(
        company_id=_CO, action="page_engineer",
        params={"situation_id": "x", "argument": "look"},
        situation_id="x", requested_by="ai",
    )


async def test_approving_runs_for_real_even_when_workspace_is_in_practice(monkeypatch):
    captured: dict = {}

    async def _spy(entry, req, approval_policy, session=None):
        captured["dry_run"] = approval_policy.get("dry_run")
        return "recorded", "ran", {"external_effect": False}

    async with Session() as s:
        await s.execute(text("DELETE FROM actions WHERE company_id = :c"), {"c": _CO})
        await s.commit()
        # queue it (approval_required -> pending), in Practice
        queued = await act({"session": s}, _req(), _REGISTRY, {"dry_run": True})
        assert queued.status == "pending_approval"
        await s.commit()

        monkeypatch.setattr(act_mod, "_execute", _spy)
        # approve it while the workspace is STILL in Practice (dry_run True)
        decided = await decide(s, queued.id, True, "ui", _REGISTRY, {"dry_run": True})
        await s.commit()

    assert decided.status == "recorded"
    assert captured["dry_run"] is False, "an explicit approval must run for real, never rehearse"


async def test_rejecting_still_sends_nothing(monkeypatch):
    ran = {"n": 0}

    async def _spy(*a, **k):
        ran["n"] += 1
        return "executed", "", {}

    async with Session() as s:
        await s.execute(text("DELETE FROM actions WHERE company_id = :c"), {"c": _CO})
        await s.commit()
        queued = await act({"session": s}, _req(), _REGISTRY, {"dry_run": True})
        await s.commit()
        monkeypatch.setattr(act_mod, "_execute", _spy)
        decided = await decide(s, queued.id, False, "ui", _REGISTRY, {"dry_run": True})
        await s.commit()

    assert decided.status == "rejected"
    assert ran["n"] == 0, "rejecting must never execute the action"
