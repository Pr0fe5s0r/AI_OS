from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core import graph
from packages.shared.schema import Event, ResolvedEntity

# Entity resolution, driven entirely by profile data. The engine has no idea
# what a "pull request" or a "purchase order" is:
#   things_cfg  = profile.things  (type mapping, actor thing, entity_rules)
#   links_cfg   = profile.links   (relationship names + the closes rule)
# Graph writes happen in core.graph (the only Cypher module).


def thing_type_for(source: str, event_type: str, things_cfg: dict) -> str:
    for spec in things_cfg.get("types", []):
        if spec.get("source") == source and spec.get("event_type") == event_type:
            return str(spec["name"])
    return str(things_cfg.get("default_type", "Event"))


def _id_tokens(textval: str, patterns: list[str]) -> set[str]:
    tokens: set[str] = set()
    for pat in patterns:
        for m in re.finditer(pat, textval):
            tokens.add(m.group(0).lower())
    return tokens


def _keyword_hit(a: str, b: str, keyword_rules: list[dict]) -> bool:
    la, lb = a.lower(), b.lower()
    for rule in keyword_rules:
        words = [w.lower() for w in rule.get("any", [])]
        if any(w in la for w in words) and any(w in lb for w in words):
            return True
    return False


async def _candidate_rows(
    session: AsyncSession, company_id: str, event_id: str, limit: int
) -> list[dict]:
    """Nearest events by embedding (Neo4j), hydrated with content from Postgres."""
    hits = await graph.similar_events(company_id, event_id, limit=limit)
    if not hits:
        return []
    sim_by_id = {h["event_id"]: float(h["similarity"]) for h in hits}
    rows = await session.execute(
        text(
            """
            SELECT id, source, type, content FROM events
            WHERE company_id = :c AND id = ANY(:ids)
            """
        ),
        {"c": company_id, "ids": list(sim_by_id)},
    )
    return [
        {"id": r.id, "source": r.source, "type": r.type, "content": r.content,
         "sim": sim_by_id[r.id]}
        for r in rows
    ]


def _last_activity(event: Event, things_cfg: dict) -> datetime:
    """When this thing last MOVED — not when it was created.

    Every stall watcher hangs off `last_activity`, and this used to be
    `event.timestamp`, which the induced mapping fills from the record's
    creation date. So "opened, then never touched again" was really measuring
    "opened a while ago", and a pull request edited an hour ago was aging at
    exactly the same rate as one nobody had opened since. The engine's own
    primer says age since last activity matters more than age since creation;
    it was measuring the thing it says not to.

    We poll snapshots rather than a change feed, so the record's own
    "last updated" stamp is the only witness to activity between two scans.
    Which field that is comes from the profile (`things.activity_field`) —
    GitHub says `updated_at`, another tool will say something else. Falls back
    to the event timestamp when a source has no such field, which is the old
    behaviour and the best available answer.
    """
    field = things_cfg.get("activity_field")
    if field:
        raw = event.metadata.get(field)
        if raw:
            try:
                return _parse_activity(raw)
            except (TypeError, ValueError):
                pass  # a malformed stamp must never lose the event
    return event.timestamp


def _parse_activity(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


async def resolve(
    session: AsyncSession,
    event: Event,
    things_cfg: dict,
    links_cfg: dict,
) -> list[ResolvedEntity]:
    """Link ``event`` into the graph using profile-supplied rules.

      - explicit ID match (regex)            -> same_as link (method "id")
      - embedding cosine >= similarity_hard  -> same_as link (method "embedding")
      - keyword rule + cosine >= soft        -> mentions link (method "keyword")
      - the profile's `closes` rule          -> its directional link type

    Requires the event's :Event mirror + embedding to already exist in Neo4j
    (the pipeline embeds before it resolves).
    """
    rules = things_cfg.get("entity_rules", {})
    ttype = thing_type_for(event.source, event.type, things_cfg)
    label = event.content.strip().splitlines()[0][:90] if event.content.strip() else event.id
    status_field = things_cfg.get("status_field", "status")

    # 1) the event's Thing + the ABOUT edge from its mirror
    await graph.upsert_thing(
        company_id=event.company_id,
        thing_id=event.id,
        thing_type=ttype,
        title=label,
        status=event.metadata.get(status_field),
        last_activity=_last_activity(event, things_cfg),
        properties={"url": event.metadata.get("url"), "source": event.source},
    )
    await graph.link_event_to_thing(event.company_id, event.id, event.id)

    # 2) the actor as a Thing (type from profile) + authored link
    actor_cfg = things_cfg.get("actor_thing")
    if actor_cfg:
        actor_id = f"actor:{event.actor.id}"
        await graph.upsert_thing(
            company_id=event.company_id,
            thing_id=actor_id,
            thing_type=str(actor_cfg.get("type", "Person")),
            title=event.actor.name,
        )
        await graph.link_things(
            event.company_id, actor_id, event.id,
            links_cfg.get("authored", "AUTHORED"),
        )

    # 3) candidate links against already-ingested events
    patterns = rules.get("id_patterns", [])
    keyword_rules = rules.get("keyword_rules", [])
    hard = float(rules.get("similarity_hard", 0.62))
    soft = float(rules.get("similarity_soft", 0.50))
    limit = int(rules.get("candidate_limit", 25))

    same_as = links_cfg.get("same_as", "SAME_AS")
    mentions = links_cfg.get("mentions", "MENTIONS")
    closes_cfg = links_cfg.get("closes") or {}
    closes_when = closes_cfg.get("when", {})
    closes_pattern = closes_when.get("content_pattern")
    self_closish = (
        ttype == closes_when.get("source_thing")
        and closes_pattern is not None
        and re.search(closes_pattern, event.content, re.I) is not None
    )

    self_tokens = _id_tokens(event.content, patterns)
    candidates = await _candidate_rows(session, event.company_id, event.id, limit)

    resolved: list[ResolvedEntity] = []
    for cand in candidates:
        sim = cand["sim"]
        shared_id = bool(self_tokens & _id_tokens(cand["content"], patterns))
        kw = _keyword_hit(event.content, cand["content"], keyword_rules)

        method: str | None = None
        link_type: str | None = None
        confidence = sim

        if shared_id:
            method, link_type, confidence = "id", same_as, 1.0
        elif sim >= hard:
            method, link_type = "embedding", same_as
        elif kw and sim >= soft:
            method, link_type = "keyword", mentions

        if link_type is None or method is None:
            continue

        # canonical ordering for symmetric links so a pair is never duplicated
        src_id, dst_id = event.id, cand["id"]
        if link_type in (same_as, mentions) and src_id > dst_id:
            src_id, dst_id = dst_id, src_id

        await graph.link_things(
            event.company_id, src_id, dst_id, link_type,
            confidence=round(confidence, 4), method=method,
        )
        cand_type = thing_type_for(cand["source"], cand["type"], things_cfg)
        resolved.append(
            ResolvedEntity(
                source_event_id=event.id,
                target_node_id=cand["id"],
                node_type=cand_type,
                method=method,
                edge_type=link_type,
                confidence=round(confidence, 4),
            )
        )

        # the profile's directional rule, e.g. PR-CLOSES-Incident or
        # Delivery-FULFILLS-PurchaseOrder — pure data, no domain in the engine
        if (
            self_closish
            and cand_type in closes_when.get("target_types", [])
            and (shared_id or kw)
        ):
            closes_type = str(closes_cfg.get("type"))
            await graph.link_things(
                event.company_id, event.id, cand["id"], closes_type,
                confidence=round(confidence, 4), method=method,
            )
            resolved.append(
                ResolvedEntity(
                    source_event_id=event.id,
                    target_node_id=cand["id"],
                    node_type=cand_type,
                    method=method,
                    edge_type=closes_type,
                    confidence=round(confidence, 4),
                )
            )

    return resolved
