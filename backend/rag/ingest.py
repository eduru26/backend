from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.logging import get_logger
from backend.db.models import (
    Announcement,
    AnnouncementAudience,
    AssignmentQuestion,
    Assignment,
    RagDocument,
    RagDocumentChunk,
)
from backend.rag.chunker import chunk_text
from backend.rag.embeddings import get_embedder

logger = get_logger(__name__)


@dataclass
class IngestResult:
    document_id: str
    chunks: int
    deduplicated: bool


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


# Default visibility per source kind. Callers can override per-document.
_DEFAULT_VIS: dict[str, list[str]] = {
    "curriculum":   ["public"],
    "policy":       ["public"],
    "announcement": ["public"],  # narrowed below per audience
    "assignment":   ["teacher", "student"],
    "note":         ["public"],
    "upload":       ["public"],
}


class RagIngestService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def ingest_text(
        self,
        *,
        tenant_id: uuid.UUID,
        kind: str,
        title: str,
        text: str,
        uploaded_by: uuid.UUID | None,
        role_visibility: list[str] | None = None,
        student_id: uuid.UUID | None = None,
        classroom_id: uuid.UUID | None = None,
        subject: str | None = None,
        topic: str | None = None,
        source_uri: str | None = None,
        metadata: dict | None = None,
    ) -> IngestResult:
        text = (text or "").strip()
        if not text:
            raise ValueError("text is empty")

        # idempotency: same tenant + same content = same document
        digest = _sha256(text)
        existing = await self._session.scalar(
            select(RagDocument).where(
                RagDocument.tenant_id == tenant_id,
                RagDocument.sha256 == digest,
            )
        )
        if existing is not None:
            logger.info(
                "rag.ingest.dedup",
                document_id=str(existing.id), kind=kind, title=title,
            )
            return IngestResult(str(existing.id), existing.chunk_count, True)

        vis = role_visibility or _DEFAULT_VIS.get(kind, ["public"])

        doc = RagDocument(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            kind=kind,
            title=title[:300],
            source_uri=source_uri[:500] if source_uri else None,
            sha256=digest,
            uploaded_by=uploaded_by,
            role_visibility=vis,
            student_id=student_id,
            classroom_id=classroom_id,
            subject=subject,
            topic=topic,
            document_metadata=metadata or {},
            chunk_count=0,
        )
        self._session.add(doc)
        await self._session.flush()

        chunks = chunk_text(text)
        if not chunks:
            return IngestResult(str(doc.id), 0, False)

        embedder = get_embedder()
        vectors = await embedder.embed_batch(chunks)

        for i, (c_text, vec) in enumerate(zip(chunks, vectors)):
            self._session.add(RagDocumentChunk(
                id=uuid.uuid4(),
                document_id=doc.id,
                tenant_id=tenant_id,
                chunk_index=i,
                content=c_text,
                embedding=vec,
                role_visibility=vis,
                student_id=student_id,
                classroom_id=classroom_id,
                subject=subject,
                topic=topic,
                chunk_metadata=metadata or {},
            ))

        doc.chunk_count = len(chunks)
        await self._session.flush()

        logger.info(
            "rag.ingest.indexed",
            document_id=str(doc.id), kind=kind, chunks=len(chunks),
        )
        return IngestResult(str(doc.id), len(chunks), False)

    # ── reindex existing DB content as RAG ─────────────────

    async def reindex_announcements(
        self, *, tenant_id: uuid.UUID, limit: int = 200
    ) -> int:
        """Index recent announcements as RAG documents.

        Visibility is derived from the announcement's audience: e.g. a
        teacher-targeted announcement should not be retrievable by a
        student. classroom_id is preserved so per-class announcements
        only surface for that class.
        """
        rows = await self._session.execute(
            select(Announcement)
            .where(Announcement.tenant_id == tenant_id)
            .order_by(Announcement.created_at.desc())
            .limit(limit)
        )
        count = 0
        for ann in rows.scalars().all():
            vis = _vis_for_audience(ann.audience)
            body = f"{ann.title}\n\n{ann.body}"
            try:
                res = await self.ingest_text(
                    tenant_id=tenant_id,
                    kind="announcement",
                    title=ann.title,
                    text=body,
                    uploaded_by=ann.author_id,
                    role_visibility=vis,
                    classroom_id=ann.classroom_id,
                    source_uri=f"announcement://{ann.id}",
                    metadata={"audience": ann.audience.value, "pinned": ann.pinned},
                )
                if not res.deduplicated:
                    count += 1
            except Exception as exc:  # noqa: BLE001 — keep going on per-row failures
                logger.warning("rag.reindex.announcement.failed",
                               announcement_id=str(ann.id), error=str(exc))
        return count

    async def reindex_assignments(
        self, *, tenant_id: uuid.UUID, limit: int = 200
    ) -> int:
        """Index assignment titles + question texts as RAG documents.

        Correct answers are NEVER indexed — that would let students
        bypass the assignment by asking the mentor. Only question stems
        and option text are included, since those are visible in the
        student UI anyway.
        """
        rows = await self._session.execute(
            select(Assignment)
            .where(Assignment.tenant_id == tenant_id)
            .order_by(Assignment.created_at.desc())
            .limit(limit)
        )
        count = 0
        for asg in rows.scalars().all():
            qs = await self._session.execute(
                select(AssignmentQuestion)
                .where(AssignmentQuestion.assignment_id == asg.id)
                .order_by(AssignmentQuestion.order_num)
            )
            q_rows = qs.scalars().all()
            if not q_rows:
                continue
            lines = [f"Assignment: {asg.title or asg.subject}"]
            for q in q_rows:
                lines.append(f"Q{q.order_num}. {q.question_text}")
                # options included (visible in UI anyway), correct answer omitted
                lines.append(f"  A) {q.option_a}")
                lines.append(f"  B) {q.option_b}")
                lines.append(f"  C) {q.option_c}")
                lines.append(f"  D) {q.option_d}")
            body = "\n".join(lines)
            try:
                res = await self.ingest_text(
                    tenant_id=tenant_id,
                    kind="assignment",
                    title=asg.title or f"{asg.subject} assignment",
                    text=body,
                    uploaded_by=asg.teacher_id,
                    role_visibility=["teacher", "student"],
                    classroom_id=asg.classroom_id,
                    subject=asg.subject,
                    source_uri=f"assignment://{asg.id}",
                    metadata={"status": asg.status.value},
                )
                if not res.deduplicated:
                    count += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("rag.reindex.assignment.failed",
                               assignment_id=str(asg.id), error=str(exc))
        return count


def _vis_for_audience(audience: AnnouncementAudience) -> list[str]:
    if audience is AnnouncementAudience.teachers:
        return ["teacher"]
    if audience is AnnouncementAudience.parents:
        return ["parent"]
    if audience is AnnouncementAudience.students:
        return ["student"]
    if audience is AnnouncementAudience.classroom:
        return ["student", "parent", "teacher"]
    # school-wide
    return ["public"]
