from __future__ import annotations

import asyncio
import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from packages.connectors.base import review_content_for, target_spec_for
from packages.core.credentials import get_credential
from packages.core.llm import chat
from packages.core.search import search
from packages.core.situations import save_situation
from packages.shared.schema import Evidence, Finding, Situation

# The reviewer engine: a grounded specialist fan-out over one record's content.
#
# Where the watcher engine (detect.py) reasons about TIMING and GRAPH shape — a
# thing that stalled, a commitment aging — a reviewer READS the thing. It grounds
# an LLM in the record's own diff/commits/thread plus its hybrid-search
# neighbours, runs several concerns over it AT ONCE, and returns structured
# Findings. Every concern, its prompt, and the gate all arrive as DATA (from the
# profile's `reviewers` slot, defaulted by the connector) — this module names no
# concern and no tool, the same discipline the rest of core keeps.
#
# A Finding with file/line anchors becomes a line-anchored Situation; without
# them, a thing-level one. Nothing here posts anywhere: findings surface through
# the ordinary situation lifecycle, and a separate, gated posting step (act.py)
# is the only thing that may ever reach a real tool.

SEVERITY_ORDER = ("info", "low", "medium", "high", "critical")

# Ceilings and the parse contract are enforced here, in ONE place, so a concern
# prompt stays about WHAT to look for and never has to restate the output shape.
_FORMAT = (
    "Return a JSON object {\"findings\": [...]}. Each finding has: "
    "severity (one of critical|high|medium|low|info), "
    "category (a short kebab-case slug like \"injection\" or \"missing-test\"), "
    "title (<= 80 chars), "
    "file_path (the file the defect is in, or null), "
    "line_start (integer line number, or null), line_end (integer, or null), "
    "confidence (a number 0..1 for how sure you are this is a real defect), "
    "rationale (cite the specific line or evidence — one or two sentences), "
    "suggestion (a concrete fix). "
    "Report ONLY real defects this change introduces or newly exposes. "
    "Prefer precision over recall: if you are unsure, leave it out. "
    "If there are none, return {\"findings\": []}."
)

_CONTENT_BUDGET = 4000   # chars of the record itself handed to each concern
_NEIGHBOUR_BUDGET = 320  # chars per grounded neighbour snippet


def _clamp_severity(severity: str, ceiling: str) -> str:
    """A docs nit may not shout `critical`. Pin a finding's severity at or
    below its concern's declared ceiling, defaulting unknown labels to `low`."""
    sev = severity if severity in SEVERITY_ORDER else "low"
    cap = ceiling if ceiling in SEVERITY_ORDER else "critical"
    if SEVERITY_ORDER.index(sev) > SEVERITY_ORDER.index(cap):
        return cap
    return sev


def _finding_id(reviewer_key: str, thing_id: str, concern: str, f: Finding) -> str:
    """A stable id per finding so a re-review UPSERTS the same situation rather
    than piling up duplicates, and a finding that moved or vanished can be
    retired. Keyed on the anchor + title, which together identify the defect
    independently of the wording around it."""
    anchor = f"{f.file_path}:{f.line_start}:{f.title}".lower()
    digest = hashlib.sha1(anchor.encode()).hexdigest()[:10]
    return f"review:{reviewer_key}:{thing_id}:{concern}:{digest}"


def _parse_findings(
    raw: str, reviewer_key: str, concern: str, ceiling: str, thing: dict[str, Any]
) -> list[Finding]:
    """Turn one concern's JSON answer into validated Findings. A malformed or
    empty answer yields nothing — a reviewer that cannot speak the contract is
    silent, never a source of garbage findings."""
    try:
        payload = json.loads(raw or "{}")
    except (json.JSONDecodeError, TypeError):
        return []
    items = payload.get("findings") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return []

    out: list[Finding] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()[:80]
        if not title:
            continue
        try:
            confidence = float(item.get("confidence", 0.0) or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        out.append(
            Finding(
                reviewer_key=reviewer_key,
                concern=concern,
                thing_id=str(thing["id"]),
                source=str(thing.get("source") or ""),
                severity=_clamp_severity(str(item.get("severity") or "low"), ceiling),
                category=str(item.get("category") or concern)[:40],
                title=title,
                file_path=(str(item["file_path"]) if item.get("file_path") else None),
                line_start=_as_int(item.get("line_start")),
                line_end=_as_int(item.get("line_end")),
                confidence=max(0.0, min(1.0, confidence)),
                rationale=str(item.get("rationale") or "").strip(),
                suggestion=str(item.get("suggestion") or "").strip(),
            )
        )
    return out


def _as_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _run_concern(concern: dict[str, Any], context: str, thing: dict[str, Any]) -> str:
    """One specialist pass — a single blocking LLM call. Wrapped in a thread by
    the caller so the concerns fan out concurrently (our parallel specialists,
    no orchestration framework needed)."""
    system = f"{concern.get('prompt', '')}\n\n{_FORMAT}"
    user = (
        f"Review this {thing.get('type') or 'change'} titled {thing.get('title')!r}.\n\n"
        f"{context}"
    )
    return chat(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        response_format={"type": "json_object"},
        temperature=0.0,
    )


async def _fetch_diff(
    session: AsyncSession, company_id: str, source: str, thing: dict[str, Any]
) -> str:
    """The record's real diff, fetched fresh through the connector and never
    stored. Without this the reviewer sees only the change SUMMARY (which files,
    how many lines) and never the code — so it guesses, exactly the ungrounded
    failure the first live run exposed. Best-effort: any missing piece (no
    fetcher, no addressable URL, no credential) degrades to the summary."""
    fetcher = review_content_for(source)
    url = (thing.get("metadata") or {}).get("url") or thing.get("url")
    if fetcher is None or not url:
        return ""
    pattern, params = target_spec_for(source)
    if not pattern:
        return ""
    match = re.search(str(pattern), str(url))
    if not match:
        return ""
    target = dict(zip(params, match.groups(), strict=False))
    repo, number = target.get("repo"), target.get("number")
    if not repo or not number:
        return ""
    cred = await get_credential(session, company_id, source)
    token = cred[0] if cred else ""
    try:
        return await fetcher(repo, number, token)
    except Exception:
        return ""


async def _ground(
    session: AsyncSession, company_id: str, thing: dict[str, Any], reviewer: dict[str, Any]
) -> str:
    """Build the context a colleague who has read the code would have: the
    record's change summary, the REAL diff (fetched fresh, never stored), and
    its nearest neighbours from hybrid search — so a finding is grounded in the
    actual code, not guessed. Each piece degrades independently rather than
    aborting the review."""
    parts = [f"--- the change ---\n{(thing.get('content') or '').strip()[:_CONTENT_BUDGET]}"]

    diff = await _fetch_diff(session, company_id, str(thing.get("source") or ""), thing)
    if diff:
        parts.append(
            "--- diff (the actual code changed; @@ headers give line numbers) ---\n" + diff
        )

    top_k = int(((reviewer.get("ground") or {}).get("retrieve") or {}).get("top_k", 0) or 0)
    if top_k:
        query = f"{thing.get('title') or ''}\n{(thing.get('content') or '')[:400]}"
        try:
            hits = await search(session, company_id, query, limit=top_k + 1)
        except Exception:
            hits = []
        snippets = [
            f"- {(h.get('content') or '').strip()[:_NEIGHBOUR_BUDGET]}"
            for h in hits
            if h.get("id") != thing["id"]
        ][:top_k]
        if snippets:
            parts.append("--- related code/records in this workspace ---\n" + "\n".join(snippets))
    return "\n\n".join(parts)


def _to_situation(reviewer_key: str, thing: dict[str, Any], f: Finding) -> Situation:
    """A Finding, rendered as the Situation that carries it through the ordinary
    feed/lifecycle. Anchors ride along so a later posting step can place an
    inline comment; `confidence` and `category` are the reviewer's own."""
    where = ""
    if f.file_path:
        where = f" ({f.file_path}" + (f":{f.line_start}" if f.line_start else "") + ")"
    summary = f.rationale
    if f.suggestion:
        summary = f"{summary}\n\nSuggested fix: {f.suggestion}".strip()
    url = (thing.get("metadata") or {}).get("url") or thing.get("url")
    return Situation(
        id=_finding_id(reviewer_key, str(thing["id"]), f.concern, f),
        company_id=thing["company_id"],
        rule=f"review.{reviewer_key}.{f.concern}",
        severity=f.severity,
        title=f"{f.title}{where}",
        summary=summary,
        recommended_action=f.suggestion or None,
        evidence=[
            Evidence(
                event_id=str(thing["id"]),
                source=f.source,
                timestamp=thing["timestamp"],
                excerpt=(thing.get("title") or "")[:200],
                url=url,
            )
        ],
        created_at=datetime.now(UTC),
        # NOT "business": the watcher engine's resolve_stale(kind="business")
        # retires every business situation not in ITS active set, which swept
        # these away one cron cycle after they were raised (the second bug the
        # first live run exposed). A reviewer's findings are retired only by the
        # reviewer's own _resolve_stale_reviews. Still visible in the feed —
        # list_situations hides only kind="system".
        kind="review",
        file_path=f.file_path,
        line_start=f.line_start,
        line_end=f.line_end,
        confidence=f.confidence,
        category=f.category,
    )


def _dedupe(findings: list[Finding]) -> list[Finding]:
    """When two concerns flag the same spot, keep the most confident and drop
    the rest — the blog's "keep highest-confidence at a location". Keyed on the
    anchor; unanchored findings (file/line None) are never merged, since we
    cannot prove they are the same defect."""
    best: dict[tuple, Finding] = {}
    kept: list[Finding] = []
    for f in sorted(findings, key=lambda x: x.confidence, reverse=True):
        if f.file_path is None or f.line_start is None:
            kept.append(f)
            continue
        key = (f.file_path, f.line_start)
        if key not in best:
            best[key] = f
            kept.append(f)
    return kept


# -------------------------------- feedback loop --------------------------------
# What the team taught us by dismissing findings. Two lessons, one signal:
#   Layer 1 — respect THIS dismissal: never reopen a finding a person waved off
#             (the finding id upserts, so without this a re-review would raise it
#             from the dead the moment the PR's timestamp advanced).
#   Layer 2 — learn the team's TASTE: a concern+category dismissed enough times
#             stops being raised at all.
# Guards against a poisoned loop: only EXPLICIT human dismissals count (nothing
# the system did to itself), a minimum before we act, and `critical` is never
# suppressed — we must not learn to hide a security critical.

FEEDBACK_MIN_DISMISSALS = 3  # minimum evidence before a category is muted


async def record_dismissal(session: AsyncSession, company_id: str, situation_id: str) -> bool:
    """Log that a person dismissed a review finding. No-op (returns False) for a
    situation that isn't a reviewer's finding, so the generic dismiss path can
    call this unconditionally."""
    row = (
        await session.execute(
            text(
                "SELECT rule, category, severity FROM situations "
                "WHERE company_id = :c AND id = :i"
            ),
            {"c": company_id, "i": situation_id},
        )
    ).first()
    if row is None or not str(row.rule or "").startswith("review."):
        return False
    parts = row.rule.split(".")  # review.{reviewer_key}.{concern}
    reviewer_key = parts[1] if len(parts) > 2 else ""
    concern = parts[-1]
    await session.execute(
        text(
            """
            INSERT INTO review_feedback
                (company_id, reviewer_key, finding_id, concern, category, severity, verdict)
            VALUES (:c, :r, :i, :concern, :cat, :sev, 'dismissed')
            """
        ),
        {
            "c": company_id, "r": reviewer_key, "i": situation_id,
            "concern": concern, "cat": row.category or concern, "sev": row.severity,
        },
    )
    return True


async def _dismissed_finding_ids(
    session: AsyncSession, company_id: str, record_id: str
) -> set[str]:
    """Finding ids on THIS record a person has dismissed — Layer 1."""
    rows = await session.execute(
        text(
            """
            SELECT DISTINCT finding_id FROM review_feedback
            WHERE company_id = :c AND verdict = 'dismissed'
              AND finding_id LIKE 'review:%:' || :tid || ':%'
            """
        ),
        {"c": company_id, "tid": record_id},
    )
    return {r.finding_id for r in rows}


async def _suppressed_categories(
    session: AsyncSession, company_id: str
) -> set[tuple[str, str]]:
    """(concern, category) pairs the team has dismissed enough times to mute —
    Layer 2. `critical` findings never contribute and are never muted, so a
    security critical can't be taught away."""
    rows = await session.execute(
        text(
            """
            SELECT concern, category, count(*) AS n FROM review_feedback
            WHERE company_id = :c AND verdict = 'dismissed' AND severity <> 'critical'
            GROUP BY concern, category
            HAVING count(*) >= :min
            """
        ),
        {"c": company_id, "min": FEEDBACK_MIN_DISMISSALS},
    )
    return {(r.concern, r.category) for r in rows}


async def suppression_report(session: AsyncSession, company_id: str) -> list[dict[str, Any]]:
    """What's muted and why — evidence, never a silent filter. Powers the
    transparency surface (and lets a person see what to un-mute)."""
    rows = await session.execute(
        text(
            """
            SELECT concern, category, count(*) AS dismissals, max(created_at) AS last
            FROM review_feedback
            WHERE company_id = :c AND verdict = 'dismissed' AND severity <> 'critical'
            GROUP BY concern, category
            HAVING count(*) >= :min
            ORDER BY count(*) DESC
            """
        ),
        {"c": company_id, "min": FEEDBACK_MIN_DISMISSALS},
    )
    return [
        {"concern": r.concern, "category": r.category,
         "dismissals": int(r.dismissals), "muted_since": r.last.isoformat()}
        for r in rows
    ]


# ------------------------------ re-review ledger ------------------------------


async def _last_reviewed_activity(
    session: AsyncSession, company_id: str, reviewer_key: str, record_id: str
) -> str | None:
    row = (
        await session.execute(
            text(
                """
                SELECT reviewed_activity FROM record_reviews
                WHERE company_id = :c AND reviewer_key = :r AND record_id = :i
                """
            ),
            {"c": company_id, "r": reviewer_key, "i": record_id},
        )
    ).first()
    return row.reviewed_activity if row else None


async def _record_review(
    session: AsyncSession,
    company_id: str,
    reviewer_key: str,
    record_id: str,
    source: str,
    activity: str | None,
    finding_count: int,
) -> None:
    await session.execute(
        text(
            """
            INSERT INTO record_reviews
                (company_id, reviewer_key, record_id, source, reviewed_activity, finding_count, reviewed_at)
            VALUES (:c, :r, :i, :s, :a, :n, now())
            ON CONFLICT (company_id, reviewer_key, record_id) DO UPDATE SET
                reviewed_activity = EXCLUDED.reviewed_activity,
                finding_count = EXCLUDED.finding_count,
                reviewed_at = now()
            """
        ),
        {"c": company_id, "r": reviewer_key, "i": record_id, "s": source, "a": activity, "n": finding_count},
    )


async def _resolve_stale_reviews(
    session: AsyncSession, company_id: str, reviewer_key: str, record_id: str, active_ids: list[str]
) -> None:
    """Retire this record's prior findings that this pass did NOT re-raise — the
    line got fixed, or the code moved. Scoped by the id prefix so it only ever
    touches THIS reviewer's findings on THIS record, never another watcher's
    business situations."""
    await session.execute(
        text(
            """
            UPDATE situations SET status = 'resolved', resolved_at = now()
            WHERE company_id = :c
              AND id LIKE :prefix
              AND status <> 'resolved'
              AND NOT (id = ANY(:active))
            """
        ),
        {"c": company_id, "prefix": f"review:{reviewer_key}:{record_id}:%", "active": active_ids},
    )


# --------------------------------- selection ---------------------------------


async def _select_things(
    session: AsyncSession, company_id: str, reviewer: dict[str, Any], status_field: str | None,
    limit: int = 25,
) -> list[dict[str, Any]]:
    """The records this reviewer targets — same selector grammar the watchers
    use (`thing_type` + a `where` matched through the profile's status_field),
    so nothing here knows GitHub calls it "state" or "open"."""
    select = reviewer.get("select") or {}
    thing_type = select.get("thing_type")
    if not thing_type:
        return []
    where = select.get("where") or {}
    status = where.get("status")

    clauses = ["company_id = :c", "type = :t"]
    params: dict[str, Any] = {"c": company_id, "t": thing_type, "l": limit}
    if status and status_field:
        # status_field is profile data, not a request; quote defensively
        clauses.append(f"metadata->>'{status_field}' = :st")
        params["st"] = status

    rows = await session.execute(
        text(
            f"""
            SELECT id, company_id, source, type, content, metadata, timestamp
            FROM events WHERE {" AND ".join(clauses)}
            ORDER BY timestamp DESC LIMIT :l
            """
        ),
        params,
    )
    out: list[dict[str, Any]] = []
    for r in rows:
        md = r.metadata if isinstance(r.metadata, dict) else json.loads(r.metadata or "{}")
        content = (r.content or "").strip()
        out.append(
            {
                "id": r.id,
                "company_id": r.company_id,
                "source": r.source,
                "type": r.type,
                "content": content,
                "title": content.splitlines()[0][:120] if content else r.id,
                "metadata": md or {},
                "timestamp": r.timestamp,
            }
        )
    return out


# ----------------------------------- engine -----------------------------------


async def review_thing(
    session: AsyncSession,
    company_id: str,
    reviewer: dict[str, Any],
    thing: dict[str, Any],
    activity_field: str | None,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Review one record through all of a reviewer's concerns and raise the
    surviving findings as situations. Skips a record whose activity stamp is
    unchanged since the last review unless ``force`` — the cost guard that keeps
    a 5-minute cadence from re-paying for a PR nobody touched."""
    reviewer_key = str(reviewer.get("key") or "reviewer")
    record_id = str(thing["id"])
    activity = str(thing["metadata"].get(activity_field)) if activity_field else None

    if not force and activity is not None:
        seen = await _last_reviewed_activity(session, company_id, reviewer_key, record_id)
        if seen == activity:
            return {"thing_id": record_id, "status": "unchanged", "findings": 0}

    context = await _ground(session, company_id, thing, reviewer)
    concerns = reviewer.get("concerns") or []

    # The fan-out: every concern reads the same grounded context at once.
    raw_answers = await asyncio.gather(
        *(asyncio.to_thread(_run_concern, c, context, thing) for c in concerns)
    )
    findings: list[Finding] = []
    for concern, raw in zip(concerns, raw_answers, strict=True):
        findings.extend(
            _parse_findings(
                raw, reviewer_key, str(concern.get("key") or "concern"),
                str(concern.get("severity_ceiling") or "critical"), thing,
            )
        )
    findings = _dedupe(findings)

    # Apply what the team taught us. A `critical` is never filtered — the safety
    # floor — so security can't be dismissed into silence.
    dismissed_ids = await _dismissed_finding_ids(session, company_id, record_id)
    muted = await _suppressed_categories(session, company_id)
    kept: list[Finding] = []
    suppressed = 0
    for f in findings:
        if f.severity != "critical":
            fid = _finding_id(reviewer_key, record_id, f.concern, f)
            if fid in dismissed_ids or (f.concern, f.category) in muted:
                suppressed += 1
                continue
        kept.append(f)
    findings = kept

    situations = [_to_situation(reviewer_key, thing, f) for f in findings]
    for s in situations:
        await save_situation(session, s)
    await _resolve_stale_reviews(
        session, company_id, reviewer_key, record_id, [s.id for s in situations]
    )
    await _record_review(
        session, company_id, reviewer_key, record_id,
        str(thing.get("source") or ""), activity, len(situations),
    )
    return {
        "thing_id": record_id,
        "status": "reviewed",
        "findings": len(situations),
        "suppressed": suppressed,
        "by_severity": _severity_counts(situations),
    }


def _severity_counts(situations: list[Situation]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for s in situations:
        counts[s.severity] = counts.get(s.severity, 0) + 1
    return counts


async def run_reviewers(
    session: AsyncSession,
    company_id: str,
    reviewers: list[dict[str, Any]],
    status_field: str | None,
    activity_field: str | None,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Run every reviewer in the profile over its selected records. The one
    entry point apps.common.analysis calls after the watcher engine."""
    results: list[dict[str, Any]] = []
    for reviewer in reviewers or []:
        things = await _select_things(session, company_id, reviewer, status_field)
        for thing in things:
            results.append(
                await review_thing(
                    session, company_id, reviewer, thing, activity_field, force=force
                )
            )
    reviewed = [r for r in results if r["status"] == "reviewed"]
    return {
        "reviewed": len(reviewed),
        "unchanged": sum(1 for r in results if r["status"] == "unchanged"),
        "findings": sum(r["findings"] for r in results),
        "results": results,
    }
