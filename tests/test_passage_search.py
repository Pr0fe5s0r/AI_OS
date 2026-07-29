from __future__ import annotations

import pytest
from sqlalchemy import text

from packages.core import chunks
from packages.core.search import search_traced
from packages.core.store import put_item
from packages.shared.schema import Item, SourceRef
from tests.conftest import SCOPE

pytestmark = pytest.mark.needs_db

# Retrieval is passage-level. These pin the parts of that which are invisible
# when they break: the store still returns results, they are just wrong or
# unciteable.

_LONG = "\n\n".join(
    [
        "# Handbook",
        "## 1. Leave policy",
        "Staff accrue leave monthly. " * 25,
        "## 2. Expenses",
        "Receipts must be submitted within thirty days of the expense. " * 15,
        "## 3. Equipment",
        "Laptops are replaced on a four year cycle. " * 20,
    ]
)


def _doc(body: str, locator: str = "handbook.md", title: str = "Handbook") -> Item:
    return Item(
        id="",
        scope=SCOPE,
        title=title,
        body=body,
        source=SourceRef(source="upload", locator=locator),
    )


async def test_writing_a_document_makes_its_passages_immediately(db):
    """Passages are built by the store, not the embedding job. Deferring them
    would leave a document unsearchable by keyword until an embedding provider
    answered — and the whole point of a keyword arm is that it works when that
    provider does not."""
    result = await put_item(db, _doc(_LONG))
    await db.commit()

    passages = await chunks.for_item(db, SCOPE, result.item.id)
    assert len(passages) > 1
    assert any("thirty days" in p.text for p in passages)


async def test_keyword_search_works_without_any_embedding(db):
    """No vectors have been written here at all — no graph, no provider."""
    await put_item(db, _doc(_LONG))
    await db.commit()

    hits, trace = await search_traced(db, SCOPE, "receipts submitted within thirty days")
    assert hits, "keyword arm alone must still answer"
    assert trace.keyword, "the keyword arm must have proposed passages"


async def test_the_citation_and_the_quote_are_the_same_passage(db):
    """A result was once cited as one section while quoting text from another,
    because the heading came from the vector arm's best passage and the excerpt
    from the keyword arm's. A citation pointing at the wrong place is worse
    than none: it looks checkable and is not."""
    await put_item(db, _doc(_LONG))
    await db.commit()

    hits, _ = await search_traced(db, SCOPE, "receipts submitted within thirty days")
    hit = hits[0]
    assert hit.passages
    winner = hit.passages[0]
    assert hit.heading == winner.heading
    # The excerpt must be drawn from the passage the heading names. Highlight
    # markers and truncation aside, its opening words have to appear in it.
    stripped = hit.excerpt.replace("[[", "").replace("]]", "")
    probe = " ".join(stripped.split()[:5])
    assert probe and probe in " ".join(winner.text.split())


async def test_a_document_reports_every_place_it_matched(db):
    """A file matching in six places is a different answer from one matching in
    a single line, and a document-level score cannot say which it is."""
    await put_item(db, _doc(_LONG))
    await db.commit()

    hits, _ = await search_traced(db, SCOPE, "staff accrue leave monthly")
    assert hits[0].passages
    assert all(p.chunk_id for p in hits[0].passages)
    scores = [p.score for p in hits[0].passages]
    assert scores == sorted(scores, reverse=True), "passages must be best first"


async def test_keyword_terms_must_share_a_passage(db):
    """A consequence of searching passages rather than documents, pinned so it
    is a decision rather than a surprise: a query whose terms live in different
    sections no longer matches on wording alone, because no single passage
    contains them all. The semantic arm is what carries such queries — which is
    also why losing that arm narrows recall, and why the trace says when it is
    unavailable."""
    await put_item(db, _doc(_LONG))
    await db.commit()

    # "leave" is in section 1 and "laptops" in section 3; nothing has both.
    _, trace = await search_traced(db, SCOPE, "leave laptops")
    assert trace.keyword == []


async def test_replacing_a_document_does_not_leave_old_passages_behind(db):
    """Passage boundaries move when text is edited, so reconciling one by one
    would strand text that still answers queries — the worst failure for a
    store whose claim is that it can show where an answer came from."""
    await put_item(db, _doc(_LONG))
    await db.commit()

    await put_item(db, _doc("# Handbook\n\n## 1. Leave policy\n\nCompletely rewritten."))
    await db.commit()

    remaining = (
        await db.execute(
            text("SELECT count(*) FROM kb_chunks WHERE workspace_id = :w AND text ILIKE :p"),
            {"w": SCOPE.workspace_id, "p": "%thirty days%"},
        )
    ).scalar_one()
    assert remaining == 0

    hits, _ = await search_traced(db, SCOPE, "receipts submitted within thirty days")
    assert not any("thirty days" in (p.text or "") for h in hits for p in h.passages)
