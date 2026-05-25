"""Curriculum proposals — AI-extracted Chapter/Topic outlines for review

A textbook PDF upload creates a `curriculum_proposals` row; the
`extract_curriculum_job` worker fills `outline_json`; the school-admin
reviews and applies it into real Chapter/Topic rows.

Revision ID: 0025_curriculum_proposals
Revises: 0024_qpapers
Create Date: 2026-05-22
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0025_curriculum_proposals"
down_revision: str | None = "0024_qpapers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS curriculum_proposals (
            id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id    UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            subject_id   UUID NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
            grade        SMALLINT NOT NULL,
            title        VARCHAR(300) NOT NULL,
            status       VARCHAR(20) NOT NULL DEFAULT 'extracting',
            outline_json JSONB,
            error        TEXT,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_curriculum_proposals_tenant "
        "ON curriculum_proposals (tenant_id);"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS curriculum_proposals;")
