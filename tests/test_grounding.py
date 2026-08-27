"""Whether an answer counts as GROUNDED.

Pure predicates — no store needed — which is why they live apart from
test_navigator.py, whose every test needs Postgres. A rule about honesty
should not stop being checked because a database is not running.
"""

from __future__ import annotations

# ------------- a refusal is not a find, however much was read -------------


def test_a_prose_refusal_is_not_reported_as_found():
    """The navigator has two ways to answer. submit_answer(found=false) says
    so outright; writing a paragraph does not, and that path set found from
    "did we read anything" alone.

    Measured against a live store: "The store does not contain a description
    of the OSI reference model diagram… the question cannot be answered" came
    back grounded, displayed under two citations the reader could see — a
    refusal wearing the evidence of an answer. The citations were not even
    the model's own; they are back-filled from what was read, and that
    back-fill is gated on the same flag.
    """
    from packages.core.navigator import _reads_as_absent

    for refusal in (
        "The store does not contain a description of the OSI reference model diagram.",
        'The document "COMPUTER NETWORKS" does not contain a description of it.',
        # This one escaped a first attempt at the rule and reached the screen:
        # the negation is carried by "none of", not by "does not".
        "None of the documents provided contain information about Atlantis.",
        "None of the passages mention the requested figure.",
        "Therefore, the question cannot be answered based on the available information.",
        "There is no information about pricing in these documents.",
        "The provided sections do not include any reference to the topic.",
        "This information was not found in the collection.",
        "The owl is not named in the text.",
    ):
        assert _reads_as_absent(refusal), refusal


def test_an_answer_that_merely_says_contain_is_left_alone():
    """The counter-test, and the reason "does not contain" is anchored to the
    STORE rather than to any subject. Unanchored it also catches a real
    finding — a policy that does not contain an exclusion is an ANSWER about
    the policy, and reporting it ungrounded would throw away a correct one.
    """
    from packages.core.navigator import _reads_as_absent

    for answer in (
        "The policy does not contain exclusions for pre-existing conditions, so cover applies.",
        "The container does not contain hazardous material, per the manifest.",
        # "none of the X" where X is not the corpus is an ordinary finding.
        "None of the regions grew faster than Nordics in 2025.",
        "None of the tests failed, so the release proceeded.",
        "The OSI model consists of seven layers, each serving the layer above.",
        "Eleven auditors are required for a quorum before ledger reconciliation.",
    ):
        assert not _reads_as_absent(answer), answer


def test_the_prose_path_consults_the_same_rule_as_the_flag():
    """Both halves of this fix live on one line, so pin the line: found comes
    from what was read AND from the answer not reading as a refusal."""
    import inspect

    from packages.core import navigator

    source = inspect.getsource(navigator)
    assert "outcome.found = bool(read) and not _reads_as_absent(drafted)" in source


# ---------- asking for `found` rather than inferring it from prose ----------


def test_the_model_is_sent_back_to_call_submit_answer():
    """The structural fix. `found` lives on submit_answer; prose does not
    carry it, and reading the words to decide kept missing — "does not
    contain", then "none of the documents contain". So the model is asked,
    once, to send the same answer through the tool that records it."""
    import inspect

    from packages.core import navigator

    source = inspect.getsource(navigator)
    assert "if not pressed_to_submit and round_number + 1 < max_rounds:" in source
    assert '{"role": "user", "content": _SUBMIT_PROPERLY}' in source
    assert "found=false" in navigator._SUBMIT_PROPERLY


def test_the_press_leaves_room_to_be_obeyed():
    """Two rounds of headroom, not one. Compliance is not guaranteed, so the
    press needs a round to be obeyed in and another to fall back in. Measured
    with a single round: a question that had been answered with six citations
    came back "I read 6 section(s) without reaching an answer"."""
    import inspect

    from packages.core import navigator

    source = inspect.getsource(navigator)
    assert "pressed_to_submit and round_number + 1 < max_rounds" in source
    assert "pressed_to_submit = False" in source, "asked once per walk, not once per round"


def test_running_out_of_rounds_is_not_a_find():
    """The give-up message says in words that no answer was reached, and left
    `found` at whatever the loop had put there — so it came back grounded over
    six citations. Same contradiction as the prose path, one level out."""
    import inspect

    from packages.core import navigator

    source = inspect.getsource(navigator)
    give_up = source[source.index("without reaching an answer") :]
    assert "outcome.found = False" in give_up[:900]
