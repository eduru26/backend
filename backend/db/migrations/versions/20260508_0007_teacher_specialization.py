"""add teacher specialization and subject-classroom assignments

Revision ID: 0007_teacher_specialization
Revises: 0006_academic_years
Create Date: 2026-05-08

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0007_teacher_specialization"
down_revision: str | None = "0006_academic_years"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = inspector.get_table_names()

    # ── 1. specialization column on users ─────────────────
    existing_user_cols = [c["name"] for c in inspector.get_columns("users")]
    if "specialization" not in existing_user_cols:
        op.add_column(
            "users",
            sa.Column("specialization", sa.String(100), nullable=True),
        )

    # ── 2. teacher_subject_assignments table ───────────────
    if "teacher_subject_assignments" not in existing_tables:
        op.create_table(
            "teacher_subject_assignments",
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
            sa.Column(
                "created_at", sa.DateTime(timezone=True),
                server_default=sa.text("now()"), nullable=False,
            ),
        )
        op.create_index("ix_tsa_tenant_id",  "teacher_subject_assignments", ["tenant_id"])
        op.create_index("ix_tsa_teacher_id", "teacher_subject_assignments", ["teacher_id"])
        op.create_unique_constraint(
            "uq_tsa_teacher_cls_subject",
            "teacher_subject_assignments",
            ["teacher_id", "classroom_id", "subject"],
        )


def downgrade() -> None:
    op.drop_constraint("uq_tsa_teacher_cls_subject", "teacher_subject_assignments", type_="unique")
    op.drop_index("ix_tsa_teacher_id", table_name="teacher_subject_assignments")
    op.drop_index("ix_tsa_tenant_id",  table_name="teacher_subject_assignments")
    op.drop_table("teacher_subject_assignments")
    op.drop_column("users", "specialization")
