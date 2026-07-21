"""workflows: saved agentic plans + their runs (Phase 2)

A workflow is a named natural-language goal compiled into an ordered list of
steps, each mapping to a tool the agent already has. It can be run manually or
on a schedule; every run is a workflow_runs row holding the live transcript and
per-step results. External-effect steps still pass the same approval/dry-run
brake a single click does — a workflow is a saved intent, never a way around
the gate.

Revision ID: 0013_workflows
Revises: 0012_norm_maturity
Create Date: 2026-07-21

"""
from alembic import op

revision = "0013_workflows"
down_revision = "0012_norm_maturity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS workflows (
            id          BIGSERIAL PRIMARY KEY,
            company_id  TEXT NOT NULL,
            name        TEXT NOT NULL,
            goal        TEXT NOT NULL,
            -- ordered [{tool, args, description, requires_approval}] — the plan
            -- the user reviewed; execution runs these live against real data.
            steps       JSONB NOT NULL DEFAULT '[]'::jsonb,
            -- null = manual only; else a cron-ish spec the scheduler reads (Part D)
            schedule    TEXT,
            enabled     BOOLEAN NOT NULL DEFAULT true,
            created_by  TEXT NOT NULL DEFAULT 'ui',
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            last_run_at TIMESTAMPTZ
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_workflows_company ON workflows (company_id, enabled)")

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS workflow_runs (
            id           BIGSERIAL PRIMARY KEY,
            workflow_id  BIGINT NOT NULL REFERENCES workflows (id) ON DELETE CASCADE,
            company_id   TEXT NOT NULL,
            -- planning | running | needs_approval | done | failed
            status       TEXT NOT NULL DEFAULT 'running',
            -- manual | scheduled
            trigger      TEXT NOT NULL DEFAULT 'manual',
            -- per-step outcomes: [{tool, args, status, detail, ...}]
            step_results JSONB NOT NULL DEFAULT '[]'::jsonb,
            summary      TEXT NOT NULL DEFAULT '',
            started_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            finished_at  TIMESTAMPTZ
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_workflow_runs_workflow ON workflow_runs (workflow_id, started_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS workflow_runs")
    op.execute("DROP TABLE IF EXISTS workflows")
