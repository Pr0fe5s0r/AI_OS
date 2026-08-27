"""remember that a record CHANGED, not just what it is now

Ingest overwrites: one row per record, holding its latest snapshot. That is
right for "what is true now" and useless for the question people actually ask
about work — *did this move, and when?* A pull request that went open →
merged, or an issue reassigned twice, left no trace at all: the row simply
read differently the next time anyone looked.

So this is the audit trail for the outside world, the counterpart to
audit_log's trail for our own actions. One row per observed change, written by
store_event when an incoming snapshot disagrees with the stored one.

`observed_at` is when WE saw it, not when it happened — we poll, so those are
different, and pretending otherwise would invent precision we do not have.

Revision ID: 0018_record_transitions
Revises: 0017_events_tenant_key
Create Date: 2026-07-23

"""
from alembic import op

revision = "0018_record_transitions"
down_revision = "0017_events_tenant_key"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE record_transitions (
            id          bigserial PRIMARY KEY,
            company_id  text        NOT NULL,
            record_id   text        NOT NULL,
            source      text        NOT NULL,
            field       text        NOT NULL,
            old_value   text,
            new_value   text,
            observed_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    # Deliberately NOT unique on (record, field, new_value): an issue that is
    # closed, reopened and closed again really did change three times, and a
    # unique index would swallow the second close. Repeats are prevented by
    # only writing when the value differs from the LATEST recorded one — see
    # store.record_transitions — which also settles the concurrent-scan case.
    op.execute(
        """
        CREATE INDEX ix_record_transitions_record
        ON record_transitions (company_id, record_id, field, observed_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX ix_record_transitions_recent
        ON record_transitions (company_id, observed_at DESC)
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS record_transitions")
