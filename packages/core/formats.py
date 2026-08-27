from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from packages.core.normalise import _REGISTRY

# ---------------------------------------------------------------------------
# THE FORMAT CONTRACT.
#
# "Which formats land natively, and what does a passage look like when they
# do?" is a question a platform building against this has to answer before it
# decides what to convert on its own side. A bare list of extensions does not
# answer it: knowing that .pptx is accepted says nothing about whether a
# citation comes back as "slide 12" or as one blob per deck.
#
# So each family declares its UNIT OF CITATION — what one retrieved passage
# corresponds to in the original — plus what is dropped and what is not
# supported. That is the part callers design against.
#
# VERSIONED, because the client is right to want it: they are building
# conversion logic on the other side of this list, and a format that starts
# landing natively changes their pipeline. The version moves when the SET of
# formats changes or when a family's unit of citation changes — not when a
# parser gets better at the same job.
#
# The descriptions are keyed by parser class and CHECKED against the live
# registry (tests/test_formats.py), so a parser added without documentation
# fails CI rather than shipping as an undocumented extension.
# ---------------------------------------------------------------------------

VERSION = "1.0.0"


@dataclass(frozen=True, slots=True)
class Format:
    family: str
    extensions: tuple[str, ...]
    # What one retrieved passage corresponds to in the original document. This
    # is the field a caller designs their citations around.
    unit: str
    notes: str = ""
    # Things this format does NOT carry into the index. Stated because silence
    # here reads as a promise.
    drops: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {
            "family": self.family,
            "extensions": list(self.extensions),
            "unit_of_citation": self.unit,
            "notes": self.notes,
            "drops": list(self.drops),
        }


_DESCRIBED: dict[str, Format] = {
    "TextParser": Format(
        family="Plain text and Markdown",
        extensions=(".txt", ".text", ".md", ".markdown"),
        unit="a section under its nearest heading",
        notes=(
            "Markdown headings are used as-is. Plain text is scanned for "
            "numbered and spoken headings so a long document still has "
            "structure to split on."
        ),
    ),
    "PdfParser": Format(
        family="PDF",
        extensions=(".pdf",),
        unit="a section under its nearest heading, carrying its page number",
        notes=(
            "Both text-native and scanned PDFs. A scanned PDF is transcribed "
            "page by page with a vision model, capped at MAX_TRANSCRIBE_PAGES "
            "(200) — a truncated transcription says so on the document rather "
            "than being quietly short. Page pictures are rendered on demand up "
            "to 5,000 pages, and figures on a cited page are cut out and shown "
            "with the answer."
        ),
        drops=("page furniture (running heads and folios)",),
    ),
    "DocxParser": Format(
        family="Word",
        extensions=(".docx",),
        unit="a section under its nearest heading",
        notes=(
            "Paragraphs and tables are read in document order, so a document "
            "whose substance is in its tables is not silently rearranged. "
            "Tables keep their columns as Markdown rows."
        ),
        drops=("fonts and colours", "comments", "tracked changes", "page numbers"),
    ),
    "PptxParser": Format(
        family="PowerPoint",
        extensions=(".pptx",),
        unit="one slide — title, body and speaker notes together",
        notes=(
            "Slides are read in slide order, not archive order, so slide 10 "
            "does not land between slide 1 and slide 2. Speaker notes are kept "
            "with their slide, because they frequently hold the argument the "
            "slide only gestures at."
        ),
        drops=("slide masters and layouts", "animations", "images (no alt text in OOXML)"),
    ),
    "XlsxParser": Format(
        family="Excel",
        extensions=(".xlsx", ".xlsm"),
        unit="a sheet, as a table with its header row",
        notes=(
            "Each sheet is kept separate and its grid preserved, so a value "
            "stays attached to the column heading that gives it meaning. Dates "
            "are read as dates rather than as Excel's serial numbers, and "
            "percentages are not left two orders of magnitude out."
        ),
        drops=("formulas (the computed value is kept)", "charts", "cell formatting"),
    ),
    "CsvParser": Format(
        family="CSV and TSV",
        extensions=(".csv", ".tsv"),
        unit="a table with its header row",
        notes="The delimiter is sniffed rather than assumed.",
    ),
    "HtmlParser": Format(
        family="HTML",
        extensions=(".html", ".htm", ".xhtml"),
        unit="a section under its nearest heading",
        notes=(
            "Headings, lists and tables survive; tables keep their columns. "
            "A page whose content is rendered by JavaScript has no text to "
            "read and is refused with that reason, rather than indexing as an "
            "empty success — save it from the browser after it loads, or "
            "upload it as PDF."
        ),
        drops=(
            "script and style contents",
            "navigation, headers, footers and sidebars",
        ),
    ),
    "ImageParser": Format(
        family="Images",
        extensions=(".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"),
        unit="the picture, described by a vision model",
        notes=(
            "An image has no text to extract, so it is read as a picture and "
            "its description is what becomes searchable. It is titled by its "
            "filename, never by the model's description of it."
        ),
    ),
}

# Formats that are NOT supported, stated because a caller needs to know what to
# convert before uploading. Silence about a format reads as support for it.
NOT_SUPPORTED: tuple[tuple[str, str], ...] = (
    (".doc, .xls, .ppt", "the pre-2007 binary Office formats — convert to their x variants"),
    (".rtf", "convert to DOCX or PDF"),
    (".epub", "convert to PDF"),
    (".eml, .msg", "email; extract the body and attachments and upload those"),
    (".zip", "archives are not unpacked; upload the files inside"),
    ("audio and video", "no transcription pipeline"),
)


def described() -> list[Format]:
    """Every registered format, in registry order."""
    return [
        _DESCRIBED[type(parser).__name__]
        for parser in _REGISTRY
        if type(parser).__name__ in _DESCRIBED
    ]


def undescribed() -> list[str]:
    """Parsers in the registry that this file has never heard of.

    The anti-drift check. A format that is accepted but undocumented is worse
    than one that is refused: a caller sends files it will never get answers
    from, and nothing says so.
    """
    return [
        type(parser).__name__
        for parser in _REGISTRY
        if type(parser).__name__ not in _DESCRIBED
    ]


def contract() -> dict[str, Any]:
    """The whole format contract, as served by GET /api/formats."""
    from packages.core.normalise import supported

    return {
        "version": VERSION,
        # Kept as a flat list of extensions for callers written against the
        # older shape of this endpoint, which returned only this.
        "supported": list(supported()),
        "formats": [f.as_dict() for f in described()],
        "not_supported": [
            {"what": what, "guidance": guidance} for what, guidance in NOT_SUPPORTED
        ],
        "on_failure": (
            "A file that cannot be parsed becomes an item at status 'failed' "
            "with the reason in metadata.failure, never a silent empty ingest. "
            "Re-uploading it is the retry. A failed re-upload never replaces a "
            "version that indexed cleanly."
        ),
    }


__all__ = ["NOT_SUPPORTED", "VERSION", "Format", "contract", "described", "undescribed"]
