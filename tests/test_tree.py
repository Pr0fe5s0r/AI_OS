from __future__ import annotations

from packages.core import tree
from packages.core.vectorless import _parse, _resolve

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


def test_a_document_with_no_headings_becomes_one_section():
    """Inventing sections the author did not write would be worse than having
    none: the model would reason over a structure that is not real."""
    root = tree.build("Just a wall of text with no structure at all.")
    assert root.children == []
    assert "wall of text" in root.text


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


# ------------------------- reading the model's choice -------------------------


def test_every_reply_shape_the_model_actually_produces_is_accepted():
    """All three of these came back from the same model on the same prompt at
    temperature zero. Only the ids are a contract; the container is not, and a
    parser accepting one shape reported 'nothing answers that' while the model
    was naming the right section."""
    documented = '{"sections": [{"doc": "d1", "id": "n016", "why": "x"}]}'
    bare_list = '{"sections": ["n016"]}'
    single = '{"section": "n016", "title": "KB-4", "contains": "connectors"}'
    for raw in (documented, bare_list, single):
        parsed = _parse(raw)
        assert [p.node_id for p in parsed] == ["n016"], raw


def test_a_fenced_reply_is_unwrapped():
    assert _parse('```json\n{"sections": ["n001"]}\n```')[0].node_id == "n001"


def test_an_unreadable_reply_selects_nothing():
    """Guessing at sections from a broken reply would be inventing a retrieval
    nobody performed."""
    assert _parse("I think section 4 looks good") == []
    assert _parse("") == []


def test_a_bare_id_is_attached_to_the_only_document_that_owns_it():
    root = tree.build(_DOC, "Handbook")
    resolved = _resolve(_parse('{"sections": ["n002"]}'), {"doc-a": root})
    assert resolved and resolved[0].item_id == "doc-a"


def test_an_ambiguous_bare_id_is_dropped_not_guessed():
    """Section ids restart at n000 in every document. Attributing a passage to
    the wrong document would put a citation under a title it never came from."""
    both = {"doc-a": tree.build(_DOC, "A"), "doc-b": tree.build(_DOC, "B")}
    assert _resolve(_parse('{"sections": ["n002"]}'), both) == []
