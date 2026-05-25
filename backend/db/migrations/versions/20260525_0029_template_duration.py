"""Question paper template — duration in minutes.

Admins set a paper's allotted time on the template ("1h 30m" / "2h"
etc.). Shown on the teacher's paper page and the printed exam header
alongside the school name and total marks.

Revision ID: 0029_template_duration
Revises: 0028_textbook_ingestion
Create Date: 2026-05-25
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0029_template_duration"
down_revision: str | None = "0028_textbook_ingestion"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE question_paper_templates "
        "ADD COLUMN IF NOT EXISTS duration_minutes SMALLINT"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE question_paper_templates "
        "DROP COLUMN IF EXISTS duration_minutes"
    )
