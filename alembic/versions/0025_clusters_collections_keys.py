"""Clusters, collections and API keys.

The store stops being one flat index per tenant and takes the shape a vector
database has: a workspace holds clusters, a cluster holds collections, and a
collection holds items. A collection is the unit that carries its own
embedding model and dimensions, because that is the setting you cannot change
without re-embedding everything under it.

`brands` becomes `collections` — a client brand was only ever one kind of
namespace, and naming it after one customer's vertical was what stopped the
same engine serving anyone else.

API keys arrive here too. Until now the only way in was a browser cookie,
which means nothing programmatic — no SDK, no MCP, no CI job — could talk to
the store at all.

Revision ID: 0025_clusters_collections_keys
"""
from alembic import op

revision = "0025_clusters_collections_keys"
down_revision = "0024_taxonomy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---------------------------- the hierarchy ----------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS clusters (
            workspace_id text        NOT NULL,
            cluster_id   text        NOT NULL,
            name         text        NOT NULL,
            region       text        NOT NULL DEFAULT 'local',
            created_at   timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (workspace_id, cluster_id)
        )
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS collections (
            workspace_id    text        NOT NULL,
            collection_id   text        NOT NULL,
            cluster_id      text        NOT NULL,
            name            text        NOT NULL,
            description     text,
            -- Fixed at creation: changing either means re-embedding every item
            -- in the collection, which is a migration and not a setting.
            embedding_model text        NOT NULL DEFAULT '',
            dimensions      integer     NOT NULL DEFAULT 1536,
            created_at      timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (workspace_id, collection_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS collections_cluster "
        "ON collections (workspace_id, cluster_id)"
    )

    # Carry over anything that already exists as a brand, so no data is
    # stranded by the rename.
    op.execute(
        """
        INSERT INTO clusters (workspace_id, cluster_id, name)
        SELECT DISTINCT tenant_id, 'default', 'Default cluster' FROM brands
        ON CONFLICT DO NOTHING
        """
    )
    op.execute(
        """
        INSERT INTO collections (workspace_id, collection_id, cluster_id, name)
        SELECT tenant_id, brand_id, 'default', name FROM brands
        ON CONFLICT DO NOTHING
        """
    )
    op.execute("DROP TABLE IF EXISTS brands")

    # ------------------------------ the rename ------------------------------
    # tenant -> workspace, brand -> collection. One name per concept, applied
    # to the columns as well as the API, so the two never drift apart.
    for table in ("kb_items", "kb_classes", "kb_item_classes"):
        op.execute(f"ALTER TABLE {table} RENAME COLUMN tenant_id TO workspace_id")
    op.execute("ALTER TABLE kb_items RENAME COLUMN brand_id TO collection_id")

    op.execute("DROP INDEX IF EXISTS kb_items_scope")
    op.execute(
        "CREATE INDEX IF NOT EXISTS kb_items_scope "
        "ON kb_items (workspace_id, collection_id, status)"
    )

    # -------------------------------- keys --------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS api_keys (
            key_id       text        PRIMARY KEY,
            workspace_id text        NOT NULL,
            name         text        NOT NULL,
            -- Only the hash is stored. The key itself is shown once, at
            -- creation, and cannot be recovered afterwards.
            key_hash     text        NOT NULL UNIQUE,
            prefix       text        NOT NULL,
            scopes       text        NOT NULL DEFAULT 'read,write',
            created_by   text,
            created_at   timestamptz NOT NULL DEFAULT now(),
            last_used_at timestamptz,
            revoked_at   timestamptz
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS api_keys_workspace ON api_keys (workspace_id)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS api_keys")
    op.execute("ALTER TABLE kb_items RENAME COLUMN collection_id TO brand_id")
    for table in ("kb_items", "kb_classes", "kb_item_classes"):
        op.execute(f"ALTER TABLE {table} RENAME COLUMN workspace_id TO tenant_id")
    op.execute("DROP TABLE IF EXISTS collections")
    op.execute("DROP TABLE IF EXISTS clusters")
