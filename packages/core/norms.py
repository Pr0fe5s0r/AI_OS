from __future__ import annotations

import statistics
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from packages.shared.schema import NormBaseline

# Maturity gates on the RAW sample count (before outlier trimming) — a mix of
# mostly-good observations and a few outliers shouldn't get unfairly demoted
# just because trimming shrank the set used for the actual stats.
_MATURITY_LEARNING_MIN = 5   # fewer than this and a baseline is barely a guess
_MATURITY_STABLE_MIN = 20    # this many before the baseline is dependable

# Zero samples is ambiguous on its own: a company connected two days ago and
# one whose rhythm's end_field will NEVER populate from what's connected look
# identical. These gate how much history has to exist, with the field never
# once populated, before "insufficient" (still learning) becomes "unmeasurable"
# (structurally missing) — see field_presence()/_maybe_unmeasurable() below.
_UNMEASURABLE_MIN_EVENTS = 10
_UNMEASURABLE_MIN_AGE_DAYS = 14.0


def _maturity(n: int) -> str:
    if n < _MATURITY_LEARNING_MIN:
        return "insufficient"
    if n < _MATURITY_STABLE_MIN:
        return "learning"
    return "stable"


def _trim_outliers(obs: list[float]) -> list[float]:
    """IQR trimming: drop points more than 1.5*IQR outside [Q1, Q3]. Needs at
    least 4 points to define quartiles meaningfully; never trims a set down
    to nothing (a single extreme run isn't allowed to erase all signal)."""
    if len(obs) < 4:
        return obs
    s = sorted(obs)
    n = len(s)

    def _quantile(q: float) -> float:
        idx = q * (n - 1)
        lo, hi = int(idx), min(int(idx) + 1, n - 1)
        frac = idx - lo
        return s[lo] + (s[hi] - s[lo]) * frac

    q1, q3 = _quantile(0.25), _quantile(0.75)
    iqr = q3 - q1
    if iqr <= 0:
        return obs
    lo_bound, hi_bound = q1 - 1.5 * iqr, q3 + 1.5 * iqr
    trimmed = [x for x in obs if lo_bound <= x <= hi_bound]
    return trimmed or obs


def _trend_per_period(chronological: list[float]) -> float:
    """Slope of a simple least-squares fit over ``chronological`` (oldest
    first) — metric units per observation. No scipy/numpy dependency,
    consistent with the rest of this module. 0.0 with fewer than 3 points."""
    n = len(chronological)
    if n < 3:
        return 0.0
    xs = list(range(n))
    x_mean = statistics.fmean(xs)
    y_mean = statistics.fmean(chronological)
    denominator = sum((x - x_mean) ** 2 for x in xs)
    if denominator == 0:
        return 0.0
    numerator = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, chronological, strict=True))
    return numerator / denominator


def learn_norms(metric_config: dict, observations: list[float]) -> NormBaseline:
    """Rolling-window stats over a vertical-defined metric. Pure stats — the
    vertical decides what the observations mean and filters to the window.

    ``observations`` must be chronological (oldest first) for the trend fit
    to mean anything — callers extract them ordered by event timestamp.

    Trend-aware: ``mean`` is not a flat average over the whole window, it's
    the regression line's value at the MOST RECENT point — "what's typical
    right now, given the trend" rather than "what was typical over the last
    90 days blended together". ``median``/``std`` are computed on the
    outlier-trimmed set (IQR method) so one freak event can't drag a
    threshold around.
    """
    obs = [float(x) for x in observations if x is not None]
    n = len(obs)  # raw count — maturity's business, not the trimmed stats'
    trimmed = _trim_outliers(obs)

    if not trimmed:
        median = mean = std = trend = 0.0
    else:
        trend = _trend_per_period(trimmed)
        median = float(statistics.median(trimmed))
        std = float(statistics.pstdev(trimmed)) if len(trimmed) > 1 else 0.0
        n_t = len(trimmed)
        # regression line evaluated at the last (most recent) index
        mean = statistics.fmean(trimmed) + trend * (n_t - 1) / 2.0
        # ...but never outside the values we actually measured. Every metric
        # here is a duration or a count, so nothing can be negative, and on a
        # short window a steep trend happily projects one: five resolutions
        # getting faster produced a "typical -1.10 hours", which the Learning
        # page then showed to a human as evidence. Extrapolating past the data
        # is a guess, not a baseline — hold the line at the observed range.
        mean = min(max(mean, min(trimmed)), max(trimmed))

    return NormBaseline(
        company_id=metric_config.get("company_id", "default"),
        metric=metric_config["name"],
        unit=metric_config.get("unit", "hours"),
        n=n,
        median=round(median, 2),
        mean=round(mean, 2),
        std=round(std, 2),
        trend_per_period=round(trend, 4),
        maturity=_maturity(n),
        window_days=int(metric_config.get("window_days", 90)),
        computed_at=datetime.now(UTC),
        scope=metric_config.get("scope", "business"),
    )


_UPSERT_NORM = text(
    """
    INSERT INTO norm_baselines
        (company_id, metric, unit, n, median, mean, std, trend_per_period, maturity,
         window_days, computed_at, scope)
    VALUES
        (:company_id, :metric, :unit, :n, :median, :mean, :std, :trend_per_period, :maturity,
         :window_days, :computed_at, :scope)
    ON CONFLICT (company_id, metric) DO UPDATE SET
        unit = EXCLUDED.unit,
        n = EXCLUDED.n,
        median = EXCLUDED.median,
        mean = EXCLUDED.mean,
        std = EXCLUDED.std,
        trend_per_period = EXCLUDED.trend_per_period,
        maturity = EXCLUDED.maturity,
        window_days = EXCLUDED.window_days,
        computed_at = EXCLUDED.computed_at,
        scope = EXCLUDED.scope
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


async def _definition_observations(
    session: AsyncSession,
    company_id: str,
    defn: dict,
    min_age_days: float = 0.0,
    max_age_days: float | None = None,
    not_before: datetime | None = None,
) -> list[float]:
    """Duration observations (hours) for one rhythm definition, in the
    half-open age window [min_age_days, max_age_days) — the shared extraction
    both compute_baselines (one window) and detect_drift (two windows
    compared against each other) build on.

    ``not_before`` additionally floors the observation's start time — how
    core.reset_norms() makes "recalculate from March" durable: once set, no
    computation ever looks earlier than that, not just the one that ran
    the moment the reset happened.

    Returned chronologically (oldest first) — learn_norms()'s trend fit
    depends on that order.
    """
    import json as _json

    rows = await session.execute(
        text(
            "SELECT timestamp, metadata FROM events WHERE company_id = :c AND source = :s AND type = :t "
            "ORDER BY timestamp ASC"
        ),
        {"c": company_id, "s": defn["source"], "t": defn["type"]},
    )
    now = datetime.now(UTC)
    window = max_age_days if max_age_days is not None else float(defn.get("window_days", 90))
    observations: list[float] = []
    for r in rows:
        md = r.metadata if isinstance(r.metadata, dict) else _json.loads(r.metadata)
        end = _parse_ts((md or {}).get(defn["end_field"]))
        start = _parse_ts(r.timestamp)
        if not end or not start:
            continue
        if not_before is not None and start < not_before:
            continue
        hours = (end - start).total_seconds() / 3600.0
        age_days = (now - start).total_seconds() / 86400.0
        if hours >= 0 and min_age_days <= age_days < window:
            observations.append(hours)
    return observations


async def field_presence(
    session: AsyncSession, company_id: str, source: str, type_: str, end_field: str
) -> dict[str, Any]:
    """Has this rhythm's end_field EVER appeared, populated, on ANY event of
    this (source, type) — across all of history, not the measurement window?

    Zero samples in a window can't tell "hasn't finished yet" from "this
    field will never be set from what's connected" apart; this can, because
    it looks at every event this (source, type) has ever produced, whether or
    not it was ever a candidate observation.
    """
    row = (
        await session.execute(
            text(
                """
                SELECT count(*) AS total,
                       count(*) FILTER (
                           WHERE metadata ? :field AND metadata ->> :field IS NOT NULL
                       ) AS populated,
                       min(timestamp) AS oldest
                FROM events WHERE company_id = :c AND source = :s AND type = :t
                """
            ),
            {"c": company_id, "s": source, "t": type_, "field": end_field},
        )
    ).one()
    oldest = _parse_ts(row.oldest)
    oldest_days = (datetime.now(UTC) - oldest).total_seconds() / 86400.0 if oldest else None
    return {
        "total_events": int(row.total),
        "ever_populated": int(row.populated) > 0,
        "oldest_days": oldest_days,
    }


async def _maybe_unmeasurable(
    session: AsyncSession, company_id: str, defn: dict, baseline: NormBaseline
) -> tuple[NormBaseline, str | None]:
    """Override a zero-sample "insufficient" baseline to "unmeasurable" once
    there's been enough history — enough events, old enough — that the field
    never once appearing stops being a coincidence of timing. Returns the
    (possibly updated) baseline and a human reason, or None if unchanged.

    Self-healing: the moment the field is populated even once, this stops
    firing on its own — no reset or manual step needed to undo it.
    """
    if baseline.n > 0:
        return baseline, None
    presence = await field_presence(session, company_id, defn["source"], defn["type"], defn["end_field"])
    enough_history = (
        presence["total_events"] >= _UNMEASURABLE_MIN_EVENTS
        and presence["oldest_days"] is not None
        and presence["oldest_days"] >= _UNMEASURABLE_MIN_AGE_DAYS
    )
    if presence["ever_populated"] or not enough_history:
        return baseline, None
    reason = (
        f"Read {presence['total_events']} {defn['type']} events going back "
        f"{presence['oldest_days']:.0f} days — none has ever had `{defn['end_field']}` set. "
        "Either this connector doesn't send that field, or the profile is pointing at the wrong one."
    )
    return baseline.model_copy(update={"maturity": "unmeasurable"}), reason


async def get_norm_reset(session: AsyncSession, company_id: str, metric: str) -> datetime | None:
    row = (
        await session.execute(
            text("SELECT reset_before FROM norm_resets WHERE company_id = :c AND metric = :m"),
            {"c": company_id, "m": metric},
        )
    ).first()
    return row.reset_before if row else None


async def reset_norms(
    session: AsyncSession,
    company_id: str,
    metric: str,
    before_date: datetime,
    reset_by: str = "system",
    defn: dict | None = None,
) -> NormBaseline | None:
    """Truncate a metric's rolling window: observations before ``before_date``
    are ignored from now on, not just for the recomputation that runs here.

    Called when a clarification resolves to "recalculate", or directly as an
    agent-invokable action ("we reorganized in March, recalculate from then")
    with no clarification card required first. Persists the floor durably
    (norm_resets) so every FUTURE compute_baselines() call keeps respecting
    it; if ``defn`` (the rhythm's source/type/end_field) is supplied, also
    recomputes immediately so the effect is visible right away.
    """
    await session.execute(
        text(
            """
            INSERT INTO norm_resets (company_id, metric, reset_before, reset_by, created_at)
            VALUES (:c, :m, :b, :by, now())
            ON CONFLICT (company_id, metric) DO UPDATE SET
                reset_before = EXCLUDED.reset_before, reset_by = EXCLUDED.reset_by, created_at = now()
            """
        ),
        {"c": company_id, "m": metric, "b": before_date, "by": reset_by},
    )
    if defn is None:
        return None
    obs = await _definition_observations(session, company_id, defn, not_before=before_date)
    baseline = learn_norms({**defn, "name": metric, "company_id": company_id}, obs)
    await save_norm(session, baseline)
    return baseline


async def compute_baselines(
    session: AsyncSession, company_id: str, norm_definitions: list[dict]
) -> list[NormBaseline]:
    """Extract observations per vertical-defined metric and learn a baseline.

    Generic: a definition names the (source, type) to look at and the metadata
    field holding the end timestamp. Duration = end - event.timestamp, in hours.
    """
    out: list[NormBaseline] = []
    for defn in norm_definitions:
        window = int(defn.get("window_days", 90))
        not_before = await get_norm_reset(session, company_id, defn["name"])
        observations = await _definition_observations(
            session, company_id, defn, max_age_days=window, not_before=not_before
        )
        baseline = learn_norms(
            {"name": defn["name"], "unit": defn.get("unit", "hours"), "window_days": window, "company_id": company_id},
            observations,
        )
        baseline, _ = await _maybe_unmeasurable(session, company_id, defn, baseline)
        await save_norm(session, baseline)
        out.append(baseline)
    return out


async def detect_drift(
    session: AsyncSession,
    company_id: str,
    defn: dict,
    recent_days: float = 14.0,
    prior_days: float = 90.0,
    k: float = 2.0,
) -> dict | None:
    """Compare a rhythm's recent window against its own prior history.

    A simple mean-shift z-test (no scipy dependency, consistent with the rest
    of this module): flags drift when the recent mean has moved more than
    ``k`` prior-standard-deviations away from the prior mean. Returns a report
    dict if drift is detected and there's enough signal on both sides to
    trust it, else None.
    """
    recent = await _definition_observations(session, company_id, defn, min_age_days=0.0, max_age_days=recent_days)
    prior = await _definition_observations(
        session, company_id, defn, min_age_days=recent_days, max_age_days=recent_days + prior_days
    )
    if len(recent) < 5 or len(prior) < 10:
        return None  # not enough signal on one side to trust a comparison

    recent_mean = statistics.fmean(recent)
    prior_mean = statistics.fmean(prior)
    prior_std = statistics.pstdev(prior) if len(prior) > 1 else 0.0
    if prior_std == 0:
        return None

    shift = abs(recent_mean - prior_mean) / prior_std
    if shift < k:
        return None

    return {
        "metric": defn["name"],
        "unit": defn.get("unit", "hours"),
        "recent_mean": round(recent_mean, 2),
        "prior_mean": round(prior_mean, 2),
        "prior_std": round(prior_std, 2),
        "shift_std_devs": round(shift, 2),
        "recent_n": len(recent),
        "prior_n": len(prior),
        "direction": "up" if recent_mean > prior_mean else "down",
    }


async def norm_evidence(
    session: AsyncSession, company_id: str, defn: dict, k: float = 2.0
) -> dict[str, Any]:
    """Show the working: every record that fed one baseline, which ones were
    ignored as outliers, and the line that would raise an alert.

    A number with no provenance is a claim. This is what turns "typically
    1.76 hours" into something a person can check, argue with, and correct —
    each point is a real record they can open.
    """
    import json as _json

    metric = defn["name"]
    not_before = await get_norm_reset(session, company_id, metric)
    window = float(defn.get("window_days", 90))
    now = datetime.now(UTC)

    rows = await session.execute(
        text(
            """
            SELECT id, timestamp, content, metadata FROM events
            WHERE company_id = :c AND source = :s AND type = :t
            ORDER BY timestamp ASC
            """
        ),
        {"c": company_id, "s": defn["source"], "t": defn["type"]},
    )

    points: list[dict[str, Any]] = []
    for r in rows:
        md = r.metadata if isinstance(r.metadata, dict) else _json.loads(r.metadata)
        end = _parse_ts((md or {}).get(defn["end_field"]))
        start = _parse_ts(r.timestamp)
        if not end or not start:
            continue
        if not_before is not None and start < not_before:
            continue
        hours = (end - start).total_seconds() / 3600.0
        age_days = (now - start).total_seconds() / 86400.0
        if hours < 0 or not (0 <= age_days < window):
            continue
        content = (r.content or "").strip()
        points.append(
            {
                "event_id": r.id,
                "title": content.splitlines()[0][:90] if content else r.id,
                "hours": round(hours, 2),
                "started": start.isoformat(),
                "finished": end.isoformat(),
                "url": (md or {}).get("url"),
            }
        )

    values = [p["hours"] for p in points]
    kept = set()
    for v in _trim_outliers(values):
        kept.add(v)
    # mark which points the IQR trim excluded, so the UI can grey them out
    for p in points:
        p["counted"] = p["hours"] in kept

    baseline = learn_norms(
        {"name": metric, "unit": defn.get("unit", "hours"), "window_days": int(window),
         "company_id": company_id},
        values,
    )
    baseline, unmeasurable_reason = await _maybe_unmeasurable(session, company_id, defn, baseline)
    return {
        "metric": metric,
        "unit": baseline.unit,
        "typical": baseline.median,
        "spread": baseline.std,
        "maturity": baseline.maturity,
        "samples": baseline.n,
        "trend_per_period": baseline.trend_per_period,
        # the line past which the watcher engine calls something overdue
        "alert_above": round(baseline.median + k * baseline.std, 2),
        "finished_when": defn.get("end_field"),
        "measured_from": {"source": defn.get("source"), "type": defn.get("type")},
        "window_days": int(window),
        "reset_before": not_before.isoformat() if not_before else None,
        "unmeasurable_reason": unmeasurable_reason,
        "points": points,
    }


async def get_norms(
    session: AsyncSession, company_id: str, scope: str | None = None
) -> list[NormBaseline]:
    clauses = ["company_id = :c"]
    params: dict = {"c": company_id}
    if scope is not None:
        clauses.append("scope = :scope")
        params["scope"] = scope
    res = await session.execute(
        text(
            f"""
            SELECT company_id, metric, unit, n, median, mean, std, trend_per_period, maturity,
                   window_days, computed_at, scope
            FROM norm_baselines
            WHERE {" AND ".join(clauses)}
            ORDER BY metric
            """
        ),
        params,
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
            trend_per_period=float(r.trend_per_period),
            maturity=r.maturity,
            window_days=r.window_days,
            computed_at=r.computed_at,
            scope=r.scope,
        )
        for r in res
    ]


# ------------------------- system self-monitoring norms -------------------------
# Same learn_norms()/save_norm() machinery the business rhythms use — a volume
# baseline is just a metric whose observations are "events in this hour bucket"
# instead of "duration of this event". scope="system" keeps it out of the
# business-facing norms UI/queries.


async def compute_volume_baseline(
    session: AsyncSession, company_id: str, source: str, window_days: int = 14
) -> NormBaseline:
    """Hourly ingest-volume baseline for one connector (events.source)."""
    rows = await session.execute(
        text(
            """
            SELECT count(*) AS n
            FROM events
            WHERE company_id = :c AND source = :s
              AND timestamp > now() - make_interval(days => :window)
              AND timestamp < date_trunc('hour', now())
            GROUP BY date_trunc('hour', timestamp)
            """
        ),
        {"c": company_id, "s": source, "window": window_days},
    )
    observations = [float(r.n) for r in rows]
    baseline = learn_norms(
        {
            "name": f"{source}_events_per_hour",
            "unit": "events/hour",
            "window_days": window_days,
            "company_id": company_id,
            "scope": "system",
        },
        observations,
    )
    await save_norm(session, baseline)
    return baseline


async def current_hourly_volume(session: AsyncSession, company_id: str, source: str) -> int:
    """Events ingested in the last rolling hour — the live reading to compare
    against ``compute_volume_baseline``'s median/std."""
    row = (
        await session.execute(
            text(
                """
                SELECT count(*) AS n FROM events
                WHERE company_id = :c AND source = :s AND timestamp > now() - interval '1 hour'
                """
            ),
            {"c": company_id, "s": source},
        )
    ).one()
    return int(row.n)
