from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# Generic company-scoped runtime settings. The core stores and returns values; it
# has no idea what any key means. The vertical decides which settings exist and
# what the defaults are.


async def get_setting(
    session: AsyncSession, company_id: str, key: str, default: Any = None
) -> Any:
    row = (
        await session.execute(
            text("SELECT value FROM settings WHERE company_id = :c AND key = :k"),
            {"c": company_id, "k": key},
        )
    ).first()
    if row is None:
        return default
    value = row.value
    return json.loads(value) if isinstance(value, str) else value


async def set_setting(session: AsyncSession, company_id: str, key: str, value: Any) -> None:
    await session.execute(
        text(
            """
            INSERT INTO settings (company_id, key, value, updated_at)
            VALUES (:c, :k, CAST(:v AS jsonb), now())
            ON CONFLICT (company_id, key) DO UPDATE SET
                value = EXCLUDED.value, updated_at = now()
            """
        ),
        {"c": company_id, "k": key, "v": json.dumps(value)},
    )
