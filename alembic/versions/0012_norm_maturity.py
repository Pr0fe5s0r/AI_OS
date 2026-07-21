"""trend-aware, outlier-trimmed norms: maturity + trend columns (checkpoint 2, part B)

Revision ID: 0012_norm_maturity
Revises: 0011_backfill
Create Date: 2026-07-17

"""
from alembic import op

revision = "0012_norm_maturity"
down_revision = "0011_backfill"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE norm_baselines ADD COLUMN IF NOT EXISTS trend_per_period DOUBLE PRECISION NOT NULL DEFAULT 0"
    )
    op.execute(
        "ALTER TABLE norm_baselines ADD COLUMN IF NOT EXISTS maturity TEXT NOT NULL DEFAULT 'insufficient'"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE norm_baselines DROP COLUMN IF EXISTS maturity")
    op.execute("ALTER TABLE norm_baselines DROP COLUMN IF EXISTS trend_per_period")
