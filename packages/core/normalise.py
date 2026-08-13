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


class ScannedDocument(UnsupportedFormat):
    """A PDF whose pages are pictures — no text layer to extract.

    Its own type rather than a message the caller has to match on, because
    something CAN be done about this one: the pages can be read as images. That
    recovery needs an object store and a vision model, neither of which belongs
    in a parser, so this is raised here and handled by the pipeline.

    Still an UnsupportedFormat, so any caller that only knows the old behaviour
    keeps treating it as an honest refusal.
    """

    def __init__(self, message: str, *, page_count: int = 0) -> None:
        super().__init__(message)
        self.page_count = page_count


class PictureDocument(ScannedDocument):
    """An image file. There is no text layer because there is no text.

    A subclass of ScannedDocument on purpose: a photograph of a whiteboard and
    a scan of a page are the same problem — content that exists only as pixels
    — and the pipeline already knows how to read one of those. Sharing the type
    means sharing the recovery, rather than writing it twice and having the two
    drift apart.
    """


# ------------------------------- helpers -------------------------------


def tidy(text: str) -> str:
    """Collapse the whitespace extraction leaves behind.

    Extractors emit ragged spacing — page breaks become runs of blank lines,
    justified text becomes double spaces. That padding is dead weight in the
    embedding and reads as a rendering fault on screen, so it is removed once,
    here, rather than in each parser.

    They also emit stray control bytes — PDFs especially leave NUL (0x00) in
    extracted text. Postgres rejects NUL in a text column outright, so a single
    one anywhere in a document fails the whole write (the item never lands, and
    the upload times out looking for a document that was refused). They carry no
    meaning, so they are stripped here too — keeping only tab and newline.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# Structure a document states in words instead of in Markdown. A .txt book, a
# statute, a tender, a circular whose PDF text layer lost its formatting — all
# carry their shape in lines like "CHAPTER I", "SECTION 4", "ANNEXURE B", and
# none of it survives as a heading.
# "TITLE" is deliberately absent: a line starting "Title:" is metadata far
# more often than it is structure, and Project Gutenberg puts one at the top
# of every book.
_PART_WORDS = r"BOOK|PART|VOLUME"
_CHAPTER_WORDS = r"CHAPTER|SECTION|ARTICLE|CLAUSE|ANNEX|ANNEXURE|APPENDIX|SCHEDULE"
# Anchored to a whole line. These words appear constantly mid-sentence ("under
# section 5 of the Act") and matching those would shred a document into
# hundreds of false headings; a real heading sits alone on its line. The
# trailing run is capped so a sentence that merely BEGINS with the word cannot
# qualify, and a line already marked up is left alone.
_SPOKEN_HEADING = re.compile(
    rf"^((?:{_PART_WORDS})|(?:{_CHAPTER_WORDS}))\b([^\n]{{0,60}})$",
    re.MULTILINE | re.IGNORECASE,
)
# Below this many hits the pattern is likelier noise than structure.
MIN_SPOKEN_HEADINGS = 3


def _names_a_part(rest: str) -> bool:
    """Does what follows the structural word actually identify a part?

    Two ways it does not, both found on real text once this pass began seeing
    whole documents instead of single pages:

      "part. The responder can also make alternative proposals"
      "chapter. So be it."

    A sentence that happens to begin with the word, at the start of a line. The
    full stop is the tell — a heading is "CHAPTER I" or "PART TWO: 1807", never
    "chapter." — and a heading does not run to a dozen words either.
    """
    if not re.search(r"[0-9A-Za-z]", rest):
        return False
    if re.match(r"\s*[.!?]", rest):
        return False
    return len(rest.split()) <= 8


def mark_spoken_headings(text: str) -> str:
    """Promote text-stated headings to Markdown ones, once, at the door.

    Found on a real book: a Project Gutenberg *War and Peace* has 730 lines
    beginning CHAPTER and 30 beginning BOOK, and **zero** Markdown headings.
    Everything downstream reads Markdown, so the whole 3.2 MB document was one
    unstructured blob — the chunker gave all 4,296 passages an empty heading,
    section summaries collapsed from ~700 into 1, and the agent's table of
    contents read "Part 1 of 1475". Catalogue reasoning had nothing to reason
    over and answered that the store did not cover the question.

    Done HERE rather than in the tree, because the tree is not the only reader:
    the chunker parses headings independently, and a fix that only taught the
    outline would leave every stored passage still headingless — which is
    exactly what happened on the first attempt.

    Conservative by construction. It refuses unless the pattern occurs at least
    MIN_SPOKEN_HEADINGS times, and never touches a document that already has
    Markdown headings — one that does has real structure, and guessing more on
    top of it can only damage what its author wrote.
    """
    if re.search(r"(?m)^#{1,6} ", text):
        return text

    # A heading names WHICH part: "BOOK ONE", "SECTION 4", "ANNEXURE B". A bare
    # word is a sentence that happened to end a line — a real book has lines
    # reading just "book." and promoting those produced headings like
    # "book. > CHAPTER X" in the stored index.
    matches = [m for m in _SPOKEN_HEADING.finditer(text) if _names_a_part(m.group(2))]
    if len(matches) < MIN_SPOKEN_HEADINGS:
        return text

    parts = set(_PART_WORDS.split("|"))

    def promote(match: re.Match[str]) -> str:
        # Part-level words nest above chapter-level ones, so a book's chapters
        # end up INSIDE the book — which is what makes "BOOK TWO > CHAPTER I"
        # an address rather than one of thirty-four identical labels.
        level = "#" if match.group(1).upper() in parts else "##"
        return f"{level} {match.group(0).strip()}"

    # Rebuilt from the accepted matches rather than re-run as a global sub,
    # so the ones filtered out above stay filtered out.
    out: list[str] = []
    last = 0
    for match in matches:
        out.append(text[last : match.start()])
        out.append(promote(match))
        last = match.end()
    out.append(text[last:])
    return "".join(out)


# The other way a document states its structure: numbering. "2.2.2 Twisted
# Pairs". Technical books, standards, specifications, tenders and reports all
# use it, and none of them says the word CHAPTER anywhere.
#
# At least one dot is required, so a bare page number at the top of a page
# ("36") can never qualify, and the title must begin with a capital.
_NUMBERED_HEADING = re.compile(
    r"^(\d{1,2}(?:\.\d{1,3}){1,3})[ \t]+([A-Z][^\n]{1,70})$",
    re.MULTILINE,
)
# A table of contents is made of the same lines with a page number on the end.
# Promoting those splits the contents page into three hundred empty sections —
# the document's own index, shredded into sections that contain nothing.
_CONTENTS_LINE = re.compile(r"[,.\s]\s*\d{1,4}\s*$")
# Numbering is only structure when it is used throughout. Below this it is more
# likely a list, a version history, or a couple of cross-references.
MIN_NUMBERED_HEADINGS = 8


def mark_numbered_headings(text: str) -> str:
    """Promote "2.2.2 Twisted Pairs" to a Markdown heading.

    Found on a real book: Tanenbaum's *Computer Networks*, 962 pages, states
    its structure entirely in section numbers. It says CHAPTER only inside a
    running page header ("36 INTRODUCTION CHAP. 1"), so the spoken-heading pass
    found nothing, and all 4,035 stored passages came out with an EMPTY
    heading — one distinct heading across a 2.4 MB book. The agent's table of
    contents was 4,035 unnamed sections, which is not a table of contents; it
    is a list of the same word four thousand times.

    Nesting follows the numbering, so 1.3.1 sits inside 1.3 and a citation
    reads as an address rather than as one of hundreds of identical labels.
    """
    if re.search(r"(?m)^#{1,6} ", text):
        return text

    matches = [m for m in _NUMBERED_HEADING.finditer(text) if not _CONTENTS_LINE.search(m.group(2))]
    if len(matches) < MIN_NUMBERED_HEADINGS:
        return text

    out: list[str] = []
    last = 0
    for match in matches:
        depth = match.group(1).count(".")
        out.append(text[last : match.start()])
        out.append(f"{'#' * min(depth + 1, 6)} {match.group(0).strip()}")
        last = match.end()
    out.append(text[last:])
    return "".join(out)


def find_structure(text: str) -> str:
    """Recover the headings a document states rather than marks up.

    Applied to the WHOLE document, once, after every page has been read. That
    matters more than it looks: both passes below need to see the pattern
    repeat before they trust it, and a PDF read page by page never shows them
    more than one heading at a time. The promoter existed and was wired into
    tidy(), which PdfParser calls per page — so on a 962-page book with 327
    numbered headings it fired exactly zero times.
    """
    return mark_numbered_headings(mark_spoken_headings(text))


def despace(text: str) -> str:
    """Rejoin text a PDF extractor split into individual characters.

    Some PDFs position every glyph separately, and the extractor renders that
    as "L i c e n s i n g  N o t i c e" — letters one space apart, words two.
    Left alone the stored document is unreadable, keyword search matches single
    letters, and the embedding describes an alphabet rather than a subject. A
    real 103 kB guide came through with EVERY line in that state.

    This must run BEFORE tidy(), which collapses runs of spaces and destroys
    the double space that marks where one word ends — the only thing that makes
    the original recoverable.

    Applied per line and only where the evidence is overwhelming, so ordinary
    prose containing "a" and "I" is never touched.
    """
    out: list[str] = []
    for line in text.splitlines():
        tokens = [t for t in line.split(" ") if t]
        singles = sum(1 for t in tokens if len(t) == 1)
        if len(tokens) >= 6 and singles >= len(tokens) * 0.7:
            words = [w.replace(" ", "") for w in re.split(r" {2,}", line.strip())]
            out.append(" ".join(w for w in words if w))
        else:
            out.append(line)
    return "\n".join(out)


# Front matter that is printed ON a page but is not what the document is
# CALLED. A 962-page networking textbook came into the store titled "This page
# intentionally left blank", because that is the first line of text in the
# file: the cover is a picture with no text layer, so page 2 was the first page
# with any words on it at all.
_FRONT_MATTER = re.compile(
    r"^(?:this page (?:is )?(?:intentionally|deliberately) left blank"
    r"|intentionally left blank"
    r"|blank page"
    r"|page \d+(?: of \d+)?"
    r"|copyright(?: ?[©(c)]+.*)?"
    r"|all rights reserved"
    r"|table of contents|contents|index|preface|foreword"
    r"|draft|confidential|for internal use only)\.?$",
    re.IGNORECASE,
)


def title_from(body: str, filename: str) -> str:
    """First real line of content, else the filename.

    Structural markup is skipped rather than treated as content: page markers
    and horizontal rules are things WE inserted, and titling an item with its
    own scaffolding ("<!-- page 1 -->") makes every document in the index look
    identical to a person scanning it.

    Printed boilerplate is skipped for the same reason, and it is not a rare
    case: front matter is exactly where a scan's first WORDS live, because the
    cover is a picture. "This page intentionally left blank" is a sentence
    about paper, not a title, and a store whose books are named after their
    blank pages cannot be browsed at all.
    """
    for line in body.splitlines():
        cleaned = line.strip()
        if cleaned.startswith("<!--") or set(cleaned) <= {"-", "=", "*", " "}:
            continue
        cleaned = cleaned.lstrip("# ").strip()
        if len(cleaned) > 2 and not _FRONT_MATTER.match(cleaned):
            return cleaned[:200]
    return filename.rsplit("/", 1)[-1][:200] or "untitled"


# ------------------------------- parsers -------------------------------


class TextParser:
    """Markdown and plain text — already canonical, only tidied."""

    extensions: tuple[str, ...] = (".md", ".markdown", ".txt", ".text")

    def parse(self, data: bytes, filename: str) -> Normalised:
        body = find_structure(tidy(data.decode("utf-8", errors="replace")))
        return Normalised(title=title_from(body, filename), body=body)


class PdfParser:
    """PDF text extraction, page by page.

    Page boundaries are kept as Markdown rules: retrieval cites a passage, and
    a page number is the most useful anchor a PDF can offer for that.
    """

    extensions: tuple[str, ...] = (".pdf",)

    def parse(self, data: bytes, filename: str) -> Normalised:
        try:
            from pypdf import PdfReader
        except ImportError as exc:  # pragma: no cover - environment guard
            raise UnsupportedFormat("pypdf is not installed; cannot read PDF") from exc

        import io

        reader = PdfReader(io.BytesIO(data))
        pages: list[str] = []
        for number, page in enumerate(reader.pages, start=1):
            # despace BEFORE tidy: tidy collapses the double space that
            # marks a word boundary in character-positioned text.
            text = tidy(despace(page.extract_text() or ""))
            if text:
                pages.append(f"<!-- page {number} -->\n{text}")

        if not pages:
            # A scanned PDF: pages of pictures with no text layer. Not a dead
            # end any more — the pipeline can read the pages with vision — but
            # still a refusal here, because a parser that returned an empty
            # body would produce an item that looks ingested and can never be
            # retrieved.
            raise ScannedDocument(
                f"no extractable text in {filename} (scanned image?)",
                page_count=len(reader.pages),
            )

        # Structure is recovered from the WHOLE document, never per page. Both
        # promoters need to see a pattern repeat before they trust it, and one
        # page never shows them more than one heading.
        body = find_structure("\n\n---\n\n".join(pages))
        info: dict[str, Any] = dict(reader.metadata or {})
        declared = str(info.get("/Title") or "").strip()
        return Normalised(
            title=declared[:200] or title_from(body, filename),
            body=body,
            metadata={"pages": len(reader.pages), "format": "pdf"},
        )


class DocxParser:
    """Word documents, in document order.

    A .docx is a zip of XML, so this needs no dependency: the standard library
    can open both. Only the parts that survive conversion to Markdown are read
    — headings, paragraphs, list items and tables. Fonts, colours, comments and
    tracked changes are dropped, because none of them is content a query could
    ever match on.

    Order matters more than it looks: paragraphs and tables interleave in the
    body, so the children of <w:body> are walked in sequence rather than
    collecting all paragraphs and then all tables, which would silently
    rearrange a document whose tables carry the substance.
    """

    extensions: tuple[str, ...] = (".docx",)

    _NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    _CORE = "{http://purl.org/dc/elements/1.1/}"

    def _text_of(self, node: Any) -> str:
        """The visible text of a paragraph or cell.

        Tabs and line breaks are elements, not characters, so they are turned
        back into whitespace — otherwise two columns of a tabbed layout fuse
        into one unreadable word.
        """
        out: list[str] = []
        for el in node.iter():
            tag = el.tag
            if tag == f"{self._NS}t":
                out.append(el.text or "")
            elif tag in (f"{self._NS}tab", f"{self._NS}br"):
                out.append(" ")
        return "".join(out).strip()

    def _paragraph(self, node: Any) -> str:
        text = self._text_of(node)
        if not text:
            return ""

        props = node.find(f"{self._NS}pPr")
        style = ""
        if props is not None:
            named = props.find(f"{self._NS}pStyle")
            if named is not None:
                style = (named.get(f"{self._NS}val") or "").lower()
            if props.find(f"{self._NS}numPr") is not None:
                return f"- {text}"

        if style.startswith("heading"):
            # "Heading2" -> "##". Depth is clamped so a document using
            # Heading9 does not emit markup no renderer honours.
            digits = "".join(c for c in style if c.isdigit())
            level = min(int(digits), 6) if digits else 1
            return f"{'#' * level} {text}"
        if style in ("title", "subtitle"):
            return f"# {text}"
        return text

    def _table(self, node: Any) -> str:
        """A table as pipe rows, with a header separator after the first row.

        Retrieval reads the flattened text, but a person reading the stored
        Markdown should still be able to tell which value sat in which column.
        """
        rows: list[str] = []
        for tr in node.findall(f"{self._NS}tr"):
            cells = [
                self._text_of(tc).replace("|", "\\|").replace("\n", " ")
                for tc in tr.findall(f"{self._NS}tc")
            ]
            if not any(cells):
                continue
            rows.append("| " + " | ".join(cells) + " |")
            if len(rows) == 1:
                rows.append("| " + " | ".join("---" for _ in cells) + " |")
        return "\n".join(rows)

    def parse(self, data: bytes, filename: str) -> Normalised:
        import io
        import zipfile
        from xml.etree import ElementTree

        try:
            archive = zipfile.ZipFile(io.BytesIO(data))
            document = archive.read("word/document.xml")
        except (zipfile.BadZipFile, KeyError) as exc:
            # A .doc renamed to .docx, or a corrupt file. Saying so is more
            # use than a stack trace about a missing zip entry.
            raise UnsupportedFormat(
                f"{filename} is not a readable Word document "
                "(the legacy .doc format is not supported — re-save it as .docx)"
            ) from exc

        root = ElementTree.fromstring(document)
        body = root.find(f"{self._NS}body")
        blocks: list[str] = []
        for child in list(body) if body is not None else []:
            if child.tag == f"{self._NS}p":
                blocks.append(self._paragraph(child))
            elif child.tag == f"{self._NS}tbl":
                blocks.append(self._table(child))

        text = tidy("\n\n".join(b for b in blocks if b))
        if not text:
            raise UnsupportedFormat(f"no extractable text in {filename}")

        declared = ""
        try:
            core = ElementTree.fromstring(archive.read("docProps/core.xml"))
            found = core.find(f"{self._CORE}title")
            declared = (found.text or "").strip() if found is not None else ""
        except (KeyError, ElementTree.ParseError):
            declared = ""

        return Normalised(
            title=declared[:200] or title_from(text, filename),
            body=text,
            metadata={"format": "docx"},
        )


class ImageParser:
    """Pictures: PNG, JPEG and friends.

    Parses nothing and says so immediately. An image has no text to extract, so
    the honest thing is to refuse here and let the pipeline read it with vision
    — the same route a scanned PDF takes. Pretending to parse it, or accepting
    it and indexing an empty body, would produce a document that looks stored
    and can never be found.
    """

    extensions: tuple[str, ...] = (
        ".png",
        ".jpg",
        ".jpeg",
        ".webp",
        ".gif",
        ".bmp",
        ".tif",
        ".tiff",
    )

    def parse(self, data: bytes, filename: str) -> Normalised:
        raise PictureDocument(f"{filename} is an image; its content is in the pixels", page_count=1)


from packages.core.office import CsvParser, PptxParser, XlsxParser  # noqa: E402

_REGISTRY: list[Parser] = [
    TextParser(),
    PdfParser(),
    DocxParser(),
    PptxParser(),
    XlsxParser(),
    CsvParser(),
    ImageParser(),
]


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


def can_parse(filename: str) -> bool:
    """Is there a parser for this name?

    Exists so the API can refuse an unreadable file while the caller is still
    listening, instead of accepting it and failing in a worker whose reason
    nobody ever sees.
    """
    return filename.lower().endswith(supported())


def normalise(data: bytes, filename: str) -> Normalised:
    """Bytes -> Markdown. The single entry point for file content."""
    return _parser_for(filename).parse(data, filename)


def normalise_text(body: str, title: str | None = None, source_name: str = "text") -> Normalised:
    """For content that arrives as text rather than a file.

    A generated report, a distilled chat session, a JSON payload someone has
    already rendered — these enter through the same door as files so that the
    write path stays singular.
    """
    cleaned = find_structure(tidy(body))
    return Normalised(title=(title or title_from(cleaned, source_name))[:200], body=cleaned)


__all__ = [
    "DocxParser",
    "Normalised",
    "Parser",
    "ImageParser",
    "PdfParser",
    "PictureDocument",
    "ScannedDocument",
    "TextParser",
    "UnsupportedFormat",
    "can_parse",
    "mark_spoken_headings",
    "despace",
    "normalise",
    "normalise_text",
    "register",
    "supported",
    "tidy",
    "title_from",
]
