"""runtime settings (dry_run toggle), company-scoped

Revision ID: 0005_settings
Revises: 0004_resolve_situations
Create Date: 2026-07-10

"""
from alembic import op

revision = "0005_settings"
down_revision = "0004_resolve_situations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            company_id TEXT NOT NULL DEFAULT 'default',
            key        TEXT NOT NULL,
            value      JSONB NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (company_id, key)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS settings")
