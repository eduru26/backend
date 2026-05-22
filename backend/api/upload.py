"""
POST /upload/excel   — teacher uploads any Excel/CSV file.
The LLM extracts structured data; the backend writes it to the DB.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import select

from backend.api.deps import SessionDep, assert_tenant, require_roles
from backend.db.models import (
    AssessmentType,
    AttendanceStatus,
    Conversation,
    Message,
    MessageRole,
    QuestionDifficulty,
    User,
    UserRole,
)
from backend.db.repositories.assessments import AssessmentRepository
from backend.db.repositories.attendance import AttendanceRepository
from backend.db.repositories.responses import ResponseRepository
from backend.db.repositories.students import StudentRepository
from backend.db.repositories.subjects import SubjectRepository
from backend.core.logging import get_logger
from backend.llm.excel_processor import process_file

router = APIRouter(prefix="/upload", tags=["upload"])
TeacherDep = Annotated[User, Depends(require_roles(UserRole.teacher))]
logger = get_logger(__name__)


class UploadResult(BaseModel):
    summary: str
    students_saved: int = 0
    attendance_saved: int = 0
    assessments_saved: int = 0
    marks_saved: int = 0
    warnings: list[str] = []
    message: str = ""  # chat-friendly formatted summary


@router.post("/excel", response_model=UploadResult)
async def upload_excel(
    file: UploadFile,
    session: SessionDep,
    user: TeacherDep,
    conversation_id: str | None = None,
) -> UploadResult:
    tenant_id = assert_tenant(user)
    filename = file.filename or "upload"
    data = await file.read()
    if not data:
        raise HTTPException(400, "empty file")

    # ── LLM extraction ──────────────────────────────────────
    try:
        extracted = await process_file(filename, data)
    except ValueError as exc:
        logger.warning("upload.parse_error", filename=filename, error=str(exc))
        raise HTTPException(422, str(exc))
    except Exception as exc:
        logger.exception("upload.llm_failed", filename=filename, error=str(exc))
        raise HTTPException(500, f"File processing failed: {exc}")

    result = UploadResult(summary=extracted.get("summary", "Data extracted"))
    warnings: list[str] = []

    # ── 1. Students ─────────────────────────────────────────
    student_rows = extracted.get("students", [])
    if student_rows:
        repo = StudentRepository(session)
        valid: list[dict] = []
        for s in student_rows:
            handle = _str(s.get("handle"))
            full_name = _str(s.get("full_name"))
            if not handle or not full_name:
                warnings.append(f"Skipped student row with missing handle/name: {s}")
                continue
            valid.append({
                "handle": handle[:64],
                "full_name": full_name[:200],
                "grade": _int(s.get("grade"), default=1),
                "section": _str(s.get("section", "A"))[:8],
                "parent_email": _str(s.get("parent_email")) or None,
            })
        if valid:
            result.students_saved = await repo.upsert_many(valid, tenant_id)

    # ── 2. Attendance ────────────────────────────────────────
    att_data = extracted.get("attendance", [])
    if att_data:
        students_repo = StudentRepository(session)
        att_repo = AttendanceRepository(session)
        att_rows: list[dict] = []
        for a in att_data:
            handle = _str(a.get("student_handle"))
            raw_date = a.get("date")
            if not handle or not raw_date:
                continue
            parsed_date = _parse_date(raw_date)
            if not parsed_date:
                warnings.append(f"Bad date '{raw_date}' for student '{handle}' — skipped")
                continue
            student = await students_repo.get_by_handle(handle, tenant_id)
            if not student:
                warnings.append(f"Attendance: unknown student handle '{handle}'")
                continue
            status = _att_status(a.get("status", "present"))
            att_rows.append({"student_id": student.id, "date": parsed_date, "status": status})
        if att_rows:
            result.attendance_saved = await att_repo.bulk_upsert(att_rows)

    # ── 3. Assessments + marks ───────────────────────────────
    for asmnt in extracted.get("assessments", []):
        try:
            n_marks = await _save_assessment(asmnt, tenant_id, user.id, session, warnings)
            result.assessments_saved += 1
            result.marks_saved += n_marks
        except Exception as exc:
            logger.exception("upload.assessment_save_failed", title=asmnt.get("title"), error=str(exc))
            warnings.append(f"Assessment '{asmnt.get('title')}' skipped: {exc}")

    result.warnings = warnings[:10]  # cap noise

    # ── 4. Build chat message ────────────────────────────────
    lines = [f"✅ **{filename}** processed — {result.summary}"]
    if result.students_saved:
        lines.append(f"• {result.students_saved} student record(s) saved")
    if result.attendance_saved:
        lines.append(f"• {result.attendance_saved} attendance record(s) saved")
    if result.assessments_saved:
        lines.append(
            f"• {result.assessments_saved} assessment(s) created "
            f"with {result.marks_saved} mark record(s)"
        )
    if not (result.students_saved or result.attendance_saved or result.assessments_saved):
        lines.append("⚠️ No data was recognised. Check the file format.")
    if warnings:
        lines.append(f"⚠️ {len(warnings)} warning(s): " + "; ".join(warnings[:3]))
    result.message = "\n".join(lines)

    # ── 5. Persist as chat messages ──────────────────────────
    if conversation_id:
        await _append_to_conversation(
            conversation_id=conversation_id,
            user_id=user.id,
            user_text=f"📎 Uploaded file: {filename}",
            assistant_text=result.message,
            session=session,
        )

    return result


# ── Helpers ───────────────────────────────────────────────


async def _save_assessment(
    data: dict,
    tenant_id: uuid.UUID,
    created_by: uuid.UUID,
    session: object,
    warnings: list[str],
) -> int:
    subjects_repo = SubjectRepository(session)  # type: ignore[arg-type]
    assessments_repo = AssessmentRepository(session)  # type: ignore[arg-type]
    students_repo = StudentRepository(session)  # type: ignore[arg-type]

    subject_name = _str(data.get("subject", "General"))
    subject = await subjects_repo.upsert_subject(subject_name)

    atype = _assessment_type(data.get("type", "unit_test"))
    raw_date = data.get("date") or str(date.today())
    adate = _parse_date(raw_date) or date.today()

    questions_raw = data.get("questions") or [
        {"question_no": 1, "topic": subject_name, "max_marks": 100, "difficulty": "medium"}
    ]
    total_max = sum(_decimal(q.get("max_marks", 10)) for q in questions_raw)

    assessment = await assessments_repo.create(
        type_=atype,
        title=_str(data.get("title", "Assessment"))[:200],
        subject_id=subject.id,
        assessment_date=adate,
        max_marks=total_max,
        created_by=created_by,
    )

    # Topics + questions
    topic_cache: dict[str, uuid.UUID] = {}
    q_rows: list[dict] = []
    for q in questions_raw:
        topic_name = _str(q.get("topic", subject_name))
        if topic_name not in topic_cache:
            topic = await subjects_repo.upsert_topic(subject_id=subject.id, name=topic_name)
            topic_cache[topic_name] = topic.id
        q_rows.append({
            "assessment_id": assessment.id,
            "question_no": _int(q.get("question_no"), default=1),
            "topic_id": topic_cache[topic_name],
            "max_marks": _decimal(q.get("max_marks", 10)),
            "difficulty": _difficulty(q.get("difficulty", "medium")),
        })

    created_qs = await assessments_repo.bulk_create_questions(q_rows)
    q_map = {q.question_no: q for q in created_qs}

    # Marks
    marks_rows: list[dict] = []
    for m in data.get("marks", []):
        handle = _str(m.get("student_handle", ""))
        if not handle:
            continue
        student = await students_repo.get_by_handle(handle, tenant_id)
        if not student:
            warnings.append(f"Marks: unknown student handle '{handle}'")
            continue
        qno = _int(m.get("question_no"), default=1)
        question = q_map.get(qno)
        if not question:
            continue
        obtained = min(_decimal(m.get("marks_obtained", 0)), question.max_marks)
        marks_rows.append({
            "question_id": question.id,
            "student_id": student.id,
            "marks_obtained": obtained,
            "is_correct": obtained >= question.max_marks,
        })

    if marks_rows:
        await ResponseRepository(session).bulk_upsert(marks_rows)  # type: ignore[arg-type]

    return len(marks_rows)


async def _append_to_conversation(
    *,
    conversation_id: str,
    user_id: uuid.UUID,
    user_text: str,
    assistant_text: str,
    session: object,
) -> None:
    try:
        cid = uuid.UUID(conversation_id)
    except ValueError:
        return
    result = await session.execute(  # type: ignore[union-attr]
        select(Conversation).where(
            Conversation.id == cid, Conversation.user_id == user_id
        )
    )
    if not result.scalar_one_or_none():
        return
    session.add(Message(conversation_id=cid, role=MessageRole.user, content=user_text))  # type: ignore[union-attr]
    session.add(Message(conversation_id=cid, role=MessageRole.assistant, content=assistant_text))  # type: ignore[union-attr]
    await session.flush()  # type: ignore[union-attr]


# ── Type coercions ────────────────────────────────────────

def _str(v: object) -> str:
    return str(v).strip() if v is not None else ""


def _int(v: object, *, default: int = 0) -> int:
    try:
        return int(str(v).strip().split(".")[0])
    except (ValueError, TypeError):
        return default


def _decimal(v: object) -> Decimal:
    try:
        return Decimal(str(v).strip())
    except InvalidOperation:
        return Decimal("0")


def _parse_date(v: object) -> date | None:
    s = str(v).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _att_status(v: object) -> AttendanceStatus:
    s = str(v).strip().lower()
    mapping = {
        "present": AttendanceStatus.present, "p": AttendanceStatus.present,
        "yes": AttendanceStatus.present, "1": AttendanceStatus.present,
        "absent": AttendanceStatus.absent, "a": AttendanceStatus.absent,
        "no": AttendanceStatus.absent, "0": AttendanceStatus.absent,
        "late": AttendanceStatus.late, "l": AttendanceStatus.late,
    }
    return mapping.get(s, AttendanceStatus.present)


def _assessment_type(v: object) -> AssessmentType:
    s = str(v).strip().lower()
    for t in AssessmentType:
        if t.value == s:
            return t
    return AssessmentType.unit_test


def _difficulty(v: object) -> QuestionDifficulty:
    s = str(v).strip().lower()
    for d in QuestionDifficulty:
        if d.value == s:
            return d
    return QuestionDifficulty.medium
