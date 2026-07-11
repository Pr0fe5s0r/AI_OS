"""understand layer: nodes + edges + norm baselines

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
        CREATE TABLE IF NOT EXISTS nodes (
            id         TEXT PRIMARY KEY,
            company_id TEXT NOT NULL DEFAULT 'default',
            type       TEXT NOT NULL,
            key        TEXT NOT NULL,
            label      TEXT NOT NULL,
            source     TEXT,
            metadata   JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_nodes_company ON nodes (company_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_nodes_company_key ON nodes (company_id, key)")

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS edges (
            id         BIGSERIAL PRIMARY KEY,
            company_id TEXT NOT NULL DEFAULT 'default',
            src_id     TEXT NOT NULL REFERENCES nodes (id) ON DELETE CASCADE,
            dst_id     TEXT NOT NULL REFERENCES nodes (id) ON DELETE CASCADE,
            type       TEXT NOT NULL,
            weight     DOUBLE PRECISION NOT NULL DEFAULT 1.0,
            metadata   JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_edge UNIQUE (company_id, src_id, dst_id, type)
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_edges_src ON edges (company_id, src_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_edges_dst ON edges (company_id, dst_id)")

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
    op.execute("DROP TABLE IF EXISTS edges")
    op.execute("DROP TABLE IF EXISTS nodes")
