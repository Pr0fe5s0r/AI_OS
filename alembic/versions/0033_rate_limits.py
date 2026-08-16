"""Per-key rate limit overrides.

One JSONB column rather than ten sparse ones. There are five cost classes and
two dials each, and a schema with ten mostly-null columns is one nobody keeps
coherent — a class added later would need another migration, and a key that
overrides nothing would carry ten NULLs to say so.

NULL means "use the environment default". That distinction matters: a key with
no opinion must not be a key whose limit is zero, so the column is nullable and
absence is read as absence rather than as a value.

Nothing is backfilled. Every existing key keeps exactly the limits the
deployment's environment gives it, which before this migration was no limit at
all — so applying it changes no key's behaviour until an operator sets one.

Revision ID: 0033_rate_limits
Revises: 0032_manage_scope
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0033_rate_limits"
down_revision = "0032_manage_scope"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "api_keys",
        sa.Column(
            "rate_limits",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment=(
                "Per-class rate limit overrides, e.g. "
                '{"agentic_per_min": 12, "agentic_concurrent": 4}. '
                "NULL or a missing entry means the environment default."
            ),
        ),
    )


def downgrade() -> None:
    op.drop_column("api_keys", "rate_limits")
