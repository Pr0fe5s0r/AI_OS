from __future__ import annotations

import json

from arq import create_pool

from packages.core.pipeline import redis_settings

# Checkpoint 2, part C: a lightweight "the feed changed" signal over Redis
# pub/sub. GET /api/feed/stream (apps.api.main) subscribes per company_id
# and forwards it as an SSE event; the frontend reacts by refetching
# /api/feed. The payload is deliberately minimal (a nudge to refetch, not a
# push of situation data itself) so the stream can never drift out of sync
# with the system of record in Postgres.

CHANNEL = "feed:{company_id}"


async def publish_feed_update(company_id: str, situation_count: int) -> None:
    pool = await create_pool(redis_settings())
    try:
        await pool.publish(
            CHANNEL.format(company_id=company_id), json.dumps({"situations": situation_count})
        )
    finally:
        await pool.aclose()
