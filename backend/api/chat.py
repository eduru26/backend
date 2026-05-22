from __future__ import annotations

import time
import uuid
from datetime import date, datetime

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.agents.analytics import AnalyticsAgent
from backend.agents.context import ContextAgent
from backend.api.deps import CurrentUser, SessionDep, assert_tenant
from backend.core.logging import get_logger

from backend.db.models import (
    Assignment, AssignmentQuestion, AssignmentResponse, AssignmentStatus,
    Conversation, Message, MessageRole, ParentStudent, Student, User, UserRole,
)
from backend.pipelines.mentor import run_pipeline
from backend.rag.analytics import QueryAnalytics

logger = get_logger(__name__)

router = APIRouter(prefix="/chat", tags=["chat"])


# ── Schemas ──────────────────────────────────────────────


class ConversationOut(BaseModel):
    id: str
    created_at: datetime


class ConversationSummaryOut(BaseModel):
    id: str
    title: str
    message_count: int
    last_message_at: datetime
    created_at: datetime


class MessageIn(BaseModel):
    content: str
    student_id: str | None = None  # parent selects a child; omit for class-level (teacher)
    # Teacher "whole class" scope: restrict context to one classroom they own.
    # Ignored when student_id is set, and ignored for non-teacher roles.
    classroom_id: str | None = None


class MessageOut(BaseModel):
    id: str
    role: str
    content: str
    created_at: datetime


class StudentOut(BaseModel):
    id: str
    name: str
    grade: int
    section: str


class StudentStatsOut(BaseModel):
    student: StudentOut
    overall_pct: float
    attendance_rate: float | None
    subject_pct: dict[str, float]
    weak_topics: list[str]


# ── Endpoints ────────────────────────────────────────────


@router.post("/conversations", response_model=ConversationOut)
async def create_conversation(user: CurrentUser, session: SessionDep) -> ConversationOut:
    conv = Conversation(user_id=user.id)
    session.add(conv)
    await session.flush()
    await session.refresh(conv)
    return ConversationOut(id=str(conv.id), created_at=conv.created_at)


@router.get("/conversations", response_model=list[ConversationSummaryOut])
async def list_conversations(
    user: CurrentUser, session: SessionDep, limit: int = 50
) -> list[ConversationSummaryOut]:
    """Return the caller's conversations newest-first. Title is derived from
    the first user message, truncated to ~60 chars."""
    from sqlalchemy import func as sa_func
    rows = await session.execute(
        select(Conversation).where(Conversation.user_id == user.id)
        .order_by(Conversation.created_at.desc())
        .limit(max(1, min(limit, 200)))
    )
    convs = rows.scalars().all()
    if not convs:
        return []

    cids = [c.id for c in convs]

    # First user message per conversation (for title)
    first_rows = await session.execute(
        select(Message.conversation_id, Message.content)
        .where(
            Message.conversation_id.in_(cids),
            Message.role == MessageRole.user,
        )
        .order_by(Message.conversation_id, Message.created_at)
    )
    first_by_conv: dict[uuid.UUID, str] = {}
    for cid, content in first_rows.all():
        first_by_conv.setdefault(cid, content)

    # Latest message timestamp + count per conversation
    agg = await session.execute(
        select(
            Message.conversation_id,
            sa_func.max(Message.created_at).label("last_at"),
            sa_func.count().label("n"),
        )
        .where(Message.conversation_id.in_(cids))
        .group_by(Message.conversation_id)
    )
    stats_by_conv: dict[uuid.UUID, tuple[datetime, int]] = {
        row.conversation_id: (row.last_at, int(row.n)) for row in agg
    }

    out: list[ConversationSummaryOut] = []
    for c in convs:
        last_at, count = stats_by_conv.get(c.id, (c.created_at, 0))
        raw_title = first_by_conv.get(c.id, "").strip()
        if not raw_title:
            title = "New conversation"
        else:
            # Use first non-empty line, truncated to ~60 chars
            line = raw_title.splitlines()[0]
            title = line if len(line) <= 60 else line[:57] + "…"
        out.append(ConversationSummaryOut(
            id=str(c.id),
            title=title,
            message_count=count,
            last_message_at=last_at,
            created_at=c.created_at,
        ))
    return out


@router.delete("/conversations/{conversation_id}", status_code=204)
async def delete_conversation(
    conversation_id: str, user: CurrentUser, session: SessionDep
) -> None:
    conv = await _get_conversation(conversation_id, user, session)
    await session.delete(conv)
    await session.flush()


@router.get("/conversations/{conversation_id}/messages", response_model=list[MessageOut])
async def get_messages(
    conversation_id: str,
    user: CurrentUser,
    session: SessionDep,
) -> list[MessageOut]:
    conv = await _get_conversation(conversation_id, user, session)
    result = await session.execute(
        select(Message)
        .where(Message.conversation_id == conv.id)
        .order_by(Message.created_at)
    )
    return [
        MessageOut(id=str(m.id), role=m.role.value, content=m.content, created_at=m.created_at)
        for m in result.scalars().all()
    ]


@router.post("/conversations/{conversation_id}/messages", response_model=MessageOut)
async def send_message(
    conversation_id: str,
    body: MessageIn,
    user: CurrentUser,
    session: SessionDep,
) -> MessageOut:
    tenant_id = assert_tenant(user)
    conv = await _get_conversation(conversation_id, user, session)

    user_msg = Message(
        conversation_id=conv.id, role=MessageRole.user, content=body.content
    )
    session.add(user_msg)
    await session.flush()

    student_id = await _resolve_student_id(user, body.student_id, session)
    classroom_id = await _resolve_classroom_id(user, body.classroom_id, session)

    # The mentor pipeline calls Ollama for insights + validation. If Ollama is
    # unreachable / the model isn't pulled, that bubbles up as an
    # httpx.HTTPError. We catch it and persist a friendly assistant message
    # instead of returning 500 (which leaves a dangling user message in the
    # conversation and shows the generic "something went wrong" toast).
    started = time.perf_counter()
    result_state: dict | None = None
    pipeline_error: str | None = None
    try:
        result_state = await run_pipeline(
            session,
            {
                "role": user.role.value,
                "user_id": str(user.id),
                "user_email": user.email or "",   # parents may have no email
                "tenant_id": str(tenant_id),
                "question": body.content,
                "student_id": student_id,
                "classroom_id": classroom_id,
                "conversation_id": conversation_id,
            },
        )
        final_response = result_state["final_response"]
    except (httpx.HTTPError, httpx.RequestError) as exc:
        logger.warning("chat.llm.unavailable", error=str(exc))
        pipeline_error = "llm_unavailable"
        final_response = (
            "I can't reach the AI service right now. Please try again in a "
            "moment. If this keeps happening, ask your administrator to check "
            "the mentor service status."
        )
    except Exception as exc:  # noqa: BLE001 — surface as graceful message
        logger.exception("chat.pipeline.failed", error=str(exc))
        pipeline_error = "pipeline_failed"
        final_response = (
            "Something went wrong while preparing your answer. Please try "
            "rephrasing your question or try again shortly."
        )

    # Telemetry — fire-and-forget. We swallow errors here so a logging
    # bug never fails a chat turn.
    try:
        latency_ms = int((time.perf_counter() - started) * 1000)
        intent = (result_state or {}).get("intent", "") or pipeline_error or "unknown"
        blocked = bool(
            (result_state or {}).get("refusal_message")
            or not (result_state or {}).get("guardrail_allowed", True)
        )
        rag_hits = len((result_state or {}).get("rag_chunks") or [])
        await QueryAnalytics(session).record(
            tenant_id=tenant_id,
            user_id=user.id,
            role=user.role.value,
            intent=str(intent)[:40],
            question=body.content,
            blocked=blocked,
            block_reason=(result_state or {}).get("guardrail_reason"),
            latency_ms=latency_ms,
            rag_hits=rag_hits,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("chat.analytics.failed", error=str(exc))

    ai_msg = Message(
        conversation_id=conv.id,
        role=MessageRole.assistant,
        content=final_response,
    )
    session.add(ai_msg)
    await session.flush()
    await session.refresh(ai_msg)

    return MessageOut(
        id=str(ai_msg.id),
        role=ai_msg.role.value,
        content=ai_msg.content,
        created_at=ai_msg.created_at,
    )


@router.get("/students", response_model=list[StudentOut])
async def list_students(user: CurrentUser, session: SessionDep) -> list[StudentOut]:
    """
    Student  → own record only (resolved from Student.user_id == user.id).
    Parent   → their children (via parent_students), scoped to tenant.
    Teacher  → all students in their school.
    """
    tid = assert_tenant(user)
    if user.role is UserRole.student:
        sid = getattr(user, "_student_id", None)
        if not sid:
            return []
        result = await session.execute(
            select(Student).where(Student.id == sid, Student.tenant_id == tid)
        )
        rows = [result.scalar_one_or_none()]
        students = [s for s in rows if s]
    elif user.role is UserRole.parent:
        result = await session.execute(
            select(Student)
            .join(ParentStudent, ParentStudent.student_id == Student.id)
            .where(ParentStudent.parent_id == user.id, Student.tenant_id == tid)
        )
        students = list(result.scalars().all())
    else:
        result = await session.execute(
            select(Student).where(Student.tenant_id == tid).order_by(Student.full_name)
        )
        students = list(result.scalars().all())

    return [
        StudentOut(id=str(s.id), name=s.full_name, grade=s.grade, section=s.section)
        for s in students
    ]


@router.get("/stats", response_model=StudentStatsOut)
async def student_stats(
    user: CurrentUser,
    session: SessionDep,
    student_id: str | None = None,
) -> StudentStatsOut:
    """
    Returns live analytics for a student without going through the chat pipeline.
    Student → own stats. Parent → child stats. Teacher → must provide student_id.
    """
    tid = assert_tenant(user)

    # Resolve which student to show stats for
    resolved_sid: str | None = None
    if user.role is UserRole.student:
        sid = getattr(user, "_student_id", None)
        resolved_sid = str(sid) if sid else None
    elif user.role is UserRole.parent:
        if student_id:
            resolved_sid = student_id
        else:
            # default to first linked child
            first = await session.scalar(
                select(ParentStudent).where(ParentStudent.parent_id == user.id).limit(1)
            )
            resolved_sid = str(first.student_id) if first else None
    else:  # teacher / school_admin
        resolved_sid = student_id

    if not resolved_sid:
        raise HTTPException(400, "student_id is required")

    # Build context the same way the pipeline does (direct, no LLM)
    ctx_agent = ContextAgent(session)
    fake_state = {
        "role": user.role.value,
        "user_id": str(user.id),
        "user_email": user.email or "",   # parents may have no email
        "tenant_id": str(tid),
        "question": "",
        "student_id": resolved_sid,
        "classroom_id": None,
        "conversation_id": "",
        "students": [], "raw_scores": [], "raw_attendance": [],
        "analytics": {}, "system_prompt": "", "user_prompt": "",
        "llm_response": "", "validation_passed": False,
        "retry_count": 0, "final_response": "",
    }
    ctx = await ctx_agent.run(fake_state)  # type: ignore[arg-type]
    analytics = AnalyticsAgent().run({**fake_state, **ctx})["analytics"]  # type: ignore[arg-type]

    students = ctx.get("students", [])
    student = students[0] if students else {"id": resolved_sid, "name": "Unknown", "grade": 0, "section": ""}

    return StudentStatsOut(
        student=StudentOut(
            id=student.get("id", resolved_sid),
            name=student.get("name", "Unknown"),
            grade=student.get("grade", 0),
            section=student.get("section", ""),
        ),
        overall_pct=analytics.get("overall_pct", 0.0),
        attendance_rate=analytics.get("attendance_rate"),
        subject_pct=analytics.get("subject_pct", {}),
        weak_topics=analytics.get("weak_topics", [])[:5],
    )


# ── Student: assignments ──────────────────────────────────


class AssignmentQuestionOut(BaseModel):
    id: str
    order_num: int
    question_text: str
    option_a: str
    option_b: str
    option_c: str
    option_d: str


class StudentAssignmentOut(BaseModel):
    id: str
    subject: str
    title: str | None
    due_date: date | None
    created_at: datetime
    question_count: int
    completed: bool
    score: int | None
    questions: list[AssignmentQuestionOut]


class SubmitIn(BaseModel):
    answers: dict[str, str]  # question_id → 'a'|'b'|'c'|'d'


class SubmitOut(BaseModel):
    score: int
    total: int
    already_submitted: bool


@router.get("/assignments", response_model=list[StudentAssignmentOut])
async def student_assignments(user: CurrentUser, session: SessionDep) -> list[StudentAssignmentOut]:
    """Return published assignments for the student's classroom.

    Only the student view (role=student token, set by roll-number login) sees
    assignments. The parent view intentionally omits this surface — parents
    use Dashboard + Chat. Correct answers are never exposed.
    """
    if user.role is not UserRole.student:
        raise HTTPException(status_code=403, detail="only students can use this endpoint")

    sid = getattr(user, "_student_id", None)
    if not sid:
        return []

    student = await session.get(Student, sid)
    if not student or not student.classroom_id:
        return []

    result = await session.execute(
        select(Assignment).where(
            Assignment.classroom_id == student.classroom_id,
            Assignment.status == AssignmentStatus.published,
        ).order_by(Assignment.created_at.desc())
    )
    assignments = result.scalars().all()

    out = []
    for a in assignments:
        q_result = await session.execute(
            select(AssignmentQuestion)
            .where(AssignmentQuestion.assignment_id == a.id)
            .order_by(AssignmentQuestion.order_num)
        )
        questions = [
            AssignmentQuestionOut(
                id=str(q.id), order_num=q.order_num,
                question_text=q.question_text,
                option_a=q.option_a, option_b=q.option_b,
                option_c=q.option_c, option_d=q.option_d,
            )
            for q in q_result.scalars().all()
        ]
        submission = await session.scalar(
            select(AssignmentResponse).where(
                AssignmentResponse.assignment_id == a.id,
                AssignmentResponse.student_id == sid,
            )
        )
        out.append(StudentAssignmentOut(
            id=str(a.id), subject=a.subject, title=a.title,
            due_date=a.due_date, created_at=a.created_at,
            question_count=len(questions),
            completed=submission is not None,
            score=submission.score if submission else None,
            questions=questions,
        ))
    return out


@router.post("/assignments/{assignment_id}/submit", response_model=SubmitOut)
async def submit_assignment(
    assignment_id: str, body: SubmitIn, user: CurrentUser, session: SessionDep
) -> SubmitOut:
    if user.role is not UserRole.student:
        raise HTTPException(status_code=403, detail="only students can submit assignments")
    sid = getattr(user, "_student_id", None)
    if not sid:
        raise HTTPException(status_code=403, detail="no student identity in token")

    try:
        aid = uuid.UUID(assignment_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid assignment id")

    assignment = await session.get(Assignment, aid)
    if not assignment or assignment.status is not AssignmentStatus.published:
        raise HTTPException(status_code=404, detail="assignment not found")

    existing = await session.scalar(
        select(AssignmentResponse).where(
            AssignmentResponse.assignment_id == aid,
            AssignmentResponse.student_id == sid,
        )
    )
    if existing:
        return SubmitOut(score=existing.score, total=existing.total_questions, already_submitted=True)

    q_result = await session.execute(
        select(AssignmentQuestion).where(AssignmentQuestion.assignment_id == aid)
    )
    questions = q_result.scalars().all()
    score = sum(1 for q in questions if body.answers.get(str(q.id)) == q.correct_option)

    session.add(AssignmentResponse(
        id=uuid.uuid4(), assignment_id=aid, student_id=sid,
        answers=body.answers, score=score, total_questions=len(questions),
    ))
    return SubmitOut(score=score, total=len(questions), already_submitted=False)


# ── Parent: AI-mentor suggestions ────────────────────────


class SuggestionsOut(BaseModel):
    student_name: str
    suggestions: list[str]


@router.get("/suggestions", response_model=SuggestionsOut)
async def student_suggestions(
    user: CurrentUser,
    session: SessionDep,
    student_id: str | None = None,
) -> SuggestionsOut:
    """Return rule-based AI-mentor suggestions for a student, derived from their analytics."""
    if user.role not in (
        UserRole.parent, UserRole.teacher, UserRole.school_admin, UserRole.student,
    ):
        raise HTTPException(status_code=403, detail="not permitted")
    tid = assert_tenant(user)

    resolved_sid = student_id
    if user.role is UserRole.student:
        # Students always view their own analytics — ignore any query override.
        sid_from_token = getattr(user, "_student_id", None)
        resolved_sid = str(sid_from_token) if sid_from_token else None
    elif user.role is UserRole.parent and not resolved_sid:
        first = await session.scalar(
            select(ParentStudent).where(ParentStudent.parent_id == user.id).limit(1)
        )
        resolved_sid = str(first.student_id) if first else None
    if not resolved_sid:
        raise HTTPException(status_code=400, detail="student_id is required")

    ctx_agent = ContextAgent(session)
    fake_state: dict = {
        "role": user.role.value, "user_id": str(user.id), "user_email": user.email or "",
        "tenant_id": str(tid), "question": "", "student_id": resolved_sid,
        "classroom_id": None,
        "conversation_id": "", "students": [], "raw_scores": [], "raw_attendance": [],
        "analytics": {}, "system_prompt": "", "user_prompt": "",
        "llm_response": "", "validation_passed": False,
        "retry_count": 0, "final_response": "",
    }
    ctx = await ctx_agent.run(fake_state)  # type: ignore[arg-type]
    analytics = AnalyticsAgent().run({**fake_state, **ctx})["analytics"]  # type: ignore[arg-type]

    students = ctx.get("students", [])
    student_name = students[0].get("name", "Student") if students else "Student"

    tips: list[str] = []
    att = analytics.get("attendance_rate")
    overall = analytics.get("overall_pct", 0.0)
    subject_pct: dict = analytics.get("subject_pct", {})
    weak_topics: list = analytics.get("weak_topics", [])

    if att is not None:
        if att < 75:
            tips.append(f"Attendance is low at {att:.0f}% — regular attendance is critical for performance.")
        elif att < 85:
            tips.append(f"Attendance is {att:.0f}% — encourage consistent attendance to improve further.")
        else:
            tips.append(f"Great attendance at {att:.0f}% — keep it up!")

    if overall:
        if overall < 50:
            tips.append(f"Overall score is {overall:.0f}% — consider extra support or tutoring sessions.")
        elif overall < 70:
            tips.append(f"Overall score is {overall:.0f}% — there is room for improvement with focused practice.")
        else:
            tips.append(f"Overall score is strong at {overall:.0f}% — maintain the momentum.")

    for subj, pct in subject_pct.items():
        if pct < 50:
            tips.append(f"{subj}: scoring {pct:.0f}% — this subject needs dedicated attention.")
        elif pct > 85:
            tips.append(f"{subj}: excellent at {pct:.0f}% — consider advanced practice problems.")

    if weak_topics:
        tips.append(f"Topics needing more practice: {', '.join(weak_topics[:3])}.")

    if not tips:
        tips.append("Not enough data yet. Check back after more assessments are recorded.")

    return SuggestionsOut(student_name=student_name, suggestions=tips[:6])


# ── Proactive suggestions (query-trend surfaced) ─────────


class ProactiveItem(BaseModel):
    label: str
    intent: str
    count: int | None = None


class ProactiveOut(BaseModel):
    items: list[ProactiveItem]


# Friendly prompts shown for each intent. Falls back to a generic label.
_INTENT_LABEL: dict[str, dict[str, str]] = {
    "student": {
        "own_performance": "How am I doing overall?",
        "attendance_query": "How is my attendance trending?",
        "proactive_suggestion": "What should I focus on next?",
        "curriculum_qa": "Explain a concept from my syllabus",
        "policy_qa": "What's the school policy on…?",
        "announcement_recall": "Any recent announcements I missed?",
    },
    "parent": {
        "child_performance": "How is my child doing in their subjects?",
        "attendance_query": "How is my child's attendance?",
        "proactive_suggestion": "What can I do to help my child?",
        "policy_qa": "What's the school policy on…?",
        "announcement_recall": "Any recent school announcements?",
    },
    "teacher": {
        "class_aggregate": "Which students in my class are at risk?",
        "own_performance": "Summarise a specific student's performance",
        "proactive_suggestion": "Where should I focus my class next week?",
        "curriculum_qa": "Find resources from the curriculum",
        "announcement_recall": "Recent announcements relevant to my class",
    },
    "school_admin": {
        "school_aggregate": "How is the school trending overall?",
        "class_aggregate": "Which classes need attention?",
        "policy_qa": "Surface a school policy",
        "announcement_recall": "Recent school-wide announcements",
    },
}


@router.get("/proactive", response_model=ProactiveOut)
async def proactive_suggestions(
    user: CurrentUser, session: SessionDep
) -> ProactiveOut:
    """Returns role-tailored question prompts.

    Items are a union of:
      (a) trending intents in this tenant in the last 7 days (popularity-based)
      (b) a static fallback so brand-new tenants still see useful prompts
    """
    tid = assert_tenant(user)
    role = user.role.value

    qa = QueryAnalytics(session)
    trending = await qa.trending_intents(tenant_id=tid, role=role, days=7, limit=4)

    role_labels = _INTENT_LABEL.get(role, {})
    seen: set[str] = set()
    items: list[ProactiveItem] = []
    for t in trending:
        label = role_labels.get(t.intent)
        if not label:
            continue
        items.append(ProactiveItem(label=label, intent=t.intent, count=t.count))
        seen.add(t.intent)

    # Pad with static defaults — keeps the UI populated for new tenants
    # and ensures every role has at least 3 prompts.
    for intent_key, label in role_labels.items():
        if intent_key in seen:
            continue
        items.append(ProactiveItem(label=label, intent=intent_key, count=None))
        if len(items) >= 6:
            break

    return ProactiveOut(items=items[:6])


# ── Helpers ──────────────────────────────────────────────


async def _get_conversation(
    conversation_id: str, user: User, session: AsyncSession
) -> Conversation:
    try:
        cid = uuid.UUID(conversation_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid conversation id")
    result = await session.execute(
        select(Conversation).where(
            Conversation.id == cid, Conversation.user_id == user.id
        )
    )
    conv = result.scalar_one_or_none()
    if not conv:
        raise HTTPException(status_code=404, detail="conversation not found")
    return conv


async def _resolve_student_id(
    user: User, requested: str | None, session: AsyncSession
) -> str | None:
    if user.role in (UserRole.teacher, UserRole.school_admin):
        return requested  # scope to one student or None for class-level

    if user.role is UserRole.student:
        sid = getattr(user, "_student_id", None)
        return str(sid) if sid else None

    # parent — verify the requested child belongs to them via junction table
    if requested:
        try:
            rid = uuid.UUID(requested)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid student_id")
        link = await session.scalar(
            select(ParentStudent).where(
                ParentStudent.parent_id == user.id,
                ParentStudent.student_id == rid,
            )
        )
        if not link:
            raise HTTPException(status_code=403, detail="not your child")
        return requested

    # default: first child
    link = await session.scalar(
        select(ParentStudent).where(ParentStudent.parent_id == user.id).limit(1)
    )
    return str(link.student_id) if link else None


async def _resolve_classroom_id(
    user: User, requested: str | None, session: AsyncSession
) -> str | None:
    """Validate a teacher-supplied classroom_id and return it.

    Only teachers and school admins can use this — for everyone else it's
    ignored. We verify the classroom belongs to the caller's tenant (and for
    teachers, that they're actually associated with it) so the pipeline can
    safely filter on it without a second tenant check inside the agent.
    """
    if not requested:
        return None
    if user.role not in (UserRole.teacher, UserRole.school_admin):
        return None
    try:
        cid = uuid.UUID(requested)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid classroom_id")

    from backend.db.models import Classroom, TeacherSubjectAssignment

    cls = await session.scalar(
        select(Classroom).where(
            Classroom.id == cid,
            Classroom.tenant_id == user.tenant_id,
        )
    )
    if not cls:
        raise HTTPException(status_code=404, detail="classroom not found")

    # Teachers can only scope to classrooms they teach (home grade or subject).
    if user.role is UserRole.teacher:
        is_home = cls.teacher_id == user.id
        if not is_home:
            sa = await session.scalar(
                select(TeacherSubjectAssignment.id).where(
                    TeacherSubjectAssignment.teacher_id == user.id,
                    TeacherSubjectAssignment.classroom_id == cid,
                )
            )
            if sa is None:
                raise HTTPException(status_code=403, detail="not your classroom")
    return requested
