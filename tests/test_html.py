"""Web pages, as the text a reader would see.

The format the client hands over most often after PDF, and the one with the
most ways to index something that is not content. Every test here is a way a
naive HTML-to-text pass goes wrong:

  * a page's minified JavaScript becomes "content", gets embedded, and answers
    questions — a store that cites a webpack bundle is worse than one that
    skipped the page;
  * the navigation menu is indexed on every page of a site, so a search matches
    a hundred pages on their shared header;
  * headings fuse into the sentence after them, and the heading tree that
    structure() reads disappears;
  * a table becomes a run-on sentence, and the rows the client cites back to
    their own clients cannot be cited at all;
  * a JavaScript-rendered page indexes as an empty success.
"""

from __future__ import annotations

import pytest

from packages.core.normalise import UnsupportedFormat, can_parse, normalise, supported

# packages.core.html_page is imported INSIDE the test that needs it, not here.
# It imports from normalise, and normalise imports it back at the bottom to
# register it — so importing the parser module first, before normalise has
# finished loading, is a circular import. The same is true of office.py. Worth
# knowing rather than worth restructuring: the registry has to live with the
# parsers it registers, and every real caller reaches them through normalise.

PAGE = b"""<!doctype html>
<html><head><title>Q3 Retention Review</title>
<style>.banner{color:red}</style>
<script>var tracking = "do not index me";</script>
</head><body>
<nav>Home | Pricing | About | Contact</nav>
<h1>Q3 Retention Review</h1>
<p>Churn fell to 4.2% after the onboarding change.</p>
<h2>By segment</h2>
<table>
  <tr><th>Segment</th><th>Churn</th><th>Owner</th></tr>
  <tr><td>SMB</td><td>6.1%</td><td>Priya</td></tr>
  <tr><td>Enterprise</td><td>1.8%</td><td>Tom</td></tr>
</table>
<ul><li>Onboarding rewrite shipped in July</li><li>Support SLA cut to 2 hours</li></ul>
<footer>Copyright 2026 - all rights reserved</footer>
</body></html>"""


def parse(data: bytes = PAGE, name: str = "review.html"):
    return normalise(data, name)


# ----------------------------- it is registered -----------------------------


def test_html_is_advertised_and_accepted():
    for extension in (".html", ".htm", ".xhtml"):
        assert extension in supported()
    assert can_parse("saved-page.html")
    assert can_parse("REPORT.HTM")


# ------------------------- what must not be indexed -------------------------


def test_script_contents_never_become_content():
    """Minified JavaScript is not prose, but it is text, and a parser that only
    strips TAGS keeps every character of it."""
    assert "do not index me" not in parse().body
    assert "tracking" not in parse().body


def test_stylesheets_never_become_content():
    assert "color:red" not in parse().body
    assert "banner" not in parse().body


def test_site_furniture_is_left_out():
    """Navigation repeats on every page of a site. Indexed, it makes a hundred
    pages equally good matches for a query about any of its words."""
    body = parse().body
    assert "Pricing" not in body
    assert "all rights reserved" not in body


# --------------------------- what must be preserved ---------------------------


def test_headings_survive_as_headings():
    """The heading tree is what structure() reads and what the chunker splits
    on — a page whose headings are flattened into prose has no structure to
    return."""
    body = parse().body
    assert "# Q3 Retention Review" in body
    assert "## By segment" in body


def test_a_heading_does_not_fuse_into_the_text_after_it():
    """HTML has no line breaks of its own; two paragraphs are two elements. A
    parser that only accumulates text produces 'By segmentSMB'."""
    body = parse().body
    assert "segmentSMB" not in body.replace(" ", "")
    assert "\n" in body.split("## By segment")[1][:3]


def test_a_table_keeps_its_columns():
    """The client cites specific rows back to their own clients. A table
    flattened into a sentence has every value and no way to say which column
    any of them came from."""
    body = parse().body
    assert "| Segment | Churn | Owner |" in body
    assert "| SMB | 6.1% | Priya |" in body
    assert "| Enterprise | 1.8% | Tom |" in body


def test_a_ragged_table_is_padded_rather_than_misaligned():
    """A short row must not shift every later cell one column left, which turns
    a correct number into a wrong one under a different heading."""
    ragged = b"""<html><body><h1>T</h1><p>Enough words here to clear the minimum
    length check for a page with content in it.</p><table>
    <tr><th>A</th><th>B</th><th>C</th></tr>
    <tr><td>1</td><td>2</td></tr></table></body></html>"""
    rows = [ln for ln in normalise(ragged, "t.html").body.splitlines() if ln.startswith("|")]
    assert rows[-1].count("|") == rows[0].count("|")


def test_lists_survive_as_lists():
    body = parse().body
    assert "- Onboarding rewrite shipped in July" in body
    assert "- Support SLA cut to 2 hours" in body


def test_image_alt_text_is_kept():
    """On a diagram-heavy page the alt text is frequently the only description
    of the picture that exists in words at all."""
    page = b"""<html><body><h1>Architecture</h1>
    <p>The overall shape of the ingestion pipeline is shown below in detail.</p>
    <img src="x.png" alt="Ingest queue feeding two workers"></body></html>"""
    assert "Ingest queue feeding two workers" in normalise(page, "a.html").body


# ------------------------------- the title -------------------------------


def test_the_title_comes_from_the_title_tag():
    assert parse().title == "Q3 Retention Review"


def test_a_page_with_no_title_tag_falls_back_to_its_first_heading():
    page = b"""<html><body><h1>Incident Review 2026-04-02</h1>
    <p>The outage lasted forty minutes and affected two regions in total.</p>
    </body></html>"""
    assert normalise(page, "x.html").title == "Incident Review 2026-04-02"


# --------------------------- failing visibly (R2.3) ---------------------------


def test_a_javascript_rendered_page_fails_visibly_rather_than_indexing_empty():
    """The single most common way an HTML ingest goes wrong: a 200 KB file that
    indexes as nothing and reports success. The reason names the cause, because
    'no readable text' leaves an operator staring at a file that plainly has
    content when they open it."""
    spa = b'<!doctype html><html><head><title>App</title></head><body><div id="root"></div><script src="/bundle.js"></script></body></html>'
    with pytest.raises(UnsupportedFormat) as raised:
        normalise(spa, "app.html")
    assert "JavaScript" in str(raised.value)


def test_a_page_of_pure_navigation_is_not_a_document():
    nav_only = b"<html><body><nav>Home | Pricing | About</nav><footer>2026</footer></body></html>"
    with pytest.raises(UnsupportedFormat):
        normalise(nav_only, "index.html")


# ------------------------------ real-world mess ------------------------------


def test_broken_markup_still_yields_what_it_can():
    """Real saved pages are malformed far more often than not, and half a page
    indexed beats a page refused over one unclosed tag."""
    broken = b"""<html><body><h1>Half Broken<p>This paragraph never closes and the
    table below is missing its end tag entirely, which is normal for a page
    somebody saved out of a browser.<table><tr><td>A<td>B</body>"""
    body = normalise(broken, "broken.html").body
    assert "Half Broken" in body
    assert "never closes" in body


def test_entities_are_decoded():
    page = b"""<html><body><h1>Pricing &amp; Terms</h1>
    <p>Fees are &pound;40 per seat &mdash; billed monthly, in advance always.</p>
    </body></html>"""
    body = normalise(page, "p.html").body
    assert "&amp;" not in body and "&pound;" not in body
    assert "£40" in body


def test_a_declared_charset_is_honoured():
    """A mojibake body is worse than a refused one: it indexes, it embeds, and
    every quotation mark in every citation is wrong."""
    page = (
        b'<html><head><meta charset="cp1252"><title>Caf\xe9</title></head>'
        b"<body><h1>Caf\xe9 report</h1><p>The caf\xe9 opened in March and has "
        b"traded profitably every month since then.</p></body></html>"
    )
    out = normalise(page, "cafe.html")
    assert "Café" in out.title
    assert "café" in out.body


def test_the_parser_does_not_need_a_third_party_library():
    """Same decision as pypdfium2 over PyMuPDF: a format this common should not
    add a dependency, and html.parser is lenient about broken markup."""
    import ast
    import inspect

    from packages.core import html_page

    # The IMPORTS, not the prose: the module comment names BeautifulSoup and
    # lxml precisely to explain why neither is used, and a substring search
    # would fail on the explanation rather than on a dependency.
    tree = ast.parse(inspect.getsource(html_page))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= {"__future__", "re", "html", "packages"}, imported


# ------------------- a file that breaks the parser, not the format -------------------


def test_a_corrupt_archive_is_a_visible_failure_not_a_vanished_document():
    """Found by uploading a damaged .docx through the console.

    Only UnsupportedFormat and ScannedDocument were caught, so a corrupt zip
    (zlib.error), truncated upload or malformed XML raised straight past the
    handler, killed the job, and left NO ROW AT ALL. The file disappeared and
    the UI showed it stuck on "indexing…" with nothing to explain it — the
    silent skip R2.1 and R2.3 exist to forbid.
    """
    import inspect

    from packages.core import pipeline

    source = inspect.getsource(pipeline.ingest_file)
    assert "except Exception" in source
    # And it lands as a failure through the same path every other failure uses.
    assert source.count("record_failure") >= 3
    assert "incompletely uploaded" in source
    # The type is kept, and QUALIFIED: zlib.error, csv.error and struct.error
    # are all named just "error", so the first version of this reason read
    # "could not be read (error)" — seen in the console, and useless in a bug
    # report.
    assert "type(exc).__module__" in source
