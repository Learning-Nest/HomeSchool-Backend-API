"""Activity assignments: an admin hands an existing draft to an educator with written requirements and a due date.

User references are plain uuids without foreign keys on purpose (see migration 0004). At most one assignment per
activity is live (open or submitted) at a time; reassigning cancels the earlier one and keeps it as history.

Revision ID: 0007
"""

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

UP = """
CREATE TABLE content.activity_assignments (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  activity_id uuid NOT NULL REFERENCES content.activities(id) ON DELETE CASCADE,
  educator_id uuid NOT NULL,
  assigned_by uuid,
  requirements text NOT NULL,
  due_date date,
  status text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'submitted', 'completed', 'cancelled')),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  submitted_at timestamptz,
  completed_at timestamptz,
  cancelled_at timestamptz
);
CREATE UNIQUE INDEX ux_assignments_live_activity ON content.activity_assignments (activity_id)
  WHERE status IN ('open', 'submitted');
CREATE INDEX ix_assignments_educator ON content.activity_assignments (educator_id, status);
CREATE INDEX ix_assignments_status_due ON content.activity_assignments (status, due_date);
"""

DOWN = """
DROP TABLE IF EXISTS content.activity_assignments;
"""


def upgrade() -> None:
    op.execute(UP)


def downgrade() -> None:
    op.execute(DOWN)
