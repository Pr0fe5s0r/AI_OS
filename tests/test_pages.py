from __future__ import annotations

import json

import pytest

from packages.core import navigator
from packages.core.store import put_item
from packages.shared.schema import Item, SourceRef
from tests.conftest import SCOPE

pytestmark = pytest.mark.needs_db

# Looking at a page is the escalation, and the whole value of it depends on WHEN
# it fires. A system that reaches for a page picture before trying the text is
# not saving a hard question, it is skipping an easy one — slower, more
# expensive, and reasoning over a transcription when the real thing was right
# there. So the gate is what these tests are about, more than the rendering.


def _call(name: str, **args) -> dict:
    return {"id": f"c-{name}", "name": name, "arguments": json.dumps(args)}


async def _seed(db, body: str, title: str = "Quarterly Report") -> str:
    await put_item(
        db,
        Item(
            id="",
            scope=SCOPE,
            title=title,
            body=body,
            source=SourceRef(source="upload", locator="report.pdf"),
        ),
    )
    await db.commit()
    return (await navigator._documents(db, SCOPE))[0]["item_id"]


# A table as PDF extraction leaves it: every figure present, and no way to
# tell which year 52 belongs to. This is the case the whole escalation exists
# for — retrieval finds the section, and the text still cannot be read.
_TABLE_PAGE = """# Quarterly Report

Intro paragraph about the quarter.

## Revenue

2024 2025 Revenue 41 52 Costs 33 39 Margin 8 13
"""


async def test_looking_is_not_offered_before_anything_has_been_read(db, monkeypatch):
    """The gate that matters. The tool is withheld from the model entirely
    until it has read something — a stronger guarantee than instructing it not
    to look first, because an instruction is advice and a missing tool is not
    callable."""
    await _seed(db, _TABLE_PAGE)
    offered: list[list[str]] = []

    def fake_chat(messages, tools, **kwargs):
        offered.append([t["function"]["name"] for t in tools])
        return {"content": "nothing here", "tool_calls": []}

    monkeypatch.setattr("packages.core.llm.chat_with_tools", fake_chat)
    monkeypatch.setattr(navigator, "MAX_ROUNDS", 1)
    await navigator.navigate(db, SCOPE, "what was revenue")

    assert offered, "the model was never called"
    assert "look_at_page" not in offered[0]
    assert "read_section" in offered[0]


async def test_looking_is_offered_once_a_page_backed_document_has_been_read(
    db, monkeypatch
):
    """After reading, and only for a document that actually HAS pages. An offer
    of something that is not there is worse than no offer: it invites a call
    that can only fail."""
    item_id = await _seed(db, _TABLE_PAGE)
    offered: list[list[str]] = []
    rounds = iter(
        [
            {"content": "", "tool_calls": [_call("read_section", doc=item_id, section="n002")]},
            {"content": "", "tool_calls": [_call("submit_answer", answer="done", found=True)]},
        ]
    )

    def fake_chat(messages, tools, **kwargs):
        offered.append([t["function"]["name"] for t in tools])
        return next(rounds)

    monkeypatch.setattr("packages.core.llm.chat_with_tools", fake_chat)
    monkeypatch.setattr("packages.core.pages.available", lambda: True)

    async def three_pages(workspace_id, item_id_, locator=""):
        return 3

    monkeypatch.setattr("packages.core.pages.count", three_pages)
    await navigator.navigate(db, SCOPE, "what was revenue")

    assert "look_at_page" not in offered[0], "not before reading"
    assert "look_at_page" in offered[1], "offered after reading a document with pages"


async def test_a_document_without_pages_never_offers_the_page_tool(db, monkeypatch):
    """Pasted text, Markdown and .docx have no page a picture could be OF.
    Nothing should suggest otherwise."""
    item_id = await _seed(db, _TABLE_PAGE)
    offered: list[list[str]] = []
    rounds = iter(
        [
            {"content": "", "tool_calls": [_call("read_section", doc=item_id, section="n002")]},
            {"content": "", "tool_calls": [_call("submit_answer", answer="done", found=True)]},
        ]
    )

    def fake_chat(messages, tools, **kwargs):
        offered.append([t["function"]["name"] for t in tools])
        return next(rounds)

    monkeypatch.setattr("packages.core.llm.chat_with_tools", fake_chat)
    monkeypatch.setattr("packages.core.pages.available", lambda: True)

    async def no_pages(workspace_id, item_id_, locator=""):
        return 0

    monkeypatch.setattr("packages.core.pages.count", no_pages)
    await navigator.navigate(db, SCOPE, "what was revenue")

    assert all("look_at_page" not in names for names in offered)


async def test_a_page_that_was_looked_at_becomes_a_citation_carrying_its_page(
    db, monkeypatch
):
    """The transcription is a claim; the page number is what lets a reader go
    and check it. A citation read with vision that could not be traced back to
    a page would be the least checkable thing in the whole store."""
    item_id = await _seed(db, _TABLE_PAGE)
    rounds = iter(
        [
            {"content": "", "tool_calls": [_call("read_section", doc=item_id, section="n002")]},
            {
                "content": "",
                "tool_calls": [
                    _call("look_at_page", doc=item_id, page=2, looking_for="revenue table")
                ],
            },
            {
                "content": "",
                "tool_calls": [
                    _call("submit_answer", answer="Revenue was 52 [2].", found=True)
                ],
            },
        ]
    )
    monkeypatch.setattr("packages.core.llm.chat_with_tools", lambda *a, **k: next(rounds))
    monkeypatch.setattr("packages.core.pages.available", lambda: True)

    async def two_pages(workspace_id, item_id_, locator=""):
        return 2

    async def transcribe(workspace_id, item_id_, page, looking_for):
        return "| | 2024 | 2025 |\n| Revenue | 41 | 52 |"

    monkeypatch.setattr("packages.core.pages.count", two_pages)
    monkeypatch.setattr(navigator, "_read_page", transcribe)

    outcome, _ = await navigator.navigate(db, SCOPE, "what was 2025 revenue")

    passages = [p for hit in outcome.hits for p in hit.passages]
    looked = [p for p in passages if p.page == 2]
    assert looked, "the page passage did not survive into the hits"
    assert "| Revenue | 41 | 52 |" in looked[0].text
    assert [s.action for s in outcome.steps] == ["opened", "read", "looked", "answered"]


async def test_a_page_that_does_not_show_it_is_recorded_and_not_cited(db, monkeypatch):
    """A look that came back empty is evidence too — it says the page WAS
    checked. It belongs in the trail; it does not belong in the citations,
    because there is nothing there to cite."""
    item_id = await _seed(db, _TABLE_PAGE)
    rounds = iter(
        [
            {"content": "", "tool_calls": [_call("read_section", doc=item_id, section="n002")]},
            {
                "content": "",
                "tool_calls": [_call("look_at_page", doc=item_id, page=1, looking_for="x")],
            },
            {"content": "", "tool_calls": [_call("submit_answer", answer="No.", found=False)]},
        ]
    )
    monkeypatch.setattr("packages.core.llm.chat_with_tools", lambda *a, **k: next(rounds))
    monkeypatch.setattr("packages.core.pages.available", lambda: True)

    async def one_page(workspace_id, item_id_, locator=""):
        return 1

    async def nothing_there(workspace_id, item_id_, page, looking_for):
        return navigator._NOT_ON_PAGE

    monkeypatch.setattr("packages.core.pages.count", one_page)
    monkeypatch.setattr(navigator, "_read_page", nothing_there)

    outcome, _ = await navigator.navigate(db, SCOPE, "what was revenue")

    assert any(s.action == "looked" and "not there" in s.detail for s in outcome.steps)
    assert all(p.page is None for hit in outcome.hits for p in hit.passages)


async def test_an_unreadable_page_degrades_to_the_text_rather_than_failing(
    db, monkeypatch
):
    """No object store, no vision model, a file that will not render, a provider
    that is down — all of them mean the same thing: carry on with the text. An
    escalation that can take the whole answer down with it is worse than no
    escalation at all."""
    item_id = await _seed(db, _TABLE_PAGE)
    rounds = iter(
        [
            {"content": "", "tool_calls": [_call("read_section", doc=item_id, section="n002")]},
            {
                "content": "",
                "tool_calls": [_call("look_at_page", doc=item_id, page=1, looking_for="x")],
            },
            {
                "content": "",
                "tool_calls": [
                    _call("submit_answer", answer="From the text [1].", found=True)
                ],
            },
        ]
    )
    monkeypatch.setattr("packages.core.llm.chat_with_tools", lambda *a, **k: next(rounds))
    monkeypatch.setattr("packages.core.pages.available", lambda: True)

    async def one_page(workspace_id, item_id_, locator=""):
        return 1

    async def broken(workspace_id, item_id_, page, looking_for):
        return None

    monkeypatch.setattr("packages.core.pages.count", one_page)
    monkeypatch.setattr(navigator, "_read_page", broken)

    outcome, _ = await navigator.navigate(db, SCOPE, "what was revenue")

    assert outcome.found is True, "the text answer must survive a failed look"
    assert outcome.answer == "From the text [1]."


async def test_looking_is_capped(db, monkeypatch):
    """Two pages is enough to check a table and the page after it. More than
    that is the agent browsing, on the most expensive call in the system."""
    assert navigator.MAX_LOOKS == 2

    item_id = await _seed(db, _TABLE_PAGE)
    offered: list[list[str]] = []
    rounds = iter(
        [
            {"content": "", "tool_calls": [_call("read_section", doc=item_id, section="n002")]},
            {
                "content": "",
                "tool_calls": [_call("look_at_page", doc=item_id, page=1, looking_for="x")],
            },
            {
                "content": "",
                "tool_calls": [_call("look_at_page", doc=item_id, page=2, looking_for="x")],
            },
            {"content": "", "tool_calls": [_call("submit_answer", answer="ok", found=True)]},
        ]
    )

    def fake_chat(messages, tools, **kwargs):
        offered.append([t["function"]["name"] for t in tools])
        return next(rounds)

    monkeypatch.setattr("packages.core.llm.chat_with_tools", fake_chat)
    monkeypatch.setattr("packages.core.pages.available", lambda: True)

    async def five_pages(workspace_id, item_id_, locator=""):
        return 5

    async def transcribe(workspace_id, item_id_, page, looking_for):
        return f"page {page} says something"

    monkeypatch.setattr("packages.core.pages.count", five_pages)
    monkeypatch.setattr(navigator, "_read_page", transcribe)
    await navigator.navigate(db, SCOPE, "what was revenue")

    assert "look_at_page" not in offered[-1], "withdrawn once the cap is reached"


def test_only_pdfs_have_pages():
    from packages.core import pages

    assert pages.renderable("application/pdf")
    assert pages.renderable("application/octet-stream", "quarterly.pdf")
    assert not pages.renderable("text/markdown", "notes.md")
    assert not pages.renderable("application/vnd.openxmlformats-officedocument", "a.docx")


def test_the_page_path_is_off_unless_a_vision_model_is_named(monkeypatch):
    """A deployment should not quietly start making a second, bigger kind of
    model call because it pulled a new image."""
    from packages.core import pages

    monkeypatch.delenv("VISION_MODEL", raising=False)
    assert pages.vision_model() == ""
    assert pages.available() is False


# --------------------------- scanned documents ---------------------------
#
# A scan was refused at ingest — which meant the documents with the strongest
# case for being read as pictures were the only ones that could not get in.


def _scanned_pdf() -> bytes:
    """A one-page PDF whose content is an image: no text layer at all."""
    import io

    from PIL import Image, ImageDraw

    page = Image.new("RGB", (1240, 1754), "white")
    draw = ImageDraw.Draw(page)
    draw.text((80, 80), "Invoice 4471", fill="black")
    draw.text((80, 140), "Total 812.40", fill="black")
    buffer = io.BytesIO()
    page.save(buffer, format="PDF")
    return buffer.getvalue()


def test_a_scan_is_refused_by_the_parser_with_its_own_type():
    """Its own exception type, not a message to match on, because something can
    be done about this one and nothing can be done about a .pptx."""
    from packages.core.normalise import ScannedDocument, UnsupportedFormat, normalise

    with pytest.raises(ScannedDocument) as raised:
        normalise(_scanned_pdf(), "scan.pdf")
    assert raised.value.page_count == 1
    # Still an UnsupportedFormat, so callers that only know the old behaviour
    # keep treating it as an honest refusal.
    assert isinstance(raised.value, UnsupportedFormat)


async def test_a_scan_is_indexed_from_what_the_pages_say(monkeypatch):
    """The point of the whole path: a document with no text becomes a document
    with text, shaped exactly like extracted PDF text so that the page index,
    the passages and the citations all behave identically."""
    from packages.core import pipeline

    monkeypatch.setattr("packages.core.pages.available", lambda: True)

    async def read_pages(data, limit=None):
        return ["# Invoice 4471\n\n| Item | Total |\n| Consulting | 812.40 |"], 1

    monkeypatch.setattr("packages.core.pages.transcribe_document", read_pages)
    parsed = await pipeline._read_scan(_scanned_pdf(), "scan.pdf", Exception())

    assert parsed is not None
    assert "<!-- page 1 -->" in parsed.body, "page markers are what the index builds sections from"
    assert "812.40" in parsed.body
    assert parsed.metadata["read_by"] == "vision"
    assert parsed.metadata["pages_read"] == 1


async def test_a_scan_that_is_too_long_says_so_in_the_document_itself(monkeypatch):
    """A reader who searches a truncated scan and finds nothing deserves to
    know the back half was never read, rather than concluding it is not there.
    Metadata alone would not tell them."""
    from packages.core import pipeline

    monkeypatch.setattr("packages.core.pages.available", lambda: True)

    async def two_of_nine(data, limit=None):
        return ["page one text", "page two text"], 9

    monkeypatch.setattr("packages.core.pages.transcribe_document", two_of_nine)
    parsed = await pipeline._read_scan(_scanned_pdf(), "long-scan.pdf", Exception())

    assert parsed is not None
    assert "were not read" in parsed.body
    assert parsed.metadata["pages"] == 9 and parsed.metadata["pages_read"] == 2


async def test_a_scan_stays_a_failure_when_there_is_no_way_to_read_it(monkeypatch):
    """No vision model, or every page came back blank. An item with no text
    looks ingested and can never be retrieved, which is worse than a refusal
    that says why."""
    from packages.core import pipeline

    monkeypatch.setattr("packages.core.pages.available", lambda: False)
    assert await pipeline._read_scan(_scanned_pdf(), "scan.pdf", Exception()) is None

    monkeypatch.setattr("packages.core.pages.available", lambda: True)

    async def all_blank(data, limit=None):
        return ["", "   "], 2

    monkeypatch.setattr("packages.core.pages.transcribe_document", all_blank)
    assert await pipeline._read_scan(_scanned_pdf(), "scan.pdf", Exception()) is None


async def test_reading_a_scan_keeps_the_pages_it_managed(monkeypatch):
    """Pages are read one at a time so a failure halfway leaves what worked. An
    exception in the middle would throw away every page that did succeed."""
    from packages.core import pages

    monkeypatch.setenv("VISION_MODEL", "vision-test")
    monkeypatch.setattr("packages.core.pages._count_sync", lambda data: 4)

    seen: list[int] = []

    async def flaky(data, page):
        seen.append(page)
        if page == 3:
            raise RuntimeError("provider is down")
        return f"page {page}"

    monkeypatch.setattr("packages.core.pages.transcribe", flaky)
    read, total = await pages.transcribe_document(b"not really a pdf")

    assert read == ["page 1", "page 2"]
    assert total == 4 and seen == [1, 2, 3]


async def test_a_prose_answer_about_a_picture_is_also_sent_back(db, monkeypatch):
    """The press lived only inside submit_answer, and this model mostly does not
    call submit_answer — it writes prose. So asked which quarter shipped most in
    a bar chart, it read the page, found the axis labels, and wrote a paragraph
    about the axis without ever looking at the bars. Four questions in a row went
    that way, every one marked grounded, because a genuinely-read section was
    genuinely cited. One gate, both paths."""
    item_id = await _seed(db, _TABLE_PAGE)
    rounds = iter(
        [
            {"content": "", "tool_calls": [_call("read_section", doc=item_id, section="n002")]},
            {"content": "The axis runs from 0 to 100 in steps of 25.", "tool_calls": []},
            {
                "content": "",
                "tool_calls": [
                    _call("look_at_page", doc=item_id, page=1, looking_for="the bar chart")
                ],
            },
            {"content": "Q4 is the tallest bar, at about 95.", "tool_calls": []},
        ]
    )
    monkeypatch.setattr("packages.core.llm.chat_with_tools", lambda *a, **k: next(rounds))
    monkeypatch.setattr("packages.core.pages.available", lambda: True)

    async def one_page(workspace_id, item_id_, locator=""):
        return 1

    async def seen(workspace_id, item_id_, page, looking_for):
        return "Bars: Q1 40, Q2 75, Q3 25, Q4 95, Q5 60."

    monkeypatch.setattr("packages.core.pages.count", one_page)
    monkeypatch.setattr(navigator, "_read_page", seen)

    outcome, _ = await navigator.navigate(db, SCOPE, "In Figure 6, which quarter shipped most?")

    actions = [s.action for s in outcome.steps]
    assert "sent back" in actions, "a prose answer about a figure must be sent back"
    assert "looked" in actions
    assert "Q4" in outcome.answer


async def test_a_prose_answer_about_ordinary_text_is_left_alone(db, monkeypatch):
    """The press is for pictures only. A question the text answers must not be
    pushed into a vision call: slower, dearer, and reasoning over a description
    when the real thing was right there."""
    item_id = await _seed(db, _TABLE_PAGE)
    rounds = iter(
        [
            {"content": "", "tool_calls": [_call("read_section", doc=item_id, section="n002")]},
            {"content": "Revenue was 52 in 2025.", "tool_calls": []},
        ]
    )
    monkeypatch.setattr("packages.core.llm.chat_with_tools", lambda *a, **k: next(rounds))
    monkeypatch.setattr("packages.core.pages.available", lambda: True)

    async def one_page(workspace_id, item_id_, locator=""):
        return 1

    monkeypatch.setattr("packages.core.pages.count", one_page)
    outcome, _ = await navigator.navigate(db, SCOPE, "What was revenue in 2025?")

    assert [s.action for s in outcome.steps] == ["opened", "read", "answered"]


def test_the_page_reader_is_asked_about_shapes_not_only_text():
    """Asked how many sides a shaded polygon has, the reader came back with
    nothing useful: the prompt told it to transcribe text and to report chart
    values, and a polygon is neither. What is DRAWN has to be asked for
    explicitly, because none of it exists in the text layer."""
    from packages.core.navigator import _LOOK_PROMPT

    prompt = _LOOK_PROMPT.lower()
    assert "sides" in prompt, "shape descriptions must be requested"
    assert "colour" in prompt or "color" in prompt
    assert "right angle" in prompt
    assert "largest" in prompt, "charts need a reading, not just a transcription"


def test_both_readers_are_asked_to_map_a_legend_not_just_describe_it():
    """Asked which elements are liquid, the store said it could not tell —
    over an image of the periodic table where the answer is right there. The
    description it had recorded said "the cells are colour-coded... liquids are
    orange" and never once said WHICH element was orange.

    A legend with nothing mapped to it answers no question, and the mapping is
    the one thing a text layer can never hold. Both readers have to be told
    that: the ingest one, which is all an image ever gets, and the mid-question
    one, which is where a specific question is actually answered."""
    from packages.core import pages
    from packages.core.navigator import _LOOK_PROMPT

    for prompt in (pages._DESCRIBE_IMAGE.lower(), _LOOK_PROMPT.lower()):
        assert "colour" in prompt
        assert "legend" in prompt or "key" in prompt
        # The instruction that matters is the mapping, not the description.
        assert "which items" in prompt or "name them" in prompt


def test_neither_reader_is_allowed_to_abbreviate():
    """Asking for the colour mapping made the reader trade completeness for it:
    the periodic table came back with rows 4 to 84 replaced by "| ... | ... |".
    For an image this text is the only record that will ever exist, so a row it
    skips is a fact nobody can retrieve afterwards."""
    from packages.core import pages
    from packages.core.navigator import _LOOK_PROMPT

    for prompt in (pages._DESCRIBE_IMAGE.lower(), _LOOK_PROMPT.lower()):
        assert "abbreviate" in prompt
        assert "and so on" in prompt


async def test_a_question_about_an_image_forces_a_look_however_it_is_worded(db, monkeypatch):
    """"Which elements are liquid at room temperature?" contains no picture
    word, so the press did not fire — and the document was a photograph of the
    periodic table whose stored text is a DESCRIPTION somebody wrote by looking
    at it. The store answered that it could not tell, over an image where the
    answer is plainly visible.

    Nobody should have to know their file has no text in it. A question about a
    document that IS a picture is a question about a picture."""
    await put_item(
        db,
        Item(
            id="",
            scope=SCOPE,
            title="Periodic table",
            body="<!-- page 1 -->\n# Periodic table\n\nA colour-coded chart of the elements.",
            source=SourceRef(source="upload", locator="table.jpg"),
        ),
    )
    await db.commit()
    item_id = (await navigator._documents(db, SCOPE))[0]["item_id"]

    monkeypatch.setattr("packages.core.pages.available", lambda: True)

    async def one_page(workspace_id, item_id_, locator=""):
        return 1

    async def looked_at(workspace_id, item_id_, page, looking_for):
        return "Names printed in blue are liquids: Bromine (Br) and Mercury (Hg)."

    monkeypatch.setattr("packages.core.pages.count", one_page)
    monkeypatch.setattr(navigator, "_read_page", looked_at)

    rounds = iter(
        [
            {"content": "", "tool_calls": [_call("read_section", doc=item_id, section="n001")]},
            {"content": "The description does not say which elements are liquid.", "tool_calls": []},
            {
                "content": "",
                "tool_calls": [
                    _call("look_at_page", doc=item_id, page=1, looking_for="liquid elements")
                ],
            },
            {"content": "Bromine and Mercury are liquid at room temperature [2].", "tool_calls": []},
        ]
    )
    monkeypatch.setattr("packages.core.llm.chat_with_tools", lambda *a, **k: next(rounds))

    outcome, _ = await navigator.navigate(
        db, SCOPE, "Which elements are liquid at room temperature?"
    )

    actions = [s.action for s in outcome.steps]
    assert "sent back" in actions, "a picture document must be looked at, not paraphrased"
    assert "looked" in actions
    assert "Mercury" in outcome.answer


async def test_a_text_document_is_not_dragged_into_a_vision_call(db, monkeypatch):
    """The rule is for documents that ARE pictures. A PDF has text of its own,
    and pushing every question about one into a vision call would be slower,
    dearer, and reasoning over a description when the real thing was there."""
    item_id = await _seed(db, _TABLE_PAGE)
    monkeypatch.setattr("packages.core.pages.available", lambda: True)

    async def one_page(workspace_id, item_id_, locator=""):
        return 1

    monkeypatch.setattr("packages.core.pages.count", one_page)
    rounds = iter(
        [
            {"content": "", "tool_calls": [_call("read_section", doc=item_id, section="n002")]},
            {"content": "Revenue was 52 in 2025.", "tool_calls": []},
        ]
    )
    monkeypatch.setattr("packages.core.llm.chat_with_tools", lambda *a, **k: next(rounds))

    outcome, _ = await navigator.navigate(db, SCOPE, "What was revenue in 2025?")
    assert [s.action for s in outcome.steps] == ["opened", "read", "answered"]


def test_a_reading_says_where_on_the_page_it_came_from():
    """A page thumbnail says "somewhere in here". A box says "this row". The
    coordinates are pulled out of the prose because they are for drawing, not
    for reading — leaving "REGION x=12 y=44" in a passage would put machinery
    back on the reader's screen."""
    from packages.core.navigator import _regions_in

    text, regions = _regions_in(
        "Mercury (Hg) and Bromine (Br) are the two liquids.\n"
        "REGION x=71.5 y=40 w=4 h=6 | Mercury cell\n"
        "REGION x=35 y=33 w=4 h=6 | Bromine cell\n"
    )

    assert "REGION" not in text, "coordinates must not leak into the evidence"
    assert text.startswith("Mercury")
    assert [r["label"] for r in regions] == ["Mercury cell", "Bromine cell"]
    assert regions[0]["x"] == 71.5 and regions[0]["h"] == 6


def test_a_box_round_the_whole_page_is_dropped():
    """A box round everything tells the reader nothing they did not already
    know, and costs them the belief that a box means something."""
    from packages.core.navigator import _regions_in

    _, regions = _regions_in("Everything.\nREGION x=0 y=0 w=100 h=100 | the page\n")
    assert regions == []


def test_boxes_are_clamped_to_the_page_rather_than_thrown_away():
    """A reader that says a row runs to 104% has still pointed at the right
    row. Discarding that would lose a good highlight over a rounding error."""
    from packages.core.navigator import _regions_in

    _, regions = _regions_in("x\nREGION x=90 y=95 w=30 h=20 | the last row\n")
    assert len(regions) == 1
    box = regions[0]
    assert box["x"] + box["w"] <= 100 and box["y"] + box["h"] <= 100


def test_nonsense_coordinates_are_ignored():
    from packages.core.navigator import _regions_in

    _, regions = _regions_in(
        "x\nREGION x=10 y=10 w=0 h=5 | zero width\nREGION x=a y=b w=c h=d | junk\n"
    )
    assert regions == []


def test_a_reading_with_no_regions_is_still_a_reading():
    """The reader is told to leave out anything it cannot place confidently, so
    no boxes is a correct and common outcome — the passage must survive it."""
    from packages.core.navigator import _regions_in

    text, regions = _regions_in("The table lists every element by weight.")
    assert text == "The table lists every element by weight."
    assert regions == []


async def test_even_a_picture_is_read_before_it_is_looked_at(db, monkeypatch):
    """Offering the page tool from the opening round LOOKS like free speed: a
    picture's text is only a description, so why read it first?

    Measured, it cost two of twenty answers. Without a read the agent does not
    yet know WHICH document it needs — it looked at the wrong one, spent a look
    on NOT_ON_THIS_PAGE, and answered from a third document entirely. Asked
    which elements are liquid, it described a chart of support tickets.

    The read is what establishes where. This test exists so the shortcut is not
    re-invented on the same reasoning.
    """
    await put_item(
        db,
        Item(
            id="",
            scope=SCOPE,
            title="Periodic table",
            body="<!-- page 1 -->\n# Periodic table\n\nA colour-coded chart.",
            source=SourceRef(source="upload", locator="table.jpg"),
        ),
    )
    await db.commit()

    offered: list[list[str]] = []
    monkeypatch.setattr("packages.core.pages.available", lambda: True)

    async def one_page(workspace_id, item_id_, locator=""):
        return 1

    monkeypatch.setattr("packages.core.pages.count", one_page)

    def fake_chat(messages, tools, **kwargs):
        offered.append([t["function"]["name"] for t in tools])
        return {"content": "nothing yet", "tool_calls": []}

    monkeypatch.setattr("packages.core.llm.chat_with_tools", fake_chat)
    monkeypatch.setattr(navigator, "MAX_ROUNDS", 1)
    await navigator.navigate(db, SCOPE, "which elements are liquid?")

    assert offered, "the model was never called"
    assert "look_at_page" not in offered[0], "not before a read, pictures included"


async def test_a_pdf_still_has_to_be_read_before_it_is_looked_at(db, monkeypatch):
    """The exception is for pictures only. A PDF has text of its own, and
    looking before reading would spend the most expensive call in the system to
    skip the cheapest one."""
    item_id = await _seed(db, _TABLE_PAGE)
    offered: list[list[str]] = []
    monkeypatch.setattr("packages.core.pages.available", lambda: True)

    async def one_page(workspace_id, item_id_, locator=""):
        return 1

    monkeypatch.setattr("packages.core.pages.count", one_page)
    rounds = iter(
        [
            {"content": "", "tool_calls": [_call("read_section", doc=item_id, section="n002")]},
            {"content": "Revenue was 52.", "tool_calls": []},
        ]
    )

    def fake_chat(messages, tools, **kwargs):
        offered.append([t["function"]["name"] for t in tools])
        return next(rounds)

    monkeypatch.setattr("packages.core.llm.chat_with_tools", fake_chat)
    await navigator.navigate(db, SCOPE, "what was revenue in 2025?")

    assert "look_at_page" not in offered[0], "a PDF is read first, as before"


async def test_the_same_page_read_for_the_same_thing_is_not_paid_for_twice(monkeypatch):
    """The most expensive call in the system, for a string we already have."""
    from packages.core import pages

    navigator._READ_CACHE.clear()
    calls: list[str] = []

    monkeypatch.setattr("packages.core.pages.available", lambda: True)

    async def fake_image(workspace_id, item_id, page):
        return b"pretend png"

    monkeypatch.setattr("packages.core.pages.image", fake_image)
    monkeypatch.setattr("packages.core.pages.vision_model", lambda: "vision-test")
    monkeypatch.setattr(
        "packages.core.llm.look",
        lambda png, prompt, model, **k: calls.append(prompt) or "Mercury and Bromine.",
    )

    first = await navigator._read_page("w", "doc", 1, "which elements are liquid")
    second = await navigator._read_page("w", "doc", 1, "Which Elements  Are Liquid")
    assert first == second == "Mercury and Bromine."
    assert len(calls) == 1, "the second read must come from the cache"

    # A different question about the same page is a different reading: the page
    # is read WITH the question in hand, so serving one for the other would
    # trade the expensive call for a wrong answer.
    await navigator._read_page("w", "doc", 1, "how many sides has the shape")
    assert len(calls) == 2

    assert pages.available()  # the guard above is what made any of this run


async def test_a_refusal_is_never_cached(monkeypatch):
    """NOT_ON_THIS_PAGE is the model failing, not a fact about the page —
    measured at 0 refusals in 5 on a page that had refused once. Cached, that
    one failure would be pinned to the page for the life of the process and the
    question could never recover."""
    navigator._READ_CACHE.clear()
    replies = iter([navigator._NOT_ON_PAGE, "Mercury and Bromine are the liquids."])

    monkeypatch.setattr("packages.core.pages.available", lambda: True)
    monkeypatch.setattr("packages.core.pages.vision_model", lambda: "vision-test")

    async def fake_image(workspace_id, item_id, page):
        return b"pretend png"

    monkeypatch.setattr("packages.core.pages.image", fake_image)
    monkeypatch.setattr("packages.core.llm.look", lambda *a, **k: next(replies))

    first = await navigator._read_page("w", "doc", 1, "which elements are liquid")
    assert first.startswith(navigator._NOT_ON_PAGE)

    # Asked again, it must reach the model rather than be handed the refusal.
    second = await navigator._read_page("w", "doc", 1, "which elements are liquid")
    assert second == "Mercury and Bromine are the liquids."
