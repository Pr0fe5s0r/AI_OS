from __future__ import annotations

import io
import zipfile

import pytest

from packages.core.normalise import (
    PictureDocument,
    ScannedDocument,
    UnsupportedFormat,
    can_parse,
    normalise,
    supported,
)

# Slides, spreadsheets and pictures. Three formats, three different ideas of
# what the unit of citation is:
#
#   a deck      -> the slide. "It's on slide 12" is how people talk about decks.
#   a workbook  -> the sheet, and the grid inside it. A spreadsheet flattened
#                  into a run of values is the failure that made PDF tables
#                  useless: every number present, none of them attached to the
#                  label that gives it meaning.
#   an image    -> the picture itself, which has no text to parse at all.


# ------------------------------- fixtures -------------------------------


def _pptx(slides: list[tuple[list[str], list[str]]]) -> bytes:
    """A minimal but real .pptx: (lines, notes) per slide."""
    A = "http://schemas.openxmlformats.org/drawingml/2006/main"
    P = "http://schemas.openxmlformats.org/presentationml/2006/main"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("docProps/core.xml", "<cp:coreProperties/>")
        for number, (lines, notes) in enumerate(slides, start=1):
            paragraphs = "".join(
                f'<a:p xmlns:a="{A}"><a:r><a:t>{line}</a:t></a:r></a:p>' for line in lines
            )
            archive.writestr(
                f"ppt/slides/slide{number}.xml",
                f'<p:sld xmlns:p="{P}" xmlns:a="{A}"><p:cSld><p:spTree>{paragraphs}</p:spTree></p:cSld></p:sld>',
            )
            if notes:
                note_paras = "".join(
                    f'<a:p xmlns:a="{A}"><a:r><a:t>{n}</a:t></a:r></a:p>' for n in notes
                )
                archive.writestr(
                    f"ppt/notesSlides/notesSlide{number}.xml",
                    f'<p:notes xmlns:p="{P}" xmlns:a="{A}">{note_paras}</p:notes>',
                )
    return buffer.getvalue()


def _xlsx(sheets: dict[str, list[list[str]]]) -> bytes:
    """A minimal but real .xlsx with inline strings."""
    NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        named = "".join(f'<sheet name="{name}"/>' for name in sheets)
        archive.writestr("xl/workbook.xml", f'<workbook xmlns="{NS}"><sheets>{named}</sheets></workbook>')
        for index, rows in enumerate(sheets.values(), start=1):
            body = []
            for row_number, row in enumerate(rows, start=1):
                cells = []
                for column, value in enumerate(row):
                    ref = f"{chr(65 + column)}{row_number}"
                    cells.append(
                        f'<c r="{ref}" t="inlineStr"><is><t>{value}</t></is></c>' if value else f'<c r="{ref}"/>'
                    )
                body.append(f'<row r="{row_number}">{"".join(cells)}</row>')
            archive.writestr(
                f"xl/worksheets/sheet{index}.xml",
                f'<worksheet xmlns="{NS}"><sheetData>{"".join(body)}</sheetData></worksheet>',
            )
    return buffer.getvalue()


def _png() -> bytes:
    from PIL import Image

    picture = Image.new("RGB", (40, 30), "white")
    buffer = io.BytesIO()
    picture.save(buffer, format="PNG")
    return buffer.getvalue()


# ------------------------------- the formats -------------------------------


def test_every_new_format_is_advertised_and_accepted():
    """The API refuses what it cannot read while the caller is still listening,
    so the list it refuses against has to be the real one."""
    for name in ("deck.pptx", "book.xlsx", "data.csv", "shot.png", "photo.jpeg"):
        assert can_parse(name), name
    for extension in (".pptx", ".xlsx", ".csv", ".png", ".jpeg", ".webp"):
        assert extension in supported()


def test_a_deck_is_indexed_slide_by_slide():
    """Slides become the same page markers a PDF gets, so the page index, the
    citations and the trail treat a deck exactly as they treat a document."""
    data = _pptx(
        [
            (["Quarterly Review", "Revenue is up"], []),
            (["Risks", "Supply chain is tight"], ["The 30% figure is pre-adjustment"]),
        ]
    )
    parsed = normalise(data, "review.pptx")

    assert "<!-- page 1 -->" in parsed.body and "<!-- page 2 -->" in parsed.body
    assert "# Quarterly Review" in parsed.body, "a slide's first line is its heading"
    assert parsed.metadata["slides"] == 2


def test_speaker_notes_are_kept():
    """The note is frequently where the real claim lives. A store that dropped
    them would answer confidently from a headline the note qualifies."""
    data = _pptx([(["Growth of 30%"], ["The 30% figure is pre-adjustment"])])
    parsed = normalise(data, "deck.pptx")
    assert "pre-adjustment" in parsed.body
    assert "Speaker notes" in parsed.body


def test_slides_are_read_in_slide_order_not_zip_order():
    """The order parts happen to be zipped in is arbitrary, and reading a deck
    out of order scrambles its argument."""
    data = _pptx([(["First"], []), (["Second"], []), (["Third"], [])])
    body = normalise(data, "deck.pptx").body
    assert body.index("First") < body.index("Second") < body.index("Third")


def test_a_workbook_keeps_its_grid():
    """The grid IS the content. Flattened into a stream of values it becomes
    the unreadable-table problem all over again."""
    data = _xlsx(
        {
            "Q3 forecast": [
                ["Region", "2024", "2025"],
                ["North", "18.4", "19.7"],
                ["South", "12.9", "11.2"],
            ]
        }
    )
    parsed = normalise(data, "forecast.xlsx")

    assert "## Q3 forecast" in parsed.body, "the sheet NAME is what people ask about"
    assert "| Region | 2024 | 2025 |" in parsed.body
    assert "| North | 18.4 | 19.7 |" in parsed.body


def test_a_workbook_with_several_sheets_keeps_them_apart():
    data = _xlsx({"Summary": [["A", "B"], ["1", "2"]], "Detail": [["C"], ["3"]]})
    parsed = normalise(data, "book.xlsx")
    assert "## Summary" in parsed.body and "## Detail" in parsed.body
    assert parsed.metadata["sheets"] == 2


def test_a_workbook_with_no_values_is_refused_rather_than_indexed_empty():
    """A file saved without cached values has nothing to index. An empty item
    looks stored and can never be found."""
    with pytest.raises(UnsupportedFormat):
        normalise(_xlsx({"Empty": []}), "empty.xlsx")


def test_a_csv_becomes_a_table_and_its_delimiter_is_sniffed():
    """A European export is semicolon separated; reading it as commas produces
    one column of gibberish."""
    comma = normalise(b"name,qty\nwidget,4\n", "a.csv").body
    assert "| name | qty |" in comma and "| widget | 4 |" in comma

    semi = normalise(b"name;qty\nwidget;4\n", "b.csv").body
    assert "| name | qty |" in semi, "the delimiter must be sniffed, not assumed"


def test_an_image_refuses_to_parse_and_says_why():
    """An image has no text layer because it has no text. Refusing here routes
    it to the vision path — the same route a scanned PDF takes."""
    with pytest.raises(PictureDocument) as raised:
        normalise(_png(), "screenshot.png")

    # A subclass of ScannedDocument, so the pipeline's existing recovery
    # applies unchanged rather than being written a second time.
    assert isinstance(raised.value, ScannedDocument)
    assert raised.value.page_count == 1


def test_an_image_is_its_own_single_page():
    """An image IS a page — the only one it has — so a citation from a
    photograph can show the photograph."""
    from packages.core import pages

    assert pages.renderable("image/png", "shot.png")
    assert pages._count_sync(_png()) == 1
    rendered = pages._render_sync(_png(), 1)
    assert rendered[:8] == b"\x89PNG\r\n\x1a\n", "pages are always served as PNG"


def test_a_spreadsheet_has_no_page_to_show():
    """Not everything has a picture, and pretending otherwise would put an
    empty frame behind a citation."""
    from packages.core import pages

    assert not pages.renderable("application/vnd.ms-excel", "book.xlsx")
    assert not pages.renderable("application/vnd.ms-powerpoint", "deck.pptx")


async def test_a_picture_is_titled_by_its_filename_not_by_its_description(monkeypatch):
    """The first line of a vision description is a sentence ABOUT the file —
    "This image is a bar chart." — and a document list titled that way tells a
    reader nothing about which file is which. A picture's filename is the only
    name it has."""
    from packages.core import pipeline

    monkeypatch.setattr("packages.core.pages.available", lambda: True)

    async def described(data, limit=None):
        return ["This image is a bar chart. Support tickets by channel."], 1

    monkeypatch.setattr("packages.core.pages.transcribe_document", described)
    parsed = await pipeline._read_scan(_png(), "tickets-chart.png", Exception())

    assert parsed is not None
    assert parsed.title == "Tickets chart"
    assert parsed.metadata["format"] == "png", "a PNG must not be recorded as a pdf"
    assert parsed.metadata["read_by"] == "vision"


async def test_a_scanned_pdf_still_takes_its_title_from_the_page(monkeypatch):
    """The filename rule is for pictures only: a scan's first line really is
    its heading, and "scan 2026 final v3" is a worse title than the one the
    document gives itself."""
    from packages.core import pipeline

    monkeypatch.setattr("packages.core.pages.available", lambda: True)

    async def transcribed(data, limit=None):
        return ["# Segment Results 2025\n\nUnaudited figures."], 1

    monkeypatch.setattr("packages.core.pages.transcribe_document", transcribed)
    parsed = await pipeline._read_scan(b"%PDF-1.4 fake", "scan-2026-final-v3.pdf", Exception())

    assert parsed is not None
    assert parsed.title == "Segment Results 2025"
    assert parsed.metadata["format"] == "pdf"


def test_a_deck_does_not_claim_a_page_picture():
    """A deck writes the same page markers a PDF does — deliberately, so slides
    index and cite like pages. That made a citation offer a picture of slide 3
    that nothing can produce, and the console drew a broken image. A promise the
    store cannot keep is worse than no promise."""
    from packages.core.navigator import _has_pictures

    assert _has_pictures({"locator": "report.pdf"})
    assert _has_pictures({"locator": "photo.JPG"})
    assert not _has_pictures({"locator": "deck.pptx"})
    assert not _has_pictures({"locator": "budget.xlsx"})
    assert not _has_pictures({"locator": "notes.md"})
    assert not _has_pictures({})


# --------------------- what a REAL workbook looks like ---------------------
#
# The first fixtures here were written by me and passed happily. A real export
# then failed in three ways at once, none of which a hand-made file exhibits.


def _real_world_xlsx() -> bytes:
    """A workbook laid out the way people actually lay one out: a margin
    column, a title block, then the headings — with dates and percentages
    stored as the numbers Excel stores them as."""
    NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "xl/workbook.xml",
            f'<workbook xmlns="{NS}"><sheets><sheet name="Plan"/></sheets></workbook>',
        )
        # style 1 is a date format, style 2 a percentage, style 0 plain
        archive.writestr(
            "xl/styles.xml",
            f'<styleSheet xmlns="{NS}"><cellXfs count="3">'
            '<xf numFmtId="0"/><xf numFmtId="14"/><xf numFmtId="9"/>'
            "</cellXfs></styleSheet>",
        )
        rows = [
            # a blank A column, a title in B, then the header, then data
            ('<row r="1"><c r="B1" t="inlineStr"><is><t>Excel Sample Data</t></is></c></row>'),
            ('<row r="2"><c r="B2" t="inlineStr"><is><t>Project Management Data</t></is></c></row>'),
            (
                '<row r="3">'
                '<c r="B3" t="inlineStr"><is><t>Task</t></is></c>'
                '<c r="C3" t="inlineStr"><is><t>Start Date</t></is></c>'
                '<c r="D3" t="inlineStr"><is><t>Progress</t></is></c>'
                "</row>"
            ),
            (
                '<row r="4">'
                '<c r="B4" t="inlineStr"><is><t>Market Research</t></is></c>'
                '<c r="C4" s="1"><v>45292</v></c>'
                '<c r="D4" s="2"><v>0.78</v></c>'
                "</row>"
            ),
        ]
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            f'<worksheet xmlns="{NS}"><sheetData>{"".join(rows)}</sheetData></worksheet>',
        )
    return buffer.getvalue()


def test_dates_are_read_as_dates_not_as_the_numbers_excel_stores():
    """Excel keeps 2024-01-01 as 45292 and puts the fact that it is a date in
    the cell's FORMAT. Ignore that and the store answers a question about a
    start date with a five-digit number — wrong in a way no reader would
    catch."""
    body = normalise(_real_world_xlsx(), "plan.xlsx").body
    assert "2024-01-01" in body
    assert "45292" not in body


def test_percentages_are_not_left_two_orders_of_magnitude_out():
    """Excel stores 78% as 0.78. Printing the stored number makes every answer
    derived from it wrong by a factor of a hundred."""
    body = normalise(_real_world_xlsx(), "plan.xlsx").body
    assert "78%" in body
    assert "| 0.78 |" not in body


def test_a_margin_column_and_a_title_block_do_not_become_the_header():
    """Nobody starts typing in A1. Taken literally, the first row becomes the
    header — so the columns are named "" and "Excel Sample Data", and every
    value sits under a heading that does not describe it."""
    body = normalise(_real_world_xlsx(), "plan.xlsx").body

    assert "| Task | Start Date | Progress |" in body, "the real header row must win"
    assert "| Excel Sample Data |" not in body, "the title block is not a header"
    # The title block is kept as text — "Project Management Data" is exactly
    # what somebody will search for — just not as column names.
    assert "Project Management Data" in body


def test_the_real_project_workbook_reads_correctly():
    """The file that failed. Kept as a test because a fixture I wrote myself
    passed while this did not."""
    body = normalise(_real_world_xlsx(), "Project-Management-Sample-Data.xlsx").body
    assert "| Market Research | 2024-01-01 | 78% |" in body


def test_a_csv_is_titled_by_its_filename_not_by_its_header_row():
    """A CSV's first line is its header, so title_from produced documents
    called "| Ticket | Client | Hours | Status |" — found in the format matrix,
    and it is what a person reads in every result list, every citation and
    every deletion dialog. A file of pure data has no title inside it."""
    data = b"Ticket,Client,Hours\nT-1041,Gharsoaps,6.5\n"
    assert normalise(data, "quarterly-spend_2026.csv").title == "Quarterly spend 2026"
    assert "|" not in normalise(data, "tickets.csv").title
