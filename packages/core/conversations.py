from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from packages.shared.schema import Message

# Minimal conversations/messages backbone (checkpoint 6, part B). Enough for
# a clarification to be a REAL persisted object on both the Feed and in chat —
# not a simulated rendering of it. No streaming, no tool-use loop: that's the
# full CP4 agent loop, a separate and larger piece of work.
#
# One conversation per company for now (the "default" thread) — multi-thread
# management is a CP4 UI concern, not needed for this checkpoint's proof.


async def get_or_create_default_conversation(session: AsyncSession, company_id: str) -> int:
    row = (
        await session.execute(
            text(
                "SELECT id FROM conversations WHERE company_id = :c ORDER BY id LIMIT 1"
            ),
            {"c": company_id},
        )
    ).first()
    if row is not None:
        return int(row.id)
    row = (
        await session.execute(
            text(
                "INSERT INTO conversations (company_id, title) VALUES (:c, 'Agent') RETURNING id"
            ),
            {"c": company_id},
        )
    ).one()
    return int(row.id)


async def add_message(
    session: AsyncSession,
    conversation_id: int,
    role: str,
    content: str = "",
    artifacts: list[dict[str, Any]] | None = None,
) -> Message:
    row = (
        await session.execute(
            text(
                """
                INSERT INTO messages (conversation_id, role, content, artifacts)
                VALUES (:cid, :role, :content, CAST(:artifacts AS jsonb))
                RETURNING id, conversation_id, role, content, artifacts, created_at
                """
            ),
            {
                "cid": conversation_id,
                "role": role,
                "content": content,
                "artifacts": json.dumps(artifacts or []),
            },
        )
    ).one()
    await session.execute(
        text("UPDATE conversations SET updated_at = now() WHERE id = :cid"), {"cid": conversation_id}
    )
    art = row.artifacts if isinstance(row.artifacts, list) else json.loads(row.artifacts)
    return Message(
        id=row.id, conversation_id=row.conversation_id, role=row.role,
        content=row.content, artifacts=art, created_at=row.created_at,
    )


async def list_messages(
    session: AsyncSession, company_id: str, conversation_id: int, limit: int = 200
) -> list[Message]:
    """Scoped: only returns messages if the conversation belongs to this company."""
    owns = (
        await session.execute(
            text("SELECT 1 FROM conversations WHERE id = :cid AND company_id = :c"),
            {"cid": conversation_id, "c": company_id},
        )
    ).first()
    if owns is None:
        return []
    rows = await session.execute(
        text(
            """
            SELECT id, conversation_id, role, content, artifacts, created_at
            FROM messages WHERE conversation_id = :cid ORDER BY created_at, id LIMIT :l
            """
        ),
        {"cid": conversation_id, "l": limit},
    )
    out = []
    for r in rows:
        art = r.artifacts if isinstance(r.artifacts, list) else json.loads(r.artifacts)
        out.append(
            Message(
                id=r.id, conversation_id=r.conversation_id, role=r.role,
                content=r.content, artifacts=art, created_at=r.created_at,
            )
        )
    return out


async def has_clarification_message(session: AsyncSession, conversation_id: int, situation_id: str) -> bool:
    """So a detector never posts the same clarification into chat twice."""
    row = (
        await session.execute(
            text(
                """
                SELECT 1 FROM messages
                WHERE conversation_id = :cid
                  AND artifacts @> CAST(:needle AS jsonb)
                LIMIT 1
                """
            ),
            {"cid": conversation_id, "needle": json.dumps([{"situation_id": situation_id}])},
        )
    ).first()
    return row is not None

