"""Words the answer would contain, which the question does not.

The gap this closes, measured: asked "Harry owl name" of a store holding the
Harry Potter novels, BOTH retrieval modes answer wrongly — hybrid says
"Pigwidgeon", agentic says the owl is not named. Asked "What is the name of
Harry Potter's owl?", both say Hedwig. The store contains the answer 182 times.

Neither arm can bridge it. The keyword arm needs a shared word and there is
none: "Hedwig" appears nowhere in the question. The semantic arm ranks passages
about handling owls above the one passage that happens to name her, because
"Harry owl name" is, as a sentence, about owls in general.

So the model is asked what the answer would probably SAY, and those words are
searched for as well.

Two properties this must have, and both are about not making retrieval worse:

  it never replaces the question   The original query runs exactly as before,
                                   on both arms. Expansion terms are searched
                                   IN ADDITION, and only through the keyword
                                   arm — the semantic arm already generalises,
                                   and re-embedding guesses would be paying to
                                   blur the query.

  a guess is never a fact          Terms are used to FIND passages, never
                                   shown to the answering model and never
                                   citable. If the model guesses "Hedwig" and
                                   the store has no Hedwig, the keyword arm
                                   returns nothing and the answer is unchanged.
                                   The invention cannot reach the reader.
"""

from __future__ import annotations

import asyncio
import os
import re
from functools import lru_cache

# How many candidate terms to use. Few, because each is an extra keyword query
# and because a long list is the model free-associating rather than answering.
MAX_TERMS = 4
# Longer than this is a phrase the tsquery will not match anyway.
MAX_TERM_CHARS = 40

_PROMPT = (
    "A search engine is about to look for the answer to this question in a "
    "document collection. Its weakness is exact words: it can only match text "
    "that is actually there.\n\n"
    "Name up to four specific words or short phrases that would very likely "
    "appear IN the sentence that answers this question — proper nouns, names, "
    "identifiers, technical terms. Guess the actual answer if you can: if the "
    "question asks what something is called, the name itself is the best term.\n\n"
    "Rules: comma-separated, nothing else. No explanation, no numbering, no "
    "quotes. Do not repeat words already in the question — those are already "
    "being searched. If you have no useful guess, reply with a single dash.\n\n"
    "QUESTION: "
)

# Anything that is not a word, a space or a hyphen is punctuation the model
# added, and punctuation in a tsquery matches nothing.
_CLEAN = re.compile(r"[^\w\s'-]")


def enabled() -> bool:
    """Off by default, and the default is the interesting part.

    It works: with it on, every benchmark query answered correctly, including
    the terse one neither arm could reach. The cost is precision, and it is
    not a rounding error — the guessed terms are real words, so they match in
    documents the question was not about. "Server requirement" expanded to
    "hardware, specification", found both in a novel, and cited three
    documents where it had cited one. A wider citation list reads as a worse
    answer even when the answer is right.

    So this is left to the operator rather than assumed: on for a store of
    short, keyword-poor queries, off for one where the sources are read.
    """
    return _flag("QUERY_EXPANSION", "false")


def enabled_in_navigator() -> bool:
    """On by default, and for the opposite reason to the switch above.

    The two callers are not paying the same price. A hybrid search SHOWS what
    it retrieved, so a guessed term that matches somewhere irrelevant becomes
    a document in the citation list and the reader sees a worse answer. The
    navigator's searches are internal: expansion moves which SECTION the agent
    is pointed at, and the agent then reads it and decides. A bad suggestion
    costs a read; a missing one costs the answer.

    Measured with expansion off everywhere, on a store holding the novels:
    "Harry owl name" walked to two sections, never reached the passage naming
    her, and answered that the owl has no name. "What is the name of Harry
    Potter's owl?" answered Hedwig in 3.41s. The gap between those two is
    exactly what expansion closes, and closing it here costs the citation list
    nothing, because these searches are never what gets cited.
    """
    return _flag("QUERY_EXPANSION_AGENTIC", "true")


def _flag(name: str, default: str) -> bool:
    return os.getenv(name, default).lower() in ("1", "true", "yes")


def _asked(question: str) -> set[str]:
    return {w for w in re.findall(r"\w+", question.lower()) if len(w) > 2}


def parse(reply: str, question: str) -> list[str]:
    """Terms from a model reply, cleaned and filtered.

    Separate from the call so the parsing is testable without a provider, and
    because this is where the guardrails live: a reply that is prose, or a
    refusal, or the question echoed back, has to come out as no terms at all
    rather than as garbage aimed at the index.
    """
    if not reply or reply.strip() in {"-", "--", "none", "None"}:
        return []
    already = _asked(question)
    out: list[str] = []
    for raw in reply.split(","):
        term = _CLEAN.sub(" ", raw).strip()
        term = " ".join(term.split())
        if not term or len(term) > MAX_TERM_CHARS:
            continue
        # A term the question already contains adds nothing: the original query
        # is searched unchanged, so it has been looked for.
        words = {w for w in re.findall(r"\w+", term.lower()) if len(w) > 2}
        if not words or words <= already:
            continue
        if term.lower() in {t.lower() for t in out}:
            continue
        out.append(term)
        if len(out) >= MAX_TERMS:
            break
    return out


@lru_cache(maxsize=512)
def _cached(question: str) -> tuple[str, ...]:
    """One model call per distinct question, for the life of the process.

    The same bound and the same reasoning as the query-embedding cache beside
    it: a console refresh, a dashboard and two agents asking the same thing are
    the common case, and the answer cannot change without a restart.
    """
    from packages.core.llm import chat

    try:
        reply = chat([{"role": "user", "content": _PROMPT + question}], temperature=0.0)
    except Exception:
        # Expansion is an improvement on the query, never a dependency. A
        # provider that will not answer costs the extra terms and nothing else.
        return ()
    return tuple(parse(reply, question))


async def terms(question: str) -> list[str]:
    """Candidate words the answer might contain. Empty when disabled or unsure.

    Off the event loop because the client blocks. Intended to be gathered with
    the semantic arm, which is also network-bound — run that way it adds no
    wall-clock to a search that was already waiting on an embedding.

    Deliberately does NOT consult a switch. There are two of them now and this
    function cannot know which caller it is serving, so the decision belongs
    at the call site — reaching for a switch here would silently make one of
    the two wrong.
    """
    if not question.strip():
        return []
    return list(await asyncio.to_thread(_cached, question))


__all__ = ["MAX_TERMS", "enabled", "parse", "terms"]
