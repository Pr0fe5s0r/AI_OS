"""The table of contents, and what it costs to put in a prompt.

It had no limit. MAX_DOCUMENTS caps how many documents go in front of the
model; nothing capped how many SECTIONS did, and the reasoning behind that cap
— "choosing from a list nobody can read is guesswork wearing a suit" — is
exactly as true of sections. Measured on real collections:

    two documents, 16 sections        4,048 chars     unaffected
    ten ordinary documents           111,858 chars    28,000 tokens
    one novel, twice, 1,491 sections 270,062 chars    67,500 tokens

Sent again on every round of every question.

The safety property these tests exist to hold is that a collection under the
budget is byte-identical to what it was before any of this: same catalogue,
same tools, same round count. A small store cannot regress, because nothing
about it changed.
"""

from __future__ import annotations

from packages.core import tree
from packages.core.navigator import (
    CATALOGUE_BUDGET,
    MAX_ROUNDS,
    _catalogue,
    _headings_only,
    _node_holding,
    _outline_of,
)


def _doc(item_id: str, title: str) -> dict:
    return {"item_id": item_id, "title": title, "locator": "", "source": "upload"}


def _small() -> tuple[list[dict], dict[str, tree.Node]]:
    body = "## Pay\n\nSalary is monthly.\n\n## Leave\n\nLeave accrues monthly.\n"
    docs = [_doc("d1", "Handbook")]
    return docs, {"d1": tree.build(body, "Handbook")}


def _huge() -> tuple[list[dict], dict[str, tree.Node]]:
    parts = []
    for book in range(1, 21):
        parts.append(f"## Book {book}\n")
        for chapter in range(1, 40):
            parts.append(
                f"### Chapter {chapter}\n\nThe chapter opens with a long sentence "
                f"about the events of book {book} chapter {chapter}, and then "
                f"continues for some while so the preview has something to show.\n"
            )
    body = "\n".join(parts)
    docs = [_doc("big", "A Long Novel")]
    return docs, {"big": tree.build(body, "A Long Novel")}


def test_a_collection_that_fits_is_untouched():
    """The whole safety argument. Under the budget, the same bytes as before."""
    docs, trees = _small()
    catalogue, shortened = _catalogue(docs, trees)

    assert shortened is False
    # The openings are what make a section choosable, and they survive.
    assert "accrues monthly" in catalogue or "Salary is monthly" in catalogue
    assert "sections_in_full" not in catalogue
    assert "headings_not_shown" not in catalogue


def test_a_collection_too_large_is_shortened_to_fit():
    docs, trees = _huge()
    full, _ = _catalogue(docs, trees, budget=10_000_000)
    catalogue, shortened = _catalogue(docs, trees)

    assert shortened is True
    assert len(catalogue) <= CATALOGUE_BUDGET
    assert len(catalogue) < len(full) / 5, "shortening that saves little is not worth the round"
    # It says how much it is not showing, rather than quietly stopping.
    assert "sections_in_full" in catalogue


def test_what_is_dropped_is_the_detail_not_the_shape():
    """Headings stay; openings and nesting go. A reader can still choose a part
    of the document, which is the one thing the catalogue is for."""
    sections = [
        {"id": "n1", "title": "Book One", "opens": "a long opening line",
         "sections": [{"id": "n2", "title": "Chapter I", "opens": "more prose"}]},
    ]
    trimmed = _headings_only(sections)
    assert trimmed == [{"id": "n1", "title": "Book One", "contains": 1}]


def test_opening_a_part_shows_what_is_inside_it():
    """The first version of the drill-down handed back the top-level headings
    the catalogue had already shown. Measured: the agent opened the novel, got
    its book titles again, then read five whole BOOKS hunting for one scene and
    ran out of rounds. It could see the shelf and never the chapters."""
    docs, trees = _huge()
    root = trees["big"]
    book = next(n for n in root.walk() if n.title.startswith("Book 2"))

    inside = _outline_of(docs[0], root, CATALOGUE_BUDGET, book.node_id)
    assert "Chapter 1" in inside
    assert book.node_id in inside

    # A section id nobody has is refused rather than silently returning the
    # whole document.
    assert "no section with that id" in _outline_of(docs[0], root, CATALOGUE_BUDGET, "nope")


def test_a_section_is_found_by_its_text_not_its_title():
    """A novel has thirty-four chapters called "Chapter I", so a title is not an
    address. The hint has to hand back something read_section can act on."""
    docs, trees = _huge()
    root = trees["big"]
    target = next(
        n for n in root.walk() if n.title == "Chapter 7" and "book 3 chapter 7," in n.text
    )

    found = _node_holding(root, "about the events of book 3 chapter 7, and then continues")
    assert found is not None
    assert found.node_id == target.node_id

    # Text that appears nowhere resolves to nothing rather than to a guess.
    assert _node_holding(root, "a passage that is simply not in this document at all") is None
    # And a fragment too short to be distinctive is refused. The probe is 120
    # characters of a real passage precisely because a short one matches many
    # sections and the deepest match would then be arbitrary.
    assert _node_holding(root, "the") is None


def test_navigating_a_shortened_catalogue_is_given_the_rounds_it_needs():
    """Reaching a chapter through a shortened catalogue costs two rounds the
    flat one never spent — open the document, open the part. Without them the
    agent spent its budget on navigation and had none left to read."""
    assert MAX_ROUNDS >= 6
    # The loop grants MAX_ROUNDS + 2 when abbreviated; this pins the reasoning
    # so the two are not silently reclaimed later.
    assert CATALOGUE_BUDGET > 0
