"""workflow triggers: an n8n-style trigger node per workflow

Adds a `trigger` jsonb ({type: manual|schedule|event, config}) — what STARTS a
workflow. The old free-text `schedule` column is folded into it (schedule
triggers now live in trigger.config.cron). Only manual fires today; schedule +
event runtimes land in the triggers-runtime build.

Revision ID: 0014_workflow_triggers
Revises: 0013_workflows
Create Date: 2026-07-21

"""
from alembic import op

revision = "0014_workflow_triggers"
down_revision = "0013_workflows"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE workflows
        ADD COLUMN IF NOT EXISTS trigger JSONB NOT NULL DEFAULT '{"type": "manual", "config": {}}'::jsonb
        """
    )
    # carry any existing free-text schedule into the new trigger shape
    op.execute(
        """
        UPDATE workflows
        SET trigger = jsonb_build_object('type', 'schedule', 'config', jsonb_build_object('cron', schedule))
        WHERE schedule IS NOT NULL AND schedule <> ''
        """
    )
    op.execute("ALTER TABLE workflows DROP COLUMN IF EXISTS schedule")
    # event-triggered workflows are found by matching a raised situation's rule/
    # severity against many workflows — index the trigger type for that scan.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_workflows_trigger_type ON workflows ((trigger ->> 'type'))"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE workflows ADD COLUMN IF NOT EXISTS schedule TEXT")
    op.execute("DROP INDEX IF EXISTS ix_workflows_trigger_type")
    op.execute("ALTER TABLE workflows DROP COLUMN IF EXISTS trigger")
