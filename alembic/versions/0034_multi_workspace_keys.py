"""A key that may reach more than one workspace, by explicit grant.

Until now a key belonged to exactly one workspace and that was the whole
boundary. A platform that studies PATTERNS across its tenants needs one
credential that can read several — and needs it to be impossible for that
credential to read a workspace nobody granted it.

So: a nullable list, not a flag. NULL means the key behaves exactly as every
key does today, reaching only `workspace_id` — the single-workspace path is
untouched, which matters because it is the path every existing key is on. A
populated list is an explicit allow-list, checked on the server for every
request, with `workspace_id` remaining the key's home (its default when no
workspace is named, and the workspace its audit rows belong to).

Nothing is backfilled: no existing key becomes multi-workspace by applying
this.

Revision ID: 0034_multi_workspace_keys
Revises: 0033_rate_limits
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0034_multi_workspace_keys"
down_revision = "0033_rate_limits"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "api_keys",
        sa.Column(
            "workspaces",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment=(
                "Explicit allow-list of workspace ids this key may reach, e.g. "
                '["acme-7a3d9c", "globex-1f2e3d"]. NULL means the key reaches '
                "only its own workspace_id — the behaviour of every key issued "
                "before this column existed."
            ),
        ),
    )


def downgrade() -> None:
    op.drop_column("api_keys", "workspaces")
