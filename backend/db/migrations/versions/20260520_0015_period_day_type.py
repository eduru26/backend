"""add day_type to period_times (weekday vs saturday schedules)

Revision ID: 0015_period_day_type
Revises: 0014_period_times
Create Date: 2026-05-20
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0015_period_day_type"
down_revision: str | None = "0014_period_times"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE period_times
            ADD COLUMN IF NOT EXISTS day_type VARCHAR(16) NOT NULL DEFAULT 'weekday';
        """
    )
    op.execute(
        "ALTER TABLE period_times "
        "DROP CONSTRAINT IF EXISTS uq_period_times_tenant_period;"
    )
    op.execute(
        """
        DO $$ BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'uq_period_times_tenant_daytype_period'
            ) THEN
                ALTER TABLE period_times
                    ADD CONSTRAINT uq_period_times_tenant_daytype_period
                    UNIQUE (tenant_id, day_type, period_number);
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE period_times "
        "DROP CONSTRAINT IF EXISTS uq_period_times_tenant_daytype_period;"
    )
    op.execute("ALTER TABLE period_times DROP COLUMN IF EXISTS day_type;")
    op.execute(
        """
        DO $$ BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'uq_period_times_tenant_period'
            ) THEN
                ALTER TABLE period_times
                    ADD CONSTRAINT uq_period_times_tenant_period
                    UNIQUE (tenant_id, period_number);
            END IF;
        END $$;
        """
    )
