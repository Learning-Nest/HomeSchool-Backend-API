"""Educator authoring: authorship and review columns on activities, an event log, and uploaded images.

User references (created_by, actor_id, ...) are plain uuids without foreign keys on purpose: the content schema is shared
and must survive when user rows are removed (tests truncate users; accounts are deactivated, never deleted).

Revision ID: 0004
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

UP = """
ALTER TABLE content.activities
  ADD COLUMN created_by uuid,
  ADD COLUMN last_edited_by uuid,
  ADD COLUMN last_edited_at timestamptz,
  ADD COLUMN submitted_by uuid,
  ADD COLUMN submitted_at timestamptz,
  ADD COLUMN reviewed_by uuid,
  ADD COLUMN reviewed_at timestamptz,
  ADD COLUMN review_note text,
  ADD COLUMN source text NOT NULL DEFAULT 'bundle',
  ADD COLUMN last_validated_at timestamptz,
  ADD COLUMN validated_hash text;
CREATE INDEX ix_activities_created_by ON content.activities (created_by);

CREATE TABLE content.activity_events (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  activity_id uuid NOT NULL REFERENCES content.activities(id) ON DELETE CASCADE,
  actor_id uuid,
  action text NOT NULL,
  at timestamptz NOT NULL DEFAULT now(),
  version integer,
  status_from text,
  status_to text,
  detail jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX ix_activity_events_activity_at ON content.activity_events (activity_id, at DESC);
CREATE INDEX ix_activity_events_actor_at ON content.activity_events (actor_id, at DESC);
CREATE INDEX ix_activity_events_at ON content.activity_events (at DESC);

CREATE TABLE content.assets (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  activity_id uuid NOT NULL REFERENCES content.activities(id) ON DELETE CASCADE,
  uploaded_by uuid,
  blob_path text NOT NULL,
  content_type text NOT NULL,
  bytes integer NOT NULL,
  width integer NOT NULL,
  height integer NOT NULL,
  sha256 text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  deleted_at timestamptz
);
CREATE INDEX ix_assets_activity ON content.assets (activity_id);
CREATE UNIQUE INDEX ux_assets_activity_sha ON content.assets (activity_id, sha256) WHERE deleted_at IS NULL;

ALTER TABLE users DROP CONSTRAINT users_platform_role_check;
ALTER TABLE users ADD CONSTRAINT users_platform_role_check
  CHECK (platform_role IN ('educator', 'content_admin', 'super_admin'));
CREATE INDEX ix_users_platform_role ON users (platform_role) WHERE platform_role IS NOT NULL;
"""

DOWN = """
DROP INDEX IF EXISTS ix_users_platform_role;
UPDATE users SET platform_role = NULL WHERE platform_role = 'educator';
ALTER TABLE users DROP CONSTRAINT IF EXISTS users_platform_role_check;
ALTER TABLE users ADD CONSTRAINT users_platform_role_check CHECK (platform_role IN ('content_admin', 'super_admin'));
DROP TABLE IF EXISTS content.assets;
DROP TABLE IF EXISTS content.activity_events;
DROP INDEX IF EXISTS content.ix_activities_created_by;
ALTER TABLE content.activities
  DROP COLUMN IF EXISTS validated_hash,
  DROP COLUMN IF EXISTS last_validated_at,
  DROP COLUMN IF EXISTS source,
  DROP COLUMN IF EXISTS review_note,
  DROP COLUMN IF EXISTS reviewed_at,
  DROP COLUMN IF EXISTS reviewed_by,
  DROP COLUMN IF EXISTS submitted_at,
  DROP COLUMN IF EXISTS submitted_by,
  DROP COLUMN IF EXISTS last_edited_at,
  DROP COLUMN IF EXISTS last_edited_by,
  DROP COLUMN IF EXISTS created_by;
"""


def upgrade() -> None:
    op.execute(UP)


def downgrade() -> None:
    op.execute(DOWN)
