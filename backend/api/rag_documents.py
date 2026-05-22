from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from backend.api.deps import SessionDep, assert_tenant, require_roles
from backend.core.logging import get_logger
from backend.db.models import User, UserRole
from backend.rag.pdf_extract import PdfUnsupportedError, extract_text as extract_pdf_text
from backend.worker.queue import enqueue, job_status

logger = get_logger(__name__)

router = APIRouter(prefix="/rag", tags=["rag"])


# Only staff can ingest into the corpus. Students/parents can only read
# via chat-side retrieval. school_admin is allowed so they can seed
# policies and curriculum docs.
StaffDep = Annotated[
    User, Depends(require_roles(UserRole.teacher, UserRole.school_admin))
]


_ALLOWED_KINDS = {"curriculum", "policy", "note", "upload"}
_ALLOWED_VIS = {"public", "student", "parent", "teacher", "school_admin"}


class EnqueuedOut(BaseModel):
    """Ingestion is async — the document is chunked + embedded by the arq
    worker. Poll GET /rag/jobs/{job_id} for completion."""
    job_id: str
    status: str = "queued"


class JobStatusOut(BaseModel):
    job_id: str
    status: str
    result: dict | None = None
    error: str | None = None


def _parse_visibility(role_visibility: str) -> list[str]:
    vis_tags = [t.strip() for t in role_visibility.split(",") if t.strip()]
    bad = [t for t in vis_tags if t not in _ALLOWED_VIS]
    if bad:
        raise HTTPException(400, f"unknown role_visibility tags: {bad}")
    return vis_tags or ["public"]


def _parse_classroom(classroom_id: str | None) -> uuid.UUID | None:
    if not classroom_id:
        return None
    try:
        return uuid.UUID(classroom_id)
    except ValueError:
        raise HTTPException(400, "invalid classroom_id")


async def _enqueue_ingest(
    *, tenant_id: uuid.UUID, kind: str, title: str, text: str,
    uploaded_by: uuid.UUID, vis_tags: list[str], classroom_id: uuid.UUID | None,
    subject: str | None, topic: str | None, source_uri: str | None,
    metadata: dict,
) -> EnqueuedOut:
    job_id = await enqueue(
        "ingest_document_job",
        tenant_id=str(tenant_id),
        kind=kind,
        title=title,
        text=text,
        uploaded_by=str(uploaded_by),
        role_visibility=vis_tags,
        classroom_id=str(classroom_id) if classroom_id else None,
        subject=subject,
        topic=topic,
        source_uri=source_uri,
        metadata=metadata,
    )
    if job_id is None:
        # Queue unreachable — surface clearly rather than silently dropping
        # the document.
        raise HTTPException(503, "ingestion queue unavailable, please retry")
    return EnqueuedOut(job_id=job_id)


@router.post("/documents", response_model=EnqueuedOut, status_code=202)
async def ingest_document(
    user: StaffDep,
    file: UploadFile = File(...),
    kind: str = Form(...),
    title: str = Form(...),
    role_visibility: str = Form("public"),     # comma-separated tags
    classroom_id: str | None = Form(None),
    subject: str | None = Form(None),
    topic: str | None = Form(None),
) -> EnqueuedOut:
    """Queue a UTF-8 text document for indexing into the RAG corpus.

    Returns 202 immediately — chunking + embedding run on the arq worker
    so the request thread isn't held for the (slow) embedding calls.
    """
    tid = assert_tenant(user)
    if kind not in _ALLOWED_KINDS:
        raise HTTPException(400, f"kind must be one of {sorted(_ALLOWED_KINDS)}")
    vis_tags = _parse_visibility(role_visibility)
    cid = _parse_classroom(classroom_id)

    raw = await file.read()
    if not raw:
        raise HTTPException(400, "empty file")
    if len(raw) > 2 * 1024 * 1024:
        raise HTTPException(413, "file too large (max 2 MB)")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(415, "file must be UTF-8 text")

    return await _enqueue_ingest(
        tenant_id=tid, kind=kind, title=title, text=text, uploaded_by=user.id,
        vis_tags=vis_tags, classroom_id=cid, subject=subject, topic=topic,
        source_uri=file.filename,
        metadata={"content_type": file.content_type or "text/plain"},
    )


@router.post("/documents/pdf", response_model=EnqueuedOut, status_code=202)
async def ingest_pdf(
    user: StaffDep,
    file: UploadFile = File(...),
    kind: str = Form("policy"),
    title: str | None = Form(None),
    role_visibility: str = Form("public"),
    classroom_id: str | None = Form(None),
    subject: str | None = Form(None),
    topic: str | None = Form(None),
) -> EnqueuedOut:
    """Queue a PDF for indexing.

    Text extraction (pypdf) runs inline — it's fast and lets us reject
    image-only PDFs synchronously. The slow part (chunking + embedding)
    is what gets queued.
    """
    tid = assert_tenant(user)
    if kind not in _ALLOWED_KINDS:
        raise HTTPException(400, f"kind must be one of {sorted(_ALLOWED_KINDS)}")
    vis_tags = _parse_visibility(role_visibility)
    cid = _parse_classroom(classroom_id)

    raw = await file.read()
    if not raw:
        raise HTTPException(400, "empty file")
    if len(raw) > 5 * 1024 * 1024:
        raise HTTPException(413, "PDF too large (max 5 MB)")
    if (file.content_type or "").lower() not in ("application/pdf", ""):
        if not (file.filename or "").lower().endswith(".pdf"):
            raise HTTPException(415, "file must be application/pdf")

    try:
        text = extract_pdf_text(raw)
    except PdfUnsupportedError as exc:
        raise HTTPException(415, str(exc))
    if not text.strip():
        raise HTTPException(422, "no extractable text in PDF (image-only PDF?)")

    return await _enqueue_ingest(
        tenant_id=tid, kind=kind, title=title or (file.filename or "PDF upload"),
        text=text, uploaded_by=user.id, vis_tags=vis_tags, classroom_id=cid,
        subject=subject, topic=topic, source_uri=file.filename,
        metadata={"content_type": "application/pdf", "byte_size": len(raw)},
    )


@router.post("/reindex", response_model=EnqueuedOut, status_code=202)
async def reindex_existing(user: StaffDep) -> EnqueuedOut:
    """Queue a re-index of this tenant's announcements + assignments."""
    tid = assert_tenant(user)
    job_id = await enqueue("reindex_job", tenant_id=str(tid))
    if job_id is None:
        raise HTTPException(503, "ingestion queue unavailable, please retry")
    return EnqueuedOut(job_id=job_id)


@router.get("/jobs/{job_id}", response_model=JobStatusOut)
async def get_job(job_id: str, user: StaffDep) -> JobStatusOut:
    """Poll the status of a previously queued ingestion job.

    Statuses: queued / in_progress / complete / not_found. When complete,
    `result` carries {document_id, chunks, deduplicated} (or the reindex
    counts).
    """
    info = await job_status(job_id)
    return JobStatusOut(
        job_id=job_id,
        status=info.get("status", "unknown"),
        result=info.get("result") if isinstance(info.get("result"), dict) else None,
        error=info.get("error"),
    )
