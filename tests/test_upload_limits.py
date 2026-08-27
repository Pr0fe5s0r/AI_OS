"""What happens when an upload is too big, or staging is unavailable.

Written after a 17MB PDF came back as "Internal Server Error" with nothing else
— no filename, no cause, nothing anyone could act on. Chasing it turned up three
faults on the same path, all of them on the error path, which is exactly why
none had been noticed:

  1. ``log`` was never defined in the module, and TWO handlers called
     ``log.exception``. The instant either fired, the handler itself raised
     NameError and a considered 500-with-a-reason became a bare 500.
  2. ``blobs.put`` sat outside any try. A full or unreachable blob store raised
     straight through the route.
  3. With blob storage off, a file of any size was enqueued inline — Redis is a
     message broker, and a multi-megabyte job payload fails somewhere deep
     inside the queue client where the message helps nobody.

There was also no upper bound at all: the whole file is in memory before the
first check, so a big enough upload takes the container with it.
"""

from __future__ import annotations

import inspect

import pytest

from apps.api import main


def test_the_module_has_the_logger_its_handlers_use():
    """The bug that turned every handled failure into an unhandled one."""
    assert hasattr(main, "log"), "handlers call log.exception; it must exist"

    source = inspect.getsource(main)
    for handler in ("Staging upload failed", "Failed to enqueue ingest_file job"):
        assert handler in source


def test_an_upload_has_an_upper_bound():
    """The file is fully in memory before anything is checked, so this is a
    memory bound per in-flight request rather than a policy. Several replicas
    each holding a large upload is how a container gets OOM-killed mid-request,
    which the caller sees as the connection simply dropping."""
    assert main.MAX_UPLOAD_BYTES >= 32 * 1024 * 1024, "must still clear a scanned book"
    assert main.MAX_UPLOAD_BYTES <= 512 * 1024 * 1024, "unbounded is not a limit"
    # The 17MB PDF that started this must be comfortably inside it.
    assert main.MAX_UPLOAD_BYTES > 17_338_552


def test_big_files_are_staged_rather_than_queued():
    assert main.INLINE_UPLOAD_LIMIT <= 4 * 1024 * 1024
    assert main.INLINE_UPLOAD_LIMIT < main.MAX_UPLOAD_BYTES


@pytest.mark.parametrize(
    "needle",
    [
        # Refuses instead of enqueuing a payload Redis cannot carry, and says
        # which knob fixes it.
        "not configured on this deployment",
        # A staging failure names the file and the cause.
        "Could not stage",
        # The size refusal reports the actual size, not just "too large".
        "over the",
    ],
)
def test_each_failure_says_what_to_do_about_it(needle):
    """"Internal Server Error" is not a diagnosis. Every refusal on this path
    names the file, the size or the missing configuration."""
    assert needle in inspect.getsource(main.ingest_file_item)


def test_staging_cannot_raise_through_the_route():
    """``blobs.put`` used to sit outside any try, so a blob store that was
    unreachable produced a bare 500 with no body worth reading."""
    source = inspect.getsource(main.ingest_file_item)
    staging = source[source.index("blobs.put") - 400 : source.index("blobs.put") + 200]
    assert "try:" in staging, "the staging write must be guarded"
