from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Protocol

# ---------------------------------------------------------------------------
# NORMALISE — anything in, Markdown out.
#
# Markdown is the canonical stored representation for everything the KB holds
# (KB-7), so this is the only place that knows about file formats. Everything
# downstream — hashing, indexing, embedding, retrieval — sees Markdown and
# never learns whether it came from a PDF, a spreadsheet or a JSON payload.
#
# The original binary is NEVER retained. We keep the extracted Markdown plus
# a link back to the source, which is what "no file copies" means in practice.
#
# Parsers register themselves against extensions. Adding DOCX/XLSX/PPTX is a
# new Parser and one line in the registry — the write path does not change.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Normalised:
    """The output of normalising one input."""

    title: str
    body: str  # Markdown
    metadata: dict[str, Any] = field(default_factory=dict)


class Parser(Protocol):
    """Turns raw bytes into Markdown. One per family of formats."""

    extensions: tuple[str, ...]

    def parse(self, data: bytes, filename: str) -> Normalised: ...


class UnsupportedFormat(Exception):
    """Raised when nothing can read this input.

    Deliberately an exception rather than a silent empty item: a format we
    cannot read must surface as a visible, re-runnable failure (KB-3), not as
    an item that exists but says nothing.
    """


# ------------------------------- helpers -------------------------------


def tidy(text: str) -> str:
    """Collapse the whitespace extraction leaves behind.

    Extractors emit ragged spacing — page breaks become runs of blank lines,
    justified text becomes double spaces. That padding is dead weight in the
    embedding and reads as a rendering fault on screen, so it is removed once,
    here, rather than in each parser.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def title_from(body: str, filename: str) -> str:
    """First real line of content, else the filename.

    Structural markup is skipped rather than treated as content: page markers
    and horizontal rules are things WE inserted, and titling an item with its
    own scaffolding ("<!-- page 1 -->") makes every document in the index look
    identical to a person scanning it.
    """
    for line in body.splitlines():
        cleaned = line.strip()
        if cleaned.startswith("<!--") or set(cleaned) <= {"-", "=", "*", " "}:
            continue
        cleaned = cleaned.lstrip("# ").strip()
        if len(cleaned) > 2:
            return cleaned[:200]
    return filename.rsplit("/", 1)[-1][:200] or "untitled"


# ------------------------------- parsers -------------------------------


class TextParser:
    """Markdown and plain text — already canonical, only tidied."""

    extensions = (".md", ".markdown", ".txt", ".text")

    def parse(self, data: bytes, filename: str) -> Normalised:
        body = tidy(data.decode("utf-8", errors="replace"))
        return Normalised(title=title_from(body, filename), body=body)


class PdfParser:
    """PDF text extraction, page by page.

    Page boundaries are kept as Markdown rules: retrieval cites a passage, and
    a page number is the most useful anchor a PDF can offer for that.
    """

    extensions = (".pdf",)

    def parse(self, data: bytes, filename: str) -> Normalised:
        try:
            from pypdf import PdfReader
        except ImportError as exc:  # pragma: no cover - environment guard
            raise UnsupportedFormat("pypdf is not installed; cannot read PDF") from exc

        import io

        reader = PdfReader(io.BytesIO(data))
        pages: list[str] = []
        for number, page in enumerate(reader.pages, start=1):
            text = tidy(page.extract_text() or "")
            if text:
                pages.append(f"<!-- page {number} -->\n{text}")

        if not pages:
            # A scanned PDF with no text layer. Honest failure beats an empty
            # item that looks ingested but can never be retrieved.
            raise UnsupportedFormat(f"no extractable text in {filename} (scanned image?)")

        body = "\n\n---\n\n".join(pages)
        info = reader.metadata or {}
        declared = str(info.get("/Title") or "").strip()
        return Normalised(
            title=declared[:200] or title_from(body, filename),
            body=body,
            metadata={"pages": len(reader.pages), "format": "pdf"},
        )


_REGISTRY: list[Parser] = [TextParser(), PdfParser()]


def register(parser: Parser) -> None:
    """Add a format. The only step needed to support DOCX, XLSX or PPTX."""
    _REGISTRY.append(parser)


def supported() -> tuple[str, ...]:
    return tuple(sorted({ext for p in _REGISTRY for ext in p.extensions}))


def _parser_for(filename: str) -> Parser:
    lowered = filename.lower()
    for parser in _REGISTRY:
        if lowered.endswith(parser.extensions):
            return parser
    raise UnsupportedFormat(
        f"no parser for {filename!r}; supported: {', '.join(supported())}"
    )


def normalise(data: bytes, filename: str) -> Normalised:
    """Bytes -> Markdown. The single entry point for file content."""
    return _parser_for(filename).parse(data, filename)


def normalise_text(body: str, title: str | None = None, source_name: str = "text") -> Normalised:
    """For content that arrives as text rather than a file.

    A generated report, a distilled chat session, a JSON payload someone has
    already rendered — these enter through the same door as files so that the
    write path stays singular.
    """
    cleaned = tidy(body)
    return Normalised(title=(title or title_from(cleaned, source_name))[:200], body=cleaned)


__all__ = [
    "Normalised",
    "Parser",
    "UnsupportedFormat",
    "normalise",
    "normalise_text",
    "register",
    "supported",
    "tidy",
    "title_from",
]
