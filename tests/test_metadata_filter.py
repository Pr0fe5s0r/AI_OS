"""Filtering retrieval by the metadata a document was ingested with.

Documents already carried arbitrary metadata; nothing could search on it. That
made every scoping dimension beyond "which collection" into a new collection,
which is modelling a filter as a namespace.

The rules that matter here are not about SQL. They are about what a caller is
entitled to see: a filter that silently fails open returns documents the caller
excluded, and an answer built on those is worse than no answer at all.
"""

from __future__ import annotations

import pytest

from packages.core.search import (
    MAX_NARROWED_ITEMS,
    RetrievalConfig,
    _by_key,
    _filters,
)
from packages.shared.schema import Scope

WS = Scope(workspace_id="ws")


# ------------------------------ the filter shape ------------------------------


def test_different_keys_must_all_match():
    grouped = _by_key((("client", "acme"), ("kind", "policy")))
    assert grouped == [("client", ["acme"]), ("kind", ["policy"])]


def test_the_same_key_matches_any_of_its_values():
    """?meta=kind:policy&meta=kind:notice&meta=client:acme reads as 'client
    acme, and either a policy or a notice' — with no syntax to learn."""
    grouped = _by_key((("kind", "policy"), ("kind", "notice"), ("client", "acme")))
    assert grouped == [("kind", ["policy", "notice"]), ("client", ["acme"])]


def test_order_is_kept_so_the_sql_reads_like_the_request():
    grouped = _by_key((("z", "1"), ("a", "2")))
    assert [key for key, _ in grouped] == ["z", "a"]


# ------------------------------ the SQL it makes ------------------------------


def test_the_key_is_bound_never_interpolated():
    """A metadata filter that built SQL out of a caller's key would be an
    injection hole dressed as a feature."""
    where, params = _filters(WS, RetrievalConfig(metadata=(("client", "acme"),)))
    assert "metadata ->> :mkey0 = ANY(:mval0)" in where
    assert params["mkey0"] == "client"
    assert params["mval0"] == ["acme"]
    # The value never appears in the statement text.
    assert "acme" not in where


def test_a_hostile_key_cannot_escape_the_bind():
    nasty = "x' OR '1'='1"
    _where, params = _filters(WS, RetrievalConfig(metadata=((nasty, "v"),)))
    assert params["mkey0"] == nasty


def test_no_filter_adds_no_clause():
    where, params = _filters(WS, RetrievalConfig())
    assert "metadata" not in where
    assert not any(key.startswith("mkey") for key in params)


def test_metadata_joins_the_other_filters_rather_than_replacing_them():
    """It is applied where every other filter is applied — at hydration, over
    whatever both arms proposed — so one clause covers semantic and keyword
    alike."""
    where, _ = _filters(
        Scope(workspace_id="ws", collection_id="acme"),
        RetrievalConfig(metadata=(("kind", "policy"),), sources=("upload",)),
    )
    assert "workspace_id = :workspace" in where
    assert "collection_id = :collection" in where
    assert "source = ANY(:sources)" in where
    assert "metadata ->> :mkey0" in where


# ------------------------------- recall ---------------------------------


def test_a_metadata_filter_widens_the_candidate_net():
    """Filtering only after ranking would be correct and quietly useless.

    Ask for five documents out of ten thousand and the top forty candidates
    contain none of them — the store answers "nothing found" about content it
    is holding. Naming ten document ids looks narrow; `client=acme` looks broad
    and may be narrower still, which is why it needs the same widening.
    """
    plain = RetrievalConfig(limit=10)
    filtered = RetrievalConfig(limit=10, metadata=(("client", "acme"),))
    assert filtered.candidates() > plain.candidates()
    assert filtered.candidates() == RetrievalConfig(limit=10, item_ids=("a",)).candidates()


def test_the_narrowing_has_a_ceiling():
    """Past some size a filter is not narrowing the search, it is describing
    most of it — and passing fifty thousand ids into `= ANY(...)` costs more
    than the ranking it was meant to help. The filter still applies; only the
    id list is dropped."""
    assert MAX_NARROWED_ITEMS >= 1000


def test_the_config_travels_into_the_trace():
    """"Nothing found" under a filter and "nothing found" without one are
    different events. A trace that cannot tell them apart explains neither."""
    cfg = RetrievalConfig(metadata=(("client", "acme"),))
    assert cfg.as_dict()["metadata"] == [["client", "acme"]]


# ------------------------- parsing what a caller sent -------------------------


def test_a_filter_is_parsed_on_the_first_colon_only():
    """So a value may contain one: a URL, a timestamp, a path."""
    from apps.api.main import _metadata_pairs

    assert _metadata_pairs(["url:https://example.com/a"]) == (
        ("url", "https://example.com/a"),
    )


def test_a_malformed_filter_is_refused_not_ignored():
    """The one failure this feature must never have.

    A filter that silently does nothing returns documents the caller meant to
    exclude, and the caller has no way to know. Better a 422 they can read.
    """
    from fastapi import HTTPException

    from apps.api.main import _metadata_pairs

    for bad in (["client"], [":acme"], ["   :x"]):
        with pytest.raises(HTTPException) as raised:
            _metadata_pairs(bad)
        assert raised.value.status_code == 422


def test_an_empty_value_is_allowed():
    # Matching a key that was written as an empty string is a real thing to
    # ask; only a missing KEY is meaningless.
    from apps.api.main import _metadata_pairs

    assert _metadata_pairs(["client:"]) == (("client", ""),)


def test_no_filter_at_all_is_not_an_error():
    from apps.api.main import _metadata_pairs

    assert _metadata_pairs(None) == ()
    assert _metadata_pairs([]) == ()


# ------------------------------ nothing matched ------------------------------


def test_no_matching_document_is_answered_not_searched():
    """Once the filter has excluded everything there is nothing to retrieve
    from, and running the query anyway would either waste a model call or —
    worse — quietly answer from documents the caller excluded."""
    import time

    from packages.core.answer import _nothing_matched

    result, trace = _nothing_matched(
        "what is the policy?", "agentic", RetrievalConfig(metadata=(("client", "x"),)), time.perf_counter()
    )
    assert result.grounded is False
    assert result.citations == []
    # And it says WHY: "no matching documents" and "nothing found in the
    # documents" are different facts about a store.
    assert "metadata" in (result.degraded or "")
    assert trace.filters["matched_documents"] == 0
