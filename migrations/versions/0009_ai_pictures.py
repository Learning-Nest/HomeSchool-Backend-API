"""AI pictures: a record of every picture-generation request, and a link from an asset to the request that made it.

A generation row keeps what the educator asked for (the description and options). It is written when OpenAI returned
pictures, whether or not any of them is kept. An asset that was uploaded from a generation points back at it, which is
how the editor can say "made with AI". User references are plain uuids without foreign keys (see migration 0004).

Revision ID: 0009
"""

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

UP = """
CREATE TABLE content.ai_generations (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  activity_id uuid NOT NULL REFERENCES content.activities(id) ON DELETE CASCADE,
  user_id uuid NOT NULL,
  description text NOT NULL,
  style text NOT NULL,
  size text NOT NULL,
  quality text NOT NULL,
  images integer NOT NULL,
  avoid_text boolean NOT NULL DEFAULT false,
  model text,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_ai_generations_activity ON content.ai_generations (activity_id, created_at);
CREATE INDEX ix_ai_generations_user ON content.ai_generations (user_id, created_at);

ALTER TABLE content.assets
  ADD COLUMN ai_generation_id uuid REFERENCES content.ai_generations(id) ON DELETE SET NULL;
"""

DOWN = """
ALTER TABLE content.assets DROP COLUMN IF EXISTS ai_generation_id;
DROP TABLE IF EXISTS content.ai_generations;
"""


def upgrade() -> None:
    op.execute(UP)


def downgrade() -> None:
    op.execute(DOWN)
