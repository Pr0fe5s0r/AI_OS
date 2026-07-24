from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import text

import packages.core.triage as T
from packages.core.db import Session
from packages.core.profile import Profile
from packages.core.triage import run_triage

# The universal inbound-triage rule: judge fresh records from ANY source and
# raise a situation only for what needs a person. Source-agnostic — these seed
# "slack" but nothing in the engine special-cases it.

_CO = "test-triage"


async def _seed(session, rows):
    for t in ("events", "record_triage", "situations"):
        await session.execute(text(f"DELETE FROM {t} WHERE company_id = :c"), {"c": _CO})
    now = datetime.now(UTC)
    for eid, src, content in rows:
        await session.execute(
            text(
                """
                INSERT INTO events (id, company_id, source, type, actor_id, actor_name,
                                     timestamp, content, metadata, content_tsv, backfilled)
                VALUES (:id, :c, :s, 'message', 'u', 'Priya', :ts, :content,
                        '{}'::jsonb, to_tsvector('english', :content), false)
                ON CONFLICT (company_id, id, timestamp) DO NOTHING
                """
            ),
            {"id": eid, "c": _CO, "s": src, "ts": now, "content": content},
        )
    await session.commit()


def _verdict(need: bool, conf: float = 0.9) -> dict:
    return {"needs_action": need, "severity": "high", "category": "bug",
            "title": "issue", "summary": "s", "confidence": conf}


async def test_flags_a_bug_report_not_chatter(monkeypatch):
    monkeypatch.setattr(T, "_classify", lambda p, r: _verdict("broken" in r["content"]))
    async with Session() as s:
        await _seed(s, [
            ("m1", "slack", "the chatbot is broken, shows an error"),
            ("m2", "slack", "good morning team, who wants coffee"),
        ])
        raised = await run_triage(s, Profile(company_id=_CO))
        await s.commit()
    assert len(raised) == 1, "only the bug report should be raised"
    r = raised[0]
    assert r.rule == "needs_triage" and r.kind == "triage" and "m1" in r.id
    async with Session() as s:
        n = (await s.execute(text("SELECT count(*) FROM record_triage WHERE company_id=:c"), {"c": _CO})).scalar_one()
    assert n == 2, "every judged record is recorded, not just the actionable one"


async def test_a_recorded_record_is_never_re_judged(monkeypatch):
    calls = {"n": 0}

    def _spy(p, r):
        calls["n"] += 1
        return _verdict(False)

    monkeypatch.setattr(T, "_classify", _spy)
    async with Session() as s:
        await _seed(s, [("m1", "slack", "hello")])
        prof = Profile(company_id=_CO)
        await run_triage(s, prof)
        await s.commit()
        first = calls["n"]
        await run_triage(s, prof)
        await s.commit()
    assert first == 1 and calls["n"] == first, "the ledger must prevent a second LLM call"


async def test_low_confidence_is_not_raised(monkeypatch):
    monkeypatch.setattr(T, "_classify", lambda p, r: _verdict(True, conf=0.3))
    async with Session() as s:
        await _seed(s, [("m1", "slack", "maybe something is off?")])
        raised = await run_triage(s, Profile(company_id=_CO))
        await s.commit()
    assert raised == [], "an unsure judgement waits, it does not invent work"


async def test_it_is_source_agnostic(monkeypatch):
    # the SAME rule fires on a github record, a zendesk record, anything
    monkeypatch.setattr(T, "_classify", lambda p, r: _verdict(True))
    async with Session() as s:
        await _seed(s, [("z1", "zendesk", "customer cannot log in"), ("g1", "github", "crash on save")])
        raised = await run_triage(s, Profile(company_id=_CO))
        await s.commit()
    assert {r.evidence[0].source for r in raised} == {"zendesk", "github"}


async def test_status_bearing_records_are_skipped_as_already_tracked(monkeypatch):
    # A record that carries a lifecycle status (a GitHub issue's `state`) is
    # already tracked work — triage skips it. A statusless inbound (a Slack
    # message) has nowhere else to go, so it IS triaged.
    monkeypatch.setattr(T, "_classify", lambda p, r: _verdict(True))
    async with Session() as s:
        for t in ("events", "record_triage", "situations"):
            await s.execute(text(f"DELETE FROM {t} WHERE company_id = :c"), {"c": _CO})
        now = datetime.now(UTC)
        for eid, src, md, content in [
            ("g1", "github", '{"state":"open"}', "crash on save"),
            ("m1", "slack", "{}", "the chatbot is broken"),
        ]:
            await s.execute(
                text(
                    "INSERT INTO events (id,company_id,source,type,actor_id,actor_name,timestamp,"
                    "content,metadata,content_tsv,backfilled) VALUES (:id,:c,:s,'x','u','x',:ts,"
                    ":content, CAST(:md AS jsonb), to_tsvector('english',:content), false) "
                    "ON CONFLICT (company_id,id,timestamp) DO NOTHING"
                ),
                {"id": eid, "c": _CO, "s": src, "ts": now, "content": content, "md": md},
            )
        await s.commit()
        raised = await run_triage(s, Profile(company_id=_CO, things={"status_field": "state"}))
        await s.commit()
    assert {r.evidence[0].source for r in raised} == {"slack"}, "the status-bearing record must be skipped"
