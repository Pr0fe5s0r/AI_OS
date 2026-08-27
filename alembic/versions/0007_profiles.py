"""profiles: the domain profile as DATA — one row per company, seven JSONB slots

Revision ID: 0007_profiles
Revises: 0006_tickets_tokens
Create Date: 2026-07-13

"""
from alembic import op

revision = "0007_profiles"
down_revision = "0006_tickets_tokens"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # slots = {sources, things, links, rhythms, watchers, moves, vocabulary}.
    # status: 'proposed' (discovery engine / unconfirmed edits) | 'confirmed'.
    # The engine loads the highest confirmed version per company.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS profiles (
            id         BIGSERIAL PRIMARY KEY,
            company_id TEXT NOT NULL,
            version    INTEGER NOT NULL DEFAULT 1,
            status     TEXT NOT NULL DEFAULT 'proposed'
                       CHECK (status IN ('proposed', 'confirmed')),
            slots      JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_profile_version UNIQUE (company_id, version)
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_profiles_company ON profiles (company_id, status)")

    # companies: minimal registry so a profile row has something to hang off.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS companies (
            id         TEXT PRIMARY KEY,
            name       TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS profiles")
    op.execute("DROP TABLE IF EXISTS companies")
