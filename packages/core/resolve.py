from __future__ import annotations

import re

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.graph import upsert_edge, upsert_node
from packages.shared.schema import Event, GraphEdge, GraphNode, ResolvedEntity

_SYMMETRIC = {"SAME_AS", "MENTIONS"}

_CANDIDATES = text(
    """
    WITH self_emb AS (
        SELECT embedding FROM event_embeddings WHERE event_id = :self
    )
    SELECT
        n.id      AS id,
        n.type    AS type,
        e.content AS content,
        1 - (em.embedding <=> (SELECT embedding FROM self_emb)) AS sim
    FROM nodes n
    JOIN events e ON e.id = n.id
    JOIN event_embeddings em ON em.event_id = n.id
    WHERE n.company_id = :c
      AND n.source IS NOT NULL
      AND n.id <> :self
      AND EXISTS (SELECT 1 FROM self_emb)
    ORDER BY sim DESC
    LIMIT :limit
    """
)


def _node_type(event: Event, rules: dict) -> str:
    mapping = rules.get("node_types", {})
    return mapping.get(event.source, {}).get(event.type, rules.get("default_node_type", "Event"))


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


async def resolve(
    session: AsyncSession, event: Event, entity_rules: dict
) -> list[ResolvedEntity]:
    """Link ``event`` to existing events/entities and write graph nodes+edges.

    Uses the vertical-supplied ``entity_rules``:
      - explicit ID match (regex)           -> SAME_AS (method "id")
      - embedding cosine >= similarity_hard  -> SAME_AS (method "embedding")
      - keyword rule + cosine >= soft        -> MENTIONS (method "keyword")
    Also records AUTHORED (person->event) and CLOSES (PR->incident) edges.
    Requires the event's embedding to already exist in event_embeddings.
    """
    node_type = _node_type(event, entity_rules)
    label = event.content.strip().splitlines()[0][:90] if event.content.strip() else event.id

    # 1) the event as a node
    await upsert_node(
        session,
        GraphNode(
            id=event.id,
            company_id=event.company_id,
            type=node_type,
            key=f"{event.source}:{event.metadata.get('number', event.id)}",
            label=label,
            source=event.source,
            metadata={
                "actor_id": event.actor.id,
                "actor_name": event.actor.name,
                "timestamp": event.timestamp.isoformat(),
                "event_type": event.type,
                "content": event.content,
                "url": event.metadata.get("url"),
            },
        ),
    )

    # 2) the person as a node + AUTHORED edge
    person_id = f"person:{event.actor.id}"
    await upsert_node(
        session,
        GraphNode(
            id=person_id,
            company_id=event.company_id,
            type="Person",
            key=event.actor.id,
            label=event.actor.name,
            source=None,
            metadata={"name": event.actor.name},
        ),
    )
    await upsert_edge(
        session,
        GraphEdge(
            company_id=event.company_id,
            src_id=person_id,
            dst_id=event.id,
            type="AUTHORED",
            weight=1.0,
        ),
    )

    # 3) candidate links against already-ingested events
    patterns = entity_rules.get("id_patterns", [])
    keyword_rules = entity_rules.get("keyword_rules", [])
    hard = float(entity_rules.get("similarity_hard", 0.62))
    soft = float(entity_rules.get("similarity_soft", 0.50))
    limit = int(entity_rules.get("candidate_limit", 25))

    self_tokens = _id_tokens(event.content, patterns)
    is_pr = event.type == "pull_request"
    self_fixish = bool(re.search(r"\b(fix|fixes|fixed|close[sd]?|resolve[sd]?)\b", event.content, re.I))

    rows = await session.execute(
        _CANDIDATES, {"self": event.id, "c": event.company_id, "limit": limit}
    )

    resolved: list[ResolvedEntity] = []
    for r in rows:
        sim = float(r.sim)
        shared_id = bool(self_tokens & _id_tokens(r.content, patterns))
        kw = _keyword_hit(event.content, r.content, keyword_rules)

        method: str | None = None
        edge_type: str | None = None
        confidence = sim

        if shared_id:
            method, edge_type, confidence = "id", "SAME_AS", 1.0
        elif sim >= hard:
            method, edge_type = "embedding", "SAME_AS"
        elif kw and sim >= soft:
            method, edge_type = "keyword", "MENTIONS"

        if edge_type is None or method is None:
            continue

        # canonical ordering for symmetric edges so we never duplicate a pair
        src_id, dst_id = event.id, r.id
        if edge_type in _SYMMETRIC and src_id > dst_id:
            src_id, dst_id = dst_id, src_id

        await upsert_edge(
            session,
            GraphEdge(
                company_id=event.company_id,
                src_id=src_id,
                dst_id=dst_id,
                type=edge_type,
                weight=round(confidence, 4),
                metadata={"method": method},
            ),
        )
        resolved.append(
            ResolvedEntity(
                source_event_id=event.id,
                target_node_id=r.id,
                node_type=r.type,
                method=method,
                edge_type=edge_type,
                confidence=round(confidence, 4),
            )
        )

        # PR that fixes an incident/issue -> directional CLOSES edge
        if is_pr and self_fixish and r.type in ("Incident", "Feature") and (shared_id or kw):
            await upsert_edge(
                session,
                GraphEdge(
                    company_id=event.company_id,
                    src_id=event.id,
                    dst_id=r.id,
                    type="CLOSES",
                    weight=round(confidence, 4),
                    metadata={"method": method},
                ),
            )
            resolved.append(
                ResolvedEntity(
                    source_event_id=event.id,
                    target_node_id=r.id,
                    node_type=r.type,
                    method=method,
                    edge_type="CLOSES",
                    confidence=round(confidence, 4),
                )
            )

    return resolved
