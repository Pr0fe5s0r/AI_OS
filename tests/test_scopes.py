"""Read, write, manage — and the gate that did not exist.

A platform operator running a multi-tenant deployment has to be able to create,
rename and delete a tenant's collection, and mint that tenant's keys, WITHOUT
being able to read a single one of their documents. Before `manage` that was
impossible to issue.

The reason it was impossible is the thing most of these tests protect. There
was no read check anywhere: fifty routes, eight of them calling require_write,
and nothing at all checking read. `read` did not mean "may read" — it meant
"not write" — so any valid key reached every document in its workspace no
matter what it had been minted with. Absence of a check meant permitted.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from apps.api import authz
from apps.api.main import app
from packages.core import keys
from packages.core.tenancy import require_manage, require_read, require_write

# --------------------------- the matrix is complete ---------------------------


def _api_routes() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for route in app.routes:
        methods = getattr(route, "methods", None)
        path = getattr(route, "path", "")
        if not methods or not path.startswith("/api/") or path.startswith("/api/auth/"):
            continue
        out.extend((method, path) for method in sorted(methods - {"HEAD", "OPTIONS"}))
    return out


def test_every_route_declares_what_it_needs():
    """The audit that makes this enforcement rather than etiquette.

    A route added next year is a CI failure, not a hole. Enforcement that
    depends on the author of the next route remembering to add a check is not
    enforcement — which is exactly how the read gate came to be missing from
    all fifty of them.
    """
    undeclared = [route for route in _api_routes() if authz.requirement(*route) is None]
    assert undeclared == [], (
        "these routes have no entry in apps/api/authz.py, and would be served "
        f"to anyone: {undeclared}"
    )


def test_the_matrix_names_no_route_that_does_not_exist():
    # A stale entry is a rule nobody is enforcing, and reads as though somebody
    # thought about a route that has since been renamed or removed.
    live = set(_api_routes())
    assert [entry for entry in authz.matrix() if entry not in live] == []


def test_an_open_route_needs_no_credential_at_all():
    """A route that requires no scope must require no CREDENTIAL.

    Asking the authorisation dependency for a principal on every request made
    /api/health answer 401, and that is not a policy decision — it is a
    container reporting itself unhealthy and an orchestrator refusing to start
    it. Caught by the Docker healthcheck going red, which is the cheapest place
    this could have been caught and not somewhere a unit test was looking.
    """
    import inspect

    source = inspect.getsource(authz.authorise)
    # The optional resolver, not the raising one.
    assert "resolve_caller_optional" in inspect.getsource(authz)
    # And the open branch returns BEFORE anything demands a principal.
    assert source.index("if not needed:") < source.index("if principal is None:")


def test_health_is_open():
    assert authz.requirement("GET", "/api/health") == authz.OPEN
    assert authz.requirement("GET", "/api/formats") == authz.OPEN


def test_an_undeclared_route_is_refused_not_served():
    """The direction this has to fail.

    A route absent from the table is one nobody has decided about. Serving it
    would make the default "anyone may", which is the fail-open behaviour that
    made a scoped key meaningless in the first place.
    """
    assert authz.requirement("GET", "/api/something-nobody-declared") is None


# ------------------------- contents versus containers -------------------------


def test_document_contents_need_read():
    for route in (
        ("GET", "/api/search"),
        ("GET", "/api/answer"),
        ("GET", "/api/items"),
        ("GET", "/api/items/{item_id}"),
        ("GET", "/api/items/{item_id}/chunks"),
        ("GET", "/api/items/{item_id}/original"),
        ("GET", "/api/items/{item_id}/pages/{page}"),
        ("GET", "/api/chunks/{chunk_id}"),
        ("POST", "/api/items/batch"),
    ):
        assert authz.requirement(*route) == authz.READ, route


def test_a_trace_is_contents():
    """It holds the question asked and excerpts of the passages that answered
    it. Anyone who can read traces can read the documents, one query at a
    time."""
    for route in (
        ("GET", "/api/traces"),
        ("GET", "/api/traces/stats"),
        ("GET", "/api/traces/{trace_id}"),
    ):
        assert authz.requirement(*route) == authz.READ, route


def test_summaries_and_the_index_graph_are_contents():
    # Model-written, but written FROM the documents and describing them closely
    # enough to be them.
    for route in (
        ("GET", "/api/collections/{collection_id}/summaries"),
        ("GET", "/api/collections/{collection_id}/graph"),
        ("GET", "/api/collections/{collection_id}/mapping"),
    ):
        assert authz.requirement(*route) == authz.READ, route


def test_administering_containers_needs_manage_and_not_read():
    for route in (
        ("POST", "/api/collections"),
        ("PATCH", "/api/collections/{collection_id}"),
        ("DELETE", "/api/collections/{collection_id}"),
        ("GET", "/api/collections/{collection_id}"),
        ("GET", "/api/keys"),
        ("POST", "/api/keys"),
        ("DELETE", "/api/keys/{key_id}"),
    ):
        needed = authz.requirement(*route)
        assert needed == authz.MANAGE, route
        assert "read" not in needed, route


def test_a_collection_summary_route_does_not_carry_titles():
    """Decided deliberately: a filename is tenant information.

    "Acme-Q3-layoffs.pdf" says something before it is opened, so an operator
    who may not read documents may not read what they are called. The route is
    manage and returns name, counts and size.
    """
    assert authz.requirement("GET", "/api/collections/{collection_id}") == authz.MANAGE
    assert authz.requirement("GET", "/api/items") == authz.READ


def test_accepting_an_invitation_is_not_an_administrative_act():
    # Requiring manage would mean only an administrator could accept their own
    # invitation, which is nobody's idea of an invitation.
    assert authz.requirement("POST", "/api/team/accept") == authz.OPEN
    assert authz.requirement("POST", "/api/team/invite") == authz.MANAGE


# ------------------------------- the gates -------------------------------


def test_a_manage_only_key_cannot_read():
    """The whole point, in one assertion."""
    operator = {"scopes": ["manage"]}
    with pytest.raises(HTTPException) as raised:
        require_read(operator)
    assert raised.value.status_code == 403
    require_manage(operator)  # and can still administer


def test_a_read_key_cannot_manage_or_write():
    reader = {"scopes": ["read"]}
    require_read(reader)
    for gate in (require_write, require_manage):
        with pytest.raises(HTTPException):
            gate(reader)


def test_a_signed_in_person_holds_every_scope():
    """Scopes narrow a KEY — a credential handed to a program. A person is
    bounded by their membership instead, and the console reads documents and
    administers collections in one session."""
    import inspect

    from packages.core import tenancy

    source = inspect.getsource(tenancy.resolve_caller)
    assert '"scopes": ["read", "write", "manage"]' in source


# --------------------------- the escalation path ---------------------------


def test_a_key_cannot_mint_a_stronger_key():
    """Without this the manage scope is decoration.

    An operator holding a manage-only key — deliberately unable to read a
    single tenant document — calls POST /api/keys, mints itself a read key, and
    reads all of them. One API call, and the separation is gone.
    """
    with pytest.raises(keys.Escalation):
        keys.check_subset({"manage"}, {"read"})
    with pytest.raises(keys.Escalation):
        keys.check_subset({"read"}, {"write"})
    with pytest.raises(keys.Escalation):
        keys.check_subset({"read", "write"}, {"manage"})


def test_a_key_may_mint_an_equal_or_weaker_one():
    keys.check_subset({"manage"}, {"manage"})
    keys.check_subset({"read", "write", "manage"}, {"read"})
    keys.check_subset({"read", "write"}, {"read", "write"})


def test_a_signed_in_person_is_not_subject_to_subsetting():
    # None means "not a key". A person is bounded by their membership.
    keys.check_subset(None, {"read", "write", "manage"})


# ------------------------------ parsing scopes ------------------------------


def test_scopes_are_parsed_not_split():
    """A stored " Read, write" would split into [" Read", " write"] and match
    nothing, silently turning a key somebody meant to be powerful into one that
    can do nothing."""
    assert keys.parse_scopes(" Read, WRITE ") == {"read", "write"}
    assert keys.parse_scopes("read,manage") == {"read", "manage"}


def test_an_unknown_scope_grants_nothing():
    assert keys.parse_scopes("admin,superuser") == set()
    assert keys.parse_scopes("") == set()
    assert keys.parse_scopes(None) == set()
