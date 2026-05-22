"""add timetable_entries table

Revision ID: 0009_timetable
Revises: 0008_parent_phone_many
Create Date: 2026-05-09
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0009_timetable"
down_revision: str | None = "0008_parent_phone_many"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "timetable_entries" in inspector.get_table_names():
        return

    op.create_table(
        "timetable_entries",
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
            sa.ForeignKey("classrooms.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("day_of_week",   sa.SmallInteger, nullable=False),  # 1=Mon … 6=Sat
        sa.Column("period_number", sa.SmallInteger, nullable=False),  # 1-8
        sa.Column("subject", sa.String(100), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
    )
    op.create_index("ix_timetable_tenant",  "timetable_entries", ["tenant_id"])
    op.create_index("ix_timetable_teacher", "timetable_entries", ["teacher_id"])
    op.create_unique_constraint(
        "uq_timetable_slot", "timetable_entries",
        ["teacher_id", "day_of_week", "period_number"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_timetable_slot", "timetable_entries", type_="unique")
    op.drop_index("ix_timetable_teacher", table_name="timetable_entries")
    op.drop_index("ix_timetable_tenant",  table_name="timetable_entries")
    op.drop_table("timetable_entries")
