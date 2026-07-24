from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# What the agent knows before it knows anything about YOU.
#
# With no seed profile, a freshly connected company starts blank: no rhythms,
# no watcher rules, no vocabulary. That is the intended design — everything
# specific gets learned. But "specific" is not the same as "everything", and an
# agent that knows nothing at all on day one is useless exactly when a person is
# forming their opinion of it.
#
# So the engine carries a PRIOR: what is true of any organization that does
# work. Not an industry, not a template — the handful of things that hold
# whether you ship software, run a warehouse, or answer tickets. It contains no
# vertical branching by construction: there is one primer, and every company
# gets the same one. What makes a company's behaviour different is what it
# LEARNS on top, never a different prior.

BASELINE_TERMS: dict[str, str] = {
    "thing": "item",
    "things": "items",
    "owner": "owner",
    "source": "tool",
}

ORGANIZATION_PRIMER = (
    "WHAT YOU ALREADY UNDERSTAND ABOUT ORGANIZATIONS.\n"
    "You have not learned this company yet, but these hold for any organization "
    "that does work, and you may reason from them from the very first minute:\n"
    "- Work arrives from tools, becomes a unit of work, and moves through states "
    "until it is finished. Every business has these, whatever it calls them.\n"
    "- A unit of work that stops moving is the single most common form of risk. "
    "Age since the last activity matters more than age since creation.\n"
    "- Unowned work is the most common failure of all. Something everyone can see "
    "and nobody owns is where things die quietly.\n"
    "- What counts as 'slow' is relative to this company's own past, never to an "
    "outside target. A number is only meaningful next to that company's normal.\n"
    "- Volume has rhythms. A sudden change in how much arrives is usually a signal "
    "about the SYSTEM (a broken connection, an outage) before it is about the work.\n"
    "- Acting on someone's real systems is consequential and often public. Prefer "
    "the reversible action; when something is visible to customers or teammates, "
    "expect a human to want a say.\n"
    "- You do not know this company's words, thresholds, or habits yet. Say so "
    "plainly when asked, and describe what you would need to learn them. Never "
    "fill the gap with a plausible-sounding number."
)


# ------------------------------ onboarding ------------------------------
# "What should I do next?" answered from real state, not a scripted tour. Each
# stage is a fact about the data, so the agent can never claim a company is set
# up when it isn't.

_STAGES: dict[str, dict[str, str]] = {
    "no_source": {
        "state": "Nothing is connected yet, so there is no data to reason about.",
        "next": "Ask them to connect their first tool, and say what will happen "
                "next: history is read, then normal patterns are learned.",
    },
    "awaiting_data": {
        "state": "A tool is connected but no records have arrived yet.",
        "next": "Say the connection is live and history is still being read. Do "
                "not speculate about what will be found.",
    },
    "shaping": {
        "state": "Records are arriving. The shape of the work is still being "
                 "worked out, and nothing specific to this company is confirmed.",
        "next": "Describe what has actually arrived so far — which tool, how "
                "many records, what they look like — and offer to confirm what "
                "each kind of record represents.",
    },
    "learning": {
        "state": "The shape is confirmed and baselines are being learned, but "
                 "there is not enough history yet to call anything normal.",
        "next": "Answer from real records, and be explicit that timings are "
                "still provisional. Do not present a learning baseline as a norm.",
    },
    "ready": {
        "state": "Enough history exists to know what normal looks like here.",
        "next": "Work normally: ground every answer in real records, and compare "
                "against this company's own baselines.",
    },
}


async def onboarding_state(
    session: AsyncSession, company_id: str, profile: Any | None = None
) -> dict[str, Any]:
    """Where this company actually is, measured from the database.

    Deliberately derived rather than stored: a "setup complete" flag drifts from
    reality the moment a connection breaks or a company is restored from backup,
    and the agent would then confidently describe a system that isn't there.
    """
    events = (
        await session.execute(
            text("SELECT count(*) FROM events WHERE company_id = :c"), {"c": company_id}
        )
    ).scalar_one()
    stable = (
        await session.execute(
            text(
                "SELECT count(*) FROM norm_baselines "
                "WHERE company_id = :c AND maturity = 'stable'"
            ),
            {"c": company_id},
        )
    ).scalar_one()

    sources = [s for s in (getattr(profile, "sources", None) or []) if s.get("enabled") is not False]
    confirmed = bool(profile is not None and getattr(profile, "things", None))

    if not sources:
        stage = "no_source"
    elif not events:
        stage = "awaiting_data"
    elif not confirmed:
        stage = "shaping"
    elif not stable:
        stage = "learning"
    else:
        stage = "ready"

    return {
        "stage": stage,
        "events": int(events),
        "stable_norms": int(stable),
        "sources": [str(s.get("source")) for s in sources],
        **_STAGES[stage],
    }


def orientation_prompt(state: dict[str, Any]) -> str:
    """The primer plus where this company actually is, for the system prompt."""
    tools = ", ".join(state["sources"]) or "none"
    return (
        f"{ORGANIZATION_PRIMER}\n\n"
        "WHERE THIS COMPANY ACTUALLY IS RIGHT NOW.\n"
        f"- Connected tools: {tools}\n"
        f"- Records held: {state['events']}\n"
        f"- Measurements settled enough to call normal: {state['stable_norms']}\n"
        f"- {state['state']}\n"
        f"WHAT TO DO NEXT: {state['next']}"
    )
