from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.search import Trace
from packages.shared.schema import Scope

# ---------------------------------------------------------------------------
# Storing and reading traces.
#
# Separate from search itself so that retrieval has no opinion about whether
# anyone is recording it. Search builds the trace; this decides where it goes.
# ---------------------------------------------------------------------------


async def record(
    session: AsyncSession,
    scope: Scope,
    trace: Trace,
    via: str = "session",
    actor: str | None = None,
) -> str:
    """Persist a trace. Never raises into the caller.

    A failure to record why an answer happened must not stop the answer being
    returned — observability that can break the thing it observes is worse
    than none.
    """
    try:
        await session.execute(
            text(
                """
                INSERT INTO query_traces
                    (trace_id, workspace_id, collection_id, query, config, filters,
                     semantic, keyword, fused, returned, timings_ms, degraded,
                     result_count, duration_ms, via, actor)
                VALUES
                    (:tid, :ws, :coll, :q, CAST(:config AS jsonb), CAST(:filters AS jsonb),
                     CAST(:semantic AS jsonb), CAST(:keyword AS jsonb), CAST(:fused AS jsonb),
                     CAST(:returned AS jsonb), CAST(:timings AS jsonb), :degraded,
                     :count, :duration, :via, :actor)
                ON CONFLICT (trace_id) DO NOTHING
                """
            ),
            {
                "tid": trace.trace_id,
                "ws": scope.workspace_id,
                "coll": scope.collection_id,
                "q": trace.query,
                "config": json.dumps(trace.config),
                "filters": json.dumps(trace.filters),
                "semantic": json.dumps(trace.semantic),
                "keyword": json.dumps(trace.keyword),
                "fused": json.dumps(trace.fused),
                "returned": json.dumps(trace.returned),
                "timings": json.dumps(trace.timings_ms),
                "degraded": trace.degraded,
                "count": len(trace.returned),
                "duration": trace.duration_ms,
                "via": via,
                "actor": actor,
            },
        )
    except Exception:
        pass
    return trace.trace_id


def _summary(r: Any) -> dict[str, Any]:
    return {
        "trace_id": r.trace_id,
        "query": r.query,
        "collection_id": r.collection_id,
        "result_count": r.result_count,
        "duration_ms": r.duration_ms,
        "degraded": r.degraded,
        "via": r.via,
        "actor": r.actor,
        "created_at": r.created_at.isoformat(),
    }


async def list_traces(
    session: AsyncSession,
    scope: Scope,
    limit: int = 50,
    only_empty: bool = False,
    only_degraded: bool = False,
) -> list[dict[str, Any]]:
    """Recent queries. The filters exist because the interesting traces are
    the ones that returned nothing or ran degraded."""
    clauses = ["workspace_id = :ws"]
    params: dict[str, Any] = {"ws": scope.workspace_id, "limit": limit}
    if scope.collection_id is not None:
        clauses.append("collection_id = :coll")
        params["coll"] = scope.collection_id
    if only_empty:
        clauses.append("result_count = 0")
    if only_degraded:
        clauses.append("degraded IS NOT NULL")

    rows = (
        await session.execute(
            text(
                f"""
                SELECT trace_id, query, collection_id, result_count, duration_ms,
                       degraded, via, actor, created_at
                FROM query_traces
                WHERE {" AND ".join(clauses)}
                ORDER BY created_at DESC
                LIMIT :limit
                """
            ),
            params,
        )
    ).all()
    return [_summary(r) for r in rows]


async def get_trace(
    session: AsyncSession, scope: Scope, trace_id: str
) -> dict[str, Any] | None:
    """One trace in full — every arm, every score, every timing."""
    row = (
        await session.execute(
            text(
                """
                SELECT trace_id, query, collection_id, config, filters, semantic,
                       keyword, fused, returned, timings_ms, degraded, result_count,
                       duration_ms, via, actor, created_at
                FROM query_traces
                WHERE workspace_id = :ws AND trace_id = :tid
                """
            ),
            {"ws": scope.workspace_id, "tid": trace_id},
        )
    ).first()
    if row is None:
        return None

    def js(value: Any) -> Any:
        return value if isinstance(value, list | dict) else json.loads(value or "null")

    return {
        **_summary(row),
        "config": js(row.config),
        "filters": js(row.filters),
        "semantic": js(row.semantic),
        "keyword": js(row.keyword),
        "fused": js(row.fused),
        "returned": js(row.returned),
        "timings_ms": js(row.timings_ms),
    }


async def stats(session: AsyncSession, scope: Scope, hours: int = 24) -> dict[str, Any]:
    """How retrieval has been behaving — the numbers a console header shows."""
    row = (
        await session.execute(
            text(
                """
                SELECT count(*) AS queries,
                       COALESCE(avg(duration_ms), 0) AS avg_ms,
                       COALESCE(
                           percentile_disc(0.95) WITHIN GROUP (ORDER BY duration_ms), 0
                       ) AS p95_ms,
                       count(*) FILTER (WHERE result_count = 0) AS empty,
                       count(*) FILTER (WHERE degraded IS NOT NULL) AS degraded
                FROM query_traces
                WHERE workspace_id = :ws
                  AND created_at > now() - make_interval(hours => :hours)
                """
            ),
            {"ws": scope.workspace_id, "hours": hours},
        )
    ).one()
    return {
        "window_hours": hours,
        "queries": int(row.queries),
        "avg_ms": round(float(row.avg_ms), 1),
        "p95_ms": int(row.p95_ms),
        "empty": int(row.empty),
        "degraded": int(row.degraded),
    }


__all__ = ["get_trace", "list_traces", "record", "stats"]
