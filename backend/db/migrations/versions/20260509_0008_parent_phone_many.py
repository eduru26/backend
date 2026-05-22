"""replace parent email with phone, many-to-many parent-student

Revision ID: 0008_parent_phone_many
Revises: 0007_teacher_specialization
Create Date: 2026-05-09
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0008_parent_phone_many"
down_revision: str | None = "0007_teacher_specialization"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = inspector.get_table_names()
    user_cols  = {c["name"] for c in inspector.get_columns("users")}
    stu_cols   = {c["name"] for c in inspector.get_columns("students")}

    # ── 1. Make users.email nullable ──────────────────────
    op.alter_column("users", "email", nullable=True)

    # ── 2. Add users.phone ─────────────────────────────────
    if "phone" not in user_cols:
        op.add_column("users", sa.Column("phone", sa.String(20), nullable=True))
        # Partial unique index — allows multiple NULLs
        op.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_users_phone "
            "ON users(phone) WHERE phone IS NOT NULL"
        )

    # ── 3. Add students.parent_phone ──────────────────────
    if "parent_phone" not in stu_cols:
        op.add_column("students", sa.Column("parent_phone", sa.String(20), nullable=True))

    # ── 4. Drop students.parent_email ─────────────────────
    if "parent_email" in stu_cols:
        op.drop_column("students", "parent_email")

    # ── 5. Create parent_students junction table ──────────
    if "parent_students" not in existing_tables:
        op.create_table(
            "parent_students",
            sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
            sa.Column(
                "parent_id", UUID(as_uuid=True),
                sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
            ),
            sa.Column(
                "student_id", UUID(as_uuid=True),
                sa.ForeignKey("students.id", ondelete="CASCADE"), nullable=False,
            ),
            sa.Column(
                "created_at", sa.DateTime(timezone=True),
                server_default=sa.text("now()"), nullable=False,
            ),
        )
        op.create_index("ix_parent_students_parent",  "parent_students", ["parent_id"])
        op.create_index("ix_parent_students_student", "parent_students", ["student_id"])
        op.create_unique_constraint(
            "uq_parent_students", "parent_students", ["parent_id", "student_id"]
        )

    # ── 6. Migrate existing student_id → junction table ───
    if "student_id" in user_cols:
        op.execute("""
            INSERT INTO parent_students (id, parent_id, student_id, created_at)
            SELECT gen_random_uuid(), id, student_id, NOW()
            FROM users
            WHERE role = 'parent' AND student_id IS NOT NULL
            ON CONFLICT (parent_id, student_id) DO NOTHING
        """)
        op.drop_column("users", "student_id")


def downgrade() -> None:
    op.add_column("users", sa.Column(
        "student_id", UUID(as_uuid=True),
        sa.ForeignKey("students.id", ondelete="CASCADE"), nullable=True,
    ))
    op.drop_constraint("uq_parent_students", "parent_students", type_="unique")
    op.drop_index("ix_parent_students_student", table_name="parent_students")
    op.drop_index("ix_parent_students_parent",  table_name="parent_students")
    op.drop_table("parent_students")
    op.drop_column("students", "parent_phone")
    op.drop_column("users", "phone")
    op.alter_column("users", "email", nullable=False)
