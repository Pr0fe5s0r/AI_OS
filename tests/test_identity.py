from __future__ import annotations

from packages.core.store import stable_item_id
from packages.shared.schema import Scope, SourceRef, content_hash
from tests.conftest import BRAND_A, BRAND_B, OTHER, SCOPE

# The hash and the item id are the two things the whole write path turns on,
# and both are pure functions — so they are provable without a database.

REF = SourceRef(source="gdrive", locator="file-123")


def test_the_hash_ignores_incidental_whitespace():
    """The hash decides whether a re-sync costs a model call.

    Content that differs only by trailing whitespace is the same knowledge, and
    treating it as a change would re-embed an entire drive every time someone
    opened and re-saved a file.
    """
    assert content_hash("same content") == content_hash("  same content\n\n")
    assert content_hash("a") != content_hash("b")


def test_an_item_id_survives_its_content_changing():
    """KB-1: the identifier must survive re-indexing and content updates.

    It is derived from where the content came from, never from the content —
    an edited document is the same item at a new version, not a new item.
    """
    first = stable_item_id(SCOPE, REF)
    again = stable_item_id(SCOPE, REF)
    assert first == again


def test_two_agencies_syncing_the_same_file_never_collide():
    """Both may sync the same public document. If the id were derived from the
    source alone, one tenant's write would overwrite the other's."""
    assert stable_item_id(SCOPE, REF) != stable_item_id(OTHER, REF)


def test_two_brands_hold_the_same_file_separately():
    assert stable_item_id(BRAND_A, REF) != stable_item_id(BRAND_B, REF)
    # And a brand's copy is distinct from one held at agency level.
    assert stable_item_id(BRAND_A, REF) != stable_item_id(SCOPE, REF)


def test_different_locations_are_different_items():
    other_file = SourceRef(source="gdrive", locator="file-999")
    assert stable_item_id(SCOPE, REF) != stable_item_id(SCOPE, other_file)


def test_the_same_path_on_two_providers_is_two_items():
    s3 = SourceRef(source="s3", locator="file-123")
    assert stable_item_id(SCOPE, REF) != stable_item_id(SCOPE, s3)


def test_scope_is_immutable():
    """Scope travels through every call. If a caller could mutate one in
    flight, isolation would depend on nobody ever doing so."""
    scope = Scope(tenant_id="a", brand_id="b")
    try:
        scope.tenant_id = "hijacked"  # type: ignore[misc]
    except Exception:
        return
    raise AssertionError("Scope must be frozen")
