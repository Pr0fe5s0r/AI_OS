"""The format contract, and the check that keeps it honest.

A list of supported formats is a promise a caller builds conversion logic
against. The failure mode this file exists to prevent is drift: a parser
registered without documentation, so a format is accepted, indexes in some
shape nobody has described, and the published contract quietly stops matching
the software.
"""

from __future__ import annotations

from packages.core import formats
from packages.core.normalise import supported


def test_every_registered_parser_is_documented():
    """The anti-drift check.

    An accepted-but-undocumented format is worse than a refused one: a caller
    sends files it will never get useful answers from, and nothing says so.
    """
    assert formats.undescribed() == [], (
        "a parser is registered with no entry in packages/core/formats.py; "
        "describe its unit of citation before shipping it"
    )


def test_every_documented_extension_is_actually_accepted():
    """The other direction: the contract must not advertise a format the
    registry cannot read."""
    advertised = {ext for f in formats.described() for ext in f.extensions}
    assert advertised == set(supported()), advertised.symmetric_difference(set(supported()))


def test_every_format_declares_what_a_passage_is():
    """'.pptx is supported' says nothing about whether a citation comes back as
    'slide 12' or as one blob per deck. The unit of citation is the part a
    caller designs around."""
    for described in formats.described():
        assert described.unit, f"{described.family} does not say what one passage is"
        assert len(described.unit) > 10


def test_the_contract_is_versioned():
    """The client is building conversion logic on the other side of this list.
    A format that starts landing natively changes their pipeline."""
    contract = formats.contract()
    assert contract["version"] == formats.VERSION
    assert contract["version"].count(".") == 2


def test_the_contract_still_carries_the_flat_list_it_used_to_return():
    """/api/formats returned {"supported": [...]} before this existed. Removing
    that key would break every caller written against it for no gain."""
    assert formats.contract()["supported"] == list(supported())


def test_the_unsupported_list_is_stated_rather_than_implied():
    """Silence about a format reads as support for it. .doc and .rtf arrive
    often enough that a caller needs to be told to convert them."""
    contract = formats.contract()
    text = " ".join(row["what"] + row["guidance"] for row in contract["not_supported"])
    for common in (".doc", ".rtf", ".zip"):
        assert common in text


def test_the_contract_says_what_happens_on_failure():
    """R2.3: a format the pipeline cannot parse must fail visibly and be
    re-runnable, and the contract is where a caller learns that."""
    on_failure = formats.contract()["on_failure"]
    assert "failed" in on_failure
    assert "metadata.failure" in on_failure
    assert "never replaces" in on_failure


def test_html_is_in_the_contract_with_its_javascript_caveat():
    """The one caveat an HTML caller will actually hit."""
    html = next(f for f in formats.described() if f.family == "HTML")
    assert "JavaScript" in html.notes
    assert any("script" in drop for drop in html.drops)


def test_a_deck_cites_by_slide_and_a_workbook_by_sheet():
    """The two the client named explicitly (R2.2)."""
    by_family = {f.family: f for f in formats.described()}
    assert "slide" in by_family["PowerPoint"].unit
    assert "sheet" in by_family["Excel"].unit
    assert "page number" in by_family["PDF"].unit
