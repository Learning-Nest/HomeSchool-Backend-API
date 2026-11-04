"""Daily, system-assigned activities and results by lesson.

Adds (all additive, so the previous app version keeps working):
* children: board_code, grade (the child's standard), paused_until (vacation pause of the daily release)
* families: plan_mode ('parent' = parents plan, as before; 'system' = LearnNest releases activities every day)
* child_books: source, active, last_release_on
* plan_items: source ('parent' | 'system' | 'revision'), batch_id, book_code, lesson_position, released_on
* activity_sessions: book_code, lesson_position (copied from the plan item, so lesson results stay stable)
* assignment_batches: one row per daily release for a child (family scoped, row-level security)
* content.release_policies: the release settings, most specific row wins (board + grade, board, default)

Revision ID: 0008
"""

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

UP = """
ALTER TABLE children
  ADD COLUMN board_code text REFERENCES content.boards(code),
  ADD COLUMN grade text,
  ADD COLUMN paused_until date;

ALTER TABLE families
  ADD COLUMN plan_mode text NOT NULL DEFAULT 'parent' CHECK (plan_mode IN ('parent', 'system'));

ALTER TABLE child_books
  ADD COLUMN source text NOT NULL DEFAULT 'parent' CHECK (source IN ('parent', 'system')),
  ADD COLUMN active boolean NOT NULL DEFAULT true,
  ADD COLUMN last_release_on date;

ALTER TABLE plan_items
  ADD COLUMN source text NOT NULL DEFAULT 'parent' CHECK (source IN ('parent', 'system', 'revision')),
  ADD COLUMN batch_id uuid,
  ADD COLUMN book_code text,
  ADD COLUMN lesson_position integer,
  ADD COLUMN released_on date;
CREATE UNIQUE INDEX ux_plan_items_system_once ON plan_items (child_id, activity_id) WHERE source = 'system';
CREATE INDEX ix_plan_items_batch ON plan_items (batch_id) WHERE batch_id IS NOT NULL;
CREATE INDEX ix_plan_items_child_status ON plan_items (child_id, status, scheduled_date);

ALTER TABLE activity_sessions
  ADD COLUMN book_code text,
  ADD COLUMN lesson_position integer;
CREATE INDEX ix_sessions_child_activity ON activity_sessions (child_id, activity_id);

CREATE TABLE assignment_batches (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  family_id uuid NOT NULL REFERENCES families(id) ON DELETE CASCADE,
  child_id uuid NOT NULL REFERENCES children(id) ON DELETE CASCADE,
  released_on date NOT NULL,
  item_count integer NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_batches_child ON assignment_batches (child_id, released_on DESC);
CREATE UNIQUE INDEX ux_batches_child_day ON assignment_batches (child_id, released_on);

CREATE TABLE content.release_policies (
  scope_board text NOT NULL DEFAULT '',
  scope_grade text NOT NULL DEFAULT '',
  release_threshold numeric(3,2) NOT NULL DEFAULT 0.70 CHECK (release_threshold > 0 AND release_threshold <= 1),
  set_minutes integer NOT NULL DEFAULT 45 CHECK (set_minutes BETWEEN 5 AND 600),
  set_max_activities integer NOT NULL DEFAULT 5 CHECK (set_max_activities BETWEEN 1 AND 30),
  max_open_items integer NOT NULL DEFAULT 10 CHECK (max_open_items BETWEEN 1 AND 100),
  school_days integer[] NOT NULL DEFAULT '{1,2,3,4,5,6,7}',
  strong_score numeric(3,2) NOT NULL DEFAULT 0.85 CHECK (strong_score > 0 AND strong_score <= 1),
  revision_open_cap integer NOT NULL DEFAULT 5 CHECK (revision_open_cap BETWEEN 0 AND 50),
  revision_cooldown_days integer NOT NULL DEFAULT 1 CHECK (revision_cooldown_days BETWEEN 0 AND 60),
  child_daily_practice_limit integer NOT NULL DEFAULT 3 CHECK (child_daily_practice_limit BETWEEN 0 AND 20),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (scope_board, scope_grade)
);
INSERT INTO content.release_policies (scope_board, scope_grade) VALUES ('', '');
"""

DOWN = """
DROP TABLE IF EXISTS content.release_policies;
DROP TABLE IF EXISTS assignment_batches;
DROP INDEX IF EXISTS ix_sessions_child_activity;
ALTER TABLE activity_sessions DROP COLUMN IF EXISTS book_code, DROP COLUMN IF EXISTS lesson_position;
DROP INDEX IF EXISTS ix_plan_items_child_status;
DROP INDEX IF EXISTS ix_plan_items_batch;
DROP INDEX IF EXISTS ux_plan_items_system_once;
ALTER TABLE plan_items DROP COLUMN IF EXISTS source, DROP COLUMN IF EXISTS batch_id, DROP COLUMN IF EXISTS book_code,
  DROP COLUMN IF EXISTS lesson_position, DROP COLUMN IF EXISTS released_on;
ALTER TABLE child_books DROP COLUMN IF EXISTS source, DROP COLUMN IF EXISTS active, DROP COLUMN IF EXISTS last_release_on;
ALTER TABLE families DROP COLUMN IF EXISTS plan_mode;
ALTER TABLE children DROP COLUMN IF EXISTS board_code, DROP COLUMN IF EXISTS grade, DROP COLUMN IF EXISTS paused_until;
"""


def upgrade() -> None:
    op.execute(UP)
    op.execute("ALTER TABLE assignment_batches ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE assignment_batches FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY family_isolation ON assignment_batches "
        "USING (app_can_access_family(family_id)) WITH CHECK (app_can_access_family(family_id))"
    )


def downgrade() -> None:
    op.execute(DOWN)
