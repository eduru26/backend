"""add assignment_responses table for student answer tracking

Revision ID: 0012_assignment_responses
Revises: 0011_assignment_questions
Create Date: 2026-05-09
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0012_assignment_responses"
down_revision: str | None = "0011_assignment_questions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "assignment_responses" in inspector.get_table_names():
        return

    op.create_table(
        "assignment_responses",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "assignment_id", UUID(as_uuid=True),
            sa.ForeignKey("assignments.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "student_id", UUID(as_uuid=True),
            sa.ForeignKey("students.id", ondelete="CASCADE"), nullable=False,
        ),
        # answers: {question_id: 'a'|'b'|'c'|'d'}
        sa.Column("answers", JSONB, nullable=False, server_default="{}"),
        sa.Column("score", sa.SmallInteger, nullable=False, server_default="0"),
        sa.Column("total_questions", sa.SmallInteger, nullable=False, server_default="0"),
        sa.Column(
            "submitted_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.UniqueConstraint("assignment_id", "student_id", name="uq_asgn_response"),
    )
    op.create_index("ix_ar_assignment", "assignment_responses", ["assignment_id"])
    op.create_index("ix_ar_student",    "assignment_responses", ["student_id"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "assignment_responses" not in inspector.get_table_names():
        return
    op.drop_index("ix_ar_student",    table_name="assignment_responses")
    op.drop_index("ix_ar_assignment", table_name="assignment_responses")
    op.drop_table("assignment_responses")
