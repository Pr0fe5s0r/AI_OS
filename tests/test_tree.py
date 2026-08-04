from __future__ import annotations

from packages.core import tree

# The tree index is the whole of "vectorless": if the structure is wrong, the
# model is reasoning over a map of a document that does not exist. Building it
# makes no model calls, so all of this is exact rather than approximate.


_DOC = """# Handbook

Introductory text before any heading.

## 1. Leave

Staff accrue leave monthly.

### 1.1 Carry-over

Up to five days may be carried over.

## 2. Expenses

Receipts within thirty days.
"""


def test_the_structure_matches_the_document():
    root = tree.build(_DOC, "Handbook")
    # The document's own "# Handbook" is the single top-level section; the
    # numbered sections nest under it, exactly as the headings say.
    assert [n.title for n in root.children] == ["Handbook"]
    assert [n.title for n in root.children[0].children] == ["1. Leave", "2. Expenses"]


def test_subsections_hang_off_their_parent():
    root = tree.build(_DOC, "Handbook")
    leave = next(n for n in root.walk() if n.title == "1. Leave")
    assert [c.title for c in leave.children] == ["1.1 Carry-over"]


def test_text_before_the_first_heading_is_kept():
    """Usually a title block or an abstract — the one place a document says
    what it is. Dropping it loses that."""
    root = tree.build("Preamble prose with no heading above it.\n\n# 1. Body\n\ntext")
    opening = next(n for n in root.walk() if n.title == "(opening)")
    assert "Preamble prose" in opening.text


def test_a_pdf_with_no_headings_is_sectioned_by_its_pages():
    """This was the bug that made vectorless look like an empty store. A PDF
    converted to text has page markers, not headings, so it produced ZERO
    sections — and a retrieval that works by choosing sections cannot see a
    document that has none. Pages are real structure, and "page 2" is a
    citation someone can check against the original file."""
    pdf = "\n\n---\n\n".join(
        [
            "<!-- page 1 -->\nOpening remarks about the subject.",
            "<!-- page 2 -->\nThe substance is on the second page.",
            "<!-- page 3 -->\nClosing remarks.",
        ]
    )
    root = tree.build(pdf, "Scanned Report")
    assert [n.title for n in root.children] == ["Page 1", "Page 2", "Page 3"]
    page_two = next(n for n in root.walk() if n.title == "Page 2")
    assert "substance is on the second page" in page_two.text


def test_flat_text_with_neither_headings_nor_pages_is_still_readable():
    """A document nothing can open is a document nothing can answer from."""
    flat = "Sentence of ordinary prose with no structure at all. " * 200
    root = tree.build(flat, "Notes")
    assert root.children, "must expose something readable"
    assert all(c.title.startswith("Part ") for c in root.children)
    # Every word survives somewhere — sectioning must not lose content.
    joined = " ".join(c.text for c in root.children)
    assert len(joined) >= len(flat.strip()) * 0.95


def test_a_short_unstructured_note_is_one_whole_section():
    root = tree.build("Just a short note with no structure at all.", "Note")
    assert [n.title for n in root.children] == ["(whole document)"]
    assert "short note" in root.children[0].text


def test_headings_inside_code_blocks_are_not_sections():
    """A '# ' in a fenced block is a comment, not a heading."""
    doc = "# Real\n\ntext\n\n```bash\n# not a heading\n```\n\n## Also real\n\nmore"
    titles = [n.title for n in tree.build(doc).walk()]
    assert "not a heading" not in titles
    assert "Also real" in titles


def test_a_skipped_heading_level_still_attaches():
    """Badly nested headings are extremely common and the content is real."""
    root = tree.build("# One\n\na\n\n### Three\n\nb")
    assert len(root.walk()) == 3  # root + two sections, nothing dropped


def test_the_outline_never_carries_a_whole_long_section():
    """Choosing what to read BEFORE reading it only works if the outline stays
    an outline. A short section's preview IS all of it, which is harmless; a
    long one must be cut, or the outline becomes the document."""
    long_section = "# A\n\n" + ("Sentence of real substance here. " * 60)
    root = tree.build(long_section)
    outline = tree.outline_json(root)
    assert len(outline) < len(long_section) / 2
    node = root.children[0]
    assert len(node.preview()) <= tree.PREVIEW_CHARS + 1  # +1 for the ellipsis
    assert node.preview().endswith("…")


def test_the_outline_previews_what_a_section_opens_with():
    """A title alone was not enough: a model picked a section about chat
    sessions for a question about connectors, on a shared word. The opening
    line disambiguates, and costs no model call."""
    root = tree.build(_DOC, "Handbook")
    leave = next(n for n in root.walk() if n.title == "1. Leave")
    assert "accrue leave monthly" in leave.preview()
    assert "1. Leave" not in leave.preview()  # its own heading is not content


def test_asking_for_a_parent_returns_its_subsections_too():
    """A parent heading usually has little text of its own — the substance is
    underneath. Returning only its own line would answer from a title."""
    root = tree.build(_DOC, "Handbook")
    leave = next(n for n in root.walk() if n.title == "1. Leave")
    body = tree.section_text(leave, include_children=True)
    assert "accrue leave monthly" in body
    assert "five days may be carried over" in body


def test_an_invented_section_id_resolves_to_nothing():
    """A section that does not exist cannot be evidence."""
    root = tree.build(_DOC, "Handbook")
    assert tree.find(root, ["n999"]) == []


def test_building_is_deterministic():
    """No model call, so the index cannot drift between runs — which is what
    makes a retrieval over it reproducible."""
    first, second = tree.build(_DOC, "H"), tree.build(_DOC, "H")
    assert tree.outline_json(first) == tree.outline_json(second)


def test_a_section_knows_which_page_it_starts_on():
    """A citation can only show the page it came off if the section knows which
    page that is. Both PDF paths — extracted text and a scan read with vision —
    write the same page markers, so one rule covers both."""
    from packages.core import tree

    body = (
        "<!-- page 1 -->\n# Report\n\nOpening remarks.\n\n"
        "---\n\n<!-- page 2 -->\n## Revenue\n\nThe table lives here.\n\n"
        "---\n\n<!-- page 3 -->\n## Outlook\n\nGuidance is unchanged.\n"
    )
    root = tree.build(body, "Report")

    def every(node):
        yield node
        for child in node.children:
            yield from every(child)

    by_title = {n.title: n for n in every(root)}

    assert tree.page_of(body, by_title["Revenue"]) == 2
    assert tree.page_of(body, by_title["Outlook"]) == 3


def test_a_document_with_no_pages_reports_none():
    """Pasted text and Markdown have no page a picture could be OF. None means
    the citation shows text only, rather than an empty frame implying something
    failed to load."""
    from packages.core import tree

    body = "# Notes\n\n## Detail\n\nSomething written directly into the store.\n"
    root = tree.build(body, "Notes")

    def every(node):
        yield node
        for child in node.children:
            yield from every(child)

    for node in every(root):
        assert tree.page_of(body, node) is None


def test_every_page_of_a_pdf_reports_its_own_page_number():
    """A fifteen-page paper has no Markdown headings, so it indexes as page
    sections — and those all begin at the same nominal line. Deriving the page
    from the line number therefore reported page 1 for all fifteen, which would
    put page 1's picture behind a citation taken from page 7. A citation
    pointing at the wrong page is worse than one pointing at no page."""
    from packages.core import tree

    body = "\n\n---\n\n".join(
        f"<!-- page {n} -->\nText of page {n}, with no heading anywhere in it." for n in range(1, 8)
    )
    root = tree.build(body, "Paper")
    pages = [tree.page_of(body, child) for child in root.children]

    assert pages == [1, 2, 3, 4, 5, 6, 7]
    assert [c.title for c in root.children][:3] == ["Page 1", "Page 2", "Page 3"]


def test_a_section_lists_the_figures_and_tables_it_contains():
    """A caption is the one piece of text a PDF keeps about a picture, and it
    was invisible to the index: page 13 of the paper previewed as "Attention
    Visualizations Input-Input Layer5 It is in this spirit that..." with the
    words "Figure 3" nowhere in it. Asked about Figure 3, the agent had no way
    to find the page and reported that nothing matched — over a document that
    had it."""
    from packages.core import tree

    body = (
        "<!-- page 13 -->\nAttention Visualizations Input-Input Layer5\n"
        "It is in this spirit that a majority of American governments have passed "
        "new laws since 2009 making the registration of voters more difficult.\n"
        "Figure 3: An example of the attention mechanism following long-distance "
        "dependencies in the encoder self-attention at layer 5 of 6.\n"
        "Table 4: The Transformer generalizes well to English constituency parsing.\n"
    )
    root = tree.build(body, "Paper")
    page = root.children[0]

    assert page.captions() == ["Figure 3", "Table 4"]
    assert page.outline()["shows"] == ["Figure 3", "Table 4"]


def test_a_mention_mid_sentence_is_not_a_caption():
    """"...as Table 1 shows" is a reference to a table, not the place it lives.
    Pointing a reader there would send them to the wrong page."""
    from packages.core import tree

    body = "<!-- page 2 -->\nPerformance improved, as Table 1 shows, across every task.\n"
    root = tree.build(body, "Report")
    assert root.children[0].captions() == []


def test_a_section_with_no_figures_says_nothing_about_them():
    from packages.core import tree

    body = "<!-- page 1 -->\nPlain prose with no captions anywhere in it.\n"
    root = tree.build(body, "Report")
    assert "shows" not in root.children[0].outline()
