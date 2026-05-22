"""School-wide analytics for school admin."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select

from backend.api.deps import CurrentUser, SessionDep, assert_tenant, require_roles
from backend.db.models import (
    Assessment, Assignment, Attendance, AttendanceStatus, Classroom,
    Question, QuestionResponse, Student, User, UserRole,
)

router = APIRouter(prefix="/school/analytics", tags=["school-analytics"])
SchoolAdminDep = Annotated[User, Depends(require_roles(UserRole.school_admin))]


class HeadcountOut(BaseModel):
    students: int
    teachers: int
    parents: int
    classrooms: int


class GradeBreakdown(BaseModel):
    grade: int
    student_count: int


class AttendanceSummary(BaseModel):
    window_days: int
    overall_rate: float | None
    present: int
    absent: int
    late: int


class SubjectAverage(BaseModel):
    subject: str
    avg_pct: float
    assessments: int


class SchoolOverviewOut(BaseModel):
    headcount: HeadcountOut
    grades: list[GradeBreakdown]
    attendance_30d: AttendanceSummary
    subject_averages: list[SubjectAverage]
    total_assignments: int
    total_assessments: int


@router.get("/overview", response_model=SchoolOverviewOut)
async def school_overview(
    user: SchoolAdminDep,
    session: SessionDep,
) -> SchoolOverviewOut:
    tenant_id = assert_tenant(user)

    student_count = (await session.execute(
        select(func.count(Student.id)).where(Student.tenant_id == tenant_id)
    )).scalar_one()

    teacher_count = (await session.execute(
        select(func.count(User.id)).where(
            User.tenant_id == tenant_id, User.role == UserRole.teacher
        )
    )).scalar_one()

    parent_count = (await session.execute(
        select(func.count(User.id)).where(
            User.tenant_id == tenant_id, User.role == UserRole.parent
        )
    )).scalar_one()

    classroom_count = (await session.execute(
        select(func.count(Classroom.id)).where(Classroom.tenant_id == tenant_id)
    )).scalar_one()

    grade_rows = (await session.execute(
        select(Student.grade, func.count(Student.id))
        .where(Student.tenant_id == tenant_id)
        .group_by(Student.grade)
        .order_by(Student.grade)
    )).all()
    grades = [GradeBreakdown(grade=g, student_count=c) for g, c in grade_rows]

    # Attendance, last 30 days
    cutoff = date.today() - timedelta(days=30)
    att_rows = (await session.execute(
        select(Attendance.status, func.count(Attendance.id))
        .join(Student, Student.id == Attendance.student_id)
        .where(Student.tenant_id == tenant_id, Attendance.date >= cutoff)
        .group_by(Attendance.status)
    )).all()
    by_status = {s: int(c) for s, c in att_rows}
    present = by_status.get(AttendanceStatus.present, 0)
    absent = by_status.get(AttendanceStatus.absent, 0)
    late = by_status.get(AttendanceStatus.late, 0)
    total_att = present + absent + late
    rate = round((present + late) / total_att * 100, 1) if total_att else None

    # Subject averages — across question responses, marks / max_marks per assessment
    subj_rows = (await session.execute(
        select(
            Assessment.subject_id,
            func.avg(QuestionResponse.marks_obtained / Question.max_marks * 100).label("avg_pct"),
            func.count(func.distinct(Assessment.id)).label("n"),
        )
        .join(Question, Question.assessment_id == Assessment.id)
        .join(QuestionResponse, QuestionResponse.question_id == Question.id)
        .join(Student, Student.id == QuestionResponse.student_id)
        .where(Student.tenant_id == tenant_id)
        .group_by(Assessment.subject_id)
    )).all()

    # Map subject_id -> name (subjects table is global by name)
    from backend.db.models import Subject
    subject_names = {
        sid: name
        for sid, name in (await session.execute(select(Subject.id, Subject.name))).all()
    }
    subject_averages = [
        SubjectAverage(
            subject=subject_names.get(sid, "Unknown"),
            avg_pct=round(float(avg or 0), 1),
            assessments=int(n),
        )
        for sid, avg, n in subj_rows
    ]

    total_assignments = (await session.execute(
        select(func.count(Assignment.id)).where(Assignment.tenant_id == tenant_id)
    )).scalar_one()

    total_assessments = (await session.execute(
        select(func.count(func.distinct(Assessment.id)))
        .join(Question, Question.assessment_id == Assessment.id)
        .join(QuestionResponse, QuestionResponse.question_id == Question.id)
        .join(Student, Student.id == QuestionResponse.student_id)
        .where(Student.tenant_id == tenant_id)
    )).scalar_one()

    return SchoolOverviewOut(
        headcount=HeadcountOut(
            students=int(student_count or 0),
            teachers=int(teacher_count or 0),
            parents=int(parent_count or 0),
            classrooms=int(classroom_count or 0),
        ),
        grades=grades,
        attendance_30d=AttendanceSummary(
            window_days=30,
            overall_rate=rate,
            present=present,
            absent=absent,
            late=late,
        ),
        subject_averages=subject_averages,
        total_assignments=int(total_assignments or 0),
        total_assessments=int(total_assessments or 0),
    )
