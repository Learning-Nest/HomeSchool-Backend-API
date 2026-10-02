"""Scrum board for the internal team (not family-scoped; no Row-Level Security).

Revision ID: 0002
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

UP = """
CREATE TABLE scrum_members (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name text NOT NULL,
  access_code_digest text NOT NULL UNIQUE,
  is_admin boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE scrum_tasks (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  title text NOT NULL,
  description text,
  category text NOT NULL DEFAULT 'dev' CHECK (category IN ('content', 'dev', 'design', 'other')),
  status text NOT NULL DEFAULT 'backlog'
    CHECK (status IN ('backlog', 'todo', 'in_progress', 'review', 'done')),
  assignee_name text,
  sprint text,
  position integer NOT NULL DEFAULT 0,
  created_by_member_id uuid REFERENCES scrum_members(id) ON DELETE SET NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX scrum_tasks_status_idx ON scrum_tasks (status);
"""

DOWN = """
DROP TABLE IF EXISTS scrum_tasks CASCADE;
DROP TABLE IF EXISTS scrum_members CASCADE;
"""


def upgrade() -> None:
    op.execute(UP)


def downgrade() -> None:
    op.execute(DOWN)
