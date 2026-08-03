"""api keys can be bound to a single collection

Until now an API key carried a workspace and nothing else, so every key could
read and write every collection in that workspace. That is the right default
for a console session, but wrong for a credential handed to a CI job or a
third party: it should be able to name one collection and be unable to touch
any other.

A nullable `collection_id` on `api_keys` expresses exactly that. NULL keeps the
old meaning — the key is workspace-wide — so every existing key keeps working
unchanged. A value binds the key: the request layer forces its scope to that
collection and refuses any other. No foreign key, matching the rest of
`api_keys`; the create path validates the collection against the workspace,
which is where the workspace is known.

Renaming a collection needs no schema change — `collections.name` already
exists and has always been mutable.

Revision ID: 0029_key_collection_and_rename
Revises: 0028_consolidation
Create Date: 2026-08-03

"""
from alembic import op

revision = "0029_key_collection_and_rename"
down_revision = "0028_consolidation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS collection_id text")


def downgrade() -> None:
    op.execute("ALTER TABLE api_keys DROP COLUMN IF EXISTS collection_id")
