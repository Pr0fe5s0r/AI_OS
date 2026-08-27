"""data deletion: deletion_requests table (checkpoint 6, part C)

Written BEFORE core.delete_company() does any work and updated as each
store (Neo4j, Redis, Postgres) completes, so a partial failure is visible
and resumable instead of silently incomplete.

Revision ID: 0010_deletion_requests
Revises: 0009_clarifications
Create Date: 2026-07-16

"""
from alembic import op

revision = "0010_deletion_requests"
down_revision = "0009_clarifications"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS deletion_requests (
            id                  BIGSERIAL PRIMARY KEY,
            company_id          TEXT NOT NULL,
            status              TEXT NOT NULL DEFAULT 'pending_confirmation'
                                CHECK (status IN ('pending_confirmation', 'queued', 'running',
                                                   'completed', 'failed')),
            confirmation_token  TEXT NOT NULL,
            audit_policy        TEXT NOT NULL DEFAULT 'anonymize'
                                CHECK (audit_policy IN ('anonymize', 'purge')),
            requested_by        TEXT NOT NULL DEFAULT 'ui',
            requested_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
            confirmed_at        TIMESTAMPTZ,
            completed_at        TIMESTAMPTZ,
            neo4j_done          BOOLEAN NOT NULL DEFAULT false,
            redis_done          BOOLEAN NOT NULL DEFAULT false,
            postgres_done       BOOLEAN NOT NULL DEFAULT false,
            error               TEXT,
            result              JSONB NOT NULL DEFAULT '{}'::jsonb
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_deletion_requests_company "
        "ON deletion_requests (company_id, requested_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS deletion_requests")
