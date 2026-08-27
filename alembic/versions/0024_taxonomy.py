"""Taxonomy and classification.

The KB owns the filing decision (KB-2.a), so an assignment records HOW it was
reached — a confidence score and the basis — and a human correction is marked
`pinned` so re-classification can never silently revert it (KB-2.b).

Classes are data, not code: adding or renaming one is an INSERT, never a
deployment. They are scoped either platform-wide (tenant_id IS NULL, set by a
platform operator) or to one agency.

Revision ID: 0024_taxonomy
"""
from alembic import op

revision = "0024_taxonomy"
down_revision = "0023_kb_items"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS kb_classes (
            class_id   text        NOT NULL,
            tenant_id  text,                 -- NULL = platform-wide
            parent_id  text,                 -- hierarchy; NULL = top level
            name       text        NOT NULL,
            description text,
            system     boolean     NOT NULL DEFAULT false,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    # A class id is unique within its scope. Expressed as an index rather than a
    # primary key because tenant_id is nullable for platform-wide classes, and
    # NULLs do not compare equal in a normal unique constraint.
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS kb_classes_scope_id
        ON kb_classes (COALESCE(tenant_id, ''), class_id)
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS kb_classes_tenant ON kb_classes (tenant_id)")

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS kb_item_classes (
            item_id     text        NOT NULL,
            class_id    text        NOT NULL,
            tenant_id   text        NOT NULL,
            confidence  real        NOT NULL DEFAULT 0,
            basis       text,
            pinned      boolean     NOT NULL DEFAULT false,
            actor       text,
            assigned_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (item_id, class_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS kb_item_classes_lookup "
        "ON kb_item_classes (tenant_id, class_id)"
    )
    # Finding what needs review: low-confidence automatic assignments.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS kb_item_classes_review
        ON kb_item_classes (tenant_id, confidence) WHERE NOT pinned
        """
    )

    # The base set that ships with the product. Extendable, never deletable —
    # `unfiled` in particular is the fallback that stops anything being dropped
    # or silently misfiled when confidence is too low.
    op.execute(
        """
        INSERT INTO kb_classes (class_id, tenant_id, parent_id, name, description, system)
        VALUES
            ('unfiled',      NULL, NULL, 'Unfiled',
             'Could not be classified with confidence. Needs review.', true),
            ('report',       NULL, NULL, 'Report',      'Generated deliverables and analyses.', true),
            ('research',     NULL, NULL, 'Research',    'Market, audience and competitor research.', true),
            ('strategy',     NULL, NULL, 'Strategy',    'Plans, positioning and recommendations.', true),
            ('creative',     NULL, NULL, 'Creative',    'Copy, assets, briefs and creative direction.', true),
            ('performance',  NULL, NULL, 'Performance', 'Campaign metrics and results.', true),
            ('client',       NULL, NULL, 'Client',      'Client context, notes and correspondence.', true),
            ('operations',   NULL, NULL, 'Operations',  'Process, scheduling and internal records.', true)
        ON CONFLICT DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS kb_item_classes")
    op.execute("DROP TABLE IF EXISTS kb_classes")
