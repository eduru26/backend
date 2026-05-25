"""Textbook PDF ingestion table — track docling pipeline state.

Replaces the offline textbook-indexer flow: the admin uploads a raw PDF,
the row is created in 'queued' state, the dedicated ingest worker walks
through extracting → chunking → embedding → ready as it processes the
PDF. Status + progress feed the admin UI so they can watch a slow OCR
job without polling stale data.

Revision ID: 0028_textbook_ingestion
Revises: 0027_question_grounding
Create Date: 2026-05-24
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0028_textbook_ingestion"
down_revision: str | None = "0027_question_grounding"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            CREATE TYPE textbook_ingest_status AS ENUM (
                'queued', 'extracting', 'chunking', 'embedding',
                'ready', 'failed', 'cancelled'
            );
        EXCEPTION WHEN duplicate_object THEN null; END $$;
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS textbook_ingestions (
            id              UUID PRIMARY KEY,
            tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            subject_id      UUID NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
            grade           SMALLINT NOT NULL,
            source_filename VARCHAR(300) NOT NULL,
            file_sha256     VARCHAR(64) NOT NULL,
            file_path       TEXT NOT NULL,
            file_size       INTEGER,
            status          textbook_ingest_status NOT NULL DEFAULT 'queued',
            progress        SMALLINT NOT NULL DEFAULT 0,
            error           TEXT,
            proposal_id     UUID REFERENCES curriculum_proposals(id) ON DELETE SET NULL,
            uploaded_by     UUID REFERENCES users(id) ON DELETE SET NULL,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_textbook_ingestions_tenant_sha
                UNIQUE (tenant_id, file_sha256)
        );
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_textbook_ingestions_tenant_id "
        "ON textbook_ingestions(tenant_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_textbook_ingestions_file_sha256 "
        "ON textbook_ingestions(file_sha256);"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS textbook_ingestions")
    op.execute("DROP TYPE IF EXISTS textbook_ingest_status")
