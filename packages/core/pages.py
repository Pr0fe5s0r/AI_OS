from __future__ import annotations

import asyncio
import io
import os
from typing import Any

from packages.core import blobs

# ---------------------------------------------------------------------------
# PAGE PICTURES — a document as it was printed, not as it was parsed.
#
# Text extraction reads a PDF the way a machine wrote it, not the way a person
# laid it out. Prose survives that; a TABLE does not. A table is meaning encoded
# in POSITION — this number belongs to that row and that column — and position
# is exactly what a stream of extracted text throws away. Pull a table out of a
# PDF and you get "2024 2025 Revenue 41 52 Costs 33 39", which contains every
# figure and answers no question about any of them.
#
# That failure is invisible to retrieval. The section is found, the words are
# all present, and the answer is still wrong — which is worse than finding
# nothing, because nothing is honest.
#
# So a page can also be looked at. This module turns one page of a stored
# original back into the picture it always was; the reading is done elsewhere,
# by a model with eyes.
#
# Rendered pages are cached beside the original they came from. Rendering is
# deterministic — the same page of the same file is the same picture — so it is
# done once and kept, not repeated per question.
# ---------------------------------------------------------------------------

# Wide enough that small table type stays legible after the model's own
# downscaling; not so wide that a page becomes a megabyte of base64.
TARGET_WIDTH = 1400
# Past this a "document" is a book, and looking at page 300 of it is not the
# workflow this exists for.
MAX_PAGES = 200


def enabled() -> bool:
    """False when no object store is configured.

    Pages are rendered FROM the stored original, so without one there is
    nothing to render and the whole path is simply not offered — the same way
    the document viewer degrades to text.
    """
    return blobs.enabled()


IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff")


def is_image(content_type: str, locator: str = "") -> bool:
    return content_type.startswith("image/") or locator.lower().endswith(IMAGE_SUFFIXES)


def renderable(content_type: str, locator: str = "") -> bool:
    """Which documents have a page a picture could be OF.

    PDFs, and images — an image is a page, the only one it has. Nothing else:
    a pasted note, a Markdown file, a .docx or a spreadsheet has no fixed page,
    and a slide deck has slides we can read but cannot render without a
    headless Office install. Those citations show text and nothing else, which
    is honest — an empty picture frame would suggest something is missing when
    nothing is.
    """
    if content_type == "application/pdf" or locator.lower().endswith(".pdf"):
        return True
    # An image IS a page — the only one it has. Treating it as such means a
    # citation from a photograph shows the photograph, with no special case
    # anywhere above this line.
    return is_image(content_type, locator)


def page_key(workspace_id: str, item_id: str, page: int) -> str:
    return f"{workspace_id}/{item_id}/pages/{page}.png"


def _looks_like_pdf(data: bytes) -> bool:
    return data[:5] == b"%PDF-"


def _image_to_png(data: bytes) -> bytes:
    """An uploaded image, re-encoded as PNG.

    Re-encoded rather than passed through so everything downstream — the
    vision call, the citation thumbnail, the browser — deals with exactly one
    format. A CMYK JPEG or a palettised GIF handed straight to a model is a
    class of failure nobody would ever look for.
    """
    from PIL import Image

    with Image.open(io.BytesIO(data)) as opened:
        # Flatten transparency onto white: a chart exported as a transparent
        # PNG becomes black-on-black to a model that composites onto its own
        # background, and the answer comes back as "the image is blank".
        picture = opened.convert("RGBA") if opened.mode in ("RGBA", "LA", "P") else opened.convert("RGB")
        if picture.mode == "RGBA":
            flat = Image.new("RGB", picture.size, "white")
            flat.paste(picture, mask=picture.split()[-1])
            picture = flat
        if picture.width > TARGET_WIDTH:
            height = round(picture.height * TARGET_WIDTH / picture.width)
            picture = picture.resize((TARGET_WIDTH, height), Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        picture.save(buffer, format="PNG", optimize=True)
        return buffer.getvalue()


def _render_sync(data: bytes, page: int) -> bytes:
    """One page of a PDF as PNG bytes. Page numbers are 1-based, as printed.

    pypdfium2 rather than PyMuPDF on purpose: PyMuPDF is AGPL, which for a
    product that ships to customers is a licence decision disguised as a
    dependency choice. pypdfium2 wraps Google's PDFium under permissive terms
    and renders just as well.
    """
    if not _looks_like_pdf(data):
        # An image has exactly one page, and it is itself.
        if page != 1:
            raise IndexError(f"page {page} of 1")
        return _image_to_png(data)

    import pypdfium2

    pdf = pypdfium2.PdfDocument(io.BytesIO(data))
    try:
        if not 1 <= page <= len(pdf):
            raise IndexError(f"page {page} of {len(pdf)}")
        target = pdf[page - 1]
        # PDF sizes are in points; scale to hit the width we want.
        width = target.get_size()[0] or 1
        image = target.render(scale=max(1.0, TARGET_WIDTH / width)).to_pil()
        buffer = io.BytesIO()
        image.save(buffer, format="PNG", optimize=True)
        return buffer.getvalue()
    finally:
        pdf.close()


def _count_sync(data: bytes) -> int:
    if not _looks_like_pdf(data):
        return 1

    import pypdfium2

    pdf = pypdfium2.PdfDocument(io.BytesIO(data))
    try:
        return len(pdf)
    finally:
        pdf.close()


async def count(workspace_id: str, item_id: str, locator: str = "") -> int:
    """How many pages this document has, or 0 if it has none to look at.

    Zero is the answer for everything that is not a stored PDF — a missing
    original, an object store that is switched off, a Markdown file. Every one
    of those means the same thing to a caller: there is no picture here.
    """
    if not enabled():
        return 0
    try:
        data, content_type = await blobs.get(blobs.key_for(workspace_id, item_id))
    except Exception:
        return 0
    if not renderable(content_type, locator):
        return 0
    try:
        return min(await asyncio.to_thread(_count_sync, data), MAX_PAGES)
    except Exception:
        # A file that will not open is not an error worth raising here: the
        # document is still indexed and still answerable from its text. Losing
        # the picture is a degradation, not a failure.
        return 0


async def image(workspace_id: str, item_id: str, page: int) -> bytes | None:
    """The PNG of one page, rendered once and cached beside the original."""
    if not enabled():
        return None
    key = page_key(workspace_id, item_id, page)
    if await blobs.exists(key):
        data, _ = await blobs.get(key)
        return data

    try:
        original, _ = await blobs.get(blobs.key_for(workspace_id, item_id))
        png = await asyncio.to_thread(_render_sync, original, page)
    except Exception:
        return None

    await blobs.put(key, png, "image/png")
    return png


# How many pages of a scan are read at ingest. Every page is a vision call, so
# a 400-page scan uploaded by accident would otherwise be a bill rather than a
# document. What is transcribed is indexed and what is not is said out loud on
# the item, so a truncated scan is visibly truncated instead of quietly short.
def max_transcribe_pages() -> int:
    return int(os.getenv("MAX_TRANSCRIBE_PAGES", "20"))


# Ingest-time reading is a different job from the navigator's mid-question look.
# There the model is told what to hunt for; here nobody has asked anything yet,
# so it takes the whole page and keeps the structure — this text is all the
# document will ever have, and anything dropped now is gone from search too.
_TRANSCRIBE = (
    "Transcribe this page completely, as Markdown. Rules:\n"
    "- Every line of text on the page, in reading order.\n"
    "- Tables as Markdown tables, each value under its own column header. "
    "Leave a cell empty if the page leaves it empty.\n"
    "- Headings as Markdown headings, matching the level they appear to be.\n"
    "- Copy numbers, dates, codes and units exactly. Never round or convert.\n"
    "- Describe a photograph or diagram in one line, in square brackets.\n"
    "- Do not summarise, interpret, or add anything not on the page.\n"
    "- If the page is blank, reply with nothing at all."
)


# A whole document that is one picture — a photograph, a screenshot, an
# exported chart. Its description is not a footnote to the text, it IS the
# text, so it is asked for in full rather than in the one line a figure inside
# a page gets.
_DESCRIBE_IMAGE = (
    "This image is an entire document in a knowledge base. Describe it fully "
    "and factually, so that someone who cannot see it could answer questions "
    "about it.\n\n"
    "Cover, in this order:\n"
    "- One line saying what kind of image it is (photograph, screenshot, "
    "chart, diagram, scan of a document, handwriting).\n"
    "- Every piece of text visible in it, transcribed exactly, including "
    "labels, captions, axis values, UI text and handwriting.\n"
    "- Tables as Markdown tables, each value under its own column header.\n"
    "- Charts: each bar, point or slice read against the axis with an "
    "approximate value, and which is largest and smallest.\n"
    "- Shapes and diagrams: what is drawn, how many sides, colours, labels, "
    "arrangement, arrow directions, and markers such as a right-angle square.\n"
    "- People, objects and setting, if it is a photograph.\n"
    "Do not speculate about anything not visible, and do not draw conclusions."
)


async def transcribe(data: bytes, page: int) -> str:
    """Read one page of a PDF, given the file's own bytes.

    Takes bytes rather than an item id because this runs during ingest, before
    anything about the document has been written down.
    """
    from packages.core.llm import look

    model = vision_model()
    if not model:
        return ""
    png = await asyncio.to_thread(_render_sync, data, page)
    prompt = _TRANSCRIBE if _looks_like_pdf(data) else _DESCRIBE_IMAGE
    seen = await asyncio.to_thread(look, png, prompt, model=model)
    return (seen or "").strip()


async def transcribe_document(data: bytes, limit: int | None = None) -> tuple[list[str], int]:
    """Read a scanned PDF page by page. Returns (page texts, pages in the file).

    Pages are read one at a time and in order, not concurrently: a scan that
    fails halfway should leave the pages it managed, and thirty parallel vision
    calls is the fastest way to be rate-limited into an empty document.
    """
    total = await asyncio.to_thread(_count_sync, data)
    ceiling = min(total, limit or max_transcribe_pages())
    out: list[str] = []
    for number in range(1, ceiling + 1):
        try:
            out.append(await transcribe(data, number))
        except Exception:
            # Keep what was read. A partial document is retrievable; an
            # exception here would throw away every page that did work.
            break
    return out, total


def vision_model() -> str:
    """The model that reads pictures, or "" when none is configured.

    Separate from CHAT_MODEL because they are different jobs: the navigator
    runs on a text model that is good at tools, and looking at a page needs a
    model with eyes. Unset means the whole looking path stays switched off and
    the system behaves exactly as it did before — a deployment should not
    silently start making a second kind of model call because it upgraded.
    """
    return os.getenv("VISION_MODEL", "").strip()


def available() -> bool:
    return enabled() and bool(vision_model())


__all__: list[Any] = [
    "MAX_PAGES",
    "TARGET_WIDTH",
    "available",
    "count",
    "enabled",
    "image",
    "page_key",
    "renderable",
    "vision_model",
]
