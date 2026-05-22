"""Student profile + parent children endpoints."""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import func, select

from backend.api.deps import CurrentUser, SessionDep, assert_tenant
from backend.db.models import (
    Attendance, AttendanceStatus, Classroom, ParentStudent, Student, User, UserRole,
)

router = APIRouter(tags=["students"])


class StudentProfileOut(BaseModel):
    id: str
    handle: str
    full_name: str
    grade: int
    section: str
    classroom_id: str | None
    classroom_name: str | None
    parent_phone: str | None
    attendance_30d_pct: float | None
    created_at: datetime


class ChildOut(BaseModel):
    id: str
    handle: str
    full_name: str
    grade: int
    section: str
    classroom_name: str | None


async def _attendance_pct_30d(session, student_id: uuid.UUID) -> float | None:
    cutoff = date.today() - timedelta(days=30)
    rows = (await session.execute(
        select(Attendance.status, func.count(Attendance.id))
        .where(Attendance.student_id == student_id, Attendance.date >= cutoff)
        .group_by(Attendance.status)
    )).all()
    by_status = {s: int(c) for s, c in rows}
    present = by_status.get(AttendanceStatus.present, 0)
    late = by_status.get(AttendanceStatus.late, 0)
    absent = by_status.get(AttendanceStatus.absent, 0)
    total = present + absent + late
    return round((present + late) / total * 100, 1) if total else None


def _can_view_student(user: User, student: Student) -> bool:
    if user.role in {UserRole.school_admin, UserRole.teacher} and user.tenant_id == student.tenant_id:
        return True
    if user.role is UserRole.student:
        return getattr(user, "_student_id", None) == student.id
    # parent check is done at the endpoint (needs DB lookup)
    return False


@router.get("/students/{student_id}", response_model=StudentProfileOut)
async def get_student_profile(
    student_id: str,
    user: CurrentUser,
    session: SessionDep,
) -> StudentProfileOut:
    sid = uuid.UUID(student_id)
    student = await session.get(Student, sid)
    if not student:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "student not found")

    if not _can_view_student(user, student):
        # Parent path: must have ParentStudent link
        if user.role is UserRole.parent:
            link = (await session.execute(
                select(ParentStudent).where(
                    ParentStudent.parent_id == user.id,
                    ParentStudent.student_id == sid,
                )
            )).scalar_one_or_none()
            if link is None:
                raise HTTPException(status.HTTP_403_FORBIDDEN, "not your child")
        else:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden")

    classroom_name: str | None = None
    if student.classroom_id:
        cls = await session.get(Classroom, student.classroom_id)
        classroom_name = cls.name if cls else None

    pct = await _attendance_pct_30d(session, student.id)

    return StudentProfileOut(
        id=str(student.id),
        handle=student.handle,
        full_name=student.full_name,
        grade=student.grade,
        section=student.section,
        classroom_id=str(student.classroom_id) if student.classroom_id else None,
        classroom_name=classroom_name,
        parent_phone=student.parent_phone,
        attendance_30d_pct=pct,
        created_at=student.created_at,
    )


class AttendanceDayRow(BaseModel):
    date: date
    status: str   # present | absent | late | unmarked


class AttendanceSummary(BaseModel):
    present: int
    absent: int
    late: int
    unmarked: int
    total_days: int            # working-day count in the range (Mon–Sat by default)
    attendance_pct: float | None


class AttendanceHistoryOut(BaseModel):
    student_id: str
    student_name: str
    from_date: date
    to_date: date
    days: list[AttendanceDayRow]
    summary: AttendanceSummary


def _working_days(start: date, end: date) -> list[date]:
    """Mon–Sat. Sunday is the only off day in the school calendar by default."""
    out: list[date] = []
    d = start
    while d <= end:
        if d.isoweekday() != 7:
            out.append(d)
        d += timedelta(days=1)
    return out


async def _assert_can_view_student(
    user: User, student: Student, session
) -> None:
    if _can_view_student(user, student):
        return
    if user.role is UserRole.parent:
        link = await session.scalar(
            select(ParentStudent).where(
                ParentStudent.parent_id == user.id,
                ParentStudent.student_id == student.id,
            )
        )
        if link is not None:
            return
    raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden")


@router.get(
    "/students/{student_id}/attendance",
    response_model=AttendanceHistoryOut,
)
async def student_attendance_history(
    student_id: str,
    user: CurrentUser,
    session: SessionDep,
    from_date: date | None = None,
    to_date: date | None = None,
) -> AttendanceHistoryOut:
    """Daily attendance for one student over a date range.
    Defaults to the trailing 30 days. Parents may only see their own children;
    students may only see themselves; teachers/school admins see anyone in
    their school.
    """
    # "me" → current student's own attendance (only valid for student role)
    if student_id == "me":
        sid_from_token = getattr(user, "_student_id", None)
        if user.role is not UserRole.student or sid_from_token is None:
            raise HTTPException(400, "'me' is only valid for the logged-in student")
        sid = sid_from_token
    else:
        try:
            sid = uuid.UUID(student_id)
        except ValueError:
            raise HTTPException(400, "invalid student id")
    student = await session.get(Student, sid)
    if not student:
        raise HTTPException(404, "student not found")
    await _assert_can_view_student(user, student, session)

    today = date.today()
    end = to_date or today
    start = from_date or (end - timedelta(days=29))
    if start > end:
        raise HTTPException(400, "from_date must be on or before to_date")
    if (end - start).days > 366:
        raise HTTPException(400, "date range cannot exceed one year")

    rows = (await session.execute(
        select(Attendance.date, Attendance.status)
        .where(
            Attendance.student_id == student.id,
            Attendance.date >= start,
            Attendance.date <= end,
        )
        .order_by(Attendance.date)
    )).all()
    by_date: dict[date, str] = {d: s.value for d, s in rows}

    days = []
    p = a = l = u = 0
    for d in _working_days(start, end):
        status_val = by_date.get(d, "unmarked")
        days.append(AttendanceDayRow(date=d, status=status_val))
        if status_val == "present": p += 1
        elif status_val == "absent": a += 1
        elif status_val == "late":   l += 1
        else: u += 1

    marked = p + a + l
    pct = round((p + l) / marked * 100, 1) if marked else None
    summary = AttendanceSummary(
        present=p, absent=a, late=l, unmarked=u,
        total_days=len(days), attendance_pct=pct,
    )

    return AttendanceHistoryOut(
        student_id=str(student.id),
        student_name=student.full_name,
        from_date=start,
        to_date=end,
        days=days,
        summary=summary,
    )


@router.get("/parent/children", response_model=list[ChildOut])
async def list_children(user: CurrentUser, session: SessionDep) -> list[ChildOut]:
    if user.role is not UserRole.parent:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "parents only")
    tid = assert_tenant(user)

    # Scope to the parent's own tenant. `_find_or_create_parent` already
    # refuses to link a child to a cross-tenant parent, but this filter is
    # defence in depth for any historical rows that pre-date that check.
    rows = (await session.execute(
        select(Student, Classroom.name)
        .join(ParentStudent, ParentStudent.student_id == Student.id)
        .outerjoin(Classroom, Classroom.id == Student.classroom_id)
        .where(
            ParentStudent.parent_id == user.id,
            Student.tenant_id == tid,
        )
        .order_by(Student.grade, Student.full_name)
    )).all()

    return [
        ChildOut(
            id=str(s.id),
            handle=s.handle,
            full_name=s.full_name,
            grade=s.grade,
            section=s.section,
            classroom_name=cls_name,
        )
        for s, cls_name in rows
    ]
