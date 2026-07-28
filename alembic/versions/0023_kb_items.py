"""The item index and two-level tenancy.

The knowledge base holds one kind of thing: an Item. This creates the
catalogue for it, plus the brand table that gives tenancy its second level
(agency -> client brand).

Identity and versioning are enforced here rather than trusted to the
application: (item_id, version) is the key, and a partial unique index
guarantees exactly one ACTIVE version per item — so "which one is current?"
can never have two answers, whatever a concurrent writer does.

Revision ID: 0023_kb_items
"""
from alembic import op

revision = "0023_kb_items"
down_revision = "0022_record_triage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Second level of tenancy. An agency is a company row; a brand lives under
    # one and never spans two.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS brands (
            tenant_id  text        NOT NULL,
            brand_id   text        NOT NULL,
            name       text        NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, brand_id)
        )
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS kb_items (
            item_id      text        NOT NULL,
            version      integer     NOT NULL,
            tenant_id    text        NOT NULL,
            brand_id     text,
            title        text        NOT NULL,
            body         text        NOT NULL,
            body_tsv     tsvector,
            source       text        NOT NULL,
            locator      text        NOT NULL,
            url          text,
            hash         text        NOT NULL,
            supersedes   text,
            status       text        NOT NULL DEFAULT 'active',
            created_at   timestamptz NOT NULL DEFAULT now(),
            updated_at   timestamptz NOT NULL DEFAULT now(),
            period_start timestamptz,
            period_end   timestamptz,
            metadata     jsonb       NOT NULL DEFAULT '{}'::jsonb,
            PRIMARY KEY (item_id, version)
        )
        """
    )

    # Exactly one current version per item — the integrity rule behind KB-1's
    # "is this current?" acceptance test.
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS kb_items_one_active
        ON kb_items (item_id) WHERE status = 'active'
        """
    )
    # Keyword recall.
    op.execute("CREATE INDEX IF NOT EXISTS kb_items_tsv ON kb_items USING GIN (body_tsv)")
    # The listing/filtering path (KB-1) and every tenant-scoped read.
    op.execute(
        "CREATE INDEX IF NOT EXISTS kb_items_scope ON kb_items (tenant_id, brand_id, status)"
    )
    # Sync's lookup: "do we already hold this source location?"
    op.execute(
        "CREATE INDEX IF NOT EXISTS kb_items_origin ON kb_items (tenant_id, source, locator)"
    )
    # Dedup probes by content.
    op.execute("CREATE INDEX IF NOT EXISTS kb_items_hash ON kb_items (tenant_id, hash)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS kb_items")
    op.execute("DROP TABLE IF EXISTS brands")
