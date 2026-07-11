from __future__ import annotations

from packages.shared.schema import Brief, Situation


def assemble_brief(situation: Situation) -> Brief:
    """Turn a Situation into a delivered Brief with an explicit evidence chain.

    Every claim is traceable: citations are the concrete event ids (and URLs)
    the detection fired on. Generic — no domain wording.
    """
    citations = [
        f"{e.source}:{e.event_id}" + (f" <{e.url}>" if e.url else "")
        for e in situation.evidence
    ]
    return Brief(
        situation_id=situation.id,
        title=situation.title,
        severity=situation.severity,
        summary=situation.summary,
        recommended_action=situation.recommended_action,
        evidence=situation.evidence,
        citations=citations,
    )
