"""Question grounding metadata — track per-question textbook backing.

The paper generator runs a grounding validator after every LLM call;
the max embedding-similarity to the retrieved chunks lands in
`grounding_score`, and the Qdrant point ids of those chunks land in
`source_chunk_ids` so the UI can show a "Show source" drawer linking
each question back to the exact textbook excerpt that grounded it.

Revision ID: 0027_question_grounding
Revises: 0026_question_user_edited
Create Date: 2026-05-24
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0027_question_grounding"
down_revision: str | None = "0026_question_user_edited"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE questions ADD COLUMN IF NOT EXISTS grounding_score "
        "DOUBLE PRECISION"
    )
    op.execute(
        "ALTER TABLE questions ADD COLUMN IF NOT EXISTS source_chunk_ids "
        "TEXT[]"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE questions DROP COLUMN IF EXISTS source_chunk_ids")
    op.execute("ALTER TABLE questions DROP COLUMN IF EXISTS grounding_score")
