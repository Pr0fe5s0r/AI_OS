from __future__ import annotations

import hashlib
import json
import os
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from packages.core.chunk import _HEADING

# ---------------------------------------------------------------------------
# THE TREE INDEX — a document's own table of contents, as an index.
#
# The method is PageIndex's (VectifyAI, MIT): instead of cutting a document
# into pieces and embedding them, keep its structure and let a model reason
# over that structure to decide what to read. Similarity is not relevance, and
# for a long structured document — a specification, a contract, a report — the
# author has already done the work of saying what is where. A heading is a
# better signal about content than a cosine score, because a person wrote it on
# purpose.
#
# The algorithm here follows theirs; the code is ours because their package is
# not installable (no pyproject) and pulls litellm and three PDF libraries in
# order to run functions that use none of them — and litellm would be a second
# model client beside the one this codebase insists on having exactly one of.
#
# Building the tree makes NO model calls. It is regex and arithmetic, so it is
# free, instant, and identical every time — which matters, because an index
# that differed between runs would make retrieval unreproducible.
# ---------------------------------------------------------------------------

# Characters per token, near enough for budgeting which sections fit in a
# prompt. A real tokeniser would be a dependency for a number that only ever
# decides "does this fit", and it is used with generous headroom.
CHARS_PER_TOKEN = 4
# A section longer than this is split at its own paragraph boundaries, so no
# single node can blow the context window on its own.
MAX_NODE_CHARS = 6000
# How much of a section's opening goes into the outline. Enough to say what the
# section is about, far short of enough to answer from.
PREVIEW_CHARS = 160


@dataclass(slots=True)
class Node:
    """One section of a document, and the sections beneath it."""

    node_id: str
    title: str
    level: int
    start_line: int
    end_line: int
    text: str
    children: list[Node] = field(default_factory=list)
    # Set when this section is literally one page of a PDF. Known exactly at
    # that point — the marker being split on says which page it is — so it is
    # recorded rather than re-derived later from line numbers that a fallback
    # section does not have.
    page: int | None = None

    @property
    def tokens(self) -> int:
        return len(self.text) // CHARS_PER_TOKEN

    def preview(self, chars: int = PREVIEW_CHARS) -> str:
        """The opening of the section, minus its own heading line.

        A title alone is often not enough to tell what a section holds: a model
        picked "Required source types" for a question about required connectors
        on the shared word, when that section is about capturing chat sessions
        and the answer sat under "Integration with Drive Sync". The first line
        of prose disambiguates that, and costs nothing to produce.

        The reference implementation asks a model to summarise each node for
        this purpose. That is better, and it is also a model call per section
        on every document — this is the free approximation.
        """
        lines = [
            line.strip()
            for line in self.text.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        opening = " ".join(lines)[:chars].strip()
        return opening + "…" if len(opening) == chars else opening

    def captions(self) -> list[str]:
        """The figures and tables this section contains, by name.

        A caption is the ONE piece of text a PDF keeps about a picture, and
        until now it was invisible: the outline showed a title and the first
        160 characters, so page 13 of a paper read "Attention Visualizations
        Input-Input Layer5 It is in this spirit that..." and the words "Figure
        3" appeared nowhere in the index. Asked about Figure 3, the agent had
        no way to find the page holding it and correctly reported that nothing
        matched — over a document that had it.

        Line starts only. Captions sit on their own line in extracted text,
        and a sentence mid-paragraph mentioning a table is a reference to it,
        not the place it lives.
        """
        found: list[str] = []
        for match in _CAPTION.finditer(self.text):
            name = f"{match.group(1).rstrip('.').title()} {match.group(2)}"
            if name not in found:
                found.append(name)
        return found

    def outline(self, with_tokens: bool = True, with_preview: bool = True) -> dict[str, Any]:
        """The node WITHOUT its full text — what the model reasons over.

        Sending the whole text would defeat the point: the idea is to choose
        what to read before reading it.
        """
        entry: dict[str, Any] = {"id": self.node_id, "title": self.title}
        if with_tokens:
            entry["tokens"] = self.tokens
        if with_preview:
            opening = self.preview()
            if opening:
                entry["opens"] = opening
        # What this section SHOWS, as opposed to says. Named separately from
        # the preview because a figure is findable by its number and by
        # nothing else — no amount of prose in the opening line will lead
        # anyone to it.
        shows = self.captions()
        if shows:
            entry["shows"] = shows
        if self.children:
            entry["sections"] = [c.outline(with_tokens, with_preview) for c in self.children]
        return entry

    def walk(self) -> list[Node]:
        out = [self]
        for child in self.children:
            out.extend(child.walk())
        return out

    def as_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "title": self.title,
            "level": self.level,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "text": self.text,
            "children": [c.as_dict() for c in self.children],
        }

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> Node:
        return Node(
            node_id=raw["node_id"],
            title=raw["title"],
            level=raw["level"],
            start_line=raw["start_line"],
            end_line=raw["end_line"],
            text=raw["text"],
            children=[Node.from_dict(c) for c in raw.get("children", [])],
        )


def _headings(markdown: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Every heading, with the line it sits on. Code blocks are skipped.

    A '# ' inside a fenced block is a shell comment or a Python comment, not a
    section — treating it as one would invent structure that is not there.
    """
    lines = markdown.split("\n")
    found: list[dict[str, Any]] = []
    fenced = False
    for number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if stripped.startswith("```"):
            fenced = not fenced
            continue
        if fenced or not stripped:
            continue
        match = _HEADING.match(stripped)
        if match:
            found.append(
                {
                    "title": match.group(2).strip(),
                    "line": number,
                    "level": len(match.group(1)),
                }
            )
    return found, lines


def _split_oversized(text: str, limit: int) -> list[str]:
    """A section too long to send whole, cut at paragraph boundaries."""
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    current: list[str] = []
    size = 0
    for para in text.split("\n\n"):
        if size and size + len(para) > limit:
            parts.append("\n\n".join(current))
            current, size = [], 0
        current.append(para)
        size += len(para) + 2
    if current:
        parts.append("\n\n".join(current))
    return parts


# A page marker inserted by the PDF reader. The one piece of structure a PDF
# reliably has, and the unit its own page numbers already refer to — so a
# citation to "page 4" is checkable against the original.
_PAGE = re.compile(r"^<!--\s*page\s+(\d+)\s*-->\s*$", re.MULTILINE)
# Target size for a block when a document has neither headings nor pages.
FALLBACK_CHARS = 2500

# A figure or table caption, at the start of a line. The only text a PDF keeps
# about a picture, and therefore the only way to find one.
_CAPTION = re.compile(
    r"^\s*(Figure|Fig\.|Table|Chart|Exhibit|Appendix)\s+([0-9]+[A-Za-z]?)\b",
    re.MULTILINE | re.IGNORECASE,
)


# Structure a plain-text document carries in its own words rather than in
# Markdown. Two ranks: a part-level word that contains chapters, and a
# chapter-level word that contains prose.
#
# This is not a novel-reading feature. A statute has ARTICLEs, a tender has
# SECTIONs, a government circular has ANNEXUREs and SCHEDULEs — none of which
# survive as Markdown when the source is a .txt or a PDF whose text layer lost
# its formatting.
_PART_WORDS = r"BOOK|PART|VOLUME"
_CHAPTER_WORDS = r"CHAPTER|SECTION|ARTICLE|CLAUSE|ANNEX|ANNEXURE|APPENDIX|SCHEDULE"
# Anchored to a whole line, because the same words appear constantly mid
# sentence ("under section 5 of the Act") and matching those would shred a
# document into hundreds of false sections. A heading sits alone on its line,
# and the trailing text is capped so a sentence that merely BEGINS with the
# word cannot qualify.
_PLAIN_HEADING = re.compile(
    rf"^[ \t]*((?:{_PART_WORDS})|(?:{_CHAPTER_WORDS}))\b[ \t]*([^\n]{{0,60}}?)[ \t]*$",
    re.MULTILINE | re.IGNORECASE,
)
# Below this many hits the pattern is more likely noise than structure, and
# arbitrary blocks are the more honest answer.
MIN_PLAIN_HEADINGS = 3


def _plain_sections(body: str) -> list[dict[str, Any]]:
    """Sections from a document whose headings are text, not Markdown.

    Found on a real book: a Project Gutenberg War and Peace has 730 lines
    beginning CHAPTER and 30 beginning BOOK, and **zero** Markdown headings. The
    outline fell through to arbitrary blocks, so the agent's table of contents
    read "Part 1 of 1475", "Part 2 of 1475", … — 1,475 rows that say nothing
    about what is in them. Catalogue reasoning had nothing to reason over, and
    the honest result was that it could not find anything.

    Titles carry their parent, so they are addresses rather than labels. A novel
    has thirty-four chapters called "CHAPTER I" and a bare title cannot pick one
    out; "BOOK TWO: 1805 › CHAPTER I" can.
    """
    matches = list(_PLAIN_HEADING.finditer(body))
    if len(matches) < MIN_PLAIN_HEADINGS:
        return []

    part_words = set(_PART_WORDS.split("|"))
    sections: list[dict[str, Any]] = []
    parent = ""
    for index, match in enumerate(matches):
        word = match.group(1).upper()
        label = " ".join(match.group(0).split())
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        text = body[start:end].strip()

        if word in part_words:
            # A part heading names what follows; its own body is usually just
            # the chapter beneath it, so it is recorded as context rather than
            # as a section with content of its own.
            parent = label
            if len(text) < 200:
                continue
        sections.append(
            {
                "title": f"{parent} › {label}" if parent and word not in part_words else label,
                "text": text,
                "start_line": body.count("\n", 0, match.start()) + 1,
            }
        )
    return sections


def _fallback_sections(body: str) -> list[dict[str, Any]]:
    """Readable units for a document with no headings.

    Pages first: a PDF has them, they are what its own numbering refers to, and
    "page 4" is a citation someone can check against the original file. Then
    headings the document states in words rather than in Markdown. Only failing
    both, blocks of whole paragraphs named by position — which tells a reader
    where they are and nothing about what is there.
    """
    pages = list(_PAGE.finditer(body))
    if pages:
        sections: list[dict[str, Any]] = []
        for index, match in enumerate(pages):
            start = match.end()
            end = pages[index + 1].start() if index + 1 < len(pages) else len(body)
            text = body[start:end].strip().strip("-").strip()
            if text:
                sections.append(
                    {
                        "title": f"Page {match.group(1)}",
                        "text": text,
                        "page": int(match.group(1)),
                        "start_line": body.count("\n", 0, match.start()) + 1,
                    }
                )
        if sections:
            return sections

    stripped = body.strip()
    if not stripped:
        return []

    spoken = _plain_sections(stripped)
    if spoken:
        return spoken

    if len(stripped) <= FALLBACK_CHARS:
        return [{"title": "(whole document)", "text": stripped}]

    blocks = _split_oversized(stripped, FALLBACK_CHARS)
    total = len(blocks)
    return [
        {"title": f"Part {number} of {total}", "text": block}
        for number, block in enumerate(blocks, start=1)
        if block.strip()
    ]


# How much source text the parsed-tree cache may hold, counted in characters of
# the Markdown that produced the trees. Trees are a few times larger than their
# source, so this is a proxy rather than a true memory bound — but it is the
# number that scales with the thing being cached, and it fails by parsing again
# rather than by growing without limit.
#
# 32M characters is roughly five War-and-Peace-sized books, or hundreds of
# ordinary documents.
CACHE_BUDGET_CHARS = int(os.getenv("TREE_CACHE_CHARS", str(32_000_000)))

# item key -> (source hash, source length, tree). An OrderedDict used as an LRU.
# The source length is stored rather than re-derived, so what is subtracted on
# eviction is exactly what was added on insert — deriving it from the tree
# instead would drift and the budget would slowly stop meaning anything.
_cache: OrderedDict[str, tuple[str, int, Node]] = OrderedDict()
_cache_chars = 0


def cache_stats() -> dict[str, int]:
    """What the cache is holding — for tests and for anyone measuring."""
    return {"entries": len(_cache), "chars": _cache_chars, "budget": CACHE_BUDGET_CHARS}


def clear_cache() -> None:
    global _cache_chars
    _cache.clear()
    _cache_chars = 0


def build_cached(key: str, markdown: str, title: str = "") -> Node:
    """``build``, reusing the tree when the same document is parsed again.

    Parsing is pure, deterministic and free of model calls — and on a large
    document it is not free of TIME. Measured: three documents totalling 6.4MB
    took 8.2 seconds to parse, on EVERY question, because the navigator rebuilt
    every tree from the body it had just read out of Postgres. That was the
    single largest fixed cost before the first model call.

    Keyed on a hash of the source, not on the item id alone: an edited document
    keeps its id and must not keep its old tree. The id is still part of the key
    so one document's versions replace each other rather than accumulating.

    Safe to share because nothing mutates a Node. ``outline()`` builds fresh
    dictionaries on every call, and the catalogue's summary injection writes
    into those dictionaries, never into the tree.
    """
    global _cache_chars
    digest = hashlib.sha256(markdown.encode("utf-8", "ignore")).hexdigest()
    cached = _cache.get(key)
    if cached is not None and cached[0] == digest:
        _cache.move_to_end(key)
        return cached[2]

    root = build(markdown, title)

    if cached is not None:
        # Replacing this document's own older version.
        _cache_chars -= cached[1]
        del _cache[key]

    size = len(markdown)
    _cache[key] = (digest, size, root)
    _cache_chars += size
    # `len(_cache) > 1` so the document just parsed is never the one evicted —
    # a cache that throws away the entry it was asked for does the work twice
    # and keeps nothing, which is worse than having no cache at all.
    while _cache_chars > CACHE_BUDGET_CHARS and len(_cache) > 1:
        _, (_, evicted_size, _tree) = _cache.popitem(last=False)
        _cache_chars -= evicted_size
    return root


def build(markdown: str, title: str = "") -> Node:
    """A document as a tree of its own sections.

    Deterministic and free: no model is called, so the same document always
    produces the same index. Text above the first heading becomes a preamble
    section rather than being dropped — it is usually the summary.
    """
    body = markdown.strip()
    headings, lines = _headings(body)

    counter = [0]

    def next_id() -> str:
        node_id = f"n{counter[0]:03d}"
        counter[0] += 1
        return node_id

    root = Node(
        node_id=next_id(),
        title=title or "Document",
        level=0,
        start_line=1,
        end_line=len(lines),
        text="",
    )

    if not headings:
        # No Markdown headings. This is NOT the rare case it looks like: a PDF
        # converted to text has page markers, not headings, so every PDF landed
        # here — and a document with no sections is invisible to a retrieval
        # that works by choosing sections. Asked what a document titled "Value
        # Education" covered, the agent correctly reported that it had no
        # sections listed, and the store looked empty when it was not.
        #
        # So the fallback uses whatever structure the document really does
        # have. Pages for a PDF, blocks of paragraphs otherwise. Neither
        # invents headings the author did not write; both give the reader
        # something honestly nameable to open.
        for section in _fallback_sections(body):
            root.children.append(
                Node(
                    node_id=next_id(),
                    title=section["title"],
                    level=1,
                    start_line=int(section.get("start_line", 1)),
                    end_line=len(lines),
                    text=section["text"],
                    page=section.get("page"),
                )
            )
        if not root.children:
            root.text = body
        return root

    # Text before the first heading. Usually a title block or an abstract, and
    # dropping it loses the one place a document says what it is.
    preamble = "\n".join(lines[: headings[0]["line"] - 1]).strip()
    flat: list[Node] = []
    if preamble:
        flat.append(
            Node(
                node_id=next_id(),
                title="(opening)",
                level=1,
                start_line=1,
                end_line=headings[0]["line"] - 1,
                text=preamble,
            )
        )

    for index, heading in enumerate(headings):
        start = heading["line"]
        end = headings[index + 1]["line"] - 1 if index + 1 < len(headings) else len(lines)
        text = "\n".join(lines[start - 1 : end]).strip()
        flat.append(
            Node(
                node_id=next_id(),
                title=heading["title"],
                level=heading["level"],
                start_line=start,
                end_line=end,
                text=text,
            )
        )

    # Nest by heading level. A level that jumps (h1 straight to h3) attaches to
    # the nearest shallower ancestor rather than being discarded, because badly
    # nested headings are extremely common and the content is still real.
    stack: list[Node] = [root]
    for node in flat:
        while len(stack) > 1 and stack[-1].level >= node.level:
            stack.pop()
        stack[-1].children.append(node)
        stack.append(node)

    return root


def page_containing(markdown: str, snippet: str) -> int | None:
    """Which page a piece of text sits on, found by locating the text itself.

    For passages that were cut by the chunker rather than by the page index —
    everything hybrid retrieval returns. Those carry no page of their own, so
    the same passage showed its page when vectorless cited it and showed
    nothing when hybrid cited it: the same evidence, described two ways,
    depending on a retrieval choice the reader did not make.

    Matched on the opening of the snippet, which is enough to locate it and
    short enough to survive the trimming a passage goes through on the way to
    a citation. None when the text cannot be found or the document has no
    pages — a citation with no page shows text only, which is honest.
    """
    opening = " ".join(snippet.split())[:120]
    if not opening:
        return None

    # The body keeps its line breaks; the snippet may not. Compare on a
    # whitespace-flattened copy and map the hit back by counting markers in the
    # same flattened text, so both sides are measured the same way.
    flat = " ".join(markdown.split())
    at = flat.find(opening)
    if at < 0:
        return None

    page: int | None = None
    for match in _PAGE_FLAT.finditer(flat):
        if match.start() > at:
            break
        page = int(match.group(1))
    return page


# The page marker again, without the line anchor: used against text that has
# had its newlines flattened so a passage can be located inside it.
_PAGE_FLAT = re.compile(r"<!--\s*page\s+(\d+)\s*-->")


def page_count(markdown: str) -> int:
    """How many addressable parts this document has — pages, or slides.

    Read from the `<!-- page N -->` markers the parsers write, so it needs no
    schema change and cannot go stale. The HIGHEST marker rather than the
    number of them: a page whose content is empty writes no marker in some
    paths, and a count of markers would then report a deck of 79 slides as
    having 74 — a number that disagrees with the citation saying "slide 79".

    Zero for everything with no pages: pasted text, Markdown, a spreadsheet.
    """
    highest = 0
    for match in _PAGE.finditer(markdown or ""):
        highest = max(highest, int(match.group(1)))
    return highest


def page_of(markdown: str, node: Node) -> int | None:
    """Which page of the original a section begins on, or None.

    Both PDF paths — extracted text and a scan read with vision — write
    `<!-- page N -->` before each page's content, so the last marker at or
    before a section's first line is the page it starts on. That is what lets a
    citation show the page it came off.

    None for everything with no pages: pasted text, Markdown, a .docx. A
    citation with no page shows text only, which is honest — better than an
    empty picture frame implying something failed to load.
    """
    if node.page is not None:
        # A page-fallback section IS a page and said so when it was cut. The
        # line-scan below cannot recover that: those sections all begin at the
        # same nominal line, so scanning would report page 1 for all fifteen
        # pages of a paper — and a citation would show the wrong page's
        # picture, which is worse than showing none.
        return node.page

    page: int | None = None
    for match in _PAGE.finditer(markdown):
        line = markdown.count("\n", 0, match.start())
        if line > node.start_line:
            break
        page = int(match.group(1))
    return page


def find(root: Node, node_ids: list[str]) -> list[Node]:
    """The named sections, in document order.

    Ids the model invented are simply absent from the result — a section that
    does not exist cannot be read, and pretending otherwise would put an empty
    string in front of an answer as though it were evidence.
    """
    wanted = set(node_ids)
    return [n for n in root.walk() if n.node_id in wanted]


def section_text(node: Node, include_children: bool = True, limit: int = MAX_NODE_CHARS) -> str:
    """A section's text, optionally with everything nested under it.

    A parent heading usually has little text of its own — the substance is in
    its subsections — so asking for "4. Requirements" has to mean the whole of
    section 4, or the answer would be built from a heading and nothing else.
    """
    if include_children:
        pieces = [n.text for n in node.walk() if n.text]
    else:
        pieces = [node.text] if node.text else []
    joined = "\n\n".join(pieces).strip()
    return _split_oversized(joined, limit)[0] if len(joined) > limit else joined


def outline_json(root: Node) -> str:
    """The structure, as the model sees it: titles and sizes, no text."""
    return json.dumps(root.outline(), ensure_ascii=False, indent=1)


def count(root: Node) -> int:
    return len(root.walk()) - 1  # the root is scaffolding, not a section


__all__ = [
    "CHARS_PER_TOKEN",
    "FALLBACK_CHARS",
    "MAX_NODE_CHARS",
    "PREVIEW_CHARS",
    "build_cached",
    "cache_stats",
    "clear_cache",
    "Node",
    "build",
    "count",
    "find",
    "outline_json",
    "page_count",
    "page_of",
    "section_text",
]
