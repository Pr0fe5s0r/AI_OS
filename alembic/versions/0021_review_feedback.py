"""remember which review findings a person rejected

A reviewer that never learns is a reviewer you eventually mute. When a person
dismisses a finding they are teaching us something — "this isn't worth my time"
— and that lesson should change what we raise next. One row per human verdict on
a finding, keyed so we can answer two questions:

  - was THIS exact finding dismissed?  (respect it; don't reopen it on re-review)
  - does the team keep dismissing a whole CATEGORY?  (stop raising it)

Deliberately records only EXPLICIT human verdicts, never anything the system did
on its own — the minimum-evidence guard against a feedback loop poisoning
itself. `verdict` is a small closed vocabulary ('dismissed' today; 'posted' /
'resolved' reserved for the positive signals).

Revision ID: 0021_review_feedback
Revises: 0020_record_reviews
Create Date: 2026-07-24

"""
from alembic import op

revision = "0021_review_feedback"
down_revision = "0020_record_reviews"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE review_feedback (
            id           bigserial   PRIMARY KEY,
            company_id   text        NOT NULL,
            reviewer_key text        NOT NULL,
            finding_id   text        NOT NULL,
            concern      text        NOT NULL,
            category     text        NOT NULL,
            severity     text        NOT NULL,
            verdict      text        NOT NULL,
            created_at   timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    # taste lookup: "how many times has this concern/category been dismissed?"
    op.execute(
        """
        CREATE INDEX ix_review_feedback_taste
        ON review_feedback (company_id, concern, category, verdict)
        """
    )
    # per-finding lookup: "was this exact finding dismissed?"
    op.execute(
        """
        CREATE INDEX ix_review_feedback_finding
        ON review_feedback (company_id, finding_id, verdict)
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS review_feedback")
