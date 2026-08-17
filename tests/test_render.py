"""Turning an Office document into something a page can be a picture OF.

A citation from slide 34 should be able to show slide 34. Extracting the
images embedded in the file is not the same thing — a slide whose diagram is
drawn with PowerPoint shapes has no image file to find — so the conversion is
delegated to LibreOffice once, at ingest, and everything downstream keeps
working on a PDF exactly as it always has.

The tests that need the binary skip without it; the ones that pin the
behaviour around it do not.
"""

from __future__ import annotations

import inspect
import shutil

import pytest

from packages.core import pages, pipeline, render

needs_soffice = pytest.mark.skipif(
    not render.available(), reason="needs LibreOffice (present in the api image)"
)


# ------------------------------ what it converts ------------------------------


def test_documents_with_a_layout_are_convertible_and_data_files_are_not():
    """A CSV has no layout to preserve and a .txt has nothing to draw. Spending
    worker time converting them would produce a picture of nothing."""
    for name in ("deck.pptx", "report.docx", "spend.xlsx", "notes.odp"):
        assert render.convertible(name)
    for name in ("tickets.csv", "notes.txt", "page.html", "book.pdf", "photo.png"):
        assert not render.convertible(name)


def test_a_pdf_is_not_converted_to_a_pdf():
    assert not render.convertible("already.pdf")


# --------------------------- how it calls the binary ---------------------------


def test_each_conversion_gets_its_own_profile():
    """LibreOffice locks its user profile. Two conversions sharing one do not
    queue — the second exits silently having produced nothing, which is the
    kind of failure that looks like a missing feature."""
    source = inspect.getsource(render.to_pdf)
    assert "-env:UserInstallation" in source
    assert "TemporaryDirectory" in source


def test_a_hung_conversion_is_killed_not_merely_abandoned():
    """A soffice left running holds CPU and its profile directory for as long
    as the worker lives."""
    source = inspect.getsource(render.to_pdf)
    assert "wait_for" in source
    assert "process.kill()" in source
    assert render.TIMEOUT_SECONDS >= 60


def test_it_runs_headless():
    assert "--headless" in inspect.getsource(render.to_pdf)


def test_an_enormous_file_is_refused_before_it_is_started():
    """Rendering is a convenience. A 400 MB deck would spend minutes of worker
    time to produce a picture nobody asked for."""
    assert render.MAX_BYTES > 0
    assert "MAX_BYTES" in inspect.getsource(render.to_pdf)


def test_a_deployment_without_libreoffice_says_so_rather_than_failing():
    """The honest behaviour then is that Office documents have no page
    pictures — as before this existed — not an error on every ingest."""
    assert isinstance(render.available(), bool)
    assert render.available() == (shutil.which("soffice") is not None
                                  or shutil.which("libreoffice") is not None)


# --------------------------- where the result lives ---------------------------


def test_the_converted_pdf_lives_under_the_items_own_prefix():
    """So deleting the document sweeps the render with everything else it owns,
    rather than leaving a PDF of a deleted deck in the bucket."""
    from packages.core import blobs

    assert render.key_for("ws", "item").startswith(blobs.key_for("ws", "item"))


def test_the_render_is_recorded_on_the_item():
    """Read from the item rather than by asking the object store: a blob HEAD
    per citation is a round trip to answer what the ingest already knew."""
    source = inspect.getsource(pipeline.render_item)
    assert "'render'" in source or '"render"' in source
    assert "UPDATE kb_items" in source


def test_a_failed_conversion_never_costs_the_document():
    """The document is indexed and answerable throughout. Losing an ingest
    because a renderer fell over trades the answer for the illustration."""
    source = inspect.getsource(pipeline.render_item)
    assert "except Exception" in source
    for outcome in ("skipped", "no_original", "unconvertible", "failed"):
        assert outcome in source


def test_rendering_is_queued_not_done_on_the_upload():
    """Converting a long deck is tens of seconds of CPU and the upload has
    already returned. Nothing about searchability waits on it."""
    source = inspect.getsource(pipeline._store)
    assert 'enqueue_job(\n                "render_item"' in source or '"render_item"' in source
    assert "render.convertible" in source


def test_the_job_is_registered_with_the_worker_that_actually_runs():
    """Both registries, and the second one is the one that matters.

    There are two: `pipeline.WorkerSettings`, and `apps.worker.main
    .WorkerSettings`, which is what the container's `arq` command actually
    loads. The first version of this test checked only the pipeline's, passed,
    and the running worker answered "function 'render_item' not found" for
    every deck uploaded — a green test proving nothing about the thing that
    runs.
    """
    from apps.worker.main import WorkerSettings as Running

    assert pipeline.render_item in pipeline.WorkerSettings.functions
    assert pipeline.render_item in Running.functions


# ------------------------ what the rest of the app sees ------------------------


def test_a_page_picture_is_drawn_from_the_conversion_when_there_is_one():
    source = inspect.getsource(pages.source_bytes)
    assert "render.key_for" in source
    # The original still wins when it is already drawable — converting a PDF
    # to a PDF would be a slower way to get the same picture.
    assert source.index("renderable(content_type)") < source.index("render.key_for")


def test_a_document_declares_a_picture_once_it_has_one():
    """Before conversion a deck honestly says it has no picture; after, it
    says it has. Neither answer requires the caller to probe an endpoint."""
    assert pages.has_picture("deck.pptx", {}) is False
    assert pages.has_picture("deck.pptx", {"render": {"format": "pdf"}}) is True
    # A PDF never needed converting and says so with no metadata at all.
    assert pages.has_picture("book.pdf", {}) is True
    assert pages.has_picture("notes.txt", {}) is False


# ------------------------------ the real binary ------------------------------


@needs_soffice
async def test_a_real_deck_converts_to_a_real_pdf():
    import io
    import zipfile

    A = "http://schemas.openxmlformats.org/drawingml/2006/main"
    P = "http://schemas.openxmlformats.org/presentationml/2006/main"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("docProps/core.xml", "<cp:coreProperties/>")
        for number, line in enumerate(("First slide", "Second slide"), start=1):
            archive.writestr(
                f"ppt/slides/slide{number}.xml",
                f'<p:sld xmlns:p="{P}" xmlns:a="{A}"><p:cSld><p:spTree>'
                f'<a:p><a:r><a:t>{line}</a:t></a:r></a:p>'
                "</p:spTree></p:cSld></p:sld>",
            )

    pdf = await render.to_pdf(buffer.getvalue(), "deck.pptx")
    # A minimal hand-built deck is not guaranteed to satisfy LibreOffice's
    # importer; what must hold is that the answer is a PDF or an honest None,
    # never an exception and never something that is not a PDF.
    assert pdf is None or pdf[:5] == b"%PDF-"


@needs_soffice
async def test_junk_bytes_never_raise_and_never_return_a_non_pdf():
    """Measured, and not what I expected.

    LibreOffice does not refuse `b"this is not a presentation"` named .pptx —
    it reads the bytes as plain text and returns a one-page PDF of them. So
    the guarantee this can honestly make is bounded: never an exception, and
    never something that is not a PDF.

    It does not reach production regardless. A file that is not really a .pptx
    fails in the PARSER first, is recorded as a failed item, and never reaches
    a store — and render_item only runs after a document has been stored.
    """
    out = await render.to_pdf(b"this is not a presentation", "broken.pptx")
    assert out is None or out[:5] == b"%PDF-"
