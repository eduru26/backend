"""RAG: full-text search column on document_chunks for hybrid retrieval

Adds:
  * `rag_document_chunks.content_tsv` — a generated tsvector column over
    `content`, English config. Generated means we don't need a trigger
    and Postgres keeps it in sync atomically on insert/update.
  * GIN index on the column so `@@` queries are O(log n).

Why generated rather than a trigger? Lower operational footprint —
trigger-free schemas are easier to reason about and migrate.

Revision ID: 0022_rag_fts
Revises: 0021_rag_corpus
Create Date: 2026-05-21
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0022_rag_fts"
down_revision: str | None = "0021_rag_corpus"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE rag_document_chunks
        ADD COLUMN IF NOT EXISTS content_tsv tsvector
            GENERATED ALWAYS AS (to_tsvector('english', coalesce(content, ''))) STORED;
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_rag_chunks_content_tsv "
        "ON rag_document_chunks USING GIN (content_tsv);"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_rag_chunks_content_tsv;")
    op.execute("ALTER TABLE rag_document_chunks DROP COLUMN IF EXISTS content_tsv;")
