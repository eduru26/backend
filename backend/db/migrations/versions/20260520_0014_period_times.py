"""add period_times bell schedule + class_reminder notification kind

Revision ID: 0014_period_times
Revises: 0013_announcements_notifications
Create Date: 2026-05-20
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0014_period_times"
down_revision: str | None = "0013_announcements_notifications"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS period_times (
            id            UUID PRIMARY KEY,
            tenant_id     UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            period_number SMALLINT NOT NULL,
            start_time    TIME NOT NULL,
            end_time      TIME NOT NULL,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_period_times_tenant_period UNIQUE (tenant_id, period_number)
        );
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_period_times_tenant ON period_times (tenant_id);"
    )

    # Add new enum value for class reminders. ALTER TYPE ... ADD VALUE cannot
    # run inside a transaction block nor a DO block, so use autocommit.
    with op.get_context().autocommit_block():
        op.execute(
            "ALTER TYPE notification_kind ADD VALUE IF NOT EXISTS 'class_reminder'"
        )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_period_times_tenant;")
    op.execute("DROP TABLE IF EXISTS period_times;")
    # NOTE: Postgres does not support dropping a single enum value; leave class_reminder in place.
