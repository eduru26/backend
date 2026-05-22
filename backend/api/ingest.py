from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, UploadFile, status

from backend.api.deps import SessionDep, assert_tenant, require_roles
from backend.core.csv_io import CSVValidationError, parse_csv_rows, sha256_hex
from backend.core.logging import get_logger
from backend.db.models import (
    AttendanceStatus,
    UploadKind,
    User,
    UserRole,
)
from backend.db.repositories.assessments import AssessmentRepository
from backend.db.repositories.attendance import AttendanceRepository
from backend.db.repositories.responses import ResponseRepository
from backend.db.repositories.students import StudentRepository
from backend.db.repositories.subjects import SubjectRepository
from backend.db.repositories.uploads import UploadRepository
from backend.models.ingestion import (
    AttendanceRowIn,
    IngestResult,
    MarksRowIn,
    QuestionBankIn,
    QuestionBankOut,
    StudentRowIn,
)

logger = get_logger(__name__)
router = APIRouter(prefix="/ingest", tags=["ingest"])

TeacherDep = Annotated[User, Depends(require_roles(UserRole.teacher))]


async def _read_upload(file: UploadFile) -> bytes:
    payload = await file.read()
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="empty upload",
        )
    return payload


async def _idempotency_check(
    *,
    sha256: str,
    session,
) -> dict | None:
    repo = UploadRepository(session)
    existing = await repo.find_by_sha256(sha256)
    if existing is None:
        return None
    return {
        "upload_id": existing.id,
        "rows_processed": existing.row_count,
        "duplicate": True,
        "detail": "identical payload already ingested",
    }


# ─── /ingest/students ────────────────────────────────────


@router.post("/students", response_model=IngestResult)
async def ingest_students(
    file: UploadFile,
    session: SessionDep,
    user: TeacherDep,
) -> IngestResult:
    payload = await _read_upload(file)
    digest = sha256_hex(payload)

    if dup := await _idempotency_check(sha256=digest, session=session):
        return IngestResult(**dup)

    try:
        rows = parse_csv_rows(payload, StudentRowIn)
    except CSVValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"line": exc.line_no, "errors": exc.errors},
        ) from exc

    tenant_id = assert_tenant(user)
    students = StudentRepository(session)
    db_rows = [
        {
            "handle": r.handle,
            "full_name": r.full_name,
            "grade": r.grade,
            "section": r.section,
            "parent_email": r.parent_email,
        }
        for r in rows
    ]
    n = await students.upsert_many(db_rows, tenant_id)

    upload = await UploadRepository(session).create(
        sha256=digest,
        kind=UploadKind.students,
        uploaded_by=user.id,
        row_count=n,
    )
    logger.info("ingest.students.ok", upload_id=str(upload.id), rows=n)
    return IngestResult(upload_id=upload.id, rows_processed=n)


# ─── /ingest/attendance ──────────────────────────────────


@router.post("/attendance", response_model=IngestResult)
async def ingest_attendance(
    file: UploadFile,
    session: SessionDep,
    user: TeacherDep,
) -> IngestResult:
    payload = await _read_upload(file)
    digest = sha256_hex(payload)

    if dup := await _idempotency_check(sha256=digest, session=session):
        return IngestResult(**dup)

    try:
        rows = parse_csv_rows(payload, AttendanceRowIn)
    except CSVValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"line": exc.line_no, "errors": exc.errors},
        ) from exc

    tenant_id = assert_tenant(user)
    students_repo = StudentRepository(session)
    handles = {r.student_handle for r in rows}
    handle_to_id: dict[str, object] = {}
    for h in handles:
        s = await students_repo.get_by_handle(h, tenant_id)
        if s is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"unknown student_handle: {h}",
            )
        handle_to_id[h] = s.id

    db_rows = [
        {
            "student_id": handle_to_id[r.student_handle],
            "date": r.date,
            "status": AttendanceStatus(r.status),
        }
        for r in rows
    ]
    n = await AttendanceRepository(session).bulk_upsert(db_rows)

    upload = await UploadRepository(session).create(
        sha256=digest,
        kind=UploadKind.attendance,
        uploaded_by=user.id,
        row_count=n,
    )
    logger.info("ingest.attendance.ok", upload_id=str(upload.id), rows=n)
    return IngestResult(upload_id=upload.id, rows_processed=n)


# ─── /ingest/question-bank ───────────────────────────────


@router.post("/question-bank", response_model=QuestionBankOut)
async def ingest_question_bank(
    payload: QuestionBankIn,
    session: SessionDep,
    user: TeacherDep,
) -> QuestionBankOut:
    raw = json.dumps(payload.model_dump(mode="json"), sort_keys=True).encode("utf-8")
    digest = sha256_hex(raw)

    uploads = UploadRepository(session)
    if existing := await uploads.find_by_sha256(digest):
        # We didn't store the assessment_id on the upload row; surface a clear duplicate
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "duplicate": True,
                "upload_id": str(existing.id),
                "message": "identical question bank already ingested",
            },
        )

    subjects = SubjectRepository(session)
    subject = await subjects.upsert_subject(payload.assessment.subject)

    assessments = AssessmentRepository(session)
    assessment = await assessments.create(
        type_=payload.assessment.type,
        title=payload.assessment.title,
        subject_id=subject.id,
        assessment_date=payload.assessment.assessment_date,
        max_marks=payload.assessment.max_marks,
        created_by=user.id,
    )

    topic_cache: dict[str, object] = {}
    question_rows: list[dict] = []
    for q in payload.questions:
        if q.topic not in topic_cache:
            topic = await subjects.upsert_topic(subject_id=subject.id, name=q.topic)
            topic_cache[q.topic] = topic.id
        question_rows.append(
            {
                "assessment_id": assessment.id,
                "question_no": q.question_no,
                "topic_id": topic_cache[q.topic],
                "max_marks": q.max_marks,
                "difficulty": q.difficulty,
            }
        )
    created = await assessments.bulk_create_questions(question_rows)

    upload = await uploads.create(
        sha256=digest,
        kind=UploadKind.question_bank,
        uploaded_by=user.id,
        row_count=len(created),
    )
    logger.info(
        "ingest.question_bank.ok",
        assessment_id=str(assessment.id),
        questions=len(created),
        upload_id=str(upload.id),
    )
    return QuestionBankOut(
        assessment_id=assessment.id,
        questions_created=len(created),
        upload_id=upload.id,
    )


# ─── /ingest/marks ───────────────────────────────────────


@router.post("/marks", response_model=IngestResult)
async def ingest_marks(
    file: UploadFile,
    session: SessionDep,
    user: TeacherDep,
) -> IngestResult:
    payload = await _read_upload(file)
    digest = sha256_hex(payload)

    if dup := await _idempotency_check(sha256=digest, session=session):
        return IngestResult(**dup)

    try:
        rows = parse_csv_rows(payload, MarksRowIn)
    except CSVValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"line": exc.line_no, "errors": exc.errors},
        ) from exc

    if not rows:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="no rows",
        )

    assessment_ids = {r.assessment_id for r in rows}
    if len(assessment_ids) != 1:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="all rows in a single upload must reference the same assessment_id",
        )
    assessment_id = next(iter(assessment_ids))

    assessments = AssessmentRepository(session)
    assessment = await assessments.get_by_id(assessment_id)
    if assessment is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"unknown assessment_id: {assessment_id}",
        )

    qmap = await assessments.map_question_nos(assessment_id)
    tenant_id = assert_tenant(user)
    students_repo = StudentRepository(session)
    handle_cache: dict[str, object] = {}
    db_rows: list[dict] = []
    for line_no, r in enumerate(rows, start=2):
        question = qmap.get(r.question_no)
        if question is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "line": line_no,
                    "errors": f"unknown question_no {r.question_no} in assessment",
                },
            )
        if r.marks_obtained > question.max_marks:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "line": line_no,
                    "errors": (
                        f"marks_obtained {r.marks_obtained} > question max_marks "
                        f"{question.max_marks}"
                    ),
                },
            )
        if r.student_handle not in handle_cache:
            student = await students_repo.get_by_handle(r.student_handle, tenant_id)
            if student is None:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail={"line": line_no, "errors": f"unknown student_handle {r.student_handle}"},
                )
            handle_cache[r.student_handle] = student.id
        db_rows.append(
            {
                "question_id": question.id,
                "student_id": handle_cache[r.student_handle],
                "marks_obtained": r.marks_obtained,
                "is_correct": r.marks_obtained == question.max_marks,
            }
        )

    n = await ResponseRepository(session).bulk_upsert(db_rows)

    upload = await UploadRepository(session).create(
        sha256=digest,
        kind=UploadKind.marks,
        uploaded_by=user.id,
        row_count=n,
    )

    # Notify parents whose children appear in this batch
    from backend.api._notify import notify_marks_posted
    affected_students = {row["student_id"] for row in db_rows}
    await notify_marks_posted(session, affected_students, assessment.title)

    logger.info(
        "ingest.marks.ok",
        upload_id=str(upload.id),
        assessment_id=str(assessment_id),
        rows=n,
    )
    return IngestResult(upload_id=upload.id, rows_processed=n)
