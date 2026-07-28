from __future__ import annotations

import json

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.crypto import seal, unseal
from packages.shared.schema import Connection

# Encrypted credential storage. Tokens are sealed via the KMS-style abstraction
# before they touch the DB and are only unsealed at call time. Generic: the core
# has no idea what "github" or "slack" mean — they're just source keys.


async def save_credential(
    session: AsyncSession, company_id: str, source: str, token: str, config: dict
) -> None:
    await session.execute(
        text(
            """
            INSERT INTO credentials (company_id, source, sealed_token, config)
            VALUES (:c, :s, :t, CAST(:cfg AS jsonb))
            ON CONFLICT (company_id, source) DO UPDATE SET
                sealed_token = EXCLUDED.sealed_token,
                config = EXCLUDED.config
            """
        ),
        {"c": company_id, "s": source, "t": seal(token) if token else "", "cfg": json.dumps(config)},
    )


async def get_credential(
    session: AsyncSession, company_id: str, source: str
) -> tuple[str, dict] | None:
    row = (
        await session.execute(
            text("SELECT sealed_token, config FROM credentials WHERE company_id=:c AND source=:s"),
            {"c": company_id, "s": source},
        )
    ).first()
    if row is None:
        return None
    cfg = row.config if isinstance(row.config, dict) else json.loads(row.config)
    token = unseal(row.sealed_token) if row.sealed_token else ""
    return token, (cfg or {})


async def list_connections(session: AsyncSession, company_id: str) -> list[Connection]:
    rows = await session.execute(
        text(
            """
            SELECT source, config, created_at, (sealed_token <> '') AS has_token
            FROM credentials WHERE company_id = :c ORDER BY source
            """
        ),
        {"c": company_id},
    )
    out: list[Connection] = []
    for r in rows:
        cfg = r.config if isinstance(r.config, dict) else json.loads(r.config)
        out.append(
            Connection(
                tenant_id=company_id,
                source=r.source,
                connected=True,
                config=cfg or {},
                created_at=r.created_at,
            )
        )
    return out


async def delete_credential(session: AsyncSession, company_id: str, source: str) -> None:
    await session.execute(
        text("DELETE FROM credentials WHERE company_id=:c AND source=:s"),
        {"c": company_id, "s": source},
    )
