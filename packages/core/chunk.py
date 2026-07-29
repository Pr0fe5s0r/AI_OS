from __future__ import annotations

import re
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# CHUNKING — a document is not one idea, and must not be one vector.
#
# Every document used to be embedded whole. A 22,000-character specification
# became a single 1536-float point: the average of everything it said, which
# is a good match for nothing it said. Retrieval could only ever answer "this
# document is broadly on topic", and the graph had exactly one node per file,
# so there was no structure in it to look at.
#
# So text is split into passages, and the passage is the unit of everything
# downstream — embedding, recall, citation, and the nodes in the graph.
#
# Splitting is structural before it is arithmetic. Markdown already marks
# where the author changed subject; cutting every N characters would put the
# boundary mid-sentence and give two passages that each say half a thing.
# Blocks are packed up to a target size and only split mid-block when a single
# block is oversized on its own.
# ---------------------------------------------------------------------------

# ~250 tokens. Small enough that a passage is about one thing, large enough to
# carry the context that makes it meaningful on its own.
DEFAULT_SIZE = 1000
# Enough to keep a sentence straddling a boundary intact in one of the two.
DEFAULT_OVERLAP = 150
# A tail shorter than this is folded back rather than left as a passage that
# says "…and therefore we recommend." with no visible subject.
MIN_TAIL = 250

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_SENTENCE = re.compile(r"(?<=[.!?])\s+")


@dataclass(slots=True, frozen=True)
class Chunk:
    """One passage of a document, and where in it the passage came from."""

    ordinal: int
    text: str
    heading: str  # e.g. "3. Retrieval > 3.2 Ranking" — empty above the first heading

    @property
    def label(self) -> str:
        """What a citation shows. The heading if there is one, else the opening."""
        return self.heading or self.text[:60].strip()


@dataclass(slots=True)
class _Block:
    text: str
    heading: str


def _blocks(markdown: str) -> list[_Block]:
    """Markdown to blocks, each carrying the heading path in force above it.

    A heading is glued to the content beneath it rather than emitted as a block
    of its own. Left separate it can end up as the last thing in a passage —
    a section title with nothing under it, while the text it introduces opens
    the next passage with no idea what it belongs to. Gluing here removes that
    case entirely instead of guarding against it at every packing decision.
    """
    out: list[_Block] = []
    stack: list[tuple[int, str]] = []
    buffer: list[str] = []
    pending_headings: list[str] = []

    def flush() -> None:
        text = "\n".join(buffer).strip()
        buffer.clear()
        if not text:
            return
        if pending_headings:
            text = "\n\n".join([*pending_headings, text])
            pending_headings.clear()
        out.append(_Block(text=text, heading=" > ".join(t for _, t in stack)))

    for line in markdown.splitlines():
        matched = _HEADING.match(line.strip())
        if matched:
            flush()
            level, title = len(matched.group(1)), matched.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            pending_headings.append(line.strip())
        elif not line.strip():
            flush()
        else:
            buffer.append(line)
    flush()

    # A heading with no body at all — the last line of a document, usually.
    if pending_headings:
        out.append(
            _Block(text="\n\n".join(pending_headings), heading=" > ".join(t for _, t in stack))
        )
    return out


def _split_oversized(text: str, size: int) -> list[str]:
    """A single block bigger than the target, cut at the best boundary available.

    Sentences first. A block with no sentence breaks at all — a wide table row,
    a base64 blob — is cut on whitespace, and only cut mid-word if it has no
    whitespace either, because at that point there is no better answer.
    """
    pieces: list[str] = []
    current = ""
    for sentence in _SENTENCE.split(text):
        if current and len(current) + len(sentence) + 1 > size:
            pieces.append(current.strip())
            current = sentence
        else:
            current = f"{current} {sentence}".strip() if current else sentence
    if current.strip():
        pieces.append(current.strip())

    final: list[str] = []
    for piece in pieces:
        while len(piece) > size:
            cut = piece.rfind(" ", 0, size)
            if cut <= 0:
                cut = size
            final.append(piece[:cut].strip())
            piece = piece[cut:].strip()
        if piece:
            final.append(piece)
    return final


def _tail(text: str, overlap: int) -> str:
    """The end of a passage, snapped to a word boundary, to open the next one."""
    if overlap <= 0 or len(text) <= overlap:
        return text
    window = text[-overlap:]
    space = window.find(" ")
    return window[space + 1 :] if space != -1 else window


def split(
    markdown: str, size: int = DEFAULT_SIZE, overlap: int = DEFAULT_OVERLAP
) -> list[Chunk]:
    """A document as passages, in order.

    Overlap exists so a fact stated across a boundary is whole in at least one
    passage. It is carried from the tail of the previous passage rather than by
    re-reading the source, so it survives the block packing above.
    """
    text = markdown.strip()
    if not text:
        return []

    blocks = _blocks(text)
    if not blocks:
        return []

    chunks: list[Chunk] = []
    current: list[str] = []
    current_heading = blocks[0].heading
    carry = ""

    def flush() -> None:
        nonlocal current, carry
        body = "\n\n".join(current).strip()
        current = []
        if not body:
            return
        joined = f"{carry}\n\n{body}".strip() if carry else body
        chunks.append(Chunk(ordinal=len(chunks), text=joined, heading=current_heading))
        carry = _tail(body, overlap)

    for block in blocks:
        pending = sum(len(c) + 2 for c in current)
        if current and pending + len(block.text) > size:
            flush()
            current_heading = block.heading

        if len(block.text) > size:
            if current:
                flush()
                current_heading = block.heading
            parts = _split_oversized(block.text, size)
            for part in parts:
                current = [part]
                current_heading = block.heading
                flush()
            continue

        if not current:
            current_heading = block.heading
        current.append(block.text)

    flush()

    # A short final passage is folded back: on its own it reads as a fragment,
    # and it embeds as one too.
    if len(chunks) > 1 and len(chunks[-1].text) < MIN_TAIL:
        last = chunks.pop()
        previous = chunks[-1]
        chunks[-1] = Chunk(
            ordinal=previous.ordinal,
            text=f"{previous.text}\n\n{last.text}",
            heading=previous.heading,
        )

    return chunks


def embedding_text(heading: str, text: str, title: str) -> str:
    """What actually gets embedded.

    The document title and heading path are prepended so a passage carries its
    own context. Without it, a passage reading "This must complete within 200ms"
    is unattributable — the model cannot tell what "this" is, and neither can
    anyone reading the result.

    Takes the fields rather than a Chunk so the stored form and the freshly
    split form can both use it without one importing the other.
    """
    header = " > ".join(p for p in (title, heading) if p)
    return f"{header}\n\n{text}" if header else text


__all__ = ["DEFAULT_OVERLAP", "DEFAULT_SIZE", "Chunk", "embedding_text", "split"]
