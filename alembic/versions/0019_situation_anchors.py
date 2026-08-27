"""line-anchor a situation to a spot in a diff (review findings)

A watcher raises a situation about a whole Thing ("this PR has stalled"). A
reviewer raises one about a LINE ("line 40 can be null here"). Same lifecycle,
same table — the difference is five nullable columns that only a review finding
fills. Kept on `situations` rather than a second table so the whole
raise/dedupe/snooze/resolve machinery, the Feed, and act() work unchanged: an
anchored row IS a line finding, an unanchored one is today's thing-level
situation.

`confidence` is the reviewer's own certainty (the autonomy gate reads it);
`category` is the concern's free-text label. All nullable, so every existing
situation is valid as-is.

Revision ID: 0019_situation_anchors
Revises: 0018_record_transitions
Create Date: 2026-07-24

"""
from alembic import op

revision = "0019_situation_anchors"
down_revision = "0018_record_transitions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE situations ADD COLUMN IF NOT EXISTS file_path  text")
    op.execute("ALTER TABLE situations ADD COLUMN IF NOT EXISTS line_start integer")
    op.execute("ALTER TABLE situations ADD COLUMN IF NOT EXISTS line_end   integer")
    op.execute("ALTER TABLE situations ADD COLUMN IF NOT EXISTS confidence double precision")
    op.execute("ALTER TABLE situations ADD COLUMN IF NOT EXISTS category   text")


def downgrade() -> None:
    op.execute("ALTER TABLE situations DROP COLUMN IF EXISTS category")
    op.execute("ALTER TABLE situations DROP COLUMN IF EXISTS confidence")
    op.execute("ALTER TABLE situations DROP COLUMN IF EXISTS line_end")
    op.execute("ALTER TABLE situations DROP COLUMN IF EXISTS line_start")
    op.execute("ALTER TABLE situations DROP COLUMN IF EXISTS file_path")
