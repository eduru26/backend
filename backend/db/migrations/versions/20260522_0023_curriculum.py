"""Curriculum: chapters table + topics.chapter_id

Adds a tenant-scoped Chapter entity between Subject and Topic so a school's
curriculum can be organised as Subject -> Chapter -> Topic. `topics.chapter_id`
is nullable with ON DELETE SET NULL — existing (CSV-ingested) topics keep
working and simply don't roll up to a chapter.

Revision ID: 0023_curriculum
Revises: 0022_rag_fts
Create Date: 2026-05-22
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0023_curriculum"
down_revision: str | None = "0022_rag_fts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS chapters (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            subject_id  UUID NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
            grade       SMALLINT NOT NULL,
            name        VARCHAR(200) NOT NULL,
            order_num   SMALLINT NOT NULL DEFAULT 0,
            source      VARCHAR(20) NOT NULL DEFAULT 'manual',
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_chapters_tenant_subject_grade_name
                UNIQUE (tenant_id, subject_id, grade, name)
        );
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_chapters_tenant ON chapters (tenant_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_chapters_subject ON chapters (subject_id);"
    )

    op.execute(
        "ALTER TABLE topics ADD COLUMN IF NOT EXISTS chapter_id UUID "
        "REFERENCES chapters(id) ON DELETE SET NULL;"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_topics_chapter "
        "ON topics (chapter_id) WHERE chapter_id IS NOT NULL;"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_topics_chapter;")
    op.execute("ALTER TABLE topics DROP COLUMN IF EXISTS chapter_id;")
    op.execute("DROP TABLE IF EXISTS chapters;")
