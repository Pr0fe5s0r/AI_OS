"""remember which records the triage detector already judged

The triage detector reads a fresh record from ANY connected source and decides
whether it needs a person — a bug report, an incident, a request — or is just
chatter. That is one LLM call per record, and the analyze pass re-runs; without
a memory it would re-judge (and re-pay for) every record every cycle. One row
per (company, source, record) holding the verdict, so a record is judged once
and only re-judged if it materially changes.

Generic by construction: nothing here is Slack- or GitHub-specific. Every source
the engine ingests flows through the same detector and the same ledger.

Revision ID: 0022_record_triage
Revises: 0021_review_feedback
Create Date: 2026-07-25

"""
from alembic import op

revision = "0022_record_triage"
down_revision = "0021_review_feedback"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE record_triage (
            company_id   text        NOT NULL,
            source       text        NOT NULL,
            record_id    text        NOT NULL,
            needs_action boolean     NOT NULL DEFAULT false,
            category     text,
            triaged_at   timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (company_id, source, record_id)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS record_triage")
