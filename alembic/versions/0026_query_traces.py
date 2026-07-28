"""Query traces.

Every retrieval records what it did: the query, the configuration it resolved
to, what each arm found and scored, what survived the filters, what came back,
and how long each stage took.

This is the difference between a store you trust and one you hope about. When
an answer is wrong the first useful question is "why did it return that?", and
without a trace the only available answers are guesses.

Traces are written on the read path, so they are deliberately cheap: capped
candidate lists, no document bodies, one insert.

Revision ID: 0026_query_traces
"""
from alembic import op

revision = "0026_query_traces"
down_revision = "0025_clusters_collections_keys"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS query_traces (
            trace_id      text        PRIMARY KEY,
            workspace_id  text        NOT NULL,
            collection_id text,
            query         text        NOT NULL,
            -- The configuration the call actually resolved to, not the one it
            -- was asked for: retrieval behaviour changing is the usual cause of
            -- output quality changing, and this is what makes that traceable.
            config        jsonb       NOT NULL DEFAULT '{}'::jsonb,
            filters       jsonb       NOT NULL DEFAULT '{}'::jsonb,
            -- What each arm proposed, and what survived fusion.
            semantic      jsonb       NOT NULL DEFAULT '[]'::jsonb,
            keyword       jsonb       NOT NULL DEFAULT '[]'::jsonb,
            fused         jsonb       NOT NULL DEFAULT '[]'::jsonb,
            returned      jsonb       NOT NULL DEFAULT '[]'::jsonb,
            timings_ms    jsonb       NOT NULL DEFAULT '{}'::jsonb,
            -- Set when an arm could not run. A degraded answer that looks
            -- identical to a healthy one is the worst kind.
            degraded      text,
            result_count  integer     NOT NULL DEFAULT 0,
            duration_ms   integer     NOT NULL DEFAULT 0,
            via           text        NOT NULL DEFAULT 'session',
            actor         text,
            created_at    timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS query_traces_recent "
        "ON query_traces (workspace_id, created_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS query_traces_collection "
        "ON query_traces (workspace_id, collection_id, created_at DESC)"
    )
    # Finding the bad ones: slow, empty, or degraded.
    op.execute(
        "CREATE INDEX IF NOT EXISTS query_traces_empty "
        "ON query_traces (workspace_id, created_at DESC) WHERE result_count = 0"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS query_traces")
