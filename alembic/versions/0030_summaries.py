"""Index summaries — a navigable semantic layer over passages.

A document is entered through its structure: the agent reads a per-document
*card* and per-section *summaries* to decide where to look, rather than hopping
raw cosine neighbours. These summaries are model-written, so — exactly like the
consolidation summaries — they are for NAVIGATION, carry their provenance
(`merged_from` = the chunk ids they cover), and are never cited: retrieval
filters them out so an answer's evidence is always a real passage.

Two node_type values join the existing `fact` and `summary`:
  card             one per document — what it covers, in a few sentences
  section_summary  one per section or probe-mapped cluster

`generated_by` records HOW a summary came to be (`ingest` at upload, `mapper`
from the background probe pass) and `probe_question` keeps the question the
mapper asked — both shown in the Summaries view so the mapping is legible.

The graph carries only navigation (summary nodes + SUMMARIZES edges + vectors);
the text lives here, in Postgres, like every other chunk.

Revision ID: 0030_summaries
"""
from alembic import op

revision = "0030_summaries"
down_revision = "0029_key_collection_and_rename"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE kb_chunks
            -- ingest | mapper (| merge, for consolidation summaries). NULL for
            -- plain facts. Shown in the UI as how a summary was produced.
            ADD COLUMN IF NOT EXISTS generated_by   text,
            -- The mapper's probe question that surfaced the covered chunks.
            -- The legible half of "how this was mapped".
            ADD COLUMN IF NOT EXISTS probe_question text
        """
    )
    # A running log of the probe-mapper: the random question it asked and how
    # many previously-unmapped chunks that pass brought under a summary.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS mapper_runs (
            id            bigserial   PRIMARY KEY,
            workspace_id  text        NOT NULL,
            collection_id text,
            question      text,
            chunks_mapped integer     NOT NULL DEFAULT 0,
            error         text,
            created_at    timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS mapper_runs_recent "
        "ON mapper_runs (workspace_id, created_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS mapper_runs")
    op.execute(
        """
        ALTER TABLE kb_chunks
            DROP COLUMN IF EXISTS generated_by,
            DROP COLUMN IF EXISTS probe_question
        """
    )
