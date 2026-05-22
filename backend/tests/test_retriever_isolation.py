from __future__ import annotations

import os
import uuid

import pytest

from backend.db.base import get_session_factory
from backend.db.models import RagDocument, Tenant, UserRole
from backend.rag.identity import IdentityClaims
from backend.rag.ingest import RagIngestService
from backend.rag.retriever import Retriever


pytestmark = pytest.mark.asyncio


# Skip when no DB is reachable (e.g. CI doesn't run Postgres).
_DB = os.environ.get("DATABASE_URL", "")
requires_db = pytest.mark.skipif(
    not _DB or "@" not in _DB,
    reason="requires DATABASE_URL pointing at Postgres",
)


async def _doc_ids_for_tenant(session, tenant_id) -> set[str]:
    from sqlalchemy import select
    rows = await session.execute(
        select(RagDocument.id).where(RagDocument.tenant_id == tenant_id)
    )
    return {str(r) for r in rows.scalars().all()}


@requires_db
async def test_retriever_rbac_isolation() -> None:
    """Two RBAC properties of the metadata-filter retriever:

    1. Cross-tenant: ingest a policy under two tenants, retrieve as a
       caller in each — every hit's document must belong to the caller's
       tenant (verified against the real document→tenant mapping).
    2. Role visibility: a chunk tagged role_visibility=['teacher'] must
       not surface for a student caller in the same tenant.

    Both checks run in one test function so they share a single event
    loop — the module-level engine pool is loop-bound.
    """
    factory = get_session_factory()
    async with factory() as session:
        from sqlalchemy import select
        tenants = (await session.execute(
            select(Tenant).order_by(Tenant.created_at).limit(2)
        )).scalars().all()
        if len(tenants) < 2:
            pytest.skip("need at least 2 tenants for isolation test")

        t_a, t_b = tenants[0], tenants[1]

        svc = RagIngestService(session)
        await svc.ingest_text(
            tenant_id=t_a.id, kind="policy", title="A policy",
            text=f"Tenant A — Leave policy says 5 days per term. ({t_a.id})",
            uploaded_by=None,
        )
        await svc.ingest_text(
            tenant_id=t_b.id, kind="policy", title="B policy",
            text=f"Tenant B — Leave policy says 10 days per term. ({t_b.id})",
            uploaded_by=None,
        )
        await session.commit()

    async with factory() as session:
        a_docs = await _doc_ids_for_tenant(session, t_a.id)
        b_docs = await _doc_ids_for_tenant(session, t_b.id)

        # Caller in tenant A — every hit must be an A-owned document.
        identity_a = IdentityClaims(
            role=UserRole.school_admin, user_id=uuid.uuid4(), tenant_id=t_a.id,
            role_visibility=("public", "school_admin", "teacher", "student", "parent"),
        )
        chunks = await Retriever(session).retrieve(
            "What is the leave policy?", identity_a,
        )
        assert chunks, "expected at least one chunk for tenant A"
        for c in chunks:
            assert c.document_id in a_docs, f"chunk doc {c.document_id} not owned by tenant A"
            assert c.document_id not in b_docs, f"cross-tenant leak: {c.document_id}"

        # Caller in tenant B — symmetric.
        identity_b = IdentityClaims(
            role=UserRole.school_admin, user_id=uuid.uuid4(), tenant_id=t_b.id,
            role_visibility=("public", "school_admin", "teacher", "student", "parent"),
        )
        chunks_b = await Retriever(session).retrieve(
            "What is the leave policy?", identity_b,
        )
        assert chunks_b, "expected at least one chunk for tenant B"
        for c in chunks_b:
            assert c.document_id in b_docs, f"chunk doc {c.document_id} not owned by tenant B"
            assert c.document_id not in a_docs, f"cross-tenant leak: {c.document_id}"

    # ── Property 2: role-visibility isolation ───────────────
    async with factory() as session:
        svc = RagIngestService(session)
        teacher_doc = await svc.ingest_text(
            tenant_id=t_a.id, kind="note", title="Teacher-only briefing",
            text="CONFIDENTIAL teacher briefing — exam moderation guidance for staff.",
            uploaded_by=None, role_visibility=["teacher"],
        )
        await session.commit()

    async with factory() as session:
        student_identity = IdentityClaims(
            role=UserRole.student, user_id=uuid.uuid4(), tenant_id=t_a.id,
            role_visibility=("public", "student"),
        )
        chunks = await Retriever(session).retrieve(
            "exam moderation guidance", student_identity,
        )
        for c in chunks:
            assert c.document_id != teacher_doc.document_id, (
                "student retrieved a teacher-only chunk"
            )
