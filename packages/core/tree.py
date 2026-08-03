from __future__ import annotations

import json
import re
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


def _fallback_sections(body: str) -> list[dict[str, str]]:
    """Readable units for a document with no headings.

    Pages first: a PDF has them, they are what its own numbering refers to, and
    "page 4" is a citation someone can check against the original file. Failing
    that, blocks of whole paragraphs, named by position so a reader at least
    knows where in the document they are.
    """
    pages = list(_PAGE.finditer(body))
    if pages:
        sections: list[dict[str, str]] = []
        for index, match in enumerate(pages):
            start = match.end()
            end = pages[index + 1].start() if index + 1 < len(pages) else len(body)
            text = body[start:end].strip().strip("-").strip()
            if text:
                sections.append({"title": f"Page {match.group(1)}", "text": text})
        if sections:
            return sections

    stripped = body.strip()
    if not stripped:
        return []
    if len(stripped) <= FALLBACK_CHARS:
        return [{"title": "(whole document)", "text": stripped}]

    blocks = _split_oversized(stripped, FALLBACK_CHARS)
    total = len(blocks)
    return [
        {"title": f"Part {number} of {total}", "text": block}
        for number, block in enumerate(blocks, start=1)
        if block.strip()
    ]


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
                    start_line=1,
                    end_line=len(lines),
                    text=section["text"],
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
    "Node",
    "build",
    "count",
    "find",
    "outline_json",
    "section_text",
]
