from __future__ import annotations

import apps.api.main as api_main

# "Scan now" has to actually scan.
#
# The button called /api/analyze, which re-reasons over events ALREADY in the
# store and never polls a connector. Next to "N events watched", a control
# named "Scan now" that cannot discover a single new record is a promise the
# product does not keep: a pull request opened minutes earlier stayed invisible
# until the 15-minute cron came around.


def test_scan_polls_before_it_reasons(monkeypatch) -> None:
    """Order matters as much as the call: analysing first would report on the
    state that existed before the click."""
    calls: list[str] = []

    async def fake_ingest(session, profile, only_source=None, wait=None):
        calls.append(f"ingest(wait={wait is not None})")
        return {"github": 3}

    async def fake_analysis(session, profile):
        calls.append("analyse")
        return {"situations": 1, "raised": []}

    monkeypatch.setattr(api_main, "trigger_ingest", fake_ingest)
    monkeypatch.setattr(api_main, "run_analysis", fake_analysis)

    import asyncio

    result = asyncio.run(api_main.scan_now.__wrapped__(  # type: ignore[attr-defined]
        company_id="test-scan", profile=_profile(),
    )) if hasattr(api_main.scan_now, "__wrapped__") else asyncio.run(
        api_main.scan_now(company_id="test-scan", profile=_profile())
    )

    assert calls == ["ingest(wait=True)", "analyse"]
    assert result["ingested"] == {"github": 3}


def test_analyze_still_does_not_poll(monkeypatch) -> None:
    """The two endpoints stay different on purpose. /api/analyze is the cheap
    re-reason used after a webhook has already delivered the data; making it
    poll would turn every call into an outbound API round-trip."""
    called: list[str] = []

    async def fake_ingest(*a, **k):
        called.append("ingest")
        return {}

    async def fake_analysis(session, profile):
        called.append("analyse")
        return {"situations": 0, "raised": []}

    monkeypatch.setattr(api_main, "trigger_ingest", fake_ingest)
    monkeypatch.setattr(api_main, "run_analysis", fake_analysis)

    import asyncio

    asyncio.run(api_main.analyze(company_id="test-scan", profile=_profile()))
    assert called == ["analyse"]


def _profile():
    from packages.core.profile import Profile

    return Profile(company_id="test-scan")
