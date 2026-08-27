from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# OFFICE DOCUMENTS -> PDF, so a slide can be LOOKED AT.
#
# A citation from slide 34 should be able to show slide 34. Nothing in this
# codebase can draw one: a .pptx is a description of shapes, text boxes,
# themes, masters and fonts, and turning that back into the picture a person
# saw is the entire job of a presentation program. Extracting the images
# embedded in the file is not the same thing — a slide whose diagram is drawn
# with PowerPoint shapes has no image file to find, and the pictures that are
# there arrive without the labels around them.
#
# So the conversion is delegated to LibreOffice, once, at ingest. Everything
# downstream is unchanged: the PDF is stored beside the original, and the page
# renderer, the figure detector and the crop logic all keep working on a PDF
# exactly as they always have. "Page 34" then means the same thing whatever
# the document started as, which is the property an API owes the applications
# built on it — otherwise every consumer writes its own per-format special
# case against us.
#
# Three things this is careful about, each learned from how soffice behaves
# rather than from how it is documented:
#
#   ITS OWN PROFILE PER RUN. LibreOffice keeps a user profile and takes a lock
#   on it. Two conversions sharing one profile do not queue — the second exits
#   silently having produced nothing. Each run gets a private profile
#   directory, which is also why this cannot simply be a long-lived daemon.
#
#   A DEADLINE, ENFORCED BY KILLING IT. soffice can hang on a malformed file
#   with no output and no exit. A timeout that only stops waiting leaves the
#   process holding CPU and its profile directory forever, so the process
#   group is killed rather than abandoned.
#
#   FAILURE IS NOT FATAL. A document that cannot be converted is still fully
#   indexed and searchable; it simply has no picture to offer, and says so.
#   Losing the whole ingest because a renderer fell over would be trading the
#   answer for the illustration.
# ---------------------------------------------------------------------------

# Formats worth converting: the ones with a fixed visual layout a reader would
# recognise. A CSV has no layout to preserve, and a .txt has nothing to draw.
CONVERTIBLE = (".pptx", ".ppt", ".docx", ".doc", ".xlsx", ".xlsm", ".xls", ".odp", ".odt", ".ods")

# A long deck is minutes of work, and this runs in a worker where nothing is
# waiting on it. Long enough for a 200-slide deck, short enough that a hung
# conversion is noticed the same day.
TIMEOUT_SECONDS = int(os.getenv("RENDER_TIMEOUT_SECONDS", "300"))

# Files past this are not converted. Rendering is a convenience; a 400 MB deck
# would spend minutes of worker time to produce a picture nobody asked for.
MAX_BYTES = int(os.getenv("RENDER_MAX_MB", "150")) * 1024 * 1024


def key_for(workspace_id: str, item_id: str) -> str:
    """Where the converted PDF lives — UNDER the item's own prefix, so deleting
    the document sweeps the render with everything else it owns."""
    return f"{workspace_id}/{item_id}/render.pdf"


def convertible(filename: str) -> bool:
    return filename.lower().endswith(CONVERTIBLE)


def _binary() -> str | None:
    return shutil.which("soffice") or shutil.which("libreoffice")


def available() -> bool:
    """Whether this deployment can convert at all.

    Checked rather than assumed: the image may be built without LibreOffice,
    and the honest behaviour then is that Office documents simply have no page
    pictures — the same as before this existed — instead of every ingest
    logging a failure.
    """
    return _binary() is not None


async def to_pdf(data: bytes, filename: str) -> bytes | None:
    """Convert one Office document to PDF. None when it cannot be done.

    None is a normal outcome, not an error: no LibreOffice installed, a file
    too large to be worth it, a format it cannot read, or a conversion that
    produced nothing. Every one of them means the same thing to the caller —
    this document has no picture to offer — and none of them should cost the
    caller its document.
    """
    binary = _binary()
    if binary is None or not convertible(filename) or len(data) > MAX_BYTES:
        return None

    suffix = Path(filename).suffix or ".bin"
    with tempfile.TemporaryDirectory(prefix="render-") as workdir:
        source = Path(workdir) / f"input{suffix}"
        source.write_bytes(data)
        profile = Path(workdir) / "profile"

        process = await asyncio.create_subprocess_exec(
            binary,
            # Its own profile, or a second conversion silently produces nothing.
            f"-env:UserInstallation=file://{profile.as_posix()}",
            "--headless",
            "--norestore",
            # No first-run dialogs, no crash-recovery prompt, no lock files
            # inherited from a previous run that died.
            "--nolockcheck",
            "--nodefault",
            "--nofirststartwizard",
            "--convert-to",
            "pdf",
            "--outdir",
            workdir,
            str(source),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=workdir,
        )
        try:
            _out, err = await asyncio.wait_for(process.communicate(), TIMEOUT_SECONDS)
        except TimeoutError:
            # Killed, not merely abandoned: a soffice left running holds CPU
            # and its profile directory for as long as the worker lives.
            process.kill()
            await process.wait()
            log.warning("render timed out after %ss: %s", TIMEOUT_SECONDS, filename)
            return None

        produced = source.with_suffix(".pdf")
        if not produced.exists():
            log.warning(
                "render produced nothing for %s: %s",
                filename,
                (err or b"").decode("utf-8", "replace")[:300],
            )
            return None
        return produced.read_bytes()


__all__ = [
    "CONVERTIBLE",
    "MAX_BYTES",
    "TIMEOUT_SECONDS",
    "available",
    "convertible",
    "key_for",
    "to_pdf",
]
