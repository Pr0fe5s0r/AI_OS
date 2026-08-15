"""Deleting one document, and everything indexed from it.

A whole workspace could be erased and a single document could not, which is a
strange pair of capabilities to ship together. These pin the two properties
that make the new route safe:

  * nothing is destroyed until a caller has been TOLD what would be, and
  * when it is destroyed, the derived state goes with it — because a passage
    that outlives its document still carries an embedding, and so still answers
    questions, which is the one outcome a deletion may never leave behind.
"""

from __future__ import annotations

import inspect

from apps.api import authz
from packages.core import blobs, erasure

# ------------------------------- the alert -------------------------------


def test_the_route_refuses_until_it_is_confirmed():
    """A DELETE without confirm=true deletes nothing and answers 409.

    Not ceremony: there is no undo here and no trash to restore from. "Delete
    this document?" is a question nobody can answer well; "delete COMPUTER
    NETWORKS, 2 versions, 3,915 passages, permanently" is.
    """
    from apps.api.main import remove_item

    source = inspect.getsource(remove_item)
    assert "if not confirm:" in source
    assert "409" in source
    # And the refusal carries the real numbers, not a generic warning.
    assert "would_delete" in source
    assert source.index("preview_item_deletion") < source.index("delete_item_and_index")


def test_the_preview_reads_before_anything_is_touched():
    source = inspect.getsource(erasure.preview_item_deletion)
    assert "SELECT" in source
    for destructive in ("DELETE FROM", "delete_prefix", "graph.delete_item"):
        assert destructive not in source, "the preview must not destroy anything"


def test_a_missing_document_is_a_404_not_a_confirmation():
    # Offering to delete something that is not there would be a dialog about
    # nothing, and a "deleted" that deleted nothing.
    source = inspect.getsource(__import__("apps.api.main", fromlist=["remove_item"]).remove_item)
    assert 'raise HTTPException(404, "No such item.")' in source


# --------------------------- the index goes too ---------------------------


def test_every_store_is_cleared():
    """Postgres rows, graph nodes and stored objects.

    A row deleted while its vectors remain is a document that is gone from the
    catalogue and still answering questions from the index.
    """
    source = inspect.getsource(erasure.delete_item)
    assert "graph.delete_item" in source
    assert "blobs.delete_prefix" in source
    for table in ("kb_chunks", "kb_item_classes", "kb_items"):
        assert table in source


def test_the_graph_goes_first_and_postgres_last():
    """The order that makes a partial failure survivable.

    While the rows exist the document is still explicable; while the vectors
    exist it is still answerable. So the answerable part goes first.
    """
    source = inspect.getsource(erasure.delete_item)
    assert source.index("graph.delete_item") < source.index("DELETE FROM")


def test_derived_state_failing_never_aborts_the_row_deletion():
    """A document whose vectors were removed but whose rows remain answers from
    nothing. Worse than orphaned page pictures lingering in a bucket."""
    source = inspect.getsource(erasure.delete_item)
    assert source.count("except Exception") >= 2
    assert "graph_error" in source and "objects_error" in source


def test_all_the_cypher_lives_in_the_graph_module():
    # The repo's own rule, and this change adds a delete.
    assert "DETACH DELETE" in inspect.getsource(__import__("packages.core.graph", fromlist=["x"]))
    assert "DETACH DELETE" not in inspect.getsource(erasure)


# ------------------------------ blob sweeping ------------------------------


def test_a_prefix_delete_sweeps_more_than_once():
    """Measured against MinIO: write an original plus 1,200 page pictures, list
    the prefix, and the listing answers ONE. Delete what it offered, list again,
    and the same prefix answers 1,201.

    The objects were there the whole time — the listing index lags a burst of
    writes — so a single list-then-delete pass leaves behind what it could not
    yet see, and reports success while doing it.
    """
    source = inspect.getsource(blobs.delete_prefix)
    assert "MAX_DELETE_SWEEPS" in source
    assert "if found == 0:" in source
    assert blobs.MAX_DELETE_SWEEPS >= 2


def test_the_sweep_is_bounded():
    # An unbounded loop would spin on a bucket somebody else is still writing.
    assert blobs.MAX_DELETE_SWEEPS <= 20


def test_pagination_is_not_forgotten():
    # A 962-page PDF has nearly a thousand rendered pages and the list call caps
    # at a thousand keys.
    source = inspect.getsource(blobs.delete_prefix)
    assert "ContinuationToken" in source and "IsTruncated" in source


def test_one_document_prefix_cannot_reach_another():
    """Item ids are fixed-length hex, so no document's key can be a strict
    prefix of another's."""
    a = blobs.key_for("ws", "a" * 32)
    b = blobs.key_for("ws", "a" * 30 + "00")
    assert not b.startswith(a)
    assert len(a) == len(b)


# -------------------------------- the scope --------------------------------


def test_deleting_a_document_is_a_write_not_a_manage():
    """An operator who may not READ a document may not destroy it either.

    Deleting a COLLECTION is the container operation, and that is where the
    manage scope's destructive power stops.
    """
    assert authz.requirement("DELETE", "/api/items/{item_id}") == authz.WRITE
    assert authz.requirement("DELETE", "/api/collections/{collection_id}") == authz.MANAGE


def test_the_deletion_is_audited():
    source = inspect.getsource(__import__("apps.api.main", fromlist=["remove_item"]).remove_item)
    assert "audit.record" in source
    assert '"item.deleted"' in source
