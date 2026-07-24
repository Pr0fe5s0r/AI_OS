from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.llm import chat
from packages.shared.schema import Evidence, Situation

# The universal inbound-triage detector.
#
# Watchers reason about TIMING and GRAPH; a reviewer reads a diff. Neither can
# tell a bug report from small talk, because that is a judgement about MEANING —
# and that judgement is the same whether the words arrived from Slack, a Zendesk
# ticket, a GitHub issue or a form POST. So this lives in the engine, runs over
# EVERY connected source's fresh records, and asks one question: does a person
# need to act on this? A "yes" becomes a situation, which then flows through the
# very same assignment / notify / autonomy path a watcher's situation does.
#
# Generic by construction: it names no source and no industry. The one prompt is
# about work in the abstract (a problem, a request, an incident) and a profile
# may override it via vocabulary.prompts.triage. A record is judged once (the
# record_triage ledger) so a re-run never re-pays for the same message.

_DEFAULT_PROMPT = (
    "You triage incoming records for a work team. Decide whether THIS record is "
    "something a person needs to act on — a bug, an incident, an outage, a "
    "complaint, or a concrete request or question that needs a response — as "
    "opposed to chatter, an FYI, an automated notice, or something already "
    "resolved.\n\n"
    "Record from {source} ({type}), by {actor}:\n{content}\n\n"
    "Answer strictly for what this text supports; do not invent problems."
)

_SEVERITY = ("info", "low", "medium", "high", "critical")


def _schema() -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "triage",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "needs_action": {"type": "boolean"},
                    "severity": {"type": "string", "enum": [*_SEVERITY]},
                    "category": {"type": "string"},   # short slug: "bug", "outage", "request"
                    "title": {"type": "string"},      # <= 80 chars, imperative
                    "summary": {"type": "string"},    # one or two sentences
                    "confidence": {"type": "number"},
                },
                "required": ["needs_action", "severity", "category", "title", "summary", "confidence"],
                "additionalProperties": False,
            },
        },
    }


def _classify(prompt_template: str, record: dict[str, Any]) -> dict[str, Any]:
    """One record → a verdict. Blocking (LLM); the caller threads it. Never
    raises — a failed judgement is treated as "not actionable", so a model
    hiccup can never invent work."""
    prompt = prompt_template.format(
        source=record.get("source", ""),
        type=record.get("type", ""),
        actor=record.get("actor", ""),
        content=(record.get("content") or "").strip()[:2000],
    )
    try:
        raw = chat([{"role": "user", "content": prompt}], response_format=_schema(), temperature=0.0)
        parsed = json.loads(raw)
    except Exception:
        return {"needs_action": False, "confidence": 0.0}
    sev = str(parsed.get("severity", "medium"))
    return {
        "needs_action": bool(parsed.get("needs_action")),
        "severity": sev if sev in _SEVERITY else "medium",
        "category": str(parsed.get("category") or "triage")[:40],
        "title": str(parsed.get("title") or "").strip()[:80],
        "summary": str(parsed.get("summary") or "").strip(),
        "confidence": max(0.0, min(1.0, float(parsed.get("confidence", 0.0) or 0.0))),
    }


async def _fresh_untriaged(
    session: AsyncSession, company_id: str, limit: int, status_field: str | None
) -> list[dict[str, Any]]:
    """Live records (never backfilled history) this company has not judged yet,
    newest first. Source-agnostic — one query over all of `events`.

    Skips records that already carry a lifecycle STATUS (the profile's
    status_field): a GitHub issue or a Zendesk ticket is already tracked work
    with its own handling, so re-triaging it just duplicates the Work view.
    A raw inbound signal with no status — a Slack message, an email, a form
    POST — has nowhere else to go, so THAT is what triage is for. Generic: the
    field name is profile data, so nothing here knows a tool's vocabulary."""
    rows = await session.execute(
        text(
            """
            SELECT e.id, e.source, e.type, e.actor_name, e.content, e.metadata, e.timestamp
            FROM events e
            WHERE e.company_id = :c AND e.backfilled = false
              AND (CAST(:sf AS text) IS NULL OR e.metadata ->> :sf IS NULL)
              AND NOT EXISTS (
                  SELECT 1 FROM record_triage t
                  WHERE t.company_id = e.company_id AND t.source = e.source AND t.record_id = e.id
              )
            ORDER BY e.timestamp DESC
            LIMIT :l
            """
        ),
        {"c": company_id, "l": limit, "sf": status_field},
    )
    out: list[dict[str, Any]] = []
    for r in rows:
        md = r.metadata if isinstance(r.metadata, dict) else json.loads(r.metadata or "{}")
        out.append({
            "id": r.id, "source": r.source, "type": r.type, "actor": r.actor_name,
            "content": r.content or "", "metadata": md or {}, "timestamp": r.timestamp,
        })
    return out


async def _record_verdict(
    session: AsyncSession, company_id: str, record: dict[str, Any], verdict: dict[str, Any]
) -> None:
    await session.execute(
        text(
            """
            INSERT INTO record_triage (company_id, source, record_id, needs_action, category, triaged_at)
            VALUES (:c, :s, :i, :n, :cat, now())
            ON CONFLICT (company_id, source, record_id) DO UPDATE SET
                needs_action = EXCLUDED.needs_action, category = EXCLUDED.category, triaged_at = now()
            """
        ),
        {"c": company_id, "s": record["source"], "i": record["id"],
         "n": bool(verdict.get("needs_action")), "cat": verdict.get("category")},
    )


def _to_situation(record: dict[str, Any], verdict: dict[str, Any], company_id: str) -> Situation:
    return Situation(
        id=f"triage:{record['source']}:{record['id']}",
        company_id=company_id,
        rule="needs_triage",
        severity=verdict["severity"],
        title=verdict["title"] or (record["content"] or "").splitlines()[0][:80],
        summary=verdict["summary"],
        recommended_action="Decide who owns this and open it as tracked work.",
        evidence=[Evidence(
            event_id=record["id"], source=record["source"], timestamp=record["timestamp"],
            excerpt=(record["content"] or "")[:200],
            url=(record["metadata"] or {}).get("url"),
        )],
        created_at=datetime.now(UTC),
        # NOT "business": the watcher engine's resolve_stale(kind="business")
        # retires every business situation it did not just raise, and it never
        # raises these — so it would sweep them away one cycle later (the same
        # trap the reviewer hit). A triaged item persists until a person handles
        # it. Still visible in the feed — only kind="system" is hidden.
        kind="triage",
        category=verdict.get("category"),
        confidence=verdict.get("confidence"),
    )


async def run_triage(
    session: AsyncSession,
    profile: Any,
    *,
    limit: int = 20,
    min_confidence: float = 0.6,
) -> list[Situation]:
    """Judge this company's fresh, un-judged records from every source and
    return a situation for each that needs a person. The caller (analysis)
    processes them exactly like watcher situations — save, assign, notify, act.

    Bounded and idempotent: only live records not already in the ledger, capped
    per run, each recorded so it is never re-judged."""
    prompt = (profile.vocabulary.get("prompts", {}) or {}).get("triage", _DEFAULT_PROMPT)
    status_field = (profile.things or {}).get("status_field")
    records = await _fresh_untriaged(session, profile.company_id, limit, status_field)
    if not records:
        return []

    verdicts = await asyncio.gather(
        *(asyncio.to_thread(_classify, prompt, r) for r in records)
    )
    raised: list[Situation] = []
    for record, verdict in zip(records, verdicts, strict=True):
        await _record_verdict(session, profile.company_id, record, verdict)
        if verdict.get("needs_action") and verdict.get("confidence", 0.0) >= min_confidence:
            raised.append(_to_situation(record, verdict, profile.company_id))
    return raised
