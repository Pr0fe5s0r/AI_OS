from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# SUPPORT CHECKING — does the evidence actually say what the answer claims?
#
# `grounded` used to mean "the model wrote a marker and the marker resolved".
# That is a check on formatting, not on truth, and it passed the worst answer
# this store has produced:
#
#     Q: Which elements are liquid at room temperature?
#     A: Mercury (Hg) is liquid at room temperature [3]. Bromine (Br) is
#        also liquid at room temperature [3].
#
# Passage [3] is a transcription of a periodic table. It contains the words
# "Mercury" and "Bromine" — they are element names — and it does not contain
# the word "liquid" anywhere. The claim is true of the world and absent from
# the source. The model supplied it from its own chemistry, attached a citation
# to it, and every downstream signal said the answer was grounded.
#
# That is the failure this module exists to catch: a TRUE claim that the
# evidence does not make. It is worse than a false one, because checking it
# requires reading the source, which is the thing a citation is supposed to
# make unnecessary.
#
# Two gates, cheapest first:
#
#   1. a lexical pre-filter, free, that decides whether asking is worth a call
#   2. an entailment check by the model, but ONLY on the claim and the passage
#      it cites — never on the question, so it cannot be tempted to answer
#
# The order matters for latency: hybrid answers in 2-6 seconds and most of them
# are fine, so the common case must not pay for a second model call.
# ---------------------------------------------------------------------------

# Words that carry no subject. A claim built only from these is not a claim.
_EMPTY = frozenset(
    """the and for are was were you your with that this from what which who whom
    how why when where does did has have had can could should would will shall
    may might must not but its it is they them their there here about into over
    under than then also more most some any all each other another between such
    been being both very only just like also however therefore thus according
    states state stated says said mention mentions mentioned passage passages
    document section text following above below shown show shows""".split()
)

# Numbers are words here. An earlier version matched letters only, and in a
# store full of tables that threw away the tokens that matter most: "28.4",
# "91.3", "79" all vanished, so a quote lifted straight out of a results table
# had almost nothing left to verify and correct answers were flagged as
# unsupported. Whatever a passage is made of, that is what has to be compared.
_WORD = re.compile(r"[a-z0-9][a-z0-9'.\-]*")

# How much of a claim's own vocabulary has to be missing from its source before
# the model is asked to look. Set from the case above: "liquid at room
# temperature" shares "mercury" and "bromine" with the passage and nothing
# else, so its novel share is high. An honest paraphrase reuses far more.
NOVEL_SHARE_TO_CHECK = 0.4

# Most checks land in under a second. One measured 34 seconds — the checker
# turning a hard case over and over — which on a hybrid answer that took 2
# seconds would be the tail wagging the dog. Past this it is abandoned and the
# answer stands unflagged: a slow check is worth less than a fast answer.
CHECK_TIMEOUT_SECONDS = 8.0


@dataclass(slots=True)
class Verdict:
    """What the check concluded, and why — the reason is shown to the reader."""

    supported: bool
    reason: str = ""
    checked: bool = False  # False when the lexical gate settled it for free


def _content(text: str) -> set[str]:
    words = (w.strip(".-'") for w in _WORD.findall(text.lower()))
    return {w for w in words if len(w) > 1 and w not in _EMPTY}


def novel_share(claim: str, evidence: str) -> float:
    """How much of the claim's vocabulary the evidence never uses.

    Deliberately blunt. It decides only whether to spend a model call, never
    whether an answer is good — a paraphrase legitimately introduces words, so
    a high share here is a question, not a verdict.
    """
    words = _content(claim)
    if not words:
        return 0.0
    known = _content(evidence)
    return len({w for w in words if w not in known}) / len(words)


# Asking "is this supported?" and believing the answer does not work. Measured:
# it cleared "Mercury (Hg) is liquid at room temperature" against a periodic
# table that never says "liquid", because the table does mention mercury and
# the model filled the rest in for itself — the same failure it was being asked
# to catch, one level up.
#
# So it is not asked for a judgement. It is asked to POINT: quote the words
# that state the claim. A quote is checkable without a model, and the check
# below is exactly that — the span has to occur in the evidence, verbatim. A
# checker that cannot find the words cannot invent them.
_PROMPT = (
    "Find the words in the EVIDENCE that state the CLAIM.\n\n"
    "EVIDENCE:\n{evidence}\n\n"
    "CLAIM:\n{claim}\n\n"
    "Reply in exactly one of these two forms:\n\n"
    "SUPPORTED\n"
    "QUOTE: <copy the exact words from the evidence, character for character>\n\n"
    "or\n\n"
    "UNSUPPORTED\n"
    "MISSING: <one short sentence naming what the evidence never says>\n\n"
    "The quote must be copied from the evidence above, not written by you. If "
    "you cannot find words that state the claim, the answer is UNSUPPORTED — "
    "including when the claim is true and you happen to know it. Naming "
    "something the evidence mentions does not license a statement the evidence "
    "never makes about it.\n"
    "Different wording for the same fact is fine, as long as some span of the "
    "evidence carries the meaning; quote that span. A total drawn from figures "
    "in the evidence is supported; quote the figures.\n"
    "A group, colour, column or heading means only what the evidence says it "
    "means. If the claim depends on what a grouping REPRESENTS, the evidence "
    "must say what it represents — quote that too, or answer UNSUPPORTED."
)

_QUOTE = re.compile(r"^\s*QUOTE:\s*(.+)$", re.I | re.M | re.S)


def _runs(text: str, size: int) -> set[str]:
    words = [w.strip(".-'") for w in _WORD.findall(text.lower())]
    words = [w for w in words if w]
    if len(words) < size:
        return {" ".join(words)} if words else set()
    return {" ".join(words[i : i + size]) for i in range(len(words) - size + 1)}


def quote_is_real(quote: str, evidence: str) -> bool:
    """Does this span actually occur in the evidence?

    Not string equality: a model reflows whitespace, drops a bracket, fixes a
    typo. Overlapping word runs survive that and do not survive invention.

    The window is sized to the QUOTE, not fixed. Fixed at four, a three-word
    quote lifted verbatim out of a results table — "EN-DE BLEU 28.4" — could
    never match a four-word window and the correct answer citing it was
    reported unsupported. A false alarm on a good answer is the worst thing
    this module can do: it is the one that teaches people to ignore the flag.
    """
    words = [w for w in (x.strip(".-'") for x in _WORD.findall(quote.lower())) if w]
    if not words:
        return False
    size = min(4, len(words))
    claimed = _runs(quote, size)
    present = _runs(evidence, size)
    return len(claimed & present) / len(claimed) >= 0.5


def _read_verdict(reply: str, evidence: str) -> Verdict:
    text = (reply or "").strip()
    head = text.split("\n", 1)[0].strip().upper()

    if head.startswith("UNSUPPORTED"):
        _, _, missing = text.partition("MISSING:")
        return Verdict(False, reason=(missing.strip() or "the evidence does not say this")[:300], checked=True)

    if head.startswith("SUPPORTED"):
        found = _QUOTE.search(text)
        quote = found.group(1).strip() if found else ""
        if not quote:
            return Verdict(False, reason="no supporting quote was given", checked=True)
        if not quote_is_real(quote, evidence):
            # The checker produced words the evidence does not contain. That is
            # the same fabrication one level up, and it is the whole reason the
            # quote is verified here rather than trusted.
            return Verdict(False, reason="the quoted words are not in the passage", checked=True)
        return Verdict(True, checked=True)

    # An unreadable reply is not evidence of anything. Calling it unsupported
    # would put a warning on a good answer every time the checker had a bad
    # day, and a warning that fires on good answers is one people learn to
    # ignore.
    return Verdict(True, reason="check inconclusive", checked=True)


async def check(claim: str, evidence: str) -> Verdict:
    """Is ``claim`` stated by ``evidence``? Cheap gate first, model second."""
    if not claim.strip() or not evidence.strip():
        return Verdict(True)

    if novel_share(claim, evidence) < NOVEL_SHARE_TO_CHECK:
        # Almost every word of the claim is already in the source. There is
        # nothing here worth a round trip.
        return Verdict(True)

    from packages.core.llm import chat

    prompt = _PROMPT.format(evidence=evidence[:6000], claim=claim[:2000])
    try:
        reply = await asyncio.wait_for(
            asyncio.to_thread(chat, [{"role": "user", "content": prompt}], temperature=0.0),
            timeout=CHECK_TIMEOUT_SECONDS,
        )
    except Exception:
        # Unreachable, or too slow to be worth the wait. Either way it fails
        # open and says nothing: this check exists to catch a wrong answer, and
        # it must never become the reason a right one arrives late or wearing a
        # warning it did not earn.
        return Verdict(True, reason="check unavailable")
    return _read_verdict(reply, evidence)


__all__ = ["NOVEL_SHARE_TO_CHECK", "Verdict", "check", "novel_share"]
