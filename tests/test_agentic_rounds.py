"""Three costs measured on a real agentic walk, and what was done about them.

Traced on a three-document collection (6.4MB, 3,615 sections). The agent read
the RIGHT document each time — that part was already working — but it took four
to five rounds to do it, and every round is a model round trip:

    r0 ranked      2.5s
    r0 opened      7.3s      <- rebuilding trees it had already built
    r1 opened doc  9.8s      <- outline it had already been given
    r2 read Page 1 1.0s      ┐
    r3 read Page 2 1.3s      ┘  one section per round
    r4 answered    9.3s
    TOTAL 31.2s

None of the three fixes changes what the agent may reach: every document is
still laid out, every tool still exists, the graph is untouched. They remove
work that was being repeated, not choices that were being made.
"""

from __future__ import annotations

import time

from packages.core import tree
from packages.core.navigator import _TOOLS, HINT_DEADLINE_SECONDS

_DOC = "\n\n".join(
    ["# Handbook"] + [f"## Section {n}\n\nClause {n} text, at some length." for n in range(1, 40)]
)


def test_a_document_is_parsed_once_not_once_per_question():
    """8.2 seconds of every question went on rebuilding trees from bodies the
    navigator had just read out of Postgres. Parsing is pure and deterministic,
    so the second parse of an unchanged document is pure waste."""
    tree.clear_cache()
    big = _DOC * 40

    cold_start = time.perf_counter()
    first = tree.build_cached("d1", big, "Handbook")
    cold = time.perf_counter() - cold_start

    warm_start = time.perf_counter()
    second = tree.build_cached("d1", big, "Handbook")
    warm = time.perf_counter() - warm_start

    assert first is second, "the same document must not be parsed twice"
    assert warm < cold / 5, f"warm {warm:.4f}s is not meaningfully faster than cold {cold:.4f}s"
    assert tree.count(first) == tree.count(tree.build(big, "Handbook"))


def test_an_edited_document_is_not_served_from_the_cache():
    """Keyed on a hash of the source, because an edited document keeps its id.
    Serving its old tree would answer from text that is no longer there — the
    one failure a cache must never have in a knowledge base."""
    tree.clear_cache()
    before = tree.build_cached("d1", _DOC, "Handbook")
    after = tree.build_cached("d1", _DOC + "\n\n## Section 99\n\nNew clause.", "Handbook")
    assert after is not before
    assert tree.count(after) > tree.count(before)
    # One document, one entry: versions replace each other rather than piling up.
    assert tree.cache_stats()["entries"] == 1


def test_the_cache_is_bounded_and_keeps_what_it_was_just_asked_for():
    """A cache with no ceiling on a store of books is a memory leak. A cache
    that evicts the entry it just built does the work twice and keeps nothing,
    which is worse than having no cache."""
    tree.clear_cache()
    stats = tree.cache_stats()
    assert stats["budget"] > 0

    # Far past the budget, one document at a time.
    big = "x" * 200_000
    for n in range(5):
        tree.build_cached(f"doc{n}", big + str(n), "T")
    assert tree.cache_stats()["chars"] <= tree.cache_stats()["budget"]
    assert tree.cache_stats()["entries"] >= 1
    # The most recent one is still there — a second call must be a hit.
    again = tree.build_cached("doc4", big + "4", "T")
    assert again is tree.build_cached("doc4", big + "4", "T")


def test_read_section_accepts_several_sections_at_once():
    """Pages 3325, 3326 and 3327 were read one per round — three model round
    trips, ~15 seconds, for an answer that needed all three. Batching is in the
    SCHEMA rather than only in the prompt, so it is a capability the model can
    see rather than an instruction it may ignore."""
    read = next(t for t in _TOOLS if t["function"]["name"] == "read_section")
    section = read["function"]["parameters"]["properties"]["section"]

    assert "anyOf" in section, "the schema must permit a list"
    kinds = {branch["type"] for branch in section["anyOf"]}
    assert kinds == {"string", "array"}, (
        "a bare string must still work — models send one anyway, and refusing "
        "it would cost a round to discover"
    )
    assert "several" in read["function"]["description"].lower()


def test_a_batch_cannot_become_a_scan():
    """Making batching free made it shotgun. Measured on the first run after
    the change: asked for the three Deathly Hallows, the agent requested 299
    sections in one call — every id from n3306 to n3604. Only MAX_READS could
    be read, so 293 refusal lines went into the prompt and the walk stopped
    being navigation and became a scan."""
    from packages.core.navigator import MAX_READS, MAX_SECTIONS_PER_CALL

    assert 1 < MAX_SECTIONS_PER_CALL <= MAX_READS, (
        "a batch larger than the read budget can only produce refusals"
    )
    section = next(
        t for t in _TOOLS if t["function"]["name"] == "read_section"
    )["function"]["parameters"]["properties"]["section"]
    assert "do not ask for a range" in section["description"]


def test_the_hint_hands_over_the_call_to_make():
    """The hint named the exact sections holding the answer and the agent still
    spent a round on open_document first — ~10 seconds to fetch an outline whose
    relevant entries were already in the prompt."""
    import inspect

    from packages.core.navigator import _where_the_words_are

    source = inspect.getsource(_where_the_words_are)
    assert "read_section(doc=" in source, "the hint must write out the call"
    assert "do NOT need open_document" in source


def test_the_idle_transaction_ceiling_sits_above_the_answer_deadline():
    """A ceiling below the deadline kills the answers it was meant to protect.

    A walk holds its database session open across every model call it makes —
    measured medians of 35s and 65s, one run at 80s — and for all of that time
    the connection is accurately `idle in transaction`. The first version of
    this setting was 120s, reasoned from how long a QUERY takes rather than how
    long the TRANSACTION is open, and would have severed real answers mid-flight
    with exactly the "network error" it was added to prevent.
    """
    from apps.api.main import ANSWER_DEADLINE_SECONDS
    from packages.core.db import IDLE_TX_TIMEOUT_MS

    assert IDLE_TX_TIMEOUT_MS / 1000 > ANSWER_DEADLINE_SECONDS, (
        "an answer the API is still waiting for must not have its connection pulled"
    )
    # Still bounded — the leak that caused the outage was 12 minutes and growing.
    assert IDLE_TX_TIMEOUT_MS <= 900_000


def test_the_answer_stream_never_goes_silent_long_enough_to_be_cut():
    """A quiet SSE stream is indistinguishable from a dead one.

    One agentic read was measured at 42.5 seconds — a single model call, no
    bytes on the wire for its whole duration. Proxies read that silence as a
    dead connection and close it, and the reader gets "network error" over an
    answer that was being produced perfectly well.

    Proven rather than reasoned: the same question finished in 87.5s straight
    to the API and failed through the dev proxy. The heartbeat is an SSE
    comment, which clients ignore by specification — the browser client here
    looks for a `data:` line and skips any frame without one — so it keeps the
    connection warm without appearing as an event.
    """
    import inspect

    from apps.api.main import HEARTBEAT_SECONDS, answer_stream

    # Under the 60s idle timeout common to proxies, and under the longest gap
    # between real events that has actually been measured.
    assert 0 < HEARTBEAT_SECONDS <= 30
    assert HEARTBEAT_SECONDS < 42.5, "a real gap this long must not go unheartbeaten"

    source = inspect.getsource(answer_stream)
    assert ": keepalive" in source
    # A comment frame, not an event: anything with a `data:` line would reach
    # the client's parser and be JSON.parse'd.
    assert 'yield ": keepalive\\n\\n"' in source


def test_the_hint_is_bounded_but_not_so_tight_it_is_lost():
    """Both failure modes are real and they pull in opposite directions.

    Unbounded, a slow hint became a dead stream — 200 OK, no events, then
    ERR_INCOMPLETE_CHUNKED_ENCODING. Bounded at 8s, the hint simply came back
    EMPTY and said nothing: the embedding alone measures 4.4-5.8s cold, so past
    the deadline the agent got no section ids, fell back to open_document, and a
    walk that should take seconds took 113.

    A silent optimisation that switches itself off is worse than one that is
    merely slow, because nothing in the trace says it happened.
    """
    assert HINT_DEADLINE_SECONDS >= 20, (
        "the embedding alone has measured 5.8s; a tight deadline loses the hint"
    )
    assert HINT_DEADLINE_SECONDS <= 60, "still bounded — an unbounded await is the original bug"
