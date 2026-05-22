"""convert assignments to multi-question (assignment_questions table)

Revision ID: 0011_assignment_questions
Revises: 0010_assignments
Create Date: 2026-05-09
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0011_assignment_questions"
down_revision: str | None = "0010_assignments"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = inspector.get_table_names()

    if "assignment_questions" not in tables:
        op.create_table(
            "assignment_questions",
            sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
            sa.Column(
                "assignment_id", UUID(as_uuid=True),
                sa.ForeignKey("assignments.id", ondelete="CASCADE"), nullable=False,
            ),
            sa.Column("order_num", sa.SmallInteger, nullable=False, server_default="1"),
            sa.Column("question_text", sa.Text, nullable=False),
            sa.Column("option_a", sa.String(500), nullable=False),
            sa.Column("option_b", sa.String(500), nullable=False),
            sa.Column("option_c", sa.String(500), nullable=False),
            sa.Column("option_d", sa.String(500), nullable=False),
            sa.Column("correct_option", sa.String(1), nullable=False),
        )
        op.create_index("ix_aq_assignment", "assignment_questions", ["assignment_id"])

    # Migrate existing single-question rows → new table, then drop old columns
    existing_cols = {c["name"] for c in inspector.get_columns("assignments")}
    if "question_text" in existing_cols:
        op.execute("""
            INSERT INTO assignment_questions
                (id, assignment_id, order_num,
                 question_text, option_a, option_b, option_c, option_d, correct_option)
            SELECT gen_random_uuid(), id, 1,
                   question_text, option_a, option_b, option_c, option_d, correct_option
            FROM assignments
        """)
        for col in ("question_text", "option_a", "option_b", "option_c", "option_d", "correct_option"):
            op.drop_column("assignments", col)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    existing_cols = {c["name"] for c in inspector.get_columns("assignments")}
    if "question_text" not in existing_cols:
        for col, typ in [
            ("question_text", sa.Text()),
            ("option_a",      sa.String(500)),
            ("option_b",      sa.String(500)),
            ("option_c",      sa.String(500)),
            ("option_d",      sa.String(500)),
            ("correct_option", sa.String(1)),
        ]:
            op.add_column("assignments", sa.Column(col, typ, nullable=True))

        op.execute("""
            UPDATE assignments a
            SET
                question_text = aq.question_text,
                option_a      = aq.option_a,
                option_b      = aq.option_b,
                option_c      = aq.option_c,
                option_d      = aq.option_d,
                correct_option = aq.correct_option
            FROM (
                SELECT DISTINCT ON (assignment_id)
                    assignment_id, question_text, option_a, option_b, option_c, option_d, correct_option
                FROM assignment_questions
                ORDER BY assignment_id, order_num
            ) aq
            WHERE a.id = aq.assignment_id
        """)

    if "assignment_questions" in inspector.get_table_names():
        op.drop_index("ix_aq_assignment", table_name="assignment_questions")
        op.drop_table("assignment_questions")
