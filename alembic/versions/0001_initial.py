"""initial schema: partitioned events + credentials + audit

Runs on PLAIN postgres:16 — no pgvector. Embeddings live in Neo4j
(:Event.embedding + a native vector index), created by core.graph.bootstrap().

Revision ID: 0001_initial
Revises:
Create Date: 2026-07-10

"""
from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Append-only events, partitioned by month on timestamp. PK includes the
    # partition key (id, timestamp) as Postgres requires.
    op.execute(
        """
        CREATE TABLE events (
            id          TEXT NOT NULL,
            company_id  TEXT NOT NULL DEFAULT 'default',
            source      TEXT NOT NULL,
            type        TEXT NOT NULL,
            actor_id    TEXT NOT NULL,
            actor_name  TEXT NOT NULL,
            actor_email TEXT,
            timestamp   TIMESTAMPTZ NOT NULL,
            content     TEXT NOT NULL,
            metadata    JSONB NOT NULL DEFAULT '{}'::jsonb,
            raw         JSONB NOT NULL DEFAULT '{}'::jsonb,
            content_tsv TSVECTOR,
            PRIMARY KEY (id, timestamp)
        ) PARTITION BY RANGE (timestamp)
        """
    )

    # Monthly partitions 2024-01 .. 2027-12, plus a DEFAULT catch-all.
    op.execute(
        """
        DO $$
        DECLARE d date := '2024-01-01';
        BEGIN
          WHILE d < '2028-01-01' LOOP
            EXECUTE format(
              'CREATE TABLE IF NOT EXISTS events_%s PARTITION OF events '
              'FOR VALUES FROM (%L) TO (%L)',
              to_char(d, 'YYYY_MM'), d::timestamptz, (d + interval '1 month')::timestamptz
            );
            d := d + interval '1 month';
          END LOOP;
        END $$;
        """
    )
    op.execute("CREATE TABLE IF NOT EXISTS events_default PARTITION OF events DEFAULT")

    op.execute("CREATE INDEX ix_events_company ON events (company_id)")
    op.execute("CREATE INDEX ix_events_id ON events (id)")
    op.execute("CREATE INDEX ix_events_tsv ON events USING GIN (content_tsv)")

    # Encrypted connector credentials (tokens sealed via core.crypto — never plaintext).
    op.execute(
        """
        CREATE TABLE credentials (
            company_id   TEXT NOT NULL,
            source       TEXT NOT NULL,
            sealed_token TEXT NOT NULL,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (company_id, source)
        )
        """
    )

    # Append-only audit log.
    op.execute(
        """
        CREATE TABLE audit_log (
            id         BIGSERIAL PRIMARY KEY,
            company_id TEXT NOT NULL,
            actor      TEXT NOT NULL,
            action     TEXT NOT NULL,
            target     TEXT NOT NULL DEFAULT '',
            metadata   JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ix_audit_company ON audit_log (company_id)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS audit_log")
    op.execute("DROP TABLE IF EXISTS credentials")
    op.execute("DROP TABLE IF EXISTS events")
