"""Picture library: every AI picture an educator chose to use, kept so other educators can find and reuse it.

A row is written when a picture made by the AI generator is uploaded into an activity. The library keeps its OWN copy of
the picture (``library/<sha256>.webp`` in blob storage), so it survives the activity being changed or archived; using a
library picture in an activity copies it into that activity like any other upload. The description is what the educator
typed when generating it, and is what searches look at.

Searching is deliberately not tied to this table's layout: services/picture_library.py has one function per search
method, so a similarity search over embeddings can later be added with its own table next to this one.
User references are plain uuids without foreign keys (see migration 0004).

Revision ID: 0010
"""

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

UP = """
CREATE TABLE content.library_pictures (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  description text NOT NULL,
  style text NOT NULL,
  size text NOT NULL,
  quality text NOT NULL,
  blob_path text NOT NULL,
  content_type text NOT NULL,
  bytes integer NOT NULL,
  width integer NOT NULL,
  height integer NOT NULL,
  sha256 text NOT NULL UNIQUE,
  created_by uuid,
  ai_generation_id uuid REFERENCES content.ai_generations(id) ON DELETE SET NULL,
  uses integer NOT NULL DEFAULT 1,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_library_pictures_created ON content.library_pictures (created_at DESC);
CREATE INDEX ix_library_pictures_style ON content.library_pictures (style, created_at DESC);
"""

DOWN = """
DROP TABLE IF EXISTS content.library_pictures;
"""


def upgrade() -> None:
    op.execute(UP)


def downgrade() -> None:
    op.execute(DOWN)
