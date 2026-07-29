from __future__ import annotations

from packages.core.chunk import DEFAULT_OVERLAP, DEFAULT_SIZE, embedding_text, split

# A document is not one idea and must not be one vector. These pin the
# properties that make a passage usable on its own — because a passage that
# cannot be read alone cannot be embedded alone either.


def test_a_short_document_is_one_passage():
    passages = split("# Title\n\nA single short paragraph.")
    assert len(passages) == 1
    assert "single short paragraph" in passages[0].text


def test_empty_input_produces_nothing():
    assert split("") == []
    assert split("   \n\n  ") == []


def test_passages_carry_their_heading_path():
    """A passage reading "must complete within 200ms" is unattributable on its
    own. The heading path is what makes it a citation rather than a fragment."""
    doc = "# Spec\n\n## 3. Retrieval\n\n### 3.2 Ranking\n\n" + ("Ranking detail. " * 40)
    passages = split(doc)
    assert any("3. Retrieval > 3.2 Ranking" in p.heading for p in passages)


def test_a_heading_is_never_stranded_from_its_content():
    """A passage ending on a bare heading is a title with nothing under it,
    while the text it introduces opens the next passage with no idea what it
    belongs to. This was a real defect: one passage was 170 characters and
    ended on '### Required source types'."""
    doc = ""
    for section in range(6):
        doc += f"\n\n## Section {section}\n\n" + ("Body text for this section. " * 20)
    for passage in split(doc):
        last_line = passage.text.strip().splitlines()[-1].strip()
        assert not last_line.startswith("#"), f"passage ends on a bare heading: {last_line!r}"


def test_passages_stay_near_the_target_size():
    doc = "\n\n".join(
        f"Paragraph number {i} with a reasonable amount of text in it. " * 3
        for i in range(60)
    )
    passages = split(doc)
    assert len(passages) > 1
    # The overlap is prepended, so the ceiling is size + overlap, not size.
    assert max(len(p.text) for p in passages) <= DEFAULT_SIZE + DEFAULT_OVERLAP + 200


def test_a_block_larger_than_the_target_is_split_not_dropped():
    """A wide table row or a wall of text with no paragraph breaks still has to
    fit. Losing it silently would be far worse than splitting it awkwardly."""
    giant = "word " * 2000
    passages = split(giant)
    assert len(passages) > 1
    assert sum(len(p.text) for p in passages) >= len(giant.strip()) * 0.9


def test_a_word_is_never_cut_in_half():
    passages = split("supercalifragilistic " * 400)
    for passage in passages:
        assert "supercalifragilisti " not in passage.text
        assert not passage.text.endswith("supercalifragilisti")


def test_overlap_keeps_a_straddling_sentence_whole_somewhere():
    """A fact stated across a boundary must be complete in at least one
    passage, or it can never be retrieved."""
    filler = "Padding sentence to push the boundary along. " * 22
    doc = filler + "\n\nThe retention period is exactly ninety days.\n\n" + filler
    passages = split(doc)
    assert any("retention period is exactly ninety days" in p.text for p in passages)


def test_ordinals_are_sequential_from_zero():
    passages = split("\n\n".join(f"Paragraph {i}. " * 30 for i in range(20)))
    assert [p.ordinal for p in passages] == list(range(len(passages)))


def test_no_passage_is_empty_or_whitespace():
    doc = "# A\n\n\n\n## B\n\n\n\ntext\n\n\n\n### C\n\n" + ("more text " * 100)
    assert all(p.text.strip() for p in split(doc))


def test_what_is_embedded_carries_the_context_a_passage_lacks():
    """Without the title and heading, "This must complete within 200ms" gives
    the model nothing to attach the requirement to."""
    text = embedding_text("4. Requirements > KB-5", "Must complete within 200ms.", "KB Spec")
    assert "KB Spec" in text and "KB-5" in text and "200ms" in text


def test_splitting_is_deterministic():
    """The same document must produce the same passages every time, or ids
    churn and every re-index looks like new content."""
    doc = "\n\n".join(f"## Section {i}\n\nContent for section {i}. " * 12 for i in range(10))
    first, second = split(doc), split(doc)
    assert [(p.ordinal, p.text, p.heading) for p in first] == [
        (p.ordinal, p.text, p.heading) for p in second
    ]
