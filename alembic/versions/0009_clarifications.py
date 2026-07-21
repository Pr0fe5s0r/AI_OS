"""clarification pattern: choices on situations, norm resets, minimal chat

Checkpoint 6, part B. situations.kind already accepts any string (no CHECK
constraint) so "clarification" needs no schema change there — this migration
adds what a clarification situation carries (choices, resolution, snooze),
a durable audit trail for norm resets, and the minimal conversations/messages
backbone so a clarification is the SAME persisted object on the Feed and in
chat, not a simulated rendering of it.

Revision ID: 0009_clarifications
Revises: 0008_connector_health
Create Date: 2026-07-15

"""
from alembic import op

revision = "0009_clarifications"
down_revision = "0008_connector_health"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE situations ADD COLUMN IF NOT EXISTS choices JSONB")
    op.execute("ALTER TABLE situations ADD COLUMN IF NOT EXISTS resolved_choice TEXT")
    op.execute("ALTER TABLE situations ADD COLUMN IF NOT EXISTS resolved_by TEXT")
    op.execute("ALTER TABLE situations ADD COLUMN IF NOT EXISTS snoozed_until TIMESTAMPTZ")

    # Durable audit trail for "recalculate from <date>": persists so future
    # norm recomputations keep respecting the floor, not just the one that
    # ran at resolution time. One row per (company_id, metric) — the latest
    # reset wins, prior resets are visible via updated_at history in audit_log.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS norm_resets (
            company_id    TEXT NOT NULL,
            metric        TEXT NOT NULL,
            reset_before  TIMESTAMPTZ NOT NULL,
            reset_by      TEXT NOT NULL DEFAULT 'system',
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (company_id, metric)
        )
        """
    )

    # Minimal chat backbone: enough for a clarification to be a real message
    # with a real artifact on both surfaces. No streaming, no tool-use loop —
    # that's the full CP4 agent loop, a separate, larger piece of work.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS conversations (
            id         BIGSERIAL PRIMARY KEY,
            company_id TEXT NOT NULL,
            title      TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_conversations_company ON conversations (company_id, updated_at DESC)"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS messages (
            id              BIGSERIAL PRIMARY KEY,
            conversation_id BIGINT NOT NULL REFERENCES conversations (id) ON DELETE CASCADE,
            role            TEXT NOT NULL CHECK (role IN ('user', 'agent', 'system')),
            content         TEXT NOT NULL DEFAULT '',
            artifacts       JSONB NOT NULL DEFAULT '[]'::jsonb,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_messages_conversation ON messages (conversation_id, created_at)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS messages")
    op.execute("DROP TABLE IF EXISTS conversations")
    op.execute("DROP TABLE IF EXISTS norm_resets")
    op.execute("ALTER TABLE situations DROP COLUMN IF EXISTS snoozed_until")
    op.execute("ALTER TABLE situations DROP COLUMN IF EXISTS resolved_by")
    op.execute("ALTER TABLE situations DROP COLUMN IF EXISTS resolved_choice")
    op.execute("ALTER TABLE situations DROP COLUMN IF EXISTS choices")
