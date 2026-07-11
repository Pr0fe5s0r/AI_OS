from __future__ import annotations

import statistics
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from packages.shared.schema import NormBaseline


def learn_norms(metric_config: dict, observations: list[float]) -> NormBaseline:
    """Rolling-window stats over a vertical-defined metric. Pure stats — the
    vertical decides what the observations mean and filters to the window."""
    obs = [float(x) for x in observations if x is not None]
    n = len(obs)
    if n == 0:
        median = mean = std = 0.0
    else:
        median = float(statistics.median(obs))
        mean = float(statistics.fmean(obs))
        std = float(statistics.pstdev(obs)) if n > 1 else 0.0

    return NormBaseline(
        company_id=metric_config.get("company_id", "default"),
        metric=metric_config["name"],
        unit=metric_config.get("unit", "hours"),
        n=n,
        median=round(median, 2),
        mean=round(mean, 2),
        std=round(std, 2),
        window_days=int(metric_config.get("window_days", 90)),
        computed_at=datetime.now(UTC),
    )


_UPSERT_NORM = text(
    """
    INSERT INTO norm_baselines
        (company_id, metric, unit, n, median, mean, std, window_days, computed_at)
    VALUES
        (:company_id, :metric, :unit, :n, :median, :mean, :std, :window_days, :computed_at)
    ON CONFLICT (company_id, metric) DO UPDATE SET
        unit = EXCLUDED.unit,
        n = EXCLUDED.n,
        median = EXCLUDED.median,
        mean = EXCLUDED.mean,
        std = EXCLUDED.std,
        window_days = EXCLUDED.window_days,
        computed_at = EXCLUDED.computed_at
    """
)


async def save_norm(session: AsyncSession, baseline: NormBaseline) -> None:
    await session.execute(_UPSERT_NORM, baseline.model_dump())


def _parse_ts(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


async def compute_baselines(
    session: AsyncSession, company_id: str, norm_definitions: list[dict]
) -> list[NormBaseline]:
    """Extract observations per vertical-defined metric and learn a baseline.

    Generic: a definition names the (source, type) to look at and the metadata
    field holding the end timestamp. Duration = end - event.timestamp, in hours.
    """
    import json as _json

    now = datetime.now(UTC)
    out: list[NormBaseline] = []

    for defn in norm_definitions:
        rows = await session.execute(
            text(
                """
                SELECT timestamp, metadata FROM events
                WHERE company_id = :c AND source = :s AND type = :t
                """
            ),
            {"c": company_id, "s": defn["source"], "t": defn["type"]},
        )
        window = int(defn.get("window_days", 90))
        observations: list[float] = []
        for r in rows:
            md = r.metadata if isinstance(r.metadata, dict) else _json.loads(r.metadata)
            end = _parse_ts((md or {}).get(defn["end_field"]))
            start = _parse_ts(r.timestamp)
            if not end or not start:
                continue
            hours = (end - start).total_seconds() / 3600.0
            age_days = (now - start).total_seconds() / 86400.0
            if hours >= 0 and age_days <= window:
                observations.append(hours)

        baseline = learn_norms(
            {
                "name": defn["name"],
                "unit": defn.get("unit", "hours"),
                "window_days": window,
                "company_id": company_id,
            },
            observations,
        )
        await save_norm(session, baseline)
        out.append(baseline)

    return out


async def get_norms(session: AsyncSession, company_id: str) -> list[NormBaseline]:
    res = await session.execute(
        text(
            """
            SELECT company_id, metric, unit, n, median, mean, std, window_days, computed_at
            FROM norm_baselines
            WHERE company_id = :c
            ORDER BY metric
            """
        ),
        {"c": company_id},
    )
    return [
        NormBaseline(
            company_id=r.company_id,
            metric=r.metric,
            unit=r.unit,
            n=r.n,
            median=float(r.median),
            mean=float(r.mean),
            std=float(r.std),
            window_days=r.window_days,
            computed_at=r.computed_at,
        )
        for r in res
    ]
