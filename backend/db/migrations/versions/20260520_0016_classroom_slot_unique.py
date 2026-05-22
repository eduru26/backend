"""prevent a classroom being double-booked in the same day/period

Revision ID: 0016_classroom_slot_unique
Revises: 0015_period_day_type
Create Date: 2026-05-20
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0016_classroom_slot_unique"
down_revision: str | None = "0015_period_day_type"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Drop any pre-existing double-bookings (keep the most recently created
    # row per classroom slot) so the unique index can be created.
    op.execute(
        """
        DELETE FROM timetable_entries t
        USING (
            SELECT id, row_number() OVER (
                PARTITION BY classroom_id, day_of_week, period_number
                ORDER BY created_at DESC, id
            ) AS rn
            FROM timetable_entries
            WHERE classroom_id IS NOT NULL
        ) d
        WHERE t.id = d.id AND d.rn > 1;
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_timetable_classroom_slot
        ON timetable_entries (classroom_id, day_of_week, period_number)
        WHERE classroom_id IS NOT NULL;
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_timetable_classroom_slot;")
