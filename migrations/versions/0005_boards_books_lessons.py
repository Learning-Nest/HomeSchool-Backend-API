"""Boards, textbooks and lessons, activity-to-lesson links, and each child's textbook progress.

Boards, books and lessons are DATA (rows imported with the content bundle), never code: adding a board needs no release.
An activity may be linked to lessons of several books (and so several boards). Activities with no link are board-neutral
and stay visible to everyone. `child_books` is family-scoped (row-level security like the other child tables).

Revision ID: 0005
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

UP = """
CREATE TABLE content.boards (
  code text PRIMARY KEY,
  name text NOT NULL,
  sort_order integer NOT NULL DEFAULT 0
);

CREATE TABLE content.books (
  code text PRIMARY KEY,
  board_code text NOT NULL REFERENCES content.boards(code),
  grade text NOT NULL,
  subject_code text NOT NULL REFERENCES content.subjects(code),
  title text NOT NULL,
  publisher text,
  edition_year integer,
  level_code text REFERENCES content.levels(code),
  status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'retired'))
);
CREATE INDEX ix_books_board_grade ON content.books (board_code, grade, subject_code);

CREATE TABLE content.lessons (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  book_code text NOT NULL REFERENCES content.books(code) ON DELETE CASCADE,
  position integer NOT NULL CHECK (position > 0),
  label text,
  title text NOT NULL,
  UNIQUE (book_code, position)
);

CREATE TABLE content.activity_lessons (
  activity_id uuid NOT NULL REFERENCES content.activities(id) ON DELETE CASCADE,
  lesson_id uuid NOT NULL REFERENCES content.lessons(id) ON DELETE CASCADE,
  position integer NOT NULL DEFAULT 0,
  PRIMARY KEY (activity_id, lesson_id)
);
CREATE INDEX ix_activity_lessons_lesson ON content.activity_lessons (lesson_id, position);

CREATE TABLE child_books (
  family_id uuid NOT NULL REFERENCES families(id) ON DELETE CASCADE,
  child_id uuid NOT NULL REFERENCES children(id) ON DELETE CASCADE,
  book_code text NOT NULL REFERENCES content.books(code),
  unlocked_through integer NOT NULL CHECK (unlocked_through >= 0),
  started_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (child_id, book_code)
);
CREATE INDEX ix_child_books_family ON child_books (family_id);
"""

DOWN = """
DROP TABLE IF EXISTS child_books;
DROP TABLE IF EXISTS content.activity_lessons, content.lessons, content.books, content.boards;
"""


def upgrade() -> None:
    op.execute(UP)
    op.execute("ALTER TABLE child_books ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE child_books FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY family_isolation ON child_books "
        "USING (app_can_access_family(family_id)) WITH CHECK (app_can_access_family(family_id))"
    )


def downgrade() -> None:
    op.execute(DOWN)
