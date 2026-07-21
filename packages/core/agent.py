from __future__ import annotations

import json
from typing import Any

from packages.core.llm import chat
from packages.shared.schema import ActionDecision, AssigneeDecision, Situation

# Generic agent runtime: given a situation and the set of actions it is allowed
# to take, the model decides WHICH action to run and with what argument, plus how
# confident it is. The core holds no opinion about what any action means — the
# allowed list and the prompt both arrive as data from the vertical.
#
# "escalate" is always available: the model's own way of saying "a human should
# decide this one". That is the honest path when it cannot choose.


def _schema(allowed_actions: list[str]) -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "action_decision",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": [*allowed_actions, "escalate"]},
                    "argument": {"type": "string"},
                    "confidence": {"type": "number"},
                    "rationale": {"type": "string"},
                },
                "required": ["action", "argument", "confidence", "rationale"],
                "additionalProperties": False,
            },
        },
    }


def decide_action(
    situation: Situation,
    allowed_actions: list[str],
    prompt_template: str,
    action_help: str = "",
) -> ActionDecision:
    """Choose an action for a situation. Never raises — failure means escalate.

    ``action_help`` is caller-supplied text describing what each action's
    `argument` must contain — profile data, not engine knowledge.
    """
    evidence = "\n".join(
        f"- {e.source}:{e.event_id} {e.url or ''}\n  {e.excerpt}" for e in situation.evidence
    )
    prompt = prompt_template.format(
        title=situation.title,
        severity=situation.severity,
        summary=situation.summary,
        recommended_action=situation.recommended_action or "none",
        evidence=evidence or "none",
        actions=", ".join(allowed_actions),
        action_help=action_help or "none provided",
    )

    try:
        raw = chat([{"role": "user", "content": prompt}], response_format=_schema(allowed_actions))
        parsed = json.loads(raw)
    except Exception as exc:  # a model/parse failure must never auto-act
        return ActionDecision(
            action="escalate", confidence=0.0, rationale=f"decision failed: {exc}"
        )

    action = str(parsed.get("action", "escalate"))
    if action not in allowed_actions and action != "escalate":
        return ActionDecision(
            action="escalate", confidence=0.0, rationale=f"model chose unregistered action {action!r}"
        )

    confidence = float(parsed.get("confidence", 0.0))
    return ActionDecision(
        action=action,
        argument=str(parsed.get("argument", "")),
        confidence=min(max(confidence, 0.0), 1.0),
        rationale=str(parsed.get("rationale", "")),
    )


def _assignee_schema(candidate_ids: list[str]) -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "assignee_decision",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "assignee": {"type": "string", "enum": [*candidate_ids, "none"]},
                    "confidence": {"type": "number"},
                    "rationale": {"type": "string"},
                },
                "required": ["assignee", "confidence", "rationale"],
                "additionalProperties": False,
            },
        },
    }


def choose_assignee(
    situation: Situation, candidates: list[dict], prompt_template: str
) -> AssigneeDecision:
    """Pick who should own this work, from candidates the caller says are free.

    ``candidates`` is opaque data supplied by the vertical — the core only knows
    each has an ``id``. Returning "none" means nobody suitable is available.
    """
    if not candidates:
        return AssigneeDecision(assignee="none", confidence=0.0, rationale="no available candidate")

    ids = [str(c["id"]) for c in candidates]
    roster = "\n".join(
        f"- {c['id']} ({c.get('name', c['id'])}) roles={c.get('roles', [])} "
        f"skills={c.get('skills', [])} workload={c.get('open_issues', 0)}/{c.get('capacity', '?')}"
        for c in candidates
    )
    evidence = "\n".join(f"- {e.source}:{e.event_id} {e.excerpt}" for e in situation.evidence)
    prompt = prompt_template.format(
        title=situation.title,
        severity=situation.severity,
        summary=situation.summary,
        evidence=evidence or "none",
        candidates=roster,
    )

    try:
        raw = chat([{"role": "user", "content": prompt}], response_format=_assignee_schema(ids))
        parsed = json.loads(raw)
    except Exception as exc:
        return AssigneeDecision(assignee="none", confidence=0.0, rationale=f"decision failed: {exc}")

    assignee = str(parsed.get("assignee", "none"))
    if assignee not in ids and assignee != "none":
        return AssigneeDecision(assignee="none", confidence=0.0, rationale="model picked an unknown person")

    confidence = float(parsed.get("confidence", 0.0))
    return AssigneeDecision(
        assignee=assignee,
        confidence=min(max(confidence, 0.0), 1.0),
        rationale=str(parsed.get("rationale", "")),
    )
