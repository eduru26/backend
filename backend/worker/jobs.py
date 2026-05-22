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
