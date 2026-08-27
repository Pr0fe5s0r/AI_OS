"""connector health monitoring: self-monitoring checkpoint, part A

Revision ID: 0008_connector_health
Revises: 0007_profiles
Create Date: 2026-07-15

"""
from alembic import op

revision = "0008_connector_health"
down_revision = "0007_profiles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # One row per (connector_type, company_id). Updated by the ingestion
    # pipeline (successes/schema failures) and by the 15-min health cron
    # (volume anomaly + field-completeness rollup -> status).
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS connector_health (
            id                  BIGSERIAL PRIMARY KEY,
            connector_type      TEXT NOT NULL,
            company_id          TEXT NOT NULL,
            last_success_at     TIMESTAMPTZ,
            consecutive_failures INTEGER NOT NULL DEFAULT 0,
            last_schema_error   TEXT,
            events_per_hour_norm DOUBLE PRECISION,
            field_completeness  JSONB NOT NULL DEFAULT '{}'::jsonb,
            status              TEXT NOT NULL DEFAULT 'healthy'
                                CHECK (status IN ('healthy', 'degraded', 'broken')),
            updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_connector_health UNIQUE (connector_type, company_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_connector_health_company ON connector_health (company_id)"
    )

    # Distinguish business rhythms (issue_resolution_hours...) from system
    # self-monitoring metrics (github_events_per_hour...) sharing the same
    # learn_norms()/norm_baselines machinery.
    op.execute(
        "ALTER TABLE norm_baselines ADD COLUMN IF NOT EXISTS scope TEXT NOT NULL DEFAULT 'business'"
    )

    # A dimension separate from severity: what KIND of thing is this
    # situation. 'system' situations are about the OS's own health, not the
    # customer's business, and are hidden from the default feed.
    op.execute(
        "ALTER TABLE situations ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'business'"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_situations_kind ON situations (company_id, kind)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_situations_kind")
    op.execute("ALTER TABLE situations DROP COLUMN IF EXISTS kind")
    op.execute("ALTER TABLE norm_baselines DROP COLUMN IF EXISTS scope")
    op.execute("DROP TABLE IF EXISTS connector_health")
