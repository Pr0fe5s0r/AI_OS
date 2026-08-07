"""Structure a document states in words instead of in Markdown.

Found by uploading a real book. A Project Gutenberg *War and Peace* has 730
lines beginning CHAPTER and 30 beginning BOOK, and **zero** Markdown headings.
Everything downstream reads Markdown, so 3.2 MB arrived as one unstructured
blob and the damage was everywhere at once:

    the chunker gave all 4,296 passages an empty heading
    section summaries collapsed from ~700 into 1
    the agent's table of contents read "Part 1 of 1475"
    catalogue retrieval answered "Nothing in these documents answers that"

This is not a novel-reading feature. A statute has ARTICLEs, a tender has
SECTIONs, a circular has ANNEXUREs — and a PDF whose text layer lost its
formatting loses all of them the same way.

Measured after the fix on the same file: 767 headings, 768 tree sections, and
the stored index went from 1 distinct heading to 358.
"""

from __future__ import annotations

import re

from packages.core import tree
from packages.core.normalise import MIN_SPOKEN_HEADINGS, mark_spoken_headings, tidy

BOOK = "\n\n".join(
    ["BOOK ONE: 1805"]
    + [f"CHAPTER {n}\n\nProse belonging to chapter {n}, at some length." for n in "I II III".split()]
    + ["BOOK TWO: 1807"]
    + [f"CHAPTER {n}\n\nMore prose, in the second book, chapter {n}." for n in "I II".split()]
)


def test_a_document_that_states_its_structure_gets_it():
    marked = mark_spoken_headings(BOOK)
    assert "# BOOK ONE: 1805" in marked
    assert "## CHAPTER I" in marked
    # Parts nest above chapters, which is what turns thirty-four identical
    # "CHAPTER I" labels into addresses you can tell apart.
    root = tree.build(marked, "A Novel")
    titles = [(n.level, n.title) for n in root.walk()]
    assert (1, "BOOK ONE: 1805") in titles
    assert (2, "CHAPTER I") in titles
    assert tree.count(root) > 5


def test_a_document_that_already_has_markdown_is_left_alone():
    """One that has real headings has real structure, written by its author.
    Guessing more on top of it can only damage what is already correct."""
    authored = "# Real Heading\n\nSECTION 1\n\nSECTION 2\n\nSECTION 3\n"
    assert mark_spoken_headings(authored) == authored


def test_the_words_are_only_headings_on_their_own_line():
    """"under section 5 of the Act" appears constantly in legal prose. Matching
    it mid-sentence would shred a document into hundreds of false sections."""
    prose = (
        "The requirement under section 5 of the Act applies.\n"
        "As set out in chapter 3, the schedule is fixed.\n"
        "Nothing in part 2 changes that.\n"
    )
    assert mark_spoken_headings(prose) == prose


def test_a_bare_structural_word_is_not_a_heading():
    """A real book has lines reading just "book." — a sentence that happened to
    end there. Promoting those produced stored headings like
    "book. > CHAPTER X", which is worse than no heading at all because it looks
    like an address and points nowhere."""
    marked = mark_spoken_headings(BOOK.replace("BOOK TWO: 1807", "book."))
    assert "# book." not in marked
    for heading in re.findall(r"(?m)^#{1,6} (.+)$", marked):
        rest = heading.split(None, 1)
        assert len(rest) > 1 and re.search(r"[0-9A-Za-z]", rest[1]), (
            f"heading {heading!r} names no part"
        )


def test_too_few_matches_is_not_structure():
    """Below the threshold the pattern is likelier noise, and arbitrary blocks
    are the more honest answer than invented sections."""
    thin = "SECTION 1\n\nsome text\n"
    assert mark_spoken_headings(thin) == thin
    assert MIN_SPOKEN_HEADINGS >= 3


def test_it_runs_as_part_of_the_one_cleanup_every_parser_shares():
    """In tidy(), not in the tree — because the tree is not the only reader.

    The first attempt taught the OUTLINE to find these headings and stopped
    there. The chunker parses headings independently, so every stored passage
    was still headingless: vectorless improved and hybrid, the section
    summaries and the whole stored index did not.
    """
    assert "## CHAPTER I" in tidy(BOOK)
