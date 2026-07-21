"""backfill: tag events pulled from history vs. live sync (checkpoint 2, part A)

Revision ID: 0011_backfill
Revises: 0010_deletion_requests
Create Date: 2026-07-17

"""
from alembic import op

revision = "0011_backfill"
down_revision = "0010_deletion_requests"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS backfilled BOOLEAN NOT NULL DEFAULT false")
    # the watcher engine (CP2 part C) evaluates live events only
    op.execute("CREATE INDEX IF NOT EXISTS ix_events_backfilled ON events (company_id, backfilled)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_events_backfilled")
    op.execute("ALTER TABLE events DROP COLUMN IF EXISTS backfilled")
