from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.crypto import seal, unseal

# One-click links inside an email carry a sealed token instead of a session.
# Three protections, because a link in an inbox is a capability:
#   1. sealed with the KMS key  -> cannot be forged or read
#   2. expires                  -> a stale inbox cannot act months later
#   3. single-use (jti in DB)   -> cannot be replayed
#
# The GET that renders a confirmation page must NEVER consume the token: mail
# scanners (Gmail, Outlook, security appliances) pre-fetch every link they see.
# Only the POST consumes it.


async def issue_token(
    session: AsyncSession,
    company_id: str,
    purpose: str,
    payload: dict[str, Any],
    ttl_days: int = 7,
    ttl_minutes: int | None = None,
) -> str:
    """``ttl_minutes`` overrides ``ttl_days`` for short-lived capabilities —
    an OAuth `state` should be valid for minutes, not the week an emailed
    approval link reasonably needs."""
    jti = uuid.uuid4().hex
    delta = timedelta(minutes=ttl_minutes) if ttl_minutes is not None else timedelta(days=ttl_days)
    expires_at = datetime.now(UTC) + delta
    await session.execute(
        text(
            """
            INSERT INTO action_tokens (jti, company_id, purpose, expires_at)
            VALUES (:j, :c, :p, :e)
            """
        ),
        {"j": jti, "c": company_id, "p": purpose, "e": expires_at},
    )
    return seal(json.dumps({"jti": jti, "purpose": purpose, "company_id": company_id, **payload}))


def _decode(token: str) -> dict[str, Any] | None:
    try:
        return json.loads(unseal(token))
    except Exception:
        return None


async def peek_token(session: AsyncSession, token: str, purpose: str) -> dict[str, Any] | None:
    """Validate WITHOUT consuming — safe for a GET rendered to a human."""
    body = _decode(token)
    if not body or body.get("purpose") != purpose:
        return None
    row = (
        await session.execute(
            text("SELECT jti, expires_at, used_at FROM action_tokens WHERE jti = :j"),
            {"j": body.get("jti", "")},
        )
    ).first()
    if row is None or row.used_at is not None or row.expires_at < datetime.now(UTC):
        return None
    return body


async def consume_token(
    session: AsyncSession, token: str, purpose: str, used_by: str = ""
) -> dict[str, Any] | None:
    """Validate and burn the token. Returns None if invalid, expired or reused."""
    body = await peek_token(session, token, purpose)
    if body is None:
        return None
    result = await session.execute(
        text(
            """
            UPDATE action_tokens SET used_at = now(), used_by = :by
            WHERE jti = :j AND used_at IS NULL
            """
        ),
        {"j": body["jti"], "by": used_by},
    )
    if getattr(result, "rowcount", 0) != 1:  # lost a race with a concurrent click
        return None
    return body
