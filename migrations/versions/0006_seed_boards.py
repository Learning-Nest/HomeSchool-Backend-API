"""Seed the first boards so the lesson pickers are not empty before any content bundle is imported.

Boards are data: more can be added by importing a bundle (taxonomy/boards.json in content-curriculum). Existing rows
are left alone, so re-running or importing the same boards later is harmless.

Revision ID: 0006
"""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "INSERT INTO content.boards (code, name, sort_order) VALUES ('CBSE', 'CBSE', 1), ('ICSE', 'ICSE', 2) "
        "ON CONFLICT (code) DO NOTHING"
    )


def downgrade() -> None:
    # Only the boards nothing refers to; a board that already has books stays.
    op.execute(
        "DELETE FROM content.boards b WHERE b.code IN ('CBSE', 'ICSE') "
        "AND NOT EXISTS (SELECT 1 FROM content.books k WHERE k.board_code = b.code)"
    )
