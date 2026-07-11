"""situations can be resolved when their trigger no longer holds

Revision ID: 0004_resolve_situations
Revises: 0003_alert_act
Create Date: 2026-07-10

"""
from alembic import op

revision = "0004_resolve_situations"
down_revision = "0003_alert_act"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE situations ADD COLUMN IF NOT EXISTS resolved_at TIMESTAMPTZ")


def downgrade() -> None:
    op.execute("ALTER TABLE situations DROP COLUMN IF EXISTS resolved_at")
