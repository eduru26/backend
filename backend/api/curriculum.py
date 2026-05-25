"""School-admin curriculum endpoints — manage the Subject -> Chapter -> Topic
tree that question papers are generated against.

Textbooks enter the system as a raw **PDF upload**: the admin uploads
the bare PDF via `POST /school/curriculum/textbooks`, the file is
stored on a shared Docker volume, and the dedicated ingest worker runs
docling on it behind the scenes — extracting markdown, splitting into
chapters/topics, embedding chunks, and upserting to Qdrant. When the
job lands in `ready` it produces a CurriculumProposal that the admin
reviews and applies (which materialises Chapter/Topic rows and
back-tags the Qdrant chunks with chapter_id / topic_id).

All routes are school_admin-only and tenant-scoped.
"""
from __future__ import annotations

import hashlib
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from backend.api.deps import SessionDep, assert_tenant, require_roles
from backend.core.logging import get_logger
from backend.db.models import (
    SUBJECTS, CurriculumProposal, IngestStatus, Subject,
    TextbookIngestion, Topic, User, UserRole,
)
from backend.db.repositories.curriculum import ChapterRepository
from backend.rag.qdrant_store import get_qdrant_store
from backend.services.textbook_ingest import get_textbook_storage_dir
from backend.worker.queue import enqueue

logger = get_logger(__name__)

# nginx is configured for the same cap. Most school textbooks under
# 200 pages sit at 10-30 MB; legacy Indian textbooks scanned at high
# DPI can push 100 MB, hence the headroom.
_PDF_MAX_BYTES = 150 * 1024 * 1024

router = APIRouter(prefix="/school/curriculum", tags=["curriculum"])
SchoolAdminDep = Annotated[User, Depends(require_roles(UserRole.school_admin))]


# ── Schemas ──────────────────────────────────────────────


class SubjectOut(BaseModel):
    id: str
    name: str


class TopicOut(BaseModel):
    id: str
    name: str
    chapter_id: str | None


class ChapterOut(BaseModel):
    id: str
    subject_id: str
    grade: int
    name: str
    order_num: int
    source: str
    topics: list[TopicOut]


class TreeOut(BaseModel):
    subject_id: str
    grade: int
    chapters: list[ChapterOut]
    unassigned_topics: list[TopicOut]  # subject topics with no chapter


class ChapterIn(BaseModel):
    subject_id: str
    grade: int
    name: str
    order_num: int = 0


class ChapterPatch(BaseModel):
    name: str | None = None
    order_num: int | None = None


class TopicIn(BaseModel):
    chapter_id: str
    name: str


class TopicPatch(BaseModel):
    name: str | None = None
    chapter_id: str | None = None  # null string omitted; use /unlink to detach


# ── helpers ──────────────────────────────────────────────


def _uuid(value: str, label: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        raise HTTPException(400, f"invalid {label}")


def _topic_out(t: Topic) -> TopicOut:
    return TopicOut(
        id=str(t.id),
        name=t.name,
        chapter_id=str(t.chapter_id) if t.chapter_id else None,
    )


# ── subjects ─────────────────────────────────────────────


@router.get("/subjects", response_model=list[SubjectOut])
async def list_subjects(user: SchoolAdminDep, session: SessionDep) -> list[SubjectOut]:
    """Return all subjects, seeding the canonical SUBJECTS list on first use
    so the curriculum UI always has something to pick."""
    assert_tenant(user)
    existing = {
        s.name for s in (await session.execute(select(Subject))).scalars().all()
    }
    missing = [n for n in SUBJECTS if n not in existing]
    for name in missing:
        session.add(Subject(id=uuid.uuid4(), name=name))
    if missing:
        await session.flush()
    rows = await session.execute(select(Subject).order_by(Subject.name))
    return [SubjectOut(id=str(s.id), name=s.name) for s in rows.scalars().all()]


# ── tree ─────────────────────────────────────────────────


@router.get("/tree", response_model=TreeOut)
async def get_tree(
    user: SchoolAdminDep,
    session: SessionDep,
    subject_id: str,
    grade: int,
) -> TreeOut:
    tid = assert_tenant(user)
    sid = _uuid(subject_id, "subject_id")
    repo = ChapterRepository(session)

    chapters = await repo.list_chapters(tenant_id=tid, subject_id=sid, grade=grade)
    chapter_out: list[ChapterOut] = []
    for ch in chapters:
        topics = await repo.topics_in_chapter(ch.id)
        chapter_out.append(ChapterOut(
            id=str(ch.id), subject_id=str(ch.subject_id), grade=ch.grade,
            name=ch.name, order_num=ch.order_num, source=ch.source,
            topics=[_topic_out(t) for t in topics],
        ))

    # Topics for this subject not linked to any chapter (e.g. CSV-ingested).
    unassigned = await session.execute(
        select(Topic).where(Topic.subject_id == sid, Topic.chapter_id.is_(None))
        .order_by(Topic.name)
    )
    return TreeOut(
        subject_id=subject_id, grade=grade, chapters=chapter_out,
        unassigned_topics=[_topic_out(t) for t in unassigned.scalars().all()],
    )


# ── chapters ─────────────────────────────────────────────


@router.post("/chapters", response_model=ChapterOut, status_code=201)
async def create_chapter(
    body: ChapterIn, user: SchoolAdminDep, session: SessionDep
) -> ChapterOut:
    tid = assert_tenant(user)
    sid = _uuid(body.subject_id, "subject_id")
    if not body.name.strip():
        raise HTTPException(400, "chapter name is required")
    repo = ChapterRepository(session)
    try:
        ch = await repo.create_chapter(
            tenant_id=tid, subject_id=sid, grade=body.grade,
            name=body.name, order_num=body.order_num,
        )
    except IntegrityError:
        raise HTTPException(409, "a chapter with that name already exists")
    return ChapterOut(
        id=str(ch.id), subject_id=str(ch.subject_id), grade=ch.grade,
        name=ch.name, order_num=ch.order_num, source=ch.source, topics=[],
    )


@router.patch("/chapters/{chapter_id}", response_model=ChapterOut)
async def update_chapter(
    chapter_id: str, body: ChapterPatch, user: SchoolAdminDep, session: SessionDep
) -> ChapterOut:
    tid = assert_tenant(user)
    repo = ChapterRepository(session)
    ch = await repo.get_chapter(_uuid(chapter_id, "chapter_id"), tid)
    if ch is None:
        raise HTTPException(404, "chapter not found")
    await repo.update_chapter(ch, name=body.name, order_num=body.order_num)
    topics = await repo.topics_in_chapter(ch.id)
    return ChapterOut(
        id=str(ch.id), subject_id=str(ch.subject_id), grade=ch.grade,
        name=ch.name, order_num=ch.order_num, source=ch.source,
        topics=[_topic_out(t) for t in topics],
    )


@router.delete("/chapters/{chapter_id}", status_code=204)
async def delete_chapter(
    chapter_id: str, user: SchoolAdminDep, session: SessionDep
) -> None:
    tid = assert_tenant(user)
    repo = ChapterRepository(session)
    ch = await repo.get_chapter(_uuid(chapter_id, "chapter_id"), tid)
    if ch is None:
        raise HTTPException(404, "chapter not found")
    await repo.delete_chapter(ch)


# ── topics ───────────────────────────────────────────────


@router.post("/topics", response_model=TopicOut, status_code=201)
async def create_topic(
    body: TopicIn, user: SchoolAdminDep, session: SessionDep
) -> TopicOut:
    tid = assert_tenant(user)
    repo = ChapterRepository(session)
    ch = await repo.get_chapter(_uuid(body.chapter_id, "chapter_id"), tid)
    if ch is None:
        raise HTTPException(404, "chapter not found")
    if not body.name.strip():
        raise HTTPException(400, "topic name is required")
    topic = await repo.upsert_topic_in_chapter(
        subject_id=ch.subject_id, chapter_id=ch.id, name=body.name,
    )
    return _topic_out(topic)


@router.patch("/topics/{topic_id}", response_model=TopicOut)
async def update_topic(
    topic_id: str, body: TopicPatch, user: SchoolAdminDep, session: SessionDep
) -> TopicOut:
    tid = assert_tenant(user)
    repo = ChapterRepository(session)
    topic = await repo.get_topic(_uuid(topic_id, "topic_id"))
    if topic is None:
        raise HTTPException(404, "topic not found")

    if body.chapter_id is not None:
        ch = await repo.get_chapter(_uuid(body.chapter_id, "chapter_id"), tid)
        if ch is None:
            raise HTTPException(404, "chapter not found")
        if ch.subject_id != topic.subject_id:
            raise HTTPException(400, "topic and chapter belong to different subjects")
        await repo.move_topic(topic, ch.id)

    if body.name is not None and body.name.strip():
        topic.name = body.name.strip()
        try:
            await session.flush()
        except IntegrityError:
            raise HTTPException(409, "a topic with that name already exists")

    return _topic_out(topic)


@router.delete("/topics/{topic_id}", status_code=204)
async def unlink_topic(
    topic_id: str, user: SchoolAdminDep, session: SessionDep
) -> None:
    """Detach a topic from its chapter (keeps the topic row — it may be
    referenced by existing questions)."""
    assert_tenant(user)
    repo = ChapterRepository(session)
    topic = await repo.get_topic(_uuid(topic_id, "topic_id"))
    if topic is None:
        raise HTTPException(404, "topic not found")
    await repo.move_topic(topic, None)


# ── textbooks + AI curriculum proposals ──────────────────


class ProposalOut(BaseModel):
    id: str
    subject_id: str
    grade: int
    title: str
    status: str            # extracting | ready | applied | failed
    outline: dict | None   # {chapters: [{name, order, topics: [...]}]}
    error: str | None


class ApplyChapterIn(BaseModel):
    name: str
    order: int = 0
    topics: list[str] = []


class ApplyOutlineIn(BaseModel):
    chapters: list[ApplyChapterIn]


def _proposal_out(p: CurriculumProposal) -> ProposalOut:
    return ProposalOut(
        id=str(p.id), subject_id=str(p.subject_id), grade=p.grade,
        title=p.title, status=p.status, outline=p.outline_json, error=p.error,
    )


class IngestionOut(BaseModel):
    id: str
    subject_id: str
    subject_name: str
    grade: int
    source_filename: str
    status: str
    progress: int
    error: str | None
    proposal_id: str | None
    file_size: int | None
    created_at: str
    updated_at: str


def _ingestion_out(row: TextbookIngestion, subject_name: str) -> IngestionOut:
    return IngestionOut(
        id=str(row.id),
        subject_id=str(row.subject_id),
        subject_name=subject_name,
        grade=row.grade,
        source_filename=row.source_filename,
        status=row.status.value if isinstance(row.status, IngestStatus) else str(row.status),
        progress=int(row.progress or 0),
        error=row.error,
        proposal_id=str(row.proposal_id) if row.proposal_id else None,
        file_size=row.file_size,
        created_at=row.created_at.isoformat() if row.created_at else "",
        updated_at=row.updated_at.isoformat() if row.updated_at else "",
    )


@router.post("/textbooks", response_model=IngestionOut, status_code=202)
async def upload_textbook(
    user: SchoolAdminDep,
    session: SessionDep,
    file: UploadFile = File(...),
    subject_id: str = Form(...),
    grade: int = Form(...),
) -> IngestionOut:
    """Upload a raw textbook PDF for the ingest worker to process.

    Flow:
      1. Saves the PDF to the shared `textbook_pdfs` volume under
         `<sha256>.pdf` (dedup key) and records a TextbookIngestion row
         with status=queued.
      2. Enqueues `ingest_textbook_job` — picked up by the dedicated
         ingest-worker which runs docling + chunks + embeds + creates
         the CurriculumProposal.
      3. Admin polls `GET /textbooks/ingestions/{id}` (or the list) to
         watch progress. Once status flips to `ready`, the proposal is
         visible under `/proposals/` for the apply step.

    Re-uploading the same bytes (same sha256) for the same tenant is a
    no-op — returns the existing ingestion row.
    """
    tid = assert_tenant(user)
    sid = _uuid(subject_id, "subject_id")
    if grade <= 0 or grade > 12:
        raise HTTPException(400, "grade must be between 1 and 12")

    subject = await session.get(Subject, sid)
    if subject is None:
        raise HTTPException(404, "subject not found")

    raw = await file.read()
    if not raw:
        raise HTTPException(400, "empty PDF")
    if len(raw) > _PDF_MAX_BYTES:
        raise HTTPException(413, f"PDF too large ({_PDF_MAX_BYTES // (1024*1024)} MB cap)")
    if not raw.startswith(b"%PDF"):
        raise HTTPException(415, "file is not a PDF")

    sha256 = hashlib.sha256(raw).hexdigest()

    # Idempotency: same tenant + same bytes → reuse existing row.
    existing = (await session.execute(
        select(TextbookIngestion).where(
            TextbookIngestion.tenant_id == tid,
            TextbookIngestion.file_sha256 == sha256,
        )
    )).scalar_one_or_none()
    if existing is not None:
        return _ingestion_out(existing, subject.name)

    storage = get_textbook_storage_dir()
    on_disk = storage / f"{sha256}.pdf"
    if not on_disk.exists():
        on_disk.write_bytes(raw)

    source_filename = (file.filename or f"{subject.name}-grade-{grade}.pdf")[:300]
    ingestion = TextbookIngestion(
        id=uuid.uuid4(),
        tenant_id=tid,
        subject_id=subject.id,
        grade=grade,
        source_filename=source_filename,
        file_sha256=sha256,
        file_path=str(on_disk),
        file_size=len(raw),
        status=IngestStatus.queued,
        progress=0,
        uploaded_by=user.id,
    )
    session.add(ingestion)
    await session.flush()

    job_id = await enqueue("ingest_textbook_job", ingestion_id=str(ingestion.id))
    if job_id is None:
        # Queue down — keep the row so the admin can retry later via
        # POST /ingestions/{id}/retry rather than re-uploading the PDF.
        raise HTTPException(503, "ingest queue unavailable, please retry")

    logger.info("textbook.uploaded",
                ingestion_id=str(ingestion.id),
                subject=subject.name, grade=grade, bytes=len(raw))
    return _ingestion_out(ingestion, subject.name)


@router.get("/textbooks/ingestions", response_model=list[IngestionOut])
async def list_ingestions(
    user: SchoolAdminDep, session: SessionDep,
) -> list[IngestionOut]:
    """All textbook PDFs uploaded by this school — newest first.

    Drives the admin status board: one card per upload showing progress
    and the resulting proposal link once ready.
    """
    tid = assert_tenant(user)
    rows = await session.execute(
        select(TextbookIngestion, Subject.name)
        .join(Subject, Subject.id == TextbookIngestion.subject_id)
        .where(TextbookIngestion.tenant_id == tid)
        .order_by(TextbookIngestion.created_at.desc())
    )
    return [_ingestion_out(ing, name) for ing, name in rows.all()]


@router.get("/textbooks/ingestions/{ingestion_id}", response_model=IngestionOut)
async def get_ingestion(
    ingestion_id: str, user: SchoolAdminDep, session: SessionDep,
) -> IngestionOut:
    tid = assert_tenant(user)
    row = await session.get(TextbookIngestion, _uuid(ingestion_id, "ingestion_id"))
    if row is None or row.tenant_id != tid:
        raise HTTPException(404, "ingestion not found")
    subject = await session.get(Subject, row.subject_id)
    return _ingestion_out(row, subject.name if subject else "—")


@router.post("/textbooks/ingestions/{ingestion_id}/retry", response_model=IngestionOut)
async def retry_ingestion(
    ingestion_id: str, user: SchoolAdminDep, session: SessionDep,
) -> IngestionOut:
    """Re-enqueue an ingestion without re-uploading the PDF.

    Allowed when the row is `failed`, `cancelled`, or `queued`. Queued
    is included so admins can unblock rows that were stuck before the
    queue-routing fix landed (older deploys could leave jobs on the
    main worker's queue, which silently dropped them as unknown
    functions). Re-running on a `queued` row is safe — the worker's
    in-job guard exits cleanly when it sees a row already in flight.
    """
    tid = assert_tenant(user)
    row = await session.get(TextbookIngestion, _uuid(ingestion_id, "ingestion_id"))
    if row is None or row.tenant_id != tid:
        raise HTTPException(404, "ingestion not found")
    if row.status not in (
        IngestStatus.failed, IngestStatus.cancelled, IngestStatus.queued,
    ):
        raise HTTPException(
            409,
            f"can only retry failed/cancelled/queued (got {row.status.value})",
        )
    row.status = IngestStatus.queued
    row.progress = 0
    row.error = None
    await session.flush()
    job_id = await enqueue("ingest_textbook_job", ingestion_id=str(row.id))
    if job_id is None:
        raise HTTPException(503, "ingest queue unavailable, please retry")
    subject = await session.get(Subject, row.subject_id)
    return _ingestion_out(row, subject.name if subject else "—")


@router.post("/textbooks/ingestions/{ingestion_id}/cancel", response_model=IngestionOut)
async def cancel_ingestion(
    ingestion_id: str, user: SchoolAdminDep, session: SessionDep,
) -> IngestionOut:
    """Soft-cancel an in-flight ingestion.

    Flips the row to `cancelled`. The worker re-reads the row between
    its long stages (docling extraction, chunking, embedding) and exits
    cleanly when it sees the flip. Already-finished rows (ready /
    failed) just transition to cancelled with no side effects — use
    Delete if you also want to remove the PDF + Qdrant chunks.
    """
    tid = assert_tenant(user)
    row = await session.get(TextbookIngestion, _uuid(ingestion_id, "ingestion_id"))
    if row is None or row.tenant_id != tid:
        raise HTTPException(404, "ingestion not found")
    if row.status == IngestStatus.cancelled:
        subject = await session.get(Subject, row.subject_id)
        return _ingestion_out(row, subject.name if subject else "—")
    row.status = IngestStatus.cancelled
    row.error = None
    await session.flush()
    logger.info("ingestion.cancelled", ingestion_id=str(row.id))
    subject = await session.get(Subject, row.subject_id)
    return _ingestion_out(row, subject.name if subject else "—")


@router.delete("/textbooks/ingestions/{ingestion_id}", status_code=204)
async def delete_ingestion(
    ingestion_id: str, user: SchoolAdminDep, session: SessionDep,
) -> None:
    """Drop an ingestion row, its PDF on disk, and its Qdrant chunks.

    Hard delete — works at any status, even while the worker is still
    processing the PDF. The worker's between-stage cancel check
    detects the missing row and exits cleanly without overwriting any
    state. Does NOT delete an already-applied proposal's chapters /
    topics: those may be referenced by existing questions. Use the
    curriculum tree endpoints to clean those up if needed.
    """
    tid = assert_tenant(user)
    row = await session.get(TextbookIngestion, _uuid(ingestion_id, "ingestion_id"))
    if row is None or row.tenant_id != tid:
        raise HTTPException(404, "ingestion not found")

    try:
        await get_qdrant_store().delete_textbook(
            tenant_id=tid, source=row.source_filename,
        )
    except Exception as exc:  # noqa: BLE001 — qdrant might be down
        logger.warning("ingestion.qdrant_delete_failed",
                       ingestion_id=str(row.id), error=str(exc))

    from pathlib import Path as _Path
    try:
        p = _Path(row.file_path)
        if p.is_file():
            p.unlink()
    except OSError as exc:
        logger.warning("ingestion.file_unlink_failed",
                       ingestion_id=str(row.id), error=str(exc))

    await session.delete(row)
    await session.flush()


@router.get("/proposals", response_model=list[ProposalOut])
async def list_proposals(
    user: SchoolAdminDep, session: SessionDep
) -> list[ProposalOut]:
    tid = assert_tenant(user)
    rows = await session.execute(
        select(CurriculumProposal)
        .where(CurriculumProposal.tenant_id == tid)
        .order_by(CurriculumProposal.created_at.desc())
    )
    return [_proposal_out(p) for p in rows.scalars().all()]


@router.get("/proposals/{proposal_id}", response_model=ProposalOut)
async def get_proposal(
    proposal_id: str, user: SchoolAdminDep, session: SessionDep
) -> ProposalOut:
    tid = assert_tenant(user)
    p = await session.get(CurriculumProposal, _uuid(proposal_id, "proposal_id"))
    if p is None or p.tenant_id != tid:
        raise HTTPException(404, "proposal not found")
    return _proposal_out(p)


@router.post("/proposals/{proposal_id}/apply", response_model=TreeOut)
async def apply_proposal(
    proposal_id: str,
    body: ApplyOutlineIn,
    user: SchoolAdminDep,
    session: SessionDep,
) -> TreeOut:
    """Commit a reviewed outline into real Chapter + Topic rows.

    The body is the admin-edited outline (not necessarily the AI's
    original). Chapters/topics that already exist are reused, so applying
    twice is safe.
    """
    tid = assert_tenant(user)
    p = await session.get(CurriculumProposal, _uuid(proposal_id, "proposal_id"))
    if p is None or p.tenant_id != tid:
        raise HTTPException(404, "proposal not found")

    repo = ChapterRepository(session)
    # Case-insensitive dedup so "Lines and Angles" and "Lines & Angles"
    # don't both materialise when the admin tweaks an existing name.
    existing_by_key: dict[str, Any] = {
        _chapter_key(c.name): c for c in await repo.list_chapters(
            tenant_id=tid, subject_id=p.subject_id, grade=p.grade
        )
    }
    # chapter_order → chapter_id  AND  (chapter_order, topic_name) → topic_id
    # built up here so Qdrant chunks can be back-tagged once the curriculum
    # tree is materialised below.
    chapter_id_by_order: dict[int, uuid.UUID] = {}
    topic_id_by_order_name: dict[tuple[int, str], uuid.UUID] = {}
    for ch_in in body.chapters:
        name = ch_in.name.strip()
        if not name:
            continue
        key = _chapter_key(name)
        chapter = existing_by_key.get(key)
        if chapter is None:
            chapter = await repo.create_chapter(
                tenant_id=tid, subject_id=p.subject_id, grade=p.grade,
                name=name, order_num=ch_in.order, source="ai_extracted",
            )
            existing_by_key[key] = chapter
        if ch_in.order:
            chapter_id_by_order[int(ch_in.order)] = chapter.id
        for topic_name in ch_in.topics:
            tname = topic_name.strip()
            if not tname:
                continue
            topic = await repo.upsert_topic_in_chapter(
                subject_id=p.subject_id, chapter_id=chapter.id,
                name=tname,
            )
            if ch_in.order:
                topic_id_by_order_name[(int(ch_in.order), tname)] = topic.id

    p.status = "applied"
    await session.flush()

    # Backfill chapter_id / topic_id onto the textbook's Qdrant chunks so
    # paper-gen retrieval can hard-filter by chapter and topic. The source
    # (PDF filename) was stashed in outline_json at import time.
    source = (p.outline_json or {}).get("source") if p.outline_json else None
    if source and (chapter_id_by_order or topic_id_by_order_name):
        try:
            await get_qdrant_store().tag_chunks_with_curriculum(
                tenant_id=tid, source=source,
                chapter_id_by_order=chapter_id_by_order,
                topic_id_by_order_name=topic_id_by_order_name,
            )
        except Exception as exc:  # noqa: BLE001 — tagging is best-effort
            logger.warning("proposal.tag_chunks_failed",
                           proposal_id=str(p.id), source=source,
                           error=str(exc))

    return await get_tree(user, session, str(p.subject_id), p.grade)


class WriteCurriculumIn(BaseModel):
    subject_id: str
    grade: int
    chapters: list[ApplyChapterIn]


@router.post("/write", response_model=TreeOut)
async def write_curriculum(
    body: WriteCurriculumIn,
    user: SchoolAdminDep,
    session: SessionDep,
) -> TreeOut:
    """Write the full curriculum for a (subject, grade) in one call.

    All existing chapters for this tenant / subject / grade are deleted
    first, then the submitted list is created fresh.  Topics that were
    linked to deleted chapters become unassigned but are NOT deleted —
    they may be referenced by existing questions.
    """
    tid = assert_tenant(user)
    sid = _uuid(body.subject_id, "subject_id")
    repo = ChapterRepository(session)

    for ch in await repo.list_chapters(tenant_id=tid, subject_id=sid, grade=body.grade):
        await repo.delete_chapter(ch)

    for i, ch_in in enumerate(body.chapters):
        name = ch_in.name.strip()
        if not name:
            continue
        chapter = await repo.create_chapter(
            tenant_id=tid, subject_id=sid, grade=body.grade,
            name=name, order_num=i + 1, source="manual",
        )
        for topic_name in ch_in.topics:
            if topic_name.strip():
                await repo.upsert_topic_in_chapter(
                    subject_id=sid, chapter_id=chapter.id, name=topic_name,
                )

    await session.flush()
    return await get_tree(user, session, str(sid), body.grade)


def _chapter_key(name: str) -> str:
    """Normalise a chapter name for dedup: lowercase, collapse whitespace,
    strip non-alphanumerics so '&' and 'and' collapse, etc."""
    import re as _re
    s = name.lower().replace("&", "and")
    s = _re.sub(r"[^a-z0-9À-￿]+", "", s)
    return s


# ── delete proposal ─────────────────────────────────────
# (no retry endpoint — pack import is synchronous, retry == re-import)


@router.delete("/proposals/{proposal_id}", status_code=204)
async def delete_proposal(
    proposal_id: str, user: SchoolAdminDep, session: SessionDep
) -> None:
    """Discard a proposal. Does NOT delete the textbook chunks already
    upserted to Qdrant — use DELETE /textbook for that."""
    tid = assert_tenant(user)
    p = await session.get(CurriculumProposal, _uuid(proposal_id, "proposal_id"))
    if p is None or p.tenant_id != tid:
        raise HTTPException(404, "proposal not found")
    await session.delete(p)
    await session.flush()


# ── textbook inventory (Qdrant-backed) ────────────────────


class TextbookInfo(BaseModel):
    source: str
    title: str | None
    subject: str | None
    grade: int | None
    chunks: int
    embedded_at: str | None


class TopicCoverage(BaseModel):
    id: str
    name: str
    content_chunks: int
    exercise_chunks: int


class ChapterCoverage(BaseModel):
    id: str
    name: str
    order_num: int
    content_chunks: int
    exercise_chunks: int
    untagged_chunks: int
    topics: list[TopicCoverage]


class CoverageOut(BaseModel):
    subject_id: str
    grade: int
    chapters: list[ChapterCoverage]
    untagged_chunks: int   # chunks in Qdrant with no chapter_id yet
    total_chunks: int


@router.get("/coverage", response_model=CoverageOut)
async def get_coverage(
    user: SchoolAdminDep,
    session: SessionDep,
    subject_id: str,
    grade: int,
) -> CoverageOut:
    """Per-chapter / per-topic chunk counts for the admin coverage view.

    Cross-references the Postgres curriculum tree with the Qdrant chunk
    payloads tagged by `apply_proposal`. A zero-count chapter means the
    pack build picked up a chapter from the ToC but no body text landed
    inside it — usually a sign of failed OCR for that chapter's pages.
    """
    tid = assert_tenant(user)
    sid = _uuid(subject_id, "subject_id")

    # Subject name is needed to scope the Qdrant query
    subject = await session.get(Subject, sid)
    if subject is None:
        raise HTTPException(404, "subject not found")

    repo = ChapterRepository(session)
    chapters = await repo.list_chapters(tenant_id=tid, subject_id=sid, grade=grade)
    qstore = get_qdrant_store()
    cov = await qstore.coverage_for(
        tenant_id=tid, subject=subject.name, grade=grade,
    )

    by_chapter = cov["by_chapter"]
    by_topic = cov["by_topic"]
    chapter_rows: list[ChapterCoverage] = []
    for ch in chapters:
        cbucket = by_chapter.get(str(ch.id), {})
        topics = await repo.topics_in_chapter(ch.id)
        topic_rows: list[TopicCoverage] = []
        for t in topics:
            tb = by_topic.get(str(t.id), {})
            topic_rows.append(TopicCoverage(
                id=str(t.id), name=t.name,
                content_chunks=int(tb.get("content", 0)),
                exercise_chunks=int(tb.get("exercise", 0)),
            ))
        chapter_rows.append(ChapterCoverage(
            id=str(ch.id), name=ch.name, order_num=ch.order_num,
            content_chunks=int(cbucket.get("content", 0)),
            exercise_chunks=int(cbucket.get("exercise", 0)),
            untagged_chunks=int(cbucket.get("untagged", 0)),
            topics=topic_rows,
        ))

    return CoverageOut(
        subject_id=subject_id, grade=grade,
        chapters=chapter_rows,
        untagged_chunks=int(cov.get("untagged_chunks", 0)),
        total_chunks=int(cov.get("total", 0)),
    )


@router.get("/textbooks", response_model=list[TextbookInfo])
async def list_textbooks(
    user: SchoolAdminDep, _session: SessionDep
) -> list[TextbookInfo]:
    """List every textbook this tenant has indexed in Qdrant."""
    tid = assert_tenant(user)
    rows = await get_qdrant_store().list_textbooks_for_tenant(tenant_id=tid)
    return [TextbookInfo(**r) for r in rows]


@router.delete("/textbooks", status_code=204)
async def delete_textbook(
    user: SchoolAdminDep, _session: SessionDep, source: str
) -> None:
    """Remove every Qdrant chunk for a (tenant, source) pair so a wrong-
    subject / wrong-grade upload can be undone."""
    tid = assert_tenant(user)
    if not source.strip():
        raise HTTPException(400, "source is required")
    await get_qdrant_store().delete_textbook(tenant_id=tid, source=source)
    logger.info("textbook.deleted", tenant=str(tid), source=source)
