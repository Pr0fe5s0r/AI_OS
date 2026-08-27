"""workflow trigger fires: the exactly-once ledger for unattended runs

A trigger runtime that re-checks on a cron MUST NOT fire the same workflow
twice for the same cause. One ledger covers both kinds because both reduce to
a claim on an idempotency key:

  schedule  fire_key = the minute bucket, "2026-07-21T09:05"  (two workers,
            or a retried cron tick, claim the same minute — only one wins)
  event     fire_key = the situation id                        (a situation
            stays open across many watcher passes; it must fire once, ever)

The claim is a single atomic INSERT ... ON CONFLICT DO NOTHING RETURNING, so
there is no read-then-write race between concurrent workers.

Revision ID: 0015_workflow_trigger_fires
Revises: 0014_workflow_triggers
Create Date: 2026-07-21

"""
from alembic import op

revision = "0015_workflow_trigger_fires"
down_revision = "0014_workflow_triggers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS workflow_trigger_fires (
            id BIGSERIAL PRIMARY KEY,
            workflow_id BIGINT NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
            fire_key TEXT NOT NULL,
            fired_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_workflow_fire UNIQUE (workflow_id, fire_key)
        )
        """
    )
    # the ledger is only ever read by "has this fired?"; keep it prunable by age
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_workflow_fires_at ON workflow_trigger_fires (fired_at)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS workflow_trigger_fires")
