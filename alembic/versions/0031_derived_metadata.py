"""Give AI-derived attributes a column of their own, apart from asserted ones.

A separate column, not a key prefix inside `metadata`, and the difference is
the whole point. Retrieval is CONSTRAINED by metadata a caller asserted —
including, for a multi-tenant platform, which client a document belongs to. If
a model's guess could land in that same object, an inferred `client` would be
byte-identical to an asserted one, and nothing downstream could tell them
apart. A convention would not stop that; a different column does.

`derived` therefore has one rule enforced everywhere it is written: it may
never be read as authority, only as a signal for discovery and ranking.

Revision ID: 0031_derived_metadata
Revises: 0030_summaries
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0031_derived_metadata"
down_revision = "0030_summaries"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "kb_items",
        sa.Column(
            "derived",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("kb_items", "derived")
