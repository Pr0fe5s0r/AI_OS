from __future__ import annotations

from apps.common.analysis import _captured_url
from packages.core.watchers import _subject

# The cross-app chain (a Slack incident opens a GitHub issue, then gets assigned
# ON that new issue) hinges on reading the created record's link back out of the
# action's stored — and length-capped — response body. And the "every card names
# the thing it is about" fix hinges on folding a record's own subject into an
# otherwise generic watcher title. Both are small, exact, and easy to regress,
# so they get pinned here.


def test_captured_url_reads_the_field_from_json():
    body = '{"number": 24, "html_url": "https://github.com/o/r/issues/24"}'
    assert _captured_url(body, "html_url") == "https://github.com/o/r/issues/24"


def test_captured_url_survives_a_truncated_body():
    # a real GitHub issue response is long and gets cut mid-JSON by the store's
    # cap — json.loads fails, but the field itself already landed, so the text
    # fallback must still recover it
    body = '{"url": "https://api/x", "html_url": "https://github.com/o/r/issues/24", "labels": [{"name": "bu'
    assert _captured_url(body, "html_url") == "https://github.com/o/r/issues/24"


def test_captured_url_is_none_when_field_absent():
    assert _captured_url('{"number": 24}', "html_url") is None
    assert _captured_url(None, "html_url") is None
    assert _captured_url('{"html_url": "x"}', "") is None


def test_subject_folds_a_record_name_into_a_generic_title():
    # the six-identical-cards bug: distinct records must yield distinct titles
    assert _subject("API is not working", "id-1") == "API is not working"
    assert _subject("", "graph:42") == "graph:42"          # falls back to id
    assert _subject(None, "graph:42") == "graph:42"


def test_subject_is_trimmed_to_one_short_line():
    long = "x" * 200
    out = _subject(long, "id")
    assert len(out) <= 60 and out.endswith("…")
    assert _subject("first line\nsecond line", "id") == "first line"
