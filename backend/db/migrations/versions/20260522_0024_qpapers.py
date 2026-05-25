"""Question papers: templates, report cards, assessment/question columns

* question_paper_templates — school-admin-defined exam skeletons.
* report_cards            — persisted result snapshot per assessment.
* assessments  +columns   — classroom_id, template_id, status, generation_meta.
* questions    +columns   — question_text, section_no, is_optional.

All assessment/question additions are nullable/defaulted so the existing
CSV marks-ingest path is unaffected. `assessments.status` defaults to
'finalized' so legacy rows still count in analytics.

Revision ID: 0024_qpapers
Revises: 0023_curriculum
Create Date: 2026-05-22
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0024_qpapers"
down_revision: str | None = "0023_curriculum"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── question_paper_templates ───────────────────────────
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS question_paper_templates (
            id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id    UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            name         VARCHAR(200) NOT NULL,
            exam_type    VARCHAR(20) NOT NULL,
            total_marks  NUMERIC(8,2) NOT NULL,
            sections     JSONB NOT NULL DEFAULT '[]'::jsonb,
            created_by   UUID REFERENCES users(id) ON DELETE SET NULL,
            is_active    BOOLEAN NOT NULL DEFAULT true,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_qp_templates_tenant_name UNIQUE (tenant_id, name)
        );
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_qp_templates_tenant "
        "ON question_paper_templates (tenant_id);"
    )

    # ── assessments: question-paper columns ────────────────
    op.execute(
        "ALTER TABLE assessments "
        "ADD COLUMN IF NOT EXISTS classroom_id UUID "
        "REFERENCES classrooms(id) ON DELETE SET NULL;"
    )
    op.execute(
        "ALTER TABLE assessments "
        "ADD COLUMN IF NOT EXISTS template_id UUID "
        "REFERENCES question_paper_templates(id) ON DELETE SET NULL;"
    )
    op.execute(
        "ALTER TABLE assessments "
        "ADD COLUMN IF NOT EXISTS status VARCHAR(20) NOT NULL DEFAULT 'finalized';"
    )
    op.execute(
        "ALTER TABLE assessments ADD COLUMN IF NOT EXISTS generation_meta JSONB;"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_assessments_classroom "
        "ON assessments (classroom_id) WHERE classroom_id IS NOT NULL;"
    )

    # ── questions: question-paper columns ──────────────────
    op.execute("ALTER TABLE questions ADD COLUMN IF NOT EXISTS question_text TEXT;")
    op.execute("ALTER TABLE questions ADD COLUMN IF NOT EXISTS section_no SMALLINT;")
    op.execute(
        "ALTER TABLE questions "
        "ADD COLUMN IF NOT EXISTS is_optional BOOLEAN NOT NULL DEFAULT false;"
    )

    # ── report_cards ───────────────────────────────────────
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS report_cards (
            id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            assessment_id UUID NOT NULL UNIQUE REFERENCES assessments(id) ON DELETE CASCADE,
            tenant_id     UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            classroom_id  UUID REFERENCES classrooms(id) ON DELETE SET NULL,
            summary_json  JSONB NOT NULL DEFAULT '{}'::jsonb,
            generated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            generated_by  UUID REFERENCES users(id) ON DELETE SET NULL
        );
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_report_cards_tenant "
        "ON report_cards (tenant_id);"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS report_cards;")
    op.execute("ALTER TABLE questions DROP COLUMN IF EXISTS is_optional;")
    op.execute("ALTER TABLE questions DROP COLUMN IF EXISTS section_no;")
    op.execute("ALTER TABLE questions DROP COLUMN IF EXISTS question_text;")
    op.execute("DROP INDEX IF EXISTS ix_assessments_classroom;")
    op.execute("ALTER TABLE assessments DROP COLUMN IF EXISTS generation_meta;")
    op.execute("ALTER TABLE assessments DROP COLUMN IF EXISTS status;")
    op.execute("ALTER TABLE assessments DROP COLUMN IF EXISTS template_id;")
    op.execute("ALTER TABLE assessments DROP COLUMN IF EXISTS classroom_id;")
    op.execute("DROP TABLE IF EXISTS question_paper_templates;")
