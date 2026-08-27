"""Self-organising memory over passages.

A background pass consolidates the store the way StixDB does: passages that say
the same thing are merged into a summary node, exact duplicates collapse, and
archived nodes decay on a half-life until they are dropped. The graph therefore
changes shape on its own — raw passages give way to hubs, hubs stabilise into
long-term nodes.

What this means, stated plainly because it is a real consequence: a retrieval
can return a node whose text was WRITTEN by a model rather than lifted from a
document. `node_type` and `merged_from` exist so that is always visible and
always traceable back to the passages it came from — a summary that cannot name
its sources is a claim with no evidence behind it.

The uploaded document itself is untouched. Passages are derived from
`kb_items.body`, so a rebuild regenerates them from source no matter what
consolidation did.

Revision ID: 0028_consolidation
"""
from alembic import op

revision = "0028_consolidation"
down_revision = "0027_chunks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE kb_chunks
            -- fact: text as it appears in a document.
            -- summary: written by a model from several facts.
            ADD COLUMN IF NOT EXISTS node_type   text NOT NULL DEFAULT 'fact',
            -- 0..1. Rises when a node is retrieved, falls once archived.
            ADD COLUMN IF NOT EXISTS importance  real NOT NULL DEFAULT 0.5,
            -- 1 raw · 2 connected · 3 working-memory hub · 4 long-term.
            -- Derived, stored so the graph can colour by it without recomputing.
            ADD COLUMN IF NOT EXISTS stage       smallint NOT NULL DEFAULT 1,
            ADD COLUMN IF NOT EXISTS access_count integer NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS last_accessed_at timestamptz,
            -- Archived nodes are excluded from retrieval and decay from here.
            ADD COLUMN IF NOT EXISTS archived_at timestamptz,
            -- The chunk_ids a summary was built from. Provenance: a summary
            -- that cannot name its sources is a claim with no evidence.
            ADD COLUMN IF NOT EXISTS merged_from jsonb NOT NULL DEFAULT '[]'::jsonb,
            -- Exact-duplicate detection, cheaper and surer than similarity.
            ADD COLUMN IF NOT EXISTS text_hash   text,
            ADD COLUMN IF NOT EXISTS cycles      integer NOT NULL DEFAULT 0
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS kb_chunks_live "
        "ON kb_chunks (workspace_id, collection_id) WHERE archived_at IS NULL"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS kb_chunks_archived "
        "ON kb_chunks (archived_at) WHERE archived_at IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS kb_chunks_dupes ON kb_chunks (workspace_id, text_hash)"
    )
    # A summary belongs to the collection, not to one document, so item_id is
    # allowed to be absent for it.
    op.execute("ALTER TABLE kb_chunks ALTER COLUMN item_id DROP NOT NULL")
    op.execute("ALTER TABLE kb_chunks ALTER COLUMN version DROP NOT NULL")

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS consolidation_runs (
            id            bigserial PRIMARY KEY,
            workspace_id  text        NOT NULL,
            collection_id text,
            merged        integer     NOT NULL DEFAULT 0,
            deduped       integer     NOT NULL DEFAULT 0,
            decayed       integer     NOT NULL DEFAULT 0,
            dropped       integer     NOT NULL DEFAULT 0,
            promoted      integer     NOT NULL DEFAULT 0,
            duration_ms   integer     NOT NULL DEFAULT 0,
            error         text,
            created_at    timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS consolidation_runs_recent "
        "ON consolidation_runs (workspace_id, created_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS consolidation_runs")
    op.execute(
        """
        ALTER TABLE kb_chunks
            DROP COLUMN IF EXISTS node_type,
            DROP COLUMN IF EXISTS importance,
            DROP COLUMN IF EXISTS stage,
            DROP COLUMN IF EXISTS access_count,
            DROP COLUMN IF EXISTS last_accessed_at,
            DROP COLUMN IF EXISTS archived_at,
            DROP COLUMN IF EXISTS merged_from,
            DROP COLUMN IF EXISTS text_hash,
            DROP COLUMN IF EXISTS cycles
        """
    )
