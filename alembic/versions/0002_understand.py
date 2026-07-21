"""understand layer: norm baselines

The knowledge graph (things, event mirrors, typed links) lives in Neo4j —
created idempotently by core.graph.bootstrap(), not by a Postgres migration.
Norm baselines stay here: they are relational rollups over events.

Revision ID: 0002_understand
Revises: 0001_initial
Create Date: 2026-07-10

"""
from alembic import op

revision = "0002_understand"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS norm_baselines (
            company_id  TEXT NOT NULL DEFAULT 'default',
            metric      TEXT NOT NULL,
            unit        TEXT NOT NULL,
            n           INTEGER NOT NULL,
            median      DOUBLE PRECISION NOT NULL,
            mean        DOUBLE PRECISION NOT NULL,
            std         DOUBLE PRECISION NOT NULL,
            window_days INTEGER NOT NULL,
            computed_at TIMESTAMPTZ NOT NULL,
            PRIMARY KEY (company_id, metric)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS norm_baselines")
