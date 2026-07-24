"""put company_id in the events primary key

An event id comes from the SOURCE, not from us: GitHub calls its fourth issue
`issue_4` in every repo it has ever hosted. The primary key was
(id, timestamp), so the moment two workspaces connected the same repo their
events collided — and because `ON CONFLICT DO UPDATE` never touched
company_id, the row kept whichever tenant inserted it FIRST while its content
was overwritten by the second. One workspace silently ate another's data, and
the loser's Work and Attention screens sat empty with no error anywhere.

That is a tenancy bug, not a dedupe nicety: every other table in this schema
scopes by company_id, and events is the one place a foreign id was trusted to
be globally unique. The fix is the key itself, so the database refuses the
collision instead of resolving it in favour of a stranger.

`timestamp` stays in the key because it is the partition key and Postgres
requires it there. `ix_events_id` goes: no query looks up an event without its
company, and the new key already indexes (company_id, id, timestamp).

Revision ID: 0017_events_tenant_key
Revises: 0016_auth
Create Date: 2026-07-22

"""
from alembic import op

revision = "0017_events_tenant_key"
down_revision = "0016_auth"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Dropping the PK on a partitioned table cascades to every partition.
    op.execute("ALTER TABLE events DROP CONSTRAINT events_pkey")
    op.execute("ALTER TABLE events ADD PRIMARY KEY (company_id, id, timestamp)")
    op.execute("DROP INDEX IF EXISTS ix_events_id")


def downgrade() -> None:
    op.execute("ALTER TABLE events DROP CONSTRAINT events_pkey")
    op.execute("ALTER TABLE events ADD PRIMARY KEY (id, timestamp)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_events_id ON events (id)")
