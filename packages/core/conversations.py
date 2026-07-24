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


async def create_conversation(
    session: AsyncSession, company_id: str, title: str = "New thread"
) -> int:
    """Start a fresh thread.

    Not a nicety. Every turn replays the last 20 messages, so a single endless
    thread meant an answer from when a workspace was empty was still being fed
    to the model days later — and the only way to escape it was for somebody to
    delete rows. A new thread is how a person says "forget that, start again".
    """
    row = (
        await session.execute(
            text(
                "INSERT INTO conversations (company_id, title) VALUES (:c, :t) RETURNING id"
            ),
            {"c": company_id, "t": (title or "New thread").strip()[:120]},
        )
    ).one()
    return int(row.id)


async def list_conversations(
    session: AsyncSession, company_id: str, limit: int = 50
) -> list[dict[str, Any]]:
    """Every thread in this workspace, most recently used first, with enough
    to render a history list without a second query per row."""
    rows = await session.execute(
        text(
            """
            SELECT c.id, c.title, c.created_at, c.updated_at,
                   count(m.id) AS message_count,
                   max(m.created_at) AS last_message_at,
                   (
                     SELECT content FROM messages
                     WHERE conversation_id = c.id AND role = 'user'
                     ORDER BY created_at LIMIT 1
                   ) AS opening
            FROM conversations c
            LEFT JOIN messages m ON m.conversation_id = c.id
            WHERE c.company_id = :c
            GROUP BY c.id
            ORDER BY coalesce(max(m.created_at), c.created_at) DESC
            LIMIT :l
            """
        ),
        {"c": company_id, "l": limit},
    )
    out = []
    for r in rows:
        # A thread nobody titled is named by what was asked first — far more
        # use in a list than "New thread" repeated eleven times.
        opening = (r.opening or "").strip().splitlines()[0][:80] if r.opening else ""
        out.append(
            {
                "id": int(r.id),
                "title": opening or (r.title or "New thread"),
                "message_count": int(r.message_count or 0),
                "created_at": r.created_at.isoformat(),
                "last_message_at": r.last_message_at.isoformat() if r.last_message_at else None,
            }
        )
    return out


async def conversation_exists(session: AsyncSession, company_id: str, conversation_id: int) -> bool:
    """Scoped: a thread id from another workspace must not resolve."""
    row = (
        await session.execute(
            text("SELECT 1 FROM conversations WHERE id = :i AND company_id = :c"),
            {"i": conversation_id, "c": company_id},
        )
    ).first()
    return row is not None


async def touch_conversation(session: AsyncSession, conversation_id: int) -> None:
    await session.execute(
        text("UPDATE conversations SET updated_at = now() WHERE id = :i"),
        {"i": conversation_id},
    )


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

