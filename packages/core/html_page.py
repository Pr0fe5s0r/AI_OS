from __future__ import annotations

import re
from html import unescape
from html.parser import HTMLParser as _StdlibHTMLParser

from packages.core.normalise import Normalised, UnsupportedFormat, tidy

# ---------------------------------------------------------------------------
# HTML -> Markdown.
#
# Built on the standard library's own parser rather than on BeautifulSoup or
# lxml: it is the same decision that put pypdfium2 in the PDF path instead of
# PyMuPDF — a format this common should not add a dependency, and html.parser
# is lenient about the broken markup real pages are full of. It never raises on
# a malformed tag, which matters because a client's saved page is far more
# often malformed than not.
#
# What this deliberately does NOT do is read the page the way a browser would.
# There is no JavaScript execution and no CSS, so a single-page app that ships
# an empty <div id="root"> yields nothing — and yields it VISIBLY, as a refusal
# rather than as an item that exists and says nothing.
#
# Three things it is careful about, each because the naive version is wrong:
#
#   SCRIPT AND STYLE are dropped, contents and all. Otherwise a page's minified
#   JavaScript becomes "content", gets chunked, gets embedded, and answers
#   questions. A retrieval store that cites a webpack bundle is worse than one
#   that skipped the page.
#
#   TABLES keep their columns, as Markdown pipe rows. The client cites specific
#   rows back to their own clients (R2.2), and a table flattened into a run-on
#   sentence cannot be cited at all.
#
#   BLOCK BOUNDARIES become blank lines. HTML has no line breaks of its own —
#   two paragraphs are two elements — so a parser that only accumulates text
#   fuses the last word of one heading into the first word of the next
#   sentence, and every heading in the document disappears into prose.
# ---------------------------------------------------------------------------

# Contents dropped entirely: not content, and actively harmful if indexed.
_SILENT = frozenset({"script", "style", "noscript", "template", "svg", "canvas"})

# Chrome that repeats on every page of a site. Kept OUT of the body so a search
# does not match a hundred pages on their shared navigation menu.
_FURNITURE = frozenset({"nav", "header", "footer", "aside"})

_HEADINGS = {"h1": "#", "h2": "##", "h3": "###", "h4": "####", "h5": "#####", "h6": "######"}
_BLOCKS = frozenset(
    {
        "p", "div", "section", "article", "main", "ul", "ol", "dl", "dd", "dt",
        "blockquote", "pre", "figure", "figcaption", "hr", "br", "address",
        *_HEADINGS,
    }
)

# A page whose visible text is shorter than this is treated as empty. A cookie
# banner and a "loading…" are not a document, and indexing them produces an
# item that exists, answers nothing, and looks like a success.
MIN_TEXT_CHARS = 40


class _Reader(_StdlibHTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title: str = ""
        self._in_title = False
        self._silent = 0
        self._furniture = 0
        # Table state. Cells are collected per row so the row can be emitted as
        # one Markdown line — a cell emitted as it is parsed would interleave
        # with anything nested inside it.
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._rows: list[list[str]] | None = None
        self._list_depth = 0

    # ------------------------------- tags -------------------------------

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SILENT:
            self._silent += 1
            return
        if self._silent:
            return
        if tag in _FURNITURE:
            self._furniture += 1
            return

        if tag == "title":
            self._in_title = True
        elif tag == "table":
            self._rows = []
        elif tag == "tr" and self._rows is not None:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag in ("ul", "ol"):
            self._list_depth += 1
            self.parts.append("\n")
        elif tag in _HEADINGS:
            self.parts.append(f"\n\n{_HEADINGS[tag]} ")
        elif tag in _BLOCKS:
            self.parts.append("\n\n")
        elif tag == "img":
            # Alt text is the only part of an image a query can match, and on a
            # diagram-heavy page it is often the only description of the
            # picture that exists.
            alt = dict(attrs).get("alt")
            if alt and alt.strip():
                self.parts.append(f"\n\n![{alt.strip()}]\n\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SILENT:
            self._silent = max(0, self._silent - 1)
            return
        if self._silent:
            return
        if tag in _FURNITURE:
            self._furniture = max(0, self._furniture - 1)
            return

        if tag == "title":
            self._in_title = False
        elif tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None and self._rows is not None:
            if any(c for c in self._row):
                self._rows.append(self._row)
            self._row = None
        elif tag == "table":
            self._flush_table()
        elif tag in ("ul", "ol"):
            self._list_depth = max(0, self._list_depth - 1)
            self.parts.append("\n")
        elif tag in _BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._silent or self._furniture:
            return
        if self._in_title:
            self.title += data
            return
        if self._cell is not None:
            self._cell.append(data)
            return
        if self._row is not None or self._rows is not None:
            # Loose text between cells is layout, not content.
            return
        self.parts.append(data)

    # ------------------------------ tables ------------------------------

    def _flush_table(self) -> None:
        rows, self._rows, self._row, self._cell = self._rows, None, None, None
        if not rows:
            return
        width = max(len(r) for r in rows)
        padded = [r + [""] * (width - len(r)) for r in rows]
        lines = ["", ""]
        lines.append("| " + " | ".join(padded[0]) + " |")
        lines.append("| " + " | ".join("---" for _ in range(width)) + " |")
        for row in padded[1:]:
            lines.append("| " + " | ".join(row) + " |")
        lines.append("")
        self.parts.append("\n".join(lines))


_BLANK_RUN = re.compile(r"\n{3,}")
_TRAILING_SPACE = re.compile(r"[ \t]+\n")


class HtmlParser:
    """Web pages, as the text a reader would see.

    Registered for .html, .htm and .xhtml. A page saved from a browser, an
    exported report, a scraped article — the formats a client actually hands
    over — all arrive here.
    """

    extensions: tuple[str, ...] = (".html", ".htm", ".xhtml")

    def parse(self, data: bytes, filename: str) -> Normalised:
        text = _decode(data)
        reader = _Reader()
        # feed() can raise on genuinely broken markup even in lenient mode; a
        # partial read is still worth keeping, because half a page indexed is
        # far better than a page refused for one malformed tag near the end.
        try:
            reader.feed(text)
            reader.close()
        except Exception:  # noqa: BLE001 - keep whatever parsed before the break
            pass

        body = _BLANK_RUN.sub("\n\n", _TRAILING_SPACE.sub("\n", "".join(reader.parts)))
        body = tidy(unescape(body).strip())

        if len(body.replace("#", "").replace("-", "").strip()) < MIN_TEXT_CHARS:
            # Visible failure rather than a silent empty item (R2.3). The most
            # common cause by far is a page whose content is rendered by
            # JavaScript, so the reason says so instead of leaving the operator
            # to guess why a 200 KB file indexed as nothing.
            raise UnsupportedFormat(
                f"{filename} has no readable text — if the page renders its "
                "content with JavaScript, save it from the browser after it "
                "has loaded, or upload it as PDF."
            )

        title = " ".join(unescape(reader.title).split())
        if not title:
            first = next((ln for ln in body.splitlines() if ln.startswith("# ")), "")
            title = first[2:].strip()
        return Normalised(title=(title or filename)[:200], body=body)


def _decode(data: bytes) -> str:
    """Bytes to text, honouring a declared charset before guessing.

    Real pages are not all UTF-8, and a mojibake body is worse than a refused
    one: it indexes, it embeds, and every quotation mark in every citation is
    wrong.
    """
    head = data[:2048].lower()
    match = re.search(rb'charset=["\']?([a-z0-9_\-]+)', head)
    if match:
        try:
            return data.decode(match.group(1).decode("ascii"), errors="replace")
        except LookupError:
            pass
    for encoding in ("utf-8", "cp1252", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


__all__ = ["MIN_TEXT_CHARS", "HtmlParser"]
