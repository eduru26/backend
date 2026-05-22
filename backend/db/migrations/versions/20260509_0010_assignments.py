"""add assignments table (one MCQ per assignment)

Revision ID: 0010_assignments
Revises: 0009_timetable
Create Date: 2026-05-09
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.dialects.postgresql import ENUM as PgEnum

revision: str = "0010_assignments"
down_revision: str | None = "0009_timetable"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "assignments" in inspector.get_table_names():
        return

    # Enum type for status
    op.execute("""
        DO $$ BEGIN
            CREATE TYPE assignment_status AS ENUM ('draft', 'published');
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$;
    """)

    op.create_table(
        "assignments",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "tenant_id", UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "teacher_id", UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "classroom_id", UUID(as_uuid=True),
            sa.ForeignKey("classrooms.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("subject", sa.String(100), nullable=False),
        sa.Column("title", sa.String(200), nullable=True),
        sa.Column("question_text", sa.Text, nullable=False),
        sa.Column("option_a", sa.String(500), nullable=False),
        sa.Column("option_b", sa.String(500), nullable=False),
        sa.Column("option_c", sa.String(500), nullable=False),
        sa.Column("option_d", sa.String(500), nullable=False),
        sa.Column("correct_option", sa.String(1), nullable=False),   # 'a' | 'b' | 'c' | 'd'
        sa.Column(
            "status",
            PgEnum("draft", "published", name="assignment_status", create_type=False),
            nullable=False, server_default="draft",
        ),
        sa.Column("due_date", sa.Date, nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
    )
    op.create_index("ix_assignments_tenant",    "assignments", ["tenant_id"])
    op.create_index("ix_assignments_teacher",   "assignments", ["teacher_id"])
    op.create_index("ix_assignments_classroom", "assignments", ["classroom_id"])


def downgrade() -> None:
    op.drop_index("ix_assignments_classroom", table_name="assignments")
    op.drop_index("ix_assignments_teacher",   table_name="assignments")
    op.drop_index("ix_assignments_tenant",    table_name="assignments")
    op.drop_table("assignments")
    op.execute("DROP TYPE IF EXISTS assignment_status")
