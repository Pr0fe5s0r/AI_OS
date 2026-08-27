"""Searching for words the answer would contain, which the question does not.

The gap, measured on a store holding the Harry Potter novels: asked "Harry owl
name" BOTH retrieval modes answer wrongly — hybrid says "Pigwidgeon", agentic
says the owl is not named. Asked "What is the name of Harry Potter's owl?",
both say Hedwig. The store contains the answer 182 times.

Neither arm can bridge it. The keyword arm needs a shared word and there is
none. The semantic arm ranks passages about handling owls above the one that
names her, because "Harry owl name" is, as a sentence, about owls in general.

These tests are mostly about the guardrails, because the danger of expansion
is not that it fails — it is that it succeeds at finding the wrong thing, or
that a word the model invented reaches the reader as though the store had said
it.
"""

from __future__ import annotations

import pytest

from packages.core import expand
from packages.core.search import DEFAULT, RetrievalConfig


def test_a_guessed_term_can_only_find_passages_never_become_one():
    """The safety property the whole feature rests on.

    Expansion terms go to the KEYWORD arm and nowhere else. They are never put
    in the prompt, never shown to the reader, never citable. If the model
    guesses "Hedwig" and the store has no Hedwig, the search returns nothing
    and the answer is exactly what it would have been.
    """
    import inspect

    from packages.core import search

    source = inspect.getsource(search.search_traced)
    where_used = source[source.index("expanded_terms") :]
    assert "keyword_search(session, scope, term" in where_used
    # The terms never join the semantic arm, and never become text anyone reads.
    assert "vector_search(scope, term" not in source
    assert "expanded_terms" not in source[: source.index("--- keyword arm")]


def test_the_original_query_is_never_replaced():
    """Expansion appends candidates. A store that answered a question before
    must answer it the same way after — the feature can only add recall."""
    import inspect

    from packages.core import search

    source = inspect.getsource(search.search_traced)
    # The unexpanded query still runs first, on both arms, exactly as before.
    assert "chunks.keyword_search(session, scope, query, limit=k)" in source
    assert "passages.append(row)" in source, "extra hits are appended, not substituted"


def test_terms_the_question_already_uses_are_dropped():
    """The original query is searched unchanged, so a term already in it has
    been looked for. Repeating it spends a query to learn nothing."""
    got = expand.parse("owl, Hedwig, Harry, snowy owl", "Harry owl name")
    assert "Hedwig" in got
    assert "owl" not in [t.lower() for t in got]
    assert "Harry" not in got


def test_a_reply_that_is_not_terms_yields_no_terms():
    """A refusal, an apology or a paragraph must come out as nothing rather
    than as garbage aimed at the index."""
    assert expand.parse("-", "q") == []
    assert expand.parse("", "q") == []
    assert expand.parse("none", "q") == []
    # Prose is not terms: every fragment here is either too long or a word the
    # question already used.
    long_prose = "I cannot determine specific terms for this question without more context about it"
    assert expand.parse(long_prose, "specific terms question context") == []


def test_punctuation_and_quoting_are_stripped():
    """A tsquery matches text, not the model's formatting. Quotes and numbering
    survive into the query otherwise and match nothing."""
    got = expand.parse('"Hedwig", 1. Pigwidgeon; Errol!', "owl")
    assert got == ["Hedwig", "1 Pigwidgeon", "Errol"] or "Hedwig" in got
    assert all('"' not in t and ";" not in t and "!" not in t for t in got)


def test_the_number_of_terms_is_bounded():
    """Each term is an extra keyword query, and a long list is the model free
    associating rather than answering."""
    got = expand.parse(",".join(f"term{n}" for n in range(20)), "question")
    assert len(got) <= expand.MAX_TERMS
    assert expand.MAX_TERMS <= 6


def test_duplicates_do_not_become_duplicate_queries():
    got = expand.parse("Hedwig, hedwig, HEDWIG", "owl name")
    assert len(got) == 1


@pytest.mark.asyncio
async def test_a_provider_failure_costs_the_terms_and_nothing_else(monkeypatch):
    """Expansion is an improvement on the query, never a dependency."""
    monkeypatch.setenv("QUERY_EXPANSION", "true")
    expand._cached.cache_clear()

    def boom(*a, **k):
        raise RuntimeError("provider down")

    import packages.core.llm as llm

    monkeypatch.setattr(llm, "chat", boom)
    assert await expand.terms("anything at all") == []


def test_the_two_callers_have_separate_switches(monkeypatch):
    """Hybrid and the navigator are not paying the same price, so one switch
    could not serve both.

    A hybrid search SHOWS what it retrieved: a guessed term matching somewhere
    irrelevant becomes a document in the citation list, and the reader sees a
    worse answer. That is why it defaults off. The navigator's hint is
    internal — expansion moves which section the agent is pointed at, the
    agent reads it and decides, and nothing there is ever cited. A missing
    suggestion there costs the answer outright: with expansion off everywhere,
    "Harry owl name" answered that the owl has no name.
    """
    monkeypatch.delenv("QUERY_EXPANSION", raising=False)
    monkeypatch.delenv("QUERY_EXPANSION_AGENTIC", raising=False)
    assert expand.enabled() is False, "hybrid defaults off — precision"
    assert expand.enabled_in_navigator() is True, "the hint defaults on — recall"

    # Each moves without disturbing the other.
    monkeypatch.setenv("QUERY_EXPANSION", "true")
    monkeypatch.setenv("QUERY_EXPANSION_AGENTIC", "false")
    assert expand.enabled() is True
    assert expand.enabled_in_navigator() is False


def test_a_caller_can_override_the_switch_in_both_directions(monkeypatch):
    """None follows the operator switch; True and False override it. A
    benchmark or a reproducible export needs retrieval that does not consult a
    model about the query, whatever the environment says."""
    assert DEFAULT.expand_query is None, "plain hybrid follows the switch"
    assert RetrievalConfig(expand_query=False).expand_query is False
    assert RetrievalConfig(expand_query=True).expand_query is True


@pytest.mark.asyncio
async def test_terms_does_not_consult_a_switch_itself(monkeypatch):
    """There are two switches now and this function cannot know which caller
    it serves. Reaching for one here would silently make the other wrong, so
    the decision stays at the call site."""
    import inspect

    source = inspect.getsource(expand.terms)
    assert "enabled()" not in source
    assert "enabled_in_navigator()" not in source


def test_the_terms_are_recorded_on_the_trace():
    """Expansion runs queries the user never typed. A result nobody can
    attribute back to a query is not explainable, which is the one thing this
    store's traces exist to prevent."""
    from packages.core.search import Trace

    trace = Trace(trace_id="t", query="q", config={}, filters={})
    assert trace.expanded == []
    trace.expanded = ["Hedwig"]
    assert trace.expanded == ["Hedwig"]


def test_the_hint_expands_but_the_agents_own_search_does_not():
    """The two navigator searches are not the same kind of query.

    The hint is a guess ABOUT the question, made before anything has been
    read, and it decides which sections the agent opens first -- the one
    decision the terse-query failure turns on.

    The search the agent runs for itself is different: it has already CHOSEN
    those words, which is what the tool is for, so asking a model what else to
    look for is guessing at a deliberate query. It also cannot be cached,
    because every query the agent writes is new -- measured at 5s -> 70s when
    every agent search spent its own expansion call.
    """
    import inspect

    from packages.core import navigator

    hint = inspect.getsource(navigator._where_the_words_are)
    assert "expand_query=expand.enabled_in_navigator()" in hint

    walk = inspect.getsource(navigator)
    assert "RetrievalConfig(limit=5, item_ids=tuple(by_id), expand_query=False)" in walk
