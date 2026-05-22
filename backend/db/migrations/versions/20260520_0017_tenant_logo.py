"""add tenants.logo_url (school logo, stored as a data URI)

Revision ID: 0017_tenant_logo
Revises: 0016_classroom_slot_unique
Create Date: 2026-05-20
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0017_tenant_logo"
down_revision: str | None = "0016_classroom_slot_unique"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS logo_url TEXT;")


def downgrade() -> None:
    op.execute("ALTER TABLE tenants DROP COLUMN IF EXISTS logo_url;")
