"""remember which record snapshot a reviewer last read

A reviewer fans an LLM call out per concern per open pull request. The analyze
pass re-runs on every scan, so without a memory it would re-review every open
PR — and pay for it — even when nothing about the PR moved. This is the review
counterpart to incremental re-embedding: one row per (company, reviewer,
record) holding the activity stamp the reviewer last read. review_thing skips a
record whose stamp is unchanged, and re-runs the moment a new commit, comment
or edit advances it.

`reviewed_activity` is the record's own last-changed value (its `updated_at`),
not our clock — the same witness the stall watchers trust, so "did this move
since I looked?" is answered by the record, not by when the cron happened to
fire.

Revision ID: 0020_record_reviews
Revises: 0019_situation_anchors
Create Date: 2026-07-24

"""
from alembic import op

revision = "0020_record_reviews"
down_revision = "0019_situation_anchors"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE record_reviews (
            company_id        text        NOT NULL,
            reviewer_key      text        NOT NULL,
            record_id         text        NOT NULL,
            source            text        NOT NULL,
            reviewed_activity text,
            finding_count     integer     NOT NULL DEFAULT 0,
            reviewed_at       timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (company_id, reviewer_key, record_id)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS record_reviews")
