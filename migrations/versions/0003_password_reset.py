"""Forgot-password support: temporary password hash + expiry, and a must-change flag on users.

Revision ID: 0003
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

UP = """
ALTER TABLE users
  ADD COLUMN reset_hash text,
  ADD COLUMN reset_expires_at timestamptz,
  ADD COLUMN must_change_password boolean NOT NULL DEFAULT false;
"""

DOWN = """
ALTER TABLE users
  DROP COLUMN IF EXISTS must_change_password,
  DROP COLUMN IF EXISTS reset_expires_at,
  DROP COLUMN IF EXISTS reset_hash;
"""


def upgrade() -> None:
    op.execute(UP)


def downgrade() -> None:
    op.execute(DOWN)
