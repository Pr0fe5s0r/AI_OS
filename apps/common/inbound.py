from __future__ import annotations

import re
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from apps.common.analysis import _current_assignee, accept_assignment, extract_target
from apps.common.context import team_roster
from packages.core import audit
from packages.core.agent import interpret_reply
from packages.core.profile import load_profile
from packages.core.situations import get_situation

# Closing the loop the other way: not just notifying a person, but READING their
# reply. A lead who answers "let Bob take this — he wrote that module" is giving
# an instruction; the system should act on it, not leave it in an inbox.
#
# Correlation is by the [ref:company:situation] tag deliver.py stamps into the
# subject — a reply quotes the subject, so the tag comes back. Only a workspace
# owner or lead may steer an assignment this way; anyone else's reply is logged
# and ignored. An email is untrusted input, so every decision still runs through
# the same skill-aware model and the same assignment path a click would.

_REF = re.compile(r"\[ref:([^:\]]+):([^\]]+)\]")

_REPLY_PROMPT = (
    "A person replied to an email about a piece of assigned work. Decide what "
    "they want done.\n\n"
    "Current owner: {current}\n"
    "People who could own it instead:\n{candidates}\n\n"
    "Their reply:\n{reply}\n\n"
    "If they clearly want it handed to a specific person on that list, "
    "action=reassign and pick them. If they're fine with it as is, action=keep. "
    "If you cannot tell, action=unclear."
)

_MIN_CONFIDENCE = 0.6


def _extract_ref(subject: str) -> tuple[str, str] | None:
    m = _REF.search(subject or "")
    return (m.group(1), m.group(2)) if m else None


def _sender_email(payload: dict) -> str:
    m = re.search(r"[\w.+-]+@[\w.-]+", str(payload.get("from", "")))
    return (m.group(0) if m else "").lower()


def _is_authorized(sender: str, roster: list[dict]) -> bool:
    """Only an owner or lead may reassign by email; everyone else is read-only."""
    return any(
        str(m.get("email", "")).lower() == sender
        and any(r in m.get("roles", []) for r in ("owner", "lead"))
        for m in roster
    )


async def handle_inbound_reply(session: AsyncSession, payload: dict) -> dict[str, Any]:
    """Process one inbound email reply. ``payload`` = {from, subject, text} — the
    shape an email-forwarding service (SendGrid Inbound Parse, Mailgun, Postmark)
    POSTs. Every early return is a legitimate "nothing to do", never a 500."""
    ref = _extract_ref(payload.get("subject", ""))
    if ref is None:
        return {"status": "no_ref"}
    company_id, situation_id = ref

    profile = await load_profile(session, company_id)
    situation = await get_situation(session, company_id, situation_id)
    if profile is None or situation is None:
        return {"status": "unknown_ref", "company_id": company_id}

    roster = await team_roster(session, company_id)
    sender = _sender_email(payload)
    if not _is_authorized(sender, roster):
        return {"status": "unauthorized_sender", "sender": sender}

    candidates = [m for m in roster if m.get("assignable", True)]
    current = await _current_assignee(session, profile, situation)
    prompts = profile.vocabulary.get("prompts", {})
    decision = interpret_reply(
        payload.get("text", ""), candidates, current,
        prompts.get("interpret_reply", _REPLY_PROMPT),
    )
    await audit.record(
        session, company_id, sender, "inbound.reply", target=situation_id, metadata=decision
    )

    if decision["action"] != "reassign" or decision["assignee"] == "none":
        return {"status": decision["action"], "situation_id": situation_id, **decision}
    if decision["confidence"] < _MIN_CONFIDENCE:
        return {"status": "low_confidence", "situation_id": situation_id, **decision}

    url = situation.evidence[0].url if situation.evidence else None
    target = extract_target(profile, url)
    if target is None:
        return {"status": "no_target", "situation_id": situation_id}

    assign_payload = {
        "situation_id": situation.id, "assignee": decision["assignee"], "target": target,
        "item_label": " ".join(str(v) for v in target.values()),
        "event_id": situation.evidence[0].event_id if situation.evidence else None,
        "title": situation.title, "summary": situation.summary,
    }
    result = await accept_assignment(session, profile, assign_payload, clicked_by=f"email:{sender}")
    return {
        "status": "reassigned", "situation_id": situation_id,
        "assignee": decision["assignee"], "by": sender,
        "assign_status": result.get("assign_status"), "rationale": decision["rationale"],
    }
