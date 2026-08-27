"""The support checker, and the two bugs found while building it.

Measured on nine labelled cases: it catches world-knowledge injection and
invented quotes, raises no false alarm on six correct answers — and does NOT
catch the case it was built for, where the model reads meaning into a grouping
the evidence never explains. That last one is recorded in
``test_a_grouping_the_evidence_never_explains_is_still_missed`` rather than
quietly left out, because a checker's blind spot is the thing a reader most
needs to know about it.
"""

from __future__ import annotations

from packages.core.support import (
    CHECK_TIMEOUT_SECONDS,
    NOVEL_SHARE_TO_CHECK,
    Verdict,
    _read_verdict,
    novel_share,
    quote_is_real,
)


def test_numbers_are_words_here():
    """The tokeniser matched letters only, so in a store full of tables it threw
    away exactly the tokens that matter: a quote lifted straight out of a
    results row had nothing left to verify, and the correct answer citing it
    was reported unsupported."""
    evidence = "Table 2. Model: Transformer (big). EN-DE BLEU 28.4. EN-FR BLEU 41.8."
    assert quote_is_real("EN-DE BLEU 28.4", evidence)
    assert quote_is_real("41.8", evidence)
    # And the numbers have to be the ones that were there.
    assert not quote_is_real("EN-DE BLEU 33.9", evidence)


def test_a_short_quote_can_still_be_verified():
    """The window was fixed at four words, so a three-word quote could never
    match and a good answer was flagged. The window is sized to the quote."""
    evidence = "| Department | Budget | Spent | Remaining |\n| Support | 400 | 377 | 23 |"
    assert quote_is_real("Support 400 377 23", evidence)
    assert quote_is_real("Remaining", evidence)


def test_an_invented_quote_is_refused():
    """A checker that only asked "is this supported?" cleared claims the
    evidence never made. Asking it to POINT is what makes the answer
    checkable without a second model — the span has to be there."""
    evidence = "Table 2. Model: Transformer (big). EN-DE BLEU 28.4."
    reply = "SUPPORTED\nQUOTE: the study found a 47% improvement in recall"
    assert _read_verdict(reply, evidence).supported is False

    real = "SUPPORTED\nQUOTE: Model: Transformer (big). EN-DE BLEU 28.4."
    assert _read_verdict(real, evidence).supported is True


def test_support_without_a_quote_is_not_support():
    evidence = "Prince Kutuzov was appointed commander-in-chief of the Russian forces."
    assert _read_verdict("SUPPORTED", evidence).supported is False


def test_an_unreadable_verdict_never_flags_a_good_answer():
    """A warning that fires on good answers is one people learn to ignore, so
    the checker having a bad day must cost nothing. It fails open."""
    evidence = "anything at all"
    for reply in ("", "I think perhaps", "MAYBE\nnot sure"):
        assert _read_verdict(reply, evidence).supported is True


def test_a_refusal_carries_what_was_missing():
    verdict = _read_verdict(
        "UNSUPPORTED\nMISSING: the evidence never states a boiling point.", "x"
    )
    assert verdict.supported is False
    assert "boiling point" in verdict.reason


def test_the_lexical_gate_only_decides_whether_to_ask():
    """It is a cost filter, never a verdict. A paraphrase legitimately
    introduces words, so a high novel share is a question and not an answer."""
    evidence = "Prince Kutuzov was appointed commander-in-chief of all the Russian forces."
    assert novel_share("Kutuzov was made commander-in-chief of the Russian forces", evidence) < 0.4
    assert novel_share("Mercury is liquid at room temperature", evidence) > 0.4
    assert novel_share("", evidence) == 0.0
    assert 0.0 < NOVEL_SHARE_TO_CHECK < 1.0


def test_the_check_cannot_outlast_the_answer_it_is_checking():
    """One check measured 34 seconds. On a hybrid answer that takes two, that
    is the tail wagging the dog — a slow check is worth less than a fast
    answer, so it is abandoned and the answer stands."""
    assert CHECK_TIMEOUT_SECONDS <= 10


def test_a_grouping_the_evidence_never_explains_is_still_missed():
    """The known blind spot, recorded rather than hidden.

    Asked which elements are liquid, hybrid answered "Mercury (Hg) and Bromine
    (Br) are liquid at room temperature [3]", citing a periodic table that
    lists both under a colour and never says what the colour means. The
    checker clears it: the quote is real, and the model fills in that blue
    means liquid — the same inference the answer made, one level up.

    Four algorithms were measured against this case. A lexical test is
    provably unable to separate it from correct answers: the fabrication
    leaves 0.80 of the question's terms absent from the evidence, and a
    correct answer about an atomic number leaves 0.75. The distributions
    overlap, so no threshold exists.

    This test asserts the shape of the gap, so that a future attempt has
    something to beat.
    """
    evidence = (
        "PERIODIC TABLE OF ELEMENTS. The cells are colour-coded. "
        "Blue: Mercury, Bromine, Iron, Gold."
    )
    # The quote the checker accepted is genuinely present — that is the trap.
    assert quote_is_real("Blue: Mercury, Bromine, Iron, Gold", evidence)
    # And nothing in the evidence says what blue means.
    assert "liquid" not in evidence.lower()


def test_a_verdict_says_whether_it_was_actually_checked():
    """`checked` separates "the gate settled it for free" from "a model looked",
    so the cost of this feature can be measured rather than guessed."""
    assert Verdict(True).checked is False
    assert _read_verdict("SUPPORTED\nQUOTE: x y z", "x y z").checked is True
