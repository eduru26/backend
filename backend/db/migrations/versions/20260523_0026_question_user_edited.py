"""Question.user_edited flag — preserve manual edits across regenerate.

When a teacher edits a question, set user_edited=true. The paper-gen
worker preserves user_edited rows on regenerate instead of deleting
the whole assessment's question set.

Revision ID: 0026_question_user_edited
Revises: 0025_curriculum_proposals
Create Date: 2026-05-23
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0026_question_user_edited"
down_revision: str | None = "0025_curriculum_proposals"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE questions ADD COLUMN IF NOT EXISTS user_edited "
        "BOOLEAN NOT NULL DEFAULT false"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE questions DROP COLUMN IF EXISTS user_edited")
