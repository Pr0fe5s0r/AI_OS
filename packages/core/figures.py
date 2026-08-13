from __future__ import annotations

import asyncio
import io
import json
import re
from dataclasses import asdict, dataclass
from typing import Any

from packages.core import blobs, pages

# ---------------------------------------------------------------------------
# FIGURES — the pictures inside a page, found without asking a model.
#
# A page picture answers "where did this come from?". It does not answer "show
# me the architecture diagram", because the diagram arrives three centimetres
# tall in the corner of a sheet of paper, with the rest of the page around it.
# For a question ABOUT a drawing — a flowchart, an architecture, a chart, a
# photograph — the page is the wrong crop.
#
# So a page is asked where its pictures are. PDFium already knows: a PDF page is
# a list of objects, and an embedded raster or a cluster of vector strokes IS
# the figure, with its box given exactly rather than estimated. That makes this
# DETERMINISTIC and free — no vision call, no tokens, no model latency. The same
# page yields the same boxes forever, which is why the result is cached beside
# the page picture it will be cropped out of.
#
# Two consequences worth stating, because they are the whole design:
#
#   * Nothing here runs on the answer path. Figures are fetched by the reader
#     AFTER an answer has streamed, against the citations it already returned.
#     A store with no figures anywhere is exactly as fast as it was before.
#   * Nothing here is a claim about meaning. This module says "there is a
#     drawing here", never "this is the architecture diagram". What a figure
#     shows is decided by the text around it — the caption, and the passage that
#     cited the page — which is evidence the store already has.
# ---------------------------------------------------------------------------

# Boxes are percentages of the page, origin top-left — the same units the vision
# regions use, so one component draws both.

# A rule under a heading is 0.1% tall and a bullet glyph is 1% wide. Neither is
# a figure, and both are on nearly every page of everything.
MIN_SIDE_PCT = 4.0
MIN_AREA_PCT = 1.2
# The same "a box round everything tells nobody anything" rule the vision
# regions use: 80% in both directions.
FULL_PAGE_AREA = 8000.0
# Per page. A page with more pictures than this is a contact sheet, and the
# reader is better served by the page itself.
MAX_FIGURES = 6
# How many strokes make a drawing rather than a table border or a box round a
# paragraph. A flowchart is dozens; a framed callout is four.
MIN_STROKES = 6
# And how many of them have to be DRAWN — curved, or diagonal. This is what
# separates a diagram from a table, deterministically and without a model: a
# table is horizontal and vertical rules and nothing else, forever, while an
# arrow has a slanted head and a rounded box has a bezier corner. Without this
# a requirements document reported four "figures" per page, every one of them a
# shaded table cell.
MIN_DRAWN_STROKES = 2
# A segment counts as drawn when it leaves the axes by more than this fraction
# of its own length — hairline rendering error in an axis-aligned rule should
# not promote a table row to a diagram.
SLANT_TOLERANCE = 0.02
# Two boxes closer than this (as a fraction of the page) belong to one drawing —
# the arrow and the box it points at are separate objects to PDFium.
CLUSTER_GAP_PCT = 2.5
# Page furniture: a masthead, a logo, a footer rule. Short, and pinned to the
# top or bottom margin. Dropped because it is on EVERY page — a reader who asked
# about a diagram is not being shown the journal's logo six times.
MARGIN_TOP_PCT = 15.0
MARGIN_BOTTOM_PCT = 88.0
FURNITURE_HEIGHT_PCT = 15.0

# Page objects examined. A dense chart can hold tens of thousands of strokes,
# and clustering is quadratic in the worst case; past this the page is scenery,
# not a diagram anybody asked about.
MAX_OBJECTS = 6000

# A form XObject is a graphic placed as a unit, which is how many drawing tools
# export a figure — the attention-visualisation figure in "Attention Is All You
# Need" is one, and the stroke clustering could never see it because its strokes
# are inside the form rather than on the page.
#
# But a form is also just a container, and some producers wrap ordinary text in
# them. So forms are trusted only when the page has a HANDFUL of page-sized
# ones: two placed figures is a figure layout, forty is a text layout, and the
# difference is not worth guessing at per box.
MAX_TRUSTED_FORMS = 6

# A candidate is confirmed against the page as it RENDERS, because the object
# tree has no opinion about white on white. That same paper carries invisible
# 62%-wide forms on three of its pages, and a reader would see each one as an
# empty rectangle labelled "figure" — worse than no figure at all, since it
# looks like something failed to load.
#
# The test is coverage, not blankness. A box holding nothing but the caption
# that sits under the real figure is not blank — it has words in it — and it is
# still not a figure. So the crop is divided into a grid and the ink has to be
# SPREAD: a caption inks one band of cells, a drawing inks most of them.
INK_GRID = 8
MIN_INK_COVERAGE = 0.25
# How far a pixel must sit from the page's background shade to count as ink.
# Loose enough to catch pale blue arrows, tight enough to ignore anti-aliasing.
INK_DELTA = 24
INK_SAMPLE = 96

# What a caption looks like when a document has one. Matched rather than
# assumed: text under a figure is a caption in a paper and body copy in a
# brochure, and labelling the wrong sentence as a caption is worse than showing
# no label at all.
_CAPTION = re.compile(
    r"^\s*((?:figure|fig\.?|table|chart|diagram|exhibit|image|plate|scheme)\s*"
    r"[0-9ivx]*\s*[.:\-–—)]?\s*.{0,160})",
    re.IGNORECASE | re.DOTALL,
)
# How far below a figure to look for its caption, as a fraction of page height.
CAPTION_BAND = 0.06


@dataclass(slots=True)
class Figure:
    """One picture inside a page, in page percentages from the top-left."""

    x: float
    y: float
    w: float
    h: float
    # "image" for an embedded raster, "drawing" for a cluster of vector strokes.
    # Said plainly because they fail differently: a missed raster is a bug, a
    # missed drawing is a threshold.
    kind: str
    caption: str = ""

    def area(self) -> float:
        return self.w * self.h


def _clamp(figure: Figure) -> Figure:
    figure.x = max(0.0, min(figure.x, 100.0))
    figure.y = max(0.0, min(figure.y, 100.0))
    figure.w = min(figure.w, 100.0 - figure.x)
    figure.h = min(figure.h, 100.0 - figure.y)
    return figure


def _furniture(figure: Figure) -> bool:
    """A short band pinned to the top or bottom of the page."""
    if figure.h >= FURNITURE_HEIGHT_PCT:
        return False
    return figure.y < MARGIN_TOP_PCT or figure.y + figure.h > MARGIN_BOTTOM_PCT


def _worth_showing(figure: Figure) -> bool:
    if figure.w < MIN_SIDE_PCT or figure.h < MIN_SIDE_PCT:
        return False
    if figure.area() < MIN_AREA_PCT * 100:
        return False
    if _furniture(figure):
        return False
    return figure.area() < FULL_PAGE_AREA


def _overlap(a: Figure, b: Figure, gap: float = 0.0) -> bool:
    return not (
        a.x > b.x + b.w + gap
        or b.x > a.x + a.w + gap
        or a.y > b.y + b.h + gap
        or b.y > a.y + a.h + gap
    )


def _union(a: Figure, b: Figure) -> Figure:
    left, top = min(a.x, b.x), min(a.y, b.y)
    right, bottom = max(a.x + a.w, b.x + b.w), max(a.y + a.h, b.y + b.h)
    return Figure(left, top, right - left, bottom - top, a.kind or b.kind)


def _merge(boxes: list[tuple[Figure, int, int]], gap: float) -> list[tuple[Figure, int, int]]:
    """Grow overlapping-or-near boxes into groups, summing their tallies.

    Each entry is (box, strokes, drawn strokes), and both counts matter as much
    as the box: six strokes in one place is a drawing only if some of them
    curve. Repeated passes rather than a single sweep — merging A into B can
    bring B within reach of C, and one pass would leave a flowchart as three
    fragments.
    """
    groups = list(boxes)
    changed = True
    while changed and len(groups) > 1:
        changed = False
        out: list[tuple[Figure, int, int]] = []
        for box, count, drawn in groups:
            for index, (other, others, other_drawn) in enumerate(out):
                if _overlap(box, other, gap):
                    out[index] = (_union(box, other), count + others, drawn + other_drawn)
                    changed = True
                    break
            else:
                out.append((box, count, drawn))
        groups = out
    return groups


def _is_drawn(obj: Any) -> bool:
    """True when this path curves or slants — i.e. when it was drawn, not ruled.

    Asked of the path's own segments rather than of its bounding box, because a
    table cell and an arrowhead have the same kind of box and completely
    different insides. A bezier is drawn by definition; a line is drawn when it
    moves in both axes at once.
    """
    import pypdfium2.raw as raw

    try:
        count = raw.FPDFPath_CountSegments(obj.raw)
    except Exception:
        return False
    if count <= 0:
        return False

    import ctypes

    last: tuple[float, float] | None = None
    for index in range(min(count, 64)):
        segment = raw.FPDFPath_GetPathSegment(obj.raw, index)
        if not segment:
            continue
        kind = raw.FPDFPathSegment_GetType(segment)
        x, y = ctypes.c_float(), ctypes.c_float()
        if not raw.FPDFPathSegment_GetPoint(segment, ctypes.byref(x), ctypes.byref(y)):
            continue
        point = (x.value, y.value)
        if kind == raw.FPDF_SEGMENT_BEZIERTO:
            return True
        if kind == raw.FPDF_SEGMENT_LINETO and last is not None:
            dx, dy = abs(point[0] - last[0]), abs(point[1] - last[1])
            reach = max(dx, dy)
            if reach > 0 and min(dx, dy) / reach > SLANT_TOLERANCE:
                return True
        last = point
    return False


def _detect_sync(data: bytes, page: int) -> list[Figure]:
    """Every picture on one page of a PDF. Page numbers are 1-based, as printed.

    An image object is a figure on its own — something was placed there as a
    picture, and that is not a judgement call. Vector strokes are only a figure
    in a CROWD: every framed paragraph and every table in the world is four
    lines, so a handful of strokes near each other is furniture, and dozens is a
    drawing.
    """
    import pypdfium2 as pdfium
    import pypdfium2.raw as raw

    pdf = pdfium.PdfDocument(io.BytesIO(data))
    try:
        if not 1 <= page <= len(pdf):
            return []
        target = pdf[page - 1]
        width, height = target.get_size()
        if not width or not height:
            return []

        def to_box(obj: Any, kind: str) -> Figure | None:
            try:
                left, bottom, right, top = obj.get_pos()
            except Exception:
                # An object with no position — a clipped form, a degenerate
                # path. Skipped rather than raised: one bad object must not
                # cost the page its other figures.
                return None
            return _clamp(
                Figure(
                    x=left / width * 100,
                    y=(height - top) / height * 100,
                    w=(right - left) / width * 100,
                    h=(top - bottom) / height * 100,
                    kind=kind,
                )
            )

        rasters: list[tuple[Figure, int, int]] = []
        strokes: list[tuple[Figure, int, int]] = []
        placed: list[Figure] = []
        seen = 0
        # max_depth so a diagram placed inside a form XObject — which is how
        # most vector figures are actually embedded — is descended into rather
        # than reported as one opaque box the size of the page.
        for obj in target.get_objects(max_depth=4):
            seen += 1
            if seen > MAX_OBJECTS:
                break
            if obj.type == raw.FPDF_PAGEOBJ_IMAGE:
                box = to_box(obj, "image")
                if box:
                    rasters.append((box, 1, 1))
            elif obj.type in (raw.FPDF_PAGEOBJ_PATH, raw.FPDF_PAGEOBJ_SHADING):
                box = to_box(obj, "drawing")
                if box:
                    strokes.append((box, 1, 1 if _is_drawn(obj) else 0))
            elif obj.type == raw.FPDF_PAGEOBJ_FORM:
                box = to_box(obj, "drawing")
                if box and _worth_showing(box):
                    placed.append(box)

        found: list[Figure] = [
            group for group, _, _ in _merge(rasters, gap=0.0) if _worth_showing(group)
        ]
        if len(placed) <= MAX_TRUSTED_FORMS:
            found.extend(placed)
        for group, count, drawn in _merge(strokes, gap=CLUSTER_GAP_PCT):
            if count < MIN_STROKES or drawn < MIN_DRAWN_STROKES:
                continue
            if _worth_showing(group) and not any(_overlap(group, kept) for kept in found):
                # A drawing that sits on top of a picture is the picture's
                # annotation, not a second figure.
                found.append(group)

        found.sort(key=lambda f: (f.y, f.x))
        found = found[:MAX_FIGURES]
        if found:
            _caption_sync(target, found, width, height)
        return found
    finally:
        pdf.close()


def _trim_caption(raw_text: str) -> str:
    """End a caption where its sentence ends.

    The band under a figure is read across the figure's own width, so on a
    two-column page it also catches the start of whatever sits beside it — the
    Transformer's caption came back as "…model architecture. ows this ove".
    Captions end in a full stop; the first one past the label is where this one
    ends. Offset past "Fig." so an abbreviation is not mistaken for the end.
    """
    text = " ".join(raw_text.split())
    stop = text.find(". ", 12)
    if stop != -1:
        return text[: stop + 1]
    return text[:160].strip()


def _caption_sync(target: Any, found: list[Figure], width: float, height: float) -> None:
    """Label each figure with the caption printed under it, when there is one.

    Read off the page rather than generated. A caption a document wrote is a
    fact about the document; a caption a model writes about a crop is another
    claim to check, which is exactly the thing this store exists to avoid.
    """
    try:
        text_page = target.get_textpage()
    except Exception:
        return
    try:
        for figure in found:
            top = height - (figure.y + figure.h) / 100 * height
            band = CAPTION_BAND * height
            try:
                below = text_page.get_text_bounded(
                    left=figure.x / 100 * width,
                    bottom=max(0.0, top - band),
                    right=(figure.x + figure.w) / 100 * width,
                    top=top,
                )
            except Exception:
                continue
            match = _CAPTION.match(" ".join((below or "").split()))
            if match:
                figure.caption = _trim_caption(match.group(1))
    finally:
        try:
            text_page.close()
        except Exception:
            pass


def _too_empty(png: bytes, figure: dict[str, Any]) -> bool:
    """True when this crop of the rendered page has too little ink, too bunched.

    Coverage rather than presence. An invisible form crops to bare paper, and a
    box that has slipped off its figure onto the caption underneath crops to two
    lines of text at one edge — both are page objects the tree is perfectly
    happy with, and neither is a picture. A drawing puts marks across most of
    its own area; that is what the grid measures.
    """
    import numpy as np
    from PIL import Image

    try:
        with Image.open(io.BytesIO(png)) as opened:
            width, height = opened.size
            box = (
                int(figure["x"] / 100 * width),
                int(figure["y"] / 100 * height),
                int((figure["x"] + figure["w"]) / 100 * width),
                int((figure["y"] + figure["h"]) / 100 * height),
            )
            if box[2] - box[0] < 2 or box[3] - box[1] < 2:
                return True
            crop = opened.crop(box).convert("L").resize(
                (INK_GRID * INK_SAMPLE // INK_GRID, INK_GRID * INK_SAMPLE // INK_GRID),
                Image.Resampling.BILINEAR,
            )
            pixels = np.asarray(crop, dtype=np.int16)

        # The page's own paper, whatever colour it is — taken as the commonest
        # shade rather than assumed white, so a dark-themed diagram (the casino
        # architecture is white on near-black) is not read as solid ink.
        background = int(np.bincount(pixels.ravel().astype(np.uint8)).argmax())
        ink = np.abs(pixels - background) > INK_DELTA

        step = pixels.shape[0] // INK_GRID
        cells = ink[: step * INK_GRID, : step * INK_GRID].reshape(
            INK_GRID, step, INK_GRID, step
        )
        inked = (cells.sum(axis=(1, 3)) > 1).sum()
        return inked / (INK_GRID * INK_GRID) < MIN_INK_COVERAGE
    except Exception:
        # Unreadable render: keep the figure. A detector that discards its
        # findings whenever the check itself fails is worse than no check.
        return False


# Stamped into the cache key. A page picture is a render and never changes, but
# a figure is a JUDGEMENT — thresholds move as documents that break them turn
# up. Bumping this retires every cached verdict at once, instead of leaving a
# store answering with rules that were replaced months ago.
FORMAT = 2


def figures_key(workspace_id: str, item_id: str, page: int) -> str:
    return f"{workspace_id}/{item_id}/figures/v{FORMAT}/{page}.json"


async def for_page(workspace_id: str, item_id: str, page: int) -> list[dict[str, Any]]:
    """The figures on one page, detected once and cached beside the original.

    Cached rather than computed at ingest for the same reason page pictures are:
    most pages of most documents are never looked at, and paying to analyse a
    700-page book on upload to serve the four pages somebody eventually cites is
    work nobody asked for. Detection is milliseconds; the cache exists so the
    reader's second glance costs nothing at all.
    """
    if not blobs.enabled():
        return []
    key = figures_key(workspace_id, item_id, page)
    if await blobs.exists(key):
        try:
            data, _ = await blobs.get(key)
            return list(json.loads(data))
        except Exception:
            # A cached result that will not parse is rebuilt, not raised.
            pass

    try:
        original, content_type = await blobs.get(blobs.key_for(workspace_id, item_id))
    except Exception:
        return []
    if not pages.renderable(content_type, ""):
        return []

    found: list[dict[str, Any]]
    if pages.is_image(content_type, ""):
        # An image document has one page and it IS the figure. Returned whole
        # rather than analysed: cropping a photograph down to the parts that
        # look drawn would hide the rest of the picture the reader asked about.
        found = [{"x": 0.0, "y": 0.0, "w": 100.0, "h": 100.0, "kind": "image", "caption": ""}]
    else:
        try:
            found = [asdict(f) for f in await asyncio.to_thread(_detect_sync, original, page)]
        except Exception:
            # A file that will not open still answers from its text. Losing the
            # figures is a degradation, not a failure.
            return []

    for figure in found:
        for edge in ("x", "y", "w", "h"):
            figure[edge] = round(float(figure[edge]), 2)

    # Checked against the page as it actually renders. This also warms the very
    # PNG the reader is about to crop, so the picture request behind it lands on
    # a cache that this call just filled.
    if found and not pages.is_image(content_type, ""):
        rendered = await pages.image(workspace_id, item_id, page)
        if rendered:
            found = [f for f in found if not _too_empty(rendered, f)]

    try:
        await blobs.put(key, json.dumps(found).encode("utf-8"), "application/json")
    except Exception:
        # Serving them uncached is better than serving nothing.
        pass
    return found


def enabled() -> bool:
    """Figures need the stored original, exactly as page pictures do."""
    return pages.enabled()


__all__ = [
    "MAX_FIGURES",
    "Figure",
    "enabled",
    "figures_key",
    "for_page",
]
