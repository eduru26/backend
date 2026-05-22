"""RAG: documents, document_chunks (pgvector + HNSW), query_events

Adds the three tables that back the Metadata-Filtered RAG layer and the
query telemetry surface used by the proactive-suggestions endpoint.

* documents       — one row per uploaded/indexed source. Idempotent on
                    sha256. Holds tenant/role/visibility metadata that is
                    inherited by all of its chunks.
* document_chunks — the searchable unit. Stores the chunk text, its 768-d
                    embedding, and the metadata filters used by the
                    retriever before vector similarity is computed. The
                    HNSW index on `embedding` makes top-K cosine search
                    fast; the btree on `(tenant_id, role_visibility)` is
                    what enforces RBAC at retrieval time.
* query_events    — append-only audit + analytics. Lets us answer "what
                    are teachers asking most this week" without trawling
                    the messages table for free-text.

Revision ID: 0021_rag_corpus
Revises: 0020_student_user_handle
Create Date: 2026-05-21
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0021_rag_corpus"
down_revision: str | None = "0020_student_user_handle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── documents ──────────────────────────────────────────
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS rag_documents (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            kind            VARCHAR(40) NOT NULL,           -- curriculum|policy|announcement|assignment|note|upload
            title           VARCHAR(300) NOT NULL,
            source_uri      VARCHAR(500),                   -- original path or external link
            sha256          CHAR(64) NOT NULL,              -- idempotency
            uploaded_by     UUID REFERENCES users(id) ON DELETE SET NULL,
            -- inherited by every chunk; chunks may also pin a narrower scope
            role_visibility TEXT[] NOT NULL DEFAULT ARRAY['public']::TEXT[],
            student_id      UUID REFERENCES students(id) ON DELETE CASCADE,
            classroom_id    UUID REFERENCES classrooms(id) ON DELETE CASCADE,
            subject         VARCHAR(100),
            topic           VARCHAR(150),
            metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,
            chunk_count     INT NOT NULL DEFAULT 0,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_rag_documents_tenant_sha UNIQUE (tenant_id, sha256)
        );
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_rag_documents_tenant_kind "
        "ON rag_documents (tenant_id, kind);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_rag_documents_classroom "
        "ON rag_documents (classroom_id) WHERE classroom_id IS NOT NULL;"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_rag_documents_student "
        "ON rag_documents (student_id) WHERE student_id IS NOT NULL;"
    )

    # ── document_chunks ────────────────────────────────────
    # pgvector extension is already enabled by migration 0001.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS rag_document_chunks (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            document_id     UUID NOT NULL REFERENCES rag_documents(id) ON DELETE CASCADE,
            tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            chunk_index     INT NOT NULL,
            content         TEXT NOT NULL,
            embedding       vector(768),
            -- denormalised from the parent doc for fast filter-before-search.
            role_visibility TEXT[] NOT NULL DEFAULT ARRAY['public']::TEXT[],
            student_id      UUID REFERENCES students(id) ON DELETE CASCADE,
            classroom_id    UUID REFERENCES classrooms(id) ON DELETE CASCADE,
            subject         VARCHAR(100),
            topic           VARCHAR(150),
            metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_rag_chunks_doc_idx UNIQUE (document_id, chunk_index)
        );
        """
    )
    # Filter-first indexes — these run BEFORE vector similarity is
    # computed because we always pre-filter by tenant and role_visibility
    # array overlap. The btree on tenant_id is cheap and selective.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_rag_chunks_tenant "
        "ON rag_document_chunks (tenant_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_rag_chunks_role_vis "
        "ON rag_document_chunks USING GIN (role_visibility);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_rag_chunks_classroom "
        "ON rag_document_chunks (classroom_id) WHERE classroom_id IS NOT NULL;"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_rag_chunks_student "
        "ON rag_document_chunks (student_id) WHERE student_id IS NOT NULL;"
    )
    # HNSW for fast approximate cosine search. m=16, ef_construction=64
    # are reasonable defaults for <1M chunks. We can rebuild with higher
    # ef once corpus grows. `vector_cosine_ops` matches the `<=>` operator
    # we use in the retriever query.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_rag_chunks_embedding_hnsw "
        "ON rag_document_chunks "
        "USING hnsw (embedding vector_cosine_ops) "
        "WITH (m = 16, ef_construction = 64);"
    )

    # ── query_events ───────────────────────────────────────
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS rag_query_events (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            role            VARCHAR(20) NOT NULL,
            intent          VARCHAR(40) NOT NULL,
            blocked         BOOLEAN NOT NULL DEFAULT FALSE,
            block_reason    VARCHAR(200),
            question_hash   CHAR(64) NOT NULL,        -- sha256 of the question for trend grouping w/o storing free text
            latency_ms      INT,
            rag_hits        INT NOT NULL DEFAULT 0,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_rag_query_events_tenant_created "
        "ON rag_query_events (tenant_id, created_at DESC);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_rag_query_events_tenant_intent "
        "ON rag_query_events (tenant_id, intent);"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS rag_query_events;")
    op.execute("DROP INDEX IF EXISTS ix_rag_chunks_embedding_hnsw;")
    op.execute("DROP INDEX IF EXISTS ix_rag_chunks_role_vis;")
    op.execute("DROP TABLE IF EXISTS rag_document_chunks;")
    op.execute("DROP TABLE IF EXISTS rag_documents;")
