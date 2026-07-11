"""alert + act layers: connections config, situations, actions

Revision ID: 0003_alert_act
Revises: 0002_understand
Create Date: 2026-07-10

"""
from alembic import op

revision = "0003_alert_act"
down_revision = "0002_understand"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # connector config (repo, subdomain, channel...) alongside the sealed token
    op.execute("ALTER TABLE credentials ADD COLUMN IF NOT EXISTS config JSONB NOT NULL DEFAULT '{}'::jsonb")

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS situations (
            id                 TEXT PRIMARY KEY,
            company_id         TEXT NOT NULL DEFAULT 'default',
            rule               TEXT NOT NULL,
            severity           TEXT NOT NULL,
            title              TEXT NOT NULL,
            summary            TEXT NOT NULL DEFAULT '',
            recommended_action TEXT,
            evidence           JSONB NOT NULL DEFAULT '[]'::jsonb,
            status             TEXT NOT NULL DEFAULT 'open',
            channel            TEXT,
            recipient          TEXT,
            delivered_at       TIMESTAMPTZ,
            created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_situations_company ON situations (company_id, created_at DESC)")

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS actions (
            id           BIGSERIAL PRIMARY KEY,
            company_id   TEXT NOT NULL DEFAULT 'default',
            situation_id TEXT,
            action       TEXT NOT NULL,
            params       JSONB NOT NULL DEFAULT '{}'::jsonb,
            status       TEXT NOT NULL,
            result       JSONB NOT NULL DEFAULT '{}'::jsonb,
            detail       TEXT NOT NULL DEFAULT '',
            requested_by TEXT NOT NULL DEFAULT 'system',
            decided_by   TEXT,
            requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            decided_at   TIMESTAMPTZ
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_actions_company ON actions (company_id, requested_at DESC)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS actions")
    op.execute("DROP TABLE IF EXISTS situations")
    op.execute("ALTER TABLE credentials DROP COLUMN IF EXISTS config")
