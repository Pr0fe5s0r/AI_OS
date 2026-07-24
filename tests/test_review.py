from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy import text

from packages.core import review as R
from packages.core.db import Session
from packages.core.situations import list_situations

# The reviewer engine: a grounded specialist fan-out that READS a record and
# raises structured, line-anchored findings — the blog's PR reviewer, made
# generic. These pin the contract that makes it trustworthy: findings land as
# anchored situations, a docs nit cannot claim `critical`, two concerns flagging
# one spot collapse to the most confident, and an unchanged record is never
# re-paid for.

_CO = "test-review"

_REVIEWER = {
    "key": "pr_review",
    "select": {"thing_type": "pull_request", "where": {"status": "open"}},
    "ground": {"retrieve": {"top_k": 0}},  # no embeddings in the test provider
    "concerns": [
        {"key": "security", "severity_ceiling": "critical", "prompt": "sec"},
        {"key": "docs", "severity_ceiling": "low", "prompt": "docs"},
    ],
}


async def _seed_pr(session, *, updated_at: str) -> dict:
    await session.execute(text("DELETE FROM events WHERE company_id = :c"), {"c": _CO})
    await session.execute(text("DELETE FROM situations WHERE company_id = :c"), {"c": _CO})
    await session.execute(text("DELETE FROM record_reviews WHERE company_id = :c"), {"c": _CO})
    now = datetime.now(UTC)
    md = {"state": "open", "updated_at": updated_at, "url": "https://github.com/o/r/pull/7"}
    await session.execute(
        text(
            """
            INSERT INTO events (id, company_id, source, type, actor_id, actor_name,
                                 timestamp, content, metadata, content_tsv, backfilled)
            VALUES ('7', :c, 'github', 'pull_request', 'u', 'u', :ts,
                    :content, CAST(:md AS jsonb), to_tsvector('english', :content), false)
            ON CONFLICT (company_id, id, timestamp) DO NOTHING
            """
        ),
        {"c": _CO, "ts": now, "content": "Add login\n\nexecutes raw SQL from user input", "md": json.dumps(md)},
    )
    await session.commit()
    return {
        "id": "7", "company_id": _CO, "source": "github", "type": "pull_request",
        "content": "Add login\n\nexecutes raw SQL from user input",
        "title": "Add login", "metadata": md, "timestamp": now,
    }


def _answer(*findings: dict) -> str:
    return json.dumps({"findings": list(findings)})


async def test_findings_land_as_anchored_situations_and_severity_is_clamped(monkeypatch):
    async with Session() as session:
        thing = await _seed_pr(session, updated_at="2026-07-24T10:00:00Z")

        # security concern returns a real critical; docs concern OVER-claims
        # critical and must be clamped down to its `low` ceiling.
        answers = iter([
            _answer({
                "severity": "critical", "category": "injection", "title": "SQL injection in login",
                "file_path": "auth.py", "line_start": 40, "line_end": 40,
                "confidence": 0.92, "rationale": "line 40 interpolates user input into SQL",
                "suggestion": "use a parameterized query",
            }),
            _answer({
                "severity": "critical", "category": "missing-doc", "title": "login() undocumented",
                "file_path": "auth.py", "line_start": 30, "confidence": 0.5,
                "rationale": "new public function", "suggestion": "add a docstring",
            }),
        ])
        monkeypatch.setattr(R, "chat", lambda *a, **k: next(answers))

        out = await R.review_thing(session, _CO, _REVIEWER, thing, activity_field="updated_at")
        await session.commit()

    assert out["status"] == "reviewed" and out["findings"] == 2, out
    async with Session() as session:
        sits = await list_situations(session, _CO)
    by_rule = {s.rule: s for s in sits}
    sec = by_rule["review.pr_review.security"]
    assert sec.severity == "critical" and sec.file_path == "auth.py" and sec.line_start == 40
    assert sec.confidence == 0.92 and sec.category == "injection"
    assert sec.kind == "review", "a finding tagged business gets swept by the watcher engine"
    docs = by_rule["review.pr_review.docs"]
    assert docs.severity == "low", f"docs nit not clamped: {docs.severity}"


async def test_watcher_resolve_stale_does_not_sweep_review_findings(monkeypatch):
    # The watcher engine retires every kind="business" situation not in its own
    # active set. Review findings must NOT be caught by that — they were, once,
    # and vanished a cron cycle after being raised. This pins the fix: a
    # business-kind resolve_stale with an EMPTY active set leaves review findings
    # open and only the reviewer's own pass retires them.
    from packages.core.situations import resolve_stale

    async with Session() as session:
        thing = await _seed_pr(session, updated_at="2026-07-24T10:00:00Z")
        monkeypatch.setattr(R, "chat", lambda *a, **k: _answer({
            "severity": "high", "title": "unsafe call", "file_path": "x.py",
            "line_start": 5, "confidence": 0.7, "rationale": "a"}))
        await R.review_thing(session, _CO, _REVIEWER, thing, activity_field="updated_at")
        # a watcher pass that raised NOTHING this cycle
        await resolve_stale(session, _CO, active_ids=[], kind="business")
        await session.commit()

    async with Session() as session:
        sits = await list_situations(session, _CO)
    review = [s for s in sits if s.rule.startswith("review.")]
    assert review and all(s.status != "resolved" for s in review), \
        "watcher resolve_stale must not touch review findings"


async def test_same_spot_from_two_concerns_dedupes_to_highest_confidence(monkeypatch):
    async with Session() as session:
        thing = await _seed_pr(session, updated_at="2026-07-24T10:00:00Z")
        answers = iter([
            _answer({"severity": "high", "title": "unsafe call", "file_path": "x.py",
                     "line_start": 5, "confidence": 0.6, "rationale": "a"}),
            _answer({"severity": "low", "title": "same spot other words", "file_path": "x.py",
                     "line_start": 5, "confidence": 0.3, "rationale": "b"}),
        ])
        monkeypatch.setattr(R, "chat", lambda *a, **k: next(answers))
        out = await R.review_thing(session, _CO, _REVIEWER, thing, activity_field="updated_at")
        await session.commit()

    assert out["findings"] == 1, "two findings at one anchor should collapse to one"
    async with Session() as session:
        sits = await list_situations(session, _CO)
    open_sits = [s for s in sits if s.status != "resolved"]
    assert len(open_sits) == 1 and open_sits[0].confidence == 0.6


async def test_unchanged_record_is_not_re_reviewed(monkeypatch):
    calls = {"n": 0}

    def _counting_chat(*a, **k):
        calls["n"] += 1
        return _answer()  # no findings

    async with Session() as session:
        thing = await _seed_pr(session, updated_at="2026-07-24T10:00:00Z")
        monkeypatch.setattr(R, "chat", _counting_chat)
        await R.review_thing(session, _CO, _REVIEWER, thing, activity_field="updated_at")
        await session.commit()
        first = calls["n"]
        # same activity stamp -> the ledger short-circuits, no LLM calls
        second = await R.review_thing(session, _CO, _REVIEWER, thing, activity_field="updated_at")
        await session.commit()

    assert first == 2, "two concerns should each be called once on first review"
    assert second["status"] == "unchanged", second
    assert calls["n"] == first, "an unchanged PR must not hit the model again"


async def test_a_new_commit_advances_the_stamp_and_triggers_re_review(monkeypatch):
    async with Session() as session:
        thing = await _seed_pr(session, updated_at="2026-07-24T10:00:00Z")
        monkeypatch.setattr(R, "chat", lambda *a, **k: _answer())
        await R.review_thing(session, _CO, _REVIEWER, thing, activity_field="updated_at")
        await session.commit()
        thing2 = {**thing, "metadata": {**thing["metadata"], "updated_at": "2026-07-24T12:00:00Z"}}
        out = await R.review_thing(session, _CO, _REVIEWER, thing2, activity_field="updated_at")
        await session.commit()
    assert out["status"] == "reviewed", "an advanced activity stamp must re-review"


# ------------------------------ Step 5: posting ------------------------------
# One PR -> one inline REQUEST_CHANGES review, assembled from all its findings
# and put through the SAME public-action gate every write uses (queued for a
# person, never auto-posted).

from datetime import UTC as _UTC  # noqa: E402

from apps.common.analysis import assemble_pr_review, request_pr_review  # noqa: E402
from packages.connectors.github import MOVES, TARGET_PARAMS, TARGET_URL_PATTERN  # noqa: E402
from packages.core.profile import Profile  # noqa: E402
from packages.shared.schema import Evidence, Situation  # noqa: E402

_MOVE = "request_changes_on_pull_request"


def _review_profile() -> Profile:
    return Profile(
        company_id=_CO,
        moves={
            "registry": {_MOVE: MOVES[_MOVE]},
            "targets": {"github": {"pattern": TARGET_URL_PATTERN, "params": list(TARGET_PARAMS)}},
        },
        reviewers=[{"key": "pr_review", "post": {"move": _MOVE}}],
    )


def _finding(concern, severity, line, title) -> Situation:
    return Situation(
        id=f"review:pr_review:7:{concern}:{title[:4]}", company_id=_CO,
        rule=f"review.pr_review.{concern}", severity=severity, title=title,
        summary=f"{title} rationale", created_at=datetime.now(_UTC), kind="review",
        file_path="demo_login.py", line_start=line, line_end=line, confidence=0.9,
        evidence=[Evidence(event_id="7", source="github", timestamp=datetime.now(_UTC),
                           excerpt="Add login", url="https://github.com/o/r/pull/7")],
    )


def test_assemble_pr_review_makes_inline_comments_from_the_move_shape():
    findings = [
        _finding("security", "critical", 7, "SQL injection"),
        _finding("docs", "low", 10, "Missing docstring"),
    ]
    body, comments = assemble_pr_review(_review_profile(), _MOVE, findings)
    # inline comment keys come from the move's declared review_comment shape
    assert comments[0] == {
        "path": "demo_login.py", "line": 7, "side": "RIGHT",
        "body": comments[0]["body"],
    }
    assert "SQL injection" in comments[0]["body"] and "CRITICAL" in comments[0]["body"]
    assert len(comments) == 2
    assert "2 issue(s)" in body and "1 critical" in body and "1 low" in body


async def test_request_pr_review_queues_for_approval_and_does_not_post(monkeypatch):
    async with Session() as session:
        await session.execute(text("DELETE FROM situations WHERE company_id=:c"), {"c": _CO})
        await session.execute(text("DELETE FROM actions WHERE company_id=:c"), {"c": _CO})
        from packages.core.situations import save_situation
        for f in [_finding("security", "critical", 7, "SQL injection"),
                  _finding("tests", "medium", 5, "No test")]:
            await save_situation(session, f)
        await session.commit()

        result = await request_pr_review(session, _review_profile(), "7", requested_by="ui")
        await session.commit()

    # public move -> queued for a person, nothing sent
    assert result is not None and result.status == "pending_approval", result

    async with Session() as session:
        row = (await session.execute(
            text("SELECT params FROM actions WHERE company_id=:c AND situation_id='pr-review:7'"),
            {"c": _CO},
        )).first()
    params = row.params if isinstance(row.params, dict) else json.loads(row.params)
    assert params["repo"] == "o/r" and params["number"] == "7"
    body = params["body"]
    assert body["event"] == "REQUEST_CHANGES"
    assert len(body["comments"]) == 2  # both anchored findings became inline comments
    assert {c["line"] for c in body["comments"]} == {7, 5}


async def test_request_pr_review_returns_none_when_nothing_open():
    async with Session() as session:
        await session.execute(text("DELETE FROM situations WHERE company_id=:c"), {"c": _CO})
        await session.commit()
        result = await request_pr_review(session, _review_profile(), "999")
    assert result is None


# ---------------------------- feedback loop ----------------------------
# Dismissing a finding teaches the reviewer: don't reopen THIS one, and once a
# category is dismissed enough, stop raising it. With one floor that can't be
# taught away — a critical is always shown.

async def _seed_and_review(session, answer_json, *, updated_at):
    import packages.core.review as RR
    thing = await _seed_pr(session, updated_at=updated_at)
    RR.chat = lambda *a, **k: answer_json  # single concern answer
    reviewer = {"key": "pr_review",
                "select": {"thing_type": "pull_request", "where": {"status": "open"}},
                "ground": {"retrieve": {"top_k": 0}},
                "concerns": [{"key": "docs", "severity_ceiling": "low", "prompt": "docs"}]}
    return await RR.review_thing(session, _CO, reviewer, thing, activity_field="updated_at", force=True)


async def test_dismissed_finding_is_not_reopened_on_re_review(monkeypatch):
    ans = _answer({"severity": "low", "category": "missing-docstring", "title": "no docstring",
                   "file_path": "demo_login.py", "line_start": 10, "confidence": 0.6, "rationale": "x"})
    async with Session() as session:
        out = await _seed_and_review(session, ans, updated_at="2026-07-24T10:00:00Z")
        await session.commit()
        assert out["findings"] == 1
        sits = await list_situations(session, _CO)
        fid = next(s.id for s in sits if s.rule.startswith("review."))
        # a person dismisses it
        await R.record_dismissal(session, _CO, fid)
        from packages.core.situations import dismiss_situation
        await dismiss_situation(session, _CO, fid)
        await session.commit()
        # re-review with the SAME finding -> must NOT reopen it
        out2 = await _seed_and_review(session, ans, updated_at="2026-07-24T12:00:00Z")
        await session.commit()
    assert out2["findings"] == 0 and out2["suppressed"] == 1, out2


async def test_a_category_dismissed_enough_gets_muted(monkeypatch):
    ans = _answer({"severity": "low", "category": "missing-docstring", "title": "no docstring here",
                   "file_path": "demo_login.py", "line_start": 10, "confidence": 0.6, "rationale": "x"})
    async with Session() as session:
        await session.execute(text("DELETE FROM review_feedback WHERE company_id=:c"), {"c": _CO})
        await session.commit()
        # three dismissals of the docs/missing-docstring category (min evidence)
        for i in range(R.FEEDBACK_MIN_DISMISSALS):
            await session.execute(text(
                "INSERT INTO review_feedback (company_id, reviewer_key, finding_id, concern, category, severity, verdict)"
                " VALUES (:c,'pr_review',:f,'docs','missing-docstring','low','dismissed')"),
                {"c": _CO, "f": f"review:pr_review:old{i}:docs:zz"})
        await session.commit()
        muted = await R._suppressed_categories(session, _CO)
        assert ("docs", "missing-docstring") in muted
        # a NEW docstring finding on a fresh PR is now muted before it's raised
        out = await _seed_and_review(session, ans, updated_at="2026-07-24T13:00:00Z")
        await session.commit()
    assert out["findings"] == 0 and out["suppressed"] == 1, out


async def test_critical_is_never_suppressed(monkeypatch):
    # even with the category dismissed to the moon, a critical is always shown
    ans = _answer({"severity": "critical", "category": "injection", "title": "SQLi",
                   "file_path": "demo_login.py", "line_start": 7, "confidence": 0.99, "rationale": "x"})
    async with Session() as session:
        await session.execute(text("DELETE FROM review_feedback WHERE company_id=:c"), {"c": _CO})
        for i in range(5):
            await session.execute(text(
                "INSERT INTO review_feedback (company_id, reviewer_key, finding_id, concern, category, severity, verdict)"
                " VALUES (:c,'pr_review',:f,'security','injection','high','dismissed')"),
                {"c": _CO, "f": f"review:pr_review:old{i}:security:zz"})
        await session.commit()
        reviewer = {"key": "pr_review", "select": {"thing_type": "pull_request", "where": {"status": "open"}},
                    "ground": {"retrieve": {"top_k": 0}},
                    "concerns": [{"key": "security", "severity_ceiling": "critical", "prompt": "sec"}]}
        thing = await _seed_pr(session, updated_at="2026-07-24T14:00:00Z")
        R.chat = lambda *a, **k: ans
        out = await R.review_thing(session, _CO, reviewer, thing, activity_field="updated_at", force=True)
        await session.commit()
    assert out["findings"] == 1 and out["suppressed"] == 0, "a critical must never be muted"
