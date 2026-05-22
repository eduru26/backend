"""add academic years, classroom capacity, student class history

Revision ID: 0006_academic_years
Revises: 0005_school_admin_classrooms
Create Date: 2026-05-07

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0006_academic_years"
down_revision: str | None = "0005_school_admin_classrooms"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = inspector.get_table_names()

    # ── 1. classroom capacity ──────────────────────────────
    existing_cls_cols = [c["name"] for c in inspector.get_columns("classrooms")]
    if "capacity" not in existing_cls_cols:
        op.add_column(
            "classrooms",
            sa.Column("capacity", sa.Integer, nullable=False, server_default="30"),
        )

    # ── 2. academic_years ──────────────────────────────────
    if "academic_years" not in existing:
        op.create_table(
            "academic_years",
            sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
            sa.Column(
                "tenant_id", UUID(as_uuid=True),
                sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False,
            ),
            sa.Column("name", sa.String(20), nullable=False),
            sa.Column("start_date", sa.Date, nullable=False),
            sa.Column("end_date", sa.Date, nullable=False),
            sa.Column("is_current", sa.Boolean, nullable=False, server_default="false"),
            sa.Column(
                "created_at", sa.DateTime(timezone=True),
                server_default=sa.text("now()"), nullable=False,
            ),
        )
        op.create_index("ix_academic_years_tenant_id", "academic_years", ["tenant_id"])
        op.create_unique_constraint(
            "uq_academic_years_tenant_name", "academic_years", ["tenant_id", "name"]
        )

    # ── 3. student_class_history ───────────────────────────
    if "student_class_history" not in existing:
        op.create_table(
            "student_class_history",
            sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
            sa.Column(
                "student_id", UUID(as_uuid=True),
                sa.ForeignKey("students.id", ondelete="CASCADE"), nullable=False,
            ),
            sa.Column(
                "classroom_id", UUID(as_uuid=True),
                sa.ForeignKey("classrooms.id", ondelete="RESTRICT"), nullable=False,
            ),
            sa.Column(
                "academic_year_id", UUID(as_uuid=True),
                sa.ForeignKey("academic_years.id", ondelete="CASCADE"), nullable=False,
            ),
            sa.Column(
                "enrolled_at", sa.DateTime(timezone=True),
                server_default=sa.text("now()"), nullable=False,
            ),
            sa.Column("promoted_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index(
            "ix_class_history_student_id", "student_class_history", ["student_id"]
        )
        op.create_index(
            "ix_class_history_year_id", "student_class_history", ["academic_year_id"]
        )
        op.create_unique_constraint(
            "uq_class_history_student_year",
            "student_class_history", ["student_id", "academic_year_id"],
        )


def downgrade() -> None:
    op.drop_index("ix_class_history_year_id", table_name="student_class_history")
    op.drop_index("ix_class_history_student_id", table_name="student_class_history")
    op.drop_constraint("uq_class_history_student_year", "student_class_history", type_="unique")
    op.drop_table("student_class_history")

    op.drop_index("ix_academic_years_tenant_id", table_name="academic_years")
    op.drop_constraint("uq_academic_years_tenant_name", "academic_years", type_="unique")
    op.drop_table("academic_years")

    op.drop_column("classrooms", "capacity")
