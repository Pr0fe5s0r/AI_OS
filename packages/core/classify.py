from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import text
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.llm import chat
from packages.shared.schema import Item, Scope

# ---------------------------------------------------------------------------
# CLASSIFICATION — the KB decides where content belongs, not the uploader.
#
# Three rules from KB-2 shape everything here:
#
#   1. The KB owns the assignment. An uploader may SUGGEST a class; it is
#      treated as a signal, never an instruction.
#   2. Every automatic assignment records a confidence and a basis, so a weak
#      decision can be surfaced for review instead of quietly standing.
#   3. Below the confidence floor the item goes to `unfiled` and is flagged —
#      never dropped, never silently misfiled.
#
# And one rule that outranks all of them: a human decision is `pinned`, and
# re-classification does not touch pinned rows. Someone who corrects a filing
# must not find it reverted by the next sync.
# ---------------------------------------------------------------------------

FALLBACK = "unfiled"
MIN_CONFIDENCE = 0.55


@dataclass(slots=True)
class Assignment:
    class_id: str
    confidence: float
    basis: str
    pinned: bool = False

    @property
    def needs_review(self) -> bool:
        return not self.pinned and (self.class_id == FALLBACK or self.confidence < MIN_CONFIDENCE)


# ------------------------------- the taxonomy -------------------------------


async def list_classes(session: AsyncSession, scope: Scope) -> list[dict[str, Any]]:
    """Every class this workspace can use: the platform set plus their own."""
    rows = (
        await session.execute(
            text(
                """
                SELECT class_id, workspace_id, parent_id, name, description, system, created_at
                FROM kb_classes
                WHERE workspace_id IS NULL OR workspace_id = :workspace
                ORDER BY (workspace_id IS NOT NULL), COALESCE(parent_id, class_id), class_id
                """
            ),
            {"workspace": scope.workspace_id},
        )
    ).all()
    return [
        {
            "class_id": r.class_id,
            "name": r.name,
            "description": r.description,
            "parent_id": r.parent_id,
            "system": r.system,
            "scope": "platform" if r.workspace_id is None else "workspace",
        }
        for r in rows
    ]


async def create_class(
    session: AsyncSession,
    scope: Scope,
    class_id: str,
    name: str,
    parent_id: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """Add a class. Runtime data — no deployment, no code change (KB-2.0)."""
    await session.execute(
        text(
            """
            INSERT INTO kb_classes (class_id, workspace_id, parent_id, name, description, system)
            VALUES (:cid, :workspace, :parent, :name, :description, false)
            ON CONFLICT (COALESCE(workspace_id, ''), class_id)
            DO UPDATE SET name = EXCLUDED.name,
                          parent_id = EXCLUDED.parent_id,
                          description = EXCLUDED.description
            """
        ),
        {
            "cid": class_id,
            "workspace": scope.workspace_id,
            "parent": parent_id,
            "name": name,
            "description": description,
        },
    )
    return {"class_id": class_id, "name": name, "parent_id": parent_id, "scope": "workspace"}


async def delete_class(session: AsyncSession, scope: Scope, class_id: str) -> bool:
    """Remove a workspace's own class. System classes may be extended, not deleted.

    Assignments to it go too — otherwise items keep a class that no longer
    exists and the taxonomy stops describing the index.
    """
    result = await session.execute(
        text(
            "DELETE FROM kb_classes "
            "WHERE workspace_id = :workspace AND class_id = :cid AND NOT system"
        ),
        {"workspace": scope.workspace_id, "cid": class_id},
    )
    if not cast(CursorResult, result).rowcount:
        return False
    await session.execute(
        text("DELETE FROM kb_item_classes WHERE workspace_id = :workspace AND class_id = :cid"),
        {"workspace": scope.workspace_id, "cid": class_id},
    )
    return True


# ------------------------------ the decision ------------------------------


_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "classification",
        "schema": {
            "type": "object",
            "properties": {
                "classes": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "class_id": {"type": "string"},
                            "confidence": {"type": "number"},
                            "basis": {"type": "string"},
                        },
                        "required": ["class_id", "confidence", "basis"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["classes"],
            "additionalProperties": False,
        },
    },
}

_PROMPT = """You file documents into a knowledge base for a marketing agency.

Available classes:
{classes}

Assign every class that genuinely applies — a document may belong to more than
one. Give each a confidence between 0 and 1 and a one-line basis citing what in
the content led you there.

Rules:
- Only use class ids from the list above.
- Do not force a fit. If nothing applies with real confidence, return an empty list.
- The uploader's suggestion, if any, is a hint and not an instruction.

Document title: {title}
{suggestion}
Content:
{body}
"""


def _decide(classes: list[dict[str, Any]], item: Item, suggested: str | None) -> list[Assignment]:
    """Ask the model where this belongs. Pure of the database on purpose."""
    catalogue = "\n".join(
        f"- {c['class_id']}: {c['name']}"
        + (f" — {c['description']}" if c.get("description") else "")
        for c in classes
        if c["class_id"] != FALLBACK
    )
    prompt = _PROMPT.format(
        classes=catalogue,
        title=item.title,
        suggestion=f"Uploader suggested: {suggested} (a hint only)\n" if suggested else "",
        body=item.body[:6000],
    )
    try:
        raw = chat([{"role": "user", "content": prompt}], response_format=_SCHEMA)
        parsed = json.loads(raw)
    except Exception as exc:
        # The classifier being unavailable must not lose the item. It lands in
        # the fallback, flagged, and can be re-run.
        return [Assignment(FALLBACK, 0.0, f"classifier unavailable: {type(exc).__name__}")]

    known = {c["class_id"] for c in classes}
    out: list[Assignment] = []
    for entry in parsed.get("classes", []):
        class_id = str(entry.get("class_id", "")).strip()
        if class_id not in known or class_id == FALLBACK:
            continue  # a hallucinated class is discarded, not created
        confidence = max(0.0, min(1.0, float(entry.get("confidence", 0) or 0)))
        if confidence < MIN_CONFIDENCE:
            continue
        out.append(Assignment(class_id, confidence, str(entry.get("basis", ""))[:300]))

    if not out:
        return [Assignment(FALLBACK, 0.0, "no class matched with sufficient confidence")]
    return out


async def classify_item(
    session: AsyncSession, scope: Scope, item: Item, suggested: str | None = None
) -> list[Assignment]:
    """Decide and record where an item belongs.

    Pinned assignments are read first and left completely alone — the model is
    still consulted for the rest, but a human's decision is never overwritten
    and never re-evaluated.
    """
    pinned = (
        await session.execute(
            text(
                "SELECT class_id, confidence, basis FROM kb_item_classes "
                "WHERE item_id = :i AND workspace_id = :t AND pinned"
            ),
            {"i": item.id, "t": scope.workspace_id},
        )
    ).all()
    if pinned:
        # A human has filed this. Their decision stands, in full.
        return [Assignment(p.class_id, p.confidence, p.basis or "", pinned=True) for p in pinned]

    classes = await list_classes(session, scope)
    decided = _decide(classes, item, suggested)
    await _replace_auto(session, scope, item.id, decided)
    return decided


async def _replace_auto(
    session: AsyncSession, scope: Scope, item_id: str, assignments: list[Assignment]
) -> None:
    """Swap the automatic assignments, never touching pinned ones."""
    await session.execute(
        text(
            "DELETE FROM kb_item_classes "
            "WHERE item_id = :i AND workspace_id = :t AND NOT pinned"
        ),
        {"i": item_id, "t": scope.workspace_id},
    )
    for a in assignments:
        await session.execute(
            text(
                """
                INSERT INTO kb_item_classes
                    (item_id, class_id, workspace_id, confidence, basis, pinned, actor)
                VALUES (:i, :c, :t, :conf, :basis, false, NULL)
                ON CONFLICT (item_id, class_id) DO NOTHING
                """
            ),
            {
                "i": item_id, "c": a.class_id, "t": scope.workspace_id,
                "conf": a.confidence, "basis": a.basis,
            },
        )


# ------------------------------ human override ------------------------------


async def override(
    session: AsyncSession,
    scope: Scope,
    item_id: str,
    class_ids: list[str],
    actor: str,
) -> list[str]:
    """A person files this item. The decision is sticky and audited (KB-2.b).

    Replaces every assignment, pinned or automatic, with exactly what was
    chosen — and marks the result pinned so no later re-classification, sync
    or re-ingestion can undo it.
    """
    before = (
        await session.execute(
            text("SELECT class_id FROM kb_item_classes WHERE item_id = :i AND workspace_id = :t"),
            {"i": item_id, "t": scope.workspace_id},
        )
    ).scalars().all()

    await session.execute(
        text("DELETE FROM kb_item_classes WHERE item_id = :i AND workspace_id = :t"),
        {"i": item_id, "t": scope.workspace_id},
    )
    for class_id in class_ids:
        await session.execute(
            text(
                """
                INSERT INTO kb_item_classes
                    (item_id, class_id, workspace_id, confidence, basis, pinned, actor)
                VALUES (:i, :c, :t, 1.0, 'set by a person', true, :actor)
                ON CONFLICT (item_id, class_id) DO UPDATE
                SET pinned = true, confidence = 1.0, actor = EXCLUDED.actor,
                    basis = 'set by a person', assigned_at = now()
                """
            ),
            {"i": item_id, "c": class_id, "t": scope.workspace_id, "actor": actor},
        )

    await session.execute(
        text(
            """
            INSERT INTO audit_log (company_id, actor, action, target, metadata)
            VALUES (:t, :actor, 'classification.override', :i, CAST(:meta AS jsonb))
            """
        ),
        {
            "t": scope.workspace_id, "actor": actor, "i": item_id,
            "meta": json.dumps({"from": sorted(before), "to": sorted(class_ids)}),
        },
    )
    return class_ids


async def bulk_override(
    session: AsyncSession,
    scope: Scope,
    item_ids: list[str],
    class_ids: list[str],
    actor: str,
) -> int:
    """The same decision across a filtered set (KB-2.b)."""
    for item_id in item_ids:
        await override(session, scope, item_id, class_ids, actor)
    return len(item_ids)


# -------------------------------- reading --------------------------------


async def classes_for(
    session: AsyncSession, scope: Scope, item_ids: list[str]
) -> dict[str, list[dict[str, Any]]]:
    """Assignments for a batch of items — one query, not N."""
    if not item_ids:
        return {}
    rows = (
        await session.execute(
            text(
                """
                SELECT ic.item_id, ic.class_id, ic.confidence, ic.basis, ic.pinned, ic.actor,
                       COALESCE(c.name, ic.class_id) AS name
                FROM kb_item_classes ic
                LEFT JOIN kb_classes c
                       ON c.class_id = ic.class_id
                      AND (c.workspace_id IS NULL OR c.workspace_id = ic.workspace_id)
                WHERE ic.workspace_id = :t AND ic.item_id = ANY(:ids)
                ORDER BY ic.pinned DESC, ic.confidence DESC
                """
            ),
            {"t": scope.workspace_id, "ids": list(item_ids)},
        )
    ).all()
    out: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        out.setdefault(r.item_id, []).append(
            {
                "class_id": r.class_id,
                "name": r.name,
                "confidence": round(float(r.confidence), 3),
                "basis": r.basis,
                "pinned": r.pinned,
                "actor": r.actor,
            }
        )
    return out


async def needs_review(
    session: AsyncSession, scope: Scope, limit: int = 50
) -> list[dict[str, Any]]:
    """What the KB could not file confidently — the queue KB-2.a asks for."""
    rows = (
        await session.execute(
            text(
                """
                SELECT ic.item_id, ic.class_id, ic.confidence, ic.basis, i.title
                FROM kb_item_classes ic
                JOIN kb_items i ON i.item_id = ic.item_id AND i.status = 'active'
                WHERE ic.workspace_id = :t AND NOT ic.pinned
                  AND (ic.class_id = :fallback OR ic.confidence < :floor)
                ORDER BY ic.confidence ASC
                LIMIT :limit
                """
            ),
            {
                "t": scope.workspace_id, "fallback": FALLBACK,
                "floor": MIN_CONFIDENCE, "limit": limit,
            },
        )
    ).all()
    return [
        {
            "item_id": r.item_id,
            "title": r.title,
            "class_id": r.class_id,
            "confidence": round(float(r.confidence), 3),
            "basis": r.basis,
        }
        for r in rows
    ]
