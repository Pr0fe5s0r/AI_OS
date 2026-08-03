from __future__ import annotations

import pytest

from packages.core.normalise import (
    UnsupportedFormat,
    can_parse,
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


def test_control_bytes_are_stripped():
    """PDF extraction leaves NUL and stray control bytes. Postgres rejects NUL
    in a text column, so one anywhere fails the whole write — they are dropped
    here, keeping only tab and newline."""
    assert "\x00" not in tidy("Bro\x00caly")
    assert tidy("Bro\x00caly") == "Brocaly"
    # Form feed (a PDF page break) and other C0 controls are deleted outright.
    assert tidy("a\x0cb") == "ab"
    # Tab and newline survive the control strip — a tab is then collapsed to a
    # space by the whitespace pass, not removed like a control byte.
    assert tidy("a\tb") == "a b"
    assert tidy("line1\nline2") == "line1\nline2"


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
    assert ".md" in formats and ".pdf" in formats and ".docx" in formats


def test_can_parse_answers_before_the_bytes_are_read():
    """The API asks this while the uploader is still listening. Getting it
    wrong means accepting a file that can only fail in a worker."""
    assert can_parse("report.DOCX") and can_parse("notes.md")
    assert not can_parse("deck.pptx") and not can_parse("sheet.xlsx")


# ------------------------------- Word documents -------------------------------

_NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def _docx(body_xml: str, title: str | None = None) -> bytes:
    """The smallest real .docx: a zip with the one part the parser reads."""
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", f"<w:document {_NS}><w:body>{body_xml}</w:body></w:document>")
        if title is not None:
            archive.writestr(
                "docProps/core.xml",
                f'<cp:coreProperties xmlns:cp="c" xmlns:dc="http://purl.org/dc/elements/1.1/">'
                f"<dc:title>{title}</dc:title></cp:coreProperties>",
            )
    return buffer.getvalue()


def _para(text: str, style: str | None = None, numbered: bool = False) -> str:
    props = ""
    if style or numbered:
        props = "<w:pPr>"
        if style:
            props += f'<w:pStyle w:val="{style}"/>'
        if numbered:
            props += "<w:numPr/>"
        props += "</w:pPr>"
    return f"<w:p>{props}<w:r><w:t>{text}</w:t></w:r></w:p>"


def test_word_structure_survives_as_markdown():
    out = normalise(
        _docx(_para("Overview", "Heading1") + _para("Body text.") + _para("first", numbered=True)),
        "spec.docx",
    )
    assert "# Overview" in out.body
    assert "Body text." in out.body
    assert "- first" in out.body
    assert out.metadata["format"] == "docx"


def test_word_tables_keep_their_columns():
    """Retrieval reads the flattened text, but a person reading the stored
    Markdown should still see which value sat in which column."""
    cells = "".join(f"<w:tc><w:p><w:r><w:t>{v}</w:t></w:r></w:p></w:tc>" for v in ("Field", "Detail"))
    row = "".join(f"<w:tc><w:p><w:r><w:t>{v}</w:t></w:r></w:p></w:tc>" for v in ("Version", "0.1"))
    table = f"<w:tbl><w:tr>{cells}</w:tr><w:tr>{row}</w:tr></w:tbl>"
    body = normalise(_docx(table), "spec.docx").body
    assert "| Field | Detail |" in body
    assert "| --- | --- |" in body
    assert "| Version | 0.1 |" in body


def test_word_body_order_is_preserved():
    """Paragraphs and tables interleave. Collecting all of one then all of the
    other would silently rearrange a document whose tables carry the substance."""
    table = "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>middle</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
    body = normalise(_docx(_para("before") + table + _para("after")), "x.docx").body
    assert body.index("before") < body.index("middle") < body.index("after")


def test_word_prefers_the_documents_declared_title():
    out = normalise(_docx(_para("Some opening line"), title="Requirements Specification"), "x.docx")
    assert out.title == "Requirements Specification"


def test_a_legacy_doc_renamed_to_docx_says_so():
    """The common mistake, and a stack trace about a missing zip entry is no
    use to the person who made it."""
    with pytest.raises(UnsupportedFormat) as exc:
        normalise(b"\xd0\xcf\x11\xe0not a zip", "old.docx")
    assert ".doc" in str(exc.value)
