"""
Academic year management + student promotion.
Accessible by school_admin role only.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import func, select, update

from backend.api.deps import SessionDep, assert_tenant, require_roles
from backend.db.models import (
    AcademicYear, Classroom, Student, StudentClassHistory, User, UserRole,
)

router = APIRouter(prefix="/school/academic-years", tags=["academic-years"])
SchoolAdminDep = Annotated[User, Depends(require_roles(UserRole.school_admin))]


# ── Schemas ──────────────────────────────────────────────


class AcademicYearIn(BaseModel):
    name: str                 # e.g. "2025-2026"
    start_date: date
    end_date: date


class AcademicYearOut(BaseModel):
    id: str
    name: str
    start_date: date
    end_date: date
    is_current: bool
    created_at: datetime


class PromoteIn(BaseModel):
    new_year_id: str          # the AcademicYear students are moving INTO
    max_grade: int = 12       # students at this grade are graduated
    # Optional filters — when both omitted, every enrolled student is promoted.
    # When classroom_ids is set, only students currently in those classrooms.
    # When student_ids is set, only those specific students (must intersect
    # classroom_ids if both are set).
    classroom_ids: list[str] | None = None
    student_ids: list[str] | None = None


class PromotedStudent(BaseModel):
    id: str
    name: str
    from_class: str
    to_class: str


class PromoteResult(BaseModel):
    promoted: int
    graduated: int
    unassigned: int           # no classroom with space at next grade
    details_promoted: list[PromotedStudent]
    details_graduated: list[str]
    details_unassigned: list[str]


# ── Endpoints ────────────────────────────────────────────


@router.get("", response_model=list[AcademicYearOut])
async def list_years(user: SchoolAdminDep, session: SessionDep) -> list[AcademicYearOut]:
    tid = assert_tenant(user)
    result = await session.execute(
        select(AcademicYear).where(AcademicYear.tenant_id == tid)
        .order_by(AcademicYear.start_date.desc())
    )
    return [_year_out(y) for y in result.scalars().all()]


@router.post("", response_model=AcademicYearOut, status_code=status.HTTP_201_CREATED)
async def create_year(
    body: AcademicYearIn, user: SchoolAdminDep, session: SessionDep
) -> AcademicYearOut:
    tid = assert_tenant(user)
    _validate_year_dates(body.start_date, body.end_date)
    if await session.scalar(
        select(AcademicYear).where(
            AcademicYear.tenant_id == tid, AcademicYear.name == body.name
        )
    ):
        raise HTTPException(409, f"Academic year '{body.name}' already exists")
    await _assert_no_overlap(session, tid, body.start_date, body.end_date)

    year = AcademicYear(
        id=uuid.uuid4(),
        tenant_id=tid,
        name=body.name,
        start_date=body.start_date,
        end_date=body.end_date,
        is_current=False,
    )
    session.add(year)
    await session.flush()
    await session.refresh(year)
    return _year_out(year)


class AcademicYearPatch(BaseModel):
    name: str | None = None
    start_date: date | None = None
    end_date: date | None = None


@router.patch("/{year_id}", response_model=AcademicYearOut)
async def update_year(
    year_id: str, body: AcademicYearPatch, user: SchoolAdminDep, session: SessionDep
) -> AcademicYearOut:
    tid = assert_tenant(user)
    year = await _get_year(year_id, tid, session)

    new_name = body.name if body.name is not None else year.name
    new_start = body.start_date if body.start_date is not None else year.start_date
    new_end = body.end_date if body.end_date is not None else year.end_date

    _validate_year_dates(new_start, new_end)

    if new_name != year.name:
        clash = await session.scalar(
            select(AcademicYear).where(
                AcademicYear.tenant_id == tid,
                AcademicYear.name == new_name,
                AcademicYear.id != year.id,
            )
        )
        if clash:
            raise HTTPException(409, f"Academic year '{new_name}' already exists")

    if new_start != year.start_date or new_end != year.end_date:
        await _assert_no_overlap(session, tid, new_start, new_end, exclude_id=year.id)

    year.name = new_name
    year.start_date = new_start
    year.end_date = new_end
    await session.flush()
    await session.refresh(year)
    return _year_out(year)


@router.delete("/{year_id}", status_code=200)
async def delete_year(
    year_id: str, user: SchoolAdminDep, session: SessionDep
) -> dict:
    """Delete an academic year. Refuses if it's the current year or has history."""
    from sqlalchemy import delete as sql_delete
    tid = assert_tenant(user)
    year = await _get_year(year_id, tid, session)
    if year.is_current:
        raise HTTPException(409, "Cannot delete the current academic year. Set another year as current first.")
    has_history = await session.scalar(
        select(func.count()).select_from(StudentClassHistory)
        .where(StudentClassHistory.academic_year_id == year.id)
    )
    if has_history:
        raise HTTPException(409, "Cannot delete: this year has promotion history.")
    await session.execute(sql_delete(AcademicYear).where(AcademicYear.id == year.id))
    await session.flush()
    return {"deleted": True}


@router.patch("/{year_id}/set-current", response_model=AcademicYearOut)
async def set_current_year(
    year_id: str, user: SchoolAdminDep, session: SessionDep
) -> AcademicYearOut:
    """Mark one year as current — clears is_current on all other years for this school."""
    tid = assert_tenant(user)
    year = await _get_year(year_id, tid, session)

    # Clear existing current flag
    await session.execute(
        update(AcademicYear)
        .where(AcademicYear.tenant_id == tid)
        .values(is_current=False)
    )
    year.is_current = True
    await session.flush()
    return _year_out(year)


@router.post("/{year_id}/promote", response_model=PromoteResult)
async def promote_students(
    year_id: str, body: PromoteIn, user: SchoolAdminDep, session: SessionDep
) -> PromoteResult:
    """
    Promote every student to the next grade's classroom.

    Steps:
      1. Record each student's CURRENT classroom in StudentClassHistory
         (linked to the CLOSING year = the year being finished).
      2. Move student.classroom_id to the matching classroom at grade+1.
      3. Students at max_grade are graduated (classroom_id = NULL).
      4. Students where no classroom has space are flagged unassigned.
    """
    tid = assert_tenant(user)

    # The year we are CLOSING — taken from the URL path so the action is
    # explicit. Must be the current active year (only the live cohort can
    # be promoted; bulk-rewriting history would corrupt past snapshots).
    closing_year = await _get_year(year_id, tid, session)
    if not closing_year.is_current:
        raise HTTPException(
            409,
            "Only the current academic year can be promoted from. "
            "Set it as current first.",
        )

    # The year students are moving INTO
    new_year = await _get_year(body.new_year_id, tid, session)
    if new_year.id == closing_year.id:
        raise HTTPException(400, "Target year must differ from the closing year.")

    # Load all classrooms in this school
    cls_result = await session.execute(
        select(Classroom).where(Classroom.tenant_id == tid)
    )
    classrooms = cls_result.scalars().all()

    # Lookup maps
    cls_by_id: dict[uuid.UUID, Classroom] = {c.id: c for c in classrooms}
    # (grade, section) → list of classrooms (sorted by capacity available, desc)
    cls_by_grade: dict[int, list[Classroom]] = {}
    for c in classrooms:
        cls_by_grade.setdefault(c.grade, []).append(c)

    # Current enrollment count per classroom
    cnt_result = await session.execute(
        select(Student.classroom_id, func.count().label("n"))
        .where(Student.tenant_id == tid, Student.classroom_id.isnot(None))
        .group_by(Student.classroom_id)
    )
    enrolled: dict[uuid.UUID, int] = {row.classroom_id: row.n for row in cnt_result}

    # Parse optional scope filters
    filter_cls_ids: set[uuid.UUID] | None = None
    if body.classroom_ids is not None:
        filter_cls_ids = set()
        for cid_str in body.classroom_ids:
            try:
                filter_cls_ids.add(uuid.UUID(cid_str))
            except ValueError:
                raise HTTPException(400, f"invalid classroom_id: {cid_str}")
        if not filter_cls_ids:
            raise HTTPException(400, "classroom_ids cannot be empty when provided")

    filter_stu_ids: set[uuid.UUID] | None = None
    if body.student_ids is not None:
        filter_stu_ids = set()
        for sid_str in body.student_ids:
            try:
                filter_stu_ids.add(uuid.UUID(sid_str))
            except ValueError:
                raise HTTPException(400, f"invalid student_id: {sid_str}")
        if not filter_stu_ids:
            raise HTTPException(400, "student_ids cannot be empty when provided")

    # All students currently assigned to a classroom in this school
    stu_stmt = (
        select(Student)
        .where(Student.tenant_id == tid, Student.classroom_id.isnot(None))
        .order_by(Student.grade, Student.full_name)
    )
    if filter_cls_ids is not None:
        stu_stmt = stu_stmt.where(Student.classroom_id.in_(list(filter_cls_ids)))
    if filter_stu_ids is not None:
        stu_stmt = stu_stmt.where(Student.id.in_(list(filter_stu_ids)))

    stu_result = await session.execute(stu_stmt)
    students = stu_result.scalars().all()

    detail_promoted: list[PromotedStudent] = []
    detail_graduated: list[str] = []
    detail_unassigned: list[str] = []

    history_to_add: list[StudentClassHistory] = []
    now = datetime.now(timezone.utc)

    for student in students:
        current_cls = cls_by_id.get(student.classroom_id)
        if not current_cls:
            continue

        # ── 1. Record history for closing year ───────────
        if closing_year:
            history_to_add.append(StudentClassHistory(
                id=uuid.uuid4(),
                student_id=student.id,
                classroom_id=current_cls.id,
                academic_year_id=closing_year.id,
                promoted_at=now,
            ))

        next_grade = current_cls.grade + 1

        # ── 2. Graduate if at max grade ──────────────────
        if next_grade > body.max_grade:
            student.classroom_id = None
            detail_graduated.append(student.full_name)
            continue

        # ── 3. Find classroom at next grade ──────────────
        candidates = cls_by_grade.get(next_grade, [])
        next_cls: Classroom | None = None

        # Prefer same section
        for c in candidates:
            if c.section == current_cls.section and enrolled.get(c.id, 0) < c.capacity:
                next_cls = c
                break

        # Fall back to any section with space
        if not next_cls:
            for c in sorted(candidates, key=lambda x: enrolled.get(x.id, 0)):
                if enrolled.get(c.id, 0) < c.capacity:
                    next_cls = c
                    break

        if not next_cls:
            student.classroom_id = None
            detail_unassigned.append(
                f"{student.full_name} (Grade {next_grade} — no space)"
            )
            continue

        # ── 4. Promote ───────────────────────────────────
        from_label = f"Gr {current_cls.grade}{current_cls.section}"
        to_label   = f"Gr {next_cls.grade}{next_cls.section}"
        student.classroom_id = next_cls.id
        student.grade = next_grade
        student.section = next_cls.section
        enrolled[next_cls.id] = enrolled.get(next_cls.id, 0) + 1

        detail_promoted.append(PromotedStudent(
            id=str(student.id), name=student.full_name,
            from_class=from_label, to_class=to_label,
        ))

    # ── 5. Persist history + (optionally) activate new year ──
    if history_to_add:
        session.add_all(history_to_add)

    # Only flip the active academic year when EVERY enrolled student was
    # promoted at once — otherwise a partial promotion would mark mid-year
    # snapshots as "history" for students who never moved.
    is_full_promotion = filter_cls_ids is None and filter_stu_ids is None
    if is_full_promotion:
        await session.execute(
            update(AcademicYear)
            .where(AcademicYear.tenant_id == tid)
            .values(is_current=False)
        )
        new_year.is_current = True
    await session.flush()

    return PromoteResult(
        promoted=len(detail_promoted),
        graduated=len(detail_graduated),
        unassigned=len(detail_unassigned),
        details_promoted=detail_promoted[:50],
        details_graduated=detail_graduated[:50],
        details_unassigned=detail_unassigned[:50],
    )


@router.get("/{year_id}/history", response_model=list[dict])
async def year_history(
    year_id: str, user: SchoolAdminDep, session: SessionDep
) -> list[dict]:
    """Full class list snapshot for a past academic year."""
    tid = assert_tenant(user)
    year = await _get_year(year_id, tid, session)
    result = await session.execute(
        select(StudentClassHistory)
        .where(StudentClassHistory.academic_year_id == year.id)
        .order_by(StudentClassHistory.enrolled_at)
    )
    rows = result.scalars().all()
    out = []
    for r in rows:
        stu = await session.get(Student, r.student_id)
        cls = await session.get(Classroom, r.classroom_id)
        out.append({
            "student_id":  str(r.student_id),
            "student_name": stu.full_name if stu else "?",
            "classroom":   cls.name if cls else "?",
            "grade":       cls.grade if cls else None,
            "section":     cls.section if cls else None,
            "enrolled_at": r.enrolled_at.isoformat(),
            "promoted_at": r.promoted_at.isoformat() if r.promoted_at else None,
        })
    return out


# ── Helpers ───────────────────────────────────────────────


def _year_out(y: AcademicYear) -> AcademicYearOut:
    return AcademicYearOut(
        id=str(y.id), name=y.name,
        start_date=y.start_date, end_date=y.end_date,
        is_current=y.is_current, created_at=y.created_at,
    )


def _validate_year_dates(start: date, end: date) -> None:
    if end <= start:
        raise HTTPException(400, "End date must be after start date.")


async def _assert_no_overlap(
    session,
    tenant_id: uuid.UUID,
    start: date,
    end: date,
    exclude_id: uuid.UUID | None = None,
) -> None:
    """Reject ranges that intersect any existing year for this school."""
    stmt = select(AcademicYear).where(
        AcademicYear.tenant_id == tenant_id,
        AcademicYear.start_date <= end,
        AcademicYear.end_date >= start,
    )
    if exclude_id is not None:
        stmt = stmt.where(AcademicYear.id != exclude_id)
    clash = await session.scalar(stmt)
    if clash:
        raise HTTPException(
            409,
            f"Date range overlaps existing year '{clash.name}' "
            f"({clash.start_date} → {clash.end_date}).",
        )


async def _get_year(
    year_id: str, tenant_id: uuid.UUID, session: object
) -> AcademicYear:
    try:
        yid = uuid.UUID(year_id)
    except ValueError:
        raise HTTPException(400, "invalid year id")
    result = await session.execute(  # type: ignore[union-attr]
        select(AcademicYear).where(
            AcademicYear.id == yid, AcademicYear.tenant_id == tenant_id
        )
    )
    year = result.scalar_one_or_none()
    if not year:
        raise HTTPException(404, "academic year not found")
    return year
