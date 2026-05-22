"""add school_admin role and classrooms table

Revision ID: 0005_school_admin_classrooms
Revises: 0004_add_tenants
Create Date: 2026-05-07

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0005_school_admin_classrooms"
down_revision: str | None = "0004_add_tenants"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = inspector.get_table_names()

    # ── 1. Add school_admin to the user_role enum ──────────
    op.execute(sa.text("ALTER TYPE user_role ADD VALUE IF NOT EXISTS 'school_admin'"))

    # ── 2. classrooms table ────────────────────────────────
    if "classrooms" not in existing:
        op.create_table(
            "classrooms",
            sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
            sa.Column(
                "tenant_id",
                UUID(as_uuid=True),
                sa.ForeignKey("tenants.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("name", sa.String(100), nullable=False),
            sa.Column("grade", sa.SmallInteger, nullable=False),
            sa.Column("section", sa.String(8), nullable=False),
            sa.Column(
                "teacher_id",
                UUID(as_uuid=True),
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
        )
        op.create_index("ix_classrooms_tenant_id", "classrooms", ["tenant_id"])
        op.create_index("ix_classrooms_teacher_id", "classrooms", ["teacher_id"])
        op.create_unique_constraint(
            "uq_classrooms_tenant_name", "classrooms", ["tenant_id", "name"]
        )

    # ── 3. Add classroom_id to students ────────────────────
    existing_cols = [c["name"] for c in inspector.get_columns("students")]
    if "classroom_id" not in existing_cols:
        op.add_column(
            "students",
            sa.Column("classroom_id", UUID(as_uuid=True), nullable=True),
        )
        op.create_foreign_key(
            "fk_students_classroom_id",
            "students", "classrooms",
            ["classroom_id"], ["id"],
            ondelete="SET NULL",
        )
        op.create_index("ix_students_classroom_id", "students", ["classroom_id"])


def downgrade() -> None:
    op.drop_index("ix_students_classroom_id", table_name="students")
    op.drop_constraint("fk_students_classroom_id", "students", type_="foreignkey")
    op.drop_column("students", "classroom_id")

    op.drop_index("ix_classrooms_teacher_id", table_name="classrooms")
    op.drop_index("ix_classrooms_tenant_id", table_name="classrooms")
    op.drop_constraint("uq_classrooms_tenant_name", "classrooms", type_="unique")
    op.drop_table("classrooms")
