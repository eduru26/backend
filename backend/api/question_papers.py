"""Teacher question-paper endpoints — generate, edit, finalize.

Generation is asynchronous: POST /generate creates an Assessment with
status='generating' and enqueues `generate_paper_job`; the client polls
GET /{id}/status until it flips to 'draft'. Marks entry, Excel export and
report cards are added by the marks router phase.
"""
from __future__ import annotations

import io
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from backend.api.deps import SessionDep, assert_tenant, require_roles
from backend.api.teacher_mgmt import _teacher_classroom_ids
from backend.core.logging import get_logger
from backend.db.models import (
    Assessment, AssessmentType, Classroom, Question, QuestionDifficulty,
    QuestionPaperTemplate, QuestionResponse, ReportCard, Student, Subject,
    TeacherSubjectAssignment, Tenant, Topic, User, UserRole,
)
from backend.db.repositories.curriculum import ChapterRepository
from backend.rag.qdrant_store import get_qdrant_store
from backend.services.paper_generator import _allocate
from backend.worker.queue import enqueue

# Subjects where AI question generation is skipped — language papers
# are authored manually by the teacher using the template as a form.
# Drama/prose passages and translation prompts don't ground well in
# retrieved chunks, and language teachers prefer to write their own.
_MANUAL_SUBJECTS = frozenset({"English", "Hindi", "Kannada"})

logger = get_logger(__name__)

router = APIRouter(prefix="/teacher/papers", tags=["question-papers"])
TeacherDep = Annotated[User, Depends(require_roles(UserRole.teacher))]


# ── Schemas ──────────────────────────────────────────────


class SubjectOption(BaseModel):
    id: str
    name: str


class TemplateOption(BaseModel):
    id: str
    name: str
    exam_type: str
    total_marks: float
    sections: list


class OptionsOut(BaseModel):
    classroom_id: str
    classroom_name: str
    grade: int
    subjects: list[SubjectOption]   # 1 => UI skips the subject step
    templates: list[TemplateOption]


class TopicLite(BaseModel):
    id: str
    name: str


class ChapterLite(BaseModel):
    id: str
    name: str
    topics: list[TopicLite]


class GenerateIn(BaseModel):
    classroom_id: str
    subject_id: str
    exam_type: str                  # 'unit_test' | 'semester_exam'
    template_id: str
    chapter_ids: list[str] = []
    topic_ids: list[str]
    # semester only: {"chapters": {id: pct}, "topics": {id: pct}}
    weightage: dict | None = None


class GenerateOut(BaseModel):
    assessment_id: str
    status: str


class StatusOut(BaseModel):
    assessment_id: str
    status: str                     # generating | draft | finalized
    error: str | None = None
    warnings: list[str] = []
    question_count: int = 0


class QuestionOut(BaseModel):
    id: str
    question_no: int
    section_no: int | None
    topic_id: str
    topic_name: str
    question_text: str | None
    max_marks: float
    difficulty: str
    is_optional: bool
    # Grounding metadata: chapter context for the "📘 Ch X" badge, the
    # validator's similarity score, and whether source citations exist
    # (the UI uses this to decide whether to show the drawer link).
    chapter_name: str | None = None
    grounding_score: float | None = None
    has_sources: bool = False


class PaperOut(BaseModel):
    assessment_id: str
    title: str
    exam_type: str
    status: str
    subject: str
    classroom_name: str
    total_marks: float
    template_sections: list
    questions: list[QuestionOut]
    # Auto-fetched from the tenant — shown at the top of the paper view
    # and in the print header. Frontend doesn't ask the admin or teacher
    # to enter it anywhere.
    school_name: str | None = None
    # Allotted time in minutes, copied from the template. Optional —
    # older templates don't carry it; the UI shows "—" or hides the
    # banner in that case.
    duration_minutes: int | None = None
    # "ai" → went through the grounded paper generator; "manual" →
    # language subject, teacher types every question into a blank
    # template. The frontend hides AI-only controls (regenerate, re-roll,
    # source drawer) when mode is manual.
    mode: str = "ai"
    # True when mode is manual *because the AI couldn't ground any
    # questions* — surface a different banner so the teacher knows the
    # fallback path was taken (vs. a language subject which is manual
    # by design).
    ai_fallback: bool = False
    # Per-topic warnings collected by paper_generator (e.g. "Section A /
    # Bar Graphs: LLM call failed"). Surfaced so the teacher knows which
    # parts of the paper need attention rather than seeing a silent shortfall.
    warnings: list[str] = []
    # Names of teacher-selected topics that produced zero questions because
    # they had no textbook coverage. Strict policy: never silently fall back
    # to general knowledge — the UI banner asks the admin to upload more
    # material or drop the topic.
    topics_no_context: list[str] = []
    # True iff `topics_no_context` is non-empty. Kept for backwards-compat
    # with existing UI consumers; new UI should read `topics_no_context`.
    has_ungrounded: bool = False


class PaperSummary(BaseModel):
    assessment_id: str
    title: str
    status: str
    subject: str
    classroom_name: str
    total_marks: float
    created_at: str


class QuestionEdit(BaseModel):
    question_id: str
    question_text: str | None = None
    max_marks: float | None = None


class PaperPatch(BaseModel):
    title: str | None = None
    questions: list[QuestionEdit] = []


# ── helpers ──────────────────────────────────────────────


def _uuid(value: str, label: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        raise HTTPException(400, f"invalid {label}")


async def _assert_classroom(
    session, tenant_id: uuid.UUID, teacher_id: uuid.UUID, classroom_id: uuid.UUID
) -> Classroom:
    allowed = await _teacher_classroom_ids(session, tenant_id, teacher_id)
    if classroom_id not in allowed:
        raise HTTPException(403, "you are not assigned to this classroom")
    cls = await session.get(Classroom, classroom_id)
    if cls is None or cls.tenant_id != tenant_id:
        raise HTTPException(404, "classroom not found")
    return cls


async def _teacher_subjects_in_class(
    session, teacher: User, classroom: Classroom
) -> list[Subject]:
    """Subjects the teacher may write papers for in this class: their
    subject assignments here, plus their specialization if they are the
    class teacher."""
    names: set[str] = set()
    rows = await session.execute(
        select(TeacherSubjectAssignment.subject).where(
            TeacherSubjectAssignment.teacher_id == teacher.id,
            TeacherSubjectAssignment.classroom_id == classroom.id,
        )
    )
    names.update(r for r in rows.scalars().all())
    if classroom.teacher_id == teacher.id and teacher.specialization:
        names.add(teacher.specialization)
    if not names:
        return []
    subs = await session.execute(select(Subject).where(Subject.name.in_(names)))
    return list(subs.scalars().all())


def _paper_question(
    q: Question, topic_name: str, chapter_name: str | None = None,
) -> QuestionOut:
    return QuestionOut(
        id=str(q.id), question_no=q.question_no, section_no=q.section_no,
        topic_id=str(q.topic_id), topic_name=topic_name,
        question_text=q.question_text, max_marks=float(q.max_marks),
        difficulty=q.difficulty.value, is_optional=q.is_optional,
        chapter_name=chapter_name,
        grounding_score=q.grounding_score,
        has_sources=bool(q.source_chunk_ids),
    )


# ── options + curriculum ─────────────────────────────────


@router.get("/options", response_model=OptionsOut)
async def generation_options(
    user: TeacherDep, session: SessionDep, classroom_id: str
) -> OptionsOut:
    tid = assert_tenant(user)
    cls = await _assert_classroom(session, tid, user.id, _uuid(classroom_id, "classroom_id"))
    subjects = await _teacher_subjects_in_class(session, user, cls)
    templates = await session.execute(
        select(QuestionPaperTemplate).where(
            QuestionPaperTemplate.tenant_id == tid,
            QuestionPaperTemplate.is_active.is_(True),
        ).order_by(QuestionPaperTemplate.name)
    )
    return OptionsOut(
        classroom_id=str(cls.id), classroom_name=cls.name, grade=cls.grade,
        subjects=[SubjectOption(id=str(s.id), name=s.name) for s in subjects],
        templates=[
            TemplateOption(
                id=str(t.id), name=t.name, exam_type=t.exam_type,
                total_marks=float(t.total_marks), sections=t.sections or [],
            )
            for t in templates.scalars().all()
        ],
    )


@router.get("/chapters", response_model=list[ChapterLite])
async def list_chapters(
    user: TeacherDep, session: SessionDep, subject_id: str, grade: int
) -> list[ChapterLite]:
    """Curriculum tree for the generation wizard (teacher-readable)."""
    tid = assert_tenant(user)
    repo = ChapterRepository(session)
    chapters = await repo.list_chapters(
        tenant_id=tid, subject_id=_uuid(subject_id, "subject_id"), grade=grade
    )
    out: list[ChapterLite] = []
    for ch in chapters:
        topics = await repo.topics_in_chapter(ch.id)
        out.append(ChapterLite(
            id=str(ch.id), name=ch.name,
            topics=[TopicLite(id=str(t.id), name=t.name) for t in topics],
        ))
    return out


# ── generate ─────────────────────────────────────────────


@router.post("/generate", response_model=GenerateOut, status_code=202)
async def generate_paper(
    body: GenerateIn, user: TeacherDep, session: SessionDep
) -> GenerateOut:
    tid = assert_tenant(user)
    cls = await _assert_classroom(session, tid, user.id, _uuid(body.classroom_id, "classroom_id"))

    subject = await session.get(Subject, _uuid(body.subject_id, "subject_id"))
    if subject is None:
        raise HTTPException(404, "subject not found")
    allowed_subjects = {s.id for s in await _teacher_subjects_in_class(session, user, cls)}
    if subject.id not in allowed_subjects:
        raise HTTPException(403, "you don't teach that subject in this class")

    template = await session.get(QuestionPaperTemplate, _uuid(body.template_id, "template_id"))
    if template is None or template.tenant_id != tid:
        raise HTTPException(404, "template not found")

    if not body.topic_ids:
        raise HTTPException(400, "select at least one topic")
    # validate topics belong to this subject
    topics = await session.execute(
        select(Topic).where(Topic.id.in_([_uuid(t, "topic_id") for t in body.topic_ids]))
    )
    topic_rows = list(topics.scalars().all())
    if any(t.subject_id != subject.id for t in topic_rows):
        raise HTTPException(400, "a selected topic does not belong to the subject")
    if not topic_rows:
        raise HTTPException(400, "no valid topics selected")

    exam_type = body.exam_type if body.exam_type in ("unit_test", "semester_exam") else "unit_test"
    a_type = AssessmentType.unit_test if exam_type == "unit_test" else AssessmentType.exam
    kind_label = "Unit Test" if exam_type == "unit_test" else "Semester Exam"

    # Language subjects bypass the AI generator and land on an empty
    # draft that the teacher fills in by hand. Everything else goes
    # through the worker for AI-grounded generation.
    is_manual = subject.name in _MANUAL_SUBJECTS
    initial_status = "draft" if is_manual else "generating"

    assessment = Assessment(
        id=uuid.uuid4(),
        type=a_type,
        title=f"{subject.name} {kind_label} — {cls.name}",
        subject_id=subject.id,
        assessment_date=date.today(),
        max_marks=Decimal(str(template.total_marks)),
        created_by=user.id,
        classroom_id=cls.id,
        template_id=template.id,
        status=initial_status,
        generation_meta={
            "tenant_id": str(tid),
            "template_id": str(template.id),
            "subject_id": str(subject.id),
            "subject_name": subject.name,
            "grade": cls.grade,
            "classroom_id": str(cls.id),
            "chapter_ids": body.chapter_ids,
            "topic_ids": [str(t.id) for t in topic_rows],
            "exam_type": exam_type,
            "weightage": body.weightage or {},
            "created_by": str(user.id),
            "mode": "manual" if is_manual else "ai",
        },
    )
    session.add(assessment)
    await session.flush()

    if is_manual:
        _seed_manual_questions(
            session, assessment, template,
            topics=topic_rows, weightage=body.weightage or {},
        )
        await session.flush()
        logger.info("paper.manual.created",
                    assessment_id=str(assessment.id),
                    subject=subject.name)
        return GenerateOut(assessment_id=str(assessment.id), status="draft")

    job_id = await enqueue("generate_paper_job", assessment_id=str(assessment.id))
    if job_id is None:
        raise HTTPException(503, "generation queue unavailable, please retry")

    logger.info("paper.generate.queued", assessment_id=str(assessment.id))
    return GenerateOut(assessment_id=str(assessment.id), status="generating")


def _seed_manual_questions(
    session, assessment: Assessment, template: QuestionPaperTemplate,
    *, topics: list[Topic], weightage: dict,
) -> None:
    """Pre-populate blank Question rows for a manual (language) paper.

    Allocates each section's question_count across the teacher-selected
    topics using the same largest-remainder logic as AI generation, so
    the form layout the teacher sees mirrors what the template demands.
    Every row starts with empty `question_text` — the teacher edits via
    the standard PATCH /papers/{id} endpoint.
    """
    if not topics:
        return
    topic_weights_meta = (weightage or {}).get("topics") or {}
    weights = [
        float(topic_weights_meta.get(str(t.id), 1.0)) for t in topics
    ]

    question_no = 1
    for section in template.sections or []:
        section_no = int(section.get("section_no", 1))
        answer_count = int(section.get("question_count", 1))
        optional_of = section.get("optional_of")
        gen_count = int(optional_of) if optional_of else answer_count
        is_optional = optional_of is not None
        marks = Decimal(str(section.get("marks_per_question", 1)))
        alloc = _allocate(gen_count, weights)
        for topic, n in zip(topics, alloc):
            for _ in range(n):
                session.add(Question(
                    id=uuid.uuid4(),
                    assessment_id=assessment.id,
                    question_no=question_no,
                    topic_id=topic.id,
                    max_marks=marks,
                    difficulty=QuestionDifficulty.medium,
                    question_text=None,
                    section_no=section_no,
                    is_optional=is_optional,
                ))
                question_no += 1


# ── status / list / detail ───────────────────────────────


async def _load_owned(session, tenant_id, teacher_id, assessment_id: uuid.UUID) -> Assessment:
    a = await session.get(Assessment, assessment_id)
    if a is None or a.subject_id is None:
        raise HTTPException(404, "paper not found")
    # papers carry a template_id; CSV-ingested assessments don't
    if a.template_id is None or a.created_by != teacher_id:
        raise HTTPException(404, "paper not found")
    return a


@router.get("/{assessment_id}/status", response_model=StatusOut)
async def paper_status(
    assessment_id: str, user: TeacherDep, session: SessionDep
) -> StatusOut:
    tid = assert_tenant(user)
    a = await _load_owned(session, tid, user.id, _uuid(assessment_id, "assessment_id"))
    meta = a.generation_meta or {}
    count = await session.scalar(
        select(Question.id).where(Question.assessment_id == a.id).limit(1)
    )
    n = len((await session.execute(
        select(Question.id).where(Question.assessment_id == a.id)
    )).scalars().all()) if count else 0
    return StatusOut(
        assessment_id=str(a.id), status=a.status,
        error=meta.get("error"), warnings=meta.get("warnings", []),
        question_count=n,
    )


@router.get("", response_model=list[PaperSummary])
async def list_papers(user: TeacherDep, session: SessionDep) -> list[PaperSummary]:
    tid = assert_tenant(user)
    rows = await session.execute(
        select(Assessment, Subject.name, Classroom.name)
        .join(Subject, Subject.id == Assessment.subject_id)
        .outerjoin(Classroom, Classroom.id == Assessment.classroom_id)
        .where(
            Assessment.created_by == user.id,
            Assessment.template_id.isnot(None),
        )
        .order_by(Assessment.created_at.desc())
    )
    out: list[PaperSummary] = []
    for a, subj, cls_name in rows.all():
        out.append(PaperSummary(
            assessment_id=str(a.id), title=a.title, status=a.status,
            subject=subj, classroom_name=cls_name or "—",
            total_marks=float(a.max_marks), created_at=a.created_at.isoformat(),
        ))
    return out


@router.get("/{assessment_id}", response_model=PaperOut)
async def get_paper(
    assessment_id: str, user: TeacherDep, session: SessionDep
) -> PaperOut:
    tid = assert_tenant(user)
    a = await _load_owned(session, tid, user.id, _uuid(assessment_id, "assessment_id"))
    subject = await session.get(Subject, a.subject_id)
    classroom = await session.get(Classroom, a.classroom_id) if a.classroom_id else None
    template = await session.get(QuestionPaperTemplate, a.template_id) if a.template_id else None
    tenant = await session.get(Tenant, tid)

    from backend.db.models import Chapter  # local to avoid circulars

    rows = await session.execute(
        select(Question, Topic.name, Chapter.name)
        .join(Topic, Topic.id == Question.topic_id)
        .outerjoin(Chapter, Chapter.id == Topic.chapter_id)
        .where(Question.assessment_id == a.id)
        .order_by(Question.question_no)
    )
    questions = [
        _paper_question(q, tname, ch_name)
        for q, tname, ch_name in rows.all()
    ]
    meta = a.generation_meta or {}
    warnings = list(meta.get("warnings") or [])
    topics_no_context = list(meta.get("topics_no_context") or [])
    return PaperOut(
        assessment_id=str(a.id), title=a.title, exam_type=meta.get("exam_type", ""),
        status=a.status, subject=subject.name if subject else "—",
        classroom_name=classroom.name if classroom else "—",
        total_marks=float(a.max_marks),
        template_sections=template.sections if template else [],
        questions=questions,
        warnings=warnings,
        topics_no_context=topics_no_context,
        has_ungrounded=bool(topics_no_context),
        mode=str(meta.get("mode") or "ai"),
        ai_fallback=bool(meta.get("ai_fallback")),
        school_name=tenant.name if tenant else None,
        duration_minutes=template.duration_minutes if template else None,
    )


@router.patch("/{assessment_id}", response_model=PaperOut)
async def edit_paper(
    assessment_id: str, body: PaperPatch, user: TeacherDep, session: SessionDep
) -> PaperOut:
    tid = assert_tenant(user)
    a = await _load_owned(session, tid, user.id, _uuid(assessment_id, "assessment_id"))
    if a.status != "draft":
        raise HTTPException(409, "only draft papers can be edited")

    if body.title is not None and body.title.strip():
        a.title = body.title.strip()[:200]

    if body.questions:
        edits = {_uuid(e.question_id, "question_id"): e for e in body.questions}
        rows = await session.execute(
            select(Question).where(
                Question.assessment_id == a.id, Question.id.in_(edits.keys())
            )
        )
        for q in rows.scalars().all():
            e = edits[q.id]
            edited = False
            if e.question_text is not None:
                q.question_text = e.question_text.strip()
                edited = True
            if e.max_marks is not None and e.max_marks > 0:
                q.max_marks = Decimal(str(e.max_marks))
                edited = True
            if edited:
                q.user_edited = True
    await session.flush()
    return await get_paper(assessment_id, user, session)


class SourceChunkOut(BaseModel):
    point_id: str
    chapter_name: str | None
    topic_name: str | None
    chapter_order: int | None
    page: int | None
    chunk_type: str
    source: str | None
    content: str


class SourcesOut(BaseModel):
    question_id: str
    grounding_score: float | None
    sources: list[SourceChunkOut]


@router.get(
    "/{assessment_id}/questions/{question_id}/sources",
    response_model=SourcesOut,
)
async def question_sources(
    assessment_id: str, question_id: str,
    user: TeacherDep, session: SessionDep,
) -> SourcesOut:
    """Return the textbook excerpts that grounded a generated question.

    Looks up the Question.source_chunk_ids (Qdrant point ids) recorded
    by the paper generator and fetches the chunk payloads tenant-scoped
    so the teacher can audit the basis of every question via the UI
    "Show source" drawer.
    """
    tid = assert_tenant(user)
    a = await _load_owned(session, tid, user.id, _uuid(assessment_id, "assessment_id"))
    qid = _uuid(question_id, "question_id")
    q = await session.get(Question, qid)
    if q is None or q.assessment_id != a.id:
        raise HTTPException(404, "question not found")

    point_ids = list(q.source_chunk_ids or [])
    hits = []
    if point_ids:
        hits = await get_qdrant_store().get_chunks_by_ids(
            tenant_id=tid, point_ids=point_ids,
        )
    # Best-effort: also surface the chapter name from Postgres in case
    # the Qdrant payload didn't get back-tagged (older imports).
    return SourcesOut(
        question_id=str(q.id),
        grounding_score=q.grounding_score,
        sources=[
            SourceChunkOut(
                point_id=h.point_id or "",
                chapter_name=None,  # Qdrant payload doesn't store names
                topic_name=h.topic_name,
                chapter_order=h.chapter_order,
                page=h.page,
                chunk_type=h.chunk_type,
                source=h.source,
                content=h.content,
            )
            for h in hits
        ],
    )


@router.post(
    "/{assessment_id}/questions/{question_id}/regenerate",
    response_model=QuestionOut,
)
async def regenerate_question(
    assessment_id: str, question_id: str,
    user: TeacherDep, session: SessionDep,
) -> QuestionOut:
    """Re-roll a single question without restarting the whole paper.

    Synchronous so the UI can replace the row in place. Reuses the same
    retrieval + grounding pipeline as the paper job, but for one topic
    and one question count.
    """
    from backend.db.models import Chapter  # local to avoid circulars

    tid = assert_tenant(user)
    a = await _load_owned(session, tid, user.id, _uuid(assessment_id, "assessment_id"))
    if a.status == "finalized":
        raise HTTPException(409, "finalized papers cannot be edited")
    if (a.generation_meta or {}).get("mode") == "manual":
        raise HTTPException(409, "manual papers don't support AI regeneration")
    qid = _uuid(question_id, "question_id")
    q = await session.get(Question, qid)
    if q is None or q.assessment_id != a.id:
        raise HTTPException(404, "question not found")
    if q.user_edited:
        raise HTTPException(409, "this question was manually edited — "
                                  "remove the edit or regenerate the whole paper")

    topic = await session.get(Topic, q.topic_id)
    if topic is None:
        raise HTTPException(404, "topic not found")
    chapter = await session.get(Chapter, topic.chapter_id) if topic.chapter_id else None
    meta = a.generation_meta or {}
    template = await session.get(QuestionPaperTemplate, a.template_id) if a.template_id else None
    qtype = "short_answer"
    if template and template.sections and q.section_no is not None:
        for sec in template.sections:
            if int(sec.get("section_no", 0)) == int(q.section_no):
                qtype = sec.get("question_type", "short_answer")
                break

    # Run the same per-topic generator the worker uses.
    from backend.services.paper_generator import PaperGenerator
    from backend.llm.ollama import get_ollama
    from backend.rag.embeddings import get_embedder
    pg = PaperGenerator(session)
    questions, hits = await pg._generate_for_topic(
        embedder=get_embedder(), qstore=get_qdrant_store(), ollama=get_ollama(),
        tenant_id=tid, grade=meta.get("grade"),
        subject_name=meta.get("subject_name") or "",
        chapter_name=chapter.name if chapter else None,
        topic=topic, qtype=qtype, count=1,
    )
    if not questions:
        raise HTTPException(
            422,
            "could not regenerate this question — the topic has no "
            "textbook coverage or the LLM returned nothing grounded",
        )
    new_q = questions[0]
    q.question_text = str(new_q.get("question_text", "")).strip() or q.question_text
    q.source_chunk_ids = [h.point_id for h in hits if h.point_id] or None
    score = new_q.get("_grounding_score")
    q.grounding_score = float(score) if isinstance(score, (int, float)) else None
    await session.flush()
    return _paper_question(q, topic.name, chapter.name if chapter else None)


@router.post("/{assessment_id}/regenerate", response_model=GenerateOut, status_code=202)
async def regenerate_paper(
    assessment_id: str, user: TeacherDep, session: SessionDep
) -> GenerateOut:
    """Re-run paper generation for the same assessment, preserving any
    teacher-edited questions (Question.user_edited=true). Useful when
    the first run produced 0 questions or warned about ungrounded topics.
    """
    tid = assert_tenant(user)
    a = await _load_owned(session, tid, user.id, _uuid(assessment_id, "assessment_id"))
    if a.status == "generating":
        raise HTTPException(409, "paper is already generating")
    if a.status == "finalized":
        raise HTTPException(409, "finalized papers cannot be regenerated")
    _meta = a.generation_meta or {}
    # Language-subject manual papers are manual by design — no AI to retry.
    # AI-fallback manual papers ARE allowed to retry (textbook may have
    # been uploaded since the first attempt failed to ground).
    if _meta.get("mode") == "manual" and not _meta.get("ai_fallback"):
        raise HTTPException(409, "manual papers don't support AI regeneration")
    a.status = "generating"
    # Clear stale warnings so the UI doesn't show last run's banner.
    meta = dict(a.generation_meta or {})
    meta["warnings"] = []
    meta["topics_no_context"] = []
    meta.pop("error", None)
    a.generation_meta = meta
    await session.flush()

    job_id = await enqueue("generate_paper_job", assessment_id=str(a.id))
    if job_id is None:
        a.status = "draft"
        await session.flush()
        raise HTTPException(503, "generation queue unavailable, please retry")
    logger.info("paper.regenerate.queued", assessment_id=str(a.id))
    return GenerateOut(assessment_id=str(a.id), status="generating")


@router.delete("/{assessment_id}", status_code=204)
async def delete_paper(
    assessment_id: str, user: TeacherDep, session: SessionDep
) -> None:
    """Delete a draft paper. Finalized papers cannot be deleted."""
    tid = assert_tenant(user)
    a = await _load_owned(session, tid, user.id, _uuid(assessment_id, "assessment_id"))
    if a.status == "finalized":
        raise HTTPException(409, "finalized papers cannot be deleted")
    await session.delete(a)


@router.post("/{assessment_id}/finalize", response_model=PaperOut)
async def finalize_paper(
    assessment_id: str, user: TeacherDep, session: SessionDep
) -> PaperOut:
    tid = assert_tenant(user)
    a = await _load_owned(session, tid, user.id, _uuid(assessment_id, "assessment_id"))
    if a.status == "generating":
        raise HTTPException(409, "paper is still generating")
    a.status = "finalized"
    await session.flush()
    return await get_paper(assessment_id, user, session)


# ── marks entry ──────────────────────────────────────────


class StudentLite(BaseModel):
    id: str
    handle: str
    name: str


class MarksQuestion(BaseModel):
    id: str
    question_no: int
    section_no: int | None
    max_marks: float


class MarksTableOut(BaseModel):
    assessment_id: str
    title: str
    status: str
    total_marks: float
    students: list[StudentLite]
    questions: list[MarksQuestion]
    marks: dict[str, dict[str, float]]   # student_id -> question_id -> marks


class MarksRowIn(BaseModel):
    student_id: str
    marks: dict[str, float]              # question_id -> marks_obtained


class MarksSaveIn(BaseModel):
    rows: list[MarksRowIn]


async def _classroom_students(session, classroom_id: uuid.UUID) -> list[Student]:
    rows = await session.execute(
        select(Student).where(Student.classroom_id == classroom_id)
        .order_by(Student.handle)
    )
    return list(rows.scalars().all())


async def _marks_table(session, a: Assessment) -> MarksTableOut:
    q_rows = (await session.execute(
        select(Question).where(Question.assessment_id == a.id)
        .order_by(Question.question_no)
    )).scalars().all()
    students = await _classroom_students(session, a.classroom_id) if a.classroom_id else []

    q_ids = [q.id for q in q_rows]
    existing: dict[str, dict[str, float]] = {}
    if q_ids:
        resp = await session.execute(
            select(QuestionResponse).where(QuestionResponse.question_id.in_(q_ids))
        )
        for r in resp.scalars().all():
            existing.setdefault(str(r.student_id), {})[str(r.question_id)] = float(r.marks_obtained)

    return MarksTableOut(
        assessment_id=str(a.id), title=a.title, status=a.status,
        total_marks=float(a.max_marks),
        students=[
            StudentLite(id=str(s.id), handle=s.handle, name=s.full_name)
            for s in students
        ],
        questions=[
            MarksQuestion(id=str(q.id), question_no=q.question_no,
                          section_no=q.section_no, max_marks=float(q.max_marks))
            for q in q_rows
        ],
        marks=existing,
    )


@router.get("/{assessment_id}/marks", response_model=MarksTableOut)
async def get_marks(
    assessment_id: str, user: TeacherDep, session: SessionDep
) -> MarksTableOut:
    tid = assert_tenant(user)
    a = await _load_owned(session, tid, user.id, _uuid(assessment_id, "assessment_id"))
    return await _marks_table(session, a)


@router.post("/{assessment_id}/marks", response_model=MarksTableOut)
async def save_marks(
    assessment_id: str, body: MarksSaveIn, user: TeacherDep, session: SessionDep
) -> MarksTableOut:
    tid = assert_tenant(user)
    a = await _load_owned(session, tid, user.id, _uuid(assessment_id, "assessment_id"))
    if a.status != "finalized":
        raise HTTPException(409, "finalize the paper before entering marks")

    # max_marks per question — to clamp + derive is_correct
    q_max: dict[uuid.UUID, Decimal] = {
        q.id: q.max_marks for q in (await session.execute(
            select(Question).where(Question.assessment_id == a.id)
        )).scalars().all()
    }
    valid_students = {
        s.id for s in await _classroom_students(session, a.classroom_id)
    } if a.classroom_id else set()

    for row in body.rows:
        try:
            sid = uuid.UUID(row.student_id)
        except ValueError:
            continue
        if sid not in valid_students:
            continue
        for q_str, value in row.marks.items():
            try:
                qid = uuid.UUID(q_str)
            except ValueError:
                continue
            if qid not in q_max:
                continue
            marks = max(Decimal("0"), min(Decimal(str(value)), q_max[qid]))
            await session.execute(
                pg_insert(QuestionResponse)
                .values(
                    id=uuid.uuid4(), question_id=qid, student_id=sid,
                    marks_obtained=marks, is_correct=marks >= q_max[qid],
                )
                .on_conflict_do_update(
                    constraint="uq_responses_question_student",
                    set_={"marks_obtained": marks, "is_correct": marks >= q_max[qid]},
                )
            )

    # snapshot the report card
    await _upsert_report_card(session, a, tid, user.id)
    await session.flush()
    return await _marks_table(session, a)


# ── report card ──────────────────────────────────────────


class ReportRow(BaseModel):
    student_id: str
    handle: str
    name: str
    obtained: float
    percentage: float
    rank: int


class ReportCardOut(BaseModel):
    assessment_id: str
    title: str
    total_marks: float
    rows: list[ReportRow]


async def _compute_report(session, a: Assessment) -> list[ReportRow]:
    students = await _classroom_students(session, a.classroom_id) if a.classroom_id else []
    q_ids = [
        q for q in (await session.execute(
            select(Question.id).where(Question.assessment_id == a.id)
        )).scalars().all()
    ]
    obtained: dict[uuid.UUID, float] = {s.id: 0.0 for s in students}
    if q_ids:
        resp = await session.execute(
            select(QuestionResponse.student_id, QuestionResponse.marks_obtained)
            .where(QuestionResponse.question_id.in_(q_ids))
        )
        for sid, marks in resp.all():
            if sid in obtained:
                obtained[sid] += float(marks)

    total = float(a.max_marks) or 1.0
    ranked = sorted(students, key=lambda s: obtained[s.id], reverse=True)
    rows: list[ReportRow] = []
    for i, s in enumerate(ranked):
        rows.append(ReportRow(
            student_id=str(s.id), handle=s.handle, name=s.full_name,
            obtained=round(obtained[s.id], 2),
            percentage=round(obtained[s.id] / total * 100, 1),
            rank=i + 1,
        ))
    return rows


async def _upsert_report_card(
    session, a: Assessment, tenant_id: uuid.UUID, user_id: uuid.UUID
) -> None:
    rows = await _compute_report(session, a)
    summary = {
        "rows": [r.model_dump() for r in rows],
        "total_marks": float(a.max_marks),
        "snapshot_at": datetime.now(timezone.utc).isoformat(),
    }
    await session.execute(
        pg_insert(ReportCard)
        .values(
            id=uuid.uuid4(), assessment_id=a.id, tenant_id=tenant_id,
            classroom_id=a.classroom_id, summary_json=summary,
            generated_by=user_id,
        )
        .on_conflict_do_update(
            index_elements=[ReportCard.assessment_id],
            set_={"summary_json": summary, "generated_by": user_id},
        )
    )


@router.get("/{assessment_id}/report-card", response_model=ReportCardOut)
async def report_card(
    assessment_id: str, user: TeacherDep, session: SessionDep
) -> ReportCardOut:
    """Live result table — recomputed from marks so edits stay reflected."""
    tid = assert_tenant(user)
    a = await _load_owned(session, tid, user.id, _uuid(assessment_id, "assessment_id"))
    rows = await _compute_report(session, a)
    return ReportCardOut(
        assessment_id=str(a.id), title=a.title,
        total_marks=float(a.max_marks), rows=rows,
    )


# ── Excel export ─────────────────────────────────────────


@router.get("/{assessment_id}/marks.xlsx")
async def marks_excel(
    assessment_id: str, user: TeacherDep, session: SessionDep
) -> Response:
    """Marks sheet — roll/name rows, one column per question grouped by
    section, ready for the teacher to fill or archive."""
    import openpyxl

    tid = assert_tenant(user)
    a = await _load_owned(session, tid, user.id, _uuid(assessment_id, "assessment_id"))
    table = await _marks_table(session, a)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Marks"

    header = ["Roll No", "Name"]
    for q in table.questions:
        sec = f"S{q.section_no}·" if q.section_no else ""
        header.append(f"{sec}Q{q.question_no} ({q.max_marks:g})")
    header.append("Total")
    ws.append(header)

    for s in table.students:
        row: list = [s.handle, s.name]
        row_total = 0.0
        smarks = table.marks.get(s.id, {})
        for q in table.questions:
            v = smarks.get(q.id)
            row.append(v if v is not None else "")
            if v is not None:
                row_total += v
        row.append(row_total if smarks else "")
        ws.append(row)

    buf = io.BytesIO()
    wb.save(buf)
    safe_title = "".join(c if c.isalnum() else "_" for c in a.title)[:60]
    return Response(
        content=buf.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{safe_title}_marks.xlsx"'},
    )
