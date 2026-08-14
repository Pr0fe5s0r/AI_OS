"""Grant `manage` only to keys that already hold the powers it names.

`manage` administers collections and keys. Before it existed, every one of
those operations was gated on `write`:

    POST   /api/clusters                    require_write
    POST   /api/collections                 require_write
    PATCH  /api/collections/{id}            require_write
    DELETE /api/collections/{id}            require_write
    POST   /api/keys                        require_write
    DELETE /api/keys/{key_id}               require_write

So the permission-preserving rule is exact and needs no guesswork: a key that
holds `write` today can already do all of it, and a key that does not, cannot.
Adding `manage` to everything would hand collection administration and key
minting to read-only keys that have never had either.

One deliberate NARROWING, and it is not an accident. A collection-bound key is
not a full-trust key — it was issued to reach exactly one tenant — so it does
not receive `manage` even if it holds `write`. Today such a key can create
collections and mint keys, which is a privilege-escalation path out of the
binding that was supposed to contain it: bound to collection A, mint an
unbound key, read collection B. Closing that is the point of the exercise, and
leaving it open for existing keys would leave the door it was built to shut.

Every key is REPORTED before it is touched, so a deployment can see in its
migration log which keys were widened and which were left alone.

Revision ID: 0032_manage_scope
Revises: 0031_derived_metadata
"""

from __future__ import annotations

import logging

from alembic import op

revision = "0032_manage_scope"
down_revision = "0031_derived_metadata"
branch_labels = None
depends_on = None

log = logging.getLogger("alembic.runtime.migration")

# A full-trust key: workspace-wide, still live, and already able to manage.
_FULL_TRUST = """
    workspace_id IS NOT NULL
    AND collection_id IS NULL
    AND revoked_at IS NULL
    AND scopes ~ '(^|,)\\s*write\\s*(,|$)'
    AND scopes !~ '(^|,)\\s*manage\\s*(,|$)'
"""


def upgrade() -> None:
    bind = op.get_bind()

    # Inspect first, and say what was found. A migration that changes a
    # permission silently is one nobody can audit afterwards.
    for row in bind.exec_driver_sql(
        "SELECT key_id, name, scopes, collection_id, revoked_at FROM api_keys"
    ).mappings():
        if row["revoked_at"] is not None:
            verdict = "left alone (revoked)"
        elif row["collection_id"] is not None:
            verdict = f"left alone (bound to {row['collection_id']!r}, not full trust)"
        elif "write" not in (row["scopes"] or ""):
            verdict = "left alone (no write scope — cannot manage today either)"
        else:
            verdict = "GRANTED manage (workspace-wide key already holding write)"
        log.info("api key %s %r [%s]: %s", row["key_id"], row["name"], row["scopes"], verdict)

    granted = bind.exec_driver_sql(
        f"UPDATE api_keys SET scopes = scopes || ',manage' WHERE {_FULL_TRUST}"  # noqa: S608
    ).rowcount
    log.info("manage granted to %s existing key(s)", granted or 0)


def downgrade() -> None:
    op.execute(
        r"""
        UPDATE api_keys
           SET scopes = regexp_replace(scopes, '(^|,)\s*manage\s*(?=,|$)', '', 'g')
         WHERE scopes ~ '(^|,)\s*manage\s*(,|$)'
        """
    )
