"""Where a citation came from, and whether a picture of it exists.

MarkVector is a knowledge base other applications build on, so the audience
for a citation is a program, not our console. That distinction is the whole
point of this file: the previous behaviour withheld a deck's slide number
because OUR console rendered a broken image when it offered a picture nothing
could produce. The remedy was aimed at the wrong layer — it fixed one
consumer's rendering by making the API answer a smaller question than it knew
the answer to, and every other consumer lost the ability to say "slide 34".

So: the location is always returned when it is known, and whether a picture
can be fetched is STATED rather than discovered by requesting one and reading
a 404.
"""

from __future__ import annotations

import inspect

from packages.core import answer, pages, tree

DECK = (
    "<!-- page 1 -->\n# Lecture 4\nHolistic development\n\n"
    "<!-- page 34 -->\n14-16 kg grain and 21,000 litres of water to 1 kg meat\n\n"
    "<!-- page 79 -->\n# Closing\n"
)


# ------------------------------ counting parts ------------------------------


def test_a_documents_parts_are_counted_from_its_markers():
    assert tree.page_count(DECK) == 79


def test_the_count_is_the_highest_marker_not_the_number_of_them():
    """A deck of 79 slides whose empty slides wrote no marker would otherwise
    report 74 — a number that contradicts the citation saying "slide 79"."""
    assert tree.page_count(DECK) == 79
    assert DECK.count("<!-- page") == 3


def test_a_document_with_no_pages_counts_none():
    """Pasted text, Markdown, a spreadsheet. No location is invented for them."""
    assert tree.page_count("# Notes\n\nNothing paginated here.") == 0
    assert tree.page_count("") == 0


# --------------------------- naming them correctly ---------------------------


def test_a_deck_has_slides_and_a_pdf_has_pages():
    """Nobody says "page 12 of the deck". The consuming application shows this
    word to its own users, and deriving it from the file extension is the sort
    of per-format special case an API exists to absorb."""
    assert pages.unit("lecture.pptx") == "slide"
    assert pages.unit("book.pdf") == "page"
    assert pages.label(34, "lecture.pptx") == "slide 34"
    assert pages.label(12, "book.pdf") == "page 12"


def test_no_location_means_no_label():
    assert pages.label(None, "lecture.pptx") is None


# ------------------- the location survives, the picture is declared -------------------


def test_the_location_is_no_longer_withheld_from_unrenderable_documents():
    """The regression this file exists for.

    The old code filtered the documents it would resolve a page for down to
    those that could be RENDERED, so a deck's citation came back page: null
    with the slide known and its marker sitting in the body.
    """
    source = inspect.getsource(answer._attach_pages)
    # Renderability may gate the PICTURE and nothing else, so the location is
    # already assigned by the time it is consulted.
    assert source.index("citation.page = page") < source.index("pages.renderable")
    assert source.index("citation.page_label") < source.index("pages.renderable")
    assert "citation.page_image" in source


def test_the_picture_url_is_offered_only_when_there_is_a_picture():
    source = inspect.getsource(answer._attach_pages)
    picture = source.split("citation.page_label")[1]
    assert "pages.renderable" in picture
    assert "/pages/" in picture


def test_a_citation_carries_all_three_fields_to_the_caller():
    """page, page_label and page_image are part of the response contract, not
    internal state — an application reads them to render its own citation."""
    fields = {f.name for f in answer.Citation.__dataclass_fields__.values()}
    assert {"page", "page_label", "page_image"} <= fields

    serialised = inspect.getsource(answer.Answer.as_dict)
    for key in ('"page"', '"page_label"', '"page_image"'):
        assert key in serialised


def test_every_citation_is_enriched_not_only_the_ones_missing_a_page():
    """A passage cut by the page index arrives with its page already set. It
    needs the label and the picture URL just as much as one resolved here, or
    the same document describes itself two ways depending on which retrieval
    mode answered."""
    source = inspect.getsource(answer._attach_pages)
    assert "wanted = citations" in source
    assert "citation.page or tree.page_containing" in source


# --------------------------- the item declares itself ---------------------------


def test_an_item_declares_what_can_be_asked_of_it():
    """An application used to learn that a deck has no page pictures by
    requesting one and reading a 404 — a capability check disguised as an
    error, indistinguishable from "the object store is down"."""
    from apps.api.main import _addressability

    source = inspect.getsource(_addressability)
    for field in ("pages", "page_unit", "page_image"):
        assert f'"{field}"' in source


def test_a_deck_declares_slides_but_no_pictures():
    from apps.api.main import _addressability
    from packages.shared.schema import SourceRef

    class _Item:
        body = DECK
        source = SourceRef(source="upload", locator="lecture.pptx")

    declared = _addressability(_Item())
    assert declared == {"pages": 79, "page_unit": "slide", "page_image": False}


def test_a_pdf_declares_pages_and_pictures():
    from apps.api.main import _addressability
    from packages.shared.schema import SourceRef

    class _Item:
        body = DECK.replace("page", "page")  # same markers, different origin
        source = SourceRef(source="upload", locator="book.pdf")

    declared = _addressability(_Item())
    assert declared["page_unit"] == "page"
    assert declared["page_image"] is True


def test_a_document_with_no_pages_declares_nothing_to_address():
    """Not "page 1 of 1". A spreadsheet has no page, and saying so plainly is
    what stops a caller building a page selector it can never use."""
    from apps.api.main import _addressability
    from packages.shared.schema import SourceRef

    class _Item:
        body = "| Vendor | Amount |\n|---|---|\n| Cloudlift | 41200 |"
        source = SourceRef(source="upload", locator="spend.xlsx")

    assert _addressability(_Item()) == {
        "pages": 0,
        "page_unit": None,
        "page_image": False,
    }
