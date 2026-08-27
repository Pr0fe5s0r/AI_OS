"""What counts as a figure, and what does not.

The detector's judgement lives in a handful of pure predicates, and every one of
them exists because a real document broke the version before it. These pin the
reasons, not the numbers — a threshold may move, but a table must never come
back as a diagram and an invisible box must never reach a reader.
"""

from __future__ import annotations

import io

import pytest

from packages.core import figures
from packages.core.figures import Figure


def box(x: float, y: float, w: float, h: float, kind: str = "drawing") -> Figure:
    return Figure(x=x, y=y, w=w, h=h, kind=kind)


# ------------------------------- what shows -------------------------------


def test_a_diagram_sized_box_shows():
    assert figures._worth_showing(box(30, 20, 40, 30))


def test_a_rule_under_a_heading_does_not():
    # 0.1% tall, full width: on nearly every page of everything.
    assert not figures._worth_showing(box(10, 40, 80, 0.1))


def test_a_bullet_glyph_does_not():
    assert not figures._worth_showing(box(12, 40, 1.2, 1.2))


def test_a_box_round_the_whole_page_does_not():
    # A box round everything tells nobody anything — the same rule the vision
    # regions use.
    assert not figures._worth_showing(box(2, 2, 96, 96))


def test_a_masthead_does_not():
    """A journal logo is a picture on page 1, 2, 3, 4, 5 and 6.

    Wide, short and pinned to the top margin. It passed every size test and was
    the first figure offered for a paper whose actual figures were further down.
    """
    assert not figures._worth_showing(box(7, 6, 85, 13))


def test_a_footer_band_does_not():
    assert not figures._worth_showing(box(5, 92, 90, 6))


def test_a_tall_figure_low_on_the_page_still_shows():
    # Height is what separates a figure at the bottom from furniture at the
    # bottom. Figures 4 and 5 of the transformer paper sit under the fold.
    assert figures._worth_showing(box(18, 60, 64, 36))


# ------------------------------- clustering -------------------------------


def test_neighbouring_strokes_become_one_drawing():
    """An arrow and the box it points at are separate objects to PDFium."""
    strokes = [(box(10, 10, 8, 6), 1, 1), (box(19, 10, 8, 6), 1, 0)]
    merged = figures._merge(strokes, gap=figures.CLUSTER_GAP_PCT)
    assert len(merged) == 1
    group, count, drawn = merged[0]
    assert count == 2 and drawn == 1
    assert group.x == 10 and round(group.w) == 17


def test_merging_is_transitive():
    """A ... B ... C, where only the neighbours touch.

    One sweep would leave a flowchart as three fragments, which is why the
    merge repeats until nothing moves.
    """
    strokes = [
        (box(10, 10, 5, 5), 1, 1),
        (box(16, 10, 5, 5), 1, 0),
        (box(22, 10, 5, 5), 1, 0),
    ]
    merged = figures._merge(strokes, gap=figures.CLUSTER_GAP_PCT)
    assert len(merged) == 1
    assert merged[0][1] == 3


def test_distant_strokes_stay_apart():
    strokes = [(box(5, 5, 6, 6), 1, 1), (box(70, 70, 6, 6), 1, 1)]
    assert len(figures._merge(strokes, gap=figures.CLUSTER_GAP_PCT)) == 2


# -------------------------------- captions --------------------------------


def test_a_caption_ends_where_its_sentence_ends():
    """The band under a figure is read across the figure's own width.

    On a two-column page that also catches the start of whatever sits beside
    it: the transformer's caption came back as "...architecture. ows this ove".
    """
    assert (
        figures._trim_caption("Figure 1: The Transformer - model architecture. ows this ove")
        == "Figure 1: The Transformer - model architecture."
    )


def test_an_abbreviation_is_not_the_end():
    trimmed = figures._trim_caption("Fig. 2: Attention over heads. and then noise")
    assert trimmed == "Fig. 2: Attention over heads."


def test_a_caption_without_a_full_stop_survives():
    assert figures._trim_caption("Figure 3 Attention heads") == "Figure 3 Attention heads"


def test_body_copy_is_not_a_caption():
    assert figures._CAPTION.match("The remainder of this section describes") is None


@pytest.mark.parametrize(
    "text",
    ["Figure 4: two heads", "Fig 2 - overview", "Table 1: results", "Diagram A: the flow"],
)
def test_real_caption_openings_match(text: str):
    assert figures._CAPTION.match(text) is not None


# ------------------------------ the ink check ------------------------------


def _page(draw) -> bytes:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (600, 800), "white")
    draw(ImageDraw.Draw(image))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def test_an_invisible_box_is_rejected():
    """The object tree has no opinion about white on white.

    Three pages of one paper carry a 62%-wide form with nothing in it. Every
    size rule passes; a reader sees an empty rectangle labelled "figure", which
    reads as something that failed to load.
    """
    png = _page(lambda _: None)
    assert figures._too_empty(png, {"x": 10, "y": 60, "w": 60, "h": 30})


def test_a_box_holding_only_a_caption_is_rejected():
    """Not blank — it has words in it — and still not a figure.

    This is why the test is coverage rather than emptiness: a candidate that has
    slipped off its figure onto the caption underneath is inked, in one band, at
    one edge.
    """

    def caption_only(draw):
        draw.rectangle([60, 490, 420, 500], fill="black")
        draw.rectangle([60, 505, 380, 515], fill="black")

    assert figures._too_empty(_page(caption_only), {"x": 5, "y": 60, "w": 70, "h": 30})


def test_a_drawing_is_kept():
    def diagram(draw):
        for row in range(6):
            for col in range(5):
                x, y = 70 + col * 70, 500 + row * 35
                draw.rectangle([x, y, x + 50, y + 24], outline="black", width=2)

    assert not figures._too_empty(_page(diagram), {"x": 10, "y": 60, "w": 70, "h": 30})


def test_a_pale_diagram_on_a_dark_page_is_kept():
    """Background is measured, not assumed white.

    An architecture diagram exported on a near-black canvas is solid ink to
    anything that assumes paper, and would be dropped for the opposite reason.
    """
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (600, 800), "#101018")
    draw = ImageDraw.Draw(image)
    for row in range(6):
        for col in range(5):
            x, y = 70 + col * 70, 500 + row * 35
            draw.rectangle([x, y, x + 50, y + 24], outline="#e8e8f0", width=2)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")

    assert not figures._too_empty(buffer.getvalue(), {"x": 10, "y": 60, "w": 70, "h": 30})


def test_an_unreadable_render_keeps_the_figure():
    # A check that discards its findings whenever the check itself fails is
    # worse than no check.
    assert not figures._too_empty(b"not a png", {"x": 10, "y": 10, "w": 30, "h": 30})


# --------------------------------- caching ---------------------------------


# ---------------------------- aiming a look ----------------------------


@pytest.mark.asyncio
async def test_a_picture_question_is_told_which_pages_hold_pictures():
    """The link between knowing where the figures are and using it.

    A document is navigated by its TEXT, and a diagram is invisible to that. The
    casino platform's architecture diagrams sit on pages 6, 7 and 9 with no
    caption to give them away, so a walk asked for "the architecture diagram"
    read the pages whose WORDS said architecture, looked at pages 4-6, and
    answered that the document has no such diagram.
    """
    from packages.core.navigator import _where_the_pictures_are

    class FakeScope:
        workspace_id = "ws"

    async def fake_scan(_workspace, _item, *_a, **_k):
        return [6, 7, 9]

    figures.scan = fake_scan  # type: ignore[assignment]
    try:
        hint = await _where_the_pictures_are(FakeScope(), "doc", "show me the architecture diagram")
        assert "6, 7, 9" in hint
        # A hint, never an assertion about what the picture shows.
        assert "look there" in hint

        # A question about words gets nothing: the scan is not free, and a page
        # list is noise to someone asking about a delivery date.
        assert await _where_the_pictures_are(FakeScope(), "doc", "what is the timeline") == ""
    finally:
        del figures.scan


@pytest.mark.asyncio
async def test_a_document_with_no_pictures_offers_nothing():
    """An offer of something that is not there is worse than no offer."""
    from packages.core.navigator import _where_the_pictures_are

    class FakeScope:
        workspace_id = "ws"

    async def empty(_workspace, _item, *_a, **_k):
        return []

    figures.scan = empty  # type: ignore[assignment]
    try:
        assert await _where_the_pictures_are(FakeScope(), "doc", "show me the diagram") == ""
    finally:
        del figures.scan


@pytest.mark.asyncio
async def test_a_failing_scan_is_silent():
    # A hint that cannot be produced is a hint not worth an error. Losing it
    # costs a worse-aimed look; raising costs the whole answer.
    from packages.core.navigator import _where_the_pictures_are

    class FakeScope:
        workspace_id = "ws"

    async def boom(_workspace, _item, *_a, **_k):
        raise RuntimeError("object store is down")

    figures.scan = boom  # type: ignore[assignment]
    try:
        assert await _where_the_pictures_are(FakeScope(), "doc", "show me the diagram") == ""
    finally:
        del figures.scan


@pytest.mark.asyncio
async def test_a_long_list_of_pages_is_not_a_hint():
    """Thirty page numbers is not a hint, it is the document again."""
    from packages.core.navigator import MAX_NAMED_PICTURE_PAGES, _where_the_pictures_are

    class FakeScope:
        workspace_id = "ws"

    async def many(_workspace, _item, *_a, **_k):
        return list(range(1, 31))

    figures.scan = many  # type: ignore[assignment]
    try:
        hint = await _where_the_pictures_are(FakeScope(), "doc", "show me the charts")
        assert str(MAX_NAMED_PICTURE_PAGES) in hint or "more" in hint
        assert "30 more" not in hint
        assert hint.count(",") < 30
    finally:
        del figures.scan


# --------------------------- looking at a figure ---------------------------


def test_a_box_measured_on_a_crop_is_moved_back_onto_the_page():
    """The model measures against the picture it was handed.

    The reader is shown the whole PAGE with the boxes drawn on it, so without
    this every highlight lands in the wrong place — and a box round the wrong
    thing is worse than no box, which is the rule the region feature is built
    on.
    """
    from packages.core.navigator import _regions_onto_the_page

    # The crop covers x 10-90 and y 15-75 of the page.
    moved = _regions_onto_the_page(
        "Report.\nREGION x=10 y=20 w=30 h=40 | the gateway box",
        (10.0, 15.0, 90.0, 75.0),
    )
    assert "x=18.00" in moved  # 10 + 10% of 80
    assert "y=27.00" in moved  # 15 + 20% of 60
    assert "w=24.00" in moved  # 30% of 80
    assert "h=24.00" in moved  # 40% of 60
    assert "the gateway box" in moved


def test_a_degenerate_crop_leaves_the_boxes_alone():
    from packages.core.navigator import _regions_onto_the_page

    text = "REGION x=10 y=20 w=30 h=40 | thing"
    assert _regions_onto_the_page(text, (50.0, 50.0, 50.0, 50.0)) == text


@pytest.mark.asyncio
async def test_a_page_with_two_figures_is_looked_at_whole():
    """There is no way to know WHICH one the question is about without asking
    the model — which is the call being made cheaper."""
    from packages.core.navigator import _crop_to_the_figure

    async def two(_ws, _item, _page):
        return [
            {"x": 10, "y": 10, "w": 30, "h": 20, "kind": "image", "caption": ""},
            {"x": 55, "y": 10, "w": 30, "h": 20, "kind": "image", "caption": ""},
        ]

    figures.for_page = two  # type: ignore[assignment]
    try:
        png, box = await _crop_to_the_figure("ws", "item", 1, b"png-bytes")
        assert box is None and png == b"png-bytes"
    finally:
        del figures.for_page


@pytest.mark.asyncio
async def test_a_failure_falls_back_to_the_whole_page():
    """A page is always a correct thing to look at. An optimisation that can
    fail the request is not one."""
    from packages.core.navigator import _crop_to_the_figure

    async def boom(_ws, _item, _page):
        raise RuntimeError("no object store")

    figures.for_page = boom  # type: ignore[assignment]
    try:
        png, box = await _crop_to_the_figure("ws", "item", 1, b"png-bytes")
        assert box is None and png == b"png-bytes"
    finally:
        del figures.for_page


@pytest.mark.asyncio
async def test_a_figure_that_fills_the_page_is_not_cropped():
    # A crop that is nearly the whole page is not a crop: it pays the
    # conversion for nothing and loses the reader's frame.
    from packages.core.navigator import _crop_to_the_figure

    async def huge(_ws, _item, _page):
        return [{"x": 2, "y": 2, "w": 95, "h": 95, "kind": "image", "caption": ""}]

    figures.for_page = huge  # type: ignore[assignment]
    try:
        _png, box = await _crop_to_the_figure("ws", "item", 1, b"png-bytes")
        assert box is None
    finally:
        del figures.for_page


@pytest.mark.asyncio
async def test_a_lone_figure_is_cropped_with_a_margin_for_its_caption():
    """A figure lifted out with a tight box loses the one line that says what
    it is."""
    from PIL import Image

    from packages.core.navigator import FIGURE_MARGIN_PCT, _crop_to_the_figure

    page = Image.new("RGB", (800, 1000), "white")
    buffer = io.BytesIO()
    page.save(buffer, format="PNG")

    async def one(_ws, _item, _page):
        return [{"x": 30, "y": 30, "w": 30, "h": 30, "kind": "drawing", "caption": ""}]

    figures.for_page = one  # type: ignore[assignment]
    try:
        png, box = await _crop_to_the_figure("ws", "item", 1, buffer.getvalue())
        assert box is not None
        left, top, right, bottom = box
        assert left == 30 - FIGURE_MARGIN_PCT
        assert bottom == 60 + FIGURE_MARGIN_PCT
        with Image.open(io.BytesIO(png)) as out:
            assert out.size[0] < 800  # it really was cut down
    finally:
        del figures.for_page


@pytest.mark.asyncio
async def test_the_hint_names_pictures_near_what_was_read():
    """Eight arbitrary pages out of 274 is not a hint.

    This is the bug the first version shipped with. The scan stopped at page 40,
    so on a 962-page textbook it found the only three picture pages in the front
    matter, the hint named 28, 29 and 31, and the walk went and looked at them
    for a diagram that lives on page 300 — then reported the diagram was not in
    the document. A hint that names the wrong pages is worse than no hint,
    because it gets followed.
    """
    from packages.core.navigator import _where_the_pictures_are

    class FakeScope:
        workspace_id = "ws"

    async def spread(_workspace, _item, *_a, **_k):
        return [28, 29, 31, 273, 274, 287, 288, 292, 293, 294, 296, 600]

    figures.scan = spread  # type: ignore[assignment]
    try:
        hint = await _where_the_pictures_are(FakeScope(), "doc", "show me the diagram", [286])
        assert "287" in hint and "288" in hint
        assert "28," not in hint and "600" not in hint
    finally:
        del figures.scan


@pytest.mark.asyncio
async def test_pictures_nowhere_near_the_reading_are_not_mentioned():
    from packages.core.navigator import _where_the_pictures_are

    class FakeScope:
        workspace_id = "ws"

    async def far_away(_workspace, _item, *_a, **_k):
        return [900, 901, 902]

    figures.scan = far_away  # type: ignore[assignment]
    try:
        assert await _where_the_pictures_are(FakeScope(), "doc", "show the chart", [50]) == ""
    finally:
        del figures.scan


def test_the_scan_reaches_a_whole_book():
    """The bound exists to stop something pathological, not to ration a cost
    that turns out to be small: a 962-page book scans in 3.3 seconds, once, and
    is cached from then on."""
    assert figures.SCAN_PAGES >= 1000


def test_the_cache_key_carries_the_format():
    """Thresholds move as documents that break them turn up.

    A page picture is a render and never changes; a figure is a judgement.
    Without the stamp a store would answer forever with rules that were
    replaced, and there is nowhere else to invalidate them from.
    """
    key = figures.figures_key("ws", "item", 3)
    assert f"/v{figures.FORMAT}/" in key
    assert key.startswith("ws/item/")
    assert key.endswith("/3.json")
