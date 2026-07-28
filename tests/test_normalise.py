from __future__ import annotations

import pytest

from packages.core.normalise import (
    UnsupportedFormat,
    normalise,
    normalise_text,
    supported,
    tidy,
    title_from,
)


def test_extraction_padding_is_collapsed():
    """Page breaks and justified spacing arrive as ragged whitespace.

    Left alone it is dead weight in the embedding and reads as a rendering
    fault on screen, so it is removed once rather than by each parser.
    """
    assert tidy("a\r\n\r\n\r\n\r\nb") == "a\n\nb"
    assert tidy("too    many     spaces") == "too many spaces"
    assert tidy("  padded  \n   lines   ") == "padded\nlines"


def test_a_title_is_always_found():
    assert title_from("# Q2 Report\n\nbody", "x.md") == "Q2 Report"
    # Nothing usable in the content: fall back to the filename, never empty.
    assert title_from("\n\n.\n", "reports/june.pdf") == "june.pdf"


def test_our_own_scaffolding_is_never_the_title():
    """The PDF parser inserts page markers and rules. Titling an item with
    them made every document in the index read as '<!-- page 1 -->'."""
    body = "<!-- page 1 -->\nQ2 Performance Review\n\n---\n\nmore text"
    assert title_from(body, "q2.pdf") == "Q2 Performance Review"


def test_markdown_passes_through_as_itself():
    out = normalise(b"# Heading\n\nSome content.", "note.md")
    assert out.title == "Heading"
    assert "Some content." in out.body


def test_an_unreadable_format_fails_loudly():
    """A format we cannot read must surface as a visible, re-runnable failure —
    never as an item that exists but says nothing."""
    with pytest.raises(UnsupportedFormat) as exc:
        normalise(b"\x00\x01", "archive.zip")
    assert "zip" in str(exc.value)


def test_text_ingestion_keeps_a_given_title():
    out = normalise_text("line one\nline two", title="Explicit Title")
    assert out.title == "Explicit Title"
    assert out.body == "line one\nline two"


def test_supported_formats_are_advertised():
    formats = supported()
    assert ".md" in formats and ".pdf" in formats
