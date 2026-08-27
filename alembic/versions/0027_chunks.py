"""Passages.

A document was embedded whole. A 22,000-character specification became one
1536-float point — the average of everything it said, which matches nothing it
said — and the collection graph had one node per file, so there was no
structure in it to look at.

The passage is now the unit of retrieval: what gets embedded, what gets
recalled, what gets cited, and what appears in the graph. Documents remain the
unit of identity and versioning; passages hang off them and are rebuilt
wholesale whenever a version's content changes, so they can never disagree
with the document they came from.

Deliberately not versioned themselves. A passage has no meaning apart from the
document version that produced it, so the (item_id, version) it belongs to is
the whole of its lineage — and superseding a document drops its passages
rather than accumulating dead text nobody can reach.

Revision ID: 0027_chunks
"""
from alembic import op

revision = "0027_chunks"
down_revision = "0026_query_traces"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS kb_chunks (
            chunk_id      text        PRIMARY KEY,
            workspace_id  text        NOT NULL,
            collection_id text,
            item_id       text        NOT NULL,
            -- Which version produced this passage. Passages for a superseded
            -- version are deleted, so this is a guard, not a history.
            version       integer     NOT NULL,
            ordinal       integer     NOT NULL,
            -- The heading path in force where the passage starts, e.g.
            -- "4. Functional Requirements > KB-3. Ingestion". This is what a
            -- citation shows, and it is why a passage can be quoted without
            -- the reader having to open the document to find out what it is
            -- talking about.
            heading       text        NOT NULL DEFAULT '',
            text          text        NOT NULL,
            content_tsv   tsvector,
            created_at    timestamptz NOT NULL DEFAULT now(),
            UNIQUE (workspace_id, item_id, version, ordinal)
        )
        """
    )
    # The keyword arm searches passages, not documents: matching a whole file
    # tells you the file mentions a word somewhere, which is not an answer.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION kb_chunks_tsv() RETURNS trigger AS $$
        BEGIN
            NEW.content_tsv :=
                setweight(to_tsvector('english', coalesce(NEW.heading, '')), 'A') ||
                setweight(to_tsvector('english', coalesce(NEW.text, '')), 'B');
            RETURN NEW;
        END
        $$ LANGUAGE plpgsql
        """
    )
    op.execute("DROP TRIGGER IF EXISTS kb_chunks_tsv_trigger ON kb_chunks")
    op.execute(
        """
        CREATE TRIGGER kb_chunks_tsv_trigger
        BEFORE INSERT OR UPDATE ON kb_chunks
        FOR EACH ROW EXECUTE FUNCTION kb_chunks_tsv()
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS kb_chunks_search ON kb_chunks USING gin (content_tsv)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS kb_chunks_scope "
        "ON kb_chunks (workspace_id, collection_id)"
    )
    # Rebuilding a document's passages deletes by item first, every time.
    op.execute(
        "CREATE INDEX IF NOT EXISTS kb_chunks_item ON kb_chunks (workspace_id, item_id)"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS kb_chunks_tsv_trigger ON kb_chunks")
    op.execute("DROP FUNCTION IF EXISTS kb_chunks_tsv()")
    op.execute("DROP TABLE IF EXISTS kb_chunks")
