"""tickets + single-use action tokens for one-click email approvals

Revision ID: 0006_tickets_tokens
Revises: 0005_settings
Create Date: 2026-07-10

"""
from alembic import op

revision = "0006_tickets_tokens"
down_revision = "0005_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # One-click links in an email must be single-use and expiring. The token
    # itself is sealed (Fernet); this table makes it un-replayable.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS action_tokens (
            jti        TEXT PRIMARY KEY,
            company_id TEXT NOT NULL,
            purpose    TEXT NOT NULL,
            expires_at TIMESTAMPTZ NOT NULL,
            used_at    TIMESTAMPTZ,
            used_by    TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS tickets (
            id              BIGSERIAL PRIMARY KEY,
            company_id      TEXT NOT NULL DEFAULT 'default',
            situation_id    TEXT,
            title           TEXT NOT NULL,
            description     TEXT NOT NULL DEFAULT '',
            assignee        TEXT NOT NULL,
            status          TEXT NOT NULL DEFAULT 'open',
            source_event_id TEXT,
            external_url    TEXT,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            closed_at       TIMESTAMPTZ
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_tickets_company ON tickets (company_id, status)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS tickets")
    op.execute("DROP TABLE IF EXISTS action_tokens")
