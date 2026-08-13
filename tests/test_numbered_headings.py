"""Structure a document states in NUMBERS rather than in words.

Found on a real book: Tanenbaum's *Computer Networks*, 962 pages, 2.4 MB. It
says CHAPTER only inside a running page header ("36 INTRODUCTION CHAP. 1"), so
the spoken-heading pass found nothing at all, and all 4,035 stored passages came
out with an EMPTY heading — ONE distinct heading across the whole book. The
agent's table of contents was 4,035 unnamed sections, which is not a table of
contents; it is the same blank four thousand times.

The book states its shape the way technical books, standards, specifications and
tenders do: "1.3 NETWORK SOFTWARE", "5.2.2 Shortest Path Algorithm".
"""

from __future__ import annotations

from packages.core.chunk import split
from packages.core.normalise import (
    MIN_NUMBERED_HEADINGS,
    find_structure,
    mark_numbered_headings,
    title_from,
)

def _prose(subject: str) -> str:
    # Long enough that each section becomes its own passage. The chunker packs
    # to about a kilobyte, so a fixture of one-line sections collapses into a
    # single chunk and proves nothing about headings.
    return (f"Discussion of {subject} at the length a real section runs to. " * 22).strip()


BOOK = "\n\n".join(
    [
        "1.1 USES OF COMPUTER NETWORKS",
        _prose("what networks are for"),
        "1.1.1 Business Applications",
        _prose("business use"),
        "1.1.2 Home Applications",
        _prose("the home"),
        "1.2 NETWORK HARDWARE",
        _prose("hardware"),
        "1.2.1 Personal Area Networks",
        _prose("personal area networks"),
        "1.2.2 Local Area Networks",
        _prose("local area networks"),
        "1.3 NETWORK SOFTWARE",
        _prose("software"),
        "1.3.1 Protocol Hierarchies",
        _prose("protocol hierarchies"),
        "2.2.2 Twisted Pairs",
        _prose("copper"),
    ]
)


def test_a_numbered_heading_becomes_a_heading():
    marked = mark_numbered_headings(BOOK)
    assert "## 1.1 USES OF COMPUTER NETWORKS" in marked
    assert "### 1.1.1 Business Applications" in marked


def test_numbering_decides_the_nesting():
    """1.3.1 sits inside 1.3, so a citation reads as an address rather than as
    one of hundreds of identical labels."""
    pieces = split(mark_numbered_headings(BOOK))
    headings = {p.heading for p in pieces if p.heading}
    assert "1.3 NETWORK SOFTWARE > 1.3.1 Protocol Hierarchies" in headings


def test_the_passages_carry_the_headings():
    """The chunker parses headings independently of the outline.

    A fix that only taught the tree would leave every stored passage still
    headingless — which is exactly what happened the first time this class of
    bug was fixed.
    """
    pieces = split(mark_numbered_headings(BOOK))
    assert len({p.heading for p in pieces if p.heading}) >= 8


def test_a_table_of_contents_is_not_a_set_of_headings():
    """The same lines, with a page number on the end.

    The book carries 656 numbered lines: 327 real headings and 329 contents
    entries. Promoting the contents shreds the index page into three hundred
    sections that contain nothing.
    """
    contents = "\n".join(
        [
            "1.1 USES OF COMPUTER NETWORKS, 3",
            "1.1.1 Business Applications, 3",
            "1.1.2 Home Applications, 6",
            "1.1.3 Mobile Users, 10",
            "1.2 NETWORK HARDWARE, 17",
            "1.2.1 Personal Area Networks, 18",
            "1.2.2 Local Area Networks, 19",
            "1.3 NETWORK SOFTWARE, 29",
            "1.3.1 Protocol Hierarchies, 29",
            "1.3.2 Design Issues, 33",
        ]
    )
    assert mark_numbered_headings(contents) == contents


def test_a_page_number_is_not_a_heading():
    """Every page of a printed book begins with one. At least one dot is
    required precisely so a bare number can never qualify."""
    paged = "\n\n".join(f"36 INTRODUCTION CHAP. 1\nProse on page {n}." for n in range(20))
    assert "#" not in mark_numbered_headings(paged)


def test_a_short_numbered_list_is_left_alone():
    """Numbering is structure only when it is used throughout. A handful is a
    list, a version history, or a couple of cross-references."""
    short = "1.1 First point\ntext\n1.2 Second point\ntext\n1.3 Third point\ntext"
    assert mark_numbered_headings(short) == short
    assert MIN_NUMBERED_HEADINGS >= 5


def test_an_authored_document_is_never_second_guessed():
    """One with Markdown headings has real structure, and guessing more on top
    of it can only damage what its author wrote."""
    authored = "# Real Heading\n\n1.1 Not a heading here\n\n" + BOOK
    assert mark_numbered_headings(authored) == authored


def test_both_passes_run_and_neither_undoes_the_other():
    marked = find_structure(BOOK)
    assert "## 1.1 USES OF COMPUTER NETWORKS" in marked


# --------------------------------- the title ---------------------------------


def test_a_book_is_not_named_after_its_blank_page():
    """A 962-page textbook entered the store titled "This page intentionally
    left blank" — the first line of text in the file, because the cover is a
    picture with no text layer.

    Front matter is exactly where a scan's first WORDS live, so this is not a
    rare case, and a store whose books are named after their blank pages cannot
    be browsed at all.
    """
    body = (
        "<!-- page 2 -->\nThis page intentionally left blank\n\n---\n\n"
        "<!-- page 3 -->\nCOMPUTER NETWORKS\nFIFTH EDITION"
    )
    assert title_from(body, "computer-networks.pdf") == "COMPUTER NETWORKS"


def test_other_printed_boilerplate_is_skipped_too():
    for boilerplate in (
        "Blank page",
        "All rights reserved",
        "CONFIDENTIAL",
        "Table of Contents",
        "Page 4 of 12",
    ):
        body = f"<!-- page 1 -->\n{boilerplate}\n\n---\n\n<!-- page 2 -->\nThe Real Title"
        assert title_from(body, "x.pdf") == "The Real Title", boilerplate


def test_a_document_that_is_only_boilerplate_falls_back_to_its_filename():
    # Never returns the boilerplate anyway: the filename is at least the name
    # somebody chose.
    body = "<!-- page 1 -->\nThis page intentionally left blank"
    assert title_from(body, "reports/q3.pdf") == "q3.pdf"


def test_a_real_title_that_merely_mentions_contents_survives():
    # Matched as a whole line, so a document actually called this keeps it.
    body = "Table of Contents Management in Distributed Systems"
    assert title_from(body, "x.pdf").startswith("Table of Contents Management")
