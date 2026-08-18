"""A key that may reach several workspaces, and cannot reach any others.

The boundary this protects is between CUSTOMERS, so the rules are deliberately
few and checked in one place:

  * a key with no grant behaves exactly as every key did before this existed —
    its own workspace, nothing else, no header able to change that;
  * a granted key may name any workspace on its list, per request;
  * anything else is refused, and recorded.

The list lives on the key and is read from the key. There is no request shape
that adds a workspace to it, which is what makes this a boundary rather than a
convention.
"""

from __future__ import annotations

import inspect

import pytest

from packages.core import keys, tenancy
from packages.core.tenancy import CrossWorkspaceAttempt, authorised_workspace


def principal(**over):
    base = {
        "company_id": "acme",
        "key_id": "k1",
        "workspaces": None,
        "scopes": ["read"],
        "via": "api_key",
    }
    return {**base, **over}


# ------------------------- the single-workspace path -------------------------


def test_an_ordinary_key_reaches_its_own_workspace():
    assert authorised_workspace(principal(), None) == "acme"


def test_an_ordinary_key_may_name_its_own_workspace_harmlessly():
    """Naming what you already are is not an escalation, and a client that
    always sends the header should not have to special-case itself."""
    assert authorised_workspace(principal(), "acme") == "acme"


def test_an_ordinary_key_cannot_name_another_workspace():
    """The header must never widen a credential. This is the case that would
    turn X-Workspace into the hole `?workspace_id=` once was."""
    with pytest.raises(CrossWorkspaceAttempt):
        authorised_workspace(principal(), "globex")


def test_an_unfilled_header_default_is_not_a_workspace_name():
    """Called directly, FastAPI's Header default arrives as an object, which is
    truthy. Read as a requested workspace it refused a request that named
    nothing — found by an existing test calling workspace_scope by hand."""
    from fastapi import Header

    assert authorised_workspace(principal(), Header(None)) == "acme"


def test_a_signed_in_person_is_bounded_the_same_way():
    """A session has no grant either — it is bounded by membership."""
    with pytest.raises(CrossWorkspaceAttempt):
        authorised_workspace(principal(via="session", key_id=None), "globex")


# ------------------------- the multi-workspace path -------------------------


def test_a_granted_key_may_choose_any_workspace_on_its_list():
    caller = principal(workspaces=["acme", "globex", "initech"])
    for workspace in ("acme", "globex", "initech"):
        assert authorised_workspace(caller, workspace) == workspace


def test_a_granted_key_defaults_to_its_home_workspace():
    """No header is not "all workspaces" — it is the one the key belongs to.
    Fanning out has to be something a caller ASKS for, one request at a time,
    so a client written for a single workspace cannot accidentally read four."""
    caller = principal(workspaces=["acme", "globex"])
    assert authorised_workspace(caller, None) == "acme"


def test_a_granted_key_cannot_reach_a_workspace_it_was_not_granted():
    caller = principal(workspaces=["acme", "globex"])
    with pytest.raises(CrossWorkspaceAttempt) as refused:
        authorised_workspace(caller, "initech")
    assert refused.value.requested == "initech"
    assert refused.value.allowed == ["acme", "globex"]


def test_the_refusal_names_neither_more_nor_less_than_it_should():
    """The message tells the caller which workspace was refused — which they
    already knew, since they asked for it — and not what else exists."""
    caller = principal(workspaces=["acme"])
    with pytest.raises(CrossWorkspaceAttempt) as refused:
        authorised_workspace(caller, "somebody-elses-workspace")
    assert "somebody-elses-workspace" in refused.value.detail
    assert refused.value.status_code == 403


# ------------------------------ where it is read ------------------------------


def test_the_grant_comes_from_the_key_and_never_from_the_request():
    """The one property that makes this a boundary. If the list could arrive on
    the request, every check below it would be theatre."""
    source = inspect.getsource(authorised_workspace)
    assert 'principal.get("workspaces")' in source
    # No route, header or body reaches this function: it takes the principal
    # and the requested id, and nothing else.
    assert list(inspect.signature(authorised_workspace).parameters) == [
        "principal",
        "requested",
    ]


def test_the_scope_dependency_enforces_it():
    """Every scoped route resolves its workspace here, so the check cannot be
    forgotten by the author of the next route."""
    source = inspect.getsource(tenancy.workspace_scope)
    assert "authorised_workspace(principal, x_workspace)" in source
    assert "x_workspace: str | None = Header" in source


def test_resolve_key_carries_the_grant():
    source = inspect.getsource(keys.resolve_key)
    assert "workspaces" in source


# ------------------------------ minting the grant ------------------------------


def test_a_key_with_no_grant_stores_nothing():
    """None, not an empty list: absence has to keep meaning "an ordinary key",
    or every existing key changes behaviour the day this ships."""
    source = inspect.getsource(keys._authorised_workspaces)
    assert "if requested is None:\n        return None" in source


def test_a_grant_always_includes_the_keys_own_workspace():
    source = inspect.getsource(keys._authorised_workspaces)
    assert "*requested, home" in source


def test_a_grant_is_refused_alongside_a_collection_binding():
    """A collection id means nothing outside the workspace that owns it —
    "bound to collection default" across four workspaces names four different
    collections."""
    source = inspect.getsource(keys._authorised_workspaces)
    assert "cannot also be bound to a collection" in source


def test_every_granted_workspace_must_exist():
    """A typo that silently grants nothing is a key that works right up until
    it is pointed at the workspace whose name was misspelled."""
    source = inspect.getsource(keys._authorised_workspaces)
    assert "No such workspace" in source


# --------------------------- the refusal is recorded ---------------------------


def test_a_refused_workspace_is_audited():
    from apps.api.main import record_cross_workspace_attempt

    handler = inspect.getsource(record_cross_workspace_attempt)
    assert '"workspace.access_denied"' in handler
    for field in ("authorised_workspaces", "requested_workspace", "key_id", "path"):
        assert field in handler


def test_recording_cannot_turn_a_refusal_into_something_else():
    from apps.api.main import record_cross_workspace_attempt

    handler = inspect.getsource(record_cross_workspace_attempt)
    assert "except Exception" in handler
    assert "status_code=403" in handler


# ------------------------- what a caller can find out -------------------------


def test_whoami_reports_every_reachable_workspace():
    """So a caller never has to discover the boundary by being refused at it,
    and can treat both kinds of key the same way."""
    from apps.api.main import whoami

    source = inspect.getsource(whoami)
    assert '"workspaces"' in source
    assert '"multi_workspace"' in source


@pytest.mark.needs_db
async def test_a_grant_is_stored_and_comes_back_on_the_key(db):
    from sqlalchemy import text

    from tests.conftest import SCOPE

    other = "poc-other-workspace"
    for workspace in (SCOPE.workspace_id, other):
        await db.execute(
            text("INSERT INTO companies (id, name) VALUES (:i, :i) ON CONFLICT DO NOTHING"),
            {"i": workspace},
        )

    minted = await keys.create_key(
        db,
        SCOPE.workspace_id,
        "multi",
        scopes="read",
        workspaces=[other],
    )
    assert minted["workspaces"] == sorted([SCOPE.workspace_id, other])

    resolved = await keys.resolve_key(db, minted["key"])
    assert resolved is not None
    assert resolved["workspaces"] == sorted([SCOPE.workspace_id, other])

    # And the enforcement reads exactly that.
    caller = {"company_id": SCOPE.workspace_id, "key_id": "k", "workspaces": resolved["workspaces"]}
    assert authorised_workspace(caller, other) == other
    with pytest.raises(CrossWorkspaceAttempt):
        authorised_workspace(caller, "never-granted")


@pytest.mark.needs_db
async def test_a_grant_naming_a_workspace_that_does_not_exist_is_refused(db):
    from tests.conftest import SCOPE

    with pytest.raises(ValueError, match="No such workspace"):
        await keys.create_key(
            db, SCOPE.workspace_id, "typo", scopes="read", workspaces=["gloabex"]
        )
