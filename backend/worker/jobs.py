from __future__ import annotations

import uuid
from typing import Any

from backend.core.logging import get_logger
from backend.db.base import get_session_factory

logger = get_logger(__name__)


# Each job opens its own DB session — the worker has no request lifecycle
# to borrow one from. We commit explicitly: arq doesn't wrap jobs in a
# transaction.


async def ingest_document_job(
    ctx: dict,
    *,
    tenant_id: str,
    kind: str,
    title: str,
    text: str,
    uploaded_by: str | None,
    role_visibility: list[str] | None = None,
    classroom_id: str | None = None,
    subject: str | None = None,
    topic: str | None = None,
    source_uri: str | None = None,
    metadata: dict | None = None,
) -> dict[str, Any]:
    """Chunk + embed + store a document. The slow path — embeddings of a
    multi-page doc take seconds, so this runs off the request thread."""
    from backend.rag.ingest import RagIngestService

    factory = get_session_factory()
    async with factory() as session:
        svc = RagIngestService(session)
        res = await svc.ingest_text(
            tenant_id=uuid.UUID(tenant_id),
            kind=kind,
            title=title,
            text=text,
            uploaded_by=uuid.UUID(uploaded_by) if uploaded_by else None,
            role_visibility=role_visibility,
            classroom_id=uuid.UUID(classroom_id) if classroom_id else None,
            subject=subject,
            topic=topic,
            source_uri=source_uri,
            metadata=metadata,
        )
        await session.commit()
    logger.info("job.ingest_document.done", document_id=res.document_id,
                chunks=res.chunks, dedup=res.deduplicated)
    return {"document_id": res.document_id, "chunks": res.chunks,
            "deduplicated": res.deduplicated}


async def reindex_job(ctx: dict, *, tenant_id: str) -> dict[str, Any]:
    """Re-index a tenant's announcements + assignments into the RAG corpus."""
    from backend.rag.ingest import RagIngestService

    tid = uuid.UUID(tenant_id)
    factory = get_session_factory()
    async with factory() as session:
        svc = RagIngestService(session)
        ann = await svc.reindex_announcements(tenant_id=tid)
        asg = await svc.reindex_assignments(tenant_id=tid)
        await session.commit()
    logger.info("job.reindex.done", tenant=tenant_id, ann=ann, asg=asg)
    return {"announcements_indexed": ann, "assignments_indexed": asg}


async def lms_event_job(
    ctx: dict, *, event_slug: str, payload: dict
) -> dict[str, Any]:
    """Process an inbound LMS event off the request thread.

    The router validated the slug + payload shape already; here we
    reconstruct the typed payload and run the handler. Durable: if the
    backend crashes mid-processing the job is retried by arq.
    """
    from backend.lms.handlers import EVENT_DISPATCH

    entry = EVENT_DISPATCH.get(event_slug)
    if entry is None:
        logger.warning("job.lms_event.unknown_slug", slug=event_slug)
        return {"ok": False, "reason": "unknown_slug"}

    payload_cls, handler = entry
    typed = payload_cls(**payload)
    await handler(typed)
    logger.info("job.lms_event.done", slug=event_slug)
    return {"ok": True, "slug": event_slug}



async def ingest_textbook_job(
    ctx: dict, *, ingestion_id: str,
) -> dict[str, Any]:
    """Run the docling-based ingest pipeline for one uploaded PDF.

    Stages: extracting → chunking → embedding → ready. On exception the
    row is marked failed in a fresh session so the admin UI sees a
    clear error rather than a row stuck in 'extracting' forever.

    Lives in this module because it's a regular arq function, but the
    dedicated ingest-worker service is the only worker that registers
    it (see backend/worker/ingest_settings.py). Keeping it out of the
    main worker prevents the docling 2-3 GB model load from bloating
    every replica of the chat/paper-gen worker.
    """
    from backend.services.textbook_ingest import (
        _IngestCancelled, mark_ingestion_failed, run_ingestion,
    )

    iid = uuid.UUID(ingestion_id)
    factory = get_session_factory()
    try:
        async with factory() as session:
            await run_ingestion(session, iid)
            await session.commit()
        return {"ingestion_id": ingestion_id, "status": "ready"}
    except _IngestCancelled as exc:
        # Admin cancelled or deleted the row mid-job. The status is
        # already `cancelled` (or the row is gone) — don't overwrite
        # it with `failed`.
        logger.info("job.ingest_textbook.cancelled",
                    ingestion_id=ingestion_id, reason=str(exc))
        return {"ingestion_id": ingestion_id, "status": "cancelled"}
    except Exception as exc:  # noqa: BLE001
        logger.exception("job.ingest_textbook.failed",
                         ingestion_id=ingestion_id, error=str(exc))
        async with factory() as session:
            await mark_ingestion_failed(session, iid, str(exc))
        return {"ingestion_id": ingestion_id, "status": "failed",
                "error": str(exc)[:500]}


async def generate_paper_job(
    ctx: dict, *, assessment_id: str
) -> dict[str, Any]:
    """Generate a question paper for an Assessment created in 'generating'
    status. On any hard failure the paper is left as 'draft' with the
    error recorded so the teacher can edit or regenerate."""
    from backend.db.models import Assessment
    from backend.services.paper_generator import PaperGenerator

    aid = uuid.UUID(assessment_id)
    factory = get_session_factory()
    async with factory() as session:
        try:
            await PaperGenerator(session).generate(aid)
            await session.commit()
        except Exception as exc:  # noqa: BLE001
            await session.rollback()
            logger.exception("job.generate_paper.failed",
                             assessment_id=assessment_id, error=str(exc))
            # mark the paper draft with the error so the UI can surface it
            async with factory() as s2:
                a = await s2.get(Assessment, aid)
                if a is not None:
                    meta = dict(a.generation_meta or {})
                    meta["error"] = str(exc)[:1000]
                    a.generation_meta = meta
                    a.status = "draft"
                    await s2.commit()
            return {"ok": False, "error": str(exc)}
    logger.info("job.generate_paper.done", assessment_id=assessment_id)
    return {"ok": True, "assessment_id": assessment_id}
