"""add announcements.broadcast_id (groups platform broadcasts across tenants)

Revision ID: 0018_announcement_broadcast_id
Revises: 0017_tenant_logo
Create Date: 2026-05-21
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0018_announcement_broadcast_id"
down_revision: str | None = "0017_tenant_logo"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE announcements ADD COLUMN IF NOT EXISTS broadcast_id UUID;"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_announcements_broadcast_id "
        "ON announcements (broadcast_id);"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_announcements_broadcast_id;")
    op.execute("ALTER TABLE announcements DROP COLUMN IF EXISTS broadcast_id;")
